/* Client-runtime harness for the helm chat panel (brick #4 round 2).
 *
 * Round 1's tests asserted only the SERVER's /api/chat shape, so three
 * CLIENT-JS ordering defects slipped through. This harness runs the ACTUAL
 * pollChat / chatHydrateParents / chatLoadOlder / chatResetLog source lifted
 * verbatim from the assembled web UI against a faithful fake server (mirrors
 * web.py's since/win/before/ids read shapes) and a minimal DOM, so a
 * regression in the STATEMENT ORDER — not just the API — fails a test.
 *
 * The real function sources are spliced in by tests/test_web_chat_client_runtime.py
 * at the /*__INJECT__*(/ marker below; everything else here is the shim world
 * those functions close over. Runs as plain CommonJS (non-strict) so the
 * spliced function declarations share this module scope with the shims. */

// ───────────────────────── minimal DOM ─────────────────────────
function parseNodes(html, parent) {
  const stack = [{children: []}];
  const re = /<(\/?)([a-zA-Z0-9]+)((?:\s+[a-zA-Z0-9:_-]+(?:="[^"]*")?)*)\s*(\/?)>/g;
  let m, last = 0;
  while ((m = re.exec(html))) {
    if (m.index > last) {
      const t = html.slice(last, m.index);
      if (t) stack[stack.length - 1].children.push(t);
    }
    last = re.lastIndex;
    const closing = m[1] === "/", tag = m[2], attrsStr = m[3], selfClose = m[4] === "/";
    if (closing) { if (stack.length > 1) stack.pop(); continue; }
    const el = new El(tag);
    const ar = /([a-zA-Z0-9:_-]+)(?:="([^"]*)")?/g;
    let a;
    while ((a = ar.exec(attrsStr))) { if (a[1]) el.attributes[a[1]] = a[2] === undefined ? "" : a[2]; }
    el.parent = stack[stack.length - 1];
    stack[stack.length - 1].children.push(el);
    if (!selfClose) stack.push(el);
  }
  if (last < html.length) { const t = html.slice(last); if (t) stack[0].children.push(t); }
  const kids = stack[0].children;
  for (const k of kids) if (k instanceof El) k.parent = parent || null;
  return kids;
}

class El {
  constructor(tag) { this.tag = tag; this.attributes = {}; this.children = []; this.parent = null;
    this.scrollTop = 0; this.clientHeight = 1000; this.scrollHeight = 1000; }
  get classList() {
    const cls = (this.attributes.class || "").split(/\s+/).filter(Boolean);
    return {contains: c => cls.indexOf(c) !== -1};
  }
  getAttribute(n) { return n in this.attributes ? this.attributes[n] : null; }
  setAttribute(n, v) { this.attributes[n] = String(v); }
  get textContent() {
    let s = ""; for (const c of this.children) s += (typeof c === "string") ? c : c.textContent; return s;
  }
  set textContent(v) { this.children = [String(v)]; }
  set innerHTML(html) { this.children = html === "" ? [] : parseNodes(html, this); }
  get innerHTML() { return this.children.map(serialize).join(""); }
  get outerHTML() { return serialize(this); }
  set outerHTML(html) {
    const repl = parseNodes(html, this.parent);
    const p = this.parent; if (!p) return;
    const i = p.children.indexOf(this);
    if (i !== -1) p.children.splice(i, 1, ...repl);
  }
  insertAdjacentHTML(pos, html) {
    const nodes = parseNodes(html, this);
    if (pos === "beforeend") this.children.push(...nodes);
    else if (pos === "afterbegin") this.children.unshift(...nodes);
  }
  querySelector(sel) { const r = this.querySelectorAll(sel); return r.length ? r[0] : null; }
  querySelectorAll(sel) {
    const groups = sel.split(",").map(s => s.trim()).filter(Boolean).map(parseSelector);
    const all = descendants(this), out = [], seen = new Set();
    for (const g of groups) {
      const last = g[g.length - 1];
      for (const el of all) {
        if (!matchCompound(el, last)) continue;
        if (g.length > 1 && !ancestorsMatch(el, g.slice(0, -1))) continue;
        if (!seen.has(el)) { seen.add(el); out.push(el); }
      }
    }
    return out;
  }
}
function serialize(n) {
  if (typeof n === "string") return n;
  let a = ""; for (const k in n.attributes) a += ' ' + k + '="' + n.attributes[k] + '"';
  return "<" + n.tag + a + ">" + n.children.map(serialize).join("") + "</" + n.tag + ">";
}
function descendants(root) {
  const out = []; (function rec(n) { for (const c of n.children) if (c instanceof El) { out.push(c); rec(c); } })(root);
  return out;
}
function parseSelector(s) {
  return s.split(/\s+/).filter(Boolean).map(tok => {
    const comp = {tag: null, classes: [], attrs: []};
    const body = tok.replace(/\[[^\]]*\]/g, m => { // pull attrs first
      const inner = m.slice(1, -1), eq = inner.indexOf("=");
      if (eq === -1) comp.attrs.push({name: inner, val: undefined});
      else comp.attrs.push({name: inner.slice(0, eq), val: inner.slice(eq + 1).replace(/^"|"$/g, "")});
      return "";
    });
    body.split(".").forEach((p, i) => { if (i === 0) { if (p) comp.tag = p; } else if (p) comp.classes.push(p); });
    return comp;
  });
}
function matchCompound(el, comp) {
  if (comp.tag && el.tag !== comp.tag) return false;
  for (const c of comp.classes) if (!el.classList.contains(c)) return false;
  for (const a of comp.attrs) {
    if (!(a.name in el.attributes)) return false;
    if (a.val !== undefined && el.attributes[a.name] !== a.val) return false;
  }
  return true;
}
function ancestorsMatch(el, comps) {
  let idx = comps.length - 1, p = el.parent;
  while (p && idx >= 0) { if (p instanceof El && matchCompound(p, comps[idx])) idx--; p = p.parent; }
  return idx < 0;
}

