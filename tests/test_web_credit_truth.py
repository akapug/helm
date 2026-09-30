#!/usr/bin/env python3
"""FLEET › CREDIT TELLS ONE TRUTH ABOUT THE ACCOUNTS (task/3635, 2636, 3634).

Three owner-surface defects on the credit page, measured on the live console
(console walk 2, findings 5, 7 and 8):

1. ONE COUNT. The nav and Home said "14 accounts" with no scope: that was the
   number of credential HOMES the provider enumerates. The page's account
   groups added up to 24, and the declared card said "26 accounts declared ·
   26 subscriptions", counting three rows that name one login three times.
   Every one of those numbers now comes from the table's own rows (one row per
   paid subscription, declared and measured joined), is said with its scope in
   words, and the groups add up to it. The declared total counts a
   subscription once however many rows name it.

2. THE TOKEN LINEAGE IS VISIBLE. /api/creds carries each claude home's token
   lineage (MATCH / MISMATCH / AMBIGUOUS / UNKNOWN), and the page drew a flag
   only for MISMATCH and AMBIGUOUS — so a home whose check could not run looked
   exactly like one that passed. Every claude home on the table now shows its
   lineage word as text, UNKNOWN with its reason in plain words, and the
   account name that comes from the home's metadata says so.

3. A SPENT ACCOUNT IS NOT A LOGIN CHORE. An exhausted account offered to copy
   a login command, and two exhausted accounts sat under "use these" because a
   live agent was on them. A login does not refill a spent allowance: the row
   says it is spent and when it comes back, offers no command, and sits with
   the walled rows. A login is named only where it is the fix (an expired or
   revoked token), in plain words: the account needs signing in again, and an
   agent can start the login for him (task/3735 retired the copied terminal
   step task/3634 drew here: the console names no command).

FOUR MORE PROPERTIES, each with an arm here or in tests/test_providers.py.
F1 a row declared "free" is an account and is never counted or worded as
paid. F2 `helm accounts --json`, the human `helm accounts` and the web card
read one totals rule and agree. F3 a codex auth.json that is valid JSON but
not an object leaves that one home's login unknown, with the reason named,
and never makes the whole measured census unreadable. F4 spent rows sort by
when their spent window comes back, the time their chip says, never by the
session window's sooner reset.

The page functions are LIFTED from the assembled console and RUN under node
against one fixture shaped like the live /api/creds and /api/accounts answers.
Every address here is built at run time from example.test parts; no real
account, login or token appears in this file.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-credit-truth-", var="HELM_HOME")

from helm import accounts, web, web_ui_loader  # noqa: E402
from tests.test_web_accounts import _extract_const, measuring  # noqa: E402
from tests.test_web_chat_client_runtime import _extract_fn  # noqa: E402

AT = "@"
ANN = "ann" + AT + "example.test"
BOB = "bob" + AT + "example.test"
CY = "cy" + AT + "example.test"
DEE = "dee" + AT + "example.test"
EVE = "eve" + AT + "example.test"
CX = "cx" + AT + "example.test"


def _lin(state, accounts_=(), metadata=None, reason=None):
    return {"state": state, "tag": "0123abcd" if state != "UNKNOWN" else None,
            "accounts": list(accounts_), "metadata": metadata,
            "reason": reason, "chrome": "UNKNOWN"}


def _cred(name, provider, sub, **kw):
    row = {"name": name, "provider": provider, "subscription": sub,
           "measured_key": "m-" + sub, "home": "/h/" + sub,
           "home_name": sub, "identity": None, "name_lies": False,
           "token_lineage": None, "active": False, "tier": None,
           "headroom": None, "state": "ok", "status": "allowed",
           "resets_at_ms": None, "windows_left": None,
           "windows_per_week": None, "windows_verdict": None,
           "member_unproven": False, "pace_5h": None}
    row.update(kw)
    return row


# THE LIVE SHAPE, in miniature. One claude login reached through two homes and
# the default pointer; a claude account whose weekly allowance is spent while
# a live agent is on it and its session window is untouched; one whose token
# expired; a codex login reached through its home and the default pointer;
# a spent codex account; two declared accounts nothing measures.
CREDS = [
    _cred(ANN, "anthropic", "s-ann", home_name="ann-example-test",
          identity=ANN, state="exhausted", status="blocked", headroom=100.0,
          active=True, tier="Team", resets_in_h=2,
          token_lineage=_lin("MATCH", [ANN], ANN)),
    # F4: spent too, its SESSION window resets sooner than ann's (1h against
    # 2h) and its spent weekly one much later (4d against 20h)
    _cred(EVE, "anthropic", "s-eve", home_name="eve-example-test",
          identity=EVE, state="exhausted", status="blocked", headroom=100.0,
          tier="Max 20x", resets_in_h=1,
          token_lineage=_lin("MATCH", [EVE], EVE)),
    _cred(BOB, "anthropic", "s-bob", home_name="bob-example-test",
          identity=BOB, state="due-refresh", headroom=None, tier="Max 20x",
          status="keepalive due",
          token_lineage=_lin("UNKNOWN", metadata=BOB,
                             reason=".credentials.json carries no refresh token")),
    _cred(BOB + "#bob-two", "anthropic", "s-bob", home_name="bob-two",
          identity=BOB, headroom=60.0, tier="Max 20x",
          token_lineage=_lin("MATCH", [BOB], BOB)),
    _cred("(default-claude)", "anthropic", "s-bob", home_name=".claude",
          identity=BOB, headroom=60.0, tier="Max 20x",
          token_lineage=_lin("UNKNOWN", metadata=BOB,
                             reason="family never recorded under an account "
                                    "by a census")),
    _cred(CY, "anthropic", "s-cy", home_name="cy-example-test", identity=CY,
          state="expired-token", headroom=None, tier="Max 20x",
          status="reauth-needed (helm's token expired 3d ago)",
          token_lineage=_lin("MISMATCH", [DEE], CY)),
    _cred("cx-one", "codex", "s-cx1", state="exhausted", status="blocked",
          headroom=0.0, tier="Team", resets_in_h=96),
    _cred("cx-two", "codex", "s-cx2", headroom=70.0, tier="Pro",
          resets_in_h=90),
    _cred("(default-codex)", "codex", "s-cx2", home_name=".codex",
          headroom=70.0, tier="Pro", resets_in_h=90),
]


def _decl(id_, vendor, sub=None, **kw):
    row = {"id": id_, "vendor": vendor, "plan": "Plan", "count": 1,
           "good_for": "work", "not_for": "nothing", "reach": "a seat",
           "subscription": sub, "measured_as": None, "price_value": None,
           "duplicate_of": [], "points_at_default": False,
           "vendor_siblings": [], "needs_describe": False,
           "confirmed": False}
    row.update(kw)
    return row


DECL = [
    _decl("anthropic-ann", "anthropic", "s-ann", measured_as=ANN),
    _decl("anthropic-bob", "anthropic", "s-bob", measured_as=BOB,
          duplicate_of=["anthropic-bob-two", "anthropic-default"]),
    _decl("anthropic-bob-two", "anthropic", "s-bob",
          measured_as=BOB + "#bob-two",
          duplicate_of=["anthropic-bob", "anthropic-default"]),
    _decl("anthropic-default", "anthropic", "s-bob",
          measured_as="(default-claude)", points_at_default=True,
          duplicate_of=["anthropic-bob", "anthropic-bob-two"]),
    _decl("codex-cx-two", "codex", "s-cx2", measured_as="cx-two"),
    _decl("codex-default", "codex", "s-cx2", measured_as="(default-codex)",
          points_at_default=True, duplicate_of=["codex-cx-two"]),
    _decl("kimi", "moonshot"),
    _decl("x-premium", "xai"),
    # F1: an account he declared FREE is an account, never a paid one
    _decl("free-tier", "gratis", billing="free", billed_free=True),
]

# the rows this fixture must draw: anthropic ann, bob, cy, eve; codex cx-one,
# cx-two; moonshot; xai; gratis — six measured, three declared and not
# measured; one declared free, so eight paid; three spent (ann, eve, cx-one)
TOTAL, MEASURED, FREE, WALLED = 9, 6, 1, 3

# THE PAGE, lifted. A name the shipped page does not define is skipped here and
# its probe below records the ReferenceError, so on a page without the cure
# every arm fails on its own assertion rather than the whole class erroring.
FNS = ("fmtIn", "statCell", "latestGauges", "quotaRows", "quotaAliasOrder",
       "quotaDeclOrder", "quotaFamilies", "acctTier", "acctResetAt",
       "acctDefaultSort", "quotaIdCell", "homeWord", "homeTile",
       "homeAccounts", "declTotalsHTML", "quotaResetCell",
       # the cure's own names
       "acctNoun", "acctCensus", "acctCensusWords", "acctCountRender",
       "acctNeedsLogin", "acctBackAt", "acctHomeLines", "linWhy", "linSaid")
CONSTS = ("QFAM_FIRST", "ATIER", "ATIER_SAID", "CRED_SAID", "LIN_SAID")

SUPPORT = r"""
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const short = (n, l = 26) => n.length > l ? n.slice(0, l - 1) + "…" : n;
const acol = () => "#999";
const NOW = Date.now();
let SEL = {anthropic: null, codex: null}, NAV_ACC = "", NAV_RENDERS = 0;
let ACCT_CENSUS = null;
const ELS = {"#qonecount": {textContent: ""}};
const $ = s => ELS[s] || null;
function renderNav() { NAV_RENDERS++; }
for (const c of CREDS) {
  if (c.resets_in_h != null) c.resets_at_ms = NOW + c.resets_in_h * 3600e3;
  delete c.resets_in_h;
}
const HIST = [
  {account: ANN, provider: "anthropic", probed_at: new Date(NOW - 60e3).toISOString(),
   gauges: [{label: "5h", utilization: 0, reset: (NOW + 2 * 3600e3) / 1000},
            {label: "7d", utilization: 1.0, reset: (NOW + 20 * 3600e3) / 1000}]},
  {account: EVE, provider: "anthropic", probed_at: new Date(NOW - 60e3).toISOString(),
   gauges: [{label: "5h", utilization: 0, reset: (NOW + 1 * 3600e3) / 1000},
            {label: "7d", utilization: 1.0, reset: (NOW + 96 * 3600e3) / 1000}]},
  {account: "cx-one", provider: "codex", probed_at: new Date(NOW - 60e3).toISOString(),
   gauges: [{label: "7d", utilization: 1.0, reset: (NOW + 96 * 3600e3) / 1000}]},
];
"""

DRIVER = r"""
const out = {errors: {}};
const probe = (k, f) => { try { out[k] = f(); } catch (e) { out.errors[k] = String(e); } };
const rows = quotaRows();
const byName = n => rows.find(r => r.cred && r.cred.name === n);
out.rows = rows.map(r => ({fam: r.fam, name: r.name, sub: r.sub,
                           aliases: r.aliases.map(a => a.name)}));
