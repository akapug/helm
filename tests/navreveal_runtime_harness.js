/* Client-runtime harness for revealActiveTab (nav narrow-screen strip).
 *
 * A review bound a FIX on 36aa9898: 27 production JS lines, zero tests, gate
 * count unchanged. The defect it cures is GEOMETRIC — a scrolling strip can
 * hide the tab it has selected — so a test that only greps the source proves
 * nothing about the arithmetic. This runs the ACTUAL revealActiveTab source,
 * lifted verbatim from the assembled web UI, against a fake strip whose rects
 * are derived from scrollLeft the way a real one's are.
 *
 * The real source is spliced in by tests/test_web_nav_reveal_runtime.py at the
 * marker below; everything else here is the shim world it closes over.
 * Plain CommonJS (non-strict) so the spliced declaration shares module scope. */

let STRIP = null;

/* The area grouping the spliced navBadge/showView read. This world has no
 * .navarea button, so the real areaBadge finds nothing and returns — the
 * roll-up's own behaviour is covered in tests/navareas_runtime_harness.js.
 * These two live at module scope because navBadge touches them on its FIRST
 * line, before any world is built. */
global.AREA_OF = {
  work: "work",
  chat: "chat",
  quota: "fleet", roster: "fleet", sessions: "fleet", configs: "fleet",
  boxes: "fleet",
  history: "history"
};
global.NAV_BADGE = {};
global.AREA_LAST = {};

/* A strip whose tab rect MOVES WITH scrollLeft, which is the whole point: a
 * fake returning fixed rects would let a no-op implementation pass. Tab
 * position is held in CONTENT coordinates and projected into viewport
 * coordinates exactly as the browser does. */
function world({stripLeft, stripW, contentW, tabX, tabW, scrollLeft}) {
  const tab = {
    classList: {contains: c => c === "on"},
    getBoundingClientRect: () => ({
      left:  stripLeft + tabX - STRIP.scrollLeft,
      right: stripLeft + tabX + tabW - STRIP.scrollLeft,
    }),
  };
  STRIP = {
    scrollLeft, scrollWidth: contentW, clientWidth: stripW,
    getBoundingClientRect: () => ({left: stripLeft, right: stripLeft + stripW}),
    querySelector: sel => (sel === ".navtab.on" ? tab : null),
  };
  return {strip: STRIP, tab};
}

let CURRENT_BADGE = null;   // navBadge creates its span when none exists
global.document = {
  querySelector: sel => (sel === ".navtabs" ? STRIP : null),
  createElement: () => CURRENT_BADGE,
};

/* GEOMETRY IS NOT THIS HARNESS'S SUBJECT (see the header). showView publishes
   the navigation's measured height for whatever sticks below it, which is a real
   browser measurement — it is measured by the Chrome arms in
   tests/test_web_home_geometry.py — so here it is a declared no-op rather than a
   fake rect this harness would then be tempted to assert about. */
function publishNavHeight() { return null; }

/*__INJECT__*/
/*__BOOT__*/

function visible(strip, tab) {
  const sr = strip.getBoundingClientRect(), tr = tab.getBoundingClientRect();
  return tr.left >= sr.left - 1 && tr.right <= sr.right + 1;
}

const CASES = {
  // the exact repro: 390px, strip 14..376, the selected tab at content x654
  hidden_right: {stripLeft: 14, stripW: 362, contentW: 900, tabX: 640, tabW: 101, scrollLeft: 0},
  // the mirror: user scrolled right, selection is off the LEFT edge
  hidden_left:  {stripLeft: 14, stripW: 362, contentW: 900, tabX: 0,   tabW: 68,  scrollLeft: 400},
  // MUST-MISS: already visible — the reveal must not scroll gratuitously
  already_ok:   {stripLeft: 14, stripW: 362, contentW: 900, tabX: 40,  tabW: 68,  scrollLeft: 0},
  // MUST-MISS: a strip with no box at all — nothing to reveal
  not_scrollable: {stripLeft: 0, stripW: 0, contentW: 0, tabX: 779, tabW: 101, scrollLeft: 0},
};

