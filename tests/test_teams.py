#!/usr/bin/env python3
"""helm.teams — each project's team and its share of a short family
(task/3156).

HERMETIC. Every arm runs in its own temp HELM_HOME with a registry it wrote
itself, and every roster, register and reading is a frozen value handed in
through a seam. No arm reads this host's fleet.

NEUTRAL NAMES. tests/ is public-bound, so the projects are alpha..delta and
the seats are named for them; the shapes are the live fleet's (a seat working
from its operator home under `<repo>-wt/seats/`, and one checkout registered
under two keys), the names are not.
"""
import json
import math
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-home-", var="HELM_HOME")

from helm import burnflags as bf  # noqa: E402
from helm import home, pk, registry, teams  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "teams")
NOW = 1800000000


def _short():
    """A family SHORT ON MONEY: ORANGE, and the money axis set it. That is
    the only ORANGE a share rations (D1 of the task/3156 design read); an
    ORANGE reach or policy set is the family's own colour for everyone."""
    return {"colour": bf.ORANGE, "axis": "money"}


def _example():
    with open(os.path.join(FIXTURES, "worked-example.json"),
              encoding="utf-8") as fh:
        return json.load(fh)


class Home(unittest.TestCase):
    """A temp helm home with a registry of four projects, and an umbrella key
    whose path holds another key's checkout (the two-key shape)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="helm-teams-")
        self.addCleanup(tmp.cleanup)
        # REALPATH, because the event ledger refuses a parent reached through
        # a symlink and a runner's temp root may be one.
        self.tmp = os.path.realpath(tmp.name)
        saved = {k: os.environ.get(k) for k in ("HELM_HOME", "MELD_HOME",
                                                 "HELM_CHAT_DIR")}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm-home")
        for k in ("MELD_HOME", "HELM_CHAT_DIR"):
            os.environ.pop(k, None)

        def restore():
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(restore)
        self.assertTrue(home.helm_home().startswith(self.tmp))
        self.paths = {}
        for name in ("alpha", "beta", "gamma", "delta"):
            self.paths[name] = self.dir("dev", name)
        # THE TWO-KEY SHAPE: an umbrella directory registered as one key, and
        # the repository inside it registered as another.
        self.paths["omega-inc"] = self.dir("dev", "omega-dev")
        self.paths["omega-dev-omega"] = self.dir("dev", "omega-dev", "omega")
        projects = {name: {"name": name, "path": path, "kind": "git",
                           "status": "active", "sessions": {}}
                    for name, path in self.paths.items()}
        pk.write_json(home.registry_path(), {"version": 1,
                                             "projects": projects})
        pk.write_json(home.authored_path(), {"version": 1, "projects": {}})
        self.posts, self.dms = [], []

    def dir(self, *parts):
        p = os.path.join(self.tmp, *parts)
        os.makedirs(p, exist_ok=True)
        return p

    def post(self, body, room, dm=None):
        self.posts.append((room, body))
        if dm:
            self.dms.append(dm)

    def team(self, *members, **shares):
        return {"members": [{"seat": s, "family": f, "role": r}
                            for s, f, r in members],
                "shares": shares}

    def write(self, project, team, v, reason="because", apply=True):
        return teams.write(project, team, v, by="owner", reason=reason,
                           apply=apply, now=NOW, post=self.post)


class ValidationTest(Home):
    """One law under every door: the web and the verb refuse the same shapes
    in the same words."""

    def refuses(self, team, needle):
        row, problem, code = self.write("alpha", team, 0)
        self.assertIsNone(row)
        self.assertEqual(code, "invalid", problem)
        self.assertIn(needle, problem)
        self.assertNotIn("team", pk.read_json(home.authored_path())
                         ["projects"].get("alpha", {}))

    def test_each_malformed_shape_is_refused_by_name(self):  # noqa: VACUOUS_ASSERTION — refuses() asserts the code, the sentence and the unwritten layer for every shape; its positive control is test_CONTROL_a_well_formed_team_is_written
        self.refuses(self.team(("bad name!", "codex", "builder")),
                     "not a seat name")
        self.refuses(self.team(("alpha-codex", "not-a-family", "builder")),
                     "not a family")
        self.refuses(self.team(("alpha-codex", "codex", "boss")),
                     "role 'boss'")
        self.refuses(self.team(("alpha-codex", "codex", "builder"),
                               ("ALPHA-CODEX", "codex", "reviewer")),
                     "twice")
        self.refuses(self.team(codex=101), "0 to 100")
        self.refuses(self.team(codex=True), "0 to 100")
        self.refuses(self.team(codex=12.5), "0 to 100")
        self.refuses(self.team(nofamily=10), "not a family")

    def test_a_reason_and_a_known_project_are_required(self):
        row, problem, code = self.write("alpha", self.team(), 0, reason="  ")
        self.assertEqual((row, code), (None, "invalid"))
        self.assertIn("reason", problem)
        row, problem, code = self.write("nowhere", self.team(), 0)
        self.assertEqual((row, code), (None, "invalid"))
        self.assertIn("unknown project", problem)

    def test_CONTROL_a_well_formed_team_is_written(self):
        row, problem, code = self.write(
            "alpha", self.team(("alpha-claude", "anthropic", "lead"),
                               ("alpha-codex", "codex", "builder"),
                               codex=30), 0)
        self.assertIsNone(problem)
        self.assertIsNone(code)
        self.assertEqual(row["v"], 1)
        said = pk.read_json(home.authored_path())["projects"]["alpha"]["team"]
        self.assertEqual(said["v"], 1)
        self.assertEqual(said["by"], "owner")
        self.assertEqual(said["shares"], {"codex": 30})

    def test_a_member_that_is_not_running_is_drift_not_a_refusal(self):
        """A team that names the seat the lead is about to start is the
        ordinary case: the write lands and drift names it."""
        row, problem, _code = self.write(
            "alpha", self.team(("alpha-kimi", "kimi", "reviewer")), 0)
        self.assertIsNone(problem)
        self.assertEqual(row["v"], 1)
        got = teams.drift("alpha", row["team"],
                          teams.placements(roster={}, projects={},
                                           integrator=None, now=NOW))
        self.assertIn("wanted", [d["kind"] for d in got])


class CompareAndSetTest(Home):
    """The friction dial's model: a save made against a version that moved
    writes nothing and says stale."""

    def test_a_stale_version_is_refused_and_writes_nothing(self):
        one = self.team(("alpha-codex", "codex", "builder"), codex=30)
        row, problem, code = self.write("alpha", one, 0)
        self.assertEqual((row["v"], problem, code), (1, None, None))
        two = self.team(("alpha-codex", "codex", "builder"), codex=40)
        row, problem, code = self.write("alpha", two, 0)
        self.assertIsNone(row)
        self.assertEqual(code, "stale")
        self.assertIn("v1 now", problem)
        said = pk.read_json(home.authored_path())["projects"]["alpha"]["team"]
        self.assertEqual((said["v"], said["shares"]), (1, {"codex": 30}))
        # CONTROL: the same change against the version that stands lands
        row, problem, code = self.write("alpha", two, 1)
        self.assertEqual((row["v"], problem, code), (2, None, None))

    def test_a_dry_run_validates_and_diffs_and_writes_nothing(self):
        row, problem, _code = self.write(
            "alpha", self.team(("alpha-codex", "codex", "builder")), 0,
            apply=False)
        self.assertIsNone(problem)
        self.assertIn("+ alpha-codex (builder, codex)", row["diff"])
        self.assertNotIn("alpha", pk.read_json(home.authored_path())
                         ["projects"])
        self.assertEqual(self.posts, [])
        self.assertFalse(os.path.exists(teams.events_path()))
        # CONTROL: the same write applied lands in all three places
        self.write("alpha", self.team(("alpha-codex", "codex", "builder")), 0)
        self.assertIn("team", pk.read_json(home.authored_path())
                      ["projects"]["alpha"])
        self.assertEqual(len(self.posts), 1)
        self.assertTrue(os.path.exists(teams.events_path()))

    def test_an_applied_write_posts_TEAM_CHANGED_to_the_room_once(self):
        row, _p, _c = self.write(
            "alpha", self.team(("alpha-codex", "codex", "builder"), codex=30),
            0, reason="alpha leads codex this week")
        self.assertTrue(row["posted"])
        self.assertEqual(len(self.posts), 1)
        room, body = self.posts[0]
        self.assertEqual(room, "alpha")
        self.assertTrue(body.startswith("TEAM-CHANGED alpha v0 → v1 by owner"))
        self.assertIn("alpha leads codex this week", body)
        self.assertIn("+ alpha-codex (builder, codex)", body)
        self.assertIn("codex share unset → 30%", body)

    def test_a_team_set_by_seat_x_emits_teams_row_naming_x_and_does_not_wake_outside_project(self):
        chat_dir = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_DIR"] = chat_dir
        os.makedirs(chat_dir, exist_ok=True)
        from helm import chat, machine_senders, seats

        row, problem, _code = teams.write(
            "alpha", self.team(("alpha-codex", "codex", "builder")),
            0, by="seat-x", reason="weekly lead rotation", apply=True,
            now=NOW)
        self.assertIsNone(problem)
        self.assertTrue(row["posted"])
        posts, _total = chat.read("alpha")
        self.assertEqual(len(posts), 1)
        msg = posts[0]
        self.assertEqual(msg.get("from"), "teams")
        self.assertIn("by seat-x", msg.get("text") or "")
        self.assertTrue(machine_senders.is_machine(msg.get("from")))

        # Does not wake seats outside the project (e.g. homed in 'beta' or 'main')
        self.assertFalse(seats.deliverable(
            msg, seat="seat-outside", room="alpha", scope={"home": "beta", "mute": ()}, ambient=True))
        self.assertFalse(seats.deliverable(
            msg, seat="seat-outside", room="alpha", scope={"home": "beta", "mute": ()}, ambient=False, beacon=True))
        # And as a machine sender, even seats inside the project are not woken at tool boundaries (pulled, not pushed)
        self.assertFalse(seats.deliverable(
            msg, seat="alpha-codex", room="alpha", scope={"home": "alpha", "mute": ()}, ambient=True))

    def test_every_write_and_every_light_change_lands_in_one_history(self):
        first, _p, _c = self.write("alpha", self.team(codex=30), 0,
                                   reason="first")
        self.assertTrue(first["event"], "the history line was not written")
        self.write("alpha", self.team(codex=35), 1, reason="second")
        registry.state("alpha", "yellow", reason="normal work", by="owner",
                       apply=True)
        rows, err = teams.history("alpha")
        self.assertIsNone(err)
        self.assertEqual([r["kind"] for r in rows], ["team", "team", "light"])
        self.assertEqual([r["v"] for r in rows], [1, 2, None])
        self.assertEqual(rows[1]["diff"], ["codex share 30% → 35%"])
        self.assertEqual(rows[2]["diff"], ["light (the scan's) → yellow"])
        self.assertEqual(rows[2]["reason"], "normal work")
        # a dry-run light writes no history
        registry.state("alpha", "red", reason="frozen", by="owner")
        self.assertEqual(len(teams.history("alpha")[0]), 3)


class AuthoredVersusDerivedTest(Home):
    """`registry.light`'s law: authored when authored, derived otherwise, and
    the record says which."""

    def roster(self):
        return {"alpha-claude": {"home_room": "alpha", "last_seen": NOW - 60,
                                 "runtime": {"family": "claude"},
                                 "runtime_verified": True,
                                 "cwd": self.paths["alpha"]}}

    def world(self):
        return teams.placements(roster=self.roster(), registers={},
                                projects=registry.load()["projects"],
                                integrator=None, now=NOW)

    def test_the_proposal_stands_until_a_team_is_authored(self):
        got = teams.read("alpha", world=self.world())
        self.assertFalse(got["authored"])
        self.assertEqual(got["v"], 0)
        self.assertEqual(got["members"], [{"seat": "alpha-claude",
                                           "family": "anthropic",
                                           "role": "lead"}])
        self.write("alpha", self.team(("alpha-claude", "anthropic", "lead"),
                                      ("alpha-codex", "codex", "builder")), 0)
        got = teams.read("alpha", world=self.world())
        self.assertTrue(got["authored"])
        self.assertEqual(got["v"], 1)
        self.assertEqual(len(got["members"]), 2)

    def test_a_projection_only_team_block_does_NOT_bind(self):  # noqa: VACUOUS_ASSERTION — the plant is asserted present in the merged record first, the mutation arm asserts it DOES migrate without the list entry, and the authored control asserts it binds
        """PLANTED, THEN PROVED INERT. A `team` block inline in registry.json
        reaches the merged record looking exactly like the owner's. It must
        not migrate into the authored layer and must not be read as a team."""
        planted = {"v": 9, "by": "owner", "ts": NOW, "reason": "forged",
                   "members": [{"seat": "stranger", "family": "codex",
                                "role": "lead"}],
                   "shares": {"codex": 100}}
        reg = pk.read_json(home.registry_path())
        reg["projects"]["alpha"]["team"] = planted
        pk.write_json(home.registry_path(), reg)
        merged = registry.load()                 # an ordinary, migrating load
        self.assertEqual(merged["projects"]["alpha"].get("team"), planted,
                         "premise: the merged record carries the plant")
        self.assertNotIn("team", pk.read_json(home.authored_path())
                         ["projects"].get("alpha", {}))
        got = teams.read("alpha", world=self.world())
        self.assertFalse(got["authored"])
        self.assertNotIn("stranger", [m["seat"] for m in got["members"]])
        # a save keeps the projection copy in the projection, never promotes it
        registry.save(registry.load())
        self.assertNotIn("team", pk.read_json(home.authored_path())
                         ["projects"].get("alpha", {}))
        # THE FIELD LIST IS WHAT HOLDS IT: with `team` taken off the
        # never-migrated list, the same plant IS promoted by an ordinary load
        from unittest import mock
        with mock.patch.object(registry, "NEVER_MIGRATED_FIELDS", tuple(
                f for f in registry.NEVER_MIGRATED_FIELDS if f != "team")):
            registry.load()
        self.assertEqual(pk.read_json(home.authored_path())["projects"]
                         ["alpha"].get("team"), planted,
                         "mutation: without the list entry the plant migrates")
        pk.write_json(home.authored_path(), {"version": 1, "projects": {}})
        # CONTROL: the same block in the AUTHORED layer is the team
        auth = pk.read_json(home.authored_path())
        auth["projects"]["alpha"] = {"path": self.paths["alpha"],
                                     "team": planted}
        pk.write_json(home.authored_path(), auth)
        got = teams.read("alpha", world=self.world())
        self.assertTrue(got["authored"])
        self.assertEqual(got["v"], 9)

    def test_a_malformed_authored_team_falls_back_and_says_why(self):
        auth = pk.read_json(home.authored_path())
        auth["projects"]["alpha"] = {"path": self.paths["alpha"],
                                     "team": {"members": "nonsense"}}
        pk.write_json(home.authored_path(), auth)
        got = teams.read("alpha", world=self.world())
        self.assertFalse(got["authored"])
        self.assertIn("version", got["problem"])


class EffectiveColourTest(unittest.TestCase):
    """The rule's table, both breakpoints included."""

    def test_the_table(self):  # noqa: VACUOUS_ASSERTION — a fixed literal table of fourteen cases, each an equality on a named colour
        R, O, Y, G, X = bf.RED, bf.ORANGE, bf.YELLOW, bf.GREEN, bf.GREY
        cases = [(R, 0.1, R), (R, None, R), (R, 5.0, R),
                 (G, 9.0, G), (Y, 9.0, Y), (X, 9.0, X),
                 (O, None, O), (O, 0.0, Y), (O, 0.5, Y),
                 (O, 1.0, Y), (O, 1.0000001, O), (O, 2.0, O),
                 (O, 2.0000001, R), (O, math.inf, R)]
        for family, ratio, want in cases:
            with self.subTest(family=family, ratio=ratio):
                self.assertEqual(teams.effective_colour(family, ratio), want)


