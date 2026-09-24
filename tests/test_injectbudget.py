#!/usr/bin/env python3
"""THE TWO ERRORS THIS INSTRUMENT EXISTS TO NOT MAKE ARE THE ARMS.

A hand-rolled version of this measurement over one transcript produced a hook
share several times too large and a repeat rate several times too large, and
both wrong numbers looked entirely credible -- the repeat figure was stable
across nineteen windows, which read as evidence rather than as the signature
of a duplication factor that happened to be uniform within that one file. Two
mistakes did it: sizing hook cost by transcript LINE bytes (which repeat the
payload across `content`, `stdout` and, where the harness writes one,
`rendered`, beside a kilobytes-long `command` the model never sees), and
counting entry repeats ACROSS compaction boundaries (where re-delivery is the
injector restoring what the seat provably lost).

THE DUPLICATION FACTOR IS NOT A CONSTANT AND NO ARM HERE ASSERTS ONE. The
copy count varies by harness build, so the arms fix it in their OWN fixtures
and assert that the census is INDIFFERENT to it -- which is the property that
has to hold on every build, rather than a number true of one session.

So every arm here pairs the reading with the WRONG reading it must not
produce: a fixture whose line bytes and rendered bytes differ by a known
factor, and one where the cross-boundary count and the within-window count
differ by a known amount. An arm that only asserted the right answer would
pass just as happily against either bug.
"""
import io
import json
import os
import contextlib
import tempfile
import unittest

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-home-", var="HELM_HOME")

from helm import doctor, injectbudget  # noqa: E402


def hook_rec(text, event="UserPromptSubmit", command="x" * 4096,
             rendered=True):
    """A hook record shaped like the harness writes one: the payload appears
    in `content`, in `stdout` AND in `rendered`, beside a large `command`
    that never reaches the model."""
    rec = {"type": "attachment",
           "attachment": {"type": "hook_success", "hookEvent": event,
                          "hookName": "%s:helm" % event, "command": command,
                          "content": text, "stdout": text, "exitCode": 0}}
    if rendered:
        rec["rendered"] = [{"content": text}]
    return rec


def work_rec(text):
    """A turn of real work -- the denominator."""
    return {"type": "assistant",
            "message": {"role": "assistant",
                        "content": [{"type": "text", "text": text}],
                        "usage": {"input_tokens": 10}}}


BOUNDARY = {"type": "system", "subtype": "compact_boundary",
            "compactMetadata": {"trigger": "auto", "preTokens": 1}}


def write_transcript(records):
    fh = tempfile.NamedTemporaryFile(prefix="injectbudget-", suffix=".jsonl",
                                     delete=False, mode="w")
    for rec in records:
        fh.write(json.dumps(rec) + "\n")
    fh.close()
    return fh.name


class PopulationIsProvenanceTest(unittest.TestCase):
    """The population is decided by who WROTE the record, not by its text."""

    def setUp(self):
        self.paths = []
        self.addCleanup(self._clean)

    def _clean(self):
        for p in self.paths:
            try:
                os.unlink(p)
            except OSError:
                pass

    def _census(self, records, **kw):
        path = write_transcript(records)
        self.paths.append(path)
        return injectbudget.census(path, **kw)

    def test_hook_text_in_a_work_turn_is_not_hook_bytes(self):
        # THE CONTROL THAT FAILS IF THE POPULATION EVER BECOMES A TEXT MATCH.
        # This assistant turn quotes an injected premise verbatim -- exactly
        # what a turn investigating the injector does, and what this very
        # file does. A grep-based census counts it; a provenance-based one
        # cannot.
        quoted = "PREMISE some-slug: a premise quoted inside a work turn"
        cen = self._census([work_rec(quoted), work_rec("more work")])
        win = injectbudget.worst(cen)
        self.assertEqual(win["hook_bytes"], 0)
        self.assertEqual(win["injections"], 0)
        self.assertGreater(win["context_bytes"], 0)

        # POSITIVE CONTROL, SAME TEXT: filed by the harness as a hook, the
        # identical string IS counted. Without this the arm above passes on a
        # census that counts nothing at all.
        cen = self._census([hook_rec(quoted), work_rec("more work")])
        win = injectbudget.worst(cen)
        self.assertEqual(win["hook_bytes"], len(quoted))
        self.assertEqual(win["injections"], 1)

    def test_hook_event_reads_the_harness_type_not_the_name(self):
        self.assertIsNone(injectbudget.hook_event(work_rec("PostToolUse")))
        self.assertEqual(injectbudget.hook_event(hook_rec("x", "Stop")), "Stop")
        # A non-hook attachment is not hook output.
        self.assertIsNone(injectbudget.hook_event(
            {"type": "attachment", "attachment": {"type": "file"}}))


