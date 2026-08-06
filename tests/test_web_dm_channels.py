#!/usr/bin/env python3
"""task/62 — DM lanes as droppable-in web channels. The dm/ namespace is
deliberately invisible to chat.list_rooms (no room fanout, no log-flush
default); ONLY the owner's web sidebar surfaces it: _rooms_summary appends
one flagged row per lane, and a POST into an open dm- channel routes through
the one owner DM seam (_api_chat_dm), never chat.post — the lane stem is
seats._seat_key output, which PASSES the recipient token regex while
addressing nothing, so the handler resolves it against the roster and
refuses an orphan lane by name. Tmp HELM_HOME + HELM_CHAT_DIR; the real
~/.helm and /dev/shm/helm-chat are never touched."""
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
            "HELM_CHAT_ROOM", "MELD_CHAT_ROOM", "HELM_CHAT_ROOM_SOURCE",
            "MELD_CHAT_ROOM_SOURCE", "HELM_CELL_BIN", "MELD_CELL_BIN",
            "HELM_CELL_PROFILE", "MELD_AGENT_PROFILE",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME")


class TestWebDmChannels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="helm-test-webdm-")
        cls.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        cls.owner_prior = seats._OWNER_NAME
        seats._OWNER_NAME = "owner"
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(cls.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(cls.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""  # transport off — hermetic
        cls.cwd_prior = os.getcwd()
        os.chdir(cls.tmp)
        cls.srv = web.make_server(0)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        failure_dir = chat.sign_failures_dir()
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.thread.join(timeout=5)
        os.chdir(cls.cwd_prior)
        for k, v in cls.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        seats._OWNER_NAME = cls.owner_prior
        shutil.rmtree(failure_dir, ignore_errors=True)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        chat.acknowledge_sign_failures()
        shutil.rmtree(chat.sign_failures_dir(), ignore_errors=True)
        shutil.rmtree(os.environ["HELM_CHAT_DIR"], ignore_errors=True)
        # the summary cache is keyed by chat root and this class reuses ONE
        # tmp root across tests — a dir-wipe must not leave a stale summary
        web._ROOMS_SUM_CACHE.clear()
        web._CHAT_OLDER_CACHE.clear()

    def req(self, path, payload=None):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        if data:
            headers["Authorization"] = "Bearer " + web.MUTATION_TOKEN
        r = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"null")

    def _seed_dm(self, seat="codex-2", text="psst"):
        seats.write_roster(seat, session="s-" + seat)
        row, err = seats.dm(seat, text, who="helm-claude")
        self.assertIsNone(err, err)
        return chat.dm_room(seat), row

    # --- the lane is a channel ------------------------------------------------

    def test_dm_lane_appears_in_rooms_summary_flagged_and_labeled(self):
        lane, _ = self._seed_dm()
        web._rooms_summary_invalidate()   # CLI-side send: TTL would hide it
        status, d = self.req("/api/chat")
        self.assertEqual(status, 200)
        rows = {r["room"]: r for r in d["rooms"]}
        self.assertIn(lane, rows)
        r = rows[lane]
        self.assertIs(r.get("dm"), True)
        self.assertEqual(r.get("seat"), "codex-2")
        self.assertEqual(r["total"], 1)
        # a DM to ANY seat reaches the owner: it counts as channel unread
        self.assertEqual(r["owner_unread"], 1)
        # the public room list itself stays un-widened: main carries no flag
        self.assertNotIn("dm", rows.get("main", {}))

    def test_dm_lane_is_readable_as_a_room(self):
        lane, _ = self._seed_dm(text="the lane serves its rows")
        status, d = self.req("/api/chat?room=" + lane)
        self.assertEqual(status, 200)
        self.assertEqual(d["total"], 1)
        self.assertEqual(d["lines"][0]["text"], "the lane serves its rows")

    def test_read_ack_zeroes_the_dm_unread(self):  # noqa: VACUOUS_ASSERTION — the pre-ack assertEqual(owner_unread, 1) is the unconditional positive control on the same observable; the rung's detector cannot see an in-test before/after pair
        lane, _ = self._seed_dm()
        # positive control FIRST: the observable can be non-zero (the ack
        # below is proven to change state, not to restate an empty default)
        status, d = self.req("/api/chat")
        row = next(r for r in d["rooms"] if r["room"] == lane)
        self.assertEqual(row["owner_unread"], 1)
        status, _ = self.req("/api/chat/read", {"room": lane})
        self.assertEqual(status, 200)
        web._ROOMS_SUM_CACHE.clear()
        status, d = self.req("/api/chat")
        row = next(r for r in d["rooms"] if r["room"] == lane)
        self.assertEqual(row["owner_unread"], 0)

    # --- posting into an open DM channel -------------------------------------

    def test_post_into_dm_channel_routes_through_the_dm_seam(self):
        lane, _ = self._seed_dm()
        status, d = self.req("/api/chat", {"room": lane, "text": "reply in place"})
        self.assertEqual(status, 200, d)
        self.assertTrue(d.get("ok"))
        # the row landed in the LANE (dm metadata stamped by seats.dm), and
        # in NO public room — the send went through the DM seam, not chat.post
        rows, total = chat.read(lane)
        self.assertEqual(total, 2)
        self.assertEqual(rows[-1]["text"], "reply in place")
        self.assertEqual(rows[-1].get("dm"), "codex-2")
        self.assertEqual(chat.read("main")[1], 0)
        self.assertNotIn(lane[len(chat.DM_PREFIX):], chat.list_rooms())

    def test_dm_reply_threads_instead_of_flattening(self):
        """codex-2's FIX: the browser posts reply_to with every
        composer reply, but both DM adapters dropped it — HTTP 200 while the
        child lacked reply_to/rts/rfrom, a silently flattened thread. The
        witness drives the real endpoint path (dm-room POST -> _api_chat_dm
        -> seats.dm -> chat.post's resolve) and demands the threading triplet
        on the stored child row."""
        lane, parent = self._seed_dm(text="parent to thread under")
        status, d = self.req("/api/chat", {
            "room": lane, "text": "threaded reply",
            "reply_to": parent.get("id")})
        self.assertEqual(status, 200, d)
        child = chat.read(lane)[0][-1]
        self.assertEqual(child.get("text"), "threaded reply")
        self.assertEqual(child.get("reply_to"), parent.get("id"))
        self.assertEqual(child.get("rfrom"), "helm-claude")  # parent's author
        self.assertTrue(child.get("rts"))                    # parent's ts rides too
        # control: a reply-less post stays unthreaded (the triplet is absent,
        # not defaulted)
        status, _ = self.req("/api/chat", {"room": lane, "text": "plain"})
        self.assertEqual(status, 200)
        plain = chat.read(lane)[0][-1]
        self.assertIsNone(plain.get("reply_to"))

    def test_post_into_orphan_dm_lane_refuses_by_name(self):
        lane, _ = self._seed_dm(seat="ghost")
        os.remove(seats.roster_path())    # the seat left; its lane remains
        web._ROOMS_SUM_CACHE.clear()
        status, d = self.req("/api/chat", {"room": lane, "text": "hello?"})
        self.assertEqual(status, 400)
        self.assertIn(lane, d["error"])
        self.assertIn("no roster seat", d["error"])
        self.assertEqual(chat.read(lane)[1], 1)   # nothing landed

    def test_public_room_post_is_unchanged_by_the_dm_branch(self):
        status, d = self.req("/api/chat", {"room": "main", "text": "control"})
        self.assertEqual(status, 200)
        self.assertEqual(chat.read("main")[1], 1)
        self.assertEqual(chat.read("main")[0][0].get("dm"), None)


if __name__ == "__main__":
    unittest.main()
