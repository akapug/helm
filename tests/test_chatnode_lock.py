#!/usr/bin/env python3
"""A LOCKED chat node can never hide again (task/2961 stage 2).

THE FAILURE: `helm chat node up` killed by an outer timeout before its last
step, provision(), leaves the node LOCKED. Four things hide that:

  * The rebased dregg node answers /status healthy:true while its cipherclerk
    is locked. helm's "a fresh node boots LOCKED and UNHEALTHY" was false for
    it, so ensure_healthy_result was satisfied and no surface said LOCKED.
  * The node refuses every submitted turn with a bodiless 403 when locked,
    before it stages or executes anything. The signer prints
    `/turns/submit returned 403 Forbidden` and exits 1.
  * chat's send path read that as send_outcome_unknown and discarded the
    signer's text, and its revive ran only when a JOIN failed, which a warm
    join cache never does.
  * provision() never wrote the RAM token, which chat prefers over the state
    token, so a stale RAM token earned 401 once a passphrase existed.

Every arm here runs on fakes and a temp HELM_HOME / HELM_CHAT_DIR. None of them
reaches a real node, a real signer or systemd.
"""
import contextlib
import io
import json
import os
import shutil
import stat
import tempfile
import time
import types
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from helm import cell, chat, chatnode, doctor  # noqa: E402

NODE = "http://127.0.0.1:8898"
# Bearer tokens are 64 hex (blake3 derive_key), so the fixtures are too:
# token-shaped on purpose, because the redaction arm must catch the shape.
FRESH = "f0" * 32
STALE = "5a" * 32
SENT = {"sent": True, "turn_hash": "a" * 64, "receipt_hash": "b" * 64,
        "chain_index": 7}
READY_SIGNER = {"configured": True, "usable": True, "state": "ready",
                "reason": "signer ready"}
# The signer's own words (dregg-client-sign post_turn: `/turns/submit
# returned {status}`, printed through `[client-sign] error: `).
FORBIDDEN = (1, "", "[client-sign] verified ML-DSA cores: sign Some, verify "
                    "Some\n[client-sign] error: /turns/submit returned 403 "
                    "Forbidden\n")
UNAUTHORIZED = (1, "", "[client-sign] error: /turns/submit returned 401 "
                       "Unauthorized\n")
COMMITTED = (0, json.dumps(SENT), "[client-sign] turn accepted: %s" % ("a" * 64))
FUNDED = {"state": "funded", "cell": "4a" * 32, "balance": 9000,
          "threshold": 1000, "observed_need": None, "reason": "",
          "grant": 1000, "low_grants": 3, "low_below": 3000,
          "grants_left": 9, "low": False}
ENV = ("HOME", "HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
       "HELM_CHAT_NODE_URL", "MELD_CHAT_NODE_URL", "HELM_CELL_PROFILE",
       "HELM_CHAT_NODE_BOOT_WAIT_S", "MELD_CHAT_NODE_BOOT_WAIT_S")


class LockBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-lock-")
        self.prior = {k: os.environ.get(k) for k in ENV}
        for k in ENV:
            os.environ.pop(k, None)
        os.environ["HOME"] = self.tmp
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = NODE
        os.environ["HELM_CELL_PROFILE"] = "test-profile"
        self.cwd = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        failures = chat.sign_failures_dir()
        os.chdir(self.cwd)
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(failures, ignore_errors=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def node(self, identity, healthy=True):
        """Stub the one node read seam. The API answers, /status reads
        `healthy`, and /api/node/identity answers `identity` verbatim."""
        def get_json(url, timeout=None):
            if url.endswith("/api/receipts"):
                return []
            if url.endswith("/status"):
                return {"healthy": healthy}
            if url.endswith("/api/node/identity"):
                return identity
            return None
        return mock.patch.object(cell, "get_json", side_effect=get_json)

    def status(self, identity):
        """`helm chat node` (status) over the stub, with every read that
        would touch the host (systemd, the journal, the live data dir, the
        signer and node binaries) replaced."""
        out, err = io.StringIO(), io.StringIO()
        with self.node(identity), \
                mock.patch.object(chatnode, "_systemctl",
                                  return_value=(0, "active")), \
                mock.patch.object(chatnode, "binary_report", return_value=None), \
                mock.patch.object(chatnode, "faucet_state", return_value=FUNDED), \
                mock.patch.object(chatnode, "_watch", return_value=None), \
                mock.patch.object(chatnode, "identity_state", return_value={
                    "saved": ["node.key"], "matched": True}), \
                mock.patch.object(chat, "transport_status",
                                  return_value={"mode": "unsigned"}), \
                mock.patch.object(cell, "signer_staleness", return_value={}), \
                mock.patch.object(cell, "node_staleness", return_value={}), \
                mock.patch.object(cell, "signer_cores", return_value={}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = chatnode.cmd_node([])
        return rc, out.getvalue() + err.getvalue()

    def doctor(self, identity):
        with self.node(identity), \
                mock.patch.object(chat, "node_head",
                                  return_value={"chain_index": 1}), \
                mock.patch.object(chat, "transport_status",
                                  return_value={"mode": "off"}), \
                mock.patch.object(chat, "cells_path", return_value=os.path.join(
                    self.tmp, "no-cells.json")), \
                mock.patch.object(chatnode, "binary_report", return_value=None), \
                mock.patch.object(chatnode, "faucet_state", return_value=FUNDED), \
                mock.patch.object(cell, "bin_status", return_value={
                    "configured": False, "usable": False}):
            return doctor.check_chat_node()

    def ram_token(self):
        with open(chat._token_path()) as f:
            return f.read()

    def plant_ram_token(self, token):
        os.makedirs(chat.chat_dir(), exist_ok=True)
        with open(chat._token_path(), "w") as f:
            f.write(token)


LOCKED = {"public_key": "ab" * 32, "agent_cell": "cd" * 32, "unlocked": False,
          "agent_balance": None, "agent_nonce": None}
UNLOCKED = dict(LOCKED, unlocked=True)
# Every shape that is not the node saying a boolean: no answer, not an object,
# the field absent, and truthy/falsy stand-ins that are not booleans.
UNREADABLE = (None, [], "unlocked", {}, {"unlocked": None},
              {"unlocked": "false"}, {"unlocked": 1}, {"unlocked": 0})


class LockedIsVisible(LockBase):
    """(a) status and doctor read the node's own `unlocked`, because a
    healthy node is no longer a provisioned one."""

    def test_status_shows_LOCKED_for_a_healthy_node_whose_cipherclerk_is_locked(self):
        rc, text = self.status(LOCKED)
        # UNCONDITIONAL CONTROL: status reached the live-node half.
        self.assertIn("API LIVE at %s" % NODE, text)
        locked = [ln for ln in text.splitlines() if "LOCKED" in ln]
        self.assertEqual(len(locked), 1, text)
        self.assertIn("helm chat node up", locked[0],
                      "the LOCKED line must carry its cure: %r" % locked[0])
        self.assertIn("403", locked[0])
        self.assertEqual(rc, 1, "a node that refuses every turn fails status")

    def test_status_of_an_unlocked_node_says_nothing_about_a_lock(self):
        rc, text = self.status(UNLOCKED)
        self.assertIn("API LIVE at %s" % NODE, text)
        self.assertEqual(rc, 0, text)
        self.assertNotIn("LOCKED", text)
        self.assertNotIn("lock state UNKNOWN", text)

    def test_status_reads_an_unreadable_identity_as_UNKNOWN_never_unlocked(self):  # noqa: VACUOUS_ASSERTION — the must-hit runs first and unconditionally: the same harness prints "LOCKED at" for a locked node, the needle the loop asserts absent
        # MUST-HIT FIRST, on the needle the loop asserts absent: the same
        # harness prints "LOCKED at" for a locked node.
        _rc, locked = self.status(LOCKED)
        self.assertIn("LOCKED at", locked)
        self.assertNotIn("lock state UNKNOWN", locked)
        for identity in UNREADABLE:
            with self.subTest(identity=identity):
                _rc, text = self.status(identity)
                self.assertIn("API LIVE at %s" % NODE, text)
                self.assertIn("lock state UNKNOWN", text)
                self.assertIn("unknown is not unlocked", text)
                self.assertNotIn("LOCKED at", text)

    def test_doctor_FAILS_a_locked_node_and_names_the_cure(self):
        rows = self.doctor(LOCKED)
        self.assertTrue([m for lvl, m in rows if lvl == doctor.OK
                         and "chat room node LIVE" in m], rows)
        locked = [(lvl, m) for lvl, m in rows if "LOCKED" in m]
        self.assertEqual(len(locked), 1, rows)
        self.assertEqual(locked[0][0], doctor.FAIL)
        self.assertIn("helm chat node up", locked[0][1])

    def test_doctor_WARNS_an_unreadable_identity_as_UNKNOWN(self):  # noqa: VACUOUS_ASSERTION — the must-hit runs first and unconditionally: the same harness reports "LOCKED at" for a locked node, the needle the loop asserts absent
        # MUST-HIT FIRST: the same harness reports "LOCKED at" for a locked
        # node, so its absence in the loop is a statement about the input.
        self.assertTrue([m for _lvl, m in self.doctor(LOCKED)
                         if "LOCKED at" in m])
        for identity in UNREADABLE:
            with self.subTest(identity=identity):
                rows = self.doctor(identity)
                self.assertTrue([m for lvl, m in rows if lvl == doctor.OK
                                 and "chat room node LIVE" in m], rows)
                hit = [(lvl, m) for lvl, m in rows if "lock state UNKNOWN" in m]
                self.assertEqual(len(hit), 1, rows)
                self.assertEqual(hit[0][0], doctor.WARN)
                self.assertFalse([m for _lvl, m in rows if "LOCKED at" in m])

    def test_doctor_says_nothing_about_a_lock_on_an_unlocked_node(self):  # noqa: VACUOUS_ASSERTION — the must-hits run first and unconditionally: the same harness reports LOCKED for a locked node and "lock state" for an unreadable one, and the LIVE row proves this call reached the check
        # MUST-HIT FIRST: the same harness speaks for a locked node and for
        # an unreadable identity.
        self.assertTrue([m for _lvl, m in self.doctor(LOCKED) if "LOCKED" in m])
        self.assertTrue([m for _lvl, m in self.doctor(None)
                         if "lock state" in m])
        rows = self.doctor(UNLOCKED)
        self.assertTrue([m for lvl, m in rows if lvl == doctor.OK
                         and "chat room node LIVE" in m], rows)
        self.assertFalse([m for _lvl, m in rows if "LOCKED" in m
                          or "lock state" in m], rows)

    def test_lock_state_is_the_nodes_boolean_or_unknown(self):
        with self.node(LOCKED):
            self.assertEqual(chatnode.lock_state(NODE)["state"], "locked")
        with self.node(UNLOCKED):
            self.assertEqual(chatnode.lock_state(NODE)["state"], "unlocked")
        for identity in UNREADABLE:
            with self.subTest(identity=identity), self.node(identity):
                got = chatnode.lock_state(NODE)
                self.assertEqual(got["state"], "unknown")
                self.assertTrue(got["reason"])


class SendHeals(LockBase):
    """(b) a 401/403 at /turns/submit is a DEFINITE refusal before
    execution: revive once, retry once."""

    def send(self, results, revive=None):
        """_sign_send over a warm join cache (no join can run, the case that
        hid the lock) and a scripted signer. Returns (info, failure, run
        mock, revive mock)."""
        with mock.patch.object(cell, "bin_status", return_value=READY_SIGNER), \
                mock.patch.object(chat, "_room_cell",
                                  return_value=("c" * 64, None, True)), \
                mock.patch.object(cell, "run_bin", side_effect=results) as run, \
                mock.patch.object(chat, "_faucet") as faucet, \
                mock.patch.object(chat, "_revive",
                                  return_value=revive or (FRESH, None)) as rv:
            info, failure = chat._sign_send("payload", "p1")
        faucet.assert_not_called()
        return info, failure, run, rv

    def test_a_403_revives_once_retries_once_and_commits(self):
        info, failure, run, rv = self.send([FORBIDDEN, COMMITTED])
        self.assertIsNone(failure)
        self.assertEqual(info["chain_index"], 7)
        self.assertEqual(run.call_count, 2)
        rv.assert_called_once_with()
        retry_env = run.call_args_list[1].kwargs["env_extra"]
        self.assertEqual(retry_env["DREGG_API_TOKEN"], FRESH,
                         "the retry must carry the revived token")

    def test_a_401_revives_once_retries_once_and_commits(self):
        info, failure, run, rv = self.send([UNAUTHORIZED, COMMITTED])
        self.assertIsNone(failure)
        self.assertEqual(info["chain_index"], 7)
        self.assertEqual(run.call_count, 2)
        rv.assert_called_once_with()

    def test_a_second_403_is_send_failed_and_names_the_cause(self):
        info, failure, run, rv = self.send([FORBIDDEN, FORBIDDEN])
        self.assertIsNone(info)
        self.assertEqual(failure["code"], "send_failed")
        self.assertIn("403", failure["reason"])
        self.assertIn("LOCKED", failure["reason"])
        self.assertIn("did not commit", failure["reason"])
        self.assertNotIn("outcome unknown", failure["reason"])
        self.assertIn("helm chat node up", failure["remediation"])
        self.assertEqual(run.call_count, 2, "retry ONCE, never twice")
        rv.assert_called_once_with()

    def test_a_revive_that_fails_is_send_failed_carrying_both_causes(self):
        why = "node revive unavailable: no stored chat-node passphrase"
        info, failure, run, rv = self.send([FORBIDDEN], revive=(None, why))
        self.assertIsNone(info)
        self.assertEqual(failure["code"], "send_failed")
        self.assertIn("403", failure["reason"])
        self.assertIn(why, failure["reason"])
        self.assertEqual(run.call_count, 1, "no retry without a revive")
        rv.assert_called_once_with()

    def test_other_statuses_and_shapes_stay_UNKNOWN_and_are_never_revived(self):  # noqa: VACUOUS_ASSERTION — the must-hit runs first and unconditionally: the same harness revives once and sends twice on the door's own words
        """Only the node's door refusal is definite. A 5xx, a 404, a lost
        connection, a 403 that is not the LAST word, and a 403 beside a
        turn-accepted line all stay UNKNOWN."""
        # MUST-HIT FIRST, on both spies the loop asserts about: the same
        # harness revives on the door's own words and sends a second time.
        _info, _failure, run, rv = self.send([FORBIDDEN, COMMITTED])
        rv.assert_called_once_with()
        self.assertEqual(run.call_count, 2)
        accepted = "[client-sign] turn accepted: %s; awaiting receipt" % ("a" * 64)
        cases = (
            (1, "", "[client-sign] error: /turns/submit returned 500 Internal "
                    "Server Error"),
            (1, "", "[client-sign] error: /turns/submit returned 404 Not Found"),
            (1, "", "[client-sign] error: POST /turns/submit: connection reset"),
            (1, "", "[client-sign] error: /turns/submit returned 403 Forbidden"
                    "\nlater words"),
            (1, "", accepted + "\n[client-sign] error: /turns/submit returned "
                               "403 Forbidden"),
            (1, "{", "[client-sign] error: /turns/submit returned 403 Forbidden"),
            (0, "", "[client-sign] error: /turns/submit returned 403 Forbidden"),
            (1, "", "[client-sign] error: /turns/submit returned 4031"),
        )
        for result in cases:
            with self.subTest(result=result):
                info, failure, run, rv = self.send([result])
                self.assertIsNone(info)
                self.assertEqual(failure["code"], "send_outcome_unknown")
                run.assert_called_once()
                rv.assert_not_called()

    def test_the_real_revive_refreshes_a_stale_ram_token_and_the_retry_uses_it(self):  # noqa: ORPHANED_MOCK — ensure_healthy_result is reached through chat._revive -> chatnode.ensure_healthy_result, a cross-module call; unlock beside it is asserted called once with the stored passphrase
        """The measured divergence: the RAM token is stale, the state holds
        the passphrase. The revive unlocks with the STORED passphrase and the
        RAM token becomes the one the node just issued."""
        chatnode.write_state({"url": NODE, "passphrase": "stored-pw",
                              "token": FRESH})
        self.plant_ram_token(STALE)
        self.assertEqual(chat._node_token(), STALE)   # the precondition
        with mock.patch.object(cell, "bin_status", return_value=READY_SIGNER), \
                mock.patch.object(chat, "_room_cell",
                                  return_value=("c" * 64, None, True)), \
                mock.patch.object(cell, "run_bin",
                                  side_effect=[UNAUTHORIZED, COMMITTED]) as run, \
                mock.patch.object(chatnode, "unlock",
                                  return_value=(FRESH, None)) as unlock, \
                mock.patch.object(chatnode, "ensure_healthy_result",
                                  return_value=(True, None)):
            info, failure = chat._sign_send("payload", "p1")
        self.assertIsNone(failure)
        self.assertEqual(info["chain_index"], 7)
        unlock.assert_called_once()
        self.assertEqual(unlock.call_args.args, (NODE, "stored-pw"))
        envs = [c.kwargs["env_extra"]["DREGG_API_TOKEN"]
                for c in run.call_args_list]
        self.assertEqual(envs, [STALE, FRESH])
        self.assertEqual(self.ram_token(), FRESH)


class ProvisionWritesRamToken(LockBase):
    """(c) after a provision the two tokens can never diverge."""

    def test_provision_writes_the_ram_token_it_stores(self):  # noqa: VACUOUS_ASSERTION — the one absence (err is None) sits beside unconditional equalities on the same provision call: the returned, stored and RAM tokens all equal FRESH
        self.plant_ram_token(STALE)
        with mock.patch.object(chatnode, "unlock", return_value=(FRESH, None)), \
                mock.patch.object(chatnode, "ensure_healthy_result",
                                  return_value=(True, None)):
            st, err = chatnode.provision(NODE)
        self.assertIsNone(err)
        self.assertEqual(st["token"], FRESH)
        self.assertEqual(chatnode.state()["token"], FRESH)
        self.assertEqual(self.ram_token(), FRESH)
        self.assertEqual(chat._node_token(), FRESH)
        self.assertEqual(stat.S_IMODE(os.stat(chat._token_path()).st_mode),
                         0o600)

    def test_a_failed_provision_leaves_the_ram_token_alone(self):  # noqa: VACUOUS_ASSERTION — st is None beside an unconditional assertIn on the same call's error, and the RAM token is read back and compared, not assumed
        self.plant_ram_token(STALE)
        with mock.patch.object(chatnode, "unlock",
                               return_value=(None, "unlock refused: invalid "
                                                   "passphrase")):
            st, err = chatnode.provision(NODE)
        self.assertIsNone(st)
        self.assertIn("invalid passphrase", err)
        self.assertEqual(self.ram_token(), STALE)


class SignerLineIsKept(LockBase):
    """(d) the DEGRADED reason carries the signer's last stderr line,
    bounded, with anything shaped like a bearer token removed."""

    OTHER = "Zk9" + "x" * 45          # base64url-ish, 48 chars

    def post(self, result, token=FRESH):
        self.plant_ram_token(token)
        with mock.patch.object(cell, "bin_status", return_value=READY_SIGNER), \
                mock.patch.object(chat, "_room_cell",
                                  return_value=("c" * 64, None, True)), \
                mock.patch.object(cell, "run_bin", return_value=result), \
                mock.patch.object(chat, "_revive", return_value=(None, "no")), \
                mock.patch.object(chat, "_faucet"):
            return chat.post("tried", who="a1", profile="p1", sign=True)

    def test_send_outcome_unknown_keeps_the_signer_line_without_its_token(self):  # noqa: VACUOUS_ASSERTION — every assertNotIn on a secret sits beside unconditional assertIns on the same reason ("the signer said", "GET receipts", "redacted"), so the reason is proven to carry the line
        # No `bearer`/`token=` framing: the existing key-based scrub would
        # catch those, and this arm is about the SHAPE and the exact value.
        line = ("[client-sign] error: GET receipts: session %s refused, retry "
                "id %s" % (FRESH, self.OTHER))
        row = self.post((1, "", "[client-sign] coordination-exempt join\n"
                         + line + "\n"))
        tr = row["transport"]
        self.assertEqual(tr["state"], "DEGRADED")
        self.assertEqual(tr["code"], "send_outcome_unknown")
        self.assertIn("outcome unknown", tr["reason"])
        self.assertIn("the signer said", tr["reason"])
        self.assertIn("GET receipts", tr["reason"])
        self.assertIn("redacted", tr["reason"])
        rendered = chat._fmt(row)
        for secret in (FRESH, FRESH[:32], self.OTHER, self.OTHER[:32]):
            self.assertNotIn(secret, tr["reason"])
            self.assertNotIn(secret, rendered)

    def test_the_exact_token_in_use_is_removed_even_when_it_is_short(self):
        short = "tok-7Qa"
        row = self.post((1, "", "[client-sign] error: GET receipts: session "
                                "tok-7Qa refused"), token=short)
        self.assertIn("GET receipts", row["transport"]["reason"])
        self.assertNotIn(short, row["transport"]["reason"])

    def test_the_signer_line_is_bounded(self):
        line = "[client-sign] error: GET receipts: " + "word " * 400
        row = self.post((1, "", line))
        reason = row["transport"]["reason"]
        self.assertIn("the signer said: [client-sign] error: GET receipts",
                      reason)
        self.assertLessEqual(len(reason), chat.REASON_CAP)
        said = reason.split("the signer said: ", 1)[1]
        self.assertLessEqual(len(said), chat.SIGNER_LINE_CAP)

    def test_send_failed_keeps_the_signer_line_that_named_the_door(self):
        with mock.patch.object(cell, "bin_status", return_value=READY_SIGNER), \
                mock.patch.object(chat, "_room_cell",
                                  return_value=("c" * 64, None, True)), \
                mock.patch.object(cell, "run_bin",
                                  side_effect=[FORBIDDEN, FORBIDDEN]), \
                mock.patch.object(chat, "_revive", return_value=(FRESH, None)):
            _info, failure = chat._sign_send("payload", "p1")
        self.assertEqual(failure["code"], "send_failed")
        self.assertIn("/turns/submit returned 403 Forbidden", failure["reason"])


def _show(active, sub, started_ago=None):
    started = (int((time.monotonic() - started_ago) * 1e6)
               if started_ago is not None else 0)
    return (0, "ActiveState=%s\nSubState=%s\nNRestarts=0\nResult=\n"
               "InvocationID=%s\nExecMainStartTimestampMonotonic=%d"
            % (active, sub, "b" * 32, started))


class UpClockExcludesCeremony(LockBase):
    """(e) `up`'s wait clock starts when the node PROCESS starts. The
    successor ceremony runs in ExecStartPre (systemd: activating/start-pre),
    and measured ceremony plus boot was ~673 s against a 600 s wait."""

    def test_time_in_the_prepare_step_is_not_counted_against_the_wait(self):
        t0 = time.time()

        def systemctl(*args):
            if args and args[0] == "show":
                if time.time() - t0 < 2.5:
                    return _show("activating", "start-pre")
                return _show("active", "running", started_ago=0.1)
            return 0, ""

        def get_json(url, timeout=None):
            return [] if time.time() - t0 > 3.0 else None
        with mock.patch.object(chatnode, "_systemctl", systemctl), \
                mock.patch.object(cell, "get_json", side_effect=get_json):
            outcome, _what = chatnode.wait_boot(NODE, unit=chatnode.UNIT,
                                                seconds=1.5)
        self.assertEqual(outcome, "up",
                         "a 1.5 s wait ran out inside the ceremony")

    def test_a_running_process_still_spends_the_wait(self):  # noqa: VACUOUS_ASSERTION — the outcome is asserted equal to "initializing" and the seconds at least the wait, both unconditional on the same wait_boot call
        """The control: once the process runs, the wait is the wait."""
        def systemctl(*args):
            return (_show("active", "running", started_ago=0.1)
                    if args and args[0] == "show" else (0, ""))
        t0 = time.time()
        with mock.patch.object(chatnode, "_systemctl", systemctl), \
                mock.patch.object(cell, "get_json", return_value=None):
            outcome, secs = chatnode.wait_boot(NODE, unit=chatnode.UNIT,
                                               seconds=1)
        self.assertEqual(outcome, "initializing")
        self.assertGreaterEqual(secs, 1)
        self.assertLess(time.time() - t0, 4)

    def test_a_prepare_that_never_ends_is_still_bounded(self):  # noqa: VACUOUS_ASSERTION — the outcome is asserted equal to "initializing", unconditionally, on the same wait_boot call whose elapsed time is bounded
        def systemctl(*args):
            return (_show("activating", "start-pre")
                    if args and args[0] == "show" else (0, ""))
        t0 = time.time()
        with mock.patch.object(chatnode, "_systemctl", systemctl), \
                mock.patch.object(chatnode, "START_TIMEOUT_S", 1), \
                mock.patch.object(cell, "get_json", return_value=None):
            outcome, _what = chatnode.wait_boot(NODE, unit=chatnode.UNIT,
                                                seconds=0.5)
        self.assertEqual(outcome, "initializing")
        self.assertLess(time.time() - t0, 5)


def _unlock_answers(status):
    """A cell.post_json whose unlock is refused with HTTP `status` (None: no
    answer at all), filling `diag` the way the real one does."""
    def post_json(url, payload, timeout=8, headers=None, diag=None):
        if diag is not None:
            diag.update(status=status, body="",
                        reason=("HTTP %s from %s" % (status, url)) if status
                        else "URLError: refused")
        return None
    return post_json


class UnlockNamesItsStatus(LockBase):
    """(b, sequel) the revive runs an unlock for every door-refused send, and
    the node admits five unlock attempts per client IP per minute (dregg
    api.rs `passphrase_limiter`; every seat here is one loopback client). A
    stored passphrase the node rejects spends that in six sends, and the
    next unlock — a revive, or the `helm chat node up` every LOCKED line
    names — is answered 429 by a node whose API is up. That answer must not
    read as `unreachable`."""

    def test_a_rate_limited_unlock_names_the_limiter_not_unreachable(self):
        with mock.patch.object(cell, "post_json", _unlock_answers(429)):
            token, err = chatnode.unlock(NODE, "stored-pw")
        self.assertIsNone(token)
        self.assertIn("429", err)
        self.assertIn("per client per minute", err)
        self.assertNotIn("unreachable", err)

    def test_any_other_http_status_is_named_and_no_answer_is_unreachable(self):
        with mock.patch.object(cell, "post_json", _unlock_answers(403)):
            _token, err = chatnode.unlock(NODE, "stored-pw")
        self.assertIn("HTTP 403", err)
        self.assertNotIn("unreachable", err)
        # THE CONTROL: nothing answered, and only then is it unreachable.
        with mock.patch.object(cell, "post_json", _unlock_answers(None)):
            _token, err = chatnode.unlock(NODE, "stored-pw")
        self.assertIn("unlock unreachable at %s" % NODE, err)

    def test_a_send_whose_revive_was_rate_limited_names_the_limiter(self):  # noqa: VACUOUS_ASSERTION — the absences (no `unreachable`, ensure_healthy_result not called, RAM token unchanged) sit beside unconditional positives on the same send: code send_failed, "the revive failed" and "429" in its reason, run_bin called once; noqa: ORPHANED_MOCK — ensure_healthy_result is reached through chat._revive -> chatnode.ensure_healthy_result, a cross-module call, and the author's test_the_real_revive_refreshes_a_stale_ram_token_and_the_retry_uses_it is the positive control that reaches it on a successful unlock
        """The real _revive over a real unlock: the send_failed reason the
        seat records carries the 429, not `unreachable`."""
        chatnode.write_state({"url": NODE, "passphrase": "stored-pw",
                              "token": FRESH})
        self.plant_ram_token(STALE)
        with mock.patch.object(cell, "bin_status", return_value=READY_SIGNER), \
                mock.patch.object(chat, "_room_cell",
                                  return_value=("c" * 64, None, True)), \
                mock.patch.object(cell, "run_bin",
                                  side_effect=[FORBIDDEN]) as run, \
                mock.patch.object(cell, "post_json", _unlock_answers(429)), \
                mock.patch.object(chatnode, "ensure_healthy_result") as healthy:
            info, failure = chat._sign_send("payload", "p1")
        self.assertIsNone(info)
        self.assertEqual(failure["code"], "send_failed")
        self.assertIn("the revive failed", failure["reason"])
        self.assertIn("429", failure["reason"])
        self.assertNotIn("unreachable", failure["reason"])
        run.assert_called_once()
        healthy.assert_not_called()
        self.assertEqual(self.ram_token(), STALE, "a refused unlock writes no token")


# dregg's own answers to an unlock the node READ (HTTP 200): post_cclerk_unlock
# says `invalid passphrase` with success false, never a 401.
REJECTED = {"success": False, "bearer_token": None,
            "error": "invalid passphrase"}
ACCEPTED = {"success": True, "bearer_token": FRESH, "error": None}
BACKOFF_FILE = ".node-unlock-backoff"


def _unlock_says(answer):
    """A cell.post_json whose unlock the node answered with `answer`."""
    def post_json(url, payload, timeout=8, headers=None, diag=None):
        return dict(answer)
    return post_json


class RefusedReviveBacksOff(LockBase):
    """(b, sequel 2) the node admits five unlock attempts per client per
    minute, and every seat here is one client. A revive the node refused
    leaves a short RAM marker, so a sibling seat's send skips its revive for
    chatnode.UNLOCK_BACKOFF_S instead of spending the budget the operator's
    `helm chat node up` needs. `up` ignores the marker and clears it."""

    def setUp(self):
        super().setUp()
        chatnode.write_state({"url": NODE, "passphrase": "stored-pw",
                              "token": FRESH})
        self.plant_ram_token(STALE)

    def marker(self):
        return os.path.join(chat.chat_dir(), BACKOFF_FILE)

    def plant_marker(self, at, reason="rate_limited"):
        """The residue a refused revive leaves, written as its writer does."""
        os.makedirs(chat.chat_dir(), exist_ok=True)
        with open(self.marker(), "w") as f:
            json.dump({"at": at, "reason": reason}, f)
        os.chmod(self.marker(), 0o600)

    def revive(self, post_json):
        """The REAL chat._revive over a scripted unlock. Returns (token, err,
        unlock POSTs)."""
        with mock.patch.object(cell, "post_json", side_effect=post_json) as post, \
                mock.patch.object(chatnode, "ensure_healthy_result",
                                  return_value=(True, None)):
            token, err = chat._revive()
        return token, err, post.call_count

    def send(self, post_json, door=FORBIDDEN):
        """One seat's send the node refuses at its door (403, or `door`) over
        a warm join cache, through the REAL _revive and a scripted unlock.
        Returns (info, failure, unlock POSTs)."""
        with mock.patch.object(cell, "bin_status", return_value=READY_SIGNER), \
                mock.patch.object(chat, "_room_cell",
                                  return_value=("c" * 64, None, True)), \
                mock.patch.object(cell, "run_bin",
                                  side_effect=[door, COMMITTED]), \
                mock.patch.object(cell, "post_json", side_effect=post_json) as post, \
                mock.patch.object(chatnode, "ensure_healthy_result",
                                  return_value=(True, None)):
            info, failure = chat._sign_send("payload", "p1")
        return info, failure, post.call_count

    def test_a_refused_revive_writes_a_short_backoff_marker_holding_no_secret(self):  # noqa: VACUOUS_ASSERTION — the loop runs over four fixed cases, and each asserts its marker exists, is 0600 and holds exactly its word; the secret absences are checked against that same raw text
        cases = ((_unlock_answers(429), "rate_limited"),
                 (_unlock_answers(401), "unauthorized"),
                 (_unlock_answers(403), "forbidden"),
                 (_unlock_says(REJECTED), "rejected"))
        for post_json, word in cases:
            with self.subTest(word=word):
                if os.path.exists(self.marker()):
                    os.remove(self.marker())
                before = time.time()
                token, err, calls = self.revive(post_json)
                self.assertIsNone(token)
                self.assertTrue(err)
                self.assertEqual(calls, 1)
                self.assertTrue(os.path.exists(self.marker()),
                                "a refused revive must leave the backoff marker")
                self.assertEqual(stat.S_IMODE(os.stat(self.marker()).st_mode),
                                 0o600)
                with open(self.marker()) as f:
                    raw = f.read()
                mark = json.loads(raw)
                self.assertEqual(sorted(mark), ["at", "reason"])
                self.assertEqual(mark["reason"], word)
                self.assertGreaterEqual(mark["at"], before)
                self.assertLessEqual(mark["at"], time.time())
                for secret in ("stored-pw", FRESH, STALE):
                    self.assertNotIn(secret, raw)

    def test_an_unlock_the_node_did_not_refuse_leaves_no_marker(self):  # noqa: VACUOUS_ASSERTION — the must-hit runs first and unconditionally: the same harness leaves the marker for a 429, and every case asserts its unlock POST ran and its revive failed
        # MUST-HIT FIRST: the same harness leaves the marker for a 429.
        self.revive(_unlock_answers(429))
        self.assertTrue(os.path.exists(self.marker()))
        os.remove(self.marker())
        # No answer at all, and a status that is not a refusal: the node may
        # be booting or broken, and a later revive is the one that cures it.
        for post_json in (_unlock_answers(None), _unlock_answers(500)):
            with self.subTest(post_json=post_json):
                token, err, calls = self.revive(post_json)
                self.assertEqual(calls, 1)
                self.assertIsNone(token)
                self.assertTrue(err)
                self.assertFalse(os.path.exists(self.marker()))

    def test_a_sibling_send_inside_the_backoff_skips_the_revive_and_names_it(self):  # noqa: VACUOUS_ASSERTION — the first send's unlock count and code are unconditional, and the loop runs over two fixed doors, each asserting its reason carries the door, the backoff, its cause and the cure
        _info, first, calls = self.send(_unlock_answers(429))
        self.assertEqual(first["code"], "send_failed")
        self.assertEqual(calls, 1, "the first seat's revive runs its unlock")
        # Both doors; the 401's is the longest reason the backoff rides in.
        for door, status in ((FORBIDDEN, "HTTP 403"), (UNAUTHORIZED, "HTTP 401")):
            with self.subTest(door=status):
                info, failure, calls = self.send(_unlock_answers(429), door)
                self.assertIsNone(info)
                self.assertEqual(calls, 0, "a sibling inside the backoff must "
                                           "not spend the node's unlock limiter")
                self.assertEqual(failure["code"], "send_failed")
                reason = failure["reason"]
                self.assertLessEqual(len(reason), chat.REASON_CAP)
                self.assertIn(status, reason)
                self.assertIn("unlock backoff", reason)
                self.assertIn("rate_limited", reason)
                self.assertIn("helm chat node up", reason)
                self.assertNotIn("elided", reason, "the backoff must fit whole")
                self.assertIn("helm chat node up", failure["remediation"])

    def test_after_the_backoff_one_revive_runs_again(self):  # noqa: VACUOUS_ASSERTION — the loop runs over two fixed planted markers, and each asserts one unlock ran and the next send names the backoff
        # An hour-old refusal, and one from an hour AHEAD (a wall clock set
        # back): neither may hold a revive, so the marker is bounded both ways.
        for at in (time.time() - 3600, time.time() + 3600):
            with self.subTest(at=at):
                self.plant_marker(at)
                _info, first, calls = self.send(_unlock_answers(429))
                self.assertEqual(calls, 1, "an expired backoff lets ONE revive run")
                self.assertEqual(first["code"], "send_failed")
                _info, second, calls = self.send(_unlock_answers(429))
                self.assertEqual(calls, 0, "that revive's refusal backs the "
                                           "next seat off again")
                self.assertIn("unlock backoff", second["reason"])

    def test_an_accepted_revive_clears_the_marker(self):  # noqa: VACUOUS_ASSERTION — the marker is planted and asserted present first, and the same send is asserted to commit, run one unlock and store the node's token
        self.plant_marker(time.time() - 3600)
        self.assertTrue(os.path.exists(self.marker()))
        info, failure, calls = self.send(_unlock_says(ACCEPTED))
        self.assertIsNone(failure)
        self.assertEqual(info["chain_index"], 7)
        self.assertEqual(calls, 1)
        self.assertEqual(self.ram_token(), FRESH)
        self.assertFalse(os.path.exists(self.marker()),
                         "an unlock the node accepted ends the backoff")

    def test_node_up_ignores_the_backoff_and_clears_it_on_success(self):  # noqa: VACUOUS_ASSERTION — the marker is planted and asserted present first, and the same provision is asserted to run its unlock and store the node's token
        self.plant_marker(time.time())          # a refusal this very second
        self.assertTrue(os.path.exists(self.marker()))
        with mock.patch.object(cell, "post_json",
                               side_effect=_unlock_says(ACCEPTED)) as post, \
                mock.patch.object(chatnode, "ensure_healthy_result",
                                  return_value=(True, None)):
            st, err = chatnode.provision(NODE)
        self.assertIsNone(err)
        self.assertEqual(st["token"], FRESH)
        post.assert_called_once()   # the operator's own unlock ran in the backoff
        self.assertEqual(self.ram_token(), FRESH)
        self.assertFalse(os.path.exists(self.marker()),
                         "a successful `up` clears the backoff")

    def test_a_refused_node_up_leaves_the_backoff_standing(self):
        at = time.time()
        self.plant_marker(at)
        with mock.patch.object(cell, "post_json",
                               side_effect=_unlock_answers(429)) as post:
            st, err = chatnode.provision(NODE)
        self.assertIsNone(st)
        self.assertIn("429", err)
        post.assert_called_once()
        with open(self.marker()) as f:
            self.assertEqual(json.load(f), {"at": at, "reason": "rate_limited"})


class WaitBootIsMonotonic(LockBase):
    """(e, sequel) wait_boot's budget is measured on time.monotonic(): a wall
    clock that jumps (NTP, a suspend, an operator's `date`) can neither
    stretch the wait nor spend it at once."""

    def jumped(self, by):
        """chatnode's `time`, with a wall clock that jumps `by` seconds after
        its first read; monotonic and sleep are the real ones."""
        reads = []

        def wall():
            reads.append(1)
            return time.time() + (by if len(reads) > 1 else 0)
        return mock.patch.object(chatnode, "time", types.SimpleNamespace(
            time=wall, monotonic=time.monotonic, sleep=time.sleep))

    def test_a_wall_clock_set_back_does_not_stretch_the_wait(self):  # noqa: VACUOUS_ASSERTION — the outcome is asserted equal to "initializing", unconditionally, on the same wait_boot call whose elapsed time is bounded
        t0 = time.monotonic()

        # THE VALVE: the API answers 4 s in, so a wait that lost its 1 s
        # budget ends `up` there instead of hanging for an hour.
        def get_json(url, timeout=None):
            return [] if time.monotonic() - t0 > 4 else None
        with self.jumped(-3600), \
                mock.patch.object(cell, "get_json", side_effect=get_json):
            outcome, _secs = chatnode.wait_boot(NODE, seconds=1)
        self.assertEqual(outcome, "initializing",
                         "a wall clock set back an hour stretched a 1 s wait")
        self.assertLess(time.monotonic() - t0, 3.5)

    def test_a_wall_clock_set_ahead_does_not_spend_the_wait_at_once(self):
        t0 = time.monotonic()

        def get_json(url, timeout=None):
            return [] if time.monotonic() - t0 > 1 else None
        with self.jumped(3600), \
                mock.patch.object(cell, "get_json", side_effect=get_json):
            outcome, secs = chatnode.wait_boot(NODE, seconds=3)
        self.assertEqual(outcome, "up",
                         "a wall clock set ahead an hour spent a 3 s wait at once")
        self.assertLess(secs, 3)


if __name__ == "__main__":
    unittest.main()
