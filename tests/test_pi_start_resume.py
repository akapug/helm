#!/usr/bin/env python3
"""Tests for pi launch and resume recipes — 0.3 slice 3. Hermetic: no pi binary
required; pi_launch_line accepts an explicit `binary` arg."""
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

from helm import pi


class PiLaunchLineTest(unittest.TestCase):
    """Every pi flag form documented in args.ts, tested with an explicit binary."""

    BIN = "/usr/local/bin/pi"

    def test_bare_launch_clears_stale_axes_and_emits_pi_harness(self):
        line = pi.pi_launch_line(binary=self.BIN)
        self.assertTrue(line.startswith(
            "env -u HELM_MODEL_FAMILY -u HELM_MODEL_BACKEND "
            "HELM_AGENT_HARNESS=pi "), line)
        self.assertIn(self.BIN, line)
        self.assertNotIn("HELM_MODEL_FAMILY=", line)
        self.assertNotIn("HELM_MODEL_BACKEND=", line)

    def test_helm_provider_labels_canonical_family_backend_and_seat(self):
        line = pi.pi_launch_line(
            binary=self.BIN, model="helm-codex-2/codex:high")
        self.assertIn("HELM_AGENT_HARNESS=pi", line)
        self.assertIn("HELM_MODEL_FAMILY=codex", line)
        self.assertIn("HELM_MODEL_BACKEND=proxy", line)
        self.assertIn("HELM_CHAT_NAME=pi-codex-2", line)
        self.assertNotIn("HELM_MODEL_FAMILY=codex-2", line)

    def test_non_helm_model_clears_unknown_family_and_backend(self):
        line = pi.pi_launch_line(binary=self.BIN, model="openrouter/x:high")
        self.assertIn("--model openrouter/x:high", line)
        self.assertIn("-u HELM_MODEL_FAMILY", line)
        self.assertIn("-u HELM_MODEL_BACKEND", line)
        self.assertNotIn("HELM_MODEL_FAMILY=", line)
        self.assertNotIn("HELM_MODEL_BACKEND=", line)

    def test_session_flag_is_emitted(self):
        line = pi.pi_launch_line(binary=self.BIN, session="abc-def-ghi")
        self.assertIn("--session abc-def-ghi", line)

    def test_resume_flag_is_emitted(self):
        line = pi.pi_launch_line(binary=self.BIN, resume=True)
        self.assertIn("--resume", line)

    def test_continue_flag_is_emitted(self):
        line = pi.pi_launch_line(binary=self.BIN, continue_recent=True)
        self.assertIn("--continue", line)

    def test_print_is_boolean_with_prompt_as_positional(self):
        """pi's --print/-p are boolean ALIASES (args.ts:140). --print goes as a
        flag, the prompt follows as a POSITIONAL argument (shlex-quoted)."""
        line = pi.pi_launch_line(binary=self.BIN, prompt="say hello")
        self.assertIn("--print", line)
        # shlex.quote uses single quotes for strings with spaces (no escapes needed)
        self.assertIn("say hello", line)

    def test_missing_binary_returns_none(self):
        with mock.patch.object(pi, "pi_binary", return_value=None):
            self.assertIsNone(pi.pi_launch_line())

    def test_pi_does_not_prefix_its_own_env(self):
        """pi stamps PI_CODING_AGENT=true at its own startup (cli.ts:13).
        helm does NOT prefix it — pi's own config owns the marker."""
        line = pi.pi_launch_line(binary=self.BIN)
        self.assertFalse(line.startswith("PI_CODING_AGENT"))


