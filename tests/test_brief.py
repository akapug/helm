#!/usr/bin/env python3
"""brief tests — hermetic: tmp HELM_HOME + HELM_ADOPTED_DIR, catalog roots and
caches repointed at tmp dirs (scanner path forced, cv never invoked), the
usage-history read repointed at a tmp file. The real ~/.helm, ~/.claude and
~/.cache are never read or written."""
import contextlib
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from helm import brief, catalog, home, pk, store, transcripts, whoami

SID_A1 = "aaaaaaaa-1111-2222-3333-444444444444"
SID_A2 = "aaaaaaab-1111-2222-3333-444444444444"
SID_B1 = "bbbbbbbb-1111-2222-3333-444444444444"
SID_OLD = "cccccccc-1111-2222-3333-444444444444"
SID_SYN = "dddddddd-1111-2222-3333-444444444444"

ACCT = "alice@example.com"


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _gauges(util):
    return [{"label": "5h", "kind": "session", "utilization": util,
             "reset": None, "limit": None, "remaining": None}]


class BriefBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-brief-")
        j = self.j = lambda *p: os.path.join(self.tmp, *p)
        self.adopted = j("adopted")
        os.makedirs(self.adopted)
        self.envp = mock.patch.dict(os.environ, {
            "HELM_HOME": j("helm"),
            "HELM_ADOPTED_DIR": self.adopted,
            "HELM_CATALOG": "scanner",  # never shell out to cv for the catalog
        })
        self.envp.start()
        for k in ("MELD_HOME", "MELD_ADOPTED_DIR"):
            os.environ.pop(k, None)
        self._cat = {k: getattr(catalog, k) for k in
                     ("CLAUDE_ROOTS", "CODEX_ROOTS", "CACHE_DIR", "CACHE", "SYN_CACHE")}
        catalog.CLAUDE_ROOTS = [j("claude-root")]
        catalog.CODEX_ROOTS = [j("codex-root")]
        catalog.CACHE_DIR = j("cache")
        catalog.CACHE = j("cache", "catalog-cache.json")
        catalog.SYN_CACHE = j("cache", "syn-cache.json")
        self._overrides = transcripts.OVERRIDES_PATH
        transcripts.OVERRIDES_PATH = j("cache", "cwd-overrides.json")
        self._hist = brief.USAGE_HISTORY
        brief.USAGE_HISTORY = j("native-usage-history.jsonl")
        transcripts._state.clear()
        transcripts._cwd_overrides = {}
        self.alpha = j("work", "alpha")
        self.beta = j("work", "beta")
        os.makedirs(self.alpha)
        os.makedirs(self.beta)
        self.now = time.time()

    def tearDown(self):
        for k, v in self._cat.items():
            setattr(catalog, k, v)
        transcripts.OVERRIDES_PATH = self._overrides
        brief.USAGE_HISTORY = self._hist
        transcripts._state.clear()
        transcripts._cwd_overrides = {}
        self.envp.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- fixtures ----------------------------------------------------------
    def seed_registry(self, extra=None):
        rec = lambda name, path: {"name": name, "path": path, "kind": "git",
                                  "status": "active", "sessions": {}}
        projects = {"alpha": rec("alpha", self.alpha), "beta": rec("beta", self.beta)}
        projects.update(extra or {})
        pk.write_json(home.registry_path(), {"version": 1, "projects": projects})

    def plant_claude(self, sid, cwd, title, mtime=None):
        d = os.path.join(catalog.CLAUDE_ROOTS[0], "slug-" + sid[:8])
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, sid + ".jsonl")
        with open(path, "w") as f:
            f.write(json.dumps({"cwd": cwd, "gitBranch": "main",
                                "timestamp": "2026-07-01T10:00:00Z",
                                "message": {"role": "user", "content": title}}) + "\n")
            f.write(json.dumps({"message": {"role": "assistant",
                                            "content": "x" * 300}}) + "\n")
        if mtime:
            os.utime(path, (mtime, mtime))
        return path

    def plant_ledger(self, rows, generation=""):
        from helm import inject
        path = inject._ledger_path() + generation
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            for r in rows:
                f.write((r if isinstance(r, str) else json.dumps(r)) + "\n")

    def plant_history(self, rows):
        with open(brief.USAGE_HISTORY, "w") as f:
            for r in rows:
                f.write((r if isinstance(r, str) else json.dumps(r)) + "\n")

    def compose(self, hours=12.0):
        transcripts._state.clear()  # fixtures planted after a cached build must count
        return brief.compose(hours=hours)


