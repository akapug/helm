#!/usr/bin/env python3
"""Four top-level pages — Work · Chat · Fleet · History — and every old
bookmark still lands on the right one.

The owner's words: "are board and work and the 'work landing' section at the
top of history (which is really the kanban board) really all separate things?
what if it was just work and history and history focuses on signing stuff",
and "since chat already needs no sub menu, maybe board needs no submenu either
and history just moves to the 4th item in the top level menu?".

So the nav is four areas. WORK is one page: the burn board, the work tab and
the scheduler merged, with the land pipeline moved onto it from History. CHAT
is unchanged. FLEET keeps its sections (credit, seats, sessions, configs,
boxes) and is the only area with a section row. HISTORY is the signing ledger
alone. Three older rounds live on in `canonView`: `storage` -> `boxes`, the
overview's `helm` -> `board`, and now `board`, `scheduler` -> `work` and
`ledger` -> `history`, so no bookmark or saved view is stranded.

That claim is mostly NEGATIVE, which is why these arms exist in two registers:

* the TABLE, asserted against the markup and against the JS map separately, so
  the two copies of the grouping cannot drift apart silently;
* the ROUTER, RUN.  The boot block is spliced and executed once per hash —
  every live view and every retired one — against a DOM built out of the
  assembled nav markup itself.  A source check could not bind this:
  `if (false && VIEWS.includes(v)) showView(v)` keeps every literal a grep
  looks for and routes nothing.
"""
import hashlib
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
# the part the six totals and the owner row live in, read as BYTES
HOME_PART = os.path.join(os.path.dirname(HERE), "helm", "web_ui", "views",
                         "00-home.html.part")
# sha256 of the <section id="dash"> ... </section> band inside that part. It is
# a TRIPWIRE, not a schema: the owner ruled the totals stay exactly where they
# are, so a change to this band is a decision about HIS surface and updates this
# line in the same commit it is made.
#
# RE-PINNED ONCE, DELIBERATELY, BY THE RULING THAT FOLLOWED (task/2355): the
# in-flight row came OFF and an owed-by row went ON at the top, so the band is
# still six rows and two of them are different rows. The previous digest was
# c4eb445c60fbf4c0a0ea28423203686cf23061f2b7997e89cb0fec4f94810415 and it is
# recorded here rather than silently replaced, because the value of a tripwire
# is that its history says how many times the surface was touched.
#
# RE-PINNED A SECOND TIME, AND THIS ONE IS AN ADDITION (task/2622). Nothing
# came off: a seventh row went on, carrying the task BACKLOG's totals. The
# owner asked how he is supposed to cross-check the priority ordering of a
# 477-row list, and the band's pipeline row counts LAND LOOPS — a different
# population answering a different question — so the queue he was asking about
# had no number anywhere on this page. The digest before this row existed was
# 1266fc53ea0c4648c4600080412726335ce4b450b17b80c71dd6b2fc6fcd3c2f.
#
# RE-PINNED A THIRD TIME (task/2975, the burn board). The seven rows and their
# order are unchanged; two cells moved or were corrected. The dregg signing
# pulse left the `record` row for the board's headline, where the design put
# it beside the capacity line, and the `owner` row's title stopped claiming
# owner asks have no web endpoint — they have ridden /api/lr for weeks. The
# digest before this change was
# cb5636e669955a7b26cb675628e4aea2370d6461340589ff4a97197aaaa59dd2.
DASH_BAND_SHA256 = \
    "64251f66149969b4376706c86ea6f82bc537d35907e99b80480bc234872eb198"
DASH_BAND_ROWS = 7

# THE RULING, AS A TABLE: view id -> (area, section label). A view that is its
# area's only page has no section row, so its label is None. The KEY is the
# view id, because that is what a hash, a bookmark and localStorage carry.
RULED = {
    "work": ("work", None),
    "chat": ("chat", None),
    "quota": ("fleet", "credit"),
    "roster": ("fleet", "seats"),
    "sessions": ("fleet", "sessions"),
    "configs": ("fleet", "configs"),
    "boxes": ("fleet", "boxes"),
    "history": ("history", None),
}
AREA_LABELS = {"work": "Work", "chat": "Chat", "fleet": "Fleet",
               "history": "History"}
AREA_ORDER = ["work", "chat", "fleet", "history"]
# EVERY RETIRED VIEW ID and the page it lands on now. `storage` and `helm` are
# the two older renames; the other three are this merge.
MOVED = {"storage": "boxes", "helm": "work", "board": "work",
         "scheduler": "work", "ledger": "history"}
