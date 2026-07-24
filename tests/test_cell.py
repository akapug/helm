#!/usr/bin/env python3
"""cell tests — hermetic. The dregg node leg is OPTIONAL and fail-open: the
node reader/writer is mocked or aimed at a dead port. The OPTIONAL a2a
transport degrades with no HELM_CELL_BIN. No meld binary is needed for
attestation — this module never attests (that is premise.py, native)."""
import contextlib
import io
import os
import shutil
import stat
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("HELM_HOME", tempfile.mkdtemp(prefix="helm-test-home-"))

from helm import cell  # noqa: E402

CELL_HEX = "ab" * 32
TURN = "cd" * 32

ENV_KEYS = ("HELM_HOME", "HELM_CELL_BIN", "MELD_CELL_BIN", "HELM_NODE_URL",
            "HELM_CELL_PROFILE", "HELM_NODE_TOKEN", "HELM_NODE_PASSPHRASE",
            "HELM_ROSTER", "HELM_NODE_ANCHOR_FEE", "HELM_NODE_ANCHOR_TIMEOUT",
            "MELD_NODE_URL", "MELD_AGENT_PROFILE",
            "MELD_NODE_TOKEN", "MELD_NODE_PASSPHRASE", "MELD_ROSTER",
            "DREGG_NODE_URL", "DREGG_PROFILE",
            "DREGG_API_TOKEN", "DREGG_NODE_PASSPHRASE")

DEAD = "http://127.0.0.1:1"   # nothing listens — fail-open, fast


class CellBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-cell-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_NODE_URL"] = DEAD

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cell.cmd_cell(args)
        return rc, out.getvalue(), err.getvalue()


class NodeUrlTest(CellBase):
    def test_helm_wins_over_legacy_and_strips(self):
        os.environ["HELM_NODE_URL"] = "http://helm:1/"
        os.environ["MELD_NODE_URL"] = "http://old:2"
        self.assertEqual(cell.node_url(), "http://helm:1")

    def test_legacy_fallback_when_no_helm(self):
        os.environ.pop("HELM_NODE_URL", None)
        os.environ["MELD_NODE_URL"] = "http://old:2/"
        self.assertEqual(cell.node_url(), "http://old:2")

    def test_default(self):
        os.environ.pop("HELM_NODE_URL", None)
        self.assertEqual(cell.node_url(), cell.DEFAULT_NODE_URL)

    def test_profile_name_precedence(self):
        self.assertEqual(cell.profile_name(), "helm-agent")
        os.environ["MELD_AGENT_PROFILE"] = "legacy"
        self.assertEqual(cell.profile_name(), "legacy")
        os.environ["HELM_CELL_PROFILE"] = "new"
        self.assertEqual(cell.profile_name(), "new")
        self.assertEqual(cell.profile_name(default="helm-test"), "new")


