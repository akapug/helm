"""helm.cred snapshots cluster: estate rows, family lineage, backup, restore
(see __init__)."""
import fcntl
import glob
import hashlib
import json
import os
import signal
import stat
import time

from .. import cred as _cred
from .. import homes
from ._common import (ACCOUNT_JSON, AUTH_JSON, KEEP, LINEAGE_FILE,
                      LINEAGE_KEEP, TS_FMT, _atomic_private, _email_or_none,
                      _json_bytes, _read_json, _read_regular, _secure_dir,
                      _stat_key, _str_or_none, _unlink, _write_private,
                      backup_root, cache_clear)
from .account import account_of, verdict_for


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


def _family_of_blob(blob):
    """Token-family fingerprint of raw credential bytes — hash in memory,
    digest prefix only, same law as _snapshot_family."""
    doc = _json_bytes(blob) or {}
    tok = (doc.get("claudeAiOauth") or {}).get("refreshToken") \
        if isinstance(doc, dict) else None
    if not isinstance(tok, str) or not tok:
        return None
    return hashlib.sha256(tok.encode()).hexdigest()[:10]


# ---------------------------------------------------------- family lineage ---
def _lineage_path():
    return os.path.join(backup_root(), LINEAGE_FILE)


def _lineage_load():
    doc = _read_json(_lineage_path())
    fams = doc.get("families") if isinstance(doc, dict) else None
    return fams if isinstance(fams, dict) else {}


def _lineage_homes(fam):
    """Home NAMES this family was ever recorded live in. Empty when unknown —
    the lineage is one refusal layer among several, never the only one."""
    entry = _lineage_load().get(fam)
    names = entry.get("homes") if isinstance(entry, dict) else None
    return ({n for n in names if isinstance(n, str)}
            if isinstance(names, list) else set())


def _lineage_accounts(fam):
    """Account emails this family was ever recorded live under. Empty when
    unknown (entries predating the account column, or a census that could not
    read the home's identity) — one refusal layer, never the only one."""
    entry = _lineage_load().get(fam)
    accts = entry.get("accounts") if isinstance(entry, dict) else None
    if not isinstance(accts, list):
        return set()
    return {a for a in (_email_or_none(x) for x in accts) if a}


def _census_pair(real):
    """(family, account_email) for a live home, read under a stat bracket over
    BOTH credential files — or None when a concurrent write straddles the two
    reads. The lineage census pairs a home's token FAMILY (.credentials.json)
    with the ACCOUNT it is live under (.claude.json); read on opposite sides of
    a /login those two files disagree — the family is still the evicted
    account's, the identity already the arriving one's — and the union-only,
    never-pruned accounts column would then record a (family, account) pairing
    that never existed on disk, thereafter auto-heal-refusing the evicted
    account's OWN legitimate snapshots as a torn pair. /login is precisely the
    event that precedes drift, so this ms-scale window is aligned with the very
    incident the lineage exists to auto-heal. Mirrors _capture_home's bracket:
    a home mid-/login-write is not a trustworthy lineage input, and dropping
    one census cycle's row is safe — the next stable census records it
    correctly. Only a family DIGEST and an email ever leave this function;
    no token byte does."""
    auth = os.path.join(real, AUTH_JSON)      # the token family lives here
    cfg = os.path.join(real, ACCOUNT_JSON)    # the identity lives here
    for _ in range(2):
        before = (_stat_key(auth), _stat_key(cfg))
        fam = homes._token_family("claude", real)
        acct = account_of(real)
        if before == (_stat_key(auth), _stat_key(cfg)):
            return fam, (acct["email"] if acct["ok"] else None)
    return None


def _snapshot_families_on_disk():
    """Every family a surviving snapshot's meta still claims — the set the
    lineage prune must never evict: the snapshot outlives any count of newer
    families, and evicting its lineage entry would expire the ever-live-
    elsewhere refusal while the consumed token it guards is still restorable."""
    fams = set()
    for d in sorted(glob.glob(os.path.join(backup_root(), "*"))):
        if not os.path.isdir(d) or os.path.islink(d):
            continue
        for s in _snapshots_in(d):
            fam = s.get("family") or _snapshot_family(s["path"])
            if fam:
                fams.add(fam)
    return fams