class RenderedNotLineBytesTest(unittest.TestCase):
    """Hook COST is the rendered block; the line is three copies and a
    command blob."""

    def setUp(self):
        self.paths = []
        self.addCleanup(lambda: [os.unlink(p) for p in self.paths
                                 if os.path.exists(p)])

    def test_the_census_is_indifferent_to_how_many_copies_the_record_holds(self):
        # THE PROPERTY THAT HOLDS ON EVERY HARNESS BUILD. Some transcripts
        # repeat the payload twice (`content` + `stdout`, byte-identical) and
        # some three times (plus `rendered`); the copy count is not a fleet
        # constant and nothing here may assume one. What must be true either
        # way is that the payload is charged ONCE.
        payload = "PREMISE a: one\nPREMISE b: two\nPREMISE c: three"
        measured = {}
        for copies, rec in ((3, hook_rec(payload)),
                            (2, hook_rec(payload, rendered=False))):
            path = write_transcript([rec, work_rec("w" * 500)])
            self.paths.append(path)
            with open(path, "rb") as fh:
                raw = fh.read().decode()
            # The fixture really does carry the copy count this arm claims.
            self.assertEqual(raw.count("PREMISE a:"), copies)
            win = injectbudget.worst(injectbudget.census(path))
            self.assertEqual(win["hook_bytes"], len(payload))
            self.assertEqual(win["injections"], 3)
            measured[copies] = win["hook_bytes"]
        # Indifference stated directly: two different records, one answer.
        self.assertEqual(measured[2], measured[3])

        # THE CONTROL on the wrong reading: a line count would be many times
        # the payload, because of the `command` blob alone.
        self.assertGreater(len(raw), 6 * len(payload))
        self.assertLess(measured[3], len(raw) / 6.0)

    def test_a_hook_that_printed_nothing_costs_nothing(self):
        path = write_transcript([hook_rec("", rendered=False),
                                 work_rec("w" * 100)])
        self.paths.append(path)
        win = injectbudget.worst(injectbudget.census(path))
        self.assertEqual(win["hook_bytes"], 0)
        self.assertEqual(win["unrendered"], 0)  # no payload -> not a blind spot

        # UNCONDITIONAL POSITIVE CONTROL ON THE SAME OBSERVABLE. Both
        # assertions above are satisfied by a census that reads nothing at
        # all, so the identical fixture with ONE printing hook must register
        # on the identical fields.
        path = write_transcript([hook_rec("", rendered=False),
                                 hook_rec("PREMISE a: printed"),
                                 work_rec("w" * 100)])
        self.paths.append(path)
        loud = injectbudget.worst(injectbudget.census(path))
        self.assertEqual(loud["hook_bytes"], len("PREMISE a: printed"))
        self.assertEqual(loud["injections"], 1)

    def test_payload_neither_field_accounts_for_is_a_blind_spot(self):
        # The control on this module's core assumption: payload the harness
        # recorded but that reached NEITHER `rendered` nor `stdout` is not
        # silently treated as absent. It is counted, so a reader can see the
        # instrument has stopped being able to see what it claims to.
        rec = hook_rec("", rendered=False)
        rec["attachment"]["content"] = "PREMISE a: x" * 40   # content only
        path = write_transcript([rec, work_rec("w" * 100)])
        self.paths.append(path)
        win = injectbudget.worst(injectbudget.census(path))
        self.assertEqual(win["hook_bytes"], 0)
        self.assertEqual(win["unrendered"], 1)
        self.assertGreater(win["unrendered_bytes"], 0)

        # CONTROL: the same payload reachable through stdout is MEASURED, not
        # counted as a blind spot -- otherwise the arm above passes on a
        # census that can see nothing at all.
        path = write_transcript([hook_rec("PREMISE a: x" * 40, rendered=False),
                                 work_rec("w" * 100)])
        self.paths.append(path)
        seen = injectbudget.worst(injectbudget.census(path))
        self.assertGreater(seen["hook_bytes"], 0)
        self.assertEqual(seen["unrendered"], 0)


