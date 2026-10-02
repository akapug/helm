#!/usr/bin/env python3
"""The doorbell releases a row the seat ACKED, and a row its state recorded
without a `start` once the seat has seen it.

THE SYMPTOM THESE ARMS PIN. A seat acked all six rows its doorbell counted,
five in its project room and one in main, by their ids, and a fresh waiter
still rang every 12 minutes: "6 unread = 6 addressed · 0 new since the last
ring". Every entry in its state was acked by the seat, and none carried
`start`, because a waiter older than that field wrote them. Two rules were
missing. An ack never counted as read for the doorbell, and a row with no
`start` could never be released by a pull, so it stayed counted until the
tool-boundary hook showed it, which a seat that pulls instead never reaches.

THE SAFE DIRECTION STAYS: a row the seat never saw stays counted. An ack from
the seat itself is evidence it saw the row; an ack from another seat is not,
and neither is a wake cursor that still holds the row.

Every arm drives the real waiter, the real `helm chat ack` and `helm chat
read` doors and the real hook pass (PullBase), in a scratch chat dir and helm
home. The old state is the real state a waiter wrote, less its `start` keys.
"""
import contextlib
import io
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import chat, seats, seats_delivery  # noqa: E402
from tests._tmphome import declaring as _tmp_declaring  # noqa: E402
from tests.test_beacon_doorbell import SEAT, SID, _unread  # noqa: E402
from tests.test_pull_delivery import OTHER, OTHER_SID, PullBase  # noqa: E402


class ReleaseBase(PullBase):

    def setUp(self):
        super().setUp()
        seats.join(session=OTHER_SID, seat=OTHER, cwd="/tmp/p", room="main")

    def ring(self, posts):
        """Post `posts` ([(text, room)]) as bob and run one waiter that rings
        for them -> the posted rows."""
        rows = [chat.post(text, who="bob", room=room) for text, room in posts]
        lines, _seen = self.follow(passes=2, clock=True)
        self.assertEqual([_unread(x) for x in lines], [len(rows)], lines)
        return rows

    def ack(self, row, seat=SEAT, session=None):
        """`helm chat ack <id>` by `seat` through the CLI door, by the row's
        stable id; `session` runs it in that session of the seat instead of
        the one the fixture declares."""
        out, err = io.StringIO(), io.StringIO()
        env = {"CLAUDE_CODE_SESSION_ID": session} if session else {}
        with mock.patch.dict(os.environ, env), \
                _tmp_declaring(["--seat", seat]), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = chat.cmd_chat(["ack", row["id"]])
        self.assertEqual(rc, 0, err.getvalue())
        self.assertIn("acked %s" % row["id"][:8], out.getvalue())

    def fresh_waiter(self):
        """A new `helm chat wait --follow` run on the minute past the backstop
        -> the lines it printed."""
        lines, _seen = self.follow(passes=23, step=60.0, clock=True)
        return lines

    def strip_starts(self):
        """The durable state as a doorbell older than the `start` field wrote
        it: the same entries, none carrying `start`."""
        from helm import beacon_doorbell as bd
        path = bd.state_path(SEAT)
        st = bd._load(path)
        stripped = [e.pop("start", None) for e in st["owed"] + st["unrung"]]
        self.assertTrue(stripped and all(isinstance(x, int) for x in stripped),
                        stripped)
        bd._save(path, st)


