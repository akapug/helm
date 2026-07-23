#!/usr/bin/env python3
"""helm.web /api/ledger/native — the live LOCAL activity surface, hermetic.
LOCAL reads only (the attest-chain coordination fallback + events journal +
RAM chat pulse — never a dregg/cave claim), no node, GET-only; every leg
fails open to an empty section at 200, never a 500. The projection is bounded
(NATIVE_REC_KEYS — no whole-record passthrough) and a tampered chain surfaces
as verified:false + detail, never hidden."""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import web  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_NODE_URL", "MELD_NODE_URL",
            "HELM_CHAT_DIR", "HELM_ACTOR")


class NativeBase(unittest.TestCase):
    """Shared scaffold: tmp HELM_HOME + tmp RAM-room dir, an ephemeral web
    server, req(). Subclasses seed fixtures in seed() BEFORE the server runs."""

    @classmethod
    def seed(cls):
        pass

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="helm-test-webnative-")
        cls.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = cls.tmp
        os.environ["HELM_CHAT_DIR"] = os.path.join(cls.tmp, "chat-ram")
        cls.seed()
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

    def req(self, path, payload=None):
        """(status, obj) — 4xx/5xx returned, not raised."""
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json",
                   "Authorization": "Bearer " + web.MUTATION_TOKEN} if data else {}
        r = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"null")


def _seed_chain():
    """Two records through the REAL writer (anchor off — no network leg)."""
    from helm import premise
    first = premise.record_attestation(
        "create", "p-first", premise.digest_payload("the first truth"),
        root="global", attest_by="helm-test", anchor=False)
    premise.record_attestation(
        "supersede", "p-second", premise.digest_payload("the evolved truth"),
        root="global", project="helm", attest_by="helm-test",
        supersedes="p-first", supersedes_record=first["rec_hash"], anchor=False)


def _seed_events(n=2):
    from helm import pk
    for i in range(n):
        pk.event("store.add", "target-%d" % i, "receipt %d" % i, actor="tester")


def _seed_chat():
    d = os.environ["HELM_CHAT_DIR"]
    os.makedirs(d, exist_ok=True)
    rows = [{"ts": "2026-07-21T10:00:00Z", "from": "kimi", "text": "hi"},
            {"ts": "2026-07-21T10:00:05Z", "from": "codex", "text": "yo"},
            {"ts": "2026-07-21T10:00:09Z", "from": "david", "react": "🔥",
             "tts": "2026-07-21T10:00:05Z", "tfrom": "codex"}]
    with open(os.path.join(d, "main.jsonl"), "w", encoding="utf-8") as f:
        f.write("".join(json.dumps(r) + "\n" for r in rows))


class TestNativeLedger(NativeBase):
    @classmethod
    def seed(cls):
        _seed_chain()
        _seed_events()
        _seed_chat()

    def test_chain_projection_newest_first_and_bounded(self):
        status, d = self.req("/api/ledger/native")
        self.assertEqual(status, 200)
        c = d["chain"]
        self.assertEqual((c["count"], c["head_index"], c["verified"]),
                         (2, 1, True))
        self.assertEqual(c["detail"], "")
        self.assertEqual([r["chain_index"] for r in c["records"]], [1, 0])
        self.assertEqual(c["head_hash"], c["records"][0]["rec_hash"])
        # bounded projection — provenance keys only, never the whole record
        self.assertEqual(set(c["records"][0]), set(web.NATIVE_REC_KEYS))
        self.assertNotIn("prev", c["records"][0])
        head = c["records"][0]
        self.assertEqual((head["op"], head["premise_id"], head["project"],
                          head["attest_by"]),
                         ("supersede", "p-second", "helm", "helm-test"))

    def test_events_are_receipts_newest_first(self):
        _, d = self.req("/api/ledger/native")
        self.assertEqual([e["target"] for e in d["events"]],
                         ["target-1", "target-0"])
        self.assertEqual(d["events"][0]["verb"], "store.add")
        self.assertEqual(d["events"][0]["actor"], "tester")

    def test_chat_pulse_counts_msgs_not_reacts(self):
        _, d = self.req("/api/ledger/native")
        p = d["chat"]
        self.assertEqual((p["rooms"], p["msgs"]), (1, 2))
        # a reaction IS activity — the newest row wins last_*
        self.assertEqual((p["last_from"], p["last_room"]), ("david", "main"))
        self.assertEqual(p["last_ts"], "2026-07-21T10:00:09Z")

    def test_read_only(self):
        status, d = self.req("/api/ledger/native", payload={"x": 1})
        self.assertEqual(status, 404)


class TestNativeLedgerEmptyHome(NativeBase):
    def test_everything_fails_open_empty(self):
        status, d = self.req("/api/ledger/native")
        self.assertEqual(status, 200)
        self.assertEqual(d["chain"], {"count": 0, "verified": True,
                                      "detail": "", "head_index": None,
                                      "head_hash": "", "records": []})
        self.assertEqual(d["events"], [])
        self.assertEqual(d["chat"], {"rooms": 0, "msgs": 0, "last_ts": "",
                                     "last_from": "", "last_room": ""})