out.group_sum = quotaFamilies(rows).reduce((n, g) => n + g[1].length, 0);
probe("census", () => acctCensus(rows));
probe("words", () => acctCensusWords(acctCensus(rows)));
probe("count_render", () => {
  acctCountRender(rows, true);
  return {nav: NAV_ACC, line: ELS["#qonecount"].textContent,
          census: ACCT_CENSUS, renders: NAV_RENDERS};
});
probe("count_all_paid", () => {
  acctCountRender(rows.filter(r => !(r.decl && r.decl.billing === "free")), true);
  return {nav: NAV_ACC, line: ELS["#qonecount"].textContent};
});
probe("count_unread", () => {
  acctCountRender(rows, false);
  return {nav: NAV_ACC, line: ELS["#qonecount"].textContent, census: ACCT_CENSUS};
});
probe("home", () => homeAccounts(acctCensus(rows)));
probe("home_unread", () => homeAccounts(null));
probe("tiers", () => Object.fromEntries(rows.filter(r => r.cred)
  .map(r => [r.cred.name, acctTier(r)])));
probe("walled", () => ATIER.WALLED);
probe("anthropic_order", () => acctDefaultSort(rows.filter(r => r.fam === "anthropic"))
  .map(r => [r.cred ? r.cred.name : r.decl.id, acctTier(r)]));
