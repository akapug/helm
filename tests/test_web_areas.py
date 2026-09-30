#!/usr/bin/env python3
"""Four top-level pages — Work · Chat · Fleet · History — and every old
bookmark still lands on the right one.

The owner's words: "are board and work and the 'work landing' section at the
top of history (which is really the kanban board) really all separate things?
what if it was just work and history and history focuses on signing stuff",
and "since chat already needs no sub menu, maybe board needs no submenu either
and history just moves to the 4th item in the top level menu?".

So the nav is four areas. WORK merged the burn board, the work tab and the
scheduler, with the land pipeline moved onto it from History; since task/3445
(the IA review's Option 1 — the owner: "why would the work page have all
projects listed twice or more listing different things?") it has a section row
of its own, and its sections speak one hash, #work/<section>. Since task/3643
the row is projects · work · pipeline · backlog: projects is its own screen,
and work is the ONE Work page, with pipeline and backlog as its two
pre-filtered entries, drawn on the same element. CHAT is unchanged. FLEET keeps its
sections (credit, seats, sessions, configs, boxes). Work and Fleet have
section rows. HISTORY is the signing ledger
alone. Three older rounds live on in `canonView`: `storage` -> `boxes`, the
overview's `helm` -> `board`, and now `board`, `scheduler` -> `work` and
`ledger` -> `history`, so no bookmark is stranded.

That claim is mostly NEGATIVE, which is why these arms exist in two registers:

* the TABLE, asserted against the markup and against the JS map separately, so
  the two copies of the grouping cannot drift apart silently;
* the ROUTER, RUN.  The boot block is spliced and executed once per hash —
  every live view and every retired one — against a DOM built out of the
  assembled nav markup itself.  A source check could not bind this:
  `if (false && VIEWS.includes(v)) showView(v)` keeps every literal a grep
  looks for and routes nothing.
"""
import json
import os
import re
import shutil
import subprocess
import unittest

from helm import web_ui_loader
from tests.test_web_chat_client_runtime import _extract_fn
from tests.test_web_nav_reveal_runtime import _boot_block

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(HERE, "navareas_runtime_harness.js")
# THE TOTALS BAND WAS SUPERSEDED by the owner's delegation of the IA redesign
# (premise owner-ia-guidelines-no-duplicate-ux-mirrors-ax-overview-home:
# "a real homepage"), task/3445. Its byte digest pinned the earlier ruling that
# the totals stay where they are. They are still on the FIRST PAGE, which is
# Home: each total is a Home tile, with the command and read-time lines left
# to the detail pages. The pipeline and backlog tiles are ONE work tile since
# task/3643, the Work page's own stage row. The band's cells went to those
# pages: the pipeline row atop the Work page, the seats row atop Fleet ›
# seats, the chain check atop the history page's local record.
# The band's last digest, for the record, was
# 64251f66149969b4376706c86ea6f82bc537d35907e99b80480bc234872eb198
# (seven rows).
HOME_TOTALS = ('id="hwork"', 'id="hseats"', 'id="hrecord"')

# THE RULING, AS A TABLE: view id -> (area, section label). A view that is its
# area's only page has no section row, so its label is None. The KEY is the
# view id, because that is what a hash, a bookmark and localStorage carry.
RULED = {
    "work": ("work", "projects"),
    "flow": ("work", "work"),
    "pipeline": ("work", "pipeline"),
    "backlog": ("work", "backlog"),
    "chat": ("chat", None),
    "quota": ("fleet", "credit"),
    "models": ("fleet", "models"),
    "roster": ("fleet", "seats"),
    "sessions": ("fleet", "sessions"),
    "configs": ("fleet", "configs"),
    "boxes": ("fleet", "boxes"),
    "history": ("history", None),
}
AREA_LABELS = {"work": "Work", "chat": "Chat", "fleet": "Fleet",
               "history": "History"}
AREA_ORDER = ["work", "chat", "fleet", "history"]
# HOME (task/3445 L3) is the page the console opens on. It has no area button:
# the brand is its way back, so it is in the router's lists and not the nav's.
HOME = "home"
# THE PAGE EACH VIEW IS DRAWN ON (`pageOf`, task/3643): pipeline and backlog
# are entries into the one Work page, so they show its element
PAGE_OF = {"pipeline": "flow", "backlog": "flow"}
# EVERY RETIRED VIEW ID and the page it lands on now. `storage` and `helm` are
# the two older renames; the other three are this merge.
MOVED = {"storage": "boxes", "helm": "work", "board": "work",
         "scheduler": "pipeline", "ledger": "history"}
