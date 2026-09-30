#!/usr/bin/env python3
"""task/2960: every review row gets a qwen27 findings pass, and it is a NOTE.

THE OWNER'S CONTRACT, one arm or more per clause:

  * every review row gets one pass, started DETACHED so filing never waits,
    and queued so only one runs at a time;
  * it is NEVER an approval, NEVER a gate, and NEVER the different-model read;
  * an empty result is not a clean review, and the note says so;
  * a reader that is down, slow or broken leaves ONE line naming why;
  * the endpoint and the model come from ONE pool row of the reader
    family's seat_catalog entry (qwen27 unless the knob names another),
    never a literal host, and the read names its budget and thinking.

NO ARM CALLS THE REAL MODEL OR THE REAL SCRIPT. Each arm writes a FIXTURE
`local-review.py` into its own temp dir — a script that prints a status line
and exits with the code under test — and points HELM_LOCAL_REVIEW_SCRIPT at it.
The helm home, the chat dir and the ledger are the LandReqBase temp ones.
"""
import contextlib
import fcntl
import io
import json
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import dispatches, eventledger, findingspass, landreq  # noqa: E402
# The module, never its TestCase: tests/test_suite_collection.py says why.
from tests import test_landreq as _landreq  # noqa: E402

READER = findingspass.READER

#: The reader's endpoint in every arm that runs the pass. The catalog carries
#: no host for it: the operator configures one in the helm home's endpoints
#: file, so a documentation-range address (RFC 5737 TEST-NET-1) stands in.
_READER_ENDPOINT = "http://192.0.2.10:8083/v1"


def _configure_endpoints(table):
    """Write the helm home's endpoints file (the contract's own location,
    spelled here rather than asked of the code under test); None removes it."""
    path = os.path.join(os.environ["HELM_HOME"], "_global", "endpoints.json")
    if table is None:
        if os.path.exists(path):
            os.remove(path)
        return path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(table, f)
    return path

CLEAN = """# Local review of abcdef012345

- files in diff: 5

## helm/alpha.py

`prompt 1200 tok, prefill 900 tok/s, decode 20.0 tok/s, 12s`
NONE FOUND

---

MEASURED: 5 reads, 40s total wall, largest prompt 3000 tokens.

No file was truncated; every file was read whole.

LOCAL-REVIEW-STATUS complete reads=5 errors=0 empty=0 cut=0 truncated=0 \
kept=0 rejected=1 unjudged=0 verify_rejected=0
"""

KEPT = """# Local review of abcdef012345

## helm/alpha.py

`prompt 1200 tok, prefill 900 tok/s, decode 20.0 tok/s, 12s`
**[JUDGE: REAL 3/3]**
1. File: `helm/alpha.py` Line: `return None if ok else row` Class: 4 - the
guard is inverted, so a refused row is returned as admitted.

<details><summary>1 finding(s) REJECTED by the judge (listed so you can \
overrule it)</summary>

[REAL 0/3] 2. File: `helm/alpha.py` Line: `pass` Class: 2 - rejected claim.
</details>

## helm/beta.py

`prompt 900 tok, prefill 900 tok/s, decode 20.0 tok/s, 9s`
**[JUDGE: REAL 2/3]**
3. File: `helm/beta.py` Line: `os.remove(path)` Class: 7 - the error is
swallowed and the caller reads a removed file as present.

---

MEASURED: 2 reads, 21s total wall, largest prompt 1200 tokens.

LOCAL-REVIEW-STATUS complete reads=2 errors=0 empty=0 cut=0 truncated=0 \
kept=2 rejected=1 unjudged=0 verify_rejected=0
"""

PARTIAL = """# Local review of abcdef012345

## helm/alpha.py

READER ERROR: timed out

## helm/beta.py

`prompt 900 tok, prefill 900 tok/s, decode 20.0 tok/s, 9s`
NONE FOUND

---

MEASURED: 1 reads, 9s total wall, largest prompt 900 tokens.

READER ERRORS, so these were NOT read: helm/alpha.py

LOCAL-REVIEW-STATUS partial reads=1 errors=1 empty=0 cut=0 truncated=0 \
kept=0 rejected=0 unjudged=0 verify_rejected=0
"""

UNREAD = """# Local review of abcdef012345

## helm/alpha.py

READER ERROR: <urlopen error [Errno 111] Connection refused>

---

MEASURED: 0 reads, 0s total wall, largest prompt 0 tokens.
no successful reads.

READER ERRORS, so these were NOT read: helm/alpha.py

NO FILE WAS READ AT ALL: every request to the reader failed. This is not a \
clean review; it is an absent one.

LOCAL-REVIEW-STATUS unread reads=0 errors=1 empty=0 cut=0 truncated=0 \
kept=0 rejected=0 unjudged=0 verify_rejected=0
"""

#: The fixture script. Parameters are baked in per arm, so no arm steers it
#: through the environment it shares with the rest of the suite.
FIXTURE = '''import os, sys, time
argv = sys.argv[1:]
RECORD = %(record)r
RELEASE = %(release)r
def note(word):
    if RECORD:
        with open(RECORD, "a") as f:
            f.write("%%s %%.6f %%d %%s\\n" %% (word, time.time(), os.getpid(),
                                              " ".join(argv)))
note("start")
# HELD, NOT TIMED: an arm that must see the pass still running blocks it on a
# file the arm creates. The cap and the vanished home only free a straggler.
cap = time.monotonic() + 60
while (RELEASE and not os.path.exists(RELEASE)
       and os.path.isdir(os.path.dirname(RELEASE)) and time.monotonic() < cap):
    time.sleep(0.01)
time.sleep(%(sleep)r)
sys.stdout.write(%(body)r)
sys.stderr.write(%(stderr)r)
if "--out" in argv and %(rc)r in (0, 2, 3):
    with open(argv[argv.index("--out") + 1], "w") as f:
        f.write(%(body)r)
note("end")
sys.exit(%(rc)r)
'''


class FindingsBase(_landreq.LandReqBase):
    """A LandReqBase home with the pass pointed at a fixture script.

    THE SWITCH STAYS OFF WHILE AN ARM FILES, unless the arm is about the
    detached start: an arm that runs the pass in-process must not race a
    detached twin of itself for the same row."""

    #: Every knob the pass reads. The ones after the switch and the script
    #: are cleared before each arm, so a value in the runner's environment
    #: steers no arm.
    KNOBS = ("HELM_QWEN27_FINDINGS_TIMEOUT_S", "HELM_QWEN27_FINDINGS_READER",
             "HELM_QWEN27_FINDINGS_THINK", "HELM_QWEN27_FINDINGS_MAX_TOKENS",
             "HELM_QWEN27_FINDINGS_THINK_MAX_TOKENS")
    KEYS = ("HELM_QWEN27_FINDINGS", "HELM_LOCAL_REVIEW_SCRIPT") + KNOBS

    def setUp(self):
        super().setUp()
        saved = {k: os.environ.get(k) for k in self.KEYS}

        def restore():
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.addCleanup(restore)
        os.environ["HELM_QWEN27_FINDINGS"] = "off"
        for key in self.KNOBS:
            os.environ.pop(key, None)
        _configure_endpoints({READER: _READER_ENDPOINT})
        self.fixtures = 0

    def fixture(self, rc=0, body=CLEAN, stderr="", sleep=0, record="",
                release=""):
        """Write one fixture local-review.py and point the pass at it."""
        self.fixtures += 1
        path = os.path.join(self.tmp, "local-review-%d.py" % self.fixtures)
        with open(path, "w", encoding="utf-8") as f:
            f.write(FIXTURE % {"rc": rc, "body": body, "stderr": stderr,
                               "sleep": sleep, "record": record,
                               "release": release})
        os.environ["HELM_LOCAL_REVIEW_SCRIPT"] = path
        return path

    def row(self, lane="lane/findings"):
        return self.dispatch(ref=self.side, lane=lane, kind="review")

    def current(self, rid):
        current, err = dispatches.snapshot()
        self.assertFalse(err, err)
        return current[rid]

    def notes(self, rid):
        return list(self.current(rid).get("findings_notes") or ())

    def run_pass(self, rid):
        """The pass, in-process: the worker's own function under its lock."""
        return findingspass.run(rid)

    def line(self, rid):
        notes = self.notes(rid)
        self.assertEqual(len(notes), 1, notes)
        return findingspass.summary(notes[-1])

    def assertUnmoved(self, before, after):
        """THE NOTE MOVES NOTHING: every field of the row but its seq and its
        notes is what it was before the pass."""
        strip = ("seq", "findings_notes")

        def plain(row):
            # a restored checkpoint and a fresh fold spell a sequence as a
            # list and a tuple respectively; the VALUES are what must match
            return json.loads(json.dumps(
                {k: v for k, v in row.items() if k not in strip},
                sort_keys=True, default=list))
        self.assertEqual(plain(after), plain(before))
        self.assertEqual(after["status"], "open")
        self.assertEqual(after["seq"], before["seq"] + 1,
                         "the note is ONE event")


