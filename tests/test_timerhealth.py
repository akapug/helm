#!/usr/bin/env python3
"""ENABLED AND RUNNING ARE TWO FACTS, and the census must not fold them.

Every arm scripts systemctl through ONE seam, so a fixture cannot describe a
box where `list-unit-files` and `show` disagree about which units exist.
"""
import unittest
from types import SimpleNamespace
from unittest import mock

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


if __name__ == "__main__":
    unittest.main()
