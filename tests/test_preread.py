#!/usr/bin/env python3
"""Arms for `helm preread`, the non-authoritative council pre-read.

NO NETWORK LIVES HERE. Every reader and the judge go through one door,
`preread.post`, so a fake that returns canned drafts covers the whole remote
surface of the module. The diff under test is a FROZEN CAPTURE of a small real
change in this repository (tests/fixtures/preread-capture.diff, two files: one
module and its test), so what the splitter, the ordering and the citation check
are measured against is a real diff's shape rather than a shape invented to
suit them.

The arms that matter most are the ones with a control beside them: the citation
check must DROP and KEEP on the same run, triage must print the pointer and
then NOT print it for the same row, and the key-absence arm asserts the key
really did reach the judge's header before asserting it reached no surface —
an absence proved against a run that never carried the value proves nothing.
"""
import ast
import io
import json
import os
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-preread-", var="HELM_HOME")

from helm import cli, dispatches, home, preread  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "preread-capture.diff")
with open(FIXTURE, encoding="utf-8") as _f:
    DIFF = _f.read()

# A line the capture really adds, and one no line of it contains. Both are
# checked against the fixture by test_the_two_citation_probes_are_honest, so a
# fixture change that silences the citation arms goes red HERE rather than
# turning that arm into a pair of vacuous passes.
IN_DIFF = "from . import vcs"
NOT_IN_DIFF = "self.assertEqual(banana, kumquat)"

FAKE_KEY = "zz-not-a-real-key-0123456789abcdef"
READER_A = "http://reader-a.invalid/v1/chat/completions"
READER_B = "http://reader-b.invalid/v1/chat/completions"
JUDGE_URL = "http://judge.invalid/v1/chat/completions"


def _answer(text, cost=None):
    body = {"choices": [{"message": {"content": text}}]}
    if cost is not None:
        body["usage"] = {"cost": cost}
    return body


class Fake:
    """One canned endpoint. `calls` is the whole record: every url, payload and
    header set that left the module."""

    def __init__(self, reader_text=None, judge_text=None, fail=(),
                 cost=0.001):
        # `or` HERE WAS THE BUG THE FABRIC CAUGHT: an empty reader_text is
        # the exact input the empty-answer arm exists to plant, and a falsy
        # default swallowed it and handed the arm the canned drafts instead.
        # A fixture that overrides its own input tests no world.
        self.reader_text = ("1. `%s` is wrong.\n\n2. `%s` is also wrong."
                            % (IN_DIFF, NOT_IN_DIFF)) \
            if reader_text is None else reader_text
        self.judge_text = judge_text
        self.fail = set(fail)
        self.cost = cost
        self.calls = []

    def __call__(self, url, payload, headers, timeout):
        self.calls.append({"url": url, "payload": payload,
                           "headers": headers or {}, "timeout": timeout})
        text = json.dumps(payload)
        for needle in self.fail:
            if needle in text:
                raise OSError("canned failure for %s" % needle)
        if url == JUDGE_URL:
            return _answer(self.judge_text if self.judge_text is not None
                           else self._judged(text), cost=self.cost)
        return _answer(self.reader_text)

    def _judged(self, text):
        """The judge keeps what the drafts said, which is what makes the
        citation check the only thing standing between a draft and the file."""
        kept = []
        if IN_DIFF in text:
            kept.append("1. `%s` — drafts 3, confidence 70." % IN_DIFF)
        if NOT_IN_DIFF in text:
            kept.append("2. `%s` — drafts 1, confidence 40." % NOT_IN_DIFF)
        return "\n".join(kept) + "\n\nDropped: nothing this round."


def config(tmp, key_file=None, **over):
    cfg = {"readers": [{"url": READER_A, "model": "reader-one", "seeds": [1, 2],
                        "think": False},
                       {"url": READER_B, "model": "reader-two", "seeds": [1],
                        "think": False}],
           "judge": {"url": JUDGE_URL, "model": "judge-one"},
           "checklist": os.path.join(tmp, "checklist.md"),
           "cap_bytes": preread.CAP_BYTES, "workers": 2}
    if key_file:
        cfg["judge"]["key_from"] = key_file
    cfg.update(over)
    return cfg


