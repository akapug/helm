/* COUNT-WORD harness for the finding-4 seat-count unification (console walk 3,
   task/3723). The seats that are active right now (a beat inside 2 minutes,
   not unverified, not an ephemeral done SA, not the owner's own row) are
   counted on THREE surfaces: Home's seats tile (`homeSeats`), the Seats
   page's "on now" strip (`dashFleet`) and its roster count (`#rostercount` in
   `renderRoster`). The joined seats (every non-ephemeral seat, gone ones
   included) are counted twice: `#rostercount` again and the "message a seat"
   picker's `#seatpickcount` (`seatPicker`). Finding 4 is one word per count,
   so one payload is fed to every renderer here and each count is read against
   its siblings: one number, one word. These run the REAL functions lifted
   verbatim from the assembled web UI under node with a minimal DOM. Runs as
   plain CommonJS (non-strict) so the lifted function bodies close over the
   shims below; this world supplies only the module-local consts the lifted
   bodies read (rrank, RCOLS, DASH_FLEET_TS) and the DOM elements they touch. */

// ───────────────────────── minimal DOM ─────────────────────────
class El {
  constructor(tag) { this.tag = tag; this.attributes = {}; this.children = [];
    this._html = ""; this._text = ""; this.value = ""; }
  get classList() {
    const cls = (this.attributes.class || "").split(/\s+/).filter(Boolean);
    return {contains: c => cls.indexOf(c) !== -1};
  }
  set innerHTML(v) { this._html = v; }
  get innerHTML() { return this._html; }
  set textContent(v) { this._text = v; }
  get textContent() { return this._text; }
  querySelectorAll() { return []; }
}
const DOM = {};
function $(sel) {
  if (!DOM[sel]) DOM[sel] = new El("div");
  return DOM[sel];
}

// ───────────────────────── module state the lifted bodies close over ─────────────────────────
// renderRoster reads these at call time, so a definition here is enough
let ROSTER_SORT = {key: "presence", dir: 1};
let ROSTER_SHOW = {absent: false, sas: false};
let LAST_ROSTER = null;
// dashFleet stamps the strip's render time here (00-core.js.part)
let DASH_FLEET_TS = 0;
// rrank + RCOLS are module-local consts of 80-roster.js.part; only the sort
// comparator and the rthead build touch them on the empty payload path.
const rrank = p => p === "fresh" ? 0 : p === "unverified" ? 1 : p === "quiet" ? 2 : 3;
const RCOLS = [
  {k: "presence", l: "", c: ""}, {k: "seat", l: "seat", c: ""},
  {k: "last_seen", l: "last", c: "num"}, {k: "doing", l: "doing", c: ""},
  {k: "task", l: "task", c: ""}, {k: "pending", l: "pend", c: "num"},
  {k: "preview", l: "last msg", c: ""}, {k: "project", l: "project", c: ""},
  {k: "session", l: "sess", c: ""},
];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
// The roster row cells render through these helpers, which do not touch the
// `#rostercount` / `#seatpickcount` elements this harness asserts. Faithful
// stubs (one representative value each) keep the row map live without lifting
// the full shared-script dependency graph: the count assignment is the subject,
// not the doing-line.
function rosterDoing(s) { return {cls: "home", text: (s.home_room ? "in #" + s.home_room : "all rooms")}; }
function rosterTask(s) { return (s.todo && s.todo.active) || ""; }
function seatRuntime(s) { return "1h"; }
function presenceLabel(p) { return p; }
function chatShort(s) { return s; }
function chatAgo(s) { return "1m"; }
function rosterListedMark(c) { return c.listed ? "" : "NOT REPORTED"; }
function rdotGlyph(p) { return p; }
function lago(s) { return "1m ago"; }
function upstreamBadge(s) { return ""; }
function memBadge(s) { return ""; }

/*__INJECT__*/

// ───────────────────────── scenarios ─────────────────────────
function scenario_mixed_and_owner() {
  const rep = {seats: [
    {seat: "a", presence: "fresh"},
    {seat: "b", presence: "fresh"},
    {seat: "c", presence: "absent"},
    {seat: "e", presence: "fresh", ephemeral: true},
    {seat: "owner", presence: "fresh", owner: true},
  ]};
  renderRoster(rep);
  seatPicker(rep);
  const roster = DOM["#rostercount"].innerHTML;
  const picker = DOM["#seatpickcount"].textContent;
  return {name: "mixed_and_owner", roster, picker};
}

function scenario_empty() {
  const rep = {seats: []};
  renderRoster(rep);
  seatPicker(rep);
  const roster = DOM["#rostercount"].innerHTML;
  const picker = DOM["#seatpickcount"].textContent;
  return {name: "empty", roster, picker};
}

// ONE presence payload fed to every surface that counts it, carrying each
// bucket the strip names: a and b active (b walled off by its vendor), q
// quiet, u unverified, g gone, an ephemeral done SA and the owner's own row.
function scenario_presence_mix() {
  const seats = [
    {seat: "a", presence: "fresh"},
    {seat: "b", presence: "fresh", availability: "UNAVAILABLE"},
    {seat: "q", presence: "quiet"},
    {seat: "u", presence: "unverified", unverified: ["a"]},
    {seat: "g", presence: "absent"},
    {seat: "e", presence: "fresh", ephemeral: true},
    {seat: "owner", presence: "fresh", owner: true},
  ];
  const home = homeSeats(seats, null, false);
  dashFleet(seats);
  renderRoster({seats});
  seatPicker({seats});
  return {name: "presence_mix", home, fleet: DOM["#dfleet"].innerHTML,
          roster: DOM["#rostercount"].innerHTML,
          picker: DOM["#seatpickcount"].textContent};
}

(async () => {
  const results = [];
  results.push(scenario_mixed_and_owner());
  results.push(scenario_empty());
  results.push(scenario_presence_mix());
  process.stdout.write(JSON.stringify(results));
  process.exit(0);
})();
