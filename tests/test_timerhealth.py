#!/usr/bin/env python3
"""ENABLED AND RUNNING ARE TWO FACTS, and the census must not fold them.

Every arm scripts systemctl through ONE seam, so a fixture cannot describe a
box where `list-unit-files` and `show` disagree about which units exist.

The second half holds the ONE installer every helm user timer goes through
(task/3307): its own arms, and a contract per installer, pinned against what
each one did before it moved. The last part holds the unit-drift census
(task/3405): the installed text of every helm unit against what its module's
template renders now.
"""
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from tests._tmphome import fake_user_systemd

from helm import doctor, timerhealth


def scripted(units, show, calls=None, refuse=(), show_rc=0):
    """One description of a box: its timer list and each unit's fields.

    `show` maps unit name -> field dict; a unit missing from it is a unit
    systemctl declined to describe, which is UNKNOWN and never healthy.
    `calls` collects every argv so an arm can bound the subprocess count.
    `refuse` names argv[0] verbs the box answers nonzero to.
    """
    listing = "\n".join("%s enabled enabled" % u for u in units)

    def run(argv):
        if calls is not None:
            calls.append(argv)
        if argv[0] in refuse:
            return 1, ""
        if argv[0] == "list-unit-files":
            return 0, listing
        if argv[0] == "show":
            asked = []
            skip = False
            for arg in argv[1:]:
                if skip:
                    skip = False
                elif arg == "-p":
                    skip = True
                else:
                    asked.append(arg)
            blocks = []
            for name in asked:
                fields = show.get(name)
                if fields is None:
                    continue
                identity = fields.get("Id", name)
                names = fields.get("Names", name)
                lines = ["Id=%s" % identity, "Names=%s" % names]
                for key, value in fields.items():
                    if key in ("Id", "Names"):
                        continue
                    # `show` PRINTS ONE LINE PER ENTRY. A fixture that joined
                    # two schedule entries into one value never exercised the
                    # duplicate-key path it was written to cover.
                    for part in str(value).split("\n"):
                        lines.append("%s=%s" % (key, part))
                blocks.append("\n".join(lines))
            return show_rc, "\n\n".join(blocks)
        raise AssertionError("unscripted systemctl call: %r" % (argv,))
    return run


def timer(active="active", enabled="enabled", nxt="1d 4h", unit=None,
          monotonic="{ OnUnitActiveUSec=3min }", calendar="", fired="",
          clock_change="no", zone_change="no", substate=None):
    """A recurring timer by default — the shape a never-fires finding is about."""
    if substate is None:
        substate = "failed" if active == "failed" else \
            "dead" if active != "active" else \
            "elapsed" if nxt == "infinity" else "waiting"
    fields = {"ActiveState": active, "SubState": substate,
              "UnitFileState": enabled,
              "NextElapseUSecRealtime": "", "NextElapseUSecMonotonic": nxt,
              "TimersMonotonic": monotonic, "TimersCalendar": calendar,
              "LastTriggerUSec": fired, "OnClockChange": clock_change,
              "OnTimezoneChange": zone_change}
    if unit is not None:
        fields["Unit"] = unit
    return fields


def service(active, result="success", load="loaded"):
    return {"LoadState": load, "ActiveState": active, "SubState": "dead",
            "Result": result}


class TimerHealthTest(unittest.TestCase):

    def verdicts(self, units, show, calendar=None):
        """THE POSITIVE CONTROL RIDES EVERY CALL, on the same call's other
        channels: the census reported no error AND returned a row for every
        unit it was asked about. Without that, an arm asserting a verdict is
        also satisfied by a census that read nothing and a dict that answered
        from a default."""
        rows, err = timerhealth.census(
            run=scripted(units, show), calendar=calendar)
        self.assertIsNone(err)
        self.assertEqual(len(rows), len(units))
        got = {r[0]: r[1] for r in rows}
        self.assertEqual(sorted(got), sorted(units))
        return got

    def test_enabled_and_not_active_is_the_state_that_LIES(self):
        """The nineteen-hour shape: the field an operator reaches for first
        keeps saying yes while the timer is not scheduled at all."""
        got = self.verdicts(["a.timer"],
                            {"a.timer": timer(active="inactive")})
        self.assertEqual(got["a.timer"], timerhealth.TRAP)

    def test_disabled_and_inactive_is_a_decision_not_a_finding(self):
        """...and the same absence with the file agreeing is somebody's
        choice. Reporting it would bury the state that lies in noise."""
        got = self.verdicts(["b.timer"],
                            {"b.timer": timer(active="inactive",
                                              enabled="disabled")})
        self.assertEqual(got["b.timer"], timerhealth.OFF)

    def test_active_with_no_next_elapse_and_a_quiet_service_NEVER_fires(self):
        got = self.verdicts(
            ["c.timer"], {"c.timer": timer(nxt="infinity"),
                          "c.service": service("inactive")})
        self.assertEqual(got["c.timer"], timerhealth.NEVER)

    def test_timer_SUBSTATE_directly_proves_a_mid_trigger(self):
        """The timer owns this state; target activity is neither necessary nor
        sufficient because the target may have been started independently."""
        calls = []
        rows, err = timerhealth.census(
            run=scripted(
                ["c.timer"],
                {"c.timer": timer(nxt="infinity", substate="running"),
                 "c.service": service("inactive")}, calls=calls))
        self.assertIsNone(err)
        self.assertEqual([row[1] for row in rows], [timerhealth.RUNNING])
        self.assertFalse(any("c.service" in argv for argv in calls))

    def test_a_spent_ONE_SHOT_is_not_accused_of_never_firing(self):
        """OnBoot work that COMPLETED looks exactly like the defect: active,
        no next elapse, service quiet. It has no next elapse because it has no
        next — and the repair this rung would print re-runs boot work."""
        got = self.verdicts(
            ["c.timer"],
            {"c.timer": timer(nxt="infinity", monotonic="{ OnBootUSec=2min }",
                              fired="Thu 2026-09-10 05:41:32 PDT"),
             "c.service": service("inactive")})
        self.assertEqual(got["c.timer"], timerhealth.SPENT)

    def test_a_ONE_SHOT_that_never_fired_and_has_no_next_IS_the_finding(self):
        """The discriminator is the FIRING, not the base: a one-shot armed for
        an elapse that never came and now has none is broken, not spent."""
        got = self.verdicts(
            ["c.timer"],
            {"c.timer": timer(nxt="infinity", monotonic="{ OnBootUSec=2min }"),
             "c.service": service("inactive")})
        self.assertEqual(got["c.timer"], timerhealth.NEVER)

    def test_a_RECURRING_timer_that_already_fired_is_still_the_finding(self):
        """A calendar timer promises another firing. Having fired before is no
        excuse for having no next elapse, and reading LastTrigger alone would
        excuse exactly the nineteen-hour case this rung exists for."""
        got = self.verdicts(
            ["c.timer"],
            {"c.timer": timer(nxt="infinity", monotonic="",
                              calendar="{ OnCalendar=daily }",
                              fired="Thu 2026-09-10 05:41:32 PDT"),
             "c.service": service("inactive")},
            calendar=lambda expression: expression == "daily")
        self.assertEqual(got["c.timer"], timerhealth.NEVER)

    def test_waiting_daily_timer_with_no_next_is_the_original_incident(self):
        got = self.verdicts(
            ["c.timer"],
            {"c.timer": timer(nxt="infinity", substate="waiting",
                              monotonic="",
                              calendar="{ OnCalendar=daily ; next_elapse=n/a }")},
            calendar=lambda expression: expression == "daily")
        self.assertEqual(got["c.timer"], timerhealth.NEVER)

    def test_two_schedule_lines_both_reach_the_recurring_test(self):
        """`show` prints one line per schedule entry, so a timer with a boot
        offset AND a repeat arrives as two TimersMonotonic lines. Keeping only
        the last would drop the recurring one and excuse a real defect."""
        got = self.verdicts(
            ["c.timer"],
            {"c.timer": timer(nxt="infinity",
                              monotonic="{ OnUnitActiveUSec=3min }\n"
                                        "{ OnBootUSec=2min }",
                              fired="Thu 2026-09-10 05:41:32 PDT"),
             "c.service": service("inactive")})
        self.assertEqual(got["c.timer"], timerhealth.NEVER)

    def test_a_FAILED_timer_is_a_finding_whatever_its_file_says(self):
        """A failed unit fires nothing, and reading it through the file state
        filed it as somebody's decision whenever the file said disabled or
        static — the loudest possible silence."""
        got = self.verdicts(
            ["a.timer", "b.timer"],
            {"a.timer": timer(active="failed", enabled="disabled"),
             "b.timer": timer(active="failed", enabled="static")})
        self.assertEqual(got["a.timer"], timerhealth.FAILED)
        self.assertEqual(got["b.timer"], timerhealth.FAILED)

    def test_an_EVENT_ONLY_timer_has_no_clock_to_wait_on(self):
        """OnClockChange fires on an event, not a schedule. It has no next
        elapse because it has none to have, and it is not spent either."""
        got = self.verdicts(
            ["c.timer"],
            {"c.timer": timer(nxt="infinity", monotonic="", calendar="",
                              clock_change="yes", substate="waiting"),
             "c.service": service("inactive")})
        self.assertEqual(got["c.timer"], timerhealth.WAITING)

    def test_an_EXHAUSTED_one_date_calendar_is_not_a_finding(self):
        """A single-date OnCalendar that has passed promises nothing further.
        Treating any calendar as a promise made every one of these a defect."""
        got = self.verdicts(
            ["c.timer"],
            {"c.timer": timer(nxt="infinity", monotonic="",
                              calendar="{ OnCalendar=2026-01-01 ; "
                                       "next_elapse=n/a }",
                              fired="Thu 2026-01-01 00:00:00 PST"),
             "c.service": service("inactive")},
            calendar=lambda _expression: False)
        self.assertEqual(got["c.timer"], timerhealth.SPENT)

    def test_a_calendar_with_a_LIVE_next_elapse_still_promises_one(self):
        """The must-differ control for the arm above: same shape, one field
        different, and the finding comes back."""
        got = self.verdicts(
            ["c.timer"],
            {"c.timer": timer(nxt="infinity", monotonic="",
                              calendar="{ OnCalendar=daily ; "
                                       "next_elapse=Fri 2026-09-11 }",
                              fired="Thu 2026-09-10 00:00:00 PDT"),
             "c.service": service("inactive")})
        self.assertEqual(got["c.timer"], timerhealth.NEVER)

    def test_a_DAILY_timer_whose_next_elapse_went_MISSING_is_the_incident(self):
        """THE HOLE THIS RESTRUCTURE NEARLY OPENED, caught by an older arm.

        The nineteen-hour shape on a CALENDAR timer reports the same
        `next_elapse=n/a` as an exhausted one-date timer, so a rule that asks
        the STAMP whether another elapse is promised excuses the exact defect
        this rung exists for. The EXPRESSION is what separates them: `daily`
        repeats whatever its stamp says.
        """
        got = self.verdicts(
            ["c.timer"],
            {"c.timer": timer(nxt="infinity", monotonic="",
                              calendar="{ OnCalendar=daily ; "
                                       "next_elapse=n/a }",
                              fired="Wed 2026-09-09 00:00:00 PDT"),
             "c.service": service("inactive")},
            calendar=lambda expression: expression == "daily")
        self.assertEqual(got["c.timer"], timerhealth.NEVER)

    def test_calendar_future_uses_systemds_parser_not_local_syntax_guesses(self):
        answers = [SimpleNamespace(returncode=0,
                                   stdout="    Next elapse: never\n"),
                   SimpleNamespace(returncode=0,
                                   stdout="    Next elapse: Fri 2026-09-11\n")]
        with mock.patch.object(timerhealth.subprocess, "run",
                               side_effect=answers) as run:
            self.assertFalse(timerhealth._calendar_future(
                "2026-01-01,03 00:00:00"))
            self.assertTrue(timerhealth._calendar_future("daily"))
        self.assertEqual(2, run.call_count)

    def test_an_EXHAUSTED_bounded_calendar_is_not_called_recurring(self):
        expression = "2026-01-01,03 00:00:00"
        got = self.verdicts(
            ["c.timer"],
            {"c.timer": timer(nxt="infinity", monotonic="",
                              calendar="{ OnCalendar=%s ; next_elapse=n/a }"
                                       % expression,
                              fired="Sat 2026-01-03 00:00:00 PST"),
             "c.service": service("inactive")},
            calendar=lambda value: False if value == expression else None)
        self.assertEqual(got["c.timer"], timerhealth.SPENT)

    def test_a_FAILED_target_is_not_a_spent_success(self):
        got = self.verdicts(
            ["c.timer"],
            {"c.timer": timer(nxt="infinity",
                              monotonic="{ OnBootUSec=2min }",
                              fired="Thu 2026-09-10 05:41:32 PDT"),
             "c.service": service("failed", result="exit-code")})
        self.assertEqual(got["c.timer"], timerhealth.FAILED)

    def test_an_inactive_target_with_a_failure_result_is_not_spent(self):
        got = self.verdicts(
            ["c.timer"],
            {"c.timer": timer(nxt="infinity",
                              monotonic="{ OnBootUSec=2min }",
                              fired="Thu 2026-09-10 05:41:32 PDT"),
             "c.service": service("inactive", result="exit-code")})
        self.assertEqual(got["c.timer"], timerhealth.FAILED)

    def test_a_missing_target_is_unknown_not_a_completed_success(self):
        got = self.verdicts(
            ["c.timer"],
            {"c.timer": timer(nxt="infinity",
                              monotonic="{ OnBootUSec=2min }",
                              fired="Thu 2026-09-10 05:41:32 PDT"),
             "c.service": service("inactive", load="not-found")})
        self.assertEqual(got["c.timer"], timerhealth.UNKNOWN)

    def test_target_activity_does_not_substitute_for_timer_running(self):
        got = self.verdicts(
            ["c.timer"],
            {"c.timer": timer(nxt="infinity", substate="elapsed"),
             "c.service": service("active")})
        self.assertEqual(got["c.timer"], timerhealth.UNKNOWN)

    def test_indirect_file_states_do_not_claim_somebody_turned_them_off(self):
        states = ("static", "generated", "indirect", "alias", "linked")
        units = ["u%d.timer" % index for index in range(len(states))]
        got = self.verdicts(
            units, dict((unit, timer(active="inactive", enabled=state))
                        for unit, state in zip(units, states)))
        self.assertEqual([got[unit] for unit in units],
                         [timerhealth.UNKNOWN] * len(units))

    def test_bulk_results_bind_back_to_the_requested_alias(self):
        got = self.verdicts(
            ["alias.timer"],
            {"alias.timer": dict(timer(), Id="canonical.timer",
                                  Names="canonical.timer alias.timer")})
        self.assertEqual(got["alias.timer"], timerhealth.HEALTHY)

    def test_one_uninstantiated_template_does_not_blind_valid_peers(self):
        units = ["valid.timer", "job@.timer"]
        calls = []
        run = scripted(units, {"valid.timer": timer()}, calls=calls, show_rc=1)
        rows, err = timerhealth.census(run=run)
        self.assertIsNone(err)
        got = dict((row[0], row[1]) for row in rows)
        self.assertEqual(got["valid.timer"], timerhealth.HEALTHY)
        self.assertEqual(got["job@.timer"], timerhealth.UNKNOWN)
        self.assertFalse(any("job@.timer" in argv for argv in calls))

    def test_a_timer_without_its_own_substate_is_UNKNOWN(self):
        fields = timer()
        del fields["SubState"]
        got = self.verdicts(["c.timer"], {"c.timer": fields})
        self.assertEqual(got["c.timer"], timerhealth.UNKNOWN)

    def test_a_unit_systemctl_would_not_describe_is_UNKNOWN(self):
        """Never healthy by default: an unreadable unit is a hole in the
        census, and a census that fills its holes with OK is worse than none."""
        got = self.verdicts(["d.timer"], {})
        self.assertEqual(got["d.timer"], timerhealth.UNKNOWN)

    def test_enabled_RUNTIME_is_enabled_and_still_the_state_that_LIES(self):
        """A runtime symlink is somebody saying yes. Reading `enabled-runtime`
        as OFF hides exactly the intent this rung exists to check, so the one
        state that actively lies gets filed as a decision nobody has to see."""
        got = self.verdicts(["a.timer"],
                            {"a.timer": timer(active="inactive",
                                              enabled="enabled-runtime")})
        self.assertEqual(got["a.timer"], timerhealth.TRAP)

    def test_a_file_state_we_do_not_RECOGNISE_is_not_a_decision(self):
        """OFF claims somebody chose this. A word systemd added since cannot
        support that claim, and guessing it is the cheaper of two errors is
        how an unread field becomes an all-clear."""
        # THE MUST-DIFFER CONTROL RIDES THE SAME CENSUS, on a fixture
        # identical but for the word: without it, a classifier that answered
        # UNKNOWN to everything would satisfy the assertion below.
        got = self.verdicts(["a.timer", "b.timer"],
                            {"a.timer": timer(active="inactive",
                                              enabled="something-new"),
                             "b.timer": timer(active="inactive",
                                              enabled="disabled")})
        self.assertEqual(got["b.timer"], timerhealth.OFF)
        self.assertEqual(got["a.timer"], timerhealth.UNKNOWN)

    def test_a_paired_service_that_could_not_be_READ_is_not_a_quiet_one(self):
        """THE VACUOUS ACCUSATION. A refused service read parses to nothing,
        which is shaped exactly like a service that answered `inactive` — and
        the second reading convicts the timer of never firing. NEVER-FIRES is
        an accusation; an unread discriminator cannot support one."""
        calls = []
        rows, err = timerhealth.census(
            run=scripted(["c.timer"], {"c.timer": timer(nxt="infinity")},
                         calls=calls))
        self.assertIsNone(err)
        self.assertEqual([r[1] for r in rows], [timerhealth.UNKNOWN])
        self.assertTrue(any(a[0] == "show" and "c.service" in a
                            for a in calls),
                        "the census never asked about the paired service at "
                        "all, so this arm proves nothing about reading it")

    def test_the_timer_NAMES_its_service_and_the_census_reads_that_name(self):
        """A timer may point Unit= anywhere. Probing <name>.service asks about
        a unit the timer does not use, and its absence then reads as a quiet
        service — a NEVER-FIRES finding about something never looked at."""
        got = self.verdicts(
            ["c.timer"],
            {"c.timer": timer(nxt="infinity", unit="worker.service",
                              monotonic="{ OnBootUSec=2min }",
                              fired="Thu 2026-09-10 05:41:32 PDT"),
             "worker.service": dict(
                 service("failed", result="exit-code"),
                 Id="canonical-worker.service",
                 Names="canonical-worker.service worker.service")})
        self.assertEqual(got["c.timer"], timerhealth.FAILED)

    def test_the_census_costs_TWO_reads_and_not_one_per_unit(self):
        """N+1 IS THE DEFECT. One show per unit turns 50 timers into 81
        subprocesses under 81 timeouts, so a rung meant to cost a second can
        hold doctor for minutes. The bound is on the CALLS, because a fast
        fixture cannot fail a timing assertion."""
        units = ["t%d.timer" % i for i in range(20)]
        show = dict((u, timer()) for u in units)
        show["t0.timer"] = timer(nxt="infinity")
        show["t0.service"] = service("inactive")
        calls = []
        rows, err = timerhealth.census(run=scripted(units, show, calls=calls))
        self.assertIsNone(err)
        self.assertEqual(len(rows), 20)
        self.assertEqual(len(calls), 3,
                         "expected list-unit-files + one bulk timer show + "
                         "one bulk service show, got %r"
                         % ([a[:2] for a in calls],))
        asked = [a for a in calls if a[0] == "show"]
        self.assertEqual([u for u in asked[1][1:] if u.endswith(".service")],
                         ["t0.service"],
                         "the second read must cover ONLY the units a service "
                         "state can still reclassify")

    def test_a_refused_bulk_describe_is_UNMEASURED_not_an_empty_box(self):
        rows, err = timerhealth.census(
            run=scripted(["a.timer"], {"a.timer": timer()}, refuse=("show",)))
        self.assertEqual(rows, [])
        self.assertIn("UNMEASURED", err)

    def test_a_census_that_could_not_RUN_says_so_instead_of_reporting_none(self):
        """An empty row list with no error means "asked, all fine". A box
        without systemd must not be able to make that claim."""
        rows, err = timerhealth.census(run=lambda argv: (None, ""))
        self.assertEqual(rows, [])
        self.assertIn("UNMEASURED", err)


