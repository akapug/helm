"""The guard identity that is deliberately not a queue slot."""
import io
import os
import tempfile
import sys
import subprocess
import unittest
from unittest import mock

from helm import gate, gatechild
from tests._gate_supervisor import require_supervisor


class GuardIdentityTest(unittest.TestCase):
    """A focused run gets containment WITHOUT acquiring a FIFO slot.

    Measured 2026-08-26: gate.py's `suite = not argv and not focus` means only
    whole-suite runs reach the guarded path, so focused runs -- the entire
    cure-round regime -- execute test code with no cgroup. Handing them a real
    position would be worse than no guard: the FIFO would believe a slot is
    held that nobody acquired.
    """

    def setUp(self):
        """THE CONTROL FOR EVERY ABSENCE ASSERTION BELOW.

        A function returning {} would satisfy "no seq", "no holder" and
        "not queued" perfectly. The positive half must hold first: the
        position exists, and it carries the two fields containment needs.
        """
        self.position = gate._guard_identity()
        self.assertIsInstance(self.position, dict)
        self.assertIn("id", self.position)
        self.assertIn("launcher_start", self.position)
        self.assertTrue(self.position["id"])

    def test_the_token_satisfies_the_cgroup_contract(self):
        """_cgroup_path requires lowercase hex and reports a bad token as an
        ENVIRONMENT fault, so a malformed one is expensive to diagnose."""
        identity = gate._guard_identity()
        token = identity["id"]
        self.assertEqual(32, len(token))
        self.assertEqual(token, token.lower())
        self.assertTrue(all(ch in "0123456789abcdef" for ch in token))
        self.assertIsNotNone(gatechild._cgroup_path(token))

    def test_it_grants_no_queue_capability(self):
        """UNFORGEABLE BY ABSENCE OF THE OBJECT, not absence of keys.

        A position-shaped value with the FIFO fields omitted is still
        structurally acceptable to any consumer that needs only `id` -- right
        about the shape, wrong about the authority. This identity is a
        different type entirely: it carries the two containment fields and
        nothing a queue consumer could mistake for a slot.
        """
        identity = gate._guard_identity()
        self.assertEqual({"id", "launcher_start"}, set(identity))

    def test_it_carries_no_queue_fields(self):  # noqa: VACUOUS_ASSERTION — setUp is this class's unconditional positive control on the same observable: it asserts the identity exists and carries a non-empty id plus launcher_start, so a function returning {} fails there before any absence assertion could pass for it
        """FIFO consumers are gated on `position is not None`, so an
        unqueued run cannot reach them: there is no object to pass."""
        identity = gate._guard_identity()
        for field in ("seq", "holder", "_legacy", "queued"):
            self.assertNotIn(field, identity)

    def test_two_positions_do_not_share_a_cgroup(self):  # noqa: VACUOUS_ASSERTION — setUp is this class's unconditional positive control on the same observable: it asserts the identity exists and carries a non-empty id plus launcher_start, so a function returning {} fails there before any absence assertion could pass for it
        """Concurrent focused runs must not land in one another's cgroup."""
        first = gate._guard_identity()["id"]
        second = gate._guard_identity()["id"]
        self.assertNotEqual(first, second)

    def test_the_starttime_is_this_process(self):  # noqa: VACUOUS_ASSERTION — setUp is this class's unconditional positive control on the same observable: it asserts the identity exists and carries a non-empty id plus launcher_start, so a function returning {} fails there before any absence assertion could pass for it
        """The guard arms parent-death against it; a wrong value makes the
        guard refuse, which is how it fails closed rather than open."""
        from helm import seats
        position = gate._guard_identity()
        self.assertEqual(seats._get_pid_starttime(os.getpid()),
                         gate._guard_identity()["launcher_start"])


