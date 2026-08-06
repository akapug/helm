/* Client-runtime harness for the mutation-bearer lifecycle (the owner's
 * first-post-forbidden bug).
 *
 * The bearer is PER-PROCESS and the owner's tab is LONG-LIVED: the web unit
 * recycles under it (RuntimeMaxSec=1h, `helm rearm`) and mints a fresh token
 * while the open page still holds the dead one — the first post after coming
 * back answered 403 "forbidden (mutations always require the bearer token the
 * UI carries)" until a manual reload. The fix: post() refreshes the token from
 * our own served page on a 403 and retries ONCE, loudly failing otherwise.
 *
 * This harness runs the ACTUAL refreshToken / post / homePost source lifted
 * verbatim from the assembled web UI (spliced at the /*__INJECT__*(/ marker by
 * tests/test_web_token_client_runtime.py) against a fake fetch that mirrors
 * web.py's do_POST guard and _ui() substitution — the page it serves for the
 * refresh is the REAL assembled page byte-for-byte, so the refresh regex is
 * exercised against the true declaration, not a toy. Plain CommonJS
 * (non-strict) so the spliced declarations share this module scope. */

const fs = require("fs");
const PAGE_SRC = fs.readFileSync("__PAGE_PATH__", "utf8"); // the real assembled page
const MARKER = "__HELM" + "_TOKEN__"; // split like the UI's guard: this file is
                                      // never served, but keep one discipline

// ───────────────────────── fake server (mirrors web.py) ─────────────────────────
const SERVER = {token: "T2", posts: [], pageFetches: 0,
                alwaysForbid: false, pageDown: false, rawPage: false, badRequest: false};

function resp(status, obj) {
  return {ok: status >= 200 && status < 300, status,
          json: () => Promise.resolve(obj),
          text: () => Promise.resolve(JSON.stringify(obj))};
}

function fetch(url, opts) {
  opts = opts || {};
  if (!opts.method || opts.method === "GET") {
    if (SERVER.pageDown) return Promise.reject(new TypeError("fetch failed"));
    SERVER.pageFetches++;
    // _ui(): the raw file with EVERY marker occurrence templated to the live
    // token (web.py uses bytes.replace = replace-all; split/join mirrors it)
    const body = SERVER.rawPage ? PAGE_SRC : PAGE_SRC.split(MARKER).join(SERVER.token);
    return Promise.resolve({ok: true, status: 200, text: () => Promise.resolve(body)});
  }
  const auth = (opts.headers || {})["Authorization"] || "";
  SERVER.posts.push({url, auth});
  if (SERVER.badRequest) return Promise.resolve(resp(400, {error: "body wants a JSON object"}));
  // do_POST: every mutation demands the per-process bearer — 403 without
  if (SERVER.alwaysForbid || auth !== "Bearer " + SERVER.token)
    return Promise.resolve(resp(403, {error: "forbidden (mutations always require the bearer token the UI carries)"}));
  return Promise.resolve(resp(200, {ok: true}));
}

const FORBIDDEN = "forbidden (mutations always require the bearer token the UI carries)";

function reset(over) {
  SERVER.token = "T2"; SERVER.posts = []; SERVER.pageFetches = 0;
  SERVER.alwaysForbid = false; SERVER.pageDown = false;
  SERVER.rawPage = false; SERVER.badRequest = false;
  TOKEN = "T1";                       // the tab's serve-time token, now stale
  Object.assign(SERVER, over || {});
}

async function threw(p) {
  try { return {threw: false, value: await p}; }
  catch (e) { return {threw: true, message: String(e && e.message || e)}; }
}

/*__INJECT__*/

// ───────────────────────── scenarios ─────────────────────────
// THE OWNER'S BUG: first post from a tab that outlived a web-unit recycle must
// refresh the bearer from the served page and succeed on ONE retry — never
// surface "forbidden" for the page's own staleness.
async function scenario_stale_bearer_first_post_recovers() {
  reset();                                             // tab T1, server now T2
  const r = await threw(post("/api/chat", {room: "main", text: "hi", name: "owner"}));
  return {name: "stale_bearer_first_post_recovers",
    pass: !r.threw && r.value.ok === true && SERVER.posts.length === 2
      && SERVER.posts[0].auth === "Bearer T1" && SERVER.posts[1].auth === "Bearer T2"
      && SERVER.pageFetches === 1 && TOKEN === "T2",
    detail: {threw: r.threw, message: r.message, posts: SERVER.posts,
             pageFetches: SERVER.pageFetches, token: TOKEN}};
}

// The adopted token STICKS: the next post rides it with no refresh round-trip.
async function scenario_next_post_rides_the_adopted_token() {
  reset({token: "T2"}); TOKEN = "T2";                  // post-recovery state
  const r = await threw(post("/api/chat", {room: "main", text: "again", name: "owner"}));
  return {name: "next_post_rides_the_adopted_token",
    pass: !r.threw && SERVER.posts.length === 1 && SERVER.pageFetches === 0,
    detail: {threw: r.threw, posts: SERVER.posts.length, pageFetches: SERVER.pageFetches}};
}