class CompactionSegmentationTest(unittest.TestCase):
    """A re-delivery after a boundary is the design working."""

    def setUp(self):
        self.paths = []
        self.addCleanup(lambda: [os.unlink(p) for p in self.paths
                                 if os.path.exists(p)])

    def _census(self, records, **kw):
        path = write_transcript(records)
        self.paths.append(path)
        return injectbudget.census(path, **kw)

    def test_same_entry_across_a_boundary_is_not_a_repeat(self):
        line = "PREMISE same-one: the body"
        cen = self._census([hook_rec(line), work_rec("w" * 100),
                            BOUNDARY,
                            hook_rec(line), work_rec("w" * 100)], whole=True)
        self.assertEqual(len(cen["windows"]), 2)
        for win in cen["windows"]:
            self.assertEqual(win["injections"], 1)
            self.assertEqual(injectbudget.repeat_rate(win), 0.0)

        # THE CONTROL: the SAME two deliveries with no boundary between them
        # ARE a repeat. Without this, an implementation that counts nothing at
        # all satisfies the arm above.
        cen = self._census([hook_rec(line), work_rec("w" * 100),
                            hook_rec(line), work_rec("w" * 100)], whole=True)
        self.assertEqual(len(cen["windows"]), 1)
        win = cen["windows"][0]
        self.assertEqual(win["injections"], 2)
        self.assertEqual(injectbudget.repeat_rate(win), 0.5)

    def test_every_tag_the_renderer_emits_is_counted(self):
        # THE GAP A RENDERER READ FOUND. A pattern built from SAMPLES matched
        # the tags that happened to be in view and dropped every PRIOR, which
        # carries its confidence between tag and slug -- an 18% undercount on
        # a real transcript, invisible to arms built from the same samples.
        # The tags and shapes here are `_entry_line_full`'s, not a sample's.
        block = "\n".join((
            "PREMISE p-one: body",
            "PRIOR 0.90 p-two: body",          # confidence between tag and slug
            "[provisional] PREMISE p-three: body",
            "MOVE p-four: body",
            "REF p-five: body",
            "TERM p-six: body",
            "WHO operator: body",
        ))
        win = injectbudget.worst(self._census([hook_rec(block),
                                               work_rec("w" * 100)]))
        self.assertEqual(win["injections"], 7)
        self.assertEqual(sorted(win["ids"]),
                         ["operator", "p-five", "p-four", "p-one", "p-six",
                          "p-three", "p-two"])

    def test_a_capability_line_is_not_miscounted_as_an_entry(self):
        # CONTROL on the arm above: `CAP <slug>` renders with NO colon, so a
        # colon-terminated pattern cannot match it. The arm proves the
        # alternation is not matching loosely on the tag alone.
        win = injectbudget.worst(self._census([hook_rec("CAP some-capability"),
                                               work_rec("w" * 100)]))
        self.assertEqual(win["injections"], 0)
        self.assertGreater(win["hook_bytes"], 0)   # the bytes still counted

    def test_first_entry_of_a_block_is_counted(self):
        # The harness prefixes the first entry on the same line, so a
        # line-anchored pattern drops exactly one entry per delivery. The
        # control is the count: three entries, one of them prefixed.
        block = ("<system-reminder>\nUserPromptSubmit hook success: "
                 "PREMISE a: one\nPREMISE b: two\nMOVE c: three\n"
                 "</system-reminder>")
        cen = self._census([hook_rec(block), work_rec("w" * 100)])
        win = injectbudget.worst(cen)
        self.assertEqual(win["injections"], 3)
        self.assertEqual(sorted(win["ids"]), ["a", "b", "c"])


