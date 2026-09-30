#!/usr/bin/env python3
"""The task ledger's self-cleaning (task/3451) — helm/taskhygiene.py.

The dominant stale class is LANDED BUT NOT CLOSED: a train carried the work
onto trunk and the task stayed open. Every arm here runs against a temporary
task ledger and, where trunk matters, a throwaway git repository whose
commits carry fake train merge subjects at controlled commit dates, so the
land link, the quiet window and the freshness contract are measured against
clocks the test sets rather than the wall clock.

Each refusing arm stands beside a passing one: a part-land beside a whole
land, a row with an open child beside one without, a fresh row beside a
stale one, a dry run beside the --apply that writes.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import stalebot, taskhygiene, taskkey, tasks  # noqa: E402
from tests.test_tasks import CliBase, TasksBase  # noqa: E402

DAY = 86400.0
T0 = 1780000000.0      # a fixed filing instant; every clock below is T0 + x


def _git(root, *args, when=None):
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1",
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    if when is not None:
        stamp = "@%d +0000" % int(when)
        env.update(GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
    got = subprocess.run(("git", "-c", "commit.gpgsign=false") + args,
                         cwd=root, env=env, capture_output=True, text=True)
    if got.returncode != 0:
        raise AssertionError("git %s failed: %s" % (args, got.stderr))
    return got.stdout.strip()


class HygieneBase(TasksBase):

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "root",
             when=T0 - DAY)
        self.dispatch_path = os.path.join(self.tmp, "dispatches.jsonl")

    def commit(self, subject, when):
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", subject,
             when=when)
        return _git(self.repo, "rev-parse", "HEAD")

    def file(self, title, owner="seat-a", at=T0, **kw):
        with mock.patch("time.time", return_value=at):
            row, err = tasks.add(title, owner, path=self.path,
                                 force_new=True, **kw)
        self.assertIsNone(err, err)
        return row

    def note(self, tid, text, at, by="seat-a"):
        with mock.patch("time.time", return_value=at):
            row, err = tasks.comment(tid, text, by=by, path=self.path)
        self.assertIsNone(err, err)
        return row

    def run_sweep(self, now, apply=False, notify=None):
        with mock.patch("time.time", return_value=now):
            return taskhygiene.sweep(
                apply=apply, notify=notify, now=now, path=self.path,
                repo=self.repo, trunk="main",
                dispatch_path=self.dispatch_path)

    def lines(self):
        with open(self.path) as fh:
            return len(fh.read().splitlines())

    def row(self, tid):
        return tasks.rows(self.path)[tid]

    def receipts(self, tid):
        return [c["text"] for c in tasks.comments_of(self.row(tid))
                if tasks.is_sweep_receipt(c)]

    def kinds(self, rep, tid=None):
        return sorted((a["kind"], a["id"]) for a in rep["actions"]
                      if tid is None or a["id"] == tid)


class ParseTrainTest(unittest.TestCase):
    """Which task a train subject closes, and when it names only a part."""

    def test_the_first_token_is_the_car_and_a_part_is_never_whole(self):
        cases = (
            ("train437: merge lane ledger-write-lock-replay-3562 at "
             "03a2c8f54e0 (task/3562, P0: auto-land held the lock)",
             "task/3562", False),
            ("train417: merge lane refusal-way-out-3529a at 490d4518080 "
             "(task/3529 part A: the refusal leads)", "task/3529", True),
            ("train412: merge lane autoland-door-exit4 at f1f686446c8 "
             "(task/3463 item 14, P0; built)", "task/3463", True),
            ("train423: merge lane toolless-seat-3533p2 at bd818f173e6 "
             "(task/3533: a seat)", "task/3533", True),
            ("train9: merge lane cut-3001 at abcdef1 (task/3001 L1a: slice)",
             "task/3001", True),
            ("train425: merge lane guard-3301-r3 at 57ac1987df2 (task/3301: "
             "the guard)", "task/3301", False),
            # "no task" says the car names none: the lane's number is not
            # borrowed for it (review 3451 finding 5)
            ("train439: merge lane successor-3571 (no task, P?, a DOOR)",
             None, False),
            ("train438: merge lane drift-probe-tmp-3578 (no-task, P?)",
             None, False),
            # a numbered change of several is a part (finding 6)
            ("train20: merge lane x at abcdef1 (task/3000 change 1 of 3: "
             "the first)", "task/3000", True),
            ("train21: merge lane y at abcdef1 (task/3000 2/3: the second)",
             "task/3000", True),
            # a LATER mention is another row, never the car
            ("train419: merge lane autoland-push-seam at b3bae6e0201 (P0, "
             "change 2 cut to task/3552; built)", None, False),
        )
        for subject, task, partial in cases:
            with self.subTest(subject=subject[:40]):
                got = taskhygiene.parse_train(subject)
                self.assertEqual((got["task"], got["partial"]), (task, partial))
        self.assertIsNone(taskhygiene.parse_train("fix: a thing (task/12)"))

    def test_a_merge_line_is_the_fact_of_its_task(self):
        """task/3643: the task a merge served is fixed at merge time. The
        line's own task is RECORDED; "no task" and "task UNKNOWN" link
        nothing; a line that says neither (a hand or historical merge) is
        INFERRED from a literal task-N in its lane's name only, never from
        today's lane records and never from a trailing number (measured on
        trunk: the number rule's one extra link was 262k -> task/262)."""
        self.assertEqual(taskhygiene.parse_train(
            "train7: merge lane task-3585-one-board at abc1234 (P?)")["task"],
            "task/3585")
        cases = (
            ("train1: merge lane x at abc1234 (task/12, P1, not a door)",
             "task/12", taskhygiene.RECORDED),
            ("train5: merge lane canary-seeded-red-0926 at abc1234 (P?, not "
             "a door)", None, None),
            ("train425: merge lane local-windows-262k at abc1234 (bonsai "
             "runs 262k windows)", None, None),
            ("train7: merge lane task-3585-one-board at abc1234 (P?, not a "
             "door)", "task/3585", taskhygiene.INFERRED),
            ("train8: merge lane task-3585-one-board", "task/3585",
             taskhygiene.INFERRED),
            ("train9: merge lane work-key-3643 (no task, P?)", None, None),
            ("train10: merge lane work-key-3643 (task UNKNOWN: contradictory"
             ", P?)", None, None),
        )
        for subject, task, basis in cases:
            with self.subTest(subject=subject[:40]):
                got = taskhygiene.parse_train(subject)
                self.assertEqual((got["task"], got["basis"]), (task, basis))
                self.assertFalse(got["partial"])