class TimerRungTest(unittest.TestCase):
    """The doctor rung over the same descriptions."""

    def rung(self, units, show):
        return doctor.check_timers(
            census=lambda: timerhealth.census(run=scripted(units, show)))

    def test_the_trap_is_reported_and_names_the_repair(self):
        out = self.rung(["a.timer"], {"a.timer": timer(active="inactive")})
        levels = [lvl for lvl, _line in out]
        self.assertIn(doctor.WARN, levels)
        text = " ".join(line for _lvl, line in out)
        self.assertIn("a.timer", text)
        self.assertIn("systemctl --user start a.timer", text)

    def test_a_healthy_box_reports_HOW_MANY_it_actually_read(self):
        """THE UNCONDITIONAL POSITIVE CONTROL. An empty finding list is what
        this rung says most days AND what it would say if the census had read
        nothing, so the OK line carries the count that separates them."""
        out = self.rung(["a.timer", "b.timer"],
                        {"a.timer": timer(), "b.timer": timer()})
        self.assertEqual([lvl for lvl, _l in out], [doctor.OK])
        self.assertIn("2 of 2", out[0][1])

    def test_a_MID_TRIGGER_box_is_never_reported_as_an_idle_one(self):
        """THE FOLDED POPULATION. `the rest are off on purpose` was a claim
        about units the line never counted: a mid-trigger timer is neither
        scheduled nor off, so a census of one RUNNING timer printed
        `0 of 1 ... the rest are off` — an all-clear about a box whose only
        timer was working at that moment."""
        out = self.rung(["c.timer"],
                        {"c.timer": timer(nxt="infinity", substate="running"),
                         "c.service": service("activating")})
        self.assertEqual([lvl for lvl, _l in out], [doctor.OK])
        line = out[0][1]
        self.assertIn("1 mid-trigger", line)
        self.assertIn("0 explicitly off", line)

    def test_an_unreadable_unit_is_warned_and_never_counted_healthy(self):
        out = self.rung(["a.timer", "d.timer"], {"a.timer": timer()})
        text = " ".join(line for _lvl, line in out)
        self.assertIn("UNMEASURED", text)
        self.assertIn("d.timer", text)
        self.assertNotIn("2 of 2", text)

    def test_an_EMPTY_successful_census_is_valid_on_a_fresh_checkout(self):
        out = self.rung([], {})
        self.assertEqual([lvl for lvl, _l in out], [doctor.OK])
        self.assertIn("0 of 0", out[0][1])

    def test_more_unreadable_units_than_fit_are_COUNTED_not_dropped(self):
        """A silent cut after three renders a census of thirty holes exactly
        like a census of three."""
        units = ["u%d.timer" % i for i in range(6)]
        out = self.rung(units, {})
        text = " ".join(line for _lvl, line in out)
        self.assertIn("6 unit(s) could not be read", text)
        self.assertIn("and 3 more", text)

    def test_a_census_that_raises_leaves_the_report_standing(self):
        """A rung that cannot look SAYS so; it must never take doctor down."""
        def boom():
            raise RuntimeError("no systemd here")
        out = doctor.check_timers(census=boom)
        self.assertEqual([lvl for lvl, _l in out], [doctor.WARN])
        self.assertIn("cannot tell", out[0][1])



# ---------------------------------------------------------------------------
# THE ONE INSTALLER (task/3307)
# ---------------------------------------------------------------------------

_RELOAD = ["--user", "daemon-reload"]


def _enable(*timers):
    return ["--user", "enable", "--now"] + list(timers)


class _Seam(object):
    """A caller's subprocess seam: records each call and answers from
    `answers` in order, an exit status or an exception to raise."""

    TimeoutExpired = subprocess.TimeoutExpired

    def __init__(self, *answers, stdout="", stderr=""):
        self.answers = list(answers)
        self.stdout, self.stderr = stdout, stderr
        self.calls = []

    def run(self, argv, **kw):
        self.calls.append((list(argv), kw))
        answer = self.answers.pop(0) if self.answers else 0
        if isinstance(answer, BaseException):
            raise answer
        return subprocess.CompletedProcess(argv, answer, self.stdout,
                                           self.stderr)

    def argvs(self):
        return [argv for argv, _kw in self.calls]


class UserUnitDirTest(unittest.TestCase):
    """ONE directory every installer writes and every reader reads."""

    def unit_dir(self, value=None, home=None):
        with mock.patch.dict(os.environ, {"HOME": "/fixture/home"}):
            os.environ.pop(timerhealth.UNIT_DIR_ENV, None)
            if value is not None:
                os.environ[timerhealth.UNIT_DIR_ENV] = value
            return timerhealth.user_unit_dir(home)

    def test_the_default_is_the_homes_own_user_unit_directory(self):
        self.assertEqual(self.unit_dir(),
                         "/fixture/home/.config/systemd/user")

    def test_the_variable_moves_it_and_empty_is_unset(self):
        self.assertEqual(timerhealth.UNIT_DIR_ENV, "HELM_USER_UNIT_DIR")
        self.assertEqual(self.unit_dir("/fixture/units"), "/fixture/units")
        self.assertEqual(self.unit_dir(""),
                         "/fixture/home/.config/systemd/user")

    def test_a_named_home_answers_for_itself_whatever_the_variable_says(self):
        self.assertEqual(self.unit_dir("/fixture/units", home="/estate"),
                         "/estate/.config/systemd/user")


