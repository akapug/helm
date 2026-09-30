#!/usr/bin/env python3
"""THE ONE WORK READER (task/3643, slice 1): one card per piece of work, on
the one pipeline reading (task/3631), never beside it.

The owner's Backlog and Pipeline count one body of work two ways and cannot
meet. `helm/work_model.py` joins the to-do ledger, the pipeline reading, the
work rooms, the trains and the land log into ONE card per piece of work in
ONE of five stages, and counts them once, on the server.

WHAT THIS FILE HOLDS, each arm run against a planted world:

  * every open task is drawn exactly once, the stage counts add up to the
    cards, and every row the pipeline reading returned is typed exactly once;
  * the TYPED settlement (active, fix-owed, record) with its provenance, and
    ON TRUNK IS EVIDENCE, NEVER A DISMISSAL;
  * the three defects the design meld found in the prototype
    (final-src/build.py), pinned before its rules were ported:
      1. it asked "on trunk?" before "does a FIX stand?", so a landed,
         unanswered FIX became a paperwork record;
      2. its `chain_landed` took ANY later landed round of the lane as the
         answer to a fix, without asking whether that fix was answered;
      3. its `finish` placed a card by its newest non-debt row and never
         moved it to Building, so a landed FIX plus a newer review read In
         review while the fix was owed (task/3538's shape);
  * age never retires anything: past a day with nobody's move is a STUCK
    mark, and an undecided review stays in review;
  * an unread project yields unknown or at-least counts, never a zero;
  * the join is the one join (`taskkey.join`, task/3703): stored, else
    literal and verified, else UNKNOWN, and every card says how; the lane
    records and the recorded lands are read live and are in the revision;
  * one snapshot per revision, reused, and the drawer's crossings come from
    that same snapshot;
  * the pipeline numbers are `pipe_counts` over the boards /api/board sends,
    the one reading every surface prints (never a second counter);
  * the cure of review 3643: a room on a `lane/`-prefixed lease is its
    request's card, a room with no round in a STALE reading is not placed,
    a handed-back task is in To do since it came back, a land helm saw but
    keeps no train for is a gap, and a held approval says so plainly.
"""
import copy
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-work-model-", var="HELM_HOME")

from helm import taskkey, web_board  # noqa: E402


def _wm():
    """The reader under test, imported at use so each arm reports alone."""
    from helm import work_model
    return work_model


NOW = 1790700000.0
DAY = 86400


def _sec(**kw):
    sec = {"source": "x", "measured_at": NOW - 5, "age_s": 5,
           "limit_s": 1200, "unavailable": None, "stale": False}
    sec.update(kw)
    return sec


def _row(rid, lane, state, **kw):
    """One live land request as `web_board._kanban_card` sends it."""
    row = {"id": rid, "lane": lane, "state": state, "task": None,
           "holder": "bob", "tip": "ab" * 6, "gate": "", "contrary": False,
           "stalled": False, "trunk_contains_tip": False,
           "on_main_unverdicted": False, "source_clean_on_main": None,
           "owes_rehold": False, "age_s": 600, "kind": "review",
           "chain_root": "chain-" + rid, "author": "alice",
           "reviewer": "bob", "owed_by": "reviewer",
           "succession_state": "held", "mark": "moving"}
    row.update(kw)
    return row


def _fix_landed(rid, lane, **kw):
    """A FIX whose work landed while it stood: the reading's `contrary`."""
    return _row(rid, lane, "CHANGES_REQUESTED", polarity="fix",
                contrary=True, contrary_state="landed",
                contrary_provenance="observed", holder="integrator",
                owed_by="integrator", mark="contrary", **kw)


def _pipe(live, on_main=None, collapsed=()):
    """One project's pipeline record, tallied by the SERVER's own tally."""
    return {"loops": list(live), "loops_cut": [], "loops_more": {},
            "rehold": None, "tally": web_board._kanban_tally(list(live)),
            "on_main": on_main, "collapsed": list(collapsed)}


def _line(klass, rows, **kw):
    line = {"class": klass, "label": klass, "count": len(rows),
            "oldest_age_s": None, "rows": list(rows)}
    line.update(kw)
    return line


def _board(live=(), other=None, fleet=None, lands=None, running=None,
           on_main=None, collapsed=()):
    """/api/board: `alpha` is the project this helm serves (the lands leg),
    every other project comes off the fleet read."""
    board = {"generated_at": NOW,
             "sections": {"lands": lands or _sec(scope="alpha"),
                          "fleet": fleet or _sec(scope="alpha"),
                          "seats": _sec(), "tasks": _sec()},
             "projects": {"alpha": {"lanes": _pipe(live, on_main, collapsed),
                                    "running": list(running or ())}}}
    for key, rows in (other or {}).items():
        board["projects"][key] = {"pipeline": _pipe(rows), "running": []}
    return board


PROJECTS = {"alpha": {"name": "alpha"}, "beta": {"name": "beta"}}


def _task(n, project="alpha", status="open", **kw):
    row = {"id": "task/%d" % n, "title": "the ask %d" % n, "status": status,
           "owner": None, "priority": "P1", "origin": "agent",
           "project": project, "ts": NOW - 3 * DAY,
           "last_updated": NOW - 3 * DAY}
    row.update(kw)
    return row


def _tasks(*rows):
    return {"rows": {r["id"]: r for r in rows},
            "history": {r["id"]: [dict(r)] for r in rows},
            "unavailable": None}


def _no_dispatch():
    return {"rows": {}, "events": {}, "chains": {}, "unavailable": None}


def _no_trains():
    return {"trains": [], "lands": {}, "ejections": [], "unavailable": None}


def _inp(board, tasks=None, dispatch=None, trains=None, now=NOW):
    return {"now": now, "board": board, "projects": PROJECTS,
            "tasks": tasks or _tasks(), "dispatch": dispatch or _no_dispatch(),
            "trains": trains or _no_trains(), "train_project": "alpha",
            "zone": "America/Los_Angeles"}


def _build(board, **kw):
    return _wm().build(_inp(board, **kw))


def _card_of(snap, rid):
    return next(c for c in snap["cards"].values()
                if any(a["id"] == rid for a in c["actions"]))


def _record(snap, rid):
    return next((r for r in snap["records"] if r["id"] == rid), None)


# ---------------------------------------------------------------------------

class SettlementTest(unittest.TestCase):
    """Each row the reading returned is typed once: active, fix-owed or
    record, with the facts that decided it."""

    def test_the_three_types_each_carry_their_provenance(self):
        wm = _wm()
        cases = (
            (_row("r1", "l1", "AWAITING_REVIEW"), None, None, wm.ACTIVE),
            (_row("r2", "l2", "CHANGES_REQUESTED", polarity="fix"), None,
             None, wm.ACTIVE),
            (_fix_landed("r3", "l3"), None, None, wm.FIX_OWED),
            (_row("r4", "l4", "AWAITING_REVIEW", trunk_contains_tip=True,
                  on_main_unverdicted=True), "on_main",
             _line("on_main", []), wm.ACTIVE),
            (_row("r5", "l5", "AWAITING_REVIEW", owed_by="integrator",
                  source_clean_on_main="owes its close"), None, None,
             wm.ACTIVE),
            (_row("r6", "l6", "REVIEWED", polarity="approve",
                  succession_state="moved"), None, None, wm.RECORD),
            (_row("r7", "l7", "CHANGES_REQUESTED", polarity="fix"),
             "superseded", _line("superseded", []), wm.ACTIVE),
            (_fix_landed("r8", "l8", contrary_discharge="a",
                         succession_state="moved"), None, None, wm.RECORD),
        )
        for card, fold, line, want in cases:
            with self.subTest(row=card["id"]):
                got = wm.settle(card, fold, line)
                self.assertEqual(got["type"], want)
                self.assertTrue(got["why"])
                self.assertEqual(got["evidence"]["state"], card["state"])
                self.assertEqual(got["evidence"]["fold"], fold)
        self.assertEqual(wm.SETTLEMENTS, ("active", "fix-owed", "record"))

    def test_a_fix_owed_names_how_the_landing_is_known(self):
        got = _wm().settle(_fix_landed("r1", "l1"))
        ev = got["evidence"]
        self.assertIn("landed", ev["landed"])
        self.assertIn("observed", ev["landed"])
        self.assertEqual(ev["discharge"], "none")
        self.assertEqual(ev["verdict"], "fix")

    def test_on_trunk_alone_never_dismisses_a_live_review(self):
        """An undecided review on work already on trunk owes a decision: on
        trunk is evidence, not a close."""
        got = _wm().settle(_row("r1", "l1", "REVIEWED", polarity="concur",
                                trunk_contains_tip=True))
        self.assertEqual(got["type"], "active")
        self.assertIs(got["evidence"]["on_trunk"], True)


class PrototypeDefectOneTest(unittest.TestCase):
    """DEFECT 1 (build.py:381-387): "on trunk" was asked before "a FIX
    stands", so a landed open FIX became a Leftovers record."""

    def test_a_landed_fix_on_trunk_is_owed_and_drawn_in_building(self):
        snap = _build(_board([_fix_landed("f1", "relay", trunk_contains_tip=True)]))
        self.assertEqual(snap["rows"]["f1"]["settlement"]["type"], "fix-owed")
        self.assertIsNone(_record(snap, "f1"))
        card = _card_of(snap, "f1")
        self.assertEqual((card["stage"], card["sub"]), ("building", "fix-owed"))
        self.assertEqual(card["actions"][0]["settlement"]["type"], "fix-owed")
        self.assertIsNotNone(card["debt"])

    def test_a_fix_on_trunk_without_the_contrary_word_is_owed(self):
        row = _row("f2", "relay", "CHANGES_REQUESTED", polarity="fix",
                   trunk_contains_tip=True, succession_state="unknown")
        snap = _build(_board([row]))
        self.assertEqual(_card_of(snap, "f2")["sub"], "fix-owed")

    def test_a_landed_fix_the_census_folded_off_the_frontier_is_owed(self):
        """The live reading folds a landed FIX whose room was cleaned up onto
        its "left over after landing" line; the fix is still unanswered."""
        row = _fix_landed("f3", "gone-room")
        line = _line("off_frontier", [row],
                     by_reason={"landed-by-ancestry": 1})
        snap = _build(_board([], collapsed=[line]))
        self.assertEqual(snap["rows"]["f3"]["settlement"]["type"], "fix-owed")
        self.assertIsNone(_record(snap, "f3"))
        card = _card_of(snap, "f3")
        self.assertEqual((card["stage"], card["sub"]), ("building", "fix-owed"))
        ev = card["actions"][0]["settlement"]["evidence"]
        self.assertEqual(ev["fold"], "off_frontier")

    def test_a_folded_fix_proved_landed_only_by_the_census_is_owed(self):
        row = _row("f4", "gone-room", "CHANGES_REQUESTED", polarity="fix",
                   trunk_contains_tip=None)
        line = _line("off_frontier", [row],
                     by_reason={"landed-by-patch-id": 1})
        snap = _build(_board([], collapsed=[line]))
        self.assertEqual(_card_of(snap, "f4")["sub"], "fix-owed")


