#!/usr/bin/env python3
"""task/3899: a seat's measured mood fed back to the seat as ONE steer.

The owner's glue `moods.ts` pattern: mood is an INPUT that nudges behaviour,
not only a display. When a seat's own measured state turns grinding or
stuck, its next `helm inject` carries one short line naming the measured
reason and the cheapest next move. It fires at most once per state change per
seat, never for a walled, idle or blocked-on-owner seat, and it rides the
same reflex lane and staged-mutation latch as the hourly mood ask.

THE CONTROLS ARE THE OWNER'S: a seat looping on one refusal must hear that it
is stuck, and a seat with no friction must hear nothing.

Every world is a temp HELM_HOME. The friction rows are written by the real
`friction.record`; nothing reads this machine's ledgers or posts anywhere.
"""
import importlib
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-seatmood-steer-", var="HELM_HOME")

from helm import friction  # noqa: E402
# the seat facade beside any seat impl the arms reach (the co-occurrence law)
from helm import seat  # noqa: E402,F401

SEAT = "mood-seat"
SID = "s-1"
NOW = 1_790_000_000.0
MIN = 60


def _sm():
    from helm import seatmood
    return seatmood


def _st():
    from helm import seatmood_steer
    return seatmood_steer


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-seatmood-steer-case-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = mock.patch.dict(os.environ, {
            "HELM_HOME": os.path.join(self.tmp, "helm"),
            "HELM_CHAT_DIR": os.path.join(self.tmp, "chat"),
            "HELM_CHAT_NAME": SEAT})
        env.start()
        self.addCleanup(env.stop)
        roster = mock.patch.object(_sm(), "_roster_key",
                                   side_effect=lambda n: (n, None))
        roster.start()
        self.addCleanup(roster.stop)

    def refuse(self, *ago_min, guard="author-gate", reason="PreToolUse:Bash"):
        for ago in ago_min:
            self.assertTrue(friction.record(guard, reason,
                                            now=NOW - ago * MIN))

    def turn(self, now=NOW, commit=True):
        """One turn start -> the lines it carries; the latch is committed
        the way `helm inject` commits it after the turn is READY."""
        got = _st().turn(SID, now=now)
        if not got:
            return []
        lines, payload = got
        if commit:
            _st().commit(payload)
        return [line for line, _rid in lines]

    @staticmethod
    def steers(lines):
        return [ln for ln in lines if ln.startswith("MOOD: helm measures")]


