#!/usr/bin/env python3
"""task/3039: a whole suite with no mode flag runs as SLICES where helm can
prove it is not a land gate, and SERIAL on every road that lands.

A sliced receipt (v10) binds a lane tip and a review's APPROVE and never a
land (`gate.land_refusal`, NEED_LAND). So the default flips only for the
lane-level rooms — a lane room admitted by `--lane-suite`, a peek, a seat's
home, a harness worktree — and every land road names its serial mode itself:
the landing window submits Fab's serial scope by name, a compose room hands
off to that window, a `train...` label is serial by rule, and inside a Fab job
no flag means Fab's serial scope. These arms pin each road, the loud fallback,
and that the default never re-opens a door train197 closed.
"""
import inspect
import json
import os
import unittest
from unittest import mock

from tests import _tmphome
from tests import test_gatewindow as _window
from tests.test_land_gate_once_doors import DoorBase, _git, _write
from helm import (fabgate, gate, gateimport, gateroute, gatewindow,
                  landwindow)


KIND_STUB = ('SLICE_VERSION = %d\nFLAGS = ("--sliced",)\n'
             % gate.SLICE_VERSION)


class SlicedDefaultBase(DoorBase):
    """DoorBase's lane and compose rooms, plus a peek, a seat home, a harness
    worktree and a train room outside the project's container, all on a main
    that ships the slice runner, on a host with CPUs to spare and outside any
    Fab job."""

    def setUp(self):
        super().setUp()
        _tmphome.own_env(self, gate.FAB_JOB_ENV, "")
        os.environ.pop(gate.FAB_JOB_ENV, None)
        for path in gate.SLICE_RUNNER_FILES:
            _write(self.root, path, "# the slice runner, as a file\n")
        # The tree's OWN helm must mint the sliced kind (`_slice_kind_missing`):
        # these are the two facts that check reads from its gate.py.
        _write(self.root, "helm/gate.py", KIND_STUB)
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "ship the slice runner")
        integrator = dict(os.environ, HELM_WORK_INTEGRATOR="1")
        _git(self.lane, "merge", "-q", "--ff-only", "main", env=integrator)
        _git(self.compose, "checkout", "-q", "--detach", "main",
             env=integrator)
        self.rooms = {}
        for kind, path in (
                ("peek", os.path.join(self.root + "-wt", "peeks", "p1")),
                ("seat", os.path.join(self.root + "-wt", "seats", "s1")),
                ("harness", os.path.join(self.root, ".claude", "worktrees",
                                         "agent-1")),
                ("outside", os.path.join(self.tmp, "var", "helm-train9"))):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            _git(self.root, "worktree", "add", "-q", "--detach", path, "main",
                 env=integrator)
            self.rooms[kind] = path
        cores = mock.patch.object(gate, "_online_cpu_count", return_value=16)
        cores.start()
        self.addCleanup(cores.stop)

    def last_mode(self):
        self.assertTrue(self.ran, "nothing reached gate.run")
        return self.ran[-1]["sliced"]

    def plan(self, *argv, env=None):
        rc, out, err = self.verb(*(argv + ("--plan", "--json")), env=env)
        return rc, json.loads(out), err


