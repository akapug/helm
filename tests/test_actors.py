#!/usr/bin/env python3
"""helm.actors' system rank capability (task/3899): the one rank a subsystem
may make with no seat in the loop.

The friction autopilot's timer pass has no seat, and tasks.update refuses a
rank change without an admitted actor. The capability that closes that gap is
narrower than any seat's rank: it mints only for a subsystem in a closed set,
and it admits one change only, a raise from P1 to P0 of a row the subsystem's
own map holds and its `source` names. These arms drive the mint and the
capability's own judgment; tests/test_frictionpilot drives it through the real
task door and the timer's pass.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-actors-", var="HELM_HOME")

from helm import actors, frictionpilot  # noqa: E402

FILED = "task/7"


def _row(tid=FILED, source=frictionpilot.POSTER, priority="P1"):
    return {"id": tid, "source": source, "priority": priority}


class SystemRankSetTest(unittest.TestCase):
    def test_the_set_names_only_the_friction_autopilot(self):
        self.assertEqual(actors.SYSTEM_RANK_SUBSYSTEMS, ("frictionpilot",))
        self.assertEqual(set(actors.SYSTEM_RANK_SOURCES),
                         set(actors.SYSTEM_RANK_SUBSYSTEMS))
        # the source the capability demands is the one the autopilot files as
        self.assertEqual(actors.SYSTEM_RANK_SOURCES[frictionpilot.SYSTEM],
                         frictionpilot.POSTER)
        # the lease set is its own closed set, untouched by this one
        self.assertEqual(actors.SYSTEM_LEASE_SUBSYSTEMS, ("gate",))

    def test_another_subsystem_name_is_refused(self):
        # the positive control on the same mint
        cap, err = actors.grant_system_rank("frictionpilot", [FILED])
        self.assertIsNone(err)
        self.assertEqual(cap.ranker, "system:frictionpilot")
        for name in ("gate", "beacon", "friction-autopilot", "FrictionPilot",
                     "frictionpilot ", "", None):
            with self.subTest(name=name):
                cap, err = actors.grant_system_rank(name, [FILED])
                self.assertIsNone(cap)
                self.assertIn("not a subsystem that may rank", err)

    def test_it_cannot_be_constructed_outside_its_mint(self):
        # REFLECTIVELY, as tests/test_identity_layer does: its construction
        # census forbids a source-visible call of the class outside the layer.
        forge = getattr(actors, "SystemRankCapability")
        with self.assertRaises(actors.ActorRefused) as got:
            forge("frictionpilot", [FILED])
        self.assertEqual(got.exception.reason, actors.FORGED)
        cap, _err = actors.grant_system_rank("frictionpilot", [FILED])
        with self.assertRaises(AttributeError):
            cap._rows = frozenset(["task/1", FILED])
        self.assertEqual(cap.rows, frozenset([FILED]))

    def test_its_holder_cannot_be_read_as_a_seat(self):
        cap, _err = actors.grant_system_rank("frictionpilot", [FILED])
        self.assertFalse(isinstance(cap, actors.AdmittedActor))
        self.assertFalse(hasattr(cap, "canonical_name"))
        self.assertTrue(cap.ranker.startswith("system:"))


class SystemRankRefusalTest(unittest.TestCase):
    def setUp(self):
        self.cap, err = actors.grant_system_rank("frictionpilot",
                                                 [FILED, "", None])
        self.assertIsNone(err)

    def test_a_raise_from_P1_to_P0_of_its_own_row_is_admitted(self):  # noqa: VACUOUS_ASSERTION — the None is the admission itself, and cap.rows is asserted unconditionally on the same capability; the refusal arms beside it drive the same refusal() to a non-empty reason
        self.assertIsNone(self.cap.refusal(_row(), {"priority": "P0"}))
        self.assertEqual(self.cap.rows, frozenset([FILED]))

    def test_a_row_it_did_not_file_is_refused(self):  # noqa: VACUOUS_ASSERTION — the first refusal() call is the admitted control; every later call on the same capability asserts a non-empty reason text
        self.assertIsNone(self.cap.refusal(_row(), {"priority": "P0"}))
        got = self.cap.refusal(_row(tid="task/8"), {"priority": "P0"})
        self.assertIn("task/8 is not a row frictionpilot filed", got)
        # in its map, but a seat filed it (an adopted row)
        got = self.cap.refusal(_row(source="seat-a"), {"priority": "P0"})
        self.assertIn("filed by seat-a, not by system:frictionpilot", got)
        got = self.cap.refusal(_row(source=None), {"priority": "P0"})
        self.assertIn("filed by nobody recorded", got)

    def test_a_lowering_is_refused(self):  # noqa: VACUOUS_ASSERTION — the first refusal() call is the admitted control; each lowering asserts the reason text on the same capability
        self.assertIsNone(self.cap.refusal(_row(), {"priority": "P0"}))
        for was, want in (("P0", "P1"), ("P1", "P2"), ("P0", "P3")):
            with self.subTest(move=(was, want)):
                got = self.cap.refusal(_row(priority=was), {"priority": want})
                self.assertIn("only raises P1 to P0", got)
                self.assertIn("a lowering", got)

    def test_anything_but_P1_to_P0_is_refused(self):  # noqa: VACUOUS_ASSERTION — the first refusal() call is the admitted control; every other move asserts the reason text on the same capability
        self.assertIsNone(self.cap.refusal(_row(), {"priority": "P0"}))
        for was, want in (("P2", "P0"), (None, "P0"), (None, "P1"),
                          ("P2", "P1"), ("P1", None)):
            with self.subTest(move=(was, want)):
                got = self.cap.refusal(_row(priority=was), {"priority": want})
                self.assertIn("only raises P1 to P0", got)
        got = self.cap.refusal(_row(), {"priority": "P0", "title": "x"})
        self.assertIn("changes only a row's priority", got)
        self.assertIn("title", got)
        got = self.cap.refusal(_row(), {})
        self.assertIn("only raises P1 to P0", got)


if __name__ == "__main__":
    unittest.main()