class SteerTest(Base):
    def test_a_seat_looping_on_one_refusal_hears_it_once(self):  # noqa: VACUOUS_ASSERTION — len(said) == 1 on the same steers() observable is the positive control before the silent repeat
        self.refuse(9, 8, 7, 6)
        said = self.steers(self.turn())
        self.assertEqual(len(said), 1, said)
        line = said[0]
        self.assertIn("stuck", line)
        self.assertIn("the same refusal by author-gate 4 times in a row", line)
        self.assertIn("Cheapest next move:", line)
        self.assertIn(_st().MOVES["loop:refusal"], line)
        # the same state on the next measured turn: silent
        self.refuse(5.5)
        later = NOW + _st().STEER_EVERY_S + 1
        self.assertEqual(self.steers(self.turn(now=later)), [])

    def test_a_seat_with_no_friction_hears_nothing(self):  # noqa: VACUOUS_ASSERTION — test_a_seat_looping_on_one_refusal_hears_it_once drives the same turn() to one line; the empty list is the claim
        self.assertEqual(self.steers(self.turn()), [])
        latch = _st().latch(SEAT)
        self.assertEqual(latch["state"], "flowing")

    def test_each_state_change_re_arms_it(self):  # noqa: VACUOUS_ASSERTION — three exact one-line counts on the same steers() observable surround the one silent turn
        step = _st().STEER_EVERY_S + 1
        self.refuse(20, 12, 2)                       # 3 by one guard
        first = self.steers(self.turn())
        self.assertEqual(len(first), 1, first)
        self.assertIn("grinding", first[0])
        self.assertIn("3 refusals by author-gate", first[0])
        # worse: a loop of the same refusal -> stuck, a new state, said once
        self.refuse(1, 0.5, 0.25)
        second = self.steers(self.turn(now=NOW + step))
        self.assertEqual(len(second), 1, second)
        self.assertIn("stuck", second[0])
        # the hour passes with no friction: flowing, silent, and re-armed
        quiet = NOW + 2 * 3600
        self.assertEqual(self.steers(self.turn(now=quiet)), [])
        self.assertEqual(_st().latch(SEAT)["state"], "flowing")
        for ago in (9, 8, 7, 6):
            friction.record("split-budget", "pre-commit",
                            now=quiet + step - ago * MIN)
        again = self.steers(self.turn(now=quiet + step))
        self.assertEqual(len(again), 1, again)
        self.assertIn("split-budget", again[0])

    def test_walled_idle_and_owner_blocked_seats_are_never_steered(self):  # noqa: VACUOUS_ASSERTION — the looping-refusal arm drives the same turn() over the same planted loop to one line; here the wall, the card and idle are the claim
        from helm import seatmood_signals as sig
        self.refuse(9, 8, 7, 6)
        wall = {"source": "family", "family": "kimi", "axis": "money",
                "why": "quota"}
        with mock.patch.object(sig, "wall", return_value=wall):
            self.assertEqual(self.steers(self.turn(commit=False)), [])
        card = {"id": "ab12cd34", "title": "pick one", "since": NOW - MIN}
        with mock.patch.object(sig, "_cards",
                               return_value=({SEAT: card}, None)):
            self.assertEqual(self.steers(self.turn(commit=False)), [])
        idle = dict(_st().reading(SEAT, SID, NOW), state="idle")
        with mock.patch.object(_st(), "reading", return_value=idle):
            self.assertEqual(self.steers(self.turn(commit=False)), [])

    def test_the_measure_is_taken_at_most_every_few_minutes(self):
        self.refuse(9, 8, 7, 6)
        self.turn()
        st = _st()
        with mock.patch.object(st, "reading",
                               wraps=st.reading) as measured:
            self.turn(now=NOW + st.STEER_EVERY_S - 1)
            self.assertEqual(measured.call_count, 0)
            self.turn(now=NOW + st.STEER_EVERY_S + 1)
            self.assertEqual(measured.call_count, 1)

    def test_the_steer_stands_in_for_the_hourly_ask(self):  # noqa: VACUOUS_ASSERTION — the steer line itself and asked_at == NOW are the positive controls beside the absent ask line
        """The steer invites the check-in itself, so the turn that carries
        it does not ask again, and the hour restarts."""
        sm = _sm()
        self.refuse(9, 8, 7, 6)
        lines = self.turn()
        self.assertEqual([ln for ln in lines if ln.startswith(
            sm.ASK_LINE[:20])], [])
        self.assertIn("helm seat mood set", self.steers(lines)[0])
        self.assertEqual(sm.asked_at(SEAT), NOW)

    def test_a_flowing_turn_still_asks_hourly(self):  # noqa: VACUOUS_ASSERTION — the exact [ASK_LINE] on the same turn() is the positive control before the silent half hour
        lines = self.turn()
        self.assertEqual(lines, [_sm().ASK_LINE])
        self.assertEqual(self.turn(now=NOW + 30 * MIN), [])

    def test_a_process_no_roster_seat_names_is_never_measured(self):  # noqa: VACUOUS_ASSERTION — the looping arm proves the same turn() measures and speaks; here an unnamed process and an off-roster name are the claim
        st = _st()
        with mock.patch.object(st, "reading") as measured:
            with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": ""}), \
                    mock.patch("helm.seats_roster.seats_for_session",
                               return_value=[]):
                self.assertIsNone(st.turn(SID, now=NOW))
            with mock.patch.object(_sm(), "_roster_key",
                                   return_value=(None, "not a seat")):
                got = st.turn(SID, now=NOW)
                self.assertTrue(got is None or got[0] == [], got)
        self.assertEqual(measured.call_count, 0)