/* THE ORDERING WORLD. The badge-width arm was source-only, so
 * MOVING the reveal above the line that widens the tab passes a grep and
 * reinstates the bug. This runs the REAL navBadge against a tab whose WIDTH IS
 * A FUNCTION OF ITS BADGE TEXT, so a reveal computed before the text lands
 * measures the old width and leaves the tab hidden — which is the defect. */
function badgeWorld({stripLeft, stripW, contentW, tabX, baseW, scrollLeft}) {
  const badge = {textContent: "", title: "", classList: {toggle() {}},
                 remove: () => { hasBadge = false; }};
  CURRENT_BADGE = badge;
  let hasBadge = false;
  const tab = {
    classList: {contains: c => c === "on"},
    querySelector: sel => (sel === ".navbadge" && hasBadge ? badge : null),
    appendChild: () => { hasBadge = true; },
    getBoundingClientRect: () => {
      const w = baseW + (hasBadge ? badge.textContent.length * 11 : 0);
      return {left:  stripLeft + tabX - STRIP.scrollLeft,
              right: stripLeft + tabX + w - STRIP.scrollLeft};
    },
  };
  STRIP = {
    scrollLeft, scrollWidth: contentW, clientWidth: stripW,
    getBoundingClientRect: () => ({left: stripLeft, right: stripLeft + stripW}),
    querySelector: sel => (sel === ".navtab.on" &&
                           tab.classList.contains("on") ? tab : null),
  };
  global.$ = sel => (sel.indexOf(".navtab[") === 0 ? tab : null);
  return {strip: STRIP, tab};
}