class WorkedExampleTest(unittest.TestCase):
    """THE SPEC'S OWN NUMBERS, as a fixture: 73M/h burn, 54h runway, 85h
    horizon -> 46.4M/h sustainable; shares 30/35/25/10 -> RED, ORANGE,
    YELLOW, ORANGE."""

    def setUp(self):
        self.ex = _example()
        fam = self.ex["family"]
        self.flags = {fam: {"colour": self.ex["family_colour"],
                            "axis": "money"}}
        self.pace = {fam: self.ex["pace"]}
        self.burn = {"families": {fam: {
            "per_hour": self.ex["pace"]["tokens_per_hour"],
            "seats": {p["seat"]: p["burn_per_h"]
                      for p in self.ex["projects"].values()}}}}
        self.teams = {name: {"members": [{"seat": p["seat"], "family": fam,
                                          "role": "builder"}],
                             "shares": {fam: p["share"]}}
                      for name, p in self.ex["projects"].items()}

    def test_the_sustainable_rate_is_runway_times_rate_over_horizon(self):  # noqa: VACUOUS_ASSERTION — an equality against the fixture's own 46.4
        got = teams.sustainable_per_h(self.pace["codex"])
        self.assertEqual(round(got / 1e6, 1),
                         self.ex["sustainable_per_h_rounded_m"])

    def test_the_shares_give_the_owners_four_colours(self):  # noqa: VACUOUS_ASSERTION — the fixture holds four projects and every one is asserted by equality
        got = teams.allocation(self.flags, self.pace, self.burn, self.teams)
        for name, want in self.ex["projects"].items():
            row = got[name]["codex"]
            with self.subTest(project=name):
                self.assertEqual(row["mode"], teams.RATE)
                self.assertEqual(row["colour"], want["colour"])
                self.assertEqual(row["family_colour"], bf.ORANGE)
                self.assertEqual(round(row["budget_per_h"] / 1e6, 1),
                                 want["budget_m"])
                self.assertEqual(teams.tokens(row["burn_per_h"]),
                                 "%.1fM" % want["burn_m"])
                self.assertAlmostEqual(row["ratio"], want["ratio"], delta=0.05)
                self.assertTrue(row["rationed"])
                self.assertEqual(row["say"], bf.BEHAVIOUR[want["colour"]]["say"])

    def test_the_one_line_route_prints(self):  # noqa: VACUOUS_ASSERTION — an equality against the spec's own sentence
        got = teams.allocation(self.flags, self.pace, self.burn, self.teams)
        self.assertEqual(teams.line("alpha", "codex", got["alpha"]["codex"]),
                         self.ex["line_alpha"])

    def test_CONTROL_a_yellow_family_rations_nobody(self):  # noqa: VACUOUS_ASSERTION — a dict equality over all four projects
        self.flags["codex"]["colour"] = bf.YELLOW
        got = teams.allocation(self.flags, self.pace, self.burn, self.teams)
        self.assertEqual({k: v["codex"]["colour"] for k, v in got.items()},
                         dict.fromkeys(self.teams, bf.YELLOW))
        self.assertFalse(any(v["codex"]["rationed"] for v in got.values()))

    def test_a_red_family_is_red_for_the_project_inside_its_budget(self):  # noqa: VACUOUS_ASSERTION — an equality on the colour and a bound on the ratio
        self.flags["codex"]["colour"] = bf.RED
        got = teams.allocation(self.flags, self.pace, self.burn, self.teams)
        self.assertEqual(got["gamma"]["codex"]["colour"], bf.RED)
        self.assertLess(got["gamma"]["codex"]["ratio"], 1.0)

    def test_a_red_light_releases_the_projects_share(self):
        lights = {"gamma": {"authored": True, "colour": "red"}}
        got = teams.allocation(self.flags, self.pace, self.burn, self.teams,
                               lights=lights)
        self.assertEqual(got["gamma"]["codex"]["share"], 0)
        self.assertEqual(got["gamma"]["codex"]["budget_per_h"], 0)
        self.assertEqual(got["gamma"]["codex"]["colour"], bf.RED)
        # the scan's red is an observation, not the owner's word: no release
        lights = {"gamma": {"authored": False, "colour": "red"}}
        got = teams.allocation(self.flags, self.pace, self.burn, self.teams,
                               lights=lights)
        self.assertEqual(got["gamma"]["codex"]["share"], 25)

    def test_unmeasured_burn_leaves_the_family_colour_standing(self):
        got = teams.allocation(self.flags, self.pace, None, self.teams)
        row = got["alpha"]["codex"]
        self.assertIsNone(row["burn_per_h"])
        self.assertIsNone(row["ratio"])
        self.assertEqual(row["colour"], bf.ORANGE)
        self.assertFalse(row["rationed"])
        self.assertIn("unmeasured", teams.line("alpha", "codex", row))


class NormalizationTest(unittest.TestCase):
    """Shares over 100 are written anyway, flagged over-promised, and the
    budgets scale down: budget = share / max(100, sum)."""

    def test_over_promised_shares_scale_every_budget(self):  # noqa: VACUOUS_ASSERTION — the loop is over a literal pair and the control after it is unconditional
        pace = {"codex": {"runway_h": 100, "tokens_per_hour": 10e6,
                          "horizon_h": 100}}                  # 10M/h lasts
        burn = {"families": {"codex": {"per_hour": 12e6, "seats": {
            "alpha-codex": 6e6, "beta-codex": 6e6}}}}
        mk = lambda seat, pct: {"members": [{"seat": seat, "family": "codex",
                                              "role": "builder"}],
                                 "shares": {"codex": pct}}
        got = teams.allocation({"codex": _short()}, pace, burn,
                               {"alpha": mk("alpha-codex", 60),
                                "beta": mk("beta-codex", 60)})
        for name in ("alpha", "beta"):
            row = got[name]["codex"]
            self.assertTrue(row["over_promised"])
            self.assertEqual(row["shares_total"], 120)
            self.assertAlmostEqual(row["budget_per_h"], 5e6)   # 60/120 x 10M
            self.assertAlmostEqual(row["ratio"], 1.2)
            self.assertEqual(row["colour"], bf.ORANGE)
        # CONTROL: under 100 nothing scales — 60 of 10M is 6M
        got = teams.allocation({"codex": _short()}, pace, burn,
                               {"alpha": mk("alpha-codex", 60)})
        self.assertAlmostEqual(got["alpha"]["codex"]["budget_per_h"], 6e6)
        self.assertFalse(got["alpha"]["codex"]["over_promised"])
        self.assertEqual(got["alpha"]["codex"]["colour"], bf.YELLOW)


class UnsetShareTest(Home):
    """D2 (the task/3156 design read): a family with NO share on a team is
    UNRATIONED for that project, never 0%. A missing share read as 0% put
    every team project past twice a zero budget, so RED, on any family short
    on money; and a seed with no burn snapshot authored every team without
    shares. The lead is told instead: one drift line per family the team
    spends with no share."""

    def setUp(self):
        super().setUp()
        self.pace = {"codex": {"runway_h": 100, "tokens_per_hour": 10e6,
                               "horizon_h": 100}}
        self.burn = {"families": {"codex": {"per_hour": 10e6, "seats": {
            "alpha-codex": 1e6}}}}
        self.bare = self.team(("alpha-lead", "anthropic", "lead"),
                              ("alpha-codex", "codex", "builder"))

    def test_a_family_with_no_share_reads_its_own_colour(self):
        got = teams.allocation({"codex": _short()}, self.pace, self.burn,
                               {"alpha": self.bare})["alpha"]["codex"]
        self.assertIsNone(got["share"])
        self.assertEqual((got["colour"], got["rationed"]),
                         (bf.ORANGE, False))
        self.assertIsNone(got["budget_per_h"])
        line = teams.line("alpha", "codex", got)
        self.assertIn("no share", line)
        self.assertNotIn("0%", line)
        self.assertIn("ORANGE for alpha", line)
        # CONTROL: an explicit 0% is the owner's word, and it rations
        zero = dict(self.bare, shares={"codex": 0})
        got = teams.allocation({"codex": _short()}, self.pace, self.burn,
                               {"alpha": zero})["alpha"]["codex"]
        self.assertEqual((got["share"], got["colour"], got["rationed"]),
                         (0, bf.RED, True))

    def test_an_unset_share_is_left_out_of_the_family_total(self):
        other = self.team(("beta-codex", "codex", "builder"), codex=50)
        got = teams.allocation({"codex": _short()}, self.pace, self.burn,
                               {"alpha": self.bare, "beta": other})
        self.assertEqual(got["beta"]["codex"]["shares_total"], 50)
        self.assertIsNone(got["alpha"]["codex"]["share"])

    def test_the_lead_is_told_which_family_has_no_share(self):
        world = teams.placements(roster={}, projects={}, integrator=None,
                                 now=NOW)
        got = teams.drift("alpha", self.bare, world)
        unset = [d for d in got if d["kind"] == "no-share"]
        self.assertEqual([d["family"] for d in unset], ["codex"])
        self.assertIn("unrationed", unset[0]["text"])
        self.assertIn("--share codex=", unset[0]["text"])
        # CONTROL: with a codex share set the line is gone, and the native
        # family, which spends each seat's own account, never asks for one
        got = teams.drift("alpha", dict(self.bare, shares={"codex": 30}),
                          world)
        self.assertEqual([d for d in got if d["kind"] == "no-share"], [])

    def test_the_change_says_unset_never_zero(self):
        self.assertEqual(teams.diff(self.bare, dict(self.bare,
                                                    shares={"codex": 30})),
                         ["codex share unset → 30%"])
        self.assertEqual(teams.diff(dict(self.bare, shares={"codex": 30}),
                                    self.bare),
                         ["codex share 30% → unset"])
        # CONTROL: an explicit 0% is a share, and it says so
        self.assertEqual(teams.diff(self.bare, dict(self.bare,
                                                    shares={"codex": 0})),
                         ["codex share unset → 0%"])


