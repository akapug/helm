#!/usr/bin/env python3
"""A landed task closes or narrows at land time (task/3746; helm/landtask.py).

THE CONTRACT THESE ARMS PIN, one class per decision:
  D1 the task a land serves: the lane's recorded task, else the chain's (the
     one join `review_door.pair_key` reads), else a trailing -<N> in the lane
     name when task/N is OPEN. Two stored records that disagree name no task.
  D2 `--whole` on a chain's first dispatch (and on `helm work claim`, armed
     in tests/test_taskkey.py) records that the lane carries the whole ask.
     At the land a whole lane's task reads LANDED and owes a seen-working
     check (helm/observed.py, tests/test_observed.py), unless open
     sub-tasks stay: then it says which. Any other land comments the one question on the
     task and asks it in the task's room, addressed to the lane's author.
  D3 the same land step runs on a hand land: `helm lr close --reason
     landed`, `helm lr foldcheck --apply` and `helm lr land`, and it closes
     the findings its chain filed. The verbs auto-land runs leave the step to
     the LAND step, which knows the LAND number.
  D4 the chain's other open rows are offered to their own close doors; a
     row with the same lane and no chain link is flagged, never closed.
The auto-land LAND step is armed in tests/test_autoland.py, and
close-candidates and confirm-close in tests/test_taskhygiene.py.

Every write goes to a scratch HELM_HOME, chat dir and repository
(ChainBase); no arm reads the real ledgers.
"""
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import chat, dispatches, landreq, landtask, review_door  # noqa: E402
from helm import taskkey, tasks  # noqa: E402
from tests.test_dispatch_chain import ChainBase, run  # noqa: E402

SHA = "5e" * 20
PROJECT = "placeholder-project"


def setUpModule():
    """This module writes dispatch rows, so it plants the live-seat stand-in
    (tests._tmphome.pin_live_seats), as its row-writing siblings do."""
    from tests._tmphome import pin_live_seats
    pin_live_seats()


