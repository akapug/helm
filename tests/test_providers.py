#!/usr/bin/env python3
"""providers tests — the native quota provider's parsing + ranking paths.
Hermetic by law: HOME points at a tmp estate, credential fixtures carry FAKE
tokens only, urllib/subprocess are mocked — no live vendor endpoint, real
token, or real process table is ever touched."""
import base64
import contextlib
import io
import json
import os
import shutil
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

from helm import providers
from helm.providers import NativeQuotaProvider, ProviderError


def fake_jwt(email):
    payload = base64.urlsafe_b64encode(json.dumps({"email": email}).encode())
    return "h." + payload.decode().rstrip("=") + ".sig"


class PureHelpersTest(unittest.TestCase):
    def test_epoch_parses_iso_z_and_offset(self):
        self.assertEqual(providers._epoch("1970-01-01T00:01:00Z"), 60)
        self.assertEqual(providers._epoch("1970-01-01T00:01:00+00:00"), 60)
        self.assertIsNone(providers._epoch("garbage"))
        self.assertIsNone(providers._epoch(None))

    def test_jwt_email(self):
        self.assertEqual(providers._jwt_email(fake_jwt("a@c.example")), "a@c.example")
        self.assertIsNone(providers._jwt_email("not-a-jwt"))
        self.assertIsNone(providers._jwt_email(""))

    def test_model_family(self):
        for m in ("gpt-5.5", "o3-pro", "o4-mini", "codex-mini"):
            self.assertEqual(providers._model_family(m), "codex", m)
        for m in ("claude-fable-5", "opus", "", None):
            self.assertEqual(providers._model_family(m), "anthropic", m)

    def test_find_cwd_depth_limited(self):
        self.assertEqual(providers._find_cwd({"a": {"cwd": "/x"}}), "/x")
        deep = {"a": {"b": {"c": {"d": {"cwd": "/deep"}}}}}
        self.assertIsNone(providers._find_cwd(deep))  # beyond depth 3


class AnthropicGaugesTest(unittest.TestCase):
    def test_new_limits_shape(self):
        data = {"limits": [
            {"kind": "session", "percent": 34, "resets_at": "1970-01-01T00:01:00Z"},
            {"kind": "weekly_all", "percent": 12, "resets_at": None},
            {"kind": "weekly_scoped", "percent": 8, "is_active": True,
             "scope": {"model": {"display_name": "Fable"}}},
            {"kind": "weekly_scoped", "percent": 0, "is_active": False,
             "scope": {"model": {"display_name": "Dormant"}}},
            {"kind": "shiny_new", "group": "weekly", "percent": 5}]}
        gauges = NativeQuotaProvider._anthropic_gauges(data)
        by_label = {g["label"]: g for g in gauges}
        self.assertEqual(by_label["5h"]["kind"], "session")
        self.assertEqual(by_label["5h"]["utilization"], 0.34)
        self.assertEqual(by_label["5h"]["reset"], 60)
        self.assertEqual(by_label["7d"]["kind"], "period")
        self.assertEqual(by_label["7d-fable"]["utilization"], 0.08)
        self.assertNotIn("7d-dormant", by_label)  # dormant scoped window = noise
        self.assertEqual(by_label["shiny_new"]["kind"], "period")  # flows through

    def test_missing_percent_remains_unread_not_zero(self):
        measured = NativeQuotaProvider._gauge("5h", "session", 25, 60)
        self.assertEqual(measured["utilization"], 0.25)
        gauge = NativeQuotaProvider._gauge("5h", "session", None, 60)
        self.assertIsNone(gauge["utilization"])

    def test_older_shape_fallback_and_overage(self):
        data = {"five_hour": {"utilization": 50, "resets_at": "1970-01-01T00:01:00Z"},
                "seven_day": {"utilization": 20, "resets_at": None},
                "seven_day_opus": {"utilization": 10},
                "extra_usage": {"is_enabled": True, "utilization": 3}}
        by_label = {g["label"]: g for g in NativeQuotaProvider._anthropic_gauges(data)}
        self.assertEqual(by_label["5h"]["utilization"], 0.5)
        self.assertEqual(by_label["7d"]["utilization"], 0.2)
        self.assertEqual(by_label["7d-opus"]["utilization"], 0.1)
        self.assertEqual(by_label["overage"]["kind"], "overage")

    def test_primary_prefers_account_wide_session(self):
        gauges = [{"label": "7d", "kind": "period", "utilization": 0.9, "reset": None},
                  {"label": "5h-scoped", "kind": "session", "utilization": 0.8, "reset": None},
                  {"label": "5h", "kind": "session", "utilization": 0.1, "reset": 60}]
        self.assertEqual(NativeQuotaProvider._primary(gauges)["label"], "5h")
        # no session gauge -> the fullest gauge wins
        self.assertEqual(NativeQuotaProvider._primary(gauges[:1])["label"], "7d")
        self.assertIsNone(NativeQuotaProvider._primary([]))

    def test_an_unread_gauge_is_never_the_least_full(self):
        """task/2935: with no session gauge the binding is the FULLEST gauge,
        and an unread one may be the fullest. Comparing None against a number
        raised; skipping it would name the measured 30% as the binding and
        report 70% headroom nobody measured. The unread gauge is returned, so
        the headroom that follows from it is unknown."""
        gauges = [{"label": "7d", "kind": "period", "utilization": 0.3, "reset": 9},
                  {"label": "30d", "kind": "period", "utilization": None, "reset": 99}]
        self.assertEqual(NativeQuotaProvider._primary(gauges)["label"], "30d")
        # CONTROL: the same gauges fully read pick the fuller one by number
        gauges[1]["utilization"] = 0.2
        self.assertEqual(NativeQuotaProvider._primary(gauges)["label"], "7d")


class CodexGaugesTest(unittest.TestCase):
    def test_windows_labels_from_seconds_and_scoped_suffix(self):
        data = {"rate_limit": {
            "primary_window": {"limit_window_seconds": 5 * 3600, "used_percent": 40,
                               "reset_at": 111},
            "secondary_window": {"limit_window_seconds": 7 * 86400, "used_percent": 15,
                                 "reset_at": 222}},
            "additional_rate_limits": [
                {"limit_name": "GPT 5.5 Pro!", "rate_limit": {
                    "primary_window": {"limit_window_seconds": 5 * 3600,
                                       "used_percent": 7}}}]}
        by_label = {g["label"]: g for g in NativeQuotaProvider._codex_gauges(data)}
        self.assertEqual(by_label["5h"]["kind"], "session")
        self.assertEqual(by_label["5h"]["utilization"], 0.4)
        self.assertEqual(by_label["7d"]["kind"], "period")
        self.assertEqual(by_label["7d"]["reset"], 222)
        self.assertIn("5h-gpt-5.5-pro", by_label)  # name sanitized into the suffix

    def test_missing_windows_yield_nothing(self):
        self.assertEqual(NativeQuotaProvider._codex_gauges({}), [])


class NativeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-prov-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # HELM_CACHE_DIR lands here too: helm.catalog freezes its cache path
        # from HOME at IMPORT, so a probe that consults helm's keepalive log
        # (the due-refresh sentence does) would otherwise read the operator's
        # real ~/.cache/helm from inside a test that believes it is hermetic.
        self.envp = mock.patch.dict(
            os.environ, {"HOME": self.tmp,
                         "HELM_CACHE_DIR": os.path.join(self.tmp, "cache"),
                         # an empty copy consults Orca's store; never the real one
                         "ORCA_USER_DATA_PATH": os.path.join(self.tmp, "orca")})
        self.envp.start()
        self.addCleanup(self.envp.stop)
        for var in ("HELM_PROVIDER", "HELM_ALLOCATION_RULES", "HELM_PROBE_LOOP"):
            os.environ.pop(var, None)
        self.hist = os.path.join(self.tmp, "history.jsonl")
        self.p = NativeQuotaProvider(history_path=self.hist)
        self.p._active_homes = lambda: set()  # never scan the real /proc

    def claude_home(self, name, email=None, rl="", org="", authed=True):
        d = os.path.join(self.tmp, ".claude-homes", name)
        os.makedirs(d, exist_ok=True)
        if email:
            with open(os.path.join(d, ".claude.json"), "w") as f:
                json.dump({"oauthAccount": {"emailAddress": email,
                                            "organizationType": org,
                                            "organizationRateLimitTier": rl}}, f)
        if authed:
            with open(os.path.join(d, ".credentials.json"), "w") as f:
                json.dump({"claudeAiOauth": {"accessToken": "FAKE-access"}}, f)
        return d

    def blank_stub(self, home):
        """A blanked credentials file: every token field emptied, the file
        itself still there."""
        with open(os.path.join(home, ".credentials.json"), "w") as f:
            json.dump({"claudeAiOauth": {"accessToken": "", "refreshToken": "",
                                         "expiresAt": 0,
                                         "refreshTokenExpiresAt": 1}}, f)

    def codex_home(self, name, email=None, token="FAKE-cx"):
        d = os.path.join(self.tmp, ".codex-homes", name)
        os.makedirs(d, exist_ok=True)
        auth = {"tokens": {"access_token": token, "account_id": "acct-1"}}
        if email:
            auth["tokens"]["id_token"] = fake_jwt(email)
        with open(os.path.join(d, "auth.json"), "w") as f:
            json.dump(auth, f)
        return d


class AccountsTest(NativeBase):
    def test_scan_identity_tier_usable_alias_dedup_and_defaults(self):
        a = self.claude_home("a-home", "u@x.example", rl="max_5x")
        self.claude_home("bare", authed=False)  # no identity, no cred -> unusable
        os.symlink(a, os.path.join(self.tmp, ".claude-homes", "alias"))
        self.codex_home("cx", "c@x.example")
        d = os.path.join(self.tmp, ".claude")  # an authed default home
        os.makedirs(d)
        with open(os.path.join(d, ".credentials.json"), "w") as f:
            json.dump({"claudeAiOauth": {"accessToken": "FAKE"}}, f)
        rows = {r["name"]: r for r in self.p.accounts()}
        self.assertEqual(set(rows), {"u@x.example", "bare", "cx", "(default-claude)"})
        u = rows["u@x.example"]
        self.assertEqual((u["provider"], u["tier"], u["usable"], u["active"]),
                         ("anthropic", "Max 5x", True, False))
        self.assertFalse(rows["bare"]["usable"])
        self.assertEqual(rows["cx"]["email"], "c@x.example")
        self.assertTrue(rows["(default-claude)"]["usable"])

    def test_same_identity_two_homes_disambiguated_never_dropped(self):
        self.claude_home("one", "same@x.example")
        self.claude_home("two", "same@x.example")
        names = sorted(r["name"] for r in self.p.accounts())
        self.assertEqual(names, ["same@x.example", "same@x.example#two"])

    def test_tier_branches(self):
        cases = ((dict(rl="max_20x"), "Max 20x"), (dict(org="claude_team"), "Team"),
                 (dict(org="claude_pro"), "Pro"), (dict(org="claude_enterprise"), "Enterprise"),
                 (dict(org="claude_max"), "Max"), (dict(), None))
        for i, (kw, want) in enumerate(cases):
            home = self.claude_home("t%d" % i, "t%d@x.example" % i, **kw)
            self.assertEqual(self.p._anthropic_identity(home)[1], want, kw)


