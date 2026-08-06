#!/usr/bin/env python3
"""brief tests — hermetic: tmp HELM_HOME + HELM_ADOPTED_DIR, catalog roots and
caches repointed at tmp dirs (scanner path forced, cv never invoked), the
usage-history read repointed at a tmp file, the cwd moved into the tmp dir so
the WHAT WE BUILT gauge never reads the real checkout. The real ~/.helm,
~/.claude and ~/.cache are never read or written."""
import contextlib
import io
import json
import os
import shutil
import subprocess
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
            "GIT_CONFIG_GLOBAL": "/dev/null",  # fixture repos, hermetic config
            "GIT_CONFIG_SYSTEM": "/dev/null",
        })
        self.envp.start()
        # The built gauge reads the cwd repo by default — park the cwd in the
        # tmp dir so no test (cmd_brief included) ever gauges the real checkout.
        self._cwd = os.getcwd()
        os.chdir(self.tmp)
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
        os.chdir(self._cwd)
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

    def compose(self, hours=12.0, repo=None):
        transcripts._state.clear()  # fixtures planted after a cached build must count
        return brief.compose(hours=hours, repo=repo)


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
        # p-cold FITS — it has simply never landed yet. That is the innocent
        # case and must not be reported as unable to fire.
        self.assertEqual(inj["cannot_fire"], [])
        self.assertEqual(inj["never_fired"], ["p-cold"])
        # feed the cold one -> the line disappears
        self.plant_ledger([{"v": 1, "ts": fresh,
                            "fired": {"pinned": ["p-cold"], "jit": [], "reflex": []}}])
        out = brief.render(self.compose())
        self.assertNotIn("pinned starvation", out)

    def test_an_entry_the_budget_never_reaches_is_starved_even_when_it_has_fired(self):
        """THE CASE THE OLD PREDICATE COULD NOT SEE, and the reason it exists.

        `starved` asked `made[id] == 0` — never fired across the whole ledger
        window. An always-entry that fired ONCE, long ago, and can no longer
        fit under the budget scores 1, not 0, and vanishes from the surface
        built to catch exactly it.

        MEASURED on the live store 2026-07-30: 5 always-entries, each rendered
        line capped to LINE_CAP=400, budget PINNED_BUDGET=1200. 3 x 400 == 1200
        exactly, so the 4th and 5th could not fire on ANY turn — and the brief
        reported NOTHING, because all five had non-zero lifetime counts. Two of
        the dead ones were the owner's rules about how to sequence work.

        NOTE ON THE FIXTURE, learned by getting it wrong first: ONE oversized
        entry cannot starve the lane, because LINE_CAP truncates every entry to
        400 bytes before the walk sees it. It takes enough entries to exhaust
        the budget — PINNED_BUDGET // LINE_CAP of them — and the next one is
        the one that can never fire. That is precisely the live shape."""
        from helm import inject, store
        fill = inject.PINNED_BUDGET // inject.LINE_CAP      # 3 at today's values
        big = "X" * (inject.LINE_CAP * 2)                   # each truncated to LINE_CAP
        planted = []
        for n in range(fill):
            i = "p-fill-%d" % n
            store.write_prior({"id": i, "statement": big,
                               "confidence": "1.0", "pin": "true"})
            planted.append(i)
        store.write_prior({"id": "p-late", "statement": big,
                           "confidence": "1.0", "pin": "true"})
        planted.append("p-late")
        fresh = _iso(self.now - 600)
        # EVERY entry fired in the past: lifetime counts are 1, never 0, so the
        # old `== 0` predicate scores the whole lane healthy.
        self.plant_ledger([{"v": 1, "ts": fresh,
                            "fired": {"pinned": planted, "jit": [], "reflex": []}}])
        inj = self.compose()["inject"]
        self.assertTrue(inj["cannot_fire"],
                        "the budget is exhausted before the last entry, so at "
                        "least one always-entry cannot fire — and a lifetime "
                        "count of 1 must not hide that")
        self.assertEqual(inj["never_fired"], [],
                         "every entry has fired, so the never-fired bucket is "
                         "empty — the two cases must not be conflated")
        out = brief.render(self.compose())
        self.assertIn("CANNOT FIRE", out)


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
    """The owner steer 2026-07-23: the provisional queue must ROUTINELY reach
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


class WhatWeBuiltTest(BriefBase):
    """The gauge (owner directive: the brief answers 'what did we build?').
    Fixture repos are REAL git: init -b main, empty commits with pinned
    committer dates, and origin/main planted as an actual remote-tracking ref
    so the gauge reads the landedness ref, never local main."""

    def _repo(self):
        r = self.j("repo")
        os.makedirs(r)
        for cmd in (("git", "init", "-q", "-b", "main"),
                    ("git", "config", "user.email", "t@t"),
                    ("git", "config", "user.name", "t")):
            subprocess.run(cmd, cwd=r, check=True, capture_output=True)
        return r

    def _land(self, repo, subject, when):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S +0000", time.gmtime(when))
        env = dict(os.environ, GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
        subprocess.run(("git", "commit", "-q", "--allow-empty", "-m", subject),
                       cwd=repo, env=env, check=True, capture_output=True)

    def _publish(self, repo):
        subprocess.run(("git", "update-ref", "refs/remotes/origin/main", "main"),
                       cwd=repo, check=True, capture_output=True)

    def test_a_local_only_ref_renders_commits_never_lands(self):
        """kimi's review finding (the landed-means-origin-main class): with no
        remote-tracking ref, trunk_ref degrades to LOCAL main — and a local
        merge is exactly the claim this gauge must never launder into a
        "land". The header must name the weaker claim; the word "land" for
        the count is reserved for a published ref. The sibling tests publish
        via _publish; this one deliberately does not."""
        r = self._repo()
        self._land(r, "local: committed but never pushed", self.now - 600)
        b = self.compose(repo=r)
        built = b["built"]
        self.assertFalse(built["published"],
                         "a remoteless repo read as published")
        self.assertEqual(built["total"], 1)
        text = brief.render(b)
        self.assertIn("LOCAL commit", text)
        self.assertIn("not proof of publication", text)
        self.assertNotIn("1 land on", text,
                         "a local-only commit rendered as a land")

    def test_lands_listed_grouped_by_day_window_enforced(self):
        r = self._repo()
        self._land(r, "land: ancient — before the gauge window",
                   self.now - 100 * 3600)
        t_old, t_mid, t_new = (self.now - 26 * 3600, self.now - 3600,
                               self.now - 600)
        self._land(r, "land: yesterday — the fleet got X", t_old)
        self._land(r, "land: earlier — the fleet got Y", t_mid)
        self._land(r, "land: just now — the fleet got Z", t_new)
        self._publish(r)
        b = self.compose(repo=r)
        built = b["built"]
        self.assertIsNone(built["git_error"])
        self.assertIsNone(built["board"]["unavailable"])
        self.assertEqual(built["ref"], "origin/main")
        self.assertEqual(built["total"], 3)  # the ancient land stays out
        # expected grouping derived from the SAME stamps the fixture planted,
        # so a midnight boundary between t_new and t_mid cannot flake the test
        day = lambda t: time.strftime("%Y-%m-%d", time.gmtime(t))
        expect = []
        for t, subj in ((t_new, "land: just now — the fleet got Z"),
                        (t_mid, "land: earlier — the fleet got Y"),
                        (t_old, "land: yesterday — the fleet got X")):
            if not expect or expect[-1][0] != day(t):
                expect.append([day(t), []])
            expect[-1][1].append(subj)
        self.assertEqual([[d["day"], [l["subject"] for l in d["lands"]]]
                          for d in built["days"]], expect)
        # shas come from git itself, never typed
        shas = subprocess.run(
            ("git", "log", "origin/main", "--first-parent", "-3", "--pretty=%h"),
            cwd=r, capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual([l["sha"] for d in built["days"] for l in d["lands"]],
                         shas)
        text = brief.render(b)
        self.assertIn("WHAT WE BUILT — 3 lands on origin/main (last 48h) · "
                      "no open land loops", text)
        self.assertIn("    %s  land: just now — the fleet got Z" % shas[0], text)
        self.assertIn("  " + day(t_new), text)
        self.assertNotIn("ancient", text)

    def test_empty_window_is_an_honest_line_never_an_absent_section(self):
        r = self._repo()
        self._land(r, "land: ancient — before the gauge window",
                   self.now - 100 * 3600)
        self._publish(r)
        b = self.compose(repo=r)
        self.assertEqual(b["built"]["total"], 0)
        self.assertIsNone(b["built"]["git_error"])
        self.assertIn("WHAT WE BUILT — nothing landed on origin/main in the "
                      "last 48h", brief.render(b))

    def test_unreadable_git_says_so(self):
        b = self.compose(repo=self.j("norepo"))  # never created, no repo above
        built = b["built"]
        self.assertTrue(built["git_error"])
        self.assertEqual((built["total"], built["days"]), (0, []))
        text = brief.render(b)
        self.assertIn("WHAT WE BUILT — trunk unreadable", text)
        self.assertIn(built["git_error"], text)  # the reason reaches the owner

    def test_board_unavailable_is_named_even_when_it_raises(self):  # noqa: VACUOUS_ASSERTION — positive controls bind the named reason into board dict AND render; mutation #3 (passthrough->None) reddens exactly this arm
        r = self._repo()
        self._land(r, "land: one real change", self.now - 600)
        self._publish(r)
        reason = "the dispatch ledger is unreadable at row 3"
        with mock.patch("helm.landreq.board_section",
                        return_value={"title": "LAND LOOPS",
                                      "unavailable": reason,
                                      "loops": [], "stalled": []}):
            b = self.compose(repo=r)
        self.assertEqual(b["built"]["board"]["unavailable"], reason)
        text = brief.render(b)
        self.assertIn("board unavailable — " + reason, text)
        self.assertIn("land: one real change", text)  # git leg still renders
        with mock.patch("helm.landreq.board_section",
                        side_effect=RuntimeError("boom")):
            b = self.compose(repo=r)
        self.assertIn("board projection raised: boom",
                      b["built"]["board"]["unavailable"])

    def test_board_counts_ride_the_headline(self):
        r = self._repo()
        self._land(r, "land: one real change", self.now - 600)
        self._publish(r)
        with mock.patch("helm.landreq.board_section",
                        return_value={"title": "LAND LOOPS", "unavailable": None,
                                      "loops": [{"id": "a"}, {"id": "b"}],
                                      "stalled": [{"id": "b"}]}):
            b = self.compose(repo=r)
        self.assertEqual(b["built"]["board"], {"loops": 2, "stalled": 1,
                                               "unavailable": None})
        self.assertIn("2 open land loops, 1 stalled", brief.render(b))

    def test_more_lands_fold_honestly(self):
        r = self._repo()
        for i in range(brief.MAX_BUILT + 3):
            self._land(r, "land: change %02d" % i, self.now - 600)
        self._publish(r)
        b = self.compose(repo=r)
        self.assertEqual(b["built"]["total"], brief.MAX_BUILT + 3)
        text = brief.render(b)
        self.assertEqual(text.count("land: change"), brief.MAX_BUILT)
        self.assertIn("(+3 more lands)", text)


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
        self.assertEqual(sorted(b), ["built", "generated_at", "hours", "inject",
                                     "knowledge", "review", "seats", "sessions",
                                     "waiting"])
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