class DefaultModeArms(SlicedDefaultBase):

    def test_a_no_flag_lane_level_gate_runs_sliced(self):
        """Every lane-level room kind runs its no-flag whole suite as slices,
        and says so on stderr with the way back to serial."""
        cases = [("lane", self.lane, ("--lane-suite", "--why", "bisect"))]
        cases += [(kind, self.rooms[kind], ())
                  for kind in ("peek", "seat", "harness")]
        for kind, room, extra in cases:
            with self.subTest(room=kind):
                self.assertEqual(gate.suite_room(room), (kind, room))
                rc, _out, err = self.verb("--repo", room, *extra)
                self.assertEqual(rc, 1, err)   # the spy's refusal: it ran
                self.assertIs(self.last_mode(), True, err)
                self.assertIn("runs as SLICES", err)
                self.assertIn("--serial", err)

    def test_serial_forces_serial_and_two_modes_are_a_usage_error(self):
        peek = self.rooms["peek"]
        self.verb("--repo", peek, "--serial")
        self.assertIs(self.last_mode(), False)
        ran = len(self.ran)
        for argv in (("--sliced", "--serial"), ("--serial", "--focus"),
                     ("--serial", "--timings", "x.json")):
            with self.subTest(argv=argv):
                rc, _out, err = self.verb("--repo", peek, *argv)
                self.assertEqual(rc, 2, err)
        rc, _out, err = self.verb("--repo", peek, "--serial", "--", "true")
        self.assertEqual(rc, 2, err)
        self.assertEqual(len(self.ran), ran, "a usage error reached the run")
        # POSITIVE CONTROL on the same room: no flag is the sliced default.
        self.verb("--repo", peek)
        self.assertIs(self.last_mode(), True)

    def test_the_shared_checkout_and_an_outside_room_stay_serial(self):
        """A room that names no lane-level kind cannot be proven to be no
        land gate: OI's train rooms stand outside the container."""
        for room in (self.root, self.rooms["outside"]):
            with self.subTest(room=room):
                self.assertEqual(gate.suite_room(room)[0], None)
                _rc, _out, err = self.verb("--repo", room)
                self.assertIs(self.last_mode(), False)
                self.assertNotIn("SLICES", err)
                rc, answer, _err = self.plan("--repo", room)
                self.assertEqual(rc, 0)
                self.assertEqual(answer["mode"], gate.SERIAL)
                self.assertNotIn("slice", answer)
                self.assertIn("not a lane, peek, seat or harness room",
                              answer["mode_reason"])

    def test_a_train_labelled_no_flag_run_is_serial(self):
        peek = self.rooms["peek"]
        for label in ("train200", "Train-probe", " train9"):
            with self.subTest(label=label):
                self.verb("--repo", peek, "--label", label)
                self.assertIs(self.last_mode(), False)
                rc, answer, _err = self.plan("--repo", peek, "--label", label)
                self.assertEqual((rc, answer["mode"]), (0, gate.SERIAL))
        ran = len(self.ran)
        rc, _out, err = self.verb("--repo", peek, "--sliced", "--label",
                                  "train200")
        self.assertEqual(rc, 2, err)
        self.assertIn("land gate", err)
        self.assertEqual(len(self.ran), ran)
        # POSITIVE CONTROL: another label keeps the default.
        self.verb("--repo", peek, "--label", "retrain-notes")
        self.assertIs(self.last_mode(), True)

    def test_inside_a_fab_job_no_flag_is_serial(self):
        """Fab's spoke publishes the receipt under the scope its forwarded
        flags name: no --sliced is the serial scope, whatever room the node's
        snapshot looks like."""
        peek = self.rooms["peek"]
        fab = {gate.FAB_JOB_ENV: "lane-x-1234"}
        self.verb("--repo", peek, env=fab)
        self.assertIs(self.last_mode(), False)
        rc, answer, _err = self.plan("--repo", peek, env=fab)
        self.assertEqual((rc, answer["mode"]), (0, gate.SERIAL))
        self.assertIn("Fab job", answer["mode_reason"])
        # The flag is Fab's slice scope, so it still runs slices there.
        self.verb("--repo", peek, "--sliced", env=fab)
        self.assertIs(self.last_mode(), True)


class LandRoadArms(SlicedDefaultBase):
    """Every road that mints a land's receipt says SERIAL on its own."""

    def launches(self):
        calls = []

        def launch(room, label=None, trunk_ref=None, supersede=False, **_kw):
            calls.append((room, label))
            return 0, {"room": room}
        return calls, mock.patch.object(gatewindow, "launch", launch)

    def test_a_compose_room_launches_serial_through_the_window(self):
        calls, patch = self.launches()
        with patch:
            rc, _out, err = self.verb("--repo", self.compose, "--label",
                                      "train9")
            self.assertEqual(rc, 0, err)
            rc, _out, err = self.verb("--repo", self.compose, "--sliced")
            self.assertEqual(rc, 2, err)
            self.assertIn("compose room", err)
        self.assertEqual(calls, [(self.compose, "train9")])
        self.assertEqual(self.ran, [], "a compose room ran outside the window")
        self.assertEqual(gate.suite_mode(self.compose)[0], gate.SERIAL)

    def test_the_window_submits_fabs_serial_scope(self):
        """`helm train` and `lr compose` launch through gatewindow, whose
        keyed job names the serial scope: the runner Fab executes for it
        carries no mode flag, and inside that Fab job no flag is serial."""
        head = _git(self.compose, "rev-parse", "HEAD")
        seen = []

        def fab(argv, _timeout):
            seen.append(argv)
            return 0, _window.measured("snoozy"), ""

        identity, err = gatewindow.job_identity(
            {"room": self.compose, "head": head}, fab=fab)
        self.assertIsNone(err, err)
        scope = identity["request"]["identity"]["scope"]
        self.assertEqual(scope, fabgate.whole_scope())
        self.assertEqual(gateimport.fab_scope_gate_args(scope), [])
        self.assertEqual(identity["request"]["identity"]["runner"]["argv"],
                         list(gateimport.FAB_RUNNER_ARGV))
        scope_json = seen[0][seen[0].index("--scope-json") + 1]
        self.assertEqual(json.loads(scope_json), fabgate.whole_scope())
        # ...and the runner that scope names is serial inside a Fab job.
        self.assertEqual(gate.suite_mode(
            self.rooms["peek"], env={gate.FAB_JOB_ENV: "gate-1"})[0],
            gate.SERIAL)

    def test_landwindow_compose_gates_through_the_window(self):
        """The train's compose verb has one gate call, and it is the window
        (whose scope the arm above pins)."""
        source = inspect.getsource(landwindow.compose)
        self.assertIn("gatewindow.launch(", source)
        self.assertNotIn("gate.run(", source)