class AckIsAReadTest(ReleaseBase):

    def test_an_acked_row_leaves_the_count(self):
        """RED before the cure: the count stayed 3 after the ack."""
        rows = self.ring([("@gemini owed %d" % i, "main") for i in range(3)])
        self.ack(rows[1])
        self.assertEqual(self.ring_count(), 2)

    def test_a_dm_the_seat_acked_leaves_the_count(self):  # noqa: VACUOUS_ASSERTION — the same state read the same way counts 1 on the line before the ack
        row, err = seats.dm(SEAT, "direct ask 1", who="bob")
        self.assertIsNone(err, err)
        lines, _seen = self.follow(passes=2, clock=True)
        self.assertEqual([_unread(x) for x in lines], [1], lines)
        self.assertEqual(self.ring_count(), 1)
        self.ack(row)
        self.assertEqual(self.ring_count(), 0)

    def test_a_fresh_waiter_stays_silent_for_rows_the_seat_acked(self):  # noqa: VACUOUS_ASSERTION — the same rows ring on the same fresh waiter in test_a_fresh_waiter_rings_for_the_row_the_seat_did_not_ack, which acks one fewer, and in DoorbellReleaseControlTest, which acks none
        """The incident's shape: rows in a project room and one in main, all
        acked by id. RED before the cure: the fresh waiter's backstop rang
        "3 unread"."""
        rows = self.ring([("@gemini project ask %d" % i, "other-project")
                          for i in range(2)] + [("@gemini main ask", "main")])
        for row in rows:
            self.ack(row)
        self.assertEqual(self.fresh_waiter(), [])

    def test_a_fresh_waiter_rings_for_the_row_the_seat_did_not_ack(self):
        """RED before the cure: the backstop counted all three rows."""
        rows = self.ring([("@gemini project ask %d" % i, "other-project")
                          for i in range(2)] + [("@gemini main ask", "main")])
        for row in rows[:2]:
            self.ack(row)
        lines = self.fresh_waiter()
        self.assertEqual([_unread(x) for x in lines], [1], lines)
        self.assertIn("] bob: @gemini main ask (+0 waiting", lines[0])
        self.assertIn("1 unread = 1 addressed", lines[0])

    def test_an_ack_inside_the_debounce_window_keeps_the_row_off_the_ring(self):
        """Two rows land after arming and the seat acks one before the window
        closes. RED before the cure: the ring counted both."""
        rows = []

        def script(n, _now):
            if n == 1:
                rows.extend(chat.post("@gemini new %d" % i, who="bob")
                            for i in range(2))
            elif n == 3:
                self.ack(rows[0])
        lines, _seen = self.follow(passes=12, step=10.0, script=script,
                                   clock=True)
        self.assertEqual([_unread(x) for x in lines], [1], lines)
        self.assertIn("] bob: @gemini new 1 (+0 waiting", lines[0])


class PreStartStateTest(ReleaseBase):
    """State written before the `start` field: the entry names where its row
    ends and not where it begins."""

    def test_a_pre_start_row_a_pull_released_leaves_the_count(self):
        """The pull printed rows 1 and 2 and not row 0, so the delivery
        cursor stays behind row 0 and the pull released the holds on rows 1
        and 2. RED before the cure: the ring counted 3 and the stop guard 1."""
        self.ring([("@gemini owed %d" % i, "main") for i in range(3)])
        self.strip_starts()
        out = self.pull("--room", "main", "--since",
                        str(self.since("main", "@gemini owed 1")))
        self.assertIn("@gemini owed 2", out)
        self.assertNotIn("@gemini owed 0", out)
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(self.hook_texts(), ["@gemini owed 0"])

    def test_a_pre_start_row_the_seat_acked_leaves_the_count(self):
        """RED before the cure: the count stayed 3."""
        rows = self.ring([("@gemini owed %d" % i, "main") for i in range(3)])
        self.strip_starts()
        self.ack(rows[1])
        self.assertEqual(self.ring_count(), 2)


class DoorbellReleaseControlTest(ReleaseBase):
    """What must stay counted: a row the seat has not read and has not
    acked, a row another seat acked, and a pre-start row the wake cursor
    still holds."""

    def test_a_fresh_waiter_rings_for_rows_nobody_acked(self):
        self.ring([("@gemini project ask %d" % i, "other-project")
                   for i in range(2)] + [("@gemini main ask", "main")])
        lines = self.fresh_waiter()
        self.assertEqual([_unread(x) for x in lines], [3], lines)

    def test_an_ack_by_another_seat_releases_nothing_for_this_seat(self):
        """The row asks both seats, and kimi acks it: gemini's count and
        its fresh waiter's backstop still carry it. The first ring led with
        the newer row, so this one was only counted, never shown, and the
        backstop may still lead with it (task/4019: a row a ring showed
        never leads again)."""
        row, _newer = self.ring([("@gemini @kimi both of you please", "main"),
                                 ("@gemini a newer ask", "main")])
        self.ack(row, seat=OTHER)
        acks = [m for m in chat.read("main")[0] if m.get("ack") == row["id"]]
        self.assertEqual([m.get("from") for m in acks], [OTHER])
        self.assertEqual(self.ring_count(), 2)
        lines = self.fresh_waiter()
        self.assertEqual([_unread(x) for x in lines], [2], lines)
        self.assertIn("both of you please", lines[0])

    def test_a_pre_start_row_the_seat_was_never_shown_stays_counted(self):
        """The waiter's drain passed all three rows and holds each one, so
        nothing released them; once the hook shows row 0, the delivery
        cursor has passed it and it leaves the count."""
        self.ring([("@gemini owed %d" % i, "main") for i in range(3)])
        self.strip_starts()
        self.assertEqual(self.ring_count(), 3)
        lines = self.fresh_waiter()
        self.assertEqual([_unread(x) for x in lines], [3], lines)
        self.hook_pass()
        self.assertEqual(self.ring_count(), 2)