class UnqueuedGuardedRunTest(unittest.TestCase):
    """A run with NO FIFO slot is still guarded, and touches no queue state.

    This is the arm the switch owed. Routing focus and custom commands
    through _queued_process with position=None is the whole containment fix
    for the local and custom paths; a suite that stays green proves the
    routing did not break, never that the unqueued path works.
    """

    def _cmd(self, script):
        return [sys.executable, "-c", script]

    def test_an_unqueued_run_lands_in_its_own_cgroup(self):  # noqa: VACUOUS_ASSERTION — the only absence here is assertIsNone(err), and three unconditional positives precede the claim: the child RAN (marker exists), rc is 0, and the cgroup string must CONTAIN this run's own identity; mutation-verified by bypassing the guard
        """THE POSITIVE CONTROL AND THE POINT: guarded means a cgroup."""
        from helm import gate
        identity = gate._guard_identity()
        require_supervisor()
        with tempfile.TemporaryDirectory() as work:
            marker = os.path.join(work, "cgroup")
            script = ("import os\n"
                      "open(%r, 'w').write("
                      "open('/proc/self/cgroup').read())\n" % marker)
            stdout, stderr, rc, err = gate._queued_process(
                os.getcwd(), self._cmd(script), None, 60,
                identity=identity, env=os.environ.copy())
            # Positive control: the child RAN and wrote its marker, so the
            # cgroup assertion below is about a real observation.
            self.assertIsNone(err, "unqueued guarded run failed: %s" % err)
            self.assertTrue(os.path.exists(marker),
                            "the child never ran; arm is vacuous")
            self.assertEqual(0, rc)
            with open(marker, encoding="utf-8") as fh:
                where = fh.read().strip()
        self.assertIn("helm-gate-" + identity["id"], where,
                      "an unqueued run must land in ITS OWN cgroup, not the "
                      "caller's: %s" % where)

    def test_an_unqueued_run_binds_no_queue_state(self):  # noqa: VACUOUS_ASSERTION — assertIsNone(err) is paired with assertEqual(0, rc) from the same run, the unconditional positive that the guarded child ran and exited 0 (a refused or failed guard returns rc None); the recorder's "probe" must-hit above proves the empty call list can go red
        """It holds no slot, so it must not bind, renew or announce one."""
        from helm import gate, seats
        calls = []
        identity = gate._guard_identity()
        # POSITIVE CONTROL ON THE RECORDER ITSELF: prove the patched hooks
        # DO fire when something calls them, or "zero calls" is satisfied by
        # a patch that never took effect.
        with mock.patch.object(seats, "gate_queue_renew",
                               lambda *a, **k: calls.append("probe") or
                               (True, None)):
            seats.gate_queue_renew(os.getcwd(), "x", 1)
        self.assertEqual(["probe"], calls,
                         "the call recorder does not record; arm is vacuous")
        calls.clear()
        # EVERY FIFO-OWNED DOOR, not the two that come to mind. bind and
        # renew are the obvious pair; _gate_post ANNOUNCES a GATE START with
        # a seq and holder an unqueued run does not have, and the legacy lock
        # is queue state too. An arm that forbids two of four doors reports
        # "touched no queue state" while two doors stand open.
        with mock.patch.object(seats, "gate_queue_bind_child",
                               lambda *a, **k: calls.append("bind") or
                               (True, None)), \
             mock.patch.object(seats, "gate_queue_renew",
                               lambda *a, **k: calls.append("renew") or
                               (True, None)), \
             mock.patch.object(gate, "_gate_post",
                               lambda *a, **k: calls.append("post")), \
             mock.patch.object(seats, "gate_queue_validate_binding",
                               lambda *a, **k: calls.append("validate") or
                               (True, None)):
            # THE CHILD MUST OUTLIVE _GATE_RENEW_S OR THIS ARM IS A NO-OP.
            # Renewal lives in the `except subprocess.TimeoutExpired` branch,
            # so a child that exits immediately returns from communicate() on
            # the first call and the loop NEVER REACHES the code under test.
            # An earlier version used `pass` and proved nothing about the
            # branch it was written for.
            with mock.patch.object(gate, "_GATE_RENEW_S", 1):
                require_supervisor()
                _out, _err_text, rc, err = gate._queued_process(
                    os.getcwd(),
                    self._cmd("import time; time.sleep(3)"), None, 30,
                    identity=identity, env=os.environ.copy())
        # THE RESULT IS NOT IGNORABLE. "No FIFO calls" is trivially true when
        # the guard failed before the supervisor and NOTHING RAN -- measured
        # on a host whose scope lacks write access. An arm that passes when
        # containment did not happen is worse than no arm: it reports the
        # property strongest exactly where it is absent. `require_supervisor`
        # answered the host question before the run, so an error here is a
        # defect and never a host fact.
        self.assertIsNone(err, "unqueued guarded run failed: %s" % err)
        self.assertEqual(0, rc, "the guarded child did not exit cleanly")
        self.assertEqual([], calls,
                         "an unqueued run touched FIFO state: %s" % calls)


