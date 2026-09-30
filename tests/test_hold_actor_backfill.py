#!/usr/bin/env python3
"""A pre-stamp SOURCE-CLEAN hold gets its holder back from the holder's own
transcript, and only on proof (task/3131).

THE STATE. Holds written before the ledger lock stamped `hold_actor` record
no hand, so `helm lr close <id> --reason source-clean-landed` refuses every
one of them with "the hold records NO HOLDER", even after the land shipped
the held tip under a verified gate. The writer's hand is still on record: its
Claude Code session transcript holds the Bash tool call that ran
`helm dispatch hold <id> --source-clean <tip>`, with the output the door
prints on success, seconds before the ledger stamped the hold.

WHAT THESE ARMS PIN:
  * one successful tool call, in the recipient's store under the helm home,
    inside the 60 s before the hold -> RECOVERABLE; `--apply` appends one
    `hold-actor-backfill` event and source-clean-landed then closes the row;
  * every other shape stays OWED and names the proof that failed: no match,
    a match only in another seat's home, two candidates, a call outside the
    window, a result that is missing or not a success, a call in another
    seat's store that started before the hold, whatever its result and
    whatever the age of its file (or the recipient's own call copied there,
    whatever instant the copy carries, or an unreadable file there), a
    recipient that wrote a round of the lane,
    a store outside the helm home, a store another seat's home resolves to;
  * the dry run writes nothing, the census never opens the owner's
    credential tree or the default Claude store, and a transcript is read one
    bounded line at a time;
  * the fold sets the holder for the hold the event names and nothing else,
    replays to the same row, and refuses a forged event of every shape.

The transcript fixtures copy the SHAPE of a real seat transcript line (an
assistant `tool_use` of Bash, the PreToolUse hook attachment carrying its
id, and the user `tool_result`), never its content.
"""
import builtins
import contextlib
import json
import os
import time
import tracemalloc
import unittest
from unittest import mock

from helm import dispatches, eventledger, home, landreq, pk
# The module, never its TestCase: tests/test_suite_collection.py says why.
from tests import test_source_clean_landed as _sc
from tests.test_landreq import run as _lr_run

READER = _sc.READER            # every row's recipient
OTHER = _sc.OTHER              # a seat that is neither recipient nor author
REASON = "SOURCE-CLEAN: read clean"


def setUpModule():
    """No dispatch row this module writes walks the host's process table."""
    from tests._tmphome import pin_live_seats
    pin_live_seats()


def _backfill():
    """The census module, imported where it is used so a missing module is a
    failure of each arm rather than of the whole file."""
    from helm import holdbackfill
    return holdbackfill


def _instant(epoch):
    """A transcript instant: UTC, milliseconds, `Z`."""
    whole = int(epoch)
    return "%s.%03dZ" % (time.strftime("%Y-%m-%dT%H:%M:%S",
                                       time.gmtime(whole)),
                         int(round((epoch - whole) * 1000)) % 1000)


def _use(tid, command, ts, session):
    return {"parentUuid": "p-" + tid, "isSidechain": False,
            "message": {"id": "msg-" + tid, "type": "message",
                        "role": "assistant", "model": "fixture-model",
                        "content": [{"type": "tool_use", "id": tid,
                                     "name": "Bash",
                                     "input": {"command": command,
                                               "description": "Hold it"}}],
                        "stop_reason": "tool_use"},
            "type": "assistant", "uuid": "u-" + tid, "timestamp": ts,
            "sessionId": session, "cwd": "/fixture/seat"}


def _hook(tid, ts, session):
    return {"parentUuid": "u-" + tid, "isSidechain": False,
            "attachment": {"type": "hook_success",
                           "hookName": "PreToolUse:Bash", "toolUseID": tid,
                           "hookEvent": "PreToolUse", "content": "",
                           "stdout": "", "stderr": "", "exitCode": 0},
            "type": "attachment", "uuid": "a-" + tid, "timestamp": ts,
            "sessionId": session}


def _result(tid, text, ts, session, is_error=False):
    return {"parentUuid": "u-" + tid, "isSidechain": False, "type": "user",
            "message": {"role": "user",
                        "content": [{"tool_use_id": tid, "type": "tool_result",
                                     "content": text, "is_error": is_error}]},
            "uuid": "r-" + tid, "timestamp": ts,
            "toolUseResult": {"stdout": text, "stderr": "",
                              "interrupted": False},
            "sourceToolAssistantUUID": "u-" + tid, "sessionId": session}


def _opening(ts, session):
    return {"parentUuid": None, "isSidechain": False, "type": "user",
            "message": {"role": "user", "content": "read the delta"},
            "uuid": "o-" + session, "timestamp": ts, "sessionId": session}


