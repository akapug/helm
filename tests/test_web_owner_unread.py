#!/usr/bin/env python3
"""helm.web agent→owner unread signal — hermetic contract tests. Every GET
/api/chat carries {owner_read, owner_unread, owner_mentions} (the nav badge on
EVERY tab): rows past the owner's last-read cursor, and the subset that
@-mention an owner name (seats.owner_names — the delivery filter's owner
rule). POST /api/chat/read is the owner's read-ack (bearer-gated like every
mutation). Fail-open law: any surprise answers zeros — no badge, never an
error. Tmp HELM_HOME + HELM_CHAT_DIR; the real ~/.helm and /dev/shm/helm-chat
are never touched."""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import chat, seats, web  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NODE_URL", "MELD_CHAT_NODE_URL",
            "HELM_CELL_BIN", "MELD_CELL_BIN",
            "HELM_CHAT_OWNER_NAMES", "MELD_CHAT_OWNER_NAMES",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_ROOM")


class TestOwnerUnread(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="helm-test-ownerunread-")
        cls.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        cls.owner_prior = seats._OWNER_NAME
        seats._OWNER_NAME = "daria"
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        # The fixture homes its posts EXPLICITLY through the same env seam
        # launched seats use — the old accidental "main" default, now stated
        # (chat.post's room default derives from env/cwd since 2026-07-29;
        # this fixture previously neither popped nor set the room seam, so
        # alone it inherited whatever the shell or cwd implied).
        os.environ["HELM_CHAT_ROOM"] = "main"
        os.environ["HELM_HOME"] = os.path.join(cls.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(cls.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""  # transport off — hermetic v1
        cls.srv = web.make_server(0)
        cls.port = cls.srv.server_address[1]
        # shutdown() waits one serve_forever poll (stdlib 0.5s)
        cls.thread = threading.Thread(target=cls.srv.serve_forever,
                                      kwargs={"poll_interval": 0.01}, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.thread.join(timeout=5)
        for k, v in cls.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        seats._OWNER_NAME = cls.owner_prior
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        shutil.rmtree(os.environ["HELM_CHAT_DIR"], ignore_errors=True)
        os.environ.pop("HELM_CHAT_OWNER_NAMES", None)

    def req(self, path, payload=None, token=True):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        if data and token:
            headers["Authorization"] = "Bearer " + web.MUTATION_TOKEN
        r = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"null")

    def signal(self):
        status, d = self.req("/api/chat?room=main&since=0")
        self.assertEqual(status, 200)
        return d

    # ── the signal rides every poll ──

    def test_empty_room_answers_zeros(self):
        d = self.signal()
        self.assertEqual((d["owner_read"], d["owner_unread"], d["owner_mentions"]),
                         (0, 0, 0))
        self.assertNotIn("owner_mention_last", d)

    def test_mentions_past_cursor_and_read_ack(self):
        chat.post("@daria ship it?", who="fable-a")
        chat.post("agent chatter, no address", who="fable-a")
        m2 = chat.post("@daria second call", who="codex-b")
        d = self.signal()
        self.assertEqual(d["owner_unread"], 3)   # every agent row is unread
        self.assertEqual(d["owner_mentions"], 2)  # two address the owner
        # dedup key + preview name the NEWEST unseen mention (the decision)
        self.assertEqual(d["owner_mention_last"], "%s|%s" % (m2["ts"], "codex-b"))
        self.assertEqual(d["owner_mention_preview"], "codex-b: @daria second call")
        # the owner read the room (chat view open + visible) — cursor advances
        status, ack = self.req("/api/chat/read", {"room": "main"})
        self.assertEqual(status, 200)
        self.assertEqual(ack["owner_read"], 3)
        d = self.signal()
        self.assertEqual((d["owner_read"], d["owner_unread"], d["owner_mentions"]),
                         (3, 0, 0))
        # the next mention is a fresh decision past the moved cursor
        chat.post("@daria again", who="fable-a")
        d = self.signal()
        self.assertEqual((d["owner_unread"], d["owner_mentions"]), (1, 1))

    def test_read_ack_demands_the_bearer(self):
        chat.post("@daria psst", who="fable-a")
        status, _ = self.req("/api/chat/read", {"room": "main"}, token=False)
        self.assertEqual(status, 403)
        self.assertEqual(self.signal()["owner_mentions"], 1)  # cursor unmoved

    # ── owner-name matching (the delivery filter's owner rule) ──

    def test_name_matching_boundaries_and_case(self):
        chat.post("@Daria case-insensitive", who="fable-a")
        chat.post("@dariax is a different seat", who="fable-a")
        chat.post("mail daria@example.com today", who="fable-a")  # not an @mention
        chat.post("@all broadcast is fleet noise, not an owner call", who="fable-a")
        d = self.signal()
        self.assertEqual(d["owner_unread"], 4)
        self.assertEqual(d["owner_mentions"], 1)

    def test_owner_names_env_override(self):
        os.environ["HELM_CHAT_OWNER_NAMES"] = "skipper"
        chat.post("@skipper aye", who="fable-a")
        chat.post("@daria is not the owner here", who="fable-a")
        d = self.signal()
        self.assertEqual(d["owner_mentions"], 1)

    def test_an_empty_override_falls_back_instead_of_erasing_the_owner(self):  # noqa: VACUOUS_ASSERTION — the absence-shaped assertion is the FINAL must-miss (a real override 'skipper' yields owner_mentions 0, proving the fix repaired the empty case rather than deleting the override feature). Its unconditional positive controls sit on the SAME observable earlier in the same test and are all NON-ZERO: owner_mentions == 2 with no override, with "", and with " , , ". A build that ignored the override entirely would fail that last line.
        """An exported-but-EMPTY HELM_CHAT_OWNER_NAMES is a misconfiguration,
        never a statement that the owner has no names. The old guard was
        `raw is not None`, so "" took the override branch and returned an
        EMPTY SET — and empty is not inert: _owner_signal builds its mention
        regex as `... if names else None`, so every @-mention of the owner
        stopped counting while owner_unread stayed correct. A quiet,
        owner-facing zero on his own badge.

        Both poles asserted on the SAME observable, and both NON-ZERO on the
        mention count, because a zero here is exactly what the bug produced.
        """
        chat.post("@daria one", who="fable-a")
        chat.post("@daria two", who="fable-a")
        # POSITIVE CONTROL: with no override at all, both mentions count.
        os.environ.pop("HELM_CHAT_OWNER_NAMES", None)
        self.assertEqual(self.signal()["owner_mentions"], 2)
        # the bug's input — an exported empty string
        os.environ["HELM_CHAT_OWNER_NAMES"] = ""
        d = self.signal()
        self.assertEqual(d["owner_mentions"], 2,
                         "an empty override erased the owner's mention badge")
        self.assertEqual(d["owner_unread"], 2)
        # whitespace/commas that name nobody are the same misconfiguration
        os.environ["HELM_CHAT_OWNER_NAMES"] = " , , "
        self.assertEqual(self.signal()["owner_mentions"], 2)
        # MUST-MISS: a REAL override still wins, or this fix would have just
        # deleted the feature rather than repaired its empty case.
        os.environ["HELM_CHAT_OWNER_NAMES"] = "skipper"
        self.assertEqual(self.signal()["owner_mentions"], 0)

    def test_owner_rail_posts_never_badge_the_owner(self):
        # the owner's own web post — even one naming himself — is not a call
        status, _ = self.req("/api/chat", {"text": "@daria note to self"})
        self.assertEqual(status, 200)
        d = self.signal()
        self.assertEqual((d["owner_unread"], d["owner_mentions"]), (0, 0))
        # a CLI post CLAIMING the owner name (no rail origin) stays an
        # ordinary row — counting it is the impersonation fail-safe
        chat.post("@daria trust me, it's me", who="daria")
        d = self.signal()
        self.assertEqual((d["owner_unread"], d["owner_mentions"]), (1, 1))

    def test_reactions_never_badge(self):
        chat.post("plain row", who="fable-a")
        row, err = chat.react(1, ":tada:", who="fable-b")
        self.assertIsNone(err)
        d = self.signal()
        self.assertEqual(d["owner_unread"], 1)   # the message, not the react
        self.assertEqual(d["owner_mentions"], 0)

    # ── cursor durability + fail-open ──

    def test_cursor_rid_survives_rotation(self):
        chat.post("old row", who="fable-a")
        keep = chat.post("kept row", who="fable-a")
        self.req("/api/chat/read", {"room": "main"})
        # the oldest half rotates out under the cursor — the rid re-anchors it
        with open(chat.room_path("main"), encoding="utf-8") as f:
            lines = [x for x in f.read().split("\n") if x]
        self.assertEqual(json.loads(lines[1])["id"], keep["id"])
        with open(chat.room_path("main"), "w", encoding="utf-8") as f:
            f.write(lines[1] + "\n")
        chat.post("@daria fresh call", who="fable-a")
        d = self.signal()
        self.assertEqual((d["owner_read"], d["owner_unread"], d["owner_mentions"]),
                         (1, 1, 1))

    def test_corrupt_cursor_fails_open_to_zero(self):
        chat.post("@daria hello", who="fable-a")
        with open(web._owner_read_path("main"), "w", encoding="utf-8") as f:
            f.write("not json {")
        d = self.signal()
        self.assertEqual((d["owner_read"], d["owner_mentions"]), (0, 1))

    def test_signal_failure_answers_zeros_never_an_error(self):
        chat.post("@daria hello", who="fable-a")
        real = seats.owner_names
        seats.owner_names = None  # not callable — the signal leg raises
        try:
            d = self.signal()
        finally:
            seats.owner_names = real
        # the poll still answers: rows + total intact, the signal zeroed
        self.assertEqual(d["total"], 1)
        self.assertEqual((d["owner_unread"], d["owner_mentions"]), (0, 0))


    def test_a_padded_sha_post_is_a_400_never_a_500(self):
        """shaguard refuses a padded short sha and chat.post RAISES. That is a
        fact about the REQUEST, and this handler already answers bad payloads
        with 400 — letting the raise escape would turn a precise, actionable
        refusal into a server fault the owner's panel cannot read."""
        import subprocess
        real = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                              text=True).stdout.strip()
        if len(real) != 40:
            self.skipTest("not a git checkout")
        # CONTROL: an ordinary post really works through this handler, so the
        # 400 below is the guard and not a broken endpoint.
        ok_status, _ok = self.req("/api/chat", {"text": "plain message"})
        self.assertEqual(ok_status, 200)
        status, body = self.req("/api/chat", {"text": "tip " + real[:12] + "9" * 28})
        self.assertEqual(status, 400, "a refusal is a bad REQUEST, not a fault")
        self.assertIn("padded short sha", str(body.get("error")))


class TestReadAckWaitsForHisHand(unittest.TestCase):
    """CONSOLE WALK 3, FINDING 25 (task/3723, P1 9): a read-only walk opened
    Chat and the page's own poll posted the owner's read-ack, so 97 unread
    in #main were consumed by a reader who was not him. The ack now waits for
    his hand: a real gesture (a click, a key or a scroll of his own, which
    the browser marks `isTrusted`) on the Chat view while the page is
    focused and visible. A poll, the page loading onto Chat, a programmatic
    navigation to it or a script's synthetic event never posts it, and one
    gesture posts it once. The page's own `chatSignal`, `chatAckDue` and
    `chatSeen` run under node over stand-ins for the DOM and the post."""

    DRIVER = r"""
const POSTS = [];
let ON = false, FOCUS = true, VIS = "visible", CHAT_ROOM = "main";
const INSIDE = {id: "chatlog"}, OUTSIDE = {id: "navtab"};
const VIEW = {classList: {contains: k => k === "on" && ON}, contains: t => t === INSIDE};
const document = {get hidden() { return VIS !== "visible"; }, get visibilityState() { return VIS; },
                  hasFocus: () => FOCUS, title: ""};
function $(sel) { return sel === "#view-chat" ? VIEW : null; }
function post(url, body) { POSTS.push([url, body]); return Promise.resolve({owner_read: 97}); }
let NAV_CHAT = 0, NAV_CHAT_MENTION = false, CHAT_SIGNAL_READ = false, CHAT_NOTIFY_KEY = "";
let CHAT_UNSEEN = null, CHAT_ACKED = null;
function homeRender() {} function renderNav() {} function faviconDot() {} function notifyOwner() {} function dashAnswers() {}
/*__INJECT__*/
const body = n => ({total: n, owner_read: 0, owner_unread: n, owner_mentions: 0});
const out = {};
ON = true;                                   // the page loaded onto #chat
chatSignal(body(97));
out.load = POSTS.length;
ON = false; chatSignal(body(97)); ON = true; // showView("chat") by code, then a poll
chatSignal(body(97));
chatSeen({isTrusted: false, target: INSIDE, type: "click"});   // a script's el.click()
out.programmatic = POSTS.length;
chatSeen({isTrusted: true, target: OUTSIDE, type: "pointerdown"});
out.outside = POSTS.length;
FOCUS = false; chatSeen({isTrusted: true, target: INSIDE, type: "wheel"}); FOCUS = true;
VIS = "hidden"; chatSeen({isTrusted: true, target: INSIDE, type: "keydown"}); VIS = "visible";
out.unfocused = POSTS.length;
chatSeen({isTrusted: true, target: INSIDE, type: "pointerdown"});
chatSeen({isTrusted: true, target: INSIDE, type: "wheel"});   // the same hand, before the next poll
out.gesture = POSTS.slice();
chatSignal({total: 97, owner_read: 97, owner_unread: 0, owner_mentions: 0});
chatSeen({isTrusted: true, target: INSIDE, type: "wheel"});   // nothing unread: nothing to ack
out.read = POSTS.length;
chatSignal({total: 98, owner_read: 97, owner_unread: 1, owner_mentions: 0});
out.newRow = POSTS.length;
chatSeen({isTrusted: true, target: INSIDE, type: "keydown"});
out.newGesture = POSTS.length;
console.log(JSON.stringify(out));
"""

    @classmethod
    def setUpClass(cls):
        import subprocess as sp
        from helm import web_ui_loader
        from tests.test_web_chat_client_runtime import _extract_fn
        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("node not available")
        cls.src = web_ui_loader.read_text()
        fns = "\n".join(_extract_fn(cls.src, n)
                        for n in ("chatSignal", "chatAckDue", "chatSeen"))
        tmp = tempfile.mkdtemp(prefix="helm-test-readack-")
        try:
            path = os.path.join(tmp, "run.js")
            with open(path, "w", encoding="utf-8") as f:
                f.write(cls.DRIVER.replace("/*__INJECT__*/", fns))
            proc = sp.run([node, path], capture_output=True, text=True,
                          timeout=60)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        assert proc.returncode == 0, proc.stderr[:3000]
        cls.out = json.loads(proc.stdout)

    def test_opening_chat_on_load_never_acks(self):
        self.assertEqual(self.out["load"], 0)
        # the positive control: the same run's gesture did post
        self.assertEqual(len(self.out["gesture"]), 1)

    def test_a_programmatic_navigation_or_a_synthetic_event_never_acks(self):
        self.assertEqual(self.out["programmatic"], 0)
        self.assertEqual(self.out["outside"], 0)
        self.assertEqual(self.out["unfocused"], 0)
        self.assertEqual(len(self.out["gesture"]), 1)       # the positive control

    def test_a_real_gesture_acks_once(self):
        self.assertEqual(self.out["gesture"],
                         [["/api/chat/read", {"room": "main"}]])
        self.assertEqual(self.out["read"], 1)
        # a new row is unread until his hand moves again
        self.assertEqual(self.out["newRow"], 1)
        self.assertEqual(self.out["newGesture"], 2)

    def test_the_ack_is_posted_only_by_the_gesture_listener(self):
        self.assertEqual(self.src.count('post("/api/chat/read"'), 1)
        from tests.test_web_chat_client_runtime import _extract_fn
        self.assertIn('post("/api/chat/read"', _extract_fn(self.src, "chatSeen"))
        self.assertNotIn("/api/chat/read", _extract_fn(self.src, "chatSignal"))
        self.assertIn('["pointerdown", "keydown", "wheel", "touchmove"].forEach(k => '
                      "document.addEventListener(k, chatSeen, {capture: true, passive: true}));",
                      self.src)


if __name__ == "__main__":
    unittest.main()
