#!/usr/bin/env python3
"""A pull the seat chose is a delivery: `helm chat read` discharges the rows
it printed in full, for the seat it runs as and no other.

THE SYMPTOM THESE ARMS PIN. The beacon doorbell rang, the seat ran the pull
the ring named, read its rows and answered one, and the backstop still rang
"1 unread = 1 addressed" every 12 minutes, while the tool-boundary hook
showed each rung row again, one per boundary: the pull moved no delivery
cursor, so every reader of that cursor still counted the rows owed.

Every arm runs the read through `chat.cmd_chat`, the CLI door, as a declared
seat, on the doorbell fixture (DoorbellBase: a scratch chat dir and helm home,
the real waiter, the real hook pass and the real stop guard). Each "stays
owed" arm is paired with rows the same read discharged, so a read that
discharges nothing cannot pass it.
"""
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import beacons, chat, seats, seats_delivery  # noqa: E402
from tests._tmphome import declaring as _tmp_declaring  # noqa: E402
from tests._tmphome import session_for as _tmp_session_for  # noqa: E402
from tests.test_beacon_doorbell import SEAT, SID, DoorbellBase, _unread  # noqa: E402

OTHER = "kimi"
OTHER_SID = _tmp_session_for(OTHER)


class _Cut(io.StringIO):
    """A stdout whose reader went away after its first `lines` lines."""

    def __init__(self, lines):
        super().__init__()
        self.left = lines

    def write(self, s):
        if self.left <= 0:
            raise BrokenPipeError(32, "Broken pipe")
        self.left -= s.count("\n")
        return super().write(s)


class PullBase(DoorbellBase):

    def rung(self, texts, room="main"):
        """`texts` posted to `room` by bob and rung once -> the ring line."""
        for text in texts:
            chat.post(text, who="bob", room=room)
        lines, _seen = self.follow(passes=2, clock=True)
        self.assertEqual(len(lines), 1, lines)
        return lines[0]

    def pull(self, *args, **kw):
        """`helm chat read ARGS` run by `seat` through the CLI door -> what it
        printed. `stdout` replaces the stream the read prints to; what the read
        said on stderr is kept in `self.pull_err`."""
        out, err = kw.get("stdout") or io.StringIO(), io.StringIO()
        try:
            with _tmp_declaring(["--seat", kw.get("seat", SEAT)]), \
                    contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                self.assertEqual(chat.cmd_chat(["read"] + list(args)), 0)
        finally:
            self.pull_err = err.getvalue()
        return out.getvalue() if hasattr(out, "getvalue") else None

    def delivery(self, seat=SEAT, session=SID, room="main"):
        return seats_delivery._cursor(room, seat, session)

    def hook_texts(self, passes=4, pattern=r"@gemini \w+ \d+"):
        """What the seat's next tool boundaries show, one row per boundary."""
        got = []
        for _ in range(passes):
            seats_delivery.deliver_any(session=SID, seat=SEAT, emit=got.append)
        return [re.search(pattern, str(x)).group(0) for x in got]

    def guard_count(self):
        """The stop guard's own `N undelivered message(s)` at a fresh stop; a
        guard that finds nothing pending prints no inbox block. The guard
        blocks once per pending set (its stopfp latch), so the latch an
        earlier count left is removed first, as catchup removes it. Its
        scratch reaper deletes real scratch, so it runs only once SeatsBase
        has switched the reaper off."""
        for name in os.listdir(chat.chat_dir()):
            if ".stopfp." in name:
                os.remove(os.path.join(chat.chat_dir(), name))
        self.assertEqual(os.environ.get("HELM_SCRATCH_GC"), "0")
        blocks, warns = seats.stop_guard(session=SID, room=self.HOME,
                                         seat=SEAT)
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

    def counts(self):
        return self.ring_count(), self.guard_count()