class EachOutcomeRecordsItsLineTest(FindingsBase):
    """Every exit code of the calling contract, and every way the pass itself
    can fail, records the right line — and moves nothing else on the row."""

    def outcome(self, **fixture):
        row = self.row()
        before = self.current(row["id"])
        self.fixture(**fixture)
        out, err = self.run_pass(row["id"])
        self.assertIsNone(err, err)
        self.assertEqual(out["id"], row["id"])
        self.assertUnmoved(before, self.current(row["id"]))
        return row, self.notes(row["id"])[-1]

    def test_exit_0_kept_0_is_NO_FINDINGS_and_says_it_is_not_clean(self):
        _row, note = self.outcome(rc=0, body=CLEAN)
        self.assertEqual(findingspass.summary(note),
                         "%s: no findings (complete, 5 reads). Not a review, "
                         "not an approval." % READER)
        self.assertEqual(note["status_line"],
                         CLEAN.strip().splitlines()[-1])
        # the whole output is stored by reference and reads back whole
        self.assertEqual(dispatches.read_brief_file(note["output_ref"],
                                                    note["output_bytes"]),
                         (CLEAN, None))

    def test_exit_0_kept_2_carries_each_kept_block_verbatim(self):
        row, note = self.outcome(rc=0, body=KEPT)
        self.assertEqual(findingspass.summary(note),
                         "%s: 2 finding(s) for the approving reviewer to "
                         "adjudicate (complete, 2 reads). Not a review, not an "
                         "approval." % READER)
        findings = note["findings"]
        self.assertIn("**[JUDGE: REAL 3/3]**\n1. File: `helm/alpha.py`",
                      findings)
        self.assertIn("**[JUDGE: REAL 2/3]**\n3. File: `helm/beta.py`",
                      findings)
        # each block under the section it was written in
        self.assertIn("## helm/alpha.py\n**[JUDGE: REAL 3/3]**", findings)
        self.assertIn("## helm/beta.py\n**[JUDGE: REAL 2/3]**", findings)
        # the judge's REJECTED finding is not a finding
        self.assertNotIn("rejected claim", findings)
        lines = findingspass.note_lines(self.current(row["id"]))
        self.assertIn("whole output: %s" % dispatches.brief_file_path(
            note["output_ref"]), "\n".join(lines))

    def test_exit_2_names_what_was_partial(self):
        _row, note = self.outcome(rc=2, body=PARTIAL)
        line = findingspass.summary(note)
        self.assertTrue(line.startswith("%s: PARTIAL read, not clean — "
                                        % READER), line)
        self.assertIn("errors=1", line)
        self.assertIn("READER ERRORS, so these were NOT read: helm/alpha.py",
                      line)
        self.assertIn("Nothing was kept from what WAS read", line)
        self.assertNotIn("no findings", line)

    def test_exit_3_is_an_ABSENT_review_and_never_no_findings(self):  # noqa: VACUOUS_ASSERTION — the absence of "no findings" is read off the same `line` asserted EQUAL to the whole ABSENT sentence one statement earlier
        _row, note = self.outcome(rc=3, body=UNREAD)
        line = findingspass.summary(note)
        self.assertEqual(line, "%s: ABSENT review (reader down or erroring), "
                         "not clean — READER ERROR: <urlopen error [Errno 111] "
                         "Connection refused>." % READER)
        self.assertNotIn("no findings", line)
        self.assertEqual(note["outcome"], "unread")

    def test_exit_1_could_not_start_is_one_line(self):  # noqa: VACUOUS_ASSERTION — the absent output reference is read off the same `note` whose rc and whole summary are asserted to exact values first
        _row, note = self.outcome(
            rc=1, body="", stderr="no endpoint: pass --endpoint or set "
            "$LOCAL_REVIEW_ENDPOINT\n")
        self.assertEqual(note["rc"], 1)
        self.assertEqual(findingspass.summary(note),
                         "%s: NOT RUN — local-review.py could not start: no "
                         "endpoint: pass --endpoint or set "
                         "$LOCAL_REVIEW_ENDPOINT. Not a review." % READER)
        self.assertNotIn("output_ref", note)

    def test_a_missing_script_is_one_line(self):  # noqa: VACUOUS_ASSERTION — the recorded line is asserted to its exact whole value and the returned row to be OPEN; nothing here is an absence
        row = self.row()
        before = self.current(row["id"])
        missing = os.path.join(self.tmp, "no-such-local-review.py")
        os.environ["HELM_LOCAL_REVIEW_SCRIPT"] = missing
        out, err = self.run_pass(row["id"])
        self.assertIsNotNone(out, err)
        self.assertUnmoved(before, self.current(row["id"]))
        self.assertEqual(self.line(row["id"]),
                         "%s: NOT RUN — the local-review script is missing at "
                         "%s. Not a review." % (READER, missing))
        self.assertEqual(out["status"], "open")

    def test_a_timeout_stops_the_script_and_is_one_line(self):  # noqa: VACUOUS_ASSERTION — the recorded line is asserted to its exact whole value and the fixture's own record to hold exactly one start and no end
        row = self.row()
        before = self.current(row["id"])
        record = os.path.join(self.tmp, "timeout-record")
        self.fixture(rc=0, body=CLEAN, sleep=120, record=record)
        os.environ["HELM_QWEN27_FINDINGS_TIMEOUT_S"] = "1"
        started = time.monotonic()
        out, err = self.run_pass(row["id"])
        elapsed = time.monotonic() - started
        self.assertIsNotNone(out, err)
        self.assertLess(elapsed, 60, "the bound did not stop the run")
        self.assertUnmoved(before, self.current(row["id"]))
        self.assertEqual(self.line(row["id"]),
                         "%s: NOT FINISHED — the pass ran past its 1 s bound "
                         "and was stopped. Not a review." % READER)
        # the fixture started and was stopped before it could finish
        with open(record, encoding="utf-8") as f:
            words = [line.split()[0] for line in f]
        self.assertEqual(words, ["start"])
        self.assertEqual(out["status"], "open")

    def test_an_exit_code_the_status_line_contradicts_is_never_clean(self):  # noqa: VACUOUS_ASSERTION — each case asserts the FAILED line POSITIVELY on the same summary string the absence of "no findings" is read from
        cases = ((0, UNREAD.replace("\n\n", "\n"), "says unread"),
                 (0, "no status line at all\n", "is missing"),
                 (3, CLEAN, "says complete"))
        for rc, body, said in cases:
            with self.subTest(rc=rc, said=said):
                row = self.row(lane="lane/contradiction-%d-%s"
                               % (rc, said.split()[-1]))
                self.fixture(rc=rc, body=body)
                out, err = self.run_pass(row["id"])
                self.assertIsNone(err, err)
                line = findingspass.summary(self.notes(row["id"])[-1])
                self.assertTrue(line.startswith("%s: FAILED — exit %d says "
                                                % (READER, rc)), line)
                self.assertIn(said, line)
                self.assertNotIn("no findings", line)

    def test_a_row_already_verdicted_gets_no_note_and_says_why(self):  # noqa: VACUOUS_ASSERTION — the refusal is asserted POSITIVELY (its reason is named) and the absent note is read off the same replayed row whose status is asserted "verdict"
        row = self.row()
        out, err = self.mark_verdict(row["id"], self.side, "reviewed",
                                     polarity="approve")
        self.assertIsNone(err, err)
        self.fixture(rc=0, body=CLEAN)
        got, why = self.run_pass(row["id"])
        self.assertIsNone(got)
        self.assertIn("nobody is left to adjudicate", why)
        self.assertEqual(self.current(row["id"])["status"], "verdict")
        self.assertNotIn("findings_notes", self.current(row["id"]))


