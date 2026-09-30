#!/usr/bin/env python3
"""A pane-woken seat owes no beacon block (task/3338).

The stop guard's armed-beacon gate blocks a seat that owes dispatch work
and has no `helm chat wait` process. A seat whose family catalogues
`wake: pane` cannot run that waiter (its harness has no Monitor); the
operator types the wake into the pane. For that seat the gate returns
nothing and writes no latch. Every other seat, and a wake field that
cannot be read, keeps today's block and today's text.
"""
import glob
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import seat, seat_catalog, seats, seats_stop_signals  # noqa: E402,F401 — seat is the facade the injection audit requires beside seat_catalog
from tests.test_seats import SeatsBase  # noqa: E402


class _Unread(dict):
    """A family record whose wake field cannot be read.

    Both lookups raise. ``dict.get`` is a C method and would otherwise
    return the stored value without calling ``__getitem__``.
    """

    def get(self, key, default=None):
        if key == "wake":
            raise OSError("wake unreadable")
        return dict.get(self, key, default)

    def __contains__(self, key):
        if key == "wake":
            raise OSError("wake unreadable")
        return dict.__contains__(self, key)

    def __getitem__(self, key):
        if key == "wake":
            raise OSError("wake unreadable")
        return dict.__getitem__(self, key)


class PaneWakeBeaconTest(SeatsBase):
    """The pane-wake exemption and its transitions, through the real verb."""

    def setUp(self):
        super().setUp()
        # SeatsBase already disables the scratch reaper. A module that drives
        # stop-guard has to name that setting itself; the parent lives in
        # another file, which the tripwire does not read.
        self.assertEqual(os.environ["HELM_SCRATCH_GC"], "0")

    def tearDown(self):
        chat_name = getattr(self, "_chat_name", None)
        if chat_name is not None:
            chat_name.stop()
        super().tearDown()

    def guard(self, payload=None, pids=(), trouble=None, obligation=True):
        stdin = json.dumps(payload or {"session_id": "s-pane"}).encode()
        with mock.patch.object(seats, "beacon_procs",
                               return_value=(list(pids), trouble)), \
                mock.patch.object(seats_stop_signals, "_beacon_obligation",
                                  return_value=obligation):
            return self.cmd("stop-guard", ["--hook-json"], stdin=stdin)

    def seat_up(self, name, session=None, runtime=None):
        # patch.dict restores on stop. A subscript assign would leak the
        # launch stamp, and stopping after SeatsBase.tearDown would wipe the
        # value that tearDown just put back.
        self._chat_name = mock.patch.dict(os.environ, {"HELM_CHAT_NAME": name})
        self._chat_name.start()
        seats.join(seat=name, session=session, runtime=runtime, cwd="/tmp/p")

    def beacon_latches(self):
        return glob.glob(os.path.join(os.environ["HELM_CHAT_DIR"],
                                       "*stopbeacon*"))

    def assert_blocks_with_the_same_text(self, name, err):
        self.assertIn("NO ARMED BEACON", err)
        self.assertIn("seat '%s'" % name, err)
        self.assertIn('Monitor(command: "helm chat wait --seat %s --follow", '
                      'description: "inbox beacon", timeout_ms: 1800000)'
                      % name, err)
        self.assertIn("re-arm it in the same turn", err)
        self.assertIn("select:Monitor", err)
        self.assertIn("98 undelivered", err)
        self.assertIn("holds owed dispatch work", err)

    def test_pane_driven_seat_that_owes_work_and_has_no_beacon_does_not_block(self):  # noqa: VACUOUS_ASSERTION — the normal-seat arm in this class is the positive control: it requires NO ARMED BEACON and a stopbeacon latch
        """The field, not the seat's name: a family that catalogues pane is
        exempt, writes no latch, and is not handed a substitute sentence."""
        family = "codex"  # noqa: SEAT_NAME — catalog family, the wake field's subject
        spec = dict(seat_catalog.FAMILIES[family], wake="pane")
        self.seat_up(family, "s-pane", {
            "family": family, "agent_harness": "claude", "backend": "proxy"})
        with mock.patch.dict(seat_catalog.FAMILIES, {family: spec}):
            rc, _out, err = self.guard()
        self.assertEqual(rc, 0, err)
        self.assertNotIn("NO ARMED BEACON", err)
        self.assertNotIn("NO PROVEN WAKE PATH", err)
        self.assertNotIn("arm the beacon", err)
        self.assertNotIn("wake path is the pane", err)
        self.assertEqual(self.beacon_latches(), [])

    def test_the_cursor_family_is_beacon_woken_and_its_seat_blocks(self):
        """The measured seat: the cursor seat's Monitor was missing only
        because its launch line turned off Claude Code's nonessential traffic,
        which takes Monitor out of the tool list (measured at claude 2.1.284).
        That switch is gone, so no family catalogues a pane wake and a cursor
        seat that owes work with no beacon gets the same block as any seat."""
        family = "cursor"  # noqa: SEAT_NAME — catalog family, the measured seat
        self.assertIn("mode", seat_catalog.FAMILIES[family])
        self.assertIsNone(seat_catalog.FAMILIES[family].get("wake"))
        self.assertEqual([f for f, fam in seat_catalog.FAMILIES.items()
                          if fam.get("wake") == "pane"], [])
        self.seat_up(family, "s-cursor", {
            "family": family, "agent_harness": "claude", "backend": "proxy"})
        rc, _out, err = self.guard({"session_id": "s-cursor"})
        self.assertEqual(rc, 2, err)
        self.assert_blocks_with_the_same_text(family, err)
        self.assertTrue(self.beacon_latches())

    def _cursor_pane(self):
        """Plant `wake: pane` on the cursor family for one arm. No family
        catalogues it now, and the name-costume arms below only mean something
        while the family whose name they borrow is pane-woken."""
        family = "cursor"  # noqa: SEAT_NAME — catalog family whose name the costumes borrow
        return mock.patch.dict(seat_catalog.FAMILIES, {
            family: dict(seat_catalog.FAMILIES[family], wake="pane")})

    def test_a_native_claude_seat_named_cursor_does_not_inherit_pane_wake(self):
        """The launch identity outranks an exact family-name costume."""
        native = {"family": "claude", "agent_harness": "claude",
                  "backend": "native"}
        self.seat_up("cursor", "s-native-cursor", native)
        with self._cursor_pane():
            rc, _out, err = self.guard({"session_id": "s-native-cursor"})
        self.assertEqual(rc, 2)
        self.assert_blocks_with_the_same_text("cursor", err)

    def test_a_nonspawnable_numbered_cursor_name_does_not_inherit_pane_wake(self):
        """A family-N spelling counts only where that family admits instances.

        Cursor is proxy-key: its only real seat is the bare family seat. A
        native or ad-hoc seat stamped ``cursor-2`` must not acquire Cursor's
        pane wake merely by choosing a name the spawn gate refuses.
        """
        self.assertEqual(seat_catalog.FAMILIES["cursor"]["mode"], "proxy-key")
        self.seat_up("cursor-2", "s-cursor-2")
        with self._cursor_pane():
            rc, _out, err = self.guard({"session_id": "s-cursor-2"})
        self.assertEqual(rc, 2)
        self.assert_blocks_with_the_same_text("cursor-2", err)

    def test_a_family_like_noninstance_name_does_not_inherit_pane_wake(self):
        self.seat_up("cursor-x", "s-cursor-x")
        with self._cursor_pane():
            rc, _out, err = self.guard({"session_id": "s-cursor-x"})
        self.assertEqual(rc, 2)
        self.assert_blocks_with_the_same_text("cursor-x", err)

    def test_pane_transition_clears_an_old_latch_and_normal_reblocks(self):
        """A stale missing latch may not suppress a later normal wake policy."""
        family = "codex"  # noqa: SEAT_NAME — catalog family under transition
        normal = dict(seat_catalog.FAMILIES[family])
        normal.pop("wake", None)
        pane = dict(normal, wake="pane")
        payload = {"session_id": "s-transition"}
        self.seat_up(family, "s-transition", {
            "family": family, "agent_harness": "claude", "backend": "proxy"})

        with mock.patch.dict(seat_catalog.FAMILIES, {family: normal}):
            rc, _out, err = self.guard(payload)
        self.assertEqual(rc, 2, err)
        self.assertTrue(self.beacon_latches())

        with mock.patch.dict(seat_catalog.FAMILIES, {family: pane}):
            rc, _out, err = self.guard(payload)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.beacon_latches(), [])

        with mock.patch.dict(seat_catalog.FAMILIES, {family: normal}):
            rc, _out, err = self.guard(payload)
        self.assertEqual(rc, 2, err)
        self.assert_blocks_with_the_same_text(family, err)

    def test_normal_seat_that_owes_work_and_has_no_beacon_still_blocks(self):
        self.seat_up("seat-under-test", "s-normal")
        rc, _out, err = self.guard({"session_id": "s-normal"})
        self.assertEqual(rc, 2)
        self.assert_blocks_with_the_same_text("seat-under-test", err)
        self.assertTrue(self.beacon_latches())

    def test_normal_seat_with_a_live_beacon_does_not_block(self):  # noqa: VACUOUS_ASSERTION — the normal-seat arm in this class is the positive control: it requires NO ARMED BEACON and a stopbeacon latch
        self.seat_up("seat-under-test", "s-armed")
        rc, _out, err = self.guard({"session_id": "s-armed"}, pids=[4242])
        self.assertEqual(rc, 0, err)
        self.assertNotIn("NO ARMED BEACON", err)

    def test_unreadable_wake_path_still_blocks(self):
        """Fail closed: a wake field that cannot be read is not pane-driven."""
        family = "codex"  # noqa: SEAT_NAME — catalog family, the unreadable field's subject
        spec = _Unread(seat_catalog.FAMILIES[family])
        spec["wake"] = "pane"
        self.seat_up(family, "s-unread", {
            "family": family, "agent_harness": "claude", "backend": "proxy"})
        with mock.patch.dict(seat_catalog.FAMILIES, {family: spec}):
            rc, _out, err = self.guard({"session_id": "s-unread"})
        self.assertEqual(rc, 2)
        self.assert_blocks_with_the_same_text(family, err)


if __name__ == "__main__":
    unittest.main()
