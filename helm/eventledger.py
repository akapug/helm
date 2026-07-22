#!/usr/bin/env python3
"""Small append-only JSONL event-ledger primitive.

Writers serialize on a stable sibling lock, append one bounded JSON event with
O_APPEND, fsync it, and roll a short write back before releasing the lock.
Readers skip malformed, non-UTF8, oversized, and truncated tail rows.  Paths
are fail-closed on symlinks; callers turn failures into their domain-specific
fail-open result (an empty read or a loud refused mutation).
"""
import contextlib
import json
import os
import stat

MAX_EVENT_BYTES = 64 * 1024


def _flags(base):
    return base | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


def _prepare(path, create=False):
    p = os.path.abspath(path)
    parent = os.path.dirname(p)
    if create:
        ancestor = parent
        while not os.path.lexists(ancestor):
            older = os.path.dirname(ancestor)
            if older == ancestor:
                break
            ancestor = older
        if os.path.realpath(ancestor) != ancestor:
            raise OSError("ledger parent contains a symlink")
        os.makedirs(parent, mode=0o700, exist_ok=True)
    if os.path.realpath(parent) != parent:
        raise OSError("ledger parent contains a symlink")
    st = os.stat(parent, follow_symlinks=False)
    if not stat.S_ISDIR(st.st_mode):
        raise OSError("ledger parent is not a directory")
    try:
        mode = os.lstat(p).st_mode
        if stat.S_ISLNK(mode):
            raise OSError("ledger path is a symlink")
        if not stat.S_ISREG(mode):
            raise OSError("ledger path is not a regular file")
    except FileNotFoundError:
        pass
    return p


@contextlib.contextmanager
def locked(path):
    """Yield True while holding the ledger's stable sibling lock, False when
    path/lock setup fails.  A mutation never proceeds unlocked."""
    try:
        p = _prepare(path, create=True)
        fd = os.open(p + ".lock", _flags(os.O_RDWR | os.O_CREAT), 0o600)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError("ledger lock is not a regular file")
        os.fchmod(fd, 0o600)
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)
    except (OSError, ValueError):
        if "fd" in locals():
            os.close(fd)
        yield False
        return
    try:
        yield True
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def events(path):
    """Return valid complete dict events in append order.  Trouble is empty.
    A final line without a newline is a torn write and is never replayed."""
    out = []
    fd = None
    try:
        p = _prepare(path)
        fd = os.open(p, _flags(os.O_RDONLY))
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError("ledger is not a regular file")
        with os.fdopen(fd, "rb") as f:
            fd = None
            for raw in f:
                if not raw.endswith(b"\n") or len(raw) > MAX_EVENT_BYTES:
                    continue
                try:
                    row = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    continue
                if isinstance(row, dict) and row.get("id"):
                    out.append(row)
    except OSError:
        pass
    finally:
        if fd is not None:
            os.close(fd)
    return out


def latest(path, accept=None):
    """Replay events to id -> latest snapshot.  ``accept(row, prior)`` may
    reject corrupt transitions without erasing the preceding good snapshot."""
    out = {}
    for row in events(path):
        rid = str(row.get("id"))
        prior = out.get(rid)
        if accept is None or accept(row, prior):
            out[rid] = row
    return out


def append_unlocked(path, row):
    """Append one event while the caller holds ``locked(path)``.

    Returns False on any failure.  A partial os.write is truncated back to the
    pre-write size before the lock can be released, so replay never mistakes a
    torn tail for an event.
    """
    fd = None
    before = None
    try:
        payload = (json.dumps(row, ensure_ascii=False, separators=(",", ":"))
                   + "\n").encode("utf-8")
        if len(payload) > MAX_EVENT_BYTES:
            return False
        p = _prepare(path, create=True)
        fd = os.open(p, _flags(os.O_WRONLY | os.O_APPEND | os.O_CREAT), 0o600)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError("ledger is not a regular file")
        os.fchmod(fd, 0o600)
        before = st.st_size
        if os.write(fd, payload) != len(payload):
            os.ftruncate(fd, before)
            os.fsync(fd)
            return False
        try:
            os.fsync(fd)
        except OSError:
            os.ftruncate(fd, before)
            raise
        return True
    except (OSError, TypeError, ValueError):
        if fd is not None and before is not None:
            try:
                os.ftruncate(fd, before)
            except OSError:
                pass
        return False
    finally:
        if fd is not None:
            os.close(fd)


def append(path, row):
    with locked(path) as held:
        return append_unlocked(path, row) if held else False
