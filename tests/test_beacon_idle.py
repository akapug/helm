#!/usr/bin/env python3
"""An idle `helm chat wait --follow` waits without working (task/3873).

A whole delivery pass at every POLL_S tick costs an idle waiter 7-10% of a
core, measured on 32 of them. Each arm drives the real
waiter, `seats.wait(follow=True)`, on a scratch estate (SeatsBase) under a
fake clock: the waiter's poll sleep moves the clock and runs the arm's
writes, so an idle hour runs in well under a second and no arm sleeps.

The first arm goes RED on a tree without helm.beacon_idle: an idle hour ran
1,801 passes there (one per tick) and about 0.8 s of this thread's CPU.
Every other arm is a wake that must survive the cure, under both watches
(inotify, and the stat fallback a host without it uses).
"""
import contextlib
import os
import sys
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import beacon_doorbell, beacon_idle, chat, seats  # noqa: E402
from helm import seats_common, seats_cursor, seats_join  # noqa: E402
from tests.test_seats import SeatsBase  # noqa: E402

SEAT, SID = "gemini", "s-idle"
#: This thread's CPU for an idle hour of the waiter, a bound with a wide
#: margin on both sides: measured 0.09-0.11 s after the cure and 0.80-1.03 s
#: before it, on the same estate.
IDLE_CPU_S = 0.5


class _Waiter(SeatsBase):
    STAMPS = False               # True: the stat fallback, not inotify

    def setUp(self):
        super().setUp()
        seats.join(session=SID, seat=SEAT, cwd="/tmp/p")
        self.clock = [1_900_000_000.0]
        if self.STAMPS:
            p = mock.patch.object(beacon_idle, "_kernel", lambda seat: None)
            p.start()
            self.addCleanup(p.stop)

    def follow(self, seconds, at=None, doorbell=False):
        """Run the waiter for `seconds` of fake time -> (lines, passes, beats,
        cpu). `at` maps a fake second to a write, run when the clock crosses
        it. `lines` pairs each emitted line with its fake second; `passes`
        are the fake seconds of the waiter's delivery passes, `beats` those
        of its presence beats (a delivery call with sink_usable=False); `cpu`
        is this thread's CPU time over the wait."""
        at, t0 = dict(at or {}), self.clock[0]
        lines, passes, beats = [], [], []

        def nap(s):
            before = self.clock[0] - t0
            self.clock[0] += s
            for t in sorted(at):
                if before < t <= self.clock[0] - t0:
                    at.pop(t)()

        real = seats_join.deliver_any

        def counted(**kw):
            (beats if kw.get("sink_usable") is False else passes).append(
                self.clock[0] - t0)
            return real(**kw)

        fake = types.SimpleNamespace(time=lambda: self.clock[0], sleep=nap)
        with contextlib.ExitStack() as stack:
            for mod in (seats_join, beacon_idle):
                stack.enter_context(mock.patch.object(mod, "time", fake))
            stack.enter_context(mock.patch.object(
                beacon_doorbell, "_now", lambda: self.clock[0]))
            stack.enter_context(mock.patch.object(
                seats_join, "deliver_any", counted))
            cpu = time.thread_time()
            self.returned = seats.wait(
                seat=SEAT, session=SID, follow=True, timeout=seconds,
                doorbell=doorbell,
                emit=lambda line: lines.append((self.clock[0] - t0, line)))
            cpu = time.thread_time() - cpu
        self.ended = self.clock[0] - t0
        return lines, sorted(set(passes)), beats, cpu


class AnIdleWaiterCostsAlmostNothingTest(_Waiter):

    def test_an_idle_hour_runs_a_pass_per_safety_period_not_per_tick(self):  # noqa: VACUOUS_ASSERTION — passes[:2] == [0.0, SAFETY_S] and a non-empty beats list are unconditional positive controls on both counted observables
        self.follow(0.01, doorbell=True)   # imports and one arming, unmeasured
        _lines, passes, beats, cpu = self.follow(3600, doorbell=True)
        self.assertEqual(passes[:2], [0.0, beacon_idle.SAFETY_S],
                         "MUST-HIT: the arming pass or the safety pass did "
                         "not run, so the count below proves nothing")
        self.assertLessEqual(
            len(passes), 3600 // beacon_idle.SAFETY_S + 2,
            "an idle hour ran %d delivery passes (one per %.0f s tick is "
            "1,801): %s" % (len(passes), chat.POLL_S, passes[:12]))
        self.assertLess(cpu, IDLE_CPU_S,
                        "an idle hour cost %.3f s of CPU over %d passes"
                        % (cpu, len(passes)))
        # THE PRESENCE BEAT NEVER LAPSES: the seat stays fresh while idle
        stamped = sorted(set(passes) | set(beats))
        gaps = [b - a for a, b in zip(stamped, stamped[1:])]
        self.assertTrue(beats, "an idle waiter never beat presence")
        self.assertLessEqual(max(gaps), beacon_idle.BEAT_S + chat.POLL_S)
        self.assertLess(beacon_idle.BEAT_S + chat.POLL_S,
                        seats_common.FRESH_S)

    @unittest.skipUnless(sys.platform.startswith("linux"),
                         "inotify is a Linux interface")
    def test_on_linux_the_waiter_reads_the_kernel_not_a_stat_per_room(self):
        watch = beacon_idle.Idle(SEAT, SID, lambda: None).watch
        self.assertIsInstance(watch, beacon_idle._Kernel)
        chat.post("a row", who="bob", room="side")
        self.assertIn("side", watch.changed())


