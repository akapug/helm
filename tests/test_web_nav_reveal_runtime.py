#!/usr/bin/env python3
"""revealActiveTab, run for real — the nav strip must show the tab it selected.

A FIX was bound on 36aa9898: the commit added 27 production JS lines and
ZERO tests, and the gate count was unchanged, which is the honest tell that
nothing exercised them. The defect being cured is GEOMETRIC (a scrolling strip
can hide its own selected tab), so a source grep would assert the function
exists while saying nothing about whether its arithmetic scrolls the right way
or at all.

So the ACTUAL function source is lifted verbatim from the assembled web UI and
run under node against a fake strip whose tab rect is DERIVED from scrollLeft,
exactly as a browser's is. A stub returning fixed rects would let a no-op body
pass every arm.

Requires node; SKIPPED (never failed) where node is absent, like every other
client-runtime harness in this suite."""
import json
import os
import shutil
import subprocess
import unittest

from helm import web_ui_loader
from tests.test_web_chat_client_runtime import _extract_fn

def _boot_block(src):
    """The boot block's verbatim `{...}`, brace-matched from its banner."""
    i = src.index("================= boot =================")
    j = src.index("{", i)
    depth, k = 0, j
    while k < len(src):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[j:k + 1]
        k += 1
    raise AssertionError("boot block not brace-matched")


HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(HERE, "navreveal_runtime_harness.js")


class TestNavRevealSource(unittest.TestCase):
    """Node-free contracts, so they hold where the runtime harness skips."""

    def test_the_reveal_is_called_after_the_active_class_moves(self):
        """showView toggles .on and THEN reveals. Reversed, it would reveal the
        PREVIOUS tab every time — a bug that still looks like it scrolls."""
        src = web_ui_loader.read_text()
        body = _extract_fn(src, "showView")
        self.assertIn("revealActiveTab()", body)
        self.assertLess(body.index('classList.toggle("on"'),
                        body.index("revealActiveTab()"))

    def test_the_badge_writer_re_reveals_after_every_mutation(self):
        """A badge changes nav-strip geometry after showView already scrolled.
        The badged tab need not be active: adding or removing a badge before the
        selected tab shifts the selected tab too."""
        body = _extract_fn(web_ui_loader.read_text(), "navBadge")
        self.assertEqual(body.count("revealActiveTab()"), 2)
        self.assertNotIn('classList.contains("on")', body)


    def test_showView_is_the_only_place_a_tab_becomes_selected(self):
        """THE BOOT PATH IS COVERED BY CONSTRUCTION, WHICH IS WHY THIS ARM
        EXISTS. The harness never executes the initial `/#ledger`
        boot, and that boot is the ORIGINAL repro — the page OPENS on ledger
        with no tab visible. The boot is a bare block in 90-shared.js.part, not
        a function, so it cannot be spliced and run the way navBadge can.

        So pin the invariant instead: exactly ONE site in the whole assembled
        UI selects a nav tab, and it lives in showView. Every selecting path —
        boot, click, hashchange, the dregg strip, the note-tab links, the two
        showView("ledger") calls — must therefore route through the function
        that reveals. A new path that sets .on directly would bypass the reveal
        silently, and this arm is what refuses it.

        Measured: 11 classList.toggle("on") sites in the UI, exactly one on
        .navtab. The others are .qtab, .chip and friends and are not tabs."""
        src = web_ui_loader.read_text()
        sel = '$$(".navtab").forEach(t => t.classList.toggle("on"'
        self.assertEqual(src.count(sel), 1,
                         "a second nav-tab selection site would bypass the reveal")
        body = _extract_fn(src, "showView")
        self.assertIn(sel, body, "the only selection site is not inside showView")

    def test_the_boot_block_routes_a_hash_view_through_showView(self):
        """And the boot really does select that way. A boot that set the class
        itself would satisfy the arm above (one site, in showView) while never
        calling it — so this checks the caller, not just the callee."""
        src = web_ui_loader.read_text()
        i = src.index("================= boot =================")
        boot = src[i:i + 700]
        self.assertIn("showView(v)", boot)
        self.assertIn("VIEWS.includes(v)", boot)
        self.assertNotIn('classList.toggle("on"', boot)


