#!/usr/bin/env python3
"""The chat directory holds rooms, not debris (task/3017).

The delivery hook lists the flat chat directory on every tool call, so its
cost is every ENTRY, not every room. Measured on the live bus: 108,177 entries
for 468 rooms — 49,718 per-cursor `.lock` siblings nothing has opened since
the cursor stack took one lock per room, 53,830 cursors (a pair per consumer
per listed room), and 404 meld rooms idle for over a week that a reboot's
restore kept bringing back with a fresh pair for every rostered seat.

These arms pin the four cures on a temp HELM_HOME and a temp chat dir only:
gc's `chat-cursor-locks` row takes every sibling nobody holds;
`chat-unpaired-cursors` takes the cursors a live session left under a seat it
no longer runs; `helm chat retire-rooms` archives idle meld rooms off the bus
and the restore honours it; `helm doctor` warns past the entry budget.

THE ARMS THAT DRIVE AN EXISTING SURFACE (gc, the restore, meld replay, the
doctor) import the new module lazily or not at all, so on the base tree each
one fails on the behaviour it pins, never on an import.
"""
import contextlib
import fcntl
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-chatdebris-", var="HELM_HOME")

from helm import chat, doctor, gc, meld, pk  # noqa: E402
from helm.seats_common import _seat_key, roster_path  # noqa: E402

ENV_KEYS = ("HELM_HOME", "HELM_CHAT_DIR", "HELM_CHAT_NODE_URL",
            "HELM_CHAT_NAME", "HELM_SCRATCH_GC", "HELM_CACHE_DIR",
            "HELM_ADOPTED_DIR", "HELM_CHAT_LOG", "MELD_CACHE_DIR")
LIVE = "0fa7c4ed-9a5e-46a0-b2da-f862eb8afad6"
MELD = "meld-1790000000-topic"
OLD = 30 * 86400


