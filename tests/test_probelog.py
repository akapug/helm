"""The shared instrument grammar's own properties, tested at the shared door.

EVERY PROPERTY HERE WAS BOUGHT BY AN INCIDENT AND NONE OF THEM HAD AN ARM.
Measured while extracting this module: mutating the reserved-key rule so a
payload may rewrite the header scored ZERO reds across the 44 arms of the
instrument that documents that rule in its own docstring. A property stated in
prose and enforced by nothing is a `documented-exclusion-untested`: the next
reader widens or deletes it because nothing complains.

AND SHARING RAISES THE STAKES RATHER THAN LOWERING THEM. One writer and one
parser now serve every instrument in this grammar, so a silent regression here
is a silent regression in all of them at once. The arms belong at the shared
door for exactly the reason the code does.
"""
import datetime
import os
import tempfile
import unittest

from helm import probelog


RUNG = "TEST-RUNG"
END = "TEST-END"
BAD = "TEST-BAD"
EVENTS = (RUNG, END)
REQUIRED = {RUNG: ("run", "rung"), END: ("run",)}
NUMERIC = ("elapsed",)


class ProbeLogBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="probelog-")
        self.path = os.path.join(self.dir, "nested", "probe.log")

    def write(self, event, fields, **kw):
        return probelog.write(self.path, event, fields,
                              required=REQUIRED.get(event, ()), **kw)

    def parse(self, line):
        return probelog.parse(line, EVENTS, REQUIRED, NUMERIC, BAD)

    def lines(self):
        with open(self.path) as fh:
            return fh.read().splitlines()


class WriteTest(ProbeLogBase):
    def test_a_record_round_trips_through_the_parser(self):
        """The positive control every refusal below is measured against."""
        self.assertTrue(self.write(RUNG, [("run", "r1"), ("rung", "identity"),
                                          ("elapsed", "0.500")]))
        row = self.parse(self.lines()[0])
        self.assertEqual(row["event"], RUNG)
        self.assertEqual(row["run"], "r1")
        self.assertEqual(row["rung"], "identity")
        self.assertEqual(row["elapsed"], "0.500")

    def test_the_writer_refuses_what_its_own_reader_would_refuse(self):
        """A blank required field wrote successfully and came back MALFORMED --
        a success that is a lie about the file."""
        # POSITIVE CONTROL FIRST, on the same writer and the same path: a
        # refusal and a writer that cannot write at all are the same green.
        self.assertTrue(self.write(RUNG, [("run", "r1"), ("rung", "identity")]))
        self.assertEqual(len(self.lines()), 1)
        self.assertFalse(self.write(RUNG, [("run", ""), ("rung", "identity")]))
        self.assertEqual(len(self.lines()), 1)

    def test_a_required_field_that_is_ABSENT_is_refused_not_only_a_blank_one(self):
        """FOUND BY A CALLER, NOT BY THIS SUITE. The first cut iterated the
        FIELDS SUPPLIED, so a required key that was simply not passed was never
        examined -- and a caller that omits empty values (the natural way to
        write "this record had no identity") filed a line the parser then
        reported as MALFORMED. Absent and blank are one refusal."""
        self.assertTrue(self.write(RUNG, [("run", "r1"), ("rung", "identity")]))
        self.assertEqual(len(self.lines()), 1)
        self.assertFalse(self.write(RUNG, [("run", "r1")]))       # rung ABSENT
        self.assertFalse(self.write(RUNG, [("run", "r1"), ("rung", "  ")]))
        self.assertEqual(len(self.lines()), 1)

    def test_it_creates_the_directory_rather_than_losing_the_record(self):
        self.assertFalse(os.path.isdir(os.path.dirname(self.path)))
        self.assertTrue(self.write(END, [("run", "r1")]))
        self.assertEqual(len(self.lines()), 1)

    def test_a_write_that_cannot_happen_returns_False_and_does_not_raise(self):  # noqa: VACUOUS_ASSERTION — control writes the identical record to a path that works; mutant M6 (raise instead of return False) reddens this arm and only this arm
        """An instrument that can take its subject down is worse than none."""
        # POSITIVE CONTROL FIRST: this exact record, written somewhere it CAN
        # go, succeeds -- so the False below is the path and not the record.
        self.assertTrue(probelog.write(self.path, END, [("run", "r1")],
                                       required=REQUIRED[END]))
        blocked = os.path.join(self.dir, "afile")
        open(blocked, "w").close()
        ok = probelog.write(os.path.join(blocked, "nope.log"), END,
                            [("run", "r1")], required=REQUIRED[END])
        self.assertFalse(ok)

    def test_appending_keeps_every_earlier_record(self):
        self.assertTrue(self.write(RUNG, [("run", "r0"), ("rung", "identity")]))
        self.assertEqual(len(self.lines()), 1)
        self.assertTrue(self.write(RUNG, [("run", "r1"), ("rung", "identity")]))
        self.assertTrue(self.write(RUNG, [("run", "r2"), ("rung", "identity")]))
        self.assertEqual(len(self.lines()), 3)
        self.assertEqual([l.split("run=")[1].split()[0] for l in self.lines()],
                         ["r0", "r1", "r2"])