// ───────────────────────── shims the real code closes over ─────────────────────────
let LOG = new El("div");
const VIEW = new El("div"); VIEW.setAttribute("class", "on"); // #view-chat is "on" => away=false
const document = {hidden: false};
const CSS = {escape: s => String(s)};                         // test keys are plain identifiers
function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
let ANSWERS = new El("div"), FLEET = new El("div");
// the composer's name box: pollChat stamps the server's owner name onto it
let NAMEBOX = {value: "", placeholder: ""};
function $(sel) {
  if (sel === "#chatlog") return LOG;
  if (sel === "#chatname") return NAMEBOX;
  if (sel === "#view-chat") return VIEW;
  if (sel === "#danswers") return ANSWERS;
  if (sel === "#dfleet") return FLEET;
  return null;                                                // #chatjump etc — chatJumpSync is shimmed
}
function toast() {}
function showView() {}
/* THE TWO HOME CARDS THIS POLL FEEDS ARE THE REAL ONES HERE, against real
   elements. They were stubs, and a stub cannot hold the property that matters:
   a FIRST read which never answers must leave a rendered NOT READ rather than a
   hidden card, and the only thing that can end such a read is the DEADLINE the
   poll passes. So `j`, both clocks and the answers card are lifted verbatim and
   the transport below is what stands in for the network.

   `chatSignal` is where production calls `dashAnswers(d)` — one line inside
   `_owner_signal`'s body, which this harness stubs (it renders the nav, the
   title and the notification too). The CALL SITE is pinned by the source arms in
   tests/test_web_lr.py; what this shim adds is that the card is driven by the
   envelope THIS POLL FETCHED rather than by a hand-written body. */