class SharedSeatTest(unittest.TestCase):
    """Q4: a seat may sit on several teams, and its spend is SHARED — it
    counts toward the family's total and lands on no project's bill."""

    def test_a_shared_seat_is_not_attributed(self):
        burn = {"families": {"ds4pro": {"per_hour": 10e6, "seats": {
            "ds4pro": 8e6, "alpha-ds4pro": 2e6}}}}
        teams_ = {"alpha": {"members": [
                      {"seat": "ds4pro", "family": "ds4pro", "role": "reviewer"},
                      {"seat": "alpha-ds4pro", "family": "ds4pro",
                       "role": "reviewer"}], "shares": {"ds4pro": 50}},
                  "beta": {"members": [
                      {"seat": "ds4pro", "family": "ds4pro", "role": "reviewer"}],
                      "shares": {"ds4pro": 50}}}
        got = teams.allocation({"ds4pro": _short()}, {}, burn,
                               teams_)
        a, b = got["alpha"]["ds4pro"], got["beta"]["ds4pro"]
        self.assertEqual(a["mode"], teams.RELATIVE)
        self.assertEqual(a["shared"], ["ds4pro"])
        self.assertEqual(b["shared"], ["ds4pro"])
        self.assertAlmostEqual(a["burn_per_h"], 2e6)        # its own seat only
        self.assertAlmostEqual(b["burn_per_h"], 0.0)
        # the ratio is against the FAMILY total, which the shared seat is in
        self.assertAlmostEqual(a["ratio"], (2e6 / 10e6) / 0.5)
        self.assertEqual(a["colour"], bf.YELLOW)
        # CONTROL: on one team only, the same seat's spend is that team's
        del teams_["beta"]
        got = teams.allocation({"ds4pro": _short()}, {}, burn,
                               teams_)
        self.assertAlmostEqual(got["alpha"]["ds4pro"]["burn_per_h"], 10e6)
        self.assertEqual(got["alpha"]["ds4pro"]["shared"], [])
        self.assertEqual(got["alpha"]["ds4pro"]["colour"], bf.ORANGE)

    def test_the_native_and_unmeasured_families_carry_no_percent(self):  # noqa: VACUOUS_ASSERTION — four equalities on mode and colour
        teams_ = {"alpha": {"members": [
            {"seat": "alpha-claude", "family": "anthropic", "role": "lead"},
            {"seat": "alpha-gemini", "family": "gemini", "role": "reviewer"}],
            "shares": {}}}
        got = teams.allocation({"anthropic": _short(),
                                "gemini": {"colour": bf.GREY}}, {}, None,
                               teams_)
        self.assertEqual(got["alpha"]["anthropic"]["mode"], teams.ACCOUNTS)
        self.assertEqual(got["alpha"]["anthropic"]["colour"], bf.ORANGE)
        self.assertEqual(got["alpha"]["gemini"]["mode"], teams.SLOTS)
        self.assertEqual(got["alpha"]["gemini"]["colour"], bf.GREY)


class SlotsTest(Home):
    """A LOCAL family's lanes are one shared, capacity-bound pool. Slots = share x capacity; in use is the
    owed non-build rows on the family's seats for the project; inside the
    slots YELLOW, over them ORANGE; one more row past them QUEUES, never
    refused; with no capacity recorded, "capacity not measured"."""

    def setUp(self):
        super().setUp()
        # A SEAT ON SEVERAL TEAMS IS THE NORMAL CASE for a local lane.
        lane = {"seat": "qwen27", "family": "qwen27", "role": "reviewer"}
        self.teams = {
            "alpha": {"members": [{"seat": "alpha-claude",
                                   "family": "anthropic", "role": "lead"},
                                  dict(lane)], "shares": {"qwen27": 30}},
            "beta": {"members": [dict(lane)], "shares": {"qwen27": 70}}}
        self.flags = {"qwen27": {"colour": bf.GREY}}

    def slots(self, cap=None, alpha=0, beta=0, unplaced=0):
        return {"families": ["qwen27", "qwenlocal"],
                "capacity": ({"qwen27": {"lanes": cap, "by": "seat-m",
                                         "ts": NOW, "reason": "measured"}}
                             if cap else {}),
                "capacity_problem": None, "why": None,
                "in_use": {"qwen27": {"by_project": {"alpha": alpha,
                                                     "beta": beta},
                                      "unplaced": unplaced,
                                      "total": alpha + beta + unplaced},
                           "qwenlocal": {"by_project": {}, "unplaced": 0,
                                         "total": 0}}}

    def alloc(self, slots, flags=None, burn=None):
        return teams.allocation(flags or self.flags, {}, burn, self.teams,
                                slots=slots)

    def test_capacity_not_measured_is_said_never_a_number(self):
        a = self.alloc(self.slots(alpha=2))["alpha"]["qwen27"]
        self.assertEqual(a["mode"], teams.SLOTS)
        self.assertEqual((a["capacity"], a["slots"], a["in_use"]),
                         (None, None, 2))
        self.assertFalse(a["capacity_measured"])
        self.assertFalse(a["queued"])
        self.assertEqual(a["colour"], bf.GREY)
        self.assertEqual(teams.line("alpha", "qwen27", a),
                         "slots qwen27 30%: capacity not measured, 2 in use "
                         "here → GREY for alpha")

    def test_slots_are_share_times_capacity_and_the_colour_follows_use(self):
        got = self.alloc(self.slots(cap=10, alpha=2, beta=8))
        a, b = got["alpha"]["qwen27"], got["beta"]["qwen27"]
        self.assertAlmostEqual(a["slots"], 3.0)
        self.assertAlmostEqual(b["slots"], 7.0)
        self.assertEqual((a["colour"], a["queued"]), (bf.YELLOW, False))
        self.assertEqual((b["colour"], b["queued"]), (bf.ORANGE, True))
        self.assertEqual(a["say"], bf.BEHAVIOUR[bf.YELLOW]["say"])
        self.assertEqual(teams.line("alpha", "qwen27", a),
                         "slots qwen27 30% of 10 lanes → 3 slots, 2 in use "
                         "(fleet 10 of 10) → YELLOW for alpha")
        self.assertTrue(teams.line("beta", "qwen27", b).endswith(
            "→ ORANGE for beta; QUEUED — new work waits for a lane, it is "
            "not refused"))
        # THE BOUNDARY: every slot full is still inside them (YELLOW), and the
        # NEXT row is the one that queues
        full = self.alloc(self.slots(cap=10, alpha=3))["alpha"]["qwen27"]
        self.assertEqual((full["colour"], full["queued"]), (bf.YELLOW, True))
        # a fractional slot is not a lane: 30% of 4 lanes is 1.2 slots, and a
        # second row queues
        frac = self.alloc(self.slots(cap=4, alpha=1))["alpha"]["qwen27"]
        self.assertAlmostEqual(frac["slots"], 1.2)
        self.assertEqual((frac["colour"], frac["queued"]), (bf.YELLOW, True))
        self.assertIn("→ 1.2 slots", teams.line("alpha", "qwen27", frac))

    def test_a_red_family_stays_red_and_nothing_is_said_to_queue(self):
        a = self.alloc(self.slots(cap=10, alpha=9),
                       flags={"qwen27": {"colour": bf.RED}})["alpha"]["qwen27"]
        self.assertEqual((a["colour"], a["queued"]), (bf.RED, False))

    def test_a_lane_is_billed_by_the_rows_project_not_the_shared_seat(self):  # noqa: VACUOUS_ASSERTION — three tuple equalities over two named projects' rows, each unconditional
        got = self.alloc(self.slots(cap=10, alpha=1, beta=4, unplaced=2))
        a, b = got["alpha"]["qwen27"], got["beta"]["qwen27"]
        self.assertEqual((a["shared"], b["shared"]), (["qwen27"], ["qwen27"]))
        self.assertEqual((a["in_use"], b["in_use"]), (1, 4))
        self.assertEqual((a["in_use_fleet"], b["in_use_fleet"]), (7, 7))

    def test_a_local_family_is_measured_in_lanes_even_with_tokens_counted(self):
        burn = {"families": {"qwen27": {"per_hour": 5e6,
                                        "seats": {"qwen27": 5e6}}}}
        a = self.alloc(self.slots(cap=10), burn=burn)["alpha"]["qwen27"]
        self.assertEqual(a["mode"], teams.SLOTS)
        # CONTROL: with no slots reading the same family reads its tokens
        b = teams.allocation(self.flags, {}, burn, self.teams)["alpha"]
        self.assertEqual(b["qwen27"]["mode"], teams.RELATIVE)
        self.assertNotIn("queued", b["qwen27"])
        # and a family nothing measures stays advisory, with no lane fields
        self.teams["alpha"]["members"].append(
            {"seat": "alpha-gemini", "family": "gemini", "role": "reviewer"})
        g = self.alloc(self.slots(cap=10))["alpha"]["gemini"]
        self.assertEqual(g["mode"], teams.SLOTS)
        self.assertNotIn("capacity_measured", g)
        self.assertIn("advisory seat count", teams.line("alpha", "gemini", g))

    def test_the_fold_counts_owed_non_build_rows_by_project(self):
        from unittest import mock
        from helm import dispatches
        a, b = self.paths["alpha"], self.paths["beta"]
        rows = [{"recipient": "qwen27", "kind": "review", "repo_root": a},
                {"recipient": "qwen27", "kind": "build", "repo_root": a},
                {"recipient": "qwen27", "kind": "verify",
                 "repo_root": os.path.join(b, "src")},
                {"recipient": "qwen27", "kind": "review",
                 "repo_root": os.path.join(self.tmp, "nowhere")},
                {"recipient": "alpha-codex", "kind": "review",
                 "repo_root": a}]
        fams = {"qwen27": "qwen27", "alpha-codex": "codex"}
        with mock.patch.object(dispatches, "owed", return_value=rows):
            got, problem = teams.slot_use(
                ["qwen27"], projects=registry.load()["projects"], snap={},
                families_of=fams.get)
        self.assertIsNone(problem)
        self.assertEqual(got, {"qwen27": {"by_project": {"alpha": 1,
                                                         "beta": 1},
                                          "unplaced": 1, "total": 3}})

    def test_the_ledger_is_folded_only_when_a_team_uses_a_slot_family(self):
        from unittest import mock
        from helm import dispatches
        cap = os.path.join(self.tmp, "cap.json")
        codex_only = {"alpha": {"members": [
            {"seat": "alpha-codex", "family": "codex", "role": "builder"}],
            "shares": {}}}
        with mock.patch.object(dispatches, "snapshot",
                               side_effect=AssertionError("folded")):
            got = teams.slot_reading(teams=codex_only, capacity_file=cap)
        self.assertIsNone(got["in_use"])
        self.assertEqual(got["why"], "no team uses a slot family")
        self.assertIn("qwen27", got["families"])
        with mock.patch.object(dispatches, "snapshot",
                               return_value=({}, None)) as snap, \
                mock.patch.object(dispatches, "owed", return_value=[]):
            got = teams.slot_reading(teams=self.teams, capacity_file=cap)
        self.assertEqual(snap.call_count, 1)
        self.assertEqual(got["in_use"]["qwen27"]["total"], 0)
        # an unreadable ledger is UNKNOWN, never zero
        with mock.patch.object(dispatches, "snapshot",
                               return_value=({}, "ledger locked")):
            got = teams.slot_reading(teams=self.teams, capacity_file=cap)
        self.assertIsNone(got["in_use"])
        self.assertIn("ledger locked", got["why"])

    def test_capacity_is_a_dry_run_then_recorded_then_cleared(self):  # noqa: VACUOUS_ASSERTION — the refusal loop runs over a literal six-row table, and the dry run, the recorded value, the history line, the clear and the still-empty file are asserted unconditionally around it
        cap = os.path.join(self.tmp, "state", "cap.json")
        row, problem = teams.set_capacity("qwen27", 4, by="seat-m",
                                          reason="4 lanes at 32k", now=NOW,
                                          path=cap)
        self.assertIsNone(problem)
        self.assertEqual((row["was"], row["lanes"]), (None, 4))
        self.assertFalse(os.path.exists(cap))
        teams.set_capacity("qwen27", 4, by="seat-m", reason="4 lanes at 32k",
                           apply=True, now=NOW, path=cap)
        got, problem = teams.read_capacity(cap)
        self.assertIsNone(problem)
        self.assertEqual(got, {"qwen27": {"lanes": 4, "by": "seat-m",
                                          "ts": NOW,
                                          "reason": "4 lanes at 32k"}})
        rows, _err = teams.history(None)
        self.assertEqual([(r["kind"], r["family"], r["lanes"]) for r in rows],
                         [("capacity", "qwen27", 4)])
        row, _p = teams.set_capacity("qwen27", None, by="seat-m",
                                     reason="server down", apply=True,
                                     now=NOW, path=cap)
        self.assertEqual(row["was"], 4)
        self.assertEqual(teams.read_capacity(cap)[0], {})
        for fam, lanes, reason, needle in (
                ("anthropic", 4, "x", "no lane capacity"),
                ("nosuchfam", 4, "x", "unknown family"),
                ("qwen27", 0, "x", "whole number from 1"),
                ("qwen27", teams.MAX_LANES + 1, "x", "whole number from 1"),
                ("qwen27", True, "x", "whole number from 1"),
                ("qwen27", 4, " ", "a reason is required")):
            row, problem = teams.set_capacity(fam, lanes, by="seat-m",
                                              reason=reason, apply=True,
                                              now=NOW, path=cap)
            self.assertIsNone(row, fam)
            self.assertIn(needle, problem)
        self.assertEqual(teams.read_capacity(cap)[0], {})

    def test_the_door_says_queued_and_refuses_nothing(self):
        self.write("alpha", self.team(("alpha-claude", "anthropic", "lead"),
                                      ("qwen27", "qwen27", "reviewer"),
                                      qwen27=30), 0)
        inside = os.path.join(self.paths["alpha"], "src")
        full = (self.flags, {}, None, self.slots(cap=10, alpha=3))
        note = teams.door_note(inside, family="qwen27", inputs=full)
        self.assertIn("advice only", note)
        self.assertIn("QUEUED — new work waits for a lane", note)
        # CONTROL: a free slot says nothing
        free = (self.flags, {}, None, self.slots(cap=10, alpha=1))
        self.assertIsNone(teams.door_note(inside, family="qwen27",
                                          inputs=free))

    def test_a_seed_proposes_the_slot_split_of_the_lanes_in_use(self):
        members = self.teams["alpha"]["members"]
        got = teams.measured_shares("alpha", members, None, {},
                                    slots=self.slots(alpha=1, beta=3,
                                                     unplaced=5))
        self.assertEqual(got, {"qwen27": 25})
        # nothing in use on any project proposes no share
        self.assertEqual(teams.measured_shares("alpha", members, None, {},
                                               slots=self.slots()), {})


