#!/usr/bin/env python3
"""The BURN BOARD (task/2975) — its one read, and the page that draws it.

Two halves, both RUN rather than grepped.

THE SERVER HALF builds a whole synthetic world in a temp helm home — a
registry with authored lights, a burn-flag snapshot written by the real fold,
a task ledger written by the real writer — and fakes only the two readings
that walk live fleet state (the roster and the land pipeline). Then it asks
`/api/board` what it joined. The world carries the three awkward cases the
owner's board must not smooth over: a project with no seats, a family with no
burn flag, and a section past its freshness limit.

THE BROWSER HALF lifts the board's renderers out of the page the server
actually assembles and runs them under node, the style of
tests/test_web.py RegistryWireReadersTest. It covers the capacity line (lanes
running and seats able to work, never as a fraction) and its no-data states, the row order, the
chips, each row's lanes, progress, repos and kanban, the owner's queue, what a
collapsed row carries on a phone, a stale section drawn as stale, the
burn-flag card, and the light setter still posting to /api/projects/state.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from unittest import mock

from tests.test_web_chat_client_runtime import _extract_fn  # noqa: E402
from tests.test_work import LandedWorld  # noqa: E402
from tests._ownerverbs import owner_verbs, view_markup  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import (burnflags, gatewindow, home, landreq, pk,  # noqa: E402
                  registry, repofacts, scheduler, tasks, vcs, web, web_board,
                  web_cache, web_ui_loader)
from helm import seat as seat_mod  # noqa: E402


NOW = time.time()

PROJECTS = ("alpha", "beta", "gamma", "delta", "epsilon")


def _seat(name, project, presence, family, **extra):
    row = {"seat": name, "project": project, "presence": presence,
           "runtime": {"family": family} if family else None,
           "runtime_verified": bool(family)}
    row.update(extra)
    return row


# THE ROSTER, as the cached report hands it over. `seat-b` is on alpha but
# ABSENT, so it is a seat of the project and spends nothing; `local-1` is live
# on a family the fold mints no flag for; `review-sa` is an ephemeral review
# agent the roster tags and every live surface hides; `stray` names a project
# the registry does not hold.
def _claim(project, lane, holder, live=True):
    return {"resource": "worktree:%s:%s" % (project, lane), "holder": holder,
            "liveness": "live" if live else "stale", "stale": not live}


# THE CLAIMS: two holders on one lane (one lane, not two), a lane on beta, and
# a dead holder's claim, which is not a running lane.
CLAIMS = [_claim("alpha", "lane-a", "seat-a"),
          _claim("alpha", "lane-a", "alpha-claude"),  # noqa: SEAT_NAME — a fixture seat named for its project, not a real seat
          _claim("beta", "lane-q", "kimi"),
          _claim("alpha", "lane-old", "seat-b", live=False)]

# EACH SEAT'S cwd is where its work resolves (`@name` is that project's
# checkout, filled in per test from the temp registry). `local-1` and
# `delta-seat` are live, usable and hold no claim, so each is ONE lane at work
# on its project; `seat-a` sits in delta's checkout but holds a claim on
# alpha, so it is counted by its claim and never twice; `stray` works in a
# directory no project claims, so it is counted nowhere and named unscoped.
ROSTER = {"room": "main", "roster_failed": False, "claims": CLAIMS, "seats": [
    _seat("alpha-claude", "alpha", "fresh", "claude",  # noqa: SEAT_NAME — the harness family word the roster records, not a seat
          last_seen=1800000200, cwd="@alpha"),
    _seat("seat-a", "alpha", "quiet", "codex", last_seen=1800000100,
          cwd="@delta"),
    _seat("local-1", "alpha", "fresh", "localllm", cwd="@alpha/src"),
    _seat("seat-b", "alpha", "absent", "codex", cwd="@delta"),
    _seat("review-sa", "alpha", "fresh", "codex", ephemeral=True,
          cwd="@delta"),
    _seat("kimi", "beta", "fresh", "kimi", cwd="@beta"),
    _seat("stray", "nowhere", "fresh", "codex", cwd="/nowhere/at/all"),
    _seat("delta-seat", None, "fresh", "codex", cwd="@delta/src"),
    # three seats that are up and still cannot take work: a walled family,
    # a paused beacon, and a family whose credit is RED
    _seat("walled-1", None, "fresh", "codex", availability="UNAVAILABLE",
          cwd="@delta"),
    _seat("paused-1", None, "fresh", "codex", beacon_paused=True,
          cwd="@delta"),
    _seat("grok-1", None, "fresh", "grok", cwd="@delta"),
]}


def _placed(roster, paths):
    """A copy of `roster` with each `@name` cwd pointed at that project's
    checkout in this test's temp registry."""
    out = json.loads(json.dumps(roster))
    for row in out["seats"]:
        cwd = row.get("cwd") or ""
        if cwd.startswith("@"):
            name, _sep, rest = cwd[1:].partition("/")
            row["cwd"] = os.path.join(paths[name], rest) if rest \
                else paths[name]
    return out


# WHAT A CARD CARRIES FOR THE ONE LAND BOARD (task/3585) off a pipeline row
# that names no task, holder, tip, gate or mark
_BARE = {"task": None, "holder": "unknown", "tip": None, "gate": "",
         "contrary": False, "stalled": False}


def _lr_body(read_age_s=4):
    """The land pipeline's body for a board scoped to alpha. Two loops, one of
    them HONORED (closed through succession, so not in flight). `read_at` is
    the instant its reading was taken, which `read_age_s` counts from."""
    return {
        "withheld": {"scope": "alpha", "scope_repo": "/x/.git",
                     "foreign": 0, "unresolved": 0, "by_project": {}},
        "read_age_s": read_age_s, "projected_age_s": read_age_s,
        "read_at": time.time() - read_age_s,
        "unavailable": None,
        "loops": [{"id": "r1", "lane": "lane-a", "honored": False},
                  {"id": "r2", "lane": "lane-b", "honored": True}],
        "building": {"total": 1, "unmeasured": 0, "unavailable": None,
                     "rows": [{"lane": "lane-c", "ahead": 2}]},
        "scheduler": {
            "unavailable": None, "owner_hold_count": 1,
            "owner_holds": [{"plain_title": "lane-a", "age_s": 600}],
            "groups": [{"label": "integrator", "count": 1,
                        "oldest_age_s": 600, "suc": {"total": 0},
                        "rows": [{"plain_title": "lane-a",
                                  "stage_class": "review", "age_s": 600}]}]},
        "recent_lands": {"rows": [{"lane": "lane-z", "task": "task/9",
                                   "age_s": 3600}],
                         "total": 1, "unavailable": None},
    }


def _lr_all_body(read_age_s=6):
    """The land pipeline over EVERY project: a foreign row carries its
    project in `foreign_project`, a row of the scope carries none, and a row
    whose repository no project claims is marked unresolved."""
    return {
        "all_projects": True,
        "withheld": {"scope": "alpha", "scope_repo": "/x/.git",
                     "foreign": 3, "unresolved": 1, "by_project": {"beta": 2}},
        "read_age_s": read_age_s, "projected_age_s": read_age_s,
        "read_at": time.time() - read_age_s,
        "unavailable": None,
        "loops": [{"id": "r1", "lane": "lane-a", "state": "AWAITING_REVIEW",
                   "honored": False},
                  {"id": "b1", "lane": "b-review", "state": "AWAITING_REVIEW",
                   "honored": False, "foreign_project": "beta"},
                  {"id": "b2", "lane": "b-ready", "state": "READY",
                   "honored": False, "foreign_project": "beta"},
                  {"id": "b3", "lane": "b-done", "state": "REVIEWED",
                   "honored": True, "foreign_project": "beta"},
                  {"id": "u1", "lane": "u-lane", "state": "OPEN",
                   "honored": False, "project_unresolved": True},
                  {"id": "x1", "lane": "x-lane", "state": "OPEN",
                   "honored": False, "foreign_project": "not-registered"}],
        "recent_lands": {"rows": [
            {"lane": "lane-z", "task": "task/9", "age_s": 3600,
             "foreign_project": None, "project_unresolved": False},
            {"lane": "b-landed", "task": None, "age_s": 60,
             "foreign_project": "beta", "project_unresolved": False}],
            "total": 2, "unavailable": None},
    }