class LandedLinkTest(HygieneBase):
    """A train naming the whole task links it and, after the quiet window,
    closes it; a part-land only annotates; an open child holds it."""

    def setUp(self):
        super().setUp()
        self.whole = self.file("the whole thing", priority="P1")["id"]
        self.part = self.file("the split thing", priority="P1")["id"]
        self.parent = self.file("the parent thing")["id"]
        self.child = self.file("the child thing",
                               continues=self.parent)["id"]
        self.done = self.file("the finished thing")["id"]
        tasks.close(self.done, "already done", path=self.path)
        n = lambda tid: tid.split("/")[1]              # noqa: E731
        self.land = self.commit(
            "train1: merge lane whole-%s at 1111111aaaa (%s, the whole)"
            % (n(self.whole), self.whole), T0 + DAY)
        self.commit("train2: merge lane split-%sa at 2222222bbbb (%s part A:"
                    " half)" % (n(self.part), self.part), T0 + DAY)
        self.commit("train3: merge lane parent-%s at 3333333cccc (%s, all)"
                    % (n(self.parent), self.parent), T0 + DAY)
        self.commit("train4: merge lane done-%s at 4444444dddd (%s, all)"
                    % (n(self.done), self.done), T0 + DAY)

    def test_dry_run_plans_the_links_and_writes_nothing(self):
        before = self.lines()
        rep = self.run_sweep(T0 + 2 * DAY)
        self.assertEqual(self.kinds(rep), sorted([
            (taskhygiene.ANNOTATE, self.whole),
            (taskhygiene.ANNOTATE_PART, self.part),
            (taskhygiene.ANNOTATE, self.parent)]))
        self.assertEqual(self.lines(), before)
        self.assertEqual(rep["applied"], [])

    def test_apply_links_once_and_a_quiet_whole_land_is_marked_never_closed(self):  # noqa: VACUOUS_ASSERTION — the land's receipt and the candidate's mark are asserted present on the same row
        linked = T0 + 2 * DAY
        rep = self.run_sweep(linked, apply=True)
        self.assertEqual(len(rep["applied"]), 3, rep["failed"])
        said = self.receipts(self.whole)
        self.assertEqual(len(said), 1)
        self.assertIn("LANDED %s by train1" % self.land[:12], said[0])
        self.assertIn("LANDED-PART", self.receipts(self.part)[0])
        # IDEMPOTENT: the same trunk links nothing twice
        again = self.run_sweep(linked + 60, apply=True)
        self.assertEqual([a for a in again["actions"]
                          if a["kind"] in (taskhygiene.ANNOTATE,
                                           taskhygiene.ANNOTATE_PART)], [])
        cands = {c["id"]: c for c in again["candidates"]}
        self.assertEqual(sorted(cands), sorted([self.whole, self.parent]))
        self.assertIn("open children", cands[self.parent]["blockers"][0])
        # A LAND CLOSES NOTHING (task/3643, task/3653): past the 72 h quiet
        # window the row is marked "landed, whole ask not yet re-read", and
        # its owner closes it after re-reading the whole ask
        self.assertEqual(cands[self.whole]["state"], taskkey.LANDED_UNREAD)
        self.assertNotIn("auto-close", said[0])
        late = self.run_sweep(linked + 73 * 3600, apply=True)
        self.assertEqual(self.kinds(late, self.whole), [])
        self.assertEqual(self.row(self.whole)["status"], "open")
        self.assertEqual(len(self.receipts(self.whole)), 1)
        cand = [c for c in late["candidates"] if c["id"] == self.whole][0]
        self.assertEqual((cand["blockers"], cand["state"]),
                         ([], taskkey.LANDED_UNREAD))
        self.assertEqual(late["counts"]["landed_unread"], 2)
        self.assertIn(taskkey.LANDED_UNREAD, "\n".join(
            taskhygiene.render(late)))
        for tid in (self.part, self.parent, self.child):
            self.assertEqual(self.row(tid)["status"], "open", tid)

    def test_todays_lane_record_never_rejoins_an_old_merge(self):  # noqa: VACUOUS_ASSERTION — the recorded land beside it is asserted linked
        """FINDING 3: lane `shared` now records task B; a bare merge of an
        earlier `shared` lane is not B's land, and a name that infers A while
        the record says B links neither."""
        from helm import taskkey as tk
        b = self.file("the new owner of the lane name")["id"]
        a = self.file("the old task")["id"]
        for lane in ("shared", "task-%s-shared" % a.split("/")[1]):
            _git(self.repo, "branch", "lane/" + lane)
            self.assertEqual(tk.record_lane(self.repo, lane, b), (True, None))
        self.commit("train7: merge lane shared at 7777777aaaa (hand-merged)",
                    T0 + DAY)
        self.commit("train8: merge lane task-%s-shared at 8888888bbbb"
                    % a.split("/")[1], T0 + DAY)
        rep = self.run_sweep(T0 + 2 * DAY, apply=True)
        self.assertEqual(self.receipts(b), [])
        self.assertEqual(self.receipts(a), [])
        self.assertIn(self.whole, [x["id"] for x in rep["applied"]])

    def test_an_unreadable_lane_record_mints_no_guessed_receipt(self):  # noqa: VACUOUS_ASSERTION — the recorded land beside it is asserted linked while the config is unreadable
        """The extra finding: with the lane records unreadable, a line the
        records could decide (a name-inferred task-901 whose lane records
        task/902) is neither annotated nor closed, and no receipt is written;
        a land the line itself records still links."""
        from helm import taskkey as tk
        one = self.file("named by the lane", tid="901")["id"]
        two = self.file("recorded for the lane", tid="902")["id"]
        _git(self.repo, "branch", "lane/task-901-x")
        self.assertEqual(tk.record_lane(self.repo, "task-901-x", two),
                         (True, None))
        self.commit("train9: merge lane task-901-x at 9999999cccc (P?, not a "
                    "door)", T0 + DAY)
        with mock.patch.object(tk, "lane_records",
                               return_value=({}, "the config is unreadable")):
            rep = self.run_sweep(T0 + 5 * DAY, apply=True)
        self.assertEqual((self.receipts(one), self.receipts(two)), ([], []))
        for tid in (one, two):
            self.assertEqual(self.kinds(rep, tid), [])
            self.assertEqual(self.row(tid)["status"], "open")
        self.assertIn(self.whole, [x["id"] for x in rep["applied"]])
        self.assertTrue(any("unreadable" in u for u in rep["unavailable"]))

    def test_motion_after_the_land_holds_the_close(self):
        self.run_sweep(T0 + 2 * DAY, apply=True)
        self.note(self.whole, "the land missed the docs half", T0 + 2.5 * DAY)
        rep = self.run_sweep(T0 + 10 * DAY)
        self.assertEqual(self.kinds(rep, self.whole), [])
        cand = [c for c in rep["candidates"] if c["id"] == self.whole][0]
        self.assertIn("motion after the land (comment", cand["blockers"][0])

    def test_a_row_filed_after_its_land_is_not_held_by_its_filing(self):
        late = self.file("recorded after it landed", at=T0 + 3 * DAY)["id"]
        self.commit("train5: merge lane late-%s at 5555555eeee (%s, all)"
                    % (late.split("/")[1], late), T0 + 2 * DAY)
        self.run_sweep(T0 + 4 * DAY, apply=True)
        rep = self.run_sweep(T0 + 8 * DAY, apply=True)
        cand = [c for c in rep["candidates"] if c["id"] == late][0]
        self.assertEqual((cand["blockers"], cand["state"]),
                         ([], taskkey.LANDED_UNREAD))
        self.assertEqual(self.kinds(rep, late), [])
        self.assertEqual(self.row(late)["status"], "open")

    def test_the_quiet_window_is_configurable(self):
        """It times the one confirm DM a P0 or owner-asked row gets; no
        row is closed when it passes."""
        p0 = self.file("burning landed", priority="P0")["id"]
        self.commit("train6: merge lane b at 6666666ffff (%s, all)" % p0,
                    T0 + DAY)
        self.run_sweep(T0 + 2 * DAY, apply=True)
        early = self.run_sweep(T0 + 2 * DAY + 2 * 3600)
        self.assertNotIn((taskhygiene.CONFIRM_ASK, p0), self.kinds(early))
        with mock.patch.dict(os.environ, {"HELM_TASK_CLOSE_QUIET_H": "1"}):
            rep = self.run_sweep(T0 + 2 * DAY + 2 * 3600)
        self.assertIn((taskhygiene.CONFIRM_ASK, p0), self.kinds(rep))
        self.assertEqual(self.kinds(rep, self.whole), [])

    def test_confirm_close_is_the_holders_word_on_a_candidate_only(self):
        self.run_sweep(T0 + 2 * DAY, apply=True)
        none, why = taskhygiene.confirm_close(self.part, "seat-a",
                                              path=self.path)
        self.assertIsNone(none)
        self.assertIn("not a close candidate", why)
        none, why = taskhygiene.confirm_close(self.whole, "seat-b",
                                              path=self.path)
        self.assertIsNone(none)
        self.assertIn("held by seat-a", why)
        row, err = taskhygiene.confirm_close(self.whole, "seat-a",
                                             path=self.path)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "closed")
        self.assertIn("confirmed by @seat-a", row["closed_reason"])


