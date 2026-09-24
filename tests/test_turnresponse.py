#!/usr/bin/env python3
"""The session transcript: the one source allowed to revert a row.

EVERY ARM BUILDS A REAL TRANSCRIPT ON DISK and drives the real reader. The
record shapes are derived from the harness's own output rather than invented,
and two of them are the reason this module is not three lines:

  * ONE RESPONSE EMITS SEVERAL `end_turn` RECORDS (a thinking block and a text
    block write two, both stamped `end_turn`).
  * THE FILE IS APPENDED TO WHILE IT IS READ, so the last bytes can be half a
    JSON object.

THE FIVE RULED NEGATIVE CONTROLS (integrator, task/2293) each have an arm:
a refused stop, a non-terminal stop_reason, a first start with no assistant
record, an interrupt, and a transcript for a different session id.
"""
import json
import os
import shutil
import tempfile
import unittest

from tests._tmphome import pin_suite_guard        # noqa: F401
from helm import turnresponse

SESSION = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
OTHER = "ffffffff-1111-2222-3333-444444444444"
T0 = 1789000000.0


def _iso(epoch):
    import datetime
    return datetime.datetime.fromtimestamp(
        epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _assistant(at, stop_reason="end_turn"):
    return {"type": "assistant", "timestamp": _iso(at),
            "message": {"role": "assistant", "stop_reason": stop_reason,
                        "content": [{"type": "text", "text": "x"}]}}


def _user(at, text="go"):
    return {"type": "user", "timestamp": _iso(at),
            "message": {"role": "user", "content": text}}


def _attachment(at):
    return {"type": "attachment", "timestamp": _iso(at), "attachment": {}}


class _Base(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="helm-test-turnresponse-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.projects = os.path.join(self.root, "projects", "-a-project")
        os.makedirs(self.projects)
        # A settings file so the pending window is READ rather than assumed.
        with open(os.path.join(self.root, "settings.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"hooks": {"Stop": [{"hooks": [
                {"command": "guard", "timeout": 30}]}]}}, fh)

    def write(self, records, session=SESSION, partial=None, extra=None):
        """`extra` is a COMPLETE line that will not parse; `partial` is an
        UNFINISHED one. They are different holes and the reader answers them
        differently, so the fixture keeps them apart."""
        path = os.path.join(self.projects, "%s.jsonl" % session)
        with open(path, "w", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r) + "\n")
            if extra is not None:
                fh.write(extra + "\n")
            if partial is not None:
                fh.write(partial)              # NO trailing newline
        return path

    def read(self, session=SESSION, now=None, **kw):
        return turnresponse.last_terminal_response(
            session, now=now if now is not None else T0 + 10000,
            root=self.root, **kw)


class TerminalResponseTest(_Base):

    def test_a_closed_terminal_response_reports_the_RECORDS_own_timestamp(self):
        """THE POSITIVE CONTROL the whole suite leans on. Not `now`, not the
        file's mtime — a file touched by anything else would then read as a
        fresh response."""
        self.write([_user(T0), _assistant(T0 + 5), _user(T0 + 900)])
        r = self.read()
        self.assertIsNone(r.err)
        self.assertIsNone(r.pending)
        self.assertTrue(r.covered)
        self.assertAlmostEqual(T0 + 5, r.ts, places=2)

    def test_ONE_response_spanning_several_end_turn_records_reads_as_one(self):  # noqa: VACUOUS_ASSERTION — the ts equality on the SAME reading is the unconditional positive control; the absence assertion beside it only pins that the answer was not an error
        """MEASURED, not supposed: a response with a thinking block and a text
        block writes two records, BOTH stamped end_turn. Reading the FIRST of
        that run and asking "is anything after it" answers yes about its own
        sibling, so a settled response would look continued forever."""
        self.write([_user(T0), _assistant(T0 + 5), _assistant(T0 + 6),
                    _user(T0 + 900)])
        r = self.read()
        self.assertIsNone(r.err)
        self.assertAlmostEqual(T0 + 6, r.ts, places=2,
                               msg="the run was not collapsed to its last")

    def test_a_LATER_terminal_response_wins_over_an_earlier_one(self):  # noqa: VACUOUS_ASSERTION — the sole assertion is a positive ts equality; there is no absence claim to control
        self.write([_assistant(T0), _user(T0 + 10), _assistant(T0 + 20),
                    _user(T0 + 30)])
        self.assertAlmostEqual(T0 + 20, self.read().ts, places=2)


