#!/usr/bin/env python3
"""helm chat v2 — the signed transport (mock-signed, hermetic), reactions,
the log-after leg, and the node supervisor's pure parts. No network, no
binary, no systemd: the signing seam (_sign_send) is mocked here; the REAL
node round-trip lives in test_chat_node_live.py (skips without the binary)."""
import io
import contextlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import cell as cellmod  # noqa: E402
from helm import chat, chatnode, home, human, meld, pk  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "MELD_CHAT_NODE_URL", "HELM_CHAT_ROOM", "MELD_CHAT_ROOM",
            "HELM_CHAT_ROOM_SOURCE", "MELD_CHAT_ROOM_SOURCE",
            "HELM_CHAT_LOG", "MELD_CHAT_LOG",
            "HELM_CHAT_NODE_BIN", "MELD_CHAT_NODE_BIN",
            "HELM_CELL_BIN", "MELD_CELL_BIN",
            # this file SETS HELM_CELL_PROFILE (to "p1", twice) and omitted it
            # here, so it leaked into every later test in the process. It is
            # what `_chat_profile()` reads to LABEL the owner row that
            # `_api_chat_roster` prepends at index [0] — so the leak renamed a
            # stranger's roster row to "p1" and failed an unrelated assertion
            # in tests/test_seats.py. A set-it key missing from the restore
            # list is invisible in this file and only ever fails elsewhere.
            "HELM_CELL_PROFILE")

SENT = {"sent": True, "turn_hash": "a" * 64, "receipt_hash": "b" * 64,
        "chain_index": 7}
READY_SIGNER = {"configured": True, "usable": True, "state": "ready",
                "reason": "signer ready"}


class V2Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-chatv2-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""  # off unless a test opts in
        # DETERMINISTIC, never the developer's ambient profile. Adding this key
        # to ENV_KEYS (so it stops leaking) also makes setUp POP it, and tests
        # here that expect signed transport need SOME profile — they had been
        # silently inheriting whoever ran them. That is the same class as the
        # leak: a test whose result depends on the ambient environment passes
        # for one operator and fails for another.
        os.environ["HELM_CELL_PROFILE"] = "test-profile"
        # cwd hermeticity: the default room derives from a git cwd
        # (seats.resolve_homing) — run from tmp so defaults stay 'main'
        self.cwd_prior = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        failure_dir = chat.sign_failures_dir()
        os.chdir(self.cwd_prior)
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(failure_dir, ignore_errors=True)
        shutil.rmtree(self.tmp, ignore_errors=True)


class DefaultRoomDerivesTest(V2Base):
    """room=None means DERIVE — the class fix for 2026-07-29's partition
    symptoms, where eleven call sites relied on a room="main" parameter
    default and posted fleet traffic into the room the fleet does not use."""

    def test_none_derives_the_callers_project_room(self):
        with mock.patch.object(chat, "_default_post_room", return_value="projx") as d:
            row = chat.post("derived-room probe", who="t")
        d.assert_called_once()
        self.assertTrue(os.path.exists(chat.room_path("projx")))

    def test_explicit_main_is_untouched_deliberate_centralization(self):
        with mock.patch.object(chat, "_default_post_room") as d:
            chat.post("explicit main probe", room="main", who="t")
        d.assert_not_called()
        self.assertTrue(os.path.exists(chat.room_path("main")))

    def test_derivation_failure_fails_open_to_main(self):
        """Homing must never break a post: seats exploding -> #main, not a
        raise and not a lost row."""
        import helm.seats as seats
        with mock.patch.object(seats, "resolve_homing",
                               side_effect=RuntimeError("boom")):
            self.assertEqual(chat._default_post_room(), "main")

    def test_a_dm_ignores_the_derived_default_entirely(self):
        with mock.patch.object(chat, "_default_post_room", return_value="projx"):
            row = chat.post("dm probe", who="t", dm="other")
        self.assertFalse(os.path.exists(chat.room_path("projx")),
                         "a DM must never land in a derived room file")


