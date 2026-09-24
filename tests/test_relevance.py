"""The long-tail re-rank (helm/relevance.py): every rule armed both ways.

RESIDENCY FAILS CLOSED: a turn reaches the outside evaluator only when its
project's AUTHORED residency is `may-leave-lan`; no field, an unknown project,
no project, a malformed value, a projection-only block and an unreadable
registry all score locally, and the evaluator is never called.
THE WAIT IS BOUNDED, THE SCORE IS NOT: the hook stops waiting at the bound, the
worker still lands its score, and a later read that turn gets it.
FAIL OPEN SAYS SO: a fallen-back live turn carries MARK and the outcome is
counted per tier; shadow and off change no delivered byte.
THE LATER READ (turn_scores) calls no model: it reads one file.

Every turn text here is synthetic.
"""
import contextlib
import http.server
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-home-", var="HELM_HOME")

from helm import home, inject, pk, registry, relevance, relevance_cli, store  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_ADOPTED_DIR", "MELD_ADOPTED_DIR",
            "HELM_CACHE_DIR", "MELD_CACHE_DIR", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CF_ENDPOINT", "MELD_CF_ENDPOINT", "HELM_CHAT_NAME", "MELD_CHAT_NAME",
            "HELM_SEAT_NAMES", "MELD_SEAT_NAMES", "CLAUDE_CODE_SESSION_ID",
            "HELM_JEV_CRED_FILE", "MELD_JEV_CRED_FILE", "AI_GATEWAY_API_KEY",
            "HELM_AGENT_HARNESS")

TEXT = "the zebra quokka migration needs a rollback plan before the deploy"


class Recorder:
    """A scorer double that records every call and answers fixed scores."""

    def __init__(self, scores=None, raises=None, model="double", threshold=0.5):
        self.calls, self.scores, self.raises = [], scores, raises
        self.model, self.threshold = model, threshold

    def jev(self, text, cands, cfg):
        self.calls.append((text, [c["id"] for c in cands]))
        if self.raises:
            raise self.raises
        return {c["id"]: (self.scores or {}).get(c["id"], 0.9) for c in cands}, self.model

    def local(self, text, cands, cfg):
        self.calls.append((text, [c["id"] for c in cands]))
        if self.raises:
            raise self.raises
        return ({c["id"]: (self.scores or {}).get(c["id"], 0.9) for c in cands},
                self.model, self.threshold)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-relevance-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.environ["HELM_CACHE_DIR"] = os.path.join(self.tmp, "cache")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_SEAT_NAMES"] = os.path.join(self.tmp, "seat-names.txt")
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])
        os.makedirs(os.path.join(home.global_dir(), ".state"), exist_ok=True)

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def settings(self, **kw):
        body = dict({"mode": "shadow"}, **kw)
        pk.write_json(os.path.join(home.global_dir(), relevance.CONFIG), body)
        return relevance.settings()

    def job(self, project=None, session="s1", text=TEXT, t0=None, wait_s=1.0, ids=("prior:a", "prior:b")):
        t0 = time.time() if t0 is None else t0
        return {"v": relevance.JOB_VERSION, "turn": relevance.turn_id(session, text, t0),
                "session": session, "project": project, "t0": t0, "mode": "shadow",
                "wait_s": wait_s, "text": text, "keyword": list(ids[:1]),
                "candidates": [{"id": i, "note": "note for %s" % i} for i in ids]}

    def plant_projects(self, authored, inline=None):
        """Registered projects under tmp; `authored`/`inline` map name ->
        residency block for the authored layer / the projection."""
        projects, entries = {}, {}
        for name in sorted(set(authored) | set(inline or {}) | {"alpha"}):
            path = os.path.join(self.tmp, "dev", name)
            os.makedirs(path, exist_ok=True)
            projects[name] = {"name": name, "path": path, "kind": "git",
                              "status": "active", "sessions": {}}
            if name in (inline or {}):
                projects[name]["residency"] = inline[name]
            entries[name] = {"path": path}
            if name in authored:
                entries[name]["residency"] = authored[name]
        pk.write_json(home.registry_path(), {"version": 1, "projects": projects})
        pk.write_json(home.authored_path(), {"version": 1, "projects": entries})


OPEN = {"value": "may-leave-lan", "reason": "non-client", "by": "owner", "ts": 1}


