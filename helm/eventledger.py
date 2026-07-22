#!/usr/bin/env python3
"""Durable append-only JSONL event-ledger primitive.

Writers serialize on a stable sibling lock, repair only an incomplete tail,
append one bounded event with O_APPEND, fsync file + new directory entries, and
roll short writes back. Readers distinguish an absent ledger (known empty) from
an unsafe/unreadable ledger (obligations unknown), while domain callers choose
whether that becomes loud unavailable state or their legacy fail-open result.
"""
import contextlib
import json
import os
import stat

MAX_EVENT_BYTES = 64 * 1024


def _flags(base):
    return base | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


def _fsync_dir(path):
    fd = os.open(path, _flags(os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _mkdirs(path):
    missing = []
    ancestor = path
    while not os.path.lexists(ancestor):
        missing.append(ancestor)
        older = os.path.dirname(ancestor)
        if older == ancestor:
            break
        ancestor = older
    if os.path.realpath(ancestor) != ancestor:
        raise OSError("ledger parent contains a symlink")
    for child in reversed(missing):
        parent = os.path.dirname(child)
        os.mkdir(child, 0o700)
        _fsync_dir(parent)


def _prepare(path, create=False):
    p = os.path.abspath(path)
    parent = os.path.dirname(p)
    if create:
        _mkdirs(parent)
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
    """Yield True under the stable sibling lock, False when setup fails.
    Mutations never proceed unlocked."""
    try:
        p = _prepare(path, create=True)
        lock_path = p + ".lock"
        new_lock = not os.path.exists(lock_path)
        fd = os.open(lock_path, _flags(os.O_RDWR | os.O_CREAT), 0o600)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise OSError("ledger lock is not a private regular file")
        os.fchmod(fd, 0o600)
        if new_lock:
            _fsync_dir(os.path.dirname(lock_path))
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


def checked_events(path):
    """Return (complete dict events, unavailable reason).

    A missing file or parent is a known empty ledger. Unsafe paths, permission
    failures, and other I/O errors are UNKNOWN, not zero obligations.
    """
    out, fd = [], None
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
        return out, None
    except FileNotFoundError:
        return [], None
    except OSError as exc:
        return [], "%s: %s" % (type(exc).__name__, exc)
    finally:
        if fd is not None:
            os.close(fd)


def events(path):
    return checked_events(path)[0]


def latest_checked(path, accept=None):
    events_, unavailable = checked_events(path)
    if unavailable:
        return {}, unavailable
    out = {}
    for row in events_:
        rid = str(row.get("id"))
        prior = out.get(rid)
        if accept is None or accept(row, prior):
            out[rid] = row
    return out, None


def latest(path, accept=None):
    return latest_checked(path, accept)[0]


def _trim_torn_tail(fd, size):
    """Return the last complete-line boundary under the ledger lock."""
    if not size or os.pread(fd, 1, size - 1) == b"\n":
        return size
    end = size
    while end:
        start = max(0, end - 8192)
        chunk = os.pread(fd, end - start, start)
        newline = chunk.rfind(b"\n")
        if newline >= 0:
            return start + newline + 1
        end = start
    return 0


def append_unlocked(path, row):
    """Append one event while the caller holds ``locked(path)``.

    Repairs a pre-existing torn tail, writes once, fsyncs, and rolls a partial
    write back. A newly-created ledger is not acknowledged until its containing
    directory entry has also been fsynced.
    """
    fd, before = None, None
    try:
        payload = (json.dumps(row, ensure_ascii=False, separators=(",", ":"))
                   + "\n").encode("utf-8")
        if len(payload) > MAX_EVENT_BYTES:
            return False
        p = _prepare(path, create=True)
        new_file = not os.path.exists(p)
        fd = os.open(p, _flags(os.O_RDWR | os.O_APPEND | os.O_CREAT), 0o600)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise OSError("ledger is not a private regular file")
        os.fchmod(fd, 0o600)
        before = _trim_torn_tail(fd, st.st_size)
        if before != st.st_size:
            os.ftruncate(fd, before)
            os.fsync(fd)
        if os.write(fd, payload) != len(payload):
            os.ftruncate(fd, before)
            os.fsync(fd)
            return False
        try:
            os.fsync(fd)
            if new_file:
                _fsync_dir(os.path.dirname(p))
        except OSError:
            os.ftruncate(fd, before)
            os.fsync(fd)
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
