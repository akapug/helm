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
import time
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
            {"ts": "2026-07-21T10:00:09Z", "from": "owner", "react": "🔥",
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
        self.assertEqual((p["last_from"], p["last_room"]), ("owner", "main"))
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
    real seat is NEVER mis-hidden (owner feature)."""

    def test_ephemeral_sa_is_flagged(self):
        self.assertTrue(web._seat_ephemeral(
            {"seat": "agent-047d53ef", "home_room": None,
             "cwd": "/tmp/claude-xyz/scratch"}))

    def test_real_named_seats_are_never_flagged(self):
        # real names, or a real home, or a non-/tmp cwd -> keep in the picker
        for s in (
            {"seat": "design-reviewer", "home_room": None, "cwd": "/tmp/x"},
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
        # planted data, not our leaked {seat: agent-047d53ef} (cross-family review).
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
            web._roster_cached("side-room")
        self.assertEqual(self.calls["n"], 2)
        self.assertIn("main", web._ROSTER_REP_CACHE)
        self.assertIn("side-room", web._ROSTER_REP_CACHE)


class TestRoomsSummarySingleFlightCache(unittest.TestCase):
    """Brick #2 of the poll->push read-model: _rooms_summary (~14s at 224
    seats x 13 rooms) ran uncached on every /api/chat poll — N clients
    stacked N computes -> ~60s requests -> thread-pool starvation (the chat
    half of the 2026-07-23 UI-blank). Single-flight + TTL, and the cache is
    KEYED BY THE CHAT ROOT so isolated test worlds (fresh tmp roots) can
    never read each other's cached summary — the cross-test-pollution class
    from brick #1, closed structurally."""

    def setUp(self):
        web._ROOMS_SUM_CACHE.clear()
        self.addCleanup(web._ROOMS_SUM_CACHE.clear)
        self.calls = {"n": 0}

    def _fake_summary(self, roster=None):
        self.calls["n"] += 1
        return [{"room": "main", "total": 1, "last": None,
                 "owner_unread": 0, "owner_mentions": 0, "seats": []}]

    def test_concurrent_pollers_share_one_compute(self):
        with mock.patch.object(web, "_rooms_summary", self._fake_summary):
            threads = [threading.Thread(target=web._rooms_summary_cached)
                       for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(self.calls["n"], 1)   # single-flight, not 8 stacked

    def test_ttl_freshness_and_expiry(self):
        with mock.patch.object(web, "_rooms_summary", self._fake_summary):
            web._rooms_summary_cached()
            web._rooms_summary_cached()          # within TTL -> cache hit
            self.assertEqual(self.calls["n"], 1)
            (key, (at, s)), = web._ROOMS_SUM_CACHE.items()
            web._ROOMS_SUM_CACHE[key] = (at - (web._ROOMS_SUM_TTL + 1), s)
            web._rooms_summary_cached()          # expired -> one recompute
            self.assertEqual(self.calls["n"], 2)

    def test_cache_is_keyed_by_chat_root(self):
        # two different chat roots -> two independent entries: an isolated
        # test world (or a future multi-root deployment) can NEVER be served
        # another root's cached summary
        with mock.patch.object(web, "_rooms_summary", self._fake_summary):
            with mock.patch("helm.chat.chat_dir", return_value="/dev/shm/world-a"):
                web._rooms_summary_cached()
                web._rooms_summary_cached()      # same root -> cache hit
            with mock.patch("helm.chat.chat_dir", return_value="/dev/shm/world-b"):
                web._rooms_summary_cached()      # new root -> own compute
        self.assertEqual(self.calls["n"], 2)
        self.assertEqual(sorted(web._ROOMS_SUM_CACHE),
                         ["/dev/shm/world-a", "/dev/shm/world-b"])

    def test_owner_write_paths_invalidate(self):
        # read-your-own-writes: an owner action (read-ack / post / dm) busts
        # the current root's entry so the NEXT poll recomputes — the unread
        # badge zeroes immediately, the owner's post shows immediately
        with mock.patch.object(web, "_rooms_summary", self._fake_summary):
            web._rooms_summary_cached()
            self.assertEqual(self.calls["n"], 1)
            web._rooms_summary_invalidate()
            web._rooms_summary_cached()          # busted -> recompute
            self.assertEqual(self.calls["n"], 2)


class TestSseDoorbellWatcher(unittest.TestCase):
    """The push leg's ground truth, PER-SERVER (meld-converged): each server
    owns its watcher state, so the concurrency races (generation, refcount,
    cross-server kill) are unexpressible — these tests pin the properties
    that REMAIN expressible: fingerprint scope, invalidate-before-ring,
    per-server death detection, crash containment, arm rollback, and
    cross-server isolation."""

    def setUp(self):
        import tempfile
        self.d = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.d, "dm"), exist_ok=True)
        self._p = mock.patch("helm.chat.chat_dir", return_value=self.d)
        self._p.start()
        self.addCleanup(self._p.stop)
        self.addCleanup(lambda: shutil.rmtree(self.d, ignore_errors=True))
        self.srv = web.make_server(0)
        self.addCleanup(self.srv.server_close)   # stops its own watcher
        web._ROOMS_SUM_CACHE.clear()
        self.addCleanup(web._ROOMS_SUM_CACHE.clear)

    def _append(self, rel, row='{"from":"x","text":"r"}\n'):
        with open(os.path.join(self.d, rel), "a") as f:
            f.write(row)

    def _arm_state_only(self):
        # unit tests drive _sse_tick directly — mark running without a thread
        with self.srv._sse["cond"]:
            self.srv._sse["running"] = True
            self.srv._sse["beat"] = time.time()

    def _drain_watchers(self):
        deadline = time.time() + 3
        while time.time() < deadline and any(
                t.name == "helm-sse-watcher" and t.is_alive()
                for t in threading.enumerate()):
            time.sleep(0.02)
        return not any(t.name == "helm-sse-watcher" and t.is_alive()
                       for t in threading.enumerate())

    def test_fingerprint_scopes_to_canonical_logs_only(self):
        self._append("main.jsonl")
        fp0 = web._chat_fingerprint()
        self.assertEqual(fp0, web._chat_fingerprint())      # still = still
        # cursor/lock/stopwhisper noise must NOT move it (7/8 doorbells were
        # this class of noise before the scope fix)
        self._append("main.cursor.agent-abc123")
        self._append("main.cursor.agent-abc123.lock", "x")
        self._append("main.stopwhisper.tmp-claude-1", "x")
        self.assertEqual(fp0, web._chat_fingerprint())
        # a room append RINGS
        self._append("main.jsonl")
        fp1 = web._chat_fingerprint()
        self.assertNotEqual(fp0, fp1)
        # a DM append in the dm/ SUBDIR rings too (the flat-listdir miss)
        self._append(os.path.join("dm", "alice.jsonl"))
        self.assertNotEqual(fp1, web._chat_fingerprint())

    def test_fingerprint_fails_open_on_missing_dir(self):
        with mock.patch("helm.chat.chat_dir", return_value="/nonexistent-x"):
            self.assertIsNone(web._chat_fingerprint())      # never raises

    def test_tick_invalidates_summary_before_ring(self):
        # a doorbell's whole point: the poll it triggers reads FRESH state —
        # the tick must pop the (global) summary cache before notifying
        self._arm_state_only()
        web._ROOMS_SUM_CACHE[self.d] = (time.time(), [{"room": "stale"}])
        self._append("main.jsonl")
        self.assertTrue(web._sse_tick(self.srv))
        self.assertNotIn(self.d, web._ROOMS_SUM_CACHE)      # invalidated
        self.assertFalse(web._sse_tick(self.srv))           # quiet = no ring

    def test_stopped_server_tick_is_a_noop(self):
        # kimi pressure-test #2: an in-flight tick on a stopped server acts
        # on NOTHING — running is checked under the server's own cond
        self._append("main.jsonl")
        self.assertFalse(web._sse_tick(self.srv))           # running=False
        self.assertEqual(self.srv._sse["beat"], 0.0)        # untouched

    def test_death_predicate_is_scoped_per_server(self):
        # kimi's ONE clear-condition: the predicate reads THAT server's
        # beat — one server's dead watcher can never false-trigger another's
        other = web.make_server(0)
        self.addCleanup(other.server_close)
        with self.srv._sse["cond"]:
            self.srv._sse.update({"running": True, "beat": time.time()})
        with other._sse["cond"]:
            other._sse.update(
                {"running": True,
                 "beat": time.time() - (web._SSE_DEAD_S + 1)})
        self.assertFalse(web._sse_watcher_dead(self.srv._sse))  # alive
        self.assertTrue(web._sse_watcher_dead(other._sse))      # dead
        with self.srv._sse["cond"]:
            self.srv._sse["running"] = False
        self.assertTrue(web._sse_watcher_dead(self.srv._sse))   # unarmed

    def test_crashed_watcher_drops_running_for_rearm(self):
        # containment: however the loop dies, THIS server's next client can
        # re-arm — a set flag with no thread = the wedged-UI class
        self._arm_state_only()
        boot = self.srv._sse["boot"]
        with mock.patch.object(web, "_sse_tick", side_effect=RuntimeError):
            with mock.patch.object(web, "_SSE_WATCH_S", 0.01):
                t = threading.Thread(target=web._sse_watcher,
                                     args=(self.srv, boot), daemon=True)
                t.start()
                t.join(timeout=3)
        self.assertFalse(t.is_alive())
        self.assertFalse(self.srv._sse["running"])           # flag dropped

    def test_superseded_thread_never_stomps_its_successor(self):
        # the boot token's reason to exist (found by THIS suite's liveness
        # pin): a superseded thread must exit WITHOUT clearing running, and
        # its finally must not stomp the successor's fresh flag
        self._arm_state_only()
        old_boot = self.srv._sse["boot"]
        with self.srv._sse["cond"]:
            self.srv._sse["boot"] += 1        # a successor armed
        with mock.patch.object(web, "_SSE_WATCH_S", 0.01):
            t = threading.Thread(target=web._sse_watcher,
                                 args=(self.srv, old_boot), daemon=True)
            t.start()
            t.join(timeout=3)
        self.assertFalse(t.is_alive())        # exited on boot mismatch...
        self.assertTrue(self.srv._sse["running"])   # ...touching NOTHING
        # and a superseded tick is equally inert
        self._append("main.jsonl")
        self.assertFalse(web._sse_tick(self.srv, old_boot))

    def test_stop_then_rearm_yields_one_watcher(self):
        # per-server there is no generation to race, but the LIVENESS
        # property stays pinned: stop + re-arm never leaves two live loops
        with mock.patch.object(web, "_SSE_WATCH_S", 0.01):
            self.assertTrue(web._sse_ensure_watcher(self.srv))
            time.sleep(0.05)
            web._sse_stop(self.srv)
            self.assertTrue(web._sse_ensure_watcher(self.srv))
            deadline = time.time() + 3
            while time.time() < deadline:
                alive = [t for t in threading.enumerate()
                         if t.name == "helm-sse-watcher" and t.is_alive()]
                if len(alive) <= 1:
                    break
                time.sleep(0.02)
        self.assertEqual(len([t for t in threading.enumerate()
                              if t.name == "helm-sse-watcher"
                              and t.is_alive()]), 1)
        self.assertTrue(self.srv._sse["running"])

    def test_arm_rolls_back_when_thread_start_refuses(self):
        with mock.patch.object(threading.Thread, "start",
                               side_effect=RuntimeError("no threads")):
            self.assertFalse(web._sse_ensure_watcher(self.srv))
        self.assertFalse(self.srv._sse["running"])
        # ...and the NEXT client on this server arms for real
        self.assertTrue(web._sse_ensure_watcher(self.srv))
        web._sse_stop(self.srv)

    def test_invalidate_outwaits_an_inflight_compute(self):
        # concurrency race, still pinned: a compute that entered its locked fill
        # BEFORE the invalidation must not survive it (global cache law)
        def slow_summary(roster=None):
            time.sleep(0.15)
            return [{"room": "stale-world"}]
        with mock.patch.object(web, "_rooms_summary", slow_summary):
            t = threading.Thread(target=web._rooms_summary_cached, daemon=True)
            t.start()
            time.sleep(0.05)                 # compute is mid-fill, lock held
            web._rooms_summary_invalidate()  # blocks until publish, then pops
            t.join(timeout=3)
        self.assertNotIn(self.d, web._ROOMS_SUM_CACHE)

    def test_closing_one_server_never_touches_anothers_watcher(self):
        # the cross-server kill race, now unexpressible — pinned anyway:
        # A's close stops A's watcher and ONLY A's
        other = web.make_server(0)
        with mock.patch.object(web, "_SSE_WATCH_S", 0.01):
            self.assertTrue(web._sse_ensure_watcher(self.srv))
            self.assertTrue(web._sse_ensure_watcher(other))
            other.server_close()                    # A closes...
            self.assertFalse(other._sse["running"])
            self.assertTrue(self.srv._sse["running"])   # ...B untouched
            other.server_close()                    # double-close: idempotent
            self.assertTrue(self.srv._sse["running"])
            web._sse_stop(self.srv)
        self.assertTrue(self._drain_watchers())