class ProbeTest(NativeBase):
    def probe(self, acct, response=None, error=None):
        fn = mock.Mock(side_effect=error) if error else mock.Mock(return_value=response)
        with mock.patch.object(self.p, "_get_json", fn):
            return self.p._probe_one(acct), fn

    def acct(self, home, provider="anthropic", name="u@x.example"):
        return {"name": name, "provider": provider, "home": home, "tier": None}

    def http_error(self, code):
        return urllib.error.HTTPError("http://x", code, "boom", None, io.BytesIO(b""))

    def _set_expiry(self, home, expires_at_ms):
        p = os.path.join(home, ".credentials.json")
        creds = json.load(open(p))
        creds["claudeAiOauth"]["expiresAt"] = expires_at_ms
        json.dump(creds, open(p, "w"))

    def test_expired_token_is_its_own_state_not_api_error(self):  # noqa: VACUOUS_ASSERTION — three positive assertions (state==expired-token, status contains reauth-needed + 15d); the fn.assert_not_called() absence has its unconditional positive control in the sibling test_live_token_still_probes_the_network (fn.assert_called_once)
        """task/381: a present-but-EXPIRED token is its own state and never
        touches the network — the four Max 20x rows the owner saw as
        'api-error' were tokens dead for 13-17 days."""
        import time as _t
        home = self.claude_home("stale", "u@x.example")
        self._set_expiry(home, int((_t.time() - 15 * 86400) * 1000))
        (cred, _hist), fn = self.probe(self.acct(home), {"limits": []})
        self.assertEqual(cred["cred_state"], "expired-token")
        self.assertIn("reauth-needed", cred["status"])
        self.assertIn("15d", cred["status"])
        fn.assert_not_called()          # a dead token never pays for a 401

    def _set_oauth(self, home, **fields):
        path = os.path.join(home, ".credentials.json")
        with open(path) as fh:
            creds = json.load(fh)
        creds["claudeAiOauth"].update(fields)
        with open(path, "w") as fh:
            json.dump(creds, fh)

    def _expired_home(self, name, email, **chain):
        """A home whose ACCESS token died an hour ago, plus whatever refresh
        chain the caller wants around it. Every token value is invented."""
        import time as _t
        home = self.claude_home(name, email)
        self._set_oauth(home, expiresAt=int((_t.time() - 3600) * 1000), **chain)
        return home

    def test_a_due_token_is_not_a_dead_account(self):  # noqa: VACUOUS_ASSERTION — four positive assertions (state==due-refresh, status carries keepalive, the apply command and the chain name); the fn.assert_not_called() absence has its unconditional positive control in test_live_token_still_probes_the_network (fn.assert_called_once on the same probe seam)
        """task/2749: an expired access token whose REFRESH chain is still
        grantable is `due-refresh` — helm's own keepalive fixes it and the
        owner owes nothing. The sentence must name that verb and must never
        say reauth, which is what sent him to six logins for six healthy
        accounts."""
        import time as _t
        home = self._expired_home(
            "due", "u@x.example", refreshToken="FAKE-refresh",
            refreshTokenExpiresAt=int((_t.time() + 30 * 86400) * 1000))
        (cred, _hist), fn = self.probe(self.acct(home), {"limits": []})
        self.assertEqual(cred["cred_state"], "due-refresh")
        self.assertIn("keepalive", cred["status"])
        self.assertIn("helm keepalive --apply", cred["status"])
        self.assertNotIn("reauth", cred["status"])
        self.assertIn(providers.CHAIN_LIVE, cred["status"])
        fn.assert_not_called()          # still a dead access token: no 401 paid

    def test_a_spent_or_absent_chain_stays_expired_token(self):
        """The two must-hit controls on the same observable: helm cannot grant
        on either of these, so the owner really does owe a login and the word
        reauth is the right one."""
        import time as _t
        past = self._expired_home(
            "spent", "v@x.example", refreshToken="FAKE-refresh",
            refreshTokenExpiresAt=int((_t.time() - 86400) * 1000))
        (cred, _h), _fn = self.probe(self.acct(past, name="v@x.example"), {"limits": []})
        self.assertEqual(cred["cred_state"], "expired-token")
        self.assertIn("reauth-needed", cred["status"])
        self.assertIn(providers.CHAIN_EXPIRED, cred["status"])
        none = self._expired_home("norefresh", "w@x.example")
        (cred2, _h2), _fn2 = self.probe(self.acct(none, name="w@x.example"), {"limits": []})
        self.assertEqual(cred2["cred_state"], "expired-token")
        self.assertIn(providers.CHAIN_ABSENT, cred2["status"])

    def test_an_absent_refresh_lifetime_is_refreshable_not_spent(self):
        """The deliberate reading of a field claude does not always write: a
        refresh token with NO refreshTokenExpiresAt is `due-refresh`, and the
        sentence says the lifetime is unproven rather than pretending it is
        live. Guessing spent here is what would leave the measured homes
        telling the owner to log in."""
        home = self._expired_home("unproven", "y@x.example", refreshToken="FAKE-refresh")
        (cred, _h), _fn = self.probe(self.acct(home, name="y@x.example"), {"limits": []})
        self.assertEqual(cred["cred_state"], "due-refresh")
        self.assertIn(providers.CHAIN_UNPROVEN, cred["status"])

    def test_the_due_sentence_carries_the_cadences_last_run(self):
        """The status answers 'is the loop turning?', not only 'what is
        wrong?' — a due-refresh account with no recorded grant and one with a
        grant by the cron read differently."""
        import time as _t
        home = self._expired_home(
            "cad", "z@x.example", refreshToken="FAKE-refresh",
            refreshTokenExpiresAt=int((_t.time() + 30 * 86400) * 1000))
        (bare, _h), _fn = self.probe(self.acct(home, name="z@x.example"), {"limits": []})
        self.assertIn("no keepalive refresh recorded", bare["status"])
        self.assertIn("--ensure-timer", bare["status"])
        cache = os.environ["HELM_CACHE_DIR"]
        os.makedirs(cache, exist_ok=True)
        with open(os.path.join(cache, "keepalive-log.jsonl"), "w") as f:
            f.write(json.dumps({"ts": "1999-01-01T00:00:00+0000", "by": "hand",
                                "home": "cad", "action": "skip"}) + "\n")
            f.write(json.dumps({"ts": "1999-01-02T00:00:00+0000", "by": "keepalive-cron",
                                "home": "cad", "action": "refreshed"}) + "\n")
        (seen, _h2), _fn2 = self.probe(self.acct(home, name="z@x.example"), {"limits": []})
        self.assertIn("1999-01-02T00:00:00+0000", seen["status"])
        self.assertIn("keepalive-cron", seen["status"])

    def test_the_sentence_never_contradicts_keepalives_last_decision(self):
        """Both poles on one home whose FILE reads refresh-live. Newest
        keepalive row needs_reauth -> the owner is told to log in (the chain is
        spent whatever the file says; on trunk this home read reauth-needed and
        that was right). Newest row refreshed -> due, keepalive named. A third
        pole: newest row a skip because Orca holds the chain -> due, and the
        cure named is the Orca sync, not a keepalive pass that will never act."""
        import time as _t
        home = self._expired_home(
            "agree", "q@x.example", refreshToken="FAKE-refresh",
            refreshTokenExpiresAt=int((_t.time() + 30 * 86400) * 1000))
        cache = os.environ["HELM_CACHE_DIR"]
        os.makedirs(cache, exist_ok=True)
        log = os.path.join(cache, "keepalive-log.jsonl")
        def newest(action, reason=""):
            with open(log, "w") as f:
                f.write(json.dumps({"ts": "1999-01-01T00:00:00+0000",
                                    "by": "keepalive-cron", "home": "agree",
                                    "action": "refreshed"}) + "\n")
                f.write(json.dumps({"ts": "1999-01-02T00:00:00+0000",
                                    "by": "keepalive-cron", "home": "agree",
                                    "action": action, "reason": reason}) + "\n")
            (cred, _h), _fn = self.probe(self.acct(home, name="q@x.example"),
                                         {"limits": []})
            return cred
        gave_up = newest("needs_reauth", "refresh HTTP 400 — one-time re-login needed")
        self.assertEqual(gave_up["cred_state"], "expired-token")
        self.assertIn("reauth-needed", gave_up["status"])
        self.assertIn("needs_reauth", gave_up["status"])
        self.assertNotIn("no login needed", gave_up["status"])
        # CONTROL: the same file with the newest row a success is due
        fine = newest("refreshed")
        self.assertEqual(fine["cred_state"], "due-refresh")
        self.assertIn("helm keepalive --apply", fine["status"])
        self.assertNotIn("reauth", fine["status"])
        # the Orca-held skip names the Orca sync as the cure
        held = newest("skip", "home is stale against Orca and its chain is "
                              "unproven: Orca holds the chain")
        self.assertEqual(held["cred_state"], "due-refresh")
        self.assertIn("helm cred sync-orca", held["status"])
        self.assertNotIn("helm keepalive --apply", held["status"])
        # a dry-run row decides nothing and never overrides the row before it
        with open(log, "a") as f:
            f.write(json.dumps({"ts": "1999-01-03T00:00:00+0000", "by": "hand",
                                "home": "agree", "action": "would-refresh"}) + "\n")
        (again, _h), _fn = self.probe(self.acct(home, name="q@x.example"), {"limits": []})
        self.assertIn("helm cred sync-orca", again["status"])

    def test_refresh_chain_names_presence_and_lifetime(self):
        now = 1_000_000.0
        ms = lambda s: int((now + s) * 1000)
        self.assertEqual(providers.refresh_chain({}, now), providers.CHAIN_ABSENT)
        self.assertEqual(providers.refresh_chain({"refreshToken": "t"}, now),
                         providers.CHAIN_UNPROVEN)
        self.assertEqual(providers.refresh_chain(
            {"refreshToken": "t", "refreshTokenExpiresAt": ms(60)}, now),
            providers.CHAIN_LIVE)
        self.assertEqual(providers.refresh_chain(
            {"refreshToken": "t", "refreshTokenExpiresAt": ms(-60)}, now),
            providers.CHAIN_EXPIRED)
        # a bool is not a lifetime (isinstance(True, int) is the trap)
        self.assertEqual(providers.refresh_chain(
            {"refreshToken": "t", "refreshTokenExpiresAt": True}, now),
            providers.CHAIN_UNPROVEN)

    def test_due_refresh_is_in_the_producers_vocabulary(self):
        """The width of every state cell derives from this tuple, so a state
        minted but not declared renders through a path nothing sized."""
        self.assertIn("due-refresh", providers.CRED_STATES)
        self.assertIn("expired-token", providers.CRED_STATES)

    def test_live_token_still_probes_the_network(self):
        """The polarity control: a token whose expiry is in the FUTURE (or
        absent) must reach the live call, so the stale check cannot swallow a
        healthy account into a false expired-token."""
        import time as _t
        home = self.claude_home("live", "u@x.example")
        self._set_expiry(home, int((_t.time() + 3600) * 1000))
        data = {"limits": [{"kind": "session", "percent": 10,
                            "resets_at": "1970-01-01T00:01:00Z"}]}
        (cred, _), fn = self.probe(self.acct(home), data)
        self.assertEqual(cred["cred_state"], "ok")
        fn.assert_called_once()
        # and expiry ABSENT (the fixture default) also reaches the network
        home2 = self.claude_home("noexp", "v@x.example")
        (cred2, _), fn2 = self.probe(self.acct(home2), data)
        self.assertEqual(cred2["cred_state"], "ok")
        fn2.assert_called_once()

    def test_anthropic_ok_and_exhausted(self):
        home = self.claude_home("a", "u@x.example")
        data = {"limits": [{"kind": "session", "percent": 30,
                            "resets_at": "1970-01-01T00:01:00Z"}]}
        (cred, hist), fn = self.probe(self.acct(home), data)
        self.assertEqual((cred["cred_state"], cred["status"]), ("ok", "allowed"))
        self.assertEqual(cred["headroom_pct"], 70.0)
        self.assertEqual(cred["resets_at_ms"], 60_000)
        self.assertEqual(hist["primary"], "5h")
        self.assertEqual(fn.call_args[0][0], providers.ANTHROPIC_USAGE_URL)
        self.assertEqual(fn.call_args[0][1]["Authorization"], "Bearer FAKE-access")
        data["limits"][0]["percent"] = 100
        (cred, _), _ = self.probe(self.acct(home), data)
        self.assertEqual((cred["cred_state"], cred["status"]), ("exhausted", "blocked"))

    def test_exhausted_and_headroom_are_read_only_off_measured_gauges(self):  # noqa: VACUOUS_ASSERTION — the headroom absence has the measured 60.0 control first on the same probe, and the unread status is positively asserted equal on the history row
        """task/2935: a limit the vendor sent with no percent is an UNREAD
        gauge. It is never 0% (headroom), never 100% (exhausted); only the
        measured gauges decide exhausted, the headroom of an unread binding
        gauge is unknown, and a reading with no measured plan gauge at all is
        UNKNOWN rather than ok."""
        home = self.claude_home("partial", "u@x.example")
        reset = "1970-01-01T00:01:00Z"
        # CONTROL on the headroom observable: a measured session has one
        (cred, _), _ = self.probe(self.acct(home), {"limits": [
            {"kind": "session", "percent": 40, "resets_at": reset},
            {"kind": "weekly_all", "percent": 40, "resets_at": None}]})
        self.assertEqual((cred["cred_state"], cred["headroom_pct"]), ("ok", 60.0))
        # an unread session beside a measured weekly: read, but no headroom
        (cred, hist), _ = self.probe(self.acct(home), {"limits": [
            {"kind": "session", "percent": None, "resets_at": reset},
            {"kind": "weekly_all", "percent": 40, "resets_at": None}]})
        self.assertEqual((cred["cred_state"], cred["status"]), ("ok", "allowed"))
        self.assertIsNone(cred["headroom_pct"])
        self.assertEqual([g["utilization"] for g in hist["gauges"]], [None, 0.4])
        # a measured wall beside an unread window is still a wall
        (cred, _), _ = self.probe(self.acct(home), {"limits": [
            {"kind": "session", "percent": None, "resets_at": reset},
            {"kind": "weekly_all", "percent": 100, "resets_at": None}]})
        self.assertEqual((cred["cred_state"], cred["status"]),
                         ("exhausted", "blocked"))
        # nothing measured: UNKNOWN, and the status is no reading
        (cred, hist), _ = self.probe(self.acct(home), {"limits": [
            {"kind": "session", "percent": None, "resets_at": reset}]})
        self.assertEqual(cred["cred_state"], "unknown")
        self.assertIsNone(cred["headroom_pct"])
        self.assertFalse(cred["status"].startswith(("allowed", "blocked")),
                         cred["status"])
        self.assertEqual(hist["status"], cred["status"])

    def test_anthropic_no_credentials_and_http_errors(self):
        home = self.claude_home("bare2", authed=False)
        (cred, _), _ = self.probe(self.acct(home))
        self.assertEqual((cred["cred_state"], cred["status"]),
                         ("api-error", "no-credentials"))
        home = self.claude_home("a2", "u@x.example")
        (cred, _), _ = self.probe(self.acct(home), error=self.http_error(401))
        self.assertEqual((cred["cred_state"], cred["status"]),
                         ("api-error", "needs_reauth"))
        (cred, _), _ = self.probe(self.acct(home), error=self.http_error(503))
        self.assertEqual(cred["status"], "http_503")
        (cred, _), _ = self.probe(self.acct(home), error=OSError("down"))
        self.assertEqual(cred["status"], "network-error")

    def test_codex_headers_gauges_tier_and_degrades_to_unknown(self):
        home = self.codex_home("cx2", "c@x.example")
        data = {"rate_limit": {"primary_window": {
                    "limit_window_seconds": 18000, "used_percent": 25, "reset_at": 5}},
                "plan_type": "pro"}
        (cred, _), fn = self.probe(self.acct(home, "codex", "cx2"), data)
        self.assertEqual((cred["cred_state"], cred["tier"]), ("ok", "Pro"))
        headers = fn.call_args[0][1]
        self.assertEqual(headers["chatgpt-account-id"], "acct-1")
        # stale token -> unknown with the self-refresh note, NEVER a failure
        (cred, _), _ = self.probe(self.acct(home, "codex", "cx2"),
                                  error=self.http_error(401))
        self.assertEqual((cred["cred_state"], cred["status"]),
                         ("unknown", "needs_reauth"))
        self.assertIn("codex refreshes", cred["note"])
        # limit_reached -> exhausted
        (cred, _), _ = self.probe(self.acct(home, "codex", "cx2"),
                                  {"rate_limit": {"limit_reached": True}})
        self.assertEqual(cred["cred_state"], "exhausted")

    def test_an_empty_copy_orca_can_refill_is_a_sync_due_not_an_api_error(self):  # noqa: VACUOUS_ASSERTION — the vendor-call positive control is test_live_token_still_probes_the_network
        """A blanked stub while Orca holds the account names the sync, and no
        vendor call is made. Without an Orca copy it stays no-credentials."""
        from helm import cred, homes
        roots = mock.patch.dict(homes.ROOTS,
                                {"claude": os.path.join(self.tmp, ".claude-homes")})  # noqa: SEAT_NAME — homes.ROOTS provider key, not a seat
        roots.start()
        self.addCleanup(roots.stop)
        cred.cache_clear()
        self.addCleanup(cred.cache_clear)
        home = self.claude_home("cpo-x-example", "cpo@x.example")
        self.blank_stub(home)
        (row, _), fn = self.probe(self.acct(home, name="cpo@x.example"))
        self.assertEqual((row["cred_state"], row["status"]),
                         ("api-error", "no-credentials"))   # no Orca copy: unchanged
        auth = os.path.join(self.tmp, "orca", "claude-accounts", "U1", "auth")
        os.makedirs(auth)
        with open(os.path.join(auth, "oauth-account.json"), "w") as f:
            json.dump({"emailAddress": "cpo@x.example"}, f)
        with open(os.path.join(auth, ".orca-managed-claude-auth"), "w") as f:
            f.write("U1")
        with open(os.path.join(auth, ".credentials.json"), "w") as f:
            json.dump({"claudeAiOauth": {
                "accessToken": "FAKE-ORCA", "refreshToken": "FAKE-ORCA-R",
                "expiresAt": int((time.time() - 3600) * 1000),
                "refreshTokenExpiresAt": int((time.time() + 86400) * 1000)}}, f)
        cred.cache_clear()
        (row, _), fn = self.probe(self.acct(home, name="cpo@x.example"))
        self.assertEqual(row["cred_state"], "due-refresh")
        self.assertIn("helm cred sync-orca --home cpo-x-example --apply", row["status"])
        self.assertNotIn("reauth", row["status"])
        fn.assert_not_called()

    def test_transient_classifier(self):
        t = NativeQuotaProvider._transient
        self.assertTrue(t("network-error"))
        self.assertTrue(t("http_429"))
        self.assertTrue(t("http_500"))
        self.assertFalse(t("http_403"))
        self.assertFalse(t("needs_reauth"))
        self.assertFalse(t("allowed"))


