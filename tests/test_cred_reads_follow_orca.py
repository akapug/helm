#!/usr/bin/env python3
"""task/2283 — every helm reader that MEASURES a Claude account reads through a
LIVE copy of that account, and where Orca maintains the account that is
Orca's copy, read-only.

THE OWNER'S MEASUREMENT: every account works in Orca, while
`helm creds` read one account "due-refresh (helm's token expired 19h ago)",
listed Orca's baseline account as "declared, not measured", `cred sync-orca`
refused a home whose chain the token endpoint had refused 26 times ("nothing
proves the home's own login chain is spent"), and the relay's read_credit
answered "no live access token". Each is helm reading its own unmaintained
copy of an account Orca keeps live.

Hermetic: HOME, the claude homes root, the default home, Orca's user-data dir,
the cred backup root and helm's cache all live under one temp dir; Orca's
runtime RPC is mocked at `OrcaAdapter.rpc` with the real accounts.list
snapshot shape; the vendor usage call is mocked at `_get_json`. Every token is
a synthetic FAKE string.
"""
import calendar
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from helm import cred, homes, keepalive, providers, remote_credit
from helm.harness import OrcaAdapter
from helm.providers import NativeQuotaProvider, ProviderError

EMAIL = "acct@x.example"
ORCA_ID = "0a1b2c3d-0000-4000-8000-00000000cafe"


def _ms(hours=0.0):
    return int((time.time() + hours * 3600) * 1000)


def _iso(hours):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + hours * 3600))


def _oauth(token, hours, refresh_days=15):
    return {"claudeAiOauth": {
        "accessToken": token + "-ACCESS", "refreshToken": token + "-REFRESH",
        "expiresAt": _ms(hours), "refreshTokenExpiresAt": _ms(refresh_days * 24),
        "scopes": ["user:inference"], "subscriptionType": "max"}}


def _usage(pct5h, pct7d):
    return {"limits": [
        {"kind": "session", "percent": pct5h, "resets_at": _iso(2)},
        {"kind": "weekly_all", "percent": pct7d, "resets_at": _iso(72)}]}


class ReadsFollowOrcaBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-reads-follow-orca-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        j = lambda *p: os.path.join(self.tmp, *p)
        self.orca = j("orca")
        for patch in (
                mock.patch.dict(os.environ, {
                    "HOME": self.tmp,
                    "HELM_CACHE_DIR": j("cache"),
                    "HELM_CRED_BACKUP_ROOT": j("cred-backups"),
                    "ORCA_USER_DATA_PATH": self.orca}),
                mock.patch.dict(homes.ROOTS, {"claude": j(".claude-homes")}),
                mock.patch.dict(homes.DEFAULTS, {"claude": j(".claude")}),
                mock.patch.object(cred, "holders_of", lambda p, default=False: []),
                # Orca's runtime is never the real one: no snapshot unless an
                # arm hands one over
                mock.patch.object(OrcaAdapter, "rpc",
                                  return_value=(None, "no orca runtime in tests"))):
            patch.start()
            self.addCleanup(patch.stop)
        for var in ("HELM_PROVIDER", "HELM_ALLOCATION_RULES", "HELM_PROBE_LOOP"):
            os.environ.pop(var, None)
        os.makedirs(homes.ROOTS["claude"])
        cred.cache_clear()
        self.addCleanup(cred.cache_clear)
        self.p = NativeQuotaProvider(history_path=j("history.jsonl"))
        self.p._active_homes = lambda: set()

    def home(self, name, email=EMAIL, creds=None, org=None):
        d = os.path.join(homes.ROOTS["claude"], name)
        os.makedirs(d, exist_ok=True)
        oa = {"emailAddress": email, "organizationType": "claude_max",
              "organizationRateLimitTier": "default_claude_max_20x"}
        if org:
            oa["organizationUuid"] = org
        with open(os.path.join(d, ".claude.json"), "w") as f:
            json.dump({"oauthAccount": oa}, f)
        if creds is not None:
            with open(os.path.join(d, ".credentials.json"), "w") as f:
                json.dump(creds, f)
        cred.cache_clear()
        return d

    def orca_account(self, email=EMAIL, creds=None, uuid=ORCA_ID, org=None):
        auth = os.path.join(self.orca, "claude-accounts", uuid, "auth")
        os.makedirs(auth, exist_ok=True)
        doc = {"emailAddress": email, "accountUuid": "acct-1",
               "organizationType": "claude_max",
               "organizationRateLimitTier": "default_claude_max_20x"}
        if org:
            doc["organizationUuid"] = org
        with open(os.path.join(auth, "oauth-account.json"), "w") as f:
            json.dump(doc, f)
        with open(os.path.join(auth, ".orca-managed-claude-auth"), "w") as f:
            f.write(uuid)
        with open(os.path.join(auth, ".credentials.json"), "w") as f:
            json.dump(creds if creds is not None else _oauth("FAKE-ORCA", 6, 25), f)
        return auth

    def rows(self, get_json):
        with mock.patch.object(self.p, "_get_json", get_json), \
                mock.patch.object(providers.time, "sleep"):
            return {r["account"]: r for r in self.p.cred_state()}

    def snapshot(self, updated_ms, session_reset_ms):
        return {"claude": {"accounts": [{"id": "acc-a", "email": EMAIL}],
                           "activeAccountId": "acc-other",
                           "activeAccountIdsByRuntime": {"host": "acc-other"}},
                "rateLimits": {
                    "claude": None,
                    "claudeTarget": {"runtime": "host", "wslDistro": None},
                    "inactiveClaudeAccounts": [{
                        "accountId": "acc-a", "updatedAt": updated_ms,
                        "isFetching": False,
                        "rateLimits": {
                            "provider": "claude", "status": "ok", "error": None,
                            "updatedAt": updated_ms,
                            "session": {"usedPercent": 30, "windowMinutes": 300,
                                        "resetsAt": session_reset_ms,
                                        "resetDescription": None},
                            "weekly": {"usedPercent": 99, "windowMinutes": 10080,
                                       "resetsAt": _ms(72),
                                       "resetDescription": None},
                            "usageMetadata": {"authProvenance": "managed:acc-a"}}}]}}

    @staticmethod
    def bytes_of(path):
        with open(os.path.join(path, ".credentials.json"), "rb") as fh:
            return fh.read()


