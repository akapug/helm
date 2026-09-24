"""Open flags whose absence must be an explicit decision.

A protection flag silently folded to zero makes the guarded open succeed without
the property its caller claims.  Required flags therefore refuse at the
operation boundary.  O_CLOEXEC is the one deliberate degradation: Python 3
makes every descriptor returned by os.open non-inheritable even when the kernel
constant is unavailable, so omitting the atomic flag preserves the property but
leaves only the fork/exec race between open and Python's fallback fcntl.
"""
import errno
import os


_ENOTSUP = getattr(errno, "ENOTSUP", getattr(errno, "EOPNOTSUPP", 95))


def verified_directory(base):
    """Add O_DIRECTORY when the caller independently verifies directory use."""
    flag = getattr(os, "O_DIRECTORY", None)
    return base if flag is None else base | flag


def filesystem_root(base):
    """Add assertions for the literal filesystem root, whose type is intrinsic."""
    value = verified_directory(base)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    return value if nofollow is None else value | nofollow


def flags(base, *required, cloexec=False):
    """Build open flags, refusing when a required capability is unavailable."""
    value = base
    missing = []
    for name in required:
        flag = getattr(os, name, None)
        if flag is None:
            missing.append(name)
        else:
            value |= flag
    if missing:
        raise OSError(_ENOTSUP,
                      "required open flag%s unavailable: %s" %
                      ("s" if len(missing) != 1 else "", ", ".join(missing)))
    if cloexec:
        # DELIBERATE DEGRADATION, not a protection silently disappearing.
        # Python 3 makes os.open descriptors non-inheritable independently;
        # without O_CLOEXEC only the narrow open-to-fcntl fork/exec race remains.
        value |= getattr(os, "O_CLOEXEC", 0)
    return value
