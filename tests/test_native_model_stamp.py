#!/usr/bin/env python3
"""The native counterpart of the proxy route's model, and its standing.

A proxy seat's model comes from a third party: the proxy dialed an upstream
and the route proof records which. A native seat has no third party, so the
strongest thing it can reach is its own harness's transcript. These arms pin
that the difference is RECORDED rather than argued -- a native model may never
be read as a measurement -- and that the reader refuses every input it cannot
reduce to one id instead of choosing among them.
"""
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from helm import seats
from helm.native_turn import native_turn_model
from helm.seats_runtime import (MODEL_ABSENT, MODEL_MEASURED,
                                MODEL_SELF_REPORTED, MODEL_SOURCES,
                                MODEL_UNKNOWN, launch_runtime,
                                native_session_model,
                                runtime_model_source)


SESSION = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _transcript(root, session, models, project="proj"):
    """Write one harness-shaped transcript naming `models` in order."""
    directory = os.path.join(root, "projects", project)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "%s.jsonl" % session)
    with open(path, "w", encoding="utf-8") as handle:
        for name in models:
            handle.write(json.dumps(
                {"type": "assistant", "sessionId": session,
                 "message": {"role": "assistant", "model": name}}) + "\n")
    return path


def _transcript_at(root, session, turns, project="proj"):
    """Write one transcript where `turns` is [(epoch_seconds, model), ...] in
    the order the harness writes them. Timestamps use the harness's real
    spelling, ISO with a fractional second and a trailing Z."""
    directory = os.path.join(root, "projects", project)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "%s.jsonl" % session)
    with open(path, "w", encoding="utf-8") as handle:
        for stamp, name in turns:
            text = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(stamp))
            frac = int(round((stamp - int(stamp)) * 1000))
            stamp = "%s.%03dZ" % (text, frac)
            handle.write(json.dumps(
                {"type": "assistant", "sessionId": session,
                 "timestamp": stamp,
                 "message": {"role": "assistant", "model": name}}) + "\n")
    return path


class NativeModelSourceTest(unittest.TestCase):
    """WHICH producer established a runtime's model -- four words, not two."""

    def test_both_known_producers_are_named_and_the_rest_is_unknown(self):  # noqa: VACUOUS_ASSERTION — every assertion here is an EQUALITY to a positive word (measured/self-reported/unknown); nothing in it asserts an absence, so there is no empty observable to control for.
        # The whole point of the vocabulary: a proxy model and a native model
        # are different KINDS of evidence, and a backend this resolver does
        # not recognise must not be folded into either of them.
        self.assertEqual(runtime_model_source(
            {"backend": "proxy", "model": "deepseek-v4-pro"}), MODEL_MEASURED)
        self.assertEqual(runtime_model_source(
            {"backend": "native", "model": "claude-opus-5"}),
            MODEL_SELF_REPORTED)
        self.assertEqual(runtime_model_source(
            {"backend": "someday", "model": "claude-opus-5"}), MODEL_UNKNOWN)

    def test_absent_is_not_unknown_and_neither_is_a_measurement(self):  # noqa: VACUOUS_ASSERTION — the unconditional control is the first assertion in the body: runtime_model_source on a runtime CARRYING a model returns MODEL_SELF_REPORTED, so this resolver is not one that answers ABSENT to everything.
        # ABSENT is the ordinary, correct state of a proxy entry bound by
        # lifecycle or a session before its first turn; UNKNOWN is a record
        # that exists and does not reduce. A field answering one word to both
        # would report a seat that has not started like one that cannot be
        # read.
        # POSITIVE CONTROL, unconditional and on the same observable: this
        # resolver DOES answer a non-absent word for a runtime that carries a
        # model, so the ABSENT answers below are its judgement rather than a
        # function that returns one word for everything.
        self.assertEqual(runtime_model_source(
            {"backend": "native", "model": "claude-opus-5"}),
            MODEL_SELF_REPORTED)
        for runtime in ({"backend": "native"}, {"backend": "proxy"},
                        {"backend": "native", "model": "   "}, {}):
            self.assertEqual(runtime_model_source(runtime), MODEL_ABSENT)
        self.assertEqual(len(set(MODEL_SOURCES)), 4)
        self.assertNotIn(MODEL_ABSENT, (MODEL_UNKNOWN, MODEL_MEASURED))


