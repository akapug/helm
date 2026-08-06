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
        self.assertEqual(providers._jwt_email(fake_jwt("a@b.c")), "a@b.c")
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
        self.envp = mock.patch.dict(os.environ, {"HOME": self.tmp})
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
        a = self.claude_home("a-home", "u@x.com", rl="max_5x")
        self.claude_home("bare", authed=False)  # no identity, no cred -> unusable
        os.symlink(a, os.path.join(self.tmp, ".claude-homes", "alias"))
        self.codex_home("cx", "c@x.com")
        d = os.path.join(self.tmp, ".claude")  # an authed default home
        os.makedirs(d)
        with open(os.path.join(d, ".credentials.json"), "w") as f:
            json.dump({"claudeAiOauth": {"accessToken": "FAKE"}}, f)
        rows = {r["name"]: r for r in self.p.accounts()}
        self.assertEqual(set(rows), {"u@x.com", "bare", "cx", "(default-claude)"})
        u = rows["u@x.com"]
        self.assertEqual((u["provider"], u["tier"], u["usable"], u["active"]),
                         ("anthropic", "Max 5x", True, False))
        self.assertFalse(rows["bare"]["usable"])
        self.assertEqual(rows["cx"]["email"], "c@x.com")
        self.assertTrue(rows["(default-claude)"]["usable"])

    def test_same_identity_two_homes_disambiguated_never_dropped(self):
        self.claude_home("one", "same@x.com")
        self.claude_home("two", "same@x.com")
        names = sorted(r["name"] for r in self.p.accounts())
        self.assertEqual(names, ["same@x.com", "same@x.com#two"])

    def test_tier_branches(self):
        cases = ((dict(rl="max_20x"), "Max 20x"), (dict(org="claude_team"), "Team"),
                 (dict(org="claude_pro"), "Pro"), (dict(org="claude_enterprise"), "Enterprise"),
                 (dict(org="claude_max"), "Max"), (dict(), None))
        for i, (kw, want) in enumerate(cases):
            home = self.claude_home("t%d" % i, "t%d@x.com" % i, **kw)
            self.assertEqual(self.p._anthropic_identity(home)[1], want, kw)


