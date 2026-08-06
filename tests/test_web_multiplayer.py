#!/usr/bin/env python3
"""helm.web multiplayer HTTP seam — hermetic contract tests. GET
/api/multiplayer/state is the relay read (open on loopback, like every GET);
POST publish/presence are owner mutations (bearer-gated, 403 without). The blind
relay stays blind — the endpoint passes opaque envelopes through and a CLIENT
folds the demo CRDT.

The cockpit tab that used to be that client was retired on 2026-07-30 (its one
owner-facing payload, the fleet's notes, moved to `helm note` + the home tab).
These endpoints deliberately OUTLIVED it as the adapter contract's HTTP face,
which is exactly why this file still exists.

Tmp HELM_MULTIPLAYER_DIR; /dev/shm/helm-multiplayer and the real ~/.helm are
never touched."""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import multiplayer, multiplayer_demo, seats, web  # noqa: E402

ENV_KEYS = ("HELM_MULTIPLAYER_DIR", "HELM_MULTIPLAYER_CAVE",
            "HELM_MULTIPLAYER_ACTOR", "HELM_MULTIPLAYER_CONNECTION",
            "HELM_MULTIPLAYER_BACKEND", "HELM_CHAT_ROOM", "HELM_CHAT_NAME",
            "HELM_CELL_PROFILE", "MELD_AGENT_PROFILE")