const out = {};
for (const [name, cfg] of Object.entries(CASES)) {
  const {strip, tab} = world(cfg);
  const before = strip.scrollLeft;
  revealActiveTab();
  out[name] = {before, after: strip.scrollLeft, visible: visible(strip, tab),
               moved: strip.scrollLeft !== before};
}
/* The active tab sits just inside the right edge, then a badge widens it. */
{
  const {strip, tab} = badgeWorld({stripLeft: 14, stripW: 362, contentW: 900,
                                   tabX: 290, baseW: 80, scrollLeft: 0});
  revealActiveTab();                       // visible before the badge lands
  const beforeBadge = visible(strip, tab);
  navBadge("boxes", "99+", false, "t");    // widens the ACTIVE tab
  out.badge_widens_active = {beforeBadge, visible: visible(strip, tab),
                             scrollLeft: strip.scrollLeft};
}
/* MUST-MISS: the same widening on a NON-active tab must not scroll anything. */
{
  const {strip, tab} = badgeWorld({stripLeft: 14, stripW: 362, contentW: 900,
                                   tabX: 290, baseW: 80, scrollLeft: 0});
  tab.classList.contains = () => false;    // this tab is not the selected one
  const before = strip.scrollLeft;
  navBadge("chat", "99+", false, "t");
  out.badge_on_inactive = {moved: strip.scrollLeft !== before};
}
/* ADDING a badge to the tab immediately LEFT of the active tab shifts the
 * active tab right. A late badge did this to the selected tab after showView
 * had already revealed it. */
{
  let hasBadge = false;
  const badge = {remove: () => { hasBadge = false; }, classList: {toggle() {}},
                 textContent: "", title: ""};
  CURRENT_BADGE = badge;
  const before = {classList: {contains: () => false},
                  querySelector: () => hasBadge ? badge : null,
                  appendChild: () => { hasBadge = true; }};
  const active = {classList: {contains: c => c === "on"},
    getBoundingClientRect: () => {
      const x = 240 + (hasBadge ? 44 : 0);
      return {left: 14 + x - STRIP.scrollLeft,
              right: 14 + x + 98 - STRIP.scrollLeft};
    }};
  STRIP = {scrollLeft: 0, scrollWidth: 520, clientWidth: 362,
    getBoundingClientRect: () => ({left: 14, right: 376}),
    querySelector: sel => sel === ".navtab.on" ? active : null};
  global.$ = sel => sel.indexOf('data-v="configs"') >= 0 ? before : null;
  const beforeBadge = visible(STRIP, active);
  navBadge("configs", "?", true, "configs unavailable");
  out.badge_added_before_active = {beforeBadge, visible: visible(STRIP, active),
                                   scrollLeft: STRIP.scrollLeft};
}
/* REMOVING a badge from the tab immediately LEFT of the active tab shifts the
 * active tab left. If the strip was scrolled to the old right edge, that shift
 * can hide the selected tab off the LEFT edge — reproduced at 390px when a
 * preceding tab's first "?" badge disappeared. */
{
  let hasBadge = true;
  const badge = {remove: () => { hasBadge = false; }, classList: {toggle() {}},
                 textContent: "?", title: ""};
  const before = {classList: {contains: () => false},
                  querySelector: () => hasBadge ? badge : null};
  const active = {classList: {contains: c => c === "on"},
    getBoundingClientRect: () => {
      const x = 330 + (hasBadge ? 44 : 0);
      return {left: 14 + x - STRIP.scrollLeft,
              right: 14 + x + 98 - STRIP.scrollLeft};
    }};
  STRIP = {scrollLeft: 360, scrollWidth: 520, clientWidth: 362,
    getBoundingClientRect: () => ({left: 14, right: 376}),
    querySelector: sel => sel === ".navtab.on" ? active : null};
  global.$ = sel => sel.indexOf('data-v="configs"') >= 0 ? before : null;
  navBadge("configs", 0, false, "");
  out.badge_removed_before_active = {visible: visible(STRIP, active),
                                     scrollLeft: STRIP.scrollLeft};
}
/* ============ THE BOOT, EXECUTED, WITH REAL SELECTION ============
 * A review, twice: (1) a source check cannot bind reachability, so the boot is
 * RUN; (2) my first run PRE-SELECTED the tab — classList.contains hardcoded
 * true and $$ stubbed to [] — so showView could not perform the selection and
 * mutating its toggle line changed nothing.
 *
 * So nothing here is pre-satisfied. Every tab carries REAL on-state, $$ returns
 * the REAL list, and the strip resolves .navtab.on by asking the tabs. showView
 * must actually select for the reveal to have anything to find. */
{
  // THE FLEET ROW, the one section row left: at 390px its last tab is off
  // the strip's right edge
  const TABW = {quota: 74, roster: 76, sessions: 92, configs: 84, boxes: 101};
  let x = 0;
  const tabs = Object.entries(TABW).map(([v, w]) => {
    const t = {dataset: {v: v}, _x: (x += w + 8) - w - 8, _w: w, _on: false};
    t.classList = {
      contains: c => c === "on" && t._on,
      toggle: (c, val) => { if (c === "on") t._on = !!val; },
    };
    t.getBoundingClientRect = () => ({
      left:  14 + t._x - STRIP.scrollLeft,
      right: 14 + t._x + t._w - STRIP.scrollLeft,
    });
    return t;
  });
  STRIP = {
    scrollLeft: 0, scrollWidth: x, clientWidth: 362,
    getBoundingClientRect: () => ({left: 14, right: 376}),
    querySelector: sel => (sel === ".navtab.on"
                           ? tabs.find(t => t._on) || null : null),
  };
  global.VIEWS = ["work", "chat", "quota", "roster", "sessions", "configs",
                  "boxes", "history"];
  global.$$ = sel => (sel === ".navtab" ? tabs : []);   // the REAL list
  global.$ = () => null;
  global.location = {hash: "#boxes", search: ""};
  global.history = {replaceState() {}};
  global.localStorage = {getItem: () => null, setItem() {}};
  global.setTimeout = () => 0;
  global.setInterval = () => 0;
  // This harness owns nav reachability, not the independent Dregg lifecycle;
  // stub its public pair just like every other unrelated boot side effect.
  for (const k of ["initQuota", "initStorage", "initSessions", "cfgInit",
                   "srevInit", "odqInit", "initChat", "initRoster",
                   "initLedger", "startDreggPolling", "stopDreggPolling",
                   "esConnect", "openSession", "goOldSection"]) {
    global[k] = () => {};
  }

  const anySelectedBefore = tabs.some(t => t._on);
  __runBoot();
  const sel = tabs.find(t => t._on);
  const boxes = tabs.find(t => t.dataset.v === "boxes");
  out.boot_reveals = {
    anySelectedBefore,
    selected: sel ? sel.dataset.v : null,
    visible: sel ? visible(STRIP, sel) : false,
    scrollLeft: STRIP.scrollLeft,
    boxesWasOffscreen: 14 + boxes._x + boxes._w > 376,
  };
}

console.log(JSON.stringify(out));