class PrototypeDefectTwoTest(unittest.TestCase):
    """DEFECT 2 (build.py:415-424): `chain_landed` answered a fix with ANY
    later landed round of the lane, never asking whether THIS fix was
    answered. Only a successor proved to carry the row answers it."""

    def world(self, **fix):
        fixrow = _fix_landed("f1", "relay-lane", age_s=5 * DAY, **fix)
        # a LATER round of the same lane, in another chain, closed as landed,
        # and a train that landed that lane after the fix was asked
        dispatch = {"rows": {
            "f1": {"id": "f1", "ts": NOW - 5 * DAY, "kind": "review",
                   "lane": "relay-lane", "chain": "chain-f1",
                   "sender": "alice", "recipient": "bob"},
            "later": {"id": "later", "ts": NOW - 2 * DAY, "kind": "review",
                      "lane": "relay-lane", "chain": "chain-later",
                      "sender": "alice", "recipient": "carol"}},
            "events": {"f1": [[NOW - 5 * DAY, "V", "fix", False]],
                       "later": [[NOW - DAY, "V", "approve", False],
                                 [NOW - DAY + 60, "L", "landed"]]},
            "chains": {"chain-f1": ["f1"], "chain-later": ["later"]},
            "unavailable": None}
        trains = {"trains": [{"name": "train9", "state": "DONE", "land": 40,
                              "intent": NOW - DAY - 600,
                              "pushed": NOW - DAY, "ended": NOW - DAY,
                              "ran": 100, "cars": [{"lane": "relay-lane",
                                                    "author": "alice",
                                                    "reader": "carol"}]}],
                  "lands": {"train9": {"n": 40, "ran": 100, "at": NOW - DAY}},
                  "ejections": [], "unavailable": None}
        return _build(_board([fixrow]), dispatch=dispatch, trains=trains)

    def test_a_later_landed_round_does_not_answer_the_fix(self):
        snap = self.world()
        self.assertEqual(snap["rows"]["f1"]["settlement"]["type"], "fix-owed")
        self.assertIsNone(_record(snap, "f1"))
        card = _card_of(snap, "f1")
        self.assertEqual((card["stage"], card["sub"]), ("building", "fix-owed"))
        # the land is on the card as evidence, with its proof
        self.assertEqual(card["land"]["train"], "train9")
        self.assertEqual(card["land"]["n"], 40)
        self.assertIn("landed", card["reached"])
        self.assertTrue(card["came_back"])

    def test_an_unverified_succession_is_no_answer_either(self):
        snap = self.world(contrary_discharge="unverified",
                          succession_state="unknown")
        self.assertEqual(_card_of(snap, "f1")["sub"], "fix-owed")

    def test_a_successor_proved_to_carry_the_row_answers_it(self):
        snap = self.world(contrary_discharge="a", succession_state="moved")
        rec = _record(snap, "f1")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["settlement"]["type"], "record")
        self.assertEqual(rec["settlement"]["evidence"]["discharge"], "a")


class PrototypeDefectThreeTest(unittest.TestCase):
    """DEFECT 3 (build.py:587-611): `finish` placed a card by its newest
    non-debt row and never moved the stage to Building (task/3538: a landed
    FIX plus a new review read In review while the fix was owed). One card,
    a primary blocked stage, and every other live move a secondary action
    with its own whose-move."""

    def setUp(self):
        self.debt = _fix_landed("debt", "ledger-post-land-task-7",
                                age_s=3 * DAY)
        self.review = _row("new", "ledger-post-land-task-7-r2",
                           "AWAITING_REVIEW", age_s=600, holder="carol",
                           reviewer="carol")
        self.snap = _build(_board([self.review, self.debt]),
                           tasks=_tasks(_task(7)))
        self.card = self.snap["cards"]["task/7"]

    def test_one_card_whose_primary_stage_is_the_fix_debt(self):
        cards = [c for c in self.snap["cards"].values() if c["task"] == "task/7"]
        self.assertEqual(len(cards), 1)
        self.assertEqual((self.card["stage"], self.card["sub"]),
                         ("building", "fix-owed"))

    def test_the_newer_review_is_a_secondary_action_with_its_own_move(self):
        acts = self.card["actions"]
        self.assertEqual([a["id"] for a in acts], ["debt", "new"])
        self.assertEqual([a["primary"] for a in acts], [True, False])
        self.assertEqual(acts[1]["stage"], "review")
        self.assertEqual(acts[1]["whose"], {"kind": "seat", "who": "carol",
                                            "why": None})
        self.assertEqual(acts[0]["whose"]["kind"], "role")

    def test_time_in_stage_is_measured_from_the_fix_request(self):
        self.assertEqual(self.card["since"], NOW - 3 * DAY)
        self.assertEqual(self.card["debt"]["since"], NOW - 3 * DAY)

    def test_the_view_hides_no_live_move(self):
        body = _wm().view(self.snap, NOW)
        card = next(c for c in body["cards"] if c["key"] == "task/7")
        self.assertEqual(len(card["actions"]), 2)
        self.assertEqual(card["stuck"], {"why": "fix-owed"})


class AgeNeverRetiresTest(unittest.TestCase):
    """Twenty-four hours with nobody's move is a STUCK mark, never a
    retirement: nothing leaves its stage for being old."""

    def test_an_undecided_review_stays_in_review_and_stuck(self):
        row = _row("u1", "old-review", "REVIEWED", polarity="concur",
                   holder="unknown",
                   owed_by="unknown (declared verdict held)",
                   age_s=200 * DAY)
        snap = _build(_board([row]))
        self.assertEqual(snap["rows"]["u1"]["settlement"]["type"], "active")
        self.assertIsNone(_record(snap, "u1"))
        card = _card_of(snap, "u1")
        self.assertEqual((card["stage"], card["sub"]), ("review", "undecided"))
        self.assertEqual(card["whose"]["kind"], "nobody")
        body = _wm().view(snap, NOW)
        wire = next(c for c in body["cards"] if c["key"] == card["key"])
        self.assertEqual(wire["stuck"]["why"], "no-move")
        self.assertGreaterEqual(wire["stuck"]["for_s"], 200 * DAY)

    def test_a_day_old_review_is_not_yet_stuck_and_a_later_one_is(self):
        row = _row("u2", "fresh-review", "REVIEWED", polarity="concur",
                   holder="unknown", age_s=3600)
        snap = _build(_board([row]))
        wm = _wm()
        key = _card_of(snap, "u2")["key"]
        early = next(c for c in wm.view(snap, NOW)["cards"] if c["key"] == key)
        late = next(c for c in wm.view(snap, NOW + DAY)["cards"]
                    if c["key"] == key)
        self.assertIsNone(early.get("stuck"))
        self.assertEqual(late["stuck"]["why"], "no-move")
        self.assertEqual(late["stage"], "review")

    def test_a_year_old_fix_debt_is_still_owed(self):
        snap = _build(_board([_fix_landed("f1", "ancient", age_s=400 * DAY)]))
        self.assertEqual(_card_of(snap, "f1")["sub"], "fix-owed")

    def test_an_old_task_stays_to_do(self):
        snap = _build(_board([]), tasks=_tasks(_task(1, ts=NOW - 900 * DAY)))
        self.assertEqual(snap["cards"]["task/1"]["stage"], "todo")

    def test_the_readings_stall_alarm_marks_a_seats_row_stuck(self):
        row = _row("s1", "slow", "AWAITING_REVIEW", stalled=True,
                   mark="stalled", holder="carol", age_s=5 * DAY)
        snap = _build(_board([row]))
        key = _card_of(snap, "s1")["key"]
        wire = next(c for c in _wm().view(snap, NOW)["cards"]
                    if c["key"] == key)
        self.assertEqual(wire["stuck"], {"why": "stalled"})
        self.assertEqual(wire["stage"], "review")


class DrawnOnceTest(unittest.TestCase):
    """Every open task is ONE card, the stage counts add up to the cards,
    and every row the reading returned is typed exactly once."""

    def setUp(self):
        tasks = _tasks(_task(1), _task(2), _task(3, project="beta"),
                       _task(4, project=None), _task(5, status="closed"),
                       _task(6, status="in_progress", owner="alice"),
                       _task(9, status="closed"))
        live = [_row("a1", "build-task-2", "AWAITING_REVIEW"),
                _row("a2", "build-task-2-r2", "CHANGES_REQUESTED",
                     polarity="fix"),
                _row("a3", "late-task-9", "AWAITING_BUILD", kind="build"),
                _row("a4", "no-task-here", "READY", polarity="approve",
                     holder="lander", owed_by="lander")]
        folded = _row("a5", "old-task-1", "AWAITING_REVIEW",
                      trunk_contains_tip=True, on_main_unverdicted=True)
        other = {"beta": [_row("b1", "beta-task-3", "AWAITING_REVIEW")]}
        self.board = _board(live, other=other,
                            on_main=_line("on_main", [folded]))
        self.snap = _build(self.board, tasks=tasks)
        self.body = _wm().view(self.snap, NOW)

    def test_every_open_task_is_exactly_one_card(self):
        drawn = [c["task"] for c in self.body["cards"] if c.get("task")]
        for tid in ("task/1", "task/2", "task/3", "task/4", "task/6"):
            with self.subTest(task=tid):
                self.assertEqual(drawn.count(tid), 1)
        # a closed task with no live work is not drawn; one with live work is
        self.assertNotIn("task/5", drawn)
        self.assertEqual(drawn.count("task/9"), 1)
        keys = [c["key"] for c in self.body["cards"]]
        self.assertEqual(len(keys), len(set(keys)))

    def test_the_stage_counts_add_up_to_the_cards(self):
        counts = self.body["counts"]
        stages = counts["stages"]
        # every stage read is exact; Landed is not measured over every
        # project, because beta merges by hand (task/3723, walk 3 finding 1),
        # and with no land in the window it is unknown, never 0
        self.assertTrue(all(stages[s]["exact"] for s in stages
                            if s != "landed"))
        self.assertEqual(stages["landed"],
                         {"n": None, "exact": False, "bound": "unknown"})
        self.assertEqual(sum(stages[s]["n"] or 0 for s in stages),
                         counts["cards"])
        self.assertEqual(counts["cards"], len(self.body["cards"]))
        self.assertEqual(sum(counts["by_project"][p][s]
                             for p in counts["by_project"] for s in stages),
                         counts["cards"])

    def test_every_row_of_the_reading_is_typed_exactly_once(self):
        tallied = sum(t["live"] for t in self.body["reading"]["tallies"].values())
        folded = 1
        self.assertEqual(sum(self.body["counts"]["settlement"].values()),
                         tallied + folded)
        self.assertEqual(sorted(self.snap["rows"]),
                         ["a1", "a2", "a3", "a4", "a5", "b1"])
        # a census fold is provenance, never a settlement: the review on
        # main with no verdict is still owed
        a5 = self.snap["rows"]["a5"]["settlement"]
        self.assertEqual((a5["type"], a5["evidence"]["fold"]),
                         ("active", "on_main"))

    def test_two_rooms_of_one_task_are_one_card_with_two_moves(self):
        card = self.snap["cards"]["task/2"]
        self.assertEqual(sorted(a["id"] for a in card["actions"]),
                         ["a1", "a2"])
        self.assertEqual(card["stage"], "building")

    def test_the_view_is_json(self):
        back = json.loads(json.dumps(self.body))
        self.assertEqual(back["counts"]["cards"], len(self.body["cards"]))

    def test_a_seat_with_no_room_is_not_a_card(self):
        board = _board([], running=[{"lane": None, "kind": "seat",
                                     "seats": ["alice"]},
                                    {"lane": "room-a", "kind": "claim",
                                     "seats": ["bob"],
                                     "landed": {"state": "unlanded"}}])
        snap = _build(board)
        self.assertEqual(list(snap["cards"]), ["lane:alpha:room-a"])

    def test_a_claimed_room_with_no_round_is_being_written(self):
        board = _board([], running=[{"lane": "fresh-task-1", "kind": "claim",
                                     "seats": ["alice"],
                                     "landed": {"state": "unlanded"}}])
        snap = _build(board, tasks=_tasks(_task(1)))
        card = snap["cards"]["task/1"]
        self.assertEqual((card["stage"], card["sub"]), ("building", "writing"))
        self.assertEqual(card["whose"]["who"], "alice")
        self.assertIsNone(card["since"])     # helm keeps no room-opened time