def run(fn, args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = fn(args)
    return rc, out.getvalue(), err.getvalue()


def row(rows, stream):
    return next(r for r in rows if r["stream"] == stream)


class DebrisBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-chatdebris-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_NAME"] = "tester"
        os.environ["HELM_SCRATCH_GC"] = "0"
        os.environ["HELM_CACHE_DIR"] = os.path.join(self.tmp, "cache")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        for d in ("chat", "cache", "adopted"):
            os.makedirs(os.path.join(self.tmp, d))
        self.chat = os.environ["HELM_CHAT_DIR"]
        self.cwd_prior = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.cwd_prior)
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def plant(self, name, body="{}\n", age=0):
        path = os.path.join(self.chat, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        if age:
            t = time.time() - age
            os.utime(path, (t, t))
        return path

    def cursor(self, room, seat, sid=None, beacon=False, lock=False):
        name = "%s%s.cursor.%s" % (".beacon-" if beacon else "", room,
                                   _seat_key(seat))
        if sid:
            name += "." + sid
        path = self.plant(name, '{"off": 0}\n')
        if lock:
            self.plant(name + ".lock", "")
        return path

    def scan(self, stream):
        with mock.patch("helm.sessions.live_sids",
                        return_value={LIVE: 4242}), \
                mock.patch("helm.beacons.entries", return_value=[]):
            return row(gc.scan(), stream)

    def gc_apply(self):
        with mock.patch("helm.sessions.live_sids",
                        return_value={LIVE: 4242}), \
                mock.patch("helm.beacons.entries", return_value=[]):
            return run(gc.cmd_gc, ["--apply"])


# ── (a) the per-cursor sibling locks ────────────────────────────────────────


class SiblingLockTest(DebrisBase):
    STANDALONE = (".cursor-topology.lock", ".cursor-room.main.lock",
                  ".cursor-estate.alice-x.lock", ".cursor-txn-room.main.lock",
                  ".roster.json.lock", "main.lock")

    def test_every_sibling_is_nominated_whether_its_cursor_is_there_or_not(self):  # noqa: VACUOUS_ASSERTION — the exact victim set asserted first is the positive control
        present = self.cursor("main", "alice", LIVE[:8], lock=True)
        gone = self.cursor("main", "bob", "dead0001", lock=True)
        os.remove(gone)
        victims = self.scan("chat-cursor-locks")["victims"]
        self.assertEqual(sorted(victims), sorted([present + ".lock",
                                                  gone + ".lock"]))
        self.assertNotIn(present, victims, "a cursor is never this row's")

    def test_standalone_locks_are_never_nominated(self):
        """Every name here is a lock a live process holds by design; deleting
        one is the two-inode bug. The present-cursor sibling is the must-hit:
        it proves the scan read this directory and took this class."""
        planted = [self.plant(n, "") for n in self.STANDALONE]
        sibling = self.cursor("main", "alice", LIVE[:8], lock=True) + ".lock"
        victims = self.scan("chat-cursor-locks")["victims"]
        self.assertEqual(victims, [sibling])
        for path in planted:
            self.assertTrue(os.path.exists(path), path)

    def test_a_lock_named_in_proc_locks_is_never_nominated(self):
        """The lock table is injected: a filesystem's stat device and the
        device the kernel prints in /proc/locks agree on tmpfs, where the bus
        lives, but not on every filesystem a test tree can sit on."""
        held = self.cursor("main", "alice", LIVE[:8], lock=True) + ".lock"
        free = self.cursor("main", "bob", LIVE[:8], lock=True) + ".lock"
        st = os.stat(held)
        with mock.patch("helm.chatdebris.held_inodes",
                        return_value={(st.st_dev, st.st_ino)}):
            victims = self.scan("chat-cursor-locks")["victims"]
        self.assertEqual(victims, [free], "a HELD lock was nominated")

    def test_the_kernel_lock_table_is_read_by_shape(self):
        """MAJOR:MINOR in hex, the inode in decimal; a blocked waiter's line
        carries an extra `->` token before the same fields."""
        from helm import chatdebris
        table = os.path.join(self.tmp, "locks")
        with open(table, "w") as f:
            f.write("1: FLOCK  ADVISORY  WRITE 7072 00:1b:11 0 EOF\n"
                    "1: -> FLOCK  ADVISORY  WRITE 7080 00:1b:11 0 EOF\n"
                    "2: POSIX  ADVISORY  WRITE 82026 103:05:53625209 0 EOF\n")
        self.assertEqual(chatdebris.held_inodes(table),
                         {(os.makedev(0, 0x1b), 11),
                          (os.makedev(0x103, 5), 53625209)})
        self.assertIsNone(chatdebris.held_inodes(table + ".missing"))

    def test_an_unreadable_lock_table_is_an_error_row_not_an_empty_one(self):
        self.cursor("main", "alice", LIVE[:8], lock=True)
        with mock.patch("helm.chatdebris.held_inodes", return_value=None):
            r = self.scan("chat-cursor-locks")
        self.assertIn("/proc/locks", r.get("error") or "")
        self.assertEqual(r["victims"], [])

    def test_a_lock_taken_after_the_scan_is_skipped_by_the_probe(self):
        """The window /proc/locks cannot cover: the scan saw it free, then a
        process took it. `_reap` must flock the victim itself and refuse."""
        taken = self.cursor("main", "alice", "dead0001", lock=True) + ".lock"
        free = self.cursor("main", "bob", "dead0002", lock=True) + ".lock"
        r = self.scan("chat-cursor-locks")
        self.assertEqual(sorted(r["victims"]), sorted([taken, free]))
        holder = open(taken, "a")
        try:
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
            lines, items, _n = gc._reap(r)
        finally:
            holder.close()
        self.assertTrue(os.path.exists(taken), "unlinked a HELD lock")
        self.assertFalse(os.path.exists(free), "must-hit: the free one stayed")
        self.assertEqual(items, 1)
        self.assertTrue(any(l.startswith("SKIPPED " + taken) for l in lines))

    def test_the_reap_never_recreates_a_victim_that_is_already_gone(self):  # noqa: VACUOUS_ASSERTION — the nomination of this exact victim is asserted before the reap
        """`_reap` opened the victim-as-lock with "a+", which CREATES it: a
        lock another row reaped between scan and reap came back as a fresh
        empty file, reported SKIPPED."""
        cursor = self.cursor("main", "alice", "dead0001", lock=True)
        os.remove(cursor)
        r = self.scan("chat-cursor-locks")
        self.assertEqual(r["victims"], [cursor + ".lock"])
        os.remove(cursor + ".lock")
        gc._reap(r)
        self.assertFalse(os.path.exists(cursor + ".lock"),
                         "the reap re-created a victim that was gone")

    def test_the_reap_does_not_list_the_directory_once_per_victim(self):  # noqa: VACUOUS_ASSERTION — items == 30 proves the reap ran on every victim
        """The row's finder is a listing of the whole chat dir; asking it
        again for every victim is quadratic in the directory it shrinks —
        49,718 victims times a 108,177-entry listing on the live bus."""
        for i in range(30):
            os.remove(self.cursor("main", "alice", "dead%04d" % i, lock=True))
        r = self.scan("chat-cursor-locks")
        self.assertEqual(len(r["victims"]), 30)
        real = os.listdir
        calls = []

        def counting(path="."):
            if os.path.abspath(str(path)) == os.path.abspath(self.chat):
                calls.append(path)
            return real(path)
        with mock.patch("os.listdir", counting):
            _lines, items, _n = gc._reap(r)
        self.assertEqual(items, 30)
        self.assertLess(len(calls), 3, "listed the chat dir %d times for 30 "
                                       "victims" % len(calls))


# ── (b) a live session its seat no longer runs ──────────────────────────────


class UnpairedCursorTest(DebrisBase):
    def roster(self, rows):
        pk.write_json(roster_path(), rows)

    def moved(self, room="main"):
        """alice's row remembers no session; bob's holds LIVE. alice still has
        LIVE's pair in `room` beside her own seat-level baseline."""
        self.roster({"alice": {"sessions": []},
                     "bob": {"session": LIVE, "sessions": [LIVE]}})
        keep = [self.cursor(room, "alice"),
                self.cursor(room, "alice", beacon=True)]
        drop = [self.cursor(room, "alice", LIVE[:8]),
                self.cursor(room, "alice", LIVE[:8], beacon=True)]
        return keep, drop

    def test_a_live_session_that_left_its_seat_is_nominated(self):  # noqa: VACUOUS_ASSERTION — the exact victim set is the positive control
        keep, drop = self.moved()
        victims = self.scan("chat-unpaired-cursors")["victims"]
        self.assertEqual(sorted(victims), sorted(drop))
        for path in keep:
            self.assertNotIn(path, victims, "the seat's own baseline")

    def test_every_keep_condition_keeps_the_pair(self):  # noqa: VACUOUS_ASSERTION — each case re-plants the shape and asserts the exact nominated set
        """Each case removes exactly one condition from the nominated shape.
        The control inside every case re-plants that shape and scans again:
        the same pair must then be nominated, so the keep is the condition
        and never a scan that read nothing."""
        cases = {
            "the seat still remembers the session": lambda: self.roster(
                {"alice": {"sessions": [LIVE]},
                 "bob": {"session": LIVE, "sessions": [LIVE]}}),
            "no seat claims the session": lambda: self.roster(
                {"alice": {"sessions": []}, "bob": {"sessions": []}}),
            "the seat has no baseline in the room": lambda: [
                os.remove(os.path.join(self.chat, n))
                for n in os.listdir(self.chat)
                if n.endswith(".cursor." + _seat_key("alice"))],
        }
        for why, mutate in cases.items():
            with self.subTest(why=why):
                for n in os.listdir(self.chat):
                    os.remove(os.path.join(self.chat, n))
                _keep, drop = self.moved()
                mutate()
                victims = self.scan("chat-unpaired-cursors")["victims"]
                self.assertEqual(victims, [], why)
                self.moved()                          # the positive control
                victims = self.scan("chat-unpaired-cursors")["victims"]
                self.assertEqual(sorted(victims), sorted(drop), why)

    def test_a_live_waiter_running_the_pair_keeps_it(self):  # noqa: VACUOUS_ASSERTION — the second call nominates the exact pair
        from helm import chatdebris
        _keep, drop = self.moved()
        rows = {"alice": {"sessions": []}, "bob": {"sessions": [LIVE]}}
        live = {LIVE[:8]}
        got = chatdebris.unpaired_session_cursors(
            liveness=(live, {(_seat_key("alice"), LIVE[:8])}, set()),
            rows=rows)
        self.assertEqual(got, [])
        got = chatdebris.unpaired_session_cursors(
            liveness=(live, set(), set()), rows=rows)
        self.assertEqual(sorted(got), sorted(drop), "must-hit")

    def test_the_seats_own_dm_lane_is_never_nominated(self):  # noqa: VACUOUS_ASSERTION — the other room's exact pair is nominated in the same scan
        lane = pk.slug(chat.DM_PREFIX + _seat_key("alice"))
        _keep, drop = self.moved(lane)
        _k2, other = self.moved("main")
        victims = self.scan("chat-unpaired-cursors")["victims"]
        self.assertEqual(sorted(victims), sorted(other))
        for path in drop:
            self.assertNotIn(path, victims)

    def test_a_dead_session_is_left_to_the_dead_cursor_row(self):
        self.roster({"alice": {"sessions": []},
                     "bob": {"session": "dead0001-x", "sessions": []}})
        self.cursor("main", "alice")
        dead = self.cursor("main", "alice", "dead0001")
        self.assertEqual(self.scan("chat-unpaired-cursors")["victims"], [])
        self.assertIn(dead, self.scan("chat-cursors")["victims"], "must-hit")

    def test_an_unreadable_roster_is_an_error_row(self):
        self.moved()
        with open(roster_path(), "w") as f:
            f.write("{not json")
        r = self.scan("chat-unpaired-cursors")
        self.assertIn("roster", r.get("error") or "")
        self.assertEqual(r["victims"], [])

    def test_apply_removes_the_pair_and_keeps_the_baseline(self):  # noqa: VACUOUS_ASSERTION — the kept baseline is asserted present in the same pass
        keep, drop = self.moved()
        rc, _out, _err = self.gc_apply()
        self.assertEqual(rc, 0)
        for path in drop:
            self.assertFalse(os.path.exists(path), path)
        for path in keep:
            self.assertTrue(os.path.exists(path), path)


# ── (c) idle meld rooms leave the bus ───────────────────────────────────────


class RetireRoomsTest(DebrisBase):
    def meld_room(self, room=MELD, age=OLD):
        chat.post("the converged spec", room=room, who="alice")
        path = chat.room_path(room)
        snap = self.plant("%s.meld.%s.json" % (room, _seat_key("alice")),
                          '{"status": "done"}\n')
        cursors = [self.cursor(room, "alice"),
                   self.cursor(room, "alice", beacon=True),
                   self.cursor(room, "bob", LIVE[:8])]
        t = time.time() - age
        os.utime(path, (t, t))
        return path, snap, cursors

    def retire(self, *args):
        return run(chat.cmd_chat, ["retire-rooms"] + list(args))

    def test_apply_archives_first_then_takes_the_room_and_its_cursors(self):  # noqa: VACUOUS_ASSERTION — the archive's bytes and index entry are asserted equal
        path, snap, cursors = self.meld_room()
        with open(path, "rb") as f:
            raw = f.read()
        rc, out, err = self.retire("--apply")
        self.assertEqual(rc, 0, err)
        self.assertIn("retired  " + MELD, out)
        self.assertFalse(os.path.exists(path), "the room is still on the bus")
        self.assertFalse(os.path.exists(snap))
        for c in cursors:
            self.assertFalse(os.path.exists(c), c)
        arch = os.path.join(chat.journal_dir(), "retired-rooms", MELD)
        with open(os.path.join(arch, MELD + ".jsonl"), "rb") as f:
            self.assertEqual(f.read(), raw, "the archive is not the room")
        self.assertTrue(os.path.exists(os.path.join(arch,
                                                    os.path.basename(snap))))
        index = pk.read_json(os.path.join(chat.journal_dir(),
                                          "retired-rooms.json"), {})
        through = index["rooms"][MELD]["through"]
        self.assertEqual(through, json.loads(raw.splitlines()[-1])["ts"])
        self.assertNotIn(MELD, chat.list_rooms())

    def test_dry_run_nominates_and_touches_nothing(self):  # noqa: VACUOUS_ASSERTION — the nomination line is asserted in the output
        path, snap, cursors = self.meld_room()
        rc, out, _err = self.retire()
        self.assertEqual(rc, 0)
        self.assertIn("would retire  " + MELD, out)
        for p in [path, snap] + cursors:
            self.assertTrue(os.path.exists(p), "dry-run touched " + p)
        self.assertFalse(os.path.exists(os.path.join(
            chat.journal_dir(), "retired-rooms.json")))

    def test_a_room_posted_to_within_the_bound_is_kept(self):  # noqa: VACUOUS_ASSERTION — the idle room beside it is asserted retired
        fresh, _s, _c = self.meld_room("meld-1790000001-fresh", age=86400)
        old, _s2, _c2 = self.meld_room()
        rc, out, _err = self.retire("--apply")
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(fresh), "retired a room in use")
        self.assertFalse(os.path.exists(old), "must-hit: the idle room stayed")
        self.assertNotIn("meld-1790000001-fresh", out)

    def test_only_meld_rooms_are_ever_retired(self):
        chat.post("an old ordinary room", room="lobby", who="alice")
        lobby = chat.room_path("lobby")
        t = time.time() - OLD
        os.utime(lobby, (t, t))
        meld_path, _s, _c = self.meld_room()
        self.retire("--apply")
        self.assertTrue(os.path.exists(lobby), "retired a non-meld room")
        self.assertFalse(os.path.exists(meld_path), "must-hit")

    def test_a_post_after_the_scan_keeps_the_room(self):
        """Idleness is re-proven under the room lock: the listing and the
        retirement are two moments, and a post can land between them."""
        from helm import chatdebris
        path, _s, cursors = self.meld_room()
        self.assertEqual([r for r, _ in chatdebris.retirable_rooms()], [MELD])
        chat.post("one more thing", room=MELD, who="bob")
        out, why = chatdebris.retire_room(MELD)
        self.assertIsNone(out)
        self.assertIn("posted", why)
        self.assertTrue(os.path.exists(path))
        for c in cursors:
            self.assertTrue(os.path.exists(c))

    def test_idle_days_must_be_a_number_of_days(self):
        self.meld_room()
        for bad in (["--idle-days", "soon"], ["--idle-days", "0"],
                    ["--idle-days"], ["--bogus"]):
            with self.subTest(args=bad):
                rc, _out, _err = self.retire(*bad)
                self.assertEqual(rc, 2)
        rc, out, _err = self.retire("--idle-days", "60")
        self.assertEqual(rc, 0)
        self.assertIn("no meld room has been idle 60 days", out)

    def test_retiring_shrinks_what_every_listing_reads(self):  # noqa: VACUOUS_ASSERTION — rc 0 and a bounded drop of more than forty entries
        """Counted without the STANDALONE locks: the retirement takes each
        seat's estate lock, and on a fresh fixture that first take is what
        creates them. On a live bus every seat's estate lock already exists
        (one per seat, never per room), so they are not what a listing of
        the directory grows by."""
        self.meld_room()
        for i in range(40):
            self.cursor(MELD, "seat%02d" % i)

        def entries():
            return [n for n in os.listdir(self.chat)
                    if not n.endswith(".lock")]
        before = len(entries())
        rc, _out, err = self.retire("--apply")
        self.assertEqual(rc, 0, err)
        self.assertLess(len(entries()), before - 40)


