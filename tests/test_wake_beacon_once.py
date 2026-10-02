#!/usr/bin/env python3
"""The one-shot beacon: `helm chat wait --follow --once` (task/4019 slice B).

A seat arms ONE background waiter that is mention-only like `--follow`,
delivers ONE wake-worthy ring, and exits. Its exit is the wake. Each arm
drives the real waiter through `seats.cmd("wait", ...)` against a scratch
chat dir and helm home (SeatsBase), with a fake clock in place of the poll
sleep, so a 60-second debounce or an hour of traffic runs in well under a
second.

THE SYMPTOMS THESE ARMS PIN, each one measured on the live fleet:
  * a 30-minute `--follow` Monitor costs a wake at every expiry, twice an
    hour, with nothing to deliver;
  * a one-shot armed during the wake that already showed its row exits at
    once on that row, a second wake for something already in context;
  * a beacon re-delivers the same announced row at every backstop and again
    after each re-arm;
  * a seat between a one-shot's exit and its re-arm reads DEAF, and the
    re-arm typer types a resume turn into its pane.

THE FIDELITY CONTRACT is one arm each: an addressed row wakes within about a
minute, an owner row and a subsystem failure row are never held back, an
unclassifiable row wakes, no row is dropped from the hook or the ring
counts, no seat reads DEAF inside the re-arm grace, and no re-arm replays a
row the seat was already shown.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import beacon_doorbell, beacons, chat  # noqa: E402
from helm import seats, seats_delivery, seats_join  # noqa: E402
from tests import test_beacons as tb  # noqa: E402
from tests._tmphome import declaring as _tmp_declaring  # noqa: E402
from tests._tmphome import session_for as _tmp_session_for  # noqa: E402
from tests.test_seats import SeatsBase  # noqa: E402

SEAT = "seat-a"
SID = _tmp_session_for(SEAT)


class _Stop(BaseException):
    """Ends one driven waiter that did not exit on its own."""


class OnceBase(SeatsBase):
    HOME = "main"

    def setUp(self):
        super().setUp()
        seats.join(session=SID, seat=SEAT, cwd="/tmp/p", room=self.HOME)
        self.t0 = 1_900_000_000.0
        self.clock = [self.t0]

    def arm(self, passes, script=None, flags=("--once",), step=None,
            sink=None):
        """Run one waiter for at most `passes` poll passes.

        -> (lines, rc): `rc` is the CLI's return code when the waiter exited
        on its own and None when the pass budget ended it. Pass k runs at
        t0 + step*(k-1) on the fake clock, which the doorbell reads too.
        `sink` is a file object to use as stdout in place of a collector."""
        step = chat.POLL_S if step is None else step
        out, err = io.StringIO(), io.StringIO()
        n = [0]

        def nap(_seconds):
            n[0] += 1
            if n[0] >= passes:
                raise _Stop
            self.clock[0] += step
            if script:
                script(n[0], self.clock[0])

        args = ["--seat", SEAT, "--follow"] + list(flags)
        fake_time = types.SimpleNamespace(time=lambda: self.clock[0],
                                          sleep=nap)
        rc = None
        with contextlib.ExitStack() as stack:
            stack.enter_context(_tmp_declaring(args))
            stack.enter_context(mock.patch.object(seats_join, "time",
                                                  fake_time))
            stack.enter_context(mock.patch.object(
                beacon_doorbell, "_now", lambda: self.clock[0]))
            stack.enter_context(contextlib.redirect_stdout(sink or out))
            stack.enter_context(contextlib.redirect_stderr(err))
            try:
                rc = seats.cmd("wait", args, "main")
            except _Stop:
                pass
        self.stderr = err.getvalue()
        if sink is not None:
            sink.flush()
            with open(sink.name) as f:
                return f.read().splitlines(), rc
        return out.getvalue().splitlines(), rc

    def mention(self, text, who="seat-b", room="main", **kw):
        return chat.post("@%s %s" % (SEAT, text), who=who, room=room, **kw)

    def hook_lines(self, boundaries):
        """The seat's PostToolUse hook at `boundaries` tool boundaries: each
        shows the oldest row the seat is still owed, in full."""
        got = []
        for _ in range(boundaries):
            seats_delivery.deliver_any(session=SID, seat=SEAT, emit=got.append)
        return got


class OnceExitsAfterOneRingTest(OnceBase):

    def test_a_pending_mention_rings_once_and_the_waiter_exits(self):
        """RED before the cure: `--once` was an unknown flag (rc 2)."""
        self.mention("please look at lane one")
        lines, rc = self.arm(passes=30)
        self.assertEqual(rc, 0, self.stderr)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("] seat-b: @seat-a please look at lane one", lines[0])

    def test_once_requires_follow(self):
        with _tmp_declaring(["--seat", SEAT]), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            rc = seats.cmd("wait", ["--seat", SEAT, "--once"], "main")
        self.assertEqual(rc, 2)
        self.assertIn("--once requires --follow", err.getvalue())

    def test_once_refuses_the_shapes_it_cannot_end_after_one_ring(self):  # noqa: VACUOUS_ASSERTION — the loop runs over a two-item literal, so both rc and stderr assertions run twice
        for extra in ("--any", "--per-row"):
            with _tmp_declaring(["--seat", SEAT]), \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                rc = seats.cmd("wait", ["--seat", SEAT, "--follow", "--once",
                                        extra], "main")
            self.assertEqual(rc, 2, extra)
            self.assertIn(extra, err.getvalue())


class OnceIsMentionOnlyTest(OnceBase):
    HOME = "team-a"

    def test_home_room_chatter_never_wakes_it_and_a_mention_does(self):
        """A bare one-shot `helm chat wait` ran AMBIENT and woke on every
        home-room row. The one-shot beacon keeps --follow's mention-only
        scope."""
        def script(n, _now):
            if n <= 5:
                chat.post("ambient chatter %d" % n, who="seat-b",
                          room="team-a")
            elif n == 8:
                self.mention("a real ask", room="team-a")
        lines, rc = self.arm(passes=80, script=script)
        self.assertEqual(rc, 0, self.stderr)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("] seat-b: @seat-a a real ask", lines[0])
        self.assertNotIn("ambient chatter", lines[0])


class OnceFidelityTest(OnceBase):

    def test_an_addressed_row_wakes_within_about_a_minute(self):  # noqa: VACUOUS_ASSERTION — rc 0 and the one ring line are the positive observables; the time bound is measured on the same run
        posted = []

        def script(n, now):
            if n == 1:
                self.mention("act on this")
                posted.append(now)
        lines, rc = self.arm(passes=200, script=script)
        self.assertEqual(rc, 0, self.stderr)
        self.assertEqual(len(lines), 1, lines)
        self.assertLessEqual(self.clock[0] - posted[0],
                             beacon_doorbell.DEBOUNCE_S + 2 * chat.POLL_S)

    def test_an_owner_row_rings_on_the_pass_that_drains_it(self):  # noqa: VACUOUS_ASSERTION — the owner lead and its count on the ring are the positive observables of the same run
        posted = []

        def script(n, now):
            if n == 1:
                self.mention("hold every land", who="daria", origin="web")
                posted.append(now)
        lines, rc = self.arm(passes=200, script=script)
        self.assertEqual(rc, 0, self.stderr)
        self.assertIn("] daria: @seat-a hold every land", lines[0])
        self.assertIn("1 from the owner", lines[0])
        self.assertLessEqual(self.clock[0] - posted[0], chat.POLL_S)

    def test_an_owner_row_after_a_shown_row_is_never_held_back(self):
        self.mention("first ask")
        first, _ = self.arm(passes=30)
        self.assertEqual(len(first), 1, first)

        def script(n, _now):
            if n == 2:
                self.mention("stop the train", who="daria", origin="web")
        second, rc = self.arm(passes=200, script=script)
        self.assertEqual(rc, 0, self.stderr)
        self.assertEqual(len(second), 1, second)
        self.assertIn("] daria: @seat-a stop the train", second[0])

    def test_a_subsystem_failure_row_wakes_it(self):
        self.mention("your lane was EJECTED from the train; the audits are "
                     "RED", who="train-blame")
        lines, rc = self.arm(passes=200)
        self.assertEqual(rc, 0, self.stderr)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("EJECTED", lines[0])

    def test_an_unclassifiable_row_wakes_it(self):
        self.mention("a row the classifier cannot read")
        with mock.patch.object(beacon_doorbell, "_kind",
                               side_effect=RuntimeError("unreadable")):
            lines, rc = self.arm(passes=200)
        self.assertEqual(rc, 0, self.stderr)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("1 unread = 1 addressed", lines[0])

    def test_no_row_is_dropped_by_the_exit(self):
        """One ring announces both rows; the hook still shows each in full,
        and both stay in the ring's unread count until the seat reads."""
        self.mention("first of two")
        self.mention("second of two")
        lines, rc = self.arm(passes=30)
        self.assertEqual(rc, 0, self.stderr)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("doorbell: 2 unread = 2 addressed", lines[0])
        shown = self.hook_lines(2)
        self.assertEqual(2, len(shown), shown)
        self.assertIn("first of two", shown[0])
        self.assertIn("second of two", shown[1])