for (const n of [ANN, BOB + "#bob-two", CY, "cx-one", "cx-two"])
  probe("id:" + n, () => quotaIdCell(byName(n)));
/* F3: a default codex home whose login could not be read says why, on its row */
probe("id:login_unknown", () => quotaIdCell({cred: Object.assign({}, byName("cx-two").cred,
  {name: "(default-codex)", home_name: ".codex", subscription: null,
   login_unknown: "auth.json is not a JSON object"}), aliases: []}));
for (const n of [ANN, "cx-two"])
  probe("reset:" + n, () => quotaResetCell(byName(n).cred, latestGauges(n)));
/* B: a walled row whose wall's reset is unknown — its session window resets
   soon but is not the spent one, or its only reset has already passed —
   sorts after a row with a known back-in time, and says unknown */
{
  const W = (name, over) => ({fam: "anthropic", name, sub: "s-" + name, aliases: [],
    decl: null, spare: [], cred: Object.assign({name, provider: "anthropic",
      state: "exhausted", status: "blocked", home_name: name, active: false,
      subscription: "s-" + name, token_lineage: null}, over)});
  const unknownSession = W("wall-session", {headroom: 100.0, resets_at_ms: NOW + 3600e3});
  const expired = W("wall-expired", {headroom: 0.0, resets_at_ms: NOW - 3600e3});
  const known = W("wall-known", {headroom: 0.0, resets_at_ms: NOW + 30 * 3600e3});
  probe("unknown_order", () => acctDefaultSort([unknownSession, expired, known]).map(r => r.name));
  for (const r of [unknownSession, expired, known]) {
    probe("chip:" + r.name, () => quotaIdCell(r));
    probe("cell:" + r.name, () => quotaResetCell(r.cred, latestGauges(r.name)));
  }
}
probe("decl_totals", () => declTotalsHTML({accounts: 26, units: 23, free: 1,
                                           monthly_spend: 1057, unpriced: 10}, 64));
