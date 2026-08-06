#!/usr/bin/env python3
"""seat CREDENTIAL truth — a probe that cannot read a cred must say UNKNOWN,
never deliver a verdict about the credential.

THE DEFECT, measured on the live fleet. `helm seat doctor` rendered:

    gemini  proxy UP pid <N> port <P>   <account> — cred unparseable

while that proxy was UP on :8390, serving, with a pool file carrying
disabled=false and a perfectly readable `expired` stamp. The cause was a
JWT-ONLY reader: `_seat_row` decoded `access_token` as a JWT and read the `exp`
claim, which is correct for codex and grok and returns NOTHING for
gemini/antigravity, whose access_token is an opaque Google token and whose
expiry authority is the record's own `expired` field. No claims was rendered as
"cred unparseable" — a finding about the CREDENTIAL produced by a limitation of
the READER. The operator is told to go re-mint working auth.

Second instance, same file, machine-readable channel: `_doctor` gated its EXIT
STATUS on `not err`, and the fully-pooled codex case returns prose through the
`err` channel that literally ends "which is the normal state". A healthy fleet
exited 1. Prose had been fixed; the status a cron reads had not.

These tests pin all three outcomes as DISTINGUISHABLE, and pin the guidance
half: an UNKNOWN carries what to do AND what not to do, because "do NOT
respawn" is what stops an operator destroying working state on a blind report.

NO REAL CREDENTIAL IS READ OR COPIED HERE. Every fixture is a synthetic file in
a tmp dir with obviously-fake token text; the JWT fixtures are assembled from
base64 of a claims dict, so nothing resembling a live token exists in tests/.
"""
import base64
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import seat  # noqa: E402


def _fake_jwt(exp):
    """A synthetic 3-segment token whose payload carries `exp`. NOT a
    credential: the signature segment is the literal string "not-a-signature"
    and the header is fake."""
    def seg(obj):
        raw = json.dumps(obj).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return "%s.%s.%s" % (seg({"alg": "none"}), seg({"exp": int(exp)}),
                         "not-a-signature")


def _rfc(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


class _Pool:
    """A synthetic seat auth-dir."""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, "auth")
        os.makedirs(self.dir)
        return self

    def __exit__(self, *exc):
        self.tmp.cleanup()
        return False

    def write(self, name, blob):
        p = os.path.join(self.dir, name)
        with open(p, "w") as f:
            json.dump(blob, f)
        return p