function chatSignal(d) { if (d) dashAnswers(d); }
let DASH_CHAT_FAILED = 0, DASH_CHAT_CADENCE_S = 2;
let DASH_ANSWERS_TS = 0, DASH_FLEET_TS = 0;
const REAL_NOW = Date.now;
const wall = () => Number(REAL_NOW.call(Date));
function resetCards() {
  ANSWERS = new El("div"); FLEET = new El("div");
  DASH_CHAT_FAILED = 0; DASH_ANSWERS_TS = 0; DASH_FLEET_TS = 0;
  DASH_CHAT_CADENCE_S = 2; Date.now = REAL_NOW;
  FETCH_MODE = "now"; FETCH_LATE_MS = 0; FETCH_CALLS.length = 0;
  SERVER.answers = undefined;
}
// THE RENDER BOUNDARY IS THE OBSERVABLE, NOT EVENTUAL MODULE STATE. A reviewer
// showed the previous arm was ORDER-BLIND: reading CHAT_DM_STATE after
// pollChat() returns proves the assignment HAPPENED, and stays green if the
// assignment MOVES to after chatRooms() — at which point the rail paints the
// previous response's state, an old-server first poll renders a silent `ok`,
// and every later transition is one poll stale. So the stub records what the
// renderer was HANDED, at the instant it was handed it.
let RAIL_SAW = [];
function chatRooms() { RAIL_SAW.push(CHAT_DM_STATE); }
function chatPresence() {}
function chatTransport() {}
function chatJumpSync() {}
function chatApplyReact() {}
// faithful copy of the (cosmetic, non-defect) arrow helper
function chatSnip(s) { const one = String(s || "").replace(/\s+/g, " ").trim();
  return one.length > 90 ? one.slice(0, 89) + "…" : one; }
// chatRow SHIM: real cosmetics (tick/reactions/clamp) are irrelevant to the
// ordering defects; what matters is that it INDEXES the row (chatIndex), emits
// the real one-level quote (chatQuote), and carries data-thread/-id + a .creplies
// slot — the exact hooks reply-count + quote reconciliation drive.
// ROW_SAW records the name the owner is marked by AT THE INSTANT each row is
// drawn, which is what the real chatRow compares every row's author with.
let ROW_SAW = [];
function chatRow(m) {
  ROW_SAW.push(chatPostAs());
  chatIndex(m);
  return '<div class="chatmsg" data-key="' + esc(chatKey(m)) + '" data-thread="' + esc(chatThreadKey(m)) +
    '" data-id="' + esc(m.id || "") + '" data-ts="' + esc(m.ts || "") + '" data-from="' + esc(String(m.from || "?")) +
    '">' + chatQuote(m) + '<span class="cwho">' + esc(String(m.from || "?")) +
    '</span><span class="creplies"></span><div class="ctxt">' + esc(String(m.text || "")) + "</div></div>";
}

// module globals the real functions assign into (declarations, not in the splice)
const CHAT_WIN = 50;
const CHAT_DIV_HTML = '<div id="chatdivider">new</div>';
const CHAT_RSET = ["👍"];
let CHAT_SINCE = 0, CHAT_GEN = null, CHAT_OLDEST_BASE = 0, CHAT_LOADING_OLDER = false, CHAT_POLLING = false;
// pollChat stamps the DM-namespace verdict every poll. The REAL chatDmState is
// lifted rather than stubbed, so these scenarios exercise the actual
// envelope->state mapping; this only declares the variable it writes to.
let CHAT_DM_STATE = "ok";
let CHAT_BY_ID = {}, CHAT_BY_KEY = {}, CHAT_REPLYN = {}, CHAT_KIDS = {}, CHAT_GONE = new Set();
let CHAT_REACTS = {}, CHAT_DIVIDED = false, CHAT_AWAY = 0, CHAT_ROOM = "main", CHAT_ROSTER = [];
let CHAT_OWNER = "";