class PrereadBase(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="helm-preread-case-")
        self.addCleanup(__import__("shutil").rmtree, self.tmp,
                        ignore_errors=True)
        with open(os.path.join(self.tmp, "checklist.md"), "w",
                  encoding="utf-8") as f:
            f.write("head\n---\n1. a defect class the readers are handed.\n")
        os.makedirs(preread.store_dir(), exist_ok=True)
        self.cfg = config(self.tmp)

    def run_one(self, fake, cfg=None, subject="row-under-test"):
        with mock.patch.object(preread, "diff_for",
                               return_value=(DIFF, None)):
            result, why = preread.run("tip-under-test", self.tmp, subject,
                                      cfg or self.cfg, transport=fake)
        self.assertIsNone(why, why)
        return result


class SplitTest(PrereadBase):
    def test_the_split_produces_one_chunk_per_diff_git_header(self):
        headers = [l for l in DIFF.splitlines() if l.startswith("diff --git ")]
        cs = preread.chunks(DIFF)
        self.assertEqual(len(cs), len(headers))
        self.assertEqual(len(cs), 2, "the frozen capture is a two-file change")
        for chunk in cs:
            self.assertTrue(chunk.startswith("diff --git "), chunk[:40])
        self.assertEqual([preread.chunk_path(c) for c in cs],
                         ["helm/scratch.py", "tests/test_scratch.py"])

    def test_code_is_read_before_tests_whatever_order_the_diff_had(self):
        """The control is the REVERSED input: a sort that did nothing would
        pass against a diff that already happened to be in the right order."""
        reversed_in = list(reversed(preread.chunks(DIFF)))
        self.assertEqual(preread.chunk_path(reversed_in[0]),
                         "tests/test_scratch.py")
        got = [path for path, _t, _c in preread.ordered(reversed_in)]
        self.assertEqual(got, ["helm/scratch.py", "tests/test_scratch.py"])

    def test_a_file_past_the_cap_is_split_by_hunk_never_cut(self):
        """The largest file in a change is the one the change is about; a cut
        at the cap loses the hunks a reader most needs. A file past the cap
        becomes parts of whole hunks, each under the cap, each carrying the
        file header and a part label, and nothing is truncated while the
        hunks allow it. The control is the uncapped run: one entry per file,
        no part label, no cut."""
        big = 10 ** 6
        whole = preread.ordered(preread.chunks(DIFF), cap_bytes=big)
        self.assertEqual([cut for _p, _t, cut in whole], [None, None])
        self.assertFalse(any("(part " in p for p, _t, _c in whole))
        multi = max(preread.chunks(DIFF), key=lambda c: c.count("\n@@ "))
        self.assertGreater(multi.count("\n@@ "), 1, "the fixture needs a two-hunk file")
        cap = len(multi.encode("utf-8")) - 1  # under that file, above its biggest hunk
        parts = preread.ordered(preread.chunks(DIFF), cap_bytes=cap)
        labelled = [(p, t, c) for p, t, c in parts if "(part " in p]
        self.assertTrue(labelled, "the file past the cap must split")
        for p, text, cut in labelled:
            self.assertIsNone(cut, "%s was cut instead of split" % p)
            self.assertNotIn("[TRUNCATED", text)
            self.assertTrue(text.startswith("diff --git "), "each part carries the header")
            self.assertIn("@@ ", text, "each part carries at least one hunk")
        self.assertRegex(labelled[0][0], r"\(part 1 of \d+\)")
        # every hunk of the split file survives, once, across its parts
        path = labelled[0][0].split(" (part")[0]
        original = [c for c in preread.chunks(DIFF) if preread.chunk_path(c) == path][0]
        self.assertEqual(sum(t.count("\n@@ ") for _p, t, _c in labelled),
                         original.count("\n@@ "))

    def test_a_single_hunk_past_the_cap_is_split_by_lines_not_cut(self):
        """The new-file shape: one hunk holding the whole module. It becomes
        parts of whole lines under the cap, each with the header, and not one
        is cut. The control is a cap no line fits under, the only case a cut
        is still declared."""
        one_hunk = max(preread.chunks(DIFF), key=lambda c: c.count("\n@@ "))
        header_end = one_hunk.index("@@ ")
        body = one_hunk[header_end:]
        cap = len(one_hunk.encode("utf-8")) // 2
        self.assertGreater(cap, len(one_hunk[:header_end].encode("utf-8")) + 80,
                           "the cap must leave room for the header and a line")
        parts = preread.ordered([one_hunk], cap_bytes=cap)
        self.assertGreater(len(parts), 1)
        for p, text, cut in parts:
            self.assertIsNone(cut, "%s was cut" % p)
            self.assertTrue(text.startswith("diff --git "))
            self.assertLessEqual(len(text.encode("utf-8")), cap)
        rebuilt = "".join(t[header_end:] for _p, t, _c in parts)
        self.assertEqual(rebuilt, body, "the parts must rebuild the hunk byte for byte")
        tiny = preread.ordered([one_hunk], cap_bytes=len(one_hunk[:header_end].encode("utf-8")) + 8)
        self.assertTrue(any(cut for _p, _t, cut in tiny), "a line longer than the cap is the one declared cut")


