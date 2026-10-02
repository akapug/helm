"""THE ONE DEFINITION OF FLEET-DOWN (`beacon_phone.qualify`): fake roster,
clock and census only.

The retired pager's delivery (`tick`, `_send`) is gone (task/3939): the
office weather pages fleet-down. These pin the qualifier it calls: the steward
or a strict majority unreachable, over a complete census matching the
attendance register, with the cohort frozen for the episode. `self.state` is
the episode the caller keeps between passes, as the weather does; `heard()`
marks it delivered, as the weather does once the phone holds the storm.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from helm import beacon_phone, beacons, seats_roster


NOW = 20000.0


class FleetDownTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, ".roster.json")
        self.patch = mock.patch.object(seats_roster, "roster_path",
                                       return_value=self.path)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.env = mock.patch.dict(os.environ, {"HELM_STEWARD_SEAT": "alpha"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.rows = {name: self.row() for name in ("alpha", "beta", "gamma")}
        self.state = dict(beacon_phone.CLEAR)
        self.save()

    def row(self, covered=NOW - 500):
        return {"session": "fixture", "attendance":
                {"covered": covered, "at": NOW, "state": beacons.COVERED,
                 "alarm": False}}

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.rows, f)

    def report(self, states=None, seats=None):
        states = states or {}
        names = seats if seats is not None else list(self.rows)
        report = []
        for name in names:
            verdict = states.get(name, beacons.COVERED)
            row = {"seat": name, "verdict": verdict,
                   "live": verdict == beacons.COVERED}
            report.append(row)
            if name in self.rows:
                self.rows[name]["attendance"].update(
                    state=verdict, alarm=beacons.unreachable(row))
        self.save()
        return {"seats": report, "live_probe": True, "agent_probe": True,
                "unreachable":
                [row for row in report if beacons.unreachable(row)]}

    def record(self, report, at=NOW):
        """What `beacons.attend` records beside the register on a --post."""
        with open(beacons.attended_path(self.path), "w",
                  encoding="utf-8") as f:
            json.dump(beacons.attended(report, at), f)

    def judge(self, report, complete=True):
        """One pass of the qualifier over the RECORDED census; the episode
        it hands back is kept."""
        if not complete:
            report = dict(report, agent_probe=False)
        self.record(report)
        got = beacon_phone.fleet(NOW, self.state)
        self.state = got["latch"]
        return got

    def heard(self):
        self.state = dict(self.state, delivered=True)

    def test_escalation_does_not_page_fleet_down(self):  # noqa: VACUOUS_ASSERTION — the chat leg posting is the unconditional positive control on the same escalate call; the silent phone is the claim
        # Arm C (task/3939): escalate no longer pages the owner — the weather
        # owns the fleet-down pager. escalate still posts the chat leg and hands
        # the pager leg dormant so the two-writers rule holds.
        rep = self.report({"alpha": beacons.DEAF})
        with mock.patch("helm.notify.owner_push", return_value=True) as push:
            first = beacons.escalate([], rep, now=NOW, complete=True)
            second = beacons.escalate([], rep, now=NOW, complete=True)
        self.assertTrue(first["chat"])
        self.assertEqual(first["phone"]["phase"], "dormant")
        self.assertFalse(first["push"])
        self.assertEqual(second["phone"]["phase"], "dormant")
        self.assertFalse(second["push"])
        push.assert_not_called()

    def test_explicit_steward_down_is_fleet_down_with_no_transitions(self):
        rep = self.report({"alpha": beacons.DEAF})
        rep["unreachable"] = []  # the qualifier reads seats, not edges
        got = self.judge(rep)
        self.assertEqual(got["phase"], "down")
        self.assertIn("steward", got["why"])
        self.assertEqual(self.state["cohort"], ["alpha", "beta", "gamma"])
        self.assertFalse(self.state["delivered"])

    def test_one_deaf_non_steward_is_not_down(self):
        self.assertEqual(self.judge(self.report({"beta": beacons.DEAF}))
                         ["phase"], "clear")

    def test_busy_steward_is_not_down(self):
        self.assertEqual(self.judge(self.report({"alpha": beacons.BUSY}))
                         ["phase"], "clear")

    def test_hostile_roster_key_stays_internal_to_the_episode(self):  # noqa: VACUOUS_ASSERTION — the down phase and the hostile key in the cohort prove the planted key was processed
        hostile = "beta\x1b[2J‮pwn"
        self.rows[hostile] = self.rows.pop("beta")
        got = self.judge(self.report({"alpha": beacons.DEAF}))
        self.assertEqual(got["phase"], "down")
        self.assertIn(hostile, self.state["cohort"])
        self.assertNotIn(hostile, got["why"])
        self.assertNotIn("\x1b", got["why"])
        self.assertNotIn("‮", got["why"])

    def test_majority_requires_two_down_and_full_current_report(self):
        got = self.judge(self.report({"beta": beacons.DEAF,
                                      "gamma": beacons.DEAF}))
        self.assertEqual(got["phase"], "down")
        self.assertIn("2 of 3 seats", got["why"])
        self.assertEqual(self.state["cohort"], ["alpha", "beta", "gamma"])
        self.state = dict(beacon_phone.CLEAR)
        # One out of one is not a fleet majority, even with no steward.
        self.rows = {"beta": self.row()}
        with mock.patch.dict(os.environ, {"HELM_STEWARD_SEAT": "typo"}):
            self.assertEqual(self.judge(self.report({"beta": beacons.DEAF}))
                             ["phase"], "clear")

    def test_graveyard_roster_rows_do_not_prevent_full_census(self):
        for i in range(90):
            self.rows["retired-%d" % i] = self.row(
                covered=NOW - beacons.ATTEND_GRACE_S - 100)
            self.rows["retired-%d" % i]["attendance"]["at"] = NOW - 100
        rep = self.report({"beta": beacons.DEAF, "gamma": beacons.DEAF},
                          seats=["alpha", "beta", "gamma"])
        self.assertEqual(self.judge(rep)["phase"], "down")
        self.assertEqual(self.state["cohort"], ["alpha", "beta", "gamma"])
        self.heard()
        self.assertEqual(self.judge(self.report(
            seats=["alpha", "beta", "gamma"]))["phase"], "clear")
        self.assertEqual(self.state, beacon_phone.CLEAR)

    def test_partial_or_stale_down_is_not_established(self):
        rep = self.report({"alpha": beacons.DEAF}, seats=["alpha"])
        got = self.judge(rep)
        self.assertEqual(got["phase"], "unknown")
        self.assertEqual(got["why"], "the beacons census lists 1 of 3 seats")
        self.assertTrue(got["short"])
        self.assertTrue(got["looks_down"])
        self.assertEqual(self.judge(rep, complete=False)["phase"], "unknown")
        rep = self.report({"alpha": beacons.DEAF})
        self.rows["beta"]["attendance"]["at"] = NOW - 10
        self.save()
        self.assertEqual(self.judge(rep)["phase"], "unknown")
        self.assertEqual(self.state, beacon_phone.CLEAR)

    def test_a_census_whose_probes_did_not_all_run_is_not_whole(self):
        rep = self.report({"alpha": beacons.DEAF})
        self.assertTrue(beacon_phone.complete(rep))
        for probe in ("live_probe", "agent_probe"):
            with self.subTest(probe=probe):
                blind = dict(rep, **{probe: False})
                self.assertFalse(beacon_phone.complete(blind))
                self.record(blind)
                got = beacon_phone.fleet(NOW, beacon_phone.CLEAR)
                self.assertEqual(got["phase"], "unknown")
                self.assertIn("probes", got["why"])

    def test_unknown_steward_never_uses_a_guess(self):
        with mock.patch.dict(os.environ, {"HELM_STEWARD_SEAT": "typo"}):
            self.assertEqual(self.judge(self.report({"alpha": beacons.DEAF}))
                             ["phase"], "clear")

    def test_unproven_without_live_beacon_counts_as_unreachable(self):
        self.assertEqual(self.judge(self.report({"alpha": beacons.UNPROVEN}))
                         ["phase"], "down")

    def test_resting_and_uncovered_seats_are_out_of_majority(self):
        self.rows["gamma"]["attendance"]["covered"] = "junk"
        rep = self.report({"beta": beacons.DEAF, "gamma": beacons.DEAF})
        self.assertEqual(self.judge(rep)["phase"], "clear")
        rep = self.report({"gamma": beacons.RESTING, "beta": beacons.DEAF})
        self.assertEqual(self.judge(rep)["phase"], "clear")

    def test_a_resting_seat_does_not_hold_off_a_majority(self):
        self.rows["delta"] = self.row()
        self.rows["epsilon"] = self.row()
        got = self.judge(self.report({"beta": beacons.DEAF,
                                      "gamma": beacons.DEAF,
                                      "delta": beacons.DEAF,
                                      "epsilon": beacons.RESTING}))
        self.assertEqual(got["phase"], "down")
        self.assertIn("3 of 4 seats", got["why"])

    def test_clear_only_after_complete_full_original_cohort_covered(self):
        self.assertEqual(self.judge(self.report({"alpha": beacons.DEAF}))
                         ["phase"], "down")
        self.heard()
        partial = self.report(seats=["alpha"])
        self.assertEqual(self.judge(partial)["phase"], "unknown")
        self.assertEqual(self.judge(partial, complete=False)["phase"],
                         "unknown")
        self.assertEqual(self.state["phase"], "down", "unknown keeps it")
        self.assertEqual(self.judge(self.report({"beta": beacons.DEAF}))
                         ["phase"], "down")
        got = self.judge(self.report())
        self.assertEqual(got["phase"], "clear")
        self.assertEqual(self.state, beacon_phone.CLEAR)

    def test_denominator_shrink_cannot_manufacture_clear(self):
        self.judge(self.report({"alpha": beacons.DEAF}))
        self.heard()
        self.rows.pop("beta")
        got = self.judge(self.report())
        self.assertEqual(got["phase"], "down")
        self.assertIn("1 of the episode's seats is not covered", got["why"])
        self.assertEqual(self.state["cohort"], ["alpha", "beta", "gamma"])

    def test_a_resting_cohort_seat_does_not_hold_a_heard_episode_open(self):
        """The owner rests a cohort seat after the outage: the census READS it
        RESTING (an intent, not an absence), so it owes no recovery and the
        episode ends once the rest of its cohort covers. The control: the
        same seat still DEAF keeps the episode down."""
        self.judge(self.report({"alpha": beacons.DEAF}))
        self.heard()
        got = self.judge(self.report({"gamma": beacons.DEAF}))
        self.assertEqual(got["phase"], "down", "control: a DEAF seat owes")
        self.assertIn("1 of the episode's seats", got["why"])
        got = self.judge(self.report({"gamma": beacons.RESTING}))
        self.assertEqual(got["phase"], "clear", got["why"])
        self.assertEqual(self.state, beacon_phone.CLEAR)

    def test_an_episode_the_phone_never_heard_ends_with_the_outage(self):
        self.judge(self.report({"alpha": beacons.DEAF}))
        self.assertFalse(self.state["delivered"])
        self.rows.pop("beta")                # not the whole cohort covers
        got = self.judge(self.report())
        self.assertEqual(got["phase"], "clear")
        self.assertIn("before the phone heard it", got["why"])
        self.assertEqual(self.state, beacon_phone.CLEAR)

    def test_a_reoutage_before_the_all_clear_keeps_the_episode_down(self):
        self.judge(self.report({"alpha": beacons.DEAF}))
        self.heard()
        cohort = self.state["cohort"]
        self.assertEqual(self.judge(self.report({"alpha": beacons.DEAF}))
                         ["phase"], "down")
        self.assertEqual(self.state["cohort"], cohort)
        self.assertTrue(self.state["delivered"])

    def test_dead_before_episode_excluded_from_cohort(self):
        """RED (finding 1): a seat already DEAD when the alarm tripped is not
        part of the fleet-down, so it must not stay in the frozen cohort and
        hold the episode DOWN forever. A seat whose DEAF spell began before
        the episode is dropped from the cohort, so recovery can proceed once
        the living seats cover. The just-fallen steward (fell within the
        episode) and a reachable seat stay; a seat dead three hours earlier
        is excluded."""
        # beta has been DEAF for three hours — before this episode — when the
        # alarm trips on alpha (the steward).
        self.rows["beta"]["attendance"].update(
            state=beacons.DEAF, alarm=True, since=NOW - 3 * 3600)
        rep = self.report({"alpha": beacons.DEAF, "beta": beacons.DEAF})
        self.assertEqual(self.judge(rep)["phase"], "down")
        # beta is dead before the episode, so it is not in the frozen cohort;
        # alpha (just-fallen steward) and gamma (reachable) are.
        self.assertEqual(self.state["cohort"], ["alpha", "gamma"])
        self.heard()
        # alpha and gamma cover; beta stays DEAF but is excluded, so the
        # episode recovers instead of being stuck DOWN.
        self.judge(self.report({"alpha": beacons.COVERED,
                                "gamma": beacons.COVERED},
                               seats=["alpha", "gamma"]))
        self.assertEqual(self.state, beacon_phone.CLEAR)

    def test_staggered_outage_over_30_min_still_trips(self):
        """RED (finding 1, round 2): a slow fleet death — seats wall one at a
        time across 30 minutes — must still page. The trip is an instant
        majority, not a window, so a seat already DEAF for 30 min stays on the
        roll and tips it, even though that seat has been DEAF for longer than
        REARM_GRACE_S."""
        # beta has been DEAF for 30 min; gamma fell 15 min ago; alpha (the
        # steward) is reachable. The two DEAF seats are a majority of three.
        self.rows["beta"]["attendance"].update(
            state=beacons.DEAF, alarm=True, since=NOW - 30 * 60)
        self.rows["gamma"]["attendance"].update(
            state=beacons.DEAF, alarm=True, since=NOW - 15 * 60)
        rep = self.report({"beta": beacons.DEAF, "gamma": beacons.DEAF})
        self.assertEqual(self.judge(rep)["phase"], "down")

    def test_census_whole_20_min_into_outage_still_trips(self):
        """RED (finding 1, round 2): the whole majority went DEAF at NOW-20min;
        only now does the census become whole (the chat node was down until
        then). That first whole census must trip, not be swallowed by
        detection lag."""
        for name in ("alpha", "beta", "gamma"):
            self.rows[name]["attendance"].update(
                state=beacons.DEAF, alarm=True, since=NOW - 20 * 60)
        rep = self.report({"alpha": beacons.DEAF, "beta": beacons.DEAF,
                           "gamma": beacons.DEAF})
        self.assertEqual(self.judge(rep)["phase"], "down")

    def test_new_after_cleared_opens_a_new_episode(self):
        """A fresh fleet outage after a cleared episode must open a new one,
        not be swallowed by the cleared latch (a reviewer's p4)."""
        self.judge(self.report({"alpha": beacons.DEAF}))
        self.heard()
        self.judge(self.report())
        self.assertEqual(self.state, beacon_phone.CLEAR)
        self.assertEqual(self.judge(self.report({"alpha": beacons.DEAF}))
                         ["phase"], "down")
        self.assertFalse(self.state["delivered"])

    def test_phantom_non_roster_census_row_is_ignored_not_refused(self):
        """RED (finding 2): a withdrawn seat that keeps re-arming a beacon
        hands the fleet a census row with no roster row; the census must
        ignore that stray seat and still qualify the roster majority, not
        refuse the whole census as identity-unreadable."""
        rep = self.report({"alpha": beacons.DEAF},
                          seats=["alpha", "beta", "gamma", "ghost"])
        self.assertEqual(self.judge(rep)["phase"], "down",
                         "phantom %r must not hide the roster majority" %
                         "ghost")

    def test_corrupt_roster_and_latch_fail_closed(self):
        """An unreadable roster raises (unreadable is not clear), and an
        unreadable latch is refused, never read as no episode."""
        rep = self.report({"alpha": beacons.DEAF})
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{broken")
        self.record(rep)
        with self.assertRaises(ValueError):
            beacon_phone.fleet(NOW, beacon_phone.CLEAR)
        latch = beacon_phone.latch_path()
        self.assertEqual(latch, self.path + ".phone.json")
        self.assertEqual(beacon_phone._latch(latch), beacon_phone.CLEAR,
                         "control: no latch file is no episode")
        with open(latch, "w", encoding="utf-8") as f:
            f.write("{broken")
        with self.assertRaises(ValueError):
            beacon_phone._latch(latch)
        for bad in ({"phase": "down", "cohort": []},
                    {"phase": "clear", "extra": 1}, ["down"]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                beacon_phone.episode(bad)


    def test_a_stale_missing_or_unreadable_census_is_unknown_and_says_so(self):
        """The recorded census speaks for now only within two beacons passes
        (STALE_S, from beacons.INTERVAL_S); past that, or absent, or
        unreadable, it establishes nothing and keeps the episode."""
        self.assertEqual(beacon_phone.STALE_S, 2 * beacons.INTERVAL_S)
        down = self.report({"alpha": beacons.DEAF})
        self.assertEqual(self.judge(down)["phase"], "down",
                         "control: the same census, fresh, is down")
        opened = dict(self.state)
        self.record(down, at=NOW - beacon_phone.STALE_S - 120)
        got = beacon_phone.fleet(NOW, opened)
        self.assertEqual((got["phase"], got["stale"]), ("unknown", True))
        self.assertIn("is 12 min old", got["why"])
        self.assertEqual(got["latch"], opened, "a stale census keeps it")
        os.unlink(beacons.attended_path(self.path))
        got = beacon_phone.fleet(NOW, opened)
        self.assertEqual(got["phase"], "unknown")
        self.assertIn("no beacons census is recorded", got["why"])
        self.assertEqual(beacon_phone.fleet(NOW)["phase"], "unknown",
                         "a register with rows and no census is named")
        for row in self.rows.values():
            row.pop("attendance")
        self.save()
        self.assertEqual(beacon_phone.fleet(NOW)["phase"], "clear",
                         "no seat has attended: no roll to be down")
        self.assertEqual(beacon_phone.fleet(NOW, opened)["phase"], "unknown",
                         "an open episode is never cleared by an absence")
        self.rows = {name: self.row() for name in ("alpha", "beta", "gamma")}
        self.save()
        with open(beacons.attended_path(self.path), "w",
                  encoding="utf-8") as f:
            f.write("{broken")
        got = beacon_phone.fleet(NOW, opened)
        self.assertEqual(got["phase"], "unknown")
        self.assertIn("could not be read", got["why"])


if __name__ == "__main__":
    unittest.main()
