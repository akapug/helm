#!/usr/bin/env python3
"""helm.web chat surface — hermetic contract tests. GET /api/chat is the poll
read (open on loopback, like every GET); POST /api/chat is the OWNER's post —
it demands the mutation bearer (403 without) and drops the owner-unread marker
the shipped reflex fires on. Tmp HELM_HOME + HELM_CHAT_DIR; the real ~/.helm
and /dev/shm/helm-chat are never touched."""
import contextlib
import http.client
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import chat, seats, web, web_chat_rooms  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NODE_URL", "MELD_CHAT_NODE_URL",
            "HELM_CHAT_ROOM", "MELD_CHAT_ROOM", "HELM_CHAT_ROOM_SOURCE",
            "MELD_CHAT_ROOM_SOURCE", "HELM_CELL_BIN", "MELD_CELL_BIN",
            "HELM_CELL_PROFILE", "MELD_AGENT_PROFILE",
            # the AMBIENT IDENTITY must not leak in from whoever runs the
            # suite: honest presence refuses a cross-seat join, so a test that
            # joins as 'codex' PASSED for a codex-named runner and FAILED for
            # everyone else. A test whose result depends on who ran it is not
            # a test (found by running main's suite from the console-design
            # seat, 2026-07-24).
            "HELM_CHAT_NAME", "MELD_CHAT_NAME",
            # OWNER NAMES ARE AMBIENT AND THIS MODULE ASSERTS ON THEM. setUpClass
            # pins seats._OWNER_NAME, but owner_names() reads the ENV FIRST and
            # returns early, so that pin CANNOT defeat an inherited value. Four
            # sibling modules already sandbox this key (test_planprompt,
            # test_nonpane_session, test_instructions_are_runnable,
            # test_web_owner_unread) and two of them SET it; a setUp that raises
            # after the set skips its own tearDown and leaves it in the process
            # for every later module. This module asserted owner_mentions
            # without sandboxing it.
            "HELM_CHAT_OWNER_NAMES", "MELD_CHAT_OWNER_NAMES")