class CredStateTest(NativeBase):
    def test_one_probe_per_identity_rows_per_home(self):
        self.claude_home("one", "same@x.example")
        self.claude_home("two", "same@x.example")
        gauges = [{"label": "5h", "kind": "session", "utilization": 0.2, "reset": None}]
        probe = mock.Mock(return_value=(
            {"account": "same@x.example", "provider": "anthropic", "cred_state": "ok",
             "headroom_pct": 80.0, "status": "allowed", "resets_at_ms": None,
             "tier": None, "home": "h", "source_at": None},
            {"provider": "anthropic", "account": "same@x.example",
             "probed_at": providers._iso_z(), "status": "allowed", "primary": "5h",
             "gauges": gauges, "source_at": None}))
        with mock.patch.object(self.p, "_probe_one", probe), \
                mock.patch.object(providers.time, "sleep"):
            rows = self.p.cred_state()
        self.assertEqual(probe.call_count, 1)      # ONE probe for the identity
        self.assertEqual(len(rows), 2)             # a row for EACH home
        self.assertEqual({r["account"] for r in rows},
                         {"same@x.example", "same@x.example#two"})
        with open(self.hist) as f:
            self.assertEqual(len(f.readlines()), 1)  # one history point, not two
        # TTL cache: a second call returns copies without re-probing
        with mock.patch.object(self.p, "_probe_one", probe):
            rows2 = self.p.cred_state()
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(len(rows2), 2)

    def test_a_null_gauge_account_reads_unread_and_its_sibling_survives(self):
        """task/2935, the task/2480 R5 class: `_probe_one` promises it never
        raises, and `cred_state` collects every probe through
        `f.result()`. A limit with no percent became a None utilization, the
        exhausted test compared it against a number, and the TypeError
        re-raised at `f.result()`, which lost EVERY sibling's row. Driven
        through the real entry point: two identities, one vendor body with
        no percent at all, one with a real one."""
        for name, token in (("null", "FAKE-null"), ("live", "FAKE-live")):
            home = self.claude_home(name, "%s@x.example" % name)
            with open(os.path.join(home, ".credentials.json"), "w") as f:
                json.dump({"claudeAiOauth": {"accessToken": token}}, f)
        reset = "1970-01-01T00:01:00Z"
        bodies = {"Bearer FAKE-null": {"limits": [
                      {"kind": "session", "percent": None, "resets_at": reset},
                      {"kind": "weekly_all", "percent": None, "resets_at": None}]},
                  "Bearer FAKE-live": {"limits": [
                      {"kind": "session", "percent": 30, "resets_at": reset}]}}
        fn = mock.Mock(side_effect=lambda _url, headers:
                       bodies[headers["Authorization"]])
        with mock.patch.object(self.p, "_get_json", fn), \
                mock.patch.object(providers.time, "sleep"):
            rows = {r["account"]: r for r in self.p.cred_state()}
        self.assertEqual(fn.call_count, 2)
        self.assertEqual(
            (rows["live@x.example"]["cred_state"], rows["live@x.example"]["status"],
             rows["live@x.example"]["headroom_pct"]), ("ok", "allowed", 70.0))
        self.assertEqual(rows["null@x.example"]["cred_state"], "unknown")
        self.assertIsNone(rows["null@x.example"]["headroom_pct"])
        # the history row is the one production writes, gauges left unread
        with open(self.hist) as f:
            hist = {r["account"]: r for r in map(json.loads, f)}
        self.assertEqual([g["utilization"] for g in hist["null@x.example"]["gauges"]],
                         [None, None])
        # and a later cycle that serves that observation through the
        # throttle door reads it the same way instead of raising
        with mock.patch.object(self.p, "_get_json",
                               side_effect=self.http_error_429()), \
                mock.patch.object(providers.time, "sleep"):
            self.p._cred_cache = None
            again = {r["account"]: r for r in self.p.cred_state()}
        self.assertEqual(again["null@x.example"]["cred_state"], "unknown")
        self.assertIsNone(again["null@x.example"]["headroom_pct"])
        self.assertEqual((again["live@x.example"]["cred_state"],
                          again["live@x.example"]["headroom_pct"]), ("ok", 70.0))

    @staticmethod
    def http_error_429():
        return urllib.error.HTTPError("http://x", 429, "slow", None,
                                      io.BytesIO(b""))

    def _stub_and_live(self):
        """Two homes, one account: the first sorts to a blanked stub, the
        second holds a live token."""
        self.blank_stub(self.claude_home("one", "same@x.example"))
        self.claude_home("two", "same@x.example")
        data = {"limits": [{"kind": "session", "percent": 20,
                            "resets_at": "1970-01-01T00:01:00Z"},
                           {"kind": "weekly_all", "percent": 35,
                            "resets_at": None}]}
        return mock.Mock(return_value=data)

    def test_an_identity_is_read_through_its_live_copy_not_its_first_home(self):
        """The identity reads through the live copy, and that reading is
        the live home's row. The stub's row keeps its OWN state: a shared
        reading never vouches for a home whose own file holds no token."""
        fn = self._stub_and_live()
        with mock.patch.object(self.p, "_get_json", fn), \
                mock.patch.object(providers.time, "sleep"):
            rows = {r["account"]: r for r in self.p.cred_state()}
        fn.assert_called_once()
        self.assertEqual(fn.call_args[0][1]["Authorization"], "Bearer FAKE-access")
        self.assertEqual(
            {a: (r["cred_state"], r["status"], r["headroom_pct"])
             for a, r in rows.items()},
            {"same@x.example": ("api-error", "no-credentials", None),
             "same@x.example#two": ("ok", "allowed", 80.0)})
        self.assertIn("holds no access token", rows["same@x.example"]["note"])
        self.assertIn("reads ok through two", rows["same@x.example"]["note"])
        self.assertNotIn("note", rows["same@x.example#two"])
        with open(self.hist) as f:  # history keeps the identity's one key
            hist = [json.loads(l) for l in f]
        self.assertEqual([(h["account"], h["status"]) for h in hist],
                         [("same@x.example", "allowed")])
        # THE IDENTITY COUNTS AS READ for the burn flag: coverage is one row
        # per history key (one per identity), fed by the live copy's reading.
        from helm import burnflags
        money, _ = burnflags.anthropic_money_rows(
            burnflags.usage_history(self.hist))
        self.assertEqual([(r["state"], r["longest_pct"]) for r in money],
                         [("ok", 35.0)])

    def test_allocate_never_places_a_seat_on_the_empty_sibling(self):
        """The stub home is ineligible under its own reason; the live
        sibling of the same account is eligible and ranks first."""
        fn = self._stub_and_live()
        with mock.patch.object(self.p, "_get_json", fn), \
                mock.patch.object(providers.time, "sleep"):
            ranked = self.p.allocate("claude-fable-5")
        by = {r["account"]: r for r in ranked}
        self.assertEqual((by["same@x.example"]["eligible"], by["same@x.example"]["blocked_by"]),
                         (False, ["no-credentials"]))
        self.assertEqual((by["same@x.example#two"]["eligible"], by["same@x.example#two"]["blocked_by"]),
                         (True, []))
        self.assertEqual(ranked[0]["account"], "same@x.example#two")

    def test_an_empty_sibling_orca_can_refill_reads_its_own_sync_due(self):
        """The stub's own state is whatever its own copy says: with Orca
        holding the account, that is due-refresh naming the sync for THIS
        home, and allocate() blocks it under that reason."""
        from helm import cred, homes
        roots = mock.patch.dict(homes.ROOTS,
                                {"claude": os.path.join(self.tmp, ".claude-homes")})  # noqa: SEAT_NAME — homes.ROOTS provider key, not a seat
        roots.start()
        self.addCleanup(roots.stop)
        cred.cache_clear()
        self.addCleanup(cred.cache_clear)
        fn = self._stub_and_live()
        auth = os.path.join(self.tmp, "orca", "claude-accounts", "U1", "auth")
        os.makedirs(auth)
        with open(os.path.join(auth, "oauth-account.json"), "w") as f:
            json.dump({"emailAddress": "same@x.example"}, f)
        with open(os.path.join(auth, ".orca-managed-claude-auth"), "w") as f:
            f.write("U1")
        with open(os.path.join(auth, ".credentials.json"), "w") as f:
            json.dump({"claudeAiOauth": {
                "accessToken": "FAKE-ORCA", "refreshToken": "FAKE-ORCA-R",
                "expiresAt": int((time.time() - 3600) * 1000),
                "refreshTokenExpiresAt": int((time.time() + 86400) * 1000)}}, f)
        with mock.patch.object(self.p, "_get_json", fn), \
                mock.patch.object(providers.time, "sleep"):
            ranked = {r["account"]: r for r in self.p.allocate("claude-fable-5")}
            rows = {r["account"]: r for r in self.p.cred_state()}
        fn.assert_called_once()
        self.assertEqual(rows["same@x.example"]["cred_state"], "due-refresh")
        self.assertIn("helm cred sync-orca --home one --apply",
                      rows["same@x.example"]["status"])
        self.assertEqual(rows["same@x.example#two"]["cred_state"], "ok")
        self.assertEqual((ranked["same@x.example"]["eligible"], ranked["same@x.example"]["blocked_by"]),
                         (False, ["due-refresh"]))
        self.assertTrue(ranked["same@x.example#two"]["eligible"])

    def oauth_copy(self, home, access, expires_in_s, refresh=None):
        """A credentials file holding `access`, expiring `expires_in_s`
        from now (negative = already expired), with an optional live
        refresh chain."""
        oauth = {"accessToken": access,
                 "expiresAt": int((time.time() + expires_in_s) * 1000)}
        if refresh:
            oauth.update(refreshToken=refresh,
                         refreshTokenExpiresAt=int((time.time() + 30 * 86400) * 1000))
        with open(os.path.join(home, ".credentials.json"), "w") as f:
            json.dump({"claudeAiOauth": oauth}, f)

    def _read_group(self):
        """cred_state and allocate over one identity -> ({account: row},
        {account: allocation}), with the one vendor call asserted."""
        fn = mock.Mock(return_value={"limits": [
            {"kind": "session", "percent": 20, "resets_at": "1970-01-01T00:01:00Z"}]})
        with mock.patch.object(self.p, "_get_json", fn), \
                mock.patch.object(providers.time, "sleep"):
            ranked = {r["account"]: r for r in self.p.allocate("claude-fable-5")}
            rows = {r["account"]: r for r in self.p.cred_state()}
        fn.assert_called_once()
        self.assertEqual(fn.call_args[0][1]["Authorization"], "Bearer FAKE-live")
        return rows, ranked

    def test_an_expired_sibling_keeps_its_own_state_and_is_never_seated(self):
        """A shared reading never vouches for an EXPIRED copy either: its
        token still 401s. Whichever home sorts first, the expired one reads
        its own expired-token (chain spent) or due-refresh (chain live), and
        allocate() blocks it; the live sibling reads ok and is eligible."""
        for dead_first in (True, False):
            for refresh, state in ((None, "expired-token"),
                                   ("FAKE-R", "due-refresh")):
                with self.subTest(dead_first=dead_first, state=state):
                    shutil.rmtree(os.path.join(self.tmp, ".claude-homes"),
                                  ignore_errors=True)
                    self.p = NativeQuotaProvider(history_path=self.hist)
                    self.p._active_homes = lambda: set()
                    dead, live = ("one", "two") if dead_first else ("two", "one")
                    self.oauth_copy(self.claude_home(dead, "same@x.example"),
                                    "FAKE-dead", -(10 * 86400 if not refresh else 3600),
                                    refresh)
                    self.oauth_copy(self.claude_home(live, "same@x.example"),
                                    "FAKE-live", 6 * 3600, "FAKE-LR")
                    rows, ranked = self._read_group()
                    name = {"one": "same@x.example", "two": "same@x.example#two"}
                    d, l = name[dead], name[live]
                    self.assertEqual(rows[d]["cred_state"], state)
                    self.assertIsNone(rows[d]["headroom_pct"])
                    self.assertIn("own copy has expired", rows[d]["note"])
                    self.assertIn("reads ok through %s" % live, rows[d]["note"])
                    self.assertEqual((rows[l]["cred_state"], rows[l]["headroom_pct"]),
                                     ("ok", 80.0))
                    self.assertFalse(ranked[d]["eligible"])
                    self.assertTrue(ranked[d]["blocked_by"])
                    self.assertTrue(ranked[l]["eligible"])

    def test_an_empty_sibling_is_still_blocked_beside_a_dated_live_copy(self):
        """The widened check keeps the empty-copy case: with a live copy
        that carries an expiresAt, the stub reads no-credentials and is
        ineligible."""
        self.blank_stub(self.claude_home("one", "same@x.example"))
        self.oauth_copy(self.claude_home("two", "same@x.example"),
                        "FAKE-live", 6 * 3600, "FAKE-LR")
        rows, ranked = self._read_group()
        self.assertEqual((rows["same@x.example"]["cred_state"], rows["same@x.example"]["status"]),
                         ("api-error", "no-credentials"))
        self.assertIn("holds no access token", rows["same@x.example"]["note"])
        self.assertEqual((ranked["same@x.example"]["eligible"], ranked["same@x.example"]["blocked_by"]),
                         (False, ["no-credentials"]))
        self.assertTrue(ranked["same@x.example#two"]["eligible"])

    def test_a_live_sibling_still_takes_the_shared_reading(self):
        """Two live copies of one account: one probe (through the later
        expiry), and both homes carry its reading with no note."""
        self.oauth_copy(self.claude_home("one", "same@x.example"),
                        "FAKE-other", 2 * 3600, "FAKE-R1")
        self.oauth_copy(self.claude_home("two", "same@x.example"),
                        "FAKE-live", 6 * 3600, "FAKE-R2")
        rows, ranked = self._read_group()
        for a in ("same@x.example", "same@x.example#two"):
            self.assertEqual((rows[a]["cred_state"], rows[a]["headroom_pct"]), ("ok", 80.0))
            self.assertNotIn("note", rows[a])
            self.assertTrue(ranked[a]["eligible"])

    def test_best_copy_ranks_an_undated_token_above_an_expired_one(self):
        """A present token with no expiresAt is not known dead; an expired
        one is. The live-looking copy is the one read."""
        one = self.claude_home("one", "same@x.example")      # FAKE-access, no expiresAt
        two = self.claude_home("two", "same@x.example")
        self.oauth_copy(two, "FAKE-dead", -3600, "FAKE-R")
        m = [{"provider": "anthropic", "home": two}, {"provider": "anthropic", "home": one}]
        self.assertEqual(NativeQuotaProvider._best_copy(m)["home"], one)
        self.oauth_copy(one, "FAKE-live", 3600)
        self.assertEqual(NativeQuotaProvider._best_copy(m)["home"], one)

    def test_a_bool_expiresAt_reads_one_way_in_the_group_and_alone(self):
        """`expiresAt: true` is not a timestamp. The member loop counted
        that copy live and gave it the shared ok, while its own probe read
        True as 1 ms past the epoch and said expired-token. Both now read
        it as undated: the group reads through the dated live copy in one
        call, and the home probed alone lets the vendor call decide."""
        odd = self.claude_home("one", "same@x.example")
        with open(os.path.join(odd, ".credentials.json"), "w") as f:
            json.dump({"claudeAiOauth": {"accessToken": "FAKE-odd",
                                         "expiresAt": True}}, f)
        self.oauth_copy(self.claude_home("two", "same@x.example"),
                        "FAKE-live", 6 * 3600, "FAKE-LR")
        rows, ranked = self._read_group()
        self.assertEqual((rows["same@x.example"]["cred_state"],
                          rows["same@x.example"]["headroom_pct"]), ("ok", 80.0))
        self.assertTrue(ranked["same@x.example"]["eligible"])
        fn = mock.Mock(return_value={"limits": [
            {"kind": "session", "percent": 20, "resets_at": "1970-01-01T00:01:00Z"}]})
        with mock.patch.object(self.p, "_get_json", fn):
            alone, _ = self.p._probe_one(
                {"provider": "anthropic", "home": odd, "name": "same@x.example"})
        self.assertEqual(alone["cred_state"], rows["same@x.example"]["cred_state"])
        fn.assert_called_once()
        self.assertEqual(fn.call_args[0][1]["Authorization"], "Bearer FAKE-odd")

    def test_an_all_expired_group_claims_no_live_read_through(self):  # noqa: VACUOUS_ASSERTION — the loop runs over a literal two-tuple, and its positive control on the same note is the assertIn of "no copy of this account is live" and the freshest home's name, so the absence of "through" is about a note that really exists
        """Every copy expired: no vendor call, every home reads its own
        state and is blocked, and the sibling's note says no copy is live
        instead of "the account itself reads X through" a dead copy."""
        for fresh_first in (True, False):
            with self.subTest(fresh_first=fresh_first):
                shutil.rmtree(os.path.join(self.tmp, ".claude-homes"),
                              ignore_errors=True)
                self.p = NativeQuotaProvider(history_path=self.hist)
                self.p._active_homes = lambda: set()
                fresh, stale = ("one", "two") if fresh_first else ("two", "one")
                self.oauth_copy(self.claude_home(fresh, "same@x.example"),
                                "FAKE-fresh", -3600, "FAKE-R")
                self.oauth_copy(self.claude_home(stale, "same@x.example"),
                                "FAKE-stale", -10 * 86400)
                fn = mock.Mock()
                with mock.patch.object(self.p, "_get_json", fn), \
                        mock.patch.object(providers.time, "sleep"):
                    ranked = {r["account"]: r for r in self.p.allocate("claude-fable-5")}
                    rows = {r["account"]: r for r in self.p.cred_state()}
                fn.assert_not_called()
                name = {"one": "same@x.example", "two": "same@x.example#two"}
                f, s = name[fresh], name[stale]
                self.assertEqual((rows[f]["cred_state"], rows[s]["cred_state"]),
                                 ("due-refresh", "expired-token"))
                self.assertNotIn("through", rows[s]["note"])
                self.assertIn("own copy has expired, and no copy of this "
                              "account is live", rows[s]["note"])
                self.assertIn("the freshest, %s, reads due-refresh" % fresh,
                              rows[s]["note"])
                self.assertFalse(ranked[f]["eligible"])
                self.assertFalse(ranked[s]["eligible"])

    def test_throttled_probe_serves_last_observation_marked_stale(self):
        self.claude_home("solo", "solo@x.example")
        seen_at = providers._iso_z(time.time() - 60)
        with open(self.hist, "w") as f:
            f.write(json.dumps({"account": "solo@x.example", "provider": "anthropic",
                                "probed_at": seen_at, "gauges": [
                                    {"label": "5h", "kind": "session",
                                     "utilization": 0.25, "reset": 99}]}) + "\n")
        probe = mock.Mock(return_value=(
            {"account": "solo@x.example", "provider": "anthropic", "cred_state": "api-error",
             "headroom_pct": None, "status": "http_429", "resets_at_ms": None,
             "tier": None, "home": "h", "source_at": None},
            {"provider": "anthropic", "account": "solo@x.example",
             "probed_at": providers._iso_z(), "status": "http_429", "primary": None,
             "gauges": [], "source_at": None}))
        with mock.patch.object(self.p, "_probe_one", probe), \
                mock.patch.object(providers.time, "sleep"):
            rows = self.p.cred_state()
        self.assertEqual(rows[0]["cred_state"], "ok")  # throttle != dead cred
        self.assertEqual(rows[0]["headroom_pct"], 75.0)
        self.assertEqual(rows[0]["source_at"], seen_at)
        self.assertIn("http_429", rows[0]["note"])
        with open(self.hist) as f:  # no fake history point was appended
            self.assertEqual(len(f.readlines()), 1)