class BudgetVerdictTest(unittest.TestCase):
    """The breach, and the two silences that must not read as clean."""

    def setUp(self):
        self.paths = []
        self.addCleanup(lambda: [os.unlink(p) for p in self.paths
                                 if os.path.exists(p)])

    def _findings(self, records, **kw):
        path = write_transcript(records)
        self.paths.append(path)
        return injectbudget.findings(injectbudget.census(path, **kw))

    def _level(self, findings, needle):
        for level, msg in findings:
            if needle in msg:
                return level
        return None

    def test_share_breaches_loudly_and_reports_when_it_does_not(self):
        # UNDER: a small hook payload beside a large turn.
        under = self._findings([hook_rec("PREMISE a: x"), work_rec("w" * 4000)])
        self.assertEqual(self._level(under, "hooks are"), injectbudget.OK)
        # THE NUMBER IS RENDERED EVEN WHEN CLEAN -- the property that makes
        # this a watcher rather than an alarm.
        self.assertIn("% of this seat's context",
                      [m for _l, m in under if "hooks are" in m][0])

        # OVER: the same shape with the proportions inverted.
        over = self._findings([hook_rec("PREMISE a: " + "x" * 4000),
                               work_rec("w" * 400)])
        self.assertEqual(self._level(over, "hooks are"), injectbudget.FAIL)
        self.assertIn("OVER the", [m for _l, m in over if "hooks are" in m][0])

    def test_empty_context_is_unknown_never_zero_percent(self):
        win = injectbudget._blank_window()
        self.assertIsNone(injectbudget.share(win))
        self.assertIsNone(injectbudget.repeat_rate(win))
        # And the finding says so rather than reporting a clean 0%.
        found = self._findings([{"type": "system", "subtype": "other"}])
        self.assertEqual(self._level(found, "UNMEASURED"), injectbudget.WARN)

    def test_repeat_verdict_names_its_own_worst_window(self):
        # THE DEFECT THIS ARM EXISTS FOR: the worst-share window and the
        # worst-repeat window are different windows, and reporting repeat from
        # the share's window leaves the breaching window unnamed. Window one
        # is all share and no repeat; window two is all repeat and little
        # share.
        dup = "PREMISE dup: body"
        found = self._findings(
            [hook_rec("PREMISE solo: " + "x" * 3000), work_rec("w" * 3000),
             BOUNDARY,
             hook_rec(dup), hook_rec(dup), hook_rec(dup), hook_rec(dup),
             work_rec("w" * 40000)],
            whole=True)
        share_msg = [m for _l, m in found if "hooks are" in m][0]
        repeat_msg = [m for _l, m in found if "injection repeat" in m][0]
        # The share line reports window one; the repeat line reports window
        # two's 75%, which window one does not have.
        self.assertIn("75%", repeat_msg)
        self.assertEqual(self._level(found, "injection repeat"),
                         injectbudget.FAIL)
        self.assertNotIn("75%", share_msg)

    def test_a_repeat_breach_reports_the_rate_and_names_no_cause(self):
        # THE ALARM MAY NOT ACCUSE. A repeat inside one window is equally
        # consistent with suppression failing and with the score escape firing
        # as designed, and this census sees neither -- it sees only that an id
        # arrived twice. Measured on helm's own fire ledger, the cooldown
        # suppresses the large majority of candidates, so a rung asserting it
        # "is NOT holding" would ship a false accusation to every seat, and one
        # that sleeps until someone trips it.
        dup = "PREMISE dup: body"
        found = self._findings([hook_rec(dup), hook_rec(dup), hook_rec(dup),
                                work_rec("w" * 4000)])
        level, msg = [(l, m) for l, m in found if "injection repeat" in m][0]
        self.assertIn(level, (injectbudget.WARN, injectbudget.FAIL))
        # Reports the rate, names the window definition, points at the reader.
        self.assertIn("67%", msg)
        self.assertIn("compaction boundary", msg)
        self.assertIn("_cooled", msg)
        self.assertIn("cannot tell which", msg)
        # And names NO cause. These are the accusations it must never make.
        for banned in ("NOT holding", "not holding", "second dedupe",
                       "boilerplate", "billed"):
            self.assertNotIn(banned, msg)

    def test_no_shipped_finding_asserts_a_cause_or_a_price(self):
        # THE SWEEP, not the instance. The class is a true number with a false
        # claim attached, so it is checked over EVERY sentence this module can
        # emit rather than over the one that carried it.
        dup = "PREMISE dup: body"
        corpora = (
            [hook_rec(dup), hook_rec(dup), hook_rec(dup), work_rec("w" * 4000)],
            [hook_rec("PREMISE a: " + "x" * 4000), work_rec("w" * 400)],
            [hook_rec("PREMISE a: x"), work_rec("w" * 4000)],
            [{"type": "system", "subtype": "other"}],
        )
        seen = 0
        for records in corpora:
            for _level, msg in self._findings(records):
                seen += 1
                for banned in ("boilerplate", "billed", "NOT holding"):
                    self.assertNotIn(banned, msg, "cause/price claim in: %s" % msg)
        # CONTROL: the sweep actually read sentences. A corpus that produced
        # none would satisfy every assertion above.
        self.assertGreater(seen, 6)


