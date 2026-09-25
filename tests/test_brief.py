#!/usr/bin/env python3
"""brief tests — hermetic: tmp HELM_HOME + HELM_ADOPTED_DIR, catalog roots and
caches repointed at tmp dirs (scanner path forced, cv never invoked), the
usage-history read repointed at a tmp file, the cwd moved into the tmp dir so
the WHAT WE BUILT gauge never reads the real checkout. The real ~/.helm,
~/.claude and ~/.cache are never read or written."""
import calendar
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
            "HELM_BOARD": j("integration-board.json"),
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

        def big(i):
            # a WHOLE line of exactly LINE_CAP bytes: a cut line stops short of
            # the cap by the width of the route the lane footer carries
            # (inject._entries._cut), so only an uncut line fills it exactly
            return "X" * (inject.LINE_CAP - len("PREMISE %s: " % i))
        planted = []
        for n in range(fill):
            i = "p-fill-%d" % n
            store.write_prior({"id": i, "statement": big(i),
                               "confidence": "1.0", "pin": "true"})
            planted.append(i)
        store.write_prior({"id": "p-late", "statement": big("p-late"),
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

    def test_a_member_unproven_row_retires_every_older_gauged_row(self):
        """task/2981. Before the member join, a Team member's row was gauged
        from a SIBLING's pool file. After it, that member's probe reads
        pool-member-unknown and carries no gauges, so this headline skipped
        it and kept printing the sibling's number. A member-unknown row is
        the member-correct answer: every gauged row older than it is
        retired, and the seat reads UNKNOWN until a newer gauged row."""
        hen = "hen@team.example"
        rows = [
            {"provider": "codex", "account": hen,
             "probed_at": _iso(self.now - 7200), "status": "allowed",
             "primary": "7d", "gauges": _gauges(0.26)},
            {"provider": "codex", "account": hen,
             "probed_at": _iso(self.now - 3600),
             "status": "pool-member-unknown"},
            # a later gaugeless row does not revive the retired reading
            {"provider": "codex", "account": hen,
             "probed_at": _iso(self.now - 1800), "status": "network-error"},
        ]
        self.plant_history(rows)
        seats = self.compose()["seats"]
        self.assertEqual([(s["account"], s["headroom_pct"]) for s in seats],
                         [(hen, None)])
        text = brief.render(self.compose())
        self.assertIn("UNKNOWN", text)
        self.assertIn("member unproven", text)
        self.assertNotIn("74%", text)
        # THE CONTROL: once the member's own file is pooled, a NEWER gauged
        # row is the member-correct one, and the seat reads it
        self.plant_history(rows + [
            {"provider": "codex", "account": hen, "probed_at": _iso(self.now),
             "status": "allowed", "primary": "7d", "gauges": _gauges(0.95)}])
        seats = self.compose()["seats"]
        self.assertEqual([(s["account"], s["headroom_pct"]) for s in seats],
                         [(hen, 5.0)])

    def test_an_unread_gauge_is_unknown_headroom_and_its_sibling_survives(self):
        """task/2935: the probe writes a None utilization for a limit the
        vendor sent with no percent. `100 - None * 100` raised and took every
        seat row down with it; the unread seat's headroom is UNKNOWN, never
        100% and never 0%."""
        other = "bob@example.com"
        self.plant_history([
            {"provider": "anthropic", "account": ACCT, "probed_at": _iso(self.now),
             "status": "allowed", "primary": "5h", "gauges": _gauges(None)},
            {"provider": "anthropic", "account": other, "probed_at": _iso(self.now),
             "status": "allowed", "primary": "5h", "gauges": _gauges(0.6)},
        ])
        seats = {s["account"]: s for s in self.compose()["seats"]}
        self.assertEqual(seats[other]["headroom_pct"], 40.0)
        self.assertIsNone(seats[ACCT]["headroom_pct"])
        self.assertEqual(seats[ACCT]["window"], "5h")

    def test_no_cache_says_run_creds(self):
        b = self.compose()
        self.assertEqual(b["seats"], [])
        self.assertIn("quota: run `helm creds`", brief.render(b))


class WaitingOnYouTest(BriefBase):
    def test_timestamp_parser_keeps_the_board_minute_shape(self):
        midnight = brief._ts_epoch("2026-08-04")
        self.assertEqual(brief._ts_epoch("2026-08-04T19:15Z") - midnight,
                         19 * 3600 + 15 * 60)
        self.assertEqual(brief._ts_epoch("2026-08-04T19:15:38Z") - midnight,
                         19 * 3600 + 15 * 60 + 38)
        self.assertEqual(pk.parse_ts_epoch("2026-08-04T19:15Z"),
                         midnight + 19 * 3600 + 15 * 60)
        self.assertEqual(brief._ts_epoch("not-a-stamp"), 0)
        self.assertIsNone(pk.parse_ts_epoch("not-a-stamp"))  # noqa: VACUOUS_ASSERTION — the same parser is positively controlled immediately above with the minute-only shape; this sibling assertion proves refusal rather than an inert return

    def test_fresh_estate_waits_on_the_interview(self):
        b = self.compose()
        self.assertIsNone(b["owner_asks_unavailable"])
        items = b["waiting"]
        self.assertTrue(any("helm interview" in it for it in items))

    def test_present_unreadable_owner_queue_is_unknown_not_empty(self):
        pk.write_json(whoami.profile_path(), {"schema_version": 1,
                                              "interview_status": "done"})
        with open(os.environ["HELM_BOARD"], "w") as f:
            f.write("not-json")
        b = self.compose()
        self.assertEqual(b["owner_asks"], [])
        self.assertIn("unreadable", b["owner_asks_unavailable"])
        text = brief.render(b)
        self.assertIn("FLEET-FILED OWNER ASKS — UNKNOWN", text)
        self.assertNotIn("quiet —", text)

    def test_alien_owner_queue_shape_is_unknown_not_empty(self):
        pk.write_json(whoami.profile_path(), {"schema_version": 1,
                                              "interview_status": "done"})
        pk.write_json(os.environ["HELM_BOARD"], {"owner_gated_queue": "done"})
        b = self.compose()
        self.assertEqual(b["owner_asks"], [])
        self.assertIn("not a list", b["owner_asks_unavailable"])
        self.assertIn("FLEET-FILED OWNER ASKS — UNKNOWN", brief.render(b))

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
        self.assertIn("ESTATE HEALTH — 3", text)

    def test_fleet_filed_asks_are_oldest_first_aged_and_visually_distinct(self):
        pk.write_json(whoami.profile_path(), {"schema_version": 1,
                                              "interview_status": "done"})
        pk.write_json(os.environ["HELM_BOARD"], {"owner_gated_queue": [
            {"ask": "decide the chart writer", "why": "choose one writer",
             "state": "waiting-owner-decision", "hold_kind": "decision",
             "since": "2026-08-26T09:14:38Z"},
            {"ask": "codex relogin", "why": "device auth expired " + "x" * 200,
             "state": "waiting-owner", "hold_kind": "owner-input", "since": "2026-08-04T19:15Z"},
            {"ask": "informational only", "why": "no change requested",
             "state": "fyi", "since": "2026-08-01T00:00:00Z"},
        ]})
        now = calendar.timegm(time.strptime("2026-08-26T09:30:00Z",
                                            "%Y-%m-%dT%H:%M:%SZ"))
        with mock.patch.object(brief.time, "time", return_value=now):
            b = self.compose()
            text = brief.render(b)
        self.assertEqual([r["ask"] for r in b["owner_asks"]],
                         ["codex relogin", "decide the chart writer"])
        self.assertEqual(b["waiting"], [])
        self.assertIn("FLEET-FILED OWNER ASKS — 2", text)
        self.assertIn("21d ago · codex relogin — device auth expired", text)
        self.assertIn("15m ago · decide the chart writer — choose one writer", text)
        ask_lines = [line for line in text.splitlines() if "ago ·" in line]
        self.assertTrue(ask_lines)
        self.assertLessEqual(max(map(len, ask_lines)), 128)
        self.assertNotIn("informational only", text)
        self.assertNotIn("ESTATE HEALTH", text)

    def test_malformed_owner_ask_age_is_unknown_not_ancient(self):
        pk.write_json(whoami.profile_path(), {"schema_version": 1,
                                              "interview_status": "done"})
        pk.write_json(os.environ["HELM_BOARD"], {"owner_gated_queue": [
            {"ask": "repair the timestamp", "why": "row is malformed",
             "state": "waiting-owner", "hold_kind": "owner-input", "since": "broken"},
            {"ask": "repair the future timestamp", "why": "row is impossible",
             "state": "waiting-owner", "hold_kind": "owner-input", "since": "2099-01-01T00:00:00Z"},
        ]})
        text = brief.render(self.compose())
        self.assertIn("age unknown · repair the timestamp", text)
        self.assertIn("age unknown · repair the future timestamp", text)
        self.assertNotIn("20000d", text)
        self.assertNotIn("0m ago", text)

    def test_a_STALE_CHAT_JOURNAL_reaches_the_owner_MORNING_BRIEF(self):
        """The fact existed on the filesystem for eight hours and no surface he
        reads carried it. `helm doctor` carries it now, but doctor is a verb
        somebody has to decide to run; the brief is the one he already reads."""
        from helm import chat
        eight_hours_ago = time.time() - 8 * 3600
        pk.write_json(chat._flush_health_path(),
                      {"state": "ok", "streak": 0, "last_ok": eight_hours_ago,
                       "quarantined": [], "stranded_rows": 0})
        stalled = self.compose()["waiting"]
        self.assertTrue(any("chat journal 8h behind" in it for it in stalled),
                        stalled)
        self.assertTrue(any("RAM only" in it for it in stalled), stalled)
        # POSITIVE CONTROL ON THE SAME OBSERVABLE: with the journal fresh the
        # line must be GONE while the list itself still carries items, or this
        # arm would pass just as well against a `waiting` list that broke.
        pk.write_json(chat._flush_health_path(),
                      {"state": "ok", "streak": 0, "last_ok": time.time(),
                       "quarantined": [], "stranded_rows": 0})
        fresh = self.compose()["waiting"]
        self.assertTrue(any("helm interview" in it for it in fresh), fresh)
        self.assertFalse(any("chat journal" in it for it in fresh), fresh)

    def test_a_QUARANTINED_room_is_NAMED_in_the_brief(self):
        """A quarantined room stays undurable until somebody repairs it — there
        is a detector and no cure today — so the owner surface must keep saying
        its name rather than reporting the flush as healthy."""
        from helm import chat
        pk.write_json(chat._flush_health_path(),
                      {"state": "degraded", "streak": 1, "last_ok": time.time(),
                       "quarantined": ["meld-1785274962-mute-backlog"],
                       "stranded_rows": 3})
        items = self.compose()["waiting"]
        named = [it for it in items if "meld-1785274962-mute-backlog" in it]
        self.assertEqual(len(named), 1, items)
        self.assertIn("undurable", named[0])

    def test_an_operator_DISABLE_is_stated_as_the_exposure_it_is(self):
        """HELM_CHAT_LOG=0 is a choice and still means no durable copy at all,
        which a morning brief must say rather than render as silence."""
        from helm import chat
        pk.write_json(chat._flush_health_path(),
                      {"state": "ok", "streak": 0, "last_ok": time.time(),
                       "quarantined": [], "stranded_rows": 0})
        with mock.patch.dict(os.environ, {"HELM_CHAT_LOG": "0"}):
            off = self.compose()["waiting"]
        self.assertTrue(any("log-flush DISABLED" in it for it in off), off)
        # POSITIVE CONTROL ON THE SAME OBSERVABLE: back on, the same list is
        # still produced and simply no longer carries the exposure line.
        on = self.compose()["waiting"]
        self.assertTrue(any("helm interview" in it for it in on), on)
        self.assertFalse(any("log-flush DISABLED" in it for it in on), on)


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
        board = b["built"]["board"]
        self.assertEqual((board["loops"], board["stalled"],
                          board["unavailable"]), (2, 1, None))
        # The bound's own disclosure travels with the counts.
        self.assertIn("undecided", board)
        self.assertEqual(board["budget_s"], brief.BRIEF_DERIVE_BUDGET_S)
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
                                     "knowledge", "owner_asks",
                                     "owner_asks_unavailable", "review", "seats",
                                     "sessions", "waiting"])
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
        self.plant_history([{"provider": "anthropic", "account": "u%d@x.example" % i,
                             "probed_at": _iso(self.now), "status": "allowed",
                             "primary": "5h", "gauges": _gauges(0.5)} for i in range(8)])
        self.plant_ledger([{"v": 1, "ts": _iso(self.now - 60),
                            "fired": {"pinned": ["p1"], "jit": [], "reflex": []}}])
        pk.write_json(os.environ["HELM_BOARD"], {"owner_gated_queue": [
            {"ask": "owner ask %d" % i, "why": "owner input %d" % i,
             "state": "waiting-owner", "hold_kind": "owner-input", "since": _iso(self.now - i * 3600)}
            for i in range(8)]})
        rc, out = self._run([])
        self.assertEqual(rc, 0)
        self.assertIn("SINCE YOU LEFT", out)
        self.assertIn("FLEET-FILED OWNER ASKS — 8", out)
        self.assertLessEqual(len(out.strip().splitlines()), 40)


