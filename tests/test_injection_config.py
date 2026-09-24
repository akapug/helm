"""Focused tests for the opt-in Config injection observation model."""
import os
import shutil
import tempfile
import unittest
from unittest import mock

from helm import configs, injection_config, seats


class InjectionConfigViewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-injection-config-")
        self.home = os.path.join(self.tmp, "claude-home")
        self.cwd = os.path.join(self.tmp, "work")
        self.sid = "session-exact-v2"
        self.path = os.path.join(self.home, "projects", "-work", self.sid + ".jsonl")
        os.makedirs(os.path.dirname(self.path))
        os.makedirs(self.cwd)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{}\n")
        self.old_roots = configs.HOME_ROOTS
        self.old_table = configs.HOME_ROOT_HARNESS
        configs.HOME_ROOTS = [self.home]
        configs.HOME_ROOT_HARNESS = {os.path.realpath(self.home): "claude"}

    def tearDown(self):
        configs.HOME_ROOTS = self.old_roots
        configs.HOME_ROOT_HARNESS = self.old_table
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _roster(self):
        return {"seat-a": {"session": self.sid, "cwd": self.cwd,
                            "runtime": {"agent_harness": "claude"},
                            "runtime_verified": True}}

    def _acquired(self):
        return self._roster(), False

    def _catalog(self):
        return {"rows": [{"i": self.sid, "h": "claude", "cwd": self.cwd,
                           "p": self.path}]}

    def test_observed_keeps_v2_utf8_and_v1_approximate_separate(self):
        rows = [
            {"v": 1, "session": self.sid, "ts": "old",
             "bytes": {"pinned": 7, "jit": 3, "reflex": 0}},
            {"v": 2, "session": self.sid, "ts": "wrong-unit",
             "sample": {"encoding": "characters", "rendered_bytes": 999,
                        "lane_bytes": {"pinned": 999}}},
            {"v": 2, "session": self.sid, "ts": "new", "turn": 4,
             "context": {"session": self.sid, "cwd": self.cwd,
                         "harness": "claude", "config_home": self.home},
             "context_sources": {"session": "hook-json", "cwd": "hook-json",
                                 "harness": "HELM_AGENT_HARNESS",
                                 "config_home": "CLAUDE_CONFIG_DIR"},
             "sample": {"encoding": "utf-8", "rendered_bytes": 19,
                        "lane_bytes": {"whisper": 0, "pinned": 19,
                                       "jit": 0, "reflex": 0}}},
        ]
        with mock.patch("helm.seats.roster_acquired", side_effect=self._acquired), \
                mock.patch("helm.transcripts.get_catalog", side_effect=self._catalog):
            out = injection_config.view("seat-a", self.sid, rows=rows)
        self.assertEqual(out["state"], "observed")
        self.assertEqual(out["target"]["config_home"]["value"],
                         os.path.realpath(self.home))
        self.assertEqual(out["samples"]["v2_utf8"]["count"], 1)
        self.assertEqual(out["samples"]["v2_utf8"]["rendered_bytes"], 19)
        self.assertEqual(out["samples"]["v1_approx"]["approx_characters"], 10)
        self.assertNotIn("rendered_bytes", out["samples"]["v1_approx"])
        self.assertNotIn("approx_characters", out["samples"]["v2_utf8"])
        self.assertEqual(out["samples"]["v3_utf8"]["count"], 0)

    def test_v3_native_default_assigns_exact_samples_and_catalog_cannot_move_it(self):  # noqa: VACUOUS_ASSERTION — the fixed two-path loop always exercises both canonical and hostile catalog locations
        default = os.path.join(self.tmp, "native", ".claude")
        row = {"v": 3, "session": self.sid, "ts": "v3", "turn": 5,
               "runtime": {"pid": 77, "proc_start": "123"},
               "context": {"session": self.sid, "cwd": self.cwd,
                           "harness": "claude", "config_home": default},
               "context_sources": {
                   "session": "agent-session-runtime",
                   "cwd": "hook-event-cwd",
                   "harness": "agent-process-runtime",
                   "config_home": "agent-HOME-default"},
               "sample": {"encoding": "utf-8", "rendered_bytes": 23,
                          "lane_bytes": {"whisper": 0, "pinned": 23,
                                         "jit": 0, "reflex": 0}}}
        legacy = {"v": 2, "session": self.sid, "ts": "v2", "turn": 4,
                  "context": {"session": self.sid, "cwd": self.cwd,
                              "harness": "claude", "config_home": default},
                  "context_sources": {
                      "session": "hook-json", "cwd": "hook-json",
                      "harness": "HELM_AGENT_HARNESS",
                      "config_home": "CLAUDE_CONFIG_DIR"},
                  "sample": {"encoding": "utf-8", "rendered_bytes": 11,
                             "lane_bytes": {"whisper": 0, "pinned": 11,
                                            "jit": 0, "reflex": 0}}}
        other_path = os.path.join(self.home, "projects", "-elsewhere",
                                  self.sid + ".jsonl")
        os.makedirs(os.path.dirname(other_path))
        with open(other_path, "w", encoding="utf-8") as f:
            f.write("{}\n")
        for path in (self.path, other_path):
            with self.subTest(path=path), mock.patch(
                    "helm.seats.roster_acquired", side_effect=self._acquired), \
                    mock.patch("helm.transcripts.get_catalog", return_value={
                        "rows": [{"i": self.sid, "h": "claude",
                                  "cwd": self.cwd, "p": path}]}):
                out = injection_config.view("seat-a", self.sid,
                                            rows=[legacy, row])
            self.assertEqual(out["state"], "observed")
            self.assertEqual(out["target"]["config_home"], {
                "value": default, "sources": ["inject-v3-agent-default"]})
            self.assertEqual(out["samples"]["v3_utf8"]["rendered_bytes"], 23)
            self.assertEqual(out["samples"]["v2_utf8"]["rendered_bytes"], 11)

    def test_v3_without_runtime_proof_is_exact_but_unassigned(self):
        row = {"v": 3, "session": self.sid,
               "context": {"session": self.sid, "cwd": self.cwd,
                           "harness": "claude", "config_home": self.home},
               "context_sources": {
                   "session": "agent-session-runtime",
                   "cwd": "hook-event-cwd",
                   "harness": "agent-process-runtime",
                   "config_home": "agent-HOME-default"},
               "sample": {"encoding": "utf-8", "rendered_bytes": 3,
                          "lane_bytes": {"whisper": 0, "pinned": 3,
                                         "jit": 0, "reflex": 0}}}
        with mock.patch("helm.seats.roster_acquired", side_effect=self._acquired), \
                mock.patch("helm.transcripts.get_catalog", side_effect=self._catalog):
            out = injection_config.view("seat-a", self.sid, rows=[row])
        self.assertIsNone(out["target"]["config_home"]["value"])
        self.assertEqual(out["samples"]["v3_utf8"]["count"], 0)
        self.assertEqual(out["samples"]["v3_unassigned"]["count"], 1)
        self.assertNotEqual(out["state"], "observed")

    def test_failed_newest_v3_stays_unassigned_without_revoking_older_fields(self):
        context = {"session": self.sid, "cwd": self.cwd,
                   "harness": "claude", "config_home": self.home}
        sources = {"session": "agent-session-runtime",
                   "cwd": "hook-event-cwd",
                   "harness": "agent-process-runtime",
                   "config_home": "agent-HOME-default"}
        sample = {"encoding": "utf-8", "rendered_bytes": 3,
                  "lane_bytes": {"whisper": 0, "pinned": 3,
                                 "jit": 0, "reflex": 0}}
        older = {"v": 3, "session": self.sid, "ts": "older",
                 "runtime": {"pid": 77, "proc_start": "123"},
                 "context": context, "context_sources": sources,
                 "sample": sample}
        failed = {"v": 3, "session": self.sid, "ts": "newer",
                  "context": {}, "context_sources": {}, "sample": sample,
                  "context_unavailable": "agent-runtime-unavailable"}
        with mock.patch("helm.seats.roster_acquired", side_effect=self._acquired), \
                mock.patch("helm.transcripts.get_catalog", side_effect=self._catalog):
            out = injection_config.view("seat-a", self.sid,
                                        rows=[older, failed])
        self.assertEqual(out["target"]["config_home"]["value"], self.home)
        self.assertEqual(out["target"]["cwd"]["value"], self.cwd)
        self.assertEqual(out["samples"]["v3_utf8"]["count"], 1)
        self.assertEqual(out["samples"]["v3_unassigned"]["count"], 1)
        self.assertEqual(out["state"], "observed")

    def test_catalog_path_proves_transcript_home_only_not_config_home(self):
        rows = [{"v": 2, "session": self.sid,
                 "context": {"session": self.sid, "cwd": self.cwd,
                             "harness": "claude"},
                 "sample": {"encoding": "utf-8", "rendered_bytes": 4,
                            "lane_bytes": {"pinned": 3}}}]
        with mock.patch("helm.seats.roster_acquired", side_effect=self._acquired), \
                mock.patch("helm.transcripts.get_catalog", side_effect=self._catalog):
            out = injection_config.view("seat-a", self.sid, rows=rows)
        self.assertEqual(out["state"], "partial")
        self.assertEqual(out["target"]["transcript_home"], {
            "value": os.path.realpath(self.home), "sources": ["catalog-path"]})
        self.assertIsNone(out["target"]["config_home"]["value"])
        self.assertEqual(out["target"]["config_home"]["sources"], [])
        self.assertIn("config_home", out["missing"])
        self.assertNotEqual(out["target"]["config_home"]["value"],
                            os.path.expanduser("~/.claude"))

    def test_v2_config_home_without_explicit_env_source_is_not_authority(self):
        rows = [{"v": 2, "session": self.sid,
                 "context": {"session": self.sid, "config_home": self.home},
                 "context_sources": {"config_home": "catalog-path"},
                 "sample": {"encoding": "utf-8", "rendered_bytes": 0,
                            "lane_bytes": {}}}]
        with mock.patch("helm.seats.roster_acquired", side_effect=self._acquired), \
                mock.patch("helm.transcripts.get_catalog", return_value={"rows": []}):
            out = injection_config.view("seat-a", self.sid, rows=rows)
        self.assertIsNone(out["target"]["config_home"]["value"])
        self.assertIn("config_home", out["missing"])

    def test_requested_seat_is_distinct_from_backend_verified_seat(self):
        with mock.patch("helm.seats.roster_acquired", side_effect=self._acquired), \
                mock.patch("helm.transcripts.get_catalog", side_effect=self._catalog):
            out = injection_config.view("stale-seat-label", self.sid, rows=[])
        self.assertEqual(out["requested"]["seat"], "stale-seat-label")
        self.assertEqual(out["target"]["seat"], {
            "value": "seat-a", "sources": ["roster-current"]})

    def test_unbacked_query_selectors_do_not_create_identity(self):
        with mock.patch("helm.seats.roster_acquired", return_value=({}, False)), \
                mock.patch("helm.transcripts.get_catalog", return_value={"rows": []}):
            out = injection_config.view("made-up", "made-up-session", rows=[])
        self.assertEqual(out["state"], "unknown")
        self.assertIsNone(out["target"]["seat"]["value"])
        self.assertIsNone(out["target"]["session"]["value"])
        self.assertIn("seat", out["missing"])

    def test_unresolved_session_never_consumes_sessionless_rows(self):
        rows = [
            {"v": 1, "bytes": {"pinned": 91}},
            {"v": 2, "context": {"harness": "claude",
                                  "config_home": self.home},
             "context_sources": {"harness": "HELM_AGENT_HARNESS",
                                 "config_home": "CLAUDE_CONFIG_DIR"},
             "sample": {"encoding": "utf-8", "rendered_bytes": 91,
                        "lane_bytes": {"whisper": 0, "pinned": 91,
                                       "jit": 0, "reflex": 0}}},
        ]
        with mock.patch("helm.seats.roster_acquired", return_value=({}, False)), \
                mock.patch("helm.transcripts.get_catalog", return_value={"rows": []}):
            out = injection_config.view(rows=rows)
        self.assertEqual(out["state"], "unknown")
        self.assertEqual(out["samples"]["v2_utf8"]["count"], 0)
        self.assertEqual(out["samples"]["v1_approx"]["count"], 0)
        self.assertIsNone(out["target"]["config_home"]["value"])

    def test_complete_session_evidence_without_roster_seat_is_partial(self):
        rows = [{"v": 2, "session": self.sid,
                 "context": {"session": self.sid, "cwd": self.cwd,
                             "harness": "claude", "config_home": self.home},
                 "context_sources": {"session": "hook-json", "cwd": "hook-json",
                                     "harness": "HELM_AGENT_HARNESS",
                                     "config_home": "CLAUDE_CONFIG_DIR"},
                 "sample": {"encoding": "utf-8", "rendered_bytes": 4,
                            "lane_bytes": {"whisper": 0, "pinned": 4,
                                           "jit": 0, "reflex": 0}}}]
        with mock.patch("helm.seats.roster_acquired", return_value=({}, False)), \
                mock.patch("helm.transcripts.get_catalog", side_effect=self._catalog):
            out = injection_config.view(session=self.sid, rows=rows)
        self.assertEqual(out["state"], "partial")
        self.assertIn("seat", out["missing"])

    def test_exact_unverified_runtime_blocks_row_level_harness_fallback(self):
        roster = self._roster()
        roster["seat-a"]["runtime_sessions"] = {
            self.sid: {"runtime": {"agent_harness": "claude"},
                       "verified": False, "source": "invalid-proxywatch"}}
        rows = [{"v": 2, "session": self.sid,
                 "context": {"session": self.sid},
                 "context_sources": {"session": "hook-json"},
                 "sample": {"encoding": "utf-8", "rendered_bytes": 0,
                            "lane_bytes": {lane: 0 for lane in
                                           ("whisper", "pinned", "jit", "reflex")}}}]
        with mock.patch("helm.seats.roster_acquired", return_value=(roster, False)), \
                mock.patch("helm.transcripts.get_catalog", return_value={"rows": []}):
            out = injection_config.view("seat-a", self.sid, rows=rows)
        self.assertIsNone(out["target"]["harness"]["value"])
        self.assertIn("harness", out["missing"])

    def test_malformed_newest_v2_cannot_supply_identity_or_exact_weight(self):
        rows = [{"v": 2, "session": self.sid,
                 "context": {"session": self.sid, "cwd": "/forged",
                             "harness": "claude", "config_home": self.home},
                 "context_sources": {"session": "hook-json", "cwd": "hook-json",
                                     "harness": "HELM_AGENT_HARNESS",
                                     "config_home": "CLAUDE_CONFIG_DIR"},
                 "sample": {"encoding": "characters", "rendered_bytes": 999,
                            "lane_bytes": {"pinned": 999}}}]
        with mock.patch("helm.seats.roster_acquired", side_effect=self._acquired), \
                mock.patch("helm.transcripts.get_catalog", return_value={"rows": []}):
            out = injection_config.view("seat-a", self.sid, rows=rows)
        self.assertEqual(out["target"]["cwd"]["value"], os.path.realpath(self.cwd))
        self.assertIsNone(out["target"]["config_home"]["value"])
        self.assertEqual(out["samples"]["v2_utf8"]["count"], 0)

    def test_catalog_cwd_is_annotation_not_identity_conflict(self):
        other = os.path.join(self.tmp, "other")
        rows = [{"v": 2, "session": self.sid,
                 "context": {"session": self.sid, "cwd": self.cwd,
                             "harness": "claude", "config_home": self.home},
                 "context_sources": {"session": "hook-json", "cwd": "hook-json",
                                     "harness": "HELM_AGENT_HARNESS",
                                     "config_home": "CLAUDE_CONFIG_DIR"},
                 "sample": {"encoding": "utf-8", "rendered_bytes": 0,
                            "lane_bytes": {lane: 0 for lane in
                                           ("whisper", "pinned", "jit", "reflex")}}}]
        with mock.patch("helm.seats.roster_acquired", side_effect=self._acquired), \
                mock.patch("helm.transcripts.get_catalog", return_value={"rows": [
                    {"i": self.sid, "h": "claude", "cwd": other, "p": self.path}]}):
            out = injection_config.view("seat-a", self.sid, rows=rows)
        self.assertNotIn("cwd", out["conflicts"])
        self.assertEqual(out["target"]["cwd"]["value"], os.path.realpath(self.cwd))
        self.assertEqual(out["annotations"]["catalog_cwd"], [other])

    def test_duplicate_catalog_physical_rows_are_ambiguous_even_when_equal(self):
        second = os.path.join(os.path.dirname(self.path), self.sid + "-copy.jsonl")
        with open(second, "w", encoding="utf-8") as f:
            f.write("{}\n")
        row = {"i": self.sid, "h": "claude", "cwd": self.cwd, "p": self.path}
        duplicate = dict(row, p=second)
        with mock.patch("helm.seats.roster_acquired", side_effect=self._acquired), \
                mock.patch("helm.transcripts.get_catalog",
                           return_value={"rows": [row, duplicate]}):
            out = injection_config.view("seat-a", self.sid, rows=[])
        self.assertIn("catalog-ambiguous", out["unavailable"])
        self.assertNotEqual(out["state"], "observed")
        self.assertEqual(out["target"]["session"]["sources"].count("catalog-id"), 1)

    def test_harness_value_rejects_another_harness_source_token(self):
        codex_home = os.path.join(self.tmp, "codex-home")
        rows = [{"v": 2, "session": self.sid,
                 "context": {"session": self.sid, "cwd": self.cwd,
                             "harness": "codex", "config_home": codex_home},
                 "context_sources": {"session": "hook-json", "cwd": "hook-json",
                                     "harness": "PI_CODING_AGENT",
                                     "config_home": "CODEX_HOME"},
                 "sample": {"encoding": "utf-8", "rendered_bytes": 0,
                            "lane_bytes": {lane: 0 for lane in
                                           ("whisper", "pinned", "jit", "reflex")}}}]
        roster = {"seat-a": {"session": self.sid, "cwd": self.cwd,
                             "runtime": {"agent_harness": "codex"},
                             "runtime_verified": True}}
        with mock.patch("helm.seats.roster_acquired", return_value=(roster, False)), \
                mock.patch("helm.transcripts.get_catalog", return_value={"rows": []}):
            out = injection_config.view("seat-a", self.sid, rows=rows)
        self.assertIsNone(out["target"]["config_home"]["value"])
        self.assertEqual(out["samples"]["v2_utf8"]["count"], 0)
        self.assertEqual(out["samples"]["v2_unassigned"]["count"], 1)

    def test_impossible_v2_byte_accounting_cannot_mint_context(self):
        rows = [{"v": 2, "session": self.sid,
                 "context": {"session": self.sid, "cwd": "/forged",
                             "harness": "claude", "config_home": self.home},
                 "context_sources": {"session": "hook-json", "cwd": "hook-json",
                                     "harness": "HELM_AGENT_HARNESS",
                                     "config_home": "CLAUDE_CONFIG_DIR"},
                 "sample": {"encoding": "utf-8", "rendered_bytes": 0,
                            "lane_bytes": {"whisper": 0, "pinned": 100,
                                           "jit": 0, "reflex": 0}}}]
        with mock.patch("helm.seats.roster_acquired", side_effect=self._acquired), \
                mock.patch("helm.transcripts.get_catalog", return_value={"rows": []}):
            out = injection_config.view("seat-a", self.sid, rows=rows)
        self.assertEqual(out["target"]["cwd"]["value"], self.cwd)
        self.assertIsNone(out["target"]["config_home"]["value"])
        self.assertEqual(out["samples"]["v2_utf8"]["count"], 0)

    def test_resumed_session_keeps_config_weight_in_separate_cohorts(self):
        home2 = os.path.join(self.tmp, "home-2")
        def row(home, rendered, turn):
            return {"v": 2, "session": self.sid, "turn": turn,
                    "context": {"session": self.sid, "cwd": self.cwd,
                                "harness": "claude", "config_home": home},
                    "context_sources": {"session": "hook-json", "cwd": "hook-json",
                                        "harness": "HELM_AGENT_HARNESS",
                                        "config_home": "CLAUDE_CONFIG_DIR"},
                    "sample": {"encoding": "utf-8", "rendered_bytes": rendered,
                               "lane_bytes": {"whisper": 0, "pinned": rendered,
                                              "jit": 0, "reflex": 0}}}
        with mock.patch("helm.seats.roster_acquired", side_effect=self._acquired), \
                mock.patch("helm.transcripts.get_catalog", side_effect=self._catalog):
            out = injection_config.view("seat-a", self.sid,
                                        rows=[row(self.home, 10, 1), row(home2, 20, 2)])
        self.assertEqual(out["target"]["config_home"]["value"], home2)
        self.assertEqual(out["samples"]["v2_utf8"]["rendered_bytes"], 20)
        self.assertEqual(out["samples"]["v2_utf8"]["count"], 1)
        self.assertEqual(out["samples"]["v2_other_cohorts"][0]["rendered_bytes"], 10)

    def test_historical_session_uses_exact_proof_not_current_row_cwd(self):
        current = "current-session"
        old_cwd = os.path.join(self.tmp, "old-work")
        os.makedirs(old_cwd)
        roster = {"seat-a": {"session": current, "sessions": [self.sid],
                             "cwd": "/current-cwd",
                             "runtime_sessions": {self.sid: {
                                 "runtime": {"agent_harness": "claude"},
                                 "verified": True, "source": "launch"}}}}
        sample = {"v": 2, "session": self.sid,
                  "context": {"session": self.sid, "cwd": old_cwd,
                              "harness": "claude", "config_home": self.home},
                  "context_sources": {"session": "hook-json", "cwd": "hook-json",
                                      "harness": "HELM_AGENT_HARNESS",
                                      "config_home": "CLAUDE_CONFIG_DIR"},
                  "sample": {"encoding": "utf-8", "rendered_bytes": 0,
                             "lane_bytes": {lane: 0 for lane in
                                            ("whisper", "pinned", "jit", "reflex")}}}
        with mock.patch("helm.seats.roster_acquired", return_value=(roster, False)), \
                mock.patch("helm.transcripts.get_catalog", side_effect=self._catalog):
            out = injection_config.view(session=self.sid, rows=[sample])
        self.assertEqual(out["target"]["seat"], {
            "value": "seat-a", "sources": ["roster-history"]})
        self.assertIn("roster-history", out["target"]["session"]["sources"])
        self.assertNotIn("roster-current", out["target"]["seat"]["sources"])
        self.assertEqual(out["target"]["cwd"]["value"], old_cwd)
        self.assertNotEqual(out["target"]["cwd"]["value"], "/current-cwd")
        self.assertEqual(out["state"], "observed")

    def test_session_only_duplicate_history_is_ambiguous_in_every_roster_order(self):  # noqa: VACUOUS_ASSERTION — both explicit insertion orders run under subTest and each must withhold seat identity, report ambiguity, and refuse observed state
        current = "current-session"
        runtime = {self.sid: {"runtime": {"agent_harness": "claude"},
                              "verified": True, "source": "launch"}}
        rows = [
            ("seat-a", {"session": current, "sessions": [self.sid],
                        "runtime_sessions": runtime}),
            ("seat-b", {"session": "other-current", "sessions": [self.sid],
                        "runtime_sessions": runtime}),
        ]
        sample = {"v": 2, "session": self.sid,
                  "context": {"session": self.sid, "cwd": self.cwd,
                              "harness": "claude", "config_home": self.home},
                  "context_sources": {"session": "hook-json", "cwd": "hook-json",
                                      "harness": "HELM_AGENT_HARNESS",
                                      "config_home": "CLAUDE_CONFIG_DIR"},
                  "sample": {"encoding": "utf-8", "rendered_bytes": 0,
                             "lane_bytes": {lane: 0 for lane in
                                            ("whisper", "pinned", "jit", "reflex")}}}
        for ordered in (rows, list(reversed(rows))):
            with self.subTest(order=[name for name, _row in ordered]), \
                    mock.patch("helm.seats.roster_acquired",
                               return_value=(dict(ordered), False)), \
                    mock.patch("helm.transcripts.get_catalog",
                               side_effect=self._catalog):
                out = injection_config.view(session=self.sid, rows=[sample])
            self.assertEqual(out["requested"], {"seat": None, "session": self.sid})
            self.assertIsNone(out["target"]["seat"]["value"])
            self.assertIn("roster-ambiguous", out["unavailable"])
            self.assertNotEqual(out["state"], "observed")

    def test_pi_explicit_config_home_is_authoritative(self):
        pi_home = os.path.join(self.tmp, "pi-home")
        rows = [{"v": 2, "session": self.sid,
                 "context": {"session": self.sid, "cwd": self.cwd,
                             "harness": "pi", "config_home": pi_home},
                 "context_sources": {"session": "hook-json", "cwd": "hook-json",
                                     "harness": "PI_CODING_AGENT",
                                     "config_home": "PI_CODING_AGENT_DIR"},
                 "sample": {"encoding": "utf-8", "rendered_bytes": 0,
                            "lane_bytes": {lane: 0 for lane in
                                           ("whisper", "pinned", "jit", "reflex")}}}]
        roster = {"seat-a": {"session": self.sid, "cwd": self.cwd,
                             "runtime": {"agent_harness": "pi"},
                             "runtime_verified": True}}
        with mock.patch("helm.seats.roster_acquired", return_value=(roster, False)), \
                mock.patch("helm.transcripts.get_catalog", return_value={"rows": []}):
            out = injection_config.view("seat-a", self.sid, rows=rows)
        self.assertEqual(out["target"]["config_home"]["value"], pi_home)
        self.assertEqual(out["state"], "observed")
        self.assertNotIn("transcript_home", out["missing"])

    def test_casefold_seat_selector_resolves_canonical_roster_key(self):
        with mock.patch("helm.seats.roster_acquired", side_effect=self._acquired), \
                mock.patch("helm.transcripts.get_catalog", side_effect=self._catalog):
            out = injection_config.view("SEAT-A", self.sid, rows=[])
        self.assertEqual(out["requested"]["seat"], "SEAT-A")
        self.assertEqual(out["target"]["seat"]["value"], "seat-a")

    def test_roster_key_is_laundered_before_backend_identity_is_emitted(self):
        hostile = "seat-\x1b[2J‮pwn"
        roster = {hostile: {"session": self.sid, "cwd": self.cwd}}
        with mock.patch("helm.seats.roster_acquired",
                        return_value=(roster, False)), \
                mock.patch("helm.transcripts.get_catalog", return_value={"rows": []}):
            out = injection_config.view(session=self.sid, rows=[])
        self.assertIn("\x1b", hostile)
        self.assertIn("‮", hostile)
        self.assertEqual(out["target"]["seat"]["value"],
                         seats._seat_label(hostile))
        self.assertNotEqual(out["target"]["seat"]["value"], hostile)

    def test_production_roster_reader_failure_is_unavailable(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch("helm.seats_roster.roster_path",
                           return_value=os.path.join(d, "roster.json")), \
                mock.patch("helm.transcripts.get_catalog", return_value={"rows": []}), \
                mock.patch("builtins.open", side_effect=PermissionError("denied")):
            rows, failed = seats.roster_acquired()
            out = injection_config.view("seat-a", self.sid, rows=[])
        self.assertEqual(rows, {})
        self.assertIs(failed, True)
        self.assertEqual(out["unavailable"], ["roster"])

    def test_corrupt_and_wrong_shaped_roster_files_are_unavailable(self):
        path = os.path.join(self.tmp, "roster.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"seat-a": {"session": "session-exact-v2"}}')
        with mock.patch("helm.seats_roster.roster_path", return_value=path), \
                mock.patch("helm.transcripts.get_catalog", return_value={"rows": []}):
            readable = injection_config.view("seat-a", self.sid, rows=[])
        self.assertNotIn("roster", readable["unavailable"])
        self.assertEqual(readable["target"]["seat"]["value"], "seat-a")
        for content in ("{not json", "[]"):
            with self.subTest(content=content):
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
                with mock.patch("helm.seats_roster.roster_path", return_value=path), \
                        mock.patch("helm.transcripts.get_catalog",
                                   return_value={"rows": []}):
                    out = injection_config.view("seat-a", self.sid, rows=[])
                self.assertIn("roster", out["unavailable"])
                self.assertIsNone(out["target"]["seat"]["value"])

    def test_absent_ledger_is_empty_but_unreadable_ledger_is_unavailable(self):
        missing = os.path.join(self.tmp, "missing-ledger.jsonl")
        with mock.patch("helm.inject._ledger._ledger_path", return_value=missing), \
                mock.patch("helm.seats.roster_acquired", side_effect=self._acquired), \
                mock.patch("helm.transcripts.get_catalog", side_effect=self._catalog):
            absent = injection_config.view("seat-a", self.sid)
        self.assertNotIn("ledger", absent["unavailable"])
        real_open = os.open
        attempted = []
        def refused(path, *args, **kwargs):
            attempted.append(str(path))
            if str(path).startswith(missing):
                raise PermissionError("denied")
            return real_open(path, *args, **kwargs)
        with mock.patch("helm.inject._ledger._ledger_path", return_value=missing), \
                mock.patch("helm.seats.roster_acquired", side_effect=self._acquired), \
                mock.patch("helm.transcripts.get_catalog", side_effect=self._catalog), \
                mock.patch("os.open", side_effect=refused):
            unreadable = injection_config.view("seat-a", self.sid)
        self.assertTrue(any(path.startswith(missing) for path in attempted),
                        "the control must reach the spool's fd-level read seam")
        self.assertIn("ledger", unreadable["unavailable"])


if __name__ == "__main__":
    unittest.main()