class AnAccountOrcaKeepsLiveIsMeasuredThroughOrcasCopyTest(ReadsFollowOrcaBase):
    def test_helms_expired_copy_is_not_the_accounts_reading(self):  # noqa: VACUOUS_ASSERTION — "ok" in the same state set is the positive control
        """THE OWNER'S CASE: helm's home copy expired 19h ago on a refresh
        chain nobody grants on, while Orca's managed copy of the same account
        is live. The account is read through Orca's copy (one GET, Orca's
        file untouched) and reads its real numbers; the home's own dead copy
        is kept as the HOME's state, never as the account's."""
        h = self.home("acct-x-example", creds=_oauth("FAKE-HELM", -19))
        auth = self.orca_account()
        before = self.bytes_of(auth)
        get = mock.Mock(return_value=_usage(40, 70))
        rows = self.rows(get)
        get.assert_called_once()
        self.assertEqual(get.call_args[0][1]["Authorization"],
                         "Bearer FAKE-ORCA-ACCESS")
        row = rows[EMAIL]
        self.assertEqual((row["cred_state"], row["headroom_pct"]), ("ok", 60.0))
        self.assertEqual(row["home"], h)
        self.assertEqual(row["home_state"], "due-refresh")
        states = {r["cred_state"] for r in rows.values()}
        self.assertIn("ok", states)
        self.assertNotIn("due-refresh", states)
        self.assertEqual(self.bytes_of(auth), before)   # read-only

    def test_orcas_fresher_live_copy_is_picked_over_stale_home_copy(self):  # noqa: VACUOUS_ASSERTION — get called once with Bearer token and row ok state are positive controls
        """When a helm home has a live token expiring soon, but Orca's managed
        copy was refreshed and has a later expiry, _best_copy ranks Orca's
        copy alongside candidate homes and selects Orca's fresher copy."""
        self.home("acct-x-example", creds=_oauth("FAKE-HELM", 1))
        auth = self.orca_account(creds=_oauth("FAKE-ORCA", 8, 25))
        before = self.bytes_of(auth)
        get = mock.Mock(return_value=_usage(40, 70))
        rows = self.rows(get)
        get.assert_called_once()
        self.assertEqual(get.call_args[0][1]["Authorization"],
                         "Bearer FAKE-ORCA-ACCESS")
        row = rows[EMAIL]
        self.assertEqual((row["cred_state"], row["headroom_pct"]), ("ok", 60.0))
        self.assertEqual(row["source"], "orca")
        self.assertEqual(self.bytes_of(auth), before)

    def test_a_home_whose_own_copy_is_dead_still_cannot_take_a_seat(self):
        """The guard the row's new reading must not lift: allocate places a
        seat on a HOME, and a dead home copy opens a "Not logged in" pane
        whatever the account reads."""
        self.home("acct-x-example", creds=_oauth("FAKE-HELM", -19))
        self.orca_account()
        with mock.patch.object(self.p, "_get_json", return_value=_usage(10, 10)), \
                mock.patch.object(providers.time, "sleep"):
            ranked = self.p.allocate("opus")
        row = next(r for r in ranked if r["account"] == EMAIL)
        self.assertFalse(row["eligible"])
        self.assertTrue(row["blocked_by"])

    def test_an_account_only_orca_holds_is_listed_and_measured_never_launched(self):
        """Orca's baseline account had no helm credhome and read "declared,
        not measured". It is an account the fleet draws on: listed, measured
        through Orca's copy, and never a launch target (no helm home holds
        it, and helm never runs a seat on Orca's own dir)."""
        self.orca_account(email="base@x.example")
        self.home("other-x-example", email="other@x.example",
                  creds=_oauth("FAKE-OTHER", 6))
        accts = {a["name"]: a for a in self.p.accounts()}
        self.assertIn("base@x.example", accts)
        a = accts["base@x.example"]
        self.assertEqual((a["provider"], a["usable"], a["source"], a["home"]),
                         ("anthropic", False, "orca", None))
        get = mock.Mock(return_value=_usage(5, 20))
        rows = self.rows(get)
        self.assertIn("Bearer FAKE-ORCA-ACCESS",
                      {c[0][1]["Authorization"] for c in get.call_args_list})
        self.assertEqual((rows["base@x.example"]["cred_state"],
                          rows["base@x.example"]["headroom_pct"]), ("ok", 95.0))
        with mock.patch.object(self.p, "_get_json", return_value=_usage(5, 20)), \
                mock.patch.object(providers.time, "sleep"):
            ranked = {r["account"] for r in self.p.allocate("opus")}
        self.assertIn("other@x.example", ranked)
        self.assertNotIn("base@x.example", ranked)
        with self.assertRaises(ProviderError):
            self.p.launch_cmd("base@x.example", "sid-1")


