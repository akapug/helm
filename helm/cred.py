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
import stat
import sys
import tempfile
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
PROC_ROOT = "/proc"                 # module-level so tests can use a fixture tree

# The pre-login guard as hook specs (hooks.py shape). SessionStart alone is NOT
# enough: a live session refreshes its OWN token, and the grant ROTATES the
# refresh token (keepalive.py's first law), so hours into a session the
# SessionStart pre-image holds a refresh token the server has already consumed
# — restoring it is the temporal twin of the byte-copy revocation bomb that
# _family_live_elsewhere refuses. keepalive cannot cover the gap either: it
# SKIPS any home with a live holder. So the guard also rides the TURN BOUNDARY
# (Stop, like hooks.SPECS' stop-guard), where an identical snapshot costs two
# file reads and a compare, and the pre-image is never more than one turn
# behind the token the next `/login` is about to destroy.
GUARD_SPECS = (
    {"name": "cred-guard", "event": "SessionStart",
     "args": "cred backup --apply --quiet", "timeout": 5,
     "own": ("cred backup --apply --quiet", "helm cred backup"), "matcher": "*"},
    {"name": "cred-guard-turn", "event": "Stop",
     "args": "cred backup --apply --quiet", "timeout": 5,
     "own": ("cred backup --apply --quiet", "helm cred backup"), "matcher": None},
)
GUARD_SPEC = GUARD_SPECS[0]      # the name the SessionStart-only callers know


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
    """Identity key for a PLAIN regular file. Symlinks are not credential
    files: following one would let a home escape its boundary between the stat
    and read."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino, st.st_dev)


def _read_regular(path):
    """(bytes, mode) from one non-symlink regular file. The descriptor is the
    object checked, closing the lstat/open swap window."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError("not a regular file")
        with os.fdopen(fd, "rb") as fh:
            fd = -1
            return fh.read(), stat.S_IMODE(st.st_mode)
    finally:
        if fd >= 0:
            os.close(fd)


def _json_bytes(blob):
    try:
        return json.loads(blob.decode("utf-8"))
    except (UnicodeError, ValueError):
        return None


def _read_json(path):
    try:
        blob, _ = _read_regular(path)
    except OSError:
        return None
    return _json_bytes(blob)


def _str_or_none(v):
    return v if isinstance(v, str) and v else None


def _email_or_none(v):
    """Normalized account identity. Control characters and path-empty folds
    are refused: identity is displayed and also selects a backup directory."""
    if not isinstance(v, str):
        return None
    email = v.strip().lower()
    if (not email or email.count("@") != 1 or "." not in email.rsplit("@", 1)[1]
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in email)
            or not homes.canonical_name(email)):
        return None
    return email


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
    email = _email_or_none(oa.get("emailAddress"))
    if not email:
        out["error"] = "oauthAccount carries no valid emailAddress"
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
            "latest_backup": snaps[-1]["ts"] if snaps else None,
            # `backups` counts the OCCUPANT's snapshots — on a drifted home that
            # is the account that just arrived, and it reads as 0 exactly when
            # the owner most needs to know the EVICTED one is safe. This is the
            # number that answers "can heal put the named account back?".
            "named_backups": 0 if r["default"] else
            len([s for s in snapshots_for_home_name(os.path.basename(real))
                 if s["has_creds"]])})
    return out


def _snapshot_expiry(snapshot_path):
    """The snapshot's ACCESS-token expiry (ms epoch), or None — the staleness
    signal. Only a TIMESTAMP leaves this function; the credential bytes beside
    it are never read into any returned value."""
    doc = _read_json(os.path.join(snapshot_path, "credentials.json")) or {}
    exp = (doc.get("claudeAiOauth") or {}).get("expiresAt")
    return exp if isinstance(exp, (int, float)) and not isinstance(exp, bool) else None


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
                    "account": _email_or_none(meta.get("account")),
                    "source_name": meta.get("source_name"),
                    "source_home": meta.get("source_home"),
                    "digest": meta.get("digest"),
                    "has_creds": _stat_key(os.path.join(p, "credentials.json")) is not None})
    return out


def snapshots(email):
    """Every snapshot of one account, oldest first (the ts name sorts)."""
    return _snapshots_in(account_dir(email)) if email else []