class _IsolatedGateHome(unittest.TestCase):
    """Any arm that drives gate.run MUST NOT write to the real store.

    gate.run MINTS RECEIPTS. An arm that calls it against the live HELM_HOME
    writes rows into the ledger the fleet reads -- receipts labelled after a
    test, in the store land authority is derived from. That these arms could
    only be run safely by setting HELM_HOME by hand is the tell: the arm was
    exposed, and mocking nothing dangerous had felt like safety.

    Mocking the writer is not isolation when the side effect IS the feature.
    """

    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(
            os.environ, {"HELM_HOME": self._home.name})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(self._home.cleanup)
        # ARM THE ISOLATION ITSELF. An isolation that silently does not take
        # effect is the same class as a patch whose anchor never matched: the
        # arm runs, looks careful, and writes to the real store anyway. Prove
        # the receipt path resolves INSIDE the temp home before any gate.run
        # is allowed to mint.
        from helm import gate as _gate
        where = os.path.realpath(_gate.receipts_path())
        self.assertTrue(
            where.startswith(os.path.realpath(self._home.name)),
            "HELM_HOME isolation did not take effect: receipts would land in "
            "%s" % where)


class SwitchIsWiredTest(_IsolatedGateHome):
    """Arms that fail if the SWITCH is reverted, not just if the helper is.

    Both earlier arms called _queued_process DIRECTLY, so reverting
    gate.run's focus/custom callsite to a bare subprocess.run left them
    green -- measured. They tested the function and not the wiring — the
    same class one level up from the per-case gaps this file closes.
    """

    def _run_custom(self, script, repo=None):
        """A custom argv still goes through the gate guard, so it needs the
        guard's cgroup supervisor exactly as a whole-suite run does."""
        from helm import gate
        require_supervisor()
        return gate.run(repo=repo or os.getcwd(),
                        argv=[sys.executable, "-c", script],
                        label="switch-arm", timeout=60)

    def test_a_custom_command_goes_through_the_guard(self):  # noqa: VACUOUS_ASSERTION — the marker file existing IS the unconditional positive control: the child ran and wrote its own cgroup, and the assertion is that the string CONTAINS helm-gate
        """A custom argv run must land in a helm-gate cgroup.

        gateequiv drives gateshard as custom argv, which is exactly the
        canonical-diagnostic path measured leaking a child two seconds after
        return. If this arm is green with the switch reverted, the switch is
        untested.
        """
        with tempfile.TemporaryDirectory() as work:
            marker = os.path.join(work, "cgroup")
            self._run_custom(
                "open(%r,'w').write(open('/proc/self/cgroup').read())"
                % marker)
            self.assertTrue(os.path.exists(marker),
                            "the custom command never ran; arm is vacuous")
            with open(marker, encoding="utf-8") as fh:
                where = fh.read().strip()
        self.assertIn("helm-gate-", where,
                      "a custom command ran OUTSIDE a gate cgroup: %s" % where)

    def test_a_custom_command_inherits_its_environment(self):
        """Custom INHERITS -- a behaviour that existed only as the absence of
        an argument at the old callsite, and which routing through the guard
        could have silently changed."""
        with tempfile.TemporaryDirectory() as work:
            marker = os.path.join(work, "env")
            with mock.patch.dict(os.environ,
                                 {"HELM_SWITCH_ARM_MARKER": "inherited"}):
                self._run_custom(
                    "import os; open(%r,'w').write("
                    "os.environ.get('HELM_SWITCH_ARM_MARKER','ABSENT'))"
                    % marker)
            self.assertTrue(os.path.exists(marker),
                            "the custom command never ran; arm is vacuous")
            with open(marker, encoding="utf-8") as fh:
                seen = fh.read().strip()
        self.assertEqual("inherited", seen,
                         "a custom command lost its inherited environment")