class ResidencyFailsClosedTest(Base):
    def test_only_an_authored_may_leave_lan_reaches_the_evaluator(self):
        self.plant_projects(
            {"omega": OPEN, "beta": dict(OPEN, value="lan-only"),
             "gamma": dict(OPEN, value="anywhere"), "zeta": "may-leave-lan"},
            inline={"delta": OPEN})
        cfg = self.settings()
        # THE PREMISE of the laundering case: the projection really carries it.
        self.assertEqual(registry.load()["projects"]["delta"]["residency"], OPEN)
        for project in (None, "ghost", "alpha", "beta", "gamma", "zeta", "delta"):
            with self.subTest(project=project):
                jev, local = Recorder(), Recorder()
                row = relevance.score_turn(self.job(project), cfg, jev.jev, local.local)
                self.assertEqual(len(jev.calls), 0, "the evaluator was called for %r" % project)
                self.assertEqual((row["tier"], row["source"], len(local.calls)),
                                 ("local", "local", 1))
                self.assertIn("lan-only", registry.residency(project)["value"])
        # THE CONTROL: the one open value does reach it, with the turn's text.
        jev, local = Recorder(), Recorder()
        row = relevance.score_turn(self.job("omega"), cfg, jev.jev, local.local)
        self.assertEqual((row["tier"], row["source"]), ("jev", "jev"))
        self.assertEqual(jev.calls[0][0], TEXT)
        self.assertEqual(len(local.calls), 1, "the local head scores beside the teacher")
        self.assertEqual(sorted(row["teach"]["scores"]), ["prior:a", "prior:b"])

    def test_an_unreadable_registry_is_lan_only(self):
        self.plant_projects({"omega": OPEN})
        cfg = self.settings()
        jev = Recorder()
        relevance.score_turn(self.job("omega"), cfg, jev.jev, Recorder().local)
        self.assertEqual(len(jev.calls), 1)                      # the control
        with open(home.authored_path(), "w") as f:
            f.write("{not json")
        jev = Recorder()
        row = relevance.score_turn(self.job("omega"), cfg, jev.jev, Recorder().local)
        self.assertEqual((len(jev.calls), row["tier"]), (0, "local"))
        self.assertIn("registry unreadable", row["why"])

    def test_a_residency_reader_that_raises_is_lan_only(self):  # noqa: VACUOUS_ASSERTION — the zero-call assertion and the one-call control read the same Recorder.calls list, unconditionally, in that order
        cfg = self.settings()
        jev = Recorder()

        def boom(project):
            raise OSError("disk")
        row = relevance.score_turn(self.job("omega"), cfg, jev.jev, Recorder().local, residency=boom)
        self.assertEqual((len(jev.calls), row["tier"], row["source"]), (0, "local", "local"))
        row = relevance.score_turn(self.job("omega"), cfg, jev.jev, Recorder().local,
                                   residency=lambda p: {"value": "may-leave-lan", "why": "t"})
        self.assertEqual((len(jev.calls), row["source"]), (1, "jev"))  # the control

    def test_a_scrub_hit_is_never_sent_even_where_residency_allows(self):  # noqa: VACUOUS_ASSERTION — the unconditional control after the loop asserts the same Recorder.calls reaches 1 on clean text
        cfg = self.settings()
        allow = lambda p: {"value": "may-leave-lan", "why": "authored"}
        for text in ("deploy with sk-abcdefgh12345678 now", "ping 10.1.2.3 first",
                     "mail someone@registrable-name.org"):
            with self.subTest(text=text):
                jev = Recorder()
                row = relevance.score_turn(self.job(text=text), cfg, jev.jev,
                                           Recorder().local, residency=allow)
                self.assertEqual((len(jev.calls), row["source"]), (0, "local"))
                self.assertIn("not sent", row["errors"]["jev"])
        jev = Recorder()
        row = relevance.score_turn(self.job(), cfg, jev.jev, Recorder().local, residency=allow)
        self.assertEqual((len(jev.calls), row["source"]), (1, "jev"))  # the control

    def test_an_evaluator_failure_falls_back_to_the_local_head(self):
        cfg = self.settings()
        allow = lambda p: {"value": "may-leave-lan", "why": "authored"}
        local = Recorder(threshold=0.4)
        row = relevance.score_turn(self.job(), cfg, Recorder(raises=RuntimeError("HTTP 429")).jev,
                                   local.local, residency=allow)
        self.assertEqual((row["tier"], row["source"], row["threshold"]), ("jev", "local", 0.4))
        self.assertIn("HTTP 429", row["errors"]["jev"])
        self.assertNotIn("teach", row)