class SinceYouLeftTest(BriefBase):
    def test_window_bucketing(self):
        self.seed_registry()
        self.plant_claude(SID_A1, self.alpha, "wire the brief verb",
                          mtime=self.now - 300)
        self.plant_claude(SID_A2, self.alpha, "test the brief verb",
                          mtime=self.now - 60)
        self.plant_claude(SID_B1, self.beta, "beta work")
        self.plant_claude(SID_OLD, self.alpha, "yesterday's work",
                          mtime=self.now - 24 * 3600)  # outside the 12h window
        self.plant_claude(SID_SYN, self.alpha,
                          "You are summarizing a Claude Code session about alpha")
        b = self.compose()
        s = b["sessions"]
        self.assertEqual(s["total"], 3)  # old + synthetic excluded
        self.assertEqual([(p["name"], p["n"]) for p in s["projects"]],
                         [("alpha", 2), ("beta", 1)])
        self.assertEqual(s["projects"][0]["latest_title"], "test the brief verb")
        text = brief.render(b)
        self.assertIn("SINCE YOU LEFT — 3 sessions, 2 projects", text)
        self.assertIn("alpha", text)
        # widen the window: the old session joins
        self.assertEqual(self.compose(hours=48)["sessions"]["total"], 4)

    def test_unassigned_sessions_bucket_under_dash(self):
        self.plant_claude(SID_A1, self.j("elsewhere"), "unregistered work")
        s = self.compose()["sessions"]
        self.assertEqual([(p["name"], p["n"]) for p in s["projects"]], [("-", 1)])


class KnowledgeDeltaTest(BriefBase):
    def test_added_updated_retired_and_drained(self):
        self.seed_registry()
        fresh, stale = _iso(self.now - 3600), "2026-06-01"
        store.write_prior({"id": "new-fact", "statement": "just learned",
                           "stated_ts": fresh})
        store.write_prior({"id": "old-fact", "statement": "revised",
                           "stated_ts": stale, "last_updated": fresh})
        store.write_prior({"id": "ancient", "statement": "unchanged",
                           "stated_ts": stale, "last_updated": "2026-06-02"})
        store.write_prior({"id": "dead", "statement": "let go", "status": "retired",
                           "stated_ts": stale, "retired_ts": _iso(self.now - 1800)})
        store.write_prior({"id": "proj-fact", "statement": "alpha-scoped",
                           "stated_ts": fresh},
                          root_dir=os.path.join(home.project_dir("alpha"), "premises"))
        for name, ts, applied in (("drain-new", _iso(self.now - 600), 3),
                                  ("drain-old", "2026-06-01T00:00:00Z", 5)):
            d = os.path.join(self.adopted, "archive", name)
            os.makedirs(d)
            pk.write_json(os.path.join(d, "RECEIPT.json"), {"ts": ts, "applied": applied})
        k = self.compose()["knowledge"]
        self.assertEqual(k["added"], ["new-fact", "proj-fact"])
        self.assertEqual(k["updated"], ["old-fact"])
        self.assertEqual(k["retired"], ["dead"])
        self.assertEqual(k["drained"], 3)  # the old receipt stays out
        text = brief.render(self.compose())
        self.assertIn("KNOWLEDGE DELTA — +2 added · 1 updated · 1 retired · 3 drained", text)
        self.assertIn("  + new-fact", text)

    def test_quiet_store_is_omitted(self):
        store.write_prior({"id": "ancient", "statement": "unchanged",
                           "stated_ts": "2026-06-01", "last_updated": "2026-06-02"})
        b = self.compose()
        self.assertEqual(b["knowledge"],
                         {"added": [], "updated": [], "retired": [], "drained": 0})
        self.assertNotIn("KNOWLEDGE DELTA", brief.render(b))