def snapshots_for_home_name(name):
    """Snapshots of the account a home NAME promises — the folded email IS the
    canonical home name, so the name resolves the backup dir directly."""
    return _snapshots_in(folded_dir(name))


def _secure_dir(path):
    """Create/verify one plain 0700 directory. Never chmod through a symlink."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
        raise OSError("backup directory is not a plain directory")
    os.chmod(path, 0o700, follow_symlinks=False)


def _write_private(path, data):
    """Owner-only FROM CREATION (O_EXCL, 0600) — never chmod-after-write."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _stage_private(path, data, mode=0o600):
    """Durably stage `data` in the destination directory. tempfile's random
    O_EXCL name avoids PID-reuse/concurrent-call collisions; bytes land while
    the fd is 0600, then the requested final mode is set before commit."""
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".helm-tmp.",
                               dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as fh:
            fd = -1
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
            os.fchmod(fh.fileno(), mode)
    except OSError:
        if fd >= 0:
            os.close(fd)
        _unlink(tmp)
        raise
    return tmp


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_private(path, data, mode=0o600):
    tmp = _stage_private(path, data, mode)
    try:
        os.replace(tmp, path)
        _fsync_dir(os.path.dirname(path))
    except OSError:
        _unlink(tmp)
        raise


def _claim_snapshot_dir(acct_dir):
    """CLAIM a snapshot dir: a name that sorts STRICTLY after every surviving
    one (newest is `snapshots()[-1]` by name alone, and pruning — which frees
    old names — can never resurrect one ahead of the newest), created with an
    EXCLUSIVE mkdir at 0700.

    Exclusive because the guard runs per turn in every home and one account can
    occupy two homes: two backups can land in the same account dir in the same
    SECOND. A shared name would mean the second writer's O_EXCL file write
    fails and its error path deletes the first writer's finished snapshot — a
    concurrent backup destroying a good pre-image. Whoever loses the mkdir race
    simply takes the next name."""
    base = time.strftime(TS_FMT, time.gmtime())
    existing = [os.path.basename(p) for p in glob.glob(os.path.join(acct_dir, "*"))
                if os.path.isdir(p)]
    top = max(existing) if existing else ""
    cand, n = base, 0
    while n < 1000:
        if cand > top:
            try:
                os.mkdir(os.path.join(acct_dir, cand), 0o700)
                os.chmod(os.path.join(acct_dir, cand), 0o700)   # umask-proof
                return os.path.join(acct_dir, cand)
            except FileExistsError:
                pass
        n += 1
        cand = "%s-%03d" % (base, n)
    raise OSError("cannot claim a snapshot name under %s" % acct_dir)


def _identical(snapshot, blob, oa):
    """Byte-identical creds AND the same identity block — the idempotence
    test, done on content (never on a timestamp)."""
    try:
        old, _ = _read_regular(os.path.join(snapshot["path"], "credentials.json"))
    except OSError:
        return False
    return old == blob and (_read_json(
        os.path.join(snapshot["path"], "account.json")) or {}) == oa


def _capture_home(real):
    """Stable credential + identity pre-image, or a secret-free reason. Both
    files are read through checked descriptors and their inode/stat keys are
    bracketed; a concurrent /login can therefore make capture REFUSE, never
    file one account's tokens under another account's identity."""
    cfg, auth = os.path.join(real, ACCOUNT_JSON), os.path.join(real, AUTH_JSON)
    for _ in range(2):
        before = (_stat_key(cfg), _stat_key(auth))
        if before[0] is None:
            return None, "%s absent, unreadable, or not a regular file" % ACCOUNT_JSON
        if before[1] is None:
            return None, "%s absent, unreadable, or not a regular file" % AUTH_JSON
        try:
            cfg_blob, _ = _read_regular(cfg)
            blob, _ = _read_regular(auth)
        except OSError as e:
            return None, "credential pre-image unreadable (%s)" % e.__class__.__name__
        if before != (_stat_key(cfg), _stat_key(auth)):
            continue
        doc = _json_bytes(cfg_blob)
        if not isinstance(doc, dict):
            return None, "%s unreadable or not an object" % ACCOUNT_JSON
        oa = doc.get("oauthAccount")
        if not isinstance(oa, dict):
            return None, "no oauthAccount block (never logged in here?)"
        email = _email_or_none(oa.get("emailAddress"))
        if not email:
            return None, "oauthAccount carries no valid emailAddress"
        oa = dict(oa, emailAddress=email)
        return {"blob": blob, "oa": oa, "email": email,
                "uuid": _str_or_none(oa.get("accountUuid")),
                "org": _str_or_none(oa.get("organizationName"))}, None
    return None, "credential files changed during pre-image capture"