class WaitBoundTest(Base):
    """The hook's wait ends at the bound; the score does not."""

    def clock(self, start=1000.0):
        now = [start]
        sleeps = []

        def sleep(s):
            sleeps.append(s)
            now[0] += s
        return now, (lambda: now[0]), sleep, sleeps

    def plant_entries(self, n=3):
        return [{"id": "e%d" % i, "type": "prior", "confidence": 0.8,
                 "statement": "Entry %d says to check the rollback plan first." % i}
                for i in range(n)]

    def test_a_late_score_is_cut_at_the_bound_and_still_reaches_later_reads(self):
        cfg = self.settings(mode="live", wait_s=0.5)
        now, clock, sleep, sleeps = self.clock()
        jobs = []
        out = relevance.at_prompt(TEXT, self.plant_entries(), session="s1", cfg=cfg,
                                  spawn=lambda job: (jobs.append(job) or (True, None)),
                                  clock=clock, sleep=sleep)
        self.assertEqual(out["state"], "fallback")
        self.assertAlmostEqual(out["waited_ms"], 500.0, delta=relevance.POLL_S * 1000 + 1)
        self.assertIsNone(relevance.turn_scores("s1"))
        # THE WORKER FINISHES LATE AND STILL LANDS: every later read that turn
        # gets the real score, and the outcome is counted as late.
        now[0] += 2.0
        row = relevance.score_turn(jobs[0], cfg, Recorder().jev, Recorder(threshold=0.3).local,
                                   clock=clock)
        self.assertEqual(row["outcome"], "late")
        got = relevance.turn_scores("s1")
        self.assertEqual((got["turn"], got["source"], got["threshold"]),
                         (out["turn"], "local", 0.3))
        self.assertEqual(sorted(got["scores"]), sorted(c["id"] for c in jobs[0]["candidates"]))
        self.assertEqual(relevance.counters()["tiers"]["local"],
                         {"in_time": 0, "late": 1, "failed": 0})

    def test_a_score_inside_the_bound_is_used_and_counted_in_time(self):
        cfg = self.settings(mode="live", wait_s=0.5)
        _now, clock, sleep, _sleeps = self.clock()

        def spawn(job):
            relevance.score_turn(job, cfg, Recorder().jev, Recorder(threshold=0.3).local, clock=clock)
            return True, None
        out = relevance.at_prompt(TEXT, self.plant_entries(), session="s1", cfg=cfg,
                                  spawn=spawn, clock=clock, sleep=sleep)
        self.assertEqual((out["state"], out["source"], out["threshold"]), ("scored", "local", 0.3))
        self.assertEqual(len(out["scores"]), 3)
        self.assertEqual(relevance.counters()["tiers"]["local"]["in_time"], 1)

    def test_shadow_starts_the_worker_and_never_waits(self):
        _now, clock, sleep, sleeps = self.clock()
        jobs = []
        spawn = lambda job: (jobs.append(job) or (True, None))
        out = relevance.at_prompt(TEXT, self.plant_entries(), session="s1",
                                  cfg=self.settings(mode="shadow"), spawn=spawn,
                                  clock=clock, sleep=sleep)
        self.assertEqual((out["state"], len(jobs), sleeps), ("pending", 1, []))
        # THE CONTROL: the same call in live mode does wait.
        relevance.at_prompt(TEXT, self.plant_entries(), session="s2",
                            cfg=self.settings(mode="live", wait_s=0.2), spawn=spawn,
                            clock=clock, sleep=sleep)
        self.assertTrue(sleeps)

    def test_a_failed_worker_ends_the_wait_before_the_bound(self):
        cfg = self.settings(mode="live", wait_s=5.0)
        _now, clock, sleep, sleeps = self.clock()

        def spawn(job):
            relevance.score_turn(job, cfg, Recorder().jev,
                                 Recorder(raises=RuntimeError("down")).local, clock=clock)
            return True, None
        out = relevance.at_prompt(TEXT, self.plant_entries(), session="s1", cfg=cfg,
                                  spawn=spawn, clock=clock, sleep=sleep)
        self.assertEqual(out["state"], "fallback")
        self.assertLess(out["waited_ms"], 100)
        self.assertEqual(relevance.counters()["tiers"]["local"]["failed"], 1)

    def test_the_job_carries_the_rendered_candidates_and_no_file_outlives_the_spawn(self):
        jobs = []
        relevance.at_prompt(TEXT, self.plant_entries(2), project="p", session="s1",
                            keyword_ids=["prior:e0"], cfg=self.settings(),
                            spawn=lambda job: (jobs.append(job) or (True, None)))
        job = jobs[0]
        self.assertEqual([c["id"] for c in job["candidates"]], ["prior:e0", "prior:e1"])
        self.assertEqual(job["candidates"][0]["note"],
                         "e0: Entry 0 says to check the rollback plan first.")
        self.assertEqual((job["project"], job["keyword"], job["text"]), ("p", ["prior:e0"], TEXT))


