"""helm.web quota-surface contract tests (slice A: providers + quota endpoints
+ tabbed UI). Hermetic: ephemeral-port server over a tmp HELM_HOME, a STUB
provider injected in place of the native one (no vendor probes, no history
writes), homes roots repointed at tmp dirs — the real ~/.helm, ~/.claude and
~/.cache are never mutated."""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import home, homes, pk, transcripts, web  # noqa: E402
from helm.providers import ProviderError  # noqa: E402

ACCT = "alice@example.com"


def _iso_z(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class StubProvider:
    """Canned provider rows in the exact shapes providers.py documents."""

    def __init__(self):
        self.home = "/fake/homes/alice-example-com"

    def accounts(self):
        return [{"name": ACCT, "provider": "anthropic", "home": self.home,
                 "active": False, "tier": "Max 20x", "usable": True, "email": ACCT}]

    def cred_state(self):
        return [{"account": ACCT, "provider": "anthropic", "cred_state": "ok",
                 "headroom_pct": 72.5, "status": "allowed",
                 "resets_at_ms": int((time.time() + 3600) * 1000),
                 "tier": "Max 20x", "home": self.home, "source_at": None}]

    def windows(self):
        return [{"account": ACCT, "provider": "anthropic", "assumed": True,
                 "cost_per_window": 0.125, "cycles": 3, "windows_per_week": 8.0,
                 "windows_left": 4.0, "windows_fit": 20.0, "verdict": "ok"}]

    def history(self, hours):
        now = time.time()
        gauge = lambda util: [{"label": "5h", "kind": "session",
                               "utilization": util, "reset": None,
                               "limit": None, "remaining": None}]
        return [{"provider": "anthropic", "account": ACCT,
                 "probed_at": _iso_z(now - 2 * 3600), "status": "allowed",
                 "primary": "5h", "gauges": gauge(0.4), "source_at": None},
                {"provider": "anthropic", "account": ACCT,
                 "probed_at": _iso_z(now - 1 * 3600), "status": "allowed",
                 "primary": "5h", "gauges": gauge(0.6), "source_at": None}]

    def allocate(self, model):
        return [{"account": ACCT, "eligible": True, "headroom_pct": 72.5,
                 "tier": "Max 20x", "home": self.home, "blocked_by": [],
                 "why": "headroom"}]


class BrokenProvider:
    """Every verb raises ProviderError — the no-provider machine."""

    def __getattr__(self, name):
        def boom(*a, **k):
            raise ProviderError("no quota provider on this machine")
        return boom


class TestWebQuota(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="helm-test-webq-")
        cls.env_prior = {k: os.environ.get(k) for k in ("HELM_HOME", "MELD_HOME")}
        os.environ["HELM_HOME"] = cls.tmp
        os.environ.pop("MELD_HOME", None)
        assert home.helm_home() == cls.tmp, "HELM_HOME override must win"
        pk.write_json(home.registry_path(), {"version": 1, "projects": {},
                                             "generated_ts": pk.now_ts()})
        # homes roots -> tmp (same repoint as test_homes; no real dirs touched)
        j = lambda *p: os.path.join(cls.tmp, *p)
        cls.homes_orig = {k: getattr(homes, k) for k in
                          ("ROOTS", "DEFAULTS", "SHARED_PROJECTS",
                           "ARCHIVE_ROOT", "LEGACY_ARCHIVE_ROOT", "_agent_procs")}
        homes.ROOTS = {"claude": j("claude-homes"), "codex": j("codex-homes")}
        homes.DEFAULTS = {"claude": j("default-claude"), "codex": j("default-codex")}
        homes.SHARED_PROJECTS = j("default-claude", "projects")
        homes.ARCHIVE_ROOT = j("helm-home-archive")
        homes.LEGACY_ARCHIVE_ROOT = j("legacy-home-archive")
        homes._agent_procs = lambda: []
        for r in homes.ROOTS.values():
            os.makedirs(r)
        # stub provider + a canned catalog (burn joins against it) — the native
        # provider and the real catalog build are never exercised here. The burn
        # endpoint reaches the catalog through transcripts.get_catalog() (the ONE
        # single-flight path both the burn view and /api/catalog now share), so
        # the stub rides there, not on a web-local cache.
        cls.provider_prior = web._PROVIDER
        web._PROVIDER = StubProvider()
        web._qstate.clear()
        cls.catalog_row = {"h": "claude", "i": "11111111-2222-3333-4444-555555555555",
                           "c": "~/dev/x", "t": "quota burn probe", "z": 4096,
                           "m": 10, "mt": int(time.time() - 1800), "cwd": "/dev/x"}
        cls._get_catalog_prior = transcripts.get_catalog
        transcripts.get_catalog = lambda refresh=False: {
            "rows": [dict(cls.catalog_row)], "stats": {}, "scanned_at": time.time()}
        cls.srv = web.make_server(0)  # ephemeral port
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.thread.join(timeout=5)
        web._PROVIDER = cls.provider_prior
        transcripts.get_catalog = cls._get_catalog_prior
        web._qstate.clear()
        for k, v in cls.homes_orig.items():
            setattr(homes, k, v)
        for k, v in cls.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # -- plumbing ----------------------------------------------------------
    def req(self, path, payload=None):
        """(status, body_json) — 4xx/5xx returned, not raised. payload -> POST."""
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        data = json.dumps(payload).encode() if payload is not None else None
        r = urllib.request.Request(url, data=data, headers=(
            {"Content-Type": "application/json",
             "Authorization": "Bearer " + web.MUTATION_TOKEN} if data else {}))
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"null")

    def _fresh(self, *keys):
        """Bust the single-flight cache for these keys (creds/history/... TTLs)."""
        with web._qlock:
            for k in list(web._qstate):
                if any(k == key or k.startswith(key + ":") for key in keys):
                    web._qstate.pop(k)

    # -- /api/creds --------------------------------------------------------
    def test_creds_returns_merged_rows(self):
        self._fresh("creds")
        status, d = self.req("/api/creds")
        self.assertEqual(status, 200)
        self.assertIsInstance(d, list)
        self.assertEqual(len(d), 1)
        row = d[0]
        for key in ("name", "provider", "home", "home_name", "identity",
                    "name_lies", "active", "tier", "headroom", "state", "status",
                    "resets_at_ms", "windows_left", "windows_per_week",
                    "windows_verdict"):
            self.assertIn(key, row, "/api/creds shape: missing %s" % key)
        self.assertEqual(row["name"], ACCT)
        self.assertEqual(row["state"], "ok")
        self.assertEqual(row["headroom"], 72.5)
        self.assertEqual(row["windows_left"], 4.0)
        self.assertEqual(row["home_name"], "alice-example-com")

    def test_creds_degrades_without_provider(self):
        prior = web._PROVIDER
        web._PROVIDER = BrokenProvider()
        self._fresh("creds")
        try:
            status, d = self.req("/api/creds")
            self.assertEqual(status, 200, "creds must never 500")
            # the shape on ProviderError is [] ; {"unavailable": true} also legal
            self.assertTrue(d == [] or (isinstance(d, dict) and d.get("unavailable")),
                            "expected empty-or-unavailable, got %r" % d)
        finally:
            web._PROVIDER = prior
            self._fresh("creds")

    # -- /api/status -------------------------------------------------------
    def test_status_shape(self):
        self._fresh("creds")
        status, d = self.req("/api/status")
        self.assertEqual(status, 200)
        self.assertIsInstance(d, dict)
        prov = d.get("provider")
        self.assertIsInstance(prov, dict)
        for key in ("configured", "present", "accounts", "degraded", "fallback"):
            self.assertIn(key, prov)
        self.assertEqual(prov["accounts"], 1)
        self.assertFalse(prov["degraded"])
        self.assertIn("cv", d)
        self.assertIn("catalogRows", d)

    # -- /api/history ------------------------------------------------------
    def test_history_oldest_first_with_gauges(self):
        self._fresh("history")
        status, d = self.req("/api/history?hours=168")
        self.assertEqual(status, 200)
        self.assertIsInstance(d, list)
        self.assertEqual(len(d), 2)
        self.assertLessEqual(d[0]["probed_at"], d[1]["probed_at"])
        self.assertEqual(d[0]["account"], ACCT)
        g = d[0]["gauges"][0]
        for key in ("label", "utilization", "reset"):
            self.assertIn(key, g)

    # -- /api/burn ---------------------------------------------------------
    def test_burn_buckets_shape_and_temporal_join(self):
        self._fresh("burn", "history")
        status, d = self.req("/api/burn?hours=48")
        self.assertEqual(status, 200)
        self.assertIn("buckets", d)
        self.assertIn("note", d)
        burn = [b for b in d["buckets"] if b["byAccount"]]
        self.assertEqual(len(burn), 1, "one 20%% drop -> one burn bucket: %r" % d)
        b = burn[0]
        for key in ("t", "byAccount", "sessions"):
            self.assertIn(key, b)
        self.assertAlmostEqual(b["byAccount"][ACCT], 20.0, places=1)
        # the catalog row last-active inside the window joins into SOME bucket
        joined = [s for bb in d["buckets"] for s in bb["sessions"]]
        self.assertTrue(any(s["i"] == self.catalog_row["i"] for s in joined))
        self.assertEqual(set(joined[0]), {"i", "t", "c", "h"})

    # -- /api/allocate -----------------------------------------------------
    def test_allocate_per_model_pick_and_ranked(self):
        self._fresh("alloc")
        status, d = self.req("/api/allocate")
        self.assertEqual(status, 200)
        self.assertIsInstance(d, dict)
        self.assertIn("fable", d)  # default HELM_ALLOC_MODELS list
        for m, v in d.items():
            self.assertIn("pick", v)
            self.assertIn("ranked", v)
        self.assertEqual(d["fable"]["pick"]["account"], ACCT)

    # -- /api/homes --------------------------------------------------------
    def test_homes_get_lists_tmp_roots_only(self):
        status, d = self.req("/api/homes")
        self.assertEqual(status, 200)
        self.assertIsInstance(d, list)  # tmp roots are empty at first

    def test_homes_post_create_prints_login_never_runs_it(self):
        status, d = self.req("/api/homes", {"action": "create", "provider": "claude",
                                            "email": "new@user.dev"})
        self.assertEqual(status, 200, d)
        self.assertIn("claude /login", d["login_cmd"])
        self.assertTrue(d["home"].startswith(self.tmp), "home must land under tmp")
        self.assertTrue(os.path.isdir(d["home"]))
        # prepared, not authed: no credentials were minted or seated
        self.assertFalse(os.path.exists(os.path.join(d["home"], ".credentials.json")))
        status, rows = self.req("/api/homes")
        self.assertTrue(any(r["name"] == "new-user-dev" for r in rows))

    def test_homes_post_unknown_action_400(self):
        status, d = self.req("/api/homes", {"action": "explode"})
        self.assertEqual(status, 400)
        self.assertIn("error", d)

    # -- the tabbed UI -----------------------------------------------------
    def test_ui_carries_nav_tabs_and_all_views(self):
        url = "http://127.0.0.1:%d/" % self.port
        with urllib.request.urlopen(url, timeout=10) as r:
            self.assertEqual(r.status, 200)
            body = r.read().decode("utf-8")
        for marker in ('id="nav"', 'class="navtab on" data-v="helm"',
                       'data-v="quota"', 'data-v="sessions"', 'data-v="configs"',
                       'id="view-helm"', 'id="view-quota"', 'id="view-sessions"',
                       'id="view-configs"'):
            self.assertIn(marker, body, "nav/view markup missing: %s" % marker)
        for marker in ('id="attn"', 'id="chart"', 'id="qtabs"', 'id="zoomhint"',
                       'id="burncard"', 'id="acctcard"', 'id="athead"',
                       'id="atbody"', 'id="homescard"', 'id="homeform"',
                       'id="hCreate"'):
            self.assertIn(marker, body, "quota view markup missing: %s" % marker)
        # the helm knowledge view survived the restructure intact
        for marker in ('id="grid"', 'id="store"', 'id="sessions"', 'id="skillsec"',
                       'id="hello"', 'id="foot"'):
            self.assertIn(marker, body, "helm view markup missing: %s" % marker)
        # slice B landed (real sessions view); slice C replaced the placeholder
        self.assertIn('id="sesscontrols"', body)
        self.assertNotIn("slice C", body)
        for marker in ('id="cfgbar"', 'id="cfgsplit"', 'id="cfgtree"',
                       'id="cfgdetail"'):
            self.assertIn(marker, body, "configs view markup missing: %s" % marker)

    def test_existing_endpoints_still_work(self):
        status, d = self.req("/api/registry")
        self.assertEqual(status, 200)
        self.assertEqual(d.get("projects"), {})
        status, d = self.req("/api/store")
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
