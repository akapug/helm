"""helm.cred shared constants, module state, and leaf helpers.

Split out of the original single-module helm/cred.py (see this package's
__init__ for the laws and the incident). `_cred` is the package namespace:
test-patchable seams (mock.patch.object(cred, ...)) resolve through it at
call time, exactly as they did when every function shared one module dict.
"""
import json
import os
import stat
import tempfile

from .. import cred as _cred
from .. import homes


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
_BACKUP_GUARDS = (
    {"name": "cred-guard", "event": "SessionStart",
     "args": "cred backup --apply --quiet", "timeout": 5,
     "own": ("cred backup --apply --quiet", "helm cred backup"), "matcher": "*"},
    {"name": "cred-guard-turn", "event": "Stop",
     "args": "cred backup --apply --quiet", "timeout": 5,
     "own": ("cred backup --apply --quiet", "helm cred backup"), "matcher": None},
)
# The HEAL leg: the guard doesn't just make the incident reversible, it
# REVERSES it — every turn boundary (and session start; heal_plan is a
# handful of file reads when nothing drifted) runs `heal --apply --quiet`,
# which only ever touches a DRIFTED home no live process holds. So a live
# borrowing session is never evicted, and the polluted home self-restores at
# the first fleet turn boundary after the borrower frees it.
_HEAL_GUARDS = (
    {"name": "cred-heal", "event": "SessionStart",
     "args": "cred heal --apply --quiet", "timeout": 10,
     "own": ("cred heal --apply --quiet", "helm cred heal"), "matcher": "*"},
    {"name": "cred-heal-turn", "event": "Stop",
     "args": "cred heal --apply --quiet", "timeout": 10,
     "own": ("cred heal --apply --quiet", "helm cred heal"), "matcher": None},
)
# Backup strictly before heal, BY CONSTRUCTION: hooks._merge_event appends
# entries in spec order, so within each event the snapshot hook precedes the
# heal hook in every settings.json this tuple installs. And the ordering is
# belt-and-braces, not load-bearing alone: heal itself refuses to evict an
# occupant it could not snapshot first (the no-preimage refusal), so
# backup-before-heal survives even a harness that runs same-event hooks
# concurrently.
GUARD_SPECS = _BACKUP_GUARDS + _HEAL_GUARDS

# The heal hook auto-types --apply and --quiet swallows every warning, so the
# unattended path REFUSES what the manual path merely warns about: a stale
# pre-image (stale-preimage), an expiry it cannot even read (expiry-unknown),
# a snapshot whose token family shows identity-discontinuity evidence of a
# mid-/login tear (torn-pair). A family ever seen live in another home
# (revocation-risk via lineage) is refused on BOTH paths. Warnings are for
# humans; a hook that can only warn itself protects no one.

# Family LINEAGE: which home NAMES (and account emails) each live token
# family has been observed with, persisted across turns at
# backup_root()/family-lineage.json. The live-bytes clash check alone is
# rotation-blind — one borrower refresh after a byte-copy borrow, the hashes
# diverge and the clash vanishes — so the refusal has to remember: a snapshot
# whose family was EVER seen live in a home other than its restore target is
# never restored (its refresh token was rotated, i.e. CONSUMED, there;
# restoring the copy trips server-side reuse detection and revokes the whole
# family, bricking the live borrower). The account column is the tear
# detector's memory: a family recorded live under one account can never be
# captured or restored bound to another.
LINEAGE_FILE = "family-lineage.json"
LINEAGE_KEEP = 512               # newest families kept; a rotated-away family ages out
# The count cap yields to the invariant: a family still claimed by any
# surviving snapshot's meta is never evicted, because the snapshot it guards
# (a possibly consumed token) outlives any count of newer families.


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
    """(bytes, mode) from one non-symlink regular file. lstat/open/fstat inode
    equality closes both symlink and regular-file swap races, even where
    O_NOFOLLOW is unavailable."""
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise OSError("not a plain regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode)
                or (st.st_dev, st.st_ino) != (before.st_dev, before.st_ino)):
            raise OSError("file changed during open")
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
    tmp = _cred._stage_private(path, data, mode)
    try:
        os.replace(tmp, path)
        _cred._fsync_dir(os.path.dirname(path))
    except OSError:
        _unlink(tmp)
        raise


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