class BackfillBase(_sc.SourceCleanBase):
    """Rows held source-clean the way the pre-stamp door wrote them, and seat
    transcript stores under this test's own helm home."""

    def setUp(self):
        super().setUp()
        real = os.path.realpath(os.path.expanduser("~/.helm"))
        self.assertNotEqual(os.path.realpath(home.helm_home()), real,
                            "the fixture would read the real helm home")
        self._sessions = 0

    # -- the ledger ---------------------------------------------------------
    def pre_stamp(self, lane, tip=None, recipient=READER, ago=600):
        """A row held source-clean with NO holder, stamped `ago` seconds back
        -> (row, the hold's epoch)."""
        tip = tip or self.b
        row = self.row(tip, lane, recipient=recipient)
        held = int(time.time()) - ago
        self.planted(row, tip, ts=pk.epoch_ts(held))
        state = self.state(row["id"])
        self.assertNotIn("hold_actor", state)
        return row, held

    def ledger_bytes(self):
        with open(dispatches.ledger_path(), "rb") as fh:
            return fh.read()

    # -- the transcripts ----------------------------------------------------
    def store(self, seat):
        """`seat`'s transcript store under this test's helm home."""
        path = os.path.join(home.global_dir(), "seats", seat, "claude",
                            "projects", "-fixture-seat")
        os.makedirs(path, exist_ok=True)
        return path

    def transcript(self, seat, lines, name=None):
        """Write one session file of `lines` into `seat`'s store -> path."""
        self._sessions += 1
        path = os.path.join(self.store(seat), name or "session-%d.jsonl"
                            % self._sessions)
        with open(path, "a", encoding="utf-8") as fh:
            for line in lines:
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        return path

    def command(self, row, tip=None):
        """The command a seat ran: id and tip through shell variables."""
        return ('R=%s; T=%s; helm dispatch hold $R --source-clean $T "%s" '
                "2>&1 | grep -v 'you ran' | tail -1"
                % (row["id"][:12], tip or self.b, REASON))

    def success(self, row, tip=None):
        return ("helm dispatch: %s — HELD SOURCE-CLEAN at %s — ON "
                "THE INTEGRATOR'S LAND GATE (%s)" % (row["id"], tip or self.b,
                                                     REASON))

    def call(self, row, held, tid, offset=-3.875, result="success",
             command=None, session="fixture-session"):
        """The lines of one hold tool call starting `offset` s from the hold."""
        start = held + offset
        lines = [_use(tid, command or self.command(row), _instant(start),
                      session),
                 _hook(tid, _instant(start + 2), session)]
        if result == "success":
            lines.append(_result(tid, "245\n" + self.success(row),
                                 _instant(start + 9), session))
        elif result == "failure":
            lines.append(_result(tid, "helm dispatch: hold reason over the "
                                 "256 budget", _instant(start + 9), session,
                                 is_error=True))
        return lines

    def session(self, row, held, calls, seat=READER, name=None):
        """One transcript for `seat`: an opening line an hour before the hold,
        then `calls` -> (path, the line number of each call's tool_use)."""
        lines = [_opening(_instant(held - 3600), "fixture-session")]
        numbers = []
        for call in calls:
            numbers.append(len(lines) + 1)
            lines.extend(call)
        return self.transcript(seat, lines, name=name), numbers

    # -- the census ---------------------------------------------------------
    def census(self, **kw):
        report, err = _backfill().census(**kw)
        self.assertIsNone(err, err)
        return report

    def entry(self, report, row):
        hits = [e for e in report["rows"] if e["id"] == row["id"]]
        self.assertEqual(len(hits), 1, report)
        return hits[0]

    def owed(self, row, kind, needle):
        """Assert the census leaves `row` OWED naming `kind`, and writes
        nothing -> the entry."""
        before = self.ledger_bytes()
        entry = self.entry(self.census(apply=True), row)
        self.assertEqual(entry["verdict"], _backfill().OWED, entry)
        self.assertEqual(entry["kind"], kind, entry)
        self.assertIn(_backfill().PROOFS[kind], entry["reason"])
        self.assertIn(needle, entry["reason"])
        self.assertIsNone(entry["evidence"])
        self.assertEqual(self.ledger_bytes(), before,
                         "an unprovable row was written")
        self.assertNotIn("hold_actor", self.state(row["id"]))
        return entry

    def owed_by(self, row, name, needle):
        """owed() for the kind the module's constant `name` spells, read only
        after the verdict: an arm for a kind the module lacks fails on the
        verdict it measured, never on the missing name -> the entry."""
        entry = self.entry(self.census(), row)
        self.assertEqual(entry["verdict"], _backfill().OWED, entry)
        return self.owed(row, getattr(_backfill(), name), needle)