class PullDischargesTest(PullBase):

    def test_the_pull_a_ring_names_discharges_its_rows_everywhere(self):  # noqa: VACUOUS_ASSERTION — the empty hook list and held set follow counts (3, 3) on the same rows before the pull, and OnlyWhatWasPrintedTest drives the same hook_texts to the rows a read did not print
        """RED before the cure: after the pull the ring still counted 3, the
        stop guard 3, and the hook showed all three again."""
        ring = self.rung(["@gemini owed %d" % i for i in range(3)])
        self.assertEqual(self.counts(), (3, 3))
        ids = re.search(r"helm chat read --id ([0-9a-f,]+) ", ring)
        self.assertIsNotNone(ids, ring)
        out = self.pull("--id", ids.group(1))
        for i in range(3):
            self.assertIn("@gemini owed %d" % i, out)
        self.assertEqual(self.counts(), (0, 0))
        self.assertEqual(self.hook_texts(), [])
        size = os.path.getsize(chat.room_path("main"))
        self.assertEqual(self.delivery()["off"], size)
        wake = seats_delivery._cursor("main", SEAT, SID, beacon=True)
        self.assertNotIn("held", wake, "a pulled row is still held")
        # The census asks which rows would have WOKEN the seat; the rung rows
        # did, before and after the pull.
        age, unreadable, evidence = beacons.undrained(SEAT, session=SID)
        self.assertEqual((age, unreadable, evidence["seen"]), (None, None, ()))

    def test_the_backstop_does_not_ring_again_for_pulled_rows(self):
        """RED before the cure: the backstop rang at t0+720 for rows the seat
        had pulled. The control is BackstopTest's unread arm, which rings."""
        for i in range(3):
            chat.post("@gemini owed %d" % i, who="bob")
        k = str(self.since("main", "@gemini owed 0"))

        def script(n, _now):
            if n == 1:
                self.pull("--room", "main", "--since", k)
        lines, seen = self.follow(passes=23, step=60.0, script=script,
                                  clock=True)
        self.assertEqual(seen[0], 1, "the catch-up ring")
        self.assertEqual(len(lines), 1, lines)

    def test_a_pull_of_a_row_in_another_room_discharges_it_there(self):  # noqa: VACUOUS_ASSERTION — the empty hook list follows counts (1, 1) on the same row before the pull, and OnlyWhatWasPrintedTest drives the same hook_texts to the rows a read did not print
        """The ring's one pull names a row in a room that is not the seat's
        home; that pull discharges the row there (RED before the cure)."""
        ring = self.rung(["@gemini a question from the other project 1"],
                         room="other-project")
        ids = re.search(r"helm chat read --id ([0-9a-f,]+) ", ring)
        self.assertIsNotNone(ids, ring)
        self.assertEqual(self.counts(), (1, 1))
        self.pull("--id", ids.group(1))
        self.assertEqual(self.counts(), (0, 0))
        self.assertEqual(self.hook_texts(pattern=r"@gemini .*"), [])

    def test_a_dm_pulled_by_its_recipient_is_discharged(self):
        seats.dm(SEAT, "direct ask 1", who="bob")
        lines, _seen = self.follow(passes=2, clock=True)
        self.assertEqual(len(lines), 1, lines)
        self.assertEqual(self.counts(), (1, 1))
        self.pull("--dm", "--since", "0")
        self.assertEqual(self.counts(), (0, 0))


class ElsewhereTest(PullBase):
    """The seat's home is another room, and the row that rang it sits in a
    room it is not homed in: the pull of that room is still its delivery."""
    HOME = "team-g"

    def test_a_row_addressed_to_the_seat_outside_its_home_is_discharged(self):  # noqa: VACUOUS_ASSERTION — the empty hook list follows counts (1, 1) on the same row before the pull, and OnlyWhatWasPrintedTest drives the same hook_texts to the rows a read did not print
        self.rung(["@gemini please look at row 1"], room="other-project")
        k = self.since("other-project", "@gemini please look at row 1")
        self.assertEqual(self.counts(), (1, 1))
        self.pull("--room", "other-project", "--since", str(k))
        self.assertEqual(self.counts(), (0, 0))
        self.assertEqual(self.hook_texts(pattern=r"@gemini please look at row \d"),
                         [])


class OnlyWhatWasPrintedTest(PullBase):
    """A row outside the read's window, or cut from what it printed, stays
    owed; the rows the same read printed whole do not."""

    def test_a_row_before_the_since_window_stays_owed(self):
        """RED before the cure on the two rows the read printed."""
        self.rung(["@gemini owed %d" % i for i in range(3)])
        out = self.pull("--room", "main", "--since",
                        str(self.since("main", "@gemini owed 1")))
        self.assertIn("@gemini owed 1", out)
        self.assertNotIn("@gemini owed 0", out)
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(self.hook_texts(), ["@gemini owed 0"])

    def test_rows_outside_limit_stay_owed(self):
        self.rung(["@gemini owed %d" % i for i in range(3)])
        out = self.pull("--room", "main", "--limit", "1")
        self.assertIn("@gemini owed 2", out)
        self.assertEqual(self.counts(), (2, 2))
        self.assertEqual(self.hook_texts(),
                         ["@gemini owed 0", "@gemini owed 1"])

    def test_rows_after_a_cut_print_stay_owed(self):
        """The reader of the read's stdout went away after one line: the row
        printed whole is discharged and the rows the print never finished
        stay owed."""
        self.rung(["@gemini owed %d" % i for i in range(3)])
        k = str(self.since("main", "@gemini owed 0"))
        with self.assertRaises(BrokenPipeError):
            self.pull("--room", "main", "--since", k, stdout=_Cut(1))
        self.assertEqual(self.counts(), (2, 2))
        self.assertEqual(self.hook_texts(),
                         ["@gemini owed 1", "@gemini owed 2"])

    def test_rows_past_what_a_harness_shows_whole_stay_owed(self):
        """A read too long for a harness to show whole is shown from its head:
        the short row at the head is discharged, the long row and the row
        after it stay owed."""
        self.rung(["@gemini owed 0", "@gemini long 1 " + "y" * 12000,
                   "@gemini owed 2"])
        k = str(self.since("main", "@gemini owed 0"))
        out = self.pull("--room", "main", "--since", k)
        self.assertIn("y" * 12000, out)
        self.assertEqual(self.counts(), (2, 2))
        self.assertEqual(self.hook_texts(pattern=r"@gemini \w+ \d"),
                         ["@gemini long 1", "@gemini owed 2"])


