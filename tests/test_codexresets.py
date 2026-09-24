#!/usr/bin/env python3
"""helm.codexresets — the two vendor calls, the policy, and the ledger.

HERMETIC BY CONSTRUCTION, AND THE CONSTRUCTION IS THE POINT. A redemption
spends a real, scarce, irreversible owner asset, so nothing here may be able to
reach the live endpoint even by accident: every arm drives a local
`http.server` whose port is chosen by the kernel, and the base URL is passed as
an argument rather than read from anywhere. HELM_HOME is a temp dir per test,
so the attempt ledger under test is never the host's.

THE READINGS ARE RECORDED, NOT INVENTED. The budget rows every policy arm
decides on come out of `codexbudget.probe_record` fed the real vendor bodies
this tree already keeps beside this file (the recorded wham-usage fixture), at
the instant those bodies were read — DERIVED from each body as a window's
absolute reset_at minus its own reset_after_seconds, because a transcribed
timestamp makes every window in the fixture read as already reset once the
clock passes it. An arm that needs a DIFFERENT world names the one field it
moves and moves it on a copy, so the shape around it stays the vendor's.

THE TOKEN CONTROL. Every result this suite produces is swept for the fixture
access token, so a note, a repr or a journal row that starts carrying the
credential fails an arm here rather than reaching a room. The sweep is only
worth its green if it can go red, so `RedactionTest` plants the token in a
place a careless note WOULD pick it up — a vendor error body that echoes the
Authorization header back — and asserts the module's own note does not.
"""
import contextlib
import copy
import io
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-codexresets-", var="HELM_HOME")

from helm import codexbudget, codexresets, proxywatch  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "codex-wham-usage-2026-09-14.json"),
          encoding="utf-8") as _f:
    RECORDED = json.load(_f)

#: Long enough to be a credential-shaped needle, and unmistakable in a diff.
FAKE_TOKEN = "fake-access-token-not-a-credential-0123456789"

#: "this argument was not given", distinct from "this argument was given as
#: None" — a reading whose reached-type is DELIBERATELY absent is its own case.
_UNSET = object()

#: ONE CREDIT ENTRY, SHAPED FROM A LIVE LISTING — the fields the vendor really
#: sends, including the two identifiers this module must never carry forward
#: (a profile id and a profile image url), so the arms below measure the drop
#: rather than assume it.
CREDIT = {"id": "credit-fixture-0001", "title": "Full reset (Weekly + 5 hr)",
          "description": "a granted rate limit reset",
          "reset_type": "codex_rate_limits", "status": "available",
          "is_supported_by_plan": True,
          "granted_at": "2026-09-01T00:00:00Z",
          "expires_at": "2026-10-03T00:00:00Z",
          "redeemed_at": None, "redeem_started_at": None,
          "profile_user_id": "profile-fixture",
          "profile_image_url": "https://example.invalid/fixture.png"}


def read_instant(body):
    """The epoch the vendor answered this body at, DERIVED from the body: a
    window's absolute reset_at minus the relative reset_after_seconds it was
    sent with. Transcribing a timestamp would make every window in the fixture
    read as already reset the moment the clock passed it."""
    rl = body["rate_limit"]
    w = rl.get("secondary_window") or rl["primary_window"]
    return w["reset_at"] - w["reset_after_seconds"]


def _weekly_key(body):
    rl = body["rate_limit"]
    return "secondary_window" if rl.get("secondary_window") else "primary_window"


#: THE DEFAULT SHAPE IS A RATE-LIMIT WALL, AND THAT IS NOT COSMETIC. The
#: recorded `team` body carries rate_limit_reached_type
#: `workspace_owner_credits_depleted` — a wall a rate-limit reset credit does
#: not lift — so every pass arm defaulting to it was measuring the policy
#: against a world it must now REFUSE. The `pro` body is a real
#: `rate_limit_reached` wall at 100%, which is the world the happy path is
#: about; the credits-depleted body still drives its own arms, by name.
DEFAULT_SHAPE = "pro"


def reading(shape=DEFAULT_SHAPE, weekly_pct=None, five_hour_pct=None,
            weekly_resets_in=None, email=None, reached=_UNSET,
            account_id=None, user_id=None, file=None, token=None):
    """One `codexbudget` budget row, built by the shipped probe from a
    recorded vendor body. Named keyword arguments move ONE field each, on a
    copy, so an arm that needs a healthier week does not also invent a plan,
    a window length or a reached-type.

    The pool FILE defaults to one per shape, because two readings sharing a
    file name are two spellings of ONE pooled credential and the rung is
    entitled to refuse them as ambiguous."""
    body = copy.deepcopy(RECORDED[shape])
    weekly = body["rate_limit"][_weekly_key(body)]
    if weekly_pct is not None:
        weekly["used_percent"] = weekly_pct
    if weekly_resets_in is not None:
        weekly["reset_after_seconds"] = weekly_resets_in
        weekly["reset_at"] = read_instant(RECORDED[shape]) + weekly_resets_in
    if five_hour_pct is not None:
        body["rate_limit"]["primary_window"]["used_percent"] = five_hour_pct
    if email is not None:
        body["email"] = email
    if account_id is not None:
        body["account_id"] = account_id
    if reached is not _UNSET:
        body["rate_limit_reached_type"] = \
            None if reached is None else {"type": reached, "details": None}
    name = file or ("codex-%s.json" % shape)
    acct = {"account_id": body["account_id"], "user_id": user_id,
            "email": body["email"],
            "file": name, "files": [name],
            "plan": body["plan_type"], "tier": "team",
            "access_token": token or FAKE_TOKEN}
    return codexbudget.probe_record(acct, get_json=lambda *_a, **_k: body,
                                    now=read_instant(RECORDED[shape]))


def weekly_reset_at(row):
    """The instant the reading says this account's weekly window reopens."""
    return codexresets.weekly_window(row)["reset_at"]


def account_of(row, token=None):
    """The pooled record serving a reading — what `codexbudget.pool_accounts`
    hands the rung, token AND member identity included."""
    return {"account_id": row["account_id"], "user_id": row.get("user_id"),
            "email": row["email"],
            "file": row["file"], "files": list(row["files"]),
            "access_token": token or FAKE_TOKEN}