class BoundedReadTest(unittest.TestCase):
    """Three outcomes, not two: found, start-of-file, and INCOMPLETE."""

    def setUp(self):
        self.paths = []
        self.addCleanup(lambda: [os.unlink(p) for p in self.paths
                                 if os.path.exists(p)])

    def _write(self, records):
        path = write_transcript(records)
        self.paths.append(path)
        return path

    def test_boundary_found_bounds_the_read(self):
        path = self._write([work_rec("old" * 2000)] * 20 + [BOUNDARY] +
                           [hook_rec("PREMISE a: x"), work_rec("new")])
        # A chunk smaller than the file, so the bounding is OBSERVABLE: with
        # the production chunk any fixture fits in one read and "bounded"
        # would be indistinguishable from "read it all".
        lines, complete, read, why = injectbudget.window_lines(path, chunk=4096)
        self.assertTrue(complete)
        self.assertEqual(why, "")
        self.assertLess(read, os.path.getsize(path))   # it did NOT read it all
        win = injectbudget.worst(injectbudget.census(path))
        self.assertEqual(win["injections"], 1)         # only the new window

    def test_a_session_that_never_compacted_is_complete(self):
        path = self._write([hook_rec("PREMISE a: x"), work_rec("w")])
        _lines, complete, _read, why = injectbudget.window_lines(path)
        self.assertTrue(complete)       # start-of-file is complete, not a cap
        self.assertEqual(why, "")

    def test_exhausting_the_cap_is_incomplete_not_a_clean_window(self):
        path = self._write([work_rec("x" * 500)] * 40)
        lines, complete, _read, why = injectbudget.window_lines(
            path, cap=1024, chunk=256)
        self.assertFalse(complete)
        self.assertIn("UNKNOWN", why)
        # AND THE VERDICT CARRIES IT. A bounded suffix reported as a window is
        # the failure; the census must say the share is over a suffix.
        cen = injectbudget.census(path, cap=1024)
        self.assertFalse(cen["complete"])
        found = injectbudget.findings(cen)
        self.assertTrue(any("SUFFIX" in m for _l, m in found))
        # AND THE HEADLINE CARRIES IT. An incomplete read may not return a
        # budget verdict at all: "under budget" about a fragment is a claim
        # the census did not earn, and a caveat printed below a confident
        # number is not a qualifier.
        head = [(l, m) for l, m in found if "hooks are" in m][0]
        self.assertEqual(head[0], injectbudget.WARN)
        self.assertIn("UNKNOWN", head[1])
        self.assertNotIn("under the", head[1])

        # CONTROL: the same file read with a sufficient cap IS complete, and
        # its headline DOES carry a budget verdict -- so the arm above cannot
        # pass on a census that calls everything unknown.
        whole = injectbudget.census(path)
        self.assertTrue(whole["complete"])
        head = [(l, m) for l, m in injectbudget.findings(whole)
                if "hooks are" in m][0]
        self.assertEqual(head[0], injectbudget.OK)
        self.assertIn("under the", head[1])

    def test_a_partial_first_line_is_dropped_not_parsed(self):
        path = self._write([work_rec("x" * 400)] * 30)
        cen = injectbudget.census(path, cap=4096)
        self.assertEqual(cen["unparsed"], 0)


def seat_row(seat, hook_kb, ctx_kb, **kw):
    """A fleet row at a chosen share, for the ranking arms."""
    row = {"seat": seat, "path": "/tmp/%s.jsonl" % seat, "mtime": 0,
           "share": (hook_kb / float(ctx_kb)) if ctx_kb else None,
           "repeat": None, "hook_bytes": hook_kb * 1024,
           "context_bytes": ctx_kb * 1024,
           "ranked": ctx_kb * 1024 >= injectbudget.MIN_CONTEXT,
           "estimated": False, "top": [], "complete": True}
    row.update(kw)
    return row


