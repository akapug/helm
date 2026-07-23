#!/usr/bin/env python3
"""helm.web ledger surface — hermetic contract tests. /api/ledger and
/api/ledger/turn are READ-ONLY projections of the attestation node's public
reads (a stdlib mock node stands in via HELM_NODE_URL); the node being down is
a NORMAL state that answers {"offline": true} at 200, never a 500. There is no
ledger POST route — the tab's one write rides the existing /api/chat bearer."""
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import chat, web  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_NODE_URL", "MELD_NODE_URL",
            "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NODE_URL", "MELD_CHAT_NODE_URL",
            "HELM_CELL_BIN", "MELD_CELL_BIN",
            "HELM_ADOPTED_DIR", "MELD_ADOPTED_DIR")

HEAD_HASH = "a" * 64
ATTEST_TURN = "b" * 64        # chain #11's turn — the premise-anchor fixture
UNJOINED_TURN = "9" * 64      # chain #10 — no local pointer names it
ACTING_CELL = "c1" * 32
IDLE_CELL = "d2" * 32

NODE_STATUS = {"healthy": True, "peer_count": 0, "latest_height": 343,
               "dag_height": 1924, "block_count": 2347, "consensus_live": True,
               "federation_mode": "solo", "public_key": "5d44" * 16,
               "state_producer": "lean", "lean_producer": True,
               "secret_seed": "MUST-NOT-PROJECT"}

# Served deliberately out of order — the endpoint must sort newest-first.
NODE_RECEIPTS = [
    {"chain_index": 11, "chain_head": False, "turn_hash": "b" * 64,
     "receipt_hash": "e" * 64, "agent": ACTING_CELL, "timestamp": 1784582956,
     "finality": "tentative", "consensus_final": True, "attested_height": 342,
     "executor_signed": True, "has_proof": False, "action_count": 1,
     "computrons_used": 50},
    {"chain_index": 12, "chain_head": True, "turn_hash": HEAD_HASH,
     "receipt_hash": "f" * 64, "agent": ACTING_CELL, "timestamp": 1784582964,
     "finality": "tentative", "consensus_final": True, "attested_height": 343,
     "executor_signed": True, "has_proof": False, "action_count": 1,
     "computrons_used": 50},
    {"chain_index": 10, "chain_head": False, "turn_hash": "9" * 64,
     "receipt_hash": "8" * 64, "agent": ACTING_CELL, "timestamp": 1784582365,
     "finality": "tentative", "consensus_final": False,
     "executor_signed": True, "has_proof": False, "action_count": 1,
     "computrons_used": 50},
]

NODE_CELLS = [
    {"id": IDLE_CELL, "balance": 7, "nonce": 0, "capability_count": 0,
     "has_program": False, "found": True},
    {"id": ACTING_CELL, "balance": 2944, "nonce": 2, "capability_count": 0,
     "has_program": False, "found": True},
]


class MockNode(BaseHTTPRequestHandler):
    def do_GET(self):
        body = None
        if self.path == "/status":
            body = NODE_STATUS
        elif self.path == "/api/receipts":
            body = NODE_RECEIPTS
        elif self.path == "/api/cells":
            body = NODE_CELLS
        elif self.path == "/api/turn/%s/status" % HEAD_HASH:
            body = {"turn_hash": HEAD_HASH, "consensus_final": True,
                    "attested_height": 343, "receipt_present": True}
        if body is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


