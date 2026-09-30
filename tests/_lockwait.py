"""Which thread a file lock is holding back, observed instead of timed.

A concurrency arm asks whether a second writer waits for the first one's
lock. Answering it by time is two mistakes at once:

  * "still alive after 50 ms" passes on a broken lock whenever the host is
    slow enough that the unheld writer has not finished yet;
  * "finished within 2 s" fails on a correct lock whenever the host is slow
    enough that the released writer has not finished yet.

A loaded build host makes both happen, and a sliced suite is a loaded host.

`observed()` wraps `fcntl.flock` for the arm's duration. A blocking take
that finds the lock held marks the calling thread as waiting on that lock
file before it blocks, and the take clears the mark once it succeeds. `wait_blocked(thread)` then answers from
what the thread did: the path of the lock it is waiting on, or None once it
has finished without ever being held back.

HANG_S is a HANG bound, never a speed claim. Every wait in these arms ends on
an event the code under test sets; only a deadlock waits HANG_S out.
"""
import contextlib
import fcntl
import os
import threading
import time
from unittest import mock

HANG_S = 60.0


class LockWaits:

    def __init__(self, real):
        self._real = real
        self._cond = threading.Condition()
        self.waiting = {}      # thread ident -> lock path it waits on now
        self.refused = {}      # lock path -> non-blocking takes refused

    @staticmethod
    def _path(fd):
        fd = fd if isinstance(fd, int) else fd.fileno()
        try:
            return os.readlink("/proc/self/fd/%d" % fd)
        except OSError:
            return "fd:%d" % fd

    def _mark(self, fd):
        path = self._path(fd)
        with self._cond:
            self.waiting[threading.get_ident()] = path
            self._cond.notify_all()

    def _refuse(self, fd):
        path = self._path(fd)
        with self._cond:
            self.refused[path] = self.refused.get(path, 0) + 1
            self._cond.notify_all()

    def _clear(self):
        with self._cond:
            self.waiting.pop(threading.get_ident(), None)
            self._cond.notify_all()

    def flock(self, fd, op):
        # A non-blocking take is not a wait: a refused one may be followed by
        # a retry or by going on without the lock (a fail-open door), and
        # only the second is a broken serialization. So only a blocking take
        # that finds the lock held marks its thread; a refused non-blocking
        # take is counted against its lock, for a polling door's arm.
        if not op & (fcntl.LOCK_EX | fcntl.LOCK_SH):
            return self._real(fd, op)
        if op & fcntl.LOCK_NB:
            try:
                return self._real(fd, op)
            except BlockingIOError:
                self._refuse(fd)
                raise
        try:
            return self._real(fd, op | fcntl.LOCK_NB)
        except BlockingIOError:
            self._mark(fd)
        try:
            return self._real(fd, op)
        finally:
            self._clear()

    def wait_blocked(self, thread, timeout=HANG_S):
        """The lock path `thread` is waiting on, once it waits; None if it
        finishes first (it was never held back) or `timeout` passes."""
        # A thread's end notifies nothing, so the wait re-reads is_alive on
        # a short tick; the tick bounds no assertion.
        deadline = time.monotonic() + timeout
        with self._cond:
            while thread.ident not in self.waiting and thread.is_alive():
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._cond.wait(min(left, 0.01))
            return self.waiting.get(thread.ident)


    def wait_refused(self, path, timeout=HANG_S):
        """True once a non-blocking take of `path` has been refused: a
        polling door has met the lock held."""
        path = os.path.realpath(path)
        with self._cond:
            return self._cond.wait_for(lambda: self.refused.get(path),
                                       timeout)


@contextlib.contextmanager
def observed():
    """Wrap `fcntl.flock` for the block; yields the LockWaits."""
    waits = LockWaits(fcntl.flock)
    with mock.patch.object(fcntl, "flock", waits.flock):
        yield waits
