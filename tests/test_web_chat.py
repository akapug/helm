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
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import chat, web  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NODE_URL", "MELD_CHAT_NODE_URL",
            "HELM_CHAT_ROOM", "MELD_CHAT_ROOM", "HELM_CHAT_ROOM_SOURCE",
            "MELD_CHAT_ROOM_SOURCE", "HELM_CELL_BIN", "MELD_CELL_BIN",
            "HELM_CELL_PROFILE", "MELD_AGENT_PROFILE")


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
        shutil.rmtree(failure_dir, ignore_errors=True)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        # Retire any process-local fallback through the real ACK lifecycle before
        # deleting this class's reused tmpfs owner between tests.
        chat.acknowledge_sign_failures()
        shutil.rmtree(chat.sign_failures_dir(), ignore_errors=True)
        shutil.rmtree(os.environ["HELM_CHAT_DIR"], ignore_errors=True)
        # the class reuses ONE tmp root; a dir-wipe rewinds the room (a thing
        # append-only signed history never does in prod), so drop the immutable-
        # slice body cache between tests or a stale slice could survive the wipe
        web._CHAT_OLDER_CACHE.clear()

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
                          "signer": False, "signer_configured": False})

    def test_web_post_with_configured_missing_signer_is_persistently_degraded(self):
        missing = os.path.join(self.tmp, "deleted\x1b[2J-token=secret-signer")
        env = {"HELM_CELL_BIN": missing, "HELM_CELL_PROFILE": "web-seat",
               "HELM_CHAT_NODE_URL": "http://127.0.0.1:1"}
        with mock.patch.dict(os.environ, env):
            status, posted = self.req("/api/chat", {"text": "still delivered"})
            self.assertEqual(status, 200)
            status, polled = self.req("/api/chat?since=0")
            self.assertEqual(status, 200)
        row = posted["msg"]
        transport = polled["transport"]
        self.assertEqual((row["transport"]["code"], transport["mode"],
                          transport["code"], transport["signer_configured"]),
                         ("signer_unavailable", "degraded",
                          "signer_unavailable", True))
        self.assertEqual(chat.sign_failures()[0]["profile"], "web-seat")
        public = json.dumps({"posted": posted, "polled": polled},
                            ensure_ascii=False)
        self.assertNotIn(missing, public)
        self.assertNotIn("secret", public)
        self.assertNotIn("\x1b", public)

    def test_web_poll_and_served_ui_show_precise_degraded_transport(self):
        failure = chat._diag("send_failed", "second send failed")
        with mock.patch.object(chat, "_sign_send", return_value=(None, failure)):
            row = chat.post("still delivered", who="agent", profile="seat-a",
                            sign=True)
        self.assertEqual(row["transport"]["state"], "DEGRADED")
        status, d = self.req("/api/chat")
        self.assertEqual(status, 200)
        t = d["transport"]
        self.assertEqual((t["mode"], t["state"], t["profile"], t["reason"]),
                         ("degraded", "DEGRADED", "seat-a",
                          "second send failed"))
        for key in ("first_failure", "last_failure", "age_s", "remediation"):
            self.assertIn(key, t)
        with urllib.request.urlopen("http://127.0.0.1:%d/" % self.port,
                                    timeout=10) as resp:
            html = resp.read().decode("utf-8")
        self.assertIn('t.mode === "degraded"', html)
        self.assertIn('t.label || t.mode || "unsigned"', html)
        self.assertIn('tp.label || tp.mode', html)
        self.assertIn('"DEGRADED · " + (t.profile', html)
        self.assertIn("t.first_failure", html)
        self.assertIn("t.last_failure", html)
        self.assertIn("t.remediation", html)
        self.assertIn('signing <span class="lbadge tent">DEGRADED</span>', html)
        self.assertIn("tp.first_failure", html)
        self.assertIn("tp.last_failure", html)
        self.assertIn('tr.state === "DEGRADED"', html)
        self.assertIn('class="cdiag"', html)
        self.assertIn('⚠ DEGRADED', html)

    def test_cell_profile_is_laundered_in_post_and_poll_json(self):
        raw = "web\x1b[31m\x01\x85‮"
        clean = chat._dsan(raw)
        failure = chat._diag(
            "send_failed", "node\x1b[2J\x01\x85‮ refused")
        env = {"HELM_CELL_PROFILE": raw, "HELM_CELL_BIN": "/bin/true",
               "HELM_CHAT_NODE_URL": "http://127.0.0.1:1"}
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(chat, "node_head", return_value={"chain_index": 1}), \
             mock.patch.object(chat, "_sign_send", return_value=(None, failure)):
            status, posted = self.req("/api/chat", {"text": "still lands"})
            self.assertEqual(status, 200)
            status, polled = self.req("/api/chat?since=0")
            self.assertEqual(status, 200)

        with open(chat.sign_failures_path()) as f:
            self.assertIn(raw, json.load(f))
        profiles = [posted["msg"]["transport"]["profile"],
                    polled["lines"][0]["transport"]["profile"],
                    polled["transport"]["profile"]]
        profiles.extend(f["profile"]
                        for f in polled["transport"]["failed_profiles"])
        self.assertTrue(profiles)
        self.assertEqual(set(profiles), {clean})
        body = json.dumps({"posted": posted, "polled": polled},
                          ensure_ascii=False)
        for ch in ("\x1b", "\x00", "\x01", "\x85", "‮"):
            self.assertNotIn(ch, body)

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
        from helm import seats, web
        # agent-path posts (direct chat.post) are TTL-bounded in the rooms
        # summary by design (<=3s); this test time-compresses that leg, so
        # elapse the TTL explicitly — the freshness SLA itself is pinned by
        # TestRoomsSummarySingleFlightCache
        web._ROOMS_SUM_CACHE.clear()
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


    # ── lazy-load window (bounded initial open + older-history pages) ──

    def test_initial_open_windows_to_the_last_win_rows_and_stamps_base(self):
        """The since=0 open returns only the last `win` rows and stamps `base`
        (the absolute index of lines[0]) so the client can page older history
        from there. This is the fix for the linear full-history dump."""
        for i in range(60):
            chat.post("row %d" % i, room="win", who="agent-a")
        status, d = self.req("/api/chat?room=win&since=0&win=50")
        self.assertEqual(status, 200)
        self.assertEqual(d["total"], 60)
        self.assertEqual(len(d["lines"]), 50)
        self.assertEqual(d["base"], 10)                       # 60 - 50
        self.assertEqual(d["lines"][0]["text"], "row 10")     # the window tail
        self.assertEqual(d["lines"][-1]["text"], "row 59")

    def test_win_default_is_fifty_when_param_omitted(self):
        for i in range(70):
            chat.post("r%d" % i, room="win", who="agent-a")
        d = self.req("/api/chat?room=win&since=0")[1]
        self.assertEqual(len(d["lines"]), 50)                 # server default WIN
        self.assertEqual(d["base"], 20)

    def test_win_all_and_win_zero_are_the_full_history_escape_hatch(self):
        for i in range(60):
            chat.post("r%d" % i, room="win", who="agent-a")
        for esc in ("all", "0"):
            d = self.req("/api/chat?room=win&since=0&win=%s" % esc)[1]
            self.assertEqual(len(d["lines"]), 60)
            self.assertEqual(d["base"], 0)
            self.assertEqual(d["total"], 60)

    def test_incremental_poll_is_unwindowed_and_carries_no_base(self):
        """The live cursor path (since>0) stays byte-identical: rows[since:],
        no window, and — critically — NO `base` field, so the 22ms/54KB
        contract is untouched."""
        for i in range(60):
            chat.post("r%d" % i, room="win", who="agent-a")
        # a wide incremental read returns EVERY row past the cursor, unwindowed
        d = self.req("/api/chat?room=win&since=1")[1]
        self.assertEqual(len(d["lines"]), 59)                 # rows[1:], not a window
        self.assertNotIn("base", d)
        self.assertEqual(d["lines"][0]["text"], "r1")
        # at the end the incremental poll is empty, still no base
        d2 = self.req("/api/chat?room=win&since=60")[1]
        self.assertEqual(d2["lines"], [])
        self.assertNotIn("base", d2)

    def test_older_page_returns_the_prior_window_body_only(self):
        """?before=<idx> is the analog of builders getRecent(before): the
        immutable slice rows[before-win:before], its `base`, and the live
        total — BODY ONLY (no transport/rooms/roster/signal/presence)."""
        for i in range(60):
            chat.post("row %d" % i, room="win", who="agent-a")
        d = self.req("/api/chat?room=win&before=10&win=50")[1]
        self.assertEqual(len(d["lines"]), 10)                 # rows[0:10]
        self.assertEqual(d["base"], 0)
        self.assertEqual(d["total"], 60)                      # total stays LIVE
        self.assertEqual(d["lines"][0]["text"], "row 0")
        self.assertEqual(d["lines"][-1]["text"], "row 9")
        # DREGGTEGRITY: the signing/transport truth is NEVER in the cached body
        for k in ("transport", "rooms", "roster", "presence",
                  "owner_unread", "owner_read", "owner_mentions"):
            self.assertNotIn(k, d)
        # a mid-history page: rows[20:40] from before=40
        d2 = self.req("/api/chat?room=win&before=40&win=20")[1]
        self.assertEqual(d2["base"], 20)
        self.assertEqual([m["text"] for m in d2["lines"]],
                         ["row %d" % i for i in range(20, 40)])
        # bad before -> 400
        self.assertEqual(self.req("/api/chat?room=win&before=x")[0], 400)

    def test_owner_signal_counts_full_rows_even_when_the_body_is_windowed(self):
        """The badge is computed from the FULL rows, never the window — 40
        unread land, the window shows 10, owner_unread is still 40."""
        for i in range(40):
            chat.post("@david row %d" % i, room="main", who="agent-a")
        d = self.req("/api/chat?room=main&since=0&win=10")[1]
        self.assertEqual(len(d["lines"]), 10)                 # windowed body
        self.assertEqual(d["owner_unread"], 40)               # full-rows signal
        self.assertEqual(d["owner_mentions"], 40)

    def test_older_page_slice_is_immutable_under_appends(self):
        """The older-page body is signed, immutable history — appending new
        rows never changes an already-fetched slice (cache-safe), while total
        tracks live. (Previously vacuous: it never asserted the post-append
        total actually advanced past the cache, so a stale total went unseen.)"""
        for i in range(50):
            chat.post("row %d" % i, room="win", who="agent-a")
        first = self.req("/api/chat?room=win&before=20&win=10")[1]
        self.assertEqual(first["total"], 50)
        chat.post("newest", room="win", who="agent-a")        # total -> 51
        again = self.req("/api/chat?room=win&before=20&win=10")[1]
        self.assertEqual([m["text"] for m in first["lines"]],
                         [m["text"] for m in again["lines"]])  # slice unchanged
        self.assertEqual(again["base"], 10)
        self.assertEqual(again["total"], 51)   # NOT the cached 50 — total tracks the append live

    # ── rotation-aware cursor + cache + reply reconciliation (cross-family FIX) ──

    def test_gen_is_stable_across_appends_and_flips_on_rotation(self):
        """The rotation cursor the client resets on: `gen` (the head-row
        fingerprint) is STABLE while rows only append and CHANGES the instant
        the oldest half rotates out — so the client can tell 'rows appended'
        from 'rows dropped/reindexed' without trusting a stale absolute count."""
        for i in range(20):
            chat.post("row %d" % i, room="gen", who="agent-a")
        d1 = self.req("/api/chat?room=gen&since=0&win=10")[1]
        gen1, total1 = d1["gen"], d1["total"]
        self.assertEqual(total1, 20)
        self.assertTrue(gen1 and gen1 != "0")
        for i in range(20, 35):                             # append: head untouched
            chat.post("row %d" % i, room="gen", who="agent-a")
        d2 = self.req("/api/chat?room=gen&since=%d" % total1)[1]
        self.assertEqual(d2["gen"], gen1)                   # append never flips the cursor
        self.assertEqual(d2["total"], 35)
        self.assertEqual([m["text"] for m in d2["lines"]],  # byte-identical incremental slice
                         ["row %d" % i for i in range(20, 35)])
        self.assertTrue(chat._rotate(chat.room_path("gen"), cap=0))  # head becomes a new row
        d3 = self.req("/api/chat?room=gen&since=35")[1]
        self.assertNotEqual(d3["gen"], gen1)                # the reset signal fires
        self.assertLess(d3["total"], total1)                # the oldest half is gone

    def test_rotate_then_regrow_past_the_cursor_is_caught_by_gen_not_total(self):
        """The exact P1 repro: a client caches since=N; the room rotates (halves)
        then regrows PAST N. total climbs back above N, so a naive total<since
        check never fires and the client would MISS the reindexed rows — but
        `gen` flipped on the rotate, which is the signal the client resets on."""
        for i in range(30):
            chat.post("row %d" % i, room="rr", who="agent-a")
        before = self.req("/api/chat?room=rr&since=0&win=10")[1]
        since, gen0 = before["total"], before["gen"]        # the client's stored cursor + gen
        self.assertEqual(since, 30)
        self.assertTrue(chat._rotate(chat.room_path("rr"), cap=0))   # 30 -> 15
        for i in range(30, 60):                             # regrow to 45 > since(30)
            chat.post("row %d" % i, room="rr", who="agent-a")
        d = self.req("/api/chat?room=rr&since=%d" % since)[1]
        self.assertGreater(d["total"], since)               # total<since would NOT fire
        self.assertNotEqual(d["gen"], gen0)                 # but gen flipped -> client resets + repaints

    def test_older_page_cache_is_rotation_aware_never_serves_dropped_rows(self):
        """P2: the older-page body cache assumed 'rows only append', so after a
        rotate it served dropped rows + a stale total for up to the TTL. Now it
        validates on the file fingerprint — a rotate is a cache MISS, so the
        second fetch reflects the rotated room, never the cached slice."""
        for i in range(50):
            chat.post("row %d" % i, room="rot", who="agent-a")
        first = self.req("/api/chat?room=rot&before=20&win=10")[1]  # caches the slice
        self.assertEqual(first["total"], 50)
        self.assertEqual([m["text"] for m in first["lines"]],
                         ["row %d" % i for i in range(10, 20)])
        self.assertTrue(chat._rotate(chat.room_path("rot"), cap=0))  # 50 -> 25, oldest gone
        live_total = chat.read("rot")[1]
        again = self.req("/api/chat?room=rot&before=20&win=10")[1]
        self.assertEqual(again["total"], live_total)        # not the stale cached 50
        self.assertNotEqual([m["text"] for m in again["lines"]],
                            [m["text"] for m in first["lines"]])   # never the dropped rows

    def test_older_cache_hit_revalidates_the_stat_and_serves_live_total(self):
        """P2 TOCTOU (codex-3): the stale design sampled the room stat at KEY
        BUILD — before the hit — so an append landing between that sample and the
        serve returned a cached stale total (served 10 while the file already held
        11). The fix validates the stat AT the hit (against the stat stored with
        the body, sampled AFTER its read), so an append in that window is a MISS
        and the live total is served. Modeled deterministically: the append is
        injected at the cache lookup — the exact window the old key straddled."""
        for i in range(10):
            chat.post("row %d" % i, room="toc", who="a")
        primed = self.req("/api/chat?room=toc&before=5&win=5")[1]   # caches the 10-row slice
        self.assertEqual(primed["total"], 10)

        class _RacingCache(dict):
            fired = False
            def get(self, *a, **k):
                if not self.fired:                 # land an append IN the lookup window
                    self.fired = True              # (between the old key's stat sample + serve)
                    chat.post("row 10", room="toc", who="a")   # file 10 -> 11 mid-request
                return super().get(*a, **k)

        racing = _RacingCache(web._CHAT_OLDER_CACHE)   # carry the primed entry across
        with mock.patch.object(web, "_CHAT_OLDER_CACHE", racing):
            served = self.req("/api/chat?room=toc&before=5&win=5")[1]
        self.assertEqual(chat.read("toc")[1], 11)      # the file really did grow
        self.assertEqual(served["total"], 11)          # live total, NEVER the cached 10
        self.assertEqual([m["text"] for m in served["lines"]],   # immutable slice intact
                         ["row %d" % i for i in range(0, 5)])

    def test_older_cache_evicts_prior_versions_bounded_to_one_per_room(self):
        """P2 growth (codex-3): the versioned key (…, stat) NEVER evicted, so
        every append minted a fresh entry — 12 appends left 12 entries, unbounded.
        The fix evicts a room's prior-version pages when a new version is written,
        so the cache holds at most one live entry per room whatever the churn."""
        for i in range(3):
            chat.post("r%d" % i, room="grow", who="a")
        web._CHAT_OLDER_CACHE.clear()
        for i in range(12):
            chat.post("more %d" % i, room="grow", who="a")       # each append reindexes the file
            self.req("/api/chat?room=grow&before=%d&win=2" % (i + 3))
        room_keys = [k for k in web._CHAT_OLDER_CACHE if k[1] == "grow"]
        self.assertLessEqual(len(room_keys), 1)                   # bounded — not one-per-append

    def test_batch_id_hydration_distinguishes_out_of_window_from_rotated_out(self):
        """P1 reply reconciliation: a parent ABOVE the loaded window is resolved
        by id (still present in the room), so its reply renders correctly —
        only a parent GENUINELY rotated out (id absent) reads 'rotated out'.
        The ids endpoint is body-only (no transport) — DREGGTEGRITY intact."""
        pid = self.req("/api/chat",
                       {"text": "the parent", "name": "agent-a"})[1]["msg"]["id"]
        for i in range(60):                                 # push the parent above a 50-window
            chat.post("filler %d" % i, room="main", who="agent-a")
        got = self.req("/api/chat?room=main&ids=%s" % pid)[1]
        self.assertEqual([m["id"] for m in got["lines"]], [pid])   # present -> resolvable, NOT "gone"
        self.assertNotIn("transport", got)                  # body-only, no signal
        self.assertEqual(got["lines"][0]["text"], "the parent")    # author+snippet are hydratable
        for _ in range(8):                                  # rotate until the parent is dropped
            if pid not in [m.get("id") for m in chat.read("main")[0]]:
                break
            chat._rotate(chat.room_path("main"), cap=0)
        self.assertNotIn(pid, [m.get("id") for m in chat.read("main")[0]])
        gone = self.req("/api/chat?room=main&ids=%s" % pid)[1]
        self.assertEqual(gone["lines"], [])                 # absent -> the client marks it truly rotated out

    def test_poll_and_older_and_ids_all_carry_the_rotation_gen(self):
        """Every read shape the client trusts stamps `gen`, so the client can
        detect rotation on the poll, guard an older-page prepend, and fingerprint
        a hydration alike."""
        for i in range(5):
            chat.post("row %d" % i, room="g2", who="agent-a")
        poll = self.req("/api/chat?room=g2&since=0&win=3")[1]
        older = self.req("/api/chat?room=g2&before=3&win=3")[1]
        ids = self.req("/api/chat?room=g2&ids=nope")[1]
        self.assertIsInstance(poll["gen"], str)
        self.assertEqual(older["gen"], poll["gen"])         # same room, same head
        self.assertIn("gen", ids)


if __name__ == "__main__":
    unittest.main()