def _lineage_record(rows_seen):
    """Merge (family, home-name[, account]) observations into the persisted
    lineage. Called only from APPLY paths (dry-runs stay recursive-metadata
    no-ops). The read-merge-write runs under an flock so two concurrent hooks
    (one session's Stop, another's SessionStart) cannot silently drop each
    other's census. Best-effort by design: a failed lock or write costs
    future lineage coverage, but the live-bytes clash check and the hook's
    stale refusal still stand."""
    seen = [r for r in rows_seen if r[0] and r[1]]
    if not seen:
        return
    lock = None
    try:
        _secure_dir(backup_root())
        lock = os.fdopen(os.open(_lineage_path() + ".lock",
                                 os.O_WRONLY | os.O_CREAT, 0o600), "w")
        fcntl.flock(lock, fcntl.LOCK_EX)
    except OSError:
        lock = None          # unlocked fallback — no worse than the old race
    try:
        fams = _lineage_load()
        now = int(time.time())
        for row in seen:
            fam, home_name = row[0], row[1]
            acct = _email_or_none(row[2]) if len(row) > 2 else None
            entry = fams.get(fam)
            if not isinstance(entry, dict) or not isinstance(entry.get("homes"), list):
                entry = {"homes": []}
            entry["homes"] = sorted({h for h in entry["homes"]
                                     if isinstance(h, str)} | {home_name})
            prior = entry.get("accounts")
            accts = ({a for a in prior if isinstance(a, str)}
                     if isinstance(prior, list) else set())
            if acct:
                accts.add(acct)
            entry["accounts"] = sorted(accts)
            entry["ts"] = now
            fams[fam] = entry
        if len(fams) > LINEAGE_KEEP:
            held = _snapshot_families_on_disk()
            stale = [f for f in sorted(
                fams, key=lambda f: (fams[f].get("ts")
                                     if isinstance(fams[f], dict) else 0) or 0)
                if f not in held]
            for fam in stale[:len(fams) - LINEAGE_KEEP]:
                del fams[fam]
        try:
            _atomic_private(_lineage_path(), json.dumps(
                {"families": fams}, indent=2, sort_keys=True).encode())
        except OSError:
            pass
    finally:
        if lock:
            lock.close()


# ----------------------------------------------------------------- backups ---
def folded_dir(folded):
    return os.path.join(backup_root(), folded)


def account_dir(email):
    """The snapshot dir for an account: ~/.cred-backups/<folded-email>/ — the
    same fold homes.py names homes with (owner@example.com -> owner-example-com)."""
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
                    "family": meta.get("family"),
                    "pair": meta.get("pair"),
                    "has_creds": _stat_key(os.path.join(p, "credentials.json")) is not None})
    return out


def snapshots(email):
    """Snapshots claiming this exact normalized account, oldest first. The
    folded directory name is lossy (`+` and `-` collide), so account metadata —
    never the directory alone — owns filtering and retention."""
    expected = _email_or_none(email)
    return ([s for s in _snapshots_in(account_dir(expected))
             if s["account"] == expected] if expected else [])


def snapshots_for_home_name(name):
    """Snapshots of the account a home NAME promises — the folded email IS the
    canonical home name, so the name resolves the backup dir directly."""
    return _snapshots_in(folded_dir(name))


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


def _foreign_family(fam, email):
    """The OTHER account whose snapshots already claim this token family, or
    None. The bytes-vs-identity coherence check for a mixed home: one home =
    one family, and a family belongs to exactly one account, so token bytes
    whose family is filed under a different account's snapshots cannot
    coherently be bound to `email` — that pairing is the torn/mixed-home
    signature, and snapshotting it would mint a poisoned pre-image that
    passes every digest check downstream."""
    if not fam:
        return None
    expected = _email_or_none(email)
    for d in sorted(glob.glob(os.path.join(backup_root(), "*"))):
        if not os.path.isdir(d) or os.path.islink(d):
            continue
        for s in _snapshots_in(d):
            if not s["account"] or s["account"] == expected:
                continue
            sfam = s.get("family") or _snapshot_family(s["path"])
            if sfam and sfam == fam:
                return s["account"]
    return None