class DeliveryTest(Base):
    """Through gather(): off and shadow change no delivered byte; live either
    re-ranks or says it fell back."""

    def setUp(self):
        super().setUp()
        store.write_prior({"id": "zebra-rule", "statement": "Zebra rule: check the rollback plan.",
                           "confidence": "0.8", "keywords": "zebra"})
        store.write_prior({"id": "quokka-rule", "statement": "Quokka rule: stage the migration.",
                           "confidence": "0.8", "keywords": "quokka"})

    def gather(self, session="s1"):
        return inject.gather(TEXT, session=session)

    def ledger(self):
        return relevance._read_ledger()

    def test_off_spawns_nothing_and_writes_nothing(self):  # noqa: VACUOUS_ASSERTION — the shadow control at the end asserts the same spawned list and ledger become non-empty; noqa: ORPHANED_MOCK — the double is reached through inject.gather -> _whisper._rerank -> relevance.at_prompt/score_turn, a cross-module path the walker does not follow
        spawned = []
        with mock.patch.object(relevance, "_spawn", lambda job: (spawned.append(job) or (True, None))):
            out = self.gather()
        self.assertTrue(any("zebra-rule" in l for l in out["jit"]))   # candidates existed
        self.assertEqual((spawned, self.ledger()), ([], []))
        self.assertFalse(os.path.exists(relevance.cache_path("s1")))
        # THE CONTROL on the same observables: shadow does spawn and write.
        self.settings(mode="shadow")
        with mock.patch.object(relevance, "_spawn", lambda job: (spawned.append(job) or (True, None))):
            self.gather("s2")
        self.assertEqual((len(spawned), [r["ev"] for r in self.ledger()]), (1, ["submit"]))
        self.assertTrue(os.path.exists(relevance.cache_path("s2")))

    def test_shadow_changes_no_delivered_byte(self):  # noqa: VACUOUS_ASSERTION — the spawned list is asserted to hold one job and the off lanes to be non-empty before the equality, and the ledger rows are read positively; noqa: ORPHANED_MOCK — the double is reached through inject.gather -> _whisper._rerank -> relevance.at_prompt/score_turn, a cross-module path the walker does not follow
        off = self.gather("s-off")
        self.settings(mode="shadow")
        spawned = []
        with mock.patch.object(relevance, "_spawn", lambda job: (spawned.append(job) or (True, None))):
            shadow = self.gather("s-shadow")
        self.assertEqual(len(spawned), 1)                       # the control: it ran
        for lane in ("pinned", "jit", "reflex"):
            self.assertEqual(shadow[lane], off[lane], lane)
        self.assertTrue(off["jit"])
        self.assertEqual([r["ev"] for r in self.ledger()], ["submit"])
        rows = inject._ledger_rows()
        self.assertEqual(rows[-1]["relevance"]["turn"], spawned[0]["turn"])
        self.assertNotIn("relevance", rows[0])

    def test_a_fallen_back_live_turn_says_so_and_is_counted(self):  # noqa: ORPHANED_MOCK — the double is reached through inject.gather -> _whisper._rerank -> relevance.at_prompt/score_turn, a cross-module path the walker does not follow
        self.settings(mode="live", wait_s=0.05)
        jobs = []
        with mock.patch.object(relevance, "_spawn", lambda job: (jobs.append(job) or (True, None))):
            out = self.gather()
        self.assertEqual(out["jit"][0], relevance.MARK)
        self.assertTrue(any("zebra-rule" in l for l in out["jit"][1:]))
        self.assertEqual(inject._ledger_rows()[-1]["relevance"]["state"], "fallback")
        with mock.patch.object(relevance, "local_scores", Recorder().local):
            relevance.score_turn(jobs[0])
        self.assertEqual(relevance.counters()["tiers"]["local"]["late"], 1)
        rep = relevance.report()
        self.assertEqual((rep["tiers"]["local"]["fallback_rate"], rep["submitted"]), (1.0, 1))
        sub = next(r for r in self.ledger() if r["ev"] == "submit")
        self.assertEqual((sub["state"], rep["live_fallback"], rep["hook_ms"]["n"]), ("fallback", 1, 1))
        self.assertGreaterEqual(sub["hook_ms"], 50.0)          # it waited the 0.05 s bound

    def test_the_fallback_mark_survives_the_arrival_cap(self):  # noqa: ORPHANED_MOCK — the double is reached through inject.gather -> _whisper._rerank -> relevance.at_prompt, a cross-module path the walker does not follow
        """The arrival cap re-renders the lane from its entries when it
        squeezes, so the mark goes on after it, or a squeezed turn would fall
        back in silence."""
        from helm import moments
        self.settings(mode="live", wait_s=0.05)
        tight = dict(moments.POLICY)
        tight[moments.TYPED] = moments.POLICY[moments.TYPED]._replace(cap=60)
        with mock.patch.object(relevance, "_spawn", lambda job: (True, None)), \
                mock.patch.object(moments, "POLICY", tight):
            out = self.gather()
        row = inject._ledger_rows()[-1]
        self.assertTrue(row.get("over_cap"), "the cap did not bind; the arm is about nothing")
        self.assertEqual(out["jit"][0], relevance.MARK)
        self.assertEqual(row["relevance"]["state"], "fallback")

    def test_a_scored_live_turn_carries_no_mark_and_delivers_by_score(self):  # noqa: VACUOUS_ASSERTION — the quokka line is asserted delivered on the same jit lane the absent zebra line and mark are read from; noqa: ORPHANED_MOCK — the double is reached through inject.gather -> _whisper._rerank -> relevance.at_prompt/score_turn, a cross-module path the walker does not follow
        self.settings(mode="live", wait_s=5.0)
        local = Recorder(scores={"prior:quokka-rule": 0.9, "prior:zebra-rule": 0.1}, threshold=0.5)

        def spawn(job):
            relevance.score_turn(job, local=local.local)
            return True, None
        with mock.patch.object(relevance, "_spawn", spawn):
            out = self.gather()
        self.assertNotIn(relevance.MARK, out["jit"])
        self.assertTrue(any("quokka-rule" in l for l in out["jit"]))
        self.assertFalse(any("zebra-rule" in l for l in out["jit"]))
        self.assertEqual(inject._ledger_rows()[-1]["relevance"]["state"], "scored")


class PerToolCallReadTest(Base):
    def test_the_read_calls_no_model_and_answers_only_a_scored_turn(self):
        cfg = self.settings()
        job = self.job()
        relevance._cache_put("s1", job["turn"], {"t0": job["t0"], "state": "pending"}, current=True)
        refuse = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network"))
        with refuse, mock.patch.object(relevance, "jev_scores", side_effect=AssertionError("model")), \
                mock.patch.object(relevance, "local_scores", side_effect=AssertionError("model")):
            self.assertIsNone(relevance.turn_scores("s1"))           # pending
        relevance.score_turn(job, cfg, Recorder().jev, Recorder(threshold=0.25).local)
        with refuse, mock.patch.object(relevance, "jev_scores", side_effect=AssertionError("model")), \
                mock.patch.object(relevance, "local_scores", side_effect=AssertionError("model")):
            got = relevance.turn_scores("s1")
        self.assertEqual((got["state"], got["threshold"], got["turn"]), ("scored", 0.25, job["turn"]))
        self.assertEqual(sorted(got["scores"]), ["prior:a", "prior:b"])

    def test_the_read_follows_the_current_turn(self):  # noqa: VACUOUS_ASSERTION — the None is bracketed by positive reads of the same turn_scores answer for the first and then the second turn
        cfg = self.settings()
        first, second = self.job(t0=1.0), self.job(t0=2.0)
        for j in (first, second):
            relevance._cache_put("s1", j["turn"], {"t0": j["t0"], "state": "pending"}, current=True)
        relevance.score_turn(first, cfg, Recorder().jev, Recorder().local)
        self.assertIsNone(relevance.turn_scores("s1"))            # current is the second
        self.assertEqual(relevance.turn_scores("s1", first["turn"])["turn"], first["turn"])
        relevance.score_turn(second, cfg, Recorder().jev, Recorder().local)
        self.assertEqual(relevance.turn_scores("s1")["turn"], second["turn"])