class ParseTest(ProbeLogBase):
    def test_a_foreign_line_is_skipped_and_our_torn_line_is_not(self):  # noqa: VACUOUS_ASSERTION — the None is the POINT (a foreign line is not ours); mutant M7 (accept any event) reddens this arm and only this arm
        """THREE OUTCOMES, NOT TWO. Dropping our own torn record renders a
        damaged log exactly like a clean one."""
        # POSITIVE CONTROL FIRST: a complete record of ours parses as ours,
        # so None below means FOREIGN and not "the parser reads nothing".
        ours = self.parse("2026-09-10T22:00:00 TEST-RUNG pid=1 | run=r1 | rung=x")
        self.assertEqual(ours["event"], RUNG)
        self.assertIsNone(self.parse("2026-09-10T22:00:00 OTHER-EVENT pid=1 | x=1"))
        torn = self.parse("2026-09-10T22:00:00 TEST-RUNG pid=1 | run=r1")
        self.assertEqual(torn["event"], BAD)
        self.assertIn("rung", torn["missing"])

    def test_the_payload_may_not_rewrite_the_header(self):  # noqa: VACUOUS_ASSERTION — control parses the same record without the reserved key; mutant M2 (let the payload rewrite it) reddens this arm and only this arm
        """A record quoting `event=TEST-END` among its fields would otherwise
        be READ as an END by a value it merely mentions."""
        # POSITIVE CONTROL FIRST, on the same observable: the identical record
        # WITHOUT the reserved key is read as the event its header announced.
        clean = self.parse("2026-09-10T22:00:00 TEST-RUNG pid=1 | run=r1 | rung=x")
        self.assertEqual(clean["event"], RUNG)
        line = "2026-09-10T22:00:00 TEST-RUNG pid=1 | run=r1 | rung=x | event=TEST-END"
        row = self.parse(line)
        self.assertEqual(row["event"], BAD)
        self.assertIn("reserved:event", row["missing"])

    def test_a_repeated_key_is_torn_not_last_value_wins(self):
        line = "2026-09-10T22:00:00 TEST-RUNG pid=1 | run=r1 | rung=a | rung=b"
        row = self.parse(line)
        self.assertEqual(row["event"], BAD)
        self.assertIn("repeated:rung", row["missing"])
        self.assertEqual(row["rung"], "a")

    def test_a_duration_that_is_not_finite_is_uncertainty_never_a_zero(self):  # noqa: VACUOUS_ASSERTION — control reads elapsed=0 on the same field; mutant M4 (accept nan/inf/negative) reddens this arm and only this arm
        """float() ACCEPTS nan, inf and negatives, so a bare parse is not a
        validation: `elapsed=nan` compares false against every bound."""
        # POSITIVE CONTROL FIRST: a real duration on the same field is read as
        # ours, so BAD below is the VALUE and never the shape of the line.
        ok = self.parse("2026-09-10T22:00:00 TEST-RUNG pid=1 | run=r1 "
                        "| rung=x | elapsed=0")
        self.assertEqual(ok["event"], RUNG)
        self.assertEqual(ok["elapsed"], "0")
        for value in ("nan", "inf", "-inf", "-1", "abc"):
            row = self.parse("2026-09-10T22:00:00 TEST-RUNG pid=1 | run=r1 "
                             "| rung=x | elapsed=%s" % value)
            self.assertEqual(row["event"], BAD, value)

    def test_a_line_missing_its_pid_is_our_own_torn_record(self):
        row = self.parse("2026-09-10T22:00:00 TEST-RUNG | run=r1 | rung=x")
        self.assertEqual(row["event"], BAD)
        self.assertIn("pid", row["missing"])