class TheFiveRuledNegativeControlsTest(_Base):

    def test_a_NON_TERMINAL_stop_reason_is_not_a_response(self):  # noqa: VACUOUS_ASSERTION — opens with an unconditional end_turn read in the SAME shape that IS found; the rung cannot credit a control reached through the self.read helper
        """`tool_use` and `pause_turn` are a turn CONTINUING. Counting them
        would make every seat look responsive at every tool call, which is
        the beacon error with a new field name."""
        # POSITIVE CONTROL, unconditional and first: an end_turn in the SAME
        # shape IS read, so the absences below are the stop_reason and not a
        # reader that sees nothing.
        self.write([_user(T0), _assistant(T0 + 5), _user(T0 + 9)])
        self.assertIsNotNone(self.read().ts)
        for reason in ("tool_use", "pause_turn", "max_tokens", None):
            self.write([_user(T0), _assistant(T0 + 5, stop_reason=reason),
                        _user(T0 + 9)])
            r = self.read()
            self.assertIsNone(r.ts, "stop_reason=%r read as a response" % reason)
            self.assertIsNone(r.err)
            self.assertTrue(r.covered)

    def test_a_FIRST_START_with_no_assistant_record_is_covered_but_empty(self):
        """A session that has started and finished nothing. Covered — the
        transcript exists and was read in full — with no record, which is the
        weaker half of the DEAD conjunction and never the whole of it."""
        self.write([_user(T0)])
        r = self.read()
        self.assertIsNone(r.err)
        self.assertIsNone(r.ts)
        self.assertTrue(r.covered)

    def test_a_DIFFERENT_session_id_is_NOT_COVERED_not_a_borrowed_answer(self):  # noqa: VACUOUS_ASSERTION — closes with an unconditional positive control reading the SAME file under its own session id; the rung cannot credit it through the self.read helper
        """The binding is the session id IN THE FILENAME. A reader that fell
        back to the newest file would hand this seat another seat's turn, and
        on this box twenty seats write into the same directory."""
        self.write([_user(T0), _assistant(T0 + 5), _user(T0 + 9)], session=OTHER)
        r = self.read(session=SESSION)
        self.assertFalse(r.covered, "a foreign transcript was read")
        self.assertIsNone(r.err)
        self.assertIsNone(r.ts)
        # POSITIVE CONTROL: the same file IS found under its own session id,
        # so the miss above is the binding and not an unreadable directory.
        self.assertIsNotNone(self.read(session=OTHER).ts)

    def test_TWO_transcripts_for_one_session_is_an_ERROR_not_a_choice(self):
        """Ambiguity is a repair question. Picking either one is a coin toss
        whose loser is a live seat's liveness."""
        self.write([_assistant(T0), _user(T0 + 9)])
        second = os.path.join(self.root, "projects", "-b-project")
        os.makedirs(second)
        with open(os.path.join(second, "%s.jsonl" % SESSION), "w",
                  encoding="utf-8") as fh:
            fh.write(json.dumps(_assistant(T0 + 500)) + "\n")
        r = self.read()
        self.assertTrue(r.err)
        self.assertIn("2 transcripts", r.err)

    def test_records_AFTER_the_terminal_one_settle_it_at_its_own_time(self):  # noqa: VACUOUS_ASSERTION — the ts equality is the positive control on the same reading; the absent pending flag is the product law being asserted
        """A refused stop appends more records. The response still terminated
        at its own timestamp — what changes is that the pending window no
        longer applies, because something demonstrably followed."""
        self.write([_assistant(T0), _attachment(T0 + 1), _user(T0 + 2)])
        r = self.read(now=T0 + 3)          # well inside the 30s window
        self.assertIsNone(r.pending, "a followed record was held pending")
        self.assertAlmostEqual(T0, r.ts, places=2)


