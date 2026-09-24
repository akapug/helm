/* Runtime contract for the ledger's delta-DOM renderer.
 * Actual production functions are spliced at the marker by the Python test. */

let NOW = 2000000000000;
Date.now = () => NOW;

class Probe {
  constructor(dataset = {}, text = "", className = "", title = "", children = {}) {
    this.dataset = dataset;
    this._text = text;
    this._className = className;
    this._title = title;
    this.children = children;
    this.writes = 0;
  }
  get textContent() { return this._text; }
  set textContent(v) { this._text = String(v); this.writes++; }
  get className() { return this._className; }
  set className(v) { this._className = String(v); this.writes++; }
  get title() { return this._title; }
  set title(v) { this._title = String(v); this.writes++; }
  querySelector(sel) { return this.children[sel] || null; }
}
class El {
  constructor(id) { this.id = id; this.writes = 0; this.html = ""; this.nodes = {}; }
  set innerHTML(v) {
    this.writes++;
    this.html = String(v);
    const all = (re, make) => Array.from(this.html.matchAll(re), make);
    const pulses = all(/<span class="lpulse" data-times="([^"]+)"><span class="mono lpulsebars" title="([^"]*)">([^<]*)<\/span>[\s\S]*?over <span class="lpulsespan">([^<]*)<\/span><\/span>/g, m => {
      const bars = new Probe({}, m[3], "mono lpulsebars", m[2]);
      const span = new Probe({}, m[4], "lpulsespan");
      return new Probe({times: m[1]}, "", "lpulse", "", {".lpulsebars": bars, ".lpulsespan": span});
    });
    const states = all(/<div class="lhead lstate" data-ts="([^"]+)"><span class="ldot([^"]*)"><\/span>[\s\S]*?<span class="lbadge([^"]*)">([^<]*)<\/span>/g, m => {
      const dot = new Probe({}, "", "ldot" + m[2]);
      const badge = new Probe({}, m[4], "lbadge" + m[3]);
      return new Probe({ts: m[1]}, "", "lstate", "", {".ldot": dot, ".lbadge": badge});
    });
    this.nodes = {
      ages: all(/class="lage" data-ts="([^"]+)"/g, m => new Probe({ts: m[1]})),
      pulses,
      states
    };
  }
  get innerHTML() { return this.html; }
  querySelector(sel) {
    return sel === ".lpulse[data-times]" ? (this.nodes.pulses || [])[0] || null : null;
  }
  querySelectorAll(sel) {
    return {
      ".lage[data-ts]": this.nodes.ages,
      ".lstate[data-ts]": this.nodes.states
    }[sel] || [];
  }
}

const ids = ["nativehead", "nativechat", "ledgernative", "ledgerstrip",
  "ledgerturns", "cavepulse", "ledgercells"];