class CredStateTriStateTest(unittest.TestCase):
    """VALID / EXPIRED / ABSENT / UNKNOWN — and UNKNOWN never reads as ABSENT."""

    def test_jwt_cred_is_valid_and_names_the_jwt_authority(self):
        future = time.time() + 40 * 3600
        with _Pool() as pool:
            pool.write("codex-fake.json", {
                "email": "fleet@example.invalid", "disabled": False,
                "access_token": _fake_jwt(future),
            })
            state, detail, email = seat.cred_state(pool.dir)
        self.assertEqual(state, seat.CRED_VALID)
        self.assertEqual(email, "fleet@example.invalid")
        self.assertIn("valid until", detail)
        self.assertIn("per jwt", detail)

    def test_non_jwt_cred_with_expired_field_is_VALID_not_unparseable(self):
        """THE LIVE DEFECT. An opaque (non-JWT) access_token plus an `expired`
        stamp in the future is a WORKING credential. The JWT-only reader called
        this "cred unparseable" while the proxy served traffic."""
        future = time.time() + 5 * 3600
        with _Pool() as pool:
            pool.write("antigravity-fake.json", {
                "email": "fleet@example.invalid", "disabled": False,
                "access_token": "opaque-not-a-jwt-placeholder",
                "expired": _rfc(future),
            })
            state, detail, email = seat.cred_state(pool.dir)
        self.assertEqual(state, seat.CRED_VALID)
        self.assertIn("valid until", detail)
        self.assertIn("per expired-field", detail)
        self.assertNotIn("unparseable", detail)

    def test_readable_dir_with_no_cred_file_is_a_REAL_absence(self):
        """A genuine negative must still read as one — the fix must not launder
        every absence into UNKNOWN."""
        with _Pool() as pool:
            state, detail, _email = seat.cred_state(pool.dir)
        self.assertEqual(state, seat.CRED_ABSENT)
        self.assertIn("no cred file", detail)
        self.assertNotIn("UNKNOWN", detail)
        self.assertIn("pool one", detail)      # says what to DO

    def test_missing_dir_is_UNKNOWN_with_do_NOT_guidance(self):
        """A path this probe cannot stat says so. It is NOT evidence about the
        credential, and the message must carry the do-NOT half."""
        with _Pool() as pool:
            gone = os.path.join(pool.dir, "nope", "deeper")
            state, detail, _email = seat.cred_state(gone)
        self.assertEqual(state, seat.CRED_UNKNOWN)
        self.assertIn("cannot read the seat auth-dir", detail)
        self.assertIn("do NOT", detail)
        self.assertIn("respawn", detail)

    def test_unparseable_file_is_UNKNOWN_never_ABSENT(self):
        """Garbage on disk means this probe cannot tell. The proxy may hold a
        good cred in memory, so the verdict channel stays silent."""
        with _Pool() as pool:
            with open(os.path.join(pool.dir, "broken.json"), "w") as f:
                f.write("{this is not json")
            state, detail, _email = seat.cred_state(pool.dir)
        self.assertEqual(state, seat.CRED_UNKNOWN)
        self.assertNotEqual(state, seat.CRED_ABSENT)
        self.assertIn("UNKNOWN", detail)
        self.assertIn("does not parse as JSON", detail)
        self.assertIn("do NOT", detail)

    def test_cred_with_no_expiry_authority_is_UNKNOWN(self):
        """Parseable JSON, real tokens, but neither a JWT `exp` nor an
        `expired` field: an UNEXPECTED SHAPE is a cannot-tell, not a finding."""
        with _Pool() as pool:
            pool.write("odd-shape.json", {
                "email": "fleet@example.invalid",
                "access_token": "opaque-not-a-jwt-placeholder",
            })
            state, detail, _email = seat.cred_state(pool.dir)
        self.assertEqual(state, seat.CRED_UNKNOWN)
        self.assertIn("no readable expiry", detail)
        self.assertIn("do NOT", detail)

    def test_expired_cred_is_a_REAL_negative_with_a_remedy(self):
        past = time.time() - 3600
        with _Pool() as pool:
            pool.write("codex-fake.json", {
                "email": "fleet@example.invalid", "disabled": False,
                "access_token": _fake_jwt(past),
            })
            state, detail, _email = seat.cred_state(pool.dir)
        self.assertEqual(state, seat.CRED_EXPIRED)
        self.assertIn("EXPIRED", detail)
        self.assertIn("re-login", detail)

    def test_proxy_disabled_cred_is_a_REAL_negative(self):
        with _Pool() as pool:
            pool.write("codex-fake.json", {
                "email": "fleet@example.invalid", "disabled": True,
                "access_token": _fake_jwt(time.time() + 9999),
            })
            state, detail, _email = seat.cred_state(pool.dir)
        self.assertEqual(state, seat.CRED_EXPIRED)
        self.assertIn("DISABLED", detail)

    def test_a_pool_is_any_of_not_first_of(self):
        """The old reader took creds[0] ALPHABETICALLY and let it speak for the
        seat, so one dead sibling reported a live pool as dead. The proxy
        hot-reloads the whole dir and uses any live cred in it."""
        with _Pool() as pool:
            pool.write("aaa-dead.json", {
                "email": "dead@example.invalid", "disabled": False,
                "access_token": _fake_jwt(time.time() - 3600),
            })
            pool.write("zzz-live.json", {
                "email": "live@example.invalid", "disabled": False,
                "access_token": _fake_jwt(time.time() + 30 * 3600),
            })
            state, detail, email = seat.cred_state(pool.dir)
        self.assertEqual(state, seat.CRED_VALID)
        self.assertEqual(email, "live@example.invalid")
        self.assertIn("+1 more pooled", detail)

    def test_unreadable_pool_does_not_outrank_a_real_finding(self):
        """A pool holding one provably-expired cred AND one unreadable file has
        a real negative to report; UNKNOWN is for when there is nothing else."""
        with _Pool() as pool:
            pool.write("aaa-expired.json", {
                "email": "fleet@example.invalid", "disabled": False,
                "access_token": _fake_jwt(time.time() - 60),
            })
            with open(os.path.join(pool.dir, "zzz-broken.json"), "w") as f:
                f.write("not json at all")
            state, _detail, _email = seat.cred_state(pool.dir)
        self.assertEqual(state, seat.CRED_EXPIRED)