class ScopeTest(unittest.TestCase):
    """A project not read is never a zero (task/3631's rule, task/3632): its
    stage counts are floors or unknown, and the scope names it."""

    def body(self, board, tasks=None):
        return _wm().view(_build(board, tasks=tasks), NOW)

    def test_a_complete_reading_counts_exactly(self):
        body = self.body(_board([_row("a1", "l1", "AWAITING_REVIEW")],
                                other={"beta": []}))
        self.assertTrue(body["scope"]["complete"])
        self.assertEqual(body["counts"]["stages"]["building"],
                         {"n": 0, "exact": True, "bound": None})

    def test_a_project_still_being_read_makes_floors_and_no_zero(self):
        board = _board([_row("a1", "l1", "AWAITING_REVIEW")],
                       other={"beta": [_row("b1", "l2", "AWAITING_REVIEW")]},
                       fleet={"loading": True})
        body = self.body(board)
        self.assertFalse(body["scope"]["complete"])
        self.assertEqual(body["scope"]["unread"], ["beta"])
        stages = body["counts"]["stages"]
        self.assertEqual(stages["review"], {"n": 1, "exact": False,
                                            "bound": "at_least"})
        # a stage with nothing seen says unknown, never 0
        self.assertEqual(stages["building"], {"n": None, "exact": False,
                                              "bound": "unknown"})
        # beta's row was not read, so it is on no card
        self.assertNotIn("b1", json.dumps(body["cards"]))

    def test_a_project_that_could_not_be_read_is_unknown_with_its_reason(self):
        board = _board([], other={"beta": []},
                       fleet={"unavailable": "the all-projects read raised"})
        body = self.body(board)
        self.assertEqual(body["scope"]["unknown"], ["beta"])
        self.assertIn("raised", body["scope"]["why"])
        self.assertIsNone(body["counts"]["stages"]["landing"]["n"])

    def test_nothing_read_is_unknown_everywhere_in_the_pipeline(self):
        board = _board([])
        board["sections"]["lands"] = {"loading": True, "scope": None}
        board["sections"]["fleet"] = {"loading": True}
        body = self.body(board)
        for s in ("building", "review", "landing"):
            with self.subTest(stage=s):
                self.assertIsNone(body["counts"]["stages"][s]["n"])
        self.assertFalse(body["reading"]["read"])

    def test_to_do_is_a_ceiling_while_a_project_with_tasks_is_unread(self):
        board = _board([], other={"beta": []}, fleet={"loading": True})
        body = self.body(board, tasks=_tasks(_task(1), _task(3, project="beta")))
        self.assertEqual(body["counts"]["stages"]["todo"],
                         {"n": 2, "exact": False, "bound": "at_most"})

    def test_a_project_whose_server_sent_fewer_rows_than_it_counted_is_a_floor(self):
        board = _board([_row("a1", "l1", "AWAITING_REVIEW")],
                       other={"beta": []})
        board["projects"]["alpha"]["lanes"]["tally"]["live"] = 5
        body = self.body(board)
        self.assertEqual(body["scope"]["partial"], ["alpha"])
        self.assertEqual(body["counts"]["stages"]["review"],
                         {"n": 1, "exact": False, "bound": "at_least"})
        self.assertFalse(body["scope"]["complete"])

    def test_a_room_is_not_placed_while_its_project_is_unread(self):
        room = {"lane": "room-b", "kind": "claim", "seats": ["bob"],
                "landed": {"state": "unlanded"}}
        board = _board([], other={"beta": []}, fleet={"loading": True})
        board["projects"]["beta"]["running"] = [room]
        body = self.body(board)
        self.assertEqual(body["counts"]["cards"], 0)
        self.assertEqual(body["scope"]["unread"], ["beta"])

    def test_a_room_with_no_round_is_not_placed_while_its_reading_is_stale(self):
        """task/3130's stale half, carried from the retired board (test_web_
        board test_a_stale_pipeline_draws_no_pipeline_column): a stale
        reading cannot say which rooms are already under review, so a room
        it holds no round for would be a false "writing" card. Its project
        is partial, and Building is a floor; a room on a request the stale
        reading does hold still rides that request's card."""
        rooms = [{"lane": "room-a", "kind": "claim", "seats": ["bob"],
                  "landed": {"state": "unlanded"}},
                 {"lane": "l1", "kind": "claim", "seats": ["alice"],
                  "landed": {"state": "unlanded"}}]
        board = _board([_row("a1", "l1", "AWAITING_REVIEW")], running=rooms,
                       other={"beta": []},
                       lands=_sec(scope="alpha", age_s=1300, limit_s=1200,
                                  stale=True))
        body = self.body(board)
        self.assertEqual([c["key"] for c in body["cards"]], ["lane:alpha:l1"])
        self.assertEqual([r["lane"] for r in body["cards"][0]["rooms"]], ["l1"])
        self.assertEqual(body["scope"]["partial"], ["alpha"])
        self.assertEqual(body["counts"]["stages"]["building"],
                         {"n": None, "exact": False, "bound": "unknown"})
        # the control: read fresh, the same room is its seat's Building move
        fresh = self.body(_board([_row("a1", "l1", "AWAITING_REVIEW")],
                                 running=rooms, other={"beta": []}))
        self.assertIn("lane:alpha:room-a", [c["key"] for c in fresh["cards"]])

    def test_an_unread_roster_makes_building_a_floor(self):
        board = _board([_row("a1", "l1", "AWAITING_BUILD", kind="build")],
                       other={"beta": []})
        board["sections"]["seats"] = {"loading": True}
        body = self.body(board)
        self.assertFalse(body["scope"]["rooms_read"])
        self.assertEqual(body["counts"]["stages"]["building"],
                         {"n": 1, "exact": False, "bound": "at_least"})
        self.assertEqual(body["counts"]["stages"]["review"]["bound"], None)

    def test_an_unreadable_to_do_list_is_no_to_do_count(self):
        tasks = {"rows": {}, "history": {}, "unavailable": "ledger unreadable"}
        body = self.body(_board([]), tasks=tasks)
        self.assertIsNone(body["counts"]["stages"]["todo"]["n"])
        self.assertEqual(body["scope"]["tasks_unavailable"],
                         "ledger unreadable")


class JoinTest(unittest.TestCase):
    """The one join (`taskkey.join`): stored, else literal and verified,
    else UNKNOWN — never a trailing number — checked against the task's own
    project."""

    LEDGER = {"task/7": _task(7), "task/926": _task(926),
              "task/8": _task(8, project="beta"),
              "task/11": _task(11, project=None)}

    def join(self, lane):
        return _wm().join_work("alpha", self.LEDGER, lane=lane)

    def test_the_answer_is_the_one_joins(self):
        self.assertEqual(self.join("relay-task-7"),
                         {"task": "task/7", "how": "literal", "why": None})
        # a dispatch row's own lane is read when no lane is given
        row = {"id": "r1", "lane": "x-task-7"}
        self.assertEqual(_wm().join_work("alpha", self.LEDGER, row=row,
                                         current={"r1": row})["task"],
                         "task/7")

    def test_a_literal_citation_joins_in_every_spelling(self):
        self.assertEqual(self.join("relay-task-7")["how"], "literal")
        for lane in ("relay-task-7", "task/7-relay", "TASK-7"):
            with self.subTest(lane=lane):
                self.assertEqual(self.join(lane)["task"], "task/7")

    def test_a_trailing_number_is_never_a_task(self):
        got = self.join("canary-seeded-red-0926")
        self.assertEqual((got["task"], got["how"]), (None, "unknown"))
        self.assertIn("no task", got["why"])

    def test_the_join_refuses_what_it_cannot_verify(self):
        wm = _wm()
        cases = (("a-task-7-and-task-8", "2 tasks"),
                 ("relay-task-8", "beta's"),
                 ("relay-task-11", "no project"),
                 ("relay-task-99", "not in the task ledger"))
        answers = [wm.join_work("alpha", self.LEDGER, lane=lane)
                   for lane, _word in cases]
        self.assertEqual(len(answers), 4)
        for (lane, word), got in zip(cases, answers):
            with self.subTest(lane=lane):
                self.assertEqual((got["task"], got["how"]), (None, "unknown"))
                self.assertIn(word, got["why"])
        self.assertEqual(wm.join_work("alpha", self.LEDGER,
                                      lane="relay-task-7"),
                         {"task": "task/7", "how": "literal", "why": None})

    def test_every_card_says_how_it_was_linked(self):
        snap = _build(_board([_row("a1", "relay-task-7", "AWAITING_REVIEW"),
                              _row("a2", "anon", "AWAITING_REVIEW")]),
                      tasks=_tasks(_task(7), _task(8)))
        hows = {c["key"]: c["link"]["how"] for c in snap["cards"].values()}
        self.assertEqual(hows, {"task/7": "literal", "lane:alpha:anon":
                                "unknown", "task/8": "task"})
        self.assertIn("no task", snap["cards"]["lane:alpha:anon"]["link"]["why"])

    def test_the_chains_first_row_names_its_task(self):
        """The one join is handed the round's dispatch row: the task its
        chain's first row recorded is a stored key, over a lane that names
        none, and over another round's literal."""
        wm = _wm()
        first = {"id": "a0", "lane": "anon", "chain": "a0",
                 "chain_root": None, "task": "task/8", "repo_id": None,
                 "ts": NOW - 600, "kind": "review"}
        later = dict(first, id="a1", chain_root="a0", task=None, ts=NOW)
        dispatch = {"rows": {"a0": first, "a1": later}, "events": {},
                    "chains": {"a0": ["a0", "a1"]}, "unavailable": None}
        inp = _inp(_board([_row("a1", "anon", "AWAITING_REVIEW",
                                chain_root="a0")]),
                   tasks=_tasks(_task(8)), dispatch=dispatch)
        card = wm.build(inp)["cards"]["task/8"]
        self.assertEqual([a["id"] for a in card["actions"]], ["a1"])
        self.assertEqual(card["link"]["how"], "stored")
        # the control: with no dispatch row, the lane alone names no task
        inp["dispatch"] = _no_dispatch()
        snap = wm.build(inp)
        self.assertEqual(_card_of(snap, "a1")["key"], "lane:alpha:anon")

    def test_a_room_rides_the_card_its_rows_stored_key_placed(self):
        """Console walk 4, P1 1: ONE WORK, ONE CARD. The lane is linked to
        task/8 only by its chain's first row, and a seat holds its room. The
        room has no dispatch row, so its own join names no task; it rides the
        card that holds the lane's action, never a second, empty card in To
        do. A landed room of a lane linked the same way rides its land's
        card, never a leftover record."""
        wm = _wm()
        first = {"id": "a0", "lane": "anon", "chain": "a0",
                 "chain_root": None, "task": "task/8", "repo_id": None,
                 "ts": NOW - 600, "kind": "review"}
        later = dict(first, id="a1", chain_root="a0", task=None, ts=NOW)
        dispatch = {"rows": {"a0": first, "a1": later}, "events": {},
                    "chains": {"a0": ["a0", "a1"]}, "unavailable": None}
        room = {"kind": "claim", "lane": "anon", "seats": ["bonsai"],
                "landed": {"state": "unlanded"}}
        snap = wm.build(_inp(_board([_row("a1", "anon", "AWAITING_REVIEW",
                                          chain_root="a0")], running=[room]),
                             tasks=_tasks(_task(8)), dispatch=dispatch))
        self.assertEqual(sorted(snap["cards"]), ["task/8"])
        card = snap["cards"]["task/8"]
        self.assertEqual(card["link"]["how"], "stored")
        self.assertEqual([r["lane"] for r in card["rooms"]], ["anon"])
        self.assertEqual(wm.view(snap, NOW)["counts"]["stages"]["todo"]["n"],
                         0)
        # THE LANDED HALF: the lane's car landed in the window, its request
        # is not in the reading, and the room's lease is still held
        car = {"id": "a1", "lane": "anon", "task": None, "author": "alice",
               "reader": "bob"}
        trains = {"trains": [{"name": "train8", "state": "DONE", "land": 480,
                              "intent": NOW - 4000, "pushed": NOW - 3600,
                              "ended": NOW - 3600, "ran": 27000,
                              "cars": [car]}],
                  "lands": {}, "ejections": [], "unavailable": None}
        landed = dict(room, landed={"state": "landed", "proof": "ancestry"})
        snap = wm.build(_inp(_board([], running=[landed]), trains=trains,
                             tasks=_tasks(_task(8)), dispatch=dispatch))
        self.assertEqual(sorted(snap["cards"]), ["task/8"])
        self.assertEqual([r["lane"] for r in snap["cards"]["task/8"]["rooms"]],
                         ["anon"])
        self.assertIsNone(_record(snap, "room:alpha:anon"))

    def test_a_room_writing_more_on_landed_work_is_its_lands_card(self):
        """Lane `foo` landed task/8 in the Landed window, and a seat still
        holds its room with commits the trunk does not have: task/8's work
        goes on. ONE card, task/8's, with its land and its seat's writing
        move, never a second "no task on file" card beside it carrying the
        same land, and never a Landed card hiding the writing. Whether the
        request is still in the reading (a record on task/8's card) or not,
        and whether the lane's room names a task or not."""
        wm = _wm()
        tip = "ab" * 20
        car = {"id": "r1", "lane": "foo", "tip": tip, "task": "task/8",
               "author": "alice", "reader": "bob"}
        trains = {"trains": [{"name": "train8", "state": "DONE", "land": 480,
                              "intent": NOW - 4000, "pushed": NOW - 3600,
                              "ended": NOW - 3600, "ran": 27000,
                              "cars": [car]}],
                  "lands": {}, "ejections": [], "unavailable": None}
        room = {"kind": "claim", "lane": "foo", "seats": ["bonsai"],
                "landed": {"state": "unlanded"}}
        for live in ((), (_row("r1", "foo", "AWAITING_REVIEW",
                               tip=tip[:12]),)):
            with self.subTest(request=bool(live)):
                snap = wm.build(_inp(_board(list(live), running=[room]),
                                     trains=trains, tasks=_tasks(_task(8))))
                self.assertEqual(sorted(snap["cards"]), ["task/8"])
                card = snap["cards"]["task/8"]
                self.assertEqual(card["land"]["train"], "train8")
                self.assertEqual([r["lane"] for r in card["rooms"]], ["foo"])
                self.assertEqual((card["stage"], card["sub"]),
                                 ("building", "writing"))
                self.assertEqual(card["whose"]["who"], "bonsai")


