#!/usr/bin/env python3
"""helm chat v2 — the signed transport (mock-signed, hermetic), reactions,
the log-after leg, and the node supervisor's pure parts. No network, no
binary, no systemd: the signing seam (_sign_send) is mocked here; the REAL
node round-trip lives in test_chat_node_live.py (skips without the binary)."""
import io
import contextlib
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import cell as cellmod  # noqa: E402
from helm import chat, chatnode, home  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "MELD_CHAT_NODE_URL", "HELM_CHAT_LOG", "MELD_CHAT_LOG",
            "HELM_CHAT_NODE_BIN", "MELD_CHAT_NODE_BIN",
            "HELM_CELL_BIN", "MELD_CELL_BIN")

SENT = {"sent": True, "turn_hash": "t" * 64, "receipt_hash": "r" * 64,
        "chain_index": 7}


class V2Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-chatv2-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""  # off unless a test opts in

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)


class TransportTest(V2Base):
    def test_node_url_env_empty_disables(self):
        self.assertIsNone(chat.node_url())
        self.assertEqual(chat.transport_status(),
                         {"mode": "unsigned", "url": None, "head": None,
                          "signer": False})

    def test_node_url_env_wins_and_strips(self):
        os.environ["HELM_CHAT_NODE_URL"] = "http://127.0.0.1:9999/"
        self.assertEqual(chat.node_url(), "http://127.0.0.1:9999")

    def test_node_url_state_file_fallback_then_default(self):
        del os.environ["HELM_CHAT_NODE_URL"]
        self.assertEqual(chat.node_url(), chatnode.default_url())
        chatnode.write_state({"url": "http://127.0.0.1:4242"})
        self.assertEqual(chat.node_url(), "http://127.0.0.1:4242")
        mode = stat.S_IMODE(os.stat(chatnode.state_path()).st_mode)
        self.assertEqual(mode, 0o600)  # the node credential is the operator's

    def test_digest_payload_contract(self):
        p = chat.digest_payload("hello")
        self.assertTrue(p.startswith(chat.CHAT_TAG))
        self.assertEqual(len(p), len(chat.CHAT_TAG) + 64)  # inside 104B budget
        self.assertEqual(p, chat.digest_payload("hello"))
        self.assertNotEqual(p, chat.digest_payload("hello!"))

    def test_signed_post_carries_the_receipt(self):
        with mock.patch.object(chat, "_sign_send", return_value=(SENT, None)) as ss:
            m = chat.post("signed :fire:", who="a1", sign=True)
        self.assertEqual(m["chain"], 7)
        self.assertEqual(m["turn"], "t" * 64)
        self.assertEqual(m["receipt"], "r" * 64)
        self.assertEqual(m["text"], "signed 🔥")  # expansion BEFORE signing
        ss.assert_called_once_with(chat.digest_payload("signed 🔥"),
                                   mock.ANY)
        self.assertNotIn("[unsigned]", chat._fmt(m))

    def test_unsigned_post_renders_the_tag(self):
        m = chat.post("plain", who="a1")  # transport disabled -> v1 row
        self.assertNotIn("chain", m)
        self.assertIn("[unsigned]", chat._fmt(m))

    def test_sign_failure_falls_back_to_unsigned(self):
        with mock.patch.object(chat, "_sign_send", return_value=(None, "down")):
            m = chat.post("tried", who="a1", sign=True)
        self.assertNotIn("chain", m)
        self.assertEqual(chat.read()[1], 1)  # the message never dies

    def test_sign_leg_raising_falls_back_to_unsigned(self):
        # fallback law, hardened: a RAISING signing leg (a raced .cells.json
        # tmp write, a surprised client) degrades to the v1 row — the post
        # must never die on the signature (a raise once killed a web POST)
        with mock.patch.object(chat, "_sign_send",
                               side_effect=RuntimeError("raced tmp write")):
            m = chat.post("survives", who="a1", sign=True)
        self.assertNotIn("chain", m)
        self.assertEqual(chat.read()[1], 1)
        os.environ["HELM_CHAT_NODE_URL"] = "http://127.0.0.1:9"
        with mock.patch.object(chat, "node_head",
                               side_effect=OSError("probe blew up")):
            chat.post("still lands", who="a1")  # sign=None probe path
        self.assertEqual(chat.read()[1], 2)

    def test_sign_send_recovery_lap(self):
        """First send fails (locked/dry) -> ONE revive + faucet -> retry wins."""
        calls = []
        outs = [(1, "", "insufficient computrons"),
                (0, json.dumps(SENT) + "\n", "")]

        def fake_run(args, timeout=90, env_extra=None):
            if args[0] == "join":
                return 0, json.dumps({"cell": "c" * 64, "joined": True}), ""
            calls.append(args[0])
            return outs.pop(0)

        os.environ["HELM_CHAT_NODE_URL"] = "http://127.0.0.1:1"
        with mock.patch.object(cellmod, "bin_ready", return_value=True), \
             mock.patch.object(cellmod, "run_bin", side_effect=fake_run), \
             mock.patch.object(chat, "_revive", return_value="tok2") as rv, \
             mock.patch.object(chat, "_faucet") as fc:
            info, err = chat._sign_send("payload", "p1")
        self.assertIsNone(err)
        self.assertEqual(info["chain_index"], 7)
        self.assertEqual(calls, ["send", "send"])
        rv.assert_called_once()
        fc.assert_called_once_with("c" * 64)
        # the join result was cached RAM-side (the room dir), not on disk
        with open(chat.cells_path()) as f:
            self.assertEqual(json.load(f)["p1"], "c" * 64)

    def test_no_signer_short_circuits_the_signing_leg(self):
        """Day-review #1: with HELM_CELL_BIN unset a signed turn is
        impossible — the leg must decline instantly: no join, no revive
        (the live unlock POST that burned the node's 5/60s budget on every
        fleet post), and _signed_row's sign=None probe must not even touch
        the node. Unsigned-by-configuration is a fact, not a fault."""
        os.environ["HELM_CHAT_NODE_URL"] = "http://127.0.0.1:1"
        with mock.patch.object(cellmod, "run_bin") as rb, \
             mock.patch.object(chat, "_revive") as rv:
            info, err = chat._sign_send("payload", "p1")
        self.assertIsNone(info)
        self.assertIn("no signer", err)
        rb.assert_not_called()
        rv.assert_not_called()
        with mock.patch.object(chat, "node_head") as nh, \
             mock.patch.object(chat, "_revive") as rv:
            m = chat.post("dark leg", who="a1")   # sign=None probe path
        nh.assert_not_called()                    # the signer gate comes FIRST
        rv.assert_not_called()
        self.assertNotIn("chain", m)
        self.assertIn("[unsigned]", chat._fmt(m))

    def test_transport_status_reports_the_signer(self):
        """A reachable node without a signer must never read "signed" —
        the exact lying status the day review caught live: node answering,
        every row [unsigned]."""
        os.environ["HELM_CHAT_NODE_URL"] = "http://127.0.0.1:1"
        with mock.patch.object(chat, "node_head",
                               return_value={"chain_index": 15}):
            st = chat.transport_status()
            self.assertEqual((st["mode"], st["signer"], st["head"]),
                             ("unsigned (no signer)", False, 15))
            with mock.patch.object(cellmod, "bin_ready", return_value=True):
                self.assertEqual(chat.transport_status()["mode"], "signed")

    def test_room_cell_cache_hits_without_binary(self):
        os.environ["HELM_CHAT_NODE_URL"] = "http://127.0.0.1:1"
        chat._ensure_dir()
        with open(chat.cells_path(), "w") as f:
            json.dump({"me": "d" * 64}, f)
        self.assertEqual(chat._room_cell("me", ""), ("d" * 64, None))


