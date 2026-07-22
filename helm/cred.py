#!/usr/bin/env python3
"""helm cred — WHO a credential home actually holds, and the safe /login.

THE INCIDENT this exists for: a session hits its limit, the human runs
`/login` inside it, and Claude Code writes the NEW account into the config dir
that session is pinned to (a live session's CLAUDE_CONFIG_DIR is immutable —
we cannot redirect that write). The dir keeps its old NAME. From that moment
every helm verb that keys on the dir NAME attributes work, tokens and
transcripts to an account that is no longer there. The evicted account's
credentials are simply gone.

We do not fight the write. We make it NON-DESTRUCTIVE, TRUTHFUL, REVERSIBLE:

  * TRUTHFUL — identity comes from CONTENT, never from the directory name.
    `account_of()` reads <dir>/.claude.json's oauthAccount block (email, uuid,
    org — identity METADATA; the tokens live in .credentials.json and are
    never read here) and caches it by mtime. `helm cred list` shows
    DIR NAME | ACTUAL ACCOUNT | verdict, so drift is visible, not inferred.
  * NON-DESTRUCTIVE — `helm cred backup` snapshots a home's .credentials.json
    bytes plus its oauthAccount block into ~/.cred-backups/<folded-email>/<ts>/
    at 0600 (dirs 0700), skipping when an identical snapshot already exists.
    `helm cred switch-guard` is that backup run deliberately BEFORE a /login
    (and `--install` wires it as a SessionStart hook in every claude home).
  * REVERSIBLE — `helm cred heal` puts the correctly-named account back into a
    drifted home from its newest snapshot. DRY-RUN BY DEFAULT, it backs the
    current occupant up first (so the undo is itself undoable), and it REFUSES
    while any live process holds that config dir — a live session is never
    evicted, never raced.

LAWS (violating these is how accounts get bricked):
  * SECRETS NEVER SURFACE. Credential bytes are read, copied and compared —
    never printed, logged, or put in an error string. Only a 12-hex sha256
    prefix (a content fingerprint, same idiom as homes._token_family) is ever
    written into metadata.
  * OWNER-ONLY FROM CREATION. Every snapshot file is opened 0o600 before a byte
    lands; every directory is 0o700. Never chmod-after-write.
  * FAIL CLOSED. An unreadable/absent .claude.json yields NO account claim —
    the row reports the reason and the mutation refuses. helm never guesses an
    identity from a directory name.
  * EVERY MUTATING PATH BACKS UP FIRST (heal, and keepalive's rotation).
"""
import glob
import hashlib
import json
import os
import sys
import time

from . import homes

ACCOUNT_JSON = ".claude.json"      # identity metadata (oauthAccount) — no tokens
AUTH_JSON = ".credentials.json"    # the secret bytes — copied, never read aloud
DEFAULT_BACKUP_ROOT = os.path.join(os.path.expanduser("~"), ".cred-backups")
TS_FMT = "%Y%m%dT%H%M%SZ"
# Keepalive snapshots a pre-image on every token rotation, so the depth is
# bounded here rather than left to grow forever: the newest KEEP snapshots per
# account survive a new write, older ones are dropped (a rotated refresh token
# is dead anyway — the RECOVERABLE pre-image is always the newest).
KEEP = 20

# the pre-login guard as a hook spec (hooks.py shape): every SessionStart
# snapshots whatever the home currently holds, so a mid-session /login always
# has a pre-image behind it. Fail-open text + timeout come from hooks.py.
GUARD_SPEC = {"name": "cred-guard", "event": "SessionStart",
              "args": "cred backup --quiet", "timeout": 5,
              "own": ("cred backup --quiet", "helm cred backup"), "matcher": "*"}


def backup_root():
    """~/.cred-backups, or HELM_CRED_BACKUP_ROOT (the test/ops override)."""
    return os.path.abspath(os.path.expanduser(
        os.environ.get("HELM_CRED_BACKUP_ROOT") or DEFAULT_BACKUP_ROOT))