class ARowWrittenWhileIdleWakesAtTheNextTickTest(_Waiter):
    """The bound a new row waits in is one tick, as it was before the cure."""

    ROWS = (
        ("an @mention in a room",
         lambda: chat.post("@gemini ping", who="bob"), False, "@gemini ping"),
        ("an @all", lambda: chat.post("@all standup now", who="bob"), False,
         "@all standup now"),
        ("an @mention in a room born while the waiter idles",
         lambda: chat.post("@gemini over here", who="bob", room="brand-new"),
         False, "#brand-new"),
        ("the seat's first DM, rung at once by the doorbell",
         lambda: seats.dm(SEAT, "psst", who="carol"), True, "carol: psst"),
    )

    def test_each_row_wakes_the_idle_waiter_one_tick_after_its_write(self):  # noqa: VACUOUS_ASSERTION — ROWS is a four-row literal, so the loop body and its positive line assertion always run
        for what, write, doorbell, said in self.ROWS:
            with self.subTest(what):
                lines, passes, _beats, _cpu = self.follow(
                    40, at={20: write}, doorbell=doorbell)
                self.assertEqual([p for p in passes if p < 20], [0.0],
                                 "the waiter was not idle before the write")
                self.assertEqual([t for t, line in lines if said in line],
                                 [20.0], lines)

    def test_another_seats_dm_runs_no_pass(self):
        _lines, passes, _beats, _cpu = self.follow(
            40, at={20: lambda: seats.dm("carol", "not yours", who="bob")})
        self.assertEqual(passes, [0.0])

    def test_the_waiter_still_ends_at_its_own_deadline(self):  # noqa: VACUOUS_ASSERTION — the fake clock ending at exactly 37.0 is the positive observable; None is what a follow waiter returns at its deadline
        self.follow(37)
        self.assertIsNone(self.returned)
        self.assertEqual(self.ended, 37.0)


class ARowWrittenWhileIdleWakesOnTheStatFallbackTest(
        ARowWrittenWhileIdleWakesAtTheNextTickTest):
    """The same three arms on the stat fallback. They are spelled out, not
    only inherited, so the land gate's test-method count (which reads the
    methods a class body defines) matches what the runner collects."""
    STAMPS = True

    def test_each_row_wakes_the_idle_waiter_one_tick_after_its_write(self):
        super().test_each_row_wakes_the_idle_waiter_one_tick_after_its_write()

    def test_another_seats_dm_runs_no_pass(self):
        super().test_another_seats_dm_runs_no_pass()

    def test_the_waiter_still_ends_at_its_own_deadline(self):
        super().test_the_waiter_still_ends_at_its_own_deadline()


class TheDoorbellStillRingsOnItsOwnClockTest(_Waiter):

    def test_a_debounced_ring_lands_on_time_without_rereading_the_state(self):
        """Reading the doorbell state at every pass while rows wait unrung
        reads every room tail behind them each time. Plain rows in another
        room run a pass at every tick here, and the state is read only when
        the ring can be due: at the row's arrival and at its debounce end."""
        reads, real = [], beacon_doorbell._forget_read

        def counted(*a, **kw):
            reads.append(self.clock[0] - t0)
            return real(*a, **kw)

        churn = {t: (lambda t=t: chat.post("churn %d" % t, who="bob",
                                           room="busy"))
                 for t in range(22, 80, 2)}
        at = dict(churn)
        at[20] = lambda: chat.post("@gemini ring me later", who="bob")
        t0 = self.clock[0]
        with mock.patch.object(beacon_doorbell, "_forget_read", counted):
            lines, passes, _beats, _cpu = self.follow(100, at=at,
                                                      doorbell=True)
        due = 20 + beacon_doorbell.debounce_s()
        self.assertTrue(set(churn) <= set(passes),
                        "MUST-HIT: the churn did not run a pass at each tick")
        self.assertEqual([t for t in reads if 20 < t < due], [])
        self.assertEqual([t for t, _line in lines], [due])

    def test_a_backlog_that_fills_the_drain_exactly_still_rings_at_once(self):
        """The arming pass drains a whole DRAIN_PASS of rows, so its drain is
        cut short and it rings nothing. The next pass drains no row, and it
        must still read the state: the catch-up ring is due then, not at the
        end of a debounce window or an hourly cap."""
        for i in range(4):
            chat.post("@gemini backlog %d" % i, who="bob")
        with mock.patch.object(beacon_doorbell, "DRAIN_PASS", 4):
            lines, passes, _beats, _cpu = self.follow(10, doorbell=True)
        self.assertEqual(passes[:2], [0.0, chat.POLL_S])
        self.assertEqual([t for t, _line in lines], [chat.POLL_S], lines)
        self.assertIn("4 unread", lines[0][1])