class InstallUserTimerTest(unittest.TestCase):
    """The shared installer, driven through a caller's seam into a temp dir."""

    SYSTEMCTL = "/fixture/bin/systemctl"

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="helm-test-install-timer-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.units = [(os.path.join(self.dir, "helm-a.service"), "SERVICE\n"),
                      (os.path.join(self.dir, "helm-a.timer"), "TIMER A\n"),
                      (os.path.join(self.dir, "helm-b.timer"), "TIMER B\n")]

    def install(self, seam, **kw):
        return timerhealth.install_user_timer(
            self.units, ("helm-a.timer", "helm-b.timer"), self.SYSTEMCTL,
            seam, **kw)

    def read(self):
        out = []
        for path, _text in self.units:
            with open(path, encoding="utf-8") as fh:
                out.append(fh.read())
        return out

    def stamps(self):
        return [(os.stat(p).st_ino, os.stat(p).st_mtime_ns)
                for p, _t in self.units]

    def test_a_fresh_install_writes_every_unit_then_one_reload_and_one_enable(self):
        seam = _Seam()
        self.assertEqual(self.install(seam), (None, False))
        self.assertEqual(self.read(), ["SERVICE\n", "TIMER A\n", "TIMER B\n"])
        self.assertEqual(seam.argvs(), [
            [self.SYSTEMCTL] + _RELOAD,
            [self.SYSTEMCTL] + _enable("helm-a.timer", "helm-b.timer")])
        for _argv, kw in seam.calls:
            # systemctl's words read as UTF-8 whatever the locale, a byte
            # that is not replaced (task/3423).
            self.assertEqual(kw, {"capture_output": True, "encoding": "utf-8",
                                  "errors": "replace", "timeout": 30})

    def test_it_runs_the_callers_seam_never_its_own(self):
        """A test stubs the module it tests; an installer that reached past
        that stub to another module's subprocess would run a real systemctl
        (task/3254's review, finding 3)."""
        seam = _Seam()
        with mock.patch.object(timerhealth.subprocess, "run",
                               side_effect=AssertionError("not the seam")):
            self.assertEqual(self.install(seam, timeout=60), (None, False))
        self.assertEqual(len(seam.calls), 2)
        self.assertEqual([kw["timeout"] for _a, kw in seam.calls], [60, 60])

    def test_an_unchanged_install_rewrites_nothing_and_reloads_only_if_asked(self):
        self.assertEqual(self.install(_Seam(), keep_unchanged=True),
                         (None, False))
        before = self.stamps()
        seam = _Seam()
        self.assertEqual(self.install(seam, keep_unchanged=True,
                                      reload_unchanged=False), (None, True))
        self.assertEqual(seam.argvs(), [
            [self.SYSTEMCTL] + _enable("helm-a.timer", "helm-b.timer")])
        seam = _Seam()
        self.assertEqual(self.install(seam, keep_unchanged=True), (None, True))
        self.assertEqual(seam.argvs()[0], [self.SYSTEMCTL] + _RELOAD)
        self.assertEqual(len(seam.argvs()), 2)
        after = self.stamps()
        self.assertEqual(len(after), 3)
        self.assertEqual(after, before)

    def test_one_changed_unit_rewrites_the_install_and_reloads(self):
        self.install(_Seam(), keep_unchanged=True)
        with open(self.units[1][0], "w", encoding="utf-8") as fh:
            fh.write("an older timer\n")
        seam = _Seam()
        self.assertEqual(self.install(seam, keep_unchanged=True,
                                      reload_unchanged=False), (None, False))
        self.assertEqual(self.read(), ["SERVICE\n", "TIMER A\n", "TIMER B\n"])
        self.assertEqual(seam.argvs()[0], [self.SYSTEMCTL] + _RELOAD)

    def test_without_keep_unchanged_every_install_writes_afresh(self):
        self.install(_Seam())
        before = self.stamps()
        self.assertEqual(self.install(_Seam()), (None, False))
        self.assertTrue(all(b[0] != a[0] for b, a in zip(before,
                                                        self.stamps())),
                        "an identical unit was not rewritten")

    def test_the_first_failing_step_ends_the_install_and_names_itself(self):
        seam = _Seam(1, stderr="  the bus is gone \n")
        error, _unchanged = self.install(seam)
        self.assertEqual(error, "/fixture/bin/systemctl --user daemon-reload "
                         "failed: the bus is gone")
        self.assertEqual(len(seam.calls), 1)
        seam = _Seam(0, 1, stdout="Unit helm-b.timer not found.\n")
        error, _unchanged = self.install(seam)
        self.assertEqual(error, "/fixture/bin/systemctl --user enable --now "
                         "helm-a.timer helm-b.timer failed: Unit helm-b.timer "
                         "not found.")

    def test_a_failures_output_is_cut_at_two_hundred_characters(self):
        seam = _Seam(1, stderr="x" * 500)
        error, _unchanged = self.install(seam)
        self.assertTrue(error.endswith(" failed: " + "x" * 200), error)
        self.assertEqual(error, "%s --user daemon-reload failed: %s"
                         % (self.SYSTEMCTL, "x" * 200))

    def test_a_timeout_and_a_spawn_that_raises_are_failures_not_tracebacks(self):
        hung = subprocess.TimeoutExpired([self.SYSTEMCTL] + _RELOAD, 30)
        error, _unchanged = self.install(_Seam(hung))
        self.assertIn("timed out after 30 seconds", error)
        self.assertEqual(error, "%s --user daemon-reload failed: %s"
                         % (self.SYSTEMCTL, hung))
        gone = FileNotFoundError(2, "No such file or directory",
                                 self.SYSTEMCTL)
        error, _unchanged = self.install(_Seam(0, gone))
        self.assertIn("failed: [Errno 2] No such file or directory", error)
        self.assertEqual(error, "%s --user enable --now helm-a.timer "
                         "helm-b.timer failed: %s" % (self.SYSTEMCTL, gone))

    def test_a_unit_that_cannot_be_written_runs_no_systemctl(self):  # noqa: VACUOUS_ASSERTION — the empty call list IS the claim; the fresh-install arm drives the same _Seam to its two calls
        blocker = os.path.join(self.dir, "blocked")
        with open(blocker, "w", encoding="utf-8") as fh:
            fh.write("a file where the unit directory should be\n")
        self.units = [(os.path.join(blocker, "helm-a.service"), "SERVICE\n")]
        seam = _Seam()
        error, _unchanged = self.install(seam)
        self.assertTrue(error.startswith("unit write failed: "), error)
        self.assertIn(blocker, error)
        self.assertEqual(seam.calls, [], "systemctl ran over a failed write")

    def test_the_caller_words_its_own_failure(self):
        seam = _Seam(1, stderr="  two\n  lines  ")
        error, _unchanged = self.install(
            seam, clean=lambda r: "[%d: %s]" % (r.returncode,
                                                " ".join(r.stderr.split())),
            failed="systemctl --user failed: %(detail)s")
        self.assertEqual(error, "systemctl --user failed: [1: two lines]")


# WHAT A FAILING systemctl PRINTED in the contract arms below: leading and
# trailing space, a blank line, a tab and more than any installer keeps, so
# each installer's own cleaning shows in its report.
_NOISE = "  line one\n\n\tline two  " + "x" * 400 + "\n"


def _canonical(out):
    return out.strip()[:200]


def _folded(out):
    return " ".join(out.split())[:300]


def _cut(out):
    from helm import pk
    return pk.cut_marked(out.strip(), 200)


class _Installer(object):
    """One installer's contract, as it stood before it moved (task/3307).

    `units()` renders the (path, text) pairs through the module's own unit
    function; `done(paths, state)` is its success report, `state` one of
    "fresh", "unchanged" and "refreshed"; `missing` its report when
    shutil.which finds no systemctl (None: it never asks); `bare` says it runs
    `systemctl` from PATH rather than the path which() resolved; `clean` and
    `failed` word a failing systemctl; `idempotent` is None, "skip-reload"
    (an unchanged install only enables) or "reload" (it still reloads);
    `switch` names its off-switch variable."""

    def __init__(self, name, install, units, timers, done, missing,
                 bare=False, clean=_canonical,
                 failed="%(cmd)s failed: %(detail)s", timeout=30,
                 idempotent=None, switch=None, prepare=None):
        self.name, self.install, self.units = name, install, units
        self.timers, self.done, self.missing = timers, done, missing
        self.bare, self.clean, self.failed = bare, clean, failed
        self.timeout, self.idempotent = timeout, idempotent
        self.switch, self.prepare = switch, prepare


def _pairs(quad):
    return [(quad[0], quad[1]), (quad[2], quad[3])]


