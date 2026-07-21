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
        cls.srv = web.make_server(0)  # ephemeral port
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
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

    def test_cli_read_clears_the_web_marker(self):
        # the full notify loop, endpoints-to-CLI: owner posts via web -> marker
        # -> an agent's `helm chat read` consumes past it -> cleared
        self.req("/api/chat", {"text": "anyone up?"})
        self.assertTrue(os.path.exists(chat.marker_path("main")))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(chat.cmd_chat(["read"]), 0)
        self.assertIn("david: anyone up?", out.getvalue())
        self.assertFalse(os.path.exists(chat.marker_path("main")))


if __name__ == "__main__":
    unittest.main()