class RecoverableTest(BackfillBase):
    """ONE SUCCESSFUL CALL IN THE RECIPIENT'S STORE, INSIDE THE WINDOW."""

    def test_a_single_match_is_RECOVERABLE_with_its_evidence_and_the_dry_run_writes_nothing(self):
        row, held = self.pre_stamp("lane/bf-one")
        path, (line,) = self.session(row, held, [self.call(row, held, "tool_A1")])
        before = self.ledger_bytes()
        entry = self.entry(self.census(), row)
        self.assertEqual(entry["verdict"], _backfill().RECOVERABLE, entry)
        self.assertIsNone(entry["kind"])
        import hashlib
        self.assertEqual(entry["evidence"], {
            "transcript": path, "line": line, "tool_use_id": "tool_A1",
            "command_sha256": hashlib.sha256(
                self.command(row).encode("utf-8")).hexdigest(),
            "tool_ts": _instant(held - 3.875)})
        self.assertEqual(entry["hold_seq"], self.state(row["id"])["hold_seq"])
        self.assertEqual(self.ledger_bytes(), before,
                         "the dry run appended to the ledger")
        self.assertNotIn("hold_actor", self.state(row["id"]))

    def test_apply_writes_ONE_backfill_and_source_clean_landed_then_closes(self):
        row, held = self.pre_stamp("lane/bf-close")
        self.session(row, held, [self.call(row, held, "tool_B1")])
        token = self.mint(self.c)
        # THE STATE THIS DOOR EXISTS FOR, measured first on the same row.
        _out, err = self.close(row, token, dry_run=True)
        self.assertIn("NO HOLDER", err or "")
        hold_seq = self.state(row["id"])["hold_seq"]
        count = self.history()
        entry = self.entry(self.census(apply=True), row)
        self.assertEqual(entry["verdict"], _backfill().WRITTEN, entry)
        self.assertEqual(self.history(), count + 1)
        (event,) = self.events_of(row["id"], "hold-actor-backfill")
        self.assertEqual(sorted(event), sorted(
            ["v", "event", "seq", "id", "ts", "hold_seq", "hold_actor",
             "evidence"]))
        self.assertEqual(event["v"], 3)
        self.assertEqual(event["hold_seq"], hold_seq)
        self.assertEqual(event["hold_actor"], READER)
        self.assertEqual(event["evidence"], entry["evidence"])
        state = self.state(row["id"])
        self.assertEqual(state["hold_actor"], READER)
        self.assertEqual(state["hold_actor_evidence"], entry["evidence"])
        self.assertEqual(state["status"], "held")
        # AND THE CLOSE NOW OPENS, recording the recovered holder.
        _out, err = self.close(row, token)
        self.assertIsNone(err, err)
        close = self.close_event(row["id"])
        self.assertEqual(close["source_clean_hold_actor"], READER)
        self.assertEqual(self.state(row["id"])["status"], "closed")

    def test_a_failed_attempt_then_a_success_in_the_window_recovers_the_success(self):
        row, held = self.pre_stamp("lane/bf-retry")
        _path, (_first, second) = self.session(row, held, [
            self.call(row, held, "tool_C1", offset=-40, result="failure"),
            self.call(row, held, "tool_C2", offset=-2)])
        entry = self.entry(self.census(), row)
        self.assertEqual(entry["verdict"], _backfill().RECOVERABLE, entry)
        self.assertEqual(entry["evidence"]["tool_use_id"], "tool_C2")
        self.assertEqual(entry["evidence"]["line"], second)

    def test_one_call_copied_into_a_second_session_file_is_ONE_candidate(self):
        row, held = self.pre_stamp("lane/bf-copy")
        call = self.call(row, held, "tool_D1")
        first, _n = self.session(row, held, [call], name="a-original.jsonl")
        self.session(row, held, [call], name="b-copy.jsonl")
        entry = self.entry(self.census(), row)
        self.assertEqual(entry["verdict"], _backfill().RECOVERABLE, entry)
        self.assertEqual(entry["evidence"]["transcript"], first)

    def test_the_second_success_line_counts_when_the_tip_moved(self):
        """A cure round moves the held tip past the dispatched one, and the
        door then prints a second line naming both; a `| tail -1` keeps only
        that one."""
        row = self.row(self.a, "lane/bf-moved")
        held = int(time.time()) - 600
        self.planted(row, self.b, ts=pk.epoch_ts(held))
        text = ("helm dispatch: the row was dispatched at %s; this hold "
                "declares %s clean" % (self.a, self.b))
        tid, start = "tool_E1", held - 5
        self.transcript(READER, [
            _opening(_instant(held - 3600), "s"),
            _use(tid, self.command(row), _instant(start), "s"),
            _result(tid, text, _instant(start + 8), "s")])
        entry = self.entry(self.census(), row)
        self.assertEqual(entry["verdict"], _backfill().RECOVERABLE, entry)


class OwedTest(BackfillBase):
    """EVERY UNPROVABLE SHAPE STAYS OWED AND SAYS WHICH PROOF FAILED. Each
    fixture differs from the recoverable one by the one fact the arm names,
    and `test_the_control_recovers` runs that recoverable shape here."""

    def test_the_control_recovers(self):
        row, held = self.pre_stamp("lane/bf-control")
        self.session(row, held, [self.call(row, held, "tool_K1")])
        entry = self.entry(self.census(), row)
        self.assertEqual(entry["verdict"], _backfill().RECOVERABLE, entry)

    def test_no_match(self):
        row, held = self.pre_stamp("lane/bf-none")
        self.session(row, held, [[_use("tool_N1", "helm lr list",
                                       _instant(held - 5), "s")]])
        self.owed(row, _backfill().NO_MATCH, "@%s" % READER)

    def test_a_match_only_in_ANOTHER_seats_home(self):
        row, held = self.pre_stamp("lane/bf-other")
        self.store(READER)
        self.session(row, held, [self.call(row, held, "tool_O1")], seat=OTHER)
        self.owed(row, _backfill().OTHER_SEAT, "@%s" % OTHER)

    def test_two_successful_candidates(self):
        row, held = self.pre_stamp("lane/bf-two")
        self.session(row, held, [self.call(row, held, "tool_T1", offset=-30),
                                 self.call(row, held, "tool_T2", offset=-3)])
        entry = self.owed(row, _backfill().AMBIGUOUS, "2 successful")
        self.assertIn("tool", entry["reason"])

    def test_a_call_outside_the_60_s_window(self):
        row, held = self.pre_stamp("lane/bf-window")
        self.session(row, held, [self.call(row, held, "tool_W1", offset=-75)])
        self.owed(row, _backfill().OUT_OF_WINDOW, "75.0 s before")

    def test_a_call_AFTER_the_hold_is_outside_the_window(self):
        row, held = self.pre_stamp("lane/bf-after")
        self.session(row, held, [self.call(row, held, "tool_W2", offset=+4)])
        self.owed(row, _backfill().OUT_OF_WINDOW, "after the hold")

    def test_a_failed_tool_result(self):
        row, held = self.pre_stamp("lane/bf-failed")
        self.session(row, held, [self.call(row, held, "tool_F1",
                                           result="failure")])
        self.owed(row, _backfill().NO_SUCCESS, "lacks the success line")

    def test_a_missing_tool_result(self):
        row, held = self.pre_stamp("lane/bf-noresult")
        self.session(row, held, [self.call(row, held, "tool_F2", result=None)])
        self.owed(row, _backfill().NO_SUCCESS, "no result read")

    def test_a_result_for_ANOTHER_row_is_not_this_rows_success(self):
        row, held = self.pre_stamp("lane/bf-foreign")
        other, _h = self.pre_stamp("lane/bf-foreign-2", ago=900)
        tid, start = "tool_F3", held - 4
        self.transcript(READER, [
            _opening(_instant(held - 3600), "s"),
            _use(tid, self.command(row), _instant(start), "s"),
            _result(tid, self.success(other), _instant(start + 8), "s")])
        self.owed(row, _backfill().NO_SUCCESS, "lacks the success line")

    def test_the_recipient_wrote_a_round_of_the_lane(self):
        import os as _os
        with mock.patch.dict(_os.environ, {"HELM_CHAT_NAME": READER}):
            first = self.row(self.side, "lane/bf-author", recipient=OTHER)
        self.assertEqual(first["sender"], READER)
        second = self.row(self.b, "lane/bf-author", supersedes=first["id"])
        held = int(time.time()) - 600
        self.planted(second, self.b, ts=pk.epoch_ts(held))
        self.session(second, held, [self.call(second, held, "tool_L1")])
        self.owed(second, _backfill().LANE_AUTHOR, "LANE AUTHOR")

    def test_a_recipient_with_no_seat_home_under_the_helm_home(self):
        row, held = self.pre_stamp("lane/bf-nohome", recipient="seat-afar")
        self.session(row, held, [self.call(row, held, "tool_H1")])
        self.owed(row, _backfill().OUTSIDE_HELM, "no seat home for @seat-afar")

    def test_a_row_that_already_records_its_holder_is_not_pending(self):
        row = self.held(self.b, "lane/bf-stamped")
        report = self.census()
        self.assertNotIn(row["id"], [e["id"] for e in report["rows"]])
        entry = self.entry(self.census(ids=[row["id"][:12]]), row)
        self.assertEqual(entry["kind"], _backfill().NOT_PENDING)
        self.assertIn("held by @%s" % READER, entry["reason"])