def backup(config_dir, apply=False):
    """Plan or snapshot one home's credentials + identity block. DRY-RUN is
    the default; apply=True is the only path that creates directories/files.
    Fail-closed: identity is captured from the same stable file pair as the
    secret bytes, never guessed from a directory name."""
    real = os.path.realpath(os.path.expanduser(config_dir or ""))
    name = os.path.basename(real)
    cap, reason = _capture_home(real)
    if cap is None:
        return {"ok": False, "action": "skip", "home": real, "name": name,
                "account": None, "reason": reason}
    blob, oa, email = cap["blob"], cap["oa"], cap["email"]
    snaps = snapshots(email)
    if snaps and _identical(snaps[-1], blob, oa):
        return {"ok": True, "action": "skip", "home": real, "name": name,
                "account": email, "dest": snaps[-1]["path"],
                "reason": "identical snapshot already exists"}
    digest = hashlib.sha256(blob).hexdigest()[:12]
    if not apply:
        return {"ok": True, "action": "would-backup", "home": real, "name": name,
                "account": email, "digest": digest,
                "reason": "snapshot would be written; add --apply"}
    dest = None
    try:
        _secure_dir(backup_root())
        _secure_dir(account_dir(email))
        dest = _claim_snapshot_dir(account_dir(email))
        _write_private(os.path.join(dest, "credentials.json"), blob)
        _write_private(os.path.join(dest, "account.json"),
                       json.dumps(oa, indent=2, sort_keys=True).encode())
        _write_private(os.path.join(dest, "meta.json"), json.dumps({
            "account": email, "uuid": cap["uuid"], "org": cap["org"],
            "source_home": real, "source_name": name, "digest": digest,
            "bytes": len(blob), "ts": os.path.basename(dest),
            "note": "digest is a sha256 PREFIX (content fingerprint); token bytes "
                    "live only in credentials.json, 0600, never printed",
        }, indent=2).encode())
        _fsync_dir(dest)
        _fsync_dir(account_dir(email))
    except OSError as e:
        _drop(dest)
        return {"ok": False, "action": "skip", "home": real, "name": name,
                "account": email,
                "reason": "snapshot write failed (%s)" % e.__class__.__name__}
    pruned = _prune(email)
    return {"ok": True, "action": "backup", "home": real, "name": name,
            "account": email, "dest": dest, "digest": digest,
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


def backup_all(apply=False):
    return [backup(r["real"], apply=apply) for r in rows() if r["authed"]]


def _pre_image(path):
    """Exact file pre-image. Absence is distinct from an unreadable/symlink
    file; only true ENOENT may later roll back by deletion."""
    try:
        blob, mode = _read_regular(path)
        return {"exists": True, "blob": blob, "mode": mode}, None
    except FileNotFoundError:
        return {"exists": False, "blob": None, "mode": None}, None
    except OSError as e:
        return None, "pre-image unreadable (%s)" % e.__class__.__name__


def _rollback_files(pre):
    """Restore exact bytes, modes, and original absence after a failed commit."""
    staged = []
    try:
        for path, image in pre.items():
            if image["exists"]:
                staged.append((path, _stage_private(
                    path, image["blob"], image["mode"])))
        for path, image in reversed(list(pre.items())):
            if image["exists"]:
                tmp = next(t for p, t in staged if p == path)
                os.replace(tmp, path)
            elif os.path.lexists(path):
                os.unlink(path)
        _fsync_dir(os.path.dirname(next(iter(pre))))
        return True
    except OSError:
        return False
    finally:
        for _, tmp in staged:
            _unlink(tmp)


def _snapshot_files(snapshot_path):
    """Checked snapshot payload. The snapshot itself and both files must be
    plain objects under the configured backup root; symlinks never restore."""
    root = os.path.realpath(backup_root())
    snap = os.path.realpath(os.path.expanduser(snapshot_path))
    try:
        if os.path.commonpath((root, snap)) != root:
            return None, None, "snapshot is outside the backup root"
        st = os.lstat(snapshot_path)
        if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
            return None, None, "snapshot is not a plain directory"
        blob, _ = _read_regular(os.path.join(snap, "credentials.json"))
        account_blob, _ = _read_regular(os.path.join(snap, "account.json"))
    except (OSError, ValueError) as e:
        return None, None, "snapshot unreadable (%s)" % e.__class__.__name__
    oa = _json_bytes(account_blob)
    email = _email_or_none(oa.get("emailAddress")) if isinstance(oa, dict) else None
    if not email:
        return None, None, "snapshot has no valid identity block — refusing"
    return blob, dict(oa, emailAddress=email), None


def restore(snapshot_path, config_dir, require_free=False):
    """Put a snapshot back transactionally. Credentials are byte-exact; the
    oauthAccount block is merged into the home's current .claude.json while all
    unrelated keys survive. Both outputs are 0600 and durably staged. Any
    staging/replace/fsync failure restores exact original bytes, modes, and
    absence for BOTH files.

    require_free=True brackets the final commit with the conservative /proc
    holder probe used by heal."""
    real = os.path.realpath(os.path.expanduser(config_dir))
    try:
        st = os.lstat(real)
    except OSError as e:
        return {"ok": False, "error": "home unavailable (%s)" % e.__class__.__name__}
    if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
        return {"ok": False, "error": "home is not a plain directory — refusing"}
    blob, oa, error = _snapshot_files(snapshot_path)
    if error:
        return {"ok": False, "error": error}
    cfg, auth = os.path.join(real, ACCOUNT_JSON), os.path.join(real, AUTH_JSON)
    pre = {}
    for path in (auth, cfg):
        image, error = _pre_image(path)
        if error:
            return {"ok": False, "error": error}
        pre[path] = image
    doc = {}
    if pre[cfg]["exists"]:
        doc = _json_bytes(pre[cfg]["blob"])
        if not isinstance(doc, dict):
            return {"ok": False, "error":
                    "%s is present but unreadable/not an object — refusing to "
                    "overwrite it (it holds this home's whole state)" % ACCOUNT_JSON}
    doc = dict(doc)
    doc["oauthAccount"] = oa
    outputs = ((auth, blob),
               (cfg, (json.dumps(doc, indent=2) + "\n").encode()))
    staged = []
    try:
        for path, data in outputs:
            staged.append((path, _stage_private(path, data, 0o600)))
    except OSError as e:
        for _, tmp in staged:
            _unlink(tmp)
        return {"ok": False, "error": "write failed (%s)" % e.__class__.__name__}
    if require_free:
        held = holders_of(real)
        if held is None or held:
            for _, tmp in staged:
                _unlink(tmp)
            return {"ok": False, "error": "home is no longer proven free — refusing"}
    changed = []
    try:
        for path, tmp in staged:
            os.replace(tmp, path)
            changed.append(path)
        _fsync_dir(real)
        got, mode = _read_regular(auth)
        if got != blob or mode != 0o600:
            raise OSError("credential verification failed")
        cfg_blob, cfg_mode = _read_regular(cfg)
        cfg_doc = _json_bytes(cfg_blob)
        if (cfg_mode != 0o600 or not isinstance(cfg_doc, dict)
                or _email_or_none((cfg_doc.get("oauthAccount") or {}).get(
                    "emailAddress")) != oa["emailAddress"]):
            raise OSError("identity verification failed")
    except OSError as e:
        for _, tmp in staged:
            _unlink(tmp)
        rolled = _rollback_files(pre) if changed else True
        cache_clear()
        if not rolled:
            return {"ok": False, "error": "write failed (%s) and exact rollback failed"
                    % e.__class__.__name__}
        return {"ok": False, "error": "write failed (%s); exact pre-image restored"
                % e.__class__.__name__}
    cache_clear()
    return {"ok": True, "account": oa["emailAddress"]}


# ------------------------------------------------------------- live holders ---
def _proc_start(pid_dir):
    """Starttime from /proc/<pid>/stat, whose comm may contain spaces/parens."""
    with open(os.path.join(pid_dir, "stat")) as fh:
        return int(fh.read().rsplit(")", 1)[1].split()[19])


def holders_of(path, default=False):
    """[(pid, comm)] every live process pinned to this config dir (our own pid
    excluded). None means uncertainty, and every caller MUST refuse.

    Each pid is bracketed by its starttime so PID reuse cannot mix one
    process's environ with another's comm. A permission/read error while the pid
    still exists is uncertainty, not evidence of absence; only a process that
    demonstrably vanished during the scan is skipped."""
    if not os.path.isdir(PROC_ROOT):
        return None
    real = os.path.realpath(path)
    me = os.getpid()
    out = []
    try:
        entries = list(os.scandir(PROC_ROOT))
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit() or int(entry.name) == me:
            continue
        pid, pdir = int(entry.name), entry.path
        try:
            start = _proc_start(pdir)
            with open(os.path.join(pdir, "environ"), "rb") as fh:
                env = fh.read()
            with open(os.path.join(pdir, "comm")) as fh:
                comm = fh.read().strip()
            if _proc_start(pdir) != start:
                return None
        except (OSError, ValueError, IndexError):
            if not os.path.exists(pdir):
                continue
            return None
        val = None
        for var in env.split(b"\0"):
            if var.startswith(b"CLAUDE_CONFIG_DIR="):
                val = os.fsdecode(var.split(b"=", 1)[1])
                break
        if val is None:
            if default and comm == "claude":
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
    head = ", ".join("pid %d" % p for p, _ in ranked[:cap])
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
    status: ready | held | cannot-probe | no-backup | revocation-risk
    (apply adds: restored | failed | no-preimage)."""
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
        snap_accounts = {s["account"] for s in snaps if s["account"]}
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
        elif len(snap_accounts) != 1:
            plan["status"], plan["reason"] = "ambiguous-backup", (
                "snapshots under %s claim multiple or missing account identities — "
                "the folded directory name is not enough to choose safely" % want)
        elif clash:
            plan["status"], plan["reason"] = "revocation-risk", (
                "that snapshot's token family is LIVE in %s — restoring it here "
                "would leave byte-copies of ONE refresh token in two homes, and "
                "reuse detection revokes the whole family. Fresh login instead: %s"
                % (clash, homes.LOGIN_CMDS["claude"](r["real"])))
        else:
            # STALENESS is the temporal twin of the shared-family bomb: an
            # access token that had already expired means whoever held this
            # home next had to refresh, and the grant ROTATES the refresh
            # token — the snapshot's copy may already be consumed, and a
            # consumed refresh token is what reuse detection revokes families
            # over. Not a refusal (this is still the only recovery on disk),
            # but the owner sees it before typing --apply.
            exp = _snapshot_expiry(snaps[-1]["path"])
            plan["stale_pre_image"] = bool(exp is not None
                                           and exp < time.time() * 1000)
            plan["status"], plan["reason"] = "ready", (
                "restore %s from %s" % (snaps[-1]["account"] or want, snaps[-1]["ts"]))
            if plan["stale_pre_image"]:
                plan["reason"] += (
                    " — WARNING: that snapshot's access token was already expired, "
                    "so the home very likely refreshed (and ROTATED the refresh "
                    "token) after it was taken; the snapshot's copy may be spent, "
                    "and a spent refresh token is what reuse detection revokes a "
                    "family over. A fresh login is the safe move: %s"
                    % homes.LOGIN_CMDS["claude"](r["real"]))
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
        pre = backup(plan["path"], apply=True)    # the evicted-now occupant, first
        if not pre["ok"]:
            # THE LAW, ENFORCED not merely attempted: no eviction without a
            # pre-image. Proceeding here would delete the occupant's only copy
            # of a live credential — the exact loss heal exists to prevent.
            plan["status"], plan["reason"] = "no-preimage", (
                "cannot snapshot the current occupant %s first (%s) — refusing "
                "to evict an account whose credentials would then exist nowhere"
                % (plan["holds"] or "?", pre["reason"]))
            continue
        plan["pre_image"] = pre.get("dest")
        holders = holders_of(plan["path"])
        if holders is None or holders:
            plan["status"] = "held" if holders else "cannot-probe"
            plan["reason"] = ("held by %s (arrived during pre-image capture) — refused"
                              % _held_note(holders)) if holders else \
                             "holder probe became uncertain — refusing"
            continue
        res = restore(plan["restore_from"], plan["path"], require_free=True)
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
            rolled = restore(pre["dest"], plan["path"], require_free=True)
            if not rolled["ok"]:
                plan["reason"] += "; pre-image rollback refused or failed"
    return {"apply": True, "plans": plans}


# ------------------------------------------------------------------ doctor ---
def doctor_rows():
    """[(level, msg)] for helm doctor: the loud drift row + the missing-backup
    row. Read-only; an audit that cannot run WARNs, it never claims health."""
    try:
        rs = rows()
    except Exception as e:                       # no exception text: it may carry a path/token
        return [("WARN", "cred identity audit unavailable (%s)"
                 % e.__class__.__name__)]
    if not rs:
        return []
    out = []
    for r in rs:
        if r["verdict"] == "DRIFT":
            out.append(("WARN",
                        "credhome %s HOLDS %s (drift — that account's home is %s); "
                        "%s, `helm cred list` shows the whole estate"
                        % (r["name"], r["account"], r["wants_home"],
                           "`helm cred heal` restores %s from its %d snapshot%s"
                           % (r["name"], r["named_backups"],
                              "s"[:r["named_backups"] != 1]) if r["named_backups"]
                           else "and NOTHING was snapshotted for %s — only a fresh "
                                "login brings it back" % r["name"])))
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


def _display_path(path):
    """Paths are operational metadata, except when a credential was pasted
    where a path belonged. Never echo a token-shaped component back."""
    parts = [p for p in os.path.normpath(str(path or "")).split(os.sep) if p]
    for part in parts:
        low = part.lower()
        if (len(part) > 72 or any(k in low for k in
                                  ("refresh-token", "access-token", "bearer-",
                                   "oauth-token", "sk-ant-"))
                or any(ord(ch) < 32 or ord(ch) == 127 for ch in part)):
            return "<redacted-path>"
    return str(path)


def _resolve_home(name):
    """--home NAME | path -> realpath, via the same resolver every other verb
    uses. No name -> $CLAUDE_CONFIG_DIR -> the default home."""
    if name:
        row, err = homes._resolve(name, "claude")
        if err:
            return None, "home could not be resolved (see `helm cred list`)"
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
            note.append("heal can restore %s (%d snapshot%s)"
                        % (r["name"], r["named_backups"],
                           "s"[:r["named_backups"] != 1]) if r["named_backups"]
                        else "NO snapshot of %s — only a fresh login brings it "
                             "back" % r["name"])
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
    args = list(args)
    quiet = "--quiet" in args
    apply = "--apply" in args
    args = [a for a in args if a not in ("--quiet", "--apply")]
    name = _take_flag(args, "--home")
    all_homes = "--all" in args
    args = [a for a in args if a != "--all"]
    if args or (all_homes and name):
        if not quiet:
            print("helm cred backup: invalid arguments", file=sys.stderr)
        return 2
    if all_homes:
        results = backup_all(apply=apply)
    else:
        path, err = _resolve_home(name)
        if err:
            if not quiet:
                print("helm cred: " + err, file=sys.stderr)
            return 1
        results = [backup(path, apply=apply)]
    if quiet:
        return 1 if any(not r["ok"] for r in results) else 0
    made = [r for r in results if r["action"] == "backup"]
    planned = [r for r in results if r["action"] == "would-backup"]
    for r in results:
        if r["action"] == "backup":
            print("  backed up %-32s <- %s  (%s)"
                  % (r["account"], r["name"], _display_path(r["dest"])))
        elif r["action"] == "would-backup":
            print("  WOULD BACK UP %-26s <- %s" % (r["account"], r["name"]))
        elif r["ok"]:
            print("  %-32s %s (%s)" % (r["account"], r["reason"], r["name"]))
        else:
            print("  SKIP %-27s %s" % (r["name"], r["reason"]))
    if apply:
        print("helm cred backup (APPLIED): %d snapshot%s written, %d already current "
              "(root %s, 0700 dirs / 0600 files — token bytes are copied, never printed)"
              % (len(made), "s"[:len(made) != 1], len(results) - len(made),
                 _display_path(backup_root())))
    else:
        print("helm cred backup (dry-run): %d snapshot%s would be written; add --apply "
              "(%d already current, no filesystem changes)"
              % (len(planned), "s"[:len(planned) != 1], len(results) - len(planned)))
    return 1 if any(not r["ok"] for r in results) else 0


def _print_switch_guard(args):
    args = list(args)
    apply = "--apply" in args
    args = [a for a in args if a not in ("--apply", "--dry")]
    install = "--install" in args
    args = [a for a in args if a != "--install"]
    name = _take_flag(args, "--home")
    if args or (install and name):
        print("helm cred switch-guard: invalid arguments", file=sys.stderr)
        return 2
    if install:
        from . import hooks
        targets = hooks.claude_homes()
        if not targets:
            print("helm cred: no claude homes to guard")
            return 0
        worst = 0
        for hname, path in targets:
            action, detail = hooks.install_home(path, dry=not apply, specs=GUARD_SPECS)
            print("  %-30s %s" % (hname, action))
            if action == "fail":
                print("    hook install failed", file=sys.stderr)
                worst = 1
        print("helm cred switch-guard (%s): SessionStart + Stop guard %s in %d home%s"
              % ("APPLIED" if apply else "dry-run — add --apply",
                 "installed" if apply else "would be installed",
                 len(targets), "s"[:len(targets) != 1]))
        return worst
    path, err = _resolve_home(name)
    if err:
        print("helm cred: " + err, file=sys.stderr)
        return 1
    res = backup(path, apply=apply)
    if not res["ok"]:
        print("helm cred switch-guard: NOT protected — %s" % res["reason"],
              file=sys.stderr)
        return 1
    if not apply and res["action"] == "would-backup":
        print("helm cred switch-guard (dry-run): %s is NOT protected yet; add --apply"
              % res["account"])
        return 0
    protected = ("snapshot " + _display_path(res["dest"])
                 if res["action"] == "backup" else
                 "already snapshotted: " + _display_path(res["dest"]))
    print("helm cred switch-guard: %s is protected (%s)" % (res["account"], protected))
    shown = _display_path(path)
    if shown != "<redacted-path>":
        print("  now safe to run:  %s" % homes.LOGIN_CMDS["claude"](shown))
    else:
        print("  home path redacted; select it by name before running /login")
    print("  after the login: `helm cred list` shows what this home now holds; "
          "`helm cred heal` puts %s back when no session holds it." % res["account"])
    return 0


def _print_heal(args):
    apply = "--apply" in args
    as_json = "--json" in args
    rest = [a for a in args if a not in ("--apply", "--json")]
    if len(rest) > 1 or any(a.startswith("-") for a in rest):
        print("helm cred heal: invalid arguments", file=sys.stderr)
        return 2
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
    bad = apply and any(p["status"] != "restored" for p in plans)
    return 1 if bad else 0


def cmd_cred(args):
    """cred [list|backup [--all] [--apply]|switch-guard [--install] [--apply]|heal [--apply]]"""
    args = list(args)
    verb = args[0] if args and not args[0].startswith("-") else "list"
    rest = args[1:] if args and not args[0].startswith("-") else args
    try:
        if verb in ("list", "ls"):
            if any(a != "--json" for a in rest):
                print("helm cred list: invalid arguments", file=sys.stderr)
                return 2
            return _print_list(rest)
        if verb == "backup":
            return _print_backup(rest)
        if verb in ("switch-guard", "guard"):
            return _print_switch_guard(rest)
        if verb == "heal":
            return _print_heal(rest)
        print("helm cred: unknown subverb — %s" % cmd_cred.__doc__, file=sys.stderr)
        return 2
    except Exception as e:
        # Last-resort CLI boundary: exception strings can embed paths or parsed
        # input. Class-only reporting guarantees no credential/token traceback.
        print("helm cred: operation failed (%s)" % e.__class__.__name__, file=sys.stderr)
        return 1