class CredExpiryAuthorityTest(unittest.TestCase):
    """`_cred_expiry` reads BOTH authorities; None means neither, not expired."""

    def test_jwt_wins_when_present(self):
        exp = int(time.time() + 100)
        got, authority = seat._cred_expiry({"access_token": _fake_jwt(exp)})
        self.assertEqual(int(got), exp)
        self.assertEqual(authority, "jwt")

    def test_nested_codex_home_shape_still_reads(self):
        exp = int(time.time() + 100)
        got, authority = seat._cred_expiry(
            {"tokens": {"access_token": _fake_jwt(exp)}})
        self.assertEqual(int(got), exp)
        self.assertEqual(authority, "jwt")

    def test_expired_field_is_the_fallback_authority(self):
        when = time.time() + 7200
        got, authority = seat._cred_expiry({
            "access_token": "opaque-not-a-jwt-placeholder",
            "expired": _rfc(when)})
        self.assertEqual(authority, "expired-field")
        self.assertAlmostEqual(got, when, delta=2)

    def test_neither_authority_is_None_not_zero(self):
        got, authority = seat._cred_expiry({"access_token": "opaque"})
        self.assertIsNone(got)
        self.assertIsNone(authority)

    def test_cred_exp_reads_the_expired_field_shape_from_disk(self):
        """`_cred_exp` returned None for this shape, and None reads as expired
        at every call site."""
        when = time.time() + 7200
        with _Pool() as pool:
            p = pool.write("antigravity-fake.json", {
                "access_token": "opaque-not-a-jwt-placeholder",
                "expired": _rfc(when)})
            self.assertAlmostEqual(seat._cred_exp(p), when, delta=2)


class SeatRowRendersUnknownTest(unittest.TestCase):
    """The SURFACE, not just the primitive: `_seat_row`'s cred column."""

    def _row(self, family, blobs, mode=None):
        with _Pool() as pool:
            for name, blob in blobs.items():
                pool.write(name, blob)
            seat_home = os.path.dirname(pool.dir)
            fams = dict(seat.FAMILIES)
            fams[family] = dict(fams.get(family) or {}, **({"mode": mode} if mode else {}))
            import unittest.mock as m
            with m.patch.object(seat, "seat_dir", lambda f, *a, **k: seat_home), \
                 m.patch.object(seat, "FAMILIES", fams), \
                 m.patch.object(seat, "_proxy_live_text",
                                lambda *a, **k: ("proxy UP pid 1 port 2", None)), \
                 m.patch.object(seat, "_minted_instances", lambda *a, **k: []):
                return seat._seat_row(family)

    def test_non_jwt_cred_row_does_not_say_unparseable(self):
        row = self._row("gemini", {"antigravity-fake.json": {
            "email": "fleet@example.invalid", "disabled": False,
            "access_token": "opaque-not-a-jwt-placeholder",
            "expired": _rfc(time.time() + 6 * 3600)}})
        self.assertNotIn("unparseable", row)
        self.assertIn("valid until", row)

    def test_proxy_key_family_with_no_pooled_cred_keeps_its_api_key_label(self):
        """A REAL absence in a proxy-key family is not a problem: the key is
        baked into config.yaml. Pinned so the UNKNOWN work cannot turn this
        into an alarm (ds4pro and kimi are live examples)."""
        row = self._row("ds4pro", {}, mode="proxy-key")
        self.assertIn("api-key cred", row)
        self.assertNotIn("UNKNOWN", row)
        self.assertNotIn("no cred file", row)

    def test_proxy_key_family_with_NO_AUTH_DIR_keeps_its_api_key_label(self):
        """THE REGRESSION DOGFOODING CAUGHT. ds4pro and kimi have no auth-dir at
        all — a MISSING dir is UNKNOWN, not ABSENT, so gating the api-key label
        on CRED_ABSENT alone put a do-NOT-respawn warning on two healthy seats.
        The unit fixture created the dir and never saw it; `helm seat doctor`
        did. This test uses a path that does not exist, like the real seats."""
        import unittest.mock as m
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "seat-with-no-auth-dir")
            os.makedirs(missing)
            fams = dict(seat.FAMILIES)
            fams["ds4pro"] = dict(fams.get("ds4pro") or {}, mode="proxy-key")
            with m.patch.object(seat, "seat_dir", lambda f, *a, **k: missing), \
                 m.patch.object(seat, "FAMILIES", fams), \
                 m.patch.object(seat, "_proxy_live_text",
                                lambda *a, **k: ("proxy UP pid 1 port 2", None)), \
                 m.patch.object(seat, "_minted_instances", lambda *a, **k: []):
                row = seat._seat_row("ds4pro")
        self.assertIn("api-key cred", row)
        self.assertNotIn("UNKNOWN", row)
        self.assertNotIn("do NOT", row)

    def test_proxy_key_family_still_reports_a_pooled_cred(self):
        """Precedence preserved from the pre-fix reader: a pooled cred file
        speaks even for a proxy-key family."""
        row = self._row("ds4pro", {"pooled-fake.json": {
            "email": "fleet@example.invalid", "disabled": False,
            "access_token": _fake_jwt(time.time() + 8 * 3600)}}, mode="proxy-key")
        self.assertIn("valid until", row)
        self.assertNotIn("api-key cred", row)

    def test_unreadable_cred_row_says_UNKNOWN_and_do_NOT(self):
        with _Pool() as pool:
            with open(os.path.join(pool.dir, "broken.json"), "w") as f:
                f.write("{nope")
            seat_home = os.path.dirname(pool.dir)
            import unittest.mock as m
            with m.patch.object(seat, "seat_dir", lambda f, *a, **k: seat_home), \
                 m.patch.object(seat, "_proxy_live_text",
                                lambda *a, **k: ("proxy UP pid 1 port 2", None)), \
                 m.patch.object(seat, "_minted_instances", lambda *a, **k: []):
                row = seat._seat_row("gemini")
        self.assertIn("UNKNOWN", row)
        self.assertIn("do NOT", row)
        self.assertNotIn("no cred", row)


