// Runtime harness for the work-tab composer-preservation contract (#231 and
// the codex rework round, chat e36096810c95). Runs the ACTUAL odqInit /
// odqRender / odqAct / odqTyping / odqComposerState / odqBox / odqCard /
// odqBadge / esc source lifted verbatim from the assembled web UI over a DOM
// stub with
// LIVE replacement semantics — the three properties the previous harness
// could not see:
//   * every innerHTML set re-derives the card objects from the freshly set
//     html (a composer stub exists only where the html actually rendered one);
//   * a replaced card DETACHES its old elements: document.activeElement falls
//     back to null and host.contains(oldEl) goes false (focus invalidation);
//   * j/post are controllable promises, so async interleavings are SCENES —
//     send -> poll fires mid-await -> resolve -> assert, not a static splice.
// Scenes (each key asserted by tests/test_web_decisions_composer_runtime.py):
//   p1        the exact send/poll race repro: sent text must NOT resurrect
//   mirror    follow-up typed while the POST awaits survives completion
//   branches  drafts held across error / unavailable / empty / card-omitted
//   order     a slow OLDER response (success or error) never replaces newer
//   focusempty a focused-but-EMPTY composer holds the redraw (P2)
//   fail      a failed send restores the text; a mid-await follow-up outranks
//   absentfail a mid-flight poll DROPS the card, THEN the send fails: the
//             text parks in the ODQ_DRAFTS ledger and returns WITH the card
//   focuserr  fetch-error catch under a FOCUSED composer: the guard holds
//             BEFORE the error card's innerHTML replacement, focus survives
//   clear     the owner deliberately empties a restored parked draft: the
//             ledger entry retracts and the next render must NOT resurrect it
//   typing / restore / control — the original three scenes, now odqInit-driven
// Output: one JSON object on stdout; the python side asserts on it.
"use strict";

const SPLICED = process.env.HELM_SPLICED_SRC;
if (!SPLICED) { console.error("HELM_SPLICED_SRC unset"); process.exit(2); }
const src = require("fs").readFileSync(SPLICED, "utf8");

// ---- DOM with live replacement semantics ----------------------------------
const state = { host: null, meta: { textContent: "" }, active: null };
function commentStub() {
  return { value: "", classList: { contains: c => c === "odqcomment" } };
}
function cardStub(id, hasComposer) {
  return {
    dataset: { id },
    style: {},
    _comment: hasComposer ? commentStub() : null,
    querySelector(sel) { return sel === ".odqcomment" ? this._comment : null; },
    querySelectorAll() { return []; },
  };
}
// one stub per data-id in the freshly set html; a composer only where the
// card's own html slice actually rendered an .odqcomment input
function deriveCards(html) {
  const starts = [...html.matchAll(/<div class="odqcard" data-id="([^"]+)"/g)];
  return starts.map((m, i) => {
    const seg = html.slice(m.index, i + 1 < starts.length ? starts[i + 1].index : html.length);
    return cardStub(m[1], seg.includes("odqcomment"));
  });
}
function makeHost() {
  return {
    _html: "", _sets: 0, _cards: [],
    set innerHTML(v) {
      this._html = v; this._sets += 1;
      const old = this._cards;
      this._cards = deriveCards(v);
      // focus invalidation: replacing the body detaches the old elements —
      // a browser drops activeElement to <body> when its element is removed
      if (old.some(c => c._comment && c._comment === state.active)) state.active = null;
    },
    get innerHTML() { return this._html; },
    querySelectorAll(sel) { return sel === ".odqcard" ? [...this._cards] : []; },
    contains(el) { return !!el && this._cards.some(c => c._comment === el); },
  };
}
global.document = {
  get activeElement() { return state.active; },
  // LIVE cards, re-derived per innerHTML set — never a frozen pre-replacement list
  querySelectorAll(sel) { return sel === "#odqlist .odqcard" ? [...state.host._cards] : []; },
};
global.$ = sel => sel === "#odqlist" ? state.host : sel === "#odqmeta" ? state.meta : null;
global.toast = () => {};