class TestNativeLedgerTamper(NativeBase):
    @classmethod
    def seed(cls):
        _seed_chain()
        # tamper: flip the head record's premise_id — rec_hash stops recomputing
        from helm import premise
        path = premise._chain_path()
        with open(path, encoding="utf-8") as f:
            lines = [json.loads(x) for x in f.read().splitlines() if x]
        lines[-1]["premise_id"] = "forged"
        with open(path, "w", encoding="utf-8") as f:
            f.write("".join(json.dumps(x) + "\n" for x in lines))

    def test_tamper_surfaces_honestly(self):
        status, d = self.req("/api/ledger/native")
        self.assertEqual(status, 200)  # surfaced, never a 500
        c = d["chain"]
        self.assertFalse(c["verified"])
        self.assertIn("rec_hash does not recompute", c["detail"])
        self.assertEqual(c["count"], 2)  # the records still project


class TestSeatEphemeralTag(unittest.TestCase):
    """The picker-declutter tag: an ephemeral review-SA (agent-<hex>, no home,
    /tmp cwd) is flagged so the live 'message a seat' picker can hide it, while a
    real seat is NEVER mis-hidden (owner feature 2026-07-23)."""

    def test_ephemeral_sa_is_flagged(self):
        self.assertTrue(web._seat_ephemeral(
            {"seat": "agent-047d53ef", "home_room": None,
             "cwd": "/tmp/claude-xyz/scratch"}))

    def test_real_named_seats_are_never_flagged(self):
        # real names, or a real home, or a non-/tmp cwd -> keep in the picker
        for s in (
            {"seat": "console-design", "home_room": None, "cwd": "/tmp/x"},
            {"seat": "codex-2", "home_room": "main", "cwd": "/home/p/helm-wt/x"},
            {"seat": "agent-047d53ef", "home_room": "main", "cwd": "/tmp/x"},
            {"seat": "agent-047d53ef", "home_room": None, "cwd": "/home/p/proj"},
            {"seat": "projx-agent", "home_room": None, "cwd": "/tmp/x"},  # not agent-<hex>
        ):
            self.assertFalse(web._seat_ephemeral(s), s["seat"])

    def test_fail_safe_on_junk(self):
        self.assertFalse(web._seat_ephemeral({}))
        self.assertFalse(web._seat_ephemeral({"seat": None}))


class TestRosterSingleFlightCache(unittest.TestCase):
    """The poll-fan-in guard: roster_report is the one heavy read on the 2s
    poll path; uncached, N concurrent polls stacked N computes and blanked the
    owner's UI (live incident 2026-07-23). The cache must be SINGLE-FLIGHT
    (concurrent pollers share one compute), TTL-fresh, and publish-ready
    (ephemeral baked in) — decision-spirit #22: memory is the read-path."""

    def setUp(self):
        web._ROSTER_REP_CACHE.clear()
        self.calls = {"n": 0}

    def tearDown(self):
        # the polluter owns cleanup: the module-level cache holds this test's
        # fake rep; clear it so a later test's roster endpoint reads its own
        # planted data, not our leaked {seat: agent-047d53ef} (kimi xrev).
        web._ROSTER_REP_CACHE.clear()

    def _fake_report(self, room):
        self.calls["n"] += 1
        return {"seats": [{"seat": "agent-047d53ef", "home_room": None,
                           "cwd": "/tmp/claude-x/s"}], "claims": []}

    def test_concurrent_pollers_share_one_compute(self):
        with mock.patch("helm.seats.roster_report", self._fake_report):
            threads = [threading.Thread(target=web._roster_cached, args=("main",))
                       for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(self.calls["n"], 1)   # single-flight, not 8 stacked

    def test_ttl_expiry_recomputes_and_freshness_serves_cache(self):
        with mock.patch("helm.seats.roster_report", self._fake_report):
            web._roster_cached("main")
            web._roster_cached("main")          # within TTL -> cache hit
            self.assertEqual(self.calls["n"], 1)
            at, rep = web._ROSTER_REP_CACHE["main"]
            web._ROSTER_REP_CACHE["main"] = (at - (web._ROSTER_REP_TTL + 1), rep)
            web._roster_cached("main")          # expired -> one recompute
            self.assertEqual(self.calls["n"], 2)

    def test_cached_rep_is_publish_ready(self):
        # the ephemeral tag is baked at compute time, so every consumer
        # (panel read + roster-git scan) gets the SAME fully-tagged rep
        with mock.patch("helm.seats.roster_report", self._fake_report):
            rep = web._roster_cached("main")
        self.assertTrue(rep["seats"][0]["ephemeral"])   # agent-<hex> + /tmp

    def test_rooms_cache_independently(self):
        with mock.patch("helm.seats.roster_report", self._fake_report):
            web._roster_cached("main")
            web._roster_cached("helm-dogfood")
        self.assertEqual(self.calls["n"], 2)
        self.assertIn("main", web._ROSTER_REP_CACHE)
        self.assertIn("helm-dogfood", web._ROSTER_REP_CACHE)


if __name__ == "__main__":
    unittest.main()
