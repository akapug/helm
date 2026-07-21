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
            "HELM_CHAT_NAME", "MELD_CHAT_NAME")


class TestOwnerUnread(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="helm-test-ownerunread-")
        cls.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(cls.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(cls.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""  # transport off — hermetic v1
        cls.srv = web.make_server(0)
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
        chat.post("@david ship it?", who="fable-a")
        chat.post("agent chatter, no address", who="fable-a")
        m2 = chat.post("@david second call", who="codex-b")
        d = self.signal()
        self.assertEqual(d["owner_unread"], 3)   # every agent row is unread
        self.assertEqual(d["owner_mentions"], 2)  # two address the owner
        # dedup key + preview name the NEWEST unseen mention (the decision)
        self.assertEqual(d["owner_mention_last"], "%s|%s" % (m2["ts"], "codex-b"))
        self.assertEqual(d["owner_mention_preview"], "codex-b: @david second call")
        # the owner read the room (chat view open + visible) — cursor advances
        status, ack = self.req("/api/chat/read", {"room": "main"})
        self.assertEqual(status, 200)
        self.assertEqual(ack["owner_read"], 3)
        d = self.signal()
        self.assertEqual((d["owner_read"], d["owner_unread"], d["owner_mentions"]),
                         (3, 0, 0))
        # the next mention is a fresh decision past the moved cursor
        chat.post("@david again", who="fable-a")
        d = self.signal()
        self.assertEqual((d["owner_unread"], d["owner_mentions"]), (1, 1))

    def test_read_ack_demands_the_bearer(self):
        chat.post("@david psst", who="fable-a")
        status, _ = self.req("/api/chat/read", {"room": "main"}, token=False)
        self.assertEqual(status, 403)
        self.assertEqual(self.signal()["owner_mentions"], 1)  # cursor unmoved

    # ── owner-name matching (the delivery filter's owner rule) ──

    def test_name_matching_boundaries_and_case(self):
        chat.post("@David case-insensitive", who="fable-a")
        chat.post("@davidx is a different seat", who="fable-a")
        chat.post("mail david@example.com today", who="fable-a")  # not an @mention
        chat.post("@all broadcast is fleet noise, not an owner call", who="fable-a")
        d = self.signal()
        self.assertEqual(d["owner_unread"], 4)
        self.assertEqual(d["owner_mentions"], 1)

    def test_owner_names_env_override(self):
        os.environ["HELM_CHAT_OWNER_NAMES"] = "skipper"
        chat.post("@skipper aye", who="fable-a")
        chat.post("@david is not the owner here", who="fable-a")
        d = self.signal()
        self.assertEqual(d["owner_mentions"], 1)

    def test_owner_rail_posts_never_badge_the_owner(self):
        # the owner's own web post — even one naming himself — is not a call
        status, _ = self.req("/api/chat", {"text": "@david note to self"})
        self.assertEqual(status, 200)
        d = self.signal()
        self.assertEqual((d["owner_unread"], d["owner_mentions"]), (0, 0))
        # a CLI post CLAIMING the owner name (no rail origin) stays an
        # ordinary row — counting it is the impersonation fail-safe
        chat.post("@david trust me, it's me", who="david")
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
        chat.post("@david fresh call", who="fable-a")
        d = self.signal()
        self.assertEqual((d["owner_read"], d["owner_unread"], d["owner_mentions"]),
                         (1, 1, 1))

    def test_corrupt_cursor_fails_open_to_zero(self):
        chat.post("@david hello", who="fable-a")
        with open(web._owner_read_path("main"), "w", encoding="utf-8") as f:
            f.write("not json {")
        d = self.signal()
        self.assertEqual((d["owner_read"], d["owner_mentions"]), (0, 1))

    def test_signal_failure_answers_zeros_never_an_error(self):
        chat.post("@david hello", who="fable-a")
        real = seats.owner_names
        seats.owner_names = None  # not callable — the signal leg raises
        try:
            d = self.signal()
        finally:
            seats.owner_names = real
        # the poll still answers: rows + total intact, the signal zeroed
        self.assertEqual(d["total"], 1)
        self.assertEqual((d["owner_unread"], d["owner_mentions"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