class ResetHandler(BaseHTTPRequestHandler):
    """The vendor's two reset-credit routes. `list_mode` / `consume_mode`
    select the answer; `seen` records what actually arrived, so an arm can
    prove the request carried the credential AND that a refused call sent
    nothing at all."""
    list_mode = "ok"
    consume_mode = "reset"
    delay = 0.0
    seen = []
    #: {bearer token: available_count}. A vendor that answers the same balance
    #: to every credential cannot show an arm that helm asked with the WRONG
    #: one — which is the defect the shared-workspace arms exist to catch.
    balances = {}
    #: Tripped by the LISTING, so two passes can be held in flight at once at
    #: the point where they have both decided and neither has written.
    gate = None

    def log_message(self, *_args):
        pass

    def _record(self, body=None):
        ResetHandler.seen.append({
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "account": self.headers.get("chatgpt-account-id"),
            "body": body})

    def _json(self, code, payload):
        raw = payload if isinstance(payload, bytes) \
            else json.dumps(payload).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            # The timeout arms hang up mid-answer on purpose. A stack trace
            # for that is noise on top of the failure a reader came to read.
            pass

    def do_GET(self):
        self._record()
        if self.path == "/elsewhere":
            # THE REDIRECT TARGET, answering the vendor's OWN success shape.
            # Under a client that follows a 302 on a POST this is the body
            # that becomes the redemption's answer, so an arm can measure
            # whether the spending path follows one.
            return self._json(200, {"code": codexresets.OUTCOME_RESET})
        if ResetHandler.gate is not None:
            try:
                ResetHandler.gate.wait()
            except threading.BrokenBarrierError:
                pass
        token = (self.headers.get("Authorization") or "")[len("Bearer "):]
        if token in ResetHandler.balances:
            count = ResetHandler.balances[token]
            return self._json(200, {"available_count": count,
                                    "total_earned_count": 5,
                                    "credits": [dict(CREDIT)
                                                for _ in range(count)]})
        mode = ResetHandler.list_mode
        if mode == "slow":
            time.sleep(ResetHandler.delay or 1.0)
            mode = "ok"
        if mode in ("401", "429", "500"):
            return self._json(int(mode), {"error": "no"})
        if mode == "malformed":
            return self._json(200, b"<html>not json at all</html>")
        if mode == "nonnumeric":
            return self._json(200, {"available_count": "two"})
        if mode == "boolean":
            return self._json(200, {"available_count": True})
        if mode == "zero":
            return self._json(200, {"available_count": 0,
                                    "total_earned_count": 5, "credits": []})
        if mode == "unusable":
            # The count says two; both entries are already redeemed. Measured
            # live: a credit entry carries its own status, so the count is a
            # claim the entries can contradict.
            return self._json(200, {"available_count": 2, "credits": [
                dict(CREDIT, status="redeemed"), dict(CREDIT, status="redeemed")]})
        return self._json(200, {"available_count": 2, "total_earned_count": 5,
                                "credits": [
                                    dict(CREDIT, expires_at="2026-10-03T00:00:00Z"),
                                    dict(CREDIT, expires_at="2026-10-17T00:00:00Z")]})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            body = None
        self._record(body)
        mode = ResetHandler.consume_mode
        if mode == "slow":
            time.sleep(ResetHandler.delay or 1.0)
            mode = "reset"
        if mode in ("400", "401", "403", "429", "500", "503"):
            return self._json(int(mode), {"error": "no"})
        if mode in ("302", "307"):
            # A REDIRECT ON THE SPENDING PATH, pointed at a route that answers
            # the vendor's success shape.
            self.send_response(int(mode))
            self.send_header("Location", "/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if mode == "echo500":
            # A VENDOR PAGE THAT QUOTES OUR OWN REQUEST BACK. The credential is
            # inside this body; a note built from it would carry it out.
            return self._json(500, {"error": "upstream rejected %s"
                                             % self.headers.get("Authorization")})
        if mode == "malformed":
            return self._json(200, b"not json")
        if mode == "weird":
            return self._json(200, {"code": "teleported"})
        return self._json(200, {"code": mode})


class FakeVendorCase(unittest.TestCase):
    """One local vendor per test, plus the token sweep every subclass runs."""

    def setUp(self):
        # REALPATH, because the event-ledger primitive refuses a parent
        # reached through a symlink — and a temp root can be one.
        self.tmp = os.path.realpath(
            tempfile.mkdtemp(prefix="helm-test-codexresets-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env = mock.patch.dict(os.environ,
                                   {"HELM_HOME": os.path.join(self.tmp, "home")})
        self.env.start()
        self.addCleanup(self.env.stop)
        ResetHandler.seen = []
        ResetHandler.list_mode = "ok"
        ResetHandler.consume_mode = "reset"
        ResetHandler.delay = 0.0
        ResetHandler.balances = {}
        ResetHandler.gate = None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ResetHandler)
        self.addCleanup(self.server.server_close)
        # shutdown() returns only after serve_forever's next poll, and the
        # stdlib polls every 0.5s: one idle half-second per test, 124 times.
        # helm/mcpd.serve_background documents the trade (a fast poll costs
        # idle CPU, nothing a per-test server minds).
        threading.Thread(target=self.server.serve_forever,
                         kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.ledger = os.path.join(self.tmp, "attempts.jsonl")
        self.account = {"account_id": "acct-fixture-team",
                        "user_id": "user-fixture-team",
                        "email": "team@fixture.invalid",
                        "file": "codex-team.json",
                        "files": ["codex-team.json"],
                        "access_token": FAKE_TOKEN}

    def auths(self, path=None):
        """The Authorization header of every request that reached the fake."""
        return [r["auth"] for r in ResetHandler.seen
                if path is None or r["path"] == path]

    def ledger_rows(self):
        rows, err = codexresets.attempts(self.ledger)
        self.assertIsNone(err)
        return rows

    def consume_requests(self):
        return [r for r in ResetHandler.seen
                if r["path"] == codexresets.CONSUME_PATH]

    def assertNoSecret(self, obj, what="result"):
        """No rendering of this object may contain the credential. `repr` of
        the whole structure is the sweep, because a note is not the only field
        a token can end up in."""
        self.assertNotIn(FAKE_TOKEN, repr(obj),
                         "%s carries the access token" % what)


class ListCreditsTest(FakeVendorCase):
    def test_a_listing_reads_the_balance_and_sends_the_credential(self):
        res = codexresets.list_credits(self.account, url_base=self.base)
        self.assertEqual(res["status"], codexresets.LIST_OK)
        self.assertEqual(res["available"], 2)
        self.assertEqual(res["total_earned"], 5)
        self.assertEqual(res["spendable"], 2)
        self.assertEqual([c["expires_at"] for c in res["credits"]],
                         ["2026-10-03T00:00:00Z", "2026-10-17T00:00:00Z"])
        # THE VENDOR'S IDENTIFIERS DO NOT SURVIVE THE READ. The fixture
        # carries them, so this is a measurement of the drop.
        self.assertEqual(sorted(res["credits"][0]),
                         ["expires_at", "reset_type", "status", "supported"])
        # THE CONTROL: the fake actually saw a request, with the credential on
        # it and the account scoped. A green status over an unvisited server
        # would certify nothing.
        self.assertEqual(len(ResetHandler.seen), 1)
        sent = ResetHandler.seen[0]
        self.assertEqual(sent["path"], codexresets.LIST_PATH)
        self.assertEqual(sent["auth"], "Bearer " + FAKE_TOKEN)
        self.assertEqual(sent["account"], "acct-fixture-team")
        self.assertNoSecret(res)

    def test_each_http_failure_is_its_own_named_status(self):
        # The unconditional control: this endpoint CAN answer `listed`, so a
        # loop below that never ran would not be mistaken for agreement.
        self.assertEqual(codexresets.list_credits(
            self.account, url_base=self.base)["status"], codexresets.LIST_OK)
        for mode, want in (("401", codexresets.LIST_UNAUTHORIZED),
                           ("429", codexresets.LIST_RATE_LIMITED),
                           ("500", codexresets.LIST_HTTP_ERROR)):
            with self.subTest(mode=mode):
                ResetHandler.list_mode = mode
                res = codexresets.list_credits(self.account, url_base=self.base)
                self.assertEqual(res["status"], want)
                self.assertIsNone(res["available"])
                self.assertNoSecret(res)

    def test_a_body_helm_cannot_read_is_malformed_never_zero_credits(self):
        self.assertEqual(codexresets.list_credits(
            self.account, url_base=self.base)["available"], 2)
        for mode in ("malformed", "nonnumeric", "boolean"):
            with self.subTest(mode=mode):
                ResetHandler.list_mode = mode
                res = codexresets.list_credits(self.account, url_base=self.base)
                self.assertEqual(res["status"], codexresets.LIST_MALFORMED)
                # NOT 0: an unreadable balance is not an empty one, and a zero
                # here would read as a measured "no credits".
                self.assertIsNone(res["available"])

    def test_a_count_the_entries_contradict_is_not_spendable(self):
        # Measured live: `available_count` is one number the vendor computes,
        # and each entry says for itself whether it is still available. The
        # SMALLER answer wins, because a credit cannot be un-spent.
        ResetHandler.list_mode = "unusable"
        res = codexresets.list_credits(self.account, url_base=self.base)
        self.assertEqual(res["available"], 2)
        self.assertEqual(res["spendable"], 0)

    def test_a_timeout_is_unreachable(self):
        ResetHandler.list_mode = "slow"
        res = codexresets.list_credits(self.account, url_base=self.base,
                                       timeout=0.15)
        self.assertEqual(res["status"], codexresets.LIST_UNREACHABLE)
        self.assertNoSecret(res)


class ConsumeTest(FakeVendorCase):
    def test_every_documented_code_comes_back_under_its_own_name(self):
        # One case outside the loop, so a loop that silently iterated nothing
        # cannot read as four passing arms.
        self.assertEqual(codexresets.consume(self.account, "key-0",
                                             url_base=self.base)["outcome"],
                         codexresets.OUTCOME_RESET)
        for code, spent in ((codexresets.OUTCOME_RESET, True),
                            (codexresets.OUTCOME_ALREADY, True),
                            (codexresets.OUTCOME_NOTHING, False),
                            (codexresets.OUTCOME_NO_CREDIT, False)):
            with self.subTest(code=code):
                ResetHandler.consume_mode = code
                res = codexresets.consume(self.account, "key-1",
                                          url_base=self.base)
                self.assertEqual(res["outcome"], code)
                self.assertEqual(res["spent"], spent)
                self.assertTrue(res["settled"])
                self.assertNoSecret(res)

    def test_the_idempotency_key_is_what_reaches_the_vendor(self):
        codexresets.consume(self.account, "key-abc", url_base=self.base)
        self.assertEqual(len(ResetHandler.seen), 1)
        sent = ResetHandler.seen[0]
        self.assertEqual(sent["path"], codexresets.CONSUME_PATH)
        self.assertEqual(sent["body"], {"redeem_request_id": "key-abc"})
        self.assertEqual(sent["auth"], "Bearer " + FAKE_TOKEN)

    def test_an_unknown_code_is_not_folded_into_a_known_one(self):
        ResetHandler.consume_mode = "weird"
        res = codexresets.consume(self.account, "key-1", url_base=self.base)
        self.assertEqual(res["outcome"], codexresets.OUTCOME_UNKNOWN_CODE)
        self.assertTrue(res["settled"])
        # THE VENDOR ANSWERED AND SAID NOTHING HELM CAN ACT ON. `spent` False
        # here would be a claim about the owner's balance that this module
        # cannot make — and the claim that drops the idempotency key.
        self.assertIsNone(res["spent"])
        self.assertEqual(res["redeem_request_id"], "key-1")
        self.assertIn(codexresets.OUTCOME_UNKNOWN_CODE,
                      codexresets.UNRESOLVED_OUTCOMES)
        # THE CONTROL on the same call: a documented code IS settled, and its
        # `spent` is a real boolean.
        ResetHandler.consume_mode = codexresets.OUTCOME_NO_CREDIT
        known = codexresets.consume(self.account, "key-2", url_base=self.base)
        self.assertIs(known["spent"], False)

    def test_a_refusal_is_settled_and_spent_nothing(self):  # noqa: VACUOUS_ASSERTION — the False IS the property under test; the control above asserts the same field reads True through the identical call
        # The control binds to the SAME root the loop below asserts on, so a
        # loop that never ran cannot read as three passing arms.
        res = codexresets.consume(self.account, "key-0", url_base=self.base)
        self.assertIs(res["spent"], True)
        for mode, want in (("401", codexresets.OUTCOME_UNAUTHORIZED),
                           # 403 IS THE OTHER HALF OF ONE BRANCH, and it is
                           # the reachable one: a 401 on the LISTING refuses
                           # before any consume, so the credential that gets
                           # this far is one that may READ the workspace's
                           # credits and not REDEEM them.
                           ("403", codexresets.OUTCOME_UNAUTHORIZED),
                           ("429", codexresets.OUTCOME_RATE_LIMITED),
                           ("400", codexresets.OUTCOME_REFUSED)):
            with self.subTest(mode=mode):
                ResetHandler.consume_mode = mode
                res = codexresets.consume(self.account, "key-1",
                                          url_base=self.base)
                self.assertEqual(res["outcome"], want)
                self.assertIs(res["spent"], False)
                self.assertTrue(res["settled"])

    def test_a_server_error_is_unknown_and_keeps_its_key(self):
        res = codexresets.consume(self.account, "key-0", url_base=self.base)
        self.assertTrue(res["settled"])      # the control, on the same root
        # A 5xx IS NOT A REFUSAL: the vendor may have redeemed the credit and
        # then failed to say so. `spent` must be None, not False, or the next
        # pass mints a second key and spends a second credit.
        for mode in ("500", "503"):
            with self.subTest(mode=mode):
                ResetHandler.consume_mode = mode
                res = codexresets.consume(self.account, "key-5xx",
                                          url_base=self.base)
                self.assertEqual(res["outcome"], codexresets.OUTCOME_UNKNOWN)
                self.assertIsNone(res["spent"])
                self.assertFalse(res["settled"])
                self.assertEqual(res["redeem_request_id"], "key-5xx")

    def test_a_timeout_is_unknown_and_keeps_its_key(self):
        ResetHandler.consume_mode = "slow"
        res = codexresets.consume(self.account, "key-slow", url_base=self.base,
                                  timeout=0.15)
        self.assertEqual(res["outcome"], codexresets.OUTCOME_UNKNOWN)
        self.assertIsNone(res["spent"])
        self.assertEqual(res["redeem_request_id"], "key-slow")
        self.assertNoSecret(res)

    def test_an_unreadable_2xx_body_is_settled_and_its_effect_unknown(self):
        # The vendor ANSWERED; helm could not read it. `settled` records the
        # first half — an answer arrived — and it is NOT a claim about the
        # balance: a 200 from the redemption endpoint is where a credit most
        # likely left it, so the key must survive.
        ResetHandler.consume_mode = "malformed"
        res = codexresets.consume(self.account, "key-1", url_base=self.base)
        self.assertEqual(res["outcome"], codexresets.OUTCOME_MALFORMED)
        self.assertTrue(res["settled"])
        self.assertIsNone(res["spent"])
        self.assertEqual(res["redeem_request_id"], "key-1")
        self.assertIn(codexresets.OUTCOME_MALFORMED,
                      codexresets.UNRESOLVED_OUTCOMES)

    def test_a_consume_without_a_key_is_refused_before_it_is_sent(self):
        res = codexresets.consume(self.account, "", url_base=self.base)
        self.assertEqual(res["outcome"], codexresets.OUTCOME_REFUSED)
        self.assertEqual(self.consume_requests(), [])
        codexresets.consume(self.account, "control-key", url_base=self.base)
        self.assertEqual([r["body"]["redeem_request_id"]
                          for r in self.consume_requests()], ["control-key"],
                         "must-hit: the recorder registers a consume, so the "
                         "emptiness above is a measurement")


class RedirectOnTheSpendingPathTest(FakeVendorCase):
    """A 3xx ON THE REDEMPTION POST. urllib follows a 302 by RE-ISSUING the
    request as a GET with the body dropped, and the redirect target's answer
    becomes this call's answer — so a URL nobody in the module named could
    hand back the vendor's own success shape, close the idempotency key, and
    have helm tell the owner a credit of his was spent."""

    def test_a_redirect_is_never_followed_and_is_never_a_vendor_code(self):
        for mode in ("302", "307"):
            with self.subTest(mode=mode):
                ResetHandler.seen = []
                ResetHandler.consume_mode = mode
                res = codexresets.consume(self.account, "key-redirect",
                                          url_base=self.base)
                self.assertEqual(res["outcome"], codexresets.OUTCOME_UNKNOWN)
                self.assertIn(res["outcome"], codexresets.UNRESOLVED_OUTCOMES,
                              "the effect is unknown, so the key is kept")
                self.assertIsNone(res["spent"])
                self.assertEqual(res["redeem_request_id"], "key-redirect")
                self.assertEqual([r["path"] for r in ResetHandler.seen],
                                 [codexresets.CONSUME_PATH],
                                 "the redirect target was fetched, and its "
                                 "answer is now this redemption's answer")
        # THE CONTROL on the identical door: a plain 200 still classifies, so
        # the two unknowns above are the redirect and not a client that
        # stopped reading answers.
        ResetHandler.seen = []
        ResetHandler.consume_mode = codexresets.OUTCOME_RESET
        ok = codexresets.consume(self.account, "key-plain", url_base=self.base)
        self.assertEqual(ok["outcome"], codexresets.OUTCOME_RESET)
        self.assertTrue(ok["spent"])
        # MUST-HIT: the redirect target really does answer the vendor's own
        # success shape, so a follower WOULD have read `reset` off it and the
        # assertions above are measurements of the refusal to go there.
        _status, raw = codexresets._call(self.base + "/elsewhere", {})
        self.assertEqual(json.loads(raw.decode("utf-8"))["code"],
                         codexresets.OUTCOME_RESET)


class RedactionTest(FakeVendorCase):
    def test_a_vendor_body_quoting_the_credential_never_reaches_the_note(self):
        # THE MUST-HIT CONTROL for the sweep: the token IS in the response the
        # fake sends, so an arm that passes here is an arm whose subject had
        # the chance to leak and did not.
        ResetHandler.consume_mode = "echo500"
        res = codexresets.consume(self.account, "key-1", url_base=self.base)
        self.assertEqual(res["outcome"], codexresets.OUTCOME_UNKNOWN)
        self.assertNoSecret(res)
        self.assertIn("http_500", res["note"])

    def test_the_scrubber_blinds_a_secret_and_spares_a_short_one(self):
        text = "the call failed for %s on acct-fixture-team" % FAKE_TOKEN
        out = codexresets._redact(text, FAKE_TOKEN, "acct-fixture-team")
        self.assertNotIn(FAKE_TOKEN, out)
        self.assertNotIn("acct-fixture-team", out)
        self.assertEqual(codexresets._redact("keep tiny", "tiny"), "keep tiny",
                         "a short needle is too generic to blind-replace")

    def test_a_row_never_prints_a_full_account_id(self):
        row = {"account_id": "acct-0123456789abcdef", "email": None}
        self.assertNotIn(row["account_id"], codexresets.label(row))
        self.assertEqual(codexresets.label({"email": "team@fixture.invalid"}),
                         "team@fixture.invalid")


class LedgerTest(FakeVendorCase):
    def test_an_attempt_round_trips_carrying_no_credential(self):
        codexresets.record_attempt("team@fixture.invalid", "key-1",
                                   codexresets.OUTCOME_RESET, "auto",
                                   now=1000.0, path=self.ledger)
        rows = self.ledger_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["redeem_request_id"], "key-1")
        self.assertEqual(rows[0]["outcome"], codexresets.OUTCOME_RESET)
        self.assertNotIn("access_token", rows[0])
        self.assertNoSecret(rows[0], "ledger row")

    def test_a_row_the_reader_would_drop_is_never_written(self):  # noqa: VACUOUS_ASSERTION — the refused write is the subject, and the control below writes a keyed row and reads it back through the same ledger
        # The ledger grammar admits only rows carrying a non-empty id, and a
        # row it drops on READ is a journal that reports an empty history over
        # a ledger full of attempts — the one sentence that authorizes
        # spending another credit.
        self.assertFalse(codexresets.record_attempt(
            "team@fixture.invalid", "", codexresets.OUTCOME_RESET, "auto",
            now=1000.0, path=self.ledger))
        self.assertEqual(self.ledger_rows(), [])
        codexresets.record_attempt("control@fixture.invalid", "control-key",
                                   codexresets.OUTCOME_NOTHING, "auto",
                                   now=1.0, path=self.ledger)
        self.assertEqual([r["account"] for r in self.ledger_rows()],
                         ["control@fixture.invalid"],
                         "must-hit: this ledger records what it is given")

    def test_an_unreadable_ledger_is_unknown_never_an_empty_history(self):
        # "Nothing has been attempted" is the sentence that authorizes a
        # spend. A ledger that cannot be read has not said it.
        os.makedirs(self.ledger)
        rows, err = codexresets.attempts(self.ledger)
        self.assertIsNone(rows)
        self.assertTrue(err)
        self.assertIsNone(codexresets.attempts_for(rows, "anyone"))

    def test_attempts_for_selects_one_account(self):
        for name in ("a@fixture.invalid", "b@fixture.invalid"):
            codexresets.record_attempt(name, "key-" + name,
                                       codexresets.OUTCOME_RESET, "auto",
                                       now=1000.0, path=self.ledger)
        rows = self.ledger_rows()
        self.assertEqual([r["account"] for r in
                          codexresets.attempts_for(rows, "a@fixture.invalid")],
                         ["a@fixture.invalid"])


class DecisionTableTest(unittest.TestCase):
    """The policy, with no I/O anywhere in it."""

    NOW = 2_000_000.0

    def attempt(self, outcome, ago, key="key-old"):
        return {"account": "team@fixture.invalid", "ts": self.NOW - ago,
                "outcome": outcome, "redeem_request_id": key}

    def decide(self, row=None, age=0.0, credits=None, history=(), now=None,
               credential_error=None, cooling=None):
        return codexresets.decide(reading() if row is None else row, age,
                                  credits, history,
                                  self.NOW if now is None else now,
                                  credential_error, cooling_reset_at=cooling)

    # Built by the shipped constructor, never hand-written: a listing fixture
    # that invents its own fields cannot notice when the real one gains a
    # gate, which is exactly what `spendable` is.
    LISTED = codexresets._list_result(codexresets.LIST_OK, available=2)
    EMPTY = codexresets._list_result(codexresets.LIST_OK, available=0)
    UNREAD = codexresets._list_result(codexresets.LIST_UNAUTHORIZED)
    REDEEMED = codexresets._list_result(
        codexresets.LIST_OK, available=2,
        credits=[{"status": "redeemed"}, {"status": "redeemed"}])

    def test_the_whole_table(self):
        spent = reading()                       # recorded: 7d at 100%
        healthy = reading(weekly_pct=41)
        cases = (
            ("no reading at all", dict(row={}, credits=self.LISTED),
             codexresets.NO_ACT, codexresets.R_NO_READING),
            ("a reading of unknown age", dict(age=None, credits=self.LISTED),
             codexresets.NO_ACT, codexresets.R_STALE_READING),
            ("a reading past the freshness bound",
             dict(age=codexbudget.GATE_MAX_AGE_S + 1, credits=self.LISTED),
             codexresets.NO_ACT, codexresets.R_STALE_READING),
            ("an account whose budget could not be read",
             dict(row=codexbudget._unknown({"email": "x@fixture.invalid"},
                                           "needs_reauth", "rejected"),
                  credits=self.LISTED),
             codexresets.NO_ACT, codexresets.R_UNKNOWN_READING),
            ("a week with room left", dict(row=healthy, credits=self.LISTED),
             codexresets.NO_ACT, codexresets.R_NOT_EXHAUSTED),
            ("a wall the vendor calls a depleted member balance",
             dict(row=reading(reached="workspace_member_credits_depleted"),
                  credits=self.LISTED),
             codexresets.NO_ACT, codexresets.R_CREDITS_DEPLETED),
            ("a wall the vendor calls a depleted owner balance",
             dict(row=reading(reached="workspace_owner_credits_depleted"),
                  credits=self.LISTED),
             codexresets.NO_ACT, codexresets.R_CREDITS_DEPLETED),
            ("a wall the vendor calls something this helm does not know",
             dict(row=reading(reached="quota_moon_phase"),
                  credits=self.LISTED),
             codexresets.NO_ACT, codexresets.R_REACHED_UNRECOGNISED),
            ("a spent week the vendor gives no reason for",
             dict(row=reading(reached=None), credits=self.LISTED),
             codexresets.NO_ACT, codexresets.R_REACHED_UNKNOWN),
            ("a reading no single pooled credential serves",
             dict(credential_error=codexresets.R_NO_CREDENTIAL,
                  credits=self.LISTED),
             codexresets.NO_ACT, codexresets.R_NO_CREDENTIAL),
            ("a reading two pooled credentials could serve",
             dict(credential_error=codexresets.R_AMBIGUOUS_CREDENTIAL,
                  credits=self.LISTED),
             codexresets.NO_ACT, codexresets.R_AMBIGUOUS_CREDENTIAL),
            ("a natural reset inside the floor",
             dict(row=reading(weekly_resets_in=
                              codexresets.NATURAL_RESET_FLOOR_S - 60),
                  credits=self.LISTED),
             codexresets.NO_ACT, codexresets.R_NATURAL_RESET_NEAR),
            ("an unreadable attempt ledger",
             dict(history=None, credits=self.LISTED),
             codexresets.NO_ACT, codexresets.R_LEDGER_UNKNOWN),
            ("an attempt inside the cool-down",
             dict(history=[self.attempt(codexresets.OUTCOME_RESET, 60)],
                  credits=self.LISTED),
             codexresets.NO_ACT, codexresets.R_COOLDOWN),
            ("a balance that would not read",
             dict(credits=self.UNREAD),
             codexresets.NO_ACT, codexresets.R_CREDITS_UNREAD),
            ("a spent week and an empty balance", dict(credits=self.EMPTY),
             codexresets.NO_ACT, codexresets.R_NO_CREDIT),
            ("a count the entries contradict", dict(credits=self.REDEEMED),
             codexresets.NO_ACT, codexresets.R_NO_CREDIT),
            ("a spent week and no balance read yet", dict(credits=None),
             codexresets.NEED_CREDITS, codexresets.R_READY),
            ("a spent week and a credit in hand", dict(credits=self.LISTED),
             codexresets.CONSUME, codexresets.R_READY),
        )
        for name, kwargs, action, reason in cases:
            with self.subTest(case=name):
                d = self.decide(**kwargs)
                self.assertEqual((d.action, d.reason), (action, reason))
                self.assertTrue(d.detail, "every row owes a reader a sentence")
        # THE CONTROL that the fixture really is the exhausted world: the same
        # inputs with the one moved field answer differently above.
        self.assertEqual(codexresets.weekly_window(spent)["used_percent"], 100)
        # AND THE CONTROL THAT THE DEFAULT FIXTURE IS A RATE-LIMIT WALL. A
        # default built from the recorded TEAM body is a CREDITS-DEPLETED
        # wall, and every CONSUME row above would then be decided about a
        # world this policy must refuse — invisibly, because the answers look
        # identical until the reached-type gate is asked.
        self.assertEqual(spent["reached_type"], codexresets.RATE_LIMIT_REACHED)

    def test_a_five_hour_wall_alone_never_spends_a_credit(self):
        # The row's own state reads `exhausted` off limit_reached, and the
        # WEEK still has room. A credit here buys back a window that refills
        # by itself in hours.
        row = reading("team", weekly_pct=30, five_hour_pct=100,
                      reached=codexresets.RATE_LIMIT_REACHED)
        self.assertEqual(row["state"], "exhausted")
        d = self.decide(row=row, credits=self.LISTED)
        self.assertEqual(d.action, codexresets.NO_ACT)
        self.assertEqual(d.reason, codexresets.R_NOT_EXHAUSTED)

    def test_a_natural_reset_past_the_floor_does_not_block(self):
        d = self.decide(row=reading(weekly_resets_in=
                                    codexresets.NATURAL_RESET_FLOOR_S + 60),
                        credits=self.LISTED)
        self.assertEqual(d.action, codexresets.CONSUME)

    def test_the_natural_reset_floor_is_an_hour_and_the_arm_says_so(self):
        # LITERAL SECONDS, NOT THE CONSTANT. A floor arm deriving its
        # fixture FROM `NATURAL_RESET_FLOOR_S` moves with it, so the constant
        # is pinned by nothing and a policy that spends a credit on a window
        # reopening in thirty minutes passes. 1800 seconds is inside an hour
        # and 7200 is past it, and no arithmetic here reads the constant.
        inside = self.decide(row=reading(weekly_resets_in=1800),
                             credits=self.LISTED)
        self.assertEqual((inside.action, inside.reason),
                         (codexresets.NO_ACT,
                          codexresets.R_NATURAL_RESET_NEAR),
                         "a weekly window that reopens in 30 minutes buys 30 "
                         "minutes for an irreversible credit")
        past = self.decide(row=reading(weekly_resets_in=7200),
                           credits=self.LISTED)
        self.assertEqual(past.action, codexresets.CONSUME,
                         "must-hit: two hours out, the same call CONSUMEs — "
                         "so the refusal above is the floor and not a fixture "
                         "nothing can pass")

    def test_the_cool_down_is_two_hours_and_the_arm_says_so(self):
        # The same shape, the same cure: an attempt 90 minutes ago must
        # block, measured against a literal 5400 seconds rather than against
        # `CONSUME_COOLDOWN_S`, so a cool-down set to 0 goes red here.
        recent = self.attempt(codexresets.OUTCOME_RESET, 5400)
        self.assertEqual(self.decide(history=[recent],
                                     credits=self.LISTED).reason,
                         codexresets.R_COOLDOWN)
        old = self.attempt(codexresets.OUTCOME_RESET, 9000)
        self.assertEqual(self.decide(history=[old], credits=self.LISTED).action,
                         codexresets.CONSUME,
                         "must-hit: 2.5 hours out the same call CONSUMEs")

    def test_an_unauthorized_attempt_does_not_burn_the_cool_down(self):
        # A 401 moved no credit and is certain about it. Making the wall wait
        # two hours after the token is refreshed costs the owner exactly the
        # capacity the credit was meant to buy back. One pass out, the next
        # pass may re-drive it.
        refused = self.attempt(codexresets.OUTCOME_UNAUTHORIZED, 960)
        self.assertEqual(self.decide(history=[refused],
                                     credits=self.LISTED).action,
                         codexresets.CONSUME)
        # THE FLOOR THE EXEMPTION STILL KEEPS, on the same observable: the
        # same refusal a minute old belongs to the pass that made it, and a
        # SECOND request inside one pass is what an exemption with no window
        # of its own sends.
        floored = self.decide(history=[self.attempt(
            codexresets.OUTCOME_UNAUTHORIZED, 60)], credits=self.LISTED)
        self.assertEqual(floored.reason, codexresets.R_COOLDOWN)
        self.assertIn("floor", floored.detail,
                      "the owner is told WHICH window he is waiting on")
        # THE CONTROL, on the same observable: a 429 one pass out DOES burn
        # the full two hours, because that one IS the vendor asking for a
        # back-off.
        backoff = self.decide(history=[self.attempt(
            codexresets.OUTCOME_RATE_LIMITED, 960)], credits=self.LISTED)
        self.assertEqual(backoff.reason, codexresets.R_COOLDOWN)
        self.assertIn("cool-down", backoff.detail)

    def test_four_refusals_in_a_row_on_one_key_end_the_exemption(self):
        # THE EXEMPTION IS FOR A TRANSIENT REFUSAL — the minutes between a
        # token expiring and the rotation that cures it. A credential that
        # answers nothing but "refused" for an hour is a standing state, no
        # pass of this rung will move it, and re-driving it every pass is a
        # write verb repeated against a refused credential for nothing.
        rows, ago = [], 4000
        for _ in range(3):
            rows.append(self.attempt(codexresets.OUTCOME_PENDING, ago,
                                     key="key-401"))
            rows.append(self.attempt(codexresets.OUTCOME_UNAUTHORIZED, ago,
                                     key="key-401"))
            ago -= 1000
        self.assertEqual(self.decide(history=list(rows),
                                     credits=self.LISTED).action,
                         codexresets.CONSUME,
                         "must-hit: three refusals in a row still re-drive, "
                         "so the refusal below is the fourth and not the run")
        rows.append(self.attempt(codexresets.OUTCOME_PENDING, ago,
                                 key="key-401"))
        rows.append(self.attempt(codexresets.OUTCOME_UNAUTHORIZED, ago,
                                 key="key-401"))
        fourth = self.decide(history=list(rows), credits=self.LISTED)
        self.assertEqual(fourth.reason, codexresets.R_COOLDOWN)
        self.assertIn("standing refusal", fourth.detail)
        # AND THE RUN IS CONSECUTIVE: an answer the vendor DID give that is
        # not a refusal sets it back to zero, so a credential that is refused,
        # answers something else, and is refused again never accumulates
        # toward the fallback. The write-ahead rows neither count nor break
        # it — they are this module's own rows, not the vendor's.
        broken = list(rows)
        broken.insert(-2, self.attempt(codexresets.OUTCOME_UNKNOWN, 1500,
                                       key="key-401"))
        self.assertEqual(codexresets.refusal_run(broken, "key-401"), 1)
        self.assertEqual(self.decide(history=broken,
                                     credits=self.LISTED).action,
                         codexresets.CONSUME)

    def test_the_exemptions_two_bounds_are_the_cadence_and_the_age_bound(self):  # noqa: VACUOUS_ASSERTION — the observable is the DERIVATION of two shipped constants from the modules that own them; the behavioural control below drives `decide` at the exact boundary
        # THE FLOOR IS ONE PASS OF THE RUNG THAT DRIVES THIS, and the run
        # bound is that floor counted up to the age a budget reading may carry
        # and still be acted on — one hour, four passes. Both numbers are read
        # from the modules that USE them, so this arm cannot drift into
        # comparing two literals nobody consults.
        self.assertEqual(codexresets.REFUSAL_FLOOR_S, proxywatch.INTERVAL_S)
        self.assertEqual(
            codexresets.REFUSAL_RUN_MAX * codexresets.REFUSAL_FLOOR_S,
            codexbudget.GATE_MAX_AGE_S)
        # The floor is load-bearing at the exact boundary: one second inside
        # it still blocks, and AT it the next pass re-drives.
        inside = self.attempt(codexresets.OUTCOME_UNAUTHORIZED,
                              codexresets.REFUSAL_FLOOR_S - 1)
        self.assertEqual(self.decide(history=[inside],
                                     credits=self.LISTED).reason,
                         codexresets.R_COOLDOWN)
        at = self.attempt(codexresets.OUTCOME_UNAUTHORIZED,
                          codexresets.REFUSAL_FLOOR_S)
        self.assertEqual(self.decide(history=[at],
                                     credits=self.LISTED).action,
                         codexresets.CONSUME)

    def test_a_write_ahead_row_its_own_pass_settled_is_not_an_attempt(self):
        # THE SHAPE THE ACTING PATH REALLY WRITES: `pending` before the send
        # and the outcome after it, one key, one instant — here one pass back,
        # so the floor a refusal keeps is not what answers.
        pending = self.attempt(codexresets.OUTCOME_PENDING, 960, key="key-401")
        refused = self.attempt(codexresets.OUTCOME_UNAUTHORIZED, 960,
                               key="key-401")
        self.assertEqual(self.decide(history=[pending, refused],
                                     credits=self.LISTED).action,
                         codexresets.CONSUME)
        # CONTROL: the pending row ALONE — nobody survived to settle it — is
        # the last word about its key and still burns the cool-down.
        self.assertEqual(self.decide(history=[pending],
                                     credits=self.LISTED).reason,
                         codexresets.R_COOLDOWN)
        # AND a pending row settled by a NON-exempt outcome burns it too, so
        # the supersession is about WHICH row speaks, never about excusing one.
        spent = self.attempt(codexresets.OUTCOME_RESET, 60, key="key-401")
        self.assertEqual(self.decide(history=[pending, spent],
                                     credits=self.LISTED).reason,
                         codexresets.R_COOLDOWN)

    def test_a_refusal_never_closes_a_key_the_vendor_never_answered(self):
        # A 401 ON A RE-DRIVE ANSWERS NOTHING ABOUT THE FIRST REQUEST: the
        # credential was refused before any redemption, so the earlier unknown
        # is still open and the next attempt owes it the SAME key.
        far = codexresets.CONSUME_COOLDOWN_S + 120
        unknown = self.attempt(codexresets.OUTCOME_UNKNOWN, far, key="key-u")
        refused = self.attempt(codexresets.OUTCOME_UNAUTHORIZED, far - 60,
                               key="key-u")
        self.assertEqual(self.decide(history=[unknown, refused],
                                     credits=self.LISTED).reuse_key, "key-u")
        # CONTROL on the same history: a VENDOR CODE for that key closes it,
        # and the next attempt mints a fresh one.
        answered = self.attempt(codexresets.OUTCOME_NOTHING, far - 60,
                                key="key-u")
        self.assertIsNone(self.decide(history=[unknown, answered],
                                      credits=self.LISTED).reuse_key)

    def test_only_the_newest_key_is_asked_and_an_older_open_one_is_not_revived(self):
        # TWO KEYS, which is what this rule needs to be visible at all: an
        # older key the vendor never answered about, and a newer key a vendor
        # code closed. Only the NEWEST key is asked — the older one was
        # superseded by an attempt this policy allowed, and reviving it would
        # re-drive a question that is cool-downs old against a wall nobody
        # measured since.
        rows = [self.attempt(codexresets.OUTCOME_UNKNOWN, 9000, key="key-old"),
                self.attempt(codexresets.OUTCOME_ALREADY, 8000, key="key-new")]
        self.assertIsNone(codexresets.open_key(rows))
        # MUST-HIT through the identical call: with the NEWEST key still
        # unresolved, the same two-key history DOES answer — with the newest
        # key and never the older one — so the None above is the rule and not
        # a function that answers nothing.
        rows[1] = self.attempt(codexresets.OUTCOME_UNKNOWN, 8000,
                               key="key-new")
        self.assertEqual(codexresets.open_key(rows), "key-new")

    def test_a_2xx_helm_cannot_classify_keeps_its_key(self):
        far = codexresets.CONSUME_COOLDOWN_S + 60
        # THE CONTROL, unconditional and on the same observable: the 5xx leg
        # keeps its key, so a green loop below is a finding about the two 2xx
        # branches rather than a field that always fills.
        self.assertEqual(self.decide(
            history=[self.attempt(codexresets.OUTCOME_UNKNOWN, far,
                                  key="key-5xx")],
            credits=self.LISTED).reuse_key, "key-5xx")
        for outcome in (codexresets.OUTCOME_MALFORMED,
                        codexresets.OUTCOME_UNKNOWN_CODE):
            with self.subTest(outcome=outcome):
                row = self.attempt(outcome, far, key="key-2xx")
                d = self.decide(history=[row], credits=self.LISTED)
                self.assertEqual(d.action, codexresets.CONSUME)
                self.assertEqual(d.reuse_key, "key-2xx")

    def test_the_cool_down_expires(self):
        old = self.attempt(codexresets.OUTCOME_RESET,
                           codexresets.CONSUME_COOLDOWN_S + 1)
        self.assertEqual(self.decide(history=[old], credits=self.LISTED).action,
                         codexresets.CONSUME)

    def test_the_cool_down_outlives_the_readings_own_staleness_bound(self):  # noqa: VACUOUS_ASSERTION — the observable is the ORDER of two shipped constants; the behavioural control beside it drives `decide` at the exact boundary
        # The reading may be an hour old by contract; a cool-down shorter than
        # that lets ONE wall be seen twice and spend two credits. Both numbers
        # are read from the modules that USE them, so a rename cannot leave
        # this arm comparing two names nobody consults.
        self.assertGreater(codexresets.CONSUME_COOLDOWN_S,
                           codexbudget.GATE_MAX_AGE_S)
        # The bound is load-bearing at the exact boundary: an attempt one
        # second inside the staleness bound still blocks.
        edge = self.attempt(codexresets.OUTCOME_RESET,
                            codexbudget.GATE_MAX_AGE_S)
        self.assertEqual(self.decide(history=[edge],
                                     credits=self.LISTED).reason,
                         codexresets.R_COOLDOWN)

    def test_an_unknown_outcome_is_re_driven_under_the_same_key(self):
        stale_unknown = self.attempt(codexresets.OUTCOME_UNKNOWN,
                                     codexresets.CONSUME_COOLDOWN_S + 1,
                                     key="key-unknown")
        d = self.decide(history=[stale_unknown], credits=self.LISTED)
        self.assertEqual(d.action, codexresets.CONSUME)
        self.assertEqual(d.reuse_key, "key-unknown")

    def test_a_settled_history_mints_a_fresh_key(self):  # noqa: VACUOUS_ASSERTION — the None IS the subject, and the second half asserts the same field FILLS for an unresolved history
        settled = self.attempt(codexresets.OUTCOME_NOTHING,
                               codexresets.CONSUME_COOLDOWN_S + 1)
        self.assertIsNone(self.decide(history=[settled],
                                      credits=self.LISTED).reuse_key)
        # The control on the same observable: the identical history with an
        # UNRESOLVED newest row DOES yield a key, so the None above is a
        # decision rather than a field nothing ever fills.
        unresolved = dict(settled, outcome=codexresets.OUTCOME_PENDING)
        self.assertEqual(self.decide(history=[unresolved],
                                     credits=self.LISTED).reuse_key,
                         settled["redeem_request_id"])

    def test_the_weekly_window_is_found_by_length_on_both_plan_shapes(self):  # noqa: VACUOUS_ASSERTION — the None is the control (a row with no windows); the loop is the positive half
        # Unconditional: the finder returns None for a row with no windows,
        # so a green loop below is a finding rather than a function that
        # answers the same thing to everything.
        self.assertIsNone(codexresets.weekly_window({"windows": []}))
        for shape in ("team", "pro"):
            with self.subTest(shape=shape):
                w = codexresets.weekly_window(reading(shape))
                self.assertIsNotNone(w)
                self.assertGreaterEqual(w["seconds"],
                                        codexresets.WEEKLY_MIN_SECONDS)


class ResetPassTest(FakeVendorCase):
    """The unattended rung, end to end, against the local vendor."""

    def pass_(self, rows=None, **kwargs):
        rows = [reading()] if rows is None else rows
        # NOT `setdefault`: Python evaluates the default EAGERLY, so an arm
        # that hands in its own accounts would still pay for deriving them
        # from rows — and an arm whose rows are deliberately undecidable would
        # raise inside the fixture instead of inside the subject.
        if "accounts" not in kwargs:
            kwargs["accounts"] = [account_of(r) for r in rows]
        kwargs.setdefault("probe", lambda _a: reading(weekly_pct=0))
        kwargs.setdefault("ledger", self.ledger)
        return codexresets.reset_pass(rows, reading_age_s=0.0,
                                      url_base=self.base, **kwargs)

    def test_a_spent_week_consumes_once_journals_and_confirms(self):
        rows = self.pass_()
        self.assertEqual(rows[0]["action"], codexresets.CONSUME)
        self.assertEqual(rows[0]["outcome"], codexresets.OUTCOME_RESET)
        self.assertEqual(rows[0]["available"], 2)
        self.assertEqual(rows[0]["after_weekly_pct"], 0,
                         "the room hears the before AND the after")
        # THE KEY IS ON DISK BEFORE THE REQUEST AND THE OUTCOME AFTER IT, so
        # a process killed between the two still leaves the key behind.
        journal = self.ledger_rows()
        self.assertEqual([r["outcome"] for r in journal],
                         [codexresets.OUTCOME_PENDING,
                          codexresets.OUTCOME_RESET])
        self.assertEqual({r["redeem_request_id"] for r in journal},
                         {rows[0]["key"]})
        self.assertFalse(journal[0]["reused_key"])
        self.assertNoSecret(rows)
        self.assertNoSecret(journal)

    def test_a_healthy_week_never_touches_the_vendor(self):
        rows = self.pass_([reading(weekly_pct=41)])
        self.assertEqual(rows[0]["action"], codexresets.NO_ACT)
        self.assertEqual(rows[0]["reason"], codexresets.R_NOT_EXHAUSTED)
        self.assertEqual(ResetHandler.seen, [],
                         "an account with room costs no vendor round-trip")
        codexresets.list_credits(self.account, url_base=self.base)
        self.assertTrue(ResetHandler.seen,
                        "must-hit: the recorder fills when it is called")
        self.assertEqual(self.ledger_rows(), [])
        codexresets.record_attempt("control@fixture.invalid", "control-key",
                                   codexresets.OUTCOME_NOTHING, "auto",
                                   now=1.0, path=self.ledger)
        self.assertEqual([r["account"] for r in self.ledger_rows()],
                         ["control@fixture.invalid"],
                         "must-hit: this ledger records what it is given")

    def test_the_journal_blocks_the_second_pass_on_one_exhaustion(self):
        first = self.pass_()
        self.assertEqual(first[0]["outcome"], codexresets.OUTCOME_RESET)
        second = self.pass_()
        self.assertEqual(second[0]["action"], codexresets.NO_ACT)
        self.assertEqual(second[0]["reason"], codexresets.R_COOLDOWN)
        self.assertEqual(len(self.ledger_rows()), 2,
                         "one exhaustion, one attempt (write-ahead + outcome)")

    def test_an_unknown_outcome_is_journaled_and_re_driven_on_one_key(self):
        ResetHandler.consume_mode = "500"
        first = self.pass_()
        self.assertEqual(first[0]["outcome"], codexresets.OUTCOME_UNKNOWN)
        key = first[0]["key"]
        # Past the cool-down the rung tries again — with the SAME key, so the
        # vendor either reports it already redeemed or redeems it once.
        ResetHandler.consume_mode = codexresets.OUTCOME_ALREADY
        later = self.pass_(now=time.time() + codexresets.CONSUME_COOLDOWN_S + 60)
        self.assertEqual(later[0]["outcome"], codexresets.OUTCOME_ALREADY)
        self.assertTrue(later[0]["reused_key"])
        self.assertEqual(later[0]["key"], key)
        sent = [s["body"]["redeem_request_id"] for s in ResetHandler.seen
                if s["path"] == codexresets.CONSUME_PATH]
        self.assertEqual(sent, [key, key],
                         "a fresh key after an unknown outcome is the second "
                         "credit this rule exists to save")

    def _keys_two_passes(self, mode):
        """Two ELIGIBLE passes over one wall, the vendor answering `mode`:
        (first row, later row, the keys the consume endpoint really got)."""
        ResetHandler.consume_mode = mode
        first = self.pass_()
        later = self.pass_(now=time.time()
                           + codexresets.CONSUME_COOLDOWN_S + 60)
        return first[0], later[0], [s["body"]["redeem_request_id"]
                                    for s in ResetHandler.seen
                                    if s["path"] == codexresets.CONSUME_PATH]

    def test_a_2xx_helm_cannot_classify_is_re_driven_on_one_key(self):  # noqa: VACUOUS_ASSERTION — the unconditional must-hit after the loop counts the two POSTs the subtests drove, so a loop that never ran reads RED
        """A 200 FROM THE REDEMPTION ENDPOINT IS WHERE A CREDIT MOST LIKELY
        LEFT THE BALANCE. Classifying an unreadable body or an unknown code as
        "settled, nothing spent" drops the idempotency key, and the next
        eligible pass mints a new one — one wall, two irreversible credits."""
        for mode, outcome in (("malformed", codexresets.OUTCOME_MALFORMED),
                              ("weird", codexresets.OUTCOME_UNKNOWN_CODE)):
            with self.subTest(mode=mode):
                ResetHandler.seen = []
                self.ledger = os.path.join(self.tmp, "k-%s.jsonl" % mode)
                first, later, sent = self._keys_two_passes(mode)
                self.assertEqual(first["outcome"], outcome)
                self.assertIsNone(first["spent"])
                self.assertEqual(len(sent), 2, "the second pass never sent")
                self.assertEqual(set(sent), {first["key"]},
                                 "a fresh key after a 2xx helm could not "
                                 "read is the second credit this rule exists "
                                 "to save")
                self.assertTrue(later["reused_key"])
                self.assertEqual({r["redeem_request_id"]
                                  for r in self.ledger_rows()},
                                 {first["key"]})
        # MUST-HIT, unconditional: `seen` is cleared at the top of each
        # subTest, so this counts the LAST subtest's two POSTs — a loop that
        # never ran at all leaves the recorder empty and reads RED here.
        self.assertEqual(len([s for s in ResetHandler.seen
                              if s["path"] == codexresets.CONSUME_PATH]), 2)

    def test_a_crash_between_the_post_and_the_outcome_row_re_drives_one_key(self):
        # THE CONTROL FOR THE COOL-DOWN'S LAST-WORD READING: a pending row
        # nobody survived to settle is the last word about its key, so it
        # still holds the wall — and past the cool-down the SAME key goes out.
        with mock.patch.object(codexresets, "record_attempt",
                               return_value=False) as lost:
            first = self.pass_()
        self.assertTrue(lost.called, "the arm must reach the outcome row")
        self.assertEqual([r["outcome"] for r in self.ledger_rows()],
                         [codexresets.OUTCOME_PENDING])
        inside = self.pass_()
        self.assertEqual(inside[0]["reason"], codexresets.R_COOLDOWN)
        later = self.pass_(now=time.time()
                           + codexresets.CONSUME_COOLDOWN_S + 60)
        self.assertTrue(later[0]["reused_key"])
        self.assertEqual(later[0]["key"], first[0]["key"])
        self.assertEqual([s["body"]["redeem_request_id"]
                          for s in ResetHandler.seen
                          if s["path"] == codexresets.CONSUME_PATH],
                         [first[0]["key"], first[0]["key"]])

    def test_a_refused_credential_does_not_burn_the_cool_down_in_a_pass(self):
        """THE EXEMPTION, MEASURED WHERE IT HAS TO HOLD. A 401 moved no credit
        and is certain about it, so the wall must not wait two hours after the
        token is refreshed. Its arm was a hand-built one-row history the
        acting path never produces: the real pass writes a `pending` row
        BEFORE the request leaves, and that row — which names no outcome, so
        no exemption can reach it — burned the two hours anyway."""
        ResetHandler.consume_mode = "401"
        first = self.pass_()
        self.assertEqual(first[0]["outcome"], codexresets.OUTCOME_UNAUTHORIZED)
        self.assertEqual([r["outcome"] for r in self.ledger_rows()],
                         [codexresets.OUTCOME_PENDING,
                          codexresets.OUTCOME_UNAUTHORIZED],
                         "the pass leaves a write-ahead row beside the "
                         "refusal — that is what the cool-down must see past")
        # AND THE FLOOR UNDER THE EXEMPTION, in the same pass shape: a second
        # attempt inside the pass that made the first is refused, so "exempt
        # from the cool-down" never means "as often as anybody asks".
        ResetHandler.consume_mode = "reset"
        inside = self.pass_()
        self.assertEqual(inside[0]["reason"], codexresets.R_COOLDOWN)
        self.assertIn("floor", inside[0]["detail"])
        second = self.pass_(now=time.time() + codexresets.REFUSAL_FLOOR_S)
        self.assertNotEqual(second[0]["reason"], codexresets.R_COOLDOWN)
        self.assertEqual(second[0]["outcome"], codexresets.OUTCOME_RESET)
        # AND IT RE-DRIVES THE REFUSED KEY rather than minting one: a 401 is
        # the vendor refusing the credential, never an answer about whether
        # that request redeemed.
        self.assertTrue(second[0]["reused_key"])
        self.assertEqual(second[0]["key"], first[0]["key"])
        # THE CONTROL, same two passes, same clock, one outcome changed: a 429
        # is NOT exempt and the second pass waits.
        ResetHandler.consume_mode = "429"
        other = os.path.join(self.tmp, "cd-429.jsonl")
        self.assertEqual(self.pass_(ledger=other)[0]["outcome"],
                         codexresets.OUTCOME_RATE_LIMITED)
        self.assertEqual(self.pass_(ledger=other)[0]["reason"],
                         codexresets.R_COOLDOWN)

    def test_a_journal_that_will_not_write_stops_the_send(self):
        # The ledger is what holds one wall to one credit. Acting while it
        # cannot record the act is how a crash becomes a second redemption.
        # The refusal is planted AT THE APPEND, not with a permission bit: the
        # READ must still succeed, or the arm would measure the unreadable-
        # ledger gate one rung earlier and never reach this branch at all.
        with mock.patch.object(codexresets.eventledger, "append_unlocked",
                               return_value=False) as refused:
            rows = self.pass_()
        self.assertTrue(refused.called, "the arm must reach the write-ahead")
        self.assertEqual(rows[0]["reason"], codexresets.R_JOURNAL_UNWRITABLE)
        self.assertIsNone(rows[0]["outcome"])
        self.assertIsNone(rows[0]["key"])
        self.assertEqual(self.consume_requests(), [])
        codexresets.consume(self.account, "control-key", url_base=self.base)
        self.assertEqual([r["body"]["redeem_request_id"]
                          for r in self.consume_requests()], ["control-key"],
                         "must-hit: the recorder registers a consume, so the "
                         "emptiness above is a measurement")
        self.assertTrue(codexresets.wanted_and_could_not(rows))

    def test_a_settled_row_over_an_older_unresolved_one_mints_a_fresh_key(self):
        # ONE KEY, TWO ROWS: the vendor answered `already_redeemed` about the
        # request it had left UNKNOWN, so that key is closed and the next
        # attempt owes a fresh one. (The rule that only the NEWEST KEY is
        # asked needs two keys to be visible at all, and it has its own arm:
        # DecisionTableTest.test_only_the_newest_key_is_asked_and_an_older_
        # open_one_is_not_revived.)
        member = codexresets.member_id(account_of(reading()))
        codexresets.record_attempt(member, "key-old",
                                   codexresets.OUTCOME_UNKNOWN, "auto",
                                   now=1.0, path=self.ledger)
        codexresets.record_attempt(member, "key-old",
                                   codexresets.OUTCOME_ALREADY, "auto",
                                   now=2.0, path=self.ledger)
        rows = self.pass_(now=time.time())
        self.assertEqual(rows[0]["outcome"], codexresets.OUTCOME_RESET)
        self.assertFalse(rows[0]["reused_key"])
        self.assertNotEqual(rows[0]["key"], "key-old")

    def test_one_pass_spends_one_credit_however_many_walls_it_finds(self):  # noqa: VACUOUS_ASSERTION — the one-send count IS the subject, and the must-hit below raises the cap and takes BOTH walls through the same recorder
        # Several pooled accounts reach the weekly wall within hours of each
        # other, and the per-account cool-down cannot see a sibling. Without
        # a pool-wide budget the first unattended pass spends every credit it
        # can reach, in one breath, before anyone has watched it spend one.
        walls = [reading(), reading("team",
                                    reached=codexresets.RATE_LIMIT_REACHED)]
        rows = self.pass_(walls)
        outcomes = sorted(r["outcome"] or r["reason"] for r in rows)
        self.assertEqual(outcomes, sorted([codexresets.OUTCOME_RESET,
                                           codexresets.R_PASS_BUDGET]))
        self.assertEqual(len(self.consume_requests()), 1)
        # The deferred account was READY, and the row says so rather than
        # reading like a healthy window.
        deferred = next(r for r in rows if r["reason"] == codexresets.R_PASS_BUDGET)
        self.assertEqual(deferred["weekly_pct"], 100)
        self.assertIn("next pass", deferred["detail"])
        self.assertEqual(len(self.ledger_rows()), 2,
                         "only the account that was acted on is journaled")
        # THE MUST-HIT ON THE SAME OBSERVABLE: raise the budget, hand the pass
        # a ledger with no cool-down in it, and BOTH walls are taken. The one
        # above is the cap doing the limiting, not the fixture.
        with mock.patch.object(codexresets, "MAX_CONSUMES_PER_PASS", 2):
            both = self.pass_(walls,
                              ledger=os.path.join(self.tmp, "second.jsonl"))
        self.assertEqual([r["outcome"] for r in both],
                         [codexresets.OUTCOME_RESET, codexresets.OUTCOME_RESET])
        self.assertEqual(len(self.consume_requests()), 3)

    def test_one_undecidable_account_costs_the_others_nothing(self):
        # The pass reports on a POOL. A reading helm cannot even decide about
        # must not take down the answer for the account that was about to have
        # its wall cleared.
        rows = self.pass_(["not-a-budget-row", reading()],
                          accounts=[account_of(reading())])
        by_reason = {r["reason"]: r for r in rows}
        self.assertEqual(sorted(by_reason), sorted([codexresets.R_RUNG_ERROR,
                                                    codexresets.R_READY]))
        self.assertEqual(by_reason[codexresets.R_READY]["outcome"],
                         codexresets.OUTCOME_RESET)
        self.assertEqual(by_reason[codexresets.R_RUNG_ERROR]["account"], "?")
        self.assertNoSecret(rows)

    def test_a_dry_run_predicts_the_cap_it_would_hit(self):
        # A dry run answers what the pass WOULD do, and what the pass would do
        # is take one wall and defer the rest. Three CONSUME rows would
        # describe a burst that cannot happen.
        rows = self.pass_([reading(),
                           reading("team",
                                   reached=codexresets.RATE_LIMIT_REACHED)],
                          dry_run=True)
        self.assertEqual(sorted(r["reason"] for r in rows),
                         sorted([codexresets.R_READY,
                                 codexresets.R_PASS_BUDGET]))
        self.assertEqual([r["action"] for r in rows],
                         [codexresets.CONSUME, codexresets.NO_ACT])

    def test_a_dry_run_spends_nothing_and_its_banner_says_what_it_sends(self):
        """A dry run IS NOT OFFLINE: every account the local gates do not
        refuse costs a read-only vendor GET. The banner is held against that
        measurement rather than against itself."""
        rows = self.pass_(dry_run=True)
        self.assertEqual(rows[0]["action"], codexresets.CONSUME)
        self.assertEqual([r["path"] for r in ResetHandler.seen],
                         [codexresets.LIST_PATH],
                         "one listing went out, and no consume did")
        banner = codexresets.DRY_RUN_BANNER % 3
        self.assertIn("NOTHING IS SPENT", banner)
        self.assertIn("listing IS sent", banner,
                      "the banner must not let a reader infer an offline run")

    def test_a_dry_run_decides_and_sends_nothing(self):
        rows = self.pass_(dry_run=True)
        self.assertEqual(rows[0]["action"], codexresets.CONSUME)
        self.assertIsNone(rows[0]["outcome"])
        self.assertEqual(self.ledger_rows(), [])
        codexresets.record_attempt("control@fixture.invalid", "control-key",
                                   codexresets.OUTCOME_NOTHING, "auto",
                                   now=1.0, path=self.ledger)
        self.assertEqual([r["account"] for r in self.ledger_rows()],
                         ["control@fixture.invalid"],
                         "must-hit: this ledger records what it is given")
        self.assertEqual(self.consume_requests(), [])
        codexresets.consume(self.account, "control-key", url_base=self.base)
        self.assertEqual([r["body"]["redeem_request_id"]
                          for r in self.consume_requests()], ["control-key"],
                         "must-hit: the recorder registers a consume, so the "
                         "emptiness above is a measurement")

    def test_an_account_with_no_pooled_credential_is_reported_not_guessed(self):
        rows = self.pass_(accounts=[])
        self.assertEqual(rows[0]["reason"], codexresets.R_NO_CREDENTIAL)
        self.assertIsNone(rows[0]["outcome"])
        self.assertEqual(ResetHandler.seen, [],
                         "a reading helm cannot bind to a credential costs no "
                         "vendor round-trip either")
        codexresets.list_credits(self.account, url_base=self.base)
        self.assertTrue(ResetHandler.seen,
                        "must-hit: the recorder fills when it is called, so "
                        "the emptiness above is a measurement")

    def test_an_empty_balance_is_wanted_and_could_not(self):
        ResetHandler.list_mode = "zero"
        rows = self.pass_()
        self.assertEqual(rows[0]["reason"], codexresets.R_NO_CREDIT)
        self.assertEqual(rows[0]["available"], 0)
        self.assertTrue(codexresets.wanted_and_could_not(rows))
        # A wall helm could not clear is not an attempt.
        self.assertEqual(self.ledger_rows(), [])
        codexresets.record_attempt("control@fixture.invalid", "control-key",
                                   codexresets.OUTCOME_NOTHING, "auto",
                                   now=1.0, path=self.ledger)
        self.assertEqual([r["account"] for r in self.ledger_rows()],
                         ["control@fixture.invalid"],
                         "must-hit: this ledger records what it is given")

    def test_an_unreadable_balance_is_wanted_and_could_not(self):
        ResetHandler.list_mode = "boolean"      # available_count unreadable
        rows = self.pass_()
        self.assertEqual(rows[0]["reason"], codexresets.R_CREDITS_UNREAD)
        self.assertIsNone(rows[0]["available"])
        self.assertTrue(codexresets.wanted_and_could_not(rows))

    def test_the_room_hears_a_spend_and_a_blocked_wall_and_nothing_else(self):
        acted = self.pass_()
        body = codexresets.pass_notice(acted)
        self.assertIn(codexresets.NOTICE_TAG, body)
        self.assertIn("pro@fixture.invalid", body)
        self.assertNotIn(FAKE_TOKEN, body)
        quiet = codexresets.pass_notice(self.pass_([reading(weekly_pct=41)]))
        self.assertIsNone(quiet, "an ordinary pass says nothing")
        self.assertEqual(codexresets.pass_lines(
            [{"account": "x", "action": codexresets.NO_ACT,
              "reason": codexresets.R_NOT_EXHAUSTED}]), [])


class ProxywatchRungTest(unittest.TestCase):
    def test_the_rung_swallows_a_raising_client_and_never_moves_the_pass(self):  # noqa: VACUOUS_ASSERTION — the None IS the swallow, and the working client above answers rows through the identical call
        # The control first: the SAME call with a working client answers rows,
        # so the None below is the swallow and not a rung that never runs.
        with mock.patch.object(codexresets, "reset_pass", return_value=[]):
            self.assertEqual(proxywatch._codex_reset_pass([{"email": "x"}]), [])
        with mock.patch.object(codexresets, "reset_pass",
                               side_effect=RuntimeError("vendor exploded")):
            self.assertIsNone(proxywatch._codex_reset_pass([{"email": "x"}]))

    def test_the_rungs_own_error_print_is_scrubbed(self):
        """The one string in this feature assembled from an exception nobody
        inspected. An exception raised under a vendor client can carry the
        url, the header or the token."""
        pool = [{"access_token": FAKE_TOKEN, "account_id": "acct-secret-0001"}]
        err = io.StringIO()
        with mock.patch.object(codexresets, "_pool_accounts",
                               return_value=(pool, True)), \
                mock.patch.object(codexresets, "reset_pass",
                                  side_effect=RuntimeError(
                                      "upstream said " + FAKE_TOKEN
                                      + " for acct-secret-0001")), \
                contextlib.redirect_stderr(err):
            proxywatch._codex_reset_pass([{"email": "x"}])
        text = err.getvalue()
        self.assertIn("RuntimeError", text,
                      "the rung still says what broke")
        self.assertIn("upstream said", text)
        self.assertNotIn(FAKE_TOKEN, text)
        self.assertNotIn("acct-secret-0001", text)

    def test_an_error_over_an_unreadable_pool_keeps_only_the_class_name(self):
        """NOTHING KNOWN TO BLIND IS NOT NOTHING TO HIDE. With no pool to
        learn the secrets from, the message is dropped rather than printed on
        the hope that it is clean."""
        err = io.StringIO()
        with mock.patch.object(codexresets, "_pool_accounts",
                               side_effect=OSError("pool dir is gone")), \
                mock.patch.object(codexresets, "reset_pass",
                                  side_effect=RuntimeError(
                                      "carrying " + FAKE_TOKEN)), \
                contextlib.redirect_stderr(err):
            proxywatch._codex_reset_pass([{"email": "x"}])
        self.assertIn("RuntimeError", err.getvalue())
        self.assertNotIn(FAKE_TOKEN, err.getvalue())
        self.assertNotIn("carrying", err.getvalue())

    def test_an_unread_pool_runs_no_rung_at_all(self):  # noqa: VACUOUS_ASSERTION — the not-called half is the subject and the must-hit below proves rows DO reach the same spy
        # None is "the pool was not read", and an unread account is never an
        # exhausted one — so nothing may be spent on its behalf.
        with mock.patch.object(codexresets, "reset_pass",
                               return_value=[]) as spent:
            self.assertIsNone(proxywatch._codex_reset_pass(None))
            spent.assert_not_called()
            # Must-hit on the same observable: rows DO reach it.
            proxywatch._codex_reset_pass([{"email": "x"}])
        self.assertEqual(spent.call_count, 1)

    def test_the_pass_reads_the_rows_it_just_probed(self):
        budget = [{"email": "x"}]
        with mock.patch.object(codexresets, "reset_pass",
                               return_value=[]) as rung:
            proxywatch._codex_reset_pass(budget)
        self.assertEqual(rung.call_args.args, (budget,),
                         "the rung decides on the rows this pass just probed")
        self.assertEqual(rung.call_args.kwargs["reading_age_s"], 0.0)



TOKEN_A = "member-A-token-0123456789abcdefghijklmnopqrstuv"
TOKEN_B = "member-B-token-0123456789abcdefghijklmnopqrstuv"
TOKEN_C = "member-C-token-0123456789abcdefghijklmnopqrstuv"


class WorkspaceIdentityBase(FakeVendorCase):
    """One ChatGPT Team workspace with three members: `members` reads them,
    all on one account id and each with its own user id and pool file, and
    `pool` pairs them with three tokens.

    THIS CLASS HOLDS NO `test_*` METHOD, and that is its contract. unittest
    collects every inherited `test_*` again under each subclass's own id, so
    an arm written here runs once per subclass. A class that needs this
    fixture subclasses THIS class; its arms go in the subclass."""

    def members(self):
        """One workspace, three members, three tokens, three pool files."""
        return [reading(email="member%s@fixture.invalid" % n,
                        account_id="acct-one-workspace", user_id="user-%s" % n,
                        file="codex-%s.json" % n)
                for n in ("a", "b", "c")]

    def pool(self, rows, tokens=(TOKEN_A, TOKEN_B, TOKEN_C)):
        return [account_of(r, t) for r, t in zip(rows, tokens)]


class WorkspaceIdentityTest(WorkspaceIdentityBase):
    """WHOSE CREDENTIAL IS THIS. On a ChatGPT Team plan the account id is the
    WORKSPACE id, shared by every member (`codexhomes._member_key`, measured
    on this fleet: three of six pooled rows carry one account id). A rung that
    resolves on the account id redeems the walled member's wall with a
    SIBLING'S bearer — spending the sibling's credit, clearing the sibling's
    windows, and leaving the walled account walled.

    A subclass of THIS class runs every arm below again under its own id,
    which is right only for one that changes the fixture those arms read. A
    class that only needs the fixture subclasses WorkspaceIdentityBase."""

    def test_control_distinct_account_ids_resolve_correctly(self):  # noqa: VACUOUS_ASSERTION — the arm IS the positive: it asserts a RESOLVED credential's email, and there is no absence in it
        """MUST-HIT: where the ids already differ the matcher was right, so a
        red arm below is about the shared workspace id and not about a broken
        fixture."""
        a, b = reading("pro"), reading("team")
        pool = [account_of(a, TOKEN_A), account_of(b, TOKEN_B)]
        self.assertEqual(codexresets.resolve_credential(pool, b)[0]["email"],
                         b["email"])

    def test_three_members_of_one_workspace_resolve_to_three_credentials(self):
        rows = self.members()
        pool = self.pool(rows)
        got = [codexresets.resolve_credential(pool, r) for r in rows]
        self.assertEqual([a["access_token"] for a, _err in got],
                         [TOKEN_A, TOKEN_B, TOKEN_C],
                         "each reading must resolve to ITS member's token")
        self.assertEqual([err for _a, err in got], [None, None, None])
        # And the ledger key follows the member, not the workspace.
        self.assertEqual(len({codexresets.member_id(a) for a in pool}), 3)

    def test_the_pass_redeems_with_the_credential_of_the_account_it_names(self):
        rows = self.members()
        pool = self.pool(rows)
        # memberB is the one at the weekly wall; the siblings have room.
        walled = rows[1]
        healthy = [dict(r) for r in (rows[0], rows[2])]
        for r in healthy:
            codexresets.weekly_window(r)["used_percent"] = 10.0
        ResetHandler.balances = {TOKEN_A: 2, TOKEN_B: 2, TOKEN_C: 2}
        out = codexresets.reset_pass([healthy[0], walled, healthy[1]],
                                     reading_age_s=0.0, url_base=self.base,
                                     accounts=pool, ledger=self.ledger,
                                     probe=lambda _a: reading(weekly_pct=0))
        acted = [r for r in out if r.get("outcome")]
        self.assertEqual([r["account"] for r in acted], [walled["email"]])
        posts = self.consume_requests()
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["auth"], "Bearer " + TOKEN_B,
                         "the credit must be redeemed with the WALLED "
                         "member's credential, never a sibling's")
        self.assertEqual(posts[0]["account"], "acct-one-workspace")

    def test_the_balance_is_read_under_the_same_member(self):
        """THE REVERSE CASE, and the one a wrong binding passes silently: the
        walled member holds NO credit and a sibling holds two. Resolved on the
        workspace id, helm reads the sibling's two, calls the wall ready, and
        sends a redemption. Bound to the member, it reads zero and refuses."""
        rows = self.members()
        pool = self.pool(rows)
        ResetHandler.balances = {TOKEN_A: 2, TOKEN_B: 0, TOKEN_C: 2}
        out = codexresets.reset_pass([rows[1]], reading_age_s=0.0,
                                     url_base=self.base, accounts=pool,
                                     ledger=self.ledger)
        self.assertEqual(out[0]["reason"], codexresets.R_NO_CREDIT)
        self.assertEqual(out[0]["spendable"], 0)
        self.assertIn(TOKEN_B, self.auths(codexresets.LIST_PATH)[0],
                      "the LISTING is the same binding as the consume")
        self.assertNotIn(TOKEN_A, repr(ResetHandler.seen),
                         "no sibling's credential was asked about this wall")
        self.assertEqual([r["path"] for r in ResetHandler.seen],
                         [codexresets.LIST_PATH],
                         "one listing reached the vendor and no consume did")

    def test_a_reading_two_credentials_could_serve_refuses(self):
        """A reading with no user id and no address proves no member of the
        workspace (task/2981). Every sibling could be it, and an asset that
        cannot be un-spent is not spent on a guess about whose it is."""
        rows = self.members()
        pool = self.pool(rows)
        orphan = dict(rows[1], user_id=None, email=None,
                      file="codex-orphan.json", files=["codex-orphan.json"])
        account, err = codexresets.resolve_credential(pool, orphan)
        self.assertIsNone(account)
        self.assertEqual(err, codexresets.R_AMBIGUOUS_CREDENTIAL)
        out = codexresets.reset_pass([orphan], reading_age_s=0.0,
                                     url_base=self.base, accounts=pool,
                                     ledger=self.ledger)
        self.assertEqual(out[0]["reason"], codexresets.R_AMBIGUOUS_CREDENTIAL)
        self.assertEqual(ResetHandler.seen, [], "nothing is sent for it")
        # MUST-HIT on the same pool AND the same recorder: a reading that DOES
        # name one member resolves, and asking the vendor under it fills the
        # recorder — so the emptiness above is a measurement.
        resolved = codexresets.resolve_credential(pool, rows[1])[0]
        self.assertEqual(resolved["access_token"], TOKEN_B)
        codexresets.list_credits(resolved, self.base)
        self.assertEqual([r["path"] for r in ResetHandler.seen],
                         [codexresets.LIST_PATH])

    def test_the_member_key_resolves_what_the_pool_file_cannot(self):  # noqa: VACUOUS_ASSERTION — two unconditional positives sit above the absence: the stale file name, and the sorted list of the three files the pool really does hold
        """THE HALF THE FILE TIE-BREAK HIDES. A budget row normally names the
        pool file it was probed from, so a matcher keyed on the WORKSPACE id
        alone still lands on one credential once the file narrows it — and
        every arm above stays green. It is a CACHED reading that exposes the
        difference: a snapshot written before a re-pool renamed the files
        names a spelling no pooled record holds any more, the file narrows
        nothing, and only the member identity says which of the three
        siblings this reading is."""
        rows = self.members()
        pool = self.pool(rows)
        stale = dict(rows[1], file="codex-renamed-away.json",
                     files=["codex-renamed-away.json"])
        # MUST-HIT FIRST, on the pool itself: no pooled record holds the
        # file this reading names, so the resolution below came from the
        # member key and from nothing else.
        self.assertEqual(stale["file"], "codex-renamed-away.json")
        self.assertEqual([a["file"] for a in pool
                          if stale["file"] in (a.get("files") or ())], [])
        self.assertEqual(sorted(a["file"] for a in pool),
                         ["codex-a.json", "codex-b.json", "codex-c.json"])
        account, err = codexresets.resolve_credential(pool, stale)
        self.assertIsNone(err, "the member identity names it on its own")
        self.assertEqual(account["access_token"], TOKEN_B)

    def test_a_reading_with_no_user_id_never_spends_a_lone_siblings_credit(self):
        """task/2981. The matcher admitted a missing user id on the account
        id alone. So a reading with no user id and no address, beside ONE
        pooled sibling, resolved to that sibling's token, and the pass would
        spend the sibling's credit on a wall that is not the sibling's."""
        rows = self.members()
        pool = self.pool(rows[:1])
        orphan = dict(rows[1], user_id=None, email=None,
                      file="codex-orphan.json", files=["codex-orphan.json"])
        self.assertEqual(codexresets.resolve_credential(pool, orphan),
                         (None, codexresets.R_AMBIGUOUS_CREDENTIAL))
        # a reading PROVEN to be another member finds no credential, which is
        # a different refusal: nobody could be it
        self.assertEqual(codexresets.resolve_credential(pool, rows[1]),
                         (None, codexresets.R_NO_CREDENTIAL))
        # THE CONTROLS. The address proves the member when the user id is
        # missing, and so does the pool file the reading was probed from.
        by_mail = dict(rows[0], user_id=None, file="codex-orphan.json",
                       files=["codex-orphan.json"])
        self.assertEqual(codexresets.resolve_credential(pool, by_mail)[0]
                         ["access_token"], TOKEN_A)
        by_file = dict(rows[0], user_id=None, email=None)
        self.assertEqual(codexresets.resolve_credential(pool, by_file)[0]
                         ["access_token"], TOKEN_A)

    def test_the_pool_file_breaks_a_tie_the_member_key_cannot(self):
        """MUST-HIT on the same observable: the identical orphaned reading,
        naming a pool file one of the two candidates holds, resolves — so the
        refusal above is ambiguity and not a matcher that answers nobody."""
        rows = self.members()
        pool = self.pool(rows)
        orphan = dict(rows[1], user_id=None)
        account, err = codexresets.resolve_credential(pool, orphan)
        self.assertIsNone(err)
        self.assertEqual(account["file"], "codex-b.json")
        self.assertEqual(account["access_token"], TOKEN_B)

    def test_a_reading_no_pooled_credential_serves_is_named_as_that(self):
        rows = self.members()
        account, err = codexresets.resolve_credential([], rows[0])
        self.assertIsNone(account)
        self.assertEqual(err, codexresets.R_NO_CREDENTIAL)
        # A pooled record with no token is the same answer: nothing to send.
        tokenless = dict(account_of(rows[0]), access_token=None)
        self.assertEqual(codexresets.resolve_credential([tokenless], rows[0]),
                         (None, codexresets.R_NO_CREDENTIAL))