class ASecondHandNamesNoWriterTest(BackfillBase):
    """A SUCCESS ONLY THE RECIPIENT'S STORE HOLDS NAMES THE WRITER; ONE MORE,
    ANYWHERE, NAMES NONE (a Fable review of task/3131). The pre-stamp hold door
    checked no recipient, so it printed its success line for an idempotent
    re-run by ANY hand: a call in another seat's store that started before the
    hold is a second writer the recipient's own success cannot rule out,
    whatever its transcript says, because a hold call can write minutes after
    it starts and one moved to the background prints no success line. And a
    store that two seats' homes resolve to is neither seat's own, since no
    transcript line names the seat that wrote it."""

    def seat_home(self, seat):
        """`seat`'s config dir under this test's helm home -> its path."""
        return os.path.join(home.global_dir(), "seats", seat, "claude")

    def link_home(self, seat, target):
        """Point `seat`'s config dir at `target` -> nothing."""
        os.makedirs(os.path.dirname(self.seat_home(seat)))
        os.symlink(target, self.seat_home(seat))

    def test_a_success_in_ANOTHER_seats_store_inside_the_window_is_a_RIVAL(self):
        row, held = self.pre_stamp("lane/bf-rival")
        self.session(row, held, [self.call(row, held, "tool_V1", offset=-20)])
        rival, _n = self.session(row, held, [
            self.call(row, held, "tool_V2", offset=-45)], seat=OTHER)
        entry = self.owed_by(row, "RIVAL", "@%s" % OTHER)
        self.assertIn(rival, entry["reason"])
        self.assertIn("idempotent re-run", entry["reason"])

    def test_no_rival_from_another_seats_calls_that_started_AFTER_the_hold(self):
        """THE CONTROL: the same two stores, and the other seat's calls, a
        success and a failure, both started after the hold's stamped second
        ended, so neither can have written it."""
        row, held = self.pre_stamp("lane/bf-rival-control")
        self.session(row, held, [self.call(row, held, "tool_V3", offset=-20)])
        self.session(row, held, [
            self.call(row, held, "tool_V4", offset=1),
            self.call(row, held, "tool_V5", offset=30, result="failure")],
            seat=OTHER)
        entry = self.entry(self.census(), row)
        self.assertEqual(entry["verdict"], _backfill().RECOVERABLE, entry)
        self.assertEqual(entry["evidence"]["tool_use_id"], "tool_V3")

    def test_a_call_there_BEFORE_the_window_moved_to_the_background_is_a_RIVAL(self):
        """11a9a9574b18's shape: its hold was written by a call that started
        171 s before the stamp, and the transcript's result says only that the
        call was moved to the background."""
        row, held = self.pre_stamp("lane/bf-rival-background")
        self.session(row, held, [self.call(row, held, "tool_W1", offset=-20)])
        start, session = held - 171, "fixture-session"
        rival, _n = self.session(row, held, [[
            _use("tool_W2", self.command(row), _instant(start), session),
            _hook("tool_W2", _instant(start + 2), session),
            _result("tool_W2", "Command did not complete within its 120s "
                    "timeout and was moved to the background (ID: bfixture). "
                    "Output is being written to: /fixture/bfixture.output.",
                    _instant(start + 120), session)]], seat=OTHER)
        entry = self.owed_by(row, "RIVAL", "@%s" % OTHER)
        self.assertIn(rival, entry["reason"])

    def test_a_failure_or_an_unread_result_there_inside_the_window_is_a_RIVAL(self):  # noqa: VACUOUS_ASSERTION — the loop runs a fixed two-case tuple and every case asserts
        """A result that lacks the success line, or that was never read, says
        nothing about whether the call wrote the hold."""
        for tid, result in (("tool_W3", "failure"), ("tool_W5", None)):
            with self.subTest(result=result):
                row, held = self.pre_stamp("lane/bf-rival-%s" % tid)
                self.session(row, held, [
                    self.call(row, held, tid + "a", offset=-20)])
                rival, _n = self.session(row, held, [
                    self.call(row, held, tid + "b", offset=-30,
                              result=result)], seat=OTHER)
                entry = self.owed_by(row, "RIVAL", "@%s" % OTHER)
                self.assertIn(rival, entry["reason"])

    def test_the_recipients_own_call_copied_into_another_seats_store_is_a_RIVAL(self):
        """One tool call in two seats' stores is a copied session, and the
        copy does not say which way it went."""
        row, held = self.pre_stamp("lane/bf-rival-copy")
        call = self.call(row, held, "tool_V6", offset=-20)
        self.session(row, held, [call])
        self.session(row, held, [call], seat=OTHER)
        entry = self.owed_by(row, "RIVAL", "@%s" % OTHER)
        self.assertIn("the same tool call", entry["reason"])

    def test_a_copy_of_the_recipients_own_call_stamped_AFTER_the_hold_is_a_RIVAL(self):
        """ANY copy of the recipient's own call refuses, whatever instant the
        copy carries: a copied session does not say which way it went."""
        row, held = self.pre_stamp("lane/bf-rival-late-copy")
        self.session(row, held, [self.call(row, held, "tool_X1", offset=-20)])
        self.session(row, held, [self.call(row, held, "tool_X1", offset=5)],
                     seat=OTHER)
        entry = self.owed_by(row, "RIVAL", "@%s" % OTHER)
        self.assertIn("the same tool call", entry["reason"])

    def test_a_call_there_INSIDE_the_holds_stamped_second_is_a_RIVAL(self):
        """The ledger floors its stamp to the second, so a call that started
        half a second into that second can have written it: only a start at
        or after the NEXT second clears a call (the control starts there)."""
        row, held = self.pre_stamp("lane/bf-rival-same-second")
        self.session(row, held, [self.call(row, held, "tool_X2", offset=-20)])
        rival, _n = self.session(row, held, [
            self.call(row, held, "tool_X3", offset=0.5)], seat=OTHER)
        entry = self.owed_by(row, "RIVAL", "@%s" % OTHER)
        self.assertIn(rival, entry["reason"])

    def test_a_call_there_whose_instant_cannot_be_read_is_a_RIVAL(self):
        """Only a start PROVABLY after the hold clears a call, and an instant
        the census cannot read proves nothing, even one that would read as
        after the hold."""
        row, held = self.pre_stamp("lane/bf-rival-no-instant")
        self.session(row, held, [self.call(row, held, "tool_X4", offset=-20)])
        odd = pk.epoch_ts(held + 30)[:-1] + "+00:00"
        rival, _n = self.session(row, held, [[
            _use("tool_X5", self.command(row), odd, "fixture-session")]],
            seat=OTHER)
        entry = self.owed_by(row, "RIVAL", "@%s" % OTHER)
        self.assertIn(rival, entry["reason"])

    def test_one_call_copied_twice_there_is_judged_by_EVERY_copy(self):
        """A copy stamped after the hold does not clear the same call's copy
        that started before it, whichever copy's result reads as a success."""
        row, held = self.pre_stamp("lane/bf-rival-two-copies")
        self.session(row, held, [self.call(row, held, "tool_X6", offset=-20)])
        early, _n = self.session(row, held, [
            self.call(row, held, "tool_X7", offset=-30, result=None)],
            seat=OTHER)
        self.session(row, held, [self.call(row, held, "tool_X7", offset=5)],
                     seat=OTHER)
        entry = self.owed_by(row, "RIVAL", "@%s" % OTHER)
        self.assertIn(early, entry["reason"])

    def test_a_file_there_last_written_before_the_window_is_still_read(self):
        """A call can write the hold minutes after its transcript was last
        written (a detached command, or a session that ended while its
        background task ran on), so a file in another seat's store is read
        whatever its age."""
        row, held = self.pre_stamp("lane/bf-rival-quiet-file")
        self.session(row, held, [self.call(row, held, "tool_X8", offset=-20)])
        start, session = held - 300, "fixture-session"
        quiet, _n = self.session(row, held, [[
            _use("tool_X9", self.command(row), _instant(start), session),
            _result("tool_X9", "Command running in background with ID: "
                    "bquiet.", _instant(start + 1), session)]], seat=OTHER)
        os.utime(quiet, (start + 1, start + 1))
        entry = self.owed_by(row, "RIVAL", "@%s" % OTHER)
        self.assertIn(quiet, entry["reason"])

    def test_a_store_two_OTHER_seats_share_is_still_read_for_a_rival(self):
        """Shared is no seat's OWN; it is still another hand's store."""
        row, held = self.pre_stamp("lane/bf-rival-shared")
        self.session(row, held, [self.call(row, held, "tool_V7", offset=-20)])
        self.session(row, held, [self.call(row, held, "tool_V8", offset=-45)],
                     seat=OTHER)
        self.link_home("seat-twin", self.seat_home(OTHER))
        entry = self.owed_by(row, "RIVAL", "@%s" % OTHER)
        self.assertIn("@seat-twin", entry["reason"])

    def test_a_store_another_seats_PROJECTS_dir_resolves_to_is_not_the_recipients_own(self):
        row, held = self.pre_stamp("lane/bf-shared")
        self.session(row, held, [self.call(row, held, "tool_Y1")])
        os.makedirs(self.seat_home(OTHER))
        store = os.path.join(self.seat_home(READER), "projects")
        os.symlink(store, os.path.join(self.seat_home(OTHER), "projects"))
        entry = self.owed_by(row, "SHARED", "@%s" % OTHER)
        self.assertIn(os.path.realpath(store), entry["reason"])

    def test_a_seat_home_two_seats_link_to_is_NEITHER_seats_own(self):
        """The seat census names one config dir once, under the first seat
        its walk meets (`seat-other` sorts before `seat-reader`); read that
        way, @seat-other's row recovers from @seat-reader's transcript."""
        mine, held = self.pre_stamp("lane/bf-shared-home")
        theirs, held2 = self.pre_stamp("lane/bf-shared-home-2",
                                       recipient=OTHER, ago=700)
        self.session(mine, held, [self.call(mine, held, "tool_Y2")])
        self.session(theirs, held2, [self.call(theirs, held2, "tool_Y3")])
        self.link_home(OTHER, self.seat_home(READER))
        # THE PREMISE, pinned: the walk meets @seat-other's link first.
        self.assertEqual(min(OTHER, READER), OTHER)  # noqa: VACUOUS_ASSERTION — a pin on two fixture constants, not an observable
        store = os.path.realpath(os.path.join(self.seat_home(READER),
                                              "projects"))
        entry = self.owed_by(theirs, "SHARED", "@%s" % READER)
        self.assertIn(store, entry["reason"])
        entry = self.owed_by(mine, "SHARED", "@%s" % OTHER)
        self.assertIn(store, entry["reason"])

    def test_an_unreadable_file_in_another_seats_store_leaves_a_rival_unruled_out(self):
        row, held = self.pre_stamp("lane/bf-rival-unread")
        self.session(row, held, [self.call(row, held, "tool_Z1", offset=-20)])
        locked, _n = self.session(row, held, [[_use(
            "tool_Z2", "helm lr list", _instant(held - 5), "s")]], seat=OTHER)
        real = builtins.open

        def guard(*args, **kw):
            if args and isinstance(args[0], (str, bytes, os.PathLike)) \
                    and os.path.abspath(os.fsdecode(args[0])) == locked:
                raise PermissionError(13, "Permission denied", locked)
            return real(*args, **kw)

        with mock.patch.object(builtins, "open", guard):
            entry = self.owed_by(row, "UNREADABLE", locked)
        self.assertIn("rival", entry["reason"])


