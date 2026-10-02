#!/usr/bin/env python3
"""FAMILIES ON FLEET › CREDIT, AND WORK'S LAST DUPLICATES (task/3445 L4, L5).

Everything measured once for the whole fleet is drawn once, on Fleet › credit:
the family strip and sheet (the burn colours), the shares across projects, the
lane capacity, the reset credits and the declare control. The Families card
heads that page and speaks its vocabulary: each model family names the
accounts that bill it, and each account group in "what may run right now"
names the families it serves. The two sides link both ways — a project's
Team-tab budget row opens its family on credit, and each share on the family
sheet opens that project's Team tab.

Work › projects keeps its capacity line's counts and no family chip, no burn
fold. "save team" is one control with one meaning, the project team's save;
the launch tray's preset has its own words. The pipeline's project filter
rides the URL: #work/pipeline?project=<key>, as the family does on credit:
#quota?family=<name>.

These arms read the SHIPPED page: where each element lives, and the routing
and header functions lifted out and run under node.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock
from urllib.parse import urlsplit

from helm import web_ui_loader
from tests.test_web_accounts import _extract_const
from tests.test_web_chat_client_runtime import _extract_fn
from tests.test_web_home import _view

_ESC = ('const esc = s => String(s ?? "").replace(/[&<>"\']/g, c => '
        '({"&":"&amp;","<":"&lt;",">":"&gt;",\'"\':"&quot;","\'":"&#39;"}[c]));\n')


def _node_json(test, script, prefix):
    """Run one node program and hand back what it printed as JSON."""
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node not available")
    tmp = tempfile.mkdtemp(prefix=prefix)
    test.addCleanup(shutil.rmtree, tmp, True)
    path = os.path.join(tmp, "run.js")
    with open(path, "w", encoding="utf-8") as f:
        f.write(script)
    proc = subprocess.run([node, path], capture_output=True, text=True,
                          timeout=60)
    test.assertEqual(proc.returncode, 0, proc.stderr[:1500])
    return json.loads(proc.stdout or "null")


class FamiliesPlacementTest(unittest.TestCase):
    """Node-free: where the Families card and its neighbours live now."""

    def setUp(self):
        self.src = web_ui_loader.read_text()

    def test_the_families_card_heads_fleet_credit_and_is_drawn_once(self):
        credit = _view(self.src, "quota")
        self.assertEqual(self.src.count('id="flagcard"'), 1)
        at = credit.index('id="flagcard"')
        for below in ('id="chartcard"', 'id="qonecard"', 'id="acctdeclared"'):
            self.assertLess(at, credit.index(below), below)
        for mark in ('id="flagoverall"', 'id="flagrows"'):
            self.assertIn(mark, credit, mark)

    def test_work_projects_keeps_its_counts_and_no_family_chip_or_burn_fold(self):
        work = _view(self.src, "work")
        self.assertIn('id="bheadline"', work)          # the capacity line stays
        for gone in ('id="flagcard"', "burn flags", "fchip"):
            self.assertNotIn(gone, work, gone)
        self.assertNotIn("function boardChip(", self.src)
        self.assertNotIn("fchip", _extract_fn(self.src, "boardCapacity"))

    def test_homes_supply_tile_opens_fleet_credit(self):
        home = _view(self.src, "home")
        self.assertRegex(home, r'<section class="htile" data-go="#quota">\s*'
                               r'<a class="hlab" href="#quota"(?: title="[^"]*")?>supply</a>')
        self.assertNotIn('data-open="flagcard"', home)

    def test_the_board_is_read_while_fleet_credit_is_on_screen(self):  # noqa: VACUOUS_ASSERTION — every view name is asserted PRESENT in boardShown; there is no absence in this arm
        shown = _extract_fn(self.src, "boardShown")
        for view in ("#view-work", "#view-home", "#view-quota"):
            self.assertIn(view, shown, view)

    def test_save_team_is_one_control_with_one_meaning(self):
        """Two buttons read "save team": the project team's save, and the
        launch tray's localStorage preset of sessions and accounts. The tray
        is a launch set, and says so."""
        self.assertEqual(self.src.count("save team"), 1)
        at = self.src.index("save team")
        self.assertIn("data-team-save=", self.src[at - 200:at])
        tray = self.src[self.src.index('<div id="tray">'):]
        tray = tray[:tray.index("</div></div>")]
        self.assertIn('id="savePreset">save launch set<', tray)
        self.assertNotIn("team", tray)

    def test_every_route_goes_through_one_query_reader_and_one_router(self):
        show = _extract_fn(self.src, "showView")
        self.assertIn("viewQuery(v)", show)
        boot = self.src[self.src.index("================= boot ================="):]
        for body in (boot[:boot.index("\n}\n")],
                     self.src[self.src.index('window.addEventListener("hashchange"'):]):
            head = body[:body.index("showView(v)")]
            self.assertIn("viewRoute(v, r.query)", head)


class BillingReadingTest(unittest.TestCase):
    """task/3461, r3 of the L4 cure: ONE reading of which account groups bill
    a family — `seat.billing_accounts` over the catalog's own routing, which
    the seeder mints its rows from and the credit page joins on. No table of
    vendors beside it: the one the r2 cure added billed ds4flash to deepseek
    while the catalog routes it through OpenCode Go."""

    @staticmethod
    def _vendor_tables(module):
        """Module-level mappings of every catalog family to a word or None —
        a second table of who bills each family, whatever it is called."""
        from helm import seat
        fams = set(seat.FAMILIES)
        return sorted(name for name, value in vars(module).items()
                      if isinstance(value, dict) and value is not seat.FAMILIES
                      and fams <= set(value)
                      and all(v is None or isinstance(v, str)
                              for v in value.values()))

    def test_there_is_no_second_table_beside_the_catalog(self):
        from helm import seat, seat_catalog
        self.assertTrue(callable(seat.billing_groups))       # the one reading
        self.assertEqual(self._vendor_tables(seat_catalog), [])
        # PLANTED: a parallel table under any name is found
        planted = dict.fromkeys(seat.FAMILIES, "somebody")
        with mock.patch.object(seat_catalog, "PLANTED_TABLE", planted,
                               create=True):
            self.assertEqual(self._vendor_tables(seat_catalog),
                             ["PLANTED_TABLE"])

    def test_the_catalog_names_each_familys_groups(self):
        """MEASURED against the catalog: ds4flash's one pool row is the
        OpenCode Go subscription (billed by opencode), ds4pro's rows are the
        DeepSeek direct key (its default, so first) and the Go subscription,
        a seat carrying one of them, and cursor's provider is its local
        bridge while its bill is Cursor."""
        from helm import burnflags, seat
        got = seat.billing_groups
        self.assertEqual(got("ds4flash"), ["opencode"])
        self.assertEqual(got("ds4pro"), ["deepseek", "opencode"])
        self.assertEqual(got("cursor"), ["cursor"])
        self.assertEqual(got("kimi"), ["moonshot"])
        self.assertEqual(got(burnflags.NATIVE_FAMILY), ["anthropic"])
        local = burnflags.local_families()
        self.assertTrue(local, "the control: the catalog serves some family "
                               "from the operator's own GPUs")
        for fam in local:
            self.assertEqual(got(fam), [], fam)                # no bill
        self.assertIsNone(got("no-such-family"))              # never a guess
        self.assertEqual(seat.billing_accounts("ds4flash"),
                         [("opencode-go", "opencode")])

    def test_a_pool_bills_in_route_order_and_a_local_row_bills_nobody(self):
        from helm import seat
        table = {"fam-mixed": {"mode": "proxy-key", "pool_default": "b-route",
                               "pool_providers": {
                                   "a-route": {"vendor": "Vendor-A"},
                                   "b-route": {},
                                   "own-box": {"base_url_from": "k"}}},
                 "fam-bridge": {"mode": "proxy-key", "provider": "the-bridge",
                                "vendor": "vendor-b"},
                 "fam-bare": {"mode": "proxy"}}
        self.assertEqual(seat.billing_accounts("fam-mixed", table),
                         [("b-route", "b-route"), ("a-route", "vendor-a")])
        self.assertEqual(seat.billing_groups("fam-mixed", table),
                         ["b-route", "vendor-a"])
        # an explicit vendor is read before the provider it bills through
        self.assertEqual(seat.billing_groups("fam-bridge", table), ["vendor-b"])
        # NO NAME FALLBACK: a family naming no vendor bills nobody named
        self.assertIsNone(seat.billing_groups("fam-bare", table))


def _bridged_without_vendor(table):
    """Families whose route ends at a process on THIS box — a sidecar, or a
    loopback base_url — that name no explicit `vendor`. Such a family's
    `provider` names the bridge, not the bill (cursor's is `cursor-bridge`),
    so the catalog must spell its bill."""
    out = []
    for name, fam in sorted(table.items()):
        if not isinstance(fam, dict) or fam.get("pool_providers"):
            continue
        host = urlsplit(str(fam.get("base_url") or "")).hostname or ""
        bridged = fam.get("sidecar") or host in ("127.0.0.1", "localhost", "::1")
        if bridged and not fam.get("vendor"):
            out.append(name)
    return out


class BillingJoinClassTest(unittest.TestCase):
    """THE CLASS THE REVIEWER NAMED (task/3445 L4 r3, task/3461).

    On a temporary helm home, `helm accounts seed` runs over the REAL catalog;
    the board's flags section is built by the real server join; and the credit
    page's real renderers group the seeded rows into the accounts table and
    draw each family's billed line. Then, for EVERY family: a billed family
    links every group it is billed to, and each of those groups names it among
    the families it serves; a local GPU family links none and says so; every
    group the seeder minted serves a family. A future catalog family whose
    bill would reach the page under another spelling, or not at all, fails
    here — the planted arms below prove the check can fail."""

    #: the measured half as the quota provider sends it: the native claude
    #: logins and the codex logins, which the seeder never mints rows for
    MEASURED = [{"name": "m-native", "provider": "anthropic"},
                {"name": "m-codex", "provider": "codex"}]

    def setUp(self):
        from helm import accountseed
        tmp = tempfile.mkdtemp(prefix="helm-billing-join-")
        self.addCleanup(shutil.rmtree, tmp, True)
        patches = (mock.patch.dict(os.environ, {
                       "HELM_ACCOUNTS": os.path.join(tmp, "accounts.json")}),
                   # Orca's store is a real directory on the real host
                   mock.patch.object(accountseed, "orca_identities",
                                     lambda *_a, **_k: []))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def page(self, extra=None):
        """(violations, drawn) for a fresh home seeded from the catalog plus
        `extra` families."""
        from helm import accounts, accountseed, burnflags, seat, web_board
        # seat.FAMILIES IS the catalog's table (the facade re-exports it), so
        # a family patched in here is one every reader of the catalog sees
        with mock.patch.dict(seat.FAMILIES, extra or {}):
            _written, _skipped, err = accounts.seed(self.MEASURED)
            self.assertIsNone(err, err)
            fams = sorted(set(seat.FAMILIES) | {burnflags.NATIVE_FAMILY})
            local = set(burnflags.local_families())
            snap = {"measured": True, "measured_at": time.time(),
                    "bound_s": 600, "overall": None,
                    "families": {f: {"colour": "GREEN"} for f in fams}}
            with mock.patch.object(web_board, "get_flags", lambda: snap):
                flags = web_board._flags_section()
        decl = accounts.read()["accounts"]
        creds = [{"name": m["name"], "provider": m["provider"], "state": "ok",
                  "subscription": "sub-" + m["name"]} for m in self.MEASURED]
        src = web_ui_loader.read_text()
        fns = "\n\n".join(_extract_fn(src, n) for n in (
            "quotaRows", "quotaAliasOrder", "quotaDeclOrder", "quotaFamilies",
            "quotaServes", "famBilled"))
        drawn = _node_json(self, _ESC + _extract_const(src, "QFAM_FIRST") + "\n"
                           + "const CREDS = %s;\nlet DECL = {accounts: %s};\n"
                             "const FLAGS = %s;\nlet QUOTA_GROUPS = null;\n"
                           % (json.dumps(creds), json.dumps(decl),
                              json.dumps(flags)) + fns + r"""
const groups = quotaFamilies(quotaRows()).map(g => g[0]);
QUOTA_GROUPS = new Set(groups);
const out = {groups, lines: {}, serves: {}};
for (const f of Object.keys(FLAGS.families)) out.lines[f] = famBilled(FLAGS.families[f]);
for (const g of groups) out.serves[g] = quotaServes(g, FLAGS);
console.log(JSON.stringify(out));
""", "helm-billing-join-")
        bad = []
        for fam in fams:
            bills, line = flags["families"][fam].get("bills"), drawn["lines"][fam]
            if bills is None:
                bad.append((fam, "the catalog names no bill"))
            elif (fam in local) != (bills == []):
                bad.append((fam, "local and billed disagree"))
            elif not bills:
                if "local GPU, no bill" not in line or "data-qfam" in line:
                    bad.append((fam, "a local family drew a bill"))
            for group in bills or ():
                if 'data-qfam="%s"' % group not in line:
                    bad.append((fam, "no account group %s on the page" % group))
                elif fam not in drawn["serves"].get(group, []):
                    bad.append((fam, "group %s does not name it" % group))
        for group, served in sorted(drawn["serves"].items()):
            if set(served) & local:
                bad.append((group, "serves a local family"))
        for row in decl:
            group = (row.get("vendor") or "").lower()
            if row.get("seeded_from") == accountseed.SEEDED_FROM \
                    and not drawn["serves"].get(group):
                bad.append((row["id"], "seeded group %s serves nothing" % group))
        return bad, drawn

    def test_every_billed_family_reaches_its_account_groups(self):
        bad, drawn = self.page()
        # THE CONTROLS on the same run: the page drew seeded groups, some
        # family links one, and a local family was drawn with no bill.
        self.assertIn("opencode", drawn["groups"])
        self.assertIn('data-qfam="cursor"', drawn["lines"]["cursor"])
        self.assertIn("local GPU, no bill", drawn["lines"]["qwen27"])
        self.assertEqual(bad, [])

    def test_a_family_whose_bill_would_not_reach_the_page_fails(self):
        """PLANTED: a family whose pool provider shares an account id with
        ds4pro's under another vendor (the seeder keeps one row per id), and a
        family that names no vendor at all."""
        bad, _drawn = self.page({
            "zz-collide": {"mode": "proxy-key", "pool_default": "deepseek",
                           "pool_providers": {"deepseek": {"vendor": "elsewhere"}}},
            "zz-unnamed": {"mode": "proxy-key"}})
        self.assertEqual(sorted({fam for fam, _why in bad}),
                         ["zz-collide", "zz-unnamed"], bad)

    def test_a_family_bridged_on_this_box_names_its_bill(self):
        """cursor's provider is its local bridge, `cursor-bridge`, which no
        account is billed as: a family whose route ends at a process on this
        box spells its bill with `vendor`."""
        from helm import seat
        self.assertEqual(_bridged_without_vendor(seat.FAMILIES), [])
        planted = dict(seat.FAMILIES["cursor"])
        planted.pop("vendor", None)
        self.assertEqual(_bridged_without_vendor({"cursor": planted}), ["cursor"])

    def test_a_pool_rows_billing_window_names_the_same_vendor(self):  # noqa: VACUOUS_ASSERTION — `seen` counts the windows the loop compared and `assertTrue(seen)` refuses a sweep that compared none
        """Two spellings of one fact must agree: a pool row spent only in a
        vendor's billing window bills that vendor's group."""
        from helm import seat
        seen = 0
        for family, fam in sorted(seat.FAMILIES.items()):
            rows = fam.get("pool_providers") or {}
            for account, group in seat.billing_accounts(family) or ():
                window = (rows.get(account) or {}).get("billing_window")
                if window:
                    seen += 1
                    self.assertEqual(group, window["vendor"], family)
        self.assertTrue(seen, "the control: some pool row carries a window")


class CureWiringTest(unittest.TestCase):
    """The L4/L5 cure's wiring, read off the shipped page."""

    def setUp(self):
        self.src = web_ui_loader.read_text()

    def test_a_family_link_scrolls_its_card_below_the_sticky_nav(self):
        route = _extract_fn(self.src, "famRoute")
        self.assertIn("scrollIntoView", route)
        self.assertIn("#flagcard", route)
        self.assertRegex(self.src, r"#flagcard\{[^}]*scroll-margin-top:calc\(var\(--navh")

    def test_the_families_header_wraps_on_a_phone(self):
        self.assertIn("#flagcard .qonetop .gmeta{white-space:normal}", self.src)

    def test_a_declare_being_typed_is_not_redrawn_under_the_caret(self):
        render = _extract_fn(self.src, "famRender")
        self.assertIn("document.activeElement", render)
        self.assertIn("force", render)
        self.assertIn("famRender(true)", _extract_fn(self.src, "famDeclare"))

    def test_an_unknown_family_or_project_falls_back_and_the_url_says_so(self):
        fam = _extract_fn(self.src, "famRender")
        self.assertIn("FAM_SEL = null", fam)
        self.assertIn('viewHashSync("quota")', fam)
        # the Work page (task/3643; task/3482 D1, run in
        # tests/test_web_work_page.py ViewSquaredWithTheReadTest): a project
        # no read or registry knows falls back to all, and the address with it
        work = _extract_fn(self.src, "wkPaint")
        self.assertIn("wkNormalize(", work)
        self.assertIn("wkNavSync()", work)

    def test_the_accounts_table_publishes_its_groups_and_redraws_the_sheet(self):
        render = _extract_fn(self.src, "renderAccts")
        self.assertIn("QUOTA_GROUPS = ", render)
        self.assertIn("famRender()", render)

    def test_the_doc_names_the_served_chips_and_no_chip_colour_on_work(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, os.pardir, "docs", "WEB.md"), encoding="utf-8") as f:
            doc = f.read()
        self.assertIn("serves <families>", doc)
        self.assertNotIn("every chip's colour", doc)

    def test_the_stale_lines_are_gone(self):
        self.assertIn('id="flagcard"', self.src)                  # control
        for gone in ("moved here from the quota tab", "#flagcard>summary"):
            self.assertNotIn(gone, self.src, gone)