class FocusEnvIsSuiteEnvTest(_IsolatedGateHome):
    """Focus gets _suite_env(); custom does NOT. Both directions.

    The split is not inheritance -- _suite_env() copies os.environ and ADDS
    markers -- so an inherit-a-marker arm cannot discriminate. The
    discriminator is the ADDED keys: HELM_GATE_SUITE_CAP, NO_COLOR,
    PYTHON_COLORS.
    """

    def test_a_custom_command_does_not_receive_the_suite_markers(self):
        """The route-level half of the coverage question.

        A custom argv run must NOT carry _suite_env()'s additions, or the
        `env=_suite_env() if focus else os.environ.copy()` split at the
        switch is not doing what it says.
        """
        from helm import gate
        require_supervisor()
        with tempfile.TemporaryDirectory() as work:
            marker = os.path.join(work, "env")
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("HELM_GATE_SUITE_CAP", None)
                gate.run(repo=os.getcwd(),
                         argv=[sys.executable, "-c",
                               "import os; open(%r,'w').write("
                               "os.environ.get('HELM_GATE_SUITE_CAP','ABSENT'))"
                               % marker],
                         label="env-arm", timeout=60)
            self.assertTrue(os.path.exists(marker),
                            "the custom command never ran; arm is vacuous")
            with open(marker, encoding="utf-8") as fh:
                seen = fh.read().strip()
        self.assertEqual("ABSENT", seen,
                         "a custom command received the SUITE's env markers")

    def test_suite_env_adds_the_markers_it_is_asserted_by(self):
        """POSITIVE CONTROL on the discriminator itself.

        If _suite_env() stopped setting HELM_GATE_SUITE_CAP, the arm above
        would pass for the wrong reason -- ABSENT everywhere.
        """
        from helm import gate
        env = gate._suite_env()
        # A KEY VIEW, NEVER THE MAPPING: _suite_env() opens with
        # dict(os.environ), so assertIn against it renders every ambient
        # value when the marker is missing (task/2370).
        self.assertIn("HELM_GATE_SUITE_CAP", tuple(env))
        self.assertEqual("0", env.get("PYTHON_COLORS"))
        self.assertEqual("1", env.get("NO_COLOR"))


class TheSwitchIsReachedByThisSuiteTest(unittest.TestCase):
    """A WHOLE-OBJECT coverage check, not another per-case arm.

    Per-case arms each close one gap — the renewal branch unreached, the
    switch untested, the env split undiscriminating, the home unisolated —
    and each fix is correct while none of them can END the
    sequence, because a per-case handler is always missing the case outside
    the set you enumerated.

    One instrument generalises where per-arm mutations did not: make the
    callsite ITSELF fail, run the suite, and see whether anything notices. That answers "does this suite reach the wiring" in one shot for
    every future case, rather than once per case that comes to mind.

    So this arm asserts the property directly: if the guarded switch is
    reachable and nothing exercises it, THIS test fails and names it.
    """

    def test_something_in_this_suite_drives_the_guarded_switch(self):
        from helm import gate
        # THE INNER ARMS MINT THROUGH THE GUARD, so where they would skip this
        # arm skips first: a probe that cannot reach the switch here says so
        # instead of reporting the switch unreached.
        require_supervisor()
        reached = []
        real = gate._queued_process

        def spy(repo, cmd, position, timeout, identity=None, env=None):
            reached.append(position)
            return real(repo, cmd, position, timeout, identity=identity,
                        env=env)

        suite = unittest.defaultTestLoader.loadTestsFromNames([
            "tests.test_gate_guard_identity.SwitchIsWiredTest",
            "tests.test_gate_guard_identity.FocusEnvIsSuiteEnvTest",
        ])
        with mock.patch.object(gate, "_queued_process", spy):
            inner = unittest.TextTestRunner(
                stream=io.StringIO(), verbosity=0).run(suite)

        # THE INNER RESULT IS NOT DISCARDABLE. "The switch was reached" is
        # satisfied by arms that reached it AND FAILED, so a suite whose
        # every arm is red would still prove coverage. Coverage without
        # outcome is the same half-truth as a green suite without coverage.
        self.assertTrue(inner.wasSuccessful(),
                        "the switch arms reached the wiring but did not "
                        "pass: %s" % [t.id() for t, _ in
                                      inner.failures + inner.errors])
        # A SKIP IS SUCCESS TO wasSuccessful(), and an inner arm that skipped
        # neither reached the switch nor passed. The supervisor was measured
        # as present above, so an inner skip here is a defect.
        self.assertEqual([], [t.id() for t, _ in inner.skipped],
                         "the switch arms skipped where the supervisor is "
                         "present: %s" % inner.skipped)
        self.assertTrue(inner.testsRun,
                        "the inner suite ran nothing; coverage is vacuous")
        self.assertTrue(
            reached,
            "NOTHING in this suite reaches gate.run's guarded switch -- the "
            "arms are testing helpers, not the wiring")
        self.assertIn(
            None, reached,
            "the switch was reached but never with position=None -- the "
            "unqueued path is still unexercised")