class ThePendingWindowTest(_Base):

    def test_a_terminal_record_with_NOTHING_after_it_is_PENDING_while_young(self):
        """THE WINDOW CODEX NAMED. A Stop hook may still be running and about
        to veto; the harness would then continue the turn and append. Reading
        this as a settled response certifies a turn that was refused."""
        self.write([_user(T0), _assistant(T0 + 5)])
        r = self.read(now=T0 + 10)         # 5s old, window is 30s
        self.assertIsNone(r.ts, "an unsettled record was reported as settled")
        self.assertTrue(r.pending)
        self.assertIn("30s", r.pending)

    def test_the_SAME_record_settles_once_the_window_has_passed(self):  # noqa: VACUOUS_ASSERTION — the discriminating pair IS the control — the same file at an earlier clock is asserted pending unconditionally on the line above
        """The discriminating pair: one file, two clocks. A window that never
        opens is indistinguishable from one that never closes."""
        self.write([_user(T0), _assistant(T0 + 5)])
        self.assertTrue(self.read(now=T0 + 10).pending)
        r = self.read(now=T0 + 5 + 31)
        self.assertIsNone(r.pending)
        self.assertAlmostEqual(T0 + 5, r.ts, places=2)

    def test_the_window_is_READ_FROM_SETTINGS_not_hardcoded(self):  # noqa: VACUOUS_ASSERTION — opens with an unconditional settled read at the default window, so the pending answer after raising the timeout is the setting being read
        """A seat that raises its Stop timeout would otherwise be certified
        early by a constant nobody updated."""
        self.write([_user(T0), _assistant(T0 + 5)])
        self.assertIsNotNone(self.read(now=T0 + 5 + 31).ts)
        with open(os.path.join(self.root, "settings.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"hooks": {"Stop": [{"hooks": [
                {"command": "slow", "timeout": 600}]}]}}, fh)
        r = self.read(now=T0 + 5 + 31)
        self.assertTrue(r.pending, "the raised timeout was not read")
        self.assertIn("600s", r.pending)

    def test_the_LONGEST_stop_hook_sets_the_window(self):
        """Not the first and not the shortest: the dispatch is not settled
        until every hook has answered or timed out."""
        with open(os.path.join(self.root, "settings.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"hooks": {"Stop": [
                {"hooks": [{"command": "fast", "timeout": 5}]},
                {"hooks": [{"command": "slow", "timeout": 400}]}]}}, fh)
        secs, err = turnresponse.stop_hook_timeout_s(root=self.root)
        self.assertIsNone(err)
        self.assertEqual(400.0, secs)

    def test_a_hook_with_NO_declared_timeout_contributes_the_HARNESS_DEFAULT(self):
        """Measured on the live settings: of four real Stop hooks, two declare
        no timeout. Treating those as zero would shrink the window to the
        shortest DECLARED one and certify inside it."""
        with open(os.path.join(self.root, "settings.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"hooks": {"Stop": [{"hooks": [
                {"command": "declared", "timeout": 5},
                {"command": "undeclared"}]}]}}, fh)
        secs, err = turnresponse.stop_hook_timeout_s(root=self.root)
        self.assertIsNone(err)
        self.assertEqual(turnresponse.DEFAULT_HOOK_TIMEOUT_S, secs)

    def test_an_UNPARSEABLE_settings_file_is_an_ERROR_not_the_default(self):
        """Silently substituting the default shortens the window on exactly
        the seats whose configuration could not be checked."""
        with open(os.path.join(self.root, "settings.json"), "w",
                  encoding="utf-8") as fh:
            fh.write("{not json")
        secs, err = turnresponse.stop_hook_timeout_s(root=self.root)
        self.assertIsNone(secs)
        self.assertTrue(err)
        self.write([_user(T0), _assistant(T0 + 5)])
        self.assertTrue(self.read().err, "the census got a usable answer from "
                                         "an unreadable configuration")