# ---------------------------------------------------------------- identity ---
_CACHE = {}   # realpath -> (stat_key, account_dict)


def cache_clear():
    """Drop the identity cache (after a write that changed a home's account)."""
    _CACHE.clear()


def _stat_key(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _str_or_none(v):
    return v if isinstance(v, str) and v else None


def _read_account(real):
    """The fail-closed read: every failure names its reason and claims NOTHING."""
    out = {"path": real, "email": None, "uuid": None, "org": None,
           "ok": False, "error": None}
    src = os.path.join(real, ACCOUNT_JSON)
    if not os.path.exists(src):
        out["error"] = "no %s" % ACCOUNT_JSON
        return out
    doc = _read_json(src)
    if not isinstance(doc, dict):
        out["error"] = "%s unreadable or not an object" % ACCOUNT_JSON
        return out
    oa = doc.get("oauthAccount")
    if not isinstance(oa, dict):
        out["error"] = "no oauthAccount block (never logged in here?)"
        return out
    email = _str_or_none(oa.get("emailAddress"))
    if not email:
        out["error"] = "oauthAccount carries no emailAddress"
        return out
    out.update(email=email, ok=True, uuid=_str_or_none(oa.get("accountUuid")),
               org=_str_or_none(oa.get("organizationName")))
    return out


def account_of(config_dir):
    """THE identity function: which account a config dir actually holds, read
    from its CONTENT. -> {path, email, uuid, org, ok, error}. Cached by the
    .claude.json stat key, so every caller can ask freely."""
    real = os.path.realpath(os.path.expanduser(config_dir or ""))
    key = _stat_key(os.path.join(real, ACCOUNT_JSON))
    hit = _CACHE.get(real)
    if hit is not None and hit[0] == key:
        return dict(hit[1])
    res = _read_account(real)
    _CACHE[real] = (key, res)
    return dict(res)


def oauth_block(config_dir):
    """The raw oauthAccount dict (identity metadata only — the tokens live in
    .credentials.json). {} when unreadable."""
    doc = _read_json(os.path.join(os.path.realpath(os.path.expanduser(config_dir)),
                                  ACCOUNT_JSON))
    oa = doc.get("oauthAccount") if isinstance(doc, dict) else None
    return oa if isinstance(oa, dict) else {}


def verdict_for(path, default=False):
    """(verdict, account) for one config dir — AGREE | DRIFT | UNKNOWN | N/A.
    N/A = the provider default home or any dir outside the claude homes root:
    the canonical-name rule (dir name == folded account email) does not apply
    there, so there is nothing to agree or disagree with."""
    real = os.path.realpath(os.path.expanduser(path))
    acct = account_of(real)
    if not acct["ok"]:
        return "UNKNOWN", acct
    named = (not default
             and os.path.dirname(real) == os.path.realpath(homes.ROOTS["claude"]))
    if not named:
        return "N/A", acct
    return ("AGREE" if homes.canonical_name(acct["email"]) == os.path.basename(real)
            else "DRIFT"), acct


def rows(provider="claude"):
    """One row per live credential home: the dir NAME beside the account it
    ACTUALLY holds, the verdict, and its backup depth. Pure read."""
    out = []
    for r in homes.homes_list():
        if r.get("archived") or r.get("broken_alias") or r["provider"] != provider:
            continue
        real = os.path.realpath(r["path"])
        verdict, acct = verdict_for(real, default=r["default"])
        snaps = snapshots(acct["email"]) if acct["ok"] else []
        out.append({
            "name": r["name"], "path": r["path"], "real": real,
            "aliases": r["aliases"], "default": r["default"], "authed": r["authed"],
            "account": acct["email"], "uuid": acct["uuid"], "org": acct["org"],
            "error": acct["error"], "verdict": verdict,
            "wants_home": homes.canonical_name(acct["email"]) if acct["ok"] else None,
            "live_pids": r["live_pids"], "backups": len(snaps),
            "family": r.get("family"),        # sha256 PREFIX of the refresh token
            "latest_backup": snaps[-1]["ts"] if snaps else None})
    return out


def _snapshot_family(snapshot_path):
    """The snapshot's token-family fingerprint — homes._token_family's law:
    the bytes are read only to hash IN MEMORY, and only the digest prefix
    leaves this function."""
    doc = _read_json(os.path.join(snapshot_path, "credentials.json")) or {}
    tok = (doc.get("claudeAiOauth") or {}).get("refreshToken")
    if not isinstance(tok, str) or not tok:
        return None
    return hashlib.sha256(tok.encode()).hexdigest()[:10]


# ----------------------------------------------------------------- backups ---
def folded_dir(folded):
    return os.path.join(backup_root(), folded)


def account_dir(email):
    """The snapshot dir for an account: ~/.cred-backups/<folded-email>/ — the
    same fold homes.py names homes with (david@x.com -> david-x-com)."""
    return folded_dir(homes.canonical_name(email))


def _snapshots_in(d):
    out = []
    for p in sorted(glob.glob(os.path.join(d, "*"))):
        if not os.path.isdir(p) or os.path.islink(p):
            continue
        meta = _read_json(os.path.join(p, "meta.json")) or {}
        out.append({"path": p, "ts": os.path.basename(p),
                    "account": meta.get("account"),
                    "source_name": meta.get("source_name"),
                    "source_home": meta.get("source_home"),
                    "digest": meta.get("digest"),
                    "has_creds": os.path.exists(os.path.join(p, "credentials.json"))})
    return out


def snapshots(email):
    """Every snapshot of one account, oldest first (the ts name sorts)."""
    return _snapshots_in(account_dir(email)) if email else []


def snapshots_for_home_name(name):
    """Snapshots of the account a home NAME promises — the folded email IS the
    canonical home name, so the name resolves the backup dir directly."""
    return _snapshots_in(folded_dir(name))


def _secure_dir(path):
    """0700 explicitly — makedirs' mode is umask-masked."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)


def _write_private(path, data):
    """Owner-only FROM CREATION (O_EXCL, 0600) — never chmod-after-write."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def _atomic_private(path, data, mode=0o600):
    tmp = "%s.helm-tmp.%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _next_snapshot_dir(acct_dir):
    """A snapshot name that sorts STRICTLY after every surviving one — newest
    is `snapshots()[-1]` by name alone, and pruning (which frees old names)
    can never resurrect one ahead of the newest."""
    base = time.strftime(TS_FMT, time.gmtime())
    existing = [os.path.basename(p) for p in glob.glob(os.path.join(acct_dir, "*"))
                if os.path.isdir(p)]
    top = max(existing) if existing else ""
    cand, n = base, 0
    while cand <= top or os.path.exists(os.path.join(acct_dir, cand)):
        n += 1
        cand = "%s-%03d" % (base, n)
    return os.path.join(acct_dir, cand)


def _identical(snapshot, blob, oa):
    """Byte-identical creds AND the same identity block — the idempotence
    test, done on content (never on a timestamp)."""
    try:
        with open(os.path.join(snapshot["path"], "credentials.json"), "rb") as fh:
            if fh.read() != blob:
                return False
    except OSError:
        return False
    return (_read_json(os.path.join(snapshot["path"], "account.json")) or {}) == oa


def backup(config_dir):
    """Snapshot one home's credentials + identity block. Fail-closed: a home
    whose account cannot be read is REPORTED and skipped, never filed under a
    guessed name. -> {ok, action: backup|skip, account, dest, reason}."""
    real = os.path.realpath(os.path.expanduser(config_dir or ""))
    name = os.path.basename(real)
    acct = account_of(real)
    if not acct["ok"]:
        return {"ok": False, "action": "skip", "home": real, "name": name,
                "account": None, "reason": acct["error"]}
    try:
        with open(os.path.join(real, AUTH_JSON), "rb") as fh:
            blob = fh.read()
    except OSError as e:
        return {"ok": False, "action": "skip", "home": real, "name": name,
                "account": acct["email"],
                "reason": "no readable %s (%s)" % (AUTH_JSON, e.__class__.__name__)}
    oa = oauth_block(real)
    snaps = snapshots(acct["email"])
    if snaps and _identical(snaps[-1], blob, oa):
        return {"ok": True, "action": "skip", "home": real, "name": name,
                "account": acct["email"], "dest": snaps[-1]["path"],
                "reason": "identical snapshot already exists"}
    digest = hashlib.sha256(blob).hexdigest()[:12]   # fingerprint only, never the bytes
    dest = None
    try:
        _secure_dir(backup_root())
        _secure_dir(account_dir(acct["email"]))
        dest = _next_snapshot_dir(account_dir(acct["email"]))
        _secure_dir(dest)
        _write_private(os.path.join(dest, "credentials.json"), blob)
        _write_private(os.path.join(dest, "account.json"),
                       json.dumps(oa, indent=2, sort_keys=True).encode())
        _write_private(os.path.join(dest, "meta.json"), json.dumps({
            "account": acct["email"], "uuid": acct["uuid"], "org": acct["org"],
            "source_home": real, "source_name": name, "digest": digest,
            "bytes": len(blob), "ts": os.path.basename(dest),
            "note": "digest is a sha256 PREFIX (content fingerprint); token bytes "
                    "live only in credentials.json, 0600, never printed",
        }, indent=2).encode())
    except OSError as e:
        # a failed snapshot is REPORTED, never raised: callers on the mutating
        # paths (keepalive's rotation) must not be blocked by a full disk —
        # an unrotated token family is the worse outcome.
        _drop(dest)                       # no half-snapshot survives to be restored
        return {"ok": False, "action": "skip", "home": real, "name": name,
                "account": acct["email"],
                "reason": "snapshot write failed (%s)" % e.__class__.__name__}
    pruned = _prune(acct["email"])
    return {"ok": True, "action": "backup", "home": real, "name": name,
            "account": acct["email"], "dest": dest, "digest": digest,
            "pruned": pruned}


def _drop(snapshot_dir):
    """Remove one snapshot dir (its own files only — never a tree walk).
    Best-effort: a snapshot that will not delete is left alone."""
    if not snapshot_dir or not os.path.isdir(snapshot_dir):
        return False
    try:
        for f in sorted(os.listdir(snapshot_dir)):
            os.unlink(os.path.join(snapshot_dir, f))
        os.rmdir(snapshot_dir)
        return True
    except OSError:
        return False


def _prune(email, keep=KEEP):
    """Drop all but the newest `keep` snapshots of one account. Never touches
    the newest (the only one heal restores from). Best-effort."""
    snaps = snapshots(email)
    return [s["ts"] for s in (snaps[:-keep] if len(snaps) > keep else [])
            if _drop(s["path"])]


def backup_all():
    return [backup(r["real"]) for r in rows() if r["authed"]]


def restore(snapshot_path, config_dir):
    """Put a snapshot back: credentials bytes (0600) AND the oauthAccount block
    (so the home's identity stops lying). Everything else in .claude.json
    survives byte-for-byte. -> {ok, account, error}."""
    real = os.path.realpath(os.path.expanduser(config_dir))
    try:
        with open(os.path.join(snapshot_path, "credentials.json"), "rb") as fh:
            blob = fh.read()
    except OSError as e:
        return {"ok": False, "error": "snapshot unreadable (%s)" % e.__class__.__name__}
    oa = _read_json(os.path.join(snapshot_path, "account.json"))
    if not isinstance(oa, dict) or not _str_or_none(oa.get("emailAddress")):
        return {"ok": False, "error": "snapshot has no identity block — refusing"}
    doc = _read_json(os.path.join(real, ACCOUNT_JSON))
    doc = doc if isinstance(doc, dict) else {}
    doc["oauthAccount"] = oa
    cfg = os.path.join(real, ACCOUNT_JSON)
    mode = 0o600
    if os.path.exists(cfg):
        mode = os.stat(cfg).st_mode & 0o777      # the operator's own perms survive
    try:
        _atomic_private(os.path.join(real, AUTH_JSON), blob)
        _atomic_private(cfg, (json.dumps(doc, indent=2) + "\n").encode(), mode=mode)
    except OSError as e:
        return {"ok": False, "error": "write failed (%s)" % e.__class__.__name__}
    cache_clear()
    return {"ok": True, "account": oa.get("emailAddress")}


# ------------------------------------------------------------- live holders ---
def holders_of(path, default=False):
    """[(pid, comm)] every live process pinned to this config dir (our own pid
    excluded). None = the probe is unavailable (no /proc) — the caller must
    then REFUSE, never assume free. Conservative on purpose: an inherited
    CLAUDE_CONFIG_DIR counts, because it means a session owns this home."""
    if not os.path.isdir("/proc"):
        return None
    real = os.path.realpath(path)
    me = os.getpid()
    out = []
    for envf in glob.glob("/proc/[0-9]*/environ"):
        try:
            pid = int(envf.split("/")[2])
        except ValueError:
            continue
        if pid == me:
            continue
        try:
            with open(envf, "rb") as fh:
                env = fh.read()
            with open("/proc/%d/comm" % pid) as fh:
                comm = fh.read().strip()
        except OSError:
            continue
        val = None
        for var in env.split(b"\0"):
            if var.startswith(b"CLAUDE_CONFIG_DIR="):
                val = var.split(b"=", 1)[1].decode("utf-8", "replace")
                break
        if val is None:
            if default and comm == "claude":     # env-less claude runs on ~/.claude
                out.append((pid, comm))
            continue
        if os.path.realpath(os.path.expanduser(val)) == real:
            out.append((pid, comm))
    return sorted(out)


def _held_note(holders, cap=3):
    """The refusal message: AGENT processes first (the session that actually
    owns the home), then whatever inherited the env, capped — the full roster
    stays on the plan for --json."""
    ranked = sorted(holders, key=lambda h: (h[1] not in ("claude", "codex"), h[0]))
    head = ", ".join("pid %d (%s)" % (p, c) for p, c in ranked[:cap])
    extra = len(ranked) - cap
    return head + (" +%d more process%s holding this dir"
                   % (extra, "es"[:2 * (extra != 1)]) if extra > 0 else "")


# -------------------------------------------------------------------- heal ---
def _family_live_elsewhere(snapshot, target, estate):
    """The home whose LIVE credentials already carry this snapshot's token
    family, if any (never the target itself). helm's oldest credential law:
    one home = one token family; a byte-copy across two homes is the
    revocation bomb (homes.py's shared-family audit). Restoring a snapshot
    that another home still holds would MINT that state, so heal refuses."""
    fam = _snapshot_family(snapshot["path"])
    if not fam:
        return None
    for other in estate:
        if other["real"] != target["real"] and other.get("family") == fam:
            return other["name"]
    return None


def heal_plan(name=None):
    """One plan per DRIFTED home: what heal WOULD do, and why it can't.
    status: ready | held | cannot-probe | no-backup | revocation-risk."""
    plans = []
    estate = rows()
    for r in estate:
        if r["verdict"] != "DRIFT":
            continue
        if name and name not in ([r["name"], os.path.basename(r["real"])]
                                 + list(r["aliases"] or [])):
            continue
        want = os.path.basename(r["real"])       # the NAME's promise (folded email)
        snaps = [s for s in snapshots_for_home_name(want) if s["has_creds"]]
        holders = holders_of(r["real"], default=r["default"])
        clash = _family_live_elsewhere(snaps[-1], r, estate) if snaps else None
        plan = {"name": r["name"], "path": r["real"], "holds": r["account"],
                "wants_account_folded": want,
                "restore_from": snaps[-1]["path"] if snaps else None,
                "restore_account": snaps[-1]["account"] if snaps else None,
                "holders": holders or []}
        if holders is None:
            plan["status"], plan["reason"] = "cannot-probe", (
                "no /proc — cannot prove the home is free; refusing to touch it")
        elif holders:
            plan["status"], plan["reason"] = "held", (
                "held by %s — a live session is never evicted" % _held_note(holders))
        elif not snaps:
            plan["status"], plan["reason"] = "no-backup", (
                "no snapshot for %s — the evicted account can only come back "
                "through a fresh login" % want)
        elif clash:
            plan["status"], plan["reason"] = "revocation-risk", (
                "that snapshot's token family is LIVE in %s — restoring it here "
                "would leave byte-copies of ONE refresh token in two homes, and "
                "reuse detection revokes the whole family. Fresh login instead: %s"
                % (clash, homes.LOGIN_CMDS["claude"](r["real"])))
        else:
            plan["status"], plan["reason"] = "ready", (
                "restore %s from %s" % (snaps[-1]["account"] or want, snaps[-1]["ts"]))
        plans.append(plan)
    return plans


def heal(name=None, apply=False):
    """DRY-RUN BY DEFAULT. With apply=True, for every `ready` plan: re-probe
    holders (the window between plan and act is where a race lives), snapshot
    the CURRENT occupant so the undo is undoable, restore, then VERIFY the home
    now reads as the expected account — rolling back if it does not."""
    plans = heal_plan(name)
    if not apply:
        return {"apply": False, "plans": plans}
    for plan in plans:
        if plan["status"] != "ready":
            continue
        holders = holders_of(plan["path"])
        if holders is None or holders:
            plan["status"] = "held" if holders else "cannot-probe"
            plan["reason"] = ("held by %s (arrived mid-heal) — refused"
                              % _held_note(holders)) if holders else \
                             "no /proc — refusing"
            continue
        pre = backup(plan["path"])               # the evicted-now occupant, first
        plan["pre_image"] = pre.get("dest")
        res = restore(plan["restore_from"], plan["path"])
        if not res["ok"]:
            plan["status"], plan["reason"] = "failed", res["error"]
            continue
        got = account_of(plan["path"])
        if got["ok"] and homes.canonical_name(got["email"]) == plan["wants_account_folded"]:
            plan["status"] = "restored"
            plan["reason"] = ("%s is home again (creds may be stale — refresh "
                              "tokens rotate; if claude rejects them, log in "
                              "fresh into this home)" % got["email"])
            continue
        plan["status"], plan["reason"] = "failed", (
            "post-restore identity is %s — rolled back"
            % (got["email"] or got["error"]))
        if pre.get("dest"):
            restore(pre["dest"], plan["path"])
    return {"apply": True, "plans": plans}


# ------------------------------------------------------------------ doctor ---
def doctor_rows():
    """[(level, msg)] for helm doctor: the loud drift row + the missing-backup
    row. Read-only; an audit that cannot run WARNs, it never claims health."""
    try:
        rs = rows()
    except Exception as e:                       # an audit must never break doctor
        return [("WARN", "cred identity audit unavailable (%s: %s)"
                 % (e.__class__.__name__, e))]
    if not rs:
        return []
    out = []
    for r in rs:
        if r["verdict"] == "DRIFT":
            out.append(("WARN",
                        "credhome %s HOLDS %s (drift — that account's home is %s); "
                        "`helm cred heal` restores the named account, "
                        "`helm cred list` shows the whole estate"
                        % (r["name"], r["account"], r["wants_home"])))
        elif r["verdict"] == "UNKNOWN" and r["authed"]:
            out.append(("WARN", "credhome %s holds credentials but its identity is "
                                "unreadable (%s) — no account claimed"
                        % (r["name"], r["error"])))
    accounts = sorted({r["account"] for r in rs if r["account"]})
    missing = [a for a in accounts if not snapshots(a)]
    for a in missing:
        out.append(("WARN", "no cred backup for %s — `helm cred backup --all` is "
                            "what makes the next /login reversible" % a))
    if not out:
        out.append(("OK", "cred homes: %d claude home%s, every dir name matches the "
                          "account it holds; %d account%s backed up"
                    % (len(rs), "s"[:len(rs) != 1],
                       len(accounts), "s"[:len(accounts) != 1])))
    return out


# --------------------------------------------------------------------- cli ---
def _take_flag(args, flag):
    if flag in args:
        i = args.index(flag)
        if i + 1 >= len(args):
            return None
        val = args[i + 1]
        del args[i:i + 2]
        return val
    return None


def _resolve_home(name):
    """--home NAME | path -> realpath, via the same resolver every other verb
    uses. No name -> $CLAUDE_CONFIG_DIR -> the default home."""
    if name:
        row, err = homes._resolve(name, "claude")
        if err:
            return None, err["error"]
        return os.path.realpath(row["path"]), None
    env = os.environ.get(homes.ENV_VAR["claude"])
    if env:
        return os.path.realpath(os.path.expanduser(env)), None
    return os.path.realpath(homes.DEFAULTS["claude"]), None


def _print_list(args):
    as_json = "--json" in args
    rs = rows()
    if as_json:
        print(json.dumps(rs, indent=2))
        return 0
    if not rs:
        print("helm cred: no claude credential homes found")
        return 0
    print("helm cred — identity read from CONTENT (%s oauthAccount), never from "
          "the dir name:" % ACCOUNT_JSON)
    print("  %-30s %-32s %-8s %-8s %s"
          % ("DIR NAME", "ACTUAL ACCOUNT", "VERDICT", "BACKUPS", "NOTE"))
    for r in sorted(rs, key=lambda r: (r["default"], r["name"])):
        note = []
        if r["verdict"] == "DRIFT":
            note.append("this account's home is %s" % r["wants_home"])
        if r["verdict"] == "UNKNOWN":
            note.append(r["error"] or "identity unreadable")
        if r["aliases"]:
            note.append("alias: " + ",".join(r["aliases"]))
        if r["live_pids"]:
            note.append("live pids " + ",".join(map(str, r["live_pids"])))
        if not r["authed"]:
            note.append("no %s" % AUTH_JSON)
        print("  %-30s %-32s %-8s %-8s %s"
              % (r["name"][:30], (r["account"] or "-")[:32], r["verdict"],
                 r["backups"], "; ".join(note)))
    drift = [r for r in rs if r["verdict"] == "DRIFT"]
    print("helm cred: %d home%s, %d drift%s%s"
          % (len(rs), "s"[:len(rs) != 1], len(drift), "" if len(drift) == 1 else "s",
             " — `helm cred heal` (dry-run) shows the repair" if drift else ""))
    return 0


def _print_backup(args):
    quiet = "--quiet" in args
    args = [a for a in args if a != "--quiet"]
    name = _take_flag(args, "--home")
    results = []
    if "--all" in args:
        results = backup_all()
    else:
        path, err = _resolve_home(name)
        if err:
            if not quiet:
                print("helm cred: " + err, file=sys.stderr)
            return 0 if quiet else 1
        results = [backup(path)]
    if quiet:                       # hook mode: a SessionStart hook prints NOTHING
        return 0
    made = [r for r in results if r["action"] == "backup"]
    for r in results:
        if r["action"] == "backup":
            print("  backed up %-32s <- %s  (%s)"
                  % (r["account"], r["name"], r["dest"]))
        elif r["ok"]:
            print("  %-32s %s (%s)" % (r["account"], r["reason"], r["name"]))
        else:
            print("  SKIP %-27s %s" % (r["name"], r["reason"]))
    print("helm cred backup: %d snapshot%s written, %d already current (root %s, "
          "0700 dirs / 0600 files — token bytes are copied, never printed)"
          % (len(made), "s"[:len(made) != 1], len(results) - len(made), backup_root()))
    return 0


def _print_switch_guard(args):
    if "--install" in args:
        from . import hooks
        dry = "--dry" in args
        targets = hooks.claude_homes()
        if not targets:
            print("helm cred: no claude homes to guard")
            return 0
        worst = 0
        for hname, path in targets:
            action, detail = hooks.install_home(path, dry=dry, specs=(GUARD_SPEC,))
            print("  %-30s %s" % (hname, action if not dry else action))
            if action == "fail":
                print("    " + detail, file=sys.stderr)
                worst = 1
        print("helm cred switch-guard: SessionStart guard %sinstalled in %d home%s "
              "— every session start snapshots what the home holds, so a mid-session "
              "/login always has a pre-image behind it"
              % ("would be " if dry else "", len(targets), "s"[:len(targets) != 1]))
        return worst
    path, err = _resolve_home(_take_flag(list(args), "--home"))
    if err:
        print("helm cred: " + err, file=sys.stderr)
        return 1
    res = backup(path)
    if not res["ok"]:
        print("helm cred switch-guard: NOT protected — %s (%s)"
              % (res["reason"], path), file=sys.stderr)
        print("  a /login here is still safe for the NEW account, but the account "
              "this home holds now cannot be restored afterwards.", file=sys.stderr)
        return 1
    print("helm cred switch-guard: %s is protected (%s)"
          % (res["account"], "snapshot " + res["dest"] if res["action"] == "backup"
             else "already snapshotted: " + res["dest"]))
    print("  now safe to run:  %s" % homes.LOGIN_CMDS["claude"](path))
    print("  after the login:  `helm cred list` shows what this home now holds; "
          "`helm cred heal` puts %s back when no session holds it."
          % res["account"])
    return 0


def _print_heal(args):
    apply = "--apply" in args
    as_json = "--json" in args
    rest = [a for a in args if not a.startswith("-")]
    res = heal(rest[0] if rest else None, apply=apply)
    if as_json:
        print(json.dumps(res, indent=2))
        return 0
    plans = res["plans"]
    if not plans:
        print("helm cred heal: no drifted home — every dir name matches the "
              "account it holds")
        return 0
    print("helm cred heal (%s):" % ("APPLIED" if apply else "dry-run — add --apply"))
    for p in plans:
        print("  %-30s holds %-32s %s" % (p["name"], p["holds"] or "-", p["status"]))
        print("      %s" % p["reason"])
        if p.get("pre_image"):
            print("      pre-image of the evicted occupant: %s" % p["pre_image"])
    bad = [p for p in plans if p["status"] in ("failed", "cannot-probe")]
    return 1 if bad else 0


def cmd_cred(args):
    """cred [list|backup [--all]|switch-guard [--install]|heal [--apply]]"""
    args = list(args)
    verb = args[0] if args and not args[0].startswith("-") else "list"
    rest = args[1:] if args and not args[0].startswith("-") else args
    if verb in ("list", "ls"):
        return _print_list(rest)
    if verb == "backup":
        return _print_backup(rest)
    if verb in ("switch-guard", "guard"):
        return _print_switch_guard(rest)
    if verb == "heal":
        return _print_heal(rest)
    print("helm cred: unknown subverb %r — %s" % (verb, cmd_cred.__doc__),
          file=sys.stderr)
    return 2
