#!/usr/bin/env python3
"""A SEAT RUNNING BEHIND ITS OWN CONFIGURATION IS A FINDING, NOT A SILENCE.

task/2885. The defect is a COMPOSITION: the setting is right, the install is
right, the launcher works, and the seat still does not have the plugin —
because it resolved its plugins when it started and the setting was written
afterwards. Every check helm owned read the FILES and reported healthy.

THE LOAD-BEARING ARM IS THE ONE WITH TWO POLES.
`test_the_config_vs_config_axis_calls_the_measured_world_healthy` plants the
world that was actually measured — a real settings.json enabling a plugin, a
real install record for it, a real installed directory — and proves
`physics._claude_plugins` issues NO warning about it. The very next arm plants
THE SAME FILES and proves `seatstale` names the seat. Either pole alone proves
nothing: the first alone is a check passing, the second alone is a check
firing, and only together do they say the new axis sees what the old one is
structurally blind to. Remove the cure and the second arm goes red while the
first stays green, which is the shape of the defect itself.

WHAT IS AND IS NOT INJECTED. The settings files are REAL files with real
mtimes, and the physics reader is the real one. Only the PROCESS AGE is
supplied, because a test cannot own a three-day-old process — and every arm
that supplies it also supplies `now` and the census, so no verdict here is a
property of the box that ran it.
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from helm import doctor, physics, seatstale

PLUGIN = "playwright@claude-plugins-official"
DAY = 86400


def levels(results, level):
    return [msg for lvl, msg in results if lvl == level]


def row(pid=4242, seat="seat-a", root="/root", child=False,
        reason=None):
    """One census row with exactly the keys `seatstale.state` contracts to
    read. Spelled out rather than built from the live census, so an arm below
    never consumes host pressure."""
    return {"pid": pid, "root": root, "child": child,
            "declared_reason": reason,
            "environ": {"HELM_CHAT_NAME": seat} if seat else {}}


def census(*rows, **flags):
    out = {"rows": list(rows), "listing_failed": False, "who_failed": False,
           "census_partial": False}
    out.update(flags)
    return out


def ages(table=None):
    """A process-age seam: {pid: seconds}. An unlisted pid answers None, which
    is the real reader's "cannot tell"."""
    table = table or {}
    return lambda pid: table.get(pid)


def stats(table=None):
    """A stat seam keyed on BASENAME, answering (mtime, why). An unnamed file
    is ABSENT, which is the ordinary case for a home with no install record."""
    table = table or {}
    return lambda path: table.get(os.path.basename(path), (None, "absent"))


def settings_at(mtime, why=None):
    return stats({"settings.json": (mtime, why)})