# the section an old hash is brought to on the page it now opens
MOVED_SECTION = {"scheduler": "schedfold"}


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

    def test_only_fleet_has_a_section_row(self):
        """Work, Chat and History are single pages with no submenu; Fleet keeps
        its five sections, quota relabelled credit, in this order."""
        groups = re.findall(r'<div class="navsec(?: on)?" data-a="([a-z]+)"',
                            _nav(self.src))
        self.assertEqual(groups, ["fleet"])
        fleet = [a for a in _areas(self.src) if a["key"] == "fleet"][0]
        self.assertEqual([(s["view"], s["label"]) for s in fleet["sections"]],
                         [("quota", "credit"), ("roster", "seats"),
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
        self.assertEqual(len(_area_of_map(self.src)), len(RULED))
        self.assertEqual(_area_of_map(self.src), markup)

    def test_VIEWS_carries_the_eight_live_ids_and_no_retired_one(self):
        """The router's own list is the ruling's ids. A retired id in it would
        route to a panel that no longer exists; canonView owns their
        migration instead."""
        line = self.src[self.src.index("const VIEWS"):].split("\n")[0]
        self.assertEqual(sorted(re.findall(r'"([a-z]+)"', line)),
                         sorted(RULED))
        for gone in MOVED:
            self.assertNotIn('"%s"' % gone, line, gone)

    def test_every_view_has_its_panel_and_the_merged_panels_are_gone(self):
        for view in RULED:
            self.assertIn('id="view-%s"' % view, self.src,
                          "the %s panel is gone" % view)
        for gone in ("board", "scheduler", "ledger"):
            self.assertNotIn('id="view-%s"' % gone, self.src, gone)
        self.assertEqual(self.src.count('id="view-work"'), 1)

    def test_the_work_page_reads_top_to_bottom_as_ruled(self):
        """On you first, then the lanes line and the project rows, then the
        fleet-wide kanban (the land pipeline) and the task backlog under them;
        the system band keeps its place on the same page."""
        page = self.src[self.src.index('id="view-work"'):
                        self.src.index('id="view-quota"')]
        order = [page.index(m) for m in (
            'id="onyou"', 'id="odq"', 'id="bhead"', 'id="dreggstrip"',
            'id="flagcard"', 'id="brows"', 'id="tierpipeline"', 'id="lrsec"',
            'id="schedfold"', 'id="schedulersec"', 'id="otq"', 'id="dash"')]
        self.assertEqual(order, sorted(order))

    def test_history_is_the_signing_ledger_alone(self):
        page = self.src[self.src.index('id="view-history"'):]
        ends = [i for i in (page.find('<div class="view"', 10),
                            page.find("<script", 10)) if i > 0]
        page = page[:min(ends)]
        for marker in ('id="tiersigned"', 'id="tierlocal"', 'id="ledgerstrip"'):
            self.assertIn(marker, page, marker)
        for marker in ('id="lrsec"', 'id="tierpipeline"', 'id="schedulersec"'):
            self.assertNotIn(marker, page, marker)

    def test_the_six_totals_and_the_owner_strip_stay_on_the_first_page(self):
        """Owner ruling: he reads only the totals, and they stay where they
        are. So the dashboard band — the pipeline partition that renders
        in-flight / moving / stalled / contrary / nonbillable and the owner
        row — stays inside the first page, which is now Work, ahead of every
        other view in the document.

        `id="dinflight"` is NOT in this sweep any more and `id="downedby"` is:
        the second ruling (task/2355) took the per-loop in-flight row off and
        put the owed-by roll-up at the top of the band."""
        src = self.src
        for marker in ('id="dash"', 'id="dpipe"', 'id="downedby"',
                       'id="downer"', 'id="dlands"'):
            self.assertIn(marker, src, "the totals band lost %s" % marker)
        self.assertNotIn('id="dinflight"', src,
                         "the in-flight row came off in task/2355; a page "
                         "still carrying it has an element no renderer writes")
        order = [src.index(m) for m in ('id="view-work"', 'id="dash"',
                                        'id="downedby"', 'id="dpipe"',
                                        'id="downer"', 'id="view-quota"')]
        self.assertEqual(order, sorted(order),
                         "the totals band left the Work page, or owed-by is "
                         "not at the top of it")
        self.assertIn("contrary > honored", src)
        for word in ("stalled", "unmeasurable", "moving"):
            self.assertIn(word, src)

    def test_the_totals_band_is_byte_for_byte_what_it_was(self):  # noqa: VACUOUS_ASSERTION — the drow count assertion is the unconditional positive control on the extracted band, and an empty extraction hashes to a different digest anyway
        """THE BYTE COMPARE, because the arm above is a marker sweep and a
        marker sweep survives a reworded label, a dropped title or a reordered
        row — every one of which changes what the owner reads while keeping
        every id it looks for. So hash the band itself, out of its own PART
        file: the merge moved sections around it and not a byte inside it."""
        with open(HOME_PART, "rb") as handle:
            part = handle.read().decode("utf-8")
        i = part.index('<section id="dash">')
        band = part[i:part.index("</section>", i) + len("</section>")]
        self.assertEqual(band.count('<div class="drow'), DASH_BAND_ROWS)
        self.assertEqual(hashlib.sha256(band.encode("utf-8")).hexdigest(),
                         DASH_BAND_SHA256,
                         "the totals band changed; the owner ruled it stays as "
                         "it is, so re-pin DASH_BAND_SHA256 only deliberately")


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
                         ("revealActiveTab", "navBadge", "areaBadge",
                          "firstSection", "canonView", "goOldSection",
                          "showView"))
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
            "panels": ["view-" + v for v in RULED],
            "anchors": sorted(set(MOVED_SECTION.values())),
            # every live view, then every retired id canonView migrates
            "hashes": views + sorted(MOVED),
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
        self.assertEqual(self.out["built"], {"tabs": 5, "panels": 8,
                                            "areas": 4, "sections": 1})
        self.assertEqual(len(self.out["routes"]), len(RULED) + len(MOVED))

    def test_every_live_hash_renders_its_own_page(self):  # noqa: VACUOUS_ASSERTION — the sorted(routes) equality ahead of the loop is the unconditional positive control on the same observable, so a harness run that measured no routes fails before the loop it would otherwise skip
        """Exactly one panel and one area are lit for each; a fleet page also
        lights its one tab and the section row, and a single page lights no tab
        and hides the empty section row."""
        self.assertEqual(sorted(self.out["routes"]),
                         sorted(list(RULED) + list(MOVED)))
        for view, (area, label) in RULED.items():
            got = self.out["routes"][view]
            self.assertEqual(got["panel"], "view-" + view,
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
            self.assertEqual(got["hash"], "#" + view)
            self.assertEqual(got["saved"], view)

    def test_every_retired_hash_lands_on_the_page_that_took_it_over(self):
        """#board, #helm and #scheduler open Work, #ledger opens History, and
        #storage still opens boxes. The page rewrites the hash and the saved
        view to the live id, so the retired one is not written back."""
        for old, new in MOVED.items():
            got = self.out["routes"][old]
            self.assertEqual(got["panel"], "view-" + new, old)
            self.assertEqual(got["area"], RULED[new][0], old)
            self.assertEqual((got["hash"], got["saved"]), ("#" + new, new), old)
            self.assertEqual(got["panels_on"], 1, old)

    def test_an_old_section_hash_is_brought_into_view(self):
        """A #scheduler bookmark lands on Work AT the scheduler, opened; the
        other retired hashes scroll nothing."""
        self.assertEqual(self.out["routes"]["scheduler"]["scrolled"],
                         ["schedfold"])
        self.assertIs(self.out["routes"]["scheduler"]["opened"], True)
        for old in ("board", "helm", "ledger", "work"):
            self.assertEqual(self.out["routes"][old]["scrolled"], [], old)

    def test_a_hash_no_view_answers_to_selects_nothing(self):
        """MUST-MISS on the same driver: nothing is pre-selected and no default
        is applied, so a snapshot wired to a constant, or a boot that ignored
        the hash, fails here while passing every arm above."""
        self.assertEqual(self.out["routes"]["work"]["panels_on"], 1)
        self.assertEqual(self.out["bogus"]["panels_on"], 0)
        self.assertIsNone(self.out["bogus"]["panel"])
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
                         [False, False, True, False, True])
        self.assertNotEqual(steps[0]["panel"], steps[3]["panel"])

    def test_coming_back_to_work_refreshes_its_queues_and_the_boot_does_not(self):
        """The page opens on Work and its queue polls fire at load; a second
        fetch from the boot queued behind the page's long reads and drew the
        decision queue UNKNOWN. So the boot reads nothing extra, and a return
        to Work from another page does."""
        self.assertEqual(self.out["routes"]["work"]["queue_reads"], 0)
        steps = self.out["click_seq"]
        self.assertEqual([s["queue_reads"] for s in steps], [0, 0, 1, 1, 1])

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
