"""The land gate runs as slices exactly while the land door admits them.

`helm gate window launch` is the one launcher of a train's land gate (`helm
train --apply` and `helm gate run` in a compose room hand to it). It measures
and submits Fab's SLICE scope when `gatewindow.land_mode` says so:

  * the land door admits a sliced receipt now (`gate.sliced_land_refusal`
    answers None: no DISABLE marker, and the canary record stands);
  * the room's tree can run slices (`gateshadow.sliceable`);
  * and the caller did not name `--serial`, the kept escape.

Anything else launches the serial scope, and the dispatch says which mode ran
and why. A sliced land gate schedules no shadow: its serial counterpart is the
nightly canary's. The disable marker the canary writes on its first
divergence turns the next launch serial with no person in the loop.
"""
import json
import os
from unittest import mock

from helm import (fabgate, gate, gatecanary, gateimport, gateshadow,
                  gatewindow, home)
from tests.test_gate_sliced_land import BOUND_ENV, age_record, stamp
from tests.test_gatewindow import QUIET_CLIENT, WindowBase, measured


def measured_for(argv, host="snoozy"):
    """`fab gate measure`'s answer for the scope the argv names: the slice
    scope's runner carries its own flag, as Fab's measure prints it."""
    body = json.loads(measured(host))
    scope = json.loads(argv[argv.index("--scope-json") + 1])
    body["runner"]["argv"] = list(gateimport.FAB_RUNNER_ARGV) \
        + gateimport.fab_scope_gate_args(scope)
    return json.dumps(body) + "\n"


class LandModeBase(WindowBase):

    def fab(self, *args, **kw):
        """WindowBase's fab spy, measuring the scope the door asked for."""
        inner = super().fab(*args, **kw)

        def _fab(argv, timeout=None, env=None):
            if argv[:3] == [gatewindow.FAB_BINARY, "gate", "measure"]:
                self.measures.append(list(argv))
                return 0, measured_for(argv), ""
            return inner(argv, timeout, env)
        return _fab

    def setUp(self):
        super().setUp()
        self.refused = None
        self.unsliceable = None
        for patch in (mock.patch.object(gateshadow, "sliceable",
                                        lambda room: self.unsliceable),):
            patch.start()
            self.addCleanup(patch.stop)

    def refusing(self, why):
        patch = mock.patch.object(gate, "sliced_land_refusal",
                                  lambda global_dir=None: why)
        patch.start()
        self.addCleanup(patch.stop)

    def scopes(self):
        """(measured scope, submitted scope) of the one launch."""
        measured = json.loads(self.measures[0][
            self.measures[0].index("--scope-json") + 1])
        body = json.loads(self.spawns[0][
            self.spawns[0].index("--request-json") + 1])
        return measured, body["identity"]["scope"]

    def shadows(self):
        return gateshadow.records(home.global_dir())


class TheLaunchFollowsTheDoor(LandModeBase):

    def test_an_admitting_door_launches_the_slice_scope_and_no_shadow(self):
        self.refusing(None)
        room = self.room("train01", self.c)
        rc, text, row = self.door(room, label="train01", detach=QUIET_CLIENT)
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.scopes(), (fabgate.slice_scope(),
                                         fabgate.slice_scope()))
        self.assertIn("mode:       SLICED — the canary stands for slices",
                      text)
        self.assertEqual(row["mode"], "sliced")
        self.assertEqual(self.shadows(), [])
        self.assertNotIn("shadow:", text)

    def test_serial_by_name_even_when_the_door_admits(self):
        self.refusing(None)
        room = self.room("train01", self.c)
        rc, text, row = self.door(room, label="train01", serial=True,
                                  detach=QUIET_CLIENT)
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.scopes(), (fabgate.whole_scope(),
                                         fabgate.whole_scope()))
        self.assertIn("mode:       SERIAL — --serial asked", text)
        self.assertEqual(row["mode"], "serial")
        self.assertEqual(len(self.shadows()), 1)     # a serial gate's shadow

    def test_a_refusing_door_launches_serial_and_says_why(self):
        self.refusing("planted: the record does not stand")
        room = self.room("train01", self.c)
        rc, text, row = self.door(room, label="train01", detach=QUIET_CLIENT)
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.scopes(), (fabgate.whole_scope(),
                                         fabgate.whole_scope()))
        self.assertIn("mode:       SERIAL — the land door refuses a sliced "
                      "receipt now (planted: the record does not stand)", text)

    def test_a_tree_that_cannot_run_slices_launches_serial(self):
        self.refusing(None)
        self.unsliceable = "the tree does not ship the slice runner (planted)"
        room = self.room("train01", self.c)
        rc, text, _row = self.door(room, label="train01", detach=QUIET_CLIENT)
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.scopes()[1], fabgate.whole_scope())
        self.assertIn("SERIAL — the tree does not ship the slice runner", text)


