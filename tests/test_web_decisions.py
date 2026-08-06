#!/usr/bin/env python3
"""helm.web owner DECISION queue — hermetic contract tests.

The web queue is the owner's decision surface (GUI-first law): GET
/api/decisions lists what waits on him; POST /api/decisions/verdict|deliver|
comment route through the SAME ownerasks functions the CLI calls (one writer
path, the store-review precedent) and demand the per-process bearer (403
without). A verdict's delivery leg — the DM back to the FILING SEAT — is
asserted against the asker's real DM lane, not the absence of a complaint.
Ephemeral-port server over tmp HELM_HOME/HELM_CHAT_DIR/HELM_BOARD; the real
~/.helm and /dev/shm/helm-chat are untouched."""
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

from helm import chat, ownerasks, pk, seats, web  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NODE_URL", "MELD_CHAT_NODE_URL",
            "HELM_CHAT_ROOM", "MELD_CHAT_ROOM", "HELM_CHAT_ROOM_SOURCE",
            "MELD_CHAT_ROOM_SOURCE", "HELM_CELL_BIN", "MELD_CELL_BIN",
            "HELM_CELL_PROFILE", "MELD_AGENT_PROFILE", "HELM_BOARD",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME",
            # FILING A CARD NOW POSTS TO THE OWNER'S PHONE. Leaving the topic
            # in the environment would make this suite buzz a real person once
            # per fixture card — the loudest possible test-hygiene leak, and
            # the only one whose blast radius is outside the machine.
            "HELM_NTFY_TOPIC", "MELD_NTFY_TOPIC")

BODY = ("Proxy fork tracking upstream drifted 40 commits.\n"
        "* Rebase now :: one conflict pass today\n"
        "*! Pin and batch :: stable this week; bigger rebase later\n")


