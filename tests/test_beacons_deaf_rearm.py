#!/usr/bin/env python3
"""task/2925 — the census names two joins apart, and a DEAF seat's pane is
asked to re-arm its beacon.

(a) The summary line "0 top-level / 0 sidechain / 8 unattributed" was ORIGIN
attribution, read from a producer stamp that nothing produces yet, and a
reader took it for the SEAT join. These arms pin the wording that tells the
two apart: a seat-joined count of its own, "unstamped" when no row was ever
stamped, and the full origin fraction (MISROUTED's denominator) as soon as any
row is.

(b) A DEAF seat whose pane DECLARES the seat has a live agent and a dead
waiter. `helm beacons --post` gives it the DEAF-IN-EFFECT path's bounded
nudge with a re-arm sentence. The arms drive the real `escalate` and the real
`resumeturn.rearm_deaf`; only the fork (`spawn_child`), the pane proof, the
live beacon probe and the pause read are doubles. No arm reaches a live pane.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from helm import beacon_origin, beacons, pk, resumeturn  # noqa: E402
from helm import seats_identity  # noqa: E402
from helm.seats_advice import beacon_monitor  # noqa: E402
from tests import test_beacons as tb  # noqa: E402

SEAT = "origin-seat"
SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
OTHER = "ffffffff-1111-2222-3333-444444444444"
#: The project the timer runs for, in these arms: the checkout under test,
#: resolved by the same helper that resolves a seat's project from its cwd.
OWN_PROJECT = seats_identity._git_project(ROOT)
PREFIELD_WHY = ("this beacon registered before origin was recorded, so "
                "nothing about its producer was ever asked")


class OriginWordingTest(tb.Base):
    """The census's attribution clause, driven through `seat_census`."""

    def setUp(self):
        super().setUp()
        self.cfg = tempfile.mkdtemp(prefix="helm-test-origin-")
        self.addCleanup(shutil.rmtree, self.cfg, True)
        self.armed = pk.parse_ts_epoch("2026-09-15T12:00:00Z")

    def _beacon(self, pid, sid, origin=None, reason="absent", prefield=False,
                starttime=100):
        """A registered LIVE waiter. `origin` stamps it through the
        producer's own formatter; `prefield` writes the row a helm that
        predates the field wrote, with no origin key at all."""
        self.waiter(pid, SEAT, sid=sid, starttime=starttime)
        os.makedirs(beacons.registry_dir(), exist_ok=True)
        row = {"seat": SEAT, "pid": pid, "session": sid,
               "starttime": starttime, "armed": self.armed,
               "home": os.environ["HELM_HOME"]}
        if not prefield:
            stamp = ({"origin": origin, "tool_use_id": "toolu_fixture",
                      "minted": self.armed} if origin else None)
            row.update(beacon_origin.row_fields(stamp,
                                                None if origin else reason))
        with open(beacons._entry_path(SEAT, pid), "w") as f:
            json.dump(row, f)

    def _row(self, sids):
        """The census row, with a pane pid (4400) DECLARING the seat."""
        agents = {"by_seat": {SEAT.casefold(): [4400]},
                  "by_pid": {4400: [SEAT]}, "home_by_pid": {}}
        return beacons.seat_census(
            SEAT, live=dict((s, tb.LAUNCHER) for s in sids),
            proc_dir=self.proc, agents=agents,
            homes=dict((s, self.cfg) for s in sids))

    @staticmethod
    def _rep(row):
        return {"seats": [row], "covered": [], "deaf": [],
                "deaf_in_effect": [], "vacant": [], "unproven": [],
                "misrouted": [row] if row["verdict"] == beacons.MISROUTED
                else [], "ghosts": [], "beacons": len(row["beacons"]),
                "surplus": 0, "ownership": beacons.ownership([row], homes={})}

    def test_all_absent_origin_says_UNSTAMPED_and_still_counts_the_seat_join(self):
        self._beacon(4501, SID)
        self._beacon(4502, OTHER, prefield=True, starttime=200)
        row = self._row((SID, OTHER))
        self.assertIs(row["agent"], True, "fixture: the pane must be named")
        self.assertEqual(2, len(row["live"]), "fixture: both beacons live")
        rep = self._rep(row)
        own = rep["ownership"]
        self.assertEqual(2, own["seat_joined"])
        self.assertEqual(2, own["unstamped"])
        self.assertTrue(own["origin_unstamped"])
        line = beacons._summary(rep)
        self.assertIn("seat-joined 2/2 live beacons", line)
        self.assertIn("origin UNSTAMPED on all 2", line)
        self.assertIn("no stamp producer exists yet", line)
        self.assertNotIn("unattributed", line,
                         "the word that sent a reader to suspect the seat "
                         "join is still on the line: %r" % line)

    def test_a_mix_of_absent_and_stamped_subagent_keeps_the_sidechain(self):
        self._beacon(4511, SID, origin=beacon_origin.ORIGIN_SUBAGENT)
        self._beacon(4512, OTHER, starttime=200)
        row = self._row((SID, OTHER))
        self.assertIn(beacons.OWNER_SUBAGENT, row["owners"],
                      "MUST-HIT: the stamped sidechain half is present")
        # AS TODAY: an unattributed sibling means the seat is not PROVEN
        # sidechain-only, so it is not MISROUTED.
        self.assertNotEqual(beacons.MISROUTED, row["verdict"])
        own = self._rep(row)["ownership"]
        self.assertEqual(1, own["sidechain"])
        self.assertEqual(1, own["unstamped"])
        self.assertFalse(own["origin_unstamped"])
        self.assertEqual([], own["misrouted"])
        line = beacons._summary(self._rep(row))
        self.assertIn("seat-joined 2/2 live beacons", line)
        self.assertIn("origin 0 top-level / 1 sidechain / 1 unattributed "
                      "(1 unstamped) of 2 live", line)

    def test_a_lone_stamped_subagent_is_still_MISROUTED(self):
        """The control for the mix: the wording change moved nothing about
        the verdict a proven sidechain-only seat earns."""
        self._beacon(4521, SID, origin=beacon_origin.ORIGIN_SUBAGENT)
        row = self._row((SID,))
        self.assertEqual(beacons.MISROUTED, row["verdict"])
        own = self._rep(row)["ownership"]
        self.assertEqual([SEAT], own["misrouted"])
        self.assertFalse(own["origin_unstamped"])
        self.assertIn("origin 0 top-level / 1 sidechain / 0 unattributed of "
                      "1 live", beacons._summary(self._rep(row)))

    def test_a_prefield_row_keeps_its_own_reason(self):
        self._beacon(4531, SID, prefield=True)
        self._beacon(4532, OTHER, reason="stale", starttime=200)
        row = self._row((SID, OTHER))
        self.assertIn(PREFIELD_WHY, row["owner_reasons"])
        self.assertTrue(any("(stale)" in why for why in row["owner_reasons"]),
                        row["owner_reasons"])
        own = self._rep(row)["ownership"]
        self.assertEqual(1, own["reasons"].get(PREFIELD_WHY))
        self.assertEqual(1, own["unstamped"],
                         "a pre-field row is unstamped, a stale stamp is not")
        self.assertFalse(own["origin_unstamped"],
                         "a REFUSED stamp is on the line, so unstamped is "
                         "not the whole truth")
        self.assertIn("2 unattributed (1 unstamped)",
                      beacons._summary(self._rep(row)))


