#!/usr/bin/env python3
"""A room written since this lane last looked at it is in the NEXT pass.

THE MEASURED COST THIS FILE PINS. `_fair_room_slice` hands a pass at most
`size` foreign rooms. A rotation that appends each newly discovered room
BEHIND every room it already knows makes the first row of a new meld room
wait out a whole lap (measured: p50 39.5 s across 507 invites) and a later
row in an existing room wait half a lap (p50 18.5 s), while a pinned room's
rows are seen in 1.9 s. A lap is `ceil(rooms / size)` passes, so that wait
grows with the size of the estate and not with how much has changed.

THE ORDER THESE ARMS HOLD THE SLICE TO. A pass takes DIRTY rooms first: a
room this lane never handed out, or whose file no longer matches the record
taken when it was last handed out. It fills the remaining slots from the fair
rotation of clean rooms, and the room at the head of that rotation (the one
handed out longest ago) is never passed over, so coverage stays eventual even
while more rooms are dirty than a pass can take.

RED ON THE PRE-CURE TREE, by behaviour and never by a missing name: every arm
here calls only `_fair_room_slice`, `_scan_rooms` and `deliver_any` with their
pre-cure signatures, so an arm that is red on that tree is red because the
room it names was not in the slice. The arms that are GREEN there too are the
preservation arms (rotation, the all-dirty drain, the flood head, the old
state shape), and each says so.

Real files in a hermetic HELM_CHAT_DIR throughout. Where time matters the
mtime is planted with os.utime(ns=...), never mocked.
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from helm import chat, pk, seats, seats_common, seats_roomscan  # noqa: E402

LANE = "probe"
SIZE = 4


def _laps(n, size=SIZE):
    return -(-n // size)


class DirtyFirstBase(unittest.TestCase):
    SEAT, SID = "dirtyseat", "s-dirtyseat"
    ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
                "HELM_CHAT_NODE_URL", "MELD_CHAT_NODE_URL", "HELM_CHAT_NAME",
                "HELM_CHAT_ROOM", "HELM_CHAT_EVENT_DIR", "HELM_ADOPTED_DIR",
                "HELM_CHAT_OWNER_NAMES", "CLAUDE_CODE_SESSION_ID",
                "CLAUDE_SESSION_ID", "CODEX_SESSION_ID")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-dirtyfirst-")
        self.prior = {k: os.environ.get(k) for k in self.ENV_KEYS}
        for k in self.ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_EVENT_DIR"] = os.path.join(self.tmp, "events")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])
        os.environ["HELM_CHAT_NODE_URL"] = ""
        self.cwd_prior = os.getcwd()
        os.chdir(self.tmp)
        # A JOINED seat, because rotation state persists only for the seat's
        # current incarnation: an unjoined seat gets the fixed prefix every
        # pass and no ring at all, and every arm below would measure that.
        seats.join(session=self.SID, seat=self.SEAT, cwd=self.tmp)
        self.clock = 1700000000 * 10 ** 9
        self.rooms = []

    def tearDown(self):
        os.chdir(self.cwd_prior)
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def room(self, name):
        """A real room file holding one more row, with its own planted mtime."""
        path = chat.room_path(name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "ab") as fh:
            fh.write(b'{"from": "bob", "text": "noise"}\n')
        self.tick(name)
        if name not in self.rooms:
            self.rooms.append(name)
        return path

    def tick(self, name):
        self.clock += 10 ** 9
        os.utime(chat.room_path(name), ns=(self.clock, self.clock))

    def estate(self, n):
        for i in range(n):
            self.room("r%02d" % i)

    def slice(self):
        return seats_roomscan._fair_room_slice(
            sorted(self.rooms), self.SEAT, self.SID, SIZE, LANE)

    def lap(self):
        """Enough passes for a fresh ring to hand every room out once."""
        return [self.slice() for _ in range(_laps(len(self.rooms)))]

    def ring(self):
        state = pk.read_json(
            seats_roomscan.scan_path(self.SEAT, self.SID, LANE), None)
        self.assertIsInstance(state, dict, "the rotation was never persisted")
        return state["rooms"]


class AFreshRoomIsInTheNextSliceTest(DirtyFirstBase):
    """Arm 1: a room born after the estate filled is looked at next pass.

    THE MUST-DIFFER CONTROL is this same world on the pre-cure tree, where
    the new room is appended behind all twelve known rooms and the next slice
    is the head of the rotation instead. The name sorts LAST on purpose, so
    an alphabetical-prefix fallback cannot pass this arm. A pass whose ring
    lock fails rotates in memory, newest-written first (task/2520), and would
    hand this room out too, so the arm also reads the SAVED ring: only a
    rotation that held its lock records the new room."""

    def test_a_room_born_after_the_lap_is_in_the_very_next_slice(self):
        self.estate(3 * SIZE)
        self.lap()
        ring = self.ring()
        self.assertIn("r00", ring)
        self.assertEqual(sorted(ring), sorted(self.rooms))
        self.room("zz-new")
        got = self.slice()
        self.assertEqual(len(got), SIZE)
        self.assertIn("zz-new", got)
        self.assertIn("zz-new", self.ring())


class AWrittenRoomIsInTheNextSliceTest(DirtyFirstBase):
    """Arm 2: a later write to a room this lane already handed out.

    Each arm writes to a room from the LAST slice of the lap, the one the
    rotation would reach last, and checks an untouched room from the same
    slice stays out, so it is the write and not the rotation that brought the
    room back."""

    def settled(self):
        self.estate(3 * SIZE)
        last = self.lap()[-1]
        self.assertEqual(len(last), SIZE)
        return last

    def test_an_append_puts_the_room_in_the_next_slice(self):
        last = self.settled()
        target, quiet = last[-1], last[0]
        self.room(target)
        got = self.slice()
        self.assertIn(target, got)
        self.assertNotIn(quiet, got)

    def test_a_new_mtime_alone_puts_the_room_in_the_next_slice(self):
        """The mtime is the change signal: same bytes, same size, a new
        st_mtime_ns planted with os.utime still reads as written."""
        last = self.settled()
        target, quiet = last[-2], last[0]
        size = os.stat(chat.room_path(target)).st_size
        self.assertGreater(size, 0)
        self.tick(target)
        after = os.stat(chat.room_path(target)).st_size
        self.assertEqual(after, size)
        got = self.slice()
        self.assertIn(target, got)
        self.assertNotIn(quiet, got)

    def test_a_replaced_file_with_the_same_mtime_and_size_reads_written(self):
        """A room replaced under its name (a rotation, a restore) can carry
        the old size and a planted-identical mtime; the inode still moved,
        and a new file is a room this lane has not read."""
        last = self.settled()
        target, quiet = last[-3], last[0]
        path = chat.room_path(target)
        st = os.stat(path)
        with open(path, "rb") as fh:
            body = fh.read()
        fresh = path + ".new"
        with open(fresh, "wb") as fh:
            fh.write(body)
        os.replace(fresh, path)
        os.utime(path, ns=(st.st_mtime_ns, st.st_mtime_ns))
        now = os.stat(path)
        self.assertEqual((now.st_mtime_ns, now.st_size),
                         (st.st_mtime_ns, st.st_size))
        self.assertNotEqual(now.st_ino, st.st_ino)
        got = self.slice()
        self.assertIn(target, got)
        self.assertNotIn(quiet, got)


class CleanRoomsStillRotateTest(DirtyFirstBase):
    """Arm 3 (PRESERVATION, green on the pre-cure tree too): with nothing
    written, every room is handed out within ceil(N/size) passes, from a
    fresh ring and again from a settled one."""

    def test_every_room_is_covered_within_one_lap_when_nothing_is_written(self):
        self.estate(10)                 # not a multiple of SIZE: 3 passes
        self.assertEqual(_laps(len(self.rooms)), 3)
        fresh = self.slice() + self.slice() + self.slice()
        settled = self.slice() + self.slice() + self.slice()
        self.assertEqual(len(fresh), 12)
        self.assertEqual(len(settled), 12)
        self.assertEqual(sorted(set(fresh)), sorted(self.rooms), "fresh ring")
        self.assertEqual(sorted(set(settled)), sorted(self.rooms),
                         "settled ring")


class MoreDirtThanAPassTest(DirtyFirstBase):
    """Arm 4: more dirty rooms than `size` drain in bounded passes, the room
    handed out longest ago first."""

    def test_dirty_rooms_drain_oldest_first_within_the_bound(self):
        """Five dirty rooms, the five handed out MOST recently, among twenty.
        THE BOUND IS ceil(dirty / (size - 1)) while a clean room waits at the
        head of the rotation, because that room keeps one slot; with these
        numbers it equals ceil(dirty / size), two passes. The pre-cure tree
        spends those two passes on the eight clean rooms at the head."""
        self.estate(5 * SIZE)
        self.lap()
        order = self.ring()
        dirty = order[-5:]
        for name in dirty:
            self.room(name)
        first, second = self.slice(), self.slice()
        self.assertIn(dirty[0], first)
        self.assertEqual(first[:3], dirty[:3],
                         "the dirty room handed out longest ago goes first")
        self.assertLessEqual(set(dirty), set(first) | set(second),
                             "a dirty room outlived ceil(5 / 3) passes")

    def test_an_all_dirty_estate_drains_in_exactly_ceil_dirty_over_size(self):
        """PRESERVATION: when every room is dirty no clean room is owed a
        slot, so nothing is held back and each pass is the next `size` rooms
        of the rotation."""
        self.estate(3 * SIZE)
        self.lap()
        order = self.ring()
        for name in order:
            self.room(name)
        got = [self.slice(), self.slice(), self.slice()]
        self.assertEqual(_laps(len(order)), 3)
        self.assertEqual(got, [order[:SIZE], order[SIZE:2 * SIZE],
                               order[2 * SIZE:]])


class TheRotationHeadIsNeverPassedOverTest(DirtyFirstBase):
    """PRESERVATION of eventual coverage under a sustained flood (green on
    the pre-cure tree, which had no dirty rooms to prefer). A room that was
    handed out and never read -- a pass cut short -- looks clean, and only
    the rotation ever brings it back. If dirty rooms could fill every slot
    for as long as they kept coming, that room would wait for the flood to
    end, and the delivery promise's "coverage is EVENTUAL" would be false."""

    def test_a_clean_room_is_handed_out_while_every_other_room_is_written(self):
        self.estate(3 * SIZE)
        self.lap()
        quiet = self.ring()[-1]                 # the rotation reaches it last
        handed = []
        for _ in range(len(self.rooms)):
            for name in self.rooms:
                if name != quiet:
                    self.room(name)
            got = self.slice()
            handed.append(got)
            if quiet in got:
                break
        self.assertTrue(all(len(g) == SIZE for g in handed))
        self.assertIn(quiet, handed[-1],
                      "a flood of %d dirty rooms per pass kept a clean room "
                      "out of %d passes" % (len(self.rooms) - 1, len(handed)))