class ReadTest(ProbeLogBase):
    def test_a_missing_file_is_reported_never_rendered_as_a_clean_bill(self):
        """An instrument that observed nothing and one that was never wired
        produce the same zero rows; only the error channel separates them."""
        # POSITIVE CONTROL FIRST: this same read, on a log that EXISTS,
        # returns rows and no error -- so the sentence below is about absence.
        self.write(RUNG, [("run", "r1"), ("rung", "x")])
        rows, err = probelog.read(self.path, EVENTS, REQUIRED, NUMERIC, BAD)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(err)
        os.remove(self.path)
        rows, err = probelog.read(self.path, EVENTS, REQUIRED, NUMERIC, BAD,
                                  absent="never wired")
        self.assertEqual(rows, [])
        self.assertEqual(err, "never wired")

    def test_an_existing_log_reads_its_rows_with_no_error(self):
        self.write(RUNG, [("run", "r1"), ("rung", "identity")])
        rows, err = probelog.read(self.path, EVENTS, REQUIRED, NUMERIC, BAD)
        self.assertIsNone(err)
        self.assertEqual([r["event"] for r in rows], [RUNG])

    def test_an_unreadable_log_is_UNMEASURED_and_not_empty(self):
        # POSITIVE CONTROL FIRST, at a readable sibling path: the same call
        # reads rows cleanly, so UNMEASURED below names the FILE and not the call.
        good = os.path.join(self.dir, "good.log")
        probelog.write(good, END, [("run", "r1")], required=REQUIRED[END])
        rows, err = probelog.read(good, EVENTS, REQUIRED, NUMERIC, BAD)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(err)
        os.makedirs(os.path.dirname(self.path))
        os.mkdir(self.path)          # a directory where a file is expected
        rows, err = probelog.read(self.path, EVENTS, REQUIRED, NUMERIC, BAD)
        self.assertEqual(rows, [])
        self.assertIn("UNMEASURED", err)