def quiet(fn, *args, **kw):
    """fn(*args, **kw) with its output captured -> (rc, out, err)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = fn(*args, **kw)
    return rc, out.getvalue(), err.getvalue()


class LandBase(ChainBase):
    """A scratch dispatch ledger, task ledger and chat dir, with one open
    task that the lanes below serve."""

    def setUp(self):
        super().setUp()
        self.asked = []
        # A TASK NUMBER A LANE NAME CAN CARRY: the trailing-number rule
        # reads three to six digits, and a scratch ledger starts at task/1.
        self.tid = self.file("the whole ask", tid="3746")

    def root(self, lane="lane-a", kind="review", **kw):
        """One row that roots its own chain, of `kind`."""
        kw.setdefault("ref", self.a)
        if kind == "review":
            kw.setdefault("task", self.tid)
        row, why = dispatches.add("seat-b", lane, repo=self.repo, kind=kind,
                                  notify=False, new_work=True, _reason=True,
                                  **kw)
        self.assertIsNone(why, why)
        return row

    def file(self, title, **kw):
        row, why = tasks.add(title, "integrator", project=PROJECT,
                             force_new=True, **kw)
        self.assertIsNone(why, why)
        return row["id"]

    @staticmethod
    def num(tid):
        return tid.split("/")[1]

    def snap(self):
        current, bad = dispatches.snapshot()
        self.assertIsNone(bad, bad)
        return current

    def task(self, tid=None):
        return tasks.get(tid or self.tid)

    def said(self, tid=None):
        return [c["text"] for c in tasks.comments_of(self.task(tid))]

    def room_rows(self, room):
        rows, _total = chat.read(room)
        return [r for r in rows if r.get("text")]

    def room_of(self, row, tid=None):
        return review_door.task_room(review_door.pair_scope(row),
                                     tid or self.tid)

    def hand(self, row, close_row=None, **kw):
        kw.setdefault("live", None)
        return landtask.hand(row["id"], "close", SHA,
                             close_row=close_row or self.refuse_all, **kw)

    def refuse_all(self, rid, reason, evidence, live=None, restart=None):
        self.asked.append((rid, reason))
        return "the %s door refused in this arm" % reason


class TheTaskALandServesTest(LandBase):
    """D1 — the lane's record, else the chain's task, else an open task/N
    named by the lane's trailing number."""

    def test_the_lane_record_names_the_task(self):
        row = self.root(lane="plain-lane", kind="build")
        self.git("branch", "lane/plain-lane")
        self.assertEqual(taskkey.record_lane(self.repo, "plain-lane",
                                             self.tid), (True, None))
        self.assertEqual(landtask.resolve(row, self.snap()),
                         (self.tid, None))

    def test_the_chain_names_the_task_for_every_round(self):  # noqa: VACUOUS_ASSERTION — both rounds are asserted to resolve to the filed id
        first = self.root(lane="plain-lane", task=self.tid)
        later, why = self.child(first["id"], lane="renamed-lane")
        self.assertIsNone(why, why)
        current = self.snap()
        for row in (first, later):
            with self.subTest(row=row["id"][:12]):
                self.assertEqual(landtask.resolve(row, current),
                                 (self.tid, None))

    def test_a_trailing_number_names_an_open_task_only(self):  # noqa: VACUOUS_ASSERTION — the open task's lane is asserted to resolve to it
        n = self.num(self.tid)
        row = self.root(lane="fix-the-thing-%s" % n, kind="build")
        self.assertNotIn("task", row)
        self.assertEqual(landtask.resolve(row, self.snap()),
                         (self.tid, None))
        done = self.file("a closed ask")
        tasks.close(done, "closed before its lane landed")
        for lane in ("fix-%s" % self.num(done), "canary-seeded-red-0%s" % n,
                     "split-%sa" % n, "guard-%s-r3" % n):
            with self.subTest(lane=lane):
                other = self.root(lane=lane, kind="build")
                task, why = landtask.resolve(other, self.snap())
                self.assertIsNone(task)
                self.assertIn("no task", why)

    def test_two_stored_records_that_disagree_name_no_task(self):
        other = self.file("another ask")
        row = self.root(lane="plain-lane", task=self.tid)
        self.git("branch", "lane/plain-lane")
        self.assertEqual(taskkey.record_lane(self.repo, "plain-lane", other),
                         (True, None))
        task, why = landtask.resolve(row, self.snap())
        self.assertIsNone(task)
        self.assertIn("contradictory", why)


    def test_unknown_task_never_closes_a_disputed_chain_even_with_stale_facts(self):
        close_findings = mock.Mock(return_value=([self.tid], []))
        close_row = mock.Mock(return_value=None)
        land = {"task": None, "why": "contradictory car task",
                "lane": "renamed-lane", "row": "d1" * 8,
                "tip": SHA, "sha": SHA, "chain": "d2" * 8,
                "chain_rows": [{"id": "d3" * 8, "kind": "review",
                                "status": "open"}], "lane_rows": [],
                "label": "LAND 1"}
        report = landtask.run(land, close_findings=close_findings,
                              close_row=close_row, post=mock.Mock())
        close_findings.assert_not_called()
        close_row.assert_not_called()
        self.assertEqual(report["discharged"], [])
        self.assertIn("UNKNOWN", " ".join(report["findings_errors"] +
                                            report["errors"]))

    def test_a_late_conflicting_child_record_leaves_hand_land_chain_open(self):
        from helm import review_findings
        other = self.file("another ask")
        first = self.root(lane="plain-lane", task=self.tid)
        child, why = self.child(first["id"], lane="renamed-lane")
        self.assertIsNone(why, why)
        self.git("branch", "lane/renamed-lane")
        self.assertEqual(taskkey.record_lane(self.repo, "renamed-lane",
                                             other), (True, None))
        current = self.snap()
        task, why = landtask.resolve(child, current)
        self.assertIsNone(task)
        self.assertIn(self.tid, why)
        self.assertIn(other, why)
        with mock.patch.object(review_findings, "named_at",
                               return_value=([other], None)), \
                mock.patch.object(review_findings, "lane_tasks",
                                  return_value=(set(), None)):
            land, err = landtask.of_row(child["id"], None, SHA,
                                        current=current)
        self.assertIsNone(err, err)
        findings = mock.Mock(return_value=([other], []))
        rows = mock.Mock(return_value=None)
        report = landtask.run(land, close_findings=findings, close_row=rows,
                              post=mock.Mock())
        findings.assert_not_called()
        rows.assert_not_called()
        self.assertEqual(report["findings_closed"], [])
        self.assertEqual(report["discharged"], [])
        self.assertIn("UNKNOWN", " ".join(report["findings_errors"] +
                                            report["errors"]))