class AckEdgeControlTest(ReleaseBase):
    """The edges of `_acked` and `_row_start`: an ack counts only when it
    names the row's FULL id in the row's OWN room,
    from this seat under recipient_matches (casefold, as the ack door
    matches), in either ack state; and a pre-start row whose start lies
    beyond BACK_BYTES, or behind a blank line, is found or kept, never
    guessed."""

    def post_ack(self, ack, who=SEAT):
        """An ack row written straight to the room, past the CLI door, which
        itself always writes the full resolved id."""
        chat.post("", room="main", who=who, ack=ack, ackstate="done")

    def test_an_ack_row_naming_a_prefix_releases_nothing(self):
        rows = self.ring([("@gemini owed %d" % i, "main") for i in range(3)])
        self.post_ack(rows[1]["id"][:8])
        self.assertEqual(self.ring_count(), 3)

    def test_an_ack_in_another_room_for_the_same_id_releases_nothing(self):
        rows = self.ring([("@gemini project ask", "other-project"),
                          ("@gemini main ask", "main")])
        self.post_ack(rows[0]["id"])
        self.assertEqual(self.ring_count(), 2)

    def test_an_ack_row_from_the_seat_in_another_case_releases(self):
        rows = self.ring([("@gemini owed %d" % i, "main") for i in range(3)])
        self.post_ack(rows[1]["id"], who="Gemini")
        acks = [m for m in chat.read("main")[0] if m.get("ack")]
        self.assertEqual([m.get("from") for m in acks], ["Gemini"], acks)
        self.assertEqual(self.ring_count(), 2)

    def test_a_blocked_ack_releases_too(self):
        rows = self.ring([("@gemini owed %d" % i, "main") for i in range(3)])
        out, err = io.StringIO(), io.StringIO()
        with _tmp_declaring(["--seat", SEAT]), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = chat.cmd_chat(["ack", rows[1]["id"], "blocked"])
        self.assertEqual(rc, 0, err.getvalue())
        self.assertEqual(self.ring_count(), 2)

    def test_a_pre_start_row_beyond_the_back_window_stays(self):
        """The pull released rows 1 and 2; with the window too short to
        reach their starts they stay counted, and leave once it can."""
        from helm import beacon_doorbell as bd
        self.ring([("@gemini owed %d" % i, "main") for i in range(3)])
        self.strip_starts()
        out = self.pull("--room", "main", "--since",
                        str(self.since("main", "@gemini owed 1")))
        self.assertIn("@gemini owed 2", out)
        with mock.patch.object(bd, "BACK_BYTES", 4):
            self.assertEqual(self.counts(), (3, 1))
        self.assertEqual(self.counts(), (1, 1))

    def test_a_pre_start_row_behind_a_blank_line_is_found(self):
        self.ring([("@gemini owed 0", "main")])
        with open(chat.room_path("main"), "ab") as f:
            f.write(b"\n")
        for i in (1, 2):
            chat.post("@gemini owed %d" % i, who="bob", room="main")
        lines, _seen = self.follow(passes=2, clock=True)
        self.assertEqual([_unread(x) for x in lines], [3], lines)
        self.strip_starts()
        out = self.pull("--room", "main", "--since",
                        str(self.since("main", "@gemini owed 1")))
        self.assertIn("@gemini owed 2", out)
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(self.hook_texts(), ["@gemini owed 0"])


