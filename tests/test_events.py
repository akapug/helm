#!/usr/bin/env python3
"""events-journal tests — mutation receipts through ONE chokepoint (pk.event):
every store/drain writer appends one {v, ts, actor, verb, target, summary}
line to _global/.state/events.jsonl AFTER its write lands, plus a
`summary_full` key on the rows whose summary is wider than the column. The fire-ledger
laws apply (O(1) append, 5MB one-generation rotation, fail-open — journal
trouble never fails the write it describes); receipts, never truth. Hermetic:
HELM_HOME / HELM_ADOPTED_DIR are tmp dirs; actor env is cleared so a real
CLAUDE_SESSION_ID from the harness never leaks into assertions."""
import contextlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-home-", var="HELM_HOME")

from helm import drain, home, pk, store  # noqa: E402

TS = "2026-07-19T00:00:00Z"
ENV_KEYS = ("HELM_HOME", "HELM_ADOPTED_DIR", "HELM_ACTOR",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID")


class EventsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-events-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def rows(self):
        try:
            with open(pk.events_path(), encoding="utf-8") as f:
                return [json.loads(l) for l in f.read().splitlines()]
        except OSError:
            return []

    def run_cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = store.cmd_store(list(args))
        return rc, out.getvalue(), err.getvalue()


class SeamTest(EventsBase):
    def test_row_shape_summary_cap_and_append(self):
        self.assertTrue(pk.event("store.add", "x-law", "s" * 300, actor="sess-1"))
        pk.event("store.retire", "x-law", "done")
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        r = rows[0]
        self.assertEqual((r["v"], r["actor"], r["verb"], r["target"]),
                         (1, "sess-1", "store.add", "x-law"))
        # narrow for the table, and SAYING SO — never a blob, never a silent
        # prefix that reads to every consumer as the whole summary
        self.assertEqual(r["summary"],
                         "s" * 200 + " … [cut: 200 of 300 chars]")
        self.assertEqual(r["summary_full"], "s" * 300)
        self.assertTrue(r["ts"])

    def test_a_summary_inside_the_width_is_unchanged_and_stands_alone(self):
        """The must-hit control. An under-bound summary is byte-identical to
        what the writer handed in and mints no second key, so the mark above
        is evidence of a real loss and not decoration on every row."""
        pk.event("store.add", "x-law", "short and whole", actor="sess-1")
        r = self.rows()[0]
        self.assertEqual(r["summary"], "short and whole")
        self.assertNotIn("summary_full", r)

    def test_the_whole_summary_is_itself_bounded_and_says_so(self):
        """One pathological row may not eat a rotation generation, so the
        second key has a bound too — and a cut there is marked like any
        other, never a second silent prefix."""
        pk.event("store.add", "x-law", "s" * 9000, actor="sess-1")
        r = self.rows()[0]
        self.assertIn("[cut: 200 of 9000 chars]", r["summary"])
        self.assertEqual(r["summary_full"],
                         "s" * pk.SUMMARY_MAX
                         + " … [cut: %d of 9000 chars]" % pk.SUMMARY_MAX)

    def test_a_marked_summary_is_the_row_every_reader_renders(self):
        """THE CONSUMERS, not a model of them: the verb that prints the trail
        renders the mark, because the notice is part of the VALUE and not a
        flag beside it that a renderer must learn about."""
        pk.event("store.add", "x-law", "why " * 200, actor="sess-1")
        rc, out, _err = self.run_cli(["events"])
        self.assertEqual(rc, 0)
        self.assertIn("[cut: 200 of 800 chars]", out)

    def test_actor_env_fallback_chain(self):
        pk.event("v", "t", "s")
        os.environ["CLAUDE_SESSION_ID"] = "claude-sess"
        pk.event("v", "t", "s")
        os.environ["HELM_ACTOR"] = "agent-z"  # HELM_ACTOR beats the session id
        pk.event("v", "t", "s")
        self.assertEqual([r["actor"] for r in self.rows()],
                         ["cli", "claude-sess", "agent-z"])

    def test_rotation_at_5mb_one_generation(self):
        path = pk.events_path()
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            f.write("x" * (pk.EVENTS_MAX + 1))
        pk.event("store.add", "y", "post-rotation")
        self.assertTrue(os.path.exists(path + ".1"))
        self.assertEqual(os.path.getsize(path + ".1"), pk.EVENTS_MAX + 1)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["summary"], "post-rotation")

    def test_fail_open_unwritable_state_dir(self):
        state = os.path.dirname(pk.events_path())
        os.makedirs(state)
        os.chmod(state, stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(lambda: os.path.isdir(state) and os.chmod(state, 0o755))
        self.assertFalse(pk.event("store.add", "x", "s"))  # no raise, ever
        # the write the receipt describes still succeeds end-to-end
        rc, out, _ = self.run_cli(["add", "prior", "x-law | statement | 0.7 | xkw"])
        self.assertEqual(rc, 0)
        self.assertIn("LIVE 'x-law'", out)


class ChokepointTest(EventsBase):
    """One receipt per writer verb — the chokepoint is adopted, not optional."""

    def test_store_writer_verbs_each_leave_one_receipt(self):
        for spec in (["add", "prior", "x-law | belief | 0.7 | xkw"],
                     ["add", "premise", "y-truth | certain | ykw"],
                     ["add", "lexicon", "youable | able to be you"],
                     ["add", "heuristic", "swarmify | split it | swarm"],
                     ["add", "reference", "dregg | crown | https://x.test | dreggkw"],
                     ["evidence", TS, "x-law", "0.1", "held up"],
                     ["supersede", TS, "x-law", "y-truth", "sharper"],
                     ["retire", TS, "y-truth", "over"]):
            rc, _, err = self.run_cli(spec)
            self.assertEqual(rc, 0, err)
        self.assertEqual([(r["verb"], r["target"]) for r in self.rows()],
                         [("store.add", "x-law"), ("store.add", "y-truth"),
                          ("store.add", "youable"), ("store.add", "swarmify"),
                          ("store.add", "dregg"),
                          ("store.evidence", "x-law"),
                          ("store.supersede", "x-law"),
                          ("store.retire", "y-truth")])

    def test_demote_and_undo_receipts(self):
        store.write_prior({"id": "pin-a", "statement": "always on",
                           "confidence": 1.0, "pin": "true", "stated_ts": TS})
        rc, _, _ = self.run_cli(["demote", "pin-a", "starved 130/130 rows"])
        self.assertEqual(rc, 0)
        rc, _, _ = self.run_cli(["demote", "pin-a", "--undo", "needed after all"])
        self.assertEqual(rc, 0)
        self.assertEqual([(r["verb"], r["target"]) for r in self.rows()],
                         [("store.demote", "pin-a"), ("store.undemote", "pin-a")])
        self.assertIn("always -> jit", self.rows()[0]["summary"])

    def test_drain_apply_and_rekey_receipts(self):
        mem = os.environ["HELM_ADOPTED_DIR"]
        pk.atomic_write(os.path.join(mem, "feedback-atomic.md"),
                        '---\nname: feedback-atomic\ndescription: "torn writes '
                        'never land"\nmetadata:\n  node_type: memory\n'
                        '  type: feedback\n---\n\nbody\n')
        pk.write_json(home.registry_path(), {"version": 1, "projects": {}})
        drain.apply(drain.classify(mem), mem)
        rows = self.rows()
        self.assertEqual(rows[-1]["verb"], "drain.apply")
        self.assertIn("1 action routed", rows[-1]["summary"])
        # dry-run rekey: no receipt; --apply with changes: one receipt
        drain.rekey()
        self.assertEqual(len(self.rows()), len(rows))
        r = drain.rekey(apply=True)
        self.assertEqual(r["rekeyed"], 1)
        self.assertEqual(self.rows()[-1]["verb"], "drain.rekey")


class ReadSideTest(EventsBase):
    def test_events_renders_recent_trail_with_limit(self):
        for i in range(4):
            pk.event("store.add", "id-%d" % i, "statement %d" % i, actor="s-%d" % i)
        rc, out, _ = self.run_cli(["events", "--limit", "2"])
        self.assertEqual(rc, 0)
        self.assertIn("helm store events (last 2):", out)
        self.assertNotIn("id-1", out)
        self.assertIn("id-2", out)
        self.assertIn("id-3", out)
        self.assertIn("store.add", out)
        self.assertIn("s-3", out)
        rc, out, _ = self.run_cli(["events"])  # default window
        self.assertIn("(last 4)", out)

    def test_read_spans_rotated_generation(self):
        path = pk.events_path()
        os.makedirs(os.path.dirname(path))
        with open(path + ".1", "w") as f:
            f.write(json.dumps({"v": 1, "ts": TS, "actor": "cli",
                                "verb": "store.add", "target": "old-row",
                                "summary": "pre-rotation"}) + "\n")
        pk.event("store.retire", "new-row", "post-rotation")
        got = pk.read_events(5)
        self.assertEqual([r["target"] for r in got], ["old-row", "new-row"])

    def test_empty_journal_and_garbled_lines(self):
        rc, out, _ = self.run_cli(["events"])
        self.assertEqual(rc, 0)
        self.assertIn("no mutation receipts yet", out)
        path = pk.events_path()
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            f.write("not json\n" + json.dumps({"v": 1, "ts": TS, "actor": "cli",
                                               "verb": "v", "target": "t",
                                               "summary": "s"}) + "\n")
        self.assertEqual(len(pk.read_events(10)), 1)  # garbled skipped, never fatal
        rc, _, err = self.run_cli(["events", "--limit", "nope"])
        self.assertEqual(rc, 2)


class ABoundedValueSaysSoTest(unittest.TestCase):
    """pk's bounded-value primitive — the shape the journal row above uses and
    the three front-matter writers share. Pure: no home, no file, no journal."""

    def test_a_value_past_the_bound_carries_both_sizes(self):
        got = pk.cut_marked("x" * 500, 100)
        self.assertIn("[cut: 100 of 500 chars]", got)
        self.assertEqual(got, "x" * 100 + " … [cut: 100 of 500 chars]")

    def test_a_value_inside_the_bound_is_byte_identical(self):
        """The must-hit control: no notice on a whole value, so the notice is
        evidence of a real loss rather than decoration on every field."""
        self.assertEqual(pk.cut_marked("whole", 100), "whole")
        self.assertEqual(pk.cut_marked("x" * 100, 100), "x" * 100)
        self.assertEqual(pk.cut_marked("x" * 101, 100),
                         "x" * 100 + " … [cut: 100 of 101 chars]")

    def test_a_none_is_stringified_rather_than_written_as_the_word(self):
        self.assertEqual(pk.cut_marked(0, 100), "0")   # a real value renders
        self.assertEqual(pk.cut_marked(None, 100), "")

    def test_a_description_is_one_quoted_line_that_cannot_tear_its_entry(self):
        got = pk.description_line('two\nlines with a " quote', 100)
        self.assertEqual(got, "two lines with a ' quote")

    def test_a_cut_description_says_so_and_still_parses_back(self):
        """The value is written INSIDE a `"..."` front-matter field, so the
        arm reads it back through the parser the shelf actually uses."""
        long_lead = "why this matters " * 30
        desc = pk.description_line(long_lead, 150)
        self.assertIn("[cut: 150 of %d chars]" % len(long_lead.strip()), desc)
        path = os.path.join(self.tmp_dir(), "entry.md")
        pk.atomic_write(path, '---\nname: e\ndescription: "%s"\n---\n\n%s\n'
                        % (desc, long_lead))
        got = pk.parse_simple_frontmatter(path, {"name": "", "description": ""})
        self.assertEqual(got["description"], desc)

    def tmp_dir(self):
        d = tempfile.mkdtemp(prefix="helm-test-cutmark-")
        self.addCleanup(shutil.rmtree, d, True)
        return d


if __name__ == "__main__":
    unittest.main()
