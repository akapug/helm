/* Client-runtime harness for revealActiveTab (nav narrow-screen strip).
 *
 * @codex-2 bound a FIX: 27 production JS lines, zero tests, gate
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

/*__INJECT__*/
/*__BOOT__*/

function visible(strip, tab) {
  const sr = strip.getBoundingClientRect(), tr = tab.getBoundingClientRect();
  return tr.left >= sr.left - 1 && tr.right <= sr.right + 1;
}

const CASES = {
  // @codex-2's exact repro: 390px #ledger, strip 14..376, tab at content x654
  hidden_right: {stripLeft: 14, stripW: 362, contentW: 900, tabX: 640, tabW: 101, scrollLeft: 0},
  // the mirror: user scrolled right, selection is off the LEFT edge
  hidden_left:  {stripLeft: 14, stripW: 362, contentW: 900, tabX: 0,   tabW: 68,  scrollLeft: 400},
  // MUST-MISS: already visible — the reveal must not scroll gratuitously
  already_ok:   {stripLeft: 14, stripW: 362, contentW: 900, tabX: 40,  tabW: 68,  scrollLeft: 0},
  // MUST-MISS: desktop display:contents — no box at all, nothing to reveal
  not_scrollable: {stripLeft: 0, stripW: 0, contentW: 0, tabX: 779, tabW: 101, scrollLeft: 0},
};

/* THE ORDERING WORLD. @codex-2: the badge-width arm was source-only, so
 * MOVING the reveal above the line that widens the tab passes a grep and
 * reinstates the bug. This runs the REAL navBadge against a tab whose WIDTH IS
 * A FUNCTION OF ITS BADGE TEXT, so a reveal computed before the text lands
 * measures the old width and leaves the tab hidden — which is the defect. */
function badgeWorld({stripLeft, stripW, contentW, tabX, baseW, scrollLeft}) {
  const badge = {textContent: "", title: "", classList: {toggle() {}}};
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
    querySelector: sel => (sel === ".navtab.on" ? tab : null),
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
  navBadge("ledger", "99+", false, "t");   // widens the ACTIVE tab
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
/* ============ THE BOOT, EXECUTED, WITH REAL SELECTION ============
 * @codex-2 twice: (1) a source check cannot bind reachability, so the boot is
 * RUN; (2) my first run PRE-SELECTED the tab — classList.contains hardcoded
 * true and $$ stubbed to [] — so showView could not perform the selection and
 * mutating its toggle line changed nothing.
 *
 * So nothing here is pre-satisfied. Every tab carries REAL on-state, $$ returns
 * the REAL list, and the strip resolves .navtab.on by asking the tabs. showView
 * must actually select for the reveal to have anything to find. */
{
  const TABW = {helm: 68, quota: 74, boxes: 70, sessions: 92, configs: 84,
                work: 62, chat: 62, roster: 76, ledger: 101};
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
  global.VIEWS = ["helm", "quota", "boxes", "sessions", "configs", "work",
                  "chat", "roster", "ledger"];
  global.$$ = sel => (sel === ".navtab" ? tabs : []);   // the REAL list
  global.$ = () => null;
  global.location = {hash: "#ledger", search: ""};
  global.history = {replaceState() {}};
  global.localStorage = {getItem: () => null, setItem() {}};
  global.setTimeout = () => 0;
  global.setInterval = () => 0;
  for (const k of ["initQuota", "initStorage", "initSessions", "cfgInit",
                   "srevInit", "odqInit", "initChat", "initRoster",
                   "initLedger", "pollDregg", "esConnect", "openSession"]) {
    global[k] = () => {};
  }

  const anySelectedBefore = tabs.some(t => t._on);
  __runBoot();
  const sel = tabs.find(t => t._on);
  const ledger = tabs.find(t => t.dataset.v === "ledger");
  out.boot_reveals = {
    anySelectedBefore,
    selected: sel ? sel.dataset.v : null,
    visible: sel ? visible(STRIP, sel) : false,
    scrollLeft: STRIP.scrollLeft,
    ledgerWasOffscreen: 14 + ledger._x + ledger._w > 376,
  };
}

console.log(JSON.stringify(out));
