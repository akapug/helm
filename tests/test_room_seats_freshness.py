"""_room_seats' consumers-fallback (second loop) is O(fresh), not O(all).

The UI-blank class, second half: _rooms_summary was ~14s because
_room_seats walked EVERY seat in the roster (~224, mostly DEAD ephemeral
review-SAs) calling seats.room_active — several pk.read_json disk cursor reads
each — until 6 sidebar slots filled. Quiet rooms scanned the whole roster.

The fix is a freshness pre-filter: only a seat with a recent presence beat
(presence_of(last_seen) != "absent", the same window presence_of paints and the
same idiom as seats._live_seats) can hold a live-sidebar slot, so the expensive
room_active cursor walk runs ONLY on seats that could legitimately be present.
That is both the speedup and a UX improvement (a seat that consumed the room
hours ago and is now dead is noise, not presence).

These tests PLANT a large roster (~200 stale/dead + a few fresh) and assert
room_active is invoked ONLY on the fresh survivors, and that a fresh
never-posted consumer still appears while a stale one does not.
"""
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from helm import web, seats  # noqa: E402

ENV_KEYS = ("HELM_HOME", "HELM_CHAT_DIR", "HELM_CHAT_NAME",
            "HELM_CHAT_NODE_URL", "HELM_CHAT_OWNER_NAMES")


class RoomSeatsFreshnessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-roomseats-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_OWNER_NAMES"] = "owner"

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _plant(self, n_dead=200, fresh=("fresh-a", "fresh-b", "fresh-c")):
        """A roster of n_dead absent seats + `fresh` recently-beating seats,
        NONE of which posted. Un-homed rows (no home_room) so every seat is in
        scope for any room. last_seen rides the roster row: there is no .seen
        file in the fresh tmp chat dir, so last_seen() falls back to the
        carried beat — old for the dead, now for the fresh."""
        now = time.time()
        roster = {}
        for i in range(n_dead):
            # absent: last beat well past QUIET_S (900s)
            roster["agent-%04x" % i] = {"last_seen": now - 10000,
                                        "cwd": "/tmp/review-%d" % i}
        for s in fresh:
            roster[s] = {"last_seen": now, "cwd": "/tmp/live"}
        return roster

    def test_room_active_runs_only_on_fresh_survivors(self):
        roster = self._plant()
        fresh = {"fresh-a", "fresh-b", "fresh-c"}
        called = []

        def fake_room_active(room, seat):
            called.append(seat)
            return True                      # every consumer "consumed" the room

        with mock.patch.object(seats, "room_active",
                               side_effect=fake_room_active):
            out = web._room_seats("main", [], roster)

        # the expensive cursor walk touched ONLY the fresh survivors — never any
        # of the 200 dead ephemeral seats (the O(all)->O(fresh) turn)
        self.assertEqual(set(called), fresh)
        self.assertEqual(len(called), len(fresh))

        # the fresh never-posted consumers appear; no dead seat leaked a slot
        shown = {r["seat"] for r in out}
        self.assertEqual(shown, fresh)
        self.assertFalse(any(s.startswith("agent-") for s in shown))

    def test_fresh_never_posted_consumer_appears_stale_does_not(self):
        # one fresh consumer + one stale consumer, both never posted, both with
        # an ACTIVE room cursor. Freshness — not room_active — decides the slot.
        now = time.time()
        roster = {
            "freshc": {"last_seen": now, "cwd": "/tmp/live"},
            "stalec": {"last_seen": now - 10000, "cwd": "/tmp/dead"},
        }
        called = []

        def fake_room_active(room, seat):
            called.append(seat)
            return True

        with mock.patch.object(seats, "room_active",
                               side_effect=fake_room_active):
            out = web._room_seats("main", [], roster)

        shown = {r["seat"] for r in out}
        self.assertIn("freshc", shown)       # genuinely fresh consumer appears
        self.assertNotIn("stalec", shown)    # a stale one does not
        # room_active never paid a disk read for the stale seat
        self.assertEqual(called, ["freshc"])

    def test_fresh_but_inactive_seat_still_gated_by_room_active(self):
        # freshness OPENS the gate; room_active still closes it. A fresh seat
        # holding a bare EOF join baseline (never consumed the room) is not
        # presence, so it must not appear even though its beat is recent.
        now = time.time()
        roster = {"freshc": {"last_seen": now, "cwd": "/tmp/live"}}

        with mock.patch.object(seats, "room_active", return_value=False):
            out = web._room_seats("main", [], roster)

        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()
