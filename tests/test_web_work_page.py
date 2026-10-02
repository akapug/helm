#!/usr/bin/env python3
"""THE ONE WORK PAGE (task/3643, slices 3-6): the backlog and the pipeline as
one page, read from `/api/work` alone.

The owner's Backlog and Pipeline pages drew the same work two ways, and the
console printed four pipeline numbers at one instant (walk 2: 36, 47, 81,
25). The Work page draws every piece of work as ONE card in ONE of five
stages, and the nav badge, Home's work tile and each project's row read the
same body, so no surface can print a number the page does not.

These arms read the SHIPPED page: the markup and the manifest for where each
part lives and what was retired, and the page's own functions lifted out of
the assembled source and run under node over a body the server's reader
(`helm/work_model.py`) built from a planted world, so an arm is about what
the owner sees, over the wire shape the server really sends.

What they pin:

  * slice 3, one count everywhere: the badge is the stuck count, Home's tile
    the stage row word for word, a project's row its `by_project` counts,
    and a source not read is "at least" or "?", never a zero;
  * slice 4, the River: five columns, one card each, the drawer and every
    filter in the address, the drawer's crossings in words;
  * slice 5, the List lens: the backlog's rows with a Stage and a Whose move
    column, Group and Sort, and Backlog and Pipeline as entries into it;
  * slice 6, the old surfaces gone with nothing left naming them;
  * the meld's rulings: a fix debt is Building and red, every owed move is on
    the card face, a land never closes the task ("landed, whole ask not yet
    re-read"), Landed is the rolling day with a Today filter;
  * walk 2, finding 17: one landed time, the land's push time, and a task's
    close is "closed"; finding 19: one phrase for a move nobody holds, never
    "@unknown";
  * task/3482 D1 and D3: an unknown project leaves the address, and a seat
    that differs only in case is one menu entry;
  * the cure of review 3643 (the pins the retired pages carried, onto the
    one page): no count the page prints is a zero over a source not read,
    a stale reading says so on every surface with its age, the rank filter
    is a set, a room on a `lane/`-prefixed lease is its request's card, the
    one read keeps the newer answer and turns a failure into UNKNOWN, and
    what needs a DOM (a drawer kept through a repaint, Esc, the keyboard's
    place, a link landing on its card) is pinned in the shipped source.
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

from tests import test_work_model as W  # noqa: E402  (plants HELM_HOME)
from tests.test_web_accounts import _extract_const  # noqa: E402
from tests.test_web_chat_client_runtime import _extract_fn  # noqa: E402

from helm import web_ui_loader  # noqa: E402

NOW, DAY = W.NOW, W.DAY

# the page's own functions the arms run, lifted verbatim
FNS = ("blN", "wkPlural", "wkDefault", "wkParse", "wkQuery", "wkEntry",
       "wkStagesOf", "wkDay", "wkMove", "wkCards", "wkPass", "wkNormalize",
       "wkCount", "wkBound", "wkNum", "wkMissing", "wkBadge", "wkHomeTile",
       "wkProjectCount", "wkSeat", "wkNobodyWhy", "wkWho", "wkMoveLabel",
       "wkWhen", "wkLandedWords", "wkWhys", "wkOthers", "wkNowNext",
       "wkLinkWords", "wkEvWords", "wkTrackHTML", "wkRankHTML", "wkIdHTML",
       "wkBackHTML", "wkProjHTML", "wkAll", "wkAgeWord", "wkCardHTML",
       "wkRowHTML", "wkSort", "wkMoreHTML", "wkTrainLineHTML",
       "wkOtherLandsHTML", "wkTodayHTML", "wkLandedHTML", "wkColHTML",
       "wkRiverHTML", "wkPagerHTML", "wkListHTML", "wkStageWordsHTML",
       "wkSeamsHTML", "wkDrawerHTML", "wkBracketsHTML", "wkHeadsHTML",
       "wkChipsHTML", "wkMenusHTML", "wkScopeHTML", "wkLineHTML",
       "wkInst", "lrDur", "lrAgo", "cardBoundS", "cardStale", "homeWord",
       "homeTile", "pkey", "navBadgeText")
CONSTS = ("WK_STAGES", "WK_SI", "WK_SPANS", "WK_RANKS", "WK_PRIOS", "WK_ENTRY",
          "WK_OMIT", "WK_GROUPS", "WK_SORTS", "WK_MARKED")
DECLS = ("WK_PER",)

# words a card may never carry: an unknown holder, a command, an endpoint
FORBIDDEN = re.compile(r"@unknown|\bhelm (task|lr|work|dispatch|gate|note|"
                       r"seat|premise|owed|burn)\b|/api/|<code>")


def _src():
    return web_ui_loader.read_text()


def _part(rel):
    with open(os.path.join(web_ui_loader.UI_DIR, rel), encoding="utf-8") as f:
        return f.read()


def _text(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", html)).strip()


# ---------------------------------------------------------------- the world

def _dispatch():
    """One round on task/4's lane: sent for review, and a fix asked with the
    reviewer's own patch — so the drawer has crossings in words."""
    return {"rows": {
        "fix4": {"id": "fix4", "ts": NOW - 3 * DAY, "kind": "review",
                 "lane": "task-4-cure", "chain": "chain-fix4",
                 "sender": "alice", "recipient": "bob"}},
        "events": {"fix4": [[NOW - 3 * DAY + 600, "V", "fix", True]]},
        "chains": {"chain-fix4": ["fix4"]}, "unavailable": None}


def _trains():
    """A train that landed task/5's lane two hours ago, and one that landed
    task/6's lane thirty hours ago (outside the day's window)."""
    return {"trains": [
        {"name": "train8", "state": "DONE", "land": 39, "intent": NOW - 30 * 3600 - 600,
         "pushed": NOW - 30 * 3600, "ended": NOW - 30 * 3600, "ran": 90,
         "cars": [{"lane": "task-6-old", "author": "alice", "reader": "bob"}]},
        {"name": "train9", "state": "DONE", "land": 40, "intent": NOW - 7800,
         "pushed": NOW - 7200, "ended": NOW - 7200, "ran": 100,
         "cars": [{"lane": "task-5-done", "author": "alice", "reader": "carol"}]}],
        "lands": {"train9": {"n": 40, "ran": 100, "at": NOW - 7200}},
        "ejections": [], "unavailable": None}


def _world(dispatch=None, seats=None, **over):
    """A planted world with a card in every stage the page words:

    task/1 unowned P0 you asked · task/2 assigned · task/3 claimed ·
    task/4 building, a fix asked, with a second room under review (two owed
    moves on one card) · `relay` a fix owed on landed work, the integrator's
    move · `stuck-review` an undecided review past a day · `ready-lane`
    approved, the train's move · task/5 landed in train9, its task still open
    · beta's review whose holder the pipeline does not name, and beta's
    task/9 on the to-do list · task/4's room, claimed, whose opening helm
    keeps no time for. `seats`, when given, is the rooms' section as read."""
    tasks = W._tasks(
        W._task(1, priority="P0", origin="owner"),
        W._task(2, owner="dana"),
        W._task(3, status="in_progress", owner="erin"),
        W._task(4, owner="alice"),
        W._task(5, owner="alice"),
        W._task(9, project="beta"))
    live = [
        W._row("fix4", "task-4-cure", "CHANGES_REQUESTED", polarity="fix",
               holder="alice", owed_by="author", age_s=2 * DAY),
        W._row("rev4", "task-4-review", "AWAITING_REVIEW", holder="carol",
               reviewer="carol", age_s=3600),
        W._fix_landed("relay", "relay", age_s=4 * DAY),
        W._row("stuck", "stuck-review", "REVIEWED", polarity="concur",
               holder="unknown", owed_by="unknown (declared verdict held)",
               age_s=3 * DAY),
        W._row("ready", "ready-lane", "READY", holder="integrator",
               owed_by="integrator", age_s=1800)]
    over.setdefault("running", [{"kind": "claim", "lane": "task-4-cure",
                                 "seats": ["alice"]}])
    board = W._board(live, other={"beta": [
        W._row("b1", "beta-work", "AWAITING_REVIEW", holder="unknown",
               owed_by="unknown", age_s=600)]}, **over)
    if seats is not None:
        board["sections"]["seats"] = seats
    return W._build(board, tasks=tasks, dispatch=dispatch or _dispatch(),
                    trains=_trains())


def _story_world():
    """0.3.3 criterion 5.1, node fixture: ONE story, whole. task/1 is the
    root; task/2, task/3 and task/4 continue it, one of them closed. The
    tasks section is read over the whole ledger (the board's other sections
    stay read but empty), so every task is a To do card and the card face,
    the drawer's children checklist and the home tile all have one story to
    count."""
    ledger = {
        "task/1": W._task(1),
        "task/2": W._task(2, continues="task/1"),
        "task/3": W._task(3, status="closed", continues="task/1"),
        "task/4": W._task(4, continues="task/1")}
    tasks = {"rows": ledger, "history": {k: [dict(r)] for k, r in ledger.items()},
             "unavailable": None}
    return W._wm().build(W._inp(W._board([]), tasks=tasks))


def _stories_world():
    """0.3.3 criterion 5.1, the List-lens story GROUP, node fixture: two
    stories (task/5 has two children, one closed, so its root reads 1 of 2;
    task/1 and task/2 have one child each, so 0 of 1) and task/6, a story of
    one. Every task is open and a To do card, so the group splits the list
    on the story each card belongs to, and the closed child under task/5
    is not a To do card: the group holds its root and its one open child."""
    ledger = {
        "task/1": W._task(1),
        "task/2": W._task(2, continues="task/1"),
        "task/5": W._task(5),
        "task/6": W._task(6, continues="task/5"),
        "task/7": W._task(7, status="closed", continues="task/5"),
        "task/8": W._task(8)}
    tasks = {"rows": ledger, "history": {k: [dict(r)] for k, r in ledger.items()},
             "unavailable": None}
    return W._wm().build(W._inp(W._board([]), tasks=tasks))


def _bodies():
    """{name: /api/work body}: the whole world, one with beta not read yet,
    one the reader could not read at all, a restart (no project's pipeline
    read), alpha's pipeline reading stale (with its clock, and without),
    the review history unreadable, and task/4's and relay's drawers."""
    wm = W._wm()
    whole = _world()
    beta_unread = _world(fleet=W._sec(scope="alpha", loading=True,
                                      measured_at=None, age_s=None))
    body = wm.view(whole, NOW)
    return {"whole": body,
            "story": wm.view(_story_world(), NOW),
            "storiesGroup": wm.view(_stories_world(), NOW),
            "unread": wm.view(beta_unread, NOW),
            "restart": wm.view(_world(lands={"loading": True, "scope": None},
                                      fleet={"loading": True}), NOW),
            "stale": wm.view(_world(lands=W._sec(
                scope="alpha", age_s=1300, limit_s=1200, stale=True)), NOW),
            "noclock": wm.view(_world(lands=W._sec(
                scope="alpha", measured_at=None, age_s=None, stale=True)), NOW),
            "dispatch": wm.view(_world(dispatch=dict(
                _dispatch(), unavailable="the dispatch ledger raised "
                                         "(OSError)")), NOW),
            "down": {"unavailable": "the work reader raised (OSError)",
                     "cards": [], "records": [], "counts": None, "rev": None},
            "events4": wm.card_events(whole, "task/4"),
            "eventsRelay": wm.card_events(whole, "lane:alpha:relay")}


def _fns():
    """FNS, and EVERY function the Work page's own part defines: an arm
    drives the page's composition (`wkParts`, `wkLoad`) with every helper it
    calls, so a helper the page gains is never missing from the harness."""
    own = re.findall(r"^(?:async )?function (\w+)\(", _part("scripts/52-work.js.part"), re.M)
    return list(dict.fromkeys(FNS + tuple(own)))


