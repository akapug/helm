/* Client-runtime harness for the four nav areas: Work · Chat · Fleet ·
 * History.
 *
 * The owner merged the board, the work tab and the scheduler into one Work
 * page and made History the signing ledger alone; only Fleet keeps a section
 * row. The load-bearing claim of that change is NEGATIVE — every old hash
 * still lands on the page that took it over — and a negative claim about a
 * ROUTER cannot be greped: `if (false && VIEWS.includes(v)) showView(v)` keeps
 * every literal a source check looks for and routes nothing.
 *
 * So the real router runs. tests/test_web_areas.py splices the ACTUAL
 * revealActiveTab / navBadge / areaBadge / canonView / goOldSection /
 * showView out of the assembled UI and the ACTUAL boot block as __runBoot, and
 * it builds the DOM below FROM THE ASSEMBLED NAV MARKUP — the areas, the
 * sections and the view panel ids are parsed out of the page, never listed
 * here. A fixture that invented its own tabs would prove nothing about the
 * shipped nav.
 *
 * Geometry is NOT this harness's subject: the strip reports scrollWidth ==
 * clientWidth, the shape of a row that fits, so revealActiveTab returns at its
 * guard. tests/navreveal_runtime_harness.js owns the scrolling cases.
 *
 * Plain CommonJS (non-strict) so the spliced declarations share module scope. */

const DOM = JSON.parse(process.env.HELM_NAV_DOM);   // parsed from the real page

function classList(el) {
  return {
    contains: c => el.classes.has(c),
    add: c => el.classes.add(c),
    remove: c => el.classes.delete(c),
    toggle: (c, val) => {
      const want = val === undefined ? !el.classes.has(c) : !!val;
      if (want) el.classes.add(c); else el.classes.delete(c);
      return want;
    },
  };
}

const ALL = [];
const SCROLLED = [];              // the ids scrollIntoView was called on
function el(tag, classes, dataset, id) {
  const node = {tag, classes: new Set(classes), dataset: dataset || {},
                id: id || "", children: [], title: "", open: false};
  node.classList = classList(node);
  node.appendChild = c => { node.children.push(c); c.parent = node; };
  node.remove = () => {
    if (!node.parent) return;
    node.parent.children = node.parent.children.filter(c => c !== node);
    const i = ALL.indexOf(node);
    if (i >= 0) ALL.splice(i, 1);
    node.parent = null;
  };
  node.querySelector = sel => descendants(node).find(c => matches(c, sel)) || null;
  /* className is a real property of a real element and BOTH badge writers set
     the class that way. A stub without it lets a badge be created and then be
     unfindable by .navbadge, which reads as "no badge was written" — the first
     run of this harness reported exactly that. */
  Object.defineProperty(node, "className", {
    get: () => [...node.classes].join(" "),
    set: v => { node.classes = new Set(String(v).split(/\s+/).filter(Boolean));
                node.classList = classList(node); },
  });
  /* textContent COERCES, because a real element's does: both badge writers
     assign a raw number (`el.textContent = val`) and a browser stores "7".
     A stub that kept the Number made every badge arm read 7 where the page
     shows "7" — measured on the first green run of this harness, where the
     production code was right and the fixture was the thing that lied. */
  let text = "";
  Object.defineProperty(node, "textContent", {
    get: () => text,
    set: v => { text = String(v); },
  });
  node.getBoundingClientRect = () => ({left: 0, right: 10});
  node.scrollIntoView = () => { SCROLLED.push(node.id); };
  ALL.push(node);
  return node;
}
function descendants(node) {
  const out = [];
  for (const c of node.children) { out.push(c); out.push(...descendants(c)); }
  return out;
}
/* Enough selector to answer exactly what the nav code asks: "#id", ".a.b" and
 * ".a[data-x=\"v\"]". Anything else throws rather than silently matching
 * nothing — a matcher that answers "no" to a selector it does not understand
 * would turn every arm below into a test of this file. */
function matches(node, sel) {
  if (sel[0] === "#") return node.id === sel.slice(1);
  const m = /^((?:\.[A-Za-z0-9_-]+)+)(?:\[data-([a-z]+)="([^"]+)"\])?$/.exec(sel);
  if (!m) throw new Error("harness selector not understood: " + sel);
  for (const c of m[1].slice(1).split(".")) if (!node.classes.has(c)) return false;
  if (m[2] && node.dataset[m[2]] !== m[3]) return false;
  return true;
}