class AnchorSubmitTest(CellBase):
    def test_accepted_returns_turn_hash(self):
        with mock.patch.object(cell, "post_json",
                               return_value={"accepted": True, "turn_hash": TURN}):
            turn, err = cell.anchor_submit("ab" * 32)
        self.assertIsNone(err)
        self.assertEqual(turn, TURN)

    def test_refusal_is_failopen_reason(self):
        with mock.patch.object(cell, "post_json",
                               return_value={"accepted": False, "error": "locked"}):
            turn, err = cell.anchor_submit("ab" * 32)
        self.assertIsNone(turn)
        self.assertIn("locked", err)

    def test_non_dict_json_fails_open_never_raises(self):
        # a valid JSON list/string/number is NOT acceptance — guard with
        # isinstance(dict) before .get(), fail open, never raise AttributeError.
        for resp in ([1, 2, 3], "ok", 42, [{"turn_hash": TURN}]):
            with mock.patch.object(cell, "post_json", return_value=resp):
                turn, err = cell.anchor_submit("ab" * 32)
            self.assertIsNone(turn, resp)
            self.assertIn("non-object", err)

    def test_anchor_declares_coordination_fee_zero_env_overridable(self):
        # (Stage B): the attest anchor is a COORDINATION turn (EmitEvent-only,
        # no balance_change), so it declares fee=0 and rides dregg's
        # coordination-exempt admission free — no cell drain, no faucet grant, no
        # [unsigned] throttle. Env-overridable via HELM_NODE_COORD_FEE for a node
        # that has NOT opted into the exempt class.
        seen = {}

        def fake_post(url, payload, timeout=8, headers=None):
            seen["fee"] = payload.get("fee")
            return {"accepted": True, "turn_hash": TURN}

        self.assertEqual(cell.DEFAULT_COORD_FEE, 0)
        with mock.patch.object(cell, "post_json", fake_post):
            cell.anchor_submit("ab" * 32)
        self.assertEqual(seen["fee"], 0)
        os.environ["HELM_NODE_COORD_FEE"] = "1000"
        try:
            with mock.patch.object(cell, "post_json", fake_post):
                cell.anchor_submit("ab" * 32)
            self.assertEqual(seen["fee"], 1000)
        finally:
            os.environ.pop("HELM_NODE_COORD_FEE", None)

    def test_turn_fee_zeroes_coordination_only_not_economic(self):
        # The client half of the leash: fee=0 ONLY for the coordination class
        # (all effects emit_event, no balance_change); an economic effect or any
        # balance_change keeps the full anchor_fee() so economic turns are never
        # zeroed — mirrors dregg Turn::is_coordination.
        coord = [{"method": "attest",
                  "effects": [{"kind": "emit_event", "topic": "t"}]}]
        econ_bc = [{"method": "pay",
                    "balance_change": {"cell": "x", "delta": -5},
                    "effects": [{"kind": "emit_event", "topic": "t"}]}]
        econ_eff = [{"method": "x",
                     "effects": [{"kind": "transfer"},
                                 {"kind": "emit_event"}]}]
        self.assertTrue(cell.is_coordination_actions(coord))
        self.assertEqual(cell.turn_fee(coord), 0)
        self.assertFalse(cell.is_coordination_actions(econ_bc))
        self.assertEqual(cell.turn_fee(econ_bc), cell.anchor_fee())
        self.assertFalse(cell.is_coordination_actions(econ_eff))
        self.assertEqual(cell.turn_fee(econ_eff), cell.anchor_fee())
        # empty forest and effect-less actions are NOT coordination
        self.assertFalse(cell.is_coordination_actions([]))
        self.assertFalse(cell.is_coordination_actions([{"method": "x",
                                                        "effects": []}]))

    def test_unreachable_is_failopen(self):
        # real fail-open against the dead port — no mock, must not raise
        turn, err = cell.anchor_submit("ab" * 32)
        self.assertIsNone(turn)
        self.assertIn("unreachable", err)

    def test_bearer_and_digest_word_wire(self):
        seen = {}

        def fake_post(url, payload, timeout=8, headers=None):
            seen["url"] = url
            seen["payload"] = payload
            seen["headers"] = headers or {}
            return {"accepted": True, "turn_hash": TURN}

        os.environ["HELM_NODE_TOKEN"] = "tok"
        with mock.patch.object(cell, "post_json", fake_post):
            cell.anchor_submit("ab" * 32, memo="helm-attest:v2:law-x")
        self.assertTrue(seen["url"].endswith(cell.ANCHOR_ENDPOINT))
        self.assertEqual(seen["headers"].get("Authorization"), "Bearer tok")
        eff = seen["payload"]["actions"][0]["effects"][0]
        self.assertEqual(eff["kind"], "emit_event")
        self.assertEqual(eff["data"], ["ab" * 32])   # the 64-hex record word
        self.assertEqual(seen["payload"]["memo"], "helm-attest:v2:law-x")

    def test_anchor_label_is_honest_node_anchored(self):
        os.environ["HELM_NODE_URL"] = "http://node:9"
        label = cell.anchor_label(TURN)
        self.assertIn("dregg node http://node:9 anchored", label)
        self.assertIn(TURN, label)
        self.assertNotIn("signed", label)   # never a signer claim


class VerifyAnchorTest(CellBase):
    def test_proof_present_is_turn_observed_not_payload_bound(self):
        # a present proof means the turn EXISTS, not that it commits this
        # record's hash — the detail says so honestly.
        with mock.patch.object(cell, "get_json",
                               return_value={"turn_hash": TURN, "proof_len": 12}):
            ok, detail = cell.verify_anchor(TURN)
        self.assertTrue(ok)
        self.assertIn("turn present", detail)
        self.assertIn("payload binding unavailable", detail)

    def test_receipt_present_is_confirmed(self):
        def by_endpoint(url, timeout=4):
            return [{"turn_hash": TURN}] if "starbridge" in url else None
        with mock.patch.object(cell, "get_json", by_endpoint):
            ok, detail = cell.verify_anchor(TURN)
        self.assertTrue(ok)
        self.assertIn("receipt present", detail)

    def test_unreachable_is_unverified(self):
        with mock.patch.object(cell, "get_json", return_value=None):
            ok, detail = cell.verify_anchor(TURN)
        self.assertFalse(ok)
        self.assertIn("unreachable", detail)