class DoctorExitStatusTest(unittest.TestCase):
    """The machine-readable verdict: a fully-pooled fleet is NOT a failure."""

    def test_pooled_codex_is_CRED_VALID_not_an_error(self):
        """`newest_valid_codex_auth` reports "nothing un-pooled to translate"
        for `seat add`; that same fact must not reach an exit code as a
        credential failure."""
        import unittest.mock as m
        with _Pool() as pool:
            pool.write("codex-fake.json", {
                "email": "fleet@example.invalid", "disabled": False,
                "access_token": _fake_jwt(time.time() + 200 * 3600)})
            with m.patch.object(seat, "_newest_unpooled_codex_auth",
                               lambda: (None, "not applicable: pooled")), \
                 m.patch("helm.codexhomes.pool_dir", lambda: pool.dir):
                state, src, detail = seat.codex_cred_state()
        self.assertEqual(state, seat.CRED_VALID)
        self.assertIsNone(src)
        self.assertNotIn(state, (seat.CRED_EXPIRED, seat.CRED_ABSENT))
        self.assertIn("not applicable", detail)

    def test_unreadable_pool_is_UNKNOWN_not_absent(self):
        import unittest.mock as m
        with m.patch.object(seat, "_newest_unpooled_codex_auth",
                           lambda: (None, "no un-pooled home")), \
             m.patch("helm.codexhomes.pool_dir",
                     lambda: "/nonexistent-synthetic/pool"):
            state, _src, detail = seat.codex_cred_state()
        self.assertEqual(state, seat.CRED_UNKNOWN)
        self.assertIn("do NOT", detail)

    def test_empty_pool_and_no_home_is_a_REAL_absence(self):
        import unittest.mock as m
        with _Pool() as pool:
            with m.patch.object(seat, "_newest_unpooled_codex_auth",
                               lambda: (None, "no valid codex cred anywhere")), \
                 m.patch("helm.codexhomes.pool_dir", lambda: pool.dir):
                state, _src, _detail = seat.codex_cred_state()
        self.assertEqual(state, seat.CRED_ABSENT)


if __name__ == "__main__":
    unittest.main()


