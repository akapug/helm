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
function $(sel) {
  if (sel === "#chatlog") return LOG;
  if (sel === "#view-chat") return VIEW;
  return null;                                                // #chatjump etc — chatJumpSync is shimmed
}
function toast() {}
function chatSignal() {}
function chatRooms() {}
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
function chatRow(m) {
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
let CHAT_BY_ID = {}, CHAT_BY_KEY = {}, CHAT_REPLYN = {}, CHAT_KIDS = {}, CHAT_GONE = new Set();
let CHAT_REACTS = {}, CHAT_DIVIDED = false, CHAT_AWAY = 0, CHAT_ROOM = "main", CHAT_ROSTER = [];

// ───────────────────────── faithful fake server (mirrors web.py) ─────────────────────────
const SERVER = {rows: [], gen: "0"};
function serve(url) {
  const u = new URL(url, "http://x"), p = u.searchParams;
  const room = p.get("room") || "main", rows = SERVER.rows, total = rows.length, gen = SERVER.gen;
  if (p.get("before") !== null) {
    const before = parseInt(p.get("before") || "0", 10), win = parseInt(p.get("win") || "50", 10) || 50;
    const end = (before >= 0 && before <= total) ? before : total, start = Math.max(0, end - win);
    return {room, lines: rows.slice(start, end), base: start, total, gen};
  }
  if (p.get("ids") !== null) {
    const want = new Set((p.get("ids") || "").split(",").filter(Boolean).slice(0, 100));
    return {room, lines: rows.filter(r => want.has(r.id)), gen};
  }
  const since = parseInt(p.get("since") || "0", 10);
  if (since > 0 && since <= total) return {room, lines: rows.slice(since), total, gen};  // incremental: NO base
  const win = parseInt(p.get("win") || "50", 10) || 50, start = Math.max(0, total - win); // windowed open + base
  return {room, lines: rows.slice(start), base: start, total, gen};
}
function j(url) { return Promise.resolve(JSON.parse(JSON.stringify(serve(url)))); }
const delay = ms => new Promise(r => setTimeout(r, ms));
const range = n => Array.from({length: n}, (_, i) => i);
function resetClient() {
  CHAT_SINCE = 0; CHAT_GEN = null; CHAT_OLDEST_BASE = 0; CHAT_LOADING_OLDER = false; CHAT_POLLING = false;
  CHAT_BY_ID = {}; CHAT_BY_KEY = {}; CHAT_REPLYN = {}; CHAT_KIDS = {}; CHAT_GONE = new Set();
  CHAT_REACTS = {}; CHAT_DIVIDED = false; CHAT_AWAY = 0; CHAT_ROOM = "main"; CHAT_ROSTER = [];
  LOG = new El("div");
}
const renderedIds = () => LOG.querySelectorAll(".chatmsg").map(el => el.getAttribute("data-id"));

/*__INJECT__*/

// ───────────────────────── scenarios ─────────────────────────
// DEFECT 1a: a rotation IMMEDIATELY AFTER a reset must still be detected — the
// windowed reset open has to leave CHAT_GEN stamped (not wiped) or the very next
// poll misses the reindexed tail (codex-3: 70 mixed old+new instead of new20..69).
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

(async () => {
  const results = [];
  for (const s of [scenario_post_reset_rotation, scenario_lazy_parent_after_reset, scenario_hydrate_over_100_parents]) {
    try { results.push(await s()); }
    catch (e) { results.push({name: s.name, pass: false, detail: {error: String(e && e.stack || e)}}); }
  }
  process.stdout.write(JSON.stringify(results));
  process.exit(results.every(r => r.pass) ? 0 : 1);
})();