def _capture_home(real):
    """Stable credential + identity pre-image, or a secret-free reason. Both
    files are read through checked descriptors and their inode/stat keys are
    bracketed; a concurrent /login can therefore make capture REFUSE, never
    file one account's tokens under another account's identity. Where the
    estate's own history makes it checkable (_foreign_family), a home whose
    token bytes belong to ANOTHER account's snapshots is refused outright —
    the post-SIGKILL mixed-home shape no bracket can see. The capture also
    records both files' stat brackets (mtime/size) plus a capture timestamp,
    so heal can later refuse a pair whose write times betray a mid-/login
    tear."""
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
        fam = _family_of_blob(blob)
        other = _foreign_family(fam, email)
        if other:
            return None, ("credential bytes carry a token family already "
                          "snapshotted for another account (%s) — a mixed or "
                          "torn home; refusing to bind them to %s"
                          % (other, email))
        # The no-history half of the same check: a home never snapshotted has
        # no _foreign_family record, but the lineage census (which sees every
        # live home each turn boundary) may still know whose account these
        # token bytes were live under. Fresh identity over another account's
        # stale tokens (the opposite tear) is refused here even on the
        # first-ever capture of a home.
        owners = _lineage_accounts(fam)
        if owners and email not in owners:
            return None, ("credential bytes carry a token family the lineage "
                          "has only seen live under another account (%s) — a "
                          "mixed or torn home; refusing to bind them to %s"
                          % (sorted(owners)[0], email))
        pair = {"cfg": {"mtime_ns": before[0][0], "size": before[0][1],
                        "sha12": hashlib.sha256(cfg_blob).hexdigest()[:12]},
                "auth": {"mtime_ns": before[1][0], "size": before[1][1]},
                "captured_at_ms": int(time.time() * 1000)}
        return {"blob": blob, "oa": oa, "email": email,
                "family": fam, "pair": pair,
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
        if apply:
            # The observation still counts even when the bytes are already
            # filed: keepalive's pre-rotation capture lands here whenever the
            # home was snapshotted at the last turn boundary, and WITHOUT the
            # census the family it is about to rotate away would never enter
            # the lineage (dry-runs stay recursive-metadata no-ops).
            _lineage_record([(cap["family"], name, email)])
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
            "family": cap["family"], "pair": cap["pair"],
            "note": "digest/family are sha256 PREFIXES (content fingerprints); "
                    "token bytes live only in credentials.json, 0600, never "
                    "printed; pair records both files' capture stat brackets",
        }, indent=2).encode())
        _cred._fsync_dir(dest)
        _cred._fsync_dir(account_dir(email))
    except OSError as e:
        _drop(dest)
        return {"ok": False, "action": "skip", "home": real, "name": name,
                "account": email,
                "reason": "snapshot write failed (%s)" % e.__class__.__name__}
    _lineage_record([(cap["family"], name, email)])
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
    return [_cred.backup(r["real"], apply=apply) for r in _cred.rows() if r["authed"]]


def _pre_image(path):
    """Exact file pre-image. Absence is distinct from an unreadable/symlink
    file; only true ENOENT may later roll back by deletion."""
    try:
        blob, mode = _read_regular(path)
        return {"exists": True, "blob": blob, "mode": mode,
                "key": _stat_key(path)}, None
    except FileNotFoundError:
        return {"exists": False, "blob": None, "mode": None, "key": None}, None
    except OSError as e:
        return None, "pre-image unreadable (%s)" % e.__class__.__name__


def _rollback_files(pre):
    """Restore exact bytes, modes, and original absence after a failed commit.

    BEST-EFFORT PER FILE, credentials first: the same persistent fault that
    broke the commit (disk-full, EIO) can also break the undo, and an all-or-
    nothing rollback that aborts on the FIRST file leaves the MORE sensitive
    one unrestored. The credential file is rolled back before the identity
    file, and a failure on either is recorded — never allowed to skip the
    other. -> True only when every file is byte-exactly back."""
    ordered = sorted(pre.items(), key=lambda kv: 0 if kv[0].endswith(AUTH_JSON) else 1)
    staged = {}
    ok = True
    for path, image in ordered:
        if not image["exists"]:
            continue
        try:
            staged[path] = _cred._stage_private(path, image["blob"], image["mode"])
        except OSError:
            ok = False          # cannot even stage this file's undo — still try the rest
    for path, image in ordered:
        try:
            if image["exists"]:
                if path in staged:
                    os.replace(staged[path], path)
            elif os.path.lexists(path):
                os.unlink(path)
        except OSError:
            ok = False
    try:
        _cred._fsync_dir(os.path.dirname(next(iter(pre))))
    except OSError:
        ok = False
    for tmp in staged.values():
        _unlink(tmp)
    return ok


# Catchable termination signals blocked across restore's two-file commit. The
# heal hook runs under `timeout 10` (hooks.py spec_command) — a SCHEDULED
# SIGTERM, not crash luck — and a kill landing between the two os.replace
# calls would leave a MIXED home: restored credentials under the occupant's
# identity. SIGKILL/power-loss cannot be masked; that residue is why
# _capture_home refuses to snapshot a home whose token bytes already belong
# to another account's snapshots.
_COMMIT_SIGNALS = frozenset(
    getattr(signal, n) for n in ("SIGTERM", "SIGINT", "SIGHUP", "SIGQUIT")
    if hasattr(signal, n))