class FleetRankingTest(unittest.TestCase):
    """Per seat, worst-first, never a mean -- and a floor under the ratio."""

    def test_the_worst_seat_is_the_headline_not_the_average(self):
        # FOUR QUIET SEATS AND ONE BREACH. The mean is comfortably under
        # budget; the headline must name the breach anyway, which is the
        # whole reason an average is refused here.
        rows = [seat_row("quiet%d" % i, 50, 1000) for i in range(4)]
        rows.append(seat_row("loud", 400, 1000))          # 40%
        found = injectbudget.fleet_findings(rows)
        head = found[0]
        self.assertEqual(head[0], injectbudget.FAIL)
        self.assertIn("loud", head[1])
        self.assertIn("40.0%", head[1])
        # CONTROL: the mean of this population is under the warning band, so a
        # mean-based headline would have been OK and named no seat.
        mean = sum(r["share"] for r in rows) / float(len(rows))
        self.assertLess(mean, injectbudget.WARN_SHARE)

    def test_a_tiny_context_is_not_ranked_and_never_takes_the_headline(self):
        # A four-kilobyte session reads as 95% hooks because SessionStart is a
        # fixed cost not yet amortised. Ranked, it steals the headline from
        # the seat genuinely over budget.
        rows = [seat_row("warming", 4, 4), seat_row("real", 250, 1000)]
        found = injectbudget.fleet_findings(rows)
        self.assertIn("real", found[0][1])
        self.assertNotIn("warming", found[0][1])
        # The tiny seat is still REPORTED, and said to be unranked.
        line = [m for _l, m in found if "warming" in m][0]
        self.assertIn("NOT ranked", line)
        # CONTROL: the same seat above the floor IS ranked and does breach.
        big = seat_row("warming", 950, 1000)
        self.assertTrue(big["ranked"])
        found = injectbudget.fleet_findings([big, seat_row("real", 250, 1000)])
        self.assertIn("warming", found[0][1])

    def test_fleet_derives_the_floor_from_the_transcript_not_the_fixture(self):
        # THE GAP A MUTATION FOUND. Every arm above feeds `fleet_findings` a
        # hand-built row whose `ranked` flag the FIXTURE decided, so removing
        # the floor from `fleet` itself changed no arm -- a fixture that
        # invents the value under test proves nothing about the producer.
        # This drives `fleet` over real files instead.
        small = write_transcript([hook_rec("PREMISE a: x"), work_rec("w" * 40)])
        big = write_transcript([hook_rec("PREMISE a: x")] +
                               [work_rec("w" * 4000)] * 120)
        for p in (small, big):
            self.addCleanup(lambda q=p: os.path.exists(q) and os.unlink(q))
        rows = {r["path"]: r for r in
                injectbudget.fleet(paths=[(0, small), (0, big)])}
        self.assertLess(rows[small]["context_bytes"], injectbudget.MIN_CONTEXT)
        self.assertFalse(rows[small]["ranked"])
        # CONTROL, SAME CALL: a transcript over the floor IS ranked, so the
        # arm cannot pass on a `fleet` that ranks nothing.
        self.assertGreaterEqual(rows[big]["context_bytes"],
                                injectbudget.MIN_CONTEXT)
        self.assertTrue(rows[big]["ranked"])
        # And the unranked seat never takes the headline off the ranked one.
        head = injectbudget.fleet_findings(list(rows.values()))[0]
        self.assertIn(rows[big]["seat"], head[1])

    def test_an_empty_fleet_is_unmeasured_not_clean(self):
        found = injectbudget.fleet_findings([])
        self.assertEqual(found[0][0], injectbudget.WARN)
        self.assertIn("UNMEASURED", found[0][1])
        # CONTROL: a populated fleet does produce a verdict.
        found = injectbudget.fleet_findings([seat_row("s", 50, 1000)])
        self.assertEqual(found[0][0], injectbudget.OK)

    def test_condensed_keeps_the_population_count(self):
        # A rung printing only breaches renders a clean fleet and an unwired
        # census identically; the count of quiet seats is what separates them.
        rows = [seat_row("quiet%d" % i, 50, 1000) for i in range(3)]
        found = injectbudget.fleet_findings(rows, condensed=True)
        self.assertEqual(len(found), 1)
        self.assertIn("3 of 3", found[0][1])


class DoctorRungTest(unittest.TestCase):
    """The rung is registered, never raises, and UNMEASURED is not clean."""

    def test_registered_in_checks(self):
        self.assertIn("check_injection_budget", doctor.CHECKS)

    def test_a_raising_survey_reports_rather_than_taking_doctor_down(self):
        def boom():
            raise RuntimeError("nope")
        out = doctor.check_injection_budget(survey=boom)
        self.assertEqual([l for l, _ in out], [doctor.WARN])
        self.assertIn("RuntimeError", out[0][1])
        self.assertIn("UNMEASURED", out[0][1])

    def test_an_empty_fleet_warns_rather_than_passing(self):
        out = doctor.check_injection_budget(survey=lambda: [])
        self.assertEqual(out[0][0], doctor.WARN)

    def test_a_real_survey_reaches_the_rung_and_breaches(self):
        # POSITIVE CONTROL on the refusal arms above: a rung that always
        # warned would satisfy every one of them.
        out = doctor.check_injection_budget(
            survey=lambda: [seat_row("hot", 400, 1000)])
        self.assertEqual(out[0][0], doctor.FAIL)
        self.assertIn("hot", out[0][1])
        # And a healthy fleet reads OK through the identical path.
        out = doctor.check_injection_budget(
            survey=lambda: [seat_row("cool", 50, 1000)])
        self.assertEqual(out[0][0], doctor.OK)


