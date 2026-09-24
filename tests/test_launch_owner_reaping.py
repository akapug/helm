#!/usr/bin/env python3
"""The seat wrapper reaps the children it adopts, not only the one it launched.

THE WRAPPER IS A SUBREAPER, so every process the harness orphans reparents to
it and becomes ITS child. The steady-state wait loop waited on the direct
harness alone, and the reaper ran only on the drain paths — so for the whole
lifetime of a healthy seat, every orphan stayed a zombie in the wrapper's own
table. Measured live at hundreds per wrapper on the longest-running seats.

EVERY ARM RUNS INSIDE ITS OWN FORK, and that is not tidiness. The reaper calls
waitpid(-1), which takes ANY child of the calling process — run in the test
process it would consume the exit status of any subprocess another test in the
same suite happened to have in flight. A fork gives these arms a process whose
only children are the ones they made themselves.
"""
import os
import signal
import sys
import unittest

from helm import seat_launch_owner as owner


def _in_a_fork(body):
    """Run body() in a child; return its exit code. 0 means every assert held.

    The child reports by EXIT CODE rather than by exception, because an
    assertion raised in a forked child would otherwise unwind into a second
    copy of the test runner and report a phantom pass in the parent's name.
    """
    pid = os.fork()
    if pid == 0:                                   # pragma: no cover - child
        code = 1
        try:
            body()
            code = 0
        except BaseException:
            import traceback
            traceback.print_exc()
        finally:
            os._exit(code)
    return os.WEXITSTATUS(os.waitpid(pid, 0)[1])


def _exiting_child(code=0, sleep=0.0):
    """Fork a child that exits with `code` after `sleep` seconds. -> pid"""
    pid = os.fork()
    if pid == 0:                                   # pragma: no cover - child
        if sleep:
            import time
            time.sleep(sleep)
        os._exit(code)
    return pid


class TheForkHarnessCanActuallyFail(unittest.TestCase):
    """The control for every arm in this file, and it is not a formality.

    Each arm below reports through _in_a_fork, so a harness that returned 0
    unconditionally — a swallowed exception, a status decoded wrong, a child
    that never ran the body — would make every one of them green while
    measuring nothing. That is not hypothetical here: the first version
    returned the RAW wait status where the arms compared against 0, and this
    file's whole point is a reaper that can eat the status it is asked about.
    """

    def test_a_body_that_raises_reports_NONZERO(self):
        def boom():
            raise AssertionError("the harness must surface this")
        self.assertNotEqual(_in_a_fork(boom), 0,
                            "the fork harness cannot report a failure, so "
                            "every arm in this file is vacuous")

    def test_a_body_that_holds_reports_ZERO(self):
        # noqa: VACUOUS_ASSERTION — the two arms of this class ARE each
        # other's controls, on one observable: the arm above proves the
        # harness can report failure, this one proves it does not report
        # failure unconditionally. Neither is meaningful alone and adding a
        # third positive here would restate the one directly above it.
        self.assertEqual(_in_a_fork(lambda: None), 0)  # noqa: VACUOUS_ASSERTION — paired control, see above


class TheReaperReportsTheOwnedChildRatherThanEatingIt(unittest.TestCase):
    """waitpid(-1) takes ANY child, and the harness is one of them."""

    def test_it_returns_the_owned_status_and_still_drains_the_rest(self):
        """THE HAZARD THAT MAKES THIS CALLABLE FROM THE WAIT LOOP. A reaper
        called beside the loop that waits on the harness can consume the one
        status the wrapper exists to reproduce, and the next waitpid(pid) then
        raises with the exit code already gone. Returning it makes the race a
        normal outcome; the remaining orphans are drained in the same pass,
        because leaving them is the whole defect."""
        def body():
            import time
            owned = _exiting_child(code=7)
            orphan = _exiting_child(code=0)
            time.sleep(0.2)
            status = owner._reap_adopted(owned=owned)
            assert status is not None, "the owned status was eaten, not returned"
            assert os.WIFEXITED(status), status
            assert os.WEXITSTATUS(status) == 7, os.WEXITSTATUS(status)
            for pid, label in ((owned, "owned"), (orphan, "orphan")):
                try:
                    os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    continue
                raise AssertionError("%s was left unreaped" % label)
        self.assertEqual(_in_a_fork(body), 0)

    def test_it_answers_None_when_the_owned_child_is_still_running(self):
        """THE CONTROL, and the arm above is meaningless without it: a reaper
        that returned a status unconditionally would end the wait loop on its
        first pass and report a fabricated exit for a live harness."""
        def body():
            import time
            owned = _exiting_child(code=0, sleep=5)
            orphan = _exiting_child(code=0)
            time.sleep(0.2)
            status = owner._reap_adopted(owned=owned)
            assert status is None, "reported an exit for a RUNNING child: %r" % status
            try:
                os.waitpid(orphan, os.WNOHANG)
            except ChildProcessError:
                pass
            else:
                raise AssertionError("the orphan was left unreaped")
            os.kill(owned, signal.SIGKILL)
            os.waitpid(owned, 0)
        self.assertEqual(_in_a_fork(body), 0)


