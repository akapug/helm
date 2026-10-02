#!/usr/bin/env python3
"""The beacon doorbell, driven through the REAL waiter entry point.

Every arm runs `helm chat wait --seat gemini --follow` through `seats.cmd`,
the path a Monitor arms, against a scratch chat dir and helm home (SeatsBase).
The waiter's own poll sleep is replaced by a step that moves a fake clock and
runs the arm's script, so a 60-second debounce or an hour of traffic runs in
well under a second and no arm sleeps.

THE SYMPTOM THESE ARMS PIN: a seat that resumes after a pause re-arms its
beacon and gets one Monitor event per pending row. Each event is a new user
turn that also runs the seat's inject hook, so fifty rows cost fifty turns.
The first two arms go RED on a tree without the doorbell and report the line
count they measured.
"""
import contextlib
import io
import os
import re
import sys
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import beacons, chat, moments, promptshape, relevance  # noqa: E402
from helm import seats, seats_delivery, seats_join  # noqa: E402
from tests import _notices  # noqa: E402
from tests._tmphome import declaring as _tmp_declaring  # noqa: E402
from tests._tmphome import session_for as _tmp_session_for  # noqa: E402
from tests.test_seats import SeatsBase  # noqa: E402

SEAT = "gemini"
SID = _tmp_session_for(SEAT)
NUDGE = ("[helm chat] more pending — `helm chat read` SHOWS them; addressed "
         "rows stay owed until you ACT or `helm chat catchup "
         "--including-mentions --apply` parks them (catchup is DRY-RUN "
         "without --apply — the flag is what makes it act)")


class _Stop(BaseException):
    """Ends one driven waiter; BaseException so no fail-open handler eats it."""


def _unread(line):
    m = re.search(r"doorbell: (\d+) unread", line)
    return int(m.group(1)) if m else None


class DoorbellBase(SeatsBase):
    HOME = "main"

    def setUp(self):
        super().setUp()
        # EOF baselines on the session the CLI waiter declares
        seats.join(session=SID, seat=SEAT, cwd="/tmp/p", room=self.HOME)
        self.t0 = 1_900_000_000.0
        self.clock = [self.t0]

    def follow(self, passes, script=None, flags=(), step=None, clock=False):
        """Run the waiter for `passes` poll passes -> (lines, seen).

        Pass k runs at t0 + step*(k-1) on the fake clock. After pass k the nap
        records how many lines the waiter had printed (`seen[k-1]`), moves the
        clock and runs `script(k, now)`. `clock=True` also points the
        doorbell's timing at the fake clock; the two RED arms leave it alone,
        because the tree they must go red on has no doorbell to point."""
        step = chat.POLL_S if step is None else step
        out, err = io.StringIO(), io.StringIO()
        seen, n = [], [0]

        def nap(_seconds):
            n[0] += 1
            seen.append(len(out.getvalue().splitlines()))
            if n[0] >= passes:
                raise _Stop
            self.clock[0] += step
            if script:
                script(n[0], self.clock[0])

        args = ["--seat", SEAT, "--follow"] + list(flags)
        fake_time = types.SimpleNamespace(time=time.time, sleep=nap)
        with contextlib.ExitStack() as stack:
            stack.enter_context(_tmp_declaring(args))
            stack.enter_context(mock.patch.object(seats_join, "time",
                                                  fake_time))
            if clock:
                from helm import beacon_doorbell
                stack.enter_context(mock.patch.object(
                    beacon_doorbell, "_now", lambda: self.clock[0]))
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            try:
                seats.cmd("wait", args, "main")
            except _Stop:
                pass
        self.stderr = err.getvalue()
        return out.getvalue().splitlines(), seen

    def since(self, room, text):
        """The `helm chat read --since` offset of the row carrying `text`."""
        rows, _total = chat.read(room)
        return next(i for i, m in enumerate(rows) if m.get("text") == text)

    def hook_pass(self, boundaries=1):
        """The seat READS: at each tool boundary its PostToolUse hook runs one
        delivery pass on the same session, which shows the oldest row the
        seat is still owed, in full, and moves the delivery cursor past it."""
        for _ in range(boundaries):
            seats_delivery.deliver_any(session=SID, seat=SEAT, emit=[].append)


