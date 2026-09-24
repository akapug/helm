"""A refusal must name its own fault, not a neighbouring subsystem."""
import tempfile
import unittest
from unittest import mock

from helm import gatechild
from tests._gate_supervisor import require_supervisor


class CreateCgroupRefusalTest(unittest.TestCase):
    """Caller faults and environment faults must not share a sentence.

    _create_cgroup can fail for two unrelated reasons: the cgroup root is
    unreadable (a property of the box and its scope) or the position token is
    not a usable cgroup name (a property of the call). A single message for
    both points the reader at the wrong subsystem, and an environment-shaped
    message is especially costly because re-running elsewhere reproduces it
    and reads as confirmation.
    """

    def test_a_valid_token_creates_and_the_arm_can_tell(self):  # noqa: VACUOUS_ASSERTION — the only absence is assertIsNone(err), and it is guarded by the unconditional positive that a real cgroup path was returned and removable
        """POSITIVE CONTROL: without it, both refusals below pass for a
        function that refuses everything.

        IT NEEDS A REAL, WRITABLE CGROUP ROOT. Success means the new
        directory carries cgroup.procs, cgroup.events and cgroup.kill, and
        only the kernel's cgroup filesystem creates those, so no fixture root
        can stand in. Where `require_supervisor` measures the root as
        unusable this arm skips; the arm below keeps the refusals controlled
        there."""
        require_supervisor()
        path, err = gatechild._create_cgroup("deadbeefcafe")
        self.assertIsNone(err, "a valid token must not be refused: %s" % err)
        self.assertIsNotNone(path)
        gatechild._remove_cgroup_tree(path)

    def test_a_valid_token_passes_both_named_refusals_on_a_root_it_owns(self):
        """THE SAME CONTROL WITH ITS PRECONDITION OWNED, so it runs on every
        node. The root is a directory this arm made, so neither the caller
        refusal nor the environment refusal may fire for a valid token: the
        call must get past both and reach the interface check, which a plain
        directory then fails. A function that refused everything as the
        caller's fault or the environment's fails here."""
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.object(gatechild, "_cgroup_root",
                                   return_value=root):
                path, err = gatechild._create_cgroup("deadbeefcafe")
        self.assertIsNone(path)
        self.assertIn("missing required interface", err)
        self.assertNotIn("caller", err)
        self.assertNotIn("environment", err)

    def test_a_malformed_token_blames_the_caller(self):
        _path, err = gatechild._create_cgroup("settling")
        self.assertIsNotNone(err)
        self.assertIn("settling", err,
                      "the refusal must quote the token it rejected")
        self.assertIn("caller", err)
        self.assertNotIn("unreadable", err,
                         "a caller fault must not be reported as an "
                         "environment fault")

    def test_an_unreadable_root_blames_the_environment(self):
        with mock.patch.object(gatechild, "_cgroup_root", lambda: None):
            _path, err = gatechild._create_cgroup("deadbeefcafe")
        self.assertIsNotNone(err)
        self.assertIn("unreadable", err)
        self.assertIn("environment", err)

    def test_the_two_faults_do_not_share_a_message(self):
        """THE PROPERTY. Distinct causes, distinct sentences -- otherwise a
        reader cannot tell which subsystem to go and look at."""
        _p1, caller = gatechild._create_cgroup("settling")
        with mock.patch.object(gatechild, "_cgroup_root", lambda: None):
            _p2, environment = gatechild._create_cgroup("deadbeefcafe")
        # Positive controls: BOTH must actually be refusals, or "they differ"
        # is satisfied by one of them being None.
        self.assertTrue(caller, "the caller case did not refuse")
        self.assertTrue(environment, "the environment case did not refuse")
        self.assertNotEqual(caller, environment)