class TheScriptsOutFileHasSomewhereToGoTest(FindingsBase):
    """MEASURED AGAINST THE REAL SCRIPT: it writes --out LAST, after every
    read, and a missing directory made it exit 1 after reading all five files
    of a real row. The worker's lock happened to create the directory, so an
    arm driving only `run` could not see it; this one drives `examine` on a
    home where the store has never existed."""

    def test_examine_on_a_fresh_home_reads_the_out_file(self):  # noqa: VACUOUS_ASSERTION — the absent store is the precondition, asserted before the run; the run is asserted to exact outcome, rc, kept and text, and the empty listing is read off the directory the run just created
        import shutil
        shutil.rmtree(findingspass.store_dir(), ignore_errors=True)
        self.assertFalse(os.path.isdir(findingspass.store_dir()))
        self.fixture(rc=0, body=CLEAN)
        fields, text = findingspass.examine(self.side, self.repo)
        self.assertEqual((fields["outcome"], fields["rc"], fields["kept"]),
                         ("complete", 0, 0))
        self.assertEqual(text, CLEAN)
        # the script's --out file is read and then removed, never left behind
        self.assertEqual([n for n in os.listdir(findingspass.store_dir())
                          if n.startswith("run-")], [])


class ExamineReadsOnlyAFullObjectIdTest(FindingsBase):
    """task/3431: the tip gate in front of the script is the FULL object id
    grammar — 40 lowercase hex (sha1) or 64 (sha256), and nothing between.
    `[0-9a-f]{40,64}` admitted 41 to 63 characters: no commit has such a
    name, so the script was started on a tip that names nothing (the
    overshoot ecc8d7c6093 cured in dispatches._FULL_TIP).

    Every arm passes a checkout that does not exist, so no arm starts the
    script: a tip the gate ADMITS stops at the next gate, which names the
    checkout, and a tip it REFUSES stops at the tip gate itself. The reason
    string therefore says which gate the tip reached."""

    def setUp(self):
        super().setUp()
        self.fixture(rc=0, body=CLEAN)
        self.gone = os.path.join(self.tmp, "no-such-checkout")
        self.refused = "the row names no full tip to read"
        self.admitted = ("the row's checkout %s is not readable here"
                         % self.gone)

    def reached(self, cases):
        """[(label, outcome, reason, text)] for each (label, tip)."""
        out = []
        for label, tip in cases:
            fields, text = findingspass.examine(tip, self.gone)
            out.append((label, fields["outcome"], fields["reason"], text))
        return out

    def test_a_length_between_the_two_hashes_is_refused(self):  # noqa: VACUOUS_ASSERTION — every case's whole (outcome, reason, output) is asserted EQUAL to a non-empty literal list naming the exact gate it reached; the empty output is the script-not-started half of that exact value
        cases = (("41 hex", "1" * 41), ("50 hex", "5" * 50),
                 ("63 hex", "6" * 63))
        self.assertEqual(self.reached(cases),
                         [(label, "not-run", self.refused, "")
                          for label, _tip in cases])

    def test_sha1_and_sha256_lengths_pass_and_other_shapes_do_not(self):  # noqa: VACUOUS_ASSERTION — every case's whole (outcome, reason, output) is asserted EQUAL to a non-empty literal list naming the exact gate it reached; the empty output is the script-not-started half of that exact value
        cases = (("40 hex", "a" * 40, self.admitted),
                 ("64 hex", "b" * 64, self.admitted),
                 ("39 hex", "c" * 39, self.refused),
                 ("65 hex", "d" * 65, self.refused),
                 ("40 uppercase", "A" * 40, self.refused),
                 ("40 non-hex", "g" * 40, self.refused),
                 ("40 hex + newline", "e" * 40 + "\n", self.refused),
                 ("empty", "", self.refused))
        self.assertEqual(self.reached([(n, t) for n, t, _w in cases]),
                         [(n, "not-run", w, "") for n, _t, w in cases])


class NoFindingsIsEarnedAtTheLedgerTest(FindingsBase):
    """THE REPLAY SIDE of "an empty result is not a clean review": a note
    that would render as no findings is refused unless the script's own
    answer earned it, whoever wrote it."""

    STATUS = ("LOCAL-REVIEW-STATUS complete reads=5 errors=0 empty=0 cut=0 "
              "truncated=0 kept=0 rejected=0 unjudged=0")

    def write(self, rid, **fields):
        return dispatches.record_findings_note(rid, self.side, fields)

    def test_complete_needs_exit_0_a_matching_status_line_and_its_counts(self):  # noqa: VACUOUS_ASSERTION — every refusal is asserted POSITIVELY (out is None, the error names the rule), and the admitted control on the same row is asserted to land
        row = self.row()
        earned = dict(outcome="complete", rc=0, status_line=self.STATUS,
                      reads=5, kept=0)
        for drop, change in (("rc", {"rc": 3}), ("status", {"status_line":
                             self.STATUS.replace("complete", "unread")}),
                             ("counts", {"reads": None}),
                             ("line", {"status_line": None})):
            with self.subTest(case=drop):
                out, err = self.write(row["id"], **dict(earned, **change))
                self.assertIsNone(out)
                self.assertIn("refused by the reducer", err)
        self.assertNotIn("findings_notes", self.current(row["id"]))
        out, err = self.write(row["id"], **earned)
        self.assertIsNone(err, err)
        self.assertEqual(len(self.notes(row["id"])), 1)

    def test_a_note_for_another_tip_is_refused(self):
        row = self.row()
        out, err = dispatches.record_findings_note(
            row["id"], self.a, {"outcome": "not-run", "reason": "x"})
        self.assertIsNone(out)
        self.assertIn("refused by the reducer", err)
        out, err = dispatches.record_findings_note(
            row["id"], self.side, {"outcome": "not-run", "reason": "x"})
        self.assertIsNone(err, err)

    def test_a_terminal_escape_in_the_findings_is_refused(self):
        row = self.row()
        out, err = self.write(row["id"], outcome="not-run", reason="r",
                              findings="line one\x1b[2Jline two")
        self.assertIsNone(out)
        self.assertIn("refused by the reducer", err)
        # and the extractor replaces it before it ever reaches the writer
        body = KEPT.replace("the\nguard", "the\x1b[2J guard")
        cleaned = findingspass.extract_findings(body)
        self.assertNotIn("\x1b", cleaned)
        self.assertIn("?[2J guard", cleaned)

    def test_findings_past_the_cap_are_cut_and_the_cut_is_marked(self):
        block = "**[JUDGE: REAL 3/3]**\n" + "x" * 5000 + "\n"
        cut = findingspass.extract_findings(block)
        self.assertLessEqual(len(cut), dispatches.FINDINGS_TEXT_CAP)
        self.assertIn("[... cut:", cut)
        self.assertIn("of %d characters shown" % len(block.strip()), cut)
        self.assertTrue(dispatches.findings_text_ok(cut))