class PositionDoesNotOrderTimestampsTest(_Base):
    """The scan reads to a budget and picks by TIMESTAMP, never by position.

    A tail holds the bytes written LAST, which is a fact about WRITE order and
    not about the `timestamp` FIELD — a record written before any cutoff can
    carry any timestamp at all. So "the latest terminal record in the tail" is
    not "the latest terminal record", and no window size makes it one.
    """

    PAD = "p" * 400

    def _bulky(self, count, first_at, gap):
        return [dict(_user(first_at + i * gap), pad=self.PAD)
                for i in range(count)]

    def test_a_RECENT_terminal_written_EARLY_is_not_missed(self):
        """THE ARM A TAIL-ONLY SCAN CANNOT PASS. The recent record sits at the
        FRONT of the file behind a megabyte of padding, with an OLD one at the
        end; a scan that stopped at a tail would report the old timestamp and
        call a live seat quiet."""
        recs = self._bulky(4000, T0, gap=1.0)
        self.write([_assistant(T0 + 9_000_000)] + recs
                   + [_assistant(T0 + 5), _user(T0 + 9)])
        r = self.read(now=T0 + 9_100_000)
        self.assertIsNone(r.err)
        self.assertAlmostEqual(T0 + 9_000_000, r.ts, places=2,
                               msg="the scan reported a positionally-later "
                                   "but OLDER record")

    def test_the_LAST_of_a_tied_run_is_taken(self):
        """One response emits several end_turn records at one instant, and the
        one written second is the later event — the only thing position is
        good for here.

        THE TIED RUN SITS AT EOF INSIDE THE PENDING WINDOW ON PURPOSE: with a
        record after it, or with the run old, both choices produce the same
        timestamp and the arm could not tell first from last. Here the FIRST
        of the run has a successor (its own sibling) while the LAST has none,
        so choosing the first would answer settled and choosing the last
        answers PENDING — a difference this arm can see.
        """
        # POSITIVE CONTROL, unconditional and first: a SINGLE young terminal
        # record with nothing after it pends, so the pending answer below is
        # the run's LAST record being chosen rather than a window that always
        # fires.
        self.write([_user(T0), _assistant(T0 + 5)])
        self.assertTrue(self.read(now=T0 + 10).pending)
        self.write([_user(T0), _assistant(T0 + 5), _assistant(T0 + 5)])
        r = self.read(now=T0 + 10)
        self.assertIsNone(r.err)
        self.assertIsNotNone(r.pending,
                             "the FIRST of the tied run was chosen: it has a "
                             "successor, so the reading settled")
        # and once the window has passed the same run reports that instant
        settled = self.read(now=T0 + 5 + 31)
        self.assertIsNone(settled.pending)
        self.assertAlmostEqual(T0 + 5, settled.ts, places=2)

    def test_past_the_BUDGET_the_reading_is_INCOMPLETE(self):
        recs = self._bulky(400, T0, gap=60.0)
        self.write(recs)
        # POSITIVE CONTROL, unconditional and first: under the real budget the
        # same file answers with complete coverage.
        full = self.read(now=T0 + 99999)
        self.assertIsNone(full.err)
        self.assertIsNone(full.incomplete)
        r = self.read(now=T0 + 99999, max_bytes=2048)
        self.assertIsNotNone(r.incomplete)
        self.assertIn("budget", r.incomplete)