// ───────────────────────── faithful fake server (mirrors web.py) ─────────────────────────
// `dm` is UNDEFINED by default, so the fake server is an OLD-SHAPE server by
// construction: its envelope simply has no dm_incomplete key, exactly like the
// generation that predates the field. Set it to a boolean to model a MODERN
// server that measured. This is the only way an arm can exercise the real poll
// wiring rather than assigning the client state by hand.
// `owner` rides ONLY the windowed initial/reset open, as web.py sends it;
// undefined models a server that could not name the owner.
const SERVER = {rows: [], gen: "0", dm: undefined, answers: undefined, owner: undefined};
function withDm(env) {
  if (SERVER.dm !== undefined) env.dm_incomplete = SERVER.dm;
  // THE OWNER-ADDRESSED READING RIDES THIS SAME ENVELOPE in production
  // (`_owner_signal` writes it into the chat body), so an arm about the answers
  // card can drive it through the real poll instead of calling the card by hand.
  if (SERVER.answers !== undefined) {
    env.owner_answers = SERVER.answers;
    env.owner_mentions = SERVER.answers.length;
  }
  return env;
}
function serve(url) {
  const u = new URL(url, "http://x"), p = u.searchParams;
  const room = p.get("room") || "main", rows = SERVER.rows, total = rows.length, gen = SERVER.gen;
  if (p.get("before") !== null) {
    const before = parseInt(p.get("before") || "0", 10), win = parseInt(p.get("win") || "50", 10) || 50;
    const end = (before >= 0 && before <= total) ? before : total, start = Math.max(0, end - win);
    return withDm({room, lines: rows.slice(start, end), base: start, total, gen});
  }
  if (p.get("ids") !== null) {
    const want = new Set((p.get("ids") || "").split(",").filter(Boolean).slice(0, 100));
    return withDm({room, lines: rows.filter(r => want.has(r.id)), gen});
  }
  const since = parseInt(p.get("since") || "0", 10);
  if (since > 0 && since <= total) return withDm({room, lines: rows.slice(since), total, gen});  // incremental: NO base
  const win = parseInt(p.get("win") || "50", 10) || 50, start = Math.max(0, total - win); // windowed open + base
  const env = withDm({room, lines: rows.slice(start), base: start, total, gen});
  if (SERVER.owner !== undefined) env.owner_name = SERVER.owner;
  return env;
}
/* THE TRANSPORT, because `j` is lifted verbatim now and `j` calls `fetch`.
   Three modes, and the third is the one no other arm in this tree can reach: a
   request that never settles. A rejected read is an answer of a kind — the catch
   runs, the failure is stamped, the lock is released — and a request that simply
   hangs is not, which is why the poll needs a deadline at all. */
let FETCH_MODE = "now";          // "now" | "late" | "pending"
let FETCH_LATE_MS = 0;
const FETCH_CALLS = [];
function fetch(url, opts) {
  const signal = (opts && opts.signal) || null;
  FETCH_CALLS.push({url: String(url), signal: signal});
  const response = () => {
    const env = JSON.parse(JSON.stringify(serve(url)));
    return {ok: true, status: 200, json: () => Promise.resolve(env)};
  };
  if (FETCH_MODE === "now") return Promise.resolve(response());
  return new Promise((resolve, reject) => {
    if (FETCH_MODE === "late") setTimeout(() => resolve(response()), FETCH_LATE_MS);
    // "pending" NEVER resolves. An abort is the only end, which is exactly the
    // deadline under test; without one this promise outlives the page.
    if (signal) signal.addEventListener("abort",
      () => reject(new Error("the request was aborted")));
  });
}
const delay = ms => new Promise(r => setTimeout(r, ms));
const range = n => Array.from({length: n}, (_, i) => i);
function resetClient() {
  CHAT_SINCE = 0; CHAT_GEN = null; CHAT_OLDEST_BASE = 0; CHAT_LOADING_OLDER = false; CHAT_POLLING = false;
  CHAT_BY_ID = {}; CHAT_BY_KEY = {}; CHAT_REPLYN = {}; CHAT_KIDS = {}; CHAT_GONE = new Set();
  CHAT_REACTS = {}; CHAT_DIVIDED = false; CHAT_AWAY = 0; CHAT_ROOM = "main"; CHAT_ROSTER = [];
  LOG = new El("div"); CHAT_OWNER = ""; NAMEBOX = {value: "", placeholder: ""}; ROW_SAW = [];
}
const renderedIds = () => LOG.querySelectorAll(".chatmsg").map(el => el.getAttribute("data-id"));

/*__INJECT__*/

