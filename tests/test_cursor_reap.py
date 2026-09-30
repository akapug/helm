#!/usr/bin/env python3
"""Per-session read cursors must be reaped when their SESSION dies.

THE LIFETIME MISMATCH: a cursor is created per (room, seat, SESSION) but the
only reaper was keyed on the SEAT, so a seat surviving many sessions leaked one
cursor per session forever. Measured on the live fleet 2026-07-25: 24,349
cursors across 520 sessions, NINE alive — 99% garbage. It costs LATENCY, not
just disk, because list_rooms scans the directory to find 26 rooms among 25,094
entries and seats._scan_rooms calls it on every tool boundary.

The dangerous direction is over-reaping: dropping a LIVE session's cursor makes
that seat re-read its whole room and re-deliver everything it already saw. So
these pin liveness-before-age, and that an unprovable liveness keeps EVERYTHING.
"""
import fcntl
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmphome import home as _tmp_home                     # noqa: E402
_tmp_home()
from tests.test_seats import SeatsBase                           # noqa: E402
from helm import chat, gc, pk, seats                             # noqa: E402
from helm.seats_common import _seat_key, roster_path             # noqa: E402


class CursorReapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-reap-")
        self.prior = os.environ.get("HELM_CHAT_DIR")
        os.environ["HELM_CHAT_DIR"] = self.tmp

    def tearDown(self):
        if self.prior is None:
            os.environ.pop("HELM_CHAT_DIR", None)
        else:
            os.environ["HELM_CHAT_DIR"] = self.prior
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cursor(self, room, seat, sid=None, age=0, room_log=True):
        # THE ROOM IS THERE unless an arm says otherwise: a cursor whose room
        # log is gone is dead on that ground alone (task/3519), and the
        # session arms below must be judged on the session
        if room_log:
            log = chat.room_path(room)
            os.makedirs(os.path.dirname(log), exist_ok=True)
            with open(log, "a") as f:
                f.write("{}\n")
        name = "%s.cursor.%s" % (room, seat)
        if sid:
            name += ".%s" % sid
        p = os.path.join(self.tmp, name)
        with open(p, "w") as f:
            f.write("42\n")
        with open(p + ".lock", "w"):
            pass
        if age:
            t = time.time() - age
            os.utime(p, (t, t))
        return p

    # --- the dangerous direction, first -------------------------------------

    def test_a_seat_admission_cursor_is_never_a_session_gc_victim(self):
        """The suffix-less cursor is the baseline every replacement session
        inherits. Its final dotted field is a seat key, never a session id."""
        keep = self._cursor("main", "codex-aaaa")
        victims, kept, err = chat._reap_for_test(live=set(), waiters=set())
        self.assertIsNone(err)
        self.assertEqual(victims, [])
        self.assertEqual(kept, 1)
        self.assertTrue(os.path.exists(keep))
        self.assertTrue(os.path.exists(keep + ".lock"))

    def test_a_live_waiters_launch_session_survives_compaction(self):
        """The harness may now hold a new session while its armed waiter still
        consumes with the launch-time id. Process liveness retains that cursor."""
        keep = self._cursor("main", "codex-aaaa", "oldbeef")
        drop = self._cursor("main", "codex-aaaa", "dead0001")
        chat._reap_for_test(live={"newbeef"}, waiters={"oldbeef"})
        self.assertTrue(os.path.exists(keep))
        self.assertFalse(os.path.exists(drop))

    def test_production_scan_consumes_the_beacon_process_verdict(self):
        keep = self._cursor("main", "codex-aaaa", "oldbeef")
        row = {"pid": 17, "seat": "codex", "session": "oldbeef"}
        with mock.patch("helm.sessions.live_sids",
                        return_value={"newbeef": 23}), \
                mock.patch("helm.beacons.entries", return_value=[row]), \
                mock.patch("helm.beacons.holder_records", return_value={}), \
                mock.patch("helm.beacons.classify", return_value={
                    "state": "unknown", "session": "oldbeef"}):
            victims, kept, err = chat.dead_cursors()
        self.assertIsNone(err)
        self.assertEqual(victims, [])
        self.assertEqual(kept, 1)
        self.assertTrue(os.path.exists(keep))

    # --- the dangerous direction, first -------------------------------------

    def test_a_live_sessions_cursor_is_never_dropped(self):
        keep = self._cursor("main", "codex-aaaa", "1111beef")
        drop = self._cursor("main", "codex-aaaa", "dead0001")
        chat._reap_for_test(live={"1111beef-full-uuid-rest"})
        self.assertTrue(os.path.exists(keep), "dropped a LIVE session's cursor")
        self.assertFalse(os.path.exists(drop))

    def test_a_short_sid_matches_its_full_session_id_by_prefix(self):
        """A filename carries the SHORT sid; live_sids returns FULL uuids. An
        equality test would call every live session dead and reap everything."""
        keep = self._cursor("helm", "kimi-bbbb", "0fa7c4ed")
        chat._reap_for_test(live={"0fa7c4ed-9a5e-46a0-b2da-f862eb8afad6"})
        self.assertTrue(os.path.exists(keep))

    def test_unprovable_liveness_keeps_everything(self):
        """An unknown session is not a dead one. If liveness cannot be
        established the reaper must touch NOTHING and say why."""
        c = self._cursor("main", "codex-aaaa", "dead0001")
        victims, kept, err = chat._reap_for_test(live=set())
        # empty live set is still a PROVEN answer; the unprovable case is an
        # exception inside live_sids, exercised below
        self.assertTrue(len(victims) >= 1 or kept >= 1)
        import helm.sessions as S
        real = S.live_sids
        S.live_sids = lambda: (_ for _ in ()).throw(RuntimeError("no homes"))
        try:
            c2 = self._cursor("main", "codex-aaaa", "dead0002")
            victims, kept, err = chat._reap_for_test()
            self.assertEqual((victims, kept), ([], 0))
            self.assertIsNotNone(err)
            self.assertTrue(os.path.exists(c2), "reaped without proving death")
        finally:
            S.live_sids = real

    def test_a_live_sessions_LONG_FORM_cursor_is_kept(self):
        """The case the first implementation got wrong. Filenames do not agree
        on sid length — measured live: 7,480 cursors with 8 chars, 3,613 with
        23, a tail to 34. Truncating only the LIVE side and testing
        `live.startswith(sid)` can never match a 23-char sid, so a live session
        holding a long-form cursor read as DEAD. It was safe on the day only
        because no live session happened to have one. Normalise BOTH sides."""
        full = "0fa7c4ed-9a5e-46a0-b2da-f862eb8afad6"
        long_form = self._cursor("main", "codex-aaaa", full[:23])
        short = self._cursor("main", "codex-aaaa", full[:8])
        chat._reap_for_test(live={full})
        self.assertTrue(os.path.exists(long_form),
                        "reaped a LIVE session's long-form cursor")
        self.assertTrue(os.path.exists(short))

    def test_a_dotted_legacy_session_key_reads_by_the_canonical_parser(self):
        """Legacy session keys may contain dots; the canonical parser takes
        everything after the FIRST dot after the seat key as the key, dots
        included. The old last-fragment read reaped a LIVE session whose key
        carried a dot — the seat then re-read its whole room. (Latent today:
        no live cursor has a dotted key — measured 0 of 19,674.)"""
        keep = self._cursor("main", "codex-aaaa", "0fa7c4ed.9a5e")
        drop = self._cursor("main", "kimi-bbbb", "dead0001.ffff")
        chat._reap_for_test(live={"0fa7c4ed.9a5e"})
        self.assertTrue(os.path.exists(keep),
                        "reaped a LIVE session's dotted-key cursor")
        self.assertFalse(os.path.exists(drop),
                         "kept a dead session's dotted-key cursor")

    # --- the ordinary direction ---------------------------------------------

    def test_a_dead_cursor_goes_and_its_lock_file_stays(self):
        """task/2520: THE REAPER NEVER UNLINKS A LOCK FILE. A lock is an
        flock handle: unlinking one a writer still holds detaches the name,
        and the next writer takes a fresh inode the first cannot see."""
        c = self._cursor("main", "codex-aaaa", "dead0001")
        chat._reap_for_test(live={"9999ffff-x"})
        self.assertFalse(os.path.exists(c))
        self.assertTrue(os.path.exists(c + ".lock"),
                        "the reaper unlinked a lock file")

    def test_identifying_never_deletes(self):
        """dead_cursors() only NAMES victims — `helm gc` does the deleting, via
        the same _reap every other retention stream uses. Two actuators for one
        kind of file is how you get two policies and a divergence nobody sees."""
        c = self._cursor("main", "codex-aaaa", "dead0001")
        victims, _kept, _e = chat.dead_cursors(live={"9999ffff-x"})
        self.assertEqual(victims, [c])      # the cursor, never its lock
        self.assertTrue(os.path.exists(c), "identifying must not delete")

    def test_the_re_proof_never_passes_a_lock_file(self):
        """The unlink asks `still_dead_cursor` again, so the finder's law has
        to hold there too (task/2520): a dead session cursor re-proves, and
        its `.lock` sibling never does, whoever names it."""
        c = self._cursor("main", "codex-aaaa", "dead0001")
        still = chat.still_dead_cursor(live={"9999ffff-x"})
        # the cursor re-proves (the re-proof is live, not a dead instrument),
        # and the lock it answers False for is really on disk
        self.assertTrue(still(c), "a dead session cursor did not re-prove")
        self.assertTrue(os.path.isfile(c + ".lock"))
        self.assertFalse(still(c + ".lock"), "the re-proof passed a lock")

    def test_non_cursor_files_are_never_touched(self):
        room = os.path.join(self.tmp, "main.jsonl")
        with open(room, "w") as f:
            f.write("{}\n")
        seen = os.path.join(self.tmp, ".seen.codex-aaaa")
        with open(seen, "w"):
            pass
        chat._reap_for_test(live=set())
        self.assertTrue(os.path.exists(room), "reaped a ROOM")
        self.assertTrue(os.path.exists(seen))

    def test_reaping_shrinks_what_list_rooms_must_scan(self):
        """The point of the whole exercise: list_rooms finds rooms by scanning
        the directory, so dead cursors are a tax on every tool boundary."""
        with open(os.path.join(self.tmp, "main.jsonl"), "w") as f:
            f.write("{}\n")
        for i in range(200):
            self._cursor("main", "codex-aaaa", "dead%04d" % i)
        cursors = lambda: [n for n in os.listdir(self.tmp)
                           if not n.endswith(".lock")]
        before = len(cursors())
        self.assertGreater(before, 200)   # 200 cursors and the room
        chat._reap_for_test(live=set())
        after = len(cursors())
        self.assertLess(after, before / 10)
        self.assertEqual(chat.list_rooms(), ["main"])

    # --- (task/3519) a cursor whose ROOM is gone ----------------------------
    # Measured on the live bus: 1,324 cursors pointed at rooms whose log no
    # longer existed, and dead_cursors kept every one — 16,142 cursors there
    # are seat-level (no session suffix, so the session rule can never reap
    # them) and the rest matched live sessions.

    OLD = 2 * 86400

    def _roster(self, rows):
        with open(roster_path(), "w") as f:
            json.dump(rows, f)

    def _log(self, room):
        log = chat.room_path(room)
        os.makedirs(os.path.dirname(log), exist_ok=True)
        with open(log, "a") as f:
            f.write("{}\n")
        return log

    def test_a_roomless_cursor_is_reaped_only_for_a_seat_that_is_over(self):
        """A LIVE SEAT'S CURSOR IS NEVER REAPED FOR A MISSING ROOM. A seat
        that joins while its home room has no log holds pre-log baselines
        there, and reaping them made the room's first post undeliverable. So
        a missing room reaps only what nothing runs: a seat-level cursor of a
        seat the roster no longer holds. A live session keeps its cursor; a
        dead one is the session rule's, room or no room."""
        live = "1111beef-full-uuid-rest"
        here, over = _seat_key("alice"), _seat_key("retired")
        self._roster({"alice": {"session": live, "sessions": [live]}})
        keep_seat = self._cursor("gone", here, age=self.OLD, room_log=False)
        keep_live = self._cursor("gone", here, "1111beef", age=self.OLD,
                                 room_log=False)
        twin = os.path.join(self.tmp, ".beacon-gone.cursor.%s" % here)
        with open(twin, "w") as f:
            f.write("42\n")
        t = time.time() - self.OLD
        os.utime(twin, (t, t))
        drop = self._cursor("gone", over, age=self.OLD, room_log=False)
        victims, kept, err = chat.dead_cursors(live={live})
        self.assertIsNone(err)
        self.assertEqual(victims, [drop], "must-hit: the over seat's cursor")
        self.assertEqual(kept, 3)
        for p in (keep_seat, keep_live, twin, drop):
            self.assertTrue(os.path.exists(p), "identifying never deletes")

    def test_an_unreadable_roster_keeps_every_roomless_cursor(self):
        """Which seats are over is the roster's answer, and no answer is not
        "none of them". The session rule does not ask it and still runs."""
        with open(roster_path(), "w") as f:
            f.write("{not json")
        c = self._cursor("gone", _seat_key("retired"), age=self.OLD,
                         room_log=False)
        dead = self._cursor("main", _seat_key("retired"), "dead0001")
        victims, kept, err = chat.dead_cursors(live=set())
        self.assertIsNone(err)
        self.assertNotIn(c, victims)
        self.assertIn(dead, victims, "must-hit: the session rule still runs")
        self.assertEqual(kept, 1)

    def test_a_roomless_cursors_lock_sibling_is_left_alone(self):
        """task/2520: unlinking a lock another writer may still hold hands the
        next opener a fresh inode. The roomless rule does not widen that."""
        c = self._cursor("gone", "codex-aaaa", age=self.OLD, room_log=False)
        victims, _kept, _err = chat.dead_cursors(live=set())
        self.assertIn(c, victims)
        self.assertNotIn(c + ".lock", victims)
        self.assertTrue(os.path.exists(c + ".lock"))

    def test_a_60_char_key_cut_at_a_dash_names_its_own_room(self):
        """pk.slug strips before it truncates, so re-slugging a key that ends
        in "-" at the 60-char cut is not the key: it named another, absent
        log, and a cursor on a room that exists read as roomless."""
        room = "a" * 59 + "-bbbb"
        key = pk.slug(room)
        self.assertEqual((len(key), key[-1]), (60, "-"))
        log = self._log(room)
        self.assertEqual(log, os.path.join(self.tmp, key + ".jsonl"))
        keep = self._cursor(key, "codex-aaaa", age=self.OLD, room_log=False)
        victims, kept, err = chat.dead_cursors(live=set())
        self.assertIsNone(err)
        self.assertNotIn(keep, victims)
        self.assertEqual(kept, 1)

    def test_a_dm_lane_cut_at_a_dash_names_its_own_log(self):
        """The same cut in the dm/ namespace: a lane whose slug is cut at a
        "-" logs to dm/<key minus dm->.jsonl."""
        room = chat.DM_PREFIX + "a" * 56 + "-xyz"
        key = pk.slug(room)
        self.assertEqual((len(key), key[-1]), (60, "-"))
        log = self._log(room)
        self.assertEqual(log, os.path.join(
            self.tmp, "dm", key[len(chat.DM_PREFIX):] + ".jsonl"))
        keep = self._cursor(key, "codex-aaaa", age=self.OLD, room_log=False)
        victims, kept, err = chat.dead_cursors(live=set())
        self.assertIsNone(err)
        self.assertNotIn(keep, victims)
        self.assertEqual(kept, 1)

    def test_a_fresh_cursor_on_a_missing_room_is_kept(self):
        """A cursor minted a moment before its room's first append is not an
        orphan. The grace is what keeps that race from reaping a baseline."""
        c = self._cursor("soon", "codex-aaaa", age=0, room_log=False)
        victims, kept, err = chat.dead_cursors(live=set())
        self.assertIsNone(err)
        self.assertNotIn(c, victims)
        self.assertEqual(kept, 1)

    def test_a_dm_lane_cursor_finds_its_log_in_the_dm_subdir(self):
        """`dm-<lane>` logs live in dm/, not beside the cursor. Reading the
        flat name would call every DM lane gone."""
        keep = self._cursor("dm-alice-abcd", "codex-aaaa", age=self.OLD)
        self.assertTrue(os.path.exists(
            os.path.join(self.tmp, "dm", "alice-abcd.jsonl")))
        victims, kept, err = chat.dead_cursors(live=set())
        self.assertIsNone(err)
        self.assertNotIn(keep, victims)
        self.assertEqual(kept, 1)

    def test_a_room_log_is_never_a_victim(self):
        c = self._cursor("gone", "codex-aaaa", age=self.OLD, room_log=False)
        self._cursor("main", "codex-aaaa", "dead0001", age=self.OLD)
        victims, _kept, _err = chat._reap_for_test(live=set())
        self.assertIn(c, victims, "must-hit: the roomless cursor was kept")
        self.assertFalse([v for v in victims if v.endswith(".jsonl")])
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "main.jsonl")))

    # --- the gc row is owner-bound: lock, then re-prove, then unlink --------

    def _gc_row(self):
        p = next(p for p in gc.POLICIES if p["stream"] == "chat-cursors")
        found = p["find"]()
        return {"stream": p["stream"], "act": p["act"], "policy": p,
                "victims": found, "now": time.time(),
                "claims": {path: gc._identity(path) for path in found}}

    def _no_sessions(self):
        stack = mock.patch("helm.sessions.live_sids", return_value=set())
        waiters = mock.patch("helm.chat._waiter_cursor_sessions",
                             return_value=set())
        stack.start()
        waiters.start()
        self.addCleanup(stack.stop)
        self.addCleanup(waiters.stop)

    def test_gc_re_proves_a_roomless_cursor_under_the_cursor_lock(self):
        """The scan and the unlink are two moments. A seat that is rostered
        between them owns the cursor again, so the unlink re-proves it."""
        self._no_sessions()
        c = self._cursor("gone", _seat_key("alice"), age=self.OLD,
                         room_log=False)
        row = self._gc_row()
        self.assertIn(c, row["victims"], "must-hit: nominated at the scan")
        self._roster({"alice": {"session": None, "sessions": []}})
        lines, items, _bytes = gc._reap(row)
        self.assertTrue(os.path.exists(c), "reaped a rostered seat's cursor")
        self.assertEqual(items, 0)
        self.assertTrue(any("SKIPPED " + c in line for line in lines), lines)

    def test_a_scan_and_reap_leave_no_state_in_the_gc_module(self):
        """The re-proof a scan arms belongs to that scan's row. Parked in a
        module global, it outlived every gc run and every test that scanned,
        which the land gate's fail-mode leak audit refused (train444)."""
        self._no_sessions()
        c = self._cursor("main", "codex-aaaa", "dead0001")

        def state():
            return {k: repr(v) for k, v in vars(gc).items()
                    if isinstance(v, (list, dict, set))}
        before = state()
        row = self._gc_row()
        self.assertIn(c, row["victims"], "must-hit: nominated at the scan")
        self.assertEqual(state(), before, "the scan parked state in gc")
        gc._reap(row)
        self.assertFalse(os.path.exists(c), "must-hit: the row's own re-proof "
                         "still reaps the dead cursor")
        self.assertEqual(state(), before, "the reap parked state in gc")

    def test_gc_skips_a_cursor_while_the_cursor_topology_lock_is_held(self):
        """A cursor initializer holds the topology lock while it mints; the
        reap takes the same lock, so it never unlinks under a writer."""
        self._no_sessions()
        c = self._cursor("main", "codex-aaaa", "dead0001")
        row = self._gc_row()
        self.assertIn(c, row["victims"], "must-hit: nominated at the scan")
        with open(os.path.join(self.tmp, ".cursor-topology.lock"), "a") as lk:
            fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
            _lines, items, _b = gc._reap(row)
        self.assertTrue(os.path.exists(c), "unlinked under a held lock")
        self.assertEqual(items, 0)
        _lines, items, _b = gc._reap(self._gc_row())
        self.assertFalse(os.path.exists(c), "must-hit: reaped once free")
        self.assertGreaterEqual(items, 1)


