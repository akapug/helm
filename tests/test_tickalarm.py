#!/usr/bin/env python3
"""A tick leg that keeps failing is heard (task/4189).

The founding incident: the dark-seat mover raised AttributeError on every
idle-dispatch pass for three days (944 journal lines), and `helm doctor` read
its on/off switch, not its result. Every arm here runs
under a temp HELM_HOME and chat directory; the room and the phone are
injected callables or patched module seams, so nothing reaches the fleet.
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from tests import _tmphome           # noqa: F401 — must precede helm.*
from helm import doctor, home, localnames, tickalarm

NOW = 1790000000.0
BOOM = "AttributeError: 'NoneType' object has no attribute 'get'"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-tickalarm-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = mock.patch.dict(os.environ, {
            "HELM_HOME": os.path.join(self.tmp, "home"),
            "HELM_CHAT_DIR": os.path.join(self.tmp, "chat"),
            "HELM_NTFY_TOPIC": "", "MELD_NTFY_TOPIC": "",
            tickalarm.SWITCH_ENV: "on"})
        env.start()
        self.addCleanup(env.stop)
        os.makedirs(home.global_dir(), exist_ok=True)
        with open(os.path.join(home.global_dir(), localnames.CONFIG), "w",
                  encoding="utf-8") as f:
            json.dump({"tick-steward-seat": "floor-claude"}, f)
        localnames._cache["stat"] = None
        self.path = os.path.join(self.tmp, "tick-legs.json")
        self.rows, self.pushes = [], []

    def post(self, text, event_id):
        self.rows.append((text, event_id))

    def push(self, body, title):
        self.pushes.append((body, title))
        return True

    def rec(self, error=None, at=0.0, leg="darkmove"):
        return tickalarm.record(leg, error, now=NOW + at, path=self.path,
                                post=self.post, push=self.push)

    def fail(self, n, start=0.0, step=60.0, leg="darkmove"):
        for i in range(n):
            self.rec(BOOM, at=start + i * step, leg=leg)


class OncePerEpisode(Base):
    def test_three_failures_post_one_row_naming_leg_error_count_owner(self):
        self.fail(2)
        self.assertEqual(self.rows, [])
        self.rec(BOOM, at=120)
        self.assertEqual(len(self.rows), 1)
        text = self.rows[0][0]
        for want in ("darkmove", BOOM, "3 passes in a row",
                     "@floor-claude"):
            self.assertIn(want, text)

    def test_a_fourth_failure_does_not_post_again(self):
        self.fail(10)
        self.assertEqual(len(self.rows), 1)
        got = tickalarm.results(path=self.path)["darkmove"]
        self.assertEqual(got["failures"], 10)
        self.assertFalse(got["ok"])
        self.assertEqual(got["error"], BOOM)

    def test_a_success_re_arms_the_alarm(self):
        self.fail(3)
        self.rec(None, at=600)
        got = tickalarm.results(path=self.path)["darkmove"]
        self.assertTrue(got["ok"])
        self.assertEqual(got["failures"], 0)
        self.fail(2, start=700)
        self.assertEqual(len(self.rows), 1)
        self.fail(1, start=900)
        self.assertEqual(len(self.rows), 2)
        self.assertNotEqual(self.rows[0][1], self.rows[1][1])

    def test_two_failures_then_a_success_never_post(self):
        self.fail(2)
        self.rec(None, at=300)
        self.fail(2, start=400)
        self.assertEqual(self.rows, [])
        self.assertEqual(
            tickalarm.results(path=self.path)["darkmove"]["failures"], 2)

    def test_legs_count_apart(self):
        self.fail(2, leg="darkmove")
        self.fail(2, leg="gc")
        self.assertEqual(self.rows, [])
        got = tickalarm.results(path=self.path)
        self.assertEqual((got["darkmove"]["failures"], got["gc"]["failures"]),
                         (2, 2))


class OwnerPush(Base):
    def test_one_push_only_after_an_hour_of_alarmed_failure(self):
        self.fail(3)                              # alarmed at +120s
        self.rec(BOOM, at=120 + 3500)
        self.assertEqual(self.pushes, [])
        self.rec(BOOM, at=120 + 3700)
        self.assertEqual(len(self.pushes), 1)
        self.assertIn("darkmove", self.pushes[0][0])
        self.rec(BOOM, at=120 + 7400)
        self.assertEqual(len(self.pushes), 1)

    def test_a_recovery_before_the_hour_pushes_nothing(self):
        self.fail(3)
        self.rec(None, at=600)
        self.rec(BOOM, at=5000)
        self.assertEqual(self.pushes, [])
        self.assertEqual(len(self.rows), 1)
        self.assertEqual(
            tickalarm.results(path=self.path)["darkmove"]["failures"], 1)

    def test_a_failed_push_stays_owed_and_is_sent_once_later(self):
        self.fail(3)
        self.push = lambda body, title: False
        self.rec(BOOM, at=4000)
        self.push = Base.push.__get__(self)
        self.rec(BOOM, at=4100)
        self.rec(BOOM, at=4200)
        self.assertEqual(len(self.pushes), 1)


class NeverRaisesNeverSpams(Base):
    def test_a_room_that_refuses_keeps_the_row_owed_and_never_raises(self):
        def refuse(text, event_id):
            raise OSError("chat down")
        self.post = refuse
        self.fail(5)
        self.post = Base.post.__get__(self)
        self.assertEqual(self.rows, [])
        self.fail(3, start=1000)
        self.assertEqual(len(self.rows), 1)

    def test_a_raising_phone_never_raises(self):
        self.fail(3)

        def boom(body, title):
            raise RuntimeError("ntfy down")
        self.push = boom
        self.assertEqual(self.rec(BOOM, at=9000), [])     # must not raise
        got = tickalarm.results(path=self.path)["darkmove"]
        self.assertEqual((got["failures"], got["pushed"]), (4, None))

    def test_an_unwritable_state_file_never_raises(self):
        blocker = os.path.join(self.tmp, "file")
        with open(blocker, "w") as f:
            f.write("x")
        self.path = os.path.join(blocker, "tick-legs.json")
        for i in range(5):
            lines = self.rec(BOOM, at=i)
            self.assertEqual(len(lines), 1)
            self.assertIn("not recorded", lines[0])
        self.assertEqual(self.rows, [])

    def test_a_corrupt_state_file_starts_fresh(self):
        with open(self.path, "w") as f:
            f.write("{not json")
        self.fail(3)
        self.assertEqual(len(self.rows), 1)

    def test_a_huge_error_is_bounded(self):
        self.rec("x" * 50000)
        got = tickalarm.results(path=self.path)["darkmove"]
        self.assertLessEqual(len(got["error"]), tickalarm.ERROR_MAX)
        self.assertTrue(got["error"].startswith("xxx"))

    def test_the_default_room_leg_uses_the_seats_room_and_an_event_id(self):
        calls = []
        with mock.patch("helm.chat.post",
                        lambda text, **kw: calls.append((text, kw))):
            for i in range(4):
                tickalarm.record("darkmove", BOOM, now=NOW + i,
                                 path=self.path, push=self.push)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["room"], "seats")
        self.assertTrue(calls[0][1]["event_id"].startswith("tickalarm:"))


class SwitchAndCost(Base):
    def test_the_suite_plants_the_switch_off(self):
        from tests import PLANTED
        self.assertEqual(PLANTED[tickalarm.SWITCH_ENV], "off")

    def test_off_records_nothing_sends_nothing_and_doctor_says_so(self):  # noqa: VACUOUS_ASSERTION — the doctor row naming the switch is the positive control on the same pass
        with mock.patch.dict(os.environ, {tickalarm.SWITCH_ENV: "off"}):
            self.fail(5)
            with mock.patch.object(tickalarm, "state_path",
                                   lambda: self.path):
                rows = doctor.check_tick_legs()
        self.assertEqual(self.rows, [])
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual([lvl for lvl, _t in rows], [doctor.WARN])
        self.assertIn("off", rows[0][1])

    def test_a_working_pass_already_recorded_takes_no_lock(self):
        self.rec(None)
        with mock.patch.object(tickalarm.fcntl, "flock") as flock:
            self.rec(None, at=60)
            self.assertEqual(flock.call_count, 0)
            self.rec(None, at=tickalarm.REFRESH_S + 1)
            self.assertGreater(flock.call_count, 0)
        self.rec(BOOM, at=tickalarm.REFRESH_S + 2)
        self.assertFalse(tickalarm.results(path=self.path)["darkmove"]["ok"])


class Watch(Base):
    def test_an_exception_is_recorded_and_raised(self):
        def die():
            raise ValueError("bad")
        with mock.patch.object(tickalarm, "state_path",
                               lambda: self.path):
            with self.assertRaises(ValueError):
                tickalarm.watch("gc", die)
        got = tickalarm.results(path=self.path)["gc"]
        self.assertFalse(got["ok"])
        self.assertIn("ValueError: bad", got["error"])

    def test_a_failed_exit_code_is_a_failure_and_a_clean_one_is_ok(self):
        with mock.patch.object(tickalarm, "state_path",
                               lambda: self.path):
            self.assertEqual(tickalarm.watch(
                "beacons", lambda: 2, failed=lambda rc: rc == 2), 2)
            self.assertFalse(tickalarm.results(path=self.path)
                             ["beacons"]["ok"])
            self.assertEqual(tickalarm.watch(
                "beacons", lambda: 1, failed=lambda rc: rc == 2), 1)
            self.assertTrue(tickalarm.results(path=self.path)
                            ["beacons"]["ok"])


class TimerEntries(Base):
    """The timer entries are recorded at `helm`'s one door, from one table."""

    def test_the_table_picks_each_timer_argv_and_nothing_else(self):  # noqa: VACUOUS_ASSERTION — the first loop pins each timer argv's leg before the None arms
        want = {("proxywatch", ("--post",)): "proxywatch",
                ("proxywatch", ("--dark-only", "--post")): "proxywatch-dark",
                ("gc", ("--apply",)): "gc",
                ("train", ("auto", "--apply", "--repo", "/r")): "autoland",
                ("beacons", ("--post",)): "beacons"}
        for (verb, rest), leg in want.items():
            self.assertEqual(tickalarm.timer_leg(verb, rest)[0], leg)
        for verb, rest in (("proxywatch", ()), ("gc", ("--dry",)),
                           ("gc", ("--apply", "--install-timer")),
                           ("train", ("auto", "--status")),
                           ("train", ("veto", "--apply")),
                           ("beacons", ("--post", "--help")),
                           ("proxywatch", ("vendor-reset", "show")),
                           ("seat", ("idle-dispatch",))):
            self.assertIsNone(tickalarm.timer_leg(verb, rest), (verb, rest))

    def test_exit_two_fails_a_watchdog_and_exit_one_does_not(self):
        failed = tickalarm.timer_leg("beacons", ["--post"])[1]
        self.assertTrue(failed(2))
        self.assertFalse(failed(1))
        self.assertIsNone(tickalarm.timer_leg("gc", ["--apply"])[1])

    def test_the_door_records_a_raising_timer_pass_and_alarms_once(self):
        from helm import cli
        posted = []

        def broken(rest):
            raise RuntimeError("gc broke")
        with mock.patch.object(tickalarm, "state_path", lambda: self.path), \
                mock.patch.object(tickalarm, "_post",
                                  lambda text, eid: posted.append(text)), \
                mock.patch.dict(cli.VERBS, {"gc": broken}):
            for _ in range(4):
                with self.assertRaises(RuntimeError):
                    cli._main(["gc", "--apply"])
        self.assertEqual(len(posted), 1)
        self.assertIn("RuntimeError: gc broke", posted[0])

    def test_the_door_leaves_other_verbs_unrecorded(self):  # noqa: VACUOUS_ASSERTION — the --apply arm after it records an ok gc on the same file
        from helm import cli
        with mock.patch.object(tickalarm, "state_path", lambda: self.path), \
                mock.patch.dict(cli.VERBS, {"gc": lambda rest: 0}):
            self.assertEqual(cli._main(["gc", "--dry"]), 0)
            self.assertEqual(tickalarm.results(path=self.path), {})
            self.assertEqual(cli._main(["gc", "--apply"]), 0)
            self.assertTrue(tickalarm.results(path=self.path)["gc"]["ok"])