class SettingsTest(Base):
    def write(self, text):
        with open(os.path.join(home.global_dir(), relevance.CONFIG), "w") as f:
            f.write(text)

    def test_absent_broken_or_unknown_mode_is_off(self):  # noqa: VACUOUS_ASSERTION — the unconditional control after the loop reads live mode and every parsed value back from the same settings() call
        self.assertEqual((relevance.settings()["mode"], relevance.settings()["why"]), ("off", None))
        for text, needle in (("{nope", "did not read"), ("[]", "not a JSON object"),
                             ('{"mode": "on"}', "mode must be one of")):
            with self.subTest(text=text):
                self.write(text)
                cfg = relevance.settings()
                self.assertEqual(cfg["mode"], "off")
                self.assertIn(needle, cfg["why"])
        self.write(json.dumps({"mode": "live", "wait_s": 0.4, "candidates": 5,
                               "local_by_choice": ["p"], "jev": {"threshold": 0.4}}))
        cfg = relevance.settings()
        self.assertEqual((cfg["mode"], cfg["wait_s"], cfg["candidates"], cfg["local_by_choice"],
                          cfg["jev"]["threshold"]), ("live", 0.4, 5, ["p"], 0.4))

    def test_out_of_range_values_take_the_defaults(self):
        self.write(json.dumps({"mode": "shadow", "wait_s": 0, "candidates": True,
                               "jev": {"base": "http://plain.example", "threshold": 7}}))
        cfg = relevance.settings()
        self.assertEqual((cfg["wait_s"], cfg["candidates"], cfg["jev"]["base"],
                          cfg["jev"]["threshold"]),
                         (relevance.WAIT_S, relevance.CANDIDATES, relevance.JEV_BASE,
                          relevance.JEV_THRESHOLD))

    def test_the_wait_is_capped_at_three_seconds(self):  # noqa: VACUOUS_ASSERTION — every assertion is an equality on the parsed wait, including the accepted 0.4 and 3 controls
        """The hook's whole budget is 10 s; a longer wait would put its own
        alarm inside the bound."""
        for wait, want in ((0.4, 0.4), (3, 3.0), (3.5, relevance.WAIT_S), (8, relevance.WAIT_S),
                           (10, relevance.WAIT_S)):
            with self.subTest(wait=wait):
                self.write(json.dumps({"mode": "live", "wait_s": wait}))
                self.assertEqual(relevance.settings()["wait_s"], want)
        self.assertEqual(relevance.WAIT_MAX_S, 3.0)

    def test_the_endpoint_is_a_key_of_the_endpoints_file_and_never_a_default(self):
        path = os.path.join(home.global_dir(), relevance.ENDPOINTS)
        url, why = relevance.endpoint()
        self.assertEqual(url, "")
        self.assertIn('"relevance"', why)
        pk.write_json(path, {"relevance": "ftp://nope"})
        self.assertEqual(relevance.endpoint()[0], "")
        pk.write_json(path, {"relevance": "http://192.0.2.7:18780/"})
        self.assertEqual(relevance.endpoint(), ("http://192.0.2.7:18780", None))


class LocalByChoiceGateTest(Base):
    def run_turn(self, head="head-a", project="p"):
        cfg = self.settings(local_by_choice=["p", "q"])
        jev = Recorder()
        allow = {"p": "may-leave-lan", "q": "lan-only"}
        row = relevance.score_turn(self.job(project), cfg, jev.jev, Recorder().local,
                                   residency=lambda p: {"value": allow.get(p, "lan-only"), "why": "t"},
                                   health=lambda: {"model": head})
        return row, jev

    def receipt(self, head, ok):
        pk.write_json(relevance.receipt_path(), {"head": head, "pass": ok})

    def test_local_by_choice_waits_for_a_passing_receipt_for_this_head(self):
        row, jev = self.run_turn()
        self.assertEqual((row["tier"], len(jev.calls)), ("jev", 1))
        self.assertIn("refused: no remeasure receipt", row["why"])
        for head, ok in (("head-b", True), ("head-a", False)):
            with self.subTest(head=head, ok=ok):
                self.receipt(head, ok)
                row, jev = self.run_turn()
                self.assertEqual((row["tier"], len(jev.calls)), ("jev", 1))
        self.receipt("head-a", True)
        row, jev = self.run_turn()
        self.assertEqual((row["tier"], row["source"], len(jev.calls)), ("local", "local", 0))

    def test_residency_forced_local_is_never_gated(self):  # noqa: VACUOUS_ASSERTION — the row's tier and source are asserted positively, and the sibling arm proves the refusal text does appear on a gated turn
        row, jev = self.run_turn(project="q")          # lan-only, no receipt at all
        self.assertEqual((row["tier"], row["source"], len(jev.calls)), ("local", "local", 0))
        self.assertNotIn("refused", row["why"])


FAKE_SCORER = r"""
import http.server, json, sys
class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a):
        return
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        data = json.dumps({"p": {r["id"]: 0.7 for r in body["rows"]},
                           "model": "fake-head", "threshold": 0.5}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
srv = http.server.HTTPServer(("127.0.0.1", 0), H)
print(srv.server_address[1], flush=True)
srv.serve_forever()
"""