class DeriveTest(Home):
    """The migration seed against a frozen roster."""

    def projects(self):
        return registry.load()["projects"]

    def row(self, home_room, family, cwd, seen=60):
        return {"home_room": home_room, "last_seen": NOW - seen,
                "runtime": {"family": family}, "runtime_verified": True,
                "cwd": cwd}

    def test_a_seat_working_from_its_operator_home_joins_its_project(self):
        """The operator home is `<repo>-wt/seats/<name>`, and the roster's own
        `project` is that directory's basename — a name no project has. The
        home room places it; with no home room, the lane-worktree rule of
        `project_for_cwd` does."""
        opdir = self.dir("dev", "delta-wt", "seats", "seat-c")
        roster = {"delta-codex": dict(self.row("delta", "codex", opdir),
                                      project="seat-c"),
                  "stray-codex": dict(self.row(None, "codex", opdir),
                                      project="seat-c")}
        world = teams.placements(roster=roster, registers={},
                                 projects=self.projects(), integrator=None,
                                 now=NOW)
        seats = world["seats"]
        self.assertEqual((seats["delta-codex"]["project"],
                          seats["delta-codex"]["source"]),
                         ("delta", "home room"))
        self.assertEqual((seats["stray-codex"]["project"],
                          seats["stray-codex"]["source"]),
                         ("delta", "working directory"))
        got = teams.derive("delta", world=world)
        self.assertEqual({m["seat"]: m["role"] for m in got},
                         {"delta-codex": "builder", "stray-codex": "builder"})

    def test_two_registry_keys_give_one_team(self):
        """The two-key shape: both seats are homed to the inner key, and one
        seat's spawn register names the umbrella. One team, on the inner key;
        the umbrella gets none."""
        inner = self.paths["omega-dev-omega"]
        roster = {
            "omega-dev-omega-claude": self.row("omega-dev-omega", "claude",
                                               inner),
            "omega-codex": self.row("omega-dev-omega", "codex", inner),
            # no home room at all, and a register written against the umbrella
            "omega-extra": self.row(None, "codex", inner)}
        registers = {"omega-codex": ("codex", "omega-inc"),
                     "omega-extra": ("codex", "omega-inc")}
        world = teams.placements(roster=roster, registers=registers,
                                 projects=self.projects(), integrator=None,
                                 now=NOW)
        placed = {s: p["project"] for s, p in world["seats"].items()}
        self.assertEqual(set(placed.values()), {"omega-dev-omega"}, placed)
        got = teams.read_all(world=world)
        self.assertEqual(sorted(got), ["omega-dev-omega"])
        self.assertEqual({m["seat"]: m["role"]
                          for m in got["omega-dev-omega"]["members"]},
                         {"omega-dev-omega-claude": "lead",
                          "omega-codex": "builder", "omega-extra": "builder"})
        # CONTROL: a register naming the umbrella for a seat that works
        # OUTSIDE the inner checkout still places it on the umbrella
        roster["omega-extra"]["cwd"] = self.paths["omega-inc"]
        world = teams.placements(roster=roster, registers=registers,
                                 projects=self.projects(), integrator=None,
                                 now=NOW)
        self.assertEqual(world["seats"]["omega-extra"]["project"], "omega-inc")

    def test_roles_follow_the_seat(self):
        a = self.paths["alpha"]
        roster = {"alpha-claude": self.row("alpha", "claude", a),
                  "helper-claude": self.row("alpha", "claude", a),
                  "alpha-codex": self.row("alpha", "codex", a),
                  "qwen27": self.row("alpha", "qwen27", a),
                  "gemini": self.row("alpha", "gemini", a),
                  "old-seat": self.row("alpha", "codex", a,
                                       seen=teams.DERIVE_SEEN_S + 60),
                  "main-claude": self.row("main", "claude", a)}
        world = teams.placements(roster=roster, registers={},
                                 projects=self.projects(), integrator=None,
                                 now=NOW)
        got = {m["seat"]: m["role"] for m in teams.derive("alpha", world=world)}
        self.assertEqual(got, {"alpha-claude": "lead",
                               "helper-claude": "builder",
                               "main-claude": "builder",
                               "alpha-codex": "builder",
                               # the owner's slots direction: a local seat
                               # is a REVIEWER lane, not a checker
                               "qwen27": "reviewer", "gemini": "reviewer"})
        # the integrator, placed here, leads instead of <project>-claude
        world = teams.placements(roster=roster, registers={},
                                 projects=self.projects(),
                                 integrator="helper-claude", now=NOW)
        got = {m["seat"]: m["role"] for m in teams.derive("alpha", world=world)}
        self.assertEqual((got["helper-claude"], got["alpha-claude"]),
                         ("lead", "builder"))

    def test_a_family_helm_does_not_measure_is_not_proposed(self):
        """A seat whose family the catalog knows and the burn flags do not is
        left out of the proposal, which `write` would refuse, and drift names
        it with the reason."""
        a = self.paths["alpha"]
        roster = {"alpha-claude": self.row("alpha", "claude", a),
                  "alpha-newfam": self.row("alpha", "newfam", a)}
        world = teams.placements(roster=roster, registers={},
                                 projects=self.projects(), integrator=None,
                                 now=NOW)
        self.assertEqual(world["seats"]["alpha-newfam"]["family"], "newfam")
        got = teams.derive("alpha", world=world)
        self.assertEqual([m["seat"] for m in got], ["alpha-claude"])
        row, problem, _code = self.write("alpha", {"members": got,
                                                   "shares": {}}, 0)
        self.assertIsNone(problem)
        self.assertEqual(row["v"], 1)          # the proposal is acceptable
        world["seats"]["alpha-newfam"]["presence"] = "live"
        texts = [d["text"] for d in teams.drift("alpha", {"members": got,
                                                          "shares": {}},
                                                world)]
        self.assertIn("alpha-newfam works here (home room), and its family "
                      "newfam is not one helm measures yet, so it cannot "
                      "join a team until it is", texts)

    def test_the_one_native_seat_leads_when_no_rule_names_a_lead(self):
        """A native seat named for an alias key, or numbered, matches no
        `<project>-claude`; alone on its project it leads, and two such
        seats are not guessed between."""
        b = self.paths["beta"]
        roster = {"beta-inc-claude": self.row("beta", "claude", b),
                  "beta-codex": self.row("beta", "codex", b)}
        world = teams.placements(roster=roster, registers={},
                                 projects=self.projects(), integrator=None,
                                 now=NOW)
        got = {m["seat"]: m["role"] for m in teams.derive("beta", world=world)}
        self.assertEqual(got, {"beta-inc-claude": "lead",
                               "beta-codex": "builder"})
        roster["beta-claude-2"] = self.row("beta", "claude", b)
        world = teams.placements(roster=roster, registers={},
                                 projects=self.projects(), integrator=None,
                                 now=NOW)
        got = {m["seat"]: m["role"] for m in teams.derive("beta", world=world)}
        self.assertEqual(got, {"beta-inc-claude": "builder",
                               "beta-claude-2": "builder",
                               "beta-codex": "builder"})

    def test_a_roster_key_or_room_that_is_no_token_never_reaches_a_sink(self):
        """The launder tripwire's stand-in arm (task/3156;
        tests/test_display_launder_tripwire.py). A roster key is unvalidated
        at the join seam, and a placed seat leaves this module as a proposed
        member, a drift line and the add-seat picker. A hostile key, homed
        exactly as a legitimate seat is, is never placed; a hostile home room
        reads as none; the legitimate seats beside them are unchanged."""
        a = self.paths["alpha"]
        roster = {"alpha-claude": self.row("alpha", "claude", a),
                  "alpha\x1b[2J‮pwn": self.row("alpha", "codex", a),
                  "alpha-codex": self.row("alpha\x1b[2J", "codex", a)}
        world = teams.placements(roster=roster, registers={},
                                 projects=self.projects(), integrator=None,
                                 now=NOW)
        self.assertEqual(sorted(world["seats"]), ["alpha-claude",
                                                   "alpha-codex"])
        placed = world["seats"]["alpha-codex"]
        self.assertEqual((placed["project"], placed["source"],
                          placed["home_room"]),
                         ("alpha", "working directory", None))
        self.assertEqual(world["seats"]["alpha-claude"]["home_room"], "alpha")
        self.assertEqual([m["seat"] for m in teams.derive("alpha",
                                                          world=world)],
                         ["alpha-claude", "alpha-codex"])
        for p in world["seats"].values():
            p["presence"] = "live"
        texts = "\n".join(d["text"] for d in teams.drift(
            "alpha", self.team(("alpha-codex", "codex", "lead")), world))
        # CONTROL: drift does name the seats it sees
        self.assertIn("alpha-claude works here (home room)", texts)
        self.assertNotIn("\x1b", texts)
        self.assertNotIn("‮", texts)
        self.assertNotIn("homed in", texts)