class CitationTest(PrereadBase):
    def test_the_two_citation_probes_are_honest(self):
        """Seeds the arms below with a must-hit: one probe IS in the capture's
        added lines and the other is in no line of it. Without this, a fixture
        edit would leave both citation arms passing for the wrong reason."""
        added = [l[1:].strip() for l in DIFF.splitlines() if l[:1] == "+"]
        self.assertTrue(any(IN_DIFF in line for line in added))
        self.assertFalse(any(NOT_IN_DIFF in line for line in DIFF.splitlines()))

    def test_a_quoted_line_in_the_diff_is_kept_and_one_that_is_not_is_dropped(self):
        text = ("1. `%s` — drafts 3, confidence 70.\n\n"
                "2. `%s` — drafts 1, confidence 40.\n\n"
                "Dropped: two readings whose own comment explains them."
                % (IN_DIFF, NOT_IN_DIFF))
        kept, dropped, notes = preread.grounded(text, DIFF)
        self.assertEqual(len(kept), 1, kept)
        self.assertEqual(len(dropped), 1, dropped)
        self.assertIn(IN_DIFF, kept[0])
        self.assertIn(NOT_IN_DIFF, dropped[0])
        self.assertTrue(any("Dropped:" in n for n in notes) or
                        any("Dropped:" in d for d in dropped))

    def test_none_found_is_no_findings_at_all_not_one_dropped(self):
        """The first live row answered exactly NONE FOUND and the file said
        `dropped: 1`. A count that moves when nothing was found is worse than
        no count: the reader acts on it."""
        kept, dropped, notes = preread.grounded("NONE FOUND", DIFF)
        self.assertEqual((kept, dropped), ([], []))
        self.assertEqual(notes, ["NONE FOUND"])
        # the control: the same function does still count a real finding
        kept, dropped, _n = preread.grounded("1. `%s` is wrong." % IN_DIFF,
                                             DIFF)
        self.assertEqual((len(kept), len(dropped)), (1, 0))

    def test_the_judges_dropped_paragraph_is_not_counted_as_a_finding(self):
        text = ("1. `%s` — drafts 3.\n\n"
                "Dropped: an except-pass whose comment says fail-open."
                % IN_DIFF)
        kept, dropped, notes = preread.grounded(text, DIFF)
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, [])
        self.assertEqual(len(notes), 0, notes)

    def test_a_merge_that_carries_nothing_falls_back_to_the_per_file_union(self):
        """The capacity failure one level up: eight per-file judgements went
        into one merge call on the first live row and it answered NONE FOUND.
        The per-file text is the evidence; the merge is a rendering of it."""
        calls = []
        fake = Fake()
        original = fake.__call__

        def empty_merge(url, payload, headers, timeout):
            body = json.dumps(payload)
            if url == JUDGE_URL and preread.MERGE_ASK[:40] in body:
                calls.append("merge")
                return _answer("NONE FOUND", cost=0.001)
            return original(url, payload, headers, timeout)

        result = self.run_one(empty_merge, subject="row-empty-merge")
        self.assertEqual(calls, ["merge"], "the merge really was asked")
        self.assertTrue(result["merge_empty"])
        self.assertEqual(len(result["kept"]), 2, result["kept"])
        self.assertEqual(result["judge_text"], "NONE FOUND")
        page = preread.render(result)
        self.assertIn("THE MERGE CALL CARRIED NOTHING", page)
        self.assertIn("- merge_empty: yes", page)
        self.assertIn(IN_DIFF, page)

    def test_both_directions_survive_a_whole_run(self):
        result = self.run_one(Fake())
        self.assertEqual(len(result["kept"]), 1, result["kept"])
        self.assertEqual(len(result["dropped"]), 1, result["dropped"])
        self.assertIn(IN_DIFF, result["kept"][0])
        self.assertIn(NOT_IN_DIFF, result["dropped"][0])