# THE HASH EACH VIEW WRITES: Work's sections speak #work/<section>, every
# other view its own id
HASH = {"work": "#work/projects", "flow": "#work/work",
        "pipeline": "#work/pipeline", "backlog": "#work/backlog"}
# WORK'S OWN HASHES, and the page each one opens
WORK_HASHES = {"work/projects": "work", "work/work": "flow",
               "work/pipeline": "pipeline", "work/backlog": "backlog",
               "work/projects?open=alpha&tab=lanes": "work"}


def _nav(src):
    """The <nav> element's source. Every arm below reads the nav, and a
    `data-v=` anywhere else on the page (a note link, a chip) must not be able
    to answer a question about the nav."""
    i = src.index("<nav id=\"nav\">")
    return src[i:src.index("</nav>", i)]


def _areas(src):
    """[{key, label, group, sections: [{view, label}]}] read off the real nav.

    Area order is the order the buttons appear in; a section group is matched to
    its area by data-a, never by position, so reordering the two rows relative to
    each other cannot silently re-parent a section. `group` says whether the
    markup gave the area a section row at all."""
    nav = _nav(src)
    groups = {m.group(1): m.group(2) for m in
              re.finditer(r'<div class="navsec(?: on)?" data-a="([a-z]+)">'
                          r'(.*?)</div>', nav, re.S)}
    out = []
    for m in re.finditer(r'<button class="navarea(?: on)?" data-a="([a-z]+)">'
                         r'([^<]+)</button>', nav):
        key, label = m.group(1), m.group(2)
        sections = [{"view": v, "label": lab} for v, lab in
                    re.findall(r'<button class="navtab(?: on)?" data-v="([a-z]+)">'
                               r'([^<]+)</button>', groups.get(key, ""))]
        out.append({"key": key, "label": label, "group": key in groups,
                    "sections": sections})
    return out


def _table(src):
    """{view: (area, label)} from the markup: a grouped area's sections, and
    an ungrouped area as the one page named for it."""
    table = {}
    for area in _areas(src):
        if not area["group"]:
            table.setdefault(area["key"], (area["key"], None))
        for sec in area["sections"]:
            if sec["view"] in table:
                raise AssertionError("%s is a section of two areas"
                                     % sec["view"])
            table[sec["view"]] = (area["key"], sec["label"])
    return table


def _area_of_map(src):
    """The AREA_OF literal, parsed out of the shipped JavaScript."""
    i = src.index("const AREA_OF = {")
    body = src[src.index("{", i) + 1:src.index("};", i)]
    return dict(re.findall(r'([a-z]+):\s*"([a-z]+)"', body))


