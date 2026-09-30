#!/usr/bin/env python3
"""HOME (task/3445 L3) — the console's first page, one screen of headlines.

The owner: "maybe there should be an actual homepage that doesnt need much
scrolling, just a really nice overall dashbaord for the app". So "/" opens
Home: an on-you strip (only while something waits), then headline tiles —
projects, pipeline, backlog, supply, accounts, seats, chat, the signing record
and fleet notes — and the away switch. Each tile is one or two numbers and a
plain line, and clicks through to the page that holds its detail; no tile is a
detail view. The owner reads the TOTALS and never the row text, so a tile names
no command, no read time and no internal subtotal: a reading past its bound
says the one muted word "stale", one that never answered "not read", and
neither draws a number. The leftovers that sat under Work's project list moved to Home or
to their natural page, so Work › projects holds the project list, its filter
bar and the capacity line.

These arms read the SHIPPED page: the markup for where each element lives, and
the headline functions lifted out and run under node.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from helm import web_ui_loader
from tests._ownerverbs import owner_verbs, shell_markup
from tests.test_web_accounts import _extract_const
from tests.test_web_board import _row, _team_board
from tests.test_web_chat_client_runtime import _extract_fn

# each tile: its label, where it clicks through to, and the cell it carries
# (ONE work tile in place of pipeline and backlog, task/3643 slice 3: the
# Work page's stage row; what it says is run in tests/test_web_work_page.py)
TILES = (("projects", "#work/projects", "hprojects"),
         ("work", "#work/work", "hwork"),
         ("supply", "#quota", "hsupply"),
         ("accounts", "#quota", "haccounts"),
         ("seats", "#roster", "hseats"),
         ("chat", "#chat", "hchat"),
         ("signing record", "#history", "hrecord"),
         ("fleet notes", "#home", "hnotes"))

# what a tile may not say: a command, a read time, or a count only an agent
# reads. "not read" is the one freshness word a tile may carry, with "stale".
JARGON = re.compile(r"helm |<code>|\bago\b|\bread \d|· read|projected|advisory|"
                    r"nonbillable|unverified|\bquiet\b|\bgone\b|keepalive")


def _view(src, v):
    """One view's markup, alone."""
    m = re.search(r'<div class="view(?: on)?" id="view-%s">' % v, src)
    assert m, "no view-%s in the page" % v
    nxt = src.find('<div class="view', m.end())
    return src[m.start():nxt if nxt > 0 else src.index("<script>")]