class InjectLedgerTest(BriefBase):
    def test_absent_ledger_tolerated(self):
        b = self.compose()
        self.assertIsNone(b["inject"])
        self.assertNotIn("inject:", brief.render(b))

    def test_ledger_stats_window_and_top(self):
        fresh = _iso(self.now - 600)
        self.plant_ledger([
            {"v": 1, "ts": fresh, "project": "alpha",
             "fired": {"pinned": ["p1"], "jit": ["j1"], "reflex": []}},
            {"v": 1, "ts": fresh, "project": "alpha",
             "fired": {"pinned": ["p1"], "jit": [], "reflex": ["r1"]}},
            {"v": 1, "ts": fresh, "silent": True},
            {"v": 1, "ts": "2026-06-01T00:00:00Z", "silent": True},  # outside window
            "not json at all",
        ])
        self.plant_ledger([{"v": 1, "ts": fresh,  # rotated generation counts too
                            "fired": {"pinned": [], "jit": ["j1"], "reflex": []}}],
                          generation=".1")
        inj = self.compose()["inject"]
        self.assertEqual((inj["turns"], inj["silent"]), (4, 1))
        self.assertEqual(inj["silent_rate"], 0.25)
        self.assertEqual(inj["top"], [("j1", 2), ("p1", 2), ("r1", 1)])
        self.assertIn("inject: 4 turns · 25% silent · top j1 ×2, p1 ×2, r1 ×1",
                      brief.render(self.compose()))

    def test_pinned_starvation_surfaced_only_when_real(self):
        """The 6/11-never-fired class reaches the owner's brief: always-on
        entries that never made an injection in the ledger window get one
        line; a fully-fed pinned lane stays silent (empty-section law)."""
        from helm import store
        store.write_prior({"id": "p-hot", "statement": "fed", "confidence": "1.0",
                           "pin": "true"})
        store.write_prior({"id": "p-cold", "statement": "starved",
                           "confidence": "1.0", "pin": "true"})
        fresh = _iso(self.now - 600)
        self.plant_ledger([{"v": 1, "ts": fresh,
                            "fired": {"pinned": ["p-hot"], "jit": [], "reflex": []}}])
        inj = self.compose()["inject"]
        self.assertEqual(inj["starved"], ["p-cold"])
        self.assertEqual(inj["always_n"], 2)
        out = brief.render(self.compose())
        self.assertIn("pinned starvation: 1 of 2 never fired: p-cold", out)
        self.assertIn("helm store pinned --stats", out)
        # feed the cold one -> the line disappears
        self.plant_ledger([{"v": 1, "ts": fresh,
                            "fired": {"pinned": ["p-cold"], "jit": [], "reflex": []}}])
        out = brief.render(self.compose())
        self.assertNotIn("pinned starvation", out)


class SeatsTest(BriefBase):
    def test_cached_observations_freshest_wins_no_probe(self):
        self.plant_history([
            {"provider": "anthropic", "account": ACCT, "probed_at": _iso(self.now - 7200),
             "status": "allowed", "primary": "5h", "gauges": _gauges(0.4)},
            {"provider": "anthropic", "account": ACCT, "probed_at": _iso(self.now - 3600),
             "status": "allowed", "primary": "5h", "gauges": _gauges(0.6)},
            {"provider": "anthropic", "account": ACCT, "probed_at": _iso(self.now),
             "status": "network-error"},  # gaugeless row never wins
            "garbage line",
        ])
        seats = self.compose()["seats"]
        self.assertEqual(len(seats), 1)
        self.assertEqual(seats[0]["account"], ACCT)
        self.assertEqual(seats[0]["headroom_pct"], 40.0)
        self.assertEqual(seats[0]["window"], "5h")
        text = brief.render(self.compose())
        self.assertIn(ACCT, text)
        self.assertIn("40% headroom (5h)", text)
        self.assertNotIn("quota: run", text)

    def test_no_cache_says_run_creds(self):
        b = self.compose()
        self.assertEqual(b["seats"], [])
        self.assertIn("quota: run `helm creds`", brief.render(b))


