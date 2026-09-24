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
        homes.LEGACY_ARCHIVE_ROOT = j("oldtool-home-archive")
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
        # shutdown() waits one serve_forever poll (stdlib 0.5s)
        cls.thread = threading.Thread(target=cls.srv.serve_forever,
                                      kwargs={"poll_interval": 0.01}, daemon=True)
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
            self.assertIn(key, row, "predecessor /api/creds shape: missing %s" % key)
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
            # the predecessor's shape on ProviderError is [] ; {"unavailable": true} also legal
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
                                            "email": "new@user.example"})
        self.assertEqual(status, 200, d)
        self.assertIn("claude /login", d["login_cmd"])
        self.assertTrue(d["home"].startswith(self.tmp), "home must land under tmp")
        self.assertTrue(os.path.isdir(d["home"]))
        # prepared, not authed: no credentials were minted or seated
        self.assertFalse(os.path.exists(os.path.join(d["home"], ".credentials.json")))
        status, rows = self.req("/api/homes")
        self.assertTrue(any(r["name"] == "new-user-example" for r in rows))

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
        for marker in ('id="nav"', 'class="navarea on" data-a="work"',
                       'data-v="quota"', 'data-v="sessions"', 'data-v="configs"',
                       'id="view-work"', 'id="view-quota"', 'id="view-sessions"',
                       'id="view-configs"'):
            self.assertIn(marker, body, "nav/view markup missing: %s" % marker)
        for marker in ('id="attn"', 'id="chart"', 'id="qtabs"', 'id="zoomhint"',
                       # the two account panels are ONE table now, by the
                       # owner's ruling: "compose instead of make new"
                       'id="burncard"', 'id="qonecard"', 'id="qonehead"',
                       'id="qonebody"', 'id="homescard"', 'id="homeform"',
                       'id="hCreate"'):
            self.assertIn(marker, body, "quota view markup missing: %s" % marker)
        # the home view survived the restructure intact as the Work page; the
        # board's rows replaced the project card grid (task/2975)
        for marker in ('id="brows"', 'id="store"', 'id="sessions"', 'id="skillsec"',
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


# ---------------------------------------------------------------------------
# the attention strip, RUN rather than mirrored
# ---------------------------------------------------------------------------

ATTN_SUPPORT = r"""
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const short = (n, l = 26) => String(n).length > l ? String(n).slice(0, l - 1) + "…" : String(n);
const HIST = [], ALLOC = {};
const strandAtReset = () => null;
let CREDS = [];
let CREDS_UNREADABLE = null;
const EL = {};
function el(sel) {
  if (!EL[sel]) EL[sel] = {sel, innerHTML: "", dataset: {},
                           classList: {remove: () => {}, add: () => {},
                                       contains: () => false, toggle: () => {}}};
  return EL[sel];
}
const $ = sel => el(sel);
"""

ATTN_DRIVER = r"""
const C = (over) => Object.assign({
  name: "a@x.example", provider: "anthropic", state: "ok", status: "allowed",
  headroom: 80, tier: "Max 20x", home: "/h", home_name: "h", identity: null,
  name_lies: false, windows_verdict: "ok",
}, over || {});

function strip(creds, unreadable) {
  CREDS = creds;
  CREDS_UNREADABLE = unreadable || null;
  renderAttn();
  return el("#attn").innerHTML;
}

const out = {};
out.ok_only = strip([C()]);
out.due = strip([C({name: "due@x.example", state: "due-refresh",
                    status: "keepalive due (helm's token expired 3h ago; its refresh chain is refresh-live) — `helm keepalive --apply` refreshes it, no login needed; last keepalive refresh T by keepalive-cron"})]);
out.expired = strip([C({name: "dead@x.example", state: "expired-token",
                        status: "reauth-needed (helm's token expired 9d ago and no-home-refresh-token; orca refreshes its own store, not this one)"})]);
out.both = strip([
  C({name: "due@x.example", state: "due-refresh", status: "keepalive due"}),
  C({name: "dead@x.example", state: "expired-token", status: "reauth-needed"})]);
out.drifted = strip([C({name: "who@x.example", name_lies: true,
                        identity: "other@x.example", home_name: "who"})]);
/* helm could not READ the accounts: no rows, and a reason. */
out.unreadable = strip([], "helm could not read the measured quota rows just now (OSError)");
/* THREE CAUSES OF ONE WORD (task/2981). Each reads state "unknown", and each
   has its own cause and cure. */
out.unproven = strip([C({name: "hey-home", provider: "codex", state: "unknown",
                         status: "pool-member-unknown", headroom: null,
                         member_unproven: true, home_name: "cx-hey"})]);
out.no_reading = strip([C({name: "quiet", state: "unknown", status: null,
                           headroom: null})]);
out.unread_row = strip([C({name: "blurred", state: "unknown", headroom: null,
                           quota_unreadable: true})]);
/* the control it has to be told apart from: a machine with no quota provider.
   Zero rows again, and nothing wrong. */
out.no_provider = strip([]);
console.log(JSON.stringify(out));
"""


QTABLE_SUPPORT = r"""
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
let CREDS = [], HIST = [];
const ROSTER = () => new Set(CREDS.map(c => c.name));
let DECL = {accounts: []};
/* DECORATION, AND STUBBED AS SUCH. The cell arm below is about which WORD the
   state cell carries; the colour of the dot, the truncation of a flag and the
   spelling of a login command are not its subject. `quotaIdCell` itself is
   lifted from the assembled page like everything else here. */
const short = n => n;
const acol = () => "#000";
const LOGIN_CMDS = {codex: h => "LOGIN " + h};
"""

QTABLE_DRIVER = r"""
/* ONE ACCOUNT, PROBED TWICE. The older probe carries a carve-out the provider
   has since retired and a window it still reports; the newer one carries only
   the window. Nothing here is named `spark` — the arm is about a window that
   STOPPED being reported, whatever it was called. */
const OLD = "2026-09-17T09:00:00Z", NEW = "2026-09-18T09:00:00Z";
const g = (label, u) => ({label, utilization: u, reset: null});
CREDS = [{name: "acct", provider: "codex"}];
HIST = [
  {account: "acct", probed_at: OLD,
   gauges: [g("5h", 0.1), g("7d", 0.2), g("7d-extra", 0), g("retired-window", 0)]},
  {account: "acct", probed_at: NEW,
   gauges: [g("5h", 0.1), g("7d", 0.2), g("7d-extra", 0)]},
];
const out = {labels: gaugeLabels()};

/* …and the CONTROL that this is a reading of the CURRENT probe rather than a
   filter on the label: with the carve-out back in the newest probe it returns,
   with no edit to the page. */
HIST.push({account: "acct", probed_at: "2026-09-19T09:00:00Z",
           gauges: [g("5h", 0.1), g("7d", 0.2), g("7d-extra", 0),
                    g("brand-new-window", 0)]});
out.labels_after = gaugeLabels();

/* THE COMPOSITION. Two credential homes on ONE login, the default pointer that
   resolves to a third, and an account nothing measures — the four shapes the
   live fleet has. `subscription` is the server's opaque key for vendor + login;
   nothing here is an address. */
CREDS = [
  {name: "one-home", provider: "codex", state: "ok", headroom: 40,
   subscription: "s-one", home_name: "h1"},
  {name: "one-home#second", provider: "codex", state: "ok", headroom: 10,
   subscription: "s-one", home_name: "h2"},
  {name: "(default-codex)", provider: "codex", state: "ok", headroom: 40,
   subscription: "s-one", home_name: "hd"},
  {name: "solo", provider: "codex", state: "ok", headroom: 70,
   subscription: "s-solo", home_name: "h3"},
];
DECL = {accounts: [
  {id: "the-one", vendor: "codex", subscription: "s-one", plan: "Big",
   confirmed: true, good_for: "the thing"},
  {id: "the-one-again", vendor: "codex", subscription: "s-one", plan: "Big"},
  {id: "nothing-measures-me", vendor: "deepseek", plan: "payg"},
]};
const composed = quotaRows();
/* THE SCREEN HE EDITS ON SHOWS THE SAME ROWS THE TABLE DOES — not a copy of
   the row set, the row set. */
out.fill_keys = fillRows().map(r => r.key);
out.table_keys = composed.map(r => r.sub);
out.fill_ids = fillRows().map(r => r.id);
out.fill_createable = fillRows().filter(r => !r.id).map(r => r.label);
out.composed = composed.map(r => ({
  cred: r.cred ? r.cred.name : null,
  aliases: r.aliases.map(a => a.name),
  decl: r.decl ? r.decl.id : null,
  spare: (r.spare || []).map(a => a.id),
}));

/* THE ORDER. Every row differs in exactly one of the things the sort reads. */
const A = (over) => Object.assign({
  name: "z", provider: "codex", state: "ok", headroom: 50, active: false,
  resets_at_ms: null,
}, over || {});
const R = (over) => ({cred: A(over), aliases: [], decl: null, spare: [],
                      fam: "codex", name: A(over).name, sub: "s-" + A(over).name});
HIST = [];
const ROWS = [
  R({name: "walled-late", headroom: 0, state: "exhausted", resets_at_ms: 3000}),
  R({name: "needs-login", headroom: null, state: "expired-token"}),
  R({name: "walled-soon", headroom: 0, state: "exhausted", resets_at_ms: 2000}),
  R({name: "roomy", headroom: 90}),
  R({name: "live-but-due", headroom: null, state: "expired-token", active: true}),
  R({name: "tight", headroom: 5}),
  /* an account NOTHING measures: no credential at all, which is not the same
     as a credential that measured zero */
  {cred: null, aliases: [], spare: [], fam: "codex", name: null, sub: "s-decl",
   decl: {id: "declared-only", vendor: "codex"}},
];
CREDS = ROWS.filter(r => r.cred).map(r => r.cred);
out.order = acctDefaultSort(ROWS).map(r => acctRowName(r));
out.tiers = ROWS.map(r => [acctRowName(r), acctTier(r)]);
out.band = acctTierRow(ATIER.UNMEASURED, 5);

/* UNREADABLE IS NOT DATALESS, on the table he steers from. A row whose windows
   helm could not READ still names an account helm measures: dropped by the
   no-data filter it becomes the page drawing "nothing there" for "could not
   read", which is the same conflation the server door was cured of, one
   surface further out. The control beside it is the home that really does have
   nothing — no auth, no reading — and that one is still a HOMES row. */
DECL = {accounts: []};
HIST = [];
CREDS = [
  {name: "windows-unreadable", provider: "codex", state: "unknown",
   subscription: "s-unreadable", home_name: "hu", quota_unreadable: true},
  {name: "nothing-at-all", provider: "codex", state: "unknown",
   subscription: "s-none", home_name: "hn"},
];
out.unreadable_kept = quotaRows().map(r => r.cred && r.cred.name);
out.unreadable_cell = quotaIdCell({cred: CREDS[0], aliases: []});
out.dataless_cell = quotaIdCell({cred: CREDS[1], aliases: []});

/* A MEMBER NO POOL FILE IS PROVEN TO BE (task/2981). The server measured the
   pool and it names other members of this workspace, not this one. That row
   is not dataless, and its cure is to pool its own credential, not a login. */
CREDS = [
  {name: "hey-home", provider: "codex", state: "unknown",
   status: "pool-member-unknown", subscription: "s-hey", home_name: "cx-hey",
   member_unproven: true},
  {name: "nothing-at-all", provider: "codex", state: "unknown",
   subscription: "s-none", home_name: "hn"},
];
out.unproven_kept = quotaRows().map(r => r.cred && r.cred.name);
out.unproven_cell = quotaIdCell({cred: CREDS[0], aliases: []});

console.log(JSON.stringify(out));
"""


class QuotaTableRuntimeTest(unittest.TestCase):
    """The two things the owner asked about this table, executed under node.

    Node is optional on non-web hosts: absent, this SKIPS."""

    EXTRACT = ("currentProbes", "gaugeLabels", "latestGauges", "acctTier",
               "acctResetAt", "acctRowName", "acctDefaultSort", "acctTierRow",
               # the composition the owner's ruling is about
               "quotaRows", "quotaAliasOrder", "quotaDeclOrder", "quotaIdCell",
               # …and the screen he EDITS them on, which must show the same set
               "fillRows", "fillComposed", "fillNeedsDescribe")
    CONSTS = ("ATIER", "ATIER_SAID")

    @classmethod
    def setUpClass(cls):
        import shutil as _sh
        import subprocess
        from helm import web_ui_loader
        from tests.test_web_chat_client_runtime import _extract_fn
        from tests.test_web_accounts import _extract_const
        cls.node = _sh.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        src = web_ui_loader.read_text()
        fns = "\n\n".join(_extract_fn(src, n) for n in cls.EXTRACT)
        # THE TIER NUMBERS COME OUT OF THE SHIPPED PAGE, never retyped here: a
        # mirror of them would keep agreeing with itself after the page moved.
        consts = "\n".join(_extract_const(src, n) for n in cls.CONSTS)
        cls.tmp = tempfile.mkdtemp(prefix="helm-web-quota-table-")
        cls.path = os.path.join(cls.tmp, "run.js")
        with open(cls.path, "w", encoding="utf-8") as f:
            f.write(QTABLE_SUPPORT + "\n" + consts + "\n"
                    + fns + "\n" + QTABLE_DRIVER)
        chk = subprocess.run([cls.node, "--check", cls.path],
                             capture_output=True, text=True)
        assert chk.returncode == 0, "node --check failed:\n" + chk.stderr
        cls.proc = subprocess.run([cls.node, cls.path], capture_output=True,
                                  text=True, timeout=60)
        try:
            cls.out = json.loads(cls.proc.stdout or "{}")
        except json.JSONDecodeError:
            cls.out = {}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def setUp(self):
        self.assertTrue(self.out, "node produced no output: "
                        + (self.proc.stderr or "")[:400])

    def test_a_window_the_provider_stopped_reporting_stops_being_a_column(self):
        """The owner: "i think openai has retired the spark 5.3 carve out, i
        dont see it in codex backends anymore". It held its column because the
        reading ran over a week of history, so "the API reports it" meant "the
        API reported it at some point this week"."""
        labels = self.out["labels"]
        self.assertNotIn("retired-window", labels)
        # THE UNCONDITIONAL CONTROL on the same call: the OTHER extra window,
        # which the newest probe still reports, is there. Without it this arm
        # would also pass on a reading that returned nothing at all, and on one
        # that had simply deleted every extra column by name.
        self.assertIn("7d-extra", labels)

    def test_a_window_the_provider_still_reports_keeps_its_column(self):
        """THE UNCONDITIONAL CONTROL on the same call, and the reason this is
        not a deletion: an extra window in the current probe is exactly as much
        an extra window as the retired one was, and it survives."""
        self.assertIn("7d-extra", self.out["labels"])
        self.assertEqual(self.out["labels"][:2], ["5h", "7d"])

    def test_a_carve_out_that_comes_back_appears_with_no_edit_to_the_page(self):
        self.assertIn("brand-new-window", self.out["labels_after"])

    def test_two_homes_on_one_login_are_ONE_row_with_the_others_as_aliases(self):
        """The owner's ruling: one row per paid subscription, and every home
        resolving to that login is an alias ON it. He was shown three rows for
        one account — two credential homes and the default pointer — and could
        not place any of them."""
        c = self.out["composed"]
        one = [r for r in c if r["cred"] == "one-home"]
        self.assertEqual(len(one), 1, c)
        self.assertEqual(sorted(one[0]["aliases"]),
                         ["(default-codex)", "one-home#second"])
        # THE CONTROL on the same composition: an account with ONE home is one
        # row with no aliases, so the grouping is keyed on the login and not
        # collapsing everything it sees.
        solo = [r for r in c if r["cred"] == "solo"]
        self.assertEqual(len(solo), 1, c)
        self.assertEqual(solo[0]["aliases"], [])

    def test_a_second_record_for_one_subscription_rides_the_row_not_a_new_one(self):
        """Nothing he edited may vanish: the row shows the record he confirmed
        and NAMES the spare beside it, which is the job he can then finish."""
        one = next(r for r in self.out["composed"] if r["cred"] == "one-home")
        self.assertEqual(one["decl"], "the-one")
        self.assertEqual(one["spare"], ["the-one-again"])

    def test_an_account_nothing_measures_is_still_a_row(self):
        """A prepaid balance or a seatless subscription has no measured half,
        and dropping it would hide something he pays for every month."""
        c = self.out["composed"]
        lonely = [r for r in c if r["decl"] == "nothing-measures-me"]
        self.assertEqual(len(lonely), 1, c)
        self.assertIsNone(lonely[0]["cred"])

    def test_every_declared_record_lands_on_exactly_one_row(self):
        """THE CONTROL over the whole composition: no record is dropped and
        none is shown twice, which is the property that makes one-row-per-
        subscription a rearrangement rather than a filter."""
        seen = []
        for r in self.out["composed"]:
            if r["decl"]:
                seen.append(r["decl"])
            seen.extend(r["spare"])
        self.assertEqual(sorted(seen),
                         ["nothing-measures-me", "the-one", "the-one-again"])

    def test_the_screen_he_edits_shows_exactly_the_rows_the_table_shows(self):
        """THE OWNER'S NAMED CONTROL. Fill mode calls the table's own
        composition rather than re-deriving one, so the two surfaces cannot
        disagree about which accounts exist — which is the failure that had him
        editing rows the table did not show and reading rows he could not
        edit."""
        self.assertEqual(self.out["fill_keys"], self.out["table_keys"])
        # …and it is a REAL set, not two empties agreeing
        self.assertTrue(self.out["fill_keys"])

    def test_a_measured_account_with_no_record_is_a_row_with_empty_cells(self):
        """It renders ONCE, under the name the quota table gives it, with
        nothing declared — never as a row that is simply missing until a seed
        mints one for it."""
        self.assertEqual(self.out["fill_createable"], ["solo"])
        # THE CONTROL on the same list: the rows that DO have a record carry
        # its id, so "solo has none" is about that row.
        self.assertIn("the-one", self.out["fill_ids"])

    def test_the_rows_worth_using_come_first_and_the_walled_ones_last(self):
        """His words: "most relevant should be top, kicked/awaiting reset
        should be bottom, no?"."""
        order = self.out["order"]
        self.assertEqual(order[-2:], ["walled-soon", "walled-late"],
                         "walled rows sink, soonest reset first: %r" % order)
        self.assertLess(order.index("roomy"), order.index("tight"),
                        "more headroom outranks less: %r" % order)

    def test_a_live_seat_outranks_every_measurement_including_a_missing_one(self):
        self.assertEqual(self.out["order"][0], "live-but-due", self.out["order"])

    def test_a_due_token_is_not_sorted_to_the_bottom_as_a_dead_account(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control on the same observable is the first assertEqual: the sort returned EVERY row it was given, so both index() calls below are real positions in a full list rather than a claim about an empty one
        """task/2680's class: two rows read expired-token while his own usage
        pages showed them working. A due token is a chore, not an empty
        account, so it sits above everything measured walled."""
        order = self.out["order"]
        self.assertEqual(sorted(order), sorted(r[0] for r in self.out["tiers"]),
                         "the control: the sort returns every row it was "
                         "given, so the positions below are real")
        self.assertLess(order.index("needs-login"), order.index("walled-soon"))
        # and it is NOT usable either — the control on the same tiering, so
        # this is a statement about a third tier rather than about leniency
        self.assertGreater(order.index("needs-login"), order.index("tight"))

    def test_the_table_says_out_loud_how_it_is_ordered(self):
        """He asked how the rows were ordered; a silently-correct table leaves
        him asking again."""
        band = self.out["band"]
        self.assertIn("not measuring", band)
        self.assertIn("atier", band)

    def test_a_row_helm_could_not_read_is_not_filtered_out_as_dataless(self):
        """"COULD NOT READ" AND "NOTHING THERE" ARE NOT THE SAME ROW. The
        no-data filter is right about a home with no auth and no reading — that
        is a HOMES row. It is wrong about an account helm measures whose quota
        windows it could not read this minute: dropping that one takes the row
        he steers from off the screen and calls it absent."""
        self.assertEqual(self.out["unreadable_kept"], ["windows-unreadable"],
                         "the unreadable row was dropped, the dataless one "
                         "kept, or both")

    def test_an_unproven_member_keeps_its_row_and_names_its_own_cause(self):
        """task/2981. A codex Team member whose workspace is pooled, with no
        pool file proven to be it, is MEASURED: it stays on the table, and its
        cell names that cause and its cure. The control beside it is the home
        with nothing at all, which is still a HOMES row."""
        self.assertEqual(self.out["unproven_kept"], ["hey-home"])
        cell = self.out["unproven_cell"]
        self.assertIn(">member unproven<", cell)
        self.assertIn("no pool file matches this member", cell)
        self.assertIn("helm codex pool cx-hey", cell)
        self.assertNotIn("LOGIN ", cell, "a login is not this row's cure")
        self.assertNotIn(">unknown<", cell)
        # the control on the same renderer: a row with no reading still
        # wears the plain word
        self.assertIn(">unknown<", self.out["dataless_cell"])

    def test_the_state_cell_says_unreadable_rather_than_unknown(self):
        """The word is the whole surface of the distinction. `unknown` is what
        a provider that ANSWERED, with no state in it, earns."""
        said = self.out["unreadable_cell"]
        self.assertIn("unreadable", said)
        self.assertIn("UNKNOWN", said, "the cell has to say what it means")
        other = self.out["dataless_cell"]
        self.assertIn("unknown", other)
        self.assertNotIn("unreadable", other,
                         "a row with nothing measured wore the word for a row "
                         "helm could not read")


class AttentionStripRuntimeTest(unittest.TestCase):
    """The pills the owner actually reads, executed under node.

    A DUE TOKEN IS NOT A DEAD ACCOUNT, and this card is where that sentence
    reaches him: while `due-refresh` did not exist, three healthy accounts rode
    the "need re-login" pill. A python mirror of the filter would rot silently,
    so the real `renderAttn` is lifted verbatim out of the assembled page.

    Node is optional on non-web hosts: absent, this SKIPS."""

    EXTRACT = ("attnPill", "renderAttn")

    @classmethod
    def setUpClass(cls):
        import shutil as _sh
        import subprocess
        from helm import web_ui_loader
        from tests.test_web_chat_client_runtime import _extract_fn
        cls.node = _sh.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        src = web_ui_loader.read_text()
        fns = "\n\n".join(_extract_fn(src, n) for n in cls.EXTRACT)
        cls.tmp = tempfile.mkdtemp(prefix="helm-web-quota-attn-")
        cls.path = os.path.join(cls.tmp, "run.js")
        with open(cls.path, "w", encoding="utf-8") as f:
            f.write(ATTN_SUPPORT + "\n" + fns + "\n" + ATTN_DRIVER)
        chk = subprocess.run([cls.node, "--check", cls.path],
                             capture_output=True, text=True)
        assert chk.returncode == 0, "node --check failed:\n" + chk.stderr
        cls.proc = subprocess.run([cls.node, cls.path], capture_output=True,
                                  text=True, timeout=60)
        try:
            cls.out = json.loads(cls.proc.stdout or "{}")
        except json.JSONDecodeError:
            cls.out = {}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def setUp(self):
        self.assertTrue(self.out, "node produced no output: " + (self.proc.stderr or "")[:400])

    def test_a_due_account_gets_the_keepalive_pill_and_never_the_relogin_one(self):
        due = self.out["due"]
        self.assertIn("1 keepalive due", due)
        self.assertNotIn("need re-login", due)
        self.assertIn("helm keepalive --apply", due)
        self.assertNotIn("reauth", due)

    def test_a_spent_account_still_gets_the_relogin_pill(self):
        """The must-hit control on the same filter: the state this one split
        AWAY from still renders exactly as it did."""
        dead = self.out["expired"]
        self.assertIn("1 need re-login", dead)
        self.assertNotIn("keepalive due", dead)

    def test_the_two_states_are_two_pills_not_one_count(self):
        both = self.out["both"]
        self.assertIn("1 keepalive due", both)
        self.assertIn("1 need re-login", both)

    def test_an_ok_account_renders_no_pill_for_either(self):
        ok = self.out["ok_only"]
        self.assertIn("all accounts healthy", ok)
        self.assertNotIn("keepalive due", ok)
        self.assertNotIn("need re-login", ok)

    def test_an_unreadable_measurement_is_a_pill_and_not_an_empty_strip(self):
        """AND ONE SURFACE FURTHER OUT. The server now says WHY the rows are
        missing; drawing nothing with that in hand tells him everything is
        fine on a tab whose numbers are all unknown."""
        said = self.out["unreadable"]
        self.assertIn("UNREADABLE", said)
        self.assertIn("OSError", said)
        self.assertNotIn("all accounts healthy", said)

    def test_a_machine_with_no_quota_provider_draws_no_alarm(self):  # noqa: VACUOUS_ASSERTION — the empty strip IS the contract here, and the assertIn on self.out["unreadable"] in this arm is the unconditional control that the same renderer DOES fill for the state this one is told apart from
        """THE CONTROL, and the arm above is meaningless without it: zero rows
        is ALSO what a host with no provider looks like, and this tab is
        fail-open there. Nothing measured is not something unread."""
        # the unconditional positive control on the SAME observable: the
        # renderer DOES draw that pill when the read failed, so the emptiness
        # below is this branch holding rather than a strip that never fills
        self.assertIn("UNREADABLE", self.out["unreadable"])
        said = self.out["no_provider"]
        self.assertNotIn("UNREADABLE", said)
        self.assertEqual(said, "", said)

    def test_an_unproven_member_gets_its_own_pill_and_cause(self):
        """task/2981. The not-reporting pill told him a pooled Team member
        had "no auth or usage endpoint". It has auth. No pool file is proven
        to be it, and pooling its own credential is the cure."""
        said = self.out["unproven"]
        self.assertIn("1 member unproven", said)
        self.assertIn("no pool file matches this member", said)
        self.assertIn("helm codex pool cx-hey", said)
        self.assertNotIn("not reporting", said)
        self.assertNotIn("no auth", said)
        self.assertNotIn("all accounts healthy", said)

    def test_a_row_with_no_reading_keeps_the_not_reporting_pill(self):
        """THE CONTROL on the same filter: a row with no reading at all
        still reads "not reporting", and says "no reading"."""
        said = self.out["no_reading"]
        self.assertIn("1 not reporting", said)
        self.assertIn("no reading", said)
        self.assertNotIn("member unproven", said)

    def test_an_unreadable_row_is_not_told_it_has_no_auth(self):
        said = self.out["unread_row"]
        self.assertIn("1 unreadable", said)
        self.assertIn("UNKNOWN, not zero", said)
        self.assertNotIn("no auth", said)
        self.assertNotIn("not reporting", said)

    def test_a_drifted_home_names_what_it_holds_and_the_repair(self):
        drifted = self.out["drifted"]
        self.assertIn("home issue", drifted)
        self.assertIn("holds other@x.example", drifted)
        self.assertIn("named who", drifted)
        self.assertIn("helm cred heal", drifted)
        self.assertNotIn("need re-login", drifted)



# ---------------------------------------------------------------------------
# what the quota chart's history is allowed to cost a phone
# ---------------------------------------------------------------------------

class HistoryWireBoundsTest(unittest.TestCase):
    """WHAT /api/history MAY WEIGH, and what it may never stop carrying.

    THE DEFECT: measured on the owner's own probe history, `/api/history?hours=168`
    shipped 766,109 bytes over 1,993 rows, and the quota tab re-fetches the whole
    list on a 120-second interval. Server time was never the complaint; bytes on
    a cellular link were. Six fields in that payload had no reader anywhere —
    three on the row, three inside every gauge — and between them they were
    316,880 of those bytes.

    The cure is a DROP at emit, not a truncation, and these arms hold it to
    both halves of that: the six go, and every field a reader touches stays
    whole, on every row that went in. `get_history` itself is untouched, because
    `get_burn` joins against the same cached list and a trim pushed one function
    inwards would narrow burn attribution with nothing on that surface saying so.
    """

    FULL_ROW = {
        "provider": "anthropic", "account": "alice@example.com",
        "probed_at": "2026-09-15T03:51:53Z", "status": "allowed",
        "primary": "5h", "source_at": None,
        "gauges": [{"label": "5h", "kind": "session", "utilization": 0.06,
                    "reset": 1789446000, "limit": None, "remaining": None},
                   {"label": "7d", "kind": "period", "utilization": 0.89,
                    "reset": 1789563600, "limit": None, "remaining": None}],
    }

    def test_the_six_fields_with_no_reader_left_the_wire(self):  # noqa: VACUOUS_ASSERTION — nine unconditional assertIn on the SAME row and the SAME two gauge dicts the assertNotIn run against, plus an equality on the account and on gauge 0's label and utilization, all outside every loop
        rows = web._history_on_the_wire([dict(self.FULL_ROW)])
        self.assertEqual(1, len(rows), "the trim dropped a row")
        row = rows[0]
        # THE UNCONDITIONAL CONTROL, before any loop below can decide not to
        # run: this row came back at all, and it is the row that went in.
        self.assertEqual("alice@example.com", row["account"])
        self.assertEqual(2, len(row["gauges"]), "the trim dropped a gauge")
        # …and on the GAUGE, which the loops below are the only other readers
        # of: the first one came through with the two values the chart plots,
        # so an absence found inside a loop is a trim and not an empty list.
        self.assertEqual("5h", row["gauges"][0]["label"])
        self.assertEqual(0.06, row["gauges"][0]["utilization"])
        for key in ("status", "primary", "source_at"):
            self.assertNotIn(key, row,
                             "%r rode a history row nothing reads it from" % key)
        for gauge in row["gauges"]:
            for key in ("kind", "limit", "remaining"):
                self.assertNotIn(key, gauge,
                                 "%r rode a gauge nothing reads it from" % key)
        # THE CONTROL, on the same row and the same gauge in the same call,
        # and spelled WITHOUT a loop on purpose: every field the page DOES read
        # is still here, so the six absences above are a trim rather than a
        # projection that collapsed or a list that was empty all along.
        self.assertIn("account", row)
        self.assertIn("probed_at", row)
        self.assertIn("provider", row)
        self.assertIn("label", row["gauges"][0])
        self.assertIn("utilization", row["gauges"][0])
        self.assertIn("reset", row["gauges"][0])
        self.assertIn("label", row["gauges"][1])
        self.assertIn("utilization", row["gauges"][1])
        self.assertIn("reset", row["gauges"][1])

    def test_a_null_a_zero_and_a_false_all_survive_the_trim(self):
        """A FIELD THE PAGE READS IS CARRIED BECAUSE IT IS READ, never because
        it looked worth carrying. `reset: null` and `utilization: 0` are the two
        values a truthiness filter would silently eat, and both mean something:
        a gauge with no reset draws no ETA, and a gauge at 0 draws a FULL bar.
        Dropping either would be a partial reading as a complete one."""
        row = web._history_on_the_wire([{
            "account": "a", "probed_at": "2026-09-15T03:51:53Z", "provider": "x",
            "gauges": [{"label": "5h", "utilization": 0, "reset": None,
                        "kind": "session"}]}])[0]
        gauge = row["gauges"][0]
        self.assertIn("reset", gauge, "a null reset was eaten as absent")
        self.assertIsNone(gauge["reset"])
        self.assertIn("utilization", gauge)
        self.assertEqual(0, gauge["utilization"], "a zero gauge was eaten")
        # THE CONTROL on the same gauge: the trim IS running here, so the two
        # survivals above are the filter keeping them rather than no filter.
        self.assertNotIn("kind", gauge, "MUST-HIT: the trim did not run")

    def test_a_row_the_provider_gave_no_gauges_is_still_a_row(self):
        """A re-auth-needed probe carries `gauges: []`. It draws no series and
        every renderer skips it — but `quotaRenderAge` reads its `probed_at` to
        say how old the TABLE is, so a row dropped here would make the whole
        table look staler than it is."""
        rows = web._history_on_the_wire([
            {"account": "a", "probed_at": "2026-09-15T03:51:53Z",
             "provider": "x", "gauges": [], "status": "reauth-needed"},
            dict(self.FULL_ROW)])
        self.assertEqual(2, len(rows), "the gaugeless probe was dropped")
        self.assertEqual([], rows[0]["gauges"],
                         "an empty gauge list became an absent one")
        self.assertIn("probed_at", rows[0])

    def test_every_row_and_every_identity_survives_the_trim(self):
        """THE CURE IS FEWER BYTES, NEVER FEWER ROWS. A reader counting this
        list, or ordering it, must get the answer it always did."""
        src = [dict(self.FULL_ROW, probed_at="2026-09-15T0%d:00:00Z" % n,
                    account="acct-%d" % n) for n in range(1, 6)]
        rows = web._history_on_the_wire(src)
        # THE UNCONDITIONAL CONTROL on the same observable: five rows in, five
        # rows out. Two empty lists are also equal.
        self.assertEqual(5, len(src))
        self.assertEqual(5, len(rows), "the trim changed how many rows exist")
        self.assertEqual([r["probed_at"] for r in src],
                         [r["probed_at"] for r in rows])
        self.assertEqual([r["account"] for r in src],
                         [r["account"] for r in rows])

    def test_the_trim_is_at_emit_so_burn_still_joins_the_full_projection(self):
        """`get_burn` reads the SAME cached history list. If the trim had gone
        one function inwards it would have taken the provider's own fields off
        the burn join too, and nothing on the burn surface would have said so."""
        import inspect
        source = inspect.getsource(web.get_history)
        self.assertNotIn("_history_on_the_wire", source,
                         "the trim moved inside get_history, where burn reads")
        # THE CONTROL: the trim really is wired to the door, so the absence
        # above is placement rather than a cure that never landed.
        self.assertIn("_history_on_the_wire", inspect.getsource(web._api_history))


class HistoryWireReadersTest(unittest.TestCase):
    """THE BROWSER HALF, RUN RATHER THAN GREPPED.

    A no-reader claim proved by searching for a field name is only as good as
    the spellings the searcher thought of. This arm does not search: it lifts
    the five functions that read the history list OUT OF THE SHIPPED PAGE,
    executes them under node over the payload the trimmed door actually emits,
    and compares their answers to the same functions over the untrimmed rows.
    Equal answers mean no reader lost anything — in the renderer's own code,
    not in a python mirror of it.

    Node is optional on non-web hosts: absent, this SKIPS.
    """

    EXTRACT = ("currentProbes", "gaugeLabels", "seriesFor", "latestGauges")

    SUPPORT = r"""
let CREDS = [], HIST = [];
const ROSTER = () => new Set(CREDS.map(c => c.name));
"""

    DRIVER = r"""
/* The three payloads come from PYTHON, not from a shape retyped here: FULL is
   what the provider hands the cache, TRIM is what the real emit-time trim made
   of it, and LOSSY is FULL with one field the page DOES read taken out. */
const read = () => ({
  probes: currentProbes().map(r => [r.account, r.probed_at]),
  labels: gaugeLabels(),
  series5: seriesFor("5h"),
  series7: seriesFor("7d"),
  latest: latestGauges(ACCT),
});
const out = {};
HIST = FULL; out.full = read();
HIST = TRIM; out.trim = read();
HIST = LOSSY; out.lossy = read();
console.log(JSON.stringify(out));
"""

    @classmethod
    def setUpClass(cls):
        import copy
        import shutil as _sh
        import subprocess
        from helm import web_ui_loader
        from tests.test_web_chat_client_runtime import _extract_fn
        cls.node = _sh.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        acct = "alice@example.com"
        gauge = lambda label, kind, util, reset: {
            "label": label, "kind": kind, "utilization": util, "reset": reset,
            "limit": None, "remaining": None}
        full = [
            {"provider": "anthropic", "account": acct, "status": "allowed",
             "primary": "5h", "source_at": None,
             "probed_at": "2026-09-15T03:00:00Z",
             "gauges": [gauge("5h", "session", 0.06, 1789446000),
                        gauge("7d", "period", 0.89, 1789563600)]},
            {"provider": "anthropic", "account": acct, "status": "blocked",
             "primary": "7d", "source_at": None,
             "probed_at": "2026-09-15T09:00:00Z",
             "gauges": [gauge("5h", "session", 0.0, None),
                        gauge("7d", "period", 0.91, 1789563600),
                        gauge("7d-extra", "period", 0.5, None)]},
            {"provider": "codex", "account": "bob@example.com",
             "status": "reauth-needed", "primary": None, "source_at": None,
             "probed_at": "2026-09-15T09:05:00Z", "gauges": []},
        ]
        # LOSSY drops `reset` — a field the page reads — from every gauge. It is
        # the unconditional positive control for the comparison itself: without
        # it, "trim equals full" would also pass for a `read()` that returned a
        # constant, or for two payloads neither of which the readers can see.
        lossy = copy.deepcopy(full)
        for row in lossy:
            for g in row["gauges"]:
                g.pop("reset", None)
        trim = web._history_on_the_wire(copy.deepcopy(full))
        src = web_ui_loader.read_text()
        fns = "\n\n".join(_extract_fn(src, n) for n in cls.EXTRACT)
        consts = ("const ACCT = %s;\nconst FULL = %s;\nconst TRIM = %s;\n"
                  "const LOSSY = %s;\nCREDS = [{name: %s}, {name: \"bob@example.com\"}];\n"
                  % (json.dumps(acct), json.dumps(full), json.dumps(trim),
                     json.dumps(lossy), json.dumps(acct)))
        cls.tmp = tempfile.mkdtemp(prefix="helm-web-history-readers-")
        cls.path = os.path.join(cls.tmp, "run.js")
        with open(cls.path, "w", encoding="utf-8") as f:
            f.write(cls.SUPPORT + consts + fns + cls.DRIVER)
        chk = subprocess.run([cls.node, "--check", cls.path],
                             capture_output=True, text=True)
        assert chk.returncode == 0, "node --check failed:\n" + chk.stderr
        cls.proc = subprocess.run([cls.node, cls.path], capture_output=True,
                                  text=True, timeout=60)
        try:
            cls.out = json.loads(cls.proc.stdout or "{}")
        except json.JSONDecodeError:
            cls.out = {}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def setUp(self):
        self.assertTrue(self.out, "node produced no output: "
                        + (self.proc.stderr or "")[:400])

    def test_the_shipped_readers_cannot_tell_the_trimmed_payload_from_the_full_one(self):
        """THE NO-READER PROOF, EXECUTED. Every reading the quota chart and the
        accounts table make off the history list is identical over the trimmed
        payload and the untrimmed one — the windows they draw, the account
        series behind each line, the reset each line projects to, and the newest
        probe of each account."""
        # THE UNCONDITIONAL CONTROL on the same observable: both sides drew
        # real windows. Two empty readings are also equal.
        self.assertEqual(["5h", "7d", "7d-extra"], self.out["full"]["labels"])
        self.assertEqual(["5h", "7d", "7d-extra"], self.out["trim"]["labels"])
        self.assertEqual(self.out["full"], self.out["trim"],
                         "a shipped reader saw a difference the trim made")

    def test_the_comparison_can_see_a_lost_field_so_the_equality_means_something(self):
        """THE UNCONDITIONAL POSITIVE CONTROL, on the same observable in the
        same node run. `reset` is a field the page DOES read, and a payload
        missing it produces a DIFFERENT reading — so the equality above is the
        trim being invisible rather than `read()` being blind."""
        # THE UNCONDITIONAL CONTROL on the same observable: the lossy side is a
        # real reading too — it still finds every window — so the inequality is
        # one field's worth of difference rather than a reader that went blind.
        self.assertEqual(["5h", "7d", "7d-extra"], self.out["lossy"]["labels"])
        self.assertNotEqual(self.out["full"], self.out["lossy"],
                            "dropping a field the page reads changed nothing, "
                            "so this comparison proves nothing about the trim")

    def test_the_readers_actually_read_something_on_this_fixture(self):
        """MUST-HIT. Two payloads that both render nothing are also equal. This
        is the arm that says the fixture reaches the code under test."""
        self.assertEqual(["5h", "7d", "7d-extra"], self.out["trim"]["labels"])
        self.assertTrue(self.out["trim"]["series5"],
                        "the 5h series is empty, so the equality above is "
                        "comparing two blanks")
        self.assertIn("5h", self.out["trim"]["latest"])
        self.assertEqual(2, len(self.out["trim"]["probes"]),
                         "currentProbes saw neither account")
