#!/usr/bin/env python3
"""A pass that nothing could feed does not look (task/3848).

THE MEASURED COST THIS FILE PINS. Every beacon waiter ran `deliver_any` every
POLL_S (2 s), and every pass listed the flat chat directory to learn which
rooms exist: 51,196 entries for 503 room logs on the live bus, 86 ms of CPU a
listing, 16 beacons, 0.94 of a core with nobody posting. The tool-boundary
hook listed it twice per tool call on top.

WHAT THE CURE KEEPS, and the arms below hold each:
  * a consumer whose every room log is unchanged since a pass proved its
    cursor at the end of that log neither lists nor scans (arm 1);
  * an append, a new room, a room an older writer created, and rows a pass
    left pending all still reach the next pass (arms 2-5), so the skip never
    drops a row;
  * a pass after a delivery never hands the same row back (arm 5), so the skip
    never redelivers one;
  * a room the cached listing still names after it left the bus is not handed
    out, so no pass mints a cursor for a room that is gone (arm 6);
  * a room that cannot be statted is never taken for quiet (arm 7);
  * a hook pass, which lives for one boundary, borrows the shared listing and
    never skips (arm 8); the waiter carries ONE proof across its polls (arm 9).

Real files in a hermetic HELM_CHAT_DIR throughout. Where time matters the
clock is injected, never slept on.
"""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._tmphome import declaring as _tmp_declaring  # noqa: E402
from helm import (chat, pk, seats, seats_common, seats_delivery,  # noqa: E402
                  seats_join, seats_roomscan)
from helm.seats_cursor import parse_cursor_path  # noqa: E402


class QuietBase(unittest.TestCase):
    SEAT, SID = "quietseat", "s-quietseat"
    ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
                "HELM_CHAT_NODE_URL", "MELD_CHAT_NODE_URL", "HELM_CHAT_NAME",
                "HELM_CHAT_ROOM", "HELM_CHAT_EVENT_DIR", "HELM_ADOPTED_DIR",
                "HELM_CHAT_OWNER_NAMES", "CLAUDE_CODE_SESSION_ID",
                "CLAUDE_SESSION_ID", "CODEX_SESSION_ID")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-quiet-")
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
        # MORE ROOMS THAN ONE PASS TAKES, and every one of them older than
        # the join: the join baselines each at its end, which is the steady
        # state of a live seat, so the first pass can already prove them all.
        self.rooms = ["old-%02d" % i
                      for i in range(seats_common.ROOM_SCAN_CAP * 2 + 8)]
        for room in self.rooms:
            chat.post("noise", who="bob", room=room)
        seats.join(session=self.SID, seat=self.SEAT, cwd=self.tmp)
        self.now = [1700000000.0]

    def tearDown(self):
        os.chdir(self.cwd_prior)
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def clock(self):
        return self.now[0]

    def consumer(self, remember=True):
        return seats_roomscan.QuietRooms(remember=remember, clock=self.clock)

    def deliver(self, quiet, channel="beacon"):
        return seats.deliver_any(session=self.SID, seat=self.SEAT,
                                 channel=channel, quiet=quiet)

    def settled(self, channel="beacon"):
        """A consumer whose pass has proved every room: the join put every
        cursor at its log's end, so one pass that finds nothing is enough."""
        quiet = self.consumer()
        self.assertIsNone(self.deliver(quiet, channel))
        return quiet

    def counted(self):
        """Count the listings and the scans a pass makes, through the real
        functions: a skip that still listed would save nothing."""
        seen = {"list": 0, "scan": 0}
        real_list, real_scan = chat.list_rooms, seats_delivery._scan_rooms

        def listing():
            seen["list"] += 1
            return real_list()

        def scan(*a, **kw):
            seen["scan"] += 1
            return real_scan(*a, **kw)
        stack = [mock.patch.object(chat, "list_rooms", listing),
                 mock.patch.object(seats_delivery, "_scan_rooms", scan)]
        for patch in stack:
            patch.start()
            self.addCleanup(patch.stop)
        return seen