class ArmingTest(DoorbellBase):

    def test_arming_with_fifty_pending_rows_rings_exactly_once(self):
        """RED without the doorbell: 50 pending rows streamed as 50 wake lines
        plus a `more pending` nudge after every fourth."""
        ids = [chat.post("@gemini backlog row %d" % i, who="bob")["id"]
               for i in range(50)]
        lines, _seen = self.follow(passes=20)
        self.assertEqual(len(lines), 1,
                         "measured %d lines for 50 pending rows; first:\n%s"
                         % (len(lines), "\n".join(lines[:6])))
        ring = lines[0]
        k = self.since("main", "@gemini backlog row 0")
        self.assertTrue(ring.startswith("[helm chat → gemini @"), ring)
        # ONE pull names the newest 24 by id; the 26 older are pulled behind
        # it from the oldest unread row of their room
        self.assertIn("] bob: @gemini backlog row 49 (+49 waiting — "
                      "helm chat read --id %s; and 26 older: helm chat read "
                      "--room main --since %d · "
                      % (",".join(reversed(ids[26:])), k), ring)
        self.assertIn("doorbell: 50 unread = 50 addressed, 0 DM, 0 @all · "
                      "50 new since the last ring", ring)
        self.assertIn("read what is addressed first", ring)
        self.assertNotIn("backlog row 48", ring, "a ring carries ONE row's text")

    def test_replay_of_a_resume_after_pause_rings_once(self):
        """RED without the doorbell: the resume-after-pause shape, ~50 rows
        mixing @all, @mentions and DMs, arriving before the seat re-arms."""
        asked = [seats.dm(SEAT, "direct one", who="bob")[0]["id"],
                 seats.dm(SEAT, "direct two", who="carol")[0]["id"]]
        for i in range(48):
            if i % 8 in (0, 3, 5):
                asked.append(chat.post("@gemini please look at lane %d" % i,
                                       who="seat-b")["id"])
            else:
                chat.post("@all standup item %d" % i, who="seat-c")
        asked.append(chat.post("@gemini the newest ask", who="kimi")["id"])
        lines, _seen = self.follow(passes=30)
        self.assertEqual(len(lines), 1,
                         "measured %d lines for the replay; first:\n%s"
                         % (len(lines), "\n".join(lines[:6])))
        ring = lines[0]
        # a DM leads over a newer mention, and ONE pull names every row to
        # act on, the lead first; the @all rows are only counted
        self.assertIn("] carol: direct two (+50 waiting — ", ring)
        self.assertIn("doorbell: 51 unread = 19 addressed, 2 DM, 30 @all",
                      ring)
        pull = re.search(r"\(\+50 waiting — helm chat read --id ([0-9a-f,]+) "
                         r"· 30 not addressed: read when idle · ", ring)
        self.assertIsNotNone(pull, ring)
        named = pull.group(1).split(",")
        self.assertEqual((named[0], sorted(named)), (asked[1], sorted(asked)))

    def test_a_waiter_that_exits_inside_the_window_leaves_the_rows_owed(self):  # noqa: VACUOUS_ASSERTION — the empty first run is paired with the second waiter's ring on the same stdout channel, asserted unconditionally below it
        """A drained row is a woken row, so the doorbell keeps it durable: the
        next waiter that arms rings for it at once."""
        def script(n, _now):
            if n == 1:
                chat.post("@gemini first", who="bob")
                chat.post("@gemini second", who="bob")
        first, _ = self.follow(passes=5, script=script, clock=True)
        second, seen = self.follow(passes=3, clock=True)
        self.assertEqual(first, [], "the window had not closed")
        self.assertEqual(len(second), 1, second)
        self.assertEqual(seen[0], 1, "the next arm rings on its first pass")
        self.assertIn("doorbell: 2 unread = 2 addressed", second[0])


class DebounceTest(DoorbellBase):

    def test_rows_inside_the_window_coalesce_into_one_ring_after_it(self):
        def script(n, _now):
            if 1 <= n <= 5:
                chat.post("@gemini burst %d" % n, who="bob")
        lines, seen = self.follow(passes=45, script=script, clock=True)
        self.assertEqual(len(lines), 1, lines)
        at = seen.index(1) + 1                 # the pass that rang
        drained = self.t0 + chat.POLL_S        # pass 2 drained burst 1
        rang = self.t0 + chat.POLL_S * (at - 1)
        self.assertGreaterEqual(rang - drained, 60.0)
        self.assertLess(rang - chat.POLL_S - drained, 60.0,
                        "the pass before the ring was still inside the window")
        self.assertIn("] bob: @gemini burst 5 (+4 waiting", lines[0])
        self.assertIn("doorbell: 5 unread = 5 addressed, 0 DM, 0 @all · "
                      "5 new since the last ring", lines[0])

    def test_rows_arriving_while_unread_add_no_line_until_the_window_closes(self):
        chat.post("@gemini pending a", who="bob")
        chat.post("@gemini pending b", who="bob")

        def script(n, _now):
            if n in (2, 10, 20):
                chat.post("@gemini late %d" % n, who="carol")
        lines, seen = self.follow(passes=40, script=script, clock=True)
        self.assertEqual(seen[0], 1, "the catch-up ring")
        drained = self.t0 + chat.POLL_S * 2    # pass 3 drained `late 2`
        closes = int((drained + 60.0 - self.t0) / chat.POLL_S) + 1
        self.assertEqual(seen[:closes - 1], [1] * (closes - 1),
                         "a row landing inside the window added a line")
        self.assertEqual(seen[closes - 1], 2, "the window closed and rang")
        self.assertEqual(len(lines), 2, lines)
        self.assertIn("doorbell: 5 unread = 5 addressed", lines[1])
        self.assertIn("3 new since the last ring", lines[1])

    def test_reading_lowers_the_count_and_one_new_row_rings_once(self):
        """(a) the seat reads: across three tool boundaries its delivery
        cursor passes the three rung rows, so they leave the count. The last
        ring is the control: not reading keeps the rung row in the count."""
        for i in range(3):
            chat.post("@gemini old %d" % i, who="bob")

        def script(n, _now):
            if n == 2:
                self.hook_pass(boundaries=3)
            elif n == 3:
                chat.post("@gemini fresh", who="carol")
            elif n == 40:
                chat.post("@gemini fresher", who="carol")
        lines, _seen = self.follow(passes=80, script=script, clock=True)
        self.assertEqual(len(lines), 3, lines)
        self.assertEqual([_unread(x) for x in lines], [3, 1, 2])
        self.assertIn("] carol: @gemini fresh (+0 waiting", lines[1])
        self.assertIn("1 new since the last ring", lines[2])