class FreshnessTest(HygieneBase):
    """P0 48 h, P1 7 d, P2 30 d: first breach pings once, second unassigns."""

    def setUp(self):
        super().setUp()
        self.p0 = self.file("burning thing", priority="P0")["id"]
        self.p1 = self.file("important thing", priority="P1")["id"]
        self.p2 = self.file("normal thing", priority="P2")["id"]
        self.p3 = self.file("someday thing", priority="P3")["id"]
        self.pings = []

    def notify(self, owner, text, key):
        self.pings.append((owner, key))
        return True

    def test_first_breach_pings_once_second_unassigns_into_triage(self):
        now = T0 + 8 * DAY
        dry = self.run_sweep(now)
        self.assertEqual(self.kinds(dry), sorted([
            (taskhygiene.PING, self.p0), (taskhygiene.PING, self.p1)]))
        self.assertEqual(self.pings, [])
        self.run_sweep(now, apply=True, notify=self.notify)
        self.assertEqual(sorted(p[0] for p in self.pings), ["seat-a"] * 2)
        self.assertIn("STALE-1 P0", self.receipts(self.p0)[0])
        # ONCE PER BREACH: the same breach never pings again
        again = self.run_sweep(now + 3600, apply=True, notify=self.notify)
        self.assertEqual(len(self.pings), 2)
        self.assertEqual(again["actions"], [])
        # the P0 window elapses again after its warning; the P1 one has not
        second = T0 + 8 * DAY + 3 * DAY
        rep = self.run_sweep(second, apply=True, notify=self.notify)
        self.assertEqual(self.kinds(rep), [(taskhygiene.UNASSIGN, self.p0)])
        row = self.row(self.p0)
        self.assertEqual((row["owner"], row["status"]), (None, "open"))
        self.assertEqual(row["released"]["by"], "stale-bot")
        self.assertEqual(row["released"]["owner_was"], "seat-a")
        self.assertIn("STALE-2 P0", self.receipts(self.p0)[-1])
        counts = taskhygiene.freshness_counts(
            now=second, path=self.path, repo=self.repo, trunk="main",
            dispatch_path=self.dispatch_path)
        self.assertEqual(counts["triage"], 1)
        self.assertEqual(counts["by_priority"]["P0"]["triage"], 1)
        self.assertEqual(counts["by_priority"]["P1"]["warned"], 1)
        self.assertEqual(counts["by_priority"]["P2"]["stale"], 0)
        self.assertEqual(counts["no_contract"], 1)

    def test_a_commit_or_dispatch_row_naming_the_task_is_motion(self):
        self.commit("fix: the important thing (%s)" % self.p1, T0 + 7 * DAY)
        with open(self.dispatch_path, "w") as fh:
            fh.write(json.dumps({"id": "d1", "ts": T0 + 7 * DAY,
                                 "lane": "burning-%s" % self.p0.split("/")[1],
                                 "kind": "build"}) + "\n")
        rep = self.run_sweep(T0 + 8 * DAY)
        self.assertEqual(self.kinds(rep), [(taskhygiene.PING, self.p0)])

    def test_an_undelivered_ping_leaves_no_receipt_and_retries(self):
        rep = self.run_sweep(T0 + 8 * DAY, apply=True,
                             notify=lambda *a: False)
        self.assertEqual(len(rep["failed"]), 2)
        self.assertEqual(self.receipts(self.p0), [])
        retry = self.run_sweep(T0 + 8 * DAY + 60)
        self.assertIn((taskhygiene.PING, self.p0), self.kinds(retry))

    def test_pings_are_capped_per_sweep(self):
        with mock.patch.dict(os.environ, {"HELM_TASK_PING_CAP": "1"}):
            rep = self.run_sweep(T0 + 8 * DAY, apply=True,
                                 notify=self.notify)
        self.assertEqual((len(self.pings), len(rep["deferred"])), (1, 1))

    def test_a_receipt_is_not_a_note_for_the_backlog_clock(self):
        self.run_sweep(T0 + 8 * DAY, apply=True, notify=self.notify)
        row = self.row(self.p1)
        self.assertEqual(len(self.receipts(self.p1)), 1)
        self.assertEqual(tasks.noted_epoch(row), T0)
        self.note(self.p1, "still on it", T0 + 9 * DAY)
        self.assertEqual(tasks.noted_epoch(self.row(self.p1)), T0 + 9 * DAY)


