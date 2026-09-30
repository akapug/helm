/* The web chat folds acks: scenarios for tests/test_web_chat_ack_fold.py.
 *
 * They run in the world tests/chat_runtime_harness.js builds (its minimal
 * DOM, its faithful fake server, its shims), after the real functions the
 * driver lifts from the assembled web UI. The driver splices that harness's
 * text up to its injection marker, then the functions, then this file, so
 * pollChat and chatLoadOlder run exactly as the page runs them.
 *
 * THE SYMPTOM. A seat acked 31 rows one call at a time, 23:43-23:46Z, and the
 * owner's chat showed 31 rows of "handled in-session". An ack is a receipt:
 * consecutive DONE acks from one sender render as ONE line that counts the
 * rows they acked, whether they arrive in one poll or one per poll. */

const msg = (n, text) => ({id: "m" + n, from: "senderS", ts: "2026-09-25T23:4" + (n % 10) + ":00Z",
                           text: text || "message " + n});
const ackRow = (n, target, extra) => Object.assign(
  {id: "a" + n, from: "recipT", ts: "2026-09-25T23:43:" + String(n % 60).padStart(2, "0") + "Z",
   text: "handled in-session", ack: target, ackstate: "done"}, extra || {});
const lines = () => LOG.querySelectorAll(".chatmsg");
const receipts = () => LOG.querySelectorAll(".chatack");
const texts = () => lines().map(el => el.textContent);
function fresh() { resetClient(); SERVER.gen = "genAck" + Math.random(); }

// 31 acks, ONE PER POLL, as the incident delivered them: the receipt the
// first ack drew is the one every later ack joins.
async function scenario_acks_one_per_poll_fold_into_one_line() {
  fresh();
  SERVER.rows = [msg(0)];
  await pollChat();                                   // the windowed open
  for (let i = 0; i < 31; i++) {
    SERVER.rows = SERVER.rows.concat([ackRow(i, "t" + i)]);
    await pollChat();                                 // incremental, since=total
  }
  SERVER.rows = SERVER.rows.concat([msg(1)]);
  await pollChat();
  const r = receipts();
  return {name: "acks_one_per_poll_fold_into_one_line",
    pass: lines().length === 3 && r.length === 1 && r[0].getAttribute("data-n") === "31"
          && r[0].textContent.indexOf("31 rows") !== -1
          && r[0].textContent.indexOf("handled in-session") !== -1,
    detail: {lines: lines().length, receipts: r.length,
             n: r.length ? r[0].getAttribute("data-n") : null, texts: texts().slice(0, 4)}};
}

// the same 31 in ONE response: the reset open that paints a room holding them
async function scenario_acks_in_one_poll_fold_into_one_line() {
  fresh();
  SERVER.rows = [msg(0)].concat(Array.from({length: 31}, (_, i) => ackRow(i, "t" + i)), [msg(1)]);
  await pollChat();
  const r = receipts();
  return {name: "acks_in_one_poll_fold_into_one_line",
    pass: lines().length === 3 && r.length === 1 && r[0].getAttribute("data-n") === "31",
    detail: {lines: lines().length, receipts: r.length, texts: texts().slice(0, 4)}};
}

// one bulk row (helm chat ack <id> <id> ...) counts the ids it names
async function scenario_a_bulk_row_counts_its_ids() {
  fresh();
  const ids = Array.from({length: 31}, (_, i) => "t" + i);
  SERVER.rows = [msg(0), ackRow(0, ids[0], {acks: ids}), msg(1)];
  await pollChat();
  const r = receipts();
  return {name: "a_bulk_row_counts_its_ids",
    pass: lines().length === 3 && r.length === 1 && r[0].getAttribute("data-n") === "31",
    detail: {lines: lines().length, receipts: r.length, texts: texts()}};
}