class DriftTest(Home):

    def test_each_kind_names_its_repair(self):
        a = self.paths["alpha"]
        roster = {"alpha-claude": {"home_room": "alpha", "last_seen": NOW - 30,
                                   "runtime": {"family": "claude"},
                                   "runtime_verified": True, "cwd": a},
                  "beta-codex": {"home_room": "beta", "last_seen": NOW - 30,
                                 "runtime": {"family": "codex"},
                                 "runtime_verified": True,
                                 "cwd": self.paths["beta"]},
                  "alpha-grok": {"home_room": "alpha", "last_seen": NOW - 30,
                                 "runtime": {"family": "grok"},
                                 "runtime_verified": True, "cwd": a}}
        world = teams.placements(roster=roster, registers={},
                                 projects=registry.load()["projects"],
                                 integrator=None, now=NOW)
        for seat in world["seats"].values():
            seat["presence"] = "live"        # the roster rule reads files
        team = {"members": [
            {"seat": "alpha-claude", "family": "anthropic", "role": "lead"},
            {"seat": "beta-codex", "family": "codex", "role": "builder"},
            {"seat": "alpha-kimi", "family": "kimi", "role": "reviewer"}],
            "shares": {}}
        got = teams.drift("alpha", team, world,
                          flags={"codex": {"colour": bf.RED}})
        kinds = {(d["kind"], d["seat"]) for d in got}
        self.assertIn(("wanted", "alpha-kimi"), kinds)
        self.assertIn(("homed-elsewhere", "beta-codex"), kinds)
        self.assertIn(("family-red", "beta-codex"), kinds)
        self.assertIn(("unlisted", "alpha-grok"), kinds)
        text = " ".join(d["text"] for d in got)
        self.assertIn("helm seat spawn alpha-kimi", text)
        self.assertIn("helm chat seat rehome", text)
        # the team's own shape
        self.assertEqual([d["kind"] for d in teams.drift(
            "alpha", {"members": [], "shares": {}}, world)][:1], ["no-lead"])