class NoLiveCopyReadsOrcasOwnMeasurementTest(ReadsFollowOrcaBase):
    def test_orcas_reading_stands_in_and_no_token_is_presented(self):
        self.home("acct-x-example", creds=_oauth("FAKE-HELM", -19))
        self.orca_account(creds=_oauth("FAKE-ORCA", -1, 25))
        at = _ms(-0.2)
        get = mock.Mock(side_effect=AssertionError("no copy is live: no GET"))
        with mock.patch.object(OrcaAdapter, "rpc",
                               return_value=(self.snapshot(at, _ms(2)), None)):
            rows = self.rows(get)
        row = rows[EMAIL]
        self.assertEqual((row["cred_state"], row["headroom_pct"]), ("ok", 70.0))
        self.assertEqual(row["source"], "orca-reading")
        self.assertEqual({w["label"]: w["used_percent"] for w in row["windows"]},
                         {"5h": 30.0, "7d": 99.0})
        self.assertIn("cannot take a seat", row["home_note"])
        self.assertEqual(calendar.timegm(time.strptime(row["source_at"],
                                                       "%Y-%m-%dT%H:%M:%SZ")),
                         at // 1000)
        self.assertNotEqual(row["cred_state"], "due-refresh")

    def test_a_window_that_ended_since_orcas_reading_is_unread(self):
        """Orca's reading is from before a 5h reset: that window's percent is
        about a window that no longer exists, so it is unread, never the
        headroom; the weekly it measured still stands."""
        self.orca_account(creds=_oauth("FAKE-ORCA", -1, 25))
        with mock.patch.object(OrcaAdapter, "rpc",
                               return_value=(self.snapshot(_ms(-7), _ms(-2)), None)):
            rows = self.rows(mock.Mock(side_effect=AssertionError("no GET")))
        self.assertIsNone(rows[EMAIL]["headroom_pct"])
        self.assertEqual(rows[EMAIL]["cred_state"], "ok")

    def test_no_copy_and_no_reading_says_so_without_blaming_helms_copy(self):
        self.home("acct-x-example", creds=_oauth("FAKE-HELM", -19))
        self.orca_account(creds=_oauth("FAKE-ORCA", -1, 25))
        rows = self.rows(mock.Mock(side_effect=AssertionError("no GET")))
        row = rows[EMAIL]
        self.assertEqual((row["cred_state"], row["status"]),
                         ("unknown", "no-live-copy"))
        self.assertIn("Orca", row["note"])
        self.assertNotIn("keepalive", row["note"])
        self.assertNotIn("/login", row["note"])


class ReviewFindingsTest(ReadsFollowOrcaBase):
    """The integrator's read of tip ca8fdbc70d2b: three mechanical findings."""

    def test_a_dead_home_fresher_than_orcas_dead_copy_is_still_not_seatable(self):
        """HIGH: every copy is dead and the helm home's token expired AFTER
        Orca's copy, so the home ranks as the read. Orca's reading stands in
        for the account; the home must still carry its own dead state and
        never be allocated."""
        self.home("acct-x-example", creds=_oauth("FAKE-HELM", -1))
        self.orca_account(creds=_oauth("FAKE-ORCA", -5, 25))
        get = mock.Mock(side_effect=AssertionError("no copy is live: no GET"))
        with mock.patch.object(OrcaAdapter, "rpc",
                               return_value=(self.snapshot(_ms(-0.2), _ms(2)), None)):
            rows = self.rows(get)
            ranked = {r["account"]: r for r in self.p.allocate("opus")}
        row = rows[EMAIL]
        self.assertEqual((row["cred_state"], row["source"]), ("ok", "orca-reading"))
        self.assertIn(row["home_state"], ("due-refresh", "expired-token"))
        self.assertFalse(ranked[EMAIL]["eligible"])
        self.assertIn(row["home_state"], ranked[EMAIL]["blocked_by"])

    def test_orcas_copy_of_another_organization_is_not_this_accounts_copy(self):
        """LOW 1: one email can sit in several organizations. Orca's copy is
        this home's account only when the identity keys both sides carry
        agree; another organization's live token is never presented."""
        self.home("acct-x-example", creds=_oauth("FAKE-HELM", -19), org="org-home")
        self.orca_account(org="org-orca")
        get = mock.Mock(side_effect=AssertionError("no copy of THIS account is live"))
        rows = self.rows(get)
        self.assertEqual(rows[EMAIL]["cred_state"], "due-refresh")
        self.assertNotEqual(rows[EMAIL].get("source"), "orca")
        r = remote_credit.read_credit("~/.claude-homes/acct-x-example",
                                      get_json=get)
        self.assertIn("error", r)
        # CONTROL: the same organization on both sides is the same account
        self.orca_account(org="org-home")
        self.p._cred_cache = None
        get_same = mock.Mock(return_value=_usage(10, 10))
        rows = self.rows(get_same)
        self.assertEqual(get_same.call_args[0][1]["Authorization"],
                         "Bearer FAKE-ORCA-ACCESS")

    def test_an_orca_account_whose_name_is_taken_still_lists(self):
        """LOW 2: an Orca-only account's name colliding with another row
        once ran os.path.basename(None) while listing."""
        d = os.path.join(self.tmp, ".codex-homes", "base@x.example")
        os.makedirs(d)
        with open(os.path.join(d, "auth.json"), "w") as f:
            json.dump({"tokens": {"access_token": "FAKE-CX"}}, f)
        self.orca_account(email="base@x.example")
        names = [a["name"] for a in self.p.accounts()]
        self.assertIn("base@x.example", names)
        self.assertEqual(len([n for n in names if n.startswith("base@x.example")]), 2)

    def test_an_orca_account_gone_before_the_probe_is_a_row_not_a_raise(self):
        """LOW 2: the Orca account listed by accounts() can be gone by the
        time cred_state looks for its copy; that group has no read at all."""
        auth = self.orca_account(email="base@x.example")
        listed = self.p._orca_accounts()
        with mock.patch.object(self.p, "_orca_accounts",
                               side_effect=[listed, []]):
            rows = self.rows(mock.Mock(side_effect=AssertionError("no GET")))
        self.assertEqual((rows["base@x.example"]["cred_state"],
                          rows["base@x.example"]["status"]),
                         ("unknown", "no-live-copy"))
        self.assertTrue(os.path.isdir(auth))


class KeepaliveOutcomeIsTheClaudeRowsTest(ReadsFollowOrcaBase):
    def log(self, *rows):
        path = keepalive._log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def test_a_codex_row_for_the_same_home_name_is_not_the_claude_outcome(self):
        """MEASURED: the keepalive sweep logs the claude home, then the codex
        home of the same name ("stale-risk"). last_outcome read the codex row
        as the claude home's decision, so `helm creds` told the owner
        keepalive would refresh a home keepalive had seen refused 26 times."""
        self.log({"ts": "2026-09-30T09:55:51-0700", "by": "keepalive-cron",
                  "home": "acct-x-example", "account": EMAIL,
                  "action": "needs_reauth",
                  "reason": "refresh HTTP 400 — one-time re-login needed"},
                 {"ts": "2026-09-30T09:55:52-0700", "by": "keepalive-cron",
                  "home": "acct-x-example", "provider": "codex",
                  "action": "stale-risk", "reason": "auth.json untouched 1938h"})
        self.assertEqual(keepalive.last_outcome("acct-x-example")["action"],
                         "needs_reauth")

    def test_a_server_error_on_the_grant_is_transient_not_a_reauth(self):
        import io
        import urllib.error
        d = self.home("five-oh-three", creds={"claudeAiOauth": {
            "accessToken": "A", "refreshToken": "R", "expiresAt": 0}})
        err = urllib.error.HTTPError("https://x", 503, "unavailable", {},
                                     io.BytesIO(b""))
        with mock.patch.object(keepalive, "_live_holder_pid", lambda p: None), \
                mock.patch.object(keepalive, "cli_identity",
                                  return_value=({"client_id": "c", "user_agent": "u",
                                                 "cli": "x"}, None)), \
                mock.patch("urllib.request.urlopen", side_effect=err):
            res = keepalive.refresh_home(d, apply=True)
        self.assertEqual(res["action"], "error")
        self.assertIn("503", res["reason"])
        self.assertNotIn("re-login", res["reason"])


class TheTokenEndpointsRefusalProvesTheChainSpentTest(ReadsFollowOrcaBase):
    def refuse(self, home, when_s, family=True):
        """One keepalive refusal row, as the shipped sweep writes it."""
        with open(os.path.join(home, ".credentials.json")) as fh:
            tok = json.load(fh)["claudeAiOauth"]["refreshToken"]
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S+0000", time.gmtime(when_s)),
               "by": "keepalive-cron", "home": os.path.basename(home),
               "account": EMAIL, "action": "needs_reauth", "http": 400,
               "reason": "refresh HTTP 400 — one-time re-login needed"}
        if family:
            row["family"] = keepalive.refresh_family(tok)
        path = keepalive._log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(row) + "\n")

    def test_a_home_the_endpoint_refused_is_stale_and_syncs_without_a_login(self):
        """sync-orca refused ("nothing proves the home's own login chain is
        spent") a home whose refresh token the token endpoint had answered
        HTTP 400 on every hour for a day. That answer IS the proof."""
        h = self.home("acct-x-example", creds=_oauth("FAKE-HELM", -19))
        self.orca_account()
        self.refuse(h, time.time() - 60)
        res = cred.sync(h)
        self.assertEqual(res["verdict"], cred.STALE)
        self.assertEqual(res["action"], "would-sync")
        self.assertIn("refused", res["reason"])
        self.assertNotIn("/login", res["reason"])

    def test_an_old_row_before_the_home_changed_is_not_proof(self):  # noqa: VACUOUS_ASSERTION — the same fixture reads STALE before the file changes
        """A refusal logged before the credentials file last changed was
        about other bytes (a later login replaced them): OWN-CHAIN stands."""
        h = self.home("acct-x-example", creds=_oauth("FAKE-HELM", -19))
        self.orca_account()
        then = time.time() - 7200
        os.utime(os.path.join(h, ".credentials.json"), (then, then))
        self.refuse(h, time.time() - 3600, family=False)
        self.assertEqual(cred.sync(h)["verdict"], cred.STALE)
        now = time.time()
        os.utime(os.path.join(h, ".credentials.json"), (now, now))
        self.assertEqual(cred.sync(h)["verdict"], cred.OWN_CHAIN)

    def test_a_refusal_of_another_refresh_token_is_not_proof(self):  # noqa: VACUOUS_ASSERTION — the same fixture reads STALE before the re-login
        h = self.home("acct-x-example", creds=_oauth("FAKE-HELM", -19))
        self.orca_account()
        self.refuse(h, time.time() - 60)
        self.assertEqual(cred.sync(h)["verdict"], cred.STALE)
        with open(os.path.join(h, ".credentials.json"), "w") as f:
            json.dump(_oauth("FAKE-RELOGIN", -19), f)
        cred.cache_clear()
        self.assertEqual(cred.sync(h)["verdict"], cred.OWN_CHAIN)