class ProbeTest(NativeBase):
    def probe(self, acct, response=None, error=None):
        fn = mock.Mock(side_effect=error) if error else mock.Mock(return_value=response)
        with mock.patch.object(self.p, "_get_json", fn):
            return self.p._probe_one(acct), fn

    def acct(self, home, provider="anthropic", name="u@x.com"):
        return {"name": name, "provider": provider, "home": home, "tier": None}

    def http_error(self, code):
        return urllib.error.HTTPError("http://x", code, "boom", None, io.BytesIO(b""))

    def _set_expiry(self, home, expires_at_ms):
        p = os.path.join(home, ".credentials.json")
        creds = json.load(open(p))
        creds["claudeAiOauth"]["expiresAt"] = expires_at_ms
        json.dump(creds, open(p, "w"))

    def test_expired_token_is_its_own_state_not_api_error(self):  # noqa: VACUOUS_ASSERTION — three positive assertions (state==expired-token, status contains reauth-needed + 15d); the fn.assert_not_called() absence has its unconditional positive control in the sibling test_live_token_still_probes_the_network (fn.assert_called_once)
        """A present-but-EXPIRED token is its own state and never touches the
        network — rows that previously read as 'api-error' were tokens dead
        for 13-17 days."""
        import time as _t
        home = self.claude_home("stale", "u@x.com")
        self._set_expiry(home, int((_t.time() - 15 * 86400) * 1000))
        (cred, _hist), fn = self.probe(self.acct(home), {"limits": []})
        self.assertEqual(cred["cred_state"], "expired-token")
        self.assertIn("reauth-needed", cred["status"])
        self.assertIn("15d", cred["status"])
        fn.assert_not_called()          # a dead token never pays for a 401

    def test_live_token_still_probes_the_network(self):
        """The polarity control: a token whose expiry is in the FUTURE (or
        absent) must reach the live call, so the stale check cannot swallow a
        healthy account into a false expired-token."""
        import time as _t
        home = self.claude_home("live", "u@x.com")
        self._set_expiry(home, int((_t.time() + 3600) * 1000))
        data = {"limits": [{"kind": "session", "percent": 10,
                            "resets_at": "1970-01-01T00:01:00Z"}]}
        (cred, _), fn = self.probe(self.acct(home), data)
        self.assertEqual(cred["cred_state"], "ok")
        fn.assert_called_once()
        # and expiry ABSENT (the fixture default) also reaches the network
        home2 = self.claude_home("noexp", "v@x.com")
        (cred2, _), fn2 = self.probe(self.acct(home2), data)
        self.assertEqual(cred2["cred_state"], "ok")
        fn2.assert_called_once()

    def test_anthropic_ok_and_exhausted(self):
        home = self.claude_home("a", "u@x.com")
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

    def test_anthropic_no_credentials_and_http_errors(self):
        home = self.claude_home("bare2", authed=False)
        (cred, _), _ = self.probe(self.acct(home))
        self.assertEqual((cred["cred_state"], cred["status"]),
                         ("api-error", "no-credentials"))
        home = self.claude_home("a2", "u@x.com")
        (cred, _), _ = self.probe(self.acct(home), error=self.http_error(401))
        self.assertEqual((cred["cred_state"], cred["status"]),
                         ("api-error", "needs_reauth"))
        (cred, _), _ = self.probe(self.acct(home), error=self.http_error(503))
        self.assertEqual(cred["status"], "http_503")
        (cred, _), _ = self.probe(self.acct(home), error=OSError("down"))
        self.assertEqual(cred["status"], "network-error")

    def test_codex_headers_gauges_tier_and_degrades_to_unknown(self):
        home = self.codex_home("cx2", "c@x.com")
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
        self.claude_home("one", "same@x.com")
        self.claude_home("two", "same@x.com")
        gauges = [{"label": "5h", "kind": "session", "utilization": 0.2, "reset": None}]
        probe = mock.Mock(return_value=(
            {"account": "same@x.com", "provider": "anthropic", "cred_state": "ok",
             "headroom_pct": 80.0, "status": "allowed", "resets_at_ms": None,
             "tier": None, "home": "h", "source_at": None},
            {"provider": "anthropic", "account": "same@x.com",
             "probed_at": providers._iso_z(), "status": "allowed", "primary": "5h",
             "gauges": gauges, "source_at": None}))
        with mock.patch.object(self.p, "_probe_one", probe), \
                mock.patch.object(providers.time, "sleep"):
            rows = self.p.cred_state()
        self.assertEqual(probe.call_count, 1)      # ONE probe for the identity
        self.assertEqual(len(rows), 2)             # a row for EACH home
        self.assertEqual({r["account"] for r in rows},
                         {"same@x.com", "same@x.com#two"})
        with open(self.hist) as f:
            self.assertEqual(len(f.readlines()), 1)  # one history point, not two
        # TTL cache: a second call returns copies without re-probing
        with mock.patch.object(self.p, "_probe_one", probe):
            rows2 = self.p.cred_state()
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(len(rows2), 2)

    def test_throttled_probe_serves_last_observation_marked_stale(self):
        self.claude_home("solo", "solo@x.com")
        seen_at = providers._iso_z(time.time() - 60)
        with open(self.hist, "w") as f:
            f.write(json.dumps({"account": "solo@x.com", "provider": "anthropic",
                                "probed_at": seen_at, "gauges": [
                                    {"label": "5h", "kind": "session",
                                     "utilization": 0.25, "reset": 99}]}) + "\n")
        probe = mock.Mock(return_value=(
            {"account": "solo@x.com", "provider": "anthropic", "cred_state": "api-error",
             "headroom_pct": None, "status": "http_429", "resets_at_ms": None,
             "tier": None, "home": "h", "source_at": None},
            {"provider": "anthropic", "account": "solo@x.com",
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


class LaunchPreflightTest(NativeBase):
    def test_launch_cmd_shapes_and_errors(self):
        self.claude_home("cl", "u@x.com")
        cx = self.codex_home("cx3", "c@x.com")
        with mock.patch.object(self.p, "_active_homes", lambda: set()):
            cmd = self.p.launch_cmd("u@x.com", "sid-1", model="fable")
            self.assertIn("CLAUDE_CONFIG_DIR=", cmd)
            self.assertIn("claude --model fable --resume sid-1", cmd)
            cmd = self.p.launch_cmd("cx3", "sid-2")
            self.assertEqual(cmd, "CODEX_HOME=%s codex resume sid-2" % cx)
            with self.assertRaises(ProviderError):
                self.p.launch_cmd("ghost", "sid")
        self.claude_home("noauth", "n@x.com", authed=False)
        with self.assertRaises(ProviderError):
            self.p.launch_cmd("n@x.com", "sid")

    def test_preflight_found_open_and_missing(self):
        self.claude_home("cl2", "u@x.com")
        store = os.path.join(self.tmp, ".claude", "projects", "-work-x")
        os.makedirs(store)
        sid = "cafe0000-1111-2222-3333-444444444444"
        with open(os.path.join(store, sid + ".jsonl"), "w") as f:
            f.write(json.dumps({"cwd": "/work/x"}) + "\n")
        quiet = mock.Mock(return_value=mock.Mock(stdout="", returncode=1))
        with mock.patch.object(providers.subprocess, "run", quiet):
            res = self.p.preflight("u@x.com", sid, "claude")
        self.assertTrue(res["resolvable"])
        self.assertEqual(res["true_cwd"], "/work/x")
        self.assertIsNone(res["reason"])
        holder = mock.Mock(return_value=mock.Mock(
            stdout="4242 claude --resume " + sid, returncode=0))
        with mock.patch.object(providers.subprocess, "run", holder):
            res = self.p.preflight("u@x.com", sid, "claude")
        self.assertEqual(res["live_holder_pid"], 4242)
        self.assertIn("double-open", res["reason"])
        with mock.patch.object(providers.subprocess, "run", quiet):
            res = self.p.preflight("u@x.com", "no-such-sid", "claude")
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