class OnlyTheCursorsOwnSeatTest(PullBase):

    def setUp(self):
        super().setUp()
        seats.join(session=OTHER_SID, seat=OTHER, cwd="/tmp/p", room="main")

    def test_a_read_as_another_seat_discharges_nothing_for_the_owner(self):  # noqa: VACUOUS_ASSERTION — the owner's three rows are asserted by value on the same hook and counts after the other seat's read, and that read's own cursor is asserted moved to the end of the room
        """The control: the other seat's read did run, and moved ITS own
        delivery cursor past the rows it printed."""
        self.rung(["@gemini owed %d" % i for i in range(3)])
        k = str(self.since("main", "@gemini owed 0"))
        other0 = self.delivery(OTHER, OTHER_SID)["off"]
        self.pull("--room", "main", "--since", k, seat=OTHER)
        size = os.path.getsize(chat.room_path("main"))
        self.assertLess(other0, size)
        self.assertEqual(self.delivery(OTHER, OTHER_SID)["off"], size)
        self.assertEqual(self.counts(), (3, 3))
        self.assertEqual(self.hook_texts(),
                         ["@gemini owed 0", "@gemini owed 1", "@gemini owed 2"])

    def test_a_read_of_anothers_dm_lane_discharges_nothing(self):
        """`--dm --seat S` by a process that is not S is a read of S's lane,
        never S's delivery; S's own pull is (the control)."""
        seats.dm(SEAT, "direct ask 1", who="bob")
        lines, _seen = self.follow(passes=2, clock=True)
        self.assertEqual(len(lines), 1, lines)
        out = self.pull("--dm", "--seat", SEAT, "--since", "0", seat=OTHER)
        self.assertIn("direct ask 1", out)
        self.assertEqual(self.counts(), (1, 1))
        self.pull("--dm", "--seat", SEAT, "--since", "0")
        self.assertEqual(self.counts(), (0, 0))


