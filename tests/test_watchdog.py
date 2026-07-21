"""Hermetic tests for helm.watchdog — the context-window brick backstop.
HELM_HOME points at a tmp dir; synthetic proxy error logs drive detection.
No chat node, no real logs, no a2a posts (check(post=False))."""
import os
import shutil
import tempfile
import time
import unittest

from helm import watchdog, seat


class WatchdogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-wd-")
        self._home = os.environ.get("HELM_HOME")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm-home")
        self.logs = os.path.join(seat.seat_dir("codex"), "auth", "logs")
        os.makedirs(self.logs, exist_ok=True)

    def tearDown(self):
        if self._home is None:
            os.environ.pop("HELM_HOME", None)
        else:
            os.environ["HELM_HOME"] = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_log(self, name, lines):
        with open(os.path.join(self.logs, name), "w") as f:
            f.write("\n".join(lines) + "\n")
        time.sleep(0.01)  # keep mtime ordering deterministic

    def test_wedge_detected_and_deduped(self):
        # a real wedge = repeated ctx-window 400s (compaction itself 400s too)
        self._write_log("error-1.log", [
            '{"status":400,"error":{"message":"input exceeds the context window"}}',
            'some normal request line',
            '{"status":400,"error":{"message":"input exceeds the context window of this model"}}',
            '{"status":400,"error":{"message":"prompt is too long: exceeds the context window"}}',
        ])
        wedged = watchdog.scan(("codex",))
        self.assertEqual(len(wedged), 1)
        self.assertEqual(wedged[0]["family"], "codex")
        self.assertGreaterEqual(wedged[0]["count"], 3)
        # first check alerts (fresh); second dedups (count unchanged)
        r1 = watchdog.check(("codex",), post=False)
        self.assertEqual([w["family"] for w in r1["fresh"]], ["codex"])
        r2 = watchdog.check(("codex",), post=False)
        self.assertEqual(r2["fresh"], [])
        self.assertEqual(len(r2["wedged"]), 1)  # still wedged, just not re-alerted

    def test_single_400_is_not_a_wedge(self):
        # one over-large turn CC could still compact past is NOT a wedge
        self._write_log("error-1.log", [
            '{"status":400,"error":{"message":"input exceeds the context window"}}',
            'normal', 'normal',
        ])
        self.assertEqual(watchdog.scan(("codex",)), [])

    def test_clear_seat_and_recovery_rearm(self):
        # wedge -> alert; seat recovers (fresh clean log) -> state re-armed;
        # a new wedge after recovery alerts AGAIN
        self._write_log("error-1.log", [
            'exceeds the context window', 'exceeds the context window'])
        self.assertEqual([w["family"] for w in
                          watchdog.check(("codex",), post=False)["fresh"]], ["codex"])
        self._write_log("error-2.log", ['all good', 'working fine'])  # newest = clean
        rec = watchdog.check(("codex",), post=False)
        self.assertEqual(rec["wedged"], [])
        self._write_log("error-3.log", [
            'exceeds the context window', 'exceeds the context window'])
        self.assertEqual([w["family"] for w in
                          watchdog.check(("codex",), post=False)["fresh"]], ["codex"])

    def test_no_logs_is_clear(self):
        self.assertEqual(watchdog.scan(("codex",)), [])

    def test_alert_text_is_actionable(self):
        txt = watchdog._alert_text({"family": "codex", "count": 4,
                                    "log": "/x/error-9.log"})
        self.assertIn("codex", txt)
        self.assertIn("/clear", txt)          # names the recovery
        self.assertIn("context window", txt.lower())


if __name__ == "__main__":
    unittest.main()