// ───────────────────────── scenarios ─────────────────────────
// THE WIRE, NOT THE MAPPING AND NOT THE RENDER. A reviewer measured that
// deleting production line `CHAT_DM_STATE = chatDmState(d)` left BOTH dedicated
// test files green: the mapping arm calls chatDmState directly and the render
// arm assigns CHAT_DM_STATE by hand, so neither touches the assignment that
// joins them. This scenario runs the REAL pollChat against the REAL fake server
// and reads the state the client ended up in, which is the one thing that
// assignment does. Delete it and the default "ok" survives an old-shape
// response, and this arm goes red.
async function scenario_dm_state_rides_the_poll() {
  resetClient();
  SERVER.rows = range(3).map(n => ({id: "m" + n, from: "a", ts: "t" + n, text: "x"}));
  SERVER.gen = "genA";
  SERVER.dm = undefined;                 // OLD server: no dm_incomplete key at all
  RAIL_SAW = [];
  await pollChat();
  const afterOld = RAIL_SAW[0];          // what the RENDERER was handed, not module state
  resetClient(); SERVER.gen = "genB"; SERVER.dm = false;   // MODERN, measured complete
  RAIL_SAW = [];
  await pollChat();
  const afterModernOk = RAIL_SAW[0];
  resetClient(); SERVER.gen = "genC"; SERVER.dm = true;    // MODERN, measured incomplete
  RAIL_SAW = [];
  await pollChat();
  const afterModernIncomplete = RAIL_SAW[0];
  SERVER.dm = undefined;                 // leave the harness as we found it
  return {name: "dm_state_rides_the_poll",
    pass: afterOld === "unreported" && afterModernOk === "ok"
          && afterModernIncomplete === "incomplete",
    detail: {afterOld, afterModernOk, afterModernIncomplete}};
}

// DEFECT 1a: a rotation IMMEDIATELY AFTER a reset must still be detected — the
// windowed reset open has to leave CHAT_GEN stamped (not wiped) or the very next
// poll misses the reindexed tail (measured: 70 mixed old+new instead of new20..69).
async function scenario_post_reset_rotation() {
  resetClient();
  SERVER.rows = range(50).map(n => ({id: "old" + n, from: "a", ts: "t" + n, text: "old-" + n}));
  SERVER.gen = "genA";
  await pollChat();                                    // windowed reset open (50 rows) -> stamps genA
  const genAfterOpen = CHAT_GEN;
  SERVER.rows = range(70).map(n => ({id: "new" + n, from: "a", ts: "n" + n, text: "new-" + n}));
  SERVER.gen = "genB";                                 // rotated + regrew PAST the cursor
  await pollChat();                                    // since=50: must see gen flip, reset, re-poll
  await delay(30);                                     // let the scheduled windowed re-poll land
  const ids = renderedIds();
  const expected = range(70).slice(20).map(n => "new" + n);   // the reset tail new20..new69
  return {name: "post_reset_rotation_no_rows_missed",
    pass: JSON.stringify(ids) === JSON.stringify(expected),
    detail: {genAfterOpen, rendered: ids.length, first: ids[0], last: ids[ids.length - 1],
             hasOld: ids.some(x => x && x.indexOf("old") === 0)}};
}

// DEFECT 1b: the same reset must PRESERVE the older-page cursor (base), or a
// parent above the window can never be paged in and its reply count never renders.
async function scenario_lazy_parent_after_reset() {
  resetClient();
  const rows = [{id: "P", from: "pa", ts: "tp", text: "the parent"}];
  for (let i = 0; i < 58; i++) rows.push({id: "f" + i, from: "a", ts: "tf" + i, text: "filler " + i});
  rows.push({id: "C", from: "cc", ts: "tc", text: "the reply", reply_to: "P"});
  SERVER.rows = rows; SERVER.gen = "genA";             // 60 rows; win=50 => P (idx0) is ABOVE the window
  await pollChat();                                    // windowed reset open -> base must be 10, NOT wiped to 0
  await delay(20);
  const baseAfterOpen = CHAT_OLDEST_BASE;
  await chatLoadOlder();                               // page older: pulls rows[0:10] incl parent P
  await delay(10);
  const pEl = LOG.querySelector('.chatmsg[data-thread="P"]');
  const count = pEl ? pEl.querySelector(".creplies").textContent : null;
  return {name: "lazy_parent_pages_and_reply_count_after_reset",
    pass: !!pEl && count === "↩1",
    detail: {baseAfterOpen, parentOnScreen: !!pEl, replyCount: count}};
}