class ReactTest(V2Base):
    def test_react_by_ordinal_and_negative(self):
        chat.post("one", who="a1")
        chat.post("two", who="a2")
        row, err = chat.react(1, ":tada:", who="a3")
        self.assertIsNone(err)
        self.assertEqual((row["react"], row["tfrom"]), ("🎉", "a1"))
        row, err = chat.react(-1, "fire", who="a3")   # bare shortcode + latest
        self.assertIsNone(err)
        self.assertEqual((row["react"], row["tfrom"]), ("🔥", "a2"))

    def test_react_by_ts_pair_and_misses(self):
        m = chat.post("hey", who="a1")
        row, err = chat.react((m["ts"], "a1"), "👀", who="a2")  # raw emoji
        self.assertIsNone(err)
        self.assertEqual(row["react"], "👀")
        self.assertEqual(chat.react((m["ts"], "ghost"), ":tada:")[0], None)
        self.assertIn("out of range", chat.react(9, ":tada:")[1])
        self.assertIn("unknown emoji", chat.react(1, ":no_such_zz:")[1])

    def test_reactions_do_not_count_as_messages_for_targets(self):
        chat.post("only", who="a1")
        chat.react(1, ":tada:", who="a2")
        row, err = chat.react(-1, ":fire:", who="a3")  # -1 is still the MESSAGE
        self.assertIsNone(err)
        self.assertEqual(row["tfrom"], "a1")

    def test_thread_aggregation_and_render(self):
        m1 = chat.post("popular", who="a1")
        chat.post("quiet", who="a2")
        chat.react(1, ":tada:", who="x")
        chat.react(1, ":tada:", who="y")
        chat.react(1, ":fire:", who="z")
        rows, total = chat.read()
        self.assertEqual(total, 5)
        msgs, reacts = chat.thread(rows)
        self.assertEqual([m["text"] for m in msgs], ["popular", "quiet"])
        counts = reacts[chat.rkey(m1)]
        self.assertEqual(counts, {"🎉": 2, "🔥": 1})
        self.assertEqual(chat.react_line(counts), "🎉×2  🔥×1")

    def test_cli_react(self):
        chat.post("cli", who="a1")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(chat.cmd_chat(["react", "1", ":rocket:"]), 0)
            self.assertEqual(chat.cmd_chat(["react", "9", ":rocket:"]), 1)
            self.assertEqual(chat.cmd_chat(["react", "x", ":rocket:"]), 2)
            self.assertEqual(chat.cmd_chat(["react", "1"]), 2)
        self.assertIn("reacted 🚀", out.getvalue())