class AStatFailureReadsDirtyTest(DirtyFirstBase):
    """Arm 5: a room whose stat fails is looked at, pass after pass. The
    failure is real: the room file is a symlink to itself, and stat raises
    ELOOP. Failing toward looking costs one slot; failing toward clean hides
    a room nobody can prove is empty."""

    def test_an_unstatable_room_is_in_the_next_slice_and_stays_there(self):
        self.estate(3 * SIZE)
        last = self.lap()[-1]
        target, quiet = last[-1], last[0]
        path = chat.room_path(target)
        os.remove(path)
        os.symlink(os.path.basename(path), path)
        with self.assertRaises(OSError):
            os.stat(path)
        first, second = self.slice(), self.slice()
        self.assertIn(target, first)
        self.assertNotIn(quiet, first)
        self.assertIn(target, second, "a failed stat was recorded as clean")


class TheOldStateShapeIsReadTest(DirtyFirstBase):
    """Arm 6 (PRESERVATION): a ring file in the old shape, `{"rooms": [...]}`
    and nothing else, keeps its rotation order. And the file this pass writes
    still carries every room in `rooms`, the one key an older reader reads,
    so a lane behind trunk reading it neither crashes nor resets."""

    def test_an_old_shape_ring_continues_where_it_was(self):
        self.estate(3 * SIZE)
        names = sorted(self.rooms)
        old = names[5:] + names[:5]             # mid-lap, not alphabetical
        path = seats_roomscan.scan_path(self.SEAT, self.SID, LANE)
        pk.write_json(path, {"rooms": old})
        first, second = self.slice(), self.slice()
        self.assertIn(old[0], first)
        self.assertIn(old[SIZE], second)
        self.assertEqual(first, old[:SIZE])
        self.assertEqual(second, old[SIZE:2 * SIZE])
        state = pk.read_json(path, None)
        rooms = state.get("rooms")
        self.assertIsInstance(rooms, list)
        self.assertIn(old[0], rooms)
        self.assertTrue(all(isinstance(r, str) for r in rooms))
        self.assertEqual(sorted(rooms), names)
        self.assertEqual(rooms[:len(old) - 2 * SIZE], old[2 * SIZE:])