class NativeSessionModelTest(unittest.TestCase):
    """What the seat's own harness recorded, or a refusal that says why."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.dir, True)

    def test_one_recorded_model_is_self_reported_and_never_measured(self):
        _transcript(self.dir, SESSION, ["claude-opus-5"] * 3)
        model, source = native_session_model(SESSION, self.dir)
        self.assertEqual(model, "claude-opus-5")
        # POSITIVE CONTROL FOR THE ABSENCE BELOW, on the same observable in
        # the same call: this reader DOES return a source word, and that word
        # is the self-reported one, so the inequality that follows is a fact
        # about the value rather than about an unreachable code path.
        self.assertEqual(source, MODEL_SELF_REPORTED)
        self.assertNotEqual(source, MODEL_MEASURED)

    def test_a_session_naming_two_models_answers_neither_of_them(self):  # noqa: VACUOUS_ASSERTION — the unconditional control is the first assertion in the body: the same reader on the same fixture shape with ONE model returns that id, so the None here is the second model.
        # Measured at 16 of 62 live sessions: a seat resumed onto another
        # model, or switched mid-session, has no single model for the session.
        # Choosing the newest would mint a fact about the whole session out of
        # its last turn.
        # POSITIVE CONTROL first, unconditional and on the same observable:
        # one model in this same fixture shape DOES come back, so the None
        # below is the second model and not an empty reader.
        _transcript(self.dir, SESSION, ["claude-opus-5"])
        self.assertEqual(native_session_model(SESSION, self.dir),
                         ("claude-opus-5", MODEL_SELF_REPORTED))
        _transcript(self.dir, SESSION,
                    ["claude-opus-5", "claude-fable-5-1", "claude-opus-5"])
        model, source = native_session_model(SESSION, self.dir)
        self.assertEqual(source, MODEL_UNKNOWN)
        self.assertIsNone(model)

    def test_the_known_non_model_marker_is_skipped_not_counted(self):
        # `<synthetic>` marks a turn the harness composed locally. It is
        # testimony that NO model answered, so it neither supplies an id nor
        # makes an otherwise single-model session ambiguous.
        _transcript(self.dir, SESSION,
                    ["<synthetic>", "claude-opus-5", "<synthetic>"])
        self.assertEqual(native_session_model(SESSION, self.dir),
                         ("claude-opus-5", MODEL_SELF_REPORTED))

    def test_only_the_marker_is_absent_rather_than_a_model(self):
        _transcript(self.dir, SESSION, ["<synthetic>"])
        self.assertEqual(native_session_model(SESSION, self.dir),
                         (None, MODEL_ABSENT))

    def test_an_unrecognised_value_poisons_rather_than_being_dropped(self):  # noqa: VACUOUS_ASSERTION — the unconditional control is the first assertion in the body: the readable id alone reads cleanly through the same call, so the None here is the unrecognised neighbour.
        # A rejected model CLEARS the standing runtime label downstream, so
        # silently discarding a spelling this reader cannot read and returning
        # the neighbouring id would report an unreadable session as a clean
        # one.
        # POSITIVE CONTROL, unconditional and on the same observable: the
        # readable id alone reads cleanly, so the None below is the
        # unrecognised neighbour and not a reader that never returns anything.
        _transcript(self.dir, SESSION, ["claude-opus-5"])
        self.assertEqual(native_session_model(SESSION, self.dir),
                         ("claude-opus-5", MODEL_SELF_REPORTED))
        _transcript(self.dir, SESSION, ["claude-opus-5", "some model!"])
        model, source = native_session_model(SESSION, self.dir)
        self.assertEqual(source, MODEL_UNKNOWN)
        self.assertIsNone(model)

    def test_no_transcript_and_no_session_are_absent(self):
        self.assertEqual(native_session_model(SESSION, self.dir),
                         (None, MODEL_ABSENT))
        self.assertEqual(native_session_model("", self.dir),
                         (None, MODEL_ABSENT))

    def test_two_files_carrying_one_session_id_refuse_to_choose(self):
        _transcript(self.dir, SESSION, ["claude-opus-5"], project="one")
        _transcript(self.dir, SESSION, ["claude-opus-5"], project="two")
        self.assertEqual(native_session_model(SESSION, self.dir),
                         (None, MODEL_UNKNOWN))

    def test_a_record_larger_than_this_reader_reads_answers_unknown(self):
        # `join` runs on a 5s SessionStart hook and the largest live
        # transcript is 1.5 GB, so this reader has a byte bound. A reader that
        # stopped early has NOT established that the session named one model,
        # and reporting the ids it happened to reach would be a partial
        # reading presented as a clean one.
        from helm import seats_runtime
        _transcript(self.dir, SESSION, ["claude-opus-5"] * 4)
        # POSITIVE CONTROL on the same observable in the same call: under a
        # generous bound this exact file reads cleanly, so the UNKNOWN below
        # is the bound and not the fixture.
        self.assertEqual(native_session_model(SESSION, self.dir),
                         ("claude-opus-5", MODEL_SELF_REPORTED))
        original = seats_runtime._TRANSCRIPT_READ_BYTES
        seats_runtime._TRANSCRIPT_READ_BYTES = 8
        try:
            self.assertEqual(native_session_model(SESSION, self.dir),
                             (None, MODEL_UNKNOWN))
        finally:
            seats_runtime._TRANSCRIPT_READ_BYTES = original

    def test_a_partial_trailing_line_does_not_poison_a_live_file(self):
        path = _transcript(self.dir, SESSION, ["claude-opus-5"])
        with open(path, "a", encoding="utf-8") as handle:
            handle.write('{"message": {"model": "claude-op')
        self.assertEqual(native_session_model(SESSION, self.dir),
                         ("claude-opus-5", MODEL_SELF_REPORTED))


class LaunchRuntimeTest(unittest.TestCase):
    """The launch seam, which until now recorded no model at all."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.dir, True)
        self.env = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": SESSION}

    def test_a_native_claude_seat_now_records_which_model_answered(self):
        _transcript(self.dir, SESSION, ["claude-opus-5"])
        runtime = launch_runtime(self.env, self.dir)
        self.assertEqual(runtime.get("model"), "claude-opus-5")
        self.assertEqual(runtime.get("family"), "claude")  # noqa: SEAT_NAME — the model FAMILY token a runtime records, not a seat identity
        self.assertEqual(runtime.get("backend"), "native")
        # AND IT IS STILL A SELF-REPORT. The model and the family are declared
        # by one seam, so recording the model adds a fact and grants no
        # independence.
        self.assertEqual(runtime_model_source(runtime), MODEL_SELF_REPORTED)

    def test_a_fresh_session_records_no_model_rather_than_a_guess(self):
        runtime = launch_runtime(self.env, self.dir)
        # POSITIVE CONTROL on the same observable in the same call: the runtime
        # IS derived and native, so the missing key is the model's absence and
        # not an empty return.
        self.assertEqual(runtime.get("backend"), "native")
        self.assertNotIn("model", runtime)

    def test_an_explicit_stamp_is_testimony_and_is_never_overwritten(self):
        _transcript(self.dir, SESSION, ["claude-opus-5"])
        env = dict(self.env, HELM_MODEL_ID="claude-fable-5")
        self.assertEqual(launch_runtime(env, self.dir).get("model"),
                         "claude-fable-5")
        # Including a malformed one: replacing a value that will be REJECTED
        # downstream with a readable one derived here would turn broken
        # testimony into a clean record.
        env = dict(self.env, HELM_MODEL_ID="not a model!")
        self.assertEqual(launch_runtime(env, self.dir).get("model"),
                         "not a model!")

    def test_a_proxied_seat_never_gets_a_native_model_invented_for_it(self):  # noqa: VACUOUS_ASSERTION — the control is unconditional and precedes the loop: this env and this transcript DO produce a model, so each in-loop absence is the seam declining.
        _transcript(self.dir, SESSION, ["claude-opus-5"])
        # POSITIVE CONTROL, unconditional and outside the loop, on the same
        # observable: this env and this transcript DO produce a model, so each
        # absence below is this seam declining for the seat it was handed.
        self.assertEqual(launch_runtime(self.env, self.dir).get("model"),
                         "claude-opus-5")
        for extra in ({"HELM_MODEL_BACKEND": "proxy"},
                      {"HELM_MODEL_FAMILY": "codex"},
                      {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8317"}):
            runtime = launch_runtime(dict(self.env, **extra), self.dir)
            self.assertTrue(runtime)
            self.assertNotIn("model", runtime)

    def test_the_model_keeps_the_producers_spelling(self):
        # `_RUNTIME_RULES` refuses to casefold the model because re-spelling
        # another producer's identifier is how two systems come to disagree
        # about which one thing they are naming. The env translator reads its
        # casefold flag from that same table per field, so a flat lowercase
        # here cannot silently undo the exception the validator declares.
        env = dict(self.env, HELM_MODEL_ID="Provider/Model-X",
                   HELM_MODEL_FAMILY="CODEX")
        runtime = seats._runtime_environment(env)
        self.assertEqual(runtime.get("model"), "Provider/Model-X")
        # Positive control that every casefolded field still lowercases.
        self.assertEqual(runtime.get("family"), "codex")

    def test_the_stamped_model_survives_the_roster_validator(self):
        # A model the validator REJECTS clears the standing runtime label, so
        # a stamp that could not survive validation would be a fleet-wide
        # authority outage in the shape of an added fact.
        _transcript(self.dir, SESSION, ["claude-opus-5"])
        runtime = launch_runtime(self.env, self.dir)
        metadata, rejected = seats._runtime_metadata(runtime)
        self.assertEqual(metadata, runtime)
        self.assertFalse(rejected)
        # POSITIVE CONTROL, unconditional and on the same observable: this
        # validator DOES reject a model it cannot read, so the False above is
        # the stamp surviving rather than a flag that is never raised.
        _spoiled, spoiled_rejected = seats._runtime_metadata(
            dict(runtime, model="some model!"))
        self.assertTrue(spoiled_rejected)


class NativeModelIsNotARouteTest(unittest.TestCase):
    """A native model must never render as a measured route."""

    def test_a_native_envelope_carrying_a_model_prints_no_route(self):
        from helm import dispatches
        # POSITIVE CONTROL in the same call: a resolved record that DOES carry
        # a route renders one, so the empty string below is this renderer
        # declining on the native shape and not a dead function.
        measured = {"verdict_author_runtime_evidence": {"resolved": {
            "family": "codex", "model": "codex", "provider": "openai",
            "upstream_model": "gpt-6-codex"}}}
        self.assertTrue(dispatches.verdict_resolved_model(measured))
        native = {"verdict_author_runtime_evidence": {"resolved": {  # noqa: SEAT_NAME — the model FAMILY token a resolved runtime records, not a seat identity
            "family": "claude", "model": "claude-opus-5", "provider": None,
            "upstream_model": None}}}
        self.assertEqual(dispatches.verdict_resolved_model(native), "")


class NativeTurnModelTest(unittest.TestCase):
    """What a seat was answering with at one moment, and its standing.

    Unlike `native_session_model` -- one session may span several models and
    therefore refuses to reduce -- this reader answers a single moment: the
    newest model entry, or, at a recorded time, the model the seat was in
    force with then, failing closed when that moment cannot be pinned to one
    id. It reads the main transcript only, and like its sibling it is
    self-reported, never measured.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.dir, True)

    def test_newest_entry_is_the_answer_when_no_moment_is_given(self):
        # ROUTING (no `at`): who answers next? The newest model entry, even
        # though the seat was on an older model earlier in the session.
        _transcript_at(self.dir, SESSION,
                       [(1790500000.0, "claude-opus-5"),
                        (1790500100.0, "claude-opus-5-5")])
        self.assertEqual(native_turn_model(SESSION, root=self.dir),
                         ("claude-opus-5-5", MODEL_SELF_REPORTED))

    def test_a_trailing_synthetic_neither_counts_nor_hides_the_model(self):
        # `<synthetic>` is the harness's own local composition: testimony that
        # no model answered that turn, so it is skipped rather than becoming
        # the newest entry and poisoning the model beside it.
        _transcript_at(self.dir, SESSION,
                       [(1790500000.0, "claude-opus-5-5"),
                        (1790500100.0, "<synthetic>")])
        self.assertEqual(native_turn_model(SESSION, root=self.dir),
                         ("claude-opus-5-5", MODEL_SELF_REPORTED))

    def test_a_moment_inside_a_model_switch_holds_the_model_in_force_then(self):
        # THE FAIL-OPEN CASE: a seat held Sonnet, then switched to Opus.
        # Admitting a read at a moment between the two must answer the model
        # in force THEN (Sonnet), never the one the seat reached later; and
        # the routing question (no moment) still answers the newest (Opus).
        t0 = 1790500000.0
        _transcript_at(self.dir, SESSION,
                       [(t0, "claude-sonnet-5"),
                        (t0 + 600.0, "claude-sonnet-5"),
                        (t0 + 1200.0, "claude-sonnet-5"),
                        (t0 + 2400.0, "claude-opus-5-5"),
                        (t0 + 3000.0, "claude-opus-5-5")])
        self.assertEqual(native_turn_model(SESSION, at=t0 + 1260.0,
                                           root=self.dir),
                         ("claude-sonnet-5", MODEL_SELF_REPORTED))
        self.assertEqual(native_turn_model(SESSION, root=self.dir),
                         ("claude-opus-5-5", MODEL_SELF_REPORTED))

    def test_a_moment_far_past_the_last_turn_fails_closed(self):
        # POSITIVE CONTROL on the same fixture: a moment within the 30-minute
        # window of the last turn answers cleanly, so the UNKNOWN below is the
        # gap, not an empty reader.
        t0 = 1790500000.0
        _transcript_at(self.dir, SESSION, [(t0, "claude-opus-5-5")])
        self.assertEqual(native_turn_model(SESSION, at=t0 + 60.0,
                                           root=self.dir),
                         ("claude-opus-5-5", MODEL_SELF_REPORTED))
        # 45 min after the only turn: no entry lies within 30 min before the
        # moment, so the seat's model at that moment is not established.
        self.assertEqual(native_turn_model(SESSION, at=t0 + 2700.0,
                                           root=self.dir),
                         (None, MODEL_UNKNOWN))

    def test_two_models_inside_the_window_fail_closed(self):
        # POSITIVE CONTROL: two turns of ONE model within the 10-minute window
        # answer cleanly, so the UNKNOWN below is the second model, not an
        # empty reader.
        t0 = 1790500000.0
        _transcript_at(self.dir, SESSION,
                       [(t0, "claude-sonnet-5"),
                        (t0 + 120.0, "claude-sonnet-5")])
        self.assertEqual(native_turn_model(SESSION, at=t0 + 300.0,
                                           root=self.dir),
                         ("claude-sonnet-5", MODEL_SELF_REPORTED))
        # A Sonnet turn and an Opus turn both within 10 min before the moment:
        # the seat's model at that moment is not one id, so the reader refuses.
        _transcript_at(self.dir, SESSION,
                       [(t0, "claude-sonnet-5"),
                        (t0 + 120.0, "claude-opus-5-5")])
        self.assertEqual(native_turn_model(SESSION, at=t0 + 300.0,
                                           root=self.dir),
                         (None, MODEL_UNKNOWN))

    def test_the_main_transcript_ignores_a_subagents_transcript(self):
        # The main transcript -- the path transcript_path names, under
        # projects/<proj>/<session>.jsonl -- is the only record this reader
        # reduces. A subagents/ transcript of the same session may hold a
        # different model and must not reach the answer.
        _transcript_at(self.dir, SESSION,
                       [(1790500000.0, "claude-sonnet-5")], project="proj")
        sub = os.path.join(self.dir, "projects", "proj", SESSION, "subagents")
        os.makedirs(sub, exist_ok=True)
        with open(os.path.join(sub, "agent-1.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(
                {"type": "assistant", "sessionId": SESSION,
                 "timestamp": "2026-09-28T06:17:16.123Z",
                 "message": {"role": "assistant",
                             "model": "claude-opus-5-5"}}) + "\n")
        self.assertEqual(native_turn_model(SESSION, root=self.dir),
                         ("claude-sonnet-5", MODEL_SELF_REPORTED))

    def test_an_unrecognised_value_at_the_answer_entry_fails_closed(self):
        # POSITIVE CONTROL, unconditional and on the same observable: the
        # readable id alone reads cleanly, so the UNKNOWN below is the
        # unrecognised neighbour, not a reader that never returns anything.
        _transcript_at(self.dir, SESSION,
                       [(1790500000.0, "claude-opus-5-5")])
        self.assertEqual(native_turn_model(SESSION, root=self.dir),
                         ("claude-opus-5-5", MODEL_SELF_REPORTED))
        # The newest entry carries a value the id pattern refuses: a model a
        # reader cannot recognise makes the answer unknown, not the neighbouring
        # readable id.
        _transcript_at(self.dir, SESSION,
                       [(1790500000.0, "claude-opus-5-5"),
                        (1790500100.0, "not a model!")])
        self.assertEqual(native_turn_model(SESSION, root=self.dir),
                         (None, MODEL_UNKNOWN))

    def test_no_transcript_is_absent(self):
        self.assertEqual(native_turn_model(SESSION, root=self.dir),
                         (None, MODEL_ABSENT))


def _stamp(epoch):
    """One epoch as the dispatch ledger spells a row's `ts`."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _entry_line(epoch, name, session=SESSION, **extra):
    text = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch))
    return json.dumps(dict(
        {"type": "assistant", "sessionId": session,
         "timestamp": "%s.000Z" % text,
         "message": {"role": "assistant", "model": name}}, **extra)) + "\n"


