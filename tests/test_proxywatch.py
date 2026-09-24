#!/usr/bin/env python3
"""proxywatch — the two rungs nothing was watching, and the change-latch.

THE OWNER'S ASK, on lifting codex's routing bench: "since codex is back in
service, please set a timer to check on it appropriately to make sure that the
CLI proxy fixes for it actually work."

WHAT WAS ALREADY WATCHED: drops, by helm-silent-drop, every 90 seconds. That is
one of three ways the fixes stop working and it was the only one anything
looked at.

  CONFIG  nothing ran the drift census on a schedule. ds4pro went a WEEK with
          nonstream-keepalive-interval absent for exactly that reason — the
          generator was fixed and nobody re-read the files.
  HANGS   stated plainly when the bench was lifted and still true: a turn that
          never completes writes no transcript row, so the drop watchdog is
          structurally blind to it.

MOST OF THIS SUITE IS ABOUT THE WATCH BEING ABLE TO FAIL, and about it staying
quiet when nothing moved. A watch that speaks every pass gets filtered, and a
filtered alarm is an absent one — so silence has to be meaningful, which means
the latch is as load-bearing as the detection.
"""
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from helm import dispatches, proxywatch, seat, seats  # noqa: E402
from helm import pk  # noqa: E402
from helm import orcaadopt as _orcaadopt  # noqa: E402
from helm import seats_common as _seats_common  # noqa: E402


def row(seat="codex", config_ok=True, drift=(), age=60, live=True,
        alerted=None, hang=False, log=None, log_detail=None,
        turn=None, turn_evidence=None, probe=None, probe_detail=None,
        family="codex", upstream=None, upstream_detail=None,
        upstream_ms=None, upstream_since=None, liveness=None):
    return {"seat": seat, "family": family, "config_ok": config_ok,
            "drift": list(drift), "transcript_age_s": age,
            "pane_live": live, "alerted_at": alerted,
            "hang_candidate": hang, "log": log, "log_detail": log_detail,
            "probe": probe, "probe_detail": probe_detail,
            "turn_state": turn, "turn_evidence": turn_evidence,
            "liveness": liveness,
            "upstream": upstream, "upstream_detail": upstream_detail,
            "upstream_ms": upstream_ms, "upstream_since": upstream_since}


def rep(*rows, upstream=None, proxy_runtime=None):
    return {"ts": 1000, "seats": list(rows), "upstream": upstream or {},
            "proxy_runtime": proxy_runtime or {}}


# THE PROOF CONSTRUCTOR LIVES IN A NEUTRAL HELPER, not here. More than one
# suite needs a proof of the shape production writes, and a suite that
# imports another SUITE inherits its module state, its temp home and its
# collection order along with the fixture. tests/_runtime_proof.py is a pure
# function with no import-time side effects, so both sides share the shape
# and neither depends on the other. Re-exported under the local name every
# call site here already uses.
from tests._runtime_proof import runtime_proof  # noqa: E402,F401


# grok's real starvation line, VERBATIM from its proxy.log — the parser is
# tested against what the incident actually wrote, not an imagined format.
GIN_402 = ('[2026-07-29 11:20:00] [a2f8ba2a] [warn ] [gin_logger.go:95] 402 '
           '|        1.255s |       127.0.0.1 | POST    '
           '"/v1/messages?beta=true"')


def gin(code, path="/v1/messages?beta=true", method="POST", level="warn ",
        ts="2026-07-29 11:20:00", body=None, truncated=False,
        origin=None):
    line = ('[%s] [a2f8ba2a] [%s] [gin_logger.go:95] %d |        1.255s '
            '|       127.0.0.1 | %s    "%s"' % (ts, level, code, method, path))
    if origin is not None:
        line += " | refusal_origin_v1=" + str(origin)
    if body is not None:
        line += " | response_body=" + json.dumps(body)
        if truncated:
            line += " [truncated]"
    return line


class SeatEnumerationTest(unittest.TestCase):
    def test_EVERY_minted_seat_is_watched_not_just_one_family(self):
        """THE GROK INCIDENT, pinned. The watch used to filter this list down
        to codex and its instances — and grok sat INERT for TWO DAYS
        (2026-07-27→29), every request 402ing into its own proxy.log, while
        the timer probed every ~15 minutes one family short. The instances
        stay pinned too: codex-2/codex-3 were the original narrower bug (the
        first live run showed one row where three were expected)."""
        with mock.patch("helm.seat._minted_seats",
                        return_value=[("codex", "codex"), ("codex", "codex-2"),
                                      ("codex", "codex-3"), ("grok", "grok"),
                                      ("kimi", "kimi")]):
            self.assertEqual(proxywatch._watched_seats(),
                             ["codex", "codex-2", "codex-3", "grok", "kimi"])

    def test_it_never_raises_when_the_register_cannot_be_read(self):
        """A watch that dies on a bad read is a watch that reports nothing,
        which reads exactly like healthy."""
        with mock.patch("helm.seat._minted_seats", side_effect=OSError("boom")):
            self.assertEqual(proxywatch._watched_seats(), ["codex"])


class AdoptedSeatsAreInTheCensusTest(unittest.TestCase):
    """A seat whose identity was bound AFTER exec is invisible to an env walk.

    `/proc/PID/environ` is frozen at exec and HELM_CHAT_NAME is exported only
    by the launcher, so a seat that was adopted or hand-launched never
    acquires one however long it runs. MEASURED on this host: 44 live claude
    processes, 10 carrying a seat name, 28 carrying none, with `unreadable`
    EMPTY — so the census returned 10 seats and blind=None, a confident and
    complete-looking answer that was missing five live seats, among them the ones that went dark.

    The cure keys those seats through the SESSION join `orcaadopt.resolve`
    already trusts for addressing, with its refusals intact. These arms drive
    the real `_live_seats` against synthetic process rows rather than the live
    host, because a census arm that reads the real machine passes or fails by
    what else is running.
    """

    def census(self, procs, roster, joined=None, unreadable=()):
        """(names, FOLDED blind) from the REAL _live_seats over supplied rows.

        `_live_seats` answers host-wide doubt and per-seat doubt separately
        (task/2739); this class is about the single answer a caller that ACTS
        sees, so it folds through the one rule. The split itself is asserted by
        `OneUnkeyableProcessCostsOneSeatTest`."""
        def claude_processes():
            return list(procs), list(unreadable)

        def roster_fn():
            return dict(roster)

        def roster_identity(seat):
            return ("sid-" + seat, [], False)

        def session_joined(seat, allprocs, sid, named, rows=None):
            return (joined or {}).get(seat, ([], None))

        with mock.patch.object(_orcaadopt, "claude_processes",
                               claude_processes), \
             mock.patch.object(_orcaadopt, "roster_identity", roster_identity), \
             mock.patch.object(_orcaadopt, "_session_joined", session_joined), \
             mock.patch.object(_seats_common, "roster", roster_fn):
            names, blind, per_seat = proxywatch._live_seats()
        return names, proxywatch.fold_blind(blind, per_seat)

    def test_an_env_named_seat_is_still_found_the_fast_way(self):
        """MUST-HIT on the unchanged path: the join is ADDITIVE, and a spawned
        seat's answer must not move."""
        names, blind = self.census(
            [{"pid": 1, "seat": "seat-a"}], {"seat-a": {}})
        self.assertEqual(names, {"seat-a"})
        self.assertIsNone(blind)

    def test_a_nameless_process_is_keyed_by_its_ROSTER_SESSION(self):
        """The five-seats-missing case, and the whole point of the lane."""
        procs = [{"pid": 1, "seat": "seat-a"}, {"pid": 2, "seat": None}]
        names, blind = self.census(
            procs, {"seat-a": {}, "seat-b": {}},
            joined={"seat-b": ([{"pid": 2, "seat": None}], None)})
        self.assertEqual(names, {"seat-a", "seat-b"})
        self.assertIsNone(blind)

    def test_a_seat_the_join_REFUSES_reports_blind_not_absence(self):
        """`_read_panes` states the rule in its own docstring: blind must never
        collapse into nobody-holds-this-seat, because that is a positive
        claim. A refused join means helm KNOWS a live process may hold the
        seat and declines to say which — the opposite of `off`."""
        procs = [{"pid": 2, "seat": None}]
        names, blind = self.census(
            procs, {"seat-b": {}},
            joined={"seat-b": ([], "two live processes carry --resume")})
        self.assertEqual(names, set())
        self.assertIsNotNone(blind, "a refused join read as a complete census")
        self.assertIn("seat-b", blind)
        self.assertIn("could not key", blind)
        # AND THE REFUSAL IS KEYED UNDERNEATH THE FOLD: the folded string above
        # is what a single-answer caller sees, but the census must still be
        # able to say WHOSE refusal it was, or a row-per-seat renderer prints
        # this sentence on every other seat (task/2739).
        with mock.patch.object(_orcaadopt, "claude_processes",
                               lambda: ([{"pid": 2, "seat": None}], [])), \
             mock.patch.object(_orcaadopt, "roster_identity",
                               lambda s: ("sid-" + s, [], False)), \
             mock.patch.object(_orcaadopt, "_session_joined",
                               lambda *a, **k: ([], "two live processes")), \
             mock.patch.object(_seats_common, "roster",
                               lambda: {"seat-b": {}}):
            _n, host, per_seat = proxywatch._live_seats()
        self.assertIsNone(host)
        self.assertEqual(sorted(per_seat), ["seat-b"])

    def test_a_seat_with_no_process_at_all_is_simply_absent(self):  # noqa: VACUOUS_ASSERTION — this is the deliberate must-MISS of the class: its positive twin is test_a_seat_the_join_REFUSES_reports_blind_not_absence, which drives the same census through the same helper and asserts blind is set, and folding them into one arm would delete the distinction between a refused join and an absent process
        """MUST-MISS: absence with nothing refused is a real `off`, and
        widening blind to cover it would make every quiet fleet read blind."""
        names, blind = self.census([], {"seat-b": {}},
                                   joined={"seat-b": ([], None)})
        self.assertEqual(names, set())
        self.assertIsNone(blind)

    def test_an_UNREADABLE_process_still_outranks_a_join_refusal(self):  # noqa: VACUOUS_ASSERTION — assertIsNotNone(blind) on the line above is the unconditional positive on the same observable; the NotIn that follows is about WHICH reason won, which is the whole subject
        """A census that could not read the host is a bigger fact than one
        seat it could not key, and the pre-existing reason must survive."""
        names, blind = self.census(
            [{"pid": 2, "seat": None}], {"seat-b": {}},
            joined={"seat-b": ([], "ambiguous")},
            unreadable=[{"pid": 99}])
        self.assertIsNotNone(blind)
        self.assertNotIn("could not key", blind,
                         "the join refusal masked the unreadable-host reason")


class OneUnkeyableProcessCostsOneSeatTest(unittest.TestCase):
    """The blast-radius bound (task/2739).

    `_session_keyed_seats` raises each refusal while keying ONE roster seat, so
    every refusal already knows its subject — but they left as bare strings and
    `_live_seats` had nowhere to put them except the one host-wide `blind`.
    MEASURED on this host before the split: 986 pids enumerated, 24 claude
    candidates, 0 unreadable, ONE refused seat — and 29 of 40 roster seats lost
    a measured `pane_live=False`, while nine `helm seat list` rows printed
    another seat's pid and refusal as their own explanation.

    Each arm carries the OTHER seat as its control: an arm that only asserted
    the refused seat is blind would pass against the old host-wide behaviour.
    """

    def census(self, refusals, procs=(), unreadable=()):
        def claude_processes():
            return list(procs), list(unreadable)

        def keyed(_procs, _named):
            return set(), list(refusals)

        with mock.patch.object(_orcaadopt, "claude_processes",
                               claude_processes), \
             mock.patch.object(proxywatch, "_session_keyed_seats", keyed):
            return proxywatch._live_seats()

    def test_a_refused_seats_doubt_reaches_THAT_SEAT_AND_NO_OTHER(self):
        """THE REPAIR. `seat-b` was unkeyable; `seat-a` was not measured at all
        by that refusal and must not inherit it."""
        names, host, per_seat = self.census([("seat-b", "seat-b (ambiguous)")])
        self.assertIsNone(host, "one seat's refusal still blinded the host")
        self.assertEqual(sorted(per_seat), ["seat-b"])
        self.assertIn("ambiguous", per_seat["seat-b"])
        self.assertEqual(names, set())

    def test_an_UNIDENTIFIED_pid_still_blinds_EVERY_seat(self):
        """THE HALF THAT MUST NOT MOVE, and the control that proves the arm
        above is a split rather than a deletion: a pid helm could not read
        could be ANY seat's process, so `cannot_look`'s doubt is genuinely
        host-wide and stays that way."""
        names, host, per_seat = self.census([], unreadable=[4242])
        self.assertTrue(host, "the unreadable bucket stopped blinding")
        self.assertIn("4242", host)
        self.assertEqual(per_seat, {})
        self.assertEqual(names, set())

    def test_an_UNREADABLE_ROSTER_is_host_wide_because_it_keys_NOTHING(self):
        """A refusal with NO subject is the roster read itself. With no roster
        no seat could be keyed, so this one belongs beside `cannot_look`'s
        doubt — the per-seat arm above is the control on the same channel."""
        names, host, per_seat = self.census(
            [(None, "the roster could not be read (OSError)")])
        self.assertIn("roster", host)
        self.assertEqual(per_seat, {})
        self.assertEqual(names, set())

    def test_a_CALLER_THAT_ACTS_folds_the_split_back_into_one_answer(self):
        """A per-seat split must not quietly un-blind a rung that acts on a
        single yes/no. `autocompact._live_seat_names` mutes its whole pass on
        ANY blindness — deliberately — so it folds through `fold_blind`, the
        one place that rule is written.

        The measured-empty control is the pole this arm is worthless without:
        without it, "returns None" would also be satisfied by a fold that
        called every quiet fleet blind."""
        names, host, per_seat = set(), None, {"seat-b": "seat-b (ambiguous)"}
        folded = proxywatch.fold_blind(host, per_seat)
        self.assertIn("could not key", folded)
        self.assertIn("seat-b", folded)
        # CONTROL: nothing refused and nothing unidentified is a MEASURED empty
        # fleet, which must stay an answer rather than become blindness.
        self.assertIsNone(proxywatch.fold_blind(None, {}))
        # and a host-wide reason is never displaced by the per-seat ones
        self.assertEqual(proxywatch.fold_blind("pid 303 unreadable", per_seat),
                         "pid 303 unreadable")
        from helm import autocompact
        with mock.patch.object(proxywatch, "_live_seats",
                               return_value=(names, host, per_seat)):
            self.assertIsNone(autocompact._live_seat_names(),
                              "a rung that acts on one yes/no read a refused "
                              "seat as a measured empty fleet")
        with mock.patch.object(proxywatch, "_live_seats",
                               return_value=({"seat-a"}, None, {})):
            self.assertEqual(autocompact._live_seat_names(), {"seat-a"})

    def test_the_refusal_NAMES_the_seat_it_was_raised_for(self):
        """THE PRODUCER, not the rig. Every arm above mocks
        `_session_keyed_seats`, so a producer that stopped attributing its
        refusals would leave them all green while the split silently became
        host-wide again. `seat-quiet` is the control: the same pass that
        refuses `seat-b` says nothing about it."""
        procs = [{"pid": 1, "seat": "stranger", "resume_sid": "sid-seat-b"}]
        roster = {"seat-b": {"session": "sid-seat-b"},
                  "seat-quiet": {"session": "sid-seat-quiet"}}
        with mock.patch.object(_orcaadopt, "roster_identity",
                               lambda s: (roster[s]["session"], [], False)), \
             mock.patch.object(_seats_common, "roster", lambda: dict(roster)):
            keyed, refusals = proxywatch._session_keyed_seats(procs, procs)
        self.assertEqual(keyed, set())
        self.assertEqual([s for s, _w in refusals], ["seat-b"],
                         "a refusal lost the seat it was raised for")
        self.assertNotIn("seat-quiet", refusals[0][1])

    def test_the_census_hands_ITS_OWN_roster_down_to_the_alias_rung(self):
        """ONE READING PER PASS. The roster is a live tmpfs file other seats
        rewrite; the alias rung must resolve against the map this sweep already
        read, not re-open it per seat. Proven by SUBSTITUTION: `roster_checked`
        is made to FAIL, so a rung that re-read would refuse — and the seat is
        keyed anyway, which only the handed-down map can explain."""
        procs = [{"pid": 1, "seat": "old-name", "resume_sid": "sid-seat-b"}]
        roster = {"seat-b": {
            "session": "sid-seat-b",
            _seats_common.RENAME_ALIAS_FIELD: {
                "old": "old-name", "at": "x", "prior": [],
                "until": pk.epoch_ts(time.time() + 3600)}}}
        with mock.patch.object(_orcaadopt, "roster_identity",
                               lambda s: ("sid-seat-b", [], False)), \
             mock.patch.object(_seats_common, "roster", lambda: dict(roster)), \
             mock.patch("helm.seats.roster_checked", return_value=({}, True)):
            keyed, refusals = proxywatch._session_keyed_seats(procs, procs)
        self.assertEqual(keyed, {"seat-b"},
                         "the alias rung re-read the roster instead of using "
                         "the one this pass already holds")
        self.assertEqual(refusals, [])

    def test_health_prints_one_seats_refusal_on_ONE_SEATS_ROW(self):
        """THE OWNER-VISIBLE HALF. `helm seat list` renders `census_blind` as
        the sentence explaining a row's UNKNOWN; before the split it printed
        gtp-codex's pid on codex, codex-2..7, codex-10 and grok. The
        `seat-live` row is the must-hit that proves this rig reports at all."""
        def keyed():
            return ({"seat-live"}, None, {"seat-b": "seat-b (ambiguous)"})

        with mock.patch.object(proxywatch, "_live_seats", keyed):
            rep = proxywatch.health(seats=["seat-live", "seat-a", "seat-b"],
                                    include_upstream=False, include_probe=False)
        got = {r["seat"]: r for r in rep["seats"]}
        self.assertTrue(got["seat-live"]["pane_live"])
        self.assertIsNone(got["seat-live"]["census_blind"])
        self.assertIs(got["seat-a"]["pane_live"], False,
                      "an unrefused seat lost its measured absence")
        self.assertIsNone(got["seat-a"]["census_blind"])
        self.assertIsNone(got["seat-b"]["pane_live"])
        self.assertIn("ambiguous", got["seat-b"]["census_blind"])

    def test_a_HOST_WIDE_reason_reaches_every_row(self):
        """The control for the arm above on the same observable: when the doubt
        IS host-wide, every row carries it — so `seat-a` reading False above is
        the split working and not a row that can never be blind."""
        def keyed():
            return (set(), "1 live claude process could not be identified",
                    {})

        with mock.patch.object(proxywatch, "_live_seats", keyed):
            rep = proxywatch.health(seats=["seat-a", "seat-b"],
                                    include_upstream=False, include_probe=False)
        got = {r["seat"]: r for r in rep["seats"]}
        # UNCONDITIONAL POSITIVE CONTROL ON THE SAME OBSERVABLE, so the loop's
        # assertions cannot pass by iterating an empty report.
        self.assertEqual(sorted(got), ["seat-a", "seat-b"])
        self.assertIsNone(got["seat-a"]["pane_live"])
        self.assertIn("could not be identified", got["seat-a"]["census_blind"])
        self.assertIsNone(got["seat-b"]["pane_live"])
        self.assertIn("could not be identified", got["seat-b"]["census_blind"])


class FindingsTest(unittest.TestCase):
    def test_a_healthy_fleet_says_NOTHING(self):
        self.assertEqual(proxywatch.findings(rep(row())), [])

    def test_CONFIG_DRIFT_is_reported_with_what_it_costs(self):
        """The message must name the CONSEQUENCE, not just the key — the whole
        reason ds4pro's week-long absence went unnoticed is that a missing line
        reads like a detail until someone says what it does."""
        f = proxywatch.findings(rep(row(config_ok=False,
                                        drift=["nonstream-keepalive-interval=None want 15"])))
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0][0], "CONFIG")
        self.assertIn("empty HTTP 200", f[0][1])

    def test_a_HANG_CANDIDATE_needs_a_LIVE_pane(self):
        """A seat nobody launched is not hung, it is off. Reporting an idle
        seat as a hang is how a watch earns the filter it then dies behind."""
        self.assertEqual(proxywatch.findings(rep(row(live=False, age=99999))), [])

    def test_a_hang_is_flagged_as_a_QUESTION_not_a_verdict(self):
        """An agent legitimately thinks for a long time. The rung exists
        because the drop watchdog is structurally blind here, not because a
        quiet pane is proof of anything."""
        f = proxywatch.findings(rep(row(hang=True, age=3600)))
        self.assertEqual(f[0][0], "HANG?")
        self.assertIn("Not a verdict", f[0][1])

    def test_a_seat_that_could_not_be_read_is_SKIPPED_not_called_healthy(self):
        r = rep({"seat": "codex-9", "error": "unknown seat"})
        self.assertEqual(proxywatch.findings(r), [])


class BlockedOnHumanVerdictTest(unittest.TestCase):
    """Fleet #124 (owner ruling: complying destroys work). A seat frozen at a
    plan-approval prompt has a DEAD turn loop AND is BLOCKED_ON_HUMAN — the
    HUNG branch used to prescribe `helm seat resume`, which DISCARDS the
    pending plan. codex-2 sat exactly there for 135 minutes 2026-08-03. The
    verdict must consult the pane-tail classifier (seat.seat_liveness, reused
    — never a second pane-tail reader) before prescribing a restart.

    These tests assert the EFFECT, not the absence of a complaint: a blocked
    fixture must yield a verdict that CARRIES the plan path and NO resume
    instruction; an unreadable classifier must degrade the prescription to
    naming the uncertainty rather than asserting resume over an unread
    state."""

    @staticmethod
    def _findings(liveness):
        # findings() is a PURE reduction over the report: the liveness was
        # sampled once in health() and rides the row. No classifier mock —
        # that would re-test the r1 impurity codex caught.
        r = rep(row(seat="codex-2", turn="hung", liveness=liveness,
                    turn_evidence="semantic entry stale 57m"))
        return proxywatch.findings(r)

    def test_a_recorded_WALL_never_guesses_remediation_without_its_seam(self):  # noqa: VACUOUS_ASSERTION — unconditional WALLED row and fallback controls prove the renderer before the remediation matrix and mutant must-miss
        honest = ("The measured wall explains the refusal; remediation cannot "
                  "be derived from this record. helm proxywatch safely "
                  "remeasures current state.")

        def assert_honest(text):
            self.assertIn(honest, text)
            lower = text.lower()
            for unsupported in ("provider", "local", "restart", "resume",
                                "reseed", "action"):
                self.assertNotIn(unsupported, lower)

        control = self._findings({"state": "WALLED",
                                  "blocked_on":
                                      "upstream RATE-LIMITED since T",
                                  "evidence": "pane-tail+proxywatch"})
        self.assertEqual(len(control), 1)
        self.assertEqual(control[0][0], "WALLED")
        self.assertIn("RATE-LIMITED", control[0][1])
        assert_honest(control[0][1])

        fallback = self._findings({"state": "WALLED",
                                   "evidence": "pane-tail+proxywatch"})
        self.assertEqual(len(fallback), 1)
        self.assertEqual(fallback[0][0], "WALLED")
        self.assertIn("a measured availability wall is present", fallback[0][1])
        assert_honest(fallback[0][1])

        rendered = []
        for name, remediation in (
                ("missing", None),
                ("unknown", {"restart": "UNKNOWN"}),
                ("helpful", {"restart": "HELPFUL",
                             "action": "restart a proxy"})):
            with self.subTest(remediation=name):
                liv = {"state": "WALLED",
                       "blocked_on": "upstream RATE-LIMITED since T",
                       "evidence": "pane-tail+proxywatch"}
                if remediation is not None:
                    liv["remediation"] = remediation
                f = self._findings(liv)
                self.assertEqual(len(f), 1)  # assert the finding row, not a legend
                level, text = f[0]
                self.assertEqual(level, "WALLED")
                self.assertIn("RATE-LIMITED", text)  # premise: WALLED rendered
                assert_honest(text)
                rendered.append(text)
        self.assertEqual(len(set(rendered)), 1)

        mutant = rendered[0].replace(
            honest,
            "The provider wall explains the missing turn; a restart does not "
            "repair upstream availability, so no resume/reseed is prescribed.")
        self.assertNotEqual(mutant, rendered[0])
        with self.assertRaises(AssertionError):
            assert_honest(mutant)

    def test_blocked_on_human_carries_the_plan_path_and_no_resume(self):
        liv = {"state": "BLOCKED_ON_HUMAN",
               "blocked_on": "~/.helm/_global/seats/codex-2/claude/plans/x.md",
               "evidence": "pane-tail"}
        f = self._findings(liv)
        self.assertEqual(f[0][0], "BLOCKED-HUMAN")
        text = f[0][1]
        # the WHERE: the reader can approve without opening the pane by hand
        self.assertIn("plans/x.md", text)
        # the EFFECT that matters: no restart INSTRUCTION survives. (The
        # words "resume/reseed" DO appear — inside the warning that they
        # would destroy the work, which is exactly what the reader must
        # hear; the assertion is that they are never IMPERATIVE.)
        self.assertNotIn("`helm seat resume", text)      # the backticked command
        self.assertNotIn("Action:", text)                # the old prescription lead
        # and it says resume/reseed would destroy the work, by name
        self.assertIn("DISCARD THE PENDING PLAN", text)

    def test_blocked_on_human_without_a_path_still_names_the_prompt(self):
        liv = {"state": "BLOCKED_ON_HUMAN",
               "blocked_on": "do you want to proceed",
               "evidence": "pane-tail"}
        f = self._findings(liv)
        self.assertEqual(f[0][0], "BLOCKED-HUMAN")
        self.assertNotIn("helm seat resume", f[0][1])

    def test_an_unreadable_classifier_degrades_the_prescription(self):
        """HUNG may stand (the fuse composed), but the verdict must name the
        blindness and the read-first gate with NO executable resume command —
        a classifier that could not see the seat cannot clear it of being
        human-blocked, so the restart prescription does not go out (codex r1
        MED: UNKNOWN-with-resume-attached is HUNG-with-resume)."""
        liv = {"state": "UNKNOWN", "blocked_on": None,
               "evidence": "read-failed"}
        f = self._findings(liv)
        self.assertEqual(f[0][0], "HUNG")
        text = f[0][1]
        self.assertIn("could not confirm", text)
        self.assertIn("READ THE PANE FIRST", text)
        self.assertNotIn("helm seat resume", text)   # no executable prescription

    def test_findings_is_a_PURE_reduction_over_one_immutable_report(self):
        """codex r1 HIGH: findings() calling seat_liveness live made the
        verdict depend on WHEN it was asked — one immutable report returned
        HUNG on one render and BLOCKED-HUMAN on the next. The classifier is
        sampled ONCE in health() and rides the row; two renders of the same
        report must agree, and the classifier is never re-consulted."""
        liv = {"state": "BLOCKED_ON_HUMAN",
               "blocked_on": "~/plans/x.md", "evidence": "pane-tail"}
        r = rep(row(seat="codex-2", turn="hung", liveness=liv,
                    turn_evidence="stale"))
        with mock.patch("helm.seat.seat_liveness",
                        side_effect=AssertionError("findings re-read the pane")):
            first = proxywatch.findings(r)
            second = proxywatch.findings(r)
        self.assertEqual(first, second)
        self.assertEqual(first[0][0], "BLOCKED-HUMAN")

    def test_a_liveness_transition_is_a_fingerprint_MOVE(self):
        """The verdict-bearing liveness state rides the fingerprint, so a
        real IDLE->BLOCKED_ON_HUMAN transition on the same hung seat speaks
        once (the state moved), rather than being invisible because the
        watched fingerprint did not change."""
        base = dict(seat="codex-2", turn="hung", turn_evidence="stale")
        fp_blocked = proxywatch.fingerprint(rep(row(
            liveness={"state": "BLOCKED_ON_HUMAN"}, **base)))
        fp_idle = proxywatch.fingerprint(rep(row(
            liveness={"state": "IDLE"}, **base)))
        fp_unknown = proxywatch.fingerprint(rep(row(
            liveness={"state": "UNKNOWN"}, **base)))
        self.assertNotEqual(fp_blocked, fp_idle)
        self.assertNotEqual(fp_blocked, fp_unknown)
        self.assertNotEqual(fp_idle, fp_unknown)

    def test_process_only_LIVE_gets_the_read_first_gate_not_a_resume(self):
        """#141 r2 (review blocker): LIVE is process evidence only — a named
        process exists, no pane tail was read, so nothing cleared this seat of
        being human-blocked. Admitting LIVE to the vocabulary without this arm
        sent it to the destructive else (resume prescribed) — the exact
        weak-evidence->strong-action promotion the lane removes upstream."""
        liv = {"state": "LIVE", "blocked_on": None,
               "evidence": "seat is named by 1 live claude process (pid 7)"}
        f = self._findings(liv)
        self.assertEqual(f[0][0], "HUNG")
        text = f[0][1]
        self.assertIn("PROCESS-ONLY", text)
        self.assertIn("READ THE PANE FIRST", text)
        self.assertNotIn("helm seat resume", text)   # no executable prescription

    def test_a_genuinely_hung_seat_keeps_the_resume_prescription(self):
        """The control: a seat the classifier clears of being human-blocked
        (RUNNING/IDLE/other) is hung in the old sense, and the original
        resume/reseed prescription is the correct one."""
        liv = {"state": "IDLE", "blocked_on": None, "evidence": "pane-tail"}
        f = self._findings(liv)
        self.assertEqual(f[0][0], "HUNG")
        self.assertIn("helm seat resume", f[0][1])


class _HealthRig(unittest.TestCase):
    """Shared rig: run the REAL health() against a tmpdir seat, hermetically.

    The endpoint probe and fuse readers
    (_inflight_for/_ctx_pct/_spawn_age_s/_compact_threshold) are mocked here
    with caller-chosen values — never left to read a live proxy, socket table,
    or seat spawn register from inside a test.
    """

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="helm-test-pwh-")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _health(self, live, age, infl=None, pct=None, spawn=None,
                include_upstream=False, upstream=None, complete=True,
                pending=False, write_age=None, work=1, seat_name="codex",
                runtime_roster=None, parsed_family=("codex", None),
                suspend_gap=0, whole=False):
        """Let health() DECIDE. An earlier draft recomputed hang_candidate in
        this helper and asserted against its own arithmetic — a test that
        cannot fail, which is why the mutation kept passing. A real transcript
        with a back-dated SEMANTIC record makes the module do the work: `age`
        dates the last completed assistant entry, `write_age` (default: age)
        back-dates the file mtime separately so retry churn is expressible.

        `live` is THREE-VALUED, exactly as the census is: True (a readable
        process names the seat), False (helm read every claude on the host and
        none does), None (helm could not read them all, so it cannot say).

        `suspend_gap` IS A READER LIKE EVERY OTHER ONE HERE, and it defaults to
        0 = "the host provably did not suspend". It has to be mocked for the
        same reason the socket census and the spawn register are: `turn_state`
        SUBTRACTS it from `age` before any rung reads the result, so a caller
        that leaves it live is not choosing a staleness at all — the machine
        underneath is. On a host that suspends, the subtraction drops every
        STALE case in this rig below HANG_S at once and the whole ladder
        answers `ok`, which is a true reading of a quantity nobody in these
        arms meant to set. The real reader has its own home: HostSuspendGapTest
        asserts it against the live kernel, and TurnStateHostSuspendTest passes
        values straight into the ladder.
        """
        tp = os.path.join(self.d, "t.jsonl")
        semantic_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                    time.gmtime(time.time() - age))
        assistant = {"type": "assistant", "timestamp": semantic_ts,
                     "message": {"role": "assistant",
                                 "stop_reason":
                                     "end_turn" if complete else "tool_use",
                                 "content": [{"type": "text", "text": "done"}]}}
        with open(tp, "w") as f:
            f.write(json.dumps(assistant) + "\n")
            if pending:
                f.write(json.dumps({"type": "queue-operation",
                                    "timestamp": "2026-07-30T15:01:35.110Z",
                                    "operation": "enqueue"}) + "\n")
        stamp = time.time() - (age if write_age is None else write_age)
        os.utime(tp, (stamp, stamp))
        with mock.patch.object(proxywatch, "_watched_seats", return_value=[seat_name]), \
                mock.patch.object(proxywatch, "probe",
                                  return_value=("healthy", "HTTP 401 fixture", 1)), \
                mock.patch.object(
                    proxywatch, "_live_seats",
                    return_value=({seat_name} if live else set(),
                                  None if live is not None else
                                  "1 live claude process could not be "
                                  "identified (pid 303)", {})), \
                mock.patch("helm.seat._seat_family", return_value=parsed_family), \
                mock.patch("helm.seat._proxy_home", return_value=self.d), \
                mock.patch("helm.seat._instance_dir", return_value=self.d), \
                mock.patch("helm.autocompact._newest_transcript", return_value=tp), \
                mock.patch("helm.pk.read_json", return_value={}), \
                mock.patch("helm.seats.roster",
                           return_value=runtime_roster or {}), \
                mock.patch.object(proxywatch, "host_suspend_gap_s",
                                  return_value=suspend_gap), \
                mock.patch.object(proxywatch, "_inflight_for",
                                  return_value=infl) as self.inflight_reader, \
                mock.patch.object(proxywatch, "_ctx_pct", return_value=pct), \
                mock.patch.object(proxywatch, "_spawn_age_s",
                                  return_value=spawn), \
                mock.patch.object(proxywatch, "_compact_threshold",
                                  return_value=90), \
                mock.patch.object(
                    proxywatch, "_open_work",
                    return_value=None if work is None else {"codex": work}
                ) as self.openwork_reader, \
                mock.patch.object(proxywatch, "upstream_health",
                                  return_value=upstream or {}) as self.upstream_reader:
            report = proxywatch.health(include_upstream=include_upstream)
            return report if whole else report["seats"][0]


class TranscriptRealityTest(unittest.TestCase):
    """The semantic reader — progress is a COMPLETED main-chain entry, and
    file mtime survives only as raw-write age. Fixtures are shaped like the
    2026-07-30 codex-2 victim's transcript, not an imagined schema."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="helm-test-pwtr-")
        self.p = os.path.join(self.d, "session.jsonl")
        self.now = time.time()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _write(self, *records, write_age=0):
        with open(self.p, "w") as f:
            for record in records:
                f.write(json.dumps(record) + "\n")
        stamp = self.now - write_age
        os.utime(self.p, (stamp, stamp))

    def _ts(self, age):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                             time.gmtime(self.now - age))

    def test_65min_queue_churn_does_not_refresh_semantic_age(self):
        """MUST-HIT fixture from review: tool completion at T-65m, then only
        user/queue retry bookkeeping while mtime remains fresh."""
        self._write(
            {"type": "assistant", "timestamp": self._ts(65 * 60),
             "message": {"role": "assistant", "stop_reason": "tool_use",
                         "content": [{"type": "tool_use", "name": "Bash"}]}},
            {"type": "user", "timestamp": self._ts(65 * 60),
             "message": {"role": "user",
                         "content": [{"type": "tool_result", "content": "ok"}]}},
            {"type": "queue-operation", "timestamp": self._ts(1),
             "operation": "enqueue"}, write_age=1)
        got = proxywatch.transcript_reality(self.p, now=self.now)
        self.assertEqual(got["semantic_kind"], "tool-result")
        self.assertAlmostEqual(got["semantic_age_s"], 65 * 60, delta=1)
        self.assertAlmostEqual(got["write_age_s"], 1, delta=0.1)
        self.assertTrue(got["pending_after"])

    def test_user_prompt_and_queue_rows_are_writes_not_completed_progress(self):
        self._write(
            {"type": "assistant", "timestamp": self._ts(120),
             "message": {"role": "assistant", "stop_reason": "end_turn",
                         "content": [{"type": "text", "text": "done"}]}},
            {"type": "queue-operation", "timestamp": self._ts(2),
             "operation": "dequeue"},
            {"type": "user", "timestamp": self._ts(2),
             "message": {"role": "user",
                         "content": [{"type": "text", "text": "continue"}]}},
            write_age=1)
        got = proxywatch.transcript_reality(self.p, now=self.now)
        self.assertAlmostEqual(got["semantic_age_s"], 120, delta=1)
        self.assertTrue(got["pending_after"])

    def test_queue_clearing_after_completed_turn_is_nothing_pending(self):
        self._write(
            {"type": "assistant", "timestamp": self._ts(3600),
             "message": {"role": "assistant", "stop_reason": "end_turn",
                         "content": [{"type": "text", "text": "done"}]}},
            {"type": "queue-operation", "timestamp": self._ts(3),
             "operation": "enqueue"},
            {"type": "queue-operation", "timestamp": self._ts(2),
             "operation": "dequeue"}, write_age=1)
        got = proxywatch.transcript_reality(self.p, now=self.now)
        self.assertEqual(got["semantic_kind"], "assistant")
        self.assertFalse(got["pending_after"])

    def test_popAll_clears_every_pending_queue_item(self):
        self._write(
            {"type": "assistant", "timestamp": self._ts(3600),
             "message": {"role": "assistant", "stop_reason": "end_turn",
                         "content": [{"type": "text", "text": "done"}]}},
            {"type": "queue-operation", "timestamp": self._ts(4),
             "operation": "enqueue"},
            {"type": "queue-operation", "timestamp": self._ts(3),
             "operation": "enqueue"},
            {"type": "queue-operation", "timestamp": self._ts(2),
             "operation": "popAll"}, write_age=1)
        got = proxywatch.transcript_reality(self.p, now=self.now)
        self.assertEqual(got["semantic_kind"], "assistant")
        self.assertFalse(got["pending_after"])

    def test_reverse_scan_crosses_many_chunks_until_semantic_truth(self):
        semantic = {"type": "assistant", "timestamp": self._ts(3600),
                    "message": {"role": "assistant", "stop_reason": "end_turn",
                                "content": [{"type": "text", "text": "done"}]}}
        noise = [{"type": "queue-operation", "timestamp": self._ts(1),
                  "operation": "enqueue", "padding": "x" * 2000}
                 for _ in range(80)]
        self._write(semantic, *noise, write_age=1)
        got = proxywatch.transcript_reality(self.p, now=self.now)
        self.assertAlmostEqual(got["semantic_age_s"], 3600, delta=1)
        self.assertTrue(got["pending_after"])

    def test_a_sidechain_entry_is_not_main_chain_progress(self):  # noqa: VACUOUS_ASSERTION — the 3600s reading IS the positive control: an unfiltered sidechain would measure ~5s
        self._write(
            {"type": "assistant", "timestamp": self._ts(3600),
             "message": {"role": "assistant", "stop_reason": "end_turn",
                         "content": [{"type": "text", "text": "done"}]}},
            {"type": "assistant", "isSidechain": True,
             "timestamp": self._ts(5),
             "message": {"role": "assistant", "stop_reason": "end_turn",
                         "content": [{"type": "text", "text": "sub"}]}},
            write_age=1)
        got = proxywatch.transcript_reality(self.p, now=self.now)
        self.assertAlmostEqual(got["semantic_age_s"], 3600, delta=1)

    def test_an_unreadable_transcript_is_all_None_never_a_measurement(self):  # noqa: VACUOUS_ASSERTION — carries its own unconditional positive control: the same reader measures the same fields once the file exists
        got = proxywatch.transcript_reality(os.path.join(self.d, "absent"),
                                            now=self.now)
        self.assertIsNone(got["semantic_age_s"])
        self.assertIsNone(got["write_age_s"])
        self.assertIsNone(got["pending_after"])
        # positive control on the same observables: once a file exists the
        # same reader MEASURES, so the Nones above were blindness, not habit
        self._write(
            {"type": "assistant", "timestamp": self._ts(60),
             "message": {"role": "assistant", "stop_reason": "end_turn",
                         "content": [{"type": "text", "text": "done"}]}},
            write_age=3)
        got = proxywatch.transcript_reality(self.p, now=self.now)
        self.assertAlmostEqual(got["write_age_s"], 3, delta=0.1)
        self.assertAlmostEqual(got["semantic_age_s"], 60, delta=1)
        self.assertFalse(got["pending_after"])

    def test_a_readable_file_with_no_semantic_record_keeps_write_age(self):
        self._write({"type": "queue-operation", "timestamp": self._ts(4),
                     "operation": "enqueue"}, write_age=7)
        got = proxywatch.transcript_reality(self.p, now=self.now)
        self.assertIsNone(got["semantic_age_s"])
        self.assertAlmostEqual(got["write_age_s"], 7, delta=0.1)
        self.assertTrue(got["pending_after"])


class HealthDerivationTest(_HealthRig):
    """health() — where hang_candidate is DERIVED, not where it is reported.

    Added because a mutation did not bite: flagging a hang without requiring a
    live pane passed all 21 tests. FindingsTest feeds health() a pre-built row
    whose hang_candidate is already decided, so nothing exercised the line that
    decides it. Third time tonight the same gap appeared — testing the
    reporting layer and leaving the derivation uncovered — which is its own
    lesson about where tests naturally land.
    """

    def test_a_DEAD_pane_is_never_a_hang_however_stale(self):
        """A seat nobody launched is not hung, it is off. codex-2's transcript
        is 8795 minutes old right now and it is simply not running."""
        row = self._health(live=False, age=999999)
        self.assertFalse(row["pane_live"])
        self.assertFalse(row["hang_candidate"])

    def test_a_LIVE_pane_past_the_window_IS_a_hang_candidate(self):
        row = self._health(live=True, age=proxywatch.HANG_S + 60)
        self.assertTrue(row["pane_live"])
        self.assertTrue(row["hang_candidate"])

    def test_a_LIVE_pane_INSIDE_the_window_is_not(self):
        row = self._health(live=True, age=60)
        self.assertFalse(row["hang_candidate"])

    def test_pane_liveness_comes_from_the_PROCESS_census(self):
        """Not from the roster, which lags — kimi and grok both had live
        processes and absent roster rows tonight. A hang check keyed on the
        roster would have called two running seats dead."""
        import inspect
        src = inspect.getsource(proxywatch._live_seats)
        self.assertIn("claude_processes", src)

    def test_the_log_rung_is_DERIVED_from_the_seats_own_proxy_log(self):
        """health() must wire logscan to the seat's proxy home — this class's
        own lesson, third time learned: testing the reporting layer and
        leaving the derivation uncovered is where mutations hide."""
        with open(os.path.join(self.d, "proxy.log"), "w") as f:
            f.write("\n".join((
                gin(401, path="/v1/chat/completions?helm_canary=1",
                    ts="2026-07-29 11:16:00"),
                gin(401, path="/v1/chat/completions",
                    ts="2026-07-29 11:17:00"),
                gin(402, ts="2026-07-29 11:18:00"),
                gin(402, ts="2026-07-29 11:19:00"), GIN_402)) + "\n")
        row = self._health(live=True, age=60)
        self.assertEqual(row["log"], "streak")
        self.assertIn("402 x3", row["log_detail"])
        self.assertEqual(row["log_status_401"]["helm_marked"], 1)
        self.assertEqual(row["log_status_401"]["unmarked"], 1)
        self.assertFalse(row["log_status_401"]["scope"]["truncated"])

    def test_a_seat_with_NO_proxy_log_derives_unknown(self):
        row = self._health(live=True, age=60)
        self.assertEqual(row["log"], "unknown")

    def test_upstream_projection_runs_AFTER_and_does_not_replace_turn_reality(self):
        family = {"codex": {"state": "HEALTHY", "detail": "current detail",
                             "ms": 7, "since": "2026-07-31T00:00:00Z"}}
        row = self._health(live=None, age=proxywatch.HANG_S + 60,
                           include_upstream=True, upstream=family)
        self.assertIsNone(row["pane_live"])
        self.assertEqual(row["turn_state"], "hung-unknown")
        self.assertEqual(row["upstream"], "HEALTHY")
        self.assertEqual(row["upstream_detail"], "current detail")
        self.assertEqual(row["upstream_ms"], 7)
        self.assertEqual(row["upstream_since"], "2026-07-31T00:00:00Z")
        self.upstream_reader.assert_called_once()

    def test_verified_runtime_family_reaches_the_canary_group(self):
        runtime = {"pi-codex": {"runtime": {"family": "codex",
                                                "agent_harness": "pi"},
                                "runtime_verified": True}}
        row = self._health(
            live=True, age=60, seat_name="pi-codex", include_upstream=True,
            runtime_roster=runtime, parsed_family=(None, "unknown display"),
            upstream={"codex": {"state": "HEALTHY"}})
        self.assertEqual(row["family"], "codex")
        passed = self.upstream_reader.call_args.args[0]
        self.assertEqual(passed[0]["seat"], "pi-codex")
        self.assertEqual(passed[0]["family"], "codex")

    def test_verified_runtime_family_resolves_a_damaged_register_endpoint(self):
        """A verified roster family can locate an EXISTING endpoint even when
        the old spawn writer omitted family/project. The register remains
        damaged and untouched; this pass only measures the current proxy."""
        runtime = {"seat-under-test": {
            "session": "current-project-session",
            "runtime": {"family": "codex", "agent_harness": "pi"},
            "runtime_verified": True}}
        measured = {}

        def current_proof(seat_name, observed_at=None):
            self.assertEqual(seat_name, "seat-under-test")
            self.assertGreater(observed_at, 0)
            measured.update(runtime_proof(
                session="current-project-session", observed=observed_at,
                route={"alias": "gpt-5.6-sol", "provider": "codex",
                       "upstream_model": "gpt-5.6-sol"}))
            return dict(measured), None

        with mock.patch("helm.seat._existing_instance_port",
                        return_value=8484) as endpoint, \
                mock.patch.object(proxywatch, "proxy_runtime_canary",
                                  side_effect=current_proof) as canary:
            report = self._health(
                live=True, age=60, seat_name="seat-under-test",
                runtime_roster=runtime,
                parsed_family=(None, "spawn register is INCOMPLETE"),
                include_upstream=True, whole=True)
        row = report["seats"][0]
        self.assertEqual(row["family"], "codex")
        self.assertEqual(row["probe"], "healthy")
        self.assertEqual(row["probe_detail"], "HTTP 401 fixture")
        self.assertEqual(report["proxy_runtime"], {"seat-under-test": measured})
        derived, err = proxywatch._proxy_proof_runtime(measured)
        self.assertIsNone(err, err)
        self.assertEqual(derived["family"], "codex")
        endpoint.assert_called_once_with("codex", "seat-under-test")
        canary.assert_called_once_with("seat-under-test",
                                       observed_at=mock.ANY)

    def test_unverified_runtime_family_cannot_bypass_a_damaged_register(self):  # noqa: VACUOUS_ASSERTION — the positive twin above reaches the same endpoint mock; this arm proves only the unverified authority difference keeps it untouched
        """A display mirror is not identity authority. Its unverified family
        field cannot select a provider's endpoint tree."""
        runtime = {"seat-under-test": {
            "runtime": {"family": "codex", "agent_harness": "pi"},
            "runtime_verified": False}}
        with mock.patch("helm.seat._existing_instance_port",
                        return_value=8484) as endpoint:
            row = self._health(
                live=True, age=60, seat_name="seat-under-test",
                runtime_roster=runtime,
                parsed_family=(None, "spawn register is INCOMPLETE"))
        self.assertIn("INCOMPLETE", row["error"])
        self.assertIsNone(row["probe"])
        endpoint.assert_not_called()

    def test_endpoint_resolution_error_survives_the_health_row(self):
        runtime = {"seat-under-test": {
            "runtime": {"family": "codex", "agent_harness": "pi"},
            "runtime_verified": True}}
        with mock.patch("helm.seat._existing_instance_port", return_value=None):
            row = self._health(
                live=True, age=60, seat_name="seat-under-test",
                runtime_roster=runtime,
                parsed_family=(None, "spawn register is INCOMPLETE"))
        self.assertIsNone(row["probe"])
        self.assertIn("no resolvable proxy endpoint", row["probe_detail"])

    def test_local_only_health_never_calls_the_authenticated_rung(self):
        self._health(live=True, age=60, include_upstream=False)
        self.upstream_reader.assert_not_called()


class TurnStateFuseTest(unittest.TestCase):
    """The HUNG fuse — the named verdict no single probe could give.

    Measured live TWICE, 2026-07-29: the primary codex seat hung, pane LIVE,
    turn loop DEAD. proxywatch said pane=live+transcript-stale (honest, not a
    verdict); the drop watchdog was structurally blind (a turn that never
    completes writes no row to count); roster said amber; chat said quiet.
    Every probe was RIGHT about its own question and the fleet still could not
    name the state — a human had to hand-count transcript rows. Bug class:
    watchdogs-correct-composition-holed.

    These tests drive the pure fuse BOTH directions: HUNG fires only when
    every rung composes, and each benign or blind case gets its own distinct
    verdict — never collapsed, never guessed.
    """

    @staticmethod
    def fuse(live=True, age=proxywatch.HANG_S + 60, log="ok", infl=0,
             pct=34.2, thr=90, spawn=2 * 3600, reality=None, work=1):
        """Defaults are the FULLY COMPOSED hung shape (one open dispatch is
        the pending work); each test bends one rung and asserts the verdict
        moves to that rung's name."""
        return proxywatch.turn_state(live, age, log, infl, pct, thr, spawn,
                                     reality=reality, open_dispatches=work)

    def test_HUNG_fires_when_every_rung_composes(self):
        state, ev = self.fuse()
        self.assertEqual(state, "hung")
        # the evidence must carry the composed signals, not just the name
        self.assertIn("0 in-flight", ev)
        self.assertIn("stale", ev)
        self.assertIn("34.2%", ev)
        self.assertIn("not fresh", ev)

    def test_an_open_request_is_THINKING_never_hung(self):
        """The rung that kills the false alarm: a long legitimate generation
        holds an ESTABLISHED connection for its whole life, and an open
        request IS the turn running. Calling it hung would page the owner on
        every 50-minute think."""
        state, ev = self.fuse(infl=2)
        self.assertEqual(state, "thinking")
        self.assertIn("2 in-flight", ev)

    def test_an_unreadable_socket_census_is_HUNG_UNKNOWN_never_hung(self):
        """THE LOAD-BEARING LAW, same as logscan's: a check that cannot see a
        case returns UNKNOWN, never a false verdict. None is 'blind', 0 is a
        measurement — collapsing them composes a false HUNG out of blindness."""
        state, ev = self.fuse(infl=None)
        self.assertEqual(state, "hung-unknown")
        self.assertIn("socket census", ev)

    def test_unreadable_context_is_HUNG_UNKNOWN(self):
        state, ev = self.fuse(pct=None)
        self.assertEqual(state, "hung-unknown")
        self.assertIn("context%", ev)

    def test_unreadable_spawn_age_is_HUNG_UNKNOWN(self):
        state, ev = self.fuse(spawn=None)
        self.assertEqual(state, "hung-unknown")
        self.assertIn("spawn age", ev)

    def test_a_fresh_spawn_is_FRESH_not_hung(self):
        """`helm seat resume` relaunches onto an OLD transcript, so the stale
        gate passes from second one — only the spawn register knows the seat
        is starting, not hung. Tonight's incident ended in exactly this shape:
        a reseed whose first minutes must not re-page the owner."""
        state, ev = self.fuse(spawn=120)
        self.assertEqual(state, "fresh")
        self.assertIn("starting", ev)

    def test_the_fresh_window_has_an_edge_and_hung_resumes_past_it(self):
        state, _ = self.fuse(spawn=proxywatch.SPAWN_FRESH_S)
        self.assertEqual(state, "fresh")
        state, _ = self.fuse(spawn=proxywatch.SPAWN_FRESH_S + 1)
        self.assertEqual(state, "hung")

    def test_at_the_compact_bar_is_COMPACT_NEEDED(self):
        """The known-benign class: a seat wedged at the compact threshold is
        autocompact's to fix, and naming it HUNG would send a resume at a seat
        that needs a /compact."""
        state, ev = self.fuse(pct=93.0)
        self.assertEqual(state, "compact-needed")
        self.assertIn("93.0%", ev)

    def test_a_starved_seat_reads_STARVED_never_hung(self):
        """The vocabulary stays distinct: the log rung OWNS the starvation
        class (grok's two-day 402 wall), and the fuse defers to it rather than
        re-diagnosing a refused seat as a hung one."""
        state, ev = self.fuse(log="streak")
        self.assertEqual(state, "starved")
        self.assertIn("STARVED", ev)

    def test_a_dead_pane_is_OFF_however_composed_the_rest_looks(self):
        state, _ = self.fuse(live=False)
        self.assertEqual(state, "off")

    def test_a_recent_turn_is_ok_and_reads_nothing_else(self):
        state, _ = self.fuse(age=60, infl=None, pct=None, spawn=None)
        self.assertEqual(state, "ok")

    def test_no_transcript_on_a_young_process_is_fresh(self):
        """A seat that has never turned has no transcript to date — on a young
        process that is starting, not unknown."""
        state, _ = self.fuse(age=None, spawn=120)
        self.assertEqual(state, "fresh")

    def test_no_transcript_on_an_old_process_is_HUNG_UNKNOWN_not_hung(self):
        """Stale-ness itself is unmeasurable without a dated semantic entry,
        and HUNG requires the measurement — an undated seat can never compose
        it."""
        state, ev = self.fuse(age=None, spawn=2 * 3600)
        self.assertEqual(state, "hung-unknown")
        self.assertIn("semantic transcript age", ev)

    def test_a_seat_OUT_OF_WORK_is_IDLE_not_hung(self):
        """Task #36's negative arm: an old transcript on a seat with NOTHING
        pending is idleness. The hang remedy (resume/reseed) aimed at an idle
        seat kills a healthy pane."""
        state, ev = self.fuse(work=0)
        self.assertEqual(state, "idle")
        self.assertIn("out of work", ev)

    def test_a_seat_WITH_open_work_and_a_stale_transcript_IS_hung(self):
        """The positive control on the same switch: the identical stale shape
        with open dispatch rows addressed to the seat composes HUNG, and the
        evidence carries the count."""
        state, ev = self.fuse(work=2)
        self.assertEqual(state, "hung")
        self.assertIn("2 open dispatches", ev)

    def test_an_unreadable_dispatch_ledger_is_HUNG_UNKNOWN_never_idle(self):
        """The tri-state law at the new input: a ledger helm could not read
        must not measure as an empty board (false IDLE) nor as work (false
        HUNG)."""
        state, ev = self.fuse(work=None)
        self.assertEqual(state, "hung-unknown")
        self.assertIn("open-dispatch census", ev)

    def test_retry_churn_with_a_held_socket_is_HUNG_not_thinking(self):
        """THE 2026-07-30 MUST-HIT: the victim held a socket for 65 minutes
        while queue-operation retries kept mtime fresh and no semantic entry
        completed. A held socket is pending work, never proof of progress."""
        reality = {"semantic_age_s": 65 * 60, "write_age_s": 30,
                   "turn_complete": False, "pending_after": True,
                   "newest_type": "queue-operation"}
        state, ev = self.fuse(age=65 * 60, infl=2, reality=reality)
        self.assertEqual(state, "hung")
        self.assertIn("churn", ev)
        self.assertIn("queue/retry", ev)

    def test_a_silent_long_generation_keeps_THINKING_with_reality_present(self):
        """The false alarm the in-flight rung exists to kill, preserved under
        turn reality: nothing written since the prompt + a held socket is a
        long legitimate generation."""
        reality = {"semantic_age_s": 65 * 60, "write_age_s": 65 * 60,
                   "turn_complete": False, "pending_after": True,
                   "newest_type": "user"}
        state, ev = self.fuse(age=65 * 60, infl=1, reality=reality)
        self.assertEqual(state, "thinking")
        self.assertIn("long generation", ev)

    def test_a_held_socket_with_unreadable_write_age_is_HUNG_UNKNOWN(self):
        reality = {"semantic_age_s": 65 * 60, "write_age_s": None,
                   "turn_complete": False, "pending_after": True}
        state, ev = self.fuse(age=65 * 60, infl=1, reality=reality)
        self.assertEqual(state, "hung-unknown")
        self.assertIn("raw-write age", ev)

    def test_a_queued_prompt_after_a_completed_turn_is_not_idle(self):
        """Transcript-pending work counts as open work even with zero
        dispatch rows: a queued owner prompt the seat never picked up is the
        hang shape, not idleness."""
        reality = {"semantic_age_s": proxywatch.HANG_S + 60, "write_age_s": 30,
                   "turn_complete": True, "pending_after": True}
        state, ev = self.fuse(reality=reality, work=0)
        self.assertEqual(state, "hung")
        self.assertIn("queued transcript work", ev)

    def test_an_open_turn_after_tool_use_is_not_idle(self):
        reality = {"semantic_age_s": proxywatch.HANG_S + 60, "write_age_s": 30,
                   "turn_complete": False, "pending_after": False}
        state, ev = self.fuse(reality=reality, work=0)
        self.assertEqual(state, "hung")
        self.assertIn("open turn", ev)

    def test_a_completed_turn_with_nothing_pending_is_IDLE_even_when_old(self):
        reality = {"semantic_age_s": proxywatch.HANG_S + 60, "write_age_s": 30,
                   "turn_complete": True, "pending_after": False}
        state, ev = self.fuse(reality=reality, work=0)
        self.assertEqual(state, "idle")
        self.assertIn("nothing queued", ev)

    def test_an_unreadable_turn_completion_is_HUNG_UNKNOWN(self):
        """reality's own fields keep the tri-state discipline: a readable file
        whose completion state could not be derived is blindness, not an
        empty pending census."""
        reality = {"semantic_age_s": proxywatch.HANG_S + 60, "write_age_s": 30,
                   "turn_complete": None, "pending_after": False}
        state, ev = self.fuse(reality=reality, work=0)
        self.assertEqual(state, "hung-unknown")
        self.assertIn("turn completion", ev)


class InflightTest(unittest.TestCase):
    """The socket census — how 'in-flight' is actually read, and its honesty.

    NOT from proxy.log: the gin logger writes ONE line per request, at
    COMPLETION, with its duration — an open/streaming request writes NOTHING
    until it finishes, so log-silence cannot distinguish a 90-minute
    generation from a dead turn loop. The kernel socket table can: each
    accepted client connection is one ESTABLISHED row whose LOCAL port is the
    proxy's. Measured live 2026-07-29 across all five seats: codex mid-work
    held 5, kimi/ds4pro 1 each, idle grok and gemini exactly 0.

    Fixtures are byte-shaped like real /proc/net/tcp rows; no live table is
    ever read from a test.
    """

    HDR = ("  sl  local_address rem_address   st tx_queue rx_queue tr "
           "tm->when retrnsmt   uid  timeout inode")
    PORT = 0x207D                            # 8317, codex's — shape realism

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="helm-test-pwi-")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    @staticmethod
    def _v4(lport, st, rport=0xD4A2):
        return ("   1: 0100007F:%04X 0100007F:%04X %s 00000000:00000000 "
                "00:00000000 00000000  1000        0 12345 1 "
                "0000000000000000 20 4 30 10 -1" % (lport, rport, st))

    def _table(self, name, *rows):
        p = os.path.join(self.d, name)
        with open(p, "w") as f:
            f.write("\n".join((self.HDR,) + rows) + "\n")
        return p

    def test_counts_only_ESTABLISHED_rows_whose_LOCAL_port_is_the_proxys(self):
        """One localhost connection appears TWICE in the table — once from
        each end. Counting the proxy-side rows (local port = listen port)
        counts each connection once; the client-side twin, the LISTEN row
        (state 0A), and other ports' traffic are all not it."""
        t = self._table("tcp",
                        self._v4(self.PORT, "0A", rport=0),      # the listener
                        self._v4(self.PORT, "01"),               # proxy side
                        self._v4(0xD4A2, "01", rport=self.PORT),  # its twin
                        self._v4(0x2082, "01"))                  # another port
        self.assertEqual(proxywatch.inflight(8317, tables=(t,)), 1)

    def test_zero_is_a_MEASUREMENT_true_silence_not_blindness(self):
        """The reading the fuse needs: a readable table with no established
        rows is affirmative silence — the hung-composable condition."""
        t = self._table("tcp", self._v4(self.PORT, "0A", rport=0))
        self.assertEqual(proxywatch.inflight(8317, tables=(t,)), 0)

    def test_an_unreadable_table_is_None_never_zero(self):
        """A census that reports silence when it is blind is how a false HUNG
        gets composed — the same unknown-stays-unknown law as logscan."""
        gone = os.path.join(self.d, "absent")
        self.assertIsNone(proxywatch.inflight(
            8317, tables=(gone, gone + "6")))

    def test_one_readable_table_is_enough_to_measure(self):
        t = self._table("tcp6",
                        "   0: 00000000000000000000000001000000:207D "
                        "00000000000000000000000001000000:D4A2 01 "
                        "00000000:00000000 00:00000000 00000000  1000        "
                        "0 12345 1 0000000000000000 20 4 30 10 -1")
        self.assertEqual(proxywatch.inflight(
            8317, tables=(os.path.join(self.d, "absent"), t)), 1)


class TheRigReadsNoLiveHostStateTest(_HealthRig):
    """A RIG THAT CALLS A LIVE-HOST READER LETS THE MACHINE DECIDE THE VERDICT.

    `turn_state` SUBTRACTS the host suspend gap from `age` before any rung
    reads it, so an unmocked reader silently rewrites the one input every arm
    in this file sets on purpose. The failure is invisible on a host that does
    not suspend and total on one that does: a server reads 0 and every arm
    passes, a laptop reads hours and every STALE arm in the rig answers `ok`
    together — the same tree, the same order, opposite results.

    That shape is worth naming because it reads exactly like test-order
    dependence from inside a single run and is not: nothing seeds it, running
    alone changes nothing, and re-running reproduces it perfectly. The
    discriminator is to run the same arms on a DIFFERENT HOST, or to drive the
    reader's value directly, which is what these two arms do.
    """

    STALE = proxywatch.HANG_S + 60

    def test_an_ambient_suspend_reading_cannot_reach_the_verdict(self):
        """THE HERMETIC CONTRACT. A live reader answering hours is patched in
        AROUND the rig; the rig's own mock must win, or this whole file's
        staleness is set by whichever machine ran it."""
        with mock.patch.object(proxywatch, "host_suspend_gap_s",
                               return_value=10 * 3600):
            row = self._health(live=True, age=self.STALE, infl=1, pct=30.0,
                               spawn=2 * 3600)
        self.assertEqual(row["turn_state"], "thinking")

    def test_the_rigs_own_knob_still_drives_the_subtraction(self):
        """THE MUST-MISS, and it is what keeps the arm above from passing on a
        rig that had simply stopped subtracting at all: asked for a suspend,
        the ladder must still correct the age and answer `ok`."""
        row = self._health(live=True, age=self.STALE, infl=1, pct=30.0,
                           spawn=2 * 3600, suspend_gap=10 * 3600)
        self.assertEqual(row["turn_state"], "ok")

    def test_a_suspend_reading_helm_cannot_take_is_not_zero(self):
        """THE KNOB IS THREE-VALUED BECAUSE THE READER IS, and `defaults to 0`
        reads past that. 0 says the host provably did not suspend; None says
        helm could not tell, and those license opposite verdicts on the same
        staleness — `hung` is a confident claim the ladder may only make when
        it knows the elapsed time was the seat's own.

        WHAT IS ALREADY PINNED ELSEWHERE, so this arm is not the coverage it
        looks like: TurnStateHostSuspendTest drives the None branch DIRECTLY at
        the ladder, with an unreadable suspend inside and outside the stale
        window. What no arm reached was that pole through the COMPOSED door —
        `health()` passes the reader's answer straight through, so the
        pass-through is where a future caller would coerce None to 0 and turn
        every unreadable reading into a confident hang.

        ITS SIBLINGS ARE ITS CONTROLS and all three differ in this one input:
        0 through the knob answers `thinking` (the ambient arm above), a real
        suspend answers `ok`, and an unreadable one answers `hung-unknown`. An
        implementation that collapsed None into either neighbour fails here.
        """
        row = self._health(live=True, age=self.STALE, infl=1, pct=30.0,
                           spawn=2 * 3600, suspend_gap=None)
        self.assertEqual(row["turn_state"], "hung-unknown")
        self.assertIn("suspended", row["turn_evidence"] or "")


class HungVerdictDerivationTest(_HealthRig):
    """health() — where the fused verdict is DERIVED, not where it is reported.

    The same lesson HealthDerivationTest carries (third time that night):
    testing the reporting layer and leaving the derivation uncovered is where
    mutations hide. These run the REAL health() and let it wire the readers
    into the fuse.
    """

    STALE = proxywatch.HANG_S + 60

    def test_health_DERIVES_hung_when_every_signal_composes(self):
        row = self._health(live=True, age=self.STALE, infl=0, pct=30.0,
                           spawn=2 * 3600)
        self.assertEqual(row["turn_state"], "hung")
        self.assertIn("0 in-flight", row["turn_evidence"])
        self.assertTrue(row["hang_candidate"])

    def test_an_open_request_derives_THINKING_not_hung(self):
        row = self._health(live=True, age=self.STALE, infl=1, pct=30.0,
                           spawn=2 * 3600)
        self.assertEqual(row["turn_state"], "thinking")

    def test_an_unreadable_census_derives_HUNG_UNKNOWN(self):
        row = self._health(live=True, age=self.STALE, infl=None, pct=30.0,
                           spawn=2 * 3600)
        self.assertEqual(row["turn_state"], "hung-unknown")
        self.assertIn("socket census", row["turn_evidence"])

    def test_a_fresh_spawn_derives_fresh_not_hung(self):
        row = self._health(live=True, age=self.STALE, infl=0, pct=30.0,
                           spawn=60)
        self.assertEqual(row["turn_state"], "fresh")

    def test_at_the_compact_bar_derives_compact_needed(self):
        row = self._health(live=True, age=self.STALE, infl=0, pct=95.0,
                           spawn=2 * 3600)
        self.assertEqual(row["turn_state"], "compact-needed")

    def test_STARVED_still_reads_starved_with_the_fuse_in_place(self):
        """The no-regression direction: a refusal streak in the seat's own
        proxy.log must still read STARVED — from the log rung, verbatim — and
        the fuse must defer to it, not re-verdict the seat as hung."""
        with open(os.path.join(self.d, "proxy.log"), "w") as f:
            f.write("\n".join((gin(402, ts="2026-07-29 11:18:00"),
                                gin(402, ts="2026-07-29 11:19:00"),
                                GIN_402)) + "\n")
        row = self._health(live=True, age=self.STALE, infl=0, pct=30.0,
                           spawn=2 * 3600)
        self.assertEqual(row["log"], "streak")
        self.assertEqual(row["turn_state"], "starved")
        f = proxywatch.findings(rep(row))
        self.assertEqual([lvl for lvl, _t in f], ["STARVED"])

    def test_the_fuse_readers_are_LAZY_a_recent_turn_reads_nothing(self):  # noqa: VACUOUS_ASSERTION — turn_state=="ok" is the positive control; the not-called arms are the laziness contract, and sibling derivation tests prove the same readers fire for candidates
        """A fleet that turned recently pays nothing new: the socket census
        never runs for a seat whose answer cannot change the verdict, and the
        dispatch ledger is never folded for it either."""
        row = self._health(live=True, age=60, infl=0, pct=30.0, spawn=3600)
        self.assertEqual(row["turn_state"], "ok")
        self.inflight_reader.assert_not_called()
        self.openwork_reader.assert_not_called()

    def test_a_dead_pane_derives_off(self):
        row = self._health(live=False, age=self.STALE)
        self.assertEqual(row["turn_state"], "off")

    def test_an_old_completed_turn_with_NO_open_work_derives_IDLE(self):
        """Task #36's negative arm at the derivation layer: stale transcript,
        live pane, everything readable, zero pending anywhere — IDLE, and the
        hang-candidate flag does not survive the verdict."""
        row = self._health(live=True, age=self.STALE, infl=0, pct=30.0,
                           spawn=2 * 3600, work=0)
        self.assertEqual(row["turn_state"], "idle")
        self.assertFalse(row["hang_candidate"],
                         "an idle seat was queued for the hang remedy")
        self.assertEqual(row["open_dispatches"], 0)
        self.assertEqual(proxywatch.findings(rep(row)), [])

    def test_the_same_stale_shape_WITH_open_work_stays_a_hang_candidate(self):
        """The positive control on the same switch (task #36's other arm)."""
        row = self._health(live=True, age=self.STALE, infl=0, pct=30.0,
                           spawn=2 * 3600, work=1)
        self.assertEqual(row["turn_state"], "hung")
        self.assertTrue(row["hang_candidate"])
        self.assertEqual(row["open_dispatches"], 1)

    def test_display_case_seat_reads_the_canonical_open_work_count(self):
        row = self._health(live=True, age=self.STALE, infl=0, pct=30.0,
                           spawn=2 * 3600, work=2, seat_name="CoDeX")
        self.assertEqual(row["open_dispatches"], 2)
        self.assertEqual(row["turn_state"], "hung")

    def test_a_blind_dispatch_ledger_derives_HUNG_UNKNOWN_not_idle(self):
        row = self._health(live=True, age=self.STALE, infl=0, pct=30.0,
                           spawn=2 * 3600, work=None)
        self.assertEqual(row["turn_state"], "hung-unknown")
        self.assertIn("open-dispatch census", row["turn_evidence"])
        self.assertIsNone(row["open_dispatches"])

    def test_health_DERIVES_churn_hung_from_semantic_stall_plus_fresh_writes(self):
        """The 2026-07-30 victim end to end: real transcript with a 46m-old
        open turn, queue row and fresh mtime, a held socket — health() must
        compose HUNG, not thinking."""
        row = self._health(live=True, age=self.STALE, infl=1, pct=30.0,
                           spawn=2 * 3600, complete=False, pending=True,
                           write_age=1, work=1)
        self.assertEqual(row["turn_state"], "hung")
        self.assertIn("queue/retry", row["turn_evidence"])
        self.assertTrue(row["hang_candidate"])

    def test_health_derives_THINKING_for_a_silent_long_generation(self):
        """The no-regression twin: same held socket, but nothing written since
        the prompt — the long-generation shape stays thinking."""
        row = self._health(live=True, age=self.STALE, infl=1, pct=30.0,
                           spawn=2 * 3600, complete=False, pending=True,
                           write_age=self.STALE, work=1)
        self.assertEqual(row["turn_state"], "thinking")


class BlindCensusIsNotOffTest(_HealthRig):
    """A PROCESS CENSUS THAT COULD NOT LOOK IS NOT A SEAT NOBODY LAUNCHED.

    The round-7 finding (dispatch 90845108, tip 4995691): "proxywatch maps
    it to off". `_live_seats()` filtered `procs` for a HELM_CHAT_NAME and threw
    the `unreadable` half away, so a seat whose only claude process helm could
    not identify came back absent from the live set — and `turn_state` reads an
    absent seat as `off`, which this module's own vocabulary defines as "a seat
    nobody launched is not hung".

    That is the exact collapse `hung-unknown` exists to prevent, one rung
    lower. The fuse already refuses to guess when the socket census or the
    context read is blind; the PANE read was the one input that answered a
    blind read with a confident verdict.
    """

    STALE = proxywatch.HANG_S + 60

    def test_a_BLIND_census_derives_HUNG_UNKNOWN_not_off(self):
        """THE MUST-HIT. Nothing about this seat is known: no readable process
        names it, and a live claude on the host could not be identified. `off`
        says helm looked and found nobody — it did not look."""
        row = self._health(live=None, age=self.STALE, infl=0, pct=30.0,
                           spawn=2 * 3600)
        self.assertEqual(
            row["turn_state"], "hung-unknown",
            "a blind process census reported the seat as DELIBERATELY OFF: %s"
            % (row["turn_evidence"],))
        self.assertIn("pane", row["turn_evidence"])

    def test_a_BLIND_census_is_never_a_HANG_either(self):
        """The other direction, and it is the reason `hung-unknown` is a
        distinct word rather than a bias: UNKNOWN must not collapse into `off`
        OR into `hung`. A watchdog acting on either guess acts wrong."""
        row = self._health(live=None, age=self.STALE, infl=0, pct=30.0,
                           spawn=2 * 3600)
        self.assertNotEqual(row["turn_state"], "hung")
        self.assertFalse(row["hang_candidate"],
                         "a seat helm could not see was queued for the hang "
                         "remedy")

    def test_a_PROVEN_absence_still_derives_off(self):
        """THE CONTROL. A census that looked at every claude on the host and
        found none naming this seat has ANSWERED, and `off` is that answer. A
        fix that made every unlaunched seat read `hung-unknown` would bury the
        real ones."""
        row = self._health(live=False, age=self.STALE, infl=0, pct=30.0,
                           spawn=2 * 3600)
        self.assertEqual(row["turn_state"], "off")

    def test_a_LIVE_seat_still_derives_its_real_verdict(self):
        """The positive control on the other side of the same switch."""
        row = self._health(live=True, age=self.STALE, infl=0, pct=30.0,
                           spawn=2 * 3600)
        self.assertEqual(row["turn_state"], "hung")

    def test_the_census_reports_BLINDNESS_from_the_unreadable_bucket(self):
        """The derivation, not the rig: `_live_seats` itself must carry the
        fact. Mocking it in the tests above proves `health` USES the flag;
        this proves the flag is actually produced."""
        from helm import orcaadopt
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([], [303])):
            names, blind, _per = proxywatch._live_seats()
        self.assertEqual(names, set())
        self.assertTrue(blind, "the unreadable bucket was dropped on the floor")
        self.assertIn("303", blind)
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([{"pid": 1, "seat": "codex"}], [])):
            names, blind, _per = proxywatch._live_seats()
        self.assertEqual(names, {"codex"})
        self.assertIsNone(blind, "a readable census must not report blindness")

    def test_a_census_that_RAISES_is_blind_not_empty(self):
        """The same rule at the same function's other exit. `except Exception:
        return set()` said "no seat has a live pane" about a scan that never
        completed — every watched seat then read `off`."""
        from helm import orcaadopt
        with mock.patch.object(orcaadopt, "claude_processes",
                               side_effect=OSError("/proc is not mounted")):
            names, blind, _per = proxywatch._live_seats()
        self.assertEqual(names, set())
        self.assertTrue(blind, "a census that RAISED reported an empty fleet")


class UpstreamSnapshotTest(unittest.TestCase):
    """The CACHED provider verdict, for surfaces that must never probe.

    proxywatch classified RATE-LIMITED / MALFORMED200 / AUTH-UNAVAILABLE for a
    long time and NOTHING read it: `helm seat where codex` printed "LIVE;
    liveness IDLE" while codex had been hard-walled for three hours. Measured
    2026-08-04: that false picture cost four reviewers 20-153 minutes each."""

    def snap(self, state, err=None, now=None):
        with mock.patch.object(proxywatch, "_read_watch_state",
                               return_value=(state, err)):
            return proxywatch.upstream_snapshot(now=now)

    def test_a_fresh_record_returns_the_verdict(self):
        up, err = self.snap({"ts": 1000, "upstream": {"codex": {
            "state": "RATE-LIMITED", "dark": True}}}, now=1060)
        self.assertIsNone(err)
        self.assertEqual(up["codex"]["state"], "RATE-LIMITED")

    # noqa: VACUOUS_ASSERTION — the control is the FIRST snap() in this same
    # test: the identical record one second INSIDE the bar returns a verdict
    # (assertIsNone(err) + assertIsNotNone(up)). The rung cannot follow it
    # because `up` is then reassigned for the stale case.
    def test_a_STALE_record_is_an_ERROR_not_a_verdict(self):
        """THE FAILURE A CACHE ADDS THAT THE SOURCE DOES NOT HAVE. A record
        from a watcher that stopped an hour ago describes a world that no
        longer exists; rendering it as current rebuilds the exact false picture
        this exists to end, in fresher-looking words."""
        # CONTROL FIRST: the same record INSIDE the bar does return a verdict,
        # so the error below measures the age and not a broken reader.
        up, err = self.snap({"ts": 1000, "upstream": {"codex": {"state": "X"}}},
                            now=1000 + proxywatch.UPSTREAM_CACHE_FRESH_S - 1)
        self.assertIsNone(err)
        self.assertIsNotNone(up)
        up, err = self.snap({"ts": 1000, "upstream": {"codex": {"state": "X"}}},
                            now=1000 + proxywatch.UPSTREAM_CACHE_FRESH_S + 1)
        # STRUCTURAL and unconditional: the refusal NAMES the age it measured,
        # so this is the staleness rung and not some other early return.
        self.assertIn("watcher is not running", err)
        self.assertIn("bar %dm" % (proxywatch.UPSTREAM_CACHE_FRESH_S // 60), err)
        self.assertIsNone(up)

    def test_an_unreadable_state_is_an_error_never_empty(self):
        """The corrupt-outbox case: a blind read must never masquerade as
        clean. Here that means it must not look like 'no families are dark'."""
        up, err = self.snap({}, err="corrupt json")
        self.assertIsNone(up)
        self.assertIn("unreadable", err)

    def test_delivery_reader_recovers_last_good_dark_after_primary_damage(self):
        with tempfile.TemporaryDirectory() as tmp:
            primary = os.path.join(tmp, "proxywatch.json")
            backup = primary + ".last-good"
            with open(primary, "w", encoding="utf-8") as handle:
                handle.write("{corrupt")
            with open(backup, "w", encoding="utf-8") as handle:
                json.dump({"ts": 1000, "upstream": {"codex": {
                    "state": "AUTH-401", "dark": True}}}, handle)
            with mock.patch.object(proxywatch, "_state_path",
                                   return_value=primary), \
                    mock.patch.object(proxywatch, "_backup_state_path",
                                      return_value=backup):
                state, err = proxywatch._read_delivery_state()
                pause, perr = proxywatch.delivery_pause(
                    "codex", state=state, now=1001)
            self.assertIsNone(err)
            self.assertIsNone(perr)
            self.assertEqual(pause["state"], "AUTH-401")

    def test_delivery_pause_holds_dark_until_measured_healthy(self):
        dark = {"ts": 1000, "upstream": {"codex": {
            "state": "RATE-LIMITED", "dark": True,
            "since": "episode-start"}}}
        pause, err = proxywatch.delivery_pause("codex-2", state=dark,
                                                now=10 ** 9)
        self.assertIsNone(err)
        self.assertEqual(pause["family"], "codex")
        self.assertEqual(pause["state"], "RATE-LIMITED")
        self.assertTrue(pause["stale"],
                        "stale observation must hold, not invent recovery")

        unknown = {"ts": 1001, "upstream": {"codex": {
            "state": "UNKNOWN", "dark": True,
            "since": "episode-start"}}}
        pause, err = proxywatch.delivery_pause("codex", state=unknown,
                                                now=1002)
        self.assertIsNone(err)
        self.assertEqual(pause["state"], "UNKNOWN")

        healthy = {"ts": 1003, "upstream": {"codex": {
            "state": "HEALTHY", "dark": False}}}
        pause, err = proxywatch.delivery_pause("codex", state=healthy,
                                                now=1004)
        self.assertIsNone(err)
        self.assertIsNone(pause)

    def test_delivery_pause_is_family_scoped_and_corruption_holds_unknown(self):
        dark = {"ts": 1000, "upstream": {
            "codex": {"state": "AUTH-UNAVAILABLE", "dark": True},
            "kimi": {"state": "HEALTHY", "dark": False}}}
        self.assertIsNotNone(proxywatch.delivery_pause(
            "codex-3", state=dark, now=1001)[0])
        self.assertIsNotNone(proxywatch.delivery_pause(
            "pi-codex", state=dark, now=1001,
            runtime={"family": "codex", "agent_harness": "pi"},
            runtime_verified=True)[0])
        self.assertIsNone(proxywatch.delivery_pause(
            "kimi", state=dark, now=1001)[0])
        self.assertEqual(proxywatch.delivery_pause(
            "pi-codex", state=dark, now=1001,
            runtime={"family": "codex"}, runtime_verified=False),
            (None, None), "a foreign runtime mirror cannot steer delivery")
        self.assertEqual(proxywatch.delivery_pause(
            "helm-claude", state=dark, now=1001), (None, None))
        pause, err = proxywatch.delivery_pause("codex", state=[])
        self.assertIsNotNone(pause)
        self.assertEqual(pause["state"], "UNKNOWN")
        self.assertIn("not an object", err)
        for malformed in (
                {"upstream": ["valid-json-wrong-shape"]},
                {"upstream": {"codex": {
                    "state": "UNKNOWN", "dark": "false"}}}):
            pause, err = proxywatch.delivery_pause("codex", state=malformed)
            self.assertIsNotNone(pause)
            self.assertTrue(err)

    def test_the_reason_carries_no_verdict_word(self):
        """REASON AND VERDICT ARE SPLIT — the caller owns the word UNKNOWN. The
        first version returned both and the surface printed
        'upstream UNKNOWN (... upstream UNKNOWN)'."""
        # UNCONDITIONAL, not inside the loop: an empty iterable or a False
        # branch would skip every assertion and the test would still pass.
        _up, err = self.snap({}, err="corrupt json")
        self.assertTrue(err, "control: this path DOES produce a reason")
        self.assertNotIn("UNKNOWN", err)
        _up2, err2 = self.snap({"ts": 1, "upstream": {"a": {}}}, now=10 ** 9)
        self.assertTrue(err2)
        self.assertNotIn("UNKNOWN", err2)


class HostSuspendGapTest(unittest.TestCase):
    """The kernel read that ARMS the suspend correction. Without it the whole
    mechanism lands inert — a parameter no caller ever supplies, which is the
    landed-but-not-in-force shape.

    CLOCK_BOOTTIME advances while the machine is suspended; CLOCK_MONOTONIC
    does not, so their difference IS the suspended total. A fact, not an
    inference — which is why it is the authority and the lockstep signature is
    only its corroborator."""

    def test_it_returns_a_real_reading_on_THIS_host(self):
        """THE ONLY UNMOCKED ASSERTION IN THIS CLASS, and the one that matters:
        the reader must work against the actual kernel, not just against my
        doubles. A mocked-only suite would prove I can parse numbers I invented."""
        v = proxywatch.host_suspend_gap_s()
        self.assertIsInstance(v, int, "a live host must yield a NUMBER, not None")
        self.assertGreaterEqual(v, 0)

    def test_the_noise_band_around_zero_is_ZERO_and_never_UNKNOWN(self):
        """MEASURED LIVE AND IT WAS A REAL DEFECT. The two clock reads are not
        simultaneous, so a never-suspended host yields a delta a few
        NANOSECONDS either side of zero. The first version returned None for
        -0.0000001, which would have made every stale seat hung-unknown and
        suppressed hang detection entirely. Clean mock integers would never
        have shown it."""
        # UNCONDITIONAL POSITIVE CONTROL, same mock path: a delta OUTSIDE the
        # band still reports its value. Without it, `== 0` would also hold for
        # a reader hardcoded to return zero — which is the very shape that
        # would suppress every hang verdict.
        with mock.patch.object(proxywatch.time, "clock_gettime",
                               side_effect=[103600.0, 100000.0]):
            self.assertEqual(proxywatch.host_suspend_gap_s(), 3600)
        with mock.patch.object(proxywatch.time, "clock_gettime",
                               side_effect=[100.0, 100.0000001]):
            self.assertEqual(proxywatch.host_suspend_gap_s(), 0)

    def test_a_real_suspend_is_reported_in_seconds(self):
        with mock.patch.object(proxywatch.time, "clock_gettime",
                               side_effect=[107200.0, 100000.0]):
            self.assertEqual(proxywatch.host_suspend_gap_s(), 7200)

    def test_broken_clock_semantics_are_UNKNOWN_never_zero(self):
        """A substantially negative delta means the two clocks do not mean what
        this depends on. Reporting 0 there is the single change that would turn
        the guard back into the six false HUNGs it exists to prevent."""
        with mock.patch.object(proxywatch.time, "clock_gettime",
                               side_effect=[100.0, 500.0]):
            self.assertIsNone(proxywatch.host_suspend_gap_s())

    def test_a_kernel_without_BOOTTIME_is_UNKNOWN(self):
        with mock.patch.object(proxywatch.time, "clock_gettime",
                               side_effect=OSError("unsupported")):
            self.assertIsNone(proxywatch.host_suspend_gap_s())


class TurnStateHostSuspendTest(unittest.TestCase):
    """A HOST SUSPEND IS NOT A PROPERTY OF ANY SEAT, AND THE LADDER ASKED ONLY
    SEATS. Staleness is measured in WALL time and wall time keeps counting
    while the box is suspended, so every seat comes back stale by the length of
    the suspend, all at once. Six seats each answered "I am stale" truthfully
    and the composition invented six hangs (measured: six lockstep false
    HUNGs, the FIX that opened this lane).

    THE FIX IS NOT A NEW VERDICT BESIDE `hung` — it is that the AGE was never
    the seat's elapsed time. Subtracting the suspend restores the quantity
    every rung below already reasons about correctly.

    From the meld: the suspend adds the SAME delta to every seat, so the
    seats are SIMILAR and never identical; and UNKNOWN must never read as "no
    suspend", which is why the input is three-valued like `pane_live`."""

    BASE = dict(pane_live=True, log_state="ok", inflight_n=0, ctx_pct=10,
                ctx_threshold=90, spawn_age=99999, open_dispatches=3,
                reality={"write_age_s": 9999, "turn_complete": True,
                         "pending_after": 2})

    def verdict(self, age, gap):
        return proxywatch.turn_state(age=age, suspend_gap_s=gap, **self.BASE)

    def test_the_SAME_wall_age_is_hung_or_ok_depending_only_on_the_suspend(self):
        """THE WHOLE FINDING IN ONE ASSERTION: identical inputs except the
        host-level fact, opposite correct verdicts. The control is the first
        line — without it this would also pass for a ladder that never says
        hung at all."""
        hung_v, hung_ev = self.verdict(7500, 0)
        ok_v, _ok_ev = self.verdict(7500, 7200)
        self.assertEqual((hung_v, ok_v), ("hung", "ok"))
        # STRUCTURAL, unconditional: the hung verdict names the measured age it
        # reasoned from. A pair of bare labels would also be produced by a
        # ladder that pattern-matched the gap without reading the transcript.
        self.assertIn("stale 125m", hung_ev)

    def test_a_seat_hung_ACROSS_a_suspend_still_reads_hung(self):
        """The subtraction removes ONLY time the host provably did not run.
        Excess staleness survives it, so this never becomes a way to hide a
        real hang behind a suspend."""
        v, ev = self.verdict(20000, 7200)
        self.assertEqual(v, "hung")
        self.assertIn("stale 213m", ev)          # 333m wall - 120m suspend

    def test_an_UNREADABLE_suspend_is_hung_unknown_never_hung(self):
        """UNKNOWN NEVER READS AS "NO SUSPEND". A reading that could not look
        has made no claim, and collapsing it into zero is exactly how one
        box-level event becomes N false hangs."""
        v, ev = self.verdict(7500, None)
        self.assertEqual(v, "hung-unknown")
        self.assertIn("HOST was suspended", ev)
        # and it is NOT the pre-existing unknown rungs speaking:
        self.assertNotIn("cannot see:", ev)

    def test_an_unreadable_suspend_INSIDE_the_stale_window_changes_nothing(self):
        """Narrow by construction: only a seat that would otherwise be a HANG
        CANDIDATE is affected. Inside the window the suspend cannot change the
        answer, so an unreadable gap must not manufacture doubt."""
        v, ev = self.verdict(60, None)
        self.assertEqual(v, "ok")
        # STRUCTURAL: `ok` here is the fresh-enough rung returning early with
        # no evidence, NOT the suspend rung having quietly fired and said
        # nothing. Those are different code paths with the same label.
        self.assertIsNone(ev)

    def test_the_default_is_zero_so_every_existing_caller_is_unchanged(self):
        """UNCONDITIONAL CONTROL on the seam: callers that never heard of this
        input must behave exactly as before, or the parameter would be a
        silent behaviour change to every existing verdict."""
        without = proxywatch.turn_state(age=7500, **self.BASE)
        self.assertEqual(without, self.verdict(7500, 0))
        self.assertEqual(without[0], "hung")


class TurnVerdictFindingsTest(unittest.TestCase):
    """The verdict's reporting layer — one finding, with evidence and action.

    A detector that computes HUNG but never surfaces it is a sensor with no
    actuator; a verdict without its evidence is a guess with confidence."""

    def test_HUNG_carries_the_evidence_and_the_action(self):
        """A seat the pane-tail classifier clears of being human-blocked is
        hung in the old sense, and the resume/reseed prescription is correct.
        (The classifier is mocked CLEAN here — an unreadable one degrades the
        prescription instead; BlockedOnHumanVerdictTest owns that arm.)"""
        liv = {"state": "IDLE", "blocked_on": None, "evidence": "pane-tail"}
        f = proxywatch.findings(rep(row(
            hang=True, turn="hung", liveness=liv,
            turn_evidence="pane LIVE; transcript stale 62m > 45m; 0 in-flight "
                          "connections at the proxy port")))
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0][0], "HUNG")
        self.assertIn("0 in-flight", f[0][1])          # the composed evidence
        self.assertIn("helm seat resume codex", f[0][1])  # the action
        self.assertIn("reseed", f[0][1])   # two resumes did not stick tonight

    def test_THINKING_asks_no_question_and_raises_no_alarm(self):
        """The false alarm the in-flight rung exists to kill: a stale live
        pane WITH an open request is a long generation, and neither HUNG nor
        the old HANG? question may fire over it."""
        self.assertEqual(proxywatch.findings(
            rep(row(hang=True, turn="thinking"))), [])

    def test_compact_needed_fresh_and_idle_are_answers_not_alarms(self):  # noqa: VACUOUS_ASSERTION — deliberate-absence contract; the positive control on the same observable is test_HUNG_carries_the_evidence_and_the_action
        for turn in ("compact-needed", "fresh", "idle"):
            self.assertEqual(proxywatch.findings(
                rep(row(hang=False, turn=turn))), [],
                "%s is an ANSWER to the hang question, not a finding" % turn)
        for turn in ("compact-needed", "fresh"):
            self.assertEqual(proxywatch.findings(
                rep(row(hang=True, turn=turn))), [],
                "%s is an ANSWER to the hang question, not a finding" % turn)

    def test_HUNG_UNKNOWN_keeps_the_question_and_names_the_blind_spot(self):
        """Where the fuse cannot see, the honest HANG? question survives —
        enriched with WHICH input was unreadable, never upgraded to a verdict."""
        f = proxywatch.findings(rep(row(
            hang=True, age=3600, turn="hung-unknown",
            turn_evidence="cannot see: socket census")))
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0][0], "HANG?")
        self.assertIn("Not a verdict", f[0][1])
        self.assertIn("cannot see: socket census", f[0][1])


class LogScanTest(unittest.TestCase):
    """The LOG rung — the grok incident's failure class, pinned.

    Measured 2026-07-29: Grok's proxy.log held a two-day HTTP 402 wall while
    the watch covered only codex-family seats. The transport class was fully
    legible on disk; owner intent was not — Grok is deliberately parked pending
    CLI proxy cursor support, not waiting on a payment. These tests pin both the
    observed line shape and the boundary between evidence and remedy.
    """

    NOISE = ("[2026-07-29 04:27:02] [--------] [info ] [model_updater.go:133] "
             "periodic model refresh completed, no changes detected")

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="helm-test-pwl-")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _scan(self, *lines):
        p = os.path.join(self.d, "proxy.log")
        with open(p, "w") as f:
            f.write("\n".join(lines) + "\n")
        return proxywatch.logscan(p)

    def test_a_402_streak_on_the_REAL_line_shape_is_a_streak(self):
        """Three of grok's verbatim 402 lines, with the refresh noise the real
        log interleaves — the scanner must find the streak between them."""
        state, detail = self._scan(
            self.NOISE, gin(402, ts="2026-07-29 11:18:00"),
            gin(402, ts="2026-07-29 11:19:00"), self.NOISE, GIN_402)
        self.assertEqual(state, "streak")
        self.assertIn("HTTP 402 x3", detail)

    def test_the_streak_finding_names_the_starvation(self):
        state, detail = self._scan(
            gin(402, ts="2026-07-29 11:18:00"),
            gin(402, ts="2026-07-29 11:19:00"), GIN_402)
        f = proxywatch.findings(rep(row(log=state, log_detail=detail,
                                         turn="starved")))
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0][0], "STARVED")
        self.assertIn("proxy refusing", f[0][1])
        self.assertIn("HTTP 402 x3", f[0][1])
        self.assertIn("starvation", f[0][1])

    def test_a_short_5xx_burst_with_a_healthy_turn_is_BLIP_not_STARVED(self):
        """The live codex-2 false alarm, synthetic but exact in shape: four
        503s in four seconds beside probe=healthy and turn=ok. A server burst
        is upstream evidence, never credential evidence, and its four-second
        window is below the named STARVED threshold."""
        lines = [gin(503, level="error", ts=ts) for ts in (
            "2026-07-29 22:37:59", "2026-07-29 22:38:00",
            "2026-07-29 22:38:01", "2026-07-29 22:38:03")]
        state, detail = self._scan(*lines)
        self.assertEqual(state, "blip")
        self.assertIn("UPSTREAM/transient", detail)
        self.assertIn("4 refusals across 4s", detail)
        self.assertIn("60s starvation threshold", detail)
        f = proxywatch.findings(rep(row(log=state, log_detail=detail,
                                         probe="healthy", turn="ok")))
        self.assertEqual([lvl for lvl, _text in f], ["BLIP"])
        self.assertNotIn("STARVED", f[0][1])
        self.assertNotIn("credential/billing", f[0][1])
        self.assertIn("taking turns", f[0][1])

    def test_each_status_class_says_only_what_its_codes_support(self):
        cases = (((401, 403, 401), "AUTH"),
                 ((402, 402, 402), "BILLING/quota-exhausted"),
                 ((429, 429, 429), "RATE-LIMITED"),
                 ((500, 502, 503), "UPSTREAM/transient"))
        for codes, cause in cases:
            with self.subTest(codes=codes):
                state, detail = self._scan(*[
                    gin(code, level="error" if code >= 500 else "warn ", ts=ts)
                    for code, ts in zip(codes, (
                        "2026-07-29 11:18:00", "2026-07-29 11:19:00",
                        "2026-07-29 11:20:00"))])
                self.assertEqual(state, "streak")
                self.assertIn("cause %s" % cause, detail)

    def test_fork_error_bodies_name_overload_not_generic_5xx(self):
        """Our CLIProxyAPI fork's silent-swallow-fix logs the quoted error
        body; unanimous named bodies outrank the code class."""
        body = ('{"type":"error","error":{"type":'
                '"service_unavailable_error","message":'
                '"Our servers are currently overloaded"}}')
        state, detail = self._scan(
            gin(503, level="error", ts="2026-07-29 11:18:00", body=body),
            gin(503, level="error", ts="2026-07-29 11:19:00", body=body),
            gin(503, level="error", ts="2026-07-29 11:20:00", body=body))
        self.assertEqual(state, "streak")
        self.assertIn("cause UPSTREAM-OVERLOADED", detail)

    def test_fork_error_bodies_keep_auth_unavailable_out_of_overload(self):
        body = ('{"type":"error","error":{"message":'
                '"auth_unavailable: no auth available '
                '(providers=xai, model=grok-build-0.1)"}}')
        state, detail = self._scan(
            gin(503, level="error", ts="2026-07-29 11:18:00", body=body),
            gin(503, level="error", ts="2026-07-29 11:19:00", body=body),
            gin(503, level="error", ts="2026-07-29 11:20:00", body=body,
                truncated=True))
        self.assertEqual(state, "streak")
        self.assertIn("cause AUTH-UNAVAILABLE", detail)
        self.assertNotIn("UPSTREAM-OVERLOADED", detail)

    def test_disagreeing_bodies_stay_cause_UNKNOWN(self):
        """Mixed named bodies establish refusal, not one cause — the same
        no-laundering law the mixed-code cluster already carries."""
        overload = ('{"type":"error","error":{"type":'
                    '"service_unavailable_error","message":"overloaded"}}')
        auth = ('{"type":"error","error":{"message":'
                '"auth_unavailable: no auth available"}}')
        state, detail = self._scan(
            gin(503, level="error", ts="2026-07-29 11:18:00", body=overload),
            gin(503, level="error", ts="2026-07-29 11:19:00", body=auth),
            gin(503, level="error", ts="2026-07-29 11:20:00", body=overload))
        self.assertEqual(state, "streak")
        self.assertIn("cause UNKNOWN", detail)

    def test_upstream_rows_without_bodies_remain_compatible(self):
        """The unforked shape is the compatibility contract: no suffix, code
        classes decide — exactly the pre-body behavior."""
        state, detail = self._scan(
            gin(503, level="error", ts="2026-07-29 11:18:00"),
            gin(503, level="error", ts="2026-07-29 11:19:00"),
            gin(503, level="error", ts="2026-07-29 11:20:00"))
        self.assertEqual(state, "streak")
        self.assertIn("cause UPSTREAM/transient", detail)

    def test_a_sustained_refusal_beside_a_healthy_turn_reports_disagreement(self):
        state, detail = self._scan(
            gin(503, level="error", ts="2026-07-29 11:18:00"),
            gin(503, level="error", ts="2026-07-29 11:19:00"),
            gin(503, level="error", ts="2026-07-29 11:20:00"))
        f = proxywatch.findings(rep(row(log=state, log_detail=detail,
                                         probe="healthy", turn="ok")))
        self.assertEqual([lvl for lvl, _text in f], ["REFUSAL"])
        self.assertIn("UPSTREAM/transient", f[0][1])
        self.assertIn("taking turns", f[0][1])
        self.assertNotIn("agent is starved", f[0][1])
        self.assertNotIn("credential/billing", f[0][1])

    def test_a_sustained_402_with_a_starved_turn_stays_STARVED_and_billing(self):
        """The grok positive control: a refusal cluster spanning the threshold
        plus a starved turn is a real starvation finding, and 402 says billing
        or quota exhaustion rather than a generic guess."""
        state, detail = self._scan(
            gin(402, ts="2026-07-29 11:18:00"),
            gin(402, ts="2026-07-29 11:19:00"),
            gin(402, ts="2026-07-29 11:20:00"))
        self.assertEqual(state, "streak")
        self.assertIn("BILLING/quota-exhausted", detail)
        f = proxywatch.findings(rep(row(log=state, log_detail=detail,
                                         probe="healthy", turn="starved")))
        self.assertEqual([lvl for lvl, _text in f], ["STARVED"])
        self.assertIn("billing", f[0][1].lower())
        self.assertIn("two-day INERT 402 wall", f[0][1])
        self.assertIn("deliberately parked pending CLI proxy cursor support",
                      f[0][1])
        self.assertIn("not waiting on a payment", f[0][1])

    def test_a_sustained_mixed_cluster_names_cause_UNKNOWN(self):
        """Mixed code classes establish refusal, not cause. The alert may name
        starvation from liveness, but it may not launder mixed evidence into a
        credential, billing, rate-limit, or upstream diagnosis."""
        state, detail = self._scan(
            gin(402, ts="2026-07-29 11:18:00"),
            gin(503, level="error", ts="2026-07-29 11:19:00"),
            gin(402, ts="2026-07-29 11:20:00"))
        self.assertEqual(state, "streak")
        self.assertIn("cause UNKNOWN", detail)
        f = proxywatch.findings(rep(row(log=state, log_detail=detail,
                                         turn="starved")))
        self.assertEqual([lvl for lvl, _text in f], ["STARVED"])
        self.assertIn("cause UNKNOWN", f[0][1])
        self.assertNotIn("likely credential/billing", f[0][1])

    def test_a_streak_that_ENDED_is_healthy_history_is_not_an_alarm(self):
        """Recent successes after the errors mean the seat recovered — a
        watch that alerts on history teaches its reader to ignore it."""
        state, _d = self._scan(GIN_402, GIN_402, GIN_402, gin(200))
        self.assertEqual(state, "ok")
        self.assertEqual(proxywatch.findings(rep(row(log="ok"))), [])

    def test_an_unreadable_log_is_UNKNOWN_never_healthy(self):  # noqa: VACUOUS_ASSERTION — unreadable None has a readable-log positive control below
        observed = proxywatch.log_observation(os.path.join(self.d, "absent.log"))
        self.assertEqual(observed["state"], "unknown")
        self.assertNotIn(observed["state"], ("ok", "idle"))
        self.assertIn("unreadable", observed["detail"])
        self.assertIsNone(observed["status_401"])
        # Compatibility control: existing refusal-state callers get the same
        # honest UNKNOWN rather than a new accounting-shaped exception.
        self.assertEqual(proxywatch.logscan(os.path.join(self.d, "absent.log"))[0],
                         "unknown")
        # Unconditional positive control on the same accounting observable: a
        # readable log produces measured counts, so None above means blindness.
        p = os.path.join(self.d, "present.log")
        with open(p, "w") as f:
            f.write(gin(401, path="/v1/chat/completions?helm_canary=1") + "\n")
        self.assertIsNotNone(proxywatch.log_observation(p)["status_401"])

    def test_unknown_is_not_a_finding_either(self):
        """Unknown stays unknown: no evidence of health, no evidence of
        failure. Saying either would be the lie."""
        self.assertEqual(
            proxywatch.findings(rep(row(log="unknown",
                                        log_detail="unreadable"))), [])

    def test_the_watch_never_counts_its_OWN_reflection(self):
        """The probe rung writes a 401 refusal to POST /v1/chat/completions on
        every healthy pass — codex's real tail carries three in ONE second
        (2026-07-29 12:48:21) — and the smoke checks GET /v1/models without a
        credential. Counting those would alarm on every quiet seat forever."""
        state, _d = self._scan(gin(401, path="/v1/chat/completions"),
                               gin(401, path="/v1/chat/completions"),
                               gin(401, path="/v1/chat/completions"),
                               gin(401, path="/v1/models", method="GET"))
        self.assertEqual(state, "idle")

    def test_HELM_MARKED_canary_traffic_is_not_agent_refusal_evidence(self):
        state, detail = self._scan(
            gin(503, path="/v1/messages?beta=true&helm_canary=1",
                level="error", ts="2026-07-29 11:18:00"),
            gin(503, path="/v1/messages?beta=true&helm_canary=1",
                level="error", ts="2026-07-29 11:19:00"),
            gin(503, path="/v1/messages?beta=true&helm_canary=1",
                level="error", ts="2026-07-29 11:20:00"))
        self.assertEqual(state, "idle")
        self.assertIn("no agent traffic", detail)

    def test_401_accounting_separates_HELM_CANARY_from_unmarked_traffic(self):
        """#97's must-hit fixture: a raw status census called every 401 an
        external hammer even though the request path already carried provenance.
        Both canary endpoints and their unmarked controls live in one log so a
        classifier that counts only by endpoint, or only by status, fails."""
        p = os.path.join(self.d, "proxy.log")
        lines = (
            gin(401, path="/v1/chat/completions?helm_canary=1"),
            gin(401, path="/v1/messages?beta=true&helm_canary=1"),
            gin(401, path="/v1/chat/completions"),
            gin(401, path="/v1/messages?beta=true"),
            gin(401, path="/v1/messages?not_helm_canary=1"),
            gin(200, path="/v1/messages?beta=true"),
        )
        with open(p, "w") as f:
            f.write("\n".join(lines) + "\n")
        observed = proxywatch.log_observation(p)
        self.assertEqual(observed["status_401"], {
            "helm_marked": 2, "unmarked": 3, "total": 5,
            "basis": "request query marker helm_canary=1",
            "scope": {"bytes": os.path.getsize(p), "truncated": False},
        })
        # Positive control on the refusal classifier: accounting must not change
        # the established answer that the latest real agent request recovered.
        self.assertEqual(observed["state"], "ok")
        rendered_row = row()
        rendered_row["log_status_401"] = observed["status_401"]
        rendered = "\n".join(proxywatch.report_lines(rep(rendered_row)))
        self.assertIn("401[tail=%dB,complete]=helm-marked:2/unmarked:3" %
                      os.path.getsize(p), rendered)

    def test_401_accounting_names_a_TRUNCATED_tail_scope(self):
        """A zero in a bounded tail is not a zero in the file. Put an older
        unmarked 401 outside the read window and require both the scoped count
        and the owner-facing truncation label."""
        p = os.path.join(self.d, "proxy.log")
        older = gin(401, path="/v1/messages?beta=true")
        recent = gin(401, path="/v1/chat/completions?helm_canary=1")
        with open(p, "w") as f:
            f.write(older + "\n" + "x" * 200 + "\n" + recent + "\n")
        tail_bytes = len(recent.encode("utf-8")) + 10
        observed = proxywatch.log_observation(p, tail_bytes=tail_bytes)
        self.assertEqual(observed["status_401"]["helm_marked"], 1)
        self.assertEqual(observed["status_401"]["unmarked"], 0)
        self.assertEqual(observed["status_401"]["scope"],
                         {"bytes": tail_bytes, "truncated": True})
        rendered_row = row()
        rendered_row["log_status_401"] = observed["status_401"]
        rendered = "\n".join(proxywatch.report_lines(rep(rendered_row)))
        self.assertIn("401[tail=%dB,truncated]" % tail_bytes, rendered)

    def test_the_grok_shape_mixed_503_and_402_is_still_ONE_streak(self):
        """grok's actual tail mixed 503 bursts with the 402s — the streak is
        class-level (refusals), not per-status."""
        state, detail = self._scan(
            gin(503, level="error", ts="2026-07-29 11:18:00"),
            gin(503, level="error", ts="2026-07-29 11:19:00"), GIN_402)
        self.assertEqual(state, "streak")
        self.assertIn("HTTP 402 x3", detail)
        self.assertIn("503", detail)
        self.assertIn("cause UNKNOWN", detail)

    def test_fewer_than_N_trailing_refusals_is_not_a_streak(self):
        state, _d = self._scan(gin(200), GIN_402, GIN_402)
        self.assertEqual(state, "ok")

    def test_a_log_with_no_agent_traffic_is_idle_not_ok(self):
        state, _d = self._scan(self.NOISE, self.NOISE)
        self.assertEqual(state, "idle")


class RefusalBodyQuotedTest(unittest.TestCase):
    """REFUSAL-class rows must carry the evidence FAMILY-DARK rows already
    carry: the refusal body, quoted, and a NAMED cause when the body is the
    known content-classifier verdict.

    Measured 2026-08-08: codex-3 refused in three 502 bursts whose bodies
    named the cause verbatim, and every alert printed "cause UNKNOWN" while
    the string sat in proxy.log all day. The owner found it by reading a
    pane. The body is the evidence; a summary that drops it makes the owner
    the parser of last resort.
    """

    # The incident body, shape-faithful: the classifier verdict rides an
    # ordinary error envelope on a 5xx whose status code says nothing.
    CYBER = ('{"error":{"message":"This content was flagged for possible '
             'cybersecurity risk. See https://chatgpt.com/cyber",'
             '"type":"invalid_request_error"}}')

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="helm-test-pwb-")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _scan(self, *lines):
        p = os.path.join(self.d, "proxy.log")
        with open(p, "w") as f:
            f.write("\n".join(lines) + "\n")
        return proxywatch.logscan(p)

    def test_the_cyber_classifier_body_is_a_NAMED_cause_not_UNKNOWN(self):
        """Mutation M1: deleting the classifier branch in _upstream_state
        drops the cause to UPSTREAM-5XX and this arm goes red."""
        state, detail = self._scan(
            gin(502, level="error", ts="2026-08-08 11:18:00", body=self.CYBER),
            gin(502, level="error", ts="2026-08-08 11:19:00", body=self.CYBER),
            gin(502, level="error", ts="2026-08-08 11:20:00", body=self.CYBER))
        self.assertEqual(state, "streak")
        self.assertIn("CONTENT-FLAGGED/cyber-classifier", detail)

    def test_the_named_cause_and_body_reach_the_STARVED_finding(self):
        """The surface proof — computed-then-discarded is the class this lane
        exists to kill, so the assertion drives findings(), not the parser."""
        state, detail = self._scan(
            gin(502, level="error", ts="2026-08-08 11:18:00", body=self.CYBER),
            gin(502, level="error", ts="2026-08-08 11:19:00", body=self.CYBER),
            gin(502, level="error", ts="2026-08-08 11:20:00", body=self.CYBER))
        f = proxywatch.findings(rep(row(log=state, log_detail=detail,
                                         turn="starved")))
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0][0], "STARVED")
        self.assertIn("CONTENT-FLAGGED/cyber-classifier", f[0][1])
        self.assertIn("flagged for possible cybersecurity risk", f[0][1])

    def test_the_NEWEST_body_is_the_one_quoted(self):
        """Mutation M2: deleting the quote-append in _log_state reds this arm
        and the two above on their body assertions."""
        state, detail = self._scan(
            gin(502, level="error", ts="2026-08-08 11:18:00",
                body='{"error":"older body"}'),
            gin(502, level="error", ts="2026-08-08 11:19:00",
                body='{"error":"older body"}'),
            gin(502, level="error", ts="2026-08-08 11:20:00",
                body='{"error":"newest body"}'))
        self.assertIn("newest body", detail)
        self.assertNotIn("older body", detail)

    def test_no_body_means_no_quote_and_no_crash(self):
        """The unforked-seat control: upstream proxy lines carry no
        response_body suffix, and the summary keeps its old shape."""
        state, detail = self._scan(
            gin(502, level="error", ts="2026-08-08 11:18:00"),
            gin(502, level="error", ts="2026-08-08 11:19:00"),
            gin(502, level="error", ts="2026-08-08 11:20:00"))
        self.assertEqual(state, "streak")
        self.assertNotIn("; body ", detail)

    def test_a_verbose_body_is_bounded_not_flooding(self):
        big = '{"error":"' + "x" * 500 + '"}'
        state, detail = self._scan(
            gin(502, level="error", ts="2026-08-08 11:18:00", body=big),
            gin(502, level="error", ts="2026-08-08 11:19:00", body=big),
            gin(502, level="error", ts="2026-08-08 11:20:00", body=big))
        self.assertIn("; body ", detail)
        self.assertIn("...", detail)
        # The property is flood-prevention, not a span length: the body is
        # clipped mid-summary (the threshold clause follows it), so assert
        # the 500-x run cannot reach the report while its head still does.
        self.assertIn("x" * 40, detail)
        self.assertNotIn("x" * 200, detail)

    RATE = ('{"type":"error","error":{"type":"rate_limit_error",'
            '"message":"no available credential; cooling down"}}')

    def test_a_log_body_alone_does_not_claim_proxy_origin(self):
        self.assertIn("cooling down", self.RATE,
                      "the must-miss must carry proxy-like prose")
        state, detail = self._scan(
            gin(429, level="warn ", ts="2026-08-09 11:23:11", body=self.RATE),
            gin(429, level="warn ", ts="2026-08-09 11:25:00", body=self.RATE),
            gin(429, level="warn ", ts="2026-08-09 11:27:14", body=self.RATE))
        self.assertEqual(state, "streak")
        self.assertIn("cause RATE-LIMITED", detail)
        self.assertNotIn("cause PROXY-COOLDOWN", detail)
        self.assertIn("no available credential; cooling down", detail)

    def test_response_body_cannot_forge_a_legacy_local_origin(self):
        self.assertEqual(proxywatch._logged_refusal_origin(
            gin(429, origin="local", body="ordinary")), "local")
        body = "provider text | refusal_origin_v1=local | more text"
        lines = [gin(429, ts=ts, body=body) for ts in (
            "2026-08-25 06:48:37", "2026-08-25 06:49:49",
            "2026-08-25 06:51:41")]
        self.assertIsNone(proxywatch._logged_refusal_origin(lines[0]))
        state, detail = self._scan(*lines)
        self.assertEqual(state, "streak")
        self.assertIn("cause RATE-LIMITED", detail)
        self.assertNotIn("cause PROXY-COOLDOWN", detail)

    def test_typed_provider_origin_survives_a_hostile_response_body(self):
        body = "provider text | refusal_origin_v1=local | more text"
        lines = [gin(429, ts=ts, body=body, origin="provider") for ts in (
            "2026-08-25 06:48:37", "2026-08-25 06:49:49",
            "2026-08-25 06:51:41")]
        self.assertEqual(
            proxywatch._logged_refusal_origin(lines[0]), "provider")
        state, detail = self._scan(*lines)
        self.assertEqual(state, "streak")
        self.assertIn("cause RATE-LIMITED", detail)
        self.assertNotIn("cause PROXY-COOLDOWN", detail)

    def test_malformed_response_body_cannot_reopen_origin_scanning(self):
        self.assertEqual(proxywatch._logged_refusal_origin(
            gin(429, origin="local", body="ordinary")), "local")
        line = gin(429, origin="local") + ' | response_body="unterminated'
        self.assertIsNone(proxywatch._logged_refusal_origin(line))
        state, detail = self._scan(
            line.replace("2026-07-29 11:20:00", "2026-08-25 06:48:37"),
            line.replace("2026-07-29 11:20:00", "2026-08-25 06:49:49"),
            line.replace("2026-07-29 11:20:00", "2026-08-25 06:51:41"))
        self.assertEqual(state, "streak")
        self.assertIn("cause RATE-LIMITED", detail)
        self.assertNotIn("cause PROXY-COOLDOWN", detail)

    def test_provider_and_untrusted_origin_keep_identical_prose_rate_limited(self):  # noqa: VACUOUS_ASSERTION — the loop's local/provider/unknown cases each assert the same cause surface
        body = ('{"type":"error","error":{"type":"rate_limit_error",'
                '"message":"All credentials for model gpt-5.6-sol are cooling '
                'down via provider codex"}}')
        for origin in ("provider", "unknown", "LOCAL", "local|provider",
                       "local | refusal_origin_v1=local", None):
            with self.subTest(origin=origin):
                lines = [gin(429, ts=ts, body=body, origin=origin) for ts in (
                    "2026-08-25 06:48:37", "2026-08-25 06:49:49",
                    "2026-08-25 06:51:41")]
                state, detail = self._scan(*lines)
                self.assertEqual(state, "streak")
                self.assertIn("cause RATE-LIMITED", detail)
                self.assertNotIn("cause PROXY-COOLDOWN", detail)

    def test_typed_local_origin_does_not_depend_on_body_logging(self):
        state, detail = self._scan(
            gin(429, ts="2026-08-25 06:48:37", origin="local"),
            gin(429, ts="2026-08-25 06:49:49", origin="local"),
            gin(429, ts="2026-08-25 06:51:41", origin="local"))
        self.assertEqual(state, "streak")
        self.assertIn("cause PROXY-COOLDOWN", detail)
        self.assertNotIn("; body ", detail)

    def test_mixed_typed_origins_without_bodies_stay_unknown(self):
        state, detail = self._scan(
            gin(429, ts="2026-08-25 06:48:37", origin="local"),
            gin(429, ts="2026-08-25 06:49:49", origin="provider"),
            gin(429, ts="2026-08-25 06:51:41", origin="local"))
        self.assertEqual(state, "streak")
        self.assertIn("cause UNKNOWN", detail)
        self.assertNotIn("cause PROXY-COOLDOWN", detail)
        self.assertNotIn("; body ", detail)
        self.assertEqual(proxywatch._refusal_cause(
            (429, 429), (None,), ("local", "provider")), "UNKNOWN")
        self.assertEqual(proxywatch._refusal_cause(
            (429, 503, 503), (None, None, None),
            ("local", None, None)), "UNKNOWN")

    def test_typed_local_origin_reaches_exact_starved_operator_output(self):
        body = ('{"type":"error","error":{"type":"rate_limit_error",'
                '"message":"All credentials for model gpt-5.6-sol are cooling '
                'down via provider codex"}}')
        state, detail = self._scan(
            gin(429, ts="2026-08-25 06:48:37", body=body, origin="local"),
            gin(429, ts="2026-08-25 06:49:49", body=body, origin="local"),
            gin(429, ts="2026-08-25 06:51:41", body=body, origin="local"))
        self.assertEqual(
            detail,
            "cause PROXY-COOLDOWN; HTTP 429 x3, 2026-08-25 06:48:37 → "
            "2026-08-25 06:51:41; body %r; 3 refusals across 184s >= 60s "
            "starvation threshold" % body)
        self.assertEqual(proxywatch.findings(rep(row(
            seat="seat-under-test", log=state, log_detail=detail, turn="starved"))), [(
                "STARVED",
                "seat-under-test: proxy refusing (%s) — sustained refusal plus "
                "turn=starved establishes starvation; the status-code class "
                "above is the cause evidence. Grok's two-day INERT 402 wall "
                "(2026-07-27→29) made this transport class legible while the "
                "watch's seat list was one family short. It does not establish "
                "the remedy or deployment intent: Grok is deliberately parked "
                "pending CLI proxy cursor support, not waiting on a payment."
                % detail)])

    def test_a_content_flag_survives_a_mixed_streak(self):
        """MEASURED LIVE 2026-08-09 on the cure that was landed to end exactly
        this silence: a seat refused 11 times in 243s carrying the classifier
        body, ONE 429 joined the window, and the alert printed cause UNKNOWN —
        because disagreeing bodies collapse the cause, which is correct for a
        question only one answer can win.

        A CONTENT FLAG IS A DIFFERENT QUESTION. "this request's text was
        refused" stays true whether or not a rate-limit shared the window, so
        it is reported as its own clause while the cause stays honestly
        UNKNOWN. Mutation: delete the mixed-streak clause and the flag
        vanishes from a window that contains it."""
        state, detail = self._scan(
            gin(502, level="error", ts="2026-08-09 11:23:11", body=self.CYBER),
            gin(502, level="error", ts="2026-08-09 11:25:00", body=self.CYBER),
            gin(429, level="warn ", ts="2026-08-09 11:27:14", body=self.RATE))
        self.assertEqual(state, "streak")
        self.assertIn("cause UNKNOWN", detail,
                      "disagreeing bodies must still collapse the CAUSE — "
                      "this arm must not pass because the cure overrode the "
                      "disagreement rule")
        self.assertIn("CONTENT-FLAGGED/cyber-classifier", detail,
                      "the actionable fact must survive the disagreement")
        self.assertIn("route this content to another family", detail)

    def test_a_unanimous_flag_is_not_reported_twice(self):
        """The control that keeps the new clause from becoming noise: when the
        cause ALREADY names the flag, the extra sentence is redundant and must
        not appear, or every classifier incident carries the same claim twice
        and readers learn to skim it."""
        state, detail = self._scan(
            gin(502, level="error", ts="2026-08-09 11:23:11", body=self.CYBER),
            gin(502, level="error", ts="2026-08-09 11:25:00", body=self.CYBER),
            gin(502, level="error", ts="2026-08-09 11:27:14", body=self.CYBER))
        self.assertEqual(state, "streak")
        self.assertIn("cause CONTENT-FLAGGED/cyber-classifier", detail)
        self.assertNotIn("AT LEAST ONE REFUSAL", detail)

    def test_a_mixed_streak_with_no_flag_says_nothing_about_flagging(self):
        """The must-MISS. A window of ordinary disagreeing refusals must not
        acquire a content-flag claim — the clause fires on a MEASURED body,
        never on the mere fact that the cause collapsed."""
        state, detail = self._scan(
            gin(503, level="error", ts="2026-08-09 11:23:11",
                body='{"error":{"message":"upstream overloaded"}}'),
            gin(503, level="error", ts="2026-08-09 11:25:00",
                body='{"error":{"message":"upstream overloaded"}}'),
            gin(429, level="warn ", ts="2026-08-09 11:27:14", body=self.RATE))
        self.assertEqual(state, "streak")
        self.assertNotIn("CONTENT-FLAGGED", detail)
        self.assertNotIn("AT LEAST ONE REFUSAL", detail)


class ProbeTest(unittest.TestCase):
    """The rung that ASKS the proxy, rather than reading about it.

    The idea is not mine — an untracked proxywatch.py appeared at the repo root
    the same minutes this module was written, by a seat that never claimed it,
    carrying a check_proxy_status() that actually talked to the endpoint.
    Preserved at tag rescue/proxywatch-root. Config and transcript age never
    ask the proxy anything, and a TCP connect only proves a socket is open.

    NO CREDENTIAL IS SENT. An unauthenticated request separates every failure
    mode on its own, which is what makes the rung both safe and sharp.
    """

    def _probe_returning(self, exc=None, status=None, body=b""):
        import urllib.error

        class _R:
            def __init__(self):
                self.status = status
            def read(self, _n=None):
                return body
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        def fake(_req, timeout=None):
            if exc:
                raise exc
            return _R()
        return mock.patch("urllib.request.urlopen", side_effect=fake)

    def test_a_401_is_HEALTHY_because_the_auth_path_answered(self):
        """Refusing an invalid key is the proxy working. Measured live on
        8317/8360/8390: all three, 401, 27-byte body."""
        import io
        import urllib.error
        with self._probe_returning(exc=urllib.error.HTTPError(
                "u", 401, "no", {}, io.BytesIO(b""))):
            state, detail, _ms = proxywatch.probe(8317)
        self.assertEqual(state, "healthy")
        self.assertIn("refused an invalid key", detail)

    def test_a_200_WITH_AN_EMPTY_BODY_is_THE_BUG(self):
        """The known CLIProxyAPI fault: a bad key answered 200-with-nothing,
        which makes an auth failure indistinguishable from a dropped
        completion. That ambiguity is the exact shape the owner reported for a
        week, and it is why this case is named rather than lumped into 'ok'."""
        with self._probe_returning(status=200, body=b"{}"):
            state, detail, _ms = proxywatch.probe(8317)
        self.assertEqual(state, "EMPTY200")
        self.assertIn("dropped completion", detail)

    def test_a_200_with_a_REAL_body_is_not_the_bug(self):
        """The negative control. Only a SUSPICIOUSLY SMALL 200 is the fault;
        flagging every 200 would make the rung useless the day auth succeeds."""
        with self._probe_returning(status=200, body=b"x" * 400):
            state, _d, _ms = proxywatch.probe(8317)
        self.assertEqual(state, "ok")

    def test_oversize_on_probe_read_returns_OVERSIZE(self):
        # Probe path also bounds the read and returns explicit OVERSIZE.
        huge = b"x" * (proxywatch.CANARY_MAX_BODY + 10)
        with self._probe_returning(status=200, body=huge):
            state, detail, _ms = proxywatch.probe(8317)
        self.assertEqual(state, "OVERSIZE")
        self.assertIn("exceeded canary cap", detail)

    def test_a_refused_connection_is_DOWN(self):
        with self._probe_returning(exc=OSError("connection refused")):
            state, _d, _ms = proxywatch.probe(8317)
        self.assertEqual(state, "down")

    def test_the_probe_NEVER_sends_a_real_credential(self):
        """A health check that needed the key would be a new place for the key
        to leak, and would fail closed on every seat whose key it lacked."""
        import inspect
        src = inspect.getsource(proxywatch.probe)
        self.assertIn("deliberately-invalid", proxywatch._BAD_KEY)
        self.assertNotIn("_pi_api_key", src)
        self.assertNotIn("api-keys", src)

    def test_the_keyless_probe_LABELS_ITSELF_in_the_log(self):
        """helm's own reflection must be identifiable in proxy.log, because a
        reader who cannot tell the instrument from the traffic misreads the
        instrument as the fault.

        The gin logger records the QUERY STRING and no headers, so a query
        marker is the only "identifying header" the log can carry — the
        Authorization bearer is invisible to every reader of that file. This
        probe deliberately sends an invalid key to prove the proxy answers
        401; unlabelled, those rows are indistinguishable from real refusals
        on the same path, and a starvation scan drafted from the 4xx tail
        would call every healthy seat starved (codex carries ~344 of these
        beside ~241 real successes)."""
        import inspect
        src = inspect.getsource(proxywatch.probe)
        # CONTROL, unconditional and on the same observable: the AUTHENTICATED
        # canary has always carried the marker, so this pins a gap between two
        # probes rather than the mere presence of a constant.
        # the source references the NAME, never the literal value — asserting
        # the value here failed the control first, which is what a control is
        # for: it caught the assertion's shape before the claim was trusted.
        self.assertIn("_CANARY_QUERY",
                      inspect.getsource(proxywatch._canary_once))
        self.assertIn("_CANARY_QUERY", src)
        # and the owner observation both excludes marked rows from refusal state
        # and preserves them in explicit provenance accounting.
        self.assertIn("_is_helm_canary_path",
                      inspect.getsource(proxywatch._log_state))
        self.assertIn("_status_401_provenance",
                      inspect.getsource(proxywatch.log_observation))

    def test_EMPTY200_and_DOWN_reach_findings(self):
        f = proxywatch.findings(rep(dict(row(), probe="EMPTY200",
                                         probe_detail="answered 200")))
        self.assertEqual(f[0][0], "EMPTY200")
        f = proxywatch.findings(rep(dict(row(), probe="down",
                                         probe_detail="refused")))
        self.assertEqual(f[0][0], "DOWN")

    def test_a_healthy_probe_says_nothing(self):
        self.assertEqual(
            proxywatch.findings(rep(dict(row(), probe="healthy"))), [])


class ProxyRuntimeProofTest(unittest.TestCase):
    def test_agent_harness_schema_change_has_a_new_version_and_v2_replays(self):
        transitional = runtime_proof()
        runtime, err = proxywatch._proxy_proof_runtime(transitional)
        self.assertIsNone(err, err)
        self.assertEqual(runtime["agent_harness"], "claude")
        legacy = dict(transitional)
        legacy.pop("agent_harness")
        runtime, err = proxywatch._proxy_proof_runtime(legacy)
        self.assertIsNone(err, err)
        self.assertNotIn("agent_harness", runtime)
        current = dict(transitional, v=proxywatch._PROXY_RUNTIME_V)
        runtime, err = proxywatch._proxy_proof_runtime(current)
        self.assertIsNone(err, err)
        self.assertEqual(runtime["agent_harness"], "claude")
        malformed = dict(current)
        malformed.pop("agent_harness")
        runtime, err = proxywatch._proxy_proof_runtime(malformed)
        self.assertIsNone(runtime)
        self.assertIn("wrong schema or version", err)
        null_harness = dict(current, agent_harness=None)
        runtime, err = proxywatch._proxy_proof_runtime(null_harness)
        self.assertIsNone(runtime)
        self.assertIn("malformed process/session identity", err)
        self.assertGreater(proxywatch._PROXY_RUNTIME_V, transitional["v"])

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="helm-test-proxy-runtime-")
        keys = ("HELM_HOME", "HELM_CHAT_DIR", "HELM_CHAT_NAME")
        self.old_env = {key: os.environ.get(key) for key in keys}
        os.environ["HELM_HOME"] = os.path.join(self.d, "helm-home")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.d, "chat")
        os.environ["HELM_CHAT_NAME"] = "measured-seat"
        os.makedirs(os.environ["HELM_HOME"])
        os.makedirs(os.environ["HELM_CHAT_DIR"])
        self.state = os.path.join(self.d, "proxywatch.json")
        self.state_patch = mock.patch.object(proxywatch, "_state_path",
                                             return_value=self.state)
        self.state_patch.start()
        proxywatch._PROXY_AUTH_CANARIES.clear()

    def tearDown(self):
        proxywatch._PROXY_AUTH_CANARIES.clear()
        self.state_patch.stop()
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.d, ignore_errors=True)

    def write_state(self, proofs, ts=1000):
        with open(self.state, "w", encoding="utf-8") as f:
            json.dump({"ts": ts, "proxy_runtime": proofs}, f)

    @staticmethod
    def shape_for(proof):
        base = {key: value for key, value in proof.items()
                if key not in ("observed_at", "canary")}
        return {"url": proof["local_base_url"], "token": "live-secret",
                "model": proof["model"], "proof": base}

    @staticmethod
    def deferred_shape_for(proof, auth_routes):
        shape = ProxyRuntimeProofTest.shape_for(proof)
        shape["proof"]["v"] = proxywatch._PROXY_RUNTIME_V
        shape["proof"].pop("route")
        shape["auth_routes"] = auth_routes
        return shape

    def assert_deferred_reproof_refuses(self, proof, shape):
        proxywatch._PROXY_AUTH_CANARIES.clear()
        self.write_state({"proof-key": proof})
        with mock.patch.object(proxywatch, "_roster_identity_for_session",
                               return_value=("measured-seat", None)), \
                mock.patch.object(proxywatch, "_proxy_runtime_shape",
                                  return_value=(shape, None)), \
                mock.patch.object(proxywatch, "proxy_runtime_canary") as canary:
            family, got, err = proxywatch.proxy_runtime_snapshot(
                proof["session"], now=1000)
        self.assertIsNone(family)
        self.assertIsNone(got)
        self.assertIn("no longer matches the cached proof", err)
        canary.assert_not_called()

    @staticmethod
    def _key_index(base_url, api_key):
        seed = "openai-compatibility:%s+%s" % (base_url, api_key)
        return hashlib.sha256(seed.encode("utf-8")).digest()[:8].hex()

    @staticmethod
    def _trace(index):
        return "20260820000000-%s-deadbeef" % index

    def _write_key_routes(self, routes, token="inbound-secret"):
        config = os.path.join(self.d, "multi-route.yaml")
        lines = ['host: "127.0.0.1"', "port: 8360", "api-keys:",
                 '  - "%s"' % token, "openai-compatibility:"]
        for route, api_key in routes:
            lines.extend(('  - name: "%s"' % route["provider"],
                          '    base-url: "%s"' % route["base_url"],
                          "    api-key-entries:",
                          '      - api-key: "%s"' % api_key,
                          "    models:",
                          '      - name: "%s"' % route["upstream_model"],
                          '        alias: "%s"' % route["alias"]))
        with open(config, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return config

    def test_record_promotes_one_measured_proof_to_the_exact_current_session(self):
        proof = runtime_proof()
        family, err = proxywatch._proxy_proof_family(proof)
        self.assertIsNone(err, err)
        seats.write_roster("measured-seat", session=proof["session"])
        self.assertTrue(proxywatch.record(
            rep(row("measured-seat"), proxy_runtime={"seat-a": proof})))
        entry = seats.runtime_entry_for_session(
            seats.roster()["measured-seat"], proof["session"])
        self.assertEqual(entry["source"], "proxywatch")
        self.assertEqual(entry["proxy_proof"], proof)
        # DERIVED, NOT TRANSCRIBED: this asserts the STORED record equals what
        # the decoder produces, so a hand-written copy stops being that the
        # moment the decoder learns a field — and then reads as a broken
        # promotion rather than a stale expectation.
        measured, derr = proxywatch._proxy_proof_runtime(proof)
        self.assertIsNone(derr, derr)
        self.assertEqual(measured.get("family"), family,
                         "control: the derivation and the family resolver "
                         "agree, so `measured` is not junk")
        self.assertEqual(entry["runtime"], measured)
        self.assertTrue(entry["verified"])

    def test_proxy_restamp_ignores_a_stale_native_row_summary(self):  # noqa: VACUOUS_ASSERTION — first is asserted non-None with the exact proof before second-restamp equality
        proof = runtime_proof()
        measured, err = proxywatch._proxy_proof_runtime(proof)
        self.assertIsNone(err, err)
        seats.write_roster("measured-seat", session=proof["session"])
        first, err = seats.stamp_proxy_runtime(
            proof["session"], measured, proof)
        self.assertIsNone(err, err)
        self.assertIsNotNone(first)
        self.assertEqual(first["proxy_proof"], proof)
        rows = seats.roster()
        rows["measured-seat"]["runtime"] = {
            "agent_harness": "claude", "family": "claude",
            "backend": "native"}
        rows["measured-seat"]["runtime_verified"] = True
        from helm import pk
        pk.write_json(seats.roster_path(), rows)
        restamped, err = seats.stamp_proxy_runtime(
            proof["session"], measured, proof)
        self.assertIsNone(err, err)
        self.assertEqual(restamped, first)

    def test_the_authority_minter_refuses_a_minimal_handwritten_proof(self):
        proof = {"session": "session-ds4pro"}
        runtime = {"agent_harness": "claude", "family": "measured-family",
                   "backend": "proxy"}
        seats.write_roster("measured-seat", session=proof["session"])
        entry, err = seats.stamp_proxy_runtime(
            proof["session"], runtime, proof)
        self.assertIsNone(entry)
        self.assertIn("wrong schema or version", err)

    def test_the_authority_minter_refuses_runtime_family_disagreement(self):  # noqa: VACUOUS_ASSERTION — the successful stamp from the same decoded proof immediately before the mismatch is the positive control
        proof = runtime_proof()
        measured, err = proxywatch._proxy_proof_runtime(proof)
        self.assertIsNone(err, err)
        claimed = dict(measured, family="costume")
        seats.write_roster("measured-seat", session=proof["session"])
        control, err = seats.stamp_proxy_runtime(
            proof["session"], measured, proof)
        self.assertIsNone(err, err)
        self.assertEqual(control["runtime"], measured)
        entry, err = seats.stamp_proxy_runtime(
            proof["session"], claimed, proof)
        self.assertIsNone(entry)
        self.assertIn("does not match its proof", err)

    def test_the_authority_minter_refuses_runtime_harness_disagreement(self):  # noqa: VACUOUS_ASSERTION — the successful stamp from the same decoded proof immediately before the mismatch is the positive control
        proof = runtime_proof()
        measured, err = proxywatch._proxy_proof_runtime(proof)
        self.assertIsNone(err, err)
        claimed = dict(measured, agent_harness="pi")
        seats.write_roster("measured-seat", session=proof["session"])
        control, err = seats.stamp_proxy_runtime(
            proof["session"], measured, proof)
        self.assertIsNone(err, err)
        self.assertEqual(control["runtime"], measured)
        entry, err = seats.stamp_proxy_runtime(
            proof["session"], claimed, proof)
        self.assertIsNone(entry)
        self.assertIn("does not match its proof", err)

    def test_promotion_passes_the_complete_proof_derived_runtime(self):  # noqa: VACUOUS_ASSERTION — the asserted stamp call is the unconditional positive control on this exact derived-runtime handoff
        proof = {"session": "measured-session"}
        measured = {"agent_harness": "pi", "family": "measured-family",
                    "backend": "proxy"}
        with mock.patch.object(proxywatch, "_proxy_proof_runtime",
                               return_value=(measured, None)), \
                mock.patch.object(seats, "stamp_proxy_runtime",
                                  return_value=({}, None)) as stamp:
            self.assertEqual(proxywatch._stamp_proxy_runtime_proofs(
                {"storage-label": proof}), [])
        stamp.assert_called_once_with("measured-session", measured, proof)

    def test_later_launch_labels_and_presence_beats_preserve_measurement(self):
        proof = runtime_proof()
        family, err = proxywatch._proxy_proof_family(proof)
        self.assertIsNone(err, err)
        seats.write_roster("measured-seat", session=proof["session"])
        self.assertTrue(proxywatch.record(
            rep(row("measured-seat"), proxy_runtime={"seat-a": proof})))
        costume = {"agent_harness": "claude", "family": "costume",
                   "backend": "proxy"}
        seats.write_roster("measured-seat", session=proof["session"],
                           runtime=costume)
        seats.write_roster("measured-seat", session=proof["session"])
        rebound, err = seats.bind_lifecycle_runtime(
            "measured-seat", proof["session"], costume)
        self.assertIsNone(err, err)
        self.assertEqual(rebound["source"], "proxywatch")
        entry = seats.runtime_entry_for_session(
            seats.roster()["measured-seat"], proof["session"])
        self.assertEqual(entry["source"], "proxywatch")
        self.assertEqual(entry["runtime"]["family"], family)
        self.assertEqual(entry["proxy_proof"], proof)

    def test_record_measures_family_from_route_not_the_storage_key(self):
        route = {"alias": "codex-wire", "provider": "openai",
                 "upstream_model": "gpt-live",
                 "base_url": "https://api.openai.test/v1"}
        proof = runtime_proof(route=route)
        configured = {
            "codex": {"mode": "proxy-key", "model": "codex-wire",
                      "provider": "openai", "upstream_model": "gpt-live",
                      "base_url": "https://api.openai.test/v1"}}
        seats.write_roster("measured-seat", session=proof["session"])
        with mock.patch.dict(seat.FAMILIES, configured, clear=True):
            self.assertTrue(proxywatch.record(rep(
                row("measured-seat"),
                proxy_runtime={"costume-key": proof})))
        entry = seats.runtime_entry_for_session(
            seats.roster()["measured-seat"], proof["session"])
        self.assertEqual(entry["runtime"]["family"], "codex")

    def test_record_withholds_duplicate_proofs_for_one_session(self):  # noqa: VACUOUS_ASSERTION — record success with two sanitized same-session proofs is the positive duplicate control before stamp absence
        proof = runtime_proof()
        seats.write_roster("measured-seat", session=proof["session"])
        self.assertTrue(proxywatch.record(rep(
            row("measured-seat"),
            proxy_runtime={"first": proof, "second": dict(proof)})))
        entry = seats.runtime_entry_for_session(
            seats.roster()["measured-seat"], proof["session"])
        self.assertIsNone(entry)

    def test_record_does_not_replace_verified_native_runtime(self):
        proof = runtime_proof()
        family, err = proxywatch._proxy_proof_family(proof)
        self.assertIsNone(err, err)
        native = {"agent_harness": "claude", "family": family,
                  "backend": "native"}
        seats.write_roster("measured-seat", session=proof["session"],
                           runtime=native)
        self.assertTrue(proxywatch.record(
            rep(row("measured-seat"), proxy_runtime={"seat-a": proof})))
        entry = seats.runtime_entry_for_session(
            seats.roster()["measured-seat"], proof["session"])
        self.assertEqual(entry, {"runtime": native, "verified": True})

    def test_snapshot_refuses_a_roster_stamp_whose_authority_fields_differ(self):  # noqa: VACUOUS_ASSERTION — the for is over a two-element tuple literal, so both refusal arms run unconditionally; test_a_roster_stamp_from_the_adjacent_pass_still_authorizes is the paired positive control through the same comparison
        # Previously named "…from another pass", which its own fixture never
        # tested (it forged proxy_pid, an authority field) and which is no
        # longer true: an adjacent-pass stamp differing ONLY in observed_at
        # now authorizes (the arm below). Every AUTHORITY field still binds —
        # a forged pid and a forged config digest both refuse.
        proof = runtime_proof()
        self.write_state({"seat-a": proof})
        for forged in (dict(proof, proxy_pid=9999),
                       dict(proof, config_sha256="b" * 64)):
            family, got, err = proxywatch.proxy_runtime_snapshot(
                proof["session"], now=1000, expected_proof=forged)
            self.assertIsNone(family)
            self.assertIsNone(got)
            self.assertIn("does not match cached proof", err)

    def test_a_roster_stamp_from_the_adjacent_pass_still_authorizes(self):
        """THE task/1067 WINDOW, closed. The watcher writes the state file
        first and re-stamps the roster after, so between those instants the
        two copies of one TRUTHFUL proof differ only in observed_at — and
        byte-equality read that clock skew as contradiction, resolving every
        proxy-backed seat's tier UNKNOWN for a window every pass, so one
        skewed read could demote a seat's every gated approve for a whole
        projection. Equality is over the authority fields; the clock is not
        one of them."""
        proof = runtime_proof()
        stamped = dict(proof, observed_at=100)     # the pass before
        self.write_state({"seat-a": proof})
        derived, err = proxywatch._proxy_proof_family(proof)
        self.assertIsNone(err, err)
        with mock.patch.object(proxywatch, "_roster_identity_for_session",
                               return_value=("measured-seat", None)), \
                mock.patch.object(proxywatch, "_proxy_runtime_shape",
                                  return_value=(self.shape_for(proof), None)), \
                mock.patch.object(proxywatch, "proxy_runtime_canary",
                                  return_value=(proof, None)):
            family, got, err = proxywatch.proxy_runtime_snapshot(
                proof["session"], now=1000, expected_proof=stamped)
        self.assertIsNone(err, err)
        self.assertEqual(family, derived)
        self.assertEqual(got["observed_at"], 1000,
                         "the STATE copy, bound to its own pass, is the one "
                         "that rides out")

    def test_a_malformed_proof_refuses_only_the_session_it_claims(self):
        """The state holds every proxy seat's proof, and one seat's record can
        be malformed for a pass on its own account (a route mapping to no
        family while that seat's config is rewritten, a foreign process
        seeding a stale record). A malformed record refuses only the session
        it claims and names itself; one claiming another session authorizes
        nothing and forbids nothing here, so a verdict never waits on a
        bystander's transient (task/2693).
        """
        proof = runtime_proof()
        foreign = dict(runtime_proof(session="session-foreign"),
                       route={"alias": "nobody", "provider": "nowhere",
                              "upstream_model": "no-model"})
        _family, why = proxywatch._proxy_proof_family(foreign)
        self.assertIsNotNone(why, "the foreign record must be malformed, "
                                  "or this arm tests nothing")
        derived, err = proxywatch._proxy_proof_family(proof)
        self.assertIsNone(err, err)

        def snapshot():
            with mock.patch.object(proxywatch, "_roster_identity_for_session",
                                   return_value=("measured-seat", None)), \
                    mock.patch.object(proxywatch, "_proxy_runtime_shape",
                                      return_value=(self.shape_for(proof), None)), \
                    mock.patch.object(proxywatch, "proxy_runtime_canary",
                                      return_value=(proof, None)):
                return proxywatch.proxy_runtime_snapshot(
                    proof["session"], now=1000)

        # unconditional positive control: the session's own proof authorizes
        self.write_state({"seat-a": proof})
        family, _got, err = snapshot()
        self.assertIsNone(err, err)
        self.assertEqual(family, derived)
        # the arm: a malformed record claiming ANOTHER session changes nothing
        self.write_state({"seat-a": proof, "seat-b": foreign})
        family, _got, err = snapshot()
        self.assertIsNone(err, "another seat's malformed proof refused this "
                               "session: %s" % err)
        self.assertEqual(family, derived)
        # mutation: the same malformed record claiming THIS session refuses,
        # and names itself, not a bystander
        own_bad = dict(foreign, session=proof["session"])
        self.write_state({"seat-a": proof, "seat-b": own_bad})
        family, got, err = snapshot()
        self.assertIsNone(family)
        self.assertIsNone(got)
        self.assertIn("malformed under seat-b", err)

    def test_live_session_record_binds_a_proxy_process_without_an_env_session(self):
        sessions_dir = os.path.join(self.d, "sessions")
        os.makedirs(sessions_dir)
        with open(os.path.join(sessions_dir, "live.json"), "w", encoding="utf-8") as f:
            json.dump({"sessionId": "session-ds4pro", "pid": 4101,
                       "procStart": 701}, f)
        env = {"ANTHROPIC_MODEL": "ds4-pro",
               "ANTHROPIC_BASE_URL": "http://127.0.0.1:8360",
               "ANTHROPIC_AUTH_TOKEN": "secret"}
        with mock.patch("helm.sessions.cred_homes", return_value=[self.d]), \
                mock.patch("helm.sessions._pid_is_claude", return_value=True), \
                mock.patch("helm.beacons.proc_env", return_value=env), \
                mock.patch("helm.beacons.proc_starttime", return_value=701):
            runtime, err = proxywatch._live_session_runtime("session-ds4pro")
        self.assertIsNone(err, err)
        self.assertEqual(runtime["agent_harness"], "claude")
        self.assertEqual(runtime["pid"], 4101)
        self.assertEqual(runtime["starttime"], 701)
        self.assertEqual(runtime["model"], "ds4-pro")

    def test_live_session_record_refuses_a_contradictory_env_session(self):
        sessions_dir = os.path.join(self.d, "sessions")
        os.makedirs(sessions_dir)
        with open(os.path.join(sessions_dir, "live.json"), "w", encoding="utf-8") as f:
            json.dump({"sessionId": "session-ds4pro", "pid": 4101,
                       "procStart": 701}, f)
        env = {"CLAUDE_CODE_SESSION_ID": "another-session",
               "ANTHROPIC_MODEL": "ds4-pro",
               "ANTHROPIC_BASE_URL": "http://127.0.0.1:8360",
               "ANTHROPIC_AUTH_TOKEN": "secret"}
        with mock.patch("helm.sessions.cred_homes", return_value=[self.d]), \
                mock.patch("helm.sessions._pid_is_claude", return_value=True), \
                mock.patch("helm.beacons.proc_env", return_value=env), \
                mock.patch("helm.beacons.proc_starttime", return_value=701):
            runtime, err = proxywatch._live_session_runtime("session-ds4pro")
        self.assertIsNone(runtime)
        self.assertIn("contradicts the roster session", err)

    def test_ds4pro_authority_is_the_exact_live_session_listener_config_and_route(self):  # noqa: VACUOUS_ASSERTION — exact positive proof fields precede secret-absence checks
        config = os.path.join(self.d, "config.yaml")
        binary = os.path.join(self.d, "cli-proxy-api")
        secret = "session-bound-secret"
        with open(config, "w", encoding="utf-8") as f:
            f.write(seat._config_yaml_key(
                8360, secret, "opencode-go",
                "https://opencode.ai/zen/go/v1", "ds4-pro", "upstream-secret",
                "deepseek-v4-pro"))
        with open(binary, "wb") as f:
            f.write(b"proxy")
        launch = seat._proxy_launch_inputs(config, binary)
        with open(os.path.join(self.d, "proxy.pid"), "w", encoding="utf-8") as f:
            f.write("4201 proc:702 %s\n" % seat._encode_launch_inputs(launch))
        runtime = {"agent_harness": "claude", "pid": 4101,
                   "starttime": 701, "model": "ds4-pro",
                   "base_url": "http://127.0.0.1:8360", "token": secret}
        listener = {"pid": 4201, "identity": "proc:702",
                    "argv": [binary, "-config", config], "config": config}
        patches = (mock.patch.object(proxywatch, "_roster_session",
                                     return_value=("session-ds4pro", "ds4pro", None)),
                   mock.patch.object(proxywatch, "_live_session_runtime",
                                     return_value=(runtime, None)),
                   mock.patch("helm.seat._port_listeners",
                              return_value=[listener]))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        shape, err = proxywatch._proxy_runtime_shape("cosmetic-label")
        self.assertIsNone(err, err)
        self.assertEqual(shape["proof"]["session"], "session-ds4pro")
        self.assertEqual(shape["proof"]["agent_harness"], "claude")
        self.assertEqual(shape["proof"]["agent_pid"], 4101)
        self.assertEqual(shape["proof"]["proxy_pid"], 4201)
        self.assertEqual(shape["proof"]["config_sha256"],
                         seat._config_digest(config))
        self.assertEqual(shape["proof"]["route"], {
            "alias": "ds4-pro", "provider": "opencode-go",
            "upstream_model": "deepseek-v4-pro",
            "base_url": "https://opencode.ai/zen/go/v1"})
        self.assertNotIn("family", shape["proof"])
        index = self._key_index(
            "https://opencode.ai/zen/go/v1", "upstream-secret")
        with mock.patch.object(
                proxywatch, "_canary_once",
                return_value=("HEALTHY", "HTTP 200 with OK", 4, 200,
                              "deepseek-v4-pro", self._trace(index))):
            proof, err = proxywatch.proxy_runtime_canary(
                "cosmetic-label", observed_at=1000)
        self.assertIsNone(err, err)
        serialized = json.dumps(proof, sort_keys=True)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("upstream-secret", serialized)
        self.assertEqual(proof["canary"], {"state": "HEALTHY", "status": 200})

    def test_oauth_authority_is_live_model_plus_loaded_credential_provider(self):  # noqa: VACUOUS_ASSERTION — every matrix arm proves the full positive route before secret-absence checks
        cases = (
            ("codex", "gpt-5.6-sol", "gpt-5.6-sol", "codex", 8317, 4301),
            ("gemini", "gemini-3.6-flash-high", "gemini-3.6-flash",
             "antigravity", 8390, 4302),
        )
        for family, model, response_model, provider, port, proxy_pid in cases:
            with self.subTest(family=family):
                root = os.path.join(self.d, family)
                auth = os.path.join(root, "auth")
                os.makedirs(auth)
                secret = family + "-session-secret"
                config = os.path.join(root, "config.yaml")
                binary = os.path.join(root, "cli-proxy-api")
                with open(config, "w", encoding="utf-8") as f:
                    f.write(seat._config_yaml(port, auth, secret))
                auth_path = os.path.join(auth, provider + "-one.json")
                with open(auth_path, "w", encoding="utf-8") as f:
                    json.dump({"type": provider, "access_token": "oauth-secret",
                               "email": "private@example.test"}, f)
                auth_index = hashlib.sha256(
                    (provider + ":" + auth_path).encode("utf-8")).digest()[:8].hex()
                trace = "20260805050000-%s-deadbeef" % auth_index
                with open(binary, "wb") as f:
                    f.write(b"proxy")
                launch = seat._proxy_launch_inputs(config, binary)
                with open(os.path.join(root, "proxy.pid"), "w",
                          encoding="utf-8") as f:
                    f.write("%d proc:%d %s\n" %
                            (proxy_pid, proxy_pid + 1,
                             seat._encode_launch_inputs(launch)))
                runtime = {"agent_harness": "claude", "pid": 4101,
                           "starttime": 701, "model": model,
                           "base_url": "http://127.0.0.1:%d" % port,
                           "token": secret}
                listener = {"pid": proxy_pid, "identity": "proc:%d" % (proxy_pid + 1),
                            "argv": [binary, "-config", config], "config": config}
                with mock.patch.object(
                        proxywatch, "_roster_session",
                        return_value=("session-" + family, "costume", None)), \
                        mock.patch.object(proxywatch, "_live_session_runtime",
                                          return_value=(runtime, None)), \
                        mock.patch("helm.seat._port_listeners",
                                   return_value=[listener]), \
                        mock.patch.object(
                            proxywatch, "_canary_once",
                            return_value=("HEALTHY", "HTTP 200 with OK", 4, 200,
                                          response_model, trace)):
                    proof, err = proxywatch.proxy_runtime_canary(
                        "costume", observed_at=1000)
                self.assertIsNone(err, err)
                self.assertEqual(proxywatch._proxy_proof_family(proof),
                                 (family, None))
                self.assertEqual(proof["route"], {
                    "alias": model, "provider": provider,
                    "upstream_model": model})
                serialized = json.dumps(proof, sort_keys=True)
                self.assertNotIn(secret, serialized)
                self.assertNotIn("oauth-secret", serialized)
                self.assertNotIn("private@example.test", serialized)
                self.assertNotIn("family", proof)

    def test_oauth_canary_refuses_an_undeclared_response_projection(self):
        route = {"alias": "gemini-3.6-flash-high", "provider": "antigravity",
                 "upstream_model": "gemini-3.6-flash-high"}
        shape = {"url": "http://127.0.0.1:8390", "token": "secret",
                 "model": "gemini-3.6-flash-high",
                 "auth_routes": {"a" * 16: (route,)},
                 "proof": {"v": 1, "route": route}}
        trace = "20260805050000-%s-deadbeef" % ("a" * 16)
        with mock.patch.object(proxywatch, "_proxy_runtime_shape",
                               return_value=(shape, None)), \
                mock.patch.object(
                    proxywatch, "_canary_once",
                    return_value=("HEALTHY", "HTTP 200", 4, 200,
                                  "gemini-3.6-flash-low", trace)):
            proof, err = proxywatch.proxy_runtime_canary("costume")
        self.assertIsNone(proof)
        self.assertIn("got 'gemini-3.6-flash-low'", err)
        self.assertIn("expected 'gemini-3.6-flash'", err)

    def test_oauth_canary_requires_selected_auth_trace_and_response_model(self):  # noqa: VACUOUS_ASSERTION — positive OAuth matrix above proves these checks admit the exact live shape
        route = {"alias": "gpt-5.6-sol", "provider": "codex",
                 "upstream_model": "gpt-5.6-sol"}
        shape = {"url": "http://127.0.0.1:8317", "token": "secret",
                 "model": "gpt-5.6-sol",
                 "auth_routes": {"a" * 16: (route,)},
                 "proof": {"v": 1, "route": route}}
        cases = (
            ("different-model", "20260805050000-%s-deadbeef" % ("a" * 16),
             "response model"),
            ("gpt-5.6-sol", "20260805050000-%s-deadbeef" % ("b" * 16),
             "trace does not name"),
            ("gpt-5.6-sol", "opaque-trace", "trace does not name"),
        )
        for response_model, trace, message in cases:
            with self.subTest(response_model=response_model, trace=trace), \
                    mock.patch.object(proxywatch, "_proxy_runtime_shape",
                                      return_value=(shape, None)), \
                    mock.patch.object(
                        proxywatch, "_canary_once",
                        return_value=("HEALTHY", "HTTP 200", 4, 200,
                                      response_model, trace)):
                proof, err = proxywatch.proxy_runtime_canary("costume")
            self.assertIsNone(proof)
            self.assertIn(message, err)

    def test_oauth_route_refuses_mixed_provider_records_and_model_mismatch(self):  # noqa: VACUOUS_ASSERTION — valid homogeneous controls prove both refusal gates are reachable
        auth = os.path.join(self.d, "auth")
        os.makedirs(auth)
        config = os.path.join(self.d, "config.yaml")
        with open(config, "w", encoding="utf-8") as f:
            f.write(seat._config_yaml(8317, auth, "inbound-secret"))
        for provider in ("codex", "antigravity"):
            with open(os.path.join(auth, provider + ".json"), "w",
                      encoding="utf-8") as f:
                json.dump({"type": provider, "access_token": "secret"}, f)
        route, token, auth_indexes, err = proxywatch._proxy_config_route(config, "gpt-5.6-sol")
        self.assertIsNone(route)
        self.assertIsNone(token)
        self.assertIn("2 provider types", err)
        os.unlink(os.path.join(auth, "antigravity.json"))
        route, token, auth_indexes, err = proxywatch._proxy_config_route(config, "gpt-5.6-sol")
        self.assertIsNone(err, err)
        self.assertEqual(token, "inbound-secret")
        self.assertEqual(len(auth_indexes), 1)
        self.assertRegex(next(iter(auth_indexes)), r"^[0-9a-f]{16}$")
        self.assertEqual(proxywatch._proxy_route_family(route), ("codex", None))
        route, token, auth_indexes, err = proxywatch._proxy_config_route(
            config, "gemini-3.6-flash-high")
        self.assertIsNone(route)
        self.assertIsNone(token)
        self.assertIn("unknown or ambiguous", err)

    def test_oauth_route_accepts_per_tier_names_and_refuses_a_foreign_one(self):  # noqa: VACUOUS_ASSERTION — the generated tiered block is the positive control before each refusal
        """A family with a subagent_tiers table emits a block whose rows name
        TWO models (an astra codex seat: astra on opus/fable from the seat's
        own launch model, sol on sonnet/haiku from the table), and both
        are ids one codex OAuth serves on the codex channel — so the route
        proof must accept it, or `seat doctor --ensure` would hand the watchdog
        a config it refuses to attest. The name it must still refuse is a model
        belonging to ANOTHER family: the name is what the upstream serves, so a
        foreign name lets a response come back stamped with another family's
        model, and family proof reads served models."""
        auth = os.path.join(self.d, "auth")
        os.makedirs(auth)
        with open(os.path.join(auth, "codex.json"), "w", encoding="utf-8") as f:
            json.dump({"type": "codex", "access_token": "secret"}, f)
        config = os.path.join(self.d, "config.yaml")

        def err(text, alias="gpt-6-astra"):
            with open(config, "w", encoding="utf-8") as f:
                f.write(text)
            route, token, _indexes, why = proxywatch._proxy_config_route(config, alias)
            if why is None:                       # the proof resolved, intact
                self.assertEqual(proxywatch._proxy_route_family(route),
                                 ("codex", None))
                self.assertEqual(token, "inbound-secret")
            return why

        # THE POSITIVE POLE, through the production door: the generator's own
        # tiered block for the family that declares the table.
        tiered = seat._config_yaml(8317, auth, "inbound-secret",
                                   channel="codex", model="gpt-6-astra",
                                   family="codex")
        self.assertIn('    - name: "gpt-5.6-sol"', tiered)   # two models present
        self.assertIn('    - name: "gpt-6-astra"', tiered)
        self.assertIsNone(err(tiered))
        # and the same config with NO alias block at all, so the arm's accept
        # is not just "this reader ignores the block"
        bare = seat._config_yaml(8317, auth, "inbound-secret")
        self.assertNotIn("oauth-model-alias", bare)
        self.assertIsNone(err(bare))

        def block(row):
            return bare + "oauth-model-alias:\n  codex:\n" + row

        # A FOREIGN FAMILY'S MODEL AS THE NAME: refused, naming what this
        # channel does serve. kimi-k3 is kimi's launch model, catalogued.
        why = err(block('    - name: "kimi-k3"\n      alias: "claude-opus-5"\n'
                        "      fork: true\n"))
        self.assertIn("another family's catalogued model", why)
        self.assertIn("gpt-6-astra", why)
        self.assertIn("kimi-k3", seat.family_catalogued_models(seat.FAMILIES["kimi"]))
        # SAME ROW, OWN-FAMILY NAME: accepted — so the refusal above is about
        # WHOSE model the name is, not about a non-default name.
        self.assertIsNone(err(block('    - name: "gpt-5.6-sol"\n'
                                    '      alias: "claude-opus-5"\n'
                                    "      fork: true\n")))
        # A NAME NO FAMILY CATALOGUES IS UNKNOWN, NOT DRIFT: the watchdog must
        # not redden a model the owner points at before helm catalogues it.
        self.assertIsNone(err(block('    - name: "gpt-7-not-yet-catalogued"\n'
                                    '      alias: "claude-opus-5"\n'
                                    "      fork: true\n")))
        # and the rename pole is untouched by the new clause: a tier name with
        # no fork still stops the family's own id routing.
        why = err(block('    - name: "gpt-5.6-sol"\n      alias: "claude-opus-5"\n'))
        self.assertIn("no fork", why)

        # THE WHOLE BLOCK A DECLARED SOL SEAT EMITS, through the production
        # door: `instance_models` puts a declared instance on gpt-5.6-sol, so
        # every row of
        # ITS config names sol while its sibling's names astra, and the proof
        # must accept both -- a refusal here would leave a declared seat
        # unattestable and `seat doctor --ensure` writing a config the
        # watchdog reds. The two blocks differ (the assertion below), so this
        # is not the astra block passing twice.
        from helm import seat_catalog as _catalog
        declared = _catalog.FAMILIES["codex"]["instance_models"]
        sol_model = _catalog.instance_launch_model(_catalog.FAMILIES["codex"],
                                                   sorted(declared)[0])
        sol_block = seat._config_yaml(8317, auth, "inbound-secret",
                                      channel="codex", model=sol_model,
                                      family="codex")
        self.assertNotIn("gpt-6-astra", sol_block)
        self.assertNotEqual(sol_block, tiered)
        self.assertIsNone(err(sol_block, alias=sol_model))

    def test_oauth_route_refuses_global_and_per_auth_alias_surfaces(self):  # noqa: VACUOUS_ASSERTION — generated config and homogeneous provider controls pass before each alias refusal
        auth = os.path.join(self.d, "auth")
        os.makedirs(auth)
        config = os.path.join(self.d, "config.yaml")
        with open(os.path.join(auth, "codex.json"), "w", encoding="utf-8") as f:
            json.dump({"type": "codex", "access_token": "secret"}, f)
        generated = seat._config_yaml(8317, auth, "inbound-secret") + \
            "routing:\n  strategy: round-robin\n"
        with open(config, "w", encoding="utf-8") as f:
            f.write(generated)
        route, token, auth_indexes, err = proxywatch._proxy_config_route(config, "gpt-5.6-sol")
        self.assertIsNone(err, err)
        self.assertEqual(proxywatch._proxy_route_family(route), ("codex", None))
        self.assertEqual(token, "inbound-secret")
        self.assertEqual(len(auth_indexes), 1)
        # THE GENERATED ALIAS BLOCK IS ACCEPTED, through the production door
        # (task/1948): every row forks and every alias is a foreign id, so the
        # family's route is not renamed and the proof still resolves.
        aliased = seat._config_yaml(8317, auth, "inbound-secret",
                                    channel="codex", model="gpt-5.6-sol")
        self.assertIn("oauth-model-alias:", aliased)          # the block is there
        with open(config, "w", encoding="utf-8") as f:
            f.write(aliased)
        route, token, auth_indexes, err = proxywatch._proxy_config_route(config, "gpt-5.6-sol")
        self.assertIsNone(err, err)
        self.assertEqual(proxywatch._proxy_route_family(route), ("codex", None))
        self.assertEqual(token, "inbound-secret")
        # a RENAME (no fork) and an alias onto a catalogued route id are refused,
        # each with its row spelled out; any other unknown top-level key still is
        other = next(m for m in seat.FAMILIES["codex"]["probe_models"]
                     if m != "gpt-5.6-sol")
        for label, block, text in (
                ("rename", 'oauth-model-alias:\n  codex:\n    - name: "gpt-5.6-sol"\n'
                           '      alias: "claude-opus-5"\n', "no fork"),
                ("catalogued alias", 'oauth-model-alias:\n  codex:\n    - name: "gpt-5.6-sol"\n'
                                     '      alias: "%s"\n      fork: true\n' % other,
                 "catalogued route id"),
                ("force-mapping", 'oauth-model-alias:\n  codex:\n    - name: "gpt-5.6-sol"\n'
                                  '      alias: "claude-opus-5"\n      fork: true\n'
                                  '      force-mapping: true\n',
                 "force-mapping"),
                ("repeated alias", 'oauth-model-alias:\n  codex:\n    - name: "gpt-5.6-sol"\n'
                                   '      alias: "%s"\n      alias: "claude-opus-5"\n'
                                   '      fork: true\n' % other,
                 "alias (repeated)"),
                ("repeated fork", 'oauth-model-alias:\n  codex:\n    - name: "gpt-5.6-sol"\n'
                                  '      alias: "claude-opus-5"\n      fork: false\n'
                                  '      fork: true\n',
                 "fork (repeated)"),
                ("unknown key", "model-aliases:\n  codex: []\n",
                 "unsupported top-level routing fields")):
            with self.subTest(block=label):
                with open(config, "w", encoding="utf-8") as f:
                    f.write(generated + block)
                route, token, auth_indexes, err = proxywatch._proxy_config_route(config, "gpt-5.6-sol")
                self.assertIsNone(route)
                self.assertIsNone(token)
                self.assertIn(text, err)
        with open(config, "w", encoding="utf-8") as f:
            f.write(generated)
        with open(os.path.join(auth, "codex.json"), "w", encoding="utf-8") as f:
            json.dump({"type": "codex", "access_token": "secret",
                       "model-aliases": [{"name": "foreign", "alias": "gpt-5.6-sol"}]}, f)
        route, token, auth_indexes, err = proxywatch._proxy_config_route(config, "gpt-5.6-sol")
        self.assertIsNone(route)
        self.assertIsNone(token)
        self.assertIn("per-auth model alias", err)

    def test_multi_route_alias_proves_the_canary_selected_credential(self):  # noqa: VACUOUS_ASSERTION — the exact two-entry auth_routes assertion proves the selection loop has two live arms
        routes = (
            ({"alias": "ds4-pro", "provider": "deepseek-direct",
              "upstream_model": "deepseek-v4-pro",
              "base_url": "https://api.deepseek.com/v1"}, "direct-key"),
            ({"alias": "ds4-pro", "provider": "opencode-go",
              "upstream_model": "deepseek-v4-pro",
              "base_url": "https://opencode.ai/zen/go/v1"}, "go-key"),
        )
        config = self._write_key_routes(routes)
        route, token, auth_routes, err = proxywatch._proxy_config_route(
            config, "ds4-pro")
        self.assertIsNone(err, err)
        self.assertIsNone(route)
        self.assertEqual(token, "inbound-secret")
        self.assertEqual(len(auth_routes), 2)
        serialized_routes = json.dumps(auth_routes, sort_keys=True)
        self.assertNotIn("direct-key", serialized_routes)
        self.assertNotIn("go-key", serialized_routes)
        for selected, api_key in routes:
            with self.subTest(provider=selected["provider"]):
                index = self._key_index(selected["base_url"], api_key)
                self.assertEqual(auth_routes[index], (selected,))
                base = {key: value for key, value in
                        runtime_proof(route=selected).items()
                        if key not in ("route", "observed_at", "canary")}
                shape = {"url": base["local_base_url"], "token": token,
                         "model": "ds4-pro", "auth_routes": auth_routes,
                         "proof": base}
                with mock.patch.object(proxywatch, "_proxy_runtime_shape",
                                       return_value=(shape, None)), \
                        mock.patch.object(
                            proxywatch, "_canary_once",
                            return_value=("HEALTHY", "HTTP 200 with OK", 4, 200,
                                          selected["upstream_model"],
                                          self._trace(index))):
                    proof, err = proxywatch.proxy_runtime_canary(
                        "costume", observed_at=1000)
                self.assertIsNone(err, err)
                self.assertEqual(proof["route"], selected)
                self.assertEqual(proxywatch._proxy_proof_family(proof),
                                 ("ds4pro", None))  # noqa: SEAT_NAME — configured family identity is the property under test

    def test_canary_accepts_OK_with_whitespace_around_it(self):
        """deepseek-v4-flash answered " OK" and the exact match read it as
        MALFORMED200, darkening a healthy family (2664 class)."""
        for text in (" OK", "OK\n", "  OK  "):
            payload = json.dumps({"type": "message", "role": "assistant",
                                  "content": [{"type": "text", "text": text}],
                                  "stop_reason": "end_turn"}).encode("utf-8")
            self.assertEqual(proxywatch._valid_canary_payload(payload),
                             "with OK", repr(text))
        wrong = json.dumps({"type": "message", "role": "assistant",
                            "content": [{"type": "text", "text": "OK OK"}],
                            "stop_reason": "end_turn"}).encode("utf-8")
        self.assertIsNone(proxywatch._valid_canary_payload(wrong))

    def test_reasoning_model_that_spent_the_cap_thinking_is_generating(self):
        """The openai-compat translator emits no thinking block, so a reasoning
        model at the eight-token cap arrives as content [] + max_tokens +
        output_tokens > 0. That is a generating model (task/2664); a dead or
        exhausted credential never carries that pair."""
        generating = json.dumps({"type": "message", "role": "assistant",
                                 "content": [], "stop_reason": "max_tokens",
                                 "usage": {"input_tokens": 34, "output_tokens": 8}}
                                ).encode("utf-8")
        self.assertEqual(proxywatch._valid_canary_payload(generating),
                         "generating at the eight-token cap (reasoning spent the budget)")
        for body in (
                {"type": "message", "role": "assistant", "content": [],
                 "stop_reason": "max_tokens", "usage": {"output_tokens": 0}},
                {"type": "message", "role": "assistant", "content": [],
                 "stop_reason": "end_turn", "usage": {"output_tokens": 8}},
                {"type": "message", "role": "assistant", "content": [],
                 "stop_reason": "max_tokens"},
                {"type": "message", "role": "assistant",
                 "content": [{"type": "text", "text": ""}],
                 "stop_reason": "max_tokens", "usage": {"output_tokens": "8"}}):
            with self.subTest(body=body):
                self.assertIsNone(proxywatch._valid_canary_payload(
                    json.dumps(body).encode("utf-8")))

    def test_walled_canary_naming_the_loaded_provider_attests_the_family(self):
        """A wall is not an identity change: the proxy's own cooldown refusal
        names the loaded route, and the proof mints with the canary recorded
        as PROXY-COOLDOWN rather than being erased for the pass."""
        known = {"alias": "ds4-pro", "provider": "opencode-go",
                 "upstream_model": "deepseek-v4-pro",
                 "base_url": "https://opencode.ai/zen/go/v1"}
        config = self._write_key_routes(((known, "go-key"),))
        _route, token, auth_routes, err = proxywatch._proxy_config_route(
            config, "ds4-pro")
        self.assertIsNone(err, err)
        base = {key: value for key, value in runtime_proof(route=known).items()
                if key not in ("route", "observed_at", "canary")}
        shape = {"url": base["local_base_url"], "token": token,
                 "model": "ds4-pro", "auth_routes": auth_routes,
                 "proof": base}
        refusal = ("HTTP 429 — no available credential for ds4-pro via provider "
                   "openai-compatible-opencode-go: 1 cooling down (reset in 3h)")
        with mock.patch.object(proxywatch, "_proxy_runtime_shape",
                               return_value=(shape, None)), \
                mock.patch.object(
                    proxywatch, "_canary_once",
                    return_value=(proxywatch._PROXY_COOLDOWN, refusal, 4, 429,
                                  None, None)):
            proof, err = proxywatch.proxy_runtime_canary("costume", observed_at=1000)
        self.assertIsNone(err, err)
        self.assertEqual(proof["route"], known)
        self.assertEqual(proof["canary"],
                         {"state": proxywatch._PROXY_COOLDOWN, "status": 429})
        self.assertEqual(proxywatch._proxy_proof_family(proof),
                         ("ds4pro", None))  # noqa: SEAT_NAME — configured family identity is the property under test
        healthy = dict(proof, canary={"state": "HEALTHY", "status": 200})
        self.assertTrue(proxywatch._proxy_proofs_equivalent(proof, healthy))

    def test_walled_canary_naming_an_unloaded_provider_mints_nothing(self):
        known = {"alias": "ds4-pro", "provider": "opencode-go",
                 "upstream_model": "deepseek-v4-pro",
                 "base_url": "https://opencode.ai/zen/go/v1"}
        config = self._write_key_routes(((known, "go-key"),))
        _route, token, auth_routes, err = proxywatch._proxy_config_route(
            config, "ds4-pro")
        self.assertIsNone(err, err)
        base = {key: value for key, value in runtime_proof(route=known).items()
                if key not in ("route", "observed_at", "canary")}
        shape = {"url": base["local_base_url"], "token": token,
                 "model": "ds4-pro", "auth_routes": auth_routes,
                 "proof": base}
        for refusal in (
                "HTTP 429 — no available credential for ds4-pro via provider "
                "openai-compatible-elsewhere: 1 cooling down",
                "HTTP 429 — no available credential for other-alias via provider "
                "openai-compatible-opencode-go: 1 cooling down",
                "HTTP 429 — rate limited"):
            with self.subTest(refusal=refusal), \
                    mock.patch.object(proxywatch, "_proxy_runtime_shape",
                                      return_value=(shape, None)), \
                    mock.patch.object(
                        proxywatch, "_canary_once",
                        return_value=(proxywatch._PROXY_COOLDOWN, refusal, 4,
                                      429, None, None)):
                proof, err = proxywatch.proxy_runtime_canary("costume")
                self.assertIsNone(proof)
                self.assertIn("walled canary cannot attest authority", err)

    def test_proof_shape_admits_exactly_two_canary_attestations(self):
        proof = runtime_proof()
        for canary in ({"state": "HEALTHY", "status": 200},
                       {"state": proxywatch._PROXY_COOLDOWN, "status": 429}):
            with self.subTest(canary=canary):
                shape, err = proxywatch._proxy_proof_shape(dict(proof, canary=canary))
                self.assertIsNone(err, err)
        for canary in ({"state": "MALFORMED200", "status": 200},
                       {"state": proxywatch._PROXY_COOLDOWN, "status": 200},
                       {"state": "HEALTHY", "status": 429},
                       {"state": "RATE-LIMITED", "status": 429}):
            with self.subTest(canary=canary):
                shape, err = proxywatch._proxy_proof_shape(dict(proof, canary=canary))
                self.assertIsNone(shape)
                self.assertIn("no authenticated canary", err)

    def test_an_unresolved_selected_route_never_borrows_a_known_fallback(self):
        unknown = {"alias": "ds4-pro", "provider": "costume",
                   "upstream_model": "costume-v1",
                   "base_url": "https://unknown.example.test/v1"}
        known = {"alias": "ds4-pro", "provider": "opencode-go",
                 "upstream_model": "deepseek-v4-pro",
                 "base_url": "https://opencode.ai/zen/go/v1"}
        routes = ((unknown, "unknown-key"), (known, "known-key"))
        config = self._write_key_routes(routes)
        _route, token, auth_routes, err = proxywatch._proxy_config_route(
            config, "ds4-pro")
        self.assertIsNone(err, err)
        base = {key: value for key, value in runtime_proof(route=known).items()
                if key not in ("route", "observed_at", "canary")}
        shape = {"url": base["local_base_url"], "token": token,
                 "model": "ds4-pro", "auth_routes": auth_routes,
                 "proof": base}
        unknown_index = self._key_index(unknown["base_url"], "unknown-key")
        with mock.patch.object(proxywatch, "_proxy_runtime_shape",
                               return_value=(shape, None)), \
                mock.patch.object(
                    proxywatch, "_canary_once",
                    return_value=("HEALTHY", "HTTP 200 with OK", 4, 200,
                                  unknown["upstream_model"],
                                  self._trace(unknown_index))):
            proof, err = proxywatch.proxy_runtime_canary("costume")
        self.assertIsNone(proof)
        self.assertIn("0 configured families", err)
        known_index = self._key_index(known["base_url"], "known-key")
        with mock.patch.object(proxywatch, "_proxy_runtime_shape",
                               return_value=(shape, None)), \
                mock.patch.object(
                    proxywatch, "_canary_once",
                    return_value=("HEALTHY", "HTTP 200 with OK", 4, 200,
                                  known["upstream_model"],
                                  self._trace(known_index))):
            proof, err = proxywatch.proxy_runtime_canary("costume")
        self.assertIsNone(err, err)
        self.assertEqual(proof["route"], known)

    def test_two_families_are_disambiguated_by_the_selected_credential(self):  # noqa: VACUOUS_ASSERTION — the literal two-family table and two auth indexes are asserted before both selection arms
        routes = (
            ({"alias": "shared", "provider": "one",
              "upstream_model": "model-one",
              "base_url": "https://one.example.test/v1"}, "one-key"),
            ({"alias": "shared", "provider": "two",
              "upstream_model": "model-two",
              "base_url": "https://two.example.test/v1"}, "two-key"),
        )
        table = {
            "family-one": {"mode": "proxy-key", "model": "shared",
                           "provider": "one", "upstream_model": "model-one",
                           "base_url": "https://one.example.test/v1"},
            "family-two": {"mode": "proxy-key", "model": "shared",
                           "provider": "two", "upstream_model": "model-two",
                           "base_url": "https://two.example.test/v1"},
        }
        config = self._write_key_routes(routes)
        _route, token, auth_routes, err = proxywatch._proxy_config_route(
            config, "shared")
        self.assertIsNone(err, err)
        for expected, (selected, api_key) in zip(table, routes):
            with self.subTest(family=expected):
                index = self._key_index(selected["base_url"], api_key)
                proof = runtime_proof(route=selected)
                base = {key: value for key, value in proof.items()
                        if key not in ("route", "observed_at", "canary")}
                shape = {"url": base["local_base_url"], "token": token,
                         "model": "shared", "auth_routes": auth_routes,
                         "proof": base}
                with mock.patch.dict(seat.FAMILIES, table, clear=True), \
                        mock.patch.object(proxywatch, "_proxy_runtime_shape",
                                          return_value=(shape, None)), \
                        mock.patch.object(
                            proxywatch, "_canary_once",
                            return_value=("HEALTHY", "HTTP 200 with OK", 4, 200,
                                          selected["upstream_model"],
                                          self._trace(index))):
                    proof, err = proxywatch.proxy_runtime_canary("costume")
                    self.assertIsNone(err, err)
                    self.assertEqual(proof["route"], selected)
                    self.assertEqual(proxywatch._proxy_proof_family(proof),
                                     (expected, None))

    def test_one_credential_index_cannot_claim_two_routes(self):
        base_url, api_key = "https://shared.example.test/v1", "shared-key"
        routes = (
            ({"alias": "shared", "provider": "one",
              "upstream_model": "model-one", "base_url": base_url}, api_key),
            ({"alias": "shared", "provider": "two",
              "upstream_model": "model-two", "base_url": base_url}, api_key),
        )
        config = self._write_key_routes(routes)
        _route, token, auth_routes, err = proxywatch._proxy_config_route(
            config, "shared")
        self.assertIsNone(err, err)
        index = self._key_index(base_url, api_key)
        self.assertEqual(len(auth_routes[index]), 2)
        proof = runtime_proof(route=routes[0][0])
        base = {key: value for key, value in proof.items()
                if key not in ("route", "observed_at", "canary")}
        shape = {"url": base["local_base_url"], "token": token,
                 "model": "shared", "auth_routes": auth_routes,
                 "proof": base}
        with mock.patch.object(proxywatch, "_proxy_runtime_shape",
                               return_value=(shape, None)), \
                mock.patch.object(
                    proxywatch, "_canary_once",
                    return_value=("HEALTHY", "HTTP 200 with OK", 4, 200,
                                  "model-one", self._trace(index))):
            proof, err = proxywatch.proxy_runtime_canary("costume")
        self.assertIsNone(proof)
        self.assertIn("does not name one loaded provider route", err)

    def test_runtime_change_across_the_canary_fails_closed(self):
        route = {"alias": "ds4-pro", "provider": "opencode-go",
                 "upstream_model": "deepseek-v4-pro",
                 "base_url": "https://opencode.ai/zen/go/v1"}
        index = "a" * 16
        shape = {"url": "http://127.0.0.1:8360", "token": "secret",
                 "model": "ds4-pro", "auth_routes": {index: (route,)},
                 "proof": {"v": 1, "route": route}}
        changed = dict(shape, model="replacement")
        with mock.patch.object(proxywatch, "_proxy_runtime_shape",
                               side_effect=[(shape, None), (changed, None)]), \
                mock.patch.object(
                    proxywatch, "_canary_once",
                    return_value=("HEALTHY", "HTTP 200", 4, 200,
                                  "deepseek-v4-pro", self._trace(index))):
            proof, err = proxywatch.proxy_runtime_canary("ds4pro")
        self.assertIsNone(proof)
        self.assertIn("changed across its canary", err)

    def test_oauth_model_provider_route_must_map_to_exactly_one_family(self):  # noqa: VACUOUS_ASSERTION — one-match control proves only duplication closes the gate
        route = {"alias": "gpt-live", "provider": "codex",
                 "upstream_model": "gpt-live"}
        one = {"codex": {"mode": "proxy", "model": "gpt-live",
                          "auth_type": "codex"}}
        with mock.patch.dict(seat.FAMILIES, one, clear=True):
            self.assertEqual(proxywatch._proxy_route_family(route),
                             ("codex", None))
        duplicate = dict(one, costume=dict(one["codex"]))
        with mock.patch.dict(seat.FAMILIES, duplicate, clear=True):
            family, err = proxywatch._proxy_route_family(route)
        self.assertIsNone(family)
        self.assertIn("2 configured families", err)

    def test_dispatch_author_resolver_reproves_a_deferred_selected_route(self):  # noqa: VACUOUS_ASSERTION — the exact resolver result and repeated no-extra-canary read prove the complete verdict-time path
        routes = (
            ({"alias": "ds4-pro", "provider": "deepseek-direct",
              "upstream_model": "deepseek-v4-pro",
              "base_url": "https://api.deepseek.com/v1"},
             "direct-candidate-secret"),
            ({"alias": "ds4-pro", "provider": "opencode-go",
              "upstream_model": "deepseek-v4-pro",
              "base_url": "https://opencode.ai/zen/go/v1"},
             "selected-candidate-secret"),
        )
        config = self._write_key_routes(routes)
        _route, _token, auth_routes, err = proxywatch._proxy_config_route(
            config, "ds4-pro")
        self.assertIsNone(err, err)
        selected = routes[1][0]
        observed = int(time.time())
        proof = dict(runtime_proof(observed=observed, route=selected),
                     v=proxywatch._PROXY_RUNTIME_V)
        self.write_state({"costume-storage-key": proof}, ts=observed)
        runtime, err = proxywatch._proxy_proof_runtime(proof)
        self.assertIsNone(err, err)
        seats.write_roster("measured-seat", session=proof["session"])
        _entry, err = seats.stamp_proxy_runtime(
            proof["session"], runtime, proof)
        self.assertIsNone(err, err)
        shape = self.deferred_shape_for(proof, auth_routes)
        with mock.patch.object(proxywatch, "_proxy_runtime_shape",
                               return_value=(shape, None)), \
                mock.patch.object(proxywatch, "proxy_runtime_canary",
                                  return_value=(proof, None)) as canary:
            first = dispatches._approval_identity_family_evidence(
                "measured-seat", session=proof["session"],
                require_exact_session=True)
            second = dispatches._approval_identity_family_evidence(
                "measured-seat", session=proof["session"],
                require_exact_session=True)
        self.assertEqual(first[0], {"ds4pro"}, first)  # noqa: SEAT_NAME — configured family identity is the property under test
        self.assertIsNone(first[3], first[3])
        self.assertEqual(second, first)
        canary.assert_called_once_with("measured-seat", observed_at=observed)
        serialized = json.dumps(first[1], sort_keys=True)
        self.assertNotIn("auth_routes", serialized)
        self.assertNotIn("direct-candidate-secret", serialized)
        self.assertNotIn("selected-candidate-secret", serialized)
        for index in auth_routes:
            self.assertNotIn(index, serialized)

    def test_deferred_reproof_refuses_a_removed_cached_route(self):  # noqa: VACUOUS_ASSERTION — the preceding dispatch-resolver arm positively proves this exact deferred owner path
        proof = runtime_proof()
        replacement = dict(proof["route"], provider="deepseek-direct",
                           base_url="https://api.deepseek.com/v1")
        shape = self.deferred_shape_for(
            proof, {"a" * 16: (replacement,)})
        self.assert_deferred_reproof_refuses(proof, shape)

    def test_deferred_reproof_compares_the_full_route_not_only_provider(self):  # noqa: VACUOUS_ASSERTION — the preceding dispatch-resolver arm positively proves this exact deferred owner path
        proof = runtime_proof()
        changed = dict(proof["route"],
                       base_url="https://mutated.example.test/v1")
        self.assertEqual(changed["provider"], proof["route"]["provider"])
        shape = self.deferred_shape_for(proof, {"a" * 16: (changed,)})
        self.assert_deferred_reproof_refuses(proof, shape)

    def test_deferred_reproof_refuses_one_index_colliding_across_routes(self):  # noqa: VACUOUS_ASSERTION — the preceding dispatch-resolver arm positively proves this exact deferred owner path
        proof = runtime_proof()
        collision = dict(proof["route"], provider="deepseek-direct",
                         base_url="https://api.deepseek.com/v1")
        shape = self.deferred_shape_for(
            proof, {"a" * 16: (proof["route"], collision)})
        self.assert_deferred_reproof_refuses(proof, shape)

    def test_deferred_reproof_accepts_multiple_credentials_for_one_route(self):
        proof = dict(runtime_proof(), v=proxywatch._PROXY_RUNTIME_V)
        self.write_state({"proof-key": proof})
        shape = self.deferred_shape_for(
            proof, {"a" * 16: (proof["route"],),
                    "b" * 16: (dict(proof["route"]),)})
        with mock.patch.object(proxywatch, "_roster_identity_for_session",
                               return_value=("measured-seat", None)), \
                mock.patch.object(proxywatch, "_proxy_runtime_shape",
                                  return_value=(shape, None)), \
                mock.patch.object(proxywatch, "proxy_runtime_canary",
                                  return_value=(proof, None)) as canary:
            family, got, err = proxywatch.proxy_runtime_snapshot(
                proof["session"], now=1000)
        self.assertEqual(family, "ds4pro")  # noqa: SEAT_NAME — configured family identity is the property under test
        self.assertEqual(got, proof)
        self.assertIsNone(err, err)
        canary.assert_called_once_with("measured-seat", observed_at=1000)

    def test_deferred_reproof_still_binds_every_route_independent_field(self):  # noqa: VACUOUS_ASSERTION — the preceding dispatch-resolver arm positively proves this exact deferred owner path
        proof = runtime_proof()
        shape = self.deferred_shape_for(
            proof, {"a" * 16: (proof["route"],)})
        for key, value in shape["proof"].items():
            if key == "v":
                continue
            changed = value + 1 if type(value) is int else value + "-changed"
            if key == "config_sha256":
                changed = "b" * 64
            with self.subTest(field=key):
                candidate = dict(shape, proof=dict(shape["proof"],
                                                    **{key: changed}))
                self.assert_deferred_reproof_refuses(proof, candidate)

    def test_deferred_reproof_refuses_cross_family_candidate_selection(self):  # noqa: VACUOUS_ASSERTION — same-family resolver success above is the positive control for this reviewer-found ambiguity
        routes = (
            {"alias": "shared", "provider": "one",
             "upstream_model": "model-one",
             "base_url": "https://one.example.test/v1"},
            {"alias": "shared", "provider": "two",
             "upstream_model": "model-two",
             "base_url": "https://two.example.test/v1"},
        )
        configured = {
            "family-one": {"mode": "proxy-key", "model": "shared",
                           "provider": "one", "upstream_model": "model-one",
                           "base_url": "https://one.example.test/v1"},
            "family-two": {"mode": "proxy-key", "model": "shared",
                           "provider": "two", "upstream_model": "model-two",
                           "base_url": "https://two.example.test/v1"},
        }
        proof = dict(runtime_proof(route=routes[1]),
                     v=proxywatch._PROXY_RUNTIME_V)
        shape = self.deferred_shape_for(
            proof, {"a" * 16: (routes[0],), "b" * 16: (routes[1],)})
        with mock.patch.dict(seat.FAMILIES, configured, clear=True):
            self.assert_deferred_reproof_refuses(proof, shape)

    def test_deferred_reproof_refuses_unknown_candidate_family(self):  # noqa: VACUOUS_ASSERTION — same-family resolver success above is the positive control for this unknown-family arm
        proof = dict(runtime_proof(), v=proxywatch._PROXY_RUNTIME_V)
        unknown = dict(proof["route"], provider="costume",
                       base_url="https://unknown.example.test/v1")
        shape = self.deferred_shape_for(
            proof, {"a" * 16: (proof["route"],),
                    "b" * 16: (unknown,)})
        self.assert_deferred_reproof_refuses(proof, shape)

    def test_deferred_reproof_refuses_cached_route_family_ambiguity(self):  # noqa: VACUOUS_ASSERTION — the one-family mapping control immediately above this section proves only ambiguity closes the gate
        proof = dict(runtime_proof(), v=proxywatch._PROXY_RUNTIME_V)
        self.write_state({"proof-key": proof})
        family = {"mode": "proxy-key", "model": "ds4-pro",
                  "provider": "opencode-go",
                  "upstream_model": "deepseek-v4-pro",
                  "base_url": "https://opencode.ai/zen/go/v1"}
        configured = {"first": family, "second": dict(family)}
        with mock.patch.dict(seat.FAMILIES, configured, clear=True), \
                mock.patch.object(proxywatch, "_proxy_runtime_shape") as shape, \
                mock.patch.object(proxywatch, "proxy_runtime_canary") as canary:
            resolved, got, err = proxywatch.proxy_runtime_snapshot(
                proof["session"], now=1000)
        self.assertIsNone(resolved)
        self.assertIsNone(got)
        self.assertIn("runtime proof is malformed", err)
        shape.assert_not_called()
        canary.assert_not_called()

    def test_singular_route_reproof_behavior_is_unchanged(self):
        proof = dict(runtime_proof(), v=proxywatch._PROXY_RUNTIME_V)
        self.write_state({"proof-key": proof})
        with mock.patch.object(proxywatch, "_roster_identity_for_session",
                               return_value=("measured-seat", None)), \
                mock.patch.object(proxywatch, "_proxy_runtime_shape",
                                  return_value=(self.shape_for(proof), None)), \
                mock.patch.object(proxywatch, "proxy_runtime_canary",
                                  return_value=(proof, None)) as canary:
            family, got, err = proxywatch.proxy_runtime_snapshot(
                proof["session"], now=1000)
        self.assertIsNone(err, err)
        self.assertEqual((family, got), ("ds4pro", proof))  # noqa: SEAT_NAME — configured family identity is the property under test
        canary.assert_called_once_with("measured-seat", observed_at=1000)

    def test_storage_key_ds4pro_cannot_override_a_route_mapping_to_Codex(self):
        route = {"alias": "codex-wire", "provider": "openai",
                 "upstream_model": "gpt-live", "base_url": "https://api.openai.test/v1"}
        proof = runtime_proof(route=route)
        self.write_state({"ds4pro": proof})
        configured = {"codex": {"mode": "proxy-key", "model": "codex-wire",
                                 "provider": "openai", "upstream_model": "gpt-live",
                                 "base_url": "https://api.openai.test/v1"}}
        with mock.patch.dict(seat.FAMILIES, configured, clear=True), \
                mock.patch.object(proxywatch, "_roster_identity_for_session",
                                  return_value=("measured-seat", None)), \
                mock.patch.object(proxywatch, "_proxy_runtime_shape",
                                  return_value=(self.shape_for(proof), None)), \
                mock.patch.object(proxywatch, "proxy_runtime_canary",
                                  return_value=(proof, None)):
            family, got, err = proxywatch.proxy_runtime_snapshot(
                "session-ds4pro", now=1000)
        self.assertIsNone(err, err)
        self.assertEqual(family, "codex")
        self.assertEqual(got["route"], route)
        self.assertNotIn("family", got)

    def test_handwritten_cache_without_a_live_runtime_is_unknown(self):  # noqa: VACUOUS_ASSERTION — valid proof schema is the positive control before live provenance refusal
        proof = runtime_proof()
        self.assertEqual(proxywatch._proxy_proof_family(proof), ("ds4pro", None))
        self.write_state({"costume-key": proof})
        with mock.patch.object(proxywatch, "_roster_identity_for_session",
                               return_value=("measured-seat", None)), \
                mock.patch.object(proxywatch, "_proxy_runtime_shape",
                                  return_value=(None, "no exact listener")), \
                mock.patch.object(proxywatch, "proxy_runtime_canary") as canary:
            family, got, err = proxywatch.proxy_runtime_snapshot(
                proof["session"], now=1000)
        self.assertIsNone(family)
        self.assertIsNone(got)
        self.assertIn("cannot re-prove cached evidence", err)
        canary.assert_not_called()

    def test_cached_proof_is_unknown_after_a_bound_process_changes(self):  # noqa: VACUOUS_ASSERTION — valid cached proof is the positive control before one live binding changes
        proof = runtime_proof()
        self.assertEqual(proxywatch._proxy_proof_family(proof), ("ds4pro", None))
        self.write_state({"ds4pro": proof})
        changed = self.shape_for(proof)
        changed["proof"] = dict(changed["proof"], proxy_pid=9999)
        with mock.patch.object(proxywatch, "_roster_identity_for_session",
                               return_value=("measured-seat", None)), \
                mock.patch.object(proxywatch, "_proxy_runtime_shape",
                                  return_value=(changed, None)), \
                mock.patch.object(proxywatch, "proxy_runtime_canary") as canary:
            family, got, err = proxywatch.proxy_runtime_snapshot(
                proof["session"], now=1000)
        self.assertIsNone(family)
        self.assertIsNone(got)
        self.assertIn("no longer matches the cached proof", err)
        canary.assert_not_called()

    def test_v2_cached_and_roster_proofs_revalidate_against_current_v3_measurement(self):  # noqa: VACUOUS_ASSERTION — the fixed two-arm tuple always asserts the concrete family and proof returned by each compatibility path
        transitional = runtime_proof()
        expected_family, err = proxywatch._proxy_proof_family(transitional)
        self.assertIsNone(err, err)
        legacy = dict(transitional)
        legacy.pop("agent_harness")
        current = dict(transitional, v=proxywatch._PROXY_RUNTIME_V)
        for name, proof in (("transitional", transitional), ("legacy", legacy)):
            with self.subTest(name=name):
                proxywatch._PROXY_AUTH_CANARIES.clear()
                self.write_state({"seat-a": proof})
                with mock.patch.object(
                        proxywatch, "_roster_identity_for_session",
                        return_value=("measured-seat", None)), \
                        mock.patch.object(
                            proxywatch, "_proxy_runtime_shape",
                            return_value=(self.shape_for(current), None)), \
                        mock.patch.object(
                            proxywatch, "proxy_runtime_canary",
                            return_value=(current, None)):
                    family, got, err = proxywatch.proxy_runtime_snapshot(
                        proof["session"], now=1000, expected_proof=proof)
                self.assertIsNone(err, err)
                self.assertEqual(family, expected_family)
                self.assertEqual(got, proof)

    def test_first_authority_read_repeats_the_canary_then_reuses_only_process_memory(self):  # noqa: VACUOUS_ASSERTION — first and second reads positively prove authority
        proof = runtime_proof()
        self.write_state({"ds4pro": proof})
        with mock.patch.object(proxywatch, "_roster_identity_for_session",
                               return_value=("measured-seat", None)), \
                mock.patch.object(proxywatch, "_proxy_runtime_shape",
                                  return_value=(self.shape_for(proof), None)) as shape, \
                mock.patch.object(proxywatch, "proxy_runtime_canary",
                                  return_value=(proof, None)) as canary:
            first = proxywatch.proxy_runtime_snapshot(proof["session"], now=1000)
            second = proxywatch.proxy_runtime_snapshot(proof["session"], now=1001)
        self.assertEqual(first, second)
        self.assertEqual(first[:2], ("ds4pro", proof))
        self.assertIsNone(first[2])
        self.assertEqual(shape.call_count, 2,
                         "every authority read must remeasure the live shape")
        canary.assert_called_once_with("measured-seat", observed_at=1000)

    def test_matching_live_shape_without_a_successful_current_canary_is_unknown(self):
        proof = runtime_proof()
        self.write_state({"ds4pro": proof})
        with mock.patch.object(proxywatch, "_roster_identity_for_session",
                               return_value=("measured-seat", None)), \
                mock.patch.object(proxywatch, "_proxy_runtime_shape",
                                  return_value=(self.shape_for(proof), None)), \
                mock.patch.object(proxywatch, "proxy_runtime_canary",
                                  return_value=(None, "HTTP 401")):
            family, got, err = proxywatch.proxy_runtime_snapshot(
                proof["session"], now=1000)
        self.assertIsNone(family)
        self.assertIsNone(got)
        self.assertIn("canary cannot attest authority", err)

    def test_missing_stale_ambiguous_and_session_mismatched_proof_are_unknown(self):  # noqa: VACUOUS_ASSERTION — every arm requires a concrete fail-closed error
        cases = (
            ("missing", {}, 1000),
            ("stale", {"ds4pro": runtime_proof()},
             1000 + proxywatch.UPSTREAM_CACHE_FRESH_S + 1),
            ("ambiguous", {"ds4pro": runtime_proof(),
                           "costume": runtime_proof()}, 1000),
            ("session mismatch", {"ds4pro": runtime_proof(session="other")},
             1000),
        )
        for name, proofs, now in cases:
            with self.subTest(name=name):
                self.write_state(proofs)
                family, proof, err = proxywatch.proxy_runtime_snapshot(
                    "session-ds4pro", now=now)
                self.assertIsNone(family)
                self.assertIsNone(proof)
                self.assertIsNotNone(err)

    def test_record_persists_only_exact_sanitized_proof(self):
        clean = runtime_proof()
        tainted = dict(clean, api_key="must-never-land")
        report = rep(row(), proxy_runtime={"ds4pro": clean, "tainted": tainted})
        self.assertTrue(proxywatch.record(report))
        with open(self.state, encoding="utf-8") as f:
            saved = json.load(f)["proxy_runtime"]
        self.assertEqual(set(saved), {"ds4pro"})
        self.assertNotIn("must-never-land", json.dumps(saved))


class UpstreamCanaryTest(unittest.TestCase):
    def test_three_second_transient_blip_stays_HEALTHY(self):
        attempts = [("UPSTREAM-OVERLOADED", "HTTP 503", 10),
                    ("HEALTHY", "HTTP 200", 20)]
        sleeps = []
        with mock.patch.object(proxywatch, "_upstream_once", side_effect=attempts):
            state, detail, elapsed = proxywatch.upstream_canary(
                "codex", sleep=sleeps.append)
        self.assertEqual((state, elapsed), ("HEALTHY", 30))
        self.assertEqual(sleeps, [proxywatch.UPSTREAM_CONFIRM_S])
        self.assertIn("transient", detail)

    def test_persistent_completed_failure_is_named_after_one_confirmation(self):
        attempts = [("UPSTREAM-OVERLOADED", "HTTP 503", 10),
                    ("UPSTREAM-OVERLOADED", "HTTP 503", 12)]
        sleeps = []
        with mock.patch.object(proxywatch, "_upstream_once", side_effect=attempts) \
                as once:
            state, detail, elapsed = proxywatch.upstream_canary(
                "codex", sleep=sleeps.append)
        self.assertEqual((state, elapsed), ("UPSTREAM-OVERLOADED", 22))
        self.assertEqual(once.call_count, 2)
        self.assertEqual(sleeps, [proxywatch.UPSTREAM_CONFIRM_S])
        self.assertIn("then UPSTREAM-OVERLOADED", detail)

    def test_client_timeout_is_never_retried_into_duplicate_token_burn(self):
        with mock.patch.object(proxywatch, "_upstream_once",
                               return_value=(proxywatch._CLIENT_TIMEOUT,
                                             "30s", 30000)) as once:
            state, _detail, _ms = proxywatch.upstream_canary(
                "kimi", sleep=mock.Mock())
        self.assertEqual(state, "TIMEOUT-500")
        once.assert_called_once_with("kimi")

    def test_request_layer_recognizes_a_client_timeout_without_waiting_for_clock(self):
        with mock.patch("helm.seat._seat_family",
                        return_value=("codex", None)), \
                mock.patch("helm.pi.seat_port", return_value=(8317, None)), \
                mock.patch("helm.pi._pi_api_key", return_value="secret"), \
                mock.patch("urllib.request.urlopen",
                           side_effect=TimeoutError("timed out")):
            state, _detail, _ms = proxywatch._upstream_once("codex")
        self.assertEqual(state, proxywatch._CLIENT_TIMEOUT)

    def test_completed_HTTP_timeout_is_confirmed_and_can_clear(self):
        attempts = [("TIMEOUT-500", "HTTP 500 — request timed out", 5),
                    ("HEALTHY", "HTTP 200 with OK", 7)]
        with mock.patch.object(proxywatch, "_upstream_once", side_effect=attempts) \
                as once:
            state, detail, elapsed = proxywatch.upstream_canary(
                "codex", sleep=lambda _seconds: None)
        self.assertEqual((state, elapsed), ("HEALTHY", 12))
        self.assertEqual(once.call_count, 2)
        self.assertIn("transient TIMEOUT-500", detail)

    def test_dark_then_unreadable_confirmation_preserves_the_dark_evidence(self):
        attempts = [("AUTH-UNAVAILABLE", "no auth", 4),
                    ("UNKNOWN", "connection reset", 2)]
        with mock.patch.object(proxywatch, "_upstream_once", side_effect=attempts):
            state, detail, elapsed = proxywatch.upstream_canary(
                "codex", sleep=lambda _seconds: None)
        self.assertEqual((state, elapsed), ("AUTH-UNAVAILABLE", 6))
        self.assertIn("confirmation unreadable", detail)

    def test_error_body_classification_names_the_supported_layer(self):
        cases = ((503, "service_unavailable_error overloaded",
                  "UPSTREAM-OVERLOADED"),
                 (503, "auth_unavailable: no auth available",
                  "AUTH-UNAVAILABLE"),
                 (500, "request timed out", "TIMEOUT-500"),
                 (503, "other", "UPSTREAM-5XX"),
                 (402, "balance", "QUOTA-WALL"),
                 (401, "bad key", "AUTH-401"),
                 (429, "slow", "RATE-LIMITED"),
                 (418, "teapot", "UPSTREAM-4XX"))
        for code, text, expected in cases:
            with self.subTest(code=code, text=text):
                self.assertEqual(proxywatch._upstream_state(code, text), expected)

    def test_money_refusal_classifier_carries_canonical_reset_without_body(self):
        detail = "HTTP 402 — usage balance exhausted. Resets in 1h 30m"
        state = proxywatch._upstream_state(
            402, "usage balance exhausted. Resets in 1h 30m")
        rec = proxywatch._compose_upstream_seat(
            "grok", (state, detail, 1), {}, 1000)
        self.assertEqual(rec["state"], proxywatch._QUOTA_WALL)
        self.assertEqual(rec["refusal_class"], "money")
        self.assertEqual(rec["resets_at_ms"], 6400000)
        self.assertEqual(rec["reset_kind"], "vendor")
        self.assertEqual(rec["reset_source"], "canary")
        self.assertEqual(rec["refusal_provenance"], "http-status:402")
        persisted = dict(rec)
        persisted.pop("detail")
        self.assertNotIn("usage balance", json.dumps(persisted))

    def test_provider_429_needs_a_known_money_signature(self):
        self.assertEqual(proxywatch._upstream_state(
            429, "GoUsageLimitError Weekly limit. Resets in 2h"),
                         proxywatch._QUOTA_WALL)
        self.assertEqual(proxywatch._upstream_state(
            429, "Rate limit exceeded: free-models-per-day"),
                         proxywatch._QUOTA_WALL)
        self.assertEqual(proxywatch._upstream_state(429, "ordinary rate limit"),
                         "RATE-LIMITED")

    def test_duration_resets_keep_single_letter_seconds(self):  # noqa: VACUOUS_ASSERTION — each case positively asserts the exact parsed reset instant; no absence is the tested observable
        for text, seconds in (("usage balance exhausted; resets in 45s", 45),
                              ("usage balance exhausted; resets in 1h 30s", 3630)):
            with self.subTest(text=text):
                self.assertEqual(proxywatch._quota_reset_ms(text, 1000),
                                 int((1000 + seconds) * 1000))

    def test_auth_unavailable_is_reach_while_recent_money_wall_stays_separate(self):
        before = {"state": proxywatch._QUOTA_WALL, "dark": True,
                  "since": "1970-01-01T00:16:40Z",
                  "wall_observed_at": "1970-01-01T00:16:40Z",
                  "refusal_class": "money", "resets_at_ms": 4000000,
                  "reset_kind": "vendor", "reset_source": "canary"}
        inherited = proxywatch._compose_upstream_seat(
            "grok", ("AUTH-UNAVAILABLE", "HTTP 503 — auth_unavailable", 1),
            before, 1600)
        self.assertEqual(inherited["state"], "AUTH-UNAVAILABLE")
        self.assertEqual(inherited["quota_wall"], proxywatch._QUOTA_WALL)
        self.assertEqual(inherited["resets_at_ms"], 4000000)
        self.assertEqual(inherited["reset_kind"], "vendor")
        self.assertEqual(inherited["reset_source"], "canary")
        from helm import burnflags
        self.assertEqual(burnflags.derive_reach(
            "grok", inherited, now=1600)["axis"], "reach")
        money = burnflags.derive_quota_wall("grok", inherited)
        self.assertEqual((money["axis"], money["expires_at"]),
                         ("money", 4000.0))
        bare = proxywatch._compose_upstream_seat(
            "grok", ("AUTH-UNAVAILABLE", "HTTP 503 — auth_unavailable", 1),
            {}, 1600)
        self.assertEqual(bare["state"], "AUTH-UNAVAILABLE")
        self.assertNotIn("quota_wall", bare)
        old = proxywatch._compose_upstream_seat(
            "grok", ("AUTH-UNAVAILABLE", "HTTP 503 — auth_unavailable", 1),
            before, 1000 + proxywatch.QUOTA_WALL_INHERIT_S + 1)
        self.assertEqual(old["state"], "AUTH-UNAVAILABLE")
        self.assertNotIn("quota_wall", old)
        reset_before = dict(before, resets_at_ms=1200000)
        after_reset = proxywatch._compose_upstream_seat(
            "grok", ("AUTH-UNAVAILABLE", "HTTP 503 — auth_unavailable", 1),
            reset_before, 1201)
        self.assertNotIn("quota_wall", after_reset)
        before_reset = proxywatch._compose_upstream_seat(
            "grok", ("AUTH-UNAVAILABLE", "HTTP 503 — auth_unavailable", 1),
            reset_before, 1199)
        self.assertEqual(before_reset["quota_wall"], proxywatch._QUOTA_WALL)

    def test_typed_local_cooldown_names_its_origin(self):
        body = ('{"type":"error","error":{"type":"rate_limit_error",'
                '"message":"All credentials for model gpt-5.6-sol are cooling '
                'down via provider codex"}}')
        self.assertIn("cooling down", body,
                      "the must-hit must carry the production refusal body")
        self.assertEqual(proxywatch._upstream_state(
            429, body, origin="local"), "PROXY-COOLDOWN")

    def test_selected_provider_429_with_proxy_like_text_keeps_its_origin(self):
        body = ('{"type":"error","error":{"type":"rate_limit_error",'
                '"message":"All credentials for model gpt-5.6-sol are cooling '
                'down via provider codex"}}')
        self.assertIn("cooling down", body,
                      "the must-miss must carry the local generator's prose")
        self.assertEqual(proxywatch._upstream_state(
            429, body, origin="provider"), "RATE-LIMITED")
        self.assertEqual(proxywatch._upstream_state(
            429, body, origin="unknown"), "RATE-LIMITED")
        self.assertEqual(proxywatch._refusal_cause((429,), (body,)),
                         "RATE-LIMITED")

    def test_provider_429_keeps_provider_origin(self):
        body = ('{"type":"error","error":{"type":"rate_limit_error",'
                '"message":"Rate limit exceeded for this organization"}}')
        self.assertIn("rate_limit_error", body,
                      "the must-miss fixture must name a provider rate limit")
        self.assertNotIn("cooling down", body.lower())
        for origin in ("provider", "unknown", None):
            with self.subTest(origin=origin):
                self.assertEqual(proxywatch._upstream_state(
                    429, body, origin=origin), "RATE-LIMITED")

    def test_unknown_429_does_not_acquire_proxy_origin(self):
        body = '{"error":{"message":"slow"}}'
        self.assertIn("slow", body,
                      "the must-miss fixture must carry an opaque 429 body")
        self.assertNotIn("model_cooldown", body)
        self.assertNotIn("cooling down", body.lower())
        self.assertEqual(proxywatch._upstream_state(
            429, body, origin="unknown"), "RATE-LIMITED")

    def test_proxy_cooldown_is_an_admitted_dark_state(self):
        record, err = proxywatch.upstream_record({"upstream": {"codex": {
            "state": "PROXY-COOLDOWN"}}}, "codex")
        self.assertIsNone(err)
        self.assertEqual(record["state"], "PROXY-COOLDOWN")
        self.assertTrue(record["dark"])
        self.assertIn("PROXY-COOLDOWN", proxywatch._UPSTREAM_DARK)

    @staticmethod
    def _refusal_through_shipped_seam(body, trace=None, origin=None):
        import io
        import urllib.error

        def refuse(_req, timeout=None):
            headers = {"X-CPA-TRACE-ID": trace} if trace else {}
            if origin is not None:
                headers["X-CPA-Refusal-Origin"] = origin
            raise urllib.error.HTTPError(
                "http://127.0.0.1:8317/v1/messages", 429, "refused", headers,
                io.BytesIO(body.encode("utf-8")))

        with tempfile.TemporaryDirectory(prefix="helm-test-pw-origin-") as tmp, \
                mock.patch.object(proxywatch, "_state_path",
                                  return_value=os.path.join(tmp, "state.json")), \
                mock.patch.object(proxywatch, "UPSTREAM_CONFIRM_S", 0), \
                mock.patch("helm.pi.seat_port", return_value=(8317, None)), \
                mock.patch("helm.pi._pi_api_key", return_value="secret"), \
                mock.patch("urllib.request.urlopen", side_effect=refuse):
            current = proxywatch.upstream_health(
                [row(seat="codex", family="codex", probe="healthy")],
                now=1000, prior={})
            family = current["codex"]
            report = rep(row(upstream=family["state"],
                             upstream_since=family["since"],
                             upstream_detail=family["detail"],
                             upstream_ms=family["ms"]), upstream=current)
            if not proxywatch.record(report, prior_state={}):
                raise AssertionError("production state writer refused the report")
            persisted, err = proxywatch.upstream_snapshot(now=1001)
            if err:
                raise AssertionError(err)
            rendered = proxywatch.report_lines(rep(
                row(upstream=persisted["codex"]["state"],
                    upstream_since=persisted["codex"]["since"]),
                upstream=persisted))
        return next(line for line in rendered if "FAMILY-DARK" in line)

    def test_proxy_cooldown_survives_the_shipped_writer_reader_seam(self):
        body = ('{"type":"error","error":{"type":"rate_limit_error",'
                '"message":"All credentials for model gpt-5.6-sol are cooling '
                'down via provider codex"}}')
        self.assertIn("All credentials for model", body,
                      "the must-hit seam input must carry proxy evidence")
        self.assertIn("cooling down via provider codex", body)
        # "upstream" IS A CLAIM ABOUT ORIGIN AND IT WAS FALSE HERE. A local
        # refusal never left the box, so the row now names whose failure it is.
        # Its sibling below is the must-miss: an UNTYPED dark state keeps the
        # word, because for those the state records WHAT was seen and nothing
        # about WHERE.
        self.assertEqual(
            self._refusal_through_shipped_seam(body, origin="local"),
            "  FAMILY-DARK codex: PROXY-COOLDOWN since "
            "1970-01-01T00:16:40Z via no representative — HELM'S OWN PROXY "
            "refused before any request left the box. That names WHERE this "
            "refusal happened and measures NOTHING about the provider — a "
            "local cooldown is often the mirror of an upstream wall. The "
            "evidence is what separates them: ")

    def test_proxy_like_provider_429_survives_the_same_seam(self):
        body = ('{"type":"error","error":{"type":"rate_limit_error",'
                '"message":"All credentials for model gpt-5.6-sol are cooling '
                'down via provider codex"}}')
        trace = "20260825195500-0123456789abcdef-deadbeef"
        self.assertIn("cooling down", body,
                      "the must-miss must carry the local generator's prose")
        self.assertEqual(
            self._refusal_through_shipped_seam(body, trace=trace),
            "  FAMILY-DARK codex: upstream RATE-LIMITED since "
            "1970-01-01T00:16:40Z via no representative — ")

    def test_local_origin_conflicting_with_selected_trace_is_not_local(self):
        body = ('{"type":"error","error":{"type":"rate_limit_error",'
                '"message":"All credentials for model gpt-5.6-sol are cooling '
                'down via provider codex"}}')
        trace = "20260825195500-0123456789abcdef-deadbeef"
        self.assertEqual(
            self._refusal_through_shipped_seam(
                body, trace=trace, origin="local"),
            "  FAMILY-DARK codex: upstream RATE-LIMITED since "
            "1970-01-01T00:16:40Z via no representative — ")

    def test_untrusted_origin_values_fail_provider_safe_at_the_same_seam(self):  # noqa: VACUOUS_ASSERTION — every malformed/absent case asserts the exact shipped operator row
        body = ('{"type":"error","error":{"type":"rate_limit_error",'
                '"message":"All credentials for model gpt-5.6-sol are cooling '
                'down via provider codex"}}')
        for origin in (None, "unknown", "LOCAL", "local|provider"):
            with self.subTest(origin=origin):
                self.assertEqual(
                    self._refusal_through_shipped_seam(body, origin=origin),
                    "  FAMILY-DARK codex: upstream RATE-LIMITED since "
                    "1970-01-01T00:16:40Z via no representative — ")

    def test_provider_429_survives_the_identical_shipped_seam(self):
        body = ('{"type":"error","error":{"type":"rate_limit_error",'
                '"message":"Rate limit exceeded for this organization"}}')
        self.assertIn("rate_limit_error", body,
                      "the positive control must carry provider evidence")
        self.assertNotIn("cooling down", body.lower())
        self.assertEqual(
            self._refusal_through_shipped_seam(
                body, trace="20260825195500-0123456789abcdef-deadbeef"),
            "  FAMILY-DARK codex: upstream RATE-LIMITED since "
            "1970-01-01T00:16:40Z via no representative — ")

    def test_real_request_is_eight_tokens_on_the_family_launch_model(self):
        from helm import seat
        seen = {}

        class Response:
            status = 200
            def read(self, _n=None):
                return (b'{"type":"message","role":"assistant",'
                        b'"content":[{"type":"text","text":"OK"}],'
                        b'"stop_reason":"end_turn"}')
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return False

        def open_(req, timeout=None):
            seen["req"], seen["timeout"] = req, timeout
            return Response()

        with mock.patch("helm.seat._seat_family", return_value=("codex", None)), \
                mock.patch("helm.pi.seat_port", return_value=(8317, None)), \
                mock.patch("helm.pi._pi_api_key",
                           return_value="secret-local-token"), \
                mock.patch("urllib.request.urlopen", side_effect=open_):
            state, detail, _ms = proxywatch._upstream_once("codex")
        body = json.loads(seen["req"].data)
        self.assertEqual(state, "HEALTHY")
        self.assertIn("with OK", detail)
        self.assertEqual(body["max_tokens"], proxywatch.UPSTREAM_TOKENS)
        self.assertEqual(body["model"], seat.FAMILIES["codex"]["model"])
        self.assertEqual(seen["timeout"], proxywatch.UPSTREAM_TIMEOUT_S)
        self.assertEqual(seen["req"].get_header("Authorization"),
                         "Bearer secret-local-token")
        self.assertIn(proxywatch._CANARY_QUERY, seen["req"].full_url)
        self.assertNotIn("secret-local-token", detail)

    def test_request_layer_returns_response_model_and_selected_auth_trace(self):
        trace = "20260805050000-0123456789abcdef-deadbeef"

        class Response:
            status = 200
            headers = {"X-CPA-TRACE-ID": trace}

            def read(self, _n=None):
                return (b'{"type":"message","role":"assistant",'
                        b'"model":"gpt-5.6-sol",'
                        b'"content":[{"type":"text","text":"OK"}],'
                        b'"stop_reason":"end_turn"}')

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        with mock.patch("urllib.request.urlopen", return_value=Response()):
            state, _detail, _ms, status, model, got_trace = \
                proxywatch._canary_once(
                    "http://127.0.0.1:8317", "secret", "gpt-5.6-sol")
        self.assertEqual((state, status), ("HEALTHY", 200))
        self.assertEqual(model, "gpt-5.6-sol")
        self.assertEqual(got_trace, trace)

    def test_reasoning_only_at_the_eight_token_cap_is_healthy(self):
        payload = (b'{"type":"message","role":"assistant",'
                   b'"content":[{"type":"thinking","thinking":"x"}],'
                   b'"stop_reason":"max_tokens"}')
        self.assertIn("thinking-only", proxywatch._valid_canary_payload(payload))

    def test_empty_reasoning_at_the_token_cap_is_not_completion_evidence(self):
        payload = (b'{"type":"message","role":"assistant",'
                   b'"content":[{"type":"thinking","thinking":""}],'
                   b'"stop_reason":"max_tokens"}')
        self.assertIsNone(proxywatch._valid_canary_payload(payload))
        numeric = (b'{"type":"message","role":"assistant",'
                   b'"content":[{"type":"thinking","thinking":17}],'
                   b'"stop_reason":"max_tokens"}')
        self.assertIsNone(proxywatch._valid_canary_payload(numeric))

    def test_the_LIVE_gemini_carrier_shape_is_accepted(self):
        """THE REGRESSION PIN, AND THE ONLY FIXTURE HERE TAKEN FROM PRODUCTION.

        Captured 2026-09-09 from the live gemini proxy through helm's own
        `_proxy_runtime_shape` route: stop_reason 'end_turn', content
        ['thinking', 'text'], the thinking block carrying an EMPTY text and a
        582-character string signature, and the text block carrying "OK".

        MEASURED DIFFERENTIAL on that exact body: trunk's `_valid_canary_payload`
        returns None (so helm calls a working provider MALFORMED200 and the
        whole family reads dark) and this lane's returns "with OK". That is the
        entire reason this lane exists, and every other arm in it is about what
        must still be REFUSED — so without this one, a future tightening can
        satisfy all of them and silently re-break gemini.

        Note WHICH branch accepts it: the FIRST, on `spoken == ["OK"]`. The carrier
        branch below is defensive coverage the live response never reaches,
        which is what makes narrowing that branch safe.
        """
        sig = "cpa-gemini-carrier-v1:next:text:" + ("A" * 550)
        payload = json.dumps({
            "id": "msg_live_shape", "type": "message", "role": "assistant",
            "model": "gemini-3.6-flash-high",
            "content": [{"type": "thinking", "thinking": "", "signature": sig},
                        {"type": "text", "text": "OK"}],
            "stop_reason": "end_turn", "stop_sequence": None,
        }).encode()
        self.assertEqual(proxywatch._valid_canary_payload(payload), "with OK",
                         "the live gemini carrier shape stopped being HEALTHY "
                         "— this is the outage this lane cures")

    def test_a_spoken_OK_is_healthy_whatever_stop_reason_says(self):
        """The with-OK branch does not gate on stop_reason, and that is a decision.

        A cross-family review of this lane asked for
        `stop_reason in _TERMINAL_STOP_REASONS` on the with-OK branch, to match
        the carrier branch, and claimed trunk refused OK under tool_use /
        pause_turn / 17 / '' / None / unknown. MEASURED against origin/main
        14138518b: trunk returns "with OK" for every one of those nine values,
        so the branch was never gated on stop_reason and this lane changes
        nothing there.

        Why it stays ungated: a spoken OK is the completion evidence. The
        carrier branch needs `complete` because a thinking-only envelope has
        no other sign the turn ended; a text block that says OK does. And the
        openai-compat translator emits stop_reason null when the upstream omits
        finish_reason, and tool_use with zero tool blocks on a known upstream
        quirk (CLIProxyAPI openai_claude_response.go:32/:422/:472) — a route
        that just answered OK would read MALFORMED200 under that tightening,
        which is the whitespace darkening this lane cures, in a new coat.
        Whoever tightens this must first show a live upstream that says OK
        while unavailable.
        """
        # unconditional positive control on the observable the loop walks:
        self.assertEqual(proxywatch._valid_canary_payload(json.dumps({
            "type": "message", "role": "assistant", "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "OK"}]}).encode()), "with OK")
        for stop in ("end_turn", "max_tokens", "tool_use", "pause_turn",
                     17, "", None, "unknown", "MISSING"):
            body = {"type": "message", "role": "assistant",
                    "content": [{"type": "text", "text": "OK"}],
                    "usage": {"output_tokens": 1}}
            if stop != "MISSING":
                body["stop_reason"] = stop
            self.assertEqual(
                proxywatch._valid_canary_payload(json.dumps(body).encode()),
                "with OK", "stop_reason=%r darkened a spoken OK" % (stop,))
        # and the tool_use block itself (not the stop reason) is what refuses:
        body = {"type": "message", "role": "assistant", "stop_reason": "tool_use",
                "content": [{"type": "text", "text": "OK"},
                            {"type": "tool_use", "id": "t", "name": "x",
                             "input": {}}]}
        self.assertIsNone(proxywatch._valid_canary_payload(
            json.dumps(body).encode()))

    def test_a_thinking_only_carrier_needs_a_COMPLETED_stop_reason(self):
        """FIX finding 3: shape without completion semantics.

        The carrier branch accepted ANY thinking-only envelope carrying a
        signature, including one whose turn is not finished and one with no
        stop_reason at all. The must-hit is asserted FIRST so the two refusals
        below are attributable to stop_reason and not to the fixture simply
        being unacceptable.
        """
        def envelope(**extra):
            body = {"type": "message", "role": "assistant",
                    "content": [{"type": "thinking", "thinking": "",
                                 "signature": "cpa-carrier-v1:opaque"}]}
            body.update(extra)
            return json.dumps(body).encode()

        self.assertEqual(
            proxywatch._valid_canary_payload(envelope(stop_reason="end_turn")),
            "with valid thinking (carrier or opaque)",
            "MUST-HIT: a completed thinking-only carrier is still accepted, so "
            "the refusals below are about completion and not about the shape")

        self.assertIsNone(
            proxywatch._valid_canary_payload(envelope()),
            "a carrier with NO stop_reason read HEALTHY — an unfinished or "
            "truncated answer cannot certify an upstream")
        self.assertIsNone(
            proxywatch._valid_canary_payload(envelope(stop_reason="tool_use")),
            "a carrier whose turn is still OPEN (tool_use) read HEALTHY")
        self.assertIsNone(
            proxywatch._valid_canary_payload(envelope(stop_reason="")),
            "an empty stop_reason read HEALTHY")
        self.assertIsNone(
            proxywatch._valid_canary_payload(envelope(stop_reason=17)),
            "a non-string stop_reason read HEALTHY")
        # The SECOND round: a DENY-LIST on an open vocabulary is a promise
        # about strings nobody has written yet. These two passed `!= tool_use`.
        self.assertIsNone(
            proxywatch._valid_canary_payload(envelope(stop_reason="pause_turn")),
            "pause_turn read HEALTHY — the turn is explicitly NOT finished")
        self.assertIsNone(
            proxywatch._valid_canary_payload(
                envelope(stop_reason="a_reason_this_file_has_never_heard_of")),
            "an UNKNOWN stop reason read HEALTHY, so any future non-terminal "
            "reason certifies an upstream the day it is invented")
        # EVERY TERMINAL REASON IS ACCEPTED. Asserted as ACCEPTANCE, not as a
        # particular sentence: `max_tokens` is caught by the earlier
        # eight-token-cap branch and answers with ITS wording, which is equally
        # correct. The first cut of this loop pinned one string and went red on
        # working code — asserting the RENDERING while meaning the STATE, which
        # is the same slip this lane's sibling arm exists to prevent.
        for terminal in proxywatch._TERMINAL_STOP_REASONS:
            self.assertIsNotNone(
                proxywatch._valid_canary_payload(envelope(stop_reason=terminal)),
                "the allowlist rejects %r, which it names as terminal" % terminal)

    def test_every_canary_read_ASKS_FOR_THE_BOUND(self):
        """The SECOND round, and the sharper of the two findings: the
        oversize arms could not see the cure they were written for.

        Their fakes ignored the `n` argument, so production reverting
        `read(CANARY_MAX_BODY + 1)` to an unbounded `read()` returned the same
        oversize blob, produced the same OVERSIZE verdict, and kept every one
        of them green. The BOUND is the cure, so the BOUND is what must be
        asserted — the state cannot discriminate here, because an unbounded
        read of an oversize body is oversize too.

        All three read sites at once: the probe, the canary 200 path, and the
        canary HTTPError path.
        """
        cap = proxywatch.CANARY_MAX_BODY

        class Recording:
            """A body that records the bound it was ASKED for."""

            status = 200
            code = 429
            headers = {}

            def __init__(self, blob):
                self.blob = blob
                self.reads = []

            def read(self, n=None):
                self.reads.append(n)
                return self.blob if n is None else self.blob[:n]

            def close(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

        # (1) probe
        body = Recording(b"x" * (cap + 10))
        with mock.patch("urllib.request.urlopen", return_value=body):
            state, _d, _ms = proxywatch.probe(8317)
        self.assertEqual(state, "OVERSIZE")
        self.assertEqual(body.reads, [cap + 1],
                         "probe() did not ask for the bound: %r" % body.reads)

        # (2) canary, 200 path
        body = Recording(b"y" * (cap + 10))
        with mock.patch("urllib.request.urlopen", return_value=body):
            state, _d, _ms, _c, _m, _t = proxywatch._canary_once(
                "http://127.0.0.1:8317", "tok", "model")
        self.assertEqual(state, "OVERSIZE")
        self.assertEqual(body.reads, [cap + 1],
                         "_canary_once did not ask for the bound on the 200 "
                         "path: %r" % body.reads)

        # (3) canary, HTTPError path
        import urllib.error
        err_body = Recording(b"e" * (cap + 10))

        def refuse(_req, timeout=None):
            raise urllib.error.HTTPError("u", 429, "rate", {}, err_body)

        with mock.patch("urllib.request.urlopen", side_effect=refuse):
            state, _d, _ms, code, _m, _t = proxywatch._canary_once(
                "http://127.0.0.1:8317", "tok", "model")
        self.assertEqual(state, "OVERSIZE")
        self.assertEqual(code, 429)
        self.assertEqual(err_body.reads, [cap + 1],
                         "the HTTPError path did not ask for the bound: %r"
                         % err_body.reads)

    def test_carrier_signature_must_be_nonempty_string(self):
        # Non-string or falsy/empty sig must not be treated as carrier.
        base = (b'{"type":"message","role":"assistant",'
                b'"content":[{"type":"thinking","thinking":""')
        for bad_sig in (b',"signature":true}',
                        b',"signature":1}',
                        b',"signature":[]}',
                        b',"signature":{}}',
                        b',"signature":""}',
                        b',"signature":"   "}'):
            payload = base + bad_sig + b']}'
            self.assertIsNone(proxywatch._valid_canary_payload(payload),
                              "bad sig %s accepted" % bad_sig)
        # Valid non-empty str sig with empty thinking text and no substantive
        # text must be accepted as carrier (even with an empty text block).
        #
        # STOP_REASON ADDED to this fixture when FIX finding 3 landed:
        # it had none, so as written it asserted that a carrier with NO
        # completion signal reads HEALTHY — which is the defect, not the
        # behaviour. This arm's subject is SIGNATURE TYPING, so it keeps its
        # subject and stops doubling as a claim about completion semantics;
        # that claim now has its own arm above, with both refusals AND a
        # must-hit so the refusals are attributable.
        ok_sig = (b'{"type":"message","role":"assistant",'
                  b'"content":[{"type":"thinking","thinking":"","signature":"cpa-xyz"},'
                  b'{"type":"text","text":""}],"stop_reason":"end_turn"}')
        self.assertIn("carrier or opaque",
                      proxywatch._valid_canary_payload(ok_sig))
        # Plain thinking-only (no sig) with non-max_tokens reason must still fail.
        plain_end = (b'{"type":"message","role":"assistant",'
                     b'"content":[{"type":"thinking","thinking":"foo"}],'
                     b'"stop_reason":"end_turn"}')
        self.assertIsNone(proxywatch._valid_canary_payload(plain_end))
        plain_absent = (b'{"type":"message","role":"assistant",'
                        b'"content":[{"type":"thinking","thinking":"foo"}]}')
        self.assertIsNone(proxywatch._valid_canary_payload(plain_absent))

    def test_oversize_body_on_canary_read_returns_OVERSIZE_not_MALFORMED(self):
        # Bounded read + explicit oversize verdict on all network paths.
        cap = proxywatch.CANARY_MAX_BODY
        huge = b"x" * (cap + 100)

        class OversizeResponse:
            status = 200
            headers = {}

            def read(self, n=None):
                # When called with cap+1, return more than cap.
                return huge

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        with mock.patch("urllib.request.urlopen", return_value=OversizeResponse()):
            state, detail, _ms, status, _model, _trace = \
                proxywatch._canary_once(
                    "http://127.0.0.1:8317", "secret", "gpt-5.6-sol")
        self.assertEqual(state, "OVERSIZE")
        self.assertIn("exceeded canary cap", detail)
        self.assertEqual(status, 200)

    def test_oversize_on_HTTPError_body_in_canary_is_OVERSIZE(self):
        import io
        import urllib.error
        cap = proxywatch.CANARY_MAX_BODY
        huge = b"e" * (cap + 50)
        def refuse(_req, timeout=None):
            raise urllib.error.HTTPError(
                "u", 429, "rate", {}, io.BytesIO(huge))
        with mock.patch("urllib.request.urlopen", side_effect=refuse):
            state, detail, _ms, code, _m, _t = proxywatch._canary_once(
                "http://127.0.0.1:8317", "secret", "model")
        self.assertEqual(state, "OVERSIZE")
        self.assertIn("exceeded canary cap", detail)
        self.assertEqual(code, 429)

    def test_arbitrary_nonempty_200_is_MALFORMED_not_healthy(self):
        self.assertIsNone(proxywatch._valid_canary_payload(
            b'{"error":{"message":"no completion"}}'))
        self.assertIsNone(proxywatch._valid_canary_payload(
            b'{"type":"message","role":"assistant",'
            b'"content":[{"type":"text","text":"OK"}],'
            b'"error":{"type":"overloaded_error"}}'))
        self.assertIsNone(proxywatch._valid_canary_payload(b'<html>ok</html>'))
        self.assertIsNone(proxywatch._valid_canary_payload(
            b'{"type":"message","role":"assistant",'
            b'"content":[{"type":"text","text":"NOT OK"}]}'))
        # " OK " with whitespace IS the answer (deepseek-v4-flash measured
        # " OK"; the exact match darkened a healthy family), so the arbitrary
        # reply this arm guards against is a different word, not padding.
        self.assertIsNone(proxywatch._valid_canary_payload(
            b'{"type":"message","role":"assistant",'
            b'"content":[{"type":"text","text":"Okay!"}]}'))
        self.assertIsNone(proxywatch._valid_canary_payload(
            b'{"type":"message","role":"assistant",'
            b'"content":[{"type":"text","text":"OK OK"}]}'))
        self.assertIsNone(proxywatch._valid_canary_payload(
            b'{"type":"message","role":"assistant","content":['
            b'{"type":"thinking"},{"type":"text","text":"OK"}]}'))
        self.assertIsNone(proxywatch._valid_canary_payload(
            b'{"type":"message","role":"assistant","content":['
            b'{"type":"thinking","thinking":17},'
            b'{"type":"text","text":"OK"}]}'))
        self.assertIsNone(proxywatch._valid_canary_payload(
            b'{"type":"message","role":"user",'
            b'"content":[{"type":"text","text":"OK"}]}'))
        self.assertIsNone(proxywatch._valid_canary_payload(
            b'{"type":"message","role":"assistant","content":['
            b'{"type":"text","text":"OK"},'
            b'{"type":"text","text":"but not exactly"}]}'))

    def test_empty_and_malformed_HTTP_200_are_named_by_the_request_layer(self):
        class Response:
            status = 200

            def __init__(self, payload):
                self.payload = payload

            def read(self, _n=None):
                return self.payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        cases = ((b"", "EMPTY200"),
                 (b'{"error":{"message":"no completion"}}', "MALFORMED200"))
        for payload, expected in cases:
            with self.subTest(expected=expected), \
                    mock.patch("helm.seat._seat_family",
                               return_value=("codex", None)), \
                    mock.patch("helm.pi.seat_port", return_value=(8317, None)), \
                    mock.patch("helm.pi._pi_api_key", return_value="secret"), \
                    mock.patch("urllib.request.urlopen",
                               return_value=Response(payload)):
                state, _detail, _ms = proxywatch._upstream_once("codex")
            self.assertEqual(state, expected)


class UpstreamFamilyHealthTest(unittest.TestCase):
    @staticmethod
    def rows():
        return [row(seat="seat-a", family="codex", probe="healthy"),
                row(seat="seat-b", family="codex", probe="healthy")]

    def reduce(self, outcomes, rows=None, prior=None, now=1000,
               birth="birth-a", identity_state="VERIFIED"):
        def observe(name, family):
            return outcomes[name], birth, identity_state

        with mock.patch.object(proxywatch, "_seat_canary_observation",
                               side_effect=observe) as canary:
            got = proxywatch.upstream_health(rows or self.rows(), now=now,
                                             prior=prior or {})
        return got, canary

    def test_every_viable_seat_is_measured_and_members_is_derived(self):
        rows = list(reversed(self.rows()))
        outcomes = {"seat-a": ("HEALTHY", "current", 7),
                    "seat-b": ("HEALTHY", "current", 5)}
        got, canary = self.reduce(outcomes, rows=rows)
        self.assertEqual({call.args for call in canary.call_args_list},
                         {("seat-a", "codex"), ("seat-b", "codex")})
        family = got["codex"]
        self.assertEqual(family["members"],
                         {name: record["state"]
                          for name, record in family["seats"].items()})
        self.assertEqual(family["ms"], 12)

    def test_seat_canaries_execute_concurrently(self):
        barrier = threading.Barrier(2)

        def observe(_name, _family):
            barrier.wait(timeout=2)
            return ("HEALTHY", "current", 1), "birth", "VERIFIED"

        rows = [row(seat="codex", family="codex", probe="healthy"),
                row(seat="kimi", family="kimi", probe="healthy")]
        with mock.patch.object(proxywatch, "_seat_canary_observation",
                               side_effect=observe) as probe:
            got = proxywatch.upstream_health(rows, now=1000, prior={})
        self.assertEqual({call.args[0] for call in probe.call_args_list},
                         {"codex", "kimi"})
        self.assertEqual(got["codex"]["state"], "HEALTHY")
        self.assertEqual(got["kimi"]["state"], "HEALTHY")

    def test_per_seat_worker_pool_is_bounded_at_eight(self):
        from concurrent.futures import ThreadPoolExecutor
        workers = []

        def executor(max_workers):
            workers.append(max_workers)
            return ThreadPoolExecutor(max_workers=max_workers)

        rows = [row(seat="codex-%d" % n, family="codex", probe="healthy")
                for n in range(9)]
        with mock.patch("concurrent.futures.ThreadPoolExecutor",
                        side_effect=executor), \
                mock.patch.object(proxywatch, "_seat_canary_observation",
                                  return_value=(("HEALTHY", "ok", 1),
                                                "birth", "VERIFIED")) as canary:
            got = proxywatch.upstream_health(rows, now=1000, prior={})
        self.assertEqual(workers, [8])
        self.assertEqual(canary.call_count, 9)
        self.assertEqual(len(got["codex"]["seats"]), 9)

    def test_one_cooldown_and_one_healthy_stays_split_visible_and_never_pauses(self):
        outcomes = {"seat-a": (proxywatch._PROXY_COOLDOWN, "local", 2),
                    "seat-b": ("HEALTHY", "ok", 3)}
        family = self.reduce(outcomes)[0]["codex"]
        self.assertEqual(family["state"], "UNKNOWN")
        self.assertFalse(family["dark"])
        self.assertFalse(proxywatch.beacon_paused(family))
        self.assertEqual(family["members"], {
            "seat-a": proxywatch._PROXY_COOLDOWN, "seat-b": "HEALTHY"})
        self.assertEqual(proxywatch.upstream_seat_sample(
            {"codex": family}, "codex", "seat-a")["state"],
            proxywatch._PROXY_COOLDOWN)
        self.assertEqual(proxywatch.upstream_seat_sample(
            {"codex": family}, "codex", "seat-b")["state"], "HEALTHY")

    def test_all_dark_differing_causes_is_display_mixed_but_never_actuates(self):  # noqa: VACUOUS_ASSERTION — state==FAMILY-MIXED and dark is True are the positive aggregate controls; the not-in/not-paused assertions pin non-actuation
        family = self.reduce({
            "seat-a": ("AUTH-401", "bad", 2),
            "seat-b": ("QUOTA-402", "quota", 3)})[0]["codex"]
        self.assertEqual(family["state"], "FAMILY-MIXED")
        self.assertTrue(family["dark"])
        self.assertNotIn("FAMILY-MIXED", proxywatch._UPSTREAM_DARK)
        self.assertFalse(proxywatch.beacon_paused(family))

    def test_unanimous_dark_keeps_the_named_cause(self):
        outcome = ("AUTH-UNAVAILABLE", "no auth", 2)
        family = self.reduce({"seat-a": outcome, "seat-b": outcome})[0]["codex"]
        self.assertEqual(family["state"], "AUTH-UNAVAILABLE")
        self.assertTrue(family["dark"])

    def test_family_quota_record_carries_only_canonical_reset_vocabulary(self):
        detail = "HTTP 402 — usage balance exhausted; resets in 1h"
        outcome = (proxywatch._QUOTA_WALL, detail, 2)
        family = self.reduce({"seat-a": outcome, "seat-b": outcome})[0]["codex"]
        self.assertEqual(family["resets_at_ms"], 4600000)
        self.assertEqual(family["reset_kind"], "vendor")
        self.assertEqual(family["reset_source"], "canary")
        for legacy in ("expires_at", "expires_kind", "expires_source"):
            self.assertNotIn(legacy, family)

    def test_one_unknown_member_reset_keeps_the_family_reset_unknown(self):  # noqa: VACUOUS_ASSERTION — family QUOTA-WALL is the positive control; reset absence is the conservative aggregate contract when one member's horizon is unknown
        family = self.reduce({
            "seat-a": (proxywatch._QUOTA_WALL,
                       "HTTP 402 — usage balance exhausted; resets in 1h", 2),
            "seat-b": (proxywatch._QUOTA_WALL,
                       "HTTP 402 — usage balance exhausted", 2)})[0]["codex"]
        self.assertEqual(family["state"], proxywatch._QUOTA_WALL)
        self.assertNotIn("resets_at_ms", family)
        self.assertNotIn("reset_kind", family)
        self.assertNotIn("reset_source", family)

    def test_no_locally_healthy_proxy_spends_no_canary_but_keeps_seat_unknown(self):  # noqa: VACUOUS_ASSERTION — UNKNOWN seat/member plus dark=False are positive controls; assert_not_called pins zero canary spend
        rows = [row(seat="codex", family="codex", probe="down")]
        family, canary = self.reduce({}, rows=rows)
        canary.assert_not_called()
        self.assertEqual(family["codex"]["state"], "UNKNOWN")
        self.assertEqual(family["codex"]["members"], {"codex": "UNKNOWN"})
        self.assertFalse(family["codex"]["dark"])

    def test_current_detail_and_ms_never_come_from_prior_state(self):
        prior = {"upstream": {"codex": {"state": "HEALTHY",
                                          "since": "old-since", "dark": False,
                                          "detail": "stale", "ms": 9999}}}
        family = self.reduce({"seat-a": ("HEALTHY", "current", 7)},
                             rows=[self.rows()[0]], prior=prior)[0]["codex"]
        self.assertEqual(family["since"], "old-since")
        self.assertEqual(family["detail"], "seat-a=HEALTHY (current)")
        self.assertEqual(family["ms"], 7)

    def test_missing_legacy_dark_family_is_preserved_as_latched_UNKNOWN(self):
        prior = {"upstream": {"codex": {"state": "AUTH-401",
                                          "since": "episode-start", "dark": True}}}
        got = proxywatch.upstream_health([], now=1000, prior=prior)["codex"]
        self.assertEqual(got["state"], "UNKNOWN")
        self.assertEqual(got["since"], "episode-start")
        self.assertTrue(got["dark"])
        self.assertEqual(got["members"], {})
        report = rep(upstream={"codex": got})
        self.assertEqual(proxywatch.findings(report)[0][0], "FAMILY-DARK")


class PerSeatCooldownTest(unittest.TestCase):
    def cooldown(self, before=None, now=1000, birth="birth-a",
                 identity_state="VERIFIED", state=None):
        return proxywatch._compose_upstream_seat(
            "codex", (state or proxywatch._PROXY_COOLDOWN, "local", 1),
            before or {}, now, birth=birth, identity_state=identity_state)

    def test_identity_brackets_the_canary_and_restart_during_it_is_unverified(self):
        order = []

        def incarnation(_family, _seat):
            order.append("identity")
            return ("birth-a", "VERIFIED") if len(order) == 1 else \
                ("birth-b", "VERIFIED")

        def canary(_seat, family=None):
            order.append("canary")
            return proxywatch._PROXY_COOLDOWN, "local", 1

        with mock.patch.object(proxywatch, "_proxy_incarnation",
                               side_effect=incarnation), \
                mock.patch.object(proxywatch, "upstream_canary",
                                  side_effect=canary):
            result, birth, state = proxywatch._seat_canary_observation(
                "codex", "codex")
        self.assertEqual(order, ["identity", "canary", "identity"])
        self.assertEqual(result[0], proxywatch._PROXY_COOLDOWN)
        self.assertIsNone(birth)
        self.assertEqual(state, "CHANGED-DURING-CANARY")

    def test_unknown_preserves_clock_and_same_birth_returns_due_not_fresh(self):  # noqa: VACUOUS_ASSERTION — preserved observed_at and the later due=True are positive controls; absence of due on UNKNOWN is the must-miss
        first = self.cooldown(now=100)
        unknown = self.cooldown(before=first, now=500, state="UNKNOWN")
        self.assertNotIn("falsification_due", unknown)
        self.assertEqual(unknown["falsification_observed_at"], proxywatch._iso(100))
        resumed = self.cooldown(before=unknown, now=1000)
        self.assertEqual(resumed["falsification_age_s"], 900)
        self.assertIs(resumed["falsification_due"], True)

    def test_verified_restart_rearms_without_moving_dark_episode(self):  # noqa: VACUOUS_ASSERTION — unchanged episode, moved observation, and age==0 are positive controls; due=False pins rearm polarity
        first = self.cooldown(now=100)
        restarted = self.cooldown(before=first, now=1000, birth="birth-b")
        self.assertEqual(restarted["since"], first["since"])
        self.assertEqual(restarted["falsification_observed_at"],
                         proxywatch._iso(1000))
        self.assertEqual(restarted["falsification_age_s"], 0)
        self.assertIs(restarted["falsification_due"], False)

    def test_unreadable_identity_preserves_last_verified_birth(self):
        first = self.cooldown(now=100)
        unreadable = self.cooldown(before=first, now=1000, birth=None,
                                   identity_state="UNREADABLE")
        self.assertEqual(unreadable["falsification_proxy_identity"], "birth-a")
        self.assertEqual(unreadable["falsification_identity_state"], "UNREADABLE")
        self.assertIs(unreadable["falsification_due"], True)

    def test_no_record_at_bar_is_fail_closed_and_first_birth_rearms(self):  # noqa: VACUOUS_ASSERTION — age==bar and later age==0 are positive controls; both false assertions pin fail-closed then rearm polarity
        no_record = self.cooldown(now=100, birth=None, identity_state="NO-RECORD")
        aged = self.cooldown(before=no_record, now=1000, birth=None,
                             identity_state="NO-RECORD")
        self.assertEqual(aged["falsification_age_s"], 900)
        self.assertIs(aged["falsification_due"], False)
        verified = self.cooldown(before=aged, now=1001, birth="birth-a")
        self.assertEqual(verified["falsification_age_s"], 0)
        self.assertIs(verified["falsification_due"], False)

    def test_malformed_clock_or_representative_never_becomes_due(self):  # noqa: VACUOUS_ASSERTION — malformed age=None and verified fixture construction are positive controls; false due pins both refusals
        malformed = self.cooldown(before={
            "state": proxywatch._PROXY_COOLDOWN, "dark": True,
            "since": proxywatch._iso(100),
            "falsification_observed_at": "not-a-time",
            "falsification_proxy_identity": "birth-a"}, now=1000)
        self.assertIsNone(malformed["falsification_age_s"])
        self.assertIs(malformed["falsification_due"], False)
        missing = proxywatch._compose_upstream_seat(
            None, (proxywatch._PROXY_COOLDOWN, "local", 1), {}, 1000,
            birth="birth-a", identity_state="VERIFIED")
        self.assertIs(missing["falsification_due"], False)

    def test_rate_limited_and_unknown_never_expose_due_fields(self):  # noqa: VACUOUS_ASSERTION — each returned state is asserted below before the three intentional field absences
        prior = self.cooldown(now=100)
        for state in ("RATE-LIMITED", "UNKNOWN"):
            with self.subTest(state=state):
                got = self.cooldown(before=prior, now=1000, state=state)
                self.assertEqual(got["state"], state)
                self.assertNotIn("falsification_due", got)
                self.assertNotIn("falsification_age_s", got)
                self.assertNotIn("falsification_seat", got)


class RemediationWriterReaderFindingsSeamTest(unittest.TestCase):
    """One persisted seat row is the sole source of restart prose."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="helm-test-remediation-seam-")
        self._path = mock.patch.object(
            proxywatch, "_state_path", return_value=os.path.join(self.d, "state.json"))
        self._path.start()

    def tearDown(self):
        self._path.stop()
        shutil.rmtree(self.d, ignore_errors=True)

    @staticmethod
    def _family(due=True, age=900):
        record = proxywatch._compose_upstream_seat(
            "codex", (proxywatch._PROXY_COOLDOWN, "local cooldown", 1), {},
            100, birth="birth-a", identity_state="VERIFIED")
        record["falsification_due"] = due
        record["falsification_age_s"] = age
        return {"state": proxywatch._PROXY_COOLDOWN, "dark": True,
                "since": record["since"],
                "falsification_bar_s": 900,
                "seats": {"codex": record},
                "members": {"codex": proxywatch._PROXY_COOLDOWN}}

    def test_writer_reader_findings_uses_exact_seat_and_never_leaks_action(self):  # noqa: VACUOUS_ASSERTION — persisted due=True, HELPFUL, and a concrete action are unconditional controls before the two intentional finding absences
        report = rep(row(seat="codex", family="codex",
                         upstream=proxywatch._PROXY_COOLDOWN),
                     upstream={"codex": self._family()})
        self.assertTrue(proxywatch.record(report, prior_state={}))
        state, err = proxywatch._read_watch_state()
        self.assertIsNone(err)
        stored, stored_err = proxywatch.upstream_seat_record(
            state, "codex", "codex")
        self.assertIsNone(stored_err)
        self.assertIs(stored["falsification_due"], True)
        self.assertEqual(stored["falsification_bar_s"], 900)

        rem = seat.upstream_remediation(state, "codex", "codex")
        self.assertEqual(rem["restart"], seat.RESTART_HELPFUL)
        self.assertEqual(
            rem["action"], rem["evidence"] +
            ". PRESCRIBES: restart this exact proxy, then rerun helm proxywatch.")
        unsupported = dict(rem, action="UNSUPPORTED ACTION MUST NOT LEAK")
        rendered = []
        for remediation in (rem, unsupported):
            liv = {"state": "WALLED", "blocked_on":
                   "upstream PROXY-COOLDOWN since %s" % stored["since"],
                   "remediation": remediation}
            findings = proxywatch.findings(rep(row(
                seat="codex", family="codex", turn="hung",
                turn_evidence="semantic entry stale 57m", liveness=liv)))
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0][0], "WALLED")
            self.assertIn("helm proxywatch safely remeasures", findings[0][1])
            self.assertNotIn("PRESCRIBES: restart this exact proxy", findings[0][1])
            self.assertNotIn("UNSUPPORTED ACTION", findings[0][1])
            rendered.append(findings[0][1])
        self.assertEqual(rendered[0], rendered[1])

    def test_owner_accepts_only_positive_exact_integer_bar_at_inclusive_boundary(self):  # noqa: VACUOUS_ASSERTION — normalized absence, UNKNOWN, and action absence pin every hostile arm; below/exact controls pin boundary polarity
        for name, bar, age in (
                ("zero", 0, 0),
                ("negative", -1, 0),
                ("bool", True, 1),
                ("float", 900.0, 900),
                ("non-numeric", "900", 900)):
            family = self._family(age=age)
            family["falsification_bar_s"] = bar
            family["seats"]["codex"]["falsification_bar_s"] = 900
            state = {"upstream": {"codex": family}}
            normalized, err = proxywatch.upstream_seat_record(
                state, "codex", "codex")
            rem = seat.upstream_remediation(state, "codex", "codex")
            with self.subTest(name=name):
                self.assertIsNone(err)
                self.assertNotIn("falsification_bar_s", normalized)
                self.assertEqual(rem["restart"], seat.RESTART_UNKNOWN)
                self.assertIsNone(rem["action"])

        below = self._family(age=899)
        below["seats"]["codex"]["falsification_bar_s"] = 1
        normalized, err = proxywatch.upstream_seat_record(
            {"upstream": {"codex": below}}, "codex", "codex")
        self.assertIsNone(err)
        self.assertEqual(normalized["falsification_bar_s"], 900)
        self.assertEqual(seat.upstream_remediation(
            {"upstream": {"codex": below}}, "codex", "codex")["restart"],
            seat.RESTART_UNKNOWN)

        boundary = self._family(age=900)
        boundary["seats"]["codex"]["falsification_bar_s"] = 1
        normalized, err = proxywatch.upstream_seat_record(
            {"upstream": {"codex": boundary}}, "codex", "codex")
        self.assertIsNone(err)
        self.assertEqual(normalized["falsification_bar_s"], 900)
        rem = seat.upstream_remediation(
            {"upstream": {"codex": boundary}}, "codex", "codex")
        self.assertEqual(rem["restart"], seat.RESTART_HELPFUL)
        self.assertEqual(
            rem["action"], rem["evidence"] +
            ". PRESCRIBES: restart this exact proxy, then rerun helm proxywatch.")

    def test_nonexact_remediation_evidence_stays_unknown(self):  # noqa: VACUOUS_ASSERTION — the seven named nonempty fixtures and exact UNKNOWN list assert every hostile arm ran before each action absence
        local = self._family()
        provider = self._family()
        provider["seats"]["codex"].update(state="RATE-LIMITED", dark=True)
        mixed = self._family()
        mixed["state"] = "FAMILY-MIXED"
        mixed["seats"]["codex"].update(state="UNKNOWN", dark=True)
        legacy = dict(self._family())
        legacy.pop("seats")
        cross = self._family()
        cross["seats"] = {"seat-b": dict(
            local["seats"]["codex"], falsification_seat="seat-b")}
        states = []
        for name, family in (
                ("fresh", self._family(due=False, age=10)),
                ("missing", dict(self._family(), seats={})),
                ("malformed", self._family(age=True)),
                ("provider", provider),
                ("mixed", mixed),
                ("legacy-family-only", legacy),
                ("cross-seat", cross)):
            state = {"upstream": {"codex": family}}
            rem = seat.upstream_remediation(state, "codex", "codex")
            with self.subTest(name=name):
                self.assertEqual(rem["restart"], seat.RESTART_UNKNOWN)
                self.assertIsNone(rem["action"])
            states.append(rem["restart"])
        self.assertEqual(states, [seat.RESTART_UNKNOWN] * 7)


class FingerprintTest(unittest.TestCase):
    def test_the_same_health_fingerprints_the_same(self):
        self.assertEqual(proxywatch.fingerprint(rep(row())),
                         proxywatch.fingerprint(rep(row())))

    def test_AGE_ALONE_is_not_a_change(self):
        """THE LOAD-BEARING ONE. Transcript age moves every single pass by
        construction. Folding it into the fingerprint would make every pass a
        'change', every pass a message, and the whole watch noise inside a
        day."""
        self.assertEqual(proxywatch.fingerprint(rep(row(age=60))),
                         proxywatch.fingerprint(rep(row(age=99999))))

    def test_current_upstream_detail_ms_and_members_do_not_move_the_latch(self):
        a = {"codex": {"state": "HEALTHY", "dark": False,
                       "detail": "first", "ms": 2, "members": {"codex": "HEALTHY"}}}
        b = {"codex": {"state": "HEALTHY", "dark": False,
                       "detail": "second", "ms": 999,
                       "members": {"codex-2": "HEALTHY"}}}
        self.assertEqual(proxywatch.fingerprint(rep(row(), upstream=a)),
                         proxywatch.fingerprint(rep(row(), upstream=b)))

    def test_named_dark_causes_share_one_episode_bucket(self):
        a = {"codex": {"state": "AUTH-401", "dark": True}}
        b = {"codex": {"state": "FAMILY-MIXED", "dark": True}}
        healthy = {"codex": {"state": "HEALTHY", "dark": False}}
        self.assertEqual(proxywatch.fingerprint(rep(row(), upstream=a)),
                         proxywatch.fingerprint(rep(row(), upstream=b)))
        self.assertNotEqual(proxywatch.fingerprint(rep(row(), upstream=a)),
                            proxywatch.fingerprint(rep(row(), upstream=healthy)))

    def test_each_cooldown_seat_moves_fresh_to_due_once_but_age_stays_quiet(self):  # noqa: VACUOUS_ASSERTION — fresh!=due is the positive edge control; due==older pins age-only quietness
        def family(due, age):
            return {"codex": {
                "state": proxywatch._PROXY_COOLDOWN, "dark": True,
                "seats": {"codex": {
                    "state": proxywatch._PROXY_COOLDOWN, "dark": True,
                    "falsification_due": due,
                    "falsification_age_s": age}}}}

        fresh = proxywatch.fingerprint(rep(row(), upstream=family(False, 899)))
        due = proxywatch.fingerprint(rep(row(), upstream=family(True, 900)))
        older = proxywatch.fingerprint(rep(row(), upstream=family(True, 99999)))
        self.assertNotEqual(fresh, due)
        self.assertEqual(due, older)

    def test_the_affected_seat_identity_is_in_the_upstream_fingerprint(self):  # noqa: VACUOUS_ASSERTION — two nonempty reports differ only by canonical seat identity; assertNotEqual is the positive observable
        def family(name):
            return {"codex": {"state": "UNKNOWN", "dark": False,
                               "seats": {name: {
                                   "state": proxywatch._PROXY_COOLDOWN,
                                   "dark": True,
                                   "falsification_due": True}}}}

        self.assertNotEqual(
            proxywatch.fingerprint(rep(row(), upstream=family("codex"))),
            proxywatch.fingerprint(rep(row(), upstream=family("seat-b"))))

    def test_a_PROBE_STATE_CHANGE_moves_it(self):
        """Caught by a mutation that did not bite. Dropping probe state from
        the fingerprint left the detection intact and the ALERTING dead: a
        proxy going healthy -> down would be correctly detected, correctly
        turned into a finding, and never announced, because the state had not
        'moved'. A watch that sees a failure and stays quiet is worse than one
        that cannot see it, since its silence is trusted."""
        healthy = rep(dict(row(), probe="healthy"))
        for broken in ("down", "hang", "EMPTY200"):
            self.assertNotEqual(
                proxywatch.fingerprint(healthy),
                proxywatch.fingerprint(rep(dict(row(), probe=broken))),
                "%s must register as a change or nobody is ever told" % broken)

    def test_a_LOG_STATE_change_moves_it(self):
        """The CHANGED discipline for the starvation rung: ok→streak must post
        once, and streak→ok (recovery) once. Without this the two-day 402
        wall would be detected on every pass and announced on none."""
        base = proxywatch.fingerprint(rep(row(log="ok")))
        for other in ("streak", "unknown", "idle"):
            self.assertNotEqual(base,
                                proxywatch.fingerprint(rep(row(log=other))),
                                "log %s must register as a change" % other)

    def test_a_TURN_STATE_change_moves_it(self):  # noqa: VACUOUS_ASSERTION — assertNotEqual against a fixed base is the positive control; each turn value must MOVE the digest
        """The CHANGED discipline for the fused verdict: ok→hung must post
        once, and hung→ok (the resume worked) once. Without this the codex
        hang would be detected on every pass and announced on none — the
        exact silent-watch shape the probe-state mutation already taught."""
        base = proxywatch.fingerprint(rep(row(turn="ok")))
        for other in ("hung", "thinking", "hung-unknown", "starved", "fresh",
                      "idle"):
            self.assertNotEqual(base,
                                proxywatch.fingerprint(rep(row(turn=other))),
                                "turn %s must register as a change" % other)

    def test_config_hang_and_a_NEW_DROP_ALERT_each_move_it(self):
        base = proxywatch.fingerprint(rep(row()))
        self.assertNotEqual(base, proxywatch.fingerprint(rep(row(config_ok=False))))
        self.assertNotEqual(base, proxywatch.fingerprint(rep(row(hang=True))))
        self.assertNotEqual(base, proxywatch.fingerprint(rep(row(alerted=12345))),
                            "a new silent-drop alert is the signal the bench "
                            "lift said would put the bench back")


class ChangeLatchTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="helm-test-pw-")
        self._p = mock.patch.object(proxywatch, "_state_path",
                                    return_value=os.path.join(self.d, "s.json"))
        self._p.start()

    def tearDown(self):
        self._p.stop()
        shutil.rmtree(self.d, ignore_errors=True)

    def test_a_FIRST_run_on_a_healthy_fleet_stays_quiet(self):
        """Installing the timer must not announce itself. A watch whose first
        act is a message teaches its reader that its messages are routine."""
        moved, prev = proxywatch.changed(rep(row()))
        self.assertFalse(moved)
        self.assertIsNone(prev)

    def test_a_FIRST_run_WITH_a_finding_does_speak(self):
        moved, _ = proxywatch.changed(rep(row(config_ok=False, drift=["x"])))
        self.assertTrue(moved)

    def test_legacy_healthy_fingerprint_migrates_without_a_false_change(self):
        family = {"codex": {"state": "HEALTHY", "since": "now",
                            "dark": False, "detail": "ok", "ms": 2,
                            "seat": "codex", "members": {"codex": "HEALTHY"}}}
        report = rep(row(upstream="HEALTHY", upstream_since="now"),
                     upstream=family)
        legacy = {"fingerprint": proxywatch.fingerprint(
            report, include_upstream=False), "ts": report["ts"],
                  "pending_chat": [], "pending_ntfy": []}
        with open(proxywatch._state_path(), "w", encoding="utf-8") as f:
            json.dump(legacy, f)
        self.assertFalse(proxywatch.changed(report)[0])
        proxywatch.record(report, prior_state=legacy)
        with open(proxywatch._state_path(), encoding="utf-8") as f:
            self.assertIn("upstream", json.load(f))
        self.assertFalse(proxywatch.changed(report)[0])

    def test_an_UNCHANGED_state_stays_quiet_on_every_later_pass(self):
        r = rep(row(config_ok=False, drift=["x"]))
        proxywatch.record(r)
        for _ in range(3):
            moved, _ = proxywatch.changed(r)
            self.assertFalse(moved, "a standing problem must not re-announce "
                                    "every fifteen minutes")

    def test_RECOVERY_is_a_change_and_is_reported(self):
        """Returning to healthy is news too — otherwise the last thing anyone
        heard is the problem, forever."""
        proxywatch.record(rep(row(config_ok=False, drift=["x"])))
        moved, _ = proxywatch.changed(rep(row()))
        self.assertTrue(moved)

    def test_an_unwritable_state_file_never_blocks_the_watch(self):
        with mock.patch.object(proxywatch, "_state_path",
                               return_value="/nonexistent/x/s.json"):
            proxywatch.record(rep(row()))          # must not raise


class UpstreamTransitionTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="helm-test-pwup-")
        self._p = mock.patch.object(proxywatch, "_state_path",
                                    return_value=os.path.join(self.d, "s.json"))
        self._p.start()

    def tearDown(self):
        self._p.stop()
        shutil.rmtree(self.d, ignore_errors=True)

    @staticmethod
    def report(state, since="2026-07-31T00:00:00Z", dark=None):
        dark = state in proxywatch._UPSTREAM_DARK if dark is None else dark
        family = {"state": state, "since": since, "dark": dark,
                  "detail": "current evidence", "ms": 7, "seat": "codex",
                  "members": {"codex": state}}
        return rep(row(upstream=state, upstream_since=since,
                       upstream_detail="current evidence", upstream_ms=7),
                   upstream={"codex": family})

    def test_dark_alerts_once_recovery_rearms_and_alerts_once(self):
        dark = self.report("UPSTREAM-OVERLOADED")
        self.assertEqual([x["kind"] for x in proxywatch.upstream_transitions(dark)],
                         ["family-dark"])
        proxywatch.record(dark)
        self.assertEqual(proxywatch.upstream_transitions(dark), [])
        recovered = self.report("HEALTHY", "2026-07-31T01:00:00Z", dark=False)
        self.assertEqual([x["kind"] for x in
                          proxywatch.upstream_transitions(recovered)],
                         ["family-recovered"])
        proxywatch.record(recovered)
        dark_again = self.report("AUTH-401", "2026-07-31T02:00:00Z")
        self.assertEqual([x["kind"] for x in
                          proxywatch.upstream_transitions(dark_again)],
                         ["family-dark"])

    def test_unknown_during_darkness_does_not_prove_recovery(self):
        proxywatch.record(self.report("AUTH-401"))
        unknown = self.report("UNKNOWN", dark=True)
        self.assertEqual(proxywatch.upstream_transitions(unknown), [])
        proxywatch.record(unknown)
        recovered = self.report("HEALTHY", "2026-07-31T01:00:00Z", dark=False)
        self.assertEqual([x["kind"] for x in
                          proxywatch.upstream_transitions(recovered)],
                         ["family-recovered"])

    def test_unknown_persists_the_last_named_dark_state_for_later_since_reset(self):
        proxywatch.record(self.report("AUTH-401", "episode-start"))
        unknown = self.report("UNKNOWN", "episode-start", dark=True)
        proxywatch.record(unknown)
        prior, err = proxywatch._read_watch_state()
        self.assertIsNone(err)
        self.assertEqual(prior["upstream"]["codex"]["state"], "UNKNOWN")
        self.assertEqual(prior["upstream"]["codex"]["last_dark_state"],
                         "AUTH-401")
        rows = [row(seat="codex", family="codex", probe="healthy")]
        with mock.patch.object(proxywatch, "upstream_canary",
                               return_value=("QUOTA-402", "quota", 2)):
            changed = proxywatch.upstream_health(rows, now=2000, prior=prior)
        self.assertEqual(changed["codex"]["since"], proxywatch._iso(2000))

    def test_record_persists_only_episode_memory_not_current_evidence(self):
        report = self.report("HEALTHY", dark=False)
        self.assertTrue(proxywatch.record(report))
        with open(proxywatch._state_path(), encoding="utf-8") as f:
            saved = json.load(f)["upstream"]["codex"]
        # The transition identity is episode memory like `since`, minted
        # fresh for a family with no prior record, so its VALUE is not fixed.
        transition = saved.pop("transition_id", None)
        self.assertTrue(isinstance(transition, str) and transition,
                        "the record carries no transition identity: %r"
                        % (transition,))
        self.assertEqual(saved, {"state": "HEALTHY",
                                 "since": "2026-07-31T00:00:00Z",
                                 "dark": False})

    def test_a_posting_pass_keeps_one_transition_identity_through_its_acknowledgement(self):
        """ONE PASS, ONE TRANSITION IDENTITY. The posting pass persists its
        outbox, delivers, and then writes again to acknowledge the delivery.
        On a pass that lifts a pause, both writes carry the same verdict, so
        the acknowledgement write keeps the identity the first write minted.
        A second identity reads, to every act that captured the first write,
        as a pause that changed while the act ran. Driven through the real
        `cmd_proxywatch --post` and the real `record`; the spy only reads the
        file each write left."""
        self.assertTrue(proxywatch.record(self.report("AUTH-401")))
        prior, err = proxywatch._read_watch_state()
        self.assertIsNone(err)
        dark_id = prior["upstream"]["codex"]["transition_id"]
        recovered = self.report("HEALTHY", "2026-07-31T01:00:00Z", dark=False)
        real_record = proxywatch.record
        writes = []

        def record(*args, **kwargs):
            written = real_record(*args, **kwargs)
            with open(proxywatch._state_path(), encoding="utf-8") as f:
                writes.append(json.load(f)["upstream"]["codex"])
            return written

        with mock.patch.object(proxywatch, "health", return_value=recovered), \
                mock.patch.object(proxywatch, "record", side_effect=record), \
                mock.patch.object(proxywatch, "_cred_follow_pass",
                                  return_value=None), \
                mock.patch.object(proxywatch, "_codex_budget_pass",
                                  return_value=None), \
                mock.patch.object(proxywatch, "read_vendor_resets",
                                  return_value=({}, None)), \
                mock.patch.object(proxywatch, "_owner_push",
                                  return_value=True) as push, \
                mock.patch("helm.chat.post"):
            proxywatch.cmd_proxywatch(["--post"])
        self.assertEqual([t["kind"] for t in push.call_args.args[0]],
                         ["family-recovered"],
                         "fixture: the pass did not lift the pause")
        self.assertEqual([(w["state"], w["dark"]) for w in writes],
                         [("HEALTHY", False), ("HEALTHY", False)],
                         "fixture: the pass did not persist and acknowledge")
        self.assertNotEqual(writes[0]["transition_id"], dark_id,
                            "fixture: the lifted pause minted no identity")
        self.assertEqual(
            writes[1]["transition_id"], writes[0]["transition_id"],
            "the acknowledgement write of the pass that lifted the pause "
            "minted a second transition identity: %r" % (writes,))

    def test_writer_reader_primary_last_good_and_ack_preserve_per_seat_evidence(self):
        fresh = proxywatch._compose_upstream_seat(
            "codex", (proxywatch._PROXY_COOLDOWN, "local", 1), {}, 100,
            birth="birth-a", identity_state="VERIFIED")
        due = proxywatch._compose_upstream_seat(
            "codex", (proxywatch._PROXY_COOLDOWN, "local", 1), fresh, 1000,
            birth="birth-a", identity_state="VERIFIED")
        family = {
            "state": proxywatch._PROXY_COOLDOWN,
            "since": due["since"], "dark": True,
            "falsification_bar_s": proxywatch.DARK_FALSIFICATION_S,
            "seats": {"codex": due},
            "members": {"codex": proxywatch._PROXY_COOLDOWN},
        }
        report = rep(row(seat="codex", upstream=proxywatch._PROXY_COOLDOWN),
                     upstream={"codex": family})
        self.assertTrue(proxywatch.record(report, pending_chat=["alert"]))
        with open(proxywatch._state_path(), encoding="utf-8") as f:
            initial = json.load(f)
        for path in (proxywatch._state_path(), proxywatch._backup_state_path()):
            with open(path, encoding="utf-8") as f:
                saved = json.load(f)
            got, err = proxywatch.upstream_seat_record(
                saved, "codex", "codex")
            self.assertIsNone(err)
            self.assertIs(got["falsification_due"], True)
            self.assertEqual(got["falsification_age_s"], 900)
            self.assertEqual(got["falsification_bar_s"],
                             proxywatch.DARK_FALSIFICATION_S)
            self.assertEqual(got["falsification_seat"], "codex")
            self.assertNotIn("falsification_bar_s",
                             saved["upstream"]["codex"]["seats"]["codex"])
            self.assertNotIn("members", saved["upstream"]["codex"])

        self.assertTrue(proxywatch.record(report, pending_chat=[],
                                          prior_state=initial))
        with open(proxywatch._state_path(), encoding="utf-8") as f:
            acked = json.load(f)
        got, err = proxywatch.upstream_seat_record(acked, "codex", "codex")
        self.assertIsNone(err)
        self.assertIs(got["falsification_due"], True)
        self.assertEqual(acked["pending_chat"], [])

    def test_one_family_dark_finding_is_emitted_for_multiple_instances(self):
        family = self.report("AUTH-401")["upstream"]
        report = rep(row(seat="codex", upstream="AUTH-401"),
                     row(seat="codex-2", upstream="AUTH-401"),
                     upstream=family)
        got = [finding for finding in proxywatch.findings(report)
               if finding[0] == "FAMILY-DARK"]
        self.assertEqual(len(got), 1)
        self.assertIn("AUTH-401", got[0][1])

    def test_named_dark_change_does_not_repeat_the_transition_or_fingerprint(self):
        first = self.report("AUTH-401")
        proxywatch.record(first)
        changed = self.report("QUOTA-402", "2026-07-31T00:10:00Z")
        self.assertEqual(proxywatch.upstream_transitions(changed), [])
        self.assertFalse(proxywatch.changed(changed)[0])

    def test_first_HEALTHY_is_a_baseline_not_a_recovery_transition(self):
        self.assertEqual(proxywatch.upstream_transitions(
            self.report("HEALTHY", dark=False)), [])

    def test_failed_phone_dark_then_recovery_delivers_both_without_chat_repeat(self):
        """The phone leg's at-least-once walk: pass 1's push fails and the
        dark edge stays queued; pass 2's push delivers the stuck dark AND the
        fresh recovery in one batch, while chat posts exactly once per edge."""
        dark = self.report("UPSTREAM-OVERLOADED")
        recovered = self.report("HEALTHY", "2026-07-31T01:00:00Z", dark=False)
        pushed = []

        def push(transitions):
            pushed.append(list(transitions))
            return len(pushed) > 1

        with mock.patch.object(proxywatch, "health",
                               side_effect=(dark, recovered)), \
                mock.patch.object(proxywatch, "_owner_push",
                                  side_effect=push), \
                mock.patch("helm.chat.post") as chat:
            self.assertEqual(proxywatch.cmd_proxywatch(["--post"]), 1)
            self.assertEqual(proxywatch.cmd_proxywatch(["--post"]), 0)
        self.assertEqual([t["kind"] for t in pushed[0]], ["family-dark"])
        self.assertEqual([t["kind"] for t in pushed[1]],
                         ["family-dark", "family-recovered"])
        self.assertEqual(chat.call_count, 2,
                         "dark and recovery each post once")
        with open(proxywatch._state_path(), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["pending_ntfy"], [])

    def test_owner_push_is_one_batch_and_opt_in(self):
        transitions = [{"kind": "family-dark", "family": "codex",
                        "state": "UPSTREAM-OVERLOADED"},
                       {"kind": "family-recovered", "family": "kimi",
                        "state": "HEALTHY"}]

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        with mock.patch.dict(os.environ, {"HELM_NTFY_TOPIC": "helm-fleet"}), \
                mock.patch("urllib.request.urlopen",
                           return_value=Response()) as open_:
            self.assertTrue(proxywatch._owner_push(transitions))
        self.assertEqual(open_.call_count, 1)
        req = open_.call_args[0][0]
        self.assertEqual(req.full_url, "https://ntfy.sh/helm-fleet")
        self.assertIn(b"codex dark", req.data)
        self.assertIn(b"kimi recovered", req.data)

    def test_owner_push_without_a_topic_is_a_deliberate_opt_out(self):  # noqa: VACUOUS_ASSERTION — assertTrue(returns True) is the positive claim; the not-called arm is the opt-out contract, and test_owner_push_is_one_batch_and_opt_in proves the same batch posts with a topic
        """No HELM_NTFY_TOPIC acknowledges the batch (True) without a network
        call — the positive-control twin above proves the same batch DOES post
        when the topic exists."""
        transitions = [{"kind": "family-dark", "family": "codex",
                        "state": "AUTH-401"}]
        with mock.patch.dict(os.environ, {"HELM_NTFY_TOPIC": "",
                                          "MELD_NTFY_TOPIC": ""}), \
                mock.patch("urllib.request.urlopen") as open_:
            self.assertTrue(proxywatch._owner_push(transitions))
        open_.assert_not_called()

    def test_owner_push_failure_returns_False_for_the_outbox_retry(self):  # noqa: VACUOUS_ASSERTION — assertFalse IS the observable under test (the outbox retry signal); the delivery positive control is test_owner_push_is_one_batch_and_opt_in
        transitions = [{"kind": "family-dark", "family": "codex",
                        "state": "AUTH-401"}]
        with mock.patch.dict(os.environ, {"HELM_NTFY_TOPIC": "helm-fleet"}), \
                mock.patch("urllib.request.urlopen",
                           side_effect=OSError("no route")):
            self.assertFalse(proxywatch._owner_push(transitions))

    def test_merge_transitions_deduplicates_a_stuck_edge_against_a_fresh_one(self):
        stuck = [{"kind": "family-dark", "family": "codex",
                  "state": "AUTH-401", "since": "t0"}]
        fresh = [{"kind": "family-dark", "family": "codex",
                  "state": "AUTH-401", "since": "t0"},
                 {"kind": "family-recovered", "family": "kimi",
                  "state": "HEALTHY", "since": "t1"}]
        merged = proxywatch._merge_transitions(stuck, fresh)
        self.assertEqual([(t["kind"], t["family"]) for t in merged],
                         [("family-dark", "codex"),
                          ("family-recovered", "kimi")])


class TimerUnitTest(unittest.TestCase):
    def test_default_cadence_is_the_installed_fifteen_minutes(self):
        """900s mirrors the INSTALLED helm-proxywatch.timer — installed is
        truth, the cadence is operational (#195, ruled 2026-08-04)."""
        self.assertEqual(proxywatch.INTERVAL_S, 900)
        _sp, _service, _tp, timer = proxywatch.timer_units()
        self.assertIn("OnUnitActiveSec=900s", timer)

    def test_template_cadence_equals_the_constant(self):
        """#195's guard: the SHIPPED template parses back to INTERVAL_S for
        BOTH cadence fields, so the next installed-truth change is ONE edit.
        Host state (~/.config) is deliberately out of scope — the suite pins
        only the internal template==constant agreement."""
        _sp, _service, _tp, timer = proxywatch.timer_units()
        self.assertIn("OnBootSec=%ds\n" % proxywatch.INTERVAL_S, timer)
        self.assertIn("OnUnitActiveSec=%ds\n" % proxywatch.INTERVAL_S, timer)
        # and these are the ONLY cadence fields — a stray hardcoded Sec=
        # line beside the parsed pair would be exactly the drift this pins.
        self.assertEqual(timer.count("Sec="), 2, timer)

    def test_the_unit_calls_the_POSTING_form(self):
        """A timer that ran the read-only form would compute the answer and
        tell nobody — the exact dead-scaffolding shape helm wiring exists to
        catch, arriving via systemd instead of via an import."""
        _sp, service, _tp, timer = proxywatch.timer_units(900)
        self.assertIn("proxywatch --post", service)
        self.assertIn("OnUnitActiveSec=900s", timer)

    def test_the_unit_uses_an_ABSOLUTE_helm_path(self):
        """A persistent unit must never capture a disposable worktree's PATH —
        seat.py's standing law, and the reason its own timer hardcodes the
        binary."""
        _sp, service, _tp, _t = proxywatch.timer_units()
        self.assertIn("/.local/bin/helm", service)

    def test_a_nonsense_interval_is_refused(self):
        ok, err = proxywatch.ensure_timer(0)
        self.assertFalse(ok)
        self.assertIn("at least 1 second", err)

    def test_found_faults_reads_as_SUCCESS_to_systemd(self):
        """Exit 1 (faults FOUND) and exit 2 (the watchdog broke) were one
        signal in systemctl — this unit read `failed (exit-code 1)` all
        through 2026-08-03 while it was the only correct instrument. The unit
        must declare 1 a success so red means broken, and nothing else."""
        _sp, service, _tp, _t = proxywatch.timer_units()
        self.assertIn("SuccessExitStatus=1", service)


class CmdTest(unittest.TestCase):
    def test_exit_code_reports_findings(self):
        with mock.patch.object(proxywatch, "health", return_value=rep(row())):
            self.assertEqual(proxywatch.cmd_proxywatch([]), 0)
        with mock.patch.object(proxywatch, "health",
                               return_value=rep(row(config_ok=False, drift=["x"]))):
            self.assertEqual(proxywatch.cmd_proxywatch([]), 1)

    def test_an_unknown_flag_is_named(self):
        self.assertNotEqual(proxywatch.cmd_proxywatch(["--pst"]), 0)

    def test_a_failing_chat_post_never_wedges_the_watch(self):
        """It runs under systemd every fifteen minutes. A raise here is a unit that
        fails forever and a signal nobody gets. Exit 2, not 1: a watch that
        cannot reach its own alert surface is a BROKEN WATCHDOG, not a finding
        — 1 is declared success in the unit (SuccessExitStatus=1), so
        returning it here would paint a mute watchdog green."""
        with mock.patch.object(proxywatch, "health",
                               return_value=rep(row(config_ok=False, drift=["x"]))), \
                mock.patch.object(proxywatch, "changed", return_value=(True, None)), \
                mock.patch.object(proxywatch, "record", return_value=True), \
                mock.patch("helm.chat.post", side_effect=RuntimeError("down")):
            self.assertEqual(proxywatch.cmd_proxywatch(["--post"]), 2)


class OutboxTest(unittest.TestCase):
    """The durable alert outbox — at-least-once delivery, fail-closed persist,
    independent channel acknowledgement."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._state = os.path.join(self._td.name, "proxywatch.json")
        p = mock.patch.object(proxywatch, "_state_path",
                              return_value=self._state)
        self._spp = p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self._td.cleanup)

    def _detected(self):
        """A report with a finding — the shape that triggers a post."""
        return rep(row(config_ok=False, drift=["missing-keepalive"]))

    def _saved(self):
        with open(self._state, encoding="utf-8") as f:
            return json.load(f)

    def test_record_persists_pending_chat_and_returns_true(self):
        r = rep(row())
        self.assertTrue(proxywatch.record(r, pending_chat=["msg1", "msg2"]))
        saved = self._saved()
        self.assertEqual(saved["pending_chat"], ["msg1", "msg2"])
        self.assertIn("fingerprint", saved)

    def test_record_returns_false_on_write_failure(self):
        r = rep(row())
        with mock.patch("helm.pk.atomic_write", side_effect=OSError("disk")):
            self.assertFalse(proxywatch.record(r, pending_chat=["msg"]))

    def test_read_watch_state_returns_empty_tuple_on_missing_file(self):
        state, err = proxywatch._read_watch_state()
        self.assertEqual(state, {})
        self.assertIsNone(err)

    def test_read_watch_state_returns_error_string_on_corrupt_file(self):
        with open(self._state, "w") as f:
            f.write("{corrupt json[")
        state, err = proxywatch._read_watch_state()
        self.assertEqual(state, {})
        self.assertIn("unreadable", err)

    def test_read_watch_state_returns_pending_queue_on_valid_file(self):
        proxywatch.record(rep(row()), pending_chat=["prior"])
        state, err = proxywatch._read_watch_state()
        self.assertIsNone(err)
        self.assertEqual(state["pending_chat"], ["prior"])

    def test_corrupt_outbox_refuses_delivery(self):  # noqa: VACUOUS_ASSERTION — rc==2 is the positive control; refusal-to-run IS the claim
        """A corrupt outbox must NOT deliver — the queue is unreadable and
        treating it as {} converts at-least-once into a loss. Return 2 (the
        watchdog itself failed — distinct from 1, faults FOUND) and name the
        file path."""
        with open(self._state, "w") as f:
            f.write("{corrupt json[")
        with mock.patch.object(proxywatch, "health",
                               return_value=self._detected()) as health, \
                mock.patch.object(proxywatch, "changed",
                                  return_value=(True, None)), \
                mock.patch("helm.chat.post") as post_mock:
            rc = proxywatch.cmd_proxywatch(["--post"])
            self.assertEqual(rc, 2)
            # noqa: VACUOUS_ASSERTION — the rc==2 above is the positive
            # control; the refusal-to-run IS the claim under test.
            health.assert_not_called()
            post_mock.assert_not_called()

    def test_post_lock_precedes_authenticated_health(self):
        order = []
        real_flock = proxywatch.fcntl.flock

        def flock(fd, op):
            if op == proxywatch.fcntl.LOCK_EX:
                order.append("lock")
            return real_flock(fd, op)

        def health(**_kwargs):
            order.append("health")
            return rep(row())

        with mock.patch.object(proxywatch.fcntl, "flock", side_effect=flock), \
                mock.patch.object(proxywatch, "health", side_effect=health), \
                mock.patch("helm.chat.post"):
            proxywatch.cmd_proxywatch(["--post"])
        self.assertEqual(order[:2], ["lock", "health"])

    def test_cmd_fails_closed_when_initial_persist_fails(self):  # noqa: VACUOUS_ASSERTION — rc==2 is the positive control; fail-closed means nothing delivered
        """A failed initial write must send nothing — the alert must not be
        delivered to a channel without durable record of the edge."""
        with mock.patch.object(proxywatch, "health",
                               return_value=self._detected()), \
                mock.patch.object(proxywatch, "changed",
                                  return_value=(True, None)), \
                mock.patch.object(proxywatch, "record", return_value=False), \
                mock.patch("helm.chat.post") as post_mock:
            rc = proxywatch.cmd_proxywatch(["--post"])
            self.assertEqual(rc, 2)
            # noqa: VACUOUS_ASSERTION — rc==2 is the positive control; the
            # fail-closed claim is exactly that nothing was delivered.
            post_mock.assert_not_called()

    def test_delivered_message_is_acked(self):
        with mock.patch.object(proxywatch, "health",
                               return_value=self._detected()), \
                mock.patch.object(proxywatch, "changed",
                                  return_value=(True, None)), \
                mock.patch("helm.chat.post"):
            proxywatch.cmd_proxywatch(["--post"])
            saved = self._saved()
            self.assertEqual(saved["pending_chat"], [])

    def test_failed_delivery_preserves_message_for_retry(self):
        with mock.patch.object(proxywatch, "health",
                               return_value=self._detected()), \
                mock.patch.object(proxywatch, "changed",
                                  return_value=(True, None)), \
                mock.patch("helm.chat.post",
                           side_effect=RuntimeError("down")):
            proxywatch.cmd_proxywatch(["--post"])
            saved = self._saved()
            self.assertEqual(len(saved["pending_chat"]), 1)
            self.assertIn("proxy health CHANGED", saved["pending_chat"][0])

    def test_force_does_not_invent_an_upstream_transition(self):
        with mock.patch.object(proxywatch, "health", return_value=rep(row())), \
                mock.patch.object(proxywatch, "upstream_transitions",
                                  return_value=[]) as transitions, \
                mock.patch("helm.chat.post") as chat:
            proxywatch.cmd_proxywatch(["--force"])
        transitions.assert_called_once()
        body = chat.call_args.args[0]
        self.assertNotIn("FAMILY-DARK", body)
        self.assertNotIn("FAMILY-RECOVERED", body)
        self.assertIn("no alerting state", body)
        self.assertNotIn("returned to healthy", body)

    def test_a_failing_owner_push_queues_only_the_ntfy_channel(self):
        """Channel independence: chat delivered and acked, the phone did not
        — the final persisted state must keep ONLY the ntfy batch."""
        family = {"codex": {"state": "UPSTREAM-OVERLOADED", "dark": True,
                            "since": "2026-07-31T00:00:00Z",
                            "detail": "HTTP 503", "ms": 7, "seat": "codex",
                            "members": {"codex": "UPSTREAM-OVERLOADED"}}}
        dark = rep(row(upstream="UPSTREAM-OVERLOADED",
                       upstream_since="2026-07-31T00:00:00Z",
                       upstream_detail="HTTP 503"), upstream=family)
        with mock.patch.object(proxywatch, "health", return_value=dark), \
                mock.patch.object(proxywatch, "changed",
                                  return_value=(True, None)), \
                mock.patch.object(proxywatch, "_owner_push",
                                  return_value=False), \
                mock.patch.object(proxywatch, "record",
                                  return_value=True) as record, \
                mock.patch("helm.chat.post"):
            self.assertEqual(proxywatch.cmd_proxywatch(["--post"]), 1)
        final = record.call_args.kwargs
        self.assertEqual(final["pending_chat"], [])
        self.assertEqual([t["kind"] for t in final["pending_ntfy"]],
                         ["family-dark"])

    def test_a_failing_chat_post_queues_only_the_chat_channel(self):
        """The mirror arm: the phone acked its (empty) batch while chat kept
        its body."""
        with mock.patch.object(proxywatch, "health",
                               return_value=self._detected()), \
                mock.patch.object(proxywatch, "changed",
                                  return_value=(True, None)), \
                mock.patch.object(proxywatch, "record",
                                  return_value=True) as record, \
                mock.patch("helm.chat.post",
                           side_effect=RuntimeError("down")):
            self.assertEqual(proxywatch.cmd_proxywatch(["--post"]), 2)
        final = record.call_args.kwargs
        self.assertEqual(len(final["pending_chat"]), 1)
        self.assertEqual(final["pending_ntfy"], [])

    def test_ack_write_failure_exits_nonzero_and_is_loud(self):
        """A failed ack must not silently drop the row. Return 2 (the watchdog
        itself failed) and print the warning so the operator and the timer
        unit both see it."""
        with mock.patch.object(proxywatch, "health",
                               return_value=self._detected()), \
                mock.patch.object(proxywatch, "changed",
                                  return_value=(True, None)), \
                mock.patch("helm.chat.post"), \
                mock.patch.object(proxywatch, "record",
                                  side_effect=[True, False]):
            rc = proxywatch.cmd_proxywatch(["--post"])
            self.assertEqual(rc, 2)
            # First record call persisted the outbox (returned True)
            # chat.post succeeded (mocked silently)
            # Second record call (ack) failed (returned False) -> rc=2

    def test_multiple_deliveries_ack_independently(self):
        """Three bodies in the queue: first succeeds, second fails, third never
        tried because the failed one blocks (ordered delivery is conservative
        but safer: a failed delivery preserves all remaining items)."""
        with mock.patch.object(proxywatch, "health",
                               return_value=rep(row())), \
                mock.patch.object(proxywatch, "changed",
                                  return_value=(True, None)):
            # seed the prior state with a stuck body from last pass
            proxywatch.record(rep(row()), pending_chat=["stale"])
            # now: changed=True adds a new body -> ["stale", "new"]
            post_responses = [None, RuntimeError("second fails")]
            with mock.patch("helm.chat.post",
                            side_effect=post_responses):
                proxywatch.cmd_proxywatch(["--post"])
            saved = self._saved()
            # "stale" was popped (delivered), then "new" failed
            # -> remaining = ["new"]
            self.assertEqual(len(saved["pending_chat"]), 1)
            self.assertIn("proxy health CHANGED", saved["pending_chat"][0])

    def test_stale_pending_item_is_delivered_on_next_pass(self):
        """When a prior pass died between persist and ack, the next pass
        delivers the stale item. At-least-once: a retry beats a lost alert."""
        proxywatch.record(rep(row()), pending_chat=["stale"])
        with mock.patch.object(proxywatch, "health",
                               return_value=rep(row())), \
                mock.patch.object(proxywatch, "changed",
                                  return_value=(False, None)), \
                mock.patch("helm.chat.post") as post_mock:
            proxywatch.cmd_proxywatch(["--post"])
            post_mock.assert_called_once_with("stale", who="proxywatch",
                                              room="helm")
        saved = self._saved()
        self.assertEqual(saved["pending_chat"], [])

    def test_first_run_without_findings_posts_nothing(self):
        """Installing the timer on a healthy fleet must not announce itself."""
        with mock.patch.object(proxywatch, "health",
                               return_value=rep(row())), \
                mock.patch("helm.chat.post") as post_mock:
            proxywatch.cmd_proxywatch(["--post"])
            post_mock.assert_not_called()
            saved = self._saved()
            self.assertEqual(saved["pending_chat"], [])

    def test_lifecycle_walk(self):
        """One alert through the full outbox cycle: persist -> deliver -> ack."""
        # Pass 1: detection fires, persisted, delivered, acked
        with mock.patch.object(proxywatch, "health",
                               return_value=self._detected()), \
                mock.patch.object(proxywatch, "changed",
                                  return_value=(True, None)), \
                mock.patch("helm.chat.post") as post_mock:
            rc = proxywatch.cmd_proxywatch(["--post"])
            self.assertEqual(rc, 1)
            post_mock.assert_called_once()
        saved = self._saved()
        self.assertEqual(saved["pending_chat"], [])
        # Pass 2: quiet — no new edges
        with mock.patch.object(proxywatch, "health",
                               return_value=self._detected()), \
                mock.patch.object(proxywatch, "changed",
                                  return_value=(False, None)), \
                mock.patch("helm.chat.post") as post_mock:
            rc = proxywatch.cmd_proxywatch(["--post"])
            self.assertEqual(rc, 1)
            post_mock.assert_not_called()
        saved = self._saved()
        self.assertEqual(saved["pending_chat"], [])

    def test_process_death_between_persist_and_send_is_recovered(self):
        """If the process dies after persist but before delivery, the next pass
        picks up the pending queue and retries."""
        proxywatch.record(rep(row()), pending_chat=["orphaned"])
        with mock.patch.object(proxywatch, "health",
                               return_value=rep(row())), \
                mock.patch.object(proxywatch, "changed",
                                  return_value=(False, None)), \
                mock.patch("helm.chat.post") as post_mock:
            proxywatch.cmd_proxywatch(["--post"])
            # "orphaned" was delivered even though nothing changed this pass
            post_mock.assert_called_once_with("orphaned", who="proxywatch",
                                              room="helm")
        saved = self._saved()
        self.assertEqual(saved["pending_chat"], [])

    def test_process_death_between_send_and_ack_is_retried(self):
        """If the process dies after sending but before ack-persist, the next
        pass sees the pending item still queued and retries (possibly duplicate
        — that is the at-least-once contract)."""
        proxywatch.record(rep(row()), pending_chat=["delivered-not-acked"])
        with mock.patch.object(proxywatch, "health",
                               return_value=rep(row())), \
                mock.patch.object(proxywatch, "changed",
                                  return_value=(False, None)), \
                mock.patch("helm.chat.post") as post_mock:
            proxywatch.cmd_proxywatch(["--post"])
        self.assertEqual(post_mock.call_count, 1)
        # The item was delivered; verify it was also acked this time
        saved = self._saved()
        self.assertEqual(saved["pending_chat"], [])


class VendorResetTest(unittest.TestCase):
    """task/45 — the owner-entered vendor-reset channel. A dark family with no
    recorded horizon reads as indefinitely broken; this is the door for the
    one person holding the provider's page. The provenance law underneath:
    the vendor reset and the measured proxy bench horizon are TWO clocks and
    never render as one."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self._cfg = os.path.join(self._td.name, "proxywatch-vendor-resets.json")
        p = mock.patch.object(proxywatch, "_vendor_resets_path",
                              return_value=self._cfg)
        p.start()
        self.addCleanup(p.stop)
        self._future = int((time.time() + 3600) * 1000)

    def test_set_show_clear_round_trips_with_provenance(self):
        rec, err = proxywatch.write_vendor_reset("codex", self._future)
        self.assertIsNone(err)
        self.assertEqual(rec["reset_kind"], "vendor")
        self.assertEqual(rec["reset_source"], "owner")
        self.assertTrue(rec.get("recorded_at"))
        table, err = proxywatch.read_vendor_resets()
        self.assertIsNone(err)
        self.assertEqual(table["codex"]["resets_at_ms"], self._future)
        removed, err = proxywatch.clear_vendor_reset("codex")
        self.assertTrue(removed)
        table, _ = proxywatch.read_vendor_resets()
        self.assertNotIn("codex", table)

    def test_a_past_horizon_is_refused_not_persisted(self):
        rec, err = proxywatch.write_vendor_reset("codex", 1000)
        self.assertIsNone(rec)
        self.assertIn("PAST", err)
        table, _ = proxywatch.read_vendor_resets()
        self.assertEqual(table, {})

    def test_a_malformed_config_is_a_diagnostic_never_a_silent_empty(self):
        with open(self._cfg, "w") as f:
            f.write("{corrupt[")
        table, err = proxywatch.read_vendor_resets()
        self.assertEqual(table, {})
        self.assertTrue(err, "corrupt config must answer with an error")
        # and a wrong-shaped file the same
        with open(self._cfg, "w") as f:
            f.write("[1,2,3]")
        table, err = proxywatch.read_vendor_resets()
        self.assertTrue(err)

    def test_a_proxywatch_pass_preserves_the_horizon(self):
        # THE PRODUCER ARM, driving the REAL composer the pass calls — never
        # a replicated copy (a copy can pass while the pass drops the field).
        proxywatch.write_vendor_reset("codex", self._future)
        rep = {"ts": "2026-08-06T08:00:00Z",
               "upstream": {"codex": {"state": "RATE-LIMITED", "dark": True,
                                      "since": "2026-08-06T07:00:00Z"}},
               "proxy_runtime": {}}
        upstream, verr = proxywatch._compose_upstream_records(rep, {})
        self.assertIsNone(verr)
        rec = upstream["codex"]
        self.assertEqual(rec["resets_at_ms"], self._future)
        self.assertEqual(rec["reset_kind"], "vendor")
        self.assertEqual(rec["reset_source"], "owner")
        # and the measured fields survive beside it — two clocks, one record
        self.assertEqual(rec["state"], "RATE-LIMITED")
        self.assertTrue(rec["dark"])
        # control on the same observable: with no owner input the axis is ABSENT
        proxywatch.clear_vendor_reset("codex")
        upstream, _ = proxywatch._compose_upstream_records(rep, {})
        self.assertNotIn("resets_at_ms", upstream["codex"],
                         "a family with no owner input must not gain one")

    def test_a_future_owner_horizon_covers_an_unknown_walled_member(self):  # noqa: VACUOUS_ASSERTION — the owner-filled reset is positively asserted on the same composed record before the no-owner absence
        """task/2935, the integrator's ruling: the family reset is UNKNOWN
        only when some walled member's reset is unknown AND no future owner
        horizon covers it. seat-b's refusal carried no reset; the owner's
        page value fills it, and the family opens at the earlier member."""
        now = time.time()
        later = int((now + 7200) * 1000)
        rep = {"ts": now, "upstream": {"codex": {
            "state": proxywatch._QUOTA_WALL, "dark": True, "since": "x",
            "seats": {
                "seat-a": {"state": proxywatch._QUOTA_WALL,
                           "quota_wall": proxywatch._QUOTA_WALL,
                           "wall_observed_at": proxywatch._iso(now),
                           "resets_at_ms": later, "reset_source": "canary"},
                "seat-b": {"state": proxywatch._QUOTA_WALL,
                           "quota_wall": proxywatch._QUOTA_WALL,
                           "wall_observed_at": proxywatch._iso(now)}}}},
               "proxy_runtime": {}}
        proxywatch.write_vendor_reset("codex", self._future)
        upstream, _ = proxywatch._compose_upstream_records(rep, {})
        self.assertEqual(upstream["codex"]["quota_wall"],
                         proxywatch._QUOTA_WALL)
        self.assertEqual((upstream["codex"]["resets_at_ms"],
                          upstream["codex"]["reset_source"]),
                         (self._future, "owner"))
        # CONTROL: the same members with no owner horizon: seat-b is
        # uncovered, so the family reset is UNKNOWN
        proxywatch.clear_vendor_reset("codex")
        upstream, _ = proxywatch._compose_upstream_records(rep, {})
        self.assertEqual(upstream["codex"]["quota_wall"],
                         proxywatch._QUOTA_WALL)
        self.assertNotIn("resets_at_ms", upstream["codex"])

    def test_owner_reset_beats_a_stale_measured_member_reset(self):
        proxywatch.write_vendor_reset("codex", self._future)
        stale = int((time.time() - 60) * 1000)
        rep = {"ts": time.time(), "upstream": {"codex": {
            "state": proxywatch._QUOTA_WALL, "dark": True, "since": "x",
            "seats": {"seat-a": {"state": proxywatch._QUOTA_WALL,
                                    "quota_wall": proxywatch._QUOTA_WALL,
                                    "resets_at_ms": stale}}}},
               "proxy_runtime": {}}
        upstream, _ = proxywatch._compose_upstream_records(rep, {})
        self.assertEqual(upstream["codex"]["resets_at_ms"], self._future)
        self.assertEqual(upstream["codex"]["reset_source"], "owner")

    def test_expired_auth_hold_is_not_resurrected_by_persistence(self):  # noqa: VACUOUS_ASSERTION — the same input record positively carries quota_wall and a reset before composition; both absences are the expiry contract
        past = int((time.time() - 60) * 1000)
        rep = {"ts": time.time(), "upstream": {"codex": {
            "state": "AUTH-UNAVAILABLE", "dark": True, "since": "x",
            "quota_wall": proxywatch._QUOTA_WALL,
            "resets_at_ms": past, "reset_source": "canary",
            "seats": {"seat-a": {"state": "AUTH-UNAVAILABLE",
                                    "quota_wall": proxywatch._QUOTA_WALL,
                                    "resets_at_ms": past}}}},
               "proxy_runtime": {}}
        upstream, _ = proxywatch._compose_upstream_records(rep, {})
        self.assertNotIn("quota_wall", upstream["codex"])
        self.assertNotIn("resets_at_ms", upstream["codex"])

    def test_a_past_horizon_expires_at_READ_not_at_render(self):
        # P1: T-1s was composed and the UI rendered "now" forever.
        # The read drops it; nothing downstream ever sees a past instant.
        past = int((time.time() - 5) * 1000)
        with open(self._cfg, "w") as f:
            json.dump({"codex": {"resets_at_ms": past, "reset_kind": "vendor",
                                 "reset_source": "owner",
                                 "recorded_at": "2026-08-06T07:00:00Z"}}, f)
        table, err = proxywatch.read_vendor_resets()
        self.assertIsNone(err)
        self.assertEqual(table, {}, "a past horizon must expire at read")
        # control: a future horizon in the SAME shape survives
        with open(self._cfg, "w") as f:
            json.dump({"codex": {"resets_at_ms": self._future,
                                 "reset_kind": "vendor",
                                 "reset_source": "owner",
                                 "recorded_at": "2026-08-06T07:00:00Z"}}, f)
        table, _ = proxywatch.read_vendor_resets()
        self.assertIn("codex", table)

    def test_a_corrupt_config_SURFACES_as_an_error_never_silent_absence(self):
        with open(self._cfg, "w") as f:
            f.write("{corrupt[")
        upstream, verr = proxywatch._compose_upstream_records(
            {"upstream": {"codex": {"state": "RATE-LIMITED", "dark": True,
                                    "since": "x"}}, "proxy_runtime": {}}, {})
        self.assertTrue(verr, "a corrupt config must surface, not swallow")
        self.assertNotIn("resets_at_ms", upstream["codex"])

    def test_set_and_clear_work_on_a_FRESH_home(self):
        # The r2 FIX, and the owner's first use of the verb:
        # _vendor_config_lock's os.open(O_CREAT) made the FILE but never the
        # parent DIRECTORY, so a fresh home raised FileNotFoundError on the
        # one command task/45 exists to give him. The class setUp pre-creates
        # the temp dir, which is why every r1 arm missed it — point the
        # config at a NOT-YET-EXISTING subdirectory to reproduce.
        fresh = os.path.join(self._td.name, "notyet", "deeper",
                             "proxywatch-vendor-resets.json")
        with mock.patch.object(proxywatch, "_vendor_resets_path",
                               return_value=fresh):
            rec, err = proxywatch.write_vendor_reset("codex", self._future)
            self.assertIsNone(err, err)
            self.assertEqual(rec["resets_at_ms"], self._future)
            table, rerr = proxywatch.read_vendor_resets()
            self.assertIn("codex", table)
            removed, cerr = proxywatch.clear_vendor_reset("codex")
            self.assertTrue(removed)
            table, _ = proxywatch.read_vendor_resets()
            self.assertNotIn("codex", table)

    def test_the_cli_verbs_are_REACHABLE(self):
        # P1, and the lesson of this lane: the helpers were driven
        # and the verb never was — guard_tail refused the whole subverb
        # family before the branch ran. Drive the actual entry point.
        import io
        import contextlib
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = proxywatch.cmd_proxywatch(["vendor-reset", "show"])
        self.assertEqual(rc, 0, err.getvalue())
        with contextlib.redirect_stderr(err):
            rc = proxywatch.cmd_proxywatch(["vendor-reset"])
        self.assertEqual(rc, 2)          # bare subverb: usage, reachable
        with contextlib.redirect_stderr(err):
            rc = proxywatch.cmd_proxywatch(["--post"])  # the flag path still routes
        self.assertIn(rc, (0, 1, 2))

    def test_a_stale_observer_never_gains_a_horizon_it_did_not_measure(self):  # noqa: VACUOUS_ASSERTION — the fresh-record block below is the unconditional positive control on the SAME observable (upstream_resets_at_ms annotates when fresh, with provenance), asserted before this arm's absence can pass vacuously
        # The horizon is an OWNER fact, but the ROSTER's stale rule is
        # untouched: a stale record withholds state AND horizon alike, because
        # a watcher that stopped writing cannot assert anything as "now".
        from helm import web_roster
        row = {"seat": "codex"}
        upstream = {"codex": {"state": "RATE-LIMITED", "dark": True,
                              "since": "2026-08-06T07:00:00Z",
                              "resets_at_ms": self._future,
                              "reset_kind": "vendor",
                              "reset_source": "owner"}}
        # stale: age past the freshness gate -> the horizon must NOT annotate
        with mock.patch.object(web_roster, "_UPSTREAM_STALE_S", 60):
            web_roster._annotate_upstream(
                row, upstream, 3600, None)
        self.assertNotIn("upstream_resets_at_ms", row,
                         "a stale record asserted a horizon")
        # the unconditional positive control on the SAME observable: a FRESH
        # record with the same horizon MUST annotate, or the absence above
        # proves nothing (the annotation path could be dead entirely).
        fresh = {"seat": "codex"}
        with mock.patch.object(web_roster, "_UPSTREAM_STALE_S", 60):
            web_roster._annotate_upstream(fresh, upstream, 30, None)
        self.assertEqual(fresh.get("upstream_resets_at_ms"), self._future,
                         "control: a fresh record annotates the horizon")
        self.assertEqual(fresh.get("upstream_reset_source"), "owner")


class WhoseFailureIsItTest(unittest.TestCase):
    """task/2092 — a dark state that is OURS must not be called upstream.

    Two surfaces describe one fact. `helm seat list` has always said "HELM'S
    OWN PROXY is in cooldown; no request reached a provider"; the FAMILY-DARK
    row said "upstream PROXY-COOLDOWN" about the same state. Both cannot be
    true, and the second is the sentence the fleet reads during a wall.
    """

    def test_dark_origin_separates_ours_from_untyped(self):  # noqa: VACUOUS_ASSERTION — every assertion here is an EQUALITY against a named answer, and the must-miss block asserts DARK_UNTYPED for ten states, which is itself a positive claim about what the classifier returns rather than an absence. The unconditional positive control is the first assertion: PROXY-COOLDOWN must equal DARK_OURS
        self.assertEqual(proxywatch.dark_origin("PROXY-COOLDOWN"),
                         proxywatch.DARK_OURS)
        for state in ("EMPTY200", "MALFORMED200"):
            self.assertEqual(proxywatch.dark_origin(state),
                             proxywatch.DARK_OUR_VALIDATION)
        # MUST-MISS, and it is the larger half: most dark states carry no
        # origin at all, and answering UNTYPED is the honest answer rather
        # than a gap to be filled in later.
        for state in ("AUTH-401", "QUOTA-402", "RATE-LIMITED", "TIMEOUT-500",
                      "UPSTREAM-4XX", "UPSTREAM-5XX", "AUTH-UNAVAILABLE",
                      "UPSTREAM-OVERLOADED", "UNKNOWN", "HEALTHY"):
            self.assertEqual(proxywatch.dark_origin(state),
                             proxywatch.DARK_UNTYPED, state)

    def test_every_dark_state_this_module_names_is_classified(self):
        """THE POPULATION IS THE MODULE'S OWN SET, not a list I typed.

        A dark state added to `_UPSTREAM_DARK` without a decision about whose
        failure it is falls through to UNTYPED, which is a safe default and a
        silent one. This arm does not forbid that — it requires the set to be
        reachable, so the next reader sees the classification exists and is
        answering about every member rather than about the three I thought of.
        """
        states = set(proxywatch._UPSTREAM_DARK)
        self.assertGreater(len(states), 5, states)
        answers = {proxywatch.dark_origin(s) for s in states}
        self.assertTrue(answers <= {proxywatch.DARK_OURS,
                                    proxywatch.DARK_OUR_VALIDATION,
                                    proxywatch.DARK_UNTYPED}, answers)
        # AND THE CLASSIFICATION IS NOT VACUOUS OVER THAT SET: at least one
        # member is ours. Without this the arm would pass on a classifier that
        # answered UNTYPED for everything.
        self.assertIn(proxywatch.DARK_OURS, answers)

    def test_the_two_surfaces_agree_about_whose_failure_it_is(self):  # noqa: VACUOUS_ASSERTION — the assertion is an EQUALITY between two computed booleans, so it fires in BOTH directions: a state classified OURS whose sentence omits the phrase fails, and a state classified UNTYPED whose sentence contains it fails too. Its unconditional positive control is the sibling test_every_dark_state_this_module_names_is_classified, which requires at least one member of the real set to be OURS — without that, this arm could pass over a classifier that answered UNTYPED for everything
        """THE SEAM, READ-ONLY. seat_usability writes the per-seat sentence and
        proxywatch writes the family row; they had grown separate copies of
        this classification and disagreed. This asserts the AGREEMENT rather
        than unifying the code, because the per-seat module is held by another
        lane — so the duplication is pinned instead of drifting silently, and
        whoever unifies it can delete this arm with the duplication.
        """
        from helm import seat_usability
        for state in sorted(proxywatch._UPSTREAM_DARK):
            sentence = seat_usability._dark_reason(state, "2026-09-11T00:00:00Z")
            origin = proxywatch.dark_origin(state)
            ours = origin == proxywatch.DARK_OURS
            self.assertEqual(
                ours, "HELM'S OWN PROXY" in sentence,
                "proxywatch.dark_origin(%r) and seat_usability's sentence "
                "disagree about whose failure it is: %s" % (state, sentence))
            # AND THE ABSENCE OF THE FALSE LEAD, WHICH IS THE HALF THAT
            # MATTERED. Asserting only the explanatory TAIL let a sentence
            # that LED with "upstream" and then contradicted itself read as
            # agreement — an arm named for agreement pinning a live
            # disagreement is worse than no arm, because the next reader
            # trusts it. A claim of origin belongs only to an UNTYPED state,
            # where it is the honest report that no origin was recorded.
            self.assertEqual(
                origin == proxywatch.DARK_UNTYPED,
                sentence.startswith("upstream "),
                "seat_usability leads with an ORIGIN CLAIM for a state "
                "classified %r: %s" % (origin, sentence))

    def _family_row(self, state):
        """The shipped FAMILY-DARK line for one family in one dark state."""
        rep = {"seats": [], "upstream": {"codex": {
            "state": state, "dark": True, "since": "2026-09-11T00:00:00Z",
            "seat": "codex", "detail": "codex=%s (evidence)" % state}}}
        rows = [line for kind, line in proxywatch.findings(rep)
                if kind == "FAMILY-DARK"]
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def test_naming_the_refusal_ours_never_says_the_provider_is_fine(self):
        """THE CORRECTION MUST NOT OVERSHOOT INTO THE OPPOSITE FALSEHOOD.

        A local cooldown is frequently the MIRROR of an upstream wall: the
        proxy cools a credential BECAUSE the provider refused it, so a family
        can sit in a purely local refusal whose cause is entirely the
        provider's. A row that says only "helm's own proxy is refusing"
        answers WHERE and is read as an answer to WHETHER — the same error
        this class exists to fix, with its sign flipped. The sentence must SAY
        that it measures nothing about the provider.
        """
        ours = self._family_row("PROXY-COOLDOWN")
        self.assertIn("HELM'S OWN PROXY", ours)
        self.assertIn("measures NOTHING about the provider", ours)
        self.assertIn("mirror of an upstream wall", ours)
        # MUST-MISS: an untyped state makes no such disclaimer, because it
        # made no origin claim to qualify.
        untyped = self._family_row("RATE-LIMITED")
        self.assertIn("upstream RATE-LIMITED", untyped)
        self.assertNotIn("measures NOTHING about the provider", untyped)

    def test_the_since_clause_stays_attached_to_the_state_in_every_branch(self):  # noqa: VACUOUS_ASSERTION — the first assertion in the body is the unconditional positive control: it renders the OURS branch and requires `PROXY-COOLDOWN since <ts>` adjacent, outside every loop and subTest, so an empty iteration cannot make this arm pass. The loop that follows widens the same observable to the validation and untyped branches
        """A DANGLING `since` IS A DIFFERENT SENTENCE, AND A FALSE ONE.

        With any full clause between the state and the timestamp the row reads
        "no request reached a provider since <timestamp>", which says the box
        sent nothing since then rather than that the state has held since
        then. The qualifier belongs AFTER the whole head for that reason, and
        this holds it for an OURS state, a validation state and an untyped
        one.
        """
        # THE UNCONDITIONAL POSITIVE CONTROL: the adjacency holds for the
        # branch this lane rewrote, asserted outside the loop so the arm
        # cannot pass by iterating over nothing.
        self.assertIn("PROXY-COOLDOWN since 2026-09-11T00:00:00Z",
                      self._family_row("PROXY-COOLDOWN"))
        for state in ("PROXY-COOLDOWN", "EMPTY200", "RATE-LIMITED"):
            with self.subTest(state=state):
                self.assertIn("%s since 2026-09-11T00:00:00Z" % state,
                              self._family_row(state))


if __name__ == "__main__":
    unittest.main()


class FanoutDemandReadingTest(unittest.TestCase):
    """The DEMAND term — see proxywatch.fanout_reading.

    Every arm here is written so that DELETING THE FEATURE REDDENS IT. That is
    not a style note: the night this landed, a reviewer removed a memo-hit
    return from a neighbouring lane and all four of its arms still passed. A
    suite that cannot fail retires a question without answering it.
    """

    def _seat(self, root, name, ages):
        """A seat instance dir holding one subagent transcript per age."""
        d = os.path.join(root, name, "claude", "projects", "proj", "sess",
                         "subagents")
        os.makedirs(d)
        now = time.time()
        for i, age in enumerate(ages):
            p = os.path.join(d, "agent-a%d.jsonl" % i)
            with open(p, "w") as fh:
                fh.write("{}\n")
            os.utime(p, (now - age, now - age))
        return os.path.join(root, name)

    def test_counts_only_subagents_inside_the_window(self):
        """MUST-HIT and its discriminator in one arm: a dead probe returning 0
        everywhere passes an all-old fixture, so the FRESH count is what makes
        the zero mean MEASURED-NONE rather than BLIND."""
        from helm import proxywatch
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        busy = self._seat(root, "busy", [5, 30, 100])
        quiet = self._seat(root, "quiet", [400, 9000])
        mixed = self._seat(root, "mixed", [10, 4000, 20])
        self.assertEqual(proxywatch.fanout_reading(busy)["active"], 3)
        self.assertEqual(proxywatch.fanout_reading(quiet)["active"], 0)
        self.assertEqual(proxywatch.fanout_reading(mixed)["active"], 2)

    def test_unreadable_is_UNKNOWN_and_never_a_measured_zero(self):
        """An UNKNOWN that decays to 0 is the whole bug class: a controller
        would read 'no demand' from a directory it could not open.

        THE POSITIVE CONTROL RUNS FIRST AND IS UNCONDITIONAL. Two of the
        assertions below are ABSENCE assertions, and an absence passes just as
        happily against a reader that answers None to EVERYTHING — which is
        precisely what a swallowed exception produces. Proving the reader CAN
        return a live count, on the same observable, in the same pass, is what
        makes the two Nones mean REFUSED-TO-GUESS rather than BROKEN. helm's
        own vacuous-assertion rung caught this arm without the control, on the
        commit that introduced it.
        """
        from helm import proxywatch
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        live = self._seat(root, "live", [5, 5])
        self.assertEqual(proxywatch.fanout_reading(live)["active"], 2)
        self.assertIsNone(proxywatch.fanout_reading(None)["active"])
        self.assertIsNone(proxywatch.fanout_reading("")["active"])
        # A DIRECTORY THAT DOES NOT EXIST IS UNKNOWN. The first cut of this
        # arm asserted 0 here and reasoned that glob answers honestly on an
        # absent path — true, and the wrong answer anyway. A must-hit control
        # against the LIVE fleet found seat `cj` with no instance dir at all,
        # reading as a confident 0 while the reader could not see the seat.
        gone = os.path.join(root, "no-such-seat")
        self.assertIsNone(proxywatch.fanout_reading(gone)["active"])
        # An instance root that EXISTS but has no claude/projects is ALSO
        # UNKNOWN — a layout we could not read, never an idle seat. THIS ARM
        # ASSERTED 0 HERE AND OUTLIVED ITS OWN CURE BY ONE COMMIT: I applied
        # The fix to the reader (whose FIX said in as many words that an
        # existing-empty instance root should be UNKNOWN) and left the test
        # pinning the pre-cure semantics. The lane then gated RED on my stale
        # expectation while trunk was innocent, and the base-check could not
        # attribute it — so a one-line stale assertion held a five-lane batch.
        empty = os.path.join(root, "empty-seat")
        os.makedirs(empty)
        self.assertIsNone(proxywatch.fanout_reading(empty)["active"])
        # THE DISCRIMINATOR that keeps UNKNOWN from swallowing real zeros: a
        # READABLE projects root with no subagent transcripts is a MEASURED 0.
        # Without this arm a reader that answered None to everything would
        # satisfy every assertion above.
        quiet = os.path.join(root, "quiet-seat")
        os.makedirs(os.path.join(quiet, "claude", "projects", "p", "s"))
        self.assertEqual(proxywatch.fanout_reading(quiet)["active"], 0)

    def test_the_window_is_a_parameter_and_actually_moves_the_answer(self):
        """Pins that window_s is CONSULTED. A hardcoded 180 passes every arm
        above and fails this one."""
        from helm import proxywatch
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        seat = self._seat(root, "s", [50, 300, 1000])
        self.assertEqual(proxywatch.fanout_reading(seat)["active"], 1)
        self.assertEqual(
            proxywatch.fanout_reading(seat, window_s=500)["active"], 2)
        self.assertEqual(
            proxywatch.fanout_reading(seat, window_s=99999)["active"], 3)

    def test_every_unreadable_shape_is_UNKNOWN_not_a_confident_zero(self):
        """The exact-source probes, one arm per adjacent state.

        THE ROOT CAUSE THESE PIN: `glob` returns [] on an unreadable directory
        WITHOUT RAISING, so the first two cuts of this reader turned every
        permission and layout failure into a confident 0. The try/except that
        was supposed to catch it could never fire. Each shape below produced a
        measured zero before the scandir rewrite.
        """
        from helm import proxywatch
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)

        # POSITIVE CONTROL FIRST, unconditional: the reader can still count.
        live = self._seat(root, "live", [5, 5])
        self.assertEqual(proxywatch.fanout_reading(live)["active"], 2)

        # (a) instance root exists but has NO claude/projects — a layout we do
        #     not understand, not an idle seat.
        bare = os.path.join(root, "bare")
        os.makedirs(bare)
        self.assertIsNone(proxywatch.fanout_reading(bare)["active"])

        # (b) `claude` is a FILE, not a directory.
        filey = os.path.join(root, "filey")
        os.makedirs(filey)
        with open(os.path.join(filey, "claude"), "w") as fh:
            fh.write("x")
        self.assertIsNone(proxywatch.fanout_reading(filey)["active"])

        # (c) `projects` is a FILE.
        filey2 = os.path.join(root, "filey2")
        os.makedirs(os.path.join(filey2, "claude"))
        with open(os.path.join(filey2, "claude", "projects"), "w") as fh:
            fh.write("x")
        self.assertIsNone(proxywatch.fanout_reading(filey2)["active"])

        # (d) projects exists but is UNREADABLE. Root ignores mode bits, so
        #     this arm would pass vacuously as root — skip rather than lie.
        if os.geteuid() != 0:
            locked = self._seat(root, "locked", [5])
            pdir = os.path.join(locked, "claude", "projects")
            os.chmod(pdir, 0o000)
            self.addCleanup(os.chmod, pdir, 0o755)
            self.assertIsNone(proxywatch.fanout_reading(locked)["active"])

        # The positive control STILL holds after all of it — proving the
        # UNKNOWNs above are discrimination, not a reader stuck on None.
        self.assertEqual(proxywatch.fanout_reading(live)["active"], 2)

    def test_a_subagents_path_that_is_not_a_directory_is_UNKNOWN(self):
        """The R2 catch — THE SAME SILENCE ONE LEVEL DOWN.

        `os.path.isdir(sub)` collapses THREE states into one False: the dir is
        ABSENT (legitimate — that session never fanned out), it EXISTS AS A
        FILE, or we CANNOT TELL. I had already replaced glob's silence at the
        projects level and at the entries level, then borrowed isdir's silence
        here — the third instance of one class inside one function.
        """
        from helm import proxywatch
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)

        # POSITIVE CONTROL, unconditional and first.
        live = self._seat(root, "live", [5])
        self.assertEqual(proxywatch.fanout_reading(live)["active"], 1)

        # ABSENT subagents dir is the LEGITIMATE case and must stay a
        # measured zero — a session that simply never fanned out.
        plain = os.path.join(root, "plain")
        os.makedirs(os.path.join(plain, "claude", "projects", "p", "s"))
        self.assertEqual(proxywatch.fanout_reading(plain)["active"], 0)

        # PRESENT BUT A FILE is a layout we do not understand -> UNKNOWN.
        filey = os.path.join(root, "filey")
        sess = os.path.join(filey, "claude", "projects", "p", "s")
        os.makedirs(sess)
        with open(os.path.join(sess, "subagents"), "w") as fh:
            fh.write("x")
        self.assertIsNone(proxywatch.fanout_reading(filey)["active"])

    def test_an_unreadable_session_dir_is_UNKNOWN(self):
        """The R2 FIX named TWO nested collapses: a subagents path that
        is a FILE, and an UNREADABLE SESSION DIR. I cured the first
        DELIBERATELY and the second FELL OUT of the same stat change — so it
        was correct with no arm pinning it, which is a cure nobody can defend
        and one refactor away from silently regressing. Probed live, then
        pinned.
        """
        if os.geteuid() == 0:
            self.skipTest("root ignores mode bits; this arm would pass vacuously")
        from helm import proxywatch
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        live = self._seat(root, "live", [5])            # positive control
        self.assertEqual(proxywatch.fanout_reading(live)["active"], 1)
        inst = os.path.join(root, "locked-session")
        sess = os.path.join(inst, "claude", "projects", "p", "s")
        os.makedirs(os.path.join(sess, "subagents"))
        os.chmod(sess, 0o000)
        self.addCleanup(os.chmod, sess, 0o755)
        self.assertIsNone(proxywatch.fanout_reading(inst)["active"])

    def test_a_dangling_subagents_symlink_is_UNKNOWN_not_absent(self):
        """The fourth boundary, MEASURED at 0 before the cure.

        os.stat FOLLOWS SYMLINKS, so a dangling `subagents` link raises ENOENT
        and read as "this session never fanned out". The link EXISTS — it is
        MALFORMED, which is a different fact from absence. lstat establishes
        the link's own existence before its target's.
        """
        from helm import proxywatch
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        live = self._seat(root, "live", [5])            # positive control
        self.assertEqual(proxywatch.fanout_reading(live)["active"], 1)
        inst = os.path.join(root, "dangling")
        sess = os.path.join(inst, "claude", "projects", "p", "s")
        os.makedirs(sess)
        os.symlink(os.path.join(sess, "nowhere"),
                   os.path.join(sess, "subagents"))
        self.assertIsNone(proxywatch.fanout_reading(inst)["active"])

    def test_a_DIRECTORY_named_like_a_transcript_is_not_a_subagent(self):
        """The fifth boundary, MEASURED at 1 before the cure.

        NAME IS NOT TYPE. `agent-*.jsonl` is a naming convention and nothing
        enforces it; a DIRECTORY with that name passed the filter and counted
        as a live subagent, inflating the demand reading a controller acts on.
        """
        from helm import proxywatch
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        live = self._seat(root, "live", [5])            # positive control
        self.assertEqual(proxywatch.fanout_reading(live)["active"], 1)
        inst = os.path.join(root, "dirnamed")
        sub = os.path.join(inst, "claude", "projects", "p", "s", "subagents")
        os.makedirs(os.path.join(sub, "agent-imposter.jsonl"))
        self.assertIsNone(proxywatch.fanout_reading(inst)["active"])

    def test_unreadable_project_and_subagents_dirs_are_UNKNOWN(self):
        """Two OSError branches were CORRECT BUT UNPINNED. That is
        the state that bit us on the session dir — right by accident, one
        refactor from regressing, and nothing would say so."""
        if os.geteuid() == 0:
            self.skipTest("root ignores mode bits; this arm would pass vacuously")
        from helm import proxywatch
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        live = self._seat(root, "live", [5])            # positive control
        self.assertEqual(proxywatch.fanout_reading(live)["active"], 1)
        a = os.path.join(root, "lockedproj")
        proj = os.path.join(a, "claude", "projects", "p")
        os.makedirs(os.path.join(proj, "s"))
        os.chmod(proj, 0o000)
        self.addCleanup(os.chmod, proj, 0o755)
        self.assertIsNone(proxywatch.fanout_reading(a)["active"])
        b = self._seat(root, "lockedsub", [5])
        subdir = os.path.join(b, "claude", "projects", "proj", "sess",
                              "subagents")
        os.chmod(subdir, 0o000)
        self.addCleanup(os.chmod, subdir, 0o755)
        self.assertIsNone(proxywatch.fanout_reading(b)["active"])

    def test_a_dangling_MATCHED_agent_symlink_is_UNKNOWN(self):
        """R4 hand-back gap (1): the follow_symlinks=False guard on
        the ENTRY stat was UNPINNED. Their mutation — e.stat(follow_symlinks=
        False) -> e.stat() — turns a dangling agent-*.jsonl link into
        FileNotFoundError, which the vanished-file skip then absorbs silently,
        and all thirteen fanout tests stayed green.

        A dangling transcript link is MALFORMED, not a subagent that finished
        while we walked. Those are different facts and only one of them is a
        legitimate skip.
        """
        from helm import proxywatch
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        live = self._seat(root, "live", [5])            # positive control
        self.assertEqual(proxywatch.fanout_reading(live)["active"], 1)
        inst = os.path.join(root, "danglingagent")
        sub = os.path.join(inst, "claude", "projects", "p", "s", "subagents")
        os.makedirs(sub)
        os.symlink(os.path.join(sub, "gone.jsonl"),
                   os.path.join(sub, "agent-dangling.jsonl"))
        self.assertIsNone(proxywatch.fanout_reading(inst)["active"])

    def test_an_empty_but_readable_projects_root_is_a_MEASURED_zero(self):
        """Proof gap (1): the 0-from-an-empty-projects-root path had
        NO arm creating one, so the mutation `if not project_dirs: return
        UNKNOWN` left every other test green. A zero nobody pins is a zero
        nobody can defend."""
        from helm import proxywatch
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        live = self._seat(root, "live", [5])            # positive control
        self.assertEqual(proxywatch.fanout_reading(live)["active"], 1)
        bare = os.path.join(root, "bare")
        os.makedirs(os.path.join(bare, "claude", "projects"))
        self.assertEqual(proxywatch.fanout_reading(bare)["active"], 0)


class FanoutReachesTheOwnerSurfaceTest(unittest.TestCase):
    """Proof gap (2), and it is the one that matters.

    EVERY arm in FanoutDemandReadingTest calls `fanout_reading` DIRECTLY. So
    deleting the health() assignment, deleting the renderer, or eliding the
    field entirely leaves them all green — the reader would be perfect and
    UNWIRED, which is the exact built-but-never-surfaced failure this whole
    lane exists to cure for credential demand. The reader is the MECHANISM;
    the rendered line is the DELIVERABLE.
    """

    def test_the_rendered_seat_line_carries_fanout_in_all_three_states(self):
        from helm import proxywatch
        r = row()
        r["fanout"] = {"active": 3, "window_s": 180}
        self.assertIn(
            "fanout=3", "\n".join(proxywatch.report_lines(rep(r))),
            "a positive count must REACH the owner-facing line")
        r["fanout"] = {"active": None, "window_s": 180}
        self.assertIn(
            "fanout=UNKNOWN", "\n".join(proxywatch.report_lines(rep(r))),
            "UNKNOWN must be VISIBLE — a blind seat that renders as calm is "
            "the whole defect")
        r["fanout"] = {"active": 0, "window_s": 180}
        rendered = "\n".join(proxywatch.report_lines(rep(r)))
        self.assertNotIn(
            "fanout", rendered,
            "a measured zero stays SILENT — nine lines of fanout=0 every "
            "fifteen minutes is the attention-budget spam this avoids, and "
            "silence is only safe because UNKNOWN prints")
        # MUST-HIT CONTROL on the silence assertion: the same row DOES render
        # its other fields, so the absence above is discrimination and not an
        # empty render.
        self.assertIn("probe=", rendered)


class FanoutSurvivesTheWholeProductionPathTest(_HealthRig):
    """The Q3 BLOCKER, and my previous arm's docstring overclaimed.

    FanoutReachesTheOwnerSurfaceTest injects `row["fanout"]` by hand and calls
    report_lines, so it pins the RENDERER and nothing else — deleting the
    health() assignment left it green. I titled that commit "prove the
    DELIVERABLE, not just the mechanism" on a test that proved ONE HOP OF TWO.

    This drives the REAL health() over a real tmpdir seat, through _HealthRig,
    and asserts the reading survives BOTH hops: health() -> row, and row ->
    rendered line. Deleting either end reddens it.
    """

    def _plant(self, count, age=5):
        sub = os.path.join(self.d, "claude", "projects", "p", "s", "subagents")
        os.makedirs(sub, exist_ok=True)
        now = time.time()
        for i in range(count):
            p = os.path.join(sub, "agent-p%d.jsonl" % i)
            with open(p, "w") as fh:
                fh.write("{}\n")
            os.utime(p, (now - age, now - age))

    def test_health_assigns_fanout_and_the_line_carries_it(self):
        from helm import proxywatch
        self._plant(2)
        row_out = self._health(live=True, age=60)
        # HOP ONE: health() actually computed and attached it.
        self.assertIsNotNone(row_out.get("fanout"),
                             "health() must ATTACH the demand reading")
        self.assertEqual(row_out["fanout"]["active"], 2)
        # HOP TWO: the same row, unmodified, reaches the owner-facing line.
        rendered = "\n".join(proxywatch.report_lines(rep(row_out)))
        self.assertIn("fanout=2", rendered)

    def test_a_seat_with_no_transcripts_is_a_measured_zero_end_to_end(self):  # noqa: VACUOUS_ASSERTION — the control is CROSS-ROW and the rung compares root variables: the same row shape with active=2 is rendered in the same pass and asserted to CARRY fanout=2, so a renderer that never emits the field fails HERE, not silently. A same-variable control (probe= on `rendered`) also proves the line rendered at all. Both are unconditional.
        """The discriminator: an existing readable layout with no subagents
        must render SILENT, not UNKNOWN — otherwise the end-to-end arm above
        would pass against a reader stuck on a constant."""
        from helm import proxywatch
        os.makedirs(os.path.join(self.d, "claude", "projects", "p", "s"),
                    exist_ok=True)
        row_out = self._health(live=True, age=60)
        self.assertEqual(row_out["fanout"]["active"], 0)
        rendered = "\n".join(proxywatch.report_lines(rep(row_out)))
        # POSITIVE CONTROL ON THE SAME OBSERVABLE, which `probe=` was not:
        # the very same row with a non-zero count DOES render the field, so
        # the silence below is the renderer discriminating rather than the
        # field being unreachable from this path. helm's vacuous-assertion
        # rung caught the weaker version on the commit that introduced it.
        loud = dict(row_out, fanout={"active": 2, "window_s": 180})
        self.assertIn("fanout=2",
                      "\n".join(proxywatch.report_lines(rep(loud))))
        self.assertNotIn("fanout", rendered)
        self.assertIn("probe=", rendered)      # the line DID render at all

    def test_an_UNKNOWN_reading_survives_row_text_AND_json(self):
        """R4 hand-back gap (2): the real-health E2E covered
        positive and zero only, so collapsing the health-layer UNKNOWN to 0 —
        or replacing the whole reading with fanout=None — left the direct
        reader tests and the hand-injected renderer arm green.

        THE ASYMMETRY THAT MAKES JSON LOAD-BEARING: the text renderer prints
        UNKNOWN for a MISSING fanout dict and for a MEASURED-unknown alike, so
        the line cannot tell them apart. `--json` dumps the row itself, where
        {"active": null, "window_s": 180} and a bare null are different facts.
        A controller reads the JSON. Pin all three.
        """
        from helm import proxywatch
        # The rig's seat dir exists but has NO claude/projects, so the reader
        # cannot see the seat — UNKNOWN by construction, through real health().
        row_out = self._health(live=True, age=60)
        self.assertIsNotNone(row_out.get("fanout"),
                             "health() must attach the reading even when it "
                             "cannot measure — a missing key and a measured "
                             "UNKNOWN are different facts")
        self.assertIsNone(row_out["fanout"]["active"])
        self.assertEqual(row_out["fanout"]["window_s"], 180)
        rendered = "\n".join(proxywatch.report_lines(rep(row_out)))
        self.assertIn("fanout=UNKNOWN", rendered)
        # THE JSON PATH, which cmd_proxywatch dumps verbatim: the window must
        # survive, not just the null.
        payload = json.loads(json.dumps({"report": rep(row_out)}))
        fan = payload["report"]["seats"][0]["fanout"]
        self.assertIsNone(fan["active"])
        self.assertEqual(fan["window_s"], 180)


class RenamedSeatKeepsItsRuntimeIndex(unittest.TestCase):
    """A rename must not silently disarm a seat's ability to bind a verdict.

    MEASURED 2026-08-27, the incident this repairs: `codex-3` was renamed to
    `helm-codex`. The rename moved the roster row, and the next hook join from
    a process whose environ still said HELM_CHAT_NAME=codex-3 re-created the
    old name as a BARE STUB — the row was literally {} — whose session the
    bind-refused guard nulls. The pass enumerates CATALOG names, so it asked
    about the stub, got nothing, and minted no runtime proof. The verdict door
    then refused FOUR measured verdicts from a seat that was working fine, and
    under the consolidation trial that was the fleet's single point of failure.

    The name was only an INDEX. These arms hold the repair to exactly that,
    and the must-misses are the ones that matter: a spawn record belonging to
    ANOTHER seat must never resolve, or the repair becomes a way to borrow a
    peer's identity."""

    SESSION = "8e5608bb-f49a-4877-a819-864f5fece77c"

    def _resolve(self, roster, spawn, family=("codex", None)):
        with mock.patch.object(proxywatch, "_roster_identity_for_session",
                               wraps=proxywatch._roster_identity_for_session), \
                mock.patch("helm.seats.roster_checked",
                           return_value=(roster, False)), \
                mock.patch("helm.seat._seat_family", return_value=family), \
                mock.patch("helm.seat._instance_dir", return_value="/nope"), \
                mock.patch("helm.seat._spawn_record", return_value=spawn):
            return proxywatch._roster_session("seat-a")

    def test_a_stale_name_resolves_to_the_seat_that_now_holds_the_session(self):
        got = self._resolve(
            roster={"seat-a": {}, "seat-b": {"session": self.SESSION}},
            spawn={"seat": "seat-a", "session": self.SESSION})
        self.assertEqual(got, (self.SESSION, "seat-b", None))

    def test_MUST_MISS_a_spawn_record_for_ANOTHER_seat_never_resolves(self):
        """The borrowed-identity attack this repair must not open."""
        got = self._resolve(
            roster={"seat-a": {}, "seat-b": {"session": self.SESSION}},
            spawn={"seat": "seat-d", "session": self.SESSION})
        self.assertEqual(got[0], None)
        self.assertIn("no exact current session", got[2])

    def test_MUST_MISS_no_spawn_record_keeps_the_original_refusal(self):
        got = self._resolve(roster={"seat-a": {}}, spawn=None)
        self.assertEqual(got[0], None)
        self.assertIn("no exact current session", got[2])

    def test_MUST_MISS_a_session_two_rows_claim_is_refused_not_guessed(self):
        """Ambiguity must refuse. Two current identities on one session is
        exactly the case where picking either one would be a fabrication."""
        got = self._resolve(
            roster={"seat-a": {}, "seat-b": {"session": self.SESSION},
                    "seat-c": {"session": self.SESSION}},
            spawn={"seat": "seat-a", "session": self.SESSION})
        self.assertEqual(got[0], None)
        self.assertIn("matched 2 current identities", got[2])

    def test_a_healthy_row_is_untouched_by_the_fallback(self):
        """The repair fires ONLY on a stub. A live row must not reach it."""
        got = self._resolve(
            roster={"seat-a": {"session": self.SESSION}},
            spawn={"seat": "seat-a", "session": "a-different-session"})
        self.assertEqual(got, (self.SESSION, "seat-a", None))

class OneScalarReaderTest(unittest.TestCase):
    """task/2126: ONE reader answers for the plan AND the watchdog. Measured
    on trunk: the two _yaml_scalar copies disagreed on 7 of 19 poles, worst
    case a single-quoted value with a trailing comment returned WITH ITS
    QUOTES ON by the watchdog side — drift that does not exist, on a field
    nobody edited. The reader returns a value or a TYPED ABSENCE and the
    callers decide: plan raises, watchdog reads UNKNOWN."""

    POLES = [
        # (source, value or None-for-absent) — a probe's measured 19, plus the
        # upstream example-file style the incidence scan named
        ('"x" # note', "x"), ("'x' # note", "x"), ("", None), ("   ", None),
        ("'unterminated", None), ('"unterminated', None),
        ('"x" trailing', None), ("bare", "bare"), ('"dq"', "dq"),
        ("'sq'", "sq"), ("'it''s'", "it's"), ('"a\\"b"', 'a"b'),
        ("true", "true"), ("false", "false"), ("0", "0"), ("null", "null"),
        ('""', ""), ("''", ""),
        ('"safe" # enum example: safe, fast', "safe"),
        # THE COMMENT BOUNDARY, three poles, because unifying two readers
        # means keeping the CORRECT copy and the first cut kept the other:
        # the generator split on [ \t]+# and the shared reader split on a
        # single space, so a TAB-commented value shipped with its comment
        # glued on. The third pole is the one a wider split breaks -- a hash
        # with NO whitespace before it belongs to the scalar.
        ("bare\t#c", "bare"), ("bare  \t #c", "bare"), ("bare#c", "bare#c"),
    ]

    def test_the_pole_table_agrees_across_both_callers(self):  # noqa: VACUOUS_ASSERTION — the must-hit is the single-quote-plus-comment pole asserted BY NAME in the next method; an empty POLES table reddens the assertNotEqual against zero here
        """Every pole through the plan's raising caller and the watchdog's
        tolerating one. Absence is TYPED on both sides and each side proves
        its own half: the plan must RAISE on an absent pole (a caught
        ValueError becoming None would conflate refusal with silence), the
        watchdog must return None on exactly those poles."""
        from helm import seat_launch_assets, proxywatch
        self.assertNotEqual(len(self.POLES), 0,
                            "an empty table passes 0 == 0 vacuously")
        for source, want in self.POLES:
            if want is None:
                with self.assertRaises(ValueError,
                                       msg="plan must refuse %r" % source):
                    seat_launch_assets._yaml_scalar_or_raise(source)
            else:
                self.assertEqual(
                    seat_launch_assets._yaml_scalar_or_raise(source), want,
                    "plan caller on %r" % source)
            self.assertEqual(proxywatch._yaml_scalar(source), want,
                             "watchdog caller on %r" % source)

    def test_the_single_quoted_comment_row_keeps_no_quotes(self):  # noqa: VACUOUS_ASSERTION — the must-hit: the divergent pole itself
        """THE LIVE POLE, by name. The old watchdog reader tested
        startswith+endswith on the quotes; a trailing comment ends the line
        in a letter, the test fell to the bare branch, and the value came
        back with its quotes still on — a DIFFERENT string than the
        generator wrote, which proxywatch then reported as drift."""
        from helm import proxywatch
        self.assertEqual(proxywatch._yaml_scalar("'x' # rotated 9/10"), "x")

    def test_a_TAB_opens_a_comment_the_same_as_a_space(self):  # noqa: VACUOUS_ASSERTION — three unconditional equalities, and the third is the must-miss that a wider split would redden
        """THE POLE THE UNIFICATION LOST, by name, because a row in a table
        is easy to delete and a named arm is not.

        YAML opens a comment on ANY whitespace, which is what the config
        GENERATOR has always done. The first cut of the shared reader split
        on a single space only, so `key:\tvalue\t#c` came back carrying its
        own comment -- and the generator writes config from this reader, so
        the comment would have shipped INTO the file as part of the value.

        THE THIRD ASSERTION IS THE BOUND. A reader that fixed the tab by
        splitting on a bare `#` would break a scalar that legitimately
        contains one, so the whitespace is required rather than incidental."""
        from helm import proxywatch, seat_launch_assets
        self.assertEqual(proxywatch._yaml_scalar("bare\t#c"), "bare")
        self.assertEqual(
            seat_launch_assets._yaml_scalar_or_raise("bare\t#c"), "bare")
        self.assertEqual(proxywatch._yaml_scalar("bare#c"), "bare#c")

    def test_the_readers_failure_class_survives_its_first_caller(self):  # noqa: VACUOUS_ASSERTION — assertRaises IS the assertion and the message content is asserted by name; the ordinary-absence control below must NOT carry the class
        """THE TYPED ABSENCE IS THE POINT OF THE LANE, and the first caller
        threw it away: `_block_scalar` read (value, err) and discarded err,
        so a MALFORMED key refused as "absent or not a scalar" -- the same
        sentence the module had before any of this. A reader that knows why
        it could not read, feeding a caller that does not say, buys nothing.

        AND ABSENCE IS STILL ABSENCE: the control is a genuinely missing key,
        which must keep the old sentence rather than acquire a failure class
        it does not have."""
        from helm import seat_launch_assets
        with self.assertRaises(ValueError) as raised:
            seat_launch_assets._block_scalar("'unterminated: x", "name")
        self.assertIn("malformed single-quoted scalar", str(raised.exception))
        with self.assertRaises(ValueError) as plain:
            seat_launch_assets._block_scalar("other: x", "name")
        self.assertIn("absent or not a scalar", str(plain.exception))
        self.assertNotIn("malformed", str(plain.exception))

    def test_the_plan_refuses_an_unterminated_scalar(self):  # noqa: VACUOUS_ASSERTION — assertRaises is the positive control: the raising call IS the assertion
        """The plan's typed absence is fatal: an unreadable desired state
        must refuse, never plan against a guess."""
        from helm import seat_launch_assets
        with self.assertRaises(ValueError):
            seat_launch_assets._yaml_scalar_or_raise("'unterminated")

    def test_the_fork_boolean_keeps_unknown_unknown(self):  # noqa: VACUOUS_ASSERTION — every state's outer err is asserted by name through generated config, and the valid-true controls must pass, so a universal-refusal cure reddens here
        """The whole route-reader contract from GENERATED config text —
        never a hand-planted row, and every fixture line written EXACTLY as
        it arrives in a real file: text -> _proxy_config_route -> parsed
        row -> consumer -> outer err. The producer types fork as a Go bool:
        omission and bare null/blank are its zero value (the documented
        rename shape, still refused); bare true/false and the y/yes/on and
        n/no/off spellings in their exact case variants are valid; a QUOTED
        true/false is a string the producer rejects; garbage is a type
        error. The last two are UNKNOWN, on their own sentence, and in a
        MIXED population both categories keep their evidence."""
        import json
        import tempfile
        from helm import proxywatch, seat

        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        auth = os.path.join(d, "auth")
        os.makedirs(auth)
        with open(os.path.join(auth, "codex.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"type": "codex", "access_token": "secret"}, f)
        config = os.path.join(d, "config.yaml")

        def route_err(fork_line, extra_line=None):
            """The row under test keeps every generated line except its own
            fork, which fork_line replaces verbatim — None omits the key.
            extra_line adds a SECOND row to ask the mixed-population
            question."""
            generated = seat._config_yaml(
                8317, auth, "inbound-secret",
                channel="codex", model="gpt-5.6-sol")
            out, skipped = [], False
            for line in generated.splitlines(keepends=True):
                if not skipped and line.strip().startswith("fork:"):
                    skipped = True
                    if fork_line is not None:
                        out.append("      fork: %s\n" % fork_line)
                    continue
                out.append(line)
            text = "".join(out)
            if extra_line is not None:
                # A sibling row in the SAME channel sequence — appended as
                # another "- name:" item under the generated block, never a
                # duplicate channel key, which yaml.v3 rejects and the
                # bounded reader would silently tolerate. The sibling's
                # alias is a real catalogued id so only its fork differs.
                text += ("    - name: gpt-5.6-sol\n"
                         "      alias: claude-sonnet-5\n"
                         "      fork: %s\n" % extra_line)
            with open(config, "w", encoding="utf-8") as f:
                f.write(text)
            return proxywatch._proxy_config_route(
                config, "gpt-5.6-sol")[3]

        # VALID TRUE, every accepted spelling: clean route, no err.
        for spelling in ("true", "True", "TRUE",
                         "y", "Y", "yes", "Yes", "YES",
                         "on", "On", "ON"):
            self.assertIsNone(route_err(spelling),
                              "bare %s is producer-valid" % spelling)
        # VALID FALSE spellings: the measured rename diagnosis stands.
        for spelling in ("false", "False", "FALSE",
                         "n", "N", "no", "No", "NO",
                         "off", "Off", "OFF"):
            err = route_err(spelling)
            self.assertIsNotNone(err)
            self.assertIn("no fork", err, spelling)
        # Omission, bare null, bare blank: the producer zero value — the
        # documented rename shape, still refused as no fork. The blank
        # pole writes `fork:` with nothing after it, which is NOT the
        # omission sentinel: an empty line and an absent line differ.
        for line in (None, "null", ""):
            err = route_err(line)
            self.assertIsNotNone(err)
            self.assertIn("no fork", err, repr(line))
        # QUOTED true/false are STRINGS the producer rejects; garbage and
        # numeric are type errors: UNKNOWN, on their own sentence, never
        # inside the rename claim. Each fixture line arrives exactly as
        # written — the spellings here are the WHOLE value.
        for line in ('"true"', "'true'", '"false"', "garbage", "0"):
            err = route_err(line)
            self.assertIsNotNone(err)
            self.assertIn("unreadable", err, repr(line))
            self.assertNotIn("renames a catalogued route", err, repr(line))
        # A trailing comment on a valid value is the documented hand-edit
        # and reads the value, and a comment-ONLY value is the producer's
        # null — the zero value, refused as no fork like omission.
        self.assertIsNone(route_err("true # rotated 9/10"))
        err = route_err("# just a note")
        self.assertIsNotNone(err)
        self.assertIn("no fork", err)
        # An unterminated quote is malformed, and a mixed-case spelling the
        # producer does not list is unsupported: both UNKNOWN.
        for line in ("'true", "tRuE"):
            err = route_err(line)
            self.assertIsNotNone(err)
            self.assertIn("unreadable", err, repr(line))
            self.assertNotIn("renames a catalogued route", err, repr(line))
        # QUOTED compatibility spellings are producer-accepted typed bools:
        # quoted yes/on read TRUE (clean), quoted off reads FALSE (rename).
        for spelling in ('"yes"', "'on'"):
            self.assertIsNone(route_err(spelling),
                              "quoted %s is a producer bool" % spelling)
        err = route_err('"Off"')
        self.assertIsNotNone(err)
        self.assertIn("no fork", err)
        # MIXED: an unreadable row beside a known-false sibling keeps BOTH
        # evidences — the rename claim for the measured row, the unreadable
        # sentence for the unknown one, in one diagnosis.
        err = route_err("garbage", extra_line="false")
        self.assertIsNotNone(err)
        self.assertIn("renames a catalogued route", err)
        self.assertIn("no fork", err)
        self.assertIn("unreadable", err)
        # AND THE ROWS MUST NOT TRADE CLAUSES: the measured-false row is the
        # sonnet sibling, the unreadable row is the opus row under test.
        rename_clause, _, unknown_clause = err.partition("; and ")
        self.assertIn("claude-sonnet-5", rename_clause)
        self.assertIn("claude-opus-5", unknown_clause)

    def test_the_watchdog_reads_absence_as_none_and_never_drift(self):  # noqa: VACUOUS_ASSERTION — the None answer against the valued pole below it
        """The watchdog's typed absence is UNKNOWN: None, and crucially NOT
        a string — a string-coerced None reads as "none" and a quote-wrapped
        "'true'" is not "true", both landing fork=False with nothing said.
        The comparison is typed."""
        from helm import proxywatch
        self.assertIsNone(proxywatch._yaml_scalar("'unterminated"))
        self.assertEqual(proxywatch._yaml_scalar("true"), "true")



class TheJoinGetsThisSeatsOwnProcessesTest(unittest.TestCase):
    """`_session_joined` EXCLUDES the `named` set before testing ambiguity and
    contradiction, so handing it the whole fleet's env-named processes silently
    disarmed those refusals.

    The canonical caller (`orcaadopt.resolve`) passes only this seat's
    processes. This census passed every named process, so a row that
    CONTRADICTED the session — a process declaring a different seat — was
    excluded as 'already named' instead of refusing, and a nameless sibling
    then keyed the seat alone. The census invented a live seat where the
    canonical owner refuses both.
    """

    def _procs(self, rows):
        return [{"pid": p, "start": "1000", "seat": seat,
                 "pane_key": "k%d" % p, "resume_sid": sid,
                 "worktree_id": None} for p, seat, sid in rows]

    def _census(self, procs, roster):
        from helm import orcaadopt
        named = [p for p in procs if p.get("seat")]
        with mock.patch.object(orcaadopt, "roster_identity",
                               return_value=("sid-b", ["sid-b"], False)), \
                mock.patch("helm.seats_common.roster", return_value=roster):
            return proxywatch._session_keyed_seats(procs, named)

    def test_a_contradicting_process_beside_a_nameless_one_refuses(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control is the FIRST `self._census(...)` call in the method body, on the same `seats`/`refusals` names; the scanner cannot follow the helper method that produces them. MUTATION-PROVEN: making _session_keyed_seats always return (set(), []) reddens BOTH arms in this class.
        """THE MEASURED SHAPE: two processes on seat-b's current session, one
        declaring itself seat-a and one nameless."""
        # UNCONDITIONAL POSITIVE CONTROL ON THE SAME OBSERVABLE: this census
        # DOES key a seat, so the empty set below is the refusal firing and
        # not a helper that never keys anything.
        seats, refusals = self._census(self._procs([(2, None, "sid-b")]),
                                       {"seat-b": {"session": "sid-b"}})
        self.assertEqual(seats, {"seat-b"})
        procs = self._procs([(1, "seat-a", "sid-b"), (2, None, "sid-b")])
        seats, refusals = self._census(procs, {"seat-b": {"session": "sid-b"}})
        self.assertEqual(seats, set(),
                         "the census invented a live seat the canonical owner "
                         "refuses")
        self.assertTrue(refusals, "it refused silently, which reads as absent")

    def test_a_single_unambiguous_process_still_keys_its_seat(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control is the FIRST `self._census(...)` call in the method body, on the same `seats`/`refusals` names; the scanner cannot follow the helper method that produces them. MUTATION-PROVEN: making _session_keyed_seats always return (set(), []) reddens BOTH arms in this class.
        """THE POSITIVE CONTROL the refusal above is worthless without: the
        join must still work, or 'refuses' would just mean 'never keys'."""
        # UNCONDITIONAL POSITIVE CONTROL ON `refusals`: the contradicting
        # pair DOES produce one, so the empty list below is this census being
        # clean rather than a helper that never refuses.
        seats, refusals = self._census(
            self._procs([(1, "seat-a", "sid-b"), (2, None, "sid-b")]),
            {"seat-b": {"session": "sid-b"}})
        self.assertTrue(refusals)
        procs = self._procs([(2, None, "sid-b")])
        seats, refusals = self._census(procs, {"seat-b": {"session": "sid-b"}})
        self.assertEqual(seats, {"seat-b"})
        self.assertEqual(refusals, [])


class UnpoolingTheLastAccountRetiresTheRefusalTest(unittest.TestCase):
    """task/2480 R3 — a successful refresh that learns the pool is EMPTY must
    publish that, not return before the writer.

    `helm codex unpool` of the last pooled account removed the files and left
    the last all-capped snapshot on disk. The next timed pass enumerated the
    pool, found nothing, and returned None BEFORE `write_snapshot` — so every
    codex dispatch kept being refused by a snapshot about accounts that were
    gone, for up to `codexbudget.GATE_MAX_AGE_S`, and then cleared by the CLOCK
    rather than by anything anyone measured."""

    def setUp(self):
        from helm import codexbudget, codexhomes, dispatches
        self.mod, self.homes, self.dispatches = codexbudget, codexhomes, dispatches
        self.tmp = tempfile.mkdtemp(prefix="helm-test-unpool-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.envp = mock.patch.dict(
            os.environ, {"HOME": self.tmp,
                         "HELM_HOME": os.path.join(self.tmp, "helm"),
                         "HELM_CACHE_DIR": os.path.join(self.tmp, "cache")})
        self.envp.start()
        self.addCleanup(self.envp.stop)
        os.environ.pop("HELM_CODEX_WEEKLY_CEILING_PCT", None)
        self.pool = self.homes.pool_dir()
        os.makedirs(self.pool, exist_ok=True)
        self.capped = [{"email": "a@x.example", "account_id": "acct-A",
                        "plan": "team", "state": "exhausted",
                        "status": "blocked", "longest_pct": 100.0,
                        "windows": [{"label": "7d", "used_percent": 100.0,
                                     "reset_after_seconds": 3600}]}]

    def fold(self, rows):
        """Persist the burn-flag fold over ONE planted world.

        THE DOOR READS THE FOLD NOW, and the fold rides the WATCH pass rather
        than the budget pass this class drives — so each reading is folded
        here explicitly. That is the honest shape: the arm is about what the
        budget snapshot DOES to a send, and the path from one to the other
        runs through the fold."""
        from helm import burnflags
        burnflags.write_snapshot({"ceiling": 90.0, "money": {"codex": rows},
                                  "money_measured_at": {"codex": time.time()},
                                  "upstream": {}, "anthropic_history": None,
                                  "declarations": None})

    def pooled(self, fname="codex-a.json"):
        with open(os.path.join(self.pool, fname), "w") as f:
            json.dump({"type": "codex", "email": "a@x.example",
                       "account_id": "acct-A",
                       "access_token": "FAKE-pool-token"}, f)

    def test_the_pass_publishes_the_empty_census_and_the_send_admits(self):  # noqa: VACUOUS_ASSERTION — the empty reading IS the product law here (an unpooled pool has no accounts), and the arm carries two unconditional positive controls on that same publication: the snapshot file is read back and its `census` field asserted to be the known-empty token, and the SAME budget call that refuses before the pass is asserted to admit after it
        self.mod.write_snapshot(self.capped, ceiling=90.0)
        self.fold(self.capped)
        # THE POSITIVE CONTROL FIRST, and it is the state the bug persisted in:
        # this snapshot really does refuse a codex send right now.
        self.assertFalse(
            self.dispatches._validate_recipient_budget("codex", False)[0])
        # the last account is unpooled — the DIRECTORY lists and is empty
        self.assertEqual(os.listdir(self.pool), [])
        self.assertEqual(proxywatch._codex_budget_pass(), [])
        rows, age = self.mod.cached_budget()
        self.assertEqual(rows, [])
        self.assertLess(age, 60)                 # freshly re-stamped, not aged
        with open(self.mod.snapshot_path(), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["census"], self.mod.CENSUS_EMPTY)
        self.fold(rows)                     # the pass's OWN product, folded
        self.assertEqual(
            self.dispatches._validate_recipient_budget("codex", False),
            (True, None, None))

    def test_a_pool_that_will_not_ENUMERATE_leaves_the_snapshot_alone(self):  # noqa: VACUOUS_ASSERTION — the None is the contract (an unread census publishes nothing) and it is covered unconditionally: the standing snapshot is asserted to still carry longest_pct 100.0 and to still refuse, and the closing must-hit re-runs the same call over a restored directory and asserts it DOES publish
        """The other half, and the one a careless cure breaks: a failed census
        must NOT be relabelled empty. The refusal stands, because nothing
        measured anything."""
        self.mod.write_snapshot(self.capped, ceiling=90.0)
        self.fold(self.capped)
        shutil.rmtree(self.pool)
        with open(self.pool, "w") as f:      # unenumerable, uid-independently
            f.write("not a directory")
        self.assertIsNone(proxywatch._codex_budget_pass())
        rows, _age = self.mod.cached_budget()
        self.assertEqual([r["longest_pct"] for r in rows], [100.0])
        self.assertFalse(
            self.dispatches._validate_recipient_budget("codex", False)[0])
        # THE MUST-HIT: with the directory restored and empty, the very same
        # call DOES clear it — so the survival above is the unread census's
        # doing and not a pass that can never write.
        os.remove(self.pool)
        os.makedirs(self.pool)
        self.assertEqual(proxywatch._codex_budget_pass(), [])
        self.assertEqual(self.mod.cached_budget()[0], [])

    def test_a_POPULATED_pool_still_probes_and_publishes(self):
        """The control on both arms above: the pass writes a real census when
        there is one, so the empty publications are about an empty POOL.

        THE READER IS THE REAL ONE (task/2480 F1). Patching `pool_budget`
        and asserting its return value comes back proves DELEGATION and
        nothing about the pool, and the defect this arm controls for lives
        INSIDE the reader — in what it makes of a directory it enumerated. So
        the only seam faked here is the VENDOR, a recorded body, and the
        assertion is on the SNAPSHOT the pass published."""
        from tests.test_providers import recorded
        self.pooled()
        with mock.patch.object(self.mod, "_get_json",
                               lambda url, headers: recorded("team")):
            rows = proxywatch._codex_budget_pass()
        self.assertEqual([r["file"] for r in rows], ["codex-a.json"])
        self.assertEqual([r["longest_pct"] for r in rows], [100.0])
        with open(self.mod.snapshot_path(), encoding="utf-8") as f:
            published = json.load(f)
        self.assertEqual(published["census"], self.mod.CENSUS_MEASURED)
        self.assertEqual([r["account_id"] for r in published["rows"]],
                         ["acct-A"])

    def test_a_POPULATED_pool_whose_enumeration_FAILS_publishes_NOTHING(self):  # noqa: VACUOUS_ASSERTION — the None is the contract (an unread census publishes nothing) and every leg around it is an unconditional positive: the standing snapshot is asserted to refuse the send BEFORE the pass, to still carry longest_pct 100.0 and still refuse AFTER it, the injected OSError is asserted to have FIRED, and the closing must-hit re-runs the same call over the same directory and asserts it DOES publish a measured census
        """task/2480 F1 at the publication door. The pool was read TWICE — a
        glob for the records, a listdir to classify the empty answer — and a
        transient OSError on the first is swallowed by the glob while the
        second succeeds over the same populated directory. The census then
        DISCARDED those names, called the pool known-empty, and this pass
        published `[]` over a standing all-capped snapshot: a refusal retired
        by an error rather than by any measurement."""
        self.mod.write_snapshot(self.capped, ceiling=90.0)
        self.fold(self.capped)
        # THE POSITIVE CONTROL FIRST, and it is the state that must survive:
        # this snapshot really does refuse a codex send right now.
        self.assertFalse(
            self.dispatches._validate_recipient_budget("codex", False)[0])
        self.pooled()                     # and the pool is NOT empty
        real_listdir, real_scandir = os.listdir, os.scandir
        armed = {"n": 1}

        def _boom(path):
            if armed["n"] and os.fspath(path) == self.pool:
                armed["n"] -= 1
                raise OSError(5, "Input/output error")

        def fake_listdir(path=".", *a, **kw):
            _boom(path)
            return real_listdir(path, *a, **kw)

        def fake_scandir(path=".", *a, **kw):
            _boom(path)
            return real_scandir(path, *a, **kw)

        with mock.patch("os.listdir", fake_listdir), \
                mock.patch("os.scandir", fake_scandir):
            self.assertIsNone(proxywatch._codex_budget_pass())
        self.assertEqual(armed["n"], 0, "the OSError was never raised")
        rows, _age = self.mod.cached_budget()
        self.assertEqual([r["longest_pct"] for r in rows], [100.0],
                         "the standing snapshot was overwritten by a pass "
                         "that read nothing")
        self.assertFalse(
            self.dispatches._validate_recipient_budget("codex", False)[0])
        # THE MUST-HIT: failed, THEN succeeded. The very same call over the
        # very same directory now publishes the POPULATED census
        # — so the silence above is the failed enumeration's doing and not a
        # pass that can no longer write. Blast radius: one pass over one real
        # file with the vendor body recorded, nothing else patched.
        from tests.test_providers import recorded
        with mock.patch.object(self.mod, "_get_json",
                               lambda url, headers: recorded("team")):
            rows = proxywatch._codex_budget_pass()
        self.assertEqual([r["file"] for r in rows], ["codex-a.json"])
        with open(self.mod.snapshot_path(), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["census"], self.mod.CENSUS_MEASURED)


class TheRebuildRecipeReachesTheWriterTest(unittest.TestCase):
    """task/2480 R7 — the projection manifest advertised bare `helm proxywatch`
    as the rebuild recipe for the codex-pool-budget projection, and that verb
    renders health and RETURNS three statements before the budget producer. An
    operator following the printed recovery recipe left the missing or stale
    snapshot exactly as it was and was told nothing."""

    def setUp(self):
        # cmd_proxywatch --post writes watch state: it must never touch the
        # real helm home from a suite.
        self.tmp = tempfile.mkdtemp(prefix="helm-test-recipe-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.envp = mock.patch.dict(
            os.environ, {"HOME": self.tmp,
                         "HELM_HOME": os.path.join(self.tmp, "helm"),
                         "HELM_CACHE_DIR": os.path.join(self.tmp, "cache")})
        self.envp.start()
        self.addCleanup(self.envp.stop)

    def recipe(self):
        from helm import registry
        return next(r for r in registry.projections()
                    if r["name"] == "codex-pool-budget")["rebuild"]

    def test_the_recipe_names_the_posting_pass_and_discloses_its_side_effects(self):
        recipe = self.recipe()
        self.assertIn("--post", recipe)
        # THE SIDE EFFECTS RIDE THE RECIPE: this is not a free local rebuild.
        self.assertIn("probe", recipe)
        self.assertIn("room", recipe)

    @staticmethod
    def _unresolvable(text):
        """Every backticked helm command in TEXT whose root verb the shipped
        parser does not dispatch. Same question the whole-suite scanner asks;
        asked here of exactly the prose this lane wrote."""
        from helm import cli
        bad = []
        for command in re.findall(r"`(helm\s+[^`\n]+)`", text):
            words = command.split()
            if len(words) >= 2 and words[1].strip(".,:;()[]{}") not in cli.VERBS:
                bad.append(command)
        return bad

    def test_the_recipe_prose_names_only_verbs_the_parser_dispatches(self):
        """The R7 prose promised `helm registry`, which is not a root verb —
        an operator typing what the comment printed got 'unknown verb'. The
        positive reads the SHIPPED comment block and the SHIPPED recipe."""
        source = open(proxywatch.__file__, encoding="utf-8").read()
        self.assertIn("the rebuild recipe for", source,
                      "the R7 comment block moved; re-anchor this arm")
        self.assertEqual(self._unresolvable(source), [])
        self.assertEqual(self._unresolvable("`%s`" % self.recipe()), [])
        # CONTROL — blast radius is this string only: nothing else in the suite
        # reads it. It is the sentence as it shipped before the cure, and the
        # resolver still calls it out, so the two assertions above are not
        # vacuous on an extractor that stopped matching.
        self.assertEqual(
            self._unresolvable("which is why `helm registry` names "
                               "`helm proxywatch --post` as the recipe"),
            ["helm registry"])

    def test_the_BARE_verb_never_reaches_the_writer_and_the_posting_one_does(self):
        """The measurement behind the recipe, on the shipped entry point."""
        with mock.patch.object(proxywatch, "health", return_value=rep(row())), \
                mock.patch.object(proxywatch, "_codex_budget_pass") as writer:
            self.assertEqual(proxywatch.cmd_proxywatch([]), 0)
        self.assertFalse(writer.called,
                         "the bare verb must stay a status read")
        with mock.patch.object(proxywatch, "health", return_value=rep(row())), \
                mock.patch.object(proxywatch, "changed", return_value=(False, None)), \
                mock.patch.object(proxywatch, "record", return_value=True), \
                mock.patch.object(proxywatch, "_owner_push", return_value=True), \
                mock.patch.object(proxywatch, "_codex_budget_pass",
                                  return_value=None) as writer:
            proxywatch.cmd_proxywatch(["--post"])
        self.assertTrue(writer.called, "the recipe's own verb must write")


class CodexPoolBudgetRidesThePassTest(unittest.TestCase):
    """task/2480 — the pass surfaces every pooled codex account's windows and
    posts FAMILY-BUDGET-LOW ONCE on the crossing.

    The owner's ask was "dunno how to make those ultra creds last longer", and
    the measurable reason they did not was that nothing on any timer looked at
    the WEEKLY window. This pass is what looks, and the snapshot it writes is
    what the dispatch budget gate reads."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-pw-budget-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # BOTH ROOTS, because this pass touches both: the home carries the
        # snapshot, the declarations and the board, and the cache carries the
        # native usage log the fold reads.
        self.envp = mock.patch.dict(
            os.environ, {"HELM_HOME": os.path.join(self.tmp, "helm"),
                         "HELM_CACHE_DIR": os.path.join(self.tmp, "cache")})
        self.envp.start()
        self.addCleanup(self.envp.stop)

    def rows(self, *pcts):
        out = []
        for i, p in enumerate(pcts):
            out.append({"email": "a%d@x.example" % i, "plan": "team",
                        "state": "unknown" if p is None else "ok",
                        "longest_pct": p, "reached_type": None,
                        "note": "usage endpoint unreachable" if p is None else None,
                        "windows": [] if p is None else
                        [{"label": "7d", "used_percent": p,
                          "reset_after_seconds": 7200},
                         {"label": "5h", "used_percent": 12.0,
                          "reset_after_seconds": 600}]})
        return out

    def test_the_pass_prints_EVERY_window_per_account(self):
        report = rep(row("codex"))
        report["codex_budget"] = self.rows(100.0, 40.0)
        lines = "\n".join(proxywatch.report_lines(report))
        self.assertIn("codex pool budget", lines)
        self.assertIn("HELM_CODEX_WEEKLY_CEILING_PCT", lines)
        self.assertIn("7d 100% resets 2.0h", lines)
        self.assertIn("5h 12% resets 10m", lines)     # BOTH windows, per account
        self.assertIn("a1@x.example", lines)

    def test_an_UNREADABLE_account_prints_unknown_and_never_a_zero(self):
        report = rep(row("codex"))
        report["codex_budget"] = self.rows(None)
        lines = proxywatch.report_lines(report)
        account = next(l for l in lines if "a0@x.example" in l)
        self.assertIn("unknown", account)
        self.assertIn("usage endpoint unreachable", account)
        # NOT A ZERO, AND NOT A PERCENT AT ALL. A 0% on an unreadable account
        # reads as wide open, which is the precise inversion that routes work
        # into a wall. The header line carries the ceiling's own "90%", so the
        # assertion is scoped to the ACCOUNT's line.
        self.assertNotIn("%", account)
        # the control: a READ account on the same renderer does print percents
        report["codex_budget"] = self.rows(44.0)
        read = next(l for l in proxywatch.report_lines(report) if "a0@x.example" in l)
        self.assertIn("44%", read)

    def test_a_pass_that_did_not_read_the_pool_prints_nothing_about_it(self):
        """The control on the two arms above: silence means NOT READ, and it is
        only safe because an account that WAS read and failed prints `unknown`
        explicitly (the arm before this one)."""
        report = rep(row("codex"))
        # THE POSITIVE CONTROL, unconditional and on the same observable: the
        # same renderer over the same report DOES print the section once a
        # budget is present, so the absence below is the missing key's doing.
        report["codex_budget"] = self.rows(40.0)
        self.assertIn("codex pool budget",
                      "\n".join(proxywatch.report_lines(report)))
        report.pop("codex_budget")
        self.assertNotIn("codex pool budget",
                         "\n".join(proxywatch.report_lines(report)))

    def test_the_decision_latch_survives_a_pass_that_did_not_read(self):
        """DEDUP IS ON THE DECISION. A pass with no budget reading must CARRY
        the latch forward — clearing it would re-post FAMILY-BUDGET-LOW on the
        next pass that does read, which is the once-becomes-every-15-minutes
        failure this whole latch exists to prevent."""
        crossed = rep(row("codex"))
        crossed["codex_budget_decision"] = "capped"
        self.assertTrue(proxywatch.record(crossed, prior_state={}))
        with open(proxywatch._state_path(), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["codex_budget"], {"decision": "capped"})
        prior, err = proxywatch._read_watch_state()
        self.assertIsNone(err)
        # a later pass that read nothing (decision None) keeps the latch
        self.assertTrue(proxywatch.record(rep(row("codex")), prior_state=prior))
        with open(proxywatch._state_path(), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["codex_budget"], {"decision": "capped"})
        # THE MUST-HIT: a pass that DID read a different decision replaces it,
        # so the carry-forward above is not simply a field nothing can write.
        moved = rep(row("codex"))
        moved["codex_budget_decision"] = "clear"
        self.assertTrue(proxywatch.record(moved, prior_state=prior))
        with open(proxywatch._state_path(), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["codex_budget"], {"decision": "clear"})

    def test_an_empty_flag_latch_is_a_latch_and_not_an_absent_one(self):
        """`or` cannot tell an EMPTY latch from an ABSENT one. A fold that
        read no family answers {}, and carrying the prior colours forward over
        it means the room never hears the next crossing — while a pass that
        did not fold at all must keep them."""
        crossed = rep(row("codex"))
        crossed["burn_flag_colours"] = {"codex": "RED"}
        crossed["burn_flag_line"] = "codex|RED"
        self.assertTrue(proxywatch.record(crossed, prior_state={}))
        prior, err = proxywatch._read_watch_state()
        self.assertIsNone(err)
        self.assertEqual(prior["burn_flags"]["colours"], {"codex": "RED"})
        # a pass that FOLDED and found no family writes the EMPTY latch
        emptied = rep(row("codex"))
        emptied["burn_flag_colours"] = {}
        emptied["burn_flag_line"] = ""
        self.assertTrue(proxywatch.record(emptied, prior_state=prior))
        with open(proxywatch._state_path(), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["burn_flags"],
                             {"colours": {}, "line": ""})
        # THE MUST-HIT CONTROL: a pass that did NOT fold (None) keeps the
        # prior, so the clearing above is the empty reading and not a latch
        # that forgets on every pass
        self.assertTrue(proxywatch.record(rep(row("codex")),
                                          prior_state=prior))
        with open(proxywatch._state_path(), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["burn_flags"]["colours"],
                             {"codex": "RED"})

    def _flags(self, *pcts):
        """The pass's own rows, folded the way the pass folds them."""
        from helm import burnflags
        return burnflags.fold({"ceiling": 90.0, "money": {"codex": self.rows(*pcts)},
                               "money_measured_at": {"codex": 1789000000.0}},
                              now=1789000000.0)["families"]

    def test_the_reset_blocked_latch_rides_the_same_state(self):
        """THE RESET RUNG'S BLOCKED HALF IS LATCHED TOO, and on the same
        terms: a wall a reset credit cannot lift stands for days while this
        pass runs every fifteen minutes. A pass that did not run the rung at
        all keeps the latch; a pass that ran it and found nothing blocked
        writes the EMPTY one, which is how the room hears the wall again if it
        comes back."""
        blocked = rep(row("codex"))
        blocked["codex_resets_blocked"] = "deadbeefdeadbeef"
        self.assertTrue(proxywatch.record(blocked, prior_state={}))
        with open(proxywatch._state_path(), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["codex_resets"],
                             {"blocked": "deadbeefdeadbeef"})
        prior, err = proxywatch._read_watch_state()
        self.assertIsNone(err)
        # a pass whose rung did not run at all carries it forward
        self.assertTrue(proxywatch.record(rep(row("codex")), prior_state=prior))
        with open(proxywatch._state_path(), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["codex_resets"],
                             {"blocked": "deadbeefdeadbeef"})
        # MUST-HIT: a pass that DID run it and found nothing blocked clears
        # it, so the carry-forward above is not a field nothing can write.
        cleared = rep(row("codex"))
        cleared["codex_resets_blocked"] = ""
        self.assertTrue(proxywatch.record(cleared, prior_state=prior))
        with open(proxywatch._state_path(), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["codex_resets"], {"blocked": ""})

    def test_the_pass_posts_once_and_not_twice_on_one_crossing(self):
        """ONE ANNOUNCER. The tag and the latch survive the move off the codex
        budget's own notice; what dies is the second producer of this post."""
        from helm import burnflags, codexbudget
        self.assertFalse(hasattr(codexbudget, "watch_notice"),
                         "two announcers on one tag is two chances to tell "
                         "the room a different thing")
        self.assertFalse(hasattr(codexbudget, "NOTICE_TAG"))
        body, colours = burnflags.watch_notice(self._flags(95.0, 99.0), None)
        self.assertIn("FAMILY-BUDGET-LOW", body)
        self.assertEqual(colours["codex"], burnflags.RED)
        again, moved = burnflags.watch_notice(self._flags(96.0, 100.0), colours)
        self.assertIsNone(again, "the numbers moved; the colour did not")
        self.assertEqual(moved["codex"], burnflags.RED)
        # THE MUST-HIT CONTROL: a pass where the colour DOES move posts, so
        # the silence above is the latch and not an announcer that never fires
        crossed, _ = burnflags.watch_notice(self._flags(10.0, 20.0), colours)
        self.assertIsNotNone(crossed)

    def test_the_pass_reads_and_writes_only_the_fixture_home(self):
        """THE FOLD RIDES THE WATCHDOG, AND THE WATCHDOG RUNS IN THE SUITE.
        Two paths in this pass were resolved from the REAL home however the
        caller set its environment: the native usage log's, a module constant
        computed at import, and the integration board's, a literal `~` path.
        A fixture run therefore read this machine's live observation log and
        wrote its live board row — and the arm beside this one had to stub the
        reader to stay off it."""
        import builtins
        from helm import board, brief, burnflags, home
        cache = os.path.join(self.tmp, "cache")
        os.makedirs(cache)
        with mock.patch.dict(os.environ, {"HELM_CACHE_DIR": cache}):
            report = rep(row("codex"))
            report["codex_budget"] = self.rows(95.0, 99.0)
            probed = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                   time.gmtime(report["ts"]))
            sentinel = {"provider": burnflags.NATIVE_FAMILY,
                        "account": "acct-a", "probed_at": probed,
                        "status": "allowed",
                        "gauges": [{"label": "7d", "utilization": 0.2,
                                    "reset": report["ts"] + 604800}]}
            with open(os.path.join(cache, "native-usage-history.jsonl"),
                      "w", encoding="utf-8") as fh:
                fh.write(json.dumps(sentinel) + "\n")
            # the fixture's OWN board, with a sentinel row on it: the board
            # writer refuses to CREATE a board, so a fixture that has none
            # would make a silent no-op look like isolation
            os.makedirs(os.path.dirname(board.path()), exist_ok=True)
            with open(board.path(), "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"burn_flags": "planted"}))
            touched, real_open = [], builtins.open

            def spy(file, *a, **kw):
                touched.append(os.path.abspath(str(file)))
                return real_open(file, *a, **kw)

            with mock.patch.object(builtins, "open", spy):
                payload = proxywatch._burn_flags_pass(report)
                proxywatch._burn_flags_board(payload, None)
            # THE FIXTURE'S OWN LOG is what the fold read, with no stub
            self.assertIn(burnflags.NATIVE_FAMILY, payload["readers"]["money"])
            self.assertEqual(payload["families"][burnflags.NATIVE_FAMILY]
                             ["coverage"]["measured"], 1)
            # and the board row landed on the FIXTURE'S board, over the
            # planted sentinel
            self.assertTrue(board.path().startswith(self.tmp + os.sep))
            with open(board.path(), encoding="utf-8") as fh:
                written = json.load(fh)["burn_flags"]
            self.assertNotEqual(written, "planted")
            self.assertIn("burn", written)
            # CONTROL: the spy saw this pass's own files, so an empty sweep
            # below cannot pass for isolation
            self.assertTrue(any(f.startswith(self.tmp + os.sep)
                                for f in touched), touched)
            # NOTHING under the live home or the live cache was opened at
            # all — for reading or for writing. The live roots are named as
            # STRINGS and never visited by this arm.
            live = (home.default_home(), board.DEFAULT,
                    os.path.dirname(brief.USAGE_HISTORY))
            for path in touched:
                for root in live:
                    self.assertFalse(
                        path == root or path.startswith(root + os.sep),
                        "the pass touched %s under the live %s" % (path, root))

    def test_empty_generic_reader_snapshot_leaves_native_and_codex_unchanged(self):  # noqa: VACUOUS_ASSERTION — the declared family list is asserted non-empty before the loop asserts each one's GREY colour and unreadable cause
        """An empty generic snapshot is an empty set of readings, not a zero
        reading and not authority to replace either inherited money source.
        Every catalog-declared family it names reads UNREAD from it (task/2936
        declared the first ones), never a number."""
        from helm import burnflags
        report = rep(row("codex"))
        report["codex_budget"] = self.rows(20.0, 30.0)
        probed = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                               time.gmtime(report["ts"]))
        history = [{"provider": burnflags.NATIVE_FAMILY, "account": "acct-a",
                    "probed_at": probed, "status": "allowed",
                    "gauges": [{"label": "7d", "utilization": 0.2,
                                "reset": report["ts"] + 604800}]}]
        empty = {"v": 1, "ts": report["ts"], "readings": []}
        with mock.patch.object(burnflags, "usage_history", return_value=history):
            absent = proxywatch._burn_flags_pass(report)
            report["money_readers"] = empty
            present = proxywatch._burn_flags_pass(report)
        for family in ("codex", burnflags.NATIVE_FAMILY):
            self.assertEqual(present["families"][family],
                             absent["families"][family])
        self.assertEqual(present["readers"]["money"],
                         list(burnflags.money_readers()))
        declared = [f for f in burnflags.money_readers()
                    if f not in ("codex", burnflags.NATIVE_FAMILY)]
        self.assertTrue(declared, "the catalog declares a generic reader")
        for family in declared:
            self.assertEqual(present["families"][family]["colour"],
                             burnflags.GREY)
            self.assertEqual(present["families"][family]["cause_id"],
                             "money:unreadable")

    def test_burn_fold_joins_owner_reset_before_later_record_composition(self):
        report = rep(row("codex"))
        report["codex_budget"] = self.rows(20.0)
        report["upstream"] = {"codex": {
            "state": proxywatch._QUOTA_WALL, "dark": True,
            "since": "2026-01-01T00:00:00Z"}}
        reset = int((report["ts"] + 3600) * 1000)
        owner = {"codex": {"resets_at_ms": reset,
                           "reset_kind": "vendor", "reset_source": "owner"}}
        with mock.patch.object(proxywatch, "read_vendor_resets",
                               return_value=(owner, None)):
            payload = proxywatch._burn_flags_pass(report)
        flag = payload["families"]["codex"]
        self.assertEqual(flag["cause_id"], "money:vendor-quota-wall")
        self.assertEqual(flag["expires_at"], reset / 1000.0)
        self.assertIn("owner", flag["expires_source"])
        # task/2935: the flag this pass writes and the record `record`
        # composes from the SAME report agree, member by member. A walled
        # member with a measured reset older than the owner's entry and one
        # with none: the owner covers both, and a PAST owner horizon covers
        # neither.
        observed = proxywatch._iso(report["ts"] - 600)
        seats = {"seat-a": {"state": proxywatch._QUOTA_WALL,
                            "quota_wall": proxywatch._QUOTA_WALL,
                            "wall_observed_at": observed,
                            "resets_at_ms": int((report["ts"] + 600) * 1000),
                            "reset_source": "canary"},
                 "seat-b": {"state": proxywatch._QUOTA_WALL,
                            "quota_wall": proxywatch._QUOTA_WALL,
                            "wall_observed_at": observed}}
        report["upstream"]["codex"]["seats"] = seats
        recorded = proxywatch._iso(report["ts"] - 60)
        for horizon, expected in ((reset, reset),
                                  (int((report["ts"] - 5) * 1000), None)):
            with self.subTest(horizon=horizon):
                table = {"codex": dict(owner["codex"], resets_at_ms=horizon,
                                       recorded_at=recorded)}
                with mock.patch.object(proxywatch, "read_vendor_resets",
                                       return_value=(table, None)):
                    flag = proxywatch._burn_flags_pass(report)[
                        "families"]["codex"]
                    durable, _err = proxywatch._compose_upstream_records(
                        report, {})
                self.assertEqual(durable["codex"].get("resets_at_ms"),
                                 expected)
                self.assertEqual(flag["expires_at"],
                                 None if expected is None
                                 else expected / 1000.0)

    def test_the_pass_writes_the_snapshot_and_the_board_line(self):
        """The fold RIDES THIS PASS and shares its clock: the readings are
        already in hand here, and folding them anywhere else would mint a
        second reading of the same world at a second instant."""
        from helm import burnflags
        report = rep(row("codex"))
        report["codex_budget"] = self.rows(95.0, 99.0)
        # NO LIVE PATH AND NO STUB: the fixture's cache root holds no usage
        # log, so the native reader answers nothing through its own door
        payload = proxywatch._burn_flags_pass(report)
        self.assertEqual(payload["families"]["codex"]["colour"], burnflags.RED)
        # CONTROL on the same observable: the codex reader IS named there
        # (beside every catalog-declared reader, unread from a pass that
        # carried no generic snapshot), so the absent native one is a reader
        # with nothing to read and not an empty field
        self.assertEqual(payload["readers"]["money"],
                         [family for family in burnflags.money_readers()
                          if family != burnflags.NATIVE_FAMILY])
        self.assertIn("codex", payload["readers"]["money"])
        flags, age = burnflags.cached_flags(now=report["ts"])
        self.assertEqual(flags["codex"]["colour"], burnflags.RED)
        self.assertIsNotNone(age)
        # the board row is ONE SCALAR STRING and is written only when it moves
        posted = []
        with mock.patch.object(proxywatch, "_burnflags") as mod:
            mod.return_value.line.return_value = "burn RED (codex, in 2.0h)"
            with mock.patch("helm.board.set_value",
                            side_effect=lambda *a, **k: posted.append((a, k))
                            or (True, None)):
                line = proxywatch._burn_flags_board(payload, None)
                proxywatch._burn_flags_board(payload, line)
        self.assertEqual(len(posted), 1, "a board write per pass is noise; "
                         "the row moves when the colour does")
        (key, value), kwargs = posted[0]
        self.assertEqual(key, "burn_flags")
        self.assertIsInstance(value, str)
        self.assertTrue(kwargs["allow_new"])

    def test_the_board_row_is_latched_on_the_reading_not_on_the_countdown(self):
        """THE LINE CARRIES A COUNTDOWN. Latching the board write on the
        rendered line makes every tick of the clock a change, so the row is
        written on every pass — the per-pass noise the latch exists to stop.
        A pass with no fold latches NOTHING and keeps the prior key."""
        from helm import burnflags
        report = rep(row("codex"))
        report["codex_budget"] = self.rows(95.0, 99.0)
        with mock.patch.object(burnflags, "usage_history", return_value=[]):
            payload = proxywatch._burn_flags_pass(report)
        posted = []
        with mock.patch("helm.board.set_value",
                        side_effect=lambda *a, **k: posted.append(a)
                        or (True, None)):
            key = proxywatch._burn_flags_board(payload, None)
            # a LATER tick of the same reading: the rendered line has moved
            # on by half an hour, the reading has not
            with mock.patch.object(burnflags, "line",
                                   side_effect=lambda snap, now=None:
                                   "burn RED; tightest codex, changes in 30m"):
                again = proxywatch._burn_flags_board(payload, key)
        self.assertEqual(again, key)
        self.assertEqual(len(posted), 1, "two ticks of one reading wrote the "
                         "board row twice")
        # THE MUST-HIT: a reading that MOVED writes, so the silence above is
        # the latch and not a row nothing can write
        with mock.patch.object(burnflags, "line_key", return_value="moved"):
            with mock.patch("helm.board.set_value",
                            side_effect=lambda *a, **k: posted.append(a)
                            or (True, None)):
                self.assertEqual(proxywatch._burn_flags_board(payload, key),
                                 "moved")
        self.assertEqual(len(posted), 2)
        # a pass that did not fold latches nothing at all
        self.assertIsNone(proxywatch._burn_flags_board(None, key))

    def test_a_fold_that_raises_never_takes_the_watchdog_down(self):
        from helm import burnflags
        report = rep(row("codex"))
        report["codex_budget"] = self.rows(95.0)
        with mock.patch.object(burnflags, "usage_history", return_value=[]):
            # CONTROL: the same call with a working fold returns a payload
            self.assertIsNotNone(proxywatch._burn_flags_pass(report))
            with mock.patch.object(burnflags, "fold",
                                   side_effect=RuntimeError("fold exploded")):
                self.assertIsNone(proxywatch._burn_flags_pass(report))

    def test_a_reader_that_raises_never_takes_the_watchdog_down(self):
        from helm import codexbudget
        # THE POSITIVE CONTROL: with a pool present and a reader that answers,
        # the pass DOES return rows — so the Nones below are the failure and
        # the empty pool, not a pass that can only ever return None.
        with mock.patch.object(codexbudget, "pool_accounts",
                               return_value=[{"email": "a@x", "file": "f"}]), \
                mock.patch.object(codexbudget, "pool_budget",
                                  return_value=[{"longest_pct": 5.0}]):
            self.assertEqual(proxywatch._codex_budget_pass(),
                             [{"longest_pct": 5.0}])
        with mock.patch.object(codexbudget, "pool_accounts",
                               side_effect=RuntimeError("pool exploded")):
            self.assertIsNone(proxywatch._codex_budget_pass())
        # A POOL THAT WILL NOT ENUMERATE IS None — nothing is known, so nothing
        # is published and the standing snapshot is left exactly as it was.
        with mock.patch.object(codexbudget, "pool_census",
                               return_value=([], codexbudget.CENSUS_UNREAD)):
            self.assertIsNone(proxywatch._codex_budget_pass())
        # A POOL THAT IS KNOWN EMPTY IS NOT None AND NOT UNREAD (task/2480 R3).
        # It is the measurement that retires an older refusal, so the pass
        # returns an EMPTY READING here and silence only for an unread census.
        with mock.patch.object(codexbudget, "pool_census",
                               return_value=([], codexbudget.CENSUS_EMPTY)):
            self.assertEqual(proxywatch._codex_budget_pass(), [])


class ProxyUsagePassTest(unittest.TestCase):
    """The sidecar meter rides the POSTING pass and only that pass: a pop
    empties the queue, so the free status read must never take it."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="helm-test-proxy-usage-pass-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.env = mock.patch.dict(os.environ, {
            "HELM_HOME": os.path.join(self.d, "helm-home"),
            "HELM_CHAT_DIR": os.path.join(self.d, "chat"),
            "HELM_CHAT_NAME": "measured-seat"})
        self.env.start()
        self.addCleanup(self.env.stop)
        os.makedirs(os.environ["HELM_HOME"])
        os.makedirs(os.environ["HELM_CHAT_DIR"])
        self.state_patch = mock.patch.object(
            proxywatch, "_state_path",
            return_value=os.path.join(self.d, "proxywatch.json"))
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)

    def test_usage_is_persisted_before_money_ledger_reads(self):
        order = []
        report = {"ts": 1000}
        with mock.patch.object(proxywatch, "_proxy_usage_pass",
                               side_effect=lambda: order.append("usage") or [1]), \
                mock.patch.object(proxywatch, "_money_pass",
                                  side_effect=lambda _now:
                                  order.append("money") or {"v": 1}):
            self.assertEqual(proxywatch._usage_then_money(report), [1])
        self.assertEqual(order, ["usage", "money"])
        self.assertEqual(report["proxy_usage"], [1])
        self.assertEqual(report["money_readers"], {"v": 1})

    def test_post_pops_the_meter_once_and_the_bare_verb_never_does(self):
        rows = [{"seat": "codex-41", "family": "codex", "status": "READ",
                 "reason": None, "records": 3, "pid": 1, "port": 1, "appended": True}]
        out = io.StringIO()
        with mock.patch.object(proxywatch, "health", return_value=rep(row())), \
                mock.patch.object(proxywatch, "record", return_value=True), \
                mock.patch.object(proxywatch, "_cred_follow_pass", return_value=None), \
                mock.patch.object(proxywatch, "_codex_budget_pass", return_value=None), \
                mock.patch.object(proxywatch, "read_vendor_resets",
                                  return_value=({}, None)), \
                mock.patch.object(proxywatch, "_owner_push", return_value=True), \
                mock.patch.object(proxywatch, "_proxy_usage_pass",
                                  return_value=rows) as pop, \
                mock.patch("helm.chat.post"), contextlib.redirect_stdout(out):
            proxywatch.cmd_proxywatch([])
            pop.assert_not_called()
            proxywatch.cmd_proxywatch(["--post", "--json"])
        self.assertEqual(pop.call_count, 1)
        text = out.getvalue()
        self.assertIn('"proxy_usage"', text)
        self.assertIn('"seat": "codex-41"', text)
        self.assertIn('"records": 3', text)

    def test_a_refused_append_in_the_pass_renders_FAILED_PERSIST_as_its_own_state(self):
        """F1 through proxywatch: the sidecar leg answers READ with one
        record, the real ledger append is refused at the writer the module
        calls, and the --post text render carries a proxy-usage line that
        says FAILED-PERSIST with the lost count — never READ, never silent.
        Control: the same pass with the writer intact prints no
        proxy-usage line (an all-READ pass is silent) and the ledger holds
        the record."""
        from helm import eventledger, proxy_usage
        record = {"timestamp": "2031-01-02T03:04:05Z", "model": "m", "request_id": "r1",
                  "token_breakdown": {"input": {"total_tokens": 1},
                                      "output": {"total_tokens": 1}}}
        read = (proxy_usage.READ, None, [record], 7, 1)
        common = [
            mock.patch.object(proxywatch, "health", return_value=rep(row())),
            mock.patch.object(proxywatch, "record", return_value=True),
            mock.patch.object(proxywatch, "_cred_follow_pass", return_value=None),
            mock.patch.object(proxywatch, "_codex_budget_pass", return_value=None),
            mock.patch.object(proxywatch, "read_vendor_resets", return_value=({}, None)),
            mock.patch.object(proxywatch, "_owner_push", return_value=True),
            mock.patch("helm.chat.post"),
            mock.patch.object(proxy_usage, "instances",
                              return_value=[("codex", "codex-41", self.d)]),
            mock.patch.object(proxy_usage, "read_seat", return_value=read)]
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            for patch in common:
                stack.enter_context(patch)
            stack.enter_context(contextlib.redirect_stdout(out))
            proxywatch.cmd_proxywatch(["--post"])
        self.assertNotIn("proxy-usage", out.getvalue())
        self.assertEqual(sum(e["kind"] == "request"
                             for e in eventledger.events(proxy_usage.ledger_path())), 1)
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            for patch in common:
                stack.enter_context(patch)
            stack.enter_context(mock.patch.object(eventledger, "append_unlocked",
                                                  return_value=False))
            stack.enter_context(contextlib.redirect_stdout(out))
            proxywatch.cmd_proxywatch(["--post"])
        self.assertIn("helm proxywatch: proxy-usage codex-41 FAILED-PERSIST — "
                      "1 of 1 record(s) not persisted", out.getvalue())
        self.assertNotIn("READ", out.getvalue().split("proxy-usage codex-41")[1].split("\n")[0])

    def test_a_refused_append_in_the_timed_pass_exits_1_and_reaches_the_outbox_and_the_post(self):
        """F2 through the real posting path: the sidecar leg answers READ
        with one record and the real ledger append is refused at the writer
        the module calls. The pass exits 1; the FIRST `record` write (the
        outbox, spied on the real function) carries a pending_chat body with
        the same proxy-usage FAILED-PERSIST line stdout printed; `chat.post`
        (stubbed the way every posting arm here stubs it) receives that body;
        and the watch state on disk carries the meter row. Control: the same
        pass with the writer intact exits 0 and no posted body mentions
        proxy-usage."""
        from helm import eventledger, proxy_usage
        record = {"timestamp": "2031-01-02T03:04:05Z", "model": "m", "request_id": "r1",
                  "token_breakdown": {"input": {"total_tokens": 1},
                                      "output": {"total_tokens": 1}}}
        read = (proxy_usage.READ, None, [record], 7, 1)
        real_record = proxywatch.record
        writes = []

        def spy(rep, pending_chat=None, pending_ntfy=None, prior_state=None):
            writes.append(list(pending_chat or ()))
            return real_record(rep, pending_chat=pending_chat,
                               pending_ntfy=pending_ntfy, prior_state=prior_state)

        real_append = eventledger.append_unlocked

        def refuse_marker(path, row):
            return False if row.get("kind") == "read" else real_append(path, row)

        def run(refuse):
            writes.clear()
            out = io.StringIO()
            with contextlib.ExitStack() as stack:
                for patch in (
                        mock.patch.object(proxywatch, "health", return_value=rep(row())),
                        mock.patch.object(proxywatch, "record", side_effect=spy),
                        mock.patch.object(proxywatch, "_cred_follow_pass", return_value=None),
                        mock.patch.object(proxywatch, "_codex_budget_pass", return_value=None),
                        mock.patch.object(proxywatch, "read_vendor_resets",
                                          return_value=({}, None)),
                        mock.patch.object(proxywatch, "_owner_push", return_value=True),
                        mock.patch.object(proxy_usage, "instances",
                                          return_value=[("codex", "codex-41", self.d)]),
                        mock.patch.object(proxy_usage, "read_seat", return_value=read)):
                    stack.enter_context(patch)
                if refuse == "marker":
                    stack.enter_context(mock.patch.object(
                        eventledger, "append_unlocked", refuse_marker))
                elif refuse:
                    stack.enter_context(mock.patch.object(
                        eventledger, "append_unlocked", return_value=False))
                post = stack.enter_context(mock.patch("helm.chat.post"))
                stack.enter_context(contextlib.redirect_stdout(out))
                rc = proxywatch.cmd_proxywatch(["--post"])
            return rc, out.getvalue(), [c.args[0] for c in post.call_args_list]

        rc, text, posted = run(refuse=False)
        self.assertEqual(rc, 0)
        self.assertFalse([b for b in posted if "proxy-usage" in b], posted)
        self.assertNotIn("proxy-usage", text)
        rc, text, posted = run(refuse=True)
        self.assertEqual(rc, 1)
        line = ("helm proxywatch: proxy-usage codex-41 FAILED-PERSIST — "
                "1 of 1 record(s) not persisted")
        self.assertIn(line, text)
        self.assertEqual(len([b for b in posted if line in b]), 1)
        self.assertTrue(writes and any(line in body for body in writes[0]), writes)
        state, err = proxywatch._read_watch_state()
        self.assertIsNone(err)
        self.assertEqual(state["proxy_usage"][0]["status"], proxy_usage.FAILED_PERSIST)
        # the header of a pass that lost records says LOST (the wording
        # control for the marker-only run below)
        self.assertIn("popped records the ledger refused are LOST",
                      [b for b in posted if line in b][0])
        # MARKER-ONLY REFUSAL: every record persisted, the read marker did
        # not. Status FAILED-PERSIST with lost 0, and every surface — stdout,
        # the posted body, the outbox write — says exactly that; the word
        # LOST (the capitalised records-lost header) appears nowhere.
        rc, text, posted = run(refuse="marker")
        self.assertEqual(rc, 1)
        marker_line = ("helm proxywatch: proxy-usage codex-41 FAILED-PERSIST — "
                       "read marker not persisted, 0 of 1 record(s) lost")
        self.assertIn(marker_line, text)
        body = [b for b in posted if marker_line in b]
        self.assertEqual(len(body), 1)
        self.assertIn("the read marker was not persisted, 0 record(s) lost", body[0])
        self.assertNotIn("LOST", body[0])
        self.assertNotIn("LOST", text)
        self.assertTrue(any(marker_line in b and "LOST" not in b for b in writes[0]),
                        writes)

    def test_a_broken_reader_never_takes_the_watch_down(self):
        err = io.StringIO()
        with mock.patch("helm.proxy_usage.snapshot", side_effect=RuntimeError("boom")), \
                contextlib.redirect_stderr(err):
            self.assertIsNone(proxywatch._proxy_usage_pass())
        self.assertIn("proxy-usage rung failed (RuntimeError: boom)", err.getvalue())



class StaleCooldownRowTest(unittest.TestCase):
    """The STALE-COOLDOWN row: a sidecar holding a codex
    credential in cooldown while this pass measured headroom on it. The
    sidecar's belief is in its memory only (nothing persisted, nothing
    re-probed at start), so the pass that reads BOTH the roster and the
    vendor window is the one instrument that can see the contradiction.
    A row, never a bounce."""

    NOW = 1_789_500_000.0

    def _entry(self, name="codex-d@example.com-team.json", email="d@example.com",
               reset_in=4 * 3600, kind="codex"):
        from datetime import datetime, timezone
        stamp = datetime.fromtimestamp(self.NOW + reset_in, timezone.utc)
        return {"type": kind, "provider": kind, "name": name, "email": email,
                "status": "error", "unavailable": True,
                "next_retry_after": stamp.strftime("%Y-%m-%dT%H:%M:%S.123456789Z")}

    def _budget(self, state="ok", pct=4.0, files=("codex-d@example.com-team.json",),
                email="d@example.com"):
        return {"files": list(files), "file": files[0], "email": email,
                "state": state, "binding": {"label": "7d", "used_percent": pct,
                                            "reset_after_seconds": 500_000}}

    def test_a_cooling_credential_with_measured_headroom_is_a_row(self):
        rows = proxywatch.codex_cooldown_rows(
            [("seat-a", 8321, [self._entry()])], [self._budget()], now=self.NOW)
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        self.assertEqual((row["seat"], row["port"], row["file"], row["email"]),
                         ("seat-a", 8321, "codex-d@example.com-team.json", "d@example.com"))
        self.assertEqual(row["proxy_reset_in_s"], 4 * 3600)
        self.assertEqual((row["measured_state"], row["measured_pct"],
                          row["measured_window"]), ("ok", 4.0, "7d"))
        text = proxywatch._cooldown_text(row)
        # the credential, the proxy's belief, the measured headroom WITH
        # POLARITY — never a bare percentage
        self.assertIn("seat-a holds codex-d@example.com-team.json (d@example.com)", text)
        self.assertIn("another 4h00m", text)
        self.assertIn("used 4% of 7d", text)
        self.assertNotIn("headroom 4", text)
        self.assertIn("only reports", text)

    def test_a_measured_wall_is_not_a_contradiction(self):
        """CONTROL: the same belief over a credential the vendor ALSO says is
        exhausted is the proxy being right."""
        rows = proxywatch.codex_cooldown_rows(
            [("seat-a", 8321, [self._entry()])],
            [self._budget(state="exhausted", pct=100.0)], now=self.NOW)
        self.assertEqual(rows, [])
        # and the positive arm on the same inputs with the window clear
        self.assertEqual(len(proxywatch.codex_cooldown_rows(
            [("seat-a", 8321, [self._entry()])],
            [self._budget(state="near", pct=85.0)], now=self.NOW)), 1)

    def test_an_elapsed_belief_an_unread_window_and_a_foreign_entry_are_silent(self):
        entries = [self._entry(reset_in=-60),                      # elapsed
                   self._entry(name="codex-x.json", email="x@x", kind="gemini"),
                   {"type": "codex", "name": "codex-nobelief.json",
                    "email": "n@x", "status": "active"}]            # no cooldown
        budget = [self._budget(), self._budget(files=("codex-x.json",), email="x@x"),
                  self._budget(files=("codex-nobelief.json",), email="n@x")]
        self.assertEqual(proxywatch.codex_cooldown_rows(
            [("seat-a", 8321, entries)], budget, now=self.NOW), [])
        # an UNKNOWN budget row (token rejected, body unreadable) contradicts
        # nothing — an unread window is not headroom
        self.assertEqual(proxywatch.codex_cooldown_rows(
            [("seat-a", 8321, [self._entry()])],
            [self._budget(state="unknown", pct=None)], now=self.NOW), [])
        # the join falls back to EMAIL when the roster's file name is not a
        # pool file this pass read
        rows = proxywatch.codex_cooldown_rows(
            [("seat-a", 8321, [self._entry(name="codex-other-spelling.json")])],
            [self._budget()], now=self.NOW)
        self.assertEqual([r["email"] for r in rows], ["d@example.com"])

    def test_the_row_is_a_finding_and_a_report_line(self):
        rows = proxywatch.codex_cooldown_rows(
            [("seat-a", 8321, [self._entry()])], [self._budget()], now=self.NOW)
        rep = {"seats": [], "upstream": {}, "codex_cooldown": rows}
        hits = [t for kind, t in proxywatch.findings(rep)
                if kind == proxywatch.STALE_COOLDOWN]
        self.assertEqual(len(hits), 1, hits)
        lines = [l for l in proxywatch.report_lines(rep) if "STALE-COOLDOWN" in l]
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("codex-d@example.com-team.json", lines[0])
        # CONTROL: no rows, no finding
        self.assertEqual([k for k, _t in proxywatch.findings(
            {"seats": [], "upstream": {}, "codex_cooldown": []})
            if k == proxywatch.STALE_COOLDOWN], [])

    def test_go_stamps_parse_with_nanoseconds_z_and_offsets(self):
        self.assertEqual(proxywatch._go_time_epoch("2026-09-16T08:04:53.123456789Z"),
                         1789545893.123456)
        self.assertEqual(proxywatch._go_time_epoch("2026-09-16T08:04:53-07:00"),
                         1789571093.0)
        self.assertIsNone(proxywatch._go_time_epoch("not a time"))
        self.assertIsNone(proxywatch._go_time_epoch(None))

    def test_the_pass_reads_every_sidecar_and_never_raises(self):
        """The I/O wrapper: sidecars come from proxy_usage.instances, the
        roster from the management endpoint, and a failure anywhere is a
        stderr line, never an exception through the watch."""
        calls = []

        def fake_auth_files(port, secret, timeout=2.0):
            calls.append(port)
            return [self._entry()], None
        tmp = tempfile.mkdtemp(prefix="helm-test-cooldown-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with open(os.path.join(tmp, "config.yaml"), "w") as f:
            f.write("port: 8321\n")
        with open(os.path.join(tmp, "mgmt.token"), "w") as f:
            f.write("not-printed\n")
        with mock.patch("helm.proxy_usage.instances",
                        return_value=[("codex", "seat-a", tmp)]), \
                mock.patch.object(proxywatch, "_auth_files", fake_auth_files), \
                mock.patch.object(seat, "_port_open", return_value=True), \
                mock.patch("time.time", return_value=self.NOW):
            rows = proxywatch._codex_cooldown_pass([self._budget()])
        self.assertEqual(calls, [8321])
        self.assertEqual([r["seat"] for r in rows], ["seat-a"])
        # no budget read: nothing to contradict, and the rung says so with None
        self.assertIsNone(proxywatch._codex_cooldown_pass(None))
        err = io.StringIO()
        with mock.patch("helm.proxy_usage.instances", side_effect=RuntimeError("boom")), \
                contextlib.redirect_stderr(err):
            self.assertIsNone(proxywatch._codex_cooldown_pass([self._budget()]))
        self.assertIn("stale-cooldown rung failed (RuntimeError: boom)", err.getvalue())


class ResolvedModelIsLegibleTest(unittest.TestCase):
    """WHICH MODEL ANSWERED, rendered from the proof and never from the alias.

    THE CONTROL THIS SUITE IS BUILT AROUND: one claude-side alias, two
    providers, two upstream ids. A renderer that reads the alias — the one
    field always present and always the same — passes every arm about a
    healthy seat and says nothing about the case the whole cure exists for.
    So every assertion below runs the SAME alias down both rungs and requires
    the two renderings to differ.
    """

    # EVERY NAME BELOW IS A CATALOG FAMILY OR A PROVIDER BLOCK, which is what
    # these arms are ABOUT; a house-convention placeholder would resolve to no
    # family, no route and no rung, so the whole suite would assert nothing.
    #
    # THE TWO ROUTES ARE THE SHARPEST CONTROL THE TABLE ALLOWS: same alias AND
    # same upstream model, different PROVIDER and different RUNG. A renderer
    # reading either the alias or the model alone renders them identically,
    # and the money question — is this seat costing per turn right now —
    # depends entirely on the field it dropped.
    FAMILY = "ds4pro"          # noqa: SEAT_NAME — the catalog family under test
    OTHER_FAMILY = "claude"    # noqa: SEAT_NAME — a family declaring no pool
    FREE = {"alias": "ds4-pro", "provider": "opencode-go",
            "upstream_model": "deepseek-v4-pro",
            "base_url": "https://opencode.ai/zen/go/v1"}
    PAID = {"alias": "ds4-pro", "provider": "deepseek-direct",
            "upstream_model": "deepseek-v4-pro",
            "base_url": "https://api.deepseek.com/v1"}

    def test_the_same_alias_on_two_rungs_renders_two_different_routes(self):
        free = proxywatch.resolved_from_proof(runtime_proof(route=self.FREE))
        paid = proxywatch.resolved_from_proof(runtime_proof(route=self.PAID))
        self.assertEqual((free["model"], free["upstream_model"]),
                         (paid["model"], paid["upstream_model"]),
                         "the control's premise: alias AND model are equal")
        free_text = proxywatch.resolved_model_phrase(free, self.FAMILY)
        paid_text = proxywatch.resolved_model_phrase(paid, self.FAMILY)
        self.assertNotEqual(free_text, paid_text)
        self.assertEqual(
            free_text, "ds4-pro -> opencode-go/deepseek-v4-pro (free)")
        self.assertEqual(
            paid_text, "ds4-pro -> deepseek-direct/deepseek-v4-pro (paid)")

    def test_a_proof_this_module_cannot_shape_resolves_to_nothing(self):  # noqa: VACUOUS_ASSERTION — the assertIsNotNone below IS the unconditional positive control, on the same call one field away
        # MUST-HIT CONTROL: the same call one field away from valid, so the
        # None below is a REFUSAL and not an unreachable code path.
        self.assertIsNotNone(
            proxywatch.resolved_from_proof(runtime_proof(route=self.PAID)))
        broken = dict(runtime_proof(route=self.PAID), config_sha256="short")
        self.assertIsNone(proxywatch.resolved_from_proof(broken))

    def test_an_undeclared_rung_is_rendered_as_no_claim_about_money(self):
        """A family that declares no cost for a provider gets no cost word."""
        resolved = {"model": "k3", "provider": "moonshot",
                    "upstream_model": "kimi-k3"}
        self.assertEqual(proxywatch.resolved_model_phrase(resolved, "kimi"),
                         "k3 -> moonshot/kimi-k3")
        # and the positive control on the same renderer: a DECLARED rung does
        # print, so the absence above is the catalog speaking, not a dead arm
        self.assertIn("(paid)", proxywatch.resolved_model_phrase(
            proxywatch.resolved_from_proof(runtime_proof(route=self.PAID)),
            self.FAMILY))

    def test_an_alias_equal_to_its_upstream_is_not_printed_twice(self):
        resolved = {"model": "kimi-k3", "provider": "moonshot",
                    "upstream_model": "kimi-k3"}
        self.assertEqual(proxywatch.resolved_model_phrase(resolved),
                         "moonshot/kimi-k3")

    def test_a_half_read_route_says_UNRESOLVED_rather_than_the_alias(self):
        """The alias alone is the answer that is always available and never
        means anything, so a record missing its route says so out loud."""
        self.assertEqual(
            proxywatch.resolved_model_phrase({"model": "ds4-pro",
                                              "provider": None,
                                              "upstream_model": None}),
            "ds4-pro -> UNRESOLVED")
        self.assertEqual(proxywatch.resolved_model_phrase({}), "? -> UNRESOLVED")
        self.assertEqual(proxywatch.resolved_model_phrase(None), "")

    def test_the_catalog_rung_is_asked_about_the_block_not_the_route(self):
        """A cost word in the route dict would break proof->family matching.

        The two declared rungs asserted below are the unconditional positive
        controls for the two None assertions that follow them: a lookup that
        answered None for everything could not pass this arm.
        """
        from helm import seat_catalog
        route = dict(self.PAID)
        paid_block = self.PAID["provider"]   # deepseek-direct, the paid rung
        self.assertIn(route, seat_catalog.proxy_routes(self.FAMILY))
        self.assertNotIn("rung", route)
        self.assertEqual(seat_catalog.provider_rung(self.FAMILY, paid_block),
                         "paid")
        self.assertEqual(seat_catalog.provider_rung(self.FAMILY, "opencode-go"),
                         "free")
        self.assertIsNone(
            seat_catalog.provider_rung(self.FAMILY, "no-such-block"))
        self.assertIsNone(
            seat_catalog.provider_rung(self.OTHER_FAMILY, paid_block))


# EVERY LINE BELOW IS VERBATIM from a live proxy.log — the request ids are
# per-request hashes and the address is localhost, so nothing here is
# sensitive and nothing here is imagined. A fixture that invents its input
# tests no world, and the whole defect this reader cures was that helm had
# never read one of these rows.
EMPTY_EXHAUSTED = (
    '[2026-09-21 16:42:05] [8ed50493] [info ] [gin_logger.go:161] 200 |    '
    '   17.289s |       127.0.0.1 | POST    "/v1/messages?beta=true" | '
    'stream_v1=committed terminal=message_stop text_bytes=54 tool_uses=0 '
    'empty_turn=1 last_input=user_text empty_recovery=exhausted '
    'empty_retry_n=2')
EMPTY_RECOVERED = (
    '[2026-09-21 16:56:40] [01c7f049] [info ] [gin_logger.go:161] 200 |    '
    '   10.576s |       127.0.0.1 | POST    "/v1/messages?beta=true" | '
    'stream_v1=committed terminal=message_stop text_bytes=40 tool_uses=0 '
    'empty_recovery=recovered empty_retry_n=1')
# The scope gap: a non-codex seat's zero-delivery row. Committed, stopped
# cleanly, nothing delivered, and NO empty-turn token — the instrument covers
# the codex path only.
ZERO_DELIVERY_UNFLAGGED = (
    '[2026-09-16 13:54:44] [061d6da3] [info ] [gin_logger.go:161] 200 |    '
    '    7.193s |       127.0.0.1 | POST    "/v1/messages?beta=true" | '
    'stream_v1=committed terminal=message_stop text_bytes=0 tool_uses=0')
DELIVERED = (
    '[2026-09-21 16:40:00] [3c1d9a77] [info ] [gin_logger.go:161] 200 |    '
    '    9.001s |       127.0.0.1 | POST    "/v1/messages?beta=true" | '
    'stream_v1=committed terminal=message_stop text_bytes=2048 tool_uses=1')


class EmptyTurnReaderTest(unittest.TestCase):
    """The consumer half. The proxy emitted these tokens for months and
    `git grep empty_recovery` over helm returned nothing, so a 200 that
    delivered no bytes read as a healthy completed turn on every surface."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)

    def _log(self, *lines):
        p = os.path.join(self.d, "proxy.log")
        with open(p, "w") as f:
            f.write("\n".join(lines) + "\n")
        return p

    def test_the_real_exhausted_row_yields_its_three_spent_generations(self):
        """An exhausted row is one generation plus two retries, all for zero
        delivered bytes. Codex holds the smallest window in the fleet, so the
        count that matters is generations spent, not turns seen."""
        d = proxywatch._stream_evidence(EMPTY_EXHAUSTED)
        self.assertEqual((d["stream"], d["terminal"]),
                         ("committed", "message_stop"))
        self.assertEqual((d["empty_turn"], d["last_input"]),
                         (True, "user_text"))
        self.assertEqual((d["recovery"], d["retry_n"]), ("exhausted", 2))
        observed = proxywatch.log_observation(self._log(EMPTY_EXHAUSTED))
        self.assertEqual(observed["empty_turns"]["wasted_generations"], 3)
        self.assertEqual(observed["empty_turns"]["recovery"], {"exhausted": 1})
        self.assertEqual(observed["empty_turns"]["last_input"],
                         {"user_text": 1})

    def test_a_recovered_row_spends_only_the_retry_that_failed(self):
        """The retry that delivered is not waste, and the row carries no
        empty_turn flag because the turn the agent received was not empty."""
        d = proxywatch._stream_evidence(EMPTY_RECOVERED)
        self.assertFalse(d["empty_turn"])
        self.assertEqual((d["recovery"], d["retry_n"]), ("recovered", 1))
        empty = proxywatch.log_observation(
            self._log(EMPTY_RECOVERED))["empty_turns"]
        self.assertEqual(empty["engaged"], 1)
        self.assertEqual(empty["wasted_generations"], 1)

    def test_an_UNINSTRUMENTED_proxy_never_reports_zero_empty_turns(self):
        """Unreadable and empty must never share a value. Two codex proxies on
        this host emit no delivery suffix at all; calling them 0 empty turns
        would be the same lie the 401 census learned not to tell."""
        empty = proxywatch.log_observation(
            self._log(GIN_402, gin(200)))["empty_turns"]
        self.assertEqual(empty["instrumented"], 0)
        self.assertEqual(empty["rows"], 2)
        self.assertIn("UNINSTRUMENTED", proxywatch._empty_turn_clause(empty))
        self.assertIn("UNINSTRUMENTED",
                      "\n".join(proxywatch.empty_turn_lines(
                          rep(self._rendered(empty)))))
        # Unconditional positive control on the SAME observable: an
        # instrumented log in the same call reports measured counts, so the
        # zero above is blindness and not a parser that reads nothing.
        seen = proxywatch.log_observation(
            self._log(EMPTY_EXHAUSTED))["empty_turns"]
        self.assertEqual(seen["instrumented"], 1)
        self.assertEqual(seen["engaged"], 1)

    def test_an_IDLE_tail_of_canaries_is_silent_not_UNINSTRUMENTED(self):
        """The label is a claim about the proxy BUILD, so it must not fire on
        a tail that merely held no agent turns. Helm's own 401 canary carries
        no delivery suffix by construction; rendered live before the
        denominator was filtered, 17 of 18 seats read UNINSTRUMENTED while
        every one of them also read log=idle."""
        canaries = self._log(
            gin(401, path="/v1/chat/completions?helm_canary=1"),
            gin(401, path="/v1/messages?beta=true&helm_canary=1"))
        empty = proxywatch.log_observation(canaries)["empty_turns"]
        self.assertEqual((empty["rows"], empty["instrumented"]), (0, 0))
        self.assertEqual(proxywatch._empty_turn_clause(empty), "")
        # The same call, same observable: an UNMARKED agent row with no suffix
        # DOES earn the label, so the silence above is discrimination and not
        # a clause that never speaks.
        agent = proxywatch.log_observation(
            self._log(gin(200, path="/v1/messages?beta=true")))["empty_turns"]
        self.assertEqual((agent["rows"], agent["instrumented"]), (1, 0))
        self.assertIn("UNINSTRUMENTED", proxywatch._empty_turn_clause(agent))

    def test_the_scope_gap_is_REPORTED_and_explicitly_NOT_diagnosed(self):
        """313 such rows sat on non-codex seats. A real empty generation and a
        wrong api key answered HTTP 200 with an empty body are identical in the
        access row, so the reader must name the observation and refuse the
        diagnosis."""
        empty = proxywatch.log_observation(
            self._log(ZERO_DELIVERY_UNFLAGGED, DELIVERED))["empty_turns"]
        self.assertEqual(empty["zero_delivery_unflagged"], 1)
        self.assertEqual(empty["engaged"], 0)
        clause = proxywatch._empty_turn_clause(empty)
        self.assertIn("zero-delivery:1(cause-UNREADABLE)", clause)
        fleet = "\n".join(proxywatch.empty_turn_lines(
            rep(self._rendered(empty))))
        self.assertIn("UNREADABLE", fleet)
        self.assertNotIn("auth failure", fleet.lower())
        # A DELIVERED turn is not a zero-delivery row: the positive control
        # that the count above discriminates rather than counting every row.
        self.assertEqual(proxywatch.log_observation(
            self._log(DELIVERED))["empty_turns"]["zero_delivery_unflagged"], 0)

    def test_a_flagged_turn_with_no_recovery_verdict_is_UNRESOLVED_not_none(self):
        """54 live rows carry empty_turn=1 and no empty_recovery at all. The
        machinery's answer is unknown there, so the row is excluded from the
        spend count and says so instead of being folded into `none`."""
        flagged = EMPTY_EXHAUSTED.split(" empty_recovery=")[0]
        empty = proxywatch.log_observation(self._log(flagged))["empty_turns"]
        self.assertEqual(empty["unresolved"], 1)
        self.assertEqual(empty["engaged"], 1)
        self.assertEqual(empty["recovery"], {})
        self.assertEqual(empty["wasted_generations"], 0)
        self.assertIn("excluded", empty["basis"])
        self.assertIn("unresolved:1", proxywatch._empty_turn_clause(empty))

    def test_an_unrecognised_recovery_verdict_stays_VISIBLE(self):
        """A new enum value is a bug in every consumer's else. Counting by the
        literal token means a verdict the fork adds later shows up the day it
        ships instead of vanishing."""
        novel = EMPTY_EXHAUSTED.replace("empty_recovery=exhausted",
                                        "empty_recovery=deferred")
        empty = proxywatch.log_observation(self._log(novel))["empty_turns"]
        self.assertEqual(empty["recovery"], {"deferred": 1})
        self.assertIn("deferred:1", proxywatch._empty_turn_clause(empty))

    def test_the_two_DOLLAR_ANCHORED_readers_survive_the_stream_suffix(self):
        """gin_logger renders the stream suffix BEFORE response_body= and
        refusal_origin_v1= exactly because those two anchor with `$`. This
        reader adds no suffix and needs no reordering — assert all three
        readings hold on one composed row, so a future append cannot quietly
        break the pair."""
        composed = (EMPTY_EXHAUSTED + ' | refusal_origin_v1=local'
                    ' | response_body="empty_recovery=exhausted upstream text"')
        self.assertEqual(proxywatch._logged_refusal_origin(composed), "local")
        self.assertEqual(proxywatch._logged_response_body(composed),
                         "empty_recovery=exhausted upstream text")
        # And the body's OWN tokens are not read as instrument readings: the
        # counts come out identical to the same row without a body.
        self.assertEqual(proxywatch._stream_evidence(composed),
                         proxywatch._stream_evidence(EMPTY_EXHAUSTED))

    def test_the_owner_meets_the_number_on_the_seat_row_and_the_roll_up(self):
        """A number nobody looks at is not a cure. The per-seat clause rides
        the line the 401 provenance already rides, and the fleet total prints
        beside the codex budget it is spending."""
        p = self._log(EMPTY_EXHAUSTED, EMPTY_EXHAUSTED, EMPTY_RECOVERED,
                      ZERO_DELIVERY_UNFLAGGED, DELIVERED)
        observed = proxywatch.log_observation(p)
        empty = observed["empty_turns"]
        self.assertEqual(empty["engaged"], 3)
        self.assertEqual(empty["wasted_generations"], 7)
        rendered = "\n".join(proxywatch.report_lines(
            rep(self._rendered(empty))))
        self.assertIn("empty[tail=%dB,complete]=exhausted:2/recovered:1 "
                      "waste=7gen" % os.path.getsize(p), rendered)
        self.assertIn("zero-delivery:1(cause-UNREADABLE)", rendered)
        self.assertIn("3 engaged", rendered)
        self.assertIn("7 upstream generation(s) delivered nothing", rendered)
        # Positive control: the refusal state and the 401 provenance this row
        # already carried are unchanged by the new clause.
        self.assertEqual(observed["state"], "ok")
        self.assertEqual(observed["status_401"]["total"], 0)

    def test_a_measured_zero_is_SILENT_and_an_unreadable_log_carries_None(self):
        """Silence means zero only because the other two answers print. A
        healthy instrumented seat adds nothing to a nine-seat report; an
        unreadable log never reaches the clause at all."""
        empty = proxywatch.log_observation(
            self._log(DELIVERED))["empty_turns"]
        self.assertEqual(empty["instrumented"], 1)
        self.assertEqual(proxywatch._empty_turn_clause(empty), "")
        self.assertEqual(proxywatch.empty_turn_lines(
            rep(self._rendered(empty))), [])
        unreadable = proxywatch.log_observation(
            os.path.join(self.d, "absent.log"))
        self.assertIsNone(unreadable["empty_turns"])
        self.assertEqual(proxywatch._empty_turn_clause(None), "")
        # Unconditional positive control on the SAME two observables: one
        # engaged turn makes both the clause and the roll-up speak, so the
        # emptiness above is a measured silence and not a mute renderer.
        loud = proxywatch.log_observation(
            self._log(EMPTY_EXHAUSTED))["empty_turns"]
        self.assertNotEqual(proxywatch._empty_turn_clause(loud), "")
        self.assertNotEqual(proxywatch.empty_turn_lines(
            rep(self._rendered(loud))), [])

    @staticmethod
    def _rendered(empty):
        r = row()
        r["log_empty_turns"] = empty
        return r
