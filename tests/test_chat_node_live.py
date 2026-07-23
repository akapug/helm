#!/usr/bin/env python3
"""chat v2 LIVE — a REAL dregg node on a tmp data-dir + the REAL client
binary: signed round-trip, receipt-on-chain verification, the fallback flip,
the provisioning recovery lap and the reflex loop. Skips cleanly (CI-safe)
when either binary is absent. Hermetic: HOME itself points at a tmp dir for
the whole class (profiles land there, never in the real ~/.dregg), the node's
data-dir is a tmp dir, the port is ephemeral."""
import contextlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import cell, chat, chatnode, home, pk, reflex  # noqa: E402

NODE_BIN = chatnode.bin_path()
MELD_BIN = cell.bin_path()

ENV_KEYS = ("HOME", "HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NODE_URL", "MELD_CHAT_NODE_URL", "HELM_CHAT_NAME",
            "MELD_CHAT_NAME", "HELM_CHAT_LOG", "MELD_CHAT_LOG",
            "HELM_CELL_PROFILE", "MELD_AGENT_PROFILE", "HELM_CELL_BIN",
            "MELD_CELL_BIN", "HELM_NODE_URL", "MELD_NODE_URL",
            "HELM_NODE_TOKEN", "MELD_NODE_TOKEN")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(NODE_BIN and MELD_BIN and os.path.exists(MELD_BIN),
                     "dregg-cave-node + meld binaries required for live tests")
class LiveNodeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="helm-test-livenode-")
        cls.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HOME"] = os.path.join(cls.tmp, "home")   # sandbox ~/.dregg
        os.makedirs(os.environ["HOME"])
        os.environ["HELM_HOME"] = os.path.join(cls.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(cls.tmp, "chat")
        os.environ["HELM_CELL_BIN"] = MELD_BIN
        os.environ["HELM_CELL_PROFILE"] = "helm-chat-test"
        cls.port = _free_port()
        cls.url = "http://127.0.0.1:%d" % cls.port
        os.environ["HELM_CHAT_NODE_URL"] = cls.url
        # cwd hermeticity: the CLI default room derives from a git cwd
        # (seats.resolve_homing) — run from tmp so defaults stay 'main'
        cls.cwd_prior = os.getcwd()
        os.chdir(cls.tmp)
        os.makedirs(os.path.join(cls.tmp, "data"))  # the node wants it present
        cls.log = open(os.path.join(cls.tmp, "node.log"), "w")
        cls.node = subprocess.Popen(
            [NODE_BIN, "run", "--data-dir", os.path.join(cls.tmp, "data"),
             "--port", str(cls.port), "--gossip-port", str(_free_port()),
             "--enable-faucet"],
            stdout=cls.log, stderr=cls.log,
            env=dict(os.environ, RUST_LOG="warn"))
        if not chatnode.wait_api(cls.url, seconds=45):
            cls._teardown_node()
            raise unittest.SkipTest("node binary present but never served its API")
        st, err = chatnode.provision(cls.url)
        if err:
            cls._teardown_node()
            raise AssertionError("provisioning the live node failed: " + err)

    @classmethod
    def _teardown_node(cls):
        cls.node.terminate()
        try:
            cls.node.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.node.kill()
            cls.node.wait()
        cls.log.close()

    @classmethod
    def tearDownClass(cls):
        cls._teardown_node()
        os.chdir(cls.cwd_prior)
        for k, v in cls.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(cls.tmp, ignore_errors=True)

    _seq = 0

    def setUp(self):
        shutil.rmtree(os.environ["HELM_CHAT_DIR"], ignore_errors=True)
        # a FRESH cell per test: the faucet is 1/min/cell and a signed send
        # costs ~1442 computrons — one shared cell would run dry mid-suite
        # (the exhaustion lesson, measured live)
        LiveNodeTest._seq += 1
        os.environ["HELM_CELL_PROFILE"] = "helm-chat-test-%d" % self._seq

    # -- the signed round-trip ------------------------------------------------

    def test_01_signed_roundtrip_receipt_on_chain(self):
        m = chat.post("live signed :rocket:", who="a1")
        self.assertEqual(m["text"], "live signed 🚀")
        self.assertIsInstance(m.get("chain"), int)
        self.assertEqual(len(m["turn"]), 64)
        receipts = cell.get_json(self.url + "/api/receipts")
        by_index = {r["chain_index"]: r for r in receipts}
        self.assertEqual(by_index[m["chain"]]["turn_hash"], m["turn"])
        self.assertTrue(by_index[m["chain"]]["executor_signed"])
        self.assertNotIn("[unsigned]", chat._fmt(m))
        # and the RAM room renders it exactly like v1
        rows, total = chat.read()
        self.assertEqual((total, rows[0]["text"]), (1, "live signed 🚀"))

    def test_02_reaction_rides_the_same_transport(self):
        chat.post("target", who="a1")
        row, err = chat.react(1, ":tada:", who="a2")
        self.assertIsNone(err)
        self.assertEqual(row["react"], "🎉")
        self.assertIsInstance(row.get("chain"), int)

    def test_03_fallback_flip_and_back(self):
        dead = "http://127.0.0.1:%d" % _free_port()
        os.environ["HELM_CHAT_NODE_URL"] = dead
        try:
            m = chat.post("while down", who="a1")
        finally:
            os.environ["HELM_CHAT_NODE_URL"] = self.url
        self.assertNotIn("chain", m)                    # dropped the signature
        self.assertIn("[unsigned]", chat._fmt(m))       # visibly
        self.assertEqual(chat.read()[1], 1)             # never the message
        m2 = chat.post("back up", who="a1")             # node back -> signed again
        self.assertIsInstance(m2.get("chain"), int)

    def test_04_stale_token_recovery_lap(self):
        chat.post("prime", who="a1")                    # cache cells + token
        chat._ensure_dir()
        pk.atomic_write(chat._token_path(), "deadbeef" * 8)  # stale token
        m = chat.post("post-reboot", who="a1")
        self.assertIsInstance(m.get("chain"), int)      # revived, not dropped

    def test_05_reflex_loop_through_the_signed_room(self):
        home.scaffold_global()
        self.assertEqual(reflex.fire("any turn"), [])
        chat.post("fleet, morning", who="david")
        chat.mark_owner_unread()
        self.assertEqual([e["id"] for e in reflex.fire("any turn")],
                         ["owner-chat-unread"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(chat.cmd_chat(["read"]), 0)
        self.assertIn("david: fleet, morning", out.getvalue())
        self.assertEqual(reflex.fire("any turn"), [])   # consumed past the post

    def test_06_doctor_sees_the_node_and_balances(self):
        from helm import doctor
        chat.post("for the balance cache", who="a1")
        results = doctor.check_chat_node()
        self.assertTrue(any("LIVE" in msg for _lvl, msg in results))
        self.assertTrue(any("helm-chat-test" in msg for _lvl, msg in results))

    def test_07_transport_status_signed(self):
        st = chat.transport_status()
        self.assertEqual(st["mode"], "signed")
        self.assertEqual(st["url"], self.url)
        self.assertIsInstance(st["head"], int)


if __name__ == "__main__":
    unittest.main()