const NAV = el("nav", [], {}, "nav");
const STRIP = el("div", ["navtabs"], {});
STRIP.scrollWidth = 100;      // a row that fits: revealActiveTab returns at its guard
STRIP.clientWidth = 100;
NAV.appendChild(STRIP);
const AREAS_ROW = el("div", ["navareas"], {});
NAV.appendChild(AREAS_ROW);
const ROOT = el("body", [], {});
ROOT.appendChild(NAV);

const AREA_BTN = {}, SECTION = {}, TAB = {}, PANEL = {}, ANCHOR = {};
for (const a of DOM.areas) {
  const b = el("button", ["navarea"], {a: a.key});
  b.textContent = a.label;
  AREA_BTN[a.key] = b;
  AREAS_ROW.appendChild(b);
  // AN AREA OF ONE PAGE HAS NO SECTION ROW IN THE MARKUP, so none is built
  if (!a.group) continue;
  const sec = el("div", ["navsec"], {a: a.key});
  SECTION[a.key] = sec;
  STRIP.appendChild(sec);
  for (const s of a.sections) {
    const t = el("button", ["navtab"], {v: s.view});
    t.textContent = s.label;
    TAB[s.view] = t;
    sec.appendChild(t);
  }
}
for (const id of DOM.panels) {
  const p = el("div", ["view"], {}, id);
  PANEL[id] = p;
  ROOT.appendChild(p);
}
for (const id of DOM.anchors) {
  const a = el("details", [], {}, id);
  ANCHOR[id] = a;
  PANEL["view-work"].appendChild(a);
}

let CREATED = null;
global.document = {
  querySelector: sel => ALL.find(n => matches(n, sel)) || null,
  querySelectorAll: sel => ALL.filter(n => matches(n, sel)),
  createElement: tag => (CREATED = el(tag, [], {})),
};
global.$ = s => document.querySelector(s);
global.$$ = s => [...document.querySelectorAll(s)];

let HASH = "";
global.location = {search: "", get hash() { return HASH; }, set hash(v) { HASH = v; }};
global.history = {replaceState(_a, _b, h) { HASH = h; }};
const STORE = {};
global.localStorage = {getItem: k => (k in STORE ? STORE[k] : null),
                       setItem: (k, v) => { STORE[k] = String(v); }};
global.setTimeout = () => 0;
global.setInterval = () => 0;
global.VIEWS = DOM.views;
global.AREA_OF = DOM.area_of;
global.AREA_LAST = {};
global.NAV_BADGE = {};
global.LR_LAST = null;
/* the Work page's two queue reads are COUNTED: coming back to Work refreshes
 * them, and the boot must not add a second fetch to the ones load fires */
let QUEUE_READS = 0;
global.odqInit = () => { QUEUE_READS++; };
for (const k of ["initQuota", "initStorage", "initSessions", "cfgInit",
                 "srevInit", "tqInit", "initChat", "initRoster",
                 "initLedger", "startDreggPolling", "stopDreggPolling",
                 "esConnect", "openSession", "schedulerShow",
                 "boardReload"]) {
  global[k] = () => {};
}

/* GEOMETRY IS NOT THIS HARNESS'S SUBJECT (see the header). showView publishes
   the navigation's measured height for whatever sticks below it, which is a real
   browser measurement — it is measured by the Chrome arms in
   tests/test_web_home_geometry.py — so here it is a declared no-op rather than a
   fake rect this harness would then be tempted to assert about. */
function publishNavHeight() { return null; }

/*__INJECT__*/
/*__BOOT__*/
/* the REAL area-click wiring, lifted verbatim: a handler this file
 * reimplemented would be this file's opinion of the nav, not the nav. */
/*__WIRE__*/

function snapshot() {
  const on = cls => ALL.filter(n => n.classes.has(cls) && n.classes.has("on"));
  const panel = on("view");
  const tab = on("navtab");
  const area = on("navarea");
  const sec = on("navsec");
  return {
    panel: panel.length === 1 ? panel[0].id : null,
    panels_on: panel.length,
    tab: tab.length === 1 ? tab[0].dataset.v : null,
    tabs_on: tab.length,
    area: area.length === 1 ? area[0].dataset.a : null,
    areas_on: area.length,
    section_open: sec.length === 1 ? sec[0].dataset.a : null,
    sections_on: sec.length,
    solo: STRIP.classes.has("solo"),
    scrolled: SCROLLED.slice(),
    opened: Object.values(ANCHOR).some(a => a.open),
    queue_reads: QUEUE_READS,
    hash: HASH,
    saved: STORE["helm.view"] || null,
  };
}
/* A FRESH PAGE, not just a deselected one. AREA_LAST and NAV_BADGE survive a
 * class reset, and the routing loop above walks every area — so without this
 * the "an area opens its FIRST section" case would silently become "reopens the
 * section the loop happened to leave", which is what the first run measured. */