class UrgentAndCapTest(DoorbellBase):

    def test_a_dm_and_an_owner_row_ring_at_once_and_carry_what_is_pending(self):
        def script(n, _now):
            if n == 1:
                chat.post("@gemini peer one", who="bob")
                chat.post("@gemini peer two", who="bob")
            elif n == 3:
                seats.dm(SEAT, "a direct ask", who="carol")
            elif n == 6:
                chat.post("@gemini hold every land", who="daria",
                          origin="web")
        lines, seen = self.follow(passes=12, script=script, clock=True)
        self.assertEqual(len(lines), 2, lines)
        self.assertEqual(seen[3], 1, "the DM rang on the pass that drained it")
        self.assertIn("doorbell: 3 unread = 2 addressed, 1 DM, 0 @all",
                      lines[0])
        self.assertEqual(seen[6], 2, "the owner row rang at once")
        self.assertIn("] daria: @gemini hold every land", lines[1])
        self.assertIn("1 from the owner", lines[1])

    def test_all_twenty_times_in_an_hour_is_capped_and_the_counts_grow(self):
        step = 20.0
        post_every = 9                         # one @all per 180 s

        def script(n, _now):
            if n % post_every == 0 and n // post_every <= 20:
                chat.post("@all standup %d" % (n // post_every), who="bob")
        lines, seen = self.follow(passes=200, script=script, step=step,
                                  clock=True)
        first = self.t0 + step * seen.index(1)
        in_hour = [i for i in range(len(seen)) if seen[i] > (seen[i - 1] if i else 0)
                   and self.t0 + step * i < first + 3600]
        self.assertEqual(len(in_hour), 12, "rings inside the first hour")
        self.assertEqual(len(lines), 13, lines[-2:])
        self.assertEqual([_unread(x) for x in lines],
                         list(range(1, 13)) + [20])
        self.assertIn("20 @all", lines[-1])
        self.assertIn("8 new since the last ring", lines[-1])

    def test_an_owner_row_rings_past_the_cap(self):
        """The debounce ring at pass 32 takes the hour's one ring, so the
        next peer row waits; the owner row rings past the cap."""
        def script(n, _now):
            if n == 1:
                chat.post("@gemini before the cap", who="bob")
            elif n == 35:
                chat.post("@gemini capped peer", who="bob")
            elif n == 70:
                chat.post("@gemini owner word", who="daria", origin="web")
        with mock.patch.dict(os.environ, {"HELM_BEACON_RINGS_PER_H": "1"}):
            lines, seen = self.follow(passes=74, script=script, clock=True)
        self.assertEqual(seen[31:70], [1] * 39,
                         "the capped peer row rang past a cap of 1")
        self.assertEqual(len(lines), 2, lines)
        self.assertIn("] daria: @gemini owner word", lines[1])
        self.assertIn("doorbell: 3 unread", lines[1])


class AmbientTest(DoorbellBase):
    HOME = "team-g"

    def test_ambient_rows_never_ring_and_a_mention_still_does(self):
        asked = []

        def script(n, _now):
            if n <= 20:
                chat.post("ambient chatter %d" % n, who="bob", room="team-g")
            elif n == 60:
                asked.append(chat.post("@gemini a real ask", who="bob",
                                       room="team-g")["id"])
        lines, seen = self.follow(passes=100, script=script, clock=True)
        self.assertEqual(seen[59], 0, "an ambient row rang")
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("] bob: @gemini a real ask", lines[0])
        self.assertIn("helm chat read --id %s · " % asked[0], lines[0])


class ClipTest(DoorbellBase):

    def test_the_ring_carries_the_whole_body_on_one_line(self):
        """RED before the cure: the ring carried the first line clipped to
        160 characters, so the seat spent a read call on the rest."""
        chat.post("@gemini " + "x" * 300 + "\nsecond line", who="bob")
        lines, _seen = self.follow(passes=2, clock=True)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("] bob: @gemini " + "x" * 300 + " ⏎ second line "
                      "(+0 waiting", lines[0])


class ReadRuleTest(DoorbellBase):
    """What counts as READ, against a real delivery cursor and room file."""

    def test_a_cursor_on_another_file_is_behind_unless_the_file_was_replaced(self):
        from helm import beacon_doorbell as bd
        chat.post("@gemini a row", who="bob")
        st_room = os.stat(chat.room_path("main"))
        here = [st_room.st_dev, st_room.st_ino, 10 ** 9]
        gone = [st_room.st_dev, st_room.st_ino + 1, 10]

        def owed(at):
            return {"key": "k%r" % (at,), "room": "main", "at": at,
                    "kind": "addressed", "owner": False, "id": None}
        # The join baselined this cursor before `main` had a file, so it names
        # no file yet: the row on today's file is still unread, and the row on
        # a file that is no longer there has offsets that name nothing.
        st = dict(bd._fresh(), owed=[owed(here), owed(gone)])
        bd._forget_read(st, SEAT, SID)
        self.assertEqual([e["at"] for e in st["owed"]], [here])
        # Once the hook moves the cursor onto the file and past the row, it is
        # read; a row beyond the cursor on the same file is not.
        self.hook_pass()
        cur = seats_delivery._cursor("main", SEAT, SID)
        self.assertEqual((cur["dev"], cur["ino"]), tuple(here[:2]))
        passed = here[:2] + [cur["off"]]
        st = dict(bd._fresh(), owed=[owed(passed), owed(here)])
        bd._forget_read(st, SEAT, SID)
        self.assertEqual([e["at"] for e in st["owed"]], [here])

    def test_the_knobs_fall_back_or_floor_instead_of_breaking_the_bell(self):
        from helm import beacon_doorbell as bd
        with mock.patch.dict(os.environ, {"HELM_BEACON_RINGS_PER_H": "3",
                                          "HELM_BEACON_DEBOUNCE_S": "3"}):
            self.assertEqual((bd.rings_per_h(), bd.debounce_s()), (3, 3.0))
        for raw, rings, debounce in (("junk", 12, 60.0), ("inf", 12, 60.0),
                                     ("nan", 12, 60.0), ("0", 1, 0.0),
                                     ("-5", 1, 0.0)):
            with mock.patch.dict(os.environ, {"HELM_BEACON_RINGS_PER_H": raw,
                                              "HELM_BEACON_DEBOUNCE_S": raw}):
                self.assertEqual((bd.rings_per_h(), bd.debounce_s()),
                                 (rings, debounce), raw)


class RingIsNotDeliveryTest(DoorbellBase):
    """A ring ANNOUNCES rows and shows the text of one. It must not count as
    delivering them: the doorbell drains on the wake cursor, so the rows it
    rang stay owed to the tool-boundary hook, which shows each one in full,
    and the delivery cursor passes a row only once the seat was shown it."""

    def owed_rows(self):
        for i in range(3):
            chat.post("@gemini owed %d" % i, who="bob")

    def cursors(self):
        return (seats_delivery._cursor("main", SEAT, SID),
                seats_delivery._cursor("main", SEAT, SID, beacon=True))

    def hook_lines(self, passes):
        got = []
        for _ in range(passes):
            seats_delivery.deliver_any(session=SID, seat=SEAT, emit=got.append)
        return got

    def guard_count(self):
        """The stop-guard's own `N undelivered message(s)`, read from its
        block; a guard that finds nothing pending prints no inbox block. The
        stop guard's scratch reaper deletes real dead-session scratch, so it
        runs only once SeatsBase has switched the reaper off."""
        self.assertEqual(os.environ.get("HELM_SCRATCH_GC"), "0")
        blocks, warns = seats.stop_guard(session=SID, room="main", seat=SEAT)
        said = [int(m.group(1)) for text in blocks + warns
                for m in [re.search(r"(\d+) undelivered message\(s\)", text)]
                if m]
        self.assertLessEqual(len(said), 1, blocks + warns)
        return said[0] if said else 0

    def ring_count(self):
        """What the next ring would say is unread, from the durable state."""
        from helm import beacon_doorbell as bd
        st = bd._load(bd.state_path(SEAT))
        bd._forget_read(st, SEAT, SID)
        return _unread(bd.ring_line(st, SEAT))

    def test_the_hook_shows_each_rung_row_in_full(self):  # noqa: VACUOUS_ASSERTION — the absent `held` key at the end is paired with its two tokens after the first hook pass, asserted unconditionally on the same wake cursor
        """RED before the cure: the hook's first pass after the ring showed
        nothing and moved the delivery cursor past all three rows."""
        self.owed_rows()
        delivery0, _wake0 = self.cursors()
        lines, _seen = self.follow(passes=2, clock=True)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("doorbell: 3 unread = 3 addressed", lines[0])
        # The ring moved neither cursor past what the drain did: the delivery
        # cursor stands where the join put it, and the wake cursor ends at the
        # last drained row.
        delivery, wake = self.cursors()
        self.assertEqual({k: delivery.get(k) for k in ("dev", "ino", "off")},
                         {k: delivery0.get(k) for k in ("dev", "ino", "off")})
        size = os.path.getsize(chat.room_path("main"))
        self.assertEqual(wake["off"], size)
        first = self.hook_lines(1)
        delivery, _wake = self.cursors()
        self.assertEqual(len(first), 1,
                         "measured %d hook lines after the ring, delivery "
                         "cursor at %r of %d bytes" % (len(first),
                                                       delivery.get("off"),
                                                       size))
        self.assertIn("] bob: @gemini owed 0 (+2 waiting — helm chat read)",
                      first[0])
        self.assertLess(delivery["off"], size, "one pass passed every row")
        self.assertEqual(len(self.cursors()[1].get("held") or ()), 2,
                         "the two rows not yet shown stay owed to the hook")
        rest = self.hook_lines(3)
        self.assertEqual([re.search(r"@gemini owed \d", x).group(0)
                          for x in rest],
                         ["@gemini owed 1", "@gemini owed 2"])
        delivery, wake = self.cursors()
        self.assertEqual(delivery["off"], size)
        self.assertNotIn("held", wake, "a shown row is still held")

    def test_the_ring_count_is_the_stop_guard_count(self):
        """RED before the cure: the ring said 3 unread and the stop-guard
        counted 0 undelivered, because the drain had crossed the rows."""
        self.owed_rows()
        lines, _seen = self.follow(passes=2, clock=True)
        self.assertEqual(len(lines), 1, lines)
        self.assertEqual((_unread(lines[0]), self.guard_count()), (3, 3))
        self.hook_lines(1)
        self.assertEqual((self.ring_count(), self.guard_count()), (2, 2))

    def test_a_rung_row_is_owed_to_the_hook_and_not_to_the_census(self):
        """The beacon census asks which rows would have WOKEN a seat that was
        not woken; a rung row woke it, so the census does not count it (RED
        before the cure: the rung row made the seat undrained, the verdict
        that raises the DEAF-IN-EFFECT alarm and types into its pane). The
        stop-guard asks what the seat has not been shown, and counts it."""
        chat.post("@gemini rung and unread", who="bob")
        lines, _seen = self.follow(passes=2, clock=True)
        self.assertEqual(len(lines), 1, lines)
        self.assertEqual(self.guard_count(), 1)
        age, unreadable, evidence = beacons.undrained(SEAT, session=SID)
        self.assertEqual((age, unreadable, evidence["seen"]), (None, None, ()),
                         "the census counted a rung row as never woken")
        # the census still counts a row that never woke the seat
        chat.post("@gemini landed after the ring", who="bob")
        age, unreadable, evidence = beacons.undrained(SEAT, session=SID)
        self.assertEqual(len(evidence["seen"]), 1, evidence)

    def test_per_row_leaves_nothing_held_for_the_hook(self):  # noqa: VACUOUS_ASSERTION — the empty hook pass is paired with the three per-row lines on the same rows, asserted unconditionally above it
        """The per-row stream shows each row in full, so it holds nothing and
        the hook has nothing more to show."""
        self.owed_rows()
        lines, _seen = self.follow(passes=3, flags=["--per-row"])
        self.assertEqual(len(lines), 3, lines)
        _delivery, wake = self.cursors()
        self.assertNotIn("held", wake)
        self.assertEqual(self.hook_lines(1), [])


class BackstopTest(DoorbellBase):
    """Rows a ring announced and the seat has not read ring again once every
    HELM_BEACON_BACKSTOP_S (720 s), with no new row, while one of them was
    only counted, never a ring's lead (task/4019: a row a ring showed whole
    never leads again). The minute step puts pass k at t0 + 60*(k-1): pass 13
    is the backstop's first pass."""
    STEP = 60.0

    def owed_rows(self):
        for i in range(3):
            chat.post("@gemini owed %d" % i, who="bob")

    def test_unread_rows_ring_again_once_past_the_backstop(self):
        """RED before the cure: the catch-up ring was the last line."""
        self.owed_rows()
        lines, seen = self.follow(passes=23, step=self.STEP, clock=True)
        self.assertEqual(len(lines), 2, lines)
        self.assertEqual(seen[12], 2, "the pass at the backstop rang")
        # the catch-up ring showed `owed 2`; the backstop leads with the
        # newest row it only counted
        self.assertIn("] bob: @gemini owed 2 (+2 waiting — ", lines[0])
        self.assertIn("] bob: @gemini owed 1 (+2 waiting — ", lines[1])
        self.assertIn("doorbell: 3 unread = 3 addressed", lines[1])
        self.assertIn("0 new since the last ring", lines[1])

    def test_no_ring_before_the_backstop(self):  # noqa: VACUOUS_ASSERTION — `seen` is twelve non-zero line counts, the catch-up ring standing on every pass before the backstop
        self.owed_rows()
        _lines, seen = self.follow(passes=12, step=self.STEP, clock=True)
        self.assertEqual(seen, [1] * 12, "rang again before the backstop")

    def test_rows_the_seat_read_get_no_backstop(self):  # noqa: VACUOUS_ASSERTION — the silent backstop is paired with the partial-read arm below it, which rings on the same schedule for the one row left unread
        self.owed_rows()

        def script(n, _now):
            if n == 1:
                self.hook_pass(boundaries=3)
        lines, _seen = self.follow(passes=23, step=self.STEP, script=script,
                                   clock=True)
        self.assertEqual(len(lines), 1, lines)

    def test_the_backstop_counts_only_what_is_still_unread(self):
        self.owed_rows()

        def script(n, _now):
            if n == 1:
                self.hook_pass()
        lines, seen = self.follow(passes=14, step=self.STEP, script=script,
                                  clock=True)
        self.assertEqual(len(lines), 2, lines)
        self.assertEqual(seen[12], 2)
        self.assertIn("] bob: @gemini owed 1 (+1 waiting — ", lines[1])
        self.assertIn("doorbell: 2 unread", lines[1])

    def test_the_backstop_waits_for_the_hourly_cap(self):
        """A backstop ring counts against the cap like any broadcast ring:
        with a cap of 1, the debounce ring at t0+120 holds it until t0+3720.
        Two rows, so the backstop has a row the debounce ring only counted."""
        def script(n, _now):
            if n == 1:
                chat.post("@gemini after arming", who="bob")
                chat.post("@gemini and one more", who="bob")
        with mock.patch.dict(os.environ, {"HELM_BEACON_RINGS_PER_H": "1"}):
            lines, seen = self.follow(passes=66, step=self.STEP,
                                      script=script, clock=True)
        self.assertEqual(seen[2], 1, "the debounce ring")
        self.assertEqual(seen[61], 1, "the backstop rang past the cap")
        self.assertEqual(seen[62], 2, "the backstop rang once the cap freed")
        self.assertEqual(len(lines), 2, lines)

    def test_an_idle_waiter_takes_the_state_lock_only_when_something_is_due(self):
        """The backstop is reached without a new event, from a time the waiter
        keeps in memory: an idle pass before it takes no lock."""
        from helm import beacon_doorbell as bd
        self.owed_rows()
        taken, real = [], bd._flocked

        # The stub forwards the call's keywords: the doorbell's take is the
        # one `unlocked_ok=True` site (task/2520), and a stub that took the
        # path alone raised TypeError there, which the waiter's fault guard
        # swallowed, so no pass rang and none counted.
        def counting(path, **kw):
            taken.append(path)
            return real(path, **kw)
        with mock.patch.object(bd, "_flocked", counting):
            lines, seen = self.follow(passes=81, step=10.0, clock=True)
        self.assertEqual(len(lines), 2, lines)
        self.assertEqual(seen[71:73], [1, 2], "the pass at t0+720 rang")
        self.assertEqual(len(taken), 2,
                         "the state lock was taken on %d of 81 passes"
                         % len(taken))

    def test_the_backstop_knob_falls_back_or_floors(self):  # noqa: VACUOUS_ASSERTION — every case compares the knob against a non-zero number of seconds
        from helm import beacon_doorbell as bd
        for raw, want in (("", 720.0), ("900", 900.0), ("junk", 720.0),
                          ("nan", 720.0), ("5", 60.0), ("-1", 60.0)):
            with mock.patch.dict(os.environ,
                                 {"HELM_BEACON_BACKSTOP_S": raw}):
                self.assertEqual(bd.backstop_s(), want, raw)


class CapBypassTest(DoorbellBase):
    """A DM, an owner row and the arming ring bypass the hourly cap as well as
    the debounce, and a ring that bypasses the cap does not count toward it."""

    def test_a_dm_rings_past_the_cap(self):
        """RED before the cure: the cap was checked before the DM."""
        def script(n, _now):
            if n == 1:
                chat.post("@gemini a peer ask", who="bob")
            elif n == 40:
                seats.dm(SEAT, "a direct ask", who="carol")
        with mock.patch.dict(os.environ, {"HELM_BEACON_RINGS_PER_H": "1"}):
            lines, seen = self.follow(passes=44, script=script, clock=True)
        self.assertEqual(seen[31], 1, "the debounce ring filled the cap")
        self.assertEqual(seen[40], 2, "the DM rang on the pass that drained it")
        self.assertIn("] carol: a direct ask", lines[1])

    def test_the_arming_ring_rings_past_the_cap(self):
        """RED before the cure: the cap was checked before the catch-up."""
        def script(n, _now):
            if n == 1:
                chat.post("@gemini a peer ask", who="bob")
        with mock.patch.dict(os.environ, {"HELM_BEACON_RINGS_PER_H": "1"}):
            first, _seen = self.follow(passes=33, script=script, clock=True)
            chat.post("@gemini while no waiter was armed", who="bob")
            second, seen = self.follow(passes=2, clock=True)
        self.assertEqual(len(first), 1, first)
        self.assertEqual(seen[0], 1, "the arming ring waited for the cap")
        self.assertIn("] bob: @gemini while no waiter was armed", second[0])

    def test_a_ring_past_the_cap_does_not_count_toward_it(self):
        """RED before the cure: the arming ring and the DM ring each used the
        hour's one ring, so the DM and then the peer row waited for the cap."""
        seats.dm(SEAT, "direct at arming", who="carol")

        def script(n, _now):
            if n == 2:
                seats.dm(SEAT, "another direct", who="carol")
            elif n == 4:
                chat.post("@gemini owner word", who="daria", origin="web")
            elif n == 6:
                chat.post("@gemini a peer ask", who="bob")
        with mock.patch.dict(os.environ, {"HELM_BEACON_RINGS_PER_H": "1"}):
            lines, _seen = self.follow(passes=40, script=script, clock=True)
        self.assertEqual(len(lines), 4, lines)
        self.assertIn("] carol: direct at arming", lines[0])
        self.assertIn("] carol: another direct", lines[1])
        self.assertIn("] daria: @gemini owner word", lines[2])
        self.assertIn("] bob: @gemini a peer ask", lines[3])


class LeadRankTest(DoorbellBase):
    """The owner's row leads, then a DM, then a row addressed to the seat,
    then an @all, each a person's or a seat's before a watcher's; reactions
    and room rows after those, again a person before a watcher. An @all never
    leads while a row addressed to the seat is unread."""

    def test_a_watchers_mention_leads_over_a_persons_all(self):
        """RED before the cure: the person's @all led, author first, and the
        seat hunted for the mention the ring did not show."""
        chat.post("@all standup now", who="carol")
        chat.post("@gemini owed rows are stale", who="owed-bot")
        lines, _seen = self.follow(passes=2, clock=True)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("] owed-bot: @gemini owed rows are stale (+1 waiting — ",
                      lines[0])

    def test_the_lead_order(self):  # noqa: VACUOUS_ASSERTION — eleven ranks compared whole against their sorted copy, and their count against the distinct count
        from helm import beacon_doorbell as bd
        person = "[helm chat → gemini @t] bob: x"
        watcher = "[helm chat → gemini @t] owed-bot: x"
        order = [bd._rank("room", True, person),
                 bd._rank("direct", False, person),
                 bd._rank("direct", False, watcher),
                 bd._rank("addressed", False, person),
                 bd._rank("addressed", False, watcher),
                 bd._rank("all", False, person),
                 bd._rank("all", False, watcher),
                 bd._rank("react", False, person),
                 bd._rank("room", False, person),
                 bd._rank("react", False, watcher),
                 bd._rank("room", False, watcher)]
        self.assertEqual(sorted(order), order)
        self.assertEqual(len(set(order)), len(order))


class CatchupHoldTest(DoorbellBase):
    """Catchup parks rows by moving the delivery cursor past them. A hold on a
    parked row is owed to nobody: no hook pass crosses it again, so it would
    pin the room's rotation at that row and keep the hook from moving the
    wake cursor on."""

    def test_catchup_drops_the_holds_on_the_rows_it_parks(self):
        """RED before the cure: the three holds outlived the park."""
        for i in range(3):
            chat.post("@all item %d" % i, who="bob")
        lines, _seen = self.follow(passes=2, clock=True)
        self.assertEqual(len(lines), 1, lines)
        wake = seats_delivery._cursor("main", SEAT, SID, beacon=True)
        self.assertEqual(len(wake.get("held") or ()), 3, wake)
        out = seats.catchup(SEAT, apply=True, session=SID)
        self.assertEqual(out["parked"], 3, out)
        wake = seats_delivery._cursor("main", SEAT, SID, beacon=True)
        self.assertEqual(wake.get("held"), None, wake)
        # Nothing behind the park holds the room: a hold, if any, is the
        # seat's own cursor at the end of the file, which retains no row.
        st = os.stat(chat.room_path("main"))
        self.assertIn(seats.rotation_hold_offset("main", st.st_dev, st.st_ino,
                                                 []), (None, st.st_size))


class PerRowTest(DoorbellBase):

    def test_per_row_keeps_the_old_stream_byte_for_byte(self):
        for i in range(6):
            chat.post("@gemini row %d" % i, who="bob")
        ts = {m["text"]: m["ts"] for m in chat.read("main")[0]}
        lines, _seen = self.follow(passes=4, flags=["--per-row"])

        def row(i, waiting):
            tail = (" (+%d waiting — helm chat read)" % waiting) if waiting \
                else ""
            return ("[helm chat → gemini @%s] bob: @gemini row %d%s"
                    % (ts["@gemini row %d" % i], i, tail))
        self.assertEqual(lines, [row(0, 5), row(1, 4), row(2, 3), row(3, 2),
                                 NUDGE, row(4, 1), row(5, 0)])

    def test_a_per_row_rearm_rings_the_rows_a_doorbell_waiter_drained(self):  # noqa: VACUOUS_ASSERTION — the empty first run is paired with the per-row arm's one ring on the same stdout channel, asserted unconditionally below it
        """A doorbell waiter drains two mentions inside the window and exits;
        the seat re-arms with --per-row. Both rows sit past the wake cursor,
        so the per-row stream has nothing for them, and the hook, which still
        owes them, runs only at a tool boundary an idle seat never reaches:
        the per-row arm rings the durable state once, or the rows are never
        announced (RED before the cure: 0 lines)."""
        def script(n, _now):
            if n == 1:
                chat.post("@gemini first", who="bob")
                chat.post("@gemini second", who="bob")
        first, _ = self.follow(passes=5, script=script, clock=True)
        self.assertEqual(first, [], "the window had not closed")
        second, seen = self.follow(passes=3, clock=True, flags=["--per-row"])
        hook = []
        seats_delivery.deliver_any(session=SID, seat=SEAT, emit=hook.append)
        self.assertEqual(len(hook), 1, hook)
        self.assertIn("] bob: @gemini first (+1 waiting", hook[0],
                      "the woken seat's first tool boundary shows the row")
        self.assertEqual(len(second), 1,
                         "measured %d lines from the --per-row re-arm for 2 "
                         "drained rows: %r" % (len(second), second))
        self.assertEqual(seen[0], 1, "the ring is the arm's first line")
        self.assertIn("doorbell: 2 unread = 2 addressed", second[0])

    def test_per_row_is_part_of_the_waiter_behavior(self):  # noqa: VACUOUS_ASSERTION — the absent key on the plain spec is paired with its presence on the per-row spec, asserted unconditionally by equality in the same method
        plain = beacons.requested_waiter_spec("main")
        per_row = beacons.requested_waiter_spec("main", per_row=True)
        self.assertNotIn("per_row", plain, "older registry rows stay equal")
        self.assertEqual(per_row, dict(plain, per_row=True))
        argv = ["python3", "/x/helm", "chat", "wait", "--seat", SEAT,
                "--follow", "--room", "main"]
        self.assertEqual(beacons.waiter_spec(1, argv=argv, env={}), plain)
        self.assertEqual(beacons.waiter_spec(1, argv=argv + ["--per-row"],
                                             env={}), per_row)


class ConsumerTest(DoorbellBase):
    """Every reader of wake lines reads a ring unchanged: promptshape and
    relevance strip its header and tail, moments finds its author."""

    def ring(self, text, who, extra=0):
        for i in range(extra):
            chat.post("@gemini older %d" % i, who="bob")
        chat.post(text, who=who)
        lines, _seen = self.follow(passes=2, clock=True)
        self.assertEqual(len(lines), 1, lines)
        return _notices.monitor(lines[0])

    def test_readers_strip_a_ring_to_its_author_and_first_line(self):
        notice = self.ring("@gemini can you review this", "kimi", extra=3)
        self.assertEqual(promptshape.substance(notice),
                         "kimi: @gemini can you review this")
        self.assertEqual(relevance.label_form(notice),
                         "kimi: @gemini can you review this")
        got = moments.classify(notice)
        self.assertEqual((got.kind, got.authors, got.waiting),
                         (moments.PEER, ("kimi",), True))

    def test_a_one_row_ring_from_a_watcher_is_still_a_machine_wake(self):
        """`+0 waiting` hides no author, so a watcher's lone @all ring stays
        the machine-broadcast kind its per-row line had."""
        notice = self.ring("@all three lanes are stale", "stale-bot")
        got = moments.classify(notice)
        self.assertEqual((got.kind, got.authors, got.waiting),
                         (moments.MACHINE, ("stale-bot",), False))
        self.assertIn("arrival.machine-broadcast", moments.signatures(notice))

    def test_the_lead_row_survives_the_live_monitor_filters(self):
        """Two live seats filter their beacon by row text: one KEEPS only
        lines naming the seat, one DROPS lines from watchers. Each ring is one
        line now, so the lead must be the row those filters are about: the
        seat's own mention over a newer @all, and a person over a newer
        watcher row."""
        chat.post("@gemini your review is due", who="bob")
        chat.post("@gemini owed rows are stale", who="owed-bot")
        chat.post("@all standup now", who="carol")
        lines, _seen = self.follow(passes=2, clock=True)
        self.assertEqual(len(lines), 1, lines)
        ring = lines[0]
        keep = re.compile(r"@gemini")                    # the positive filter
        drop = re.compile(r"owed-bot|proxywatch")        # the negative filter
        self.assertIn("] bob: @gemini your review is due (+2 waiting", ring)
        self.assertTrue(keep.search(ring) and not drop.search(ring), ring)
        self.assertIn("doorbell: 3 unread = 2 addressed, 0 DM, 1 @all", ring)

    def test_the_usage_states_the_doorbell_contract(self):
        usage = chat.HELP["wait"]
        self.assertIn("[--per-row]", usage.splitlines()[0])
        for words in ("DOORBELL", "--per-row restores one line per matching",
                      "After a ring, pull with helm chat read and read what "
                      "is\n  addressed first"):
            self.assertIn(words, usage)


if __name__ == "__main__":
    unittest.main()