class DelegateReadTest(PullBase):
    """A subagent or a Workflow agent inherits its seat's session and name, so
    the read cannot tell it from the seat; only the PreToolUse payload's
    agent_id can (actors.SIDECHAIN_RULE). What a delegate reads is shown to
    the delegate, never to the seat, so its read delivers nothing, as the
    tool-boundary hook delivers nothing at a delegate's boundary."""

    def pretooluse(self, command, agent_id=None):
        """The installed PreToolUse hook sees a Bash call in the seat's
        session, from a delegate when `agent_id` is given."""
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                   "session_id": SID, "cwd": "/tmp/p",
                   "tool_use_id": "toolu_1", "tool_input": {"command": command}}
        if agent_id:
            payload.update(agent_id=agent_id, agent_type="general-purpose")
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(chat.cmd_argv_guard([]), 0)

    def test_a_read_a_delegate_runs_delivers_nothing_to_the_seat(self):
        """RED before the cure: the delegate's read discharged the seat's
        three rows, which the seat itself never saw."""
        self.rung(["@gemini owed %d" % i for i in range(3)])
        k = str(self.since("main", "@gemini owed 0"))
        command = "helm chat read --room main --since %s" % k
        self.pretooluse(command, agent_id="a1b2")
        out = self.pull("--room", "main", "--since", k)
        self.assertIn("@gemini owed 2", out)
        self.assertEqual(self.counts(), (3, 3))
        self.assertEqual(self.hook_texts(),
                         ["@gemini owed 0", "@gemini owed 1", "@gemini owed 2"])

    def test_the_seats_own_read_through_the_same_hook_delivers(self):
        """The control: the same call from the main thread (no agent_id)."""
        self.rung(["@gemini owed %d" % i for i in range(3)])
        k = str(self.since("main", "@gemini owed 0"))
        self.pretooluse("helm chat read --room main --since %s" % k)
        self.pull("--room", "main", "--since", k)
        self.assertEqual(self.counts(), (0, 0))

    def test_a_delegate_call_that_names_no_chat_leaves_the_seats_read_delivering(self):
        """RED before the cure: any delegate call marked the session, so a
        seat running subagents never had a pull delivered. Only a delegate
        call that RUNS a chat read or ack marks it (the arm above blocks;
        task/3696 narrowed it from a command that merely names `chat`)."""
        self.rung(["@gemini owed %d" % i for i in range(3)])
        k = str(self.since("main", "@gemini owed 0"))
        self.pretooluse("git status --short", agent_id="a1b2")
        self.pull("--room", "main", "--since", k)
        self.assertEqual(self.counts(), (0, 0))

    def marked(self):
        from helm import pull_delivery
        return pull_delivery._delegate_marked(SID)

    def unmark(self):
        from helm import pull_delivery
        with contextlib.suppress(FileNotFoundError):
            os.remove(pull_delivery._delegate_path(SID))

    def test_a_delegate_call_that_only_mentions_chat_leaves_the_seats_read_delivering(self):  # noqa: VACUOUS_ASSERTION — each command's own pull is asserted to print its row, and the mark it leaves unset is the file the next arm asserts set by the same hook
        """RED before the cure (task/3696): every delegate command holding
        the letters `chat` marked the session, a path to helm/chat.py or to a
        test module included, so a seat that runs Workflows had no read
        delivered for 15 minutes after each such call. Measured on the
        integrator: a Workflow agent's `sed -n 1430,1470p
        tests/test_chat_argv_guard.py` came 5 minutes before the seat ran
        its ring's pull, and that read delivered nothing and said nothing."""
        for i, command in enumerate((
                "sed -n 1430,1470p tests/test_chat_argv_guard.py",
                "git grep -n owed -- helm/chat.py helm/chatshort.py",
                "ls /dev/shm/helm-chat | head -3",
                "./bin/helm chat --help 2>&1 | head -3",
                "echo 'run helm chat read when idle'")):
            with self.subTest(command=command):
                self.unmark()
                text = "@gemini owed %d" % i
                self.rung([text])
                k = str(self.since("main", text))
                self.pretooluse(command, agent_id="a1b2")
                self.assertFalse(self.marked())
                out = self.pull("--room", "main", "--since", k)
                self.assertIn(text, out)
                self.assertEqual(self.counts(), (0, 0))

    def test_a_delegate_call_that_runs_a_read_or_an_ack_marks_the_session(self):  # noqa: VACUOUS_ASSERTION — each command's mark is asserted unset before its hook call and set after it, on the same file
        """The two verbs whose run by a delegate reads as its seat's own
        evidence still mark in every spelling: a read (pull_delivery,
        seats_lastread) and an ack (seats_ack's session stamp). Text the
        reader cannot settle, a verb and a program a runtime value supplies,
        all mark: a miss delivers the seat a row it never saw."""
        for command in ("./bin/helm chat read --id 0123456789ab",
                        "python3 -m helm chat ack 0123456789ab",
                        "timeout 5 helm chat",
                        "helm chat --room main read | cut -c1-80",
                        "cd /tmp && bash -c 'helm chat read --since 3'",
                        "helm chat $VERB",
                        "$H chat read",
                        # nothing proves $H is helm, so its help ask marks
                        "H=helm; $H chat read --help",
                        "echo 'an unclosed quote about chat"):
            with self.subTest(command=command):
                self.unmark()
                self.assertFalse(self.marked())
                self.pretooluse(command, agent_id="a1b2")
                self.assertTrue(self.marked())

    def test_a_delegates_help_ask_of_a_read_or_an_ack_marks_nothing(self):  # noqa: VACUOUS_ASSERTION — each command's own pull is asserted to print its row, and the arm above asserts the same hook sets the same file for the same verbs without the ask
        """RED before the cure: `cmd_chat` answers `--help` or `-h` before
        any verb runs, so the ask reads and acks nothing, but a delegate
        asking a read's or an ack's usage (which helm tells it to do before
        running a verb) marked the session, and the seat's own reads
        delivered nothing for 15 minutes."""
        for i, command in enumerate((
                "helm chat read --help",
                "./bin/helm chat --room main ack -h",
                "helm chat read --since 3 --help",
                # measured: a Workflow agent's usage sweep 14 minutes before
                # one of the integrator's reads; a verb a runtime value
                # supplies asks help too, and helm answers it before any work
                "for v in lr gate seat chat dispatch; do echo \"=== $v\"; "
                "timeout 20 ./bin/helm $v --help 2>&1 | head -3 | "
                "cut -c1-600; done")):
            with self.subTest(command=command):
                self.unmark()
                text = "@gemini owed %d" % i
                self.rung([text])
                k = str(self.since("main", text))
                self.pretooluse(command, agent_id="a1b2")
                self.assertFalse(self.marked())
                self.assertIn(text, self.pull("--room", "main", "--since", k))
                self.assertEqual(self.counts(), (0, 0))

    def test_the_seats_read_inside_a_delegates_mark_says_it_delivers_nothing(self):
        """RED before the cure: a read inside the mark returned without a
        word, so a seat that ran the pull its ring named was rung again at
        every backstop with nothing to say why. It now says so, as a read
        into a filter does, and names the ack that clears the ring."""
        self.rung(["@gemini owed %d" % i for i in range(3)])
        k = str(self.since("main", "@gemini owed 0"))
        self.pretooluse("helm chat read --room main --since %s" % k,
                        agent_id="a1b2")
        out = self.pull("--room", "main", "--since", k)
        self.assertIn("@gemini owed 2", out)
        self.assertEqual(self.counts(), (3, 3))
        self.assertIn("this read delivers nothing", self.pull_err)
        self.assertIn("delegate", self.pull_err)
        self.assertIn("helm chat ack", self.pull_err)

    def test_a_delegate_call_that_runs_another_chat_verb_leaves_the_seats_read_delivering(self):  # noqa: VACUOUS_ASSERTION — each command's own pull is asserted to print its row, and the marking arm above sets the same file through the same hook for a read and an ack
        """The must-miss for the two verbs: a delegate that posts, lists the
        roster, asks what it owes or waits once runs `helm chat` and reads
        or acks nothing, so a mark keyed on the `chat` argument alone (a
        mutant every other arm here passes) kept the seat's reads from
        delivering."""
        for i, command in enumerate((
                "helm chat post 'review done, verdict in the row'",
                "./bin/helm chat seats --all",
                "python3 -m helm chat pending",
                "helm chat --room main wait --any")):
            with self.subTest(command=command):
                self.unmark()
                text = "@gemini owed %d" % i
                self.rung([text])
                k = str(self.since("main", text))
                self.pretooluse(command, agent_id="a1b2")
                self.assertFalse(self.marked())
                self.assertIn(text, self.pull("--room", "main", "--since", k))
                self.assertEqual(self.counts(), (0, 0))

    def test_a_long_read_inside_a_delegates_mark_names_the_size_that_delivers(self):
        """RED before the cure: a read longer than a harness shows whole
        said `a read once the mark lapses delivers them`, and that read, run
        once the mark lapsed, delivered only the row at its head (`whole`):
        the long row and the row after it stayed owed, and the ring rang for
        them again. The line names what the read that delivers prints."""
        self.rung(["@gemini owed 0", "@gemini long 1 " + "y" * 12000,
                   "@gemini owed 2"])
        k = str(self.since("main", "@gemini owed 0"))
        self.pretooluse("helm chat read --room main", agent_id="a1b2")
        self.pull("--room", "main", "--since", k)
        said = self.pull_err
        self.assertEqual(self.counts(), (3, 3))
        self.unmark()
        self.pull("--room", "main", "--since", k)       # the read it named
        self.assertEqual(self.counts(), (2, 2))
        self.assertIn("a read once the mark lapses delivers them if it prints "
                      "them whole in at most 10 KiB and 256 lines", said)

    def first_row_too_long(self, marked):
        """RED before the cure: when the FIRST row a read printed was longer
        than a harness shows whole, no row was shown whole, and the read
        returned before its notice, inside a delegate's mark or not: the row
        stayed owed, the ring rang again, and nothing said why."""
        text = "@gemini first 0 " + "z" * 12000
        self.rung([text])
        k = str(self.since("main", text))
        if marked:
            self.pretooluse("helm chat read --room main", agent_id="a1b2")
        self.pull("--room", "main", "--since", k)
        said = self.pull_err
        self.assertEqual(self.counts(), (1, 1))
        self.assertIn("this read delivers nothing", said)
        self.assertIn("no printed row was shown whole", said)
        # no read shows a 12 KiB row whole, so none is promised
        self.assertIn("no read shows a row over 10 KiB or 256 lines whole",
                      said)
        self.assertIn("helm chat ack", said)
        self.assertNotIn("delivers them", said)
        self.assertEqual("delegate" in said, marked)

    def test_a_first_row_one_read_shows_whole_names_that_read(self):  # noqa: VACUOUS_ASSERTION — counts, the notice and the one-row read's delivery are asserted unconditionally
        """A first row past the head a harness shows but within what a
        one-row read shows whole: this read delivers nothing, and the read
        it names (`--id`) does."""
        text = "@gemini first 0 " + "z" * 3000
        self.rung([text, "@gemini next 1 " + "z" * 9000])
        k = str(self.since("main", text))
        self.pull("--room", "main", "--since", k)
        said = self.pull_err
        self.assertEqual(self.counts(), (2, 2))
        self.assertIn("no printed row was shown whole", said)
        self.assertIn("a read delivers them if it prints them whole in at "
                      "most 10 KiB and 256 lines", said)
        rid = chat.read("main")[0][int(k)]["id"]
        self.assertIn("@gemini first 0", self.pull("--id", rid))
        self.assertEqual(self.counts(), (1, 1))

    def test_a_read_whose_first_row_is_too_long_says_it_delivers_nothing(self):  # noqa: VACUOUS_ASSERTION — first_row_too_long asserts counts (1, 1) and the notice text unconditionally
        self.first_row_too_long(marked=False)

    def test_a_delegates_read_whose_first_row_is_too_long_says_why(self):  # noqa: VACUOUS_ASSERTION — first_row_too_long asserts counts (1, 1) and the notice text unconditionally
        self.first_row_too_long(marked=True)