class BinPathTest(CellBase):
    def test_explicit_env_wins(self):
        os.environ["HELM_CELL_BIN"] = "/some/where/cellbin"
        self.assertEqual(cell.bin_path(), "/some/where/cellbin")

    def test_legacy_bin_env_fallback(self):
        os.environ["MELD_CELL_BIN"] = "/legacy/cellbin"
        self.assertEqual(cell.bin_path(), "/legacy/cellbin")

    def test_no_auto_resolution_returns_none(self):
        # never a PATH probe, never a sibling-build guess — unset => None
        self.assertIsNone(cell.bin_path())
        self.assertEqual(cell.bin_status()["state"], "unset")

    def test_bin_status_distinguishes_missing_and_non_executable(self):
        missing = os.path.join(self.tmp, "deleted-signer\x1b[2J")
        os.environ["HELM_CELL_BIN"] = missing
        status = cell.bin_status()
        self.assertEqual((status["configured"], status["usable"], status["state"]),
                         (True, False, "missing"))
        self.assertIn("path does not exist", status["reason"])
        self.assertNotIn(missing, status["reason"])

        directory = os.path.join(self.tmp, "signer-dir")
        os.mkdir(directory)
        os.environ["HELM_CELL_BIN"] = directory
        self.assertEqual(cell.bin_status()["state"], "not_file")

        os.environ["HELM_CELL_BIN"] = missing
        with open(missing, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(missing, 0o600)
        status = cell.bin_status()
        self.assertEqual(status["state"], "not_executable")
        self.assertIn("not executable", status["reason"])
        os.chmod(missing, 0o700)
        self.assertEqual(cell.bin_status()["state"], "ready")

    def test_run_bin_degrades_without_binary(self):
        rc, out, err = cell.run_bin(["roster"])
        self.assertIsNone(rc)
        self.assertIn("a2a transport unavailable", err)


class BuildEnvTest(CellBase):
    def test_helm_maps_onto_meld_and_dregg_names(self):
        # the dregg-native signer (dregg-client-sign) reads DREGG_*; the
        # legacy meld-style bin reads MELD_* — one HELM_* feeds both
        os.environ.update(HELM_NODE_URL="http://helm:1", HELM_CELL_PROFILE="p1",
                          HELM_NODE_TOKEN="tok", HELM_NODE_PASSPHRASE="pw")
        env = cell.build_env()
        self.assertEqual(env["MELD_NODE_URL"], "http://helm:1")
        self.assertEqual(env["DREGG_NODE_URL"], "http://helm:1")
        self.assertEqual(env["MELD_AGENT_PROFILE"], "p1")
        self.assertEqual(env["DREGG_PROFILE"], "p1")
        self.assertEqual(env["MELD_NODE_TOKEN"], "tok")
        self.assertEqual(env["DREGG_API_TOKEN"], "tok")
        self.assertEqual(env["DREGG_NODE_PASSPHRASE"], "pw")

    def test_absent_helm_leaves_direct_dregg_env_untouched(self):
        # env2: no HELM_NODE_TOKEN set — a directly-exported DREGG_API_TOKEN
        # survives; unset HELM vars never mint empty DREGG ones
        os.environ["DREGG_API_TOKEN"] = "direct"
        env = cell.build_env()
        self.assertEqual(env["DREGG_API_TOKEN"], "direct")
        self.assertNotIn("DREGG_NODE_PASSPHRASE", env)

    def test_signer_env_file_fills_gaps_but_os_environ_wins(self):
        # An operator's signer.env configures the signer's runtime posture (e.g.
        # a marshal-only devnet's DREGG_ALLOW_UNAUDITED_PQ) ONCE, read fresh on
        # every signed turn so a running seat picks it up with no relaunch.
        # Absent file => no-op; the file fills GAPS only, real env WINS.
        self.assertNotIn("DREGG_ALLOW_UNAUDITED_PQ", cell.build_env())  # absent file
        sp = os.path.join(cell.home.helm_home(), "signer.env")
        os.makedirs(os.path.dirname(sp), exist_ok=True)
        with open(sp, "w", encoding="utf-8") as f:
            f.write("# devnet marshal-only posture\n"
                    "DREGG_ALLOW_UNAUDITED_PQ=1\n"
                    "\n"
                    "DREGG_API_TOKEN=fromfile\n")
        os.environ["DREGG_API_TOKEN"] = "fromenv"   # explicit env must WIN
        env = cell.build_env()
        self.assertEqual(env["DREGG_ALLOW_UNAUDITED_PQ"], "1")  # gap filled
        self.assertEqual(env["DREGG_API_TOKEN"], "fromenv")     # env beats file

    def test_signer_env_file_explicit_path_and_malformed_lines(self):
        sp = os.path.join(self.tmp, "custom-signer.env")
        with open(sp, "w", encoding="utf-8") as f:
            f.write("# comment\nNOEQUALS\n  DREGG_ALLOW_UNAUDITED_PQ = 1 \n")
        os.environ["HELM_CELL_ENV_FILE"] = sp
        try:
            env = cell.build_env()
            self.assertEqual(env["DREGG_ALLOW_UNAUDITED_PQ"], "1")  # trimmed
            self.assertNotIn("NOEQUALS", env)                       # skipped
        finally:
            os.environ.pop("HELM_CELL_ENV_FILE", None)


class A2ATransportTest(CellBase):
    STUB = ("#!/bin/sh\n"
            'echo "argv:$@" >> "$STUB_LOG"\n'
            'echo "MELD_NODE_URL=$MELD_NODE_URL" >> "$STUB_LOG"\n'
            'echo "DREGG_NODE_URL=$DREGG_NODE_URL" >> "$STUB_LOG"\n'
            "exit 0\n")

    def write_stub(self):
        path = os.path.join(self.tmp, "cellbin-stub")
        with open(path, "w") as f:
            f.write(self.STUB)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        os.environ["HELM_CELL_BIN"] = path
        os.environ["STUB_LOG"] = os.path.join(self.tmp, "stub.log")
        return path

    def tearDown(self):
        os.environ.pop("STUB_LOG", None)
        super().tearDown()

    def test_passthrough_runs_when_binary_present(self):
        self.write_stub()
        os.environ["HELM_NODE_URL"] = "http://helm:1"
        rc, _, _ = self.run_cli(["roster", "--json"])
        self.assertEqual(rc, 0)
        with open(os.environ["STUB_LOG"]) as f:
            log = f.read()
        self.assertIn("argv:roster --json", log)
        self.assertIn("MELD_NODE_URL=http://helm:1", log)   # env2 mapping
        self.assertIn("DREGG_NODE_URL=http://helm:1", log)  # dregg-native name

    def test_passthrough_degrades_without_binary(self):
        rc, _, err = self.run_cli(["send", "--to", CELL_HEX, "x"])
        self.assertEqual(rc, 1)
        self.assertIn("a2a transport unavailable", err)

    def test_unknown_verb_and_usage(self):
        rc, _, _ = self.run_cli([])
        self.assertEqual(rc, 2)
        rc, _, err = self.run_cli(["frobnicate"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown verb", err)


class StatusTest(CellBase):
    def test_unreachable_node_is_graceful(self):
        os.environ["HELM_ROSTER"] = os.path.join(self.tmp, "roster.toml")
        with open(os.environ["HELM_ROSTER"], "w") as f:
            f.write('interval_secs = 60\n[[cell]]\nid = "%s"\nlabel = "t1"\n' % CELL_HEX)
        rc, out, _ = self.run_cli(["status"])
        self.assertEqual(rc, 1)
        self.assertIn("UNREACHABLE", out)
        self.assertIn("1 cell (t1)", out)
        self.assertIn("a2a transport OFF", out)

    def test_live_node_reports_head(self):
        with mock.patch.object(cell, "get_json", return_value=[
                {"chain_index": 5, "finality": "tentative"}]):
            rc, out, _ = self.run_cli(["status"])
        self.assertEqual(rc, 0)
        self.assertIn("node LIVE", out)
        self.assertIn("chain head 5", out)

    def test_live_node_with_configured_missing_signer_is_unavailable(self):
        os.environ["HELM_CELL_BIN"] = os.path.join(self.tmp, "deleted-signer")
        with mock.patch.object(cell, "get_json", return_value=[
                {"chain_index": 5, "finality": "tentative"}]):
            rc, out, _ = self.run_cli(["status"])
        self.assertEqual(rc, 1)
        self.assertIn("signer UNAVAILABLE", out)
        self.assertIn("path does not exist", out)
        self.assertNotIn("transport OFF", out)


if __name__ == "__main__":
    unittest.main()