class ShownRowsDoNotRingAgainTest(OnceBase):
    """One row is delivered once per seat. A row a ring led with, or that
    the hook showed, is SHOWN, and no later ring leads with it."""

    def test_a_rearm_during_the_wake_that_showed_its_row_does_not_ring(self):  # noqa: VACUOUS_ASSERTION — the first arm rings the same row on the same stdout channel, asserted unconditionally above the silent re-arm
        self.mention("the row that woke it")
        first, rc = self.arm(passes=30)
        self.assertEqual((len(first), rc), (1, 0), first)
        second, rc = self.arm(passes=40)
        self.assertEqual(second, [], "the re-arm rang a row already shown")
        self.assertIsNone(rc, "the re-arm exited with nothing to deliver")

    def test_a_rearm_past_the_backstop_does_not_replay_a_shown_row(self):  # noqa: VACUOUS_ASSERTION — the first arm rings the same row on the same stdout channel, asserted unconditionally above the silent re-arm
        """RED before the cure: the first pass of the re-armed waiter rang the
        backstop at once for a row a ring had shown two hours earlier."""
        self.mention("an old assignment")
        first, _ = self.arm(passes=30)
        self.assertEqual(len(first), 1, first)
        self.clock[0] += 2 * 3600
        second, rc = self.arm(passes=40, step=60.0)
        self.assertEqual(second, [], "the re-arm replayed an old row")
        self.assertIsNone(rc)

    def test_a_long_running_beacon_does_not_re_lead_a_shown_row(self):
        """RED before the cure: the backstop re-rang the same row every
        twelve minutes for as long as it stayed unread."""
        self.mention("a stale sweep row")
        lines, _rc = self.arm(passes=60, flags=(), step=60.0)
        self.assertEqual(len(lines), 1, lines)

    def test_a_row_the_hook_showed_inside_the_window_never_rings(self):  # noqa: VACUOUS_ASSERTION — the hook pass inside the script asserts it showed the row, and test_CONTROL_a_new_row_after_a_shown_one_still_rings rings on the same channel
        """RED before the cure: the doorbell kept a drained row whose ring
        was still in its debounce window after the hook had shown it."""
        def script(n, _now):
            if n == 1:
                self.mention("shown at a tool boundary")
            elif n == 3:
                self.assertEqual(1, len(self.hook_lines(1)))
        lines, rc = self.arm(passes=100, flags=(), script=script)
        self.assertEqual(lines, [], "a ring for a row the hook already showed")
        self.assertIsNone(rc)

    def test_CONTROL_a_new_row_after_a_shown_one_still_rings(self):
        self.mention("shown first")
        first, _ = self.arm(passes=30)
        self.assertEqual(len(first), 1, first)

        def script(n, _now):
            if n == 2:
                self.mention("a fresh ask")
        second, rc = self.arm(passes=200, script=script)
        self.assertEqual(rc, 0, self.stderr)
        self.assertEqual(len(second), 1, second)
        self.assertIn("] seat-b: @seat-a a fresh ask", second[0])
        self.assertIn("doorbell: 2 unread", second[0])

    def test_CONTROL_the_backstop_still_rings_a_row_that_was_only_counted(self):
        """A ring shows ONE row whole; the rows it only counted were never
        shown, so the backstop may still lead with one of them."""
        self.mention("older ask")
        self.mention("newer ask")
        lines, _rc = self.arm(passes=14, flags=(), step=60.0)
        self.assertEqual(len(lines), 2, lines)
        self.assertIn("] seat-b: @seat-a newer ask", lines[0])
        self.assertIn("] seat-b: @seat-a older ask", lines[1])