class LedgerBase(unittest.TestCase):
    """Shared scaffold: tmp HELM_HOME, an ephemeral web server, req()."""
    node_url = None  # set by subclasses before the web server starts

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="helm-test-webledger-")
        cls.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = cls.tmp
        os.environ["HELM_NODE_URL"] = cls.node_url
        # hermetic locality for the transport + about joins: the RAM room and
        # the adopted store must never read this machine's real state, and the
        # SET-BUT-EMPTY chat-node url is the documented signed-transport kill
        # switch — a class that wants "signed" opts in explicitly.
        os.environ["HELM_CHAT_DIR"] = os.path.join(cls.tmp, "chat-ram")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(cls.tmp, "adopted")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        cls.srv = web.make_server(0)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        failure_dir = chat.sign_failures_dir()
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.thread.join(timeout=5)
        for k, v in cls.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(failure_dir, ignore_errors=True)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def req(self, path, payload=None, token=True):
        """(status, obj) — 4xx/5xx returned, not raised."""
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        if data and token:
            headers["Authorization"] = "Bearer " + web.MUTATION_TOKEN
        r = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"null")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestWebLedger(LedgerBase):
    @classmethod
    def setUpClass(cls):
        cls.node = ThreadingHTTPServer(("127.0.0.1", 0), MockNode)
        cls.node.daemon_threads = True
        cls.node_url = "http://127.0.0.1:%d" % cls.node.server_address[1]
        cls.node_thread = threading.Thread(target=cls.node.serve_forever,
                                           daemon=True)
        cls.node_thread.start()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls.node.shutdown()
        cls.node.server_close()
        cls.node_thread.join(timeout=5)

    def test_aggregate_shape_and_order(self):
        status, d = self.req("/api/ledger")
        self.assertEqual(status, 200)
        self.assertNotIn("offline", d)
        self.assertEqual(d["node"], self.node_url)
        # newest-first by chain_index, head on top
        self.assertEqual([t["chain_index"] for t in d["turns"]], [12, 11, 10])
        self.assertTrue(d["turns"][0]["chain_head"])
        self.assertEqual(d["turns"][0]["attested_height"], 343)

    def test_status_is_the_declared_subset(self):
        _, d = self.req("/api/ledger")
        self.assertEqual(set(d["status"]), set(web.LEDGER_STATUS_KEYS))
        self.assertNotIn("secret_seed", d["status"])  # projection, not passthrough
        self.assertEqual(d["status"]["latest_height"], 343)

    def test_cells_carry_the_activity_join(self):
        _, d = self.req("/api/ledger")
        by_id = {c["id"]: c for c in d["cells"]}
        acting, idle = by_id[ACTING_CELL], by_id[IDLE_CELL]
        self.assertEqual(acting["last_turn_ts"], 1784582964)  # newest of its 3
        self.assertEqual(acting["recent_turns"], 3)
        self.assertIsNone(idle["last_turn_ts"])
        self.assertEqual(idle["recent_turns"], 0)
        # active seats sort before never-acted ones
        self.assertEqual(d["cells"][0]["id"], ACTING_CELL)

    def test_turn_status_proxy(self):
        status, d = self.req("/api/ledger/turn?hash=" + HEAD_HASH)
        self.assertEqual(status, 200)
        self.assertEqual((d["consensus_final"], d["attested_height"]),
                         (True, 343))

    def test_turn_status_hash_gate(self):
        for q in ("", "?hash=", "?hash=xyz", "?hash=" + "a" * 63,
                  "?hash=" + "g" * 64, "?hash=../status"):
            status, d = self.req("/api/ledger/turn" + q)
            self.assertEqual(status, 400, q)
            self.assertIn("error", d)

    def test_turn_status_unknown_hash_degrades(self):
        # the mock node 404s an unknown hash — the proxy degrades, never 500s
        status, d = self.req("/api/ledger/turn?hash=" + "0" * 64)
        self.assertEqual(status, 200)
        self.assertTrue(d["unavailable"])

    def test_ledger_is_read_only(self):
        # no POST route exists — even a bearer-carrying POST is 404
        for path in ("/api/ledger", "/api/ledger/turn"):
            status, d = self.req(path, payload={"x": 1})
            self.assertEqual(status, 404, path)

    def test_transport_truth_without_signer(self):
        # no HELM_CELL_BIN + chat node disabled -> the honest "unsigned";
        # nothing is ever labeled as a known post/anchor without local proof
        _, d = self.req("/api/ledger")
        self.assertEqual(d["transport"]["mode"], "unsigned")
        self.assertFalse(d["transport"]["signer"])
        for t in d["turns"]:
            self.assertNotIn("about", t)