// A GENUINE forbidden (server refuses the very token it serves) is NOT masked:
// the refresh yields the token already held, so nothing retries — one attempt,
// one loud error carrying the server's exact words.
async function scenario_genuine_forbidden_still_loud() {
  reset({alwaysForbid: true}); TOKEN = "T2";           // held token IS current
  const r = await threw(post("/api/chat", {room: "main", text: "hi", name: "owner"}));
  return {name: "genuine_forbidden_still_loud",
    pass: r.threw && r.message === FORBIDDEN
      && SERVER.posts.length === 1 && SERVER.pageFetches === 1,
    detail: {threw: r.threw, message: r.message, posts: SERVER.posts.length,
             pageFetches: SERVER.pageFetches}};
}

// A refresh that cannot reach the server changes nothing and hides nothing:
// the ORIGINAL 403 still surfaces (post's .catch on refreshToken only).
async function scenario_refresh_down_original_403_surfaces() {
  reset({pageDown: true});                             // stale T1, page unreachable
  const r = await threw(post("/api/chat", {room: "main", text: "hi", name: "owner"}));
  return {name: "refresh_down_original_403_surfaces",
    pass: r.threw && r.message === FORBIDDEN && SERVER.posts.length === 1 && TOKEN === "T1",
    detail: {threw: r.threw, message: r.message, posts: SERVER.posts.length, token: TOKEN}};
}

// Non-403 failures never trigger the bearer path: no page fetch, no retry.
async function scenario_non_403_never_refreshes() {
  reset({badRequest: true}); TOKEN = "T2";
  const r = await threw(post("/api/chat", {room: "main", text: "hi", name: "owner"}));
  return {name: "non_403_never_refreshes",
    pass: r.threw && r.message === "body wants a JSON object"
      && SERVER.posts.length === 1 && SERVER.pageFetches === 0,
    detail: {threw: r.threw, message: r.message, posts: SERVER.posts.length,
             pageFetches: SERVER.pageFetches}};
}

// A page served UNSUBSTITUTED (file:// dev open, broken templating) must never
// be adopted as a bearer: the marker guard refuses, the 403 stays loud.
async function scenario_unsubstituted_page_never_adopts_the_marker() {
  reset({rawPage: true});
  const r = await threw(post("/api/chat", {room: "main", text: "hi", name: "owner"}));
  return {name: "unsubstituted_page_never_adopts_the_marker",
    pass: r.threw && r.message === FORBIDDEN && TOKEN === "T1" && SERVER.posts.length === 1,
    detail: {threw: r.threw, message: r.message, token: TOKEN, posts: SERVER.posts.length}};
}

// refreshToken against the REAL page: the regex finds the true declaration and
// adopts the live token (pins the _ui() substitution ↔ client-regex contract).
async function scenario_refresh_regex_finds_the_real_declaration() {
  reset({token: "T3"});
  const r = await threw(refreshToken());
  return {name: "refresh_regex_finds_the_real_declaration",
    pass: !r.threw && TOKEN === "T3" && SERVER.pageFetches === 1,
    detail: {threw: r.threw, message: r.message, token: TOKEN, pageFetches: SERVER.pageFetches}};
}

// homePost keeps its callers' {error: ...}-object contract (never a throw) on
// BOTH arms, and shares the stale-bearer retry by riding post().
async function scenario_homepost_error_object_contract() {
  reset({alwaysForbid: true}); TOKEN = "T2";
  const bad = await threw(homePost({action: "verify", name: "x", provider: "claude"}));
  const badOk = !bad.threw && bad.value && bad.value.error === FORBIDDEN;
  reset();                                             // stale T1 → retry recovers
  const good = await threw(homePost({action: "verify", name: "x", provider: "claude"}));
  const goodOk = !good.threw && good.value.ok === true
    && SERVER.posts.length === 2 && SERVER.posts[1].url === "/api/homes";
  return {name: "homepost_error_object_contract",
    pass: badOk && goodOk,
    detail: {badThrew: bad.threw, badValue: bad.value, goodThrew: good.threw,
             goodValue: good.value, posts: SERVER.posts}};
}

(async () => {
  const results = [];
  for (const s of [scenario_stale_bearer_first_post_recovers,
                   scenario_next_post_rides_the_adopted_token,
                   scenario_genuine_forbidden_still_loud,
                   scenario_refresh_down_original_403_surfaces,
                   scenario_non_403_never_refreshes,
                   scenario_unsubstituted_page_never_adopts_the_marker,
                   scenario_refresh_regex_finds_the_real_declaration,
                   scenario_homepost_error_object_contract]) {
    try { results.push(await s()); }
    catch (e) { results.push({name: s.name, pass: false, detail: {error: String(e && e.stack || e)}}); }
  }
  process.stdout.write(JSON.stringify(results));
  process.exit(results.every(r => r.pass) ? 0 : 1);
})();