class _CountingFile:
    """An open binary file that tallies every byte handed to the reader,
    by read() and by line iteration alike."""

    def __init__(self, handle, tally):
        self._handle, self._tally = handle, tally

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._handle.close()

    def __iter__(self):
        for line in self._handle:
            self._tally.append(len(line))
            yield line

    def read(self, size=-1):
        data = self._handle.read(size)
        self._tally.append(len(data))
        return data

    def seek(self, *args):
        return self._handle.seek(*args)

    def tell(self):
        return self._handle.tell()


class NativeTurnBackwardReadTest(unittest.TestCase):
    """The reader walks the transcript from its END (task/3508): both of its
    questions reach only recent turns, and a live native transcript passes
    600 MiB, so a forward scan paid for the whole file to learn its tail."""

    T0 = 1790500000.0

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.dir, True)

    def _write(self, lines):
        directory = os.path.join(self.dir, "projects", "proj")
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "%s.jsonl" % SESSION)
        with open(path, "w", encoding="utf-8") as handle:
            handle.writelines(lines)
        return path

    def _filler(self, total, epoch=None):
        """User lines carrying no model key, `total` bytes of them. Every
        line of a real transcript carries its own `timestamp`, and so do
        these (T0 + 100 unless named): the walk may end on any line older
        than its window, not only on a model entry."""
        text = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(
            self.T0 + 100 if epoch is None else epoch))
        line = json.dumps({"type": "user", "sessionId": SESSION,
                           "timestamp": text,
                           "message": {"role": "user",
                                       "content": "x" * 4000}}) + "\n"
        return [line] * (total // len(line) + 1)

    def _counted(self, *args, **kwargs):
        from helm import native_turn
        tally = []
        real = open

        def spy(path, mode="r", *rest, **kw):
            return _CountingFile(real(path, mode, *rest, **kw), tally)

        with mock.patch.object(native_turn, "open", spy, create=True):
            got = native_turn_model(*args, **kwargs)
        return got, sum(tally)

    def test_a_large_transcript_answers_from_its_tail_in_a_bounded_read(self):
        # 8 MiB of old turns, then the model the seat answers with now.
        path = self._write([_entry_line(self.T0, "claude-sonnet-5")]
                           + self._filler(8 << 20)
                           + [_entry_line(self.T0 + 7200, "claude-opus-5-5")])
        size = os.path.getsize(path)
        got, read = self._counted(SESSION, root=self.dir)
        self.assertEqual(got, ("claude-opus-5-5", MODEL_SELF_REPORTED))
        self.assertLess(read, size // 4, "routing read %d of %d bytes"
                        % (read, size))
        # ADMISSION at a moment one minute after the newest turn: the same
        # bound, because the walk stops on the first line older than the
        # moment's ten-minute window, entry or not.
        got, read = self._counted(SESSION, at=self.T0 + 7260, root=self.dir)
        self.assertEqual(got, ("claude-opus-5-5", MODEL_SELF_REPORTED))
        self.assertLess(read, size // 4, "admission read %d of %d bytes"
                        % (read, size))
        # A moment an hour after the newest turn: no entry lies within 30
        # minutes before it, UNKNOWN, and it too is settled at the tail.
        got, read = self._counted(SESSION, at=self.T0 + 10800, root=self.dir)
        self.assertEqual(got, (None, MODEL_UNKNOWN))
        self.assertLess(read, size // 4, "stale-moment read %d of %d bytes"
                        % (read, size))
        # A moment in the old Sonnet turn's window, reached only by walking
        # back through every newer line: the same reader, the whole walk.
        got, _read = self._counted(SESSION, at=self.T0 + 60, root=self.dir)
        self.assertEqual(got, ("claude-sonnet-5", MODEL_SELF_REPORTED))

    def test_timestamped_lines_end_the_walk_before_the_next_entry(self):
        # No model entry lies within 30 minutes before the moment, and the
        # lines between say so by their own stamps: UNKNOWN, read at the tail
        # rather than by walking back to the next entry 8 MiB away.
        path = self._write([_entry_line(self.T0, "claude-opus-5-5")]
                           + self._filler(8 << 20)
                           + self._filler(4 << 10, epoch=self.T0 + 7000))
        size = os.path.getsize(path)
        got, read = self._counted(SESSION, at=self.T0 + 9000, root=self.dir)
        self.assertEqual(got, (None, MODEL_UNKNOWN))
        self.assertLess(read, size // 4, "read %d of %d bytes" % (read, size))
        # POSITIVE CONTROL on the same file: a moment within 30 minutes of
        # the one entry answers it, so the UNKNOWN above is the gap.
        self.assertEqual(native_turn_model(SESSION, at=self.T0 + 60,
                                           root=self.dir),
                         ("claude-opus-5-5", MODEL_SELF_REPORTED))

    def test_a_line_split_across_reads_is_reassembled(self):
        from helm import native_turn
        self._write([_entry_line(self.T0, "claude-opus-5-5"),
                     _entry_line(self.T0 + 700, "claude-opus-5-5"),
                     _entry_line(self.T0 + 1400, "claude-sonnet-5")])
        # A 7-byte read splits every line many times over.
        with mock.patch.object(native_turn, "_TURN_CHUNK_BYTES", 7):
            self.assertEqual(native_turn_model(SESSION, root=self.dir),
                             ("claude-sonnet-5", MODEL_SELF_REPORTED))
            self.assertEqual(native_turn_model(SESSION, at=self.T0 + 760,
                                               root=self.dir),
                             ("claude-opus-5-5", MODEL_SELF_REPORTED))
            # The FIRST line of the file, reached only once every read
            # before it has been carried back to the file's start.
            self.assertEqual(native_turn_model(SESSION, at=self.T0 + 60,
                                               root=self.dir),
                             ("claude-opus-5-5", MODEL_SELF_REPORTED))

    def test_a_walk_the_byte_bound_cuts_short_is_unknown(self):
        from helm import native_turn
        self._write([_entry_line(self.T0, "claude-opus-5-5")]
                    + self._filler(64 << 10))
        # POSITIVE CONTROL on the same observable: under the real bound the
        # one entry, 64 KiB back from the end, is found.
        self.assertEqual(native_turn_model(SESSION, root=self.dir),
                         ("claude-opus-5-5", MODEL_SELF_REPORTED))
        # A bound that ends before the walk reaches it: the reader stopped
        # short of establishing the newest entry, so it is UNKNOWN, never
        # ABSENT and never a guess.
        with mock.patch.object(native_turn, "_TURN_READ_BYTES", 4096):
            self.assertEqual(native_turn_model(SESSION, root=self.dir),
                             (None, MODEL_UNKNOWN))

    def test_a_sidechain_entry_is_not_the_seats_model(self):
        # POSITIVE CONTROL: the same Haiku entry, written as the seat's own,
        # IS the newest answer, so the Opus below is the flag being read.
        self._write([_entry_line(self.T0, "claude-opus-5-5"),
                     _entry_line(self.T0 + 60, "claude-haiku-4-5")])
        self.assertEqual(native_turn_model(SESSION, root=self.dir),
                         ("claude-haiku-4-5", MODEL_SELF_REPORTED))
        # A subagent's turn written into the main transcript (isSidechain)
        # is that subagent's model, not the seat's.
        self._write([_entry_line(self.T0, "claude-opus-5-5"),
                     _entry_line(self.T0 + 60, "claude-haiku-4-5",
                                 isSidechain=True)])
        self.assertEqual(native_turn_model(SESSION, root=self.dir),
                         ("claude-opus-5-5", MODEL_SELF_REPORTED))

    def test_a_recorded_stamp_is_the_moment_and_an_unreadable_one_is_unknown(self):
        self._write([_entry_line(self.T0, "claude-sonnet-5"),
                     _entry_line(self.T0 + 2400, "claude-opus-5-5")])
        # POSITIVE CONTROL: routing answers the newest turn.
        self.assertEqual(native_turn_model(SESSION, root=self.dir),
                         ("claude-opus-5-5", MODEL_SELF_REPORTED))
        # A ledger row's `ts` is the moment, spelled as the ledger spells it.
        self.assertEqual(native_turn_model(SESSION, at=_stamp(self.T0 + 60),
                                           root=self.dir),
                         ("claude-sonnet-5", MODEL_SELF_REPORTED))
        # A RECORDED read whose moment cannot be placed is UNKNOWN. Falling
        # through to routing's newest turn would admit the Opus the seat
        # reached later, which fails open.
        for at in ("", "not a time", float("nan"), True):
            with self.subTest(at=at):
                self.assertEqual(native_turn_model(SESSION, at=at,
                                                   root=self.dir),
                                 (None, MODEL_UNKNOWN))


class NativeTurnSubagentAdmissionTest(unittest.TestCase):
    """A SUBAGENT ACTS IN ITS SEAT'S NAME (task/3508, the review FIX
    b7c5e42b4398): it runs `helm dispatch hold` and `verdict` as the seat, so
    an Opus seat whose Sonnet subagent held is not an Opus holder. In
    ADMISSION only, an assistant entry stamped in the ten minutes before the
    moment that names another model -- an isSidechain line of the main
    file, or a line of a subagents/ transcript touched in that window --
    makes the answer UNKNOWN. ROUTING reads the seat alone, unchanged."""

    T0 = 1790500000.0
    HOLD = T0 + 1200

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.dir, True)
        # An Opus seat that answered every few minutes up to the hold.
        self.main = [(self.T0 + s, "claude-opus-5-5")
                     for s in (0, 300, 600, 900, 1140)]
        _transcript_at(self.dir, SESSION, self.main)

    def _subagent(self, turns, mtime=None, name="agent-a1.jsonl", run=None):
        """One subagent transcript of SESSION, where the harness writes it:
        <session>/subagents/, or subagents/workflows/<run>/ for a workflow's
        agent. Its mtime is its last entry's, unless named."""
        parts = [self.dir, "projects", "proj", SESSION, "subagents"]
        if run:
            parts += ["workflows", run]
        directory = os.path.join(*parts)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as handle:
            for epoch, model in turns:
                handle.write(_entry_line(epoch, model, isSidechain=True))
        stamp = turns[-1][0] if mtime is None else mtime
        os.utime(path, (stamp, stamp))
        return path

    def _opened(self, **kwargs):
        """(answer at the hold, every path the reader opened)."""
        from helm import native_turn
        paths, real = [], open

        def spy(path, *rest, **kw):
            paths.append(path)
            return real(path, *rest, **kw)

        with mock.patch.object(native_turn, "open", spy, create=True):
            got = native_turn_model(SESSION, at=kwargs.get("at", self.HOLD),
                                    root=self.dir)
        return got, paths

    def test_another_models_subagent_inside_the_window_is_unknown(self):
        # POSITIVE CONTROL: the seat alone is an Opus holder.
        self.assertEqual(native_turn_model(SESSION, at=self.HOLD,
                                           root=self.dir),
                         ("claude-opus-5-5", MODEL_SELF_REPORTED))
        # (a) Its Sonnet subagent wrote an entry three minutes before the
        # hold: Sonnet was acting in the seat's name then.
        self._subagent([(self.HOLD - 180, "claude-sonnet-5")])
        self.assertEqual(native_turn_model(SESSION, at=self.HOLD,
                                           root=self.dir),
                         (None, MODEL_UNKNOWN))
        # ROUTING is unchanged: who answers next is the seat.
        self.assertEqual(native_turn_model(SESSION, root=self.dir),
                         ("claude-opus-5-5", MODEL_SELF_REPORTED))

    def test_a_workflow_agent_of_another_model_is_a_subagent_too(self):
        # A Workflow's agents are written one level down, under
        # subagents/workflows/<run>/, and act in the seat's name the same.
        self._subagent([(self.HOLD - 180, "claude-haiku-4-5")],
                       run="wf_0123abcd-456")
        self.assertEqual(native_turn_model(SESSION, at=self.HOLD,
                                           root=self.dir),
                         (None, MODEL_UNKNOWN))

    def test_a_subagent_entry_past_the_window_leaves_the_seats_model(self):
        # (b) The same Sonnet entry eleven minutes before the hold, in a file
        # touched after it: the file is read, and its entry's own stamp
        # places it outside the window.
        path = self._subagent([(self.HOLD - 660, "claude-sonnet-5")],
                              mtime=self.HOLD + 60)
        got, opened = self._opened()
        self.assertEqual(got, ("claude-opus-5-5", MODEL_SELF_REPORTED))
        self.assertIn(path, opened)

    def test_a_main_file_sidechain_entry_inside_the_window_is_unknown(self):
        # (c) A subagent's turn written into the MAIN transcript.
        def write(side_epoch):
            lines = [_entry_line(t, m) for t, m in self.main]
            lines.append(_entry_line(side_epoch, "claude-sonnet-5",
                                     isSidechain=True))
            lines.sort(key=lambda line: json.loads(line)["timestamp"])
            with open(os.path.join(self.dir, "projects", "proj",
                                   "%s.jsonl" % SESSION), "w",
                      encoding="utf-8") as handle:
                handle.writelines(lines)
        # POSITIVE CONTROL: the same line eleven minutes before the hold is
        # outside the window, so the UNKNOWN below is the line's moment.
        write(self.HOLD - 660)
        self.assertEqual(native_turn_model(SESSION, at=self.HOLD,
                                           root=self.dir),
                         ("claude-opus-5-5", MODEL_SELF_REPORTED))
        write(self.HOLD - 180)
        self.assertEqual(native_turn_model(SESSION, at=self.HOLD,
                                           root=self.dir),
                         (None, MODEL_UNKNOWN))
        self.assertEqual(native_turn_model(SESSION, root=self.dir),
                         ("claude-opus-5-5", MODEL_SELF_REPORTED))

    def test_a_subagent_file_older_than_the_window_is_never_opened(self):
        # (d) A subagent that last wrote fifteen minutes before the hold is
        # never opened; one touched inside the window is (the control).
        old = self._subagent([(self.HOLD - 900, "claude-sonnet-5")],
                             name="agent-old.jsonl")
        fresh = self._subagent([(self.HOLD - 60, "claude-opus-5-5")],
                               name="agent-fresh.jsonl")
        got, opened = self._opened()
        self.assertEqual(got, ("claude-opus-5-5", MODEL_SELF_REPORTED))
        self.assertNotIn(old, opened)
        self.assertIn(fresh, opened)

    def test_a_same_model_subagent_inside_the_window_keeps_the_answer(self):
        # (e) An Opus subagent, and a `<synthetic>` line (the harness's own
        # composition, no model's), both inside the window: read, and
        # neither is another model.
        opus = self._subagent([(self.HOLD - 120, "claude-opus-5-5")])
        synthetic = self._subagent([(self.HOLD - 90, "<synthetic>")],
                                   name="agent-a2.jsonl")
        got, opened = self._opened()
        self.assertEqual(got, ("claude-opus-5-5", MODEL_SELF_REPORTED))
        self.assertIn(opus, opened)
        self.assertIn(synthetic, opened)

    def test_a_subagent_walk_the_byte_bound_cuts_short_is_unknown(self):
        from helm import native_turn
        # A subagent touched in the window whose lines after the hold run
        # past the bound before the walk reaches its window.
        path = self._subagent([(self.HOLD - 900, "claude-sonnet-5")],
                              mtime=self.HOLD + 60)
        filler = json.dumps({
            "type": "user", "sessionId": SESSION, "isSidechain": True,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                                       time.gmtime(self.HOLD + 60)),
            "message": {"role": "user", "content": "x" * 4000}}) + "\n"
        with open(path, "a", encoding="utf-8") as handle:
            handle.writelines([filler] * 16)
        os.utime(path, (self.HOLD + 60, self.HOLD + 60))
        # POSITIVE CONTROL: under the real bound the walk settles it.
        self.assertEqual(native_turn_model(SESSION, at=self.HOLD,
                                           root=self.dir),
                         ("claude-opus-5-5", MODEL_SELF_REPORTED))
        with mock.patch.object(native_turn, "_TURN_READ_BYTES", 8192):
            self.assertEqual(native_turn_model(SESSION, at=self.HOLD,
                                               root=self.dir),
                             (None, MODEL_UNKNOWN))