class BoardJoinTest(unittest.TestCase):
    """GET /api/board over a synthetic world in a temp home."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="helm-web-board-")
        self.addCleanup(tmp.cleanup)
        prior = {k: os.environ.get(k) for k in ("HELM_HOME", "HELM_CHAT_NAME")}
        os.environ["HELM_HOME"] = os.path.join(tmp.name, "helm-home")
        os.environ["HELM_CHAT_NAME"] = "the-owner"
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None
                                 else os.environ.__setitem__(k, v)
                                 for k, v in prior.items()])
        self.assertTrue(home.helm_home().startswith(tmp.name),
                        "HELM_HOME must win, or this reads the live fleet")
        projects, self.paths = {}, {}
        for i, name in enumerate(PROJECTS):
            path = os.path.join(tmp.name, "dev", name)
            os.makedirs(path)
            self.paths[name] = path
            # alpha worked an hour ago; the rest forty days ago
            projects[name] = {"name": name, "path": path, "status": "active",
                              "last_seen": (NOW - 3600 if name == "alpha"
                                            else NOW - 40 * 86400),
                              "sessions": {"harness-a": 1}}
        pk.write_json(home.registry_path(), {"version": 1,
                                             "projects": projects})
        for name, colour in (("alpha", "green"), ("beta", "red"),
                             ("delta", "yellow"), ("epsilon", "yellow")):
            _row, problem = registry.state(name, colour, reason="fixture",
                                           by="owner", apply=True)
            self.assertIsNone(problem)
        # THE REAL FOLD writes the snapshot, from owner declarations: a
        # hand-written snapshot would be this file's opinion of the shape.
        # THE CLOCK IS THE TEST'S OWN, taken here and not at import: a module
        # imported early in a long run would otherwise age its own snapshot
        # past the freshness bound before its first arm ran.
        self.now = time.time()
        self.assertTrue(burnflags.write_snapshot(inputs={"declarations": {
            "families": {
                "anthropic": {"colour": "ORANGE", "until": self.now + 7200,
                              "why": "one account left"},
                "codex": {"colour": "YELLOW", "until": self.now + 7200,
                          "why": "weekly window half spent"},
                "grok": {"colour": "RED", "until": self.now + 7200,
                         "why": "out of credit"}}}},
            now=self.now))
        self.snap_ts = pk.read_json(burnflags.snapshot_path())["ts"]
        for title, project, status in (
                ("a1", "alpha", "open"), ("a2", "alpha", "in_progress"),
                ("a3", "alpha", "open"), ("a4", "alpha", "closed"),
                ("b1", "beta", "open"), ("d1", "delta", "open"),
                ("u1", None, "open"), ("n1", "nowhere", "open")):
            _row, err = tasks.add(
                title, "seat-a", project=project, status=status,
                priority="P1",
                closed_reason="done" if status == "closed" else None)
            self.assertIsNone(err, err)
        self.calls = {"roster": 0, "lr": 0, "lr_all": 0}
        self.lr = _lr_body()
        self.roster = _placed(ROSTER, self.paths)
        self.lr_all = _lr_all_body()
        self.all_gate = threading.Event()
        self.all_gate.set()
        web_board._fleet_reset()
        self.addCleanup(web_board._fleet_reset)
        self.addCleanup(self.all_gate.set)
        # GH IS NEVER RUN FOR REAL HERE. Every visibility the board queues is
        # answered by this stub, each arm sets the answer it needs, and the
        # queue is drained before the temp home it writes into is removed.
        self.gh_asked, self.gh_answer = [], (None, "gh is stubbed in this test")
        self.real_gh = repofacts._gh_visibility
        patcher = mock.patch.object(repofacts, "_gh_visibility", self._gh)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(repofacts.drain, 5)
        web._qstate.pop("flags", None)
        web_board._forget()
        self.addCleanup(web._qstate.pop, "flags", None)
        self.addCleanup(web_board._forget)
        # EVERY LEG GETS A BUDGET NO TEST BOX CAN MISS. The arms about a slow
        # leg shorten that one leg's own; the rest must never time out here.
        budgets = mock.patch.object(
            web_board, "_LEG_BUDGET_S", dict.fromkeys(web_board._LEGS, 60),
            create=True)
        budgets.start()
        self.addCleanup(budgets.stop)
        self.addCleanup(web._ROSTER_REP_CACHE.pop, "main", None)

    def _gh(self, slug):
        self.gh_asked.append(slug)
        return self.gh_answer

    def _roster(self, room):
        self.calls["roster"] += 1
        web._ROSTER_REP_CACHE[room] = (self.now - 7, self.roster)
        return self.roster

    def _api_lr(self, qs):
        if qs.get("all_projects"):
            # THE ALL-PROJECTS READ, answered from its own gate so an arm can
            # hold it (a cold projection) or fail it
            self.calls["lr_all"] += 1
            self.all_gate.wait(10)
            if isinstance(self.lr_all, Exception):
                raise self.lr_all
            return self.lr_all, 200
        self.calls["lr"] += 1
        return self.lr, 200

    def board(self):
        with mock.patch.object(web, "_roster_cached", self._roster), \
                mock.patch.object(web, "_api_lr", self._api_lr):
            try:
                return web._api_board()
            finally:
                web._ROSTER_REP_CACHE.pop("main", None)

    # -- the join --------------------------------------------------------

    def test_each_project_gets_its_own_open_tasks_and_nothing_else(self):
        got = self.board()["projects"]
        self.assertEqual(got["alpha"]["tasks"]["open"], 3,
                         "a closed row counted as open work, or a live one "
                         "went missing")
        self.assertEqual(got["alpha"]["tasks"]["in_progress"], 1)
        self.assertEqual([t["title"] for t in got["alpha"]["tasks"]["top"]],
                         ["a1", "a2", "a3"])
        self.assertEqual(got["beta"]["tasks"]["open"], 1)

    def test_a_projects_line_counts_its_open_p0_and_p1(self):
        """The project's line says "3 open · 3 P1" (task/3445): the join
        counts each open row's rank, and a closed P1 is not one of them."""
        got = self.board()["projects"]
        self.assertEqual((got["alpha"]["tasks"]["p0"],
                          got["alpha"]["tasks"]["p1"]), (0, 3))
        self.assertEqual(got["beta"]["tasks"]["p1"], 1)

    def test_a_project_with_no_seats_carries_empty_seats_not_a_guess(self):
        """delta has open work and nobody seated on it. Its join says so in
        two empty lists; it must not borrow another project's seats."""
        delta = self.board()["projects"]["delta"]
        self.assertEqual(delta["tasks"]["open"], 1)   # the join DID reach it
        self.assertEqual(delta["seats"], [])
        self.assertEqual(delta["families"], [])

    def test_every_chip_takes_its_colour_from_its_familys_burn_flag(self):
        fams = {f["family"]: f for f in
                self.board()["projects"]["alpha"]["families"]}
        self.assertEqual(sorted(fams), ["anthropic", "codex", "localllm"])
        self.assertEqual(fams["anthropic"]["colour"], "ORANGE")
        self.assertIn("one account left", fams["anthropic"]["cause"])
        self.assertEqual(fams["codex"]["colour"], "YELLOW")
        # the seats behind each chip ride with it
        self.assertEqual(fams["anthropic"]["seats"], ["alpha-claude"])
        self.assertEqual(fams["codex"]["seats"], ["seat-a"])

    def test_each_family_names_the_account_groups_that_bill_it(self):
        """The Families card on Fleet › credit speaks model families, the
        accounts table vendors; the flags section carries the join: the seat
        catalog's one reading of the groups that bill each family, in route
        order — the reading the seeder mints its rows from (task/3461)."""
        from helm import seat
        fams = self.board()["sections"]["flags"]["families"]
        self.assertIn("anthropic", fams)
        self.assertIn("codex", fams)
        for fam in fams:
            self.assertEqual(fams[fam]["bills"], seat.billing_groups(fam), fam)
            self.assertNotIn("vendor", fams[fam], fam)
            self.assertIn("money_provenance", fams[fam], fam)
        self.assertEqual(fams["anthropic"]["bills"], ["anthropic"])
        self.assertEqual(fams["codex"]["bills"], ["codex"])

    def test_a_family_with_no_flag_is_carried_and_says_so(self):
        body = self.board()
        fams = {f["family"]: f for f in body["projects"]["alpha"]["families"]}
        # the positive control on the same observable: a family WITH a flag
        # arrives coloured, so the None below is the missing flag.
        self.assertEqual(fams["codex"]["colour"], "YELLOW")
        self.assertIsNone(fams["localllm"]["colour"])
        self.assertIn("codex", body["sections"]["flags"]["families"])
        self.assertNotIn("localllm", body["sections"]["flags"]["families"])

    def test_an_absent_seat_is_listed_and_spends_nothing(self):
        alpha = self.board()["projects"]["alpha"]
        seats = {s["seat"]: s for s in alpha["seats"]}
        self.assertEqual(seats["seat-b"]["presence"], "absent")
        self.assertEqual([f["seats"] for f in alpha["families"]
                          if f["family"] == "codex"], [["seat-a"]])
        self.assertNotIn("review-sa", seats,
                         "an ephemeral review agent reached the board")

    def test_a_projects_last_activity_is_its_newest_seat_beat(self):
        """The registry's `last_seen` is as old as the last sync; a seat's beat
        is live, so the join carries the newest one."""
        got = self.board()["projects"]
        self.assertEqual(got["alpha"]["active_at"], 1800000200)
        self.assertIn("seats", got["delta"])          # delta WAS joined
        self.assertNotIn("active_at", got["delta"])   # nobody seated, no beat

    def test_readings_nobody_can_place_are_counted_not_dropped(self):
        sections = self.board()["sections"]
        self.assertEqual(sections["seats"]["unplaced"], 1)
        self.assertEqual(sections["tasks"]["unplaced"], 1)
        self.assertEqual(sections["tasks"]["unscoped"], 1)

    def test_the_land_pipeline_lands_only_on_the_project_it_projects(self):
        body = self.board()
        self.assertEqual(body["sections"]["lands"]["scope"], "alpha")
        alpha = body["projects"]["alpha"]
        self.assertEqual(alpha["lanes"]["in_flight"], 1,
                         "an honored loop counted as work in flight")
        self.assertEqual(alpha["lanes"]["filed"], ["lane-a"])
        self.assertEqual(alpha["lanes"]["building"], 1)
        self.assertEqual(alpha["owner"]["holds"], 1)
        self.assertEqual(alpha["waits"][0]["label"], "integrator")
        # another project is NOT READ by this pipeline, which is not zero
        self.assertIn("tasks", body["projects"]["beta"])     # beta WAS joined
        self.assertNotIn("lanes", body["projects"]["beta"])

    # -- progress, repos and the quiet rule -------------------------------------

    def _commit(self, path, at):
        """One more commit on the checkout's main, dated `at`."""
        when = "@%d +0000" % at
        subprocess.run(
            ["git", "-c", "user.name=seat-a", "-c", "user.email=a@b.test",
             "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty",
             "-m", "a land"],
            cwd=path, check=True, capture_output=True,
            env=dict(os.environ, GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when,
                     GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1"))

    def test_progress_counts_seven_days_of_lands_and_of_tasks(self):
        self._repo(self.paths["alpha"], int(self.now) - 10 * 86400)
        for back in (3, 1):                         # two lands this week
            self._commit(self.paths["alpha"], int(self.now) - back * 86400)
        a1 = [r for r in tasks.snapshot()[0].values() if r["title"] == "a1"]
        _row, err = tasks.close(a1[0]["id"], "done")
        self.assertIsNone(err, err)
        prog = self.board()["projects"]["alpha"]["progress"]
        self.assertEqual(prog["lands7"], 2)         # the ten-day-old one is out
        # a1..a3 were opened this week and a1 was closed; a4 was BORN closed,
        # a record of history rather than work, so it is neither
        self.assertEqual((prog["opened7"], prog["closed7"]), (3, 1))
        self.assertEqual(self.board()["projects"]["beta"]["progress"]
                         ["opened7"], 1)            # the per-project control

    def test_a_trunk_idle_for_a_week_counts_zero_lands_without_a_log_walk(self):
        self._repo(self.paths["alpha"], int(self.now) - 10 * 86400)
        walked = []
        real = web_board._land_times
        with mock.patch.object(web_board, "_land_times",
                               lambda *a: walked.append(a) or real(*a)):
            prog = self.board()["projects"]["alpha"]["progress"]
            self.assertEqual(prog["lands7"], 0)
            self.assertEqual(walked, [])
            # the control: a land this week makes the same read walk
            web_board._forget()
            self._commit(self.paths["alpha"], int(self.now) - 60)
            self.assertEqual(self.board()["projects"]["alpha"]["progress"]
                             ["lands7"], 1)
        self.assertEqual(len(walked), 1)

    def test_progress_with_no_readable_trunk_is_unknown_never_zero(self):
        prog = self.board()["projects"]["beta"]["progress"]
        self.assertIsNone(prog["lands7"])           # beta is a plain directory
        self.assertEqual(prog["opened7"], 1)        # the same row's control

    def test_the_quiet_rule_is_decided_on_the_server(self):
        got = self.board()["projects"]
        # gamma: idle forty days, nothing open, no light
        self.assertIs(got["gamma"]["quiet"], True)
        # delta: idle, a yellow light set now, and open work
        self.assertIs(got["delta"]["quiet"], False)
        # epsilon: idle, nothing open, and a light the owner set today
        self.assertIs(got["epsilon"]["quiet"], False)
        # alpha: active an hour ago
        self.assertIs(got["alpha"]["quiet"], False)

    def test_a_light_set_now_unfolds_a_quiet_project_on_the_next_read(self):
        self.assertIs(self.board()["projects"]["gamma"]["quiet"], True)
        _row, problem = registry.state("gamma", "green", reason="back on",
                                       by="owner", apply=True)
        self.assertIsNone(problem)
        # the joins are still cached; the light is read per response
        self.assertIs(self.board()["projects"]["gamma"]["quiet"], False)
        self.assertEqual(self.calls["roster"], 1)

    def test_an_unread_backlog_folds_nothing(self):
        with mock.patch.object(tasks, "snapshot",
                               return_value=({}, "ledger unreadable")):
            got = self.board()["projects"]
        self.assertIs(got["gamma"]["quiet"], False)
        web_board._forget()
        self.assertIs(self.board()["projects"]["gamma"]["quiet"], True)

    def test_repos_are_read_from_the_checkouts_remotes(self):
        run = self._repo(self.paths["alpha"], int(self.now) - 60)
        run("remote", "add", "origin", "git@github.com:akapug/alpha.git")
        run("remote", "add", "upstream", "https://github.com/emberian/alpha")
        repos = self.board()["projects"]["alpha"]["repos"]
        self.assertEqual([(r["remote"], r["slug"]) for r in repos],
                         [("origin", "akapug/alpha"),
                          ("upstream", "emberian/alpha")])
        self.assertEqual(repos[0]["url"], "https://github.com/akapug/alpha")
        self.assertEqual(repos[1]["url"], "https://github.com/emberian/alpha")

    def test_a_remote_url_carrying_a_credential_never_reaches_the_wire(self):
        run = self._repo(self.paths["alpha"], int(self.now) - 60)
        run("remote", "add", "origin",
            "https://someone:not-a-real-secret@github.com/akapug/alpha.git")
        body = json.dumps(self.board())
        self.assertIn("akapug/alpha", body)         # the remote WAS read
        self.assertNotIn("not-a-real-secret", body)

    def test_visibility_comes_from_gh_once_and_is_cached(self):
        run = self._repo(self.paths["alpha"], int(self.now) - 60)
        run("remote", "add", "origin", "git@github.com:akapug/alpha.git")
        self.gh_answer = ("PRIVATE", None)
        first = self.board()["projects"]["alpha"]["repos"][0]
        self.assertTrue(repofacts.drain(5))
        web_board._forget()
        second = self.board()["projects"]["alpha"]["repos"][0]
        web_board._forget()
        third = self.board()["projects"]["alpha"]["repos"][0]
        self.assertEqual(first["visibility"], "pending")
        self.assertEqual(second["visibility"], "private")
        self.assertEqual(third["visibility"], "private")
        self.assertEqual(self.gh_asked, ["akapug/alpha"], "gh was asked twice")
        # and the cache outlives the process: it is on disk
        self.assertEqual(pk.read_json(repofacts.cache_path())["repos"]
                         ["akapug/alpha"]["visibility"], "private")

    def test_a_visibility_older_than_a_day_is_asked_again(self):
        self.gh_answer = ("PUBLIC", None)
        repofacts.visibility(["akapug/alpha"], now=self.now)
        self.assertTrue(repofacts.drain(5))
        got = repofacts.visibility(["akapug/alpha"], now=self.now)
        self.assertEqual(got["akapug/alpha"]["visibility"], "public")
        self.assertEqual(self.gh_asked, ["akapug/alpha"])
        later = repofacts.visibility(["akapug/alpha"],
                                     now=self.now + 25 * 3600)
        self.assertTrue(repofacts.drain(5))
        # the old answer is shown while it is asked again, marked as old
        self.assertEqual(later["akapug/alpha"]["visibility"], "public")
        self.assertIs(later["akapug/alpha"]["stale"], True)
        self.assertEqual(self.gh_asked, ["akapug/alpha", "akapug/alpha"])

    def test_gh_failing_reads_unknown_never_a_guess(self):
        self.gh_answer = (None, "gh: not logged in")
        repofacts.visibility(["akapug/beta"], now=self.now)
        self.assertTrue(repofacts.drain(5))
        got = repofacts.visibility(["akapug/beta"], now=self.now)
        self.assertEqual(got["akapug/beta"]["visibility"], "unknown")
        self.assertIn("not logged in", got["akapug/beta"]["why"])

    def test_gh_itself_is_asked_for_the_visibility_field(self):
        """The one real spawn, with `gh` replaced by a script on PATH: the
        argv is what the brief names, with the host spelled out so GH_HOST
        cannot send the ask elsewhere (tests/test_or_free_model_class.py),
        and an answer gh does not recognise is unknown rather than a
        guess."""
        bindir = tempfile.mkdtemp(prefix="helm-fake-gh-")
        self.addCleanup(shutil.rmtree, bindir, True)
        log = os.path.join(bindir, "argv")
        with open(os.path.join(bindir, "gh"), "w") as handle:
            handle.write('#!/bin/sh\necho "$@" > "%s"\n'
                         'echo \'{"visibility":"PUBLIC"}\'\n' % log)
        os.chmod(os.path.join(bindir, "gh"), 0o755)
        with mock.patch.dict(os.environ,
                             {"PATH": bindir + os.pathsep + os.environ["PATH"]}):
            got = self.real_gh("akapug/alpha")
        self.assertEqual(got, ("PUBLIC", None))
        with open(log) as handle:
            self.assertEqual(handle.read().split(),
                             ["repo", "view", "github.com/akapug/alpha",
                              "--json", "visibility"])

    def test_a_quiet_projects_repos_are_never_sent_to_gh(self):
        run = self._repo(self.paths["gamma"], int(self.now) - 50 * 86400)
        run("remote", "add", "origin", "git@github.com:akapug/gamma.git")
        run2 = self._repo(self.paths["alpha"], int(self.now) - 60)
        run2("remote", "add", "origin", "git@github.com:akapug/alpha.git")
        got = self.board()["projects"]
        self.assertTrue(repofacts.drain(5))
        self.assertIs(got["gamma"]["quiet"], True)
        self.assertEqual(got["gamma"]["repos"][0]["slug"], "akapug/gamma")
        self.assertEqual(got["gamma"]["repos"][0]["visibility"], "unasked")
        self.assertEqual(self.gh_asked, ["akapug/alpha"])   # the control

    def test_a_remote_that_is_not_github_is_unknown_and_links_nothing(self):
        run = self._repo(self.paths["alpha"], int(self.now) - 60)
        run("remote", "add", "origin", "https://gitlab.example/x/alpha.git")
        repo = self.board()["projects"]["alpha"]["repos"][0]
        self.assertEqual(repo["visibility"], "unknown")
        self.assertIsNone(repo["url"])
        self.assertIsNone(repo["slug"])
        self.assertEqual(self.gh_asked, [])

    def test_a_checkout_with_no_remote_has_no_repos_and_a_plain_dir_is_unknown(
            self):
        self._repo(self.paths["alpha"], int(self.now) - 60)
        got = self.board()["projects"]
        self.assertEqual(got["alpha"]["repos"], [])
        self.assertIsNone(got["beta"]["repos"])
        self.assertIn("checkout", got["beta"]["repos_unavailable"])

    # -- the per-project kanban -------------------------------------------------

    def test_the_scope_projects_kanban_carries_its_loops_and_its_lands(self):
        self.lr["loops"] = [
            {"id": "r1", "lane": "lane-a", "state": "AWAITING_REVIEW",
             "honored": False},
            {"id": "r3", "lane": "lane-g", "state": "READY", "honored": False},
            {"id": "r2", "lane": "lane-b", "state": "AWAITING_REVIEW",
             "honored": True}]
        alpha = self.board()["projects"]["alpha"]
        # `trunk_contains_tip` rides every card, None where the pipeline
        # never answered it — "not asked", which the page draws as before —
        # and beside it the one predicate's answer, False over a None, and
        # the source-clean sentence, None on a row that owes no on-main move
        self.assertEqual(alpha["lanes"]["loops"],
                         [{"id": "r1", "lane": "lane-a",
                           "state": "AWAITING_REVIEW",
                           "trunk_contains_tip": None,
                           "on_main_unverdicted": False,
                           "source_clean_on_main": None,
                           "owes_rehold": False, "age_s": None,
                           "mark": "moving", **_BARE},
                          {"id": "r3", "lane": "lane-g", "state": "READY",
                           **_BARE,
                           "trunk_contains_tip": None,
                           "on_main_unverdicted": False,
                           "source_clean_on_main": None,
                           "owes_rehold": False, "age_s": None,
                           "mark": "moving"}])
        self.assertEqual(alpha["lanes"]["building_lanes"], ["lane-c"])
        self.assertEqual(alpha["landed"],
                         [{"lane": "lane-z", "task": "task/9", "age_s": 3600,
                           "tip": None, "gate": ""}])

    def test_an_unread_lands_list_is_unknown_not_empty(self):
        self.lr["recent_lands"] = {"rows": [], "total": None,
                                   "unavailable": "ledger unreadable"}
        alpha = self.board()["projects"]["alpha"]
        self.assertIsNone(alpha["landed"])
        self.assertEqual(alpha["lanes"]["filed"], ["lane-a"])   # the control

    # -- every other project's pipeline, read in the background ---------------

    def test_other_projects_pipeline_is_loading_and_never_holds_the_board(self):
        """The all-projects read is a whole second projection; the board
        answers without waiting on it and says the columns are loading."""
        self.all_gate.clear()                        # a cold projection
        started = time.time()
        body = self.board()
        self.assertLess(time.time() - started, 5)
        self.assertIs(body["sections"]["fleet"]["loading"], True)
        self.assertIsNone(body["sections"]["fleet"]["unavailable"])
        self.assertNotIn("pipeline", body["projects"]["beta"])
        self.all_gate.set()                          # the projection finishes
        self.assertTrue(web_board._fleet_wait(5))
        self.assertEqual(self.calls["lr_all"], 1)    # it WAS asked, once

    def test_other_projects_pipeline_fills_once_the_read_lands(self):
        self.board()
        self.assertTrue(web_board._fleet_wait(5))
        got = self.board()
        fleet = got["sections"]["fleet"]
        self.assertFalse(fleet.get("loading"))
        self.assertIsNone(fleet["unavailable"])
        beta = got["projects"]["beta"]["pipeline"]
        self.assertEqual([(c["lane"], c["state"]) for c in beta["loops"]],
                         [("b-review", "AWAITING_REVIEW"),
                          ("b-ready", "READY")])      # the honored row left
        self.assertEqual([r["lane"] for r in beta["landed"]], ["b-landed"])
        # a project the read reached and found nothing for is EMPTY, not
        # absent — and has no count line to draw, which is None, not a zero
        self.assertEqual(got["projects"]["gamma"]["pipeline"],
                         {"loops": [], "landed": [], "on_main": None,
                          "collapsed": [], "loops_more": {}, "rehold": None,
                          "tally": {"live": 0, "marks": {"contrary": 0,
                                                         "stalled": 0,
                                                         "nonbillable": 0,
                                                         "moving": 0},
                                    "holders": {}},
                          "landed_more": 0})
        # the scope project keeps its own scoped read
        self.assertNotIn("pipeline", got["projects"]["alpha"])
        # rows no registered project owns are counted, never placed
        self.assertEqual(fleet["unplaced"], 2)
        self.assertIsNone(fleet["landed_partial"])    # the whole list was read

    def test_a_capped_lands_list_says_how_much_of_it_was_read(self):
        self.lr_all["recent_lands"]["total"] = 40      # the newest 2 of 40
        self.board()
        self.assertTrue(web_board._fleet_wait(5))
        fleet = self.board()["sections"]["fleet"]
        self.assertEqual(fleet["landed_partial"], {"shown": 2, "total": 40})

    def test_a_failed_all_projects_read_is_unknown_never_empty(self):
        self.lr_all = OSError("projection exploded")
        self.board()
        self.assertTrue(web_board._fleet_wait(5))
        got = self.board()
        self.assertIn("OSError", got["sections"]["fleet"]["unavailable"])
        self.assertNotIn("pipeline", got["projects"]["beta"])
        self.lr_all = {"unavailable": "ledger unreadable", "loops": []}
        web_board._fleet_reset()
        self.board()
        self.assertTrue(web_board._fleet_wait(5))
        self.assertEqual(self.board()["sections"]["fleet"]["unavailable"],
                         "ledger unreadable")

    def test_a_warming_all_projects_read_is_still_loading(self):
        self.lr_all = {"warming": True}
        self.board()
        self.assertTrue(web_board._fleet_wait(5))
        fleet = self.board()["sections"]["fleet"]
        self.assertIs(fleet["loading"], True)
        self.assertIsNone(fleet["unavailable"])

    # -- a warm read keeps its stamps (task/3657) ------------------------------

    def test_an_unmoved_all_projects_reading_keeps_its_read_stamp(self):
        """EVERY board response asks the all-projects read again, and while
        nothing moved the pipeline answers each ask with the SAME reading.
        The fleet section's clock is that reading's own instant, so it is one
        stamp on every response. Derived from each ask's completion less a
        whole-second age, it moved on every response, and every Work poll
        read a new revision and rebuilt its snapshot (task/3657)."""
        first = self.board()["sections"]["fleet"]["measured_at"]
        self.assertTrue(web_board._fleet_wait(5))
        time.sleep(0.3)                 # a later ask of the same reading
        self.board()
        self.assertTrue(web_board._fleet_wait(5))
        again = self.board()["sections"]["fleet"]["measured_at"]
        self.assertGreaterEqual(self.calls["lr_all"], 2)  # asked again
        self.assertIsNotNone(first)
        self.assertEqual(again, first)
        self.assertEqual(first, self.lr_all["read_at"])

    def test_a_reread_of_an_unmoved_land_pipeline_keeps_its_read_stamp(self):
        """The lands leg reads the pipeline again behind its served reading;
        an unmoved pipeline answers with the same reading, and the section
        keeps that reading's stamp rather than the re-read's clock less a
        whole-second age (task/3657)."""
        first = self.board()["sections"]["lands"]["measured_at"]
        web_board._forget()             # the leg is read again
        time.sleep(0.3)
        again = self.board()["sections"]["lands"]["measured_at"]
        self.assertEqual(self.calls["lr"], 2)
        self.assertEqual(again, first)
        self.assertEqual(first, self.lr["read_at"])

    def test_a_leg_read_past_its_budget_does_not_hold_the_next_board(self):
        """A board waits for each leg until THAT READ's budget runs out. A read
        an earlier board already waited its budget out on is still being
        read, and the next board says so at once: waiting a second budget on
        it cost every warm poll three seconds on the owner's console while
        the land projection rebuilt (task/3657). What each board waited is
        read off the join it made, never off the clock."""
        gate = threading.Event()
        self.addCleanup(gate.set)
        waits, real = [], web_board._read_behind

        def slow(qs):
            if not qs.get("all_projects"):
                gate.wait(10)
            return self._api_lr(qs)

        class Joined:
            """The lands read's thread, recording each board's wait on it."""
            def __init__(self, thread):
                self.thread = thread

            def join(self, timeout=None):
                waits.append(timeout)
                self.thread.join(timeout)

            def is_alive(self):
                return self.thread.is_alive()

        def spied(key, *args, **kw):
            thread, box = real(key, *args, **kw)
            if thread is None or key != "board:lands":
                return thread, box
            return Joined(thread), box
        with mock.patch.dict(web_board._LEG_BUDGET_S, {"lands": 0.3}), \
                mock.patch.object(web_board, "_read_behind", spied), \
                mock.patch.object(web, "_roster_cached", self._roster), \
                mock.patch.object(web, "_api_lr", slow):
            first = web._api_board()
            second = web._api_board()
            gate.set()
            self.assertTrue(self._settle("lands"))
            third = web._api_board()
        self.assertEqual(len(waits), 2, waits)
        self.assertGreater(waits[0], 0.2, "the first board did not wait for "
                           "the leg read it started")
        self.assertEqual(waits[1], 0.0, "the next board waited a second "
                         "budget on a read already past its own")
        self.assertIs(first["sections"]["lands"]["loading"], True)
        self.assertIs(second["sections"]["lands"]["loading"], True)
        self.assertIsNone(second["sections"]["lands"]["unavailable"])
        self.assertEqual(self.calls["lr"], 1, "a second lands read started")
        # the read it left behind lands, and is what the board then serves
        self.assertFalse(third["sections"]["lands"].get("loading"))
        self.assertEqual(third["projects"]["alpha"]["lanes"]["in_flight"], 1)

    def test_the_marks_are_the_boards_own_four_sections(self):
        """THE WORK READER'S REVISION MARKS FOUR SECTIONS (lands, fleet,
        seats, tasks: each one's clock, scope and state). `_board_marks`
        answers them off the board's OWN legs and fleet read, without the
        joins, so a Work poll with nothing moved costs no board build; and
        they are the sections `/api/board` carries, so the revision they make
        is the whole board's (task/3657)."""
        from helm import work_model
        with mock.patch.object(web, "_roster_cached", self._roster), \
                mock.patch.object(web, "_api_lr", self._api_lr):
            web._api_board()
            self.assertTrue(web_board._fleet_wait(5))
            board = web._api_board()
            self.assertTrue(web_board._fleet_wait(5))
            with mock.patch.object(web_board, "_seats_join",
                                   side_effect=AssertionError("joined")):
                marks, projects = web_board._board_marks()
        self.assertEqual(sorted(marks["sections"]),
                         ["fleet", "lands", "seats", "tasks"])
        for name in ("fleet", "lands", "seats", "tasks"):
            for key in ("measured_at", "scope", "unavailable", "loading",
                        "stale"):
                with self.subTest(section=name, field=key):
                    self.assertEqual(marks["sections"][name].get(key),
                                     board["sections"][name].get(key))
        self.assertIsNotNone(marks["sections"]["fleet"]["measured_at"])
        self.assertEqual(projects, web_board._projects())
        self.assertEqual(
            work_model.revision(marks, projects, "fp", NOW),
            work_model.revision(board, web_board._projects(), "fp", NOW))

    # -- the last land, off each project's own trunk ---------------------------

    def _repo(self, path, committed_at, push=False):
        """Make `path` a git checkout whose one commit on main carries
        `committed_at`, optionally pushed to a bare origin."""
        env = dict(os.environ, GIT_AUTHOR_DATE="@%d +0000" % committed_at,
                   GIT_COMMITTER_DATE="@%d +0000" % committed_at,
                   GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        git = ["git", "-c", "user.name=seat-a", "-c", "user.email=a@b.test",
               "-c", "commit.gpgsign=false"]
        run = lambda *a: subprocess.run(git + list(a), cwd=path, env=env,
                                        check=True, capture_output=True)
        run("init", "-q", "-b", "main")
        run("commit", "-q", "--allow-empty", "-m", "a land")
        if push:
            bare = path + ".origin.git"
            subprocess.run(["git", "init", "-q", "--bare", bare], check=True,
                           capture_output=True, env=env)
            run("remote", "add", "origin", bare)
            # THE PUSH RUNS ON THE REAL CLOCK. A ref log entry is stamped with
            # the committer identity's date, so a push under the pinned date
            # would log the commit's instant and prove nothing.
            now_env = {k: v for k, v in env.items()
                       if k not in ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE")}
            subprocess.run(git + ["push", "-q", "origin", "main"], cwd=path,
                           env=now_env, check=True, capture_output=True)
        return run

    def test_last_land_is_the_newest_commit_on_the_projects_main(self):
        """The trains land by a fast-forward push and file no land-request
        row, so the pipeline's newest close is hours behind the trunk. The
        trunk itself is the answer."""
        committed = int(self.now) - 60
        self._repo(self.paths["alpha"], committed)
        land = self.board()["projects"]["alpha"]["last_land"]
        # the land-request row says an hour ago; the trunk says a minute ago
        self.assertEqual(self.lr["recent_lands"]["rows"][0]["age_s"], 3600)
        self.assertEqual(land["at"], committed)
        self.assertEqual(land["how"], "commit")
        self.assertEqual(land["ref"], "main")
        self.assertLess(land["age_s"], 3600)

    def test_a_land_this_checkout_pushed_is_dated_by_the_push(self):
        """A train merge is committed, gated, then pushed: the commit's clock
        is when it was made, the push is when it landed. The ref's own log
        says which, and only a push is taken as the land instant."""
        committed = int(self.now) - 7200
        self._repo(self.paths["alpha"], committed, push=True)
        land = self.board()["projects"]["alpha"]["last_land"]
        self.assertEqual(land["how"], "push")
        self.assertGreater(land["at"], committed + 3600)

    def test_a_project_with_no_readable_trunk_says_so_and_never_zero(self):
        committed = int(self.now) - 60
        self._repo(self.paths["alpha"], committed)          # the control
        got = self.board()["projects"]
        self.assertEqual(got["alpha"]["last_land"]["at"], committed)
        # beta's path is a plain directory, not a checkout
        self.assertIn("unavailable", got["beta"]["last_land"])
        self.assertNotIn("at", got["beta"]["last_land"])
        self.assertEqual(self.board()["sections"]["trunk"]["source"],
                         "git for-each-ref")

    def test_the_gate_window_is_read_and_unreadable_is_never_no_gate(self):
        """task/3129: the board reads the store `helm gate window show`
        reads. None launched here: every project with a checkout carries an
        empty list, which is an answer. A store nobody can read is the gate
        section UNAVAILABLE with its reason, and no project carries a list."""
        self._repo(self.paths["alpha"], int(self.now) - 60)
        body = self.board()
        self.assertIsNone(body["sections"]["gate"]["unavailable"])
        self.assertEqual(body["sections"]["gate"]["source"],
                         "helm gate window show")
        self.assertEqual(body["projects"]["alpha"]["gate"], [])
        self.assertNotIn("gate", body["projects"]["beta"])   # no checkout
        path = gatewindow.runs_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("{not json")
        web_board._forget()
        body = self.board()
        self.assertIn("could not be read",
                      body["sections"]["gate"]["unavailable"])
        self.assertNotIn("gate", body["projects"]["alpha"],
                         "an unreadable gate window was sent as no gate")

    # -- the clocks --------------------------------------------------------

    def test_every_section_carries_its_own_clock(self):
        sections = self.board()["sections"]
        self.assertEqual(sorted(sections),
                         ["flags", "fleet", "gate", "lands", "lights",
                          "seats", "tasks", "teams", "trunk"])
        for name, sec in sections.items():
            for key in ("source", "measured_at", "age_s", "limit_s", "stale",
                        "unavailable"):
                self.assertIn(key, sec, "%s has no %s" % (name, key))
        self.assertEqual(sections["flags"]["measured_at"], self.snap_ts)
        self.assertEqual(sections["seats"]["measured_at"], self.now - 7)
        self.assertLess(abs(sections["lands"]["measured_at"] - (self.now - 4)),
                        30)

    def test_a_section_past_its_limit_is_stale_and_never_fresh(self):  # noqa: VACUOUS_ASSERTION — the False arms are each a control for the assertIs(True) on the same `stale` key two reads later, and the age/limit comparison is positive
        fresh = self.board()["sections"]
        # the control: the SAME section, read four seconds ago, is fresh
        self.assertIs(fresh["lands"]["stale"], False)
        web_board._forget()
        self.lr = _lr_body(read_age_s=10 ** 6)
        stale = self.board()["sections"]
        self.assertIs(stale["lands"]["stale"], True)
        # the land pipeline's section is bounded by the two bounds its age
        # adds up from, the leg's and the pipeline body's own (task/3632)
        self.assertEqual(stale["lands"]["limit_s"], web_board.PIPELINE_LIMIT_S)
        self.assertGreater(stale["lands"]["age_s"], stale["lands"]["limit_s"])
        for name in ("flags", "tasks", "seats", "lights", "trunk"):
            self.assertIs(stale[name]["stale"], False, name)

    def test_an_unreadable_backlog_is_unknown_and_never_zero(self):
        with mock.patch.object(tasks, "snapshot",
                               return_value=({}, "ledger unreadable")):
            body = self.board()
        self.assertIn("ledger unreadable",
                      body["sections"]["tasks"]["unavailable"])
        self.assertNotIn("tasks", body["projects"]["alpha"],
                         "an unread backlog was joined as a number")
        # the rest of the board still answers beside it
        self.assertTrue(body["projects"]["alpha"]["families"])

    def test_a_slow_land_pipeline_does_not_hold_the_board(self):
        """The land projection's first read after a restart ran 113s on a
        loaded box. The board answers without it, says why, and does not pin
        that answer for a cache window: the next read gets the warmed lanes."""
        gate = threading.Event()

        def slow(qs):
            gate.wait(10)
            return self._api_lr(qs)
        with mock.patch.dict(web_board._LEG_BUDGET_S, {"lands": 0.2}), \
                mock.patch.object(web, "_roster_cached", self._roster), \
                mock.patch.object(web, "_api_lr", slow):
            first = web._api_board()
            self.assertNotIn("board:lands", web._qstate,
                             "a timed-out lanes read was cached for a window")
            gate.set()
            time.sleep(0.2)
            second = web._api_board()
        self.assertIs(first["sections"]["lands"]["loading"], True)
        self.assertIs(first["sections"]["lands"]["retry"], True)
        self.assertIsNone(first["sections"]["lands"]["unavailable"])
        self.assertIn("tasks", first["projects"]["alpha"])  # the rest answered
        self.assertNotIn("lanes", first["projects"]["alpha"])
        self.assertIsNone(second["sections"]["lands"]["unavailable"])
        self.assertEqual(second["projects"]["alpha"]["lanes"]["in_flight"], 1)

    def _slow_roster(self, gate, done):
        """A roster read that waits on `gate`, the way a cold roster report
        took ten seconds after a restart, and sets `done` once it answered."""
        def slow(room):
            gate.wait(10)
            try:
                return self._roster(room)
            finally:
                done.set()
        return slow

    def test_a_slow_leg_answers_inside_its_budget_as_still_being_read(self):  # noqa: VACUOUS_ASSERTION — the absent seats join and the None headline sit beside the unconditional loading/retry asserts on the same section and the counted tasks join of the same body; the next-read arm asserts those same keys filled
        """The first read after a restart took 70s against the page's 45s,
        ten of them the roster. A leg past its budget is named as still being
        read, and every other leg answers beside it."""
        gate, done = threading.Event(), threading.Event()
        self.addCleanup(gate.set)
        with mock.patch.dict(web_board._LEG_BUDGET_S, {"seats": 0.3}), \
                mock.patch.object(web, "_roster_cached",
                                  self._slow_roster(gate, done)), \
                mock.patch.object(web, "_api_lr", self._api_lr):
            started = time.monotonic()
            first = web._api_board()
            took = time.monotonic() - started
            gate.set()
            self.assertTrue(done.wait(10))
        self.assertLess(took, 5, "the board waited on its slowest leg")
        seats = first["sections"]["seats"]
        self.assertIs(seats.get("loading"), True)
        self.assertIs(seats.get("retry"), True)
        self.assertIsNone(seats["unavailable"],
                          "a leg still being read was drawn as UNKNOWN")
        # nothing the slow leg owns is drawn, and never as a zero
        self.assertIsNone(first["headline"]["lanes"]["running"])
        self.assertIsNone(first["headline"]["lanes"]["possible"])
        self.assertNotIn("seats", first["projects"]["alpha"])
        # the control: every other leg answered beside it
        self.assertIsNone(first["sections"]["tasks"]["unavailable"])
        self.assertEqual(first["projects"]["alpha"]["tasks"]["open"], 3)

    def test_the_next_read_after_the_slow_leg_lands_carries_its_data(self):
        """The read the budget left behind keeps going, and it is what the
        next board read serves: no second roster read is started."""
        gate, done = threading.Event(), threading.Event()
        self.addCleanup(gate.set)
        with mock.patch.dict(web_board._LEG_BUDGET_S, {"seats": 0.3}), \
                mock.patch.object(web, "_roster_cached",
                                  self._slow_roster(gate, done)), \
                mock.patch.object(web, "_api_lr", self._api_lr):
            first = web._api_board()
            gate.set()
            self.assertTrue(done.wait(10))
            # the read LANDS once it has kept its answer: its budget was
            # waited out by the first read, so the next one is not held on it
            self.assertTrue(self._settle("seats"))
            second = web._api_board()
        self.assertIs(first["sections"]["seats"].get("loading"), True)
        self.assertFalse(second["sections"]["seats"].get("loading"))
        self.assertIsNone(second["sections"]["seats"]["unavailable"])
        self.assertEqual(second["headline"]["lanes"]["claimed"], 2)
        self.assertIn("seats", second["projects"]["alpha"])
        self.assertEqual(self.calls["roster"], 1,
                         "the warming read was dropped and a second started")

    def _settle(self, name, timeout=5):
        """True once no read of the board's `name` leg is running, in the
        foreground or behind a served reading."""
        key = "board:" + name
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            held = web_cache._qreads.get(key)
            if not (held is not None and held[0].is_alive()) \
                    and key not in web._qinflight:
                return True
            time.sleep(0.02)
        return False

    def test_seats_able_waits_for_the_credit_flags(self):
        """M leaves out a seat whose family is RED, so it cannot be counted
        before the flags are read. grok is RED here: while the flags are still
        being read, grok-1 must not count as able to work."""
        gate, done = threading.Event(), threading.Event()
        self.addCleanup(gate.set)
        real = web_board._flags_section

        def slow():
            gate.wait(10)
            try:
                return real()
            finally:
                done.set()
        with mock.patch.dict(web_board._LEG_BUDGET_S, {"flags": 0.3}), \
                mock.patch.object(web_board, "_flags_section", slow), \
                mock.patch.object(web, "_roster_cached", self._roster), \
                mock.patch.object(web, "_api_lr", self._api_lr):
            first = web._api_board()
            gate.set()
            self.assertTrue(done.wait(10))
            self.assertTrue(self._settle("flags"))
            second = web._api_board()
        self.assertIs(first["sections"]["flags"].get("loading"), True)
        self.assertIsNone(first["headline"]["lanes"]["possible"],
                          "a RED seat was counted able before the flags "
                          "were read")
        self.assertIs(first["sections"]["seats"].get("loading"), True)
        # the control: once the flags are read, grok-1 is out and M is read
        self.assertEqual(second["headline"]["lanes"]["possible"], 6)

    def test_a_warming_answer_that_lands_late_is_not_kept(self):
        """The lands leg ran past its budget and then answered "warming". The
        pipeline is ready by the next read, which must carry its lanes and not
        the late marker."""
        gate = threading.Event()
        self.addCleanup(gate.set)
        late = [{"warming": True}]

        def lr(qs):
            if late and not qs.get("all_projects"):
                gate.wait(10)
                return late.pop(), 200
            return self._api_lr(qs)
        with mock.patch.dict(web_board._LEG_BUDGET_S, {"lands": 0.2}), \
                mock.patch.object(web, "_roster_cached", self._roster), \
                mock.patch.object(web, "_api_lr", lr):
            first = web._api_board()
            gate.set()
            self.assertTrue(self._settle("lands"))
            second = web._api_board()
        self.assertIs(first["sections"]["lands"].get("loading"), True)
        self.assertEqual(late, [])                  # the late answer DID land
        self.assertFalse(second["sections"]["lands"].get("loading"),
                         "a warming answer that landed late was served after "
                         "the pipeline was ready")
        self.assertEqual(second["projects"]["alpha"]["lanes"]["in_flight"], 1)

    def test_an_unreadable_leg_is_asked_again_on_the_next_read(self):
        """An answer that says its source could not be read is not kept for a
        window: once the source is readable, the very next read says so."""
        with mock.patch.object(tasks, "snapshot",
                               return_value=({}, "ledger unreadable")):
            first = self.board()
        second = self.board()
        self.assertIn("ledger unreadable",
                      first["sections"]["tasks"]["unavailable"])
        self.assertIsNone(second["sections"]["tasks"]["unavailable"],
                          "an unreadable answer was kept for a window")
        self.assertEqual(second["projects"]["alpha"]["tasks"]["open"], 3)

    def test_a_leg_whose_read_behind_fails_stops_drawing_its_numbers(self):
        """After one good roster read, every read behind it raises. The page
        must stop drawing the last numbers as current: the section says the
        roster could not be read and how old the reading it holds back is."""
        calls = []

        def roster(room):
            calls.append(room)
            if len(calls) > 1:
                raise OSError("roster gone")
            return self._roster(room)
        with mock.patch.object(web_board, "BOARD_TTL_S", 0), \
                mock.patch.object(web, "_roster_cached", roster), \
                mock.patch.object(web, "_api_lr", self._api_lr):
            first = web._api_board()
            self.assertTrue(self._settle("seats"))
            web._api_board()            # the last reading, a read behind it
            self.assertTrue(self._settle("seats"))
            third = web._api_board()
        self.assertEqual(first["headline"]["lanes"]["possible"], 6)
        self.assertGreaterEqual(len(calls), 2)      # a read behind DID run
        seats = third["sections"]["seats"]
        self.assertIn("OSError", seats["unavailable"] or "")
        self.assertIn("old", seats["unavailable"])
        self.assertIsNone(third["headline"]["lanes"]["possible"])
        self.assertNotIn("seats", third["projects"]["alpha"])

    # -- the headline and the owner's write --------------------------------

    def test_the_headline_is_lanes_running_of_seats_able_and_green(self):
        head = self.board()["headline"]
        self.assertEqual(head["green"], 1)
        # the tightest family's colour: grok is declared RED in this world
        self.assertEqual((head["colour"], head["family"]), ("RED", "grok"))
        # `landed` is 0 here and not absent: these fixture projects have no
        # checkout, so every claim's landedness is UNKNOWN and none is counted
        self.assertEqual(head["lanes"], {"running": 4, "possible": 6,
                                         "claimed": 2, "seats": 2,
                                         "offboard": 0, "landed": 0,
                                         "unscoped": ["stray"]})
        self.assertNotIn("capacity", head)

    def test_R_counts_live_lanes_once_across_every_project(self):  # noqa: VACUOUS_ASSERTION — the claimed count (2) and the exact alpha and beta entry dicts after the loops are unconditional positives on the same running[] entries
        """Two holders on one lane are one lane; a dead holder's claim is
        not running; beta's lane counts beside alpha's."""
        body = self.board()
        self.assertEqual(body["headline"]["lanes"]["claimed"], 2)
        # EVERY CLAIM CARRIES ITS LANDEDNESS, and these fixture projects have
        # no git checkout at their registered paths: the answer is UNKNOWN,
        # naming why, never a verdict and never absent
        alpha = dict(body["projects"]["alpha"]["running"][0])
        beta = [dict(r) for r in body["projects"]["beta"]["running"]]
        for landed in [alpha.pop("landed")] + [r.pop("landed") for r in beta]:
            self.assertEqual(landed["state"], "unknown", landed)
            self.assertIn("no git checkout", landed["proof"])
        # DIRT IS ASKED OF LANDED LANES ONLY: an unknown one is not asked, so
        # the key is present and null — never a clean false
        for r in [alpha] + beta:
            self.assertIn("dirty", r)
            self.assertIsNone(r.pop("dirty"))
        # NO GATE RUNS HERE (task/3130): every claim carries `on_gate`, null
        for r in [alpha] + beta:
            self.assertIn("on_gate", r)
            self.assertIsNone(r.pop("on_gate"))
        self.assertEqual(body["headline"]["lanes"]["landed"], 0)
        self.assertEqual(alpha,
                         {"lane": "lane-a", "kind": "claim",
                          "seats": ["alpha-claude", "seat-a"]})  # noqa: SEAT_NAME — the fixture seat above
        self.assertEqual(beta,
                         [{"lane": "lane-q", "kind": "claim",
                           "seats": ["kimi"]}])

    def test_a_claimless_live_seat_is_one_lane_on_the_project_it_works_in(self):
        """The owner's complaint: agents work on projects that claim no
        worktree lane. A live, usable seat with no claim is ONE lane on the
        project its cwd resolves to, with the same derivation helm scopes
        with."""
        got = self.board()["projects"]
        self.assertEqual(got["delta"]["running"],
                         [{"lane": None, "kind": "seat",
                           "seats": ["delta-seat"]}])
        self.assertEqual(got["alpha"]["running"][1],
                         {"lane": None, "kind": "seat", "seats": ["local-1"]})
        self.assertEqual(self.board()["headline"]["lanes"]["seats"], 2)

    def test_a_seat_holding_a_claim_is_counted_by_its_claim_never_twice(self):
        """seat-a works in delta's checkout and holds a lane on alpha: it is
        in alpha's claimed lane and in no seat lane anywhere."""
        got = self.board()["projects"]
        at_work = [s for rec in got.values() for r in rec.get("running", ())
                   if r["kind"] == "seat" for s in r["seats"]]
        self.assertIn("delta-seat", at_work)            # the control
        self.assertNotIn("seat-a", at_work)
        self.assertIn("seat-a", got["alpha"]["running"][0]["seats"])

    def test_a_paused_walled_red_absent_or_ephemeral_seat_is_no_lane(self):
        at_work = [s for rec in self.board()["projects"].values()
                   for r in rec.get("running", ()) if r["kind"] == "seat"
                   for s in r["seats"]]
        self.assertEqual(sorted(at_work), ["delta-seat", "local-1"])
        for idle in ("paused-1", "walled-1", "grok-1", "seat-b", "review-sa"):
            self.assertNotIn(idle, at_work, idle)

    def test_a_seat_whose_cwd_resolves_nowhere_is_named_never_guessed(self):
        body = self.board()
        self.assertEqual(body["headline"]["lanes"]["unscoped"], ["stray"])
        placed = [s for rec in body["projects"].values()
                  for r in rec.get("running", ()) for s in r["seats"]]
        self.assertNotIn("stray", placed)
        self.assertIn("kimi", placed)                   # the control

    def test_a_project_whose_only_sign_of_life_is_a_seat_at_work_stays_up(self):
        self.assertIs(self.board()["projects"]["gamma"]["quiet"], True)
        web_board._forget()
        self.roster["seats"].append(_seat("gamma-seat", None, "fresh", "codex",
                                          cwd=self.paths["gamma"]))
        got = self.board()["projects"]["gamma"]
        self.assertEqual(got["running"], [{"lane": None, "kind": "seat",
                                           "seats": ["gamma-seat"]}])
        self.assertIs(got["quiet"], False)

    def test_the_headline_is_the_sum_of_its_shares(self):
        """A claim on a project the registry does not hold still runs: it
        counts in R and in an `offboard` share, so the per-project shares and
        `offboard` add up to R exactly."""
        self.roster["claims"].append(_claim("off-board", "lane-x", "ghost"))
        body = self.board()
        lanes = body["headline"]["lanes"]
        self.assertEqual((lanes["running"], lanes["offboard"]), (5, 1))
        shares = sum(len(rec.get("running") or ())
                     for rec in body["projects"].values())
        self.assertEqual(shares + lanes["offboard"], lanes["running"])

    def test_an_unverified_claude_seat_is_read_as_anthropic(self):
        """The name fallback answers the HARNESS word; the flags are keyed by
        the credential word. Under a RED anthropic flag an unverified
        <project>-claude seat is not able to work, and its chip is the
        anthropic one."""
        self.assertTrue(burnflags.write_snapshot(inputs={"declarations": {
            "families": {"anthropic": {"colour": "RED",
                                       "until": self.now + 7200,
                                       "why": "out of credit"}}}},
            now=self.now))
        web._qstate.pop("flags", None)
        able = self.board()["headline"]["lanes"]["possible"]
        web_board._forget()
        self.roster["seats"].append(_seat("epsilon-claude", "alpha", "fresh",  # noqa: SEAT_NAME — a fixture seat named for its project and harness, not a real seat
                                          None, cwd=self.paths["epsilon"]))
        real = seat_mod.family_for
        harness = "claude"  # noqa: SEAT_NAME — the harness word the name door answers, not a seat
        with mock.patch.object(seat_mod, "family_for",
                               lambda name: (harness, None)
                               if name == "epsilon-claude" else real(name)):
            body = self.board()
        self.assertEqual(body["headline"]["lanes"]["possible"], able)
        fams = {f["family"]: f for f in body["projects"]["alpha"]["families"]}
        self.assertNotIn(harness, fams)
        self.assertIn("epsilon-claude", fams["anthropic"]["seats"])
        self.assertEqual(fams["anthropic"]["colour"], "RED")

    def test_M_counts_seats_able_to_work_now_one_each(self):
        """Up, not paused, not walled, and not on a RED family. The three
        seats that fail one leg each are the controls: without them every up
        seat would count and the number would be the presence count."""
        able = self.board()["headline"]["lanes"]["possible"]
        self.assertEqual(able, 6)
        live = [s for s in ROSTER["seats"]
                if s["presence"] != "absent" and not s.get("ephemeral")]
        self.assertEqual(len(live), 9)                  # 3 of them excluded

    def test_M_is_unknown_when_the_roster_is_unreadable(self):
        def broken(room):
            raise OSError("roster gone")
        with mock.patch.object(web, "_roster_cached", broken), \
                mock.patch.object(web, "_api_lr", self._api_lr):
            head = web._api_board()["headline"]
        self.assertEqual(head["lanes"], {"running": None, "possible": None,
                                         "claimed": None, "seats": None,
                                         "offboard": None, "landed": None,
                                         "unscoped": None})
        self.assertEqual(self.board()["headline"]["green"], 1)  # the control

    def test_a_light_he_sets_shows_at_once_and_the_joins_stay_cached(self):
        first = self.board()
        self.assertEqual(first["headline"]["green"], 1)
        joins = lambda: {k: self.calls[k] for k in ("roster", "lr")}
        self.assertEqual(joins(), {"roster": 1, "lr": 1})
        got, status = web._api_projects_state(
            {"name": "beta", "colour": "green", "reason": "go"})
        self.assertEqual(status, 200, got)
        second = self.board()
        self.assertEqual(second["headline"]["green"], 2,
                         "the owner's write waited out the join cache")
        # (the all-projects read is asked on every response by design: its
        # own serve-stale cache decides what that costs)
        self.assertEqual(joins(), {"roster": 1, "lr": 1},
                         "the expensive joins were rebuilt inside the TTL")

    # -- the teams leg and its write door (task/3156) --------------------

    def team(self, *members, **shares):
        return {"members": [{"seat": s, "family": f, "role": r}
                            for s, f, r in members], "shares": shares}

    def test_the_teams_leg_joins_an_authored_team_with_its_own_clock(self):  # noqa: VACUOUS_ASSERTION — the absent team before the write is the control for the equalities on the team after it
        from helm import teams
        before = self.board()
        self.assertNotIn("team", before["projects"]["alpha"])   # the control
        self.assertIs(before["sections"]["teams"]["stale"], False)
        row, problem, _code = teams.write(
            "alpha", self.team(("alpha-codex", "codex", "builder"), codex=30),
            0, by="owner", reason="alpha leads codex", apply=True,
            post=lambda body, room: None)
        self.assertIsNone(problem)
        web_board._forget()
        got = self.board()
        team = got["projects"]["alpha"]["team"]
        self.assertEqual((team["v"], team["authored"], team["binding"]),
                         (1, True, True))
        self.assertEqual(team["shares"], {"codex": 30})
        self.assertEqual(team["members"][0]["state"], "wanted")
        self.assertEqual(team["history"][-1]["reason"], "alpha leads codex")
        sec = got["sections"]["teams"]
        self.assertEqual(sec["source"], "helm team")
        self.assertIsInstance(sec["measured_at"], float)
        self.assertIn("RED", sec["say"])
        self.assertEqual(sorted(sec["roles"]),
                         ["builder", "checker", "lead", "reviewer"])
        # THE LANES AND THE TIER RIDE THE SECTION: the card folds a slot share
        # and drops at E3 what route drops only when both reach it
        self.assertIn("qwen27", sec["slots"]["families"])
        self.assertIn("codex", sec["tier"]["families"])
        self.assertEqual(sec["tier"]["kinds"], ["review", "verify"])

    def test_the_team_door_answers_200_400_and_409(self):
        with mock.patch("helm.chat.post") as posted:
            body, status = web._api_projects_team({
                "name": "alpha", "expected": 0, "reason": "first team",
                "team": self.team(("alpha-codex", "codex", "builder"),
                                  codex=30)})
            self.assertEqual(status, 200, body)
            self.assertEqual((body["ok"], body["v"]), (True, 1))
            self.assertEqual(posted.call_count, 1)
            self.assertEqual(posted.call_args.kwargs["room"], "alpha")
            # the version moved: the same save again is STALE and writes nothing
            body, status = web._api_projects_team({
                "name": "alpha", "expected": 0, "reason": "again",
                "team": self.team(codex=60)})
            self.assertEqual((status, body["code"]), (409, "stale"), body)
            self.assertIn("v1 now", body["error"])
            # an invalid team, a missing version, an unknown project
            body, status = web._api_projects_team({
                "name": "alpha", "expected": 1, "reason": "x",
                "team": self.team(("alpha-codex", "codex", "boss"))})
            self.assertEqual((status, body["code"]), (400, "invalid"), body)
            body, status = web._api_projects_team({
                "name": "alpha", "reason": "x", "team": self.team()})
            self.assertEqual(status, 400, body)
            body, status = web._api_projects_team({
                "name": "nowhere", "expected": 0, "reason": "x",
                "team": self.team()})
            self.assertEqual(status, 400, body)
            # A ROSTER THAT DID NOT READ (round 3, ruling c): the family door
            # cannot check the members, so the save is refused, 503, and
            # names the class
            from helm import seats_roster
            with mock.patch.object(seats_roster, "roster_checked",
                                   return_value=({}, True)):
                body, status = web._api_projects_team({
                    "name": "alpha", "expected": 1, "reason": "roster torn",
                    "team": self.team(("alpha-codex", "codex", "builder"),
                                      codex=60)})
            self.assertEqual((status, body.get("code")), (503, "unread"),
                             body)
            self.assertIn("RosterUnread", body["error"])
        from helm import teams
        self.assertEqual(teams.authored_only("alpha")["shares"],
                         {"codex": 30}, "a refused save wrote something")

    def test_a_saved_team_shows_on_the_next_board_read(self):
        """The door drops the kept teams reading: the owner's save must not
        wait out the leg's cache, or his next save meets a 409 against the
        version the page is still showing."""
        self.assertNotIn("team", self.board()["projects"]["alpha"])
        with mock.patch("helm.chat.post"):
            body, status = web._api_projects_team({
                "name": "alpha", "expected": 0, "reason": "first team",
                "team": self.team(("alpha-codex", "codex", "builder"))})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.board()["projects"]["alpha"]["team"]["v"], 1)

    def test_the_declare_door_is_worse_only(self):  # noqa: ORPHANED_MOCK — the door reads burnflags.family_flag through its module attribute, and the 400 not-worse answer is the double firing
        flag = {"colour": "ORANGE", "axes": {"money": "ORANGE"},
                "expires_at": time.time() + 3600}
        with mock.patch.object(burnflags, "family_flag", return_value=flag):
            body, status = web._api_burn_declare({
                "family": "codex", "colour": "YELLOW", "until": "24h",
                "reason": "an improvement"})
            self.assertEqual((status, body["code"]), (400, "not-worse"), body)
            self.assertIn("raise its share", body["error"])
            body, status = web._api_burn_declare({
                "family": "codex", "colour": "RED", "until": "reset",
                "reason": "hold codex for the release"})
        self.assertEqual(status, 200, body)
        said = burnflags.read_declarations()["families"]["codex"]
        self.assertEqual((said["colour"], said["until"]),
                         ("RED", flag["expires_at"]))
        body, status = web._api_burn_declare({
            "family": "codex", "colour": "RED", "until": "24h", "reason": ""})
        self.assertEqual(status, 400, body)

    def test_the_endpoint_is_registered_and_served(self):
        self.assertIs(web.API["/api/board"], web._api_board)
        self.assertIs(web.POST_API["/api/projects/team"],
                      web._api_projects_team)
        self.assertIs(web.POST_API["/api/burn/declare"],
                      web._api_burn_declare)
        self.assertIn("/api/projects/state", web.POST_API)   # the write door
        self.assertNotIn("/api/board", web.POST_API)
        srv = web.make_server(0)
        self.addCleanup(srv.server_close)
        # shutdown() waits one serve_forever poll (stdlib default 0.5s); see
        # helm/mcpd.serve_background for the poll trade.
        t = threading.Thread(target=srv.serve_forever,
                             kwargs={"poll_interval": 0.01}, daemon=True)
        t.start()
        self.addCleanup(srv.shutdown)
        with mock.patch.object(web, "_roster_cached", self._roster), \
                mock.patch.object(web, "_api_lr", self._api_lr):
            url = "http://127.0.0.1:%d/api/board" % srv.server_address[1]
            with urllib.request.urlopen(url, timeout=10) as r:
                self.assertEqual(r.status, 200)
                body = json.loads(r.read().decode("utf-8"))
        self.assertEqual(body["headline"]["green"], 1)
        self.assertIn("alpha", body["projects"])


# ---------------------------------------------------------------------------
# the browser half

_DECL = r"^(?:const|let) %s = .*;$"

# THE TEAM CARD'S FUNCTIONS AND STATE (task/3156): the board's open row and
# its burn card now draw through them, so every node run of the board lifts
# them with it.
TEAM_FNS = ("teamTokens", "teamRatio", "teamEff", "teamShort", "teamMode",
            "teamLane",
            "teamSlots", "teamWord", "teamPct", "teamBurning",
            "teamFailedText",
            "teamCurrent", "teamDraft", "teamLightRed", "teamAlloc",
            "teamLine", "teamTabHref", "famHref", "shareBar", "slotBar",
            "teamCredits", "famBilled", "famSheet",
            "teamMini",
            "teamFamRow", "teamMemberRow", "teamAddRow", "teamDiff",
            "teamDriftLines", "teamRoute", "teamHistory", "teamParts",
            "teamSplit", "teamFold",
            "teamSection")
TEAM_DECLS = ("TEAM_DRAFT", "TEAM_MSG", "TEAM_KIND", "FAM_SEL", "FAM_MSG",
              "TEAM_KINDS", "TEAM_RANK", "TEAM_PALETTE", "TEAM_FOLDS")
# the accounts table's groups, which the family sheet links into (the credit
# page's own read; null until it has answered)
TEAM_DECLS += ("QUOTA_GROUPS",)


def _flag(colour, cause):
    return {"colour": colour, "cause": cause, "axis": "declared",
            "provenance": "owner-declared", "expires_at": None}


def _sec(**extra):
    base = {"source": "x", "measured_at": 1, "age_s": 5, "limit_s": 600,
            "stale": False, "unavailable": None}
    base.update(extra)
    return base


# A LEG PAST ITS BUDGET, as the server sends it: no reading yet, no clock,
# and asked for again soon
_READING = {"loading": True, "retry": True, "measured_at": None,
            "age_s": None, "unavailable": None}


def _repo_row(remote, slug, visibility, why=None, stale=False):
    return {"remote": remote, "slug": slug,
            "url": "https://github.com/" + slug if slug else None,
            "visibility": visibility, "why": why, "checked_at": 1,
            "stale": stale}


def _board(running=3, possible=5, green=1, **over):
    """A readable board body for the renderers."""
    sections = {
        "lights": _sec(limit_s=None),
        "flags": _sec(limit_s=2400, families={
            "anthropic": _flag("ORANGE", "one account left"),
            "codex": _flag("YELLOW", "weekly window half spent")},
            overall={"colour": "ORANGE", "family": "anthropic",
                     "until": None, "capacity": 1}),
        "tasks": _sec(unscoped=0, unplaced=0),
        "seats": _sec(unplaced=0),
        "lands": _sec(scope="alpha"),
        "trunk": _sec(),
        "teams": _sec(seats=[], pace={}, burn=None, say={}, roles={}),
        "fleet": _sec(unplaced=0),
        "gate": _sec(unplaced=0, unknown_hosts=[]),
    }
    for name, patch in over.items():
        sections[name] = dict(sections[name], **patch)
    quiet = {"tasks": {"open": 0, "in_progress": 0, "top": []},
             "seats": [], "families": [], "quiet": True}
    return {"headline": {"lanes": {"running": running, "possible": possible,
                                   "claimed": None if running is None else 2,
                                   "seats": None if running is None else 1,
                                   "offboard": None if running is None
                                   else 0,
                                   "unscoped": None if running is None
                                   else ["stray"]},
                         "green": green, "colour": "ORANGE"},
            "sections": sections,
            "projects": {
                "alpha": {
                    "tasks": {"open": 3, "in_progress": 1, "top": [
                        {"id": "task/1", "title": "wire the board",
                         "priority": "P1", "status": "in_progress"}]},
                    "seats": [{"seat": "alpha-claude", "presence": "fresh",
                               "family": "anthropic"},
                              {"seat": "local-1", "presence": "fresh",
                               "family": "localllm"}],
                    "families": [
                        {"family": "anthropic", "colour": "ORANGE",
                         "cause": "one account left",
                         "seats": ["alpha-claude"]},
                        {"family": "localllm", "colour": None, "cause": None,
                         "seats": ["local-1"]}],
                    "running": [{"lane": "lane-a", "kind": "claim",
                                 "seats": ["alpha-claude"]},
                                {"lane": "lane-b", "kind": "claim",
                                 "seats": ["seat-a"]},
                                {"lane": None, "kind": "seat",
                                 "seats": ["local-1"]}],
                    "lanes": {"in_flight": 5, "filed": ["lane-a"],
                              "building": 1, "building_lanes": ["lane-c"],
                              "loops": [
                                  {"id": "r1", "lane": "lane-a",
                                   "state": "AWAITING_REVIEW"},
                                  {"id": "r3", "lane": "lane-g",
                                   "state": "READY"},
                                  {"id": "r4", "lane": "lane-f",
                                   "state": "CHANGES_REQUESTED"},
                                  {"id": "r5", "lane": "lane-m",
                                   "state": "MERGED_LOCAL"},
                                  {"id": "r6", "lane": "lane-o",
                                   "state": "OPEN"}]},
                    "landed": [{"lane": "lane-z", "task": "task/9",
                                "age_s": 3600}],
                    "owner": {"holds": 1},
                    "waits": [{"label": "integrator", "count": 1,
                               "oldest_age_s": 600, "rows": [
                                   {"plain_title": "lane-a",
                                    "stage_class": "review", "age_s": 600}]}],
                    "progress": {"lands7": 12, "opened7": 4, "closed7": 9},
                    "repos": [_repo_row("origin", "akapug/alpha", "private"),
                              _repo_row("upstream", "emberian/alpha",
                                        "public")],
                    "quiet": False,
                    "last_land": {"at": 1, "age_s": 3600, "how": "push",
                                  "sha": "4611958e44a2", "ref": "main"}},
                "beta": {"tasks": {"open": 1, "in_progress": 0, "top": []},
                         "seats": [], "families": [], "quiet": False,
                         "running": [{"lane": "lane-q", "kind": "claim",
                                      "seats": ["kimi"]}],
                         "pipeline": {
                             "loops": [{"id": "b1", "lane": "b-review",
                                        "state": "AWAITING_REVIEW"},
                                       {"id": "b2", "lane": "b-fix",
                                        "state": "CHANGES_REQUESTED"}],
                             "landed": [{"lane": "b-landed", "task": None,
                                         "age_s": 60}]},
                         "progress": {"lands7": None, "opened7": 1,
                                      "closed7": 0},
                         "repos": None,
                         "repos_unavailable": "no git checkout at the "
                                              "registered path",
                         "last_land": {"unavailable": "not a git checkout"}},
                "quietone": quiet, "litold": quiet, "never": quiet,
                "lit": dict(quiet, quiet=False),
                "beaty": dict(quiet, quiet=False,
                              active_at=QUIET_NOW - 60)}}


def _row(name, colour=None, status="active", last_seen=1900000000, ts=None):
    lit = ({"colour": colour, "authored": True, "reason": "fixture",
            "by": "owner", "ts": ts, "key": name} if colour else
           {"colour": status, "authored": False, "reason": "", "by": "",
            "ts": None, "key": name})
    return {"name": name, "path": "/fake/dev/" + name, "status": status,
            "last_seen": last_seen, "sessions": {"harness-a": 1}, "light": lit}


# THE QUIET FOLD'S WORLD, on one fixed clock. The server decides which rows are
# quiet (tests above); the page folds exactly those, and only while the backlog
# the decision rests on is current.
QUIET_NOW = 1900000000
_OLD = QUIET_NOW - 40 * 86400
QROWS = [_row("quietone", last_seen=_OLD),
         _row("beta", last_seen=_OLD),                   # open work
         _row("lit", "red", last_seen=_OLD, ts=QUIET_NOW - 5 * 86400),
         _row("litold", "red", last_seen=_OLD, ts=_OLD),  # an old light
         _row("beaty", last_seen=_OLD),                  # a seat beat today
         _row("never", last_seen=None)]

ROWS = [_row("zeta"), _row("red1", "red"), _row("g-old", "green", last_seen=1),
        _row("yel", "yellow"), _row("org", "orange"), _row("g-new", "green"),
        _row("quiet", status="dormant", last_seen=5)]

# THE OWNER'S QUEUE, as the two reads hand it over: the decisions door and the
# land pipeline's scheduler model.
_ODQ_NONE = {"counts": {"open": 0, "undelivered": 0}, "entries": []}
_ODQ_TWO = {"counts": {"open": 2, "undelivered": 0}, "entries": [{}, {}]}


def _lr_model(asks=(), holds=(), **extra):
    model = {"owner_asks": list(asks), "owner_ask_count": len(asks),
             "owner_asks_dropped": 0, "owner_holds": list(holds)}
    model.update(extra)
    return {"read_age_s": 4, "scheduler": model}


class BoardRendererRuntimeTest(unittest.TestCase):
    """The board's renderers, lifted out of the SHIPPED page and run."""

    FNS = ("age", "ago", "light", "pkey", "lineageN", "lightset", "dmsg",
           "detailBody", "detailPane", "setLight", "lrDur", "lrAgo",
           "cardBoundS", "cardStale",
           "flagWhen", "flagsHTML", "boardRank", "boardSort", "boardSec",
           "boardSecState", "boardSecWord", "boardQuiet", "boardLayout",
           "boardCapacity", "boardTeam", "boardCount",
           "boardLanes", "boardLaneWord", "boardProgress", "boardRepoBadge",
           "boardRepos", "boardLand", "boardWide", "boardDetail", "projTab",
           "boardRowHTML", "onYouRead", "lrStale") + TEAM_FNS
    CONSTS = ("LIGHTS", "FLAGCOL", "LIGHT_RANK", "PROJ_TABS")
    DECLS = ("DETAIL_HAVE", "DETAIL_ROUTE", "BOARD_DIRTY") + TEAM_DECLS

    SUPPORT = r"""
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const POSTED = [];
let RELOADS = 0;
async function post(url, body) { POSTED.push({url, body}); return {ok: true}; }
async function boardReload() { RELOADS++; }
const _rowjs = name => ({name, path: "/fake/dev/" + name, status: "active",
  last_seen: 1900000000, sessions: {}, light: {colour: "active", authored: false}});
// THE WORK PAGE (scripts/52-work.js.part, run in tests/test_web_work_page.py)
// stands in by name: a row's count is its share of the one work read, and a
// project's Work tab is that page locked to the project. `WORK` is the read.
let WORK = null;
const WK = {proj: {lock: null, view: {}}};
function wkProjectCount(d, key) {
  return '<span class="bcount">work of ' + key + (d ? " read" : " unread") + "</span>";
}
function wkProjFor(key) { WK.proj.lock = key; }
function wkHTML(inst) { return '<section class="wk" data-wk="proj" data-lock="' + inst.lock + '"></section>'; }
function fakeBox(key, reason) {
  const msg = {textContent: ""};
  const why = {value: reason, focused: false, focus() { this.focused = true; }};
  return {dataset: {key}, msg, why,
          querySelector: s => s === ".lightmsg" ? msg : s === ".lightwhy" ? why : null};
}
"""

    DRIVER = r"""
(async () => {
const out = {};
out.c_ok = boardCapacity(BOARD, 12);
const OTHER = JSON.parse(JSON.stringify(BOARD));
OTHER.headline.lanes.running = 5;
OTHER.headline.lanes.offboard = 1;
out.c_other = boardCapacity(OTHER, 12);
out.c_noactive = boardCapacity(BOARD);
const LANDED_HELD = JSON.parse(JSON.stringify(BOARD));
LANDED_HELD.headline.lanes.landed = 2;
out.c_landed = boardCapacity(LANDED_HELD, 12);
const NONE_LANDED = JSON.parse(JSON.stringify(BOARD));
NONE_LANDED.headline.lanes.landed = 0;
out.c_none_landed = boardCapacity(NONE_LANDED, 12);
out.c_noregistry = boardCapacity(BOARD_NOREGISTRY, 12);
const HARNESS_ROWS = [Object.assign(_rowjs("zed"), {sessions: {"harness-x": 3}}), _rowjs("yan")];
out.q_harness = boardLayout(HARNESS_ROWS, BOARD, "harness-x", false).shown.map(p => p.name);
out.c_pending = boardCapacity(null);
out.c_failed = boardCapacity({unavailable: "/api/board -> 500"});
out.c_noroster = boardCapacity(BOARD_NOROSTER);
out.c_seats_stale = boardCapacity(BOARD_SEATS_STALE);
out.c_flags_stale = boardCapacity(BOARD_FLAGS_STALE);
out.c_unmeasured = boardCapacity(BOARD_UNMEASURED);
out.c_nolights = boardCapacity(BOARD_NOLIGHTS);
out.c_reading = boardCapacity(BOARD_READING, 12);
out.row_reading = boardRowHTML(ALPHA, BOARD_READING, false);
out.open_reading = boardRowHTML(ALPHA, BOARD_READING, true);
out.open_reading_work = boardRowHTML(ALPHA, BOARD_READING, true, "work");
out.open_reading_about = boardRowHTML(ALPHA, BOARD_READING, true, "about");
out.flags_reading = flagsHTML(BOARD_READING.sections.flags);
out.sorted = boardSort(ROWS).map(p => p.name);
out.team_none = boardTeam(ALPHA, BOARD.projects.alpha, BOARD);
out.team_reading = boardTeam(ALPHA, BOARD.projects.alpha, BOARD_READING);
out.collapsed = boardRowHTML(ALPHA, BOARD, false);
out.expanded = boardRowHTML(ALPHA, BOARD, true);
out.expanded_work = boardRowHTML(ALPHA, BOARD, true, "work");
out.expanded_about = boardRowHTML(ALPHA, BOARD, true, "about");
out.expanded_beta = boardRowHTML(BETA, BOARD, true, "work");
out.tab_bogus = boardRowHTML(ALPHA, BOARD, true, "nonsense");
// AN OLD TAB NAME (tab=lanes, tab=tasks) is no tab any more: `projRoute`
// maps those addresses to the Work tab, and a name the strip lacks is Team
out.tab_lanes = boardRowHTML(ALPHA, BOARD, true, "lanes");
WORK = {d: {cards: []}, at: 0};
out.collapsed_read = boardRowHTML(ALPHA, BOARD, false);
WORK = null;
out.seats_stale = boardRowHTML(ALPHA, BOARD_SEATS_STALE, false);
out.flags_ok = flagsHTML(BOARD.sections.flags);
out.flags_unmeasured = flagsHTML(BOARD_UNMEASURED.sections.flags);
out.flags_stale = flagsHTML(BOARD_FLAGS_STALE.sections.flags);
out.lanes_alpha = boardLanes(BOARD.projects.alpha, BOARD);
out.lanes_none = boardLanes({running: []}, BOARD);
out.lanes_claimed_only = boardLanes(BOARD.projects.beta, BOARD);
out.lanes_stale = boardLanes(BOARD_SEATS_STALE.projects.alpha, BOARD_SEATS_STALE);
out.p_alpha = boardProgress(BOARD.projects.alpha, BOARD);
out.p_grow = boardProgress({progress: {lands7: 1, opened7: 5, closed7: 2}}, BOARD);
out.p_even = boardProgress({progress: {lands7: 0, opened7: 2, closed7: 2}}, BOARD);
out.p_beta = boardProgress(BOARD.projects.beta, BOARD);
out.p_tasks_stale = boardProgress(BOARD_TASKS_STALE.projects.alpha, BOARD_TASKS_STALE);
out.p_trunk_stale = boardProgress(BOARD_TRUNK_STALE.projects.alpha, BOARD_TRUNK_STALE);
out.p_master = boardProgress({progress: {lands7: 3, opened7: 1, closed7: 1},
  last_land: {at: 1, age_s: 60, how: "push", sha: "4611958e44a2", ref: "master"}}, BOARD);
out.r_fork = boardRepos(BOARD.projects.alpha);
out.r_unread = boardRepos(BOARD.projects.beta);
out.r_none = boardRepos({repos: []});
out.r_nojoin = boardRepos(undefined);
const MANY = {repos: [
  {remote: "origin", slug: "akapug/p", url: "https://github.com/akapug/p", visibility: "private"},
  {remote: "mirror", slug: "akapug/p", url: "https://github.com/akapug/p", visibility: "private"},
  {remote: "box-a", slug: null, url: null, visibility: "unknown", why: "not a GitHub remote"},
  {remote: "box-b", slug: null, url: null, visibility: "unknown", why: "not a GitHub remote"}]};
out.r_many_cell = boardRepos(MANY, false);
out.r_many_open = boardRepos(MANY, true);
out.r_offonly = boardRepos({repos: [MANY.repos[2]]}, false);
out.b_unknown = boardRepoBadge({remote: "origin", slug: null, url: null,
  visibility: "unknown", why: "not a GitHub remote"});
out.b_ghfail = boardRepoBadge({remote: "origin", slug: "akapug/x",
  url: "https://github.com/akapug/x", visibility: "unknown", why: "gh: not logged in"});
out.b_pending = boardRepoBadge({remote: "origin", slug: "akapug/x",
  url: "https://github.com/akapug/x", visibility: "pending"});
out.b_unasked = boardRepoBadge({remote: "origin", slug: "akapug/x",
  url: "https://github.com/akapug/x", visibility: "unasked"});
out.b_public = boardRepoBadge({remote: "origin", slug: "akapug/x",
  url: "https://github.com/akapug/x", visibility: "public", stale: true});
out.lanes_seats_only = boardLanes({running: [{lane: null, kind: "seat", seats: ["g-1"]},
  {lane: null, kind: "seat", seats: ["g-2"]}]}, BOARD);
out.y_quiet = onYouRead(ODQ_NONE, LR_NONE);
out.y_decisions = onYouRead(ODQ_TWO, LR_NONE);
out.y_asks = onYouRead(ODQ_NONE, LR_ASKS);
out.y_holds = onYouRead(ODQ_NONE, LR_HOLDS);
out.y_unread = onYouRead(undefined, undefined);
out.y_half = onYouRead(ODQ_NONE, undefined);
out.y_lr_down = onYouRead(ODQ_NONE, {unavailable: "pipeline UNKNOWN"});
out.y_lr_stale = onYouRead(ODQ_NONE, Object.assign({}, LR_NONE, {read_age_s: 9999}));
out.y_odq_down = onYouRead({unavailable: true, why: "ledger gone"}, LR_NONE);
const lay = boardLayout(QROWS, BOARD, "", false);
const lit = boardLayout(ROWS, BOARD, "", false, "green");
out.lit_green = [lit.lead.map(p => p.name), lit.quiet.length];
out.lit_unset = boardLayout(ROWS, BOARD, "", false, "unset").shown.map(p => p.name);
out.q_lead = lay.lead.map(p => p.name);
out.q_fold = lay.quiet.map(p => p.name);
out.q_shown = lay.shown.length;
const found = boardLayout(QROWS, BOARD, "quietone", false);
out.q_found = [found.lead.map(p => p.name), found.quiet.length, found.shown.length];
const unread = boardLayout(QROWS, BOARD_TASKS_DOWN, "", false);
out.q_unread = unread.quiet.map(p => p.name);
out.land_ok = boardWide(ALPHA, BOARD.projects.alpha, BOARD);
out.land_none = boardWide(BETA, BOARD.projects.beta, BOARD);
out.land_nojoin = boardWide(GAMMA, undefined, BOARD);
out.land_stale = boardWide(ALPHA, BOARD_TRUNK_STALE.projects.alpha, BOARD_TRUNK_STALE);
// THE PROJECT THE TRAIN LANDS: its newest train's push, off the work read
WORK = {d: {trains: {project: "alpha", recent: [{name: "train9", land: 40,
  at: Date.now() / 1000 - 7200}]}}, at: Date.now()};
out.land_train = boardWide(ALPHA, BOARD.projects.alpha, BOARD);
out.land_train_other = boardWide(BETA, BOARD.projects.beta, BOARD);
// A QUIET DAY: no train in the day's window; the last one landed three days
// ago, or the last one was vetoed and none landed in the window
WORK = {d: {trains: {project: "alpha", recent: [], last: {name: "train8", state: "DONE",
  land: 39, at: Date.now() / 1000 - 3 * 86400}}}, at: Date.now()};
out.land_train_quiet = boardWide(ALPHA, BOARD.projects.alpha, BOARD);
WORK = {d: {trains: {project: "alpha", recent: [], last: {name: "train10", state: "VETOED",
  land: null, at: Date.now() / 1000 - 600}}}, at: Date.now()};
out.land_train_none = boardWide(ALPHA, BOARD.projects.alpha, BOARD);
WORK = null;
out.active_join = boardWide(ALPHA_OLD, {active_at: Date.now() / 1000 - 60}, BOARD);
out.active_old = boardWide(ALPHA_OLD, undefined, BOARD);
out.active_none = boardWide(NEVER, undefined, BOARD);
const box = fakeBox("alpha", "because it is the one that ships");
await setLight(box, "green");
const bare = fakeBox("alpha", "   ");
await setLight(bare, "red");
out.posted = POSTED;
out.reloads = RELOADS;
out.bare_msg = bare.msg.textContent;
out.bare_focused = bare.why.focused;
out.setter_key = lightset(ALPHA).match(/data-key='([^']*)'/)[1];
console.log(JSON.stringify(out));
})();
"""

    @classmethod
    def setUpClass(cls):
        from tests.test_web_chat_client_runtime import _extract_fn
        from tests.test_web_accounts import _extract_const
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        src = web_ui_loader.read_text()
        cls.src = src

        def decl(name):
            m = re.search(_DECL % re.escape(name), src, re.M)
            assert m, "no declaration of %s in the assembled page" % name
            return m.group(0)

        fns = "\n\n".join(_extract_fn(src, n) for n in cls.FNS)
        consts = "\n".join(_extract_const(src, n) for n in cls.CONSTS)
        decls = "\n".join(decl(n) for n in cls.DECLS)
        worlds = {
            "BOARD": _board(),
            "BOARD_NOROSTER": _board(running=None, possible=None, seats={
                "unavailable": "the roster could not be read (OSError)"}),
            "BOARD_SEATS_STALE": _board(seats={"stale": True,
                                               "age_s": 99999}),
            "BOARD_UNMEASURED": _board(flags={
                "unavailable": "no fresh burn-flag snapshot", "families": {},
                "overall": None, "measured_at": None, "age_s": None}),
            "BOARD_FLAGS_STALE": _board(flags={"stale": True, "age_s": 9000}),
            "BOARD_NOLIGHTS": _board(green=None, lights={
                "unavailable": "registry unreadable"}),
            "BOARD_TASKS_STALE": _board(tasks={"stale": True,
                                               "age_s": 99999}),
            "BOARD_TASKS_DOWN": _board(tasks={
                "unavailable": "ledger unreadable"}),
            "BOARD_NOREGISTRY": _board(running=None, possible=5),
            "BOARD_TRUNK_STALE": _board(trunk={"stale": True,
                                               "age_s": 99999}),
            # THE FIRST READ AFTER A RESTART: every leg past its budget
            "BOARD_READING": _board(running=None, possible=None, **{
                name: dict(_READING, **extra) for name, extra in (
                    ("seats", {}), ("tasks", {}), ("trunk", {}),
                    ("teams", {}),
                    ("lands", {"scope": None}),
                    ("flags", {"families": {}, "overall": None}),
                    ("fleet", {"loading": False, "retry": False,
                               "measured_at": 1, "age_s": 5,
                               "scope": "alpha"}))}),
            "QROWS": QROWS, "QUIET_NOW": QUIET_NOW,
            "BETA": _row("beta"),
            "ROWS": ROWS,
            "ALPHA": _row("alpha", "green"),
            "ALPHA_OLD": _row("alpha", "green", last_seen=86400),
            "NEVER": _row("never", last_seen=None),
            "GAMMA": _row("gamma"),
            "ODQ_NONE": _ODQ_NONE, "ODQ_TWO": _ODQ_TWO,
            "LR_NONE": _lr_model(),
            "LR_ASKS": _lr_model(asks=[{"plain_title": "pick a name",
                                        "age_s": 600, "age_known": True}]),
            "LR_HOLDS": _lr_model(holds=[{"plain_title": "lane-a",
                                          "age_s": 60, "age_known": True}]),
        }
        payload = "".join("const %s = %s;\n" % (k, json.dumps(v))
                          for k, v in worlds.items())
        cls.tmp = tempfile.mkdtemp(prefix="helm-web-board-runtime-")
        path = os.path.join(cls.tmp, "run.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(cls.SUPPORT + consts + "\n" + decls + "\n" + payload
                    + fns + cls.DRIVER)
        chk = subprocess.run([cls.node, "--check", path],
                             capture_output=True, text=True)
        assert chk.returncode == 0, "node --check failed:\n" + chk.stderr
        cls.proc = subprocess.run([cls.node, path], capture_output=True,
                                  text=True, timeout=60)
        try:
            cls.out = json.loads(cls.proc.stdout or "{}")
        except json.JSONDecodeError:
            cls.out = {}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def setUp(self):
        self.assertTrue(self.out, "node produced no output: "
                        + (self.proc.stderr or "")[:600])

    # -- the capacity line ---------------------------------------------------

    def test_the_capacity_line_states_lanes_and_seats_as_two_facts(self):
        """Not a fraction: a seat running subagents holds several lanes, so
        lanes can outnumber seats, and "R of M" read as nonsense then."""
        c = self.out["c_ok"]
        self.assertIn("3 lanes running</b> · <b>5 seats able to work", c)
        self.assertNotIn("running of", c)
        self.assertNotIn("possible", c)
        self.assertIn("1 green", c)
        # a tap opens Fleet's seats
        self.assertIn('href="#roster"', c)

    def test_R_says_how_many_of_its_lanes_are_landed_with_the_lease_held(self):
        """R STAYS LIVE CLAIMS — a land releases no lease — and says how many
        of them are already on main, off `headline.lanes.landed`, the count
        the server takes from the same running[] verdicts the kanban draws."""
        text = re.sub(r"<[^>]*>", "", self.out["c_landed"])
        self.assertIn("3 lanes running, 2 of them landed (lease still held)"
                      " · 5 seats able to work", text)
        # THE CONTROLS: absent (an older server) and zero both say nothing
        for key in ("c_ok", "c_none_landed"):
            self.assertNotIn("landed", re.sub(r"<[^>]*>", "", self.out[key]),
                             key)

    def test_the_capacity_line_lists_no_project(self):  # noqa: VACUOUS_ASSERTION — "lanes running" is asserted on the same text first, the unconditional positive control
        """EACH PROJECT IS LISTED ONCE (task/3445): its lanes are on its own
        line, so the capacity line no longer names projects."""
        text = re.sub(r"<[^>]*>", "", self.out["c_ok"])
        self.assertIn("lanes running", text)
        self.assertNotIn("alpha 3", text)
        self.assertNotIn("beta 1", text)

    def test_the_active_count_rides_beside_green(self):
        """The overview's "N active" (the scan's own word) was lost with the
        card grid; it is restored on the lanes line, and only when counted."""
        self.assertIn("1 green · 12 active", self.out["c_ok"])
        self.assertNotIn("active", self.out["c_noactive"].split("green")[1][:40])

    def test_lanes_on_no_projects_line_are_said_so_the_lines_add_up(self):
        self.assertIn("1 on no project's line", self.out["c_other"])
        # no off-board claim, nothing said (read the text, not the markup)
        self.assertNotIn("no project's line",
                         re.sub(r"<[^>]*>", "", self.out["c_ok"]))

    def test_an_unreadable_registry_is_blamed_not_the_roster(self):
        # what he READS: the text, not the markup (the tap link is #roster)
        text = re.sub(r"<[^>]*>", "", self.out["c_noregistry"])
        self.assertIn("registry could not be read", text)
        self.assertNotIn("roster", text)
        self.assertIn("5 seats able to work", text)      # the roster still read

    def test_a_leg_still_being_read_says_so_never_unknown_or_zero(self):  # noqa: VACUOUS_ASSERTION — every absent word is read off a text the same arm unconditionally asserts carries "still being read" or "reading", so an empty render fails first
        """What he READS on the first paint after a restart, the text and not
        the markup: each part still being read says so. The page it replaces
        drew every row as "seats ? / ? open / lanes ? / land ?" under "board
        UNKNOWN"."""
        text = lambda html: re.sub(r"<[^>]*>", "", html)
        cap = text(self.out["c_reading"])
        self.assertIn("still being read", cap)
        self.assertNotIn("UNKNOWN", cap)
        self.assertIn("1 green · 12 active", cap)        # the lights still read
        row = text(self.out["row_reading"])
        self.assertIn("reading", row)
        for unknown in ("? open", "lanes ?", "land ?", "seats ?", "0 open",
                        "no lanes", "STALE"):
            self.assertNotIn(unknown, row, unknown)
        # EACH TAB, still being read, says so in its own words (task/3445);
        # the Work tab is the Work page's, which says what IT has read
        # (tests/test_web_work_page.py OneCountEverywhereTest)
        opened = text(self.out["open_reading"])          # the Team tab
        about = text(self.out["open_reading_about"])
        self.assertIn("team still being read", opened)
        self.assertIn("newest on main still being read", about)
        for got in (opened, about):
            self.assertNotIn("UNKNOWN", got)
        self.assertNotIn("nobody is seated", opened)     # never an answer
        self.assertIn('data-wk="proj" data-lock="alpha"', self.out["open_reading_work"])
        flags = text(self.out["flags_reading"]["head"])
        self.assertIn("still being read", flags)
        self.assertNotIn("not measured", flags)

    def test_the_filter_matches_a_harness_name_too(self):
        self.assertEqual(self.out["q_harness"], ["zed"])

    def test_a_seat_working_where_no_project_claims_is_named(self):
        c = self.out["c_ok"]
        self.assertIn("1 seat unscoped", c)
        self.assertIn("stray", c)
        self.assertIn("2 claimed", c)
        self.assertIn("1 seat at work", c)

    def test_the_about_n_projects_phrasing_is_retired(self):
        for key in ("c_ok", "c_noroster", "c_unmeasured", "c_flags_stale"):
            self.assertNotIn("at once", self.out[key], key)
            self.assertNotIn("capacity: about", self.out[key], key)
        self.assertNotIn("at once", self.out["flags_ok"]["head"])
        self.assertIn("lanes running", self.out["c_ok"])  # the control

    def test_the_capacity_line_keeps_its_counts_and_no_family_chip(self):
        """Supply is Fleet › credit's (task/3445 L4): the line under Work ›
        projects counts lanes, seats and lights, and draws no family."""
        c = self.out["c_ok"]
        self.assertIn("lanes running", c)               # the counts stay
        self.assertIn("1 green · 12 active", c)
        for key in ("c_ok", "c_flags_stale", "c_unmeasured", "c_reading"):
            self.assertNotIn("fchip", self.out[key], key)
            self.assertNotIn("bfams", self.out[key], key)
            self.assertNotIn("credit not measured", self.out[key], key)
            self.assertNotIn("credit: still being read", self.out[key], key)

    def test_the_capacity_line_never_draws_a_number_it_did_not_read(self):
        self.assertIn("not read yet", self.out["c_pending"])
        self.assertIn("UNKNOWN", self.out["c_failed"])
        # the server's sentence, escaped like every other string on the page
        self.assertIn("/api/board -&gt; 500", self.out["c_failed"])
        self.assertIn("lanes UNKNOWN", self.out["c_noroster"])
        self.assertIn("roster could not be read", self.out["c_noroster"])
        # no NUMBER of lanes (the link's hover text names the words)
        self.assertNotRegex(self.out["c_noroster"], r"\d+ lanes? running")
        self.assertIn("green UNKNOWN", self.out["c_nolights"])
        self.assertIn("3 lanes running", self.out["c_nolights"])  # control

    def test_a_stale_roster_draws_the_lanes_stale(self):
        self.assertIn("STALE", self.out["c_seats_stale"])
        self.assertNotIn("3 lanes running", self.out["c_seats_stale"])

    # -- the rows ----------------------------------------------------------

    def test_rows_sort_green_yellow_orange_red_then_unset(self):
        self.assertEqual(self.out["sorted"],
                         ["g-new", "g-old", "yel", "org", "red1", "zeta",
                          "quiet"])

    def test_a_line_says_its_team_state_and_never_guesses_one(self):
        """The line's team cell (task/3445): "no team" only when the teams
        leg was read; still being read, it says so."""
        self.assertIn("no team", self.out["team_none"])
        self.assertIn("team reading", self.out["team_reading"])
        self.assertNotIn("no team", self.out["team_reading"])

    def test_a_collapsed_row_carries_light_name_team_and_one_count(self):
        """ONE LINE PER PROJECT (task/3445): light · name · team state ·
        its work, then on a desktop its lanes, last land, activity and repos.
        No family chips (supply) and no week's progress (the About tab).
        ONE COUNT EVERYWHERE (task/3643 slice 3): the count is the project's
        share of the Work page's one read, not the board's task tally; what
        that share says is run in tests/test_web_work_page.py."""
        row = self.out["collapsed"]
        self.assertIn('class="dot green authored"', row)
        self.assertIn(">alpha<", row)
        self.assertIn('class="bteam', row)
        self.assertNotIn("fchip", row)
        self.assertEqual(row.count('class="bcount'), 1)
        self.assertIn("work of alpha unread", row)
        self.assertIn("work of alpha read", self.out["collapsed_read"])
        self.assertNotIn("3 open", row)
        self.assertNotIn("bdetail", row, "a collapsed row drew its detail")
        # everything beyond those four is desktop-only, by class
        wide = row[row.index('class="bwide'):]
        for word in ("3 lanes", "merged", "active", "private"):
            self.assertIn(word, wide, word)
        self.assertNotIn("12 lands", wide)

    def test_the_phone_rule_hides_the_desktop_cells(self):
        css = self.src
        m = re.search(r"@media \(max-width:680px\)\{([^@]*\.brow \.bwide"
                      r"\{display:none\}[^@]*)\}", css)
        self.assertIsNotNone(m, "no phone rule hides the desktop-only cells")

    def test_an_open_project_is_three_tabs_one_at_a_time(self):  # noqa: VACUOUS_ASSERTION — every tab row's strip is asserted EQUAL to the three tab names, and the Work tab's locked mount is asserted present on the same render the absences read
        """TEAM · WORK · ABOUT (task/3445, task/3643 slice 6); the Team tab by
        default, and each tab draws only its own part. The Lanes and Tasks
        tabs were this project's share of the land board and of the backlog;
        they are one tab now, the Work page locked to this project."""
        team, work = self.out["expanded"], self.out["expanded_work"]
        for row in (team, work, self.out["expanded_about"]):
            self.assertEqual(re.findall(r'data-ptab="([a-z]+)"', row),
                             ["team", "work", "about"])
            self.assertEqual(row.count('class="ptab on"'), 1)
        self.assertIn('class="ptab on" aria-selected="true" data-ptab="team"', team)
        for key in ("tab_bogus", "tab_lanes"):
            self.assertIn('class="ptab on" aria-selected="true" data-ptab="team"',
                          self.out[key], key)
        # TEAM: the light setter, then the team
        self.assertIn("lightset", team)
        self.assertIn("dteam", team)
        self.assertNotIn('data-wk="proj"', team)
        # WORK: the Work page, locked to this project and to no other
        self.assertIn('data-wk="proj" data-lock="alpha"', work)
        self.assertIn('data-wk="proj" data-lock="beta"', self.out["expanded_beta"])
        self.assertNotIn("lightset", work)
        for gone in ('class="bkb"', "owed rows", "tasks not read yet"):
            self.assertNotIn(gone, work, gone)
        # ABOUT: its week, its repositories, what is on file
        about = self.out["expanded_about"]
        self.assertIn("4 opened", about)                # its progress
        self.assertIn("emberian/alpha", about)          # both halves of a fork
        self.assertIn("akapug/alpha", about)
        self.assertIn("dwrap", about)

    def test_an_open_projects_line_and_tabs_are_one_sticky_head(self):
        """The helm project's Team tab is 4,443 px tall at 1440 px, and its
        name and tab strip scrolled away with it (task/3475). The open row
        draws its line and its tabs together in `.bstick`, which the
        stylesheet sticks below the nav, and only the tab's body after it."""
        row = self.out["expanded"]
        stick = row.index('<div class="bstick">')
        body = row.index('<div class="bdetail">')
        self.assertLess(stick, row.index('<div class="bline">'))
        self.assertLess(row.index('<div class="bline">'), row.index('class="ptabs"'))
        self.assertLess(row.index('class="ptabs"'), body)
        self.assertLess(body, row.index('class="ptabbody"'))
        self.assertEqual(row.count('class="ptabs"'), 1)
        self.assertNotIn("bstick", self.out["collapsed"])

    def test_the_light_filter_shows_one_colour_or_the_unset(self):
        self.assertEqual(self.out["lit_green"], [["g-new", "g-old"], 0])
        self.assertEqual(self.out["lit_unset"], ["zeta", "quiet"])

    def test_last_activity_is_the_newer_of_the_sync_and_the_seat_beat(self):
        self.assertIn("active today", self.out["active_join"])
        self.assertNotIn("active today", self.out["active_old"])  # the control
        self.assertIn("no activity seen", self.out["active_none"])
        self.assertNotIn("never ago", self.out["active_none"])

    # -- lanes --------------------------------------------------------------

    def test_the_lanes_cell_names_each_lane_and_its_seats(self):
        cell = self.out["lanes_alpha"]
        self.assertIn("lane-a (alpha-claude)", cell)
        self.assertIn("1 on you", cell)                 # the owner hold
        self.assertIn("no lanes", self.out["lanes_none"])

    def test_a_seat_at_work_is_labelled_as_one_not_passed_off_as_a_claim(self):
        cell = self.out["lanes_alpha"]
        self.assertIn("3 lanes (2 claimed, 1 seat at work)", cell)
        self.assertIn("local-1 at work, no lane claimed", cell)
        # a project whose lanes are all claimed carries no split, and one
        # with no claim says only how many seats are at work
        self.assertIn(">1 lane<", self.out["lanes_claimed_only"])
        self.assertIn("2 lanes (2 seats at work)", self.out["lanes_seats_only"])

    def test_a_stale_roster_draws_no_lane_count(self):
        self.assertIn("STALE", self.out["lanes_stale"])
        self.assertNotIn("3 lanes", self.out["lanes_stale"])
        self.assertIn("STALE", self.out["seats_stale"])

    # -- progress -----------------------------------------------------------

    def test_progress_is_commits_then_the_backlogs_net_arrow(self):
        """ONE WORD PER NOUN (console walk 4, P1 3): the week counts the
        first-parent commits on the project's main, which a project that
        merges by hand makes too; "land" is what helm records, and its Work
        tab says that is not measured. So About says "commits on main",
        never "lands"."""
        cell, line = self.out["p_alpha"]["cell"], self.out["p_alpha"]["line"]
        self.assertIn("12 commits", cell)
        self.assertIn("this week: 12 commits on main", line)
        self.assertIn("↓5", cell)                       # 9 closed, 4 opened
        self.assertIn("4 opened", line)
        self.assertIn("9 closed", line)
        self.assertIn("↑3", self.out["p_grow"]["cell"])  # 5 opened, 2 closed
        self.assertIn("1 commit on main", self.out["p_grow"]["line"])
        self.assertIn("→0", self.out["p_even"]["cell"])
        self.assertIn("0 commits", self.out["p_even"]["cell"])
        # a project trunked on master counts master's commits, and says so,
        # as its About's newest commit does ("newest commit on master")
        self.assertIn("this week: 3 commits on master",
                      self.out["p_master"]["line"])
        self.assertNotIn("main", self.out["p_master"]["line"])
        for key in ("p_alpha", "p_grow", "p_even", "p_beta", "p_tasks_stale",
                    "p_trunk_stale", "p_master"):
            for half in ("cell", "line"):
                with self.subTest(progress=key, half=half):
                    self.assertNotIn("land", self.out[key][half].lower())

    def test_progress_never_draws_a_count_it_did_not_read(self):
        self.assertIn("commits ?", self.out["p_beta"]["cell"])
        self.assertNotIn("0 commits", self.out["p_beta"]["cell"])
        stale = self.out["p_tasks_stale"]["cell"]
        self.assertIn("12 commits", stale)               # the trunk still read
        self.assertNotIn("↓", stale)
        self.assertIn("STALE", self.out["p_trunk_stale"]["cell"])
        self.assertNotIn("12 commits", self.out["p_trunk_stale"]["cell"])

    # -- repos --------------------------------------------------------------

    def test_a_fork_pair_shows_both_repos_each_linked_with_its_visibility(self):
        got = self.out["r_fork"]
        self.assertIn('href="https://github.com/akapug/alpha"', got)
        self.assertIn('href="https://github.com/emberian/alpha"', got)
        self.assertIn("private", got)
        self.assertIn("public", got)
        self.assertLess(got.index("akapug/alpha"), got.index("emberian/alpha"))

    def test_an_unknown_visibility_links_nothing(self):
        for key in ("b_unknown", "b_ghfail"):
            self.assertIn("unknown", self.out[key], key)
            self.assertNotIn("href", self.out[key], key)
        self.assertIn("gh: not logged in", self.out["b_ghfail"])
        self.assertIn("href", self.out["b_public"])     # the control

    def test_a_visibility_being_checked_or_not_asked_is_never_a_guess(self):
        self.assertIn("checking", self.out["b_pending"])
        self.assertIn("not checked", self.out["b_unasked"])
        for key in ("b_pending", "b_unasked"):
            for word in ("public", "private", "href"):
                self.assertNotIn(word, self.out[key], key)
        self.assertIn("older than a day", self.out["b_public"])

    def test_the_row_cell_keeps_to_one_badge_per_repository(self):
        """Two remotes on one repository are one badge, and the remotes that
        are not on GitHub are ONE unknown badge naming them; the open row
        lists every remote."""
        cell = self.out["r_many_cell"]
        self.assertEqual(cell.count('href="https://github.com/akapug/p"'), 1)
        self.assertIn("+2 unknown", cell)
        self.assertIn("box-a, box-b", cell)
        opened = self.out["r_many_open"]
        self.assertEqual(opened.count('class="rrepo"'), 4)
        self.assertIn("box-a (not on GitHub)", opened)
        self.assertIn(">unknown</span>", self.out["r_offonly"])
        self.assertNotIn("+1 unknown", self.out["r_offonly"])

    def test_no_remote_and_no_checkout_are_two_different_sentences(self):
        self.assertIn("no remote", self.out["r_none"])
        self.assertIn("repos ?", self.out["r_unread"])
        self.assertIn("no git checkout", self.out["r_unread"])
        self.assertIn("not read yet", self.out["r_nojoin"])

    # -- on you -------------------------------------------------------------

    def test_nothing_on_you_is_one_quiet_line(self):
        got = self.out["y_quiet"]
        self.assertIs(got["quiet"], True)
        self.assertIn("nothing is waiting on you", got["line"])
        self.assertIs(got["queue"], False)
        self.assertEqual(got["asks"], "")

    def test_decisions_open_the_queue_and_name_the_count(self):
        got = self.out["y_decisions"]
        self.assertIs(got["quiet"], False)
        self.assertIs(got["queue"], True)
        self.assertIn("2 decisions", got["line"])

    def test_owner_asks_and_holds_from_the_pipeline_are_listed(self):
        asks, holds = self.out["y_asks"], self.out["y_holds"]
        self.assertIn("1 owner ask", asks["line"])
        self.assertIn("pick a name", asks["asks"])
        self.assertIs(asks["queue"], False)
        self.assertIn("1 land request held for you", holds["line"])
        self.assertIn("lane-a", holds["asks"])

    def test_on_you_is_never_quiet_over_a_read_it_does_not_have(self):
        for key in ("y_unread", "y_half", "y_lr_down", "y_lr_stale",
                    "y_odq_down"):
            self.assertIs(self.out[key]["quiet"], False, key)
        self.assertIn("not read yet", self.out["y_unread"]["line"])
        self.assertIn("UNKNOWN", self.out["y_lr_down"]["line"])
        self.assertIn("owner asks STALE, 3h old", self.out["y_lr_stale"]["line"])
        self.assertIn("decisions UNKNOWN", self.out["y_odq_down"]["line"])
        self.assertIs(self.out["y_odq_down"]["queue"], True)

    def test_homes_strip_says_one_muted_word_where_work_says_why(self):
        """Home's on-you strip is a headline: a read past its bound is the
        word "stale", one not read or failed "not read" — the tiles' words,
        never the age or the reason, which are Work's On-you block's."""
        word = lambda w: '<span class="hstale">%s</span>' % w
        stale, down = self.out["y_lr_stale"]["home"], self.out["y_lr_down"]["home"]
        self.assertEqual(stale, "owner asks " + word("stale"))
        self.assertEqual(down, "owner asks " + word("not read"))
        self.assertEqual(self.out["y_unread"]["home"], word("not read"))
        self.assertEqual(self.out["y_half"]["home"], "owner asks " + word("not read"))
        self.assertEqual(self.out["y_odq_down"]["home"], "decisions " + word("not read"))
        self.assertEqual(self.out["y_asks"]["home"], self.out["y_asks"]["line"])
        for key in ("y_unread", "y_half", "y_lr_down", "y_lr_stale", "y_odq_down"):
            self.assertNotIn("not read yet", self.out[key]["home"], key)

    # -- the last land cell --------------------------------------------------

    def test_the_land_cell_reads_the_trunk_and_says_merged(self):
        """ONE LANDED TIME (walk 2, finding 17): a project with no train
        record merges by hand, so its trunk's newest commit is "merged", and
        "landed" is only ever a train's push."""
        land = self.out["land_ok"][self.out["land_ok"].index("bland"):]
        self.assertIn("merged 1h ago", land)
        self.assertNotIn("landed", land.split("bact")[0])
        self.assertIn("4611958e44a2", land)             # the sha, on hover

    def test_the_train_projects_land_cell_is_the_trains_push_time(self):  # noqa: VACUOUS_ASSERTION — the same land cell is asserted to read 'landed 2h ago' and to name train9 before its 'merged' absence is read
        """...and the project the train lands says "landed" at its newest
        train's push, the time the Work page's Landed column prints."""
        land = self.out["land_train"][self.out["land_train"].index("bland"):]
        self.assertIn("landed 2h ago", land)
        self.assertIn("train9", land)
        self.assertNotIn("merged", land.split("bact")[0])
        other = self.out["land_train_other"]
        self.assertNotIn("landed", other[other.index("bland"):].split("bact")[0])
        # the work read lands after the board's: the cell is redrawn in place
        # when it does, or the train's project read "merged" for a minute
        self.assertIn("boardLand(", _extract_fn(self.src, "boardCountsPaint"))

    def test_a_quiet_day_never_calls_the_train_project_merged_by_hand(self):
        """The day's window holds only DONE trains of the last 24 h. After a
        quiet day the train's project fell through to its trunk's newest
        commit, "merged 3h ago · merged by hand, so helm records no land",
        false for the one project that lands by train. Its last DONE train
        is its land; with none, the cell says no train landed in the day."""
        quiet = self.out["land_train_quiet"]
        quiet = quiet[quiet.index("bland"):].split("bact")[0]
        self.assertIn("landed 3d ago", quiet)
        self.assertIn("train8", quiet)
        self.assertNotIn("merged", quiet)
        none = self.out["land_train_none"]
        none = none[none.index("bland"):].split("bact")[0]
        self.assertIn("no land in 24 h", none)
        self.assertNotIn("merged", none)

    def test_an_unreadable_trunk_reads_not_read_here(self):  # noqa: VACUOUS_ASSERTION — land_ok is asserted to carry 'merged' unconditionally after the loop, the positive control on the same cell
        for key in ("land_none", "land_nojoin"):
            land = self.out[key][self.out[key].index("bland"):]
            self.assertIn("not read here", land, key)
            self.assertNotIn("merged", land.split("bact")[0], key)
        self.assertIn("merged", self.out["land_ok"])     # the control

    def test_a_stale_trunk_reads_stale_not_a_time(self):
        land = self.out["land_stale"][self.out["land_stale"].index("bland"):]
        self.assertIn("STALE", land.split("bact")[0])
        self.assertNotIn("merged", land.split("bact")[0])

    # -- the quiet fold --------------------------------------------------------

    def test_a_row_the_server_calls_quiet_folds(self):
        self.assertEqual(sorted(self.out["q_fold"]),
                         ["litold", "never", "quietone"])

    def test_a_row_the_server_keeps_up_stays_up(self):
        for name in ("beta", "lit", "beaty"):
            self.assertIn(name, self.out["q_lead"], name)
            self.assertNotIn(name, self.out["q_fold"], name)

    def test_the_filter_finds_a_folded_project(self):
        lead, folded, shown = self.out["q_found"]
        self.assertEqual(lead, ["quietone"])
        self.assertEqual((folded, shown), (0, 1))

    def test_folding_changes_no_count(self):
        self.assertEqual(self.out["q_shown"], len(QROWS))
        self.assertEqual(len(self.out["q_lead"]) + len(self.out["q_fold"]),
                         len(QROWS))
        self.assertIn('"quiet (" + ', _extract_fn(self.src, "renderBoard"))

    def test_an_unread_backlog_never_folds_a_row_as_if_it_were_zero(self):
        self.assertIn("quietone", self.out["q_fold"])    # the control
        self.assertEqual(self.out["q_unread"], [])

    # -- the flag card (moved from the quota tab) -----------------------------

    def test_the_flag_card_renders_overall_and_every_family(self):
        head, rows = self.out["flags_ok"]["head"], self.out["flags_ok"]["rows"]
        self.assertIn("ORANGE", head)
        self.assertIn("tightest anthropic", head)
        self.assertIn("anthropic", rows)
        self.assertIn("weekly window half spent", rows)
        self.assertIn("DECLARED by the owner", rows)

    def test_the_flag_card_says_not_measured_and_why(self):
        got = self.out["flags_unmeasured"]
        self.assertIn("not measured", got["head"])
        self.assertIn("no fresh burn-flag snapshot", got["head"])
        self.assertEqual(got["rows"], "")

    def test_a_stale_flag_card_draws_no_colours(self):
        got = self.out["flags_stale"]
        self.assertIn("STALE", got["head"])
        self.assertNotIn("ORANGE", got["head"])
        self.assertNotIn("#c9772e", got["rows"])

    # -- the setter ----------------------------------------------------------

    def test_the_setter_posts_the_colour_and_reason_to_the_projects_door(self):
        self.assertEqual(self.out["posted"], [
            {"url": "/api/projects/state",
             "body": {"name": "alpha", "colour": "green",
                      "reason": "because it is the one that ships"}}])
        self.assertEqual(self.out["reloads"], 1)
        self.assertEqual(self.out["setter_key"], "alpha")

    def test_the_setter_refuses_a_colour_without_a_reason(self):
        self.assertIn("Say why first", self.out["bare_msg"])
        self.assertTrue(self.out["bare_focused"])
        self.assertEqual(len(self.out["posted"]), 1,
                         "a colour with no reason reached the door")


def _team_board():
    """A board carrying the teams leg: the spec's worked example on codex
    (46.4M/h lasts; alpha 30%, beta 35%), a proposed team on gamma, and the
    flag card's codex reading with its reset credits."""
    say = {"GREEN": "open more lanes", "YELLOW": "normal work, no extra lanes",
           "ORANGE": "critical path only", "RED": "start nothing new on this family",
           "GREY": "not measured"}
    roles = {"lead": ["review", "build", "verify", "delegate", "research",
                      "council"],
             "builder": ["build", "delegate", "review"],
             "reviewer": ["review", "verify", "council"],
             "checker": ["verify", "research"]}

    def member(seat, fam, role, state="live"):
        return {"seat": seat, "family": fam, "role": role, "state": state,
                "shared": []}
    alpha_alloc = {"codex": {"mode": "rate", "share": 30, "colour": "RED",
                             "family_colour": "ORANGE", "rationed": True}}
    return {
        "headline": {"lanes": {}, "green": 1, "colour": "ORANGE"},
        "sections": {
            "flags": _sec(families={
                "codex": {"colour": "ORANGE", "axis": "money",
                          "cause": "runway 54h is under the 85h horizon",
                          "provenance": "measured", "expires_at": None,
                          "credits": {"usable": 1, "unread": 3,
                                      "why": "3 accounts not asked for a "
                                             "balance this pass"}},
                "anthropic": {"colour": "YELLOW", "axis": "money",
                              "cause": "4 of 6 accounts past half",
                              "provenance": "measured", "expires_at": None},
                "kimi": {"colour": "GREY", "axis": None,
                         "cause": "no money reader exists for this family",
                         "provenance": "unmeasured", "expires_at": None},
                "qwen27": {"colour": "GREY", "axis": None,
                           "cause": "local on the operator's own GPUs, free",
                           "provenance": "unmeasured", "expires_at": None}},
                overall={"colour": "ORANGE", "family": "codex"}),
            "teams": _sec(seats=[{"seat": "qwen27", "family": "qwen27",
                                  "presence": "live", "project": "alpha"}],
                          pace={"codex": {"sustainable_per_h": 54 * 73e6 / 85,
                                          "tokens_per_hour": 73e6}},
                          burn={"codex": {"per_hour": 73e6, "seats": {
                              "alpha-codex": 32.85e6, "beta-codex": 22.63e6,
                              "gamma-codex": 10.95e6}}},
                          # THE LANES: qwen27 measured at 4, and four rows on
                          # it — two on delta, one on alpha (no share), one
                          # on no project
                          slots={"families": ["qwen27", "qwenlocal"],
                                 "capacity": {"qwen27": {
                                     "lanes": 4, "by": "seat-m",
                                     "ts": 1900000000,
                                     "reason": "4 concurrent at 32k"}},
                                 "capacity_problem": None, "why": None,
                                 "in_use": {
                                     "qwen27": {"by_project": {"delta": 2,
                                                               "alpha": 1},
                                                "unplaced": 1, "total": 4},
                                     "qwenlocal": {"by_project": {},
                                                   "unplaced": 0,
                                                   "total": 0}}},
                          tier={"kinds": ["review", "verify"],
                                "families": ["anthropic", "codex", "ds4pro",
                                             "kimi", "grok"]},
                          say=say, roles=roles,
                          # THE AXES WHOSE ORANGE A SHARE RATIONS, read from
                          # `teams.RATION_AXES` by the server, never copied
                          ration_axes=["money", "declared"])},
        "projects": {
            "alpha": {"team": {
                "v": 2, "authored": True, "binding": True, "by": "owner",
                "ts": 1900000000, "reason": "alpha leads codex",
                "members": [member("alpha-claude", "anthropic", "lead"),
                            member("alpha-codex", "codex", "builder")],
                "shares": {"codex": 30}, "allocation": alpha_alloc,
                "drift": [], "history": [
                    {"kind": "team", "v": 2, "by": "owner", "ts": 1900000000,
                     "reason": "alpha leads codex",
                     "diff": ["codex share 25% → 30%"]}]}},
            "beta": {"team": {
                "v": 1, "authored": True, "binding": True, "by": "owner",
                "ts": 1900000000, "reason": "beta",
                "members": [member("beta-codex", "codex", "builder")],
                "shares": {"codex": 35},
                "allocation": {"codex": {"mode": "rate", "share": 35}},
                "drift": [], "history": []}},
            "delta": {"team": {
                "v": 1, "authored": True, "binding": True, "by": "owner",
                "ts": 1900000000, "reason": "delta reads on the local lane",
                "members": [member("delta-claude", "anthropic", "lead"),
                            member("qwen27", "qwen27", "reviewer")],
                "shares": {"qwen27": 30}, "allocation": {},
                "drift": [], "history": []}},
            "gamma": {"team": {
                "v": 0, "authored": False, "binding": False, "by": "",
                "ts": None, "reason": "",
                "members": [member("gamma-claude", "anthropic", "lead"),
                            member("gamma-codex", "codex", "builder")],
                "shares": {"codex": 15}, "allocation": {}, "drift": [
                    {"kind": "unlisted", "seat": "stray-seat",
                     "text": "stray-seat works here and is not on the team"}],
                "history": []}}}}


class TeamCardRuntimeTest(unittest.TestCase):
    """task/3156: the team card and the family sheet, lifted out of the
    SHIPPED page and run under node over a board carrying the teams leg."""

    DRIVER = r"""
const out = {};
const A = BOARD.projects.alpha;
out.alloc = teamAlloc("alpha", teamCurrent("alpha", A), BOARD, false);
out.line = teamLine("alpha", "codex", out.alloc.codex);
// D1: an ORANGE that the reach axis set rations nobody
BOARD.sections.flags.families.codex.axis = "reach";
out.reach = teamAlloc("alpha", teamCurrent("alpha", A), BOARD, false).codex;
BOARD.sections.flags.families.codex.axis = "money";
// D2: a family with no share is unrationed on the card, never 0%
const BARE = {members: A.team.members, shares: {}};
out.unset = teamAlloc("alpha", BARE, BOARD, false).codex;
out.unset_line = teamLine("alpha", "codex", out.unset);
out.unset_diff = teamDiff(BARE, {members: A.team.members, shares: {codex: 30}});
// D3: a member is billed to the family it SPENDS, which the server sends
out.spends = Object.keys(teamAlloc("alpha", {members: [{seat: "alpha-native",
  family: "codex", spends: "anthropic", role: "reviewer"}], shares: {}}, BOARD, false));
// D6: a burn reader that RAISED reads FAILED on the card, never unmeasured
const TSEC = BOARD.sections.teams, TBURN = TSEC.burn;
TSEC.burn = null;
TSEC.failed = {burn: "ValueError"};
out.failed = teamAlloc("alpha", teamCurrent("alpha", A), BOARD, false).codex;
out.failed_line = teamLine("alpha", "codex", out.failed);
out.failed_section = teamSection(ALPHA, A, BOARD);
TSEC.failed = {};
out.unread_line = teamLine("alpha", "codex", teamAlloc("alpha", teamCurrent("alpha", A), BOARD, false).codex);
TSEC.burn = TBURN;
delete TSEC.failed;
out.section = teamSection(ALPHA, A, BOARD);
out.mini = teamMini(ALPHA, A, BOARD);
out.flags = flagsHTML(BOARD.sections.flags, BOARD);
out.bar = shareBar("codex", BOARD);
out.gamma = teamSection(GAMMA, BOARD.projects.gamma, BOARD);
out.gamma_mini = teamMini(GAMMA, BOARD.projects.gamma, BOARD);
out.red_light = teamAlloc("alpha", teamCurrent("alpha", A), BOARD, true).codex;
// THE OWNER PRESSES + EIGHT TIMES: a draft, the budget moves at once
const dr = teamDraft("alpha", A);
dr.shares.codex = 70;
out.after = teamAlloc("alpha", dr, BOARD, false).codex;
out.section_after = teamSection(ALPHA, A, BOARD);
out.bar_after = shareBar("codex", BOARD);
out.diff = teamDiff(A.team, dr);
dr.members.push({seat: "qwen27", family: "qwen27", role: "reviewer", state: "live", added: true});
dr.members[1].gone = true;
out.diff2 = teamDiff(A.team, dr);
out.route_build = teamRoute("alpha", dr, teamAlloc("alpha", dr, BOARD, false), "build", false,
  BOARD.sections.flags.families, BOARD.sections.teams.roles, BOARD.sections.teams.say);
out.route_red = teamRoute("alpha", dr, {}, "build", true, {}, BOARD.sections.teams.roles, {});
out.drift = teamDriftLines("alpha", A.team, dr);
// THE LANES: delta holds 2 of its 1.2 slots on qwen27 (30% of 4 lanes)
const D = BOARD.projects.delta;
out.lane = teamAlloc("delta", teamCurrent("delta", D), BOARD, false).qwen27;
out.lane_line = teamLine("delta", "qwen27", out.lane);
out.lane_section = teamSection(DELTA, D, BOARD);
out.lane_mini = teamMini(DELTA, D, BOARD);
out.lane_bar = slotBar("qwen27", BOARD);
out.lane_route = teamRoute("delta", teamCurrent("delta", D), {qwen27: out.lane}, "council", false,
  BOARD.sections.flags.families, BOARD.sections.teams.roles, BOARD.sections.teams.say, BOARD.sections.teams.tier);
out.lane_review = teamRoute("delta", teamCurrent("delta", D), {qwen27: out.lane}, "review", false,
  BOARD.sections.flags.families, BOARD.sections.teams.roles, BOARD.sections.teams.say, BOARD.sections.teams.tier);
FAM_SEL = "qwen27";
out.lane_sheet = flagsHTML(BOARD.sections.flags, BOARD).rows;
const dd = teamDraft("delta", D);
dd.shares.qwen27 = 80;
out.lane_after = teamAlloc("delta", dd, BOARD, false).qwen27;
TEAM_DRAFT.delete("delta");
// NOT MEASURED: the same world with no capacity recorded
BOARD.sections.teams.slots.capacity = {};
out.unmeasured = teamAlloc("delta", teamCurrent("delta", D), BOARD, false).qwen27;
out.unmeasured_line = teamLine("delta", "qwen27", out.unmeasured);
out.unmeasured_section = teamSection(DELTA, D, BOARD);
out.unmeasured_bar = slotBar("qwen27", BOARD);
FAM_SEL = null;
// the sheet names the accounts that bill its family (task/3445 L4): a link
// only where an account group on credit can answer it
BOARD.sections.flags.families.codex.bills = ["codex"];
FAM_SEL = "codex";
out.billed_unread = flagsHTML(BOARD.sections.flags, BOARD).rows;   // no table yet
QUOTA_GROUPS = new Set(["codex"]);
out.billed = flagsHTML(BOARD.sections.flags, BOARD).rows;
QUOTA_GROUPS = new Set(["anthropic"]);
out.billed_nogroup = flagsHTML(BOARD.sections.flags, BOARD).rows;
BOARD.sections.flags.families.qwen27.bills = [];                  // local: no bill
FAM_SEL = "qwen27";
out.billed_lane = flagsHTML(BOARD.sections.flags, BOARD).rows;
QUOTA_GROUPS = null;
FAM_SEL = null;
out.eff = [teamEff("ORANGE", 1.0), teamEff("ORANGE", 2.0), teamEff("ORANGE", 2.0001),
           teamEff("RED", 0.1), teamEff("YELLOW", 9), teamEff("ORANGE", null)];
out.tokens = [teamTokens(10.95e6), teamTokens(13.914e6), teamTokens(640e3)];
console.log(JSON.stringify(out));
"""

    @classmethod
    def setUpClass(cls):
        from tests.test_web_accounts import _extract_const
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        src = web_ui_loader.read_text()

        def decl(name):
            m = re.search(_DECL % re.escape(name), src, re.M)
            assert m, "no declaration of %s in the assembled page" % name
            return m.group(0)
        fns = "\n\n".join(_extract_fn(src, n) for n in (
            "age", "ago", "pkey", "lrDur", "lrAgo", "flagWhen", "flagsHTML",
            "boardSec", "boardSecState", "boardSecWord") + TEAM_FNS)
        support = ('const esc = s => String(s ?? "").replace(/[&<>"\']/g, c => '
                   '({"&":"&amp;","<":"&lt;",">":"&gt;",\'"\':"&quot;","\'":"&#39;"}[c]));\n')
        worlds = {"BOARD": _team_board(), "ALPHA": _row("alpha", "green"),
                  "GAMMA": _row("gamma"), "DELTA": _row("delta", "green")}
        cls.BOARD = worlds["BOARD"]
        payload = "".join("const %s = %s;\n" % (k, json.dumps(v))
                          for k, v in worlds.items())
        cls.tmp = tempfile.mkdtemp(prefix="helm-web-teams-runtime-")
        path = os.path.join(cls.tmp, "run.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(support + _extract_const(src, "FLAGCOL") + "\n"
                    + "\n".join(decl(n) for n in TEAM_DECLS) + "\n"
                    + payload + fns + cls.DRIVER)
        chk = subprocess.run([cls.node, "--check", path],
                             capture_output=True, text=True)
        assert chk.returncode == 0, "node --check failed:\n" + chk.stderr
        cls.proc = subprocess.run([cls.node, path], capture_output=True,
                                  text=True, timeout=60)
        try:
            cls.out = json.loads(cls.proc.stdout or "{}")
        except json.JSONDecodeError:
            cls.out = {}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def setUp(self):
        self.assertTrue(self.out, "node produced no output: "
                        + (self.proc.stderr or "")[:1200])

    def _run_driver(self, expr, extra=""):
        """Run a one-line driver under node, returning the JSON string output."""
        from tests.test_web_accounts import _extract_const
        src = web_ui_loader.read_text()
        support = ('const esc = s => String(s ?? "").replace(/[&<>"\']/g, c => '
                   '({"&":"&amp;","<":"&lt;",">":"&gt;",\'"\':"&quot;","\'":"&#39;"}[c]));\n')
        fns = "\n\n".join(_extract_fn(src, n) for n in (
            "age", "ago", "pkey", "lrDur", "lrAgo", "flagWhen", "flagsHTML",
            "boardSec", "boardSecState", "boardSecWord") + TEAM_FNS)
        decls = "\n".join(re.search(_DECL % re.escape(n), src, re.M).group(0)
                          for n in TEAM_DECLS)
        header = support + _extract_const(src, "FLAGCOL") + "\n" + decls
        path = os.path.join(self.tmp, "run2.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(header + fns + extra + expr + "\nconsole.log(JSON.stringify(out));\n")
        p = subprocess.run([self.node, path], capture_output=True, text=True, timeout=30)
        assert p.returncode == 0, "node failed: " + p.stderr[:600]
        self.out2 = json.loads(p.stdout or "{}")
        return self.out2

    def test_shareBar_with_raised_teams_section(self):
        """When the teams leg raised, shareBar says UNKNOWN, not 'No project'."""
        teams = dict(self.BOARD["sections"]["teams"],
                     loading=None, retry=None,
                     unavailable="the team read raised (ValueError)",
                     measured_at=None, age_s=None)
        board = json.loads(json.dumps(self.BOARD))
        board["sections"]["teams"] = teams
        # clear project shares so shareBar hits the empty-keys path
        for p in board["projects"].values():
            p.setdefault("team", {})["shares"] = {}
        out = self._run_driver("out = shareBar('codex', board);",
                               "const board = " + json.dumps(board) + ";\n")
        self.assertIn("UNKNOWN", out)
        self.assertNotIn("No project has a", out)
        self.assertIn("ValueError", out)

    def test_shareBar_with_loading_teams_section(self):
        """When the teams leg is still being read, shareBar says not read yet."""
        teams = dict(self.BOARD["sections"]["teams"],
                     loading=True, retry=True,
                     measured_at=None, age_s=None,
                     unavailable=None)
        board = json.loads(json.dumps(self.BOARD))
        board["sections"]["teams"] = teams
        for p in board["projects"].values():
            p.setdefault("team", {})["shares"] = {}
        out = self._run_driver("out = shareBar('codex', board);",
                               "const board = " + json.dumps(board) + ";\n")
        self.assertIn("not read yet", out)
        self.assertNotIn("No project has a", out)
        self.assertNotIn("UNKNOWN", out)

    def test_shareBar_with_zero_shares_still_says_no_project(self):
        """A read teams section with zero codex shares: keep 'No project'."""
        teams = dict(self.BOARD["sections"]["teams"], unavailable=None)
        board = json.loads(json.dumps(self.BOARD))
        board["projects"] = {"alpha": {"team": {"binding": True,
                                                 "members": [], "shares": {}}}}
        board["sections"]["teams"] = teams
        out = self._run_driver("out = shareBar('codex', board);",
                               "const board = " + json.dumps(board) + ";\n")
        self.assertIn("No project has a codex share yet", out)

    def test_shareBar_no_three_space_runs_in_output(self):
        """shareBar output never has three consecutive spaces."""
        bar = self.out["bar"]
        self.assertNotIn("   ", bar)
        bar_after = self.out["bar_after"]
        self.assertNotIn("   ", bar_after)

    def test_the_card_folds_the_worked_example_as_the_server_does(self):
        a = self.out["alloc"]["codex"]
        self.assertEqual((a["mode"], a["share"], a["total"]), ("rate", 30, 65))
        self.assertEqual(a["colour"], "RED")
        self.assertTrue(a["rationed"])
        self.assertEqual(self.out["line"], "share codex 30% → 13.9M/h budget, "
                         "burning 32.9M/h (2.4×) → RED for alpha")
        self.assertEqual(self.out["tokens"], ["11.0M", "13.9M", "640k"])
        self.assertEqual(self.out["eff"], ["YELLOW", "ORANGE", "RED", "RED",
                                           "YELLOW", "ORANGE"])

    def test_an_orange_that_reach_set_rations_nobody_on_the_card(self):  # noqa: VACUOUS_ASSERTION — two unconditional tuple equalities, the reach fold and its money control
        """D1: the card folds a draft as `teams.allocation` folds a saved
        team, so an ORANGE the reach axis set is the family's own colour
        here too, never a budget colour."""
        got = self.out["reach"]
        self.assertEqual((got["colour"], got["rationed"]), ("ORANGE", False))
        # CONTROL: the money axis behind the same ORANGE rations it
        a = self.out["alloc"]["codex"]
        self.assertEqual((a["colour"], a["rationed"]), ("RED", True))

    def test_a_family_with_no_share_is_unrationed_on_the_card(self):  # noqa: VACUOUS_ASSERTION — the unset share's absence sits beside the saved 30% share asserted by equality on the same fold
        """D2: the card folds an unset share as the server does, so the
        family's own ORANGE stands and nothing reads 0%."""
        got = self.out["unset"]
        self.assertIsNone(got["share"])
        self.assertEqual((got["colour"], got["rationed"]), ("ORANGE", False))
        self.assertIn("no share", self.out["unset_line"])
        self.assertNotIn("0%", self.out["unset_line"])
        self.assertEqual(self.out["unset_diff"], ["codex share unset → 30%"])
        # CONTROL: the saved 30% share on the same family rations it
        self.assertEqual(self.out["alloc"]["codex"]["share"], 30)

    def test_a_member_is_billed_to_the_family_it_spends(self):
        """D3: the server sends each member's `spends` (the family door's
        answer) beside the authored family, and the card folds a draft on
        it, as `teams.allocation` does."""
        self.assertEqual(self.out["spends"], ["anthropic"])
        # CONTROL: a member the door names nothing for folds on its family
        self.assertIn("codex", self.out["alloc"])

    def test_a_reader_that_raised_reads_failed_on_the_card(self):
        """D6: the server names each reader that RAISED in the section's
        `failed`, and the card says FAILED where it would say unmeasured."""
        self.assertIn("burning FAILED (ValueError)", self.out["failed_line"])
        self.assertNotIn("unmeasured", self.out["failed_line"])
        self.assertEqual(self.out["failed"].get("burnFailed"), "ValueError")
        self.assertIn("readings FAILED", self.out["failed_section"])
        # CONTROL: no burn reading at all is unmeasured, never FAILED
        self.assertIn("burning unmeasured", self.out["unread_line"])

    def test_the_team_section_draws_every_part_of_the_mockup(self):
        sec = self.out["section"]
        for needle in ("team v2", "budgets", "data-team-step", "30%",
                       "13.9M/h", "alpha-codex", "data-team-role",
                       "data-team-rm", "data-team-addseat", "qwen27",
                       "new seat alpha-", "what the lead will do",
                       "what agents hear", "history",
                       "codex share 25% → 30%", "save team v2 → v3",
                       "No changes.", "reset credits: 1 usable"):
            self.assertIn(needle, sec, needle)
        self.assertIn(" disabled>save team", sec)       # nothing to save
        self.assertIn("codex RED 2.4×", self.out["mini"])

    def test_a_press_moves_the_budget_the_bar_and_the_save_bar_at_once(self):
        after = self.out["after"]
        self.assertEqual((after["share"], after["total"]), (70, 105))
        self.assertTrue(after["over"])
        self.assertEqual(after["colour"], "ORANGE")      # 32.9 vs 30.9M/h
        sec = self.out["section_after"]
        self.assertIn("Not saved: codex share 30% → 70%", sec)
        self.assertNotIn(" disabled>save team", sec)
        self.assertIn("alpha 70%", self.out["bar_after"])
        self.assertIn("(unsaved)", self.out["bar_after"])
        self.assertIn("over-promised: shares add to 105%",
                      self.out["bar_after"])
        self.assertEqual(self.out["diff"], ["codex share 30% → 70%"])
        self.assertEqual(self.out["diff2"], ["− alpha-codex",
                                             "+ qwen27 (reviewer, qwen27)",
                                             "codex share 30% → 70%"])

    def test_the_route_preview_and_the_drift_follow_the_draft(self):
        self.assertIn("not @qwen27: role reviewer does not take build",
                      self.out["route_build"])
        self.assertIn("E9", self.out["route_build"])
        self.assertIn("is RED — start nothing new here", self.out["route_red"])
        self.assertIn("alpha-codex leaves: it keeps its lanes until it "
                      "releases them, and gets no new work here.",
                      self.out["drift"])

    def test_a_red_light_releases_the_share(self):
        red = self.out["red_light"]
        self.assertEqual((red["share"], red["budget"]), (0, 0))
        self.assertEqual(red["colour"], "RED")

    def test_the_family_sheet_links_each_share_to_its_projects_team_tab(self):
        """Fleet › credit holds the shares across projects; each project's
        share opens that project's Team tab (task/3445 L4)."""
        self.assertIn('href="#work/projects?open=alpha&amp;tab=team"', self.out["bar"])
        self.assertIn("alpha 30%", self.out["bar"])      # the legend still reads
        self.assertIn('href="#work/projects?open=delta&amp;tab=team"', self.out["lane_bar"])

    def test_a_budget_row_links_its_family_on_fleet_credit(self):
        self.assertIn('href="#quota?family=codex"', self.out["section"])

    def test_the_family_sheet_links_its_accounts_only_where_a_group_answers(self):
        """A link exists only where an account group on credit can answer it:
        before the accounts table is read the sheet says so; a vendor no group
        carries is named, not linked; a family served from the operator's own
        GPUs has no bill at all (task/3445 L4 cure)."""
        self.assertIn('data-qfam="codex"', self.out["billed"])
        for key in ("billed_unread", "billed_nogroup", "billed_lane"):
            self.assertNotIn("data-qfam", self.out[key], key)
        self.assertIn("accounts not read yet", self.out["billed_unread"])
        self.assertIn("no account on this page", self.out["billed_nogroup"])
        self.assertIn("local GPU, no bill", self.out["billed_lane"])
        self.assertIn("own GPUs", self.out["billed_lane"])  # the lane sheet drew

    def test_the_family_sheet_carries_shares_credits_and_the_declare(self):
        rows = self.out["flags"]["rows"]
        for needle in ('data-fam-sel="codex"', "fchip fpick on",
                       "Lasts to the reset only at <b>46.4M/h</b>",
                       "the fleet burns <b>73.0M/h</b>", "alpha 30%",
                       "beta 35%", "unallocated 35% (held back)",
                       "Reset credits:", "3 accounts not asked",
                       "declare a colour for codex", "data-declare="):
            self.assertIn(needle, rows, needle)
        self.assertIn("runway 54h is under the 85h horizon", rows)
        self.assertEqual(self.out["bar"].count("<i style"), 2)

    def test_a_local_lane_folds_into_slots_as_the_server_does(self):  # noqa: VACUOUS_ASSERTION — the saved fold asserts queued True and the draft asserts it False, each an unconditional equality on the same field
        """The owner's slots direction on the card: 30% of 4 lanes is 1.2
        slots, delta holds 2 — ORANGE, and the next row QUEUES."""
        a = self.out["lane"]
        self.assertEqual((a["mode"], a["lane"], a["capacity"], a["inUse"],
                          a["fleet"]), ("slots", True, 4, 2, 4))
        self.assertAlmostEqual(a["slots"], 1.2)
        self.assertEqual((a["colour"], a["queued"]), ("ORANGE", True))
        # the same sentence `teams.line` prints (tests/test_teams.py)
        self.assertEqual(self.out["lane_line"],
                         "slots qwen27 30% of 4 lanes → 1.2 slots, 2 in use "
                         "(fleet 4 of 4) → ORANGE for delta; QUEUED — new "
                         "work waits for a lane, it is not refused")
        # a press to 80% is 3.2 slots: inside them, and the next row fits
        after = self.out["lane_after"]
        self.assertAlmostEqual(after["slots"], 3.2)
        self.assertEqual((after["colour"], after["queued"]),
                         ("YELLOW", False))

    def test_the_card_steps_a_slot_share_and_says_queued(self):
        sec = self.out["lane_section"]
        for needle in ('data-fam="qwen27"', "of <b>4 lanes</b>",
                       "<b>1.2 slots</b>", "<b>2</b> in use here",
                       "fleet 4 of 4", "▲ slots 1.2",
                       "QUEUED: one more row here waits for a lane",
                       "data-team-kind='council'"):
            self.assertIn(needle, sec, needle)
        self.assertIn("qwen27 ORANGE 2 of 1.2 slots, queued",
                      self.out["lane_mini"])

    def test_the_route_preview_queues_the_lane_and_keeps_the_tier(self):
        self.assertIn("TAKE @qwen27", self.out["lane_route"])
        self.assertIn("QUEUED</b> — slots qwen27 30% of 4 lanes",
                      self.out["lane_route"])
        # a review CLOSES a row: the owner's approval tier decides (E3)
        self.assertIn("not @qwen27: qwen27 is not in the owner&#39;s "
                      "approval tier", self.out["lane_review"])
        self.assertIn("(E3)", self.out["lane_review"])

    def test_the_family_sheet_draws_the_lanes_bar(self):
        sheet = self.out["lane_sheet"]
        for needle in ("fchip fpick on", "Capacity 4 lanes, measured by "
                       "seat-m", "4 concurrent at 32k", "tstack tslots",
                       "<b>4 lanes</b> in all · <b>4</b> in use · 0 free"):
            self.assertIn(needle, sheet, needle)
        bar = self.out["lane_bar"]
        for needle in ("delta 30% = 1.2 slots · 2 in use", ">over<",
                       "alpha: 1 in use, no share", "on no project: 1 in use",
                       "unallocated 70% = 2.8 slots", 'class="over"'):
            self.assertIn(needle, bar, needle)
        self.assertEqual(bar.count("<i style"), 1)      # delta alone

    def test_with_no_capacity_recorded_it_says_so_and_no_number(self):
        u = self.out["unmeasured"]
        self.assertEqual((u["capacity"], u["slots"], u["queued"]),
                         (None, None, False))
        self.assertEqual(u["colour"], "GREY")
        self.assertEqual(self.out["unmeasured_line"],
                         "slots qwen27 30%: capacity not measured, 2 in use "
                         "here → GREY for delta")
        self.assertIn("<b>capacity not measured</b>",
                      self.out["unmeasured_section"])
        self.assertNotIn("slots</b>", self.out["unmeasured_section"])
        bar = self.out["unmeasured_bar"]
        self.assertIn("<b>capacity not measured</b> · <b>4</b> in use", bar)
        self.assertIn("delta 30% · 2 in use", bar)
        self.assertNotIn("= ", bar)

    def test_a_proposed_team_offers_accept_and_binds_nothing(self):
        self.assertIn("proposed team", self.out["gamma"])
        self.assertIn("data-team-accept", self.out["gamma"])
        self.assertIn("stray-seat works here", self.out["gamma"])
        self.assertIn("team proposed", self.out["gamma_mini"])
        self.assertNotIn("gamma", self.out["bar"])        # not binding

    def test_work_and_the_families_card_tell_the_owner_no_helm_verb(self):  # noqa: VACUOUS_ASSERTION — each render's absence of a verb follows an unconditional positive control on the same markup, asserting the branch named was drawn
        """RULE 2 ON WORK › PROJECTS AND THE FAMILIES CARD (console walk 3,
        open points). With no projects the list said "run `helm sync`, then
        refresh"; the families card, past its bound, said "`helm burn` reads
        them fresh". The card's stale branch runs; the list is markup."""
        board = json.loads(json.dumps(self.BOARD))
        board["sections"]["flags"].update(stale=True, age_s=900, limit_s=600)
        out = self._run_driver(
            "const out = {stale: flagsHTML(board.sections.flags, board).head};",
            "const board = " + json.dumps(board) + ";\n")
        work = view_markup(web_ui_loader.read_text(), "work")
        # POSITIVE CONTROLS: the stale branch, and the list's empty state
        self.assertIn("STALE", out["stale"])
        self.assertIn('id="brows"', work)
        for name, markup in (("stale", out["stale"]), ("work", work)):
            self.assertEqual(owner_verbs(markup), [], "%s: %s" % (name, markup))

    def test_the_team_tab_tells_the_owner_no_helm_verb(self):
        """RULE 2 ON WORK › PROJECTS › <PROJECT> › TEAM (console walk 3, #7).
        The tab said "the lead starts or resumes it (`helm seat spawn
        <project>-codex` for a new codex seat on this project)" and headed its
        route box `helm route review --project <project>`; its other states named
        `helm team set` and `helm team capacity`. The owner does not use a
        terminal.

        The lead's lines come from the REAL `teams.drift` over a world that
        raises every kind of drift it can name, so the tab is read over the
        text the server actually sends."""
        from helm import teams
        team = {"members": [
            {"seat": "alpha-claude", "family": "anthropic", "role": "lead"},
            {"seat": "alpha-kimi", "family": "kimi", "role": "reviewer"},
            {"seat": "beta-codex", "family": "codex", "role": "builder"},
            {"seat": "alpha-grok", "family": "grok", "role": "reviewer"}],
            "shares": {}}

        def placed(fam, project, home=None, source="home room"):
            return {"family": fam, "presence": "live", "project": project,
                    "home_room": home, "source": source}
        seats = {"alpha-claude": placed("anthropic", "alpha", "alpha"),
                 "beta-codex": placed("codex", "beta", "beta"),
                 "alpha-grok": placed("kimi", "alpha", "alpha"),
                 "stray-codex": placed("codex", "alpha", source="its cwd"),
                 "odd-seat": placed("unmeasured-fam", "alpha", source="its cwd")}
        drift = teams.drift("alpha", team, {"seats": seats,
                                            "roster": dict.fromkeys(seats)},
                            flags={"codex": {"colour": burnflags.RED}})
        # POSITIVE CONTROL: the world raised every kind the lead is told about
        self.assertEqual({d["kind"] for d in drift},
                         {"wanted", "family-differs", "homed-elsewhere",
                          "family-red", "no-share", "unlisted"})
        board = json.loads(json.dumps(self.BOARD))
        board["projects"]["alpha"]["team"].update(
            members=[dict(m, state="wanted" if m["seat"] == "alpha-kimi"
                          else "live", shared=[]) for m in team["members"]],
            shares={}, drift=drift)
        board["sections"]["teams"]["slots"]["capacity"] = {}  # lanes unmeasured
        extra = "".join("const %s = %s;\n" % (k, json.dumps(v)) for k, v in (
            ("board", board), ("ALPHA", _row("alpha", "green")),
            ("DELTA", _row("delta", "green"))))
        out = self._run_driver("""const out = {};
out.saved = teamSection(ALPHA, board.projects.alpha, board);
const dr = teamDraft("alpha", board.projects.alpha);
dr.members.push({seat: "alpha-qwen", family: "qwen27", role: "reviewer",
                 state: "wanted", added: true, shared: []});
out.draft = teamSection(ALPHA, board.projects.alpha, board);
TEAM_DRAFT.delete("alpha");
out.lanes = teamSection(DELTA, board.projects.delta, board);
out.none = teamSection(ALPHA, {team: null}, board);
FAM_SEL = "qwen27";
out.sheet = flagsHTML(board.sections.flags, board).rows;""", extra)
        # POSITIVE CONTROLS on the same renders: each is the state named
        self.assertIn("alpha-kimi is not running", out["saved"])
        self.assertIn("what agents hear", out["saved"])
        self.assertIn("Start alpha-qwen", out["draft"])
        self.assertIn("capacity not measured", out["lanes"])
        self.assertIn("no team", out["none"])
        self.assertIn("Capacity not measured", out["sheet"])
        for name in ("saved", "draft", "lanes", "none", "sheet"):
            self.assertEqual(owner_verbs(out[name]), [],
                             "%s: %s" % (name, out[name]))


class BoardReadAgainRuntimeTest(unittest.TestCase):
    """The page's own read of the board, lifted out of the SHIPPED page and
    run: while a section is still being read it asks again soon, and it stops
    asking once nothing is."""

    FNS = ("boardRetryMs", "boardShown", "loadBoard", "boardActive")
    CONSTS = ("BOARD_POLL_MS", "BOARD_FETCH_MS", "BOARD_RETRY_MS")

    HARNESS = r"""
let BOARD = null, BOARD_AGAIN = null, BOARD_TRIES = 0;
const TIMERS = [], ASKED = [];
let NEXT = null, RENDERS = 0, SHOWN = true;
function setTimeout(fn, ms) { TIMERS.push({fn, ms}); return TIMERS.length; }
function clearTimeout(id) {}
async function j(url, ms) { ASKED.push([url, ms]); return JSON.parse(JSON.stringify(NEXT)); }
function renderBoard() { RENDERS++; }
const $ = sel => ({classList: {contains: c => c === "on" && SHOWN}});
const document = {hidden: false};
"""

    DRIVER = r"""
(async () => {
const out = {};
NEXT = READING; await loadBoard();
out.first = TIMERS.map(t => t.ms);
await TIMERS[0].fn();                      // the quick read: still reading
out.second = TIMERS.map(t => t.ms);
NEXT = DONE; await TIMERS[1].fn();         // the leg has landed
out.done = TIMERS.map(t => t.ms);
out.asked = ASKED.map(a => a[0]);
out.renders = RENDERS;
out.seats = BOARD.sections.seats;
SHOWN = false; NEXT = READING; await loadBoard();
out.off_fired = await TIMERS[TIMERS.length - 1].fn();
out.off_asked = ASKED.length;
out.ms = [boardRetryMs(READING, 0), boardRetryMs(READING, 3),
          boardRetryMs(READING, 4), boardRetryMs(DONE, 0),
          boardRetryMs({unavailable: "x"}, 0), boardRetryMs(null, 0)];
out.active = boardActive(MIXED);
console.log(JSON.stringify(out));
})();
"""

    @classmethod
    def setUpClass(cls):
        from tests.test_web_chat_client_runtime import _extract_fn
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        src = web_ui_loader.read_text()
        cls.render_src = _extract_fn(src, "renderBoard")

        def decl(name):
            m = re.search(_DECL % re.escape(name), src, re.M)
            assert m, "no declaration of %s in the assembled page" % name
            return m.group(0)
        done = _board()
        reading = _board(running=None, possible=None,
                         seats=dict(_READING))
        mixed = [dict(_row("a"), status="active"),
                 dict(_row("b"), status="dormant"),
                 dict(_row("c"), status="active"),
                 dict(_row("d"), status="retired"), _row("e", "green")]
        payload = "".join("const %s = %s;\n" % (k, json.dumps(v)) for k, v in
                          (("READING", reading), ("DONE", done),
                           ("MIXED", mixed)))
        cls.tmp = tempfile.mkdtemp(prefix="helm-web-board-again-")
        path = os.path.join(cls.tmp, "run.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(cls.HARNESS + "\n".join(decl(n) for n in cls.CONSTS)
                    + "\n" + payload
                    + "\n\n".join(_extract_fn(src, n) for n in cls.FNS)
                    + cls.DRIVER)
        cls.proc = subprocess.run([cls.node, path], capture_output=True,
                                  text=True, timeout=60)
        try:
            cls.out = json.loads(cls.proc.stdout or "{}")
        except json.JSONDecodeError:
            cls.out = {}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def setUp(self):
        self.assertTrue(self.out, "node produced no output: "
                        + (self.proc.stderr or "")[:600])

    def test_a_section_still_being_read_is_asked_for_again_soon(self):
        """5s, then 10s: well inside the minute's own read, and not a
        cadence of its own once the leg has landed."""
        self.assertEqual(self.out["first"], [5000])
        self.assertEqual(self.out["second"], [5000, 10000])
        self.assertEqual(self.out["done"], [5000, 10000],
                         "the page kept asking after nothing was loading")
        self.assertEqual(self.out["asked"], ["/api/board"] * 3)
        self.assertEqual(self.out["renders"], 3)
        self.assertFalse(self.out["seats"].get("loading"))

    def test_the_quick_read_waits_for_the_work_page_to_be_on_screen(self):
        self.assertFalse(self.out["off_fired"])
        self.assertEqual(self.out["off_asked"], 4,
                         "a hidden page fetched the board on the quick timer")

    def test_the_quick_read_backs_off_and_hands_over_to_the_minute(self):
        self.assertEqual(self.out["ms"],
                         [5000, 40000, None, None, None, None])

    def test_the_active_count_is_the_projects_the_scan_calls_active(self):
        """The restored "N active" is the scan's own word: a status of
        `active`, whatever the light says. The page's wiring passes this
        count, not the number of rows."""
        self.assertEqual(self.out["active"], 3)
        self.assertIn("boardCapacity(d, boardActive(projects))",
                      self.render_src)


class BoardWarmStartTest(unittest.TestCase):
    """`helm web` warms the board at start-up without waiting on it."""

    def _start(self, hold):
        """Start `helm web` with a board read that takes `hold` seconds, and
        return what the server saw the moment it began to serve: (the warm-up
        had begun, the warm-up had finished, seconds from start to serving)."""
        from helm import web_server, webserve
        tmp = tempfile.TemporaryDirectory(prefix="helm-web-warm-")
        self.addCleanup(tmp.cleanup)
        began, finished, gate, seen = (threading.Event(), threading.Event(),
                                       threading.Event(), [])
        self.addCleanup(gate.set)

        def slow_board():
            began.set()
            gate.wait(hold)
            finished.set()
            return {}

        class Serving:
            server_address = ("127.0.0.1", 0)

            def serve_forever(self):
                seen.append((began.wait(5), finished.is_set(),
                             time.monotonic() - started))
                raise KeyboardInterrupt

            def server_close(self):
                pass
        with mock.patch.dict(os.environ, {"HELM_HOME": tmp.name}), \
                mock.patch.object(web_server, "make_server",
                                  lambda port: Serving()), \
                mock.patch.object(web_server, "_prewarm_configs",
                                  lambda: None), \
                mock.patch.object(web, "_api_board", slow_board), \
                mock.patch.object(webserve, "register"), \
                mock.patch.object(webserve, "forget"), \
                mock.patch("sys.stdout"):
            started = time.monotonic()
            self.assertEqual(web_server.cmd_web([]), 0)
        self.assertEqual(len(seen), 1)
        return seen[0]

    def test_the_server_serves_while_the_board_warms(self):
        """The warm-up has STARTED and has NOT finished when the server
        begins to serve, and serving did not wait out the board read."""
        began, finished, took = self._start(10)
        self.assertTrue(began, "the board was never warmed")
        self.assertFalse(finished, "the server waited for the warm-up")
        self.assertLess(took, 5)

    def test_the_arm_sees_a_warm_up_that_blocks(self):
        """The control on the arm above: the same start with the warm-up run
        in line, the shape a blocking prewarm has, is caught by both of its
        observables. The earlier arm could not see it."""
        from helm import web_server
        with mock.patch.object(web_server, "_prewarm",
                               lambda name, warm: warm()):
            began, finished, took = self._start(0.5)
        self.assertTrue(began)
        self.assertTrue(finished, "a blocking warm-up read as a background one")
        self.assertGreaterEqual(took, 0.5)

if __name__ == "__main__":
    unittest.main()


def _work_placed(running, loops=(), **pipe):
    """WHERE THE WORK PAGE PUTS EACH HELD LANE: `running` (a project's
    `/api/board` rooms) and `loops` (its live land requests, as
    `web_board._kanban_card` sends them) read by `work_model.build`, the one
    reader `/api/work` serves and the Work page draws (task/3643 slice 6
    retired the land board's kanban that drew them before). -> {lane: sorted
    places}, a place being the stage of the move the lane is on, or "record"
    for a room on landed work whose lease release is all that is owed
    (Leftovers the agents clear). `pipe` extends the project's pipeline
    record (its `on_main` and `collapsed` lines)."""
    from helm import work_model
    now = time.time()
    sec = {"source": "x", "measured_at": now - 5, "age_s": 5,
           "limit_s": 1200, "unavailable": None, "stale": False}
    live = list(loops)
    board = {"generated_at": now,
             "sections": {"lands": dict(sec, scope="proj"),
                          "fleet": dict(sec, scope="proj"),
                          "seats": dict(sec), "tasks": dict(sec)},
             "projects": {"proj": {"lanes": dict({
                 "loops": live, "loops_cut": [], "loops_more": {},
                 "rehold": None, "tally": web_board._kanban_tally(live),
                 "on_main": None, "collapsed": []}, **pipe),
                 "running": list(running)}}}
    snap = work_model.build({
        "now": now, "board": board, "projects": {"proj": {"name": "proj"}},
        "tasks": {"rows": {}, "history": {}, "unavailable": None},
        "dispatch": {"rows": {}, "events": {}, "chains": {},
                     "unavailable": None},
        "trains": {"trains": [], "lands": {}, "ejections": [],
                   "unavailable": None},
        "train_project": "proj", "zone": "America/Los_Angeles"})
    got = {}
    for card in snap["cards"].values():
        for act in card["actions"]:
            got.setdefault(act["lane"], []).append(act["stage"])
    for rec in snap["records"]:
        got.setdefault(rec["lane"], []).append("record")
    return {lane: sorted(places) for lane, places in got.items()}


def _claim_room(lane, state, proof="LANDED by ancestry (the tip itself is "
                "on the trunk)", dirty=None):
    """One held room as `web_board._seats_join` sends it in `running`."""
    return {"lane": lane, "kind": "claim", "seats": ["s1"],
            "landed": {"state": state, "proof": proof, "tip": "ab" * 6},
            "dirty": dirty}


class LandedOnTheWorkPageTest(unittest.TestCase):
    """WHAT THE OWNER READ: "about half the listed lanes already landed ...
    why werent they listed as landed in helm?" The land board's kanban drew
    a lane BUILDING for as long as its lease was held; it is retired
    (task/3643 slice 6), and these arms read the same wire shapes through
    the Work page's one reader. What the page does with each card it is
    sent is run in tests/test_web_work_page.py; how each row is typed is
    tests/test_work_model.py."""

    def test_a_room_on_landed_work_is_a_leftover_never_building(self):
        """A landed lease is a record whose lease release its seat owes
        (Leftovers the agents clear); unlanded and never-started rooms are
        their seat's Building move. THE GAP, named: a landed room whose room
        holds uncommitted work was drawn "room DIRTY (commit or park first)"
        on the kanban; `/api/work`'s rooms carry no `dirty`, so the page
        cannot say it, and `helm work list` still does."""
        got = _work_placed([_claim_room("landed-lane", "landed"),
                            _claim_room("dirty-landed", "landed", dirty=True),
                            _claim_room("building-lane", "unlanded"),
                            _claim_room("just-claimed", "unstarted"),
                            _claim_room("never-landed", "gone")])
        self.assertEqual(got, {"landed-lane": ["record"],
                               "dirty-landed": ["record"],
                               "building-lane": ["building"],
                               "just-claimed": ["building"],
                               "never-landed": ["building"]})

    def test_a_BUILD_row_sent_against_trunk_is_building_not_landed(self):
        """fold-checkpoint-key-3048 and seat-signs-as-itself-3049 read as
        LANDED the moment they were sent. The projection answers a build
        row's containment off its LANE, so a build with nothing authored is
        not contained, and the Work page draws AWAITING_BUILD in Building,
        beside a request under review in In review."""
        build = web_board._kanban_card({"id": "b", "state": "AWAITING_BUILD",
                                        "lane": "seat-signs-as-itself-3049",
                                        "kind": "build",
                                        "trunk_contains_tip": False})
        review = web_board._kanban_card({"id": "r", "lane": "under-review",
                                         "state": "AWAITING_REVIEW",
                                         "trunk_contains_tip": False})
        self.assertEqual(_work_placed([], [build, review]),
                         {"seat-signs-as-itself-3049": ["building"],
                          "under-review": ["review"]})

    def test_the_server_marks_what_is_on_main_with_no_verdict(self):  # noqa: VACUOUS_ASSERTION — the owner-gated card's flag is asserted True and both cards are asserted placed by an exact lane list
        """THE SERVER DECIDES, ONCE: the card carries whether its work is
        on main with no verdict recorded, and a FIX-verdicted row whose tip
        is on main is not that (it stays listed, a fix owed)."""
        fix = web_board._kanban_card({"id": "f", "lane": "fix-on-main",
                                      "state": "CHANGES_REQUESTED",
                                      "polarity": "fix",
                                      "trunk_contains_tip": True})
        gated = web_board._kanban_card({"id": "g", "lane": "owner-gated",
                                        "state": "AWAITING_REVIEW",
                                        "trunk_contains_tip": True})
        self.assertIs(fix["on_main_unverdicted"], False)
        self.assertIs(gated["on_main_unverdicted"], True)
        # THE PAGE NEVER REFOLDS a card it was sent: both are placed, as
        # moves on work that is on trunk
        got = _work_placed([], [fix, gated])
        self.assertEqual(sorted(got), ["fix-on-main", "owner-gated"])


def _loop(rid, lane, state="AWAITING_REVIEW", dwell_s=3600, **kw):
    """One in-flight land-request card, in the fields `landreq_cli.card`
    carries and `web_land._lr_project` stamps the census onto."""
    row = {"id": rid, "lane": lane, "state": state, "kind": "review",
           "terminal": False, "honored": False, "chain_root": rid,
           "supersedes": None, "owed_by": "reviewer",
           "holder_role": "reviewer", "holder_seat": "codex",
           "dwell_s": dwell_s, "dwell_known": True, "hold_ts": None,
           "contrary": False, "stalled": False,
           "trunk_contains_tip": None, "frontier": None,
           "frontier_rung": None}
    row.update(kw)
    return row


class LiveObligationsJoinTest(unittest.TestCase):
    """THE BOARD'S JSON LISTS LIVE OBLIGATIONS; THE REST IS ONE LINE EACH.

    The owner read "unknown (declared verdict held) 51 waiting, oldest 52d",
    "author @integrator 40 waiting, oldest 60d", and a kanban LANDED
    column of lanes "on main, no verdict recorded" — two of them BUILD rows
    that read as landed the moment they were sent. `/api/board` is what the
    page and any seat reading it consume, so the rule is asserted here, on the
    wire, before anything is drawn."""

    DAY = 86400

    def join(self, cards, active, loops=None, lands=()):
        model = scheduler.project(cards, active_ids=active, now=time.time(),
                                  projection_age_s=5)
        by_id = {c["id"]: c for c in cards}
        body = {"withheld": {"scope": "proj"}, "read_age_s": 5,
                "unavailable": None,
                "loops": [by_id[i] for i in (loops if loops is not None
                                             else active)],
                "building": {"rows": [], "total": 0, "unavailable": None},
                "scheduler": model,
                "recent_lands": {"rows": list(lands), "total": len(lands),
                                 "unavailable": None}}
        _sec, rec = web_board._lands_join(lambda _qs: (body, 200))
        return rec["proj"]

    def test_waits_list_live_rows_and_one_line_per_collapsed_class(self):
        cards = [
            _loop("live", "live-lane", holder_role="author",
                  holder_seat="opus", frontier="on-frontier",
                  frontier_rung="lane-family"),
            _loop("anc", "anc-lane", holder_role="author", holder_seat="opus",
                  frontier="landed-by-ancestry", frontier_rung="landing",
                  dwell_s=60 * self.DAY),
            _loop("pid", "pid-lane", frontier="landed-by-patch-id",
                  frontier_rung="landing", dwell_s=9 * self.DAY),
            _loop("pruned", "pruned-lane", frontier="unclassified",
                  frontier_rung="object", dwell_s=20 * self.DAY),
            # a FRESH review whose label matches no branch: `tip`, work owed
            _loop("fresh", "fresh-review", frontier="unclassified",
                  frontier_rung="tip", dwell_s=600),
            _loop("held", "held-lane", state="REVIEWED",
                  holder_role="unknown (declared verdict held)",
                  holder_seat=None, dwell_s=52 * self.DAY),
            _loop("ready", "ready-lane", state="READY", holder_role="lander",
                  holder_seat=None, frontier="on-frontier",
                  frontier_rung="lane-family", dwell_s=13 * self.DAY)]
        rec = self.join(cards, active=["live", "anc", "pid", "pruned",
                                       "fresh", "ready"])
        listed = {w["label"]: w["count"] for w in rec["waits"]}
        self.assertEqual(listed, {"author @opus": 1, "lander": 1,
                                  "reviewer @codex": 1},
                         "a row off the live frontier, an unplaced row or an "
                         "absorbed row was listed as a wait")
        lines = {c["class"]: c for c in rec["waits_collapsed"]}
        self.assertEqual({k: v["count"] for k, v in lines.items()},
                         {"off_frontier": 2, "unclassified": 1,
                          "superseded": 1})
        self.assertEqual(lines["off_frontier"]["oldest_age_s"],
                         60 * self.DAY + 5)
        self.assertEqual(lines["off_frontier"]["command"],
                         "helm lr retire --off-frontier")
        self.assertEqual(lines["superseded"]["command"], "helm lr list --all")
        for line in rec["waits_collapsed"]:
            self.assertTrue(line["label"] and line["command"], line)

    def test_the_kanban_draws_live_cards_and_one_on_main_line(self):
        cards = [
            # a BUILD sent against trunk: the projection answered its lane
            # (nothing authored), so its tip is NOT contained — it builds
            _loop("build", "seat-signs-as-itself", state="AWAITING_BUILD",
                  kind="build", trunk_contains_tip=False,
                  frontier="on-frontier", frontier_rung="lane-family"),
            _loop("onmain1", "reviewed-in-chat", trunk_contains_tip=True,
                  frontier="on-frontier", frontier_rung="lane-family",
                  dwell_s=3 * self.DAY),
            _loop("onmain2", "also-in-history", trunk_contains_tip=True,
                  dwell_s=self.DAY),
            _loop("left", "left-over", frontier="landed-by-ancestry",
                  frontier_rung="landing", trunk_contains_tip=True),
            _loop("review", "under-review", frontier="on-frontier",
                  frontier_rung="lane-family", trunk_contains_tip=False)]
        rec = self.join(cards, active=[c["id"] for c in cards])
        lanes = rec["lanes"]
        self.assertEqual(sorted(c["lane"] for c in lanes["loops"]),
                         ["seat-signs-as-itself", "under-review"])
        self.assertEqual(lanes["in_flight"], 2)
        on_main = lanes["on_main"]
        self.assertEqual(on_main["count"], 2)
        self.assertEqual(sorted(on_main["lanes"]),
                         ["also-in-history", "reviewed-in-chat"])
        self.assertEqual(on_main["oldest_age_s"], 3 * self.DAY + 5)
        self.assertEqual(on_main["command"], "helm lr list")
        self.assertIn("no verdict recorded", on_main["label"])
        self.assertEqual([(c["class"], c["count"]) for c in lanes["collapsed"]],
                         [("off_frontier", 1)])

    def test_the_waits_and_the_kanban_fold_the_same_rows_on_main(self):
        """FINDING 3, RULED: ONE RULE ON BOTH SURFACES. The kanban folded on
        the containment mark and the waits never read it, so the integrator's
        listed waits held 8 rows the kanban beside them counted "on main with
        no verdict recorded". Both now ask `scheduler.collapse_class`, over
        `landreq.on_main_unverdicted`: the same rows are counted on the same
        line with the same count and command on both, none of them is a listed
        wait, and a row on main under a recorded FIX is listed on both."""
        cards = [
            _loop("clean1", "source-clean-1", holder_role="integrator",
                  holder_seat=None, owed_by="integrator",
                  trunk_contains_tip=True, frontier="unclassified",
                  frontier_rung="tip", dwell_s=5 * self.DAY),
            _loop("clean2", "source-clean-2", holder_role="integrator",
                  holder_seat=None, owed_by="integrator",
                  trunk_contains_tip=True, frontier="on-frontier",
                  frontier_rung="lane-family", dwell_s=self.DAY),
            _loop("fix", "fix-on-main", state="CHANGES_REQUESTED",
                  polarity="fix", holder_role="author", holder_seat="opus",
                  owed_by="author", trunk_contains_tip=True,
                  frontier="on-frontier", frontier_rung="lane-family"),
            _loop("live", "live-review", trunk_contains_tip=False,
                  frontier="on-frontier", frontier_rung="lane-family")]
        rec = self.join(cards, active=[c["id"] for c in cards])
        listed = sorted(r["plain_title"] for g in rec["waits"]
                        for r in g["rows"])
        self.assertEqual(listed, ["fix-on-main", "live-review"])
        waits_line = {c["class"]: c for c in rec["waits_collapsed"]}["on_main"]
        kanban_line = rec["lanes"]["on_main"]
        self.assertEqual(waits_line["count"], 2)
        for key in ("count", "label", "command", "oldest_age_s"):
            self.assertEqual(waits_line[key], kanban_line[key], key)
        self.assertEqual(waits_line["oldest_age_s"], 5 * self.DAY + 5)
        self.assertEqual(sorted(kanban_line["lanes"]),
                         ["source-clean-1", "source-clean-2"])
        self.assertEqual(sorted(c["lane"] for c in rec["lanes"]["loops"]),
                         ["fix-on-main", "live-review"])
        # THE ACCOUNTING HOLDS ON BOTH SURFACES
        model_lines = sum(c["count"] for c in rec["waits_collapsed"])
        self.assertEqual(len(listed) + model_lines, len(cards))
        self.assertEqual(len(rec["lanes"]["loops"]) + kanban_line["count"]
                         + sum(c["count"] for c in rec["lanes"]["collapsed"]),
                         len(cards))

    def test_the_waits_past_the_listed_groups_are_counted(self):
        """task/3130: the waits list TOP_WAITS groups; the rest are counted
        in `waits_more`, never dropped silently. THE CONTROL: a model with
        fewer groups counts none."""
        groups = [{"label": "g%d" % i, "count": 1, "oldest_age_s": 60,
                   "rows": []} for i in range(web_board.TOP_WAITS + 2)]
        body = {"withheld": {"scope": "proj"}, "read_age_s": 5,
                "unavailable": None, "loops": [],
                "scheduler": {"unavailable": None, "groups": groups},
                "recent_lands": {"rows": [], "total": 0, "unavailable": None}}
        _sec, rec = web_board._lands_join(lambda _qs: (body, 200))
        self.assertEqual(len(rec["proj"]["waits"]), web_board.TOP_WAITS)
        self.assertEqual(rec["proj"]["waits_more"], 2)
        body["scheduler"]["groups"] = groups[:3]
        _sec, rec = web_board._lands_join(lambda _qs: (body, 200))
        self.assertEqual([g["label"] for g in rec["proj"]["waits"]],
                         ["g0", "g1", "g2"])
        self.assertEqual(rec["proj"]["waits_more"], 0)

    def test_nothing_on_main_is_no_line_not_a_zero_claim(self):
        rec = self.join([_loop("r", "lane-r")], active=["r"])
        # THE POSITIVE CONTROL on the same join: the live row IS drawn
        self.assertEqual([c["lane"] for c in rec["lanes"]["loops"]],
                         ["lane-r"])
        self.assertEqual([w["count"] for w in rec["waits"]], [1])
        self.assertIsNone(rec["lanes"]["on_main"])
        self.assertEqual(rec["lanes"]["collapsed"], [])
        self.assertEqual(rec["waits_collapsed"], [])

    def test_every_other_projects_kanban_takes_the_same_split(self):
        body = {"withheld": {"scope": "alpha"}, "read_age_s": 5,
                "unavailable": None,
                "loops": [
                    _loop("b1", "b-live", foreign_project="beta"),
                    _loop("b2", "b-on-main", foreign_project="beta",
                          trunk_contains_tip=True),
                    _loop("b3", "b-build", foreign_project="beta",
                          state="AWAITING_BUILD", kind="build",
                          trunk_contains_tip=False)],
                "recent_lands": {"rows": [], "total": 0, "unavailable": None}}
        with mock.patch.object(web_board, "_fleet_kick", lambda: None), \
                mock.patch.dict(web_board._FLEET,
                                {"done": (time.time(), body, None)}):
            _sec, out = web_board._fleet_now(["alpha", "beta"], time.time())
        beta = out["beta"]
        self.assertEqual(sorted(c["lane"] for c in beta["loops"]),
                         ["b-build", "b-live"])
        self.assertEqual(beta["on_main"]["count"], 1)
        self.assertEqual(beta["on_main"]["lanes"], ["b-on-main"])


class LandedLaneParityTest(LandedWorld):
    """THE AGENT VIEW AND THE OWNER VIEW OF ONE LANE GIVE ONE ANSWER.

    "that's not *my* board that's our board, just the webui (UX not AX)
    version" (the owner): agents read `helm work list`, the owner
    reads the Work page, and both must say the same thing about the same
    lane. One real tree, three lanes — merged, open, claimed at the trunk —
    read through the CLI render and through `/api/board`'s seats join, then
    placed by the Work page's one reader (`_work_placed`)."""

    def board(self):
        from helm import seats
        rep = {"seats": [], "claims": seats.claims_list(gc=False),
               "roster_failed": False}
        _sec_, out, _fleet = web_board._seats_join(
            {"proj": {"path": self.root}}, {"families": {}}, time.time(), rep)
        return out["proj"]["running"]

    def world(self):
        self.lane("merged", commits=1)
        self.lane("open", commits=1)
        self.land("merged")
        self.lane("fresh")

    def test_the_CLI_and_the_board_agree_about_every_lane(self):
        from helm import work
        self.world()
        rc, out, err = self.work("list", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        cli = {r["lane"]: r["landed"] for r in work.list_rows(self.root)}
        web = {r["lane"]: r["landed"] for r in self.board()}
        self.assertEqual(sorted(web), ["fresh", "merged", "open"])
        for lane in web:
            self.assertEqual((web[lane]["state"], web[lane]["proof"]),
                             (cli[lane]["state"], cli[lane]["proof"]), lane)
        self.assertEqual(web["merged"]["state"], "landed")
        self.assertEqual(web["fresh"]["state"], "unstarted")
        # THE RENDERED WORDS, both surfaces: the CLI line under the row ...
        landed_lines = [l for l in out.splitlines() if "LANDED —" in l]
        self.assertEqual(len(landed_lines), 1, out)
        self.assertIn("helm work release merged", landed_lines[0])
        # ... and the owner's Work page, placed from the SAME board rows: the
        # landed room is a leftover whose lease release is owed (the CLI's
        # "helm work release merged"), the others are Building
        self.assertEqual(_work_placed(self.board()),
                         {"merged": ["record"], "fresh": ["building"],
                          "open": ["building"]})

    def test_a_second_board_read_asks_git_only_the_landed_rooms_dirt(self):  # noqa: VACUOUS_ASSERTION — the second read's calls are asserted EQUAL to ["status", "worktree"], a positive non-empty list, and the first read's assertGreater(asked, 0) is its unconditional control
        """LANDEDNESS IS ANSWERED FROM ITS STAMP; DIRT IS NOT STAMPABLE. Over
        an unmoved repository the second read asks git nothing about which
        lanes landed, and re-reads the working tree of the LANDED rooms only —
        one `git status` each, never one per claim — plus the registry that
        locates them."""
        self.world()
        calls, spy = self.spy()
        with spy:
            first = self.board()
            asked = len(calls)
            second = self.board()
        self.assertGreater(asked, 0, "MUST-HIT: the first board read asked "
                           "git whether its lanes landed")
        # three claims, one landed: one status, on each read
        for got in (calls[:asked], calls[asked:]):
            self.assertEqual(len([c for c in got if c[0] == "status"]), 1,
                             got)
        self.assertEqual(sorted(c[0] for c in calls[asked:]),
                         ["status", "worktree"],
                         "a board rebuilt over an unmoved repository asked "
                         "git more than the landed room's dirt: %r"
                         % (calls[asked:],))
        self.assertEqual(first, second)


class LandedSurfaceMatrixTest(LandedWorld):
    """EVERY SURFACE THAT ANSWERS "IS THIS HELD LANE DONE?", OVER EVERY STATE
    A HELD LANE CAN BE IN, IN ONE REAL TREE.

    Surfaces: `helm work list` (the LANDED line and its DIRTY clause),
    `helm lr foldcheck`'s advice (`_print_landed_leases`), `landed_leases`
    (release / kept), the Work page (where its one reader places each
    lane, `_work_placed`) and the web headline (`headline.lanes.landed`, drawn by
    the SHIPPED `boardCapacity`). States: landed clean, landed under a dirty
    room, reset back to the trunk after committing, an idle lane that
    fast-forwarded the trunk in, claimed at the trunk, an idle lane that
    merged the trunk in with a merge commit, landed by patch identity.

    `MATRIX` is the expected cell for every (state, surface); each arm reads
    one surface for every state, so a failure names the surface and the lane.
    """

    # lane: (verdict, CLI line, landed_leases, Work page places). A landed
    # room is a record (Leftovers the agents clear); the dirty one too —
    # `/api/work`'s rooms carry no `dirty`, so the page cannot keep it in
    # Building the way the retired kanban did, and the CLI's DIRTY line and
    # `landed_leases` keeping it are where that is read.
    MATRIX = {
        "clean":     ("landed", "LANDED", "release", ("record",)),
        "dirty":     ("landed", "LANDED DIRTY", "kept", ("record",)),
        "reset":     ("unlanded", None, None, ("building",)),
        "ffidle":    ("unstarted", None, None, ("building",)),
        "fresh":     ("unstarted", None, None, ("building",)),
        "mergeidle": ("unknown", None, None, ("building",)),
        "picked":    ("landed", "LANDED", "release", ("record",)),
    }

    def world(self):
        self.lane("clean", commits=1)
        dirty = self.lane("dirty", commits=1)
        reset = self.lane("reset", commits=2)
        ffidle = self.lane("ffidle")
        mergeidle = self.lane("mergeidle")
        picked = self.lane("picked", commits=1)
        self.land("clean")
        self.land("dirty")
        # THE TRUNK MOVED FIRST (the two lands), so the pick mints a new
        # object: landed by content, still ahead by object id
        self.git(self.root, "cherry-pick",
                 self.git(picked, "rev-parse", "HEAD"))
        self.push()
        with open(os.path.join(dirty, "uncommitted.txt"), "w") as f:
            f.write("precious uncommitted bytes\n")
        self.git(reset, "reset", "--hard", "origin/main")
        self.git(ffidle, "merge", "-q", "origin/main")
        self.git(mergeidle, "merge", "--no-ff", "-q", "-m",
                 "merge the trunk in", "origin/main")
        self.lane("fresh")
        # MUST-HIT: each fixture is the shape it is named for
        trunk = self.git(self.root, "rev-parse", "origin/main")
        for lane in ("reset", "ffidle", "fresh"):
            self.assertEqual(self.git(self.root, "rev-parse", "lane/" + lane),
                             trunk, lane)
        self.assertEqual(self.git(self.root, "rev-list", "--count",
                                  "--merges", "origin/main..lane/mergeidle"),
                         "1")
        self.assertEqual(self.git(self.root, "rev-list", "--count",
                                  "origin/main..lane/picked"), "1")

    def board(self):
        from helm import seats
        rep = {"seats": [], "claims": seats.claims_list(gc=False),
               "roster_failed": False}
        _sec_, out, fleet = web_board._seats_join(
            {"proj": {"path": self.root}}, {"families": {}}, time.time(), rep)
        return out["proj"]["running"], fleet

    @staticmethod
    def capacity(fleet):
        """`boardCapacity` from the SHIPPED page over a board carrying
        `fleet` as its headline — the words the owner reads."""
        from tests.test_web_accounts import _extract_const
        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("node not available")
        src = web_ui_loader.read_text()
        fns = "\n\n".join(_extract_fn(src, n) for n in (
            "lrDur", "lrAgo", "boardSec", "boardSecState",
            "boardCapacity"))
        board = {"headline": {"lanes": fleet},
                 "sections": {"seats": _sec(), "flags": _sec(families={})},
                 "projects": {}}
        script = ("const esc = s => String(s);\n"
                  + _extract_const(src, "FLAGCOL") + "\n" + fns + "\n"
                  + "console.log(JSON.stringify(boardCapacity(%s)));\n"
                  % json.dumps(board))
        tmp = tempfile.mkdtemp(prefix="helm-web-board-matrix-")
        try:
            path = os.path.join(tmp, "run.js")
            with open(path, "w", encoding="utf-8") as f:
                f.write(script)
            proc = subprocess.run([node, path], capture_output=True,
                                  text=True, timeout=60)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        assert proc.returncode == 0, proc.stderr[:2000]
        return re.sub(r"<[^>]*>", "", json.loads(proc.stdout))

    def setUp(self):
        super().setUp()
        self.world()

    def test_the_verdict_and_dirt_are_one_answer_on_the_CLI_and_the_board(self):  # noqa: VACUOUS_ASSERTION — the lane set is asserted equal to MATRIX's seven lanes first, so the per-lane loop runs seven times; dirty True / clean False / patch identity / reflog are unconditional positives after it
        from helm import work
        cli = {r["lane"]: r for r in work.list_rows(self.root)}
        web = {r["lane"]: r for r in self.board()[0]}
        self.assertEqual(sorted(web), sorted(self.MATRIX))
        for lane, (verdict, _cli, _rel, _cols) in self.MATRIX.items():
            self.assertEqual(cli[lane]["landed"]["state"], verdict,
                             (lane, cli[lane]["landed"]))
            self.assertEqual((web[lane]["landed"]["state"],
                              web[lane]["landed"]["proof"]),
                             (cli[lane]["landed"]["state"],
                              cli[lane]["landed"]["proof"]), lane)
            # DIRT: the board asks it of LANDED lanes only, and there it is
            # the CLI's own flag; elsewhere it is not asked (null)
            self.assertEqual(web[lane]["dirty"],
                             cli[lane]["dirty"] if verdict == "landed"
                             else None, lane)
        self.assertIs(web["dirty"]["dirty"], True)
        self.assertIs(web["clean"]["dirty"], False)
        self.assertIn("patch identity", web["picked"]["landed"]["proof"])
        self.assertIn("only in the reflog", web["reset"]["landed"]["proof"])

    def test_helm_work_list_prints_LANDED_and_DIRTY_where_the_matrix_says(self):  # noqa: VACUOUS_ASSERTION — MATRIX holds three LANDED cells and one LANDED DIRTY cell, each asserted by equality on the rendered line; next() raises if a lane has no row
        rc, out, err = self.work("list", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        lines = out.splitlines()
        for lane, (_v, want, _rel, _cols) in self.MATRIX.items():
            at = next(i for i, l in enumerate(lines)
                      if "GUARDED %s " % lane in l)
            follow = lines[at + 1] if at + 1 < len(lines) else ""
            said = None if "LANDED —" not in follow else (
                "LANDED DIRTY" if "room is DIRTY" in follow else "LANDED")
            self.assertEqual(said, want, (lane, follow))

    def test_landed_leases_releases_the_clean_ones_and_keeps_the_dirty_one(self):
        from helm import work
        release, kept = work.landed_leases(self.root)
        got = {r["lane"]: "release" for r in release}
        got.update({r["lane"]: "kept" for r, _why in kept})
        self.assertEqual(got, {lane: row[2] for lane, row in
                               self.MATRIX.items() if row[2]})
        self.assertIn("DIRTY", dict((r["lane"], w) for r, w in kept)["dirty"])

    def test_foldcheck_advice_names_the_same_lanes(self):
        import contextlib
        import io
        from helm import landreq_cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            landreq_cli._print_landed_leases(self.root)
        out = buf.getvalue()
        self.assertIn("LEASES ON LANDED WORK", out)
        release, _, kept = out.partition("KEPT")
        for lane, (_v, _cli, want, _cols) in self.MATRIX.items():
            said = ("release" if "  %s (" % lane in release
                    else "kept" if "  %s (" % lane in kept else None)
            self.assertEqual(said, want, (lane, out))

    def test_the_work_page_places_each_lane_where_the_matrix_says(self):  # noqa: VACUOUS_ASSERTION — the placed lanes are asserted EQUAL to the matrix's seven before the per-lane equalities run
        running, _fleet = self.board()
        got = _work_placed(running)
        self.assertEqual(sorted(got), sorted(self.MATRIX))
        for lane, (_v, _cli, _rel, want) in self.MATRIX.items():
            self.assertEqual(tuple(got[lane]), want, lane)

    def test_the_headline_counts_the_landed_held_lanes_in_R_and_names_them(self):
        running, fleet = self.board()
        landed = sorted(r["lane"] for r in running
                        if r["landed"]["state"] == "landed")
        self.assertEqual(landed, sorted(lane for lane, row in
                                        self.MATRIX.items()
                                        if row[0] == "landed"))
        self.assertEqual((fleet["claimed"], fleet["landed"]),
                         (len(self.MATRIX), len(landed)))
        self.assertIn("%d lanes running, %d of them landed (lease still held)"
                      % (fleet["running"], len(landed)),
                      self.capacity(fleet))


class BoardPipelineFeedTest(unittest.TestCase):
    """WHAT THE SERVER SENDS OF ONE PROJECT'S PIPELINE, and that it cuts
    nothing silently (task/3129, task/3130). The land board's kanban that
    drew these is retired (task/3643 slice 6); the Work page reads them
    through `/api/work`, whose reader counts every row the pipeline sent
    (tests/test_work_model.py), so what is pinned here is the feed."""

    def served(self, cards):
        live, on_main, folded = web_board._kanban_split(cards, 0)
        feed = web_board._kanban_feed(live)
        return feed["loops"], {"on_main": on_main, "collapsed": folded,
                               "loops_more": feed["loops_more"],
                               "rehold": feed["rehold"]}

    def test_every_state_the_cap_cut_is_counted(self):
        cards = ([_loop("r%d" % i, "rev-%d" % i) for i in range(4)]
                 + [_loop("g%d" % i, "gate-%d" % i, state="READY")
                    for i in range(2)]
                 + [_loop("b0", "build-0", state="AWAITING_BUILD",
                          kind="build")])
        with mock.patch.object(web_board, "KANBAN_ROWS", 3):
            loops, extra = self.served(cards)
            under, under_extra = self.served(cards[:2])
        self.assertEqual(extra["loops_more"],
                         {"AWAITING_REVIEW": 1, "READY": 2,
                          "AWAITING_BUILD": 1})
        self.assertEqual(len(loops) + sum(extra["loops_more"].values()),
                         len(cards))
        # UNDER THE CAP, the control: every card sent, nothing more
        self.assertEqual([c["lane"] for c in under], ["rev-0", "rev-1"])
        self.assertFalse(under_extra["loops_more"])

    def test_landed_re_holds_are_one_line_not_cards(self):
        door = "helm dispatch release x; helm dispatch hold x <reason>"
        held = [_loop("h%d" % i, "held-%d" % i, trunk_contains_tip=True,
                      source_clean_tip="c" * 40, reviewer="kimi",
                      source_clean_rehold={"kind": "NO HOLDER", "door": door,
                                           "why": "NO HOLDER; " + door},
                      dwell_s=(i + 1) * 86400)
                for i in range(2)]
        for row in held:
            row["source_clean_on_main"] = landreq.source_clean_on_main(row)
            self.assertIn("RE-HOLD owed by @kimi", row["source_clean_on_main"])
        live = _loop("r1", "can-move")
        only_loops, only = self.served(held)
        loops, extra = self.served([held[0], live, held[1]])
        self.assertEqual(only_loops, [])
        self.assertEqual(only["rehold"]["count"], 2)
        # MIXED: the card that can move is sent, the re-holds on the line
        self.assertEqual([c["lane"] for c in loops], ["can-move"])
        self.assertEqual(extra["rehold"]["count"], 2)

    def test_the_server_sends_actionable_rows_first_and_counts_every_cut(self):
        door = "helm dispatch release x; helm dispatch hold x <reason>"
        rehold = _loop("h1", "held-1", trunk_contains_tip=True,
                       source_clean_tip="c" * 40, reviewer="kimi",
                       source_clean_rehold={"kind": "NO HOLDER", "door": door,
                                            "why": door})
        rehold["source_clean_on_main"] = landreq.source_clean_on_main(rehold)
        cards = [_loop("o1", "opened", state="OPEN"),
                 _loop("y1", "ready", state="READY"),
                 _loop("v1", "reviewed-on-main", state="REVIEWED",
                       polarity="approve", trunk_contains_tip=True),
                 _loop("a1", "awaiting"),
                 rehold,
                 _loop("f1", "sent-back", state="CHANGES_REQUESTED",
                       polarity="fix", trunk_contains_tip=False)]
        body = {"withheld": {"scope": "proj"}, "read_age_s": 5,
                "unavailable": None, "loops": cards,
                "building": {"rows": [], "total": 0, "unavailable": None},
                "recent_lands": {"rows": [], "total": 0,
                                 "unavailable": None}}
        with mock.patch.object(web_board, "KANBAN_ROWS", 3):
            _sec_, rec = web_board._lands_join(lambda _qs: (body, 200))
        lanes = rec["proj"]["lanes"]
        # actionable and NOT on main first, each group in the pipeline's order
        self.assertEqual([c["lane"] for c in lanes["loops"]],
                         ["awaiting", "sent-back", "opened"])
        self.assertEqual(lanes["loops_more"], {"READY": 1, "REVIEWED": 1})
        self.assertEqual((lanes["rehold"]["count"], lanes["rehold"]["lanes"]),
                         (1, ["held-1"]))
        self.assertEqual(lanes["rehold"]["command"], "helm lr list")
        self.assertIn("RE-HOLD owed by @kimi",
                      lanes["rehold"]["rows"][0]["owed"])
        # NOTHING IS CUT SILENTLY: cards + cuts + the line = every live card
        self.assertEqual(len(lanes["loops"]) + sum(lanes["loops_more"].values())
                         + lanes["rehold"]["count"], lanes["in_flight"])
        self.assertEqual(lanes["in_flight"], len(cards))

    def test_the_landed_and_building_lists_say_what_their_caps_cut(self):
        rows = [{"lane": "l%d" % i, "task": None, "age_s": 60 * i}
                for i in range(6)]
        ahead = [{"lane": "a%d" % i, "ahead": 1} for i in range(10)]
        body = {"withheld": {"scope": "proj"}, "read_age_s": 5,
                "unavailable": None, "loops": [],
                "building": {"rows": ahead, "total": 12, "unavailable": None},
                "recent_lands": {"rows": rows, "total": 40,
                                 "unavailable": None}}
        _sec_, rec = web_board._lands_join(lambda _qs: (body, 200))
        proj = rec["proj"]
        self.assertEqual(proj["landed_more"], 34)
        self.assertEqual(proj["lanes"]["building_more"],
                         12 - web_board.TOP_LANES)
        self.assertEqual([r["lane"] for r in proj["landed"]],
                         ["l%d" % i for i in range(6)])
        # UNDER THE CAP, the control: no more counted
        body["recent_lands"]["total"] = 6
        body["building"]["total"] = 8
        _sec_, rec = web_board._lands_join(lambda _qs: (body, 200))
        self.assertEqual((rec["proj"]["landed_more"],
                          rec["proj"]["lanes"]["building_more"]), (0, 0))


class GateOnTheBoardTest(LandedWorld):
    """THE GATE THE BOARD DRAWS IS THE RUN `helm gate window show` READS, AND
    A LEASE ITS TRAIN CARRIES IS ON IT — in one real tree: a compose room
    that merged `trained` the way `helm train` does, a record in the window
    store naming it, and four leases (in the train, open, landed, claimed at
    the trunk). The node is the only seam, and it is the door's own."""

    def setUp(self):
        super().setUp()
        self.lane("trained", commits=1)
        self.lane("open", commits=1)
        self.lane("merged", commits=1)
        self.land("merged")
        self.lane("fresh")
        self.trunk = self.git(self.root, "rev-parse", "origin/main")
        self.room = os.path.join(self.tmp, "rooms", "train901")
        self.git(self.root, "worktree", "add", "-q", "--detach", self.room,
                 self.trunk)
        self.git(self.room, "merge", "--no-ff", "-q", "-m",
                 "train901: merge lane trained", "lane/trained")
        self.head = self.git(self.room, "rev-parse", "HEAD")
        self.tip = self.git(self.root, "rev-parse", "lane/trained")
        self.now = float(int(time.time()))
        self.store = os.path.join(self.tmp, "gate-window", "runs.json")
        gatewindow.write_runs(self.store, [{
            "project": gatewindow.project_id(self.room), "room": self.room,
            "head": self.head, "trunk": self.trunk, "label": "train901",
            "pid": None, "ts": self.now - 2760, "token": "t1",
            "host": "node-a", "run_id": "r1"}])
        self.projects = {"proj": {"path": self.root}}

    def gate(self, node=("r1",), path=None):
        live = None if node is None else {rid: "RUNNING" for rid in node}
        return web_board._gate_join(self.projects, path=path or self.store,
                                    inflight=lambda host: live,
                                    now=lambda: self.now)

    def test_a_running_gate_is_its_label_node_age_head_and_train(self):  # noqa: VACUOUS_ASSERTION — the retired run's empty list is the same project key whose one card the first read asserts by exact fields, lanes and liveness
        sec, out = self.gate()
        self.assertIsNone(sec["unavailable"])
        (card,) = out["proj"]
        self.assertEqual((card["label"], card["train"], card["host"],
                          card["age_s"], card["head"], card["running"]),
                         ("train901", "train901", "node-a", 2760, self.head,
                          "running"))
        self.assertEqual(card["lanes"], [{"lane": "trained",
                                          "tip": self.tip[:12]}])
        # a node that could not be read keeps the run, and says UNKNOWN
        _sec, out = self.gate(node=None)
        self.assertEqual(out["proj"][0]["running"], "unknown")
        self.assertIn("node-a", out["proj"][0]["running_why"])
        # a node that says the run is over: no gate, and the list says so
        sec, out = self.gate(node=())
        self.assertEqual(out["proj"], [])
        self.assertIsNone(sec["unavailable"])

    def test_no_store_is_no_gate_and_an_unreadable_one_is_UNKNOWN(self):
        sec, out = self.gate(path=os.path.join(self.tmp, "none", "runs.json"))
        self.assertIsNone(sec["unavailable"])
        self.assertEqual(out, {"proj": []})
        with open(self.store, "w") as fh:
            fh.write("{not json")
        sec, out = self.gate()
        self.assertIn("could not be read", sec["unavailable"])
        self.assertIn(self.store, sec["unavailable"])
        self.assertEqual(out, {}, "an unreadable window was drawn as no gate")

    def rep(self):
        from helm import seats
        return {"seats": [], "claims": seats.claims_list(gc=False),
                "roster_failed": False}

    def test_one_ancestry_question_per_leased_lane_per_board_build(self):  # noqa: VACUOUS_ASSERTION — the None on_gate lanes sit beside `trained` asserted on the gate by exact head and label on the same running rows, and the asked list is asserted equal to two named tips
        _sec, out = self.gate()
        gates = web_board._gate_heads(out)
        self.assertEqual(gates, {"proj": {"head": self.head,
                                          "label": "train901"}})
        asked, real = [], vcs.GitVcs.ancestry

        def ancestry(this, root, tip, ref):
            if ref == self.head:
                asked.append(tip)
            return real(this, root, tip, ref)
        memo = {}
        with mock.patch.object(vcs.GitVcs, "ancestry", ancestry):
            _s, first, _f = web_board._seats_join(
                self.projects, {"families": {}}, time.time(), self.rep(),
                gates=gates, memo=memo)
            once = list(asked)
            web_board._seats_join(self.projects, {"families": {}},
                                  time.time(), self.rep(), gates=gates,
                                  memo=memo)
        running = {r["lane"]: r for r in first["proj"]["running"]}
        self.assertEqual(running["trained"]["on_gate"],
                         {"head": self.head[:12], "label": "train901"})
        # the controls: work not in the train, work already on the trunk and
        # a lease at the trunk (an ancestor of every head) are not on it
        for lane in ("open", "merged", "fresh"):
            self.assertIsNone(running[lane]["on_gate"], lane)
        # the two lanes with work off the trunk were asked, once each, and
        # the same build's cache answered the second read
        self.assertEqual(sorted(once), sorted(
            [self.tip, self.git(self.root, "rev-parse", "lane/open")]))
        self.assertEqual(asked, once)
        # THE WORK PAGE over the same rows: the landed room is a leftover and
        # the rest are Building. THE GAP, named: the retired kanban drew
        # `trained` on the running gate; `/api/work` carries no room's
        # `on_gate` and no running gate, so the page cannot place it there.
        self.assertEqual(_work_placed(first["proj"]["running"]),
                         {"merged": ["record"], "trained": ["building"],
                          "open": ["building"], "fresh": ["building"]})

    def test_the_board_read_carries_the_gate_and_the_lane_on_it(self):  # noqa: ORPHANED_MOCK — node_inflight is reached through /api/board's gate leg: web_board._gate_join calls gatewindow.live_runs, whose default node reader it is; the card asserted by label proves it answered
        """End to end through `/api/board`: the registry, the roster's real
        claims and the window store the door writes, in this helm home."""
        pk.write_json(home.registry_path(), {"version": 1, "projects": {
            "proj": {"name": "proj", "path": self.root}}})
        gatewindow.write_runs(gatewindow.runs_path(),
                              gatewindow.read_runs(self.store))
        rep = self.rep()
        body = {"withheld": {"scope": "proj"}, "read_age_s": 1,
                "unavailable": None, "loops": [],
                "building": {"rows": [], "total": 0, "unavailable": None},
                "recent_lands": {"rows": [], "total": 0, "unavailable": None}}
        web_board._forget()
        web._qstate.pop("flags", None)
        self.addCleanup(web_board._forget)
        self.addCleanup(web_board._fleet_reset)
        self.addCleanup(web._qstate.pop, "flags", None)
        with mock.patch.dict(web_board._LEG_BUDGET_S,
                             dict.fromkeys(web_board._LEGS, 60)), \
                mock.patch.object(web, "_roster_cached", lambda room: rep), \
                mock.patch.object(web, "_api_lr", lambda qs: (
                    {"warming": True} if qs.get("all_projects") else body,
                    200)), \
                mock.patch.object(gatewindow, "node_inflight",
                                  lambda host, runner=None: {"r1": "RUNNING"}), \
                mock.patch.object(repofacts, "_gh_visibility",
                                  lambda slug: (None, "stubbed")):
            got = web._api_board()
            self.assertTrue(web_board._fleet_wait(10))
        self.assertIsNone(got["sections"]["gate"]["unavailable"])
        self.assertEqual(got["sections"]["gate"]["source"],
                         "helm gate window show")
        proj = got["projects"]["proj"]
        self.assertEqual([g["label"] for g in proj["gate"]], ["train901"])
        on_gate = {r["lane"]: r.get("on_gate") for r in proj["running"]}
        self.assertEqual(on_gate["trained"]["head"], self.head[:12])
        self.assertIsNone(on_gate["open"])