def setUpModule():
    """No dispatch row this module writes walks the host's process table
    (task/3039; see tests._tmphome.pin_live_seats)."""
    from tests._tmphome import pin_live_seats
    pin_live_seats()


if __name__ == "__main__":
    unittest.main()


class TheBoardCallIsBoundedTest(BriefBase):
    """The brief reduces the board to two integers, so it must not pay a
    reader-who-finishes price to compute them.

    These arms drive the shipped `_built` with the REAL landreq module and
    assert on what it does to the projection, not on a re-description of it.
    """

    def test_the_board_is_projected_inside_a_scope_with_the_briefs_budget(self):  # noqa: ORPHANED_MOCK — the double DOES fire and the arm asserts on state only it can set: `seen` is populated inside board_section, so every assertion below is a PRESENCE claim that fails if the double never ran. The walker modelled the graph from _derive_expired rather than from _built, which is the entry point this arm actually drives.
        """THE DEFECT THIS FIXES IS THAT THE BOUND COULD NOT FIRE. The derive
        deadline is memoised PER PROJECTION SCOPE; with no scope open the memo
        recomputes a fresh deadline every lookup and the door always answers
        False. So a scope being open is not decoration here — it is the whole
        difference between a guard and an inert call.
        """
        from helm import landreq, projscope
        seen = {}

        def board_section(now=None):
            seen["active"] = projscope.active()
            # The deadline must be STABLE across lookups inside the
            # projection; two different answers means no scope is memoising it.
            seen["stable"] = (landreq._derive_expired() is
                              landreq._derive_expired())
            return {"title": "LAND LOOPS", "unavailable": None,
                    "loops": [], "stalled": []}

        armed = []
        with mock.patch.object(landreq, "board_section", board_section), \
                mock.patch.object(landreq, "arm_derive_budget",
                                  side_effect=lambda s: armed.append(s)):
            brief._built(repo=self.tmp)
        self.assertEqual(armed, [brief.BRIEF_DERIVE_BUDGET_S],
                         "the brief did not arm its own derive budget")
        self.assertTrue(seen.get("active"),
                        "board_section ran outside a projection scope, where "
                        "the derive bound cannot fire")
        self.assertTrue(seen.get("stable"))

    def test_the_budget_is_the_hooks_number_not_the_boards(self):
        """A brief that charged the board reader's budget to a 10s hook is the
        defect, not a smaller version of it. The two must not be equal."""
        from helm import landreq
        self.assertLess(brief.BRIEF_DERIVE_BUDGET_S,
                        landreq.BOARD_DERIVE_BUDGET_S)
        self.assertGreater(brief.BRIEF_DERIVE_BUDGET_S, 0)

    def test_only_two_integers_survive_the_board(self):  # noqa: VACUOUS_ASSERTION — the must-miss asserts three specific strings are ABSENT from a rendered brief whose board was given RICH rows carrying them, and the preceding assertEqual on loops/stalled/unavailable is the unconditional positive that the render ran at all
        """WHY BOUNDING IS SAFE, stated as a property rather than as a promise:
        every landing proof the projection derives is discarded here, so a row
        that could not be derived costs the brief nothing it keeps."""
        from helm import landreq
        rich = {"title": "LAND LOOPS", "unavailable": None,
                "loops": [{"id": "a", "proof": "ANCESTOR", "tip": "deadbeef"},
                          {"id": "b", "proof": "UNKNOWN", "tip": "cafe"}],
                "stalled": [{"id": "b", "why": "no reviewer"}]}
        with mock.patch.object(landreq, "board_section", return_value=rich):
            built = brief._built(repo=self.tmp)
        self.assertEqual((built["board"]["loops"], built["board"]["stalled"],
                          built["board"]["unavailable"]), (2, 1, None))
        # MUST-MISS: no proof, tip or reason reaches the brief, so deriving
        # them was work the brief threw away.
        self.assertNotIn("ANCESTOR", json.dumps(built))
        self.assertNotIn("deadbeef", json.dumps(built))
        self.assertNotIn("no reviewer", json.dumps(built))

    def test_a_raising_board_still_yields_a_section(self):
        """Unchanged by this lane and pinned because the bound introduces a new
        way for the projection to end early: the gauge going silent is
        indistinguishable from 'nothing shipped', the one lie it exists to
        prevent."""
        from helm import landreq
        with mock.patch.object(landreq, "board_section",
                               side_effect=RuntimeError("projection exploded")):
            built = brief._built(repo=self.tmp)
        self.assertEqual(built["board"]["loops"], 0)
        self.assertIn("projection exploded", built["board"]["unavailable"])


