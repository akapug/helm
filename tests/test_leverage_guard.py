#!/usr/bin/env python3
"""The leverage guard (task/3821): the biggest lever first, checked at the
moment work is chosen.

A seat that picks a task while an open task with more leverage has no lane is
stopped with one line naming what it skips, unless it says why with
`--because "<one line>"`; the skipped ids and the reason are then recorded.
More leverage is (a) a sibling in the same story that the story's `order`
field puts first, or, where no order decides the two, one ranked strictly
higher (its priority, or a fast tax cut at the same priority; age alone
never speaks), or (b) an owner-asked P0/P1 task in the same project two
ranks or more above the pick. A skipped task that continues the chosen one depends on it, so it is
never an inversion. A ledger that cannot be read prints LEVER CHECK UNKNOWN
and the work goes ahead.

The doors: `helm work claim --task`, a chain's first `dispatch send` (build
or review), and the idle-seat offer, which is ordered by rank, then age, with
the story's lead first. The morning report names a story whose lead has no
lane while a later sub-task went first, and owner-asked P0/P1 work with no
lane for more than two hours.

FINISH FIRST, at the same doors: a seat whose started work sits unlanded
(a lane it holds whose tip is not on trunk, with no commit, write or dispatch
for taskkey.FINISH_FIRST_IDLE_S) is stopped from starting new work with one
line naming the oldest highest-leverage item, unless it says why with
`--start-anyway "<one line>"`, which is recorded like `--because`. The
integrator answers for the fleet: every held lane, and every row held
SOURCE-CLEAN for taskkey.FINISH_FIRST_HELD_S that is waiting to land. A lane
claimed with a reason carries it to its first dispatch, `dispatch add
--new-work` is weighed like `send`, and the idle-seat offer names a story's
lead sub-task instead of the story row.

The checks weigh only a seat's own choice: a first row on a lane that already
records its task is not weighed again (the claim was), a retried send gets
its row back, and only the CLI is weighed (`weigh=True`). A sub-task is ranked
by its story, an `in_progress` task has someone on it, the reason also lands
as one comment on the chosen task, and every line names five ids at most.

NEITHER CHECK REFUSES: each STEERS, and speaks only when it matters. The
door prints ONE bigger lever (the best-ranked, oldest) or the oldest stalled
item once per session, chosen task and lever; an owner-asked lever speaks
only over a ranked task at least two ranks below it (a story's lead always
speaks),
then goes ahead with rc 0; with no reason given it records "went ahead
without a reason" on the lane or the dispatch row, and only a reason the
seat gave becomes a comment on the task; the morning ORDER lines show the
skip. Adopting an existing unlanded lane is not a new pick, and a lease
grant is movement on the idle clock. One pick reads the dispatch ledger
once and the task ledger once, and the census spends one deadline. A review send
is not held to FINISH FIRST, a hold whose work reached trunk is finished, and
a lane with an open or held dispatch row is waiting on it, not stalled.

The planted story: S with three P1 sub-tasks filed in order A, B, C, none
with a lane, and the order A, B, C set on S (same-rank siblings speak only
by an explicit order). Every arm runs in a scratch HELM_HOME and a scratch
repository.
"""
import json
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import (brief, dispatches, pk, seats, seats_lander,  # noqa: E402
                  taskkey, tasks, work)
from tests.test_dispatch_chain import ChainBase, run  # noqa: E402
from tests.test_seats import SeatsBase  # noqa: E402
from tests.test_work import WorkBase  # noqa: E402

BECAUSE = "the tooling it needs lands first"
NONE = "went ahead without a reason"
START = "the release needs this before the stalled lane can land"
UNREADABLE = (frozenset(), "the dispatch ledger could not be read (planted)")


def setUpModule():
    """This module writes dispatch rows, so it plants the live-seat stand-in
    (tests._tmphome.pin_live_seats), as its row-writing siblings do."""
    from tests._tmphome import pin_live_seats
    pin_live_seats()