class HistoryTest(NativeBase):
    def test_cutoff_filter_and_cold_start_seeding(self):
        row = {"provider": "anthropic", "account": "a", "probed_at": providers._iso_z(),
               "status": "allowed", "gauges": [{"label": "5h"}]}
        old = dict(row, probed_at="2000-01-01T00:00:00Z")
        self.p.cred_state = lambda: self.p._append_history([row, old])
        rows = self.p.history(1)  # empty file -> one cred_state cycle seeds it
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["probed_at"], row["probed_at"])


class WindowsModelTest(NativeBase):
    def test_cost_per_window_fallback_and_observed_ratio(self):
        now = 1000.0
        cost, n = NativeQuotaProvider._cost_per_window({}, now)
        self.assertEqual((cost, n), (NativeQuotaProvider.FALLBACK_FULL_WINDOW_COST, 0))
        cycles = {100: [(0.5, 0.10, 7000), (0.5, 0.16, 7000)],
                  200: [(0.4, 0.20, 8000), (0.4, 0.30, 8000)],
                  2000: [(0.9, 0.0, 9000)]}  # still open (reset > now) -> excluded
        cost, n = NativeQuotaProvider._cost_per_window(cycles, now)
        self.assertEqual(n, 2)
        # median ratio 0.25, mean peak 0.45 -> 0.1125
        self.assertAlmostEqual(cost, 0.45 * 0.25)

    def windows_with(self, gauges, cycles=None):
        model = {"acct": {"latest": {"provider": "anthropic", "probed_at": "x",
                                     "gauges": gauges}, "cycles": cycles or {}}}
        with mock.patch.object(self.p, "cred_state", lambda: []), \
                mock.patch.object(self.p, "_history_model", lambda now, **kw: model):
            return self.p.windows()[0]

    def test_verdicts(self):
        now = time.time()
        session = {"label": "5h", "kind": "session", "utilization": 0.5, "reset": now}
        row = self.windows_with([session])  # no weekly gauge at all
        self.assertEqual((row["verdict"], row["assumed"]), ("no-weekly", True))
        weekly = {"label": "7d", "kind": "period", "utilization": 0.1,
                  "reset": now + 10 * 3600}
        row = self.windows_with([session, weekly])
        self.assertEqual(row["verdict"], "waste-danger")  # more budget than slots
        self.assertEqual(row["windows_fit"], 2.0)
        self.assertEqual(row["windows_left"], 2.0)
        weekly = {"label": "7d", "kind": "period", "utilization": 0.9,
                  "reset": now + 100 * 3600}
        row = self.windows_with([session, weekly])
        self.assertEqual(row["verdict"], "ok")
        self.assertAlmostEqual(row["windows_left"], 0.8, places=2)

    def test_an_unread_gauge_is_no_budget_reading_in_the_windows_model(self):
        """task/2935: an unread weekly is not a weekly at 0% (a budget for
        every slot, read as waste-danger and so as abundance), and an unread
        session peak is not a cycle sample. Arithmetic on either None raises
        inside the model and takes every account's row with it."""
        now = time.time()
        session = {"label": "5h", "kind": "session", "utilization": 0.5, "reset": now}
        weekly = {"label": "7d", "kind": "period", "utilization": None,
                  "reset": now + 10 * 3600}
        row = self.windows_with([session, weekly])
        self.assertEqual(row["verdict"], "no-weekly")
        self.assertNotEqual(row["verdict"], "waste-danger")
        # the history pass skips an unread session sample rather than
        # comparing it against the measured peaks of its cycle
        stamp = providers._iso_z()
        with open(self.hist, "w") as f:
            for util in (0.4, None):
                f.write(json.dumps({"account": "a", "probed_at": stamp, "gauges": [
                    dict(session, reset=now - 60, utilization=util),
                    dict(weekly, utilization=0.1)]}) + "\n")
        model = self.p._history_model(now)
        self.assertEqual(model["a"]["cycles"][now - 60], [(0.4, 0.1, weekly["reset"])])
        self.assertEqual(NativeQuotaProvider._cost_per_window(
            model["a"]["cycles"], now)[1], 1)


class AllocateTest(NativeBase):
    def alloc(self, model, accounts, states, rules=None):
        with mock.patch.object(self.p, "accounts", lambda: accounts), \
                mock.patch.object(self.p, "cred_state", lambda: states), \
                mock.patch.object(self.p, "_allocation_rules", lambda: rules or {}):
            return self.p.allocate(model)

    ACCTS = [
        {"name": "fresh", "provider": "anthropic", "home": "/h1", "usable": True, "tier": None},
        {"name": "dry", "provider": "anthropic", "home": "/h2", "usable": True, "tier": None},
        {"name": "err", "provider": "anthropic", "home": "/h3", "usable": True, "tier": None},
        {"name": "cx", "provider": "codex", "home": "/h4", "usable": True, "tier": None}]
    STATES = [
        {"account": "fresh", "cred_state": "ok", "headroom_pct": 80, "tier": None, "status": "allowed"},
        {"account": "dry", "cred_state": "exhausted", "headroom_pct": 0, "tier": None, "status": "blocked"},
        {"account": "err", "cred_state": "api-error", "headroom_pct": None, "tier": None,
         "status": "needs_reauth"},
        {"account": "cx", "cred_state": "unknown", "headroom_pct": None, "tier": None,
         "status": "needs_reauth"}]

    def test_family_filter_blocking_and_headroom_order(self):
        rows = self.alloc("claude-fable-5", self.ACCTS, self.STATES)
        self.assertEqual([r["account"] for r in rows], ["fresh", "dry", "err"])
        by = {r["account"]: r for r in rows}
        self.assertTrue(by["fresh"]["eligible"])
        self.assertEqual(by["dry"]["blocked_by"], ["exhausted"])
        self.assertEqual(by["err"]["blocked_by"], ["needs_reauth"])
        # codex family: the "unknown" cred STAYS eligible (self-refresh on launch)
        rows = self.alloc("gpt-5.5", self.ACCTS, self.STATES)
        self.assertEqual([r["account"] for r in rows], ["cx"])
        self.assertTrue(rows[0]["eligible"])

    def test_an_expired_anthropic_token_is_ineligible(self):  # noqa: VACUOUS_ASSERTION — exact eligible/blocked_by tuple is an unconditional structural assertion on the allocator result
        account = {"name": "expired", "provider": "anthropic", "home": "/dead",
                   "usable": True, "tier": "Max"}
        state = {"account": "expired", "cred_state": "expired-token",
                 "headroom_pct": None, "tier": "Max", "status": "reauth-needed"}
        row = self.alloc("claude-fable-5", [account], [state])[0]
        self.assertEqual((row["eligible"], row["blocked_by"]),
                         (False, ["reauth-needed"]))

    def test_a_due_refresh_account_is_blocked_under_its_own_reason(self):
        """The split moved WHO must act, not whether a seat may launch: a dead
        access token still 401s, so the door stays shut. The reason is the
        state, because the status here is a paragraph and blocked_by is a list
        of words a ranker prints."""
        account = {"name": "due", "provider": "anthropic", "home": "/due",
                   "usable": True, "tier": "Max"}
        state = {"account": "due", "cred_state": "due-refresh",
                 "headroom_pct": None, "tier": "Max",
                 "status": "keepalive due (helm's token expired 3h ago; its "
                           "refresh chain is refresh-live) — `helm keepalive "
                           "--apply` refreshes it, no login needed"}
        row = self.alloc("claude-fable-5", [account], [state])[0]
        self.assertEqual((row["eligible"], row["blocked_by"]),
                         (False, ["due-refresh"]))
        # the control on the same door: an ok account of the same family is
        # eligible, so "blocked" is a statement about this state
        ok = dict(account, name="fine")
        okst = {"account": "fine", "cred_state": "ok", "headroom_pct": 70,
                "tier": "Max", "status": "allowed"}
        self.assertTrue(self.alloc("claude-fable-5", [ok], [okst])[0]["eligible"])

    def test_rules_prefer_avoid_and_low_headroom_avoid_blocks(self):
        states = [dict(s) for s in self.STATES]
        states[1].update(cred_state="ok", headroom_pct=5, status="allowed")  # dry: low
        rules = {"models": {"fable": {"prefer": ["err"], "avoid": ["dry"]}}}
        rows = self.alloc("claude-fable-5", self.ACCTS, states, rules)
        self.assertEqual([r["account"] for r in rows], ["err", "fresh", "dry"])
        self.assertEqual(rows[0]["why"], "preferred by rules")
        avoided = rows[-1]
        self.assertFalse(avoided["eligible"])  # avoided AND headroom<10
        self.assertIn("avoid-rule+headroom<10", avoided["blocked_by"])
        # a HEALTHY avoided account is pushed back but stays usable
        states[1]["headroom_pct"] = 60
        rows = self.alloc("claude-fable-5", self.ACCTS, states, rules)
        back = next(r for r in rows if r["account"] == "dry")
        self.assertTrue(back["eligible"])
        self.assertEqual(back["why"], "avoided by rules")

    def test_drain_pin_ranks_expiring_budget_first(self):
        now = time.time()
        model = {"fresh": {"latest": {"gauges": [
            {"label": "7d", "kind": "period", "utilization": 0.3,
             "reset": now + 10 * 3600}]}, "cycles": {}}}
        with mock.patch.object(self.p, "_history_model", lambda n, **kw: model):
            rows = self.alloc("claude-fable-5", self.ACCTS, self.STATES,
                              {"drain_pin": {"enabled": True}})
        self.assertEqual(rows[0]["account"], "fresh")
        self.assertIn("drain-pin: 70%", rows[0]["why"])

    def test_an_unread_weekly_never_pins_a_drain(self):  # noqa: VACUOUS_ASSERTION — the sibling arm above pins the same account on a measured 30% weekly through the same door
        """task/2935: `1 - None` raised inside the ranker; an unread weekly
        is not 100% remaining either, so it cannot pin."""
        now = time.time()
        model = {"fresh": {"latest": {"gauges": [
            {"label": "7d", "kind": "period", "utilization": None,
             "reset": now + 10 * 3600}]}, "cycles": {}}}
        with mock.patch.object(self.p, "_history_model", lambda n, **kw: model):
            rows = self.alloc("claude-fable-5", self.ACCTS, self.STATES,
                              {"drain_pin": {"enabled": True}})
        self.assertEqual([r["account"] for r in rows], ["fresh", "dry", "err"])
        self.assertFalse(rows[0]["why"].startswith("drain-pin"))


class LaunchPreflightTest(NativeBase):
    def test_launch_cmd_shapes_and_errors(self):
        self.claude_home("cl", "u@x.example")
        cx = self.codex_home("cx3", "c@x.example")
        with mock.patch.object(self.p, "_active_homes", lambda: set()):
            cmd = self.p.launch_cmd("u@x.example", "sid-1", model="fable")
            self.assertIn("CLAUDE_CONFIG_DIR=", cmd)
            self.assertIn("claude --model fable --resume sid-1", cmd)
            cmd = self.p.launch_cmd("cx3", "sid-2")
            self.assertEqual(cmd, "CODEX_HOME=%s codex resume sid-2" % cx)
            with self.assertRaises(ProviderError):
                self.p.launch_cmd("ghost", "sid")
        self.claude_home("noauth", "n@x.example", authed=False)
        with self.assertRaises(ProviderError):
            self.p.launch_cmd("n@x.example", "sid")

    def test_preflight_found_open_and_missing(self):
        self.claude_home("cl2", "u@x.example")
        store = os.path.join(self.tmp, ".claude", "projects", "-work-x")
        os.makedirs(store)
        sid = "cafe0000-1111-2222-3333-444444444444"
        with open(os.path.join(store, sid + ".jsonl"), "w") as f:
            f.write(json.dumps({"cwd": "/work/x"}) + "\n")
        quiet = mock.Mock(return_value=mock.Mock(stdout="", returncode=1))
        with mock.patch.object(providers.subprocess, "run", quiet):
            res = self.p.preflight("u@x.example", sid, "claude")
        self.assertTrue(res["resolvable"])
        self.assertEqual(res["true_cwd"], "/work/x")
        self.assertIsNone(res["reason"])
        holder = mock.Mock(return_value=mock.Mock(
            stdout="4242 claude --resume " + sid, returncode=0))
        with mock.patch.object(providers.subprocess, "run", holder):
            res = self.p.preflight("u@x.example", sid, "claude")
        self.assertEqual(res["live_holder_pid"], 4242)
        self.assertIn("double-open", res["reason"])
        with mock.patch.object(providers.subprocess, "run", quiet):
            res = self.p.preflight("u@x.example", "no-such-sid", "claude")
        self.assertFalse(res["resolvable"])
        self.assertIn("No conversation found", res["reason"])


class SelectionTest(NativeBase):
    def test_default_is_native_cli_optin_falls_back_when_missing(self):
        self.assertIsInstance(providers.default_provider(), NativeQuotaProvider)
        # cli opt-in with a named binary that isn't installed -> native, loudly
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"HELM_PROVIDER": "cli",
                                          "HELM_QUOTA_CLI": "fakequota"}), \
                mock.patch.object(providers.shutil, "which", lambda b: None), \
                contextlib.redirect_stderr(err):
            self.assertIsInstance(providers.default_provider(), NativeQuotaProvider)
        self.assertIn("using native provider", err.getvalue())
        with mock.patch.dict(os.environ, {"HELM_PROVIDER": "cli",
                                          "HELM_QUOTA_CLI": "fakequota"}), \
                mock.patch.object(providers.shutil, "which", lambda b: "/bin/" + b):
            prov = providers.default_provider()
        self.assertIsInstance(prov, providers.CliQuotaProvider)
        self.assertEqual(prov.binary, "fakequota")

    def test_cli_optin_without_binary_config_falls_back_loudly(self):
        # no baked default binary (the quota CLI is a site choice, not shipped
        # code's): HELM_PROVIDER=cli with HELM_QUOTA_CLI unset -> native, loudly
        env = {"HELM_PROVIDER": "cli"}
        os.environ.pop("HELM_QUOTA_CLI", None)
        err = io.StringIO()
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(providers.shutil, "which",
                                  lambda b: "/bin/" + b), \
                contextlib.redirect_stderr(err):
            self.assertIsInstance(providers.default_provider(), NativeQuotaProvider)
        self.assertIn("HELM_QUOTA_CLI is unset", err.getvalue())

    def test_cli_provider_error_paths(self):
        cli = providers.CliQuotaProvider(binary="definitely-not-a-real-binary")
        with self.assertRaises(ProviderError):
            cli._run("list")
        # an unconfigured binary raises the config error, never subprocess junk
        os.environ.pop("HELM_QUOTA_CLI", None)
        with self.assertRaises(ProviderError):
            providers.CliQuotaProvider()._run("list")
        done = mock.Mock(returncode=0, stdout="not json", stderr="")
        with mock.patch.object(providers.subprocess, "run", return_value=done):
            with self.assertRaises(ProviderError):
                cli._json("list", "--json")
        failed = mock.Mock(returncode=3, stdout="", stderr="boom")
        with mock.patch.object(providers.subprocess, "run", return_value=failed):
            self.assertEqual(cli._json("list", "--json", default=[]), [])
            with self.assertRaises(ProviderError):
                cli._json("list", "--json")


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------------
# task/2480 — the codex POOL budget, on EVERY window.
#
# The state these arms pin: a pooled codex account at its WEEKLY cap while the
# 5h window reads healthy, and a codex CLI home whose access token is weeks
# stale and answers HTTP 401 while the pooled token for the same account
# answers 200.
# --------------------------------------------------------------------------
FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "codex-wham-usage-2026-09-14.json")