class AckedRowLeavesTheHookTest(ReleaseBase):
    """task/3164: a row the seat ACKED leaves the tool-boundary hook and the
    stop guard as it leaves the ring. The three read one ack predicate
    (`beacon_doorbell._acks`), so the ring count and the stop guard's count
    agree, and the hook passes the row without showing it and drops its
    hold.

    THE BOUND. The hook and the guard read acks only in the window they
    already read, SCAN_CAP bytes from the delivery cursor, so the per-call
    path reads no more of the room than it did. An ack past that window is
    not seen there: its row is shown and counted, which is the safe
    direction, and the ring, which reads to the end of the room, drops it."""

    def filler(self, room, n):
        """`n` plain rows in `room` that are addressed to nobody."""
        for i in range(n):
            chat.post("chatter %d %s" % (i, "x" * 200), who="bob", room=room)

    def test_an_acked_row_leaves_the_hook_and_the_guard_count(self):
        """RED before the cure: the stop guard counted 3 and the hook showed
        all three rows, the acked one included."""
        rows = self.ring([("@gemini owed %d" % i, "main") for i in range(3)])
        self.ack(rows[1])
        wake = seats_delivery._cursor("main", SEAT, SID, beacon=True)
        self.assertEqual(len(wake["held"]), 3, "the ring held all three rows")
        self.assertEqual(self.counts(), (2, 2))
        self.assertEqual(self.hook_texts(), ["@gemini owed 0", "@gemini owed 2"])
        self.assertEqual(self.counts(), (0, 0))
        wake = seats_delivery._cursor("main", SEAT, SID, beacon=True)
        self.assertNotIn("held", wake, "the acked row's hold was kept")

    def test_a_row_acked_before_any_ring_is_not_shown(self):
        """No waiter ran, so nothing rang or held the rows; the seat acked
        one. RED before the cure: the guard counted 2 and the hook showed
        both."""
        rows = [chat.post("@gemini owed %d" % i, who="bob") for i in range(2)]
        self.ack(rows[0])
        self.assertEqual(self.guard_count(), 1)
        self.assertEqual(self.hook_texts(), ["@gemini owed 1"])
        self.assertEqual(self.guard_count(), 0)

    def test_an_acked_row_in_another_room_leaves_the_hook_there(self):
        """RED before the cure: the guard counted 2 and the hook showed
        both rows of the other room."""
        rows = self.ring([("@gemini owed %d" % i, "other-project")
                          for i in range(2)])
        self.ack(rows[1])
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(self.hook_texts(), ["@gemini owed 0"])
        self.assertEqual(self.counts(), (0, 0))

    def test_an_ack_by_another_seat_leaves_the_row_on_the_hook(self):
        rows = self.ring([("@gemini @kimi owed %d" % i, "main")
                          for i in range(2)])
        self.ack(rows[1], seat=OTHER)
        self.assertEqual(self.counts(), (2, 2))
        self.assertEqual(self.hook_texts(pattern=r"owed \d"),
                         ["owed 0", "owed 1"])

    def test_an_ack_naming_a_prefix_leaves_the_row_on_the_hook(self):
        rows = self.ring([("@gemini owed %d" % i, "main") for i in range(2)])
        chat.post("", room="main", who=SEAT, ack=rows[1]["id"][:8],
                  ackstate="done")
        self.assertEqual(self.counts(), (2, 2))
        self.assertEqual(self.hook_texts(), ["@gemini owed 0", "@gemini owed 1"])

    def test_an_ack_past_the_window_the_hook_read_is_not_seen_there(self):
        """The bound, pinned: the ack lies past SCAN_CAP bytes of chatter,
        so the hook shows the acked row and the guard counts it, while the
        ring, which reads to the end of the room, drops it. Unpatched, the
        same rows go the other way (the arm above)."""
        rows = self.ring([("@gemini owed %d" % i, "other-project")
                          for i in range(3)])
        self.filler("other-project", 40)
        self.ack(rows[1])
        with mock.patch.object(seats_delivery, "SCAN_CAP", 4096):
            self.assertEqual(self.counts(), (2, 3))
            self.assertEqual(self.hook_texts(),
                             ["@gemini owed 0", "@gemini owed 1",
                              "@gemini owed 2"])

    def test_the_hook_reads_each_room_at_most_once_per_pass(self):
        """The ack check reads no more of the room: it asks the rows the
        pass already read. A read to the end of the room per tool call, as
        the ring's `_acked` does, would open the room a second time."""
        rows = self.ring([("@gemini owed %d" % i, "main") for i in range(3)])
        self.ack(rows[1])
        path, real, opened = chat.room_path("main"), open, []

        def counting(name, *a, **kw):
            if str(name) == path:
                opened[-1] += 1
            return real(name, *a, **kw)
        got = []
        with mock.patch("builtins.open", counting):
            for _ in range(3):
                opened.append(0)
                seats_delivery.deliver_any(session=SID, seat=SEAT,
                                           emit=got.append)
        self.assertEqual([re.search(r"@gemini owed \d", str(x)).group(0)
                          for x in got], ["@gemini owed 0", "@gemini owed 2"])
        self.assertTrue(opened[0] and max(opened) == 1, opened)