class FailureTest(PrereadBase):
    def test_a_reader_failure_on_one_file_leaves_it_not_read_and_judges_the_rest(self):  # noqa: VACUOUS_ASSERTION — the surviving file's kept findings and its judge call are the positive half
        fake = Fake(fail=("tests/test_scratch.py",))
        result = self.run_one(fake)
        self.assertEqual(result["not_read"], ["tests/test_scratch.py"])
        self.assertIn("helm/scratch.py", result["files"])
        self.assertTrue(result["kept"], "the other file was still judged")
        legs = {(f["leg"], f["file"]) for f in result["failures"]}
        self.assertEqual({leg for leg, _f in legs}, {"reader"})
        self.assertEqual({f for _l, f in legs}, {"tests/test_scratch.py"})
        judged = [c for c in fake.calls if c["url"] == JUDGE_URL]
        self.assertTrue(judged, "the surviving file still reached the judge")
        for call in judged:
            self.assertNotIn("tests/test_scratch.py",
                             json.dumps(call["payload"]))

    def test_a_judge_failure_is_read_but_not_judged_never_not_read(self):
        """A true status field can accuse the wrong party. A file whose drafts
        exist and whose judge leg died is not a file nobody read, and the two
        states ask for opposite repairs."""
        fake = Fake()
        original = fake.__call__

        def judge_dies(url, payload, headers, timeout):
            if url == JUDGE_URL and "FILE: helm/scratch.py" in json.dumps(payload):
                fake.calls.append({"url": url, "payload": payload,
                                   "headers": headers or {},
                                   "timeout": timeout})
                raise OSError("the judge leg is down")
            return original(url, payload, headers, timeout)

        result = self.run_one(judge_dies)
        self.assertTrue(any(c["url"] == JUDGE_URL for c in fake.calls),
                        "the control: the judge leg really was reached")
        self.assertEqual(result["not_judged"], ["helm/scratch.py"])
        self.assertEqual(result["not_read"], [],
                         "the readers did answer for that file")
        self.assertTrue(any(f["leg"] == "judge" for f in result["failures"]))
        self.assertTrue(result["kept"], "the other file was still judged")
        self.assertIn("- not_judged: helm/scratch.py",
                      preread.render(result))

    def test_an_empty_judge_answer_is_not_judged_and_never_a_clean_file(self):
        """The same thinking-model shape on the judge leg: drafts exist, the
        judge returns no content and no error. The file is not_judged, the
        header cannot say every file was judged, and the control is the other
        file, judged as before."""
        fake = Fake()
        original = fake.__call__

        def judge_blank(url, payload, headers, timeout):
            if url == JUDGE_URL and "FILE: helm/scratch.py" in json.dumps(payload):
                fake.calls.append({"url": url, "payload": payload,
                                   "headers": headers or {},
                                   "timeout": timeout})
                return _answer("", cost=0.001)
            return original(url, payload, headers, timeout)

        result = self.run_one(judge_blank)
        self.assertEqual(result["not_judged"], ["helm/scratch.py"])
        self.assertEqual(result["not_read"], [])
        self.assertEqual(result["judged_files"], len(result["files"]) - 1)
        self.assertTrue(any(f["leg"] == "judge" and f["why"] == "empty answer"
                            for f in result["failures"]), result["failures"])
        self.assertTrue(result["kept"], "the other file was still judged")

    def test_an_empty_answer_is_a_failure_and_not_a_clean_file(self):
        """A thinking model that spends its budget reasoning answers with no
        content and no error. Counting that as a read file is how a pre-read
        reports a clean bill for a file nothing looked at."""
        result = self.run_one(Fake(reader_text=""))
        self.assertEqual(sorted(result["not_read"]),
                         ["helm/scratch.py", "tests/test_scratch.py"])
        self.assertEqual(result["not_judged"], [])
        self.assertEqual(result["drafts"], 0)
        self.assertTrue(all(f["why"] == "empty answer"
                            for f in result["failures"]), result["failures"])