class TestNavRevealRuntime(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        with open(HARNESS, encoding="utf-8") as f:
            template = f.read()
        assert "/*__INJECT__*/" in template
        src = web_ui_loader.read_text()
        # BOTH functions: the ordering arm runs the REAL navBadge, because the
        # defect it guards is WHERE the reveal sits inside it, not whether the
        # call exists.
        # THE WHOLE CHAIN, REAL: hash -> canonView -> showView -> reveal.
        # Stubbing showView would test the harness; splicing it is what makes
        # the boot arm bind reachability.
        # areaBadge joins them because navBadge now CALLS it on every path: a
        # section in a closed area is off screen, so its badge rolls up to the
        # area button. Splicing the real one rather than stubbing keeps this
        # harness honest about what navBadge actually executes.
        fn = "\n\n".join(_extract_fn(src, n) for n in
                          ("revealActiveTab", "navBadge", "areaBadge",
                           "canonView", "showView"))
        cls.script = template.replace("/*__INJECT__*/", fn)
        # THE BOOT IS SPLICED AS A CALLABLE, not asserted about. It is a bare
        # block, so brace-match it from its banner and wrap it — that is the
        # only way to bind REACHABILITY, which no source check can (the
        # broke three string arms, the last with `if (false && ...)`).
        cls.script = cls.script.replace("/*__BOOT__*/",
                                        "function __runBoot() " + _boot_block(src))
        p = subprocess.run([cls.node, "-e", cls.script],
                           capture_output=True, text=True, timeout=60)
        if p.returncode != 0:
            raise AssertionError("harness failed: " + (p.stderr or "")[:600])
        cls.out = json.loads(p.stdout.strip().splitlines()[-1])

    def test_a_tab_off_the_right_edge_is_revealed(self):
        """The exact repro: strip 14..376, selected tab at content 640,
        scrollLeft 0 — the page is ON that view with no tab looking selected."""
        r = self.out["hidden_right"]
        self.assertEqual(r["before"], 0)
        self.assertTrue(r["moved"], "the reveal did not scroll at all")
        self.assertTrue(r["visible"], "tab still hidden after reveal")

    def test_a_tab_off_the_left_edge_is_revealed(self):
        """The mirror case. An implementation that only handles the right edge
        passes the repro above and still strands a user who scrolled right."""
        r = self.out["hidden_left"]
        self.assertTrue(r["moved"])
        self.assertTrue(r["visible"])

    def test_an_already_visible_tab_is_not_scrolled(self):
        """MUST-MISS on the same call: a reveal that always scrolls would pass
        both arms above while yanking the strip on every view switch."""
        r = self.out["already_ok"]
        self.assertFalse(r["moved"], "scrolled a tab that was already visible")
        self.assertTrue(r["visible"])

    def test_a_non_scrollable_strip_is_left_alone(self):  # noqa: VACUOUS_ASSERTION — the first assertion is an unconditional positive control on the SAME `moved` field from the SAME harness run (hidden_right moved); the rung cannot span the dict key, but a constant-False field or an unrun harness fails that line before reaching the absence assertions
        """A strip with nothing to scroll: scrollWidth == clientWidth == 0, the
        shape a strip that has not been laid out has. The reveal must return
        immediately rather than compute against a zero-width rect. (This was
        the desktop case while .navtabs was display:contents; since the sections
        moved onto their own row the desktop no-op is the equal-width one, which
        this same early return covers.)"""
        # UNCONDITIONAL CONTROL on the same observable, from the same harness
        # run: another case DID move. Without it, a harness that never ran this
        # case — or a `moved` field wired to a constant False — satisfies the
        # assertion below while proving nothing about the early return.
        self.assertTrue(self.out["hidden_right"]["moved"])
        r = self.out["not_scrollable"]
        self.assertFalse(r["moved"])
        self.assertEqual(r["after"], 0)


    def test_a_badge_that_widens_the_active_tab_re_reveals_it(self):
        """The badge arm was SOURCE-ONLY and order-blind. Moving the
        reveal ABOVE the line that sets textContent still contains the call, so
        a grep passes while the reveal measures the OLD width and leaves the tab
        hidden — the exact bug it was added to fix.

        This runs the real navBadge against a tab whose WIDTH IS A FUNCTION OF
        ITS BADGE TEXT. The tab starts visible, so a reveal that fires before
        the text lands has nothing to do and the tab ends hidden."""
        r = self.out["badge_widens_active"]
        self.assertTrue(r["beforeBadge"], "fixture broken: tab hidden before the badge")
        self.assertTrue(r["visible"], "badge widened the active tab and it was not re-revealed")

    def test_booting_on_a_hash_view_reveals_its_tab(self):
        """THE ORIGINAL REPRO, EXECUTED. The page OPENS on a hash whose tab
        sits off-screen (#boxes, the last of Fleet's row at 390px); boot must
        leave it visible.

        This RUNS the real boot block rather than asserting about its text,
        because three string-level arms were broken in a row — the last with
        `if (false && VIEWS.includes(v)) showView(v)`, which keeps every
        literal a source check looks for and never executes. Reachability is
        not a property of source text."""
        r = self.out["boot_reveals"]
        # NOTHING IS PRE-SATISFIED (the fifth finding): no tab is
        # selected before boot, so showView must do the selecting, and the
        # boxes tab really is off the strip's right edge to begin with.
        self.assertFalse(r["anySelectedBefore"], "fixture pre-selected a tab")
        self.assertTrue(r["boxesWasOffscreen"], "fixture: boxes was not off-screen")
        self.assertEqual(r["selected"], "boxes", "showView did not select the hash view")
        self.assertTrue(r["visible"], "boot did not reveal the selected tab")
        self.assertGreater(r["scrollLeft"], 0)

    def test_a_badge_before_the_active_tab_re_reveals_it(self):
        """The production repro: a late alarm badge widened a preceding tab
        and pushed the already-selected tab beyond the strip."""
        r = self.out["badge_added_before_active"]
        self.assertTrue(r["beforeBadge"], "fixture: selected tab was already hidden")
        self.assertTrue(r["visible"], "preceding badge hid the selected tab")
        self.assertGreater(r["scrollLeft"], 0)

    def test_removing_a_badge_before_the_active_tab_re_reveals_it(self):
        """The mirror: removing a preceding badge can pull the selected tab
        behind the strip's left edge when the strip remains scrolled right."""
        r = self.out["badge_removed_before_active"]
        self.assertTrue(r["visible"], "preceding badge removal hid the selected tab")
        self.assertLess(r["scrollLeft"], 360)

    def test_an_inactive_badge_that_does_not_shift_selection_scrolls_nothing(self):  # noqa: VACUOUS_ASSERTION — badge_added_before_active is the same-run positive control proving navBadge can move this strip; badge_on_inactive must then remain still
        """MUST-MISS: calling reveal after each badge mutation is harmless when
        the selected tab is absent from this synthetic strip."""
        self.assertGreater(self.out["badge_added_before_active"]["scrollLeft"], 0)
        self.assertFalse(self.out["badge_on_inactive"]["moved"])


if __name__ == "__main__":
    unittest.main()