class DarkMoverWired(Base):
    """The founding leg: darkmove.run's own catch now feeds the alarm."""

    def test_three_raising_passes_post_once(self):
        from helm import darkmove
        posted = []
        with mock.patch.object(tickalarm, "state_path", lambda: self.path), \
                mock.patch.object(tickalarm, "_post",
                                  lambda text, eid: posted.append(text)), \
                mock.patch.object(darkmove, "_run",
                                  side_effect=AttributeError("x")):
            for _ in range(4):
                lines, moves = darkmove.run(apply=False, post=False)
                self.assertIn("stopped (AttributeError", lines[0])
        self.assertEqual(len(posted), 1)
        self.assertIn("darkmove", posted[0])


class Doctor(Base):
    def check(self):
        with mock.patch.object(tickalarm, "state_path", lambda: self.path):
            return doctor.check_tick_legs()

    def test_a_failing_leg_is_a_doctor_failure(self):
        self.fail(3)
        rows = self.check()
        bad = [text for level, text in rows if level == doctor.FAIL]
        self.assertEqual(len(bad), 1)
        self.assertIn("darkmove", bad[0])
        self.assertIn(BOOM, bad[0])

    def test_one_failure_warns_and_an_ok_leg_is_ok(self):
        self.rec(None, leg="gc")
        self.rec(BOOM, leg="darkmove")
        levels = {level for level, _text in self.check()}
        self.assertEqual(levels, {doctor.WARN, doctor.OK})
        self.assertNotIn(doctor.FAIL, levels)

    def test_no_record_yet_is_ok(self):
        self.assertEqual([lvl for lvl, _t in self.check()], [doctor.OK])

    def test_the_check_is_registered(self):
        self.assertIn("check_tick_legs", doctor.CHECKS)


if __name__ == "__main__":
    unittest.main()