class EstimatedReadingTest(unittest.TestCase):
    """Two transcript grammars; a missing `rendered` is not a zero."""

    def setUp(self):
        self.paths = []
        self.addCleanup(lambda: [os.unlink(p) for p in self.paths
                                 if os.path.exists(p)])

    def _census(self, records):
        path = write_transcript(records)
        self.paths.append(path)
        return injectbudget.census(path)

    def test_stdout_stands_in_when_the_harness_records_no_rendered_block(self):
        # THE DANGEROUS CASE: older transcripts carry no `rendered` field at
        # all, and reading that as zero reports a confident 0.0% for every
        # seat on that build -- the worst possible output for an instrument
        # whose job is noticing growth.
        payload = "PREMISE a: one\nPREMISE b: two"
        cen = self._census([hook_rec(payload, rendered=False),
                            work_rec("w" * 900)])
        win = injectbudget.worst(cen)
        self.assertEqual(win["hook_bytes"], len(payload))
        self.assertEqual(win["estimated_bytes"], len(payload))
        self.assertEqual(win["injections"], 2)
        # AND THE HEADLINE CARRIES THE QUALIFIER, because a caveat printed
        # below a confident number is not a qualifier -- the clause holding
        # the number is the one a reader acts on.
        msg = [m for _l, m in injectbudget.findings(cen) if "hooks are" in m][0]
        self.assertIn("ESTIMATED", msg)

    def test_an_exact_reading_is_not_marked_estimated(self):
        # CONTROL on the arm above: with `rendered` present nothing is
        # estimated and the qualifier must be absent, or it means nothing.
        cen = self._census([hook_rec("PREMISE a: one"), work_rec("w" * 900)])
        win = injectbudget.worst(cen)
        self.assertEqual(win["estimated_bytes"], 0)
        msg = [m for _l, m in injectbudget.findings(cen) if "hooks are" in m][0]
        self.assertNotIn("ESTIMATED", msg)

    def test_a_json_envelope_costs_only_what_it_injects(self):
        # A hook answering `{}` declines to inject anything. Charging the
        # envelope's own punctuation measures the protocol, not the payload --
        # on a real old-format transcript the naive reading was twice the true
        # one, all of the excess on hooks that injected nothing.
        rec = hook_rec("", rendered=False)
        rec["attachment"]["stdout"] = '{"suppressOutput": true}'
        cen = self._census([rec, work_rec("w" * 900)])
        self.assertEqual(injectbudget.worst(cen)["hook_bytes"], 0)

        # CONTROL, SAME SHAPE: an envelope that DOES carry additionalContext
        # is charged for exactly that text and nothing else -- so the arm
        # above cannot pass on a reader that zeroes every JSON stdout.
        rec = hook_rec("", rendered=False)
        rec["attachment"]["stdout"] = json.dumps(
            {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                    "additionalContext": "PREMISE a: real"}})
        cen = self._census([rec, work_rec("w" * 900)])
        win = injectbudget.worst(cen)
        self.assertEqual(win["hook_bytes"], len("PREMISE a: real"))
        self.assertEqual(win["injections"], 1)

    def test_unparseable_stdout_is_counted_not_dropped_to_zero(self):
        # A truncated body that merely starts with a brace is still text the
        # harness may have injected. Silently zeroing it would understate.
        rec = hook_rec("", rendered=False)
        rec["attachment"]["stdout"] = '{"additionalContext": "PREMISE a: tr'
        cen = self._census([rec, work_rec("w" * 900)])
        self.assertGreater(injectbudget.worst(cen)["hook_bytes"], 0)

    def test_plain_stdout_is_taken_whole(self):
        # The UserPromptSubmit form: the harness wraps stdout verbatim.
        payload = "PREMISE a: one\nPREMISE b: two"
        rec = hook_rec(payload, rendered=False)
        cen = self._census([rec, work_rec("w" * 900)])
        self.assertEqual(injectbudget.worst(cen)["hook_bytes"], len(payload))

    def test_cost_is_attributed_to_the_hook_not_only_the_event(self):
        # "PostToolUse is 60%" names no actor; the hook's own name does.
        cen = self._census([hook_rec("x" * 200, event="PostToolUse"),
                            work_rec("w" * 900)])
        win = injectbudget.worst(cen)
        self.assertEqual(win["by_hook"], {"PostToolUse:helm": 200})