class RemoteCreditReadsTheLiveCopyTest(ReadsFollowOrcaBase):
    def test_a_tilde_home_is_expanded_and_read_through_orcas_live_copy(self):  # noqa: VACUOUS_ASSERTION — the GET's bearer and the credit read are the positive controls
        self.home("acct-x-example", creds=_oauth("FAKE-HELM", -19))
        auth = self.orca_account()
        before = self.bytes_of(auth)
        body = {remote_credit.CREDIT_KEY: {"remaining_dollars": 40.0,
                                           "used_dollars": 10.0,
                                           "limit_dollars": 50.0,
                                           "resets_at": "2026-10-30T00:00:00Z"}}
        get = mock.Mock(return_value=body)
        r = remote_credit.read_credit("~/.claude-homes/acct-x-example",
                                      get_json=get)
        self.assertEqual((r.get("account"), r.get("left")), (EMAIL, 40.0))
        self.assertEqual(get.call_args[0][1]["Authorization"],
                         "Bearer FAKE-ORCA-ACCESS")
        self.assertEqual(self.bytes_of(auth), before)
        self.assertTrue(remote_credit.readable("~/.claude-homes/acct-x-example"))

    def test_no_live_copy_names_every_copy_it_looked_at(self):
        self.home("acct-x-example", creds=_oauth("FAKE-HELM", -19))
        self.orca_account(creds=_oauth("FAKE-ORCA", -1, 25))
        r = remote_credit.read_credit("~/.claude-homes/acct-x-example",
                                      get_json=mock.Mock(side_effect=AssertionError))
        self.assertIn("Orca", r["error"])
        self.assertNotEqual(r["error"], "no live access token")


if __name__ == "__main__":
    unittest.main()