def _installers():
    from helm import (autocompact, autoland, beacons, checkoutwatch,
                      gatecanary, gateshadow, gc,
                      keepalive, offpeak, owedpush, pressurewatch,
                      proxy_fork_watch, proxywatch, releasenightly,
                      remote_relay, seat, stalebot, tasksmirror,
                      upstream_watch, work)

    def every(interval, words="timer enabled every"):
        return lambda paths, _state: "%s %ds (%s)" % (words, interval,
                                                       paths[-1])

    def nightly(at):
        return lambda paths, state: "%s nightly at %s (%s)" % (
            "already installed, unchanged," if state == "unchanged"
            else "installed", at, paths[-1])

    def watch_done(paths, state):
        return "%s every %ds (%s)" % (
            "already installed, unchanged," if state == "unchanged"
            else "installed", checkoutwatch.INTERVAL_S, paths[-1])

    def pressure_done(paths, state):
        return "%s every %ds (%s)" % (
            "already installed, unchanged," if state == "unchanged"
            else "installed", pressurewatch.INTERVAL_S, paths[-1])

    def keepalive_done(paths, state):
        interval = keepalive.DEFAULT_INTERVAL_S
        if state == "unchanged":
            return ("keepalive cadence already installed every %ds, "
                    "unchanged (%s)" % (interval, paths[-1]))
        return "keepalive cadence %s every %ds (%s)" % (
            "refreshed" if state == "refreshed" else "enabled", interval,
            paths[-1])

    def no_crontab(case):
        patch = mock.patch.object(keepalive, "hand_crontab", lambda: [])
        patch.start()
        case.addCleanup(patch.stop)

    def fork_watch_ready(case):
        """The checks it makes before any unit: a clone, go, and an
        executable installed helm under the (fake) home."""
        helm_bin = os.path.join(os.path.expanduser("~"), ".local", "bin",
                                "helm")
        os.makedirs(os.path.dirname(helm_bin))
        with open(helm_bin, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\n")
        os.chmod(helm_bin, 0o755)
        patch = mock.patch.object(proxy_fork_watch, "_inspect_clone",
                                  return_value=("/fixture/clone", "HEAD"))
        patch.start()
        case.addCleanup(patch.stop)
        case.which_also["go"] = "/fixture/bin/go"

    def relay_units():
        here = os.path.dirname(os.path.dirname(os.path.abspath(
            remote_relay.__file__)))
        udir = os.environ["HELM_USER_UNIT_DIR"]
        return [(os.path.join(udir, "helm-remote-relay.service"),
                 remote_relay._UNIT_SERVICE % {
                     "cwd": work.find_root(here) or here,
                     "bot": remote_relay.BOT,
                     "helm": os.path.join(os.path.expanduser("~"), ".local",
                                          "bin", "helm"),
                     "knobs": remote_relay._unit_knobs()}),
                (os.path.join(udir, "helm-remote-relay.timer"),
                 remote_relay._UNIT_TIMER % {"interval": 600})]

    def relay_ready(case):
        case.env_off.append(remote_relay.INTERVAL_ENV)

    return [
        _Installer(
            "autoland", autoland.ensure_timer,
            lambda: _pairs(autoland.timer_units()), (autoland.TIMER_NAME,),
            lambda paths, state: "%s every %d s (%s)" % (
                "already installed, unchanged," if state == "unchanged"
                else "installed", autoland.INTERVAL_S, paths[-1]),
            "systemctl unavailable; run `%s --apply` every %d s from another "
            "scheduler" % (autoland.PROG, autoland.INTERVAL_S),
            idempotent="skip-reload", switch=autoland.TIMER_ENV),
        _Installer(
            "autocompact", autocompact.ensure_timer,
            lambda: _pairs(autocompact._timer_units()),
            ("helm-autocompact.timer",),
            every(autocompact.DEFAULT_INTERVAL_S),
            "systemctl unavailable; run autocompact from another scheduler",
            switch=autocompact.TIMER_ENV),
        _Installer(
            "beacons", beacons.ensure_timer,
            lambda: _pairs(beacons.timer_units()), ("helm-beacons.timer",),
            every(beacons.INTERVAL_S),
            "systemctl unavailable; run `helm beacons --post` from another "
            "scheduler"),
        # checkout-watch (task/3301) is the canary's shape: an unchanged
        # install skips the write and the reload, and TIMER_ENV turns it off.
        # Its row was written against checkoutwatch.ensure_timer as it stood
        # at the lane's reviewed tip, before it moved, and held green there.
        _Installer(
            "checkoutwatch", checkoutwatch.ensure_timer,
            lambda: _pairs(checkoutwatch.timer_units()),
            (checkoutwatch.TIMER_NAME,), watch_done,
            "systemctl unavailable; run `helm work checkout-watch --apply` "
            "from another scheduler", idempotent="skip-reload",
            switch=checkoutwatch.TIMER_ENV),
        _Installer(
            "gatecanary", gatecanary.ensure_timer,
            lambda: _pairs(gatecanary.timer_units()),
            (gatecanary.TIMER_NAME,), nightly(gatecanary.TIMER_AT),
            "systemctl unavailable; run `helm gate canary run` nightly from "
            "another scheduler", idempotent="skip-reload",
            switch=gatecanary.TIMER_ENV),
        _Installer(
            "gateshadow", gateshadow.ensure_timer,
            lambda: _pairs(gateshadow.timer_units()),
            (gateshadow.TIMER_NAME,), every(gateshadow.TIMER_INTERVAL_S,
                                            "every"),
            "systemctl unavailable; run `helm gate shadow run` from another "
            "scheduler"),
        _Installer(
            "gc", gc.ensure_timer, lambda: _pairs(gc.timer_units()),
            ("helm-gc.timer",), every(gc.TIMER_INTERVAL_S),
            "systemctl unavailable; run `helm gc --apply` from another "
            "scheduler (cron, a supervisor) — the drain matters more than "
            "the mechanism"),
        _Installer(
            "keepalive", keepalive.ensure_timer,
            lambda: _pairs(keepalive._timer_units()),
            (keepalive.TIMER_NAME,), keepalive_done,
            "systemctl unavailable; run `helm keepalive --apply` from "
            "another scheduler", idempotent="reload", prepare=no_crontab),
        _Installer(
            "offpeak", offpeak.install_timer,
            lambda: _pairs(offpeak.timer_units()), ("helm-offpeak.timer",),
            lambda paths, _s: "enabled helm-offpeak.timer (%s)" % paths[-1],
            None, bare=True, clean=lambda out: _folded(out or "exit 1"),
            failed="systemctl --user failed: %(detail)s"),
        _Installer(
            "owedpush", owedpush.ensure_timer,
            lambda: _pairs(owedpush._timer_units()),
            ("helm-owed-push.timer",),
            every(owedpush.DEFAULT_INTERVAL_S, "owed-push cadence enabled "
                                               "every"),
            "systemctl unavailable; run `helm owed-push` from another "
            "scheduler"),
        # pressure-watch (task/3714) is checkout-watch's shape: an unchanged
        # install skips the write and the reload, and TIMER_ENV turns it off.
        _Installer(
            "pressurewatch", pressurewatch.ensure_timer,
            lambda: _pairs(pressurewatch.timer_units()),
            (pressurewatch.TIMER_NAME,), pressure_done,
            "systemctl unavailable; run `helm pressure-watch --post` from "
            "another scheduler", idempotent="skip-reload",
            switch=pressurewatch.TIMER_ENV),
        _Installer(
            "proxy_fork_watch", proxy_fork_watch.install_timer,
            lambda: _pairs(proxy_fork_watch.timer_units()),
            ("helm-proxy-fork-watch.timer",),
            lambda paths, _s: "enabled daily timer %s" % paths[-1],
            "systemctl unavailable", bare=True,
            clean=lambda out: _folded(out or "exit 1"),
            prepare=fork_watch_ready),
        _Installer(
            "proxywatch", proxywatch.ensure_timer,
            lambda: (_pairs(proxywatch.timer_units())
                     + _pairs(proxywatch.dark_timer_units())),
            ("helm-proxywatch.timer", "helm-proxywatch-dark.timer"),
            lambda paths, _s: (
                "timer enabled every %ds (%s); dark-family recheck every "
                "%ds (%s)" % (proxywatch.INTERVAL_S, paths[1],
                              proxywatch.DARK_INTERVAL_S, paths[3])),
            "systemctl unavailable; run `helm proxywatch --post` from "
            "another scheduler"),
        _Installer(
            "releasenightly", releasenightly.ensure_timer,
            lambda: _pairs(releasenightly.timer_units()),
            (releasenightly.TIMER_NAME,), nightly(releasenightly.TIMER_AT),
            "systemctl unavailable; the other scheduler must set "
            "HELM_RELEASE_NIGHTLY_SOURCE=timer in the command's environment "
            "before running `helm release nightly run --timer` nightly, or "
            "the run records its source as manual", timeout=60,
            idempotent="skip-reload", switch=releasenightly.TIMER_ENV),
        _Installer(
            "remote_relay", remote_relay.ensure_timer, relay_units,
            ("helm-remote-relay.timer",),
            lambda _p, _s: "remote relay cadence enabled every 600s",
            "systemctl unavailable; run `helm remote tick` from another "
            "scheduler", prepare=relay_ready),
        _Installer(
            "seat_lifecycle", seat.ensure_rebind_timer,
            lambda: _pairs(seat.rebind_timer_units()),
            ("helm-seat-rebind.timer",),
            every(seat.REBIND_INTERVAL_S),
            "systemctl unavailable; run `helm seat resume --all --apply` "
            "from another scheduler"),
        _Installer(
            "stalebot", stalebot.ensure_timer,
            lambda: _pairs(stalebot._timer_units()),
            ("helm-stale-bot.timer",),
            every(stalebot.DEFAULT_INTERVAL_S, "stale-bot cadence enabled "
                                               "every"),
            "systemctl unavailable; run `helm stale sweep` from another "
            "scheduler"),
        _Installer(
            "tasksmirror", tasksmirror.ensure_timer,
            lambda: _pairs(tasksmirror._timer_units()),
            ("helm-tasks-mirror.timer",),
            every(tasksmirror.DEFAULT_INTERVAL_S, "mirror cadence enabled "
                                                  "every"),
            "systemctl unavailable; run `helm task mirror --apply` from "
            "another scheduler"),
        _Installer(
            "upstream_watch", upstream_watch.install_timer,
            lambda: _pairs(upstream_watch.timer_units()),
            ("helm-upstream-watch.timer",),
            lambda paths, _s: "enabled the daily timer %s" % paths[-1],
            "systemctl unavailable; run `helm upstream-watch` from another "
            "scheduler", clean=_cut),
    ]


class InstallerContractTest(unittest.TestCase):
    """EVERY INSTALLER KEEPS ITS OWN CONTRACT THROUGH THE SHARED ONE.

    Each installer runs its real code through tests._tmphome.fake_user_systemd:
    its units land under a temp home, a recording systemctl answers, and a
    pass-through spy on subprocess.run records which systemctl each call named
    and its timeout. Every cell below was written against the installers as
    they stood before task/3307 and held green there: the words, the units,
    the verb sequence, the idempotence, the off-switch and the failure text
    are the contract the move had to keep (task/3254's review, findings 4-10).
    """

    def setUp(self):
        self.which_also = {}
        self.env_off = []
        self.seen = []
        # The ORIGINALS, taken once: a subTest's ready() patches over the
        # last one's, and a spy that delegated to the patch below it would
        # record each call once per layer.
        self.real_which, self.real_run = shutil.which, subprocess.run

    def ready(self, spec, rc=0, stderr="", systemctl=True):
        """Fake the installer's world; -> the FakeUserSystemd."""
        fake = fake_user_systemd(self, rc=rc, stderr=stderr)
        if spec.prepare:
            spec.prepare(self)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for key in [spec.switch] + self.env_off:
            if key:
                os.environ.pop(key, None)

        def which(name, *a, **kw):
            if name == "systemctl" and not systemctl:
                return None
            if name in self.which_also:
                return self.which_also[name]
            return self.real_which(name, *a, **kw)

        def run(argv, *a, **kw):
            if argv and os.path.basename(str(argv[0])) == "systemctl":
                self.seen.append((list(argv), kw.get("timeout")))
            return self.real_run(argv, *a, **kw)
        for patch in (mock.patch("shutil.which", side_effect=which),
                      mock.patch.object(subprocess, "run", side_effect=run)):
            patch.start()
            self.addCleanup(patch.stop)
        return fake

    def program(self, spec, fake):
        return "systemctl" if spec.bare else fake.systemctl

    def listing(self, fake):
        try:
            return sorted(os.listdir(fake.unit_dir))
        except FileNotFoundError:
            return []

    def check_units(self, spec, fake):
        units = spec.units()
        self.assertEqual({os.path.dirname(p) for p, _t in units},
                         {fake.unit_dir}, spec.name)
        self.assertEqual(self.listing(fake),
                         sorted(os.path.basename(p) for p, _t in units))
        for path, text in units:
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), text, path)
        return units

    def test_a_fresh_install_writes_its_units_and_says_its_own_words(self):  # noqa: VACUOUS_ASSERTION — the loop is over the non-empty literal table of nineteen installers and every row asserts ok, its exact report, its unit files byte for byte and its exact systemctl calls
        for spec in _installers():
            with self.subTest(spec.name):
                self.seen[:] = []
                fake = self.ready(spec)
                ok, detail = spec.install()
                self.assertIs(ok, True, detail)
                units = self.check_units(spec, fake)
                self.assertEqual(detail, spec.done([p for p, _t in units],
                                                   "fresh"))
                self.assertEqual(fake.calls(),
                                 [_RELOAD, _enable(*spec.timers)])
                program = self.program(spec, fake)
                self.assertEqual(self.seen, [
                    ([program] + _RELOAD, spec.timeout),
                    ([program] + _enable(*spec.timers), spec.timeout)])

    def test_a_second_install_is_idempotent_exactly_where_it_was(self):  # noqa: VACUOUS_ASSERTION — the loop is over the non-empty literal table; every row asserts the second report and the second call list positively
        for spec in _installers():
            with self.subTest(spec.name):
                fake = self.ready(spec)
                ok, _first = spec.install()
                self.assertIs(ok, True, _first)
                units = self.check_units(spec, fake)
                paths = [p for p, _t in units]
                stamps = [(os.stat(p).st_ino, os.stat(p).st_mtime_ns)
                          for p in paths]
                ok, detail = spec.install()
                self.assertIs(ok, True, detail)
                self.check_units(spec, fake)
                again = fake.calls()[2:]
                after = [(os.stat(p).st_ino, os.stat(p).st_mtime_ns)
                         for p in paths]
                if spec.idempotent is None:
                    self.assertEqual(detail, spec.done(paths, "fresh"))
                    self.assertEqual(again, [_RELOAD, _enable(*spec.timers)])
                    self.assertNotEqual(after, stamps, "not rewritten")
                    continue
                self.assertEqual(detail, spec.done(paths, "unchanged"))
                self.assertEqual(after, stamps, "an unchanged unit moved")
                self.assertEqual(again, (
                    [_enable(*spec.timers)]
                    if spec.idempotent == "skip-reload"
                    else [_RELOAD, _enable(*spec.timers)]))

    def test_keepalive_says_refreshed_when_its_unit_had_drifted(self):
        spec = [s for s in _installers() if s.name == "keepalive"][0]
        fake = self.ready(spec)
        spec.install()
        paths = [p for p, _t in spec.units()]
        with open(paths[-1], "w", encoding="utf-8") as fh:
            fh.write("[Timer]\nOnUnitActiveSec=1s\n")
        ok, detail = spec.install()
        self.assertIs(ok, True, detail)
        self.assertIn("keepalive cadence refreshed every", detail)
        self.assertEqual(detail, spec.done(paths, "refreshed"))
        self.check_units(spec, fake)
        self.assertEqual(fake.calls()[2:], [_RELOAD, _enable(*spec.timers)])

    def test_an_off_switch_writes_nothing_and_runs_nothing(self):  # noqa: VACUOUS_ASSERTION — the loop is over the six installers with a switch; each asserts ok is None and its exact report, and the fresh-install arm drives the same seam to write and run
        switched = [s for s in _installers() if s.switch]
        self.assertEqual(sorted(s.name for s in switched),
                         ["autocompact", "autoland", "checkoutwatch",
                          "gatecanary", "pressurewatch",
                          "releasenightly"])
        for spec in switched:
            with self.subTest(spec.name):
                fake = self.ready(spec)
                os.environ[spec.switch] = "0"
                ok, detail = spec.install()
                self.assertIsNone(ok, detail)
                self.assertEqual(detail, "install skipped by %s=0: no unit "
                                 "file written, no systemctl run"
                                 % spec.switch)
                self.assertFalse(os.path.exists(fake.unit_dir))
                self.assertEqual(fake.calls(), [])

    def test_no_systemctl_on_the_host_is_each_installers_own_sentence(self):  # noqa: VACUOUS_ASSERTION — the loop is over the non-empty table of installers that ask which(); each asserts ok is False and its exact report, and the fresh-install arm drives the same seam to write and run
        asking = [s for s in _installers() if s.missing]
        self.assertEqual(len(asking), 18)
        for spec in asking:
            with self.subTest(spec.name):
                fake = self.ready(spec, systemctl=False)
                ok, detail = spec.install()
                self.assertIs(ok, False)
                self.assertEqual(detail, spec.missing)
                self.assertFalse(os.path.exists(fake.unit_dir))
                self.assertEqual(fake.calls(), [])

    def test_a_failing_systemctl_is_reported_in_each_installers_words(self):  # noqa: VACUOUS_ASSERTION — the loop is over the non-empty literal table; every row asserts ok is False, its exact report, its written units and the one call it made
        for spec in _installers():
            with self.subTest(spec.name):
                fake = self.ready(spec, rc=1, stderr=_NOISE)
                ok, detail = spec.install()
                self.assertIs(ok, False)
                self.assertEqual(detail, spec.failed % {
                    "cmd": " ".join([self.program(spec, fake)] + _RELOAD),
                    "detail": spec.clean(_NOISE)})
                self.check_units(spec, fake)
                self.assertEqual(fake.calls(), [_RELOAD])

    def test_a_silent_failure_says_what_little_it_knows(self):  # noqa: VACUOUS_ASSERTION — the loop is over the non-empty literal table; every row asserts its exact report
        for spec in _installers():
            with self.subTest(spec.name):
                fake = self.ready(spec, rc=1)
                ok, detail = spec.install()
                self.assertIs(ok, False)
                self.assertEqual(detail, spec.failed % {
                    "cmd": " ".join([self.program(spec, fake)] + _RELOAD),
                    "detail": spec.clean("")})

    def test_an_unchanged_install_that_fails_names_the_enable(self):  # noqa: VACUOUS_ASSERTION — the loop is over the five skip-reload installers; each asserts its exact report and the one call the failing fake saw
        for spec in [s for s in _installers()
                     if s.idempotent == "skip-reload"]:
            with self.subTest(spec.name):
                self.ready(spec)
                ok, detail = spec.install()
                self.assertIs(ok, True, detail)
                failing = fake_user_systemd(self, rc=1, stderr="no bus\n",
                                            home=False)
                ok, detail = spec.install()
                self.assertIs(ok, False)
                self.assertEqual(detail, "%s --user enable --now %s failed: "
                                 "no bus" % (failing.systemctl,
                                             " ".join(spec.timers)))
                self.assertEqual(failing.calls(), [_enable(*spec.timers)])

    def test_a_spawn_that_raises_is_reported_where_it_always_was(self):  # noqa: VACUOUS_ASSERTION — the loop is over the three installers that caught OSError before task/3307; each asserts ok is False and its exact report
        gone = FileNotFoundError(2, "No such file or directory", "systemctl")
        caught = [s for s in _installers()
                  if s.name in ("offpeak", "proxy_fork_watch",
                                "upstream_watch")]
        for spec in caught:
            with self.subTest(spec.name):
                fake = self.ready(spec)

                def run(argv, *a, **kw):
                    if os.path.basename(str(argv[0])) == "systemctl":
                        raise gone
                    return self.real_run(argv, *a, **kw)
                with mock.patch.object(subprocess, "run", side_effect=run):
                    ok, detail = spec.install()
                self.assertIs(ok, False)
                self.assertEqual(detail, spec.failed % {
                    "cmd": " ".join([self.program(spec, fake)] + _RELOAD),
                    "detail": gone})

    # -- the move itself: these two were red before task/3307 --------------

    def test_every_installer_writes_through_the_one_installer(self):  # noqa: VACUOUS_ASSERTION — the loop is over the non-empty literal table; every row asserts one call with its exact unit paths and timers
        for spec in _installers():
            with self.subTest(spec.name):
                self.ready(spec)
                shared = mock.Mock(return_value=(None, False))
                with mock.patch.object(timerhealth, "install_user_timer",
                                       shared, create=True):
                    spec.install()
                self.assertEqual(shared.call_count, 1,
                                 "%s installed without the shared installer"
                                 % spec.name)
                units, timers = shared.call_args[0][:2]
                self.assertEqual([p for p, _t in units],
                                 [p for p, _t in spec.units()])
                self.assertEqual(tuple(timers), spec.timers)

    def test_every_unit_path_comes_from_the_one_unit_dir(self):  # noqa: VACUOUS_ASSERTION — the loop is over the non-empty literal table plus the three other writers and readers; every row asserts every path's directory
        from helm import chat, chatnode
        planted = os.path.join(tempfile.mkdtemp(prefix="helm-test-udir-"),
                               "units")
        self.addCleanup(shutil.rmtree, os.path.dirname(planted), True)
        renders = [(s.name, s.units) for s in _installers()
                   if s.name != "remote_relay"]
        renders += [
            ("chat", lambda: _pairs(chat._logflush_timer_units())),
            ("chatnode", lambda: [(chatnode.unit_path(), ""),
                                  (os.path.join(chatnode._dropin_dir(
                                      "x.service"), "10.conf"), "")])]
        for name, render in renders:
            with self.subTest(name), mock.patch.object(
                    timerhealth, "user_unit_dir", create=True,
                    return_value=planted):
                dirs = {os.path.dirname(p) for p, _t in render()}
                self.assertTrue(dirs, name)
                self.assertEqual({d.replace("/x.service.d", "")
                                  for d in dirs}, {planted}, name)