class CreditsDepletedTest(FakeVendorCase):
    """WHY IT IS WALLED. A rate-limit reset credit lifts a RATE-LIMIT wall.
    `workspace_member_credits_depleted` and `workspace_owner_credits_depleted`
    are states of the workspace's CREDIT balance, and whether the vendor would
    even accept a reset against one is UNVERIFIED — it cannot be verified
    without spending the owner's asset to ask, which is why this refuses."""

    def test_the_recorded_team_body_is_a_credits_depleted_wall(self):
        """The fixture really is that world, so the refusal below is about the
        reached type rather than about an invented body."""
        row = reading("team")
        self.assertEqual(row["reached_type"],
                         "workspace_owner_credits_depleted")
        self.assertEqual(codexresets.weekly_window(row)["used_percent"], 100)

    def test_a_credits_depleted_wall_is_never_offered_a_reset(self):
        for kind in codexresets.CREDITS_DEPLETED_TYPES:
            with self.subTest(reached=kind):
                out = codexresets.reset_pass(
                    [reading(reached=kind)], reading_age_s=0.0,
                    url_base=self.base,
                    accounts=[account_of(reading(reached=kind))],
                    ledger=self.ledger)
                self.assertEqual(out[0]["reason"],
                                 codexresets.R_CREDITS_DEPLETED)
        self.assertEqual(ResetHandler.seen, [],
                         "not even the balance is read for a wall a credit "
                         "cannot lift")
        codexresets.list_credits(self.account, url_base=self.base)
        self.assertTrue(ResetHandler.seen,
                        "must-hit: the recorder fills when it is called")

    def test_control_a_plain_rate_limit_wall_is_still_taken(self):
        """MUST-HIT through the identical call: the recorded pro body is
        rate_limit_reached at 100%, and it spends."""
        row = reading()
        out = codexresets.reset_pass([row], reading_age_s=0.0,
                                     url_base=self.base,
                                     accounts=[account_of(row)],
                                     ledger=self.ledger,
                                     probe=lambda _a: reading(weekly_pct=0))
        self.assertEqual(out[0]["outcome"], codexresets.OUTCOME_RESET)

    def test_a_reached_type_this_helm_does_not_know_refuses(self):
        d = codexresets.decide(reading(reached="workspace_frozen"), 0.0, None,
                               [], time.time(), None)
        self.assertEqual(d.reason, codexresets.R_REACHED_UNRECOGNISED)

    def test_a_spent_week_the_vendor_gives_no_reason_for_refuses(self):
        row = reading(reached=None)
        self.assertEqual(row["state"], "exhausted",
                         "must-hit: the row really is a spent week")
        self.assertIsNone(row["reached_type"])
        d = codexresets.decide(row, 0.0, None, [], time.time(), None)
        self.assertEqual(d.reason, codexresets.R_REACHED_UNKNOWN)
        # MUST-HIT: the same body WITH the recorded reason is taken, so the
        # refusal is the missing reason and not the rest of the fixture.
        named = codexresets.decide(reading(), 0.0, None, [], time.time(), None)
        self.assertEqual(named.action, codexresets.NEED_CREDITS)