probe("decl_totals_all_paid", () => declTotalsHTML({accounts: 3, units: 2, free: 0,
                                                    monthly_spend: 7, unpriced: 0}, 64));
console.log(JSON.stringify(out));
"""


def _strip(html):
    """What a reader SEES: tags (and so every hover title) removed."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", "", html or "")).strip()


class CreditTruthRuntimeTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("node not available")
        src = web_ui_loader.read_text()
        parts, cls.missing = [], []
        for name in FNS:
            try:
                parts.append(_extract_fn(src, name))
            except AssertionError:
                cls.missing.append(name)
        consts = []
        for name in CONSTS:
            try:
                consts.append(_extract_const(src, name))
            except (AssertionError, ValueError):
                cls.missing.append(name)
        data = ("const ANN = %s, BOB = %s, CY = %s, DEE = %s, EVE = %s;\n"
                "const CREDS = %s;\nlet DECL = {accounts: %s};\n"
                % tuple(json.dumps(v) for v in (ANN, BOB, CY, DEE, EVE, CREDS, DECL)))
        cls.tmp = tempfile.mkdtemp(prefix="helm-credit-truth-")
        path = os.path.join(cls.tmp, "run.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(data + SUPPORT + "\n".join(consts) + "\n"
                    + "\n\n".join(parts) + DRIVER)
        proc = subprocess.run([node, path], capture_output=True, text=True,
                              timeout=60)
        cls.stderr = proc.stderr
        try:
            cls.out = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            cls.out = {}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def got(self, key):
        self.assertTrue(self.out, "node produced no output: "
                        + (self.stderr or "")[:1500])
        self.assertNotIn(key, self.out["errors"],
                         "%s raised on the shipped page (missing: %s)"
                         % (key, ", ".join(self.missing)))
        return self.out[key]

    # ---- 1. one count, its scope in words, the groups add up to it --------

    def test_one_subscription_is_one_row_however_many_homes_reach_it(self):
        rows = self.got("rows")
        subs = [r["sub"] for r in rows]
        self.assertEqual(len(rows), 9, rows)
        self.assertEqual(subs.count("s-bob"), 1, rows)
        self.assertEqual(subs.count("s-cx2"), 1, rows)
        bob = next(r for r in rows if r["sub"] == "s-bob")
        self.assertIn("(default-claude)", bob["aliases"])
        self.assertEqual(sorted(bob["aliases"]),
                         sorted([BOB, "(default-claude)"]))

    def test_the_census_is_the_table_and_the_groups_add_up_to_it(self):
        census = self.got("census")
        self.assertEqual(census["total"], TOTAL)
        self.assertEqual(census["total"], len(self.got("rows")))
        self.assertEqual(census["total"], self.got("group_sum"))
        self.assertEqual(sum(n for _, n in census["fams"]), census["total"])
        self.assertEqual(census["measured"], MEASURED)
        self.assertEqual(census["walled"], WALLED)  # ann, eve and cx-one
        self.assertEqual(census["login"], 1)        # cy's expired token
        self.assertEqual(census["free"], FREE)      # F1: the gratis row

    def test_the_scope_is_said_in_words_with_both_halves(self):
        """F1: the noun is "accounts", split into paid and free by what he
        declared, then into measured and declared-only."""
        words = self.got("words")
        self.assertTrue(words.startswith("%d accounts (%d paid, %d free)"
                                         % (TOTAL, TOTAL - FREE, FREE)), words)
        self.assertIn("%d measured" % MEASURED, words)
        self.assertIn("%d declared" % (TOTAL - MEASURED), words)

    def test_nav_and_the_credit_page_draw_the_one_census(self):
        got = self.got("count_render")
        nav = _strip(got["nav"])
        self.assertIn(str(TOTAL), nav)
        self.assertIn("accounts", nav)
        self.assertIn("%d free" % FREE, nav)          # F1: scope in words
        self.assertNotIn("paid accounts", nav)       # not every one is paid
        self.assertIn("<b>%d</b>" % TOTAL, got["nav"])  # the table's rows
        self.assertEqual(got["line"], self.got("words"))
        self.assertEqual(got["census"]["total"], TOTAL)   # Home reads this
        self.assertGreaterEqual(got["renders"], 1)
        # F1's control: where no row is free the nav says so
        self.assertIn("all paid", _strip(self.got("count_all_paid")["nav"]))
        # a count that could not be read draws nothing, never a number
        unread = self.got("count_unread")
        self.assertEqual((unread["nav"], unread["line"], unread["census"]),
                         ("", "", None))

    def test_home_says_the_same_number_with_the_same_scope(self):
        home = _strip(self.got("home"))
        self.assertIn("%d accounts" % TOTAL, home)
        self.assertIn("%d paid" % (TOTAL - FREE), home)
        self.assertIn("%d free" % FREE, home)
        self.assertIn("%d measured" % MEASURED, home)
        self.assertIn("%d spent until reset" % WALLED, home)
        self.assertIn("1 needs a new login", home)
        self.assertNotIn("helm ", home)
        self.assertIn("not read", _strip(self.got("home_unread")))

    # ---- 3. a spent account is walled and offers no login ----------------

    def test_an_exhausted_account_is_walled_even_with_a_live_agent_on_it(self):
        tiers, walled = self.got("tiers"), self.got("walled")
        self.assertEqual(walled, 2)                 # ATIER.WALLED, lifted
        self.assertIn(ANN, tiers)
        self.assertEqual(tiers[ANN], walled)
        self.assertEqual(tiers[EVE], walled)
        self.assertEqual(tiers["cx-one"], walled)
        self.assertNotEqual(tiers[BOB + "#bob-two"], walled)   # the control
        order = self.got("anthropic_order")
        self.assertIn([ANN, walled], order)

    def test_spent_rows_sort_by_when_they_are_back_not_the_session_reset(self):
        """F4: "spent, soonest back first" is the time each row's chip says.
        eve's session window resets sooner than ann's (1h against 2h), but
        its spent weekly window is back in 4d and ann's in 20h."""
        order = self.got("anthropic_order")
        walled = self.got("walled")
        self.assertEqual(order[-2:], [[ANN, walled], [EVE, walled]], order)

    def test_an_exhausted_account_says_it_is_spent_and_when_it_is_back(self):
        ann, cx = self.got("id:" + ANN), self.got("id:cx-one")
        self.assertIn("spent", ann)
        self.assertIn("back in 20h", _strip(ann))
        self.assertIn("spent", cx)
        self.assertIn("back in 4d", _strip(cx))
        for said in ("data-fix", "stfix", "login"):
            self.assertNotIn(said, ann)
            self.assertNotIn(said, cx)

    def test_a_wall_with_no_known_reset_sorts_last_and_says_unknown(self):
        """B: a spent row never borrows a reset that is not its wall's — a
        session window it has not spent, or a reset already past — so it says
        "reset time unknown" and sorts after every row whose back-in time is
        known."""
        order = self.got("unknown_order")
        self.assertEqual(order[0], "wall-known", order)
        self.assertEqual(sorted(order[1:]), ["wall-expired", "wall-session"])
        self.assertIn("back in 30h", _strip(self.got("chip:wall-known")))
        for name in ("wall-session", "wall-expired"):
            self.assertIn("reset time unknown", _strip(self.got("chip:" + name)))

    def test_the_reset_column_of_a_wall_with_no_known_reset_says_nothing_borrowed(self):
        """B, the row's other "when": its resets-in cell agrees with its chip
        — a dash, never the unspent session window's sooner reset."""
        self.assertEqual(_strip(self.got("cell:wall-known")), "30h")   # control
        self.assertEqual(_strip(self.got("cell:wall-session")), "—")
        self.assertEqual(_strip(self.got("cell:wall-expired")), "—")

    def test_a_spent_rows_reset_column_says_when_its_wall_lifts(self):
        """One row, one "when": the resets-in cell of a spent account is the
        time its state word gives, not an unspent window's sooner reset (the
        session window here resets in 2h, the spent weekly one in 20h)."""
        self.assertEqual(_strip(self.got("reset:" + ANN)), "20h")
        self.assertEqual(_strip(self.got("reset:cx-two")), "4d")   # the control

    def test_a_login_is_named_only_where_it_is_the_fix_and_never_as_a_command(self):
        """task/3735: the row says the account needs signing in again and
        that an agent can start the login; it shows no login command and no
        terminal step, and there is nothing to copy."""
        html = self.got("id:" + CY)
        self.assertIn("needs signing in again", _strip(html))
        self.assertIn("an agent can start the login", html)
        for said in ("claude /login", "data-fix", "terminal"):
            self.assertNotIn(said, html)
        codex = self.got("id:cx-two")                        # the control
        self.assertIn("cx-two", codex)
        self.assertNotIn("login", codex)
        self.assertNotIn("signing in", codex)

    # ---- 2. every claude home's lineage, visible ------------------------

    def test_every_claude_home_on_a_row_shows_its_lineage_as_text(self):
        seen = _strip(self.got("id:" + BOB + "#bob-two"))
        self.assertEqual(seen.count("token lineage"), 3, seen)
        self.assertIn("token lineage MATCH", seen)
        self.assertEqual(seen.count("token lineage UNKNOWN"), 2, seen)
        self.assertIn("no refresh token", seen)
        self.assertIn("not yet seen", seen)
        for home in ("bob-two", "bob-example-test", ".claude"):
            self.assertIn(home, seen)

    def test_unknown_is_drawn_unlike_match(self):
        html = self.got("id:" + BOB + "#bob-two")
        self.assertIn("lin-UNKNOWN", html)
        self.assertIn("lin-MATCH", html)

    def test_a_mismatch_names_both_accounts_in_text(self):
        seen = _strip(self.got("id:" + CY))
        self.assertIn("token lineage MISMATCH", seen)
        self.assertIn(DEE, seen)

    def test_a_claude_account_name_says_it_is_metadata_and_codex_does_not(self):
        self.assertIn("metadata", _strip(self.got("id:" + ANN)))
        self.assertIn("token lineage MATCH", _strip(self.got("id:" + ANN)))
        codex = _strip(self.got("id:cx-two"))
        self.assertIn("cx-two", codex)
        self.assertNotIn("metadata", codex)
        self.assertNotIn("lineage", codex)

    # ---- the declared card says the same noun -----------------------------

    def test_the_declared_total_names_paid_accounts_and_rows_apart(self):
        seen = _strip(self.got("decl_totals"))
        self.assertIn("23 accounts declared (22 paid, 1 free)", seen)
        self.assertIn("26 rows", seen)
        self.assertNotIn("26 accounts", seen)
        control = _strip(self.got("decl_totals_all_paid"))
        self.assertIn("2 accounts declared (2 paid, 0 free)", control)

    def test_a_default_home_whose_login_is_unread_says_why_on_its_row(self):
        """F3: the row names the reason, as text, instead of the census going
        unreadable."""
        seen = _strip(self.got("id:login_unknown"))
        self.assertIn("login unknown", seen)
        self.assertIn(".codex", seen)