class HomeFixture(unittest.TestCase):
    """A real config home: a real settings.json enabling a real plugin, a real
    install record, a real installed directory. Written to disk because the
    control arm runs the REAL physics reader over it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "credhome")
        self.installed = os.path.join(self.home, "plugins", "cache", "pw")
        os.makedirs(self.installed)
        self.settings = os.path.join(self.home, "settings.json")
        with open(self.settings, "w", encoding="utf-8") as fh:
            json.dump({"enabledPlugins": {PLUGIN: True}}, fh)
        self.record = os.path.join(self.home, "plugins",
                                   "installed_plugins.json")
        with open(self.record, "w", encoding="utf-8") as fh:
            json.dump({"plugins": {PLUGIN: [{"installPath": self.installed}]}},
                      fh)
        self.now = 1_700_000_000.0
        # The measured shape, as two ages against ONE now: the files were
        # written a day ago and the arms below run a four-day-old process
        # against them, so the seat is THREE DAYS behind its own settings.
        self.written = self.now - 1 * DAY
        for p in (self.settings, self.record):
            os.utime(p, (self.written, self.written))

    def layers(self):
        """The settings layers, spelled from THIS home only. Built by hand
        rather than by `_claude_settings_layers` so a managed-settings file on
        the host running the suite cannot reach the assertion."""
        with open(self.settings, encoding="utf-8") as fh:
            return [("user", self.settings, json.load(fh))]


class TheOldAxisIsBlindTest(HomeFixture):
    def test_the_config_vs_config_axis_calls_the_measured_world_healthy(self):
        """POLE ONE. `physics._claude_plugins` joins enabledPlugins against the
        home's install records. The plugin is enabled AND installed, so it has
        nothing to say — which is exactly the state that hid the defect."""
        summary, dirs, warnings = physics._claude_plugins(self.home,
                                                          self.layers())
        self.assertEqual(summary["enabled"], [PLUGIN])
        self.assertEqual([p for p, _d in dirs], [PLUGIN])
        self.assertEqual(warnings, [],
                         "the config-vs-config axis warned about a world it "
                         "is supposed to find healthy — the control rotted")

    def test_the_config_vs_running_axis_names_the_seat_in_the_same_world(self):
        """POLE TWO. The SAME files, plus the one fact the old axis never
        reads: the process is older than they are."""
        st = seatstale.state(
            census=census(row(pid=7, seat="seat-a", root=self.home)),
            now=self.now, age=ages({7: 4 * DAY}))
        self.assertTrue(st["read"])
        self.assertEqual([r["seat"] for r in st["stale"]], ["seat-a"])
        self.assertEqual(st["stale"][0]["behind_s"], 3 * DAY)
        # BOTH watched files are behind, and the report says which.
        self.assertEqual(sorted(rel for rel, _g in st["stale"][0]["files"]),
                         sorted(seatstale.WATCHED))

    def test_the_same_seat_started_after_the_write_is_not_named(self):
        """THE NEGATIVE POLE, in the same world. A check that fires on this
        fixture no matter the age is not measuring the age."""
        st = seatstale.state(
            census=census(row(pid=7, seat="seat-a", root=self.home)),
            now=self.now, age=ages({7: DAY // 2}))
        self.assertEqual(st["stale"], [])
        self.assertEqual(st["measured"], 1)


class MeasurementTest(unittest.TestCase):
    def test_a_write_inside_the_grace_is_not_drift(self):
        """A launch writes its own settings around the exec, so the two stamps
        straddle by seconds. That may never read as a stale seat."""
        inside = seatstale.state(
            census=census(row(pid=1)), now=1000.0, age=ages({1: 100.0}),
            stat=settings_at(1000.0 - 100.0 + seatstale.GRACE_S - 1))
        self.assertEqual(inside["stale"], [])
        outside = seatstale.state(
            census=census(row(pid=1)), now=1000.0, age=ages({1: 100.0}),
            stat=settings_at(1000.0 - 100.0 + seatstale.GRACE_S + 30))
        self.assertEqual([r["seat"] for r in outside["stale"]],
                         ["seat-a"])

    def test_an_absent_settings_file_is_not_drift(self):
        """A home that has no such file is not behind it. Absence is not a
        finding, and it is not a blindness either."""
        st = seatstale.state(census=census(row(pid=1)), now=1000.0,
                             age=ages({1: 900.0}), stat=stats())
        self.assertEqual(st["stale"], [])
        self.assertEqual(st["blind"], [])
        self.assertEqual(st["measured"], 1)

    def test_a_subagent_row_is_never_named(self):
        """A subagent inherits the seat's environ and dies inside one turn.
        Naming it would tell the owner to restart something that has no
        restart."""
        world = dict(now=1000.0, age=ages({1: 900.0}),
                     stat=settings_at(800.0))
        # POSITIVE CONTROL FIRST, on the same observable: the identical row
        # with `child` false IS named, so the empty result below is the
        # child flag doing work and not a fixture that measures nothing.
        parent = seatstale.state(census=census(row(pid=1)), **world)
        self.assertEqual([r["seat"] for r in parent["stale"]], ["seat-a"])
        st = seatstale.state(census=census(row(pid=1, child=True)), **world)
        self.assertEqual(st["stale"], [])
        self.assertEqual(st["unnamed_stale"], 0)
        self.assertEqual(st["measured"], 0)

    def test_a_process_with_no_seat_name_is_counted_and_not_named(self):
        st = seatstale.state(
            census=census(row(pid=1, seat=None)), now=1000.0,
            age=ages({1: 900.0}), stat=settings_at(800.0))
        self.assertEqual(st["stale"], [])
        self.assertEqual(st["unnamed_stale"], 1)

    def test_one_seat_with_two_processes_is_named_once_at_its_worst(self):
        """A relaunch can leave an older process wearing the same name. The
        owner restarts a SEAT, so two lines would read as two problems."""
        st = seatstale.state(
            census=census(row(pid=1, seat="seat-b"), row(pid=2, seat="seat-b")),
            now=10 * DAY, age=ages({1: 2 * DAY, 2: 9 * DAY}),
            stat=settings_at(10 * DAY - DAY))
        self.assertEqual([r["seat"] for r in st["stale"]], ["seat-b"])
        self.assertEqual(st["stale"][0]["behind_s"], 8 * DAY)
        self.assertEqual(st["stale"][0]["pid"], 2)

    def test_the_worst_seat_is_reported_first(self):
        st = seatstale.state(
            census=census(row(pid=1, seat="a"), row(pid=2, seat="b")),
            now=10 * DAY, age=ages({1: 3 * DAY, 2: 9 * DAY}),
            stat=settings_at(10 * DAY - DAY))
        self.assertEqual([r["seat"] for r in st["stale"]], ["b", "a"])


class BlindnessIsNotHealthTest(unittest.TestCase):
    """Every unreadable input produces a NAMED unknown. A row that could not be
    decided and was dropped would read as a clean seat, which is the exact
    collapse this module exists to end."""

    def test_a_failed_enumeration_never_reports_a_current_fleet(self):
        st = seatstale.state(census=census(listing_failed=True), now=1000.0,
                             age=ages(), stat=stats())
        self.assertFalse(st["read"])
        self.assertIn("failed", st["why"])
        self.assertEqual(st["stale"], [])
        results = self._doctor(st)
        self.assertEqual(levels(results, doctor.OK), [])
        self.assertTrue(any("UNKNOWN" in m
                            for m in levels(results, doctor.WARN)), results)

    def test_a_partial_census_qualifies_the_report(self):
        st = seatstale.state(census=census(row(pid=1), census_partial=True),
                             now=1000.0, age=ages({1: 10.0}), stat=stats())
        self.assertTrue(st["partial"])
        self.assertTrue(any("FLOOR" in m
                            for m in levels(self._doctor(st), doctor.WARN)))

    def test_an_untrusted_config_root_is_blind_not_clean(self):
        st = seatstale.state(
            census=census(row(pid=1, root=None, reason="config-untrusted")),
            now=1000.0, age=ages({1: 900.0}), stat=stats())
        self.assertEqual(st["stale"], [])
        self.assertEqual(st["measured"], 0)
        self.assertEqual(len(st["blind"]), 1)
        self.assertIn("config-untrusted", st["blind"][0]["why"])

    def test_an_unreadable_start_time_is_blind_not_clean(self):
        st = seatstale.state(census=census(row(pid=1)), now=1000.0,
                             age=ages(), stat=stats())
        self.assertEqual(st["measured"], 0)
        self.assertIn("start time", st["blind"][0]["why"])

    def test_an_unreadable_settings_file_is_blind_not_clean(self):
        st = seatstale.state(
            census=census(row(pid=1)), now=1000.0, age=ages({1: 900.0}),
            stat=stats({"settings.json": (None, "unreadable (PermissionError)")}))
        self.assertEqual(st["measured"], 0)
        self.assertEqual(st["stale"], [])
        self.assertIn("unreadable", st["blind"][0]["why"])

    def test_a_future_mtime_is_blind_not_an_enormous_drift(self):
        """A clock that went forward would otherwise mint a drift of whatever
        the skew is, and a made-up number is more expensive than an unknown."""
        st = seatstale.state(
            census=census(row(pid=1)), now=1000.0, age=ages({1: 10.0}),
            stat=settings_at(1000.0 + 10 * DAY))
        self.assertEqual(st["stale"], [])
        self.assertIn("future", st["blind"][0]["why"])

    def _doctor(self, st):
        with mock.patch.object(seatstale, "state", lambda **_k: st):
            return doctor.check_seat_physics_currency()


class OwnerSurfaceTest(unittest.TestCase):
    """The remedy is read by someone who has never typed a helm verb and does
    not open a terminal unless told to."""

    def _report(self, st):
        with mock.patch.object(seatstale, "state", lambda **_k: st):
            return doctor.check_seat_physics_currency()

    def stale(self, count):
        return seatstale.state(
            census=census(*[row(pid=i, seat="seat-%d" % i)
                            for i in range(1, count + 1)]),
            now=10 * DAY,
            age=ages({i: (2 + i) * DAY for i in range(1, count + 1)}),
            stat=settings_at(10 * DAY - DAY))

    def test_the_remedy_names_the_seat_and_asks_for_a_restart(self):
        msgs = levels(self._report(self.stale(1)), doctor.WARN)
        self.assertEqual(len(msgs), 1, msgs)
        self.assertIn("seat-1", msgs[0])
        self.assertIn("RESTARTED", msgs[0])

    def test_the_remedy_asks_for_no_command_and_no_terminal(self):
        """A command he would have to run is not a remedy he can act on. This
        pins the absence: a later edit that helpfully adds one turns it red."""
        msgs = levels(self._report(self.stale(1)), doctor.WARN)
        # The line EXISTS and says the one thing it must, so the absences
        # below are read off a real remedy rather than off an empty string.
        self.assertEqual(len(msgs), 1, msgs)
        self.assertIn("NEEDS TO BE RESTARTED", msgs[0])
        for banned in ("`", "$ ", "helm ", "--", "kill", "pkill"):
            self.assertNotIn(banned, msgs[0],
                             "the owner remedy carries %r" % banned)

    def test_naming_is_capped_and_the_cut_says_so(self):
        over = seatstale.NAME_CAP + 2
        msgs = levels(self._report(self.stale(over)), doctor.WARN)
        named = [m for m in msgs if "IS RUNNING ON OLD SETTINGS" in m]
        self.assertEqual(len(named), seatstale.NAME_CAP)
        self.assertTrue(any("2 further seat(s)" in m for m in msgs), msgs)

    def test_a_current_fleet_reports_ok_and_says_what_it_measured(self):
        st = seatstale.state(census=census(row(pid=1)), now=1000.0,
                             age=ages({1: 10.0}), stat=stats())
        msgs = levels(self._report(st), doctor.OK)
        self.assertEqual(len(msgs), 1, msgs)
        self.assertIn("1 seat(s)", msgs[0])
        # CONTROL ON THE EMPTY OBSERVABLE: this reporter DOES emit WARN rows,
        # proven on a stale world, so the empty WARN list is this fleet being
        # current and not a surface that never warns.
        self.assertTrue(levels(self._report(self.stale(1)), doctor.WARN))
        self.assertEqual(levels(self._report(st), doctor.WARN), [])

    def test_an_unnamed_stale_process_is_reported_without_a_pid_remedy(self):
        st = seatstale.state(
            census=census(row(pid=1, seat=None)), now=1000.0,
            age=ages({1: 900.0}), stat=settings_at(800.0))
        msgs = levels(self._report(st), doctor.WARN)
        self.assertTrue(any("could not read a seat name" in m for m in msgs),
                        msgs)
        # CONTROL ON THE EMPTY OBSERVABLE: the reporter DOES emit an OK row
        # when nothing is behind, so its absence here is the unnamed process
        # withholding the all-clear rather than a surface with no OK arm.
        current = seatstale.state(census=census(row(pid=1)), now=1000.0,
                                  age=ages({1: 10.0}), stat=stats())
        self.assertTrue(levels(self._report(current), doctor.OK))
        self.assertEqual(levels(self._report(st), doctor.OK), [])

    def test_the_duration_reads_as_english(self):
        self.assertEqual(seatstale.span(3 * DAY + 5), "3 days")
        self.assertEqual(seatstale.span(DAY), "1 day")
        self.assertEqual(seatstale.span(3600 * 5), "5 hours")
        self.assertEqual(seatstale.span(90), "1 minute")
        self.assertEqual(seatstale.span(-4), "0 seconds")


class RegistrationTest(unittest.TestCase):
    def test_the_check_runs_inside_helm_doctor(self):
        """An unregistered check is a function nobody calls — the defect this
        row exists for was invisible for exactly that kind of reason."""
        self.assertIn("check_seat_physics_currency", doctor.CHECKS)
        self.assertTrue(callable(
            getattr(doctor, "check_seat_physics_currency")))

    def test_the_live_check_answers_without_raising(self):
        """It reads the real census on the box running the suite, so it asserts
        the SHAPE and never the estate: every row is a (level, message) pair
        with a level doctor knows."""
        results = doctor.check_seat_physics_currency()
        self.assertTrue(results)
        for lvl, msg in results:
            self.assertIn(lvl, (doctor.OK, doctor.WARN, doctor.FAIL))
            self.assertTrue(msg.strip())


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# THE MEMORY BASE: a session that began before its home carried the variable
# ---------------------------------------------------------------------------

LINKED = "/homes/linked-fixture"
BASE = "/shared/.claude"
VAR = "CLAUDE_CODE_REMOTE_MEMORY_DIR"
SID = "aaaaaaaa-1111-2222-3333-444444444444"


def mrow(pid=4242, seat="seat-a", root=LINKED, env=None, start="1000",
         child=False):
    environ = dict({"HELM_CHAT_NAME": seat} if seat else {}, **(env or {}))
    return {"pid": pid, "root": root, "child": child, "start": start,
            "session": SID, "environ": environ}


def needs(root):
    """The memory-base seam: only the linked fixture home needs one."""
    return BASE if root == LINKED else None


def seen(table):
    """A witness seam: {pid: [value-or-None] | None}."""
    def witness(pid, _start):
        if pid not in table:
            raise AssertionError("witness consulted for pid %s" % pid)
        return table[pid]
    return witness


class MemoryBaseTest(unittest.TestCase):
    """The incident's two poles on one seam: a session whose early children
    lack the base PREDATES the render, one whose early children carry it does
    not. Only the witness and the base answer are injected; the verdict is the
    real function's."""

    def state(self, *rows, witness=None, **flags):
        return seatstale.memory_base_state(
            census=census(*rows, **flags), witness=witness or seen({}),
            base=needs, age=ages({r["pid"]: 5 * DAY for r in rows}))

    def test_a_session_whose_early_children_lack_the_base_predates_it(self):
        st = self.state(mrow(), witness=seen({4242: [None, None, None]}))
        self.assertEqual([r["seat"] for r in st["predates"]], ["seat-a"])
        row = st["predates"][0]
        self.assertEqual((row["home"], row["session"], row["witnesses"]),
                         ("linked-fixture", SID, 3))
        self.assertEqual(st["blind"], [])

    def test_a_session_whose_early_children_carry_the_base_is_current(self):
        """THE CONTROL POLE: same row, same seam, the base present in one
        early child — the session resolved the real path."""
        st = self.state(mrow(), witness=seen({4242: [None, BASE]}))
        self.assertEqual(st["predates"], [])
        self.assertEqual(st["measured"], 1)

    def test_launched_with_the_base_needs_no_witness(self):  # noqa: VACUOUS_ASSERTION — measured==1 is the positive control; the raising witness seam proves no child was consulted
        st = self.state(mrow(env={VAR: BASE}))    # seen({}) raises if asked
        self.assertEqual((st["predates"], st["measured"]), ([], 1))

    def test_a_different_base_in_every_early_child_still_predates(self):
        st = self.state(mrow(), witness=seen({4242: ["/elsewhere"]}))
        self.assertEqual(len(st["predates"]), 1)

    def test_no_early_child_is_blind_never_current(self):
        st = self.state(mrow(), witness=seen({4242: []}))
        self.assertEqual(st["predates"], [])
        self.assertEqual(st["measured"], 0)
        self.assertIn("no child", st["blind"][0]["why"])

    def test_unreadable_children_are_blind_with_their_own_reason(self):
        st = self.state(mrow(), witness=seen({4242: None}))
        self.assertIn("could not be read", st["blind"][0]["why"])

    def test_a_home_that_needs_no_base_is_out_of_scope(self):
        st = self.state(mrow(root="/homes/real-projects"),
                        mrow(pid=7, child=True, seat="sub"))
        self.assertEqual((st["measured"], st["predates"], st["blind"]),
                         (0, [], []))
        # CONTROL: the same seat on the linked home IS measured.
        self.assertEqual(self.state(mrow(env={VAR: BASE}))["measured"], 1)

    def test_a_failed_listing_is_not_a_clean_fleet(self):
        st = self.state(mrow(), listing_failed=True)
        self.assertFalse(st["read"])
        self.assertIn("enumeration failed", st["why"])
        self.assertEqual(st["predates"], [])