class TestWebLedgerSigned(LedgerBase):
    """dregg-primary signing LIVE: an explicit signer binary + a room node
    that answers -> transport.mode is "signed", and every turn a LOCAL pointer
    names carries its about label — the owner's chat post ({turn} on the RAM
    row, topic helm.chat) and the premise anchor (attest_anchor_turn on the
    store entry, topic helm.attest). A turn nothing local names stays
    unlabeled — helm never invents an identity for it."""

    @classmethod
    def setUpClass(cls):
        cls.node = ThreadingHTTPServer(("127.0.0.1", 0), MockNode)
        cls.node.daemon_threads = True
        cls.node_url = "http://127.0.0.1:%d" % cls.node.server_address[1]
        cls.node_thread = threading.Thread(target=cls.node.serve_forever,
                                           daemon=True)
        cls.node_thread.start()
        super().setUpClass()
        # the explicit signer: bin_ready's bar is a real executable FILE
        fake = os.path.join(cls.tmp, "fake-cell-bin")
        with open(fake, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(fake, 0o755)
        os.environ["HELM_CELL_BIN"] = fake
        os.environ["HELM_CHAT_NODE_URL"] = cls.node_url  # room node answers
        # the owner's web post whose signed leg landed as the head turn
        chat_d = os.environ["HELM_CHAT_DIR"]
        os.makedirs(chat_d, exist_ok=True)
        with open(os.path.join(chat_d, "main.jsonl"), "w") as f:
            f.write(json.dumps({"ts": "2026-07-21T09:00:00", "from": "david",
                                "text": "hello fleet", "origin": "web",
                                "turn": HEAD_HASH, "receipt": "f" * 64,
                                "chain": 12, "id": "aabbccddeeff"}) + "\n")
        # a premise whose optional dregg anchor landed as turn #11
        from helm import store
        store.write_prior({"id": "anchored-prem",
                           "statement": "signed-ledger fixture premise",
                           "confidence": 1.0,
                           "stated_ts": "2026-07-21T09:00:00",
                           "attest_anchor_turn": ATTEST_TURN})

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls.node.shutdown()
        cls.node.server_close()
        cls.node_thread.join(timeout=5)

    def _turns_by_hash(self):
        status, d = self.req("/api/ledger")
        self.assertEqual(status, 200)
        return d, {t["turn_hash"]: t for t in d["turns"]}

    def test_transport_reports_signed(self):
        d, _ = self._turns_by_hash()
        self.assertEqual(d["transport"]["mode"], "signed")
        self.assertTrue(d["transport"]["signer"])

    def test_chat_turn_carries_its_about_label(self):
        _, by_hash = self._turns_by_hash()
        ab = by_hash[HEAD_HASH]["about"]
        self.assertEqual((ab["kind"], ab["from"], ab["room"], ab["text"]),
                         ("chat", "david", "main", "hello fleet"))

    def test_attest_turn_carries_its_about_label(self):
        _, by_hash = self._turns_by_hash()
        ab = by_hash[ATTEST_TURN]["about"]
        self.assertEqual((ab["kind"], ab["id"]), ("attest", "anchored-prem"))

    def test_unjoinable_turn_stays_unlabeled(self):
        _, by_hash = self._turns_by_hash()
        self.assertNotIn("about", by_hash[UNJOINED_TURN])


class TestWebLedgerOffline(LedgerBase):
    @classmethod
    def setUpClass(cls):
        cls.node_url = "http://127.0.0.1:%d" % _free_port()  # nothing listens
        super().setUpClass()

    def test_ledger_fails_open(self):
        status, d = self.req("/api/ledger")
        self.assertEqual(status, 200)
        self.assertTrue(d["offline"])
        self.assertEqual(d["node"], self.node_url)
        self.assertEqual(d["transport"]["mode"], "unsigned")

    def test_offline_ledger_keeps_persistent_chat_degradation(self):
        chat._record_sign_failure(
            "retired-seat", chat._diag("send_failed", "persistent failure"))
        try:
            status, d = self.req("/api/ledger")
            self.assertEqual(status, 200)
            self.assertTrue(d["offline"])
            self.assertEqual((d["transport"]["mode"],
                              d["transport"]["profile"],
                              d["transport"]["reason"]),
                             ("degraded", "retired-seat", "persistent failure"))
        finally:
            chat.acknowledge_sign_failures("retired-seat")

    def test_offline_ledger_launders_transport_profile_and_reason(self):
        raw = "ledger\x1b[31m\x00\x85‮"
        reason = "node\x1b[2J\x01\x85‮ down"
        chat._record_sign_failure(raw, chat._diag("send_failed", reason))
        try:
            status, d = self.req("/api/ledger")
            self.assertEqual(status, 200)
            self.assertTrue(d["offline"])
            self.assertEqual(d["transport"]["profile"], chat._dsan(raw))
            body = json.dumps(d, ensure_ascii=False)
            for ch in ("\x1b", "\x00", "\x01", "\x85", "‮"):
                self.assertNotIn(ch, body)
        finally:
            chat.acknowledge_sign_failures(raw)

    def test_turn_status_fails_open(self):
        status, d = self.req("/api/ledger/turn?hash=" + "a" * 64)
        self.assertEqual(status, 200)
        self.assertTrue(d["unavailable"])
        self.assertEqual(d["node"], self.node_url)


if __name__ == "__main__":
    unittest.main()
