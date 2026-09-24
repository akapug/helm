/* Runtime contract for WHO the owner's web posts are sent as.
 *
 * The page puts no name on a post the owner did not type: not in its name box,
 * not on a reaction, not on a ledger seat message. These scenarios run the REAL initChat,
 * sendChat, sendReact, sendSeatMsg, chatRow and chatOwnerStamp, lifted verbatim
 * from the assembled web UI by tests/test_web_chat_name_runtime.py and spliced
 * at the marker below, against a recording `post` and a minimal DOM.
 *
 * Three states of the server's configuration, as the page meets them:
 *   configured   the reset open carried owner_name (pollChat stamps it)
 *   unnamed      the reset open carried no owner_name (the server could not
 *                resolve one)
 *   unread       the poll failed or answered unavailable, so nothing was
 *                stamped at all
 * and three paths: the composer, the reply (a threaded post and a reaction),
 * and the ledger's message-a-seat card. Plain CommonJS so the spliced
 * declarations share this module scope. */

class Input { constructor() { this.value = ""; this.placeholder = ""; } }
const LOGEL = {scrollTop: 0, scrollHeight: 0};
let NAME = new Input(), TEXT = new Input(), SEATKEY = new Input(), SEATLINE = new Input();
let SEATMSG = {className: "", textContent: ""};
function $(sel) {
  return {"#chatname": NAME, "#chattext": TEXT, "#seatkey": SEATKEY,
          "#seatline": SEATLINE, "#seatmsg": SEATMSG, "#chatlog": LOGEL}[sel] || null;
}
const STORE = new Map();
const localStorage = {
  getItem: k => (STORE.has(k) ? STORE.get(k) : null),
  setItem: (k, v) => { STORE.set(k, String(v)); },
  removeItem: k => { STORE.delete(k); },
};
const POSTS = [];
async function post(url, body) {
  POSTS.push({url, body: JSON.parse(JSON.stringify(body))});
  return {ok: true};
}
async function pollChat() {}
function toast() {}
function closeSeatPicker() {}
let CHAT_REPLY_TO = null;
function chatClearReply() { CHAT_REPLY_TO = null; }
let CHAT_ROOM = "main", CHAT_WIRED = true;   // wired: initChat skips its listeners
// chatRow's cosmetic dependencies. The subject there is the owner mark only.
const CHAT_RSET = ["+1"], CHAT_CLAMP_CHARS = 1200, CHAT_CLAMP_LINES = 14;
function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
function chatIndex() {}
function chatKey(m) { return (m.ts || "") + "|" + (m.from || ""); }
function chatThreadKey(m) { return chatKey(m); }
function chatQuote() { return ""; }
function dayStamp() { return ""; }

/*__INJECT__*/

function freshPage() {
  STORE.clear(); POSTS.length = 0;
  NAME = new Input(); TEXT = new Input(); SEATKEY = new Input(); SEATLINE = new Input();
  SEATMSG = {className: "", textContent: ""};
  CHAT_REPLY_TO = null; CHAT_OWNER = "";
}
// What the page learns from the server, per state. pollChat calls
// chatOwnerStamp(d) with each good envelope; a failed or unavailable poll
// returns before it, so the unread state stamps nothing.
const STATES = {
  configured: () => chatOwnerStamp({lines: [], base: 0, owner_name: "harbor"}),
  unnamed: () => chatOwnerStamp({lines: [], base: 0}),
  unread: () => {},
};
const marked = who => /class="chatmsg owner"/.test(chatRow({from: who, ts: "t1", text: "x"}));
const names = () => POSTS.map(p => (Object.prototype.hasOwnProperty.call(p.body, "name")
                                    ? p.body.name : null));

async function runState(state) {
  freshPage();
  initChat();
  STATES[state]();
  const shown = {value: NAME.value, placeholder: NAME.placeholder};
  // composer
  TEXT.value = "hello"; await sendChat();
  // reply path: a threaded post, then a reaction on a row
  CHAT_REPLY_TO = "row-1"; TEXT.value = "re"; await sendChat();
  await sendReact({dataset: {ts: "t0", from: "seat-a"}}, "+1");
  // the ledger's message-a-seat card: a DM, then a broadcast
  SEATKEY.value = "seat-a"; SEATLINE.value = "ping";
  await sendSeatMsg({preventDefault() {}});
  SEATKEY.value = ""; SEATLINE.value = "all hands";
  await sendSeatMsg({preventDefault() {}});
  return {
    shown, sent: names(), urls: POSTS.map(p => p.url),
    replyTo: POSTS[1] && POSTS[1].body.reply_to,
    stored: Object.fromEntries(STORE),
    ownerMarked: marked("harbor"),
    wordMarked: marked("owner"),
  };
}

async function runTyped() {
  freshPage();
  initChat();
  STATES.configured();
  NAME.value = "skipper";
  TEXT.value = "hello"; await sendChat();
  CHAT_REPLY_TO = "row-1"; TEXT.value = "re"; await sendChat();
  await sendReact({dataset: {ts: "t0", from: "seat-a"}}, "+1");
  SEATKEY.value = "seat-a"; SEATLINE.value = "ping";
  await sendSeatMsg({preventDefault() {}});
  const typedStore = Object.fromEntries(STORE);
  const typedMarked = marked("skipper"), serverNameMarked = marked("harbor");
  // a reload keeps what he typed
  const kept = (() => { NAME = new Input(); initChat(); return NAME.value; })();
  // he empties the box and sends: back to the server's name, and forgotten
  NAME.value = ""; TEXT.value = "plain"; await sendChat();
  return {sent: names(), typedStore, typedMarked, serverNameMarked, kept,
          afterClear: Object.fromEntries(STORE)};
}

async function runLegacy() {
  // a key an earlier page wrote on every send, its own default included
  freshPage();
  STORE.set("helm.chatname", "somebody");
  initChat();
  const value = NAME.value, left = Object.fromEntries(STORE);
  SEATKEY.value = "seat-a"; SEATLINE.value = "ping";
  await sendSeatMsg({preventDefault() {}});
  return {value, left, sent: names()};
}

(async () => {
  const out = [];
  for (const s of Object.keys(STATES)) out.push({name: "state_" + s, detail: await runState(s)});
  out.push({name: "typed", detail: await runTyped()});
  out.push({name: "legacy", detail: await runLegacy()});
  process.stdout.write(JSON.stringify(out));
})().catch(e => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