class _RecordingPosition(dict):
    """Falsy like None, but RECORDS any lookup. -> position-shaped sentinel

    The unqueued path is guarded by `if position:`, so a FALSY object takes
    every branch None takes. Unlike None it survives a subscript, so an
    UNGUARDED `position["_legacy"]` records itself instead of raising -- which
    is the difference between "no crash, therefore probably fine" and a
    positive record of every key the code reached for.
    """

    def __init__(self):
        super().__init__()
        self.touched = []

    def __bool__(self):
        return False

    def __getitem__(self, key):
        self.touched.append(key)
        return mock.MagicMock()

    def get(self, key, default=None):
        self.touched.append(key)
        return default


class UnqueuedTouchesNoLegacyLockTest(unittest.TestCase):
    """An unqueued run must not reach for _legacy, seq, holder or id.

    Passing None proves only that nothing crashed; a falsy RECORDING sentinel
    proves what was actually asked for. An arm can claim to cover the
    legacy lock while in fact patching gate_queue_validate_binding -- the claim
    and the code disagreeing is the defect class this whole file keeps
    finding.
    """

    def test_the_unqueued_path_reaches_for_no_queue_key(self):  # noqa: VACUOUS_ASSERTION — test_the_sentinel_records_when_something_does_reach in this class is the unconditional positive control on the same observable: it proves the recorder DOES record and is falsy, so an empty touch list cannot come from a dead sentinel; mutation-verified by loosening the guard to `is not None`
        from helm import gate
        sentinel = _RecordingPosition()
        identity = gate._guard_identity()
        require_supervisor()
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(os.environ, {"HELM_HOME": home}):
                _o, _e, rc, err = gate._queued_process(
                    os.getcwd(),
                    [sys.executable, "-c", "pass"], sentinel, 60,
                    identity=identity, env=os.environ.copy())
        # THE RESULT IS NOT IGNORABLE -- the same defect the zero-state arm
        # had. "Reached for no queue key" is trivially true when the guard
        # refused before the supervisor and the child never ran, and that is
        # exactly the host condition under which this arm most needs to be
        # honest. An empty touch list means nothing until something executed.
        self.assertIsNone(err, "unqueued guarded run failed: %s" % err)
        self.assertEqual(0, rc, "the guarded child did not exit cleanly")
        self.assertEqual(
            [], sentinel.touched,
            "an unqueued run reached for queue state: %s" % sentinel.touched)

    def test_the_sentinel_records_when_something_does_reach(self):
        """POSITIVE CONTROL on the recorder: a sentinel that records nothing
        would satisfy the arm above no matter what the code did."""
        sentinel = _RecordingPosition()
        _ = sentinel["_legacy"]
        sentinel.get("seq")
        self.assertEqual(["_legacy", "seq"], sentinel.touched)
        self.assertFalse(sentinel, "the sentinel must be FALSY like None")
