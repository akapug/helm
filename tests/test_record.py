#!/usr/bin/env python3
"""record tests — the session-keyed tool-outcome recorder (PostToolUse).

Pins the laws: session_id-keyed state (never pane), fail-open + zero output on
every payload shape, NO subprocess on the passive hot path (git probed only on
dirtying tools, in the tool's workdir), the conservative stuck gate (reads
never arm it), the loop-thrash hash chain, the two verify-grounding artifacts
(command-log.jsonl with REAL exit codes, digests never raw command lines;
edit-targets.log basenames), O(1) append rotation, the apostrophe-payload
regression (the ancestor's python-c silent-noop class), and the claude wiring
(merge-preserving PostToolUse install + status + doctor rows). Hermetic:
HELM_HOME is a tmp dir; install tests re-point homes/configs at tmp roots
(the test_hooks pattern) — the live ~/.claude is never touched."""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("HELM_HOME", tempfile.mkdtemp(prefix="helm-test-home-"))

from helm import configs, doctor, homes, hooks, pk, record  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME")


class RecordBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-record-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ev(self, tool="Read", sid="sess-1", tin=None, resp=None, **extra):
        e = {"session_id": sid, "tool_name": tool, "cwd": self.tmp,
             "hook_event_name": "PostToolUse"}
        if tin is not None:
            e["tool_input"] = tin
        if resp is not None:
            e["tool_response"] = resp
        e.update(extra)
        return e

    def run_cmd(self, args, stdin_text=None):
        out, err = io.StringIO(), io.StringIO()
        stdin = io.StringIO(stdin_text or "")
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(sys, "stdin", stdin):
            rc = record.cmd_record(list(args))
        return rc, out.getvalue(), err.getvalue()

    def counters(self, sid="sess-1"):
        return record.counters(sid)

    def artifact(self, name, sid="sess-1"):
        try:
            with open(os.path.join(record.session_dir(sid), name),
                      encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""


class PassiveAndDirtyTest(RecordBase):
    def test_passive_streak_and_no_subprocess_on_passive_ops(self):
        probe = mock.Mock(return_value=False)
        with mock.patch.object(record, "_git_dirty", probe):
            for tool in ("Read", "Grep", "Glob"):
                record.record(self.ev(tool=tool))
            self.assertEqual(self.counters()["passive-streak"], 3)
            probe.assert_not_called()  # the <5ms hot path never forks
            record.record(self.ev(tool="Edit", tin={"file_path": "/x/y.py"}))
        self.assertEqual(self.counters()["passive-streak"], 0)

    def test_git_probed_only_on_dirtying_tools_in_tool_workdir(self):
        probe = mock.Mock(return_value=True)
        with mock.patch.object(record, "_git_dirty", probe):
            record.record(self.ev(tool="Bash",
                                  tin={"command": "touch x", "cwd": "/wt/a"}))
            probe.assert_called_once_with("/wt/a")
            self.assertEqual(self.counters()["last-dirty"], 1)
            self.assertEqual(self.counters()["dirty-streak"], 1)
            record.record(self.ev(tool="Read"))  # cached last-dirty, no re-probe
            self.assertEqual(probe.call_count, 1)
            self.assertEqual(self.counters()["dirty-streak"], 2)
            record.record(self.ev(tool="Bash",
                                  tin={"command": "git commit -m x"}))
        self.assertEqual(self.counters()["dirty-streak"], 0)  # commit resets
        self.assertEqual(self.counters()["passive-streak"], 0)  # commit = forward
        probe2 = mock.Mock(return_value=False)
        with mock.patch.object(record, "_git_dirty", probe2):
            record.record(self.ev(tool="Bash", tin={"command": "ls"}))
        self.assertEqual(self.counters()["last-dirty"], 0)
        self.assertEqual(self.counters()["dirty-streak"], 0)

    def test_workdir_falls_back_to_event_cwd(self):
        probe = mock.Mock(return_value=False)
        with mock.patch.object(record, "_git_dirty", probe):
            record.record(self.ev(tool="Write", tin={"file_path": "/a/b.py"}))
        probe.assert_called_once_with(self.tmp)


class SignalTest(RecordBase):
    def test_stuck_gated_to_action_tools_reads_never_arm(self):
        with mock.patch.object(record, "_git_dirty", return_value=False):
            record.record(self.ev(tool="Bash", tin={"command": "x"},
                                  resp={"stdout": "API Error: rate limit hit"}))
            self.assertEqual(self.counters()["stuck-signal"], 1)
            self.assertEqual(self.counters()["stuck-streak"], 1)
            record.record(self.ev(tool="Bash", tin={"command": "y"},
                                  resp="permission denied"))  # string resp tolerated
            self.assertEqual(self.counters()["stuck-streak"], 2)
            # a Read whose CONTENT mentions errors is data, not a signal
            record.record(self.ev(tool="Read",
                                  resp={"file": "docs on rate limits: API Error"}))
            self.assertEqual(self.counters()["stuck-signal"], 1)
            self.assertEqual(self.counters()["stuck-streak"], 2)
            record.record(self.ev(tool="Bash", tin={"command": "z"},
                                  resp={"stdout": "all good"}))
        self.assertEqual(self.counters()["stuck-signal"], 0)
        self.assertEqual(self.counters()["stuck-streak"], 0)

    def test_loop_thrash_hash_chain(self):
        with mock.patch.object(record, "_git_dirty", return_value=False):
            record.record(self.ev(tool="Bash", tin={"command": "make build"}))
            self.assertEqual(self.counters()["loop-streak"], 0)
            record.record(self.ev(tool="Bash", tin={"command": "make  build"}))
            self.assertEqual(self.counters()["loop-streak"], 1)  # ws-normalized
            record.record(self.ev(tool="Bash", tin={"command": "ls"}))
            self.assertEqual(self.counters()["loop-streak"], 0)
            record.record(self.ev(tool="Bash", tin={"command": "make build"}))
            self.assertEqual(self.counters()["loop-streak"], 1)  # A-B-A thrash
            for _ in range(record.HASH_WINDOW + 2):
                record.record(self.ev(tool="Bash", tin={"command": "ls"}))
        self.assertLessEqual(len(self.counters()["cmd-hash-chain"]),
                             record.HASH_WINDOW)


class ArtifactTest(RecordBase):
    def test_command_log_real_exit_codes_digests_never_raw(self):
        with mock.patch.object(record, "_git_dirty", return_value=False):
            record.record(self.ev(
                tool="Bash",
                tin={"command": "python3 -W error::ResourceWarning -m unittest "
                                "discover -s tests"},
                resp={"exitCode": 1, "stdout": "FAILED"}))
            record.record(self.ev(tool="Bash",
                                  tin={"command": "pytest -q secret_arg"},
                                  resp={"exitCode": 0}))
            record.record(self.ev(tool="Bash", tin={"command": "just check"},
                                  resp={"stdout": "ok"}))  # no exit key -> -1
            record.record(self.ev(tool="Bash", tin={"command": "ls -la"},
                                  resp={"exitCode": 0}))  # not a runner
        rows = [json.loads(l) for l in
                self.artifact("command-log.jsonl").splitlines()]
        self.assertEqual([r["exit"] for r in rows], [1, 0, -1])
        self.assertIn("unittest", rows[0]["token"])
        self.assertEqual(rows[1]["token"], "pytest")
        self.assertEqual(rows[2]["token"], "just check")
        raw = self.artifact("command-log.jsonl")
        self.assertNotIn("secret_arg", raw)          # digest, never the raw line
        self.assertNotIn("discover -s tests", raw)
        self.assertTrue(all(len(r["digest"]) == 12 for r in rows))

    def test_edit_targets_basenames(self):
        with mock.patch.object(record, "_git_dirty", return_value=False):
            record.record(self.ev(tool="Edit", tin={"file_path": "/a/b/c.py"}))
            record.record(self.ev(tool="Write", tin={"file_path": "/d/e.md"}))
            record.record(self.ev(tool="Read", tin={"file_path": "/f/g.py"}))
        self.assertEqual(self.artifact("edit-targets.log"), "c.py\ne.md\n")

    def test_append_rotation_is_one_generation(self):
        with mock.patch.object(record, "LOG_MAX", 64), \
                mock.patch.object(record, "_git_dirty", return_value=False):
            for _ in range(4):
                record.record(self.ev(tool="Bash", tin={"command": "pytest"},
                                      resp={"exitCode": 0}))
        d = record.session_dir("sess-1")
        self.assertTrue(os.path.exists(os.path.join(d, "command-log.jsonl.1")))


class KeyingAndFailOpenTest(RecordBase):
    def test_state_keyed_by_session_id(self):
        with mock.patch.object(record, "_git_dirty", return_value=False):
            record.record(self.ev(tool="Read", sid="alpha"))
            record.record(self.ev(tool="Read", sid="alpha"))
            record.record(self.ev(tool="Read", sid="beta"))
        self.assertEqual(record.counters("alpha")["passive-streak"], 2)
        self.assertEqual(record.counters("beta")["passive-streak"], 1)
        self.assertEqual(record.session_key("a/b c!"), "a_b_c_")

    def test_parse_event_gates_keyless_payloads(self):
        self.assertIsNone(record.parse_event("not json{"))
        self.assertIsNone(record.parse_event('["list"]'))
        self.assertIsNone(record.parse_event(json.dumps({"tool_name": "Read"})))
        self.assertIsNone(record.parse_event(json.dumps({"session_id": "s"})))
        e = record.parse_event(json.dumps(self.ev()))
        self.assertEqual(e["tool_name"], "Read")

    def test_cmd_record_silent_rc0_on_any_payload(self):
        rc, out, err = self.run_cmd(["--hook-json"], "garbled{{{")
        self.assertEqual((rc, out, err), (0, "", ""))
        rc, out, err = self.run_cmd([], json.dumps(self.ev(tool="Grep")))
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertEqual(self.counters()["passive-streak"], 1)

    def test_apostrophe_payload_records_clean(self):
        # the ancestor's bug class: tool content with quotes broke a shell
        # python -c wrapper into a silent noop. Pure argv verb: must record.
        cmd = "echo 'it'\\''s \"quoted\" — $weird `stuff`'"
        with mock.patch.object(record, "_git_dirty", return_value=False):
            rc, out, err = self.run_cmd(
                ["--hook-json"], json.dumps(self.ev(tool="Bash",
                                                    tin={"command": cmd})))
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertEqual(self.counters()["last-tool"], "Bash")

    def test_fail_open_when_state_unwritable(self):
        with mock.patch.object(pk, "write_json", side_effect=OSError("disk full")):
            record.record(self.ev())  # must not raise
        rc, out, err = self.run_cmd([], json.dumps(self.ev()))
        self.assertEqual(rc, 0)

    def test_unknown_subverb_usage(self):
        rc, _, err = self.run_cmd(["bogus"])
        self.assertEqual(rc, 2)
        self.assertIn("usage:", err)


class WiringBase(RecordBase):
    """test_hooks' hermetic homes/configs re-pointing, for install/status/doctor."""

    def setUp(self):
        super().setUp()
        j = lambda *p: os.path.join(self.tmp, *p)
        self._homes_orig = {k: getattr(homes, k) for k in ("ROOTS", "DEFAULTS")}
        homes.ROOTS = {"claude": j("claude-homes"), "codex": j("codex-homes")}
        homes.DEFAULTS = {"claude": j("default-claude"), "codex": j("default-codex")}
        os.makedirs(homes.ROOTS["claude"])
        os.makedirs(homes.DEFAULTS["claude"])
        self._cfg_orig = (configs.HOME_ROOTS, configs.BACKUP_DIR)
        configs.HOME_ROOTS = [homes.DEFAULTS["claude"]]
        configs.BACKUP_DIR = j("backups")

    def tearDown(self):
        for k, v in self._homes_orig.items():
            setattr(homes, k, v)
        configs.HOME_ROOTS, configs.BACKUP_DIR = self._cfg_orig
        super().tearDown()

    def mk_home(self, name, settings=None):
        d = os.path.join(homes.ROOTS["claude"], name)
        os.makedirs(d, exist_ok=True)
        configs.HOME_ROOTS.append(d)
        if settings is not None:
            with open(os.path.join(d, "settings.json"), "w") as f:
                json.dump(settings, f, indent=2)
        return d

    def read_settings(self, d):
        with open(os.path.join(d, "settings.json"), encoding="utf-8") as f:
            return json.load(f)


class WiringTest(WiringBase):
    def test_hook_command_fail_open_and_resolvable(self):
        cmd = record.hook_command()
        self.assertTrue(cmd.startswith("timeout "), cmd)
        self.assertTrue(cmd.endswith("|| true"), cmd)
        self.assertIn("record --hook-json", cmd)
        self.assertIn(hooks.helm_bin(), cmd)
        self.assertTrue(record._resolvable(cmd))
        self.assertFalse(record._resolvable("/gone/helm record --hook-json"))

    def test_install_covers_homes_idempotent_merge_preserving(self):
        a = self.mk_home("a-user-dev", settings={
            "model": "opus",
            "hooks": {
                "UserPromptSubmit": [{"hooks": [
                    {"type": "command", "command": hooks.hook_command()}]}],
                "PostToolUse": [{"matcher": "Bash", "hooks": [
                    {"type": "command", "command": "echo post"}]}]}})
        rc, out, err = self.run_cmd(["install"])
        self.assertEqual(rc, 0, err)
        self.assertIn("2 of 2 claude homes covered", out)
        got = self.read_settings(a)
        self.assertEqual(got["model"], "opus")  # foreign keys survive
        self.assertEqual(got["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"],
                         hooks.hook_command())  # inject entry survives
        cmds = record._event_cmds(got)
        self.assertIn("echo post", cmds)        # foreign PostToolUse survives
        self.assertIn(record.hook_command(), cmds)
        self.assertEqual(got["hooks"]["PostToolUse"][0]["matcher"], "Bash")
        rc, out, _ = self.run_cmd(["install"])  # idempotent
        self.assertEqual(rc, 0)
        self.assertIn("hook up to date", out)
        self.assertEqual(len([c for c in record._event_cmds(self.read_settings(a))
                              if record._ours(c)]), 1)

    def test_install_dry_writes_nothing(self):
        self.mk_home("a-user-dev")
        rc, out, _ = self.run_cmd(["install", "--dry"])
        self.assertEqual(rc, 0)
        self.assertIn("dry — nothing written", out)
        self.assertIn("record --hook-json", out)
        self.assertFalse(os.path.exists(
            os.path.join(homes.DEFAULTS["claude"], "settings.json")))

    def test_status_and_doctor_rows(self):
        self.mk_home("a-user-dev")
        rows = record.doctor_rows()
        self.assertEqual(rows[0][0], doctor.WARN)
        self.assertIn("record coverage: 0 of 2", rows[0][1])
        self.assertIn("helm record install", rows[0][1])
        rc, _, _ = self.run_cmd(["install"])
        self.assertEqual(rc, 0)
        rows = record.doctor_rows()
        self.assertEqual(rows[0], (doctor.OK, "record coverage: 2 of 2 claude homes"))
        self.assertEqual(rows[1][0], doctor.WARN)  # wired but never fired
        self.assertIn("no PostToolUse event", rows[1][1])
        with mock.patch.object(record, "_git_dirty", return_value=False):
            record.record(self.ev(tool="Read", sid="live-sess"))
        rows = record.doctor_rows()
        self.assertEqual(rows[1][0], doctor.OK)
        self.assertIn("reflex-state: 1 session", rows[1][1])
        rc, out, _ = self.run_cmd(["status"])
        self.assertEqual(rc, 0)
        self.assertIn("wiring: 2 of 2 claude homes", out)
        self.assertIn("live-sess", out)

    def test_status_empty_state(self):
        rc, out, _ = self.run_cmd(["status"])
        self.assertEqual(rc, 0)
        self.assertIn("no reflex-state yet", out)


if __name__ == "__main__":
    unittest.main()