HOOK = r"""
import json, sys, threading
sys.path.insert(0, sys.argv[1])
from helm import relevance
entries = [{"id": "e1", "type": "prior", "confidence": 0.8,
            "statement": "Entry one says to check the rollback plan."}]
out = relevance.at_prompt(sys.argv[2], entries, session="s1", cfg=relevance.settings())
print(json.dumps(dict(out, threads=threading.active_count())))
"""

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The hook, with os.fork failing on its SECOND call in any process: the
#: intermediate child's own fork is that second call (the counter is copied
#: into it at 1), which is the loaded host's EAGAIN.
HOOK_SECOND_FORK_FAILS = r"""
import errno, json, os, sys
sys.path.insert(0, sys.argv[1])
real, calls = os.fork, [0]
def fork():
    calls[0] += 1
    if calls[0] >= 2:
        raise BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable")
    return real()
os.fork = fork
from helm import relevance
entries = [{"id": "e1", "type": "prior", "confidence": 0.8,
            "statement": "Entry one says to check the rollback plan."}]
out = relevance.at_prompt(sys.argv[2], entries, session="s1", cfg=relevance.settings())
print(json.dumps(out))
"""


class DetachedWorkerTest(Base):
    """The real spawn: a detached worker fills the cache AFTER the process
    that started it has exited, on both paths."""

    def setUp(self):
        super().setUp()
        import subprocess
        self.server = subprocess.Popen([sys.executable, "-c", FAKE_SCORER],
                                       stdout=subprocess.PIPE, text=True)
        self.addCleanup(self.server.wait, 10)
        self.addCleanup(self.server.kill)
        port = int(self.server.stdout.readline())
        pk.write_json(os.path.join(home.global_dir(), relevance.ENDPOINTS),
                      {"relevance": "http://127.0.0.1:%d" % port})
        self.settings(mode="shadow")

    def wait_scored(self, session="s1"):
        deadline, got = time.time() + 60, None
        while time.time() < deadline and got is None:
            got = relevance.turn_scores(session)
            time.sleep(0.1)
        self.assertIsNotNone(got, "the worker never scored the turn: %s" % self.log())
        self.assertEqual((got["source"], got["model"], got["scores"]),
                         ("local", "fake-head", {"prior:e1": 0.7}))
        return got

    def test_a_single_threaded_hook_forks_and_the_worker_outlives_it(self):  # noqa: VACUOUS_ASSERTION — wait_scored asserts the cache holds the worker's exact scores, source and model, and the submit rows are asserted to be exactly one fork
        import subprocess
        done = subprocess.run([sys.executable, "-c", HOOK, ROOT, TEXT], capture_output=True,
                              text=True, timeout=60, env=dict(os.environ))
        out = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual((out["state"], out["threads"]), ("pending", 1), done.stderr)
        got = self.wait_scored()
        self.assertEqual(got["turn"], out["turn"])
        subs = [r for r in relevance._read_ledger() if r["ev"] == "submit"]
        self.assertEqual([r.get("via") for r in subs], ["fork"])

    def test_a_failed_second_fork_is_no_worker_and_writes_nothing_to_the_hook(self):  # noqa: VACUOUS_ASSERTION — the empty stderr sits beside positive reads of the same run: state fallback, spawned False with its reason, and the traceback found in worker.log
        """The intermediate child exits nonzero, so the spawn answers False, a
        live hook does not wait for a worker that never existed, and the
        child's traceback goes to worker.log, never the harness's pipe."""
        import subprocess
        self.settings(mode="live", wait_s=2.0)
        done = subprocess.run([sys.executable, "-c", HOOK_SECOND_FORK_FAILS, ROOT, TEXT],
                              capture_output=True, text=True, timeout=60, env=dict(os.environ))
        out = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual(done.stderr, "")
        self.assertEqual(out["state"], "fallback")
        self.assertLess(out["waited_ms"], 1000.0)             # not the 2 s bound
        sub = [r for r in relevance._read_ledger() if r["ev"] == "submit"]
        self.assertEqual([r["spawned"] for r in sub], [False])
        self.assertIn("intermediate child exited 1", sub[0]["why"])
        self.assertIn("BlockingIOError", self.log())
        self.assertIsNone(relevance.turn_scores("s1"))

    def test_a_threaded_process_starts_a_fresh_interpreter_and_leaves_no_job_file(self):
        import threading
        stop = threading.Event()
        t = threading.Thread(target=stop.wait, daemon=True)
        t.start()
        self.addCleanup(stop.set)
        entries = [{"id": "e1", "type": "prior", "confidence": 0.8,
                    "statement": "Entry one says to check the rollback plan."}]
        out = relevance.at_prompt(TEXT, entries, session="s1", cfg=relevance.settings())
        self.assertEqual(out["state"], "pending")
        self.assertEqual([n for n in os.listdir(relevance._state_dir()) if n.startswith(".job-")], [])
        got = self.wait_scored()
        self.assertEqual(got["turn"], out["turn"])
        subs = [r for r in relevance._read_ledger() if r["ev"] == "submit"]
        self.assertEqual([r.get("via") for r in subs], ["exec"])
        evs = [r["ev"] for r in relevance._read_ledger() if r.get("turn") == out["turn"]]
        self.assertEqual(sorted(evs), ["score", "submit"])

    def log(self):
        try:
            with open(os.path.join(relevance._state_dir(), "worker.log")) as f:
                return f.read()[-2000:]
        except OSError:
            return "(no worker log)"