class OutputTest(PrereadBase):
    def test_the_judge_is_told_which_file_it_is_judging(self):
        """The ask requires every survivor to name its file and the drafts
        carry only a model and a seed, so the file has to be said."""
        fake = Fake()
        self.run_one(fake, subject="row-file-named")
        asked = [json.dumps(c["payload"]) for c in fake.calls
                 if c["url"] == JUDGE_URL]
        self.assertTrue(asked)
        self.assertTrue(any("FILE: helm/scratch.py" in a for a in asked),
                        "no judge call named the file it was judging")
        self.assertTrue(any("FILE: tests/test_scratch.py" in a
                            for a in asked))

    def test_the_file_header_and_the_ledger_line_carry_the_same_counts(self):
        result = self.run_one(Fake(cost=0.0025))
        path, line = preread.write(result)
        got = preread.facts(path)
        self.assertEqual(int(got["kept"]), line["kept"], got)
        self.assertEqual(int(got["dropped"]), line["dropped"])
        self.assertEqual(int(got["drafts"]), line["drafts"])
        self.assertEqual(got["judge"], line["judge"])
        self.assertEqual(got["ref"], line["ref"])
        self.assertEqual(float(got["cost"]), line["cost"])
        self.assertEqual(got["ts"], line["ts"])
        with open(preread.ledger_path(), encoding="utf-8") as f:
            written = [json.loads(l) for l in f if l.strip()]
        self.assertEqual(written[-1], line)
        self.assertEqual(sorted(line), ["cost", "drafts", "dropped", "judge",
                                        "kept", "readers", "ref", "row",
                                        "ts", "wall_s"])
        body = open(path, encoding="utf-8").read()
        self.assertIn("THIS IS NOT A VERDICT", body)
        self.assertIn(IN_DIFF, body)

    def test_a_judge_that_reports_no_cost_says_unknown_rather_than_zero(self):  # noqa: VACUOUS_ASSERTION — a priced run is measured first in the same method, on the same two surfaces
        """Zero and unmeasured are different facts about spend, and a ledger
        that folds them prints a free fleet."""
        priced, line = preread.write(self.run_one(Fake(cost=0.004),
                                                  subject="row-priced"))
        self.assertGreater(float(preread.facts(priced)["cost"]), 0)
        self.assertEqual(line["cost"], round(0.004 * 3, 6),
                         "three judge calls, each reporting its own cost")
        result = self.run_one(Fake(cost=None), subject="row-unpriced")
        self.assertIsNone(result["cost"])
        path, line = preread.write(result)
        self.assertIsNone(line["cost"])
        self.assertIn("UNKNOWN", preread.facts(path)["cost"])

    def test_a_finding_that_quotes_a_header_line_cannot_rewrite_the_counts(self):
        """The header block is the pointer's only source, so a model quoting a
        line shaped like a header field must not reach it."""
        fake = Fake(judge_text="1. `%s` — see `- kept: 99` below." % IN_DIFF)
        result = self.run_one(fake, subject="row-header-spoof")
        path, line = preread.write(result)
        self.assertEqual(int(preread.facts(path)["kept"]), line["kept"])
        self.assertNotEqual(line["kept"], 99)
        self.assertIn("- kept: 99", open(path, encoding="utf-8").read(),
                      "the control: the spoof really is in the file")

    def test_the_triage_pointer_appears_only_once_the_file_exists(self):
        rid = "row-triage-control"
        self.assertEqual(preread.triage_line(rid), "",
                         "the control: no file, no line")
        result = self.run_one(Fake(), subject=rid)
        path, _line = preread.write(result)
        line = preread.triage_line(rid)
        self.assertTrue(line.startswith("pre-read: " + path), line)
        self.assertIn("%d findings" % len(result["kept"]), line)
        self.assertIn("%d dropped" % len(result["dropped"]), line)
        self.assertIn("judge judge-one", line)
        os.remove(path)
        self.assertEqual(preread.triage_line(rid), "")