class CoverageIsDirectionalTest(_Base):
    """A hole can only HIDE activity, never manufacture it.

    So an incomplete reading may support a LIVE answer and may never carry a
    row to DARK. Blanket-refusing instead was measured and unusable: a live
    transcript is almost always mid-write, so a trailing fragment is the
    NORMAL state and every rostered seat read UNKNOWN — 46 holding went to 0
    on the live fleet. A predicate that answers UNKNOWN for everyone is not
    conservative, it is silent.
    """

    def test_a_hole_marks_the_reading_INCOMPLETE_rather_than_failing_it(self):
        # POSITIVE CONTROL, unconditional and first: the same records without
        # the hole read complete, so `incomplete` below is the hole.
        self.write([_user(T0), _assistant(T0 + 5), _user(T0 + 9)])
        clean = self.read(now=T0 + 600)
        self.assertIsNone(clean.incomplete)
        self.assertIsNotNone(clean.ts)
        self.write([_user(T0), _assistant(T0 + 5), _user(T0 + 9)],
                   extra="{not json")
        r = self.read(now=T0 + 600)
        self.assertIsNone(r.err, "a hole failed the reading instead of "
                                 "marking it")
        self.assertAlmostEqual(T0 + 5, r.ts, places=2)
        self.assertIn("MORE RECENT", r.incomplete or "")

    def test_a_TRAILING_FRAGMENT_marks_incomplete_and_does_not_fail(self):
        """The normal state of a transcript being written right now."""
        self.write([_user(T0), _assistant(T0 + 5), _user(T0 + 9)],
                   partial='{"type": "assi')
        r = self.read(now=T0 + 600)
        self.assertIsNone(r.err)
        self.assertAlmostEqual(T0 + 5, r.ts, places=2)
        self.assertIsNotNone(r.incomplete)

    def test_an_INCOMPLETE_reading_can_HOLD_a_seat_but_never_reverts_it(self):
        """THE ASYMMETRY, driven through the predicate rather than described.

        A hole could hide a NEWER response, so it can only make a seat look
        older than it is: harmless when the answer is already LIVE, decisive
        when the answer would be a revert.
        """
        from helm import ownership
        rows = {"seat-a": {}}

        def ev(ts, incomplete):
            return lambda s, r: ownership.TurnEvidence(
                ts=ts, source=turnresponse.SOURCE,
                proves_terminal_response=True, incomplete=incomplete)

        now = 1_700_000_000.0
        old = now - 99 * 3600
        # UNCONDITIONAL POSITIVE CONTROLS AT BOTH POLES with COMPLETE
        # coverage, so each answer below is the coverage flag and not the
        # timestamp doing the work.
        self.assertEqual(ownership.HOLDING, ownership.owner_state(
            "seat-a", rows, ev(now - 60, None), lambda s: False, now=now)[0])
        self.assertEqual(ownership.DARK, ownership.owner_state(
            "seat-a", rows, ev(old, None), lambda s: False, now=now)[0])
        # FRESH plus a hole is still HOLDING: a hidden newer record would only
        # make it more alive.
        self.assertEqual(ownership.HOLDING, ownership.owner_state(
            "seat-a", rows, ev(now - 60, "1 record(s) could not be read"),
            lambda s: False, now=now)[0])
        # OLD plus a hole cannot revert: the hole may be the recent response.
        verdict, why = ownership.owner_state(
            "seat-a", rows, ev(old, "1 record(s) could not be read"),
            lambda s: False, now=now)
        self.assertEqual(ownership.UNKNOWN, verdict)
        self.assertIn("could not read all of its evidence", why)

    def test_NO_record_plus_a_hole_also_cannot_revert(self):
        """"Nothing in what I could see" is not "nothing"."""
        from helm import ownership
        rows = {"seat-a": {}}

        def ev(incomplete):
            return lambda s, r: ownership.TurnEvidence(
                ts=None, source=turnresponse.SOURCE,
                proves_terminal_response=True, incomplete=incomplete)
        now = 1_700_000_000.0
        self.assertEqual(ownership.DARK, ownership.owner_state(
            "seat-a", rows, ev(None), lambda s: False, now=now)[0])
        self.assertEqual(ownership.UNKNOWN, ownership.owner_state(
            "seat-a", rows, ev("1 record(s) could not be read"),
            lambda s: False, now=now)[0])


