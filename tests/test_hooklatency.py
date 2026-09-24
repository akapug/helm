"""Telemetry owners with private fake roots; no hook installation or network."""
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from helm import (cli, hooklatency as latency, hookoutcome, hookrun, hooks,
                  home, posttoolrun, record, seats, seats_cli, seats_common, seats_rename,
                  toolwhisper)


def _spin():
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        pass                                # the grace alarm lands HERE


class HookLatencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(latency, "path", lambda: str(self.root / "ledger")))
        self.stack.enter_context(patch.object(home, "chat_name", lambda: "fake-seat"))
        self.stack.enter_context(patch.dict(os.environ, {"HELM_NO_TREE_WARNING": "1"}))
        self.addCleanup(lambda: self.assertFalse(latency.active()))

    def pair(self, milliseconds=1, outcome="completed"):
        rows = []
        ticks = iter([1_000_000, 1_000_000 + int(milliseconds * 1_000_000)])
        with patch.object(latency, "append", lambda row: rows.append(dict(row)) or True), \
             patch.object(latency.time, "monotonic_ns", lambda: next(ticks)):
            with latency.event_scope("standalone"):
                latency.bind("fake-session")
                latency.mark(outcome)
        return rows

    def put(self, rows):
        for row in rows:
            self.assertTrue(latency.append(row))

    def test_the_alarm_records_the_frame_it_interrupted(self):
        """task/2462 — THE SIGNAL HANDLER IS HANDED THE ANSWER AND DROPPED IT.

        SIGALRM interrupts whatever was executing and passes that exact frame,
        so "what was this span blocked IN" is answered at the instant the
        budget expires. The span boundary can only say WHICH STAGE was
        innermost, which is the coarser question the per-stage census could
        already answer — and answered misleadingly, because one timed-out hook
        writes a timeout row for every ancestor span.
        """
        def the_place_it_was_stuck():
            return hookrun._where(sys._getframe())

        where = the_place_it_was_stuck()
        self.assertIsNotNone(where)
        # THE FUNCTION THAT WAS RUNNING, NAMED. A stage name could never carry
        # this; that is the whole point of the field.
        self.assertIn("the_place_it_was_stuck", where)
        self.assertIn("test_hooklatency.py", where)
        self.assertNotIn("/", where,
                         "basenames, not paths — this string is written to "
                         "every timeout row and built in a signal handler")
        # BOUNDED, because it is built inside a signal handler.
        self.assertLessEqual(len(where.split(" < ")), hookrun._WHERE_FRAMES)

    def test_a_timed_out_span_carries_WHERE_and_its_ancestors_agree(self):
        """ONE INCIDENT, NOT THREE. Every ancestor span records the same
        frame, which is what makes nesting legible: three timeout rows naming
        ONE place are one hook, where three rows naming three stages read as
        three separate problems."""
        rows = []

        def boom():
            exc = hookrun._Timeout()
            exc.where = hookrun._where(sys._getframe())
            raise exc

        with patch.object(latency, "append",
                          lambda row: rows.append(dict(row)) or True):
            with self.assertRaises(hookrun._Timeout):
                with latency.event_scope("composite"):
                    with latency.stage("delivery"):
                        boom()
        ends = [r for r in rows if r["kind"] == "END"]
        self.assertTrue(ends, "fixture: no END rows, so this arm measures "
                              "nothing")
        timeouts = [r for r in ends if r["outcome"] == "timeout"]
        self.assertGreaterEqual(len(timeouts), 2,
                                "fixture: the timeout did not reach an "
                                "ancestor span, so agreement is untestable")
        self.assertTrue(all("boom" in (r["blocked_in"] or "")
                            for r in timeouts), timeouts)
        self.assertEqual(len({r["blocked_in"] for r in timeouts}), 1,
                         "the ancestors disagree about where the budget "
                         "expired, so the rows cannot be collapsed")

    def test_a_rejected_pair_publishes_no_location(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive is the second half of this arm: a SURVIVING pair publishes its location through the same report(), so the empty list above is the rejection and not a surface that never fills. It carries a different `where` string on purpose, because the rejected rows stay in the store and a shared string could not tell the two answers apart
        """Finding r1 F1: the incident took its evidence one pass BEFORE the
        pair was checked. Two individually schema-valid rows whose fixed
        fields disagree are counted INVALID, and their location was already
        in the incident by then -- asserted to the reader through the JSON
        and the CLI with nothing marking it discarded."""
        rows = self.pair(outcome="timeout")
        start = [r for r in rows if r["kind"] == "START"][0]
        end = [r for r in rows if r["kind"] == "END"][0]
        end["blocked_in"] = "fake.py:1:fake"
        self.assertEqual(start["stage"], end["stage"],
                         "fixture: the pair must START compatible so the "
                         "disagreement below is the only thing this measures")
        end["stage"] = "delivery" if start["stage"] != "delivery" else "record"
        self.put([start, end])
        got = latency.report()
        self.assertGreaterEqual(got["counts"]["invalid"], 1,
                                "fixture: the pair was not rejected, so this "
                                "arm is not about a rejected pair")
        self.assertEqual([b for b in got["blocked_in"]
                          if b["where"] == "fake.py:1:fake"], [],
                         "a pair this same pass counted INVALID put its "
                         "location into the incident")
        # THE CONTROL, unconditional and through the same reader: a pair that
        # SURVIVES publishes its location, so the absence above is the
        # rejection and not a surface that never fills.
        ok_rows = self.pair(outcome="timeout")
        ok_end = [r for r in ok_rows if r["kind"] == "END"][0]
        ok_end["blocked_in"] = "real.py:2:real"
        self.put(ok_rows)
        again = latency.report()
        self.assertEqual([b["where"] for b in again["blocked_in"]],
                         ["real.py:2:real"])

    def test_a_timeout_the_dispatcher_CONSUMES_still_names_its_frame(self):
        """Finding r1 F2: the producer and the consumer of `blocked_in` met on
        one path only. `_span` reads the frame off a timeout that PROPAGATES
        through it -- but the dispatcher's own handler catch consumes the
        timeout INSIDE those spans, so they exit normally, nothing re-raises,
        and every END row said UNKNOWN while the signal handler had already
        captured the frame. This drives the shipped catch, through run_one,
        with a real SIGALRM."""
        rows = []

        def the_uninstrumented_write():
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                pass                        # the alarm lands in THIS frame
            return 0

        spec = {"name": "record", "args": "record", "timeout": 0.05}
        outcome = {}
        with patch.object(latency, "append",
                          lambda row: rows.append(dict(row)) or True), \
                patch("helm.cli.main", lambda args: the_uninstrumented_write()):
            with latency.event_scope("composite"):
                rc = hookrun.run_one(spec, "{}", outcome=outcome)
        self.assertEqual(rc, 0, "the timeout fails OPEN; that law does not move")
        self.assertEqual(outcome.get("status"), hookoutcome.UNCHECKED)
        timeouts = [r for r in rows
                    if r["kind"] == "END" and r["outcome"] == "timeout"]
        self.assertTrue(timeouts, "fixture: the alarm never fired, so nothing "
                                  "here is about a consumed timeout")
        self.assertTrue(all("the_uninstrumented_write" in (r["blocked_in"] or "")
                            for r in timeouts), timeouts)
        # THE CONTROL, unconditional: a handler that RETURNS leaves the field
        # empty, so the frame above came from the timeout and not from every
        # END row carrying some string.
        clean = []
        with patch.object(latency, "append",
                          lambda row: clean.append(dict(row)) or True), \
                patch("helm.cli.main", lambda args: 0):
            with latency.event_scope("composite"):
                hookrun.run_one(spec, "{}", outcome={})
        ends = [r for r in clean if r["kind"] == "END"]
        self.assertTrue(ends, "fixture: the control wrote no END rows")
        self.assertTrue(all(r["blocked_in"] is None for r in ends), ends)

    def test_an_oversized_frame_is_truncated_rather_than_dropped(self):
        """EIGHT FRAMES IS NOT A BYTE BUDGET. The frame walk is bounded by
        COUNT and the row by BYTES, so long basenames and function names
        serialize past MAX_ROW — `append` refuses the row, a timeout the
        signal handler successfully diagnosed is dropped, and the END row
        that would have recorded the drop is refused by the same rule. The
        DIAGNOSTIC is the part that yields."""
        rows = []
        # REAL SHAPES, NOT A BLOB: eight frames whose basenames and function
        # names are long but entirely legal — the shape a deep call through
        # descriptively-named helpers actually produces.
        huge = " < ".join(
            "%s_%d.py:%d:%s_%d"
            % ("a_module_with_a_very_descriptive_and_long_file_name" * 3, i,
               i, "a_function_whose_name_says_exactly_what_it_does" * 3, i)
            for i in range(hookrun._WHERE_FRAMES))
        self.assertGreater(len(huge), latency.MAX_ROW,
                           "fixture: the frame list is not big enough to "
                           "overflow a row, so this arm measures nothing")

        def boom():
            exc = hookrun._Timeout()
            exc.where = huge
            raise exc

        with patch.object(latency, "append",
                          lambda row: rows.append(dict(row)) or True):
            with self.assertRaises(hookrun._Timeout):
                with latency.event_scope("composite"):
                    with latency.stage("delivery"):
                        boom()
        ends = [r for r in rows if r["kind"] == "END"
                and r["outcome"] == "timeout"]
        self.assertTrue(ends, "fixture: no timeout END rows")
        for r in ends:
            self.assertLessEqual(len(r["blocked_in"] or ""),
                                 latency.MAX_BLOCKED_IN)
            self.assertTrue((r["blocked_in"] or "").endswith("..."),
                            "a truncated frame list must say so")
            # AND THE ROW STILL FITS, which is the whole point: the real
            # `append` would refuse it otherwise and the timeout would be
            # censored.
            self.assertTrue(latency.append(r))
        # THE CONTROL, unconditional: an ordinary frame list is carried
        # WHOLE and unmarked, so the truncation is about size and not a
        # renderer that always cuts.
        small = []
        def ok_boom():
            exc = hookrun._Timeout()
            exc.where = hookrun._where(sys._getframe())
            raise exc

        with patch.object(latency, "append",
                          lambda row: small.append(dict(row)) or True):
            with self.assertRaises(hookrun._Timeout):
                with latency.event_scope("composite"):
                    with latency.stage("delivery"):
                        ok_boom()
        kept = [r for r in small if r["kind"] == "END"
                and r["outcome"] == "timeout"]
        self.assertTrue(kept)
        self.assertFalse(any((r["blocked_in"] or "").endswith("...")
                             for r in kept), kept)
        self.assertTrue(all("ok_boom" in (r["blocked_in"] or "")
                            for r in kept), kept)

    def _ledger_ends(self):
        path = Path(latency.path())
        if not path.exists():
            return []
        return [r for r in (json.loads(line) for line in
                            path.read_text().splitlines() if line.strip())
                if r["kind"] == "END"]

    def test_a_non_ascii_frame_is_fitted_by_its_encoded_bytes(self):  # noqa: VACUOUS_ASSERTION — the loop body runs over rows whose count is asserted to be exactly two on the line before it, and the control's whole-frame equality is unconditional
        """task/2462 r3: 512 CHARACTERS IS NOT A 512-BYTE BUDGET.
        The wire is ASCII JSON, so a legal non-ASCII filename or function name
        costs six bytes a code point and an astral one twelve; a character
        bound lets the row past MAX_ROW and `append` refuses it, censoring the
        timeout. Driven through the REAL writer and the REAL append, and read
        back from the ledger file, so a refused row is a missing row."""
        wide = "\u00e9" * 300 + "\U0001d538" * 300
        self.assertGreater(len(json.dumps(wide)), latency.MAX_ROW,
                           "fixture: the frame is not wide enough on the wire "
                           "to overflow a row, so this arm measures nothing")
        self.assertLess(len(wide), latency.MAX_ROW,
                        "fixture: the frame must be SHORT in characters, or a "
                        "character bound would also catch it")

        def boom(where):
            exc = hookrun._Timeout()
            exc.where = where
            raise exc

        with self.assertRaises(hookrun._Timeout):
            with latency.event_scope("composite"):
                with latency.stage("delivery"):
                    boom(wide)
        ends = [r for r in self._ledger_ends() if r["outcome"] == "timeout"]
        self.assertEqual(len(ends), 2, "the timeout END rows were refused, so "
                         "the diagnosed timeout was censored: %r" % ends)
        for r in ends:
            self.assertTrue(r["blocked_in"].endswith("..."), r["blocked_in"])
            self.assertLessEqual(len(json.dumps(r["blocked_in"])) - 2,
                                 latency.MAX_BLOCKED_IN)
            self.assertLessEqual(len(latency._wire(r)), latency.MAX_ROW)
            self.assertTrue(wide.startswith(r["blocked_in"][:-3]),
                            "the cut must be a prefix of the real frame")
        # THE CONTROL, unconditional: a short non-ASCII frame is carried WHOLE,
        # so the cut above is about wire size and not about the alphabet.
        os.unlink(latency.path())
        short = "\u00e9" * 20 + "\U0001d538" * 20
        with self.assertRaises(hookrun._Timeout):
            with latency.event_scope("composite"):
                with latency.stage("delivery"):
                    boom(short)
        kept = [r for r in self._ledger_ends() if r["outcome"] == "timeout"]
        self.assertEqual([r["blocked_in"] for r in kept], [short, short])

    def test_the_diagnostic_yields_to_the_ROW_not_only_to_its_own_cap(self):
        """The field cap is one of two limits; the other is what is left of
        MAX_ROW after every other field. With the field cap lifted out of the
        way, the row bound alone must still keep the row writable."""
        huge = "x" * (4 * latency.MAX_ROW)
        with patch.object(latency, "MAX_BLOCKED_IN", 10 * latency.MAX_ROW):
            with self.assertRaises(hookrun._Timeout):
                with latency.event_scope("composite"):
                    exc = hookrun._Timeout()
                    exc.where = huge
                    raise exc
        ends = [r for r in self._ledger_ends() if r["outcome"] == "timeout"]
        self.assertEqual(len(ends), 1, ends)
        self.assertTrue(ends[0]["blocked_in"].endswith("..."))
        self.assertLessEqual(len(latency._wire(ends[0])), latency.MAX_ROW)
        # AND IT USES THE ROOM: a fit that threw the whole field away would
        # also be writable, and would name nothing.
        self.assertGreater(len(latency._wire(ends[0])), latency.MAX_ROW - 6)

    def _slow(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            pass                            # the grace alarm lands HERE
        return None

    def test_a_timeout_posttoolrun_CONSUMES_names_its_frame_and_stays_a_timeout(self):
        """task/2462 r3 F1: `_run_one` was the only consumer that
        handed a caught timeout's frame to the observer. The PostToolUse
        composite catches the same `_Timeout` from its own grace alarm, so the
        event span exited with no location and `failure` marked it unchecked.
        A real SIGALRM from the shipped `_grace_deadline`, through `lookup`."""
        ev = posttoolrun._Event()
        token = posttoolrun._CURRENT.set(ev)
        self.addCleanup(posttoolrun._CURRENT.reset, token)
        with contextlib.redirect_stderr(io.StringIO()):
            with latency.event_scope("composite"):
                got = ev.lookup("record", self._slow)
        self.assertIsNone(got)
        ends = self._ledger_ends()
        event = [r for r in ends if r["stage"] == "event"]
        self.assertEqual(len(event), 1, ends)
        self.assertEqual(event[0]["outcome"], "timeout",
                         "a KNOWN timeout was buried as %r" % event[0]["outcome"])
        self.assertIn("_slow", event[0]["blocked_in"] or "", event[0])
        # THE CONTROL, unconditional: a getter that answers leaves the event
        # without a timeout or a location.
        os.unlink(latency.path())
        with latency.event_scope("composite"):
            self.assertEqual(ev.lookup("record", lambda: "spec"), "spec")
        event = [r for r in self._ledger_ends() if r["stage"] == "event"]
        self.assertEqual([(r["outcome"], r["blocked_in"]) for r in event],
                         [("completed", None)])

    def test_an_alarm_publication_timeout_is_a_timeout_with_its_frame(self):  # noqa: VACUOUS_ASSERTION, ORPHANED_MOCK — the unconditional positive is the first half (a timed-out emit leaves the event END timeout WITH its frame); the two spec doubles are reached as the getters `run` hands to `_Event.lookup`, which the modeled graph does not follow
        """The same door at the fallback publication, which no latency stage
        surrounds: a timed-out emit must leave the event a TIMEOUT that names
        its frame, never an UNCHECKED with no location. Driven through `run`
        with the shipped grace alarm."""
        def refused():
            raise RuntimeError("no declaration")

        with patch.object(posttoolrun, "_record_spec", refused), \
                patch.object(posttoolrun, "_delivery_spec", lambda: None), \
                patch.object(posttoolrun._Event, "emit",
                             lambda self, whisper=False: _spin()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(posttoolrun.run(payload="{}"), 0)
        event = [r for r in self._ledger_ends() if r["stage"] == "event"]
        self.assertEqual(len(event), 1, self._ledger_ends())
        self.assertEqual(event[0]["outcome"], "timeout")
        self.assertIn("_spin", event[0]["blocked_in"] or "", event[0])
        # THE CONTROL: an emit that returns leaves the refused declaration as
        # what it is -- unchecked, with no location.
        os.unlink(latency.path())
        with patch.object(posttoolrun, "_record_spec", refused), \
                patch.object(posttoolrun, "_delivery_spec", lambda: None), \
                patch.object(posttoolrun._Event, "emit",
                             lambda self, whisper=False: None), \
                contextlib.redirect_stderr(io.StringIO()):
            posttoolrun.run(payload="{}")
        event = [r for r in self._ledger_ends() if r["stage"] == "event"]
        # Whatever the refused declaration marked it, it is not a timeout and
        # it names no frame.
        self.assertEqual(len(event), 1, self._ledger_ends())
        self.assertNotEqual(event[0]["outcome"], "timeout", event[0])
        self.assertIsNone(event[0]["blocked_in"], event[0])

    def test_two_deadlines_in_one_event_are_two_timeouts(self):
        """task/2462 r3 F3: the report keyed incidents by EVENT so
        an ancestor's echo would not triple one timeout -- and so two handlers
        that each exhausted their own budget in one shared frame read as one.
        The rows come from the real writer; the count is the innermost
        timed-out spans."""
        where = "shared.py:7:the_shared_frame"
        with latency.event_scope("composite"):
            for stage in ("record", "delivery"):
                with latency.stage(stage):
                    latency.mark("timeout")
                    latency.blocked_in(where)
        got = [b for b in latency.report()["blocked_in"] if b["where"] == where]
        self.assertEqual(got, [{"where": where, "events": 1, "timeouts": 2}])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            latency.cmd([])
        self.assertIn("2 timeouts in 1 event  " + where, out.getvalue())
        # THE CONTROL, unconditional: ONE handler's timeout, echoed by its
        # event span, is one timeout -- the echo the event key existed for.
        os.unlink(latency.path())
        with latency.event_scope("composite"):
            with latency.stage("record"):
                latency.mark("timeout")
                latency.blocked_in(where)
        got = [b for b in latency.report()["blocked_in"] if b["where"] == where]
        self.assertEqual(got, [{"where": where, "events": 1, "timeouts": 1}])

    def test_the_v1_rows_already_on_disk_stay_READABLE(self):
        """A NEW FIELD IS A NEW VERSION OR IT INVALIDATES THE HISTORY. The key
        set is exact in BOTH directions, so adding `blocked_in` to one frozen
        set would have rejected every row already written — and the reader
        reports rejects as `invalid`, so the loss arrives as a counter nobody
        reads rather than as an error."""
        rows = self.pair()
        legacy = [dict(r) for r in rows]
        for r in legacy:
            del r["blocked_in"]
            r["schema"] = 1
        self.put(legacy)
        got = latency.report()
        self.assertEqual(got["counts"]["invalid"], 0,
                         "the v1 rows on disk were rejected by the v2 reader")
        self.assertGreaterEqual(got["counts"]["starts"], 1)
        # THE MUST-HIT: a row whose key set matches NEITHER version is still
        # refused, so the acceptance above is about versioning and not about a
        # validator that stopped checking. `append` REFUSES at the WRITE door
        # — which is why the read-side `invalid` counter stays 0 here and why
        # a non-zero one on a live ledger is about rows some OTHER writer put
        # there, not about these.
        bogus = dict(legacy[0])
        bogus["unexpected"] = 1
        self.assertFalse(latency.append(bogus),
                         "a row matching neither schema was accepted")
        self.assertEqual(latency.report()["counts"]["invalid"], 0)

    def test_a_window_on_the_wrong_clock_REFUSES_instead_of_reporting_zero(self):
        """task/2458 — the census door a seat would otherwise hand-roll.

        These rows carry `time_ns`, an ABSOLUTE instant; probelog's files
        carry local wall-clock text. A seat reads both in one sitting with
        interchangeable-looking cut strings, and the wrong pairing returns a
        clean, believable zero rather than an error.
        """
        rows = self.pair()
        self.put(rows)

        # FIXTURE FIRST: unwindowed, this ledger reports something. Without
        # this the refusal below is indistinguishable from an empty file.
        full = latency.report()
        self.assertNotIn("refused", full)
        self.assertGreaterEqual(full["counts"]["starts"], 1,
                                "fixture: nothing was recorded, so the window "
                                "arms below measure an empty store")

        naive = latency.report(since="2026-09-13T12:00:00")
        self.assertIn("refused", naive)
        self.assertIn("ABSOLUTE", naive["refused"])
        self.assertEqual(naive["populations"], [],
                         "a refused window still published populations")

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = latency.cmd(["--since", "2026-09-13T12:00:00"])
        self.assertEqual(rc, 2, "the verb reported a clean census at rc 0 on "
                                "a cut it could not honour")
        self.assertIn("REFUSED", err.getvalue())

    def test_an_ABSOLUTE_window_is_accepted_and_actually_bounds(self):
        """THE POSITIVE CONTROL on the same door, in both directions: a zoned
        cut resolves, keeps what is inside and drops what is outside."""
        rows = self.pair()
        self.put(rows)
        stamp = max(r["time_ns"] for r in rows)
        before = latency.report(since="1999-01-01T00:00:00Z")
        self.assertNotIn("refused", before)
        self.assertGreaterEqual(before["counts"]["starts"], 1,
                                "an open-past window dropped live rows")

        after = latency.report(since="2999-01-01T00:00:00Z")
        self.assertNotIn("refused", after)
        self.assertEqual(after["counts"]["starts"], 0,
                         "a window in the far future kept rows stamped %s"
                         % stamp)

    def test_a_span_WHOLLY_INSIDE_reads_the_same_through_BOTH_cuts(self):
        """A window that contains a span entirely must not change one number
        about it.

        Without this, every window arm here is about what the filter DROPS and
        none is about what it must leave alone — and a filter that quietly
        perturbed the spans it kept would pass all of them.
        """
        rows = self.pair()
        self.put(rows)
        stamp = max(r["time_ns"] for r in rows)
        one_second = 1_000_000_000
        lo = self._iso(min(r["time_ns"] for r in rows) - one_second)
        hi = self._iso(stamp + one_second)

        unbounded = latency.report()
        windowed = latency.report(since=lo, until=hi)
        self.assertNotIn("refused", windowed)
        # UNCONDITIONAL POSITIVE ON THE SAME OBJECT: two EMPTY reports are
        # equal too, so the equality below says nothing until the windowed
        # census is known to contain the span.
        self.assertGreaterEqual(windowed["counts"]["starts"], 1,
                                "the window dropped the span it contains, so "
                                "the equality below compares two empty "
                                "censuses")
        self.assertEqual(windowed["counts"], unbounded["counts"],
                         "a window containing the span whole changed the "
                         "counts")
        self.assertEqual(windowed["populations"], unbounded["populations"],
                         "a window containing the span whole changed a "
                         "population")

    @staticmethod
    def _iso(time_ns):
        """An ABSOLUTE cut string for an epoch-ns instant, composed by integer
        arithmetic — never `fromtimestamp(ns / 1e9)`, which is the float oracle
        this lane exists to remove."""
        import datetime as _dt
        whole, micros = divmod(time_ns // 1000, 1_000_000)
        return (_dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)
                + _dt.timedelta(seconds=whole, microseconds=micros)).isoformat()

    def test_the_RENDERED_WINDOW_LINE_names_the_right_counter(self):
        """THE DOCSTRING AND THE PRINTED LINE ARE TWO SURFACES and only one of
        them reaches an operator. The line said a straddling span is counted
        as an ORPHAN; a START-only straddler is CENSORED, and orphan_ends is
        the END-only case."""
        rows = self.pair()
        self.put(rows)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = latency.cmd(["--since", "1999-01-01T00:00:00Z"])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("Window", text, "the window line was not rendered at all")
        self.assertIn("CENSORED", text,
                      "the rendered line does not name the START-only "
                      "counter: %r" % text.splitlines()[:1])
        self.assertNotIn("counted as an orphan, not as a completed span", text,
                         "the old mislabel came back")

    def test_EVERY_PUBLIC_SYNOPSIS_names_the_window_flags(self):
        """THREE SITES, NOT ONE, and I had cured a different one than the
        route a reader actually hits.

        `helm --help` renders cli.py's verb-help entry, `helm hooks --help`
        short-circuits through it, hooks.py carries the subcommand usage, and
        docs/VERBS.md is the written reference. A flag added to one spelling
        and not the others is a synopsis that lies by omission, and a reader
        who checks the one they happen to hit learns the wrong surface.
        """
        from helm import cli, hooks
        root = os.path.dirname(os.path.dirname(os.path.abspath(hooks.__file__)))
        verbs = os.path.join(root, "docs", "VERBS.md")
        with io.open(verbs, encoding="utf-8") as fh:
            doc = fh.read()
        # UNCONDITIONAL: the loops below assert nothing if this table is
        # empty or loses a site, and losing a site is exactly the regression
        # this arm exists to catch.
        sites = {
            "cli.py verb help": cli._VERB_HELP["hooks"],
            "hooks.py usage": hooks._USAGE,
            "hooks latency usage": latency.cmd.__doc__ or "",
            "docs/VERBS.md": doc,
        }
        self.assertEqual(len(sites), 4,
                         "the synopsis table changed size, so this arm is no "
                         "longer checking the population it was written for")
        # MUST-HIT: every site must mention the verb at all, or an empty
        # string would satisfy the flag check below by containing nothing.
        for name, text in sites.items():
            if name == "hooks latency usage":
                continue
            # SPELLED PER SITE, deliberately: cli.py renders the subverb
            # inside a `hooks [a|b|latency ...]` alternation while hooks.py
            # and the docs write it out. The must-hit asks that each site
            # mentions the subverb AT ALL, so an empty string cannot satisfy
            # the flag assertions below.
            self.assertIn("latency", text,
                          "%s does not document the verb at all" % name)
        for name, text in sites.items():
            if name == "hooks latency usage":
                continue
            for flag in ("--since", "--until"):
                self.assertIn(flag, text,
                              "%s does not name %s, so a reader who checks "
                              "that surface learns the verb has no window"
                              % (name, flag))

    def test_native_report_rejects_unrepresentable_numeric_rows_without_mutation(self):
        cases = []
        for field in ("wall_ms", "monotonic_ns", "time_ns", "sequence", "observed_drops"):
            cases.extend((field, value) for value in (10**500, 10**100, 1 << 63, -1, True))
            if field != "wall_ms": cases.append((field, 1.5))
        cases.extend(("wall_ms", value) for value in (float("nan"), float("inf"), float("-inf")))
        for index, (field, value) in enumerate(cases):
            with self.subTest(field=field, value=value):
                estate = self.root / str(index)
                estate.mkdir()
                with patch.object(latency, "path", lambda: str(estate / "ledger")):
                    start, end = self.pair()
                    self.put([start])
                    end[field] = value
                    wire = (json.dumps(end) + "\n").encode()
                    self.assertLessEqual(len(wire), latency.MAX_ROW)
                    with open(latency.path(), "ab") as stream: stream.write(wire)
                    self.put(self.pair(7))
                    before = {p.name: p.read_bytes() for p in estate.iterdir()}
                    out = io.StringIO()
                    with contextlib.redirect_stdout(out):
                        self.assertEqual(0, cli.main(["hooks", "latency", "--json"]))
                    result = json.loads(out.getvalue())
                    self.assertEqual(1, result["counts"]["invalid"])
                    self.assertEqual(1, result["counts"]["censored"])
                    self.assertEqual(1, result["counts"]["completed"])
                    self.assertEqual(0, result["counts"]["malformed"])
                    completed = next(g for g in result["populations"] if g["completed"])
                    self.assertEqual(7, completed["p95_ms"])
                    self.assertEqual(before, {p.name: p.read_bytes() for p in estate.iterdir()})
                    self.assertFalse(latency.append(end))
                    self.assertEqual(before, {p.name: p.read_bytes() for p in estate.iterdir()})

    def test_native_report_accepts_finite_boundary_and_counter_aggregation(self):
        limit = (1 << 63) - 1
        for _ in range(2):
            start, end = self.pair()
            start.update(monotonic_ns=0, time_ns=0, sequence=limit - 1, observed_drops=limit - 1)
            end.update(monotonic_ns=limit, time_ns=limit, sequence=limit, observed_drops=limit,
                       wall_ms=limit / 1_000_000)
            self.put([start, end])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(0, cli.main(["hooks", "latency", "--json"]))
        result = json.loads(out.getvalue())
        self.assertEqual(0, result["counts"]["invalid"])
        self.assertEqual(2, result["counts"]["completed"])
        self.assertEqual(limit / 1_000_000, result["populations"][0]["p95_ms"])
        self.assertEqual(limit / 3.6e12, result["coverage"]["retained_time_range_hours"])
        self.assertEqual(2 * limit, result["coverage"]["observed_drops_lower_bound"])

    def test_unrepresentable_clock_samples_are_unavailable_not_zero_success(self):
        with patch.object(latency.time, "monotonic_ns", return_value=10**500), \
             patch.object(latency.time, "time_ns", return_value=10**500):
            with latency.event_scope("standalone"): pass
        result = latency.report()
        self.assertEqual(1, result["counts"]["invalid"])
        self.assertEqual(0, result["counts"]["completed"])
        self.assertIsNone(result["coverage"]["retained_time_range_hours"])
        rows = [json.loads(line) for line in Path(latency.path()).read_text().splitlines() if line.strip()]
        self.assertTrue(rows)
        self.assertTrue(all(row["monotonic_ns"] is None and row["time_ns"] is None for row in rows))

    def test_numeric_validation_preserves_cancellation_and_readonly_state(self):
        self.put(self.pair())
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        sentinel = KeyboardInterrupt("numeric validation")
        with patch.object(latency.math, "isfinite", side_effect=sentinel):
            with self.assertRaises(KeyboardInterrupt) as caught:
                cli.main(["hooks", "latency", "--json"])
        self.assertIs(sentinel, caught.exception)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    def test_nearest_rank_and_unknown_launch_population(self):
        for n in range(1, 21):
            self.put(self.pair(n))
        result = latency.report()
        group = result["populations"][0]
        self.assertEqual((20, 10, 19), (group["completed"], group["p50_ms"], group["p95_ms"]))
        for key in ("launch_attempts", "startup_ms", "filesystem_wall_bound"):
            self.assertIsNone(result["coverage"][key])
        self.assertEqual("unknown", result["coverage"]["unreported_loss"])
        self.assertFalse(result["coverage"]["historical_census_complete"])

    def test_native_empty_report_never_creates_state(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(0, cli.main(["hooks", "latency", "--json"]))
        result = json.loads(out.getvalue())
        self.assertEqual([], result["populations"])
        self.assertIsNone(result["coverage"]["retained_time_range_hours"])
        self.assertEqual("uncertain", result["coverage"]["snapshot"])
        self.assertEqual([], list(self.root.iterdir()))

    def test_native_parser_rejects_junk_before_help(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(2, hooks.cmd_hooks(["latency", "--unknown", "--help"]))
            self.assertEqual(0, hooks.cmd_hooks(["latency", "--help"]))
        self.assertEqual([], list(self.root.iterdir()))

    def test_cross_generation_pairs_are_joined(self):
        start, end = self.pair(7)
        self.put([start])
        os.replace(latency.path(), latency.path() + ".1")
        self.put([end])
        result = latency.report()
        self.assertEqual(1, result["counts"]["completed"])
        self.assertEqual(7, result["populations"][0]["p95_ms"])
        self.assertEqual("coherent-for-cooperating-writers", result["coverage"]["snapshot"])

    def test_censored_is_not_running_or_completed(self):
        start, _ = self.pair()
        _, end = self.pair()
        self.put([start, end])
        result = latency.report()
        self.assertEqual(1, result["counts"]["censored"])
        self.assertEqual(1, result["counts"]["orphan_ends"])
        self.assertEqual(0, result["counts"]["completed"])
        self.assertIn("completion-unobserved", json.dumps(result))
        self.assertNotIn("running", json.dumps(result))

    def test_timeout_cancellation_semantics_excluded_from_quantiles(self):
        for outcome in ("timeout", "cancelled", "unchecked", "skipped", "exception", "failed-open"):
            self.put(self.pair(outcome=outcome))
        result = latency.report()
        self.assertEqual(0, result["counts"]["completed"])
        self.assertEqual(1, result["counts"]["timeout"])
        self.assertEqual(1, result["counts"]["cancellation"])
        self.assertTrue(all(row["p95_ms"] is None for row in result["populations"]))

    def test_duplicates_conflicts_and_invalid_rows_cannot_measure(self):
        start, end = self.pair()
        self.put([start, end, start, dict(end, wall_ms=999)])
        with open(latency.path(), "ab") as stream:
            stream.write(b'{broken}\n{"schema":1,"schema":1}\n')
            stream.write(json.dumps(dict(start, outcome=[])).encode() + b'\n')
        result = latency.report()
        self.assertEqual(1, result["counts"]["duplicates"])
        self.assertEqual(1, result["counts"]["conflicts"])
        self.assertEqual(2, result["counts"]["malformed"])
        self.assertEqual(0, result["counts"]["completed"])
        self.assertGreaterEqual(result["counts"]["invalid"], 2)

    def test_schema_rejects_sensitive_or_unbounded_fields(self):
        row = self.pair()[0]
        for key in ("payload", "command", "cwd", "exception", "url"):
            self.assertFalse(latency.append(dict(row, **{key: "private"})))
        for key in ("seat", "session"):
            self.assertFalse(latency.append(dict(row, **{key: "x" * 129})))
        self.assertEqual([], list(self.root.iterdir()))

    def test_clock_fault_is_not_zero_duration(self):
        with patch.object(latency.time, "monotonic_ns", side_effect=OSError("clock")):
            with latency.event_scope("standalone"):
                pass
        result = latency.report()
        self.assertEqual(1, result["counts"]["invalid"])
        self.assertEqual(0, result["counts"]["completed"])
        self.assertIsNone(result["populations"][0]["p50_ms"])

    def test_backward_and_mismatched_clocks_are_invalid(self):
        for mode in ("backward", "mismatch", "missing"):
            start, end = self.pair()
            if mode == "backward": end["monotonic_ns"] = start["monotonic_ns"] - 1
            if mode == "mismatch": end["wall_ms"] = 99
            if mode == "missing": end["wall_ms"] = None
            self.put([start, end])
        self.assertEqual(3, latency.report()["counts"]["invalid"])
        self.assertEqual(0, latency.report()["counts"]["completed"])

    def test_shared_reader_lock_causes_observed_writer_drop(self):
        with latency.event_scope("composite"):
            fd = os.open(latency.path() + ".lock", os.O_RDONLY)
            try:
                fcntl.flock(fd, fcntl.LOCK_SH)
                with latency.stage("delivery"):
                    os.close(fd)
                    fd = None
            finally:
                if fd is not None: os.close(fd)
        result = latency.report()
        self.assertEqual(1, result["coverage"]["observed_drops_lower_bound"])
        self.assertEqual(1, result["counts"]["orphan_ends"])
        self.assertEqual(1, result["counts"]["completed"])
        self.assertEqual("unknown", result["coverage"]["unreported_loss"])

    def test_reader_contention_is_explicitly_uncertain(self):
        self.put(self.pair())
        with open(latency.path() + ".lock", "r") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            self.assertEqual("uncertain", latency.report()["coverage"]["snapshot"])

    def test_readonly_snapshot_releases_before_parsing(self):
        self.put(self.pair())
        original = latency._valid
        hits = []
        def valid(row):
            with open(latency.path() + ".lock", "r") as held:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                hits.append(True)
            return original(row)
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        with patch.object(latency, "_valid", valid), \
             patch.object(os, "makedirs", side_effect=AssertionError("reader created")), \
             patch.object(os, "replace", side_effect=AssertionError("reader rotated")):
            self.assertEqual(1, latency.report()["counts"]["completed"])
        self.assertTrue(hits)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    def test_rotation_bounds_storage_not_historical_census(self):
        with patch.object(latency, "MAX_BYTES", 1600):
            for _ in range(10): self.put(self.pair())
            self.assertEqual(4, len(list(self.root.iterdir())))
            self.assertTrue(all(p.stat().st_size <= 1600 for p in self.root.iterdir()))
            result = latency.report()
        self.assertLess(result["coverage"]["registered_events"], 10)
        self.assertFalse(result["coverage"]["historical_census_complete"])

    def test_short_write_leaves_orphan_not_successful_measurement(self):
        original = os.write
        writes = []
        def short(fd, wire):
            writes.append(True)
            return original(fd, wire[:20] if len(writes) == 1 else wire)
        with patch.object(os, "write", short):
            with latency.event_scope("standalone"): pass
        result = latency.report()
        self.assertEqual(1, result["counts"]["malformed"])
        self.assertEqual(1, result["counts"]["orphan_ends"])
        self.assertEqual(0, result["counts"]["completed"])
        self.assertEqual(1, result["coverage"]["observed_drops_lower_bound"])

    def test_logger_error_and_cancellation_cleanup(self):
        with latency.event_scope("composite"):
            with patch.object(latency, "append", side_effect=OSError("private")):
                with latency.stage("record"): pass
        self.assertEqual(2, latency.report()["coverage"]["observed_drops_lower_bound"])
        sentinel = KeyboardInterrupt("cancel")
        with self.assertRaises(KeyboardInterrupt) as caught:
            with patch.object(latency, "append", side_effect=sentinel):
                with latency.event_scope("composite"): pass
        self.assertIs(sentinel, caught.exception)
        self.assertFalse(latency.active())

    def test_primary_cancellation_survives_logger_cancellation_on_unwind(self):
        primary, secondary = KeyboardInterrupt("primary"), KeyboardInterrupt("logger")
        original = latency.append
        def append(row):
            if row["stage"] == "record" and row["kind"] == "END": raise secondary
            return original(row)
        with self.assertRaises(KeyboardInterrupt) as caught:
            with patch.object(latency, "append", append):
                with latency.event_scope("composite"):
                    with latency.stage("record"): raise primary
        self.assertIs(primary, caught.exception)
        self.assertIs(secondary, caught.exception.__cause__)
        self.assertFalse(latency.active())

    def test_acquisition_end_cancellation_releases_before_body(self):
        original = latency.append
        sentinel = KeyboardInterrupt("acquired")
        body = []
        def append(row):
            if row["stage"] == "lock-seats" and row["kind"] == "END":
                self.assertEqual("acquired", row["outcome"])
                raise sentinel
            return original(row)
        target = str(self.root / "owned.lock")
        with self.assertRaises(KeyboardInterrupt) as caught:
            with patch.object(latency, "append", append):
                with latency.event_scope("composite"):
                    with seats_common._flocked(target): body.append(True)
        self.assertIs(sentinel, caught.exception)
        self.assertEqual([], body)
        with open(target, "a") as check:
            fcntl.flock(check, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_a_budgeted_wait_skips_a_held_lock_instead_of_being_killed(self):  # noqa: VACUOUS_ASSERTION — the control is unconditional and on the SAME fd and the same call: release the holder and the identical acquisition returns True
        """THE OWNER'S SYMPTOM, AT ITS MEASURED CAUSE.

        MEASURED IN PRODUCTION on this host before this arm existed: five
        consecutive `PostToolUse delivery: handler timed out after 2s — event
        ALLOWED and UNCHECKED` lines, every one with `blocked_in` naming
        `hooklatency.flock < proxywatch.delivery_state_guard`, each having
        waited 1.6-1.9s of a 2s budget for a FLEET-WIDE lock and then been
        killed before reading a single room. The store was not slow and the
        hook did not fail: it queued behind other seats.

        An unbounded `LOCK_EX` inside a bounded budget has exactly one ending.
        With a deadline the wait becomes a value the caller can act on.
        """
        target = self.root / "budgeted.lock"
        holder = os.open(str(target), os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, holder)
        fcntl.flock(holder, fcntl.LOCK_EX)
        fd = os.open(str(target), os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, fd)

        began = time.monotonic()
        held = latency.flock(fd, fcntl.LOCK_EX, "lock-proxywatch",
                             deadline=time.monotonic() + 0.20)
        waited = time.monotonic() - began
        self.assertIs(held, False, "a held lock was reported as acquired")
        self.assertGreaterEqual(waited, 0.15,
                                "the wait did not use its deadline at all")
        self.assertLess(waited, 2.0, "the bounded wait did not end")

        # THE CONTROL, on the same fd and the same call: release the holder and
        # the identical acquisition succeeds. Without it, a `flock` that always
        # answered False would pass every assertion above.
        fcntl.flock(holder, fcntl.LOCK_UN)
        self.assertIs(latency.flock(fd, fcntl.LOCK_EX, "lock-proxywatch",
                                    deadline=time.monotonic() + 0.20), True)
        fcntl.flock(fd, fcntl.LOCK_UN)

    def test_a_skipped_acquisition_still_closes_its_span(self):
        """AN INSTRUMENT THAT DROPS THE ROW REPORTS THE UNCONTENDED WORLD.

        The first cut of the bounded wait set the span outcome to `busy`, which
        is not in `OUTCOMES` — so `_valid` rejected the END row and the probe
        read twelve STARTs against six ENDs: the six acquisitions the change
        exists to make visible were exactly the ones that vanished.
        """
        target = self.root / "span.lock"
        holder = os.open(str(target), os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, holder)
        fcntl.flock(holder, fcntl.LOCK_EX)
        fd = os.open(str(target), os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, fd)
        rows = []
        with patch.object(latency, "append", rows.append):
            with latency.event_scope("composite"):
                latency.flock(fd, fcntl.LOCK_EX, "lock-proxywatch",
                              deadline=time.monotonic() + 0.05)
        lock = [r for r in rows if r["stage"] == "lock-proxywatch"]
        self.assertEqual([r["kind"] for r in lock], ["START", "END"],
                         "the skipped acquisition left an unclosed span")
        self.assertEqual(lock[1]["outcome"], "skipped")
        self.assertIn(lock[1]["outcome"], latency.OUTCOMES,
                      "MUST-HIT: the outcome is outside the schema's own "
                      "vocabulary, so `_valid` drops the row that proves this")

    def test_remaining_budget_is_the_alarm_not_a_guess(self):
        self.assertIsNone(latency.remaining_budget(),
                          "MUST-HIT: an alarm is armed before this arm starts, "
                          "so the reading below is not this arm's")
        signal.setitimer(signal.ITIMER_REAL, 5.0)
        try:
            left = latency.remaining_budget()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
        self.assertIsNotNone(left)
        self.assertGreater(left, 4.0)
        self.assertLessEqual(left, 5.0)
        self.assertIsNone(latency.remaining_budget(),
                          "a disarmed alarm still reported a budget")

    def test_an_unbounded_caller_is_byte_for_byte_unchanged(self):  # noqa: VACUOUS_ASSERTION — the first assertion is an unconditional positive control on the same observable: an unbounded caller against a free lock is handed True
        """OPT-IN, and this is the control that says so. `record()` and the
        chat writer are not retried by a next tool call: a silent skip there
        would lose the write this lock exists to order."""
        from helm import proxywatch
        with patch.object(proxywatch, "_state_path",
                          lambda: str(self.root / "state")):
            with proxywatch.delivery_state_guard() as held:
                self.assertIs(held, True)
            target = self.root / ".proxywatch-state.lock"
            holder = os.open(str(target), os.O_CREAT | os.O_RDWR, 0o600)
            self.addCleanup(os.close, holder)
            fcntl.flock(holder, fcntl.LOCK_EX)
            with proxywatch.delivery_state_guard(wait=0.05) as held:
                self.assertIs(held, False,
                              "a bounded caller took a lock another process "
                              "holds")
            fcntl.flock(holder, fcntl.LOCK_UN)

    def test_a_busy_lock_delivers_nothing_and_changes_nothing(self):  # noqa: VACUOUS_ASSERTION — the control at the end is unconditional and on the same observable: with the holder released the identical guard yields the real pause decision, so the None above discriminates contention and not a guard that always declines
        """THE CONSEQUENCE AT THE DELIVERY DOOR. BUSY is truthy, so every
        delivery site's existing `if pause: return None` already declines —
        the bounded wait needed no new branch, only a name for the state."""
        from helm import proxywatch, seats_delivery, seats_identity
        with patch.object(proxywatch, "_state_path",
                          lambda: str(self.root / "state")):
            target = self.root / ".proxywatch-state.lock"
            os.makedirs(str(self.root), exist_ok=True)
            holder = os.open(str(target), os.O_CREAT | os.O_RDWR, 0o600)
            self.addCleanup(os.close, holder)
            fcntl.flock(holder, fcntl.LOCK_EX)
            with patch.object(proxywatch, "delivery_wait", lambda: 0.05):
                with patch.object(seats_identity, "_delivery_pause",
                                  side_effect=AssertionError(
                                      "the pause decision was read without "
                                      "the lock that orders it")):
                    with seats_identity._delivery_guard("seat-x") as pause:
                        self.assertIs(pause, proxywatch.DELIVERY_BUSY)
                        self.assertTrue(pause, "BUSY must read as 'do not "
                                               "deliver' at every site that "
                                               "already tests the pause")
                    with patch.object(seats_delivery, "_warn_disagreement",
                                      return_value=False):
                        with patch.object(seats_delivery, "touch_seen",
                                          side_effect=AssertionError(
                                              "presence was touched on a "
                                              "boundary that delivered "
                                              "nothing")):
                            self.assertIsNone(seats_delivery.deliver_any(
                                seat="seat-x", emit=lambda line: None))
            # CONTROL: release the lock and the same call reaches the pause
            # decision, so the arm above discriminates contention and not a
            # guard that always declines.
            fcntl.flock(holder, fcntl.LOCK_UN)
            with patch.object(proxywatch, "delivery_wait", lambda: 0.05):
                with patch.object(seats_identity, "_delivery_pause",
                                  return_value="the-real-decision"):
                    with seats_identity._delivery_guard("seat-x") as pause:
                        self.assertEqual(pause, "the-real-decision")

    def test_pair_proxywatch_and_room_release_on_acquisition_end_cancel(self):
        from helm import chat, proxywatch, toolwhisper
        original = latency.append
        for name in ("proxywatch", "room", "whisper"):
            with self.subTest(owner=name), contextlib.ExitStack() as stack:
                if name == "proxywatch":
                    stack.enter_context(patch.object(proxywatch, "_state_path", lambda: str(self.root / "state")))
                    target = self.root / ".proxywatch-state.lock"
                    owner = proxywatch.delivery_state_guard
                elif name == "room":
                    stack.enter_context(patch.object(chat, "chat_dir", lambda: str(self.root)))
                    target = self.root / "fake.lock"
                    owner = lambda: chat._room_lock("fake")
                else:
                    stack.enter_context(patch.object(toolwhisper, "_latch_path", lambda sid: str(self.root / "latch")))
                    target = self.root / "latch.pair-lock"
                    @contextlib.contextmanager
                    def owner():
                        toolwhisper.commit_for_pair({"session": "fake", "rule": "fake"})
                        yield
                sentinel = KeyboardInterrupt("after acquisition")
                def append(row):
                    if row["stage"] == "lock-" + name and row["kind"] == "END":
                        self.assertEqual("acquired", row["outcome"])
                        raise sentinel
                    return original(row)
                with self.assertRaises(KeyboardInterrupt) as caught:
                    with patch.object(latency, "append", append):
                        with latency.event_scope("composite"):
                            with owner(): self.fail("body entered after telemetry cancellation")
                self.assertIs(sentinel, caught.exception)
                with open(target, "a") as check:
                    fcntl.flock(check, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertFalse((self.root / "latch").exists())

    def test_lock_fail_open_is_not_acquired(self):
        target = str(self.root / "owned.lock")
        with open(target, "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            with latency.event_scope("composite"):
                with seats_common._flocked(target, blocking=False) as lock:
                    self.assertIsNone(lock.f)
        group = next(g for g in latency.report()["populations"] if g["stage"] == "lock-seats")
        self.assertEqual("failed-open", group["outcome"])
        self.assertIsNone(group["p95_ms"])

    def test_shared_lock_does_not_register_unrelated_commands(self):
        with seats_common._flocked(str(self.root / "other")): pass
        self.assertFalse(Path(latency.path()).exists())
        with patch.object(cli, "VERBS", {"record": lambda args: 0}), \
             patch.object(hooks, "hook_skips_here", return_value=False):
            self.assertEqual(0, hookrun.run_one({"name": "record", "args": "record --hook-json", "timeout": 0}, "{}"))
        self.assertFalse(Path(latency.path()).exists())
        self.assertTrue(latency.entry_allowed())

    def test_dispatch_timeout_preserves_timer_and_stream_cleanup(self):
        before = sys.stdin, list(sys.argv), os.getcwd(), signal.getsignal(signal.SIGALRM)
        with patch.object(cli, "main", side_effect=hookrun._Timeout()), \
             contextlib.redirect_stderr(io.StringIO()):
            with latency.event_scope("composite"):
                self.assertEqual(0, hookrun.run_one({"name": "record", "args": "record --hook-json", "timeout": .1}, "{}"))
        self.assertEqual(before, (sys.stdin, sys.argv, os.getcwd(), signal.getsignal(signal.SIGALRM)))
        self.assertEqual((0., 0.), signal.getitimer(signal.ITIMER_REAL))
        self.assertEqual(2, latency.report()["counts"]["timeout"])

    def _record_producers(self, composite):
        # Keep parser, recorder and both persistence owners real. Only unrelated
        # delivery/identity and external Git work are replaced with private seams.
        for index, name in enumerate((["PostToolUse"], {"event": "PostToolUse"},
                                      "PostToolUse", "PostToolUseFailure")):
            with self.subTest(composite=composite, event=name), contextlib.ExitStack() as stack:
                root = self.root / str(index)
                root.mkdir()
                for obj, attr, value in (
                    (latency, "path", lambda: str(root / "ledger")),
                    (record, "session_dir", lambda sid: str(root / "record")),
                    (record, "_git_dirty", lambda cwd: False),
                    (hooks, "hook_skips_here", lambda verb, rest: False),
                    (toolwhisper, "prepare_for_pair", lambda session: None),
                    (seats_rename, "recover_seat_rename", lambda: True),
                    (seats, "resolve_homing", lambda *a, **kw: ("main", "operator")),
                    (seats, "_assert_own_seat", lambda *a, **kw: ("fake-seat", None)),
                    (seats_cli.actors, "grant_on_behalf", lambda *a, **kw: (None, None)),
                    (seats_cli, "_record_posttool_delegation", lambda event: None),
                    (seats_cli, "deliver_any", lambda **kw: None),
                ):
                    stack.enter_context(patch.object(obj, attr, value))
                event = {"session_id": "producer-session", "tool_name": "Edit",
                         "hook_event_name": name, "cwd": str(root),
                         "tool_input": {"file_path": str(root / "synthetic-target.txt")}}
                raw = json.dumps(event)
                self.assertEqual(event, record.parse_event(raw))
                argv = (["hooks", "run", "PostToolUse", "--installed", "--hook-json"]
                        if composite else ["record", "--hook-json"])
                before = (sys.stdin, list(sys.argv), signal.getsignal(signal.SIGALRM),
                          signal.getitimer(signal.ITIMER_REAL))
                out, err = io.StringIO(), io.StringIO()
                with tempfile.TemporaryFile() as publication:
                    saved = os.dup(1)
                    try:
                        os.dup2(publication.fileno(), 1)
                        with patch.object(sys, "stdin", io.StringIO(raw)), \
                             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                            self.assertEqual(0, cli.main(argv))
                    finally:
                        os.dup2(saved, 1)
                        os.close(saved)
                    publication.seek(0)
                    self.assertEqual(b"", publication.read())
                self.assertEqual(("", ""), (out.getvalue(), err.getvalue()))
                self.assertEqual(before, (sys.stdin, list(sys.argv), signal.getsignal(signal.SIGALRM),
                                          signal.getitimer(signal.ITIMER_REAL)))
                failed = name == "PostToolUseFailure"
                counters = record.counters("producer-session")
                self.assertEqual("Edit", counters.get("last-tool"))
                self.assertEqual(int(failed), counters.get("passive-streak"))
                for artifact, text in (("edit-targets.log", "synthetic-target.txt\n"),
                                       ("edit-paths.log", record._home_relative(str(root / "synthetic-target.txt")) + "\n")):
                    target = root / "record" / artifact
                    self.assertEqual(not failed, target.exists())
                    if not failed: self.assertEqual(text, target.read_text())
                # Failure hooks are installed separately. A failure payload sent
                # through the PostToolUse-only composite proves recorder behavior,
                # not compatibility of that discordant telemetry event pair.
                if not (composite and failed):
                    report = latency.report()
                    self.assertEqual(0, report["counts"]["invalid"])
                    groups = [g for g in report["populations"] if g["stage"] == "event"]
                    self.assertEqual(1, len(groups))
                    expected = name if type(name) is str else ("PostToolUse" if composite else "PostToolUse-or-Failure")
                    self.assertEqual(expected, groups[0]["event"])
                    self.assertEqual(1, groups[0]["completed"])
                self.assertFalse(latency.active())
                self.assertTrue(latency.entry_allowed())

    def test_standalone_bind_preserves_real_recording_for_event_shapes(self):
        self._record_producers(False)

    def test_composite_bind_preserves_real_recording_for_event_shapes(self):
        self._record_producers(True)

    def test_bind_does_not_coerce_unsupported_event(self):
        class EventLike:
            def __str__(self): raise AssertionError("unsupported event coerced")
        with latency.event_scope("standalone"):
            latency.bind("session", EventLike())
        self.assertEqual(1, latency.report()["counts"]["completed"])

    def test_bind_preserves_cancellation(self):
        sentinel = KeyboardInterrupt("bind")
        with self.assertRaises(KeyboardInterrupt) as caught:
            with latency.event_scope("standalone"):
                with patch.object(latency, "_identity", side_effect=sentinel):
                    latency.bind("session", "PostToolUse")
        self.assertIs(sentinel, caught.exception)
        self.assertFalse(latency.active())

    def test_scoped_standalone_is_skipped_not_success(self):
        with patch.object(hooks, "hook_skips_here", return_value=True):
            self.assertEqual(0, cli.main(["chat", "deliver", "--hook-json"]))
        result = latency.report()
        self.assertEqual(0, result["counts"]["completed"])
        self.assertEqual(2, len(result["populations"]))
        self.assertEqual({"event", "delivery"}, {g["stage"] for g in result["populations"]})
        self.assertTrue(all(g["outcome"] == "skipped" and g["mode"] == "standalone"
                            and g["matched"] == 1 and g["completed"] == 0
                            for g in result["populations"]))