class TestAreaTable(unittest.TestCase):
    """Node-free: the grouping, read off the shipped page."""

    def setUp(self):
        self.src = web_ui_loader.read_text()

    def test_the_four_areas_are_work_chat_fleet_and_history(self):  # noqa: VACUOUS_ASSERTION — every assertion here is an equality against a non-empty literal, so an empty parse fails each one rather than satisfying it
        """In the owner's order, with History fourth."""
        areas = _areas(self.src)
        self.assertEqual([a["key"] for a in areas], AREA_ORDER)
        self.assertEqual({a["key"]: a["label"] for a in areas}, AREA_LABELS)

    def test_work_and_fleet_have_section_rows(self):
        """Chat and History are single pages with no submenu; Work has four
        sections (task/3445, task/3643) and Fleet keeps its six, quota
        relabelled credit, in this order."""
        groups = re.findall(r'<div class="navsec(?: on)?" data-a="([a-z]+)"',
                            _nav(self.src))
        self.assertEqual(groups, ["work", "fleet"])
        work = [a for a in _areas(self.src) if a["key"] == "work"][0]
        self.assertEqual([(s["view"], s["label"]) for s in work["sections"]],
                         [("work", "projects"), ("flow", "work"),
                          ("pipeline", "pipeline"), ("backlog", "backlog")])
        fleet = [a for a in _areas(self.src) if a["key"] == "fleet"][0]
        self.assertEqual([(s["view"], s["label"]) for s in fleet["sections"]],
                         [("quota", "credit"), ("models", "models"), ("roster", "seats"),
                          ("sessions", "sessions"), ("configs", "configs"),
                          ("boxes", "boxes")])

    def test_every_view_is_under_exactly_one_area(self):
        """THE RULING AS A TABLE, and each fleet section appears in the nav
        exactly once (two would give the router two homes for one hash)."""
        self.assertEqual(_table(self.src), RULED)
        nav = _nav(self.src)
        for view, (_area, label) in RULED.items():
            self.assertEqual(nav.count('data-v="%s"' % view),
                             0 if label is None else 1, view)

    def test_the_script_map_agrees_with_the_nav_markup(self):  # noqa: VACUOUS_ASSERTION — the two len() assertions above the equality are the unconditional positive controls: two parsers that both return nothing agree perfectly, so sizing both sides is what refuses an empty page
        """TWO COPIES OF ONE GROUPING: the markup groups the buttons, AREA_OF
        groups the views for showView and the badge roll-up. Drift between them
        opens an area whose page is hidden, which reads as a blank screen."""
        markup = {view: area for view, (area, _l) in _table(self.src).items()}
        self.assertEqual(len(markup), len(RULED))
        self.assertEqual(len(_area_of_map(self.src)), len(RULED) + 1)
        self.assertEqual(_area_of_map(self.src), dict(markup, home=HOME))
        self.assertIn('id="brand" href="#home"', _nav(self.src))

    def test_VIEWS_carries_the_live_ids_and_no_retired_one(self):  # noqa: VACUOUS_ASSERTION — the sorted-list equality against RULED is the unconditional positive control on the same VIEWS line the absence loop reads
        """The router's own list is the ruling's ids. A retired id in it would
        route to a panel that no longer exists; canonView owns their
        migration instead."""
        line = self.src[self.src.index("const VIEWS"):].split("\n")[0]
        self.assertEqual(sorted(re.findall(r'"([a-z]+)"', line)),
                         sorted(list(RULED) + [HOME]))
        for gone in MOVED:
            self.assertNotIn('"%s"' % gone, line, gone)

    def test_every_view_has_its_panel_and_the_merged_panels_are_gone(self):
        for view in RULED:
            self.assertIn('id="view-%s"' % PAGE_OF.get(view, view), self.src,
                          "the %s panel is gone" % view)
        # the pipeline and backlog have no panel of their own: they are the
        # Work page's entries (task/3643)
        for gone in ("board", "scheduler", "ledger", "pipeline", "backlog"):
            self.assertNotIn('id="view-%s"' % gone, self.src, gone)
        self.assertEqual(self.src.count('id="view-work"'), 1)

    def test_work_is_two_screens_each_as_ruled(self):  # noqa: VACUOUS_ASSERTION — str.index() RAISES on any missing marker, so every marker is asserted present before an order or absence is compared
        """PROJECTS: on you first, then the lanes line, the filter bar and one
        line per project — and nothing under them (task/3445 L3: the system
        band became Home's tiles). WORK (task/3643): the one Work page, then
        its three folds; the pipeline's and the backlog's screens, the land
        board, who waits on whom and the owed lists are gone."""
        def view(v):
            i = re.search(r'<div class="view(?: on)?" id="view-%s">' % v,
                          self.src).start()
            return self.src[i:self.src.index('<div class="view"', i + 10)]
        work, flow = view("work"), view("flow")
        order = [work.index(m) for m in (
            'id="onyou"', 'id="odq"', 'id="bhead"',
            'id="controls"', 'id="lightf"', 'id="brows"')]
        self.assertEqual(order, sorted(order))
        order = [flow.index(m) for m in (
            'id="wkmain"', 'id="wkseams"', 'id="wkleft"', 'id="wkelse"')]
        self.assertEqual(order, sorted(order))
        for gone in ('id="fleetkb"', 'id="walldetail"', 'id="lrsec"',
                     'id="pipesum"', 'id="dpipe"', 'id="downedby"',
                     'id="dlands"', 'id="tierpipeline"', 'id="pipeproj"',
                     'id="pipekb"', 'id="schedfold"', 'id="schedulersec"',
                     'id="obd"', 'id="oub"', 'id="ocb"', 'id="otq"'):
            self.assertNotIn(gone, self.src)
        self.assertNotIn('id="wkmain"', work)

    def test_history_is_the_signing_ledger_alone(self):  # noqa: VACUOUS_ASSERTION — each ledger marker is asserted PRESENT on the same page slice the absence loop reads, and str.index() raises when the history view is missing
        page = self.src[self.src.index('id="view-history"'):]
        ends = [i for i in (page.find('<div class="view"', 10),
                            page.find("<script", 10)) if i > 0]
        page = page[:min(ends)]
        for marker in ('id="tiersigned"', 'id="tierlocal"', 'id="ledgerstrip"'):
            self.assertIn(marker, page, marker)
        for marker in ('id="lrsec"', 'id="wkmain"', 'id="schedulersec"'):
            self.assertNotIn(marker, page, marker)

    def test_the_totals_stay_on_the_first_page_which_is_home(self):  # noqa: VACUOUS_ASSERTION — str.index() RAISES on any missing total, so each is asserted present before its placement is compared
        """Owner ruling: he reads only the totals, and they stay on the first
        page. The first page is Home (task/3445 L3), each total its own tile;
        the band that held them is gone, and nothing carries the in-flight row
        task/2355 took off."""
        src = self.src
        home = src[src.index('id="view-home"'):]
        home = home[:home.index('<div class="view')]
        for marker in HOME_TOTALS:
            self.assertIn(marker, home, "the %s total left Home" % marker)
        for gone in ('id="dash"', 'id="dinflight"'):
            self.assertNotIn(gone, src, gone)
        # ONE work tile in place of the pipeline and backlog tiles, which
        # counted the same work two ways (task/3643)
        for gone in ('id="hpipe"', 'id="hbacklog"'):
            self.assertNotIn(gone, src, gone)