class NothingChangedTest(QuietBase):
    """Arm 1: nothing changed since a pass proved every room, so the next
    pass neither lists the directory nor scans a room."""

    def test_a_proven_quiet_estate_is_neither_listed_nor_scanned(self):  # noqa: VACUOUS_ASSERTION — a skipped pass returning nothing is the contract; the counters are proved live by the unproved pass at the end
        quiet = self.settled()
        seen = self.counted()
        got = [self.deliver(quiet) for _ in range(5)]
        self.assertEqual(got, [None] * 5)
        self.assertEqual(seen, {"list": 0, "scan": 0})
        self.deliver(None)
        self.assertEqual(seen, {"list": 1, "scan": 1},
                         "must-hit: the counters see a pass with no proof")

    def test_the_control_without_a_proof_lists_and_scans_every_pass(self):
        """The must-differ control: the same world, passes with no consumer
        proof, which is the pass every beacon made before this change."""
        self.settled()
        seen = self.counted()
        for _ in range(3):
            seats.deliver_any(session=self.SID, seat=self.SEAT,
                              channel="beacon")
        self.assertEqual(seen, {"list": 3, "scan": 3})


class AChangeReachesTheNextPassTest(QuietBase):
    """Arms 2-4: whatever changed, the pass after it runs and delivers."""

    def test_an_append_to_a_known_room_is_delivered(self):
        quiet = self.settled()
        chat.post("@%s look here" % self.SEAT, who="bob", room=self.rooms[7])
        self.assertIn("look here", self.deliver(quiet) or "")

    def test_a_new_room_is_listed_and_its_first_row_delivered(self):
        quiet = self.settled()
        chat.post("@%s join the meld" % self.SEAT, who="bob", room="zz-meld")
        seen = self.counted()
        self.assertIn("join the meld", self.deliver(quiet) or "")
        self.assertEqual(seen["list"], 1, "the new room was found without "
                         "the one listing its creation asks for")

    def test_a_room_an_older_writer_made_is_found_within_the_bound(self):
        """A writer running older code creates a log without moving the
        room-set token. The listing is trusted for NAMES_MAX_AGE_S, and the
        room is found on the first pass after it."""
        quiet = self.settled()
        path = chat.room_path("zz-legacy")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"id": "x1", "ts": "2030-01-01T00:00:00Z", "from": '
                    '"bob", "text": "@%s from an old writer"}\n' % self.SEAT)
        self.now[0] += seats_roomscan.NAMES_MAX_AGE_S + 1
        self.assertIn("from an old writer", self.deliver(quiet) or "")


class PendingRowsKeepTheConsumerAwakeTest(QuietBase):
    """Arm 5: one pass delivers one event and leaves the rest pending. The
    log does not change between those passes, and the next pass must still
    run; after the last one, nothing is handed back again."""

    def test_every_row_is_delivered_once_and_then_the_pass_is_skipped(self):  # noqa: VACUOUS_ASSERTION — the first two passes deliver both rows; a third delivery would be a replay, so its absence is the contract
        quiet = self.settled()
        room = self.rooms[3]
        chat.post("@%s row one" % self.SEAT, who="bob", room=room)
        chat.post("@%s row two" % self.SEAT, who="bob", room=room)
        got = [self.deliver(quiet) for _ in range(2)]
        self.assertIn("row one", got[0] or "")
        self.assertIn("row two", got[1] or "")
        seen = self.counted()
        self.assertIsNone(self.deliver(quiet), "a row came back again")
        self.assertEqual(seen["scan"], 1, "the pass that proved the room "
                         "drained never ran")
        self.assertIsNone(self.deliver(quiet))
        self.assertEqual(seen["scan"], 1, "the proved estate was scanned")


