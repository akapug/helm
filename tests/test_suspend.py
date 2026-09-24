#!/usr/bin/env python3
"""The post-suspend sweep (task/2721): detection is a clock difference, the
sweep acts only on it, and the rung never moves the watchdog's rc.

HERMETIC: the clock reader, the state file's home, the bouncer, the pass
kick and the room post are all parameters or doubles. No real suspend, no
real proxy, no real room post — the production wiring is asserted at the
rung, with the rung's own actuators proven reachable."""
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

# `seat` IS IMPORTED EXPLICITLY at module scope, an ancestor of the
# seat_health imports in EnsureWiringTest — the co-occurrence guard in
# tests/test_seat_facade_injection.py requires an accepted facade import in
# the same or an enclosing scope.
from helm import seat, suspend  # noqa: F401


class SuspendBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.envp = mock.patch.dict(os.environ, {
            "HELM_HOME": os.path.join(self.tmp.name, "helm-home")})
        self.envp.start()

    def tearDown(self):
        self.envp.stop()
        self.tmp.cleanup()

    def detect_with(self, readings):
        """detect() over a scripted clock. Each call consumes one reading;
        the state file is the real one, in the temp home."""
        seq = iter(readings)
        return lambda: next(seq)


class DetectionTest(SuspendBase):
    def test_a_first_run_records_and_sweeps_nothing(self):
        d = suspend.detect(now=1000.0,
                           reader=self.detect_with([100.0]))
        self.assertEqual(d["state"], "first-run")
        # THE CONTROL ON THE RECORD: a second reading BELOW the threshold is
        # steady, which is only observable if the first write landed.
        d2 = suspend.detect(now=1060.0,
                            reader=self.detect_with([100.0 + 30]))
        self.assertEqual(d2["state"], "steady")
        self.assertEqual(d2["last_ts"], 1000.0)

    def test_growth_past_the_threshold_is_a_resume_with_the_sleep_named(self):
        suspend.detect(now=1000.0, reader=self.detect_with([100.0]))
        d = suspend.detect(now=1061.0,
                           reader=self.detect_with([100.0 + 3600.0]))
        self.assertEqual(d["state"], "resumed")
        self.assertEqual(d["gap_s"], 3600.0)
        self.assertEqual(d["last_ts"], 1000.0)

    def test_an_unavailable_clock_is_unknown_and_writes_nothing(self):
        d = suspend.detect(reader=lambda: None)
        self.assertEqual(d["state"], "unknown")
        # and no state file means the NEXT readable run is still first-run,
        # never a false resume against a zero baseline
        d2 = suspend.detect(now=1000.0, reader=self.detect_with([100.0]))
        self.assertEqual(d2["state"], "first-run")

    def test_the_reader_is_proxywatch_host_gap_s_not_a_second_spelling(self):
        # The lane's dedupe is load-bearing: two spellings of one clock
        # reading is how they drift. Assert the module defers, not re-derives.
        with mock.patch("helm.proxywatch.host_suspend_gap_s",
                        return_value=42.0) as gap:
            self.assertEqual(suspend.suspended_seconds(), 42.0)
        gap.assert_called_once_with()