class TestWebChat(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="helm-test-webchat-")
        cls.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        cls.owner_prior = seats._OWNER_NAME
        seats._OWNER_NAME = "daria"
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
        # shutdown() waits one serve_forever poll (stdlib 0.5s)
        cls.thread = threading.Thread(target=cls.srv.serve_forever,
                                      kwargs={"poll_interval": 0.01}, daemon=True)
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
        # Retire any process-local fallback through the real ACK lifecycle before
        # deleting this class's reused tmpfs owner between tests.
        chat.acknowledge_sign_failures()
        shutil.rmtree(chat.sign_failures_dir(), ignore_errors=True)
        shutil.rmtree(os.environ["HELM_CHAT_DIR"], ignore_errors=True)
        # the class reuses ONE tmp root; a dir-wipe rewinds the room (a thing
        # append-only signed history never does in prod), so drop the immutable-
        # slice body cache between tests or a stale slice could survive the wipe
        web._CHAT_OLDER_CACHE.clear()

    #: Transport attempts after the first. A dropped connection is not an
    #: answer from the server, and an arm whose subject is a badge must not
    #: die of one — but the budget is small on purpose, so a server that
    #: drops SYSTEMATICALLY still exhausts it and reds.
    TRANSPORT_RETRIES = 2

    #: Every drop this fixture absorbed, for the life of the class. Kept so
    #: the retry can never be a silence: it is printed as it happens and
    #: read back by the arm below.
    transport_drops = 0

    def req(self, path, payload=None, token=True):
        """(status, obj) — 4xx/5xx returned, not raised. payload -> POST;
        token=False drops the bearer (the CSRF-shaped request).

        A TRANSPORT DROP IS EMITTED AND COUNTED ALWAYS, AND RETRIED ONLY ON
        A READ. An HTTP status is an ANSWER from the server and is returned as
        one; a connection that closes without a response is not an answer at
        all, and the arms here assert on badges and windows, not on transport.
        Absorbing it silently would hide a real defect, so each drop prints one
        line naming the path, the method, the attempt and the exception, and
        increments a counter the arm below reads.

        A MUTATION IS NEVER REPLAYED. A POST that reaches the server and
        COMMITS, whose response is then lost, is indistinguishable from one
        that never arrived — and this API carries no idempotency key, so a
        retry would append the row a second time. A GET has no such hazard:
        re-reading a room changes nothing. So a dropped POST raises on the
        first drop, counted and emitted, and only a GET spends the budget.

        The budget is deliberately small: a server dropping systematically
        exhausts it and the exception is raised, so this makes a read robust
        to a blip without making it blind to a broken server.

        WHERE THE LINE LANDS, stated exactly: stderr, which reaches the run
        log and is greppable across every gate on either node. It reaches a
        gate RECEIPT only when the budget is exhausted or the request was a
        mutation, because a receipt records failures — a passing run's stderr
        is in the log and not in the record.
        """
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        if data and token:
            headers["Authorization"] = "Bearer " + web.MUTATION_TOKEN
        r = urllib.request.Request(url, data=data, headers=headers)
        # A MUTATION GETS ONE ATTEMPT. Only a read may be repeated.
        budget = 0 if data is not None else self.TRANSPORT_RETRIES
        method = "POST" if data is not None else "GET"
        for attempt in range(budget + 1):
            try:
                with urllib.request.urlopen(r, timeout=10) as resp:
                    return resp.status, json.loads(resp.read() or b"null")
            except urllib.error.HTTPError as e:
                # AN ANSWER, NOT A DROP. Returned, never retried.
                with e:
                    return e.code, json.loads(e.read() or b"null")
            except (http.client.RemoteDisconnected, ConnectionError,
                    TimeoutError, urllib.error.URLError, OSError) as e:
                type(self).transport_drops += 1
                print("helm web transport drop: %s %s attempt %d/%d: %s: %s"
                      % (method, path, attempt + 1, budget + 1,
                         type(e).__name__, e), file=sys.stderr)
                if attempt == budget:
                    raise

    def test_a_dropped_connection_is_RETRIED_EMITTED_and_COUNTED(self):  # noqa: VACUOUS_ASSERTION — status 200, room main, two urlopen calls, the counter incremented and three substrings of the emitted line are all unconditional positives
        """A READ whose connection dies is not an answer, and an arm whose
        subject is a badge must not die of one.

        THE RETRY MUST NOT BECOME A SILENCE. Every drop it absorbs prints a
        line and increments a counter, so a run is a measurement of how often
        the embedded server drops rather than a quiet pass that says nothing
        about it."""
        real = urllib.request.urlopen
        calls = []

        def drop_once(request, *a, **kw):
            calls.append(1)
            if len(calls) == 1:
                raise http.client.RemoteDisconnected(
                    "Remote end closed connection without response")
            return real(request, *a, **kw)

        before = type(self).transport_drops
        err = io.StringIO()
        with mock.patch.object(urllib.request, "urlopen", drop_once), \
                contextlib.redirect_stderr(err):
            status, d = self.req("/api/chat")
        # THE RETRY WORKED: the second attempt got the real answer.
        self.assertEqual(status, 200)
        self.assertEqual(d["room"], "main")
        self.assertEqual(len(calls), 2)
        # ...IT WAS COUNTED...
        self.assertEqual(type(self).transport_drops, before + 1)
        # ...AND IT WAS SAID OUT LOUD, naming the path and the exception.
        said = err.getvalue()
        self.assertIn("helm web transport drop", said)
        self.assertIn("/api/chat", said)
        self.assertIn("RemoteDisconnected", said)

    def test_a_SYSTEMATIC_drop_still_reds_rather_than_being_absorbed(self):  # noqa: VACUOUS_ASSERTION — assertRaises is the observable, and the counter and the emitted-line count are both asserted to equal budget+1 unconditionally
        """The other half, and the one that keeps this from hiding a broken
        server: the budget is finite, so a connection that always drops
        exhausts it and the exception reaches the runner."""
        def always_drop(request, *a, **kw):
            raise http.client.RemoteDisconnected(
                "Remote end closed connection without response")

        before = type(self).transport_drops
        err = io.StringIO()
        with mock.patch.object(urllib.request, "urlopen", always_drop), \
                contextlib.redirect_stderr(err), \
                self.assertRaises(http.client.RemoteDisconnected):
            self.req("/api/chat")
        # EVERY attempt was counted and emitted, not just the last.
        self.assertEqual(type(self).transport_drops,
                         before + self.TRANSPORT_RETRIES + 1)
        self.assertEqual(err.getvalue().count("helm web transport drop"),
                         self.TRANSPORT_RETRIES + 1)

    def test_a_MUTATION_is_never_replayed_after_a_drop(self):  # noqa: VACUOUS_ASSERTION — the GET control immediately below is asserted to retry on the same fixture, so the single POST attempt is the method rule and not a dead retry
        """A POST THAT COMMITS AND THEN LOSES ITS RESPONSE IS INDISTINGUISHABLE
        FROM ONE THAT NEVER ARRIVED, and this API carries no idempotency key —
        so a retry would append the row a second time. A dropped mutation
        raises on the first drop, counted and emitted like any other, and is
        never sent again."""
        def always_drop(request, *a, **kw):
            raise http.client.RemoteDisconnected(
                "Remote end closed connection without response")

        before = type(self).transport_drops
        err = io.StringIO()
        with mock.patch.object(urllib.request, "urlopen", always_drop), \
                contextlib.redirect_stderr(err), \
                self.assertRaises(http.client.RemoteDisconnected):
            self.req("/api/chat", payload={"text": "never sent twice"})
        # EXACTLY ONE ATTEMPT, and it said so.
        self.assertEqual(type(self).transport_drops, before + 1)
        said = err.getvalue()
        self.assertEqual(said.count("helm web transport drop"), 1)
        self.assertIn("POST /api/chat attempt 1/1", said)
        # THE CONTROL, on the same fixture: a READ of the same path DOES spend
        # the budget, so the single attempt above is the method rule rather
        # than a retry that stopped working.
        before = type(self).transport_drops
        with mock.patch.object(urllib.request, "urlopen", always_drop), \
                contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(http.client.RemoteDisconnected):
            self.req("/api/chat")
        self.assertEqual(type(self).transport_drops,
                         before + self.TRANSPORT_RETRIES + 1)
        # ...AND THE ROOM IS UNCHANGED: nothing was committed by either.
        self.assertEqual(self.req("/api/chat")[1]["total"], 0)

    def test_an_HTTP_status_is_an_ANSWER_and_is_never_retried(self):  # noqa: VACUOUS_ASSERTION — status 403 and exactly one urlopen call are unconditional positives; the unchanged counter is the paired negative that makes them discriminating
        """A 4xx is the server speaking. Retrying it would turn a contract
        assertion into three requests and could mask a real refusal."""
        calls = []
        real = urllib.request.urlopen

        def counted(request, *a, **kw):
            calls.append(1)
            return real(request, *a, **kw)

        before = type(self).transport_drops
        with mock.patch.object(urllib.request, "urlopen", counted):
            status, _ = self.req("/api/chat", payload={"text": "x"},
                                 token=False)
        self.assertEqual(status, 403)
        self.assertEqual(len(calls), 1, "an HTTP answer was retried")
        self.assertEqual(type(self).transport_drops, before,
                         "an HTTP answer was counted as a transport drop")

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
        chat.post("@daria urgent", room="team-fe", who="codex")  # agent elsewhere
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
                              "tfrom": target["from"], "name": "daria"})
        self.assertEqual(status, 200)
        self.assertEqual(d["msg"]["react"], "🎉")
        self.assertEqual(d["msg"]["tfrom"], "daria")
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
        self.assertEqual(d["msg"]["from"], "daria")          # the default name
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

    def test_the_owner_envelope_says_when_the_DM_NAMESPACE_is_unreadable(self):  # noqa: VACUOUS_ASSERTION — the False pole HAS unconditional positive controls on the same observable (room == main and lines == [seed], asserted on that exact response before the flag is read), and the True pole is the discriminating half; the rung keys on the `is False` shape, which here is the must-miss of a two-pole flag, not an unguarded absence
        """A dm/ dir that cannot be READ used to render as an owner with NO
        DMs — on the owner's OWN surface, which is the worst place to fail
        open. web_chat_rooms carried its own _dm_lanes with the identical
        `except OSError: return []` as the ack ladder's, a separate function
        with the same name and the same defect.

        THE SIGNAL RIDES THE ENVELOPE, not the room rows: a namespace is not a
        room, and emitting a synthetic row for it is exactly the fake sentinel
        that sorted ahead of real work and ate the pending cap.

        BOTH POLES. A flag only ever asserted PRESENT passes just as happily
        on a version that emits it unconditionally — and then every healthy
        poll carries a false alarm, which is the same vacuity as a cap
        disclosure that never checks the under-cap case."""
        chat.post("seed", room="main", who="daria")
        healthy = self.req("/api/chat?since=0")[1]
        # UNCONDITIONAL POSITIVE CONTROL on the same observable: prove this IS
        # the rendered envelope before reading a False out of it. Without it
        # the healthy pole is an assertion about a response nothing verified.
        self.assertEqual(healthy["room"], "main")
        self.assertEqual([m["text"] for m in healthy["lines"]], ["seed"])
        self.assertIs(healthy["dm_incomplete"], False,
                      "a readable namespace was reported incomplete")

        dmdir = os.path.join(chat.chat_dir(), "dm")
        shutil.rmtree(dmdir, ignore_errors=True)
        with open(dmdir, "w", encoding="utf-8") as f:
            f.write("a FILE where the directory belongs")  # listdir -> OSError
        broken = self.req("/api/chat?since=0")[1]
        self.assertIs(broken["dm_incomplete"], True,
                      "the owner's surface reported no DMs over a namespace "
                      "it could not read")

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
        into #main and called it a DM."""
        from helm import seats
        seats.join(session="s-web-dm", seat="codex", cwd="/tmp/p")
        status, d = self.req("/api/chat/dm", {"to": "codex", "text": " go ",
                                              "name": "daria"})
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
        self.assertIn("daria: anyone up?", out.getvalue())
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
        seats.write_roster("example-app-platform-codex", session="s-1")
        seats.write_roster("helm-opus-integrator", session="s-2")
        d = self.req("/api/chat")[1]
        self.assertEqual(d["roster"],
                         ["example-app-platform-codex", "helm-opus-integrator"])

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
            chat.post("@daria row %d" % i, room="team-busy", who="noisy-seat")
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
        """?before=<idx> is the analog of an earlier chat client's
        getRecent(before): the immutable slice rows[before-win:before], its
        `base`, and the live total — BODY ONLY (no
        transport/rooms/roster/signal/presence)."""
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
            chat.post("@daria row %d" % i, room="main", who="agent-a")
        d = self.req("/api/chat?room=main&since=0&win=10")[1]
        self.assertEqual(len(d["lines"]), 10)                 # windowed body
        self.assertEqual(d["owner_unread"], 40)               # full-rows signal
        self.assertEqual(d["owner_mentions"], 40)

    # ── "answers for you": the same reading the push path makes (task/2364) ─
    def test_the_answers_rows_are_the_same_reading_the_notification_uses(self):
        """ONE READ, TWO RENDERINGS. `_owner_signal` decides what @-mentions the
        owner, and the browser notification already fires off it; the card must
        not be a second extraction with a matching rule of its own, because the
        two would then be free to disagree about what reached him and the
        disagreement would be invisible.

        So the preview and the newest answer row are asserted to be THE SAME
        row, and the count the card shows is the count the badge shows."""
        chat.post("plain status line", room="main", who="agent-a")
        chat.post("@daria the astra swap is done", room="main", who="agent-a")
        chat.post("@daria and the proxy is back", room="main", who="agent-b")
        d = self.req("/api/chat?room=main&since=0")[1]
        self.assertEqual(d["owner_mentions"], 2)
        answers = d["owner_answers"]
        self.assertEqual([a["from"] for a in answers], ["agent-b", "agent-a"])
        self.assertEqual(answers[0]["text"], "@daria and the proxy is back")
        # the preview the NOTIFICATION carries is this same newest row
        self.assertEqual(d["owner_mention_preview"],
                         "%s: %s" % (answers[0]["from"], answers[0]["text"]))
        # and the row that is NOT addressed to him is in neither
        for a in answers:
            self.assertNotIn("plain status line", a["text"])

    def test_every_answer_row_carries_its_own_age_from_the_server(self):
        """Ages are resolved at RESPONSE time, the same law the lands rows
        follow: the browser's clock must never be the one comparison that
        decides whether a reply is fresh."""
        chat.post("@daria one", room="main", who="agent-a")
        answers = self.req("/api/chat?room=main&since=0")[1]["owner_answers"]
        self.assertEqual(len(answers), 1)
        self.assertIsInstance(answers[0]["age_s"], int)
        self.assertGreaterEqual(answers[0]["age_s"], 0)
        self.assertLess(answers[0]["age_s"], 300)
        self.assertEqual(answers[0]["room"], "main")

    def test_the_count_is_the_whole_population_and_only_the_list_is_capped(self):
        """A cap is a display budget and may never edit a count. The card can
        honestly say "4 of 9" only because it was never handed a truncated
        number — the walk counts every mention and the slice happens last."""
        for i in range(9):
            chat.post("@daria reply %d" % i, room="main", who="agent-a")
        d = self.req("/api/chat?room=main&since=0")[1]
        self.assertEqual(d["owner_mentions"], 9)
        answers = d["owner_answers"]
        self.assertEqual(len(answers), web_chat_rooms._OWNER_ANSWER_CAP)
        # NEWEST FIRST, and it is the newest of the NINE rather than the newest
        # of some earlier slice
        self.assertEqual(answers[0]["text"], "@daria reply 8")

    def test_a_room_with_nothing_addressed_to_him_sends_an_EMPTY_list(self):
        """PRESENT AND EMPTY, never absent. An absent key tells the card the
        server predates the reading, which is a fact about the SERVER; a
        measured zero is a fact about the ROOM, and the card says two different
        sentences for them."""
        chat.post("agents talking to each other", room="main", who="agent-a")
        d = self.req("/api/chat?room=main&since=0")[1]
        self.assertIn("owner_answers", d)
        self.assertEqual(d["owner_answers"], [])
        self.assertEqual(d["owner_mentions"], 0)
        # THE POSITIVE CONTROL IN THE SAME RUN: the reading is not simply blind
        chat.post("@daria one for you", room="main", who="agent-a")
        d = self.req("/api/chat?room=main&since=0")[1]
        self.assertEqual(len(d["owner_answers"]), 1)

    def test_the_owners_own_posts_are_not_answers_to_himself(self):
        """The owner rails' own rows never badge the owner, and they must not
        become answers either — a card telling him he has a reply from himself
        is the second-queue failure in miniature."""
        chat.post("@daria from an agent", room="main", who="agent-a")
        d = self.req("/api/chat?room=main&since=0")[1]
        froms = [a["from"] for a in d["owner_answers"]]
        self.assertEqual(froms, ["agent-a"])

    def test_reading_the_room_clears_the_card_rather_than_a_queue(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control runs FIRST and on the same observable: assertEqual(len(owner_answers), 1) over the same route, plus the read-ack POST is asserted to answer 200 before either emptiness claim
        """IT IS NOT AN INBOX. The card holds nothing and acknowledges nothing:
        the rows are whatever sits past his chat cursor, so advancing the cursor
        empties it. This is the arm that would red if the card ever grew state
        of its own."""
        chat.post("@daria waiting for you", room="main", who="agent-a")
        self.assertEqual(
            len(self.req("/api/chat?room=main&since=0")[1]["owner_answers"]), 1)
        self.assertEqual(self.req("/api/chat/read", {"room": "main"})[0], 200)
        d = self.req("/api/chat?room=main&since=0")[1]
        self.assertEqual(d["owner_answers"], [])
        self.assertEqual(d["owner_mentions"], 0)

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
        """P2 TOCTOU: the stale design sampled the room stat at KEY
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
        """P2 growth: the versioned key (…, stat) NEVER evicted, so
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



class TestWebChatRoomsStandsAloneOnImport(unittest.TestCase):
    """web_chat_rooms copies helm.web's globals — but ONLY when helm.web is
    ALREADY in sys.modules (`sys.modules.get(__package__ + ".web")`). On the
    other import order os/re/time were unbound, so every os./re./time. use
    raised NameError, and _owner_signal's `except Exception` answered
    {owner_read: 0, owner_unread: 0, owner_mentions: 0} — the owner's unread
    badge read ZERO forever with nothing logged anywhere.

    THIS ARM MUST RUN IN A FRESH INTERPRETER. This module imports helm.web at
    the top, so an in-process check inherits the working order and is
    VACUOUSLY GREEN against the bug it claims to cover — which is exactly how
    this survived five gate reds over five weeks on two hosts.
    """

    def test_owner_signal_is_live_without_helm_web_imported_first(self):  # noqa: VACUOUS_ASSERTION — the absence-shaped assertion is assertEqual(r.returncode, 0); its unconditional positive controls sit on the SAME observable (that subprocess's own stdout) two lines later and are both NON-ZERO: owner_unread == 2 and owner_mentions == 1. A crash gives a non-zero rc, and empty stdout makes json.loads raise, so neither pole can pass silently. MUTATION-PROVEN 2026-09-09: the same program run against unfixed main dies NameError name 'os' is not defined and the arm goes red; against this tree it returns owner_unread 2, owner_mentions 1.
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        tmp = tempfile.mkdtemp(prefix="helm-test-wcr-standalone-")
        self.addCleanup(shutil.rmtree, tmp, True)
        env = {k: v for k, v in os.environ.items() if k not in ENV_KEYS}
        env["HELM_HOME"] = os.path.join(tmp, "helm")
        env["HELM_CHAT_DIR"] = os.path.join(tmp, "chat")
        prog = (
            "import json, sys\n"
            "from helm import web_chat_rooms as w\n"
            # the double IS in effect: prove the masking import never happened,
            # or this arm proves nothing about the order it exists to cover.
            "assert 'helm.web' not in sys.modules, 'helm.web got imported'\n"
            "path = w._owner_read_path('main')\n"   # raised NameError before
            # names= is passed EXPLICITLY: _owner_signal falls back to
            # seats.owner_names(), which resolves from ambient config, so the
            # ambient version of this arm passed here and FAILED ON FAB with
            # owner_mentions 0 != 1. A test whose result depends on who ran it
            # is not a test — and the regex is still built from `names`, so
            # this keeps the `re` coverage while removing the environment.
            "sig = w._owner_signal('r', [{'text': 'hello', 'id': 'a1'},\n"
            "                            {'text': '@owner-probe ping', 'id': 'a2'}],\n"
            "                      names={'owner-probe'})\n"
            # `time` is used ONLY by _rooms_summary_cached, so without this
            # call a mutant that drops `import time` alone still passes every
            # assertion above (a probe measured exactly that on 2fde71711).
            # Two calls: the store line always evaluates time.time(), and the
            # second call's cache HIT proves the TTL comparison ran too.
            "first = w._rooms_summary_cached()\n"
            "cached = w._rooms_summary_cached()\n"
            "print(json.dumps({'path': path, 'sig': sig,\n"
            "                  'cache_hit': cached is first}))\n"
        )
        r = subprocess.run([sys.executable, "-c", prog], cwd=root, env=env,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.loads(r.stdout)
        self.assertTrue(d["path"].startswith(env["HELM_CHAT_DIR"]),
                        "owner-read state escaped the tmp chat dir: %s" % d["path"])
        # The discriminating assertions, both NON-ZERO: zeros are exactly what
        # the swallowed NameError produced, and a zero is indistinguishable
        # from "nothing unread" — so an absence assertion here would pass
        # against the bug. owner_mentions is not decoration: the mention regex
        # is the ONLY path that touches `re`, so without it this arm covers os
        # and leaves two of the three missing imports unexercised.
        self.assertEqual(d["sig"]["owner_unread"], 2)
        self.assertEqual(d["sig"]["owner_mentions"], 1)
        # the third import: identity here means _rooms_summary_cached compared
        # time.time() against the stored stamp and took the TTL branch.
        self.assertTrue(d["cache_hit"], "cache miss — the TTL branch never ran")


# THE ROOM SUMMARY'S MEASURED FILL, on the owner's own chat root: six
# consecutive `_rooms_summary` calls at 0.93s / 1.21s median / 1.56s slowest.
_MEASURED_ROOMS_FILL_S = 1.56


class RoomsSummaryTtlIsSizedAgainstItsPollTest(unittest.TestCase):
    """A CACHE THAT EXPIRES AS FAST AS IT IS CONSUMED IS NOT A CACHE.

    THE DEFECT, measured: `_ROOMS_SUM_TTL` was 3.0s, the summary takes ~1.2s
    to build, and /api/chat is polled every 2s — so the entry covered exactly
    ONE poll and expired before the next, and every second poll blocked ~1.2s
    rebuilding it. This tree has shipped the same shape twice before (a 46s
    /api/ready under a 30s ttl, a 6s roster build under a 2.5s one), so both
    relations are pinned here rather than left in prose that cannot go red.
    """

    def test_the_ttl_outlasts_the_fill_it_caches(self):
        self.assertGreater(
            web_chat_rooms._ROOMS_SUM_TTL, _MEASURED_ROOMS_FILL_S,
            "the ttl (%ss) is shorter than the build it caches (%ss), so the "
            "entry can never serve warm and every lapse blocks a caller"
            % (web_chat_rooms._ROOMS_SUM_TTL, _MEASURED_ROOMS_FILL_S))

    def test_the_ttl_outlasts_the_poll_that_consumes_it(self):  # noqa: VACUOUS_ASSERTION — the unconditional must-hit is `len(found) == 1`: the poll cadence is READ off the assembled page and its absence fails the arm before any comparison
        """AND IT MUST OUTLAST THE POLL BY A MULTIPLE, NOT A MARGIN. A ttl
        merely LONGER than one poll interval still misses every second poll.
        The cadence is read off the shipped page so the two numbers cannot
        drift apart in two files."""
        from helm import web_ui_loader
        found = re.findall(r"setInterval\(esStretch\(pollChat\),\s*(\d+)\)",
                           web_ui_loader.read_text())
        # THE MUST-HIT. A renamed poller would leave this arm comparing
        # against nothing and reporting green.
        self.assertEqual(1, len(found),
                         "the chat poll cadence could not be read off the "
                         "assembled page at all: %r" % (found,))
        poll_s = int(found[0]) / 1000.0
        self.assertGreaterEqual(
            web_chat_rooms._ROOMS_SUM_TTL, poll_s * 3,
            "the ttl (%ss) is not a MULTIPLE of the %ss poll that consumes "
            "it, so most polls still pay the rebuild"
            % (web_chat_rooms._ROOMS_SUM_TTL, poll_s))


if __name__ == "__main__":
    unittest.main()