class AGoneRoomMintsNothingTest(QuietBase):
    """Arm 6: a room retired off the bus stays in the cached listing until
    the next listing, and a pass must not hand it out: handing it out mints
    a fresh cursor pair for a room that no longer exists."""

    def cursors_on(self, key):
        root = chat.chat_dir()
        return [p["path"] for n in os.listdir(root)
                for p in [parse_cursor_path(os.path.join(root, n))]
                if p is not None and p["room"] == key]

    def test_a_removed_room_gets_no_cursor_from_a_pass(self):  # noqa: VACUOUS_ASSERTION — no cursor on a gone room is the contract; the must-hit counts the join's cursors on it first
        quiet = self.settled()
        gone = self.rooms[5]
        key = pk.slug(gone)
        held = self.cursors_on(key)
        self.assertTrue(held, "must-hit: the join minted no cursor here")
        os.remove(chat.room_path(gone))
        for path in held:
            os.remove(path)
        for _ in range(4):
            self.deliver(quiet)
        self.assertEqual(self.cursors_on(key), [])


class AnUnstattableRoomIsNeverQuietTest(QuietBase):
    """Arm 7: a room that cannot be statted might hold anything, so the
    consumer never takes it for quiet."""

    def test_a_room_that_cannot_be_statted_keeps_the_pass_running(self):
        quiet = self.settled()
        loop = chat.room_path(self.rooms[2])
        os.remove(loop)
        os.symlink(os.path.basename(loop), loop)
        seen = self.counted()
        for _ in range(3):
            self.deliver(quiet)
        self.assertEqual(seen["scan"], 3)


class AnUnprovableEstateCostsNoMoreThanItsScanTest(QuietBase):
    """A consumer whose rooms never prove quiet -- here delivery is switched
    off, so no pass mints a cursor on a room born after the join -- must not
    pay for proving on top of the scan it already makes: after its first
    pass it reads only the rooms it scanned and the rooms that moved."""

    def test_a_pass_after_the_first_reads_only_its_slice(self):  # noqa: VACUOUS_ASSERTION — delivery is switched off, so every pass returns nothing; the observable is how many rooms a pass reads
        late = seats_common.ROOM_SCAN_CAP * 4
        for i in range(late):
            chat.post("noise", who="bob", room="late-%02d" % i)
        prior = os.environ.get("HELM_CHAT_DELIVER")
        os.environ["HELM_CHAT_DELIVER"] = "0"
        self.addCleanup(lambda: os.environ.pop("HELM_CHAT_DELIVER", None)
                        if prior is None else
                        os.environ.__setitem__("HELM_CHAT_DELIVER", prior))
        quiet = self.consumer()
        self.assertIsNone(self.deliver(quiet))
        asked = []
        real = seats_delivery._room_dirty

        def spy(room, *a, **kw):
            asked.append(room)
            return real(room, *a, **kw)
        with mock.patch.object(seats_delivery, "_room_dirty", spy):
            self.assertIsNone(self.deliver(quiet))
        self.assertTrue(asked, "must-hit: the second pass scanned nothing")
        self.assertLessEqual(len(asked), 2 * seats_common.ROOM_SCAN_CAP,
                             "the pass re-read every unproved room")


class AHandedOutRoomIsAskedAgainTest(QuietBase):
    """A pass that hands out a room reads this consumer's cursor on it, so
    what that pass finds replaces the proof the room had, even at the same
    stamp. A cursor moved back with no append (the case `QuietRooms` names)
    reads dirty there; if the pass does not deliver from it, a proof left
    standing would skip the room on every later poll."""

    def proved(self):
        quiet = self.settled()
        room = self.rooms[5]
        snap = quiet.snapshot(chat.list_rooms(), self.SEAT, "main")
        self.assertEqual(quiet.clean.get(room), snap[room],
                         "must-hit: the room starts proved at its stamp")
        return quiet, room, snap

    def test_a_proved_room_the_pass_found_dirty_loses_its_proof(self):
        quiet, room, snap = self.proved()
        quiet.settle(snap, lambda r: r == room, looked=[room])
        self.assertNotIn(room, quiet.clean)
        self.assertFalse(quiet.quiet(snap), "a room seen holding rows was "
                         "left proved, so the next poll skips it")

    def test_the_control_a_room_not_handed_out_keeps_its_proof(self):
        """The must-differ control: the same dirty answer, for a room the
        pass did not hand out, is never asked, so the proof stands."""
        quiet, room, snap = self.proved()
        quiet.settle(snap, lambda r: r == room, looked=())
        self.assertEqual(quiet.clean.get(room), snap[room])
        self.assertTrue(quiet.quiet(snap))