class CreditRuntimeTest(unittest.TestCase):
    """The routing and the account-group header, lifted and run."""

    DRIVER = r"""
const out = {};
let FAM_SEL = null, BOARD = null;
const RENDERS = [];
function famRender() { RENDERS.push("fam"); }
function projRoute(q) { RENDERS.push("proj:" + q); }
function projQuery() { return "?open=helm&tab=team"; }
// THE WORK PAGE stands in by name: its own address is run in
// tests/test_web_work_page.py AddressTest
const WK = {main: {view: {}}};
function wkQuery(view, v, lock, qkey) { return "?wk=" + v + "&key=" + qkey; }
function wkRoute(v, q) { RENDERS.push("wk:" + v + ":" + q); }
out.pipe = ["flow", "pipeline", "backlog"].map(viewQuery);
famRoute("family=grok");
out.fam = [FAM_SEL, famQuery(), viewQuery("quota")];
famRoute("");
out.fam_none = [FAM_SEL, famQuery()];
out.work = viewQuery("work");
out.other = viewQuery("chat");
RENDERS.length = 0;
viewRoute("work", "open=x"); viewRoute("pipeline", "project=beta");
viewRoute("quota", "family=kimi"); viewRoute("chat", "x=1");
out.routed = [RENDERS.slice(), FAM_SEL];
out.parse = [hashView("work/pipeline?project=helm"), hashView("quota?family=grok")];
const FLAGS = {families: {grok: {colour: "ORANGE", bills: ["xai"]},
                          kimi: {colour: "RED", bills: ["moonshot"]},
                          cursor: {colour: "YELLOW", bills: ["cursor"]},
                          gemini: {colour: "GREEN", bills: ["antigravity"]},
                          opus46: {colour: "GREY", bills: ["antigravity"]},
                          deepseek: {colour: "GREEN", bills: ["elsewhere"]},
                          twoway: {colour: "GREEN", bills: ["first-co", "second-co"]},
                          qwen27: {colour: "GREEN", bills: []}}};
out.serves = [quotaServes("xai", FLAGS), quotaServes("antigravity", FLAGS),
              quotaServes("cursor", FLAGS), quotaServes("deepseek", FLAGS),
              quotaServes("xai", null), quotaServes("second-co", FLAGS)];
out.head = quotaFamHeadHTML("xai", [{}, {}], 7, FLAGS);
out.head_bare = quotaFamHeadHTML("deepseek", [{}], 7, FLAGS);
// THE BILLED LINE: every group in route order, a link only where the table
// carries it; before the table is read, and once it is read and empty
QUOTA_GROUPS = null;
out.two_unread = famBilled(FLAGS.families.twoway);
QUOTA_GROUPS = new Set(["second-co"]);
out.two = famBilled(FLAGS.families.twoway);
QUOTA_GROUPS = new Set();
out.two_empty = famBilled(FLAGS.families.twoway);
out.local = famBilled(FLAGS.families.qwen27);
out.unnamed = famBilled({colour: "GREEN", bills: null});
out.old_server = famBilled({colour: "GREEN"});
console.log(JSON.stringify(out));
"""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        src = web_ui_loader.read_text()
        fns = "\n\n".join(_extract_fn(src, n) for n in (
            "canonView", "viewHash", "hashView", "pageOf",
            "famQuery", "famRoute", "viewQuery", "viewRoute", "quotaServes",
            "quotaFamHeadHTML", "famBilled"))
        support = (_ESC + "const QFOLD = {};\nconst $ = () => null;\n"
                   "let QUOTA_GROUPS = null;\n")
        cls.tmp = tempfile.mkdtemp(prefix="helm-web-credit-")
        path = os.path.join(cls.tmp, "run.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(support + _extract_const(src, "FLAGCOL") + "\n" + fns + cls.DRIVER)
        chk = subprocess.run([cls.node, "--check", path], capture_output=True, text=True)
        assert chk.returncode == 0, "node --check failed:\n" + chk.stderr
        cls.proc = subprocess.run([cls.node, path], capture_output=True,
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
                        + (self.proc.stderr or "")[:1200])

    def test_the_work_pages_views_hand_the_url_to_the_work_page(self):
        """The Work page and its pipeline and backlog entries each write
        their state through the Work page's one query (task/3643)."""
        self.assertEqual(self.out["pipe"], ["?wk=flow&key=q", "?wk=pipeline&key=q",
                                            "?wk=backlog&key=q"])
        self.assertEqual(self.out["parse"][0],
                         {"v": "pipeline", "path": "work/pipeline", "query": "project=helm"})

    def test_the_family_on_credit_rides_the_url(self):
        self.assertEqual(self.out["fam"], ["grok", "?family=grok", "?family=grok"])
        self.assertEqual(self.out["fam_none"], [None, ""])
        self.assertEqual(self.out["parse"][1],
                         {"v": "quota", "path": "quota", "query": "family=grok"})

    def test_one_router_hands_each_view_its_own_query(self):
        self.assertEqual(self.out["work"], "?open=helm&tab=team")
        self.assertEqual(self.out["other"], "")
        self.assertEqual(self.out["routed"],
                         [["proj:open=x", "wk:pipeline:project=beta", "fam"], "kimi"])

    def test_an_account_group_names_the_families_it_serves(self):
        """The vendor that bills an account, and the model families that
        vendor serves: ONE key, the family's billing vendor. A family named
        like a group but billed elsewhere is not served by it (no name
        fallback), and a local family is served by none."""
        self.assertEqual(self.out["serves"], [["grok"], ["gemini", "opus46"],
                                              ["cursor"], [], [], ["twoway"]])
        self.assertNotIn("serves", self.out["head_bare"])     # deepseek: billed elsewhere
        head = self.out["head"]
        text = re.sub(r"<[^>]*>", "", head)
        self.assertIn("xai", text)
        self.assertIn("serves grok", text)
        self.assertIn("2 accounts", text)
        self.assertIn("#c9772e", head)                   # grok's ORANGE
        self.assertIn('data-fam="xai"', head)
        self.assertNotIn("serves", self.out["head_bare"])
        self.assertIn("1 account", re.sub(r"<[^>]*>", "", self.out["head_bare"]))

    def test_the_billed_line_names_every_group_in_route_order(self):  # noqa: VACUOUS_ASSERTION — the link that IS drawn (`data-qfam="second-co"`) is asserted first on the same renderer's output, so the absences below are about lines that drew
        """A family billed through two groups says so, first then second,
        and each is a link only where the accounts table carries it."""
        text = lambda k: re.sub(r"<[^>]*>", "", self.out[k])  # noqa: E731
        two = self.out["two"]
        self.assertIn('data-qfam="second-co"', two)
        self.assertNotIn('data-qfam="first-co"', two)
        self.assertRegex(text("two"), r"billed to first-co .*first, then second-co")
        self.assertLess(two.index("first-co"), two.index("second-co"))
        self.assertIn("no account on this page", text("two"))
        self.assertIn("first-co first, then second-co — accounts not read yet",
                      text("two_unread"))
        self.assertIn("no accounts declared", text("two_empty"))
        for key in ("two_unread", "two_empty", "local", "unnamed"):
            self.assertNotIn("data-qfam", self.out[key], key)
        self.assertIn("local GPU, no bill", text("local"))
        self.assertIn("names no account that bills it", text("unnamed"))
        self.assertEqual(self.out["old_server"], "")


class EmptyAccountsTableTest(unittest.TestCase):
    """The minor of r2: with zero rows `renderAccts` returned before it set
    QUOTA_GROUPS, so every family sheet said "accounts not read yet" forever.
    A table that was read and is empty says so; one whose sources could not
    be read stays unread, never "no accounts"."""

    def test_an_empty_table_is_read_and_says_no_accounts(self):
        src = web_ui_loader.read_text()
        fns = "\n\n".join(_extract_fn(src, n) for n in ("renderAccts", "famBilled"))
        out = _node_json(self, _ESC + r"""
let QUOTA_GROUPS = null, CREDS_UNREADABLE = null;
let CREDS_READ = false, DECL_READ = false;
let DECL = {accounts: [], unreadable: null};
const ELS = {"#qonehead": {innerHTML: ""}, "#qonebody": {innerHTML: "x"}};
const $ = s => ELS[s] || null;
const gaugeLabels = () => [], quotaHeadHTML = () => "", quotaRows = () => [];
const quotaRenderAge = () => {}, acctCountRender = () => {};
let DRAWN = 0;
function famRender() { DRAWN++; }
""" + fns + r"""
const out = {};
// MEASURED LIVE: the board redraws the table before either read has landed
renderAccts();
out.before = [QUOTA_GROUPS, famBilled({bills: ["cursor"]})];
CREDS_READ = true;
renderAccts();
out.half = QUOTA_GROUPS;
DECL_READ = true;
renderAccts();
out.empty = [QUOTA_GROUPS instanceof Set, QUOTA_GROUPS ? QUOTA_GROUPS.size : -1,
             DRAWN, ELS["#qonebody"].innerHTML, famBilled({bills: ["cursor"]})];
QUOTA_GROUPS = null; DECL.unreadable = "the inventory could not be read";
renderAccts();
out.unreadable = [QUOTA_GROUPS, famBilled({bills: ["cursor"]})];
console.log(JSON.stringify(out));
""", "helm-empty-accts-")
        self.assertEqual(out["empty"][:4], [True, 0, 3, ""])   # 3 redraws
        self.assertIn("no accounts declared", out["empty"][4])
        self.assertIsNone(out["before"][0])
        self.assertIn("accounts not read yet", out["before"][1])
        self.assertIsNone(out["half"])          # the declared read is still out
        self.assertIsNone(out["unreadable"][0])
        self.assertIn("accounts not read yet", out["unreadable"][1])

    def test_an_unreadable_source_publishes_no_groups_even_with_rows(self):
        """With rows on the table, an inventory (or a measured read) that
        could not be read must not read as absence: its groups are unknown,
        so the Families card says the accounts are not read rather than "no
        account on this page carries that name" for a group only it holds."""
        src = web_ui_loader.read_text()
        fns = "\n\n".join(_extract_fn(src, n) for n in ("renderAccts", "famBilled"))
        out = _node_json(self, _ESC + r"""
let QUOTA_GROUPS = null, CREDS_UNREADABLE = null;
let CREDS_READ = true, DECL_READ = true;
let DECL = {accounts: [], unreadable: null};
const QFOLD = {};
const ELS = {"#qonehead": {innerHTML: ""}, "#qonebody": {innerHTML: ""}};
const $ = s => ELS[s] || null;
const gaugeLabels = () => [], quotaHeadHTML = () => "";
const quotaRows = () => [{fam: "codex"}];
const quotaFamilies = rows => [["codex", rows]];
const boardSec = () => null, boardSecState = () => "unread";
const quotaFamHeadHTML = () => "", acctDefaultSort = rs => rs, acctTier = () => 0;
const acctTierRow = () => "", quotaRowHTML = () => "", quotaBind = () => {};
const quotaRenderAge = () => {}, acctCountRender = () => {};
function famRender() {}
""" + fns + r"""
const out = {};
renderAccts();
out.read = [QUOTA_GROUPS ? [...QUOTA_GROUPS] : null, famBilled({bills: ["codex", "cursor"]})];
DECL.unreadable = "the inventory could not be read";
renderAccts();
out.decl = [QUOTA_GROUPS, famBilled({bills: ["codex", "cursor"]})];
DECL.unreadable = null; CREDS_UNREADABLE = "the provider reads failed";
renderAccts();
out.creds = [QUOTA_GROUPS, famBilled({bills: ["codex", "cursor"]})];
console.log(JSON.stringify(out));
""", "helm-unreadable-accts-")
        self.assertEqual(out["read"][0], ["codex"])                     # the control
        self.assertIn('data-qfam="codex"', out["read"][1])
        self.assertIn("no account on this page carries that name", out["read"][1])
        for key in ("decl", "creds"):
            self.assertIsNone(out[key][0], key)
            self.assertIn("accounts not read yet", out[key][1], key)
            self.assertNotIn("no account on this page carries that name", out[key][1], key)

    def test_the_table_publishes_its_groups_only_once_both_reads_landed(self):  # noqa: VACUOUS_ASSERTION — the loop is over a two-item literal, so both loaders' assertions always run
        """…and with rows too: groups drawn from the measured rows alone
        would name every declared-only group absent for the moment the
        inventory is still loading."""
        src = web_ui_loader.read_text()
        render = _extract_fn(src, "renderAccts")
        self.assertIn("CREDS_READ && DECL_READ", render)
        self.assertRegex(render, r"QUOTA_GROUPS = read \? new Set\(groups\.map")
        for fn, flag in (("loadCreds", "CREDS_READ = true"),
                         ("loadDeclared", "DECL_READ = true")):
            body = _extract_fn(src, fn)
            self.assertIn(flag, body, fn)
            self.assertLess(body.index(flag), body.index("renderAccts()"), fn)


class CreditPhoneWrapTest(unittest.TestCase):
    """At 420 px the hints on Fleet › credit's folded cards (#acctdeclared,
    #burncard, #homescard) were CLIPPED by the card's overflow:hidden, since
    `.gmeta` never wraps. A card summary's hint wraps, and on a phone it takes
    its own line under the title."""

    def test_a_card_summarys_hint_wraps_instead_of_clipping(self):  # noqa: VACUOUS_ASSERTION — the three cards and the wrap rules are asserted present on the same assembled page before any absence
        src = web_ui_loader.read_text()
        credit = _view(src, "quota")
        for card in ("acctdeclared", "burncard", "homescard"):
            self.assertIn('<details class="grp" id="%s"' % card, credit, card)
        self.assertRegex(src, r"\.grp>summary\{[^}]*flex-wrap:wrap")
        self.assertRegex(src, r"\.grp>summary \.gmeta\{[^}]*white-space:normal")
        self.assertRegex(src, r"@media \(max-width:680px\)\{[^@]*\.grp>summary "
                              r"\.gmeta\{[^}]*flex-basis:100%")
        # nothing narrower puts the nowrap back on these three hints
        for card in ("acctdeclared", "burncard", "homescard"):
            self.assertNotRegex(src, r"#%s>summary \.gmeta\{[^}]*nowrap" % card)


if __name__ == "__main__":
    unittest.main()
