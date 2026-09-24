#!/usr/bin/env python3
"""THE INSTRUMENT'S OWN SILENCES ARE THE PROPERTIES WORTH TESTING.

Two of this module's three claims are about NOT doing something -- a healthy
ladder writes nothing, and a missing log is not a quiet one -- so every arm
that asserts an absence carries a positive control on the same observable.
"""
import os
import shutil
import tempfile
import time
import unittest

from unittest import mock

from helm import doctor, procage, seats_stop_timing, stopprobe


class StopProbeWriteTest(unittest.TestCase):

    def setUp(self):
        fh = tempfile.NamedTemporaryFile(prefix="stopprobe-", suffix=".log",
                                         delete=False)
        fh.close()
        os.unlink(fh.name)          # a path that does not exist yet
        self.log = fh.name
        self.addCleanup(self._remove)

    def _remove(self):
        try:
            os.unlink(self.log)
        except OSError:
            pass

    def lines(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as fh:
            return [ln for ln in fh.read().splitlines() if ln.strip()]

    def test_a_healthy_ladder_writes_NOTHING(self):  # noqa: VACUOUS_ASSERTION — the observable is the FILE, read by a separate call from the writer's, so no same-call control can exist; the writer's own False and the one-line file two lines below are the must-differ pair
        """The median stop is a few seconds and is not news. Recording it
        would bury the tail and pay a write on turns that are working."""
        for rung, total in (("identity", 0.02), ("seam", 1.9), ("inbox", 2.9)):
            self.assertFalse(stopprobe.record("r", rung, 0.1, total, log=self.log, slow=3.0))
        self.assertEqual(self.lines(), [])
        # THE POSITIVE CONTROL, same writer, same file, one step over the
        # line: without it a writer that never writes at all passes above.
        self.assertTrue(stopprobe.record("r", "seam", 2.0, 3.1, log=self.log, slow=3.0))
        self.assertEqual(len(self.lines()), 1)

    def test_the_writer_REFUSES_what_its_own_reader_would_refuse(self):  # noqa: VACUOUS_ASSERTION — the observable is the FILE, read by a separate call from the writer's, so no same-call control can exist; the writer's own True and the RUNG that reads back four lines below are the must-differ pair
        """A blank identity wrote successfully and came back MALFORMED, so
        the writer reported a record it had already made unreadable — a
        success that is a lie about the file."""
        for blank in ("", "   "):
            self.assertFalse(stopprobe.record(blank, "seam", 1.0, 9.0,
                                              log=self.log, slow=3.0), blank)
            self.assertFalse(stopprobe.record_end(blank, "seam", 9.0,
                                                  log=self.log, slow=3.0))
        self.assertEqual(self.lines(), [])
        # POSITIVE CONTROL, same writer and file: a real identity DOES write,
        # so the refusals above are the check and not a dead writer.
        self.assertTrue(stopprobe.record("r", "seam", 1.0, 9.0,
                                         log=self.log, slow=3.0))
        rows, _err = stopprobe.read(log=self.log)
        self.assertEqual([r["event"] for r in rows], [stopprobe.RUNG])

    def test_a_duration_that_is_NOT_A_REAL_NUMBER_never_closes_a_run(self):
        """float() accepts nan, inf and negatives, so a bare parse is not a
        validation: `total=nan` would close a run with a value that compares
        false against every bound."""
        fh = tempfile.NamedTemporaryFile(mode="w", prefix="stopprobe-",
                                         suffix=".log", delete=False)
        for bad in ("nan", "inf", "-1.0"):
            fh.write("2026-09-10T12:00:01 STOP-END pid=2 | seat=s | run=a "
                     "| rung=seam | total=%s\n" % bad)
        fh.write("2026-09-10T12:00:02 STOP-RUNG pid=2 | seat=s | run=a "
                 "| rung=seam | elapsed=1.0 | total=6.0\n")
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        rows, err = stopprobe.read(log=fh.name)
        self.assertIsNone(err)
        self.assertEqual([r["event"] for r in rows],
                         [stopprobe.MALFORMED] * 3 + [stopprobe.RUNG])
        # THE RUN IS STILL OPEN: no END survived to close it.
        unfinished, _by = stopprobe.findings(rows)
        self.assertEqual(len(unfinished), 1)

    def test_an_identity_mint_that_cannot_read_ENTROPY_still_returns_one(self):
        """This runs inside a hook before the caller's own guard exists."""
        with mock.patch.object(stopprobe.os, "urandom",
                               side_effect=OSError("no entropy")):
            minted = stopprobe.new_run_id()
        self.assertTrue(minted and minted.strip())
        # POSITIVE CONTROL: the ordinary path also mints, and the two differ,
        # so the fallback is a fallback and not the only branch that works.
        self.assertNotEqual(minted, stopprobe.new_run_id())

    def test_the_record_carries_the_siblings_grammar(self):
        """Anything that reads hookprobe.log must read this file too."""
        stopprobe.record("r", "seam", 5.412, 6.942, log=self.log, slow=3.0)
        line = self.lines()[0]
        head, _, rest = line.partition(" | ")
        stamp, event, pid = head.split()
        self.assertEqual(len(stamp), 19, stamp)     # iso, seconds resolution
        self.assertEqual(event, stopprobe.RUNG)
        self.assertTrue(pid.startswith("pid="), pid)
        self.assertIn("run=", rest)
        self.assertIn("rung=seam", rest)
        self.assertIn("elapsed=5.412", rest)
        self.assertIn("total=6.942", rest)

    def test_a_writer_that_CANNOT_write_never_raises(self):
        """This runs inside a hook. An instrument that can take the guard
        down is worse than no instrument at all."""
        # A PATH UNDER A REGULAR FILE: makedirs cannot make a directory out
        # of a file, so this is unwritable in a way no mkdir can rescue.
        wall = tempfile.NamedTemporaryFile(prefix="stopprobe-wall-",
                                           delete=False)
        wall.write(b"not a directory")
        wall.close()
        self.addCleanup(os.unlink, wall.name)
        blocked = os.path.join(wall.name, "cannot", "exist.log")
        self.assertFalse(stopprobe.record("r", "seam", 1.0, 9.0, log=blocked, slow=3.0))
        # POSITIVE CONTROL: the same call on a writable path DOES write, so
        # the False above is the refusal and not a writer that never works.
        self.assertTrue(stopprobe.record("r", "seam", 1.0, 9.0, log=self.log, slow=3.0))

    def test_the_last_line_names_the_last_rung_seen_to_FINISH(self):
        """The interesting run never reaches its end: the outer timeout kills
        the guard mid-rung, and a killed process writes no summary. Per-
        boundary appends are what make the answer survive the kill."""
        run = "r1"
        stopprobe.record(run, "dispatch-ledger", 1.1, 3.4, log=self.log, slow=3.0)
        stopprobe.record(run, "claims", 1.3, 4.7, log=self.log, slow=3.0)
        stopprobe.record(run, "seam", 5.4, 10.1, log=self.log, slow=3.0)
        # ... and then nothing: no record_end, because the axe fell.
        rows, err = stopprobe.read(log=self.log)
        self.assertIsNone(err)
        self.assertEqual(len(rows), 3)
        truncated, by_rung = stopprobe.findings(rows)
        self.assertEqual(len(truncated), 1)
        self.assertEqual(truncated[0]["last"], "seam")
        self.assertEqual(by_rung, {"seam": 1})

    def test_a_run_that_REACHED_its_end_is_not_a_finding(self):
        """The must-differ control for the arm above: the same three
        boundaries plus an END, and the finding goes away."""
        run = "r1"
        stopprobe.record(run, "dispatch-ledger", 1.1, 3.4, log=self.log, slow=3.0)
        stopprobe.record(run, "seam", 5.4, 10.1, log=self.log, slow=3.0)
        stopprobe.record_end(run, "response", 10.2, log=self.log, slow=3.0)
        rows, err = stopprobe.read(log=self.log)
        self.assertIsNone(err)
        self.assertEqual(len(rows), 3, "the END line was not recorded at all")
        truncated, by_rung = stopprobe.findings(rows)
        self.assertEqual(truncated, [])
        self.assertEqual(by_rung, {})


class StopProbeReadTest(unittest.TestCase):

    def test_a_MISSING_log_is_UNMEASURED_and_never_a_clean_bill(self):
        """Three different worlds produce zero rows: no slow stop has
        happened, the writer was never wired, and the file was deleted. Only
        the error channel separates them."""
        rows, err = stopprobe.read(log="/nonexistent/stopprobe.log")
        self.assertEqual(rows, [])
        self.assertIn("UNMEASURED", err)

    def test_a_line_this_module_did_not_write_is_SKIPPED_not_guessed(self):
        fh = tempfile.NamedTemporaryFile(mode="w", prefix="stopprobe-",
                                         suffix=".log", delete=False)
        fh.write("2026-09-10T12:00:00 BLOW pid=1 cmd=helm inject | wall=9.02\n")
        fh.write("a line from nowhere\n")
        fh.write("2026-09-10T12:00:01 STOP-RUNG pid=2 | seat=s | run=a "
                 "| rung=seam | elapsed=5.000 | total=6.000\n")
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        rows, err = stopprobe.read(log=fh.name)
        self.assertIsNone(err)
        self.assertEqual([r["event"] for r in rows], [stopprobe.RUNG])
        self.assertEqual(rows[0]["rung"], "seam")


    def test_a_torn_line_of_OURS_parses_as_MALFORMED(self):
        fh = tempfile.NamedTemporaryFile(mode="w", prefix="stopprobe-",
                                         suffix=".log", delete=False)
        fh.write("2026-09-10T12:00:01 STOP-RUNG pid=2 | seat=s | rung=seam "
                 "| elapsed=5.000 | total=6.000 | run=a\n")
        fh.write("2026-09-10T12:00:02 STOP-RUNG pid=3 | seat=s | rung=\n")
        fh.write("2026-09-10T12:00:03 BLOW pid=1 cmd=helm inject\n")
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        rows, err = stopprobe.read(log=fh.name)
        self.assertIsNone(err)
        self.assertEqual([r["event"] for r in rows],
                         [stopprobe.RUNG, stopprobe.MALFORMED])
        self.assertEqual(len(stopprobe.malformed(rows)), 1)

    def test_a_payload_field_may_not_REWRITE_the_header(self):
        """A record quoting `event=STOP-END` among its fields would otherwise
        overwrite the event the line announced, so a RUNG could be read as a
        completion by a value it merely carries."""
        fh = tempfile.NamedTemporaryFile(mode="w", prefix="stopprobe-",
                                         suffix=".log", delete=False)
        fh.write("2026-09-10T12:00:01 STOP-RUNG pid=2 | seat=s | run=a "
                 "| rung=seam | elapsed=1.0 | total=6.0 | event=STOP-END\n")
        fh.write("2026-09-10T12:00:02 STOP-RUNG pid=2 | seat=s | run=a "
                 "| rung=seam | elapsed=1.0 | total=6.0 | total=9.9\n")
        fh.write("2026-09-10T12:00:03 STOP-RUNG pid=2 | seat=s | run=a "
                 "| rung=seam | elapsed=1.0 | total=later\n")
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        rows, err = stopprobe.read(log=fh.name)
        self.assertIsNone(err)
        self.assertEqual([r["event"] for r in rows],
                         [stopprobe.MALFORMED] * 3)
        self.assertEqual(len(stopprobe.malformed(rows)), 3)
        # POSITIVE CONTROL, same reader, same file shape, nothing hostile:
        # a clean record still parses as a RUNG, so the three refusals above
        # are the checks firing and not a parser that refuses everything.
        clean = tempfile.NamedTemporaryFile(mode="w", prefix="stopprobe-",
                                            suffix=".log", delete=False)
        clean.write("2026-09-10T12:00:04 STOP-RUNG pid=2 | seat=s | run=a "
                    "| rung=seam | elapsed=1.0 | total=6.0\n")
        clean.close()
        self.addCleanup(os.unlink, clean.name)
        good, _err = stopprobe.read(log=clean.name)
        self.assertEqual([r["event"] for r in good], [stopprobe.RUNG])

    def test_a_malformed_env_threshold_never_raises_at_IMPORT(self):
        for raw in ("x", "", "nan", "inf", "-1", None):
            self.assertEqual(stopprobe.threshold_from_env(raw, stopprobe._DEFAULT_SLOW), 3.0, repr(raw))
        # POSITIVE CONTROL: a legitimate value IS honoured, so the default
        # above is a rejection and not a function that ignores its argument.
        self.assertEqual(stopprobe.threshold_from_env("7.5", stopprobe._DEFAULT_SLOW), 7.5)


class StopProbeAgeTest(unittest.TestCase):
    """PROCESS AGE: the field that separates a slow ladder from a late one.

    Its own fixture rather than a subclass of the writer's: inheriting that
    class would re-run all nine of its arms under a second name, doubling
    their cost and their count while proving nothing new about the age.
    """

    def setUp(self):
        fh = tempfile.NamedTemporaryFile(prefix="stopprobe-age-", suffix=".log",
                                         delete=False)
        fh.close()
        os.unlink(fh.name)          # a path that does not exist yet
        self.log = fh.name
        self.addCleanup(self._remove)

    def _remove(self):
        try:
            os.unlink(self.log)
        except OSError:
            pass

    def lines(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as fh:
            return [ln for ln in fh.read().splitlines() if ln.strip()]

    def test_a_real_record_carries_the_start_age_its_OWNER_measured(self):  # noqa: VACUOUS_ASSERTION — the two assertNotEqual lines are the must-differ half; the unconditional positive on the same observable is the assertIn(' | began=') and the assertEqual against the value the owner measured, two lines above them
        measured = procage.process_age()
        self.assertIsNotNone(measured, "this box must have /proc for this arm")
        self.assertTrue(stopprobe.record("r", "seam", 2.0, 3.1,
                                         log=self.log, slow=3.0,
                                         began=measured))
        line = self.lines()[0]
        self.assertIn(" | began=", line)
        began = float(line.split(" | began=")[1].split(" | ")[0])
        self.assertEqual("%.3f" % began, "%.3f" % measured)
        # THE MUST-DIFFER PAIR: a writer that echoed the LADDER's total, or a
        # zero default, would both be visible here.
        self.assertNotEqual("%.3f" % began, "3.100")
        self.assertNotEqual("%.3f" % began, "0.000")

    def test_the_start_age_does_not_MOVE_when_reporting_is_delayed(self):
        """THE DEFECT A REVIEW FOUND, pinned. The first cut sampled the age
        inside the writer, so any delay between the caller freezing `total`
        and the line being written -- an _emit to stderr can block -- landed
        in `age - total` and was reported as time the process spent BEFORE the
        ladder. Here the owner measures once and two records written 50ms
        apart under the SAME ladder must carry the identical beginning.
        """
        measured = procage.process_age()
        self.assertTrue(stopprobe.record("r", "seam", 2.0, 3.1,
                                         log=self.log, slow=3.0,
                                         began=measured))
        time.sleep(0.05)                 # stand in for a blocking stderr write
        self.assertTrue(stopprobe.record("r", "inbox", 1.0, 4.1,
                                         log=self.log, slow=3.0,
                                         began=measured))
        rows = [stopprobe._parse(line) for line in self.lines()]
        begins = {row["began"] for row in rows}
        self.assertEqual(len(begins), 1, "the beginning must not drift: %r" % begins)
        # MUST-DIFFER CONTROL on the same pair: the TOTALS did advance, so an
        # equal beginning is a constant and not two records that are identical.
        self.assertEqual({row["total"] for row in rows}, {"3.100", "4.100"})
        run = stopprobe.runs(rows)[0]
        self.assertAlmostEqual(run["began_at_age"], measured, places=3)

    def test_a_start_age_that_cannot_be_READ_is_absent_and_never_zero(self):  # noqa: VACUOUS_ASSERTION — the observable is the FILE, read by a separate call from the writer's, so no same-call control can exist; the unmocked real record two lines above writes ' | age=' into this very file and is the must-differ pair for the absence below
        """A 0 here is the STRONGEST claim the field can make -- the ladder is
        the entire life of the process -- so it may never stand for 'unknown'.
        """
        # THE POSITIVE CONTROL RUNS FIRST AND UNCONDITIONAL: a record whose
        # owner DID measure, so the absence below is read against a writer
        # proven to stamp.
        self.assertTrue(stopprobe.record("r", "seam", 2.0, 3.2,
                                         log=self.log, slow=3.0, began=41.5))
        self.assertIn(" | began=41.500", self.lines()[0])
        # An owner that could not measure passes None -- /proc unreadable, or
        # not Linux at all -- and the field is simply not written.
        self.assertTrue(stopprobe.record("r", "seam", 2.0, 3.1,
                                         log=self.log, slow=3.0, began=None))
        self.assertNotIn("began=", self.lines()[1])

    def test_the_reader_separates_a_SLOW_ladder_from_a_LATE_one(self):
        slow = [{"event": stopprobe.RUNG, "pid": "7", "seat": "s", "run": "slow",
                 "rung": "seam", "elapsed": "5.4", "total": "10.3",
                 "began": "0.1"}]
        late = [{"event": stopprobe.RUNG, "pid": "8", "seat": "s", "run": "late",
                 "rung": "seam", "elapsed": "5.4", "total": "10.3",
                 "began": "49.7"}]
        # IDENTICAL TOTALS. Before this field the two were indistinguishable,
        # which is the whole reason the field exists. Anchored on the LITERAL
        # rather than compared to each other: two runs whose totals both failed
        # to parse are also equal, and would satisfy a comparison vacuously.
        self.assertEqual(stopprobe.runs(slow)[0]["total"], 10.3)
        self.assertEqual(stopprobe.runs(late)[0]["total"], 10.3)
        self.assertAlmostEqual(stopprobe.runs(slow)[0]["began_at_age"], 0.1,
                               places=3)
        self.assertAlmostEqual(stopprobe.runs(late)[0]["began_at_age"], 49.7,
                               places=3)

    def test_a_record_written_before_the_field_EXISTED_still_parses(self):
        """2586 records predate this field. A reader that called them torn
        would destroy the population it was added to describe."""
        line = ("2026-09-10T10:00:00 %s pid=7 | seat=s | run=a | rung=seam "
                "| elapsed=5.4 | total=10.1" % stopprobe.RUNG)
        row = stopprobe._parse(line)
        self.assertEqual(row["event"], stopprobe.RUNG)
        self.assertIsNone(stopprobe.runs([row])[0]["began_at_age"])
        # MUST-DIFFER: the same line WITH an age is not None, so the None
        # above is the missing field and not a reader that never computes it.
        with_age = stopprobe._parse(line + " | began=1.9")
        self.assertAlmostEqual(stopprobe.runs([with_age])[0]["began_at_age"],
                               1.9, places=3)

    def test_a_start_age_that_is_not_a_DURATION_is_a_torn_record(self):
        for bad in ("nan", "-1", "inf", "later"):
            line = ("2026-09-10T10:00:00 %s pid=7 | seat=s | run=a | rung=seam "
                    "| elapsed=5.4 | total=10.1 | began=%s"
                    % (stopprobe.RUNG, bad))
            self.assertEqual(stopprobe._parse(line)["event"],
                             stopprobe.MALFORMED, bad)
        good = ("2026-09-10T10:00:00 %s pid=7 | seat=s | run=a | rung=seam "
                "| elapsed=5.4 | total=10.1 | began=12.0" % stopprobe.RUNG)
        self.assertEqual(stopprobe._parse(good)["event"], stopprobe.RUNG)


class StopProbeAgeRungTest(unittest.TestCase):
    """The doctor rung reports WHEN the ladder began, with no threshold."""

    def rung(self, rows, err=None):
        return doctor.check_stop_timings(read=lambda: (rows, err))

    def _row(self, run, total, began):
        row = {"event": stopprobe.RUNG, "pid": "7", "seat": "s", "run": run,
               "rung": "seam", "elapsed": "5.0", "total": total}
        if began is not None:
            row["began"] = began
        return row

    def test_the_rung_names_when_the_ladder_began(self):
        out = self.rung([self._row("a", "10.3", "49.7")])
        self.assertIn("began 49.7s into its process", out[0][1])

    def test_records_with_NO_age_say_so_instead_of_reporting_zero(self):
        out = self.rung([self._row("a", "10.3", None)])
        self.assertIn("no run carries a ladder-start age", out[0][1])
        self.assertNotIn("began 0.0s", out[0][1])
        # AND IT MUST NOT CLAIM PROVENANCE IT CANNOT HAVE. A fresh record whose
        # owner could not read /proc is missing the field exactly as an old one
        # is; saying "predate the field" names the comfortable world of two.
        self.assertNotIn("predate the field)", out[0][1])
        self.assertIn("cannot tell which", out[0][1])
        # MUST-DIFFER on the same rung: one row with a beginning and the
        # sentence changes.
        aged = self.rung([self._row("b", "10.3", "49.7")])
        self.assertIn("began 49.7s", aged[0][1])

    def test_a_MIXED_population_declares_how_many_carry_the_age(self):
        out = self.rung([self._row("a", "10.3", "49.7"),
                         self._row("b", "11.0", "0.1"),
                         self._row("c", "12.0", None)])
        self.assertIn("0.1s-49.7s", out[0][1])
        self.assertIn("2 of 3 carry it", out[0][1])
        self.assertIn("UNAVAILABLE", out[0][1])


class StopProbeRungTest(unittest.TestCase):
    """The doctor rung over the same log."""

    def rung(self, rows, err):
        return doctor.check_stop_timings(read=lambda: (rows, err))

    def test_an_unreadable_log_is_reported_and_never_an_all_clear(self):
        out = self.rung([], "no stop-timing log at /x — UNMEASURED, not quiet")
        self.assertEqual([lvl for lvl, _l in out], [doctor.WARN])
        self.assertIn("UNMEASURED", out[0][1])

    def test_a_truncated_ladder_names_its_rung_and_the_count(self):
        rows = [
            {"event": stopprobe.RUNG, "pid": "7", "seat": "s", "run": "a",
             "rung": "seam", "elapsed": "5.4", "total": "10.1"},
            {"event": stopprobe.RUNG, "pid": "8", "seat": "s", "run": "b",
             "rung": "seam", "elapsed": "6.0", "total": "11.0"},
        ]
        out = self.rung(rows, None)
        self.assertEqual([lvl for lvl, _l in out], [doctor.WARN])
        self.assertIn("seam", out[0][1])
        self.assertIn("2", out[0][1])

    def test_a_log_with_only_COMPLETE_slow_ladders_reports_the_count(self):
        """THE UNCONDITIONAL POSITIVE CONTROL: an empty finding list is what
        this rung says on a good day AND what it would say if the log had
        been read as empty, so the OK line carries how many it read."""
        rows = [
            {"event": stopprobe.RUNG, "pid": "7", "seat": "s", "run": "a",
             "rung": "seam", "elapsed": "3.1", "total": "4.0"},
            {"event": stopprobe.END, "pid": "7", "seat": "s", "run": "a",
             "rung": "response", "total": "4.2"},
        ]
        out = self.rung(rows, None)
        self.assertEqual([lvl for lvl, _l in out], [doctor.OK])
        self.assertIn("1", out[0][1])

    def test_a_REUSED_PID_cannot_hide_an_unfinished_new_run(self):
        """A pid is not a run. Grouping a lifetime log by pid folds a
        finished ladder together with a NEW one under the same pid, and the
        new one's missing end disappears into the old one's END."""
        rows = [
            {"event": stopprobe.RUNG, "pid": "7", "seat": "s", "run": "old",
             "rung": "seam", "elapsed": "3.1", "total": "4.0"},
            {"event": stopprobe.END, "pid": "7", "seat": "s", "run": "old",
             "rung": "response", "total": "4.2"},
            {"event": stopprobe.RUNG, "pid": "7", "seat": "s", "run": "new",
             "rung": "claims", "elapsed": "9.0", "total": "9.0"},
        ]
        unfinished, by_rung = stopprobe.findings(rows)
        self.assertEqual(len(unfinished), 1)
        self.assertEqual(by_rung, {"claims": 1})
        # POSITIVE CONTROL on the same rows: the finished ladder is still
        # seen, so the count above is a discrimination and not a blanket.
        self.assertEqual(len(stopprobe.runs(rows)), 2)

    def test_OUR_OWN_torn_line_is_MALFORMED_and_never_dropped(self):
        rows = [
            {"event": stopprobe.RUNG, "pid": "7", "seat": "s", "run": "a",
             "rung": "seam", "elapsed": "5.4", "total": "10.1"},
            {"event": stopprobe.MALFORMED, "pid": "7", "missing": "total",
             "raw": "torn"},
        ]
        out = self.rung(rows, None)
        text = " ".join(line for _lvl, line in out)
        self.assertIn("MALFORMED", text)
        self.assertIn("UNKNOWN", text)

    def test_an_absent_END_is_reported_as_UNOBSERVED_not_as_a_death(self):
        """The rung said a stop that dies mid-rung ended UNCHECKED. An absent
        end is also what a still-running ladder and a failed end-append look
        like, and the rung cannot tell those apart."""
        rows = [{"event": stopprobe.RUNG, "pid": "7", "seat": "s", "run": "a",
                 "rung": "seam", "elapsed": "5.4", "total": "10.1"}]
        line = self.rung(rows, None)[0][1]
        self.assertIn("recorded no end", line)
        self.assertIn("STILL RUNNING", line)
        self.assertNotIn("UNCHECKED", line)

    def test_the_rung_is_REGISTERED_and_not_merely_defined(self):
        """BUILT IS NOT WIRED, AND THIS SHIPPED UNWIRED ONCE. The function
        existed, its arms were green, the module was reachable, and
        `helm wiring --gate` was satisfied — because reachability of the
        MODULE is a different question from registration of the CHECK. Only
        the registry answers it, so the registry is what is asserted.
        """
        self.assertIn("check_stop_timings", doctor.CHECKS)
        # POSITIVE CONTROL on the same observable: the registry is a real
        # tuple of real check names, not an empty or arbitrary container.
        # The neighbour named here must exist on TRUNK: a check that lives
        # only in a sibling lane makes this control fail for a reason that
        # has nothing to do with the property under test.
        self.assertIn("check_stale_bot", doctor.CHECKS)
        self.assertTrue(all(hasattr(doctor, name) for name in doctor.CHECKS))

    def test_a_reader_that_raises_leaves_the_report_standing(self):
        def boom():
            raise RuntimeError("no log here")
        out = doctor.check_stop_timings(read=boom)
        self.assertEqual([lvl for lvl, _l in out], [doctor.WARN])
        self.assertIn("cannot tell", out[0][1])


class RungTimingWiringTest(unittest.TestCase):
    """THE SENSOR IS ONLY DELIVERED WHEN SOMETHING CALLS IT.

    A module with a green reader and no caller is an instrument that reports
    UNMEASURED forever, and its own doctor rung would say so honestly while
    everyone read the silence as calm.
    """

    def setUp(self):
        fh = tempfile.NamedTemporaryFile(prefix="stopprobe-wire-",
                                         suffix=".log", delete=False)
        fh.close()
        os.unlink(fh.name)
        self.log = fh.name
        self.addCleanup(self._remove)
        # THROUGH THE REAL DOOR, NOT A PATCHED CONSTANT. Patching a module
        # attribute proved the writer honoured that attribute and nothing
        # about how production resolves its path — which is exactly how a
        # module that could not see HELM_HOME kept a green suite while writing
        # into the live log. The env override IS the production door.
        self._log_patch = mock.patch.dict(
            os.environ, {"HELM_STOPPROBE_LOG": self.log})
        self._slow_patch = mock.patch.object(stopprobe, "SLOW", 0.0)
        self._log_patch.start()
        self._slow_patch.start()
        self.addCleanup(self._log_patch.stop)
        self.addCleanup(self._slow_patch.stop)

    def _remove(self):
        try:
            os.unlink(self.log)
        except OSError:
            pass

    def test_a_ladder_that_RUNS_leaves_its_boundaries_in_the_log(self):
        ladder = seats_stop_timing.RungTiming()
        ladder.done("identity", next_name="wiring")
        ladder.done("wiring")
        ladder.finish("wiring")
        rows, err = stopprobe.read(log=self.log)
        self.assertIsNone(err, "the ladder wrote no file at all")
        events = [(r["event"], r.get("rung")) for r in rows]
        self.assertIn((stopprobe.RUNG, "identity"), events)
        self.assertIn((stopprobe.RUNG, "wiring"), events)
        self.assertIn((stopprobe.END, "wiring"), events)

    def test_EVERY_record_the_real_ladder_writes_carries_ONE_beginning(self):  # noqa: VACUOUS_ASSERTION — the assertNotIn(None) is the must-differ half; the unconditional positives on the same rows are the assertEqual of the single beginning against ladder.start_age and the assertGreater on the distinct totals
        """THE THREADING, not the parameter. A previous lane of mine added an
        argument to this writer and proved it by calling the writer directly,
        which tests the parameter and says nothing about whether production
        passes it -- dropping the threading reddened zero arms. This drives the
        REAL RungTiming through every one of its three writer paths -- `done`,
        the end, and `measure` (NOT `begin`, which writes nothing) -- and
        demands one beginning across all of them.
        """
        ladder = seats_stop_timing.RungTiming()
        self.assertIsNotNone(ladder.start_age,
                             "this box must have /proc for this arm")
        # Real elapsed time between boundaries, so the totals genuinely
        # advance and the must-differ control below is not satisfied by a
        # ladder that ran in under a millisecond.
        time.sleep(0.02)
        ladder.done("identity", next_name="wiring")
        # `measure` IS THE THIRD WRITER, AND `begin` IS NOT. begin only names a
        # span and emits; it writes no record. A sequence built from begin+done
        # therefore exercises `done` twice and leaves _measure_finish untouched,
        # so an arm written that way claims three writer paths and reaches two
        # -- the same overclaim this arm exists to prevent. Verified with a spy
        # on _measure_finish rather than by reading the call sites.
        time.sleep(0.02)
        ladder.measure("external", lambda: None)
        time.sleep(0.02)
        ladder.finish("wiring")
        rows, err = stopprobe.read(log=self.log)
        self.assertIsNone(err)
        begins = {r.get("began") for r in rows}
        self.assertEqual(len(rows) >= 3 and len(begins), 1,
                         "rows=%d beginnings=%r" % (len(rows), begins))
        self.assertNotIn(None, begins, "a record reached the log unstamped")
        self.assertEqual(begins.pop(), "%.3f" % ladder.start_age)
        # MUST-DIFFER on the same rows: the totals DID advance across them, so
        # a single beginning is a measured constant and not three identical
        # records.
        self.assertGreater(len({r.get("total") for r in rows}), 1)

    def test_a_rung_that_NAMES_NO_SUCCESSOR_does_not_end_the_ladder(self):
        """THE HIGH FINDING OF ROUND TWO. The real guard ends `mechanical`
        without naming a successor and the CLI then continues into `response`
        under the SAME run. Reading a missing argument as the lifecycle's end
        stamped the ladder complete at `mechanical`, and a `response` that
        stalled was hidden behind that stamp — a record claiming exactly the
        completion this instrument exists to doubt.
        """
        ladder = seats_stop_timing.RungTiming()
        ladder.done("mechanical")            # no successor named, not the end
        rows, _err = stopprobe.read(log=self.log)
        self.assertEqual([r["event"] for r in rows], [stopprobe.RUNG])
        unfinished, _by = stopprobe.findings(rows)
        self.assertEqual(len(unfinished), 1, "an END was stamped anyway")
        # THE TWIN, on the same ladder and the same run: the lifecycle's own
        # terminal DOES end it, so the absence above is the missing END and
        # not a writer that cannot record one.
        ladder.done("response")
        ladder.finish("response")
        rows, _err = stopprobe.read(log=self.log)
        unfinished, _by = stopprobe.findings(rows)
        self.assertEqual(unfinished, [])

    def test_a_ladder_CUT_SHORT_leaves_the_last_rung_it_finished(self):
        """The must-hit: no END, and findings names the last rung."""
        ladder = seats_stop_timing.RungTiming()
        ladder.done("identity", next_name="seam")
        # ... and then the outer timeout kills it mid-seam: no further call
        # arrives, and `identity` is the last rung SEEN TO FINISH.
        rows, _err = stopprobe.read(log=self.log)
        truncated, by_rung = stopprobe.findings(rows)
        self.assertEqual(len(truncated), 1)
        self.assertEqual(by_rung, {"identity": 1})

    def test_a_FAST_ladder_writes_nothing_through_the_real_caller(self):
        """The silence property, proven through production's own call path
        rather than through a direct call to the writer."""
        self._slow_patch.stop()
        with mock.patch.object(stopprobe, "SLOW", 3600.0):
            ladder = seats_stop_timing.RungTiming()
            ladder.done("identity", next_name="wiring")
            ladder.done("wiring")
        self.assertFalse(os.path.exists(self.log))
        # POSITIVE CONTROL on the same observable: the same two calls under a
        # zero threshold DO leave the file behind.
        self._slow_patch.start()
        ladder = seats_stop_timing.RungTiming()
        ladder.done("identity", next_name="wiring")
        ladder.done("wiring")
        self.assertTrue(os.path.exists(self.log))


if __name__ == "__main__":
    unittest.main()


class TheDogfoodFoundThreeThingsTheArmsCouldNotTest(unittest.TestCase):
    """THREE DEFECTS THAT ONLY A READ OF THE REAL LOG COULD SHOW, and each one
    was invisible to a green suite for its own reason.

    THE PATH BYPASSED HELM_HOME. Every helm test is hermetic by pointing
    HELM_HOME at a temp directory and every sibling module reaches its files
    through `home.helm_home()`. This module expanded `~` directly, so that
    isolation could not reach it and any suite driving a Stop ladder wrote into
    the live log under fixture seat names. The arms could not see it because
    they patched the module's path CONSTANT — proving the writer honoured that
    attribute, and nothing about how production resolves a path.

    THE SEAT WAS ASKED OF THE ENVIRONMENT. The guard resolves its acting seat
    in its first rung; the hook process it runs in exports no seat name, so
    re-deriving inside the writer answered "unknown" for essentially every
    production record. A per-seat population is the whole reason this
    instrument replaced a transcript scrape.

    THE END WAS ONE CALL TOO EARLY, and this is the one worth remembering: the
    arms drove the ladder BY HAND, in the correct order, so they could never
    catch a CALLER that drove it in the wrong one. The lifecycle ends at the
    response publisher, not when the guard function returns."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="stopprobe-dogfood-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env = mock.patch.dict(os.environ, {"HELM_HOME": self.tmp})
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop("HELM_STOPPROBE_LOG", None)
        self.slow = mock.patch.object(stopprobe, "SLOW", 0.0)
        self.slow.start()
        self.addCleanup(self.slow.stop)

    # -- the path ----------------------------------------------------------

    def test_HELM_HOME_reaches_the_log_so_a_suite_cannot_write_to_production(self):
        """The isolation every other module gets, and the one this file broke.
        The control is the same call with HELM_HOME pointed somewhere else:
        a path that ignored it would be identical under both."""
        mine = stopprobe.log_path()
        self.assertTrue(mine.startswith(self.tmp),
                        "the log escaped HELM_HOME: %s" % mine)
        with mock.patch.dict(os.environ, {"HELM_HOME": self.tmp + "-other"}):
            self.assertNotEqual(stopprobe.log_path(), mine)

    def test_the_named_override_still_wins_for_the_one_live_producer(self):
        """The override exists so a REAL Stop can write a REAL record
        somewhere other than production. HELM_HOME must not take it away."""
        with mock.patch.dict(os.environ,
                             {"HELM_STOPPROBE_LOG": self.tmp + "/named.log"}):
            self.assertEqual(stopprobe.log_path(), self.tmp + "/named.log")

    def test_the_path_is_resolved_per_call_not_at_import(self):
        """A constant computed at import is decided by whoever imported first,
        which in a suite is the harness — before any fixture set anything."""
        first = stopprobe.log_path()
        with mock.patch.dict(os.environ, {"HELM_HOME": self.tmp + "-later"}):
            self.assertNotEqual(stopprobe.log_path(), first)

    # -- the seat ----------------------------------------------------------

    def test_the_seat_the_LADDER_knows_is_what_gets_recorded(self):
        """Not the environment, which the hook process does not carry. The
        control is the same write with no seat known: it must NOT silently
        borrow the caller's name."""
        log = os.path.join(self.tmp, "seat.log")
        stopprobe.record("r-1", "identity", 0.1, 9.0, log=log, seat="seat-a")
        stopprobe.record("r-1", "inbox", 0.1, 9.1, log=log)
        rows, err = stopprobe.read(log=log)
        self.assertIsNone(err)
        self.assertEqual([r["seat"] for r in rows], ["seat-a", "unknown"])

    def test_a_KNOWN_seat_is_laundered_like_every_other_display_sink(self):
        """It arrives from a resolver, not from a validated ingestion seam, and
        this file is a sink somebody later greps."""
        log = os.path.join(self.tmp, "hostile.log")
        stopprobe.record("r-2", "identity", 0.1, 9.0, log=log,
                         seat="‮seat-b")
        rows, _err = stopprobe.read(log=log)
        self.assertEqual(len(rows), 1)
        self.assertNotIn("‮", rows[0]["seat"])
        self.assertIn("seat-b", rows[0]["seat"])   # the name still survives

    def test_the_LADDER_carries_its_seat_into_every_record_it_writes(self):
        """THE ARM THE FIRST MUTATION PASS SAID WAS MISSING. Calling the writer
        directly with a seat proves the WRITER honours a parameter; it says
        nothing about whether the ladder threads it, and threading it is the
        whole fix — removing `seat=self.seat` from the boundary writer reddened
        NOTHING until this existed.

        Both writers are covered, because they are two call sites: the rung
        boundary and the ladder's end."""
        log = os.path.join(self.tmp, "ladder.log")
        with mock.patch.dict(os.environ, {"HELM_STOPPROBE_LOG": log}):
            ladder = seats_stop_timing.RungTiming()
            ladder.seat = "seat-a"
            ladder.done("identity", next_name="inbox")
            ladder.done("inbox")
            ladder.finish("inbox")
        rows, err = stopprobe.read(log=log)
        self.assertIsNone(err)
        self.assertEqual(sorted({r["seat"] for r in rows}), ["seat-a"],
                         "the ladder knew its seat and the records did not")
        # AND THE MUST-DIFFER CONTROL: a ladder that was never told stays
        # honest about not knowing, rather than borrowing an ambient name.
        anon = os.path.join(self.tmp, "anon.log")
        with mock.patch.dict(os.environ, {"HELM_STOPPROBE_LOG": anon}):
            quiet = seats_stop_timing.RungTiming()
            quiet.done("identity", next_name="inbox")
        rows, _err = stopprobe.read(log=anon)
        self.assertEqual([r["seat"] for r in rows], ["unknown"])

    def test_the_MEASURED_span_carries_the_seat_too(self):
        """THE THIRD CALL SITE, and the arm above does not reach it. `measure`
        is a separate writer from `done` — the heavy rungs go through it — and
        dropping the seat there reddened nothing until this existed. Three
        writers is three sites; a sweep of the ones you happened to think of is
        not a sweep."""
        log = os.path.join(self.tmp, "measured.log")
        with mock.patch.dict(os.environ, {"HELM_STOPPROBE_LOG": log}):
            ladder = seats_stop_timing.RungTiming()
            ladder.seat = "seat-a"
            ladder.measure("seam", lambda: "answer")
        rows, err = stopprobe.read(log=log)
        self.assertIsNone(err)
        self.assertEqual([(r.get("rung"), r["seat"]) for r in rows],
                         [("seam", "seat-a")])

    # -- the end -----------------------------------------------------------

    def test_NOTHING_is_recorded_after_the_END_through_the_REAL_publisher(self):
        """THE ARM THAT WOULD HAVE CAUGHT IT. Driving the ladder by hand in the
        right order proves the writer can be called correctly, never that
        production calls it correctly — and production ended the run one call
        before its last rung began. This goes through the publisher."""
        from helm import seats_stop_budget, seats_stop_response
        log = os.path.join(self.tmp, "order.log")
        with mock.patch.dict(os.environ, {"HELM_STOPPROBE_LOG": log}):
            budget = seats_stop_budget.State()
            budget.timing.seat = "seat-a"
            budget.timing.done("mechanical")
            seats_stop_response.publish([], [], budget)
        rows, err = stopprobe.read(log=log)
        self.assertIsNone(err)
        events = [r["event"] for r in rows]
        self.assertIn(stopprobe.END, events)
        self.assertEqual(events[-1], stopprobe.END,
                         "a boundary was recorded after its own run's end: %s"
                         % [(r["event"], r.get("rung")) for r in rows])
        self.assertEqual(rows[-1].get("rung"), "response",
                         "the end names a rung that is not the ladder's last")
        self.assertEqual(sorted({r["seat"] for r in rows}), ["seat-a"],
                         "the production path lost the seat the ladder knew")

    def test_an_EXPIRED_ladder_still_declares_no_end(self):
        """The case the durable record exists to catch. publish_expired does
        not route through the response rung at all, so it holds no run identity
        and cannot end a run it was never part of. The positive control is the
        arm above: an ordinary publish DOES write one."""
        from helm import seats_stop_budget, seats_stop_response
        log = os.path.join(self.tmp, "expired.log")
        with mock.patch.dict(os.environ, {"HELM_STOPPROBE_LOG": log}):
            budget = seats_stop_budget.State()
            budget.timing.seat = "seat-a"
            budget.timing.done("seam", next_name="ndp")
            seats_stop_response.publish_expired(["blocked"], [])
        rows, _err = stopprobe.read(log=log)
        self.assertNotIn(stopprobe.END, [r["event"] for r in rows])
        unfinished, _by = stopprobe.findings(rows)
        self.assertEqual(len(unfinished), 1)