class WholeIsRecordedOnTheChainTest(LandBase):
    """D2 — `dispatch add|send --task task/N --whole` records on the chain's
    first row that the lane carries the whole ask."""

    def test_a_new_chain_records_whole_beside_its_task(self):  # noqa: VACUOUS_ASSERTION — the whole row's task_whole is asserted True beside the part row's absence
        row = self.root(task=self.tid, whole=True)
        self.assertIs(row.get("task_whole"), True)
        current = self.snap()
        self.assertIs(current[row["id"]].get("task_whole"), True)
        self.assertTrue(landtask.whole_of(self.tid, row, current))
        part = self.root(lane="lane-b", task=self.tid)
        self.assertNotIn("task_whole", part)
        self.assertFalse(landtask.whole_of(self.tid, part, self.snap()))

    def test_a_lane_record_of_the_whole_ask_is_read_at_the_land(self):
        row = self.root(lane="plain-lane", task=self.tid)
        self.git("branch", "lane/plain-lane")
        self.assertEqual(taskkey.record_whole(self.repo, "plain-lane",
                                              self.tid), (True, None))
        self.assertTrue(landtask.whole_of(self.tid, row, self.snap()))
        self.assertFalse(landtask.whole_of(self.file("another ask"), row,
                                           self.snap()))

    def test_whole_without_a_task_or_on_a_continuation_refuses(self):
        row, why = dispatches.add("seat-b", "plain-lane", repo=self.repo,
                                  kind="build", notify=False, new_work=True,
                                  _reason=True, ref=self.a, whole=True)
        self.assertIsNone(row)
        self.assertIn("--whole", why)
        parent = self.root(task=self.tid)
        kid, why = self.child(parent["id"], whole=True)
        self.assertIsNone(kid)
        self.assertIn("--whole", why)

    def test_send_reads_whole_only_in_its_trailing_options(self):
        """`--whole` is authority over the task's close, so a send reads it
        as `--force` is read: in the trailing option block, never inside
        the brief's prose."""
        for lane, words, tail in (
                ("lane-a", ["build", "it"], ["--whole"]),
                # lane-b carries a real path flag (--part) so the send
                # proceeds under the task/3938 gate; the "--whole" that appears
                # in the message prose must NOT be read as the path flag, so
                # task_whole stays absent and the prose is preserved intact.
                ("lane-b", ["keep", "--whole", "as", "words"], ["--part"])):
            rc, _out, err = run(dispatches.cmd_dispatch, [
                "send", "seat-b", lane] + words + [
                    "--ref", self.a, "--kind", "review", "--repo", self.repo,
                    "--new-work", "--task", self.tid] + tail)
            self.assertEqual(rc, 0, err)
        rows = {r["lane"]: r for r in dispatches.rows().values()}
        self.assertIs(rows["lane-a"].get("task_whole"), True)
        self.assertNotIn("task_whole", rows["lane-b"])
        self.assertIn("keep --whole as words",
                      rows["lane-b"].get("message_body") or "")

    def test_the_cli_takes_whole(self):
        rc, _out, err = run(dispatches.cmd_dispatch,
                            ["add", "seat-b", "lane-a", "--ref", self.a,
                             "--kind", "review", "--repo", self.repo,
                             "--new-work", "--task", self.tid, "--whole"])
        self.assertEqual(rc, 0, err)
        (row,) = dispatches.rows().values()
        self.assertEqual((row["task"], row.get("task_whole")),
                         (self.tid, True))