class ItIsNeverTheDifferentModelReadTest(FindingsBase):
    """NEVER AN APPROVAL, NEVER A GATE, NEVER THE DIFFERENT-MODEL READ.

    A row whose only reader is the qwen27 pass is exactly as owed as a row
    with no reader at all; the doors that could turn the note into a read
    refuse; and the real reader's approve is neither needed-less nor blocked
    by the note."""

    AUTHOR = "claude-opus-5-5"

    def on_behalf(self, rid, model, polarity):
        return dispatches.mark_verdict(
            rid, self.side, "read clean", polarity=polarity, basis="measured",
            bind_author=True, reviewer_model=model, reviewer_run="wf-1",
            author_model=self.AUTHOR)

    def test_an_approve_on_a_row_with_only_the_note_still_needs_its_reader(self):  # noqa: VACUOUS_ASSERTION — every refusal in the loop is asserted POSITIVELY by its message, and after it the same row is asserted OPEN, then to take a Fable read and a real approve unconditionally
        noted = self.row(lane="lane/noted")
        twin = self.row(lane="lane/twin")
        self.fixture(rc=0, body=CLEAN)
        out, err = self.run_pass(noted["id"])
        self.assertIsNone(err, err)
        self.assertEqual(self.notes(noted["id"])[-1]["outcome"], "complete")
        # THE LAND LADDER READS THE NOTED ROW EXACTLY AS THE UN-NOTED TWIN
        lr_noted, err = landreq.get(noted["id"])
        self.assertIsNone(err, err)
        lr_twin, err = landreq.get(twin["id"])
        self.assertIsNone(err, err)
        self.assertEqual(lr_noted["state"], lr_twin["state"])
        self.assertNotEqual(lr_noted["state"], "READY")
        lr_open = lr_noted["state"]
        # the qwen27 read cannot be recorded as the read, in either polarity
        for polarity in ("approve", "concur"):
            with self.subTest(polarity=polarity):
                got, err = self.on_behalf(noted["id"], READER, polarity)
                self.assertIsNone(got)
                self.assertIn("findings pass's model", err)
        current = self.current(noted["id"])
        self.assertEqual(current["status"], "open")
        self.assertNotIn("advisory_reads", current)
        # CONTROL: the same door takes another family's read, so the
        # refusal above is about the model and not the door
        got, err = self.on_behalf(noted["id"], "fable", "concur")
        self.assertIsNone(err, err)
        self.assertEqual(len(self.current(noted["id"])["advisory_reads"]), 1)
        self.assertEqual(self.current(noted["id"])["status"], "open")
        # AND THE REAL READER'S APPROVE IS NOT BLOCKED BY THE NOTE: never a
        # gate. The same approve on the un-noted twin lands the same state.
        for row in (noted, twin):
            out, err = self.mark_verdict(row["id"], self.side, "reviewed",
                                         polarity="approve")
            self.assertIsNone(err, err)
            self.assertEqual(out["status"], "verdict")
        lr_noted, err = landreq.get(noted["id"])
        self.assertIsNone(err, err)
        lr_twin, err = landreq.get(twin["id"])
        self.assertIsNone(err, err)
        self.assertEqual(lr_noted["state"], lr_twin["state"])
        self.assertNotEqual(lr_noted["state"], lr_open)
        self.assertEqual(len(self.current(noted["id"])["findings_notes"]), 1)

    def test_a_replayed_advisory_read_by_the_findings_model_is_inert(self):
        row = self.row()
        current = self.current(row["id"])
        event = {"v": 3, "event": "advisory-read", "seq": current["seq"] + 1,
                 "id": row["id"], "ts": dispatches.pk.now_ts(),
                 "reviewed_tip": self.side, "verdict_ref": "read clean",
                 "polarity": "concur", "reviewer_model": READER,
                 "reviewer_run": "wf-1", "author_model": self.AUTHOR,
                 "author_model_source": "declared",
                 "recorded_by": "integrator", "reviewer_family": READER,
                 "independence": "cross-family"}
        self.assertIsNotNone(dispatches._advisory_record(
            dict(event, reviewer_model="gpt-6-astra",
                 reviewer_family="codex"), current)[0],
            "the control shape is refused too, so this arm proves nothing")
        record, err = dispatches._advisory_record(event, current)
        self.assertIsNone(record)
        self.assertIn("independence", err)


class TheEndpointIsTheCatalogsTest(unittest.TestCase):
    """The reader family's seat_catalog entry names the endpoint and the
    model, both off one pool row; no host is written in the pass. The provider key is read generically, because the serving stack
    under that entry moves."""

    def setUp(self):
        import tempfile
        self.home = tempfile.mkdtemp(prefix="helm-test-findings-endpoint-")
        env = mock.patch.dict(os.environ, {"HELM_HOME": self.home})
        env.start()
        self.addCleanup(env.stop)
        # the patch restores the whole environment, this knob included
        os.environ.pop("HELM_QWEN27_FINDINGS_READER", None)

    def table(self, **entry):
        """{READER: entry}, shaped as a catalog entry: it names its model,
        and a pool row that declares no rung is the owner's own (`free`)."""
        pool = entry.get("pool_providers")
        if isinstance(pool, dict):
            entry["pool_providers"] = {
                key: dict({"rung": "free"}, **row) if isinstance(row, dict)
                else row for key, row in pool.items()}
        return {READER: dict({"model": READER}, **entry)}

    def test_only_the_owners_own_model_is_ever_sent_a_diff(self):
        """ONLY A ROW DECLARED `rung: free` IS A PROVIDER. The pass sends the
        row's diff to the endpoint with no key, so a vendor's metered row, or
        a row that declares no rung, is never one it reads from."""
        vendor = {"base_url": "https://api.vendor.invalid/v1",
                  "upstream_model": "vendor-model", "rung": "paid"}
        local = {"base_url": "http://local.invalid:8/v1",
                 "upstream_model": "local-model", "rung": "free"}
        url, model, why = findingspass.endpoint(self.table(
            pool_providers={"vendor": vendor, "local": local},
            pool_default="vendor"))
        self.assertIsNone(why, why)
        self.assertEqual((url, model), ("http://local.invalid:8/v1/chat/"
                                        "completions", "local-model"))
        url, model, why = findingspass.endpoint(self.table(
            pool_providers={"vendor": vendor}))
        self.assertEqual((url, model), (None, None))
        self.assertIn("vendor: its rung is paid", why)
        url, model, why = findingspass.endpoint({READER: {
            "model": READER,
            "pool_providers": {"bare": {"base_url": "http://bare.invalid/v1"}}}})
        self.assertEqual((url, model), (None, None))
        self.assertIn("bare: its rung is undeclared", why)

    def test_pool_default_wins_else_the_first_listed(self):
        pool = {"first": {"base_url": "http://one.invalid:1/v1/",
                          "upstream_model": "model-one"},
                "second": {"base_url": "http://two.invalid:2/v1",
                           "upstream_model": "model-two"}}
        url, model, why = findingspass.endpoint(
            self.table(pool_providers=pool, pool_default="second"))
        self.assertIsNone(why, why)
        self.assertEqual(url, "http://two.invalid:2/v1/chat/completions")
        # the model comes off the SAME row as the endpoint
        self.assertEqual(model, "model-two")
        url, model, why = findingspass.endpoint(self.table(pool_providers=pool))
        self.assertIsNone(why, why)
        self.assertEqual(url, "http://one.invalid:1/v1/chat/completions")
        self.assertEqual(model, "model-one")

    def test_no_entry_or_no_usable_provider_says_why(self):
        url, model, why = findingspass.endpoint({})
        self.assertEqual((url, model), (None, None))
        self.assertIn("no %s entry" % READER, why)
        url, model, why = findingspass.endpoint(self.table(pool_providers={
            "x": {"base_url": "ftp://nope"}}))
        self.assertEqual((url, model), (None, None))
        self.assertIn("no pool provider", why)
        url, model, why = findingspass.endpoint(self.table(pool_providers={
            "x": {"base_url": "http://ok.invalid/v1"}}))
        self.assertIsNone(why, why)
        self.assertEqual(url, "http://ok.invalid/v1/chat/completions")
        # a row naming no upstream model asks for the entry's own model
        self.assertEqual(model, READER)

    def test_a_row_and_an_entry_naming_no_model_is_no_provider(self):
        url, model, why = findingspass.endpoint({READER: {"pool_providers": {
            "x": {"base_url": "http://ok.invalid/v1"}}}})
        self.assertEqual((url, model), (None, None))
        self.assertIn("no pool provider", why)
        self.assertIn("x: the row names no model", why)

    def test_a_row_on_the_operators_box_resolves_through_the_endpoints_file(self):  # noqa: VACUOUS_ASSERTION — the None is the error channel of the call whose url is asserted to its exact value
        """A row with base_url_from and no base_url is the shape the shipped
        catalog gives the reader. Reading base_url directly found NO provider
        for it, so every note read NOT RUN while the endpoint was set."""
        pool = {"local": {"base_url_from": READER}}
        _configure_endpoints({READER: _READER_ENDPOINT + "/"})
        url, _model, why = findingspass.endpoint(self.table(pool_providers=pool))
        self.assertIsNone(why, why)
        self.assertEqual(url, _READER_ENDPOINT + "/chat/completions")

    def test_an_unconfigured_endpoint_is_not_run_and_names_the_file(self):
        pool = {"local": {"base_url_from": READER}}
        path = _configure_endpoints(None)
        url, _model, why = findingspass.endpoint(self.table(pool_providers=pool))
        self.assertIsNone(url)
        self.assertIn("no pool provider", why)
        self.assertIn(path, why)
        self.assertIn("not configured", why)
        _configure_endpoints({READER: "ftp://nope"})
        url, _model, why = findingspass.endpoint(self.table(pool_providers=pool))
        self.assertIsNone(url)
        self.assertIn("not an http(s) URL", why)

    def test_an_unconfigured_default_falls_to_a_provider_that_resolves(self):
        pool = {"local": {"base_url_from": READER},
                "public": {"base_url": "http://two.invalid:2/v1"}}
        _configure_endpoints(None)
        url, _model, why = findingspass.endpoint(
            self.table(pool_providers=pool, pool_default="local"))
        self.assertIsNone(why, why)
        self.assertEqual(url, "http://two.invalid:2/v1/chat/completions")
        _configure_endpoints({READER: _READER_ENDPOINT})
        url, _model, why = findingspass.endpoint(
            self.table(pool_providers=pool, pool_default="local"))
        self.assertIsNone(why, why)
        self.assertEqual(url, _READER_ENDPOINT + "/chat/completions")

    def test_the_shipped_catalog_resolves_and_the_pass_writes_no_host(self):  # noqa: VACUOUS_ASSERTION — the resolved url is asserted to be one of the catalog's own bases, and the no-host scan reads the source this module is loaded from
        from helm import seat, seat_catalog  # noqa: F401 — the facade first (seat_compat)
        _configure_endpoints({READER: _READER_ENDPOINT + "/"})
        url, model, why = findingspass.endpoint()
        self.assertIsNone(why, why)
        entry = seat_catalog.FAMILIES[READER]
        bases = [seat_catalog.pool_base_url(p)[0] for p in
                 entry["pool_providers"].values()]
        self.assertIn(url[:-len("/chat/completions")], bases)
        self.assertEqual(url, _READER_ENDPOINT + "/chat/completions")
        self.assertIn(model, [p.get("upstream_model") for p in
                              entry["pool_providers"].values()])
        with open(findingspass.__file__, encoding="utf-8") as f:
            source = f.read()
        import re
        self.assertIsNone(re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", source),
                          "an IP literal is written in the pass")
        self.assertNotIn("http://", source.replace('"http://"', ""))