// DEFECT 2: chatHydrateParents must chunk ALL pending ids and only mark absent an
// id it actually SENT — with 101 pending parents the 101st (never in the 100-slice)
// must resolve, not be branded "rotated out".
async function scenario_hydrate_over_100_parents() {
  resetClient();
  const parents = range(101).map(n => ({id: "p" + n, from: "pa" + n, ts: "tp" + n, text: "parent " + n}));
  const children = range(101).map(n => ({id: "c" + n, from: "ca" + n, ts: "tc" + n, text: "reply " + n, reply_to: "p" + n}));
  SERVER.rows = parents.concat(children); SERVER.gen = "genA";   // every parent lives in the room
  let html = ""; for (const c of children) html += chatRow(c);   // render the 101 replies -> 101 pending quotes
  LOG.insertAdjacentHTML("beforeend", html);
  const pendingCount = LOG.querySelectorAll(".cquote.pending[data-parent]").length;
  await chatHydrateParents();
  await delay(10);
  return {name: "hydrate_over_100_parents_101st_not_orphaned",
    pass: pendingCount === 101 && !!CHAT_BY_ID["p100"] && !CHAT_GONE.has("p100"),
    detail: {pendingCount, p100resolved: !!CHAT_BY_ID["p100"], p100gone: CHAT_GONE.has("p100"), goneSize: CHAT_GONE.size}};
}

// R1 — THE FIRST READ THAT NEVER ANSWERS. Before the deadline this was
// unreachable: `await j(url)` on a hung endpoint never settles, so the catch
// never runs, no stamp of either kind is written, the `finally` that releases
// CHAT_POLLING is never reached, and the answers card — hidden until a read
// populates it — stays hidden for the life of the page while every later beat
// returns at the lock. The cadence is set small so the deadline is a real wait
// this harness can afford; the DERIVATION is asserted from it (cardBoundS times
// the published cadence, in milliseconds), so the arm is about the rule rather
// than about one number.
async function scenario_pending_first_read_is_NOT_READ() {
  resetClient();
  resetCards();
  SERVER.rows = range(3).map(n => ({id: "p" + n, from: "a", ts: "t" + n, text: "x"}));
  SERVER.gen = "genPending";
  DASH_CHAT_CADENCE_S = 0.05;                 // 4 x 0.05s => a 200ms deadline
  FETCH_MODE = "pending";
  const realJ = j;
  const deadlines = [];
  j = (url, ms) => { deadlines.push(ms); return realJ(url, ms); };
  // A CONTROLLED CLOCK FOR THE STAMPS ONLY: the deadline runs on real timers
  // (setTimeout inside `j`), the AGE the card prints is read off Date.now, and
  // holding the latter is what lets this arm advance past the failed attempt
  // without waiting two minutes.
  let NOW = 1.7e12;
  Date.now = () => NOW;
  const started = wall();
  await pollChat();                           // only the deadline can end this
  const waitedMs = wall() - started;
  const failedStamp = DASH_CHAT_FAILED;
  const lockedAfter = CHAT_POLLING;
  const answeredStamp = DASH_ANSWERS_TS;
  NOW += 130 * 1000;                          // the attempt ages, on that clock
  dashAnswersClock();
  dashFleetClock();
  const card = ANSWERS.innerHTML, row = FLEET.innerHTML;
  // THE LOCK IS THE OTHER HALF: a released poll reaches the transport again.
  FETCH_MODE = "now";
  await pollChat();
  const reads = FETCH_CALLS.length;
  const rendered = renderedIds().length;
  j = realJ;
  resetCards();
  return {name: "pending_first_read_is_NOT_READ",
    pass: deadlines[0] === 200 && failedStamp > 0 && lockedAfter === false
          && answeredStamp === 0 && card.indexOf("NOT READ") !== -1
          && card.indexOf("2m ago") !== -1 && row.indexOf("NOT READ") !== -1
          && reads === 2 && rendered === 3,
    detail: {deadlineMs: deadlines[0], deadlines: deadlines.length,
             waitedMs: waitedMs, failedStamp: failedStamp,
             lockedAfter: lockedAfter, answeredStamp: answeredStamp,
             card: card, fleet: row, reads: reads, rendered: rendered}};
}