class WaitingOnYouTest(BriefBase):
    def test_fresh_estate_waits_on_the_interview(self):
        items = self.compose()["waiting"]
        self.assertTrue(any("helm interview" in it for it in items))

    def test_owner_gates_surface(self):
        pk.write_json(whoami.profile_path(), {"schema_version": 1,
                                              "interview_status": "done"})
        qp = os.path.join(home.global_dir(), ".state", "attest-queue.jsonl")
        os.makedirs(os.path.dirname(qp), exist_ok=True)
        with open(qp, "w") as f:
            f.write(json.dumps({"id": "a"}) + "\n" + json.dumps({"id": "b"}) + "\n")
        self.seed_registry(extra={"gone": {"name": "gone", "kind": "git",
                                           "status": "active", "sessions": {},
                                           "path": self.j("repos", "gone")}})
        for n in ("prem-twin.md", "prior-twin.md"):
            with open(os.path.join(self.adopted, n), "w") as f:
                f.write("---\nname: x\n---\n")
        items = self.compose()["waiting"]
        self.assertFalse(any("helm interview" in it for it in items))
        self.assertIn("2 attestations queued — `helm premise --retry-queue`", items)
        self.assertTrue(any("1 project pointer stale (gone)" in it for it in items))
        self.assertTrue(any("1 prem/prior duplicate pair" in it for it in items))
        text = brief.render(self.compose())
        self.assertIn("WAITING ON YOU", text)


class StoreReviewQueueTest(BriefBase):
    """The provisional queue must ROUTINELY reach
    the owner. helm brief gains a store-review-queue section — provisional
    (firing, awaiting ratify) listed newest-first as `[type] id - statement`
    (clipped 80), candidate count only, omitted entirely when both are zero
    (the empty-section law). Hermetic: entries seeded straight to disk with an
    explicit status (no xrev-clear, so the ntfy path is never exercised here)."""

    def _prov(self, pid, stmt, ts):
        store.write_prior({"id": pid, "statement": stmt, "confidence": 0.7,
                           "status": "provisional", "xrev_by": "codex-seat",
                           "xrev_ts": ts, "stated_ts": ts, "last_updated": ts})

    def test_provisional_listed_newest_first_candidate_counted(self):
        older, newer = _iso(self.now - 7200), _iso(self.now - 600)
        self._prov("prov-old", "the older cleared belief", older)
        self._prov("prov-new", "the newer cleared belief", newer)
        store.write_lexicon({"term": "cand-a", "definition": "a raw guess",
                             "status": "candidate", "updated_ts": newer})
        store.write_prior({"id": "cand-b", "statement": "another guess",
                           "confidence": 0.6, "status": "candidate",
                           "stated_ts": newer, "last_updated": newer})
        b = self.compose()
        rq = b["review"]
        self.assertEqual([e["id"] for e in rq["provisional"]],
                         ["prov-new", "prov-old"])   # newest graduation first
        self.assertEqual(rq["candidate"], 2)          # count only, not listed
        text = brief.render(b)
        self.assertIn("STORE REVIEW QUEUE — 2 provisional (firing, awaiting your "
                      "ratify) · 2 candidate", text)
        self.assertIn("[prior] prov-new - the newer cleared belief", text)
        self.assertLess(text.index("prov-new"), text.index("prov-old"))  # ordered
        self.assertNotIn("cand-a", text)              # candidates are count-only

    def test_statement_clipped_to_80(self):
        self._prov("long-one", "y" * 200, _iso(self.now - 100))
        line = [l for l in brief.render(self.compose()).splitlines()
                if "[prior] long-one" in l][0]
        self.assertIn("y" * 80, line)
        self.assertNotIn("y" * 81, line)

    def test_more_provisional_folds(self):
        for i in range(8):
            self._prov("prov-%02d" % i, "cleared belief %d" % i,
                       _iso(self.now - i * 60))
        text = brief.render(self.compose())
        self.assertEqual(text.count("[prior] prov-"), brief.MAX_REVIEW)
        self.assertIn("(+2 more provisional)", text)

    def test_candidate_only_still_renders_section(self):
        store.write_lexicon({"term": "cand-a", "definition": "a raw guess",
                             "status": "candidate", "updated_ts": _iso(self.now)})
        b = self.compose()
        self.assertEqual(b["review"], {"provisional": [], "candidate": 1})
        text = brief.render(b)
        self.assertIn("STORE REVIEW QUEUE — 0 provisional", text)
        self.assertIn("· 1 candidate", text)

    def test_section_omitted_when_queue_empty(self):
        store.write_prior({"id": "live-one", "statement": "confirmed truth",
                           "stated_ts": "2026-06-01", "last_updated": "2026-06-02"})
        b = self.compose()
        self.assertEqual(b["review"], {"provisional": [], "candidate": 0})
        self.assertNotIn("STORE REVIEW QUEUE", brief.render(b))