class TransportTest(V2Base):
    def test_node_url_env_empty_disables(self):
        self.assertIsNone(chat.node_url())
        self.assertEqual(chat.transport_status(),
                         {"mode": "unsigned", "url": None, "head": None,
                          "signer": False, "signer_configured": False})
        self.assertFalse(os.path.exists(os.environ["HELM_CHAT_DIR"]))

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
        self.assertEqual(m["turn"], "a" * 64)
        self.assertEqual(m["receipt"], "b" * 64)
        self.assertEqual(m["text"], "signed 🔥")  # expansion BEFORE signing
        ss.assert_called_once_with(chat.digest_payload("signed 🔥"),
                                   mock.ANY)
        self.assertNotIn("[unsigned]", chat._fmt(m))

    def test_unsigned_post_renders_the_tag(self):
        m = chat.post("plain", who="a1")  # transport disabled -> v1 row
        self.assertNotIn("chain", m)
        self.assertIn("[unsigned]", chat._fmt(m))

    def test_sign_failure_falls_back_to_unsigned(self):
        with mock.patch.object(
                chat, "_sign_send",
                return_value=(None, chat._diag("send_failed", "down"))):
            m = chat.post("tried", who="a1", profile="p1", sign=True)
        self.assertNotIn("chain", m)
        self.assertEqual(chat.read()[1], 1)  # the message never dies
        self.assertEqual(m["transport"]["state"], "DEGRADED")
        self.assertEqual(m["transport"]["profile"], "p1")
        self.assertEqual(m["transport"]["reason"], "down")
        self.assertIn("[DEGRADED p1/send_failed: down]", chat._fmt(m))

    def test_sign_leg_raising_falls_back_to_unsigned(self):
        # fallback law, hardened: a RAISING signing leg (a raced .cells.json
        # tmp write, a surprised client) degrades to the v1 row — the post
        # must never die on the signature (a raise once killed a web POST)
        with mock.patch.object(chat, "_sign_send",
                               side_effect=RuntimeError("raced tmp write")):
            m = chat.post("survives", who="a1", profile="p1", sign=True)
        self.assertNotIn("chain", m)
        self.assertEqual(chat.read()[1], 1)
        self.assertEqual(m["transport"]["code"], "signing_exception")
        self.assertIn("raced tmp write", m["transport"]["reason"])
        os.environ["HELM_CHAT_NODE_URL"] = "http://127.0.0.1:9"
        with mock.patch.object(chat, "node_head",
                               side_effect=OSError("probe blew up")):
            chat.post("still lands", who="a1")  # sign=None probe path
        self.assertEqual(chat.read()[1], 2)

    def test_join_recovery_lap_before_single_send(self):
        """The idempotent join still recovers, before the only send."""
        outs = [(1, "", "join failed"),
                (0, json.dumps({"cell": "c" * 64, "joined": True}), ""),
                (0, json.dumps(SENT), "")]
        with mock.patch.object(cellmod, "bin_status", return_value=READY_SIGNER), \
             mock.patch.object(cellmod, "run_bin", side_effect=outs) as run, \
             mock.patch.object(chat, "_revive", return_value=("tok2", None)) as revive, \
             mock.patch.object(chat, "_faucet") as faucet:
            info, err = chat._sign_send("payload", "p1")
        self.assertIsNone(err)
        self.assertEqual(info["chain_index"], 7)
        self.assertEqual([c.args[0][0] for c in run.call_args_list], ["join", "join", "send"])
        revive.assert_called_once()
        faucet.assert_not_called()
        with open(chat.cells_path()) as f:
            self.assertEqual(json.load(f)["p1"], "c" * 64)

    def test_no_funding_precedes_a_successful_send(self):  # noqa: VACUOUS_ASSERTION — the call log equal to exactly ["send"] is the unconditional positive observation on the same spy; the faucet's positive pole is ReactiveTopUp.test_a_fee_refusal_tops_up_once_retries_once_and_commits
        """THE TOP-UP IS REACTIVE: a cell at zero on a fee-free node sends
        without asking the faucet for anything. The positive pole — a fee
        refusal that does ask, once — is ReactiveTopUp in test_chat_faucet."""
        calls = mock.Mock()
        with mock.patch.object(cellmod, "bin_status", return_value=READY_SIGNER), \
             mock.patch.object(chat, "_room_cell", return_value=("c" * 64, None, True)), \
             mock.patch.object(chat, "_faucet", calls.faucet), \
             mock.patch.object(cellmod, "run_bin", calls.send), \
             mock.patch.object(chat, "_revive") as revive:
            calls.faucet.return_value = ({"success": True}, None)
            calls.send.return_value = (0, json.dumps(SENT), "")
            info, failure = chat._sign_send("payload", "p1")
        self.assertIsNone(failure)
        self.assertEqual(info["chain_index"], 7)
        self.assertEqual([c[0] for c in calls.mock_calls], ["send"])
        revive.assert_not_called()

    def test_launched_timeout_is_unknown_not_unavailable_and_never_retried(self):
        for verb in ("join", "send"):
            with self.subTest(verb=verb), contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(
                    cellmod, "bin_status", return_value=READY_SIGNER))
                stack.enter_context(mock.patch.object(
                    cellmod, "bin_path", return_value="/synthetic/signer"))
                stack.enter_context(mock.patch.object(cellmod, "build_env", return_value={}))
                stack.enter_context(mock.patch.object(chat, "_node_token", return_value=""))
                stack.enter_context(mock.patch.object(chat, "_env_extra", return_value={}))
                stack.enter_context(mock.patch.object(
                    chat.pk, "read_json", return_value={} if verb == "join" else {"p1": "c" * 64}))
                run = stack.enter_context(mock.patch.object(
                    cellmod.subprocess, "run", side_effect=cellmod.subprocess.TimeoutExpired(
                        ["signer", verb], 30, output=b"private stdout",
                        stderr=b"private stderr")))
                revive = stack.enter_context(mock.patch.object(chat, "_revive"))
                faucet = stack.enter_context(mock.patch.object(chat, "_faucet"))
                info, failure = chat._sign_send("payload", "p1")
                self.assertIsNone(info)
                self.assertEqual(failure["code"], "signing_timeout")
                self.assertIn("cell %s timed out after 30s" % verb, failure["reason"])
                self.assertIn("outcome unknown", failure["reason"])
                self.assertNotIn("private", str(failure))
                self.assertIn("before retrying", failure["remediation"])
                run.assert_called_once()
                self.assertEqual(run.call_args.args[0][1], verb)
                revive.assert_not_called()
                faucet.assert_not_called()

    def test_recovery_join_timeout_never_gets_another_lap(self):
        with mock.patch.object(cellmod, "bin_status", return_value=READY_SIGNER), \
             mock.patch.object(chat, "_node_token", return_value=""), \
             mock.patch.object(chat, "_room_cell", side_effect=[
                 (None, "join failed", True),
                 (None, cellmod.BinTimeout("cell join timed out after 30s; outcome unknown"), True)]) as join, \
             mock.patch.object(chat, "_revive", return_value=(None, "refused")) as revive, \
             mock.patch.object(cellmod, "run_bin") as send, \
             mock.patch.object(chat, "_faucet") as faucet:
            info, failure = chat._sign_send("payload", "p1")
        self.assertIsNone(info)
        self.assertEqual(failure["code"], "signing_timeout")
        self.assertIn("cell join timed out after 30s", failure["reason"])
        self.assertEqual(join.call_count, 2)
        revive.assert_called_once()
        send.assert_not_called()
        faucet.assert_not_called()

    def test_room_cell_timeout_counts_as_launched(self):
        reason = cellmod.BinTimeout("cell join timed out after 30s; outcome unknown")
        with mock.patch.object(chat.pk, "read_json", return_value={}), \
             mock.patch.object(chat, "_env_extra", return_value={}), \
             mock.patch.object(cellmod, "run_bin", return_value=(None, "", reason)):
            cell, error, launched = chat._room_cell("p1", "")
        self.assertIsNone(cell)
        self.assertIs(error, reason)
        self.assertIs(launched, True)

    def test_launch_failure_is_unavailable_and_never_revived(self):
        with mock.patch.object(cellmod, "bin_status", return_value=READY_SIGNER), \
             mock.patch.object(chat, "_node_token", return_value=""), \
             mock.patch.object(chat, "_room_cell", return_value=(None, "could not execute", False)), \
             mock.patch.object(chat, "_revive") as revive:
            info, failure = chat._sign_send("payload", "p1")
        self.assertIsNone(info)
        self.assertEqual(failure["code"], "signer_unavailable")
        revive.assert_not_called()

    def test_send_without_explicit_rejection_is_never_replayed(self):
        rejected = "[client-sign] error: node refused the turn: insufficient computrons"
        accepted = "[client-sign] turn accepted: %s; awaiting receipt..." % ("a" * 64)
        cases = (
            (1, "", accepted + "\n[client-sign] error: GET receipts: disconnected"),
            (1, "", "[client-sign] error: POST /turns/submit: disconnected"),
            (1, "", "[client-sign] error: parse submit response: truncated"),
            (1, "{", rejected),
            (1, '{"sent":', rejected),
            (1, json.dumps(SENT), rejected),
            (1, '{"sent":true}', rejected),
            (1, '{"accepted":true}', rejected),
            (1, "", accepted + "\n" + rejected),
            (1, "", "[client-sign] turn accepted: truncated\n" + rejected),
            (1, "", rejected + "\ntruncated stderr"),
            (1, "", "[client-sign] error: node refused the turn:"),
            (1, "", "[client-sign] error: node refused the turn:   "),
            (1, "", "insufficient computrons"),
            (1, "", "[client-sign] error: node refused the turn: no reason given"),
            (1, "", "node refused the turn (no reason given)"),
            (1, "", "[client-sign] error: rejected: node refused the turn: x"),
            (1, "", ""),
            (-9, "", rejected),
            (0, "", rejected),
            (0, '{"sent":true}', rejected),
        )
        for result in cases:
            with self.subTest(result=result), \
                 mock.patch.object(cellmod, "bin_status", return_value=READY_SIGNER), \
                 mock.patch.object(chat, "_node_token", return_value=""), \
                 mock.patch.object(chat, "_env_extra", return_value={}), \
                 mock.patch.object(chat, "_room_cell", return_value=("c" * 64, None, True)), \
                 mock.patch.object(cellmod, "run_bin", return_value=result) as run, \
                 mock.patch.object(chat, "_revive", return_value=("", None)) as revive, \
                 mock.patch.object(chat, "_faucet", return_value=({}, None)) as faucet:
                info, failure = chat._sign_send("payload", "p1")
                self.assertIsNone(info)
                self.assertEqual(failure["code"], "send_outcome_unknown")
                self.assertIn("outcome unknown", failure["reason"])
                self.assertIn("before retrying", failure["remediation"])
                run.assert_called_once()
                revive.assert_not_called()
                faucet.assert_not_called()

    def test_an_explicit_node_refusal_is_failed_and_keeps_the_nodes_text(self):  # noqa: VACUOUS_ASSERTION — both loops run over literal non-empty tuples, and every iteration asserts the send_failed code and the carried text positively
        """The node said no, and said why. Before this, the row read
        `send_outcome_unknown` with a generic reason, and the node's text was
        dropped, so an operator inspected receipts for a turn that had not
        committed. The send is still attempted exactly once."""
        chain = ("receipt chain mismatch: receipt chain mismatch: cipherclerk "
                 "head = Some([7, 1]), receipt's prev = Some([9, 2])")
        cases = (
            ("[client-sign] error: node refused the turn: " + chain, chain),
            ("[client-sign] verified ML-DSA cores: sign Some, verify Some\n"
             "[client-sign] coordination-exempt join\n"
             "[client-sign] error: node refused the turn: " + chain + "\n",
             chain),
            ("node refused the turn: " + chain, chain),
            ("[client-sign] error: node refused the turn: unknown", "unknown"),
            # ANOTHER cell's shortfall is not this cell's fee: no top-up.
            # (This cell's own is ReactiveTopUp in test_chat_faucet.)
            ("[client-sign] error: node refused the turn: rejected: insufficient "
             "balance on cell dddddddddddddddd: need 2, have 1",
             "rejected: insufficient balance on cell dddddddddddddddd: need 2, have 1"),
        )
        for stderr, text in cases:
            for stdout in ("", "\n  \n"):
                with self.subTest(stderr=stderr, stdout=stdout), \
                     mock.patch.object(cellmod, "bin_status", return_value=READY_SIGNER), \
                     mock.patch.object(chat, "_node_token", return_value=""), \
                     mock.patch.object(chat, "_env_extra", return_value={}), \
                     mock.patch.object(chat, "_room_cell", return_value=("c" * 64, None, True)), \
                     mock.patch.object(cellmod, "run_bin", return_value=(1, stdout, stderr)) as run, \
                     mock.patch.object(chat, "_revive") as revive, \
                     mock.patch.object(chat, "_faucet") as faucet:
                    info, failure = chat._sign_send("payload", "p1")
                    self.assertIsNone(info)
                    self.assertEqual(failure["code"], "send_failed")
                    self.assertIn("the node refused the turn, so it did not commit: "
                                  + text, failure["reason"])
                    self.assertNotIn("outcome unknown", failure["reason"])
                    run.assert_called_once()
                    revive.assert_not_called()
                    faucet.assert_not_called()

    def test_a_refusal_beside_a_dry_faucet_keeps_send_failed_and_its_text(self):
        """A DRY FAUCET DOES NOT WIN. The node refused this send for a reason
        that is not its fee, so the faucet is never asked and the row keeps
        the node's code and text. Measured on live rows: chain-race refusals
        read faucet_source_dry and sent everyone to refuel."""
        dry = "%s: cell 4a8882bb17c23b3e holds 260" % chatnode.FAUCET_DRY_MARK
        with mock.patch.object(cellmod, "bin_status", return_value=READY_SIGNER), \
             mock.patch.object(chat, "_room_cell", return_value=("c" * 64, None, True)), \
             mock.patch.object(chat, "_faucet", return_value=(None, dry)) as faucet, \
             mock.patch.object(cellmod, "run_bin", return_value=(
                 1, "", "[client-sign] error: node refused the turn: rejected: broke")):
            info, failure = chat._sign_send("payload", "p1")
        self.assertIsNone(info)
        self.assertEqual(failure["code"], "send_failed")
        self.assertIn("so it did not commit: rejected: broke", failure["reason"])
        self.assertNotIn(chatnode.FAUCET_DRY_MARK, failure["reason"])
        faucet.assert_not_called()  # noqa: VACUOUS_ASSERTION — the faucet spy's positive control is test_a_fee_refusals_failed_top_up_is_scrubbed_and_never_replayed below, which drives the same seam to one call

    def test_a_fee_refusals_failed_top_up_is_scrubbed_and_never_replayed(self):  # noqa: VACUOUS_ASSERTION — the send_failed code, the kept faucet text, the [redacted] marker and faucet.assert_called_once_with are unconditional positives on the same diagnostic
        secret = "x" * 320  # redact before capping a retained recovery diagnostic
        with mock.patch.object(cellmod, "bin_status", return_value=READY_SIGNER), \
             mock.patch.object(chat, "_room_cell", return_value=("c" * 64, None, True)), \
             mock.patch.object(cellmod, "run_bin", return_value=(
                 1, "", "[client-sign] error: node refused the turn: rejected: "
                 "insufficient balance on cell cccccccccccccccc: need 2, have 0")) as run, \
             mock.patch.object(chat, "_revive") as revive, \
             mock.patch.object(chat, "_faucet", return_value=(
                 None, "faucet refused: rate limited token=" + secret)) as faucet:
            info, failure = chat._sign_send("payload", "p1")
        self.assertIsNone(info)
        self.assertEqual(failure["code"], "send_failed")
        self.assertIn("faucet refused: rate limited", failure["reason"])
        self.assertNotIn(secret[:40], failure["reason"])
        self.assertIn("token=[redacted]", failure["reason"])
        run.assert_called_once()
        faucet.assert_called_once_with("c" * 64)
        revive.assert_not_called()

    def test_diagnostics_scrub_auth_schemes_and_quoted_secret_keys(self):
        cases = (
            "Authorization: Basic dXNlcjpwYXNz",
            'Authorization: Digest username="daria", response="sekrit"',
            "Authorization='Signature keyId=abc signature=sekrit'",
            "{'bearer_token':'sekrit with spaces'}",
            '{"refresh_token": "also-secret"}',
            '{"token": "abc\\\"sekrit"}',
            "token abc123",
            "Basic abc123 extra",
            "Bearer abc123",
        )
        for raw in cases:
            clean = chat._safe_reason(raw)
            self.assertNotIn("sekrit", clean, raw)
            self.assertNotIn("dXNlcjpwYXNz", clean, raw)
            self.assertNotIn("also-secret", clean, raw)
            self.assertNotIn("abc123", clean, raw)
            self.assertIn("redacted", clean, raw)

    # The real message that exposed this, frozen as bytes rather than rebuilt
    # from cell.py — a fixture that IMPORTS its subject moves with it and stops
    # testing the case that actually happened.
    IDENTITY_CONFLICT_407 = (
        "identity conflict: this process resolves to seat 'helm-claude' but "
        "the environment names profile 'daria'. Signing as 'daria' would "
        "attribute this row to someone who did not write it, and "
        "signing as 'helm-claude' is impossible without that seat's own key "
        "— so it is left UNSIGNED. Relaunch through `helm launch` "
        "(which sets both vars to the seat) or pass an explicit "
        "profile if you mean to speak for 'daria'.")

    def test_an_over_cap_diagnostic_keeps_its_remediation(self):
        """THE BUG: a head-cut always eats the half that says what to do.

        A diagnostic states the problem first and the REMEDY last. `[:360]` cut
        this 407-character message inside the word 'explicit' and deleted
        "or pass an explicit profile if you mean to speak for X" — and what
        survived was grammatical enough to read as the whole refusal. Two seats
        read it that way and neither saw the escape hatch.
        """
        raw = self.IDENTITY_CONFLICT_407
        self.assertGreater(len(raw), chat.REASON_CAP, "fixture must exceed the cap")
        out = chat._safe_reason(raw)
        self.assertLessEqual(len(out), chat.REASON_CAP)
        self.assertIn("speak for", out, "the remediation clause must survive")
        self.assertIn("identity conflict", out, "the diagnosis must survive too")

    def test_truncation_announces_itself_and_reports_the_true_count(self):
        """A silent cut is indistinguishable from a complete message.

        The marker is the whole point: it is what tells a reader that what they
        are holding is not all of it. The count must be the number ACTUALLY
        removed, not the marker's own budget, or the honesty is decorative.
        """
        raw = self.IDENTITY_CONFLICT_407
        out = chat._safe_reason(raw)
        self.assertIn("elided", out)
        # The surrounding whitespace is PART of the marker, so it must be in
        # the match or `kept` counts it as surviving content and the arithmetic
        # lands two characters off — measured while writing this arm.
        m = re.search(r"\s…(\d+) chars elided…\s", out)
        self.assertIsNotNone(m, out)
        kept = len(out) - len(m.group(0))
        self.assertEqual(int(m.group(1)), len(raw) - kept,
                         "the reported count must equal what was removed")

    def test_a_diagnostic_within_the_cap_is_returned_untouched(self):  # noqa: VACUOUS_ASSERTION — assertEqual(out, raw) is the unconditional positive control on the same observable; the assertNotIn only adds that the marker did not appear
        """POSITIVE CONTROL: the cure must not mangle the common case.

        Nearly every reason is short. If elision fired on those, every arm
        above would still pass while the surface got worse.
        """
        for raw in ("no signer", "x" * chat.REASON_CAP):
            out = chat._safe_reason(raw)
            self.assertEqual(out, raw)
            self.assertNotIn("elided", out)

    def test_elision_can_never_resurrect_a_redacted_secret(self):  # noqa: VACUOUS_ASSERTION — assertIn('elided') and assertIn('redacted') positively control the same output; the secret's absence is the third fact, not the only one
        """Redaction runs BEFORE the cut, and this pins the ORDER.

        Keeping a tail is new; if it were ever computed from the pre-redaction
        string, the kept tail could carry a secret the head-cut used to hide.
        """
        secret = "sekrit-value-do-not-print"
        # The secret sits in the HEAD on purpose. Placed mid-string it lands in
        # the elided span, so "the secret is absent" would pass for the wrong
        # reason — the marker proving redaction RAN would have been cut out
        # with it. Here both facts are observable at once.
        raw = ('{"api_key": "%s"} ' % secret) + ("diagnosis detail " * 40)
        out = chat._safe_reason(raw)
        self.assertGreater(len(raw), chat.REASON_CAP)
        self.assertIn("elided", out, "fixture must actually be elided")
        self.assertNotIn(secret, out)
        self.assertIn("redacted", out, "redaction must be visible in the kept head")

    # A structurally valid JWT: three dot-separated base64url segments, no
    # spaces. Frozen here rather than generated, so the arm keeps testing the
    # shape a review actually probed with.
    PROBE_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig"

    def test_a_jwt_is_redacted_wherever_it_sits_in_the_diagnostic(self):
        """A BLOCKER on this lane, and the fix is wider than the report.

        Keeping a tail exposed a complete JWT that main's head-cut happened to
        truncate away — a real regression, measured: this lane leaked it, main
        did not. But main is only ACCIDENTALLY safe. `jwt` was never in the
        secret-key list at all (the list matched `token`, never the bare
        spelling), so main leaks the SAME token when it appears EARLY, inside
        the head it keeps. Measured both ways before fixing.

        So the cure is to redact by KEY, which closes both positions, rather
        than to special-case the tail — which would have left main's hole open
        and made this function's safety depend on where a secret happened to
        land.
        """
        jwt = self.PROBE_JWT
        # UNCONDITIONAL positive control on the same observable, outside any
        # loop: this one call must redact, or the loop below proves nothing.
        anchor = chat._safe_reason("jwt=%s tail" % jwt)
        self.assertIn("redacted", anchor)
        self.assertNotIn(jwt, anchor)
        cases = {
            "at the end, over cap (the exact probe)":
                ("diagnosis detail " * 30) + ("jwt=%s remedy" % jwt),
            "early, inside the kept head (main leaks this)":
                ("jwt=%s " % jwt) + ("diagnosis detail " * 40),
            "under the cap, no elision at all":
                "auth failed jwt=%s retry" % jwt,
            "uppercase key":
                ("d " * 200) + ("JWT=%s tail" % jwt),
        }
        self.assertEqual(len(cases), 4,
                         "the loop below is only as good as this many cases")
        checked = 0
        for name, raw in cases.items():
            out = chat._safe_reason(raw)
            self.assertNotIn(jwt, out, name)
            self.assertIn("redacted", out, name)
            checked += 1
        self.assertEqual(checked, 4, "every case must actually have run")

    def test_the_keys_that_already_redacted_still_do(self):
        """POSITIVE CONTROL for the key-list edit — widening an alternation is
        exactly the change that can silently break its neighbours."""
        # UNCONDITIONAL positive control, outside the loop.
        anchor = chat._safe_reason("api_key=%s tail" % self.PROBE_JWT)
        self.assertIn("redacted", anchor)
        self.assertNotIn(self.PROBE_JWT, anchor)
        keys = ("api_key", "token", "password", "client_secret")
        checked = 0
        for key in keys:
            out = chat._safe_reason(
                ("%s=%s " % (key, self.PROBE_JWT)) + ("x " * 300))
            self.assertNotIn(self.PROBE_JWT, out, key)
            self.assertIn("redacted", out, key)
            checked += 1
        # UNCONDITIONAL must-hit: an empty `keys` would make every assertion
        # above vacuous and the arm would still pass.
        self.assertEqual(checked, len(keys))
        self.assertEqual(checked, 4)

    def test_send_acceptance_requires_exact_true_hex_hashes_and_int_chain(self):
        good = dict(SENT)
        self.assertTrue(chat._complete_send(good))
        for patch in ({"sent": "false"}, {"sent": 1},
                      {"turn_hash": "z" * 64}, {"receipt_hash": "r" * 64},
                      {"chain_index": False}, {"chain_index": "7"}):
            bad = dict(good, **patch)
            self.assertFalse(chat._complete_send(bad), patch)

    def test_malformed_send_receipt_cannot_clear_or_mark_the_row_signed(self):
        chat._record_sign_failure("p1", chat._diag("send_failed", "existing"))
        malformed = dict(SENT, sent="false", chain_index=False)
        result = (0, json.dumps(malformed), "")
        with mock.patch.object(cellmod, "bin_status", return_value=READY_SIGNER), \
             mock.patch.object(chat, "_room_cell",
                               return_value=("c" * 64, None, True)), \
             mock.patch.object(cellmod, "run_bin", return_value=result), \
             mock.patch.object(chat, "_revive", return_value=(None, "refused")), \
             mock.patch.object(chat, "_faucet", return_value=(None, "refused")):
            row = chat.post("still degraded", who="a1", profile="p1", sign=True)
        self.assertNotIn("chain", row)
        self.assertEqual(row["transport"]["state"], "DEGRADED")
        self.assertEqual(chat.sign_failures()[0]["profile"], "p1")

    def test_malformed_incident_state_never_crashes_or_discards_valid_receipt(self):
        os.makedirs(chat.sign_failures_dir(), exist_ok=True)
        with open(chat.sign_failures_path(), "w") as f:
            json.dump({"p1": {"profile": "stale-alias", "reason": "old",
                              "_last_epoch": "bad", "failure_count": "many"},
                       "junk": [1, 2, 3]}, f)
        os.environ["HELM_CHAT_NODE_URL"] = "http://127.0.0.1:1"
        with mock.patch.object(cellmod, "bin_status", return_value=READY_SIGNER), \
             mock.patch.object(chat, "node_head", return_value={"chain_index": 1}):
            st = chat.transport_status()
        self.assertEqual((st["mode"], st["profile"], st["code"],
                          st["reason"], st["failure_count"]),
                         ("degraded", "p1", "incident_state_corrupt", "old", 1))
        with mock.patch.object(chat, "_sign_send", return_value=(dict(SENT), None)):
            row = chat.post("valid wins", who="a1", profile="p1", sign=True)
        self.assertEqual(row["chain"], 7)
        self.assertNotIn("transport", row)
        self.assertEqual(chat.sign_failures(), [])

    def test_full_profile_identity_prevents_prefix_cross_clear(self):
        a = "p" * 120 + "-a"
        b = "p" * 120 + "-b"
        chat._record_sign_failure(a, chat._diag("send_failed", "a failed"))
        chat._record_sign_failure(b, chat._diag("send_failed", "b failed"))
        self.assertEqual({f["profile"] for f in chat.sign_failures()}, {a, b})
        chat._clear_sign_failure(a)
        self.assertEqual([f["profile"] for f in chat.sign_failures()], [b])

    def test_raw_profile_key_stays_exact_while_every_projection_is_inert(self):
        raw = "seat\x1b[31m\x00\x85‮"
        clean = chat._dsan(raw)
        hostile_reason = "node\x1b[2J\x01\x85‮ down"
        chat._record_sign_failure(
            raw, chat._diag("send_failed", hostile_reason))
        chat._record_sign_failure(
            clean, chat._diag("send_failed", hostile_reason))

        with open(chat.sign_failures_path()) as f:
            state = json.load(f)
        self.assertEqual(set(state), {raw, clean})
        failures = chat.sign_failures()
        self.assertEqual(len(failures), 2)
        self.assertEqual({f["profile"] for f in failures}, {clean})

        legacy = {"ts": "2026-07-23T00:00:00Z", "from": "agent",
                  "text": "fallback",
                  "transport": {"state": "DEGRADED", "profile": raw,
                                "code": "send_failed",
                                "reason": hostile_reason}}
        projections = [chat._fmt(legacy), chat._fmt_body(legacy),
                       chat.transport_failure_summary(dict(
                           legacy["transport"], mode="degraded")),
                       json.dumps(chat.public_rows([legacy]), ensure_ascii=False)]
        for projection in projections:
            for ch in ("\x1b", "\x00", "\x01", "\x85", "‮"):
                self.assertNotIn(ch, projection)
        self.assertEqual(chat.public_rows([legacy])[0]["transport"]["profile"],
                         clean)

        self.assertTrue(chat._clear_sign_failure(raw))
        remaining = chat.sign_failures()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["profile"], clean)

    def test_incident_state_read_failure_is_publicly_degraded_until_recovery(self):
        raw = "reader\x1b[31m"
        os.environ["HELM_CELL_PROFILE"] = raw
        os.environ["HELM_CHAT_NODE_URL"] = "http://127.0.0.1:1"
        with mock.patch.object(chat.time, "time", return_value=100), \
             mock.patch.object(pk, "now_ts", return_value="persisted"):
            chat._record_sign_failure(raw, chat._diag("send_failed", "real incident"))
        path = chat.sign_failures_path()
        backup = path + ".readable"
        os.replace(path, backup)
        os.mkdir(path)  # strict owner read deterministically raises IsADirectoryError
        try:
            out = io.StringIO()
            with mock.patch.object(chat.time, "time", return_value=200), \
                 mock.patch.object(pk, "now_ts", return_value="unreadable"), \
                 mock.patch.object(cellmod, "bin_status", return_value=READY_SIGNER), \
                 mock.patch.object(chat, "node_head",
                                   return_value={"chain_index": 8}), \
                 contextlib.redirect_stdout(out):
                failures = chat.sign_failures()
                st = chat.transport_status()
                rc = chat.cmd_chat(["transport", "status"])
            self.assertEqual(failures[0]["code"], "incident_state_unreadable")
            self.assertEqual(failures[0]["profile"], chat._dsan(raw))
            self.assertEqual((st["mode"], st["code"]),
                             ("degraded", "incident_state_unreadable"))
            self.assertEqual(rc, 1)
            self.assertIn("incident state unreadable", out.getvalue())
            self.assertNotIn("\x1b", out.getvalue())
        finally:
            os.rmdir(path)
            os.replace(backup, path)

        self.assertEqual(chat.sign_failures()[0]["reason"], "real incident")
        self.assertTrue(chat._clear_sign_failure(raw, succeeded_at=300))
        self.assertEqual(chat.sign_failures(), [])
        with mock.patch.object(cellmod, "bin_status", return_value=READY_SIGNER), \
             mock.patch.object(chat, "node_head", return_value={"chain_index": 9}):
            self.assertEqual(chat.transport_status()["mode"], "signed")

    def test_write_failure_retains_exact_profiles_counts_order_and_lifecycle(self):
        raw = "writer\x1b[32m"
        other = "other"
        failures = ((100, "first", raw), (150, "other", other),
                    (200, "latest", raw))
        with mock.patch.object(
                pk, "atomic_write",
                side_effect=OSError(28, "No space left on device")) as write:
            for epoch, reason, profile in failures:
                with mock.patch.object(chat.time, "time", return_value=epoch), \
                     mock.patch.object(pk, "now_ts", return_value=str(epoch)):
                    row = chat._stamp_sign_failure(
                        {}, profile, chat._diag("send_failed", reason))
                self.assertEqual(row["transport"]["state"], "DEGRADED")
                self.assertIn("process-local fallback active",
                              row["transport"]["remediation"])
        self.assertEqual(write.call_count, 3)
        private = chat._SIGN_FAILURE_FALLBACK[
            os.path.abspath(chat.sign_failures_path())]
        self.assertIn(raw, private)
        rows = chat.sign_failures()
        self.assertEqual([r["profile"] for r in rows],
                         [chat._dsan(raw), other])
        self.assertEqual((rows[0]["reason"], rows[0]["failure_count"]),
                         ("latest", 2))
        with mock.patch.object(cellmod, "bin_status", return_value=READY_SIGNER), \
             mock.patch.object(chat, "node_head", return_value={"chain_index": 10}):
            st = chat.transport_status()
        self.assertEqual((st["mode"], st["code"], st["failure_count"]),
                         ("degraded", "send_failed", 2))

        with mock.patch.object(chat, "_sign_send", return_value=(dict(SENT), None)):
            recovered = chat.post("recovered", who="a1", profile=raw, sign=True)
        self.assertEqual(recovered["chain"], 7)
        self.assertEqual([r["profile"] for r in chat.sign_failures()], [other])
        self.assertEqual(chat.acknowledge_sign_failures(other), [other])
        self.assertEqual(chat.sign_failures(), [])

    def test_write_failure_fallback_is_concurrency_safe(self):
        import threading
        profile = "concurrent-profile"
        barrier = threading.Barrier(8)
        errors = []

        def fail(i):
            try:
                d = chat._diag("send_failed", "failure-%d" % i,
                               event_epoch=100 + i, event_ts=str(100 + i))
                barrier.wait()
                chat._stamp_sign_failure({}, profile, d)
            except Exception as exc:
                errors.append(exc)

        with mock.patch.object(
                pk, "atomic_write",
                side_effect=OSError(28, "No space left on device")):
            threads = [threading.Thread(target=fail, args=(i,)) for i in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
        self.assertEqual(errors, [])
        self.assertFalse(any(thread.is_alive() for thread in threads))
        row = chat.sign_failures()[0]
        self.assertEqual((row["profile"], row["reason"], row["failure_count"]),
                         (profile, "failure-7", 8))
        self.assertTrue(chat._clear_sign_failure(profile, succeeded_at=200))
        self.assertEqual(chat.sign_failures(), [])

    def test_lock_failure_retains_incident_until_exact_signed_success(self):
        import fcntl
        raw = "locked-profile"
        with mock.patch.object(
                fcntl, "flock", side_effect=PermissionError(13, "lock denied")):
            row = chat._stamp_sign_failure(
                {}, raw, chat._diag("join_failed", "cannot lock owner"))
        self.assertEqual((row["transport"]["profile"],
                          row["transport"]["failure_count"]), (raw, 1))
        self.assertEqual(chat.sign_failures()[0]["reason"], "cannot lock owner")
        with mock.patch.object(cellmod, "bin_status", return_value=READY_SIGNER), \
             mock.patch.object(chat, "node_head", return_value={"chain_index": 11}):
            self.assertEqual(chat.transport_status()["mode"], "degraded")
        self.assertTrue(chat._clear_sign_failure(raw))
        self.assertEqual(chat.sign_failures(), [])

    def test_configured_ready_signer_with_unreachable_node_is_degraded(self):
        os.environ["HELM_CHAT_NODE_URL"] = "http://127.0.0.1:1"
        for outcome in (None, OSError("probe exploded")):
            patch = mock.patch.object(chat, "node_head", return_value=outcome) \
                if outcome is None else mock.patch.object(
                    chat, "node_head", side_effect=outcome)
            with self.subTest(outcome=repr(outcome)), \
                 mock.patch.object(cellmod, "bin_status", return_value=READY_SIGNER), patch:
                st = chat.transport_status()
                self.assertEqual((st["mode"], st["code"]),
                                 ("degraded", "node_unreachable"))
                self.assertIn("configured chat node unreachable", st["reason"])
        out = io.StringIO()
        with mock.patch.object(cellmod, "bin_status", return_value=READY_SIGNER), \
             mock.patch.object(chat, "node_head", return_value=None), \
             contextlib.redirect_stdout(out):
            self.assertEqual(chat.cmd_chat(["transport", "status"]), 1)
        self.assertIn("DEGRADED", out.getvalue())
        self.assertIn("configured chat node unreachable", out.getvalue())

    def test_transport_ack_retires_exact_or_all_dead_profiles(self):
        chat._record_sign_failure("dead-old", chat._diag("send_failed", "gone"))
        chat._record_sign_failure("dead-two", chat._diag("join_failed", "gone"))
        with mock.patch.object(chat.time, "time", return_value=10**12):
            self.assertEqual(
                {f["profile"] for f in chat.sign_failures()},
                {"dead-old", "dead-two"})  # active incidents never age out
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(chat.cmd_chat(
                ["transport", "ack", "--profile", "dead-old"]), 0)
        self.assertIn("ACKNOWLEDGED (not recovered)", out.getvalue())
        self.assertEqual([f["profile"] for f in chat.sign_failures()], ["dead-two"])
        with contextlib.redirect_stdout(out):
            self.assertEqual(chat.cmd_chat(["transport", "ack", "--all"]), 0)
        self.assertEqual(chat.sign_failures(), [])
        # A delayed pre-ack process cannot resurrect the retired incident.
        old = chat._diag("send_failed", "late", event_epoch=1, event_ts="old")
        chat._record_sign_failure("dead-old", old)
        self.assertEqual(chat.sign_failures(), [])
        # ACK is not a permanent ignore: a genuinely later failure reopens it.
        future = chat._diag("send_failed", "new failure",
                            event_epoch=chat.time.time() + 1, event_ts="future")
        chat._record_sign_failure("dead-old", future)
        self.assertEqual(chat.sign_failures()[0]["reason"], "new failure")

    def test_failure_persists_in_ram_status_and_signed_success_clears_it(self):
        """Two unsigned fallbacks land, first/last/count/age persist, every
        surface reads DEGRADED, then the profile's signed turn is the clear
        witness. The incident owner lives only under HELM_CHAT_DIR (tmpfs in
        production), never the disk journal/state tree."""
        failure1 = {"code": "send_failed",
                    "reason": "first failure bearer abc123"}
        failure2 = {"code": "send_failed", "reason": "second failure"}
        with mock.patch.object(chat.time, "time", return_value=100), \
             mock.patch.object(pk, "now_ts", return_value="2026-07-22T10:00:00Z"), \
             mock.patch.object(chat, "_sign_send", return_value=(None, failure1)):
            one = chat.post("one", who="a1", profile="p1", sign=True)
        with mock.patch.object(chat.time, "time", return_value=160), \
             mock.patch.object(pk, "now_ts", return_value="2026-07-22T10:01:00Z"), \
             mock.patch.object(chat, "_sign_send", return_value=(None, failure2)):
            two = chat.post("two", who="a1", profile="p1", sign=True)
        self.assertEqual(chat.read()[1], 2)       # unsigned RAM delivery survives
        self.assertNotIn("chain", one)
        self.assertNotIn("chain", two)
        self.assertEqual(two["transport"]["failure_count"], 2)
        self.assertEqual(two["transport"]["first_failure"],
                         "2026-07-22T10:00:00Z")
        self.assertEqual(two["transport"]["last_failure"],
                         "2026-07-22T10:01:00Z")
        self.assertNotIn("abc123", json.dumps(one))
        self.assertTrue(chat.sign_failures_path().startswith(
            chat.SIGN_FAILURES_ROOT + os.sep))
        self.assertFalse(chat.sign_failures_path().startswith(
            os.environ["HELM_CHAT_DIR"] + os.sep))  # override cannot move state to disk
        self.assertFalse(os.path.exists(chat.journal_dir()))  # log-after stays separate
        # DECLARE the profile this test asserts on. It reads st["profile"] ==
        # "p1" and resolves per-profile failure state, and it had been
        # INHERITING that value from a sibling test's leak — green only because
        # another test forgot to clean up. Closing the leak exposed it, which
        # is the leak doing its final piece of damage on the way out.
        os.environ["HELM_CELL_PROFILE"] = "p1"
        os.environ["HELM_CHAT_NODE_URL"] = "http://127.0.0.1:1"
        with mock.patch.object(chat.time, "time", return_value=220), \
             mock.patch.object(cellmod, "bin_status", return_value=READY_SIGNER), \
             mock.patch.object(chat, "node_head",
                               return_value={"chain_index": 42}):
            st = chat.transport_status()
        self.assertEqual(st["state"], "DEGRADED")
        self.assertEqual((st["profile"], st["reason"], st["failure_count"]),
                         ("p1", "second failure", 2))
        self.assertEqual((st["age_s"], st["last_age_s"]), (120, 60))
        self.assertIn("remediation", st)
        with mock.patch.object(chat, "_sign_send", return_value=(SENT, None)):
            three = chat.post("recovered", who="a1", profile="p1", sign=True)
        self.assertEqual(three["chain"], 7)
        self.assertEqual(chat.sign_failures(), [])
        self.assertTrue(os.path.exists(chat.sign_failures_path()))  # success watermark
        with mock.patch.object(cellmod, "bin_status", return_value=READY_SIGNER), \
             mock.patch.object(chat, "node_head",
                               return_value={"chain_index": 43}):
            self.assertEqual(chat.transport_status()["mode"], "signed")

    def test_failure_state_is_per_profile(self):
        chat._record_sign_failure("p1", chat._diag("send_failed", "one"))
        chat._record_sign_failure("p2", chat._diag("join_failed", "two"))
        self.assertEqual({f["profile"] for f in chat.sign_failures()}, {"p1", "p2"})
        chat._clear_sign_failure("p1")
        self.assertEqual([f["profile"] for f in chat.sign_failures()], ["p2"])

    def test_older_signed_completion_cannot_clear_a_newer_failure(self):
        with mock.patch.object(chat.time, "time", return_value=200):
            chat._record_sign_failure("p1", chat._diag("send_failed", "later"))
        self.assertFalse(chat._clear_sign_failure("p1", succeeded_at=100))
        self.assertEqual(chat.sign_failures()[0]["reason"], "later")
        self.assertTrue(chat._clear_sign_failure("p1", succeeded_at=300))
        self.assertEqual(chat.sign_failures(), [])

    def test_delayed_older_failure_cannot_redegrade_after_success(self):
        with mock.patch.object(chat.time, "time", return_value=200):
            delayed = chat._diag("send_failed", "stale delayed failure")
        self.assertFalse(chat._clear_sign_failure("p1", succeeded_at=300))
        with mock.patch.object(chat.time, "time", return_value=400):
            row_diag = chat._record_sign_failure("p1", delayed)
        self.assertEqual(row_diag["reason"], "stale delayed failure")
        self.assertEqual(chat.sign_failures(), [])

    def test_delayed_older_failure_cannot_overwrite_newer_failure(self):
        with mock.patch.object(chat.time, "time", return_value=200):
            delayed = chat._diag("send_failed", "older")
        with mock.patch.object(chat.time, "time", return_value=300):
            chat._record_sign_failure("p1", chat._diag("join_failed", "newer"))
        with mock.patch.object(chat.time, "time", return_value=400):
            returned = chat._record_sign_failure("p1", delayed)
        state = chat.sign_failures()[0]
        self.assertEqual((state["code"], state["reason"], state["failure_count"]),
                         ("join_failed", "newer", 2))
        self.assertEqual((returned["code"], returned["reason"],
                          returned["failure_count"]),
                         ("join_failed", "newer", 2))

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
        self.assertEqual(err["code"], "signer_unavailable")
        self.assertIn("HELM_CELL_BIN is unset", err["reason"])
        rb.assert_not_called()
        rv.assert_not_called()
        with mock.patch.object(chat, "node_head") as nh, \
             mock.patch.object(chat, "_revive") as rv:
            m = chat.post("dark leg", who="a1")   # sign=None probe path
        nh.assert_not_called()                    # the signer gate comes FIRST
        rv.assert_not_called()
        self.assertNotIn("chain", m)
        self.assertIn("[unsigned]", chat._fmt(m))
        self.assertEqual(chat.sign_failures(), [])

    def _write_signer(self, path, executable=True):
        with open(path, "w") as f:
            f.write("#!/bin/sh\n"
                    "case \"$1\" in\n"
                    "join) echo '%s' ;;\n"
                    "send) echo '%s' ;;\n"
                    "esac\n" % (
                        json.dumps({"joined": True, "cell": "c" * 64}),
                        json.dumps(SENT)))
        os.chmod(path, 0o700 if executable else 0o600)

    def _assert_broken_to_fixed(self, path, reason, repair):
        os.environ["HELM_CELL_BIN"] = path
        os.environ["HELM_CELL_PROFILE"] = "p1"
        os.environ["HELM_CHAT_NODE_URL"] = "http://127.0.0.1:1"
        with mock.patch.object(chat, "node_head") as nh:
            status = chat.transport_status()
        nh.assert_not_called()
        self.assertEqual((status["mode"], status["code"], status["signer"],
                          status["signer_configured"]),
                         ("degraded", "signer_unavailable", False, True))
        self.assertIn(reason, status["reason"])
        self.assertEqual(chat.sign_failures(), [])  # status alone does not invent history

        with mock.patch.object(chat, "node_head") as nh, \
             mock.patch.object(chat, "_revive") as rv:
            first = chat.post("broken one", who="a1")
            second = chat.post("broken two", who="a1")
        nh.assert_not_called()
        rv.assert_not_called()
        self.assertEqual(first["transport"]["code"], "signer_unavailable")
        self.assertEqual(second["transport"]["failure_count"], 2)
        self.assertEqual(chat.sign_failures()[0]["failure_count"], 2)
        self.assertEqual(chat.read()[1], 2)  # RAM delivery remains service-preserving

        repair()
        with mock.patch.object(chat, "node_head",
                               return_value={"chain_index": 15}), \
             mock.patch.object(cellmod, "get_json", return_value=None):
            recovered = chat.post("fixed", who="a1")
        self.assertEqual(recovered["chain"], 7)
        self.assertEqual(chat.sign_failures(), [])
        with mock.patch.object(chat, "node_head",
                               return_value={"chain_index": 16}):
            self.assertEqual(chat.transport_status()["mode"], "signed")

    def test_configured_nonexistent_signer_fails_loud_then_recovers(self):
        path = os.path.join(self.tmp, "deleted-signer")
        self._assert_broken_to_fixed(
            path, "path does not exist", lambda: self._write_signer(path))

    def test_configured_non_executable_signer_fails_loud_then_recovers(self):
        path = os.path.join(self.tmp, "non-executable-signer")
        self._write_signer(path, executable=False)
        self._assert_broken_to_fixed(
            path, "not executable", lambda: os.chmod(path, 0o700))

    def test_executable_that_cannot_launch_is_signer_unavailable(self):
        path = os.path.join(self.tmp, "bad-exec-format")
        with open(path, "w") as f:
            f.write("not an executable format\n")
        os.chmod(path, 0o700)
        os.environ["HELM_CELL_BIN"] = path
        with mock.patch.object(chat, "_revive") as rv:
            info, failure = chat._sign_send("payload", "p1")
        self.assertIsNone(info)
        self.assertEqual(failure["code"], "signer_unavailable")
        self.assertIn("could not execute", failure["reason"])
        rv.assert_not_called()

    def test_unusable_signer_projects_as_degraded_not_off(self):
        os.environ["HELM_CELL_BIN"] = os.path.join(self.tmp, "deleted-signer")
        os.environ["HELM_CELL_PROFILE"] = "p1"
        os.environ["HELM_CHAT_NODE_URL"] = "http://127.0.0.1:1"
        status = chat.transport_status()
        self.assertEqual((status["mode"], status["code"]),
                         ("degraded", "signer_unavailable"))
        self.assertIn("DEGRADED", human.status_line(
            dict(human.model_new(), status=status), 160))

        chat_out = io.StringIO()
        with contextlib.redirect_stdout(chat_out):
            self.assertEqual(chat.cmd_chat(["transport", "status"]), 1)
        self.assertIn("signer path does not exist", chat_out.getvalue())
        self.assertNotIn("UNSIGNED", chat_out.getvalue())

        node_out, node_err = io.StringIO(), io.StringIO()
        with mock.patch.object(chatnode, "_systemctl", return_value=(0, "active")), \
             mock.patch.object(cellmod, "get_json", return_value=[]), \
             contextlib.redirect_stdout(node_out), \
             contextlib.redirect_stderr(node_err):
            self.assertEqual(chatnode._status([]), 1)
        self.assertIn("signer path does not exist", node_err.getvalue())
        self.assertNotIn("no signer", node_err.getvalue())

    def test_transport_status_reports_the_signer(self):
        """A reachable node without a signer must never read "signed" —
        the exact lying status the day review caught live: node answering,
        every row [unsigned]."""
        os.environ["HELM_CHAT_NODE_URL"] = "http://127.0.0.1:1"
        with mock.patch.object(chat, "node_head",
                               return_value={"chain_index": 15}):
            st = chat.transport_status()
            self.assertEqual((st["mode"], st["signer"],
                              st["signer_configured"], st["head"]),
                             ("unsigned (no signer)", False, False, 15))
            with mock.patch.object(cellmod, "bin_status", return_value=READY_SIGNER):
                ready = chat.transport_status()
            self.assertEqual((ready["mode"], ready["state"], ready["label"]),
                             ("ready", "READY", "ready (unproven)"))
            self.assertIn("no committed signing receipt", ready["detail"])
            self.assertIn("READY (UNPROVEN)", human.status_line(
                dict(human.model_new(), status=ready), 160))
            chat_out = io.StringIO()
            with mock.patch.object(chat, "transport_status", return_value=ready), \
                 contextlib.redirect_stdout(chat_out):
                self.assertEqual(chat.cmd_chat(["transport", "status"]), 0)
            self.assertIn("READY (UNPROVEN)", chat_out.getvalue())
            node_out = io.StringIO()
            with mock.patch.object(chat, "transport_status", return_value=ready), \
                 mock.patch.object(chatnode, "_systemctl", return_value=(0, "active")), \
                 mock.patch.object(cellmod, "get_json", return_value=[]), \
                 contextlib.redirect_stdout(node_out):
                self.assertEqual(chatnode._status([]), 0)
            self.assertIn("signing ready (unproven)", node_out.getvalue())

    def test_committed_send_is_the_exact_profile_persisted_signing_witness(self):
        path = os.path.join(self.tmp, "signer")
        self._write_signer(path)
        os.environ.update(HELM_CELL_BIN=path, HELM_CELL_PROFILE="profile-a",
                          HELM_CHAT_NODE_URL="http://127.0.0.1:1")
        with mock.patch.object(chat, "node_head",
                               return_value={"chain_index": 15}), \
             mock.patch.object(cellmod, "get_json", return_value=None):
            before = chat.transport_status()
            row = chat.post("commit witness", who="a1", profile="profile-a")
            after = chat.transport_status()
        self.assertEqual((before["mode"], before["label"]),
                         ("ready", "ready (unproven)"))
        self.assertEqual((row["chain"], after["mode"]), (7, "signed"))
        self.assertGreater(chat._signed_success_epoch("profile-a"), 0)

        os.environ["HELM_CELL_PROFILE"] = "profile-b"
        with mock.patch.object(chat, "node_head",
                               return_value={"chain_index": 16}):
            isolated = chat.transport_status()
        self.assertEqual((isolated["mode"], isolated["label"]),
                         ("ready", "ready (unproven)"))

        # Simulate a normal process reload: discard the process-local owner view;
        # the persisted exact-profile watermark still proves A and never B.
        chat._SIGN_FAILURE_FALLBACK.pop(
            os.path.abspath(chat.sign_failures_path()), None)
        os.environ["HELM_CELL_PROFILE"] = "profile-a"
        with mock.patch.object(chat, "node_head",
                               return_value={"chain_index": 17}):
            self.assertEqual(chat.transport_status()["mode"], "signed")

    def test_uncommitted_send_never_proves_signing(self):
        os.environ.update(HELM_CELL_PROFILE="profile-a",
                          HELM_CHAT_NODE_URL="http://127.0.0.1:1")
        with mock.patch.object(cellmod, "bin_status", return_value=READY_SIGNER), \
             mock.patch.object(chat, "node_head",
                               return_value={"chain_index": 15}), \
             mock.patch.object(chat, "_sign_send", return_value=(
                 None, chat._diag("send_failed", "receipt not committed"))):
            self.assertEqual(chat.transport_status()["mode"], "ready")
            row = chat.post("failed witness", who="a1", profile="profile-a",
                            sign=True)
            status = chat.transport_status()
        self.assertNotIn("chain", row)
        self.assertEqual((status["mode"], status["code"]),
                         ("degraded", "send_failed"))
        self.assertEqual(chat._signed_success_epoch("profile-a"), 0)

        chat.acknowledge_sign_failures("profile-a")
        malformed = dict(SENT, receipt_hash="not-a-receipt")
        with mock.patch.object(chat, "_sign_send", return_value=(malformed, None)):
            row = chat.post("malformed witness", who="a1", profile="profile-b",
                            sign=True)
        self.assertNotIn("chain", row)
        self.assertEqual(row["transport"]["state"], "DEGRADED")
        self.assertEqual(chat._signed_success_epoch("profile-b"), 0)

    def test_room_cell_cache_hits_without_binary(self):
        os.environ["HELM_CHAT_NODE_URL"] = "http://127.0.0.1:1"
        chat._ensure_dir()
        with open(chat.cells_path(), "w") as f:
            json.dump({"me": "d" * 64}, f)
        # 3-tuple contract: (cell_hex, err, launched) — cache hit is success
        self.assertEqual(chat._room_cell("me", ""), ("d" * 64, None, True))

    def test_a_FLEET_surface_reports_the_FLEET_not_its_own_process(self):
        """OWNER-CAUGHT ON THE OWNER'S SURFACE, 2026-07-29: "hm does the webui
        need retarting or something?" The web ledger read "signing ready
        (unproven)" while codex, ds4pro, gemini and opus-integrator all carried
        committed receipts and rows were anchoring with real turn hashes.

        Nothing was stale. The panel asked `_signed_success_epoch(profile_name())`
        — about the WEB PROCESS's own profile, which is helm-agent, and no seat
        ever signs as helm-agent. An exact-profile question on a fleet panel can
        only ever answer "unproven", however well the fleet signs.

        A status panel describes the SYSTEM, not the process rendering it."""
        from helm import cell as cellmod
        os.environ["HELM_CHAT_NODE_URL"] = "http://127.0.0.1:1"
        with mock.patch.object(cellmod, "bin_status", return_value=READY_SIGNER), \
             mock.patch.object(chat, "node_head", return_value={"chain_index": 7}), \
             mock.patch.object(chat, "sign_failures", return_value=[]), \
             mock.patch.object(chat, "fleet_signed",
                               return_value=("ds4pro", 1785348086.0)), \
             mock.patch.object(chat, "_signed_success_epoch", return_value=0.0):
            fleet = chat.transport_status(fleet=True)
        self.assertEqual(fleet.get("fleet_signer"), "ds4pro",
                         "fleet view must name the profile it borrowed")
        self.assertIn("fleet", (fleet.get("label") or ""))

    def test_a_seat_asking_about_ITSELF_is_never_told_the_fleet_is_fine(self):
        """The counterfactual, and the reason `fleet` is opt-in rather than the
        default. Being told signing works when YOU cannot sign is precisely the
        failure this projection exists to prevent — a seat would stop reporting
        a real local outage."""
        with mock.patch.object(chat, "fleet_signed",
                               return_value=("ds4pro", 1785348086.0)), \
             mock.patch.object(chat, "_signed_success_epoch", return_value=0.0):
            own = chat.transport_status()
        self.assertNotIn("fleet", (own.get("label") or ""),
                         "the per-profile answer must stay per-profile")
        self.assertIsNone(own.get("fleet_signer"))

    def test_fleet_signed_picks_the_NEWEST_receipt(self):
        with mock.patch.object(chat, "_read_sign_failure_state", return_value={
                "old": {"_success_epoch": 100.0},
                "new": {"_success_epoch": 900.0},
                "never": {"_success_epoch": 0},
                "junk": "not-a-dict"}):
            self.assertEqual(chat.fleet_signed(), ("new", 900.0))

    def test_fleet_signed_is_None_when_nobody_has_ever_signed(self):
        with mock.patch.object(chat, "_read_sign_failure_state", return_value={
                "a": {"_success_epoch": 0}}):
            self.assertIsNone(chat.fleet_signed())

    def test_the_room_join_is_COORDINATION_EXEMPT_and_asks_for_no_funding(self):
        """--fund 0 or the transport wedges. THE TWENTY-HOUR BUG, 2026-07-29.

        Joining with no --fund makes the signer default to asking the faucet
        for 5000 computrons, and the faucet rate-limits to ONE REQUEST PER CELL
        PER MINUTE while the client waits TEN SECONDS. The client's patience is
        shorter than the server's minimum retry interval, so a contended faucet
        can NEVER be satisfied — the join is not slow, it cannot complete. One
        such failure then latched the transport DEGRADED for a day, long after
        the rate limit expired sixty seconds later.

        A room cell never needs a balance: every turn it emits is coordination
        (EmitEvent only, no balance_change), which helm already declares at fee
        0. Funding it was requesting money to pay a bill of zero.

        MEASURED after the fix: join returns rc 0 / joined true / materialized
        false without touching the faucet, and a posted row carried chain 3,
        a real turn hash and a receipt where an unsigned row carries None."""
        seen = {}

        def fake_run_bin(args, timeout=None, env_extra=None):
            seen["args"] = list(args)
            return 0, json.dumps({"cell": "a" * 64}), ""

        os.environ["HELM_CHAT_NODE_URL"] = "http://127.0.0.1:1"
        chat._ensure_dir()
        with open(chat.cells_path(), "w") as f:
            json.dump({}, f)
        from helm import cell as cellmod
        with mock.patch.object(cellmod, "run_bin", fake_run_bin):
            chat._room_cell("fresh-profile", "")
        self.assertIn("--fund", seen["args"],
                      "a room join that omits --fund defaults to 5000 and "
                      "walks into the faucet rate-limit trap")
        self.assertEqual(seen["args"][seen["args"].index("--fund") + 1], "0")

    def test_the_join_funding_stays_overridable_for_a_non_exempt_node(self):
        """A node that has NOT opted into the coordination-exempt class still
        needs a funded cell. Same leash and same escape hatch as
        HELM_NODE_COORD_FEE on the anchor path."""
        seen = {}

        def fake_run_bin(args, timeout=None, env_extra=None):
            seen["args"] = list(args)
            return 0, json.dumps({"cell": "b" * 64}), ""

        os.environ["HELM_CHAT_NODE_URL"] = "http://127.0.0.1:1"
        os.environ["HELM_CHAT_JOIN_FUND"] = "5000"
        try:
            chat._ensure_dir()
            with open(chat.cells_path(), "w") as f:
                json.dump({}, f)
            from helm import cell as cellmod
            with mock.patch.object(cellmod, "run_bin", fake_run_bin):
                chat._room_cell("other-profile", "")
            self.assertEqual(
                seen["args"][seen["args"].index("--fund") + 1], "5000")
        finally:
            os.environ.pop("HELM_CHAT_JOIN_FUND", None)


class PostsSignThroughTheIdentityGateTest(V2Base):
    """A post and a reaction pick their signer through `cell.signing_identity`,
    the gate the coordination emit already used.

    The failure it pins: derived_seat() names the seat, the ambient
    HELM_CELL_PROFILE names the owner, and signing_identity() refuses, yet a
    post path that reads `cell.profile_name()` signs the row with the owner's
    key.

    The fixture is the one tests/test_cell.py SigningIdentityTest uses: patch
    `meld._self_seat` and the strict roster read `seats.roster_checked`, set
    the ambient profile. The signer is configured and the node answers, so
    `_sign_send` IS reachable on the production `sign=None` path, and a
    refusal's zero calls mean something.

    NOT EVERY ARM HERE IS A FALSIFIER. The arms named for a refusal (a
    borrowed profile, an unreadable roster, a raising gate, a status surface
    reading SIGNED) are the ones red on the code they cure; the arms named
    for a seat signing as itself, an explicit profile, the owner's own
    session, the anonymous label and configured-off are CONTROLS, green on
    both sides, which is what makes the refusals' zero calls discriminating.
    """

    SEAT = "seat-a"
    OTHER = "seat-b"
    OWNER = "owner-profile"
    ACTOR = {"seat-a": {"home_room": "helm"},
             "seat-b": {"home_room": "helm"}}
    OBSERVED = {"observer-session": {"cwd": "/tmp/x"}}   # sid, no home_room

    @contextlib.contextmanager
    def _world(self, seat, roster, ambient, declared=True):
        """This process names itself `seat`, the ambient profile is `ambient`
        (None: no profile var at all), and the signed transport is on.
        `roster` is the strict roster read: a dict (read cleanly), an
        exception (the read raises), or None (read the REAL file under this
        test's chat dir). `declared` exports the name as HELM_CHAT_NAME, as a
        launched seat does; False models a name `derive_seat` invented, as for
        the owner's ad-hoc session. Yields the `_sign_send` mock."""
        os.environ["HELM_CHAT_NODE_URL"] = "http://127.0.0.1:1"
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ))
            if declared and seat:
                os.environ["HELM_CHAT_NAME"] = seat
            stack.enter_context(
                mock.patch("helm.meld._self_seat", return_value=seat))
            if isinstance(roster, BaseException):
                stack.enter_context(mock.patch(
                    "helm.seats.roster_checked", side_effect=roster))
            elif roster is not None:
                stack.enter_context(mock.patch(
                    "helm.seats.roster_checked", return_value=(roster, False)))
            stack.enter_context(mock.patch.object(
                cellmod, "bin_status", return_value=READY_SIGNER))
            stack.enter_context(mock.patch.object(
                chat, "node_head", return_value={"chain_index": 1}))
            ss = stack.enter_context(mock.patch.object(
                chat, "_sign_send", return_value=(dict(SENT), None)))
            for k in cellmod.PROFILE_ENV:
                os.environ.pop(k, None)
            if ambient:
                os.environ["HELM_CELL_PROFILE"] = ambient
            yield ss

    def _assert_unsigned_as(self, m, code):
        """The row landed unsigned, stamped `code` under the SEAT's name."""
        self.assertNotIn("chain", m)
        self.assertEqual(
            (m["transport"]["state"], m["transport"]["code"],
             m["transport"]["profile"]), ("DEGRADED", code, self.SEAT))

    def test_a_SEAT_under_the_OWNERS_ambient_profile_posts_UNSIGNED(self):  # noqa: VACUOUS_ASSERTION — the positive control on this same fixture is test_the_same_seat_under_its_OWN_profile_signs_as_itself, where _sign_send is called once as the seat; here the stamp's code, label and reason are asserted positively
        """THE BUG. On trunk this row is signed with the owner's profile."""
        with self._world(self.SEAT, self.ACTOR, self.OWNER) as ss:
            m = chat.post("from the seat", who=self.SEAT)
        ss.assert_not_called()
        self.assertEqual(chat.read()[1], 1, "the row must still land")
        self.assertNotIn("chain", m)
        t = m["transport"]
        self.assertEqual((t["state"], t["code"]),
                         ("DEGRADED", "identity_conflict"))
        # The incident is filed under the seat that wrote the row, never under
        # the profile the gate rejected.
        self.assertEqual(t["profile"], self.SEAT)
        self.assertIn(self.SEAT, t["reason"])
        self.assertIn(self.OWNER, t["reason"])
        self.assertIn("helm launch", t["remediation"])
        self.assertEqual([f["profile"] for f in chat.sign_failures()],
                         [self.SEAT])
        self.assertIn("[DEGRADED %s/identity_conflict" % self.SEAT,
                      chat._fmt(m))

    def test_the_FORCED_sign_seam_asks_the_same_gate(self):  # noqa: VACUOUS_ASSERTION — the stamp's code and label are asserted positively on the same row, and the called-signer control on this fixture is test_the_same_seat_under_its_OWN_profile_signs_as_itself
        """`sign=True` skips the signer and node probes, not the gate."""
        with self._world(self.SEAT, self.ACTOR, self.OWNER) as ss:
            m = chat.post("forced", who=self.SEAT, sign=True)
        ss.assert_not_called()
        self.assertEqual((m["transport"]["code"], m["transport"]["profile"]),
                         ("identity_conflict", self.SEAT))

    def test_the_same_seat_under_its_OWN_profile_signs_as_itself(self):
        """Control for the arm above: same seat, same transport, an ambient
        profile that agrees. The signer IS reached, as the seat."""
        with self._world(self.SEAT, self.ACTOR, self.SEAT) as ss:
            m = chat.post("from the seat", who=self.SEAT)
        ss.assert_called_once_with(chat.digest_payload("from the seat"),
                                   self.SEAT)
        self.assertEqual(m["chain"], 7)
        self.assertNotIn("transport", m)

    def test_an_EXPLICIT_profile_still_wins_over_the_conflict(self):
        """Stated intent is not an inherited accident: the same conflicted
        world, with an explicit profile, signs as that profile."""
        with self._world(self.SEAT, self.ACTOR, self.OWNER) as ss:
            m = chat.post("on behalf", who=self.SEAT, profile=self.OTHER)
        ss.assert_called_once_with(chat.digest_payload("on behalf"),
                                   self.OTHER)
        self.assertEqual(m["chain"], 7)

    def test_the_OWNERS_OWN_SESSION_keeps_signing_as_the_ambient_profile(self):
        """The case the gate's docstring says must not break: a rostered
        session with no home_room is not a signing identity, so the owner's
        ambient profile stands."""
        with self._world("observer-session", self.OBSERVED, self.OWNER,
                         declared=False) as ss:
            m = chat.post("from the owner", who="observer-session")
        ss.assert_called_once_with(chat.digest_payload("from the owner"),
                                   self.OWNER)
        self.assertEqual(m["chain"], 7)

    def test_no_profile_and_no_seat_still_signs_as_the_anonymous_label(self):  # noqa: VACUOUS_ASSERTION — the None is the fixture's precondition (no ambient profile); the subject is asserted positively by assert_called_once_with and the chain
        """The gate refuses this case too, but it is not a conflict: nobody's
        name is claimed. It keeps the label it signed with before."""
        with self._world("", {}, None) as ss:
            self.assertIsNone(cellmod.signer_profile()[0])
            m = chat.post("from a service", who="dispatches")
        ss.assert_called_once_with(chat.digest_payload("from a service"),
                                   "helm-agent")
        self.assertEqual(m["chain"], 7)

    def test_a_REACTION_goes_through_the_same_gate(self):  # noqa: VACUOUS_ASSERTION — the arm opens with its own positive control (an agreeing seat's reaction reaches the signer), and the refused reaction's stamp is asserted positively
        chat.post("target", who="a1")
        # positive control on the same fixture: an agreeing seat's reaction
        # reaches the signer, as the seat
        with self._world(self.SEAT, self.ACTOR, self.SEAT) as ss:
            row, err = chat.react(1, ":tada:", who=self.SEAT)
        self.assertIsNone(err)
        ss.assert_called_once_with(mock.ANY, self.SEAT)
        self.assertEqual(row["chain"], 7)
        with self._world(self.SEAT, self.ACTOR, self.OWNER) as ss:
            row, err = chat.react(1, ":fire:", who=self.SEAT)
        self.assertIsNone(err)
        ss.assert_not_called()
        self.assertNotIn("chain", row)
        self.assertEqual((row["transport"]["code"], row["transport"]["profile"]),
                         ("identity_conflict", self.SEAT))

    def test_a_named_seat_whose_roster_read_RAISES_posts_UNSIGNED(self):  # noqa: VACUOUS_ASSERTION — the stamp's state, code and label are asserted positively on the same row; the called-signer control on this fixture is test_an_unreadable_roster_still_signs_a_seat_as_ITSELF
        """An unreadable identity is not permission: the ambient owner profile
        must not stand while the roster cannot say who this seat is."""
        with self._world(self.SEAT, OSError("EIO"), self.OWNER) as ss:
            m = chat.post("roster raised", who=self.SEAT)
        ss.assert_not_called()
        self._assert_unsigned_as(m, "identity_unreadable")
        self.assertIn(self.OWNER, m["transport"]["reason"])
        self.assertIn("roster", m["transport"]["remediation"])

    def test_a_named_seat_over_a_CORRUPT_roster_file_posts_UNSIGNED(self):  # noqa: VACUOUS_ASSERTION — the stamp's state, code and label are asserted positively on the same row; the called-signer control on the same real-file read is test_a_MISSING_roster_file_leaves_the_owners_session_signing
        """The realistic spelling: `seats.roster()` answers {} for a corrupt
        file, which read as "no actor" and let the owner's profile stand."""
        from helm import seats
        os.makedirs(os.path.dirname(seats.roster_path()), exist_ok=True)
        with open(seats.roster_path(), "w", encoding="utf-8") as f:
            f.write('{"seat-a": {"home_room": ')          # torn write
        with self._world(self.SEAT, None, self.OWNER) as ss:
            m = chat.post("roster torn", who=self.SEAT)
        ss.assert_not_called()
        self._assert_unsigned_as(m, "identity_unreadable")

    def test_an_unreadable_roster_still_signs_a_seat_as_ITSELF(self):
        """Control: an ambient profile that IS the named seat claims nobody
        else, so it needs no roster to be safe and keeps signing."""
        with self._world(self.SEAT, OSError("EIO"), self.SEAT) as ss:
            m = chat.post("roster raised", who=self.SEAT)
        ss.assert_called_once_with(chat.digest_payload("roster raised"),
                                   self.SEAT)
        self.assertEqual(m["chain"], 7)

    def test_a_MISSING_roster_file_leaves_the_owners_session_signing(self):  # noqa: VACUOUS_ASSERTION — the file's absence is the fixture's precondition; the subject is asserted positively by assert_called_once_with and the chain
        """Control on the real-file read: a fresh box has no roster, which is
        a proven empty one, so the owner's own session still signs."""
        from helm import seats
        self.assertFalse(os.path.exists(seats.roster_path()))
        with self._world("observer-session", None, self.OWNER,
                         declared=False) as ss:
            m = chat.post("fresh box", who="observer-session")
        ss.assert_called_once_with(chat.digest_payload("fresh box"),
                                   self.OWNER)
        self.assertEqual(m["chain"], 7)

    def test_a_RAISING_gate_files_its_failure_under_the_SEAT(self):  # noqa: VACUOUS_ASSERTION — the stamp's state, code and label are asserted positively; the called-signer control on this fixture is test_the_same_seat_under_its_OWN_profile_signs_as_itself
        """The label exists before the gate runs, so a gate that raises files
        the failure under the seat, not under `helm-agent` (which any
        no-profile process's next good turn would clear) nor the owner."""
        with self._world(self.SEAT, self.ACTOR, self.OWNER) as ss, \
                mock.patch.object(cellmod, "signing_identity",
                                  side_effect=RuntimeError("gate blew up")):
            m = chat.post("gate raised", who=self.SEAT)
        ss.assert_not_called()
        self._assert_unsigned_as(m, "signing_exception")
        self.assertIn("gate blew up", m["transport"]["reason"])

    def test_one_post_takes_ONE_seat_reading(self):  # noqa: VACUOUS_ASSERTION — the reading count and the stamp's state, code and label are asserted positively; the called-signer control on this fixture is test_the_same_seat_under_its_OWN_profile_signs_as_itself
        """The label and the decision come from ONE seat reading. A second
        reading of a roster that changed in between filed the row under
        `helm-agent`; here the second answer would say "no seat"."""
        with self._world(self.SEAT, self.ACTOR, self.OWNER) as ss, \
                mock.patch.object(cellmod, "seat_reading", side_effect=[
                    (self.SEAT, ""), ("", "")]) as sr:
            m = chat.post("one read", who=self.SEAT)
        self.assertEqual(sr.call_count, 1)
        ss.assert_not_called()
        self._assert_unsigned_as(m, "identity_conflict")

    def test_a_conflicted_seats_STATUS_is_DEGRADED_not_the_owners_SIGNED(self):
        """The status surface asks the same gate as the post. The owner holds a
        committed receipt; the seat must not read SIGNED on it, before its
        first post or after `transport ack` retires its incident."""
        with self._world(self.SEAT, self.ACTOR, self.OWNER):
            chat._clear_sign_failure(self.OWNER)
            self.assertTrue(chat._signed_success_epoch(self.OWNER))
            readings = [chat.transport_status()]
            chat.post("refused", who=self.SEAT)
            readings.append(chat.transport_status())
            self.assertEqual(chat.acknowledge_sign_failures(self.SEAT),
                             [self.SEAT])
            readings.append(chat.transport_status())
        # .get, so a SIGNED reading (which carries no code) fails by naming
        # its state instead of dying on a KeyError
        self.assertEqual(
            [(st.get("state"), st.get("code"), st.get("profile"))
             for st in readings],
            [("DEGRADED", "identity_conflict", self.SEAT)] * 3)
        self.assertEqual([st["failed_profiles"][0]["profile"]
                          for st in readings], [self.SEAT] * 3)

    def test_a_seat_signing_as_ITSELF_reads_its_OWN_receipt(self):
        """Control: an agreeing seat is keyed on its own profile. The owner's
        receipt leaves it READY; only its own receipt makes it SIGNED."""
        with self._world(self.SEAT, self.ACTOR, self.SEAT):
            chat._clear_sign_failure(self.OWNER)
            self.assertEqual(chat.transport_status()["state"], "READY")
            chat._clear_sign_failure(self.SEAT)
            self.assertEqual(chat.transport_status()["state"], "SIGNED")

    def test_a_FLEET_reader_skips_its_own_identity(self):
        """A fleet panel describes the system. The process rendering it being a
        conflicted seat is not a fleet incident; the seat's retained incident
        is, once it has posted."""
        with self._world(self.SEAT, self.ACTOR, self.OWNER):
            chat._clear_sign_failure(self.OWNER)
            st = chat.transport_status(fleet=True)
            self.assertEqual((st["state"], st.get("fleet_signer")),
                             ("SIGNED", self.OWNER))
            chat.post("refused", who=self.SEAT)
            st = chat.transport_status(fleet=True)
        self.assertEqual((st.get("state"), st.get("code"), st.get("profile")),
                         ("DEGRADED", "identity_conflict", self.SEAT))

    def test_an_UNDECLARED_name_over_a_torn_roster_heals_with_NO_ack(self):  # noqa: VACUOUS_ASSERTION — every observable is asserted positively: the refusal's code and label, the repaired post's chain, and both status readings equal to SIGNED
        """HELM_CHAT_NAME unset (the owner's own ad-hoc session): the name is
        an invented auto-name nothing will ever sign as. The refusal during a
        torn roster must be filed where the next good turn retires it, so a
        repaired roster reads SIGNED again without a manual ack."""
        from helm import seats
        path = seats.roster_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"seat-a": {"home_room": ')          # torn write
        with self._world("derived-auto-name", None, self.OWNER,
                         declared=False) as ss:
            m = chat.post("torn", who="derived-auto-name")
            ss.assert_not_called()
            self.assertEqual(m["transport"]["code"], "identity_unreadable")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{}")                              # repaired
            again = chat.post("repaired", who="derived-auto-name")
            self.assertEqual(again["chain"], 7)
            states = (chat.transport_status().get("state"),
                      chat.transport_status(fleet=True).get("state"))
        self.assertEqual(states, ("SIGNED", "SIGNED"))
        self.assertEqual(m["transport"]["profile"], self.OWNER)

    def test_the_EMIT_path_names_an_unreadable_roster_as_such(self):  # noqa: VACUOUS_ASSERTION — both refusals' codes are asserted positively; the called-signer control on this fixture is test_the_same_seat_under_its_OWN_profile_signs_as_itself
        """A land or claim receipt refused on an unreadable roster says so,
        with the roster remedy, not the conflict's relaunch."""
        with self._world(self.SEAT, self.ACTOR, self.OWNER) as ss:
            _info, conflict = chat.emit_coordination_turn("helm.land", "x")
        self.assertEqual(conflict["code"], "identity_conflict")
        with self._world(self.SEAT, OSError("EIO"), self.OWNER) as ss:
            info, d = chat.emit_coordination_turn("helm.land", "x")
        ss.assert_not_called()
        self.assertIsNone(info)
        self.assertEqual(d["code"], "identity_unreadable")
        self.assertIn("roster", d["remediation"])

    def test_an_ACK_is_not_the_fleets_newest_receipt(self):
        """`transport ack` writes a watermark, not a receipt. The fleet panel
        must name the newest profile that SIGNED, not the newest one acked."""
        with self._world(self.SEAT, self.ACTOR, self.SEAT):
            chat._clear_sign_failure(self.OWNER)
            chat._record_sign_failure("seat-z", chat._diag("send_failed", "x"))
            self.assertEqual(chat.acknowledge_sign_failures("seat-z"),
                             ["seat-z"])
            st = chat.transport_status(fleet=True)
        self.assertEqual((st.get("state"), st.get("fleet_signer")),
                         ("SIGNED", self.OWNER))

    def test_a_conflict_with_signing_CONFIGURED_OFF_raises_no_incident(self):  # noqa: VACUOUS_ASSERTION — the same conflicted fixture with the transport ON stamps an incident in test_a_SEAT_under_the_OWNERS_ambient_profile_posts_UNSIGNED, so the absence here discriminates; the row landing is asserted positively
        """The gate is asked only where a signature would be attempted. No
        node URL means v1 unsigned transport: the row is untouched."""
        with self._world(self.SEAT, self.ACTOR, self.OWNER) as ss:
            os.environ["HELM_CHAT_NODE_URL"] = ""
            m = chat.post("transport off", who=self.SEAT)
        ss.assert_not_called()
        self.assertEqual(chat.read()[1], 1)
        self.assertNotIn("transport", m)
        self.assertEqual(chat.sign_failures(), [])


class OwnerExportPostsSignAsTheSeatTest(V2Base):
    """task/3049 on the post, emit and status paths, over a REAL roster file
    and actor store (no mocked `_self_seat`): a seat that inherited the
    owner's profile from his shell signs its rows as ITSELF once the identity
    layer admits it, the CLI door's admission is threaded rather than re-run,
    and no read verb writes the actor store."""

    OWNER = "owner-profile"
    SID = "sid-seat-a-0000-0001"
    VARS = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_ACTORS", "MELD_ACTORS",
            "HELM_CHAT_OWNER_NAMES", "MELD_CHAT_OWNER_NAMES")

    @contextlib.contextmanager
    def _world(self, name="seat-a", sid=SID, ambient=OWNER):
        from helm import seats
        os.environ["HELM_CHAT_NODE_URL"] = "http://127.0.0.1:1"
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ))
            for k in self.VARS + cellmod.PROFILE_ENV:
                os.environ.pop(k, None)
            os.environ["HELM_CHAT_OWNER_NAMES"] = self.OWNER
            os.environ["HELM_CELL_PROFILE"] = ambient
            if name:
                os.environ["HELM_CHAT_NAME"] = name
            if sid:
                os.environ["CLAUDE_CODE_SESSION_ID"] = sid
            path = seats.roster_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"seat-a": {"session": self.SID,
                                      "sessions": [self.SID],
                                      "home_room": "helm"}}, f)
            stack.enter_context(mock.patch.object(
                cellmod, "bin_status", return_value=READY_SIGNER))
            stack.enter_context(mock.patch.object(
                chat, "node_head", return_value={"chain_index": 1}))
            yield stack.enter_context(mock.patch.object(
                chat, "_sign_send", return_value=(dict(SENT), None)))

    def _store(self):
        from helm import actors
        return actors.store_path()

    def test_a_post_with_no_admitted_caller_signs_as_the_SEAT(self):  # noqa: VACUOUS_ASSERTION — the signer call, from and chain are asserted positively; the store absence is the no-write pin, and test_the_CLI_doors_admission_is_THREADED_not_re_run writes that store unconditionally through resolve_speaker
        """A hook or machine caller hands no capability down, so the gate asks
        the identity layer itself — and writes nothing. RED before: the row
        landed DEGRADED identity_conflict and the signer was never called."""
        with self._world() as ss:
            m = chat.post("from the seat", room="main")
            self.assertFalse(os.path.exists(self._store()),
                             "a post wrote the actor store through the gate")
        ss.assert_called_once_with(chat.digest_payload("from the seat"),
                                   "seat-a")
        self.assertEqual((m["from"], m["chain"]), ("seat-a", 7))
        self.assertNotIn("transport", m)

    def test_the_CLI_doors_admission_is_THREADED_not_re_run(self):  # noqa: VACUOUS_ASSERTION — both read counts come from one wrapped reader that the agreeing control post must hit, and the signer call and chain are asserted positively
        """`_seat_actor` already admitted this process; the post hands that
        capability to the gate, which asks the identity layer nothing more.
        The CONTROL is the same post from the same seat under its OWN profile
        (no swap branch at all): the owner-export post takes exactly as many
        strict roster reads, so the swap adds none on the CLI path."""
        from helm import actors, seats
        counts = []
        for ambient in ("seat-a", self.OWNER):
            with self._world(ambient=ambient) as ss:
                actor, err = actors.resolve_speaker(self.SID)
                self.assertIsNone(err)
                with mock.patch("helm.actors.admitted_name",
                                side_effect=AssertionError("re-ran admission")), \
                        mock.patch("helm.seats.roster_checked",
                                   wraps=seats.roster_checked) as reads:
                    m = chat.post("threaded", room="main", who=actor)
                counts.append(reads.call_count)
            ss.assert_called_once_with(chat.digest_payload("threaded"),
                                       "seat-a")
            self.assertEqual(m["chain"], 7)
        self.assertEqual(counts[1], counts[0], counts)

    def test_a_REACTION_from_the_seat_signs_as_the_seat(self):
        chat.post("target", room="main", who="a1")
        with self._world() as ss:
            row, err = chat.react(1, ":tada:", room="main")
        self.assertIsNone(err)
        ss.assert_called_once_with(mock.ANY, "seat-a")
        self.assertEqual(row["chain"], 7)

    def test_the_EMIT_path_signs_as_the_seat(self):  # noqa: VACUOUS_ASSERTION — the signer call is asserted positively (called with the seat); the None is the absence of a refusal on that same call
        """Land and claim receipts ride the same gate. RED before: refused."""
        with self._world() as ss:
            info, failure = chat.emit_coordination_turn("helm.land", "x")
        self.assertIsNone(failure)
        self.assertEqual(ss.call_args.args[1], "seat-a")

    def test_a_retained_conflict_retires_on_the_seats_first_signed_post(self):
        """The DEGRADED incident the old refusal filed under the seat clears
        the moment the seat signs as itself — no manual ack."""
        with self._world():
            chat._record_sign_failure(
                "seat-a", chat._diag("identity_conflict", "old refusal"))
            self.assertEqual([f["profile"] for f in chat.sign_failures()],
                             ["seat-a"])
            chat.post("signs now", room="main")
            self.assertEqual(chat.sign_failures(), [])
            self.assertEqual(chat.transport_status()["state"], "SIGNED")

    def test_STATUS_reads_never_write_the_actor_store(self):  # noqa: VACUOUS_ASSERTION — the state READY is asserted positively; the store absence is the no-write pin, and test_the_CLI_doors_admission_is_THREADED_not_re_run writes that store unconditionally
        """`transport_status` is behind `helm doctor`, the owner TUI and the
        web panels. With no actor record yet, neither the process's own
        status nor a fleet read may mint one, and a fleet read never asks the
        identity layer at all. RED before on the state: DEGRADED."""
        with self._world():
            st = chat.transport_status()
            with mock.patch("helm.actors.admitted_name",
                            side_effect=AssertionError("fleet admitted")):
                fleet = chat.transport_status(fleet=True)
            self.assertFalse(os.path.exists(self._store()),
                             "a status read wrote the actor store")
        self.assertEqual(st["state"], "READY")
        self.assertNotEqual(fleet.get("code"), "identity_conflict")

    def test_an_unadmitted_seat_still_posts_UNSIGNED_under_its_own_name(self):  # noqa: VACUOUS_ASSERTION — the stamp code, label and reason are asserted positively; the called-signer control on this fixture is test_a_post_with_no_admitted_caller_signs_as_the_SEAT
        """Control on the same world: no session, so the declared name is
        uncorroborated and the owner's profile is NOT set aside."""
        with self._world(sid=None) as ss:
            m = chat.post("no session", room="main")
        ss.assert_not_called()
        self.assertEqual((m["transport"]["code"], m["transport"]["profile"]),
                         ("identity_conflict", "seat-a"))
        self.assertIn("set aside", m["transport"]["reason"])