def _block_commit_signals():
    try:
        return signal.pthread_sigmask(signal.SIG_BLOCK, _COMMIT_SIGNALS)
    except (AttributeError, ValueError, OSError):
        return None                # platform without pthread_sigmask: best effort


def _unblock_commit_signals(old):
    if old is None:
        return
    try:
        signal.pthread_sigmask(signal.SIG_SETMASK, old)
    except (ValueError, OSError):
        pass


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
        meta_blob, _ = _read_regular(os.path.join(snap, "meta.json"))
    except (OSError, ValueError) as e:
        return None, None, "snapshot unreadable (%s)" % e.__class__.__name__
    oa = _json_bytes(account_blob)
    email = _email_or_none(oa.get("emailAddress")) if isinstance(oa, dict) else None
    if not email:
        return None, None, "snapshot has no valid identity block — refusing"
    meta = _json_bytes(meta_blob)
    if (not isinstance(meta, dict) or _email_or_none(meta.get("account")) != email
            or meta.get("digest") != hashlib.sha256(blob).hexdigest()[:12]
            or meta.get("bytes") != len(blob)):
        return None, None, "snapshot metadata/content mismatch — refusing"
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
    home_mode = stat.S_IMODE(st.st_mode)
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
            staged.append((path, _cred._stage_private(path, data, 0o600)))
    except OSError as e:
        for _, tmp in staged:
            _unlink(tmp)
        return {"ok": False, "error": "write failed (%s)" % e.__class__.__name__}
    if require_free:
        # IRREDUCIBLE TOCTOU residual, on the record: without an flock on the
        # home, a session launching into this drifted dir in the few ms
        # between this probe and the replaces below starts on bytes we are
        # about to swap. Three probes bracket the window (plan, post-backup
        # re-probe, here), helm never launches into DRIFT homes, the occupant
        # is snapshotted first so nothing is lost, and that session's own
        # next credential write re-drifts the home so heal refuses (held)
        # thereafter. Accepted residual — not fixable at this layer.
        held = _cred.holders_of(real)
        if held is None or held:
            for _, tmp in staged:
                _unlink(tmp)
            return {"ok": False, "error": "home is no longer proven free — refusing"}
    for path, image in pre.items():
        stable = (_stat_key(path) == image["key"] if image["exists"]
                  else not os.path.lexists(path))
        if not stable:
            for _, tmp in staged:
                _unlink(tmp)
            return {"ok": False, "error": "home changed during restore — refusing"}
    changed = []
    dir_changed = False
    # The whole commit — replaces, verify, AND any rollback — runs with
    # catchable termination signals blocked: the pair lands together or is
    # rolled back together, never left half-swapped by the hook's own
    # `timeout 10` SIGTERM.
    mask = _block_commit_signals()
    try:
        try:
            os.chmod(real, 0o700, follow_symlinks=False)
            dir_changed = home_mode != 0o700
            for path, tmp in staged:
                os.replace(tmp, path)
                changed.append(path)
            _cred._fsync_dir(real)
            got, mode = _read_regular(auth)
            if got != blob or mode != 0o600:
                raise OSError("credential verification failed")
            cfg_blob, cfg_mode = _read_regular(cfg)
            cfg_doc = _json_bytes(cfg_blob)
            if (stat.S_IMODE(os.lstat(real).st_mode) != 0o700
                    or cfg_mode != 0o600 or not isinstance(cfg_doc, dict)
                    or _email_or_none((cfg_doc.get("oauthAccount") or {}).get(
                        "emailAddress")) != oa["emailAddress"]):
                raise OSError("identity verification failed")
        except OSError as e:
            for _, tmp in staged:
                _unlink(tmp)
            rolled = _rollback_files({p: pre[p] for p in changed}) if changed else True
            if dir_changed:
                try:
                    os.chmod(real, home_mode, follow_symlinks=False)
                    _cred._fsync_dir(real)
                except OSError:
                    rolled = False
            cache_clear()
            if not rolled:
                return {"ok": False, "error": "write failed (%s) and exact rollback failed"
                        % e.__class__.__name__}
            return {"ok": False, "error": "write failed (%s); exact pre-image restored"
                    % e.__class__.__name__}
    finally:
        _unblock_commit_signals(mask)
    cache_clear()
    return {"ok": True, "account": oa["emailAddress"]}