class TheLandStepTest(LandBase):
    """D2 — at the land a whole lane closes its task, and every other land
    asks the one question on the task and in its room."""

    def test_a_whole_lane_leaves_its_task_landed_owing_a_check(self):
        """helm/observed.py: a whole land no longer closes its task; the task
        reads LANDED with one named check owner, told once in its room."""
        row = self.root(lane="lane-a", task=self.tid, whole=True)
        report = self.hand(row)
        got = self.task()
        self.assertEqual(got["status"], "open")
        self.assertEqual(report["action"], landtask.LANDED)
        owner = got["landed"]["owner"]
        self.assertTrue(owner, got["landed"])
        self.assertEqual(report["check_owner"], owner)
        (told,) = self.room_rows(self.room_of(row))
        self.assertTrue(told["text"].startswith("@%s %s " % (owner,
                                                             self.tid)),
                        told["text"])
        # the task's own page says it
        rc, out, err = run(tasks.cmd_task, ["show", self.tid])
        self.assertEqual(rc, 0, err)
        self.assertIn("landed — owes a seen-working check by @%s" % owner,
                      out)
        self.assertIn("%s LANDED — owes a seen-working check by @%s"
                      % (self.tid, owner), "\n".join(landtask.lines(report)))

    def test_a_whole_lane_over_open_sub_tasks_stays_open_and_names_them(self):  # noqa: VACUOUS_ASSERTION — the comment naming the open sub-task is asserted exactly
        kid = self.file("the remainder", continues=self.tid)
        grand = self.file("the remainder's own part", continues=kid)
        tasks.close(kid, "its part went with an earlier land",
                    open_children=tasks.OPEN_CHILDREN_STAY)
        row = self.root(lane="lane-a", task=self.tid, whole=True)
        report = self.hand(row)
        self.assertEqual(self.task()["status"], "open")
        self.assertEqual(report["action"], landtask.STAYS_OPEN)
        self.assertEqual(report["open_children"], [grand])
        (text,) = self.said()
        self.assertEqual(text, "landed whole by hand at %s (lane lane-a at "
                         "%s): not closed, because its sub-task %s is open"
                         % (SHA[:12], row["tip"][:12], grand))

    def test_a_part_land_asks_the_one_question(self):
        row = self.root(lane="lane-a", task=self.tid)
        report = self.hand(row)
        self.assertEqual(self.task()["status"], "open")
        self.assertEqual(report["action"], landtask.ASKED)
        question = ("landed by hand at %s (lane lane-a at %s): is the whole "
                    "ask done? close it, narrow its title, or file the "
                    "remainder with --continues %s"
                    % (SHA[:12], row["tip"][:12], self.tid))
        self.assertEqual(self.said(), [question])
        room = self.room_of(row)
        self.assertEqual(room, "%s-%s" % (review_door.pair_scope(row),
                                          self.num(self.tid)))
        self.assertEqual(report["room"], room)
        (asked,) = self.room_rows(room)
        self.assertTrue(asked["text"].startswith(
            "@%s %s " % (dispatches.custodian_of(row), self.tid)),
            asked["text"])
        self.assertIn("is the whole ask done?", asked["text"])
        self.assertIn("asked in %s" % room, "\n".join(landtask.lines(report)))
        # the task's own page carries the question
        rc, out, err = run(tasks.cmd_task, ["show", self.tid])
        self.assertEqual(rc, 0, err)
        self.assertIn("is the whole ask done?", out)

    def test_a_part_land_names_the_story_root_in_continues(self):  # noqa: VACUOUS_ASSERTION — the --continues id is asserted to be the story root, not the served task
        part = self.file("a part of the ask", continues=self.tid)
        row = self.root(lane="lane-a", task=part)
        report = self.hand(row)
        self.assertEqual(self.task(part)["status"], "open")
        self.assertEqual(report["action"], landtask.ASKED)
        question = ("landed by hand at %s (lane lane-a at %s): is the whole "
                    "ask done? close it, narrow its title, or file the "
                    "remainder with --continues %s"
                    % (SHA[:12], row["tip"][:12], self.tid))
        self.assertEqual(self.said(part), [question])

    def test_the_land_step_is_idempotent(self):  # noqa: VACUOUS_ASSERTION — one comment and one room row are asserted after two runs
        row = self.root(lane="lane-a", task=self.tid)
        self.hand(row)
        again = self.hand(row)
        self.assertEqual(len(self.said()), 1)
        self.assertEqual(len(self.room_rows(self.room_of(row))), 1)
        self.assertEqual(again["action"], landtask.ASKED)
        whole = self.file("a second whole ask")
        other = self.root(lane="lane-b", task=whole, whole=True)
        self.hand(other)
        repeat = self.hand(other)
        self.assertEqual(repeat["action"], landtask.LANDED)
        self.assertEqual(self.task(whole)["status"], "open")
        self.assertEqual(len(self.said(whole)), 1)
        self.assertEqual(len(self.room_rows(self.room_of(other, whole))), 1)

    def test_a_closed_task_is_left_as_it_is(self):  # noqa: VACUOUS_ASSERTION — the recorded close reason is asserted unchanged
        row = self.root(lane="lane-a", task=self.tid)
        tasks.close(self.tid, "closed by its owner before the land")
        report = self.hand(row)
        self.assertEqual(report["action"], landtask.ALREADY_CLOSED)
        self.assertEqual(self.task()["closed_reason"],
                         "closed by its owner before the land")
        self.assertEqual(self.said(), [])
        self.assertEqual(self.room_rows(self.room_of(row)), [])

    def test_no_task_closes_and_asks_nothing_and_says_so(self):  # noqa: VACUOUS_ASSERTION — the no-task words are asserted; an unchanged ledger is the point
        row = self.root(lane="plain-lane", kind="build")
        with open(tasks.ledger_path()) as fh:
            before = fh.read()
        report = self.hand(row)
        self.assertEqual(report["action"], landtask.NO_TASK)
        with open(tasks.ledger_path()) as fh:
            self.assertEqual(fh.read(), before)
        said = "\n".join(landtask.lines(report))
        self.assertIn("no task", said)
        self.assertIn("nothing was closed or asked", said)

    def test_a_task_named_by_the_lane_number_is_asked(self):
        row = self.root(lane="fix-the-thing-%s" % self.num(self.tid))
        report = self.hand(row)
        self.assertEqual((report["task"], report["action"]),
                         (self.tid, landtask.ASKED))
        self.assertEqual(len(self.said()), 1)