def _git(repo, *args):
    """git in a scratch repository, reading no config of the host's."""
    env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null",
               GIT_CONFIG_SYSTEM="/dev/null", GIT_AUTHOR_NAME="t",
               GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@example.invalid")
    got = subprocess.run(("git", "-C", repo) + args, env=env,
                         capture_output=True, text=True)
    if got.returncode != 0:
        raise AssertionError("git %s: %s" % (args, got.stderr))
    return got.stdout.strip()


class OneJoinTest(unittest.TestCase):
    """task/3703: THE WORK PAGE READS THE ONE JOIN (`taskkey.join`) now that
    the stored task key is on trunk (task/3643, slice 2). A lane whose record
    names a task is on that task's card, whatever its name cites; the lane
    records are read with the rest of the live world and are in the revision
    marker; a land is joined by what was recorded at it; and an open task
    whose land `taskkey.lands_by_task` recorded reads LANDED_UNREAD
    (`taskkey.landed_state`), whether its land is in the Landed window or
    older."""

    def setUp(self):
        _wm().forget()
        self.addCleanup(_wm().forget)
        self.tmp = tempfile.mkdtemp(prefix="helm-test-work-model-repo-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "root")
        _git(self.repo, "branch", "lane/fix-task-7")

    def test_a_stored_key_puts_the_lane_on_its_tasks_card(self):
        """The lane's name says task-7 and its record says task/8: the one
        join takes the record, for its round and for its room alike."""
        room = {"kind": "claim", "lane": "lane/fix-task-7", "seats": ["bob"]}
        board = _board([_row("a1", "fix-task-7", "AWAITING_REVIEW")],
                       running=[room],
                       other={"beta": [_row("b1", "fix-task-9",
                                            "AWAITING_REVIEW")]})
        inp = _inp(board, tasks=_tasks(_task(7), _task(8),
                                       _task(9, project="beta")))
        inp["lanes"] = {"fix-task-7": frozenset({"task/8"}),
                        "fix-task-9": frozenset({"task/8"})}
        snap = _wm().build(inp)
        card = _card_of(snap, "a1")
        self.assertEqual((card["key"], card["link"]["how"]),
                         ("task/8", "stored"))
        self.assertEqual([r["lane"] for r in card["rooms"]],
                         ["lane/fix-task-7"])
        self.assertEqual(snap["cards"]["task/7"]["actions"], [])
        # THE RECORDS ARE THE TRAIN PROJECT'S: the checkout this helm serves
        # keeps them, so beta's lane of that name joins by its own name
        self.assertEqual(_card_of(snap, "b1")["key"], "task/9")

    def test_lane_records_not_read_leave_a_lane_unknown(self):
        """A record helm could not read may name another task, so the one
        join's answer for that lane is UNKNOWN with the reason, never its
        literal name."""
        inp = _inp(_board([_row("a1", "fix-task-7", "AWAITING_REVIEW")]),
                   tasks=_tasks(_task(7)))
        inp["lanes_unavailable"] = "the config could not be read"
        card = _card_of(_wm().build(inp), "a1")
        self.assertEqual(card["key"], "lane:alpha:fix-task-7")
        self.assertEqual(card["link"]["how"], "unknown")
        self.assertIn("the config could not be read", card["link"]["why"])

    def test_a_land_is_joined_by_what_was_recorded_at_it(self):
        """HISTORY IS A FACT (`taskkey`): a train car joins by the task it
        recorded, else its lane's literal, never by today's lane records,
        since a lane name can be reused for another task."""
        cars = [{"id": "c1", "lane": "anon", "task": "task/9",
                 "author": "alice", "reader": "bob"},
                {"id": "c2", "lane": "fix-task-7", "task": None,
                 "author": "alice", "reader": "bob"}]
        trains = {"trains": [{"name": "train8", "state": "DONE", "land": 480,
                              "intent": NOW - 8000, "pushed": NOW - 7200,
                              "ended": NOW - 7200, "ran": 27000,
                              "cars": cars}],
                  "lands": {"train8": {"n": 480, "ran": 27000,
                                       "at": NOW - 7200}},
                  "ejections": [], "unavailable": None}
        inp = _inp(_board([]), trains=trains, tasks=_tasks(
            _task(7, status="closed"), _task(8), _task(9, status="closed")))
        inp["lanes"] = {"fix-task-7": frozenset({"task/8"}),
                        "anon": frozenset({"task/8"})}
        snap = _wm().build(inp)
        self.assertEqual(snap["cards"]["task/9"]["land"]["train"], "train8")
        self.assertEqual(snap["cards"]["task/7"]["stage"], "landed")
        self.assertIsNone(snap["cards"]["task/8"]["land"])
        self.assertEqual(snap["cards"]["task/8"]["stage"], "todo")
        # THE REQUEST A TRAIN PUSHED IS THAT LAND TOO (`_carried`): its
        # record rides the task its car recorded, not the one its lane's
        # record names today
        tip = "ab" * 20
        cars.append({"id": "r1", "lane": "work-again", "tip": tip,
                     "task": "task/7", "author": "alice", "reader": "bob"})
        inp = _inp(_board([_row("r1", "work-again", "AWAITING_REVIEW",
                                tip=tip[:12])]), trains=trains,
                   tasks=_tasks(_task(7, status="closed"), _task(8)))
        inp["lanes"] = {"work-again": frozenset({"task/8"})}
        snap = _wm().build(inp)
        self.assertEqual(snap["rows"]["r1"]["settlement"]["type"], "record")
        self.assertEqual((snap["rows"]["r1"]["task"],
                          _record(snap, "r1")["card"]), ("task/7", "task/7"))
        self.assertEqual(snap["cards"]["task/8"]["lanes"], [])

    def test_a_reused_lane_name_carries_no_old_land(self):
        """HISTORY IS A FACT (`taskkey`) on the card too: lane `foo` landed
        task/7, and its name was reused for task/8, whose record joins the
        live round. The old land is task/7's: it is never task/8's land,
        dot or crossing, in the Landed window or older. The control: lane
        `bar`'s own land still reaches its task's card by the lane's name."""
        wm = _wm()
        for age in (7200, 3 * DAY):
            with self.subTest(age=age):
                cars = [{"id": "c1", "lane": "foo", "task": "task/7",
                         "author": "alice", "reader": "bob"},
                        {"id": "c2", "lane": "bar", "task": "task/9",
                         "author": "alice", "reader": "bob"}]
                trains = {"trains": [{"name": "train8", "state": "DONE",
                                      "land": 480, "intent": NOW - age - 600,
                                      "pushed": NOW - age, "ended": NOW - age,
                                      "ran": 27000, "cars": cars}],
                          "lands": {"train8": {"n": 480, "ran": 27000,
                                               "at": NOW - age}},
                          "ejections": [], "unavailable": None}
                inp = _inp(_board([_row("a1", "foo", "AWAITING_REVIEW"),
                                   _row("b1", "bar", "AWAITING_REVIEW")]),
                           trains=trains,
                           tasks=_tasks(_task(7, status="closed"), _task(8),
                                        _task(9)))
                inp["lanes"] = {"foo": frozenset({"task/8"}),
                                "bar": frozenset({"task/9"})}
                snap = wm.build(inp)
                card = snap["cards"]["task/8"]
                self.assertEqual([a["id"] for a in card["actions"]], ["a1"])
                self.assertIsNone(card["land"])
                self.assertFalse({"landing", "landed"} & set(card["reached"]))
                self.assertEqual([e for e in wm.card_events(snap, "task/8")[
                    "events"] if e[1] in ("J", "M", "G")], [])
                # the control: a lane's own land is still found by its name
                mine = snap["cards"]["task/9"]
                self.assertEqual([a["id"] for a in mine["actions"]], ["b1"])
                self.assertEqual(mine["land"]["train"], "train8")
                self.assertIn("landed", mine["reached"])
                self.assertIn("M", [e[1] for e in wm.card_events(
                    snap, "task/9")["events"]])

    def test_a_reused_lane_names_room_rides_its_own_recorded_task(self):
        """Lane `foo` landed task/7 in the Landed window, so its request is
        a record on task/7's card. Its name was reused: its record now names
        task/8, and a seat holds its room. The room is task/8's work, never
        the old land's: task/8 is being written, and task/7's card carries
        no room. A landed room of that lane rides task/8's card too. The
        control: a room recorded for task/8 on a lane whose round the join
        could not name (its chain records task/9) still rides that round's
        card, one card and never two."""
        wm = _wm()
        tip = "ab" * 20
        car = {"id": "r1", "lane": "foo", "tip": tip, "task": "task/7",
               "author": "alice", "reader": "bob"}
        trains = {"trains": [{"name": "train8", "state": "DONE", "land": 480,
                              "intent": NOW - 4000, "pushed": NOW - 3600,
                              "ended": NOW - 3600, "ran": 27000,
                              "cars": [car]}],
                  "lands": {}, "ejections": [], "unavailable": None}
        for state in ("unlanded", "landed"):
            with self.subTest(room=state):
                room = {"kind": "claim", "lane": "foo", "seats": ["bonsai"],
                        "landed": {"state": state, "proof": "ancestry"}}
                inp = _inp(_board([_row("r1", "foo", "AWAITING_REVIEW",
                                        tip=tip[:12])], running=[room]),
                           trains=trains, tasks=_tasks(
                               _task(7, status="closed"), _task(8)))
                inp["lanes"] = {"foo": frozenset({"task/8"})}
                snap = wm.build(inp)
                self.assertEqual(_record(snap, "r1")["card"], "task/7")
                self.assertEqual(snap["cards"]["task/7"]["rooms"], [])
                mine = snap["cards"]["task/8"]
                self.assertEqual([r["lane"] for r in mine["rooms"]], ["foo"])
                self.assertEqual(mine["stage"], "building"
                                 if state == "unlanded" else "todo")
                self.assertIsNone(_record(snap, "room:alpha:foo"))
        # the control: the round's own join is contradictory, so its card
        # names no task, and the room recorded for task/8 rides it
        first = {"id": "b0", "lane": "bar", "chain": "b0", "chain_root": None,
                 "task": "task/9", "repo_id": None, "ts": NOW - 600,
                 "kind": "review"}
        dispatch = {"rows": {"b0": first}, "events": {},
                    "chains": {"b0": ["b0"]}, "unavailable": None}
        room = {"kind": "claim", "lane": "bar", "seats": ["bonsai"],
                "landed": {"state": "unlanded"}}
        inp = _inp(_board([_row("b0", "bar", "AWAITING_REVIEW",
                                chain_root="b0")], running=[room]),
                   tasks=_tasks(_task(8), _task(9)), dispatch=dispatch)
        inp["lanes"] = {"bar": frozenset({"task/8"})}
        snap = wm.build(inp)
        card = _card_of(snap, "b0")
        self.assertEqual((card["key"], card["link"]["how"]),
                         ("lane:alpha:bar", "unknown"))
        self.assertEqual([r["lane"] for r in card["rooms"]], ["bar"])
        self.assertEqual(snap["cards"]["task/8"]["rooms"], [])

    def test_each_use_of_a_lane_name_keeps_its_own_land(self):
        """Both uses of lane `foo` landed: task/7's in train8, then task/8's
        in train9. Each task's card carries the land its car recorded,
        never the newest land of the name."""
        def train(name, n, at, car):
            return {"name": name, "state": "DONE", "land": n,
                    "intent": NOW - at - 600, "pushed": NOW - at,
                    "ended": NOW - at, "ran": 27000,
                    "cars": [dict(car, lane="foo", author="alice",
                                  reader="bob")]}
        trains = {"trains": [train("train8", 480, 7200,
                                   {"id": "c1", "task": "task/7"}),
                             train("train9", 481, 3600,
                                   {"id": "c3", "task": "task/8"})],
                  "lands": {}, "ejections": [], "unavailable": None}
        snap = _wm().build(_inp(_board([]), trains=trains, tasks=_tasks(
            _task(7, status="closed"), _task(8, status="closed"))))
        self.assertEqual(snap["cards"]["task/7"]["land"]["train"], "train8")
        self.assertEqual(snap["cards"]["task/8"]["land"]["train"], "train9")

    def test_a_new_stored_key_changes_the_revision(self):
        """`revision`: every input is in the marker. The lane records live
        in the repository's git config, so a record written there is a new
        revision, never the kept snapshot of the old one."""
        wm = _wm()
        src = wm.LiveSources(root=self.repo)
        board = _board([])
        before = src.fingerprint(src.paths())
        self.assertEqual(taskkey.record_lane(self.repo, "fix-task-7",
                                             "task/8"), (True, None))
        after = src.fingerprint(src.paths())
        self.assertNotEqual(wm.revision(board, PROJECTS, before, NOW),
                            wm.revision(board, PROJECTS, after, NOW))
        common = _git(self.repo, "rev-parse", "--path-format=absolute",
                      "--git-common-dir")
        self.assertIn(os.path.join(common, "config"), src.paths())
        # the control: a checkout git cannot name adds no path, and never a
        # None the fingerprint would choke on
        bare = os.path.join(self.tmp, "not-a-repo")
        os.makedirs(bare)
        paths = wm.LiveSources(root=bare).paths()
        self.assertTrue(all(isinstance(p, str) for p in paths), paths)
        self.assertIsNotNone(src.fingerprint(paths))

    def test_the_live_read_carries_the_lane_records_and_the_lands(self):
        """LiveSources.read hands the build the lane records of the checkout
        it serves and `taskkey.lands_by_task`'s index, so the live page
        puts the recorded lane on its task's card."""
        wm = _wm()
        self.assertEqual(taskkey.record_lane(self.repo, "fix-task-7",
                                             "task/8"), (True, None))
        board = _board([_row("a1", "fix-task-7", "AWAITING_REVIEW")])

        class Src(wm.LiveSources):
            def board(self):
                return board

            def projects(self):
                return dict(PROJECTS)

            def marks(self):
                return board, dict(PROJECTS)

            def read(self, b, p):
                got = wm.LiveSources.read(self, b, p)
                got["tasks"] = _tasks(_task(7), _task(8))
                return got
        src = Src(root=self.repo)
        seen, real = [], taskkey.lands_by_task

        def spy(*args, **kw):
            seen.append((args, real(*args, **kw)))
            return seen[-1][1]
        with mock.patch.object(taskkey, "lands_by_task", spy):
            got = src.read(board, PROJECTS)
        self.assertEqual((got["lanes"], got["lanes_unavailable"]),
                         ({"fix-task-7": frozenset({"task/8"})}, None))
        self.assertEqual(len(seen), 1)
        (args, (index, why)), = seen
        self.assertEqual(args[0], self.repo)
        self.assertEqual(got["task_lands"], None if why else index)
        snap = wm.snapshot(src, now=NOW)
        self.assertEqual(_card_of(snap, "a1")["key"], "task/8")

    def test_a_landed_task_reads_landed_whole_ask_not_yet_re_read(self):
        """No land closes a task (task/3626): an open task whose land the
        one join recorded (`taskkey.lands_by_task`) is LANDED_UNREAD
        (`taskkey.landed_state`), even when no lane of its card names that
        land: in To do past the Landed window, in Landed inside it."""
        wm = _wm()
        self.assertEqual(taskkey.LANDED_UNREAD,
                         "landed, whole ask not yet re-read")
        land = {"land": 470, "train": "train7", "head": "ab" * 20,
                "ts": NOW - 3 * DAY, "lane": "anon", "row": "r9",
                "via": "stored", "abandoned": False}
        inp = _inp(_board([]), tasks=_tasks(_task(9), _task(10)))
        inp["task_lands"] = {"task/9": (land,)}
        snap = wm.build(inp)
        card = snap["cards"]["task/9"]
        self.assertEqual((card["stage"], card["sub"]), ("todo", "landed-open"))
        # the control: an open task no land was recorded for
        self.assertEqual((snap["cards"]["task/10"]["stage"],
                          snap["cards"]["task/10"]["sub"]), ("todo", None))
        # inside the window, its car recording the task
        trains = {"trains": [{"name": "train8", "state": "DONE", "land": 480,
                              "intent": NOW - 8000, "pushed": NOW - 7200,
                              "ended": NOW - 7200, "ran": 27000,
                              "cars": [{"id": "c9", "lane": "anon",
                                        "task": "task/9", "author": "alice",
                                        "reader": "bob"}]}],
                  "lands": {"train8": {"n": 480, "ran": 27000,
                                       "at": NOW - 7200}},
                  "ejections": [], "unavailable": None}
        inp = _inp(_board([]), tasks=_tasks(_task(9)), trains=trains)
        inp["task_lands"] = {"task/9": (dict(land, land=480, train="train8",
                                             ts=NOW - 7200),)}
        card = wm.build(inp)["cards"]["task/9"]
        self.assertEqual((card["stage"], card["sub"]), ("landed", "open"))
        self.assertEqual(card["whose"]["why"], taskkey.LANDED_UNREAD)


class LandedTest(unittest.TestCase):
    """Landed is the last 24 h of lands, each with its proof on the card; a
    task whose work landed but is still open says so; projects that merge by
    hand are unmeasured; Today is counted in the owner's clock."""

    def trains(self, *spec):
        trains, lands = [], {}
        for name, n, pushed, lane in spec:
            trains.append({"name": name, "state": "DONE", "land": n,
                           "intent": pushed - 900, "pushed": pushed,
                           "ended": pushed, "ran": 27000,
                           "cars": [{"lane": lane, "author": "alice",
                                     "reader": "bob"}]})
            lands[name] = {"n": n, "ran": 27000, "at": pushed}
        return {"trains": trains, "lands": lands, "ejections": [],
                "unavailable": None}

    def test_a_land_in_the_window_is_landed_with_its_proof(self):
        snap = _build(_board([]),
                      tasks=_tasks(_task(5, status="closed")),
                      trains=self.trains(("train8", 480, NOW - 7200,
                                          "work-task-5")))
        card = snap["cards"]["task/5"]
        self.assertEqual(card["stage"], "landed")
        self.assertEqual({k: card["land"][k] for k in ("train", "n", "ran")},
                         {"train": "train8", "n": 480, "ran": 27000})

    def test_a_land_older_than_the_window_is_not_drawn_as_landed(self):
        snap = _build(_board([]), trains=self.trains(
            ("train7", 470, NOW - 30 * 3600, "work-task-5")))
        self.assertEqual(snap["cards"], {})

    def test_an_open_task_whose_work_landed_says_so(self):
        snap = _build(_board([]), tasks=_tasks(_task(6)),
                      trains=self.trains(("train8", 480, NOW - 7200,
                                          "work-task-6")))
        card = snap["cards"]["task/6"]
        self.assertEqual((card["stage"], card["sub"]), ("landed", "open"))

    def test_projects_that_merge_by_hand_are_unmeasured(self):
        board = _board([], other={"beta": [_row("b1", "l", "AWAITING_REVIEW")]})
        body = _wm().view(_build(board), NOW)
        self.assertEqual(body["counts"]["landed"]["measured"], ["alpha"])
        self.assertEqual(body["counts"]["landed"]["unmeasured"], ["beta"])

    def test_landed_over_every_project_is_a_floor_while_one_merges_by_hand(
            self):
        """WALK 3, FINDING 1 (task/3723): Home said "61 landed · all
        projects" and the server marked Landed exact, but only alpha's lands
        are recorded: every other project's are not measured. Over every
        project Landed is a floor, and a floor of nothing is unknown."""
        land = self.trains(("train8", 480, NOW - 7200, "work-task-5"))
        tasks = _tasks(_task(5, status="closed"))
        body = _wm().view(_build(_board([], other={"beta": []}), tasks=tasks,
                                 trains=land), NOW)
        self.assertEqual(body["counts"]["stages"]["landed"],
                         {"n": 1, "exact": False, "bound": "at_least"})
        self.assertEqual(body["counts"]["landed"]["others"], 1)
        # a registered project with no work in it merges by hand too
        body = _wm().view(_build(_board([]), tasks=tasks, trains=land), NOW)
        self.assertEqual(body["counts"]["stages"]["landed"]["bound"],
                         "at_least")
        # no land in the window: unknown, never a 0
        body = _wm().view(_build(_board([], other={"beta": []})), NOW)
        self.assertEqual(body["counts"]["stages"]["landed"],
                         {"n": None, "exact": False, "bound": "unknown"})
        # the positive control: alpha alone is every project, and exact
        inp = _inp(_board([]), tasks=tasks, trains=land)
        inp["projects"] = {"alpha": {"name": "alpha"}}
        body = _wm().view(_wm().build(inp), NOW)
        self.assertEqual(body["counts"]["stages"]["landed"],
                         {"n": 1, "exact": True, "bound": None})
        self.assertEqual(body["counts"]["landed"]["others"], 0)
        self.assertTrue(body["scope"]["complete"])

    def test_today_is_counted_in_the_owners_clock(self):
        try:
            from zoneinfo import ZoneInfo
            zone = ZoneInfo("America/Los_Angeles")
        except Exception:                    # noqa: BLE001 — host has no tz data
            self.skipTest("no tz data for the owner's zone on this host")
        local_now = datetime.datetime(2026, 9, 29, 9, 0, tzinfo=zone)
        now = local_now.timestamp()
        after_midnight = datetime.datetime(2026, 9, 29, 0, 30,
                                           tzinfo=zone).timestamp()
        before_midnight = datetime.datetime(2026, 9, 28, 23, 30,
                                            tzinfo=zone).timestamp()
        snap = _wm().build(_inp(_board([]), now=now, trains=self.trains(
            ("train8", 480, after_midnight, "one-task-5"),
            ("train9", 481, before_midnight, "two-task-6")),
            tasks=_tasks(_task(5, status="closed"),
                         _task(6, status="closed"))))
        landed = _wm().view(snap, now)["counts"]["landed"]
        self.assertEqual((landed["n"], landed["today"]), (2, 1))


class TrainCarriedTest(unittest.TestCase):
    """WALK 3, FINDING 3 (task/3723): train483 landed work-page-3643, yet its
    card sat in Landing "reviewed clean; waiting for the train" for 13
    minutes under the line "train483 landed LAND 508". The card carried the
    land, but it was placed by its request row, which the pipeline reading
    still held open, and the train's own proof was read only for a card
    with no open row. A train car IS a request (its id) at a tip: a train
    that pushed the request at its own tip landed it, whatever the reading
    still says, so the row is a record and the card is Landed. A train that
    has not pushed, or that carried another tip of the lane, proves nothing
    about the row."""

    TIP = "ab" * 20

    def trains(self, state="DONE", pushed=NOW - 600, tip=None, rid="r1"):
        return {"trains": [{"name": "train483", "state": state, "land": 508,
                            "intent": NOW - 1500, "pushed": pushed,
                            "ended": pushed or NOW - 300, "ran": 27000,
                            "cars": [{"id": rid, "lane": "work-page-3643",
                                      "tip": tip or self.TIP,
                                      "author": "alice", "reader": "bob"}]}],
                "lands": {"train483": {"n": 508, "ran": 27000, "at": pushed}}
                if pushed else {}, "ejections": [], "unavailable": None}

    def world(self, trains=None, lands=None):
        row = _row("r1", "work-page-3643", "AWAITING_REVIEW",
                   owed_by="integrator", holder="integrator",
                   tip=self.TIP[:12])
        snap = _build(_board([row], lands=lands), trains=trains)
        return snap, snap["cards"]["lane:alpha:work-page-3643"]

    def test_a_request_its_train_landed_is_landed_not_waiting(self):  # noqa: VACUOUS_ASSERTION — the empty `actions` sits beside unconditional positives on the same card (stage "landed", its land, its record), and test_a_gated_train_has_not_landed_it holds the same row as an action
        snap, card = self.world(self.trains())
        self.assertEqual(card["stage"], "landed")
        self.assertEqual(card["actions"], [])
        self.assertEqual((card["land"]["train"], card["land"]["n"]),
                         ("train483", 508))
        rec = _record(snap, "r1")
        self.assertEqual(rec["settlement"]["type"], "record")
        self.assertEqual(rec["settlement"]["evidence"]["train"], "train483")
        self.assertIn("train483", rec["settlement"]["why"])
        self.assertEqual(card["records"], ["r1"])
        body = _wm().view(snap, NOW)
        self.assertEqual(body["counts"]["stages"]["landing"]["n"] or 0, 0)
        wire = next(c for c in body["cards"]
                    if c["key"] == "lane:alpha:work-page-3643")
        self.assertEqual((wire["stage"], wire["whose"]["kind"]),
                         ("landed", "done"))

    def test_the_proof_does_not_wait_for_a_stale_reading(self):
        _snap, card = self.world(self.trains(), lands=_sec(
            scope="alpha", age_s=1300, limit_s=1200, stale=True))
        self.assertEqual(card["stage"], "landed")

    def test_a_gated_train_has_not_landed_it(self):
        _snap, card = self.world(self.trains(state="RUNNING", pushed=None))
        self.assertEqual([a["id"] for a in card["actions"]], ["r1"])
        for state in ("RUNNING", "VETOED", "ABANDONED"):
            with self.subTest(state=state):
                snap, card = self.world(self.trains(state=state,
                                                    pushed=None))
                self.assertEqual((card["stage"], card["sub"]),
                                 ("landing", "clean"))
                self.assertEqual(card["whose"]["kind"], "train")
                self.assertEqual(snap["rows"]["r1"]["settlement"]["type"],
                                 "active")

    def test_a_train_that_carried_another_tip_proves_nothing(self):
        snap, card = self.world(self.trains(tip="cd" * 20))
        self.assertEqual((card["stage"], card["sub"]), ("landing", "clean"))
        self.assertEqual(snap["rows"]["r1"]["settlement"]["type"], "active")
        # the lane's land is still on the card, as evidence
        self.assertEqual(card["land"]["train"], "train483")

    def test_the_positive_control_no_train_is_waiting_for_the_train(self):
        _snap, card = self.world()
        self.assertEqual((card["stage"], card["sub"]), ("landing", "clean"))
        self.assertEqual(card["whose"]["kind"], "train")

    def test_a_source_clean_car_is_carried_at_its_held_tip(self):
        """A SOURCE-CLEAN CAR RIDES AT ITS HELD TIP (`landreq.source_clean_car`,
        task/3723), and the hold door admits any descendant of the
        dispatched tip the row's `tip` names, a cure round's later commit
        (`dispatches._source_clean_lineage_error`). Walk 3's card was such a
        hold ("reviewed clean; waiting for the train"): a train that pushed
        the HELD tip landed the request; one that pushed a tip the hold has
        since moved off proves nothing."""
        held = "cd" * 20

        def world(hold):
            row = _row("r1", "work-page-3643", "AWAITING_REVIEW",
                       owed_by="integrator", holder="integrator",
                       tip=self.TIP[:12], source_clean_tip=hold)
            snap = _build(_board([row]), trains=self.trains(tip=held))
            return snap, snap["cards"]["lane:alpha:work-page-3643"]
        snap, card = world(held)
        self.assertEqual(card["stage"], "landed")
        self.assertEqual(snap["rows"]["r1"]["settlement"]["type"], "record")
        self.assertEqual(
            snap["rows"]["r1"]["settlement"]["evidence"]["train"], "train483")
        snap, card = world("ef" * 20)
        self.assertEqual((card["stage"], card["sub"]), ("landing", "clean"))
        self.assertEqual(snap["rows"]["r1"]["settlement"]["type"], "active")

    def test_the_train_reader_keeps_each_cars_request_and_tip(self):
        """The archive's car carries `id` (the request) and `tip`; the reader
        dropped both, so nothing could say WHICH request a train landed."""
        from helm import autoland, eventledger, landwindow
        train = {"name": "train483", "state": "DONE", "land": 508,
                 "pushed_ts": NOW - 600, "created_ts": NOW - 1500,
                 "cars": [{"id": "r1", "lane": "work-page-3643",
                           "tip": self.TIP, "author": "alice",
                           "reader": "bob"}]}
        with mock.patch.object(autoland, "archived", return_value=[train]), \
                mock.patch.object(autoland, "land_log",
                                  return_value=({}, None)), \
                mock.patch.object(landwindow, "ejections_path",
                                  return_value="/nonexistent/ejections"), \
                mock.patch.object(eventledger, "checked_events",
                                  return_value=([], None)):
            got = _wm().read_trains("/nonexistent-root")
        car = got["trains"][0]["cars"][0]
        self.assertEqual((car["id"], car["tip"], car["lane"]),
                         ("r1", self.TIP, "work-page-3643"))


class SnapshotCacheTest(unittest.TestCase):
    """ONE build per revision; the drawer's crossings come from the same
    snapshot, computed once."""

    class Src:
        def __init__(self, board, tasks):
            self._board, self._tasks = board, tasks
            self._projects = dict(PROJECTS)
            self._marks = None
            self.fp, self.reads, self.boards = "fp-1", 0, 0

        def board(self):
            self.boards += 1
            return self._board

        def projects(self):
            return self._projects

        def marks(self):
            """The four marked sections and the registry, without a whole
            board read: the whole board's sections unless an arm plants
            marks read at another instant."""
            return (self._board if self._marks is None else self._marks,
                    self._projects)

        def paths(self):
            return ["ledger"]

        def fingerprint(self, paths):
            return self.fp

        def read(self, board, projects):
            self.reads += 1
            got = _inp(board, tasks=self._tasks)
            got.pop("now")
            got["projects"] = projects
            return got

    def setUp(self):
        _wm().forget()
        self.addCleanup(_wm().forget)
        self.src = self.Src(_board([_row("a1", "relay-task-7",
                                         "AWAITING_REVIEW")]),
                            _tasks(_task(7)))

    def test_one_revision_is_built_once_and_reused(self):
        wm = _wm()
        first = wm.snapshot(self.src, now=NOW)
        again = wm.snapshot(self.src, now=NOW + 45)
        self.assertIs(first, again)
        self.assertEqual(self.src.reads, 1)

    def test_a_moved_ledger_or_a_new_reading_builds_again(self):
        wm = _wm()
        first = wm.snapshot(self.src, now=NOW)
        self.src.fp = "fp-2"
        second = wm.snapshot(self.src, now=NOW)
        self.assertIsNot(first, second)
        self.src._board = copy.deepcopy(self.src._board)
        self.src._board["sections"]["lands"]["measured_at"] = NOW + 1
        third = wm.snapshot(self.src, now=NOW)
        self.assertIsNot(second, third)
        self.assertEqual(self.src.reads, 3)

    def test_a_poll_with_nothing_moved_reads_no_whole_board(self):
        """The Work page polls every 45 s and every console page counts off
        it (task/3657). A poll whose MARKS (the four sections the revision
        reads, and the registry) name the kept revision is answered from it
        without a whole board read: the board's joins cost seconds on the
        owner's console and the kept snapshot never used them."""
        wm = _wm()
        first = wm.snapshot(self.src, now=NOW)
        again = wm.snapshot(self.src, now=NOW + 45)
        self.assertIs(first, again)
        self.assertEqual(self.src.boards, 1)
        self.assertEqual(self.src.reads, 1)

    def test_a_new_reading_is_kept_under_the_whole_boards_revision(self):
        """The marks only ask whether a revision is kept. A build reads the
        whole board and is kept under THAT board's revision, so a leg read
        that landed between the marks and the whole read is the reading the
        snapshot is filed under, and the next poll's marks find it."""
        wm = _wm()
        older = copy.deepcopy(self.src._board)
        older["sections"]["lands"]["measured_at"] = NOW - 120
        self.src._marks = older
        snap = wm.snapshot(self.src, now=NOW)
        self.assertEqual(snap["rev"], wm.revision(
            self.src._board, self.src._projects, self.src.fp, NOW))
        self.src._marks = None          # the marks now read the new leg
        self.assertIs(wm.snapshot(self.src, now=NOW), snap)
        self.assertEqual((self.src.boards, self.src.reads), (1, 1))

    def test_the_landed_window_tick_builds_again(self):
        wm = _wm()
        first = wm.snapshot(self.src, now=NOW)
        later = wm.snapshot(self.src, now=NOW + wm.TICK_S)
        self.assertIsNot(first, later)

    def test_a_drawer_reads_the_snapshot_its_page_came_from(self):
        wm = _wm()
        page, _ = wm.api({}, src=self.src, now=NOW)
        self.src.fp = "fp-2"
        wm.snapshot(self.src, now=NOW)          # the ledger moved on
        drawer, status = wm.api({"key": ["task/7"], "rev": [page["rev"]]},
                                src=self.src, now=NOW)
        self.assertEqual(status, 200)
        self.assertEqual(drawer["rev"], page["rev"])
        self.assertEqual(self.src.reads, 2)

    def test_crossings_are_computed_once_on_the_snapshot(self):
        wm = _wm()
        snap = wm.snapshot(self.src, now=NOW)
        first = wm.card_events(snap, "task/7")
        self.assertIs(wm.card_events(snap, "task/7"), first)
        self.assertIs(snap["events"]["task/7"], first)
        self.assertEqual(first["timed"] + first["gaps"], len(first["events"]))
        self.assertIsNone(wm.card_events(snap, "task/404"))


class ApiTest(unittest.TestCase):
    """GET /api/work: the page body, one card's drawer, never a 500."""

    def setUp(self):
        _wm().forget()
        self.addCleanup(_wm().forget)
        self.src = SnapshotCacheTest.Src(
            _board([_row("a1", "relay-task-7", "AWAITING_REVIEW")]),
            _tasks(_task(7)))

    def test_the_route_is_served(self):
        from helm import web
        self.assertIn("/api/work", web.QUERY_API)
        self.assertIs(web.QUERY_API["/api/work"], web._api_work)

    def test_the_body_carries_cards_counts_scope_and_the_reading(self):
        body, status = _wm().api({}, src=self.src, now=NOW)
        self.assertEqual(status, 200)
        for key in ("rev", "cards", "records", "counts", "scope", "reading",
                    "zone"):
            self.assertIn(key, body)
        self.assertEqual(body["counts"]["stages"]["review"]["n"], 1)
        self.assertEqual(body["reading"]["live"], 1)

    def test_a_drawer_carries_its_card_and_crossings(self):
        body, status = _wm().api({"key": ["task/7"]}, src=self.src, now=NOW)
        self.assertEqual(status, 200)
        self.assertEqual(body["card"]["key"], "task/7")
        self.assertIsInstance(body["events"], list)

    def test_an_unknown_card_is_a_404_that_says_so(self):
        body, status = _wm().api({"key": ["task/404"]}, src=self.src, now=NOW)
        self.assertEqual(status, 404)
        self.assertIn("task/404", body["unavailable"])

    def test_a_reader_that_raises_answers_unavailable_not_500(self):
        class Broken(SnapshotCacheTest.Src):
            def board(self):
                raise OSError("boom")
        body, status = _wm().api({}, src=Broken(None, None), now=NOW)
        self.assertEqual(status, 200)
        self.assertIn("OSError", body["unavailable"])
        self.assertIsNone(body["counts"])


class OneReadingOverTheWorldsTest(unittest.TestCase):
    """NEVER A SECOND COUNTER: every pipeline number the console prints is
    `pipe_counts` over the boards `/api/board` sends. Its page twin,
    `pipeCounts`, and the parity arm that ran the two side by side were
    retired with the surfaces that drew it (task/3643, slice 6): the Work
    page, the nav badge, Home's tile and a project's row read this reading
    through /api/work and count nothing of their own. What the page's arms
    asserted of the one reading over these worlds (the retired browser half
    of tests/test_web_pipeline_truth.py) is asserted here of the server's."""

    @classmethod
    def setUpClass(cls):
        from tests import test_web_pipeline_truth as truth
        from tests import test_web_landboard as lb
        cls.worlds = dict(truth._worlds(), board=lb._world())
        cls.projects = {r["name"]: r for r in lb._ROWS}

    def read(self, name, project=""):
        return _wm().pipe_counts(self.projects, self.worlds[name], project)

    def test_the_reading_is_the_server_tallies_summed(self):
        c = self.read("board")
        self.assertEqual(c["live"], 90)
        self.assertEqual(c["marks"], {"contrary": 2, "stalled": 10,
                                      "nonbillable": 0, "moving": 78})
        self.assertEqual(c["alarm"], 12)
        self.assertTrue(c["complete"])

    def test_a_scoped_reading_counts_its_project_alone(self):
        c = self.read("board", "alpha")
        self.assertEqual(c["counted"], ["alpha"])
        self.assertEqual((c["live"], c["marks"]["stalled"],
                          c["marks"]["contrary"]), (50, 10, 1))

    def test_the_reading_carries_every_item_its_numbers_count(self):  # noqa: VACUOUS_ASSERTION — the item count is asserted EQUAL to the reading's live count and the first item EQUAL to a literal row; every other arm is an equality
        """One item per live request, each with its project, stage, holder,
        age and task, and a project's items are its tally row for row."""
        c = self.read("whole")
        items = c["items"]
        self.assertEqual(len(items), c["live"])
        for project, tally in c["tallies"].items():
            self.assertEqual(sum(1 for i in items if i["project"] == project),
                             tally["live"], project)
        by_mark, by_holder = {}, {}
        for i in items:
            by_mark[i["mark"]] = by_mark.get(i["mark"], 0) + 1
            by_holder[i["holder"]] = by_holder.get(i["holder"], 0) + 1
        self.assertEqual(by_mark, {k: v for k, v in c["marks"].items() if v})
        self.assertEqual(by_holder, c["holders"])
        first = next(i for i in items if i["id"] == "a-r1")
        self.assertEqual({k: first[k] for k in ("project", "lane", "stage",
                                                 "state", "mark", "holder",
                                                 "age_s", "task")},
                         {"project": "alpha", "lane": "a-rev-1",
                          "stage": "review", "state": "AWAITING_REVIEW",
                          "mark": "contrary", "holder": "codex",
                          "age_s": 600, "task": None})

    def test_a_project_still_being_read_is_in_no_number(self):
        c = self.read("loading")
        self.assertEqual(sorted(c["unread"]), ["beta", "gamma"])
        self.assertEqual(c["counted"], ["alpha"])
        self.assertEqual(c["live"], 50)
        self.assertTrue(c["read"])
        self.assertFalse(c["complete"])

    def test_a_restart_counts_nothing_and_says_nothing_was_read(self):
        c = self.read("restart")
        self.assertEqual(sorted(c["unread"]), ["alpha", "beta", "gamma"])
        self.assertEqual((c["live"], c["read"], c["complete"]),
                         (0, False, False))

    def test_a_floor_of_nothing_over_a_partial_scope_is_no_count(self):  # noqa: VACUOUS_ASSERTION — the arm is one equality against a literal tuple naming the floor of nothing, not an absence
        c = self.read("partial_zero")
        self.assertEqual((c["live"], c["read"], c["complete"]),
                         (0, True, False))

    def test_an_unreadable_fleet_is_unknown_never_a_count(self):
        c = self.read("down")
        self.assertEqual(sorted(c["unknown"]), ["beta", "gamma"])
        self.assertIn("OSError", c["why"])
        self.assertFalse(c["complete"])

    def test_a_stale_reading_keeps_its_numbers_and_says_stale(self):
        c = self.read("stale")
        self.assertEqual((c["live"], c["stale"]), (90, True))
        self.assertEqual(c["limit_s"], 1200)

    def test_a_stale_reading_with_no_clock_claims_no_age(self):  # noqa: VACUOUS_ASSERTION — the arm is one equality against a literal tuple, the live count 90 among it
        c = self.read("noclock")
        self.assertEqual((c["stale"], c["age_s"], c["live"]),
                         (True, None, 90))

    def test_the_reader_counts_the_servers_tallies(self):
        board = _board([_row("a1", "l1", "AWAITING_REVIEW", stalled=True,
                             mark="stalled"),
                        _fix_landed("a2", "l2")],
                       other={"beta": [_row("b1", "l3", "READY")]})
        body = _wm().view(_build(board), NOW)
        self.assertEqual(body["reading"]["live"], 3)
        self.assertEqual(body["reading"]["marks"]["contrary"], 1)
        self.assertEqual(body["reading"]["alarm"], 2)
        self.assertEqual(body["reading"]["tallies"]["alpha"],
                         web_board._kanban_tally(
                             board["projects"]["alpha"]["lanes"]["loops"]))



# ---------------------------------------------------------------------------

class OwedMovesAndUnreadSourcesTest(unittest.TestCase):
    """Seven properties, one arm each (F1-F7): unknown landedness is no land;
    a fold settles nothing; a landed clean hold owes a live close; a room is
    its own move; a reading no longer kept is gone, never swapped; an unread
    land source is no zero; the registry is part of the revision."""

    def test_F1_unknown_landedness_is_neither_a_land_proof_nor_a_debt(self):
        """A fold off the frontier on a line that mixes landed rows with
        abandoned ones does not say whether THIS row landed: its fix is
        still asked for, and nothing claims a land."""
        row = _row("f1", "mixed-room", "CHANGES_REQUESTED", polarity="fix",
                   trunk_contains_tip=None)
        line = _line("off_frontier", [row],
                     by_reason={"landed-by-ancestry": 3,
                                "abandoned-unreachable": 1})
        snap = _build(_board([], collapsed=[line]))
        got = snap["rows"]["f1"]["settlement"]
        self.assertEqual(got["type"], "active")
        self.assertTrue(got["evidence"]["landed"].startswith("unknown"))
        card = _card_of(snap, "f1")
        self.assertEqual((card["stage"], card["sub"]), ("building", "fix"))
        self.assertIsNone(card["debt"])
        self.assertIsNone(card["land"])
        self.assertNotIn("landed", card["reached"])

    def test_F2_a_fold_never_settles_an_unanswered_fix(self):
        for klass in ("unclassified", "superseded", "on_main"):
            with self.subTest(fold=klass):
                row = _row("f2", "folded-" + klass, "CHANGES_REQUESTED",
                           polarity="fix")
                snap = _build(_board([], collapsed=[_line(klass, [row])]))
                got = snap["rows"]["f2"]["settlement"]
                self.assertEqual((got["type"], got["evidence"]["fold"]),
                                 ("active", klass))
                card = _card_of(snap, "f2")
                self.assertEqual((card["stage"], card["sub"]),
                                 ("building", "fix"))
                self.assertIsNone(_record(snap, "f2"))

    def test_F3_a_landed_clean_hold_owes_a_live_close_or_re_hold(self):
        close = _row("c1", "clean-close", "AWAITING_REVIEW",
                     owed_by="integrator", holder="integrator",
                     trunk_contains_tip=True,
                     source_clean_on_main="on main · SOURCE-CLEAN close "
                                          "owed by the integrator")
        rehold = _row("c2", "clean-rehold", "AWAITING_REVIEW",
                      owed_by="reviewer", holder="carol", owes_rehold=True,
                      trunk_contains_tip=True,
                      source_clean_on_main="on main · RE-HOLD owed by @carol")
        snap = _build(_board([close, rehold]))
        self.assertEqual(snap["records"], [])
        a = _card_of(snap, "c1")["actions"][0]
        self.assertEqual((a["settlement"]["type"], a["stage"], a["sub"]),
                         ("active", "landing", "close-owed"))
        # the train takes no landed row: the close is the integrator's
        self.assertEqual(a["whose"]["kind"], "role")
        self.assertEqual(a["whose"]["who"], "integrator")
        b = _card_of(snap, "c2")["actions"][0]
        self.assertEqual((b["sub"], b["whose"]["kind"], b["whose"]["who"]),
                         ("rehold-owed", "seat", "carol"))
        self.assertIn("landed", _card_of(snap, "c2")["reached"])

    def test_F4_an_independent_room_is_its_own_move_and_reaches_the_wire(self):
        review = _row("r1", "review-task-7", "AWAITING_REVIEW",
                      holder="carol")
        room = {"lane": "second-room-task-7", "kind": "claim",
                "seats": ["alice"], "landed": {"state": "unlanded"}}
        snap = _build(_board([review], running=[room]),
                      tasks=_tasks(_task(7)))
        card = snap["cards"]["task/7"]
        moves = {a["id"]: (a["stage"], a["sub"], a["whose"].get("who"))
                 for a in card["actions"]}
        self.assertEqual(moves, {
            "r1": ("review", "wait", "carol"),
            "room:alpha:second-room-task-7": ("building", "writing",
                                              "alice")})
        self.assertEqual(card["stage"], "building")
        wire = next(c for c in _wm().view(snap, NOW)["cards"]
                    if c["key"] == "task/7")
        self.assertEqual([r["lane"] for r in wire["rooms"]],
                         ["second-room-task-7"])
        self.assertEqual(len(wire["actions"]), 2)

    def test_F5_a_drawer_for_an_evicted_reading_says_it_is_gone(self):
        wm = _wm()
        wm.forget()
        self.addCleanup(wm.forget)
        src = SnapshotCacheTest.Src(
            _board([_row("a1", "relay-task-7", "AWAITING_REVIEW")]),
            _tasks(_task(7)))
        page, _ = wm.api({}, src=src, now=NOW)
        for fp in ("fp-2", "fp-3"):             # two newer readings evict it
            src.fp = fp
            wm.snapshot(src, now=NOW)
        self.assertEqual(src.reads, 3)
        body, status = wm.api({"key": ["task/7"], "rev": [page["rev"]]},
                              src=src, now=NOW)
        self.assertEqual(status, 410)
        self.assertEqual(body["gone"], page["rev"])
        self.assertIn("reload", body["unavailable"])
        self.assertNotIn("events", body)
        self.assertNotIn("card", body)
        self.assertEqual(src.reads, 3)          # nothing was built for it

    def test_F6_an_unreadable_land_source_is_no_landed_zero(self):
        trains = {"trains": [], "lands": {}, "ejections": [],
                  "unavailable": "land log unreadable"}
        body = _wm().view(_build(_board([], other={"beta": []}),
                                 tasks=_tasks(_task(1)), trains=trains), NOW)
        self.assertEqual(body["counts"]["stages"]["landed"],
                         {"n": None, "exact": False, "bound": "unknown"})
        self.assertIsNone(body["counts"]["landed"]["n"])
        self.assertIsNone(body["counts"]["landed"]["today"])
        self.assertFalse(body["scope"]["complete"])
        self.assertEqual(body["scope"]["trains_unavailable"],
                         "land log unreadable")
        # the sources that were read still count exactly
        self.assertEqual(body["counts"]["stages"]["todo"]["n"], 1)

    def test_F7_a_changed_project_registry_is_a_new_revision(self):
        wm = _wm()
        wm.forget()
        self.addCleanup(wm.forget)
        src = SnapshotCacheTest.Src(_board([], other={"beta": []}),
                                    _tasks(_task(1)))
        first = wm.snapshot(src, now=NOW)
        self.assertTrue(wm.view(first, NOW)["scope"]["complete"])
        src._projects = dict(PROJECTS, gamma={"name": "gamma"})
        second = wm.snapshot(src, now=NOW)       # every other input frozen
        self.assertNotEqual(second["rev"], first["rev"])
        scope = wm.view(second, NOW)["scope"]
        self.assertEqual(scope["unread"], ["gamma"])
        self.assertFalse(scope["complete"])


class LanePrefixTest(unittest.TestCase):
    """CURE2 3585, P2, carried from the retired board (test_web_landboard's
    `_leased` and `_prefixed` worlds): the leases a lane holds through
    review carry a `lane/` prefix its request does not. A room on
    `lane/a-rev-0` is the room of the request on `a-rev-0`: one card, in
    one stage, and no phantom "writing" card in Building."""

    @staticmethod
    def room(lane, seat):
        return {"kind": "claim", "lane": lane, "seats": [seat],
                "landed": {"state": "unlanded"}}

    def test_a_prefixed_lease_and_its_request_are_one_card(self):
        board = _board([_row("r0", "a-rev-0", "AWAITING_REVIEW")],
                       running=[self.room("lane/a-rev-0", "claude-x")],
                       other={"beta": []})
        body = _wm().view(_build(board), NOW)
        self.assertEqual([(c["key"], c["stage"]) for c in body["cards"]],
                         [("lane:alpha:a-rev-0", "review")])
        self.assertEqual(body["counts"]["stages"]["building"]["n"], 0)
        # the raw lease name is kept on the card for display
        self.assertEqual([r["lane"] for r in body["cards"][0]["rooms"]],
                         ["lane/a-rev-0"])

    def test_a_lease_on_a_request_sent_back_rides_its_card(self):
        board = _board([_row("f0", "a-fix-0", "CHANGES_REQUESTED",
                             polarity="fix")],
                       running=[self.room("lane/a-fix-0", "claude-x"),
                                self.room("a-lease-only", "gemini-y")])
        snap = _build(board)
        body = _wm().view(snap, NOW)
        self.assertEqual(sorted(c["key"] for c in body["cards"]),
                         ["lane:alpha:a-fix-0", "lane:alpha:a-lease-only"])
        self.assertEqual([a["id"] for a in
                          snap["cards"]["lane:alpha:a-fix-0"]["actions"]],
                         ["f0"])
        self.assertEqual(body["counts"]["stages"]["building"]["n"], 2)

    def test_a_task_linked_prefixed_lease_stays_in_review(self):
        """For a task's lane the phantom was worse: the room became a second
        action, and the card moved from In review to Building with a false
        "@seat is writing it"."""
        board = _board([_row("r7", "task-7-x", "AWAITING_REVIEW",
                             holder="carol", reviewer="carol")],
                       running=[self.room("lane/task-7-x", "alice")])
        snap = _build(board, tasks=_tasks(_task(7)))
        card = snap["cards"]["task/7"]
        self.assertEqual((card["stage"], [a["id"] for a in card["actions"]]),
                         ("review", ["r7"]))
        self.assertEqual(card["whose"]["who"], "carol")

    def test_a_prefixed_car_is_the_lanes_land(self):
        trains = {"trains": [{"name": "train9", "state": "DONE", "land": 40,
                              "intent": NOW - 7800, "pushed": NOW - 7200,
                              "ended": NOW - 7200, "ran": 100,
                              "cars": [{"lane": "lane/a-rev-0"}]}],
                  "lands": {}, "ejections": [], "unavailable": None}
        snap = _build(_board([_row("r0", "a-rev-0", "AWAITING_REVIEW")]),
                      trains=trains)
        card = snap["cards"]["lane:alpha:a-rev-0"]
        self.assertEqual((card["land"] or {}).get("train"), "train9")
        self.assertEqual(len(snap["cards"]), 1)


class CureWordsAndTimesTest(unittest.TestCase):
    """Review 3643: a To do card's time in its stage, a land with no train
    on file, and a held approval, each as the owner reads it."""

    def test_a_handed_back_task_is_in_to_do_since_it_came_back(self):  # noqa: VACUOUS_ASSERTION — every arm is an equality against a literal: the sub, the hand-back time, and the control's filing time
        """The after-420 shot read "↩ handed back … 4d ago" and "in stage
        55d" on one row: the stage clock ran from the filing."""
        t = _task(1, ts=NOW - 55 * DAY, last_updated=NOW - 4 * DAY)
        hist = [dict(t, owner="alice", last_updated=NOW - 20 * DAY),
                dict(t, owner=None, released={"by": "alice",
                                              "ts": NOW - 4 * DAY})]
        tasks = {"rows": {"task/1": t}, "history": {"task/1": hist},
                 "unavailable": None}
        card = _build(_board([]), tasks=tasks)["cards"]["task/1"]
        self.assertEqual(card["sub"], "released")
        self.assertEqual(card["since"], NOW - 4 * DAY)
        # the control: a task never handed back is in To do since it was filed
        card = _build(_board([]), tasks=_tasks(_task(2)))["cards"]["task/2"]
        self.assertEqual(card["since"], NOW - 3 * DAY)

    def test_a_land_helm_saw_but_keeps_no_train_for_is_a_gap(self):
        """card_events promises a crossing helm keeps no time for is a gap,
        never left out; a fix debt's work found on trunk had no land at all
        on its path."""
        snap = _build(_board([_fix_landed("f1", "relay")]))
        key = _card_of(snap, "f1")["key"]
        got = _wm().card_events(snap, key)
        self.assertIn([None, "G", "landed"], got["events"])
        # the control: a card no land reached has its room's gap, and no land
        room = {"kind": "claim", "lane": "l1", "seats": ["bob"],
                "landed": {"state": "unlanded"}}
        snap = _build(_board([_row("a1", "l1", "AWAITING_REVIEW")],
                             running=[room]))
        got = _wm().card_events(snap, "lane:alpha:l1")
        self.assertIn([None, "W", "l1"], got["events"])
        self.assertNotIn([None, "G", "landed"], got["events"])

    def test_a_held_approval_says_so_plainly(self):
        row = _row("u1", "l1", "REVIEWED", polarity="approve",
                   holder="unknown", owed_by="unknown")
        card = _card_of(_build(_board([row])), "u1")
        self.assertEqual(card["whose"]["why"],
                         "approved, but by a reader whose approval cannot "
                         "land it")


if __name__ == "__main__":
    unittest.main()
