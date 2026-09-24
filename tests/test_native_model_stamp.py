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
import unittest

from helm import seats
from helm.seats_runtime import (MODEL_ABSENT, MODEL_MEASURED,
                                MODEL_SELF_REPORTED, MODEL_SOURCES,
                                MODEL_UNKNOWN, launch_runtime,
                                native_session_model, runtime_model_source)


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


if __name__ == "__main__":
    unittest.main()