class AckIsThisSessionsTest(ReleaseBase):
    """task/3164 round 2: an ack releases a row from the tool-boundary hook
    and the stop guard only for the SESSION that wrote it, and never for a
    delegate. A delegate (a subagent or a Workflow agent) shares its seat's
    session and name, and its ack is not the seat's evidence, as its read is
    not (helm.pull_delivery). A co-named session's cursors are its own, so
    its ack does not hide the row from its sibling (the fan-out law in
    seats_delivery). An ack that comes before its row, or one that records
    no session, is not honoured there either. The ring stays per seat and
    still drops each of these rows.

    Every arm below asserts what main shows for the same rows, the row shown
    and counted; the arms in AckedRowLeavesTheHookTest are the controls, the
    same ack from this session, after its row, releasing it."""

    SID2 = "sibling-gemini-2"

    def swap_last_two_lines(self, room):
        """Put the room's last row before the one ahead of it, in place, on
        the same file: the ack the CLI door just wrote then precedes the row
        it names."""
        path = chat.room_path(room)
        with open(path, "r+b") as f:
            data = f.read()
            a, b = data.split(b"\n")[-3:-1]
            f.seek(len(data) - len(a) - len(b) - 2)
            f.write(b + b"\n" + a + b"\n")

    def test_a_delegates_ack_leaves_the_row_on_the_hook_and_the_guard(self):
        """The reader's probe. RED on the round-1 tip: the guard counted 1
        and the hook showed only row 0."""
        from helm import pull_delivery
        rows = self.ring([("@gemini owed %d" % i, "main") for i in range(2)])
        pull_delivery.mark_delegate(SID)
        self.ack(rows[1])
        acks = [m for m in chat.read("main")[0] if m.get("ack") == rows[1]["id"]]
        self.assertEqual(len(acks), 1, acks)
        self.assertNotIn("session", acks[0], "a delegate's ack names a session")
        self.assertEqual(self.counts(), (1, 2))
        self.assertEqual(self.hook_texts(), ["@gemini owed 0", "@gemini owed 1"])

    def test_a_conamed_sessions_ack_leaves_the_row_for_its_sibling(self):
        """RED on the round-1 tip: this session's guard counted 1 and its
        hook showed only row 0. The acking session's own hook passes the
        row: that half is the control."""
        seats.join(session=self.SID2, seat=SEAT, cwd="/tmp/p", room="main")
        rows = [chat.post("@gemini owed %d" % i, who="bob") for i in range(2)]
        self.ack(rows[1], session=self.SID2)
        self.assertEqual(self.guard_count(), 2)
        self.assertEqual(self.hook_texts(), ["@gemini owed 0", "@gemini owed 1"])
        got = []
        for _ in range(3):
            seats_delivery.deliver_any(session=self.SID2, seat=SEAT,
                                       emit=got.append)
        self.assertEqual([re.search(r"@gemini owed \d", str(x)).group(0)
                          for x in got], ["@gemini owed 0"])

    def test_an_ack_written_before_its_row_leaves_the_row_shown(self):
        """RED on the round-1 tip: the guard counted 1 and the hook showed
        only row 0. The ring reads acks only from a row's end on."""
        rows = [chat.post("@gemini owed %d" % i, who="bob") for i in range(2)]
        self.ack(rows[1])
        tail = chat.read("main")[0][-2:]
        self.assertEqual([tail[0].get("id"), tail[1].get("ack")],
                         [rows[1]["id"], rows[1]["id"]], tail)
        self.swap_last_two_lines("main")
        tail = chat.read("main")[0][-2:]
        self.assertEqual([tail[0].get("ack"), tail[1].get("id")],
                         [rows[1]["id"], rows[1]["id"]], tail)
        self.assertEqual(self.guard_count(), 2)
        self.assertEqual(self.hook_texts(), ["@gemini owed 0", "@gemini owed 1"])

    def test_an_ack_that_records_no_session_releases_only_the_ring(self):
        """An ack row older than the session field, or written past the CLI
        door: its provenance is unknown, so the hook and the guard keep the
        row, and the ring, per seat, drops it as before. RED on the round-1
        tip: the guard counted 2 and the hook skipped row 1."""
        rows = self.ring([("@gemini owed %d" % i, "main") for i in range(3)])
        chat.post("", room="main", who=SEAT, ack=rows[1]["id"],
                  ackstate="done")
        self.assertEqual(self.counts(), (2, 3))
        self.assertEqual(self.hook_texts(),
                         ["@gemini owed 0", "@gemini owed 1", "@gemini owed 2"])


