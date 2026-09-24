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
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import chat, pk, store, web, web_ledger, web_ui_loader  # noqa: E402
from tests.test_web_chat_client_runtime import _extract_fn  # noqa: E402

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


def _reply(h, body, code=200):
    """An EMPTY 404 for None (what axum serves for a route it does not have),
    else the JSON at `code` — shared by every mock-node handler."""
    if body is None:
        h.send_response(404)
        h.send_header("Content-Length", "0")
        h.end_headers()
        return
    raw = json.dumps(body).encode()
    h.send_response(code)
    h.send_header("Content-Type", "application/json")
    h.send_header("Content-Length", str(len(raw)))
    h.end_headers()
    h.wfile.write(raw)


# The node's OWN verdict shapes, copied from `get_turn_verdict` in
# node/src/api.rs at the rebased dregg tip (the route does not exist on the
# lane/fee-loop-plus-coordination build). `height` is the commit-log height
# and deliberately differs from the head receipt's chain_index (12) and
# attested_height (343), so an arm can tell a proxied height from one the
# proxy made up out of the ledger list.
VERDICT_HEIGHT = 812
VERDICT_ACCEPTED = {
    "turn_hash": HEAD_HASH, "verdict": "accepted", "terminal": True,
    "height": VERDICT_HEIGHT, "block_id": "3c" * 32, "receipt_hash": "f" * 64,
    "attested": {"merkle_root": "7d" * 32, "quorum": 1, "threshold": 1,
                 "structurally_complete": True}}


def _verdict_unknown(h):
    return {"turn_hash": h, "verdict": "unknown", "terminal": False,
            "detail": "this node holds no record of this turn hash"}


class MockNode(BaseHTTPRequestHandler):
    def do_GET(self):
        body, code = None, 200
        if self.path == "/status":
            body = NODE_STATUS
        elif self.path == "/api/receipts":
            body = NODE_RECEIPTS
        elif self.path == "/api/cells":
            body = NODE_CELLS
        elif self.path == "/api/turn/%s/verdict" % HEAD_HASH:
            body = VERDICT_ACCEPTED
        elif self.path.startswith("/api/turn/") \
                and self.path.endswith("/verdict"):
            body, code = _verdict_unknown(self.path.split("/")[3]), 404
        _reply(self, body, code)

    def log_message(self, *a):
        pass


WRONG_NODE_CELL = "ab" * 32