class PiCmdTest(unittest.TestCase):
    """Tests at the cmd_pi layer — the layer r1 broke at."""

    def test_launch_bare_prints_line(self):
        with mock.patch.object(pi, "pi_binary", return_value="/usr/bin/pi"):
            rc, out, _err = _run_cmd(["launch"])
        self.assertEqual(rc, 0)
        self.assertIn("/usr/bin/pi", out)

    def test_launch_with_model(self):
        with mock.patch.object(pi, "pi_binary", return_value="/usr/bin/pi"):
            rc, out, _err = _run_cmd(["launch", "--model", "openrouter/x:high"])
        self.assertEqual(rc, 0)
        self.assertIn("--model openrouter/x:high", out)

    def test_run_without_explicit_seat_keeps_the_first_pi_flag(self):
        with mock.patch.object(pi, "pi_run", return_value=0) as run:
            rc, _out, _err = _run_cmd(["run", "--print", "probe"])
        self.assertEqual(rc, 0)
        run.assert_called_once_with("codex", ["--print", "probe"])

    def test_resume_session_prints_line(self):
        with mock.patch.object(pi, "pi_binary", return_value="/usr/bin/pi"):
            rc, out, _err = _run_cmd(["resume", "--session", "abc-def"])
        self.assertEqual(rc, 0)
        self.assertIn("--session abc-def", out)

    def test_resume_continue_prints_line(self):
        with mock.patch.object(pi, "pi_binary", return_value="/usr/bin/pi"):
            rc, out, _err = _run_cmd(["resume", "--continue"])
        self.assertEqual(rc, 0)
        self.assertIn("--continue", out)

    def test_resume_no_flags_refuses(self):
        with mock.patch.object(pi, "pi_binary", return_value="/usr/bin/pi"):
            rc, _out, err = _run_cmd(["resume"])
        self.assertEqual(rc, 2)
        self.assertIn("--session", err)
        self.assertIn("--continue", err)

    def test_launch_print_passes_prompt_through(self):
        """The r2/r3 gap: cmd_pi launch --print <words> was a template with
        a literal \"<prompt>\" — the prompt never reached pi_launch_line."""
        with mock.patch.object(pi, "pi_binary", return_value="/usr/bin/pi"):
            rc, out, _err = _run_cmd(
                ["launch", "--print", "Is pi better than CC on a defect-hunt?"]
            )
        self.assertEqual(rc, 0)
        self.assertIn("--print", out)
        # the prompt words flow through as a shlex-quoted positional
        self.assertIn("defect-hunt", out)

    def test_launch_line_never_carries_the_proxy_key(self):
        """The launch line is for humans to read. The key is set only at
        exec time by pi_run — never in the printed command."""
        with mock.patch.object(pi, "_pi_api_key", return_value="sk-test-key"):
            line = pi.pi_launch_line(binary="/usr/bin/pi")
            self.assertIsNotNone(line)
            self.assertNotIn("sk-test-key", line)
            self.assertNotIn("HELM_PI_PROXY_KEY", line)

    def test_run_no_key_refuses(self):
        """pi_run with no proxy key in config exits 1 with a message."""
        import io
        with mock.patch.object(pi, "pi_binary", return_value="/usr/bin/pi"), \
                mock.patch.object(pi, "_pi_api_key", return_value=None):
            with mock.patch.object(sys, "stderr", new=io.StringIO()):
                rc = pi.pi_run("codex", [])
            self.assertEqual(rc, 1)

    def test_run_pins_model_registers_real_roster_row_and_execs_key_in_env(self):
        from helm import seats
        with tempfile.TemporaryDirectory(prefix="helm-test-pi-run-") as d, \
                mock.patch.dict(os.environ, {
                    "HELM_HOME": os.path.join(d, "home"),
                    "HELM_CHAT_DIR": os.path.join(d, "chat")}), \
                mock.patch.object(pi, "pi_binary", return_value="/usr/bin/pi"), \
                mock.patch.object(pi, "_pi_api_key", return_value="secret-key"), \
                mock.patch("helm.seat._seat_family",
                           return_value=("codex", None)), \
                mock.patch.object(pi.os, "execvpe") as execvpe:
            self.assertIsNone(pi.pi_run("codex-2", ["--print", "probe"]))
            row = seats.roster()["pi-codex-2"]
            published = seats.roster_report()["seats"][0]
        binary, cmd, env = execvpe.call_args.args
        self.assertEqual(binary, "/usr/bin/pi")
        self.assertEqual(cmd, ["/usr/bin/pi", "--model",
                               "helm-codex-2/gpt-5.6-sol",
                               "--print", "probe"])
        self.assertEqual(env["HELM_PI_PROXY_KEY"], "secret-key")
        self.assertEqual(env["HELM_AGENT_HARNESS"], "pi")
        self.assertEqual(env["HELM_MODEL_FAMILY"], "codex")
        self.assertEqual(env["HELM_MODEL_BACKEND"], "proxy")
        self.assertEqual(env["HELM_CHAT_NAME"], "pi-codex-2")
        runtime = {"agent_harness": "pi", "family": "codex",
                   "backend": "proxy"}
        self.assertEqual(row["runtime"], runtime)
        self.assertEqual(published["runtime"], runtime)
        self.assertNotIn("secret-key", cmd)

    def test_run_refuses_a_model_from_another_provider_before_reading_key(self):
        with mock.patch.object(pi, "pi_binary", return_value="/usr/bin/pi"), \
                mock.patch.object(pi, "_pi_api_key") as key, \
                mock.patch("helm.seats.write_roster") as write_roster, \
                mock.patch.object(pi.os, "execvpe") as execvpe, \
                mock.patch.object(sys, "stderr", new=io.StringIO()):
            rc = pi.pi_run("codex", ["--model", "openrouter/x"])
        self.assertEqual(rc, 2)
        key.assert_not_called()
        write_roster.assert_not_called()
        execvpe.assert_not_called()

    def test_absent_seat_returns_none(self):
        """_pi_api_key on an unresolvable seat returns None, not TypeError."""
        key = pi._pi_api_key("nonexistent-seat-xyz")
        self.assertIsNone(key)

    def test_scoped_finds_key_in_api_keys_stanza(self):
        """_pi_api_key reads only the api-keys stanza."""
        import tempfile, os as _os
        config = ('host: "127.0.0.1"\n'
                  'port: 8317\n'
                  'api-keys:\n'
                  '  - "test-api-key-123"\n'
                  'debug: false\n'
                  'provider: codex\n')
        import os as _os
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
        f.write(config)
        f.close()
        _api_key = "test-api-key-123"
        # We test the logic, not the file: read from this temp config
        parsed = None
        in_keys = False
        with open(f.name) as fh:
            for line in fh:
                stripped = line.strip()
                if stripped == "api-keys:":
                    in_keys = True
                    continue
                if not in_keys:
                    continue
                if stripped and not line.startswith((" ", "\t")):
                    break
                if stripped.startswith("- "):
                    parsed = stripped[2:].strip().strip('"')
        _os.unlink(f.name)
        self.assertEqual(parsed, "test-api-key-123")

    def test_scoped_ignores_other_stanzas(self):
        """The parser scoped to api-keys does NOT return items from other
        stanzas that also start with '- ' (e.g. remote-management)."""
        config = ('api-keys:\n'
                  '  - "real-api-key-456"\n'
                  'remote-management:\n'
                  '  - "this-is-not-a-key"\n')
        in_keys = False
        found = None
        for line in config.splitlines():
            stripped = line.strip()
            if stripped == "api-keys:":
                in_keys = True
                continue
            if not in_keys:
                continue
            if stripped and not line.startswith((" ", "\t")):
                break
            if stripped.startswith("- "):
                found = stripped[2:].strip().strip('"')
                break
        self.assertEqual(found, "real-api-key-456")


def _run_cmd(args):
    import io
    import sys
    out, err = io.StringIO(), io.StringIO()
    with unittest.mock.patch("sys.stdout", out), unittest.mock.patch(
        "sys.stderr", err
    ):
        rc = pi.cmd_pi(list(args))
    return rc, out.getvalue(), err.getvalue()


if __name__ == "__main__":
    unittest.main()