class EarlyWitnessTest(unittest.TestCase):
    """The REAL /proc reader over a planted tree in the incident's shape: the
    children born with the session lack the variable, a child born an hour
    later (after the settings reload) carries it, and only the first kind is
    a witness."""

    def setUp(self):
        self.proc = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.proc, True)
        self.hz = os.sysconf("SC_CLK_TCK") or 100

    def plant(self, pid, ppid, start, env):
        d = os.path.join(self.proc, str(pid))
        os.makedirs(d)
        fields = ["S", str(ppid)] + ["0"] * 17 + [str(start)]
        with open(os.path.join(d, "stat"), "w") as f:
            f.write("%d (c (x)) %s\n" % (pid, " ".join(fields)))
        with open(os.path.join(d, "environ"), "wb") as f:
            f.write(b"\0".join(b"%s=%s" % (k.encode(), v.encode())
                               for k, v in env.items()) + b"\0")

    def test_only_children_born_with_the_session_witness_it(self):
        self.plant(10, 1, 1000, {})
        self.plant(11, 10, 1000 + 2 * self.hz, {"PATH": "/bin"})
        self.plant(12, 10, 1000 + 3600 * self.hz, {VAR: BASE})
        self.plant(13, 99, 1000, {VAR: BASE})           # someone else's child
        got = seatstale.early_witnesses(self.proc)(10, "1000")
        self.assertEqual(got, [None])
        # CONTROL: the late child is really there and really carries it, so
        # its absence above is the birth window at work, not a planting error.
        self.assertEqual(seatstale._env_value(12, VAR, self.proc), (True, BASE))

    def test_an_unreadable_proc_is_none_not_an_empty_witness_list(self):  # noqa: VACUOUS_ASSERTION — test_only_children_born_with_the_session_witness_it is the positive control on the same reader
        got = seatstale.early_witnesses(os.path.join(self.proc, "nope"))(10, "1")
        self.assertIsNone(got)


