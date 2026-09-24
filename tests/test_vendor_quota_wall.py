"""A vendor QUOTA wall is not a bad key.

MEASURED: kimi answered the canary HTTP 403 with the body "You've
reached your weekly (7-day) usage limit", and its own usages endpoint showed
the week spent with a resetTime. The code-only rule (401/403 -> AUTH-401) read
that as a dead credential, so `helm burn` showed kimi RED on the REACH axis,
"unknown when". These arms drive the real classifier, the real record
composition and the real burn fold:

  1. kimi's real 403 body is a QUOTA-WALL, read on the MONEY axis with a reset;
  2. a genuine 401 "invalid or may have expired" stays AUTH-401 on reach;
  3. a 403 whose body names nothing stays AUTH-401;
  4. a 403 whose only "quota" is the proxy's own envelope code stays AUTH-401;
  5. a vendor's own JSON quota code is a wall on 403 and 429 — the proxy's
     stamp is told apart by its envelope, never by the word;
  6. gemini's RESOURCE_EXHAUSTED 429 that says "per minute" reads RATE-LIMITED;
  7. a 403 our proxy marks local is OURS (PROXY-LOCAL-403, ORANGE on reach)
     whatever its body says — origin is read before the quota words;
  8. a vendor code written after a long message still names the wall: the
     classifier reads the whole bounded body and only the detail is cut;
  9. a reset written after a long message is still the wall's horizon: the
     reset is read from that same whole body, and only the instant is kept.

The proxy envelope around the kimi message is the shape CLIProxyAPI's claude
handler renders for a 403 (`claudeErrorTypeFromStatus`); the message text is
the measured one.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import burnflags as bf  # noqa: E402
from helm import proxywatch  # noqa: E402

KIMI_MESSAGE = "You've reached your weekly (7-day) usage limit"
KIMI_403 = json.dumps({"type": "error", "error": {
    "type": "permission_error", "message": KIMI_MESSAGE}})
EXPIRED_401 = json.dumps({"type": "error", "error": {
    "type": "authentication_error",
    "message": "The API key is invalid or may have expired"}})
UNRELATED_403 = json.dumps({"type": "error", "error": {
    "type": "permission_error",
    "message": "Request not allowed from this region"}})


def _openai_403(message):
    """The OpenAI-shaped envelope CLIProxyAPI renders for every 403 whose
    upstream text is not JSON (`BuildErrorResponseBody`,
    sdk/api/handlers/handlers.go): the code is `insufficient_quota` whatever
    the upstream said."""
    return json.dumps({"error": {"message": message,
                                 "type": "permission_error",
                                 "code": "insufficient_quota"}})


# A vendor's OWN insufficient_quota body, in the shape OpenAI documents for
# it (`type` and `code` both insufficient_quota, plus `param`). The message is
# constructed without the word "quota", so only the vendor's code can decide.
VENDOR_MESSAGE = "Please check your plan and billing details."
VENDOR_QUOTA = json.dumps({"error": {
    "message": VENDOR_MESSAGE, "type": "insufficient_quota", "param": None,
    "code": "insufficient_quota"}})
# The same body as the claude handler renders it for the canary
# (`claudeErrorDetailFromText` keeps the vendor's error type and message).
VENDOR_QUOTA_CLAUDE = json.dumps({"type": "error", "error": {
    "type": "insufficient_quota", "message": VENDOR_MESSAGE}})
# Gemini's per-minute RESOURCE_EXHAUSTED (Google's published wording; the
# claude handler types it rate_limit_error because the body has no type).
GEMINI_PER_MINUTE_MESSAGE = (
    "Quota exceeded for quota metric 'Generate Content API requests per "
    "minute' and limit 'GenerateContent request limit per minute for a "
    "region' of service 'generativelanguage.googleapis.com' for consumer "
    "'project_number:0'.")
GEMINI_PER_MINUTE = json.dumps({"error": {
    "code": 429, "message": GEMINI_PER_MINUTE_MESSAGE,
    "status": "RESOURCE_EXHAUSTED"}})
GEMINI_PER_MINUTE_CLAUDE = json.dumps({"type": "error", "error": {
    "type": "rate_limit_error", "message": GEMINI_PER_MINUTE_MESSAGE}})
CYBER = json.dumps({"error": {
    "message": "This content was flagged for possible cybersecurity risk. "
               "See https://chatgpt.com/cyber",
    "type": "invalid_request_error"}})


def _canary(code, body, headers=None):
    def refuse(_req, timeout=None):
        raise urllib.error.HTTPError("u", code, "no", headers or {},
                                     io.BytesIO(body.encode("utf-8")))
    with mock.patch("urllib.request.urlopen", side_effect=refuse):
        state, detail, _ms, status, _m, _t = proxywatch._canary_once(
            "http://127.0.0.1:8318", "tok", "kimi-model")
    return state, detail, status


LOCAL = {"X-CPA-Refusal-Origin": "local"}
SELECTED_TRACE = {"X-CPA-TRACE-ID": "20260923130000-0123456789abcdef-deadbeef"}


class VendorQuotaWallTest(unittest.TestCase):

    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.tmp = td.name
        self.cfg = os.path.join(td.name, "proxywatch-vendor-resets.json")
        p = mock.patch.object(proxywatch, "_vendor_resets_path",
                              return_value=self.cfg)
        p.start()
        self.addCleanup(p.stop)
        self.now = time.time()
        self.assertIn("kimi", bf.families(),
                      "the arms re-key onto the real family vocabulary")

    def _owner(self, resets_at_ms, recorded_at):
        """Plant one owner-entered kimi horizon exactly as the config holds
        it. `write_vendor_reset` stamps the wall clock and refuses a past
        horizon; the arms below need both instants chosen."""
        with open(self.cfg, "w", encoding="utf-8") as f:
            json.dump({"kimi": {"resets_at_ms": resets_at_ms,
                                "reset_kind": "vendor",
                                "reset_source": "owner",
                                "recorded_at": proxywatch._iso(recorded_at)}},
                      f)

    def _record(self, state, detail, owner_reset_ms=None, before=None,
                owner_recorded_at=None):
        """One seat observation -> the persisted kimi family record.

        The owner's page value is recorded an hour BEFORE the refusal is
        measured unless the arm says otherwise: which of the two readings of
        the vendor clock is newer decides between them (task/2935)."""
        if owner_reset_ms is not None:
            rec, err = proxywatch.write_vendor_reset("kimi", owner_reset_ms)
            self.assertIsNone(err, err)
            self._owner(owner_reset_ms, self.now - 3600
                        if owner_recorded_at is None else owner_recorded_at)
        seat = proxywatch._compose_upstream_seat(
            "kimi", (state, detail, 12), {}, self.now)
        rep = {"upstream": {"kimi": {
            "state": state, "since": seat["since"], "dark": seat["dark"],
            "seats": {"kimi": seat}}}}
        upstream, err = proxywatch._compose_upstream_records(
            rep, {"kimi": before} if before else {})
        self.assertIsNone(err, err)
        return upstream["kimi"]

    def _flag(self, record):
        return bf.fold({"upstream": {"kimi": record}},
                       now=self.now)["families"]["kimi"]

    def _burn(self, code, body, headers):
        """One refused canary through proxywatch's production writer to the
        snapshot `helm burn` reads -> (flag, `helm burn why kimi` text, the
        bytes both persisted files hold)."""
        def refuse(_req, timeout=None):
            raise urllib.error.HTTPError("u", code, "no", dict(headers),
                                         io.BytesIO(body.encode("utf-8")))
        watch = os.path.join(self.tmp, "proxywatch.json")
        snap = os.path.join(self.tmp, "burn-flags.json")
        with mock.patch.object(proxywatch, "_state_path", return_value=watch), \
                mock.patch.object(proxywatch, "UPSTREAM_CONFIRM_S", 0), \
                mock.patch.object(proxywatch, "_proxy_incarnation",
                                  return_value=(None, "NO-REPRESENTATIVE")), \
                mock.patch("helm.pi.seat_port", return_value=(8318, None)), \
                mock.patch("helm.pi._pi_api_key", return_value="tok"), \
                mock.patch("urllib.request.urlopen", side_effect=refuse), \
                mock.patch.object(bf, "snapshot_path", return_value=snap):
            current = proxywatch.upstream_health(
                [{"seat": "kimi", "family": "kimi", "probe": "healthy"}],
                now=self.now, prior={})
            self.assertTrue(proxywatch.record(
                {"ts": self.now, "seats": [], "upstream": current},
                prior_state={}), "the production state writer refused")
            self.assertTrue(bf.write_snapshot(
                {"upstream": bf.upstream_block(now=self.now)}, now=self.now))
            out, text = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(bf.cmd_burn(["why", "kimi", "--json"]), 0)
            with contextlib.redirect_stdout(text):
                bf.cmd_burn(["why", "kimi"])
        stored = []
        for path in (watch, snap):
            with open(path, encoding="utf-8") as f:
                stored.append(f.read())
        return json.loads(out.getvalue()), text.getvalue(), "".join(stored)

    # ------------------------------------------------------------ arm 1

    def test_kimis_real_403_is_a_quota_wall_on_money_with_the_owners_reset(self):  # noqa: VACUOUS_ASSERTION — the owner-filled reset and its expiry are positively asserted on the same record and flag before the no-owner control asserts their absence
        state, detail, status = _canary(403, KIMI_403)
        self.assertEqual((state, status), ("QUOTA-WALL", 403))
        self.assertIn(KIMI_MESSAGE, detail,
                      "the must-hit must carry kimi's measured body")
        # THE INTEGRATOR'S RULING (task/2935): the refusal carries no reset,
        # so this member's reset is UNKNOWN, and a FUTURE owner horizon fills
        # exactly that. The family reset is UNKNOWN only where no future owner
        # horizon covers the unknown member.
        owner = int((self.now + 3 * 3600) * 1000)
        record = self._record(state, detail, owner_reset_ms=owner)
        self.assertTrue(record["dark"], "a quota wall still pauses delivery")
        self.assertEqual((record["resets_at_ms"], record["reset_source"]),
                         (owner, "owner"))
        self.assertIsNone(bf.derive_reach("kimi", record, now=self.now),
                          "a quota wall says nothing about reach")
        flag = self._flag(record)
        self.assertEqual((flag["colour"], flag["axis"], flag["cause_id"]),
                         (bf.RED, "money", "money:vendor-quota-wall"))
        self.assertEqual(flag["axes"]["reach"], None)
        self.assertEqual((flag["expires_at"], flag["expires_kind"]),
                         (owner / 1000.0, "vendor-reset"))
        self.assertIn("owner", flag["expires_source"])
        # CONTROL: with no owner horizon the same refusal's reset is UNKNOWN
        # and says so, so the instant above is the owner's and nobody else's
        proxywatch.clear_vendor_reset("kimi")
        record = self._record(state, detail)
        self.assertNotIn("resets_at_ms", record)
        flag = self._flag(record)
        self.assertIsNone(flag["expires_at"])
        self.assertEqual(flag["expires_kind"], "none")
        self.assertIn("unknown when", "\n".join(bf.render(
            {"families": {"kimi": flag}, "overall": {}}, now=self.now)))

    def test_the_flag_and_the_durable_record_resolve_one_family_reset(self):  # noqa: VACUOUS_ASSERTION — the loop runs a fixed five-case tuple, and its first case positively asserts the owner reset on both paths
        """task/2935 finding 2: the owner horizon was joined twice, by two
        rules. The posting pass's fold filled any walled family whose reset
        was missing — a past horizon included — while the durable record
        refused to fill an unknown member at all, so the flag `can_spend`
        reads and the record every other surface reads named different
        instants. ONE helper now decides both. The same inputs go through
        the record composition and through the fold the posting pass runs,
        and both must name the same family reset."""
        at = self.now + 7200
        measured = json.dumps({"type": "error", "error": {
            "type": "permission_error", "message": "%s; resets at %s"
            % (KIMI_MESSAGE, proxywatch._iso(at))}})
        at_ms = int(proxywatch._parse_timestamp(proxywatch._iso(at)) * 1000)
        owner = int((self.now + 3 * 3600) * 1000)
        cases = (
            # (name, body, owner horizon, owner recorded, pass instant, expected)
            ("an unknown member, a future owner horizon", KIMI_403, owner,
             self.now - 3600, self.now, owner),
            ("an unknown member, no owner horizon", KIMI_403, None, None,
             self.now, None),
            ("a measured reset newer than the owner entry", measured, owner,
             self.now - 3600, self.now, at_ms),
            ("an owner entry newer than the measured reset", measured, owner,
             self.now + 60, self.now, owner),
            ("an owner horizon past at the pass instant", KIMI_403, owner,
             self.now - 3600, self.now + 4 * 3600, None),
        )
        for name, body, horizon, recorded, now, expected in cases:
            with self.subTest(name):
                if horizon is None:
                    proxywatch.clear_vendor_reset("kimi")
                else:
                    self._owner(horizon, recorded)
                state, detail, _ = _canary(403, body)
                seat = proxywatch._compose_upstream_seat(
                    "kimi", (state, detail, 12), {}, now)
                rep = {"ts": now, "upstream": {"kimi": {
                    "state": state, "since": seat["since"],
                    "dark": seat["dark"], "seats": {"kimi": seat}}}}
                durable, err = proxywatch._compose_upstream_records(rep, {})
                self.assertIsNone(err, err)
                self.assertEqual(durable["kimi"].get("resets_at_ms"), expected)
                posted = bf.fold({"upstream": rep["upstream"],
                                  "upstream_before": {},
                                  "vendor_resets":
                                      proxywatch.read_vendor_resets()[0]},
                                 now=now)["families"]["kimi"]
                persisted = bf.fold({"upstream": durable},
                                    now=now)["families"]["kimi"]
                self.assertEqual(posted["cause_id"], "money:vendor-quota-wall")
                self.assertEqual(posted["expires_at"],
                                 None if expected is None else expected / 1000.0)
                self.assertEqual(
                    (posted["expires_at"], posted["expires_source"]),
                    (persisted["expires_at"], persisted["expires_source"]))

    def test_a_reset_the_refusal_itself_carries_outranks_the_owners(self):
        owner = int((self.now + 3 * 3600) * 1000)
        # (a) an instant in the body
        at = proxywatch._iso(self.now + 7200)
        body = json.dumps({"type": "error", "error": {
            "type": "permission_error",
            "message": "%s; resets at %s" % (KIMI_MESSAGE, at)}})
        state, detail, _ = _canary(403, body)
        record = self._record(state, detail, owner_reset_ms=owner)
        self.assertEqual(record["reset_source"], "canary")
        self.assertEqual(record["resets_at_ms"],
                         int(proxywatch._parse_timestamp(at) * 1000))
        # (b) a Retry-After header on a quota 429
        state, detail, _ = _canary(429, KIMI_403, {"Retry-After": "5400"})
        self.assertEqual(state, "QUOTA-WALL")
        record = self._record(state, detail, owner_reset_ms=owner)
        self.assertEqual(record["reset_source"], "canary")
        self.assertAlmostEqual(record["resets_at_ms"] / 1000.0,
                               self.now + 5400, delta=5)
        # the refusal's reset outranks the owner's only as the NEWER reading
        # of the vendor clock: an owner entry recorded after the refusal was
        # measured replaces it (task/2935)
        record = self._record(state, detail, owner_reset_ms=owner,
                              owner_recorded_at=self.now + 60)
        self.assertEqual((record["resets_at_ms"], record["reset_source"]),
                         (owner, "owner"))
        # CONTROL: with no member instant the owner's future horizon fills
        # the unknown member
        record = self._record(*_canary(403, KIMI_403)[:2],
                              owner_reset_ms=owner)
        self.assertEqual((record["resets_at_ms"], record["reset_source"]),
                         (owner, "owner"))

    def test_a_quota_wall_with_no_reset_known_is_red_and_says_so(self):
        state, detail, _ = _canary(403, KIMI_403)
        record = self._record(state, detail)
        self.assertNotIn("resets_at_ms", record)
        flag = self._flag(record)
        self.assertEqual((flag["colour"], flag["axis"]), (bf.RED, "money"))
        self.assertIsNone(flag["expires_at"])
        self.assertEqual(flag["expires_kind"], "none")
        self.assertIn("no vendor reset known", bf.derive_quota_wall(
            "kimi", record)["expires_source"])
        # an envelope typed invalid_request_error is not credential wording
        self.assertEqual(proxywatch._upstream_state(403, json.dumps(
            {"type": "error", "error": {"type": "invalid_request_error",
                                        "message": KIMI_MESSAGE}})),
            "QUOTA-WALL")

    def test_the_proxy_cooldown_that_follows_the_wall_holds_it_on_money(self):  # noqa: VACUOUS_ASSERTION — the same record key is asserted present on the wall and mirror passes first
        # MEASURED: after kimi's 403 the proxy answered the next
        # canary itself, "1 cooling down (reset in 29m32s)". Between two
        # upstream refusals the family reads PROXY-COOLDOWN, and the wall must
        # not flap back to reach every other pass.
        cooling = ('{"type":"error","error":{"type":"rate_limit_error",'
                   '"message":"All credentials for model kimi-k3 are cooling '
                   'down via provider moonshot"}}')
        owner = int((self.now + 3 * 3600) * 1000)
        wall = self._record(*_canary(403, KIMI_403)[:2], owner_reset_ms=owner)
        self.assertEqual(wall["quota_wall"], "QUOTA-WALL")
        state = proxywatch._upstream_state(429, cooling, origin="local")
        self.assertEqual(state, "PROXY-COOLDOWN")
        mirror = self._record(state, "HTTP 429 — " + cooling, before=wall)
        self.assertEqual(mirror["quota_wall"], "QUOTA-WALL")
        self.assertIsNone(bf.derive_reach("kimi", mirror, now=self.now))
        flag = self._flag(mirror)
        self.assertEqual((flag["colour"], flag["axis"], flag["expires_at"]),
                         (bf.RED, "money", owner / 1000.0))
        self.assertIn("mirrors it", flag["cause"])
        # a second cooldown pass keeps holding it
        again = self._record(state, "HTTP 429", before=mirror)
        self.assertEqual(again["quota_wall"], "QUOTA-WALL")
        # CONTROL: any other state ends it, and a cooldown with no wall
        # before it is still OUR cooldown on reach
        healthy = self._record("HEALTHY", "HTTP 200 with OK", before=again)
        self.assertNotIn("quota_wall", healthy)
        fresh = self._record(state, "HTTP 429", before=healthy)
        self.assertNotIn("quota_wall", fresh)
        self.assertEqual(bf.derive_reach("kimi", fresh, now=self.now)
                         ["cause_id"], "reach:our-cooldown")
        self.assertIsNone(bf.derive_quota_wall("kimi", fresh))

    def test_429_quota_wording_is_a_wall_and_rate_wording_is_a_rate(self):
        cases = ((429, KIMI_MESSAGE, "QUOTA-WALL"),
                 (429, "weekly usage limit reached", "QUOTA-WALL"),
                 (429, "Rate limit exceeded for this organization",
                  "RATE-LIMITED"),
                 (429, "Quota exceeded for quota metric 'Generate Content API "
                       "requests per minute'", "RATE-LIMITED"),
                 (429, '{"type":"error","error":{"type":"rate_limit_error",'
                       '"message":"slow down"}}', "RATE-LIMITED"))
        for code, text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(proxywatch._upstream_state(code, text),
                                 expected)
        # a LOCAL cooldown stays ours whatever its body says
        self.assertEqual(proxywatch._upstream_state(
            429, KIMI_MESSAGE, origin="local"), "PROXY-COOLDOWN")

    # ------------------------------------------------------------ arm 2

    def test_a_genuine_401_invalid_or_expired_stays_auth_on_reach(self):
        state, detail, status = _canary(401, EXPIRED_401)
        self.assertEqual((state, status), ("AUTH-401", 401))
        self.assertEqual(proxywatch._upstream_state(403, EXPIRED_401),
                         "AUTH-401", "credential wording on a 403 is auth")
        self.assertEqual(proxywatch._upstream_state(
            403, "usage limit: the API key is invalid"), "AUTH-401")
        record = self._record(state, detail,
                              owner_reset_ms=int((self.now + 3600) * 1000))
        self.assertIsNone(bf.derive_quota_wall("kimi", record))
        flag = self._flag(dict(record, since="2026-01-01T00:00:00Z"))
        self.assertEqual((flag["colour"], flag["axis"], flag["cause_id"]),
                         (bf.RED, "reach", "reach:upstream-dark"))

    # ------------------------------------------------------------ arm 3

    def test_a_403_whose_body_is_unrelated_stays_auth(self):
        state, _detail, _ = _canary(403, UNRELATED_403)
        self.assertEqual(state, "AUTH-401")
        self.assertEqual(proxywatch._upstream_state(403, ""), "AUTH-401")
        self.assertEqual(proxywatch._upstream_state(403, "Forbidden"),
                         "AUTH-401")

    # ------------------------------------------------------------ arm 4

    def test_the_proxys_own_insufficient_quota_code_is_not_a_quota(self):
        # The proxy's envelope code is not the vendor's word: a plain 403, a
        # region block and a bare empty-message 403 in that envelope carry no
        # quota meaning, so they stay AUTH-401 on both the canary and the
        # classifier.
        forbidden = _openai_403("Forbidden")
        self.assertIn("insufficient_quota", forbidden)
        self.assertEqual(proxywatch._upstream_state(403, forbidden),
                         "AUTH-401")
        for message in ("Forbidden", "Request not allowed from this region",
                        "Forbidden: this model is not enabled for your org"):
            with self.subTest(message=message):
                self.assertEqual(_canary(403, _openai_403(message))[0],
                                 "AUTH-401")
                self.assertEqual(proxywatch._upstream_state(
                    403, _openai_403(message)), "AUTH-401")
        # CONTROL: an explicit quota or credit signal in the vendor's own
        # words is a wall, in the same envelope or none
        walls = (_openai_403("You exceeded your current quota, please check "
                             "your plan and billing details."),
                 _openai_403(KIMI_MESSAGE),
                 "Daily limit reached for this key",
                 "insufficient balance on this account",
                 '{"code":"personal-team-blocked:spending-limit","error":'
                 '"You have run out of credits or need a Grok subscription."}')
        for text in walls:
            with self.subTest(text=text):
                self.assertEqual(proxywatch._upstream_state(403, text),
                                 "QUOTA-WALL")

    # ------------------------------------------------------------ arm 5

    def test_a_vendors_own_insufficient_quota_code_is_a_wall(self):
        # The proxy passes valid upstream JSON through unchanged, so the
        # vendor's code arrives in the vendor's shape and names the wall on
        # both a 403 and a 429, raw and as the canary receives it.
        self.assertEqual(proxywatch._upstream_state(403, VENDOR_QUOTA),
                         "QUOTA-WALL")
        moonshot = json.dumps({"error": {
            "message": "Your account org-x<ak-y> is suspended, please check "
                       "your plan and billing details",
            "type": "exceeded_current_quota_error"}})
        for code in (403, 429):
            for body in (VENDOR_QUOTA, VENDOR_QUOTA_CLAUDE, moonshot):
                with self.subTest(code=code, body=body):
                    self.assertEqual(proxywatch._upstream_state(code, body),
                                     "QUOTA-WALL")
                    self.assertEqual(_canary(code, body)[0], "QUOTA-WALL")
        # CONTROL: the SAME message under the SAME code in the proxy's own
        # stamped envelope is not a wall — only the envelope differs
        stamped = _openai_403(VENDOR_MESSAGE)
        self.assertIn('"code": "insufficient_quota"', stamped)
        self.assertEqual(proxywatch._upstream_state(403, stamped), "AUTH-401")
        self.assertEqual(_canary(403, stamped)[0], "AUTH-401")

    # ------------------------------------------------------------ arm 6

    def test_geminis_per_minute_resource_exhausted_reads_as_a_rate(self):
        # Pins TODAY's reading: "per minute" is rate wording, so this 429
        # stays RATE-LIMITED although it also says "quota". A change here
        # must be a decision, not a side effect.
        state, detail, status = _canary(429, GEMINI_PER_MINUTE)
        self.assertEqual((state, status), ("RATE-LIMITED", 429))
        self.assertIn("RESOURCE_EXHAUSTED", detail)
        for body in (GEMINI_PER_MINUTE, GEMINI_PER_MINUTE_CLAUDE):
            with self.subTest(body=body):
                self.assertEqual(proxywatch._upstream_state(429, body),
                                 "RATE-LIMITED")
                self.assertEqual(_canary(429, body)[0], "RATE-LIMITED")

    # ------------------------------------------------------------ arm 7

    def test_a_403_our_proxy_minted_is_ours_whatever_its_body_says(self):  # noqa: VACUOUS_ASSERTION — the absences (no money wall, no held quota_wall, no cooldown wording) are the contract; each observable's positive side is asserted first: the ORANGE reach flag, the dark PROXY-LOCAL-403 record, the HELM'S OWN PROXY sentence
        """THE REVIEW'S MUST-HIT. A 403 our proxy marks local, with quota words
        in its body and no selected trace, read QUOTA-WALL: vendor money RED,
        "wait for the vendor reset", for a refusal no vendor ever made. The
        persisted flag `helm burn` reads is asserted first.

        PRODUCER SEAM. The installed proxy (7.2.110-helm.11, commit
        4f15bdf7af52) sets `X-CPA-Refusal-Origin: local` at
        internal/logging/cpa_trace.go:120, so the header is live. Its only
        caller (RecordRefusalOrigin, sdk/api/handlers/handlers_errors.go:88)
        marks the selector's modelCooldownError, which renders 429
        (sdk/cliproxy/auth/selector.go:319): at that commit no producer pairs
        the mark with a 403. This arm is the contract for when one does. A
        proxy 403 WITHOUT the mark stays origin unknown, and its body decides
        (arms 4 and 5)."""
        flag, shown, _stored = self._burn(403, KIMI_403, LOCAL)
        self.assertEqual((flag["colour"], flag["axis"], flag["cause_id"]),
                         (bf.ORANGE, "reach", "reach:our-refusal"))
        # kimi has no money reader, so with no wall its money slot reads
        # unmeasured: a refusal no vendor made never sets the money axis
        self.assertEqual(flag["axes"]["money"], bf.GREY)
        self.assertEqual(flag["expires_kind"], "none")
        self.assertIn("our proxy refused this itself (PROXY-LOCAL-403)",
                      flag["cause"])
        self.assertIn("read the proxy's stated reason", flag["cause"])
        for word in ("cooldown", "vendor reset", "wait"):
            self.assertNotIn(word, flag["cause"])
        self.assertIn("PROXY-LOCAL-403", shown)
        # the classifier and the canary, on every quota wording
        for body in (KIMI_403, _openai_403(KIMI_MESSAGE), VENDOR_QUOTA):
            with self.subTest(body=body):
                self.assertEqual(proxywatch._upstream_state(
                    403, body, origin="local"), "PROXY-LOCAL-403")
                state, _detail, status = _canary(403, body, LOCAL)
                self.assertEqual((state, status), ("PROXY-LOCAL-403", 403))
        # a local 403 after a vendor wall ends the wall: nothing ties the two
        wall = self._record(*_canary(403, KIMI_403)[:2])
        self.assertEqual(wall["quota_wall"], "QUOTA-WALL")
        after = self._record("PROXY-LOCAL-403", "HTTP 403", before=wall)
        self.assertEqual((after["state"], after["dark"]),
                         ("PROXY-LOCAL-403", True))
        self.assertNotIn("quota_wall", after)
        self.assertIsNone(bf.derive_quota_wall("kimi", after))
        # the log rung reads the same mark the same way, with or without body
        self.assertEqual(proxywatch._refusal_cause(
            [403] * 3, [KIMI_403] * 3, ["local"] * 3), "PROXY-LOCAL-403")
        self.assertEqual(proxywatch._refusal_cause(
            [403] * 3, None, ["local"] * 3), "PROXY-LOCAL-403")
        # every surface names it ours, and none of them a cooldown
        from helm import seat_usability
        self.assertEqual(proxywatch.dark_origin("PROXY-LOCAL-403"),
                         proxywatch.DARK_OURS)
        why = seat_usability._dark_reason("PROXY-LOCAL-403", "T")
        self.assertIn("HELM'S OWN PROXY", why)
        self.assertNotIn("cooldown", why)
        self.assertEqual(seat_usability._DARK_OURS, {
            s for s in proxywatch._UPSTREAM_DARK
            if proxywatch.dark_origin(s) == proxywatch.DARK_OURS})
        # CONTROL: the same body with no mark, or with the mark beside a
        # selected trace (origin unknown), is still the vendor's wall
        flag, _shown, _stored = self._burn(403, KIMI_403, {})
        self.assertEqual((flag["axis"], flag["cause_id"]),
                         ("money", "money:vendor-quota-wall"))
        self.assertEqual(_canary(403, KIMI_403,
                                 dict(LOCAL, **SELECTED_TRACE))[0],
                         "QUOTA-WALL")
        # Local origin outranks every body classifier on both codes: neither
        # quota nor cyber wording in a refusal our proxy wrote is provider
        # evidence. The same cyber body remains named for upstream/untyped
        # refusals, so the priority cannot erase that diagnostic.
        for code, ours in ((403, "PROXY-LOCAL-403"),
                           (429, "PROXY-COOLDOWN")):
            for body in (KIMI_403, CYBER):
                with self.subTest(code=code, body=body):
                    self.assertEqual(proxywatch._upstream_state(
                        code, body, origin="local"), ours)
        for code in (403, 429, 502):
            with self.subTest(upstream_cyber=code):
                self.assertEqual(proxywatch._upstream_state(code, CYBER),
                                 "CONTENT-FLAGGED/cyber-classifier")

    # ------------------------------------------------------------ arm 8

    def test_a_vendor_code_after_a_long_message_still_names_the_wall(self):  # noqa: VACUOUS_ASSERTION — the uncut tail never being stored is product law; the same detail and stored text carry positive controls (the exact cut detail, QUOTA-WALL persisted)
        # The display is cut at 400 characters. Cutting BEFORE the parse broke
        # the JSON, so a code written after a long message never reached the
        # classifier: a 403 read AUTH-401 and a 429 RATE-LIMITED.
        message = " ".join([VENDOR_MESSAGE] * 12)
        body = json.dumps({"error": {
            "message": message, "type": "insufficient_quota", "param": None,
            "code": "insufficient_quota"}})
        self.assertGreater(body.index("insufficient_quota"), 400)
        for code in (403, 429):
            with self.subTest(code=code):
                state, detail, status = _canary(code, body)
                self.assertEqual((state, status), ("QUOTA-WALL", code))
                # only the detail is cut, and the cut never held the code
                self.assertEqual(detail, "HTTP %d — %s" % (code, body[:400]))
                self.assertNotIn("insufficient_quota", detail)
        flag, _shown, stored = self._burn(403, body, {})
        self.assertEqual((flag["axis"], flag["cause_id"]),
                         ("money", "money:vendor-quota-wall"))
        # the whole body reached the classifier, and its uncut tail was not
        # stored in either persisted file (which do hold the verdict)
        self.assertIn("QUOTA-WALL", stored)
        self.assertNotIn("insufficient_quota", stored)

    # ------------------------------------------------------------ arm 9

    def test_a_reset_written_after_the_cut_is_still_the_horizon(self):  # noqa: VACUOUS_ASSERTION — the uncut tail never being stored is product law; the same stored text carries positive controls (QUOTA-WALL and the reset persisted as resets_at_ms)
        # The reset was parsed from the 400-character detail, so a reset the
        # vendor wrote after a long message was dropped with the cut and the
        # wall read "no vendor reset known".
        at = proxywatch._iso(self.now + 7200)
        body = json.dumps({"type": "error", "error": {
            "type": "permission_error",
            "message": "%s. It resets at %s. See the membership page." % (
                " ".join([KIMI_MESSAGE] * 10), at)}})
        self.assertGreater(body.index(at), 400)
        self.assertGreater(body.index("membership page"), 400)
        for code, headers in ((403, {}), (429, {}),
                              (429, {"Retry-After": "5400"})):
            with self.subTest(code=code, headers=headers):
                state, detail, _ = _canary(code, body, headers)
                self.assertEqual(state, "QUOTA-WALL")
                # the detail is still the cut; it gains the instant only
                self.assertTrue(detail.startswith(
                    "HTTP %d — %s" % (code, body[:400])))
                self.assertNotIn("membership page", detail)
                # the body's instant wins over Retry-After, as before the cut
                record = self._record(state, detail)
                self.assertEqual((record["resets_at_ms"],
                                  record["reset_source"]),
                                 (int(proxywatch._parse_timestamp(at) * 1000),
                                  "canary"))
        flag, _shown, stored = self._burn(403, body, {})
        self.assertEqual((flag["cause_id"], flag["expires_kind"]),
                         ("money:vendor-quota-wall", "vendor-reset"))
        self.assertEqual(flag["expires_at"], proxywatch._parse_timestamp(at))
        # the reset is stored; the uncut tail after it is not
        self.assertIn("QUOTA-WALL", stored)
        self.assertIn('"resets_at_ms": %d' % int(
            proxywatch._parse_timestamp(at) * 1000), stored)
        self.assertNotIn("membership page", stored)
        # CONTROL: an instant in the shown text is not written twice
        near = json.dumps({"type": "error", "error": {
            "type": "permission_error",
            "message": "%s; resets at %s" % (KIMI_MESSAGE, at)}})
        state, detail, _ = _canary(403, near)
        self.assertEqual(detail, "HTTP 403 — %s" % near)
        self.assertEqual(self._record(state, detail)["resets_at_ms"],
                         int(proxywatch._parse_timestamp(at) * 1000))


if __name__ == "__main__":
    unittest.main()