class TheRecordIsReadDefensivelyTest(DirtyFirstBase):
    """A record the reader cannot use is read as NO record: the room is
    dirty and the pass looks at it, and nothing raises."""

    def test_a_malformed_record_map_reads_every_room_as_unseen(self):
        self.estate(3 * SIZE)
        names = sorted(self.rooms)
        path = seats_roomscan.scan_path(self.SEAT, self.SID, LANE)
        pk.write_json(path, {"rooms": names, seats_roomscan.LOOKED: "junk"})
        got = self.slice()
        self.assertIn(names[0], got)
        self.assertEqual(got, names[:SIZE])

    def test_a_record_of_the_wrong_type_reads_dirty(self):
        self.estate(3 * SIZE)
        last = self.lap()[-1]
        target, quiet = last[-1], last[0]
        path = seats_roomscan.scan_path(self.SEAT, self.SID, LANE)
        state = pk.read_json(path, None)
        state[seats_roomscan.LOOKED][target] = 5
        pk.write_json(path, state)
        got = self.slice()
        self.assertIn(target, got)
        self.assertNotIn(quiet, got)


class GiveBackTest(DirtyFirstBase):
    """`_give_back` returns rooms a pass never read to the state they had,
    and never undoes a record another pass wrote since."""

    def lend(self):
        lent = {}
        got = seats_roomscan._fair_room_slice(
            sorted(self.rooms), self.SEAT, self.SID, SIZE, LANE, lent=lent)
        self.assertEqual(sorted(lent), sorted(got))
        return got, lent

    def looked(self):
        state = pk.read_json(
            seats_roomscan.scan_path(self.SEAT, self.SID, LANE), None)
        return state[seats_roomscan.LOOKED]

    def test_an_unread_clean_room_goes_back_clean_and_the_hit_goes_back_dirty(
            self):
        """The same clean room, handed out by the rotation and never read:
        given back as merely UNREAD it keeps its clean record, so the next
        pass need not spend a slot on it; given back as the HIT it loses the
        record, because one event per pass may have left rows behind it."""
        self.estate(3 * SIZE)
        self.lap()
        got, lent = self.lend()                 # nothing dirty: pure rotation
        unread, hit = got[0], got[1]
        seats_roomscan._give_back([hit, unread], self.SEAT, self.SID, LANE,
                                  lent, hit=hit)
        looked = self.looked()
        self.assertIn(unread, looked)
        self.assertEqual(looked[unread], lent[unread][0])
        self.assertNotIn(hit, looked)
        nxt = self.slice()
        self.assertIn(hit, nxt)
        self.assertNotIn(unread, nxt)

    def test_a_record_another_pass_wrote_since_is_left_standing(self):
        self.estate(3 * SIZE)
        self.lap()
        self.room("zz-new")
        got, lent = self.lend()
        self.assertIn("zz-new", got)
        self.room("zz-new")                     # written again ...
        again = self.slice()                    # ... and handed out again
        self.assertIn("zz-new", again)
        standing = self.looked()["zz-new"]
        self.assertNotEqual(standing, lent["zz-new"][1])
        seats_roomscan._give_back(["zz-new"], self.SEAT, self.SID, LANE, lent)
        kept = self.looked()
        self.assertIn("zz-new", kept)
        self.assertEqual(kept["zz-new"], standing)
        # THE POLE: the same give-back with no later pass does undo it.
        self.room("zz-other")
        mine, mine_lent = self.lend()
        self.assertIn("zz-other", mine)
        seats_roomscan._give_back(["zz-other"], self.SEAT, self.SID, LANE,
                                  mine_lent)
        after = self.looked()
        self.assertIn("zz-new", after)
        self.assertNotIn("zz-other", after)

    def test_a_give_back_that_cannot_lock_says_so(self):
        import contextlib
        import io
        from unittest import mock
        self.estate(3 * SIZE)
        got, lent = self.lend()
        err = io.StringIO()
        with mock.patch.object(seats_roomscan, "_flocked",
                               side_effect=OSError("ring unreadable")), \
                contextlib.redirect_stderr(err):
            seats_roomscan._give_back(got, self.SEAT, self.SID, LANE, lent,
                                      hit=got[0])
        self.assertIn("give-back failed", err.getvalue())
        self.assertIn(got[0], self.looked())    # the record was not touched