class BulkAckReleasesEveryIdTest(ReleaseBase):
    """One `helm chat ack <id> <id> ...` writes one row per room naming every
    id (tests/test_ack_is_a_receipt.py), and `beacon_doorbell._acks`, the one
    predicate the ring, the tool-boundary hook and the stop guard read, reads
    every id on it, at the scope each reads it: the ring per seat, the hook
    and the guard per session.

    THE SYMPTOM. A seat cleared 31 owed rows with 31 calls, one room row
    each, because the verb took one id. A bulk row that `_acks` read as its
    first id alone would leave the other 30 owed, so the seat would ack them
    again, one by one."""

    def ack_many(self, rows, session=None):
        """`helm chat ack <id> <id> ...` by the seat through the CLI door."""
        out, err = io.StringIO(), io.StringIO()
        env = {"CLAUDE_CODE_SESSION_ID": session} if session else {}
        with mock.patch.dict(os.environ, env), \
                _tmp_declaring(["--seat", SEAT]), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = chat.cmd_chat(["ack"] + [r["id"] for r in rows])
        self.assertEqual(rc, 0, err.getvalue())
        return out.getvalue()

    def test_one_bulk_ack_releases_every_named_row_everywhere(self):  # noqa: VACUOUS_ASSERTION — the counts read (1, 1) and the hook shows row 4 before the final (0, 0)
        """RED before the cure: the verb read the second id as the ack
        state and refused."""
        rows = self.ring([("@gemini owed %d" % i, "main") for i in range(5)])
        n = len(chat.read("main")[0])
        self.ack_many(rows[:4])
        self.assertEqual(len(chat.read("main")[0]), n + 1)
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(self.hook_texts(), ["@gemini owed 4"])
        self.assertEqual(self.counts(), (0, 0))

    def test_a_bulk_ack_across_two_rooms_releases_both(self):  # noqa: VACUOUS_ASSERTION — the same rows ring 3 on the same fresh waiter in DoorbellReleaseControlTest, which acks none, and the counts read (3, 3) before the ack
        rows = self.ring([("@gemini project ask %d" % i, "other-project")
                          for i in range(2)] + [("@gemini main ask", "main")])
        self.assertEqual(self.counts(), (3, 3))
        self.ack_many(rows)
        self.assertEqual(self.counts(), (0, 0))
        self.assertEqual(self.hook_texts(), [])
        self.assertEqual(self.fresh_waiter(), [])

    def test_a_bulk_ack_from_a_sibling_session_leaves_every_row_here(self):  # noqa: VACUOUS_ASSERTION — this session's guard counts 2 and its hook shows both rows; the sibling's empty hook is its own ack honoured
        """The session scope holds for every id on a bulk row: a co-named
        sibling's bulk ack leaves both rows shown and counted for this
        session, and its own hook passes them."""
        sid2 = AckIsThisSessionsTest.SID2
        seats.join(session=sid2, seat=SEAT, cwd="/tmp/p", room="main")
        rows = [chat.post("@gemini owed %d" % i, who="bob") for i in range(2)]
        self.ack_many(rows, session=sid2)
        self.assertEqual(self.guard_count(), 2)
        self.assertEqual(self.hook_texts(), ["@gemini owed 0", "@gemini owed 1"])
        got = []
        for _ in range(3):
            seats_delivery.deliver_any(session=sid2, seat=SEAT,
                                       emit=got.append)
        self.assertEqual(got, [])