class _Touched(BaseException):
    """Raised by the guard on a forbidden path: a BaseException, so no
    `except Exception` in the code under test can swallow it."""


class TheCensusStaysUnderTheHelmHomeTest(BackfillBase):
    """NEVER THE OWNER'S CREDENTIAL TREE, NEVER THE DEFAULT CLAUDE STORE.
    HOME points at a temp user dir whose `.claude-homes` and `.claude` both
    hold a transcript that WOULD prove the hold; every open, scandir and
    listdir is guarded, and the guard is shown to fire first."""

    def decoy(self, user, root, calls):
        path = os.path.join(user, root, "acct", "claude", "projects", "-s")
        os.makedirs(path)
        lines = [_opening(_instant(time.time() - 7200), "s")]
        for call in calls:
            lines.extend(call)
        with open(os.path.join(path, "s.jsonl"), "w", encoding="utf-8") as fh:
            fh.write("".join(json.dumps(line) + "\n" for line in lines))
        return os.path.join(user, root, "acct", "claude")

    def test_no_path_under_the_owner_trees_is_ever_opened(self):  # noqa: VACUOUS_ASSERTION — the guard is first shown to raise on a planted open of the decoy, and each row's verdict is asserted by value: a census that read the decoy would find RECOVERABLE proof for both rows there
        linked, held = self.pre_stamp("lane/bf-decoy")
        walked, held2 = self.pre_stamp("lane/bf-decoy-2", recipient=OTHER,
                                       ago=700)
        calls = [self.call(linked, held, "tool_X1"),
                 self.call(walked, held2, "tool_X2")]
        user = os.path.join(self.tmp, "user")
        homes = self.decoy(user, ".claude-homes", calls)
        self.decoy(user, ".claude", calls)
        # ONE RECIPIENT'S SEAT HOME IS A LINK INTO THE CREDENTIAL TREE; THE
        # OTHER'S STORE, AND A THIRD SEAT'S, CARRY A LINK INTO IT.
        seat = os.path.join(home.global_dir(), "seats", READER)
        os.makedirs(seat)
        os.symlink(homes, os.path.join(seat, "claude"))
        for name in (OTHER, "seat-third"):
            os.symlink(os.path.join(homes, "projects", "-s"),
                       os.path.join(self.store(name), "-linked"))
        forbidden = (os.path.join(user, ".claude-homes"),
                     os.path.join(user, ".claude"))
        touched = []

        def guard(real):
            def inner(*args, **kw):
                target = args[0] if args else kw.get("path", kw.get("file"))
                if isinstance(target, (str, bytes, os.PathLike)):
                    text = os.path.abspath(os.fsdecode(target))
                    if any(text == f or text.startswith(f + os.sep)
                           for f in forbidden):
                        touched.append(text)
                        raise _Touched(text)
                return real(*args, **kw)
            return inner

        patches = (mock.patch.dict(os.environ, {"HOME": user}),
                   mock.patch.object(builtins, "open", guard(builtins.open)),
                   mock.patch.object(os, "scandir", guard(os.scandir)),
                   mock.patch.object(os, "listdir", guard(os.listdir)))
        # THE PATCHES CLOSE HERE, NEVER IN A CLEANUP. A cleanup runs after
        # tearDown, and patch.dict's stop puts back the whole environment it
        # saw at start: this test's HELM_HOME and chat dir, after tearDown had
        # restored the caller's. Every later module in the process then read
        # a deleted helm home (tests.test_notify's escalation lock).
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            # THE GUARD FIRES: a planted open of the decoy is refused first.
            with self.assertRaises(_Touched):
                open(os.path.join(homes, "projects", "-s", "s.jsonl"))
            touched.clear()
            report, err = _backfill().census()
        self.assertIsNone(err, err)
        self.assertEqual(touched, [], "the census opened the owner's tree")
        entry = self.entry(report, linked)
        self.assertEqual(entry["kind"], _backfill().OUTSIDE_HELM, entry)
        self.assertIn(os.path.realpath(homes), entry["reason"])
        entry = self.entry(report, walked)
        self.assertEqual(entry["kind"], _backfill().NO_MATCH, entry)