class TheHookBorrowsTheListingTest(QuietBase):
    """Arm 8: a hook process lives for one tool boundary, so it keeps no
    proof between passes; it reads the listing another pass already made
    and lists only when the room set may have moved."""

    def test_a_second_hook_pass_reads_the_listing_and_still_scans(self):
        self.deliver(self.consumer(remember=False), "hook")
        seen = self.counted()
        self.deliver(self.consumer(remember=False), "hook")
        self.assertEqual(seen, {"list": 0, "scan": 1})

    def test_a_hook_pass_lists_again_once_a_room_is_created(self):
        # a lap of hook passes first, so the ring has handed every room out
        # once: a hook keeps no proof, and a room its ring never reached is
        # handed out before a room just written, exactly as before
        for _ in range(-(-len(self.rooms) // seats_common.ROOM_SCAN_CAP) + 1):
            self.deliver(self.consumer(remember=False), "hook")
        chat.post("@%s new room" % self.SEAT, who="bob", room="zz-hook")
        seen = self.counted()
        got = self.deliver(self.consumer(remember=False), "hook")
        self.assertIn("new room", got or "")
        self.assertEqual(seen["list"], 1)

    def test_the_hook_passes_a_consumer_that_keeps_no_proof(self):  # noqa: VACUOUS_ASSERTION — the observable is the argument the hook hands the pass, and the spy is where it lands
        seen = []
        real = seats_delivery.deliver_any

        def spy(**kw):
            seen.append(kw.get("quiet"))
            return real(**kw)
        args = ["deliver", "--seat", self.SEAT]
        with mock.patch("helm.seats_cli.deliver_any", spy), \
                _tmp_declaring(args), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            seats.cmd(args[0], args[1:], "main")
        self.assertEqual(len(seen), 1)
        self.assertIsInstance(seen[0], seats_roomscan.QuietRooms)
        self.assertFalse(seen[0].remember)


class TheWaiterCarriesOneProofTest(QuietBase):
    """Arm 9: the beacon waiter holds one consumer for its whole life, on
    both of its delivery legs, so the proof one poll made spares the next."""

    def polls(self, doorbell):
        """The consumer each delivery call of one waiter carries. A poll the
        idle gate skips (helm.beacon_idle, task/3873) makes no call at all,
        so a row written after the first pass owes the waiter a second."""
        seen = []
        real = seats_join.deliver_any

        def spy(**kw):
            seen.append(kw.get("quiet"))
            line = real(**kw)
            if len(seen) == 1:
                chat.post("a row after the first pass", who="bob",
                          room=self.rooms[0])
            return line
        with mock.patch.object(seats_join, "deliver_any", spy):
            seats.wait(seat=self.SEAT, session=self.SID, follow=True,
                       timeout=0.5, poll=0.01, emit=[].append,
                       doorbell=doorbell)
        return seen

    def test_the_doorbell_leg_passes_the_same_consumer_every_poll(self):
        seen = self.polls(doorbell=True)
        self.assertGreaterEqual(len(seen), 2, "the waiter polled once")
        self.assertIsInstance(seen[0], seats_roomscan.QuietRooms)
        self.assertTrue(seen[0].remember)
        self.assertEqual({id(q) for q in seen}, {id(seen[0])},
                         "a poll got a fresh consumer")

    def test_the_per_row_leg_passes_the_same_consumer_every_poll(self):
        seen = self.polls(doorbell=False)
        self.assertGreaterEqual(len(seen), 2, "the waiter polled once")
        self.assertIsInstance(seen[0], seats_roomscan.QuietRooms)
        self.assertTrue(seen[0].remember)
        self.assertEqual({id(q) for q in seen}, {id(seen[0])},
                         "a poll got a fresh consumer")


if __name__ == "__main__":
    unittest.main()