class FilteredReadTest(PullBase):
    """A read whose stdout goes into a filter (`| tail -25`, `| head -40`)
    writes every line into the pipe without an error, and the filter drops
    some of them: the rows the seat saw are not the rows it printed. So a
    pipe that another process of this process's session holds open delivers
    nothing. A regular file (Claude Code hands a command one) or a pipe only
    this process or an ancestor reads (a harness reading the output) is not
    a filter."""

    def ring3(self):
        self.rung(["@gemini owed %d" % i for i in range(3)])
        return str(self.since("main", "@gemini owed 0"))

    def planted_proc(self, *pids):
        """A process table holding only this process, its ancestors and
        `pids`, each a link to its real /proc entry.

        THE SCAN READS EVERY PROCESS OF THIS SESSION, and fails closed on one
        whose descriptors it cannot list. Pointed at the host's whole table,
        these arms were red or green by what else the session held: in a slice
        worker, whose session is the worker's own, a process an earlier module
        left there made every arm read `could not be examined
        (PermissionError)`. The arms are about the processes they start, so
        the table holds exactly those, and the scan still reads the real
        entries of each."""
        view = os.path.join(self.tmp, "proc-view-%d" % len(os.listdir(
            self.tmp)))
        os.makedirs(view)
        from helm import pull_delivery
        pid, seen = os.getpid(), set()
        while pid > 1 and pid not in seen:
            seen.add(pid)
            pid = pull_delivery._stat("/proc", pid)[0]
        for pid in seen | set(pids):
            os.symlink("/proc/%d" % pid, os.path.join(view, str(pid)))
        return view

    def into_pipe(self, k, reader=None, proc_dir=None, own_group=False):
        """The pull with stdout on a pipe read by `reader` (an argv run as a
        real child of this process: in its process group, or with `own_group`
        in a process group of its own inside the same session), or by this
        process. The scan reads `proc_dir`, by default a table holding this
        process, its ancestors and the reader (`planted_proc`)."""
        r, w = os.pipe()
        child = subprocess.Popen(reader, stdin=r, stdout=subprocess.DEVNULL,
                                 preexec_fn=os.setpgrp if own_group else None) \
            if reader else None
        if child is not None:
            os.close(r)
        if proc_dir is None:
            proc_dir = self.planted_proc(*([child.pid] if child else []))
        out = os.fdopen(w, "w")
        try:
            with mock.patch.dict(os.environ, {"HELM_PROC": proc_dir}):
                try:
                    self.pull("--room", "main", "--since", k, stdout=out)
                except BrokenPipeError:
                    pass                  # the filter left after its lines
        finally:
            try:
                out.close()
            except BrokenPipeError:
                pass
            if child is not None:
                child.wait(30)
            else:
                while os.read(r, 65536):
                    pass
                os.close(r)

    def test_a_read_piped_to_tail_delivers_nothing(self):
        """RED before the cure: `| tail -1` showed one row and the pull
        delivered all three."""
        self.into_pipe(self.ring3(), ["tail", "-1"])
        self.assertEqual(self.counts(), (3, 3))
        self.assertIn("piped to another program", self.pull_err)
        self.assertEqual(self.hook_texts(),
                         ["@gemini owed 0", "@gemini owed 1", "@gemini owed 2"])

    def test_a_read_piped_to_head_delivers_nothing(self):
        """RED before the cure: `| head -1` showed one row and the pull
        delivered all three."""
        self.into_pipe(self.ring3(), ["head", "-1"])
        self.assertEqual(self.counts(), (3, 3))

    def test_a_filter_in_another_process_group_of_this_session_is_seen(self):
        """RED before the cure: `timeout N helm chat read | tail -1` puts the
        read in a process group of its own (coreutils timeout calls setpgid
        unless --foreground) and leaves tail in the shell's, so a scan bounded
        to the read's process group found no reader and the pull delivered
        all three rows while tail showed one. Measured on the tip under
        `bash -c`, `sh -c` and `bash +m -c`; only a job-control shell, which
        groups a pipeline on its own, hid it. The reader here takes its own
        group, as the read does under timeout; the session is still shared."""
        self.into_pipe(self.ring3(), ["tail", "-1"], own_group=True)
        self.assertEqual(self.counts(), (3, 3))
        self.assertIn("piped to another program", self.pull_err)
        self.assertEqual(self.hook_texts(),
                         ["@gemini owed 0", "@gemini owed 1", "@gemini owed 2"])

    def test_a_read_to_a_regular_file_delivers(self):
        """The control: Claude Code hands a Bash command a regular file."""
        k = self.ring3()
        path = os.path.join(self.tmp, "read.out")
        with open(path, "w") as f:
            self.pull("--room", "main", "--since", k, stdout=f)
        with open(path) as f:
            self.assertIn("@gemini owed 2", f.read())
        self.assertEqual(self.counts(), (0, 0))

    def test_a_pipe_only_this_process_reads_delivers(self):
        """The control for a harness that hands a command a pipe and reads it
        itself: no other process of the group holds the pipe."""
        self.into_pipe(self.ring3())
        self.assertEqual(self.counts(), (0, 0))

    def test_a_pipe_whose_readers_cannot_be_listed_delivers_nothing(self):
        """Fail closed: the same pipe with a process table that cannot be
        read (the fixture's empty proc dir) delivers nothing (RED before the
        cure)."""
        self.into_pipe(self.ring3(), proc_dir=os.path.join(self.tmp, "proc"))
        self.assertEqual(self.counts(), (3, 3))

    def test_a_long_piped_read_names_the_size_that_delivers(self):
        """RED before the cure: a piped read longer than a harness shows
        whole said `a read whose output is not piped delivers them`, and the
        same read not piped delivered only the row at its head (`whole`), so
        the seat that did as the line said was rung again for the rest."""
        self.rung(["@gemini owed 0", "@gemini long 1 " + "y" * 12000,
                   "@gemini owed 2"])
        k = str(self.since("main", "@gemini owed 0"))
        self.into_pipe(k, ["cat"])
        said = self.pull_err
        self.assertIn("piped to another program", said)
        self.assertEqual(self.counts(), (3, 3))
        self.pull("--room", "main", "--since", k)       # the read it named
        self.assertEqual(self.counts(), (2, 2))
        self.assertIn("a read whose output is not piped delivers them if it "
                      "prints them whole in at most 10 KiB and 256 lines", said)

    def test_a_short_piped_read_names_its_own_read_unpiped(self):
        """The control: a piped read a harness would show whole names the
        same read not piped, with no size, and that read delivers every
        row."""
        k = self.ring3()
        self.into_pipe(k, ["cat"])
        said = self.pull_err
        self.assertEqual(self.counts(), (3, 3))
        self.pull("--room", "main", "--since", k)
        self.assertEqual(self.counts(), (0, 0))
        self.assertIn("a read whose output is not piped delivers them, and",
                      said)