class TheBoundDisclosesWhatItCouldNotDeriveTest(unittest.TestCase):
    """THE ARMS THAT REPLACE A MOCKED board_section.

    A mocked board returns whatever counts the author chose, so it can never
    show what a BUDGET does to them — the only question this bound raises.
    These drive the REAL landreq projection with nothing varied but the derive
    budget.

    THEY READ THE REPOSITORY THIS LEDGER TRACKS rather than a synthetic one,
    and that is deliberate rather than lazy: `dispatches.send` REFUSES a ref
    from any other repository ("a row filed here would be invisible to everyone
    who can act on it"), so a tmp-repo fixture cannot hold land-request rows at
    all — measured, the refusal names both paths. A projection over zero rows
    would make every assertion below vacuous, which the first arm checks for
    explicitly instead of passing quietly.
    """

    def _board(self, budget):
        from helm import landreq, projscope
        with projscope.scope():
            landreq.arm_derive_budget(budget)
            b = landreq.board_section()
        # THE COUNT COMES OFF THE BOARD, NOT RECOUNTED FROM THE TWO LISTS.
        # Recounting here was the defect: `loops` and `stalled` overlap and
        # both withhold, so a count over them is neither unique nor complete.
        return {"rows": len(b["loops"]) + len(b["stalled"]),
                "undecided": b["undecided"],
                "loops": len(b["loops"]), "stalled": len(b["stalled"])}

    def test_a_starved_budget_never_decides_MORE_than_a_generous_one(self):  # noqa: VACUOUS_ASSERTION — the zero-row branch asserts an absence BECAUSE the gate's snapshot ledger holds no land requests, which the comment states and the inequality above covers whenever rows exist; measured on the live ledger the same call yields 37 undecided at 1s against 28 unbounded
        """THE PROPERTY THE BOUND OWES, and the only one a sample can support.

        Its whole effect is to derive LESS, so undecided must be monotone in
        the budget. This does NOT claim the counts are budget-invariant — that
        claim was refuted: a shorter budget leaves rows underived, and an
        underived row can move between the open and stalled sets.
        """
        generous = self._board(30.0)
        starved = self._board(0.001)
        self.assertGreaterEqual(starved["undecided"], generous["undecided"])
        # NO IMPLICATION RUNS FROM `rows` TO `undecided`, AND ASSERTING ONE
        # HERE WOULD REDDEN ON CORRECT BEHAVIOUR. "Both rendered lists empty
        # therefore undecided is 0" holds only where the count is computed
        # FROM those lists; it is computed from the projected population,
        # which retains rows the lists withhold. A row that is UNKNOWN,
        # terminal or relieved yields loops 0, stalled 0 and undecided 1.
        #
        # What is left is the monotonicity above, which is the real property,
        # and the independence itself is proven with a built population by
        # TheCountFollowsThePopulationThroughTheRealBriefTest below — not
        # asserted here over whatever the environment happens to hold.
        self.assertIsInstance(generous["undecided"], int)
        self.assertGreaterEqual(generous["undecided"], 0)

    def test_the_rendered_line_declares_a_partial_projection(self):
        """AND IT MUST REACH THE READER. A count computed over rows nothing
        could derive, printed as an exact number, is the defect the bound
        introduces if it is silent."""
        b = brief.compose()
        b["built"]["board"] = dict(b["built"]["board"], undecided=4, loops=2,
                                   stalled=1, budget_s=1.0, unavailable=None)
        out = brief.render(b)
        self.assertIn("undecided", out)
        self.assertIn("may move", out)

    def test_a_fully_decided_board_says_nothing_extra(self):
        """MUST-MISS. The disclosure appears only when something was left
        undecided, or it is noise on every brief and stops being read."""
        b = brief.compose()
        b["built"]["board"] = dict(b["built"]["board"], undecided=0, loops=2,
                                   stalled=1, unavailable=None)
        out = brief.render(b)
        self.assertNotIn("undecided", out)
        self.assertIn("2 open land loops", out)


