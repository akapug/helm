/* Runtime contract for the real scheduler renderer functions. */
const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
function lrAgo(s) {
  if (s === null || s === undefined || s === "" || isNaN(Number(s))) return "unknown";
  const n = Math.max(0, Math.round(Number(s)));
  return n < 60 ? n + "s ago" : n < 3600 ? Math.round(n / 60) + "m ago"
    : n < 86400 ? Math.round(n / 3600) + "h ago" : Math.round(n / 86400) + "d ago";
}
/*__INJECT__*/

const group = (label, rows, suc = {stalled: 0, unmeasurable: 0, contrary: 0, total: 0}) => ({
  id: "holder-" + label, holder_role: label.split(" @")[0], label,
  count: rows.length, dropped: 0, oldest_age_s: 7200, suc, rows
});
const row = (id, more = {}) => Object.assign({id, chain_root: id, supersedes: null,
  title: "lane " + id, plain_title: "lane " + id, state: "OPEN", stage_class: "open",
  owed_by: "integrator", owed_by_present: true, owed_by_known: true,
  holder_role: "integrator", holder_seat: null,
  age_s: 3600, age_known: true, stalled: false, unmeasurable: false,
  contrary: false, honored: false, active: true}, more);
const healthy = {read_age_s: 12, ledger_age_s: 40, scheduler: {
  unavailable: null, row_count: 3, active_count: 3,
  suc: {stalled: 1, unmeasurable: 1, contrary: 1, total: 3, zero: false},
  owner_asks_unavailable: null, owner_ask_count: 2, owner_asks_dropped: 0,
  owner_holds: [], owner_hold_count: 0,
  owner_asks: [{id: "owner-ask-0", owner_ask: true, title: "relogin",
    plain_title: "relogin", detail: "device auth expired", hold_kind: "usage-reset",
    age_s: 86400, age_known: true},
    {id: "owner-ask-1", owner_ask: true, title: "attach drive",
     plain_title: "attach drive", detail: "physical input", hold_kind: "owner-input",
     age_s: 3600, age_known: true}],
  groups: [
    group("integrator", [row("new", {supersedes: "old", contrary: true})],
      {stalled: 0, unmeasurable: 0, contrary: 1, total: 1}),
    group("reviewer @codex", [row("review", {holder_role: "reviewer", holder_seat: "codex",
      owed_by: "reviewer", unmeasurable: true})],
      {stalled: 0, unmeasurable: 1, contrary: 0, total: 1}),
    group("author @kimi", [row("fix", {holder_role: "author", holder_seat: "kimi",
      owed_by: "author", stalled: true, age_s: null, age_known: false})],
      {stalled: 1, unmeasurable: 0, contrary: 0, total: 1}),
  ], edges: [
    {kind: "waits_on_owner_usage_reset", from: "owner-ask-0", to: "owner"},
    {kind: "waits_on_owner_owner_input", from: "owner-ask-1", to: "owner"},
    {kind: "waits_on", from: "new", to: "holder-integrator", stage_class: "open"},
    {kind: "supersedes", from: "new", to: "old", target_known: false},
    {kind: "waits_on", from: "review", to: "holder-reviewer @codex", stage_class: "open"},
    {kind: "waits_on", from: "fix", to: "holder-author @kimi", stage_class: "open"}
  ]
}};
const html = schedulerHTML(healthy);
const unknown = schedulerHTML({unavailable: "permission denied"});
const unknownWithAsk = schedulerHTML(Object.assign({}, healthy,
  {unavailable: "permission denied"}));
const missing = schedulerHTML({read_age_s: 1, ledger_age_s: 2});
const zero = schedulerHTML({read_age_s: 1, ledger_age_s: 2, scheduler: {
  unavailable: null, row_count: 0, active_count: 0,
  suc: {stalled: 0, unmeasurable: 0, contrary: 0, total: 0, zero: true},
  owner_asks: [], owner_ask_count: 0, owner_asks_dropped: 0,
  owner_asks_unavailable: null, owner_holds: [], owner_hold_count: 0,
  groups: [], edges: []
}});
const owner = {innerHTML: "", querySelectorAll: () => []};
global.$ = sel => sel === "#downer" ? owner : null;
dashOwner({read_age_s: 3, scheduler: {owner_asks: [], owner_ask_count: 0,
           owner_asks_unavailable: null, owner_holds: [], unavailable: "ledger failed"}},
           false, false);
