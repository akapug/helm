#!/usr/bin/env python3
"""attribute tests — the token-effort rollup. Hermetic: transcripts are tmp
fixtures, the catalog/lens/accounts are stubbed; no real store, catalog build,
or provider is ever touched."""
import contextlib
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from helm import attribute


def claude_line(out=100, cache=0, model="claude-opus-4-8", uuid=None, role="assistant"):
    return json.dumps({
        "type": role, "timestamp": "2026-07-19T04:25:11.338Z", "uuid": uuid,
        "message": {"model": model,
                    "usage": {"input_tokens": 25000, "cache_read_input_tokens": 9999,
                              "cache_creation_input_tokens": cache, "output_tokens": out}}})


def codex_count(out):
    return json.dumps({"timestamp": "2026-07-02T01:13:09.002Z", "type": "event_msg",
                       "payload": {"type": "token_count", "info": {
                           "total_token_usage": {"output_tokens": 999_999},
                           "last_token_usage": {"input_tokens": 20868,
                                                "cached_input_tokens": 4480,
                                                "output_tokens": out}}}})


def codex_turn(model="gpt-5.5"):
    return json.dumps({"timestamp": "2026-07-02T01:12:55.446Z", "type": "turn_context",
                       "payload": {"cwd": "/w", "model": model}})


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-attr-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def plant(self, name, lines):
        p = os.path.join(self.tmp, name)
        with open(p, "w") as f:
            f.write("\n".join(lines) + "\n")
        return p

    def test_claude_effort_is_output_plus_cache_creation_never_input(self):
        p = self.plant("s.jsonl", [
            claude_line(out=300, cache=1000, uuid="a"),
            claude_line(out=200, cache=0, uuid="b"),
            claude_line(role="user")])
        effort, model = attribute._scan_claude(p, set())
        self.assertEqual(effort, 1500)  # 300+1000+200; input/cache_read ignored
        self.assertEqual(model, "claude-opus-4-8")

    def test_claude_cross_file_uuid_dedup_pruned_copy_counts_once(self):
        seen = set()
        a = self.plant("orig.jsonl", [claude_line(out=100, uuid="same")])
        b = self.plant("pruned.jsonl", [claude_line(out=100, uuid="same")])
        e1, _ = attribute._scan_claude(a, seen)
        e2, _ = attribute._scan_claude(b, seen)
        self.assertEqual((e1, e2), (100, 0))

    def test_claude_dominant_model_wins(self):
        p = self.plant("m.jsonl", [
            claude_line(out=1, model="claude-fable-5", uuid="1"),
            claude_line(out=1, model="claude-fable-5", uuid="2"),
            claude_line(out=1, model="claude-opus-4-8", uuid="3")])
        _, model = attribute._scan_claude(p, set())
        self.assertEqual(model, "claude-fable-5")

    def test_codex_sums_last_usage_never_the_cumulative_total(self):
        p = self.plant("rollout.jsonl", [
            codex_turn(), codex_count(438), codex_count(62), "not json"])
        effort, model = attribute._scan_codex(p)
        self.assertEqual(effort, 500)  # never the 999_999 total_token_usage
        self.assertEqual(model, "gpt-5.5")

    def test_missing_file_is_zero_not_a_crash(self):
        self.assertEqual(attribute._scan_claude("/no/such", set()), (0, "unknown"))
        self.assertEqual(attribute._scan_codex("/no/such"), (0, "unknown"))


class CredForTest(unittest.TestCase):
    ACCTS = [{"name": "a", "provider": "codex", "home": "/x/.codex-homes/a"},
             {"name": "deep", "provider": "codex", "home": "/x/.codex-homes/a/nested"},
             {"name": "default", "provider": "anthropic", "home": "/x/.claude"}]

    def test_path_boundary_not_substring(self):
        # home ".../a" must NOT capture a session under ".../abc"
        self.assertIsNone(attribute.cred_for("/x/.codex-homes/abc/sessions/r.jsonl",
                                             self.ACCTS))
        self.assertEqual(attribute.cred_for("/x/.codex-homes/a/sessions/r.jsonl",
                                            self.ACCTS), "codex:a")

    def test_longest_home_wins(self):
        self.assertEqual(attribute.cred_for("/x/.codex-homes/a/nested/s/r.jsonl",
                                            self.ACCTS), "codex:deep")

    def test_default_store_is_never_guessed(self):
        # the default ~/.claude store is not cred-specific -> UNATTRIBUTED
        self.assertIsNone(attribute.cred_for("/x/.claude/projects/s/x.jsonl",
                                             self.ACCTS))