class TestWebDecisions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="helm-test-webdec-")
        cls.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        cls.owner_prior = seats._OWNER_NAME
        seats._OWNER_NAME = "owner"
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(cls.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(cls.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""      # transport off — hermetic
        os.environ["HELM_BOARD"] = os.path.join(cls.tmp, "board.json")
        cls.cwd_prior = os.getcwd()
        os.chdir(cls.tmp)
        cls.srv = web.make_server(0)
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
        seats._OWNER_NAME = cls.owner_prior
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        # a clean fixture per test: fresh ledgers/chat/board, one open card
        for sub in ("helm", "chat"):
            shutil.rmtree(os.path.join(self.tmp, sub), ignore_errors=True)
        pk.write_json(os.environ["HELM_BOARD"], {"owner_gated_queue": []})
        ctx, opts, err = ownerasks.parse_card_body(BODY)
        self.assertIsNone(err)
        row, problem = ownerasks.file_decision(
            "proxy fork drift", ctx, opts, "builder-9", refs=["lane/proxy"])
        self.assertIsNotNone(row, problem)
        self.rid = row["id"]

    def req(self, path, payload=None, token=True, raw=False):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        if data and token:
            headers["Authorization"] = "Bearer " + web.MUTATION_TOKEN
        r = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                body = resp.read()
                return resp.status, (body if raw else json.loads(body or b"null"))
        except urllib.error.HTTPError as e:
            with e:
                body = e.read()
                return e.code, (body if raw else json.loads(body or b"null"))

    def dm_lane(self, seat):
        p = chat.room_path(chat.dm_room(seat))
        if not os.path.exists(p):
            return []
        with open(p, encoding="utf-8") as f:
            return [json.loads(l) for l in f]

    def tree_bytes(self):
        """Every durable byte this environment owns — ledgers, board, events
        journal, chat lanes — as {relpath: raw bytes}. A forbidden request
        must leave this map IDENTICAL: folded-status checks alone would let
        a 403 append another open snapshot (folds to the same status), touch
        the board, or journal an event and still read as 'no mutation'
        (codex review, 2026-08-04)."""
        out = {}
        for root, _dirs, files in os.walk(self.tmp):
            for name in files:
                p = os.path.join(root, name)
                with open(p, "rb") as f:
                    out[os.path.relpath(p, self.tmp)] = f.read()
        return out

    # -- GET /api/decisions -------------------------------------------------
    def test_queue_lists_the_open_card_with_its_options(self):
        status, d = self.req("/api/decisions")
        self.assertEqual(status, 200, d)
        self.assertEqual(d["counts"], {"open": 1, "undelivered": 0})
        e = d["entries"][0]
        self.assertEqual((e["id"], e["status"], e["asker"]),
                         (self.rid, "open", "builder-9"))
        self.assertIn("drifted 40 commits", e["context"])
        self.assertEqual([o["label"] for o in e["options"]],
                         ["Rebase now", "Pin and batch"])
        self.assertTrue(e["options"][1]["recommended"])
        self.assertEqual(e["refs"], ["lane/proxy"])

    def test_delivered_cards_leave_the_queue(self):
        ownerasks.decide(self.rid, "1")
        ownerasks.deliver_verdict(self.rid)
        status, d = self.req("/api/decisions")
        self.assertEqual(status, 200)
        self.assertEqual(d["entries"], [])

    def test_lost_dm_returns_to_the_queue_and_web_retry_recovers(self):  # noqa: VACUOUS_ASSERTION — the final empty-entries absence has its unconditional positive control earlier in this SAME test: the same GET returned the dm_lost entry before the recovery POST
        """codex round 3 ([1000]): a reboot-eaten DM must be DISCOVERABLE in
        the browser — the GET projects recently-delivered cards with missing
        evidence back into the queue (dm_lost), and the retry POST reopens
        durably before resending."""
        ownerasks.decide(self.rid, "2")
        first, err = ownerasks.deliver_verdict(self.rid)
        self.assertIsNone(err)
        os.unlink(chat.room_path(chat.dm_room("builder-9")))  # evidence gone
        status, d = self.req("/api/decisions")
        self.assertEqual(status, 200)
        self.assertEqual(d["counts"], {"open": 0, "undelivered": 1})
        e = d["entries"][0]
        self.assertEqual((e["id"], e["status"], e["dm_lost"]),
                         (self.rid, "delivered", True))
        # the web retry is the recovery path: reopen + resend in one POST
        status, d = self.req("/api/decisions/deliver", {"id": self.rid})
        self.assertEqual(status, 200, d)
        self.assertEqual(d["status"], "delivered")
        self.assertNotEqual(d["delivered_ref"], first["delivered_ref"])
        self.assertEqual(len(self.dm_lane("builder-9")), 1)
        _status, d = self.req("/api/decisions")
        self.assertEqual(d["entries"], [], "verified-delivered leaves again")

    def test_lost_dm_with_failed_resend_stays_on_the_queue_as_decided(self):  # noqa: VACUOUS_ASSERTION — every terminal assert here is PRESENCE (counts, the entry tuple); the 400's injected error string propagating into d.error is the control that the patch seam intercepts
        """The failure half: loss observed via the web retry whose resend
        FAILS — the durable reopen must land first, so the card stays on the
        queue as decided (retry still reachable), never invisible."""
        ownerasks.decide(self.rid, "2")
        _first, err = ownerasks.deliver_verdict(self.rid)
        self.assertIsNone(err)
        os.unlink(chat.room_path(chat.dm_room("builder-9")))
        with mock.patch.object(seats, "dm",
                               return_value=(None, "transport down")):
            status, d = self.req("/api/decisions/deliver", {"id": self.rid})
        self.assertEqual(status, 400)
        self.assertIn("visible", d["error"])
        status, d = self.req("/api/decisions")
        self.assertEqual(status, 200)
        self.assertEqual(d["counts"], {"open": 0, "undelivered": 1})
        e = d["entries"][0]
        self.assertEqual((e["id"], e["status"], e["dm_lost"]),
                         (self.rid, "decided", False),
                         "durably reopened — visible as decided, not lost")

    # -- POST /api/decisions/verdict (one writer path + the delivery leg) ---
    def test_verdict_closes_and_DMs_the_asker(self):
        # POSITIVE CONTROL on the board observable BEFORE the verdict: sync
        # the open card in and prove the projection HOLDS a row, so the empty
        # queue asserted after the verdict is the derivation moving — not a
        # sync that never ran over a board that started empty.
        ok, berr = ownerasks.sync_board()
        self.assertTrue(ok, berr)
        before = pk.read_json(os.environ["HELM_BOARD"])["owner_gated_queue"]
        self.assertEqual(len(before), 1)
        self.assertIn("proxy fork drift", before[0]["ask"])
        status, d = self.req("/api/decisions/verdict",
                             {"id": self.rid, "choice": "2",
                              "comment": "batch it"})
        self.assertEqual(status, 200, d)
        self.assertTrue(d["delivered"], d)
        self.assertEqual((d["status"], d["label"]),
                         ("delivered", "Pin and batch"))
        # the SAME ledger the CLI reads sees the closure
        self.assertEqual(ownerasks.decision_rows()[self.rid]["status"],
                         "delivered")
        # the asker's DM lane holds the verdict — the effect, not a no-error
        lane = self.dm_lane("builder-9")
        self.assertEqual(len(lane), 1)
        self.assertIn("Pin and batch", lane[0]["text"])
        self.assertIn("batch it", lane[0]["text"])
        self.assertEqual(lane[0]["from"], "owner")   # the OWNER's word
        # and the board projection emptied (auto-derived, single source)
        self.assertEqual(pk.read_json(os.environ["HELM_BOARD"])
                         ["owner_gated_queue"], [])

    def test_verdict_refusals_are_400(self):
        for payload, want in (({}, "required"),
                              ({"id": self.rid}, "required"),
                              ({"id": "ghost", "choice": "1"}, "no such"),
                              ({"id": self.rid, "choice": "9"}, "matches no")):
            status, d = self.req("/api/decisions/verdict", payload)
            self.assertEqual(status, 400, (payload, d))
            self.assertIn(want, d["error"])
        # UNCONDITIONAL, outside the loop: none of the refusals mutated — the
        # card is still open AND the endpoint still accepts a real verdict
        # (the positive control proving 400s above came from the rules, not
        # from a handler that refuses everything).
        self.assertEqual(ownerasks.decision_rows()[self.rid]["status"], "open")
        status, d = self.req("/api/decisions/verdict",
                             {"id": self.rid, "choice": "1"})
        self.assertEqual(status, 200, d)

    # -- POST /api/decisions/comment (non-closing) --------------------------
    def test_comment_stays_open_and_reaches_the_asker(self):
        status, d = self.req("/api/decisions/comment",
                             {"id": self.rid, "text": "cost of the rebase?"})
        self.assertEqual(status, 200, d)
        self.assertEqual(d["status"], "open")
        self.assertIn("cost of the rebase?",
                      self.dm_lane("builder-9")[0]["text"])

    # -- mutation hardening: every decision POST demands the bearer ---------
    def test_decision_posts_403_without_token(self):  # noqa: VACUOUS_ASSERTION — the byte-identity and empty-lane absences have their unconditional positive control at the END of this same test: the bearer'd comment CHANGES the byte map and fills the SAME lane, so the identity above measured the 403
        before = self.tree_bytes()
        for ep, payload in (("/api/decisions/verdict",
                             {"id": self.rid, "choice": "1"}),
                            ("/api/decisions/deliver", {"id": self.rid}),
                            ("/api/decisions/comment",
                             {"id": self.rid, "text": "x"})):
            status, d = self.req(ep, payload, token=False)
            self.assertEqual(status, 403, (ep, d))
            self.assertIn("error", d)
        # a 403 must not mutate ANY durable byte — raw ledgers, board,
        # events journal, chat lanes, byte-identical — not merely the folded
        # status (an appended open snapshot folds to the same status).
        self.assertEqual(before, self.tree_bytes(),
                         "a forbidden request changed durable bytes")
        self.assertEqual(ownerasks.decision_rows()[self.rid]["status"], "open")
        self.assertEqual(self.dm_lane("builder-9"), [])
        # POSITIVE CONTROL on the same observables: WITH the bearer, the same
        # endpoint family mutates — the byte map CHANGES (the comparator
        # provably sees writes) and the same lane fills — so the identity
        # above measured the 403, not a blind fingerprint.
        status, d = self.req("/api/decisions/comment",
                             {"id": self.rid, "text": "with the bearer"})
        self.assertEqual(status, 200, d)
        self.assertNotEqual(before, self.tree_bytes())
        self.assertEqual(len(self.dm_lane("builder-9")), 1)

    # -- the UI carries the queue markup + JS + the routes ------------------
    def test_ui_decision_markup_and_js(self):
        status, body = self.req("/", raw=True)
        self.assertEqual(status, 200)
        body = body.decode("utf-8")
        for marker in ('id="odq"', 'id="odqlist"', 'id="odqmeta"',
                       'id="odqReload"', 'class="odqcard', 'id="odqbadge"'):
            self.assertIn(marker, body, "decision markup missing: %s" % marker)
        for fn in ("odqInit", "odqRender", "odqCard", "odqAct", "odqBadge"):
            self.assertIn("function " + fn, body, "decision JS missing: %s" % fn)
        self.assertIn("re-send from the ledger", body,
                      "the dm_lost retry surface is missing")
        for route in ("/api/decisions", "/api/decisions/verdict",
                      "/api/decisions/deliver", "/api/decisions/comment"):
            self.assertIn(route, body)

    def test_ui_queue_renders_on_the_work_tab_not_home(self):  # noqa: VACUOUS_ASSERTION — body.index() RAISES on any missing marker, so every marker is asserted present before the order comparison can run
        """Owner ruling 2026-08-04 (live at the console, superseding both
        earlier placements): decisions, the task backlog and store review
        compose into ONE owner<->fleet surface — a new `work` tab. This lane
        ships the decisions section inside the tab's section container
        (#worksections, so #218's siblings join it without restructuring);
        the home deck carries at most a count that links there. Pinned by
        document order: the queue section sits inside view-work (after its
        open, before the next view), and the home badge (inside view-helm)
        precedes view-quota."""
        _status, body = self.req("/", raw=True)
        body = body.decode("utf-8")
        self.assertIn('data-v="work">work</button>', body,
                      "the work navtab is missing")
        order = [body.index(m) for m in (
            'id="odqbadge"', 'id="view-work"', 'id="worksections"',
            'id="odq"', 'id="view-chat"')]
        self.assertEqual(order, sorted(order),
                         "markup order broke the work-tab placement")
        self.assertIn('"work"', body.split("const VIEWS")[1][:120],
                      "the VIEWS array does not route the work tab")
        # view-helm is the first view and view-quota the second: a badge
        # before view-quota is inside the home view, not a later tab
        self.assertLess(body.index('id="odqbadge"'),
                        body.index('id="view-quota"'),
                        "the badge belongs on the home view")
        self.assertIn('href="#work"', body, "the badge links to the work tab")


if __name__ == "__main__":
    unittest.main()