class FallbackArms(SlicedDefaultBase):

    def test_too_few_cpus_falls_back_to_serial_loudly(self):
        peek = self.rooms["peek"]
        with mock.patch.object(gate, "_online_cpu_count", return_value=2):
            _rc, _out, err = self.verb("--repo", peek)
            self.assertIs(self.last_mode(), False)
            self.assertIn("runs SERIAL, not as slices", err)
            self.assertIn("2 online CPUs", err)
            # The plan answer is for a run on ANOTHER host: this host's CPUs
            # do not decide it.
            rc, answer, _err = self.plan("--repo", peek)
            self.assertEqual((rc, answer["mode"]), (0, gate.SLICED))
        with mock.patch.object(gate, "_online_cpu_count", return_value=None):
            _rc, _out, err = self.verb("--repo", peek)
            self.assertIs(self.last_mode(), False)
            self.assertIn("unknown number of online CPUs", err)

    def test_a_tree_without_the_runner_falls_back_to_serial_loudly(self):
        peek = self.rooms["peek"]
        _git(peek, "rm", "-q", gate.SLICE_RUNNER)
        _git(peek, "commit", "-qm", "an older tree: no slice runner")
        _rc, _out, err = self.verb("--repo", peek)
        self.assertIs(self.last_mode(), False)
        self.assertIn("runs SERIAL, not as slices", err)
        self.assertIn(gate.SLICE_RUNNER, err)
        rc, answer, _err = self.plan("--repo", peek)
        self.assertEqual((rc, answer["mode"]), (0, gate.SERIAL))
        self.assertIn(gate.SLICE_RUNNER, answer["note"])
        self.assertNotIn("slice", answer)

    def test_a_tree_whose_helm_predates_the_kind_falls_back_loudly(self):
        """Trunk shipped helm/gateslice.py as a diagnostic before its gate.py
        took --sliced. The runner files are there; the tree's own helm, which
        is what a node or a box runs, cannot mint the kind."""
        peek = self.rooms["peek"]
        _write(peek, "helm/gate.py", "SUITE = ('unittest',)\n")
        _git(peek, "commit", "-qam", "a trunk before v10")
        for path in gate.SLICE_RUNNER_FILES:
            self.assertTrue(os.path.isfile(os.path.join(peek, path)))
        _rc, _out, err = self.verb("--repo", peek)
        self.assertIs(self.last_mode(), False)
        self.assertIn("runs SERIAL, not as slices", err)
        self.assertIn("predates the sliced kind", err)
        rc, answer, _err = self.plan("--repo", peek)
        self.assertEqual((rc, answer["mode"]), (0, gate.SERIAL))
        self.assertIn("predates the sliced kind", answer["note"])

    def test_this_helm_passes_its_own_kind_check(self):
        """The check reads two spellings from a tree's gate.py; this file must
        carry both, or a rename silently turns every default serial."""
        here = os.path.dirname(os.path.dirname(os.path.abspath(gate.__file__)))
        self.assertIsNone(gate._slice_kind_missing(here))

    def test_timings_follow_the_resolved_mode(self):
        peek = self.rooms["peek"]
        self.verb("--repo", peek, "--timings", "x.json")
        self.assertIs(self.last_mode(), True)
        ran = len(self.ran)
        rc, _out, err = self.verb("--repo", self.root, "--timings", "x.json")
        self.assertEqual(rc, 2, err)
        self.assertIn("this run is SERIAL", err)
        self.assertEqual(len(self.ran), ran)