class EmptyAnchorNode(BaseHTTPRequestHandler):
    """The 2026-07-29 incident shape, generalized: a healthy node with real
    cells (nonces from earlier activity) whose receipt list is EMPTY, because
    signed turns land on a DIFFERENT node (the chat room node). Synthetic
    values; the incident is real — the banner read one node, the panel read
    the other, and 34 live turns rendered as an empty feed."""

    def do_GET(self):
        body = None
        if self.path == "/status":
            body = NODE_STATUS
        elif self.path == "/api/receipts":
            body = []
        elif self.path == "/api/cells":
            body = [{"id": WRONG_NODE_CELL, "balance": 30000, "nonce": 10,
                     "capability_count": 0, "has_program": False,
                     "found": True}]
        _reply(self, body)

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
        # shutdown() waits one serve_forever poll (stdlib 0.5s)
        cls.thread = threading.Thread(target=cls.srv.serve_forever,
                                      kwargs={"poll_interval": 0.01}, daemon=True)
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

    def setUp(self):
        # THE NAME-JOIN IS CACHED PROCESS-WIDE UNDER A CONSTANT KEY, and every
        # class here seeds its own HELM_HOME and its own rooms. Without this,
        # a class asserting a turn carries NO label would read the previous
        # class's labels — a stale body crossing a fixture boundary.
        web._qstate.pop("ledger-about", None)
        self.addCleanup(web._qstate.pop, "ledger-about", None)

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
                                           kwargs={"poll_interval": 0.01},
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

    def test_cells_carry_seat_names_from_chats_join_cache(self):
        # chat's RAM-side cache ({profile: cell_hex}) names the signing
        # account so the owner reads "codex", not a hex id; a cell the cache
        # does not know stays honestly unnamed — helm never invents a name.
        pk.write_json(chat.cells_path(), {"codex": ACTING_CELL})
        try:
            _, d = self.req("/api/ledger")
            by_id = {c["id"]: c for c in d["cells"]}
            self.assertEqual(by_id[ACTING_CELL]["seat"], "codex")
            self.assertNotIn("seat", by_id[IDLE_CELL])
        finally:
            os.unlink(chat.cells_path())

    def test_turn_status_proxy(self):
        """The node's VERDICT, verbatim. The route this proxied before
        (/api/turn/<h>/status) never existed on any dregg build, so every
        answer it ever gave was `unavailable`."""
        status, d = self.req("/api/ledger/turn?hash=" + HEAD_HASH)
        self.assertEqual(status, 200)
        self.assertNotIn("unavailable", d)
        self.assertEqual((d["verdict"], d["terminal"], d["source"]),
                         ("accepted", True, "verdict"))
        # the COMMIT-LOG height, passed through — never the receipt's
        # chain_index (12) or attested_height (343) from the list beside it
        self.assertEqual(d["height"], VERDICT_HEIGHT)
        self.assertEqual(d["attested"]["quorum"], 1)

    def test_turn_status_hash_gate(self):
        for q in ("", "?hash=", "?hash=xyz", "?hash=" + "a" * 63,
                  "?hash=" + "g" * 64, "?hash=../status"):
            status, d = self.req("/api/ledger/turn" + q)
            self.assertEqual(status, 400, q)
            self.assertIn("error", d)

    def test_turn_status_unknown_hash_is_the_nodes_unknown(self):
        """The node answers an unknown hash 404 WITH a body. That body is an
        answer ("ask another node, or ask again"), not a missing route, so it
        must reach the reader as `unknown` rather than fold into
        `unavailable` — never a 500 either way."""
        status, d = self.req("/api/ledger/turn?hash=" + "0" * 64)
        self.assertEqual(status, 200)
        self.assertNotIn("unavailable", d)
        self.assertEqual((d["verdict"], d["terminal"], d["source"]),
                         ("unknown", False, "verdict"))

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
                                           kwargs={"poll_interval": 0.01},
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
            f.write(json.dumps({"ts": "2026-07-21T09:00:00", "from": "daria",
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

    def test_transport_reports_ready_until_this_profile_commits(self):
        d, _ = self._turns_by_hash()
        self.assertEqual((d["transport"]["mode"], d["transport"]["label"]),
                         ("ready", "ready (unproven)"))
        self.assertTrue(d["transport"]["signer"])

    def test_chat_turn_carries_its_about_label(self):
        _, by_hash = self._turns_by_hash()
        ab = by_hash[HEAD_HASH]["about"]
        self.assertEqual((ab["kind"], ab["from"], ab["room"], ab["text"]),
                         ("chat", "daria", "main", "hello fleet"))

    def test_attest_turn_carries_its_about_label(self):
        _, by_hash = self._turns_by_hash()
        ab = by_hash[ATTEST_TURN]["about"]
        self.assertEqual((ab["kind"], ab["id"]), ("attest", "anchored-prem"))

    def test_unjoinable_turn_stays_unlabeled(self):
        _, by_hash = self._turns_by_hash()
        self.assertNotIn("about", by_hash[UNJOINED_TURN])

    def test_the_name_join_runs_ONCE_for_many_requests_in_one_window(self):
        """task/2703: `_turn_about` walked every entry file AND every room on
        EVERY request — 181 ms on the live corpus (459 rooms, 1,305 entry
        files), against a console that polls /api/ledger every 3000 ms from
        every open socket. The counter is of `_turn_about` itself, the whole
        dominating walk, not of the endpoint."""
        calls = []
        real = web_ledger._turn_about
        with mock.patch.object(web_ledger, "_turn_about",
                               lambda *a: (calls.append(1), real(*a))[1]):
            # UNCONDITIONAL POSITIVE CONTROL: prove the counter moves before
            # asking the subject to hold it still. A patch that failed to take
            # would let every assertion below pass by counting nothing.
            web_ledger._turn_about({HEAD_HASH})
            self.assertEqual(len(calls), 1, "the _turn_about counter never moved")
            calls.clear()

            for _ in range(6):
                _, by_hash = self._turns_by_hash()
                self.assertEqual(by_hash[HEAD_HASH]["about"]["from"], "daria")
            self.assertEqual(len(calls), 1,
                             "six requests inside one TTL window ran the join "
                             "%d times; the cache is not holding" % len(calls))

    def test_the_window_ENDS_and_a_new_label_reaches_the_join(self):
        d, by_hash = self._turns_by_hash()
        # UNCONDITIONAL POSITIVE CONTROL on the SAME observable: `about` is
        # absent below, and an absence proves nothing if the join is dark or
        # the key is misspelled. A sibling turn in the same payload carries
        # one, so "absent" here means unjoined rather than unread.
        self.assertEqual(by_hash[HEAD_HASH]["about"]["from"], "daria")
        self.assertNotIn("about", by_hash[UNJOINED_TURN])
        self.assertLess(d["about_age_s"], 5)

        # the room is CLASS fixture state; a sibling test asserts this very
        # turn stays unlabeled, so the append is undone before it runs
        room = os.path.join(os.environ["HELM_CHAT_DIR"], "main.jsonl")
        with open(room, encoding="utf-8") as f:
            prior = f.read()
        self.addCleanup(lambda: open(room, "w", encoding="utf-8").write(prior))
        with open(room, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": "2026-07-21T09:05:00", "from": "daria",
                                "text": "named later", "origin": "web",
                                "turn": UNJOINED_TURN, "receipt": "e" * 64,
                                "chain": 13, "id": "bbccddeeff00"}) + "\n")

        # inside the window the new label is invisible — the cache working, and
        # the reason about_age_s has to be on the wire beside it
        held_d, held = self._turns_by_hash()
        # the same positive control over the SECOND payload: the join has to be
        # alive in it for its silence about this turn to mean anything
        self.assertEqual(held[HEAD_HASH]["about"]["from"], "daria")
        self.assertNotIn("about", held[UNJOINED_TURN])

        ts, body = web._qstate["ledger-about"]
        web._qstate["ledger-about"] = (
            ts - web_ledger._LEDGER_WALK_TTL_S - 1, body)
        after_d, after = self._turns_by_hash()
        self.assertEqual(after[UNJOINED_TURN]["about"]["text"], "named later",
                         "the TTL expired and the join still held the old walk")
        # the age is a reading about the JOIN, and it moves with the join
        self.assertGreater(held_d["about_age_s"], -1)
        self.assertLess(after_d["about_age_s"], 5)

    def test_attested_store_projection_skips_impossible_file_types(self):  # noqa: VACUOUS_ASSERTION — seen>0 proves the projected parser ran before the negative filename assertions
        os.makedirs(os.environ["HELM_ADOPTED_DIR"], exist_ok=True)
        noise = os.path.join(os.environ["HELM_ADOPTED_DIR"], "lex-noise.md")
        with open(noise, "w", encoding="utf-8") as f:
            f.write("---\nterm: noise\ndefinition: not an attested prior\n---\n")
        load = sys.modules["helm.store.load"]
        real_parse = pk.parse_simple_frontmatter
        seen = []

        def parse(path, *args, **kwargs):
            seen.append(os.path.basename(path))
            return real_parse(path, *args, **kwargs)

        try:
            with mock.patch.object(load.pk, "parse_simple_frontmatter",
                                   side_effect=parse):
                rows = store._attested_priors()
            self.assertGreater(len(seen), 0)  # the anchored prior was parsed
            self.assertTrue(all(n.startswith((store.PRIOR_PREFIX,
                                              store.LEGACY_PREFIX))
                                for n in seen), seen)
            self.assertNotIn("lex-noise.md", seen)
            self.assertIn("anchored-prem", {e["id"] for e in rows})
        finally:
            os.unlink(noise)

    def test_unattested_narrow_prior_still_shadows_attested_wide_prior(self):  # noqa: VACUOUS_ASSERTION — the existing anchored-prem hit proves the projection is non-empty before shadow-turn is asserted absent
        wide = store.write_prior(
            {"id": "shadow-turn", "statement": "wide", "confidence": 1.0,
             "attest_anchor_turn": HEAD_HASH},
            root_dir=os.environ["HELM_ADOPTED_DIR"])
        narrow = None
        try:
            self.assertIn("shadow-turn", {e["id"]
                                          for e in store._attested_priors()})
            narrow = store.write_prior(
                {"id": "shadow-turn", "statement": "narrow", "confidence": 1.0})
            self.assertNotIn("shadow-turn", {e["id"]
                                             for e in store._attested_priors()})
        finally:
            os.unlink(wide)
            if narrow:
                os.unlink(narrow)

    def test_bounded_join_skips_rows_that_cannot_carry_a_turn(self):  # noqa: VACUOUS_ASSERTION — parse.call_count==2 and the escaped row's exact payload prove the bounded parser hit both candidate arms
        """The cold-path turn join parses only candidate lines, while retaining
        JSON's escaped-key semantics. The two MUST-HIT lines prove the parser ran;
        1,000 unrelated rows prove it did not silently return to all-room JSON
        parsing. The narrow store reader is likewise mandatory — restoring
        load_all makes the hostile control raise."""
        os.makedirs(chat.chat_dir(), exist_ok=True)
        path = chat.room_path("side")
        with open(path, "w", encoding="utf-8") as f:
            for i in range(1000):
                f.write(json.dumps({"from": "noise", "text": "row %d" % i}) + "\n")
            # A JSON escape in the KEY still decodes to "turn". A byte filter
            # that recognizes only the literal spelling would lose this row.
            f.write('{"\\u0074urn":"%s","from":"z","text":"escaped"}\n'
                    % HEAD_HASH)
        try:
            real_msg = chat._msg
            with mock.patch.object(store, "load_all",
                                   side_effect=AssertionError("broad store read")), \
                    mock.patch.object(chat, "_msg", wraps=real_msg) as parse:
                about = web._turn_about({HEAD_HASH})
            # main's literal-key row + side's escaped-key row, never the 1,000
            # unrelated rows. side sorts after main and therefore wins exactly
            # as the full room-order projection does.
            self.assertEqual(parse.call_count, 2)
            self.assertEqual((about[HEAD_HASH]["room"], about[HEAD_HASH]["text"]),
                             ("side", "escaped"))
        finally:
            os.unlink(path)

    def test_bounded_join_matches_the_full_projection_for_the_receipt_window(self):
        full = web._turn_about()
        self.assertIn(HEAD_HASH, full)
        self.assertIn(ATTEST_TURN, full)
        wanted = {HEAD_HASH, ATTEST_TURN, UNJOINED_TURN}
        bounded = web._turn_about(wanted)
        self.assertEqual(bounded, {h: full[h] for h in wanted if h in full})

    def test_empty_receipt_window_reads_no_local_projection(self):
        self.assertIn(HEAD_HASH, web._turn_about({HEAD_HASH}))
        store_reads = []
        chat_reads = []

        def read_store():
            store_reads.append(True)
            return []

        def read_rooms():
            chat_reads.append(True)
            return []

        with mock.patch.object(store, "_attested_priors",
                               side_effect=read_store), \
                mock.patch.object(chat, "list_rooms", side_effect=read_rooms):
            self.assertEqual(web._turn_about({HEAD_HASH}), {})
            self.assertEqual((store_reads, chat_reads), ([True], [True]))
            store_reads.clear()
            chat_reads.clear()
            self.assertEqual(web._turn_about(set()), {})
            self.assertEqual((store_reads, chat_reads), ([], []))

    def test_bounded_join_keeps_each_leg_fail_open(self):
        self.assertIn(HEAD_HASH, web._turn_about({HEAD_HASH}))
        with mock.patch.object(store, "_attested_priors",
                               side_effect=OSError("store unreadable")), \
                mock.patch.object(chat, "list_rooms", return_value=["main"]), \
                mock.patch("builtins.open", side_effect=OSError("room unreadable")):
            self.assertEqual(web._turn_about({HEAD_HASH}), {})


class TestWebLedgerSplitBrain(LedgerBase):
    """Two REAL nodes exist in production: the chat room node (where signed
    turns land since dregg-primary) and the anchor node (cell.node_url()'s
    default). On 2026-07-29 the ledger tab read the anchor node while the
    banner's transport truth read the chat node — "signing live" over an
    empty feed. The ledger endpoints must read the node where turns LAND."""

    @classmethod
    def setUpClass(cls):
        cls.anchor = ThreadingHTTPServer(("127.0.0.1", 0), EmptyAnchorNode)
        cls.anchor.daemon_threads = True
        cls.anchor_thread = threading.Thread(target=cls.anchor.serve_forever,
                                             kwargs={"poll_interval": 0.01},
                                             daemon=True)
        cls.anchor_thread.start()
        cls.chatnode = ThreadingHTTPServer(("127.0.0.1", 0), MockNode)
        cls.chatnode.daemon_threads = True
        cls.chat_url = "http://127.0.0.1:%d" % cls.chatnode.server_address[1]
        cls.chat_thread = threading.Thread(target=cls.chatnode.serve_forever,
                                           kwargs={"poll_interval": 0.01},
                                           daemon=True)
        cls.chat_thread.start()
        cls.node_url = "http://127.0.0.1:%d" % cls.anchor.server_address[1]
        super().setUpClass()                      # HELM_NODE_URL -> anchor
        os.environ["HELM_CHAT_NODE_URL"] = cls.chat_url  # where turns land

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        for srv, th in ((cls.anchor, cls.anchor_thread),
                        (cls.chatnode, cls.chat_thread)):
            srv.shutdown()
            srv.server_close()
            th.join(timeout=5)

    def test_ledger_reads_the_node_where_turns_land(self):
        status, d = self.req("/api/ledger")
        self.assertEqual(status, 200)
        self.assertEqual(d["node"], self.chat_url)
        self.assertEqual([t["chain_index"] for t in d["turns"]], [12, 11, 10])
        # and the cells are the signing node's — not the anchor node's
        ids = {c["id"] for c in d["cells"]}
        self.assertIn(ACTING_CELL, ids)
        self.assertNotIn(WRONG_NODE_CELL, ids)

    def test_turn_status_proxies_the_same_node(self):
        # only the chat node holds this turn's certificate; the anchor node
        # 404s it — a proxy still reading the anchor node answers unavailable
        status, d = self.req("/api/ledger/turn?hash=" + HEAD_HASH)
        self.assertEqual(status, 200)
        self.assertEqual((d.get("verdict"), d.get("node")),
                         ("accepted", self.chat_url))


class PreVerdictNode(BaseHTTPRequestHandler):
    """A build older than the verdict route (dregg's
    lane/fee-loop-plus-coordination): its router
    has /api/turn/{hash}/proof and NO /verdict or /anchor, and no fallback
    handler, so both come back as axum's empty 404."""

    def do_GET(self):
        _reply(self, {"/status": NODE_STATUS, "/api/receipts": NODE_RECEIPTS,
                      "/api/cells": NODE_CELLS}.get(self.path))

    def log_message(self, *a):
        pass


class AnchorOnlyNode(PreVerdictNode):
    """A build with /anchor and without /verdict. Its 404 carries
    `anchor_status: not_committed`, which says "not committed HERE" and is
    neither accepted, rejected nor pending."""

    def do_GET(self):
        if self.path == "/api/turn/%s/anchor" % HEAD_HASH:
            _reply(self, {"turn_hash": HEAD_HASH,
                          "anchor_status": "not_committed"}, 404)
            return
        super().do_GET()


class _OneNodeLedger(LedgerBase):
    handler = None

    @classmethod
    def setUpClass(cls):
        cls.node = ThreadingHTTPServer(("127.0.0.1", 0), cls.handler)
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


class TestTurnVerdictOnAPreVerdictNode(_OneNodeLedger):
    handler = PreVerdictNode

    def test_a_node_with_neither_route_says_so_and_infers_nothing(self):
        status, d = self.req("/api/ledger/turn?hash=" + HEAD_HASH)
        self.assertEqual(status, 200)
        self.assertTrue(d["unavailable"])
        self.assertEqual(d["node"], self.node_url)
        self.assertIn("/verdict", d["reason"])
        self.assertIn("/anchor", d["reason"])
        self.assertNotIn("verdict", d)


class TestTurnVerdictOnAnAnchorOnlyNode(_OneNodeLedger):
    handler = AnchorOnlyNode

    def test_falls_back_to_anchor_and_never_turns_it_into_a_verdict(self):
        status, d = self.req("/api/ledger/turn?hash=" + HEAD_HASH)
        self.assertEqual(status, 200)
        self.assertNotIn("unavailable", d)
        self.assertEqual((d["source"], d["anchor_status"]),
                         ("anchor", "not_committed"))
        self.assertNotIn("verdict", d)


class LedgerPlainLanguageTest(unittest.TestCase):
    """Static card vocabulary; behavioral empty states run under node in
    test_web_ledger_client_runtime.LedgerHeaderRuntimeTest, without a mirror."""

    def test_the_cards_lead_with_plain_language(self):
        """The owner's acceptance test (2026-07-29): "i dont really know what
        most of these things mean and no one else will either." Every box's
        first line is plain words; helm-internal vocabulary appears only as a
        parenthetical mapping, and the jargon-led labels must stay gone."""
        src = web_ui_loader.read_text()
        for plain in ("signed entries", "who signed", "local activity",
                      "shared record · signed",
                      "this machine only · not signed",
                      "the same entries, grouped by author"):
            self.assertIn(plain, src)
        for jargon_led in ("cave turn ledger", "live fleet activity",
                           "signed-turn cells", "no cave turns yet",
                           "the durable dregg attestation"):
            self.assertNotIn(jargon_led, src)


class LedgerSurfaceAgreementTest(unittest.TestCase):
    """THE SEAM, not the sides. cavePulse counts the entries and ledgerBadges
    labels each one; they read the SAME field and must never give one row two
    answers. Testing each function alone cannot catch a disagreement between
    them — both were internally consistent and green when they disagreed.

    Live 2026-08-05 (codex, review of ec7c2ec1): the pulse tested
    `finality === "final"` before certificate absence while the badge tested
    absence first, so {chain_index:2, finality:"final"} rendered
    "solo-committed" on its badge and "all 1 final" in the summary. Both
    functions run VERBATIM here, one row at a time, and the row's two renderings
    are compared — which is the only shape of test that could have failed."""

    NOW = 1784583000

    # each row, and the ONE state both surfaces must agree it is in
    ROWS = (
        ("absent_tentative", {"chain_index": 7, "finality": "tentative"}, "solo"),
        ("absent_final_word", {"chain_index": 2, "finality": "final"}, "solo"),
        ("absent_no_finality", {"chain_index": 3}, "solo"),
        ("cert_true", {"chain_index": 4, "consensus_final": True,
                       "attested_height": 342, "finality": "tentative"}, "final"),
        ("cert_false", {"chain_index": 5, "consensus_final": False,
                        "finality": "tentative"}, "wait"),
        ("cert_false_final_word", {"chain_index": 6, "consensus_final": False,
                                   "finality": "final"}, "final"),
    )

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        src = web_ui_loader.read_text()
        esc = [ln for ln in src.splitlines() if ln.startswith("const esc =")]
        assert esc, "esc() is no longer a one-line const in assembled web UI"
        cls.tmp = tempfile.mkdtemp(prefix="helm-agree-")
        cls.path = os.path.join(cls.tmp, "run.js")
        with open(cls.path, "w", encoding="utf-8") as f:
            f.write(esc[0] + "\n\n" + _extract_fn(src, "cavePulse")
                    + "\n\n" + _extract_fn(src, "ledgerBadges") + """

const cases = JSON.parse(require("fs").readFileSync(process.argv[2], "utf8"));
const out = {};
for (const k of Object.keys(cases)) {
  const t = cases[k].turn;
  out[k] = {badge: ledgerBadges(t),
            pulse: cavePulse([t], t.chain_index)};
}
process.stdout.write(JSON.stringify(out));
""")
        chk = subprocess.run([cls.node, "--check", cls.path],
                             capture_output=True, text=True)
        assert chk.returncode == 0, "node --check failed:\n" + chk.stderr

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    @staticmethod
    def _badge_state(html):
        if "solo-committed" in html:
            return "solo"
        if "final @ h" in html:
            return "final"
        if ">final<" in html:
            return "final"
        if "tentative" in html:
            return "wait"
        return "UNREADABLE(%s)" % html

    @staticmethod
    def _pulse_state(html):
        if "solo-committed" in html:
            return "solo"
        if "awaiting finality" in html:
            return "wait"
        if "final" in html:
            return "final"
        return "UNREADABLE(%s)" % html

    def test_one_row_never_gets_two_answers(self):  # noqa: VACUOUS_ASSERTION — two unconditional controls run BEFORE the loop, both on what node actually returned: len(got) == len(ROWS), and assertIn("solo-committed", got["absent_final_word"]["badge"]) proving the row this test exists for really rendered. An empty node run fails there, not silently in an unentered loop.
        cases = {name: {"turn": dict(t, timestamp=self.NOW - 60),
                        "now": self.NOW} for name, t, _ in self.ROWS}
        path = os.path.join(self.tmp, "cases.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cases, f)
        p = subprocess.run([self.node, self.path, path],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        got = json.loads(p.stdout)
        # UNCONDITIONAL POSITIVE CONTROL, before any loop: an emptied matrix
        # would make every per-row assertion below vacuous by never running, and
        # a node that produced nothing would look identical to full agreement.
        self.assertEqual(len(got), len(self.ROWS),
                         "node returned %d renderings for %d rows"
                         % (len(got), len(self.ROWS)))
        self.assertIn("solo-committed", got["absent_final_word"]["badge"],
                      "the row this test exists for did not even render")
        # the fixture must actually exercise every state, or an agreement that
        # only ever saw one branch would read as full coverage
        self.assertEqual({want for _, _, want in self.ROWS},
                         {"solo", "final", "wait"},
                         "the row matrix stopped covering all three states")
        for name, _turn, want in self.ROWS:
            badge = self._badge_state(got[name]["badge"])
            pulse = self._pulse_state(got[name]["pulse"])
            self.assertEqual(badge, pulse,
                             "%s: the badge says %s and the summary says %s "
                             "for ONE row — %s vs %s"
                             % (name, badge, pulse,
                                got[name]["badge"], got[name]["pulse"]))
            self.assertEqual(badge, want,
                             "%s: both surfaces agree on %s, but the honest "
                             "state is %s" % (name, badge, want))


class LedgerBadgeRuntimeTest(unittest.TestCase):
    """ledgerBadges — the per-entry finality tier — run VERBATIM under node.

    #220: the badge gated on t.consensus_final, a field only dregg PR #54 would
    have served and which closed UNMERGED upstream. No node has ever served it,
    so every entry fell to the "tentative" fallback and the page promised the
    owner a wait that could never complete. The three states are asserted by
    the WORDS the owner reads, not by the function not throwing."""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        src = web_ui_loader.read_text()
        # the REAL esc(), pinned verbatim — a stub here would test my stub
        esc = [ln for ln in src.splitlines() if ln.startswith("const esc =")]
        assert esc, "esc() is no longer a one-line const in assembled web UI"
        cls.tmp = tempfile.mkdtemp(prefix="helm-badge-")
        cls.path = os.path.join(cls.tmp, "run.js")
        with open(cls.path, "w", encoding="utf-8") as f:
            f.write(esc[0] + "\n\n" + _extract_fn(src, "ledgerBadges") + """

const cases = JSON.parse(require("fs").readFileSync(process.argv[2], "utf8"));
const out = {};
for (const k of Object.keys(cases)) out[k] = ledgerBadges(cases[k]);
process.stdout.write(JSON.stringify(out));
""")
        chk = subprocess.run([cls.node, "--check", cls.path],
                             capture_output=True, text=True)
        assert chk.returncode == 0, "node --check failed:\n" + chk.stderr

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def badge(self, **cases):
        path = os.path.join(self.tmp, "cases.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cases, f)
        p = subprocess.run([self.node, self.path, path],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def test_an_absent_certificate_reads_solo_committed_not_tentative(self):
        """The deployed shape: no consensus_final key at all."""
        out = self.badge(solo={"chain_index": 77, "finality": "tentative"})
        self.assertIn("solo-committed", out["solo"])
        self.assertIn("h77", out["solo"])
        self.assertNotIn("tentative", out["solo"])

    def test_a_served_certificate_still_reads_final(self):
        """PR-54-shaped: the field is present and true."""
        out = self.badge(fin={"chain_index": 9, "consensus_final": True,
                              "attested_height": 342, "finality": "tentative"})
        self.assertIn("final @ h342", out["fin"])
        self.assertNotIn("solo-committed", out["fin"])

    def test_a_served_certificate_saying_not_yet_is_a_REAL_wait(self):
        """Present-but-false is the one case that genuinely awaits finality.
        Collapsing it into solo-committed would replace one lie with another."""
        out = self.badge(w={"chain_index": 9, "consensus_final": False,
                            "finality": "tentative"})
        self.assertIn("tentative", out["w"])
        self.assertNotIn("solo-committed", out["w"])

    def test_an_unreadable_position_says_so_rather_than_inventing_one(self):
        """The repo's governing bug class: a datum that cannot be read must
        never render as empty, zero, or fine."""
        out = self.badge(nox={"finality": "tentative"})
        self.assertIn("solo-committed", out["nox"])
        self.assertIn("h?", out["nox"])
        self.assertNotIn("h0", out["nox"])

    def test_the_inverted_predate_comment_is_gone(self):
        """The old comment called the fallback a legacy path "for nodes that
        predate the certificate" — exactly backwards: no node ever served it,
        so the fallback is the ONLY path. Pin the correction so it cannot
        silently return."""
        src = web_ui_loader.read_text()
        self.assertNotIn("nodes that predate the certificate", src)
        self.assertIn("closed UNMERGED", src)


class SigningPulseRuntimeTest(unittest.TestCase):
    """cavePulse — the signing-pulse line — run VERBATIM under node (the
    CardRuntimeBase pattern from test_web_lr.py): the assertion is about the
    words the owner sees, not about the function not throwing. The pulse is
    pure (turns + head in, stable string out); clock-dependent children run
    through cavePulseClock in the client runtime test. Skipped, not failed,
    where node is unavailable."""

    NOW = 1784583000

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        src = web_ui_loader.read_text()
        cls.tmp = tempfile.mkdtemp(prefix="helm-pulse-")
        cls.path = os.path.join(cls.tmp, "run.js")
        with open(cls.path, "w", encoding="utf-8") as f:
            f.write(_extract_fn(src, "cavePulse") + """

const cases = JSON.parse(require("fs").readFileSync(process.argv[2], "utf8"));
const out = {};
for (const k of Object.keys(cases))
  out[k] = cavePulse(cases[k].turns, cases[k].head);
process.stdout.write(JSON.stringify(out));
""")
        chk = subprocess.run([cls.node, "--check", cls.path],
                             capture_output=True, text=True)
        assert chk.returncode == 0, "node --check failed:\n" + chk.stderr

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def pulse(self, **cases):
        path = os.path.join(self.tmp, "cases.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cases, f)
        p = subprocess.run([self.node, self.path, path],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    @staticmethod
    def turn(i, ts, final=True):
        return {"chain_index": i, "timestamp": ts,
                "consensus_final": final, "finality": "tentative"}

    def test_an_empty_read_is_measured_as_empty_not_rendered_blank(self):
        out = self.pulse(empty={"turns": [], "head": None, "now": self.NOW},
                         missing={"turns": None, "head": None, "now": self.NOW})
        self.assertIn("no signed entries in this read", out["empty"])
        self.assertIn("no signed entries in this read", out["missing"])
        # no counts invented over nothing
        self.assertNotIn("final", out["empty"])
        self.assertNotIn("final", out["missing"])

    def test_a_healthy_span_reads_unbroken_with_all_final(self):
        turns = [self.turn(12, self.NOW - 60), self.turn(11, self.NOW - 180),
                 self.turn(10, self.NOW - 300)]
        out = self.pulse(ok={"turns": turns, "head": 12, "now": self.NOW})["ok"]
        self.assertIn("entries #10–#12 unbroken ✓", out)
        self.assertIn("all <b>3</b> final", out)
        self.assertIn("3 entries over", out)
        self.assertNotIn("gap", out)
        self.assertNotIn("behind", out)

    def test_a_gap_in_the_shown_positions_is_counted_out_loud(self):
        turns = [self.turn(8, self.NOW - 60), self.turn(5, self.NOW - 300)]
        out = self.pulse(gap={"turns": turns, "head": 8, "now": self.NOW})["gap"]
        self.assertIn("⚠ gap", out)
        self.assertIn("2 missing", out)

    def test_a_read_behind_the_services_head_says_so(self):
        turns = [self.turn(12, self.NOW - 60)]
        out = self.pulse(b={"turns": turns, "head": 20, "now": self.NOW})["b"]
        self.assertIn("behind", out)
        self.assertIn("#20", out)

    @staticmethod
    def solo(i, ts):
        """A receipt from a node that serves NO consensus certificate: the
        field is ABSENT, which is exactly what our deployed solo node returns.
        `finality` is "tentative" by design there and finalization is
        internal-only, so it never becomes anything else."""
        return {"chain_index": i, "timestamp": ts, "finality": "tentative"}

    def test_the_finality_split_counts_both_sides(self):
        turns = [self.turn(3, self.NOW - 60, final=True),
                 self.turn(2, self.NOW - 120, final=False),
                 self.turn(1, self.NOW - 180, final=False)]
        out = self.pulse(m={"turns": turns, "head": 3, "now": self.NOW})["m"]
        self.assertIn("<b>1</b> final", out)
        self.assertIn("<b>2</b> awaiting finality", out)

    def test_a_solo_read_never_promises_a_wait_that_cannot_complete(self):
        """#220: every entry from a solo node was counted "awaiting finality"
        because consensus_final was absent — a field no node has ever served
        (dregg PR #54, closed unmerged). The count must say what is true."""
        turns = [self.solo(3, self.NOW - 60), self.solo(2, self.NOW - 120),
                 self.solo(1, self.NOW - 180)]
        out = self.pulse(s={"turns": turns, "head": 3, "now": self.NOW})["s"]
        self.assertIn("all <b>3</b> solo-committed", out)
        self.assertNotIn("awaiting finality", out)

    def test_a_mixed_read_separates_solo_from_a_real_wait(self):
        """The three states are not two. A certificate-serving node saying
        "not yet" is a REAL wait and must keep saying so, side by side with
        solo entries that are not waiting for anything."""
        turns = [self.turn(4, self.NOW - 60, final=True),
                 self.turn(3, self.NOW - 120, final=False),
                 self.solo(2, self.NOW - 180)]
        out = self.pulse(x={"turns": turns, "head": 4, "now": self.NOW})["x"]
        self.assertIn("<b>1</b> final", out)
        self.assertIn("<b>1</b> solo-committed", out)
        self.assertIn("<b>1</b> awaiting finality", out)

    def test_certificate_absence_decides_before_any_finality_word(self):
        """The row codex caught at review: certificate ABSENT but finality
        "final". Absence must win, because a node serving no certificate cannot
        report consensus finality — and ledgerBadges already called this row
        solo, so counting it "final" made the two surfaces contradict on one
        entry.

        THE PREVIOUS VERSION OF THIS TEST ASSERTED THE CONTRADICTION. It built
        exactly this row and expected it in the final bucket, so it defended
        the divergence while its own docstring claimed the states matched. A
        test written from the implementation pins whatever the code does; this
        one is written from the invariant."""
        turns = [{"chain_index": 2, "timestamp": self.NOW - 60,
                  "finality": "final"},
                 self.solo(1, self.NOW - 120)]
        out = self.pulse(d={"turns": turns, "head": 2, "now": self.NOW})["d"]
        self.assertIn("all <b>2</b> solo-committed", out)
        self.assertNotIn("awaiting finality", out)
        self.assertNotIn("</b> final", out)

    def test_unreadable_times_and_positions_say_so_not_zero(self):
        """The repo's governing bug class: a datum that cannot be read must
        never render as empty, zero, or fine."""
        no_ts = [{"chain_index": 4, "consensus_final": True},
                 {"chain_index": 3, "consensus_final": True}]
        no_ix = [{"timestamp": self.NOW - 60, "consensus_final": True}]
        out = self.pulse(ts={"turns": no_ts, "head": 4, "now": self.NOW},
                         ix={"turns": no_ix, "head": 4, "now": self.NOW})
        self.assertIn("times unreadable", out["ts"])
        self.assertIn("entries #3–#4 unbroken ✓", out["ts"])  # the rest still measures
        self.assertIn("positions unreadable", out["ix"])


class ThePulseAndTheRecordSplitAreWiredTest(unittest.TestCase):
    """The wiring leg (the TheConsoleRendersItTest pattern): a pure renderer
    nothing mounts, feeds, or resets is a showcase nothing shows."""

    def setUp(self):
        self.ui = web_ui_loader.read_text()

    def test_the_pulse_line_is_mounted_in_the_signed_entries_card(self):  # noqa: VACUOUS_ASSERTION — the assertIn on the id IS the unconditional positive control; the order checks after it are positions, and .index() raises on absence anyway
        """Mounted between the card's header and its rows — the same place
        the local card mounts its chat-pulse legend (the prior art)."""
        self.assertIn('id="cavepulse"', self.ui)
        i = self.ui.index('id="cavepulse"')
        self.assertGreater(i, self.ui.index('id="cavecard"'))
        self.assertLess(i, self.ui.index('id="ledgerturns"'))

    def test_the_poll_feeds_the_pulse_and_the_offline_branch_says_UNKNOWN(self):
        src = _extract_fn(self.ui, "pollLedger")
        self.assertIn("cavePulse(d.turns, LEDGER_HEAD", src)
        # offline: the pulse says UNKNOWN in words — never left holding the
        # previous render as if the service were still answering
        off = src.split("if (d.offline)", 1)[1].split("return;", 1)[0]
        self.assertIn("cavepulse", off)
        self.assertIn("UNKNOWN", off)

    def test_the_strip_compares_the_two_records_and_never_fakes_a_zero(self):
        src = _extract_fn(self.ui, "ledgerStrip")
        self.assertIn("record:", src)
        self.assertIn("local-only", src)
        # either side unread renders the word UNKNOWN, not 0
        self.assertIn('sharedN === null ? "UNKNOWN"', src)
        self.assertIn('NATIVE_COUNT === null ? "UNKNOWN"', src)
        # and an unreadable native read RESETS the stash rather than serving
        # the last count over a record it can no longer see
        poll = _extract_fn(self.ui, "pollLedger")
        self.assertIn("NATIVE_COUNT = null", poll)

    def test_the_who_signed_rows_carry_the_share_meter(self):
        src = _extract_fn(self.ui, "ledgerCells")
        self.assertIn("lmeter", src)
        self.assertIn("peak", src)


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
