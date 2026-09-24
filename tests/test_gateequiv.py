import contextlib
import copy
import io
import json
import hashlib
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from helm import gate, gateequiv, gateshard, gatetestrecord


def _sample(phase=None, workers=None, load=2.0, competitors=None):
    row = {
        "load": [load, load, load],
        "affinity_cores": 8,
        "host_cores": 8,
        "memory": {"MemAvailable": 8 << 30, "MemFree": 4 << 30},
        "competing_suites": list(competitors or ()),
    }
    if phase is not None:
        row["phase"] = phase
    if workers is not None:
        row["workers"] = list(workers)
    return row


def _timing_arms(serial, sharded, loads=None, competitors=None):
    walls = {"serial": iter(serial), "sharded": iter(sharded)}
    loads = iter(loads or [2.0] * (len(serial) + len(sharded)))
    rows = []
    for trial in gateequiv._timing_trials(len(serial)):
        load = next(loads)
        rows.append(dict(
            trial,
            receipt_wall=next(walls[trial["kind"]]),
            receipt={"status": "OK", "ran": 1, "skipped": 0},
            reason=None,
            before=_sample(load=load, competitors=competitors),
            after=_sample(load=load, competitors=competitors),
        ))
    return rows


class _LiveArm(dict):
    """An arm whose byte evidence is re-minted on every read.

    THESE FIXTURES MUTATE THEIR ROWS AFTER CONSTRUCTION -- an outcome flipped,
    a count compensated, a failfast boundary moved -- and the binder compares
    a supervisor's claimed SHA-256 against the digest preserved at collection.
    Digests computed once at setUp go stale the moment a test edits a row, so
    the arm would fail on evidence drift rather than on the property under
    test, and every such arm would be measuring the fixture.

    Production has no such problem: collection happens once, after the run,
    and nothing edits a row afterwards.
    """

    def __init__(self, case, kind, artifacts):
        super().__init__(case._arm_body(kind, artifacts))
        self._case = case
        self._artifacts = artifacts

    def __getitem__(self, key):
        if key == "artifact_files":
            return self._case._mint_files(self._artifacts)
        return super().__getitem__(key)

    def get(self, key, default=None):
        if key == "artifact_files":
            return self._case._mint_files(self._artifacts)
        return super().get(key, default)


class _CustomResult(unittest.TextTestResult):
    made = 0

    def __init__(self, *args, **kwargs):
        _CustomResult.made += 1
        super().__init__(*args, **kwargs)


class _SlottedResult(unittest.TextTestResult):
    __slots__ = ()


class _ExactResult(unittest.TextTestResult):
    def addSuccess(self, test):
        callback = self.addSuccess
        if type(self) is not _ExactResult \
                or callback.__func__ is not type(self).addSuccess \
                or "addSuccess" in self.__dict__:
            raise RuntimeError("result callback identity changed")
        super().addSuccess(test)


class OutcomeRecorderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.context = {
            "token": "a" * 32,
            "role": "worker",
            "root_pid": os.getpid(),
            "root_start": gatetestrecord.process_start(),
            "shard": 2,
            "modules": [__name__],
        }

    def tearDown(self):
        self.tmp.cleanup()

    def record_suite(self, cases, resultclass=unittest.TextTestResult):
        suite = unittest.TestSuite(cases)
        runner = unittest.TextTestRunner(
            stream=open(os.devnull, "w", encoding="utf-8"),
            resultclass=resultclass,
        )
        self.addCleanup(runner.stream.close)
        result, path = gatetestrecord.run_recorded(
            runner, suite, self.tmp.name, self.context)
        return result, path, runner

    def run_suite(self, cases, resultclass=unittest.TextTestResult):
        result, path, runner = self.record_suite(cases, resultclass)
        with open(path, encoding="utf-8") as fh:
            return result, json.load(fh), runner

    def test_module_timing_matches_nonempty_modules_and_sorts_ties_by_name(self):
        class Alpha(unittest.TestCase):
            def runTest(self):
                pass

        class Beta(unittest.TestCase):
            def runTest(self):
                pass

        Alpha.__module__ = "tests.test_alpha"
        Beta.__module__ = "tests.test_beta"
        alpha, beta = Alpha(), Beta()
        ledger = gatetestrecord._Ledger(
            [alpha.id(), beta.id()], [Alpha.__module__, Beta.__module__])
        # Exact clocks make the ranking arm bite: equal wall time must use the
        # module name as its deterministic tie-break, while CPU remains measured.
        with mock.patch.object(
                gatetestrecord.time, "perf_counter",
                side_effect=(10.0, 12.0, 20.0, 22.0)), \
                mock.patch.object(
                    gatetestrecord.time, "process_time",
                    side_effect=(1.0, 1.5, 2.0, 2.25)):
            ledger.start(beta, beta.id())
            ledger.stop(beta, beta.id())
            ledger.start(alpha, alpha.id())
            ledger.stop(alpha, alpha.id())
        timing = gatetestrecord.timing_summary(ledger)
        self.assertEqual(timing["state"], "COMPLETE")
        self.assertEqual(timing["planned_modules"], 2)
        self.assertEqual(timing["measured_modules"], 2)
        self.assertEqual(timing["measured_tests"], 2)
        self.assertGreater(timing["module_wall"], 0,
                           "positive control: real matched modules were timed")
        self.assertEqual([row["module"] for row in timing["top"]], [
            "tests.test_alpha", "tests.test_beta"])
        self.assertEqual(timing["process_cpu"], 0.75)

    def test_raw_multi_module_aggregate_reconciles_at_footer_boundary(self):
        planned = ["tests.test_alpha.Case.test_one",
                   "tests.test_beta.Case.test_two"]
        modules = ["tests.test_alpha", "tests.test_beta"]
        ledger = gatetestrecord._Ledger(planned, modules)
        ledger.started = list(planned)
        ledger.stopped = list(planned)
        for module in modules:
            ledger._timings[module].update({
                "tests": 1, "wall": 0.5002506, "process_cpu": 0.2500006})
        timing = gatetestrecord.timing_summary(ledger)
        self.assertEqual(timing["state"], "COMPLETE")
        self.assertEqual([row["wall"] for row in timing["top"]],
                         [0.500251, 0.500251])
        self.assertEqual(
            round(sum(row["wall"] for row in timing["top"]), 6), 1.000502,
            "must-hit control: per-module rounding accumulates one microsecond")
        self.assertEqual(timing["module_wall"], 1.000501,
                         "the aggregate rounds the raw sum exactly once")
        self.assertEqual(timing["process_cpu"], 0.500001,
                         "CPU aggregation follows the same owner-layer rule")
        receipt = {"id": "a" * 16, "ran": 2, "elapsed": 1.0}
        event = gate._module_timing_event(receipt, timing)
        self.assertEqual(event["state"], "COMPLETE")
        self.assertIsNone(gate._timing_event_error(event))
        beyond = copy.deepcopy(event)
        beyond["module_wall"] += 0.000001
        beyond["id"] = gate._timing_id(beyond)
        self.assertIn("exceeds runner elapsed",
                      gate._timing_event_error(beyond))

    def test_three_correlated_half_microsecond_rows_reconcile(self):
        modules = ["tests.test_%s" % name for name in ("a", "b", "c")]
        planned = ["%s.Case.test_one" % module for module in modules]
        ledger = gatetestrecord._Ledger(planned, modules)
        ledger.started = list(planned)
        ledger.stopped = list(planned)
        for module in modules:
            ledger._timings[module].update({
                "tests": 1, "wall": 0.0000005, "process_cpu": 0.0})
        timing = gatetestrecord.timing_summary(ledger)
        self.assertEqual([row["wall"] for row in timing["top"]], [0.0] * 3)
        self.assertEqual(timing["module_wall"], 0.000002,
                         "must-hit control: correlated row rounding differs by "
                         "two microseconds from the once-rounded raw aggregate")
        event = gate._module_timing_event(
            {"id": "a" * 16, "ran": 3, "elapsed": 0.001}, timing)
        self.assertEqual(event["state"], "COMPLETE")
        self.assertIsNone(gate._timing_event_error(event))

    def test_missing_planned_module_makes_timing_unknown_without_a_top_list(self):
        class Seen(unittest.TestCase):
            def runTest(self):
                pass

        Seen.__module__ = "tests.test_seen"
        seen = Seen()
        ledger = gatetestrecord._Ledger(
            [seen.id(), "tests.test_lost.Case.test_lost"],
            [Seen.__module__, "tests.test_lost"])
        with mock.patch.object(
                gatetestrecord.time, "perf_counter", side_effect=(1.0, 2.0)), \
                mock.patch.object(
                    gatetestrecord.time, "process_time", side_effect=(1.0, 1.1)):
            ledger.start(seen, seen.id())
            ledger.stop(seen, seen.id())
        timing = gatetestrecord.timing_summary(ledger)
        self.assertEqual(timing["measured_modules"], 1,
                         "positive control: the parser matched a real module")
        self.assertGreater(timing["module_wall"], 0)
        self.assertEqual(timing["state"], "UNKNOWN")
        self.assertEqual(timing["top"], [])
        # THE REASON MUST NAME THE INPUT THAT FAILED. The census is a
        # conjunction; while it reported one fixed sentence, a partial census
        # could not be acted on, and every whole-suite receipt carried that
        # sentence and withheld its ranking on it.
        self.assertIn("tests.test_lost", timing["reason"])
        self.assertIn("1 planned module(s) contributed no timing",
                      timing["reason"])
        self.assertIn("1 planned test(s) never started", timing["reason"])
        self.assertNotIn("tests.test_seen", timing["reason"],
                         "control: the module that WAS timed is not accused")

    def test_census_reason_separates_each_failing_input(self):
        """Each conjunct gets its own sentence, and only when it is the one
        that failed. A reason that named every input on every failure would be
        no more actionable than the one sentence it replaced."""
        def summarize(ledger):
            return gatetestrecord.timing_summary(ledger)["reason"]

        planned = ["tests.test_a.Case.test_one"]
        ran = gatetestrecord._Ledger(planned, ["tests.test_a"])
        ran.started = list(planned)
        ran.stopped = list(planned)
        ran._timings["tests.test_a"].update(
            {"tests": 1, "wall": 0.1, "process_cpu": 0.05})
        self.assertIsNone(summarize(ran),
                          "positive control: a whole census has no reason")

        invalid = copy.deepcopy(ran)
        invalid.valid = False
        self.assertIn("the outcome ledger is not valid", summarize(invalid))
        self.assertNotIn("contributed no timing", summarize(invalid),
                         "an intact module census is not also accused")

        unplanned = copy.deepcopy(ran)
        unplanned._timings["tests.test_ghost"].update(
            {"tests": 1, "wall": 0.1, "process_cpu": 0.0})
        reason = summarize(unplanned)
        self.assertIn("1 timed module(s) were never planned", reason)
        self.assertIn("tests.test_ghost", reason)

        mismatched = copy.deepcopy(ran)
        mismatched.started = list(planned) * 2
        self.assertIn("timed 1 test(s) against 2 started",
                      summarize(mismatched))

    def test_census_reason_is_bounded_when_the_whole_suite_is_unmeasured(self):
        """The diagnosis names a few and counts the rest. It rides an advisory
        event with a per-event byte limit, so it must not grow with the
        suite."""
        modules = ["tests.test_m%03d" % index for index in range(400)]
        planned = ["%s.Case.test_one" % module for module in modules]
        ledger = gatetestrecord._Ledger(planned, modules)
        reason = gatetestrecord.timing_summary(ledger)["reason"]
        self.assertLessEqual(len(reason), gatetestrecord.TIMING_REASON_CAP)
        self.assertIn("400 planned module(s) contributed no timing", reason)
        self.assertIn("and %d more" % (400 - gatetestrecord.TIMING_NAME_CAP),
                      reason)
        self.assertIn("tests.test_m000", reason,
                      "positive control: the sample is real module names")
        self.assertNotIn("tests.test_m399", reason,
                         "must-hit control: the tail is counted, never named")

    def _long_census(self, stem, count=418):
        """A REAL census over `count` unstarted modules plus one timed module
        nobody planned: four gaps, and the unplanned one is named LAST."""
        modules = ["tests.%s_%03d" % (stem, index) for index in range(count)]
        ledger = gatetestrecord._Ledger(
            ["%s.Case.test_one" % module for module in modules], modules)
        ledger._timings["tests.test_ghost"].update(
            {"tests": 1, "wall": 0.1, "process_cpu": 0.0})
        timing = gatetestrecord.timing_summary(ledger)
        event = gate._module_timing_event(
            {"id": "a" * 16, "ran": 0, "elapsed": 60.0}, timing)
        return timing, event

    def _shown(self, event):
        out = io.StringIO()
        with mock.patch.object(gate, "_timing_for_receipt",
                               return_value=(event, None)), \
                contextlib.redirect_stdout(out):
            gate._show_module_timing(event["receipt"])
        return out.getvalue()

    def test_a_census_reason_past_the_failure_cap_survives_whole(self):
        """The census names its gaps in order under its own 2000-character
        bound. A 500-character failure-text cut on the way to the timing
        event or to `gate show` removes every gap past character 500 whole,
        and one receipt lost a 418-module gap that way. The reason must reach
        the event and the screen WHOLE."""
        timing, event = self._long_census("test_census_reason_module")
        reason = timing["reason"]
        last = "1 timed module(s) were never planned (tests.test_ghost)"
        self.assertTrue(reason.endswith(last),
                        "MUST-HIT: the census itself kept its last gap")
        self.assertGreater(reason.index(last), gate._FAILURE_TEXT_CAP,
                           "control: the last gap starts past the 500 cap")
        self.assertLessEqual(len(reason), gatetestrecord.TIMING_REASON_CAP)
        self.assertIsNone(gate._timing_event_error(event))
        self.assertEqual(event["state"], "UNKNOWN")
        self.assertEqual(event["reason"], reason,
                         "the event keeps the census reason whole")
        self.assertIn("timing     UNKNOWN (%s)\n" % reason,
                      self._shown(event),
                      "gate show prints the census reason whole")

    def test_a_census_reason_stops_at_the_census_bound_not_before(self):
        """Up to the census bound and no further. Long module names fill the
        census reason to exactly its cap; the event and `gate show` keep all
        of it. A reason longer than the census bound, which no census writes,
        is still cut, visibly, at that bound."""
        timing, event = self._long_census("test_" + "long_module_name_" * 9)
        reason = timing["reason"]
        self.assertEqual(len(reason), gatetestrecord.TIMING_REASON_CAP,
                         "MUST-HIT: the census filled its own bound")
        self.assertEqual(event["reason"], reason)
        self.assertIsNone(gate._timing_event_error(event))
        self.assertIn("content id does not match", gate._timing_event_error(
            dict(event, reason=reason[:-1])),
            "positive control: the reader does judge this reason")
        self.assertIn("UNKNOWN (%s)\n" % reason, self._shown(event))
        over = gate._module_timing_event(
            {"id": "a" * 16, "ran": 0, "elapsed": 60.0},
            dict(timing, reason="r" * (gatetestrecord.TIMING_REASON_CAP + 1)))
        self.assertEqual(
            over["reason"],
            "r" * (gatetestrecord.TIMING_REASON_CAP - 3) + "...")
        self.assertEqual(gate._failure_text("r" * 600),
                         "r" * (gate._FAILURE_TEXT_CAP - 3) + "...",
                         "real failure text keeps its 500 cap")

    def test_old_clipped_and_new_whole_census_reasons_both_read(self):
        """The id hashes the stored reason, so a row keeps the id it was
        written with: a row minted with the old 500-character cut still
        recomputes and reads, and a whole reason reads through the same,
        unchanged, reader (the one the importer calls)."""
        timing, event = self._long_census("test_census_reason_module")
        for reason in (gate._failure_text(timing["reason"]), timing["reason"]):
            row = dict(event, reason=reason)
            row["id"] = gate._timing_id(row)
            self.assertIsNone(gate._timing_event_error(row), len(reason))
        self.assertTrue(gate._failure_text(timing["reason"]).endswith("..."),
                        "control: the old row really was clipped")

    def test_records_compound_subtests_cleanup_and_ordinary_outcomes(self):
        class Probe(unittest.TestCase):
            def test_compound(self):
                with self.subTest(arm="fail"):
                    self.fail("failure")
                with self.subTest(arm="error"):
                    raise RuntimeError("error")

            def tearDown(self):
                def cleanup():
                    raise RuntimeError("cleanup")
                self.addCleanup(cleanup)

        _result, row, _runner = self.run_suite([Probe("test_compound")])
        kinds = [event["kind"] for event in row["events"]]
        self.assertEqual(kinds, [
            "start", "subtest-failure", "subtest-error", "error", "stop",
        ])
        self.assertEqual(row["planned"], [Probe("test_compound").id()])
        self.assertEqual(row["started"], [Probe("test_compound").id()])
        self.assertEqual(row["unexecuted"], [])
        self.assertEqual(row["counts"]["ran"], 1)
        self.assertEqual(row["counts"]["failures"], 1)
        self.assertEqual(row["counts"]["errors"], 2)

    def test_records_skip_expected_failure_and_unexpected_success(self):
        class Outcomes(unittest.TestCase):
            @unittest.skip("not on this node")
            def test_skip(self):
                pass

            @unittest.expectedFailure
            def test_xfail(self):
                self.fail("expected")

            @unittest.expectedFailure
            def test_xpass(self):
                pass

        _result, row, _runner = self.run_suite([
            Outcomes("test_skip"), Outcomes("test_xfail"),
            Outcomes("test_xpass"),
        ])
        terminal = [event["kind"] for event in row["events"]
                    if event["kind"] not in ("start", "stop")]
        self.assertEqual(terminal, [
            "skipped", "expected-failure", "unexpected-success",
        ])
        self.assertEqual(row["counts"]["skipped"], 1)
        self.assertEqual(row["counts"]["expected_failures"], 1)
        self.assertEqual(row["counts"]["unexpected_successes"], 1)

    def test_skipped_subtest_is_owned_by_active_parent(self):
        class Probe(unittest.TestCase):
            def test_skip(self):
                with self.subTest(arm="skip"):
                    self.skipTest("not on this node")

        test = Probe("test_skip")
        _result, row, _runner = self.run_suite([test])
        self.assertEqual(row["started"], [test.id()])
        self.assertEqual(row["stopped"], [test.id()])
        self.assertEqual(row["unexecuted"], [])
        self.assertEqual([event["kind"] for event in row["events"]], [
            "start", "subtest-skipped", "stop",
        ])
        self.assertEqual(row["events"][1]["test"], test.id())
        self.assertIn("arm='skip'", row["events"][1]["subtest"])
        self.assertEqual(row["counts"]["skipped"], 1)
        gatetestrecord.validate_artifact(row, self.context["token"])
        self.assertEqual(gateequiv._outcomes([row])[1], [])

    def test_fixture_event_without_start_and_unexecuted_test_stay_explicit(self):
        class FixtureError(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                raise RuntimeError("fixture")

            def test_never_runs(self):
                self.fail("unreachable")

        _result, row, _runner = self.run_suite(
            [FixtureError("test_never_runs")])
        self.assertEqual(row["started"], [])
        self.assertEqual(row["unexecuted"], [
            FixtureError("test_never_runs").id(),
        ])
        self.assertEqual(row["events"][0]["kind"], "error")
        self.assertIn("setUpClass", row["events"][0]["test"])

    def test_fixture_skip_without_start_stays_explicit(self):
        class FixtureSkip(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                raise unittest.SkipTest("fixture")

            def test_never_runs(self):
                self.fail("unreachable")

        FixtureSkip.__qualname__ = "FixtureSkip"
        _result, row, _runner = self.run_suite([
            FixtureSkip("test_never_runs")])
        self.assertEqual(row["started"], [])
        self.assertEqual(row["events"][0]["kind"], "skipped")
        self.assertEqual(row["events"][0]["test"],
                         __name__ + ".FixtureSkip.setUpClass")

    def test_duplicate_ids_remain_a_multiset(self):
        class Duplicate(unittest.TestCase):
            def runTest(self):
                pass

        first = Duplicate()
        second = Duplicate()
        _result, row, _runner = self.run_suite([first, second])
        self.assertEqual(row["planned"], [first.id(), second.id()])
        self.assertEqual(row["started"], [first.id(), second.id()])
        self.assertEqual(sum(event["kind"] == "ok"
                             for event in row["events"]), 2)
        gatetestrecord.validate_artifact(row, self.context["token"])

    def test_preserves_custom_resultclass_and_test_time_nested_runner(self):
        class Nested(unittest.TestCase):
            def test_nested(self):
                runner = unittest.TextTestRunner(
                    stream=open(os.devnull, "w", encoding="utf-8"),
                    resultclass=_CustomResult,
                )
                self.addCleanup(runner.stream.close)
                result = runner.run(unittest.FunctionTestCase(lambda: None))
                self.assertIs(type(result), _CustomResult)

        _CustomResult.made = 0
        result, row, runner = self.run_suite(
            [Nested("test_nested")], resultclass=_CustomResult)
        self.assertIs(runner.resultclass, _CustomResult)
        self.assertIs(type(result), _CustomResult)
        self.assertEqual(_CustomResult.made, 2)
        self.assertEqual([event["kind"] for event in row["events"]],
                         ["start", "ok", "stop"])

    def test_recording_preserves_callback_visible_exact_result_identity(self):  # noqa: VACUOUS_ASSERTION — successful exact-type callbacks and the recorded event sequence are unconditional controls for the absence assertions
        class Passing(unittest.TestCase):
            def runTest(self):
                pass

        result, row, runner = self.run_suite(
            [Passing()], resultclass=_ExactResult)
        self.assertIs(runner.resultclass, _ExactResult)
        self.assertIs(type(result), _ExactResult)
        self.assertTrue(result.wasSuccessful())
        self.assertNotIn("addSuccess", result.__dict__)
        self.assertNotIn("startTest", result.__dict__)
        self.assertEqual([event["kind"] for event in row["events"]],
                         ["start", "ok", "stop"])
        self.assertIsNone(sys.getprofile())

    def test_callback_identity_is_snapshotted_before_custom_mutation(self):
        class MutatingResult(unittest.TextTestResult):
            def addSuccess(self, test):
                test.id = lambda: "forged.after.callback"
                super().addSuccess(test)

        test = unittest.FunctionTestCase(lambda: None)
        original = test.id()
        _result, row, _runner = self.run_suite(
            [test], resultclass=MutatingResult)
        self.assertEqual(row["planned"], [original])
        self.assertEqual(row["events"], [
            {"test": original, "kind": "start"},
            {"test": original, "kind": "ok"},
            {"test": original, "kind": "stop"},
        ])

    def test_raising_custom_callback_cannot_publish_partial_ledger(self):  # noqa: VACUOUS_ASSERTION — the raised callback is the positive execution control; no artifact proves partial evidence was refused
        class RaisingResult(unittest.TextTestResult):
            def addSuccess(self, test):
                raise RuntimeError("callback refused")

        with self.assertRaisesRegex(RuntimeError, "callback refused"):
            self.record_suite([
                unittest.FunctionTestCase(lambda: None),
            ], resultclass=RaisingResult)
        self.assertEqual(os.listdir(self.tmp.name), [])
        self.assertIsNone(sys.getprofile())

    def test_intentional_callback_mutation_is_preserved_without_artifact(self):
        mutation = []

        class Mutating(unittest.TestCase):
            def run(self, result=None):
                original = result.addSuccess

                def replacement(test):
                    mutation.append(replacement)
                    return original(test)

                result.addSuccess = replacement
                mutation.append(replacement)
                return super().run(result)

            def runTest(self):
                pass

        result, path, _runner = self.record_suite([Mutating()])
        self.assertTrue(result.wasSuccessful())
        self.assertIsNone(path)
        self.assertIs(result.addSuccess, mutation[0])
        self.assertEqual(mutation, [mutation[0], mutation[0]])

    def test_successful_early_stop_without_fixture_evidence_is_refused(self):
        class StopEarly(unittest.TestCase):
            def run(self, result=None):
                value = super().run(result)
                result.stop()
                return value

            def runTest(self):
                pass

        result, path, _runner = self.record_suite([
            StopEarly(), unittest.FunctionTestCase(lambda: None),
        ])
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(result.testsRun, 1)
        self.assertIsNone(path)

    def test_preexisting_profiler_is_preserved_and_recording_refused(self):  # noqa: VACUOUS_ASSERTION — exact profiler identity and successful suite execution are positive controls; artifact absence proves refusal
        def profiler(_frame, _event, _arg):
            pass

        sys.setprofile(profiler)
        try:
            result, path, _runner = self.record_suite([
                unittest.FunctionTestCase(lambda: None),
            ])
            self.assertIs(sys.getprofile(), profiler)
        finally:
            sys.setprofile(None)
        self.assertTrue(result.wasSuccessful())
        self.assertIsNone(path)

    def test_unknown_callback_operation_fails_closed(self):
        self.assertEqual(gatetestrecord._CALLBACKS["addSuccess"],
                         ("event", "ok"))
        ledger = gatetestrecord._Ledger([])
        gatetestrecord._commit_callback(
            ledger, ("future-callback", None, None, None, None))
        self.assertFalse(ledger.valid)

    def test_uninstrumentable_result_runs_unchanged_without_artifact(self):  # noqa: VACUOUS_ASSERTION — result success is the positive control; artifact absence proves fail-closed measurement
        class Passing(unittest.TestCase):
            def runTest(self):
                pass

        suite = unittest.TestSuite([Passing()])
        runner = unittest.TextTestRunner(
            stream=open(os.devnull, "w", encoding="utf-8"),
            resultclass=_SlottedResult,
        )
        self.addCleanup(runner.stream.close)
        with mock.patch.object(
                gatetestrecord, "_Observer", side_effect=TypeError("layout")):
            result, path = gatetestrecord.run_recorded(
                runner, suite, self.tmp.name, self.context)
        self.assertTrue(result.wasSuccessful())
        self.assertIsNone(path)
        self.assertEqual(os.listdir(self.tmp.name), [])

    def test_unreadable_identity_preserves_suite_without_artifact(self):  # noqa: VACUOUS_ASSERTION — result success is the positive control; artifact absence is the safety claim
        class Unreadable(unittest.TestCase):
            def id(self):
                raise RuntimeError("identity")

            def runTest(self):
                pass

        suite = unittest.TestSuite([Unreadable()])
        runner = unittest.TextTestRunner(
            stream=open(os.devnull, "w", encoding="utf-8"))
        self.addCleanup(runner.stream.close)
        result, path = gatetestrecord.run_recorded(
            runner, suite, self.tmp.name, self.context)
        self.assertTrue(result.wasSuccessful())
        self.assertIsNone(path)
        self.assertEqual(os.listdir(self.tmp.name), [])

    def test_ordinary_recorders_do_not_require_module_identity(self):
        class Moduleless(unittest.TestCase):
            def runTest(self):
                pass

        Moduleless.__module__ = ""
        recorded, path, _runner = self.record_suite([Moduleless()])
        self.assertTrue(recorded.wasSuccessful())
        self.assertIsNotNone(path,
                             "ordinary profile recording lost valid outcome evidence")
        with open(os.devnull, "w", encoding="utf-8") as stream:
            controlled, controlled_path = gatetestrecord.run_controlled(
                unittest.TestSuite([Moduleless()]), self.tmp.name,
                self.context, stream=stream)
        self.assertTrue(controlled.wasSuccessful())
        self.assertIsNotNone(
            controlled_path,
            "ordinary controlled recording lost valid outcome evidence")

    def test_atomic_publish_failure_preserves_suite_and_leaves_no_partial(self):  # noqa: VACUOUS_ASSERTION — result success proves the exercised runner; no partial file is the atomicity claim
        class Passing(unittest.TestCase):
            def runTest(self):
                pass

        suite = unittest.TestSuite([Passing()])
        runner = unittest.TextTestRunner(
            stream=open(os.devnull, "w", encoding="utf-8"))
        self.addCleanup(runner.stream.close)
        with mock.patch.object(gatetestrecord.os, "replace",
                               side_effect=OSError("disk")):
            result, path = gatetestrecord.run_recorded(
                runner, suite, self.tmp.name, self.context)
        self.assertTrue(result.wasSuccessful())
        self.assertIsNone(path)
        self.assertEqual(os.listdir(self.tmp.name), [])

    def test_writes_one_atomic_run_specific_artifact(self):  # noqa: VACUOUS_ASSERTION — exact one-file census and token/PID identity are unconditional positive controls
        class Passing(unittest.TestCase):
            def runTest(self):
                pass

        _result, row, _runner = self.run_suite([Passing()])
        names = sorted(os.listdir(self.tmp.name))
        self.assertEqual(len(names), 1)
        self.assertFalse(names[0].startswith("."))
        self.assertIn(self.context["token"], names[0])
        self.assertEqual(row["token"], self.context["token"])
        self.assertEqual(row["pid"], os.getpid())
        self.assertEqual(row["start"], gatetestrecord.process_start())

    def test_strict_reader_refuses_foreign_and_malformed_artifacts(self):  # noqa: VACUOUS_ASSERTION — the valid artifact round-trip is the unconditional positive control
        valid = {
            "v": 1,
            "token": self.context["token"],
            "role": "worker",
            "pid": os.getpid(),
            "start": gatetestrecord.process_start(),
            "root_pid": os.getpid(),
            "root_start": gatetestrecord.process_start(),
            "shard": 2,
            "modules": [__name__],
            "planned": ["tests.Probe.test_one"],
            "started": ["tests.Probe.test_one"],
            "stopped": ["tests.Probe.test_one"],
            "unexecuted": [],
            "events": [
                {"test": "tests.Probe.test_one", "kind": "start"},
                {"test": "tests.Probe.test_one", "kind": "ok"},
                {"test": "tests.Probe.test_one", "kind": "stop"},
            ],
            "counts": {
                "ran": 1, "skipped": 0, "failures": 0, "errors": 0,
                "expected_failures": 0, "unexpected_successes": 0,
                "ok": True,
            },
        }
        self.assertEqual(
            gatetestrecord.validate_artifact(valid, self.context["token"]),
            valid)
        with self.assertRaisesRegex(ValueError, "token"):
            gatetestrecord.validate_artifact(
                dict(valid, token="b" * 32), self.context["token"])
        malformed = dict(valid)
        malformed["events"] = [{"test": "tests.Probe.test_one",
                                 "kind": "UNKNOWN"}]
        with self.assertRaisesRegex(ValueError, "event"):
            gatetestrecord.validate_artifact(
                malformed, self.context["token"])
        impossible = [
            {"test": "tests.Probe.test_one", "kind": "subtest-ok"},
            {"test": "tests.Probe.test_one", "kind": "ok",
             "subtest": "tests.Probe.test_one (arm='forged')"},
        ]
        for event in impossible:
            with self.subTest(event=event):
                malformed = dict(valid, events=[event])
                with self.assertRaisesRegex(ValueError, "event"):
                    gatetestrecord.validate_artifact(
                        malformed, self.context["token"])
        no_terminal = dict(valid, events=[
            {"test": "tests.Probe.test_one", "kind": "start"},
            {"test": "tests.Probe.test_one", "kind": "stop"},
        ])
        with self.assertRaisesRegex(ValueError, "terminal"):
            gatetestrecord.validate_artifact(
                no_terminal, self.context["token"])
        orphan = dict(valid, events=[
            {"test": "tests.Probe.test_one", "kind": "ok"},
        ])
        with self.assertRaisesRegex(ValueError, "event|active"):
            gatetestrecord.validate_artifact(orphan, self.context["token"])


_SYNTH = "tests.test_census_synthetic"
_SYNTH_ONLY_SKIPS = "tests.test_census_synthetic_skips"


def _runs():
    class Runs(unittest.TestCase):
        def test_one(self):
            pass

    Runs.__module__, Runs.__qualname__ = _SYNTH, "Runs"
    return Runs


def _class_setup(raiser, module=_SYNTH, cleanup=None):
    """A class of two tests whose setUpClass calls `raiser` (and, first,
    registers `cleanup` as a class cleanup when one is given)."""
    class Skips(unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            if cleanup is not None:
                cls.addClassCleanup(cleanup)
            raiser()

        def test_a(self):
            raise AssertionError("a class-skipped test ran")

        def test_b(self):
            raise AssertionError("a class-skipped test ran")

    Skips.__module__, Skips.__qualname__ = module, "Skips"
    return Skips


def _skip():
    raise unittest.SkipTest("git filter-repo is not installed")


def _explode():
    raise RuntimeError("setup exploded")


class ClassSkipCensusTest(unittest.TestCase):
    """A class whose setUpClass raises SkipTest starts none of its tests and
    records ONE skip on a `setUpClass (mod.Class)` stand-in. The serial timing
    census counted those tests as never started, so every whole-suite timing
    on a host without git filter-repo read UNKNOWN. These arms run a REAL
    synthetic suite through the same controlled adapter the serial timing
    runner uses, so the events are unittest's own."""

    def census(self, *classes, extra_planned=()):
        loader = unittest.TestLoader()
        suite = unittest.TestSuite(
            loader.loadTestsFromTestCase(cls) for cls in classes)
        planned = gatetestrecord.planned_tests(suite) + list(extra_planned)
        ledger = gatetestrecord._Ledger([name for name, _m in planned],
                                        [module for _n, module in planned])
        stream = open(os.devnull, "w", encoding="utf-8")
        self.addCleanup(stream.close)
        runner = unittest.TextTestRunner(
            stream=stream,
            resultclass=gatetestrecord.controlled_result_class(ledger))
        result = runner.run(suite)
        self.assertTrue(ledger.valid, "MUST-HIT: the ledger recorded the run")
        return result, gatetestrecord.timing_summary(ledger)

    def event(self, result, timing):
        return gate._module_timing_event(
            {"id": "a" * 16, "ran": result.testsRun, "elapsed": 60.0}, timing)

    def test_a_class_skip_is_covered_and_the_class_is_named(self):
        result, timing = self.census(_runs(), _class_setup(_skip))
        self.assertEqual((result.testsRun, len(result.skipped)), (1, 1),
                         "MUST-HIT: one test ran and one class-level skip")
        self.assertEqual(timing["state"], "COMPLETE", timing["reason"])
        self.assertIsNone(timing["reason"])
        self.assertEqual((timing["planned_modules"], timing["measured_modules"],
                          timing["measured_tests"]), (1, 1, 1))
        self.assertEqual(
            timing["skipped_by_class"],
            "2 test(s) in 1 class(es) skipped by setup: %s.Skips "
            "(2: git filter-repo is not installed)" % _SYNTH)
        event = self.event(result, timing)
        self.assertEqual(event["state"], "COMPLETE")
        self.assertEqual(event["skipped_by_class"], timing["skipped_by_class"])
        self.assertIsNone(gate._timing_event_error(event))

    def test_a_setup_class_error_is_still_not_covered(self):
        result, timing = self.census(_runs(), _class_setup(_explode))
        self.assertEqual(len(result.errors), 1,
                         "MUST-HIT: the setUpClass error was recorded")
        self.assertEqual(timing["state"], "UNKNOWN")
        self.assertIn("2 planned test(s) never started (%s.Skips.test_a, "
                      "%s.Skips.test_b)" % (_SYNTH, _SYNTH), timing["reason"])
        self.assertNotIn("skipped_by_class", timing,
                         "an error is never reported as a skip")

    def test_an_error_after_the_skip_is_still_not_covered(self):
        """The class cleanup runs after the skip and raises. unittest reports
        a skip AND an error on the same setUpClass stand-in."""
        result, timing = self.census(
            _runs(), _class_setup(_skip, cleanup=_explode))
        self.assertEqual((len(result.skipped), len(result.errors)), (1, 1),
                         "MUST-HIT: the stand-in carries a skip and an error")
        self.assertEqual(timing["state"], "UNKNOWN")
        self.assertIn("2 planned test(s) never started", timing["reason"])
        self.assertNotIn("skipped_by_class", timing)

    def test_a_skip_covers_only_its_own_class(self):
        lost = ("%s.Lost.test_lost" % _SYNTH, _SYNTH)
        _result, timing = self.census(
            _runs(), _class_setup(_skip), extra_planned=[lost])
        self.assertEqual(timing["state"], "UNKNOWN")
        self.assertIn("1 planned test(s) never started (%s)" % lost[0],
                      timing["reason"])
        self.assertNotIn("Skips.test_a", timing["reason"])
        self.assertIn("%s.Skips (2:" % _SYNTH, timing["skipped_by_class"])

    def test_a_module_held_whole_by_a_class_skip_owes_no_timing(self):
        result, timing = self.census(
            _runs(), _class_setup(_skip, module=_SYNTH_ONLY_SKIPS))
        self.assertEqual(timing["state"], "COMPLETE", timing["reason"])
        self.assertEqual((timing["planned_modules"],
                          timing["measured_modules"]), (1, 1))
        self.assertIn("%s.Skips (2:" % _SYNTH_ONLY_SKIPS,
                      timing["skipped_by_class"])
        self.assertIsNone(gate._timing_event_error(self.event(result, timing)))

    def test_a_module_setup_skip_is_covered_and_named(self):
        import types
        module = types.ModuleType(_SYNTH_ONLY_SKIPS)
        module.setUpModule = _skip
        sys.modules[_SYNTH_ONLY_SKIPS] = module
        self.addCleanup(sys.modules.pop, _SYNTH_ONLY_SKIPS, None)
        Held = _class_setup(lambda: None, module=_SYNTH_ONLY_SKIPS)
        result, timing = self.census(_runs(), Held)
        self.assertEqual(len(result.skipped), 1,
                         "MUST-HIT: one module-level skip")
        self.assertEqual(timing["state"], "COMPLETE", timing["reason"])
        self.assertEqual(
            timing["skipped_by_class"],
            "2 test(s) in 1 class(es) skipped by setup: %s [module] "
            "(2: git filter-repo is not installed)" % _SYNTH_ONLY_SKIPS)

    def test_an_old_timing_row_keeps_its_id_and_the_new_field_is_hashed(self):
        result, timing = self.census(_runs(), _class_setup(_skip))
        event = self.event(result, timing)
        old = {key: value for key, value in event.items()
               if key != "skipped_by_class"}
        payload = {key: old.get(key) for key in (
            "v", "event", "receipt", "state", "reason", "planned_modules",
            "measured_modules", "measured_tests", "runner_elapsed",
            "module_wall", "process_cpu", "unattributed_wall", "top")}
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
        self.assertEqual(gate._timing_id(old), hashlib.sha256(
            raw.encode("utf-8", "surrogatepass")).hexdigest()[:len(old["id"])],
            "a row without the field hashes exactly as before it existed")
        self.assertNotEqual(gate._timing_id(old), event["id"])
        forged = dict(event, skipped_by_class="0 test(s) skipped: nothing")
        self.assertIn("content id does not match",
                      gate._timing_event_error(forged))
        self.assertIn("skipped-by-class detail is unreadable",
                      gate._timing_event_error(dict(event, skipped_by_class="")))

    def test_gate_show_prints_the_skip_on_the_timing_line(self):
        result, timing = self.census(_runs(), _class_setup(_skip))
        event = self.event(result, timing)
        out = io.StringIO()
        with mock.patch.object(gate, "_timing_for_receipt",
                               return_value=(event, None)), \
                contextlib.redirect_stdout(out):
            gate._show_module_timing(event["receipt"])
        text = out.getvalue()
        self.assertIn("timing     COMPLETE", text)
        self.assertIn("2 test(s) in 1 class(es) skipped by setup: %s.Skips"
                      % _SYNTH, text)

    def test_the_detail_names_what_fits_and_always_keeps_the_count(self):
        """Seven long class names do not fit one line. The line names the
        ones that fit and counts the rest; the count is never what is cut."""
        classes = ["tests.test_web_home_geometry.%sGeometryAtThePhoneWidthTest"
                   % name for name in ("Alpha", "Bravo", "Charlie", "Delta",
                                       "Echo", "Foxtrot", "Golf")]
        held = {cls + ".setUpClass": [cls + ".test_x"] for cls in classes}
        fixtures = {name: "no chrome/chromium available" for name in held}
        text = gatetestrecord._skip_detail(held, fixtures)
        self.assertLessEqual(len(text), gatetestrecord.TIMING_SKIP_CAP)
        self.assertTrue(text.startswith(
            "7 test(s) in 7 class(es) skipped by setup: "), text)
        named = sum(cls in text for cls in classes)
        self.assertGreater(named, 0, "MUST-HIT: some class is named")
        self.assertLess(named, 7, "MUST-HIT: this arm needs an overflow")
        self.assertTrue(text.endswith(" and %d more" % (7 - named)), text)
        self.assertNotIn("…", text)


class _ComparatorFixture(unittest.TestCase):
    """Fixture + helpers only, NO test methods.

    Split out so the binder arms can reuse the three-hop fixture WITHOUT
    subclassing ComparatorTest, which re-runs all fourteen of its arms a
    second time under the subclass name. Measured: 25 tests where 11 were
    written.
    """
    def setUp(self):
        self.token = "c" * 32
        self.test_id = "tests.test_probe.Probe.test_probe"
        self.serial_row = self.record("serial", os.getpid(), None, [])
        worker_pid = os.getpid() + 1
        self.worker_row = self.record(
            "worker", worker_pid, 1, ["tests.test_probe"])
        self.root_row = {
            "v": 1,
            "type": "sharded-root",
            "token": self.token,
            "pid": os.getpid(),
            "start": gatetestrecord.process_start(),
            "owner_pid": os.getppid(),
            "owner_start": gatetestrecord.process_start(os.getppid()),
            "bins": [["tests.test_probe"]],
            "assignments": [{
                "index": 1,
                "modules": ["tests.test_probe"],
                "pid": worker_pid,
                "start": self.worker_row["start"],
            }],
            "samples": [
                _sample("launch", [worker_pid]),
                _sample("steady", [worker_pid]),
                _sample("peak", [worker_pid]),
            ],
            "counts": dict(self.worker_row["counts"]),
            "rc": 0,
        }
        # A CANONICAL ROOT IS v2 AND ITS PLAN IS TRUE. The binder recomputes
        # plan_digest from the workers' own planned ids, so a fixture cannot
        # carry a placeholder here -- which is the point: a syntactically
        # valid but FALSE plan used to authorize.
        self.root_row["v"] = 2
        self.root_row["plan"] = {
            "digest": gateshard.plan_digest(
                {name: len(self.worker_row["planned"])
                 for name in self.worker_row["modules"]},
                {name: list(self.worker_row["planned"])
                 for name in self.worker_row["modules"]}),
            "modules": sorted(self.worker_row["modules"]),
            "planned": len(self.worker_row["planned"]),
        }
        # THE CANONICAL TOPOLOGY IS THREE HOPS. The pool launches a
        # SUPERVISOR, which owns the inner test process; so the root's
        # assignment names the supervisor, the supervisor names the inner,
        # and the worker names the supervisor as its root. These fixtures
        # modelled the old two-hop shape and had to move with the runner --
        # updating them is cheaper than making authority evidence optional.
        self.supervisor_row = {
            "v": 1,
            "type": "worker-supervisor",
            "token": self.token,
            "shard": self.worker_row["shard"],
            "modules": list(self.worker_row["modules"]),
            "pid": worker_pid,
            "start": self.worker_row["start"],
            "root_pid": self.root_row["pid"],
            "root_start": self.root_row["start"],
            "inner_pid": worker_pid + 1,
            "inner_start": self.worker_row["start"],
            "inner_artifact_digest": None,   # filled in by arm(), from bytes
            "inner_rc": 0,
            "sweep_ok": True,
        }
        self.worker_row["pid"] = self.supervisor_row["inner_pid"]
        self.worker_row["root_pid"] = self.supervisor_row["pid"]
        self.worker_row["root_start"] = self.supervisor_row["start"]
        self.serial = self.arm("serial", [self.serial_row])
        self.sharded = self.arm(
            "sharded",
            [self.root_row, self.supervisor_row, self.worker_row])

    def record(self, role, pid, shard, modules):
        start = gatetestrecord.process_start() or 1
        return {
            "v": 1,
            "token": self.token,
            "role": role,
            "pid": pid,
            "start": start,
            "root_pid": os.getpid(),
            "root_start": start,
            "shard": shard,
            "modules": modules,
            "planned": [self.test_id],
            "started": [self.test_id],
            "stopped": [self.test_id],
            "unexecuted": [],
            "events": [
                {"test": self.test_id, "kind": "start"},
                {"test": self.test_id, "kind": "ok"},
                {"test": self.test_id, "kind": "stop"},
            ],
            "counts": {
                "ran": 1, "skipped": 0, "failures": 0, "errors": 0,
                "expected_failures": 0, "unexpected_successes": 0,
                "ok": True,
            },
        }

    def _basename(self, row):
        """THE PRODUCER'S grammar, never a fixture's idea of it.

        This method used to invent "<token>-sharded-0-<pid>" for roots while
        the producer writes "<token>-sharded-root-<pid>" -- wrong in both
        halves, agreeing with itself, and green across every arm built on it.
        A test that mints its own filenames is testing its own opinion.
        """
        return gatetestrecord.artifact_basename(row)

    def arm(self, kind, artifacts):
        """Build an arm whose byte evidence stays true to its rows."""
        return _LiveArm(self, kind, artifacts)

    def _remint_plan(self, artifacts):
        """Recompute the root plan from the CURRENT worker rows.

        Same lifecycle hazard as the digests, one level up: arms edit a
        worker's planned ids to move a failfast boundary, and a plan computed
        at setUp then describes a run that no longer exists. Production
        computes the plan once, from the census that produced the run.
        """
        workers = [row for row in artifacts if row.get("role") == "worker"]
        roots = [row for row in artifacts
                 if row.get("type") == "sharded-root"]
        if not workers or not roots or roots[0].get("v") != 2:
            return
        counts, ids = {}, {}
        for row in workers:
            for name in row["modules"]:
                counts[name] = len(row["planned"])
                ids[name] = list(row["planned"])
        roots[0]["plan"] = {
            "digest": gateshard.plan_digest(counts, ids),
            "modules": sorted(counts),
            "planned": sum(len(row["planned"]) for row in workers),
        }

    def _mint_files(self, artifacts):
        self._remint_plan(artifacts)
        # BYTE EVIDENCE, MINTED THE WAY COLLECTION MINTS IT. The binder
        # compares a supervisor's claimed SHA-256 against the digest preserved
        # at collection, so a fixture that omits it cannot exercise the chain
        # at all -- and one that invents an unrelated digest would prove the
        # binder accepts anything.
        files = {}
        for row in artifacts:
            raw = json.dumps(row, sort_keys=True).encode("utf-8")
            name = self._basename(row)
            files[name] = {"basename": name,
                           "raw_sha256": hashlib.sha256(raw).hexdigest(),
                           "row": row}
        for row in artifacts:
            if row.get("type") != "worker-supervisor":
                continue
            inner = gatetestrecord.artifact_basename({
                "role": "worker", "token": self.token,
                "shard": row.get("shard") or 0, "pid": row["inner_pid"]})
            if inner in files:
                # inner_rc FOLLOWS THE WORKER'S OUTCOME, because in
                # production it IS the worker's exit code. Arms here flip a
                # worker to failed to test divergence; a supervisor still
                # claiming rc=0 beside it is an INCOHERENT FIXTURE, and the
                # binder rightly calls that unknown. Deriving it keeps the
                # fixture honest instead of teaching the binder to ignore a
                # real disagreement.
                row["inner_rc"] = 0 if files[inner]["row"]["counts"]["ok"] \
                    else 1
                row["inner_artifact_digest"] = files[inner]["raw_sha256"]
                # the supervisor row's own bytes changed, so re-mint its
                # envelope rather than leaving a stale digest behind
                name = self._basename(row)
                raw = json.dumps(row, sort_keys=True).encode("utf-8")
                files[name] = {"basename": name,
                               "raw_sha256": hashlib.sha256(raw).hexdigest(),
                               "row": row}
        return files

    def _arm_body(self, kind, artifacts):
        return {
            "kind": kind,
            "token": self.token,
            "owner": ({
                "pid": self.root_row["owner_pid"],
                "start": self.root_row["owner_start"],
            } if kind == "sharded" else {
                "pid": os.getpid(),
                "start": gatetestrecord.process_start(),
            }),
            "reason": None,
            "artifacts": artifacts,
            "receipt": {"ran": 1, "skipped": 0, "status": "OK"},
        }


class ComparatorTest(_ComparatorFixture):
    def test_exact_worker_outcomes_are_equivalent(self):
        self.assertEqual(gateequiv.compare(
            self.serial, self.sharded)["equivalence_state"], "equivalent")

    def test_missing_serial_reference_is_unknown_not_qualification(self):
        missing = dict(self.serial, reason="serial receipt unavailable")
        with mock.patch.dict(os.environ, {"FAB_ID": "focused-control"}), \
                mock.patch.object(
                    gateequiv.gate, "tree_state",
                    return_value=("a" * 40, "b" * 40, False, None)), \
                mock.patch.object(gateequiv, "_run_arm", return_value=missing):
            result = gateequiv.run(repo=".", repeats=1, timing=False)
        self.assertEqual(result["equivalence_state"], "unknown")
        self.assertIn("authoritative serial reference unavailable",
                      result["reason"])
        self.assertEqual(result["authority"], "serial-only")
        self.assertEqual(result["sharded"], [])

    def test_compensating_worker_outcome_regression_is_divergent(self):
        self.worker_row["events"][1]["kind"] = "failure"
        self.worker_row["counts"].update(failures=1, ok=False)
        self.root_row["counts"].update(failures=1, ok=False)
        self.root_row["rc"] = 1
        self.sharded["receipt"]["status"] = "FAILED"
        self.assertEqual(gateequiv.compare(
            self.serial, self.sharded)["equivalence_state"], "divergent")

    def _make_failfast(self, row):
        later = "tests.test_probe.Probe.test_later"
        row["planned"].append(later)
        row["unexecuted"].append(later)
        row["events"][1]["kind"] = "failure"
        row["counts"].update(failures=1, ok=False)
        return later

    def _matching_failfast(self):
        self._make_failfast(self.serial_row)
        self._make_failfast(self.worker_row)
        self.root_row["counts"].update(failures=1, ok=False)
        self.root_row["rc"] = 1
        self.serial["receipt"]["status"] = "FAILED"
        self.sharded["receipt"]["status"] = "FAILED"

    def test_matching_failfast_evidence_is_equivalent(self):
        self._matching_failfast()
        self.assertEqual(gateequiv.compare(
            self.serial, self.sharded)["equivalence_state"], "equivalent")

    def test_matching_failed_gates_are_equivalent_and_failed(self):
        self._matching_failfast()
        controls = _timing_arms([1.0], [0.5])
        for arm in controls:
            arm["receipt"]["status"] = "FAILED"
        with mock.patch.dict(os.environ, {"FAB_ID": "focused-control"}), \
                mock.patch.object(
                    gateequiv.gate, "tree_state",
                    side_effect=[("a" * 40, "b" * 40, False, None)] * 2), \
                mock.patch.object(
                    gateequiv, "_run_arm",
                    side_effect=[self.serial, self.sharded]), \
                mock.patch.object(
                    gateequiv, "_run_timing_arm", side_effect=controls):
            result = gateequiv.run(repo=".", repeats=1, timing=False)
        self.assertEqual(result["equivalence_state"], "equivalent")
        self.assertEqual(result["gate_status"], "FAILED")
        self.assertIsNone(result["reason"])
        self.assertEqual(result["authority"], "serial-only")
        self.assertIn("never qualifies", result["observation"])

    def test_different_failfast_boundary_is_divergent(self):
        later = self._make_failfast(self.serial_row)
        self._make_failfast(self.worker_row)
        self.worker_row["started"].append(later)
        self.worker_row["stopped"].append(later)
        self.worker_row["unexecuted"].clear()
        self.worker_row["events"].extend([
            {"test": later, "kind": "start"},
            {"test": later, "kind": "ok"},
            {"test": later, "kind": "stop"},
        ])
        self.worker_row["counts"].update(ran=2, failures=1, ok=False)
        self.root_row["counts"].update(ran=2, failures=1, ok=False)
        self.root_row["rc"] = 1
        self.serial["receipt"]["status"] = "FAILED"
        self.sharded["receipt"].update(ran=2, status="FAILED")
        self.assertEqual(gateequiv.compare(
            self.serial, self.sharded)["equivalence_state"], "divergent")

    def test_foreign_row_pid_mismatch_and_timeout_are_unknown(self):
        self.assertEqual(gateequiv.compare(
            self.serial, self.sharded)["equivalence_state"], "equivalent")
        foreign = dict(self.worker_row, pid=self.worker_row["pid"] + 10)
        bad_samples = [dict(row) for row in self.root_row["samples"]]
        bad_samples[0]["workers"] = [999999]
        bad_root = dict(self.root_row, samples=bad_samples)
        cases = [
            self.arm("sharded", [self.root_row, self.worker_row, foreign]),
            self.arm("sharded", [
                self.root_row, dict(self.worker_row, pid=999999),
            ]),
            self.arm("sharded", [bad_root, self.worker_row]),
            self.arm("sharded", [self.root_row, {}]),
            dict(self.sharded, reason="runner never exited (timeout)"),
        ]
        for arm in cases:
            with self.subTest(arm=arm.get("reason") or len(arm["artifacts"])):
                self.assertEqual(gateequiv.compare(
                    self.serial, arm)["equivalence_state"], "unknown")

    def test_timing_comparison_requires_explicit_balanced_pairs(self):
        result = gateequiv.timing_comparison(
            _timing_arms([10.0, 10.0], [9.0, 11.0]))
        self.assertEqual(result["state"], "inconclusive")
        self.assertIn("change sign", result["reason"])
        overlap = gateequiv.timing_comparison(
            _timing_arms([10.0, 12.0], [9.0, 11.0]))
        self.assertEqual(overlap["state"], "inconclusive")
        self.assertIn("overlap", overlap["reason"])
        incomplete = _timing_arms([10.0], [9.0])[:-1]
        self.assertEqual(gateequiv.timing_comparison(
            incomplete)["state"], "unknown")
        reordered = list(reversed(_timing_arms([10.0], [9.0])))
        self.assertEqual(gateequiv.timing_comparison(
            reordered)["state"], "unknown")

    def test_timing_resource_imbalance_is_inconclusive(self):
        balanced = gateequiv.timing_comparison(
            _timing_arms([20.0], [1.0]))
        self.assertEqual(balanced["state"], "sharded-faster")
        cases = {
            "load": _timing_arms([20.0], [1.0], loads=[1.0, 7.0]),
            "competitor": _timing_arms(
                [20.0], [1.0], competitors=[{
                    "pid": 99, "start": 100, "argv": ["gate"],
                }]),
            "cores": _timing_arms([20.0], [1.0]),
        }
        cases["cores"][1]["before"]["affinity_cores"] = 4
        for name, arms in cases.items():
            with self.subTest(name=name):
                result = gateequiv.timing_comparison(arms)
                self.assertEqual(result["state"], "inconclusive")
                self.assertIn("resource", result["reason"])
                self.assertEqual(
                    result["incomparable_pairs"][0]["pair_id"], 1)

    def test_timing_order_alternates_explicit_pairs_at_requested_cost(self):
        trials = gateequiv._timing_trials(3)
        self.assertEqual([row["kind"] for row in trials], [
            "serial", "sharded", "sharded", "serial",
            "serial", "sharded",
        ])
        self.assertEqual([row["pair_id"] for row in trials], [1, 1, 2, 2, 3, 3])
        self.assertEqual([row["block_id"] for row in trials], [1, 1, 1, 1, 2, 2])
        self.assertEqual(len(trials), 6)

    def test_fatal_timing_arm_short_circuits_before_receipt_comparison(self):
        fatal = {
            "kind": "serial", "reason": "timing gate refused",
            "receipt": None, "receipt_wall": None,
        }
        with mock.patch.dict(os.environ, {"FAB_ID": "focused-control"}), \
                mock.patch.object(
                    gateequiv.gate, "tree_state",
                    return_value=("a" * 40, "b" * 40, False, None)), \
                mock.patch.object(
                    gateequiv, "_run_arm",
                    side_effect=[self.serial, self.sharded]), \
                mock.patch.object(
                    gateequiv, "_run_timing_arm", return_value=fatal):
            result = gateequiv.run(repo=".", repeats=1)
        self.assertEqual(result["equivalence_state"], "unknown")
        self.assertEqual(result["reason"], "timing gate refused")
        self.assertIsNone(result["timing"]["arms"][0]["receipt"])

    def test_every_timing_arm_matches_its_kind_baseline(self):
        controls = _timing_arms([3.0, 3.0], [1.0, 1.0])
        controls[2]["receipt"] = {"status": "OK", "ran": 2, "skipped": 0}
        with mock.patch.dict(os.environ, {"FAB_ID": "focused-control"}), \
                mock.patch.object(
                    gateequiv.gate, "tree_state",
                    return_value=("a" * 40, "b" * 40, False, None)), \
                mock.patch.object(
                    gateequiv, "_run_arm",
                    side_effect=[self.serial, self.sharded, self.sharded]), \
                mock.patch.object(
                    gateequiv, "_run_timing_arm", side_effect=controls):
            result = gateequiv.run(repo=".", repeats=2)
        self.assertEqual(result["equivalence_state"], "unknown")
        self.assertIn("evidence changed", result["reason"])

    def test_uninstrumented_timing_seam_clears_measurement_state(self):
        planted = {
            "HELM_GATE_RECORD_TOKEN": "timing-must-clear",
            "HELM_GATE_RECORD_ROLE": "serial",
        }
        with mock.patch.dict(os.environ, planted, clear=False):
            with gateequiv._without_measurement_env():
                for key in gatetestrecord.ENV_KEYS:
                    # A KEY VIEW, NEVER os.environ ITSELF (task/2370): the
                    # mapping form renders every ambient value on failure.
                    self.assertNotIn(key, tuple(os.environ))
            self.assertEqual(os.environ["HELM_GATE_RECORD_TOKEN"],
                             "timing-must-clear")
        self.assertIn("excluded from fleet-tax timing",
                      gateequiv.TIMING_DISCLOSURE)
        self.assertIn("receipt wall is the comparison",
                      gateequiv.TIMING_DISCLOSURE)

    def test_receipt_tree_movement_is_unknown(self):
        row = {
            "head": "a" * 40, "head_after": "b" * 40,
            "tree": "c" * 40, "tree_after": "c" * 40,
            "dirty": False, "dirty_after": False,
            "suite": True, "status": "OK", "argv": ["python"],
        }
        self.assertIn("moved", gateequiv._receipt_reason(
            row, "a" * 40, "c" * 40, ["python"]))


class SerialArmTest(unittest.TestCase):
    def _row(self, suite, argv):
        return {
            "head": "a" * 40, "head_after": "a" * 40,
            "tree": "b" * 40, "tree_after": "b" * 40,
            "dirty": False, "dirty_after": False, "suite": suite,
            "argv": argv, "status": "OK", "host": {"node": "fab"},
            "wall": 1.0,
        }

    def test_serial_arm_uses_default_gate_without_mutating_suite(self):
        argv = [sys.executable] + list(gateequiv.SERIAL_SUITE)
        original = gate.SUITE
        with mock.patch.object(gateequiv, "sample", return_value=_sample()), \
                mock.patch.object(gate, "run", return_value=(
                    self._row(True, argv), None)) as run:
            arm = gateequiv._run_arm(
                ".", gateequiv.SERIAL_SUITE, "serial", "a" * 40,
                "b" * 40, 30)
        run.assert_called_once_with(repo=".", timeout=30)
        self.assertIsNone(arm["reason"])
        self.assertEqual(gate.SUITE, original)

    def test_sharded_arm_is_custom_and_nonbinding(self):
        argv = [sys.executable] + list(gateequiv.SHARDED_SUITE)
        with mock.patch.object(gateequiv, "sample", return_value=_sample()), \
                mock.patch.object(gate, "run", return_value=(
                    self._row(False, argv), None)) as run:
            arm = gateequiv._run_arm(
                ".", gateequiv.SHARDED_SUITE, "sharded", "a" * 40,
                "b" * 40, 30)
        self.assertIsNone(arm["reason"])
        self.assertEqual(run.call_args.kwargs["argv"], argv)
        self.assertIn("DIAGNOSTIC", run.call_args.kwargs["label"])
        self.assertFalse(arm["receipt"]["suite"])
        self.assertFalse(gate._suite_shaped(argv))

    def test_gate_cli_delegates_to_diagnostic_equivalence_main(self):
        argv = ["--repo", "/tmp/probe", "--repeats", "2", "--no-timing"]
        with mock.patch.object(gateequiv, "main", return_value=7) as main:
            self.assertEqual(gate.cmd_gate(["equiv"] + argv), 7)
        main.assert_called_once_with(argv)
        self.assertIn("helm gate equiv", gate.USAGE)

    def test_literal_cli_records_only_the_root_after_discovery(self):  # noqa: VACUOUS_ASSERTION — subprocess rc plus exact one-artifact census prove the discovery path ran
        with tempfile.TemporaryDirectory() as repo, \
                tempfile.TemporaryDirectory() as records:
            os.makedirs(os.path.join(repo, "tests"))
            with open(os.path.join(repo, "tests", "__init__.py"),
                      "w", encoding="utf-8") as fh:
                fh.write("from helm import gatetestrecord\n"
                         "gatetestrecord.arm_serial_from_env()\n")
            with open(os.path.join(repo, "tests", "test_probe.py"),
                      "w", encoding="utf-8") as fh:
                fh.write(textwrap.dedent("""
                    import os
                    import unittest

                    _discovery_runner = unittest.TextTestRunner(
                        stream=open(os.devnull, "w", encoding="utf-8"))
                    _discovery_result = _discovery_runner.run(
                        unittest.FunctionTestCase(lambda: None))
                    _discovery_runner.stream.close()
                    if not _discovery_result.wasSuccessful():
                        raise RuntimeError("discovery-time nested runner failed")
                    _program_stream = open(os.devnull, "w", encoding="utf-8")
                    _discovery_program = unittest.TestProgram(
                        module=None, argv=["nested"], exit=False,
                        testRunner=unittest.TextTestRunner(stream=_program_stream))
                    _program_stream.close()

                    class Probe(unittest.TestCase):
                        def test_root_only(self):
                            for key in (
                                    "HELM_GATE_RECORD_DIR",
                                    "HELM_GATE_RECORD_TOKEN",
                                    "HELM_GATE_RECORD_ROLE"):
                                # key view, never the mapping — this CHILD
                                # runs on the build node too (task/2370)
                                self.assertNotIn(key, tuple(os.environ))
                            nested = unittest.TextTestRunner(
                                stream=open(os.devnull, "w", encoding="utf-8"))
                            self.addCleanup(nested.stream.close)
                            result = nested.run(unittest.FunctionTestCase(lambda: None))
                            self.assertTrue(result.wasSuccessful())
                """))
            env = dict(os.environ)
            env.update({
                "PYTHONPATH": os.path.dirname(os.path.dirname(__file__)),
                "HELM_GATE_RECORD_DIR": records,
                "HELM_GATE_RECORD_TOKEN": "literal-cli-token-000000000000",
                "HELM_GATE_RECORD_ROLE": "serial",
                "HELM_GATE_RECORD_ROOT_PID": str(os.getpid()),
                "HELM_GATE_RECORD_ROOT_START": str(
                    gatetestrecord.process_start()),
            })
            run = subprocess.run([
                sys.executable, "-m", "unittest", "discover",
                "-s", "tests", "-t", ".",
            ], cwd=repo, env=env, capture_output=True, text=True, timeout=30)
            names = os.listdir(records)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(len(names), 1)
            with open(os.path.join(records, names[0]), encoding="utf-8") as fh:
                row = json.load(fh)
        gatetestrecord.validate_artifact(
            row, "literal-cli-token-000000000000")
        self.assertEqual(row["planned"], [
            "tests.test_probe.Probe.test_root_only",
        ])
        self.assertEqual([event["kind"] for event in row["events"]],
                         ["start", "ok", "stop"])

    def test_absent_opt_in_state_does_not_patch_unittest(self):  # noqa: VACUOUS_ASSERTION — false arm result plus exact method identity are unconditional controls
        original = unittest.TextTestRunner._makeResult
        with mock.patch.dict(os.environ, {}, clear=False):
            for key in gatetestrecord.ENV_KEYS:
                os.environ.pop(key, None)
            self.assertFalse(gatetestrecord.arm_serial_from_env())
        self.assertIs(unittest.TextTestRunner._makeResult, original)


class ThreeHopBinderTest(_ComparatorFixture):
    """One arm per ARROW: break a link, the chain must read UNKNOWN.

    The binder's whole value is that no hop is unwitnessed, so every arrow
    needs its own arm. A single "the chain is checked" test would pass with
    five of six arrows deleted.
    """

    def _unknown(self, why):
        state = gateequiv.compare(self.serial, self.sharded)["equivalence_state"]
        self.assertEqual("unknown", state, why)

    def test_the_intact_chain_is_equivalent(self):
        """THE POSITIVE CONTROL. Without it every arm below passes for a
        binder that returns unknown unconditionally."""
        self.assertEqual(
            "equivalent",
            gateequiv.compare(self.serial, self.sharded)["equivalence_state"])

    def test_a_supervisor_naming_another_root_is_unknown(self):  # noqa: VACUOUS_ASSERTION — test_the_intact_chain_is_equivalent in this class is the unconditional positive control on the same observable: it proves the binder returns 'equivalent' for an intact chain, so a binder answering 'unknown' unconditionally fails there before any of these could pass for it
        self.supervisor_row["root_pid"] += 1
        self._unknown("supervisor -> root arrow is unchecked")

    def test_a_supervisor_naming_another_root_generation_is_unknown(self):  # noqa: VACUOUS_ASSERTION — test_the_intact_chain_is_equivalent in this class is the unconditional positive control on the same observable: it proves the binder returns 'equivalent' for an intact chain, so a binder answering 'unknown' unconditionally fails there before any of these could pass for it
        self.supervisor_row["root_start"] += 1
        self._unknown("supervisor -> root generation is unchecked")

    def test_a_worker_naming_another_supervisor_is_unknown(self):  # noqa: VACUOUS_ASSERTION — test_the_intact_chain_is_equivalent in this class is the unconditional positive control on the same observable: it proves the binder returns 'equivalent' for an intact chain, so a binder answering 'unknown' unconditionally fails there before any of these could pass for it
        self.worker_row["root_pid"] += 1
        self._unknown("worker -> supervisor arrow is unchecked")

    def test_a_supervisor_naming_another_inner_is_unknown(self):  # noqa: VACUOUS_ASSERTION — test_the_intact_chain_is_equivalent in this class is the unconditional positive control on the same observable: it proves the binder returns 'equivalent' for an intact chain, so a binder answering 'unknown' unconditionally fails there before any of these could pass for it
        self.supervisor_row["inner_pid"] += 100
        self._unknown("supervisor -> inner arrow is unchecked")

    def test_an_assignment_naming_another_process_is_unknown(self):  # noqa: VACUOUS_ASSERTION — test_the_intact_chain_is_equivalent in this class is the unconditional positive control on the same observable: it proves the binder returns 'equivalent' for an intact chain, so a binder answering 'unknown' unconditionally fails there before any of these could pass for it
        self.root_row["assignments"][0]["pid"] += 1
        self._unknown("root -> supervisor arrow is unchecked")

    def test_a_module_disagreement_across_the_chain_is_unknown(self):  # noqa: VACUOUS_ASSERTION — test_the_intact_chain_is_equivalent in this class is the unconditional positive control on the same observable: it proves the binder returns 'equivalent' for an intact chain, so a binder answering 'unknown' unconditionally fails there before any of these could pass for it
        self.supervisor_row["modules"] = ["tests.test_somewhere_else"]
        self._unknown("module agreement across hops is unchecked")

    def test_a_shard_disagreement_across_the_chain_is_unknown(self):  # noqa: VACUOUS_ASSERTION — test_the_intact_chain_is_equivalent in this class is the unconditional positive control on the same observable: it proves the binder returns 'equivalent' for an intact chain, so a binder answering 'unknown' unconditionally fails there before any of these could pass for it
        self.supervisor_row["shard"] = self.worker_row["shard"] + 5
        self._unknown("shard agreement across hops is unchecked")

    def test_a_containment_failure_is_unknown(self):  # noqa: VACUOUS_ASSERTION — test_the_intact_chain_is_equivalent in this class is the unconditional positive control on the same observable: it proves the binder returns 'equivalent' for an intact chain, so a binder answering 'unknown' unconditionally fails there before any of these could pass for it
        self.supervisor_row["sweep_ok"] = False
        self._unknown("a supervisor that failed to contain must not certify")

    def test_a_missing_supervisor_row_is_unknown(self):  # noqa: VACUOUS_ASSERTION — test_the_intact_chain_is_equivalent in this class is the unconditional positive control on the same observable: it proves the binder returns 'equivalent' for an intact chain, so a binder answering 'unknown' unconditionally fails there before any of these could pass for it
        self.sharded = self.arm("sharded", [self.root_row, self.worker_row])
        self._unknown("an unwitnessed hop must not certify")

    def test_a_second_supervisor_for_one_shard_is_unknown(self):  # noqa: VACUOUS_ASSERTION — test_the_intact_chain_is_equivalent in this class is the unconditional positive control on the same observable: it proves the binder returns 'equivalent' for an intact chain, so a binder answering 'unknown' unconditionally fails there before any of these could pass for it
        """A BYTE-IDENTICAL duplicate cannot exist and is not the case.

        Filenames are derived from the row, so two identical supervisor
        rows name ONE file and the envelope collection holds one of them --
        the duplicate is impossible by construction, which is the property
        rather than a gap. The reachable case is a SECOND supervisor
        claiming the same shard with a different identity: its own
        filename, its own envelope, and a census that must refuse it.
        """
        second = dict(self.supervisor_row,
                      pid=self.supervisor_row["pid"] + 50)
        self.sharded = self.arm(
            "sharded", [self.root_row, self.supervisor_row, second,
                        self.worker_row])
        self._unknown("a second supervisor for one shard must not certify")


class ThreeHopBinderFieldTest(_ComparatorFixture):
    """One mutation per arrow, for every arrow rather than the pids alone.

    Covering only the PIDs leaves every `start`, the digest, the derived
    filename, the packed bin and each plan field unmutated -- so a
    one-per-arrow claim is true of the arrows that came to mind and false of
    the chain.
    """

    def _frozen(self):
        """A snapshot arm that does NOT re-mint its evidence.

        _LiveArm recomputes digests and the plan on every read, which is
        right for arms that edit a worker row -- and FATAL for arms that
        falsify the evidence itself: the fixture healed the mutation before
        the binder saw it, and four "unknown" arms passed as "equivalent"
        while proving nothing. Freeze first, then falsify.
        """
        return dict(self.sharded, artifact_files=self.sharded["artifact_files"])

    def _unknown(self, why, arm=None):
        state = gateequiv.compare(
            self.serial, arm or self.sharded)["equivalence_state"]
        self.assertEqual("unknown", state, why)

    def test_the_intact_chain_is_equivalent(self):
        """The control every arm below depends on."""
        self.assertEqual(
            "equivalent",
            gateequiv.compare(self.serial, self.sharded)["equivalence_state"])

    def test_an_assignment_naming_another_generation_is_unknown(self):  # noqa: VACUOUS_ASSERTION — test_the_intact_chain_is_equivalent in this class is the unconditional positive control on the same observable
        self.root_row["assignments"][0]["start"] += 1
        self._unknown("assignment start is unchecked")

    def test_a_supervisor_naming_another_inner_generation_is_unknown(self):  # noqa: VACUOUS_ASSERTION — see the control in this class
        self.supervisor_row["inner_start"] += 1
        self._unknown("supervisor inner_start is unchecked")

    def test_a_worker_naming_another_supervisor_generation_is_unknown(self):  # noqa: VACUOUS_ASSERTION — see the control in this class
        self.worker_row["root_start"] += 1
        self._unknown("worker root_start is unchecked")

    def test_a_false_digest_claim_is_unknown(self):  # noqa: VACUOUS_ASSERTION — see the control in this class
        arm = self._frozen()
        self.supervisor_row["inner_artifact_digest"] = "0" * 64
        self._unknown("the digest claim is unchecked", arm)

    def test_a_packed_bin_is_unknown(self):  # noqa: VACUOUS_ASSERTION — see the control in this class
        """Agreement across hops is not isolation."""
        packed = ["tests.test_probe", "tests.test_hidden_state"]
        self.root_row["bins"] = [list(packed)]
        self.root_row["assignments"][0]["modules"] = list(packed)
        self.supervisor_row["modules"] = list(packed)
        self.worker_row["modules"] = list(packed)
        self._unknown("a multi-module process can hide producer/consumer "
                      "state")

    def test_a_false_plan_module_census_is_unknown(self):  # noqa: VACUOUS_ASSERTION — see the control in this class
        arm = self._frozen()
        self.root_row["plan"] = dict(self.root_row["plan"], modules=["tests.wrong"])
        self._unknown("plan modules are unbound", arm)

    def test_a_false_plan_count_is_unknown(self):  # noqa: VACUOUS_ASSERTION — see the control in this class
        arm = self._frozen()
        self.root_row["plan"] = dict(self.root_row["plan"], planned=999)
        self._unknown("plan count is unbound", arm)

    def test_a_false_plan_digest_is_unknown(self):  # noqa: VACUOUS_ASSERTION — see the control in this class
        arm = self._frozen()
        self.root_row["plan"] = dict(self.root_row["plan"], digest="0" * 64)
        self._unknown("plan digest is unbound", arm)

    def test_a_v1_root_cannot_carry_canonical_authority(self):  # noqa: VACUOUS_ASSERTION — see the control in this class
        self.root_row["v"] = 1
        del self.root_row["plan"]
        self._unknown("a root with no planning evidence must not authorize")


class EnvelopeIdentityTest(_ComparatorFixture):
    """Filename, bytes and row are ONE object, or the arm is unknown.

    These exist because the mutation for the binding survived: removing the
    filename-vs-row check left every arm green. A check nothing exercises is
    a check nobody will notice removing.
    """

    def _frozen_files(self):
        return dict(self.sharded["artifact_files"])

    def _unknown(self, files, why):
        arm = dict(self.sharded, artifact_files=files)
        self.assertEqual(
            "unknown",
            gateequiv.compare(self.serial, arm)["equivalence_state"], why)

    def test_the_intact_collection_is_equivalent(self):
        """The control: these refusals mean nothing without it."""
        self.assertEqual(
            "equivalent",
            gateequiv.compare(self.serial, self.sharded)["equivalence_state"])

    def test_an_envelope_key_that_is_not_the_rows_name_is_unknown(self):  # noqa: VACUOUS_ASSERTION — test_the_intact_collection_is_equivalent is this class's unconditional positive control on the same observable
        files = self._frozen_files()
        name = next(n for n in files if "-worker-" in n)
        envelope = files.pop(name)
        files["nonsense-name.json"] = dict(envelope,
                                           basename="nonsense-name.json")
        self._unknown(files, "an envelope may not be stored under a name its "
                             "row does not derive")

    def test_a_row_whose_shard_contradicts_its_filename_is_unknown(self):  # noqa: VACUOUS_ASSERTION — see the control in this class
        files = self._frozen_files()
        name = next(n for n in files if "-worker-" in n)
        envelope = files[name]
        files[name] = dict(envelope,
                           row=dict(envelope["row"], shard=99))
        self._unknown(files, "a row must name the file it is stored in")

    def test_a_row_that_cannot_name_its_own_file_is_unknown(self):  # noqa: VACUOUS_ASSERTION — see the control in this class
        files = self._frozen_files()
        name = next(n for n in files if "-worker-" in n)
        envelope = files[name]
        broken = dict(envelope["row"])
        del broken["pid"]
        files[name] = dict(envelope, row=broken)
        self._unknown(files, "a row missing its identity cannot be bound")

    def test_a_worker_and_supervisor_swapping_envelopes_is_unknown(self):  # noqa: VACUOUS_ASSERTION — see the control in this class
        """A CROSS-TYPE swap, which the typed census also catches.

        This arm was named for the two-worker permutation and does not
        perform it: this fixture has ONE worker, so the name described a case
        it could not express. The real two-worker swap -- the permutation the
        filename binding actually exists for -- is TwoWorkerEnvelopeTest.
        """
        files = self._frozen_files()
        worker = next(n for n in files if "-worker-" in n)
        chain = next(n for n in files if "-supervisor-" in n)
        files[worker], files[chain] = (
            dict(files[chain], basename=worker),
            dict(files[worker], basename=chain))
        self._unknown(files, "rows may not be crossed between filenames")


class TwoWorkerEnvelopeBase(_ComparatorFixture):
    """The two-worker fixture: setUp adds a second worker, with its own test
    id and its own supervisor, to the comparator fixture, and builds the
    serial run (`serial_two`) and the two-shard run (`two`) it is compared
    with; `_state` reads one arm's equivalence state against that serial
    run.

    THIS CLASS HOLDS NO `test_*` METHOD, and that is its contract. unittest
    collects every inherited `test_*` again under each subclass's own id, so
    an arm written here runs once per subclass. A class that needs this
    fixture subclasses THIS class; its arms go in the subclass."""

    def setUp(self):
        super(TwoWorkerEnvelopeBase, self).setUp()
        second_module = "tests.test_probe_two"
        inner_two = self.supervisor_row["inner_pid"] + 10
        sup_two_pid = self.supervisor_row["pid"] + 10

        self.worker_two = self.record("worker", inner_two, 2, [second_module])
        # ITS OWN TEST ID. Both shards reporting the same id makes the arms
        # DIVERGENT against a serial run that executed two distinct tests --
        # a fixture defect that would have read as a comparator finding.
        second_id = self.test_id + "2"
        self.worker_two["planned"] = [second_id]
        self.worker_two["started"] = [second_id]
        self.worker_two["stopped"] = [second_id]
        self.worker_two["events"] = [
            {"test": second_id, "kind": k} for k in ("start", "ok", "stop")]
        self.worker_two["root_pid"] = sup_two_pid
        self.worker_two["root_start"] = self.worker_two["start"]
        self.supervisor_two = dict(
            self.supervisor_row,
            shard=2,
            modules=[second_module],
            pid=sup_two_pid,
            start=self.worker_two["start"],
            inner_pid=inner_two,
            inner_start=self.worker_two["start"],
            inner_artifact_digest=None)

        root = dict(self.root_row)
        root["bins"] = [list(self.worker_row["modules"]), [second_module]]
        root["assignments"] = [
            dict(self.root_row["assignments"][0]),
            {"index": 2, "modules": [second_module],
             "pid": sup_two_pid, "start": self.worker_two["start"]},
        ]
        pids = [self.supervisor_row["pid"], sup_two_pid]
        root["samples"] = [_sample("launch", pids), _sample("steady", pids),
                           _sample("peak", pids)]
        counts = dict(self.worker_row["counts"])
        counts["ran"] = 2
        root["counts"] = counts
        self.root_two = root

        serial_row = self.record("serial", os.getpid(), None, [])
        serial_row["planned"] = [self.test_id, self.test_id + "2"]
        serial_row["started"] = list(serial_row["planned"])
        serial_row["stopped"] = list(serial_row["planned"])
        serial_row["counts"] = dict(serial_row["counts"], ran=2)
        serial_row["events"] = [
            {"test": t, "kind": k}
            for t in serial_row["planned"]
            for k in ("start", "ok", "stop")]
        self.serial_two = self.arm("serial", [serial_row])
        self.two = self.arm("sharded", [
            root, self.supervisor_row, self.worker_row,
            self.supervisor_two, self.worker_two])
        # _arm_body hardcodes a one-test receipt; this topology ran two.
        for arm in (self.serial_two, self.two):
            arm["receipt"] = {"ran": 2, "skipped": 0, "status": "OK"}

    def _state(self, arm):
        return gateequiv.compare(self.serial_two, arm)["equivalence_state"]


class TwoWorkerEnvelopeTest(TwoWorkerEnvelopeBase):
    """THE PERMUTATION THE FILENAME BINDING EXISTS FOR.

    Every other envelope arm crosses rows of DIFFERENT types, which the typed
    census refuses on its own -- so they cannot tell whether the filename
    binding does anything. Two workers swapping envelopes is the case where
    the census is satisfied (same type, same count) and only the
    filename-names-this-row check stands between a crossed pair and an
    equivalent verdict.

    A subclass of THIS class runs every arm below again under its own id,
    which is right only for one that changes the fixture those arms read. A
    class that only needs the fixture subclasses TwoWorkerEnvelopeBase."""

    def test_the_two_shard_collection_is_equivalent(self):  # noqa: VACUOUS_ASSERTION — this IS the class's unconditional positive control: it asserts the equivalent state, the structural opposite of the refusals it enables
        """THE CONTROL. Without it every refusal below could come from a
        fixture that never assembled rather than from the binding."""
        self.assertEqual("equivalent", self._state(self.two),
                         "the two-worker fixture does not assemble")

    def test_two_workers_swapping_envelopes_is_unknown(self):  # noqa: VACUOUS_ASSERTION — test_the_two_shard_collection_is_equivalent is this class's unconditional positive control on the same observable
        """SAME TYPE, SAME COUNT, CROSSED ROWS.

        The typed census cannot see this: both envelopes hold worker rows and
        the population is unchanged.

        THIS ARM SWAPS WHOLE ENVELOPES -- row AND raw digest together -- so
        the supervisor's digest comparison catches it downstream and the
        filename-derivation check is not what refuses it here. That is why
        deleting the derivation check leaves this arm green, and it is NOT
        evidence that the check is idle. The arm that isolates it swaps ONLY
        the rows: test_two_workers_swapping_ROWS_is_unknown, below.
        """
        files = dict(self.two["artifact_files"])
        names = sorted(n for n in files if "-worker-" in n)
        self.assertEqual(2, len(names),
                         "MUST-HIT: this arm needs two worker envelopes, "
                         "found %s" % names)
        one, two = names
        files[one], files[two] = (
            dict(files[two], basename=one),
            dict(files[one], basename=two))
        arm = dict(self.two, artifact_files=files)
        self.assertEqual("unknown", self._state(arm),
                         "two workers crossed their envelopes and the arm "
                         "was still called equivalent")


class AssignmentSchemaTest(_ComparatorFixture):
    """An assignment carries FOUR fields, and nothing else.

    Accepting unknown keys means a root can carry a field the binder never
    examines. Nothing refuses what nothing reads, so the arm still reads
    equivalent while the assignment claims an authority of its own.
    """

    def _state(self, root):
        arm = self.arm("sharded",
                       [root, self.supervisor_row, self.worker_row])
        arm["receipt"] = {"ran": 1, "skipped": 0, "status": "OK"}
        return gateequiv.compare(self.serial, arm)["equivalence_state"]

    def test_the_intact_assignment_is_equivalent(self):  # noqa: VACUOUS_ASSERTION — this IS the class's unconditional positive control: it asserts the equivalent state, the structural opposite of the refusals it enables
        """THE CONTROL: the refusals below mean nothing without it."""
        self.assertEqual("equivalent", self._state(dict(self.root_row)))

    def test_an_assignment_carrying_a_foreign_field_is_unknown(self):  # noqa: VACUOUS_ASSERTION — test_the_intact_assignment_is_equivalent is this class's unconditional positive control on the same observable
        root = dict(self.root_row)
        root["assignments"] = [dict(root["assignments"][0],
                                    foreign_authority="mine")]
        self.assertEqual("unknown", self._state(root),
                         "an assignment claiming its own authority was "
                         "accepted because nothing reads that field")

    def test_an_assignment_missing_a_required_field_is_unknown(self):  # noqa: VACUOUS_ASSERTION — see the control in this class
        root = dict(self.root_row)
        short = dict(root["assignments"][0])
        del short["start"]
        root["assignments"] = [short]
        self.assertEqual("unknown", self._state(root),
                         "an assignment with no start was accepted")


class RowsAndBytesAreOneCollectionTest(_ComparatorFixture):
    """The envelope rows must BE the artifact rows, by identity.

    Equality is not enough: the binder asserts `is` against these objects,
    so a row that merely compares equal passes every value check and then
    fails to be the row the chain named. Consolidating validation into one
    constructor is exactly where such a check goes missing.
    """

    def test_the_intact_collection_is_equivalent(self):
        """THE CONTROL."""
        self.assertEqual(
            "equivalent",
            gateequiv.compare(self.serial, self.sharded)["equivalence_state"])

    def test_an_equal_but_foreign_row_is_unknown(self):  # noqa: VACUOUS_ASSERTION — test_the_intact_collection_is_equivalent is this class's unconditional positive control on the same observable
        files = dict(self.sharded["artifact_files"])
        name = next(n for n in files if "-worker-" in n)
        twin = copy.deepcopy(files[name]["row"])
        self.assertEqual(twin, files[name]["row"],
                         "MUST-HIT: the twin must be EQUAL, or this arm is "
                         "testing inequality rather than identity")
        self.assertIsNot(twin, files[name]["row"])
        files[name] = dict(files[name], row=twin)
        arm = dict(self.sharded, artifact_files=files)
        self.assertEqual(
            "unknown",
            gateequiv.compare(self.serial, arm)["equivalence_state"],
            "a row that merely compares equal was accepted as the row the "
            "chain named")


class ConstructorIsTheWholeValidationTest(_ComparatorFixture):
    """C-E must refuse INSIDE the constructor, not in its caller.

    The contract is that a returned object is fully validated evidence. While
    throughput, the three-hop binder and the plan binding lived in the caller,
    "one constructor" was true of the SHAPE and false of the VALIDATION: a
    second caller could obtain an object that had passed A-B only and looked
    exactly like a checked one.

    These arms call the constructor DIRECTLY, so they cannot be satisfied by
    a refusal that happens further out.
    """

    def _frozen(self, artifacts):
        """An arm whose evidence is minted ONCE, then never re-derived.

        _LiveArm re-mints on every read, and _mint_files re-computes the root
        plan from the current workers first -- so an arm that corrupts a plan
        digest through the live path has its corruption HEALED before the
        constructor ever sees it. That is the fixture proving itself right.
        """
        files = self._mint_files(artifacts)
        body = dict(self._arm_body("sharded", artifacts))
        body["artifact_files"] = files
        body["receipt"] = {"ran": 1, "skipped": 0, "status": "OK"}
        return body

    def _refuses(self, arm, fragment):
        with self.assertRaises(ValueError) as caught:
            gateequiv._validated_sharded_evidence(arm)
        self.assertIn(fragment, str(caught.exception))

    def test_the_intact_arm_constructs(self):
        """THE CONTROL: every refusal below is meaningless without it."""
        evidence = gateequiv._validated_sharded_evidence(self.sharded)
        self.assertEqual(1, evidence["count"])

    def test_a_false_plan_digest_is_refused_by_the_constructor(self):  # noqa: VACUOUS_ASSERTION — test_the_intact_arm_constructs is this class's unconditional positive control: it proves the constructor RETURNS for good evidence, so these raises are about the defect and not a constructor that always refuses
        """STAGE E, reached through the constructor alone."""
        root = copy.deepcopy(self.root_row)
        arm = self._frozen([root, self.supervisor_row, self.worker_row])
        # AFTER minting, or _remint_plan repairs it. Nothing compares the
        # root's own bytes to its envelope digest, so this stays a plan
        # defect rather than becoming a byte-evidence one.
        root["plan"]["digest"] = "0" * 64
        self._refuses(arm, "does not bind the worker evidence")

    def test_a_foreign_throughput_sample_is_refused_by_the_constructor(self):  # noqa: VACUOUS_ASSERTION — see the control in this class
        """STAGE C, reached through the constructor alone."""
        root = copy.deepcopy(self.root_row)
        root["samples"] = [_sample("launch", [999999]),
                           _sample("steady", [999999]),
                           _sample("peak", [999999])]
        arm = self._frozen([root, self.supervisor_row, self.worker_row])
        self._refuses(arm, "worker throughput sample is missing or foreign")

    def test_a_broken_chain_hop_is_refused_by_the_constructor(self):  # noqa: VACUOUS_ASSERTION — see the control in this class
        """STAGE D, reached through the constructor alone."""
        chain = dict(self.supervisor_row, root_pid=424242)
        arm = self._frozen([self.root_row, chain, self.worker_row])
        self._refuses(arm, "supervisor owner is foreign")


class RowOnlySwapTest(TwoWorkerEnvelopeBase):
    """The permutation that ISOLATES the filename-derivation check.

    Swapping whole envelopes moves the raw digest with the row, so the
    supervisor's digest claim stops matching and the binder refuses
    downstream -- the derivation check never gets credit or blame. Swapping
    ONLY THE ROWS leaves every filename and every digest exactly where it
    was, so the bytes still agree with the supervisor and the ONLY thing
    wrong is which row sits under which name.

    That is the case the derivation check exists for, and until now nothing
    exercised it: deleting the check left all seven envelope arms green.
    """

    def test_two_workers_swapping_ROWS_is_unknown(self):  # noqa: VACUOUS_ASSERTION — test_the_two_shard_collection_is_equivalent, in TwoWorkerEnvelopeTest on the same TwoWorkerEnvelopeBase fixture, is the unconditional positive control on the same observable
        files = dict(self.two["artifact_files"])
        names = sorted(n for n in files if "-worker-" in n)
        self.assertEqual(2, len(names),
                         "MUST-HIT: this arm needs two worker envelopes")
        one, two = names
        # ROWS ONLY. basename and raw_sha256 stay bound to their own files.
        files[one] = dict(files[one], row=files[two]["row"])
        files[two] = dict(files[two], row=dict(self.two["artifact_files"])[one]["row"])
        for name in (one, two):
            self.assertEqual(name, files[name]["basename"],
                             "MUST-HIT: the basename must NOT have moved")
        arm = dict(self.two, artifact_files=files)
        self.assertEqual("unknown", self._state(arm),
                         "two worker rows were crossed under unchanged "
                         "filenames and the arm was still equivalent")