class ATranscriptIsStreamedTest(BackfillBase):
    """A STORE IS HUNDREDS OF MEGABYTES. A 20 MiB single line (a large tool
    result) and 8 MiB of ordinary lines stand before the one tool call; the
    census finds it, at the right line number, inside a memory bound a whole
    read — or a whole read of that one line — could never meet."""

    def test_the_census_holds_one_bounded_line_at_a_time(self):
        row, held = self.pre_stamp("lane/bf-large")
        path = os.path.join(self.store(READER), "large.jsonl")
        filler = {"type": "user", "timestamp": _instant(held - 50),
                  "message": {"role": "user", "content": "x" * 4000}}
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(_opening(_instant(held - 3600), "s")) + "\n")
            fh.write(json.dumps({"type": "user", "message": {
                "role": "user", "content": row["id"][:12] + " --source-clean "
                + "y" * (20 << 20)}}) + "\n")
            for _ in range(2048):
                fh.write(json.dumps(filler) + "\n")
            call = self.call(row, held, "tool_S1")
            fh.write("".join(json.dumps(line) + "\n" for line in call))
        size = os.path.getsize(path)
        self.assertGreater(size, 28 << 20)
        _backfill()
        from helm import hooks  # noqa: F401 — imported before the trace
        dispatches.snapshot()
        tracemalloc.start()
        try:
            report = self.census()
            _now, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        entry = self.entry(report, row)
        self.assertEqual(entry["verdict"], _backfill().RECOVERABLE, entry)
        self.assertEqual(entry["evidence"]["line"], 2 + 2048 + 1)
        self.assertLess(peak, 8 << 20,
                        "the census held %d bytes at once over a %d byte "
                        "transcript" % (peak, size))