def recorded(key):
    """A body RECORDED from the vendor, never imagined. See the fixture's own
    `_origin` field for exactly what was captured and what was scrubbed."""
    with open(FIXTURE) as f:
        return json.load(f)[key]


class RecordedFixtureIsTheRealShapeTest(unittest.TestCase):
    """The fixture is load-bearing for every arm below, so it states its own
    provenance and the two facts the lane turns on."""

    def test_the_fixture_declares_its_origin_and_carries_the_bug_shape(self):
        with open(FIXTURE) as f:
            doc = json.load(f)
        self.assertIn("backend-api/wham/usage", doc["_origin"])
        self.assertIn("2026-09-14", doc["_origin"])
        team = doc["team"]["rate_limit"]
        # THE BUG IN ONE BODY: real headroom on the 5h, nothing left on the 7d.
        self.assertEqual(team["primary_window"]["used_percent"], 52)
        self.assertEqual(team["primary_window"]["limit_window_seconds"], 18000)
        self.assertEqual(team["secondary_window"]["used_percent"], 100)
        self.assertEqual(team["secondary_window"]["limit_window_seconds"], 604800)
        self.assertTrue(team["limit_reached"])
        # and the pro shape: ONE 7d primary, no secondary at all.
        pro = doc["pro"]["rate_limit"]
        self.assertEqual(pro["primary_window"]["limit_window_seconds"], 604800)
        self.assertIsNone(pro["secondary_window"])


class CodexBudgetReaderTest(unittest.TestCase):
    ACCT = {"account_id": "acct-fixture-team", "email": "team@fixture.invalid",
            "plan": "team", "tier": "team", "file": "codex-team.json",
            "files": ["codex-team.json"], "access_token": "FAKE-pool-token"}

    def setUp(self):
        from helm import codexbudget
        self.mod = codexbudget
        self.tmp = tempfile.mkdtemp(prefix="helm-test-budget-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.envp = mock.patch.dict(os.environ, {"HOME": self.tmp,
                                                 "HELM_HOME": os.path.join(self.tmp, ".helm"),
                                                 "HELM_CACHE_DIR": os.path.join(self.tmp, "cache")})
        self.envp.start()
        self.addCleanup(self.envp.stop)
        os.environ.pop("HELM_CODEX_WEEKLY_CEILING_PCT", None)
        self.now = 1_789_378_195.0     # the instant the fixture was recorded

    def probe(self, body=None, error=None, acct=None, ceiling=None):
        get = mock.Mock(side_effect=error) if error else mock.Mock(return_value=body)
        row = self.mod.probe_record(acct or self.ACCT, get_json=get,
                                    now=self.now, ceiling=ceiling)
        return row, get

    def http_error(self, code):
        return urllib.error.HTTPError("http://x", code, "boom", None, io.BytesIO(b""))

    def test_the_team_body_yields_both_windows_and_the_WEEKLY_is_what_binds(self):
        row, get = self.probe(recorded("team"))
        self.assertEqual(get.call_args[0][0], providers.CODEX_USAGE_URL)
        self.assertEqual(get.call_args[0][1]["chatgpt-account-id"], "acct-fixture-team")
        self.assertEqual([w["label"] for w in row["windows"]], ["7d", "5h"])
        by = {w["label"]: w for w in row["windows"]}
        # THE CONTROL FOR "THE WEEKLY BINDS": the 5h window on this very body is
        # measurably LOWER (52), so binding on the 7d is a choice this reader
        # made and not the only number available to it.
        self.assertEqual(by["5h"]["used_percent"], 52.0)
        self.assertEqual(by["7d"]["used_percent"], 100.0)
        self.assertEqual(row["binding_gauge"]["label"], "7d")
        self.assertEqual(row["longest_pct"], 100.0)
        self.assertEqual(row["state"], "exhausted")
        self.assertEqual(row["status"], "blocked")
        self.assertFalse(row["allowed"])
        # the vendor sends reached_type as an OBJECT; the row carries a string
        self.assertEqual(row["reached_type"], "workspace_owner_credits_depleted")
        # reset_after is derived from reset_at against the recorded instant,
        # and lands on the vendor's own reset_after_seconds (5431 / 483728)
        self.assertEqual(by["5h"]["reset_after_seconds"], 5431)
        self.assertEqual(by["7d"]["reset_after_seconds"], 483729)

    def test_the_pro_body_has_ONE_account_window_and_a_scoped_one_that_is_not_headroom(self):
        row, _ = self.probe(recorded("pro"))
        self.assertEqual([w["label"] for w in row["windows"]], ["7d"])
        self.assertEqual(row["longest_pct"], 100.0)
        self.assertEqual(row["plan"], "pro")
        # THE CONTROL: the scoped 0% Spark budget IS parsed — it is present on
        # the gauges — so excluding it from `windows` is the reader's rule and
        # not a parse that silently dropped it. Were it counted, this account
        # would read as having headroom while it is weekly-capped.
        labels = [g["label"] for g in row["gauges"]]
        self.assertIn("5h-gpt-5.3-codex-spark", labels)
        self.assertIn("7d-gpt-5.3-codex-spark", labels)
        self.assertEqual(row["binding_gauge"]["label"], "7d")

    def test_a_REJECTED_token_is_unknown_and_never_zero(self):
        row, _ = self.probe(error=self.http_error(401))
        self.assertEqual((row["state"], row["status"]), ("unknown", "needs_reauth"))
        self.assertIsNone(row["longest_pct"])       # NEVER 0.0 — 0 reads as wide open
        self.assertEqual(row["windows"], [])
        self.assertIn("re-pool", row["note"])
        # THE CONTROL: the same account on the recorded body DOES produce a
        # number, so the None above is the rejection's doing and not a reader
        # that never yields one.
        ok, _ = self.probe(recorded("team"))
        self.assertEqual(ok["longest_pct"], 100.0)

    def test_an_UNREACHABLE_endpoint_is_unknown_and_never_zero(self):
        # THE POSITIVE CONTROL FIRST, unconditionally: this reader DOES mint a
        # number for this account on a real body, so the Nones below are the
        # failure's doing and not a reader that never yields one.
        self.assertEqual(self.probe(recorded("team"))[0]["longest_pct"], 100.0)
        row, _ = self.probe(error=OSError("down"))
        self.assertEqual((row["state"], row["status"]), ("unknown", "network-error"))
        self.assertIsNone(row["longest_pct"])
        row, _ = self.probe(error=self.http_error(503))
        self.assertEqual((row["state"], row["status"]), ("unknown", "http_503"))
        self.assertIsNone(row["longest_pct"])

    def test_no_token_reaches_the_row_or_the_snapshot(self):  # noqa: VACUOUS_ASSERTION — the unconditional assertIn('acct-fixture-team', text) is the positive control on the same observable: it proves the snapshot was actually written and read back, so an assertNotIn on the token cannot pass over an empty file
        row, _ = self.probe(recorded("team"))
        self.assertNotIn("FAKE-pool-token", json.dumps(row))
        self.mod.write_snapshot([row], ceiling=90.0, now=self.now)
        with open(self.mod.snapshot_path()) as f:
            text = f.read()
        self.assertNotIn("FAKE-pool-token", text)
        self.assertIn("acct-fixture-team", text)    # the control: it DID write

    def test_the_ceiling_is_owner_settable_and_junk_falls_back(self):
        self.assertEqual(self.mod.ceiling_pct(), 90.0)
        for raw, want in (("75", 75.0), ("100", 100.0), ("", 90.0),
                          ("banana", 90.0), ("0", 90.0), ("101", 90.0)):
            with mock.patch.dict(os.environ,
                                 {"HELM_CODEX_WEEKLY_CEILING_PCT": raw}):
                self.assertEqual(self.mod.ceiling_pct(), want, raw)

    def test_near_is_the_ceiling_and_ok_is_below_it(self):
        body = json.loads(json.dumps(recorded("team")))
        body["rate_limit"]["limit_reached"] = False
        body["rate_limit"]["allowed"] = True
        body["rate_limit"]["secondary_window"]["used_percent"] = 91
        row, _ = self.probe(body, ceiling=90.0)
        self.assertEqual(row["state"], "near")
        body["rate_limit"]["secondary_window"]["used_percent"] = 50
        row, _ = self.probe(body, ceiling=90.0)
        self.assertEqual(row["state"], "ok")


class CodexBudgetVerdictTest(unittest.TestCase):
    def setUp(self):
        from helm import codexbudget
        self.mod = codexbudget

    def rows(self, *pcts):
        return [{"email": "a%d@x" % i, "state": "ok",
                 "longest_pct": p, "source": "measured",
                 "windows": [{"label": "7d", "used_percent": p,
                              "reset_after_seconds": 3600,
                              "source": "measured"}]}
                for i, p in enumerate(pcts)]

    def test_capped_requires_every_account_READ_and_over(self):
        v = self.mod.verdict(self.rows(95.0, 99.0), ceiling=90.0)
        self.assertEqual(v["decision"], "capped")
        # ONE UNREADABLE ACCOUNT IS NOT A MEASURED WALL: refusing there would
        # be the guard-fires-on-absence class.
        v = self.mod.verdict(self.rows(95.0, 99.0) + self.rows(None), ceiling=90.0)
        self.assertEqual(v["decision"], "mixed")

    def test_mixed_names_the_headroom_and_clear_is_silent(self):
        v = self.mod.verdict(self.rows(95.0, 40.0), ceiling=90.0)
        self.assertEqual(v["decision"], "mixed")
        self.assertIn("a1@x 40% used", self.mod.headroom_text(v))
        self.assertEqual(self.mod.verdict(self.rows(40.0, 10.0),
                                          ceiling=90.0)["decision"], "clear")
        self.assertEqual(self.mod.verdict(self.rows(None),
                                          ceiling=90.0)["decision"], "unknown")
        self.assertEqual(self.mod.verdict([], ceiling=90.0)["decision"], "unknown")

    def test_the_notice_latches_on_the_colour_not_the_numbers(self):  # noqa: VACUOUS_ASSERTION — the crossing post and its tag are asserted PRESENT in this method before the unchanged-colour pass is asserted silent
        """THE ANNOUNCER FOR THIS TAG LIVES IN ONE PLACE. One tag with two
        producers is two chances to tell the room a different thing, so this
        module publishes the verdict and the burn-flag fold publishes the
        post, over every family at once. The latch is on the COLOUR, and for
        this family's rows the crossing is the same one the pool's decision
        names."""
        from helm import burnflags
        self.assertFalse(hasattr(self.mod, "watch_notice"))
        self.assertFalse(hasattr(self.mod, "NOTICE_TAG"))
        flags = burnflags.fold({"ceiling": 90.0,
                                "money": {"codex": self.rows(95.0, 99.0)},
                                "money_measured_at": {"codex": 1789000000.0}},
                               now=1789000000.0)["families"]
        body, colours = burnflags.watch_notice(flags, None)
        self.assertEqual(colours["codex"], burnflags.RED)
        self.assertIn(burnflags.NOTICE_TAG, body)
        # SAME COLOUR, DIFFERENT NUMBERS -> SILENT. Percentages move every
        # pass by construction; only the crossing is news.
        moved = burnflags.fold({"ceiling": 90.0,
                                "money": {"codex": self.rows(96.0, 100.0)},
                                "money_measured_at": {"codex": 1789000000.0}},
                               now=1789000000.0)["families"]
        again, colours2 = burnflags.watch_notice(moved, colours)
        self.assertIsNone(again)
        self.assertEqual(colours2["codex"], burnflags.RED)
        # and a pool with headroom never posts at all
        clear = burnflags.fold({"ceiling": 90.0,
                                "money": {"codex": self.rows(10.0)},
                                "money_measured_at": {"codex": 1789000000.0}},
                               now=1789000000.0)["families"]
        self.assertEqual(clear["codex"]["colour"], burnflags.YELLOW)
        quiet, colours3 = burnflags.watch_notice(clear, None)
        self.assertIsNone(quiet)
        self.assertEqual(colours3["codex"], burnflags.YELLOW)
        self.assertEqual(self.mod.verdict(self.rows(10.0),
                                          ceiling=90.0)["decision"], "clear")