def _run(driver, bodies=None, prelude="", run_async=False):
    """Run `driver` over the page's own functions and the bodies; `out` is
    what it returns. `prelude` is top-level code (a stand-in the page's
    functions call by name); `run_async` runs the driver inside an async
    function, so it may `await`."""
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node not available")
    src = _src()
    esc = [ln for ln in src.splitlines() if ln.startswith("const esc = ")]
    assert len(esc) == 1, "the page's esc moved"
    decls = [re.search(r"^const %s = [^;]+;" % n, src, re.M).group(0) for n in DECLS]
    body = driver + "\nconsole.log(JSON.stringify(out));\n"
    if run_async:
        body = "(async () => {\n" + body + "})().catch(e => { console.error(e && e.stack || e); process.exit(1); });\n"
    script = "\n".join(esc + [_extract_const(src, n) for n in CONSTS] + decls
                       + [_extract_fn(src, n) for n in _fns()]) \
        + "\nconst B = %s;\nconst NOW = %r;\nconst out = {};\n" % (
            json.dumps(bodies or _bodies()), NOW) \
        + prelude + "\n" + body
    tmp = tempfile.mkdtemp(prefix="helm-web-work-page-")
    try:
        path = os.path.join(tmp, "run.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(script)
        proc = subprocess.run([node, path], capture_output=True, text=True,
                              timeout=60)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    assert proc.returncode == 0, proc.stderr[:3000]
    return json.loads(proc.stdout)


# ------------------------------------------------------------------ markup

class OnePageMarkupTest(unittest.TestCase):
    """Node-free: where the page lives, and what it replaced."""

    def setUp(self):
        self.src = _src()

    def test_the_page_is_one_view_with_its_section_and_three_folds(self):
        view = _part("views/03-work.html.part")
        self.assertIn('<div class="view" id="view-flow">', view)
        self.assertIn('<section class="wk" id="wkmain" data-wk="main">', view)
        for fold in ("wkseams", "wkleft", "wkelse"):
            self.assertIn('<details class="grp" id="%s">' % fold, view)
        for gone in ('id="view-pipeline"', 'id="view-backlog"'):
            self.assertNotIn(gone, self.src)

    def test_the_manifest_registers_the_work_parts_and_drops_the_old(self):
        with open(os.path.join(web_ui_loader.UI_DIR, "manifest.txt"),
                  encoding="utf-8") as f:
            parts = f.read().split()
        for new in ("views/03-work.html.part", "scripts/52-work.js.part",
                    "styles/74-work.css.part"):
            self.assertIn(new, parts)
        self.assertEqual(parts.index("scripts/52-work.js.part"),
                         parts.index("scripts/50-work.js.part") + 1)
        for gone in ("views/02-pipeline.html.part", "views/04-backlog.html.part",
                     "scripts/16-landcard.js.part",
                     "scripts/65-scheduler.js.part",
                     "styles/65-scheduler.css.part"):
            self.assertNotIn(gone, parts)
            self.assertFalse(os.path.exists(os.path.join(web_ui_loader.UI_DIR, gone)))

    def test_the_retired_renderers_are_gone_and_nothing_names_them(self):
        """Slice 6: the land board strip and rows, who waits on whom, the
        burn-down, cured and undeclared lists, and the backlog component
        the List lens replaced — each gone, and no caller left behind."""
        for name in ("obdRender", "ocbRender", "oubRender", "obdInit",
                     "landBoard", "landBoardHTML", "lbCardHTML", "lbReqHTML",
                     "pipelineRender", "pipeCounts", "pipeNav", "pipeShow",
                     "schedulerHTML", "schedulerShow", "boardKanban",
                     "boardWaits", "projOwed", "projTasks", "blSkeleton",
                     "blParse", "blQuery", "tqCard", "tqHeadHTML", "tqInit",
                     "homePipeline", "homeBacklog", "lrPartition",
                     "goOldSection", "NAV_LR"):
            with self.subTest(name=name):
                self.assertIsNone(re.search(r"\b%s\b" % name, self.src))
        # an unconditional control: the Work page's own renderer is there
        self.assertIn("function wkCardHTML(", self.src)

    def test_the_page_reads_the_work_only_from_api_work(self):
        work = _part("scripts/52-work.js.part")
        self.assertIn('j("/api/work", WK_FETCH_MS)', work)
        self.assertIn('j("/api/work?key=', work)
        # no second reader of work on the page: the backlog's paged read and
        # the owed read the burn-down drew are gone from every script
        for gone in ('j("/api/backlog', 'j("/api/owed', 'j("/api/scheduler'):
            self.assertNotIn(gone, self.src)
        self.assertEqual(self.src.count('j("/api/work"'), 1)

    def test_home_has_one_work_tile_in_place_of_pipeline_and_backlog(self):
        home = _part("views/05-home.html.part")
        self.assertIn('<section class="htile hwide" data-go="#work/work">', home)
        self.assertIn('id="hwork"', home)
        for gone in ('id="hpipe"', 'id="hbacklog"'):
            self.assertNotIn(gone, home)
        self.assertIn('set("hwork", wkHomeTile(', _part("scripts/12-home.js.part"))

    def test_a_project_has_team_work_and_about_tabs(self):
        self.assertIn('const PROJ_TABS = [["team", "Team"], ["work", "Work"], ["about", "About"]];',
                      self.src)
        body = _extract_fn(self.src, "projTab")
        self.assertIn("wkHTML(WK.proj)", body)
        # the old tabs' addresses open the Work tab at their entry
        route = _extract_fn(self.src, "projRoute")
        self.assertIn("lanes: WK_ENTRY.pipeline", route)
        self.assertIn("WK_ENTRY.backlog", route)

    def test_the_nav_badge_and_the_row_count_read_the_work(self):
        self.assertIn('navBadge("flow", b.n, true, b.title, b.bound);',
                      _extract_fn(self.src, "wkPaintAll"))
        self.assertIn("wkProjectCount(WORK && WORK.d, key)",
                      _extract_fn(self.src, "boardCount"))

    def test_the_phone_draws_one_column_and_a_full_screen_drawer(self):
        css = _part("styles/74-work.css.part")
        phone = css[css.index("@media (max-width:680px){"):]
        self.assertIn(".wkriver{grid-template-columns:minmax(0,1fr)}", phone)
        self.assertIn(".wkdrawer,.wklist .wkdrawer{position:fixed;inset:0;", phone)
        self.assertIn(".wkdtop{position:sticky;top:0;", phone)
        # nothing in the phone rules is wider than the screen
        self.assertNotRegex(phone, r"min-width:\s*[3-9]\d\dpx")

    def test_no_card_text_names_a_command(self):
        work = _part("scripts/52-work.js.part")
        for lit in re.findall(r'"([^"\n]*)"', work):
            if lit.startswith("/api/"):
                continue            # a read's own address, never drawn
            self.assertIsNone(FORBIDDEN.search(lit), lit)


# ---------------------------------------------------------------- runtime

class AddressTest(unittest.TestCase):
    """THE VIEW IS THE ADDRESS: every filter and the drawer, and the old
    addresses land on their filter."""

    @classmethod
    def setUpClass(cls):
        cls.out = _run("""
const p = s => wkParse(s, "q");
out.round = wkQuery(p("lens=list&stage=review&project=alpha&move=bob&prio=P1&q=relay&stuck=1&back=1&asked=1&notask=1&today=1&group=move&sort=longest&page=3&card=task/4"), "flow", null, "q");
out.back = wkQuery(p(out.round.slice(1)), "flow", null, "q");
out.def = wkQuery(wkDefault(), "flow", null, "q");
out.old = p("priority=P0,P1&owner=unowned&q=x&group=none&status=open");
out.only = [p("only=stalled").stuck, p("only=@bob").move, p("only=live").stuck];
out.junk = p("lens=grid&stage=nope&group=nope&sort=nope&page=-4&prio=P9");
out.entry = [wkEntry(Object.assign(wkDefault(), WK_ENTRY.backlog)),
             wkEntry(Object.assign(wkDefault(), WK_ENTRY.pipeline)),
             wkEntry(wkDefault()), wkEntry(Object.assign(wkDefault(), {stage: "todo"}))];
out.bq = wkQuery(Object.assign(wkDefault(), WK_ENTRY.backlog, {q: "x"}), "backlog", null, "q");
out.pq = wkQuery(Object.assign(wkDefault(), WK_ENTRY.pipeline, {project: "helm"}), "pipeline", null, "q");
out.locked = wkQuery(Object.assign(wkDefault(), {project: "alpha", q: "x", card: "task/4"}), "flow", {project: "alpha"}, "wq");
out.ranks = [wkQuery(p("prio=P0,P1"), "flow", null, "q"), p(wkQuery(p("prio=P1,P0,P9"), "flow", null, "q").slice(1)).prio,
             p("priority=unranked,P2").prio];
out.owners = [p("owner=alice,bob").move, p("owner=bob").move];
""")

    def test_the_address_holds_every_filter_and_the_drawer(self):
        r = self.out["round"]
        for part in ("lens=list", "stage=review", "project=alpha", "move=bob",
                     "prio=P1", "q=relay", "stuck=1", "back=1", "asked=1",
                     "notask=1", "today=1", "group=move", "sort=longest",
                     "page=3", "card=task/4"):
            self.assertIn(part, r)
        self.assertEqual(self.out["back"], r, "the address reads back as itself")
        self.assertEqual(self.out["def"], "")

    def test_the_old_backlog_and_board_addresses_land_on_their_filter(self):
        """The old backlog's `priority=P0,P1` is BOTH ranks (its quick chips:
        "two ranks must OR"), never P0 alone with every P1 row dropped; an
        old `owner` list names one seat or none, never a key no card has."""
        old = self.out["old"]
        self.assertEqual((old["prio"], old["move"], old["q"], old["group"]),
                         ("P0,P1", "unowned", "x", "one"))
        self.assertEqual(self.out["only"], [True, "bob", False])
        self.assertEqual(self.out["owners"], ["", "bob"])

    def test_a_set_of_ranks_round_trips_through_the_address(self):
        """test_web_backlog_view's round trip, carried: `prio=P0,P1` reads
        back as itself, in rank order, and a rank no menu offers drops."""
        self.assertEqual(self.out["ranks"], ["?prio=P0,P1", "P0,P1", "P2,none"])

    def test_a_value_no_menu_offers_falls_back_to_the_default(self):
        j = self.out["junk"]
        self.assertEqual((j["lens"], j["stage"], j["group"], j["sort"], j["page"], j["prio"]),
                         ("river", "", "stage", "", 1, ""))

    def test_backlog_and_pipeline_are_entries_into_the_one_page(self):
        self.assertEqual(self.out["entry"], ["backlog", "pipeline", "flow", "flow"])
        # an entry's address leaves out what the entry implies
        self.assertEqual(self.out["bq"], "?q=x")
        self.assertEqual(self.out["pq"], "?project=helm")
        # a project's tab leaves out its locked project, its search is `wq`
        self.assertEqual(self.out["locked"], "?wq=x&card=task/4")


class OneCountEverywhereTest(unittest.TestCase):
    """SLICE 3: the badge, Home's tile, a project's row and the head row are
    one count over the server's cards, and a source not read is never a
    zero."""

    @classmethod
    def setUpClass(cls):
        cls.bodies = _bodies()
        cls.out = _run("""
for (const k of ["whole", "unread", "down"]) {
  const d = B[k], cards = wkCards(d, NOW), M = wkCount(cards);
  out[k] = {badge: wkBadge(d), home: wkHomeTile(d, 0), alpha: wkProjectCount(d, "alpha"),
            beta: wkProjectCount(d, "beta"), n: M.n, stuck: M.stuck,
            bounds: WK_STAGES.map(s => wkBound(d, s.k, "all")),
            betaBounds: WK_STAGES.map(s => wkBound(d, s.k, "beta")),
            alphaBounds: WK_STAGES.map(s => wkBound(d, s.k, "alpha")),
            heads: wkHeadsHTML(wkDefault(), M, Object.fromEntries(WK_STAGES.map(s => [s.k, wkBound(d, s.k, "all")])))};
}
out.stale = wkHomeTile(B.whole, 4 * 45 + 1);
""", cls.bodies)

    def test_the_head_row_counts_what_the_server_counted(self):  # noqa: VACUOUS_ASSERTION — `exact` is asserted True for four stages and False for Landed in one loop, and Landed's bound "at_least" is an unconditional positive on the same stage entry
        d = self.bodies["whole"]
        for s, c in d["counts"]["stages"].items():
            with self.subTest(stage=s):
                # Landed over every project is a floor: beta merges by hand,
                # so only alpha's lands are measured (task/3723)
                self.assertEqual(c["exact"], s != "landed")
                self.assertEqual(self.out["whole"]["n"][s]["all"], c["n"])
        self.assertEqual(d["counts"]["stages"]["landed"]["bound"], "at_least")
        self.assertEqual(self.out["whole"]["stuck"], d["counts"]["stuck"])
        self.assertGreater(d["counts"]["stuck"], 0)

    def test_the_badge_is_the_stuck_count_over_every_project(self):
        d = self.bodies["whole"]
        b = self.out["whole"]["badge"]
        self.assertEqual(b["n"], d["counts"]["stuck"])
        self.assertIn("stuck, across all projects", b["title"])
        self.assertEqual(self.out["down"]["badge"]["n"], "?")

    def test_home_prints_the_stage_row_word_for_word(self):
        st = self.bodies["whole"]["counts"]["stages"]
        home = _text(self.out["whole"]["home"])
        self.assertIn("%d to do › %d building › %d in review › %d landing › at least %d landed"
                      % tuple(st[s]["n"] for s in ("todo", "building", "review",
                                                   "landing", "landed")), home)
        self.assertIn("%d stuck" % self.bodies["whole"]["counts"]["stuck"], home)
        self.assertIn("all projects", home)
        self.assertIn("not read", _text(self.out["down"]["home"]))
        self.assertIn("stale", _text(self.out["stale"]))

    def test_a_projects_row_is_its_share_of_the_same_cards(self):
        bp = self.bodies["whole"]["counts"]["by_project"]["alpha"]
        alpha = _text(self.out["whole"]["alpha"])
        self.assertIn("%d to do" % bp["todo"], alpha)
        self.assertIn("%d under way" % (bp["building"] + bp["review"] + bp["landing"]), alpha)
        # the landed count rides the hover (the row's land cell says when)
        self.assertIn("%d landed in the last 24 h" % bp["landed"], self.out["whole"]["alpha"])
        self.assertNotIn("landed", alpha)
        self.assertIn("?", _text(self.out["down"]["alpha"]))

    def test_a_project_not_read_is_a_floor_or_unknown_never_a_zero(self):
        u = self.out["unread"]
        # the pipeline stages are floors over every project, To do a ceiling
        self.assertEqual(u["bounds"][1:4], ["at_least"] * 3)
        self.assertEqual(u["bounds"][0], "at_most")
        # beta, the project not read, carries them; alpha, read, does not
        self.assertEqual(u["betaBounds"][1:4], ["at_least"] * 3)
        self.assertEqual(u["alphaBounds"], [None] * 5)
        beta = _text(u["beta"])
        self.assertNotIn("0 under way", beta)
        self.assertTrue("?" in beta or "at least" in beta, beta)
        home = _text(u["home"])
        self.assertIn("at least", home)
        self.assertIn("not read", home)
        self.assertRegex(_text(u["heads"]), r"≥ \d")
        self.assertNotEqual(u["badge"]["n"], 0)


class CardWordsTest(unittest.TestCase):
    """What one card says: whose move, why, every owed move, how it landed."""

    @classmethod
    def setUpClass(cls):
        cls.bodies = _bodies()
        cls.out = _run("""
const d = B.whole, cards = wkCards(d, NOW), v = wkDefault();
out.face = {}; out.row = {}; out.who = {}; out.next = {}; out.alarm = {}; out.sub = {};
cards.forEach(c => { out.face[c.key] = wkCardHTML(c, v, null, d, NOW); out.row[c.key] = wkRowHTML(c, v, null, NOW);
  out.who[c.key] = wkWho(c).text; out.next[c.key] = wkNowNext(c, d, NOW); out.alarm[c.key] = c.alarm; out.sub[c.key] = c.sub; });
out.moves = cards.map(c => c.move);
out.menu = wkMenusHTML(wkInst("main"), cards);
out.closed = wkEvWords([NOW - 60, "D", "train9"], cards[0], []);
out.landedEv = wkEvWords([NOW - 7200, "M", "train9", 40, 100], cards[0], []);
out.landedCard = cards.find(c => c.stage === "landed");
""", cls.bodies)

    def test_no_card_names_an_unknown_holder_or_a_command(self):
        for k in self.out["face"]:
            with self.subTest(card=k):
                self.assertIsNone(FORBIDDEN.search(self.out["face"][k]))
                self.assertIsNone(FORBIDDEN.search(self.out["row"][k]))

    def test_a_move_nobody_holds_is_one_phrase(self):
        """Walk 2, finding 19: the undecided review, beta's unheld review
        and the fix owed on the integrator's role each say "nobody has it",
        and the menu counts them under that one label, each card once."""
        for k in ("lane:alpha:stuck-review", "lane:beta:beta-work", "lane:alpha:relay"):
            with self.subTest(card=k):
                self.assertEqual(self.out["who"][k], "nobody has it")
        self.assertEqual(len(self.out["moves"]), len(self.bodies["whole"]["cards"]))
        menu = _text(self.out["menu"])
        self.assertEqual(menu.count("nobody has it"), 1)
        self.assertEqual(self.out["moves"].count("nobody"),
                         sum(1 for c in self.bodies["whole"]["cards"]
                             if c["whose"]["kind"] in ("nobody", "role")))

    def test_a_fix_owed_on_landed_work_is_building_and_red(self):
        face = self.out["face"]["lane:alpha:relay"]
        self.assertEqual(self.out["alarm"]["lane:alpha:relay"], "debt")
        self.assertIn('class="wkcard bad', face)
        self.assertIn("↩ fix owed", face)
        self.assertIn('<i class="n b">', face)
        self.assertIn("stood unanswered", _text(face))

    def test_every_owed_move_is_on_the_card_face(self):
        """The meld's ruling: task/4 has a fix asked in one room and a
        review in another: ONE card, Building, and the review is on its face
        with its own whose move."""
        face = _text(self.out["face"]["task/4"])
        self.assertEqual(self.out["sub"]["task/4"], "fix")
        self.assertIn("+ In review · @carol", face)
        self.assertIn("@alice", face)

    def test_a_land_never_closes_the_task(self):
        """No auto-close at land: task/5 landed in train9 and is still open;
        its card reads "landed, whole ask not yet re-read"."""
        card = self.out["landedCard"]
        self.assertEqual((card["key"], card["sub"]), ("task/5", "open"))
        self.assertIn("landed, whole ask not yet re-read", _text(self.out["face"]["task/5"]))
        self.assertIn("re-reads the whole ask", self.out["next"]["task/5"][1])

    def test_one_landed_time_the_push_and_a_close_is_closed(self):
        """Walk 2, finding 17: every "landed" is the land proof's push time;
        a task's close says "closed", never "landed"."""
        self.assertIn("landed 2h ago", _text(self.out["row"]["task/5"]))
        self.assertEqual(self.out["landedCard"]["land"]["at"], NOW - 7200)
        self.assertEqual(self.out["closed"][1], "Task closed, naming train9")
        self.assertNotIn("landed", self.out["closed"][1].lower())
        self.assertIn("Landed in train9 as LAND 40", self.out["landedEv"][1])

    def test_the_trains_move_and_the_owner_words(self):
        self.assertEqual(self.out["who"]["lane:alpha:ready-lane"], "the train’s move")
        self.assertEqual(self.out["who"]["task/1"], "UNOWNED")
        self.assertIn("★", self.out["face"]["task/1"])
        self.assertIn("P0", self.out["face"]["task/1"])
        self.assertEqual(self.out["who"]["task/2"], "@dana")


class LensesTest(unittest.TestCase):
    """SLICES 4 AND 5: the River's five columns and the List lens's rows,
    groups and sorts, and the drawer under the card it belongs to."""

    @classmethod
    def setUpClass(cls):
        cls.bodies = _bodies()
        cls.out = _run("""
const d = B.whole, cards = wkCards(d, NOW);
const inst = (view) => Object.assign(wkInst("main"), {view: Object.assign(wkDefault(), view)});
out.river = wkRiverHTML(inst({}), d, cards, NOW);
out.riverOpen = wkRiverHTML(Object.assign(inst({card: "task/4"}), {ev: {key: "task/4", rev: d.rev, events: B.events4.events}}), d, cards, NOW);
out.focus = wkRiverHTML(inst({stage: "building", card: "task/4"}), d, cards, NOW);
out.listStage = wkListHTML(inst({lens: "list"}), d, cards, NOW);
out.listMove = wkListHTML(inst({lens: "list", group: "move"}), d, cards, NOW);
out.backlog = wkListHTML(inst(Object.assign({}, WK_ENTRY.backlog)), d, cards, NOW);
out.open = wkListHTML(inst(Object.assign({}, WK_ENTRY.backlog, {card: "task/1"})), d, cards, NOW);
out.longest = cards.filter(c => c.stage === "todo").sort(wkSort("todo", "longest")).map(c => c.key);
out.order = cards.filter(c => c.stage === "todo").sort(wkSort("todo", "")).map(c => c.key);
out.pipe = wkStagesOf(Object.assign(wkDefault(), WK_ENTRY.pipeline));
out.today = cards.filter(c => c.stage === "landed").map(c => [c.key, c.today]);
""", cls.bodies)

    def test_the_river_is_five_columns_one_card_each(self):
        river = self.out["river"]
        self.assertEqual(river.count('<div class="wkcol"'), 5)
        cards = self.bodies["whole"]["cards"]
        for c in cards:
            self.assertEqual(river.count('data-card="%s"' % c["key"]), 1, c["key"])
        self.assertIn("train9", river)
        self.assertIn("LAND 40", river)
        self.assertNotIn("wkdrawer", river)

    def test_the_drawer_opens_across_the_width_under_the_columns(self):
        r = self.out["riverOpen"]
        self.assertGreater(r.index('<div class="wkdrawer"'), r.rindex('<div class="wkcol"'))
        self.assertIn('data-for="task/4"', r)
        # one stage filling the width: the drawer follows its card
        f = self.out["focus"]
        self.assertLess(f.index('data-card="task/4"'), f.index('<div class="wkdrawer"'))

    def test_the_list_is_the_backlogs_rows_with_stage_and_whose_move(self):
        s = self.out["listStage"]
        self.assertIn("<span>stage</span><span>whose move</span>", s)
        # stage groups name no number: the head row above says it
        self.assertNotIn('class="blghead">To do <span class="blcnt">', s)
        m = self.out["listMove"]
        self.assertIn('class="blghead">nobody has it <span class="blcnt">', m)

    def test_the_backlog_is_to_do_as_a_list_with_its_drawer_under_its_row(self):
        b = self.out["backlog"]
        self.assertIn("This is the backlog", b)
        self.assertEqual(b.count('class="wkrow'), 4)          # tasks 1, 2, 3, 9
        o = self.out["open"]
        self.assertLess(o.index('data-card="task/1"'), o.index('<div class="wkdrawer"'))
        self.assertLess(o.index('<div class="wkdrawer"'), o.index('data-card="task/2"'))

    def test_to_do_keeps_the_backlogs_order_and_sort_overrides_it(self):
        self.assertEqual(self.out["order"][0], "task/1")          # P0 first
        self.assertEqual(len(self.out["longest"]), len(self.out["order"]))
        self.assertEqual(self.out["pipe"], ["building", "review", "landing"])

    def test_landed_today_is_the_owners_clock(self):
        self.assertEqual(self.out["today"], [["task/5", True]])


class DrawerTest(unittest.TestCase):
    """THE DRAWER: Now and Next, the land proof, and where it has been, in
    words, with a gap for a crossing helm keeps no time for."""

    @classmethod
    def setUpClass(cls):
        cls.bodies = _bodies()
        cls.out = _run("""
const d = B.whole, cards = wkCards(d, NOW), by = k => cards.find(c => c.key === k);
const i4 = Object.assign(wkInst("main"), {ev: {key: "task/4", rev: d.rev, events: B.events4.events}});
out.task4 = wkDrawerHTML(i4, d, by("task/4"), NOW);
const ir = Object.assign(wkInst("main"), {ev: {key: "lane:alpha:relay", rev: d.rev, events: B.eventsRelay.events}});
out.relay = wkDrawerHTML(ir, d, by("lane:alpha:relay"), NOW);
out.reading = wkDrawerHTML(wkInst("main"), d, by("task/5"), NOW);
// a task nobody owns: under way, its move is the card's and the task's
// missing owner is no second label; in To do, UNOWNED IS its move
out.underway_unowned = wkDrawerHTML(wkInst("main"), d, Object.assign({}, by("task/4"), {owner: null}), NOW);
out.todo_unowned = wkDrawerHTML(wkInst("main"), d, by("task/1"), NOW);
out.words = B.events4.events.map(e => wkEvWords(e, by("task/4"), B.events4.events));
""", cls.bodies)

    def test_now_next_and_where_it_has_been(self):
        t = _text(self.out["task4"])
        for part in ("Now", "Next", "Where it has been", "Now: building",
                     "@bob asked for a fix and attached their own patch",
                     "Round 1: @alice sent it to @bob for review",
                     "Also owed on this work: In review"):
            self.assertIn(part, t)
        self.assertIn('class="otqnotes wknotes"', self.out["task4"])  # the task's own notes
        self.assertIsNone(FORBIDDEN.search(self.out["task4"]))

    def test_a_crossing_helm_keeps_no_time_for_is_a_gap_never_left_out(self):
        self.assertIn('<li class="gap">', self.out["task4"])
        self.assertIn("helm does not record when", self.out["task4"])
        self.assertTrue(all(w for w in self.out["words"] if w is not None))

    def test_a_fix_debt_names_the_owed_fix_and_its_link(self):
        t = _text(self.out["relay"])
        self.assertIn("A fix is owed on landed work", t)
        self.assertIn("No task on file", t)
        self.assertIsNone(FORBIDDEN.search(self.out["relay"]))

    def test_a_fix_debts_land_with_no_train_is_said_never_left_out(self):
        """The meld's Q8: debt after a land carries explicit land proof. The
        relay's work was seen on trunk and no train carried it: the path has
        a gap for the land, and the drawer says what proof there is."""
        t = _text(self.out["relay"])
        self.assertIn("Land proof: seen in the shared code", t)
        self.assertIn("Landed in the shared code (seen there)", t)
        self.assertIn(["G", "landed"], [e[1:] for e in self.bodies["eventsRelay"]["events"]
                                        if e[0] is None])

    def test_a_task_nobody_owns_is_unowned_only_in_to_do(self):
        """Walk 2, finding 19, in the drawer: the card under way already says
        whose move it is, so the task's empty owner field is not a second
        word for the same row; in To do, UNOWNED is the move itself."""
        self.assertIn("UNOWNED", _text(self.out["todo_unowned"]))
        self.assertIn("Where it has been", _text(self.out["underway_unowned"]))
        self.assertNotIn("UNOWNED", _text(self.out["underway_unowned"]))

    def test_a_drawer_says_it_is_reading_before_its_crossings_arrive(self):
        self.assertIn("reading where it has been", self.out["reading"])


class StoryRenderTest(unittest.TestCase):
    """0.3.3 criterion 5.1: one story (task/1, three children, one closed)
    reads "1 of 3 done" on the card face and in the drawer's checklist, and
    the Home tile counts it as the one open story the page holds."""

    @classmethod
    def setUpClass(cls):
        cls.bodies = _bodies()
        cls.out = _run("""
const d = B.story, cards = wkCards(d, NOW), by = k => cards.find(c => c.key === k);
out.face = wkCardHTML(by("task/1"), wkDefault(), null, d, NOW);
out.child_face = wkCardHTML(by("task/4"), wkDefault(), null, d, NOW);
out.root = by("task/1");
out.child = by("task/4");
out.drawer = wkDrawerHTML(Object.assign(wkInst("main"), {ev: {key: "task/1", rev: d.rev, events: []}}), d, by("task/1"), NOW);
out.home = wkHomeTile(d, 0);
out.counts = d.counts;
""", cls.bodies)

    def test_the_root_card_face_reads_done_of_total(self):
        # the root's own story: done of total over its children, all depths
        self.assertIn('class="wkstory"', self.out["face"])
        self.assertIn("1 of 3 done", _text(self.out["face"]))
        self.assertEqual(self.out["root"]["story"]["done"], 1)
        self.assertEqual(self.out["root"]["story"]["total"], 3)

    def test_a_leaf_child_shows_no_rollup_of_its_own(self):
        # task/4 continues task/1 but holds nothing below itself: its own
        # count is 0 of 0, so the face's "when total>0" gate keeps it from
        # faking a story roll-up of its own. (The positive control for
        # "wkstory appears" is the root's face in the arm above.)
        self.assertEqual(self.out["child"]["story"]["total"], 0)
        self.assertIn(self.out["child"]["title"], self.out["child_face"])
        self.assertNotIn('class="wkstory"', self.out["child_face"])

    def test_the_drawer_lists_the_story_as_a_checklist(self):
        t = self.out["drawer"]
        self.assertIn('class="wkchild"', t)
        self.assertEqual(t.count('class="wkchild"'), 3)
        self.assertIn("1 of 3 done", _text(t))
        # the one closed child is ticked; the open ones are not
        self.assertEqual(t.count('class="wkcheck on"'), 1)
        self.assertIn("task/3", t)
        self.assertIn("task/2", t)
        self.assertIn("task/4", t)
        self.assertIsNone(FORBIDDEN.search(t))

    def test_the_home_tile_counts_the_open_story(self):
        self.assertIn("1 open story", _text(self.out["home"]))
        self.assertEqual(self.out["counts"]["stories_open"], 1)


class StoryGroupTest(unittest.TestCase):
    """0.3.3 criterion 5.1, the List-lens story GROUP: a story's members
    (the root and its open children, one card each) sit under a single group
    keyed by the story's root, and the group's head names the story's OWN
    roll-up — "1 of 2 done" — not the group's row count. A story of one (a
    task with no children) is still a story: it forms a group of its own,
    keyed by its own root, and, holding no roll-up, sorts after every story
    that has one."""

    @classmethod
    def setUpClass(cls):
        cls.bodies = _bodies()
        cls.out = _run("""
const d = B.storiesGroup, cards = wkCards(d, NOW), by = k => cards.find(c => c.key === k);
const inst = (view) => Object.assign(wkInst("main"), {view: Object.assign(wkDefault(), view)});
out.list = wkListHTML(inst({lens: "list", group: "story"}), d, cards, NOW);
out.groups = [...out.list.matchAll(/data-g="([^"]*)"/g)].map(m => m[1]);
out.root = by("task/5").story;
out.child = by("task/6").story;
out.singleton = by("task/8").story;
out.roots = {
  "task/1": (by("task/1").story || {}).root,
  "task/2": (by("task/2").story || {}).root,
  "task/5": (by("task/5").story || {}).root,
  "task/6": (by("task/6").story || {}).root,
  "task/8": (by("task/8").story || {}).root
};
out.menu = wkMenusHTML(inst({lens: "list", group: "story"}), cards);
""", cls.bodies)

    def test_the_members_share_one_group_keyed_by_the_story_root(self):
        # task/5 (the root) and task/6 (its open child) share one group,
        # keyed by the root; the closed child task/7 is not a To do card,
        # so the group holds the root and its one open child.
        self.assertIn("task/5", self.out["groups"])
        self.assertEqual(self.out["roots"]["task/5"], "task/5")
        self.assertEqual(self.out["roots"]["task/6"], "task/5")
        self.assertNotIn("task/7", self.out["groups"])

    def test_the_story_head_names_the_root_rollup_not_the_row_count(self):
        # the group holds two cards (the root and its open child); the head
        # must read the root's own "1 of 2 done" — the roll-up span — and
        # carry no row-count span at all, so the number it owns is the
        # story's, not a count of the two members.
        m = re.search(r'data-g="task/5".*?</summary>', self.out["list"])
        self.assertIsNotNone(m, self.out["list"])
        raw = m.group(0)
        head = _text(raw)
        self.assertIn('class="cfgmut"', raw)
        self.assertIn("1 of 2 done", head)
        self.assertNotIn('class="blcnt"', raw)

    def test_a_story_of_one_sorts_after_every_story_with_a_rollup(self):
        # task/8 has no children: a story of one, its own root (task/8) with
        # no roll-up. It forms a group of its own and, holding no roll-up,
        # sorts after every story that has one.
        self.assertEqual(self.out["singleton"]["root_total"], 0)
        self.assertEqual(self.out["singleton"]["root"], "task/8")
        self.assertIn("task/8", self.out["groups"])
        self.assertEqual(self.out["groups"][-1], "task/8")
        self.assertLess(self.out["groups"].index("task/5"),
                        self.out["groups"].index("task/8"))

    def test_the_group_menu_offers_story_and_says_so(self):
        self.assertIn('data-m="group"', self.out["menu"])
        self.assertIn("Group: story", _text(self.out["menu"]))
        self.assertIsNone(FORBIDDEN.search(self.out["list"]))


class LinkWordsTest(unittest.TestCase):
    """task/3703: the one join links a card by a STORED key (`taskkey`):
    the task recorded for the work. Such a card says so, and never "No task
    on file", which only a card with no task says."""

    def test_a_stored_link_is_said_never_no_task(self):
        out = _run("""
const c = {stage: "review", lanes: ["fix-task-7"]};
out.stored = wkLinkWords(Object.assign({}, c, {task: "task/8", link: {how: "stored", why: null}}));
out.literal = wkLinkWords(Object.assign({}, c, {task: "task/7", link: {how: "literal", why: null}}));
out.none = wkLinkWords(Object.assign({}, c, {task: null, link: {how: "unknown", why: "no task is recorded for this work or named by it"}}));
""")
        self.assertIn("task/8", out["stored"])
        self.assertNotIn("No task on file", out["stored"])
        self.assertIsNone(FORBIDDEN.search(out["stored"]))
        # the controls: a literal link and a card with no task
        self.assertIn("in the work room", out["literal"])
        self.assertIn("No task on file", out["none"])


class ViewSquaredWithTheReadTest(unittest.TestCase):
    """task/3482 D1 and D3, carried by the page that retired the backlog."""

    @classmethod
    def setUpClass(cls):
        cls.bodies = _bodies()
        cls.out = _run("""
const d = B.whole, reg = ["alpha", "beta", "gamma"];
out.unknown = wkNormalize(Object.assign(wkDefault(), {project: "nosuch"}), d, reg).project;
out.registered = wkNormalize(Object.assign(wkDefault(), {project: "gamma"}), d, reg).project;
out.unread = wkNormalize(Object.assign(wkDefault(), {project: "nosuch"}), null, reg).project;
const v = wkNormalize(Object.assign(wkDefault(), {move: "DANA"}), d, reg);
out.move = v.move;
const cards = wkCards(d, NOW).filter(c => wkPass(c, v, null));
out.menu = wkMenusHTML(Object.assign(wkInst("main"), {view: v}), wkCards(d, NOW));
out.scope = wkScopeHTML(Object.assign(wkInst("main"), {view: Object.assign(wkDefault(), {project: "nosuch"})}), d, reg);
out.addr = wkQuery(wkNormalize(Object.assign(wkDefault(), {project: "nosuch"}), d, reg), "flow", null, "q");
out.matched = cards.map(c => c.key);
""", cls.bodies)

    def test_an_unknown_project_falls_back_and_leaves_the_address(self):
        """D1: a project neither the work nor the registry knows falls back
        to every project and is dropped from the address; the menu never
        lists it. A registered project with no work stays; nothing is
        dropped before the work is read."""
        self.assertEqual(self.out["unknown"], "all")
        self.assertEqual(self.out["addr"], "")
        self.assertEqual(self.out["registered"], "gamma")
        self.assertEqual(self.out["unread"], "nosuch")
        self.assertNotIn('data-v="nosuch"', self.out["scope"])

    def test_a_seat_that_differs_only_in_case_is_one_entry(self):
        """D3: the server's filters ignore case, so a pick of DANA is the
        cards' own @dana, one menu entry, checked."""
        self.assertEqual(self.out["move"], "dana")
        self.assertEqual(self.out["menu"].count('data-v="dana"'), 1)
        self.assertNotIn('data-v="DANA"', self.out["menu"])
        self.assertEqual(self.out["matched"], ["task/2"])


# the page's composition over one body, the way `wkPaint` calls it
PARTS = """
let WORK = null;
let projects = [];
function parts(d, view, reg) {
  WORK = d ? {d: d, at: Date.now()} : null;
  projects = (reg || ["alpha", "beta", "gamma"]).map(name => ({name: name}));
  const inst = Object.assign(wkInst("main"), {view: Object.assign(wkDefault(), view || {})});
  return wkParts(inst);
}
"""


class NeverAZeroTest(unittest.TestCase):
    """A SOURCE NOT READ IS NEVER A ZERO on ANY count the page prints: the
    chips, the "Showing" line, the Whose move and Rank menus, the Project
    menu, the columns and the read line, not only the badge, Home, a
    project's row and the stage heads (OneCountEverywhereTest). The retired
    pages pinned it (test_web_landboard test_nothing_read_is_not_a_zero,
    test_web_lr test_a_FAILED_read_clears_every_number_to_UNKNOWN,
    test_web_pipeline_truth test_a_restart_draws_no_zero and
    test_a_board_not_read_draws_no_empty_column, test_web_board
    test_a_failed_read_draws_unknown_columns_never_empty_ones); these arms
    carry the pins onto the one page."""

    @classmethod
    def setUpClass(cls):
        cls.bodies = _bodies()
        cls.out = _run("""
for (const k of ["unread", "down", "none", "restart"]) {
  const d = k === "none" ? null : B[k];
  const p = parts(d, {}), f = parts(d, {stage: "building"});
  out[k] = {chips: p.chips, line: p.line, menus: p.menus, scope: p.scope, read: p.read,
            badge: wkBadge(d), body: p.body, focus: f.body};
}
const more = JSON.parse(JSON.stringify(B.unread));
more.scope.unread = more.scope.unread.concat(["delta"]);
out.moreScope = parts(more, {}, ["alpha", "beta", "gamma", "delta"]).scope;
out.betaView = parts(B.whole, {project: "beta"}).body;
out.dispatch = {read: parts(B.dispatch, {}).read, badge: wkBadge(B.dispatch)};
out.whole = parts(B.whole, {}).read;
out.atMost0 = wkNum(0, "at_most", false);
""", cls.bodies, prelude=PARTS)

    def test_nothing_read_draws_no_zero_on_any_count(self):
        for k in ("unread", "down", "none", "restart"):
            for part in ("chips", "line", "menus", "scope"):
                with self.subTest(body=k, part=part):
                    self.assertNotRegex(self.out[k][part], r">0<")
        # the positive control: before the read every count is "?"
        self.assertIn("?", _text(self.out["down"]["chips"]))
        self.assertIn("Showing ?", _text(self.out["none"]["line"]))

    def test_a_partial_reading_bounds_the_chips_and_the_line_like_the_badge(self):
        """The badge said "at least N" (a project not read) while the stuck
        chip said a bare N and "Showing" a bare total: three answers at one
        moment, the defect this page exists to remove."""
        u = self.out["unread"]
        self.assertTrue(u["badge"]["title"].startswith("at least"), u["badge"])
        stuck = re.search(r'data-chip="stuck".*?</button>', u["chips"]).group(0)
        self.assertIn("≥ %d" % u["badge"]["n"], _text(stuck))
        self.assertIn("Showing at least", _text(u["line"]))

    def test_a_stage_nobody_read_is_not_read_yet_never_empty(self):
        for key in ("body", "focus"):
            with self.subTest(view=key):
                self.assertNotIn("none match", self.out["restart"][key])
                self.assertIn("not read yet", _text(self.out["restart"][key]))
        # read, and no filter on: an empty stage is "nothing here", never a
        # filter blamed for it
        self.assertIn("nothing here", _text(self.out["betaView"]))
        self.assertNotIn("none match", self.out["betaView"])

    def test_a_project_not_read_is_never_one_with_nothing_in_it(self):
        note = _text(self.out["moreScope"])
        self.assertIn("1 registered project with nothing in either is not listed", note)
        self.assertIn("1 registered project not read yet is not listed either", note)
        self.assertIn("delta", self.out["moreScope"])       # named, in its hover

    def test_the_read_line_is_one_count_with_the_names_in_its_hover(self):
        """task/3632: a strip that listed 238 names. One count; the names
        ride the hover."""
        r = self.out["unread"]["read"]
        self.assertIn("1 project not read yet", _text(r))
        self.assertNotIn("beta", _text(r))
        self.assertIn('title="beta"', r)

    def test_every_reason_a_reading_is_not_whole_is_named(self):
        self.assertIn("no project’s pipeline was read yet", _text(self.out["restart"]["read"]))
        d = self.out["dispatch"]
        self.assertIn("review history could not be read", _text(d["read"]))
        self.assertIn("review history could not be read", d["badge"]["title"])
        self.assertNotIn("some projects are not read yet", d["badge"]["title"])
        self.assertEqual(self.out["whole"], "")              # a whole reading says nothing

    def test_a_ceiling_of_nothing_is_zero(self):
        self.assertEqual(self.out["atMost0"], "0")


def _unsure_bodies():
    """{whole, unsure}: the planted world read whole, and the same world
    while alpha's pipeline is not read yet (its lands leg still loading) and
    the work rooms are not read. The server then draws every open alpha task
    in To do, task/4 among them though its room is open and its fix is
    owed, and beta's task/9 in To do while the rooms are not read."""
    wm = W._wm()
    loading = W._sec(loading=True, measured_at=None, age_s=None)
    return {"whole": wm.view(_world(), NOW),
            "unsure": wm.view(_world(lands=dict(loading, scope="alpha"),
                                     seats=loading), NOW)}


def _notask_chip(chips):
    """The "no task on file" chip, in words."""
    return _text(re.search(r'<button[^>]*data-chip="notask".*?</button>', chips).group(0))


class AToDoCardNotWhollyReadTest(unittest.TestCase):
    """A TO-DO CARD IS ONLY AS FAR AS HELM HAS READ. The server draws every
    open task in To do until a work room or a pipeline row moves it on, so
    while the card's project's pipeline is not read, or the work rooms are
    not read, a card in To do may already be under way. Then no surface
    says that nobody holds it, that it is not started or has no work room,
    or that any idle seat may take it: NOW and NEXT on every To do branch
    (unowned, not ranked, handed back, claimed, assigned), the card face and
    the list row ("not read", never UNOWNED), the drawer's task-owner word
    and its "Now:" line, the Whose move menu and group (the cards not read
    counted apart, as a floor), and the "no task on file" chip (a floor, or
    "?" over nothing). An owner or a claim is a stored fact and is still
    named. The controls: the same cards read whole keep their words."""

    @classmethod
    def setUpClass(cls):
        cls.out = _run("""
const handed = {sub: "released", released: {by: "seat-a", at: NOW - 3 * 3600}};
for (const k of ["whole", "unsure"]) {
  const d = B[k], cards = wkCards(d, NOW), by = key => cards.find(c => c.key === key), t1 = by("task/1");
  const cs = {unowned: t1, unranked: Object.assign({}, t1, {rank: null}), released: Object.assign({}, t1, handed),
              claimed: by("task/3"), assigned: by("task/2"), underway: by("task/4"), rooms: by("task/9")};
  const o = out[k] = {scope: d.scope, stage: {}, nn: {}, face: {}, row: {}, drawer: {}};
  Object.keys(cs).forEach(n => {
    const c = cs[n], inst = Object.assign(wkInst("main"), {ev: {key: c.key, rev: d.rev, events: []}});
    o.stage[n] = c.stage;
    o.nn[n] = wkNowNext(c, d, NOW);
    o.face[n] = wkCardHTML(c, wkDefault(), null, d, NOW);
    o.row[n] = wkRowHTML(c, wkDefault(), null, NOW);
    o.drawer[n] = wkDrawerHTML(inst, d, c, NOW);
  });
  const every = parts(d, {}), alpha = parts(d, {project: "alpha"});
  o.menus = every.menus; o.chips = every.chips; o.alphaChips = alpha.chips;
  o.listMove = wkListHTML(Object.assign(wkInst("main"), {view: Object.assign(wkDefault(), {lens: "list", group: "move"})}), d, cards, NOW);
}
""", _unsure_bodies(), prelude=PARTS)

    def test_the_read_under_test_is_not_whole_and_the_control_is(self):
        u, w = self.out["unsure"], self.out["whole"]
        self.assertEqual((u["scope"]["complete"], u["scope"]["unread"], u["scope"]["rooms_read"]),
                         (False, ["alpha"], False))
        self.assertEqual((w["scope"]["complete"], w["scope"]["unread"], w["scope"]["rooms_read"]),
                         (True, [], True))
        # task/4 is under way in its room; the read not whole draws it in To do
        self.assertEqual((w["stage"]["underway"], u["stage"]["underway"]), ("building", "todo"))
        self.assertEqual(set(u["stage"].values()), {"todo"})

    def test_now_and_next_say_it_may_be_under_way_on_every_branch(self):
        nn = self.out["unsure"]["nn"]
        may = "This project’s pipeline is not read yet, so it may already be under way."
        free = "The next read settles whether a seat already has it."
        self.assertEqual(nn["unowned"], ["On the to-do list as far as helm has read, ranked P0. " + may, free])
        self.assertEqual(nn["unranked"], ["On the to-do list as far as helm has read, not ranked yet. " + may, free])
        self.assertEqual(nn["released"], ["Back on the to-do list as far as helm has read: @seat-a handed it back "
                                          "to the pool 3h ago. " + may, free])
        self.assertEqual(nn["claimed"], ["Claimed by @erin. " + may,
                                         "The next read settles whether @erin has opened a work room."])
        self.assertEqual(nn["assigned"], ["On the to-do list as far as helm has read, ranked P1, assigned to @dana. " + may,
                                          "The next read settles whether @dana has started it."])
        self.assertEqual(nn["underway"], ["On the to-do list as far as helm has read, ranked P1, assigned to @alice. " + may,
                                          "The next read settles whether @alice has started it."])
        self.assertEqual(nn["rooms"], ["On the to-do list as far as helm has read, ranked P1. The work rooms are not "
                                       "read yet, so it may already be under way.", free])

    def test_no_now_or_next_offers_the_card_or_says_nobody_holds_it(self):
        said = " ".join(sum(self.out["unsure"]["nn"].values(), []))
        self.assertEqual(said.count("so it may already be under way."), 7)
        self.assertEqual(said.count("The next read settles whether"), 7)
        self.assertNotIn("Any idle seat", said)
        self.assertNotIn("Nobody holds it", said)
        self.assertNotIn("Not started", said)
        self.assertNotIn("no work room yet", said)
        self.assertNotIn("then a seat takes it", said)

    def test_the_card_face_and_row_say_not_read_never_unowned(self):
        f = self.out["unsure"]["face"]
        self.assertIn(">not read</span>", f["unowned"])
        self.assertIn("this project’s pipeline is not read yet", f["unowned"])     # the chip's hover
        self.assertNotIn("UNOWNED", f["unowned"])
        row = self.out["unsure"]["row"]["unowned"]
        self.assertIn(">not read</span>", row)
        self.assertNotIn("UNOWNED", row)
        rooms = f["rooms"]
        self.assertIn(">not read</span>", rooms)
        self.assertIn("the work rooms are not read yet", rooms)
        self.assertNotIn("UNOWNED", rooms)
        # an owner is a stored fact, still named
        self.assertIn(">@dana</span>", f["assigned"])

    def test_the_card_face_never_offers_a_handed_back_card_or_denies_a_claimed_one_a_room(self):
        rel = _text(self.out["unsure"]["face"]["released"])
        self.assertIn("↩ handed back by @seat-a, 3h ago; this project’s pipeline is not read yet", rel)
        self.assertNotIn("any seat may take it", rel)
        cl = _text(self.out["unsure"]["face"]["claimed"])
        self.assertIn("claimed; this project’s pipeline is not read yet", cl)
        self.assertNotIn("no work room yet", cl)

    def test_the_drawer_says_not_read_never_unowned_or_free(self):
        dr = self.out["unsure"]["drawer"]
        t = _text(dr["unowned"])
        self.assertIn("Now: to do · not read · for 3d", t)
        self.assertIn("who holds it: not read yet", t)
        self.assertIn("so it may already be under way", t)
        self.assertNotIn("UNOWNED", t)
        self.assertNotIn("Any idle seat", t)
        self.assertNotIn("Nobody holds it", t)
        a = _text(dr["underway"])
        self.assertIn("assigned to @alice", a)
        self.assertIn("task owner @alice", a)
        self.assertNotIn("Not started", a)
        c = _text(dr["claimed"])
        self.assertIn("Claimed by @erin", c)
        self.assertNotIn("no work room yet", c)

    def test_the_whose_move_menu_and_group_count_the_cards_not_read_apart_as_a_floor(self):
        m = _text(self.out["unsure"]["menus"])
        self.assertIn("not read ≥ 2", m)
        self.assertNotIn("UNOWNED", m)
        g = _text(self.out["unsure"]["listMove"])
        self.assertIn("not read ≥ 2", g)
        self.assertNotIn("UNOWNED", g)

    def test_the_no_task_chip_over_a_read_not_whole_is_a_floor(self):
        self.assertRegex(_notask_chip(self.out["unsure"]["chips"]), r"^no task on file ≥ \d+$")
        self.assertEqual(_notask_chip(self.out["unsure"]["alphaChips"]), "no task on file ?")

    def test_a_card_read_whole_keeps_its_now_and_next(self):
        nn = self.out["whole"]["nn"]
        self.assertEqual(nn["unowned"], ["On the to-do list, ranked P0. Nobody holds it.", "Any idle seat may take it."])
        self.assertEqual(nn["unranked"], ["On the to-do list, not ranked yet. Nobody holds it.",
                                          "Someone ranks it; then a seat takes it."])
        self.assertEqual(nn["released"], ["Back on the to-do list: @seat-a handed it back to the pool 3h ago.",
                                          "Any idle seat may take it."])
        self.assertEqual(nn["claimed"], ["Claimed by @erin; no work room yet.", "@erin opens a work room and starts building."])
        self.assertEqual(nn["assigned"], ["On the to-do list, ranked P1, assigned to @dana. Not started.", "@dana starts it."])
        self.assertEqual(nn["rooms"], ["On the to-do list, ranked P1. Nobody holds it.", "Any idle seat may take it."])

    def test_a_card_read_whole_keeps_its_face_drawer_menu_and_chip(self):
        w = self.out["whole"]
        self.assertIn(">UNOWNED</span>", w["face"]["unowned"])
        self.assertIn(">UNOWNED</span>", w["row"]["unowned"])
        self.assertIn("↩ handed back by @seat-a, 3h ago; any seat may take it", _text(w["face"]["released"]))
        self.assertIn("claimed; no work room yet", _text(w["face"]["claimed"]))
        t = _text(w["drawer"]["unowned"])
        self.assertIn("Now: to do · UNOWNED · for 3d", t)
        self.assertEqual(t.count("UNOWNED"), 2)          # the task-owner word and the Now line
        self.assertIn("UNOWNED 2", _text(w["menus"]))
        self.assertIn("UNOWNED 2", _text(w["listMove"]))
        self.assertRegex(_notask_chip(w["alphaChips"]), r"^no task on file \d+$")


def _title(html):
    """The hover of the first element in `html` that has one."""
    return re.search(r'title="([^"]*)"', html).group(1)



class AToDoCardInAStaleReadingTest(unittest.TestCase):
    """A STALE READING IS NOT A WHOLE ONE. When the pipeline reading is past
    its bound, the page head already says the stages under way may have
    moved, so a to-do card in it may already be under way too: NOW and NEXT,
    the card face and the drawer hedge as they do while a project is not
    read, naming the reading's age. The control is the same reading fresh,
    which keeps today's words."""

    @classmethod
    def setUpClass(cls):
        cls.out = _run("""
const fresh = B.whole;
const stale = Object.assign({}, fresh, {reading: Object.assign({}, fresh.reading,
  {stale: true, age_s: 3649, limit_s: 1200})});
for (const [k, d] of [["fresh", fresh], ["stale", stale]]) {
  const cards = wkCards(d, NOW), by = key => cards.find(c => c.key === key);
  const cs = {unowned: by("task/1"), assigned: by("task/2"), claimed: by("task/3")};
  const o = out[k] = {nn: {}, face: {}, drawer: {}, why: {}};
  Object.keys(cs).forEach(n => {
    const c = cs[n], inst = Object.assign(wkInst("main"), {ev: {key: c.key, rev: d.rev, events: []}});
    o.why[n] = wkUnsure(c, d);
    o.nn[n] = wkNowNext(c, d, NOW);
    o.face[n] = wkCardHTML(c, wkDefault(), null, d, NOW);
    o.drawer[n] = wkDrawerHTML(inst, d, c, NOW);
  });
}
""", _unsure_bodies(), prelude=PARTS)

    def test_a_stale_reading_is_a_reason_a_to_do_card_is_unsure(self):
        why = self.out["stale"]["why"]
        self.assertEqual(why["unowned"], "the pipeline reading is stale, 1h old, past its 20m bound")
        self.assertEqual(why["assigned"], why["unowned"])
        self.assertEqual(self.out["fresh"]["why"], {"unowned": "", "assigned": "", "claimed": ""})

    def test_now_and_next_hedge_in_a_stale_reading(self):
        nn = self.out["stale"]["nn"]
        may = "The pipeline reading is stale, 1h old, past its 20m bound, so it may already be under way."
        self.assertEqual(nn["unowned"], ["On the to-do list as far as helm has read, ranked P0. " + may,
                                         "The next read settles whether a seat already has it."])
        self.assertEqual(nn["assigned"], ["On the to-do list as far as helm has read, ranked P1, assigned to @dana. "
                                          + may, "The next read settles whether @dana has started it."])
        said = " ".join(sum(nn.values(), []))
        self.assertNotIn("Nobody holds it", said)
        self.assertNotIn("Not started", said)
        self.assertNotIn("Any idle seat", said)

    def test_the_card_face_and_drawer_say_not_read_in_a_stale_reading(self):
        f, dr = self.out["stale"]["face"], _text(self.out["stale"]["drawer"]["unowned"])
        self.assertIn(">not read</span>", f["unowned"])
        self.assertNotIn("UNOWNED", f["unowned"])
        self.assertIn("who holds it: not read yet", dr)
        self.assertNotIn("UNOWNED", dr)

    def test_the_same_reading_fresh_keeps_its_words(self):
        nn = self.out["fresh"]["nn"]
        self.assertEqual(nn["unowned"], ["On the to-do list, ranked P0. Nobody holds it.", "Any idle seat may take it."])
        self.assertEqual(nn["assigned"], ["On the to-do list, ranked P1, assigned to @dana. Not started.", "@dana starts it."])
        self.assertIn(">UNOWNED</span>", self.out["fresh"]["face"]["unowned"])

class ACountNotWhollyReadTest(unittest.TestCase):
    """A COUNT NOT WHOLLY READ SAYS "AT LEAST" OR "?", NEVER A BARE NUMBER OR
    0, inside one project too (the second reading of review 3643):

      * F1: a project's chips, "Showing" line and menus count the cards seen
        in it. When ANY of its five stages is not exact (the to-do list or
        the trains unreadable, the rooms not read, its pipeline not read),
        the cards seen are a floor, and a floor of nothing is "?";
      * F2: a project's row says its stage counts twice, on the line and in
        its hover, and the hover carries each stage's bound as the line does;
      * F3: the Project menu calls a registered project with nothing in it
        idle only when every count of it was read; a project with a count not
        read is named as not read.

    The world: alpha and beta registered, both pipelines read and empty, the
    rooms and the trains read, and the to-do list read (`read`, the positive
    control: every count exact, and an empty one a numeric 0) or unreadable
    (`todoDown`)."""

    @classmethod
    def setUpClass(cls):
        wm, board = W._wm(), W._board([], other={"beta": []})
        down = {"rows": {}, "history": {},
                "unavailable": "the task ledger raised (OSError)"}
        cls.bodies = dict(_bodies(), read=wm.view(W._build(board), NOW),
                          todoDown=wm.view(W._build(board, tasks=down), NOW))
        cls.out = _run("""
const reg = ["alpha", "beta"];
for (const k of ["read", "todoDown"]) {
  const d = B[k], a = parts(d, {project: "alpha"}, reg);
  out[k] = {chips: a.chips, line: a.line, menus: a.menus, scope: parts(d, {}, reg).scope,
            stuckLine: parts(d, {project: "alpha", stuck: true}, reg).line,
            row: wkProjectCount(d, "alpha"), bound: wkCountBound(d, "alpha")};
}
out.betaRow = wkProjectCount(B.unread, "beta");
out.alphaRow = wkProjectCount(B.unread, "alpha");
""", cls.bodies, prelude=PARTS)

    def test_a_project_count_with_any_stage_not_read_is_a_floor(self):
        """F1: the bound asked only Building, In review and Landing, so with
        the to-do list unreadable an empty alpha's chips and Rank menu said
        a bare 0 under a To do head of "?"."""
        body = self.bodies["todoDown"]
        self.assertFalse(body["scope"]["complete"])
        self.assertEqual(body["counts"]["stages"]["todo"]["bound"], "unknown")
        self.assertEqual(body["counts"]["cards"], 0)
        self.assertEqual(self.out["todoDown"]["bound"], "at_least")
        for part in ("chips", "line", "stuckLine", "menus", "scope"):
            with self.subTest(part=part):
                self.assertNotRegex(self.out["todoDown"][part], r">0<")
        self.assertIn("?", _text(self.out["todoDown"]["chips"]))
        self.assertIn("Showing ?", _text(self.out["todoDown"]["line"]))

    def test_a_project_read_whole_still_counts_a_numeric_zero(self):
        """The positive control: the same empty alpha with every source read
        is exact, and each of its empty counts is 0."""
        r = self.out["read"]
        self.assertTrue(self.bodies["read"]["scope"]["complete"])
        self.assertIsNone(r["bound"])
        self.assertRegex(r["chips"], r">0<")
        self.assertNotIn("?", _text(r["chips"]))
        self.assertIn("Showing 0", _text(r["line"]))
        self.assertEqual(_text(r["row"]), "0 to do")
        self.assertEqual(_title(r["row"]), "0 to do · 0 building · 0 in review"
                         " · 0 landing · 0 landed in the last 24 h")

    def test_a_projects_hover_carries_each_stages_bound(self):
        """F2: the row's line said "? to do" while its hover said "0 to do",
        and beta's line "? under way" while its hover said "0 building"."""
        t = _title(self.out["todoDown"]["row"])
        self.assertIn("? to do", t)
        self.assertNotIn("0 to do", t)
        self.assertIn("0 building", t)                      # read, so exact
        beta = _title(self.out["betaRow"])
        for s in ("building", "in review", "landing"):
            with self.subTest(stage=s):
                self.assertIn("? " + s, beta)
                self.assertNotIn("0 " + s, beta)
        n = self.bodies["unread"]["counts"]["by_project"]["beta"]["todo"]
        self.assertGreater(n, 0)
        self.assertIn("at most %d to do" % n, beta)
        # alpha, read whole beside the beta not read, stays exact
        alpha = _title(self.out["alphaRow"])
        bp = self.bodies["unread"]["counts"]["by_project"]["alpha"]
        self.assertIn("%d to do · %d building" % (bp["todo"], bp["building"]), alpha)
        self.assertNotIn("?", alpha)
        self.assertNotIn("at ", alpha)

    def test_an_empty_project_not_read_is_never_idle(self):
        """F3: the Project menu called an empty registered project one "with
        nothing in either" whenever the server did not name it as not read,
        so with the to-do list unreadable alpha and beta, whose to-do items
        nobody read, were called idle."""
        scope = self.out["todoDown"]["scope"]
        self.assertNotIn("with nothing in either", _text(scope))
        self.assertIn("2 registered projects not read yet are not listed", _text(scope))
        self.assertIn('title="not read yet: alpha, beta"', scope)
        # the positive control: read whole, both are idle and none is unread
        note = _text(self.out["read"]["scope"])
        self.assertIn("2 registered projects with nothing in either are not listed", note)
        self.assertNotIn("not read yet", note)


def _carried(state="DONE"):
    """A request, reviewed clean on work-page-3643, that train483 carried
    at its own tip: DONE and pushed ten minutes ago (walk 3, finding 3), or
    still at its gate (`RUNNING`, nothing pushed)."""
    tip = "ab" * 20
    pushed = NOW - 600 if state == "DONE" else None
    trains = {"trains": [{"name": "train483", "state": state, "land": 508,
                          "intent": NOW - 1500, "pushed": pushed,
                          "ended": pushed or NOW - 300, "ran": 27000,
                          "cars": [{"id": "r1", "lane": "work-page-3643", "tip": tip,
                                    "author": "alice", "reader": "bob"}]}],
              "lands": {"train483": {"n": 508, "ran": 27000, "at": pushed}} if pushed else {},
              "ejections": [], "unavailable": None}
    row = W._row("r1", "work-page-3643", "AWAITING_REVIEW", owed_by="integrator",
                 holder="integrator", tip=tip[:12], age_s=7200)
    return W._wm().view(W._build(W._board([row], other={"beta": []}), trains=trains), NOW)


def _partial():
    """alpha read only in part (its server counted more rows than it sent),
    with a land in the window: its pipeline stages are floors, its Landed is
    still measured."""
    board = W._board([W._row("ready", "ready-lane", "READY", holder="integrator",
                             owed_by="integrator", age_s=1800)], other={"beta": []})
    board["projects"]["alpha"]["lanes"]["tally"]["live"] = 5
    return W._wm().view(W._build(board, trains=_trains()), NOW)


class WorkTruthTest(unittest.TestCase):
    """WALK 3'S WORK TRUTH (task/3723): a count not wholly read says "at
    least" or "?", never a bare number or 0, and one number and one word per
    noun on every surface. Each cell of the state table is one arm:

      * finding 1: a project MEASURED (alpha, whose lands helm records),
        measured but PARTIAL (its pipeline read in part), UNMEASURED (beta
        merges by hand: its Landed is "not measured", never 0, on the Work
        tab's head, the row hover, the menu counts and the line), and
        UNREAD (beta not read, still never a 0); over every project Landed
        is a floor, and Home names the scope it counts;
      * finding 2: the badge, Home and the stuck chip on an EXACT, a
        PARTIAL and an UNKNOWN reading: one bound, one formatter
        (`navBadgeText`), so the badge never says 94 beside a chip's ≥94;
        and a stage's flags carry that stage's bound;
      * finding 3: a card whose train LANDED its request is in Landed, never
        "waiting for the train"; one whose train is still GATED waits;
      * finding 8: the Leftovers fold READ counts its records; NOT READ it
        says so, and never counts none of them or says nothing waits."""

    @classmethod
    def setUpClass(cls):
        cls.bodies = dict(_bodies(), partial=_partial(), carried=_carried(),
                          gated=_carried("RUNNING"))
        cls.out = _run("""
const reg = ["alpha", "beta"], cells = [["whole", "alpha"], ["partial", "alpha"], ["whole", "beta"], ["unread", "beta"]];
const tab = (d, p) => { WORK = {d: d, at: Date.now()}; projects = reg.map(n => ({name: n})); return wkParts(wkInst("proj", p)); };
out.cell = {};
for (const [k, p] of cells) {
  const t = tab(B[k], p);
  out.cell[k + ":" + p] = {bounds: WK_STAGES.map(s => wkBound(B[k], s.k, p)), row: wkProjectCount(B[k], p),
                           heads: t.heads, line: t.line, chips: t.chips, menus: t.menus};
}
out.menu = parts(B.whole, {}, reg).scope;
out.allHeads = parts(B.whole, {}, reg).heads;
out.badge = {};
for (const k of ["whole", "unread", "down"]) {
  const b = wkBadge(B[k]), all = k === "down" ? {} : parts(B[k], {}, reg);
  out.badge[k] = {n: b.n, bound: b.bound, text: navBadgeText(b.n, b.bound), home: wkHomeTile(B[k], 0),
                  fold: wkFoldElseHTML(B[k], 0), chips: all.chips || "", heads: all.heads || ""};
}
out.big = [navBadgeText(120, "at_least"), navBadgeText(94, "at_least"), navBadgeText(94, null), navBadgeText("?", "unknown")];
out.train = {};
for (const k of ["carried", "gated"]) {
  const d = B[k], cards = wkCards(d, NOW), inst = wkInst("main");
  out.train[k] = {landing: wkColHTML(inst, d, WK_STAGES[3], cards, 6, NOW), landed: wkColHTML(inst, d, WK_STAGES[4], cards, 6, NOW)};
}
out.left = {down: wkFoldLeftHTML(B.down, NOW), none: wkFoldLeftHTML(null, NOW), whole: wkFoldLeftHTML(B.whole, NOW),
            carried: wkFoldLeftHTML(B.carried, NOW), restart: wkFoldLeftHTML(B.restart, NOW),
            unread: wkFoldLeftHTML(B.unread, NOW)};
const trainsDown = JSON.parse(JSON.stringify(B.whole));
trainsDown.scope.trains_unavailable = "the land log could not be read (OSError)";
out.seams = {};
const dispatchNone = JSON.parse(JSON.stringify(B.dispatch));
dispatchNone.cards.forEach(c => { c.came_back = false; });
for (const [k, d] of [["whole", B.whole], ["restart", B.restart], ["unread", B.unread], ["trainsDown", trainsDown],
                      ["dispatch", B.dispatch], ["dispatchNone", dispatchNone]])
  out.seams[k] = wkFoldSeamsHTML(d, wkCards(d, NOW));
""", cls.bodies, prelude=PARTS)

    def cell(self, key):
        return self.out["cell"][key]

    # ---- finding 1: a project's Landed, by state

    def test_a_measured_project_counts_its_lands(self):
        c = self.cell("whole:alpha")
        self.assertEqual(c["bounds"], [None] * 5)
        n = self.bodies["whole"]["counts"]["by_project"]["alpha"]["landed"]
        self.assertGreater(n, 0)
        self.assertIn("%d landed in the last 24 h" % n, _title(c["row"]))
        landed = re.search(r'data-stage="landed".*?</button>', c["heads"]).group(0)
        self.assertIn('<span class="wkhn">%d</span>' % n, landed)
        self.assertNotIn("not measured", landed)

    def test_a_measured_project_read_in_part_keeps_its_landed_exact(self):
        c = self.cell("partial:alpha")
        self.assertEqual(self.bodies["partial"]["scope"]["partial"], ["alpha"])
        # a floor of what was seen; a floor of nothing is unknown (the
        # server's rule), and never a 0
        self.assertEqual(c["bounds"][1:4], ["unknown", "unknown", "at_least"])
        self.assertIsNone(c["bounds"][4])
        self.assertIn("1 landed in the last 24 h", _title(c["row"]))

    def test_an_unmeasured_project_is_not_measured_never_zero(self):
        c = self.cell("whole:beta")
        self.assertEqual(c["bounds"], [None] * 4 + ["unmeasured"])
        t = _title(c["row"])
        self.assertIn("landed not measured", t)
        self.assertNotIn("0 landed", t)
        landed = re.search(r'data-stage="landed".*?</button>', c["heads"]).group(0)
        self.assertIn("not measured", _text(landed))
        self.assertNotIn('<span class="wkhn">0</span>', landed)
        # the cards seen in beta are a floor: its Landed is not among them
        self.assertIn("Showing at least", _text(c["line"]))
        beta = re.search(r'data-v="beta".*?</button>', self.out["menu"]).group(0)
        self.assertIn("≥", _text(beta))
        # a stuck count takes no landed card, so it stays exact
        stuck = re.search(r'<button[^>]*data-chip="stuck".*?</button>', c["chips"]).group(0)
        self.assertNotIn("≥", _text(stuck))

    def test_an_unread_project_is_still_not_measured_never_zero(self):
        c = self.cell("unread:beta")
        self.assertEqual(c["bounds"][1:4], ["at_least"] * 3)
        self.assertEqual(c["bounds"][4], "unmeasured")
        self.assertIn("landed not measured", _title(c["row"]))
        self.assertNotIn("0 landed", _title(c["row"]))

    def test_landed_over_every_project_is_a_floor_and_home_names_its_scope(self):
        st = self.bodies["whole"]["counts"]["stages"]["landed"]
        self.assertEqual(st["bound"], "at_least")
        landed = re.search(r'data-stage="landed".*?</button>', self.out["allHeads"]).group(0)
        self.assertIn("≥ %d" % st["n"], _text(landed))
        home = _text(self.out["badge"]["whole"]["home"])
        self.assertIn("at least %d landed" % st["n"], home)
        self.assertIn("landed: alpha only", home)

    # ---- finding 2: the badge, Home and the chip, by reading

    def test_an_exact_reading_is_one_bare_number_everywhere(self):
        b = self.out["badge"]["whole"]
        self.assertIsNone(b["bound"])
        self.assertEqual(b["text"], str(b["n"]))
        self.assertIn("%d stuck" % b["n"], _text(b["home"]))
        self.assertNotIn("at least %d stuck" % b["n"], _text(b["home"]))
        stuck = re.search(r'<button[^>]*data-chip="stuck".*?</button>', b["chips"]).group(0)
        self.assertEqual(_text(stuck), "stuck %d" % b["n"])
        self.assertNotRegex(_text(b["heads"]), r"≥ \d+ stuck")

    def test_a_partial_reading_is_at_least_on_the_badge_home_and_chip(self):
        b = self.out["badge"]["unread"]
        self.assertEqual(b["bound"], "at_least")
        self.assertGreater(b["n"], 0)
        self.assertEqual(b["text"], "≥%d" % b["n"])
        self.assertIn("at least %d stuck" % b["n"], _text(b["home"]))
        self.assertIn(">≥%d<" % b["n"], b["fold"])
        stuck = re.search(r'<button[^>]*data-chip="stuck".*?</button>', b["chips"]).group(0)
        self.assertIn("≥ %d" % b["n"], _text(stuck))
        # a stage's flags carry that stage's bound
        self.assertRegex(_text(b["heads"]), r"≥ \d+ stuck")

    def test_an_unknown_reading_is_a_question_mark_everywhere(self):
        b = self.out["badge"]["down"]
        self.assertEqual((b["n"], b["text"]), ("?", "?"))
        self.assertIn("not read", _text(b["home"]))
        self.assertNotRegex(_text(b["home"]), r"\d")

    def test_the_badge_text_is_one_formatter(self):
        self.assertEqual(self.out["big"], ["99+", "≥94", "94", "?"])

    # ---- finding 3: a card and its train

    def test_a_card_whose_train_landed_is_in_landed(self):
        t = self.out["train"]["carried"]
        self.assertNotIn("Work page 3643", t["landing"])
        self.assertNotIn("waiting for the train", t["landing"])
        self.assertIn("Work page 3643", t["landed"])
        self.assertIn("LAND 508", t["landed"])

    def test_a_card_whose_train_is_gated_waits_for_it(self):
        t = self.out["train"]["gated"]
        self.assertIn("Work page 3643", t["landing"])
        self.assertIn("waiting for the train", t["landing"])
        self.assertNotIn("Work page 3643", t["landed"])

    # ---- finding 8: the Leftovers fold

    def test_leftovers_not_read_say_so_never_zero(self):
        self.assertIn("not read yet", _text(self.out["left"]["down"]))
        for k in ("down", "none"):
            with self.subTest(body=k):
                t = _text(self.out["left"][k])
                self.assertIn("not read yet", t)
                self.assertNotIn("0 review record", t)
                self.assertNotIn("Nothing here waits on you", t)
        # the reason the read failed rides with it
        self.assertIn("the work reader raised (OSError)", self.out["left"]["down"])

    def test_leftovers_read_count_their_records(self):
        t = _text(self.out["left"]["whole"])
        self.assertIn("Nothing here waits on you", t)
        self.assertRegex(t, r"\d+ review records?")
        carried = _text(self.out["left"]["carried"])
        self.assertIn("a train already landed", carried)
        self.assertIn("work-page-3643", carried)

    def test_leftovers_over_a_reading_not_whole_never_count_zero(self):
        """NOT READ IS NEVER NONE ON A BODY THAT HAS COUNTS TOO (task/3723):
        the records are typed off the rows of the projects that were read,
        so over a restart (no project's pipeline read) none are typed, and
        the fold must not count them as none or say nothing waits; with
        beta not read each count is a floor beside the badge's "at least"."""
        restart = _text(self.out["left"]["restart"])
        self.assertIn("not read yet", restart)
        self.assertNotRegex(restart, r"\b0 ")
        self.assertNotIn("Nothing here waits on you", restart)
        # read in part: each count is a floor, and a floor of nothing is "?"
        unread = _text(self.out["left"]["unread"])
        self.assertIn("at least 1 landed over an unanswered fix", unread)
        self.assertIn("? review records", unread)
        self.assertNotIn("0 review record", unread)
        # the positive control: the whole reading's counts stay bare
        whole = _text(self.out["left"]["whole"])
        self.assertIn("1 landed over an unanswered fix", whole)
        self.assertNotIn("at least", whole)

    def test_the_seams_fold_never_counts_0_over_a_source_not_read(self):
        """The same rule on the seams fold (task/3723): over a restart the
        cards that came back were counted off the to-do list alone and
        said "none"; with beta not read each stage's count is a floor; and
        with the trains not read in full (the ejection store among them)
        the pieces thrown off a train are a floor, never 0."""
        restart = _text(self.out["seams"]["restart"])
        self.assertIn("each card is in now: not known", restart)
        self.assertNotIn("now: none", restart)
        self.assertRegex(_text(self.out["seams"]["unread"]),
                         r"each card is in now: .*↩ at least \d")
        self.assertIn("? pieces of work were thrown off",
                      _text(self.out["seams"]["trainsDown"]))
        # the positive control: the whole reading's counts stay bare
        whole = _text(self.out["seams"]["whole"])
        self.assertIn("0 pieces of work were thrown off", whole)
        self.assertRegex(whole, r"each card is in now: .*↩ \d")
        self.assertNotIn("at least", whole)

    def test_the_seams_fold_is_a_floor_while_the_review_history_is_not_read(self):  # noqa: VACUOUS_ASSERTION — each absence sits beside an unconditional assertRegex positive on the same fold text
        """A card came back when it reached a stage it is no longer in, and
        the stages it reached are read off the review history: with that
        history not read (or the rooms or the to-do list), fewer cards came
        back than did. The read line says "where each card has been is not
        known" and the came-back chip is a floor; the seams fold, the same
        count split by stage, must not print it bare or as "none"."""
        disp = _text(self.out["seams"]["dispatch"])
        self.assertRegex(disp, r"each card is in now: .*↩ at least \d")
        self.assertNotRegex(disp, r"↩ \d")
        none = _text(self.out["seams"]["dispatchNone"])
        self.assertRegex(none, r"each card is in now: (\?|none in what was read)")
        self.assertNotIn("each card is in now: none.", none)


class StaleReadingTest(unittest.TestCase):
    """A STALE READING IS NEVER DRAWN AS CURRENT (task/3632, walk 2, finding
    21). The server sends `reading.stale`, `age_s` and `limit_s`; the read
    line, the badge's hover and Home's tile say it, once, with the age, and
    the tile keeps its numbers, dated. The retired pins: test_web_home
    test_pipeline_is_the_one_reading_and_keeps_it_when_stale,
    test_web_pipeline_truth test_a_stale_section_keeps_its_last_reading_and_
    says_stale_once and test_a_stale_reading_with_no_clock_says_so_on_every_
    surface and test_a_missing_clock_claims_no_bound, test_web_landboard
    test_an_unread_read_is_said_and_a_stale_one_keeps_its_numbers."""

    @classmethod
    def setUpClass(cls):
        cls.bodies = _bodies()
        cls.out = _run("""
for (const k of ["stale", "noclock", "whole"]) {
  out[k] = {read: parts(B[k], {}).read, home: wkHomeTile(B[k], 0), badge: wkBadge(B[k])};
}
out.held = wkHomeTile(B.whole, 4 * 45 + 1);
""", cls.bodies, prelude=PARTS)

    def test_a_stale_reading_says_so_once_with_its_age_and_its_bound(self):
        s = self.out["stale"]
        read = _text(s["read"])
        self.assertIn("stale, 22m old, past its 20m bound", read)
        self.assertEqual(read.count("stale"), 1)
        home = _text(s["home"])
        self.assertIn("to do ›", home)                 # it keeps its numbers
        self.assertIn("stale, 22m old", home)
        self.assertIn("stale, 22m old", s["badge"]["title"])
        whole = _text(self.out["whole"]["home"])
        self.assertIn("to do ›", whole)                  # the control's numbers
        self.assertNotIn("stale", whole)                 # and no stale word

    def test_a_stale_reading_with_no_clock_names_no_age_and_no_bound(self):
        n = self.out["noclock"]
        read = _text(n["read"])
        self.assertIn("stale, age unknown", read)
        self.assertNotIn("bound", read)
        self.assertIn("stale, age unknown", _text(n["home"]))
        self.assertIn("to do ›", _text(n["home"]))

    def test_a_body_held_past_the_pages_bound_keeps_its_numbers_dated(self):
        held = _text(self.out["held"])
        self.assertIn("to do ›", held)
        self.assertIn("stale, 3m old", held)


class RankSetTest(unittest.TestCase):
    """THE RANK FILTER IS A SET (test_web_backlog_view's quick chips: "two
    ranks must OR"): the owner's "P0 and P1 first" view is both ranks, the
    menu checks both, and the line names both."""

    @classmethod
    def setUpClass(cls):
        cls.bodies = _bodies()
        cls.out = _run("""
const d = B.whole, cards = wkCards(d, NOW), v = wkParse("priority=P0,P1", "q");
const inst = Object.assign(wkInst("main"), {view: v}), shown = cards.filter(c => wkPass(c, v, null));
out.shown = shown.map(c => c.rank);
out.menu = wkMenusHTML(inst, cards);
out.line = wkLineHTML(inst, wkCount(shown));
""", cls.bodies)

    def test_both_ranks_are_shown_checked_and_named(self):
        self.assertEqual(sorted(set(self.out["shown"])), ["P0", "P1"])
        rank = re.search(r'data-m="prio".*?</details>', self.out["menu"]).group(0)
        self.assertEqual(rank.count('<span class="blchk">✓</span>'), 2)
        self.assertIn("rank: P0 or P1", _text(self.out["line"]))


class CureWordsTest(unittest.TestCase):
    """What the page says, cured: an unknown age is a word, an error names no
    endpoint, the leftovers say what each record is, the fold's badge is the
    badge the nav draws, the filter line does not stutter, the owner is
    "you", and the toggles say whether they are on."""

    @classmethod
    def setUpClass(cls):
        cls.bodies = _bodies()
        cls.out = _run("""
const d = B.whole, cards = wkCards(d, NOW), by = k => cards.find(c => c.key === k);
out.nullAge = wkWhys(Object.assign({}, by("task/4"), {stuck: {why: "stalled"}, since: null, age: null, sub: "wait"}), d, NOW);
out.held = [wkEvWords([NOW, "K", 1, false, false], by("task/4"), []), wkEvWords([NOW, "k", 2], by("task/4"), [])];
out.whyRead = wkReadLineHTML({unavailable: "/api/work -> timed out after 30s", counts: null}, 0);
out.whyBadge = wkBadge({unavailable: "/api/work -> timed out after 30s", counts: null}).title;
out.whySeams = wkSeamsHTML(by("task/4"), {key: "task/4", err: "/api/work?key=task/4&rev=r1 -> 410: that reading is gone; reload the page"}, false, NOW);
out.left = wkFoldLeftHTML(Object.assign({}, d, {records: [
  {id: "room:alpha:old-room", lane: "old-room", project: "alpha", state: "ROOM", whose: {kind: "seat", who: "erin"},
   settlement: {type: "record", why: "the room's branch is on trunk; only its lease is still held"}},
  {id: "r1", lane: "old-review", project: "alpha", state: "REVIEWED", age_s: 3600,
   settlement: {type: "record", why: "a later round carried it"}}]}), NOW);
const many = JSON.parse(JSON.stringify(d));
many.counts.stuck = 173;
many.scope.complete = true;
out.elseMany = wkFoldElseHTML(many, 0);
out.pipeLine = wkLineHTML(Object.assign(wkInst("main"), {view: Object.assign(wkDefault(), WK_ENTRY.pipeline)}), wkCount(cards), {}, d);
out.chips = wkChipsHTML(Object.assign(wkDefault(), {stuck: true}), wkCount(cards), null);
out.brackets = wkBracketsHTML(Object.assign(wkDefault(), {stage: "pipeline"}), wkCount(cards), null);
out.drawer = wkDrawerHTML(wkInst("main"), d, by("task/4"), NOW);
ONYOU_HOME = 'owner asks <span class="hstale">stale</span>';
out.onyouStale = wkOnYouHTML();
ONYOU_HOME = 'decisions <span class="hstale">not read</span> · owner asks <span class="hstale">not read</span>';
out.onyouUnread = wkOnYouHTML();
ONYOU_HOME = '<b>2 owner asks</b>';
out.onyouTwo = wkOnYouHTML();
""", cls.bodies, prelude="""
let ONYOU_HOME = "";
const ONYOU = {};
function onYouRead() { return {quiet: false, home: ONYOU_HOME}; }
""")

    def test_an_unknown_age_is_a_word_never_null(self):
        text = " ".join(w[1] for w in self.out["nullAge"])
        self.assertIn("no move for an unknown time", text)
        self.assertNotIn("null", text)

    def test_an_error_names_no_endpoint(self):
        for k in ("whyRead", "whyBadge", "whySeams"):
            with self.subTest(surface=k):
                self.assertNotIn("/api/", self.out[k])
        self.assertIn("timed out after 30s", self.out["whyRead"])
        self.assertIn("timed out after 30s", self.out["whyBadge"])
        self.assertIn("410: that reading is gone", self.out["whySeams"])

    def test_the_leftovers_say_what_each_record_is(self):
        t = _text(self.out["left"])
        self.assertIn("<b>1</b> review record left open", self.out["left"])
        self.assertIn("<b>1</b> work room whose work landed", self.out["left"])
        self.assertIn("@erin still holds the room", t)
        for word in ("lease", "branch", "trunk", "room:"):
            with self.subTest(word=word):
                self.assertNotIn(word, t)

    def test_the_folds_badge_is_the_badge_the_nav_draws(self):
        self.assertIn(">99+<", self.out["elseMany"])
        t = _text(self.out["elseMany"])
        self.assertIn("173 pieces of work are stuck", t)
        self.assertIn("With no filter on, it is the stuck chip", t)

    def test_the_filter_line_names_a_stage_once(self):
        t = _text(self.out["pipeLine"])
        self.assertIn("only the pipeline ×", t)
        self.assertNotIn("only only", t)

    def test_the_owner_is_you_and_holds_are_plain(self):
        self.assertEqual(self.out["held"][0][1], "Held: reviewed, waiting")
        self.assertEqual(self.out["held"][1][1], "Round 2: its seat let go of it")
        self.assertIn("your asks may be out of date", _text(self.out["onyouStale"]))
        self.assertIn("your asks are not read yet", _text(self.out["onyouUnread"]))
        self.assertIn("2 asks of yours", _text(self.out["onyouTwo"]))
        for k in ("onyouStale", "onyouUnread", "onyouTwo"):
            self.assertNotIn("owner ask", self.out[k])

    def test_the_toggles_say_whether_they_are_on(self):
        for k in ("chips", "brackets"):
            with self.subTest(part=k):
                self.assertIn('aria-pressed="true"', self.out[k])
                self.assertIn('aria-pressed="false"', self.out[k])
        self.assertIn('aria-label="a note for the fleet on this task"', self.out["drawer"])


class TheOneReadTest(unittest.TestCase):
    """`wkLoad`, EXECUTED over a stand-in read (test_web_burndown_runtime
    test_A_STALE_IN_FLIGHT_READ_NEVER_OVERWRITES_A_NEWER_ONE and test_web_lr
    AHungReadIsAnAnswerTest, carried): the badge boots as "?", a slow older
    read never overwrites a newer one, and a failed read replaces the last
    good one with UNKNOWN, never leaving numbers standing."""

    @classmethod
    def setUpClass(cls):
        cls.bodies = _bodies()
        cls.out = _run("""
wkPaintAll();
out.boot = NAV[NAV.length - 1];
const older = wkLoad(), newer = wkLoad();
PENDING[1].res(B.whole);
await newer;
PENDING[0].res(B.down);
await older;
out.kept = {counts: !!WORK.d.counts, unavailable: WORK.d.unavailable || null, nav: NAV[NAV.length - 1][1]};
PENDING = [];
const failed = wkLoad();
PENDING[0].rej(new Error("/api/work -> timed out after 30s"));
await failed;
out.failed = {counts: WORK.d.counts, unavailable: WORK.d.unavailable, nav: NAV[NAV.length - 1][1]};
out.asked = ASKED;
""", cls.bodies, run_async=True, prelude="""
let WORK = null, WORK_GEN = 0, PENDING = [];
const WK_FETCH_MS = 30000, NAV = [], ASKED = [];
const WK = {main: {}, proj: {}};
function navBadge(v, n, mention, title) { NAV.push([v, n, title]); }
function wkPaint() {}
function j(url, ms) { ASKED.push([url, ms]); return new Promise((res, rej) => PENDING.push({res: res, rej: rej})); }
""")

    def test_the_badge_boots_as_unknown(self):  # noqa: VACUOUS_ASSERTION — both arms are positive: the boot badge EQUALS ["flow", "?"] and the boot paint's index is asserted before the first read's
        self.assertEqual(self.out["boot"][:2], ["flow", "?"])
        work = _part("scripts/52-work.js.part")
        self.assertLess(work.index("\nsetTimeout(wkPaintAll, 0);"),
                        work.index("\nsetTimeout(wkLoad, 0);"))

    def test_a_slow_older_read_never_overwrites_a_newer_one(self):
        self.assertEqual(self.out["kept"], {"counts": True, "unavailable": None,
                                            "nav": self.bodies["whole"]["counts"]["stuck"]})

    def test_a_failed_read_is_unknown_never_the_last_numbers(self):
        f = self.out["failed"]
        self.assertIsNone(f["counts"])
        self.assertIn("timed out", f["unavailable"])
        self.assertEqual(f["nav"], "?")
        self.assertEqual({ms for _u, ms in self.out["asked"]}, {30000})


class DomPinsTest(unittest.TestCase):
    """What needs a DOM, pinned in the shipped source (there is no DOM under
    node in this tree; the behaviour is the recorded browser run): Esc closes
    only the drawer on screen and never the project row under a drawer; a
    drawer the reader is in survives a repaint that draws none; a phone's
    drawer keeps its scroll; the keyboard keeps its place; a link to one card
    lands on its drawer; a project tab opened on a card reads its crossings;
    and the card face's first line never clips its came-back count."""

    def setUp(self):
        self.src = _src()
        self.paint = _extract_fn(self.src, "wkPaint")

    def test_esc_closes_only_the_drawer_on_screen(self):
        at = self.src.index('if (ev.key === "Escape" && BOARD_OPEN.size')
        core = self.src[at:self.src.index("BOARD_OPEN.clear();", at)]
        self.assertIn('$("#view-work").classList.contains("on")', core)
        self.assertIn("WK.proj.view.card && wkRoot(WK.proj)", core)
        work = _part("scripts/52-work.js.part")
        at = work.index('if (ev.key === "Escape") {')
        self.assertIn("r.offsetParent !== null", work[at:work.index("return;", at)])

    def test_a_drawer_the_reader_is_in_survives_a_repaint_that_draws_none(self):
        self.assertIn("slot.prepend(keep)", self.paint)
        self.assertLess(self.paint.index("fresh.replaceWith(keep)"),
                        self.paint.index("slot.prepend(keep)"))

    def test_a_phone_drawer_keeps_its_scroll(self):
        self.assertLess(self.paint.index("was.scrollTop"),
                        self.paint.index("WK_SLOTS.forEach("))
        self.assertIn("fr.scrollTop = top", self.paint)

    def test_the_keyboard_keeps_its_place_across_a_repaint(self):
        self.assertLess(self.paint.index("document.activeElement, fk"),
                        self.paint.index("WK_SLOTS.forEach("))
        self.assertIn("el.focus({preventScroll: true})", self.paint)

    def test_a_link_to_one_card_lands_on_its_drawer(self):
        self.assertIn("WK.main.landCard = !!WK.main.view.card",
                      _extract_fn(self.src, "wkRoute"))
        self.assertIn("goTarget(showView.cur, dr)", self.paint)

    def test_a_project_tab_opened_on_a_card_reads_its_crossings(self):
        self.assertIn("setTimeout(() => wkPaint(WK.proj), 0)",
                      _extract_fn(self.src, "projTab"))

    def test_the_card_faces_first_line_never_clips_its_count(self):
        css = _part("styles/74-work.css.part")
        self.assertIn("flex-wrap:wrap", css[css.index(".wkline1{"):].split("}")[0])
        self.assertIn(".wkline1>.wkback{flex:0 0 auto;overflow:visible", css)
        self.assertIn(".wk .dslink{color:var(--accent)", css)


if __name__ == "__main__":
    unittest.main()