class TheFoldBindsTheNamedHoldTest(BackfillBase):
    """THE EVENT SETS THE HOLDER OF THE HOLD IT NAMES AND OF NOTHING ELSE, AND
    REPLAY ASKS EVERY LEDGER LAW THE WRITER ASKED."""

    def recovered(self, lane):
        row, held = self.pre_stamp(lane)
        self.session(row, held, [self.call(row, held, "tool_R1")])
        entry = self.entry(self.census(), row)
        self.assertEqual(entry["verdict"], _backfill().RECOVERABLE, entry)
        return row, entry

    def event(self, row, **over):
        state = self.state(row["id"])
        event = {"v": 3, "event": "hold-actor-backfill",
                 "seq": state["seq"] + 1, "id": row["id"],
                 "ts": pk.now_ts(), "hold_seq": state.get("hold_seq"),
                 "hold_actor": READER,
                 "evidence": {"transcript": "/fixture/t.jsonl", "line": 7,
                              "tool_use_id": "tool_Z1",
                              "command_sha256": "ab" * 32,
                              "tool_ts": _instant(dispatches.instant_epoch(
                                  state["hold_ts"]) - 3)}}
        event.update(over)
        return event

    def append(self, event):
        self.assertTrue(eventledger.append(dispatches.ledger_path(), event))
        return self.state(event["id"])

    def test_replay_folds_the_same_row_as_the_writer_returned(self):
        row, entry = self.recovered("lane/bf-replay")
        out, err = dispatches.record_hold_actor_backfill(
            row["id"], entry["hold_seq"], entry["evidence"])
        self.assertIsNone(err, err)
        self.assertEqual(out["hold_actor"], READER)
        again = self.state(row["id"])
        cold = dispatches._fold(list(eventledger.events(
            dispatches.ledger_path())))[0][row["id"]]
        self.assertEqual(again, out)
        self.assertEqual(cold, out)
        # THE SAME EVIDENCE AGAIN IS THE SAME ROW, and appends nothing.
        count = self.history()
        same, err = dispatches.record_hold_actor_backfill(
            row["id"], entry["hold_seq"], entry["evidence"])
        self.assertIsNone(err, err)
        self.assertEqual(same, out)
        self.assertEqual(self.history(), count)

    def test_a_forged_event_of_every_shape_is_inert_beside_the_admitted_one(self):
        row, _held = self.pre_stamp("lane/bf-forged")
        before = self.state(row["id"])
        hold_ts = dispatches.instant_epoch(before["hold_ts"])
        forged = {
            "another hold's seq": {"hold_seq": before["hold_seq"] - 1},
            "a holder not the recipient": {"hold_actor": OTHER},
            "a call outside the window": {"evidence": dict(
                self.event(row)["evidence"], tool_ts=_instant(hold_ts - 61))},
            "evidence missing a key": {"evidence": {"transcript": "/t"}},
            "a relative transcript": {"evidence": dict(
                self.event(row)["evidence"], transcript="t.jsonl")},
            "an unreadable instant": {"ts": "yesterday"},
        }
        for name, over in forged.items():
            with self.subTest(forged=name):
                self.assertEqual(self.append(self.event(row, **over)), before,
                                 "a forged backfill moved the row")
        # THE CONTROL: the well-formed event, at the next seq, is taken.
        after = self.append(self.event(row, seq=before["seq"] + 1))
        self.assertEqual(after["hold_actor"], READER)

    def test_a_backfill_does_not_outlive_its_hold(self):
        row, entry = self.recovered("lane/bf-rehold")
        dispatches.record_hold_actor_backfill(row["id"], entry["hold_seq"],
                                              entry["evidence"])
        self.assertEqual(self.state(row["id"])["hold_actor"], READER)
        _out, err = dispatches.mark_release(row["id"])
        self.assertIsNone(err, err)
        released = self.state(row["id"])
        self.assertNotIn("hold_actor", released)
        self.assertNotIn("hold_actor_evidence", released)
        self.assertNotIn("hold_seq", released)
        self.planted(row, self.b)
        reheld = self.state(row["id"])
        self.assertNotIn("hold_actor", reheld)
        self.assertNotIn("hold_actor_evidence", reheld)
        # AND AN EVENT NAMING THE OLD HOLD CANNOT REACH THE NEW ONE.
        stale = self.event(row, hold_seq=entry["hold_seq"])
        self.assertNotEqual(entry["hold_seq"], reheld["hold_seq"])
        self.assertEqual(self.append(stale), reheld)

    def test_the_writer_refuses_a_stamped_hold_and_a_lane_author(self):
        stamped = self.held(self.b, "lane/bf-w-stamped")
        state = self.state(stamped["id"])
        count = self.history()
        out, err = dispatches.record_hold_actor_backfill(
            stamped["id"], state["hold_seq"], self.event(stamped)["evidence"])
        self.assertIsNone(out)
        self.assertIn("already records its holder", err)
        self.assertEqual(self.history(), count)
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": READER}):
            first = self.row(self.side, "lane/bf-w-author", recipient=OTHER)
        second = self.row(self.b, "lane/bf-w-author", supersedes=first["id"])
        self.planted(second, self.b)
        state = self.state(second["id"])
        count = self.history()
        out, err = dispatches.record_hold_actor_backfill(
            second["id"], state["hold_seq"], self.event(second)["evidence"])
        self.assertIsNone(out)
        self.assertIn("LANE AUTHOR", err)
        self.assertEqual(self.history(), count)
        # THE CONTROL on the same door: a clean pre-stamp row is written.
        row, _held = self.pre_stamp("lane/bf-w-clean")
        state = self.state(row["id"])
        count = self.history()
        out, err = dispatches.record_hold_actor_backfill(
            row["id"], state["hold_seq"], self.event(row)["evidence"])
        self.assertIsNone(err, err)
        self.assertEqual(self.history(), count + 1)

    def test_the_hold_names_its_seq_at_the_writer_and_in_replay(self):
        row = self.row(self.b, "lane/bf-seq")
        out = self.hold(row, self.b)
        self.assertEqual(out["hold_seq"], out["seq"])
        self.assertEqual(self.state(row["id"])["hold_seq"], out["seq"])

    def test_a_retired_row_takes_no_backfill(self):
        """THE REDUCER'S RETIREMENT GUARD COVERS THE KIND: the same event the
        live row takes is returned unapplied once the row reads retired."""
        row, _held = self.pre_stamp("lane/bf-retired")
        state = self.state(row["id"])
        event = self.event(row)
        taken = dispatches._apply(state, event)
        self.assertIsNot(taken, state, "the live row refused the event, so "
                                       "the retired arm below measures nothing")
        self.assertEqual(taken["hold_actor"], READER)
        with mock.patch.object(dispatches, "_retired_admin_by",
                               return_value="author-unresolvable"):
            self.assertIs(dispatches._apply(state, event), state)

    def test_the_kind_is_known_and_credits_no_hand(self):
        self.assertIn("hold-actor-backfill", dispatches.KNOWN_EVENT_KINDS)
        self.assertEqual(dispatches.LEDGER_EVENT_ACTORS["hold-actor-backfill"],
                         ())
        self.assertIn(("hold-actor-backfill", "hold_actor"),
                      dispatches.LEDGER_NOT_AN_ACTOR)
        self.assertIn("hold-actor-backfill", dispatches._ACTIVE_ONLY_EVENTS)