function clear() {
  for (const n of ALL) n.classes.delete("on");
  STRIP.classes.delete("solo");
  SCROLLED.length = 0;
  QUEUE_READS = 0;
  showView.cur = undefined;           // a fresh page has shown nothing yet
  for (const a of Object.values(ANCHOR)) a.open = false;
  for (const k of Object.keys(AREA_LAST)) delete AREA_LAST[k];
  for (const k of Object.keys(NAV_BADGE)) delete NAV_BADGE[k];
  for (const b of Object.values(AREA_BTN)) {
    const badge = b.querySelector(".navbadge");
    if (badge) badge.remove();
  }
  for (const t of Object.values(TAB)) {
    const badge = t.querySelector(".navbadge");
    if (badge) badge.remove();
  }
  delete STORE["helm.view"];
  HASH = "";
}
function badgeOf(node) {
  const b = node.querySelector(".navbadge");
  return b ? {text: b.textContent, mention: b.classes.has("mention"),
              title: b.title} : null;
}

const out = {built: {tabs: Object.keys(TAB).length,
                     panels: Object.keys(PANEL).length,
                     areas: Object.keys(AREA_BTN).length,
                     sections: Object.keys(SECTION).length}};

/* EVERY OLD HASH ROUTE, THROUGH THE REAL BOOT. Each case starts from a world
 * where NOTHING is selected, so the boot must do the selecting for the
 * snapshot to say anything at all. */
out.routes = {};
for (const h of DOM.hashes) {
  clear();
  HASH = "#" + h;
  __runBoot();
  out.routes[h] = snapshot();
}

/* THE MUST-MISS on that loop: a hash no view answers to must select NOTHING.
 * Without it, a snapshot that reported a constant "view-work" — or a boot that
 * selected a default regardless of the hash — would satisfy every arm above. */
clear();
HASH = "#bogus-not-a-view";
__runBoot();
out.bogus = snapshot();

/* AN AREA CLICK OPENS ITS FIRST SECTION, THEN THE ONE YOU LEFT. */
clear();
HASH = "#" + DOM.hashes[0];
__runBoot();
out.click_seq = [];
for (const step of DOM.click_sequence) {
  if (step.view) showView(step.view);
  else AREA_BTN[step.area].onclick();
  out.click_seq.push(Object.assign({step: step.view || step.area}, snapshot()));
}

/* A SINGLE PAGE'S BADGE: Chat and Work have no tab to carry one, so the area
 * button is where it shows. */
clear();
showView("history");
navBadge("chat", 3, true, "three unread");
navBadge("work", "?", true, "the land pipeline is unreadable");
out.badge_solo = {chat: badgeOf(AREA_BTN.chat), work: badgeOf(AREA_BTN.work)};
navBadge("work", 0, false, "");

/* THE ROLL-UP: a badge on a section in a CLOSED area must still be visible, so
 * it lands on the area button too. Work is open here, so Fleet's is hidden. */
showView("work");
navBadge("roster", 2, false, "two");
out.badge_closed_area = {area: badgeOf(AREA_BTN.fleet),
                         section: badgeOf(TAB.roster),
                         section_visible: SECTION.fleet.classes.has("on")};
navBadge("sessions", 5, false, "five");
out.badge_sums = {area: badgeOf(AREA_BTN.fleet)};
navBadge("roster", "?", true, "unreadable");
out.badge_unknown_wins = {area: badgeOf(AREA_BTN.fleet)};
navBadge("roster", 0, false, "");
navBadge("sessions", 0, false, "");
out.badge_cleared = {area: badgeOf(AREA_BTN.fleet),
                     chat_still: badgeOf(AREA_BTN.chat)};
navBadge("chat", 0, false, "");
out.badge_all_cleared = {chat: badgeOf(AREA_BTN.chat)};

console.log(JSON.stringify(out));