class OnceSinkTest(OnceBase):

    def test_a_background_task_file_sink_is_the_wake_and_is_not_refused(self):
        """A one-shot armed as a background task writes to a regular file,
        and the harness reads that file when the task exits. RED before the
        cure: the waiter refused a file sink."""
        self.mention("into a task output file")
        with tempfile.NamedTemporaryFile("w", suffix=".out") as sink:
            lines, rc = self.arm(passes=30, sink=sink)
        self.assertEqual(rc, 0, self.stderr)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("into a task output file", lines[0])

    def test_CONTROL_a_follow_beacon_into_a_file_is_still_refused(self):
        self.mention("must not be consumed")
        with tempfile.NamedTemporaryFile("w", suffix=".out") as sink:
            lines, _rc = self.arm(passes=5, flags=(), sink=sink)
        self.assertEqual(1, len(lines), lines)
        self.assertIn("REFUSING this beacon", lines[0])

    def test_the_exit_stamps_the_registry_row_instead_of_dropping_it(self):
        self.mention("stamp me")
        lines, rc = self.arm(passes=30)
        self.assertEqual((len(lines), rc), (1, 0), self.stderr)
        rows = [r for r in beacons.entries(SEAT) if r["pid"] == os.getpid()]
        self.assertEqual(1, len(rows), beacons.entries(SEAT))
        self.assertEqual("once", rows[0].get("ended_how"))
        self.assertTrue(rows[0].get("waiter", {}).get("once"))