class CutDeclaresItsClockTest(ProbeLogBase):
    """task/2458 — a cut string and a log line look identical and can be kept
    on different clocks, and the comparison then returns a believable ZERO.

    A zero is the one answer nobody re-checks: "no samples in that window"
    reads as a fact about the fleet rather than as a fact about the cut. The
    incident behind this class split a local-time log at UTC and reported that
    a cure had changed nothing.
    """

    def test_the_WRITER_is_what_declares_the_clock(self):
        """NOT A CONSTANT SOMEBODY TYPED. The rule is only true while the
        writer keeps stamping local text, so read the file it actually
        produces: a naive stamp has no zone marker and no offset."""
        self.assertTrue(self.write(RUNG, (("run", "r1"), ("rung", "a"))))
        stamp = self.lines()[0].split(" ", 1)[0]
        self.assertNotIn("Z", stamp,
                         "the writer now marks a zone, so `probelog.CLOCK` is "
                         "stale and every cut below is judged by the wrong "
                         "rule: %r" % stamp)
        self.assertIsNone(
            datetime.datetime.fromisoformat(stamp).utcoffset(),
            "the writer now stamps an offset: %r" % stamp)
        self.assertEqual(probelog.CLOCK, "naive-local")
        # THE UNCONDITIONAL POSITIVE ON THE SAME STRING: a stamp this writer
        # produced is itself a VALID cut on this clock, and comes back
        # unchanged. That ties the two halves together — the absences above
        # are about a stamp that parses, not about an empty line.
        self.assertEqual(probelog.cut(stamp), (stamp, None))

    def test_a_ZONED_cut_against_LOCAL_rows_REFUSES(self):
        value, why = probelog.cut("2026-09-13T19:00:00Z")
        self.assertIsNone(value, "a zoned cut was accepted against local rows")
        self.assertIn("LOCAL wall clock", why)
        # AN OFFSET IS A ZONE TOO. `fromisoformat` accepts it silently, so a
        # rule that only looked for a trailing Z would pass this one through.
        value2, why2 = probelog.cut("2026-09-13T12:00:00-07:00")
        self.assertIsNone(value2, "an OFFSET cut was accepted: %r" % why2)
        self.assertIn("LOCAL wall clock", why2)

    def test_a_NAIVE_cut_against_ABSOLUTE_rows_REFUSES(self):
        """THE SAME RULE FROM THE OTHER SIDE, and the direction that is easy
        to forget: a store of absolute instants cannot place an unmarked
        string without guessing which zone the caller meant."""
        value, why = probelog.cut("2026-09-13T12:00:00",
                                  probelog.CLOCK_ABSOLUTE)
        self.assertIsNone(value)
        self.assertIn("ABSOLUTE", why)
        # THE POSITIVE ON THE SAME DOOR: a zoned cut on this clock RESOLVES,
        # so the refusal above is about the mismatch and not about a function
        # that refuses everything.
        ok, none = probelog.cut("2026-09-13T19:00:00Z",
                                probelog.CLOCK_ABSOLUTE)
        self.assertIsNone(none)
        days = (datetime.date(2026, 9, 13) - datetime.date(1970, 1, 1)).days
        self.assertEqual(ok, (days * 86400 + 19 * 3600) * 1_000_000_000)

    def test_EVERY_ZONE_SPELLING_refuses_not_only_the_one_i_thought_of(self):
        """The first cut of this rule hand-matched `+HH:MM` BY POSITION.

        `fromisoformat` also accepts `+HHMM` and `+HH`, so those parsed as
        AWARE while the awareness test called them naive — they sailed through
        the very check they had to fail. The rule now asks the PARSED object,
        so no spelling escapes it.
        """
        for spelling in ("2026-09-13T19:00:00Z", "2026-09-13T19:00:00z",
                         "2026-09-13T12:00:00+07:00", "2026-09-13T12:00:00+0700",
                         "2026-09-13T12:00:00+07", "2026-09-13T12:00:00-07:00",
                         "2026-09-13T19:00:00+00:00"):
            value, why = probelog.cut(spelling)
            self.assertIsNone(value, "accepted a zoned cut: %r" % spelling)
            self.assertIn("LOCAL wall clock", why, spelling)
        # THE POSITIVE ON THE SAME DOOR: the unzoned twin of the same instant
        # resolves, so the arm is about the ZONE and not about the string.
        ok, none = probelog.cut("2026-09-13T12:00:00")
        self.assertIsNone(none)
        self.assertEqual(ok, "2026-09-13T12:00:00")

    def test_awareness_is_utcoffset_not_the_tzinfo_ATTRIBUTE(self):
        """A tzinfo whose `utcoffset()` answers None is nominally NAIVE under
        the datetime contract, so attribute presence is not the fact."""
        class Nominal(datetime.tzinfo):
            def utcoffset(self, dt):
                return None

            def dst(self, dt):
                return None

        nominal = datetime.datetime(2026, 9, 13, 12, tzinfo=Nominal())
        self.assertIsNotNone(nominal.tzinfo, "fixture: no tzinfo attached")
        self.assertIsNone(nominal.utcoffset(),
                          "fixture: this tzinfo reports an offset, so it is "
                          "not the nominally-naive case this arm is about")
        value, why = probelog.cut(nominal.isoformat())
        self.assertIsNone(why, "a nominally-naive cut was refused: %s" % why)
        # THE SAME OBSERVABLE, POSITIVELY: the cut resolves to the exact text
        # a naive cut of that instant produces, so "not refused" is measured
        # as "usable" rather than as a quiet None.
        self.assertEqual(value, "2026-09-13T12:00:00")

    def test_a_FRACTION_is_kept_and_lands_after_its_own_second(self):
        """`strftime("%FT%T")` FLOORED it, widening the window by up to a
        second. Rows carry no fraction, so a `.5` cut must sort after a row
        stamped on that second and before the next one — measured in BOTH
        directions against a row this test writes."""
        self.assertTrue(self.write(RUNG, (("run", "r1"), ("rung", "a"))))
        rows, why = probelog.read(self.path, EVENTS, REQUIRED, NUMERIC, BAD)
        self.assertIsNone(why)
        stamp = rows[0]["ts"]                       # exactly `%FT%T`
        self.assertNotIn(".", stamp, "the writer now stamps a fraction: %r"
                         % stamp)

        half = stamp + ".5"
        kept, why = probelog.read(self.path, EVENTS, REQUIRED, NUMERIC, BAD,
                                  until=half)
        self.assertIsNone(why)
        self.assertEqual(len(kept), 1,
                         "an `until` half a second AFTER the row dropped it, "
                         "so the fraction was floored onto the row's second")
        dropped, why = probelog.read(self.path, EVENTS, REQUIRED, NUMERIC, BAD,
                                     since=half)
        self.assertIsNone(why)
        self.assertEqual(dropped, [],
                         "a `since` half a second AFTER the row kept it")

    def test_the_ABSOLUTE_boundary_is_EXACT_to_one_nanosecond(self):
        """NO FLOAT ORACLE. `timestamp()` is a float64 carrying about sixteen
        significant digits while an epoch instant needs nineteen, so the old
        path shifted the boundary by hundreds of nanoseconds. The expected
        value here is composed by integer arithmetic from the calendar, not
        read back from the function under test."""
        value, why = probelog.cut("2026-09-13T19:00:00Z",
                                  probelog.CLOCK_ABSOLUTE)
        self.assertIsNone(why)
        days = (datetime.date(2026, 9, 13) - datetime.date(1970, 1, 1)).days
        self.assertEqual(value, (days * 86400 + 19 * 3600) * 1_000_000_000)
        self.assertIsInstance(value, int, "the cut came back as a float")

        # ONE MICROSECOND, the finest input `fromisoformat` parses, must move
        # the boundary by EXACTLY 1000 nanoseconds.
        finer, why = probelog.cut("2026-09-13T19:00:00.000001Z",
                                  probelog.CLOCK_ABSOLUTE)
        self.assertIsNone(why)
        self.assertEqual(finer - value, 1000)

    def test_a_cut_FINER_than_the_parser_REFUSES_rather_than_truncating(self):  # noqa: VACUOUS_ASSERTION — the refusals are asserted inside a two-spelling loop; the unconditional positive control on the same observable is the dotted six-digit cut resolved OUTSIDE any loop further down, plus the re-derived parser premise asserting fromisoformat still truncates
        """MEASURED: `fromisoformat` accepts any number of fractional digits
        and keeps SIX. `...00.000000001Z` parses to microsecond ZERO — a
        nanosecond cut swallowed whole — so the caller gets a window they did
        not ask for and nothing says so. Silently relocating a boundary is the
        defect this whole function exists to refuse, one unit down."""
        # BOTH DECIMAL MARKS. ISO-8601 allows a COMMA and `fromisoformat`
        # accepts it, so a dot-only search let exactly this case through.
        for spelling in ("2026-09-13T19:00:00.000000001Z",
                         "2026-09-13T19:00:00,000000001+00:00"):
            value, why = probelog.cut(spelling, probelog.CLOCK_ABSOLUTE)
            self.assertIsNone(value,
                              "a sub-microsecond cut was accepted: %r"
                              % spelling)
            self.assertIn("fractional digits", why)
            self.assertIn("6", why)
        # THE PREMISE FOR THE COMMA HALF, re-derived rather than assumed: the
        # parser really does take a comma and really does truncate it.
        comma = datetime.datetime.fromisoformat(
            "2026-09-13T19:00:00,000000001+00:00")
        self.assertEqual(comma.microsecond, 0,
                         "the parser no longer accepts a comma fraction, so "
                         "that half of this refusal guards nothing")
        # THE POSITIVE ON THE SAME DOOR: SIX digits is what the parser keeps,
        # so the refusal is about the overflow and not about fractions.
        days = (datetime.date(2026, 9, 13) - datetime.date(1970, 1, 1)).days
        want = (days * 86400 + 19 * 3600) * 1_000_000_000 + 123456 * 1000
        # UNCONDITIONAL, outside the loop: the same instant spelled with a DOT
        # resolves, so the refusals above are about the digit count and not
        # about a function that refuses fractions. An empty loop below would
        # assert nothing, so this one case stands on its own.
        dotted, none = probelog.cut("2026-09-13T19:00:00.123456Z",
                                    probelog.CLOCK_ABSOLUTE)
        self.assertIsNone(none)
        self.assertEqual(dotted, want)
        for spelling in ("2026-09-13T19:00:00.123456Z",
                         "2026-09-13T19:00:00,123456+00:00"):
            ok, none = probelog.cut(spelling, probelog.CLOCK_ABSOLUTE)
            self.assertIsNone(none, spelling)
            self.assertEqual(ok, want, spelling)
        # AND THE PREMISE IS RE-DERIVED, not trusted: the parser really does
        # truncate, so this refusal is covering a live hazard.
        swallowed = datetime.datetime.fromisoformat(
            "2026-09-13T19:00:00.000000001+00:00")
        self.assertEqual(swallowed.microsecond, 0,
                         "the parser no longer truncates, so this refusal is "
                         "guarding a hazard that has gone")

    def test_NO_CUT_is_not_a_BAD_cut(self):
        self.assertEqual(probelog.cut(None), (None, None))
        self.assertEqual(probelog.cut(""), (None, None))
        self.assertEqual(probelog.cut("   "), (None, None))

    def test_a_REFUSED_cut_returns_NO_ROWS_not_every_row(self):
        """THE OUTCOME THE CLASS IS ABOUT. Handing back the unfiltered rows
        beside a refusal invites the caller to use the rows and drop the
        sentence, which is the same silent wrongness one layer up."""
        for run in ("r1", "r2"):
            self.assertTrue(self.write(RUNG, (("run", run), ("rung", "a"))))
        every, err = probelog.read(self.path, EVENTS, REQUIRED, NUMERIC, BAD)
        self.assertIsNone(err)
        self.assertEqual(len(every), 2, "fixture: the rows were not written")

        rows, why = probelog.read(self.path, EVENTS, REQUIRED, NUMERIC, BAD,
                                  since="2026-09-13T19:00:00Z")
        self.assertEqual(rows, [], "a refused cut returned rows anyway")
        self.assertIn("LOCAL wall clock", why)

    def test_an_ACCEPTED_cut_actually_FILTERS(self):
        """The positive control: the window machinery must be able to keep and
        to drop, or a refusal proves nothing about a working filter."""
        for run in ("r1", "r2"):
            self.assertTrue(self.write(RUNG, (("run", run), ("rung", "a"))))
        rows, why = probelog.read(self.path, EVENTS, REQUIRED, NUMERIC, BAD)
        self.assertIsNone(why)
        stamp = rows[0]["ts"]

        kept, why = probelog.read(self.path, EVENTS, REQUIRED, NUMERIC, BAD,
                                  since="1999-01-01T00:00:00")
        self.assertIsNone(why)
        self.assertEqual(len(kept), 2, "an open-past cut dropped live rows")

        dropped, why = probelog.read(self.path, EVENTS, REQUIRED, NUMERIC,
                                     BAD, since="2999-01-01T00:00:00")
        self.assertIsNone(why)
        self.assertEqual(dropped, [],
                         "a cut in the far future kept rows stamped %s"
                         % stamp)


class SeatTest(ProbeLogBase):
    def test_an_unresolvable_seat_is_unknown_and_never_raises(self):
        """A rejected name is not a reason to lose the record."""
        # POSITIVE CONTROL FIRST: an ordinary name survives this same call
        # unchanged, so "unknown" below is the REJECTION and not the default.
        self.assertEqual(probelog.seat("seat-a"), "seat-a")
        self.assertEqual(probelog.seat("\x01\x02"), "unknown")

    def test_a_seat_rides_the_record_it_was_written_with(self):
        self.assertTrue(self.write(END, [("run", "r1")], known_seat="seat-a"))
        self.assertIn("seat=seat-a", self.lines()[0])


if __name__ == "__main__":
    unittest.main()