class DeafSeatRearmNudgeTest(tb.Base):
    """The re-arm leg of `escalate`, with the real `rearm_deaf` behind it."""

    def setUp(self):
        super().setUp()
        self.spawned = []
        self.paused = ""

    def att(self, seat="alpha"):
        path = os.path.join(os.environ["HELM_CHAT_DIR"], ".roster.json")
        with open(path) as f:
            return json.load(f)[seat]["attendance"]

    def seat_row(self, cwd=ROOT, family="claude", backend="native"):  # noqa: SEAT_NAME — a runtime FAMILY value, not a seat identity
        """Give alpha's roster row a checkout and a verified runtime: the
        two facts the auto-nudge scope reads."""
        path = os.path.join(os.environ["HELM_CHAT_DIR"], ".roster.json")
        with open(path) as f:
            r = json.load(f)
        runtime = {"agent_harness": "claude", "family": family,  # noqa: SEAT_NAME — a harness name, not a seat identity
                   "backend": backend}
        r["alpha"].update(cwd=cwd, runtime_verified=True, runtime=runtime)
        with open(path, "w") as f:
            json.dump(r, f)

    def pass_(self, agent=True, covered=False, fresh=True, owed=None,
              obligation=False, **row):
        """One census, attend and escalate. `agent` plants a pane that
        DECLARES alpha; `covered` plants a live waiter for it. `owed` puts
        one addressed row in alpha's room, read WHOLE (default: owed unless
        covered); `obligation` is the dispatch reader's answer."""
        owed = (not covered) if owed is None else owed
        if fresh:
            self.roster("alpha")
            self.seat_row(**row)
        if agent:
            self.agent(90, "alpha")
        if covered:
            self.waiter(613, "alpha", sid=tb.SID_A)
        else:
            shutil.rmtree(os.path.join(self.proc, "613"), True)
            for path in os.listdir(beacons.registry_dir()) \
                    if os.path.isdir(beacons.registry_dir()) else ():
                os.unlink(os.path.join(beacons.registry_dir(), path))
        ev = {"oldest": None, "scanned": ("helm",), "seen": (),
              "bounded": {}, "estate": ("complete", "")}
        waited = None
        if owed:
            waited = 600.0
            ev = dict(ev, oldest=("helm", "r1"), seen=(("helm", "r1"),))

        def spawn(argv, pass_fds=()):
            i = argv.index("--text-file")
            with open(argv[i + 1]) as f:
                self.spawned.append((argv, f.read()))
        with mock.patch.object(beacons, "undrained",
                               return_value=(waited, None, ev)), \
                mock.patch("helm.seats_stop_signals._beacon_obligation",
                           return_value=obligation), \
                mock.patch.object(resumeturn, "_own_project", create=True,
                                  return_value=OWN_PROJECT), \
                mock.patch.object(beacons, "live_sessions",
                                  return_value={tb.SID_A: 90}), \
                mock.patch.dict(os.environ, {"HELM_PROC": self.proc}), \
                mock.patch("helm.chat.post"), \
                mock.patch("helm.notify.owner_push", return_value=True), \
                mock.patch("helm.notify.configured", return_value=True), \
                mock.patch.object(resumeturn, "spawn_child",
                                  side_effect=spawn), \
                mock.patch.object(resumeturn, "_registered",
                                  return_value=(None, None)), \
                mock.patch.object(resumeturn, "_still_deaf",
                                  return_value=(True, "")), \
                mock.patch.object(resumeturn, "_delivery_paused",
                                  side_effect=lambda *_a: self.paused):
            rep = beacons.census()
            reg = beacons.attend(rep)
            out = beacons.escalate(reg["transitions"], rep)
        return rep, out

    def test_a_DEAF_seat_with_a_declared_pane_gets_one_rearm_nudge(self):
        rep, out = self.pass_()
        self.assertEqual([beacons.DEAF], [r["verdict"] for r in rep["seats"]],
                         "fixture: the census did not read DEAF")
        self.assertIs(rep["seats"][0]["agent"], True)
        self.assertEqual(1, len(self.spawned), out)
        argv, text = self.spawned[0]
        key = argv[argv.index("--record-key") + 1]
        self.assertEqual(resumeturn._rearm_key("alpha", tb.SID_A), key)
        self.assertIn("--attempt", argv)
        self.assertIn(beacon_monitor("alpha"), text,
                      "the nudge must name the exact unfiltered recipe")
        self.assertEqual([("alpha", "wake")],
                         [e[:2] for e in out.get("rearmed") or ()])
        self.assertIsNone(out.get("nudged"),
                          "a DEAF seat is not the DEAF-IN-EFFECT turn nudge")
        ep = self.att()["repair"]
        self.assertEqual(("rearm", "wake"), (ep.get("kind"), ep["outcome"]))
        self.assertEqual(self.att()["since"], ep["since"])

    def test_a_DEAF_seat_that_owes_nothing_is_never_typed_into(self):  # noqa: VACUOUS_ASSERTION — positive control is test_a_DEAF_seat_with_an_owed_addressed_row_is_nudged, the same pass with one owed row
        """Canon `ping-silent-while-owing-never-wake-clean-idle`: a clean
        idle seat is never woken, and a DEAF one is no exception. A typed
        prompt starts a paid turn on a seat that owes nothing."""
        rep, out = self.pass_(owed=False)
        self.assertEqual([beacons.DEAF], [r["verdict"] for r in rep["seats"]],
                         "the census still reports it DEAF")
        self.assertIs(rep["seats"][0]["agent"], True,
                      "fixture: the pane is declared, so only owing decides")
        self.assertEqual([], self.spawned)
        self.assertIsNone(out.get("rearmed"))
        self.assertTrue(beacons._repair_due(self.att()),
                        "owing nothing now must not settle the spell")

    def test_a_DEAF_seat_with_an_owed_addressed_row_is_nudged(self):
        _rep, out = self.pass_(owed=True)
        self.assertEqual(1, len(self.spawned), out)
        self.assertIn("a row addressed to you has waited 600s",
                      self.spawned[0][1],
                      "the nudge must say what the seat owes")

    def test_a_DEAF_seat_that_owes_dispatch_work_is_nudged(self):
        _rep, out = self.pass_(owed=False, obligation=True)
        self.assertEqual(1, len(self.spawned), out)
        self.assertIn("you owe open dispatch work", self.spawned[0][1])

    def test_a_foreign_project_seat_is_reported_and_never_typed_into(self):  # noqa: VACUOUS_ASSERTION — positive control is test_a_DEAF_seat_with_an_owed_addressed_row_is_nudged, the same owing seat in this project
        """RULING: the timer types only into seats of the project it runs
        for. Another project's seats and runtime belong to its lead."""
        other = tempfile.mkdtemp(prefix="helm-test-foreign-")
        self.addCleanup(shutil.rmtree, other, True)
        subprocess.run(["git", "init", "-q", other], check=True)
        theirs = seats_identity._git_project(other)
        self.assertTrue(theirs and theirs != OWN_PROJECT,
                        "fixture: the foreign checkout must resolve to "
                        "another project")
        _rep, out = self.pass_(cwd=other)
        self.assertEqual([], self.spawned)
        (seat, action, detail), = out.get("rearmed") or [(None,) * 3]
        self.assertEqual(("alpha", "foreign-project"), (seat, action))
        self.assertIn(theirs, detail, "the report must name its project")
        self.assertIn("owes at least 1 row", detail)

    def test_a_paid_family_seat_is_reported_and_never_typed_into(self):  # noqa: VACUOUS_ASSERTION — positive control is test_a_DEAF_seat_with_an_owed_addressed_row_is_nudged, the same owing seat on native claude
        """RULING: automatic typing reaches native claude seats only. A
        proxy or codex family is billed per turn."""
        _rep, out = self.pass_(family="grok", backend="proxy")
        self.assertEqual([], self.spawned)
        (seat, action, detail), = out.get("rearmed") or [(None,) * 3]
        self.assertEqual(("alpha", "paid-family"), (seat, action))
        self.assertIn("owes at least 1 row; wake is a paid turn; not "
                      "auto-nudged", detail)

    def test_a_DEAF_seat_whose_agent_is_unknown_is_never_typed_into(self):  # noqa: VACUOUS_ASSERTION — positive control is test_a_DEAF_seat_with_a_declared_pane_gets_one_rearm_nudge, the same pass with the pane planted
        rep, out = self.pass_(agent=False)
        self.assertEqual([beacons.DEAF], [r["verdict"] for r in rep["seats"]])
        self.assertIsNone(rep["seats"][0]["agent"],
                          "fixture: the census must read the agent UNKNOWN")
        self.assertEqual([], self.spawned)
        self.assertIsNone(out.get("rearmed"))

    def test_a_paused_seat_is_refused_and_stays_due(self):  # noqa: VACUOUS_ASSERTION — the refusal is asserted positively on out['rearmed'] == paused; the unpaused control is test_a_DEAF_seat_with_a_declared_pane_gets_one_rearm_nudge
        self.paused = "state DARK"
        _rep, out = self.pass_()
        self.assertEqual([], self.spawned)
        self.assertEqual([("alpha", "paused")],
                         [e[:2] for e in out.get("rearmed") or ()])
        self.assertTrue(beacons._repair_due(self.att()),
                        "a refusal before the act must not settle the spell")

    def test_a_second_pass_in_the_same_spell_does_not_nudge_again(self):  # noqa: VACUOUS_ASSERTION — the first pass's spawn count of 1 is the unconditional positive control on the same observable
        self.pass_()
        self.assertEqual(1, len(self.spawned))
        since = self.att()["since"]
        # THE BOUND: an immediate second pass meets the debounce.
        _rep, out = self.pass_(fresh=False)
        self.assertEqual(1, len(self.spawned), "the bound let a second nudge "
                         "through within the debounce")
        self.assertEqual([("alpha", "debounce")],
                         [e[:2] for e in out.get("rearmed") or ()])
        # THE LATCH: once the child reports it typed, the spell is settled
        # even with every rate bound lifted.
        ep = self.att()["repair"]
        self.assertTrue(beacons.settle_repair("alpha", "typed", "took it",
                                              attempt=ep["attempt"]))
        with mock.patch.object(resumeturn, "_debounce_s", return_value=0), \
                mock.patch.object(resumeturn, "_spiral_s", return_value=0):
            _rep, out = self.pass_(fresh=False)
        self.assertEqual(since, self.att()["since"], "fixture: same spell")
        self.assertEqual(1, len(self.spawned),
                         "a settled spell was nudged again")
        self.assertIsNone(out.get("rearmed"))

    def test_the_rearm_child_is_licensed_by_no_beacon_not_by_an_owed_row(self):  # noqa: ORPHANED_MOCK — the doubles are reached through the lambda _admission returns, which the walker does not follow
        """The child's act doors re-ask the RE-ARM question. The owed-row
        licence would refuse every DEAF seat with an empty backlog as
        drained, so the nudge would never type."""
        self.roster("alpha")
        self.seat_row()
        own = mock.patch.object(resumeturn, "_own_project", create=True,
                                return_value=OWN_PROJECT)
        own.start()
        self.addCleanup(own.stop)
        key = resumeturn._rearm_key("alpha", tb.SID_A)
        admit = resumeturn._admission("alpha", tb.SID_A, key, None, None)
        with mock.patch.object(resumeturn, "_pause_verdict",
                               return_value=("", "")), \
                mock.patch.object(resumeturn, "_still_owed",
                                  return_value=(600.0, "")), \
                mock.patch.object(resumeturn, "_still_deaf",
                                  return_value=(True, "")):
            grant = admit("settle")
            self.assertTrue(grant.ok, grant.why)
            ok, why = grant.still()
            self.assertIs(ok, True, why)
        # A SEAT THAT DRAINED DURING THE SETTLE OWES NOTHING: withheld, and
        # not a settlement, so a row that arrives later in the spell counts.
        with mock.patch.object(resumeturn, "_pause_verdict",
                               return_value=("", "")), \
                mock.patch.object(resumeturn, "_still_owed",
                                  return_value=(None, "nothing owed")), \
                mock.patch("helm.seats_stop_signals._beacon_obligation",
                           return_value=False), \
                mock.patch.object(resumeturn, "_still_deaf",
                                  return_value=(True, "")):
            grant = admit("settle")
        self.assertFalse(grant.ok)
        self.assertEqual("idle", grant.kind)
        self.assertEqual("withheld", resumeturn._outcome_of(grant.kind))
        with mock.patch.object(resumeturn, "_pause_verdict",
                               return_value=("", "")), \
                mock.patch.object(resumeturn, "_still_deaf",
                                  return_value=(False, "re-armed")):
            grant = admit("settle")
        self.assertFalse(grant.ok)
        self.assertEqual("drained", grant.kind,
                         "a seat that re-armed settles the spell")

    def test_a_seat_covered_again_settles_the_latch(self):
        self.pass_()
        self.assertEqual("wake", self.att()["repair"]["outcome"])
        rep, _out = self.pass_(covered=True, fresh=False)
        self.assertEqual([beacons.COVERED],
                         [r["verdict"] for r in rep["seats"]],
                         "fixture: the waiter must read covered")
        ep = self.att()["repair"]
        self.assertEqual("covered", ep["outcome"])
        self.assertFalse(beacons._repair_due(dict(self.att(),
                                                  since=ep["since"])))
        # A NEW DEAF SPELL IS DUE AGAIN, so the latch closed one spell only.
        time.sleep(0.01)
        with mock.patch.object(resumeturn, "_debounce_s", return_value=0), \
                mock.patch.object(resumeturn, "_spiral_s", return_value=0):
            self.pass_(fresh=False)
        self.assertEqual(2, len(self.spawned),
                         "the next DEAF spell was never nudged")


if __name__ == "__main__":
    unittest.main()
