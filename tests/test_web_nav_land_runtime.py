#!/usr/bin/env python3
"""Where a navigation lands, run for real (task/3475).

A measured owner-walk of the web console found four navigation defects:

1. a section change kept the last page's scroll, and a deep link such as
   #work/projects?open=helm&tab=team landed at scrollY 0 while the open
   project sat 1,720 px down;
2. an in-page link ("who waits on whom") put its heading UNDER the sticky
   navigation;
3. an open project's name and tab strip scrolled away over a Team tab 4,443 px
   tall at 1440 px;
4. the Team tab listed budget rows for families the project does not use and
   every absent seat.

The landing is ARITHMETIC over the navigation's measured height, so the real
functions are lifted out of the assembled page and run under node against a
world whose rects move with the window's scroll (tests/navland_runtime_harness.js).
The sticky head and the scroll margins are CSS a node world cannot resolve, so
their contracts are held over the shipped stylesheet here, and the open row's
markup is held in tests/test_web_board.py over the real renderer.

Requires node for the runtime class; SKIPPED (never failed) where node is
absent, like every other client-runtime harness in this suite."""
import json
import os
import re
import shutil
import subprocess
import unittest

from helm import web_ui_loader
from tests.test_web_chat_client_runtime import _extract_fn

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(HERE, "navland_runtime_harness.js")

FNS = ("publishNavHeight", "landY", "viewTarget", "goTarget", "boardLandAgain",
       "teamSplit", "teamFold")


class NavLandSourceTest(unittest.TestCase):
    """Node-free contracts over the shipped page."""

    @classmethod
    def setUpClass(cls):
        cls.src = web_ui_loader.read_text()

    def test_a_page_change_lands_on_its_views_target(self):
        """showView lands through goTarget with the view's own target, so
        every section change and every routed link follows one rule."""
        body = _extract_fn(self.src, "showView")
        self.assertIn("if (was !== v) goTarget(v, viewTarget(v));", body)
        self.assertNotIn("window.scrollTo(0, 0)", body)

    def test_the_landing_reads_the_navs_real_height(self):
        body = _extract_fn(self.src, "goTarget")
        self.assertIn("publishNavHeight()", body)
        self.assertIsNone(re.search(r"\b10[0-9]\b", body), "a hard-coded nav height")

    def test_a_deep_links_project_lands_when_its_row_is_drawn(self):
        body = _extract_fn(self.src, "renderBoard")
        self.assertIn('boardLandAgain(showView.cur, goTarget.at, window.scrollY)) goTarget("work", orow)', body)

    def test_every_in_page_target_clears_the_sticky_nav(self):
        """A scrollIntoView lands a target at the viewport top, under the
        sticky navigation, unless the target carries a scroll margin of the
        nav's measured height. Zero specificity, so a target with its own
        clearance (the credit table's rows) keeps it."""
        self.assertIn(":where(.view [id],.view .brow){scroll-margin-top:calc(var(--navh,48px) + 8px)}",
                      self.src)
        self.assertIn("tr.qfam{scroll-margin-top:calc(var(--navh,48px) + 44px)}", self.src)

    def test_the_open_projects_head_sticks_below_the_nav(self):
        self.assertIn(".brow.open>.bstick{position:sticky;top:var(--navh,48px);", self.src)
        rule = re.search(r"#brows\{[^}]*\}", self.src).group(0)
        self.assertIn("overflow:clip", rule)
        self.assertNotIn("overflow:hidden", rule,
                         "a scroll container between the row and the page stops the head sticking")

    def test_the_landing_and_the_scroll_margin_agree_on_8_px(self):
        self.assertIn("const LAND_GAP = 8;", self.src)
        body = _extract_fn(self.src, "goTarget")
        self.assertIn("LAND_GAP", body)
        self.assertIn("goTarget.at = {v: v, y: Math.round(window.scrollY)};", body)

    def test_a_tab_picked_from_deep_in_a_tab_lands_on_its_row(self):
        """CURE P2-2: the strip sticks, so a tab picked from 3,000 px down a
        long tab swaps the body under a window that stays there. The swap
        reads whether the row's top is under the nav BEFORE it redraws, and
        lands the fresh row when it was."""
        body = _extract_fn(self.src, "projTabSwap")
        stuck = body.index("const stuck = row.getBoundingClientRect().top < (publishNavHeight() || 0);")
        swap = body.index("row.outerHTML = ")
        land = body.index('if (fresh && stuck) goTarget("work", fresh);')
        self.assertLess(stuck, swap)
        self.assertLess(swap, land)
        self.assertIn("publishStickHeight()", body)

    def test_an_in_row_target_clears_the_sticky_head(self):
        """CURE P2-3: the backlog pager inside a project's Tasks tab scrolled
        its top into view, where the sticky head covered it. The head's
        height is published as --bstickh and the Work page inside a
        project's Work tab (task/3643, the tab that replaced Tasks) clears
        both."""
        self.assertIn(".bdetail .wk{margin:4px 0 8px;scroll-margin-top:calc("
                      "var(--navh,48px) + var(--bstickh,0px) + 8px)}", self.src)
        self.assertIn("publishStickHeight()", _extract_fn(self.src, "renderBoard"))
        body = _extract_fn(self.src, "publishStickHeight")
        self.assertIn('"--bstickh"', body)

    def test_a_row_opened_by_a_tap_publishes_its_sticky_head(self):  # noqa: VACUOUS_ASSERTION — str.index() raises on each missing call, so the swap, the publish and the early return are asserted present before their order is compared
        """helm-codex's read of the P2-3 cure: a start at
        #work/projects?tab=work with nothing open publishes 0, and a tap
        that opens a closed project straight into Work swaps the row in
        place through boardToggle. Unless that swap publishes, the pager's
        scroll margin still reads --bstickh 0 and the head covers the
        target until the next board read. Closing publishes too, back to 0.
        Pinned at the source like the tab swap: boardToggle's world (the
        project list, the open set, the row renderer, the URL writer) is
        more than the node harness hosts."""
        body = _extract_fn(self.src, "boardToggle")
        swap = body.index("row.outerHTML = boardRowHTML(p, BOARD, open, PROJ_TAB);")
        publish = body.index("publishStickHeight();", swap)
        early = body.index("if (!open || !fresh) return;")
        self.assertLess(publish, early, "a close must publish 0 before the "
                        "early return, and an open before the pager scrolls")

    def test_the_dropped_pipeline_views_leave_no_css(self):
        for dead in (".pipeviews", "#pipehead"):
            self.assertNotIn(dead, self.src)
        # nor a doc claim: #scheduler opens Work › pipeline, and the holder
        # view it named went with that commit
        with open(os.path.join(os.path.dirname(HERE), "docs", "WEB.md"),
                  encoding="utf-8") as f:
            self.assertNotIn("(its holder view)", f.read())

    def test_the_team_tab_folds_what_the_project_does_not_use(self):
        body = _extract_fn(self.src, "teamParts")
        self.assertIn("teamSplit(cur, famOrder)", body)
        self.assertIn('" other famil"', body)
        self.assertIn('" absent"', body)


class NavLandRuntimeTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        with open(HARNESS, encoding="utf-8") as f:
            template = f.read()
        src = web_ui_loader.read_text()
        self_check = "\nconst TEAM_FOLDS = new Set();\n" in src
        assert self_check, "TEAM_FOLDS is no longer a plain Set"
        gap = re.search(r"\nconst LAND_GAP = \d+;\n", src)
        assert gap, "LAND_GAP is no longer a plain number"
        inject = "\n\n".join(["const TEAM_FOLDS = new Set();", gap.group(0).strip()]
                               + [_extract_fn(src, n) for n in FNS])
        script = template.replace("/*__INJECT__*/", inject)
        p = subprocess.run([cls.node, "-e", script], capture_output=True, text=True, timeout=60)
        if p.returncode != 0:
            raise AssertionError("harness failed: " + (p.stderr or "")[:800])
        cls.out = json.loads(p.stdout.strip().splitlines()[-1])

    def test_the_landing_puts_the_targets_top_just_below_the_nav(self):
        """The deep link's project at page top 1,822 under a 102 px nav lands
        at scrollY 1,720 with its top exactly at the nav's bottom edge — and
        a wrapped 158 px nav, landing from far below, the same way."""
        self.assertEqual(self.out["landY"], [1718, 1698, 0, 0])
        a = self.out["land_1440"]
        # the target's top 8 px below the nav: the same 8 px every in-page
        # target's scroll-margin-top leaves (LAND_GAP)
        self.assertEqual((a["y"], a["top_after"], a["calls"]), (1712, 110, [[0, 1712]]))
        self.assertEqual(a["navh"], "102px")
        self.assertEqual(a["at"], {"v": "work", "y": 1712})
        b = self.out["land_420"]
        self.assertEqual((b["y"], b["top_after"]), (1656, 166))

    def test_a_clamped_landing_records_where_the_window_is_and_lands_again(self):
        """CURE P2-1: the page was too short, so the browser stopped the
        window at 1000; the landing records 1000, not the 1712 it asked
        for, so the redraw after the page grows sees an unmoved reader and
        lands the open project under the nav."""
        c = self.out["clamped"]
        self.assertEqual(c["at"], {"v": "work", "y": 1000})
        self.assertTrue(c["again"])
        self.assertEqual((c["y"], c["top_after"]), (1712, 110))

    def test_a_view_with_no_target_lands_at_its_top(self):
        """The backlog opened at scrollY 811, past its own header."""
        self.assertEqual(self.out["land_none"], {"y": 0, "at": {"v": "backlog", "y": 0}})

    def test_work_lands_on_its_open_project_and_no_other_view_has_one(self):
        self.assertEqual(self.out["targets"], {"work": True, "pipeline": None, "backlog": None})
        self.assertIsNone(self.out["target_closed"])

    def test_a_redraw_lands_again_only_while_the_reader_has_not_scrolled(self):
        self.assertEqual(self.out["again"], [True, True, False, False, False, False])

    def test_absent_seats_and_unused_families_fold(self):
        """A family with no share here and no present seat folds; a present
        seat's family, a shared family and an unsaved seat stay open."""
        s = self.out["split"]
        self.assertEqual(s["used"], ["codex", "anthropic", "qwen"])
        self.assertEqual(s["other"], ["grok", "kimi", "pi", "cursor"])
        self.assertEqual(s["here"], [0, 1, 4, 5])
        self.assertEqual(s["absent"], [2, 3, 6])
        self.assertEqual(self.out["split_empty"], {"used": [], "other": [], "here": [], "absent": []})

    def test_a_fold_shows_its_count_and_keeps_the_readers_open_state(self):
        self.assertEqual(self.out["fold"], '<details class="tfold" data-team-fold="helm:absent">'
                         "<summary>3 absent</summary><i>rows</i></details>")
        self.assertEqual(self.out["fold_none"], "")
        self.assertIn('data-team-fold="helm:fams" open><summary>2 other families</summary>',
                      self.out["fold_kept"])


if __name__ == "__main__":
    unittest.main()