class TheInvocationContractTest(FindingsBase):
    """The script is called the way its owner specified: the row's tip, the
    judge on, the row's checkout, helm's checklist, the catalog endpoint."""

    def test_the_argv_is_the_calling_contract(self):
        row = self.row()
        record = os.path.join(self.tmp, "argv-record")
        self.fixture(rc=0, body=CLEAN, record=record)
        out, err = self.run_pass(row["id"])
        self.assertIsNotNone(out, err)
        with open(record, encoding="utf-8") as f:
            argv = f.readline().split()[3:]
        url, _model, _why = findingspass.endpoint()
        self.assertEqual(argv[0], self.side)
        self.assertIn("--judge", argv)
        self.assertEqual(argv[argv.index("--repo") + 1],
                         self.current(row["id"])["repo_root"])
        self.assertEqual(argv[argv.index("--checklist") + 1],
                         findingspass.checklist_path())
        self.assertTrue(os.path.isfile(findingspass.checklist_path()))
        self.assertEqual(argv[argv.index("--endpoint") + 1], url)
        self.assertIn("--out", argv)


#: A reader family that exists only in the arms that patch it into the
#: catalog, with its own pool row, endpoint key and upstream model id: an argv
#: that carries them was resolved by name, never by the qwen27 default.
_FIXTURE_READER = "fixture-reader"
_FIXTURE_MODEL = "Fixture/Reader-9B"
_FIXTURE_ENDPOINT = "http://192.0.2.20:8081/v1"
_FIXTURE_ENTRY = {"model": _FIXTURE_READER, "probe_models": (_FIXTURE_MODEL,),
                  "pool_default": "local",
                  "pool_providers": {"local": {
                      "base_url_from": _FIXTURE_READER,
                      "upstream_model": _FIXTURE_MODEL, "rung": "free"}}}


def _flag(argv, name):
    """The value after `name` in a recorded argv, or None when it is absent."""
    return argv[argv.index(name) + 1] if name in argv else None