class ObservedRefusalTest(FakeVendorCase):
    """THE 429 HELM HAS ALREADY SEEN. The owner learns of a wall by seeing a
    429, not by reading a percentage, because the usage endpoint's weekly
    percent LAGS the refusals. helm accepts a 429 it has already recorded for
    the same credential as a second signal of zero — bounded, because the
    proxy's cooldown is a BELIEF held for the whole window and a belief over a
    reading with real headroom is the stale-cooldown defect, not a wall."""

    def cooling(self, row, at=None):
        return {row["file"]: weekly_reset_at(row) if at is None else at}

    def before_the_wall(self, row):
        """AN INSTANT DERIVED FROM THE FIXTURE, NEVER THE WALL CLOCK. Every
        belief here is an absolute instant recorded in the vendor bodies
        beside this file, and a belief the reader's clock has already passed
        is filtered as ELAPSED before any other property of it is examined —
        so an arm that asks `time.time()` measures the calendar and stops
        measuring its subject the day the fixture ages past today. An hour
        before the window this fixture says reopens is inside every one of
        these beliefs, for as long as the file exists."""
        return weekly_reset_at(row) - 3600

    def test_a_lagging_reading_and_a_429_naming_the_week_is_a_wall(self):
        row = reading(weekly_pct=92)
        now = self.before_the_wall(row)
        # CONTROL FIRST: the same reading with no 429 on record refuses.
        self.assertEqual(
            codexresets.decide(row, 0.0, None, [], now, None).reason,
            codexresets.R_NOT_EXHAUSTED)
        d = codexresets.decide(row, 0.0, None, [], now, None,
                               cooling_reset_at=weekly_reset_at(row))
        self.assertEqual(d.action, codexresets.NEED_CREDITS)
        self.assertEqual(codexresets.weekly_wall(row, weekly_reset_at(row),
                                                 now),
                         codexresets.WALL_OBSERVED)

    def test_a_429_that_names_the_five_hour_window_is_not_a_weekly_wall(self):
        row = reading("team", weekly_pct=92,
                      reached=codexresets.RATE_LIMIT_REACHED)
        five_hour = next(w for w in row["windows"]
                         if w["seconds"] < codexresets.WEEKLY_MIN_SECONDS)
        now = five_hour["reset_at"] - 600
        self.assertGreater(five_hour["reset_at"], now,
                           "must-hit: the belief is LIVE at this instant, so "
                           "the refusal below is about which window it names "
                           "and not about a belief the clock has passed")
        self.assertEqual(codexresets.weekly_wall(row, weekly_reset_at(row),
                                                 now),
                         codexresets.WALL_OBSERVED,
                         "must-hit: at this same instant a belief naming the "
                         "WEEKLY window is a wall")
        d = codexresets.decide(row, 0.0, None, [], now, None,
                               cooling_reset_at=five_hour["reset_at"])
        self.assertEqual(d.reason, codexresets.R_NOT_EXHAUSTED)

    def test_a_belief_that_names_both_windows_names_neither(self):  # noqa: VACUOUS_ASSERTION — the assertLess above it is the unconditional positive: it MEASURES that the fixture's two windows really do reopen together
        """A 5h wall whose retry instant happens to fall within tolerance of
        the weekly reset must not be read as a weekly refusal."""
        row = reading("team", weekly_pct=92,
                      reached=codexresets.RATE_LIMIT_REACHED,
                      weekly_resets_in=5431 + 60)
        five_hour = next(w for w in row["windows"]
                         if w["seconds"] < codexresets.WEEKLY_MIN_SECONDS)
        at, now = weekly_reset_at(row), self.before_the_wall(row)
        self.assertLess(abs(at - five_hour["reset_at"]),
                        codexresets.COOLING_MATCH_TOLERANCE_S,
                        "must-hit: the two windows really do reopen together "
                        "in this fixture")
        # MUST-HIT ON THIS SAME BODY, ONE FIELD MOVED: the identical fixture
        # with its weekly window pushed clear of the 5h one IS a wall for a
        # belief naming it at this instant, so the None below is the
        # COLLISION and not a belief that names nothing. A control built from
        # the OTHER recorded shape cannot say that — its weekly window
        # reopens days away from this instant, so it reads None for exactly
        # the reason this arm exists to rule out, and the must-hit then
        # measures the two shapes' distance rather than the collision.
        apart = reading("team", weekly_pct=92,
                        reached=codexresets.RATE_LIMIT_REACHED,
                        weekly_resets_in=5431 + 60
                        + 4 * codexresets.COOLING_MATCH_TOLERANCE_S)
        self.assertGreater(abs(weekly_reset_at(apart) - five_hour["reset_at"]),
                           codexresets.COOLING_MATCH_TOLERANCE_S,
                           "must-hit: and in THAT fixture the two windows do "
                           "NOT reopen together")
        self.assertEqual(codexresets.weekly_wall(apart, weekly_reset_at(apart),
                                                 now),
                         codexresets.WALL_OBSERVED,
                         "must-hit: at this instant a belief naming ONLY the "
                         "weekly window IS a wall, so the None below is the "
                         "collision and not an elapsed belief")
        self.assertIsNone(codexresets.weekly_wall(row, at, now))

    def test_a_reading_with_real_headroom_contradicts_the_belief(self):  # noqa: VACUOUS_ASSERTION — the positive on this observable is test_a_lagging_reading_and_a_429_naming_the_week_is_a_wall, which drives the identical call to WALL_OBSERVED
        """The measured stale-cooldown world: a sidecar holding a credential
        in cooldown while the vendor reads 4% used. That is the sidecar being
        wrong, and it is never an authorization to spend."""
        row = reading(weekly_pct=4)
        at, now = weekly_reset_at(row), self.before_the_wall(row)
        self.assertEqual(codexresets.weekly_wall(reading(weekly_pct=92),
                                                 at, now),
                         codexresets.WALL_OBSERVED,
                         "must-hit: the same belief at the same instant IS a "
                         "wall for a reading at the wall's edge")
        self.assertIsNone(codexresets.weekly_wall(row, at, now))

    def test_an_elapsed_belief_is_no_evidence(self):
        row = reading(weekly_pct=92)
        at = weekly_reset_at(row)
        self.assertEqual(codexresets.weekly_wall(row, at, at - 60),
                         codexresets.WALL_OBSERVED,
                         "must-hit: a minute before it elapses the same "
                         "belief IS evidence")
        self.assertIsNone(codexresets.weekly_wall(row, at, at + 1))

    def test_a_429_is_a_signal_of_zero_and_never_a_signal_of_why(self):
        """MEASURED, not assumed. The roster entries this evidence comes from
        carry the vendor's own 429 body, and across every cooling pooled
        credential on this fleet that body reads error.type
        `usage_limit_reached` — for the rate-limited accounts and for the
        credits-depleted ones alike. So a 429 cannot supply the reason a
        reading is missing, and a wall with no stated reason refuses however
        loudly the proxy is holding that credential out."""
        row = reading(reached=None)
        at, now = weekly_reset_at(row), self.before_the_wall(row)
        self.assertTrue(codexresets.cooling_names_weekly(row, at, now),
                        "must-hit: the 429 really does name this window")
        d = codexresets.decide(row, 0.0, self.LISTED, [], now, None,
                               cooling_reset_at=at)
        self.assertEqual((d.action, d.reason),
                         (codexresets.NO_ACT, codexresets.R_REACHED_UNKNOWN))
        # AND THE CONTROL that the 429 is doing its OTHER job on the identical
        # inputs: the same body at 92% used, with the vendor's reason on it,
        # is taken — which is the lag correction the signal exists for.
        lagging = reading(weekly_pct=92)
        self.assertEqual(
            codexresets.decide(lagging, 0.0, self.LISTED, [],
                               self.before_the_wall(lagging), None,
                               cooling_reset_at=weekly_reset_at(lagging)
                               ).action,
            codexresets.CONSUME)

    LISTED = codexresets._list_result(codexresets.LIST_OK, available=2)

    def test_the_pass_binds_the_429_to_the_resolved_credential(self):
        row = reading(weekly_pct=92)
        now = self.before_the_wall(row)
        out = codexresets.reset_pass([row], reading_age_s=0.0, now=now,
                                     url_base=self.base,
                                     accounts=[account_of(row)],
                                     ledger=self.ledger,
                                     cooling=self.cooling(row),
                                     probe=lambda _a: reading(weekly_pct=0))
        self.assertEqual(out[0]["outcome"], codexresets.OUTCOME_RESET)
        self.assertEqual(out[0]["wall"], codexresets.WALL_OBSERVED)
        # A belief filed under ANOTHER credential's pool file moves nothing.
        other = codexresets.reset_pass(
            [reading(weekly_pct=92)], reading_age_s=0.0, now=now,
            url_base=self.base,
            accounts=[account_of(reading(weekly_pct=92))],
            ledger=os.path.join(self.tmp, "second.jsonl"),
            cooling={"codex-somebody-else.json": weekly_reset_at(row)})
        self.assertEqual(other[0]["reason"], codexresets.R_NOT_EXHAUSTED)