class TheUndecidedCountIsOverThePopulationTest(unittest.TestCase):
    """WHICH COLLECTION THE COUNT IS TAKEN FROM, asked three ways.

    The counted set must be the PROJECTED POPULATION, not the two rendered
    lists. Those lists are two predicates over one population: they OVERLAP (a
    stalled row is a non-terminal loop past its threshold), so a sum across
    them bills a row twice; and they each WITHHOLD (foreign, terminal,
    relieved), so an undecided row can be in neither while still being a row
    the reader failed to decide.

    WHAT THESE FIXTURES ARE AND ARE NOT. They supply `lrs` and the two row
    lists directly, which means they do NOT prove that a real projection
    produces overlapping or withheld rows — the shipped `_loop_rows` and
    `_stalled_rows` establish that, and are read, not re-implemented here. What
    they do prove is the only thing in dispute: given a population and two
    lists that disagree with it, `board_section` answers from the population.
    Each arm asserts THE FIXTURE IS THE HAZARD first, so it cannot pass on a
    build where the lists happen to agree with the population anyway.
    """

    def _lr(self, rid, state):
        return {"id": rid, "land_state": state}

    def _section(self, lrs, loops, stalled):
        from helm import landreq
        with mock.patch.object(landreq, "project_raw",
                               return_value=(lrs, {}, None)), \
                mock.patch.object(landreq, "_loop_rows",
                                  side_effect=lambda *a, **k: loops), \
                mock.patch.object(landreq, "_stalled_rows",
                                  side_effect=lambda *a, **k: stalled), \
                mock.patch.object(landreq, "card", side_effect=lambda lr: lr):
            return landreq.board_section()

    def test_a_row_in_BOTH_lists_is_counted_ONCE(self):
        both = self._lr("b", "UNKNOWN")
        lrs = {"a": self._lr("a", "LANDED"), "b": both}
        # THE FIXTURE IS THE HAZARD: the undecided row is in both lists, so a
        # count summed across them reads 2 for one row.
        section = self._section(lrs, [lrs["a"], both], [both])
        self.assertEqual(
            sum(1 for r in section["loops"] + section["stalled"]
                if r["land_state"] == "UNKNOWN"), 2,
            "the fixture no longer double-bills, so this arm proves nothing")
        self.assertEqual(section["undecided"], 1)

    def test_a_row_in_NEITHER_list_is_still_counted(self):
        hidden = self._lr("h", "UNKNOWN")
        lrs = {"a": self._lr("a", "LANDED"), "h": hidden}
        # THE FIXTURE IS THE HAZARD: the only undecided row was withheld from
        # both rendered lists, so a count over them reads 0.
        section = self._section(lrs, [lrs["a"]], [])
        self.assertEqual(
            sum(1 for r in section["loops"] + section["stalled"]
                if r["land_state"] == "UNKNOWN"), 0,
            "the fixture no longer withholds, so this arm proves nothing")
        self.assertEqual(section["undecided"], 1)

    def test_a_FOREIGN_undecided_row_is_not_this_boards_uncertainty(self):
        """SCOPED THE WAY THE LISTS ARE. `project_raw` MARKS foreign rows
        rather than dropping them, so counting the raw population would report
        this board as uncertain about work it does not owe."""
        theirs = dict(self._lr("t", "UNKNOWN"), foreign=True)
        lrs = {"t": theirs, "a": self._lr("a", "LANDED")}
        # THE FIXTURE IS THE HAZARD: the population's only undecided row
        # belongs to another project.
        self.assertEqual(
            sum(1 for lr in lrs.values()
                if lr["land_state"] == "UNKNOWN"), 1,
            "the fixture has no foreign undecided row, so this proves nothing")
        self.assertEqual(self._section(lrs, [lrs["a"]], [])["undecided"], 0)

    def test_an_UNKNOWN_PROVENANCE_undecided_row_IS_counted(self):
        """THE OTHER DIRECTION, and the one a stricter predicate would get
        wrong. A row whose project cannot be resolved is not a row this board
        has proven it does not owe, so it stays counted — the same choice
        `_this_boards_row` makes for the rendered lists, and the reason this
        uses the VISIBILITY predicate rather than `_observation_owned`."""
        unresolved = dict(self._lr("u", "UNKNOWN"), project_unresolved=True)
        lrs = {"u": unresolved}
        self.assertEqual(self._section(lrs, [], [])["undecided"], 1)

    def test_an_UNAVAILABLE_projection_reports_None_not_zero(self):
        from helm import landreq
        with mock.patch.object(landreq, "project_raw",
                               return_value=({}, {}, "ledger unreadable")):
            section = landreq.board_section()
        # UNCONDITIONAL POSITIVE CONTROL on the same observable, so an
        # assertion about an ABSENT value cannot pass over a section this call
        # never built. A missing key would read None too.
        self.assertIn("undecided", section)
        self.assertEqual(section["unavailable"], "ledger unreadable")
        self.assertEqual((section["loops"], section["stalled"]), ([], []))
        self.assertIsNone(section["undecided"],
                          "a refused projection decided nothing, which is not "
                          "the same fact as having decided everything")

    def test_an_UNTRUSTWORTHY_CHAIN_keeps_the_count_it_had_measured(self):
        """The population was projected before the fold refused, so the number
        is real and rides out with the refusal."""
        from helm import landreq
        lrs = {"a": self._lr("a", "UNKNOWN"), "b": self._lr("b", "LANDED")}
        boom = landreq._ChainUntrustworthy("partial fold")
        with mock.patch.object(landreq, "project_raw",
                               return_value=(lrs, {}, None)), \
                mock.patch.object(landreq, "_loop_rows", side_effect=boom):
            section = landreq.board_section()
        self.assertIn("partial fold", section["unavailable"])
        self.assertEqual(section["undecided"], 1)