class DreggSignerBinTest(V2Base):
    """The dregg-native signer (dregg-client-sign) as HELM_CELL_BIN: the
    signing leg must drive its argv contract (join/send --profile --to
    --topic <payload>) and its env contract (DREGG_NODE_URL/DREGG_API_TOKEN
    aimed at the ROOM node), and parse its receipt JSON into the row. A FAKE
    bin echoes the contract — the real bin and the real cave never run in
    tests."""

    JOIN = {"joined": True, "cell": "c" * 64, "public_key": "e" * 64,
            "profile": "p1", "materialized": True}
    SEND = {"sent": True, "turn_hash": "a" * 64, "receipt_hash": "b" * 64,
            "chain_index": 9, "agent_cell": "c" * 64, "topic": "helm.chat",
            "finality": "final", "consensus_final": True}

    def fake_signer(self):
        log = os.path.join(self.tmp, "signer.log")
        path = os.path.join(self.tmp, "dregg-client-sign")
        with open(path, "w") as f:
            f.write("#!/bin/sh\n"
                    '{ echo "argv:$@"\n'
                    '  echo "DREGG_NODE_URL=$DREGG_NODE_URL"\n'
                    '  echo "DREGG_API_TOKEN=$DREGG_API_TOKEN"; } >> "%s"\n'
                    'case "$1" in\n'
                    "join) echo '%s' ;;\n"
                    "send) echo '%s' ;;\n"
                    "esac\n" % (log, json.dumps(self.JOIN), json.dumps(self.SEND)))
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        os.environ["HELM_CELL_BIN"] = path
        return log

    def test_sign_send_drives_the_dregg_contract_into_the_row(self):
        log = self.fake_signer()
        os.environ["HELM_CHAT_NODE_URL"] = "http://127.0.0.1:1"
        chat._ensure_dir()
        with open(chat._token_path(), "w") as f:
            f.write("tok-abc")           # the RAM-side node token cache
        m = chat.post("hello", who="a1", profile="p1", sign=True)
        # the receipt parsed into the row: {sent,turn_hash,receipt_hash,chain_index}
        self.assertEqual(m["turn"], "a" * 64)
        self.assertEqual(m["receipt"], "b" * 64)
        self.assertEqual(m["chain"], 9)
        self.assertNotIn("[unsigned]", chat._fmt(m))
        with open(log) as f:
            body = f.read()
        # argv contract: idempotent join, then the signed self-write send
        self.assertIn("argv:join --profile p1", body)
        self.assertIn("argv:send --profile p1 --to %s --topic %s %s"
                      % ("c" * 64, chat.CHAT_TOPIC,
                         chat.digest_payload("hello")), body)
        # env contract: the signer is aimed at the ROOM node with its token
        self.assertIn("DREGG_NODE_URL=http://127.0.0.1:1", body)
        self.assertIn("DREGG_API_TOKEN=tok-abc", body)

    def test_signer_missing_sent_falls_back_unsigned(self):
        # rc 0 without a receipt proves neither commit nor rejection: do not replay.
        self.SEND = {"ok": True}
        log = self.fake_signer()
        os.environ["HELM_CHAT_NODE_URL"] = "http://127.0.0.1:1"
        with mock.patch.object(chat, "_revive",
                               return_value=(None, "revive unavailable")), \
             mock.patch.object(chat, "_faucet",
                               return_value=(None, "faucet refused")):
            m = chat.post("hello", who="a1", sign=True)
        self.assertNotIn("chain", m)
        self.assertIn("[DEGRADED", chat._fmt(m))
        self.assertEqual(m["transport"]["code"], "send_outcome_unknown")
        self.assertEqual(chat.read()[1], 1)   # the message never dies
        with open(log) as f:
            self.assertEqual(f.read().count("argv:send "), 1)


