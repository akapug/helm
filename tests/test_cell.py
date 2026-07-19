#!/usr/bin/env python3
"""cell tests — hermetic: the binary is a stub script (HELM_CELL_BIN), the
helm home is a tempdir (HELM_HOME). The real node, ~/.dregg and ~/.helm are
never touched."""
import contextlib
import io
import os
import shutil
import stat
import tempfile
import unittest

os.environ.setdefault("HELM_HOME", tempfile.mkdtemp(prefix="helm-test-home-"))

from helm import cell, home  # noqa: E402

CELL_HEX = "ab" * 32
TURN = "cd" * 32

ENV_KEYS = ("HELM_HOME", "HELM_CELL_BIN", "MELD_CELL_BIN", "HELM_NODE_URL",
            "HELM_CELL_PROFILE", "HELM_NODE_TOKEN", "HELM_NODE_PASSPHRASE",
            "HELM_ROSTER", "MELD_NODE_URL", "MELD_AGENT_PROFILE",
            "MELD_NODE_TOKEN", "MELD_NODE_PASSPHRASE", "MELD_ROSTER",
            "HELM_SNAPSHOT_HOOK", "MELD_SNAPSHOT_HOOK",
            "STUB_LOG", "STUB_CELL", "STUB_TURN")

# One stub for every subcommand: records argv + the mapped env per call,
# answers join/send with the binary's real one-JSON-line stdout contract.
STUB = """#!/bin/sh
{
  echo "argv:$@"
  echo "MELD_NODE_URL=$MELD_NODE_URL"
  echo "MELD_AGENT_PROFILE=$MELD_AGENT_PROFILE"
  echo "MELD_NODE_TOKEN=$MELD_NODE_TOKEN"
  echo "MELD_ROSTER=$MELD_ROSTER"
} >> "$STUB_LOG"
case "$1" in
  join) echo '{"joined":true,"node":"stub","profile":"'"$MELD_AGENT_PROFILE"'","cell":"'"$STUB_CELL"'","turn_hash":"tj","receipt_hash":"rj","chain_index":1}';;
  send) echo '{"sent":true,"to":"'"$STUB_CELL"'","seq":1,"bytes":73,"slots":11,"turn_hash":"'"$STUB_TURN"'","receipt_hash":"rs","chain_index":2}';;
esac
exit 0
"""

FAILING_STUB = "#!/bin/sh\necho 'boom: node gate refused' >&2\nexit 1\n"


class CellBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-cell-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_SNAPSHOT_HOOK"] = ""  # never a real cave snapshot
        self.log = os.path.join(self.tmp, "stub.log")
        os.environ["STUB_LOG"] = self.log
        os.environ["STUB_CELL"] = CELL_HEX
        os.environ["STUB_TURN"] = TURN

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_stub(self, body=STUB):
        path = os.path.join(self.tmp, "meld-stub")
        with open(path, "w") as f:
            f.write(body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        os.environ["HELM_CELL_BIN"] = path
        return path

    def stub_calls(self):
        try:
            with open(self.log) as f:
                return f.read()
        except OSError:
            return ""

    def run_cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cell.cmd_cell(args)
        return rc, out.getvalue(), err.getvalue()


class EnvMapTest(CellBase):
    def test_helm_env_wins_over_legacy(self):
        os.environ.update({"HELM_NODE_URL": "http://helm:1", "MELD_NODE_URL": "http://old:2",
                           "HELM_CELL_PROFILE": "helm-prof", "MELD_AGENT_PROFILE": "old-prof",
                           "HELM_NODE_TOKEN": "helm-tok", "HELM_ROSTER": "/tmp/helm-roster",
                           "HELM_NODE_PASSPHRASE": "helm-pass"})
        env = cell.build_env()
        self.assertEqual(env["MELD_NODE_URL"], "http://helm:1")
        self.assertEqual(env["MELD_AGENT_PROFILE"], "helm-prof")
        self.assertEqual(env["MELD_NODE_TOKEN"], "helm-tok")
        self.assertEqual(env["MELD_NODE_PASSPHRASE"], "helm-pass")
        self.assertEqual(env["MELD_ROSTER"], "/tmp/helm-roster")

    def test_legacy_fallback_untouched_without_helm(self):
        os.environ.update({"MELD_NODE_URL": "http://old:2",
                           "MELD_AGENT_PROFILE": "old-prof"})
        env = cell.build_env()
        self.assertEqual(env["MELD_NODE_URL"], "http://old:2")
        self.assertEqual(env["MELD_AGENT_PROFILE"], "old-prof")
        self.assertNotIn("MELD_NODE_TOKEN", env)

    def test_subprocess_sees_mapped_env(self):
        self.write_stub()
        os.environ.update({"HELM_NODE_URL": "http://helm:1",
                           "HELM_CELL_PROFILE": "helm-prof",
                           "HELM_NODE_TOKEN": "tok", "HELM_ROSTER": "/r.toml"})
        rc, _, _ = self.run_cli(["roster", "--json"])
        self.assertEqual(rc, 0)
        log = self.stub_calls()
        self.assertIn("argv:roster --json", log)
        self.assertIn("MELD_NODE_URL=http://helm:1", log)
        self.assertIn("MELD_AGENT_PROFILE=helm-prof", log)
        self.assertIn("MELD_NODE_TOKEN=tok", log)
        self.assertIn("MELD_ROSTER=/r.toml", log)

    def test_profile_name_precedence(self):
        self.assertEqual(cell.profile_name(), "meld-agent")
        os.environ["MELD_AGENT_PROFILE"] = "legacy"
        self.assertEqual(cell.profile_name(), "legacy")
        os.environ["HELM_CELL_PROFILE"] = "new"
        self.assertEqual(cell.profile_name(), "new")
        self.assertEqual(cell.profile_name(default="helm-test"), "new")


class BinPathTest(CellBase):
    def test_explicit_env_wins(self):
        os.environ["HELM_CELL_BIN"] = "/some/where/meld"
        self.assertEqual(cell.bin_path(), "/some/where/meld")

    def test_legacy_bin_env_fallback(self):
        os.environ["MELD_CELL_BIN"] = "/legacy/meld"
        self.assertEqual(cell.bin_path(), "/legacy/meld")

    def test_missing_binary_is_graceful(self):
        os.environ["HELM_CELL_BIN"] = os.path.join(self.tmp, "no-such-binary")
        rc, _, err = self.run_cli(["join"])
        self.assertEqual(rc, 1)
        self.assertIn("substrate unavailable", err)
        rc2, _, err2 = cell.run_bin(["join"])
        self.assertIsNone(rc2)
        self.assertIn("not found", err2)

    def test_unknown_verb_and_usage(self):
        rc, _, err = self.run_cli([])
        self.assertEqual(rc, 2)
        rc, _, err = self.run_cli(["frobnicate"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown verb", err)


class OwnCellTest(CellBase):
    def test_join_parses_and_caches_cell_hex(self):
        self.write_stub()
        hexid, err = cell.own_cell("p1")
        self.assertIsNone(err)
        self.assertEqual(hexid, CELL_HEX)
        self.assertEqual(self.stub_calls().count("argv:join"), 1)
        # cache hit: no second binary invocation
        hexid2, err2 = cell.own_cell("p1")
        self.assertEqual((hexid2, err2), (CELL_HEX, None))
        self.assertEqual(self.stub_calls().count("argv:join"), 1)
        # the cache lives under the (tmp) helm home
        cache = cell._cells_cache_path()
        self.assertTrue(cache.startswith(home.helm_home()))
        with open(cache) as f:
            self.assertIn(CELL_HEX, f.read())

    def test_join_failure_reports_reason(self):
        self.write_stub(FAILING_STUB)
        hexid, err = cell.own_cell("p1")
        self.assertIsNone(hexid)
        self.assertIn("meld join failed", err)
        self.assertIn("node gate refused", err)


class SendSelfTest(CellBase):
    def test_self_write_targets_own_cell(self):
        self.write_stub()
        info, err = cell.send_self("prem:b2b:deadbeef", "p1")
        self.assertIsNone(err)
        self.assertEqual(info["turn_hash"], TURN)
        self.assertEqual(info["chain_index"], 2)
        log = self.stub_calls()
        self.assertIn("argv:join --profile p1", log)
        self.assertIn("argv:send --profile p1 --to %s prem:b2b:deadbeef" % CELL_HEX, log)

    def test_send_failure_reports_reason(self):
        self.write_stub()
        cell.own_cell("p1")  # prime the cache with the good stub
        self.write_stub(FAILING_STUB)
        info, err = cell.send_self("prem:b2b:deadbeef", "p1")
        self.assertIsNone(info)
        self.assertIn("meld send failed", err)

    def test_snapshot_hook_fires_after_attestation_only(self):
        # the log-after leg of the unified cave: a SUCCESSFUL send_self fires
        # the snapshot hook; a failed one (no turn landed) must not
        mark = os.path.join(self.tmp, "snapped")
        hook = os.path.join(self.tmp, "snapshot-hook")
        with open(hook, "w") as f:
            f.write("#!/bin/sh\necho hit >> %s\n" % mark)
        os.chmod(hook, 0o755)
        os.environ["HELM_SNAPSHOT_HOOK"] = hook
        self.write_stub(FAILING_STUB)
        cell.send_self("prem:b2b:deadbeef", "p1")
        self.assertFalse(os.path.exists(mark))
        self.write_stub()
        info, err = cell.send_self("prem:b2b:deadbeef", "p1")
        self.assertIsNone(err)
        with open(mark) as f:
            self.assertEqual(f.read().strip(), "hit")

    def test_snapshot_hook_absent_or_disabled_is_a_noop(self):
        self.assertFalse(cell.fire_snapshot_hook())          # "" -> disabled
        os.environ["HELM_SNAPSHOT_HOOK"] = os.path.join(self.tmp, "missing")
        self.assertFalse(cell.fire_snapshot_hook())          # not executable

    def test_snapshot_hook_nonzero_exit_is_not_a_snapshot(self):
        # a failed flush (disk full) must not read as snapshotted
        hook = os.path.join(self.tmp, "bad-hook")
        with open(hook, "w") as f:
            f.write("#!/bin/sh\nexit 3\n")
        os.chmod(hook, 0o755)
        os.environ["HELM_SNAPSHOT_HOOK"] = hook
        self.assertFalse(cell.fire_snapshot_hook())


class StatusTest(CellBase):
    def test_unreachable_node_is_graceful(self):
        os.environ["HELM_NODE_URL"] = "http://127.0.0.1:1"  # nothing listens
        os.environ["HELM_ROSTER"] = os.path.join(self.tmp, "roster.toml")
        with open(os.environ["HELM_ROSTER"], "w") as f:
            f.write('interval_secs = 60\n[[cell]]\nid = "%s"\nlabel = "t1"\n' % CELL_HEX)
        rc, out, _ = self.run_cli(["status"])
        self.assertEqual(rc, 1)
        self.assertIn("UNREACHABLE", out)
        self.assertIn("1 cell (t1)", out)
        self.assertIn("profile 'meld-agent'", out)


if __name__ == "__main__":
    unittest.main()