# ---------------------------------------------------------------------------
# UNIT DRIFT (task/3405): the installed unit against its module's template
# ---------------------------------------------------------------------------

def _plant(path, text):
    """Write one unit file the way a past install left it."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(text, bytes) else "w"
    with open(path, mode) as fh:
        fh.write(text)


def _rows_of(rows, module):
    """{unit file name: row} for one module's rows."""
    return {r["unit"]: r for r in rows if r["module"] == module}


def _snapshot(unit_dir):
    """Every file under the unit directory: (inode, mtime, bytes)."""
    out = {}
    for name in sorted(os.listdir(unit_dir)):
        path = os.path.join(unit_dir, name)
        if os.path.isfile(path):
            st = os.stat(path)
            with open(path, "rb") as fh:
                out[name] = (st.st_ino, st.st_mtime_ns, fh.read())
    return out


def _timer_renderers():
    """Every helm module holding a unit template with a [Timer] section,
    named the way the drift table names it (dotted, relative to helm/).

    IT WALKS THE WHOLE PACKAGE, subpackages too, and reads string constants
    through ast, so a timer added anywhere under helm/ is counted whether or
    not it calls the shared installer."""
    import ast
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pkg = os.path.join(here, "helm")
    found = set()
    for dirpath, dirnames, filenames in os.walk(pkg):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in sorted(filenames):
            if not name.endswith(".py"):
                continue
            full = os.path.join(dirpath, name)
            with open(full, encoding="utf-8") as fh:
                src = fh.read()
            if "[Timer]" not in src:
                continue
            for node in ast.walk(ast.parse(src)):
                if isinstance(node, ast.Constant) \
                        and isinstance(node.value, str) \
                        and node.value.startswith("[Unit]") \
                        and "\n[Timer]\n" in node.value:
                    rel = os.path.relpath(full, pkg)[:-len(".py")]
                    found.add(rel.replace(os.sep, "."))
    return found


class _ChatLogflush(object):
    """chat's log-flush installer as an installer-table row: it keeps its own
    write loop (timerhealth's installer section says why), prints instead of
    returning (ok, detail), and runs the `systemctl` on PATH."""

    name = "chat"
    prepare = None
    switch = None

    @staticmethod
    def install():
        import contextlib
        import io
        from helm import chat
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = chat._logflush_install_timer(["--install-timer", "--apply"])
        return rc == 0, out.getvalue()


class UnitDriftTest(unittest.TestCase):
    """THE INSTALLED TEXT AGAINST THE TEMPLATE THAT WOULD WRITE IT NOW.

    helm-release-nightly.service was installed before its template gained
    Environment=HELM_RELEASE_NIGHTLY_SOURCE=timer, so every timer night
    recorded as MANUAL and the release streak could never count, while the
    timer census read the unit healthy: it elapsed and it fired. Every arm
    here writes units under a temp home (tests._tmphome.fake_user_systemd)
    and reads them back through timerhealth.drift."""

    setUp = InstallerContractTest.setUp
    ready = InstallerContractTest.ready

    def nightly_without_its_source_line(self):
        """-> the nightly module, after planting the SOURCE_ENV shape."""
        from helm import releasenightly
        spath, service, tpath, timer = releasenightly.timer_units()
        line = "Environment=%s=timer\n" % releasenightly.SOURCE_ENV
        self.assertIn(line, service)
        _plant(spath, service.replace(line, ""))
        _plant(tpath, timer)
        return releasenightly

    # -- (1) RED on trunk: the nightly shape reads DRIFTED -----------------

    def test_a_unit_missing_a_line_its_template_now_renders_is_DRIFTED(self):
        fake_user_systemd(self)
        nightly = self.nightly_without_its_source_line()
        got = _rows_of(timerhealth.drift(), "releasenightly")
        row = got[nightly.SERVICE_NAME]
        self.assertEqual(row["verdict"], timerhealth.DRIFTED, row)
        self.assertIn("missing Environment=HELM_RELEASE_NIGHTLY_SOURCE=timer",
                      row["detail"])
        self.assertEqual(row["command"], "helm release nightly --install-timer")
        # THE CONTROL IN THE SAME READ: the pair's other unit holds exactly
        # what its template renders, so the census compared line by line
        # rather than calling the whole module drifted.
        self.assertEqual(got[nightly.TIMER_NAME]["verdict"], timerhealth.CLEAN)

    def test_a_changed_command_line_and_a_dropped_line_are_both_named(self):
        from helm import gatecanary
        fake_user_systemd(self)
        spath, service, tpath, timer = gatecanary.timer_units()
        old = service.replace(" gate canary run ", " gate canary nightly ")
        self.assertNotEqual(old, service)
        _plant(spath, old + "Restart=on-failure\n")
        _plant(tpath, timer)
        row = _rows_of(timerhealth.drift(), "gatecanary")[
            gatecanary.SERVICE_NAME]
        self.assertEqual(row["verdict"], timerhealth.DRIFTED, row)
        self.assertIn("extra Restart=on-failure", row["detail"])
        self.assertIn("extra ExecStart=", row["detail"])
        self.assertIn("gate canary nightly", row["detail"])
        self.assertIn("missing ExecStart=", row["detail"])
        self.assertEqual(row["command"], "helm gate canary --install-timer")

    def test_a_half_written_install_is_DRIFTED_not_absent(self):
        """An install the installer now writes as FOUR files, found as the two
        an older proxywatch wrote before the dark-family recheck existed."""
        from helm import proxywatch
        fake_user_systemd(self)
        spath, service, tpath, timer = proxywatch.timer_units()
        _plant(spath, service)
        _plant(tpath, timer)
        got = _rows_of(timerhealth.drift(), "proxywatch")
        self.assertEqual(got["helm-proxywatch.service"]["verdict"],
                         timerhealth.CLEAN)
        dark = got["helm-proxywatch-dark.timer"]
        self.assertEqual(dark["verdict"], timerhealth.DRIFTED, dark)
        self.assertIn("not installed", dark["detail"])
        self.assertEqual(dark["command"], "helm proxywatch --install-timer")

    # -- (2) RED on trunk: doctor and rearm surface it ---------------------

    def test_doctor_names_the_drifted_unit_and_the_line_that_re_installs_it(self):
        fake_user_systemd(self)
        nightly = self.nightly_without_its_source_line()
        out = doctor.check_unit_drift()
        self.assertEqual([lvl for lvl, _line in out], [doctor.WARN], out)
        text = out[0][1]
        self.assertIn(nightly.SERVICE_NAME, text)
        self.assertIn("DRIFTED", text)
        self.assertIn("Environment=HELM_RELEASE_NIGHTLY_SOURCE=timer", text)
        self.assertIn("`helm release nightly --install-timer`", text)

    def test_the_drift_rung_is_REGISTERED_so_doctor_runs_it(self):
        self.assertIn("check_unit_drift", doctor.CHECKS)
        self.assertEqual(doctor.CHECKS.index("check_unit_drift"),
                         doctor.CHECKS.index("check_timers") + 1)

    # -- (3) control: every module's own install reads CLEAN ---------------

    def test_every_module_that_renders_a_timer_has_a_drift_template(self):
        """A NEW MODULE WITHOUT COVERAGE FAILS HERE. The walk is the census
        of what renders a timer; the table is what the drift census reads."""
        renders = _timer_renderers()
        self.assertIn("releasenightly", renders)      # the walk sees one
        table = {row.module for row in timerhealth.UNIT_TEMPLATES}
        self.assertEqual(sorted(renders - table), [],
                         "renders a [Timer] the drift census never reads")
        self.assertEqual(sorted(table - renders), [],
                         "a drift template for a module that renders none")

    def test_a_unit_its_own_installer_wrote_reads_CLEAN_for_every_module(self):  # noqa: VACUOUS_ASSERTION — the loop is over the installer table plus chat, asserted equal to the drift table first; every row installs through its real installer and asserts its exact unit set and one CLEAN verdict
        specs = _installers() + [_ChatLogflush()]
        self.assertEqual(sorted(s.name for s in specs),
                         sorted({r.module for r in timerhealth.UNIT_TEMPLATES}))
        for spec in specs:
            with self.subTest(spec.name):
                fake = self.ready(spec)
                ok, detail = spec.install()
                self.assertIs(ok, True, detail)
                rows = _rows_of(timerhealth.drift(), spec.name)
                self.assertEqual(sorted(rows), sorted(os.listdir(fake.unit_dir)))
                self.assertEqual({r["verdict"] for r in rows.values()},
                                 {timerhealth.CLEAN}, rows)

    def test_every_declared_input_is_a_wildcard_in_its_render(self):  # noqa: VACUOUS_ASSERTION — the loop is over the non-empty drift table; every row asserts each declared input appears as a wildcard in its own render
        self.assertTrue(timerhealth.UNIT_TEMPLATES)
        for row in timerhealth.UNIT_TEMPLATES:
            with self.subTest("%s.%s" % (row.module, row.render)):
                text = "".join(t for _p, t in timerhealth.template_units(row))
                for name in tuple(row.wild) + tuple(b for b, _ in row.blocks):
                    self.assertIn(timerhealth.wildcard(name), text, name)

    # -- (4) control: per-install inputs are not drift ---------------------

    def test_another_home_checkout_interval_and_knobs_do_not_read_as_drift(self):
        """Each module's own render, given inputs no install on this box
        would use, reads CLEAN: the census recovers them from the text."""
        fake_user_systemd(self)
        other = {"helm": "/fixture/elsewhere/.local/bin/helm",
                 "cwd": "/fixture/srv/another checkout",
                 "env": "/fixture/elsewhere/.config/helm/fixture.env",
                 "interval": "77"}
        blocks = {"knobs": "Environment=HELM_REMOTE_FIXTURE=1\n"
                           "Environment=HELM_REMOTE_GH=/fixture/bin/gh\n",
                  "env": "Environment=HELM_UPSTREAM_WATCH_CLAUDE="
                         "/fixture/bin/claude\n"}
        planted = 0
        for row in timerhealth.UNIT_TEMPLATES:
            inputs = {k: other[k] for k in row.wild}
            inputs.update((b, blocks[b]) for b, _prefix in row.blocks)
            for path, text in timerhealth.template_units(row, inputs):
                _plant(path, text)
                planted += 1
        rows = timerhealth.drift()
        self.assertEqual(len(rows), planted)
        self.assertEqual({r["verdict"] for r in rows}, {timerhealth.CLEAN},
                         [r for r in rows if r["verdict"] != timerhealth.CLEAN])

    def test_the_reinstall_line_carries_the_inputs_it_recovered(self):
        """A drifted unit's command re-installs with the SAME inputs: the
        interval a past `--interval` chose, and the environment knobs the
        installer copied into the unit from its own environment."""
        fake_user_systemd(self)
        ac = [r for r in timerhealth.UNIT_TEMPLATES
              if r.module == "autocompact"][0]
        (spath, service), (tpath, timer) = timerhealth.template_units(
            ac, {"helm": "/fixture/bin/helm", "cwd": "/fixture/co",
                 "interval": "77"})
        _plant(spath, service + "Nice=5\n")
        _plant(tpath, timer)
        rr = [r for r in timerhealth.UNIT_TEMPLATES
              if r.module == "remote_relay"][0]
        (rspath, rservice), (rtpath, rtimer) = timerhealth.template_units(
            rr, {"helm": "/fixture/bin/helm", "cwd": "/fixture/co",
                 "interval": "600",
                 "knobs": "Environment=HELM_REMOTE_TICK_INTERVAL_S=600\n"
                          "Environment=HELM_REMOTE_GH=/fixture/bin/g h\n"})
        _plant(rspath, rservice.replace("ExecStart=", "ExecStartPre=/bin/true\n"
                                        "ExecStart="))
        _plant(rtpath, rtimer)
        rows = timerhealth.drift()
        self.assertEqual(
            _rows_of(rows, "autocompact")["helm-autocompact.service"]["command"],
            "helm seat autocompact --install-timer --apply --interval 77")
        self.assertEqual(
            _rows_of(rows, "remote_relay")["helm-remote-relay.service"]
            ["command"],
            "HELM_REMOTE_TICK_INTERVAL_S=600 HELM_REMOTE_GH='/fixture/bin/g h'"
            " helm remote ensure-timer")

    def test_the_census_reads_nothing_of_its_own_home_or_checkout(self):  # noqa: VACUOUS_ASSERTION — the loop is over the non-empty drift table; every row asserts its wildcard render is byte-identical under another home, config home and checkout
        from helm import work
        fake_user_systemd(self)
        before = [[t for _p, t in timerhealth.template_units(row)]
                  for row in timerhealth.UNIT_TEMPLATES]
        self.assertTrue(before)
        elsewhere = tempfile.mkdtemp(prefix="helm-test-drift-home-")
        self.addCleanup(shutil.rmtree, elsewhere, True)
        with mock.patch.dict(os.environ, {"HOME": elsewhere,
                                          "XDG_CONFIG_HOME": elsewhere}), \
                mock.patch.object(work, "find_root",
                                  return_value="/fixture/other/checkout"):
            after = [[t for _p, t in timerhealth.template_units(row)]
                     for row in timerhealth.UNIT_TEMPLATES]
        for row, was, now in zip(timerhealth.UNIT_TEMPLATES, before, after):
            with self.subTest(row.module):
                self.assertEqual(now, was)

    def test_the_build_host_is_config_the_unit_names_never_unit_text(self):  # noqa: VACUOUS_ASSERTION — the loop is over the two modules that take --host; each asserts its env file changed and its units read CLEAN under both hosts
        from helm import gatecanary, releasenightly
        fleet = mock.patch.object(releasenightly, "fab_hosts",
                                  return_value=(["build-a", "build-b"], None))
        fleet.start()
        self.addCleanup(fleet.stop)
        for spec in [s for s in _installers()
                     if s.name in ("gatecanary", "releasenightly")]:
            with self.subTest(spec.name):
                fake = self.ready(spec)
                # THE ENV FILE LANDS UNDER THE TEMP HOME, whatever the runner's
                # own config home says; ready() restores the environment.
                os.environ["XDG_CONFIG_HOME"] = os.path.join(fake.home, "xdg")
                mod = {"gatecanary": gatecanary,
                       "releasenightly": releasenightly}[spec.name]
                for host in ("build-a", "build-b"):
                    ok, detail = mod.write_host(host)
                    self.assertIs(ok, True, detail)
                    ok, detail = spec.install()
                    self.assertIs(ok, True, detail)
                    with open(mod.config_env_path(), encoding="utf-8") as fh:
                        self.assertIn(host, fh.read())
                    rows = _rows_of(timerhealth.drift(), spec.name)
                    self.assertEqual(len(rows), 2)
                    self.assertEqual({r["verdict"] for r in rows.values()},
                                     {timerhealth.CLEAN})

    # -- (5) control: what cannot be compared is UNKNOWN -------------------

    def test_an_unreadable_unit_is_UNKNOWN_never_clean_or_drifted(self):
        from helm import gc
        fake_user_systemd(self)
        spath, _service, tpath, timer = gc.timer_units()
        os.makedirs(spath)                      # a directory where a unit was
        _plant(tpath, timer)
        got = _rows_of(timerhealth.drift(), "gc")
        self.assertEqual(got["helm-gc.service"]["verdict"],
                         timerhealth.UNKNOWN)
        self.assertIn("unreadable", got["helm-gc.service"]["detail"])
        self.assertEqual(got["helm-gc.timer"]["verdict"], timerhealth.CLEAN)
        _plant(tpath, b"[Timer]\nOnBootSec=\xff\xfe\n")
        got = _rows_of(timerhealth.drift(), "gc")
        self.assertEqual(got["helm-gc.timer"]["verdict"], timerhealth.UNKNOWN)
        self.assertIn("UTF-8", got["helm-gc.timer"]["detail"])

    def test_inputs_that_disagree_with_each_other_are_UNKNOWN(self):
        """The canary's service names its checkout twice. When the two say
        different things, which one the installer passed cannot be recovered;
        the verdict is neither clean nor drifted."""
        from helm import gatecanary
        fake_user_systemd(self)
        row = [r for r in timerhealth.UNIT_TEMPLATES
               if r.module == "gatecanary"][0]
        (spath, service), (tpath, timer) = timerhealth.template_units(
            row, {"helm": "/fixture/bin/helm", "cwd": "/fixture/a",
                  "env": "/fixture/gate-canary.env"})
        _plant(spath, service.replace("--repo /fixture/a", "--repo /fixture/b"))
        _plant(tpath, timer)
        got = _rows_of(timerhealth.drift(), "gatecanary")
        service_row = got[gatecanary.SERVICE_NAME]
        self.assertEqual(service_row["verdict"], timerhealth.UNKNOWN,
                         service_row)
        self.assertIn("cwd", service_row["detail"])
        self.assertIn("/fixture/b", service_row["detail"])
        self.assertEqual(got[gatecanary.TIMER_NAME]["verdict"],
                         timerhealth.CLEAN)

    def test_a_template_that_cannot_be_rendered_is_UNKNOWN(self):
        from helm import releasenightly
        fake_user_systemd(self)
        with mock.patch.object(releasenightly, "timer_units",
                               side_effect=RuntimeError("no render")):
            got = _rows_of(timerhealth.drift(), "releasenightly")
        self.assertEqual([r["verdict"] for r in got.values()],
                         [timerhealth.UNKNOWN])
        self.assertIn("RuntimeError", list(got.values())[0]["detail"])

    def test_doctor_reports_UNKNOWN_as_unmeasured_and_never_counts_it(self):
        from helm import gc
        fake_user_systemd(self)
        spath, _service, tpath, timer = gc.timer_units()
        os.makedirs(spath)
        _plant(tpath, timer)
        out = doctor.check_unit_drift()
        self.assertEqual([lvl for lvl, _line in out], [doctor.WARN], out)
        self.assertIn("UNMEASURED", out[0][1])
        self.assertIn("helm-gc.service", out[0][1])

    def test_a_clean_box_says_how_many_units_it_compared(self):
        from helm import gc
        fake_user_systemd(self)
        spath, service, tpath, timer = gc.timer_units()
        _plant(spath, service)
        _plant(tpath, timer)
        out = doctor.check_unit_drift()
        self.assertEqual([lvl for lvl, _line in out], [doctor.OK], out)
        self.assertIn("2 of 2", out[0][1])

    # -- (6) control: the check writes nothing and reloads nothing ---------

    def test_the_census_writes_no_unit_and_runs_no_systemctl(self):  # noqa: VACUOUS_ASSERTION — the empty call list is the claim; the same read's rows carry a DRIFTED and a CLEAN verdict, so the census did its work
        from helm import gc, rearm
        fake = fake_user_systemd(self)
        self.nightly_without_its_source_line()
        spath, service, tpath, timer = gc.timer_units()
        _plant(spath, service)
        _plant(tpath, timer)
        before = _snapshot(fake.unit_dir)
        refuse = AssertionError("the drift census wrote")
        with mock.patch.object(timerhealth, "install_user_timer",
                               side_effect=refuse), \
                mock.patch("helm.pk.atomic_write", side_effect=refuse):
            rows = timerhealth.drift()
            out = doctor.check_unit_drift()
            found = rearm._unit_drift()
        verdicts = sorted({r["verdict"] for r in rows
                           if r["verdict"] != timerhealth.ABSENT})
        self.assertEqual(verdicts, [timerhealth.CLEAN, timerhealth.DRIFTED])
        self.assertIn(doctor.WARN, [lvl for lvl, _line in out])
        self.assertEqual([r["unit"] for r in found],
                         ["helm-release-nightly.service"])
        self.assertEqual(_snapshot(fake.unit_dir), before)
        self.assertEqual(fake.calls(), [])