const els = Object.fromEntries(ids.map(id => [id, new El(id)]));
function $(sel) { return els[sel.slice(1)]; }
function esc(s) {
  return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
const lsh = h => h ? String(h).slice(0, 10) + "…" : "—";
const lago = t => {
  if (!t) return "—";
  const s = Math.max(0, Date.now() / 1e3 - t);
  return s < 60 ? Math.round(s) + "s ago" : s < 3600 ? Math.round(s / 60) + "m ago"
    : s < 86400 ? Math.round(s / 3600) + "h ago" : Math.round(s / 86400) + "d ago";
};
let LEDGER_SIGNED = true, LEDGER_HEAD = 3, NATIVE_COUNT = 1;
let LEDGER_NAMES = {cell1: "codex"};

/*__INJECT__*/

const native = {
  chain: {count: 1, verified: true, head_index: 1, records: [{
    ts: "2033-05-18T03:33:08Z", chain_index: 1, rec_hash: "a".repeat(64),
    op: "create", premise_id: "p1", project: "helm"
  }]},
  events: [{ts: "2033-05-18T03:33:07Z", verb: "store", target: "p1", summary: "kept"}],
  chat: {msgs: 2, rooms: 1, last_ts: "2033-05-18T03:33:09Z", last_from: "codex", last_room: "helm"}
};
const turn = (chain_index, timestamp) => ({chain_index, timestamp,
  turn_hash: String(chain_index).repeat(64), receipt_hash: "c".repeat(64),
  agent: "cell1", action_count: 1, computrons_used: 2});
const signed = {
  node: "http://ledger", status: {healthy: true, latest_height: 3, dag_height: 3,
    state_producer: "lean", lean_producer: true, federation_mode: "solo", peer_count: 0},
  transport: {mode: "signed"},
  turns: [turn(3, 1999999988), turn(2, 1999999950), turn(1, 1999999900)],
  cells: [{id: "cell1", seat: "codex", last_turn_ts: 1999999882,
    recent_turns: 1, nonce: 2, balance: 3}]
};

function render() {
  nativeHead(native.chain); nativeChat(native.chat); nativeRows(native);
  ledgerStrip(signed); ledgerTurns(signed.turns);
  const pulse = $("#cavepulse");
  ledgerHTML(pulse, cavePulse(signed.turns, LEDGER_HEAD));
  cavePulseClock(pulse);
  ledgerCells(signed.cells);
}
function totalWrites() { return ids.reduce((n, id) => n + els[id].writes, 0); }
function nodes(kind) { return ids.flatMap(id => els[id].nodes[kind] || []); }
function probeWrites(p) { return p.writes + Object.values(p.children).reduce((n, c) => n + probeWrites(c), 0); }
function nodeWrites(kind) { return nodes(kind).reduce((n, x) => n + probeWrites(x), 0); }
function ageTexts() { return nodes("ages").map(a => a.textContent); }
function pulseView() {
  const p = nodes("pulses")[0], bars = p.querySelector(".lpulsebars"), span = p.querySelector(".lpulsespan");
  return {bars: bars.textContent, title: bars.title, span: span.textContent};
}
function stateView() {
  const s = nodes("states")[0];
  if (!s) return {};
  const dot = s.querySelector(".ldot"), badge = s.querySelector(".lbadge");
  return {dot: dot.className, badge: badge.className, label: badge.textContent};
}
const groups = ["ages", "pulses", "states"];
const writeSnapshot = () => Object.fromEntries(groups.map(k => [k, nodeWrites(k)]));

render();
const firstWrites = totalWrites();
const firstTexts = ageTexts();
const firstDynamicWrites = writeSnapshot();
const firstPulse = pulseView(), firstState = stateView();
NOW += 60000;
render();
const secondWrites = totalWrites() - firstWrites;
const secondTexts = ageTexts();
const secondDynamicWrites = Object.fromEntries(groups.map(k => [k, nodeWrites(k) - firstDynamicWrites[k]]));
const secondPulse = pulseView(), secondState = stateView();
native.chain.count = 2;
nativeHead(native.chain);
const changedWrites = totalWrites() - firstWrites - secondWrites;

const degraded = {...signed, transport: {mode: "degraded", profile: "p", reason: "offline",
  remediation: "repair", first_failure: "first", last_failure: new Date(NOW - 12000).toISOString(),
  last_age_s: 12}};
const stripWrites = els.ledgerstrip.writes;
ledgerStrip(degraded);
const degradedFirstWrites = els.ledgerstrip.writes - stripWrites;
const degradedAgeBefore = els.ledgerstrip.nodes.ages[0].textContent;
degraded.transport.last_age_s = 999;
NOW += 3000;
ledgerStrip(degraded);
const degradedSecondWrites = els.ledgerstrip.writes - stripWrites - degradedFirstWrites;
const degradedAgeAfter = els.ledgerstrip.nodes.ages[0].textContent;

const detail = {firstWrites, secondWrites, changedWrites, firstTexts, secondTexts,
  firstDynamicWrites, secondDynamicWrites, firstPulse, secondPulse, firstState, secondState,
  degradedFirstWrites, degradedSecondWrites, degradedAgeBefore, degradedAgeAfter,
  panelWrites: Object.fromEntries(ids.map(id => [id, els[id].writes]))};
console.log(JSON.stringify([{name: "unchanged_poll_keeps_rows_and_refreshes_ages", detail}]));
