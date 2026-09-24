/* Executable client contract for Config injection observation + roster isolation. */
const calls = [];
const deadlines = [];
const detail = {innerHTML: ""};
const rosterView = {classList: {contains: name => name === "on"}};
let seatPopNode = null;
function button(cls) { return {className: cls, dataset: {}, onclick: null}; }
const document = {hidden: false,
  body: {appendChild: node => { seatPopNode = node; }},
  createElement: () => {
    const buttons = {".spcfg": button("spcfg"), ".spjump": button("spjump"),
      ".spren": button("spren")};
    return {className: "", id: "", innerHTML: "", style: {}, offsetWidth: 240,
      querySelector: sel => buttons[sel], remove: () => { seatPopNode = null; },
      buttons};
  },
  addEventListener: () => {}, removeEventListener: () => {}};
const window = {innerWidth: 1200};
function $(sel) {
  if (sel === "#cfgdetail") return detail;
  if (sel === "#view-roster") return rosterView;
  if (sel === "#seatpop") return seatPopNode;
  return null;
}
function esc(s) { return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"); }
const rosterRenders = [], pickerRenders = [], orphanRenders = [];
function rosterOrphans(rep) { orphanRenders.push(rep); }
function renderRoster(rep) { LAST_ROSTER = rep; rosterRenders.push(rep); }
function seatPicker(rep) { pickerRenders.push(rep); }
function showView(name) { calls.push("view:" + name); }
function closeSeatPop() { if (seatPopNode) seatPopNode.remove(); }
function presenceLabel(p) { return p; }
function seatRuntime(s) { return (s.runtime && s.runtime.agent_harness) || ""; }
function lago() { return "now"; }
function chatShort(s) { return s; }
function post() { return Promise.resolve({}); }
function toast() {}
function _seatPopAway() {}
function _seatPopEsc() {}
let LAST_ROSTER = null, ROSTER_TODOS = {}, ROSTER_POLLING = false;
let ROSTER_TODOS_POLLING = false;
let response = {};
const injectionQueue = [], rosterQueue = [], todosQueue = [];
function deferred() {
  let resolve, reject;
  const promise = new Promise((ok, no) => { resolve = ok; reject = no; });
  return {promise, resolve, reject};
}
function j(url, ms) {
  calls.push(url);
  deadlines.push({url, ms});
  if (url.indexOf("/api/config/injection?") === 0) {
    const queued = injectionQueue.shift();
    return queued ? queued.promise : Promise.resolve(response);
  }
  if (url === "/api/todos") {
    const queued = todosQueue.shift();
    return queued ? queued.promise : Promise.resolve({seats: []});
  }
  if (url === "/api/chat/roster") {
    const queued = rosterQueue.shift();
    return queued ? queued.promise : Promise.resolve({seats: []});
  }
  return Promise.reject(new Error("unexpected read " + url));
}
function configResponse(requestedSeat, verifiedSeat, session, bytes) {
  return {state: "partial", requested: {seat: requestedSeat, session}, target: {
    seat: {value: verifiedSeat, sources: ["roster-current"]},
    session: {value: session, sources: ["roster-current"]},
    cwd: {value: "/work/" + verifiedSeat, sources: ["roster-current"]},
    harness: {value: "claude", sources: ["roster-verified-runtime"]},
    transcript_home: {value: "/transcripts/" + verifiedSeat, sources: ["catalog-path"]},
    config_home: {value: null, sources: []}}, missing: ["config_home"],
    samples: {v3_utf8: {count: 1, rendered_bytes: bytes, average_rendered_bytes: bytes},
      v2_utf8: {count: 1, rendered_bytes: 7, average_rendered_bytes: 7},
      v1_approx: {count: 2, approx_characters: 9, average_approx_characters: 4.5}}};
}

/*__INJECT__*/

const results = [];
async function run() {
  calls.length = 0;
  deadlines.length = 0;
  await pollRoster();
  const pollReads = calls.slice();
  const pollDeadlines = deadlines.slice();
  results.push({name: "roster_poll_is_lightweight", pass:
    JSON.stringify(pollReads) === JSON.stringify(["/api/chat/roster"]),
    detail: {reads: pollReads}});
  results.push({name: "roster_poll_is_bounded", pass:
    pollDeadlines.length === 1 && pollDeadlines[0].ms === 8000,
    detail: {deadlines: pollDeadlines}});

  calls.length = 0;
  deadlines.length = 0;
  rosterRenders.length = 0;
  pickerRenders.length = 0;
  orphanRenders.length = 0;
  const hungTodos = deferred(), fastRoster = deferred();
  todosQueue.push(hungTodos);
  rosterQueue.push(fastRoster);
  const todosRead = pollRosterTodos();
  const duplicateTodosRead = pollRosterTodos();
  const rosterRead = pollRoster();
  const independentCalls = calls.slice();
  fastRoster.resolve({tag: "presence-fast", seats: []});
  await rosterRead;
  await duplicateTodosRead;
  const rosterSettledFirst = rosterRenders.length === 1 &&
    rosterRenders[0].tag === "presence-fast" && pickerRenders.length === 1 &&
    ROSTER_POLLING === false && ROSTER_TODOS_POLLING === true;
  hungTodos.resolve({tag: "todos-slow", seats: [{seat: "seat-a", total: 1}]});
  await todosRead;
  results.push({name: "roster_and_todos_settle_independently", pass:
    JSON.stringify(independentCalls) === JSON.stringify(["/api/todos", "/api/chat/roster"]) &&
    rosterSettledFirst && calls.filter(x => x === "/api/todos").length === 1 &&
    ROSTER_TODOS_POLLING === false && ROSTER_TODOS["seat-a"].total === 1 &&
    orphanRenders.length === 1 && rosterRenders.length === 2,
    detail: {calls, rosterSettledFirst, rosterPolling: ROSTER_POLLING,
      todosPolling: ROSTER_TODOS_POLLING, renders: rosterRenders.length}});
  const splitDeadlines = deadlines.slice();
  results.push({name: "roster_and_todos_have_distinct_bounds", pass:
    splitDeadlines.length === 2 &&
    splitDeadlines.some(x => x.url === "/api/chat/roster" && x.ms === 8000) &&
    splitDeadlines.some(x => x.url === "/api/todos" && x.ms === 12000),
    detail: {deadlines: splitDeadlines}});

  calls.length = 0;
  deadlines.length = 0;
  LAST_ROSTER = {seats: [{seat: "requested-seat-a", session: "s-1",
    presence: "fresh", runtime: {agent_harness: "claude"}}]};
  response = configResponse("requested-seat-a", "verified-seat-a", "s-1", 13);
  const anchor = {getBoundingClientRect: () => ({left: 20, bottom: 30})};
  seatPop(anchor, "requested-seat-a", "s-1");
  const popupReads = calls.slice();
  const configButton = seatPopNode.buttons[".spcfg"];
  configButton.onclick();
  await new Promise(resolve => setTimeout(resolve, 0));
  const configReads = calls.slice();
  results.push({name: "roster_popup_is_network_silent", pass:
    popupReads.length === 0 && typeof configButton.onclick === "function",
    detail: {reads: popupReads}});
  results.push({name: "roster_deep_link_reads_config_only_after_click", pass:
    configReads[0] === "view:configs" && configReads.length === 2 &&
    configReads[1].indexOf("/api/config/injection?seat=requested-seat-a&session=s-1") === 0,
    detail: {reads: configReads}});
  results.push({name: "config_renders_requested_and_verified_identity", pass:
    detail.innerHTML.indexOf("requested seat") !== -1 &&
    detail.innerHTML.indexOf("requested-seat-a") !== -1 &&
    detail.innerHTML.indexOf("backend-verified target") !== -1 &&
    detail.innerHTML.indexOf("verified-seat-a") !== -1 &&
    detail.innerHTML.indexOf("transcript home") !== -1,
    detail: {html: detail.innerHTML}});
  results.push({name: "config_renders_state_and_versioned_units", pass:
    detail.innerHTML.indexOf("partial") !== -1 &&
    detail.innerHTML.indexOf("v3 exact UTF-8 · agent runtime") !== -1 &&
    detail.innerHTML.indexOf("13 B total") !== -1 &&
    detail.innerHTML.indexOf("v2 exact UTF-8 · explicit env") !== -1 &&
    detail.innerHTML.indexOf("v1 approximate") !== -1 &&
    detail.innerHTML.indexOf("~9 characters total") !== -1 &&
    detail.innerHTML.indexOf("config home") !== -1 &&
    detail.innerHTML.indexOf("unknown") !== -1,
    detail: {html: detail.innerHTML}});

  response = configResponse("requested-seat-a", "verified-seat-a", "s-1", 0);
  response.samples.v3_utf8 = {state: "unknown", count: 0, rendered_bytes: 0};
  response.samples.v2_utf8 = {state: "unknown", count: 0, rendered_bytes: 0};
  response.samples.v1_approx = {state: "unknown", count: 0, approx_characters: 0};
  response.unavailable = ["ledger"];
  await cfgInjection("requested-seat-a", "s-1");
  results.push({name: "config_unknown_samples_and_unavailable_stay_explicit", pass:
    detail.innerHTML.indexOf("v1 approximate</span> <b>0 unassigned historical samples</b> · unknown") !== -1 &&
    detail.innerHTML.indexOf("unavailable: ledger") !== -1 &&
    detail.innerHTML.indexOf("· none") === -1,
    detail: {html: detail.innerHTML}});

  calls.length = 0;
  deadlines.length = 0;
  const old = deferred(), fresh = deferred();
  injectionQueue.push(old, fresh);
  const oldRequest = cfgInjection("requested-old", "s-old");
  const freshRequest = cfgInjection("requested-new", "s-new");
  fresh.resolve(configResponse("requested-new", "verified-new", "s-new", 22));
  await freshRequest;
  const freshHTML = detail.innerHTML;
  old.resolve(configResponse("requested-old", "verified-old", "s-old", 99));
  await oldRequest;
  results.push({name: "config_stale_response_is_ignored", pass:
    detail.innerHTML === freshHTML &&
    detail.innerHTML.indexOf("requested-new") !== -1 &&
    detail.innerHTML.indexOf("verified-new") !== -1 &&
    detail.innerHTML.indexOf("requested-old") === -1 &&
    detail.innerHTML.indexOf("verified-old") === -1,
    detail: {html: detail.innerHTML}});
  const configDeadlines = deadlines.filter(x => x.url.indexOf("/api/config/injection?") === 0);
  results.push({name: "config_requests_have_bounded_timeout", pass:
    configDeadlines.length === 2 && configDeadlines.every(x => x.ms === 8000),
    detail: {deadlines: configDeadlines}});

  const leaving = deferred();
  injectionQueue.push(leaving);
  const abandoned = cfgInjection("requested-abandoned", "s-abandoned");
  cfgDetailClaim();
  detail.innerHTML = '<div class="cfgitem">a different Config detail owns this surface</div>';
  leaving.resolve(configResponse("requested-abandoned", "verified-abandoned", "s-abandoned", 44));
  await abandoned;
  results.push({name: "config_navigation_invalidates_pending_observation", pass:
    detail.innerHTML.indexOf("a different Config detail owns this surface") !== -1 &&
    detail.innerHTML.indexOf("requested-abandoned") === -1 &&
    detail.innerHTML.indexOf("verified-abandoned") === -1,
    detail: {html: detail.innerHTML}});

  process.stdout.write(JSON.stringify(results));
}
run().catch(err => { process.stderr.write(String(err.stack || err)); process.exit(1); });