class TheReadIsAskedToConcludeTest(FindingsBase):
    """THE PASS NAMES THE READ'S BUDGET, MODEL AND THINKING ON EVERY RUN, and
    the reader family is a knob resolved from the catalog by name.

    Left to the script's defaults the budget is 4096 tokens, and on the
    stored outputs of 423 qwen27 reads at that budget, 98 ran to exactly 4096
    and were cut while still reasoning. The defaults here are the model
    makers' recommended output lengths: 16384 with thinking off and 32768
    with it on."""

    def one_run(self, lane):
        """(the argv the script was started with, the row's newest note)."""
        row = self.row(lane=lane)
        record = os.path.join(self.tmp, "argv-" + lane.replace("/", "-"))
        self.fixture(rc=0, body=CLEAN, record=record)
        out, err = self.run_pass(row["id"])
        self.assertIsNotNone(out, err)
        with open(record, encoding="utf-8") as f:
            argv = f.readline().split()[3:]
        return argv, self.notes(row["id"])[-1]

    def catalog_model(self):
        """The upstream model id on the pool row seat_catalog's qwen27 entry
        defaults to, read off the catalog rather than asked of the pass."""
        from helm import seat, seat_catalog  # noqa: F401 — the facade first (seat_compat)
        entry = seat_catalog.FAMILIES[READER]
        return entry["pool_providers"][entry["pool_default"]]["upstream_model"]

    def with_fixture_reader(self):
        from helm import seat, seat_catalog  # noqa: F401 — the facade first (seat_compat)
        patch = mock.patch.dict(seat_catalog.FAMILIES,
                                {_FIXTURE_READER: _FIXTURE_ENTRY})
        patch.start()
        self.addCleanup(patch.stop)
        _configure_endpoints({READER: _READER_ENDPOINT,
                              _FIXTURE_READER: _FIXTURE_ENDPOINT})

    def test_thinking_off_names_its_budget_and_the_catalogs_model(self):
        argv, note = self.one_run("lane/concludes-off")
        self.assertEqual(_flag(argv, "--max-tokens"), "16384", argv)
        self.assertEqual(_flag(argv, "--model"), self.catalog_model(), argv)
        # the judge asks the reader's endpoint, so it names the same model
        self.assertEqual(_flag(argv, "--judge-model"), self.catalog_model(),
                         argv)
        self.assertNotIn("--think", argv)
        self.assertEqual(note["reader"], READER)

    def test_thinking_on_passes_think_and_its_own_budget(self):
        os.environ["HELM_QWEN27_FINDINGS_THINK"] = "on"
        # the thinking-off budget does not steer a thinking read
        os.environ["HELM_QWEN27_FINDINGS_MAX_TOKENS"] = "8192"
        argv, _note = self.one_run("lane/concludes-on")
        self.assertIn("--think", argv)
        self.assertEqual(_flag(argv, "--max-tokens"), "32768", argv)

    def test_each_budget_has_one_knob(self):
        os.environ["HELM_QWEN27_FINDINGS_MAX_TOKENS"] = "8192"
        argv, _note = self.one_run("lane/concludes-knob-off")
        self.assertEqual(_flag(argv, "--max-tokens"), "8192", argv)
        os.environ["HELM_QWEN27_FINDINGS_THINK"] = "on"
        os.environ["HELM_QWEN27_FINDINGS_THINK_MAX_TOKENS"] = "40960"
        argv, _note = self.one_run("lane/concludes-knob-on")
        self.assertIn("--think", argv)
        self.assertEqual(_flag(argv, "--max-tokens"), "40960", argv)

    def test_a_value_that_is_not_a_positive_integer_is_ignored(self):  # noqa: VACUOUS_ASSERTION — both loops run over non-empty literal tuples, and every subtest asserts an exact budget or an exact boolean
        for bad in ("", " ", "abc", "0", "-5", "12.5"):
            with self.subTest(value=bad):
                os.environ["HELM_QWEN27_FINDINGS_THINK"] = "off"
                os.environ["HELM_QWEN27_FINDINGS_MAX_TOKENS"] = bad
                self.assertEqual(findingspass.max_tokens(), 16384)
                os.environ["HELM_QWEN27_FINDINGS_THINK"] = "on"
                os.environ["HELM_QWEN27_FINDINGS_THINK_MAX_TOKENS"] = bad
                self.assertEqual(findingspass.max_tokens(), 32768)
        for word, on in (("on", True), ("1", True), ("yes", True),
                         ("true", True), (" ON ", True), ("off", False),
                         ("0", False), ("", False), ("maybe", False)):
            with self.subTest(think=word):
                os.environ["HELM_QWEN27_FINDINGS_THINK"] = word
                self.assertIs(findingspass.thinking(), on)

    def test_the_bound_scales_with_the_budget_and_its_knob_wins(self):
        """A read that runs to its cap takes time in proportion to the cap,
        so the default bound is 3600 s per 16384 tokens of budget and never
        less than 3600 s."""
        self.assertEqual(findingspass.timeout_s(), 3600)
        os.environ["HELM_QWEN27_FINDINGS_THINK"] = "on"
        self.assertEqual(findingspass.timeout_s(), 7200)
        os.environ["HELM_QWEN27_FINDINGS_THINK_MAX_TOKENS"] = "65536"
        self.assertEqual(findingspass.timeout_s(), 14400)
        os.environ["HELM_QWEN27_FINDINGS_THINK"] = "off"
        os.environ["HELM_QWEN27_FINDINGS_MAX_TOKENS"] = "4096"
        self.assertEqual(findingspass.timeout_s(), 3600)
        os.environ["HELM_QWEN27_FINDINGS_TIMEOUT_S"] = "900"
        self.assertEqual(findingspass.timeout_s(), 900)

    def test_the_reader_is_a_knob_resolved_from_the_catalog_by_name(self):  # noqa: VACUOUS_ASSERTION — every observable is asserted to an exact value: the endpoint, both model flags, the note's reader and its whole line
        self.with_fixture_reader()
        os.environ["HELM_QWEN27_FINDINGS_READER"] = _FIXTURE_READER
        argv, note = self.one_run("lane/concludes-reader")
        self.assertEqual(_flag(argv, "--endpoint"),
                         _FIXTURE_ENDPOINT + "/chat/completions", argv)
        self.assertEqual(_flag(argv, "--model"), _FIXTURE_MODEL, argv)
        self.assertEqual(_flag(argv, "--judge-model"), _FIXTURE_MODEL, argv)
        self.assertEqual(note["reader"], _FIXTURE_READER)
        self.assertEqual(findingspass.summary(note),
                         "%s: no findings (complete, 5 reads). Not a review, "
                         "not an approval." % _FIXTURE_READER)

    def test_an_unknown_reader_family_is_not_run_and_says_why(self):  # noqa: VACUOUS_ASSERTION — the unstarted script is read off the same run whose note is asserted to its exact outcome, reader and whole line
        os.environ["HELM_QWEN27_FINDINGS_READER"] = "no-such-reader"
        row = self.row(lane="lane/concludes-unknown")
        record = os.path.join(self.tmp, "argv-unknown")
        self.fixture(rc=0, body=CLEAN, record=record)
        out, err = self.run_pass(row["id"])
        self.assertIsNone(err, err)
        note = self.notes(row["id"])[-1]
        self.assertEqual(note["outcome"], "not-run")
        self.assertEqual(note["reader"], "no-such-reader")
        self.assertEqual(findingspass.summary(note),
                         "no-such-reader: NOT RUN — no no-such-reader "
                         "endpoint: seat_catalog has no no-such-reader entry. "
                         "Not a review.")
        self.assertFalse(os.path.exists(record),
                         "the script was started with no reader to ask")

    def test_a_name_that_is_not_a_family_name_is_ignored(self):  # noqa: VACUOUS_ASSERTION — the loop runs over a non-empty literal tuple and each subtest asserts reader() EQUAL to an exact family name
        for raw, want in (("", READER), ("  ", READER), ("Qwen Local", READER),
                          ("../x", READER), ("x" * 65, READER),
                          (" QwenLocal ", "qwenlocal"),
                          ("qwenlocal", "qwenlocal")):
            with self.subTest(raw=raw):
                os.environ["HELM_QWEN27_FINDINGS_READER"] = raw
                self.assertEqual(findingspass.reader(), want)

    def test_a_read_by_one_reader_leaves_the_row_owed_the_others(self):  # noqa: VACUOUS_ASSERTION — the None is the skipped run's row channel, whose reason is asserted positively after the same row is asserted to carry both readers' notes exactly
        self.with_fixture_reader()
        row = self.row(lane="lane/concludes-two")
        self.fixture(rc=0, body=CLEAN)
        out, err = self.run_pass(row["id"])
        self.assertIsNone(err, err)
        os.environ["HELM_QWEN27_FINDINGS_READER"] = _FIXTURE_READER
        out, err = self.run_pass(row["id"])
        self.assertIsNone(err, err)
        self.assertEqual([n["reader"] for n in self.notes(row["id"])],
                         [READER, _FIXTURE_READER])
        out, err = self.run_pass(row["id"])
        self.assertIsNone(out)
        self.assertIn("already carries a read", err)

    def test_the_ledger_keeps_the_reader_that_read_and_refuses_a_malformed_one(self):  # noqa: VACUOUS_ASSERTION — each refusal is asserted POSITIVELY by its reducer message, and the admitted control on the same row is asserted to land with its reader
        row = self.row(lane="lane/concludes-ledger")
        for bad in ("", "not a family", "x" * 65, "a\x1bb"):
            with self.subTest(reader=bad):
                out, err = dispatches.record_findings_note(
                    row["id"], self.side, {"outcome": "not-run",
                                           "reason": "r", "reader": bad})
                self.assertIsNone(out)
                self.assertIn("refused by the reducer", err)
        out, err = dispatches.record_findings_note(
            row["id"], self.side, {"outcome": "not-run", "reason": "r",
                                   "reader": "qwenlocal"})
        self.assertIsNone(err, err)
        self.assertEqual([n["reader"] for n in self.notes(row["id"])],
                         ["qwenlocal"])

    def test_the_configured_readers_model_is_never_the_different_model_read(self):  # noqa: VACUOUS_ASSERTION — the admitted control runs unconditionally before the loop, and each refusal in the loop over a literal tuple is asserted positively by its message
        self.with_fixture_reader()
        author = "claude-opus-5-5"
        # CONTROL: with the knob unset the same model is admitted, so the
        # refusal below is the knob's and not the door's
        given, err = dispatches._on_behalf_shape(_FIXTURE_READER, "wf-1",
                                                 author, "concur")
        self.assertIsNone(err, err)
        self.assertEqual(given["reviewer_model"], _FIXTURE_READER)
        os.environ["HELM_QWEN27_FINDINGS_READER"] = _FIXTURE_READER
        for model in (_FIXTURE_READER, READER):
            with self.subTest(model=model):
                given, err = dispatches._on_behalf_shape(model, "wf-1",
                                                         author, "concur")
                self.assertIsNone(given)
                self.assertIn("findings pass's model", err)