class CacheSweepTest(Base):
    """One cache and one lock per session would accumulate forever; the
    worker sweeps those untouched for CACHE_TTL."""

    def plant(self, session, age_days):
        relevance._cache_put(session, "t", {"t0": 1.0, "state": "pending"}, current=True)
        path = relevance.cache_path(session)
        old = time.time() - age_days * 86400
        for p in (path, path + ".lock"):
            os.utime(p, (old, old))
        return path

    def test_stale_caches_go_fresh_and_locked_ones_stay(self):
        import fcntl
        stale, fresh, held = self.plant("stale", 8), self.plant("fresh", 1), self.plant("held", 9)
        orphan = os.path.join(relevance._state_dir(), "gone.json.lock")
        open(orphan, "w").close()
        os.utime(orphan, (time.time() - 9 * 86400,) * 2)
        fd = os.open(held + ".lock", os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.assertEqual(relevance.sweep(), 1)
        self.assertFalse(os.path.exists(stale) or os.path.exists(stale + ".lock"))
        self.assertFalse(os.path.exists(orphan))
        self.assertTrue(os.path.exists(fresh) and os.path.exists(held))
        self.assertEqual(relevance._cache_entry("fresh")[1]["state"], "pending")

    def test_the_worker_sweeps(self):
        stale = self.plant("stale", 8)
        relevance.score_turn(self.job(session="s2"), self.settings(), Recorder().jev, Recorder().local)
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(relevance.cache_path("s2")))


NOTICE = ("<task-notification>\n<task-id>b1</task-id>\n<status>completed</status>\n"
          "<summary>Monitor event: \"helm chat\"</summary>\n"
          "<event>[helm chat #room \u2192 seat @2026-01-01T00:00:00Z] peer: the zebra "
          "rollback needs a plan (+3 waiting \u2014 helm chat read --room room)</event>\n"
          "</task-notification>")


class LabelFormTest(Base):
    """The turn is scored in the form its labels had."""

    def test_envelopes_frames_and_headers_go_as_the_labels_read_them(self):
        self.assertEqual(relevance.label_form(NOTICE), "peer: the zebra rollback needs a plan")
        done = ("<task-notification><summary>Background command \"x\" completed</summary>"
                "<result>exit code 1</result></task-notification>")
        self.assertEqual(relevance.label_form(done),
                         'Background command "x" completed\nexit code 1')
        hand = ('<agent-message from="child">[Subagent hand-back] a frame. The report follows: '
                "the migration is staged</agent-message>")
        self.assertEqual(relevance.label_form(hand), "the migration is staged")
        typed = "[harness: note]\nplease stage the zebra migration\n[helm chat] more pending \u2014 2"
        self.assertEqual(relevance.label_form(typed), "please stage the zebra migration")
        self.assertEqual(relevance.label_form(TEXT), TEXT)                  # typed text is kept

    def test_a_long_turn_is_cut_as_the_labels_were_and_the_head_cut_agrees(self):  # noqa: VACUOUS_ASSERTION — both assertions are equalities on constructed strings; nothing asserted is an absence
        long = "a" * 2500 + "b" * 1000 + "c" * 900
        got = relevance.label_form(long)
        self.assertEqual(got, long[:2000] + "\n[...]\n" + long[-900:])
        cut1000 = lambda t: t if len(t) <= 1000 else t[:700] + "\n[...]\n" + t[-300:]
        self.assertEqual(cut1000(got), cut1000(long))

    def test_the_job_and_the_evaluator_carry_the_label_form(self):
        jobs = []
        relevance.at_prompt(NOTICE, [{"id": "e1", "type": "prior", "confidence": 0.8,
                                      "statement": "Entry one."}],
                            session="s1", cfg=self.settings(),
                            spawn=lambda job: (jobs.append(job) or (True, "test")))
        self.assertEqual(jobs[0]["text"], "peer: the zebra rollback needs a plan")
        self.assertIsNone(relevance.at_prompt(
            "<task-notification><summary>Monitor event: x</summary></task-notification>",
            [{"id": "e1", "type": "prior", "confidence": 0.8, "statement": "Entry one."}],
            session="s2", cfg=self.settings(), spawn=lambda job: (jobs.append(job) or (True, "t"))))
        self.assertEqual(len(jobs), 1)                  # an empty label form scores nothing

    def test_the_evaluator_is_sent_the_bytes_the_scrub_read(self):  # noqa: VACUOUS_ASSERTION — the two empty scrub reads sit beside the premise that a second pass differs, the payload's equality with the job text, and the positive hit the double pass produces
        """The job's text is the label form; the evaluator door sends it AS
        GIVEN. A second reading is not a no-op: a strip can join two
        fragments into a scrub token, and a hand-back carrying a task notice
        reads as the notice. So the arm plants inputs whose second pass
        differs, scrubs the job text (clean), and asserts the payload is the
        job text byte for byte."""
        joined = ("deadbeefdeadbeef(+3 waiting — helm [helm chat x]chat read)"
                  "deadbeefdeadbeef")
        nested = ('<agent-message from="child">[Subagent hand-back] a frame. '
                  "The report follows: <task-notification><summary>done</summary>"
                  "<result>the migration is staged</result></task-notification>"
                  "</agent-message>")
        for raw in (joined, nested):
            text = relevance.label_form(raw)
            self.assertNotEqual(relevance.label_form(text), text)     # the premise: a second pass differs
            self.assertEqual(relevance.scrub_hits(text), [])           # and the job text is clean
            sent = []
            post = lambda url, body, timeout, headers=None: (
                sent.append(body["state"]["message"]) or {"answers": {"q0": {"probability": 0.9}}})
            with mock.patch.object(relevance, "_post", post), \
                    mock.patch.object(relevance, "jev_key", lambda cfg: "k"):
                row = relevance.score_turn(
                    self.job(project="alpha", text=text, ids=("prior:a",)), self.settings(),
                    relevance.jev_scores, Recorder().local,
                    residency=lambda p: {"value": "may-leave-lan", "why": "authored"})
            self.assertEqual((row["source"], sent), ("jev", [text]))
            self.assertEqual(relevance.scrub_hits(*sent), [])
        # the joined shape's second pass is a scrub hit: the defect this arm holds shut
        self.assertEqual(relevance.scrub_hits(relevance.label_form(relevance.label_form(joined))),
                         [r"\b[0-9a-fA-F]{32,}\b"])