class HomeMarkupTest(unittest.TestCase):
    """Node-free: where each element lives now."""

    def setUp(self):
        self.src = web_ui_loader.read_text()
        self.home = _view(self.src, "home")

    def test_home_is_the_page_the_console_opens_on(self):
        self.assertIn('<div class="view on" id="view-home">', self.src)
        self.assertEqual(self.src.count('class="view on"'), 1)
        # the brand is the way back to it, and it is lit on Home
        self.assertIn('<a class="brand on" id="brand" href="#home"', self.src)
        # "/" opens Home, never the page read last
        boot = self.src[self.src.index("================= boot ================="):]
        boot = boot[:boot.index("\n}\n")]
        self.assertIn('showView("home")', boot)
        self.assertNotIn('getItem("helm.view")', boot)

    def test_each_tile_is_a_headline_that_clicks_through(self):  # noqa: VACUOUS_ASSERTION — str.index() RAISES on any missing tile or cell, so every one is asserted present before the order comparison runs
        at = []
        for label, go, cell in TILES:
            tile = re.search(r'<section class="htile(?: hwide)?" data-go="%s"[^>]*>\s*'
                             r'<a class="hlab" href="%s"(?: title="[^"]*")?>%s</a>' % (
                                 re.escape(go), re.escape(go), re.escape(label)),
                             self.home)
            self.assertIsNotNone(tile, label)
            at.append(tile.start())
            self.assertIn('id="%s"' % cell, self.home[tile.start():], label)
        self.assertEqual(at, sorted(at))
        # the strip and the away switch; every note, folded under the grid
        # the band's hover explanations ride the tiles' labels
        self.assertIn("every piece of work in every project", self.home)
        for mark in ('id="honyou"', 'id="honyouline"', 'id="posturesec"',
                     'id="homenotes"', 'id="notesec"',
                     'id="danswers"', 'id="foot"', 'id="hello"'):
            self.assertIn(mark, self.home, mark)
        self.assertIn('data-open="homenotes"', self.home)

    def test_the_seats_tile_hover_names_its_count_as_the_tile_does(self):
        """The tile says "N active"; its hover said "seats at work right now",
        and "at work" is the Projects headline's claimless seats (finding 4)."""
        label = re.search(r'<a class="hlab" href="#roster" title="([^"]*)">seats</a>',
                          self.home)
        self.assertIsNotNone(label)
        self.assertIn("active", label.group(1))
        self.assertNotIn("at work", label.group(1))
        self.assertNotIn("able to work", label.group(1))

    def test_every_headline_boots_saying_not_read_yet(self):
        grid = self.home[self.home.index('<div class="hgrid">'):]
        self.assertGreaterEqual(grid.count("not read yet"), len(TILES))

    def test_no_tile_is_a_detail_view(self):  # noqa: VACUOUS_ASSERTION — the tiles are asserted present by the arm above; this one asserts the detail mounts are elsewhere and present there
        for mount, where in (('id="wkmain"', "flow"),
                             ('id="brows"', "work"),
                             ('id="readysec"', "roster"),
                             ('id="frictionsec"', "configs"),
                             ('id="store"', "configs"), ('id="skillsec"', "configs"),
                             # the old band's cells, each atop its own page
                             # (the pipeline's are the Work page's stage row
                             # now, task/3643)
                             ('id="dfleet"', "roster"),
                             ('id="dchain"', "history"),
                             # the families card heads Fleet › credit (L4)
                             ('id="flagcard"', "quota")):
            self.assertNotIn(mount, self.home, mount)
            self.assertIn(mount, _view(self.src, where), mount)

    def test_home_names_no_command_and_no_read_time(self):
        """The tiles and everything around them, as he sees them: comments
        and hover titles are not on screen, so they are taken out first."""
        seen = re.sub(r"<!--.*?-->|\s(?:title|placeholder)=\"[^\"]*\"", "", self.home,
                      flags=re.S)
        self.assertIn("not read yet", seen)          # control: tiles are here
        self.assertIsNone(JARGON.search(seen), JARGON.search(seen))
        self.assertNotIn('class="hhow"', self.home)

    def test_the_grid_is_three_columns_down_to_960_then_two(self):
        css = self.src[self.src.index("/* ---------- HOME (task/3445 L3)"):]
        self.assertIn(".hgrid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));", css)
        self.assertIn("@media (max-width:959px){.hgrid{grid-template-columns:"
                      "repeat(2,minmax(0,1fr))}", css)
        # the away card is a tile of the grid, not a tall column beside it
        self.assertNotIn("grid-row:1/span", css)

    def test_work_projects_holds_the_list_its_filter_and_the_capacity_line(self):
        work = _view(self.src, "work")
        for mark in ('id="onyou"', 'id="bhead"', 'id="bheadline"',
                     'id="controls"', 'id="q"', 'id="lightf"',
                     'id="brows"'):
            self.assertIn(mark, work, mark)
        for gone in ('id="dash"', 'id="dreggstrip"', 'id="posturesec"',
                     'id="readysec"', 'id="frictionsec"', 'id="notesec"',
                     'id="store"', 'id="skillsec"', 'id="danswers"',
                     'id="foot"', 'id="hello"', 'id="stats"', 'class="brandrow"'):
            self.assertNotIn(gone, work, gone)

    def test_the_duplicates_are_deleted_not_moved(self):  # noqa: VACUOUS_ASSERTION — each absence sits beside the page's positive control: the page assembled and carries Home
        """The sessions strip repeated Fleet › sessions under a second count,
        the header repeated the nav, and the owner row repeated the on-you
        strip."""
        self.assertIn('id="view-home"', self.src)
        for gone in ('<section id="sessions">', 'id="stats"', 'id="downer"',
                     'id="dash"', "function sessStrip(", "function homeStats(",
                     # nothing stale: the owner strip had no place left to
                     # draw, the queue cell and the signing strip are Home's
                     # backlog and signing tiles, and "/" always opens Home,
                     # so a saved last page had no reader
                     "function dashOwner(", "function dashQueue(",
                     "function homeReady(", 'id="dqueue"', 'id="dreggstrip"',
                     'id="hready"', 'localStorage.setItem("helm.view"'):
            self.assertNotIn(gone, self.src, gone)

    def test_the_banners_are_page_chrome_on_every_page(self):
        first_view = self.src.index('<div class="view')
        for mark in ('<div id="banner">', '<div id="stalebanner">'):
            self.assertLess(self.src.index(mark), first_view, mark)

    def test_home_and_the_shell_tell_the_owner_no_helm_verb(self):
        """RULE 2 ON HOME'S MARKUP (console walk 3, open points): the tiles'
        hovers named `helm ready` and `helm premise-check --chain`, and the
        banner a dead server raises, on every page, said "start it with
        `helm web`". HTML comments are not shown and are not read."""
        home, shell = self.home, shell_markup(self.src)
        # POSITIVE CONTROLS: each slice is the markup named
        self.assertIn('id="hseats"', home)
        self.assertIn('id="banner"', shell)
        self.assertEqual(owner_verbs(home), [], home)
        self.assertEqual(owner_verbs(shell), [], shell)