class RoomlessDeliveryTest(SeatsBase):
    """The review's repro: a seat joins while its home room has no log, its
    four pre-log baselines sit an hour, the reaper runs, and the first post
    then creates the room. That post must be delivered."""

    ROOM = "lab"
    SID = "sess-1234"

    def _join(self):
        seats.join(session=self.SID, cwd="/tmp/p", seat="alice",
                   room=self.ROOM, room_explicit=True)
        self.assertFalse(os.path.exists(chat.room_path(self.ROOM)))

    def _cursors(self):
        d = chat.chat_dir()
        return [os.path.join(d, n) for n in sorted(os.listdir(d))
                if ".cursor." in n and not n.endswith((".lock", ".tmp"))
                and pk.slug(self.ROOM) + ".cursor." in n]

    def _age(self, seconds):
        """Every lab cursor and the seat's join, `seconds` in the past."""
        t = time.time() - seconds
        for p in self._cursors():
            os.utime(p, (t, t))
        with open(roster_path()) as f:
            rows = json.load(f)
        rows["alice"]["joined"] = pk.epoch_ts(t)
        with open(roster_path(), "w") as f:
            json.dump(rows, f)

    def test_the_first_post_after_the_reaper_ran_is_delivered(self):
        self._join()
        self.assertEqual(len(self._cursors()), 4)
        self._age(2 * chat.ROOMLESS_CURSOR_S)
        chat._reap_for_test(live={self.SID})
        chat.post("@alice the first post in this room", who="bob",
                  room=self.ROOM)
        line = seats.deliver(session=self.SID, seat="alice", room=self.ROOM)
        self.assertIn("the first post in this room", line or "")

    def test_a_missing_primary_cursor_on_a_room_born_after_the_join_reads_from_0(self):
        """INDEPENDENT OF THE REAPER: however the baselines went, a room log
        born after the seat joined holds only post-join news, so the missing
        cursor starts at 0 instead of taking the EOF self-heal."""
        self._join()
        self._age(2 * chat.ROOMLESS_CURSOR_S)
        for p in self._cursors():
            os.unlink(p)
        chat.post("@alice the first post in this room", who="bob",
                  room=self.ROOM)
        line = seats.deliver(session=self.SID, seat="alice", room=self.ROOM)
        self.assertIn("the first post in this room", line or "")

    def test_a_missing_primary_cursor_on_an_older_room_never_floods(self):  # noqa: VACUOUS_ASSERTION — the EOF self-heal is the contract for pre-join backlog
        """The control: a room with history from before the join keeps the
        EOF self-heal, so pre-join backlog is never replayed."""
        chat.post("@alice history from before the join", who="old",
                  room=self.ROOM)
        seats.join(session=self.SID, cwd="/tmp/p", seat="alice",
                   room=self.ROOM, room_explicit=True)
        for p in self._cursors():
            os.unlink(p)
        line = seats.deliver(session=self.SID, seat="alice", room=self.ROOM)
        self.assertNotIn("history", line or "")


if __name__ == "__main__":
    unittest.main()