class NeverMintsTest(PrereadBase):
    def _preread_lines(self):
        try:
            with open(preread.ledger_path(), encoding="utf-8") as f:
                return len([l for l in f if l.strip()])
        except OSError:
            return 0

    def test_a_whole_preread_leaves_the_dispatch_ledger_untouched(self):  # noqa: VACUOUS_ASSERTION — the pre-read's own ledger is asserted to grow by one in the same run
        """The never-mints arm. A pre-read that could write that file could
        close a row, and weak models may not approve anything."""
        lines_before = self._preread_lines()
        ledger = dispatches.ledger_path()
        os.makedirs(os.path.dirname(ledger), exist_ok=True)
        with open(ledger, "a", encoding="utf-8") as f:
            f.write(json.dumps({"v": 3, "event": "dispatch", "seq": 0,
                                "id": "aaaa1111", "ts": "2026-01-01T00:00:00Z",
                                "recipient": "seat-a", "lane": "lane-a",
                                "ref": "tip-under-test", "status": "open"})
                    + "\n")
        before = open(ledger, "rb").read()
        result = self.run_one(Fake())
        preread.write(result)
        after = open(ledger, "rb").read()
        self.assertEqual(after.count(b"\n"), before.count(b"\n"))
        self.assertEqual(after, before,
                         "a pre-read rewrote the dispatch ledger")
        # THE POSITIVE CONTROL: the same run DID write, to its own ledger. An
        # unchanged dispatch ledger beside a pre-read that never ran would be
        # the same observable and would prove nothing. Counted as a DELTA
        # because the temp home is per process and every case in this module
        # appends to the same pre-read ledger.
        self.assertEqual(self._preread_lines(), lines_before + 1)


