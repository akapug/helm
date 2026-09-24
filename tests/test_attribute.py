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
                                   "sessions": 1, "input_tokens": 0,
                                   "requests": 0, "note": None})
        self.assertEqual(rows[1], {"key": "anthropic:a", "effort_tokens": 150,
                                   "sessions": 2, "input_tokens": 0,
                                   "requests": 0, "note": None})

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
        self.accounts = [{"name": "seat@x.example", "provider": "codex",
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
        self.assertIn("2 sessions + 0 sidecar requests (last 7d, limit 200", out)  # the 30d row is out
        self.assertIn("alpha", out)          # lens resolved the project
        self.assertIn("/work/beta", out)     # no project -> the cwd shows
        self.assertIn("never raw input", out)

    def test_by_cred_attributes_codex_home_claude_stays_unattributed(self):
        rc, out, _ = self.run_cmd(["--by", "cred", "--json"])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        rows = {r["key"]: r for r in data["rows"]}
        self.assertEqual(rows["UNATTRIBUTED"]["effort_tokens"], 1000)
        self.assertEqual(rows["codex:seat@x.example"]["effort_tokens"], 500)

    def _ledger(self):
        """The sidecar meter's ledger under this test's HELM_HOME, written
        through the SHIPPED producer (`proxy_usage.request_event` /
        `read_event` over a record shaped by the fork's own JSON tags)."""
        from helm import eventledger, proxy_usage
        tb = {"schema_version": 2, "quality": "complete", "total_tokens": 100_900,
              "input": {"total_tokens": 100_000, "uncached_tokens": 20_000,
                        "cache_read_tokens": 80_000, "cache_write_tokens": 0},
              "output": {"total_tokens": 900, "non_reasoning_tokens": 600,
                         "reasoning_tokens": 300}, "unclassified_tokens": 0}
        rec = {"timestamp": "2031-01-02T03:04:05Z", "source": "pool@x.example",
               "model": "gpt-5.6-sol", "alias": "claude-sonnet-5",
               "auth_type": "oauth",  # the fork tags the KIND of auth on every
               # record; it decides nothing about whether `source` is kept
               "token_breakdown": tb, "api_key": "never-in-the-ledger"}
        # the provenance that keeps `source` verbatim: the pool file in the
        # sidecar's declared auth-dir that the record's own auth_index
        # names carries that email, read by the shipped pool reader off a
        # config shaped like the live one
        auth_dir = os.path.join(self.tmp, "codex-41", "auth")
        os.makedirs(auth_dir)
        with open(os.path.join(auth_dir, "codex-0.json"), "w") as f:
            json.dump({"type": "codex", "email": "pool@x.example"}, f)
        config = os.path.join(self.tmp, "codex-41", "config.yaml")
        with open(config, "w") as f:
            f.write("port: 8399\nauth-dir: %s\n" % json.dumps(auth_dir))
        accounts, faults = proxy_usage.pool_accounts(config)
        self.assertEqual((list(accounts.values()), faults), (["pool@x.example"], []))
        rec["auth_index"] = next(iter(accounts))
        now = time.time()
        path = proxy_usage.ledger_path()
        for ev in (proxy_usage.request_event("codex", "codex-41", 77, 8399, rec, now,
                                             accounts),
                   proxy_usage.request_event("codex", "codex-41", 77, 8399,
                                             dict(rec, source=""), now, accounts),
                   proxy_usage.read_event("codex", "codex-41", 77, 8399,
                                          proxy_usage.READ, None, 2, now),
                   proxy_usage.read_event("codex", "codex-42", None, 8400,
                                          proxy_usage.UNREADABLE,
                                          "port 8400 closed", 0, now)):
            # the record's own timestamp is in the window ONLY via the read
            # time here: `at` is far in the future and never filtered out
            self.assertTrue(eventledger.append(path, ev))
        return path

    def test_by_seat_joins_the_sidecar_meter_and_an_unread_sidecar_says_why(self):
        """Positive: two sidecar requests roll up under their seat with
        OUTPUT as effort and INPUT reported beside it, sessions stay
        UNATTRIBUTED (the catalog names no seat), and the seat whose last
        read was UNREADABLE is a ZERO row carrying the reason. Control: with
        no ledger the seat table is UNATTRIBUTED alone."""
        with mock.patch.dict(os.environ, {"HELM_HOME": os.path.join(self.tmp, "home")}):
            rc, out, _ = self.run_cmd(["--by", "seat", "--json"])
            self.assertEqual(rc, 0)
            self.assertEqual([r["key"] for r in json.loads(out)["rows"]],
                             ["UNATTRIBUTED"])
            self._ledger()
            rc, out, _ = self.run_cmd(["--by", "seat", "--json"])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["requests"], 2)
        rows = {r["key"]: r for r in data["rows"]}
        self.assertEqual((rows["codex-41"]["effort_tokens"], rows["codex-41"]["input_tokens"],
                          rows["codex-41"]["requests"], rows["codex-41"]["sessions"]),
                         (1800, 200_000, 2, 0))
        self.assertEqual(rows["UNATTRIBUTED"]["sessions"], 2)
        self.assertEqual(rows["UNATTRIBUTED"]["requests"], 0)
        self.assertEqual((rows["codex-42"]["effort_tokens"], rows["codex-42"]["input_tokens"],
                          rows["codex-42"]["note"]),
                         (0, 0, "UNREADABLE — port 8400 closed"))
        self.assertEqual(data["unread_seats"], {"codex-42": "port 8400 closed"})
        self.assertEqual([r["key"] for r in data["rows"]][0], "codex-41")  # input sorts it first
        with mock.patch.dict(os.environ, {"HELM_HOME": os.path.join(self.tmp, "home")}):
            rc, out, _ = self.run_cmd(["--by", "seat"])
        self.assertIn("2 sessions + 2 sidecar requests", out)
        self.assertIn("in ", out)
        self.assertIn("UNREADABLE — port 8400 closed", out)
        self.assertNotIn("never-in-the-ledger", out)

    def test_by_cred_names_the_pooled_account_and_a_record_without_one_stays_unattributed(self):
        """The cred key is `<family>:<source>` — the OAuth email the proxy
        recorded — beside the session-catalog creds; a record whose producer
        named no source joins UNATTRIBUTED rather than a guessed account, and
        the unread sidecar is a trailer under every non-seat dimension.
        Control: `--project` is a session filter, so the meter is left out
        and the trailer with it."""
        with mock.patch.dict(os.environ, {"HELM_HOME": os.path.join(self.tmp, "home")}):
            self._ledger()
            rc, out, _ = self.run_cmd(["--by", "cred", "--json"])
            self.assertEqual(rc, 0)
            rows = {r["key"]: r for r in json.loads(out)["rows"]}
            self.assertEqual((rows["codex:pool@x.example"]["input_tokens"],
                              rows["codex:pool@x.example"]["effort_tokens"],
                              rows["codex:pool@x.example"]["requests"]),
                             (100_000, 900, 1))
            self.assertEqual((rows["UNATTRIBUTED"]["effort_tokens"],
                              rows["UNATTRIBUTED"]["input_tokens"],
                              rows["UNATTRIBUTED"]["sessions"],
                              rows["UNATTRIBUTED"]["requests"]),
                             (1900, 100_000, 1, 1))
            self.assertEqual(rows["codex:seat@x.example"]["effort_tokens"], 500)
            rc, out, _ = self.run_cmd(["--by", "cred"])
            self.assertIn("sidecar unread: codex-42 — UNREADABLE — port 8400 closed", out)
            rc, out, _ = self.run_cmd(["--by", "cred", "--project", "alpha", "--json"])
            data = json.loads(out)
        self.assertEqual(data["requests"], 0)
        self.assertEqual(data["unread_seats"], {})
        self.assertNotIn("codex:pool@x.example", {r["key"] for r in data["rows"]})

    def test_a_raising_meter_reader_is_UNREADABLE_not_zero(self):
        with mock.patch.object(attribute, "proxy_efforts", side_effect=OSError("boom")):
            rc, out, _err = self.run_cmd(["--by", "seat", "--json"])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertTrue(data["incomplete"])
        self.assertIn("boom", data["meter_unknown"])
        self.assertEqual([r["key"] for r in data["rows"]], ["UNATTRIBUTED"])

    def test_an_unreadable_meter_ledger_renders_UNREADABLE_and_marks_the_rollup_incomplete(self):
        """F2 (a), through the real reader on a real path: a DIRECTORY where
        the ledger should be. The sidecar totals are not 0 — they are
        UNREADABLE with the reader's reason, `incomplete` is True in --json,
        and the text trailer says the totals are INCOMPLETE. Control: arm
        (c) below, a clean empty ledger, is 0 requests with no flag."""
        from helm import proxy_usage
        with mock.patch.dict(os.environ, {"HELM_HOME": os.path.join(self.tmp, "home")}):
            os.makedirs(proxy_usage.ledger_path())
            rc, out, _ = self.run_cmd(["--by", "cred", "--json"])
            self.assertEqual(rc, 0)
            data = json.loads(out)
            self.assertTrue(data["incomplete"])
            self.assertIn("ledger unreadable", data["meter_unknown"])
            self.assertEqual(data["requests"], 0)
            rc, out, _ = self.run_cmd(["--by", "cred"])
        self.assertIn("sidecar meter: UNREADABLE — ledger unreadable", out)
        self.assertIn("INCOMPLETE", out)

    def test_a_malformed_ledger_row_is_UNKNOWN_and_the_good_row_still_counts(self):
        """F2 (b): one good request event and one malformed complete line
        in the real ledger. The good record rolls up (1 request, its
        tokens), the malformed one is reported UNKNOWN by line and never
        counted, and the rollup is marked incomplete."""
        from helm import proxy_usage
        with mock.patch.dict(os.environ, {"HELM_HOME": os.path.join(self.tmp, "home")}):
            self._ledger()
            with open(proxy_usage.ledger_path(), "a") as f:
                f.write("this line is not a row\n")
            rc, out, _ = self.run_cmd(["--by", "seat", "--json"])
            self.assertEqual(rc, 0)
            data = json.loads(out)
            self.assertEqual(data["requests"], 2)
            self.assertTrue(data["incomplete"])
            self.assertIn("malformed ledger row(s) skipped, uncounted", data["meter_unknown"])
            rows = {r["key"]: r for r in data["rows"]}
            self.assertEqual(rows["codex-41"]["input_tokens"], 200_000)
            rc, out, _ = self.run_cmd(["--by", "seat"])
        self.assertIn("sidecar meter: UNREADABLE — malformed ledger row(s)", out)

    def _marked(self, *markers):
        """A real ledger: two of the shipped producer's request events for
        one seat, then the given read markers in order — each
        (status, reason, records, persisted) — so the LATEST marker is the
        one under test."""
        from helm import eventledger, proxy_usage
        path = self._ledger()
        for i, (status, reason, records, persisted) in enumerate(markers):
            self.assertTrue(eventledger.append(path, proxy_usage.read_event(
                "codex", "codex-41", 77, 8399, status, reason, records,
                time.time() + i + 1, persisted=persisted)))
        return path

    def test_a_seat_whose_last_marker_is_FAILED_PERSIST_renders_PARTIAL_and_incomplete(self):
        """F1 (a), through the real reader: the seat's latest marker says
        the last pass persisted 1 of 2 records. The seat's row still counts
        what reached the ledger but carries `PARTIAL — 1 record(s) lost —
        <reason>`, --json says incomplete true with the reason in
        meter_unknown, and under --by cred the trailer says PARTIAL. Control:
        arm (b), the same ledger ending on a READ marker."""
        from helm import proxy_usage
        with mock.patch.dict(os.environ, {"HELM_HOME": os.path.join(self.tmp, "home")}):
            self._marked((proxy_usage.FAILED_PERSIST, "ledger refused 1 of 2 record(s)",
                          2, 1))
            rc, out, _ = self.run_cmd(["--by", "seat", "--json"])
            self.assertEqual(rc, 0)
            data = json.loads(out)
            rows = {r["key"]: r for r in data["rows"]}
            self.assertEqual(rows["codex-41"]["requests"], 2)
            self.assertEqual(rows["codex-41"]["note"],
                             "PARTIAL — 1 record(s) lost — ledger refused 1 of 2 record(s)")
            self.assertTrue(data["incomplete"])
            self.assertIn("codex-41 FAILED-PERSIST — 1 record(s) lost — ledger refused 1 of 2",
                          data["meter_unknown"])
            self.assertNotIn("codex-41", data["unread_seats"])
            rc, out, _ = self.run_cmd(["--by", "cred"])
        self.assertIn("sidecar meter: PARTIAL — codex-41 FAILED-PERSIST", out)
        self.assertIn("INCOMPLETE", out)
        self.assertNotIn("codex-41 — UNREADABLE", out)

    def test_an_empty_table_with_a_FAILED_PERSIST_marker_says_PARTIAL_not_UNREADABLE(self):
        """The empty-table branch of the text render takes its trailer word
        from the marker status like the full-table one: a readable ledger
        holding only a FAILED-PERSIST marker, with every session outside the
        window, renders `sidecar meter: PARTIAL` and never UNREADABLE.
        Control: the same empty window over an unreadable ledger (a
        directory in its place) renders UNREADABLE."""
        from helm import eventledger, proxy_usage
        with mock.patch.dict(os.environ, {"HELM_HOME": os.path.join(self.tmp, "home")}):
            path = proxy_usage.ledger_path()
            self.assertTrue(eventledger.append(path, proxy_usage.read_event(
                "codex", "codex-41", 77, 8399, proxy_usage.FAILED_PERSIST,
                "ledger refused 1 of 2 record(s)", 2, time.time(), persisted=1)))
            rc, out, _ = self.run_cmd(["--by", "cred", "--since", "0h"])
            self.assertEqual(rc, 0)
            self.assertIn("no sessions or sidecar requests", out)
            self.assertIn("sidecar meter: PARTIAL — codex-41 FAILED-PERSIST", out)
            self.assertNotIn("UNREADABLE", out)
            os.remove(path)
            os.makedirs(path)
            rc, out, _ = self.run_cmd(["--by", "cred", "--since", "0h"])
        self.assertIn("no sessions or sidecar requests", out)
        self.assertIn("sidecar meter: UNREADABLE — ledger unreadable", out)
        self.assertNotIn("PARTIAL", out)

    def test_a_seat_whose_last_marker_is_READ_renders_no_note_and_is_complete(self):
        """F1 (b), the control: the same rows ending on a READ marker carry
        no note, incomplete is false and meter_unknown None."""
        from helm import proxy_usage
        with mock.patch.dict(os.environ, {"HELM_HOME": os.path.join(self.tmp, "home")}):
            self._marked((proxy_usage.READ, None, 2, 2))
            rc, out, _ = self.run_cmd(["--by", "seat", "--json"])
        data = json.loads(out)
        rows = {r["key"]: r for r in data["rows"]}
        self.assertEqual((rows["codex-41"]["requests"], rows["codex-41"]["note"]), (2, None))
        self.assertEqual((data["incomplete"], data["meter_unknown"]), (False, None))

    def test_the_latest_marker_decides_a_later_READ_clears_PARTIAL(self):
        """F1 (c): a FAILED-PERSIST marker followed by a later READ marker for
        the same seat renders clean — the loss was told on the pass that
        lost, and the pass after it landed whole."""
        from helm import proxy_usage
        with mock.patch.dict(os.environ, {"HELM_HOME": os.path.join(self.tmp, "home")}):
            self._marked((proxy_usage.FAILED_PERSIST, "ledger refused 1 of 2 record(s)",
                          2, 1),
                         (proxy_usage.READ, None, 3, 3))
            rc, out, _ = self.run_cmd(["--by", "seat", "--json"])
        data = json.loads(out)
        rows = {r["key"]: r for r in data["rows"]}
        self.assertIsNone(rows["codex-41"]["note"])
        self.assertEqual((data["incomplete"], data["meter_unknown"]), (False, None))

    def test_a_clean_empty_ledger_is_zero_with_no_incompleteness_flag(self):
        """F2 (c), the control: an empty ledger file reads whole, zero
        sidecar requests, `incomplete` False, no UNREADABLE trailer."""
        from helm import proxy_usage
        with mock.patch.dict(os.environ, {"HELM_HOME": os.path.join(self.tmp, "home")}):
            os.makedirs(os.path.dirname(proxy_usage.ledger_path()))
            open(proxy_usage.ledger_path(), "w").close()
            rc, out, _ = self.run_cmd(["--by", "seat", "--json"])
            self.assertEqual(rc, 0)
            data = json.loads(out)
            self.assertEqual((data["requests"], data["incomplete"], data["meter_unknown"]),
                             (0, False, None))
            rc, out, _ = self.run_cmd(["--by", "seat"])
        self.assertNotIn("UNREADABLE", out)
        self.assertNotIn("INCOMPLETE", out)

    def test_by_model_and_limit(self):
        rc, out, _ = self.run_cmd(["--by", "model", "--limit", "1"])
        self.assertEqual(rc, 0)
        self.assertIn("claude-opus-4-8", out)
        self.assertNotIn("gpt-5.5", out)  # limit 1 keeps only the newest row

    def test_project_filter(self):
        rc, out, _ = self.run_cmd(["--project", "alpha", "--json"])
        self.assertEqual(json.loads(out)["sessions"], 1)

    def test_seat_is_a_dimension_and_a_bad_one_names_all_four(self):
        rc, _out, err = self.run_cmd(["--by", "pane"])
        self.assertEqual(rc, 2)
        self.assertIn("project|model|cred|seat", err)

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