class TestAreaRouterRuntime(unittest.TestCase):
    """The real boot, the real showView, the real area handler — executed."""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        src = web_ui_loader.read_text()
        with open(HARNESS, encoding="utf-8") as handle:
            template = handle.read()
        fn = "\n\n".join(_extract_fn(src, n) for n in
                         ("revealActiveTab", "navBadge", "navBadgeText", "areaBadge",
                          "firstSection", "canonView", "viewHash", "hashView",
                          "pageOf", "landY", "viewTarget", "goTarget",
                          "navSelect", "showView"))
        script = template.replace("/*__INJECT__*/", fn)
        script = script.replace("/*__BOOT__*/",
                                "function __runBoot() " + _boot_block(src))
        script = script.replace("/*__WIRE__*/", _area_wiring(src))
        areas = _areas(src)
        views = re.findall(r'"([a-z]+)"',
                           src[src.index("const VIEWS"):].split("\n")[0])
        dom = {
            "areas": areas,
            "views": views,
            "area_of": _area_of_map(src),
            # the page's own panels, read off it
            "panels": re.findall(r'<div class="view(?: on)?" id="(view-[a-z]+)"',
                                 src),
            # every live view, then every retired id canonView migrates
            "hashes": views + sorted(MOVED) + sorted(WORK_HASHES),
            "click_sequence": [{"area": "fleet"}, {"view": "sessions"},
                               {"area": "work"}, {"area": "fleet"},
                               {"area": "history"}],
        }
        env = dict(os.environ, HELM_NAV_DOM=json.dumps(dom))
        proc = subprocess.run([cls.node, "-e", script], env=env,
                              capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise AssertionError("harness failed: " + (proc.stderr or "")[:800])
        cls.out = json.loads(proc.stdout.strip().splitlines()[-1])
        cls.dom = dom

    def test_the_harness_built_the_real_nav(self):
        """THE INPUT CONTROL. Every arm below reads a snapshot of a DOM that was
        built from the parsed page; if the parse returned nothing the snapshots
        are all empty and every absence arm passes vacuously."""
        self.assertEqual(self.out["built"], {"tabs": 10, "panels": 11,
                                            "areas": 4, "sections": 2})
        self.assertEqual(len(self.out["routes"]),
                         len(RULED) + 1 + len(MOVED) + len(WORK_HASHES))

    def test_every_live_hash_renders_its_own_page(self):  # noqa: VACUOUS_ASSERTION — the sorted(routes) equality ahead of the loop is the unconditional positive control on the same observable, so a harness run that measured no routes fails before the loop it would otherwise skip
        """Exactly one panel and one area are lit for each; a fleet page also
        lights its one tab and the section row, and a single page lights no tab
        and hides the empty section row."""
        self.assertEqual(sorted(self.out["routes"]),
                         sorted(list(RULED) + [HOME] + list(MOVED) + list(WORK_HASHES)))
        # HOME: its panel, no area button lit and no section row
        got = self.out["routes"][HOME]
        self.assertEqual((got["panel"], got["panels_on"], got["areas_on"],
                          got["solo"], got["hash"]),
                         ("view-home", 1, 0, True, "#home"))
        for view, (area, label) in RULED.items():
            got = self.out["routes"][view]
            self.assertEqual(got["panel"], "view-" + PAGE_OF.get(view, view),
                             "#%s rendered %s" % (view, got["panel"]))
            self.assertEqual(got["area"], area, "#%s opened %s" % (view, got["area"]))
            self.assertEqual([got["panels_on"], got["areas_on"]], [1, 1])
            if label is None:
                self.assertEqual([got["tabs_on"], got["sections_on"]], [0, 0])
                self.assertIs(got["solo"], True, view)
            else:
                self.assertEqual(got["tab"], view)
                self.assertEqual(got["section_open"], area)
                self.assertEqual([got["tabs_on"], got["sections_on"]], [1, 1])
                self.assertIs(got["solo"], False, view)
            self.assertEqual(got["hash"], HASH.get(view, "#" + view))
            # "/" always opens Home, so no page is saved for a boot to read
            self.assertIsNone(got["saved"], view)

    def test_works_section_hashes_open_their_section(self):
        """#work/projects and #work/work each open their screen, and
        #work/pipeline and #work/backlog open the Work page with their own
        tab lit; a section's state rides after a "?"."""
        self.assertEqual(self.out["routes"]["work/pipeline"]["panel"],
                         "view-flow")              # the unconditional control
        for h, view in WORK_HASHES.items():
            got = self.out["routes"][h]
            self.assertEqual((got["panel"], got["tab"]),
                             ("view-" + PAGE_OF.get(view, view), view), h)
            self.assertEqual(got["panels_on"], 1, h)

    def test_every_retired_hash_lands_on_the_page_that_took_it_over(self):  # noqa: VACUOUS_ASSERTION — the scheduler route's panel is asserted equal to view-flow unconditionally before the loop, and every loop arm is an equality
        """#board, #helm and #scheduler open Work, #ledger opens History, and
        #storage still opens boxes. The page rewrites the hash to the live
        id, so the retired one is not written back."""
        self.assertEqual(self.out["routes"]["scheduler"]["panel"],
                         "view-flow")              # the unconditional control
        for old, new in MOVED.items():
            got = self.out["routes"][old]
            self.assertEqual(got["panel"], "view-" + PAGE_OF.get(new, new), old)
            self.assertEqual(got["area"], RULED[new][0], old)
            self.assertEqual((got["hash"], got["saved"]),
                             (HASH.get(new, "#" + new), None), old)
            self.assertEqual(got["panels_on"], 1, old)

    def test_an_old_scheduler_hash_opens_who_has_what(self):
        """"Who waits on whom" became the pipeline's List grouped by who has
        it (task/3643): a #scheduler bookmark routes the pipeline entry with
        that lens and grouping; the other retired hashes route no state."""
        self.assertEqual(self.out["routes"]["scheduler"]["routed"],
                         [["pipeline", "lens=list&group=move"]])
        for old in ("board", "helm", "ledger", "work"):
            self.assertEqual(self.out["routes"][old]["routed"],
                             [[MOVED.get(old, old), ""]], old)
            self.assertEqual(self.out["routes"][old]["scrolled"], [], old)

    def test_a_hash_no_view_answers_to_opens_home(self):
        """MUST-MISS on the same driver: nothing is pre-selected, so the page a
        hash opens is the boot's doing. "/" and a hash no view answers to open
        HOME (task/3445 L3); a hash a view answers to opens that view."""
        self.assertEqual(self.out["routes"]["work"]["panel"], "view-work")
        self.assertEqual(self.out["bogus"]["panels_on"], 1)
        self.assertEqual(self.out["bogus"]["panel"], "view-home")
        self.assertEqual(self.out["bogus"]["areas_on"], 0)

    def test_an_area_opens_its_first_section_then_the_one_you_left(self):
        """Clicking Fleet the first time opens credit; after reading sessions,
        coming back to Fleet returns to sessions. Work and History each open
        their one page. The click runs the REAL handler."""
        steps = self.out["click_seq"]
        self.assertEqual([s["step"] for s in steps],
                         ["fleet", "sessions", "work", "fleet", "history"])
        self.assertEqual([s["panel"] for s in steps],
                         ["view-quota", "view-sessions", "view-work",
                          "view-sessions", "view-history"])
        self.assertEqual([s["solo"] for s in steps],
                         [False, False, False, False, True])
        self.assertNotEqual(steps[0]["panel"], steps[3]["panel"])

    def test_coming_back_to_work_refreshes_its_queues_and_the_boot_does_not(self):
        """The page opens on Work and its queue polls fire at load; a second
        fetch from the boot queued behind the page's long reads and drew the
        decision queue UNKNOWN. So the boot reads nothing extra, and a return
        to Work from another page does."""
        self.assertEqual(self.out["routes"]["work"]["queue_reads"], 0)
        steps = self.out["click_seq"]
        self.assertEqual([s["queue_reads"] for s in steps], [0, 0, 1, 1, 1])

    def test_a_page_change_lands_at_the_new_pages_top(self):
        """The walk opened Work › backlog at scrollY 702-811, past its own
        header: a page change kept the old page's scroll. Every page change
        lands at the top (the page starts right under the sticky nav), a
        boot included; showing the page already on screen scrolls nothing."""
        steps = self.out["click_seq"]
        self.assertEqual([len(s["tops"]) for s in steps], [2, 3, 4, 5, 6])
        self.assertEqual(set(map(tuple, steps[-1]["tops"])), {(0, 0)})
        for h in ("work/backlog", "quota", "history"):
            self.assertEqual(self.out["routes"][h]["tops"], [[0, 0]], h)
        self.assertEqual(self.out["same_view_tops"], 0)

    def test_a_single_page_areas_badge_is_on_its_area_button(self):
        """Chat has no section row, so its unread count lives on the Chat
        button — mention and all — and the land pipeline's alarm, now on
        Work, lives on the Work button."""
        got = self.out["badge_solo"]
        self.assertEqual(got["chat"]["text"], "3")
        self.assertTrue(got["chat"]["mention"])
        self.assertIn("three unread", got["chat"]["title"])
        self.assertEqual(got["work"]["text"], "?")

    def test_a_badge_in_a_closed_area_rolls_up_to_the_area_button(self):
        got = self.out["badge_closed_area"]
        self.assertFalse(got["section_visible"], "fixture: fleet was open")
        self.assertIsNotNone(got["section"], "the section badge itself is gone")
        self.assertEqual(got["area"]["text"], "2")

    def test_counts_in_one_area_add_and_an_unreadable_count_wins(self):
        self.assertEqual(self.out["badge_sums"]["area"]["text"], "7")
        # a floor keeps its bound on the tab and on the sum (task/3723)
        self.assertEqual(self.out["badge_floor"]["section"]["text"], "≥2")
        self.assertEqual(self.out["badge_floor"]["area"]["text"], "≥7")
        self.assertEqual(self.out["badge_unknown_wins"]["area"]["text"], "?")
        self.assertTrue(self.out["badge_unknown_wins"]["area"]["mention"])

    def test_a_cleared_area_drops_its_badge_and_leaves_the_others(self):  # noqa: VACUOUS_ASSERTION — chat_still reads "3" from the same harness run, an unconditional positive on the same observable (an area badge), so a run that wrote no badges at all fails there before the absence assertions
        got = self.out["badge_cleared"]
        self.assertIsNone(got["area"], "Fleet kept a badge at zero")
        self.assertEqual(got["chat_still"]["text"], "3")
        self.assertIsNone(self.out["badge_all_cleared"]["chat"])


def _area_wiring(src):
    """The verbatim `$$(".navarea").forEach(...)` statement.

    Reachability of a click handler is not a property of source text, so the
    handler is RUN — which means it has to be lifted, not retyped. It is a bare
    statement rather than a declaration, so paren-match it from its call."""
    needle = '$$(".navarea").forEach(b => b.onclick'
    i = src.index(needle)          # the WIRING, not showView's own .navarea line
    j = src.index(".forEach(", i) + len(".forEach")
    depth, k = 0, j
    while k < len(src):
        if src[k] == "(":
            depth += 1
        elif src[k] == ")":
            depth -= 1
            if depth == 0:
                return src[i:k + 1] + ";"
        k += 1
    raise AssertionError("the area wiring statement is not paren-matched")


if __name__ == "__main__":
    unittest.main()