class TestSseDoorbellWire(unittest.TestCase):
    """Non-vacuous WIRE controls: a real server, a raw socket, actual SSE
    frames — reconnect catch-up, no phantom rings, and PROMPT stream death."""

    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.d = tempfile.mkdtemp()
        os.makedirs(os.path.join(cls.d, "dm"), exist_ok=True)
        cls.env_prior = os.environ.get("HELM_CHAT_DIR")
        os.environ["HELM_CHAT_DIR"] = cls.d
        cls.srv = web.make_server(0)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()               # stops ITS OWN watcher
        cls.thread.join(timeout=5)
        if cls.env_prior is None:
            os.environ.pop("HELM_CHAT_DIR", None)
        else:
            os.environ["HELM_CHAT_DIR"] = cls.env_prior
        shutil.rmtree(cls.d, ignore_errors=True)

    def _stream(self, extra_headers="", read_s=1.5):
        import socket
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        s.settimeout(read_s)
        req = ("GET /api/events HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n%s\r\n"
               % (self.port, extra_headers))
        s.sendall(req.encode())
        buf = b""
        try:
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
        except socket.timeout:
            pass
        finally:
            s.close()
        return buf.decode("utf-8", "replace")

    def test_stale_last_event_id_gets_immediate_catchup_ring(self):
        # reconnect semantics: doorbells are contentless, so ONE immediate
        # ring + the client's cursor read replays any gap
        got = self._stream("Last-Event-ID: 999\r\n")
        self.assertIn(": helm sse doorbell", got)
        self.assertIn("event: chat", got)                    # the catch-up

    def test_fresh_connect_gets_handshake_not_phantom_ring(self):
        got = self._stream()
        self.assertIn(": helm sse doorbell", got)
        self.assertNotIn("event: chat", got)                 # quiet = quiet

    def test_dead_watcher_ends_the_stream_promptly(self):
        # the wedge class: a watcherless server must END its streams (the
        # client's ES dies -> 2s poll fallback) — and PROMPTLY: the stop's
        # notify releases the death-aware wait, no 5s linger
        import socket
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        s.settimeout(8)
        s.sendall(("GET /api/events HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n"
                   % self.port).encode())
        time.sleep(0.3)                       # stream is up
        t0 = time.time()
        web._sse_stop(self.srv)               # kill THIS server's watcher
        ended = False
        try:
            while True:
                if not s.recv(4096):
                    ended = True               # server closed the stream
                    break
        except socket.timeout:
            ended = False
        finally:
            elapsed = time.time() - t0
            s.close()
        self.assertTrue(ended)
        self.assertLess(elapsed, 3.0)          # released, not timed out


if __name__ == "__main__":
    unittest.main()