# ── the restore honours a retirement ────────────────────────────────────────


class RestoreHonoursRetirementTest(DebrisBase):
    """Written against the index FILE, the restore's contract input, so each
    arm is red on a tree whose restore ignores it."""

    def journal(self, lines):
        os.makedirs(chat.journal_dir(), exist_ok=True)
        with open(os.path.join(chat.journal_dir(), "chat-2026-01-01.log"),
                  "a", encoding="utf-8") as f:
            f.write("".join(line + "\n" for line in lines))

    def retired(self, room, through):
        pk.write_json(os.path.join(chat.journal_dir(), "retired-rooms.json"),
                      {"v": 1, "rooms": {room: {"through": through}}})

    def texts(self, room):
        return [r.get("text") for r in chat.read(room)[0]
                if r.get("from") != "journal"]

    def test_a_reboot_does_not_resurrect_a_retired_room(self):  # noqa: VACUOUS_ASSERTION — main's restored rows are asserted first
        self.journal(["2026-01-01T00:00:00Z [%s] alice: old spec" % MELD,
                      "2026-01-01T00:00:01Z [main] alice: still here"])
        self.retired(MELD, "2026-01-01T00:00:00Z")
        chat.restore_journal(apply=True)
        self.assertEqual(self.texts("main"), ["still here"], "must-hit")
        self.assertFalse(os.path.exists(chat.room_path(MELD)),
                         "the restore resurrected a retired room")
        self.assertEqual([n for n in os.listdir(self.chat)
                          if MELD in n and ".cursor." in n], [],
                         "the restore minted cursors in a retired room")

    def test_a_row_posted_after_the_retirement_still_restores(self):
        self.journal(["2026-01-01T00:00:00Z [%s] alice: old spec" % MELD,
                      "2026-02-01T00:00:00Z [%s] bob: reborn" % MELD])
        self.retired(MELD, "2026-01-01T00:00:00Z")
        chat.restore_journal(apply=True)
        self.assertEqual(self.texts(MELD), ["reborn"])

    def test_the_first_listing_after_a_reboot_skips_the_retired_room(self):
        self.journal(["2026-01-01T00:00:00Z [%s] alice: old spec" % MELD,
                      "2026-01-01T00:00:01Z [main] alice: still here"])
        self.retired(MELD, "2026-01-01T00:00:00Z")
        rooms = chat.list_rooms()           # the auto-restore latch fires here
        self.assertIn("main", rooms, "must-hit: the latch did not restore")
        self.assertNotIn(MELD, rooms)

    def test_meld_replay_skips_a_retired_room(self):
        self.retired(MELD, "2026-01-01T00:00:00Z")
        other = "meld-1790000002-live"
        with mock.patch.object(meld, "_lifecycle_rooms",
                               return_value=([MELD, other], None)), \
                mock.patch.object(meld, "replay_room",
                                  return_value={"state": "ok"}) as replay:
            meld.replay_durable(apply=True)
        self.assertEqual([c.args[0] for c in replay.call_args_list], [other])


# ── the doctor rung ─────────────────────────────────────────────────────────


class DoctorRungTest(DebrisBase):
    def test_it_is_registered(self):
        self.assertIn("check_chat_dir_debris", doctor.CHECKS)

    def test_over_budget_warns_and_names_both_commands(self):
        chat.post("spec", room=MELD, who="alice")
        t = time.time() - OLD
        os.utime(chat.room_path(MELD), (t, t))
        for i in range(260):
            self.cursor(MELD, "seat%03d" % i, lock=True)
        [(level, line)] = doctor.check_chat_dir_debris()
        self.assertEqual(level, doctor.WARN)
        self.assertIn("260 cursors", line)
        self.assertIn("260 cursor-sibling locks", line)
        self.assertIn("1 meld rooms idle", line)
        self.assertIn("helm gc --apply", line)
        self.assertIn("helm chat retire-rooms --apply", line)

    def test_a_small_directory_is_ok(self):
        chat.post("hello", room="main", who="alice")
        self.cursor("main", "alice")
        [(level, line)] = doctor.check_chat_dir_debris()
        self.assertEqual(level, doctor.OK, line)
        self.assertIn("for 1 rooms", line)


if __name__ == "__main__":
    unittest.main()