class MalformedInputTest(_Base):

    def test_a_HALF_WRITTEN_final_record_is_NOT_PARSED(self):
        """The harness appends while this reads, so the last bytes can be half
        a JSON object. It must not be PARSED — a prefix can mean something
        else entirely — and the reading it produces is marked INCOMPLETE
        rather than failed, because a fragment is the normal state of a file
        being written and refusing on it silences the whole verb."""
        # POSITIVE CONTROL, unconditional and first: the same records without
        # the fragment read COMPLETE.
        self.write([_user(T0), _assistant(T0 + 5), _user(T0 + 9)])
        clean = self.read(now=T0 + 600)
        self.assertIsNone(clean.incomplete)
        self.write([_user(T0), _assistant(T0 + 5), _user(T0 + 9)],
                   partial='{"type": "assist')
        r = self.read(now=T0 + 600)
        self.assertIsNone(r.err, "a fragment failed the whole reading")
        self.assertAlmostEqual(T0 + 5, r.ts, places=2)
        self.assertIsNotNone(r.incomplete)

    def test_an_UNPARSEABLE_line_BEFORE_the_record_does_not_abort_the_scan(self):
        """One bad line in a 50,000-record file must not cost the whole
        answer. A hole BEFORE the terminal record cannot be more recent than a
        record that follows it, so the positive survives it — which is the
        half of the completeness rule that keeps it proportionate.
        """
        path = self.write([_user(T0)])
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("{not json\n")
            fh.write(json.dumps(_assistant(T0 + 5)) + "\n")
            fh.write(json.dumps(_user(T0 + 9)) + "\n")
        r = self.read(now=T0 + 600)
        self.assertIsNone(r.err)
        self.assertAlmostEqual(T0 + 5, r.ts, places=2)

    def test_a_terminal_record_with_NO_readable_timestamp_is_an_ERROR(self):
        rec = _assistant(T0 + 5)
        rec["timestamp"] = "not-a-time"
        self.write([_user(T0), rec, _user(T0 + 9)])
        r = self.read()
        self.assertIsNone(r.ts)
        self.assertTrue(r.err)

    def test_a_session_id_that_is_not_a_usable_filename_is_REFUSED(self):  # noqa: VACUOUS_ASSERTION — intentional absence assertion: a refused path is the product law, and every other arm in this file proves valid session ids DO resolve
        """The id arrives from a roster row anybody can write, and it becomes
        a PATH."""
        for bad in ("../../etc/passwd", "a/b", "", "x", "."):
            path, err = turnresponse.transcript_path(bad, root=self.root)
            self.assertIsNone(path, "accepted %r" % bad)
            self.assertTrue(err or path is None)

    def test_an_ABSENT_transcript_is_NOT_COVERED_rather_than_empty(self):  # noqa: VACUOUS_ASSERTION — intentional absence assertion: not-covered on an absent file is the product law, and TerminalResponseTest proves a present file IS covered
        """Every proxy-family seat writes no Claude transcript at all. Reading
        that as "has done nothing" is the beacon error with its sign flipped,
        and it would revert a working seat's rows."""
        r = self.read()
        self.assertFalse(r.covered)
        self.assertIsNone(r.err)
        self.assertIsNone(r.ts)


