"""Filesystem primitives the standard library does not expose.

WHY THIS MODULE EXISTS RATHER THAN AN IMPORT FROM `configs`: the
config editor happens to be where the renameat2 binding was first needed, but
editing configuration is not the owner of a kernel primitive. A second consumer
importing `configs._io` to move a directory would make every filesystem caller
depend on the config subsystem's import graph and lifecycle, which is a
dependency nobody would choose on purpose. The binding moves to a module named
for what it IS; `configs` keeps its own wrapper and its monkeypatch seam
untouched, so nothing over there changes shape.

`os.rename` is destructive BY SPECIFICATION — POSIX says it replaces the
destination atomically — so every check-then-rename is a TOCTOU. RENAME_NOREPLACE
moves the check INSIDE the syscall, which is the only place it cannot be raced,
for the same reason `os.link` rather than a copy is the publish primitive
wherever a no-clobber create is needed.
"""
import ctypes
import errno
import os

# Linux spells it renameat2; macOS spells the same dirfd-relative primitive
# renameatx_np. Darwin's RENAME_SWAP == 2 == Linux's RENAME_EXCHANGE, so the
# EXCHANGE flag carries across unchanged. NOREPLACE is Linux-only in practice;
# callers must treat its absence as "cannot do this safely", never as licence
# to fall back to a plain rename, which is precisely the clobber being avoided.
RENAME_NOREPLACE = 1
RENAME_EXCHANGE = 2


def pread(fd, size, offset):
    """Read at one offset, preserving it when ``os.pread`` is unavailable.

    The fallback moves a shared descriptor, so callers must already serialize
    access to it. Restoring the cursor keeps positional-read semantics intact.
    """
    read = getattr(os, "pread", None)
    if read is not None:
        return read(fd, size, offset)
    cursor = os.lseek(fd, 0, os.SEEK_CUR)
    try:
        os.lseek(fd, offset, os.SEEK_SET)
        return os.read(fd, size)
    finally:
        os.lseek(fd, cursor, os.SEEK_SET)


_LIBC = ctypes.CDLL(None, use_errno=True)
_RENAMEAT2 = getattr(_LIBC, "renameat2", None)
if _RENAMEAT2 is None:
    _RENAMEAT2 = getattr(_LIBC, "renameatx_np", None)
if _RENAMEAT2 is not None:
    _RENAMEAT2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                           ctypes.c_char_p, ctypes.c_uint)
    _RENAMEAT2.restype = ctypes.c_int


def have_renameat2():
    """Whether the SYMBOL is bound. Necessary, and nowhere near sufficient —
    see supports_noreplace(), which is what callers should ask."""
    return _RENAMEAT2 is not None


def supports_noreplace(dirfd):
    """Does RENAME_NOREPLACE actually WORK on this substrate? -> None | why.

    A BOUND SYMBOL IS NOT A WORKING SYSCALL (item 8). have_renameat2
    was true whenever libc exported renameat2 — so a kernel returning ENOSYS,
    or a filesystem answering EINVAL/EOPNOTSUPP to the NOREPLACE flag, passed
    the preflight, and the claim directory, undo directory and manifest were
    all created before the first real call discovered it. The transaction left
    artifacts behind while reporting that nothing was removed.

    So the preflight EXERCISES the operation, with the exact flag, on the
    directory the transaction will actually use — a rename of a name that does
    not exist. ENOENT means the machinery works and only the source was
    missing, which is the answer we want. ENOSYS/EINVAL/EOPNOTSUPP mean this
    substrate cannot promise no-clobber, and that must be known BEFORE
    anything is created."""
    if _RENAMEAT2 is None:
        return "renameat2 is not available on this system"
    probe = ".helm-noreplace-probe-%s" % os.urandom(4).hex()
    try:
        renameat2(dirfd, probe, dirfd, probe + ".dst", RENAME_NOREPLACE)
    except OSError as e:
        if e.errno == errno.ENOENT:
            return None            # the flag was accepted; the source was not
        if e.errno in (getattr(errno, "ENOSYS", 38),
                       getattr(errno, "EINVAL", 22),
                       getattr(errno, "EOPNOTSUPP", 95),
                       getattr(errno, "ENOTSUP", 95)):
            return ("this filesystem or kernel does not support a no-clobber "
                    "rename (%s), so a move here could silently overwrite "
                    "something" % e.strerror)
        return None                # some other error: the flag itself is fine
    # A SUCCESS here would mean it renamed something that does not exist.
    return "the no-clobber rename probe behaved impossibly on this system"


# NOT `errno.ENOTSUP` AT MODULE SCOPE. It is absent on some interpreters, and
# an AttributeError raised while REPORTING that a syscall is unavailable is
# the report failing in the same breath as the thing it reports (a
# GraalPy probe).
_ENOTSUP = getattr(errno, "ENOTSUP", getattr(errno, "EOPNOTSUPP", 95))


def renameat2(olddirfd, old, newdirfd, new, flags):
    """The full two-directory form. Raises OSError on failure.

    TWO DIRECTORY DESCRIPTORS, which is the whole point of taking it out of
    configs: that wrapper passes ONE dfd for both sides, so it can rename
    WITHIN a directory and cannot express "move this member from the directory
    I pinned into the one I created" — which is the only move a transaction
    that has capabilities on both ends should ever make."""
    if _RENAMEAT2 is None:
        raise OSError(_ENOTSUP, "renameat2 unavailable")
    # CLEARED BEFORE THE CALL (item 9). errno is a THREAD-LOCAL
    # LEFTOVER, not a return value: a call that fails without setting it hands
    # back whatever the last failing libc call in this thread left behind. So
    # a genuinely unsupported renameat2 came back as FileNotFoundError or
    # FileExistsError depending on the seed — and the owner turns those into a
    # false GONE ("something else removed this file") or a destination
    # collision that never happened. My zero-errno cure made the RARE case
    # honest and left the common one reading a stale value underneath it.
    ctypes.set_errno(0)
    if _RENAMEAT2(olddirfd, os.fsencode(old), newdirfd, os.fsencode(new),
                  flags) != 0:
        e = ctypes.get_errno()
        if not e:
            # A NONZERO RETURN WITH errno 0 IS A FAILURE WE CANNOT NAME, and
            # it must never read as success. Measured on GraalPy: a NOREPLACE
            # collision yields errno 0, so OSError(0, "Success") came back —
            # a caller checking `isinstance(err, FileExistsError)` sees
            # neither an error it recognises nor a None, and the kernel had
            # in fact refused and preserved the bytes. The interpreter's
            # errno plumbing is not part of this module's contract; what IS
            # part of it is never reporting an unrecognisable failure as
            # anything other than a failure.
            raise OSError(_ENOTSUP,
                          "the rename failed and this interpreter did not "
                          "report why (errno was not set), so no-clobber "
                          "cannot be confirmed")
        raise OSError(e, os.strerror(e))


def rename_noreplace(olddirfd, old, newdirfd, new):
    """Move a member without ever clobbering. -> None on success, else the
    OSError.

    THE ERROR IS RETURNED, NOT A BOOL. EEXIST ("somebody holds that name") and
    EXDEV ("different filesystems") and ENOTSUP ("this kernel cannot promise
    no-clobber") are three different situations with three different correct
    responses, and a bare False collapses them into one — the cannot-tell
    fail-open this codebase keeps curing."""
    try:
        renameat2(olddirfd, old, newdirfd, new, RENAME_NOREPLACE)
    except OSError as e:
        return e
    return None