// ---- controllable network --------------------------------------------------
const net = { queue: [] }; // each entry: {kind, url, res, rej}
global.j = url => new Promise((res, rej) => net.queue.push({ kind: "j", url, res, rej }));
global.post = url => new Promise((res, rej) => net.queue.push({ kind: "post", url, res, rej }));
const tick = () => new Promise(r => setImmediate(r)); // flush the await chain
function take(kind) {
  const i = net.queue.findIndex(q => q.kind === kind);
  if (i < 0) throw new Error("no pending " + kind + " request");
  return net.queue.splice(i, 1)[0];
}

// ---- splice the real functions --------------------------------------------
// indirect eval runs non-strict at global scope: function declarations attach
// to globalThis while const/let (esc, ODQ_DRAFTS, ODQ_GEN) stay eval-scoped —
// the appended peek line bridges the draft ledger out for scene resets.
// eslint-disable-next-line no-eval
(0, eval)(src + "\nglobalThis.__odqDrafts = ODQ_DRAFTS;");
const { odqInit, odqAct, __odqDrafts } = globalThis;

// ---- scene plumbing --------------------------------------------------------
const row = id => ({ id, title: "t " + id, context: "c", asker: "seat", ts: "2026-08-05T02:06", status: "open", options: [{ key: "ok", label: "go", consequence: "q" }] });
const feed = (...ids) => ({ entries: ids.map(row), counts: { open: ids.length } });
const card = id => state.host._cards.find(c => c.dataset.id === id);
const boxVal = id => { const c = card(id); return c && c._comment ? c._comment.value : "(no live composer for " + id + ")"; };
async function seed(...ids) {
  net.queue.length = 0;
  for (const k of Object.keys(__odqDrafts)) delete __odqDrafts[k];
  state.active = null; state.meta.textContent = ""; state.host = makeHost();
  odqInit();
  take("j").res(feed(...ids));
  await tick();
}
async function poll(d) { // one 45s-poll round-trip resolved NOW
  odqInit();
  take("j").res(d);
  await tick();
}
const out = {};