class DuplicateProposalTest(HygieneBase):

    def test_near_titles_are_proposed_and_never_merged(self):
        old = self.file("gate refuses a whole suite in a lane room")["id"]
        new = self.file("gate refuses a whole suite in lane room",
                        at=T0 + DAY)["id"]
        self.file("an unrelated row about chat")
        a = self.file("rank sweep for backlog", refs=["docs/x.md"])["id"]
        b = self.file("rank sweep for the owner backlog board",
                      refs=["docs/x.md"], at=T0 + DAY)["id"]
        before = self.lines()
        rep = self.run_sweep(T0 + 2 * DAY, apply=True, notify=lambda *a: True)
        pairs = sorted((d["id"], d["of"]) for d in rep["duplicates"])
        self.assertEqual(pairs, sorted([(new, old), (b, a)]))
        self.assertEqual(self.lines(), before)
        for tid in (old, new, a, b):
            self.assertEqual(self.row(tid)["status"], "open")


class SweepCliTest(CliBase):
    """`helm stale sweep` carries the task section; `helm task
    close-candidates` and `confirm-close` are its doors."""

    def stale(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = stalebot.cmd_stale(list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_apply_is_opt_in_and_contradicting_flags_refuse(self):
        rep = {"items": [], "digests": {}, "swept": 0, "posted": [],
               "latched": 0, "oldest_unproposed_s": 0, "no_deadline": 0,
               "unavailable": [], "failed": []}
        hyg = {"actions": [], "applied": [], "failed": [], "deferred": [],
               "duplicates": [], "candidates": [], "unavailable": [],
               "counts": {}, "apply": False}
        with mock.patch.object(stalebot, "sweep", return_value=rep), \
                mock.patch.object(taskhygiene, "sweep",
                                  return_value=hyg) as th:
            rc, out, _err = self.stale("sweep")
            self.assertEqual(rc, 0)
            self.assertEqual(th.call_args[1], {"apply": False})
            self.assertIn("dry run", out)
            rc, _out, _err = self.stale("sweep", "--apply")
            self.assertEqual(th.call_args[1], {"apply": True})
            rc, _out, err = self.stale("sweep", "--apply", "--dry-run")
        self.assertEqual(rc, 2)
        self.assertIn("contradict", err)
        self.assertEqual(th.call_count, 2)

    def test_close_candidates_lists_and_confirm_close_closes(self):
        row, err = tasks.add("landed row", "seat-a", path=tasks.ledger_path(),
                             force_new=True)
        self.assertIsNone(err, err)
        tid = row["id"]
        commits = [{"sha": "a" * 40, "ct": time.time() - 60,
                    "subject": "", "named": {tid},
                    "train": {"train": "train7", "lane": "l-1", "tip": None,
                              "task": tid, "partial": False}}]
        with mock.patch.object(taskhygiene, "trunk_commits",
                               return_value=(commits, None)), \
                mock.patch.object(taskhygiene, "dispatch_motion",
                                  return_value=({}, None)), \
                mock.patch.object(taskhygiene, "lane_commits",
                                  return_value=({}, None)):
            taskhygiene.sweep(apply=True)
            rc, out, _err = self.cli("close-candidates", "--json")
            self.assertEqual(rc, 0)
            got = json.loads(out)["candidates"]
            self.assertEqual([c["id"] for c in got], [tid])
            rc, _out, err = self.cli("confirm-close", tid, "junk")
            self.assertEqual(rc, 2)
            rc, out, err = self.cli("confirm-close", tid)
        self.assertEqual(rc, 0, err)
        self.assertIn("confirmed by @seat-a", out)
        self.assertEqual(tasks.get(tid)["status"], "closed")


class ReceiptMarkTest(unittest.TestCase):
    """Finding 1: the receipt prefix is not command-shaped text."""

    def test_the_receipt_mark_never_reads_as_a_promised_verb(self):
        from tests import test_instructions_are_runnable as audit
        for text in (tasks.SWEEP_RECEIPT_MARK,
                     tasks.SWEEP_RECEIPT_MARK + " LANDED abc by train1 at x]"):
            self.assertIsNone(audit._COMMAND_LABEL.match(text), text)
        self.assertTrue(tasks.is_sweep_receipt(
            {"text": tasks.SWEEP_RECEIPT_MARK + " STALE-1 P0 x]"}))


class BlindSweepTest(HygieneBase):
    """Finding 2: a motion source that cannot be read can hide the motion a
    ping, an unassign or a close would be wrong about, so none is planned
    and the rows it cannot judge count UNKNOWN, never stale."""

    def setUp(self):
        super().setUp()
        self.p0 = self.file("burning thing", priority="P0")["id"]
        self.p1 = self.file("important thing", priority="P1")["id"]
        self.pings = []

    def notify(self, owner, text, key):
        self.pings.append(owner)
        return True

    def sweep_with(self, now, **kw):
        with mock.patch("time.time", return_value=now):
            args = dict(apply=True, notify=self.notify, now=now,
                        path=self.path, repo=self.repo, trunk="main",
                        dispatch_path=self.dispatch_path)
            args.update(kw)
            return taskhygiene.sweep(**args)

    def assert_blind(self, rep, counts):
        self.assertTrue(rep["unavailable"])
        self.assertEqual([a for a in rep["actions"] if a["kind"] in (
            taskhygiene.PING, taskhygiene.UNASSIGN, taskhygiene.CONFIRM_ASK)],
            [])
        self.assertEqual(self.pings, [])
        self.assertEqual(counts["stale"], 0)
        self.assertEqual(counts["unknown"], 2)
        self.assertEqual(counts["by_priority"]["P0"]["unknown"], 1)

    def test_an_unreadable_trunk_log_pings_and_unassigns_nobody(self):
        now = T0 + 8 * DAY
        rep = self.sweep_with(now, trunk="no-such-ref")
        counts = taskhygiene.freshness_counts(
            now=now, path=self.path, repo=self.repo, trunk="no-such-ref",
            dispatch_path=self.dispatch_path)
        self.assert_blind(rep, counts)
        self.assertEqual(self.row(self.p0)["owner"], "seat-a")

    def test_an_unreadable_dispatch_ledger_pings_and_unassigns_nobody(self):
        os.makedirs(self.dispatch_path)
        now = T0 + 8 * DAY
        rep = self.sweep_with(now)
        counts = taskhygiene.freshness_counts(
            now=now, path=self.path, repo=self.repo, trunk="main",
            dispatch_path=self.dispatch_path)
        self.assert_blind(rep, counts)

    def test_an_unreadable_claims_table_pings_nobody(self):
        from helm import seats_common
        os.makedirs(os.path.dirname(seats_common.claims_path()),
                    exist_ok=True)
        with open(seats_common.claims_path(), "w") as fh:
            fh.write("{not json")
        now = T0 + 8 * DAY
        rep = self.sweep_with(now)
        counts = taskhygiene.freshness_counts(
            now=now, path=self.path, repo=self.repo, trunk="main",
            dispatch_path=self.dispatch_path)
        self.assert_blind(rep, counts)

    def test_a_blind_sweep_never_auto_closes_a_candidate(self):
        self.commit("train1: merge lane a at 1111111aaaa (%s, all)" % self.p1,
                    T0 + DAY)
        self.sweep_with(T0 + DAY + 60)
        self.assertEqual(len(self.receipts(self.p1)), 1)
        os.makedirs(self.dispatch_path)
        rep = self.sweep_with(T0 + 6 * DAY)
        self.assertEqual(self.kinds(rep, self.p1), [])
        self.assertEqual(self.row(self.p1)["status"], "open")
        cand = [c for c in rep["candidates"] if c["id"] == self.p1][0]
        self.assertIn("UNKNOWN", " ".join(cand["blockers"]))


class MotionHistoryTest(HygieneBase):
    """Findings 3 and 4: motion is read from what actually happened on the
    row and in the lanes, not from the row's newest last_updated."""

    def setUp(self):
        super().setUp()
        self.tid = self.file("the thing", priority="P1")["id"]
        self.p0 = self.file("the burning thing", priority="P0",
                            tid="4242")["id"]
        self.pings = []

    def notify(self, owner, text, key):
        self.pings.append(owner)
        return True

    def room(self, lane, repo=None):
        """Cut lane/<lane>'s room where `helm work claim` would, so a lease
        on it binds to its repository (`dispatches._lane_claim_binds`)."""
        from helm.work import _lanes
        repo = repo or self.repo
        path = _lanes.lane_path(repo, lane)
        have = subprocess.run(("git", "-C", repo, "show-ref", "--verify",
                               "--quiet", "refs/heads/lane/" + lane)
                              ).returncode == 0
        _git(repo, "worktree", "add", "-q", path,
             *(("lane/" + lane,) if have else ("-b", "lane/" + lane)))
        return path

    def repo_id(self, repo=None):
        from helm import dispatches
        return dispatches._repo_info(repo or self.repo)["repo_id"]

    def test_a_note_between_the_land_and_the_link_holds_the_close(self):
        self.commit("train1: merge lane a at 1111111aaaa (%s, all)" % self.tid,
                    T0 + DAY)
        with mock.patch("time.time", return_value=T0 + DAY + 3600):
            _row, err = tasks.update(self.tid, path=self.path,
                                     note="only half landed; more to do")
        self.assertIsNone(err, err)
        self.run_sweep(T0 + 2 * DAY, apply=True, notify=self.notify)
        self.assertEqual(len(self.receipts(self.tid)), 1)
        rep = self.run_sweep(T0 + 6 * DAY, apply=True, notify=self.notify)
        self.assertEqual(self.kinds(rep, self.tid), [])
        self.assertEqual(self.row(self.tid)["status"], "open")
        cand = [c for c in rep["candidates"] if c["id"] == self.tid][0]
        self.assertIn("motion after the land", cand["blockers"][0])

    def test_a_lane_branch_commit_naming_the_task_is_motion(self):
        _git(self.repo, "checkout", "-q", "-b", "lane-x")
        self.commit("wip on %s" % self.p0, T0 + 2.5 * DAY)
        _git(self.repo, "checkout", "-q", "main")
        rep = self.run_sweep(T0 + 3 * DAY)
        self.assertNotIn((taskhygiene.PING, self.p0), self.kinds(rep))
        # the control: without that commit the P0 is past its 48 h
        _git(self.repo, "branch", "-q", "-D", "lane-x")
        rep = self.run_sweep(T0 + 3 * DAY)
        self.assertIn((taskhygiene.PING, self.p0), self.kinds(rep))

    def test_a_live_work_lease_on_the_tasks_lane_is_motion(self):  # noqa: VACUOUS_ASSERTION — the lease motion is asserted present for both lanes and the P1's ping is asserted
        """A lease is motion for the task its lane serves by the one join
        (task/3643): the lane's record, or a literal task-N in its name."""
        from helm import seats_claims, taskkey
        from helm.work import _lanes
        _git(self.repo, "branch", "lane/burning-fix")
        written, why = taskkey.record_lane(self.repo, "burning-fix",
                                           self.p0)
        self.assertEqual((written, why), (True, None))
        for lane in ("burning-fix", "task-4242-burning"):
            self.room(lane)
            res = _lanes.resource(self.repo, lane)
            ok, msg, lease = seats_claims.claim(res, "seat-a",
                                                repo=self.repo_id())
            self.assertTrue(ok, msg)
            self.assertIn(self.p0, taskhygiene.lease_motion(
                T0, lanes=taskkey.lane_records(self.repo)[0],
                project=_lanes.project_token(self.repo),
                repo_id=self.repo_id())[0])
            self.assertTrue(seats_claims.release(res, "seat-a",
                                                 lease=lease)[0])
        ok, msg, _lease = seats_claims.claim(
            _lanes.resource(self.repo, "burning-fix"), "seat-a",
            repo=self.repo_id())
        self.assertTrue(ok, msg)
        rep = self.run_sweep(T0 + 8 * DAY, apply=True, notify=self.notify)
        self.assertNotIn(self.p0, [a["id"] for a in rep["actions"]])
        self.assertEqual(self.row(self.p0)["owner"], "seat-a")
        self.assertEqual(self.pings, ["seat-a"])     # the P1 alone

    def test_a_lease_in_another_project_is_not_this_projects_motion(self):  # noqa: VACUOUS_ASSERTION — this project's lease of the same name is asserted as motion
        """FINDING 2: a lease is `worktree:<project>:<lane>`; the lane's
        record belongs to THIS project's repository, so another project's
        lease of the same lane name is not motion for it."""
        from helm import seats_claims, taskkey
        from helm.work import _lanes
        _git(self.repo, "branch", "lane/burning-fix")
        self.assertEqual(taskkey.record_lane(self.repo, "burning-fix",
                                             self.p0), (True, None))
        lanes = taskkey.lane_records(self.repo)[0]
        mine = _lanes.project_token(self.repo)
        self.room("burning-fix")
        ok, msg, lease = seats_claims.claim(
            "worktree:another-project:burning-fix", "seat-a")
        self.assertTrue(ok, msg)
        self.assertNotIn(self.p0, taskhygiene.lease_motion(
            T0, lanes=lanes, project=mine, repo_id=self.repo_id())[0])
        ok, msg, _lease = seats_claims.claim(
            _lanes.resource(self.repo, "burning-fix"), "seat-a",
            repo=self.repo_id())
        self.assertTrue(ok, msg)
        self.assertIn(self.p0, taskhygiene.lease_motion(
            T0, lanes=lanes, project=mine, repo_id=self.repo_id())[0])

    def test_another_projects_lease_is_never_this_projects_motion(self):  # noqa: VACUOUS_ASSERTION — the same literal lane leased in this project is asserted as motion
        """R1: the project gate covers the literal path too: another
        project's lease on `task-4242-fix` is not this project's task/4242
        motion, so it cannot suppress a stale action here."""
        from helm import seats_claims
        from helm.work import _lanes
        mine = _lanes.project_token(self.repo)
        self.room("task-4242-fix")
        ok, msg, _lease = seats_claims.claim(
            "worktree:another-project:task-4242-fix", "seat-a")
        self.assertTrue(ok, msg)
        self.assertNotIn(self.p0, taskhygiene.lease_motion(
            T0, lanes={}, project=mine, repo_id=self.repo_id())[0])
        ok, msg, _lease = seats_claims.claim(
            _lanes.resource(self.repo, "task-4242-fix"), "seat-a",
            repo=self.repo_id())
        self.assertTrue(ok, msg)
        self.assertIn(self.p0, taskhygiene.lease_motion(
            T0, lanes={}, project=mine, repo_id=self.repo_id())[0])

    def test_a_same_named_repositorys_lease_is_not_this_repositorys_motion(self):  # noqa: VACUOUS_ASSERTION — the same lease with this repository's room is asserted as motion
        """R1-collision: the lease key is the root's BASENAME, so two
        repositories named `helm` share `worktree:helm:<lane>`. A lease
        counts for the sweep's repository only when its lane room binds to
        that repository (`dispatches._lane_claim_binds`); the claims key is
        unchanged."""
        from helm import seats_claims
        a, b = (os.path.join(os.path.realpath(self.tmp), side, "helm")
                for side in ("a", "b"))
        for repo in (a, b):
            os.makedirs(repo)
            _git(repo, "init", "-q", "-b", "main")
            _git(repo, "commit", "-q", "--allow-empty", "-m", "root",
                 when=T0 - DAY)
        ok, msg, _lease = seats_claims.claim("worktree:helm:task-4242-fix",
                                             "seat-a", repo=self.repo_id(a))
        self.assertTrue(ok, msg)
        self.room("task-4242-fix", repo=b)
        self.assertNotIn(self.p0, taskhygiene.lease_motion(
            T0, lanes={}, project="helm", repo_id=self.repo_id(a))[0])
        self.room("task-4242-fix", repo=a)
        self.assertIn(self.p0, taskhygiene.lease_motion(
            T0, lanes={}, project="helm", repo_id=self.repo_id(a))[0])

    def test_a_room_left_behind_is_not_a_grant(self):  # noqa: VACUOUS_ASSERTION — the grant recorded for A with A's room is asserted as motion
        """Round 5: a room outlives the grant that cut it (an unlanded
        release keeps it), so the room alone is half the evidence. A lease
        counts for A only when the live GRANT records A's repository
        (`dispatches._grant_binds_repo`) AND A's room binds to A
        (`_lane_claim_binds`), read from the claims `progress_state` reads
        (`dispatches.live_claims`)."""
        from helm import seats_claims
        a, b = (os.path.join(os.path.realpath(self.tmp), side, "helm")
                for side in ("a", "b"))
        for repo in (a, b):
            os.makedirs(repo)
            _git(repo, "init", "-q", "-b", "main")
            _git(repo, "commit", "-q", "--allow-empty", "-m", "root",
                 when=T0 - DAY)
        res = "worktree:helm:task-4242-fix"
        self.room("task-4242-fix", repo=a)          # A's room, left behind
        self.room("task-4242-fix", repo=b)
        for repo, counts in ((b, False), (None, False), (a, True)):
            with self.subTest(grant=repo):
                ok, msg, lease = seats_claims.claim(
                    res, "seat-a", repo=self.repo_id(repo) if repo else None)
                self.assertTrue(ok, msg)
                try:
                    got = self.p0 in taskhygiene.lease_motion(
                        T0, lanes={}, project="helm",
                        repo_id=self.repo_id(a))[0]
                finally:
                    self.assertTrue(seats_claims.release(
                        res, "seat-a", lease=lease)[0])
                self.assertEqual(got, counts)

    def test_a_lease_whose_lane_only_ends_in_the_number_is_not_motion(self):  # noqa: VACUOUS_ASSERTION — the P0's action is asserted present, the positive control on the absent motion
        """The suffix guess is gone (task/3643): `burning-fix-4242` names no
        task, so its lease is not motion for task/4242, and the P0 is judged
        on its other motion."""
        from helm import seats_claims
        from helm.work import _lanes
        ok, msg, _lease = seats_claims.claim(
            _lanes.resource(self.repo, "burning-fix-4242"), "seat-a")
        self.assertTrue(ok, msg)
        self.assertNotIn(self.p0, taskhygiene.lease_motion(T0)[0])
        rep = self.run_sweep(T0 + 8 * DAY, notify=self.notify)
        self.assertIn(self.p0, [a["id"] for a in rep["actions"]])


class LandSafetyTest(HygieneBase):
    """Findings 7 and 8: a reverted land never links or marks, a P0 or an
    owner-asked row still waits for its holder's word, and a stood-down row
    is skipped. Since task/3643 no land closes any row."""

    def setUp(self):
        super().setUp()
        self.pings = []

    def notify(self, owner, text, key):
        self.pings.append((owner, key))
        return True

    def land(self, tid, n, when):
        return self.commit("train%d: merge lane l at 1111111aaaa (%s, all)"
                           % (n, tid), when)

    def test_a_reverted_train_is_never_linked(self):
        tid = self.file("reverted thing")["id"]
        sha = self.land(tid, 1, T0 + DAY)
        self.commit("back out train1\n\nThis reverts commit %s." % sha,
                    T0 + 1.5 * DAY)
        rep = self.run_sweep(T0 + 2 * DAY, apply=True, notify=self.notify)
        self.assertEqual(self.kinds(rep, tid), [])
        self.assertEqual(self.receipts(tid), [])

    def test_a_linked_land_reverted_later_never_auto_closes(self):
        tid = self.file("reverted later")["id"]
        sha = self.land(tid, 1, T0 + DAY)
        self.run_sweep(T0 + 2 * DAY, apply=True, notify=self.notify)
        self.assertEqual(len(self.receipts(tid)), 1)
        self.commit("back out train1\n\nThis reverts commit %s." % sha,
                    T0 + 2.5 * DAY)
        rep = self.run_sweep(T0 + 7 * DAY, apply=True, notify=self.notify)
        self.assertEqual(self.kinds(rep, tid), [])
        self.assertEqual(self.row(tid)["status"], "open")
        cand = [c for c in rep["candidates"] if c["id"] == tid][0]
        self.assertIn("reverted", " ".join(cand["blockers"]))

    def test_p0_and_owner_asked_rows_wait_for_a_word_and_dm_once(self):
        p0 = self.file("burning landed", priority="P0")["id"]
        asked = self.file("the owner asked", origin="owner")["id"]
        plain = self.file("plain landed")["id"]
        for i, tid in enumerate((p0, asked, plain)):
            self.land(tid, i + 1, T0 + DAY)
        self.run_sweep(T0 + DAY + 60, apply=True, notify=self.notify)
        rep = self.run_sweep(T0 + 5 * DAY, apply=True, notify=self.notify)
        # the plain row is marked, never closed, and nobody is DMed for it
        self.assertEqual(self.row(plain)["status"], "open")
        self.assertEqual(self.kinds(rep, plain), [])
        for tid in (p0, asked):
            self.assertEqual(self.row(tid)["status"], "open", tid)
            self.assertIn((taskhygiene.CONFIRM_ASK, tid), self.kinds(rep))
        self.assertEqual([o for o, _k in self.pings], ["seat-a"] * 2)
        self.assertEqual(len({k for _o, k in self.pings}), 2)
        again = self.run_sweep(T0 + 6 * DAY, apply=True, notify=self.notify)
        self.assertEqual(len(self.pings), 2)
        self.assertEqual(self.kinds(again), [])
        cands = {c["id"]: c for c in again["candidates"]}
        self.assertEqual(sorted(cands), sorted([p0, asked, plain]))
        self.assertEqual({c["state"] for c in cands.values()},
                         {taskkey.LANDED_UNREAD})
        row, err = taskhygiene.confirm_close(asked, "seat-a", path=self.path)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "closed")

    def test_a_stood_down_row_is_skipped_entirely(self):
        tid = self.file("parked thing", priority="P0")["id"]
        with mock.patch("time.time", return_value=T0 + 60):
            _row, err = tasks.update(tid, path=self.path, standdown={
                "reason": "owner work order", "ts": T0 + 60, "by": "seat-a"})
        self.assertIsNone(err, err)
        self.land(tid, 1, T0 + DAY)
        rep = self.run_sweep(T0 + 6 * DAY, apply=True, notify=self.notify)
        self.assertEqual(self.kinds(rep, tid), [])
        self.assertEqual(self.receipts(tid), [])
        self.assertEqual(rep["candidates"], [])


if __name__ == "__main__":
    unittest.main()