class CodexPoolCensusTest(unittest.TestCase):
    """task/2480 R2/R4/R5 — what the POOL DIRECTORY answers, and what it is
    honest about when it cannot answer.

    Every arm drives the shipped readers over REAL FILES in the pool directory
    the shipped `codexhomes.pool_dir()` names: the defects here are all about
    what the reader does with a file, so a fixture that handed the reader a
    list of dicts would test nothing about any of them."""

    def setUp(self):
        from helm import codexbudget, codexhomes
        self.mod, self.homes = codexbudget, codexhomes
        self.tmp = tempfile.mkdtemp(prefix="helm-test-pool-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.envp = mock.patch.dict(
            os.environ, {"HOME": self.tmp,
                         "HELM_HOME": os.path.join(self.tmp, ".helm"),
                         "HELM_CACHE_DIR": os.path.join(self.tmp, "cache")})
        self.envp.start()
        self.addCleanup(self.envp.stop)
        os.environ.pop("HELM_CODEX_WEEKLY_CEILING_PCT", None)
        self.pool = self.homes.pool_dir()
        os.makedirs(self.pool, exist_ok=True)
        self.now = 1_789_378_195.0

    def pooled(self, fname, **fields):
        rec = {"type": "codex", "email": "a@x.example", "account_id": "acct-A",
               "access_token": "FAKE-pool-token"}
        rec.update(fields)
        with open(os.path.join(self.pool, fname), "w") as f:
            json.dump(rec, f)

    def broken(self, fname="codex-broken.json"):
        with open(os.path.join(self.pool, fname), "w") as f:
            f.write("{ this file is not json")
        return fname

    def capped(self, acct):
        """The account's row over the RECORDED capped body — the shipped
        prober, not a hand-written row."""
        return self.mod.probe_record(acct, get_json=lambda u, h: recorded("team"),
                                     now=self.now, ceiling=90.0)

    def test_an_UNPARSEABLE_pool_FILE_stays_in_the_census_as_UNKNOWN(self):
        """R2 — a file that will not parse must not DROP OUT of the census.
        Where it drops out, one capped account plus one unreadable file reads
        as a complete, fully measured, fully capped census: the single state
        that REFUSES every codex dispatch. The file's account, type and
        availability are all unknown, and that uncertainty is exactly why the
        census is not complete."""
        self.pooled("codex-a.json")
        broken = self.broken()
        accounts, census = self.mod.pool_census()
        self.assertEqual(census, self.mod.CENSUS_MEASURED)
        self.assertEqual(sorted(a["file"] for a in accounts),
                         ["codex-a.json", broken])
        unread = next(a for a in accounts if a.get("unread"))
        row = self.mod.probe_record(unread)
        self.assertEqual((row["state"], row["status"]),
                         ("unknown", "unreadable-file"))
        self.assertIsNone(row["longest_pct"])       # NEVER 0.0, NEVER a wall
        self.assertIn(broken, row["note"])
        capped = self.capped(next(a for a in accounts if not a.get("unread")))
        self.assertEqual(capped["longest_pct"], 100.0)
        v = self.mod.verdict([capped, row], ceiling=90.0)
        self.assertEqual(v["decision"], "mixed")    # warns and ADMITS
        self.assertIn(broken, self.mod.warning_text("codex", v))
        # THE CONTROL, on the identical capped row: the SAME census WITHOUT the
        # unreadable file does refuse. Blast radius: one verdict call over one
        # row — it asserts the defect's own state is still reachable, so the
        # `mixed` above is the unread file's doing and not a verdict that can
        # no longer say `capped`.
        self.assertEqual(self.mod.verdict([capped], ceiling=90.0)["decision"],
                         "capped")

    def test_a_PARSEABLE_tokenless_record_is_a_DIFFERENT_unknown(self):
        """Two ways of not knowing, and they send an operator to two different
        places: the bytes would not parse, versus the record parsed and holds
        no token. Neither is a wall."""
        self.pooled("codex-a.json", access_token=None)
        accounts, _ = self.mod.pool_census()
        row = self.mod.probe_record(accounts[0])
        self.assertEqual((row["state"], row["status"]),
                         ("unknown", "no-credentials"))
        self.assertIsNone(row["longest_pct"])
        self.assertEqual(self.mod.verdict([row], ceiling=90.0)["decision"],
                         "unknown")
        # a record that PARSES and is not a codex credential is neither: it is
        # KNOWN not to be pooled codex, and stays out of the census entirely.
        self.pooled("gemini.json", type="gemini")
        self.assertEqual([a["file"] for a in self.mod.pool_accounts()],
                         ["codex-a.json"])

    def test_team_members_sharing_a_workspace_id_are_probed_each(self):
        """Measured: d@, hen@ and admin@ carry ONE
        chatgpt_account_id (the Team WORKSPACE) and three chatgpt_user_ids;
        keyed on the account alone the census probed d@ once and printed the
        reading as admin@'s. The credential is account AND user."""
        def jwt(user):
            payload = base64.urlsafe_b64encode(json.dumps(
                {"https://api.openai.com/auth": {"chatgpt_user_id": user}}).encode())
            return "h." + payload.decode().rstrip("=") + ".sig"
        self.pooled("codex-d.json", email="d@x.example", account_id="ws-1",
                    id_token=jwt("user-d"))
        self.pooled("codex-admin.json", email="admin@x.example", account_id="ws-1",
                    id_token=jwt("user-admin"))
        rows = self.mod.pool_accounts()
        self.assertEqual([(r["file"], r["user_id"]) for r in rows],
                         [("codex-admin.json", "user-admin"), ("codex-d.json", "user-d")])
        # CONTROL: the same user under a second spelling IS one budget
        self.pooled("codex-d-again.json", email="d@x.example", account_id="ws-1",
                    id_token=jwt("user-d"))
        rows = {r["file"]: r for r in self.mod.pool_accounts()}
        # the census is filename-sorted, so the `-again` spelling is read first
        self.assertEqual(sorted(rows), ["codex-admin.json", "codex-d-again.json"])
        self.assertEqual(rows["codex-d-again.json"]["files"],
                         ["codex-d-again.json", "codex-d.json"])
        # and two records whose user ids are BOTH unknown no longer fold:
        # the unknown half could be any member — including the same one — so
        # the fold that merged them was writing an unproven identity (2742).
        # They stay two rows, each honestly UNKNOWN about which member it is.
        self.pooled("codex-p.json", email="p@x.example", account_id="acct-P")
        self.pooled("codex-p2.json", email="p@x.example", account_id="acct-P")
        rows = {r["file"]: r for r in self.mod.pool_accounts()}
        self.assertIn("codex-p.json", rows)
        self.assertIn("codex-p2.json", rows)
        self.assertEqual(rows["codex-p.json"]["files"], ["codex-p.json"])

    def test_an_unknown_user_id_never_folds_into_a_known_sibling(self):
        """THE 2742 PROBE, on the census door itself: one record's user claim
        unreadable, its sibling's known. The permissive matcher folds them
        and the row carries BOTH files with the FIRST record's token, so a
        probe of one member reads as the other's. The strict comparator keeps
        them apart."""
        def jwt(user):
            payload = base64.urlsafe_b64encode(json.dumps(
                {"https://api.openai.com/auth": {"chatgpt_user_id": user}}).encode())
            return "h." + payload.decode().rstrip("=") + ".sig"
        self.pooled("codex-known.json", email="k@x.example", account_id="ws-9",
                    id_token=jwt("user-k"))
        self.pooled("codex-faded.json", email="k@x.example", account_id="ws-9",
                    id_token="h.e30.sig")   # a token carrying NO user claim
        rows = {r["file"]: r for r in self.mod.pool_accounts()}
        # MUST-HIT: both files were read at all.
        self.assertEqual(sorted(rows), ["codex-faded.json", "codex-known.json"])
        self.assertEqual(rows["codex-known.json"]["files"],
                         ["codex-known.json"],
                         "the unknown-user record folded into the known "
                         "sibling — one row, one token, two files")
        self.assertIsNone(rows["codex-faded.json"]["user_id"])
        self.assertEqual(rows["codex-faded.json"]["files"],
                         ["codex-faded.json"])

    def test_the_EMPTY_pool_and_the_UNREADABLE_one_are_not_one_answer(self):
        """R3's census half. `helm codex unpool` of the last account leaves a
        directory that lists and is empty — a MEASUREMENT that the pool is
        gone. A directory that will not enumerate is silence."""
        self.assertEqual(self.mod.pool_census(), ([], self.mod.CENSUS_EMPTY))
        self.pooled("codex-a.json")
        self.assertEqual(self.mod.pool_census()[1], self.mod.CENSUS_MEASURED)
        os.remove(os.path.join(self.pool, "codex-a.json"))
        self.assertEqual(self.mod.pool_census(), ([], self.mod.CENSUS_EMPTY))
        # AN UNENUMERABLE POOL IS `unread`, and the trigger is uid-independent
        # on purpose: a chmod 000 arm passes only while the suite runs as a
        # non-root user, and this suite must answer the same question in a
        # container that runs as root.
        shutil.rmtree(self.pool)
        with open(self.pool, "w") as f:
            f.write("not a directory")
        self.assertEqual(self.mod.pool_census(), ([], self.mod.CENSUS_UNREAD))

    def test_a_pool_path_carrying_GLOB_METACHARACTERS_is_read_LITERALLY(self):  # noqa: VACUOUS_ASSERTION — this arm asserts no absence at all: BOTH legs of the loop assert a PRESENT census (CENSUS_MEASURED naming codex-A.json and acct-A), and each leg first asserts its own premise unconditionally (the pool path carries the home spelling, the shipped writer returned ok, the file is on disk). The loop runs a fixed two-element literal, so neither leg can be skipped
        """task/2480 F1, witness B. HELM_HOME is a LITERAL path everywhere else
        in helm: `home` accepts /safe/helm[budget], `seat_paths` derives the
        codex seat under it, and the SHIPPED pool writer puts a real
        codex-A.json inside it. Only the READER disagreed — it went through
        `glob`, which reads that bracket pair as a character class, matches
        nothing, and answers `[]` on EVERY pass, forever. `pool_census` then
        called a populated directory KNOWN-EMPTY, which is the one reading
        that retires a standing all-capped refusal.

        Both legs run the same fixture end to end through the shipped writer
        and the shipped reader; only the HOME's spelling differs."""
        from tests.test_codexhomes import _auth_json
        for leaf in ("helm[budget]", "helm-budget"):
            with self.subTest(home=leaf):
                tmp = tempfile.mkdtemp(prefix="helm-test-poolglob-")
                self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
                envp = mock.patch.dict(
                    os.environ,
                    {"HOME": tmp,
                     "HELM_HOME": os.path.join(tmp, leaf),
                     "HELM_CACHE_DIR": os.path.join(tmp, "cache"),
                     "HELM_CODEX_HOMES_DIR": os.path.join(tmp, "codex-homes")})
                envp.start()
                try:
                    d = os.path.join(os.environ["HELM_CODEX_HOMES_DIR"], "A")
                    os.makedirs(d)
                    auth, _exp = _auth_json("A", email="a@x.example", plan="team")
                    with open(os.path.join(d, "auth.json"), "w") as f:
                        json.dump(auth, f)
                    res = self.homes.codex_pool("A")     # THE SHIPPED WRITER
                    self.assertTrue(res.get("ok"), res)
                    # the fixture's own premise: the pool path really does
                    # carry this spelling, and a real file really is in it.
                    self.assertIn(leaf, self.homes.pool_dir())
                    self.assertEqual(
                        [n for n in os.listdir(self.homes.pool_dir())
                         if n.endswith(".json")], ["codex-A.json"])
                    accounts, census = self.mod.pool_census()
                    self.assertEqual(
                        census, self.mod.CENSUS_MEASURED,
                        "a populated pool under %r read as %r — a directory "
                        "the writer just filled may never be published as "
                        "empty" % (self.homes.pool_dir(), census))
                    self.assertEqual([a["file"] for a in accounts],
                                     ["codex-A.json"])
                    self.assertEqual(accounts[0]["account_id"], "acct-A")
                finally:
                    envp.stop()
        # THE CONTROL is the second leg above: the SAME record, written by the
        # SAME shipped writer, under a PLAIN home. Blast radius: one extra
        # census over one file. It is green before and after this cure, so the
        # bracket leg's answer is about the metacharacters in the path and not
        # about a fixture that cannot produce a measured census at all.

    def test_an_enumeration_that_FAILS_over_a_populated_pool_is_UNREAD(self):  # noqa: VACUOUS_ASSERTION — the empty rows list IS the product law (an unread census publishes no accounts) and the token beside it is asserted to be CENSUS_UNREAD, never CENSUS_EMPTY. Two unconditional positive controls sit on the same observable: the injected OSError is asserted to have FIRED, and the same call over the same untouched directory is asserted to report CENSUS_MEASURED naming codex-a.json
        """task/2480 F1, witness A. The pool was enumerated TWICE: a glob for
        the records, then a listdir to classify an empty answer. A transient
        OSError on the FIRST is swallowed by the glob and yields `[]`; the
        SECOND then succeeds over the unchanged, populated directory — and the
        census threw those names away and published KNOWN-EMPTY, retiring a
        standing all-capped refusal that nothing had measured away.

        So the failure is injected into the FIRST enumeration only, whichever
        call that is: pre-cure that is glob's `os.scandir` (and the surviving
        `os.listdir` then reports the pool is fine), post-cure it is the ONE
        `os.listdir` the reader makes."""
        self.pooled("codex-a.json")
        real_listdir, real_scandir = os.listdir, os.scandir
        armed = {"n": 1}

        def _boom(path):
            if armed["n"] and os.fspath(path) == self.pool:
                armed["n"] -= 1
                raise OSError(5, "Input/output error")

        def fake_listdir(path=".", *a, **kw):
            _boom(path)
            return real_listdir(path, *a, **kw)

        def fake_scandir(path=".", *a, **kw):
            _boom(path)
            return real_scandir(path, *a, **kw)

        with mock.patch("os.listdir", fake_listdir), \
                mock.patch("os.scandir", fake_scandir):
            self.assertEqual(
                self.mod.pool_census(), ([], self.mod.CENSUS_UNREAD),
                "an enumeration that FAILED is not a measurement that the "
                "pool is empty — publishing known-empty here clears a "
                "standing refusal on the strength of an error")
        # ASSERT THE INJECTED INPUT ARRIVED: a fixture that never fired would
        # make the line above a reading of an ordinary pass.
        self.assertEqual(armed["n"], 0, "the OSError was never raised")
        # THE MUST-HIT, on the SAME directory, untouched: the very same call
        # now reports the populated census. Blast radius: one census over one
        # real file — it goes red only if this reader can never say MEASURED,
        # which is exactly what would make the UNREAD above worthless.
        accounts, census = self.mod.pool_census()
        self.assertEqual(census, self.mod.CENSUS_MEASURED)
        self.assertEqual([a["file"] for a in accounts], ["codex-a.json"])

    def test_the_email_fallback_never_crosses_a_KNOWN_different_account_id(self):
        """R4 — the fallback exists for a home whose id the pool does not
        carry, and it was applied on ANY id miss: a home holding account A fell
        through to pooled account B because B shares the login address, and the
        caller then probed B's token under B's account header and rendered the
        answer as A's budget."""
        self.pooled("codex-b.json", account_id="acct-B", email="shared@x.example")
        hit, note = self.mod.pool_record_for(account_id="acct-B",
                                             email="shared@x.example")
        self.assertEqual(hit["file"], "codex-b.json")    # exact id wins
        self.assertIsNone(note)
        miss, note = self.mod.pool_record_for(account_id="acct-OTHER",
                                              email="shared@x.example")
        self.assertIsNone(miss)
        self.assertIn("acct-B", note)                    # BOTH ids named
        self.assertIn("acct-OTHER", note)
        # THE LEGITIMATE FALLBACKS ARE UNTOUCHED, and they are the control that
        # proves the refusal above is about the CONTRADICTION and not about the
        # fallback being switched off: an email-only lookup still resolves, and
        # so does a pooled row carrying no id of its own to contradict with.
        email_only, note = self.mod.pool_record_for(email="shared@x.example")
        self.assertEqual(email_only["file"], "codex-b.json")
        self.assertIsNone(note)
        self.pooled("codex-noid.json", account_id=None, email="noid@x.example")
        anon, note = self.mod.pool_record_for(account_id="acct-ZZZ",
                                              email="noid@x.example")
        self.assertEqual(anon["file"], "codex-noid.json")
        self.assertIsNone(note)
        self.assertEqual(self.mod.pool_record_for(account_id="acct-ZZZ",
                                                  email="nobody@x.example"),
                         (None, None))

    def test_a_MALFORMED_window_is_unknown_for_that_account_and_never_a_zero(self):  # noqa: VACUOUS_ASSERTION — the loop's per-shape assertions are the absences, and each is covered on its own row by the unconditional `status == "malformed"` pair above it plus the three unconditional positive controls below, which drive the SAME prober over numeric 0, numeric 100 and an absent window and assert it still mints 0.0, exhausted and ok
        """R5 — the per-account parse ran OUTSIDE the GET's try. A present 7d
        window with a null used_percent was normalized by `(percent or 0)` into
        a MEASURED 0%, which votes a capped pool clear; a string percent or a
        wrong-shaped rate_limit RAISED, out through the comprehension that
        probes the pool."""
        acct = {"account_id": "acct-A", "email": "a@x.example", "plan": "team",
                "tier": "team", "file": "codex-a.json", "files": [],
                "access_token": "FAKE-pool-token"}

        def probe(body):
            return self.mod.probe_record(acct, get_json=lambda u, h: body,
                                         now=self.now, ceiling=90.0)

        def week(pct):
            return {"rate_limit": {"secondary_window": {
                "used_percent": pct, "limit_window_seconds": 604800,
                "reset_at": 1789861924}}}

        # THE FORGED ZERO ITSELF, UNCONDITIONALLY AND BOUND, because it is the
        # case the finding turns on: `status == "malformed"` is a positive
        # control on the SAME row that carries the None below, so the None
        # cannot pass over a reader that returned nothing at all.
        null = probe(week(None))
        self.assertEqual(null["status"], "malformed")
        self.assertEqual(null["state"], "unknown")
        self.assertIsNone(null["longest_pct"])
        self.assertIn("used_percent", null["note"])
        for label, body in (
                ("missing percent", {"rate_limit": {"secondary_window": {
                    "limit_window_seconds": 604800}}}),
                ("string percent", week("52")),
                ("true percent", week(True)),
                ("rate_limit is a string", {"rate_limit": "nope"}),
                ("window is a list", {"rate_limit": {"primary_window": [1]}})):
            row = probe(body)
            self.assertEqual((row["state"], row["status"]),
                             ("unknown", "malformed"), label)
            self.assertIsNone(row["longest_pct"], label)
        # THE CONTROLS THAT KEEP THIS FROM BEING A READER THAT ONLY SAYS NO: a
        # numeric 0 is a MEASURED zero and stays one, 100 still caps, and an
        # absent window claims nothing at all.
        self.assertEqual(probe(week(0))["longest_pct"], 0.0)
        self.assertEqual(probe(week(0))["state"], "ok")
        self.assertEqual(probe(week(100))["state"], "exhausted")
        self.assertEqual(probe({"rate_limit": {"secondary_window": None}})["state"],
                         "ok")

    def test_ONE_malformed_account_never_costs_the_healthy_siblings(self):
        """The half of R5 that empties the whole pass: `pool_budget` builds its
        rows in a comprehension, so one account raising discarded every healthy
        sibling already read — and proxywatch then wrote no snapshot at all."""
        self.pooled("codex-a.json", account_id="acct-A", email="a@x.example")
        self.pooled("codex-b.json", account_id="acct-B", email="b@x.example",
                    access_token="FAKE-pool-token-B")
        bodies = {"FAKE-pool-token": recorded("team"),
                  "FAKE-pool-token-B": {"rate_limit": {"primary_window": {
                      "used_percent": "garbage", "limit_window_seconds": 18000}}}}
        rows = self.mod.pool_budget(
            get_json=lambda u, h: bodies[h["Authorization"].split()[1]],
            now=self.now, ceiling=90.0, write_cache=False)
        by = {r["file"]: r for r in rows}
        self.assertEqual(sorted(by), ["codex-a.json", "codex-b.json"])
        # the must-hit: the malformed account really did take the malformed
        # path, so the surviving sibling is not a pass where nothing went wrong
        self.assertEqual(by["codex-b.json"]["status"], "malformed")
        self.assertIsNone(by["codex-b.json"]["longest_pct"])
        self.assertEqual(by["codex-a.json"]["longest_pct"], 100.0)
        self.assertEqual(by["codex-a.json"]["state"], "exhausted")