class ConfigTest(PrereadBase):
    def test_the_config_absent_refusal_names_the_path(self):
        missing = os.path.join(self.tmp, "not-here", "preread.json")
        cfg, why = preread.load_config(missing)
        self.assertIsNone(cfg)
        self.assertIn(missing, why)
        self.assertIn(preread.EXAMPLE, why)

    def test_a_config_with_no_judge_refuses_rather_than_judging_itself(self):
        path = os.path.join(self.tmp, "preread.json")
        body = config(self.tmp)
        body.pop("judge")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(body))
        cfg, why = preread.load_config(path)
        self.assertIsNone(cfg)
        self.assertIn("judge", why)
        self.assertIn(path, why)

    def test_a_good_config_loads(self):
        """The control for the two refusals above: the same writer, a complete
        body, and it must pass — a loader that refused everything would satisfy
        both arms."""
        path = os.path.join(self.tmp, "preread.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(config(self.tmp)))
        cfg, why = preread.load_config(path)
        self.assertIsNone(why, why)
        self.assertEqual(cfg["judge"]["model"], "judge-one")

    def test_an_unreadable_checklist_refuses_rather_than_running_unscaffolded(self):
        cfg = config(self.tmp, checklist=os.path.join(self.tmp, "gone.md"))
        text, why = preread.checklist_text(cfg)
        self.assertIsNone(text)
        self.assertIn("gone.md", why)

    def test_the_shipped_checklist_is_what_the_readers_are_handed(self):
        cfg = config(self.tmp)
        cfg.pop("checklist")
        text, why = preread.checklist_text(cfg)
        self.assertIsNone(why, why)
        self.assertIn("NONE FOUND", text)
        self.assertNotIn("# The pre-read checklist", text,
                         "the page's own preamble is not sent to the readers")


class KeyTest(PrereadBase):
    def test_the_key_reaches_the_judge_header_and_no_surface_at_all(self):  # noqa: VACUOUS_ASSERTION — the header equality above each absence is the positive control
        """Both halves in one run. The first assertion is the control: an
        absence proved against a run that never carried a key proves nothing
        about a module that leaks one."""
        key_file = os.path.join(self.tmp, "seat-config.yaml")
        with open(key_file, "w", encoding="utf-8") as f:
            f.write("providers:\n  - name: judge\n    api-keys:\n"
                    "      - api-key: %s\n" % FAKE_KEY)
        self.assertEqual(preread.judge_key(key_file), FAKE_KEY)
        cfg = config(self.tmp, key_file=key_file)
        fake = Fake()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            result = self.run_one(fake, cfg=cfg)
            path, line = preread.write(result)
        judged = [c for c in fake.calls if c["url"] == JUDGE_URL]
        self.assertTrue(judged)
        self.assertEqual(judged[0]["headers"].get("authorization"),
                         "Bearer " + FAKE_KEY)
        for c in fake.calls:
            if c["url"] != JUDGE_URL:
                self.assertNotIn("authorization", c["headers"],
                                 "a local reader was handed the judge's key")
        written = open(path, encoding="utf-8").read()
        ledger = open(preread.ledger_path(), encoding="utf-8").read()
        for surface, what in ((out.getvalue(), "stdout"),
                              (err.getvalue(), "stderr"),
                              (written, "the pre-read file"),
                              (ledger, "the pre-read ledger"),
                              (json.dumps(line), "the ledger line"),
                              (json.dumps(result, default=str),
                               "the result")):
            self.assertNotIn(FAKE_KEY, surface, "the key reached " + what)

    def test_a_file_with_no_key_line_answers_none_rather_than_a_fragment(self):  # noqa: VACUOUS_ASSERTION — a file that DOES carry a key line is read first, by the same reader
        keyed = os.path.join(self.tmp, "keyed.yaml")
        with open(keyed, "w", encoding="utf-8") as f:
            f.write("providers:\n  - name: judge\n    api-keys:\n"
                    "      - api-key: %s\n" % FAKE_KEY)
        self.assertEqual(preread.judge_key(keyed), FAKE_KEY,
                         "the control: this reader does find a real key line")
        path = os.path.join(self.tmp, "keyless.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write("providers:\n  - name: judge\n    # api-key: see the vault\n")
        self.assertIsNone(preread.judge_key(path))
        self.assertIsNone(preread.judge_key(os.path.join(self.tmp, "absent")))


class SeamTest(PrereadBase):
    def test_the_diff_is_taken_through_the_vcs_seam_and_not_a_spawn(self):  # noqa: VACUOUS_ASSERTION — the recorded argv and the returned diff are positive observables
        """The direct-spawn class this repository routes through one module.
        The fake backend is the whole proof: if `diff_for` ever spawned git
        itself, the recorded argv would be empty and this goes red."""
        seen = []

        class FakeBackend:
            def text(self, cwd, *args, **kw):
                seen.append((cwd, args))
                if args[0] == "merge-base":
                    return 0, "base-sha", ""
                return 0, DIFF, ""

        with mock.patch("helm.vcs.backend", return_value=FakeBackend()):
            diff, why = preread.diff_for("tip-under-test", self.tmp,
                                         trunk="origin/main")
        self.assertIsNone(why, why)
        self.assertEqual(diff, DIFF)
        self.assertEqual([args[0] for _cwd, args in seen],
                         ["merge-base", "diff"])
        self.assertEqual(seen[0][1][1:], ("origin/main", "tip-under-test"))
        self.assertIn("base-sha..tip-under-test", seen[1][1])

    def test_an_unreadable_merge_base_refuses_and_names_the_trunk(self):
        class Broken:
            def text(self, cwd, *args, **kw):
                return 128, "", "fatal: not a valid object name"

        with mock.patch("helm.vcs.backend", return_value=Broken()):
            diff, why = preread.diff_for("tip-under-test", self.tmp,
                                         trunk="origin/main")
        self.assertIsNone(diff)
        self.assertIn("origin/main", why)
        self.assertIn("tip-under-test", why)

    def test_the_module_spawns_nothing_itself(self):  # noqa: VACUOUS_ASSERTION — the urllib.request assertion proves the census really read this module's imports
        """The census, not a habit: this module may not import the spawn
        machinery at all, so a direct git call cannot be written here without
        first going red."""
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "helm", "preread.py"),
            encoding="utf-8").read()
        tree = ast.parse(src)
        imported = {a.name for n in ast.walk(tree)
                    if isinstance(n, ast.Import) for a in n.names}
        imported |= {n.module for n in ast.walk(tree)
                     if isinstance(n, ast.ImportFrom) and n.module}
        self.assertIn("urllib.request", imported, "the census reads imports")
        self.assertNotIn("subprocess", imported)
        self.assertNotIn("os.popen", src)