class RowNumberIsNotAnIdTest(DoorbellBase):
    """`helm chat read` prints a row's [n] and, beside it, the first
    characters of its id; `helm chat ack` takes the id. A row NUMBER typed
    into ack must never resolve some other row whose id starts with those
    digits."""

    def planted(self, rid, to, text):
        """A DM to `to` whose row id is `rid`."""
        real = os.urandom

        def urandom(n):
            return bytes.fromhex(rid) if n == 6 else real(n)
        with mock.patch("os.urandom", side_effect=urandom):
            row, err = seats.dm(to, text, who="bob")
        self.assertIsNone(err, err)
        self.assertEqual(row["id"], rid)
        return row

    def ack(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with _tmp_declaring(["--seat", SEAT]), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = chat.cmd_chat(["ack"] + list(args))
        return rc, out.getvalue(), err.getvalue()

    def acks(self):
        return [m for room in (seats.dm_lane(OTHER), seats.dm_lane(SEAT))
                for m in chat.read(room)[0] if m.get("ack")]

    def test_a_row_number_never_resolves_another_seats_message(self):  # noqa: VACUOUS_ASSERTION — a refused ack writes nothing by law; the all-digit arm below asserts one ack row on the same lanes
        """RED before the cure: `ack 1243` prefix-matched the row 1243bab1,
        addressed to another seat, and answered about THAT row."""
        self.planted("1243bab1cafe", OTHER, "for the other seat")
        rc, _out, err = self.ack("1243")
        self.assertEqual(rc, 1)
        self.assertIn("row number", err)
        self.assertNotIn("1243bab1", err)
        self.assertEqual(self.acks(), [])

    def test_an_id_the_read_printed_still_resolves_when_it_is_all_digits(self):  # noqa: VACUOUS_ASSERTION — the empty ack list before the ack is paired with the one ack row after it, asserted on the same lanes
        mine = self.planted("124300000000", SEAT, "for gemini")
        self.assertEqual(self.acks(), [])
        rc, out, err = self.ack(mine["id"][:8])
        self.assertEqual(rc, 0, err)
        self.assertIn("acked 12430000", out)
        self.assertEqual([m["ack"] for m in self.acks()], [mine["id"]])

    def test_read_prints_each_rows_id_beside_its_number(self):
        """RED before the cure: the read printed [n] and no id, while the ack
        usage said the read shows it."""
        row = chat.post("@gemini find me by id", who="bob")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(chat.cmd_chat(["read", "--room", "main"]), 0)
        line = next(x for x in out.getvalue().splitlines()
                    if "find me by id" in x)
        self.assertTrue(line.lstrip().startswith(
            "[1] %s " % row["id"][:8]), line)
        self.assertIn("ack", chat.HELP["ack"])
        self.assertIn("beside [n]", chat.HELP["ack"])


class AckTakesTheNumberTheLastReadPrintedTest(PullBase):
    """task/3382: 35 failed chat acks on the local seats, 11 of them typing
    the [n] that `helm chat read` prints. An [n] (or a bare n) resolves to
    the row YOUR last read printed at that number; an n it did not print is
    refused in one line, and nothing is acked."""

    ack = RowNumberIsNotAnIdTest.ack

    def owed(self, count):
        return [chat.post("@%s task %d" % (SEAT, i), who="bob")
                for i in range(count)]

    def acked(self):
        return [a for m in chat.read("main")[0] if m.get("ack")
                for a in chat.ack_ids(m)]

    def test_bracketed_and_bare_numbers_resolve_through_the_last_read(self):
        from helm import pk, seats_lastread
        rows = self.owed(3)
        out = self.pull("--room", "main")
        self.assertIn("[2] %s" % rows[1]["id"][:8], out)
        record = pk.read_json(seats_lastread.path(SEAT), None)
        self.assertEqual(record["rows"],
                         {str(n): r["id"] for n, r in enumerate(rows, 1)})
        rc, said, err = self.ack("[2]")
        self.assertEqual(rc, 0, err)
        self.assertIn(rows[1]["id"][:8], said)
        rc, said, err = self.ack("3")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.acked(), [rows[1]["id"], rows[2]["id"]])

    def test_a_number_the_last_read_did_not_print_refuses_in_one_line(self):
        rows = self.owed(3)
        out = self.pull("--room", "main", "--limit", "1")
        self.assertIn("[3] %s" % rows[2]["id"][:8], out)
        self.assertNotIn(rows[0]["id"][:8], out)
        for token in ("[1]", "1", "[9]"):
            rc, _said, err = self.ack(token)
            self.assertEqual(rc, 1, (token, err))
            self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertIn("did not print", err)
            self.assertIn("[3]", err)
        self.assertEqual(self.acked(), [])
        rc, _said, err = self.ack("[3]")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.acked(), [rows[2]["id"]])

    def test_a_number_mixed_with_an_id_acks_both(self):
        rows = self.owed(2)
        self.pull("--room", "main")
        rc, _said, err = self.ack("[1]", rows[1]["id"][:8])
        self.assertEqual(rc, 0, err)
        acked = self.acked()
        self.assertEqual(len(acked), 2, acked)
        self.assertEqual(sorted(acked), sorted([rows[0]["id"], rows[1]["id"]]))

    def test_a_read_naming_another_seat_records_nothing(self):
        """The reader is the admitted actor: a read that ASSERTS another
        seat is refused that identity, so it cannot overwrite either seat's
        numbers, and the seat's own last read still resolves."""
        from helm import pk, seats_lastread
        rows = self.owed(2)
        self.pull("--room", "main")
        mine = pk.read_json(seats_lastread.path(SEAT), None)
        self.assertEqual(mine["rows"]["1"], rows[0]["id"])
        self.pull("--dm", "--seat", OTHER)
        self.assertEqual(pk.read_json(seats_lastread.path(SEAT), None), mine)
        self.assertIsNone(pk.read_json(seats_lastread.path(OTHER), None))
        rc, _said, err = self.ack("[1]")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.acked(), [rows[0]["id"]])

    def test_a_read_a_delegate_may_have_made_resolves_no_number(self):
        """The mark cannot say whose read it was (pull_delivery), so its
        numbers name nothing; the id beside [n] still acks."""
        from helm import pull_delivery
        rows = self.owed(1)
        pull_delivery.mark_delegate(SID)
        self.pull("--room", "main")
        rc, _said, err = self.ack("[1]")
        self.assertEqual(rc, 1, err)
        self.assertIn("delegate", err)
        self.assertEqual(self.acked(), [])
        rc, _said, err = self.ack(rows[0]["id"][:8])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.acked(), [rows[0]["id"]])