class TheIdleGateNeverStrandsARoomTest(_Waiter):
    """What `Idle.skip` answers when its inputs fail or go stale."""

    def idle(self):
        clock = types.SimpleNamespace(time=lambda: self.clock[0])
        p = mock.patch.object(beacon_idle, "time", clock)
        p.start()
        self.addCleanup(p.stop)
        idle = beacon_idle.Idle(SEAT, SID, lambda: None)
        self.assertFalse(idle.skip(), "the arming tick ran no pass")
        return idle

    def test_a_watch_that_raises_runs_a_pass_and_the_waiter_lives(self):  # noqa: VACUOUS_ASSERTION — False is skip()'s positive answer (a pass runs); a raise, the defect, errors the arm
        """A relative HELM_HOME whose cwd was removed makes chat_dir raise."""
        with mock.patch.object(beacon_idle, "_kernel", lambda seat: None):
            idle = self.idle()
        with mock.patch.object(chat, "chat_dir",
                               side_effect=FileNotFoundError("cwd gone")):
            self.assertFalse(idle.skip())

    def test_after_the_safety_pass_every_room_is_checked_again(self):
        """A cursor that moved back makes a room dirty with no write to it.
        The safety pass reads only a slice of the rooms, so the tick after
        it checks every room's cursor, and a room still dirty keeps a pass
        at every tick until a pass reads it."""
        chat.post("a plain row", who="bob")
        self.follow(1)                   # beacon cursors at the end of main
        idle = self.idle()
        self.clock[0] += chat.POLL_S
        self.assertTrue(idle.skip(), "MUST-HIT: the estate was not clean")
        os.remove(seats_cursor.beacon_cursor_path("main", SEAT, SID))
        self.clock[0] += chat.POLL_S
        self.assertTrue(idle.skip(), "a room nobody wrote ran a pass")
        self.clock[0] += beacon_idle.SAFETY_S
        self.assertFalse(idle.skip(), "the safety pass did not run")
        self.clock[0] += chat.POLL_S
        self.assertFalse(idle.skip(), "main is still behind its cursor and "
                         "the tick after the safety pass skipped it")

    def test_a_room_the_gate_finds_behind_its_cursor_is_read_not_skipped(
            self):
        """The waiter's quiet proof (seats_roomscan.QuietRooms, task/3848)
        skips a pass while every room log is at the stamp it was proved
        quiet at, so it cannot see a cursor moved back with no write. The
        gate sees one after the safety pass and must drop that room's proof:
        kept, it runs a pass at every tick, each skipped by the proof, and
        the room is never read again."""
        chat.post("a plain row", who="bob")
        path = seats_cursor.beacon_cursor_path("main", SEAT, SID)
        _lines, passes, _beats, _cpu = self.follow(
            700, at={20: lambda: os.remove(path)})
        read = beacon_idle.SAFETY_S + chat.POLL_S   # the tick after the safety
        self.assertTrue(os.path.exists(path), "no pass read main again")
        self.assertEqual(passes, [0.0, beacon_idle.SAFETY_S, read,
                                  read + beacon_idle.SAFETY_S])

    @unittest.skipUnless(sys.platform.startswith("linux"),
                         "inotify is a Linux interface")
    def test_a_dm_directory_the_kernel_would_not_watch_is_checked(self):
        """dm/ exists but its watch cannot be added (ENOSPC, ENOMEM,
        EACCES) while the chat directory's can: a DM then writes no event
        this watch reads, so it answers every room, not an empty set."""
        seats.dm("carol", "makes dm/", who="bob")
        real = beacon_idle._Kernel._add

        def add(watch, path):
            return None if path.endswith(os.sep + "dm") else real(watch, path)

        with mock.patch.object(beacon_idle._Kernel, "_add", add):
            watch = beacon_idle._kernel(SEAT)
            self.assertIsNotNone(watch.top, "MUST-HIT: no chat dir watch")
            seats.dm(SEAT, "psst", who="carol")
            got = watch.changed()
        self.assertTrue(got is None or seats_common.dm_lane(SEAT) in got,
                        "a DM to the seat reported %r" % (got,))


if __name__ == "__main__":
    unittest.main()
