"""Arms for `helm/fanout.py` — the DEMAND term, per seat.

F23 IS THE WHOLE MODULE: a fan-out reading never invents a zero. Every arm
here is paired with the control that proves the walk could have returned a
number, because "0" and "I could not look" render identically in a cap
subtraction and differ by the whole bill.

NO LIVE SEAT IS READ. The instance tree is built under a temp dir and the
seat-to-directory resolver is seamed, so nothing here depends on which seats
exist on the host running it.
"""

import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import fanout                                 # noqa: E402
from helm import proxywatch                             # noqa: E402


class _Seat(object):
    """The seat facade's two answers, seamed. `_instance_dir` is what the
    watchdog pass itself calls, so the arms exercise the same locator."""

    def __init__(self, root, family="codex"):
        self.root, self.family = root, family
        self.FAMILIES = {family: {}}

    def _seat_family(self, name):
        if name.startswith("unknown"):
            return None, "no family for %r" % name
        return self.family, None

    def _instance_dir(self, family, seat):
        return os.path.join(self.root, family, "instances", seat)


class FanoutLiveTest(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="helm-fanout-arm-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.seat = _Seat(self.root)

    def _tree(self, name, count, age_s=0.0):
        inst = self.seat._instance_dir(self.seat.family, name)
        sub = os.path.join(inst, "claude", "projects", "proj", "sess",
                           "subagents")   # noqa: SEAT_NAME — the transcript
        # tree's own directory layout, which is what the walk globs; no seat
        # is named here.
        os.makedirs(sub)
        now = time.time()
        for i in range(count):
            path = os.path.join(sub, "agent-%d.jsonl" % i)
            with open(path, "w") as fh:
                fh.write("{}\n")
            os.utime(path, (now - age_s, now - age_s))
        return inst

    def test_f23_a_readable_tree_reports_the_measured_count(self):
        self._tree("seat-a", 3)
        row = fanout.live("seat-a", seatmod=self.seat)
        self.assertEqual(row["running"], 3)
        self.assertTrue(row["measured"])
        self.assertIsNone(row["why"])
        self.assertEqual(row["family"], "codex")

    def test_f23_a_proven_empty_tree_is_a_measured_zero(self):
        """THE CONTROL FOR EVERY UNKNOWN BELOW: a zero IS reachable, from a
        directory the walk proved it could read."""
        self._tree("seat-b", 0)
        row = fanout.live("seat-b", seatmod=self.seat)
        self.assertEqual(row["running"], 0)
        self.assertTrue(row["measured"])

    def test_f23_a_missing_instance_tree_is_UNKNOWN_and_never_zero(self):
        row = fanout.live("seat-never-launched", seatmod=self.seat)
        self.assertIsNone(row["running"])
        self.assertFalse(row["measured"])
        self.assertIn("UNKNOWN", row["why"])

    def test_f23_stale_transcripts_fall_out_of_the_window(self):
        self._tree("seat-c", 2, age_s=proxywatch._FANOUT_WINDOW_S + 60)
        row = fanout.live("seat-c", seatmod=self.seat)
        self.assertEqual(row["running"], 0)
        self.assertTrue(row["measured"])
        self.assertEqual(row["window_s"], proxywatch._FANOUT_WINDOW_S)

    def test_f23_an_unresolved_family_is_UNKNOWN_with_the_resolvers_reason(self):
        row = fanout.live("unknown-seat", seatmod=self.seat)
        self.assertIsNone(row["running"])
        self.assertFalse(row["measured"])
        self.assertIn("family is unresolved", row["why"])

    def test_f23_an_empty_seat_name_is_UNKNOWN_rather_than_the_whole_root(self):
        row = fanout.live("", seatmod=self.seat)
        self.assertIsNone(row["running"])
        self.assertIn("no seat name", row["why"])

    def test_f23_a_reader_that_raises_does_not_become_an_idle_seat(self):
        def explodes(instance_dir, now=None):
            raise OSError("the walk failed")
        self._tree("seat-d", 4)
        row = fanout.live("seat-d", seatmod=self.seat, reading=explodes)
        self.assertIsNone(row["running"])
        self.assertFalse(row["measured"])
        self.assertIn("the fan-out reading failed", row["why"])
        # the control: the same tree, the real reader, reads 4
        self.assertEqual(fanout.live("seat-d", seatmod=self.seat)["running"],
                         4)

    def test_f23_an_UNKNOWN_reading_is_carried_out_unflattened(self):
        def blind(instance_dir, now=None):
            return {"active": None, "window_s": 180}
        self._tree("seat-e", 2)
        # THE POSITIVE CONTROL FIRST: this tree really does hold two live
        # transcripts, so the None below is the reading being carried out
        # unflattened and not an empty directory answering honestly.
        self.assertEqual(fanout.live("seat-e", seatmod=self.seat)["running"],
                         2)
        row = fanout.live("seat-e", seatmod=self.seat, reading=blind)
        self.assertIsNone(row["running"])
        self.assertFalse(row["measured"])

    def test_the_walk_is_not_reimplemented_here(self):
        """ARCHAEOLOGY, ASSERTED. The counting walk lives in `proxywatch`
        with two rounds of cures in it; this module resolves a seat name and
        delegates. An arm, because a later author 'simplifying' the import
        away would silently re-open every cured hazard."""
        seen = []

        def spy(instance_dir, now=None):
            seen.append(instance_dir)
            return {"active": 1, "window_s": 180}
        self._tree("seat-f", 1)
        fanout.live("seat-f", seatmod=self.seat, reading=spy)
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].endswith(os.path.join("instances", "seat-f")))
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "helm", "fanout.py")
        with open(path) as fh:
            source = fh.read()
        self.assertIn("proxywatch.fanout_reading", source)
        # The walk's own machinery, named where it cannot appear in prose:
        # `agent-` is not on this list because the docstring NAMES the cured
        # hazard, and an arm that forbids describing what it protects is a
        # worse arm than none.
        for reimplemented in ("os.scandir", "st_mtime", "S_ISREG",
                              "follow_symlinks"):
            self.assertNotIn(reimplemented, source)

    def test_the_default_reading_is_the_watchdogs_own(self):
        self._tree("seat-g", 2)
        direct = proxywatch.fanout_reading(
            self.seat._instance_dir("codex", "seat-g"))
        self.assertEqual(direct["active"], 2)       # the reading is not None
        row = fanout.live("seat-g", seatmod=self.seat)
        self.assertEqual(row["running"], direct["active"])
        self.assertEqual(row["window_s"], direct["window_s"])


if __name__ == "__main__":
    unittest.main()