class TheCaveatSurvivesAnEmptyBoardTest(BriefBase):
    """THE READING WHERE THE DISCLOSURE MATTERS MOST IS THE ONE IT WAS MISSING.

    At zero loops an undecided row changes the ANSWER rather than the
    magnitude: a reader told there is nothing in flight stops looking, and the
    rows the brief could not decide are exactly the ones that might be.
    """

    def _render(self, **board):
        b = brief.compose(repo=self.tmp)
        b["built"]["board"] = dict(b["built"]["board"], unavailable=None,
                                   budget_s=1.0, **board)
        return brief.render(b)

    def test_zero_loops_with_undecided_rows_SAYS_SO(self):
        out = self._render(loops=0, stalled=0, undecided=3)
        self.assertIn("no open land loops", out)
        self.assertIn("3 undecided", out)
        self.assertIn("may move", out)

    def test_zero_loops_with_nothing_undecided_stays_quiet(self):
        """MUST-MISS, so the disclosure is not simply always printed."""
        out = self._render(loops=0, stalled=0, undecided=0)
        self.assertIn("no open land loops", out)
        self.assertNotIn("undecided", out)

    def test_a_None_count_is_not_rendered_as_a_number(self):
        """An unavailable projection carries undecided=None; formatting that
        into the line would print 'None undecided'."""
        b = brief.compose(repo=self.tmp)
        b["built"]["board"] = dict(b["built"]["board"], loops=0, stalled=0,
                                   undecided=None,
                                   unavailable="ledger unreadable")
        out = brief.render(b)
        self.assertIn("board unavailable", out)
        self.assertNotIn("undecided", out)
        self.assertNotIn("None", out.split("WHAT WE BUILT")[-1].split("\n")[0])