# ---------------------------------------------------------------------------
# ENVIRONMENT= ASSIGNMENTS (task/3423): what systemd reads from a knob line
# ---------------------------------------------------------------------------

# WHAT systemd 259 READ from each Environment= value, measured on the dev box
# through `systemd --test --user` (its parse dump) and `systemd-analyze
# verify`: the words, before specifiers. A word it cannot read (an unclosed
# quote, an escape outside its table) STOPS the line there: the words before it
# are set, and it and the rest are not, although the log line ("Invalid
# syntax, ignoring: ...") quotes the whole line (task/3423, measured: `DREGG_A=0
# "unclosed` sets DREGG_A=0). A word that is no NAME=value it ignored
# ("Invalid environment assignment, ignoring: h").
_SYSTEMD_259 = (
    ("HELM_REMOTE_GH=/fixture/bin/g h", ["HELM_REMOTE_GH=/fixture/bin/g", "h"]),
    ('"HELM_REMOTE_GH=/fixture/bin/g h"', ["HELM_REMOTE_GH=/fixture/bin/g h"]),
    ("DREGG_B=1 AWS_B=y", ["DREGG_B=1", "AWS_B=y"]),
    ('HELM_REMOTE_X=a"b', []),
    ("HELM_REMOTE_X=it's", []),
    ("HELM_REMOTE_X=C:\\path", []),
    ("DREGG_A=1\\ AWS_SECRET_ACCESS_KEY=x", []),
    ('DREGG_A=0 "unclosed', ["DREGG_A=0"]),
    ("DREGG_A=0 B=x\\q C=1", ["DREGG_A=0"]),
    ("B=x\\q DREGG_A=0", []),
    ('DREGG_A=0 X=a"b', ["DREGG_A=0"]),
    ('O_MID=a"b c"d', ["O_MID=ab cd"]),
    ("'O_SQ=a\\\\b c'", ["O_SQ=a\\b c"]),
    ("O_BS=a\\\\b", ["O_BS=a\\b"]),
    ('"O_SPC=a\\sb"', ["O_SPC=a b"]),
    ('"O_TAB=a\\tb"', ["O_TAB=a\tb"]),
    ('"K=\\"\'\\\\%%"', ['K="\'\\%%']),
    ("K=", ["K="]),
)

_ESCAPES = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t",
            "v": "\v", "\\": "\\", '"': '"', "'": "'", "s": " "}


def _set_by(words):
    """{name: value} systemd sets from its `words`: "%%" is one "%", a lone
    "%" fails the arm (systemd would expand it as a specifier), and a word
    that is no NAME=value is ignored, as systemd ignores it."""
    out = {}
    for word in words:
        if "%" in word.replace("%%", ""):
            raise AssertionError("a specifier systemd would expand: %r" % word)
        name, sep, value = word.partition("=")
        if sep and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            out[name] = value.replace("%%", "%")
    return out


def _systemd_reads(value):
    """{name: value} systemd sets from one Environment= line's `value`. AN
    ORACLE INDEPENDENT OF THE MODULE UNDER TEST, written from systemd.exec(5)
    and systemd.syntax(7) "Quoting" and pinned to _SYSTEMD_259: unquoted
    whitespace ends a word, a quote opens a quoted run anywhere in a word,
    the named C escapes are read in and out of quotes, and any other escape
    or an unclosed quote stops the line: the words before that word are set.
    A numeric escape fails the arm: no writer writes one, and this oracle
    does not model it."""
    words, word, quote, i = [], None, None, 0
    while i < len(value):
        c, i = value[i], i + 1
        if c == "\\":
            e = value[i:i + 1]
            if e and e in "xuU01234567":
                raise AssertionError("a numeric escape: %r" % value)
            if e not in _ESCAPES:
                return _set_by(words)
            word, i = (word or "") + _ESCAPES[e], i + 1
        elif quote:
            quote, word = (None, word) if c == quote else (quote, word + c)
        elif c in "\"'":
            quote, word = c, word or ""
        elif c in " \t\n\r":
            if word is not None:
                words.append(word)
            word = None
        else:
            word = (word or "") + c
    if quote:
        return _set_by(words)
    return _set_by(words + ([] if word is None else [word]))


def _environment_set(text):
    """{name: value} every Environment= line of a unit `text` sets, in
    order."""
    out = {}
    for line in text.split("\n"):
        if line.startswith("Environment="):
            out.update(_systemd_reads(line[len("Environment="):]))
    return out