class RestartedBeaconTest(PullBase):
    """task/3696, the measured incident. A meld outcome row addressed to the
    integrator rang its beacon at 16:30, 16:47, 17:04 and 17:16 although the
    seat had run the ring's pull three times and read the room once. Each of
    those reads was piped (`| cut -c1-400 | head -6`), and each ran within
    15 minutes of a Workflow agent's command that held the letters `chat`
    (`sed -n 1430,1470p tests/test_chat_argv_guard.py`), which marked the
    session. The pipe kept each read from delivering, which is its rule; the
    mark kept every read from delivering, silently, so no read the seat
    could run would have ended the backstop. A waiter re-armed at the
    Monitor's 30-minute expiry is past the backstop, so it rings at once
    for every row still owed."""

    into_pipe = FilteredReadTest.into_pipe
    planted_proc = FilteredReadTest.planted_proc
    pretooluse = DelegateReadTest.pretooluse
    ack = RowNumberIsNotAnIdTest.ack
    TEXT = "@gemini MELD OUTCOME: AGREED, the cut is yours"

    def rung_row(self):
        """The row rung once -> (its id as the ring's pull names it, its
        `--since` offset)."""
        ring = self.rung([self.TEXT])
        ids = re.search(r"helm chat read --id ([0-9a-f]+) ", ring)
        self.assertIsNotNone(ids, ring)
        return ids.group(1), str(self.since("main", self.TEXT))

    def restarted(self, passes=3):
        """What a waiter armed at the Monitor's 30-minute expiry prints."""
        self.clock[0] = self.t0 + 1800.0
        lines, _seen = self.follow(passes=passes, clock=True)
        return lines

    def test_a_read_after_a_piped_read_ends_the_ring_for_good(self):  # noqa: VACUOUS_ASSERTION — the restarted waiter's silence is paired with the unread-row arm below, whose restarted waiter rings the same row on the same schedule
        """RED before the cure: the seat's read that was not piped came
        inside the mark the delegate's `sed` left, delivered nothing, and
        the restarted waiter rang the row again."""
        rid, k = self.rung_row()
        self.pretooluse("sed -n 1430,1470p tests/test_chat_argv_guard.py",
                        agent_id="a1b2")
        self.into_pipe(k, ["head", "-40"])
        piped = self.pull_err
        self.assertEqual(self.counts(), (1, 1))
        out = self.pull("--id", rid)
        self.assertIn(self.TEXT, out)
        self.assertEqual(self.counts(), (0, 0))
        self.assertEqual(self.restarted(), [])
        self.assertIn("piped to another program", piped)

    def test_an_unread_row_the_ring_showed_stays_owed_and_is_not_replayed(self):  # noqa: VACUOUS_ASSERTION — the counts (1, 1) before and after are the positive observables; the control below rings a counted-only row on the same restarted waiter
        """A row the seat never read (its one read was piped) stays owed and
        counted. Its ring carried it whole, so a waiter re-armed past the
        backstop does not ring it again (task/4019: one row is delivered once
        per seat); the hook and the stop guard still owe it."""
        _rid, k = self.rung_row()
        self.into_pipe(k, ["head", "-40"])
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(self.restarted(passes=6), [])
        self.assertEqual(self.counts(), (1, 1))

    def test_an_unread_row_the_ring_only_counted_rings_a_restarted_waiter_once(self):
        """The control: a row the ring only counted was never shown, so the
        waiter re-armed past the backstop rings it on its first pass, once,
        and stays silent until its own backstop."""
        self.rung(["@gemini an older ask", self.TEXT])
        self.clock[0] = self.t0 + 1800.0
        lines, seen = self.follow(passes=6, clock=True)
        self.assertEqual(len(lines), 1, lines)
        self.assertEqual(seen[0], 1, "the re-armed waiter rings at once")
        self.assertIn("] bob: @gemini an older ask (+1 waiting", lines[0])
        self.assertIn("doorbell: 2 unread = 2 addressed", lines[0])

    def test_an_ack_inside_a_delegates_mark_ends_the_ring(self):  # noqa: VACUOUS_ASSERTION — the restarted waiter's silence is paired with the unread-row arm above, whose restarted waiter rings the same row on the same schedule
        """An ack clears the ring per seat, from any session and inside a
        delegate's mark (beacon_doorbell._acks): the other way a seat that
        handled a row ends its ring."""
        rid, _k = self.rung_row()
        self.pretooluse("helm chat read --room main", agent_id="a1b2")
        rc, _said, err = self.ack(rid)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.restarted(), [])

    def test_a_piped_read_inside_a_delegates_mark_names_both_causes(self):
        """RED before the cure: a read that was piped AND inside a delegate's
        mark (the measured incident had both) named only the pipe, and told
        the seat that a read whose output is not piped delivers its row,
        which inside the mark delivers nothing either. It names both causes
        and the one read that does deliver."""
        _rid, k = self.rung_row()
        self.pretooluse("helm chat read --room main", agent_id="a1b2")
        self.into_pipe(k, ["head", "-40"])
        self.assertEqual(self.counts(), (1, 1))
        self.assertIn("piped to another program", self.pull_err)
        self.assertIn("a delegate of this session", self.pull_err)
        self.assertIn("a read whose output is not piped, once the mark "
                      "lapses, delivers them", self.pull_err)


if __name__ == "__main__":
    unittest.main()