def _native_evidence(session=SESSION, version=5, **runtime):
    """The family evidence `_approval_identity_family_evidence` returns for
    a verified native roster runtime (dispatches_tier.py)."""
    body = {"agent_harness": "claude", "family": "claude", "backend": "native"}
    body.update(runtime)
    evidence = {"v": version, "identity": "reader-seat",
                "roster_identity": "reader-seat", "runtime": body,
                "runtime_verified": True}
    if version == 5:
        evidence["session"] = session
    return evidence


class NativeRuntimeModelTest(unittest.TestCase):
    """`dispatches._runtime_model` is THE model reader the approval tier,
    `helm reviewers` and auto-land share. A native Claude runtime stamps no
    model, so it reads the seat's own transcript on the session its family
    evidence is bound to: the newest turn for routing, the turn in force at a
    recorded read's moment for admission (task/3508)."""

    T0 = 1790500000.0

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.dir, True)
        env = mock.patch.dict(os.environ, {"HELM_CLAUDE_DIR": self.dir})
        env.start()
        self.addCleanup(env.stop)
        # A seat that held on Sonnet, then switched to Opus.
        _transcript_at(self.dir, SESSION,
                       [(self.T0, "claude-sonnet-5"),
                        (self.T0 + 600, "claude-sonnet-5"),
                        (self.T0 + 1200, "claude-sonnet-5"),
                        (self.T0 + 2400, "claude-opus-5-5"),
                        (self.T0 + 3000, "claude-opus-5-5")])

    def _evidence(self, evidence):
        from helm import dispatches
        return mock.patch.object(
            dispatches, "_approval_identity_family_evidence",
            lambda recipient, session=None, require_exact_session=False:
            ({"claude"}, evidence, "anchor", None))

    def test_a_native_seat_answers_its_transcripts_model_at_the_moment(self):
        from helm import dispatches
        with self._evidence(_native_evidence()):
            # ROUTING: who answers next is the newest turn.
            self.assertEqual(dispatches._runtime_model("reader-seat"),
                             "claude-opus-5-5")
            # ADMISSION: a read recorded in the Sonnet phase is a Sonnet read,
            # as a number or as the ledger's own stamp.
            self.assertEqual(dispatches._runtime_model(
                "reader-seat", at=self.T0 + 1260), "claude-sonnet-5")
            self.assertEqual(dispatches._runtime_model(
                "reader-seat", at=_stamp(self.T0 + 1260)), "claude-sonnet-5")
            self.assertEqual(dispatches._runtime_model(
                "reader-seat", at=_stamp(self.T0 + 3060)), "claude-opus-5-5")

    def test_a_stamp_and_a_measured_route_still_answer_first(self):
        from helm import dispatches
        # The launcher's stamp is testimony and is never replaced.
        with self._evidence(_native_evidence(model="claude-fable-5-1")):
            self.assertEqual(dispatches._runtime_model("reader-seat"),
                             "claude-fable-5-1")
        # A proxy seat's measured route is unchanged by a transcript that
        # happens to carry its session id.
        proxy = {"v": 3, "identity": "reader-seat", "session": SESSION,
                 "proxy_proof": {"route": {"upstream_model": "gpt-6-astra"}}}
        with self._evidence(proxy):
            self.assertEqual(dispatches._runtime_model("reader-seat"),
                             "gpt-6-astra")

    def test_the_author_readers_keep_the_stamp_only_rule(self):
        from helm import dispatches
        with self._evidence(_native_evidence()):
            # POSITIVE CONTROL: the same seat reads its transcript's model.
            self.assertEqual(dispatches._runtime_model("reader-seat"),
                             "claude-opus-5-5")
            self.assertEqual(dispatches._runtime_model(
                "reader-seat", stamped_only=True), None)

    def test_no_bound_session_or_a_non_claude_runtime_reads_no_transcript(self):
        from helm import dispatches
        # POSITIVE CONTROL: the session-bound claude record reads it.
        with self._evidence(_native_evidence()):
            self.assertEqual(dispatches._runtime_model("reader-seat"),
                             "claude-opus-5-5")
        for evidence in (_native_evidence(version=4),
                         _native_evidence(family="codex")):
            with self.subTest(evidence=evidence), self._evidence(evidence):
                self.assertEqual(dispatches._runtime_model("reader-seat"),
                                 None)

    def test_a_model_selector_admits_the_model_in_force_at_the_moment(self):
        from helm import dispatches, seats, store
        policy = {"id": "prior-1", "policy_reason": "opus reads",
                  "policy_members": ["model:claude-opus-5-5"]}
        with self._evidence(_native_evidence()), \
                mock.patch.object(store, "load_certain_policy",
                                  lambda *a, **k: (policy, None)), \
                mock.patch.object(seats, "resolve_recipient",
                                  lambda name: (name, None)):
            # ROUTING: the seat answers on Opus now, so it is in the tier.
            self.assertEqual(dispatches.approval_tier(
                "reader-seat", repo=self.dir)[0], "ok")
            self.assertEqual(dispatches.approval_tier(
                "reader-seat", repo=self.dir, at=_stamp(self.T0 + 3060))[0],
                "ok")
            # A read recorded in the Sonnet phase was not an Opus read.
            self.assertEqual(dispatches.approval_tier(
                "reader-seat", repo=self.dir, at=_stamp(self.T0 + 1260))[0],
                "outside")


if __name__ == "__main__":
    unittest.main()