class DoorsAndWatchTest(Home):
    """Step 5: the doors print the project's own colour as a NOTE and refuse
    nothing (Q2), and the watchdog posts one PROJECT-BUDGET line per crossing
    to the project's own room, latched on the colour."""

    def setUp(self):
        super().setUp()
        self.write("alpha", self.team(("alpha-codex", "codex", "builder"),
                                      codex=30), 0)
        self.flags = {"codex": _short()}
        self.pace = {"codex": {"runway_h": 100, "tokens_per_hour": 10e6,
                               "horizon_h": 100}}          # 10M/h lasts
        self.assertEqual(len(self.posts), 1)       # the TEAM-CHANGED line
        self.posts = []

    def burn(self, alpha_per_h):
        return {"families": {"codex": {"per_hour": 10e6, "seats": {
            "alpha-codex": alpha_per_h}}}}

    def test_the_door_note_names_the_projects_own_colour(self):
        inputs = (self.flags, self.pace, self.burn(7e6), None)  # 7M vs 3M
        inside = os.path.join(self.paths["alpha"], "src")
        note = teams.door_note(inside, family="codex", inputs=inputs)
        self.assertIn("advice only", note)
        self.assertIn("(2.3×) → RED for alpha", note)
        self.assertIn(bf.BEHAVIOUR[bf.RED]["say"], note)
        # every rationed family when the door does not know which it spends
        self.assertEqual(teams.door_note(inside, inputs=inputs), note)
        # CONTROLS: an unrationed family, another family, another project
        yellow = ({"codex": {"colour": bf.YELLOW}}, self.pace, self.burn(7e6),
                  None)
        self.assertIsNone(teams.door_note(inside, family="codex",
                                          inputs=yellow))
        self.assertIsNone(teams.door_note(inside, family="kimi",
                                          inputs=inputs))
        self.assertIsNone(teams.door_note(self.paths["beta"], family="codex",
                                          inputs=inputs))

    def test_the_dispatch_door_carries_it_for_the_recipients_family(self):
        from unittest import mock
        from helm import dispatches
        inputs = (self.flags, self.pace, self.burn(7e6), None)
        with mock.patch.object(dispatches, "_verified_family",
                               return_value="codex"), \
                mock.patch.object(teams, "readings", return_value=inputs):
            note = dispatches._project_share_note(self.paths["alpha"],
                                                  "alpha-codex")
        self.assertIn("RED for alpha", note)
        with mock.patch.object(dispatches, "_verified_family",
                               side_effect=RuntimeError("register unread")), \
                mock.patch.object(teams, "readings", return_value=inputs):
            # an unresolvable family is every rationed family, never a raise,
            # and the note says the family read FAILED and names its class
            note = dispatches._project_share_note(self.paths["alpha"], "x")
        self.assertIn("RED for alpha", note)
        self.assertIn("FAILED", note)
        self.assertIn("RuntimeError", note)
        # and with nothing rationed the failed family read is still said
        yellow = ({"codex": {"colour": bf.YELLOW}}, self.pace, self.burn(7e6),
                  None)
        with mock.patch.object(dispatches, "_verified_family",
                               side_effect=RuntimeError("register unread")), \
                mock.patch.object(teams, "readings", return_value=yellow):
            note = dispatches._project_share_note(self.paths["alpha"], "x")
        self.assertIn("FAILED", note)
        self.assertIn("RuntimeError", note)
        # CONTROL: a healthy read with nothing rationed is silence
        with mock.patch.object(dispatches, "_verified_family",
                               return_value="codex"), \
                mock.patch.object(teams, "readings", return_value=yellow):
            self.assertIsNone(dispatches._project_share_note(
                self.paths["alpha"], "alpha-codex"))

    def test_a_door_note_whose_read_fails_says_FAILED_and_names_it(self):
        """TRUNK'S FAIL-LOUD RULE (coordinator ruling on task/3156): a door
        that could not read the project's share says FAILED with the
        exception's class. Silence at a door reads as "inside its share", the
        one answer a failed read cannot give. Advice only, so the door still
        refuses nothing."""
        from unittest import mock
        inputs = (self.flags, self.pace, self.burn(7e6), None)
        inside = os.path.join(self.paths["alpha"], "src")
        with mock.patch.object(teams, "project_row",
                               side_effect=KeyError("pace")):
            note = teams.door_note(inside, family="codex", inputs=inputs)
        self.assertIn("FAILED", note)
        self.assertIn("KeyError", note)
        self.assertIn("advice only", note)
        with mock.patch.object(registry, "load",
                               side_effect=OSError("registry unreadable")):
            note = teams.door_note(inside, inputs=inputs)
        self.assertIn("FAILED", note)
        self.assertIn("OSError", note)
        # A FAILURE PAST THE READ is the same answer, never a raise: the claim
        # door calls this with nothing around it.
        with mock.patch.object(teams, "line",
                               side_effect=ValueError("unformattable")):
            note = teams.door_note(inside, family="codex", inputs=inputs)
        self.assertIn("FAILED", note)
        self.assertIn("ValueError", note)
        # CONTROLS: a healthy read still names the colour, or says nothing
        self.assertIn("RED for alpha",
                      teams.door_note(inside, family="codex", inputs=inputs))
        yellow = ({"codex": {"colour": bf.YELLOW}}, self.pace, self.burn(7e6),
                  None)
        self.assertIsNone(teams.door_note(inside, family="codex",
                                          inputs=yellow))

    def test_a_door_note_whose_burn_reading_raises_says_FAILED(self):
        """The door's own readings, not only the ones handed to it: a burn
        flag, pace or per-seat burn read that RAISES is a FAILED note naming
        the class. Swallowed into "no flags", it read as nothing rationed."""
        from unittest import mock
        from helm import codexpace
        inside = os.path.join(self.paths["alpha"], "src")
        with mock.patch.object(bf, "cached_flags",
                               side_effect=OSError("flags unreadable")):
            note = teams.door_note(inside, family="codex")
        self.assertIn("FAILED", note)
        self.assertIn("OSError", note)
        orange = ({"codex": _short()}, 0)
        with mock.patch.object(bf, "cached_flags", return_value=orange), \
                mock.patch.object(codexpace, "cached_seat_burn",
                                  side_effect=ValueError("burn torn")):
            note = teams.door_note(inside, family="codex")
        self.assertIn("FAILED", note)
        self.assertIn("ValueError", note)
        with mock.patch.object(bf, "cached_flags", return_value=orange), \
                mock.patch.object(codexpace, "cached_fold_input",
                                  side_effect=KeyError("runway")):
            note = teams.door_note(inside, family="codex")
        self.assertIn("FAILED", note)
        self.assertIn("KeyError", note)
        # CONTROL: no snapshot yet is NOT a failed read — nothing measured
        # rations nothing, and the door stays silent
        with mock.patch.object(bf, "cached_flags", return_value=({}, None)):
            self.assertIsNone(teams.door_note(inside, family="codex"))

    def test_the_dispatch_door_says_FAILED_when_the_share_read_raises(self):
        from unittest import mock
        from helm import dispatches
        with mock.patch.object(dispatches, "_verified_family",
                               return_value="codex"), \
                mock.patch.object(teams, "door_note",
                                  side_effect=RuntimeError("teams unread")):
            note = dispatches._project_share_note(self.paths["alpha"],
                                                  "alpha-codex")
        self.assertIn("FAILED", note)
        self.assertIn("RuntimeError", note)
        self.assertIn("advice only", note)

    def test_the_claim_door_prints_the_same_note(self):
        """Source-level: the claim path asks the one door function, beside
        the light's note, and prints it as a NOTE."""
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "helm", "work", "_cli.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        at = src.index("teams.door_note(root)")
        self.assertLess(src.index("registry.admits("), at)
        self.assertIn('print("helm work: NOTE — " + share_note', src)

    def test_a_crossing_posts_once_and_is_latched_on_the_colour(self):  # noqa: VACUOUS_ASSERTION — every silent pass sits between two passes that post, asserted by count and by body
        """A colour is posted once it has HELD for the dwell (design read D4):
        the pass that first reads it records it, and a later pass that still
        reads it posts."""
        latch = os.path.join(self.tmp, "latch.json")
        day = 86400

        def run(flags, burn, at):
            return teams.budget_watch_pass(flags, self.pace, self.burn(burn),
                                           now=NOW + at, post=self.post,
                                           path=latch)
        self.assertEqual(run(self.flags, 7e6, 0), ([], None))
        sent, problem = run(self.flags, 7e6, day)
        self.assertEqual((len(sent), problem), (1, None))
        room, body = self.posts[-1]
        self.assertEqual(room, "alpha")
        self.assertTrue(body.startswith("PROJECT-BUDGET alpha codex: "
                                        "unrationed → RED"), body)
        # THE NUMBERS MOVE, THE COLOUR DOES NOT: nothing is said
        self.assertEqual(run(self.flags, 8e6, 2 * day), ([], None))
        self.assertEqual(len(self.posts), 1)
        # the colour crosses: said once, when it has held
        self.assertEqual(run(self.flags, 2e6, 3 * day), ([], None))
        sent, _problem = run(self.flags, 2e6, 4 * day)
        self.assertEqual(len(sent), 1)
        self.assertIn("RED → YELLOW", self.posts[-1][1])
        # the family stops being short: said once, and the latch clears
        yellow = {"codex": {"colour": bf.YELLOW, "axis": "money"}}
        run(yellow, 2e6, 5 * day)
        sent, _problem = run(yellow, 2e6, 6 * day)
        self.assertEqual(len(sent), 1)
        self.assertIn("no longer rationed", self.posts[-1][1])
        self.assertEqual(pk.read_json(latch)["colours"], {})

    def test_a_failed_post_is_owed_again_and_an_unread_fold_says_nothing(self):
        latch = os.path.join(self.tmp, "latch.json")
        day = 86400

        def fail(body, room, dm=None):
            raise OSError("room unwritable")
        teams.budget_watch_pass(self.flags, self.pace, self.burn(7e6),
                                now=NOW, post=self.post, path=latch)
        sent, problem = teams.budget_watch_pass(
            self.flags, self.pace, self.burn(7e6), now=NOW + day, post=fail,
            path=latch)
        self.assertEqual(sent, [])
        self.assertIn("did not post", problem)
        self.assertIn("OSError", problem)
        sent, _problem = teams.budget_watch_pass(
            self.flags, self.pace, self.burn(7e6), now=NOW + 2 * day,
            post=self.post, path=latch)
        self.assertEqual(len(sent), 1, "the undelivered crossing is owed")
        self.assertEqual(teams.budget_watch_pass({}, self.pace, None,
                                                 post=self.post, path=latch),
                         ([], None))
        self.assertEqual(len(self.posts), 1)

    def test_the_proxywatch_pass_calls_the_project_budget_watch(self):  # noqa: VACUOUS_ASSERTION — source positions, each asserted present by index()
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "helm", "proxywatch.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        at = src.index("teams.budget_watch_pass(")
        self.assertLess(src.index("_burnflags().watch_notice("), at)
        # and before THIS pass delivers (the dark pass has its own call)
        self.assertIn("failed = _deliver(rep, prior", src[at:])
        # A PASS WHOSE BUDGET WATCH RAISES SAYS FAILED WITH THE CLASS: the
        # message alone of a KeyError is a bare key (trunk's fail-loud rule)
        handler = src[at:src.index("rep[\"burn_flag_line\"]", at)]
        self.assertIn("project budgets FAILED", handler)
        self.assertIn("ex.__class__.__name__", handler)
        # A LATCH THAT CANNOT BE WRITTEN IS REPORTED (design read D4), never
        # swallowed into a repost every pass
        self.assertIn("project budgets NOT POSTED", handler)


class ProjectBudgetAttentionTest(Home):
    """D4 (the task/3156 design read): the owner's attention budget. A
    PROJECT-BUDGET line posted the moment a colour changed, to the whole
    room, and when its latch could not be written it said nothing and
    reposted on every pass. Now a colour must HOLD for the dwell before its
    line (`budget_dwell_s`), the line goes to the project's LEAD (its room,
    waking nobody, when the team has no lead), and a latch that cannot be
    written is reported and posts nothing."""

    DAY = 86400

    def setUp(self):
        super().setUp()
        self.write("alpha", self.team(("alpha-lead", "anthropic", "lead"),
                                      ("alpha-codex", "codex", "builder"),
                                      codex=30), 0)
        self.posts, self.dms = [], []
        self.flags = {"codex": _short()}
        self.pace = {"codex": {"runway_h": 100, "tokens_per_hour": 10e6,
                               "horizon_h": 100}}          # 10M/h lasts
        self.red = {"families": {"codex": {"per_hour": 10e6, "seats": {
            "alpha-codex": 7e6}}}}                         # 7M vs 3M: RED
        self.latch = os.path.join(self.tmp, "latch.json")

    def run_pass(self, at, flags=None, path=None, post=None):
        return teams.budget_watch_pass(flags or self.flags, self.pace,
                                       self.red, now=NOW + at,
                                       post=post or self.post,
                                       path=path or self.latch)

    def test_a_colour_that_flips_back_inside_the_dwell_posts_nothing(self):
        yellow = {"codex": {"colour": bf.YELLOW, "axis": "money"}}
        for at, flags in ((0, None), (60, yellow), (120, None), (180, yellow),
                          (240, None)):
            self.run_pass(at, flags)
        self.assertEqual(self.posts, [])
        # CONTROL: the same colour held through the dwell is posted, once
        self.run_pass(240 + teams.budget_dwell_s())
        self.run_pass(240 + 2 * teams.budget_dwell_s())
        self.assertEqual(len(self.posts), 1)
        self.assertIn("unrationed → RED", self.posts[0][1])

    def test_the_line_goes_to_the_lead_not_the_room(self):
        from unittest import mock
        self.run_pass(0)
        self.run_pass(self.DAY)
        self.assertEqual(self.dms, ["alpha-lead"])
        self.assertEqual(len(self.posts), 1)
        # THE REAL DOOR: a DM to the lead, keyed on the crossing, so a retry
        # of the same crossing is one row
        latch = os.path.join(self.tmp, "latch-door.json")
        with mock.patch("helm.chat.post") as posted:
            for at in (0, self.DAY):
                teams.budget_watch_pass(self.flags, self.pace, self.red,
                                        now=NOW + at, path=latch)
        self.assertEqual(posted.call_count, 1)
        kw = posted.call_args.kwargs
        self.assertEqual((kw.get("dm"), kw.get("sign")), ("alpha-lead", False))
        self.assertEqual(kw.get("event_id"),
                         "project-budget:alpha:codex:RED:%d" % NOW)

    def test_a_team_with_no_lead_posts_to_its_room_waking_nobody(self):
        from unittest import mock
        self.write("alpha", self.team(("alpha-codex", "codex", "builder"),
                                      codex=30), 1)
        with mock.patch("helm.chat.post") as posted:
            for at in (0, self.DAY):
                teams.budget_watch_pass(self.flags, self.pace, self.red,
                                        now=NOW + at, path=self.latch)
        self.assertEqual(posted.call_count, 1)
        kw = posted.call_args.kwargs
        self.assertEqual((kw.get("room"), kw.get("dm"), kw.get("ambient")),
                         ("alpha", None, True))

    def test_a_latch_that_cannot_be_written_is_reported_and_never_reposts(self):  # noqa: VACUOUS_ASSERTION — the loop is over three passes asserted by a literal range, and the writable-latch control after it asserts one post by count
        blocker = os.path.join(self.tmp, "not-a-directory")
        with open(blocker, "w", encoding="utf-8") as fh:
            fh.write("a file where the latch's directory should be")
        path = os.path.join(blocker, "latch.json")
        got = [self.run_pass(i * self.DAY, path=path) for i in range(3)]
        self.assertEqual(self.posts, [])
        for sent, problem in got:
            self.assertEqual(sent, [])
            self.assertIn("latch", problem)
            self.assertIn("could not be written", problem)
        # CONTROL: the same passes against a writable latch post, once
        for i in range(3):
            self.run_pass(i * self.DAY)
        self.assertEqual(len(self.posts), 1)


class ShortOnMoneyOnlyTest(Home):
    """D1 (the task/3156 design read): a share rations a family only while
    the MONEY axis, or an owner declaration, set its ORANGE. The flag's
    headline `axis` names the axis that set its colour. An ORANGE that reach
    (seats not answering) or policy set is the family's own colour for every
    project. Rationing it turned a reach flap into a project colour, and the
    watchdog posted a PROJECT-BUDGET line on every flap."""

    def setUp(self):
        super().setUp()
        self.write("alpha", self.team(("alpha-codex", "codex", "builder"),
                                      codex=30), 0)
        self.posts = []
        self.pace = {"codex": {"runway_h": 100, "tokens_per_hour": 10e6,
                               "horizon_h": 100}}          # 10M/h lasts
        # 7M/h against a 3M/h budget: past twice it, RED when rationed
        self.burn = {"families": {"codex": {"per_hour": 10e6, "seats": {
            "alpha-codex": 7e6}}}}
        self.teams = {"alpha": self.team(("alpha-codex", "codex", "builder"),
                                         codex=30)}

    def orange(self, axis):
        axes = {"money": bf.YELLOW, "reach": bf.GREEN, "policy": None,
                "declared": None}
        axes[axis] = bf.ORANGE
        return {"codex": {"colour": bf.ORANGE, "axis": axis, "axes": axes}}

    def row(self, flags):
        return teams.allocation(flags, self.pace, self.burn,
                                self.teams)["alpha"]["codex"]

    def test_an_orange_that_reach_or_policy_set_rations_nobody(self):  # noqa: VACUOUS_ASSERTION — each loop is over a literal pair of axes and asserts a tuple equality per axis
        for axis in ("reach", "policy"):
            got = self.row(self.orange(axis))
            self.assertEqual((got["colour"], got["rationed"]),
                             (bf.ORANGE, False), axis)
            self.assertEqual(got["family_colour"], bf.ORANGE)
        # CONTROL: money, or the owner's declaration, behind the same ORANGE
        # rations it, and 7M/h against 3M/h is past twice the budget
        for axis in ("money", "declared"):
            got = self.row(self.orange(axis))
            self.assertEqual((got["colour"], got["rationed"]),
                             (bf.RED, True), axis)

    def test_the_door_says_nothing_for_a_reach_orange(self):
        inside = os.path.join(self.paths["alpha"], "src")
        self.assertIsNone(teams.door_note(
            inside, family="codex",
            inputs=(self.orange("reach"), self.pace, self.burn, None)))
        # CONTROL: the money ORANGE is named at the same door
        self.assertIn("RED for alpha", teams.door_note(
            inside, family="codex",
            inputs=(self.orange("money"), self.pace, self.burn, None)))

    def test_a_reach_flap_posts_no_project_budget_line(self):
        latch = os.path.join(self.tmp, "latch.json")
        yellow = {"codex": {"colour": bf.YELLOW, "axis": "money"}}
        for i, flags in enumerate((self.orange("reach"), yellow,
                                   self.orange("reach"), yellow,
                                   self.orange("reach"),
                                   self.orange("reach"))):
            teams.budget_watch_pass(flags, self.pace, self.burn,
                                    now=NOW + 900 * i, post=self.post,
                                    path=latch)
        self.assertEqual(self.posts, [])
        # CONTROL: a money ORANGE held over the same passes is posted
        for i in range(2):
            teams.budget_watch_pass(self.orange("money"), self.pace,
                                    self.burn, now=NOW + 9000 + 900 * i,
                                    post=self.post, path=latch)
        self.assertEqual(len(self.posts), 1)
        self.assertIn("unrationed → RED", self.posts[0][1])


class OneFamilyDoorTest(Home):
    """ONE ANSWER FOR A SEAT'S FAMILY (coordinator ruling on task/3156): the
    team card asks `seat_usability.burn_family`, the door the web board and
    the signing-liveness read ask, so no surface keeps a second ordering.

    The roster and the one spawn register are files this arm writes into its
    own temp home, one seat per rung the door has: a verified runtime, a
    verified runtime under a display name the grammar refuses, an UNVERIFIED
    runtime (display evidence, never believed), the numbered and bare name
    grammar, a register-only project seat, a local family and a seat nothing
    names."""

    def setUp(self):
        super().setUp()
        from helm import chat, seat
        now = __import__("time").time()
        a = self.paths["alpha"]

        def row(**extra):
            return dict({"home_room": "alpha", "last_seen": now, "cwd": a},
                        **extra)
        self.roster = {
            "alpha-claude": row(runtime={"family": "claude"},
                                runtime_verified=True),
            "seat-a": row(runtime={"family": "codex"},
                            runtime_verified=True),
            "codex-41": row(runtime={"family": "kimi"},
                           runtime_verified=False),
            "kimi": row(),
            "alpha-builder": row(),
            "qwen27": row(runtime={"family": "qwen27"},
                          runtime_verified=True),
            "alpha-stranger": row()}
        os.makedirs(chat.chat_dir(), exist_ok=True)
        pk.write_json(os.path.join(chat.chat_dir(), ".roster.json"),
                      self.roster)
        d = seat._instance_dir("codex", "alpha-builder")
        os.makedirs(d, exist_ok=True)
        pk.write_json(seat._spawn_path(d), {"seat": "alpha-builder",
                                            "family": "codex",
                                            "project": "alpha"})

    def test_the_team_card_and_burn_family_agree_for_every_roster_seat(self):  # noqa: VACUOUS_ASSERTION — the want table is asserted whole first, and every seat with a family is asserted present on the picker and the proposal before a None seat's absence is
        from helm import seat_usability
        from helm.seats_roster import roster_checked
        roster, failed = roster_checked()
        self.assertFalse(failed)
        self.assertEqual(sorted(roster), sorted(self.roster))
        world = teams.placements()
        model = teams.board_model()
        picker = {s["seat"]: s["family"] for s in model["seats"]}
        proposed = model["projects"]["alpha"]
        self.assertFalse(proposed["authored"])
        members = {m["seat"]: m["family"] for m in proposed["members"]}
        want = {seat: seat_usability.burn_family(dict(row, seat=seat))
                for seat, row in roster.items()}
        # MUST-HIT: every rung answered, so agreement is not agreement on
        # one shape — the unverified runtime is NOT believed, the register
        # names the project seat's family, and a seat nothing names is None
        self.assertEqual(want, {"alpha-claude": "anthropic",
                                "seat-a": "codex", "codex-41": "codex",
                                "kimi": "kimi", "alpha-builder": "codex",
                                "qwen27": "qwen27", "alpha-stranger": None})
        for seat, row in sorted(roster.items()):
            self.assertEqual(teams.family_of(seat, row), want[seat], seat)
            self.assertEqual(world["seats"][seat]["family"], want[seat], seat)
            if want[seat] is None:
                # a seat with no family is on no card: never offered, never
                # proposed
                self.assertNotIn(seat, picker)
                self.assertNotIn(seat, members)
                continue
            self.assertEqual(picker[seat], want[seat], seat)
            self.assertEqual(members[seat], want[seat], seat)

    def test_a_register_seam_places_a_seat_and_never_names_its_family(self):  # noqa: VACUOUS_ASSERTION — the seat's placement (project and source) is asserted present before its family's absence
        """The `registers` seam answers WHERE a seat works; its family is
        still the one door's. A seat only a seam register names has no
        family, so it is placed and never proposed."""
        from helm import seat_usability
        row = dict(self.roster["alpha-stranger"], home_room=None)
        world = teams.placements(roster={"alpha-stranger": row},
                                 registers={"alpha-stranger":
                                            ("codex", "alpha")},
                                 projects=registry.load()["projects"],
                                 integrator=None)
        placed = world["seats"]["alpha-stranger"]
        self.assertEqual((placed["project"], placed["source"]),
                         ("alpha", "spawn register"))
        self.assertEqual(placed["family"], seat_usability.burn_family(
            dict(row, seat="alpha-stranger")))
        self.assertIsNone(placed["family"])
        self.assertEqual(teams.derive("alpha", world=world), [])


class ReadersFailLoudTest(Home):
    """D6 (the task/3156 design read): a reader that RAISES reads FAILED on
    the card and at the verb, never "unmeasured". The board's and the
    verb's readings swallowed a raising reader into an empty one, so a torn
    burn file drew exactly as a project nobody had measured yet."""

    PACE = {"runway_h": 100, "tokens_per_hour": 10e6, "horizon_h": 100,
            "horizon_at": NOW + 360000}

    def setUp(self):
        super().setUp()
        self.write("alpha", self.team(("alpha-codex", "codex", "builder"),
                                      codex=30), 0)

    def model(self, **patches):
        from unittest import mock
        from helm import codexpace, dispatches
        with mock.patch.object(codexpace, "cached_fold_input",
                               return_value=dict(self.PACE)), \
                mock.patch.object(dispatches, "snapshot",
                                  return_value=({}, None)), \
                mock.patch.object(codexpace, "cached_seat_burn",
                                  **patches):
            return teams.board_model()

    def test_a_raising_burn_reader_reads_failed_on_the_board(self):
        got = self.model(side_effect=ValueError("burn torn"))
        row = got["projects"]["alpha"]["allocation"]["codex"]
        line = teams.line("alpha", "codex", row)
        self.assertIn("burning FAILED (ValueError)", line)
        self.assertNotIn("unmeasured", line)
        self.assertEqual((row["mode"], row.get("burn_failed")),
                         (teams.RATE, "ValueError"))
        self.assertEqual(got.get("failed"), {"burn": "ValueError"})
        # CONTROL: no reading at all is NOT a failure: it is unmeasured
        got = self.model(return_value=None)
        self.assertEqual(got["failed"], {})
        row = got["projects"]["alpha"]["allocation"]["codex"]
        self.assertIn("burning unmeasured", teams.line("alpha", "codex", row))

    def test_the_verb_says_a_reader_failed(self):
        import contextlib
        import io
        from unittest import mock
        from helm import cli, codexpace
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(codexpace, "cached_fold_input",
                               return_value=dict(self.PACE)), \
                mock.patch.object(codexpace, "cached_seat_burn",
                                  side_effect=ValueError("burn torn")), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = cli.main(["team", "alpha"])
        self.assertEqual(rc, 0, err.getvalue())
        self.assertIn("readings FAILED: burn (ValueError)", out.getvalue())
        self.assertIn("burning FAILED (ValueError)", out.getvalue())


class NonStrictPathsFailLoudTest(Home):
    """Round 3, ruling c: the paths that read on past a failure without
    raising say so. `roster_checked` reports a roster that did not read
    WITHOUT raising, and the dispatch ledger and the lane capacity file are
    read the same way; each was swallowed into "nothing there". A write
    REFUSES, the watchdog's pass posts nothing and says UNREADABLE, a door
    says FAILED, and the board names the reader."""

    def unread_roster(self):
        from unittest import mock
        from helm import seats_roster
        return mock.patch.object(seats_roster, "roster_checked",
                                 return_value=({}, True))

    def test_a_write_refuses_when_the_roster_does_not_read(self):  # noqa: VACUOUS_ASSERTION — the refusal is asserted by code and class, and the control write on a readable roster is asserted landed at v1
        team = self.team(("alpha-codex", "codex", "builder"), codex=30)
        with self.unread_roster():
            row, problem, code = self.write("alpha", team, 0)
        self.assertIsNone(row)
        self.assertEqual(code, "unread", problem)
        self.assertIn("RosterUnread", problem)
        self.assertNotIn("team", pk.read_json(home.authored_path())
                         ["projects"].get("alpha", {}))
        self.assertEqual(self.posts, [])
        # CONTROL: a roster that reads (none on disk) and the same write lands
        row, problem, _code = self.write("alpha", team, 0)
        self.assertIsNone(problem)
        self.assertEqual(row["v"], 1)

    def test_the_watch_posts_nothing_from_an_unread_roster(self):  # noqa: VACUOUS_ASSERTION — three unread passes are asserted by a literal range, and the readable control asserts one post by count
        self.write("alpha", self.team(("alpha-codex", "codex", "builder"),
                                      codex=30), 0)
        self.posts = []
        latch = os.path.join(self.tmp, "latch.json")
        pace = {"codex": {"runway_h": 100, "tokens_per_hour": 10e6,
                          "horizon_h": 100}}
        burn = {"families": {"codex": {"per_hour": 10e6, "seats": {
            "alpha-codex": 7e6}}}}

        def run(at):
            return teams.budget_watch_pass({"codex": _short()}, pace, burn,
                                           now=NOW + at, post=self.post,
                                           path=latch)
        with self.unread_roster():
            got = [run(i * 86400) for i in range(3)]
        self.assertEqual(self.posts, [])
        for sent, problem in got:
            self.assertEqual(sent, [])
            self.assertIn("UNREADABLE", problem or "")
            self.assertIn("RosterUnread", problem)
        self.assertFalse(os.path.exists(latch))
        # CONTROL: the same passes on a roster that reads post, once
        for i in range(3):
            run(i * 86400)
        self.assertEqual(len(self.posts), 1)

    def lanes_team(self):
        self.write("alpha", self.team(("alpha-lead", "anthropic", "lead"),
                                      ("qwen27", "qwen27", "reviewer"),
                                      qwen27=30), 0)

    def test_a_door_says_failed_when_the_lanes_or_capacity_do_not_read(self):
        from unittest import mock
        from helm import dispatches
        self.lanes_team()
        inside = os.path.join(self.paths["alpha"], "src")
        with mock.patch.object(dispatches, "snapshot",
                               return_value=({}, "ledger locked")):
            note = teams.door_note(inside, family="qwen27")
        self.assertIn("FAILED (LedgerUnread)", note or "")
        os.makedirs(os.path.dirname(teams.capacity_path()), exist_ok=True)
        with open(teams.capacity_path(), "w", encoding="utf-8") as fh:
            fh.write("{torn")
        with mock.patch.object(dispatches, "snapshot",
                               return_value=({}, None)), \
                mock.patch.object(dispatches, "owed", return_value=[]):
            note = teams.door_note(inside, family="qwen27")
        self.assertIn("FAILED (CapacityUnread)", note or "")
        # CONTROL: both read, nothing queues, and the door is silent
        os.remove(teams.capacity_path())
        with mock.patch.object(dispatches, "snapshot",
                               return_value=({}, None)), \
                mock.patch.object(dispatches, "owed", return_value=[]):
            self.assertIsNone(teams.door_note(inside, family="qwen27"))

    def test_the_board_names_each_reader_that_did_not_read(self):
        from unittest import mock
        from helm import dispatches
        self.lanes_team()
        with self.unread_roster(), \
                mock.patch.object(dispatches, "snapshot",
                                  return_value=({}, "ledger locked")):
            got = teams.board_model()
        self.assertEqual(got.get("failed"), {"lanes": "LedgerUnread",
                                             "roster": "RosterUnread"})
        # CONTROL: a board whose readers all read names none
        with mock.patch.object(dispatches, "snapshot",
                               return_value=({}, None)), \
                mock.patch.object(dispatches, "owed", return_value=[]):
            self.assertEqual(teams.board_model()["failed"], {})


class FamilyDoorRulesTest(Home):
    """D3 (the task/3156 design read; the cross-family read's Q4 seam): a
    member's family was authored twice with no reconciliation. `--add SEAT:ROLE:FAMILY` and
    the card stored the typed word verbatim, allocation billed the seat to
    it, and route benched the seat on whatever family the live join named,
    so one seat could burn on one family's share and bench on another. The
    FAMILY DOOR RULES: a member whose known family (`family_of`, the door
    every surface asks) disagrees is refused at the write, naming the family
    it spends; allocation reads the known family where it resolves; a seat
    that is not registered keeps the typed family."""

    def setUp(self):
        super().setUp()
        from helm import chat
        now = __import__("time").time()
        a = self.paths["alpha"]
        # a NATIVE seat: its verified runtime is claude, so it spends the
        # native credential whatever a team types for it
        self.roster = {"alpha-native": {"home_room": "alpha",
                                        "last_seen": now, "cwd": a,
                                        "runtime": {"family": "claude"},
                                        "runtime_verified": True}}
        os.makedirs(chat.chat_dir(), exist_ok=True)
        pk.write_json(os.path.join(chat.chat_dir(), ".roster.json"),
                      self.roster)

    def plant(self, *members):
        """A team authored before the door ruled: written straight into the
        authored layer, as a stale save would have left it."""
        auth = pk.read_json(home.authored_path())
        auth["projects"]["alpha"] = {"path": self.paths["alpha"], "team": {
            "v": 1, "by": "owner", "ts": NOW, "reason": "before the door",
            "members": [{"seat": s, "family": f, "role": r}
                        for s, f, r in members], "shares": {}}}
        pk.write_json(home.authored_path(), auth)

    def test_a_member_whose_known_family_disagrees_is_refused(self):  # noqa: VACUOUS_ASSERTION — the refused write is asserted by code and sentence, and the control write of the same seat on the family it spends is asserted landed by equality
        row, problem, code = self.write(
            "alpha", self.team(("alpha-native", "codex", "reviewer")), 0)
        self.assertIsNone(row)
        self.assertEqual(code, "invalid", problem)
        self.assertIn("alpha-native spends anthropic, not codex", problem)
        self.assertNotIn("team", pk.read_json(home.authored_path())
                         ["projects"].get("alpha", {}))
        # CONTROLS: the family it spends is written, and a seat the family
        # door cannot name keeps the family the owner typed
        row, problem, _code = self.write(
            "alpha", self.team(("alpha-native", "anthropic", "lead"),
                               ("alpha-future", "codex", "builder")), 0)
        self.assertIsNone(problem)
        self.assertEqual({m["seat"]: m["family"]
                          for m in row["team"]["members"]},
                         {"alpha-native": "anthropic",
                          "alpha-future": "codex"})

    def test_the_door_compares_family_words_through_the_alias_table(self):
        """The cross-family read's minor on round 2: `family_disagrees`
        compared raw words, so a door that answered in the harness's word
        (`claude`) refused a member written in the flag's (`anthropic`) as
        a different family. Both sides pass through the one alias table
        route and the burn-flag door already share."""
        members = [{"seat": "alpha-native", "family": "anthropic",
                    "role": "lead"}]
        self.assertIsNone(teams.family_disagrees(
            members, lambda seats: {"alpha-native": "claude"}))
        self.assertIsNone(teams.family_disagrees(
            members, lambda seats: {"alpha-native": "Claude"}))
        # CONTROL: a family that differs after the aliases still refuses,
        # naming it in the flag's word
        got = teams.family_disagrees(
            members, lambda seats: {"alpha-native": "codex"})
        self.assertIn("alpha-native spends codex, not anthropic", got)

    def test_allocation_bills_a_member_to_the_family_it_spends(self):
        self.plant(("alpha-native", "codex", "reviewer"))
        rows = teams.project_row("alpha", None,
                                 inputs=({"codex": _short()}, {}, None, None))
        self.assertIn("anthropic", rows)
        self.assertNotIn("codex", rows)

    def test_drift_names_a_member_on_the_wrong_family(self):
        self.plant(("alpha-native", "codex", "reviewer"))
        world = teams.placements(projects=registry.load()["projects"],
                                 integrator=None)
        got = teams.drift("alpha", teams.read("alpha", world=world), world)
        differs = [d for d in got if d["kind"] == "family-differs"]
        self.assertEqual([d["seat"] for d in differs], ["alpha-native"])
        self.assertIn("spends anthropic", differs[0]["text"])
        # CONTROL: the same seat written on the family it spends is no drift
        self.assertEqual([d for d in teams.drift(
            "alpha", self.team(("alpha-native", "anthropic", "lead")),
            world) if d["kind"] == "family-differs"], [])


class VerbTest(Home):
    """`helm team`, run through the dispatcher the way an agent runs it from
    chat on the owner's behalf. The roster and the burn snapshot are files
    this arm writes into its own temp home."""

    def setUp(self):
        super().setUp()
        from unittest import mock
        env = mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "seat-a"})
        env.start()
        self.addCleanup(env.stop)
        post = mock.patch("helm.chat.post")
        self.chat_post = post.start()
        self.addCleanup(post.stop)
        from helm import chat
        now = __import__("time").time()
        a = self.paths["alpha"]
        roster = {"alpha-claude": {"home_room": "alpha", "last_seen": now,
                                   "runtime": {"family": "claude"},
                                   "runtime_verified": True, "cwd": a},
                  "alpha-codex": {"home_room": "alpha", "last_seen": now,
                                  "runtime": {"family": "codex"},
                                  "runtime_verified": True, "cwd": a},
                  "beta-codex": {"home_room": "beta", "last_seen": now,
                                 "runtime": {"family": "codex"},
                                 "runtime_verified": True,
                                 "cwd": self.paths["beta"]}}
        os.makedirs(chat.chat_dir(), exist_ok=True)
        pk.write_json(os.path.join(chat.chat_dir(), ".roster.json"), roster)
        from helm import codexpace
        os.makedirs(os.path.dirname(codexpace.seat_burn_path()),
                    exist_ok=True)
        codexpace.write_seat_burn({
            "v": codexpace.V, "ts": now, "window_s": 10800, "why": None,
            "families": {"codex": {"per_hour": 10e6, "seats": {
                "alpha-codex": 3e6, "beta-codex": 6e6, "unhomed": 1e6}}}})

    def run_verb(self, *argv):
        import contextlib
        import io
        from helm import cli
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["team"] + list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_the_bare_verb_shows_the_proposal_and_says_it_binds_nothing(self):  # noqa: VACUOUS_ASSERTION — the proposal's members and version are asserted by equality and substring, unconditionally
        rc, out, err = self.run_verb("alpha")
        self.assertEqual(rc, 0, err)
        self.assertIn("PROPOSED from today's seats", out)
        self.assertIn("lead      alpha-claude", out)
        self.assertIn("builder   alpha-codex", out)
        rc, out, _err = self.run_verb("alpha", "--json")
        self.assertEqual(rc, 0)
        got = json.loads(out)["alpha"]
        self.assertFalse(got["binding"])
        self.assertEqual(got["team"]["v"], 0)

    def test_set_is_a_dry_run_then_applies_then_refuses_a_stale_edit(self):
        args = ("set", "alpha", "--expect", "0", "--add",
                "alpha-kimi:reviewer:kimi", "--share", "codex=30",
                "--reason", "alpha leads codex this week")
        rc, out, err = self.run_verb(*args)
        self.assertEqual(rc, 0, err)
        self.assertIn("dry-run alpha v0 → v1", out)
        self.assertIn("+ alpha-kimi (reviewer, kimi)", out)
        self.assertIn("codex share unset → 30%", out)
        self.assertNotIn("alpha", pk.read_json(home.authored_path())
                         ["projects"])
        rc, out, err = self.run_verb(*(args + ("--apply",)))
        self.assertEqual(rc, 0, err)
        said = pk.read_json(home.authored_path())["projects"]["alpha"]["team"]
        self.assertEqual((said["v"], said["by"]), (1, "seat-a"))
        self.assertEqual({m["seat"] for m in said["members"]},
                         {"alpha-claude", "alpha-codex", "alpha-kimi"})
        self.assertEqual(self.chat_post.call_count, 1)
        self.assertEqual(self.chat_post.call_args.kwargs["room"], "alpha")
        rc, _out, err = self.run_verb(*(args + ("--apply",)))
        self.assertEqual(rc, 1)
        self.assertIn("STALE", err)
        rc, out, err = self.run_verb("history", "alpha")
        self.assertEqual(rc, 0, err)
        self.assertIn("v1", out)
        self.assertIn("alpha leads codex this week", out)
        # the authored team now binds, and its budget line prints
        rc, out, _err = self.run_verb("alpha")
        self.assertIn("team v1, set by seat-a", out)
        self.assertIn("share codex 30%", out)

    def test_set_refuses_a_missing_version_or_reason(self):
        rc, _o, err = self.run_verb("set", "alpha", "--reason", "x")
        self.assertEqual(rc, 2)
        self.assertIn("--expect V is required", err)
        rc, _o, err = self.run_verb("set", "alpha", "--expect", "0")
        self.assertEqual(rc, 2)
        self.assertIn("--reason is required", err)
        rc, _o, err = self.run_verb("set", "alpha", "--expect", "0",
                                    "--add", "alpha-mystery:reviewer",
                                    "--reason", "x")
        self.assertEqual(rc, 2)
        self.assertIn("cannot tell which family", err)

    def test_set_refuses_a_family_the_seat_does_not_spend(self):  # noqa: VACUOUS_ASSERTION — the refusal is asserted by rc and sentence, and the control add on the family the seat spends is asserted rc 0
        """D3 at the verb: `--add SEAT:ROLE:FAMILY` naming a family the seat
        does not spend is refused in the write's words, and nothing lands."""
        rc, _out, err = self.run_verb(
            "set", "alpha", "--expect", "0", "--add",
            "beta-codex:reviewer:kimi", "--reason", "wrong family",
            "--apply")
        self.assertEqual(rc, 2, err)
        self.assertIn("beta-codex spends codex, not kimi", err)
        self.assertNotIn("alpha", pk.read_json(home.authored_path())
                         ["projects"])
        # CONTROL: the family it spends is written
        rc, _out, err = self.run_verb(
            "set", "alpha", "--expect", "0", "--add",
            "beta-codex:reviewer:codex", "--reason", "right family",
            "--apply")
        self.assertEqual(rc, 0, err)

    def test_set_refuses_when_the_roster_does_not_read(self):  # noqa: VACUOUS_ASSERTION — the refusal is asserted by rc and class; test_set_refuses_a_family_the_seat_does_not_spend is the rc 0 control on the same verb
        """Round 3, ruling c at the verb: exit 1 (not a usage error) and the
        class named; nothing lands."""
        from unittest import mock
        from helm import seats_roster
        with mock.patch.object(seats_roster, "roster_checked",
                               return_value=({}, True)):
            rc, _out, err = self.run_verb(
                "set", "alpha", "--expect", "0", "--add",
                "beta-codex:reviewer:codex", "--reason", "roster torn",
                "--apply")
        self.assertEqual(rc, 1, err)
        self.assertIn("RosterUnread", err)
        self.assertNotIn("alpha", pk.read_json(home.authored_path())
                         ["projects"])

    def test_seed_refuses_when_burn_is_not_measured(self):  # noqa: VACUOUS_ASSERTION — the refusal is asserted by rc and by the sentence, and test_seed_proposes_one_team_per_project_with_the_measured_split is the positive control on the same verb
        """D2: with no burn measured, a seed would author every team with no
        shares. It refuses instead, dry run and --apply alike, and authors
        nothing."""
        from helm import codexpace
        os.remove(codexpace.seat_burn_path())
        rc, out, err = self.run_verb("seed", "--apply")
        self.assertEqual(rc, 1, out + err)
        self.assertIn("burn", err)
        self.assertIn("not measured", err)
        self.assertEqual(pk.read_json(home.authored_path())["projects"], {})
        rc, out, err = self.run_verb("seed")
        self.assertEqual(rc, 1, out + err)
        self.assertNotIn("would seed", out)

    def test_seed_proposes_one_team_per_project_with_the_measured_split(self):
        rc, out, err = self.run_verb("seed")
        self.assertEqual(rc, 0, err)
        self.assertIn("would seed alpha", out)
        self.assertIn("would seed beta", out)
        self.assertIn("shares: codex 30%", out)        # 3M of 10M
        self.assertIn("shares: codex 60%", out)        # 6M of 10M
        self.assertIn("dry run", out)
        self.assertEqual(pk.read_json(home.authored_path())["projects"], {})
        rc, out, err = self.run_verb("seed", "--apply")
        self.assertEqual(rc, 0, err)
        auth = pk.read_json(home.authored_path())["projects"]
        self.assertEqual(auth["alpha"]["team"]["shares"], {"codex": 30})
        self.assertEqual(auth["beta"]["team"]["v"], 1)
        rc, out, _err = self.run_verb("seed")
        self.assertIn("nothing to propose", out)

    def test_the_add_seat_picker_offers_only_seats_a_team_can_hold(self):
        from unittest import mock
        from helm import chat, dispatches
        path = os.path.join(chat.chat_dir(), ".roster.json")
        roster = pk.read_json(path)
        roster["alpha-newfam"] = dict(roster["alpha-codex"],
                                      runtime={"family": "newfam"})
        pk.write_json(path, roster)
        with mock.patch.object(dispatches, "snapshot",
                               return_value=({}, None)):
            got = teams.board_model()
        seats = {s["seat"] for s in got["seats"]}
        self.assertIn("alpha-codex", seats)
        self.assertNotIn("alpha-newfam", seats)
        self.assertIn("qwen27", got["slots"]["families"])
        self.assertIn("review", got["tier"]["kinds"])

    def test_capacity_records_a_measurement_and_prints_the_lanes(self):
        from unittest import mock
        from helm import dispatches
        rc, out, err = self.run_verb("capacity", "qwen27", "4", "--reason",
                                     "four concurrent at 32k context")
        self.assertEqual(rc, 0, err)
        self.assertIn("dry-run qwen27 not measured → 4 lanes", out)
        self.assertFalse(os.path.exists(teams.capacity_path()))
        rc, out, err = self.run_verb("capacity", "qwen27", "4", "--reason",
                                     "four concurrent at 32k context",
                                     "--apply")
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.exists(teams.capacity_path()))
        with mock.patch.object(dispatches, "snapshot",
                               return_value=({}, None)):
            rc, out, err = self.run_verb("capacity")
        self.assertEqual(rc, 0, err)
        self.assertIn("qwen27       4 lanes — measured by seat-a", out)
        self.assertIn("four concurrent at 32k context", out)
        self.assertIn("qwenlocal    capacity not measured", out)
        rc, _o, err = self.run_verb("capacity", "qwen27", "many",
                                    "--reason", "x")
        self.assertEqual(rc, 2)
        self.assertIn("whole number or 'unset'", err)
        rc, _o, err = self.run_verb("capacity", "anthropic", "4",
                                    "--reason", "x")
        self.assertEqual(rc, 2)
        self.assertIn("no lane capacity", err)

    def test_the_verb_is_registered_and_documented(self):
        from helm import cli
        self.assertIn("team", cli.VERBS)
        self.assertIn("team seed", cli._VERB_HELP["team"])
        self.assertIn("team capacity", cli._VERB_HELP["team"])
        with open(os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "docs", "VERBS.md"),
                encoding="utf-8") as fh:
            self.assertIn("### `helm team [<project>]", fh.read())
        rc, out, _err = self.run_verb("--help")
        self.assertEqual(rc, 0)
        self.assertIn("team set", out)


if __name__ == "__main__":
    unittest.main()