class CmdBriefTest(BriefBase):
    def _run(self, args):
        out = io.StringIO()
        transcripts._state.clear()
        with contextlib.redirect_stdout(out):
            rc = brief.cmd_brief(args)
        return rc, out.getvalue()

    def test_json_shape(self):
        self.seed_registry()
        self.plant_claude(SID_A1, self.alpha, "alpha work")
        rc, out = self._run(["--json"])
        self.assertEqual(rc, 0)
        b = json.loads(out)
        self.assertEqual(sorted(b), ["generated_at", "hours", "inject", "knowledge",
                                     "review", "seats", "sessions", "waiting"])
        self.assertEqual(b["hours"], 12.0)
        self.assertEqual(b["sessions"]["total"], 1)
        self.assertIsNone(b["inject"])
        rc, out = self._run(["--json", "--hours", "24"])
        self.assertEqual(json.loads(out)["hours"], 24.0)

    def test_bad_hours_is_usage_error(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(brief.cmd_brief(["--hours", "soon"]), 2)
        self.assertIn("usage:", err.getvalue())

    def test_empty_estate_renders_short_valid_brief(self):
        pk.write_json(whoami.profile_path(), {"schema_version": 1,
                                              "interview_status": "done"})
        rc, out = self._run([])
        self.assertEqual(rc, 0)
        lines = out.strip().splitlines()
        self.assertTrue(lines[0].startswith("helm brief — "))
        self.assertIn("quiet — nothing new in the window.", lines[1])
        self.assertIn("quota: run `helm creds`", out)
        self.assertLessEqual(len(lines), 12)

    def test_render_stays_under_the_line_cap(self):
        self.seed_registry()
        for i in range(8):
            sid = "%08x-1111-2222-3333-444444444444" % i
            proj = self.j("work", "p%d" % i)
            os.makedirs(proj, exist_ok=True)
            self.plant_claude(sid, proj, "work in p%d" % i)
        self.plant_history([{"provider": "anthropic", "account": "u%d@x.com" % i,
                             "probed_at": _iso(self.now), "status": "allowed",
                             "primary": "5h", "gauges": _gauges(0.5)} for i in range(8)])
        self.plant_ledger([{"v": 1, "ts": _iso(self.now - 60),
                            "fired": {"pinned": ["p1"], "jit": [], "reflex": []}}])
        rc, out = self._run([])
        self.assertEqual(rc, 0)
        self.assertLessEqual(len(out.strip().splitlines()), 40)


if __name__ == "__main__":
    unittest.main()
