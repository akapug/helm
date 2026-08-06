#!/usr/bin/env python3
"""Orca selector misses keep the visible pane without losing the requested cwd.

Normal seat homes are adopted through the daemon RPC before spawn. A selector
can still miss for explicit cwd, preserved lane resumes, fail-open adoption, or
discovery timing. That narrow Orca failure retries without the registry selector
and carries cwd in the command; every other failure remains loud. The adapter
owns this once for every caller.
"""
import os
import shlex
import subprocess
import tempfile
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from helm import harness, sessions  # noqa: E402


class OrcaSpawnTest(unittest.TestCase):
    def setUp(self):
        self.ad = harness.OrcaAdapter("/usr/bin/orca")
        self.calls = []

    def _runner(self, fail_on_selector=False, fail_always=None):
        def run(args, env=None):
            self.calls.append(list(args))
            if fail_always:
                raise harness.HarnessError(fail_always)
            if fail_on_selector and any(a.startswith("path:") for a in args):
                raise harness.HarnessError(
                    'orca terminal create: rc 1 — {"code":"selector_not_found"}',
                    code="selector_not_found")
            return {"terminal": {"handle": "term_ok"}}
        return run

    def test_happy_path_still_uses_the_selector(self):
        with mock.patch.object(self.ad, "_run", side_effect=self._runner()):
            self.assertEqual(self.ad.spawn("launch.sh", title="seat",
                                           cwd="/w/seat"), "term_ok")
        self.assertEqual(len(self.calls), 1)
        self.assertIn("path:/w/seat", self.calls[0])

    def test_selector_miss_retries_and_still_returns_a_pane(self):
        with mock.patch.object(self.ad, "_run",
                               side_effect=self._runner(fail_on_selector=True)):
            self.assertEqual(self.ad.spawn("launch.sh", title="seat",
                                           cwd="/w/seat"), "term_ok")
        self.assertEqual(len(self.calls), 2)
        self.assertFalse(any(a.startswith("path:") for a in self.calls[1]))

    def test_retry_carries_cwd_in_the_command(self):
        with mock.patch.object(self.ad, "_run",
                               side_effect=self._runner(fail_on_selector=True)):
            self.ad.spawn("/x/launch.sh", title="seat", cwd="/w/seat")
        cmd = self.calls[1][self.calls[1].index("--command") + 1]
        self.assertIn("cd /w/seat", cmd)
        self.assertIn("exec sh -lc /x/launch.sh", cmd)

    def test_retry_quotes_a_hostile_path(self):
        with mock.patch.object(self.ad, "_run",
                               side_effect=self._runner(fail_on_selector=True)):
            self.ad.spawn("/x/launch.sh", title="seat", cwd="/w/a b;rm -rf /")
        cmd = self.calls[1][self.calls[1].index("--command") + 1]
        self.assertIn("'/w/a b;rm -rf /'", cmd)

    def test_fallback_preserves_compound_commands_and_assignments(self):
        with tempfile.TemporaryDirectory(prefix="helm-test-orca-command-") as cwd:
            out = os.path.join(cwd, "out")
            command = ("X=kept sh -c 'printf %s \"$X\" > out' && "
                       "printf '\\nsecond' >> out")
            with mock.patch.object(self.ad, "_run",
                                   side_effect=self._runner(fail_on_selector=True)):
                self.ad.spawn(command, title="seat", cwd=cwd)
            wrapped = self.calls[1][self.calls[1].index("--command") + 1]
            ran = subprocess.run(["sh", "-c", wrapped], capture_output=True,
                                 text=True, timeout=5)
            self.assertEqual(ran.returncode, 0, ran.stderr)
            with open(out) as f:
                self.assertEqual(f.read(), "kept\nsecond")

    def test_non_selector_failure_stays_loud_and_is_not_retried(self):
        with mock.patch.object(self.ad, "_run",
                               side_effect=self._runner(fail_always="daemon unavailable")):
            with self.assertRaises(harness.HarnessError):
                self.ad.spawn("launch.sh", title="seat", cwd="/w/seat")
        self.assertEqual(len(self.calls), 1)

    def test_near_match_or_incidental_text_is_not_retried(self):
        errors = (
            harness.HarnessError("near match", code="selector_not_found_after_timeout"),
            harness.HarnessError("prior selector_not_found cached"),
        )
        for error in errors:
            with self.subTest(error=str(error)), \
                 mock.patch.object(self.ad, "_run", side_effect=error) as run:
                with self.assertRaises(harness.HarnessError):
                    self.ad.spawn("launch.sh", title="seat", cwd="/w/seat")
            self.assertEqual(run.call_count, 1)

    def test_cli_json_error_code_drives_the_narrow_retry(self):
        failed = mock.Mock(returncode=1, stdout="",
                           stderr='{"code":"selector_not_found"}')
        passed = mock.Mock(returncode=0, stderr="", stdout=(
            '{"result":{"terminal":{"handle":"term_ok"}}}'))
        with mock.patch.object(harness.subprocess, "run",
                               side_effect=[failed, passed]) as run:
            self.assertEqual(self.ad.spawn("launch.sh", title="seat",
                                           cwd="/w/seat"), "term_ok")
        self.assertEqual(run.call_count, 2)

    def test_no_cwd_means_no_selector_and_no_retry(self):
        with mock.patch.object(self.ad, "_run", side_effect=self._runner()):
            self.assertEqual(self.ad.spawn("launch.sh", title="seat"), "term_ok")
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("--worktree", self.calls[0])

    def test_session_resume_in_an_unadopted_lane_uses_adapter_fallback(self):
        with tempfile.TemporaryDirectory(prefix="helm-test-orca-resume-") as cwd:
            observed = os.path.join(cwd, "observed-cwd")
            script = os.path.join(cwd, "resume.sh")
            with open(script, "w") as f:
                f.write("#!/bin/sh\npwd > %s\n" % shlex.quote(observed))
            os.chmod(script, 0o700)
            row = {"i": "uuid-resume", "h": "claude", "cwd": cwd}
            with mock.patch.object(self.ad, "_run",
                                   side_effect=self._runner(fail_on_selector=True)), \
                 mock.patch.object(harness, "detect", return_value=self.ad), \
                 mock.patch.object(sessions, "mint_resume_script",
                                   return_value=script):
                path, handle, adapter = sessions.spawn_resume(row)
            self.assertEqual((path, handle, adapter), (script, "term_ok", "orca"))
            self.assertEqual(len(self.calls), 2, "the adapter owns the only retry")
            cmd = self.calls[1][self.calls[1].index("--command") + 1]
            self.assertIn("cd %s" % shlex.quote(cwd), cmd)
            ran = subprocess.run(["sh", "-c", cmd], capture_output=True,
                                 text=True, timeout=5)
            self.assertEqual(ran.returncode, 0, ran.stderr)
            with open(observed) as f:
                self.assertEqual(f.read().strip(), cwd)


if __name__ == "__main__":
    unittest.main()