class MoveTest(unittest.TestCase):
    def test_every_cause_the_judge_names_has_a_move(self):  # noqa: VACUOUS_ASSERTION — the loop runs over a non-empty literal and the superset assertion after it is unconditional
        st, sm = _st(), _sm()
        for cause in ("loop:refusal", "loop:rerun", "loop:failing",
                      "refusals", "quiet", "stalled", "context"):
            m = {"state": "stuck", "reason": "R", "cause": cause}
            line = st.line(m)
            self.assertTrue(line.startswith("MOOD: helm measures you stuck: R."),
                            line)
            self.assertIn(st.MOVES[cause], line)
            self.assertLessEqual(len(line), 400)
        self.assertTrue(set(st.MOVES) >= set(sm.FRICTION_CAUSES))


class InjectTest(Base):
    def test_a_latched_mood_edge_survives_the_repeat_filter_allowance(self):
        from helm import inject, injection_schema
        _whisper = importlib.import_module("helm.inject._whisper")
        st = _st()
        env = {"HELM_ADOPTED_DIR": os.path.join(self.tmp, "adopted"),
               "HELM_CACHE_DIR": os.path.join(self.tmp, "cache"),
               "HELM_SEAT_NAMES": os.path.join(self.tmp, "seat-names.txt")}
        os.makedirs(env["HELM_ADOPTED_DIR"])
        context = (injection_schema.V3, {"harness": "claude",
                                         "session": SID}, {}, None, None)
        states = ("stuck", "flowing") * 3
        moods = [{"state": state, "reason": "same measured loop",
                  "cause": "loop:refusal", "seat": SEAT} for state in states]
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(_whisper, "_sample_context",
                                  return_value=context), \
                mock.patch.object(st, "reading", side_effect=moods):
            said = []
            for _ in states:
                # The latch is committed by the real gather; only its measured
                # input is synthetic, so this exercises the assembled filter.
                with mock.patch.object(st, "STEER_EVERY_S", 0):
                    said.append(self.steers(inject.gather(
                        "carry on with the build", session=SID)["reflex"]))
        self.assertEqual([len(lines) for lines in said], [1, 0, 1, 0, 1, 0],
                         said)
        self.assertEqual(said[0], said[2])
        self.assertEqual(said[2], said[4])
        self.assertEqual(st.latch(SEAT)["state"], "flowing")

    def test_the_steer_rides_inject_once_and_commits_its_latch(self):  # noqa: VACUOUS_ASSERTION — the first turn's exactly-one steer and the committed latch are the positive controls on the same lane
        from helm import inject, injection_schema
        _whisper = importlib.import_module("helm.inject._whisper")
        env = {"HELM_ADOPTED_DIR": os.path.join(self.tmp, "adopted"),
               "HELM_CACHE_DIR": os.path.join(self.tmp, "cache"),
               "HELM_SEAT_NAMES": os.path.join(self.tmp, "seat-names.txt")}
        os.makedirs(env["HELM_ADOPTED_DIR"])
        now = time.time()
        for ago in (9, 8, 7, 6):
            friction.record("author-gate", "PreToolUse:Bash",
                            now=now - ago * MIN)
        context = (injection_schema.V3, {"harness": "claude",
                                         "session": SID}, {}, None, None)
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(_whisper, "_sample_context",
                                  return_value=context):
            said = [inject.gather("carry on with the build", session=SID)
                    ["reflex"] for _ in range(2)]
        hits = [self.steers(lane) for lane in said]
        self.assertEqual(len(hits[0]), 1, said)
        self.assertIn("stuck", hits[0][0])
        self.assertEqual(hits[1], [], said)
        latch = _st().latch(SEAT)
        self.assertEqual(latch["state"], "stuck")
        with open(_st().latch_path(SEAT), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["seat"], SEAT)


if __name__ == "__main__":
    unittest.main()
