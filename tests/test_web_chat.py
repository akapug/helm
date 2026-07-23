#!/usr/bin/env python3
"""helm.web chat surface — hermetic contract tests. GET /api/chat is the poll
read (open on loopback, like every GET); POST /api/chat is the OWNER's post —
it demands the mutation bearer (403 without) and drops the owner-unread marker
the shipped reflex fires on. Tmp HELM_HOME + HELM_CHAT_DIR; the real ~/.helm
and /dev/shm/helm-chat are never touched."""
import contextlib
import io
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

from helm import chat, web  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NODE_URL", "MELD_CHAT_NODE_URL",
            "HELM_CELL_BIN", "MELD_CELL_BIN")


class TestWebChat(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="helm-test-webchat-")
        cls.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(cls.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(cls.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""  # transport off — hermetic v1
        # cwd hermeticity: the CLI default room derives from a git cwd
        # (seats.resolve_homing) — run from tmp so defaults stay 'main'
        cls.cwd_prior = os.getcwd()
        os.chdir(cls.tmp)
        cls.srv = web.make_server(0)  # ephemeral port
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.thread.join(timeout=5)
        os.chdir(cls.cwd_prior)
        for k, v in cls.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        # each test starts with an empty room dir (rooms are per-test state)
        shutil.rmtree(os.environ["HELM_CHAT_DIR"], ignore_errors=True)

    def req(self, path, payload=None, token=True):
        """(status, obj) — 4xx/5xx returned, not raised. payload -> POST;
        token=False drops the bearer (the CSRF-shaped request)."""
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

    def test_get_empty_room(self):
        status, d = self.req("/api/chat")
        self.assertEqual(status, 200)
        self.assertEqual(d["room"], "main")
        self.assertEqual((d["lines"], d["total"]), ([], 0))
        # the transport truth rides every poll: disabled env -> unsigned, no url
        self.assertEqual(d["transport"],
                         {"mode": "unsigned", "url": None, "head": None,
                          "signer": False})

    def test_rooms_sidebar_lists_channels_with_cross_room_signal(self):
        """slice-1: /api/chat carries `rooms` (the channel sidebar) with a
        per-room owner signal, so a mention in a NON-current room is visible
        for the summed badge — the fix for the single-room-invisible hole."""
        from helm import chat
        self.req("/api/chat", {"text": "hello main"})            # owner in main
        chat.post("@david urgent", room="team-fe", who="codex")  # agent elsewhere
        status, d = self.req("/api/chat")                        # viewing main
        self.assertEqual(status, 200)
        rooms = {r["room"]: r for r in d["rooms"]}
        self.assertIn("main", rooms)
        self.assertIn("team-fe", rooms)
        self.assertEqual(rooms["main"]["total"], 1)
        self.assertGreaterEqual(rooms["team-fe"]["owner_mentions"], 1)

    def test_react_endpoint_roundtrip(self):
        self.req("/api/chat", {"text": "ship it"})
        status, d = self.req("/api/chat?since=0")
        target = d["lines"][0]
        status, d = self.req("/api/chat/react",
                             {"emoji": ":tada:", "tts": target["ts"],
                              "tfrom": target["from"], "name": "david"})
        self.assertEqual(status, 200)
        self.assertEqual(d["msg"]["react"], "🎉")
        self.assertEqual(d["msg"]["tfrom"], "david")
        status, d = self.req("/api/chat?since=0")
        self.assertEqual(d["total"], 2)  # reaction rows ride the same poll
        self.assertEqual(d["lines"][1]["react"], "🎉")

    def test_react_endpoint_rejects_bad_payloads(self):
        for payload in ({}, {"emoji": ":tada:"},
                        {"emoji": ":nope-such:", "tts": "x", "tfrom": "y"}):
            status, d = self.req("/api/chat/react", payload)
            self.assertEqual(status, 400, payload)
            self.assertIn("error", d)

    def test_post_demands_the_bearer(self):
        status, d = self.req("/api/chat", {"text": "sneak"}, token=False)
        self.assertEqual(status, 403)
        self.assertFalse(os.path.exists(chat.room_path("main")))
        self.assertFalse(os.path.exists(chat.marker_path("main")))

    def test_owner_post_appends_marks_and_reads_back(self):
        status, d = self.req("/api/chat", {"text": "  morning, fleet  "})
        self.assertEqual(status, 200)
        self.assertTrue(d["ok"])
        self.assertEqual(d["total"], 1)
        self.assertEqual(d["msg"]["from"], "david")          # the default name
        self.assertEqual(d["msg"]["text"], "morning, fleet")  # trimmed
        with open(chat.marker_path("main")) as f:
            self.assertEqual(f.read(), "1")  # marker carries the post-time count
        status, d = self.req("/api/chat?room=main&since=0")
        self.assertEqual(status, 200)
        self.assertEqual(d["total"], 1)
        self.assertEqual(d["lines"][0]["text"], "morning, fleet")
        # the web GET is the owner's own poll — it must NOT clear the marker
        self.assertTrue(os.path.exists(chat.marker_path("main")))

    def test_post_name_field_and_room(self):
        status, d = self.req("/api/chat", {"text": "yo", "name": "skipper",
                                          "room": "ops"})
        self.assertEqual(status, 200)
        self.assertEqual(d["msg"]["from"], "skipper")
        self.assertEqual(chat.read("ops")[1], 1)
        self.assertTrue(os.path.exists(chat.marker_path("ops")))

    def test_post_rejects_empty_text(self):
        for payload in ({}, {"text": "   "}, {"text": 7}):
            status, d = self.req("/api/chat", payload)
            self.assertEqual(status, 400, payload)
            self.assertIn("error", d)

    def test_get_since_and_bad_since(self):
        chat.post("one", who="a1")
        chat.post("two", who="a2")
        status, d = self.req("/api/chat?since=1")
        self.assertEqual(status, 200)
        self.assertEqual([m["text"] for m in d["lines"]], ["two"])
        self.assertEqual(d["total"], 2)
        status, d = self.req("/api/chat?since=x")
        self.assertEqual(status, 400)

    def test_dm_endpoint_is_a_true_dm_never_a_room_post(self):
        """The ledger 'message a seat' card's fixed route: POST /api/chat/dm
        hits ONE recipient's private lane — the old path posted '@seat …'
        into #main and called it a DM (owner-flagged)."""
        from helm import seats
        seats.join(session="s-web-dm", seat="codex", cwd="/tmp/p")
        status, d = self.req("/api/chat/dm", {"to": "codex", "text": " go ",
                                              "name": "david"})
        self.assertEqual(status, 200)
        self.assertTrue(d["ok"])
        self.assertEqual(d["msg"]["dm"], "codex")
        self.assertEqual(d["msg"]["text"], "go")
        # NO room fanout: no channel appears, #main empty, no owner marker
        self.assertEqual(chat.list_rooms(), [])
        self.assertEqual(chat.read("main")[1], 0)
        self.assertFalse(os.path.exists(chat.marker_path("main")))
        # the recipient's delivery lane surfaces it as a DM
        line = seats.deliver_any(session="s-web-dm", seat="codex")
        self.assertIn("go", line)
        self.assertIn("dm", line)
        # bad payloads answer 400; the bearer is demanded like every POST
        status, d = self.req("/api/chat/dm", {"to": "codex", "text": "  "})
        self.assertEqual(status, 400)
        status, d = self.req("/api/chat/dm", {"to": "no such!", "text": "x"})
        self.assertEqual(status, 400)
        status, _d = self.req("/api/chat/dm", {"to": "codex", "text": "y"},
                              token=False)
        self.assertEqual(status, 403)

    def test_cli_read_clears_the_web_marker(self):
        # the full notify loop, endpoints-to-CLI: owner posts via web -> marker
        # -> an agent's `helm chat read` consumes past it -> cleared
        self.req("/api/chat", {"text": "anyone up?"})
        self.assertTrue(os.path.exists(chat.marker_path("main")))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(chat.cmd_chat(["read"]), 0)
        self.assertIn("david: anyone up?", out.getvalue())
        self.assertFalse(os.path.exists(chat.marker_path("main")))

    # ── threading + the sidebar's activity signal (the owner surface) ──

    def test_owner_reply_threads_through_the_endpoint(self):
        """The web reply, end to end: POST carries reply_to, the row that comes
        back off the poll carries the parent's id AND identity."""
        self.req("/api/chat", {"text": "the parent post"})
        parent = self.req("/api/chat?since=0")[1]["lines"][0]
        status, d = self.req("/api/chat", {"text": "the threaded answer",
                                           "reply_to": parent["id"]})
        self.assertEqual(status, 200)
        self.assertEqual(d["msg"]["reply_to"], parent["id"])
        rows = self.req("/api/chat?since=0")[1]["lines"]
        self.assertEqual(rows[1]["reply_to"], parent["id"])
        self.assertEqual((rows[1]["rts"], rows[1]["rfrom"]),
                         (parent["ts"], parent["from"]))
        self.assertEqual(rows[0].get("reply_to"), None)   # the parent is plain

    def test_reply_to_a_rotated_out_parent_is_served_not_refused(self):
        status, d = self.req("/api/chat", {"text": "orphan", "reply_to": "beef99"})
        self.assertEqual(status, 200)
        self.assertEqual(d["msg"]["reply_to"], "beef99")
        self.assertNotIn("rts", d["msg"])

    def test_a_web_reply_wakes_the_parent_rows_author(self):
        """The beacon law, asserted at the ENDPOINT (inverted 2026-07-22 —
        replying replaces typing the @mention): the reply row the web writes
        carries the parent's identity and wakes EXACTLY the parent's author —
        the text alone would have woken nobody, and a bystander seat decides
        identically with and without the pointer."""
        from helm import seats
        seats.write_roster("codex", session="s-codex")
        seats.write_roster("kimi", session="s-kimi")
        self.req("/api/chat", {"text": "seed", "name": "codex"})
        p = self.req("/api/chat?since=0")[1]["lines"][0]
        self.req("/api/chat", {"text": "no mention here", "reply_to": p["id"]})
        row = self.req("/api/chat?since=0")[1]["lines"][1]
        plain = {k: v for k, v in row.items()
                 if k not in ("reply_to", "rts", "rfrom")}
        self.assertTrue(seats.deliverable(row, "codex", "main"))
        self.assertFalse(seats.deliverable(plain, "codex", "main"))
        self.assertFalse(seats.deliverable(row, "kimi", "main"))
        self.assertEqual(seats.deliverable(row, "kimi", "main"),
                         seats.deliverable(plain, "kimi", "main"))

    def test_reply_click_seeds_the_composer_with_the_authors_at(self):
        """The reply affordance's visible face (owner ask 2026-07-22): the
        endpoint half threads the payload's reply_to, and the SERVED page
        carries the seed-@ mechanism — chatSetReply feeds the parent's author
        into chatSeedMention, which prepends "@author " to the composer and
        refuses a duplicate. Asserted against the mechanism's own statements,
        not a comment."""
        # endpoint half: the payload the seeded composer sends threads
        self.req("/api/chat", {"text": "parent", "name": "codex"})
        p = self.req("/api/chat?since=0")[1]["lines"][0]
        d = self.req("/api/chat", {"text": "@codex on it",
                                   "reply_to": p["id"]})[1]
        self.assertEqual(d["msg"]["reply_to"], p["id"])
        # template half: GET / (served fresh, token templated) — the click
        # handler seeds with the PARENT'S author...
        url = "http://127.0.0.1:%d/" % self.port
        with urllib.request.urlopen(url, timeout=10) as resp:
            html = resp.read().decode("utf-8")
        self.assertIn('chatSeedMention(String(p.from || '
                      'div.dataset.from || "").trim())', html)
        # ...the seed is the @-prefixed author PREPENDED to the composer...
        self.assertIn('CHAT_REPLY_SEED = "@" + author + " "', html)
        self.assertIn("el.value = CHAT_REPLY_SEED + el.value", html)
        # ...an already-typed @author is never doubled (the dedupe regex)...
        self.assertIn(r'new RegExp("(^|\\s)@" + author.replace', html)
        # ...and cancel strips only the untouched seed (removable, not sticky)
        self.assertIn("el.value.startsWith(CHAT_REPLY_SEED)", html)

    def test_poll_carries_the_live_roster_for_mention_completion(self):
        from helm import seats
        seats.write_roster("goodtimes-platform-codex", session="s-1")
        seats.write_roster("helm-opus-integrator", session="s-2")
        d = self.req("/api/chat")[1]
        self.assertEqual(d["roster"],
                         ["goodtimes-platform-codex", "helm-opus-integrator"])

    def test_sidebar_rooms_carry_unread_mentions_age_and_seat_presence(self):
        """The badge computation the owner UX rides: a quiet room and a busy
        one are distinguishable WITHOUT opening either (the 4-row-vs-88-row
        failure)."""
        from helm import seats
        seats.write_roster("noisy-seat", session="s-noisy")
        chat.post("quiet corner", room="team-quiet", who="noisy-seat")
        for i in range(8):
            chat.post("@david row %d" % i, room="team-busy", who="noisy-seat")
        rooms = {r["room"]: r for r in self.req("/api/chat")[1]["rooms"]}
        self.assertEqual(rooms["team-quiet"]["owner_unread"], 1)
        self.assertEqual(rooms["team-busy"]["owner_unread"], 8)
        self.assertEqual(rooms["team-busy"]["owner_mentions"], 8)
        self.assertEqual(rooms["team-quiet"]["owner_mentions"], 0)
        self.assertTrue(rooms["team-busy"]["last"])          # the age source
        seat = rooms["team-busy"]["seats"][0]
        self.assertEqual(seat["seat"], "noisy-seat")
        self.assertIn(seat["presence"], ("fresh", "quiet", "absent"))
        self.assertIsInstance(seat["last_seen"], float)      # the age source

    def test_read_ack_zeroes_only_the_room_the_owner_looked_at(self):
        chat.post("in main", room="main", who="agent-a")
        chat.post("elsewhere", room="team-x", who="agent-a")
        self.req("/api/chat/read", {"room": "main"})
        rooms = {r["room"]: r for r in self.req("/api/chat")[1]["rooms"]}
        self.assertEqual(rooms["main"]["owner_unread"], 0)
        self.assertEqual(rooms["team-x"]["owner_unread"], 1)


if __name__ == "__main__":
    unittest.main()