class CliTest(unittest.TestCase):

    def test_verb_refuses_junk_and_prints_its_own_usage(self):
        from helm import cli
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["injectbudget", "--bogus"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown arg", err.getvalue())

    def test_json_form_carries_the_resolved_share(self):
        # A consumer must read the same share the verdict used rather than
        # recomputing it from the parts and diverging.
        path = write_transcript([hook_rec("PREMISE a: x"), work_rec("w" * 900)])
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        out = io.StringIO()
        from helm import cli
        with contextlib.redirect_stdout(out):
            rc = cli.main(["injectbudget", "--path", path, "--json"])
        self.assertEqual(rc, 0)
        blob = json.loads(out.getvalue())
        win = blob["windows"][-1]
        self.assertAlmostEqual(win["share"],
                               win["hook_bytes"] / float(win["context_bytes"]))
        self.assertEqual(win["distinct"], 1)


if __name__ == "__main__":
    unittest.main()


class Utf8ByteUnitTest(unittest.TestCase):
    """A field named bytes holds BYTES, and only a non-ASCII fixture can say so.

    The live case is not exotic: `_entry_line` appends U+2026 on truncation --
    one character, three bytes -- so a character count is short by two on every
    truncated entry. A pure-ASCII fixture cannot tell the two implementations
    apart and would ship the defect back, so each arm pairs the right answer
    with the WRONG one it must not produce. task/2691.
    """

    # Six characters, twelve UTF-8 bytes: every character is two bytes, so a
    # character count reads exactly half and the two can never coincide.
    TWO_BYTE = "\u00e9" * 6

    def setUp(self):
        self.paths = []

    def tearDown(self):
        for p in self.paths:
            try:
                os.unlink(p)
            except OSError:
                pass

    def test_the_helper_counts_bytes_and_ascii_is_why_a_control_is_needed(self):
        self.assertEqual(6, len(self.TWO_BYTE))                    # wrong unit
        self.assertEqual(12, injectbudget.utf8_len(self.TWO_BYTE))  # right one
        # THE CONTROL: on pure ASCII the two agree, which is exactly why an
        # ASCII-only fixture proves nothing about which one is implemented.
        self.assertEqual(len("abcdef"), injectbudget.utf8_len("abcdef"))

    def test_a_non_string_is_zero_rather_than_a_raise(self):
        for junk in (None, 17, [], {}):
            with self.subTest(repr(junk)):
                self.assertEqual(0, injectbudget.utf8_len(junk))

    def test_a_census_charges_utf8_bytes_for_a_non_ascii_injection(self):
        """Driven through census, the production route, not a private helper."""
        path = write_transcript([hook_rec(self.TWO_BYTE, rendered=False),
                                 work_rec("w" * 500)])
        self.paths.append(path)
        win = injectbudget.worst(injectbudget.census(path))
        self.assertEqual(12, win["hook_bytes"])
        # The wrong reading, named: a character count would say six.
        self.assertNotEqual(len(self.TWO_BYTE), win["hook_bytes"])

    def test_the_share_is_unchanged_by_the_unit_because_both_sides_moved(self):
        """The share was never the defect and this pins that it stays sound.

        Numerator and denominator are both UTF-8 now, as they were both
        characters before, so the ratio is what it always was. An arm checking
        only the absolute would not notice a cure that converted one side.
        """
        ascii_path = write_transcript([hook_rec("abcdef", rendered=False),
                                       work_rec("w" * 500)])
        wide_path = write_transcript([hook_rec(self.TWO_BYTE, rendered=False),
                                      work_rec("w" * 500)])
        self.paths += [ascii_path, wide_path]
        a = injectbudget.worst(injectbudget.census(ascii_path))
        w = injectbudget.worst(injectbudget.census(wide_path))
        # Same character count, twice the bytes, and the hook side is the only
        # part that grew -- so the shares must differ, and the ASCII one is the
        # control proving the fixture pair actually discriminates.
        self.assertEqual(6, len("abcdef"))
        self.assertEqual(2 * a["hook_bytes"], w["hook_bytes"])
        self.assertGreater(injectbudget.share(w), injectbudget.share(a))
