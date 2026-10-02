"""A review or build row never goes to a long-lived LEAD seat whose home
project is not the lane's project (task/4039).

A lead's context window holds only its own project's work. A row booked to
another project's lead loads that work into a context the lead keeps, and
every later turn it takes pays for it; an idle lead costs nothing. The
sender's own subagent or orchestration run spends the same tokens once, in a
context that is thrown away (store premise
a-leads-context-holds-only-its-own-projects-work).

The one writer every `dispatch send` and `dispatch add` reaches
(`dispatches._base`) reads the recipient's role and home from the roster, its
spawn register and its family, and refuses the front-door seat, another
project's lead and any seat it cannot place. Bench seats and the lane
project's own leads are admitted, and so is a lead whose TEAM covers the
lane's project: a product pair works across several registered repositories
(a sibling repo, a fork, a dependency), so its lead's teammate, or the lane
project's authored team, places the lead on that project's work. A bench
seat's hand-back to the lead that dispatched its build is admitted too: the
row's supersedes chain holds a row from that lead to that bench seat. Every
seat and project here is a fixture.
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import dispatches, home, seat, seat_catalog, seats, teams  # noqa: E402,F401 — the facade first (seat_compat)
from helm.seat_lifecycle_sessions import project_families  # noqa: E402
from tests.test_dispatches import DispatchBase  # noqa: E402

PREMISE = "a-leads-context-holds-only-its-own-projects-work"
NATIVE = {"agent_harness": "claude", "family": "claude", "backend": "native"}


def _bench_name():
    """A seat named for a catalog family no project seat is made of, that
    the catalog does not keep off the review bench: built from the catalog,
    so no fixture names a real seat."""
    family = next(f for f in sorted(seat_catalog.FAMILIES)
                  if f not in project_families()
                  and not seat_catalog.FAMILIES[f].get("bench_role"))
    return "%s-91" % family


class _LeadWorld(DispatchBase):
    """The send and add doors, end to end, on a temp home, repos and roster."""

    def setUp(self):
        super().setUp()
        # THE REAL DOOR, put back over the fixture's one-project stand-in
        # (tests/_tmphome.pin_lead_context); the stand-in's cleanup restores
        # it again at the end of the case.
        dispatches._lead_context_refusal = self._real_lead_context_refusal
        other = os.path.join(self.tmp, "other-project")
        os.makedirs(other)
        # THE PRODUCT'S SIBLING REPOSITORY, registered as a project of its
        # own: the real shape of a product pair working its second repo.
        self.sibling = os.path.join(self.tmp, "acme-api")
        os.makedirs(self.sibling)
        for args in (("init", "-q"), ("config", "user.email", "t@example.com"),
                     ("config", "user.name", "Test"),
                     ("commit", "-q", "--allow-empty", "-m", "api")):
            self.git(*args, cwd=self.sibling)
        self.sibling_tip = self.git("rev-parse", "HEAD", cwd=self.sibling)
        path = home.registry_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "projects": {
                "acme": {"path": os.path.realpath(self.repo)},
                "acme-api": {"path": os.path.realpath(self.sibling)},
                "otherproj": {"path": os.path.realpath(other)}}}, handle)
        seats.write_roster("acme-lead", cwd=self.repo, home_room="acme",
                           home_room_source="explicit", runtime=NATIVE)
        seats.write_roster("acme-partner", cwd=self.repo, home_room="acme",
                           home_room_source="explicit", runtime=NATIVE)
        seats.write_roster("other-lead", cwd=other, home_room="otherproj",
                           home_room_source="explicit", runtime=NATIVE)
        seats.write_roster("front-door-seat", cwd=self.repo,
                           home_room="main", home_room_source="explicit",
                           runtime=NATIVE)
        self.bench = _bench_name()
        seats.write_roster(self.bench, cwd=self.repo, home_room="acme",
                           home_room_source="explicit")

    def send(self, recipient, kind="review", sender=None, sibling=False):
        """One row from `sender` (the fixture's own seat when None), on the
        acme repository or, with `sibling`, on acme's sibling repository."""
        # One lane per send: a second open row on one lane is its own refusal.
        # Every row names its task: a new review chain with no task meets the
        # taskless-review refusal, and an admitted arm would then measure that
        # door instead of this one.
        self._lanes = getattr(self, "_lanes", 0) + 1
        env = {"HELM_CHAT_NAME": sender} if sender else {}
        with mock.patch.object(seats, "dm",
                               return_value=({"id": "post-1"}, None)), \
                mock.patch.dict(os.environ, env):
            return dispatches.send(
                recipient, "lead-context-%d" % self._lanes,
                "please read the tip and record a verdict",
                self.sibling_tip if sibling else self.a,
                repo=self.sibling if sibling else self.repo, sign=False,
                kind=kind, new_work=True, force=True,
                task=self.review_task["id"])

    def assertRefused(self, row, why, *words):
        self.assertIsNone(row, why)
        self.assertIn(PREMISE, why)
        self.assertIn("--force does not open it", why)
        corrected = [line for line in why.splitlines()
                     if line.startswith("corrected:")]
        self.assertEqual(len(corrected), 1, why)
        self.assertIn("subagent", corrected[0])
        self.assertIn("--reviewer-run", corrected[0])
        for word in words:
            self.assertIn(word, why)
        self.assertEqual(dispatches.open_rows(), [])


class LeadContextGuardTest(_LeadWorld):
    """One lead, one project: the door's own refusals and admissions."""

    def test_a_review_to_another_projects_lead_is_refused(self):  # noqa: VACUOUS_ASSERTION — the refusal is asserted by its words and the empty ledger is the same home the own-lead CONTROL arm proves a row lands in
        row, why, _sent = self.send("other-lead")
        self.assertRefused(row, why, "otherproj", "acme")

    def test_a_build_to_another_projects_lead_is_refused(self):  # noqa: VACUOUS_ASSERTION — as above, for the other kind
        row, why, _sent = self.send("other-lead", kind="build")
        self.assertRefused(row, why, "otherproj")

    def test_an_add_to_another_projects_lead_is_refused(self):  # noqa: VACUOUS_ASSERTION — the second door reaches the same writer
        row, why = dispatches.add("other-lead", "lead-add", ref=self.a,
                                  repo=self.repo, kind="review",
                                  new_work=True, notify=False, force=True,
                                  task=self.review_task["id"], _reason=True)
        self.assertRefused(row, why, "otherproj")

    def test_a_review_to_the_front_door_seat_is_refused(self):  # noqa: VACUOUS_ASSERTION — the refusal is asserted by its words and the empty ledger is the same home the CONTROL arms prove rows land in
        row, why, _sent = self.send("front-door-seat")
        self.assertRefused(row, why, "front-door", "#main")

    def test_CONTROL_a_review_to_a_bench_seat_is_admitted(self):
        row, why, _sent = self.send(self.bench)
        self.assertIsNotNone(row, why)
        self.assertEqual(row["recipient"], self.bench)

    def test_CONTROL_a_review_to_the_lane_projects_own_lead_is_admitted(self):
        row, why, _sent = self.send("acme-lead")
        self.assertIsNotNone(row, why)
        self.assertEqual(row["recipient"], "acme-lead")

    def test_an_unreadable_roster_refuses(self):  # noqa: VACUOUS_ASSERTION — the refusal names the unread roster, and the bench CONTROL arm proves this same seat is admitted while the roster reads
        with open(seats.roster_path(), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertTrue(seats.roster_checked()[1],
                        "the roster still reads, so this arm measures "
                        "nothing")
        row, why, _sent = self.send(self.bench)
        self.assertRefused(row, why, "the seat roster did not read")

    def test_a_rostered_seat_with_no_family_or_home_refuses(self):  # noqa: VACUOUS_ASSERTION — the refusal names what is missing; the bench CONTROL arm proves a placeable seat is admitted
        seats.write_roster("unplaced-seat")
        row, why, _sent = self.send("unplaced-seat")
        self.assertRefused(row, why, "no readable role and home")


class TeamSpanTest(_LeadWorld):
    """A team works across several registered repositories (task/4039). The
    door read `teams.place(lead) == project_for_cwd(lane)` by exact registry
    key, so a product pair was refused its own traffic on its sibling repo,
    its fork and its dependency repo. These arms replay that shape: acme's
    lead and its partner, both homed in acme, on acme's sibling repository
    `acme-api`, a project of its own."""

    def test_RED_a_pair_partner_on_its_products_sibling_repo_is_admitted(self):
        row, why, _sent = self.send("acme-partner", sender="acme-lead",
                                    sibling=True)
        self.assertIsNotNone(row, why)
        self.assertEqual(row["recipient"], "acme-partner")
        self.assertEqual(len(dispatches.open_rows()), 1)

    def test_RED_a_build_to_the_partner_on_the_sibling_repo_is_admitted(self):
        row, why, _sent = self.send("acme-partner", kind="build",
                                    sender="acme-lead", sibling=True)
        self.assertIsNotNone(row, why)
        self.assertEqual(row["recipient"], "acme-partner")

    def test_RED_the_lane_projects_authored_team_covers_a_shared_seat(self):
        # The owner seats other-lead on acme-api's team as well (a seat may
        # sit on several teams), so a sender from outside its home still
        # reaches it on acme-api's work.
        made, problem, _code = teams.write(
            "acme-api", {"members": [{"seat": "other-lead",
                                      "family": "anthropic",
                                      "role": "builder"}]}, 0, by="owner",
            reason="shared seat", apply=True, post=lambda *_a: None,
            families_of=lambda _seats: {})
        self.assertIsNotNone(made, problem)
        row, why, _sent = self.send("other-lead", sibling=True)
        self.assertIsNotNone(row, why)
        self.assertEqual(row["recipient"], "other-lead")

    def test_RED_an_outside_sender_to_another_projects_lead_is_refused(self):  # noqa: VACUOUS_ASSERTION — the refusal is asserted by its words; the sibling-repo arm above proves a teammate's row on the same door lands
        # acme's lead is no teammate of otherproj's lead, so its row on
        # acme's own repository, or on acme's sibling, does not reach it.
        row, why, _sent = self.send("other-lead", sender="acme-lead")
        self.assertRefused(row, why, "otherproj", "acme-lead")
        row, why, _sent = self.send("other-lead", sender="acme-lead",
                                    sibling=True)
        self.assertRefused(row, why, "otherproj", "acme-api")

    def test_RED_an_unplaced_sender_to_the_partner_on_the_sibling_is_refused(self):  # noqa: VACUOUS_ASSERTION — the teammate arm above is the control: the same row from acme's lead lands
        row, why, _sent = self.send("acme-partner", sibling=True)
        self.assertRefused(row, why, "acme-api", "integrator")

    def test_RED_the_front_door_is_refused_even_from_a_teammate(self):  # noqa: VACUOUS_ASSERTION — the refusal is asserted by its words; the bench and own-lead CONTROL arms prove rows land
        row, why, _sent = self.send("front-door-seat", sender="acme-lead")
        self.assertRefused(row, why, "front-door", "#main")

    def test_RED_a_front_door_sender_is_on_no_team(self):  # noqa: VACUOUS_ASSERTION — the teammate arm above is the control
        # The front-door seat works in acme's checkout, so its working
        # directory alone would place it on acme; homed in #main, it is on no
        # project's team and carries no lead's work across repositories.
        row, why, _sent = self.send("acme-partner", sender="front-door-seat",
                                    sibling=True)
        self.assertRefused(row, why, "acme-api")


class HandBackTest(_LeadWorld):
    """A bench seat hands its work back to the lead that dispatched it
    (task/4039). The lead sent the bench a build; the bench's review row
    `--supersedes` that build and goes back to the lead. When the lead leads
    another project, the door refused it: the lead's own dispatched work
    could not come home. The door now admits a row whose supersedes chain
    holds a row FROM this recipient TO this sender on this repository, read
    off the ledger. Here otherproj's lead dispatches on acme's repository."""

    def row(self, recipient, sender, lane, kind="build", supersedes=None):
        env = {"HELM_CHAT_NAME": sender}
        with mock.patch.object(seats, "dm",
                               return_value=({"id": "post-1"}, None)), \
                mock.patch.dict(os.environ, env):
            return dispatches.send(
                recipient, lane, "please read the tip and record a verdict",
                self.a, repo=self.repo, sign=False, kind=kind,
                new_work=supersedes is None, supersedes=supersedes,
                task=self.review_task["id"] if supersedes is None else None,
                force=True)

    def test_RED_a_bench_hand_back_to_the_lead_that_sent_its_build_is_admitted(self):
        build, why, _sent = self.row(self.bench, "other-lead", "hand-back")
        self.assertIsNotNone(build, why)
        back, why, _sent = self.row("other-lead", self.bench, "hand-back",
                                    kind="review", supersedes=build["id"])
        self.assertIsNotNone(back, why)
        self.assertEqual(back["recipient"], "other-lead")
        self.assertEqual(back["supersedes"], build["id"])
        # The chain's later rounds come home the same way: the lead's fix
        # round to the bench, and the bench's next hand-back above it.
        fix, why, _sent = self.row(self.bench, "other-lead", "hand-back",
                                   supersedes=back["id"])
        self.assertIsNotNone(fix, why)
        again, why, _sent = self.row("other-lead", self.bench, "hand-back",
                                     kind="review", supersedes=fix["id"])
        self.assertIsNotNone(again, why)

    def test_RED_a_bench_new_row_to_that_lead_is_still_refused(self):  # noqa: VACUOUS_ASSERTION — the hand-back arm above is the control: the same seats, the same lead, admitted when the chain proves the shape
        build, why, _sent = self.row(self.bench, "other-lead", "new-row")
        self.assertIsNotNone(build, why)
        row, why, _sent = self.row("other-lead", self.bench, "new-row-2",
                                   kind="review")
        self.assertIsNone(row, why)
        self.assertIn(PREMISE, why)
        self.assertIn("otherproj", why)

    def test_RED_a_chain_from_another_sender_is_still_refused(self):  # noqa: VACUOUS_ASSERTION — the hand-back arm above is the control
        # acme's lead sent the build, so a row from the bench to otherproj's
        # lead superseding it answers no dispatch of otherproj's lead.
        build, why, _sent = self.row(self.bench, "acme-lead", "other-sender")
        self.assertIsNotNone(build, why)
        row, why, _sent = self.row("other-lead", self.bench, "other-sender",
                                   kind="review", supersedes=build["id"])
        self.assertIsNone(row, why)
        self.assertIn(PREMISE, why)
        self.assertIn("otherproj", why)


class HandBackWalkTest(_LeadWorld):
    """The walk itself, over a fixture ledger: it reads only the ledger, and
    a cycle, a missing ancestor, a walk past its depth and an unreadable
    ledger all admit nothing."""

    def setUp(self):
        super().setUp()
        self.repo_id = dispatches._repo_info(self.repo)["repo_id"]
        self.build = {"id": "b" * 32, "sender": "other-lead",
                      "recipient": self.bench, "repo_id": self.repo_id}

    def ask(self, ledger, supersedes):
        return self._real_lead_context_refusal(
            "other-lead", self.repo_id, sender=self.bench,
            supersedes=supersedes, current=ledger)

    def link(self, rid, parent):
        return {"id": rid, "sender": self.bench, "recipient": "acme-lead",
                "repo_id": self.repo_id, "supersedes": parent}

    def test_CONTROL_an_ancestor_build_from_the_lead_admits(self):  # noqa: VACUOUS_ASSERTION — the door returns None to admit; the refusal arms below are its positive control on the same call, each naming the premise
        ledger = {r["id"]: r for r in (self.build,
                                       self.link("a" * 32, "b" * 32))}
        self.assertIsNone(self.ask(ledger, "a" * 12))

    def test_RED_a_cycle_is_refused(self):
        ledger = {r["id"]: r for r in (self.build,
                                       self.link("a" * 32, "c" * 32),
                                       self.link("c" * 32, "a" * 32))}
        self.assertIn(PREMISE, self.ask(ledger, "a" * 32))

    def test_RED_a_missing_ancestor_is_refused(self):
        ledger = {r["id"]: r for r in (self.build,
                                       self.link("a" * 32, "c" * 32))}
        self.assertIn(PREMISE, self.ask(ledger, "a" * 32))
        self.assertIn(PREMISE, self.ask(ledger, "d" * 32))

    def test_RED_the_ancestor_must_be_on_this_repository(self):
        ledger = {r["id"]: r for r in (dict(self.build, repo_id="elsewhere"),
                                       self.link("a" * 32, "b" * 32))}
        self.assertIn(PREMISE, self.ask(ledger, "a" * 32))

    def test_RED_a_walk_past_its_depth_admits_nothing(self):
        ids = ["%032x" % (i + 1) for i in range(dispatches.HAND_BACK_DEPTH)]
        ledger = {self.build["id"]: self.build}
        for rid, parent in zip(ids, ids[1:] + [self.build["id"]]):
            ledger[rid] = self.link(rid, parent)
        self.assertIn(PREMISE, self.ask(ledger, ids[0]))
        self.assertIsNone(self.ask(ledger, ids[1]), "one row shorter, the "
                          "same chain reaches the build, so the arm above "
                          "measures the depth and nothing else")

    def test_RED_an_unreadable_ledger_admits_nothing(self):
        with mock.patch.object(dispatches, "snapshot",
                               return_value=({}, "fixture: unreadable")):
            self.assertIn(PREMISE, self.ask(None, "a" * 32))


if __name__ == "__main__":
    unittest.main()