class TheOffSwitchAndTheStartTest(FindingsBase):
    """The switch starts nothing; on, a review row is started detached in its
    own session, and a start that fails still leaves the row its one line."""

    def test_off_starts_nothing_and_on_starts_a_detached_worker(self):
        row = {"id": "ab" * 16, "kind": "review", "tip": self.side}
        popen = mock.Mock()
        os.environ["HELM_QWEN27_FINDINGS"] = "off"
        self.assertEqual(findingspass.queue(row, popen=popen), (False, None))
        self.assertFalse(popen.called)
        os.environ["HELM_QWEN27_FINDINGS"] = "on"
        self.assertEqual(findingspass.queue(dict(row, kind="build"),
                                            popen=popen), (False, None))
        self.assertFalse(popen.called)
        self.assertEqual(findingspass.queue(row, popen=popen), (True, None))
        self.assertEqual(popen.call_count, 1)
        argv = popen.call_args[0][0]
        self.assertEqual(argv[:len(findingspass.WORKER_ARGV)],
                         findingspass.WORKER_ARGV)
        self.assertEqual(argv[-3:], ["-m", "helm.findingspass", "ab" * 16])
        self.assertTrue(popen.call_args[1]["start_new_session"])
        # the shell that backgrounds the worker is reaped, never left running
        self.assertEqual(popen.return_value.wait.call_count, 1)

    def test_a_start_that_fails_still_leaves_one_line(self):
        def boom(*_a, **_k):
            raise OSError("no processes left")
        os.environ["HELM_QWEN27_FINDINGS"] = "on"
        started, why = findingspass.queue(
            {"id": "ab" * 16, "kind": "review"}, popen=boom)
        self.assertFalse(started)
        self.assertIn("no processes left", why)
        with mock.patch.object(findingspass, "queue",
                               return_value=(False, "OSError: boom")):
            row = self.row()
        self.assertEqual(self.current(row["id"])["status"], "open")
        self.assertEqual(self.line(row["id"]),
                         "%s: NOT RUN — the pass could not be started: "
                         "OSError: boom. Not a review." % READER)

    def test_off_files_a_review_row_with_no_note(self):  # noqa: VACUOUS_ASSERTION — the row is asserted OPEN and present on the same snapshot the absent key is read from
        os.environ["HELM_QWEN27_FINDINGS"] = "off"
        row = self.row()
        current = self.current(row["id"])
        self.assertEqual(current["status"], "open")
        self.assertNotIn("findings_notes", current)


class DetachedAndQueuedTest(FindingsBase):
    """THE REAL START: `dispatches.add` of a review row starts the worker in
    its own process, filing returns while it sleeps, and two passes never
    overlap. The bounds are generous because the box is shared."""

    def wait_for_notes(self, rids, bound=180):
        deadline = time.monotonic() + bound
        while time.monotonic() < deadline:
            if all(self.notes(rid) for rid in rids):
                return
            time.sleep(0.05)
        self.fail("no note landed within %ds; worker log: %s"
                  % (bound, self.worker_log()))

    def worker_log(self):
        try:
            with open(findingspass.log_path(), encoding="utf-8") as f:
                return f.read()[-2000:]
        except OSError as exc:
            return "(unreadable: %s)" % exc

    def test_filing_returns_promptly_while_the_pass_sleeps(self):  # noqa: VACUOUS_ASSERTION — the absent note right after filing is the timing precondition; the SAME row is then asserted to carry the exact no-findings line once the pass lands
        record = os.path.join(self.tmp, "held-record")
        release = os.path.join(self.tmp, "held-release")
        self.fixture(rc=0, body=CLEAN, record=record, release=release)
        os.environ["HELM_QWEN27_FINDINGS"] = "on"
        started = time.monotonic()
        row = self.row()
        filed = time.monotonic() - started
        self.assertLess(filed, 12, "filing waited on the pass")
        self.assertNotIn("findings_notes", self.current(row["id"]),
                         "a note landed while the fixture was held, so "
                         "the timing above measured nothing")
        # the pass really is running (and held) now, and still no note
        words, deadline = [], time.monotonic() + 180
        while not words and time.monotonic() < deadline:
            try:
                with open(record, encoding="utf-8") as f:
                    words = [ln.split()[0] for ln in f if ln.endswith("\n")]
            except FileNotFoundError:
                pass
            time.sleep(0 if words else 0.05)
        self.assertEqual(words, ["start"], self.worker_log())
        self.assertNotIn("findings_notes", self.current(row["id"]))
        with open(release, "w", encoding="utf-8"):
            pass
        self.wait_for_notes([row["id"]])
        self.assertEqual(self.line(row["id"]),
                         "%s: no findings (complete, 5 reads). Not a review, "
                         "not an approval." % READER)

    def test_two_filings_run_one_after_the_other_under_the_lock(self):  # noqa: VACUOUS_ASSERTION — two recorded runs are asserted (len == 2) before the non-overlap is compared, and both rows are awaited to carry a note
        record = os.path.join(self.tmp, "queue-record")
        self.fixture(rc=0, body=CLEAN, sleep=1, record=record)
        os.environ["HELM_QWEN27_FINDINGS"] = "on"
        first = self.row(lane="lane/first")
        second = self.row(lane="lane/second")
        self.wait_for_notes([first["id"], second["id"]])
        with open(record, encoding="utf-8") as f:
            marks = [line.split()[:3] for line in f]
        runs = {}
        for word, stamp, pid in marks:
            runs.setdefault(pid, {})[word] = float(stamp)
        self.assertEqual(len(runs), 2, marks)
        spans = sorted((r["start"], r["end"]) for r in runs.values())
        self.assertLessEqual(spans[0][1], spans[1][0],
                             "two passes overlapped: %r" % (spans,))


class TheReviewerSeesTheNoteTest(FindingsBase):
    """Where the approving reviewer reads the row: triage on a named row, and
    both verdict paths."""

    def cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = dispatches.cmd_dispatch(args)
        return rc, out.getvalue(), err.getvalue()

    def noted(self, body=UNREAD, rc=3):
        row = self.row()
        self.fixture(rc=rc, body=body)
        out, err = self.run_pass(row["id"])
        self.assertIsNone(err, err)
        return row

    def test_triage_on_a_named_row_prints_the_note(self):
        row = self.noted()
        rc, out, err = self.cli(["triage", row["id"]])
        self.assertEqual(rc, 0, err)
        self.assertIn("ABSENT review (reader down or erroring), not clean",
                      out)
        self.assertIn("LOCAL-REVIEW-STATUS unread reads=0", out)
        self.assertIn("whole output:", out)

    def test_the_verdict_path_says_the_note_back(self):
        row = self.noted(body=KEPT, rc=0)
        with self.verdict_author():
            rc, out, err = self.cli(["verdict", row["id"], self.side,
                                     "--concur", "--measured", "read",
                                     "clean"])
        self.assertEqual(rc, 0, err)
        self.assertIn("helm dispatch: findings pass (", out)
        self.assertIn("2 finding(s) for the approving reviewer to adjudicate",
                      out)
        self.assertIn("helm dispatch:     **[JUDGE: REAL 3/3]**", out)

    def test_the_advisory_verdict_path_says_the_note_back(self):
        row = self.noted()
        rc, out, err = self.cli(["verdict", row["id"], self.side, "--concur",
                                 "--measured", "--reviewer-model", "fable",
                                 "--reviewer-run", "wf-1", "--author-model",
                                 "claude-opus-5-5", "read", "clean"])
        self.assertEqual(rc, 0, err)
        self.assertIn("ADVISORY read by model fable", out)
        self.assertIn("ABSENT review (reader down or erroring)", out)



#: A local-review.py stand-in that, while it runs, asks for the dispatch
#: ledger lock from its own process and records whether it could have it.
LOCK_PROBE = '''import fcntl, os, sys
argv = sys.argv[1:]
fd = os.open(%(lock)r, os.O_RDWR | os.O_CREAT, 0o600)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    word = "free"
    fcntl.flock(fd, fcntl.LOCK_UN)
except BlockingIOError:
    word = "held"
os.close(fd)
with open(%(record)r, "a") as f:
    f.write(word + "\\n")
sys.stdout.write(%(body)r)
if "--out" in argv:
    with open(argv[argv.index("--out") + 1], "w") as f:
        f.write(%(body)r)
sys.exit(0)
'''