class LogFlushTest(V2Base):
    def _log_files(self):
        d = chat.journal_dir()
        return sorted(f for f in os.listdir(d) if f.startswith("chat-")) \
            if os.path.isdir(d) else []

    def _body(self):
        with open(os.path.join(chat.journal_dir(), self._log_files()[0])) as f:
            return f.read()

    def test_flush_is_idempotent_and_out_of_band(self):
        chat.post("first", who="a1")
        chat.post("second :fire:", who="a2")
        chat.react(1, ":tada:", who="a2")
        # the send path wrote NOTHING under the journal (the RAM canon)
        self.assertEqual(self._log_files(), [])
        self.assertEqual(chat.log_flush(), 3)
        self.assertEqual(chat.log_flush(), 0)          # high-water mark holds
        self.assertEqual(len(self._log_files()), 1)
        body = self._body()
        self.assertIn("a1: first [unsigned]", body)
        self.assertIn("second 🔥", body)
        self.assertIn("reacted 🎉", body)
        chat.post("third", who="a1")
        self.assertEqual(chat.log_flush(), 1)          # only the delta
        self.assertEqual(self._body().count("third"), 1)

    def test_signed_rows_log_their_chain(self):
        with mock.patch.object(chat, "_sign_send", return_value=(SENT, None)):
            chat.post("attested", who="a1", sign=True)
        chat.log_flush()
        body = self._body()
        self.assertIn("attested {chain 7}", body)
        self.assertNotIn("[unsigned]", body)

    def test_rotation_reconciles_against_the_fingerprint(self):
        for i in range(4):
            chat.post("m%d" % i, who="a1")
        self.assertEqual(chat.log_flush(), 4)
        # the room rotates: newest half survives; flushed mark points mid-file
        with mock.patch.object(chat, "SIZE_CAP", 10):
            chat._rotate(chat.room_path("main"))
        chat.post("fresh", who="a1")
        self.assertEqual(chat.log_flush(), 1)  # only the new row — no re-log
        self.assertEqual(self._body().count("m3"), 1)

    def test_rotation_gap_is_loud(self):
        chat.post("only", who="a1")
        chat.log_flush()
        # the whole room turned over — mark's fingerprint is gone
        os.remove(chat.room_path("main"))
        chat.post("after-wipe", who="a1")
        chat.log_flush()
        body = self._body()
        self.assertIn("rotation gap", body)
        self.assertIn("after-wipe", body)

    def test_operator_disable(self):
        os.environ["HELM_CHAT_LOG"] = "0"
        chat.post("hidden", who="a1")
        self.assertEqual(chat.log_flush(), -1)
        self.assertTrue(chat.log_disabled())
        self.assertEqual(self._log_files(), [])

    def test_cli_log_flush(self):
        chat.post("cli", who="a1")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(chat.cmd_chat(["log-flush"]), 0)
        self.assertIn("appended 1 row", out.getvalue())
        with contextlib.redirect_stdout(out):
            self.assertEqual(chat.cmd_chat(["log-flush", "--room", "ghost"]), 0)