// an older page folds its own run too
async function scenario_an_older_page_folds_its_acks() {
  fresh();
  const rows = [msg(0)].concat(Array.from({length: 5}, (_, i) => ackRow(i, "t" + i)));
  for (let i = 1; i <= 50; i++) rows.push(msg(i));
  SERVER.rows = rows;                                 // 56 rows: 6 above the window
  await pollChat();
  await chatLoadOlder();
  const r = receipts();
  return {name: "an_older_page_folds_its_acks",
    pass: lines().length === 52 && r.length === 1 && r[0].getAttribute("data-n") === "5",
    detail: {lines: lines().length, receipts: r.length}};
}

// CONTROLS. A single ack still shows; a blocked ack, another sender and a
// message between acks each break the run. These pass on the unfolded page as
// well, which is the point: the fold may hide nothing that was a line of its own.
async function scenario_a_single_ack_still_shows() {
  fresh();
  SERVER.rows = [msg(0), ackRow(0, "t0"), msg(1)];
  await pollChat();
  const t = texts();
  return {name: "a_single_ack_still_shows",
    pass: t.length === 3 && t[1].indexOf("handled in-session") !== -1 && t[1].indexOf("recipT") !== -1,
    detail: {texts: t}};
}

// `acks` extends an ack row; without its singular discriminator it is ordinary
// malformed conversation and must not collapse into an invisible receipt.
async function scenario_acks_without_ack_do_not_fold() {
  fresh();
  SERVER.rows = [msg(0), {id: "bad", from: "recipT", ts: "2026-09-25T23:43:00Z",
                          text: "not a receipt", acks: ["t0", "t1"], ackstate: "done"}, msg(1)];
  await pollChat();
  const t = texts();
  return {name: "acks_without_ack_do_not_fold",
    pass: t.length === 3 && receipts().length === 0 && t[1].indexOf("not a receipt") !== -1,
    detail: {texts: t, receipts: receipts().length}};
}

// `acks` is trusted only when its first id is `ack`, as the writer stores it
// (chat.post); a list led by another id names only `ack` (chat.ack_ids).
async function scenario_acks_led_by_another_id_name_only_ack() {
  fresh();
  SERVER.rows = [msg(0), ackRow(0, "t0", {acks: ["t9", "t8", "t7"]}), msg(1)];
  await pollChat();
  const r = receipts();
  return {name: "acks_led_by_another_id_name_only_ack",
    pass: lines().length === 3 && r.length === 1 && r[0].getAttribute("data-n") === "1",
    detail: {lines: lines().length, receipts: r.length,
             n: r.length ? r[0].getAttribute("data-n") : null, texts: texts()}};
}

async function scenario_what_breaks_a_run() {
  fresh();
  SERVER.rows = [ackRow(0, "t0"),
                 ackRow(1, "t1", {ackstate: "blocked", text: "waiting on creds"}),
                 ackRow(2, "t2"),
                 ackRow(3, "t3", {from: "recipU"}),
                 ackRow(4, "t4"),
                 msg(0),
                 ackRow(5, "t5")];
  await pollChat();
  const t = texts();
  return {name: "what_breaks_a_run",
    pass: t.length === 7 && t[1].indexOf("waiting on creds") !== -1 && t[3].indexOf("recipU") !== -1,
    detail: {texts: t}};
}

(async () => {
  const results = [];
  for (const s of [scenario_acks_one_per_poll_fold_into_one_line,
                   scenario_acks_in_one_poll_fold_into_one_line,
                   scenario_a_bulk_row_counts_its_ids,
                   scenario_an_older_page_folds_its_acks,
                   scenario_a_single_ack_still_shows,
                   scenario_acks_without_ack_do_not_fold,
                   scenario_acks_led_by_another_id_name_only_ack,
                   scenario_what_breaks_a_run]) {
    try { results.push(await s()); }
    catch (e) { results.push({name: s.name.replace(/^scenario_/, ""), pass: false,
                              detail: {error: String(e && e.stack || e)}}); }
  }
  process.stdout.write(JSON.stringify(results));
  process.exit(results.every(r => r.pass) ? 0 : 1);
})();