class TheVerbTest(BackfillBase):
    """`helm lr backfill-hold-actor`: a dry run by default, --apply writes,
    --json is the report, and the help names the verb."""

    def test_dry_run_then_apply_then_json(self):
        row, held = self.pre_stamp("lane/bf-cli")
        self.session(row, held, [self.call(row, held, "tool_V1")])
        before = self.ledger_bytes()
        rc, out, err = _lr_run(["backfill-hold-actor"])
        self.assertEqual(rc, 0, err)
        self.assertIn("RECOVERABLE", out)
        self.assertIn(row["id"][:12], out)
        self.assertIn("dry run", out)
        self.assertEqual(self.ledger_bytes(), before)
        rc, out, err = _lr_run(["backfill-hold-actor", row["id"][:12],
                                "--apply"])
        self.assertEqual(rc, 0, err)
        self.assertIn("WRITTEN", out)
        self.assertEqual(self.state(row["id"])["hold_actor"], READER)
        rc, out, err = _lr_run(["backfill-hold-actor", row["id"][:12],
                                "--json"])
        self.assertEqual(rc, 1, "a row no longer pending is not recoverable")
        report = json.loads(out)
        self.assertEqual(report["rows"][0]["kind"], _backfill().NOT_PENDING)

    def test_an_owed_row_exits_1_and_junk_exits_2(self):
        row, _held = self.pre_stamp("lane/bf-cli-owed")
        self.store(READER)
        rc, out, _err = _lr_run(["backfill-hold-actor"])
        self.assertEqual(rc, 1)
        self.assertIn(_backfill().PROOFS[_backfill().NO_MATCH], out)
        rc, _out, err = _lr_run(["backfill-hold-actor", "--bogus"])
        self.assertEqual(rc, 2)
        self.assertIn("--bogus", err)

    def test_the_help_names_the_verb(self):
        from helm import cli
        self.assertIn("backfill-hold-actor [<id>...] [--apply] [--json]",
                      cli._VERB_HELP["lr"])
        self.assertIn("backfill-hold-actor", landreq.USAGE)


if __name__ == "__main__":
    unittest.main()