class CodexPoolNearMissIsNamedTest(NativeBase):
    """R4 at the door the owner reads: a near-miss in the pool must not be
    probed as if it were this account, and must not be silent either."""

    def setUp(self):
        super().setUp()
        self.helmp = mock.patch.dict(
            os.environ, {"HELM_HOME": os.path.join(self.tmp, ".helm"),
                         "HELM_CACHE_DIR": os.path.join(self.tmp, "cache")})
        self.helmp.start()
        self.addCleanup(self.helmp.stop)

    def home(self, name, account_id, email):
        d = os.path.join(self.tmp, ".codex-homes", name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "auth.json"), "w") as f:
            json.dump({"tokens": {"access_token": "HOME-TOKEN",
                                  "account_id": account_id,
                                  "id_token": fake_jwt(email)}}, f)
        return d

    def pool(self, account_id, email, token):
        from helm import codexhomes
        d = codexhomes.pool_dir()
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "codex-%s.json" % account_id), "w") as f:
            json.dump({"type": "codex", "email": email,
                       "account_id": account_id, "access_token": token}, f)

    def test_a_contradictory_pool_row_is_NOT_probed_and_IS_named(self):
        home = self.home("cxA", "acct-A", "shared@x.example")
        self.pool("acct-B", "shared@x.example", "POOL-TOKEN-B")
        acct = {"name": "cx", "provider": "codex", "home": home, "tier": None,
                "email": "shared@x.example"}
        get = mock.Mock(return_value=recorded("team"))
        with mock.patch.object(self.p, "_get_json", get):
            cred, _ = self.p._probe_one(acct)
        # the OTHER account's token is never sent, under any header
        self.assertEqual(get.call_args[0][1]["Authorization"], "Bearer HOME-TOKEN")
        self.assertEqual(get.call_args[0][1].get("chatgpt-account-id"), "acct-A")
        self.assertIn("acct-B", cred["note"])
        self.assertIn("acct-A", cred["note"])
        self.assertIn("NOT pooled", cred["note"])
        # THE CONTROL, on an otherwise identical call: make the pooled row's id
        # MATCH and the pool is used exactly as before, with no note. Blast
        # radius: one more file in the same pool dir, nothing else moves — so
        # the home read above is the CONTRADICTION's doing and not this fixture
        # failing to pool anything at all.
        self.pool("acct-A", "shared@x.example", "POOL-TOKEN-A")
        get2 = mock.Mock(return_value=recorded("team"))
        with mock.patch.object(self.p, "_get_json", get2):
            cred2, _ = self.p._probe_one(acct)
        self.assertEqual(get2.call_args[0][1]["Authorization"],
                         "Bearer POOL-TOKEN-A")
        # the positive control on the SAME row as the absent note: this call
        # produced a real reading, so the None is the pooled path being silent
        # and not a probe that failed and returned an empty row.
        self.assertEqual(cred2["cred_state"], "exhausted")
        self.assertEqual(cred2["headroom_pct"], 0.0)
        self.assertIsNone(cred2.get("note"))


class CodexCredRowReadsThePoolTest(NativeBase):
    """The cure at the door the owner reads: `helm creds` codex rows."""

    def pool(self, account_id, token, email="pool@x.example", plan="team"):
        from helm import codexhomes
        d = codexhomes.pool_dir()
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "codex-%s.json" % email), "w") as f:
            json.dump({"type": "codex", "email": email, "account_id": account_id,
                       "access_token": token, "disabled": False}, f)
        return d

    def setUp(self):
        super().setUp()
        self.helmp = mock.patch.dict(
            os.environ, {"HELM_HOME": os.path.join(self.tmp, ".helm"),
                         "HELM_CACHE_DIR": os.path.join(self.tmp, "cache")})
        self.helmp.start()
        self.addCleanup(self.helmp.stop)

    def acct(self, home, name="cx"):
        return {"name": name, "provider": "codex", "home": home, "tier": None,
                "email": "pool@x.example"}

    def test_the_row_reads_the_POOLED_token_not_the_stale_home_one(self):
        home = self.codex_home("cxp", "pool@x.example", token="HOME-TOKEN-39-DAYS-OLD")
        self.pool("acct-1", "POOL-TOKEN")
        get = mock.Mock(return_value=recorded("team"))
        with mock.patch.object(self.p, "_get_json", get):
            cred, _ = self.p._probe_one(self.acct(home))
        self.assertEqual(get.call_args[0][1]["Authorization"], "Bearer POOL-TOKEN")
        self.assertEqual(cred["cred_state"], "exhausted")
        # THE CONTROL, on an otherwise-identical account: with NO pooled file
        # the same call falls back to the home token, so the assertion above
        # can only pass through the pool lookup and not through a constant.
        shutil.rmtree(self.pool("acct-1", "x"), ignore_errors=True)
        get2 = mock.Mock(return_value=recorded("team"))
        with mock.patch.object(self.p, "_get_json", get2):
            cred2, _ = self.p._probe_one(self.acct(home))
        self.assertEqual(get2.call_args[0][1]["Authorization"],
                         "Bearer HOME-TOKEN-39-DAYS-OLD")
        self.assertIn("NOT pooled", cred2["note"] or "")

    def test_headroom_and_reset_bind_to_the_WEEKLY_window(self):
        home = self.codex_home("cxw", "pool@x.example", token="HOME")
        self.pool("acct-1", "POOL-TOKEN")
        with mock.patch.object(self.p, "_get_json",
                               mock.Mock(return_value=recorded("team"))):
            cred, _ = self.p._probe_one(self.acct(home))
        self.assertEqual(cred["headroom_pct"], 0.0)
        self.assertEqual(cred["resets_at_ms"], 1789861924 * 1000)
        self.assertEqual([w["label"] for w in cred["windows"]], ["7d", "5h"])
        # THE CONTROL THAT NAMES WHAT CHANGED: the OLD binding rule, applied to
        # the very same gauges, still picks the 5h window and its 48% headroom
        # — the reading the owner saw while every pooled account was capped.
        gauges = NativeQuotaProvider._codex_gauges(recorded("team"))
        self.assertEqual(NativeQuotaProvider._primary(gauges)["label"], "5h")
        self.assertEqual(NativeQuotaProvider._primary(gauges)["utilization"], 0.52)

    def test_a_pool_401_is_unknown_and_names_where_it_read(self):
        home = self.codex_home("cx401", "pool@x.example", token="HOME")
        self.pool("acct-1", "POOL-TOKEN")
        err = urllib.error.HTTPError("http://x", 401, "no", None, io.BytesIO(b""))
        with mock.patch.object(self.p, "_get_json", mock.Mock(side_effect=err)):
            cred, _ = self.p._probe_one(self.acct(home))
        self.assertEqual((cred["cred_state"], cred["status"]),
                         ("unknown", "needs_reauth"))
        self.assertIn("the proxy pool", cred["note"])
        self.assertIsNone(cred["headroom_pct"])


class CodexTeamMembersReadTheirOwnPoolFileTest(NativeBase):
    """task/2981: `helm creds` for the members of ONE Team workspace.

    On a ChatGPT Team plan the account id is the WORKSPACE id, and every
    member shares it. The creds join matched a home to the pool on that id
    first. So every member's row was served by the member file the census
    listed first. Measured: admin@, d@ and hen@ all showed one identical
    57% headroom and one reset, while hen had walled.

    Every arm plants REAL files (codex homes and pool records, with fake
    tokens and fake addresses) and drives the shipped `_probe_one`. The fake
    vendor answers PER BEARER TOKEN. So a row can carry a member's numbers
    only if it sent that member's pooled token."""

    WORKSPACE = "ws-fake-0001"
    # name: (email, user id, weekly used %, weekly reset epoch)
    MEMBERS = {"admin": ("admin@team.example", "user-fake-admin", 26, 1790700000),
               "d": ("d@team.example", "user-fake-d", 60, 1790600000),
               "hen": ("hen@team.example", "user-fake-hen", 95, 1790500000)}
    SOLO = ("solo@personal.example", "user-fake-solo", 40, 1790400000)

    def setUp(self):
        super().setUp()
        self.helmp = mock.patch.dict(
            os.environ, {"HELM_HOME": os.path.join(self.tmp, ".helm"),
                         "HELM_CACHE_DIR": os.path.join(self.tmp, "cache")})
        self.helmp.start()
        self.addCleanup(self.helmp.stop)
        from helm import codexbudget, codexhomes
        self.budget, self.pool_dir = codexbudget, codexhomes.pool_dir()
        os.makedirs(self.pool_dir, exist_ok=True)
        self.who = dict(self.MEMBERS, solo=self.SOLO)

    @staticmethod
    def id_token(email, user_id, plan):
        auth = {"chatgpt_plan_type": plan}
        if user_id:
            auth["chatgpt_user_id"] = user_id
        claims = {"https://api.openai.com/auth": auth}
        if email:
            claims["email"] = email
        seg = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode()
        return "h." + seg.rstrip("=") + ".sig"

    def pool(self, name, account_id=None, plan="team"):
        email, uid, _pct, _reset = self.who[name]
        path = os.path.join(self.pool_dir, "codex-%s-%s.json" % (email, plan))
        with open(path, "w") as f:
            json.dump({"type": "codex", "email": email,
                       "account_id": account_id or self.WORKSPACE,
                       "id_token": self.id_token(email, uid, plan),
                       "access_token": "POOL-TOKEN-" + name,
                       "disabled": False}, f)
        return path

    def home(self, name, email=None, user_id=True, account_id=None,
             plan="team"):
        """A codex home for `name`. `email` overrides the address the home
        carries ("" = no address claim at all)."""
        mail, uid, _pct, _reset = self.who[name]
        mail = mail if email is None else email
        d = os.path.join(self.tmp, ".codex-homes", "cx-" + name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "auth.json"), "w") as f:
            json.dump({"tokens": {
                "access_token": "HOME-TOKEN-" + name,
                "account_id": account_id or self.WORKSPACE,
                "id_token": self.id_token(mail, uid if user_id else None,
                                          plan)}}, f)
        return {"name": "cx-" + name, "provider": "codex", "home": d,
                "tier": None, "email": mail or None}

    def body(self, name):
        """The RECORDED team body, stamped as this member's: its own address
        and its own weekly window. The 5h window is kept low, so the weekly
        window is every member's binding window."""
        email, _uid, pct, reset = self.who[name]
        data = recorded("team")
        data["email"] = email
        data["rate_limit_reached_type"] = None
        rl = data["rate_limit"]
        rl.update(allowed=True, limit_reached=False)
        rl["primary_window"]["used_percent"] = 10
        rl["secondary_window"].update(used_percent=pct, reset_at=reset)
        return data

    def vendor(self):
        by_token = {"Bearer POOL-TOKEN-" + n: n for n in self.who}
        return mock.Mock(side_effect=lambda url, headers:
                         self.body(by_token[headers["Authorization"]]))

    def creds_row(self, acct):
        get = self.vendor()
        with mock.patch.object(self.p, "_get_json", get):
            cred, _ = self.p._probe_one(acct)
        return cred, [c[0][1]["Authorization"] for c in get.call_args_list]

    def test_three_members_of_one_workspace_each_read_their_OWN_file(self):  # noqa: VACUOUS_ASSERTION — the absent note sits beside two unconditional positive controls on the same row, the member's own Bearer token sent and its own headroom and reset
        for name in self.MEMBERS:
            self.pool(name)
        # d@'s home carries NO user-id claim, so its row is joined on account
        # id AND email. admin@ and hen@ are joined on the member key.
        homes = {"admin": self.home("admin"),
                 "d": self.home("d", user_id=False),
                 "hen": self.home("hen")}
        for name, acct in homes.items():
            with self.subTest(member=name):
                cred, sent = self.creds_row(acct)
                self.assertEqual(sent, ["Bearer POOL-TOKEN-" + name])
                _email, _uid, pct, reset = self.MEMBERS[name]
                self.assertEqual(cred["headroom_pct"], 100.0 - pct)
                self.assertEqual(cred["resets_at_ms"], reset * 1000)
                self.assertIsNone(cred.get("note"))

    def test_the_join_names_the_member_and_never_the_workspace(self):
        """The same law at `pool_record_for` itself. Both member spellings
        resolve to the member's own file. The workspace id ALONE resolves to
        nobody and says so."""
        files = {name: os.path.basename(self.pool(name))
                 for name in self.MEMBERS}
        for name, (email, uid, _pct, _reset) in self.MEMBERS.items():
            with self.subTest(member=name):
                by_key, note = self.budget.pool_record_for(
                    account_id=self.WORKSPACE, user_id=uid)
                self.assertEqual((by_key["file"], note), (files[name], None))
                by_mail, note = self.budget.pool_record_for(
                    account_id=self.WORKSPACE, email=email)
                self.assertEqual((by_mail["file"], note), (files[name], None))
        match = self.budget.pool_record_for(account_id=self.WORKSPACE)
        self.assertEqual(match[0], None)
        self.assertTrue(match.unknown)
        self.assertIn("WORKSPACE", match[1])

    def test_a_member_with_NO_file_reads_UNKNOWN_not_a_siblings_numbers(self):  # noqa: VACUOUS_ASSERTION — the absent headroom, reset and vendor call are the product law, and the control at the end drives the SAME home after pooling its file and asserts the token sent and a 5.0 headroom
        self.pool("admin")
        d_file = self.pool("d")
        acct = self.home("hen")
        for siblings in ("admin and d", "admin alone"):
            if siblings == "admin alone":
                # with ONE sibling left, the pool no longer shows the id
                # shared. The record's own team plan names it a workspace.
                os.remove(d_file)
            with self.subTest(pooled=siblings):
                cred, sent = self.creds_row(acct)
                self.assertEqual((cred["cred_state"], cred["status"]),
                                 ("unknown", "pool-member-unknown"))
                self.assertIsNone(cred["headroom_pct"])
                self.assertIsNone(cred["resets_at_ms"])
                # no sibling's token was sent, and neither was the home's
                # own copy: the pool may hold this member under a spelling
                # the join could not prove, and the home copy is the stale one
                self.assertEqual(sent, [])
                self.assertIn("hen@team.example", cred["note"])
                self.assertIn("WORKSPACE", cred["note"])
        # THE CONTROL, on the same home: pool hen's own file, and the same
        # call reads it. So the UNKNOWN above comes from the missing member,
        # not from a home the join can never resolve.
        self.pool("hen")
        cred, sent = self.creds_row(acct)
        self.assertEqual(sent, ["Bearer POOL-TOKEN-hen"])
        self.assertEqual(cred["headroom_pct"], 5.0)

    def test_a_personal_account_is_read_exactly_as_before(self):  # noqa: VACUOUS_ASSERTION — the absent note sits beside two unconditional positive controls on the same row, the pooled Bearer token sent and a 60.0 headroom
        """A personal plan's account id names ONE person. So the id alone
        still serves, including the two shapes the member rule would refuse
        on a workspace: a home whose address is spelled differently, and a
        home with no identity claims at all."""
        self.pool("solo", account_id="acct-fake-solo", plan="pro")
        for label, kw in (("same address", {}),
                          ("renamed address, no user id",
                           {"email": "solo.old@personal.example",
                            "user_id": False}),
                          ("no identity claims", {"email": "",
                                                  "user_id": False})):
            with self.subTest(home=label):
                cred, sent = self.creds_row(self.home(
                    "solo", account_id="acct-fake-solo", plan="pro", **kw))
                self.assertEqual(sent, ["Bearer POOL-TOKEN-solo"])
                self.assertEqual(cred["headroom_pct"], 60.0)
                self.assertIsNone(cred.get("note"))

    def test_the_creds_row_for_hey_EQUALS_the_proxywatch_budget_row(self):  # noqa: VACUOUS_ASSERTION — every other assertion is an equality against the budget row, and the one inequality is against admin's row, whose own 26% weekly window is asserted first
        """ACCEPTANCE (task/2981). `codexbudget.pool_budget` is the reader
        proxywatch runs on every pass. It reads per FILE and was right all
        along. The owner's creds card must agree with that reader's row for
        hen on every number the card prints."""
        for name in self.MEMBERS:
            self.pool(name)
        budget = self.budget.pool_budget(get_json=self.vendor(),
                                         write_cache=False)
        by_email = {r["email"]: r for r in budget}
        row = by_email["hen@team.example"]
        cred, _sent = self.creds_row(self.home("hen"))
        gauge = row["binding_gauge"]
        self.assertEqual(cred["headroom_pct"],
                         round(100 - gauge["utilization"] * 100, 1))
        self.assertEqual(cred["resets_at_ms"], gauge["reset"] * 1000)

        def shape(windows):
            return [(w["label"], w["used_percent"], w["reset_at"])
                    for w in windows]
        self.assertEqual(shape(cred["windows"]), shape(row["windows"]))
        # and NOT admin's row, which is what the card printed for hen
        admin = shape(by_email["admin@team.example"]["windows"])
        self.assertEqual(admin[0][:2], ("7d", 26.0))
        self.assertNotEqual(shape(cred["windows"]), admin)

    def clear_pool(self):
        for fname in os.listdir(self.pool_dir):
            os.remove(os.path.join(self.pool_dir, fname))

    def test_an_UNRECOGNISED_plan_never_lends_a_siblings_file(self):  # noqa: VACUOUS_ASSERTION — the absent vendor call sits beside the UNKNOWN status asserted on the same row, and the control pools hen's own file and asserts its Bearer token sent
        """Only a plan helm KNOWS is personal names one person. A Team-like
        plan helm has no word for (business, enterprise, edu) is a possible
        workspace, so its id alone proves no member. hen's home carries no
        user-id claim, and its address is not admin's."""
        for plan in ("business", "enterprise", "edu"):
            with self.subTest(plan=plan):
                self.clear_pool()
                self.pool("admin", plan=plan)
                acct = self.home("hen", user_id=False, plan=plan)
                cred, sent = self.creds_row(acct)
                self.assertEqual(sent, [])
                self.assertEqual((cred["cred_state"], cred["status"]),
                                 ("unknown", "pool-member-unknown"))
                match = self.budget.pool_record_for(
                    account_id=self.WORKSPACE, email="hen@team.example")
                self.assertEqual((match[0], match.unknown), (None, True))
                # THE CONTROL: pool hen's own file, and the same home reads it
                self.pool("hen", plan=plan)
                cred, sent = self.creds_row(acct)
                self.assertEqual(sent, ["Bearer POOL-TOKEN-hen"])
                self.assertEqual(cred["headroom_pct"], 5.0)

    def test_an_UNREADABLE_plan_with_no_address_reads_UNKNOWN(self):  # noqa: VACUOUS_ASSERTION — the absent vendor call sits beside the UNKNOWN status on the same row, and the personal control on the same home shape asserts its Bearer token sent
        """A pooled file whose plan will not read may be a workspace. A home
        with no address and no user id proves no member of it. So the row is
        UNKNOWN, and admin's file is never read as this home's."""
        self.pool("admin", plan=None)
        acct = self.home("hen", email="", user_id=False, plan=None)
        cred, sent = self.creds_row(acct)
        self.assertEqual(sent, [])
        self.assertEqual((cred["cred_state"], cred["status"]),
                         ("unknown", "pool-member-unknown"))
        match = self.budget.pool_record_for(account_id=self.WORKSPACE)
        self.assertEqual((match[0], match.unknown), (None, True))
        # THE CONTROL: the same home shape on a KNOWN personal plan still
        # reads its pooled file on the id alone
        self.pool("solo", account_id="acct-fake-solo", plan="pro")
        cred, sent = self.creds_row(self.home(
            "solo", email="", user_id=False, account_id="acct-fake-solo",
            plan="pro"))
        self.assertEqual(sent, ["Bearer POOL-TOKEN-solo"])
        self.assertEqual(cred["headroom_pct"], 60.0)

    def test_the_address_match_ignores_case_in_BOTH_joins(self):
        """One address rule for the member join and the email fallback:
        casefolded, and a different address never matches."""
        hen = os.path.basename(self.pool("hen"))
        solo = os.path.basename(self.pool("solo", account_id="acct-fake-solo",
                                          plan="pro"))
        for spelling in ("HEN@Team.Example", "hen@team.example"):
            with self.subTest(spelling=spelling):
                by_member = self.budget.pool_record_for(
                    account_id=self.WORKSPACE, email=spelling)
                self.assertEqual((by_member[0]["file"], by_member[1]),
                                 (hen, None))
                by_mail = self.budget.pool_record_for(email=spelling)
                self.assertEqual((by_mail[0]["file"], by_mail[1]), (hen, None))
        self.assertEqual(self.budget.pool_record_for(
            email="Solo@Personal.Example")[0]["file"], solo)
        # a different address never matches, by either join
        self.assertEqual(self.budget.pool_record_for(
            email="HAY@team.example"), (None, None))
        miss = self.budget.pool_record_for(account_id=self.WORKSPACE,
                                           email="HAY@team.example")
        self.assertEqual((miss[0], miss.unknown), (None, True))


    def test_a_match_survives_copy_deepcopy_and_pickle_whole(self):  # noqa: VACUOUS_ASSERTION — every copy is compared by equality to an original whose hit record and UNKNOWN flag are asserted unconditionally first, so an empty pair cannot pass
        """`PoolMatch` is a pair with `unknown` riding beside it. Copied or
        pickled, it came back as ((record, note), None): the pair folded into
        the record slot, so a copied UNKNOWN read as a hit whose record is a
        tuple. Each path must return the same pair and the same flag."""
        import copy
        import pickle
        self.pool("hen")
        hit = self.budget.pool_record_for(account_id=self.WORKSPACE,
                                          email="hen@team.example")
        unknown = self.budget.pool_record_for(account_id=self.WORKSPACE)
        # the control: the two originals really are a hit and an UNKNOWN
        self.assertEqual((hit[0]["email"], hit.unknown),
                         ("hen@team.example", False))
        self.assertEqual((unknown[0], unknown.unknown), (None, True))
        for name, dup in (("copy", copy.copy), ("deepcopy", copy.deepcopy),
                          ("pickle", lambda m: pickle.loads(pickle.dumps(m))),
                          ("pickle-0", lambda m: pickle.loads(
                              pickle.dumps(m, protocol=0)))):
            for label, match in (("hit", hit), ("unknown", unknown)):
                with self.subTest(path=name, match=label):
                    got = dup(match)
                    self.assertIsInstance(got, self.budget.PoolMatch)
                    self.assertEqual(tuple(got), tuple(match))
                    self.assertIs(got.unknown, match.unknown)