class _Story(object):
    """Fixture verbs shared by every door's arms."""

    def file(self, title, **kw):
        kw.setdefault("project", "helm")
        row, err = tasks.add(title, None, force_new=True, **kw)
        self.assertIsNone(err, err)
        return row["id"]

    def plant(self, root_rank="P1", rank="P1", order=True):
        """S, then its sub-tasks A, B and C (`rank`), filed in that order,
        and, with `order`, S's order set to A, B, C: siblings of one rank
        are ordered only by an explicit order, never by age."""
        self.S = self.file("the release story", priority=root_rank)
        self.A = self.file("sub-task alpha", continues=self.S, priority=rank)
        self.B = self.file("sub-task beta", continues=self.S, priority=rank)
        self.C = self.file("sub-task gamma", continues=self.S, priority=rank)
        if order:
            _row, err = tasks.update(self.S, order=[self.A, self.B, self.C])
            self.assertIsNone(err, err)

    def later(self, hours):
        """The finish-first clock, `hours` from now."""
        return mock.patch.object(taskkey, "_now",
                                 return_value=time.time() + hours * 3600)

    def lander(self, seat):
        """`seat` is the integrator."""
        return mock.patch.object(seats_lander, "lander_seat",
                                 return_value=(seat, None))

    def backdate(self, tid, hours):
        """Append the row again, filed `hours` ago: the ledger's last line
        for an id is the row."""
        row = dict(tasks.get(tid), ts=time.time() - hours * 3600)
        with open(tasks.ledger_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")


class ClaimDoorTest(_Story, WorkBase):
    """Door 1: `helm work claim <lane> --task T`."""

    def claim(self, lane, tid, *extra):
        return self.work("claim", lane, "--seat", "s1", "--task", tid, "--part", *extra)

    def test_a_planted_inversion_steers_and_records_no_reason(self):  # noqa: VACUOUS_ASSERTION — the steer line and the lane's lever record are the controls; no task comment is the contract
        """Arm 1: C goes ahead of A and B, which come first in the story's
        order and have no lane. The claim says so, goes ahead with rc 0, and
        records the skip on the lane as "went ahead without a reason". Round
        5, R4-3: no reason was given, so the task gets no comment."""
        self.plant()
        rc, out, err = self.claim("lane-c", self.C)
        self.assertEqual(0, rc, out + err)
        self.assertIn("LEVER INVERSION: %s goes ahead of %s (story %s, "
                      "no lane)." % (self.C, self.A, self.S), err)
        self.assertNotIn(self.B, err)
        self.assertIn('next time, say why with --because "<one line>"', err)
        self.assertEqual(frozenset({self.C}),
                         taskkey.lane_records(self.root)[0]["lane-c"])
        self.assertEqual({"lane-c": {"task": self.C,
                                     "skipped": [self.A, self.B],
                                     "because": NONE}},
                         taskkey.lever_records(self.root)[0])
        self.assertEqual([], tasks.get(self.C).get("comments") or [])

    def test_the_steer_is_said_once_per_session_and_task(self):  # noqa: VACUOUS_ASSERTION — the first claim's steer and the second's recorded reason are the controls; the second steer's absence is the contract
        """N4: a second pick of the same task in the same session is not
        told again, and its skip is still recorded."""
        self.plant()
        rc, out, err = self.claim("lane-c", self.C)
        self.assertEqual(0, rc, out + err)
        self.assertIn("LEVER INVERSION", err)
        rc, out, err = self.claim("lane-c2", self.C)
        self.assertEqual(0, rc, out + err)
        self.assertIn("serves %s" % self.C, err)
        self.assertNotIn("LEVER INVERSION", out + err)
        self.assertEqual(NONE,
                         taskkey.lever_records(self.root)[0]["lane-c2"][
                             "because"])

    def test_a_new_bigger_lever_speaks_on_a_second_pick(self):  # noqa: VACUOUS_ASSERTION — both claims' steers are asserted to name their lever
        """Round 4, item 5: the latch names the lever, so a new one is said
        on the next pick of the same task."""
        self.plant(root_rank="P2", rank="P2")
        for lane, tid in (("lane-a", self.A), ("lane-b", self.B)):
            rc, out, err = self.claim(lane, tid)
            self.assertEqual(0, rc, out + err)
        first = self.file("an owner-asked lever", priority="P0",
                          origin="owner")
        rc, out, err = self.claim("lane-c", self.C)
        self.assertEqual(0, rc, out + err)
        self.assertIn("goes ahead of %s (owner-asked P0" % first, err)
        _row, err2 = tasks.close(first, "done by hand")
        self.assertIsNone(err2, err2)
        second = self.file("a newer owner-asked lever", priority="P0",
                           origin="owner")
        rc, out, err = self.claim("lane-c2", self.C)
        self.assertEqual(0, rc, out + err)
        self.assertIn("goes ahead of %s (owner-asked P0" % second, err)

    def test_an_aged_latch_entry_does_not_quiet_the_steer(self):  # noqa: VACUOUS_ASSERTION — both claims' steers are asserted present
        """Round 4, item 4: an entry past STEER_KEEP_S is filtered first."""
        self.plant()
        rc, out, err = self.claim("lane-c", self.C)
        self.assertIn("LEVER INVERSION", err)
        with mock.patch.object(taskkey, "_now", return_value=time.time()
                               + taskkey.STEER_KEEP_S + 3600):
            rc, out, err = self.claim("lane-c2", self.C)
        self.assertEqual(0, rc, out + err)
        self.assertIn("LEVER INVERSION", err)

    def test_because_goes_ahead_and_records_the_skipped_and_the_reason(self):
        """Arm 2: the same claim with --because succeeds, and the lane record
        carries the skipped ids and the reason."""
        self.plant()
        rc, out, err = self.claim("lane-c", self.C, "--because", BECAUSE)
        self.assertEqual(0, rc, out + err)
        self.assertEqual(frozenset({self.C}),
                         taskkey.lane_records(self.root)[0]["lane-c"])
        records, why = taskkey.lever_records(self.root)
        self.assertIsNone(why, why)
        self.assertEqual({"lane-c": {"task": self.C,
                                     "skipped": [self.A, self.B],
                                     "because": BECAUSE}}, records)

    def test_skipped_tasks_with_live_lanes_leave_the_claim_silent(self):  # noqa: VACUOUS_ASSERTION — each claim's stderr names the task it serves; no LEVER line is the contract
        """Arm 3, the legitimate half of the plant-and-point pair: A first is
        silent, B after A (which now has a lane) is silent, and C after both
        is silent."""
        self.plant()
        for lane, tid in (("lane-a", self.A), ("lane-b", self.B),
                          ("lane-c", self.C)):
            rc, out, err = self.claim(lane, tid)
            self.assertEqual(0, rc, out + err)
            self.assertIn("serves %s" % tid, err)
            self.assertNotIn("LEVER", out + err)

    def test_the_story_order_beats_age(self):  # noqa: VACUOUS_ASSERTION — the steer names C and the stored order is asserted equal to a literal; B absent and a silent C claim are the contract
        """Arm 4: with order [C, A, B], claiming A fires and names C (and not
        B, which comes after A), and claiming C is silent."""
        self.plant()
        rc, out, err = run(tasks.cmd_task, [
            "update", self.S, "--order",
            ",".join((self.C, self.A, self.B))])
        self.assertEqual(0, rc, out + err)
        self.assertEqual([self.C, self.A, self.B], tasks.get(self.S)["order"])
        rc, out, err = self.claim("lane-a", self.A)
        self.assertEqual(0, rc, out + err)
        self.assertIn("LEVER INVERSION: %s goes ahead of %s (story %s, "
                      "no lane)" % (self.A, self.C, self.S), err)
        self.assertNotIn(self.B, err)
        rc, out, err = self.claim("lane-c", self.C)
        self.assertEqual(0, rc, out + err)
        self.assertNotIn("LEVER", out + err)

    def test_same_rank_siblings_with_no_order_are_silent_by_age(self):  # noqa: VACUOUS_ASSERTION — the explicit-order claim's steer is the control; the silent claims before it are the contract
        """Round 5, R4-1: with no `order`, an older sibling of the same rank
        is not a lever: age is not a judgment. The live cases are this
        shape: task/3745 was told it goes ahead of task/3743, and task/3749
        that it skips task/3743. An explicit order still speaks."""
        self.plant(order=False)
        self.assertEqual((), tasks.leverage_inversion(
            self.C, live=set()).skipped)
        for lane, tid in (("lane-c", self.C), ("lane-b", self.B)):
            rc, out, err = self.claim(lane, tid)
            self.assertEqual(0, rc, out + err)
            self.assertIn("serves %s" % tid, err)
            self.assertNotIn("LEVER", out + err)
        self.assertEqual({}, taskkey.lever_records(self.root)[0])
        _row, err = tasks.update(self.S, order=[self.A, self.B, self.C])
        self.assertIsNone(err, err)
        rc, out, err = self.claim("lane-c2", self.C)
        self.assertEqual(0, rc, out + err)
        self.assertIn("LEVER INVERSION: %s goes ahead of %s (story %s, "
                      "no lane)." % (self.C, self.A, self.S), err)

    def test_with_no_order_a_story_lead_speaks_only_when_it_ranks_higher(self):
        """Round 5, R4-1: a higher priority, or a fast tax cut at the same
        priority, speaks; an older row of the same rank does not."""
        story = self.file("the release story", priority="P1")
        older = self.file("an older P2 part", continues=story, priority="P2")
        lead = self.file("a P1 part", continues=story, priority="P1")
        pick = self.file("a newer P2 part", continues=story, priority="P2")
        cut = self.file("a newer P2 part that is a fast tax cut",
                        continues=story, priority="P2", tax=300,
                        tax_cost=150)
        self.assertEqual([lead, cut], tasks.lever_skipped(
            tasks.leverage_inversion(pick, live=set())))
        rc, out, err = self.claim("lane-pick", pick)
        self.assertEqual(0, rc, out + err)
        self.assertIn("LEVER INVERSION: %s goes ahead of %s (story %s, "
                      "no lane)." % (pick, lead, story), err)
        self.assertEqual([lead, cut], taskkey.lever_records(self.root)[0][
            "lane-pick"]["skipped"])
        self.assertNotIn(older, err)

    def test_an_order_naming_a_row_that_is_not_a_sub_task_is_refused(self):  # noqa: VACUOUS_ASSERTION — the refusal names the row; the order the plant set, unchanged, is the contract
        self.plant()
        other = self.file("a row in no story")
        rc, out, err = run(tasks.cmd_task, [
            "update", self.S, "--order", ",".join((self.C, other))])
        self.assertEqual(2, rc, out + err)
        self.assertIn(other, err)
        self.assertIn("sub-task", err)
        self.assertEqual([self.A, self.B, self.C], tasks.get(self.S)["order"])

    def test_a_skipped_task_that_continues_the_chosen_one_is_no_inversion(self):  # noqa: VACUOUS_ASSERTION — the second claim's steer names the independent row; the dependent row absent is the contract
        """Dependency-aware: an owner-asked P0 that continues C builds on C,
        so claiming C first is the right order. An independent owner-asked P0
        with no lane is the inversion, and is named alone."""
        self.plant(root_rank="P2", rank="P2")
        for lane, tid in (("lane-a", self.A), ("lane-b", self.B)):
            rc, out, err = self.claim(lane, tid)
            self.assertEqual(0, rc, out + err)
        builds_on = self.file("the half that builds on gamma",
                              continues=self.C, priority="P0",
                              origin="owner")
        rc, out, err = self.claim("lane-c", self.C)
        self.assertEqual(0, rc, out + err)
        self.assertNotIn("LEVER", out + err)
        free = self.file("an independent owner-asked lever", priority="P0",
                         origin="owner")
        rc, out, err = self.claim("lane-c2", self.C)
        self.assertEqual(0, rc, out + err)
        self.assertIn("LEVER INVERSION: %s goes ahead of %s (owner-asked P0, "
                      "no lane)" % (self.C, free), err)
        self.assertNotIn(builds_on, err)

    def test_an_unreadable_ledger_says_UNKNOWN_and_the_claim_proceeds(self):  # noqa: VACUOUS_ASSERTION — the UNKNOWN line and the recorded lane are the controls
        self.plant()
        with mock.patch.object(taskkey, "live_tasks", return_value=UNREADABLE):
            rc, out, err = self.claim("lane-c", self.C)
        self.assertEqual(0, rc, out + err)
        self.assertIn("LEVER CHECK UNKNOWN", err)
        self.assertIn("could not be read (planted)", err)
        self.assertEqual(frozenset({self.C}),
                         taskkey.lane_records(self.root)[0]["lane-c"])

    def test_because_without_a_task_is_refused(self):  # noqa: VACUOUS_ASSERTION — the refusal names --task; no room is the contract
        rc, out, err = self.work("claim", "lane-x", "--seat", "s1",
                                 "--because", BECAUSE)
        self.assertEqual(2, rc, out + err)
        self.assertIn("--task", err)
        self.assertFalse(os.path.isdir(os.path.join(self.tmp, "proj-wt",
                                                    "lane-x")))


class ReviewRoundTest(_Story, WorkBase):
    """F2-F5 at the claim door and in the owner's line."""

    def claim(self, lane, tid, *extra):
        return self.work("claim", lane, "--seat", "s1", "--task", tid, "--part", *extra)

    def order_lines(self):
        text, _rc = brief.morning_report(repo=self.root)
        self.assertIn("LANDED", text)
        return [ln for ln in text.splitlines() if ln.startswith("ORDER")]

    def test_a_sub_task_is_ranked_by_its_story(self):  # noqa: VACUOUS_ASSERTION — the lone row's steer names the owner-asked row; the silent sub-task claim is the contract
        """F2: a P2 sub-task of a P0 story weighs as P0, so an owner-asked
        P0 is no lever over it; a lone P2 row is two ranks below it."""
        story = self.file("the release story", priority="P0")
        part = self.file("a P2 part of it", continues=story, priority="P2")
        asked = self.file("an owner-asked row nobody took", priority="P0",
                          origin="owner")
        rc, out, err = self.claim("lane-part", part)
        self.assertEqual(0, rc, out + err)
        self.assertNotIn("LEVER", out + err)
        lone = self.file("a lone P2 row", priority="P2")
        rc, out, err = self.claim("lane-lone", lone)
        self.assertEqual(0, rc, out + err)
        self.assertIn("%s (owner-asked P0, no lane)" % asked, err)

    def test_an_in_progress_task_has_someone_on_it(self):  # noqa: VACUOUS_ASSERTION — the steer names B alone; A absent is the contract
        """F3: what `helm task claim` writes counts as live."""
        self.plant()
        _row, err = tasks.update(self.A, status="in_progress", owner="seat-b")
        self.assertIsNone(err, err)
        rc, out, err = self.claim("lane-c", self.C)
        self.assertEqual(0, rc, out + err)
        self.assertIn("LEVER INVERSION: %s goes ahead of %s (story %s, "
                      "no lane)" % (self.C, self.B, self.S), err)
        self.assertNotIn(self.A, err)

    def test_an_in_progress_lead_leaves_the_owner_line_quiet(self):  # noqa: VACUOUS_ASSERTION — the claim's rc and the LANDED heading are the controls; no ORDER line is the contract
        """F3 in the morning report's story line."""
        self.plant()
        _row, err = tasks.update(self.A, status="in_progress", owner="seat-b")
        self.assertIsNone(err, err)
        rc, out, err = self.claim("lane-c", self.C, "--because", BECAUSE)
        self.assertEqual(0, rc, out + err)
        self.assertEqual([], self.order_lines())

    def test_the_reason_is_one_comment_on_the_chosen_task(self):
        """F4: the lane record goes with the branch; the comment stays."""
        self.plant()
        rc, out, err = self.claim("lane-c", self.C, "--because", BECAUSE)
        self.assertEqual(0, rc, out + err)
        self.assertEqual(
            ['lane lane-c took %s ahead of %s, %s (--because): "%s"'
             % (self.C, self.A, self.B, BECAUSE)],
            [c.get("text") for c in tasks.get(self.C)["comments"]])

    def test_the_owner_asked_line_says_what_went_ahead_and_why(self):
        """F4: the owner-asked line names the task that went ahead of its
        rows and the reason."""
        waiting = self.file("an owner-asked row nobody took", priority="P0",
                            origin="owner")
        self.backdate(waiting, 3)
        lone = self.file("a smaller row", priority="P2")
        rc, out, err = self.claim("lane-lone", lone, "--because", BECAUSE)
        self.assertEqual(0, rc, out + err)
        lines = self.order_lines()
        self.assertEqual(1, len(lines), lines)
        self.assertIn(waiting, lines[0])
        self.assertIn('went ahead: %s "%s"' % (lone, BECAUSE), lines[0])

    def test_the_steer_names_one_lever_and_the_owner_line_five(self):  # noqa: VACUOUS_ASSERTION — each line is asserted to name its ids; the ids past the cap absent is the contract
        """F5, and round 4's one lever at the door: the best-ranked,
        oldest."""
        asked = [self.file("owner-asked row %s" % word, priority="P0",
                           origin="owner")
                 for word in ("one", "two", "three", "four", "five", "six",
                              "seven")]
        for tid in asked:
            self.backdate(tid, 3)
        lone = self.file("a smaller row", priority="P2")
        rc, out, err = self.claim("lane-lone", lone)
        self.assertEqual(0, rc, out + err)
        (line,) = [ln for ln in err.splitlines() if "LEVER INVERSION" in ln]
        (order,) = [ln for ln in self.order_lines() if "owner-asked" in ln]
        self.assertNotIn("more", line)
        for got, cap in ((line, 1), (order, 5)):
            words = got.replace(",", " ").replace(")", " ") + " "
            for tid in asked[:cap]:
                self.assertIn(tid + " ", words)
            for tid in asked[cap:]:
                self.assertNotIn(tid + " ", words)
        self.assertIn("+2 more", order)

    def test_the_fire_rate_on_a_realistic_backlog(self):
        """Round 4, item 2 (the attention budget): a backlog shaped like the
        live one, measured by asking the check for every open task as a
        fresh pick. Three stale owner-asked P0s nobody took; four P1 stories
        of eight sub-tasks each (ranks P2, P2, P2, unranked, unranked, P3,
        P2, unranked) with no order; and lone rows P1 x6, P2 x20, P3 x5,
        unranked x20. It speaks for the 16 sub-tasks ranked below their
        story's P2 lead (the P3 and the three unranked in each story; the
        other P2s are only younger, R4-1) and the 25 lone P2/P3 rows two
        ranks below an owner-asked P0: 41 of 90. Round 4 spoke for 53, and
        the rule before it for 87."""
        cycle = ("P2", "P2", "P2", None, None, "P3", "P2", None)
        for n in range(3):
            self.file("stale owner-asked lever %d" % n, priority="P0",
                      origin="owner")
        for n in range(4):
            story = self.file("story %d" % n, priority="P1")
            for m, rank in enumerate(cycle):
                self.file("story %d part %d" % (n, m), continues=story,
                          priority=rank)
        for rank, count in (("P1", 6), ("P2", 20), ("P3", 5), (None, 20)):
            for m in range(count):
                self.file("lone %s row %d" % (rank, m), priority=rank)
        known = tasks.rows()
        opened = [t for t, r in known.items() if r.get("status") == "open"]
        fired = [t for t in opened if tasks.leverage_inversion(
            t, known=known, live=set()).skipped]
        self.assertEqual((41, 90), (len(fired), len(opened)))


class SendDoorTest(_Story, ChainBase):
    """Door 2: the FIRST row of a chain that carries a task, build or
    review."""

    def send(self, lane="lane-c", kind="build", **kw):
        kw.setdefault("ref", self.a)
        self.said = kw.setdefault("steer", [])
        return dispatches.send("seat-b", lane, "take the gamma part",
                               kw.pop("ref"), repo=self.repo, kind=kind,
                               sign=False, weigh=True, **kw)

    def test_a_first_build_row_skipping_the_story_order_steers(self):
        self.plant()
        row, why, _sent = self.send(new_work=True, task=self.C)
        self.assertIsNone(why, why)
        self.assertEqual(1, len(self.said), self.said)
        self.assertIn("LEVER INVERSION: %s goes ahead of %s (story %s, "
                      "no lane)." % (self.C, self.A, self.S), self.said[0])
        stored = dispatches.rows()[row["id"]]
        self.assertEqual([self.A, self.B], stored["lever_skipped"])
        self.assertEqual(NONE, stored["lever_because"])

    def test_a_first_review_row_is_checked_too(self):  # noqa: VACUOUS_ASSERTION — the steer names the task and the row stores the recorded reason
        self.plant()
        row, why, _sent = self.send(kind="review", new_work=True, task=self.C)
        self.assertIsNone(why, why)
        self.assertIn("LEVER INVERSION: %s goes ahead of" % self.C,
                      self.said[0])
        self.assertEqual(NONE, dispatches.rows()[row["id"]]["lever_because"])

    def test_because_records_the_skipped_and_the_reason_on_the_row(self):
        self.plant()
        row, why, _sent = self.send(new_work=True, task=self.C,
                                    lever_because=BECAUSE)
        self.assertIsNone(why, why)
        stored = dispatches.rows()[row["id"]]
        self.assertEqual(self.C, stored["task"])
        self.assertEqual([self.A, self.B], stored["lever_skipped"])
        self.assertEqual(BECAUSE, stored["lever_because"])

    def test_a_first_row_reads_each_ledger_once(self):
        """Round 5, R4-5 at the dispatch door: the retry lookup and both
        checks share one dispatch snapshot and one task-ledger read."""
        self.plant()
        row = {"id": "f" * 32, "task": self.C, "sender": "seat-b",
               "repo_root": self.repo, "lane": "lane-c", "kind": "build"}
        with mock.patch.object(dispatches, "snapshot",
                               wraps=dispatches.snapshot) as read_rows, \
                mock.patch.object(tasks, "snapshot",
                                  wraps=tasks.snapshot) as read_tasks:
            why = dispatches._lever_rung(row, None, steer=[])
        self.assertIsNone(why, why)
        self.assertEqual([self.A, self.B], row["lever_skipped"])
        self.assertEqual((1, 1), (read_rows.call_count, read_tasks.call_count))

    def test_a_later_row_in_the_chain_stays_silent(self):  # noqa: VACUOUS_ASSERTION — the later row's chain root is asserted equal to the first row; absent lever fields are the contract
        self.plant()
        first, why, _sent = self.send(new_work=True, task=self.C,
                                      lever_because=BECAUSE)
        self.assertIsNone(why, why)
        later, why, _sent = self.send(lane="lane-c-2", ref=self.b,
                                      supersedes=first["id"])
        self.assertIsNone(why, why)
        self.assertEqual(first["id"], later["chain_root"])
        self.assertNotIn("lever_skipped", later)
        self.assertNotIn("lever_because", later)

    def test_an_unreadable_ledger_says_UNKNOWN_and_the_send_proceeds(self):
        self.plant()
        with mock.patch.object(taskkey, "live_tasks", return_value=UNREADABLE):
            row, why, _sent = self.send(new_work=True, task=self.C)
        self.assertIsNone(why, why)
        stored = dispatches.rows()[row["id"]]
        self.assertEqual(self.C, stored["task"])
        self.assertIn("could not be read (planted)", stored["lever_unknown"])

    def test_the_cli_steers_with_rc_0_and_takes_because(self):  # noqa: VACUOUS_ASSERTION — the first send's steer names the skipped ids and both rows store their reasons; the second send's silence is the contract
        self.plant()

        def argv(lane, *extra):
            return ["send", "seat-b", lane, "take the gamma part", "--ref",
                    self.a, "--kind", "build", "--repo", self.repo,
                    "--new-work", "--task", self.C, "--part"] + list(extra)
        rc, out, err = run(dispatches.cmd_dispatch, argv("lane-c"))
        self.assertEqual(0, rc, out + err)
        self.assertIn("LEVER INVERSION: %s goes ahead of %s (story"
                      % (self.C, self.A), err)
        rc, out, err = run(dispatches.cmd_dispatch,
                           argv("lane-c2", "--because", BECAUSE))
        self.assertEqual(0, rc, out + err)
        self.assertIn(BECAUSE, out + err)
        self.assertNotIn("LEVER INVERSION", out + err)
        rows = sorted(dispatches.rows().values(), key=lambda r: r["lane"])
        self.assertEqual([NONE, BECAUSE],
                         [r["lever_because"] for r in rows])
        # F4: a reason the seat gave is one comment on the chosen task, and
        # a retry that gets the same row back adds none. Round 5, R4-3: the
        # row with no reason keeps it on the row and comments nothing.
        run(dispatches.cmd_dispatch, argv("lane-c2", "--because", BECAUSE))
        self.assertEqual(
            ['dispatch %s took %s ahead of %s, %s (--because): "%s"'
             % (rows[1]["id"][:12], self.C, self.A, self.B, BECAUSE)],
            [c.get("text") for c in tasks.get(self.C)["comments"]])

    def test_a_later_refusal_does_not_swallow_the_steer(self):  # noqa: VACUOUS_ASSERTION — the steer and the planted refusal are both asserted in stderr
        """Round 4, item 3: the steer is marked said by the check, so it is
        printed even when the send is refused after it."""
        self.plant()
        with mock.patch.object(dispatches, "_append_dispatch",
                               return_value=(None, "planted refusal", False)):
            rc, out, err = run(dispatches.cmd_dispatch, [
                "send", "seat-b", "lane-c", "take the gamma part", "--ref",
                self.a, "--kind", "build", "--repo", self.repo, "--new-work",
                "--task", self.C, "--part"])
        self.assertNotEqual(0, rc, out + err)
        self.assertIn("planted refusal", err)
        self.assertIn("LEVER INVERSION: %s goes ahead of %s" % (self.C, self.A),
                      err)

    def test_a_retried_send_gets_its_row_back_not_a_refusal(self):  # noqa: VACUOUS_ASSERTION — the retry's row is asserted equal to the first row's id; no LEVER in its reason is the contract
        """F6: the existing-operation lookup comes first."""
        self.plant()
        first, why, _sent = self.send(new_work=True, task=self.C,
                                      lever_because=BECAUSE)
        self.assertIsNone(why, why)
        again, why, _sent = self.send(new_work=True, task=self.C)
        self.assertEqual(first["id"], again["id"])
        self.assertNotIn("LEVER", why or "")

    def test_an_internal_sender_is_never_weighed(self):  # noqa: VACUOUS_ASSERTION — the row is written and asserted to carry its task; absent lever fields are the contract
        """F6: only the CLI weighs a row (`weigh=True`)."""
        self.plant()
        row, why, _sent = dispatches.send(
            "seat-b", "lane-c", "take the gamma part", self.a,
            repo=self.repo, kind="build", sign=False, new_work=True,
            task=self.C)
        self.assertIsNone(why, why)
        stored = dispatches.rows()[row["id"]]
        self.assertEqual(self.C, stored["task"])
        self.assertNotIn("lever_skipped", stored)


class OfferOrderTest(_Story, SeatsBase):
    """Door 3: the idle-seat offer is ordered by rank, then age, as its
    docstring says, and a story's lead comes first."""

    def setUp(self):
        super().setUp()
        seats.join(session="s-lever", seat="seat-under-test", cwd="/tmp/p")

    def offers(self):
        with mock.patch.object(seats, "_git_project", return_value="helm"):
            return seats._offer_rows("seat-under-test")

    def test_the_task_producer_offers_rank_before_age(self):
        low = self.file("an older row ranked P2", priority="P2")
        high = self.file("a newer row ranked P0", priority="P0")
        got = [o[5]["id"] for o in tasks.offer_rows(seat="seat-under-test", live=())]
        self.assertEqual([high, low], got)

    def test_a_ranked_task_row_leads_an_older_unranked_dispatch_row(self):
        path = dispatches.ledger_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        ts = pk.now_ts()
        with open(path, "a", encoding="utf-8") as fh:
            for event in ({"v": 3, "event": "dispatch", "seq": 0,
                           "id": "ab12cd34ef560011", "ts": ts,
                           "recipient": "ghost", "lane": "review the canary",
                           "tip": "a" * 40, "ref": "abc", "deadline_s": 2700,
                           "status": "open"},
                          {"v": 3, "event": "delivered", "seq": 1,
                           "id": "ab12cd34ef560011", "ts": ts,
                           "delivery_ref": "dm-x"}):
                fh.write(json.dumps(event) + "\n")
        high = self.file("a newer row ranked P0", priority="P0")
        rows = self.offers()
        self.assertEqual([high, "ab12cd34ef560011"],
                         [r[5]["id"] for r in rows])
        self.assertEqual("task", rows[0][4])

    def test_the_story_order_puts_its_lead_first(self):  # noqa: VACUOUS_ASSERTION — the offer order is asserted equal to literal ids before and after the order is set
        self.plant(root_rank="P3")
        rows = self.offers()
        self.assertEqual([self.A, self.B, self.C],
                         [r[5]["id"] for r in rows])
        _row, err = tasks.update(self.S, order=[self.C, self.A, self.B])
        self.assertIsNone(err, err)
        rows = self.offers()
        self.assertEqual([self.C, self.A, self.B],
                         [r[5]["id"] for r in rows])

    def test_a_story_row_is_offered_as_its_lead_sub_task(self):  # noqa: VACUOUS_ASSERTION — the offer is asserted equal to literal ids; the story row absent is the contract
        """Gap (c): the story's sub-tasks are the work, so a P0 story whose
        sub-tasks are P2 is offered as its lead sub-task, at the story's
        place, and the story row itself is not offered."""
        self.S = self.file("the release story", priority="P0")
        self.A = self.file("sub-task alpha", continues=self.S, priority="P2")
        self.B = self.file("sub-task beta", continues=self.S, priority="P2")
        lone = self.file("a lone P1 row", priority="P1")
        rows = self.offers()
        self.assertEqual([self.A, lone, self.B], [r[5]["id"] for r in rows])


class FinishFirstClaimTest(_Story, WorkBase):
    """FINISH FIRST at `helm work claim --task`."""

    def claim(self, lane, tid, *extra):
        return self.work("claim", lane, "--seat", "s1", "--task", tid, "--part", *extra)

    def started(self, lane, seat="s1", hours=0):
        """A lane `seat` holds, with one commit that is not on trunk, dated
        `hours` ago -> the lease id."""
        rc, out, err = self.work("claim", lane, "--seat", seat)
        self.assertEqual(0, rc, out + err)
        room = work.lane_path(self.root, lane)
        with open(os.path.join(room, lane + ".txt"), "w") as fh:
            fh.write("started\n")
        env = dict(os.environ, HELM_WORK_INTEGRATOR="1")
        if hours:
            stamp = "%d +0000" % (time.time() - hours * 3600)
            env.update(GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
        for cmd in (("add", "-A"), ("commit", "-q", "-m", "start " + lane)):
            got = subprocess.run(("git", "-C", room) + cmd,
                                 capture_output=True, text=True, env=env)
            self.assertEqual(0, got.returncode, got.stderr)
        return out.strip().split("\t")[2]

    def test_a_stalled_lane_of_this_seat_steers_a_new_pick(self):
        self.started("old-lane")
        tid = self.file("a new piece of work")
        with self.later(5):
            rc, out, err = self.claim("new-lane", tid)
        self.assertEqual(0, rc, out + err)
        self.assertIn("FINISH FIRST: ", err)
        self.assertIn("lane/old-lane", err)
        self.assertIn('--start-anyway "<one line>"', err)
        self.assertEqual({"new-lane": {"task": tid, "over": ["lane/old-lane"],
                                       "reason": NONE}},
                         taskkey.start_records(self.root)[0])

    def test_one_stalled_item_is_said_once_not_on_every_pick(self):
        """The FINISH FIRST line names the stalled item, not the pick, so a
        second pick of a different task in the same session does not say it
        again; the skip is still recorded. A new stalled item that leads the
        census speaks. Measured on the live ledger 2026-09-30: one hold held
        108h to 127h led all 32 of the integrator's picks."""
        self.started("old-lane")
        first = self.file("a new piece of work")
        second = self.file("another new piece of work")
        with self.later(5):
            rc, out, err = self.claim("new-lane", first)
            self.assertEqual(0, rc, out + err)
            self.assertIn("FINISH FIRST: ", err)
            self.assertIn("lane/old-lane", err)
            rc, out, err = self.claim("new-lane-2", second)
        self.assertEqual(0, rc, out + err)
        self.assertNotIn("FINISH FIRST", out + err)
        self.assertEqual({"task": second, "over": ["lane/old-lane"],
                          "reason": NONE},
                         taskkey.start_records(self.root)[0]["new-lane-2"])
        self.started("hot-lane")
        hot = self.file("the lever the hot lane serves", priority="P0")
        written, why = taskkey.record_lane(self.root, "hot-lane", hot)
        self.assertTrue(written, why)
        third = self.file("a third new piece of work")
        with self.later(5):
            rc, out, err = self.claim("new-lane-3", third)
        self.assertEqual(0, rc, out + err)
        self.assertIn("FINISH FIRST: ", err)
        self.assertIn("lane/hot-lane", err)

    def test_a_lease_grant_is_movement_on_the_idle_clock(self):
        """Round 5, R4-4: a lane whose last commit is 72h old, granted now,
        is not stalled seconds later; five hours after the grant it is, and
        the line counts from the grant."""
        self.started("old-lane", hours=72)
        tid = self.file("a new piece of work")
        rc, out, err = self.claim("new-lane", tid)
        self.assertEqual(0, rc, out + err)
        self.assertIn("serves %s" % tid, err)
        self.assertNotIn("FINISH FIRST", out + err)
        other = self.file("another new piece of work")
        with self.later(5):
            rc, out, err = self.claim("new-lane-2", other)
        self.assertEqual(0, rc, out + err)
        self.assertIn("FINISH FIRST: ", err)
        self.assertIn("lane/old-lane", err)
        self.assertIn("for 5h", err)

    def test_adopting_an_unlanded_lane_is_not_a_new_pick(self):  # noqa: VACUOUS_ASSERTION — the fresh claim's two steers after it are the control; the silent adoption and its empty records are the contract
        """Round 5, R4-4: claiming an existing lane whose work is not on
        trunk takes up started work, so neither check weighs it; a fresh
        lane for the same task right after is weighed by both."""
        self.plant()
        self.started("old-lane")
        lease = self.started("adopt-me")
        rc, out, err = self.work("release", "adopt-me", "--seat", "s1",
                                 "--lease", lease)
        self.assertEqual(0, rc, out + err)
        self.assertTrue(os.path.isdir(work.lane_path(self.root, "adopt-me")))
        with self.later(5):
            rc, out, err = self.claim("adopt-me", self.C)
            self.assertEqual(0, rc, out + err)
            self.assertIn("serves %s" % self.C, err)
            self.assertNotIn("LEVER", out + err)
            self.assertNotIn("FINISH FIRST", out + err)
            self.assertEqual({}, taskkey.lever_records(self.root)[0])
            self.assertEqual({}, taskkey.start_records(self.root)[0])
            rc, out, err = self.claim("new-lane", self.C)
        self.assertEqual(0, rc, out + err)
        self.assertIn("LEVER INVERSION: %s goes ahead of %s" % (self.C,
                                                                 self.A), err)
        self.assertIn("FINISH FIRST: ", err)
        self.assertIn("lane/old-lane", err)

    def test_one_pick_reads_each_ledger_once(self):
        """Round 5, R4-5: the two checks and the FINISH FIRST line share one
        dispatch snapshot and one task-ledger read."""
        from helm.work import _cli
        self.plant()
        self.started("old-lane")
        with self.later(5), \
                mock.patch.object(dispatches, "snapshot",
                                  wraps=dispatches.snapshot) as read_rows, \
                mock.patch.object(tasks, "snapshot",
                                  wraps=tasks.snapshot) as read_tasks:
            gate, rc = _cli._lever_gate(self.root, "new-lane", self.C, [],
                                        "s1")
        self.assertIsNone(rc)
        lever, finish = gate
        self.assertEqual([self.A, self.B], tasks.lever_skipped(lever))
        self.assertEqual(["lane/old-lane"], taskkey.finish_over(finish))
        self.assertEqual((1, 1), (read_rows.call_count, read_tasks.call_count))

    def test_a_lane_waiting_on_a_dispatch_row_is_not_stalled(self):
        """N3: an open or held row on the lane means it waits on someone."""
        self.started("old-lane")
        waiting = {"r" * 32: {"id": "r" * 32, "lane": "old-lane",
                              "status": "open", "ts": pk.now_ts()}}
        with self.later(5):
            quiet = taskkey.in_flight(self.root, "s1", current=waiting)
            loud = taskkey.in_flight(self.root, "s1", current={})
        self.assertEqual((), quiet.items)
        self.assertEqual(["lane/old-lane"], taskkey.finish_over(loud))

    def test_start_anyway_goes_ahead_and_records_what_it_went_ahead_of(self):
        self.started("old-lane")
        tid = self.file("a new piece of work")
        with self.later(5):
            rc, out, err = self.claim("new-lane", tid, "--start-anyway", START)
        self.assertEqual(0, rc, out + err)
        records, why = taskkey.start_records(self.root)
        self.assertIsNone(why, why)
        self.assertEqual({"new-lane": {"task": tid, "over": ["lane/old-lane"],
                                       "reason": START}}, records)

    def test_a_fresh_lane_is_silent(self):  # noqa: VACUOUS_ASSERTION — the claim's stderr names the task it serves; no FINISH FIRST line is the contract
        self.started("fresh-lane")
        tid = self.file("a new piece of work")
        rc, out, err = self.claim("new-lane", tid)
        self.assertEqual(0, rc, out + err)
        self.assertIn("serves %s" % tid, err)
        self.assertNotIn("FINISH FIRST", out + err)

    def test_another_seats_lane_is_theirs_and_the_integrators(self):  # noqa: VACUOUS_ASSERTION — the integrator's steer names the other seat's lane; the silent claim is the contract
        self.started("their-lane", seat="s2")
        tid = self.file("a new piece of work")
        with self.later(5):
            rc, out, err = self.claim("new-lane", tid)
        self.assertEqual(0, rc, out + err)
        self.assertNotIn("FINISH FIRST", out + err)
        other = self.file("another new piece of work")
        with self.later(5), self.lander("s1"):
            rc, out, err = self.claim("new-lane-2", other)
        self.assertEqual(0, rc, out + err)
        self.assertIn("lane/their-lane", err)

    def test_an_unreadable_census_says_UNKNOWN_and_the_claim_proceeds(self):
        from helm.work import _gc
        tid = self.file("a new piece of work")
        with mock.patch.object(_gc, "list_rows",
                               side_effect=OSError("planted census")):
            rc, out, err = self.claim("new-lane", tid)
        self.assertEqual(0, rc, out + err)
        self.assertIn("FINISH FIRST CHECK UNKNOWN", err)
        self.assertIn("planted census", err)

    def test_start_anyway_without_a_task_is_refused(self):  # noqa: VACUOUS_ASSERTION — the refusal names --task; no room is the contract
        rc, out, err = self.work("claim", "lane-x", "--seat", "s1",
                                 "--start-anyway", START)
        self.assertEqual(2, rc, out + err)
        self.assertIn("--start-anyway answers a check only a --task claim "
                      "runs", err)
        self.assertFalse(os.path.isdir(work.lane_path(self.root, "lane-x")))


class FinishFirstSendTest(_Story, ChainBase):
    """FINISH FIRST at a chain's first `dispatch send` and `add`, the
    integrator's source-clean rows, and a lane's reasons carried to its
    first dispatch."""

    def send(self, lane="lane-new", **kw):
        self.said = kw.setdefault("steer", [])
        kw.setdefault("kind", "build")
        if kw["kind"] == "review":
            kw.setdefault("task", self.review_task["id"])
        return dispatches.send("seat-b", lane, "take the next part", self.b,
                               repo=self.repo, sign=False, new_work=True,
                               weigh=True, **kw)

    def held_clean(self, landed=False, lane="waiting-lane"):
        """A row its recipient held SOURCE-CLEAN, waiting to land: its tip
        is a commit on the lane's own branch, which trunk does not hold
        unless `landed`."""
        tip = self.a
        if not landed:
            self.git("checkout", "-q", "-b", "lane/" + lane)
            tip = self.commit("work on " + lane)
            self.git("checkout", "-q", self.main)
        row = self.root(lane=lane, ref=tip)
        with mock.patch.object(dispatches, "_acting_author",
                               return_value=(row["recipient"], None)):
            held, why = dispatches.mark_hold(
                row["id"], "awaiting the land gate; fab Ran 5 tests OK", source_clean_tip=tip)
        self.assertIsNone(why, why)
        return held

    def test_the_integrator_is_steered_to_a_source_clean_row_first(self):  # noqa: VACUOUS_ASSERTION — the steer names the held row and both rows store their reasons; the quiet sends are the contract
        held = self.held_clean()
        with self.later(2):
            quiet, why, _sent = self.send(lane="lane-quiet")
        self.assertIsNone(why, why)
        self.assertNotIn("start_anyway", quiet)
        with self.lander("integrator"):
            fresh, why, _sent = self.send(lane="lane-fresh")
            self.assertIsNone(why, why)
            self.assertNotIn("start_anyway", fresh)
            with self.later(2):
                row, why, _sent = self.send()
                self.assertIsNone(why, why)
                self.assertIn("FINISH FIRST: ", self.said[0])
                self.assertIn(held["id"][:12], self.said[0])
                self.assertIn("SOURCE-CLEAN", self.said[0])
                again, why, _sent = self.send(lane="lane-new-2",
                                              start_anyway=START)
        self.assertIsNone(why, why)
        over = ["dispatch:" + held["id"][:12]]
        stored = dispatches.rows()
        self.assertEqual((NONE, over), (stored[row["id"]]["start_anyway"],
                                        stored[row["id"]]["start_anyway_over"]))
        self.assertEqual((START, over),
                         (stored[again["id"]]["start_anyway"],
                          stored[again["id"]]["start_anyway_over"]))

    def test_an_unknown_landing_proof_falls_back_to_git_cherry(self):  # noqa: VACUOUS_ASSERTION — the unlanded hold is recorded on the row; the landed hold's absence is the contract
        """Round 4, item 1: landreq's proof reads unknown for a tip its
        patch index does not cover; `git cherry` against the pinned trunk
        decides that one tip."""
        from helm import landreq
        self.held_clean(landed=True, lane="landed-lane")
        held = self.held_clean()
        with mock.patch.object(landreq, "_landing_proof",
                               return_value="unknown"), \
                self.later(2), self.lander("integrator"):
            row, why, _sent = self.send(lane="lane-one")
        self.assertIsNone(why, why)
        self.assertEqual(["dispatch:" + held["id"][:12]],
                         dispatches.rows()[row["id"]]["start_anyway_over"])

    def test_a_hold_whose_landing_cannot_be_read_is_counted(self):
        """Round 4, item 1: a false steer is one line; a silent skip hides a
        stall."""
        from helm import landreq
        held = self.held_clean(landed=True, lane="landed-lane")
        with mock.patch.object(landreq, "_landing_proof",
                               return_value="unknown"), \
                mock.patch.object(taskkey, "_cherry_landed",
                                  return_value=None), \
                self.later(2), self.lander("integrator"):
            row, why, _sent = self.send(lane="lane-one")
        self.assertIsNone(why, why)
        self.assertEqual(["dispatch:" + held["id"][:12]],
                         dispatches.rows()[row["id"]]["start_anyway_over"])

    def test_the_census_spends_one_deadline_across_its_holds(self):  # noqa: VACUOUS_ASSERTION — the one cherry timeout asked and both holds recorded on the row are asserted equal to literals
        """Round 5, R4-5: every hold's landing proof and git cherry share
        one census deadline, so N holds cost one budget, not N. Once it is
        spent, a hold is counted without asking git."""
        from helm import landreq
        first = self.held_clean(lane="waiting-one")
        second = self.held_clean(lane="waiting-two")
        clock, asked = [1000.0], []

        def cherry(gitdir, trunk, tip, timeout):
            asked.append(timeout)
            clock[0] += taskkey.CENSUS_BUDGET_S
            return None
        with mock.patch.object(landreq, "_landing_proof",
                               return_value="unknown"), \
                mock.patch.object(taskkey, "_monotonic",
                                  side_effect=lambda: clock[0]), \
                mock.patch.object(taskkey, "_cherry_landed",
                                  side_effect=cherry), \
                self.later(2), self.lander("integrator"):
            row, why, _sent = self.send(lane="lane-one")
        self.assertIsNone(why, why)
        self.assertEqual([taskkey.CENSUS_BUDGET_S], asked)
        self.assertEqual(
            sorted("dispatch:" + h["id"][:12] for h in (first, second)),
            sorted(dispatches.rows()[row["id"]]["start_anyway_over"]))

    def test_a_review_send_is_not_held_to_finish_first(self):  # noqa: VACUOUS_ASSERTION — the build send's row records the held row; the review row's absent fields are the contract
        """N1: a review moves started work toward landing."""
        held = self.held_clean()
        with self.later(2), self.lander("integrator"):
            review, why, _sent = self.send(lane="lane-read", kind="review")
            self.assertIsNone(why, why)
            build, why, _sent = self.send(lane="lane-build")
        self.assertIsNone(why, why)
        stored = dispatches.rows()
        self.assertNotIn("start_anyway_over", stored[review["id"]])
        self.assertEqual(["dispatch:" + held["id"][:12]],
                         stored[build["id"]]["start_anyway_over"])

    def test_a_hold_whose_work_reached_trunk_is_finished(self):  # noqa: VACUOUS_ASSERTION — the unlanded hold is recorded on the second row; the landed hold's absence is the contract
        """N2: landreq's landing proof says the held tip is on trunk."""
        self.held_clean(landed=True, lane="landed-lane")
        with self.later(2), self.lander("integrator"):
            row, why, _sent = self.send(lane="lane-one")
        self.assertIsNone(why, why)
        self.assertNotIn("start_anyway_over", dispatches.rows()[row["id"]])
        held = self.held_clean()
        with self.later(2), self.lander("integrator"):
            row, why, _sent = self.send(lane="lane-two")
        self.assertEqual(["dispatch:" + held["id"][:12]],
                         dispatches.rows()[row["id"]]["start_anyway_over"])

    def test_a_lane_claimed_with_reasons_carries_them_to_its_first_send(self):
        """Gap (b): the lane's --because and --start-anyway are not asked
        again at its first dispatch."""
        self.plant()
        held = self.held_clean()
        self.git("branch", "lane/lane-c")
        self.assertEqual((True, None),
                         taskkey.record_lane(self.repo, "lane-c", self.C))
        self.assertEqual((True, None), taskkey.record_lever(
            self.repo, "lane-c", [self.A, self.B], BECAUSE))
        self.assertEqual((True, None), taskkey.record_start(
            self.repo, "lane-c", ["dispatch:" + held["id"][:12]], START))
        with self.later(2), self.lander("integrator"):
            row, why, _sent = self.send(lane="lane-c", task=self.C)
        self.assertIsNone(why, why)
        stored = dispatches.rows()[row["id"]]
        self.assertEqual(BECAUSE, stored["lever_because"])
        self.assertEqual([self.A, self.B], stored["lever_skipped"])
        self.assertEqual(START, stored["start_anyway"])
        self.assertEqual("lane/lane-c", stored["reasons_from"])

    def test_a_first_row_on_a_lane_that_records_its_task_is_not_weighed(self):  # noqa: VACUOUS_ASSERTION — the row is written and carries the lane's task; absent lever and start fields are the contract
        """F1: the claim weighed the pick, so the review of the lane's work
        is not refused for it, by either check."""
        self.plant()
        self.held_clean()
        self.git("branch", "lane/lane-c")
        self.assertEqual((True, None),
                         taskkey.record_lane(self.repo, "lane-c", self.C))
        with self.later(2), self.lander("integrator"):
            row, why, _sent = dispatches.send(
                "seat-b", "lane-c", "read the gamma part", self.b,
                repo=self.repo, kind="review", sign=False, new_work=True,
                weigh=True)
        self.assertIsNone(why, why)
        stored = dispatches.rows()[row["id"]]
        self.assertEqual(self.C, stored["task"])
        for key in ("lever_skipped", "lever_because", "start_anyway",
                    "reasons_from"):
            self.assertNotIn(key, stored)

    def test_dispatch_add_new_work_is_weighed_like_send(self):  # noqa: VACUOUS_ASSERTION — the steer names the skipped ids and both rows store their reasons
        """Gap (a)."""
        self.plant()
        said = []
        row, why = dispatches.add("seat-b", "lane-c", ref=self.a,
                                  repo=self.repo, kind="build", notify=False,
                                  new_work=True, _reason=True, task=self.C,
                                  weigh=True, steer=said)
        self.assertIsNone(why, why)
        self.assertIn("LEVER INVERSION: %s goes ahead of %s (story"
                      % (self.C, self.A), said[0])
        self.assertEqual(NONE, dispatches.rows()[row["id"]]["lever_because"])
        rc, out, err = run(dispatches.cmd_dispatch, [
            "add", "seat-b", "lane-c2", "--ref", self.a, "--kind", "build",
            "--repo", self.repo, "--new-work", "--task", self.C, "--part",
            "--because", BECAUSE])
        self.assertEqual(0, rc, out + err)
        (stored,) = [r for r in dispatches.rows().values()
                     if r["lane"] == "lane-c2"]
        self.assertEqual(BECAUSE, stored["lever_because"])


class OwnerLineTest(_Story, WorkBase):
    """The owner's line in the morning report (`helm brief --report`)."""

    def order_lines(self):
        text, _rc = brief.morning_report(repo=self.root)
        self.assertIn("LANDED", text)
        return [ln for ln in text.splitlines() if ln.startswith("ORDER")]

    def test_it_names_the_lead_with_no_lane_and_what_went_first_and_why(self):
        self.plant()
        rc, out, err = self.work("claim", "lane-c", "--seat", "s1", "--task",
                                 self.C, "--part", "--because", BECAUSE)
        self.assertEqual(0, rc, out + err)
        lines = self.order_lines()
        self.assertEqual(1, len(lines), lines)
        self.assertIn("(%s): lead %s has no lane" % (self.S, self.A), lines[0])
        self.assertIn('%s went first: "%s"' % (self.C, BECAUSE), lines[0])

    def test_it_lists_owner_asked_P0_P1_with_no_lane_for_over_two_hours(self):
        waiting = self.file("an owner-asked row nobody took", priority="P0",
                            origin="owner")
        self.backdate(waiting, 3)
        fresh = self.file("an owner-asked row filed just now",
                          priority="P1", origin="owner")
        agent = self.file("an agent row nobody took", priority="P0",
                          origin="agent")
        self.backdate(agent, 3)
        lines = self.order_lines()
        self.assertEqual(1, len(lines), lines)
        self.assertIn("owner-asked", lines[0])
        self.assertIn(waiting, lines[0])
        self.assertNotIn(fresh, lines[0])
        self.assertNotIn(agent, lines[0])

    def test_nothing_inverted_or_unrouted_shows_no_line(self):  # noqa: VACUOUS_ASSERTION — the claim's rc and the LANDED heading in order_lines are the controls; an absent line is the contract
        self.plant()
        rc, out, err = self.work("claim", "lane-a", "--seat", "s1", "--task",
                                 self.A, "--part")
        self.assertEqual(0, rc, out + err)
        self.assertEqual([], self.order_lines())


if __name__ == "__main__":
    unittest.main()