class CredRemainderAndRollingTest(unittest.TestCase):
    """THE LINE SAID THE SAME THING ABOUT TWO OPPOSITE STATES.

    Measured live: `helm seat doctor` rendered gemini as
    "valid until 22:33:41Z (0h left, per expired-field)" — eight minutes, on a
    token that had been refreshing hourly for FOURTEEN HOURS. Two separate
    defects compose into that reading:

      `(exp - now) // 3600` truncates EVERY sub-hour remainder to `0h`, and
      nothing distinguished "expiring, and the proxy will silently mint
      another in seconds" from "expiring, and the seat stops".

    A reader who acts on it is responding to an integer truncation. A seat was
    very nearly announced as about to die mid-lane on the strength of it, and
    only the proxy's 14-hour uptime falsified the alarm before it was sent.

    The EXPIRED branch already named the refresh_token, so the code knew — it
    only said so AFTER expiry, when the reassurance is worthless."""

    def test_a_sub_hour_remainder_does_not_collapse_to_zero(self):
        # POSITIVE CONTROL first, unconditional: the long case still reads in
        # hours, so a failure below is the sub-hour arm and not a unit rewrite.
        self.assertIn("h", seat._cred_remaining(50000))
        self.assertEqual(seat._cred_remaining(508), "8m")
        self.assertEqual(seat._cred_remaining(3599), "59m")
        self.assertNotIn("0h", seat._cred_remaining(508))

    def test_a_sub_minute_remainder_does_not_collapse_either(self):
        """The same bug one unit down: rounding 40s to '0m' would re-create it."""
        self.assertEqual(seat._cred_remaining(40), "<1m")
        self.assertNotIn("0m", seat._cred_remaining(40))

    def test_a_rolling_cred_says_a_low_remainder_is_normal(self):
        """Both facts come from the RECORD and neither is a prediction: a short
        `expires_in` means the stamp is designed to roll, a `refresh_token`
        means the proxy holds what it needs to roll it."""
        soon = time.time() + 500
        with _Pool() as pool:
            pool.write("gemini-fake.json", {
                "email": "roll@example.invalid", "disabled": False,
                "expired": _rfc(soon),
                "expires_in": 3599, "refresh_token": "REFRESH-SENTINEL",
            })
            state, detail, _email = seat.cred_state(pool.dir)
        self.assertEqual(state, seat.CRED_VALID)
        self.assertIn("ROLLING", detail)
        self.assertIn("8m left", detail, "the remainder must not read 0h")
        self.assertIn("NORMAL", detail)
        # AND IT NEVER RENDERS THE TOKEN. Presence only, like _cred_expiry.
        self.assertNotIn("REFRESH-SENTINEL", detail)

    def test_a_cred_with_no_refresh_token_gets_NO_reassurance(self):
        """THE ARM THAT KEEPS THIS FROM LAUNDERING EVERY EXPIRY INTO FINE. A
        token that genuinely cannot roll must read as genuinely expiring —
        otherwise the fix makes a dying seat look healthy, which is worse than
        the truncation it replaces."""
        soon = time.time() + 500
        with _Pool() as pool:
            pool.write("dying-fake.json", {
                "email": "dying@example.invalid", "disabled": False,
                "expired": _rfc(soon), "expires_in": 3599,
            })
            state, detail, _email = seat.cred_state(pool.dir)
        self.assertEqual(state, seat.CRED_VALID)
        # POSITIVE CONTROL on the same observable: the line rendered, and it
        # carries the honest remainder — so the missing ROLLING is the arm.
        self.assertIn("8m left", detail)
        self.assertNotIn("ROLLING", detail)

    def test_a_refresh_token_with_no_usable_lifetime_claims_less(self):
        """Capability without a lifetime cannot say 'a low remainder is
        normal' — it can only say the proxy CAN mint another."""
        soon = time.time() + 500
        with _Pool() as pool:
            pool.write("nolife-fake.json", {
                "email": "nolife@example.invalid", "disabled": False,
                "expired": _rfc(soon), "refresh_token": "x",
            })
            _state, detail, _email = seat.cred_state(pool.dir)
        self.assertIn("ROLLING", detail)
        self.assertIn("can mint another", detail)
        self.assertNotIn("NORMAL", detail,
                         "without a lifetime it must not assert normality")

    def test_an_implausible_lifetime_is_not_trusted_as_a_cadence(self):
        """A 999999s 'lifetime' is not an hourly cadence and must not be
        reported as one — bad input degrades to the weaker sentence."""
        self.assertIn("can mint another",
                      seat._cred_rolling({"refresh_token": "x",
                                          "expires_in": 999999}))
        self.assertEqual(seat._cred_rolling({"expires_in": 3599}), "",
                         "no refresh_token means no rolling claim at all")
        self.assertEqual(seat._cred_rolling(None), "")
