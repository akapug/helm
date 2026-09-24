#!/usr/bin/env python3
"""LAYOUT, MEASURED BY A REAL ENGINE — the two defects on this page that no
string can see.

Both findings here are GEOMETRIC: a sticky element covering an input, and a
flex row summing past the viewport. Neither is a property of any source text —
the CSS that produces both is individually correct — so the only instrument
that can hold them is a layout engine over the REAL assembled page. A python or
node mirror of flexbox would be a second implementation free to disagree with
the browser, which is the class of defect this suite spends most of its arms
refusing.

So these arms run headless Chrome over the page `helm web` serves, with ONE
substitution: the application script is replaced by a probe that lifts the real
`publishNavHeight` and the real landed-row renderers out of the same assembled
source and calls them. Nothing fetches, so nothing here depends on a server;
everything measured is the shipped markup under the shipped stylesheet.

WHERE CHROME IS ABSENT THESE SKIP, exactly as the node harnesses skip. A skipped
arm proves nothing, which is why each measurement carries its own MUST-HIT in
the same browser run: the same probe is measured a second time with the cure
disabled, and the arm asserts that the second measurement FAILS the property. An
instrument that cannot see the defect cannot witness its absence.
"""
import json
import os
import re
import shutil
import subprocess
import unittest

from helm import web_ui_loader
from tests.test_web_chat_client_runtime import _extract_fn

CHROME = ("google-chrome", "google-chrome-stable", "chromium",
          "chromium-browser")
# Headless Chrome clamps its window to 500px wide, so a 390px VIEWPORT is not
# available to this instrument. The phone rules key on `max-width:680px`, which a
# 500px viewport satisfies exactly as a 390px one does, so the media query under
# test is live; the 390px design floor is then applied as the CONTENT width,
# which is what a flex row's own layout depends on.
PHONE_VIEWPORT = (500, 844)
PHONE_CONTENT = 390
DESKTOP_VIEWPORT = (1400, 900)


def _chrome():
    for name in CHROME:
        path = shutil.which(name)
        if path:
            return path
    return None


def _page_without_its_app_script(src):
    """The assembled page with the application script's BODY removed.

    The markup, every stylesheet and the script tag's position are untouched:
    what goes is only the code that would fetch. A probe is injected in its
    place, and the functions the probe drives are lifted out of the same source
    it replaced, so the page under measurement is the shipped page.
    """
    blocks = [(m.start(), m.end(), m.group(1))
              for m in re.finditer(r"(?s)<script>(.*?)</script>", src)]
    assert blocks, "the assembled page carries no inline script"
    start, end, body = max(blocks, key=lambda b: len(b[2]))
    assert "function showView(" in body, \
        "the largest inline script is not the application script"
    # The WHOLE element goes, tags included: the probe brings its own.
    return src[:start], src[end:]