class CoolingRosterTest(unittest.TestCase):
    """The 429 evidence comes from the roster helm ALREADY reads every pass
    (the stale-cooldown rung's management call), never from a new probe and
    never from a log — and only from the sidecars that LOAD THE POOL."""

    NOW = 1_789_500_000.0
    #: The seats whose sidecar loads the pool, as `pool_sidecar_seats` answers
    #: it. Every call below is scoped, because unscoped is not a reading this
    #: function offers.
    POOL = {"seat-a", "seat-b"}

    def entry(self, name="codex-a.json", reset_in=4 * 3600, kind="codex",
              at=None):
        """One roster entry. `at` is the instant the belief is measured
        AGAINST — the arms that call the pure function pin a fixed NOW, and
        the arm that goes through the rung has to use the real clock, because
        the rung reads its own."""
        from datetime import datetime, timezone
        base = self.NOW if at is None else at
        stamp = datetime.fromtimestamp(base + reset_in, timezone.utc)
        return {"type": kind, "name": name, "status": "error",
                "next_retry_after": stamp.strftime("%Y-%m-%dT%H:%M:%S.123456789Z")}

    def test_a_cooling_codex_credential_is_one_entry_keyed_by_pool_file(self):
        got = proxywatch.codex_cooling_by_file(
            [("seat-a", 8321, [self.entry()])], self.POOL, now=self.NOW)
        self.assertEqual(list(got), ["codex-a.json"])
        self.assertAlmostEqual(got["codex-a.json"], self.NOW + 4 * 3600,
                               delta=1.0)

    def test_elapsed_foreign_and_beliefless_entries_are_not_evidence(self):
        entries = [self.entry(reset_in=-60),
                   self.entry(name="codex-g.json", kind="gemini"),
                   {"type": "codex", "name": "codex-n.json", "status": "active"}]
        self.assertEqual(proxywatch.codex_cooling_by_file(
            [("seat-a", 8321, entries)], self.POOL, now=self.NOW), {})
        # MUST-HIT: one live codex belief in the SAME roster does register.
        self.assertEqual(list(proxywatch.codex_cooling_by_file(
            [("seat-a", 8321, entries + [self.entry()])], self.POOL,
            now=self.NOW)), ["codex-a.json"])

    def test_two_sidecars_disagreeing_keep_the_later_instant(self):  # noqa: VACUOUS_ASSERTION — assertAlmostEqual against a computed instant is the positive; nothing here asserts an absence
        got = proxywatch.codex_cooling_by_file(
            [("seat-a", 8321, [self.entry(reset_in=3600)]),
             ("seat-b", 8322, [self.entry(reset_in=7200)])], self.POOL,
            now=self.NOW)
        self.assertAlmostEqual(got["codex-a.json"], self.NOW + 7200, delta=1.0)

    def test_a_sidecar_that_does_not_load_the_pool_is_not_evidence(self):
        """THE ROSTER NAMES A CREDENTIAL BY ITS FILE AND BY NOTHING ELSE, so
        a same-named file in a proxy holding its OWN auth dir would otherwise
        supply a 429 for a pooled credential it has no relation to. Measured
        on this fleet: one cooling codex file belongs to no pooled account and
        is held by the one codex sidecar with an auth dir of its own."""
        foreign = [("seat-z", 8399, [self.entry(name="codex-a.json")])]
        self.assertEqual(proxywatch.codex_cooling_by_file(
            foreign, self.POOL, now=self.NOW), {},
            "a foreign sidecar spoke for a pooled credential")
        # MUST-HIT: the SAME entry, from a seat that does load the pool, is
        # evidence — so the emptiness above is the scope and not the entry.
        self.assertEqual(list(proxywatch.codex_cooling_by_file(
            foreign, {"seat-z"}, now=self.NOW)), ["codex-a.json"])
        # AND AN UNKNOWN SCOPE IS NO SCOPE: None yields no evidence rather
        # than every sidecar's, which can only make the policy refuse more.
        self.assertEqual(proxywatch.codex_cooling_by_file(
            foreign, None, now=self.NOW), {})

    def test_the_pool_sidecars_are_the_ones_whose_config_names_the_pool_dir(self):
        """WHICH SIDECAR SERVES THE POOL IS THE CONFIG'S ANSWER, not the seat
        name's: `helm seat` refuses to mint a codex family whose declared
        auth-dir is not `codexhomes.pool_dir()`, so the declaration is the
        binding."""
        from helm import codexhomes
        from helm import proxy_usage as pu
        tmp = os.path.realpath(tempfile.mkdtemp(prefix="helm-test-pool-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with mock.patch.dict(os.environ, {"HELM_HOME": tmp}):
            pool = codexhomes.pool_dir()
            os.makedirs(pool, exist_ok=True)
            homes = {}
            for seat, auth in (("seat-a", pool),
                               ("seat-b", os.path.join(tmp, "other", "auth"))):
                d = os.path.join(tmp, "homes", seat)
                os.makedirs(d, exist_ok=True)
                with open(os.path.join(d, "config.yaml"), "w",
                          encoding="utf-8") as f:
                    f.write("port: 8317\nauth-dir: %s\n" % auth)
                homes[seat] = d
            with mock.patch.object(
                    pu, "instances",
                    return_value=[("codex", s, homes[s]) for s in homes]):
                self.assertEqual(proxywatch.pool_sidecar_seats(), {"seat-a"})
                # MUST-HIT: point the second config AT the pool and it joins,
                # so the exclusion above is the auth-dir and not the loop.
                with open(os.path.join(homes["seat-b"], "config.yaml"), "w",
                          encoding="utf-8") as f:
                    f.write("port: 8318\nauth-dir: %s\n" % pool)
                self.assertEqual(proxywatch.pool_sidecar_seats(),
                                 {"seat-a", "seat-b"})

    def test_the_rung_hands_the_policy_the_roster_the_pass_read(self):
        entries = [self.entry(at=time.time())]
        with mock.patch.object(codexresets, "reset_pass",
                               return_value=[]) as rung, \
                mock.patch.object(proxywatch, "pool_sidecar_seats",
                                  return_value={"seat-a"}):
            proxywatch._codex_reset_pass(
                [{"email": "x"}],
                sidecars=[("seat-a", 1, entries),
                          ("seat-z", 2, [self.entry(name="codex-z.json",
                                                    at=time.time())])])
        # SCOPED AT THE PRODUCTION DOOR TOO: the foreign sidecar's entry is
        # not in what the rung hands the policy.
        self.assertEqual(list(rung.call_args.kwargs["cooling"]),
                         ["codex-a.json"])
        # CONTROL: with no roster in hand the rung hands over None, which can
        # only make the policy refuse more.
        with mock.patch.object(codexresets, "reset_pass",
                               return_value=[]) as rung:
            proxywatch._codex_reset_pass([{"email": "x"}])
        self.assertIsNone(rung.call_args.kwargs["cooling"])


class ConcurrentPassTest(FakeVendorCase):
    """MUTUAL EXCLUSION. The attempt history is read at the top of a pass; a
    second pass — the successor of a long one, or the manual door typed while
    the rung runs — can read the same empty history and spend for the same
    wall. The lock has to span re-read -> cool-down -> write-ahead append."""

    def run_pass(self, ledger=None):
        row = reading()
        return codexresets.reset_pass([row], reading_age_s=0.0,
                                      url_base=self.base,
                                      accounts=[account_of(row)],
                                      ledger=ledger or self.ledger,
                                      probe=lambda _a: reading(weekly_pct=0))

    def test_two_overlapping_passes_spend_one_credit_for_one_wall(self):
        # The two passes are held at the LISTING — the point where both have
        # decided and neither has written — and released together, which is
        # the interleaving a long pass and its successor produce.
        ResetHandler.gate = threading.Barrier(
            2, timeout=30, action=lambda: setattr(ResetHandler, "gate", None))
        threads = [threading.Thread(target=self.run_pass) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(len(self.consume_requests()), 1,
                         "one wall must cost one credit even when two passes "
                         "overlap")

    def test_control_two_serial_passes_spend_one(self):
        """MUST-HIT: serialized, the cool-down already held — so the arm above
        is about the overlap and not about a ledger that never blocks."""
        self.run_pass()
        self.run_pass()
        self.assertEqual(len(self.consume_requests()), 1)

    def test_a_ledger_whose_lock_will_not_take_spends_nothing(self):
        import contextlib as _c

        @_c.contextmanager
        def refuse(_path, timeout=None):
            yield False

        with mock.patch.object(codexresets.eventledger, "locked", refuse):
            rows = self.run_pass()
        self.assertEqual(rows[0]["reason"], codexresets.R_LEDGER_LOCKED)
        self.assertEqual(self.consume_requests(), [])
        self.assertEqual(self.ledger_rows(), [],
                         "nothing is even written down")
        # MUST-HIT on the same calls: with the real lock it spends, the
        # recorder registers the consume and the ledger fills — so the two
        # emptinesses above are measurements.
        self.assertEqual(self.run_pass()[0]["outcome"],
                         codexresets.OUTCOME_RESET)
        self.assertEqual(len(self.consume_requests()), 1)
        self.assertEqual([r["outcome"] for r in self.ledger_rows()],
                         [codexresets.OUTCOME_PENDING,
                          codexresets.OUTCOME_RESET])


class LedgerKeyTest(FakeVendorCase):
    """The ledger is keyed on the CREDENTIAL, not on what a surface calls it
    today: `codexbudget.probe_record` takes the email from the VENDOR'S body,
    so a body that omits it renames the account and hands the same credential
    a second, empty history."""

    def run_pass(self, row, account, ledger=None):
        return codexresets.reset_pass([row], reading_age_s=0.0,
                                      url_base=self.base, accounts=[account],
                                      ledger=ledger or self.ledger,
                                      probe=lambda _a: reading(weekly_pct=0))

    def test_the_history_survives_a_reading_that_loses_its_email(self):
        row = reading()
        first = self.run_pass(row, account_of(row))
        self.assertEqual(first[0]["outcome"], codexresets.OUTCOME_RESET)
        nameless = dict(row, email=None)
        second = self.run_pass(nameless,
                               dict(account_of(nameless), email=None))
        self.assertEqual(second[0]["reason"], codexresets.R_COOLDOWN)
        self.assertEqual(len(self.consume_requests()), 1,
                         "the same credential must not get a second credit "
                         "because its row lost a display name")

    def test_the_row_carries_the_member_digest_and_no_account_id(self):
        row = reading()
        self.run_pass(row, account_of(row))
        journal = self.ledger_rows()
        self.assertEqual({r["member"] for r in journal},
                         {codexresets.member_id(account_of(row))})
        self.assertNotIn(row["account_id"], repr(journal))
        self.assertNotIn(FAKE_TOKEN, repr(journal))
        self.assertEqual({r["account"] for r in journal}, {row["email"]},
                         "the label still rides the row, for a human reading "
                         "the file")

    def test_two_members_of_one_workspace_keep_separate_histories(self):
        a = reading(email="a@fixture.invalid", account_id="acct-ws",
                    user_id="user-a", file="codex-a.json")
        b = reading(email="b@fixture.invalid", account_id="acct-ws",
                    user_id="user-b", file="codex-b.json")
        pool = [account_of(a, TOKEN_A), account_of(b, TOKEN_B)]
        ResetHandler.balances = {TOKEN_A: 2, TOKEN_B: 2}
        with mock.patch.object(codexresets, "MAX_CONSUMES_PER_PASS", 2):
            out = codexresets.reset_pass([a, b], reading_age_s=0.0,
                                         url_base=self.base, accounts=pool,
                                         ledger=self.ledger,
                                         probe=lambda _a: reading(weekly_pct=0))
        self.assertEqual([r["outcome"] for r in out],
                         [codexresets.OUTCOME_RESET, codexresets.OUTCOME_RESET],
                         "one member's attempt is not the other's cool-down")
        self.assertEqual(len({r["member"] for r in self.ledger_rows()}), 2)


class BaseUrlFenceTest(FakeVendorCase):
    """NOTHING DEFAULTS TO THE VENDOR. The base url is an argument of every
    call that can reach the network, so an arm, a script or a future rung that
    forgets one is refused rather than redeeming a real credit."""

    def test_a_consume_with_no_base_is_refused_before_it_is_sent(self):
        res = codexresets.consume(self.account, "a-key", None)
        self.assertEqual(res["outcome"], codexresets.OUTCOME_REFUSED)
        self.assertIs(res["spent"], False)
        self.assertEqual(ResetHandler.seen, [])
        # MUST-HIT: the identical call WITH a base reaches the recorder.
        codexresets.consume(self.account, "a-key", self.base)
        self.assertEqual([r["path"] for r in ResetHandler.seen],
                         [codexresets.CONSUME_PATH])
        self.assertEqual([r["body"]["redeem_request_id"]
                          for r in self.consume_requests()], ["a-key"])

    def test_a_listing_with_no_base_is_refused_before_it_is_sent(self):
        res = codexresets.list_credits(self.account, None)
        self.assertEqual(res["status"], codexresets.LIST_NO_BASE)
        self.assertEqual(ResetHandler.seen, [])
        self.assertEqual(codexresets.list_credits(
            self.account, self.base)["status"], codexresets.LIST_OK,
            "must-hit: the identical call WITH a base reads a balance")
        self.assertEqual([r["path"] for r in ResetHandler.seen],
                         [codexresets.LIST_PATH])

    def test_a_pass_with_no_base_decides_nothing_and_sends_nothing(self):
        row = reading()
        rows = codexresets.reset_pass([row], reading_age_s=0.0,
                                      accounts=[account_of(row)],
                                      ledger=self.ledger)
        self.assertEqual(rows[0]["reason"], codexresets.R_NO_BASE_URL)
        self.assertEqual(ResetHandler.seen, [])
        self.assertEqual(self.ledger_rows(), [])
        self.assertEqual(codexresets.reset_pass(
            [row], reading_age_s=0.0, url_base=self.base,
            accounts=[account_of(row)], ledger=self.ledger,
            probe=lambda _a: reading(weekly_pct=0))[0]["outcome"],
            codexresets.OUTCOME_RESET,
            "must-hit: the identical pass WITH a base spends")
        self.assertEqual([r["path"] for r in ResetHandler.seen],
                         [codexresets.LIST_PATH, codexresets.CONSUME_PATH,
                          codexresets.LIST_PATH])
        self.assertEqual([r["outcome"] for r in self.ledger_rows()],
                         [codexresets.OUTCOME_PENDING,
                          codexresets.OUTCOME_RESET])

    def test_an_unread_pool_is_never_an_account_to_spend_from(self):
        from helm import codexbudget
        with mock.patch.object(codexbudget, "pool_census",
                               return_value=([], codexbudget.CENSUS_UNREAD)):
            rows = codexresets.reset_pass([reading()], reading_age_s=0.0,
                                          url_base=self.base,
                                          ledger=self.ledger)
        self.assertEqual(rows[0]["reason"], codexresets.R_NO_POOL)
        self.assertEqual(rows[0]["action"], codexresets.NO_ACT)
        self.assertIn(codexresets.R_NO_POOL,
                      codexresets.pass_notice(rows) or "",
                      "an unreadable pool is a systemic state the room hears")
        self.assertEqual(ResetHandler.seen, [])
        # MUST-HIT: the same pass over a MEASURED census reaches the vendor.
        row = reading()
        codexresets.reset_pass([row], reading_age_s=0.0, url_base=self.base,
                               accounts=[account_of(row)],
                               ledger=self.ledger,
                               probe=lambda _a: reading(weekly_pct=0))
        self.assertEqual([r["path"] for r in ResetHandler.seen][:1],
                         [codexresets.LIST_PATH])

    def test_a_pass_that_can_act_on_nothing_at_all_says_so(self):
        """THE OWNER'S ACCEPTANCE TEST IS THAT SILENCE MEANS HEALTHY. A pass
        with no vendor base can act on nothing, for a reason no wall of his
        will cure, and a rung that is inert for a systemic reason while saying
        nothing is the one state he cannot detect by not watching."""
        row = reading()
        rows = codexresets.reset_pass([row], reading_age_s=0.0,
                                      accounts=[account_of(row)],
                                      ledger=self.ledger)
        notice = codexresets.pass_notice(rows)
        self.assertIsNotNone(notice, "an inert rung said nothing at all")
        self.assertIn(codexresets.R_NO_BASE_URL, notice)
        self.assertTrue(codexresets.blocked_digest(rows),
                        "the systemic line is latched like any blocked wall, "
                        "so it is said once and not every fifteen minutes")
        # THE CONTROL, through the same renderer: an ORDINARY quiet pass — a
        # week with room in it — still says nothing, so the line above is the
        # systemic state and not a rung that now speaks on every pass.
        healthy = reading(weekly_pct=41)
        quiet = codexresets.reset_pass([healthy], reading_age_s=0.0,
                                       url_base=self.base,
                                       accounts=[account_of(healthy)],
                                       ledger=self.ledger)
        self.assertIsNone(codexresets.pass_notice(quiet))

    def test_the_live_vendor_is_named_by_exactly_the_production_doors(self):
        """A SOURCE CENSUS, because the fence is structural: `live_base_url`
        is the only resolver of the real endpoint, and the set of modules that
        call it is the complete set of places a request to the owner's account
        can originate."""
        import glob
        callers = set()
        for path in glob.glob(os.path.join(os.path.dirname(HERE), "helm",
                                           "*.py")):
            with open(path, encoding="utf-8") as f:
                text = f.read()
            if "live_base_url(" in text.replace("def live_base_url(", ""):
                callers.add(os.path.basename(path))
        self.assertEqual(callers, {"codexresets.py", "proxywatch.py"},
                         "a new caller of the live vendor is a new place the "
                         "owner's credits can be spent from")


class RoomLineTest(FakeVendorCase):
    """The one surface that announces an irreversible act. It says which
    account, what happened to its weekly window, and what is LEFT."""

    def test_a_spend_reads_as_an_outcome_with_the_balance_after_it(self):
        row = reading()
        ResetHandler.balances = {FAKE_TOKEN: 2}
        rows = codexresets.reset_pass([row], reading_age_s=0.0,
                                      url_base=self.base,
                                      accounts=[account_of(row)],
                                      ledger=self.ledger,
                                      probe=lambda _a: reading(weekly_pct=0))
        # The balance AFTER is re-read, not derived: the fake now answers 1.
        ResetHandler.balances = {FAKE_TOKEN: 1}
        line = codexresets.result_line(rows[0])
        self.assertIn(row["email"], line)
        self.assertIn("weekly window RESET", line)
        self.assertIn("100% used -> 0% used", line)
        self.assertIn("1 credit spent", line)
        self.assertIn("2 left", line)

    def test_the_after_balance_is_measured_and_says_so_when_it_is_not(self):
        row = reading()
        rows = codexresets.reset_pass([row], reading_age_s=0.0,
                                      url_base=self.base,
                                      accounts=[account_of(row)],
                                      ledger=self.ledger,
                                      probe=lambda _a: reading(weekly_pct=0))
        self.assertEqual(rows[0]["spendable_after"], 2,
                         "the re-read is a second listing, not spendable - 1")
        unread = dict(rows[0], spendable_after=None)
        # READ ALOUD, not pattern-matched: this is the sentence the owner gets
        # when the confirming listing fails, and "1 credit spent, balance
        # unread left" is not one.
        self.assertTrue(codexresets.result_line(unread).endswith(
            "weekly window RESET (100% used -> 0% used); 1 credit spent, and "
            "the balance left could not be read"),
            codexresets.result_line(unread))
        # The control, same renderer, same row shape: the measured balance
        # reads as a count.
        self.assertTrue(codexresets.result_line(rows[0]).endswith(
            "weekly window RESET (100% used -> 0% used); 1 credit spent, "
            "2 left"), codexresets.result_line(rows[0]))

    def test_a_wanted_wall_that_could_not_be_taken_says_why(self):
        ResetHandler.list_mode = "zero"
        row = reading()
        rows = codexresets.reset_pass([row], reading_age_s=0.0,
                                      url_base=self.base,
                                      accounts=[account_of(row)],
                                      ledger=self.ledger)
        line = codexresets.result_line(rows[0])
        self.assertIn("no credit spent", line)
        self.assertIn(codexresets.R_NO_CREDIT, line)
        self.assertIn(row["email"], line)

    def test_an_unknown_outcome_never_claims_nothing_was_spent(self):
        ResetHandler.consume_mode = "500"
        row = reading()
        rows = codexresets.reset_pass([row], reading_age_s=0.0,
                                      url_base=self.base,
                                      accounts=[account_of(row)],
                                      ledger=self.ledger)
        line = codexresets.result_line(rows[0])
        self.assertIn("UNKNOWN", line)
        self.assertNotIn("no credit was spent", line)
        self.assertIn("the same request is retried rather than a new one",
                      line)

    def test_a_2xx_helm_could_not_read_states_neither_fact_it_lacks(self):
        # THE ROOM SAID A THING IT COULD NOT KNOW: "no credit was spent" over
        # a 200 from the redemption endpoint with a body helm could not read.
        for mode in ("malformed", "weird"):
            with self.subTest(mode=mode):
                ResetHandler.seen = []
                ResetHandler.consume_mode = mode
                row = reading()
                ledger = os.path.join(self.tmp, "line-%s.jsonl" % mode)
                rows = codexresets.reset_pass([row], reading_age_s=0.0,
                                              url_base=self.base,
                                              accounts=[account_of(row)],
                                              ledger=ledger)
                line = codexresets.result_line(rows[0])
                self.assertNotIn("no credit was spent", line)
                self.assertIn("WHETHER A CREDIT WAS SPENT IS UNKNOWN", line)
                self.assertIn("the same request is retried", line)
        # THE CONTROL, through the same renderer and the same pass: a 401 IS
        # certain about the balance and says so.
        ResetHandler.consume_mode = "401"
        row = reading()
        rows = codexresets.reset_pass(
            [row], reading_age_s=0.0, url_base=self.base,
            accounts=[account_of(row)],
            ledger=os.path.join(self.tmp, "line-401.jsonl"))
        self.assertIn("no credit was spent", codexresets.result_line(rows[0]))


class RoomLatchTest(FakeVendorCase):
    """A SPEND IS AN EVENT; A BLOCKED WALL IS A STATE, and the room needs
    them on different terms. Measured on this fleet's own pool: four accounts
    sit at a spent week helm cannot clear — two whose wall is a credit balance
    a rate-limit reset does not lift, two with no credit left to spend — and
    they stay that way for days. The pass that decides this runs every fifteen
    minutes, so an unlatched blocked line posts the same four sentences a
    hundred times a day and buries the one pass where something happened."""

    def rows_for(self, *pairs):
        """A pass over (reading, token) pairs against the fake, whose balance
        is per TOKEN — so one account can be out of credit in the same pass
        another spends one."""
        return codexresets.reset_pass(
            [r for r, _t in pairs], reading_age_s=0.0, url_base=self.base,
            accounts=[account_of(r, t) for r, t in pairs],
            ledger=os.path.join(self.tmp, "latch.jsonl"),
            probe=lambda _a: reading(weekly_pct=0))

    def walled(self):
        """The reading whose wall is real and whose balance is empty: the rung
        wants to act and cannot, pass after pass."""
        return reading()

    def spender(self):
        return reading("team", email="spender@fixture.invalid",
                       reached=codexresets.RATE_LIMIT_REACHED,
                       file="codex-spender.json")

    def setUp(self):
        super().setUp()
        ResetHandler.balances = {TOKEN_A: 0, TOKEN_B: 2}

    def test_a_blocked_wall_speaks_once_and_not_on_every_pass(self):
        first = self.rows_for((self.walled(), TOKEN_A))
        self.assertEqual(first[0]["reason"], codexresets.R_NO_CREDIT,
                         "must-hit: this pass really is a wanted wall helm "
                         "could not take")
        # THE OTHER HALF OF THE LATCH'S ONE GRAMMAR, pinned to its literal: a
        # row the vendor never answered has no outcome, so the state it is
        # latched on is the REASON this pass refused.
        self.assertEqual(codexresets.latch_reason(first[0]), "no-credit")
        body, latch = codexresets.watch_notice(first, None)
        self.assertIsNotNone(body, "the first pass tells the room")
        self.assertTrue(latch)
        again, latch2 = codexresets.watch_notice(
            self.rows_for((self.walled(), TOKEN_A)), latch)
        self.assertIsNone(again,
                          "the same wall for the same reason is not news")
        self.assertEqual(latch2, latch)
        # MUST-HIT: a DIFFERENT account blocked, under the identical prior
        # latch, does speak — so the silence above is the dedup and not a
        # renderer that stopped rendering.
        other = reading("team", email="other@fixture.invalid",
                        reached=codexresets.RATE_LIMIT_REACHED,
                        file="codex-other.json")
        moved, latch3 = codexresets.watch_notice(
            self.rows_for((other, TOKEN_A)), latch)
        self.assertIsNotNone(moved)
        self.assertNotEqual(latch3, latch)
        self.assertIn("other@fixture.invalid", moved)

    def test_a_spend_speaks_even_under_an_unchanged_latch(self):
        """The event half. A credit leaving the owner's balance is never held
        back because the wall beside it has not moved."""
        _body, latch = codexresets.watch_notice(
            self.rows_for((self.walled(), TOKEN_A)), None)
        mixed = self.rows_for((self.walled(), TOKEN_A),
                              (self.spender(), TOKEN_B))
        acted = [r for r in mixed if r.get("outcome")]
        self.assertTrue(acted, "must-hit: this pass really did spend one")
        body, unchanged = codexresets.watch_notice(mixed, latch)
        self.assertEqual(unchanged, latch,
                         "must-hit: and the blocked half really did NOT move")
        self.assertIsNotNone(body, "an irreversible act is never deduped")
        self.assertIn("weekly window RESET", body)

    def test_the_latch_clears_when_the_wall_does(self):
        spoke, latch = codexresets.watch_notice(
            self.rows_for((self.walled(), TOKEN_A)), None)
        self.assertIsNotNone(spoke, "must-hit: the identical call DOES render "
                                    "a body for a blocked wall, so the None "
                                    "below is the clearing")
        self.assertTrue(latch, "must-hit: and it DOES mint a latch")
        quiet = self.rows_for((reading(weekly_pct=41), TOKEN_A))
        body, cleared = codexresets.watch_notice(quiet, latch)
        self.assertIsNone(body, "a healthy week says nothing")
        # The latch is the PAIRS now, and a pass whose rows still carry this
        # member proves the state left, so the pair drops.
        self.assertEqual(cleared, [], "and it clears the latch")
        back, again = codexresets.watch_notice(
            self.rows_for((self.walled(), TOKEN_A)), cleared)
        self.assertIsNotNone(back, "the same wall returning is news again")
        self.assertEqual(again, latch)


class ExemptionRateTest(FakeVendorCase):
    """THE RATE THE COOL-DOWN EXEMPTION RUNS AT, measured through the real
    pass. A credential whose LISTING succeeds and whose REDEMPTION answers
    401/403 is the state the exemption was written for, and it is also the
    state in which nothing else bounds the rung: the cool-down is the only
    thing that says how often one account's redemption may be attempted, so
    an outcome exempt from it repeats as often as the pass runs — a write
    verb against the vendor, and a room line, every fifteen minutes."""

    #: The installed rung cadence (`helm-proxywatch.timer`), and the spacing
    #: the passes below run at. An arm in `DecisionTableTest` pins the
    #: module's floor to `proxywatch.INTERVAL_S`; this is the clock a reader
    #: of these numbers should have in mind.
    PASS_S = 900.0

    def eight_passes(self, consume_mode):
        """Eight ELIGIBLE passes one rung interval apart over ONE wall the
        vendor answers `consume_mode` to, each pass's rows carried through
        `watch_notice` with the prior latch: (consume POSTs, room bodies).

        The LISTING answers normally throughout, which is the state that
        reaches a refused consume at all: a credential the listing rejects
        refuses at `credits-unread` before anything is sent."""
        ResetHandler.seen = []
        ResetHandler.consume_mode = consume_mode
        row = reading()
        ledger = os.path.join(self.tmp, "rate-%s.jsonl" % consume_mode)
        start, latch, bodies = time.time(), None, 0
        for i in range(8):
            rows = codexresets.reset_pass(
                [row], reading_age_s=0.0, url_base=self.base,
                accounts=[account_of(row)], ledger=ledger,
                probe=lambda _a: reading(weekly_pct=0),
                now=start + i * self.PASS_S)
            body, latch = codexresets.watch_notice(rows, latch)
            bodies += 1 if body else 0
        return len(self.consume_requests()), bodies

    def test_a_standing_refusal_is_bounded_in_requests_and_in_room_lines(self):
        """UNBOUNDED WAS THE MEASUREMENT: eight passes, eight POSTs carrying a
        refused credential, eight room lines saying the same thing. The floor
        and the run bound make that four requests and one line, after which
        the ordinary cool-down carries it at one request per two hours."""
        self.assertEqual(self.eight_passes("403"), (4, 1))
        # THE CONTROL, same driver, same clock, one outcome changed: a 429 is
        # NOT exempt, so the cool-down bounds it — which is how (4, 1) above
        # is a reading about the exemption and not about the driver.
        self.assertEqual(self.eight_passes("429"), (1, 1))

    def test_a_refusal_is_a_state_in_the_latch_and_the_docstring_says_so(self):
        """THE SENTENCE AND THE MEASUREMENT, PINNED TOGETHER. A docstring
        that gives the reason for never latching an attempt as "that happens
        at most once per cool-down anyway" is false for exactly the outcomes
        the cool-down exempts, and a reader who trusts it stops looking for
        the bound that is missing. So the sentence is an assertion here, and
        the passes below measure the behaviour it claims."""
        doc = " ".join((codexresets.watch_notice.__doc__ or "").split())
        self.assertIn("SO IS A CREDENTIAL THE VENDOR REFUSES", doc)
        self.assertNotIn("at most once per cool-down anyway", doc)
        ResetHandler.consume_mode = "401"
        ledger = os.path.join(self.tmp, "latched-401.jsonl")
        row = reading()
        start = time.time()
        first = codexresets.reset_pass(
            [row], reading_age_s=0.0, url_base=self.base,
            accounts=[account_of(row)], ledger=ledger, now=start,
            probe=lambda _a: reading(weekly_pct=0))
        self.assertEqual(first[0]["outcome"],
                         codexresets.OUTCOME_UNAUTHORIZED,
                         "must-hit: this pass really did attempt and get "
                         "refused")
        body, latch = codexresets.watch_notice(first, None)
        self.assertIsNotNone(body, "the room hears the state once")
        self.assertTrue(latch, "and a refused attempt IS in the latch")
        # THE STATE THE LATCH IS KEYED ON IS THE OUTCOME, pinned to its
        # literal: a latch that keys every row on one word would hold a
        # credential's state and a wall's state under the same fingerprint,
        # and the room would go quiet on whichever arrived second.
        self.assertEqual(codexresets.latch_reason(first[0]), "unauthorized")
        second = codexresets.reset_pass(
            [row], reading_age_s=0.0, url_base=self.base,
            accounts=[account_of(row)], ledger=ledger,
            now=start + self.PASS_S, probe=lambda _a: reading(weekly_pct=0))
        self.assertEqual(second[0]["outcome"],
                         codexresets.OUTCOME_UNAUTHORIZED,
                         "must-hit: the next pass DID attempt again — the "
                         "silence below is the latch, not the floor")
        again, unchanged = codexresets.watch_notice(second, latch)
        self.assertIsNone(again, "the same refusal is not news")
        self.assertEqual(unchanged, latch)
        # THE CONTROL ON THE LATCH'S SUBJECT: a 429 attempt is an EVENT — the
        # cool-down bounds how often it can happen — so it is not in the
        # latch at all, and the identical call renders an empty digest.
        ResetHandler.consume_mode = "429"
        backoff = codexresets.reset_pass(
            [row], reading_age_s=0.0, url_base=self.base,
            accounts=[account_of(row)],
            ledger=os.path.join(self.tmp, "latched-429.jsonl"), now=start,
            probe=lambda _a: reading(weekly_pct=0))
        self.assertEqual(backoff[0]["outcome"],
                         codexresets.OUTCOME_RATE_LIMITED)
        self.assertEqual(codexresets.blocked_digest(backoff), "")
        self.assertTrue(codexresets.blocked_digest(first))


class ManualDoorTest(FakeVendorCase):
    """`helm codex resets --consume <account>` names the account by its
    DISPLAY LABEL, and a display label is not an identity: the label falls
    back email -> truncated account id -> pool file, and on a Team plan the
    account id is the WORKSPACE's, shared by every member."""

    def pair(self):
        """Two members of ONE workspace, neither reading carrying an email —
        so both render the same name."""
        return [reading(email="", account_id="acct-one-workspace",
                        file="codex-m1.json"),
                reading(email="", account_id="acct-one-workspace",
                        file="codex-m2.json")]

    def door(self, readings, target, accounts=None):
        """`_run_consume` with the pool and the snapshot mocked, the base
        pointed at THIS test's vendor, and stderr captured."""
        err = io.StringIO()
        accounts = [account_of(r) for r in readings] \
            if accounts is None else accounts
        with mock.patch.object(codexbudget, "cached_budget",
                               return_value=(readings, 60.0)), \
                mock.patch.object(codexresets, "_pool_accounts",
                                  return_value=(accounts, True)), \
                mock.patch.dict(os.environ,
                                {"HELM_CODEX_RESETS_BASE_URL": self.base}), \
                mock.patch.object(codexresets, "_cooling_now",
                                  return_value=None), \
                contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = codexresets._run_consume(target)
        return rc, err.getvalue()

    def test_the_handle_the_refusal_names_a_credential_by_is_this_value(self):  # noqa: VACUOUS_ASSERTION — the two literal handles above the absence are the unconditional positives on the same observable: a degenerate short_file fails them before any assertNotIn is reached
        """THE REFUSAL'S WHOLE JOB IS TO TELL TWO CREDENTIALS APART, so the
        handle is pinned to a LITERAL. An arm that derives what it expects by
        calling the function under test moves with it: `short_file` degraded
        to a constant would name both candidates identically and still read
        green, leaving the owner no way to tell which is which in the one
        message that exists to distinguish them."""
        self.assertEqual(codexresets.short_file("codex-m1.json"), "bcd5cf…")
        self.assertEqual(codexresets.short_file("codex-m2.json"), "014c58…")
        # AND THE TWO PROPERTIES THE REFUSAL RESTS ON: distinct files get
        # distinct handles, and no handle carries the file name, because a
        # pool file is named from the account's own spelling and can be an
        # email address.
        self.assertNotEqual(codexresets.short_file("codex-m1.json"),
                            codexresets.short_file("codex-m2.json"))
        for name in ("codex-m1.json", "you@fixture.invalid.json"):
            handle = codexresets.short_file(name)
            self.assertNotIn(name.split(".")[0], handle)
            self.assertEqual(len(handle), 7, "six hex and an ellipsis")
            self.assertTrue(set(handle[:6]) <= set("0123456789abcdef"),
                            "the handle is a hex digest prefix")

    def test_an_ambiguous_name_refuses_and_names_the_candidates(self):  # noqa: VACUOUS_ASSERTION — the empty recorder IS the subject, and the unconditional must-hit below fills it through the same recorder
        rows = self.pair()
        target = codexresets.label(rows[0])
        self.assertEqual(target, codexresets.label(rows[1]),
                         "fixture: the two readings must share one label")
        rc, err = self.door(rows, target)
        self.assertEqual(rc, 2)
        self.assertEqual(ResetHandler.seen, [],
                         "the ambiguous door reached the vendor")
        self.assertIn("refusing to guess", err)
        for r in rows:
            self.assertIn(codexresets.short_file(r["file"]), err,
                          "the refusal must name WHICH credentials it means")
            self.assertNotIn(r["file"], err,
                             "a pool file name can carry an email")
        self.assertNoSecret(err, "the refusal")
        # MUST-HIT on the same recorder: it registers a request when one is
        # made, so the emptiness above is a measurement of the refusal.
        codexresets.list_credits(self.account, url_base=self.base)
        self.assertEqual([r["path"] for r in ResetHandler.seen],
                         [codexresets.LIST_PATH])

    def test_control_a_name_one_account_carries_goes_through(self):
        # THE SAME DOOR, one reading, everything else identical: it acts, so
        # the refusal above is the ambiguity and not a door that never sends.
        row = reading()
        rc, err = self.door([row], codexresets.label(row))
        self.assertEqual(rc, 0, err)
        self.assertEqual([r["path"] for r in ResetHandler.seen],
                         [codexresets.LIST_PATH, codexresets.CONSUME_PATH,
                          codexresets.LIST_PATH])

    def test_a_name_no_account_carries_still_refuses(self):
        rc, err = self.door([reading()], "nobody@fixture.invalid")
        self.assertEqual(rc, 2)
        self.assertIn("no pooled codex account named", err)
        self.assertEqual(ResetHandler.seen, [])
        # MUST-HIT: the same recorder fills when a call is made.
        codexresets.list_credits(self.account, url_base=self.base)
        self.assertEqual([r["path"] for r in ResetHandler.seen],
                         [codexresets.LIST_PATH])


class SurfaceTest(unittest.TestCase):
    """The columns say what they hold, and the doc's sample block is rendered
    by the shipped renderer rather than transcribed beside it."""

    ROW = {"account": "you@example.com", "spendable": 2, "weekly_pct": 100.0,
           "reset_in_s": 483728, "status": codexresets.LIST_OK, "note": None}

    def test_the_last_column_says_it_is_a_read_status(self):
        header = codexresets.list_header()
        self.assertIn("balance read", header)
        self.assertIn("credits", header)
        line = codexresets.list_line(self.ROW)
        self.assertIn("listed", line)
        self.assertIn(" 2 ", line)

    def test_no_pooled_credential_is_not_rendered_as_unauthorized(self):
        row = dict(self.ROW, spendable=None,
                   status=codexresets.LIST_NO_CREDENTIAL,
                   note="no single pooled credential serves this account")
        line = codexresets.list_line(row)
        self.assertIn(codexresets.LIST_NO_CREDENTIAL, line)
        self.assertNotIn(codexresets.LIST_UNAUTHORIZED, line,
                         "a credential helm never had is not one the vendor "
                         "refused")

    def test_the_listing_path_renders_an_unbindable_row_as_no_credential(self):
        """Through `_listing_rows`, not a hand-built row: the status an
        operator reads comes from the same resolution the acting path uses,
        and "no pooled credential" is a different fact from "the vendor
        refused this token"."""
        from helm import codexbudget
        row = reading()
        with mock.patch.object(codexbudget, "cached_budget",
                               return_value=([row], 60.0)), \
                mock.patch.object(codexresets, "_pool_accounts",
                                  return_value=([], True)):
            rows, note, _r, _a = codexresets._listing_rows("http://127.0.0.1:1")
        self.assertEqual([r["status"] for r in rows],
                         [codexresets.LIST_NO_CREDENTIAL])
        self.assertIn("no single pooled credential", rows[0]["note"])
        # MUST-HIT on the same call: a pool that DOES bind reads a status the
        # vendor produced, so the answer above is the resolution and not a
        # function that always says the same thing.
        with mock.patch.object(codexbudget, "cached_budget",
                               return_value=([row], 60.0)), \
                mock.patch.object(codexresets, "_pool_accounts",
                                  return_value=([account_of(row)], True)):
            bound, _n, _r, _a = codexresets._listing_rows("http://127.0.0.1:1")
        self.assertEqual([r["status"] for r in bound],
                         [codexresets.LIST_UNREACHABLE])

    def test_the_docs_sample_block_is_what_the_verb_prints(self):
        doc = os.path.join(os.path.dirname(HERE), "docs", "VERBS.md")
        with open(doc, encoding="utf-8") as f:
            text = f.read()
        self.assertIn(codexresets.list_header().rstrip(), text,
                      "docs/VERBS.md quotes a table header the verb does not "
                      "print")
        dry = codexresets.dry_run_line(
            {"account": "you@example.com", "action": codexresets.NO_ACT,
             "reason": codexresets.R_NOT_EXHAUSTED,
             "detail": "the weekly window is 41% used, not spent"})
        self.assertIn(dry.rstrip(), text,
                      "docs/VERBS.md quotes a dry-run line the verb does not "
                      "print")

class CliDoorTest(unittest.TestCase):
    def test_the_manual_door_refuses_to_pick_an_account(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = codexresets.cmd_resets(["--consume"])
        self.assertEqual(rc, 2)
        self.assertIn("wants a value", err.getvalue())

    def test_junk_refuses_through_the_real_verb(self):
        # Through `cli.main`, so the arm covers the codex dispatcher's wiring
        # as well as this verb's own tail guard.
        from helm import cli
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = cli.main(["codex", "resets", "--bogus"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown arg", err.getvalue())

    def test_the_help_tail_names_the_three_doors(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = codexresets.cmd_resets(["--help"])
        self.assertEqual(rc, 0)
        self.assertIn("resets", out.getvalue())
        for door in ("--dry-run", "--consume", "--json"):
            self.assertIn(door, out.getvalue())


if __name__ == "__main__":
    unittest.main()


class MemberIdentityStabilityTest(WorkspaceIdentityBase):
    """task/2734's MEDIUM, cured: the member digest is a function of the
    (account, user) pair, and the pair can LOSE its user half when the claim
    stops being readable — a new digest, an empty history, a cool-down never
    consulted, and the same wall spent twice. History reads must find both
    spellings; a census fold must never merge the unknown half into a known
    sibling; and an identity that cannot be proven blocks the spend."""

    def test_history_written_known_is_found_when_the_claim_drops_out(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive controls on the same observables are inside: pass one MUST spend (len(posts1) == 1) and the digest MUST have moved (assertNotEqual) before the empty-posts assertion is read
        """THE VERIFIER'S PROBE: one wall, user id known on the first pass,
        unreadable on the second. Before the cure the second pass keyed a
        fresh digest, saw no history, and spent again. After it, the blanked
        variant is asked too and the cool-down answers."""
        rows = self.members()
        pool = self.pool(rows)
        walled = rows[1]
        # PASS ONE, identity complete: the wall is taken and journaled.
        ResetHandler.balances = {TOKEN_A: 2, TOKEN_B: 2, TOKEN_C: 2}
        out1 = codexresets.reset_pass([walled], reading_age_s=0.0,
                                      url_base=self.base, accounts=pool,
                                      ledger=self.ledger,
                                      probe=lambda _a: reading(weekly_pct=0))
        posts1 = self.consume_requests()
        self.assertEqual(len(posts1), 1, "the first pass must spend")
        known_digest = codexresets.member_id(pool[1])
        # THE CLAIM DROPS OUT: the same credential, unreadable user id.
        degraded = dict(pool[1])
        degraded["user_id"] = None
        pool2 = [pool[0], degraded, pool[2]]
        # THE DIGEST MOVED, and the arm says so out loud — without that, the
        # whole test measures nothing.
        self.assertNotEqual(codexresets.member_id(degraded), known_digest,
                            "fixture: the identity change must move the "
                            "digest, or the second pass is not the defect's")
        # PASS TWO, sixty seconds later, same wall.
        out2 = codexresets.reset_pass([walled], reading_age_s=0.0,
                                      url_base=self.base, accounts=pool2,
                                      ledger=self.ledger,
                                      probe=lambda _a: reading(weekly_pct=0))
        posts2 = self.consume_requests()[len(posts1):]
        self.assertEqual(posts2, [],
                         "the second pass found no history and spent again: "
                         "%r" % (posts2,))
        self.assertEqual(out2[0]["reason"], codexresets.R_COOLDOWN)

    def test_a_member_digests_read_matches_both_spellings(self):
        from helm import codexhomes
        both = codexhomes.member_digests("acct-x", "user-1",
                                         codexresets.MEMBER_ID_LEN)
        self.assertEqual(len(both), 2)
        self.assertEqual(both[0], codexresets.member_id(
            {"account_id": "acct-x", "user_id": "user-1"}))
        self.assertEqual(both[1], codexresets.member_id(
            {"account_id": "acct-x"}),
            "the second spelling is the unknown-user digest of the SAME "
            "account — the key the first pass wrote under while the claim "
            "was unreadable")
        unknown_only = codexhomes.member_digests("acct-x", None,
                                                 codexresets.MEMBER_ID_LEN)
        self.assertEqual(unknown_only, (both[1],))
        # attempts_for selects through the tuple.
        rows = [{"member": both[1], "ts": 1.0},
                {"member": "somebody-else", "ts": 2.0}]
        got = codexresets.attempts_for(rows, both)
        self.assertEqual([r["ts"] for r in got], [1.0])

    def test_an_unknown_user_id_neither_folds_nor_matches(self):
        """Two questions: whether two records may share ONE ROW
        (`_same_census_member`), and whether a record is an identity's
        credential (`_serves_member`). On a workspace an unknown user id is
        no to the first and UNPROVEN to the second (task/2981). The matcher
        that answered yes on the account id alone is gone."""
        from helm import codexhomes
        self.assertFalse(codexhomes._same_census_member(
            ("acct-one-workspace", None), ("acct-one-workspace", "user-b")))
        self.assertIsNone(codexhomes._serves_member(
            {"user_id": "user-b", "email": None, "plan": "team"}, None, None,
            False))
        # THE CONTROLS: both halves known and equal is yes to both
        self.assertTrue(codexhomes._same_census_member(
            ("acct-one-workspace", "user-b"), ("acct-one-workspace", "user-b")))
        self.assertIs(codexhomes._serves_member(
            {"user_id": "user-b", "email": None, "plan": "team"}, "user-b",
            None, True), True)