class AnUnreadablePoolIsOneAccountsUnknownTest(NativeBase):
    """task/2480 R5 F1, at the two doors the owner actually reads.

    Round 3 made the pool reader RAISE on any enumeration OSError but
    FileNotFoundError. `_probe_one` documents "never raises; failure degrades
    the row", and `cred_state` depends on that literally: it fans the probes
    out through `concurrent.futures` and collects them at `f.result()`, which
    RETHROWS. So one unreadable directory, belonging to one family, destroyed
    the whole scorecard — every healthy Anthropic sibling's row with it — and
    `helm creds`, whose only OSError handling is `except ProviderError`,
    printed a traceback.

    THE FAULT IS REAL AND UID-INDEPENDENT: the pool directory is replaced by a
    regular FILE, so `os.listdir` raises NotADirectoryError for root and
    non-root alike. Nothing about the reader is mocked; the only faked seam is
    the VENDOR, whose codex body is the one recorded from the live endpoint."""

    def setUp(self):
        super().setUp()
        self.helmp = mock.patch.dict(
            os.environ, {"HELM_HOME": os.path.join(self.tmp, ".helm"),
                         "HELM_CACHE_DIR": os.path.join(self.tmp, "cache")})
        self.helmp.start()
        self.addCleanup(self.helmp.stop)
        from helm import codexhomes
        self.homes = codexhomes
        self.pool = codexhomes.pool_dir()
        os.makedirs(self.pool, exist_ok=True)
        # A: a readable, usable codex home, pooled. B: a healthy Anthropic
        # sibling that has nothing to do with the codex pool.
        self.a_home = self.codex_home("cxA", "a@x.example", token="HOME-TOKEN")
        self.claude_home("clB", "b@x.example", rl="max_5x")
        self.anthropic_body = {"limits": [
            {"kind": "session", "percent": 10, "resets_at": None},
            {"kind": "weekly_all", "percent": 5, "resets_at": None}]}

    def _pool_a(self):
        with open(os.path.join(self.pool, "codex-A.json"), "w") as f:
            json.dump({"type": "codex", "email": "a@x.example",
                       "account_id": "acct-1", "access_token": "POOL-TOKEN",
                       "disabled": False}, f)

    def _break_the_pool(self):
        shutil.rmtree(self.pool)
        with open(self.pool, "w") as f:
            f.write("not a directory")
        with self.assertRaises(OSError):   # the fixture's own premise
            os.listdir(self.pool)

    def _vendor(self, url, _headers):
        return recorded("team") if "wham" in url else self.anthropic_body

    def _scorecard(self):
        self.p._cred_cache = None
        with mock.patch.object(self.p, "_get_json", self._vendor), \
                mock.patch.object(providers.time, "sleep"):
            return {r["account"]: r for r in self.p.cred_state()}

    def test_the_unreadable_pool_degrades_A_and_leaves_B_untouched(self):
        # THE CONTROL COMES FIRST, through the same doors: with the pool
        # readable, A probes the POOLED token and reads exhausted. Blast
        # radius: one extra scorecard over the same two homes — it fails only
        # if this fixture can never produce a measured codex row at all, which
        # is exactly what would make the UNKNOWN below worthless.
        self._pool_a()
        before = self._scorecard()
        self.assertEqual(before["cxA"]["cred_state"], "exhausted")
        self.assertEqual(before["b@x.example"]["cred_state"], "ok")

        self._break_the_pool()
        rows = self._scorecard()
        # A is UNKNOWN, and the row NAMES the directory that could not be read
        self.assertEqual((rows["cxA"]["cred_state"], rows["cxA"]["status"]),
                         ("unknown", "pool-unread"))
        self.assertIsNone(rows["cxA"]["headroom_pct"])
        self.assertIn(self.pool, rows["cxA"]["note"])
        self.assertIn("UNKNOWN", rows["cxA"]["note"])
        # THE SIBLING SURVIVES — this is the whole finding. Its row is
        # byte-identical to the one the readable pool produced.
        self.assertEqual(rows["b@x.example"]["cred_state"], "ok")
        self.assertEqual(rows["b@x.example"]["headroom_pct"],
                         before["b@x.example"]["headroom_pct"])
        # and the HOME's stale token is NOT quietly probed instead: an
        # uncertain diagnostic dressed as a reading is worse than the
        # traceback it replaced.
        self.assertNotIn("HOME-TOKEN", rows["cxA"]["note"])

    def test_helm_creds_prints_an_UNKNOWN_line_and_the_reason(self):
        """The owner's door. `creds` catches ProviderError and nothing else,
        so an OSError out of the pool reader reached the terminal as a
        traceback with no table at all."""
        from helm import creds as creds_mod
        self._pool_a()
        self._break_the_pool()
        self.p._cred_cache = None
        buf = io.StringIO()
        with mock.patch.object(creds_mod, "default_provider", lambda: self.p), \
                mock.patch.object(self.p, "_get_json", self._vendor), \
                mock.patch.object(providers.time, "sleep"), \
                contextlib.redirect_stdout(buf):
            rc = creds_mod.cmd_creds([])
        text = buf.getvalue()
        self.assertEqual(rc, 0)                    # a table, not a traceback
        self.assertIn("helm creds (2 accounts):", text)
        a_line = next(l for l in text.splitlines() if l.split()[1:2] == ["cxA"])
        self.assertIn("unknown", a_line)
        b_line = next(l for l in text.splitlines()
                      if l.split()[1:2] == ["b@x.example"])
        self.assertIn("ok", b_line)                # the sibling still renders
        self.assertIn(self.pool, text)             # the REASON is printed
        # THE CONTROL on the same rendering: with the pool readable the same
        # command prints A's measured state and no pool note at all. Blast
        # radius: one more cmd_creds over the same two homes.
        os.remove(self.pool)
        os.makedirs(self.pool)
        self._pool_a()
        self.p._cred_cache = None
        buf2 = io.StringIO()
        with mock.patch.object(creds_mod, "default_provider", lambda: self.p), \
                mock.patch.object(self.p, "_get_json", self._vendor), \
                mock.patch.object(providers.time, "sleep"), \
                contextlib.redirect_stdout(buf2):
            creds_mod.cmd_creds([])
        self.assertIn("exhausted", buf2.getvalue())
        self.assertNotIn("would not enumerate", buf2.getvalue())

    def test_an_EMPTY_pool_is_still_the_ordinary_home_fallback(self):
        """The other control, and the one a heavy-handed cure breaks: an
        empty pool ENUMERATED, so it is a measurement — this account is not
        pooled — and the home fallback is exactly right for it. Only an
        UNREAD pool refuses to read anything."""
        rows = self._scorecard()            # pool dir exists and is empty
        self.assertEqual(rows["cxA"]["cred_state"], "exhausted")
        self.assertIn("NOT pooled", rows["cxA"]["note"])


class AnUnreadPoolProbesNothingAndPublishesNothingTest(unittest.TestCase):
    """task/2480 R5 F1 at `pool_budget`, the function that WRITES the snapshot
    the dispatch gate reads.

    `pool_accounts` sees ROWS and never the reading's kind, so an unread
    census — which carries no records — looks exactly like an empty pool to
    it. Before the cure the raise was what stopped `pool_budget` publishing
    `[]` over a standing all-capped snapshot; with the raise gone, the census
    check has to be the thing that stops it, or the original defect is back
    with a quieter mechanism."""

    def setUp(self):
        from helm import codexbudget, codexhomes
        self.mod, self.homes = codexbudget, codexhomes
        self.tmp = tempfile.mkdtemp(prefix="helm-test-poolbudget-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.envp = mock.patch.dict(
            os.environ, {"HOME": self.tmp,
                         "HELM_HOME": os.path.join(self.tmp, "helm"),
                         "HELM_CACHE_DIR": os.path.join(self.tmp, "cache")})
        self.envp.start()
        self.addCleanup(self.envp.stop)
        os.environ.pop("HELM_CODEX_WEEKLY_CEILING_PCT", None)
        self.pool = self.homes.pool_dir()
        os.makedirs(self.pool, exist_ok=True)

    def _pooled(self):
        with open(os.path.join(self.pool, "codex-a.json"), "w") as f:
            json.dump({"type": "codex", "email": "a@x.example",
                       "account_id": "acct-A",
                       "access_token": "FAKE-pool-token"}, f)

    def test_an_unread_pool_writes_no_snapshot_at_all(self):  # noqa: VACUOUS_ASSERTION — the empty rows list and the zero probe count ARE the product law (an unread pool probes nothing), and the positive control runs FIRST and unconditionally on the same observables: the populated pass is asserted to return the real row, to create the snapshot file, and the snapshot's mtime read there is what the closing assertion proves UNCHANGED
        # THE POSITIVE CONTROL FIRST, on the same call: a populated pool DOES
        # probe and DOES publish. Blast radius: one budget pass over one real
        # file with the vendor body recorded — it goes red only if this
        # function can no longer write, which is what would make the silence
        # below meaningless.
        self._pooled()
        rows = self.mod.pool_budget(get_json=lambda u, h: recorded("team"))
        self.assertEqual([r["file"] for r in rows], ["codex-a.json"])
        self.assertTrue(os.path.exists(self.mod.snapshot_path()))
        stamp = os.stat(self.mod.snapshot_path()).st_mtime_ns

        shutil.rmtree(self.pool)
        with open(self.pool, "w") as f:      # unenumerable, uid-independently
            f.write("not a directory")
        probe = mock.Mock(side_effect=AssertionError("probed an unread pool"))
        self.assertEqual(self.mod.pool_budget(get_json=probe), [])
        self.assertEqual(probe.call_count, 0)
        self.assertEqual(os.stat(self.mod.snapshot_path()).st_mtime_ns, stamp,
                         "an unread pool overwrote the standing snapshot")
