"""tests/_lockwait.py answers "is this thread held back by a lock?" from what
the thread did, and it must say no as readily as yes.

The concurrency arms in tests.test_seats and tests.test_seats_rename use it in
place of "still alive after 50 ms". An observer that called every thread held
back would make each of those arms pass on a broken lock, so the no-answers
are the arms that matter here: a writer whose lock is free, and a fail-open
door whose non-blocking take is refused and goes on without the lock.
"""
import fcntl
import os
import shutil
import tempfile
import threading
import unittest

from tests import _lockwait


class LockWaitsTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-lockwait-")
        self.path = os.path.join(self.tmp, "x.lock")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def take(self, flags, took):
        with open(self.path, "a") as fh:
            try:
                fcntl.flock(fh.fileno(), flags)
            except BlockingIOError:
                took.append(False)
                return
            took.append(True)
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def test_a_writer_behind_a_held_lock_is_seen_waiting_on_it(self):
        took = []
        with open(self.path, "a") as held, _lockwait.observed() as waits:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX)
            writer = threading.Thread(target=self.take,
                                      args=(fcntl.LOCK_EX, took))
            writer.start()
            self.assertEqual(waits.wait_blocked(writer),
                             os.path.realpath(self.path))
            self.assertEqual(took, [])
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
            writer.join(_lockwait.HANG_S)
        self.assertEqual(took, [True])
        self.assertEqual(waits.waiting, {})

    def test_a_writer_whose_lock_is_free_is_never_held_back(self):
        took = []
        with _lockwait.observed() as waits:
            writer = threading.Thread(target=self.take,
                                      args=(fcntl.LOCK_EX, took))
            writer.start()
            self.assertIsNone(waits.wait_blocked(writer))
        self.assertEqual(took, [True])

    def test_a_refused_non_blocking_take_is_not_a_wait(self):
        took = []
        with open(self.path, "a") as held, _lockwait.observed() as waits:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX)
            door = threading.Thread(target=self.take,
                                    args=(fcntl.LOCK_EX | fcntl.LOCK_NB, took))
            door.start()
            self.assertIsNone(waits.wait_blocked(door))
            # ... but it is counted against its lock, for a polling door.
            self.assertTrue(waits.wait_refused(self.path))
        self.assertEqual(took, [False])

    def test_an_uncontended_non_blocking_take_is_never_refused(self):
        took = []
        with _lockwait.observed() as waits:
            door = threading.Thread(target=self.take,
                                    args=(fcntl.LOCK_EX | fcntl.LOCK_NB, took))
            door.start()
            door.join(_lockwait.HANG_S)
            self.assertFalse(waits.wait_refused(self.path, timeout=0))
        self.assertEqual(took, [True])

    def test_the_real_flock_is_back_after_the_block(self):
        real = fcntl.flock
        with _lockwait.observed():
            self.assertIsNot(fcntl.flock, real)
        self.assertIs(fcntl.flock, real)


if __name__ == "__main__":
    unittest.main()