class HomeHeadlineRuntimeTest(unittest.TestCase):
    """The headline functions, lifted out of the SHIPPED page and run."""

    DRIVER = r"""
const out = {};
const NOW = Date.now();
const ROWS = [ALPHA, BETA, GAMMA];
out.projects = homeProjects(BOARD, ROWS);
out.projects_unread = homeProjects(null, ROWS);
out.projects_down = homeProjects({unavailable: "OSError"}, ROWS);
const LR = {loops: [{id: "a"}, {id: "b", contrary: true}, {id: "c", stalled: true},
                    {id: "d", honored: true}, {id: "e"}],
            stalled_ids: [], unmeasurable: [{id: "e"}], closed_recent: [],
            closed_total: 7, read_age_s: 3,
            native_chain: {count: 41, verified: true, head_index: 40}};
out.supply = homeSupply(BOARD);
out.supply_unread = homeSupply(null);
/* the census the credit table last drew (task/3635): its rows, how many helm
   measures, how many are spent until a reset, how many need a login; null
   until both reads land or when either could not be read */
out.accounts = homeAccounts({total: 4, measured: 3, free: 0, walled: 1, login: 1});
out.accounts_quiet = homeAccounts({total: 1, measured: 1, free: 0, walled: 0, login: 0});
/* a row he declared FREE is an account, never a paid one */
out.accounts_free = homeAccounts({total: 2, measured: 1, free: 1, walled: 0, login: 0});
out.accounts_unread = [homeAccounts(null), homeAccounts(undefined)];
const PRES = [{seat: "a", presence: "fresh"},
              {seat: "b", presence: "fresh", availability: "UNAVAILABLE"},
              {seat: "c", presence: "quiet"}, {seat: "d", presence: "absent"},
              {seat: "u", presence: "fresh", unverified: true},
              {seat: "x", presence: "fresh", ephemeral: true},
              {seat: "o", presence: "fresh", owner: true}];
out.seats = homeSeats(PRES, {ready: "READY"}, false);
out.seats_notready = homeSeats(PRES, {ready: "NOT READY"}, false);
out.seats_noready = homeSeats(PRES, {pending: true}, false);
out.seats_stale = homeSeats(PRES, {ready: "READY"}, true);
out.seats_unread = homeSeats(null, null, false);
out.chat = [homeChat(0, false, false), homeChat(0, false, true), homeChat(3, true, true)];
/* the SIGNED reading: the shared record's signed count (d.status.dag_height)
   and the transport mode. dag_height is the same field History reads for its
   "signed·shared" number. */
const SIGNED = {d: {status: {dag_height: 7425}, transport: {mode: "signed"},
                  turns: [{chain_index: 9}]}, at: NOW};
const UNSIGNED = {d: {transport: {mode: "unsigned"}}, at: NOW};
out.record = homeRecord(SIGNED, LR, NOW);
out.record_down = homeRecord({d: {offline: true}, at: NOW}, LR, NOW);
/* no dag_height in the read: the signed side reads UNKNOWN (never 0) */
out.record_unsigned = homeRecord({d: {transport: {mode: "unsigned"}, status: {}},
                                  at: NOW}, LR, NOW);
out.record_failing = homeRecord(SIGNED, Object.assign({}, LR,
  {native_chain: {count: 41, verified: false, detail: "hash 12"}}), NOW);
out.record_old_chain = homeRecord(SIGNED, Object.assign({}, LR, {read_age_s: 600}), NOW);
/* a read 0-count local chain draws "0 local-only" (not UNKNOWN) — a fresh
   machine's answer is 0, and 0 is a number, so the count test is typeof not truthy */
out.record_empty_chain = homeRecord(SIGNED, Object.assign({}, LR,
  {native_chain: {count: 0, verified: true}}), NOW);
out.record_stale = homeRecord({d: SIGNED.d, at: NOW - 120e3}, LR, NOW);
out.record_unread = homeRecord(null, null, NOW);
out.notes = homeNotes({notes: [{key: "a", headline: "fresh one", stale: false},
                                {key: "b", headline: "old one", stale: true}]});
out.notes_unread = [homeNotes(null), homeNotes({unavailable: true})];
console.log(JSON.stringify(out));
"""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        src = web_ui_loader.read_text()
        fns = "\n\n".join(_extract_fn(src, n) for n in (
            "lrDur", "lrAgo", "flagWhen", "boardSec", "boardSecState",
            "boardSecWord", "boardActive", "famSheet", "flagsHTML",
            "teamMode", "teamLane", "teamWord", "teamTokens", "teamCredits",
            "shareBar", "slotBar", "teamCurrent",
            "cardBoundS", "cardStale", "lrStale",
            "homeWord", "homeTile", "homeProjects",
            "homeSupply", "homeAccounts", "homeSeats",
            "homeChat", "homeRecord", "homeNotes"))
        support = ('const esc = s => String(s ?? "").replace(/[&<>"\']/g, c => '
                   '({"&":"&amp;","<":"&lt;",">":"&gt;",\'"\':"&quot;","\'":"&#39;"}[c]));\n'
                   "const TEAM_DRAFT = new Map();\nlet FAM_SEL = null;\nlet FAM_MSG = null;\n"
                   'const TEAM_PALETTE = ["#111"];\n')
        board = _team_board()
        board["headline"] = {"green": 2, "lanes": {"running": 15, "possible": 13}}
        board["sections"]["lights"] = board["sections"]["flags"].copy()
        board["sections"]["seats"] = board["sections"]["flags"].copy()
        worlds = {"BOARD": board, "ALPHA": _row("alpha", "green"),
                  "BETA": _row("beta", "yellow"), "GAMMA": _row("gamma")}
        payload = "".join("const %s = %s;\n" % (k, json.dumps(v))
                          for k, v in worlds.items())
        cls.tmp = tempfile.mkdtemp(prefix="helm-web-home-")
        path = os.path.join(cls.tmp, "run.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(support + _extract_const(src, "FLAGCOL") + "\n" + payload
                    + fns + cls.DRIVER)
        chk = subprocess.run([cls.node, "--check", path],
                             capture_output=True, text=True)
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

    STALE = '<span class="hstale">stale</span>'
    UNREAD = '<span class="hstale">not read</span>'

    def text(self, key):
        return re.sub(r"<[^>]*>", "", self.out[key])

    def test_no_headline_names_a_command_a_read_time_or_a_subtotal(self):  # noqa: VACUOUS_ASSERTION — the projects headline is asserted to read 'green' on the same run before the jargon absences are read over every headline
        self.assertIn("green", self.out["projects"])       # control: it ran
        for key, html in self.out.items():
            for one in html if isinstance(html, list) else [html]:
                text = re.sub(r"<[^>]*>", "", one)
                self.assertIsNone(JARGON.search(text), (key, text))

    def test_projects_is_green_and_active_then_lanes_and_teams(self):
        self.assertEqual(self.text("projects"),
                         "2 green · 3 active15 lanes running · 1 team to accept")
        self.assertEqual(self.out["projects_unread"], self.UNREAD)
        self.assertEqual(self.out["projects_down"], self.UNREAD)

    def test_supply_counts_red_and_orange_families_and_names_the_tightest(self):
        self.assertEqual(self.text("supply"), "0 red · 1 orangetightest: codex")
        self.assertEqual(self.out["supply_unread"], self.UNREAD)

    def test_accounts_say_how_many_want_him_and_never_a_false_zero(self):
        """The credit table's own count, with its scope in words (task/3635):
        paid accounts, how many of them helm measures, and a spent one is
        spent until its reset, never a login chore (task/3634)."""
        self.assertEqual(self.text("accounts"),
                         "4 accounts4 paid · 3 measured · 1 spent until reset"
                         " · 1 needs a new login")
        self.assertEqual(self.text("accounts_quiet"),
                         "1 account1 paid · 1 measured · all fine")
        self.assertEqual(self.text("accounts_free"),
                         "2 accounts1 paid · 1 free · 1 measured · all fine")
        self.assertEqual(self.out["accounts_unread"], [self.UNREAD] * 2)

    def test_seats_are_active_and_unavailable_then_ready(self):
        # a and b are active; the quiet, the gone, the unverified, the
        # ephemeral and the owner's own row are not; b is walled off
        self.assertEqual(self.text("seats"), "2 active · 1 unavailableready to resume")
        self.assertEqual(self.text("seats_notready"),
                         "2 active · 1 unavailablenot ready to resume")
        self.assertEqual(self.text("seats_noready"), "2 active · 1 unavailable")
        self.assertEqual(self.out["seats_stale"], self.STALE)
        self.assertEqual(self.out["seats_unread"], self.UNREAD)

    def test_the_active_count_wears_no_other_counts_word(self):  # noqa: VACUOUS_ASSERTION — the first statement asserts the no-ready tile exactly (2 active, 1 unavailable), the same homeSeats render the loop's absences read
        """COUNT-WORD UNIFICATION (console walk 3, finding 4). The tile counts
        the seats ACTIVE right now, the word every roster row and the Seats
        strip already use for that set. It is not "able to work", the Projects
        headline's count (`web_board._usable`), which leaves out a walled-off
        seat: b is walled off here and counted, so the tile would call it able
        to work beside "1 unavailable". Nor "at work", the Projects headline's
        claimless seats."""
        self.assertEqual(self.text("seats_noready"), "2 active · 1 unavailable")
        for key in ("seats", "seats_notready", "seats_noready"):
            text = self.text(key)
            self.assertTrue(text.startswith("2 active · 1 unavailable"), (key, text))
            self.assertNotIn("able to work", text, key)
            self.assertNotIn("at work", text, key)

    def test_chat_and_notes_each_say_unread_before_a_number(self):
        self.assertEqual([re.sub(r"<[^>]*>", "", c) for c in self.out["chat"]],
                         ["not read", "nothing unread", "3 unread · @you"])
        self.assertEqual(self.text("notes"), "1 new · 1 olderfresh one")
        self.assertEqual(self.out["notes_unread"], [self.UNREAD] * 2)

    def test_record_signed(self):
        # live + the same comparison History draws beside its 7,425 signed
        # entries: the shared count (dregg d.status.dag_height) vs the local
        # chain's count (lr.native_chain.count), with the chain's own check
        self.assertIn("signing live", self.text("record"))
        self.assertIn("7425 signed · 41 local-only · chain ✓", self.text("record"))
        # the shared number is the same field History shows (dregg status.dag_height)
        self.assertNotIn("UNKNOWN", self.text("record"))
        # the local word is the LOCAL chain's own hash check, never the shared
        # record's "verified"
        self.assertNotIn("verified", self.text("record"))

    def test_record_unsigned(self):
        # the "signed·shared" number is UNKNOWN (never 0) while there is no
        # dag_height; the local side is still drawn while its own read is inside
        # the bound
        self.assertNotIn("signing live", self.text("record_unsigned"))
        self.assertIn("UNKNOWN signed · 41 local-only · chain ✓", self.text("record_unsigned"))
        self.assertIn("signing unsigned", self.text("record_unsigned"))

    def test_record_down(self):
        # the shared side is "signing down" (dregg.d.offline) — one word, no
        # count; UNKNOWN appears only on the local side
        self.assertIn("signing down", self.text("record_down"))
        self.assertNotIn("UNKNOWN", self.text("record_down"))
        # a down shared record carries no shared count
        self.assertNotIn("signed ·", self.text("record_down"))

    def test_record_empty_chain_reads_zero(self):
        # a READ local chain with count 0 is 0, not UNKNOWN: the count test is
        # typeof, not truthiness, so a fresh machine's "0 local-only · chain ✓"
        # draws its real zero
        self.assertIn("0 local-only · chain ✓", self.text("record_empty_chain"))
        self.assertNotIn("UNKNOWN", self.text("record_empty_chain"))
        # the shared side still shows its number, unaffected by the local count
        self.assertIn("7425 signed", self.text("record_empty_chain"))

    def test_record_stale_or_unread(self):  # noqa: VACUOUS_ASSERTION — the three observables are concrete positive controls (STALE/UNREAD HTML, "UNKNOWN local-only", "7425 signed"), not absent-assertions; the guard's empty-observable heuristic does not apply
        # a reading past its bound says only "stale"; one never answered "not
        # read", and neither draws a number
        self.assertEqual(self.out["record_stale"], self.STALE)
        self.assertEqual(self.out["record_unread"], self.UNREAD)
        # a chain count past its read's bound is not drawn on the local side; the
        # shared count still rides (it rides the signing poll, not the lr read)
        self.assertIn("UNKNOWN local-only", self.text("record_old_chain"))
        self.assertIn("7425 signed", self.text("record_old_chain"))

    def test_every_reading_a_tile_draws_redraws_home_when_it_lands(self):
        """A tile says "not read" only while nothing is held: each poll a
        tile reads redraws Home as its answer lands, rather than waiting for
        the five-second clock."""
        src = web_ui_loader.read_text()
        self.assertIn("setInterval(homeRender, 5000)", src)   # the clock stays
        for fn in ("notesSec", "dashFleet", "chatSignal", "readyShow",
                   "pollDregg", "renderBoard", "lrShow", "wkPaintAll"):
            self.assertIn("homeRender()", _extract_fn(src, fn), fn)

    def test_the_on_you_strip_boots_in_the_tiles_word(self):
        home = _view(web_ui_loader.read_text(), "home")
        strip = home[home.index('id="honyouline"'):]
        strip = strip[:strip.index("</section>")]
        self.assertIn('<span class="hstale">not read</span>', strip)
        self.assertNotIn("not read yet", strip)

    def test_a_tile_click_routes_through_the_router_and_opens_its_fold(self):
        src = web_ui_loader.read_text()
        i = src.index('const tile = ev.target.closest("#view-home .htile, #view-home .hstrip");')
        handler = src[i:src.index("});", i)]
        self.assertIn("hashView(", handler)
        self.assertIn("showView(r.v)", handler)
        self.assertIn("tile.dataset.open", handler)