class RollupTest(unittest.TestCase):
    def test_unattributed_visible_and_sorted_desc(self):
        efforts = [
            {"project": "p", "model": "opus", "cred": "anthropic:a", "effort": 100},
            {"project": "p", "model": "fable", "cred": "anthropic:a", "effort": 50},
            {"project": "q", "model": "opus", "cred": None, "effort": 200}]
        rows = attribute.rollup(efforts, "cred")
        self.assertEqual(rows[0], {"key": "UNATTRIBUTED", "effort_tokens": 200,
                                   "sessions": 1})
        self.assertEqual(rows[1], {"key": "anthropic:a", "effort_tokens": 150,
                                   "sessions": 2})

    def test_same_name_cross_provider_never_merges(self):
        efforts = [{"project": "p", "model": "m", "cred": "anthropic:x", "effort": 10},
                   {"project": "p", "model": "m", "cred": "codex:x", "effort": 20}]
        self.assertEqual(len(attribute.rollup(efforts, "cred")), 2)

    def test_project_and_model_dimensions(self):
        efforts = [{"project": "p", "model": "m1", "cred": None, "effort": 5},
                   {"project": "p", "model": "m2", "cred": None, "effort": 7}]
        self.assertEqual(attribute.rollup(efforts, "project")[0]["effort_tokens"], 12)
        self.assertEqual([r["key"] for r in attribute.rollup(efforts, "model")],
                         ["m2", "m1"])


class CmdTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-attr-cmd-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        now = time.time()
        cl = os.path.join(self.tmp, "s1.jsonl")
        with open(cl, "w") as f:
            f.write(claude_line(out=1000, uuid="c1") + "\n")
        cx = os.path.join(self.tmp, ".codex-homes", "seat", "sessions", "r.jsonl")
        os.makedirs(os.path.dirname(cx))
        with open(cx, "w") as f:
            f.write(codex_turn() + "\n" + codex_count(500) + "\n")
        self.rows = [
            {"h": "claude", "p": cl, "cwd": "/work/alpha", "mt": int(now) - 60},
            {"h": "codex", "p": cx, "cwd": "/work/beta", "mt": int(now) - 120},
            {"h": "claude", "p": cl, "cwd": "/work/old", "mt": int(now) - 30 * 86400}]
        self.accounts = [{"name": "seat@x.com", "provider": "codex",
                          "home": os.path.join(self.tmp, ".codex-homes", "seat")}]
        self.lens = [("alpha", "/work/alpha")]

    def run_cmd(self, args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(attribute, "_catalog_rows", return_value=self.rows), \
                mock.patch.object(attribute, "_lens", return_value=self.lens), \
                mock.patch.object(attribute, "_accounts", return_value=self.accounts), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = attribute.cmd_attribute(args)
        return rc, out.getvalue(), err.getvalue()

    def test_default_by_project_since_filters_old_rows(self):
        rc, out, _ = self.run_cmd([])
        self.assertEqual(rc, 0)
        self.assertIn("effort by project", out)
        self.assertIn("2 sessions (last 7d, limit 200", out)  # the 30d row is out
        self.assertIn("alpha", out)          # lens resolved the project
        self.assertIn("/work/beta", out)     # no project -> the cwd shows
        self.assertIn("never raw input", out)

    def test_by_cred_attributes_codex_home_claude_stays_unattributed(self):
        rc, out, _ = self.run_cmd(["--by", "cred", "--json"])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        rows = {r["key"]: r for r in data["rows"]}
        self.assertEqual(rows["UNATTRIBUTED"]["effort_tokens"], 1000)
        self.assertEqual(rows["codex:seat@x.com"]["effort_tokens"], 500)

    def test_by_model_and_limit(self):
        rc, out, _ = self.run_cmd(["--by", "model", "--limit", "1"])
        self.assertEqual(rc, 0)
        self.assertIn("claude-opus-4-8", out)
        self.assertNotIn("gpt-5.5", out)  # limit 1 keeps only the newest row

    def test_project_filter(self):
        rc, out, _ = self.run_cmd(["--project", "alpha", "--json"])
        self.assertEqual(json.loads(out)["sessions"], 1)

    def test_bad_args(self):
        rc, _, err = self.run_cmd(["--by", "nope"])
        self.assertEqual(rc, 2)
        self.assertIn("--by must be", err)
        rc, _, err = self.run_cmd(["--since", "soon"])
        self.assertEqual(rc, 2)
        self.assertIn("bad --since", err)
        rc, _, err = self.run_cmd(["--by"])
        self.assertEqual(rc, 2)

    def test_empty_window(self):
        rc, out, _ = self.run_cmd(["--since", "0.000001h"])
        self.assertEqual(rc, 0)
        self.assertIn("no sessions", out)


class SinceTest(unittest.TestCase):
    def test_parse_since(self):
        self.assertEqual(attribute._parse_since("7d"), 7 * 86400)
        self.assertEqual(attribute._parse_since("24h"), 24 * 3600)
        self.assertEqual(attribute._parse_since("3"), 3 * 86400)
        self.assertIsNone(attribute._parse_since("soon"))


if __name__ == "__main__":
    unittest.main()