class TheChainsRowsTest(LandBase):
    """D4 — every open row of the landed chain is offered to its own doors;
    a same-lane row with no chain link is flagged, never closed."""

    def test_chain_rows_are_discharged_and_an_unchained_row_is_flagged(self):  # noqa: VACUOUS_ASSERTION — the door asked and the flag are asserted exactly
        first = self.root(lane="lane-a", task=self.tid, kind="build")
        car, why = self.child(first["id"], lane="lane-a")
        self.assertIsNone(why, why)
        # A DELIBERATE FORK of the lane: a second chain nothing links
        stray = self.root(lane="lane-a", ref=self.b, force=True)

        def close_row(rid, reason, evidence, live=None, restart=None):
            self.asked.append((rid, reason))
            self.assertIn(SHA[:12], evidence)
            return None if (rid, reason) == (first["id"], "discharged") \
                else "refused"
        report = self.hand(car, close_row=close_row)
        self.assertEqual(self.asked, [(first["id"], "discharged")])
        self.assertEqual(report["discharged"], [(first["id"], "discharged")])
        self.assertEqual([f[0] for f in report["flagged"]], [stray["id"]])
        flag = [t for t in self.said() if stray["id"][:12] in t]
        self.assertEqual(len(flag), 1, self.said())
        self.assertIn("no chain link", flag[0])
        said = "\n".join(landtask.lines(report))
        self.assertIn("discharged row %s" % first["id"][:12], said)
        self.assertIn("FLAGGED row %s" % stray["id"][:12], said)

    def test_a_declared_delivery_asks_the_landed_door_first(self):
        first = self.root(lane="lane-a", task=self.tid, kind="build")
        car, _why = self.child(first["id"], lane="lane-a")
        self.hand(car, live=True)
        self.assertEqual(self.asked, [(first["id"], "landed"),
                                      (first["id"], "discharged"),
                                      (first["id"], "carried")])

    def test_a_row_every_door_refuses_stays_open_and_is_said(self):
        first = self.root(lane="lane-a", task=self.tid)
        car, _why = self.child(first["id"], lane="lane-a")
        report = self.hand(car)
        self.assertEqual(self.asked, [(first["id"], "discharged"),
                                      (first["id"], "carried")])
        self.assertEqual([k[0] for k in report["kept_open"]], [first["id"]])
        self.assertIn("NOT discharged row %s" % first["id"][:12],
                      "\n".join(landtask.lines(report)))
        self.assertTrue(any(first["id"][:12] in t for t in self.said()))