// THE UNCONDITIONAL POSITIVE CONTROL FOR THE ARM ABOVE, in the same harness
// over the same transport: a read that answers LATE but inside the deadline
// renders for real — the rows reach the log, no failure is stamped, and the
// answers card draws the reading the poll fetched with the provenance line every
// chat-fed card carries. An instrument that cannot render a real read proves
// nothing by rendering NOT READ.
async function scenario_a_read_that_answers_LATE_still_renders() {
  resetClient();
  resetCards();
  SERVER.rows = range(2).map(n => ({id: "L" + n, from: "seat-a", ts: "t" + n, text: "late " + n}));
  SERVER.gen = "genLate";
  SERVER.answers = [{from: "seat-a", text: "@daria the node is back",
                     ts: "2026-09-13T00:00:00Z", room: "main", age_s: 12}];
  DASH_CHAT_CADENCE_S = 0.05;                 // the same 200ms deadline
  FETCH_MODE = "late"; FETCH_LATE_MS = 40;    // inside it
  await pollChat();
  const ids = renderedIds();
  const card = ANSWERS.innerHTML;
  const out = {name: "a_read_that_answers_LATE_still_renders",
    pass: JSON.stringify(ids) === JSON.stringify(["L0", "L1"])
          && DASH_CHAT_FAILED === 0 && DASH_ANSWERS_TS > 0
          && card.indexOf("answers for you") !== -1
          && card.indexOf("helm chat read") !== -1
          && card.indexOf("the node is back") !== -1,
    detail: {rendered: ids, failedStamp: DASH_CHAT_FAILED,
             answeredStamp: DASH_ANSWERS_TS, card: card}};
  resetCards();
  return out;
}

// THE OWNER'S NAME RIDES THE RESET OPEN AND IS IN PLACE BEFORE ITS ROWS DRAW.
// The page ships no person's name, so the only name it can mark his rows with
// is the one the server announces. Stamped AFTER the render, the first paint
// would mark nothing and every later row would be marked: the order is the
// property, so the arm reads what each row saw at the instant it was drawn.
async function scenario_owner_name_rides_the_reset_open() {
  resetClient();
  SERVER.rows = range(3).map(n => ({id: "o" + n, from: "harbor", ts: "t" + n, text: "x"}));
  SERVER.gen = "genOwner"; SERVER.owner = "harbor";
  await pollChat();                                   // windowed reset open: carries it
  const opened = ROW_SAW.slice(), placeholder = NAMEBOX.placeholder;
  SERVER.rows = SERVER.rows.concat([{id: "o3", from: "seat-a", ts: "t3", text: "y"}]);
  ROW_SAW = [];
  await pollChat();                                   // incremental: carries no name
  const later = ROW_SAW.slice(), kept = CHAT_OWNER;
  resetClient(); SERVER.gen = "genNoOwner"; SERVER.owner = undefined;
  await pollChat();                                   // a server that named nobody
  const unnamed = ROW_SAW.slice(), unnamedPlaceholder = NAMEBOX.placeholder;
  SERVER.owner = undefined;
  return {name: "owner_name_rides_the_reset_open",
    pass: JSON.stringify(opened) === JSON.stringify(["harbor", "harbor", "harbor"])
          && placeholder === "harbor"
          && JSON.stringify(later) === JSON.stringify(["harbor"]) && kept === "harbor"
          && unnamed.length === 4 && unnamed.every(x => x === "")
          && unnamedPlaceholder === "owner",
    detail: {opened, placeholder, later, kept, unnamed, unnamedPlaceholder}};
}

(async () => {
  const results = [];
  for (const s of [scenario_owner_name_rides_the_reset_open, scenario_post_reset_rotation, scenario_lazy_parent_after_reset, scenario_hydrate_over_100_parents, scenario_dm_state_rides_the_poll, scenario_pending_first_read_is_NOT_READ, scenario_a_read_that_answers_LATE_still_renders]) {
    try { results.push(await s()); }
    catch (e) { results.push({name: s.name, pass: false, detail: {error: String(e && e.stack || e)}}); }
  }
  process.stdout.write(JSON.stringify(results));
  process.exit(results.every(r => r.pass) ? 0 : 1);
})();