class JevClientTest(Base):
    def test_one_request_one_question_per_candidate_and_the_key_only_in_the_header(self):
        seen = {}

        class Gateway(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                return

            def do_POST(self):
                seen["auth"] = self.headers["Authorization"]
                seen["path"] = self.path
                seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                code = 429 if seen.get("refuse") else 200
                data = json.dumps({"answers": {q: {"probability": 0.25 * (i + 1)}
                                               for i, q in enumerate(sorted(seen["body"]["questions"]))}}).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        cred = os.path.join(self.tmp, "cred.md")
        with open(cred, "w") as f:
            f.write("# a credential\nAI\\_GATEWAY\\_API\\_KEY=test\\_value\\_123\n")
        cfg = relevance.settings()
        cfg["jev"].update(base="http://127.0.0.1:%d" % srv.server_address[1], cred_file=cred)
        cands = [{"id": "prior:a", "note": "a: first"}, {"id": "prior:b", "note": "b: second"}]
        scores, model = relevance.jev_scores(TEXT, cands, cfg)
        self.assertEqual((scores, model), ({"prior:a": 0.25, "prior:b": 0.5}, relevance.JEV_MODEL))
        self.assertEqual((seen["path"], seen["auth"]), ("/v1/evaluate", "Bearer test_value_123"))
        self.assertEqual(seen["body"]["state"], {"message": TEXT})
        self.assertEqual(seen["body"]["questions"]["q0"]["instructions"], relevance.QUESTION % "a: first")
        seen["refuse"] = True
        with self.assertRaises(RuntimeError) as ctx:
            relevance.jev_scores(TEXT, cands, cfg)
        self.assertIn("HTTP 429", str(ctx.exception))
        self.assertNotIn("test_value_123", str(ctx.exception))


class ReportAndVerbTest(Base):
    def test_the_report_reads_fallbacks_per_tier_and_the_teachers_agreement(self):
        cfg = self.settings()
        allow = lambda p: {"value": "may-leave-lan", "why": "t"}
        jev = Recorder(scores={"prior:a": 0.9, "prior:b": 0.1})
        local = Recorder(scores={"prior:a": 0.8, "prior:b": 0.2}, threshold=0.5)
        relevance.score_turn(self.job(), cfg, jev.jev, local.local, residency=allow)
        late = self.job(t0=time.time() - 30)
        relevance.score_turn(late, cfg, jev.jev, local.local)
        rep = relevance.report()
        self.assertEqual(rep["tiers"]["jev"]["fallback_rate"], 0.0)
        self.assertEqual(rep["tiers"]["local"]["fallback_rate"], 1.0)
        self.assertEqual((rep["teach"]["pairs"], rep["teach"]["auc_local_vs_evaluator"],
                          rep["teach"]["decision_agreement"]), (2, 1.0, 1.0))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = relevance_cli.cmd_relevance(["report"])
        self.assertEqual(rc, 0)
        self.assertIn("fallback 1.0", out.getvalue())

    def test_status_names_the_credential_state_and_never_its_value(self):  # noqa: VACUOUS_ASSERTION — the same output is asserted to say `credential present`, so the absent value is not an empty print
        cred = os.path.join(self.tmp, "cred.md")
        with open(cred, "w") as f:
            f.write("KEY=secret\\_value\\_987\n")
        self.settings(jev={"cred_file": cred})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(relevance_cli.cmd_relevance([]), 0)
        self.assertIn("credential present", out.getvalue())
        self.assertNotIn("secret_value_987", out.getvalue())
        self.assertIn("scorer     UNCONFIGURED", out.getvalue())


class ResidencyVerbTest(Base):
    def test_the_verb_lists_writes_and_clears_the_field(self):
        from helm import cli
        self.plant_projects({})
        run = lambda *a: self._run(cli.main, list(a))
        rc, out = run("projects", "residency")
        self.assertEqual(rc, 0)
        self.assertIn("alpha  lan-only\n", out)
        rc, out = run("projects", "residency", "alpha", "may-leave-lan", "--reason", "non-client")
        self.assertIn("dry-run alpha lan-only (no field) -> may-leave-lan", out)
        self.assertEqual(registry.residency("alpha")["value"], "lan-only")
        rc, out = run("projects", "residency", "alpha", "may-leave-lan", "--reason",
                      "non-client", "--apply")
        self.assertEqual((rc, registry.residency("alpha")["value"]), (0, "may-leave-lan"))
        rc, out = run("projects", "residency")
        self.assertIn("alpha  may-leave-lan*  non-client", out)
        rc, out = run("projects", "residency", "alpha", "clear", "--apply")
        self.assertEqual((rc, registry.residency("alpha")["value"]), (0, "lan-only"))
        rc, _out = run("projects", "residency", "alpha", "may-leave-lan", "--apply")
        self.assertEqual((rc, registry.residency("alpha")["value"]), (1, "lan-only"))

    def _run(self, fn, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fn(argv)
        return rc, out.getvalue() + err.getvalue()


if __name__ == "__main__":
    unittest.main()
