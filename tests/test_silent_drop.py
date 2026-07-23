"""Hermetic tests for helm.silent_drop — the empty-completion loud-fail.
HELM_HOME points at a tmp dir; planted transcripts drive the detector. No real
chat posts (post=False / quiet) and no pane injection ever (the rung is
read-only). The detector is validated against the drop-after-generate
signature proven on the live codex transcript (75/75 drops, 0 false
positives on a healthy seat)."""
import json
import os
import shutil
import tempfile
import unittest

from helm import seat, silent_drop

SID = "11111111-1111-1111-1111-111111111111"


def asst(content, stop="end_turn", ot=50, ts="2026-07-23T10:00:00Z",
         sidechain=False):
    return json.dumps({
        "type": "assistant", "isSidechain": sidechain, "timestamp": ts,
        "message": {"role": "assistant", "stop_reason": stop,
                    "content": content, "usage": {"output_tokens": ot}}})


class SilentDropTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-ss-")
        self._home = os.environ.pop("HELM_HOME", None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm-home")
        self.d = seat.seat_dir("codex")
        self.proj = os.path.join(self.d, "claude", "projects", "-tmp-proj")
        os.makedirs(self.proj, exist_ok=True)

    def tearDown(self):
        if self._home is None:
            os.environ.pop("HELM_HOME", None)
        else:
            os.environ["HELM_HOME"] = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def plant(self, lines, sid=SID):
        p = os.path.join(self.proj, sid + ".jsonl")
        with open(p, "w") as f:
            f.write("\n".join(lines) + "\n")
        return p

    # -- the detector ------------------------------------------------------

    def test_drop_signature_detected(self):
        # end_turn + empty content + output_tokens>0 = drop-after-generate
        self.plant([asst([], ot=103)])
        f = silent_drop.scan_seat("codex")
        self.assertIsNotNone(f)
        self.assertEqual(f["output_tokens"], 103)
        self.assertEqual(f["session"], SID)

    def test_empty_text_block_is_a_drop(self):
        # the proxy translates a drop into an empty text block + end_turn
        self.plant([asst([{"type": "text", "text": ""}], ot=49)])
        self.assertIsNotNone(silent_drop.scan_seat("codex"))

    def test_empty_thinking_wrapper_is_a_drop(self):
        # the line464 form: empty thinking wrapper, real text dropped
        self.plant([asst([{"type": "thinking", "thinking": "",
                            "signature": "gAAAA"}], ot=57)])
        self.assertIsNotNone(silent_drop.scan_seat("codex"))

    # -- non-drops (false-positive guards) ------------------------------

    def test_real_text_is_not_a_drop(self):
        self.plant([asst([{"type": "text", "text": "an answer"}], ot=50)])
        self.assertIsNone(silent_drop.scan_seat("codex"))

    def test_tool_use_is_not_a_drop(self):
        self.plant([asst([{"type": "tool_use", "name": "Bash"}], ot=50)])
        self.assertIsNone(silent_drop.scan_seat("codex"))

    def test_deliberate_thinking_park_is_not_a_drop(self):
        self.plant([asst([{"type": "thinking", "thinking": "no action"}],
                         ot=30)])
        self.assertIsNone(silent_drop.scan_seat("codex"))

    def test_zero_output_tokens_is_not_a_drop(self):
        # a genuine refusal/classifier produces ~0 output — not our class
        self.plant([asst([], ot=0)])
        self.assertIsNone(silent_drop.scan_seat("codex"))

    def test_tool_use_stop_reason_is_not_a_drop(self):
        self.plant([asst([], stop="tool_use", ot=50)])
        self.assertIsNone(silent_drop.scan_seat("codex"))

    def test_sidechain_never_counts(self):
        self.plant([asst([], ot=99, sidechain=True)])
        self.assertIsNone(silent_drop.scan_seat("codex"))

    def test_reports_the_newest_drop(self):
        self.plant([asst([], ot=10, ts="2026-07-23T10:00:00Z"),
                    asst([{"type": "text", "text": "healthy"}], ot=200),
                    asst([], ot=77, ts="2026-07-23T11:00:00Z")])
        f = silent_drop.scan_seat("codex")
        self.assertEqual(f["output_tokens"], 77)
        self.assertEqual(f["ts"], "2026-07-23T11:00:00Z")

    # -- the latch ----------------------------------------------------------

    def test_latch_suppresses_repeat_same_ts(self):
        self.plant([asst([], ot=50, ts="2026-07-23T10:00:00Z")])
        r1 = silent_drop.check(seats=["codex"], post=False)
        self.assertEqual(len(r1["alerted"]), 1)
        r2 = silent_drop.check(seats=["codex"], post=False)
        self.assertEqual(len(r2["alerted"]), 0)
        self.assertTrue(r2["findings"][0]["latched"])

    def test_alert_text_names_seat_tokens_and_integrator(self):
        txt = silent_drop._alert_text(
            {"seat": "codex", "output_tokens": 103, "session": SID,
             "ts": "2026-07-23T10:00:00Z"})
        self.assertIn("@codex", txt)
        self.assertIn("@opus-integrator", txt)
        self.assertIn("103", txt)
        self.assertIn("SILENT-DROP", txt)


if __name__ == "__main__":
    unittest.main()