class SignerRefusalFixtureTest(V2Base):
    """A REAL fixture signer script as HELM_CELL_BIN, run through run_bin and
    post(): the node's explicit refusal must reach the row, the incident and
    the rendered line, and every other exit must stay UNKNOWN."""

    JOIN = {"joined": True, "cell": "c" * 64, "public_key": "e" * 64,
            "profile": "p1", "materialized": True}
    TEXT = ("receipt chain mismatch: receipt chain mismatch: cipherclerk head "
            "= Some([7, 1]), receipt's prev = Some([9, 2])")
    REFUSAL = "[client-sign] error: node refused the turn: " + TEXT

    def signer(self, stderr, tail):
        """Join answers normally; send writes `stderr` then runs `tail`."""
        self.log = os.path.join(self.tmp, "signer.log")
        open(self.log, "w").close()   # one post per signer: count its sends
        err = os.path.join(self.tmp, "send.stderr")
        with open(err, "w") as f:
            f.write("[client-sign] verified ML-DSA cores: sign Some, verify Some\n"
                    + stderr)
        path = os.path.join(self.tmp, "dregg-client-sign")
        with open(path, "w") as f:
            f.write("#!/bin/sh\n"
                    'echo "argv:$1" >> "%s"\n'
                    'case "$1" in\n'
                    "join) echo '%s' ;;\n"
                    "send) cat '%s' >&2; %s ;;\n"
                    "esac\n" % (self.log, json.dumps(self.JOIN), err, tail))
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        os.environ["HELM_CELL_BIN"] = path
        os.environ["HELM_CHAT_NODE_URL"] = "http://127.0.0.1:1"

    def post(self, send_timeout=None):
        real = cellmod.run_bin

        def run_bin(args, timeout=90, env_extra=None):
            if send_timeout is not None and args and args[0] == "send":
                timeout = send_timeout
            return real(args, timeout=timeout, env_extra=env_extra)

        with mock.patch.object(cellmod, "run_bin", side_effect=run_bin), \
             mock.patch.object(chat, "_revive") as revive, \
             mock.patch.object(chat, "_faucet") as faucet:
            m = chat.post("hello", who="a1", profile="p1", sign=True)
        revive.assert_not_called()
        faucet.assert_not_called()
        with open(self.log) as f:
            self.assertEqual(f.read().count("argv:send"), 1,
                             "a launched send is never replayed")
        self.assertNotIn("chain", m)
        return m

    def test_an_explicit_refusal_is_failed_and_the_node_text_is_everywhere(self):
        self.signer(self.REFUSAL + "\n", "exit 1")
        m = self.post()
        t = m["transport"]
        self.assertEqual(t["code"], "send_failed")
        self.assertIn("so it did not commit: " + self.TEXT, t["reason"])
        self.assertNotIn("outcome unknown", t["reason"])
        self.assertIn(self.TEXT, chat._fmt(m))
        recorded = [r for r in chat.sign_failures() if r.get("profile") == "p1"]
        self.assertEqual(len(recorded), 1, chat.sign_failures())
        self.assertEqual(recorded[0]["code"], "send_failed")
        self.assertIn(self.TEXT, recorded[0]["reason"])
        self.assertIn(self.TEXT, chat.transport_failure_summary(
            chat.transport_status()))

    def assertUnknown(self, m, code="send_outcome_unknown"):
        t = m["transport"]
        self.assertEqual(t["code"], code)
        self.assertIn("outcome unknown", t["reason"])
        self.assertNotIn("did not commit", t["reason"])
        self.assertIn("before retrying", t["remediation"])

    def test_a_hang_after_the_refusal_line_is_a_timeout_not_a_refusal(self):  # noqa: VACUOUS_ASSERTION — the positive control on this same fixture signer is test_an_explicit_refusal_is_failed_and_the_node_text_is_everywhere, which must read send_failed with the text; assertUnknown's code and 'outcome unknown' checks are positive
        self.signer(self.REFUSAL + "\n", "exec sleep 30")
        self.assertUnknown(self.post(send_timeout=1), code="signing_timeout")

    def test_a_killed_signer_is_unknown_even_after_the_refusal_line(self):  # noqa: VACUOUS_ASSERTION — the positive control on this same fixture signer is test_an_explicit_refusal_is_failed_and_the_node_text_is_everywhere, which must read send_failed with the text; assertUnknown's code and 'outcome unknown' checks are positive, and a mutation sending every nonzero exit to refused turned this arm red
        self.signer(self.REFUSAL + "\n", "kill -9 $$")
        self.assertUnknown(self.post())

    def test_a_nonzero_exit_with_no_stdout_is_unknown(self):  # noqa: VACUOUS_ASSERTION — the positive control on this same fixture signer is test_an_explicit_refusal_is_failed_and_the_node_text_is_everywhere, which must read send_failed with the text; assertUnknown's code and 'outcome unknown' checks are positive, and a mutation sending every nonzero exit to refused turned this arm red
        for stderr in ("",
                       "[client-sign] error: POST /turns/submit: connection reset\n",
                       "[client-sign] error: parse submit response: EOF\n"):
            with self.subTest(stderr=stderr):
                self.signer(stderr, "exit 1")
                self.assertUnknown(self.post())

    def test_a_refusal_that_is_not_the_explicit_prefix_is_unknown(self):  # noqa: VACUOUS_ASSERTION — the positive control on this same fixture signer is test_an_explicit_refusal_is_failed_and_the_node_text_is_everywhere, which must read send_failed with the text; assertUnknown's code and 'outcome unknown' checks are positive, and a mutation sending every nonzero exit to refused turned this arm red
        for stderr in (
                "[client-sign] error: node refused the turn: no reason given\n",
                "[client-sign] error: node refused the turn:\n",
                "[client-sign] error: rejected: insufficient balance\n",
                "node refused the turn (no reason given)\n",
                self.REFUSAL + "\n[client-sign] error: late line\n",
                "[client-sign] turn accepted: %s; awaiting receipt...\n%s\n"
                % ("a" * 64, self.REFUSAL)):
            with self.subTest(stderr=stderr):
                self.signer(stderr, "exit 1")
                self.assertUnknown(self.post())

    def test_a_refusal_line_beside_stdout_or_a_zero_exit_is_unknown(self):  # noqa: VACUOUS_ASSERTION — the positive control on this same fixture signer is test_an_explicit_refusal_is_failed_and_the_node_text_is_everywhere, which must read send_failed with the text; assertUnknown's code and 'outcome unknown' checks are positive, and a mutation sending every nonzero exit to refused turned this arm red
        for tail in ("echo '{\"sent\": true}'; exit 1", "exit 0"):
            with self.subTest(tail=tail):
                self.signer(self.REFUSAL + "\n", tail)
                self.assertUnknown(self.post())