class NodeSupervisorTest(V2Base):
    def test_unit_text_contract(self):
        t = chatnode.unit_text("/x/dregg-cave-node")
        self.assertIn("--data-dir /dev/shm/helm-chat-node", t)   # tmpfs — RAM law
        self.assertIn("--port 8898", t)
        self.assertIn("--enable-faucet", t)                      # never die on balance
        self.assertIn("/x/dregg-cave-node run", t)
        self.assertIn("WantedBy=default.target", t)

    def test_bin_path_env_override(self):
        os.environ["HELM_CHAT_NODE_BIN"] = "/custom/node-bin"
        self.assertEqual(chatnode.bin_path(), "/custom/node-bin")

    def test_write_state_is_0600_from_birth(self):
        # the credential (passphrase + token) must never ride a umask-mode
        # tmp — 0600 from creation, a stale permissive tmp re-tightened
        stale = chatnode.state_path() + ".tmp"
        os.makedirs(os.path.dirname(stale), exist_ok=True)
        with open(stale, "w") as f:
            f.write("old")
        os.chmod(stale, 0o644)
        chatnode.write_state({"url": "u", "passphrase": "secret", "token": "t"})
        p = chatnode.state_path()
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        self.assertFalse(os.path.exists(stale))
        self.assertEqual(chatnode.state()["passphrase"], "secret")

    def test_bootstrap_cell_is_deterministic_hex(self):
        h = chatnode.bootstrap_cell_hex()
        self.assertEqual(len(h), 64)
        int(h, 16)
        self.assertEqual(h, chatnode.bootstrap_cell_hex())

    def test_cmd_node_usage_and_missing_binary(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(chatnode.cmd_node(["bogus"]), 2)
        with mock.patch.object(chatnode, "bin_path", return_value=None), \
             contextlib.redirect_stderr(err):
            self.assertEqual(chatnode.cmd_node(["up"]), 1)
        self.assertIn("not found", err.getvalue())

    def test_cmd_chat_dispatches_node(self):
        with mock.patch.object(chatnode, "cmd_node", return_value=0) as cn:
            self.assertEqual(chat.cmd_chat(["node", "status"]), 0)
        cn.assert_called_once_with(["status"])


if __name__ == "__main__":
    unittest.main()