class TheRootIsReadOnceTest(unittest.TestCase):
    """The root is read once, so a second read cannot invent a caller fault.

    _create_cgroup read the root, then called _cgroup_path which read it
    AGAIN. With the first read succeeding and the second not, `root` was
    truthy so the environment branch was skipped, and a VALID lowercase token
    was reported as the caller's malformed one -- this module's own defect
    class, rebuilt inside its cure.
    """

    def test_a_second_root_read_cannot_invent_a_caller_fault(self):  # noqa: VACUOUS_ASSERTION — test_a_genuinely_malformed_token_still_names_the_caller in this module is the unconditional positive control on the same string: it proves the caller phrasing IS produced when the caller is at fault
        from unittest import mock
        from helm import gatechild
        with mock.patch.object(gatechild, "_cgroup_root",
                               side_effect=["/sys/fs/cgroup/valid", None]):
            path, why = gatechild._create_cgroup("abc123")
        self.assertIsNone(path)
        self.assertNotIn("caller, not the environment", why,
                         "a valid token was blamed on the caller because the "
                         "root was read twice: %s" % why)

    def test_the_root_is_read_exactly_once(self):
        """The property, not just this symptom: a second read is a second
        chance to disagree, and any disagreement misattributes."""
        from unittest import mock
        from helm import gatechild
        with mock.patch.object(gatechild, "_cgroup_root",
                               return_value="") as root:
            gatechild._create_cgroup("abc123")
        self.assertEqual(1, root.call_count,
                         "the root was read %d times" % root.call_count)

    def test_a_genuinely_malformed_token_still_names_the_caller(self):
        """MUST-MISS: the fix must not stop blaming the caller when the
        caller IS at fault."""
        from unittest import mock
        from helm import gatechild
        with mock.patch.object(gatechild, "_cgroup_root",
                               return_value="/sys/fs/cgroup/valid"):
            _path, why = gatechild._create_cgroup("NOT-HEX!")
        self.assertIn("caller, not the environment", why)


class CaseIsNotFoldedTest(unittest.TestCase):
    """An uppercase token is REFUSED, never quietly canonicalised.

    Lowercasing aliases two distinct positions onto one cgroup identity:
    "ABC" and "abc" would name the same subtree, so two runs could share a
    containment boundary and each would kill the other's processes when it
    cleaned up. The contract said lowercase-only and the code said otherwise.
    """

    def test_an_uppercase_token_is_refused(self):
        from unittest import mock
        from helm import gatechild
        self.assertIsNone(gatechild._cgroup_token("ABCDEF"))
        with mock.patch.object(gatechild, "_cgroup_root",
                               return_value="/sys/fs/cgroup/valid"):
            _path, why = gatechild._create_cgroup("ABCDEF")
        self.assertIn("caller, not the environment", why)

    def test_the_lowercase_twin_is_accepted(self):
        """POSITIVE CONTROL: the refusal above is about CASE, not about a
        token this function rejects for some other reason."""
        from helm import gatechild
        self.assertEqual("abcdef", gatechild._cgroup_token("abcdef"))

    def test_case_variants_cannot_collide_on_one_path(self):
        """The property the refusal protects, stated directly."""
        from unittest import mock
        from helm import gatechild
        with mock.patch.object(gatechild, "_cgroup_root",
                               return_value="/sys/fs/cgroup/valid"):
            lower = gatechild._cgroup_path("abcdef")
            upper = gatechild._cgroup_path("ABCDEF")
        self.assertIsNotNone(lower)
        self.assertIsNone(upper,
                          "two case variants resolved to cgroup paths: "
                          "%s and %s" % (lower, upper))