class HomeGeometryBase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.chrome = _chrome()
        if not cls.chrome:
            raise unittest.SkipTest("no chrome/chromium available")
        cls.src = web_ui_loader.read_text()
        cls.before, cls.after = _page_without_its_app_script(cls.src)
        cls.tmp = os.environ.get("TMPDIR") or "/tmp"

    def measure(self, probe, viewport, name):
        """Run one probe inside the real page and return what it wrote.

        The probe writes its answer into a <pre> the page did not have, which is
        what `--dump-dom` can carry back out. A probe that throws writes nothing
        and this fails on the missing element rather than on a default value —
        an absent measurement must never read as a measurement of zero.
        """
        page = self.before + probe + self.after
        path = os.path.join(self.tmp, "helm-geom-%s.html" % name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(page)
        try:
            proc = subprocess.run(
                [self.chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
                 "--window-size=%d,%d" % viewport, "--virtual-time-budget=4000",
                 "--dump-dom", "file://" + path],
                capture_output=True, text=True, timeout=180)
        finally:
            os.unlink(path)
        dom = proc.stdout
        found = re.search(r'(?s)<pre id="helmgeom">(.*?)</pre>', dom)
        self.assertIsNotNone(
            found, "the probe wrote no measurement — chrome said %r"
                   % (proc.stderr or "")[-600:])
        return json.loads(found.group(1))


class StickyControlsClearTheNavigationTest(HomeGeometryBase):
    """THE SESSIONS SEARCH INPUT IS REACHABLE AT A SUPPORTED WIDTH.

    #nav is sticky at top:0 and the Sessions controls stick under it. The
    clearance was a constant written for a one-row nav; the sections now occupy
    a second row, so at 1400x900 with the page scrolled the navigation ends
    below where the controls stop, and a pointer inside the search input lands on
    a navigation button. The input is what the view exists to type into.
    """

    def probe(self):
        return """<script>
const $ = s => document.querySelector(s);
""" + _extract_fn(self.src, "publishNavHeight") + """
function hit() {
  const nav = $("#nav").getBoundingClientRect();
  const input = $("#view-sessions input[type=search]").getBoundingClientRect();
  const at = document.elementFromPoint(input.left + 24,
                                       input.top + input.height / 2);
  return {navBottom: nav.bottom, inputTop: input.top, inputBottom: input.bottom,
          navh: getComputedStyle(document.documentElement)
                  .getPropertyValue("--navh").trim(),
          stickyTop: getComputedStyle($("#sesscontrols")).top,
          landedOn: at ? (at.id || at.className || at.tagName) : null,
          insideControls: !!(at && at.closest && at.closest("#sesscontrols")),
          insideNav: !!(at && at.closest && at.closest("#nav"))};
}
const out = {};
document.querySelectorAll(".view").forEach(v => v.classList.remove("on"));
$("#view-sessions").classList.add("on");
// TALL RESULTS, so the page genuinely scrolls: a sticky element that never
// leaves its static position cannot cover anything, and a measurement taken
// without scrolling would pass over the defect.
const rows = document.createElement("div");
rows.style.height = "4000px";
$("#view-sessions").appendChild(rows);
// THE MUST-HIT FIRST, with the cure withheld. The published height is what the
// stylesheet reads; removing it drops the controls back onto the old constant,
// and this measurement is what proves the probe can SEE the defect it is about
// to assert the absence of.
document.documentElement.style.removeProperty("--navh");
window.scrollTo(0, 400);
out.uncured = hit();
publishNavHeight();
window.scrollTo(0, 400);
out.cured = hit();
const pre = document.createElement("pre");
pre.id = "helmgeom";
pre.textContent = JSON.stringify(out);
document.body.appendChild(pre);
</script>"""

    def test_the_search_input_is_not_covered_by_the_second_nav_row(self):
        out = self.measure(self.probe(), DESKTOP_VIEWPORT, "sticky")
        cured, uncured = out["cured"], out["uncured"]
        # THE MUST-HIT: with the measured height withheld, the controls sit at
        # the old constant, the navigation reaches past them, and the pointer
        # lands inside the navigation instead of the input. Asserted FIRST,
        # because every claim below is an absence and an instrument that cannot
        # see this defect cannot witness its repair.
        self.assertLess(uncured["inputTop"], uncured["navBottom"],
                        "the uncured control did not reproduce the overlap")
        self.assertTrue(uncured["insideNav"], uncured)
        # AND THE CURE, over the same page in the same browser: the navigation's
        # real height is published, the controls clear it, and the pointer inside
        # the input reaches the input.
        self.assertTrue(cured["navh"].endswith("px"), cured)
        self.assertEqual(cured["stickyTop"], cured["navh"], cured)
        self.assertGreaterEqual(cured["inputTop"], cured["navBottom"] - 1, cured)
        self.assertTrue(cured["insideControls"], cured)
        self.assertFalse(cured["insideNav"], cured)


class TheNavHeightSURVIVESTheLateStatsRowTest(HomeGeometryBase):
    """THE NAV GROWS AFTER THE PAGE HAS ALREADY MEASURED IT.

    `showView` measures the navigation once, on the way into a view, and the
    stats arrive LATER — the session catalog answers asynchronously and fills
    `#navstats`. At phone widths that row is `flex:1 0 100%`, so the stats are a
    whole extra nav row, and the published `--navh` the sticky controls read
    still describes the navigation as it was BEFORE they landed. The controls
    then sit under the navigation, and a pointer in the search input hits a nav
    button again — the same defect as the constant, arriving by a different road,
    and invisible to an arm that calls the publisher by hand.

    So the cure is that whatever writes the stats republishes the height, and
    this measures it through the REAL renderNav.
    """

    def probe(self):
        esc = [ln for ln in self.src.splitlines() if ln.startswith("const esc = ")]
        assert len(esc) == 1, "the assembled page's esc definition moved"
        return """<script>
const $ = s => document.querySelector(s);
""" + esc[0] + "\n" + _extract_fn(self.src, "publishNavHeight") + "\n" \
            + _extract_fn(self.src, "statCell") + """
// THE MODULE STATE renderNav READS. `NAV_SESS` is what the catalog callback
// writes; the rest are the other cells' own, empty here.
let NAV_ACC = "", NAV_SESS = "", NAV_CHAT = 0, NAV_CHAT_MENTION = false;
let NAV_LR = 0, NAV_LR_TITLE = "";
// THE BADGE PATH IS SHIMMED AND THE HEIGHT PATH IS REAL, deliberately: badges
// have their own arms (they mutate a tab, not the nav's row count), and what is
// under measurement here is the republish that follows the stats mutation. The
// two statements this arm depends on — the `#navstats` write and the height
// publish — are the real renderNav's own.
function navBadge() {}
""" + _extract_fn(self.src, "renderNav") + """
function hit() {
  const nav = $("#nav").getBoundingClientRect();
  const input = $("#view-sessions input[type=search]").getBoundingClientRect();
  // SAMPLED NEAR THE INPUT'S TOP EDGE, which is the part a too-short clearance
  // hides: one extra nav row covers about 30px of it, and a point at the
  // input's mid-height can sit below the navigation while the input's typing
  // edge is under it. The centre would have made this probe blind to exactly
  // the defect it is here for.
  const at = document.elementFromPoint(input.left + 24, input.top + 4);
  return {navBottom: nav.bottom, navHeight: nav.height, inputTop: input.top,
          navh: getComputedStyle(document.documentElement)
                  .getPropertyValue("--navh").trim(),
          stickyTop: getComputedStyle($("#sesscontrols")).top,
          landedOn: at ? (at.id || at.className || at.tagName) : null,
          insideControls: !!(at && at.closest && at.closest("#sesscontrols")),
          insideNav: !!(at && at.closest && at.closest("#nav"))};
}
const out = {};
document.querySelectorAll(".view").forEach(v => v.classList.remove("on"));
$("#view-sessions").classList.add("on");
// TALL RESULTS so the page genuinely scrolls; a sticky element still in its
// static position cannot cover anything.
const rows = document.createElement("div");
rows.style.height = "4000px";
$("#view-sessions").appendChild(rows);
// THE FIRST MEASUREMENT, on the nav as `showView` finds it: no stats yet.
$("#navstats").innerHTML = "";
out.first = publishNavHeight();
window.scrollTo(0, 400);
out.before = hit();
// THE STATS LAND LATE, exactly as the catalog callback delivers them: real
// cells from the real statCell, then the real renderNav.
NAV_SESS = statCell(1234, "sessions", "+40 reference transcripts")
  + statCell("9\u00b73", "cl\u00b7cx") + statCell("12.5", "GB");
renderNav();
window.scrollTo(0, 400);
out.cured = hit();
// THE MUST-HIT, with the cure's EFFECT withheld: the stats are on the page and
// the height published is the one measured before they arrived, which is what
// the page carried when renderNav did not republish. This is the measurement
// that proves the probe can see the defect.
document.documentElement.style.setProperty("--navh", out.first + "px");
window.scrollTo(0, 400);
out.uncured = hit();
const pre = document.createElement("pre");
pre.id = "helmgeom";
pre.textContent = JSON.stringify(out);
document.body.appendChild(pre);
</script>"""

    def test_the_stats_row_republishes_the_height_the_controls_read(self):
        out = self.measure(self.probe(), PHONE_VIEWPORT, "navstats")
        before, cured, uncured = out["before"], out["cured"], out["uncured"]
        # UNCONDITIONAL POSITIVE CONTROL FIRST, on the same page and the same
        # observable: BEFORE the stats land the published height is correct and
        # the pointer inside the search input reaches the input. Every claim
        # below is therefore about what the late stats did, not about a page that
        # was already broken.
        self.assertEqual(before["stickyTop"], before["navh"], before)
        self.assertTrue(before["insideControls"], before)
        # THE STATS REALLY ADDED A ROW: without this the arm could pass over a
        # viewport where the stats change nothing, and prove nothing.
        self.assertGreater(cured["navHeight"], before["navHeight"], out)
        # THE MUST-HIT: stats present, old height published — the navigation
        # reaches past the controls and the pointer lands inside it.
        self.assertLess(uncured["inputTop"], uncured["navBottom"], uncured)
        self.assertTrue(uncured["insideNav"], uncured)
        # AND THE CURE: renderNav republished, so the number the stylesheet reads
        # moved with the nav and the input is reachable again.
        self.assertNotEqual(cured["navh"], before["navh"], out)
        self.assertEqual(cured["navh"], str(round(cured["navHeight"])) + "px",
                         cured)
        self.assertEqual(cured["stickyTop"], cured["navh"], cured)
        self.assertGreaterEqual(cured["inputTop"], cured["navBottom"] - 1, cured)
        self.assertTrue(cured["insideControls"], cured)
        self.assertFalse(cured["insideNav"], cured)


class ALandedRowFitsThePhoneTest(HomeGeometryBase):
    """AN EXPANDED LANDED ROW DOES NOT DRAG THE PAGE SIDEWAYS.

    The row has six cells and five of them refuse to shrink, so an ordinary
    short land — a two-character lane, a task number, an eight-character tip, a
    twelve-character gate token and the unrecorded-proof verdict — summed past
    the 390px design floor and widened the document. The rows are rendered by
    the REAL renderers lifted out of the assembled page, over the envelope the
    REAL producer emits, so the measured row is the row the owner gets.
    """

    def probe(self):
        fns = "\n\n".join(_extract_fn(self.src, n) for n in
                          ("lrDur", "lrAgo", "cardSource", "cardBoundS",
                           "cardStale",
                           "dashLandRow",
                           "dashLandGroups", "dashLandPrimary",
                           "dashLandVerdictKey", "dashLandGroup", "dashLands"))
        esc = [ln for ln in self.src.splitlines() if ln.startswith("const esc = ")]
        assert len(esc) == 1, "the assembled page's esc definition moved"
        row = json.dumps({
            "lane": "ui", "task": "2355", "reviewed_tip": "a" * 40,
            "gate": "b" * 16, "trunk_sha": "c" * 40,
            "trunk_ref": "refs/remotes/origin/main", "on_trunk": None,
            "how": None, "chain_root": None, "ts": "2026-09-12T00:00:00Z",
            "age_s": 3600, "ts_unreadable": False})
        return """<script>
const $ = s => document.querySelector(s);
""" + esc[0] + "\n" + fns + """
// THE CONTENT WIDTH IS THE PHONE FLOOR. The viewport is what the media query
// reads; this is what the flex row lays out inside.
const wrap = document.createElement("div");
wrap.style.width = "%dpx";
wrap.style.margin = "0";
document.body.style.margin = "0";
const band = $("#dash");
wrap.appendChild(band);
document.body.insertBefore(wrap, document.body.firstChild);
const row = %s;
dashLands({rows: [row], total: 1, rows_truncated: false,
           source: "helm lr list", unavailable: null}, false, 3);
function widest() {
  const left = wrap.getBoundingClientRect().left;
  let right = left, worst = null;
  $("#dlands").querySelectorAll("*").forEach(node => {
    const r = node.getBoundingClientRect();
    if (r.width && r.right > right) { right = r.right; worst = node.className; }
  });
  return {overflow: Math.round((right - left) * 100) / 100, worst: worst,
          wrapped: getComputedStyle($("#dlands .dland")).flexWrap};
}
const out = {cured: widest()};
// THE MUST-HIT: the phone rule is what makes the row wrap. Overriding it back to
// a single line reproduces the overflow in this same browser, which is what
// proves this measurement can see it.
$("#dlands").querySelectorAll(".dland").forEach(node => {
  node.style.flexWrap = "nowrap";
});
out.uncured = widest();
const pre = document.createElement("pre");
pre.id = "helmgeom";
pre.textContent = JSON.stringify(out);
document.body.appendChild(pre);
</script>""" % (PHONE_CONTENT, row)

    def test_the_row_wraps_instead_of_widening_the_page(self):
        out = self.measure(self.probe(), PHONE_VIEWPORT, "phone")
        cured, uncured = out["cured"], out["uncured"]
        # THE MUST-HIT FIRST: held on one line, this exact row overflows the
        # phone floor, so the instrument demonstrably sees the defect.
        self.assertGreater(uncured["overflow"], PHONE_CONTENT,
                           "the uncured control did not reproduce the overflow")
        # AND THE CURE: the shipped rule wraps the row and every cell lands
        # inside the floor.
        self.assertEqual(cured["wrapped"], "wrap", cured)
        self.assertLessEqual(cured["overflow"], PHONE_CONTENT, cured)


if __name__ == "__main__":
    unittest.main()