def _writers():
    """(writer, knob, render): every helm module that writes an Environment=
    line from a value it does not choose (the sweep of task/3423), and the
    unit text it writes for (knob, value)."""
    from helm import chatnode, remote_relay, upstream_watch

    def relay(knob, value):
        return remote_relay._unit_knobs({
            "HELM_REMOTE_CLAUDE": "/fixture/bin/claude",
            "HELM_REMOTE_GH": "/fixture/bin/gh", knob: value})

    def watch(knob, value):
        return upstream_watch.timer_units(env={
            "HELM_UPSTREAM_WATCH_CLAUDE": "/fixture/bin/claude",
            knob: value})[1]

    def mirror(knob, value):
        """The mirror is handed each value as the running peer has it, after
        systemd expanded its specifiers (task/3432)."""
        path = chatnode.write_posture_dropin(
            [("the fixture peer's Environment", "%s=%s" % (knob, value))])
        if path is None:
            return ""
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    return (("remote_relay", "HELM_REMOTE_FIXTURE_PATH", relay),
            ("upstream_watch claude", "HELM_UPSTREAM_WATCH_CLAUDE", watch),
            ("upstream_watch home", "HELM_UPSTREAM_WATCH_CLAUDE_HOME", watch),
            # A POSTURE NAME: the mirror writes no other (task/3423,
            # meta-claude's A), and its renderer is what this reads.
            ("chatnode posture", "DREGG_ALLOW_UNAUDITED_PQ", mirror))


class EnvAssignmentTest(unittest.TestCase):
    """A UNIT'S Environment= LINE IS NOT KEY=VALUE TEXT (task/3423).

    systemd splits each line on whitespace, reads quotes and C escapes, and
    expands "%" specifiers, and every writer below wrote its value raw: a
    claude path with a space set the path's first word, and a quote or a
    backslash dropped the whole line. Each writer's own text is read back
    through an oracle pinned to what systemd 259 read."""

    def setUp(self):
        fake_user_systemd(self)

    def assert_round_trip(self, value):
        for writer, knob, render in _writers():
            with self.subTest(writer):
                text = render(knob, value)
                self.assertEqual(_environment_set(text).get(knob), value, text)
                self.assertIn('\nEnvironment="%s=' % knob, text)
        self.assertEqual(
            _systemd_reads(timerhealth.env_assignment("HELM_REMOTE_V", value)),
            {"HELM_REMOTE_V": value})

    # -- the oracle against systemd 259 ------------------------------------

    def test_the_oracle_reads_each_line_as_systemd_259_did(self):  # noqa: VACUOUS_ASSERTION — the loop is over the non-empty measured table, asserted non-empty first; every row asserts the oracle's exact result
        """Only the oracle reads Environment= text now: the posture mirror
        reads the running peer's own environment instead (task/3432)."""
        self.assertTrue(_SYSTEMD_259)
        for line, words in _SYSTEMD_259:
            with self.subTest(line):
                self.assertEqual(_systemd_reads(line), _set_by(words))

    # -- RED on the base: special characters reach systemd intact ----------

    def test_a_value_with_a_space_round_trips_through_every_writer(self):  # noqa: VACUOUS_ASSERTION — assert_round_trip asserts the helper's exact read, then loops over the non-empty literal writer table; every row asserts its exact value and its quoted line
        self.assert_round_trip("/fixture/g h/bin")

    def test_a_quote_and_a_backslash_round_trip_through_every_writer(self):  # noqa: VACUOUS_ASSERTION — assert_round_trip asserts the helper's exact read, then loops over the non-empty literal writer table; every row asserts its exact value and its quoted line
        self.assert_round_trip("/fixture/it's \"q\"\\b/bin")

    def test_a_percent_sign_is_a_percent_sign_not_a_specifier(self):  # noqa: VACUOUS_ASSERTION — the loop is over the non-empty literal writer table; every row asserts its exact value, and the helper's exact read follows
        """systemd expands %h to the home directory (measured). A knob is
        the operator's literal value, so its percent sign is doubled. So is
        the posture mirror's: the running peer's value is one systemd
        already expanded."""
        value = "/fixture/100%h/bin"
        for writer, knob, render in _writers():
            with self.subTest(writer):
                self.assertEqual(
                    _environment_set(render(knob, value)).get(knob), value)
        self.assertEqual(
            _systemd_reads(timerhealth.env_assignment("HELM_REMOTE_V", value)),
            {"HELM_REMOTE_V": value})

    def test_a_control_character_is_left_out_never_written_raw(self):  # noqa: VACUOUS_ASSERTION — the absence is the contract (no line can carry the value); the loop is over the non-empty literal writer table, and test_a_plain_value_renders_byte_identical_to_before is the positive control on the same renders
        """A raw newline ends the directive and starts another, so a value
        carrying one is left out: it never sets its knob, and it can never
        add a line to the unit. The posture mirror writes each loosening
        flag on every run, so it sets that one EMPTY, which dregg reads as
        refusal (task/3432); every other writer sets it not at all."""
        value = "/fixture/a\nExecStartPre=/bin/false"
        for writer, knob, render in _writers():
            with self.subTest(writer):
                text = render(knob, value)
                self.assertNotIn("\nExecStartPre=", text)
                self.assertEqual(_environment_set(text).get(knob),
                                 "" if writer == "chatnode posture" else None)
        self.assertIsNone(timerhealth.env_assignment("HELM_REMOTE_V", value))

    # -- control: what the base already wrote right does not churn ---------

    def test_a_plain_value_renders_byte_identical_to_before(self):  # noqa: VACUOUS_ASSERTION — the loop is over the non-empty writer table; every row asserts its exact unquoted line
        for writer, knob, render in _writers():
            with self.subTest(writer):
                self.assertIn("\nEnvironment=%s=/fixture/bin/claude-2.1_x\n"
                              % knob, render(knob, "/fixture/bin/claude-2.1_x"))

    def test_the_posture_mirror_writes_what_systemd_expanded_literally(self):
        """`systemctl show` reports a value after its specifiers were
        expanded (measured, 259: `DREGG_P=100%%` shows `DREGG_P=100%`, and
        %h shows the home directory), so a "%" the mirror is handed is a
        percent sign, and written bare our unit would expand it a second
        time (task/3423)."""
        writer, knob, mirror = _writers()[-1]
        self.assertEqual(writer, "chatnode posture")
        self.assertIn('\nEnvironment="%s=%%%%h/x"\n' % knob,
                      mirror(knob, "%h/x"))

    # -- the drift census reads both the old and the new form --------------

    def plant(self, module, block, knobs):
        """Plant `module`'s units as an install with the knob lines `knobs`
        left them."""
        spec = [r for r in timerhealth.UNIT_TEMPLATES if r.module == module][0]
        inputs = {"helm": "/fixture/bin/helm", "cwd": "/fixture/co",
                  "interval": "600"}
        inputs = dict((k, inputs[k]) for k in spec.wild)
        inputs[block] = knobs
        for path, text in timerhealth.template_units(spec, inputs):
            _plant(path, text.encode("utf-8"))

    def test_an_old_unquoted_plain_knob_unit_reads_CLEAN(self):
        self.plant("remote_relay", "knobs",
                   "Environment=HELM_REMOTE_TICK_INTERVAL_S=600\n"
                   "Environment=HELM_REMOTE_GH=/fixture/bin/gh\n")
        self.plant("upstream_watch", "env",
                   "Environment=HELM_UPSTREAM_WATCH_CLAUDE=/fixture/bin/claude\n")
        rows = [r for r in timerhealth.drift()
                if r["module"] in ("remote_relay", "upstream_watch")]
        self.assertEqual(len(rows), 4)
        self.assertEqual({r["verdict"] for r in rows}, {timerhealth.CLEAN},
                         rows)

    def test_an_old_unquoted_knob_the_renderer_now_quotes_reads_DRIFTED(self):
        self.plant("remote_relay", "knobs",
                   "Environment=HELM_REMOTE_TICK_INTERVAL_S=600\n"
                   "Environment=HELM_REMOTE_GH=/fixture/bin/g h\n"
                   "Environment=HELM_REMOTE_PCT=100%h\n")
        self.plant("upstream_watch", "env",
                   "Environment=HELM_UPSTREAM_WATCH_CLAUDE=/fixture/my bin/claude\n")
        rows = timerhealth.drift()
        relay = _rows_of(rows, "remote_relay")
        watch = _rows_of(rows, "upstream_watch")
        self.assertEqual(relay["helm-remote-relay.service"]["verdict"],
                         timerhealth.DRIFTED)
        self.assertEqual(watch["helm-upstream-watch.service"]["verdict"],
                         timerhealth.DRIFTED)
        detail = relay["helm-remote-relay.service"]["detail"]
        self.assertIn("misquoted Environment=HELM_REMOTE_GH=/fixture/bin/g h "
                      "(a re-install writes "
                      "Environment=\"HELM_REMOTE_GH=/fixture/bin/g h\")", detail)
        self.assertIn("misquoted Environment=HELM_REMOTE_PCT=100%h (a re-install "
                      "writes Environment=\"HELM_REMOTE_PCT=100%%h\")", detail)
        self.assertNotIn("TICK_INTERVAL", detail)
        self.assertEqual(relay["helm-remote-relay.timer"]["verdict"],
                         timerhealth.CLEAN)
        # THE RE-INSTALL LINE CARRIES THE VALUE THE OLD INSTALLER WAS GIVEN,
        # which is what a re-install from the same environment renders.
        self.assertEqual(
            relay["helm-remote-relay.service"]["command"],
            "HELM_REMOTE_TICK_INTERVAL_S=600 HELM_REMOTE_GH='/fixture/bin/g h'"
            " HELM_REMOTE_PCT=100%h helm remote ensure-timer")
        self.assertEqual(
            watch["helm-upstream-watch.service"]["command"],
            "HELM_UPSTREAM_WATCH_CLAUDE='/fixture/my bin/claude'"
            " helm upstream-watch --install-timer")

    def test_an_old_knob_is_judged_by_the_value_systemd_sets(self):
        """meta-claude's D on 907400b780f, which replaces F6's first reading.
        systemd strips a space, a tab and a carriage return from the end of a
        line (measured, 259: `K=/x/claude\\r` sets /x/claude and `K=/a ` sets
        /a), so an unquoted knob line sets what is left, and a re-install
        from that value writes the same directive. Reading its raw bytes
        called a working unit DRIFTED, and its re-install line carried a
        value no installer on main was given: remote_relay refused a "\\r",
        so following it dropped the knob, and it quoted a trailing space, so
        a working path became a broken one. The line reads as what systemd
        sets, and the re-install line carries that value unquoted."""
        self.plant("remote_relay", "knobs",
                   "Environment=HELM_REMOTE_CLAUDE=/x/claude\r\n"
                   "Environment=HELM_REMOTE_GH=/usr/bin/gh \n")
        relay = _rows_of(timerhealth.drift(), "remote_relay")
        service = relay["helm-remote-relay.service"]
        self.assertEqual(service["verdict"], timerhealth.CLEAN, service)
        self.assertEqual(service["command"],
                         "HELM_REMOTE_CLAUDE=/x/claude"
                         " HELM_REMOTE_GH=/usr/bin/gh helm remote ensure-timer")
        self.assertEqual(relay["helm-remote-relay.timer"]["verdict"],
                         timerhealth.CLEAN)

    def test_a_crlf_upstream_watch_unit_reads_CLEAN_as_on_main(self):
        """A hand-edited unit with CRLF line endings: systemd strips each
        "\\r" (measured), and upstream_watch._knob strips what it is given,
        so a re-install writes the same directives. The lane read the knob
        line's "\\r" as its value and called the unit DRIFTED ("a re-install
        leaves it out"); main read it CLEAN."""
        spec = [r for r in timerhealth.UNIT_TEMPLATES
                if r.module == "upstream_watch"][0]
        for path, text in timerhealth.template_units(spec, {
                "env": "Environment=HELM_UPSTREAM_WATCH_CLAUDE=/x/claude\n"}):
            _plant(path, text.replace("\n", "\r\n").encode("utf-8"))
        watch = _rows_of(timerhealth.drift(), "upstream_watch")
        self.assertEqual(len(watch), 2)
        self.assertEqual({r["verdict"] for r in watch.values()},
                         {timerhealth.CLEAN}, watch)
        self.assertEqual(watch["helm-upstream-watch.service"]["command"],
                         "HELM_UPSTREAM_WATCH_CLAUDE=/x/claude"
                         " helm upstream-watch --install-timer")

    def test_a_byte_systemd_keeps_at_a_knobs_end_is_still_its_value(self):
        """F6's false-clean stays cured: the ONE rule strips exactly what
        systemd strips, no more. A form feed ending an unquoted value is part
        of the value (measured, 259), and no Environment= line helm writes
        can carry it, so a re-install leaves that knob out: DRIFTED, and its
        re-install line keeps the byte. A trailing space beside it is
        systemd's to strip, and is no drift."""
        self.plant("remote_relay", "knobs",
                   "Environment=HELM_REMOTE_TICK_INTERVAL_S=600\n"
                   "Environment=HELM_REMOTE_GH=/fixture/bin/g \n"
                   "Environment=HELM_REMOTE_FF=/fixture/f\x0c\n")
        relay = _rows_of(timerhealth.drift(), "remote_relay")
        service = relay["helm-remote-relay.service"]
        self.assertEqual(service["verdict"], timerhealth.DRIFTED, service)
        self.assertIn("misquoted Environment=HELM_REMOTE_FF=/fixture/f? "
                      "(a re-install leaves it out)", service["detail"])
        self.assertNotIn("HELM_REMOTE_GH", service["detail"])
        self.assertEqual(
            service["command"],
            "HELM_REMOTE_TICK_INTERVAL_S=600 HELM_REMOTE_GH=/fixture/bin/g"
            " HELM_REMOTE_FF='/fixture/f\x0c' helm remote ensure-timer")
        self.assertEqual(relay["helm-remote-relay.timer"]["verdict"],
                         timerhealth.CLEAN)

    def test_an_old_knob_ending_in_a_byte_systemd_keeps_stays_whole(self):
        """F6's other half. systemd strips only " \\t\\n\\r" (measured, 259): an
        NBSP ending an unquoted value is part of the value, and env_assignment
        writes that value unquoted, so the unit IS what a re-install writes
        and reads CLEAN. Python's strip dropped the NBSP from the re-install
        line, which would then have set a different value."""
        self.plant("remote_relay", "knobs",
                   "Environment=HELM_REMOTE_GH=/fixture/bin/gh\u00a0\n")
        relay = _rows_of(timerhealth.drift(), "remote_relay")
        self.assertEqual({r["verdict"] for r in relay.values()},
                         {timerhealth.CLEAN}, relay)
        self.assertEqual(relay["helm-remote-relay.service"]["command"],
                         "HELM_REMOTE_GH='/fixture/bin/gh\u00a0'"
                         " helm remote ensure-timer")

    def test_what_systemd_strips_from_a_literal_line_is_not_drift(self):
        """The control: a literal directive line allows exactly what systemd
        strips from its end (a space, a tab, a carriage return), so a unit
        systemd reads the same as the template stays CLEAN."""
        spec = [r for r in timerhealth.UNIT_TEMPLATES
                if r.module == "remote_relay"][0]
        for path, text in timerhealth.template_units(spec, {
                "helm": "/fixture/bin/helm", "cwd": "/fixture/co",
                "interval": "600", "knobs": ""}):
            _plant(path, text.replace("\n", " \t\r\n").encode("utf-8"))
        relay = _rows_of(timerhealth.drift(), "remote_relay")
        self.assertEqual(len(relay), 2)
        self.assertEqual({r["verdict"] for r in relay.values()},
                         {timerhealth.CLEAN}, relay)

    def test_the_quoted_form_the_renderer_writes_reads_CLEAN(self):
        value = "it's \"q\"\\b 100%h"
        self.plant("remote_relay", "knobs",
                   'Environment="HELM_REMOTE_GH=/fixture/bin/g h"\n'
                   'Environment="HELM_REMOTE_Q=it\'s \\"q\\"\\\\b 100%%h"\n')
        self.plant("upstream_watch", "env",
                   'Environment="HELM_UPSTREAM_WATCH_CLAUDE=/fixture/my bin/claude"\n')
        rows = [r for r in timerhealth.drift()
                if r["module"] in ("remote_relay", "upstream_watch")]
        self.assertEqual(len(rows), 4)
        self.assertEqual({r["verdict"] for r in rows}, {timerhealth.CLEAN},
                         rows)
        self.assertEqual(
            _rows_of(rows, "remote_relay")["helm-remote-relay.service"]
            ["command"],
            "HELM_REMOTE_GH='/fixture/bin/g h' HELM_REMOTE_Q=%s"
            " helm remote ensure-timer" % shlex.quote(value))

    def test_a_unit_its_installer_wrote_with_special_knobs_reads_CLEAN(self):  # noqa: VACUOUS_ASSERTION — the loop is over two literal renders, each asserting its exact knob value; rows is asserted to hold exactly four CLEAN rows
        from helm import remote_relay, upstream_watch
        value = "/fixture/it's \"q\"\\b 100%h/bin"
        env = mock.patch.dict(os.environ, {
            "HELM_REMOTE_GH": value, "HELM_REMOTE_CLAUDE": "/fixture/bin/claude",
            "HELM_UPSTREAM_WATCH_CLAUDE": value})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(remote_relay.INTERVAL_ENV, None)
        for render, knob in ((remote_relay.timer_units, "HELM_REMOTE_GH"),
                             (upstream_watch.timer_units,
                              "HELM_UPSTREAM_WATCH_CLAUDE")):
            spath, service, tpath, timer = render()
            self.assertEqual(_environment_set(service).get(knob), value,
                             service)
            _plant(spath, service)
            _plant(tpath, timer)
        rows = [r for r in timerhealth.drift()
                if r["module"] in ("remote_relay", "upstream_watch")]
        self.assertEqual(len(rows), 4)
        self.assertEqual({r["verdict"] for r in rows}, {timerhealth.CLEAN},
                         rows)

    # -- an operator's padding is systemd's to strip, never ours to quote --

    def install_relay(self, knobs):
        """remote_relay's units as `helm remote ensure-timer` renders them
        from an installer environment whose HELM_REMOTE_* knobs are exactly
        `knobs`, planted where the census reads them -> (the service text,
        {name: value} of the HELM_REMOTE_* knobs it sets, the relay's drift
        rows)."""
        from helm import remote_relay
        env = mock.patch.dict(os.environ, knobs)
        env.start()
        self.addCleanup(env.stop)
        for name in [n for n in os.environ
                     if n.startswith("HELM_REMOTE_") and n not in knobs]:
            del os.environ[name]
        spath, service, tpath, timer = remote_relay.timer_units()
        _plant(spath, service.encode("utf-8"))
        _plant(tpath, timer.encode("utf-8"))
        sets = dict((k, v) for k, v in _environment_set(service).items()
                    if k.startswith("HELM_REMOTE_"))
        return service, sets, _rows_of(timerhealth.drift(), "remote_relay")

    def test_a_knob_padded_with_what_systemd_strips_is_written_stripped(self):
        """THE PADDING WAS NEVER THE OPERATOR'S PATH (task/3423, the round-3
        builder's report). Written raw on main, `HELM_REMOTE_GH=/usr/bin/gh `
        set /usr/bin/gh, because systemd strips a space, a tab, a carriage
        return and a newline from the end of a line (measured, 259); the
        renderer quoted it, and systemd set "/usr/bin/gh ", a path that does
        not resolve. A "\\r" left the whole knob out, and a leading space
        split the raw word on main and set the knob empty, so the relay fell
        back to PATH, where quoted it sets a path that does not resolve.
        remote_relay strips exactly systemd's whitespace from both ends of
        each knob, as upstream_watch._knob strips its own, so each line sets
        what the operator meant, unquoted unless the renderer must quote it,
        and the census reads the install CLEAN with those values."""
        service, sets, relay = self.install_relay({
            "HELM_REMOTE_GH": "/usr/bin/gh ",
            "HELM_REMOTE_CLAUDE": "/x/claude\r",
            "HELM_REMOTE_FIXTURE_LEAD": " \t/fixture/lead",
            "HELM_REMOTE_FIXTURE_PAD": "/fixture/g h/x \t\r\n"})
        self.assertEqual(sets, {
            "HELM_REMOTE_CLAUDE": "/x/claude",
            "HELM_REMOTE_FIXTURE_LEAD": "/fixture/lead",
            "HELM_REMOTE_FIXTURE_PAD": "/fixture/g h/x",
            "HELM_REMOTE_GH": "/usr/bin/gh"}, service)
        self.assertIn("\nEnvironment=HELM_REMOTE_GH=/usr/bin/gh\n", service)
        self.assertIn("\nEnvironment=HELM_REMOTE_CLAUDE=/x/claude\n", service)
        self.assertIn("\nEnvironment=HELM_REMOTE_FIXTURE_LEAD=/fixture/lead\n",
                      service)
        self.assertIn('\nEnvironment="HELM_REMOTE_FIXTURE_PAD=/fixture/g h/x"\n',
                      service)
        self.assertEqual(len(relay), 2)
        self.assertEqual({r["verdict"] for r in relay.values()},
                         {timerhealth.CLEAN}, relay)
        self.assertEqual(
            relay["helm-remote-relay.service"]["command"],
            "HELM_REMOTE_CLAUDE=/x/claude HELM_REMOTE_FIXTURE_LEAD=/fixture/lead"
            " HELM_REMOTE_FIXTURE_PAD='/fixture/g h/x'"
            " HELM_REMOTE_GH=/usr/bin/gh helm remote ensure-timer")

    def test_a_program_knob_that_is_all_padding_falls_back_to_PATH(self):
        """On main `HELM_REMOTE_GH=  ` was written raw, systemd set the knob
        empty, and the relay found gh on PATH at run time; quoted, the
        padding was the program. Stripped, it is no value, so the installer
        writes the gh it finds on PATH, as it does when the knob is unset."""
        with mock.patch("shutil.which", side_effect={
                "gh": "/fixture/bin/gh", "claude": "/fixture/bin/claude"}.get):
            service, sets, relay = self.install_relay({
                "HELM_REMOTE_GH": "   ", "HELM_REMOTE_CLAUDE": "/x/claude"})
        self.assertEqual(sets, {"HELM_REMOTE_CLAUDE": "/x/claude",
                                "HELM_REMOTE_GH": "/fixture/bin/gh"}, service)
        self.assertEqual({r["verdict"] for r in relay.values()},
                         {timerhealth.CLEAN}, relay)

    def test_a_knob_systemd_keeps_whole_is_written_whole(self):
        """The control: the strip takes exactly systemd's whitespace from the
        ends, no more (measured, 259). An inner space stays and is quoted; a
        trailing NBSP is part of the value, written unquoted as the renderer
        writes it; a trailing form feed is part of the value too, which no
        line helm writes can carry, so that knob is left out rather than
        written as a shorter path. Python's str.strip takes both."""
        service, sets, relay = self.install_relay({
            "HELM_REMOTE_GH": "/fixture/g h/gh",
            "HELM_REMOTE_CLAUDE": "/fixture/bin/claude ",
            "HELM_REMOTE_FIXTURE_FF": "/fixture/f\x0c"})
        self.assertEqual(sets, {
            "HELM_REMOTE_CLAUDE": "/fixture/bin/claude ",
            "HELM_REMOTE_GH": "/fixture/g h/gh"}, service)
        self.assertIn('\nEnvironment="HELM_REMOTE_GH=/fixture/g h/gh"\n',
                      service)
        self.assertIn("\nEnvironment=HELM_REMOTE_CLAUDE=/fixture/bin/claude"
                      " \n", service)
        self.assertEqual(len(relay), 2)
        self.assertEqual({r["verdict"] for r in relay.values()},
                         {timerhealth.CLEAN}, relay)
        self.assertEqual(
            relay["helm-remote-relay.service"]["command"],
            "HELM_REMOTE_CLAUDE='/fixture/bin/claude '"
            " HELM_REMOTE_GH='/fixture/g h/gh' helm remote ensure-timer")


