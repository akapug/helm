"""The seats lock door, split out of seats_common.py (task/2520).

`_flocked` flocks a stable sibling lock file and FAILS CLOSED: a lock that
cannot be opened or taken raises `LockUnavailable`, naming the lock and why.
seats_common imports both back, so every caller keeps its spelling.
"""


class LockUnavailable(OSError):
    """A seats lock that could not be taken: which lock, and why. An OSError,
    so every door that already refuses on a filesystem fault refuses on it."""

    def __init__(self, path, why):
        super().__init__("helm: the lock %s is unavailable (%s) — refusing to "
                         "write without it" % (path, why))
        self.path, self.why = path, why


class _flocked:
    """flock a STABLE sibling lock file (never an atomic-replaced file).

    IT FAILS CLOSED (task/2520). A lock that cannot be opened or taken raises
    `LockUnavailable`, naming the lock and why, and the body never runs: it
    used to set `.f` to None and run the body unlocked, so eleven roster
    writers and every cursor lock wrote with no lock held. Three forms do not
    raise, and each is spelled at its call site:
      * `blocking=False` — hot hook evidence and the claims poll: contention
        is UNKNOWN, `.f` is None, and the caller reads `.f` and skips or
        refuses; it never consumes the PostToolUse/Stop budget. The CLAIMS
        lock is only ever taken this way (`_claim_flocked` polls it against
        CLAIM_LOCK_WAIT_S and its callers refuse). It never makes a missing
        parent directory: that is the caller's call;
      * `check=True` — the caller reads `.f` and refuses when it is None,
        with its own sentence; it never writes unlocked;
      * `unlocked_ok=True` — a site that must proceed unlocked, and says why
        in a comment beside the call.
    tests/test_lock_fails_closed.py holds the census of every call site."""

    def __init__(self, path, blocking=True, check=False, unlocked_ok=False):
        self.path, self.blocking, self.f = path, blocking, None
        self.raises = blocking and not check and not unlocked_ok

    def __enter__(self):
        try:
            import fcntl
            import os
            # A MISSING PARENT IS NOT A FAULT: the lock is a stable sibling in
            # helm's own state directory, which a fresh host has not made yet
            # and every writer makes anyway. Only a lock that still cannot be
            # opened is refused.
            #
            # A NON-BLOCKING TAKE MAKES NO DIRECTORY. Its caller reads `.f`
            # and owns what a missing directory means: `_claim_flocked` makes
            # one only for `create_dir=True`, and its strict doors refuse a
            # HELM_CHAT_DIR that points at a deleted tree by name (task/3001).
            # Made here, that tree came back empty and a claims rebind
            # reported "no matching live lease" from a ledger that was not
            # the real one.
            parent = os.path.dirname(self.path)
            if self.blocking and parent and not os.path.isdir(parent):
                os.makedirs(parent, mode=0o700, exist_ok=True)
            self.f = open(self.path, "a")
            flags = fcntl.LOCK_EX | (0 if self.blocking else fcntl.LOCK_NB)
            from . import hooklatency
            hooklatency.flock(self.f.fileno(), flags, "lock-seats",
                              fail_open=not self.raises)
        except OSError as exc:
            if self.f is not None:
                self.f.close()
            self.f = None
            if self.raises:
                raise LockUnavailable(self.path, exc.strerror or str(exc)) from exc
        except BaseException:
            # A telemetry END may cancel after acquisition, before __enter__
            # returns. No body owns cleanup yet; close before propagating.
            if self.f is not None:
                self.f.close()
                self.f = None
            raise
        return self

    def __exit__(self, *exc):
        if self.f is not None:
            try:
                import fcntl
                fcntl.flock(self.f.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            self.f.close()
        return False