const ownerEmptyUnreadable = owner.innerHTML;
dashOwner({read_age_s: 3, scheduler: healthy.scheduler, unmeasurable: []}, false, false);
const ownerKnownUnreadable = owner.innerHTML;
const capped = JSON.parse(JSON.stringify(healthy.scheduler));
capped.owner_ask_count = 10;
capped.owner_asks_dropped = 8;
dashOwner({read_age_s: 3, scheduler: capped}, false, true);
const ownerCapped = owner.innerHTML;
const allCapped = JSON.parse(JSON.stringify(healthy.scheduler));
allCapped.owner_asks = [];
allCapped.owner_ask_count = 3;
allCapped.owner_asks_dropped = 3;
const allCappedHTML = schedulerHTML({read_age_s: 1, ledger_age_s: 2,
  scheduler: allCapped});
dashOwner({read_age_s: 3, scheduler: allCapped}, false, true);
const ownerAllCapped = owner.innerHTML;
const ownerHold = row("owner-hold", {holder_role: "owner", owed_by: "reviewer",
  detail: "choose publication", age_s: 7200});
dashOwner({read_age_s: 3, scheduler: {owner_asks: [], owner_ask_count: 0,
  owner_asks_unavailable: null, owner_holds: [ownerHold], unavailable: null}}, false, true);
const ownerHeld = owner.innerHTML;
console.log(JSON.stringify([{name: "assembled_scheduler_contract", detail: {
  hasAllGroups: ["integrator", "reviewer @codex", "author @kimi"].every(x => html.includes(x)),
  ownerBeforeGraph: html.indexOf("waits on owner first") < html.indexOf("schgraph"),
  hasSUC: html.includes("S 1 · U 1 · C 1"),
  hasBlocking: html.includes("data-edge-to=\"holder-integrator\""),
  hasSupersession: html.includes("supersedes old") && html.includes("predecessor not in this graph"),
  hasAges: html.includes("1h ago") && html.includes("age unknown"),
  hasFreshness: html.includes("ledger read 12s ago · newest ledger write 40s ago"),
  hasListDoor: html.includes("the kanban above, row by row"),
  usageResetEdge: html.includes("usage reset ←")
    && html.includes('data-edge-kind="waits_on_owner_usage_reset"'),
  ordinaryOwnerEdge: html.includes("owner ←")
    && html.includes('data-edge-kind="waits_on_owner_owner_input"'),
  unavailableLoud: unknown.includes("scheduler UNKNOWN") && !unknown.includes("no non-terminal"),
  unavailableKeepsAsk: unknownWithAsk.includes("relogin")
    && unknownWithAsk.indexOf("relogin") < unknownWithAsk.indexOf("scheduler UNKNOWN"),
  missingLoud: missing.includes("SCHEDULER NOT SENT — UNKNOWN"),
  emptyMeasured: zero.includes("no non-terminal land-request rows") && zero.includes("zero SUC"),
  ownerEmptyUnreadableLoud: ownerEmptyUnreadable.includes("pipeline UNKNOWN")
    && !ownerEmptyUnreadable.includes("no fleet-filed owner asks"),
  ownerKnownUnreadableVisible: ownerKnownUnreadable.includes("2 OWNER ASKS")
    && ownerKnownUnreadable.includes("relogin"),
  ownerCapDisclosed: ownerCapped.includes("10 OWNER ASKS")
    && ownerCapped.includes("+8 more not shown"),
  allCappedDisclosed: allCappedHTML.includes("3 actionable asks")
    && allCappedHTML.includes("and 3 more owner asks")
    && ownerAllCapped.includes("3 OWNER ASKS")
    && ownerAllCapped.includes("all 3 details capped"),
  canonicalOwnerHold: ownerHeld.includes("1 LR HOLD ON OWNER")
    && ownerHeld.includes("choose publication")
}}]));
