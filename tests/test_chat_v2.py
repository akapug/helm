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
        with mock.patch.object(cellmod, "bin_status", return_value=READY_SIGNER), \
             mock.patch.object(cellmod, "run_bin", side_effect=fake_run), \
             mock.patch.object(chat, "_revive", return_value=("tok2", None)) as rv, \
             mock.patch.object(chat, "_faucet",
                               return_value=({"success": True}, None)) as fc:
            info, err = chat._sign_send("payload", "p1")
        self.assertIsNone(err)
        self.assertEqual(info["chain_index"], 7)
        self.assertEqual(calls, ["send", "send"])
        rv.assert_called_once()
        fc.assert_called_once_with("c" * 64)
        # the join result was cached RAM-side (the room dir), not on disk
        with open(chat.cells_path()) as f:
            self.assertEqual(json.load(f)["p1"], "c" * 64)

    def test_two_send_failures_and_faucet_false_are_returned_precisely(self):
        """The proven failure: both sends + a success:false faucet used to be
        discarded. The final diagnostic names every leg and redacts secrets."""
        secret = "x" * 320  # long enough that tail-slicing first loses `token=`
        outs = [(1, "", "insufficient computrons token=" + secret),
                (1, "", "still insufficient")]
        os.environ["HELM_CHAT_NODE_URL"] = "http://127.0.0.1:1"
        with mock.patch.object(cellmod, "bin_status", return_value=READY_SIGNER), \
             mock.patch.object(chat, "_room_cell",
                               return_value=("c" * 64, None, True)), \
             mock.patch.object(chat, "_balance", return_value=0), \
             mock.patch.object(cellmod, "run_bin", side_effect=outs) as rb, \
             mock.patch.object(chat, "_revive",
                               return_value=(None, "unlock refused")), \
             mock.patch.object(chat, "_faucet",
                               return_value=(None, "faucet refused: rate limited")) as fc:
            info, failure = chat._sign_send("payload", "p1")
        self.assertIsNone(info)
        self.assertEqual(failure["code"], "send_failed")
        self.assertIn("attempt 1 rc 1", failure["reason"])
        self.assertIn("attempt 2 rc 1", failure["reason"])
        self.assertIn("faucet refused: rate limited", failure["reason"])
        self.assertIn("unlock refused", failure["reason"])
        self.assertNotIn(secret[:40], failure["reason"])
        self.assertIn("token=[redacted]", failure["reason"])
        self.assertEqual(rb.call_count, 2)
        self.assertEqual(fc.call_count, 2)  # proactive + recovery grants

    def test_diagnostics_scrub_auth_schemes_and_quoted_secret_keys(self):
        cases = (
            "Authorization: Basic dXNlcjpwYXNz",
            'Authorization: Digest username="owner", response="sekrit"',
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
             mock.patch.object(chat, "_balance", return_value=None), \
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
        # a rc-0 line that never says sent:true is NOT a receipt — fail open
        self.SEND = {"ok": True}
        self.fake_signer()
        os.environ["HELM_CHAT_NODE_URL"] = "http://127.0.0.1:1"
        with mock.patch.object(chat, "_revive",
                               return_value=(None, "revive unavailable")), \
             mock.patch.object(chat, "_faucet",
                               return_value=(None, "faucet refused")):
            m = chat.post("hello", who="a1", sign=True)
        self.assertNotIn("chain", m)
        self.assertIn("[DEGRADED", chat._fmt(m))
        self.assertEqual(m["transport"]["code"], "send_failed")
        self.assertEqual(chat.read()[1], 1)   # the message never dies


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
        chat.react(1, ":tada:", who="owner")
        self.assertEqual(self._counts(m), {"🎉": 1})    # first click adds
        row, err = chat.react(1, ":tada:", who="owner")  # the retry/re-click
        self.assertIsNone(err)
        self.assertTrue(row["un"])                       # a tombstone, not a dup
        self.assertEqual(self._counts(m), {})            # toggle-off removes
        chat.react(1, ":tada:", who="owner")             # third click re-adds
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
            chat._append({"ts": "2026-07-21T10:00:00", "from": "owner",
                          "react": "❤️", "tts": m["ts"], "tfrom": "a1"}, "main")
        self.assertEqual(self._counts(m), {"❤️": 1})    # legend self-heals on read
        chat.react(1, "❤️", who="owner")                 # next click sees ON ->
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

    def test_lifecycle_failure_precedes_rendered_rows_and_cli_is_loud(self):  # noqa: VACUOUS_ASSERTION — normal flush control precedes planted failure
        chat.post("control renders", who="a1")
        self.assertEqual(chat.log_flush(), 1)
        self.assertTrue(self._log_files())
        shutil.rmtree(chat.journal_dir())
        chat.post("must not render", who="a1")
        with mock.patch.object(meld, "flush_lifecycle",
                               side_effect=meld.LifecycleError("planted")):
            with self.assertRaises(meld.LifecycleError):
                chat.log_flush()
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(chat.cmd_chat(["log-flush"]), 1)
        self.assertIn("FAILED before rendered rows", err.getvalue())
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
        with mock.patch.object(chatnode, "bin_path", return_value=None), \
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