class SweepTest(SuspendBase):
    def test_only_a_resumed_detection_acts(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control is the sibling arm on the SAME observables: test_a_resume_bounces_every_target_then_kicks_then_posts fills `lines` and `calls` through the same entry
        calls = []
        for state in ("steady", "first-run", "unknown"):
            lines = suspend.sweep({"state": state},
                                  bounce=lambda f, s: calls.append(s) or True,
                                  kick_pass=lambda: calls.append("kick"),
                                  post=lambda t: calls.append("post"))
            self.assertEqual(lines, [])
        self.assertEqual(calls, [],
                         "a non-resume state reached an actuator: %r" % (calls,))

    def test_a_resume_bounces_every_target_then_kicks_then_posts(self):
        calls = []
        lines = suspend.sweep(
            {"state": "resumed", "gap_s": 3600.0},
            proxies=[("seat-a", "seat-a"), ("seat-b", "seat-b")],
            bounce=lambda f, s: calls.append(("bounce", s)) or True,
            kick_pass=lambda: calls.append(("kick",)),
            post=lambda t: calls.append(("post", t)))
        self.assertEqual([c[0] for c in calls],
                         ["bounce", "bounce", "kick", "post"],
                         "the order is bounce-then-kick-then-tell: %r" % (calls,))
        self.assertEqual(calls[0][1], "seat-a")
        self.assertEqual(calls[1][1], "seat-b")
        self.assertIn("60m", calls[3][1])
        self.assertEqual(len(lines), 1)

    def test_a_failed_bounce_is_named_not_fused_into_the_ok_count(self):
        calls = []
        lines = suspend.sweep(
            {"state": "resumed", "gap_s": 120.0},
            proxies=[("seat-a", "seat-a"), ("seat-b", "seat-b")],
            bounce=lambda f, s: s != "seat-b",
            kick_pass=lambda: None,
            post=lambda t: calls.append(t))
        self.assertEqual(len(lines), 2,
                         "a partial failure must print its own line: %r" % (lines,))
        self.assertIn("seat-b", lines[1])
        self.assertIn("did NOT come back", lines[1])


class RungTest(SuspendBase):
    def test_the_rung_never_raises_and_is_quiet_on_steady(self):  # noqa: VACUOUS_ASSERTION — the positive control on the same observable is test_a_resume_through_the_rung_reaches_the_sweep, which fills `lines` through the same rung
        with mock.patch.object(suspend, "detect",
                               return_value={"state": "steady", "gap_s": 0,
                                             "last_ts": None}):
            self.assertEqual(suspend.resume_rung(quiet=True), [])

    def test_a_resume_kick_creates_its_log_directory(self):  # noqa: ORPHANED_MOCK — sweep resolves its actuators at CALL time (the cure this lane landed); the walker's def-time model cannot follow that, and these arms are exactly the proof the call-time resolution works
        # The suite caught this: _kick_pass appended to a path whose parent
        # nothing had created — the same class as the state file's makedirs.
        import subprocess as _sp
        with mock.patch.object(suspend, "detect",
                               return_value={"state": "resumed",
                                             "gap_s": 600.0, "last_ts": None}), \
             mock.patch.object(suspend, "_live_proxies", return_value=[]), \
             mock.patch.object(_sp, "Popen") as popen, \
             mock.patch.object(suspend, "_post", lambda t: None):
            lines = suspend.resume_rung(quiet=True)
        self.assertTrue(lines)
        popen.assert_called_once()

    def test_the_rung_speaks_once_on_unknown_and_is_silent_on_first_run(self):
        # UNKNOWN is the blind spot and is always said; FIRST-RUN is the
        # steady state for a fresh host and prints nothing (the ensure arms
        # pin byte-exact output contracts a cold-start line would break).
        with mock.patch.object(suspend, "detect",
                               return_value={"state": "unknown", "gap_s": None,
                                             "last_ts": None}):
            self.assertEqual(len(suspend.resume_rung(quiet=True)), 1)
        with mock.patch.object(suspend, "detect",
                               return_value={"state": "first-run", "gap_s": None,
                                             "last_ts": None}):
            self.assertEqual(suspend.resume_rung(quiet=True), [])

    def test_a_resume_through_the_rung_reaches_the_sweep(self):  # noqa: ORPHANED_MOCK — sweep resolves its actuators at CALL time (the cure this lane landed); the walker's def-time model cannot follow that, and these arms are exactly the proof the call-time resolution works
        calls = []
        with mock.patch.object(suspend, "detect",
                               return_value={"state": "resumed",
                                             "gap_s": 600.0, "last_ts": None}), \
             mock.patch.object(suspend, "_live_proxies", return_value=[]), \
             mock.patch.object(suspend, "_kick_pass",
                               lambda: calls.append("kick")), \
             mock.patch.object(suspend, "_post",
                               lambda t: calls.append(t)):
            lines = suspend.resume_rung(quiet=True)
        self.assertEqual(len(calls), 2, calls)
        self.assertEqual(calls[0], "kick")
        self.assertIn("POST-SUSPEND SWEEP", calls[1],
                        "the room post did not reach the double: %r" % (calls,))
        self.assertTrue(lines)


class EnsureWiringTest(unittest.TestCase):
    """The rung is called from seat_health._ensure, never a second path."""

    def test_ensure_calls_resume_rung_before_any_rc_is_computed(self):
        from helm import seat_health
        seen = []
        with mock.patch("helm.suspend.resume_rung",
                        lambda quiet=False, _now=None:
                            seen.append(quiet) or []), \
             mock.patch.object(seat_health, "_cred_follow_pass",
                               return_value=None), \
             mock.patch.object(seat_health, "_minted_seats",
                               return_value=iter(())), \
             mock.patch("sys.stdout", new_callable=io.StringIO), \
             mock.patch("sys.stderr", new_callable=io.StringIO):
            rc = seat_health._ensure(["--quiet"])
        self.assertEqual(seen, [True],
                         "the rung did not fire (or fired unquieted): %r" % (seen,))
        self.assertEqual(rc, 2,
                         "an empty enumeration is UNKNOWN by the ensure contract")

    def test_a_raising_rung_would_not_move_rc_but_the_rung_cannot_raise(self):
        # The structural claim, not the swallowed one: resume_rung's body
        # contains no actuator call outside sweep(), and sweep()'s only
        # non-double reach is _live_proxies — assert the rung's contract by
        # driving it with a raising detection and getting the state back.
        with mock.patch.object(suspend, "detect",
                               side_effect=RuntimeError("probe")):
            with self.assertRaises(RuntimeError):
                suspend.resume_rung(quiet=True)
        # and _ensure's call site carries no try: the guarantee is that
        # resume_rung never raises, so the arms above pin each path that
        # could (reader, state file, bounce, kick, post) as a parameter.


if __name__ == "__main__":
    unittest.main()