class HomeOwnerVerbsTest(unittest.TestCase):
    """RULE 2 ON HOME'S CARDS (console walk 3, open points): the page names
    places and states facts, and never tells the owner to run a helm verb.
    The notes card, the footer line and the away card are lifted out of the
    shipped page and run. The markup half is in HomeMarkupTest, and the
    answers card runs with the band in tests/test_web_lr.py.

    The away card is drawn with no presets: a preset is the notice the owner
    sends to the agents, in their words, not a line the console says to him."""

    DRIVER = r"""
const els = {};
const $ = sel => els[sel] || (els[sel] = {innerHTML: "",
  classList: {add() {}, remove() {}}, querySelectorAll: () => []});
const showView = () => {};
const noteAgo = _iso => "just now";
const NOW = Math.floor(Date.now() / 1000);
const out = {};
notesSec({notes: [{key: "landed", by: "a seat", ts: "2026-09-29T00:00:00Z",
                   headline: "Gate green", stale: false}], cap: 64});
out.notes = $("#notesec").innerHTML;
foot({});
out.foot = $("#foot").innerHTML;
out.away = postureCardHTML({away: {state: "away", owner_door: false,
                                   by: "a timer", via: "a unit", t: NOW - 60},
                            notice: {state: "none"}, presets: [],
                            notice_max: 240}, null, NOW, "");
console.log(JSON.stringify(out));
"""

    @classmethod
    def setUpClass(cls):
        cls.src = web_ui_loader.read_text()
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        esc, = [ln for ln in cls.src.splitlines()
                if ln.startswith("const esc = ")]
        fns = "\n\n".join(_extract_fn(cls.src, n) for n in (
            "age", "noteGoto", "noteRowHTML", "notesSec", "foot",
            "postureAgo", "postureCardHTML"))
        cls.tmp = tempfile.mkdtemp(prefix="helm-web-home-verbs-")
        path = os.path.join(cls.tmp, "run.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(esc + "\n" + fns + cls.DRIVER)
        chk = subprocess.run([cls.node, "--check", path],
                             capture_output=True, text=True)
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

    def test_the_home_cards_tell_the_owner_no_helm_verb(self):
        """The notes card showed `helm note set <key> <headline> --detail …
        --goto …`, the footer said "never synced — run `helm sync`", and the
        away card said agents coordinate "through helm chat and dispatch"."""
        self.assertTrue(self.out, "node produced no output: "
                        + (self.proc.stderr or "")[:1200])
        # POSITIVE CONTROLS on the same renders: each is the state named
        self.assertIn("Gate green", self.out["notes"])
        self.assertIn("never synced", self.out["foot"])
        self.assertIn("marked away", self.out["away"])
        for name in ("notes", "foot", "away"):
            self.assertEqual(owner_verbs(self.out[name]), [],
                             "%s: %s" % (name, self.out[name]))


if __name__ == "__main__":
    unittest.main()