class DeliveryReachesAFreshRoomOnTheNextPassTest(DirtyFirstBase):
    """Arms 1 and 7 end to end, through `deliver_any` on a real estate larger
    than one pass: the path both the beacon and the tool-boundary hook take.
    The seat joins BEFORE the estate exists, so every room is post-join news
    and backfills; its noise is foreign chatter that delivers nothing."""

    def settle(self):
        for i in range(seats_common.ROOM_SCAN_CAP * 2 + 8):
            chat.post("noise", who="bob", room="old-%02d" % i)
        return [seats.deliver_any(session=self.SID, seat=self.SEAT)
                for _ in range(4)]

    def test_a_new_rooms_first_row_is_delivered_on_the_next_pass(self):
        quiet = self.settle()
        chat.post("@%s join the meld" % self.SEAT, who="bob", room="zz-meld")
        got = seats.deliver_any(session=self.SID, seat=self.SEAT)
        self.assertEqual(quiet.count(None), 4)
        self.assertIn("join the meld", got or "")

    def test_a_hit_hands_the_unread_rest_of_its_slice_back(self):
        """Arm 7. A pass stops at its first delivery, so rooms handed to it
        after the hit were never read. If handing out were the same as
        reading, the second invite would wait a lap behind forty rooms."""
        quiet = self.settle()
        chat.post("@%s first invite" % self.SEAT, who="bob", room="zz-a")
        chat.post("@%s second invite" % self.SEAT, who="bob", room="zz-b")
        got = [seats.deliver_any(session=self.SID, seat=self.SEAT)
               for _ in range(2)]
        self.assertEqual(quiet.count(None), 4)
        self.assertIn("first invite", got[0] or "")
        self.assertIn("second invite", got[1] or "")

    def test_the_hit_room_itself_is_read_again_next_pass(self):
        """Arm 7, the hit room. One pass delivers one event and leaves the
        room's later rows pending, so the room that hit is not done."""
        quiet = self.settle()
        chat.post("@%s row one" % self.SEAT, who="bob", room="zz-c")
        chat.post("@%s row two" % self.SEAT, who="bob", room="zz-c")
        got = [seats.deliver_any(session=self.SID, seat=self.SEAT)
               for _ in range(2)]
        self.assertEqual(quiet.count(None), 4)
        self.assertIn("row one", got[0] or "")
        self.assertIn("row two", got[1] or "")