class DoorOrderArms(SlicedDefaultBase):
    """train197's doors decide before the mode: the default never re-opens a
    refused lane-room suite or a tree that already holds a receipt."""

    def test_sliced_never_reenables_a_refused_lane_room_suite(self):
        for extra in ((), ("--sliced",)):
            with self.subTest(argv=extra):
                rc, _out, err = self.verb("--repo", self.lane, *extra)
                self.assertEqual(rc, 1, err)
                self.assertIn("LANE", err)
                self.assertNotIn("SLICES", err)
                rc, answer, _err = self.plan("--repo", self.lane, *extra)
                self.assertEqual(rc, 1)
                self.assertIsNone(answer["plan"])
        self.assertEqual(self.ran, [], "the default re-opened a lane room")
        # POSITIVE CONTROL: the escape reaches the run, as slices.
        self.verb("--repo", self.lane, "--lane-suite", "--why", "flake")
        self.assertIs(self.last_mode(), True)

    def test_a_green_tree_still_refuses_under_the_sliced_default(self):
        peek = self.rooms["peek"]
        row = self.plant(peek)
        rc, _out, err = self.verb("--repo", peek)
        self.assertEqual(rc, 1, err)
        self.assertIn(row["id"], err)
        self.assertEqual(self.ran, [])


class PlanAndBoxArms(SlicedDefaultBase):

    def test_the_plan_answer_names_the_mode_and_sizes_the_slices(self):
        rc, answer, _err = self.plan("--repo", self.rooms["peek"])
        self.assertEqual(rc, 0)
        self.assertEqual(answer["mode"], gate.SLICED)
        self.assertIn("peek room", answer["mode_reason"])
        self.assertEqual(answer["slice"]["workers"],
                         answer["slice"]["slots"] * gate._CORES_PER_SUITE)
        # `plan` stays the serial contract every reader validates.
        self.assertEqual(answer["plan"]["source"], "helm-default")
        # The hub does not tell the operator "slices" about a run a node
        # executes however its runner decides: no note for the default.
        self.assertNotIn("note", answer)
        rc, answer, _err = self.plan("--repo", self.rooms["peek"], "--serial")
        self.assertEqual((rc, answer["mode"]), (0, gate.SERIAL))

    def test_the_plan_text_names_the_mode(self):
        rc, out, _err = self.verb("--repo", self.rooms["peek"], "--plan")
        self.assertEqual(rc, 0)
        self.assertIn("mode         sliced", out)
        rc, out, _err = self.verb("--repo", self.root, "--plan")
        self.assertEqual(rc, 0)
        self.assertIn("mode         serial", out)

    def test_a_box_route_takes_the_resolved_mode(self):
        peek = self.rooms["peek"]
        with mock.patch.object(gateroute, "cmd_route", return_value=0) as route, \
                mock.patch.object(gate, "_online_cpu_count", return_value=2):
            self.verb("--box", "snoozy", "--repo", peek)
            self.verb("--box", "snoozy", "--repo", peek, "--serial")
            self.verb("--box", "snoozy", "--repo", self.root)
        # The box's own CPUs decide a routed run, never this host's.
        self.assertEqual([c.kwargs["sliced"] for c in route.call_args_list],
                         [True, False, False])


class SuiteModeUnitArms(unittest.TestCase):
    """The resolver's order without a repository: flags first, then Fab, then
    the train label — each ahead of any room."""

    def test_flags_fab_and_label_decide_before_the_room(self):
        """THE REASON IS THE OBSERVABLE, not the mode: a repository that does
        not exist resolves serial anyway (it ships no helm), so a mode-only
        assertion here would stay green with any of these rules deleted."""
        repo = "/nonexistent/helm-wt/peeks/p"
        cases = ((dict(sliced=True), gate.SLICED, "--sliced"),
                 (dict(serial=True), gate.SERIAL, "--serial"),
                 (dict(env={gate.FAB_JOB_ENV: "x"}), gate.SERIAL, "Fab job"),
                 (dict(label="train3"), gate.SERIAL, "train"),
                 (dict(), gate.SERIAL, "ships no helm"))
        for kwargs, mode, why in cases:
            with self.subTest(kwargs=kwargs):
                kwargs.setdefault("env", {})
                got, reason, fallback = gate.suite_mode(repo, **kwargs)
                self.assertEqual((got, fallback), (mode, False))
                self.assertIn(why, reason)

    def test_the_programmatic_run_stays_serial(self):
        """The default lives in the verb: `gate equiv` calls run() and must
        never inherit it."""
        self.assertIs(
            inspect.signature(gate.run).parameters["sliced"].default, False)


if __name__ == "__main__":
    unittest.main()