class DeclaredTotalsCountOnceTest(unittest.TestCase):
    """THE DECLARED TOTAL COUNTS ONE SUBSCRIPTION ONCE. Rows that name one
    login — two homes of it, and the default pointer at it — are one bill, so
    they are one paid account, priced once, however many rows say so. The
    CLI's totals line and the web card read the same function."""

    MEASURED = [
        {"name": BOB, "provider": "anthropic", "email": BOB, "home": ""},
        {"name": BOB + "#bob-two", "provider": "anthropic", "email": BOB,
         "home": ""},
        {"name": "(default-claude)", "provider": "anthropic", "email": BOB,
         "home": ""},
        {"name": "cx-two", "provider": "codex", "email": CX, "home": ""},
        {"name": "(default-codex)", "provider": "codex", "email": CX,
         "home": ""},
    ]

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="helm-credit-totals-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "accounts.json")
        prev = os.environ.get("HELM_ACCOUNTS")
        os.environ["HELM_ACCOUNTS"] = self.path
        self.addCleanup(lambda: os.environ.__setitem__("HELM_ACCOUNTS", prev)
                        if prev is not None
                        else os.environ.pop("HELM_ACCOUNTS", None))
        base = {"plan": "Plan", "count": 1, "good_for": "work",
                "not_for": "nothing", "reach": "a seat"}
        for row in (
                dict(base, id="acct-bob", vendor="anthropic",
                     measured_as=BOB, price_month="$200"),
                dict(base, id="acct-bob-two", vendor="anthropic",
                     measured_as=BOB + "#bob-two"),
                dict(base, id="acct-bob-default", vendor="anthropic",
                     measured_as="(default-claude)"),
                dict(base, id="acct-cx", vendor="codex", measured_as="cx-two"),
                dict(base, id="acct-cx-default", vendor="codex",
                     measured_as="(default-codex)"),
                dict(base, id="acct-kimi", vendor="moonshot",
                     price_month="$100"),
                dict(base, id="acct-free", vendor="gratis", billing="free")):
            _, err, _ = accounts.save(row, accounts_path=self.path)
            self.assertIsNone(err, err)

    def test_the_card_counts_four_accounts_from_seven_rows(self):
        t = accounts.client_rows(accounts_path=self.path,
                                 measured=self.MEASURED)["totals"]
        self.assertEqual(t["accounts"], 7)          # rows written down
        self.assertEqual(t["units"], 4)             # bob, cx, kimi, free
        self.assertEqual(t["free"], 1)              # F1: declared free
        self.assertEqual(t["monthly_spend"], 300.0)
        self.assertEqual(t["unpriced"], 1)          # the cx bill, once; free is $0

    def test_the_web_door_answers_the_same_totals(self):
        with measuring(self.MEASURED):
            got = web._api_accounts()
        self.assertEqual(got["totals"]["accounts"], 7, got["totals"])
        self.assertEqual(got["totals"]["units"], 4, got["totals"])

    def test_the_cli_totals_line_reads_the_same_function(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            accounts._print_rows(accounts.read(self.path, measured=self.MEASURED),
                                 self.MEASURED)
        line = [ln for ln in buf.getvalue().splitlines() if "totals:" in ln]
        self.assertEqual(len(line), 1, buf.getvalue())
        self.assertIn("7 account(s), 4 unit(s)", line[0])

    def test_json_human_and_web_say_one_total(self):
        """F2: ONE RULE for all three outputs. `helm accounts --json`, the
        human `helm accounts` and the web card read the same totals on a
        fixture where seven rows name four subscriptions."""
        import contextlib
        import io
        with measuring(self.MEASURED):
            web_t = web._api_accounts()["totals"]
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(accounts.cmd_accounts(["--json"]), 0)
            human = io.StringIO()
            with contextlib.redirect_stdout(human):
                self.assertEqual(accounts.cmd_accounts([]), 0)
        json_t = json.loads(out.getvalue())["totals"]
        self.assertEqual(json_t["units"], 4, json_t)
        self.assertEqual(web_t["units"], 4, web_t)
        self.assertEqual(json_t, web_t)
        line = [ln for ln in human.getvalue().splitlines() if "totals:" in ln]
        self.assertEqual(len(line), 1, human.getvalue())
        self.assertIn("%d account(s), %d unit(s)"
                      % (json_t["accounts"], json_t["units"]), line[0])

    def test_without_the_measured_side_a_pointer_stays_its_own(self):
        """THE CONTROL: read with no measured rows, the two homes spelled with
        one login still merge (the rows alone prove it), while the two default
        pointers, which only the measured side resolves, stay their own."""
        t = accounts.read(self.path)["totals"]
        self.assertEqual(t["accounts"], 7)
        self.assertEqual(t["units"], 6)



class FreeIsOneRuleServerToPageTest(unittest.TestCase):
    """A: whether a subscription is FREE is decided once, by the rule the
    declared totals use (every billing its rows state is free), and the page
    counts the server's answer carried on the row — so a free record beside a
    paid duplicate is paid everywhere, and an unstated record beside a free
    duplicate is free everywhere, whichever record the table speaks for."""

    FNS = ("quotaRows", "quotaAliasOrder", "quotaDeclOrder", "quotaFamilies",
           "acctCensus", "acctTier", "acctNeedsLogin")
    CONSTS = ("QFAM_FIRST", "ATIER")

    def setUp(self):
        if not shutil.which("node"):
            raise unittest.SkipTest("node not available")
        self.dir = tempfile.mkdtemp(prefix="helm-credit-free-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "accounts.json")

    def census_free(self, rows):
        """(the declared totals' free count, the page census's) for `rows`,
        two records of one login, the first confirmed so the table speaks
        for it."""
        for row in rows:
            _, err, _ = accounts.save(dict({"plan": "Plan", "count": 1,
                                            "good_for": "work",
                                            "not_for": "nothing",
                                            "reach": "a seat",
                                            "vendor": "vendx"}, **row),
                                      accounts_path=self.path)
            self.assertIsNone(err, err)
        view = accounts.client_rows(accounts_path=self.path, measured=[])
        src = web_ui_loader.read_text()
        js = ("const esc = s => String(s);\nconst CREDS = [];\n"
              "let DECL = {accounts: %s};\n" % json.dumps(view["accounts"])
              + "\n".join(_extract_const(src, n) for n in self.CONSTS) + "\n"
              + "\n\n".join(_extract_fn(src, n) for n in self.FNS)
              + "\nconst rows = quotaRows();\n"
              "console.log(JSON.stringify({rows: rows.length, "
              "primary: rows[0].decl.id, free: acctCensus(rows).free}));\n")
        path = os.path.join(self.dir, "run.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(js)
        proc = subprocess.run([shutil.which("node"), path], capture_output=True,
                              text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        got = json.loads(proc.stdout)
        self.assertEqual(got["rows"], 1, got)       # one subscription, one row
        return view["totals"]["free"], got["free"], got["primary"]

    def test_a_free_record_beside_a_paid_duplicate_is_paid_everywhere(self):
        server, page, primary = self.census_free([
            {"id": "vx-free", "measured_as": "vx-login#one", "billing": "free",
             "confirmed": True},
            {"id": "vx-paid", "measured_as": "vx-login#two", "billing": "sub"}])
        self.assertEqual(primary, "vx-free")        # the table speaks for it
        self.assertEqual(server, 0)
        self.assertEqual(page, server)

    def test_an_unstated_record_beside_a_free_duplicate_is_free_everywhere(self):
        server, page, primary = self.census_free([
            {"id": "vx-unstated", "measured_as": "vx-login#one",
             "confirmed": True},
            {"id": "vx-free", "measured_as": "vx-login#two", "billing": "free"}])
        self.assertEqual(primary, "vx-unstated")
        self.assertEqual(server, 1)
        self.assertEqual(page, server)


class LoginUnknownReachesThePageTest(unittest.TestCase):
    """F3: a home whose login the provider could not read carries the reason
    to the credit table's row, and the rest of the census is read."""

    def test_the_reason_rides_the_measured_row(self):
        rows = [{"name": "(default-codex)", "provider": "codex", "home": "",
                 "email": None, "login_unknown": "auth.json is not a JSON object"},
                {"name": "cx-two", "provider": "codex", "home": "", "email": CX}]
        with measuring(rows):
            got = {r["name"]: r for r in web.get_creds()}
        self.assertEqual(sorted(got), ["(default-codex)", "cx-two"])
        self.assertEqual(got["(default-codex)"]["login_unknown"],
                         "auth.json is not a JSON object")
        self.assertIsNone(got["cx-two"]["login_unknown"])


if __name__ == "__main__":
    unittest.main()