class APlantedMarkerSendsTheLaunchBackToSerial(LandModeBase):
    """The real predicate over a real record: standing admits slices, and the
    marker the canary writes on a divergence turns the next launch serial."""

    def stand(self):
        gd = home.global_dir()
        self.assertTrue(gatecanary.finder_rows({
            "host": "finder-node", "each": {"clean": True},
            "whole": {"clean": True}}, "c" * 40, gd))
        minted = stamp()
        for n in range(gatecanary.STANDING_AGREE):
            tree = "%040x" % (n + 1)
            self.assertTrue(gatecanary.append_verdict(
                {"verdict": gatecanary.AGREE, "reason": "planted",
                 "shared_failures": 1 if n == 0 else 0, "divergences": []},
                {"id": "s%d" % n, "tree": tree, "ts": minted,
                 "status": "FAILED" if n == 0 else "OK",
                 "host": {"node": "serial-node"}},
                {"id": "l%d" % n, "tree": tree, "host": {"node": "node-%d" % n},
                 "ts": minted, "slice_authority": {"leak_mode": "fail"}},
                gatecanary.COMPARE, gd))

    def test_a_silent_canary_turns_the_next_launch_serial(self):
        self.stand()
        room = self.room("train01", self.c)
        with mock.patch.dict(os.environ):
            os.environ.pop(BOUND_ENV, None)
            self.assertEqual(gatewindow.land_mode(room)[0], True)
            age_record(gatecanary.history_path(home.global_dir()), 37)
            sliced, why = gatewindow.land_mode(room)
        self.assertEqual(sliced, False)
        self.assertIn("the land door refuses a sliced receipt now", why)
        self.assertIn("the canary has been silent since", why)

    def test_the_marker_turns_the_next_launch_serial(self):
        self.stand()
        room = self.room("train01", self.c)
        self.assertEqual(gatewindow.land_mode(room)[0], True)
        rc, text, _row = self.door(room, label="train01", detach=QUIET_CLIENT)
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.scopes()[1], fabgate.slice_scope())
        # The night's canary diverges: it writes the marker, nothing else.
        gatecanary.write_marker({"reason": "planted divergence",
                                 "divergences": []},
                                {"tree": "d" * 40, "id": "night-serial"},
                                {"id": "night-sliced"})
        sliced, why = gatewindow.land_mode(room)
        self.assertEqual(sliced, False)
        self.assertIn("the gate canary saw serial and sliced disagree", why)
        second = self.room("train02", self.b)
        self.measures[:], self.spawns[:] = [], []
        rc, text, _row = self.door(second, label="train02", trunk_ref=self.b,
                                   detach=QUIET_CLIENT,
                                   observe=self.observe(()))
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.scopes()[1], fabgate.whole_scope())
        self.assertIn("mode:       SERIAL — the land door refuses a sliced "
                      "receipt now (the gate canary saw serial and sliced "
                      "disagree", text)
