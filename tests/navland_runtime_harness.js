/* Client-runtime harness for WHERE A NAVIGATION LANDS (task/3475).
 *
 * The owner-walk measured four navigation defects on the web console: a
 * section change kept the last page's scroll, a deep link to an open project
 * landed at scrollY 0 with the project 1,720 px down, an in-page link put its
 * heading UNDER the sticky navigation, and an open project's head scrolled
 * away over its tab. Each cure is
 * arithmetic or state over the real page source, so the ACTUAL functions are
 * spliced in at the marker below by tests/test_web_nav_land_runtime.py and run
 * against a small world whose rects move with the window's scroll the way a
 * browser's do: a fake returning fixed rects would let a no-op landing pass.
 *
 * Plain CommonJS (non-strict) so the spliced declarations share module scope. */

const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));

/* THE WINDOW: one scroll offset, and every element's viewport top is its page
 * top less it. */
const WIN = {y: 0, calls: [], max: Infinity};
/* scrollTo CLAMPS like a browser's: the window cannot scroll past the page's
   height less its own, so a landing asked for below that stops short. */
global.window = {
  get scrollY() { return WIN.y; },
  scrollTo(x, y) { WIN.calls.push([x, y]); WIN.y = Math.max(0, Math.min(y, WIN.max)); },
};
function box(pageTop, h, extra) {
  return Object.assign({pageTop, h, hidden: false, dataset: {}, attrs: {},
    classList: {set: new Set(), toggle(c, on) { on ? this.set.add(c) : this.set.delete(c); },
                contains(c) { return this.set.has(c); }},
    setAttribute(k, v) { this.attrs[k] = v; },
    innerHTML: "",
    getBoundingClientRect() { return {top: this.pageTop - WIN.y, height: this.h}; }}, extra || {});
}
/* the sticky navigation: always at the viewport top, with a height the test
 * sets, which is exactly what publishNavHeight has to read */
const NAV = {h: 102, getBoundingClientRect() { return {top: 0, height: NAV.h}; }};
const ROOT_STYLE = {};
const EL = {"#nav": NAV};
let OPEN_ROW = null;
global.document = {
  documentElement: {style: {setProperty(k, v) { ROOT_STYLE[k] = v; }}},
  querySelector: sel => (sel === "#brows .brow.open" ? OPEN_ROW : null),
  addEventListener() {},
};
const $ = sel => EL[sel] || null;
function boardSec(d, name) { return (d && d.sections && d.sections[name]) || null; }
function showView() {}

/*__INJECT__*/

const out = {};

/* 1. landY, and goTarget against the nav's MEASURED height */
out.landY = [landY(1820, 0, 102), landY(300, 1500, 102), landY(40, 0, 102), landY(0, 0, null)];
function landAt(navH, pageTop, from) {
  NAV.h = navH; WIN.y = from; WIN.calls.length = 0;
  const el = box(pageTop, 400);
  const y = goTarget("work", el);
  return {y, top_after: el.getBoundingClientRect().top, calls: WIN.calls.slice(),
          navh: ROOT_STYLE["--navh"], at: goTarget.at};
}
out.land_1440 = landAt(102, 1822, 0);          // the deep link: project 1,720 px under the nav
out.land_420 = landAt(158, 1822, 3000);        // a wrapped nav, from far below
out.land_none = (() => { WIN.y = 811; WIN.calls.length = 0; goTarget("backlog", null);
  return {y: WIN.y, at: goTarget.at}; })();

/* viewTarget: Work lands on its open project, every other view on its top */
OPEN_ROW = box(1822, 4443);
out.targets = {work: viewTarget("work") === OPEN_ROW, pipeline: viewTarget("pipeline"),
               backlog: viewTarget("backlog")};
OPEN_ROW = null;
out.target_closed = viewTarget("work");

/* the deep link's second landing: only while Work is shown and unmoved */
out.again = [boardLandAgain("work", {v: "work", y: 0}, 0),
             boardLandAgain("work", {v: "work", y: 1720}, 1720.4),
             boardLandAgain("work", {v: "work", y: 0}, 240),
             boardLandAgain("pipeline", {v: "work", y: 0}, 0),
             boardLandAgain("work", {v: "backlog", y: 0}, 0),
             boardLandAgain("work", undefined, 0)];

/* a CLAMPED first landing: the page is still too short to scroll the open
   project under the nav, so the window stops at its bottom; the landing
   records where it really is, and once the page grows the redraw lands the
   project for real */
out.clamped = (() => {
  NAV.h = 102; WIN.y = 0; WIN.max = 1000; WIN.calls.length = 0;
  OPEN_ROW = box(1822, 4443);
  goTarget("work", viewTarget("work"));
  const at = goTarget.at, again = boardLandAgain("work", at, window.scrollY);
  WIN.max = Infinity;
  if (again) goTarget("work", viewTarget("work"));
  const r = {at, again, y: WIN.y, top_after: OPEN_ROW.getBoundingClientRect().top};
  OPEN_ROW = null;
  return r;
})();

/* 4. the Team tab's folds */
const CUR = {shares: {codex: 30},
  members: [{seat: "helm-claude", family: "anthropic", state: "live"},
            {seat: "helm-codex", family: "openai", spends: "codex", state: "idle"},
            {seat: "old-grok", family: "grok", state: "absent"},
            {seat: "old-kimi", family: "kimi", state: "absent"},
            {seat: "new-qwen", family: "qwen", state: "absent", added: true},
            {seat: "gone-pi", family: "pi", state: "absent", gone: true},
            {seat: "nostate", family: "kimi"}]};
out.split = teamSplit(CUR, ["codex", "anthropic", "grok", "kimi", "qwen", "pi", "cursor"]);
out.split_empty = teamSplit({}, []);
out.fold = teamFold("helm", "absent", "3 absent", "<i>rows</i>", 3);
out.fold_none = teamFold("helm", "absent", "0 absent", "", 0);
TEAM_FOLDS.add("helm:fams");
out.fold_kept = teamFold("helm", "fams", "2 other families", "<i>f</i>", 2);

console.log(JSON.stringify(out));