class BulkPathIsNamedFirstTest(ReleaseBase):
    """The ring and the stop guard name the bulk ack before any other way to
    clear the count, so a seat holding rows it already handled clears them in
    one call. The per-row form was the only one on offer, and a seat took it
    31 times. A ring whose unread rows are nothing to ack — @all rows,
    reactions, room rows, with no addressed row and no DM — names no ack at
    all: they clear by reading. A DM is its recipient's to ack, so a ring of
    a DM keeps the bulk line."""

    BULK = "helm chat ack <id> <id>"

    def test_the_ring_names_the_bulk_ack_before_the_pull(self):
        """RED before the cure: the ring named no ack at all."""
        from helm import beacon_doorbell as bd
        line = self.rung(["@gemini owed %d" % i for i in range(3)])
        self.assertIn(self.BULK, line)
        self.assertLess(line.index(self.BULK), line.index("pull, and read"))
        self.assertTrue(bd._TAIL.search(line),
                        "the tail no longer strips as fixed text: " + line)

    def test_an_all_only_ring_does_not_name_the_bulk_ack(self):
        """RED before the cure: the ring suggested an ack for a row an ack
        cannot clear, and offered no way that can. An @all row clears by
        reading."""
        from helm import beacon_doorbell as bd
        line = self.rung(["@all standup"])
        self.assertIn("1 @all", line)
        self.assertNotIn(self.BULK, line)
        self.assertIn("clears by reading, not by ack", line)
        self.assertTrue(bd._TAIL.search(line),
                        "the tail no longer strips as fixed text: " + line)

    def test_a_dm_only_ring_still_names_the_bulk_ack(self):
        """Control, green today: a DM is its recipient's to ack, so the ring
        of a DM keeps the bulk line."""
        from helm import beacon_doorbell as bd
        row, err = seats.dm(SEAT, "direct ask 1", who="bob")
        self.assertIsNone(err, err)
        lines, _seen = self.follow(passes=2, clock=True)
        self.assertEqual([_unread(x) for x in lines], [1], lines)
        line = lines[0]
        self.assertIn(self.BULK, line)
        self.assertTrue(bd._TAIL.search(line),
                        "the tail no longer strips as fixed text: " + line)

    def test_a_reaction_only_ring_does_not_name_the_bulk_ack(self):
        """RED on 68d7e3ee9e1: a ring of one reaction (0 addressed, 0 DM,
        0 @all) still named the bulk ack, and an ack of a reaction row is
        refused as not addressed. Only a ring holding an addressed row or a
        DM names the ack."""
        from helm import beacon_doorbell as bd
        chat.post("my row", who=SEAT, room="main")
        self.follow(passes=2, clock=True)
        chat.react(-1, "+1", room="main", who="bob")
        lines, _seen = self.follow(passes=2, clock=True)
        self.assertEqual([_unread(x) for x in lines], [1], lines)
        line = lines[0]
        self.assertIn("1 reactions", line)
        self.assertNotIn(self.BULK, line)
        self.assertIn("clears by reading, not by ack", line)
        self.assertTrue(bd._TAIL.search(line),
                        "the tail no longer strips as fixed text: " + line)

    def test_the_stop_guard_names_the_bulk_ack_first(self):
        """RED before the cure: the block named no ack at all."""
        for i in range(3):
            chat.post("@gemini owed %d" % i, who="bob")
        self.assertEqual(os.environ.get("HELM_SCRATCH_GC"), "0")
        blocks, _warns = seats.stop_guard(session=SID, room=self.HOME,
                                          seat=SEAT)
        inbox = [b for b in blocks if "undelivered message(s)" in b]
        self.assertEqual(len(inbox), 1, blocks)
        self.assertIn(self.BULK, inbox[0])
        self.assertLess(inbox[0].index(self.BULK),
                        inbox[0].index("helm chat read"))
        self.assertIn("helm chat catchup --including-mentions --apply",
                      inbox[0])


class PullPastAFoldedRunTest(ReleaseBase):

    def test_a_pull_past_a_folded_run_discharges_the_row_after_it(self):
        """300 DONE acks from another seat sit between two rows the seat
        owes. Printed one per row they run past what a harness shows whole,
        so only the rows in the read's head count and the row after them
        stays owed. Folded, they print one line (helm chat read), which the
        pull counts once. RED before the cure: counts (1, 1)."""
        chat.post("@gemini owed 0", who="bob")
        for i in range(300):
            chat.post("", room="main", who=OTHER, ack="%012x" % i,
                      ackstate="done")
        chat.post("@gemini owed 1", who="bob")
        lines, _seen = self.follow(passes=2, clock=True)
        self.assertEqual([_unread(x) for x in lines], [2], lines)
        out = self.pull("--room", "main")
        self.assertIn("@gemini owed 1", out)
        self.assertEqual(self.counts(), (0, 0))
        self.assertEqual(sum(1 for x in out.splitlines() if " ACK " in x), 1,
                         out)


if __name__ == "__main__":
    unittest.main()