(async () => {
// ---- scene p1: the exact send/poll race repro (codex e36096810c95) --------
{
  await seed("card-aaa", "card-bbb");
  const el = card("card-aaa");
  el._comment.value = "already submitted";
  state.active = null; // send blurs the input (focus moved to the button)
  const act = odqAct("/api/decisions/comment", { id: "card-aaa", text: "already submitted" }, el, "commented on");
  out.p1_cleared_at_send = el._comment.value; // "" BEFORE the await resolves
  await poll(feed("card-aaa", "card-bbb"));   // 45s poll fires mid-await
  out.p1_poll_replaced_card = card("card-aaa") !== el;
  out.p1_live_after_poll = boxVal("card-aaa");           // bug measured: "already submitted"
  take("post").res({});                        // POST now succeeds
  await act;
  take("j").res(feed("card-aaa", "card-bbb")); // odqAct's own refresh render
  await tick();
  out.p1_live_after_action_refresh = boxVal("card-aaa"); // bug measured: "already submitted"
}

// ---- scene mirror: follow-up typed while the POST awaits survives ---------
{
  await seed("card-aaa");
  const el = card("card-aaa");
  el._comment.value = "first comment";
  state.active = null;
  const act = odqAct("/api/decisions/comment", { id: "card-aaa", text: "first comment" }, el, "commented on");
  el._comment.value = "next thought"; // the owner keeps typing mid-flight, then blurs
  take("post").res({});
  await act;
  take("j").res(feed("card-aaa"));    // completion's refresh render
  await tick();
  out.mirror_followup = boxVal("card-aaa"); // old code cleared it to ""
}

// ---- scene branches: drafts held across EVERY replacement branch ----------
{
  await seed("card-aaa", "card-bbb");
  card("card-aaa")._comment.value = "parked draft"; // blurred: active stays null

  odqInit(); take("j").rej(new Error("boom")); await tick(); // fetch-error card
  out.branch_error_replaced = /unreadable/.test(state.host.innerHTML);
  await poll(feed("card-aaa", "card-bbb"));
  out.branch_error_value = boxVal("card-aaa");

  await poll({ unavailable: true, why: "down" });            // unavailable card
  out.branch_unavail_replaced = /unavailable/.test(state.host.innerHTML);
  await poll(feed("card-aaa", "card-bbb"));
  out.branch_unavail_value = boxVal("card-aaa");

  await poll(feed());                                        // empty-queue card
  out.branch_empty_replaced = /no open decision cards/.test(state.host.innerHTML);
  await poll(feed("card-aaa", "card-bbb"));
  out.branch_empty_value = boxVal("card-aaa");

  await poll(feed("card-bbb"));                              // render omits the card
  out.branch_omit_gone = !card("card-aaa");
  out.branch_omit_sibling = boxVal("card-bbb");              // must-miss: no leak
  await poll(feed("card-aaa", "card-bbb"));
  out.branch_omit_value = boxVal("card-aaa");
  out.branch_sibling_value = boxVal("card-bbb");
}

// ---- scene order: a slow OLDER response never replaces a newer render -----
{
  await seed("card-aaa");
  odqInit(); const older = take("j");
  odqInit(); const newer = take("j");
  newer.res(feed("card-bbb")); await tick();
  const setsAfterNewer = state.host._sets;
  older.res(feed("card-aaa")); await tick(); // stale success must be discarded
  out.order_stale_sets = state.host._sets - setsAfterNewer; // MUST be 0
  out.order_shows_newer = !!card("card-bbb") && !card("card-aaa");
  odqInit(); const older2 = take("j");
  odqInit(); const newer2 = take("j");
  newer2.res(feed("card-bbb")); await tick();
  older2.rej(new Error("slow fail")); await tick(); // stale ERROR discarded too
  out.order_stale_error_kept = !/unreadable/.test(state.host.innerHTML);
}

// ---- scene focusempty: focused-but-EMPTY composer holds the redraw (P2) ---
{
  await seed("card-aaa");
  const c = card("card-aaa")._comment;
  state.active = c; // focused, zero keystrokes so far
  const sets = state.host._sets;
  await poll(feed("card-aaa"));
  out.focusempty_sets = state.host._sets - sets;   // MUST be 0
  out.focusempty_focus_alive = state.active === c; // replacement would null it
  out.focusempty_meta = state.meta.textContent;
}

// ---- scene fail: a failed send must not eat the comment -------------------
{
  await seed("card-aaa");
  const el = card("card-aaa");
  el._comment.value = "precious";
  const act = odqAct("/api/decisions/comment", { id: "card-aaa", text: "precious" }, el, "commented on");
  take("post").rej(new Error("503"));
  await act;
  out.fail_restored = boxVal("card-aaa");

  // mid-await poll replaced the card, THEN the send fails: restore must land
  // in the LIVE box, not the detached one
  await seed("card-aaa");
  const el2 = card("card-aaa");
  el2._comment.value = "precious2";
  const act2 = odqAct("/api/decisions/comment", { id: "card-aaa", text: "precious2" }, el2, "commented on");
  await poll(feed("card-aaa"));
  take("post").rej(new Error("503"));
  await act2;
  out.fail_restored_live = boxVal("card-aaa");
  out.fail_detached_old = el2._comment.value; // stays "" — never resurrected

  // a follow-up typed mid-await OUTRANKS the failed text (owner's newer intent)
  await seed("card-aaa");
  const el3 = card("card-aaa");
  el3._comment.value = "failed text";
  const act3 = odqAct("/api/decisions/comment", { id: "card-aaa", text: "failed text" }, el3, "commented on");
  el3._comment.value = "newer intent";
  take("post").rej(new Error("503"));
  await act3;
  out.fail_followup_wins = boxVal("card-aaa");
}

// ---- scene absentfail: the card VANISHES mid-flight, THEN the send fails --
// (codex rework P1-1) rejection lands with NO live box: odqAct's ledger
// fallback must park the text so it returns WITH the card — deleting the
// fallback loses the comment the moment the card comes back.
{
  await seed("card-aaa", "card-bbb");
  const el = card("card-aaa");
  el._comment.value = "precious3";
  const act = odqAct("/api/decisions/comment", { id: "card-aaa", text: "precious3" }, el, "commented on");
  await poll(feed("card-bbb"));                // mid-await poll DROPS the card
  out.absent_card_gone = !card("card-aaa");
  take("post").rej(new Error("503"));          // the send fails — no live box exists
  await act;
  out.absent_parked = __odqDrafts["card-aaa"] || "";
  await poll(feed("card-aaa", "card-bbb"));    // the card returns
  out.absent_returned_value = boxVal("card-aaa");
  out.absent_detached_old = el._comment.value; // stays "" — off-DOM, never written
  out.absent_sibling = boxVal("card-bbb");     // must-miss: no leak
}

// ---- scene focuserr: fetch-error catch under a FOCUSED-EMPTY composer -----
// (codex rework P1-2) the typing/state guard must run BEFORE the catch's
// innerHTML replacement: moved after it, the error card detaches the focused
// input FIRST and the now-dead focus waves the guard through — sets goes 1
// and focus dies, exactly what this scene measures.
{
  await seed("card-aaa");
  const c = card("card-aaa")._comment;
  state.active = c;                            // focused, zero keystrokes
  const sets = state.host._sets;
  odqInit(); take("j").rej(new Error("boom")); await tick();
  out.focuserr_sets = state.host._sets - sets;   // MUST be 0
  out.focuserr_focus_alive = state.active === c; // replacement would null it
  out.focuserr_meta = state.meta.textContent;
}

// ---- scene clear: deliberately emptying a restored draft stays cleared ----
// (codex rework P1-3) an emptied box RETRACTS its ledger entry; without the
// retraction in odqComposerState the next render resurrects the stale text
// the owner just deleted.
{
  await seed("card-aaa", "card-bbb");
  card("card-aaa")._comment.value = "stale text"; // blurred park
  await poll(feed("card-aaa", "card-bbb"));
  out.clear_restored_first = boxVal("card-aaa");  // positive control: restored
  card("card-aaa")._comment.value = "";           // the owner clears it, still blurred
  await poll(feed("card-aaa", "card-bbb"));       // the next 45s render
  out.clear_stays_cleared = boxVal("card-aaa");   // MUST stay ""
  out.clear_ledger_empty = !("card-aaa" in __odqDrafts);
}

// ---- original three scenes, now driven through the real odqInit -----------
{
  await seed("card-aaa", "card-bbb"); // typing guard: focused + non-empty
  const c = card("card-aaa")._comment;
  c.value = "half a thought"; state.active = c;
  const sets = state.host._sets;
  await poll(feed("card-aaa", "card-bbb"));
  out.typing_sets = state.host._sets - sets; // MUST be 0
  out.typing_meta = state.meta.textContent;
}
{
  await seed("card-aaa", "card-bbb"); // restore: blurred draft returns to ITS card
  card("card-aaa")._comment.value = "half a thought";
  const sets = state.host._sets;
  await poll(feed("card-aaa", "card-bbb"));
  out.restore_sets = state.host._sets - sets; // MUST be >= 1
  out.restore_value = boxVal("card-aaa");
  out.sibling_value = boxVal("card-bbb");
}
{
  await seed("card-aaa", "card-bbb"); // control: the instrument can SEE a redraw
  const sets = state.host._sets;
  await poll(feed("card-aaa", "card-bbb"));
  out.control_sets = state.host._sets - sets; // MUST be >= 1
}

process.stdout.write(JSON.stringify(out));
})().catch(e => { console.error(e.stack || String(e)); process.exit(1); });