class TheLedgerLockCoversOnlyTheWriteTest(FindingsBase):
    """THE DISPATCH LEDGER LOCK IS TAKEN FOR THE WRITE, NEVER FOR THE FOLD
    AND NEVER FOR THE MODEL CALL.

    A fold held under that lock makes every `dispatch send` and `dispatch
    verdict` in the fleet wait for it, and a cold fold is minutes under load.
    The model call must never be under that lock either. Each arm asks for
    the lock from a separate descriptor at the moment in question, so the
    answer is what any other writer would have met."""

    NOTE = {"outcome": "not-run", "reason": "the arm's own note"}

    def lock_free(self):
        fd = os.open(dispatches.ledger_path() + ".lock",
                     os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return True
        finally:
            os.close(fd)

    def legacy_verdict(self, rid, seq):
        """Another writer's append, landed with no lock of ours held: the
        undeclared verdict shape `CloseBase.verdict_row` writes. `seq` is the
        row's, read BEFORE the arm patches the snapshot it counts."""
        self.assertTrue(eventledger.append_unlocked(dispatches.ledger_path(), {
            "v": 3, "event": "verdict", "seq": seq + 1, "id": rid,
            "ts": dispatches.pk.now_ts(), "reviewed_tip": self.side,
            "verdict_ref": "legacy review"}))

    def test_the_fold_and_the_checkpoint_advance_run_with_the_lock_free(self):  # noqa: VACUOUS_ASSERTION — the None is the error channel of the call whose note is asserted to land (len == 1), and both probe lists are asserted to exact values
        row = self.row()
        probes = {"snapshot": [], "advance": []}
        real_snapshot = dispatches.snapshot
        real_advance = dispatches._advance_checkpoint

        def snapshot():
            probes["snapshot"].append(self.lock_free())
            return real_snapshot()

        def advance():
            probes["advance"].append(self.lock_free())
            return real_advance()
        with mock.patch.object(dispatches, "snapshot", side_effect=snapshot), \
                mock.patch.object(dispatches, "_advance_checkpoint",
                                  side_effect=advance):
            out, err = dispatches.record_findings_note(
                row["id"], self.side, dict(self.NOTE))
        self.assertIsNone(err, err)
        self.assertEqual(out["id"], row["id"])
        self.assertEqual(len(self.notes(row["id"])), 1)
        self.assertEqual(probes, {"snapshot": [True], "advance": [True]},
                         "the fold or the checkpoint advance ran while the "
                         "note held the dispatch ledger lock")

    def test_an_append_between_the_read_and_the_write_is_read_again(self):  # noqa: VACUOUS_ASSERTION — the None is the error channel of the call whose out.seq is asserted exactly; the re-read count and the bystander's status are asserted positively
        row = self.row()
        bystander = self.row(lane="lane/findings-bystander")
        before = self.current(row["id"])
        seq = self.current(bystander["id"])["seq"]
        real_snapshot = dispatches.snapshot
        reads = []

        def snapshot():
            got = real_snapshot()
            reads.append(1)
            if len(reads) == 1:
                self.legacy_verdict(bystander["id"], seq)
            return got
        with mock.patch.object(dispatches, "snapshot", side_effect=snapshot):
            out, err = dispatches.record_findings_note(
                row["id"], self.side, dict(self.NOTE))
        self.assertIsNone(err, err)
        self.assertEqual(len(reads), 2, "the write did not read again after "
                                        "another writer's append")
        # the bystander's append took, so the re-read had something to see
        self.assertEqual(self.current(bystander["id"])["status"], "verdict")
        self.assertUnmoved(before, self.current(row["id"]))
        self.assertEqual(out["seq"], before["seq"] + 1)

    def test_a_row_answered_between_the_read_and_the_write_gets_no_note(self):
        row = self.row()
        seq = self.current(row["id"])["seq"]
        real_snapshot = dispatches.snapshot
        reads = []

        def snapshot():
            got = real_snapshot()
            reads.append(1)
            if len(reads) == 1:
                self.legacy_verdict(row["id"], seq)
            return got
        with mock.patch.object(dispatches, "snapshot", side_effect=snapshot):
            out, err = dispatches.record_findings_note(
                row["id"], self.side, dict(self.NOTE))
        self.assertIsNone(out, "a note landed on a row its read no longer "
                               "described")
        self.assertIn("lands only on a row still owed a review", err or "")
        after = self.current(row["id"])
        self.assertEqual(after["status"], "verdict")
        self.assertNotIn("findings_notes", after)

    def test_a_writer_that_lands_after_every_read_cannot_starve_the_note(self):  # noqa: VACUOUS_ASSERTION — the None is the error channel of the call whose out.seq is asserted exactly; the read count, the lock probes and every bystander's status are asserted to exact values
        """MEASURED WITH REAL PROCESSES, reviewing the first cut of this
        lane: a batch of back-to-back locked closes — fold under the lock,
        append, no pause, the shape of `helm lr retire --apply` — re-took
        the lock faster than an unlocked reader could read again, and the
        note was dropped after eight tries in 3 of 3 runs, while the locked
        writer before the lane landed it after one wait. Here another writer
        lands after EVERY read: every read but the last runs unlocked and
        misses, the last reads under the lock, and the note lands."""
        row = self.row()
        before = self.current(row["id"])
        tries = dispatches.FINDINGS_NOTE_TRIES
        bystanders = [self.row(lane="lane/findings-by-%d" % i)
                      for i in range(tries)]
        seqs = {b["id"]: self.current(b["id"])["seq"] for b in bystanders}
        real_snapshot = dispatches.snapshot
        reads, probes = [], []

        def snapshot():
            probes.append(self.lock_free())
            got = real_snapshot()
            by = bystanders[len(reads)]
            reads.append(1)
            self.legacy_verdict(by["id"], seqs[by["id"]])
            return got
        with mock.patch.object(dispatches, "snapshot", side_effect=snapshot):
            out, err = dispatches.record_findings_note(
                row["id"], self.side, dict(self.NOTE))
        self.assertIsNone(err, err)
        self.assertEqual(len(reads), tries, "the note gave up, or landed "
                                            "before its last try")
        self.assertEqual(probes, [True] * (tries - 1) + [False],
                         "every read but the last runs with the lock free; "
                         "the last reads under it")
        self.assertEqual({self.current(b["id"])["status"] for b in bystanders},
                         {"verdict"}, "a bystander's append did not take")
        self.assertEqual(len(self.notes(row["id"])), 1)
        self.assertUnmoved(before, self.current(row["id"]))
        self.assertEqual(out["seq"], before["seq"] + 1)

    def test_the_model_call_runs_with_the_lock_free(self):  # noqa: VACUOUS_ASSERTION — the None is the error channel of the run whose row id is asserted; the probe record is asserted to the exact list ['free'] and the note to its whole line
        """A PIN, GREEN BEFORE THIS CURE: measured, the worker never held the
        dispatch ledger lock across the model call, and this keeps it so."""
        row = self.row()
        record = os.path.join(self.tmp, "ledger-lock-probe")
        script = os.path.join(self.tmp, "local-review-lock-probe.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(LOCK_PROBE % {"lock": dispatches.ledger_path() + ".lock",
                                  "record": record, "body": CLEAN})
        os.environ["HELM_LOCAL_REVIEW_SCRIPT"] = script
        out, err = self.run_pass(row["id"])
        self.assertIsNone(err, err)
        self.assertEqual(out["id"], row["id"])
        with open(record, encoding="utf-8") as f:
            self.assertEqual(f.read().split(), ["free"],
                             "the model call ran under the dispatch ledger "
                             "lock")
        self.assertEqual(self.line(row["id"]),
                         "%s: no findings (complete, 5 reads). Not a review, "
                         "not an approval." % READER)


if __name__ == "__main__":
    unittest.main()
