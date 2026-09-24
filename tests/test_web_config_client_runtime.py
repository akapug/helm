"""Executable browser-JS contract for Config injection observation."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from helm import web_ui_loader

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(HERE, "config_runtime_harness.js")
EXTRACT = ["pollRoster", "pollRosterTodos", "seatPop", "seatToConfig",
           "cfgDetailClaim", "cfgInjection", "cfgObservedField"]


def _extract_fn(src, name):
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", src)
    if not m:
        raise AssertionError("function not found in assembled web UI: " + name)
    i = src.index("{", m.end())
    depth, j, quote, escaped = 0, i, None, False
    while j < len(src):
        c = src[j]
        if quote:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == quote:
                quote = None
        elif c in "\"'`":
            quote = c
        elif c == "/" and j + 1 < len(src) and src[j + 1] == "/":
            j = src.find("\n", j)
            if j < 0:
                break
            continue
        elif c == "/" and j + 1 < len(src) and src[j + 1] == "*":
            j = src.index("*/", j) + 2
            continue
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[m.start():j + 1]
        j += 1
    raise AssertionError("unbalanced function " + name)


class TestConfigClientRuntime(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("node not available")
        source = web_ui_loader.read_text()
        cls.source = source
        generation = "let CFG_DETAIL_GEN = 0;"
        assert generation in source, "Config detail generation owner is missing"
        functions = generation + "\n\n" + "\n\n".join(
            _extract_fn(source, name) for name in EXTRACT)
        with open(HARNESS, encoding="utf-8") as f:
            script = f.read().replace("/*__INJECT__*/", functions)
        cls.tmp = tempfile.mkdtemp(prefix="helm-config-runtime-")
        cls.path = os.path.join(cls.tmp, "run.js")
        with open(cls.path, "w", encoding="utf-8") as f:
            f.write(script)
        check = subprocess.run([node, "--check", cls.path], capture_output=True,
                               text=True)
        assert check.returncode == 0, check.stderr
        cls.proc = subprocess.run([node, cls.path], capture_output=True, text=True,
                                  timeout=30)
        try:
            cls.results = {row["name"]: row
                           for row in json.loads(cls.proc.stdout or "[]")}
        except json.JSONDecodeError:
            cls.results = {}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def result(self, name):
        self.assertEqual(self.proc.returncode, 0, self.proc.stderr)
        self.assertIn(name, self.results,
                      "missing %s; stdout=%r stderr=%r" %
                      (name, self.proc.stdout, self.proc.stderr))
        return self.results[name]

    def test_normal_roster_poll_reads_no_config_telemetry_or_transcripts(self):
        row = self.result("roster_poll_is_lightweight")
        self.assertTrue(row["pass"], row["detail"])

    def test_roster_timer_keeps_two_second_presence_cadence_under_sse(self):  # noqa: VACUOUS_ASSERTION — exact positive wiring controls both unstretched presence and stretched todos
        self.assertIn("setInterval(pollRoster, 2000)", self.source)
        self.assertNotIn("setInterval(esStretch(pollRoster), 2000)", self.source)
        self.assertIn("setInterval(esStretch(pollRosterTodos), 2000)", self.source)

    def test_roster_poll_reads_are_bounded(self):
        row = self.result("roster_poll_is_bounded")
        self.assertTrue(row["pass"], row["detail"])

    def test_hung_todos_does_not_block_presence_or_share_its_latch(self):
        row = self.result("roster_and_todos_settle_independently")
        self.assertTrue(row["pass"], row["detail"])

    def test_roster_and_todos_reads_have_distinct_timeouts(self):
        row = self.result("roster_and_todos_have_distinct_bounds")
        self.assertTrue(row["pass"], row["detail"])

    def test_opening_popup_performs_no_network_read(self):
        row = self.result("roster_popup_is_network_silent")
        self.assertTrue(row["pass"], row["detail"])

    def test_popup_deep_link_fetches_config_observation_on_demand(self):
        row = self.result("roster_deep_link_reads_config_only_after_click")
        self.assertTrue(row["pass"], row["detail"])

    def test_config_ui_distinguishes_requested_from_backend_verified_seat(self):
        row = self.result("config_renders_requested_and_verified_identity")
        self.assertTrue(row["pass"], row["detail"])

    def test_config_ui_distinguishes_state_and_measurement_versions(self):
        row = self.result("config_renders_state_and_versioned_units")
        self.assertTrue(row["pass"], row["detail"])

    def test_unknown_sample_units_and_unavailability_remain_explicit(self):
        row = self.result("config_unknown_samples_and_unavailable_stay_explicit")
        self.assertTrue(row["pass"], row["detail"])

    def test_older_config_response_cannot_replace_newer_identity(self):
        row = self.result("config_stale_response_is_ignored")
        self.assertTrue(row["pass"], row["detail"])

    def test_config_observation_request_has_bounded_timeout(self):
        row = self.result("config_requests_have_bounded_timeout")
        self.assertTrue(row["pass"], row["detail"])

    def test_other_config_navigation_invalidates_pending_observation(self):
        row = self.result("config_navigation_invalidates_pending_observation")
        self.assertTrue(row["pass"], row["detail"])


if __name__ == "__main__":
    unittest.main()