class MemoryBaseDoctorTest(unittest.TestCase):
    def report(self, st):
        with mock.patch.object(seatstale, "memory_base_state", lambda: st):
            return doctor.check_memory_base_sessions()

    def world(self, witness):
        return seatstale.memory_base_state(
            census=census(mrow()), witness=seen({4242: witness}), base=needs,
            age=ages({4242: DAY}))

    def test_a_predating_seat_is_a_named_fail_with_the_resume_relaunch(self):
        msgs = levels(self.report(self.world([None])), doctor.FAIL)
        self.assertEqual(len(msgs), 1, msgs)
        for want in ("seat-a", "linked-fixture", "permission prompt",
                     "--resume %s" % SID, "helm launch --seat seat-a --home "
                     "linked-fixture", "never types into a pane"):
            self.assertIn(want, msgs[0])

    def test_a_current_fleet_is_ok_and_the_fail_arm_is_real(self):
        ok = self.report(self.world([BASE]))
        self.assertEqual(levels(ok, doctor.FAIL), [])
        self.assertEqual(len(levels(ok, doctor.OK)), 1)
        self.assertTrue(levels(self.report(self.world([None])), doctor.FAIL))

    def test_an_undecided_session_warns_and_withholds_the_all_clear(self):
        rows = self.report(self.world([]))
        self.assertEqual(levels(rows, doctor.OK), [])
        self.assertIn("could not be decided", levels(rows, doctor.WARN)[0])

    def test_an_unread_census_is_unknown(self):
        rows = self.report({"read": False, "why": "fixture", "measured": 0,
                            "predates": [], "blind": []})
        self.assertIn("UNKNOWN", levels(rows, doctor.WARN)[0])

    def test_the_rung_runs_inside_helm_doctor(self):
        self.assertIn("check_memory_base_sessions", doctor.CHECKS)