class TriageWiringTest(PrereadBase):
    def test_triage_prints_the_pointer_for_a_named_row_only_when_it_exists(self):  # noqa: VACUOUS_ASSERTION — the same row prints the line in the second half, which is the positive control
        """Drives the real triage printing loop for one named row, twice: once
        with a pre-read on disk and once without. The surrounding measurement
        is mocked so the only thing that varies between the two runs is the
        file."""
        rid = "bbbb2222cccc3333"
        row = {"id": rid, "recipient": "seat-a", "lane": "lane-under-test",
               "ts": "2026-01-01T00:00:00Z", "status": "open",
               "ref": "tip-under-test", "repo_root": self.tmp}

        def triage():
            out = io.StringIO()
            with mock.patch.object(dispatches, "snapshot",
                                   return_value=({rid: row}, None)), \
                    mock.patch.object(dispatches, "owed", return_value=[row]), \
                    mock.patch.object(dispatches, "_resolve_row",
                                      return_value=(row, None)), \
                    mock.patch.object(dispatches, "cwd_scope",
                                      return_value=(None, None, "no scope")), \
                    mock.patch.object(dispatches, "body_of",
                                      return_value=("", None)), \
                    mock.patch("helm.clearspan.re_measure",
                               return_value=("pending", "detail")), \
                    mock.patch("sys.stdout", out):
                dispatches._cmd_dispatch(["triage", rid])
            return out.getvalue()

        self.assertNotIn("pre-read:", triage(), "the control: no file, no line")
        result = self.run_one(Fake(), subject=rid)
        path, _line = preread.write(result)
        printed = triage()
        self.assertIn("pre-read: " + path, printed)
        self.assertIn("judge judge-one", printed)


class SurfaceTest(unittest.TestCase):
    def test_the_verb_is_registered_and_its_help_carries_the_synopsis(self):
        self.assertIn("preread", cli.VERBS)
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            rc = cli._main(["preread", "--help"])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("preread <dispatch-id>", text)
        self.assertIn("MINTS NO VERDICT", text)

    def test_the_synopsis_has_exactly_one_home(self):  # noqa: VACUOUS_ASSERTION — an equality between two live values, not an absence
        """`usage()` reads cli's entry rather than keeping a second copy, so
        the two can never drift."""
        self.assertEqual(preread.usage(), "helm " + cli._VERB_HELP["preread"])

    def test_the_reference_documents_the_verb_and_the_example_config(self):
        docs = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "docs")
        with open(os.path.join(docs, "VERBS.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("### `helm preread <dispatch-id>", text)
        self.assertIn("preread-config.example.json", text)
        self.assertIn("mints no verdict", text.lower())
        with open(os.path.join(docs, "preread-config.example.json"),
                  encoding="utf-8") as f:
            example = json.load(f)
        self.assertEqual(sorted(example["judge"]),
                         ["key_from", "max_tokens", "model", "timeout", "url"])
        self.assertTrue(example["readers"])

    def test_the_store_is_declared_and_is_not_a_squatter(self):
        from helm import registry
        names = {r["name"] for r in registry.projections()}
        for owed in ("preread-config", "prereads", "preread-ledger"):
            self.assertIn(owed, names)
        globs = {g for r in registry.projections() for g in r["globs"]}
        self.assertIn("_global/prereads/*.md", globs)
        self.assertIn("_global/preread.json", globs)

    def test_a_reader_answering_without_thinking_is_told_so_on_the_wire(self):
        """The one prototype fact a fresh reader cannot rediscover cheaply: a
        thinking model that exhausts max_tokens returns EMPTY content, so the
        flag is what makes an empty answer mean an empty answer."""
        body = preread.reader_payload({"model": "m", "think": False},
                                      "checklist", "helm/x.py", "diff")
        self.assertEqual(body["chat_template_kwargs"],
                         {"enable_thinking": False})
        self.assertEqual(body["max_tokens"], preread.READER_MAX_TOKENS)
        self.assertEqual(body["repeat_penalty"], 1.1)
        thinking = preread.reader_payload({"model": "m", "think": True},
                                          "checklist", "helm/x.py", "diff")
        self.assertNotIn("chat_template_kwargs", thinking)

    def test_every_remote_call_carries_max_tokens(self):
        reader = preread.reader_payload({"model": "m"}, "c", "p", "d")
        judge = preread.judge_payload({"model": "j"}, "ask", "drafts")
        self.assertEqual(reader["max_tokens"], preread.READER_MAX_TOKENS)
        self.assertEqual(judge["max_tokens"], preread.JUDGE_MAX_TOKENS)

    def test_home_is_a_temp_directory(self):
        """CONTRIBUTING's rule, asserted rather than assumed: these arms write
        a store, and a run against the real home would write the fleet's."""
        where = home.helm_home()
        self.assertTrue(os.path.isdir(where), where)
        self.assertNotEqual(os.path.realpath(where),
                            os.path.realpath(os.path.expanduser("~/.helm")))


if __name__ == "__main__":
    unittest.main()