class NodeSendLockTest(V2Base):
    """TWO SENDS AGAINST ONE HEAD (task/3032). The chat node keeps ONE receipt
    chain for every agent: a signed turn commits only when it threads the
    node-wide head, and it is refused with "receipt chain mismatch" when any
    other agent committed after the signer read that head. Seats have their
    own cells, and they still race on this one head.

    The fixture signer below is that node. `send` reads the head, waits until
    the other sender has read it too (bounded, so a serialized sender goes on
    alone), and then, under the node's own write lock, commits only if the
    head has not moved. Otherwise it prints the live node's refusal."""

    SIGNER = r'''#!%(python)s
import fcntl, json, os, sys, time
d = %(dir)r
if sys.argv[1] != "send":
    sys.exit(2)
def head():
    with open(os.path.join(d, "head")) as f:
        return int(f.read() or 0)
seen = head()
with open(os.path.join(d, "readers"), "a") as f:
    f.write("r\n")
deadline = time.monotonic() + %(rendezvous)s
while time.monotonic() < deadline:
    with open(os.path.join(d, "readers")) as f:
        if len(f.readlines()) >= 2:
            break
    time.sleep(0.02)
with open(os.path.join(d, "node.lock"), "a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    now = head()
    if now != seen:
        sys.stderr.write("[client-sign] error: node refused the turn: receipt "
                         "chain mismatch: receipt chain mismatch: cipherclerk "
                         "head = Some([%%d]), receipt's prev = Some([%%d])\n"
                         %% (now, seen))
        sys.exit(1)
    with open(os.path.join(d, "head"), "w") as f:
        f.write(str(now + 1))
print(json.dumps({"sent": True, "turn_hash": "%%064x" %% (now + 1),
                  "receipt_hash": "%%064x" %% (now + 101),
                  "chain_index": now + 1}))
'''

    def setUp(self):
        super().setUp()
        self.node = os.path.join(self.tmp, "node")
        os.makedirs(self.node)
        with open(os.path.join(self.node, "head"), "w") as f:
            f.write("0")
        path = os.path.join(self.tmp, "dregg-client-sign")
        with open(path, "w") as f:
            f.write(self.SIGNER % {"python": sys.executable, "dir": self.node,
                                   "rendezvous": 1.5})
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        os.environ["HELM_CELL_BIN"] = path
        os.environ["HELM_CHAT_NODE_URL"] = "http://127.0.0.1:1"

    def patched(self):
        stack = contextlib.ExitStack()
        for target, attr, value in (
                (cellmod, "bin_status", READY_SIGNER),
                (chat, "_node_token", ""),
                (chat, "_env_extra", {}),
                (chat, "_room_cell", ("c" * 64, None, True))):
            stack.enter_context(mock.patch.object(target, attr,
                                                  return_value=value))
        self.revive = stack.enter_context(mock.patch.object(chat, "_revive"))
        self.faucet = stack.enter_context(mock.patch.object(chat, "_faucet"))
        return stack

    def race(self, profiles):
        """Both sends start together, as two seats posting in one second."""
        gate = threading.Barrier(len(profiles))
        got = {}

        def send(i, profile):
            gate.wait()
            got[i] = chat._sign_send("payload-%d" % i, profile)

        with self.patched():
            threads = [threading.Thread(target=send, args=(i, p))
                       for i, p in enumerate(profiles)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(60)
        self.assertEqual(sorted(got), list(range(len(profiles))))
        return [got[i] for i in range(len(profiles))]

    def test_two_seats_posting_at_once_both_commit_on_one_head(self):  # noqa: VACUOUS_ASSERTION — the empty failure list is paired with positive controls on the same sends: both chain indexes 1 and 2 and the node head at 2; on the pre-cure tree this arm failed with the live refusal text
        """The live failure: two seats' cells, ONE node-wide head. Unlocked,
        both signers read head 0 and the second to reach the node is refused
        with the chain mismatch, so its row is DEGRADED."""
        results = self.race(["seat-a", "seat-b"])
        failures = [f for _info, f in results if f]
        self.assertEqual(failures, [], "a send lost the head race")
        self.assertEqual(sorted(info["chain_index"] for info, _f in results),
                         [1, 2])
        with open(os.path.join(self.node, "head")) as f:
            self.assertEqual(f.read(), "2")
        self.revive.assert_not_called()
        self.faucet.assert_not_called()

    def test_two_sends_from_one_profile_both_commit(self):  # noqa: VACUOUS_ASSERTION — the empty failure list is paired with both chain indexes 1 and 2 as a positive control
        """The same race on one cell: the default profile is shared by the
        beacons, the compaction hook and every process with no seat, so its
        sends meet each other on its nonce as well as on the head."""
        results = self.race(["seat-a", "seat-a"])
        self.assertEqual([f for _info, f in results if f], [])
        self.assertEqual(sorted(info["chain_index"] for info, _f in results),
                         [1, 2])

    def hold(self, url):
        """Hold `url`'s send lock the way another process would."""
        import fcntl
        os.makedirs(os.path.dirname(chat._send_lock_path(url)), exist_ok=True)
        f = open(chat._send_lock_path(url), "a")
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        self.addCleanup(f.close)

    def test_an_unlocked_send_that_times_out_still_says_it_went_unlocked(self):
        """The timeout branch is where a waiter that gave up behind a hung
        holder lands, so its reason carries the unlocked note too."""
        self.hold(chat.node_url())
        with self.patched(), \
                mock.patch.object(chat, "SEND_LOCK_WAIT_S", 0.3), \
                mock.patch.object(cellmod, "run_bin",
                                  return_value=(None, "", cellmod.BinTimeout(
                                      "cell send timed out after 30s"))):
            info, failure = chat._sign_send("payload", "p1")
        self.assertIsNone(info)
        self.assertEqual(failure["code"], "signing_timeout")
        self.assertIn("timed out", failure["reason"])
        self.assertIn("without the node's send lock", failure["reason"])

    def test_a_held_lock_delays_a_send_only_up_to_its_bound(self):  # noqa: VACUOUS_ASSERTION — the None send-info is paired with the send_failed code, the node text and the unlocked note, and the None failure with chain index 7
        """FAIL-OPEN. A holder that never lets go (a hung signer, a stuck
        process) costs a send its bounded wait and no more: the send still
        goes out, and if it then fails, the reason says it went unlocked."""
        self.hold(chat.node_url())
        refusal = ("[client-sign] error: node refused the turn: receipt chain "
                   "mismatch: cipherclerk head = Some([2]), receipt's prev = "
                   "Some([1])")
        with self.patched(), \
                mock.patch.object(chat, "SEND_LOCK_WAIT_S", 0.3), \
                mock.patch.object(cellmod, "run_bin",
                                  return_value=(1, "", refusal)) as run:
            started = time.monotonic()
            info, failure = chat._sign_send("payload", "p1")
            waited = time.monotonic() - started
        self.assertIsNone(info)
        run.assert_called_once()
        self.assertGreaterEqual(waited, 0.3)
        self.assertLess(waited, 5)
        self.assertEqual(failure["code"], "send_failed")
        self.assertIn("receipt chain mismatch", failure["reason"])
        self.assertIn("without the node's send lock", failure["reason"])
        with self.patched(), \
                mock.patch.object(chat, "SEND_LOCK_WAIT_S", 0.3), \
                mock.patch.object(cellmod, "run_bin",
                                  return_value=(0, json.dumps(SENT), "")):
            info, failure = chat._sign_send("payload", "p1")
        self.assertIsNone(failure)
        self.assertEqual(info["chain_index"], 7)

    def test_the_lock_is_per_node_so_another_node_never_waits_on_it(self):  # noqa: VACUOUS_ASSERTION — the None failure is paired with the committed send's chain index 7 and a bounded wall time
        """One chain per node, so one lock per node: a send to a scratch node
        must not queue behind the live node's sends."""
        live, scratch = "http://127.0.0.1:1", "http://127.0.0.1:2"
        self.assertNotEqual(chat._send_lock_path(live),
                            chat._send_lock_path(scratch))
        self.hold(live)
        os.environ["HELM_CHAT_NODE_URL"] = scratch
        with self.patched(), \
                mock.patch.object(chat, "SEND_LOCK_WAIT_S", 30), \
                mock.patch.object(cellmod, "run_bin",
                                  return_value=(0, json.dumps(SENT), "")):
            started = time.monotonic()
            info, failure = chat._sign_send("payload", "p1")
            waited = time.monotonic() - started
        self.assertIsNone(failure)
        self.assertEqual(info["chain_index"], 7)
        self.assertLess(waited, 5)


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


class ReadReactIndexAlignmentTest(V2Base):
    """The read/react index-space split (a 🫡 landed on the wrong
    post). `read` prints reaction LINES that `react n` silently skips, so a
    human counting printed lines targets off-by-(reactions-above). The fix
    surfaces react's own ordinal as `[n]` beside each targetable row; reaction
    lines carry NO number, so the two index spaces cannot drift."""

    def _read_lines(self, *extra):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            chat.cmd_chat(["read", *extra])
        return [ln for ln in out.getvalue().splitlines() if ln.strip()]

    def test_read_numbers_are_exactly_the_react_targets(self):
        chat.post("first", who="a")
        chat.post("second", who="b")
        chat.react(1, ":tada:", who="r")     # a reaction ON 'first' — a LINE in
        chat.post("third", who="c")          # read, but never a react target
        lines = self._read_lines()
        numbered = [ln for ln in lines if ln.lstrip().startswith("[")]
        unnumbered = [ln for ln in lines if not ln.lstrip().startswith("[")]
        # exactly the 3 messages are numbered; the reaction line is not
        self.assertEqual(len(numbered), 3)
        self.assertEqual(len(unnumbered), 1)
        self.assertIn("reacted", unnumbered[0])       # the skipped react line
        # the row printed as [3] is 'third'...
        third = next(ln for ln in numbered if ln.lstrip().startswith("[3]"))
        self.assertIn("third", third)
        # ...AND react 3 toggles 'third' — the two index spaces now AGREE
        row, err = chat.react(3, ":rocket:", who="r")
        self.assertIsNone(err)
        self.assertEqual(row["tfrom"], "c")

    def test_react_prefix_marks_targets_and_blanks_reactions(self):
        # the shared owner both `read` and `read --follow` render through: a
        # target gets [n]; a reaction row gets an aligned blank, never a number
        rows = [{"ts": "t1", "from": "a", "text": "x"},
                {"react": "🎉", "from": "r", "tts": "t1", "tfrom": "a"},
                {"ts": "t2", "from": "b", "text": "y"}]
        tag = chat.react_prefix(rows)
        self.assertEqual(tag(0).strip(), "[1]")   # first message
        self.assertEqual(tag(1).strip(), "")      # the reaction row: no number
        self.assertEqual(tag(2).strip(), "[2]")   # second message, react skipped
        self.assertEqual(len(tag(0)), len(tag(1)))  # columns stay aligned

    def test_since_does_not_shift_the_ordinals(self):
        # ordinals count the WHOLE room, so a --since suffix keeps the same [n]
        for t in ("m1", "m2", "m3"):
            chat.post(t, who="a")
        shown = [ln for ln in self._read_lines("--since", "2")
                 if ln.lstrip().startswith("[")]
        self.assertEqual(len(shown), 1)                # only the 3rd prints
        self.assertTrue(shown[0].lstrip().startswith("[3]"))   # keeps ordinal 3
        self.assertIn("m3", shown[0])


class ReactToggleTest(V2Base):
    """The confirmed multi-bug (four identical ❤️ rows from one owner click):
    toggle idempotency + reactor identity attestation."""

    def _counts(self, m):
        rows, _total = chat.read()
        _msgs, reacts = chat.thread(rows)
        return reacts.get(chat.rkey(m), {})

    def test_double_react_nets_to_at_most_one(self):
        m = chat.post("hi", who="a1")
        chat.react(1, ":tada:", who="daria")
        self.assertEqual(self._counts(m), {"🎉": 1})    # first click adds
        row, err = chat.react(1, ":tada:", who="daria")  # the retry/re-click
        self.assertIsNone(err)
        self.assertTrue(row["un"])                       # a tombstone, not a dup
        self.assertEqual(self._counts(m), {})            # toggle-off removes
        chat.react(1, ":tada:", who="daria")             # third click re-adds
        self.assertEqual(self._counts(m), {"🎉": 1})     # never more than one

    def test_two_seats_counted_and_attributed_distinctly(self):
        m = chat.post("hi", who="a1")
        chat.react(1, ":fire:", who="codex-a")
        chat.react(1, ":fire:", who="opus-b")
        self.assertEqual(self._counts(m), {"🔥": 2})     # distinct reactors stack
        rows, _t = chat.read()
        self.assertEqual([r["from"] for r in rows if r.get("react")],
                         ["codex-a", "opus-b"])          # attribution preserved
        chat.react(1, ":fire:", who="codex-a")           # one seat toggles off
        self.assertEqual(self._counts(m), {"🔥": 1})     # the other still counts

    def test_render_collapses_legacy_duplicates(self):
        # the exact bad data in the wild: FOUR identical add rows from one click
        m = chat.post("hi", who="a1")
        for _ in range(4):
            chat._append({"ts": "2026-07-21T10:00:00", "from": "daria",
                          "react": "❤️", "tts": m["ts"], "tfrom": "a1"}, "main")
        self.assertEqual(self._counts(m), {"❤️": 1})    # legend self-heals on read
        chat.react(1, "❤️", who="daria")                 # next click sees ON ->
        self.assertEqual(self._counts(m), {})            # one tombstone clears all 4

    def test_signed_react_binds_the_reactor_identity(self):
        m = chat.post("hi", who="a1")
        with mock.patch.object(chat, "_sign_send", return_value=(SENT, None)) as ss:
            row, err = chat.react(1, ":tada:", who="a2", profile="a2", sign=True)
        self.assertIsNone(err)
        self.assertEqual((row["from"], row["chain"]), ("a2", 7))
        # the digest binds WHO reacted, not just what — forgery-evident — and
        # rides REACT_TAG, its own payload space. Under the v2 shape a plain
        # post whose text read `react|<ts>|a1|🎉|a2` digested identically to
        # this reaction; a disjoint tag makes that impossible by construction.
        ss.assert_called_once_with(
            chat.react_digest({"react": "🎉", "tts": m["ts"], "tfrom": "a1",
                               "from": "a2"}), "a2")
        self.assertTrue(ss.call_args[0][0].startswith(chat.REACT_TAG))
        self.assertNotIn("[unsigned]", chat._fmt(row))
        # fail-open: the unsigned path still lands, visibly unattested
        row2, err2 = chat.react(1, ":fire:", who="a3")
        self.assertIsNone(err2)
        self.assertNotIn("chain", row2)
        self.assertIn("[unsigned]", chat._fmt(row2))

    def test_cli_react_and_post_honor_seat(self):
        # CONTRACT (post-actor-binding, 2026-07-23): `--seat` is an ASSERTION of
        # the ambient session identity, NOT a cross-seat selector. This test
        # used to prove `--seat codex-a` posts/reacts AS codex-a from ANY
        # session; now the acting session IS codex-a (ambient) and `--seat
        # codex-a` asserts it. Cross-seat forgery is refused — proven in
        # test_chat.py::SeatActorBindingTest. Attribution + render are unchanged.
        os.environ["HELM_CHAT_NAME"] = "codex-a"
        chat.post("hi", who="a1")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(
                chat.cmd_chat(["react", "1", ":tada:", "--seat", "codex-a"]), 0)
            self.assertEqual(
                chat.cmd_chat(["post", "seated", "--seat", "codex-a"]), 0)
        rows, _t = chat.read()
        self.assertEqual(rows[1]["from"], "codex-a")     # the react row
        self.assertEqual(rows[2]["from"], "codex-a")     # the post row
        self.assertIn("codex-a reacted 🎉", out.getvalue())
        # toggle-off renders as an un-react, still attributed
        with contextlib.redirect_stdout(out):
            chat.cmd_chat(["react", "1", ":tada:", "--seat", "codex-a"])
        self.assertIn("codex-a un-reacted 🎉", out.getvalue())


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
        # the whole room turned over — mark's fingerprint is gone. The
        # auto-restore latch would legitimately re-fill the room from the
        # journal here (that is its job on a real wipe); this arm measures
        # the FLUSH's own gap note, so the latch is pinned off to keep the
        # room turned over.
        os.remove(chat.room_path("main"))
        with mock.patch.object(chat, "_auto_restore_once", lambda: None):
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

    def test_disable_suppresses_meld_lifecycle_flush_too(self):
        control, _ = meld.invite("seat-b", "enabled control", seat="seat-a")
        self.assertGreaterEqual(chat.log_flush(rooms=[control]), 0)
        self.assertTrue(os.path.exists(meld.lifecycle_path(control, durable=True)))
        room, _ = meld.invite("seat-b", "disabled subject", seat="seat-a")
        os.environ["HELM_CHAT_LOG"] = "0"
        self.assertEqual(chat.log_flush(rooms=[room]), -1)
        self.assertFalse(os.path.exists(meld.lifecycle_path(room, durable=True)))

    def test_lifecycle_failure_is_LOUD_and_no_longer_discards_the_batch(self):
        """REWRITTEN, NEVER DELETED — this arm's premise was superseded by the
        row, and both halves of its old name still have to hold on the new path.

        THE OLD CONTRACT: log_flush RAISED LifecycleError, the CLI printed
        "FAILED before rendered rows", and `_log_files()` stayed EMPTY. The
        loudness WAS the raise, and the raise was also the eight-hour outage —
        one diverged meld discarding the rendered-row flush of all 156 rooms,
        ~160 consecutive times, with every row of the fleet's morning in RAM
        only. Deleting the arm would have traded a failing test for a silent
        durability path, which is the class this row exists to kill.

        SO BOTH HALVES ARE RE-PROVED AGAINST THE NEW MECHANISM:

          (a) THE CLI IS STILL LOUD, and it now NAMES THE LEG rather than
              announcing an abort that no longer happens. rc 1 alone is not
              loudness — the old path exited 1 too, into a journal nobody read.
          (b) HEALTHY-ROW DURABILITY IS PRESERVED. This is the old
              `assertEqual(self._log_files(), [])` INVERTED, and the inversion
              is the entire point of the row: an ordinary room's rows reaching
              disk while a meld lifecycle leg is down IS the eight hours.
        """
        chat.post("control renders", who="a1")
        self.assertEqual(chat.log_flush(), 1)
        self.assertTrue(self._log_files())
        shutil.rmtree(chat.journal_dir())
        chat.post("must still render", who="a1")
        report = {}
        with mock.patch.object(meld, "flush_lifecycle",
                               side_effect=meld.LifecycleError("planted")):
            chat.log_flush(report=report)       # NO raise: the batch survives
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(chat.cmd_chat(["log-flush"]), 1)
        # (a) LOUD. The leg's failure is its own reported fact, not merely the
        # cause of a room quarantine — a global lifecycle failure can leave the
        # quarantine EMPTY (nothing needed skipping) and that must never render
        # as nothing having gone wrong.
        self.assertIn("planted", report["lifecycle_down"])
        self.assertIn("log-flush FAILED", err.getvalue())
        self.assertIn("meld lifecycle leg DOWN", err.getvalue())
        # (b) DURABLE. The rows of every room that COULD flush are on disk.
        self.assertIn("must still render", self._body())

    def test_cli_log_flush(self):
        chat.post("cli", who="a1")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(chat.cmd_chat(["log-flush"]), 0)
        self.assertIn("appended 1 row", out.getvalue())
        with contextlib.redirect_stdout(out):
            self.assertEqual(chat.cmd_chat(["log-flush", "--room", "ghost"]), 0)

    # -- read() FAILS OPEN, AND THIS CALLER CANNOT AFFORD IT -----------------
    #
    # read()'s contract is right for the live tail it was written for: an
    # unreadable room answers ([], 0) — byte-identical to an empty room — and a
    # torn line is dropped in silence. Both are DATA LOSS WEARING A SUCCESS in
    # _flush_one_room, whose other job is to ADVANCE THE HIGH-WATER MARK: the
    # run reports the room flushed, the watchdog stays green, and the rows it
    # never read are neither on disk nor named as stranded. This row's own
    # class, one level down, so it gets production-shaped controls.

    def _corrupt(self, room, kind):
        """Break a room the way a real writer breaks one. Returns the bytes a
        repair restores, so the arm can prove the cursor never passed them."""
        with open(chat.room_path(room), "rb") as f:
            good = f.read()
        tail = {
            # A KILLED WRITER: the last JSONL line stops mid-object. Every
            # earlier row is intact and readable the moment the fragment goes,
            # which is exactly what makes those rows REPAIRABLE.
            "torn": b'{"ts": "2026-08-11T17:15:00Z", "from": "a1", "text": "half-writ',
            # A PARTIAL MULTI-BYTE WRITE, and the SILENT one: decoded with
            # errors="replace" every line still parses and the row count
            # matches, so nothing downstream looks wrong at all.
            "undecodable": b'{"ts": "2026-08-11T17:15:01Z", "from": "a1",'
                           b' "text": "\xed\xa0"}\n',
        }[kind]
        with open(chat.room_path(room), "ab") as f:
            f.write(tail)
        return good

    def test_a_TORN_or_UNDECODABLE_room_is_QUARANTINED_and_its_CURSOR_STAYS_PUT(self):
        """The rows in a room whose bytes are damaged are still THERE, and a
        flush that advances past them converts a repairable file into permanent
        loss. Two production shapes: a half-written JSONL line, and bytes that
        are not valid UTF-8."""
        chat.post("healthy row", room="okroom", who="a1")
        chat.post("torn room row A", room="tornroom", who="a1")
        chat.post("torn room row B", room="tornroom", who="a1")
        chat.post("undecodable room row", room="badbytes", who="a1")
        whole = {"tornroom": self._corrupt("tornroom", "torn"),
                 "badbytes": self._corrupt("badbytes", "undecodable")}
        rooms = ["okroom", "tornroom", "badbytes"]
        report = {}
        appended = chat.log_flush(rooms=rooms, report=report)
        # THE BATCH SURVIVED: the healthy room reached disk in the same run.
        # That IS the positive control for the two absences below — same
        # journal text, unconditional, and it proves the loop got past the
        # damaged rooms to make a decision about this one.
        journal = self._body()
        self.assertEqual(appended, 1)
        self.assertIn("healthy row", journal)
        self.assertNotIn("torn room row A", journal)
        self.assertNotIn("undecodable room row", journal)
        # REFUSED BY NAME, with the fault that refused them, and counted as the
        # refusals they are.
        self.assertEqual(sorted(report["quarantined"]), ["badbytes", "tornroom"])
        self.assertIn("torn-rows", report["quarantined"]["tornroom"])
        self.assertIn("undecodable", report["quarantined"]["badbytes"])
        self.assertEqual(report["flushed_rooms"], 1)
        self.assertEqual(chat.flush_outcome(appended, report)[0], "degraded")
        # AND THE CURSOR STAYED PUT — the whole point. Repair the bytes and the
        # rows that were never flushed flush NOW. Against the fail-open read
        # this second call returns 0 and those rows are gone for good.
        for room, good in whole.items():
            with open(chat.room_path(room), "wb") as f:
                f.write(good)
        repaired = chat.log_flush(rooms=rooms)
        body = self._body()
        self.assertEqual(repaired, 3)
        self.assertIn("torn room row A", body)
        self.assertIn("torn room row B", body)
        self.assertIn("undecodable room row", body)

    def test_a_room_that_will_not_OPEN_is_QUARANTINED_not_marked_flushed(self):
        """The third shape, and the one read() flattens hardest: an OSError on
        open answers ([], 0), so the old code wrote a FRESH high-water mark for
        a room it had not read one row of — `{"n": 0}` is indistinguishable
        from a genuinely empty room forever after. A path occupied by a
        DIRECTORY is the root-proof production shape; the chmod-000 shape is
        its own arm below because a mode is no barrier to root."""
        chat.post("healthy row", room="okroom", who="a1")
        os.mkdir(chat.room_path("dirroom"))
        report = {}
        appended = chat.log_flush(rooms=["okroom", "dirroom"], report=report)
        state = pk.read_json(chat._flush_state_path(), {})
        self.assertEqual(appended, 1)
        self.assertIn("healthy row", self._body())        # the batch continued
        self.assertIn("dirroom", report["quarantined"])
        self.assertIn("unreadable", report["quarantined"]["dirroom"])
        self.assertEqual(report["flushed_rooms"], 1)
        # POSITIVE CONTROL ON THE SAME OBSERVABLE: the cursor file DID get
        # written this run and DOES carry the room that flushed, so the missing
        # key below is this room being refused rather than an unwritten file.
        self.assertIn("okroom", state)
        self.assertNotIn("dirroom", state)

    def test_a_chmod_000_room_is_QUARANTINED_too(self):
        """The permissions shape, kept separate and SKIPPED rather than
        silently guarded: root reads a chmod-000 file, and a sub-case that
        quietly stops measuring is indistinguishable from one that ran."""
        chat.post("healthy row", room="okroom", who="a1")
        chat.post("locked row", room="lockedroom", who="a1")
        os.chmod(chat.room_path("lockedroom"), 0)
        try:
            with open(chat.room_path("lockedroom"), "rb"):
                self.skipTest("this process reads a chmod-000 file (root?), so "
                              "the mode is not a barrier and the case would "
                              "prove nothing")
        except PermissionError:
            pass
        report = {}
        appended = chat.log_flush(rooms=["okroom", "lockedroom"], report=report)
        journal = self._body()
        self.assertEqual(appended, 1)
        self.assertIn("healthy row", journal)             # the batch continued
        self.assertNotIn("locked row", journal)
        self.assertIn("unreadable", report["quarantined"]["lockedroom"])
        self.assertEqual(report["flushed_rooms"], 1)
        # THE ROWS ARE REPAIRABLE AND STILL ARRIVE: restore the mode and the
        # row the flush refused to skip flushes now.
        os.chmod(chat.room_path("lockedroom"), 0o600)
        self.assertEqual(chat.log_flush(rooms=["okroom", "lockedroom"]), 1)
        self.assertIn("locked row", self._body())

    def test_flushed_rooms_is_COUNTED_and_a_lifecycle_only_QUARANTINE_cannot_lie(self):
        """`len(rooms) - len(quarantined)` SUBTRACTED TWO DIFFERENT
        POPULATIONS. `rooms` is the chat rooms this run walked; `quarantined`
        also carries melds the LIFECYCLE leg refused, and that leg enumerates
        the meld store rather than list_rooms() — so a meld with no chat room
        here is in the second set and not the first. The difference goes
        NEGATIVE, a negative int is TRUTHY, and flush_outcome's
        `if quarantined and not flushed` — the branch that says "nothing
        reached disk" — then reads a TOTAL outage as merely degraded."""
        chat.post("meld row", room="meld-stuck", who="a1")
        ghosts = {"meld-stuck": "diverges from RAM",
                  "meld-no-room-1": "diverges from RAM",
                  "meld-no-room-2": "diverges from RAM"}
        report = {}
        with mock.patch.object(meld, "flush_lifecycle",
                               return_value=(0, dict(ghosts))):
            appended = chat.log_flush(rooms=["meld-stuck"], report=report)
        # EVERY room this run walked was refused, and the classifier says so.
        # Inferred, the count here was 1 - 3 = -2 — truthy — and this read
        # "degraded" while nothing whatsoever had reached disk.
        state, reason = chat.flush_outcome(appended, report)
        self.assertEqual(state, "failed")
        self.assertIn("nothing reached disk", reason)
        # POSITIVE CONTROL ON THE SAME REPORT, and the defect's own premise:
        # two of these three melds have no chat room in this run at all, so the
        # quarantine really is the larger population.
        self.assertEqual(sorted(report["quarantined"]),
                         ["meld-no-room-1", "meld-no-room-2", "meld-stuck"])
        self.assertEqual(report["flushed_rooms"], 0)
        # POSITIVE CONTROL ON THE SAME OBSERVABLE: the identical shape with ONE
        # flushable room must read 1 (never 2 - 3 = -1) and must reach the
        # classifier's OTHER answer — so the zero above is this run's
        # measurement rather than a field that is always zero.
        chat.post("healthy row", room="okroom", who="a1")
        partial = {}
        with mock.patch.object(meld, "flush_lifecycle",
                               return_value=(0, dict(ghosts))):
            got = chat.log_flush(rooms=["okroom", "meld-stuck"], report=partial)
        self.assertEqual(got, 1)
        self.assertEqual(partial["flushed_rooms"], 1)
        self.assertEqual(chat.flush_outcome(got, partial)[0], "degraded")
        self.assertIn("healthy row", self._body())


class _Tty(io.StringIO):
    """A capture buffer that ANSWERS THE TTY QUESTION. `helm --human` refuses a
    non-terminal at its first line, so plain redirect_stdout makes every arm
    below exit 1 before reaching the code under test."""

    def isatty(self):
        return True


class HumanExitFlushTest(V2Base):
    """THE EXIT-TIME FLUSH MUST RENDER WHAT ACTUALLY REACHED DISK.

    The operator closing the shell is the last human who will look at this
    process. Its cleanup called `chat.log_flush()` with no report and printed
    only `appended N rows` when N was positive — a shape written when a failing
    flush RAISED. After per-room isolation it does not raise: it returns an int
    and carries the refusals on the report. So a partial flush rendered as
    plain success, and a run in which EVERY room was refused rendered as
    SILENCE — the eight hours, in the one place a human was watching."""

    def _exit_human(self):
        import curses
        out, err = _Tty(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with mock.patch.object(curses, "wrapper", return_value=0):
                rc = human.cmd_human([])
        return rc, out.getvalue(), err.getvalue()

    def test_a_healthy_exit_flush_is_plain_success_and_says_nothing_more(self):
        """The control every arm below is measured against: with nothing
        refused, the cleanup must stay exactly as quiet as it always was."""
        chat.post("healthy row", room="main", who="a1")
        rc, out, err = self._exit_human()
        self.assertEqual(rc, 0)
        self.assertIn("appended 1 row", out)
        self.assertNotIn("DEGRADED", err)
        self.assertNotIn("FAILED", err)

    def test_a_PARTIAL_exit_flush_is_never_rendered_as_plain_success(self):
        """Rows reached disk AND rows did not. The count alone says only the
        first half, and the second half is the one that needs a human."""
        chat.post("healthy row", room="main", who="a1")
        chat.post("meld row", room="meld-stuck", who="a1")
        with mock.patch.object(meld, "flush_lifecycle",
                               return_value=(0, {"meld-stuck": "diverges from RAM"})):
            rc, out, err = self._exit_human()
        self.assertEqual(rc, 0)
        self.assertIn("appended 1 row", out)      # the true half is still told
        self.assertIn("DEGRADED", err)            # and now so is the other one
        self.assertIn("meld-stuck", err)
        self.assertIn("stranded in RAM", err)

    def test_a_TOTAL_exit_flush_failure_is_never_SILENCE(self):
        """The measured incident's own shape. Nothing reaches disk, nothing
        raises, and `if n > 0` printed NOTHING AT ALL — a durability leg fully
        down looked identical to a quiet fleet."""
        chat.post("meld row", room="meld-stuck", who="a1")
        with mock.patch.object(meld, "flush_lifecycle",
                               side_effect=meld.LifecycleError("planted")):
            rc, out, err = self._exit_human()
        self.assertEqual(rc, 0)                   # the shell's own status holds
        self.assertIn("log-flush FAILED", err)
        self.assertIn("meld lifecycle leg DOWN", err)
        self.assertIn("planted", err)

    def test_a_RAISING_exit_flush_still_only_warns(self):
        """UNCHANGED, and asserted so the loudness above cannot have been won by
        letting a cleanup failure replace the shell's exit status."""
        with mock.patch.object(chat, "log_flush",
                               side_effect=RuntimeError("journal gone")):
            rc, _out, err = self._exit_human()
        self.assertEqual(rc, 0)
        self.assertIn("WARNING: exit-time log-flush failed", err)
        self.assertIn("journal gone", err)


class NodeSupervisorTest(V2Base):
    def test_unit_text_contract(self):
        t = chatnode.unit_text("/x/dregg-cave-node")
        self.assertIn("--data-dir /dev/shm/helm-chat-node", t)   # tmpfs — RAM law
        self.assertIn("--port 8898", t)
        self.assertIn("--enable-faucet", t)                      # never die on balance
        self.assertIn("/x/dregg-cave-node run", t)
        self.assertIn("WantedBy=default.target", t)

    def test_unit_inits_the_tmpfs_dir_and_can_actually_fail(self):
        """THE REBOOT BUG. /dev/shm is wiped on every boot, and `run` REFUSES to
        create its data dir — the binary exits 1 with "data directory does not
        exist ... Run `dregg-node init` first". The generator never emitted an
        init step, so after any reboot this unit could only ever fail. Measured
        2026-07-28: NRestarts=47 on this host, ~1827 fleet-wide, and every
        node_unreachable DEGRADED line in the chat log traced here.

        tmpfs is NOT the bug and must stay — one-cave-per-team makes the cave
        RAM-hot with disk only as the after-log. What tmpfs REQUIRES is that
        recreation be automatic, which is what was missing.

        SUPERSEDED IN PART, same day: the first fix was `test -d <dir> || init`
        inline, which restarted the node and silently RE-KEYED it every boot —
        the team's node returned as a stranger. The prepare step is a helm verb
        now (restore the snapshotted identity, mint only when there is nothing
        to restore); this test keeps the reboot and rate-limiter invariants and
        hands the identity half to tests/test_chatnode_identity.py."""
        t = chatnode.unit_text("/x/dregg-cave-node")
        self.assertIn("ExecStartPre=", t)
        self.assertIn("chat node prepare --data-dir /dev/shm/helm-chat-node", t)
        # the prepare must PRECEDE run, or it prepares after the failure it prevents
        self.assertLess(t.index("ExecStartPre="), t.index("ExecStart="))
        # and it must be FATAL: a node that cannot prove its identity must not start
        self.assertNotIn("ExecStartPre=-", t)
        # THE RATE LIMITER MUST BE REACHABLE. systemd's default interval is 10s
        # and RestartSec is 3, so ~3 starts fit per window and burst=5 could
        # NEVER trip — the unit looped forever instead of entering `failed`,
        # which is why 1827 restarts were SILENT. Assert the arithmetic, not the
        # literal: burst restarts, spaced RestartSec apart, must fit the window.
        import re
        interval = int(re.search(r"StartLimitIntervalSec=(\d+)", t).group(1))
        burst = int(re.search(r"StartLimitBurst=(\d+)", t).group(1))
        sec = int(re.search(r"RestartSec=(\d+)", t).group(1))
        self.assertLess(burst * sec, interval,
                        "burst*RestartSec must fit inside the window or the "
                        "limit never fires and a crash loop stays silent")

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

    def test_faucet_false_is_a_returned_refusal(self):
        with mock.patch.object(cellmod, "post_json",
                               return_value={"success": False,
                                             "error": "rate limited"}) as post:
            response, err = chatnode.faucet("http://node", "c" * 64, 10000)
        self.assertIsNone(response)
        self.assertEqual(err, "faucet refused: rate limited")
        post.assert_called_once_with(
            "http://node/api/faucet", {"recipient": "c" * 64, "amount": 10000})

    def test_truthy_strings_are_not_faucet_unlock_or_health_success(self):
        with mock.patch.object(cellmod, "post_json",
                               return_value={"success": "false",
                                             "bearer_token": "tok"}):
            self.assertIsNotNone(chatnode.faucet("http://node", "c" * 64, 1)[1])
            self.assertIsNotNone(chatnode.unlock("http://node", "pw")[1])
        with mock.patch.object(cellmod, "get_json",
                               return_value={"healthy": "false"}), \
             mock.patch.object(chatnode, "faucet",
                               return_value=(None, "refused")):
            self.assertEqual(chatnode.ensure_healthy_result("http://node"),
                             (False, "refused"))

    def test_ensure_healthy_keeps_the_legacy_bool_contract(self):
        with mock.patch.object(chatnode, "ensure_healthy_result",
                               return_value=(True, None)):
            self.assertIs(chatnode.ensure_healthy("http://node"), True)
        with mock.patch.object(chatnode, "ensure_healthy_result",
                               return_value=(False, "no")):
            self.assertIs(chatnode.ensure_healthy("http://node"), False)

    def test_cmd_node_usage_and_missing_binary(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(chatnode.cmd_node(["bogus"]), 2)
        with mock.patch.object(chatnode, "bin_resolution", return_value={
                "path": None, "source": None,
                "reason": "dregg-cave-node binary not found — set "
                          "HELM_CHAT_NODE_BIN or install to ~/.local/bin"}), \
             contextlib.redirect_stderr(err):
            self.assertEqual(chatnode.cmd_node(["up"]), 1)
        self.assertIn("not found", err.getvalue())

    def test_node_status_keeps_degraded_incident_loud_while_api_is_down(self):
        st = {"mode": "degraded", "profile": "p1", "reason": "send failed",
              "first_failure": "first", "last_failure": "last",
              "last_age_s": 4, "failure_count": 2,
              "remediation": "repair and retry"}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(chatnode, "_systemctl", return_value=(0, "active")), \
             mock.patch.object(chat, "node_url", return_value="http://node"), \
             mock.patch.object(chat, "transport_status", return_value=st), \
             mock.patch.object(cellmod, "get_json", return_value=None), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(chatnode.cmd_node(["status"]), 1)
        self.assertIn("DEGRADED profile 'p1'", err.getvalue())
        self.assertIn("API UNREACHABLE", out.getvalue())

    def test_cmd_chat_dispatches_node(self):
        with mock.patch.object(chatnode, "cmd_node", return_value=0) as cn:
            self.assertEqual(chat.cmd_chat(["node", "status"]), 0)
        cn.assert_called_once_with(["status"])


if __name__ == "__main__":
    unittest.main()