class AnUnlockableRingStaysFairTest(DirtyFirstBase):
    """The ring lock FAILS CLOSED (task/2520), and a pass whose ring cannot be
    locked must still cover the estate. It neither writes the ring unlocked
    (a lost update against a concurrent pass) nor returns the same
    alphabetical prefix on every pass, which starves every room sorting
    after it until the lock is repaired. The lock is broken for real: a
    directory where it lives."""

    def break_ring_lock(self, lane):
        lock = seats_roomscan.scan_path(self.SEAT, self.SID, lane) + ".lock"
        if os.path.isfile(lock):
            os.remove(lock)
        os.makedirs(lock)

    def test_a_dirty_room_past_the_prefix_is_reached_and_the_ring_is_kept(self):
        self.estate(3 * SIZE)
        self.lap()
        path = seats_roomscan.scan_path(self.SEAT, self.SID, LANE)
        before = pk.read_json(path, None)
        self.assertIn("rooms", before, "the control lap persisted no ring")
        self.break_ring_lock(LANE)
        self.room("zz-meld")                    # sorts past every prefix
        for i in range(SIZE):                   # and is not the newest write
            self.room("r%02d" % i)
        passes = [self.slice() for _ in range(_laps(len(self.rooms)) + 1)]
        self.assertTrue(any("zz-meld" in got for got in passes), passes)
        # every pass still hands out a full slice: it delivers
        self.assertEqual([len(got) for got in passes], [SIZE] * len(passes))
        # and the ring was never written without its lock
        self.assertEqual(pk.read_json(path, None), before)

    def test_a_mention_past_the_cap_is_delivered_with_the_ring_unlockable(self):
        """End to end, through `deliver_any` on the deliver lane."""
        cap = seats_common.ROOM_SCAN_CAP
        for i in range(cap * 2 + 8):
            chat.post("noise", who="bob", room="old-%02d" % i)
        quiet = [seats.deliver_any(session=self.SID, seat=self.SEAT)
                 for _ in range(4)]
        self.break_ring_lock("deliver")
        chat.post("@%s join the meld" % self.SEAT, who="bob", room="zz-meld")
        for i in range(cap):                    # it is not the newest write
            chat.post("noise", who="bob", room="old-%02d" % i)
        # the pins take at most four of the cap, so a lap is this many passes
        bound = _laps(len(chat.list_rooms()), cap - 4) + 1
        got = [seats.deliver_any(session=self.SID, seat=self.SEAT)
               for _ in range(bound)]
        self.assertEqual(quiet.count(None), 4)
        self.assertTrue(any("join the meld" in (g or "") for g in got), got)


class TheNewestFirstBranchTest(DirtyFirstBase):
    """The branch with no scan lane sorts by mtime, newest first, and keeps
    `size`. A fresh write sorts FIRST there, so it never had the lap-long
    wait. It does share the stat question: an unreadable room scored 0.0,
    the oldest possible time, is the first room dropped, and a room nobody
    can stat is not a room anybody has proven empty."""

    def test_an_unreadable_room_is_looked_at_and_a_vanished_one_is_not(self):
        n = seats_common.ROOM_SCAN_CAP + 4
        self.estate(n)                          # r00 oldest
        loop = chat.room_path("r00")
        os.remove(loop)
        os.symlink(os.path.basename(loop), loop)
        os.symlink("nowhere.jsonl", chat.room_path("gone"))
        got = seats_roomscan._scan_rooms("main")
        self.assertIn("r%02d" % (n - 1), got)   # the newest room is kept
        self.assertIn("r00", got, "an unreadable room was scored as oldest")
        self.assertNotIn("gone", got)


if __name__ == "__main__":
    unittest.main()