class OnceSpecTest(unittest.TestCase):

    def test_once_is_part_of_the_waiter_behavior(self):  # noqa: VACUOUS_ASSERTION — the absent key on the plain spec is paired with its presence on the once spec, asserted by equality in the same method
        plain = beacons.requested_waiter_spec("main")
        once = beacons.requested_waiter_spec("main", once=True)
        self.assertNotIn("once", plain)
        self.assertEqual(dict(plain, once=True), once)
        argv = [sys.executable, "/x/bin/helm", "chat", "wait", "--seat",
                SEAT, "--follow", "--once", "--room", "main"]
        self.assertEqual(once, beacons.waiter_spec(1, argv=argv, env={}))

    def test_a_once_incumbent_writing_a_file_is_a_waking_sink(self):
        token = (0o100000, 1, 2, 0)
        self.assertEqual(beacons.SINK_ADMISSIBLE, beacons.waking_sink(
            {"follow": True, "once": True}, beacons.SINK_REFUTES, token))
        self.assertEqual(beacons.SINK_REFUTES, beacons.waking_sink(
            {"follow": True}, beacons.SINK_REFUTES, token))
        devnull = (0o020000, 5, 6, beacons._NULL_RDEV)
        self.assertEqual(beacons.SINK_REFUTES, beacons.waking_sink(
            {"follow": True, "once": True}, beacons.SINK_REFUTES, devnull),
            "/dev/null wakes nobody, one-shot or not")


class OnceExitGraceTest(tb.Base):
    """A one-shot that delivered and exited is a seat re-arming, not a deaf
    seat, for REARM_GRACE_S after its exit."""

    def plant_ended(self, pid, ended_ago, seat="seat-a", how="once"):
        os.makedirs(beacons.registry_dir(), exist_ok=True)
        now = time.time()
        row = {"seat": seat, "pid": pid, "session": tb.SID_A,
               "starttime": 100, "armed": now - ended_ago - 40,
               "home": os.environ["HELM_HOME"],
               "waiter": {"follow": True, "once": True}}
        if how:
            row.update(ended=now - ended_ago, ended_how=how)
        with open(beacons._entry_path(seat, pid), "w") as f:
            json.dump(row, f)

    def census(self):
        with mock.patch.object(beacons, "live_sessions",
                               return_value={tb.SID_A: 90}):
            return beacons.census(proc_dir=self.proc)

    def test_a_one_shot_that_just_delivered_reads_WAKING(self):
        """RED before the cure: the census pruned the row and read DEAF, and
        the re-arm typer typed into the pane mid-wake."""
        self.roster("seat-a")
        self.agent(90, "seat-a")
        self.plant_ended(4242, 30)
        rep = self.census()
        row = rep["seats"][0]
        self.assertEqual(beacons.WAKING, row["verdict"], row["why"])
        self.assertIn("one-shot", row["why"])
        self.assertEqual([], rep["unreachable"])
        self.assertEqual(1, len(beacons.entries("seat-a")),
                         "the row is kept for the grace")

    def test_CONTROL_past_the_grace_it_is_DEAF_and_pruned(self):  # noqa: VACUOUS_ASSERTION — the verdict DEAF is the positive observable; the emptied registry is the prune it implies
        self.roster("seat-a")
        self.agent(90, "seat-a")
        self.plant_ended(4242, beacons.REARM_GRACE_S + 60)
        rep = self.census()
        self.assertEqual([beacons.DEAF], [r["verdict"] for r in rep["seats"]])
        self.assertEqual([], beacons.entries("seat-a"))

    def test_CONTROL_an_unstamped_death_mid_life_is_DEAF(self):
        self.roster("seat-a")
        self.agent(90, "seat-a")
        self.plant_ended(4242, 30, how=None)
        rep = self.census()
        self.assertEqual([beacons.DEAF], [r["verdict"] for r in rep["seats"]])

    def test_CONTROL_no_declaring_pane_is_never_WAKING(self):
        self.roster("seat-a")
        self.plant_ended(4242, 30)
        rep = self.census()
        self.assertEqual([beacons.DEAF], [r["verdict"] for r in rep["seats"]])

    def test_mark_ended_stamps_only_this_process_row(self):
        os.makedirs(beacons.registry_dir(), exist_ok=True)
        for pid in (4242, 4343):
            with open(beacons._entry_path("seat-a", pid), "w") as f:
                json.dump({"seat": "seat-a", "pid": pid, "armed": 1.0}, f)
        self.assertTrue(beacons.mark_ended("seat-a", pid=4242, now=50.0))
        rows = {r["pid"]: r for r in beacons.entries("seat-a")}
        self.assertEqual((50.0, "once"),
                         (rows[4242]["ended"], rows[4242]["ended_how"]))
        self.assertNotIn("ended", rows[4343])


if __name__ == "__main__":
    unittest.main()