class TheSupervisorReapsWhatItAdoptsTest(unittest.TestCase):
    """A subreaper that never reaps is a promise taken and half paid.

    _set_subreaper arms PR_SET_CHILD_SUBREAPER so _kill_descendants can see the
    whole tree, which reparents every orphaned descendant onto the supervisor.
    Nothing then reaped them: task/1151 measured 22952 zombies across three
    live gates (~2300/min/gate) and one build host's gate carried 10,116. Against
    ulimit -u 745210 that is roughly 3.7h of concurrent gating before fork()
    begins to fail.

    THE SECOND ARM IS THE ONE THAT MATTERS, and it guards the CURE rather than
    the defect. The obvious reaper — a bare os.waitpid(-1, 0) — also reaps the
    TRACKED child, and CPython's Popen._try_wait catches the resulting
    ChildProcessError and substitutes sts=0. A FAILING SUITE THEN REPORTS EXIT
    0: a silent-green gate, strictly worse than the leak and invisible from the
    receipt. Measured directly while building this: a child exiting 3, reaped
    naively, is reported by child.wait() as 0.
    """

    # An intermediate that forks a child and exits at once. The fork outlives
    # its parent, so it REPARENTS to whoever holds subreaper — us — and then
    # exits, which is exactly how a suite's gits become zombies.
    _ORPHANER = ("import os, time\n"
                 "if os.fork() == 0:\n"
                 "    time.sleep(0.4)\n"
                 "    os._exit(0)\n"
                 "os._exit(0)\n")

    def _zombies_parented_to_us(self):
        import os
        n = 0
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open("/proc/%s/stat" % entry) as fh:
                    tail = fh.read().split(") ", 1)[1].split()
            except (OSError, IndexError):
                continue
            if tail[0] == "Z" and int(tail[1]) == os.getpid():
                n += 1
        return n

    def _restore_subreaper(self, prior):
        if not gatechild._set_subreaper(prior):
            self.fail("could not restore PR_SET_CHILD_SUBREAPER after fixture")
        self.assertIs(
            gatechild._subreaper_state(), prior,
            "fixture left the unittest process with a different subreaper state")

    def setUp(self):
        prior = gatechild._subreaper_state()
        if prior is None or not gatechild._set_subreaper():
            self.skipTest("PR_SET_CHILD_SUBREAPER unavailable on this kernel")
        # Process-global, and persistent across the remainder of the whole
        # suite. Without this cleanup the fixture itself becomes the collector
        # for every later orphan: task/1914 measured 2,569 zombie git children
        # under the unittest MAIN process during one ordinary gate.
        self.addCleanup(self._restore_subreaper, prior)

    def test_orphans_adopted_during_the_run_do_not_accumulate(self):
        """THE LEAK IS SHOWN BEFORE IT IS CURED, in this method.

        Asserting only "zombies are back to baseline" passes trivially when the
        fixture never adopted an orphan at all — 0 == 0 reads exactly like a
        working cure. helm's vacuous-assertion rung flagged that, correctly. So
        the arm first proves it can SEE the leak (orphans exit unreaped and the
        count rises), and only then proves the reaper clears it. Measured while
        writing: 8 orphans appear, _drain_adopted takes them to 0.
        """
        import subprocess, sys, time
        # The whole-suite runner may already have one exited descendant waiting
        # when this fixture becomes its subreaper. Drain that unrelated status
        # before taking the baseline: otherwise the cure correctly reaps it and
        # the final count falls below `before` (measured 1 -> 0), which this arm
        # misreports as a leak. The MUST-HIT below still creates and observes its
        # own fresh adopted orphans, so this cannot turn the test into 0 == 0.
        gatechild._drain_adopted()
        before = self._zombies_parented_to_us()

        # MUST-HIT: without reaping, adopted orphans accumulate. If this does
        # not fire, the fixture is not creating the condition under test and
        # everything below is decorative.
        for _ in range(8):
            subprocess.run([sys.executable, "-c", self._ORPHANER])
        time.sleep(0.8)
        leaked = self._zombies_parented_to_us() - before
        self.assertGreater(
            leaked, 0,
            "no orphan was adopted, so this arm cannot observe the leak it "
            "exists to prove is cured — the fixture, not the cure, is broken")
        gatechild._drain_adopted()

        # And now the cure, across a real supervised run.
        tracked = subprocess.Popen(
            [sys.executable, "-c", "import time, sys; time.sleep(1.5); sys.exit(0)"])
        for _ in range(8):
            subprocess.run([sys.executable, "-c", self._ORPHANER])
        gatechild._wait_reaping_adopted(tracked)
        time.sleep(0.2)
        gatechild._drain_adopted()
        # NOT EQUALITY TO `before`, AND THE GATE PROVED WHY. This arm read
        # `assertEqual(now, before)` and went red on a build host with "0 != 1 …
        # never reaped — at 0 zombies", a message refuting its own assertion:
        # there were ZERO zombies left. `before` had been 1 — a zombie that
        # already existed when the test started, which the reaper then cleared.
        # So the arm failed BECAUSE THE CURE WORKED ON SOMETHING IT DID NOT
        # CREATE. `before` is a reading of a shared machine, not a constant the
        # test owns, and a loaded box supplies a non-zero one.
        #
        # The obligation is that adopted orphans do not ACCUMULATE, so the
        # assertion is that the count did not RISE. It keeps every tooth: the
        # must-hit above already proved this fixture can drive the count up, so
        # a reaper that stops reaping still fails here with now > before.
        now = self._zombies_parented_to_us()
        self.assertLessEqual(
            now, before,
            "orphans adopted while the suite ran were never reaped — this is "
            "the leak itself, at %d zombies against a baseline of %d"
            % (now, before))

    def test_a_FAILING_child_still_reports_its_own_exit_code(self):
        """THE SILENT-GREEN GUARD. Swap in the naive reaper and this goes red.

        Nothing else here can catch that regression: the zombie arm above is
        satisfied by a naive waitpid(-1) reaper, which reaps orphans perfectly
        well AND destroys the verdict. Only asserting the tracked child's own
        non-zero code discriminates the two cures.
        """
        import subprocess, sys
        # Start the orphan first, then keep the tracked child alive beyond the
        # orphan's 0.4s lifetime. The old ordering ended the tracked child at
        # 0.3s, leaving a LIVE adopted orphan behind; it exited between tests
        # and became the next case's baseline zombie. A naive blocking reaper
        # still steals the later tracked status, so the silent-green guard keeps
        # its discrimination while the fixture stops leaking across cases.
        subprocess.run([sys.executable, "-c", self._ORPHANER])
        tracked = subprocess.Popen(
            [sys.executable, "-c", "import time, sys; time.sleep(0.6); sys.exit(3)"])
        rc = gatechild._wait_reaping_adopted(tracked)
        self.assertEqual(
            rc, 3,
            "the supervisor reported %r for a suite that exited 3 — a reaper "
            "that steals the tracked child's status turns every failing gate "
            "green" % rc)

    def test_a_PASSING_child_is_not_reported_as_failure(self):  # noqa: VACUOUS_ASSERTION — this IS the positive control for test_a_FAILING_child_still_reports_its_own_exit_code; the pair pins a mapping (3 stays 3, 0 stays 0) and the heuristic cannot see a control that lives in a sibling method.
        """The positive control the arm above needs.

        Asserting only that 3 survives would also pass if the supervisor
        returned the child's code by accident while breaking success. This
        pins the other end so the pair describes the mapping, not one point.
        """
        import subprocess, sys
        tracked = subprocess.Popen([sys.executable, "-c", "raise SystemExit(0)"])
        self.assertEqual(gatechild._wait_reaping_adopted(tracked), 0)

    def test_the_final_drain_does_not_block_on_a_LIVE_orphan(self):
        """A finished gate must not be held open by a stray helper.

        _drain_adopted is WNOHANG for this reason. If it ever blocks, a suite
        that leaves one long-running helper behind hangs the supervisor, which
        is a worse failure than the zombies it exists to clear.
        """
        import subprocess, sys, time
        live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            started = time.monotonic()
            gatechild._drain_adopted()
            elapsed = time.monotonic() - started
            # POSITIVE CONTROL on the same observable: the orphan is still
            # RUNNING afterwards. Timing alone would also be satisfied by a
            # drain that killed or reaped it instantly, which is the opposite
            # of the non-blocking property this arm claims.
            self.assertIsNone(
                live.poll(),
                "the live orphan is gone after the drain — the drain consumed "
                "it rather than declining to wait for it")
            self.assertLess(
                elapsed, 1.0,
                "the drain blocked while a live orphan was still running")
            # POSITIVE CONTROL on that same poll(): show it can report the
            # OTHER value. assertIsNone alone is satisfied by a poll() that is
            # broken, or by a Popen this process cannot observe at all, and
            # those look identical to "still running".
            live.kill()
            self.assertIsNotNone(
                live.wait(),
                "poll/wait cannot report this process in either direction, so "
                "the still-running assertion above proved nothing")
        finally:
            if live.poll() is None:      # the arm kills it as its own control
                live.kill()
                live.wait()