class TheHandLandsTest(LandBase):
    """D3 — the verbs that record a land by hand run the land step, and the
    verbs auto-land runs leave it to the LAND step."""

    def hand_with_fix(self, row, fix):
        """A synthetic FIX at a real ancestor of this land's tip."""
        current = self.snap()
        current[fix["id"]] = {**current[fix["id"]], "polarity": "fix",
                              "reviewed_tip": self.a}
        with mock.patch.object(dispatches, "snapshot",
                               return_value=(current, None)):
            return self.hand(row)

    def test_lr_close_landed_runs_the_land_step(self):
        row = self.root(lane="lane-a", task=self.tid)
        out = {"id": row["id"], "close_reason": "landed",
               "closing_trunk_sha": SHA}
        seen = []

        def hand(rid, verb, sha, live=None, restart=None, **_kw):
            seen.append((rid, verb, sha, live, restart))
            return {"task": self.tid, "action": landtask.ASKED,
                    "lines": ["the land step ran"]}
        with mock.patch.object(landreq, "close", return_value=(out, None)), \
                mock.patch.object(landtask, "hand", hand), \
                mock.patch.object(landtask, "lines",
                                  lambda report: report["lines"]):
            rc, got, err = run(landreq.cmd_lr, ["close", row["id"],
                                                "--reason", "landed",
                                                "--live"])
            self.assertEqual(rc, 0, err)
            self.assertIn("the land step ran", got)
            self.assertEqual(seen, [(row["id"], "close", SHA, True, None)])
            del seen[:]
            for argv in (["--reason", "landed", "--live", "--dry-run"],
                         ["--reason", "withdrawn", "--evidence", "moot"]):
                dry = dict(out, dry_run=True, reason="landed") \
                    if "--dry-run" in argv else dict(
                        out, close_reason="withdrawn")
                with self.subTest(argv=argv), mock.patch.object(
                        landreq, "close", return_value=(dry, None)):
                    rc, _got, err = run(landreq.cmd_lr,
                                        ["close", row["id"]] + argv)
                    self.assertEqual(rc, 0, err)
            self.assertEqual(seen, [])

    def test_foldcheck_apply_runs_it_for_each_row_it_closed(self):
        from helm import landreq_cli
        entries = [{"id": "a1" * 16, "tip": "1" * 40, "holder": "r",
                    "verdict": "CLOSED", "why": "ok"},
                   {"id": "a2" * 16, "tip": "2" * 40, "holder": "r",
                    "verdict": "REFUSED", "why": "no"}]
        seen = []
        with mock.patch.object(landreq, "source_clean_landings",
                               return_value=(entries, None)), \
                mock.patch.object(landtask, "hand",
                                  lambda rid, verb, sha, **kw: seen.append(
                                      (rid, verb, sha)) or {}), \
                mock.patch.object(landtask, "lines", lambda report: []):
            rc, _out, err = quiet(landreq_cli._print_source_clean_landings,
                                  self.repo, SHA, "gate:x", apply=True)
            self.assertEqual(rc, 0, err)
            self.assertEqual(seen, [("a1" * 16, "foldcheck", SHA)])
            del seen[:]
            quiet(landreq_cli._print_source_clean_landings, self.repo, SHA,
                  "gate:x", apply=False)
        self.assertEqual(seen, [])

    def test_lr_land_runs_it_once_trunk_carries_the_tip(self):  # noqa: VACUOUS_ASSERTION — the on-trunk cell asserts the one call exactly
        lr = {"id": "b1" * 16, "lane": "lane-a", "branch": "lane/lane-a",
              "repo_id": os.path.join(self.repo, ".git"),
              "reviewed_tip": "3" * 40}
        seen = []
        for state, runs in ((landreq.ANCESTOR, 1), ("not-ancestor", 0)):
            with self.subTest(state=state), \
                    mock.patch.object(landreq, "get",
                                      return_value=(lr, None)), \
                    mock.patch.object(landreq, "_tracked_deletions",
                                      return_value=([], None)), \
                    mock.patch.object(landreq, "_resolve_ref",
                                      return_value="refs/heads/main"), \
                    mock.patch.object(landreq, "_trunk_sha",
                                      return_value=SHA), \
                    mock.patch.object(landreq, "_patch_id",
                                      return_value="p" * 40), \
                    mock.patch.object(landreq, "_ancestry",
                                      return_value=state), \
                    mock.patch.object(landreq, "record_land",
                                      return_value=({"chain": "c",
                                                     "turn": "t"}, None)), \
                    mock.patch.object(landtask, "hand",
                                      lambda rid, verb, sha, **kw:
                                      seen.append((rid, verb, sha)) or {}), \
                    mock.patch.object(landtask, "lines", lambda report: []):
                del seen[:]
                rc, out, err = run(landreq.cmd_lr, ["land", lr["id"]])
                self.assertEqual(rc, 0, err)
                self.assertEqual(seen, [(lr["id"], "land", SHA)] * runs)
                if not runs:
                    self.assertIn("land step", out + err)

    def test_the_verbs_auto_land_runs_leave_the_step_to_it(self):  # noqa: VACUOUS_ASSERTION — the deferred report is asserted; nothing written is the point
        row = self.root(lane="lane-a", task=self.tid, whole=True)
        with mock.patch.dict(os.environ,
                             {landtask.DEFER_ENV: landtask.AUTO_LAND}):
            report = self.hand(row)
        self.assertTrue(report.get("deferred"))
        self.assertEqual(self.task()["status"], "open")
        self.assertEqual(self.said(), [])

    def test_a_hand_land_closes_the_chains_findings(self):  # noqa: VACUOUS_ASSERTION — the finding's close reason is asserted exactly
        first = self.root(lane="lane-a", task=self.tid)
        row, why = self.child(first["id"], lane="lane-a", ref=self.b)
        self.assertIsNone(why, why)
        chain = first.get("chain_root") or first["id"]
        found, why = tasks.add("the retry drops the lock", "integrator",
                               project=PROJECT, force_new=True,
                               continues=self.tid, found_in=first["id"],
                               found_chain=chain)
        self.assertIsNone(why, why)
        report = self.hand_with_fix(row, first)
        got = self.task(found["id"])
        self.assertEqual((got["status"], got["closed_reason"]),
                         ("closed", "cured in a hand land at %s" % SHA[:12]))
        self.assertEqual([c[0] for c in report["findings_closed"]],
                         [found["id"]])

    def test_a_hand_land_keeps_a_finding_a_live_numbered_lane_serves(self):  # noqa: VACUOUS_ASSERTION — open numbered-lane task is paired with another finding closed by the same hand LAND
        first = self.root(lane="lane-a")
        row, why = self.child(first["id"], lane="lane-a", ref=self.b)
        self.assertIsNone(why, why)
        chain = first.get("chain_root") or first["id"]
        mine, why = tasks.add("numbered lane work", "integrator",
                              tid="699999", project=PROJECT, force_new=True,
                              found_in=first["id"], found_chain=chain)
        self.assertIsNone(why, why)
        other, why = tasks.add("other review work", "integrator",
                               project=PROJECT, force_new=True,
                               found_in=first["id"], found_chain=chain)
        self.assertIsNone(why, why)
        self.git("branch", "lane/review-699999", row["tip"])
        report = self.hand_with_fix(row, first)
        self.assertEqual(self.task(mine["id"])["status"], "open")
        # CONTROL: an unrelated finding from the same chain is cured.
        self.assertEqual([t for t, _r in report["findings_closed"]],
                         [other["id"]])

    def test_a_hand_land_that_cannot_read_the_findings_says_so(self):  # noqa: VACUOUS_ASSERTION — open finding is paired with a reported failed lane read in the same hand LAND
        """task/3862 L2, on the hand path: a findings read that fails at the
        land is said in the step's report, never a silent nothing."""
        from helm import review_findings
        first = self.root(lane="lane-a", task=self.tid)
        row, why = self.child(first["id"], lane="lane-a", ref=self.b)
        self.assertIsNone(why, why)
        chain = first.get("chain_root") or first["id"]
        found, why = tasks.add("the retry drops the lock", "integrator",
                               project=PROJECT, force_new=True,
                               continues=self.tid, found_in=first["id"],
                               found_chain=chain)
        self.assertIsNone(why, why)
        current = self.snap()
        current[first["id"]] = {**current[first["id"]], "polarity": "fix",
                                "reviewed_tip": self.a}
        with mock.patch.object(dispatches, "snapshot",
                               return_value=(current, None)), \
                mock.patch.object(review_findings, "lane_tasks",
                                  return_value=(set(), "the lane records of r "
                                                "could not be read (torn)")):
            report = self.hand(row)
        self.assertEqual(self.task(found["id"])["status"], "open")
        self.assertEqual(report["findings_errors"], [
            "the land could not read them: the lane records of r could not "
            "be read (torn); nothing was closed"])
        self.assertIn("NOT DONE — the chain's findings: the land could not "
                      "read them", "\n".join(landtask.lines(report)))


class CommentShapeTest(unittest.TestCase):
    """The land step's comments are read back by the close-candidate list
    through the one parser, never a second regex."""

    def test_every_comment_the_step_writes_parses_back(self):  # noqa: VACUOUS_ASSERTION — each written comment is asserted to parse to its fields
        land = {"task": "task/7", "lane": "lane-a", "tip": "1" * 40,
                "label": "LAND 537", "sha": SHA}
        asked = landtask.question(land)
        got = landtask.parse_comment(asked)
        self.assertEqual((got["land"], got["sha"], got["lane"], got["whole"]),
                         ("LAND 537", SHA[:12], "lane-a", False))
        stays = landtask.stays_open(land, ["task/8"])
        got = landtask.parse_comment(stays)
        self.assertEqual((got["land"], got["whole"]), ("LAND 537", True))
        hand = landtask.question(dict(land, label=None))
        self.assertEqual(landtask.parse_comment(hand)["land"], None)
        self.assertIsNone(landtask.parse_comment("a person's comment"))


if __name__ == "__main__":
    unittest.main()