class TheCountFollowsThePopulationThroughTheRealBriefTest(BriefBase):
    """THE DISCLOSURE, DRIVEN THROUGH THE REAL PATH TO THE REAL READER.

    The arms above this one reach the board by replacing `project_raw` and both
    reducers and then overwriting the rendered board, so they prove the
    ARITHMETIC and never that underivation travels from a projection to a line
    a human reads. A reviewer named that exactly: a fixture that invents its
    input. These arms invent nothing. A real git repository, real rows written
    through `dispatches.add`, the real projection, the real `_built`, the real
    `render`.

    THE POPULATION IS BUILT TO DISAGREE WITH THE LISTS, because that
    disagreement IS the cure. A chain whose predecessor is absorbed by a
    descendant is folded out of the rendered loops — showing both would
    double-bill one chain — while the projection keeps it. Measured on this
    fixture: five rows, FOUR undecided in the population, THREE undecided
    across the two rendered lists. Every arm asserts that gap exists before
    asserting what the brief does with it, so none can pass on a build where
    the lists happen to agree with the population.
    """

    def setUp(self):
        super().setUp()
        from tests._tmphome import pin_dispatch_home
        from helm import dispatches
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        for a in (("init", "-q"), ("config", "user.email", "t@example.com"),
                  ("config", "user.name", "T")):
            self.git(*a)
        self.main = self.git("symbolic-ref", "--short", "HEAD")
        self.a = self.commit("a")
        self.git("branch", "side", self.a)
        self.b = self.commit("b")
        self.git("checkout", "-q", "side")
        self.side = self.commit("side", path="g")
        self.git("checkout", "-q", self.main)
        # THE WRITE DOOR NEEDS AN AUTHOR AND A ROOM. BriefBase supplies
        # neither, and `dispatches.add` answers a refusal with None rather
        # than an exception — so without these the fixture builds an EMPTY
        # population and every arm below becomes true of nothing.
        #
        # RESTORE THESE THREE KEYS AND NOTHING ELSE. A NESTED patch.dict IS
        # THE TRAP HERE, and it is worse than the bare assignment it would
        # replace. `patch.dict.stop()` restores THE WHOLE MAPPING to the
        # snapshot it took at start(), and unittest runs tearDown BEFORE
        # addCleanup callbacks — so BriefBase's own patch (HELM_CATALOG,
        # GIT_CONFIG_GLOBAL and friends) stops FIRST, and a second patch
        # stopped after it RESURRECTS every value the first one had just
        # removed. Measured: a key set only by the outer patch reads its
        # patched value again after BOTH have stopped.
        #
        # That leak is silent and it travels: HELM_CATALOG=scanner makes
        # `catalog._build_from_cv` return None for every later module, and
        # GIT_CONFIG_GLOBAL=/dev/null makes a fresh fixture repo's `git
        # commit` exit 128 with no author identity. Both were observed, in
        # test_cv_roots and test_dispatches, from exactly this shape.
        for key, value in (("HELM_CHAT_DIR", os.path.join(self.tmp, "chat")),
                           ("HELM_CHAT_NODE_URL", ""),
                           ("HELM_CHAT_NAME", "integrator")):
            self.addCleanup(self._restore_env, key, os.environ.get(key))
            os.environ[key] = value
        pin_dispatch_home(self, self.repo)

        def add(lane, ref, **kw):
            row = dispatches.add("seat-a", lane, ref=ref, repo=self.repo,
                                 notify=False, kind="review", **kw)
            # CHECKED HERE, NOT LATER. A refusal three lines down surfaces as
            # a TypeError on None and reads like a bug in the arm.
            self.assertIsNotNone(row, "dispatch door refused %s" % lane)
            return row

        self.plain = add("lane/one", self.side, new_work=True)
        # THE WITHHELD ONE. The child absorbs the parent's debt, so the
        # frontier fold drops the PARENT from the rendered loops while the
        # projection still holds it.
        self.parent = add("lane/chain", self.b, new_work=True)
        self.child = add("lane/chain", self.side, supersedes=self.parent["id"])

    @staticmethod
    def _restore_env(key, prior):
        """Put ONE key back, so the restore cannot reach a neighbour's."""
        if prior is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = prior

    def git(self, *args):
        return subprocess.run(["git", "-C", self.repo, *args],
                              capture_output=True, text=True,
                              check=True).stdout.strip()

    def commit(self, text, path="state"):
        with open(os.path.join(self.repo, path), "a", encoding="utf-8") as f:
            f.write(text + "\n")
        self.git("add", path)
        self.git("commit", "-q", "-m", text)
        return self.git("rev-parse", "HEAD")

    def _board(self, budget=1.0):
        from helm import landreq, projscope
        with projscope.scope():
            landreq.arm_derive_budget(budget)
            return landreq.board_section()

    @staticmethod
    def _naive(section):
        """A count over the two RENDERED LISTS — the wrong population.

        This is the control, not the measurement: the disclosure is only
        meaningful where this number disagrees with the population's.
        """
        return sum(1 for r in section["loops"] + section["stalled"]
                   if str(r.get("land_state") or "").upper() == "UNKNOWN")

    def test_the_population_and_the_lists_DISAGREE_on_this_real_fixture(self):
        """THE HAZARD, ESTABLISHED FIRST AND FROM REAL PRODUCERS.

        If this ever stops failing to agree, every arm below is testing
        nothing, and it should fail here rather than pass quietly there.
        """
        section = self._board()
        self.assertIsNone(section["unavailable"], section["unavailable"])
        self.assertGreater(section["undecided"], 0,
                           "the fixture produced no undecided rows")
        self.assertGreater(
            section["undecided"], self._naive(section),
            "the rendered lists no longer withhold an undecided row, so this "
            "fixture cannot show the difference the cure exists for")

    def test_the_brief_reports_the_POPULATION_count(self):
        section = self._board()
        built = brief._built(repo=self.repo)
        self.assertEqual(built["board"]["undecided"], section["undecided"])
        self.assertNotEqual(built["board"]["undecided"], self._naive(section))

    def test_a_row_is_counted_ONCE_even_though_it_sits_in_two_lists(self):
        """UNIQUENESS, over rows the projection actually produced.

        The count can never exceed the number of distinct undecided rows the
        projection holds, whatever the two lists do with them.
        """
        from helm import landreq, projscope
        with projscope.scope():
            landreq.arm_derive_budget(1.0)
            lrs, _raw, unavailable = landreq.project_raw()
        self.assertIsNone(unavailable)
        distinct = {rid for rid, lr in lrs.items()
                    if str(lr.get("land_state") or "").upper() == "UNKNOWN"
                    and not lr.get("foreign")}
        self.assertTrue(distinct, "no undecided rows in the population")
        self.assertEqual(self._board()["undecided"], len(distinct))

    def test_the_DISCLOSURE_REACHES_THE_READER_through_the_real_render(self):
        """END TO END, which is the arm the review said was missing.

        Real rows, real projection, real `_built`, real `render`, and the
        number a human reads.
        """
        built = brief._built(repo=self.repo)
        n = built["board"]["undecided"]
        self.assertGreater(n, 0, "nothing was undecided, so the line is silent")
        b = brief.compose(repo=self.repo)
        b["built"] = built
        out = brief.render(b)
        self.assertIn("%d undecided" % n, out)
        self.assertIn("may move", out)

    def test_a_generous_budget_over_a_NONEMPTY_population_still_discloses(self):
        """THE COLD CONTROL, and it asserts its own population first.

        A budget arm in an environment whose ledger holds no land requests is
        true of nothing, so the non-emptiness is checked rather than assumed.
        """
        generous, starved = self._board(30.0), self._board(0.001)
        self.assertGreater(generous["undecided"], 0)
        self.assertGreaterEqual(starved["undecided"], generous["undecided"])
