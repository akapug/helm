"""_room_seats' consumers-fallback (second loop) is O(fresh), not O(all).

The 2026-07-23 UI-blank second half: _rooms_summary was ~14s because
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
from helm import chat, pk, web, seats, seats_cursor  # noqa: E402

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
        os.environ["HELM_CHAT_OWNER_NAMES"] = "daria"

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

    def test_whole_summary_indexes_cursor_directory_once(self):
        """The cold-open regression: room fanout must not glob the whole chat
        directory once per fresh (room, seat) pair. Session cursor activity is
        still surfaced from the one request-wide filename snapshot."""
        roster = self._plant(n_dead=0, fresh=("fresh-a", "fresh-b"))
        for room in ("main", "side"):
            os.makedirs(chat.chat_dir(), exist_ok=True)
            with open(chat.room_path(room), "w", encoding="utf-8") as f:
                f.write('{"from":"someone","text":"hello","ts":"1"}\n')
        pk.write_json(seats.cursor_path("side", "fresh-a", "session-a"),
                      {"active": True})
        # Production held ~25k siblings. These unrelated cursor-shaped names
        # make a per-pair directory walk expensive while remaining cheap enough
        # for a unit guard; the canonical index must list once.
        for i in range(1000):
            open(os.path.join(chat.chat_dir(),
                              "noise.cursor.dead-%04d.session" % i), "a").close()
        real_listdir = os.listdir
        calls = []

        def counted(path):
            if os.path.realpath(path) == os.path.realpath(chat.chat_dir()):
                calls.append(path)
            return real_listdir(path)

        with mock.patch("os.listdir", side_effect=counted):
            rooms = {r["room"]: r for r in web._rooms_summary(roster)}

        self.assertEqual(calls.count(chat.chat_dir()), 2)  # rooms + cursors
        self.assertIn("fresh-a", {r["seat"] for r in rooms["side"]["seats"]})
        self.assertNotIn("fresh-b", {r["seat"] for r in rooms["side"]["seats"]})

    def test_unreadable_cursor_snapshot_falls_back_to_direct_probe(self):
        roster = self._plant(n_dead=0, fresh=("fresh-a",))
        os.makedirs(chat.chat_dir(), exist_ok=True)
        with open(chat.room_path("main"), "w", encoding="utf-8") as f:
            f.write('{"from":"someone","text":"hello","ts":"1"}\n')
        with mock.patch.object(seats, "_cursor_path_index", return_value=None), \
                mock.patch.object(seats, "room_active", return_value=True) as active:
            rooms = web._rooms_summary(roster)
        active.assert_called_once_with("main", "fresh-a")
        self.assertEqual([r["seat"] for r in rooms[0]["seats"]], ["fresh-a"])

    def test_summary_resolves_owner_identity_once(self):
        roster = self._plant(n_dead=0, fresh=())
        os.makedirs(chat.chat_dir(), exist_ok=True)
        for room in ("main", "side"):
            with open(chat.room_path(room), "w", encoding="utf-8") as f:
                f.write('{"from":"x","text":"@daria hi","ts":"1"}\n')
        with mock.patch.object(seats, "owner_names", return_value={"daria"}) as owner:
            rows = web._rooms_summary(roster)
        self.assertEqual(owner.call_count, 1)
        self.assertEqual([r["owner_mentions"] for r in rows], [1, 1])

    def _plant_cursors(self, n, room="main"):
        """n real delivery cursors, named by seats.cursor_path itself — the
        index must be measured against the filenames production writes, not a
        shape this test invented."""
        os.makedirs(chat.chat_dir(), exist_ok=True)
        for i in range(n):
            pk.write_json(seats.cursor_path(room, "seat-%04d" % i, "sess"),
                          {"active": True, "off": 0})

    def test_cursor_index_resolves_the_chat_root_once_for_any_population(self):
        """The web-poll burn (task/2879): _cursor_path_index keyed every parsed
        cursor through a bare _cursor_path_from_keys, so chat.chat_dir() —
        home.surface_origin's expanduser+realpath — ran ONCE PER CURSOR FILE.
        Measured live that was 58,362 resolutions per index and 3.35s of a
        4.27s build. The root is loop-invariant; the count must be too."""
        real = chat.chat_dir
        # POSITIVE CONTROL FIRST, and on the exact call this cure lifted out of
        # the loop: the unrooted builder still resolves, so a counter that
        # cannot move could never satisfy this.
        with mock.patch.object(chat, "chat_dir", side_effect=real) as probe:
            seats_cursor._cursor_path_from_keys("main", "seat-0000")
        self.assertEqual(probe.call_count, 1)

        self._plant_cursors(20)
        with mock.patch.object(chat, "chat_dir", side_effect=real) as small:
            small_index = seats._cursor_path_index()
        self.assertEqual(len(small_index), 20)   # the walk really saw them all
        self.assertEqual(small.call_count, 1)

        self._plant_cursors(400)
        with mock.patch.object(chat, "chat_dir", side_effect=real) as big:
            big_index = seats._cursor_path_index()
        self.assertEqual(len(big_index), 400)
        self.assertEqual(big.call_count, 1)
        # 20x the files, the SAME root resolutions — N-independence is the
        # property, not the constant.
        self.assertEqual(big.call_count, small.call_count)

    def test_cursor_index_still_keys_on_the_path_room_active_looks_up(self):
        """The correctness the speedup must not spend: the index is only
        useful because room_active indexes it with seats.cursor_path(room,
        seat). Hoisting the root changes which STRING builds the key, so the
        key must still be that exact path, and still carry the session
        cursor's own path as its value."""
        self._plant_cursors(3, room="side")
        index = seats._cursor_path_index()
        self.assertEqual(len(index), 3)
        base = seats.cursor_path("side", "seat-0001")
        self.assertIn(base, index)
        self.assertEqual(index[base],
                         [seats.cursor_path("side", "seat-0001", "sess")])
        self.assertTrue(seats.room_active("side", "seat-0001", index))


if __name__ == "__main__":
    unittest.main()