class TestWebMultiplayer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="helm-test-webmp-")
        cls.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        cls.owner_prior = seats._OWNER_NAME
        seats._OWNER_NAME = "owner"
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_MULTIPLAYER_DIR"] = os.path.join(cls.tmp, "mp")
        cls.srv = web.make_server(0)  # ephemeral port
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.thread.join(timeout=5)
        for k, v in cls.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        seats._OWNER_NAME = cls.owner_prior
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        shutil.rmtree(os.environ["HELM_MULTIPLAYER_DIR"], ignore_errors=True)

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

    def test_state_on_empty_cave_is_clean(self):
        s, d = self.req("/api/multiplayer/state?cave=demo&doc=board")
        self.assertEqual(s, 200)
        self.assertEqual((d["updates"], d["peers"], d["cursor"]), ([], [], "0"))
        self.assertEqual((d["cave"], d["doc"]), ("demo", "board"))

    def test_owner_publish_appears_and_demands_the_bearer(self):
        # CSRF: publish without the bearer is refused and writes nothing
        s, _ = self.req("/api/multiplayer/publish",
                        {"cave": "demo", "key": "greeting", "value": "hi"},
                        token=False)
        self.assertEqual(s, 403)
        self.assertEqual(self.req("/api/multiplayer/state?cave=demo&doc=board")[1]
                         ["updates"], [])
        # with the bearer it lands as the owner seat 'owner'
        s, d = self.req("/api/multiplayer/publish",
                        {"cave": "demo", "key": "greeting", "value": "hi"})
        self.assertEqual(s, 200)
        self.assertEqual(d["ack"]["actor"], "owner")
        s, d = self.req("/api/multiplayer/state?cave=demo&doc=board")
        self.assertEqual([u["actor"] for u in d["updates"]], ["owner"])
        self.assertEqual(d["caves"], ["demo"])  # discoverable off the doc header

    def test_two_actors_converge_by_lww_through_the_blind_relay(self):
        # the owner writes greeting via the endpoint...
        self.req("/api/multiplayer/publish",
                 {"cave": "demo", "key": "greeting", "value": "hello owner"})
        # ...a 'terminal' (another client) writes the SAME key LATER, straight
        # through the blind relay, plus a second key
        relay, _ = multiplayer.adapters()
        time.sleep(0.01)  # a real gap so codex's ts strictly follows owner's
        relay.publish("demo", "board", "codex",
                      multiplayer_demo.encode("greeting", "hi codex", "codex"))
        relay.publish("demo", "board", "codex",
                      multiplayer_demo.encode("status", "building", "codex"))
        s, d = self.req("/api/multiplayer/state?cave=demo&doc=board")
        self.assertEqual(s, 200)
        # the endpoint stays blind — it passes 3 raw envelopes, folds NOTHING
        self.assertEqual(len(d["updates"]), 3)
        self.assertTrue(all("update" in u for u in d["updates"]))
        # the CLIENT's fold (what the browser runs) converges: the later write wins
        board = {c["key"]: c
                 for c in multiplayer_demo.materialize(d["updates"])["board"]}
        self.assertEqual((board["greeting"]["value"], board["greeting"]["actor"]),
                         ("hi codex", "codex"))
        self.assertEqual(board["status"]["value"], "building")

    def test_presence_makes_the_owner_a_peer_and_is_bearer_gated(self):
        self.assertEqual(self.req("/api/multiplayer/presence", {"cave": "demo"},
                                  token=False)[0], 403)
        s, d = self.req("/api/multiplayer/presence",
                        {"cave": "demo", "state": "watching"})
        self.assertEqual(s, 200)
        self.assertEqual((d["peer"]["actor"], d["peer"]["connection"]),
                         ("owner", "cockpit"))
        s, d = self.req("/api/multiplayer/state?cave=demo&doc=board")
        self.assertEqual([(p["actor"], p["state"]) for p in d["peers"]],
                         [("owner", "watching")])

    def test_publish_rejects_bad_key_and_state_rejects_bad_cave_name(self):
        self.assertEqual(self.req("/api/multiplayer/publish",
                                  {"cave": "demo", "key": "  ", "value": "x"})[0], 400)
        self.assertEqual(self.req("/api/multiplayer/publish",
                                  {"cave": "demo", "key": "k", "value": 7})[0], 400)
        bad = urllib.parse.quote("bad\x1bname")
        self.assertEqual(self.req("/api/multiplayer/state?cave=" + bad)[0], 400)

    def test_stale_cursor_resets_from_head_not_500(self):
        self.req("/api/multiplayer/publish", {"cave": "demo", "key": "k", "value": "v"})
        s, d = self.req("/api/multiplayer/state?cave=demo&doc=board&after=deadbeef:4")
        self.assertEqual(s, 200)
        self.assertTrue(d["reset"])
        self.assertEqual(len(d["updates"]), 1)  # replayed from the head

    def test_state_launders_relay_supplied_identity_fields(self):
        # bidi (U+202E, category Cf) passes the source-side _identity validator
        # (it rejects only Cc), so a relay client CAN plant it in actor/
        # connection/state/cave. The web sink must launder every identity/display
        # field it emits — the analog of the CLI's launder + the chat wire's
        # public_rows — while the opaque `update` rides through RAW: the browser
        # folds the CRDT and launders the decoded cell at its own render seam.
        BIDI = "‮"
        relay, presence = multiplayer.adapters()
        relay.publish("demo", "board", "code" + BIDI + "x",
                      multiplayer_demo.encode("greeting", "he" + BIDI + "llo",
                                              "code" + BIDI + "x"))
        presence.heartbeat("demo", "ag" + BIDI + "ent", "bu" + BIDI + "ild",
                           connection="ta" + BIDI + "b1")
        relay.publish("ca" + BIDI + "ve9", "board", "x",
                      multiplayer_demo.encode("k", "v", "x"))
        s, d = self.req("/api/multiplayer/state?cave=demo&doc=board")
        self.assertEqual(s, 200)
        p = d["peers"][0]
        # peer identity/display columns laundered; the visible name still survives
        self.assertEqual((p["actor"], p["connection"], p["state"]),
                         ("agent", "tab1", "build"))
        self.assertEqual(d["updates"][0]["actor"], "codex")  # envelope actor
        self.assertIn("cave9", d["caves"])                   # discoverable name
        # NO server-emitted identity field carries the bidi override
        for v in (p["actor"], p["connection"], p["state"],
                  d["updates"][0]["actor"], *d["caves"]):
            self.assertNotIn(BIDI, v)
        # BUT the opaque update rides through RAW — the relay stays blind, and
        # whichever client DECODES it launders at its own render seam (the CLI
        # fold: tests/test_multiplayer.py::test_cli_status_launders_relay_supplied_values).
        self.assertIn(BIDI, d["updates"][0]["update"])

    def test_the_served_page_no_longer_folds_an_opaque_update(self):
        """The cave tab was retired on 2026-07-30, and with it the ONE reason a
        browser-side launder existed: the page folded the blind relay's opaque
        updates itself, so the server structurally could not launder a decoded
        cell and the client had to. Nothing on this page reads these endpoints
        now, so the client fold and its launder went WITH the panel instead of
        sitting unused and drifting out of step with the encoder.

        This is not a coverage loss, it is a move — the decode seams that
        remain are both covered above/elsewhere:
          • the SERVER wire (peer + envelope actor + cave names) launders in
            _mp_pub and is pinned by test_state_launders_relay_supplied_identities;
          • the TERMINAL fold launders in multiplayer._display and is pinned by
            tests/test_multiplayer.py::test_cli_status_launders_relay_supplied_values.
        """
        with urllib.request.urlopen("http://127.0.0.1:%d/" % self.port,
                                    timeout=10) as resp:
            html = resp.read().decode("utf-8")
        for gone in ('data-v="cave"', 'id="view-cave"', "function materializeCave",
                     "function pollCave", "mpLaunder(", "/api/multiplayer/"):
            self.assertNotIn(gone, html, "%s survived the cave tab" % gone)
        # the demo CRDT kind is the CLIENT's word, and the only client left is
        # the CLI — the browser must not carry a second, drifting copy of it
        self.assertNotIn(multiplayer_demo.KIND, html)

    def test_the_endpoints_outlive_the_tab(self):
        """No tab reads them; they are still SERVED. They are the HTTP face of
        the adapter contract for a bridge or a script, and retiring an owner
        surface is not a reason to break a protocol seam."""
        self.assertIn("/api/multiplayer/state", web.QUERY_API)
        self.assertIn("/api/multiplayer/publish", web.POST_API)
        self.assertIn("/api/multiplayer/presence", web.POST_API)
        s, d = self.req("/api/multiplayer/state?cave=demo&doc=board")
        self.assertEqual(s, 200)
        self.assertEqual(d["cave"], "demo")


if __name__ == "__main__":
    unittest.main()
