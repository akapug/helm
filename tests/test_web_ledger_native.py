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

from helm import web, web_ui_loader  # noqa: E402

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
            {"seat": "agent-deadbeef", "home_room": None,
             "cwd": "/tmp/claude-xyz/scratch"}))

    def test_real_named_seats_are_never_flagged(self):
        # real names, or a real home, or a non-/tmp cwd -> keep in the picker
        for s in (
            {"seat": "design-reviewer", "home_room": None, "cwd": "/tmp/x"},
            {"seat": "codex-2", "home_room": "main", "cwd": "/home/p/helm-wt/x"},
            {"seat": "agent-deadbeef", "home_room": "main", "cwd": "/tmp/x"},
            {"seat": "agent-deadbeef", "home_room": None, "cwd": "/home/p/proj"},
            {"seat": "projx-agent", "home_room": None, "cwd": "/tmp/x"},  # not agent-<hex>
        ):
            self.assertFalse(web._seat_ephemeral(s), s["seat"])

    def test_fail_safe_on_junk(self):
        self.assertFalse(web._seat_ephemeral({}))
        self.assertFalse(web._seat_ephemeral({"seat": None}))


class TestRosterSingleFlightCache(unittest.TestCase):
    """The poll-fan-in guard: roster_report is the one heavy read on the 2s
    poll path; uncached, N concurrent polls stacked N computes and blanked the
    UI blank (thread-pool starvation class). The cache must be SINGLE-FLIGHT
    (concurrent pollers share one compute), TTL-fresh, and publish-ready
    (ephemeral baked in) — decision-spirit #22: memory is the read-path."""

    def setUp(self):
        web._ROSTER_REP_CACHE.clear()
        self.calls = {"n": 0}

    def tearDown(self):
        # the polluter owns cleanup: the module-level cache holds this test's
        # fake rep; clear it so a later test's roster endpoint reads its own
        # planted data, not our leaked {seat: agent-deadbeef} (cross-family review).
        web._ROSTER_REP_CACHE.clear()

    def _fake_report(self, room):
        self.calls["n"] += 1
        return {"seats": [{"seat": "agent-deadbeef", "home_room": None,
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
    """Part of the poll->push read-model: _rooms_summary (~14s at 224
    seats x 13 rooms) ran uncached on every /api/chat poll — N clients
    stacked N computes -> ~60s requests -> thread-pool starvation (the chat
    half of the UI-blank class). Single-flight + TTL, and the cache is
    KEYED BY THE CHAT ROOT so isolated test worlds (fresh tmp roots) can
    never read each other's cached summary — the cross-test-pollution class,
    closed structurally."""

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


class TestRoomsSummaryPerRoomParseMemo(unittest.TestCase):
    """The TTL cache above bounds how OFTEN the summary
    recomputes; the per-room parse memo bounds what a recompute COSTS.
    Parsed (rows, total) is keyed by the room file's (st_mtime_ns, st_size,
    st_ino), so a recompute re-parses ONLY rooms whose file moved, every
    writer invalidates by key (append grows size; rotate/restore swap the
    inode), and any stat surprise falls open to the direct read — a stale
    summary that survives a write would be worse than the perf bug."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self._p = mock.patch("helm.chat.chat_dir", return_value=self.d)
        self._p.start()
        self.addCleanup(self._p.stop)
        self.addCleanup(lambda: shutil.rmtree(self.d, ignore_errors=True))
        for c in (web._ROOMS_SUM_CACHE, web._ROOM_ROWS_MEMO):
            c.clear()
            self.addCleanup(c.clear)
        self.path = os.path.join(self.d, "main.jsonl")

    def _append(self, text):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"from": "x", "text": text,
                                "ts": time.time()}) + "\n")

    def test_unchanged_room_serves_memo_no_reparse(self):
        self._append("hello")
        first = web._rooms_summary(roster={})
        self.assertEqual((first[0]["room"], first[0]["total"]), ("main", 1))
        # the proof is a poisoned parse: if the second summary touches
        # chat.read AT ALL for the unchanged file, the per-room except drops
        # the room and this equality fails loudly — cached result, no re-parse
        with mock.patch("helm.chat.read",
                        side_effect=AssertionError("re-parsed unchanged room")):
            second = web._rooms_summary(roster={})
        self.assertEqual(second, first)

    def test_append_moves_the_key_and_new_content_appears(self):
        self._append("row 0")
        self.assertEqual(web._rooms_summary(roster={})[0]["total"], 1)
        self._append("row 1")            # even same-mtime-tick: size grew
        self.assertEqual(web._rooms_summary(roster={})[0]["total"], 2)

    def test_same_size_replace_still_invalidates(self):
        # the corner (mtime, size) alone is weakest on: same LENGTH, new
        # content, swapped fast — the st_ino leg catches the replace even
        # where an mtime tick would be too coarse
        self._append("AAAA")
        self.assertEqual(web._rooms_summary(roster={})[0]["total"], 1)
        with open(self.path, encoding="utf-8") as f:
            old = f.read()
        new = old.replace("AAAA", "BBBB")
        self.assertEqual(len(new), len(old))      # same st_size by construction
        tmp = self.path + ".swap"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(new)
        os.replace(tmp, self.path)
        rows, total = web._room_rows_memo("main")
        self.assertEqual((total, rows[0]["text"]), (1, "BBBB"))

    def test_stat_failure_falls_open_and_drops_the_entry(self):
        self._append("truth")
        web._rooms_summary(roster={})             # memo populated
        web._ROOM_ROWS_MEMO[self.path] = (
            (0, 0, 0), [{"from": "x", "text": "stale"}], 1)  # poison the memo
        real = os.stat

        def boom(p, *a, **k):
            if str(p) == self.path:
                raise OSError("stat down")
            return real(p, *a, **k)
        with mock.patch("os.stat", side_effect=boom):
            rows, total = web._room_rows_memo("main")
        # uncached truth served, never the poisoned row — and the entry died
        self.assertEqual((total, rows[0]["text"]), (1, "truth"))
        self.assertNotIn(self.path, web._ROOM_ROWS_MEMO)

    def test_raising_read_is_never_cached(self):
        self._append("late truth")
        with mock.patch("helm.chat.read", side_effect=OSError("parse died")):
            self.assertEqual(web._rooms_summary(roster={}), [])  # room skipped
        self.assertNotIn(self.path, web._ROOM_ROWS_MEMO)   # no error cached
        self.assertEqual(web._rooms_summary(roster={})[0]["total"], 1)

    def test_vanished_room_entry_is_evicted(self):
        self._append("here")
        web._rooms_summary(roster={})
        self.assertIn(self.path, web._ROOM_ROWS_MEMO)
        os.unlink(self.path)
        web._rooms_summary(roster={})
        self.assertNotIn(self.path, web._ROOM_ROWS_MEMO)


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

    def test_the_listing_is_not_repeated_while_the_directory_is_unchanged(self):  # noqa: VACUOUS_ASSERTION — the call RESULT is bound, not discarded: assertIsNotNone(fp0) then 20 assertEqual(_chat_fingerprint(), fp0) inside the spy, then an APPEND asserted to CHANGE it. The analyzer cannot credit them because the result is compared to a local rather than a literal; the counted-call assertions are the SECONDARY claim
        """THE COST WAS THE LISTING AND ALMOST NONE OF IT WAS USEFUL. Measured
        on a live chat dir: 8318 entries, 103 of them .jsonl, and
        one fingerprint was 8.35ms of which 5.70ms was this one listdir — four
        times a second, forever. A directory's mtime moves on create/delete and
        NOT on append, so the name list can be cached across ticks and the
        per-file stats still catch every append."""
        web._CHAT_NAMES.clear()
        self._append("main.jsonl")
        real = os.listdir
        calls = []

        def counting(path):
            calls.append(path)
            return real(path)

        with mock.patch("os.listdir", side_effect=counting):
            fp0 = web._chat_fingerprint()
            # MUST-HIT CONTROL: the first pass DOES list, once per base dir.
            # Without this, "no further listings" would also be what a broken
            # probe that never called listdir at all produces.
            self.assertEqual(len(calls), 2, calls)
            # AND THE ANSWER IS STILL AN ANSWER. Counting a spy proves only
            # that the spy ran; a fingerprint that returned None every time
            # would also stop listing and would also pass the count above.
            self.assertIsNotNone(fp0)
            for _ in range(20):
                self.assertEqual(web._chat_fingerprint(), fp0)
            self.assertEqual(len(calls), 2,
                             "20 more ticks re-listed the directory")
            # AN APPEND STILL RINGS WITH THE NAMES CACHED — the cache holds
            # names, never contents, and this is the whole reason that is safe
            self._append("main.jsonl")
            fp1 = web._chat_fingerprint()
            self.assertNotEqual(fp0, fp1)
            self.assertEqual(len(calls), 2, "an APPEND must not re-list")
            # and a CREATE moves the dir, so the very next tick re-lists
            self._append("brand-new-room.jsonl")
            self.assertNotEqual(fp1, web._chat_fingerprint())
            self.assertGreater(len(calls), 2)

    def test_a_new_room_rings_even_though_the_names_were_cached(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control on the same observable runs BEFORE every absence-shaped claim: an append to an already-cached log rings (fp0 != fp_app), so a fingerprint that changed on every call could not satisfy the assertNotEquals that follow
        """The correctness the cache must not cost. A room created after the
        names were cached has to reach the doorbell — otherwise its whole
        conversation is invisible until something else touches the dir."""
        web._CHAT_NAMES.clear()
        self._append("main.jsonl")
        fp0 = web._chat_fingerprint()
        self.assertIsNotNone(fp0)
        self.assertEqual(fp0, web._chat_fingerprint())   # cached and quiet
        # UNCONDITIONAL POSITIVE CONTROL on the same observable, BEFORE the
        # create: an APPEND to an already-cached log rings. Without it, a
        # fingerprint that changed on every call would satisfy every
        # assertNotEqual below while meaning nothing.
        self._append("main.jsonl")
        fp_app = web._chat_fingerprint()
        self.assertNotEqual(fp0, fp_app)
        self._append("a-room-that-did-not-exist.jsonl")
        self.assertNotEqual(fp_app, web._chat_fingerprint())
        # deletion is the other direction of the same signal
        fp1 = web._chat_fingerprint()
        os.unlink(os.path.join(self.d, "a-room-that-did-not-exist.jsonl"))
        self.assertNotEqual(fp1, web._chat_fingerprint())

    def test_the_ledger_poll_stops_when_nobody_is_looking(self):
        """The mandate is "whether or not anyone is looking", and this is
        the poll it was about. `pollLedger` already returned early when the
        ledger TAB was not selected — but a tab being selected says nothing
        about whether the WINDOW is on screen, and an orca pane parked on the
        ledger tab behind another pane is selected and invisible forever.

        MEASURED live: /api/ledger costs 1160ms of server CPU per
        request and /api/ledger/native rides the same tick, on a 3000ms timer,
        from three open sockets. pollDregg, pollRoster and the chat poll all
        already take the document.hidden guard; the console's most expensive
        poll was the one without it.

        Source-level because pollLedger is network-bound and the runtime
        harnesses in this tree extract client functions the same way."""
        from tests.test_web_chat_client_runtime import _extract_fn
        src = web_ui_loader.read_text()
        tick = _extract_fn(src, "pollLedgerTick")
        poll = _extract_fn(src, "pollLedger")
        init = _extract_fn(src, "initLedger")
        # MUST-HIT CONTROLS: both functions were actually found, and the
        # pre-existing tab guard is still there — an empty extraction would
        # otherwise satisfy every assertIn below by vacuity.
        self.assertIn("view-ledger", tick)
        self.assertIn("setInterval(pollLedgerTick, 3000)", init)
        # the BODY keeps the name pollLedger — two live arms in
        # tests/test_web_ledger.py extract it and assert on its contents, and
        # the cadence policy is what got the new name
        self.assertIn("cavePulse", poll)
        # the guard itself
        self.assertIn("document.hidden", tick)
        # and the in-flight latch: a request costing more than a second on a
        # three-second timer STACKS, so a slow ledger makes itself slower
        self.assertIn("LEDGER_POLLING", tick)
        # THE OTHER HALF — without it the guard is a regression, because the
        # first thing the owner sees on returning would be however stale the
        # tab went while hidden
        self.assertIn("visibilitychange", init)
        # pollDregg is the pattern this composes with, not a rival
        self.assertIn("document.hidden", _extract_fn(src, "pollDregg"))

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
        # an in-flight tick on a stopped server acts
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
            with mock.patch.object(web, "_SSE_WATCH_S", 0.01), \
                    mock.patch.object(web, "_SSE_IDLE_WATCH_S", 0.01):
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
        with mock.patch.object(web, "_SSE_WATCH_S", 0.01), \
                mock.patch.object(web, "_SSE_IDLE_WATCH_S", 0.01):
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
        with mock.patch.object(web, "_SSE_WATCH_S", 0.01), \
                mock.patch.object(web, "_SSE_IDLE_WATCH_S", 0.01):
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
        with mock.patch.object(web, "_SSE_WATCH_S", 0.01), \
                mock.patch.object(web, "_SSE_IDLE_WATCH_S", 0.01):
            self.assertTrue(web._sse_ensure_watcher(self.srv))
            self.assertTrue(web._sse_ensure_watcher(other))
            other.server_close()                    # A closes...
            self.assertFalse(other._sse["running"])
            self.assertTrue(self.srv._sse["running"])   # ...B untouched
            other.server_close()                    # double-close: idempotent
            self.assertTrue(self.srv._sse["running"])
            web._sse_stop(self.srv)
        self.assertTrue(self._drain_watchers())

    def test_viewer_connect_wakes_idle_watcher(self):  # noqa: VACUOUS_ASSERTION — the positive arm verifies seq advances with notify; the negative arm is the adversarial control
        # A watcher on the idle cadence must switch to active when a viewer
        # connects. Measured via seq (only advances on fingerprint change).
        with mock.patch.object(web, "_SSE_IDLE_WATCH_S", 0.30), \
                mock.patch.object(web, "_SSE_WATCH_S", 0.02):
            self.assertTrue(web._sse_ensure_watcher(self.srv))  # starts thread
            time.sleep(0.05)
            with self.srv._sse["cond"]:
                self.srv._sse["connected"] = 1  # viewer connects
                self.srv._sse["cond"].notify_all()
                seq_before = self.srv._sse["seq"]
            self._append("main.jsonl")
            time.sleep(0.15)                     # ~7 active ticks
            with self.srv._sse["cond"]:
                seq_after = self.srv._sse["seq"]
            self.assertGreater(seq_after, seq_before)  # ticked on active
            web._sse_stop(self.srv)
        self.assertTrue(self._drain_watchers())
        # NEGATIVE: without notify, same sleep yields no advance
        with mock.patch.object(web, "_SSE_IDLE_WATCH_S", 0.30):
            self.assertTrue(web._sse_ensure_watcher(self.srv))
            time.sleep(0.05)
            with self.srv._sse["cond"]:
                seq_before = self.srv._sse["seq"]
                self.srv._sse["connected"] += 1
                # no notify_all — watcher stays on idle cadence
            self._append("main.jsonl")
            time.sleep(0.15)                     # < one idle cycle
            with self.srv._sse["cond"]:
                seq_after = self.srv._sse["seq"]
            self.assertEqual(seq_before, seq_after)  # noqa: VACUOUS_ASSERTION — the positive arm proves seq advances with notify
            web._sse_stop(self.srv)
        self.assertTrue(self._drain_watchers())

    def test_viewer_disconnect_falls_back_to_idle(self):  # noqa: VACUOUS_ASSERTION — the seq increment on the second append is the positive control on the idle tick
        # After the last viewer leaves, the watcher returns to idle cadence
        # but still ticks on fingerprint changes — it survives.
        with mock.patch.object(web, "_SSE_WATCH_S", 0.02), \
                mock.patch.object(web, "_SSE_IDLE_WATCH_S", 0.15):
            with self.srv._sse["cond"]:
                self.srv._sse["connected"] = 1
            self.assertTrue(web._sse_ensure_watcher(self.srv))  # starts thread
            self._append("main.jsonl")             # drain the first tick
            time.sleep(0.10)
            with self.srv._sse["cond"]:
                self.srv._sse["connected"] = 0
                seq_before = self.srv._sse["seq"]
            self._append("main.jsonl")             # fingerprinted, ticked on idle
            time.sleep(0.25)
            with self.srv._sse["cond"]:
                seq_after = self.srv._sse["seq"]
            self.assertEqual(seq_after, seq_before + 1)  # one tick
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

    def test_connected_read_cannot_lose_real_handler_notify(self):
        # Force the read-before-wait interleaving exactly. get() snapshots
        # connected=0, then gives a real Handler._sse connector a bounded
        # chance to finish. With the read under cond, that connector cannot
        # finish until cond.wait() atomically releases the lock; with the old
        # two-block watcher it finishes and notifies before the wait begins.
        import socket

        read_started = threading.Event()
        request_sent = threading.Event()
        connector_done = threading.Event()
        doorbell = threading.Event()
        release = threading.Event()
        connector_errors = []

        class OwnedCondition(threading.Condition):
            owner = None

            def __enter__(self):
                value = super().__enter__()
                self.owner = threading.get_ident()
                return value

            def __exit__(self, *args):
                try:
                    return super().__exit__(*args)
                finally:
                    self.owner = None

        class ConnectedReadGate(dict):
            completed_during_read = False
            read_under_cond = None
            request_reached_wire = False

            def get(self, key, default=None):
                value = super().get(key, default)
                if key == "connected" and not read_started.is_set():
                    self.read_under_cond = \
                        self["cond"].owner == threading.get_ident()
                    read_started.set()
                    self.request_reached_wire = request_sent.wait(1)
                    if self.request_reached_wire:
                        connector_done.wait(0.20)
                    self.completed_during_read = connector_done.is_set()
                return value

        srv = web.make_server(0)
        state = ConnectedReadGate(web._sse_state())
        state["cond"] = OwnedCondition()
        srv._sse = state
        port = srv.server_address[1]
        server_thread = threading.Thread(target=srv.serve_forever, daemon=True)
        server_thread.start()

        def connect_after_read():
            s = None
            try:
                # The watcher baseline already exists when read_started fires.
                # Change it before Handler._sse notifies, so the wake has an
                # observable seq increment and wire doorbell to deliver.
                with open(os.path.join(self.d, "race.jsonl"), "a") as f:
                    f.write('{"from":"race","text":"wake"}\n')
                s = socket.create_connection(("127.0.0.1", port), timeout=2)
                s.settimeout(0.05)
                s.sendall(("GET /api/events HTTP/1.1\r\n"
                           "Host: 127.0.0.1:%d\r\n\r\n" % port).encode())
                request_sent.set()
                buf = b""
                while not release.is_set():
                    try:
                        chunk = s.recv(4096)
                    except socket.timeout:
                        continue
                    if not chunk:
                        break
                    buf += chunk
                    if b": helm sse doorbell" in buf:
                        connector_done.set()
                    if b"event: chat" in buf:
                        doorbell.set()
                        release.wait(1)
                        break
            except Exception as e:
                connector_errors.append(e)
                connector_done.set()
            finally:
                if s is not None:
                    s.close()

        connector_thread = None
        try:
            with mock.patch.object(web, "_SSE_IDLE_WATCH_S", 2.0), \
                    mock.patch.object(web, "_SSE_WATCH_S", 0.02):
                self.assertTrue(web._sse_ensure_watcher(srv))
                self.assertTrue(read_started.wait(1),
                                "watcher never read connected")
                # Deliberately start only after get('connected') has begun.
                connector_thread = threading.Thread(target=connect_after_read,
                                                    daemon=True)
                connector_thread.start()
                self.assertTrue(connector_done.wait(1), connector_errors)
                self.assertTrue(srv._sse.request_reached_wire,
                                "connector never sent the real request")
                self.assertTrue(
                    doorbell.wait(0.50),
                    "connect notify was lost before watcher wait; "
                    "connector_completed_during_read=%r seq=%r" %
                    (srv._sse.completed_during_read, srv._sse["seq"]))
                with srv._sse["cond"]:
                    self.assertEqual(srv._sse["seq"], 1)
                    self.assertGreater(srv._sse["beat"], 0)
                self.assertTrue(srv._sse.read_under_cond,
                                "connected cadence read escaped cond")
                self.assertFalse(srv._sse.completed_during_read)
                self.assertFalse(connector_errors)
        finally:
            release.set()
            if connector_thread is not None:
                connector_thread.join(timeout=2)
            srv.shutdown()
            srv.server_close()
            server_thread.join(timeout=2)

    def test_real_handler_disconnect_wakes_active_watcher(self):
        # Drive the real Handler._sse finally through a controlled broken
        # wire. The sink does not raise until the watcher has snapshotted
        # connected=1 and is committed to the long active wait. Handler's
        # disconnect notify must wake it to observe connected=0 promptly.
        initial_idle_read = threading.Event()
        event_write_started = threading.Event()
        active_read = threading.Event()
        disconnect_written = threading.Event()
        disconnect_notified = threading.Event()
        idle_read = threading.Event()
        handler_errors = []

        class DisconnectCondition(threading.Condition):
            def notify_all(self):
                if disconnect_written.is_set():
                    disconnect_notified.set()
                return super().notify_all()

        class DisconnectState(dict):
            def get(self, key, default=None):
                value = super().get(key, default)
                if key == "connected":
                    if self["seq"] == 0 and value == 0:
                        initial_idle_read.set()
                    elif self["seq"] > 0:
                        if value > 0:
                            active_read.set()
                        elif active_read.is_set():
                            idle_read.set()
                return value

            def __setitem__(self, key, value):
                old = dict.get(self, key)
                super().__setitem__(key, value)
                if key == "connected" and old and value == 0:
                    disconnect_written.set()

        class BrokenWire:
            def write(self, data):
                if b"event: chat" in data:
                    event_write_started.set()
                    if not active_read.wait(1):
                        handler_errors.append(
                            RuntimeError("watcher never selected active cadence"))
                    raise BrokenPipeError
                return len(data)

            def flush(self):
                pass

        srv = web.make_server(0)
        state = DisconnectState(web._sse_state())
        state["cond"] = DisconnectCondition()
        srv._sse = state
        handler = web.Handler.__new__(web.Handler)
        handler.server = srv
        handler.headers = {}
        handler.wfile = BrokenWire()
        handler.send_response = lambda status: None
        handler.send_header = lambda name, value: None
        handler.end_headers = lambda: None
        handler.close_connection = False

        def serve_stream():
            try:
                handler._sse()
            except Exception as e:
                handler_errors.append(e)

        handler_thread = None
        try:
            with mock.patch.object(web, "_SSE_IDLE_WATCH_S", 2.0), \
                    mock.patch.object(web, "_SSE_WATCH_S", 2.0):
                self.assertTrue(web._sse_ensure_watcher(srv))
                self.assertTrue(initial_idle_read.wait(1),
                                "watcher never selected initial idle cadence")
                # Change the fingerprint before the real handler connects. Its
                # connect notify wakes the watcher and produces the write that
                # BrokenWire turns into a deterministic disconnect.
                with open(os.path.join(self.d, "disconnect.jsonl"), "a") as f:
                    f.write('{"from":"race","text":"disconnect"}\n')
                handler_thread = threading.Thread(target=serve_stream,
                                                  daemon=True)
                handler_thread.start()
                self.assertTrue(event_write_started.wait(1), handler_errors)
                self.assertTrue(active_read.wait(1), handler_errors)
                handler_thread.join(timeout=1)
                self.assertFalse(handler_thread.is_alive())
                self.assertEqual(srv._sse["connected"], 0)
                self.assertTrue(disconnect_written.is_set())
                self.assertTrue(disconnect_notified.wait(0.50),
                                "Handler finally omitted disconnect notify")
                self.assertTrue(
                    idle_read.wait(0.50),
                    "disconnect notify did not wake the watcher from its "
                    "stale connected=1 active wait")
                self.assertFalse(handler_errors)
        finally:
            web._sse_stop(srv)
            srv.server_close()
            if handler_thread is not None:
                handler_thread.join(timeout=2)

    def test_viewer_connect_wakes_watcher_through_real_handler(self):  # noqa: VACUOUS_ASSERTION — the event:chat delivery through the real Handler._sse connect path is the positive control
        # Open a real SSE stream through Handler._sse — the production
        # connected/notify_all path. Stop and re-arm the watcher fresh
        # so its state is deterministic from prior wire tests.
        with mock.patch.object(web, "_SSE_IDLE_WATCH_S", 2.0), \
                mock.patch.object(web, "_SSE_WATCH_S", 0.02):
            import socket

            web._sse_stop(self.srv)              # kill prior watcher
            deadline = time.time() + 3
            while time.time() < deadline and any(
                    t.name == "helm-sse-watcher" and t.is_alive()
                    for t in threading.enumerate()):
                time.sleep(0.02)
            self.assertTrue(web._sse_ensure_watcher(self.srv))  # fresh arm
            time.sleep(0.10)                     # settle: one idle cycle
            # Prove watcher is on idle: beat static over < idle period
            with self.srv._sse["cond"]:
                beat_before = self.srv._sse["beat"]
            time.sleep(0.15)
            with self.srv._sse["cond"]:
                self.assertEqual(self.srv._sse["beat"], beat_before)

            # Connect through real Handler._sse. Read only until the
            # handshake delimiter — NOT a timeout drain (at 1.5s the 0.50s
            # idle cadence passes and the watcher naturally becomes active
            # even with connect notify deleted).
            s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            s.settimeout(0.50)  # < idle cadence — timeout proves stale
            s.sendall(("GET /api/events HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n"
                       % self.port).encode())
            buf = b""
            try:
                while b": helm sse doorbell" not in buf:
                    buf += s.recv(4096)
                    if b"\n\n" in buf:
                        break
            except socket.timeout:
                pass
            self.assertIn(b": helm sse doorbell", buf)
            with self.srv._sse["cond"]:
                self.assertEqual(self.srv._sse["connected"], 1)

            # Watcher was asleep; handler woke it. Fingerprint-append
            # immediately while the idle window (0.50 - ~0.01s so far)
            # is still open — must reach wire within active cadence.
            with open(os.path.join(self.d, "main.jsonl"), "a") as f:
                f.write('{"from":"x","text":"ping"}\n')
            t0 = time.time()
            doorbell = False
            try:
                while time.time() - t0 < 0.30:
                    chunk = s.recv(4096)
                    if b"event: chat" in chunk:
                        doorbell = True
                        break
            except socket.timeout:
                pass
            self.assertTrue(doorbell)
            self.assertLess(time.time() - t0, 0.10)  # active: well under idle
            s.close()
            web._sse_stop(self.srv)



if __name__ == "__main__":
    unittest.main()


class UpstreamStarvationReachesTheRosterTest(unittest.TestCase):
    """proxywatch classified starvation CORRECTLY for days and nobody could see
    it. Measured against raw proxy log tails: ds4pro RATE-LIMITED,
    grok AUTH-UNAVAILABLE, gemini MALFORMED200, codex/kimi HEALTHY — every one
    accurate, and every seat row the console renders carried state=None.

    The cost of that gap is a wrong read, not a missing nicety: a rate-limited
    seat is alive and TRYING and being refused, and on the roster it looks
    exactly like an idle one. The owner cannot act on a difference he cannot
    see.

    These tests pin the JOIN and the three ways it must refuse to guess."""

    def setUp(self):
        self._saved = dict(web._ROSTER_REP_CACHE)
        web._ROSTER_REP_CACHE.clear()

    def tearDown(self):
        web._ROSTER_REP_CACHE.clear()
        web._ROSTER_REP_CACHE.update(self._saved)

    def _row(self, seat, upstream, age=60, err=None, family="ds4pro",
             runtime=None):
        row = {"seat": seat}
        if runtime is not None:
            row["runtime"] = runtime
            row["runtime_verified"] = True
        from helm import seat as seatmod
        with mock.patch.object(seatmod, "_seat_family",
                               lambda n: (family, None) if family else
                                         (None, "unknown seat")):
            web._annotate_upstream(row, upstream, age, err)
        return row

    def test_a_starved_family_reaches_the_row(self):
        """THE BUG: the classifier knew and the row said None."""
        row = self._row("ds4pro", {"ds4pro": {"state": "RATE-LIMITED",
                                              "since": "2026-07-31T16:26:13Z"}})
        self.assertEqual(row["upstream"], "RATE-LIMITED")
        self.assertEqual(row["upstream_since"], "2026-07-31T16:26:13Z")
        self.assertEqual(row["upstream_family"], "ds4pro")
        self.assertTrue(row["beacon_paused"])

    def test_runtime_family_not_display_name_drives_the_pause_badge(self):
        row = self._row(
            "pi-codex", {"codex": {"state": "RATE-LIMITED", "dark": True}},
            family=None, runtime={"family": "codex", "agent_harness": "pi"})
        self.assertEqual(row["upstream_family"], "codex")
        self.assertTrue(row["beacon_paused"])

    def test_a_seat_with_NO_proxy_family_carries_no_badge_at_all(self):
        """Not UNKNOWN — NOTHING. A claude-direct seat has no upstream that
        could starve it, so a badge there is ink on every row that can never
        carry the condition. Absence is the correct answer, and it is a
        different answer from 'I could not tell'."""
        row = self._row("helm-claude-2", {"ds4pro": {"state": "RATE-LIMITED"}},
                        family=None)
        self.assertEqual(row, {"seat": "helm-claude-2"})

    def test_an_UNREADABLE_record_is_UNKNOWN_and_never_healthy(self):
        """The could-not-look-recorded-as-a-fact class, on the one surface
        where silence reads as health. If proxywatch's record is unreadable
        the honest answer is UNKNOWN with the reason attached."""
        row = self._row("ds4pro", {}, err="proxywatch state unreadable — OSError")
        self.assertEqual(row["upstream"], "UNKNOWN")
        self.assertIn("unreadable", row["upstream_why"])

    def test_structural_corruption_is_unknown_paused_not_a_500(self):
        row = self._row("ds4pro", {}, err="proxywatch upstream state is not an object")
        self.assertEqual(row["upstream"], "UNKNOWN")
        self.assertTrue(row["beacon_paused"])
        self.assertIn("not an object", row["upstream_why"])

    def test_dark_to_unknown_has_one_visible_state_on_both_surfaces(self):
        row = self._row("ds4pro", {"ds4pro": {
            "state": "UNKNOWN", "dark": True,
            "last_dark_state": "AUTH-401", "since": "episode"}})
        self.assertEqual(row["upstream"], "UNKNOWN")
        self.assertTrue(row["beacon_paused"])
        self.assertEqual(row["upstream_last_dark"], "AUTH-401")

    def test_a_STALE_record_stops_asserting_and_keeps_what_it_last_saw(self):
        """A record older than the staleness bound describes THEN, not NOW.
        It degrades to UNKNOWN — but the last observation is preserved under
        its own key, because 'it was RATE-LIMITED 50 minutes ago and the
        watcher has gone quiet' is more useful than either half alone."""
        row = self._row("ds4pro", {"ds4pro": {"state": "RATE-LIMITED"}},
                        age=web._UPSTREAM_STALE_S + 60)
        self.assertEqual(row["upstream"], "UNKNOWN")
        self.assertEqual(row["upstream_stale_state"], "RATE-LIMITED")
        self.assertTrue(row["beacon_paused"],
                        "stale observation is not a measured recovery")
        self.assertIn("not the state now", row["upstream_why"])

    def test_the_CONTROL_a_fresh_record_inside_the_bound_is_NOT_stale(self):
        """The half that must not change: the staleness rule must not fire in
        normal operation. It was set to 20 minutes first and every badge on
        the fleet read UNKNOWN, because the installed timer is
        OnUnitActiveSec=900s and 15-minute cycles cross 20 routinely. A
        staleness rule that always fires does not report staleness, it deletes
        the feature."""
        row = self._row("ds4pro", {"ds4pro": {"state": "RATE-LIMITED"}},
                        age=web._UPSTREAM_STALE_S - 60)
        self.assertEqual(row["upstream"], "RATE-LIMITED")
        self.assertNotIn("upstream_stale_state", row)

    def test_measured_healthy_clears_the_owner_facing_pause(self):
        row = self._row("ds4pro", {"ds4pro": {
            "state": "HEALTHY", "dark": False}},
            age=web._UPSTREAM_STALE_S - 60)
        self.assertEqual(row["upstream"], "HEALTHY")
        self.assertFalse(row["beacon_paused"])

    def test_roster_badge_names_retention_and_automatic_resume(self):
        source = web_ui_loader.read_text()
        self.assertIn('"PAUSED \\u00b7 " + (u || "UNKNOWN")', source)
        self.assertIn("messages stay pending", source)
        self.assertIn("HEALTHY result resumes delivery automatically", source)

    def test_the_join_is_WIRED_into_the_cached_roster_not_merely_defined(self):
        """A helper nobody calls is exactly as useful as no helper. Every other
        test here calls _annotate_upstream directly, so deleting its CALL SITE
        in _roster_cached would leave all of them green and the console blank —
        which is the built-not-wired shape this repo has a guard for."""
        from helm import seat as seatmod, proxywatch
        fake = {"seats": [{"seat": "ds4pro"}, {"seat": "helm-claude-2"}]}
        with mock.patch.object(web, "_ROSTER_REP_TTL", 0), \
             mock.patch("helm.seats.roster_report", lambda room: fake), \
             mock.patch.object(seatmod, "_seat_family",
                               lambda n: ("ds4pro", None) if n == "ds4pro"
                                         else (None, "unknown seat")), \
             mock.patch.object(proxywatch, "_read_delivery_state",
                               lambda: ({"upstream": {"ds4pro": {
                                   "state": "RATE-LIMITED",
                                   "since": "2026-07-31T16:26:13Z"}}}, None)):
            rep = web._roster_cached("main")
        rows = {r["seat"]: r for r in rep["seats"]}
        self.assertEqual(rows["ds4pro"].get("upstream"), "RATE-LIMITED")
        self.assertTrue(rows["ds4pro"].get("beacon_paused"))
        self.assertIsNone(rows["helm-claude-2"].get("upstream"))

    # ── the wall verdict and the reset horizon are the RECORD's ──────
    # The owner learned about a 429 daily-quota wall from pane errors,
    # because the roster row carried the state name but not the record's own
    # wall verdict — and a badge that cannot tell a wall from any other
    # non-healthy blip cannot say "this seat is refused, not idle" (a wall
    # is a fact, not unreadiness). Both fields are PASS-THROUGH from
    # proxywatch's persisted sweep: one sheet decides what is a wall, and
    # the roster never grows a second opinion.

    def test_a_walled_family_carries_the_records_own_dark_verdict(self):
        """`dark` was composed by proxywatch at write time (_UPSTREAM_DARK);
        the row carries THAT bit, not a re-derivation from the state name."""
        row = self._row("gemini", {"gemini": {"state": "RATE-LIMITED",
                                              "since": "2026-08-05T00:56:43Z",
                                              "dark": True}}, family="gemini")
        self.assertIs(row["upstream_dark"], True)
        self.assertEqual(row["upstream_since"], "2026-08-05T00:56:43Z")

    def test_a_recorded_reset_horizon_reaches_the_row_as_epoch_ms(self):
        """resets_at_ms is the repo-wide reset-horizon vocabulary (providers,
        creds, the quota tab). Nothing writes it into the family record yet —
        a planned standing-config will — so this pins the SEAM: the day
        the record carries a number, the roster row carries it too."""
        row = self._row("kimi", {"kimi": {"state": "AUTH-UNAVAILABLE",
                                          "since": "2026-08-04T23:11:06Z",
                                          "dark": True,
                                          "resets_at_ms": 1785912345000}},
                        family="kimi")
        self.assertEqual(row["upstream_resets_at_ms"], 1785912345000)

    def test_an_absent_reset_horizon_stays_absent_not_null(self):
        """Today's records carry no horizon, and the row must say so by NOT
        carrying the key — a null would render as an empty claim, and the
        badge's honest form for this case is 'no recorded reset horizon'."""
        row = self._row("gemini", {"gemini": {"state": "RATE-LIMITED",
                                              "dark": True}}, family="gemini")
        self.assertEqual(row["upstream"], "RATE-LIMITED")   # the join RAN —
        self.assertIs(row["upstream_dark"], True)           # absence below is
        self.assertNotIn("upstream_resets_at_ms", row)      # a verdict, not a no-op

    def _garbage_row(self, garbage):
        return self._row("gemini", {"gemini": {"state": "RATE-LIMITED",
                                               "dark": True,
                                               "resets_at_ms": garbage}},
                         family="gemini")

    def test_a_garbage_reset_horizon_is_dropped_not_rendered(self):
        """A record is at-least-once persisted state, not a trusted schema:
        a string or a bool where epoch-ms belongs must vanish, not reach the
        badge as NaN arithmetic. Unrolled (not a loop) so each absence sits
        beside an unconditional proof the join ran on that same row."""
        row = self._garbage_row("soon")
        self.assertEqual(row["upstream"], "RATE-LIMITED")
        self.assertNotIn("upstream_resets_at_ms", row)
        row = self._garbage_row(True)
        self.assertEqual(row["upstream"], "RATE-LIMITED")
        self.assertNotIn("upstream_resets_at_ms", row)
        row = self._garbage_row(None)
        self.assertEqual(row["upstream"], "RATE-LIMITED")
        self.assertNotIn("upstream_resets_at_ms", row)
        row = self._garbage_row([1785912345000])
        self.assertEqual(row["upstream"], "RATE-LIMITED")
        self.assertNotIn("upstream_resets_at_ms", row)

    def test_a_non_finite_horizon_is_dropped_and_the_seat_keeps_its_wall(self):
        """A cross-family review's blocker, parse half. float("nan") passes an
        isinstance number check and int() then RAISES — and json persists NaN
        happily (allow_nan default), so ONE malformed field in the record
        killed the whole /api/chat/roster payload. The finite gate drops the
        horizon at the parse boundary; the seat's WALL FIELDS must survive
        untouched, because losing the badge to a corrupt horizon would re-open
        exactly the silence the wall-verdict pass-through closed."""
        row = self._garbage_row(float("nan"))
        self.assertEqual(row["upstream"], "RATE-LIMITED")
        self.assertIs(row["upstream_dark"], True)
        self.assertNotIn("upstream_resets_at_ms", row)
        row = self._garbage_row(float("inf"))
        self.assertEqual(row["upstream"], "RATE-LIMITED")
        self.assertIs(row["upstream_dark"], True)
        self.assertNotIn("upstream_resets_at_ms", row)
        row = self._garbage_row(float("-inf"))
        self.assertEqual(row["upstream"], "RATE-LIMITED")
        self.assertIs(row["upstream_dark"], True)
        self.assertNotIn("upstream_resets_at_ms", row)

    def test_a_corrupt_dark_value_maps_to_None_never_True(self):
        """A cross-family review's blocker, server half. bool("false") is True
        and bool(1) is True — a corrupt persisted verdict must not mint an
        asserted wall. Only EXACT booleans pass through; anything else becomes
        None ("verdict unreadable"), which the client styles calm, never red.
        The key stays PRESENT so a current server is distinguishable from the
        old one whose absence means walled-fallback."""
        row = self._row("gemini", {"gemini": {"state": "RATE-LIMITED",
                                              "dark": "false"}},
                        family="gemini")
        self.assertEqual(row["upstream"], "RATE-LIMITED")   # the join RAN
        self.assertIn("upstream_dark", row)
        self.assertIsNone(row["upstream_dark"])
        row = self._row("gemini", {"gemini": {"state": "RATE-LIMITED",
                                              "dark": 1}}, family="gemini")
        self.assertEqual(row["upstream"], "RATE-LIMITED")
        self.assertIn("upstream_dark", row)
        self.assertIsNone(row["upstream_dark"])

    def test_a_poisoned_record_degrades_one_seat_never_the_payload(self):
        """A cross-family review's blocker, endpoint half. The general law
        behind the finite gate: NO failure inside the upstream join may ride
        up to _api_chat_roster's catch-all and blank every seat. A join that
        raises degrades THAT seat's field to an honest UNKNOWN-with-reason;
        the sibling seat is annotated normally and the payload survives."""
        from helm import seat as seatmod, proxywatch
        fake = {"seats": [{"seat": "gemini"}, {"seat": "ds4pro"}]}
        real = web._annotate_upstream

        def poisoned(row, upstream, age, err):
            if row.get("seat") == "gemini":
                raise ValueError("cannot convert float NaN to integer")
            real(row, upstream, age, err)

        with mock.patch.object(web, "_ROSTER_REP_TTL", 0), \
             mock.patch("helm.seats.roster_report", lambda room: fake), \
             mock.patch.object(web, "_annotate_upstream", poisoned), \
             mock.patch.object(seatmod, "_seat_family",
                               lambda n: (n, None)), \
             mock.patch.object(proxywatch, "_read_delivery_state",
                               lambda: ({"upstream": {"ds4pro": {
                                   "state": "RATE-LIMITED",
                                   "since": "2026-08-05T00:56:43Z",
                                   "dark": True}}}, None)):
            rep = web._roster_cached("main")
        rows = {r["seat"]: r for r in rep["seats"]}
        self.assertEqual(len(rows), 2)                      # payload SURVIVED
        self.assertEqual(rows["ds4pro"]["upstream"], "RATE-LIMITED")
        self.assertEqual(rows["gemini"]["upstream"], "UNKNOWN")
        self.assertIn("join failed", rows["gemini"]["upstream_why"])
        self.assertIn("ValueError", rows["gemini"]["upstream_why"])

    def test_a_stale_record_asserts_neither_wall_nor_horizon(self):
        """A watcher that stopped writing forfeits BOTH new claims, exactly as
        it forfeits the state: 'it was walled and resets at T' from a dead
        recorder is the could-not-look-read-as-fact bug wearing two new keys.
        The last-seen state stays preserved under its own stale key."""
        row = self._row("gemini", {"gemini": {"state": "RATE-LIMITED",
                                              "dark": True,
                                              "resets_at_ms": 1785912345000}},
                        age=web._UPSTREAM_STALE_S + 60, family="gemini")
        self.assertEqual(row["upstream"], "UNKNOWN")
        self.assertEqual(row["upstream_stale_state"], "RATE-LIMITED")
        self.assertNotIn("upstream_dark", row)
        self.assertNotIn("upstream_resets_at_ms", row)

    def test_a_healthy_family_carries_an_explicit_false_not_an_absence(self):
        """The client's half-live tolerance reads a MISSING upstream_dark as
        'old server, every recorded non-healthy state is a named cause' — so
        a CURRENT server must write False explicitly on fresh rows, or a
        healthy-but-blipped state would inherit wall language it never
        earned."""
        row = self._row("codex", {"codex": {"state": "HEALTHY",
                                            "dark": False}}, family="codex")
        self.assertEqual(row["upstream"], "HEALTHY")        # the join RAN
        self.assertIn("upstream_dark", row)                 # the key EXISTS
        self.assertIs(row["upstream_dark"], False)          # and is exact False