class SettingsAcquisitionTest(_Base):
    """The PENDING bound is read from JSON anybody can edit, on a path with no
    per-seat exception boundary around it — so every consumed container is
    shape-checked and a failure is a structured error, never a raise."""

    def _put(self, obj, name="settings.json"):
        path = os.path.join(self.root, name)
        with open(path, "w", encoding="utf-8") as fh:
            if isinstance(obj, str):
                fh.write(obj)
            else:
                json.dump(obj, fh)

    def test_a_malformed_nested_container_is_an_ERROR_not_a_crash(self):  # noqa: VACUOUS_ASSERTION — opens with an unconditional positive control returning a real 30.0 bound, so every refusal below is the shape and not a reader that never answers
        """`{"hooks": [1]}` makes `.get("Stop")` raise on a list. The census
        calls this reader ONCE globally, outside its per-seat try, so a raise
        here does not degrade one seat's evidence — it takes down the whole
        verb."""
        # POSITIVE CONTROL, unconditional and first: a valid file answers, so
        # every refusal below is the shape and not a reader that never works.
        self._put({"hooks": {"Stop": [{"hooks": [{"timeout": 30}]}]}})
        secs, err = turnresponse.stop_hook_timeout_s(root=self.root)
        self.assertIsNone(err)
        self.assertEqual(30.0, secs)
        for bad in ({"hooks": [1]},
                    {"hooks": {"Stop": {"a": 1}}},
                    {"hooks": {"Stop": [7]}},
                    {"hooks": {"Stop": [{"hooks": 5}]}},
                    {"hooks": {"Stop": [{"hooks": [3]}]}},
                    "{not json"):
            self._put(bad)
            try:
                secs, err = turnresponse.stop_hook_timeout_s(root=self.root)
            except Exception as e:              # noqa: BLE001
                self.fail("acquisition RAISED on %r: %s" % (bad, e))
            self.assertIsNone(secs, "answered over %r" % (bad,))
            self.assertTrue(err)

    def test_a_timeout_must_be_a_FINITE_non_negative_duration(self):  # noqa: VACUOUS_ASSERTION — opens with an unconditional positive control returning 30.0 through the same door
        """NaN is the dangerous one, not the obviously-bad strings: every
        comparison against it is False, so a NaN bound silently answers
        SETTLED for every record. An infinity pends forever."""
        self._put({"hooks": {"Stop": [{"hooks": [{"timeout": 30}]}]}})
        self.assertEqual(
            30.0, turnresponse.stop_hook_timeout_s(root=self.root)[0])
        for bad in (float("nan"), float("inf"), -5, True, "x"):
            self._put({"hooks": {"Stop": [{"hooks": [{"timeout": bad}]}]}})
            secs, err = turnresponse.stop_hook_timeout_s(root=self.root)
            self.assertIsNone(secs, "accepted timeout=%r" % (bad,))
            self.assertTrue(err)

    def test_a_PROJECT_settings_file_raises_the_bound(self):
        """helm merges a per-project `.claude/settings.local.json` into a
        launched session, so a bound read from the user root alone can be
        SHORTER than the session's effective one — and a short bound certifies
        a record the agreed policy says is still PENDING."""
        self._put({"hooks": {"Stop": [{"hooks": [{"timeout": 60}]}]}})
        self.assertEqual(
            60.0, turnresponse.stop_hook_timeout_s(root=self.root)[0])
        project = os.path.join(self.root, "a-project", ".claude")
        os.makedirs(project)
        with open(os.path.join(project, "settings.local.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"hooks": {"Stop": [{"hooks": [{"timeout": 600}]}]}}, fh)
        secs, err = turnresponse.stop_hook_timeout_s(
            root=self.root, project_dirs=[project])
        self.assertIsNone(err)
        self.assertEqual(600.0, secs)
        # AND THE POLICY IT ENFORCES: a 100s-old record is settled under 60
        # and PENDING under 600. Same transcript, same clock, two scopes.
        self.write([_user(T0), _assistant(T0 + 5)])
        self.assertIsNotNone(self.read(now=T0 + 105, timeout_s=60.0).ts)
        self.assertTrue(self.read(now=T0 + 105, timeout_s=600.0).pending)


class TheAllowlistBindingTest(_Base):

    def test_this_source_IS_the_one_ownership_allows(self):
        """The producer and the predicate's allowlist must not drift: if they
        do, the only terminal-response source silently stops proving anything
        and every seat reads UNKNOWN forever."""
        from helm import ownership
        self.assertIn(turnresponse.SOURCE,
                      ownership.TERMINAL_RESPONSE_SOURCES)


if __name__ == "__main__":
    unittest.main()