class TheDrainIsBOUNDED(unittest.TestCase):
    """A waitpid that never says "nothing left" must not become a hang.

    THIS IS THE ARM FOR A DEFECT I SHIPPED. Moving the reaper into the wait
    loop put an UNBOUNDED drain into a path that re-enters every few
    milliseconds, and the drain's only exits were ECHILD and a zero pid. The
    existing wrapper fixtures mock os.waitpid with a constant
    `return_value=(654, status)` — correct for every caller the function had
    before, and an infinite loop for the new one. It hung a whole-suite gate
    for 37 minutes until SIGTERM, and the receipt read UNKNOWN rather than
    FAILED, which is a different animal and easy to misread as flaky.

    These arms need NO fork: the hazard is arithmetic on a mocked syscall, and
    a real waitpid can never reproduce it.
    """

    def test_a_waitpid_that_always_answers_terminates_anyway(self):
        import unittest.mock as mock
        with mock.patch.object(owner.os, "waitpid", return_value=(654, 0)):
            # NO TIMEOUT AND NO THREAD: if the bound is gone this call never
            # returns and the arm hangs, which is the same signal the gate
            # gave and is impossible to mistake for a pass.
            status = owner._reap_adopted(owned=654)
        self.assertEqual(status, 0)

    def test_the_bound_is_not_so_small_it_starves_a_real_backlog(self):
        """The other side: a cap of 1 would 'pass' the arm above while taking
        _WAIT_POLL per zombie, so a seat holding hundreds would drain over
        seconds instead of one pass. Pinning the order of magnitude keeps the
        cure honest without pinning an exact number."""
        self.assertGreaterEqual(owner._REAP_PER_PASS, 16)

    def test_a_real_waitpid_still_drains_everything_in_one_pass(self):
        """CONTROL, and the reason the cap is not a behaviour change: under a
        REAL waitpid the drain still empties the table, because the kernel
        answers 'nothing left' long before the cap."""
        def body():
            import time
            kids = [_exiting_child(code=0) for _ in range(8)]
            time.sleep(0.25)
            owner._reap_adopted()
            for pid in kids:
                try:
                    os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    continue
                raise AssertionError("pid %d survived a single pass" % pid)
        self.assertEqual(_in_a_fork(body), 0)


class TheSteadyStateLoopReapsWhatItAdopts(unittest.TestCase):
    """The defect itself, asserted on the observable rather than on the source."""

    def test_an_orphan_is_reaped_WHILE_the_harness_is_still_running(self):
        """THE ARM THAT FAILS ON THE OLD CODE. The loop is entered with an
        orphan already exited and a harness that lives another moment. Before
        the cure the loop only ever waited on the harness, so the orphan was
        still waitable when the loop returned; now it is gone, and the harness
        status is reported unchanged. Both halves are asserted: reaping the
        orphan must not cost the exit code the wrapper exists to reproduce."""
        def body():
            import time
            harness = _exiting_child(code=3, sleep=0.35)
            orphan = _exiting_child(code=0)
            time.sleep(0.1)
            status = owner._wait_owned_child(harness, os.getpgrp(),
                                             lambda: False)
            assert os.WIFEXITED(status), status
            assert os.WEXITSTATUS(status) == 3, os.WEXITSTATUS(status)
            try:
                os.waitpid(orphan, os.WNOHANG)
            except ChildProcessError:
                return
            raise AssertionError(
                "the orphan survived the wait loop — the steady state still "
                "reaps only the direct child")
        self.assertEqual(_in_a_fork(body), 0)

    def test_a_lone_harness_still_reports_its_status(self):
        """The unconditional positive: with nothing to adopt, the loop must
        behave exactly as it did, or the arm above is satisfied by a loop that
        broke the ordinary path to win the orphan one."""
        def body():
            harness = _exiting_child(code=5, sleep=0.1)
            status = owner._wait_owned_child(harness, os.getpgrp(),
                                             lambda: False)
            assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 5, status
        self.assertEqual(_in_a_fork(body), 0)


if __name__ == "__main__":                          # pragma: no cover
    unittest.main()
