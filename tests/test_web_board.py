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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import (burnflags, home, pk, registry, repofacts,  # noqa: E402
                  scheduler, tasks, web, web_board, web_cache, web_ui_loader)
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


def _lr_body(read_age_s=4):
    """The land pipeline's body for a board scoped to alpha. Two loops, one of
    them HONORED (closed through succession, so not in flight)."""
    return {
        "withheld": {"scope": "alpha", "scope_repo": "/x/.git",
                     "foreign": 0, "unresolved": 0, "by_project": {}},
        "read_age_s": read_age_s, "projected_age_s": read_age_s,
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
            web_board, "_LEG_BUDGET_S", dict.fromkeys(
                ("flags", "tasks", "seats", "lands", "trunk"), 60),
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
        argv is what the brief names, and an answer gh does not recognise is
        unknown rather than a guess."""
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
                             ["repo", "view", "akapug/alpha", "--json",
                              "visibility"])

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
        # and beside it the one predicate's answer, False over a None
        self.assertEqual(alpha["lanes"]["loops"],
                         [{"id": "r1", "lane": "lane-a",
                           "state": "AWAITING_REVIEW",
                           "trunk_contains_tip": None,
                           "on_main_unverdicted": False},
                          {"id": "r3", "lane": "lane-g", "state": "READY",
                           "trunk_contains_tip": None,
                           "on_main_unverdicted": False}])
        self.assertEqual(alpha["lanes"]["building_lanes"], ["lane-c"])
        self.assertEqual(alpha["landed"],
                         [{"lane": "lane-z", "task": "task/9", "age_s": 3600}])

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
                          "collapsed": []})
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

    # -- the clocks --------------------------------------------------------

    def test_every_section_carries_its_own_clock(self):
        sections = self.board()["sections"]
        self.assertEqual(sorted(sections),
                         ["flags", "fleet", "lands", "lights", "seats",
                          "tasks", "trunk"])
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
        self.assertEqual(stale["lands"]["limit_s"], web_board.SECTION_LIMIT_S)
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

    def test_the_endpoint_is_registered_and_served(self):
        self.assertIs(web.API["/api/board"], web._api_board)
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
        "fleet": _sec(unplaced=0),
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
           "boardCapacity", "boardChip", "boardChips", "boardCount",
           "boardLanes", "boardLaneWord", "boardProgress", "boardRepoBadge",
           "boardRepos",
           "boardKanban", "boardKanbanHTML", "boardKanbanCount",
           "boardFoldLine", "fleetKanbanHTML", "boardWide", "boardWaits",
           "boardDetail", "boardRowHTML", "onYouRead")
    CONSTS = ("LIGHTS", "FLAGCOL", "LIGHT_RANK", "KANBAN_OF")
    DECLS = ("DETAIL_HAVE", "DETAIL_ROUTE", "BOARD_DIRTY")

    SUPPORT = r"""
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const POSTED = [];
let RELOADS = 0;
async function post(url, body) { POSTED.push({url, body}); return {ok: true}; }
async function boardReload() { RELOADS++; }
const _rowjs = name => ({name, path: "/fake/dev/" + name, status: "active",
  last_seen: 1900000000, sessions: {}, light: {colour: "active", authored: false}});
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
out.flags_reading = flagsHTML(BOARD_READING.sections.flags);
out.sorted = boardSort(ROWS).map(p => p.name);
out.chips = boardChips(BOARD.projects.alpha, BOARD);
out.chips_stale = boardChips(BOARD_FLAGS_STALE.projects.alpha, BOARD_FLAGS_STALE);
out.chips_none = boardChips(BOARD.projects.beta, BOARD);
out.collapsed = boardRowHTML(ALPHA, BOARD, false);
out.expanded = boardRowHTML(ALPHA, BOARD, true);
out.expanded_beta = boardRowHTML(BETA, BOARD, true);
out.gamma = boardRowHTML(GAMMA, BOARD, false);
out.gamma_notasks = boardRowHTML(GAMMA, BOARD_TASKS_DOWN, false);
out.seats_stale = boardRowHTML(ALPHA, BOARD_SEATS_STALE, false);
out.tasks_stale = boardRowHTML(ALPHA, BOARD_TASKS_STALE, false);
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
out.kb_alpha = boardKanban(ALPHA, BOARD.projects.alpha, BOARD);
out.kb_beta = boardKanban(BETA, BOARD.projects.beta, BOARD);
out.kb_beta_loading = boardKanban(BETA, BOARD_FLEET_LOADING.projects.beta, BOARD_FLEET_LOADING);
out.kb_beta_down = boardKanban(BETA, BOARD_FLEET_DOWN.projects.beta, BOARD_FLEET_DOWN);
out.kb_html_loading = boardKanbanHTML(out.kb_beta_loading);
out.kb_html_down = boardKanbanHTML(out.kb_beta_down);
const PARTIAL = JSON.parse(JSON.stringify(BOARD));
PARTIAL.sections.fleet.landed_partial = {shown: 6, total: 700};
PARTIAL.projects.beta.pipeline.landed = [];
out.kb_partial = boardKanban(BETA, PARTIAL.projects.beta, PARTIAL);
out.kb_html_partial = boardKanbanHTML(out.kb_partial);
out.lanes_seats_only = boardLanes({running: [{lane: null, kind: "seat", seats: ["g-1"]},
  {lane: null, kind: "seat", seats: ["g-2"]}]}, BOARD);
const FLEET_ROWS = [ALPHA, BETA, GAMMA];
out.fk_ok = fleetKanbanHTML(FLEET_ROWS, BOARD);
out.fk_loading = fleetKanbanHTML(FLEET_ROWS, BOARD_FLEET_LOADING);
out.fk_down = fleetKanbanHTML(FLEET_ROWS, BOARD_FLEET_DOWN);
out.fk_pending = fleetKanbanHTML(FLEET_ROWS, null);
out.kb_stale = boardKanban(ALPHA, BOARD_LANDS_STALE.projects.alpha, BOARD_LANDS_STALE);
out.kb_html = boardKanbanHTML(out.kb_alpha);
out.kb_html_beta = boardKanbanHTML(out.kb_beta);
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
            "BOARD_LANDS_STALE": _board(lands={"stale": True,
                                               "age_s": 99999}),
            "BOARD_TASKS_STALE": _board(tasks={"stale": True,
                                               "age_s": 99999}),
            "BOARD_TASKS_DOWN": _board(tasks={
                "unavailable": "ledger unreadable"}),
            "BOARD_NOREGISTRY": _board(running=None, possible=5),
            "BOARD_FLEET_LOADING": _board(fleet={
                "loading": True, "measured_at": None, "age_s": None}),
            "BOARD_FLEET_DOWN": _board(fleet={
                "unavailable": "the all-projects read raised (OSError)"}),
            "BOARD_TRUNK_STALE": _board(trunk={"stale": True,
                                               "age_s": 99999}),
            # THE FIRST READ AFTER A RESTART: every leg past its budget
            "BOARD_READING": _board(running=None, possible=None, **{
                name: dict(_READING, **extra) for name, extra in (
                    ("seats", {}), ("tasks", {}), ("trunk", {}),
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

    def test_the_capacity_line_says_where_the_lanes_are(self):
        # alpha runs three lanes, beta one; busiest first
        self.assertIn("alpha 3 · beta 1", self.out["c_ok"])
        self.assertNotIn("alpha 3", self.out["c_noroster"])  # unread: no shares

    def test_the_active_count_rides_beside_green(self):
        """The overview's "N active" (the scan's own word) was lost with the
        card grid; it is restored on the lanes line, and only when counted."""
        self.assertIn("1 green · 12 active", self.out["c_ok"])
        self.assertNotIn("active", self.out["c_noactive"].split("green")[1][:40])

    def test_an_off_board_share_makes_the_shares_add_up(self):
        self.assertIn("alpha 3 · beta 1 · other 1", self.out["c_other"])
        # no off-board claim, no "other" share (read the text, not the markup)
        self.assertNotIn("other", re.sub(r"<[^>]*>", "", self.out["c_ok"]))

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
        opened = text(self.out["open_reading"])
        for part in ("seats still being read", "tasks still being read",
                     "lanes still being read",
                     "land pipeline still being read",
                     "last land still being read"):
            self.assertIn(part, opened, part)
        self.assertNotIn("UNKNOWN", opened)
        self.assertNotIn("nobody is seated", opened)     # never an answer
        # the scoped pipeline has not named its project yet; the fleet read
        # has, so this row's kanban is still being read, not "not read here"
        self.assertIn("still being read", opened.split("open tasks")[0])
        self.assertNotIn("not read here", opened.split("open tasks")[0])
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

    def test_the_capacity_line_carries_one_chip_per_family_in_its_colour(self):
        c = self.out["c_ok"]
        self.assertEqual(c.count('class="fchip'), 2)
        self.assertIn("anthropic", c)
        self.assertIn("#c9772e", c)                     # ORANGE
        self.assertIn("#b58a2e", c)                     # YELLOW, codex

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

    def test_stale_or_unmeasured_flags_draw_uncoloured_family_chips(self):
        for key in ("c_flags_stale", "c_unmeasured"):
            self.assertNotIn("#c9772e", self.out[key], key)
        self.assertIn("fstale", self.out["c_flags_stale"])
        self.assertIn("not measured", self.out["c_unmeasured"])

    # -- the rows ----------------------------------------------------------

    def test_rows_sort_green_yellow_orange_red_then_unset(self):
        self.assertEqual(self.out["sorted"],
                         ["g-new", "g-old", "yel", "org", "red1", "zeta",
                          "quiet"])

    def test_each_chip_carries_its_family_colour_and_its_reason(self):
        chips = self.out["chips"]
        self.assertIn("anthropic", chips)
        self.assertIn("one account left", chips)       # the hover reason
        self.assertIn("#c9772e", chips)                # ORANGE, from FLAGCOL
        self.assertEqual(chips.count('class="fchip'), 2)

    def test_a_family_with_no_flag_says_no_flag(self):
        chips = self.out["chips"]
        at = chips.index("localllm")
        self.assertIn("no flag", chips[at - 400:at + 400])
        self.assertIn("fnone", chips)

    def test_a_project_with_no_seats_draws_no_chips(self):
        self.assertIn('class="bchips"', self.out["chips_none"])   # drawn at all
        self.assertNotIn("fchip", self.out["chips_none"])

    def test_a_collapsed_row_carries_light_name_chips_and_one_count(self):
        row = self.out["collapsed"]
        self.assertIn('class="dot green authored"', row)
        self.assertIn(">alpha<", row)
        self.assertIn("fchip", row)
        self.assertEqual(row.count('class="bcount'), 1)
        self.assertIn("3 open", row)
        self.assertNotIn("bdetail", row, "a collapsed row drew its detail")
        # everything beyond those four is desktop-only, by class
        wide = row[row.index('class="bwide'):]
        for word in ("3 lanes", "12 lands", "landed", "active", "private"):
            self.assertIn(word, wide, word)

    def test_the_phone_rule_hides_the_desktop_cells(self):
        css = self.src
        m = re.search(r"@media \(max-width:680px\)\{([^@]*\.brow \.bwide"
                      r"\{display:none\}[^@]*)\}", css)
        self.assertIsNotNone(m, "no phone rule hides the desktop-only cells")

    def test_an_expanded_row_carries_kanban_lanes_progress_repos_and_more(self):
        row = self.out["expanded"]
        self.assertIn("bdetail", row)
        self.assertIn("lightset", row)
        self.assertIn("wire the board", row)            # its tasks
        self.assertIn("alpha-claude", row)              # its seats
        self.assertIn("integrator", row)                # its waits
        self.assertIn("one account left", row)          # the burn reason
        self.assertIn("no burn flag", row)              # the family without one
        self.assertIn('class="bkb"', row)               # its kanban
        self.assertIn("lane-b (seat-a)", row)           # a lane and its seat
        self.assertIn("local-1 at work, no lane claimed", row)
        self.assertIn("4 opened", row)                  # its progress
        self.assertIn("emberian/alpha", row)            # both halves of a fork
        self.assertIn("akapug/alpha", row)

    def test_a_project_the_pipeline_does_not_project_says_so(self):
        row = self.out["expanded_beta"]
        self.assertIn("lane-q", row)                    # its running lane
        self.assertIn("not read here", row)
        self.assertNotIn("integrator", row)

    def test_a_project_the_joins_never_reached_reads_zero_only_when_read(self):
        self.assertIn("0 open", self.out["gamma"])
        self.assertNotIn("0 open", self.out["gamma_notasks"])
        self.assertIn("?", self.out["gamma_notasks"])

    def test_a_stale_tasks_section_does_not_draw_its_count(self):
        row = self.out["tasks_stale"]
        count = row[row.index('class="bcount'):]
        self.assertNotIn("3 open", count[:200])
        self.assertIn("STALE", count[:300])

    def test_stale_flags_draw_no_colour_on_the_chips(self):
        chips = self.out["chips_stale"]
        self.assertIn("fstale", chips)
        self.assertNotIn("#c9772e", chips)

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

    def test_progress_is_lands_then_the_backlogs_net_arrow(self):
        cell, line = self.out["p_alpha"]["cell"], self.out["p_alpha"]["line"]
        self.assertIn("12 lands", cell)
        self.assertIn("↓5", cell)                       # 9 closed, 4 opened
        self.assertIn("4 opened", line)
        self.assertIn("9 closed", line)
        self.assertIn("↑3", self.out["p_grow"]["cell"])  # 5 opened, 2 closed
        self.assertIn("→0", self.out["p_even"]["cell"])
        self.assertIn("0 lands", self.out["p_even"]["cell"])

    def test_progress_never_draws_a_count_it_did_not_read(self):
        self.assertIn("lands ?", self.out["p_beta"]["cell"])
        self.assertNotIn("0 lands", self.out["p_beta"]["cell"])
        stale = self.out["p_tasks_stale"]["cell"]
        self.assertIn("12 lands", stale)                 # the lands still read
        self.assertNotIn("↓", stale)
        self.assertIn("STALE", self.out["p_trunk_stale"]["cell"])
        self.assertNotIn("12 lands", self.out["p_trunk_stale"]["cell"])

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

    # -- the kanban ---------------------------------------------------------

    def test_the_kanban_splits_the_pipeline_into_four_columns(self):
        kb = self.out["kb_alpha"]
        lanes = {col: [c["lane"] for c in kb[col]] for col in kb}
        # lane-a holds a claim AND a review: it is drawn where it got to; a
        # seat at work with no claim is its own card, marked as a seat
        self.assertEqual(lanes["building"],
                         ["lane-b", "@local-1", "lane-c", "lane-f"])
        self.assertEqual(lanes["review"], ["lane-a", "lane-o"])
        self.assertEqual(lanes["gate"], ["lane-g", "lane-m"])
        self.assertEqual(lanes["landed"], ["lane-z"])

    def test_another_projects_columns_come_from_the_all_projects_read(self):
        kb = self.out["kb_beta"]
        lanes = {col: [c["lane"] for c in kb[col]] for col in kb}
        self.assertEqual(lanes, {"building": ["lane-q", "b-fix"],
                                 "review": ["b-review"], "gate": [],
                                 "landed": ["b-landed"]})
        self.assertNotIn("not read here", self.out["kb_html_beta"])

    def test_while_that_read_warms_the_columns_say_loading(self):
        kb = self.out["kb_beta_loading"]
        self.assertEqual([c["lane"] for c in kb["building"]], ["lane-q"])
        for col in ("review", "gate", "landed"):
            self.assertEqual(kb[col], "loading", col)
        self.assertEqual(self.out["kb_html_loading"].count("still being read"), 3)

    def test_a_failed_read_draws_unknown_columns_never_empty_ones(self):
        kb = self.out["kb_beta_down"]
        for col in ("review", "gate", "landed"):
            self.assertEqual(kb[col], "unknown", col)
        html = self.out["kb_html_down"]
        self.assertEqual(html.count(">UNKNOWN<"), 3)
        self.assertNotIn(">none<", html)
        self.assertIn(">none<", self.out["kb_html_beta"])  # the control: gate 0

    def test_a_capped_lands_list_never_reads_as_none_landed(self):
        self.assertEqual(self.out["kb_partial"]["landed"], "partial")
        self.assertIn("none among the newest lands read",
                      self.out["kb_html_partial"])
        # the control: the same project with its land in the list draws it
        self.assertEqual([c["lane"] for c in self.out["kb_beta"]["landed"]],
                         ["b-landed"])

    def test_a_stale_pipeline_draws_no_pipeline_column(self):
        kb = self.out["kb_stale"]
        self.assertEqual([c["lane"] for c in kb["building"]],
                         ["lane-a", "lane-b", "@local-1"])  # the roster read
        for col in ("review", "gate", "landed"):
            self.assertEqual(kb[col], "stale", col)

    # -- the fleet kanban, grouped by project -----------------------------------

    def test_the_fleet_kanban_is_grouped_by_project_with_nothing_withheld(self):
        html = self.out["fk_ok"]
        self.assertLess(html.index(">alpha<"), html.index(">beta<"))
        self.assertEqual(html.count('class="bkb"'), 2)   # gamma has nothing
        self.assertNotIn(">gamma<", html)
        self.assertIn("b-review", html)
        for word in ("withheld", "--all-projects"):
            self.assertNotIn(word, html)

    def test_the_fleet_kanban_says_loading_then_unknown_never_empty(self):
        self.assertIn("still being read", self.out["fk_loading"])
        self.assertIn(">beta<", self.out["fk_loading"])   # its lanes still read
        self.assertIn("UNKNOWN", self.out["fk_down"])
        self.assertIn("OSError", self.out["fk_down"])
        self.assertIn("not read yet", self.out["fk_pending"])

    def test_the_kanban_draws_four_named_columns_with_counts(self):
        html = self.out["kb_html"]
        for word in ("building", "review", "gate", "landed"):
            self.assertIn(">" + word, html, word)
        self.assertEqual(html.count('class="bkcol'), 4)
        self.assertIn("task/9", html)

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

    # -- the last land cell --------------------------------------------------

    def test_the_land_cell_reads_the_trunk(self):
        land = self.out["land_ok"][self.out["land_ok"].index("bland"):]
        self.assertIn("landed 1h ago", land)
        self.assertIn("4611958e44a2", land)             # the sha, on hover

    def test_an_unreadable_trunk_reads_not_read_here(self):
        for key in ("land_none", "land_nojoin"):
            land = self.out[key][self.out[key].index("bland"):]
            self.assertIn("not read here", land, key)
            self.assertNotIn("landed", land.split("bact")[0], key)
        self.assertIn("landed", self.out["land_ok"])     # the control

    def test_a_stale_trunk_reads_stale_not_a_time(self):
        land = self.out["land_stale"][self.out["land_stale"].index("bland"):]
        self.assertIn("STALE", land.split("bact")[0])
        self.assertNotIn("landed", land.split("bact")[0])

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


class LandedOnTheKanbanTest(unittest.TestCase):
    """WHAT THE OWNER READ: "about half the listed lanes already landed ...
    why werent they listed as landed in helm?" A kanban that reads the
    paperwork draws a lane BUILDING for as long as its lease is held and a
    request under REVIEW for as long as no verdict is recorded, and a land
    does neither. These arms run the SHIPPED `boardKanban` under node over the wire
    shapes the server's producers emit (`running[].landed` from
    `work.lanes_landed`, `web_board._kanban_card`)."""

    FNS = ("pkey", "lrDur", "lrAgo", "boardSec", "boardSecState",
           "boardKanban", "boardKanbanHTML", "boardKanbanCount",
           "boardFoldLine", "boardLaneWord")

    @classmethod
    def kanban(cls, cases):
        """{name: (running, loops, landed)} -> {name: columns}, one node run.
        `landed` None is a pipeline whose lands list could not be read."""
        from tests.test_web_accounts import _extract_const
        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("node not available")
        src = web_ui_loader.read_text()
        fns = "\n\n".join(_extract_fn(src, n) for n in cls.FNS)
        board = {"sections": {"seats": _sec(), "lands": _sec(scope="proj"),
                              "fleet": _sec(scope="proj")}}
        worlds = {name: {"running": run,
                         "lanes": dict({"loops": loops, "building_lanes": []},
                                       **(extra[0] if extra else {})),
                         "landed": landed}
                  for name, (run, loops, landed, *extra) in cases.items()}
        dirty = re.search(_DECL % "BOARD_DIRTY", src, re.M).group(0)
        script = ("const esc = s => String(s);\n"
                  + _extract_const(src, "KANBAN_OF") + "\n" + dirty + "\n"
                  + fns + "\n"
                  + "const BOARD = %s;\nconst W = %s;\nconst out = {};\n"
                  % (json.dumps(board), json.dumps(worlds))
                  + "for (const k in W) { out[k] = boardKanban({name: "
                    "'proj'}, W[k], BOARD); out[k].html = "
                    "boardKanbanHTML(out[k]); }\n"
                  + "out.words = (W.parity || W[Object.keys(W)[0]]).running"
                    ".map(boardLaneWord);\n"
                  + "console.log(JSON.stringify(out));\n")
        tmp = tempfile.mkdtemp(prefix="helm-web-board-landed-")
        try:
            path = os.path.join(tmp, "run.js")
            with open(path, "w", encoding="utf-8") as f:
                f.write(script)
            proc = subprocess.run([node, path], capture_output=True,
                                  text=True, timeout=60)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        assert proc.returncode == 0, proc.stderr[:2000]
        return json.loads(proc.stdout)

    @staticmethod
    def lanes(col):
        return [r["lane"] for r in col] if isinstance(col, list) else col

    @staticmethod
    def served(cards):
        """(loops, lanes extras) as the SERVER sends them for these cards:
        `web_board._kanban_split`, the call `_lands_join` makes, so the page
        is fed exactly the live cards and count lines it would receive."""
        live, on_main, folded = web_board._kanban_split(cards, 0)
        return live, {"on_main": on_main, "collapsed": folded}

    @staticmethod
    def claim(lane, state, proof="LANDED by ancestry (the tip itself is on "
              "the trunk)", dirty=None):
        return {"lane": lane, "kind": "claim", "seats": ["s1"],
                "landed": {"state": state, "proof": proof, "tip": "ab" * 6},
                "dirty": dirty}

    def test_a_landed_lane_under_a_DIRTY_room_is_landed_AND_still_building(self):
        """THE CLI KEEPS IT, SO THE BOARD DOES. `helm work list` prints a
        landed row whose room is dirty as DIRTY (commit or --park first) and
        `helm lr foldcheck` keeps it rather than offering its release; a
        kanban that drew it landed alone would hide uncommitted work. It is
        drawn in both columns and each card says why."""
        run = [self.claim("dirty-landed", "landed", dirty=True),
               self.claim("clean-landed", "landed", dirty=False)]
        both = self.kanban({"parity": (run, [], [])})
        got = dict(both["parity"], words=both["words"])
        landed = {r["lane"]: r["note"] for r in got["landed"]}
        building = {r["lane"]: r["note"] for r in got["building"]}
        self.assertEqual(sorted(landed), ["clean-landed", "dirty-landed"])
        self.assertIn("room DIRTY (commit or park first)",
                      landed["dirty-landed"])
        self.assertEqual(sorted(building), ["dirty-landed"])
        self.assertIn("landed on main", building["dirty-landed"])
        self.assertIn("room DIRTY (commit or park first)",
                      building["dirty-landed"])
        # THE CONTROL on the same columns: a clean landed room says nothing
        # of dirt and is not building
        self.assertNotIn("DIRTY", landed["clean-landed"])
        self.assertIn("room DIRTY", got["words"][0])
        self.assertNotIn("DIRTY", got["words"][1])

    def test_a_DIRTY_landed_lane_is_one_building_card_when_landed_is_unread(self):
        got = self.kanban({"unread": ([self.claim("dirty-landed", "landed",
                                                  dirty=True)],
                                      [], None)})["unread"]
        self.assertEqual(got["landed"], "unknown")
        self.assertEqual(self.lanes(got["building"]), ["dirty-landed"])
        note = got["building"][0]["note"]
        self.assertIn("LANDED", note)
        self.assertIn("room DIRTY (commit or park first)", note)

    def test_a_lane_its_land_request_drew_landed_still_says_its_room_is_DIRTY(self):
        landed = [{"lane": "lane/dirty-landed", "task": "task/9",
                   "age_s": 60}]
        got = self.kanban({"filed": ([self.claim("dirty-landed", "landed",
                                                 dirty=True)],
                                     [], landed)})["filed"]
        self.assertEqual(self.lanes(got["landed"]), ["lane/dirty-landed"])
        self.assertIn("task/9", got["landed"][0]["note"])
        self.assertIn("room DIRTY", got["landed"][0]["note"])
        self.assertEqual(self.lanes(got["building"]), ["dirty-landed"])

    def test_a_landed_lease_and_an_on_main_request_draw_as_LANDED(self):
        on_main = web_board._kanban_card({"id": "r1", "lane": "reviewed-in-chat",
                                          "state": "AWAITING_REVIEW",
                                          "trunk_contains_tip": True})
        waiting = web_board._kanban_card({"id": "r2", "lane": "waiting",
                                          "state": "AWAITING_REVIEW",
                                          "trunk_contains_tip": False})
        run = [self.claim("landed-lane", "landed"),
               self.claim("building-lane", "unlanded"),
               self.claim("just-claimed", "unstarted"),
               self.claim("reviewed-in-chat", "gone")]
        loops, extra = self.served([on_main, waiting])
        got = self.kanban({"parity": (run, loops, [], extra)})["parity"]
        # THE ON-MAIN REQUEST IS COUNTED ON ONE LINE, not drawn as a card: it
        # is ledger debris nobody moves, and the line names the listing
        self.assertEqual(self.lanes(got["landed"]), ["landed-lane", None])
        summary = got["landed"][1]
        self.assertEqual(summary["summary"], 1)
        self.assertIn("no verdict recorded", summary["note"])
        self.assertIn("helm lr list", summary["note"])
        notes = {r["lane"]: r["note"] for r in got["landed"]}
        self.assertIn("lease still held", notes["landed-lane"])
        self.assertIn("LANDED by ancestry", notes["landed-lane"])
        # the CONTROLS on the same columns: unlanded and never-started work is
        # still building, and a request NOT on main is still under review
        self.assertEqual(self.lanes(got["building"]),
                         ["building-lane", "just-claimed"])
        self.assertEqual(self.lanes(got["review"]), ["waiting"])

    def test_an_unread_landed_column_keeps_the_card_where_it_was_and_says_so(self):
        on_main = web_board._kanban_card({"id": "r1", "lane": "on-main",
                                          "state": "AWAITING_REVIEW",
                                          "trunk_contains_tip": True})
        loops, extra = self.served([on_main])
        got = self.kanban({"unread": ([self.claim("landed-lane", "landed")],
                                      loops, None, extra)})["unread"]
        self.assertEqual(got["landed"], "unknown")
        building = {r["lane"]: r["note"] for r in got["building"]}
        self.assertIn("LANDED", building["landed-lane"])
        # the count line stays where its cards came from, and says so
        summary = [r for r in got["review"] if r.get("summary")]
        self.assertEqual([r["summary"] for r in summary], [1])
        self.assertIn("ON MAIN", summary[0]["note"])
        self.assertNotIn("on-main", self.lanes(got["building"]))

    def test_a_lane_already_landed_is_not_ALSO_building_unless_it_moved_on(self):
        landed = [{"lane": "lane/done", "task": None, "age_s": 60},
                  {"lane": "lane/next-round", "task": None, "age_s": 60},
                  {"lane": "lane/branch-gone", "task": None, "age_s": 60}]
        run = [self.claim("done", "unstarted"),
               self.claim("next-round", "unlanded"),
               self.claim("branch-gone", "gone"),
               self.claim("never-landed", "gone")]
        got = self.kanban({"relanded": (run, [], landed)})["relanded"]
        self.assertEqual(self.lanes(got["building"]),
                         ["next-round", "never-landed"])
        notes = {r["lane"]: r["note"] for r in got["building"]}
        self.assertIn("lease only — no room or branch", notes["never-landed"])
        self.assertNotIn("lease only", notes["next-round"])

    def test_a_BUILD_row_sent_against_trunk_is_building_not_landed(self):
        """fold-checkpoint-key-3048 and seat-signs-as-itself-3049 read as
        LANDED the moment they were sent. The projection now answers a build
        row's containment off its LANE, so a build with nothing authored is
        not contained, and the kanban draws AWAITING_BUILD where it is: in
        building."""
        build = web_board._kanban_card({"id": "b", "state": "AWAITING_BUILD",
                                        "lane": "seat-signs-as-itself-3049",
                                        "kind": "build",
                                        "trunk_contains_tip": False})
        review = web_board._kanban_card({"id": "r", "lane": "under-review",
                                         "state": "AWAITING_REVIEW",
                                         "trunk_contains_tip": False})
        got = self.kanban({"sent": ([], [build, review], [])})["sent"]
        self.assertEqual(self.lanes(got["building"]),
                         ["seat-signs-as-itself-3049"])
        self.assertEqual(got["building"][0]["note"], "AWAITING_BUILD")
        self.assertEqual(self.lanes(got["review"]), ["under-review"])
        self.assertEqual(got["landed"], [])

    def test_on_main_rows_are_ONE_count_line_naming_the_listing(self):
        """The server counts them (`lanes.on_main`); the page draws one line
        with the count, the oldest age and the command — and the lanes it
        counts are placed, so a claim on one of them is not drawn building."""
        on_main = {"label": "on main with no verdict recorded", "count": 7,
                   "oldest_age_s": 3 * 86400,
                   "lanes": ["m%d" % i for i in range(7)],
                   "command": "helm lr list"}
        verdicted = [{"lane": "lane/verdicted", "task": "task/9",
                      "age_s": 60}]
        got = self.kanban({"line": (
            [self.claim("m3", "unlanded"), self.claim("fresh", "unlanded"),
             # a landed lease on a lane the line already counts is not a
             # second entry beside it — one entry per lane
             self.claim("m4", "landed")],
            [], verdicted, {"on_main": on_main})})["line"]
        self.assertEqual(self.lanes(got["landed"]), ["lane/verdicted", None])
        line = got["landed"][1]
        self.assertEqual(line["summary"], 7)
        for part in ("on main with no verdict recorded", "oldest 3d",
                     "helm lr list"):
            self.assertIn(part, line["note"])
        # the review WITH a verdict that landed is still its own card
        self.assertIn("task/9", got["landed"][0]["note"])
        self.assertEqual(self.lanes(got["building"]), ["fresh"])
        # the column head counts what the line counts, not one card
        self.assertIn('landed <span class="bmut">8</span>', got["html"])

    def test_the_page_never_refolds_a_card_the_server_sent(self):  # noqa: VACUOUS_ASSERTION — the two cards are asserted PRESENT in their columns by exact lane lists and the server's line by its exact count, on the same render
        """THE SERVER DECIDES, ONCE. A page that folds a card marked
        `trunk_contains_tip` into the on-main line itself is a second copy of
        the rule, reading containment alone — and it counts a FIX-verdicted
        row whose tip is on main (which the server keeps LISTED) as "no
        verdict recorded". Every card the server sends is drawn as a card;
        the count is the server's line and nothing else."""
        fix = web_board._kanban_card({"id": "f", "lane": "fix-on-main",
                                      "state": "CHANGES_REQUESTED",
                                      "polarity": "fix",
                                      "trunk_contains_tip": True})
        gated = web_board._kanban_card({"id": "g", "lane": "owner-gated",
                                        "state": "AWAITING_REVIEW",
                                        "trunk_contains_tip": True})
        on_main = web_board._kanban_card({"id": "m", "lane": "in-history",
                                          "state": "AWAITING_REVIEW",
                                          "trunk_contains_tip": True})
        self.assertIs(fix["on_main_unverdicted"], False)
        self.assertIs(on_main["on_main_unverdicted"], True)
        # the owner-gated hold the server kept listed is sent as a card
        # although its work is on main with no verdict
        self.assertIs(gated["on_main_unverdicted"], True)
        got = self.kanban({"sent": ([], [fix, gated], [],
                                    {"on_main": {"count": 1, "lanes":
                                                 ["in-history"],
                                                 "label": "on main with no "
                                                 "verdict recorded",
                                                 "command": "helm lr list",
                                                 "oldest_age_s": None}})})
        got = got["sent"]
        self.assertEqual(self.lanes(got["building"]), ["fix-on-main"])
        self.assertEqual(self.lanes(got["review"]), ["owner-gated"])
        self.assertEqual([r.get("summary") for r in got["landed"]], [1],
                         "the page counted a card the server kept listed")

    def test_collapsed_lines_are_drawn_under_the_columns_with_their_command(self):  # noqa: VACUOUS_ASSERTION — the drawn line and its command are asserted PRESENT in the same kanban HTML first; the plain run's missing fold is the paired absence
        lines = [{"class": "off_frontier", "count": 4, "command":
                  "helm lr retire --off-frontier", "oldest_age_s": 50 * 86400,
                  "label": "left over after landing or abandonment (off the "
                  "live frontier)", "by_reason": {}}]
        got = self.kanban({"fold": ([], [], [], {"collapsed": lines})})["fold"]
        self.assertEqual(got["collapsed"], lines)
        self.assertIn("<b>4</b> left over after landing", got["html"])
        self.assertIn("oldest 50d", got["html"])
        self.assertIn("helm lr retire --off-frontier", got["html"])
        plain = self.kanban({"none": ([], [], [])})["none"]
        self.assertNotIn("bkfold", plain["html"])

    def test_the_lanes_hover_says_a_landed_lane_is_landed(self):
        got = self.kanban({"parity": ([self.claim("landed-lane", "landed"),
                                       self.claim("open", "unlanded")],
                                      [], [])})
        self.assertIn("landed on main, lease still held", got["words"][0])
        self.assertNotIn("landed", got["words"][1])


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


class BoardWaitsRuntimeTest(unittest.TestCase):
    """THE WAITS SECTION, drawn by the SHIPPED `boardWaits`: the live groups
    as before, then one line per collapsed class with its count, its oldest
    age and the command that lists it."""

    FNS = ("pkey", "lrDur", "lrAgo", "boardSec", "boardSecState",
           "boardSecWord", "boardFoldLine", "boardWaits")

    def render(self, j, scope="proj"):
        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("node not available")
        src = web_ui_loader.read_text()
        fns = "\n\n".join(_extract_fn(src, n) for n in self.FNS)
        board = {"sections": {"lands": _sec(scope=scope)}}
        script = ('const esc = s => String(s ?? "").replace(/[&<>"\']/g, '
                  'c => ({"&":"&amp;","<":"&lt;",">":"&gt;",\'"\':"&quot;",'
                  '"\'":"&#39;"}[c]));\n' + fns + "\n"
                  + "console.log(JSON.stringify(boardWaits({name: 'proj'}, "
                  "%s, %s)));\n" % (json.dumps(j), json.dumps(board)))
        tmp = tempfile.mkdtemp(prefix="helm-web-board-waits-")
        try:
            path = os.path.join(tmp, "run.js")
            with open(path, "w", encoding="utf-8") as f:
                f.write(script)
            proc = subprocess.run([node, path], capture_output=True,
                                  text=True, timeout=60)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        assert proc.returncode == 0, proc.stderr[:2000]
        return json.loads(proc.stdout)

    def test_live_groups_then_one_line_per_collapsed_class(self):
        html = self.render({
            "waits": [{"label": "lander", "count": 1, "oldest_age_s": 600,
                       "rows": [{"plain_title": "a live ready", "age_s": 600,
                                 "stage_class": "ready"}]}],
            "waits_collapsed": [
                {"class": "off_frontier", "count": 249,
                 "oldest_age_s": 60 * 86400, "command":
                 "helm lr retire --off-frontier",
                 "label": "left over after landing or abandonment (off the "
                 "live frontier)"},
                {"class": "superseded", "count": 290, "oldest_age_s": None,
                 "command": "helm lr list --all",
                 "label": "absorbed or settled by a later round (nobody owes "
                 "a move)"}]})
        self.assertIn("<b>lander</b> · 1 waiting", html)
        self.assertIn("a live ready", html)
        self.assertIn("<b>249</b> left over after landing", html)
        self.assertIn("oldest 60d", html)
        self.assertIn("<code>helm lr retire --off-frontier</code>", html)
        self.assertIn("<b>290</b> absorbed or settled", html)
        self.assertIn("<code>helm lr list --all</code>", html)
        self.assertNotIn("nothing is waiting", html)

    def test_only_collapsed_rows_says_no_live_obligation_and_keeps_the_lines(self):
        html = self.render({"waits": [], "waits_collapsed": [
            {"class": "unclassified", "count": 2, "oldest_age_s": 86400,
             "command": "helm lr retire --off-frontier",
             "label": "unclassified (the lane is gone and helm cannot place "
             "the work)"}]})
        self.assertIn("no live obligation is waiting", html)
        self.assertIn("<b>2</b> unclassified", html)
        self.assertNotIn("nothing is waiting", html)

    def test_an_older_body_without_lines_draws_as_before(self):
        self.assertIn("nothing is waiting", self.render({"waits": []}))
        self.assertIn("not read here", self.render({"waits": []},
                                                   scope="other"))


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
    reads the kanban, and both must say the same thing about the same lane.
    One real tree, three lanes — merged, open, claimed at the trunk — read
    through the CLI render and through `/api/board`'s seats join, then drawn
    by the shipped kanban."""

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
        # ... and the owner's kanban column, drawn from the SAME board rows
        kb = LandedOnTheKanbanTest.kanban(
            {"parity": (self.board(), [], [])})["parity"]
        self.assertEqual(LandedOnTheKanbanTest.lanes(kb["landed"]), ["merged"])
        self.assertEqual(LandedOnTheKanbanTest.lanes(kb["building"]),
                         ["fresh", "open"])

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
    (release / kept), the web kanban (columns, drawn by the SHIPPED
    `boardKanban`) and the web headline (`headline.lanes.landed`, drawn by
    the SHIPPED `boardCapacity`). States: landed clean, landed under a dirty
    room, reset back to the trunk after committing, an idle lane that
    fast-forwarded the trunk in, claimed at the trunk, an idle lane that
    merged the trunk in with a merge commit, landed by patch identity.

    `MATRIX` is the expected cell for every (state, surface); each arm reads
    one surface for every state, so a failure names the surface and the lane.
    """

    # lane: (verdict, CLI line, landed_leases, kanban columns)
    MATRIX = {
        "clean":     ("landed", "LANDED", "release", ("landed",)),
        "dirty":     ("landed", "LANDED DIRTY", "kept", ("building", "landed")),
        "reset":     ("unlanded", None, None, ("building",)),
        "ffidle":    ("unstarted", None, None, ("building",)),
        "fresh":     ("unstarted", None, None, ("building",)),
        "mergeidle": ("unknown", None, None, ("building",)),
        "picked":    ("landed", "LANDED", "release", ("landed",)),
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
            "lrDur", "lrAgo", "boardSec", "boardSecState", "boardChip",
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

    def test_the_kanban_draws_each_lane_where_the_matrix_says(self):  # noqa: VACUOUS_ASSERTION — every lane's column tuple is asserted by equality to a non-empty MATRIX cell, and the dirty lane's two notes are counted (== 2) before their text is read
        running, _fleet = self.board()
        kb = LandedOnTheKanbanTest.kanban(
            {"matrix": (running, [], [])})["matrix"]
        cols = {}
        for col in ("building", "review", "gate", "landed"):
            for r in kb[col]:
                cols.setdefault(r["lane"], []).append(col)
        for lane, (_v, _cli, _rel, want) in self.MATRIX.items():
            self.assertEqual(tuple(sorted(cols.get(lane, ()))), want, lane)
        notes = [r["note"] for col in ("building", "landed")
                 for r in kb[col] if r["lane"] == "dirty"]
        self.assertEqual(len(notes), 2)
        for note in notes:
            self.assertIn("room DIRTY (commit or park first)", note)
        self.assertFalse([r for col in ("building", "landed") for r in kb[col]
                          if r["lane"] != "dirty" and "DIRTY" in r["note"]])

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