class SystemctlDecodeTest(unittest.TestCase):
    """systemctl PRINTS RAW UTF-8 WHATEVER THE LOCALE (meta-claude's C on
    907400b780f; measured under LC_ALL=C: an e-acute printed as the bytes
    0xc3 0xa9). _systemctl read its answer in the locale's encoding, so a
    Latin-1 locale handed the posture mirror those two bytes as two
    characters, U+00C3 U+00A9, a value the peer does not have, and an ASCII
    one read a systemctl that answered as one that could not be run. It
    reads UTF-8, strictly: bytes that are not UTF-8 are not measured."""

    def setUp(self):
        self.fake = fake_user_systemd(self)

    def answer(self, data):
        """The fake systemctl prints `data` (bytes) and exits 0."""
        with open(self.fake.systemctl, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\nprintf '%s'\n" % "".join(
                "\\%03o" % b for b in data))

    @unittest.skipIf(sys.flags.utf8_mode,
                     "UTF-8 mode reads every child as UTF-8 whatever the locale")
    def test_its_answer_is_read_as_utf8_in_any_locale(self):  # noqa: VACUOUS_ASSERTION — the loop is over two literal locales; every row first asserts the simulated locale mis-reads the same fake's answer (its control), then _systemctl's exact result
        import locale
        said = "Environment=DREGG_NOTE=caf\u00e9\n"
        self.answer(said.encode("utf-8"))
        for encoding, misread in (("iso-8859-1", "caf\u00c3\u00a9"),
                                  ("ascii", None)):
            with self.subTest(encoding), mock.patch.object(
                    locale, "getencoding", return_value=encoding):
                try:
                    control = subprocess.run(
                        [self.fake.systemctl], capture_output=True,
                        text=True).stdout
                except UnicodeDecodeError:
                    control = None
                self.assertEqual(control and control.split("=")[-1][:-1],
                                 misread, "the control: this locale mis-reads")
                self.assertEqual(timerhealth._systemctl(["show", "x"]),
                                 (0, said))

    @unittest.skipIf(sys.flags.utf8_mode,
                     "UTF-8 mode reads every child as UTF-8 whatever the locale")
    def test_an_installers_failure_relays_systemctls_words_in_any_locale(self):  # noqa: VACUOUS_ASSERTION — the loop is over two literal locales; every row asserts install_user_timer's exact report
        """install_user_timer relays systemctl's own words when a step
        fails, and systemctl prints UTF-8: read in the locale's encoding, a
        Latin-1 locale relayed a path's e-acute as two characters and an
        ASCII one raised out of the installer. It reads UTF-8, a byte that
        is not replaced: the words are for reading (task/3423)."""
        import locale
        words = "Unit file /srv/caf\u00e9/x.timer does not exist."
        fake_user_systemd(self, rc=1, stderr=words + "\n", home=False)
        for encoding in ("iso-8859-1", "ascii"):
            with self.subTest(encoding), mock.patch.object(
                    locale, "getencoding", return_value=encoding):
                self.assertEqual(
                    timerhealth.install_user_timer([], ["x.timer"]),
                    ("systemctl --user daemon-reload failed: " + words,
                     False))

    def test_an_answer_that_is_not_utf8_is_not_measured(self):
        import locale
        self.answer(b"Environment=DREGG_NOTE=\xff\n")
        with mock.patch.object(locale, "getencoding",
                               return_value="iso-8859-1"):
            self.assertEqual(timerhealth._systemctl(["show", "x"]), (None, ""))
        self.answer(b"Environment=DREGG_NOTE=1\n")
        self.assertEqual(timerhealth._systemctl(["show", "x"]),
                         (0, "Environment=DREGG_NOTE=1\n"),
                         "the control: the same fake, answering UTF-8")


if __name__ == "__main__":
    unittest.main()