class TheSubreaperFixtureRestoresItsCallerTest(unittest.TestCase):
    """The regression fixture must not mutate the rest of the whole suite."""

    def test_one_completed_case_restores_the_process_global_state(self):
        """Run one real case in a fresh process, then inspect that SAME process.

        Checking this runner would be circular: an earlier broken case may
        already have enabled subreaping. The child starts with its own kernel
        state, runs the fixture lifecycle, and reports whether that lifecycle
        put the state back. Removing addCleanup makes this arm fail.
        """
        import subprocess, sys
        probe = (
            "from helm import gatechild\n"
            "from tests.test_gatechild_refusals import "
            "TheSupervisorReapsWhatItAdoptsTest as Case\n"
            "before = gatechild._subreaper_state()\n"
            "if before is None:\n"
            "    raise SystemExit('PR_GET_CHILD_SUBREAPER unavailable')\n"
            "case = Case('test_a_PASSING_child_is_not_reported_as_failure')\n"
            "result = case.run()\n"
            "after = gatechild._subreaper_state()\n"
            "print(repr(before), repr(after), result.wasSuccessful())\n"
            "raise SystemExit(0 if result.wasSuccessful() and after is before else 1)\n")
        run = subprocess.run(
            [sys.executable, "-c", probe], text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(
            run.returncode, 0,
            "a completed fixture case leaked PR_SET_CHILD_SUBREAPER into its "
            "caller: stdout=%r stderr=%r" % (run.stdout, run.stderr))
        self.assertIn("True", run.stdout,
                      "the nested fixture case did not complete successfully")
