#!/usr/bin/env python3
"""A review's findings are sub-tasks of the task under review, and its notes
are comments on it (task/3742; helm/review_findings.py).

THE CONTRACT THESE ARMS PIN, one class per decision:
  D1 `helm dispatch verdict --finding` files each finding, in the verdict's
     own write, as a task continuing the task the chain serves, owned by the
     lane's author; `--note` is a comment on that task and never a row; a
     named finding derives `--finding-count`, and a count that disagrees
     refuses. A finding the task door would refuse refuses the verdict.
  D2 `--finding-carried task/N` names an open finding of this chain instead
     of filing it twice; one outside the chain, closed, or under another task
     refuses.
  D3 a finding closes when a row of its chain is held source-clean at a tip
     strictly descending from every FIX that named it, and as "retracted"
     with the FIX that filed it when the retraction reads source-clean;
     nothing else closes it. (The LAND close is armed in
     tests/test_autoland.py, where the LAND step runs.)
  D4 a review chain with no task refuses before a verdict can file findings;
     an old no-task chain also refuses, rather than minting top-level work.

Every write goes to a scratch HELM_HOME (td.DispatchBase); no arm reads the
real ledgers.
"""
import os
import unittest
from unittest import mock

from helm import dispatches, landreq, review_findings, taskkey, tasks, seats
from tests import _tmphome, test_dispatches as td
from tests.test_dispatches import run

REVIEWER = "seat-b"
PROJECT = "placeholder-project"
FIX_ANSWERS = ("--prior-relation", "new", "--worse-than-main",
               "helm/placeholder.py", "--no-patch-because",
               "a design finding for the meld")


class FindingsBase(td.DispatchBase):
    """A scratch dispatch ledger and task ledger, with one open task that
    review chains serve."""

    def setUp(self):
        super().setUp()
        self.task, why = tasks.add("the reviewed work", "integrator",
                                   project=PROJECT, force_new=True)
        self.assertIsNone(why, why)
        self.tid = self.task["id"]

    def row(self, ref=None, task=True, **kw):
        kw.setdefault("recipient", REVIEWER)
        if "supersedes" not in kw:
            kw["task"] = self.tid if task else False
        return self.add(ref=ref or self.b, **kw)

    def verdict(self, row, *flags, polarity="--fix"):
        answers = FIX_ANSWERS if polarity == "--fix" else ()
        with self.verdict_author():
            return run(dispatches.cmd_dispatch, [
                "verdict", row["id"], row["tip"], polarity, "--measured",
                *answers, *flags, "the read found work"])

    def library_fix(self, row, findings=(), carried=(), notes=()):
        """A FIX recorded through the library door, as the seat's CLI does
        once its flags are parsed."""
        out, why = dispatches.mark_verdict(
            row["id"], row["tip"], "the read found work", "fix",
            basis="measured", prior_relation="new",
            worse_than_main_paths=["helm/placeholder.py"],
            no_patch_because="a design finding for the meld",
            findings=list(findings), notes=list(notes),
            **({"findings_carried": list(carried)} if carried else {}))
        self.assertIsNone(why, why)
        return out

    def found_in(self, rid):
        return sorted((r for r in tasks.rows().values()
                       if r.get("found_in") == rid),
                      key=tasks.sort_key)

    def state(self, rid):
        return dispatches.snapshot()[0][rid]

    def chain(self, row):
        return row.get("chain_root") or row["id"]


class AFindingIsASubTaskOfTheTaskUnderReviewTest(FindingsBase):
    """D1 — the one door files the work where the task's story shows it."""

    def test_a_FIX_with_one_finding_files_one_sub_task(self):
        row = self.row()
        rc, out, err = self.verdict(row, "--finding",
                                    "the retry drops the lock")
        self.assertEqual(rc, 0, err)
        kids = self.found_in(row["id"])
        self.assertEqual([k["title"] for k in kids],
                         ["the retry drops the lock"])
        kid = kids[0]
        self.assertEqual(kid["continues"], self.tid)
        self.assertEqual(kid["owner"], row["sender"])
        self.assertEqual(kid["project"], PROJECT)
        self.assertEqual(kid["origin"], "agent")
        self.assertEqual(kid["source"], REVIEWER)
        self.assertEqual(kid["status"], "open")
        self.assertEqual(kid["found_chain"], self.chain(row))
        self.assertIn("found in review of %s: row %s" % (
            self.tid, row["id"][:12]), kid["note"])
        # the count is derived from the one finding named
        state = self.state(row["id"])
        self.assertEqual(state["finding_count"], 1)
        self.assertEqual(state["findings"], ["the retry drops the lock"])
        self.assertEqual(state["findings_task"], self.tid)
        self.assertIn("helm dispatch: finding filed: %s (sub-task of %s, "
                      "owner @%s): the retry drops the lock"
                      % (kid["id"], self.tid, row["sender"]), out)

    def test_the_task_shows_the_finding_as_its_sub_task(self):
        row = self.row()
        rc, _out, err = self.verdict(row, "--finding",
                                     "the retry drops the lock")
        self.assertEqual(rc, 0, err)
        kid = self.found_in(row["id"])[0]
        rc, out, err = run(tasks.cmd_task, ["show", self.tid])
        self.assertEqual(rc, 0, err)
        self.assertIn(kid["id"], out)
        self.assertIn("the retry drops the lock", out)
        self.assertIn("0 of 1 done", out)
        # and the finding names its parent and where it was found
        rc, out, err = run(tasks.cmd_task, ["show", kid["id"]])
        self.assertEqual(rc, 0, err)
        self.assertIn(self.tid, out)
        self.assertIn("found in review of %s: row %s" % (
            self.tid, row["id"][:12]), out)

    def test_lr_show_lists_the_findings_of_the_row(self):
        row = self.row()
        rc, _out, err = self.verdict(row, "--finding",
                                     "the retry drops the lock")
        self.assertEqual(rc, 0, err)
        kid = self.found_in(row["id"])[0]
        rc, out, err = run(landreq.cmd_lr, ["show", row["id"]])
        self.assertEqual(rc, 0, err)
        self.assertIn("  finding   %-11s %-11s %s" % (
            kid["id"], "open", "the retry drops the lock"), out)

    def test_three_findings_and_a_note_print_what_was_filed(self):
        row = self.row()
        rc, out, err = self.verdict(
            row, "--finding", "the retry drops the lock",
            "--finding", "the cap is off by one",
            "--finding", "the refusal names no remedy",
            "--note", "the naming reads well")
        self.assertEqual(rc, 0, err)
        kids = self.found_in(row["id"])
        self.assertEqual(len(kids), 3)
        by_title = {k["title"]: k["id"] for k in kids}
        self.assertIn("helm dispatch: findings: 3; prior relation: new", out)
        lines = [ln for ln in out.splitlines()
                 if ln.startswith(("helm dispatch: finding filed",
                                   "helm dispatch: finding carried",
                                   "helm dispatch: note on",
                                   "helm dispatch: no task"))]
        self.assertEqual(lines, [
            "helm dispatch: finding filed: %s (sub-task of %s, owner @%s): "
            "the retry drops the lock"
            % (by_title["the retry drops the lock"], self.tid, row["sender"]),
            "helm dispatch: finding filed: %s (sub-task of %s, owner @%s): "
            "the cap is off by one"
            % (by_title["the cap is off by one"], self.tid, row["sender"]),
            "helm dispatch: finding filed: %s (sub-task of %s, owner @%s): "
            "the refusal names no remedy"
            % (by_title["the refusal names no remedy"], self.tid,
               row["sender"]),
            "helm dispatch: note on %s (a comment, not a row): the naming "
            "reads well" % self.tid])
        # the note is a comment on the task, written by the reviewer
        comments = tasks.rows()[self.tid]["comments"]
        self.assertEqual([(c["text"], c["by"]) for c in comments], [(
            "review note (row %s at %s): the naming reads well"
            % (row["id"][:12], row["tip"][:12]), REVIEWER)])
        rc, show, err = run(tasks.cmd_task, ["show", self.tid])
        self.assertEqual(rc, 0, err)
        self.assertIn("0 of 3 done", show)
        self.assertIn("the naming reads well", show)

    def test_a_CONCUR_with_a_note_files_no_row(self):  # noqa: VACUOUS_ASSERTION — the unchanged row count is paired with the note's comment asserted exactly on the same task, and the refused --finding beside it
        row = self.row()
        before = sorted(tasks.rows())
        rc, out, err = self.verdict(row, "--note", "a clean, careful change",
                                    polarity="--concur")
        self.assertEqual(rc, 0, err)
        self.assertEqual(sorted(tasks.rows()), before)
        self.assertEqual(self.found_in(row["id"]), [])
        self.assertEqual([c["text"] for c in
                          tasks.rows()[self.tid]["comments"]],
                         ["review note (row %s at %s): a clean, careful "
                          "change" % (row["id"][:12], row["tip"][:12])])
        self.assertIn("helm dispatch: note on %s (a comment, not a row): "
                      "a clean, careful change" % self.tid, out)
        # CONTROL: the same polarity naming WORK refuses, and records nothing
        other = self.row()
        rc, _out, err = self.verdict(other, "--finding", "a real defect",
                                     polarity="--concur")
        self.assertEqual(rc, 1, err)
        self.assertIn("name the work a FIX hands back", err)
        self.assertEqual(self.state(other["id"])["status"], "open")
        self.assertEqual(sorted(tasks.rows()), before)

    def test_an_APPROVE_with_a_note_files_no_row(self):  # noqa: VACUOUS_ASSERTION — the unchanged row count is paired with the note's comment asserted exactly on the same task
        row = self.row(ref=self.side)
        before = sorted(tasks.rows())
        with mock.patch.object(dispatches.gate, "bind", return_value=(
                "VERIFIED", "ab" * 8, "a test receipt")):
            out, why = dispatches.mark_verdict(
                row["id"], row["tip"], "approved", "approve",
                basis="measured", notes=["the tests read well"])
        self.assertIsNone(why, why)
        self.assertEqual(out["polarity"], "approve")
        self.assertEqual(sorted(tasks.rows()), before)
        self.assertEqual([c["text"] for c in
                          tasks.rows()[self.tid]["comments"]],
                         ["review note (row %s at %s): the tests read well"
                          % (row["id"][:12], row["tip"][:12])])

    def test_a_count_that_disagrees_with_the_findings_refuses(self):
        row = self.row()
        rc, _out, err = self.verdict(row, "--finding-count", "2",
                                     "--finding", "the retry drops the lock")
        self.assertEqual(rc, 2, err)
        self.assertIn("--finding-count 2 disagrees with the 1 finding(s) "
                      "named", err)
        self.assertEqual(self.state(row["id"])["status"], "open")
        self.assertEqual(self.found_in(row["id"]), [])
        rc, _out, err = self.verdict(row, "--finding-count", "UNKNOWN",
                                     "--finding", "the retry drops the lock")
        self.assertEqual(rc, 2, err)
        self.assertIn("the count is known", err)
        # CONTROL: the count that agrees is the one it would derive
        rc, _out, err = self.verdict(row, "--finding-count", "1",
                                     "--finding", "the retry drops the lock")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.state(row["id"])["finding_count"], 1)
        self.assertEqual(len(self.found_in(row["id"])), 1)

    def test_library_uncured_count_only_refuses_before_the_verdict_append(self):
        row = self.row()
        before = sorted(tasks.rows())
        self.assertIn(self.tid, before)
        out, why = dispatches.mark_verdict(
            row["id"], row["tip"], "two uncured findings", "fix",
            basis="measured", finding_count=2, prior_relation="new",
            worse_than_main_paths=["helm/placeholder.py"],
            no_patch_because="the review needs a design meld")
        self.assertIsNone(out)
        self.assertIn("name each uncured finding with --finding", why)
        self.assertEqual(self.state(row["id"])["status"], "open")
        self.assertEqual(sorted(tasks.rows()), before)
        # The shared API still accepts the same count when it names the work.
        out, why = dispatches.mark_verdict(
            row["id"], row["tip"], "two uncured findings", "fix",
            basis="measured", finding_count=2, prior_relation="new",
            worse_than_main_paths=["helm/placeholder.py"],
            no_patch_because="the review needs a design meld",
            findings=["the retry drops the lock", "the cap is off by one"])
        self.assertIsNone(why, why)
        self.assertEqual(out["finding_count"], 2)
        self.assertEqual(len(self.found_in(row["id"])), 2)

    def test_an_exact_retry_of_a_recorded_count_only_fix_returns_the_standing_verdict(self):  # noqa: VACUOUS_ASSERTION — positive control on standing verdict replay and control refusals
        """task/4050 residual A2: an exact retry of a recorded count-only FIX returns
        the standing verdict rather than being refused by the count-only guard."""
        row = self.row()
        # Record a count-only FIX directly in the ledger (as existed on historical rows)
        event = {
            "v": 3,
            "event": "verdict",
            "seq": row["seq"] + 1,
            "id": row["id"],
            "ts": dispatches.pk.now_ts(),
            "reviewed_tip": row["tip"],
            "verdict_ref": "counted only",
            "polarity": "fix",
            "basis": "measured",
            "finding_count": 2,
            "prior_relation": "new",
            "no_patch_because": "a design finding for a meld",
            "gate": "",
            "gate_caps": [],
        }
        from helm import eventledger
        self.assertTrue(eventledger.append(dispatches.ledger_path(), event))
        standing = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(standing["status"], "verdict")
        self.assertEqual(standing["finding_count"], 2)

        # 1. An exact retry returns the standing verdict (fails at e485f20361c)
        out, why = dispatches.mark_verdict(
            row["id"], row["tip"], "counted only", "fix",
            basis="measured", finding_count=2, prior_relation="new",
            no_patch_because="a design finding for a meld")
        self.assertIsNone(why, why)
        self.assertIsNotNone(out)
        self.assertEqual(out["id"], row["id"])
        self.assertEqual(out["status"], "verdict")
        self.assertEqual(out["finding_count"], 2)

        # 2. CONTROL: a retry that differs in any field is still refused
        _diff, diff_why = dispatches.mark_verdict(
            row["id"], row["tip"], "different evidence", "fix",
            basis="measured", finding_count=2, prior_relation="new",
            no_patch_because="a design finding for a meld")
        self.assertIsNone(_diff)
        self.assertIn("already has a verdict (closed) — a standing verdict is immutable", diff_why)

        # 3. CONTROL: a new count-only FIX on an open row is still guarded exactly as before
        new_row = self.row(ref=self.c)
        new_out, new_why = dispatches.mark_verdict(
            new_row["id"], new_row["tip"], "new count only", "fix",
            basis="measured", finding_count=2, prior_relation="new",
            no_patch_because="a design finding for a meld")
        self.assertIsNone(new_out)
        self.assertIn("name each uncured finding with --finding", new_why)

    def test_an_exact_cli_retry_of_a_recorded_count_only_fix_reconciles(self):
        """The same retry through `helm dispatch verdict`, the door a seat
        actually uses. The CLI carried its own copy of the count-only guard
        ahead of mark_verdict, so moving the library guard behind the
        reconcile left the CLI retry refused by a guard about NEW work."""
        row = self.row()
        from helm import eventledger
        self.assertTrue(eventledger.append(dispatches.ledger_path(), {
            "v": 3, "event": "verdict", "seq": row["seq"] + 1,
            "id": row["id"], "ts": dispatches.pk.now_ts(),
            "reviewed_tip": row["tip"], "verdict_ref": "the read found work",
            "polarity": "fix", "basis": "measured", "finding_count": 2,
            "prior_relation": "new", "exit_answer": "worse-than-main",
            "worse_than_main_paths": ["helm/placeholder.py"],
            "no_patch_because": "a design finding for the meld",
            "gate": "", "gate_caps": []}))
        self.assertEqual(self.state(row["id"])["status"], "verdict")
        before = len(dispatches.history(row["id"]))
        rc, _out, err = self.verdict(row, "--finding-count", "2")
        self.assertEqual((rc, err), (0, ""),
                         "an exact retry must reconcile, not be guarded")
        self.assertEqual(len(dispatches.history(row["id"])), before)
        # CONTROL: the same count-only FIX on an open row is still refused,
        # and records nothing.
        fresh = self.row(ref=self.c)
        rc, _out, err = self.verdict(fresh, "--finding-count", "2")
        self.assertNotEqual(rc, 0)
        self.assertIn("name each uncured finding with --finding", err)
        self.assertEqual(self.state(fresh["id"])["status"], "open")

    def test_uncured_count_only_refuses_without_recording_work(self):
        row = self.row()
        before = sorted(tasks.rows())
        self.assertIn(self.tid, before)
        rc, _out, err = self.verdict(row, "--finding-count", "2")
        # the shared write door refuses it (rc 1), after its retry reconcile
        self.assertEqual(rc, 1, err)
        self.assertIn("name each uncured finding with --finding", err)
        self.assertEqual(self.state(row["id"])["status"], "open")
        self.assertEqual(sorted(tasks.rows()), before)
        rc, _out, err = self.verdict(row, "--finding-count", "2",
                                     "--finding", "the retry drops the lock",
                                     "--finding", "the cap is off by one")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.found_in(row["id"])), 2)

    def test_a_finding_the_task_door_refuses_refuses_the_verdict(self):
        row = self.row()
        rc, _out, err = self.verdict(row, "--finding",
                                     "patch the orca binary to stop it")
        self.assertEqual(rc, 1, err)
        self.assertIn("refused by the task door", err)
        self.assertEqual(self.state(row["id"])["status"], "open")
        self.assertEqual(self.found_in(row["id"]), [])
        # CONTROL: the same verdict with a finding the door takes
        rc, _out, err = self.verdict(row, "--finding",
                                     "the retry drops the lock")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.found_in(row["id"])), 1)

    def test_a_ledger_that_refused_the_filing_is_filed_on_retry(self):  # noqa: VACUOUS_ASSERTION — the empty filing is paired with the same row filed exactly once by the retry below
        row = self.row()
        real = tasks.add
        with mock.patch.object(tasks, "add", return_value=(
                None, "task ledger is not writable")):
            out = self.library_fix(row, findings=["the retry drops the lock"])
        self.assertEqual(out["status"], "verdict")
        self.assertEqual(out["findings"], ["the retry drops the lock"])
        self.assertEqual(self.found_in(row["id"]), [])
        self.assertIn("task ledger is not writable",
                      "; ".join(out["findings_report"]["errors"]))
        self.assertIs(tasks.add, real)
        again = self.library_fix(row, findings=["the retry drops the lock"])
        kids = self.found_in(row["id"])
        self.assertEqual([k["title"] for k in kids],
                         ["the retry drops the lock"])
        self.assertEqual(again["findings_report"]["filed"],
                         [(kids[0]["id"], "the retry drops the lock")])
        # and a third run files nothing twice
        self.library_fix(row, findings=["the retry drops the lock"])
        self.assertEqual(len(self.found_in(row["id"])), 1)

    def test_a_task_row_records_where_a_finding_was_found(self):
        row, why = tasks.add("a finding filed by hand", "integrator",
                             force_new=True, found_in="ab" * 8)
        self.assertIsNone(row)
        self.assertIn("both or neither", why)
        # CONTROL: both halves record, and the wire publishes them
        row, why = tasks.add("a finding filed by hand", "integrator",
                             force_new=True, found_in="ab" * 8,
                             found_chain="cd" * 8)
        self.assertIsNone(why, why)
        wire = tasks.public_row(tasks.rows()[row["id"]])
        self.assertEqual((wire["found_in"], wire["found_chain"]),
                         ("ab" * 8, "cd" * 8))

    def test_a_refused_verdict_line_keeps_the_findings(self):
        row = self.row()
        with self.verdict_author():
            rc, _out, err = run(dispatches.cmd_dispatch, [
                "verdict", row["id"], row["tip"], "--fix", "--measured",
                "--prior-relation", "new", "--worse-than-main",
                "helm/placeholder.py", "--finding", "the retry drops the lock",
                "--note", "the naming reads well", "the read found work"])
        self.assertEqual(rc, 2, err)
        line = [ln for ln in err.splitlines()
                if ln.startswith("corrected: ")][-1]
        self.assertIn("--finding 'the retry drops the lock'", line)
        self.assertIn("--note 'the naming reads well'", line)
        self.assertNotIn("--finding-count", line)


class ReviewerRedArmsTest(FindingsBase):
    """Reviewer arms (task/3742 slice b review): a finding is never born
    open under a closed parent, and a refused non-FIX line keeps the work."""

    def test_a_FIX_on_a_chain_whose_task_is_closed_files_no_open_child(self):
        row = self.row()
        _row, why = tasks.close(self.tid, "the owner closed it")
        self.assertIsNone(why, why)
        rc, _out, err = self.verdict(row, "--finding",
                                     "the retry drops the lock")
        self.assertEqual(rc, 1, err)
        self.assertIn("%s is closed" % self.tid, err)
        self.assertEqual(self.state(row["id"])["status"], "open")
        self.assertEqual(self.found_in(row["id"]), [])
        # CONTROL: a note on the closed task is still only a comment
        rc, _out, err = self.verdict(row, "--note", "the naming reads well",
                                     polarity="--concur")
        self.assertEqual(rc, 0, err)

    def test_a_refused_CONCUR_line_keeps_the_findings_it_named(self):
        row = self.row()
        with self.verdict_author():
            rc, _out, err = run(dispatches.cmd_dispatch, [
                "verdict", row["id"], row["tip"], "--concur", "--measured",
                "--finding", "the retry drops the lock",
                "the read found work"])
        self.assertEqual(rc, 1, err)
        line = [ln for ln in err.splitlines()
                if ln.startswith("corrected: ")][-1]
        self.assertIn("--finding 'the retry drops the lock'", line)
        self.assertNotIn(" --concur ", line)
        # CONTROL: a CONCUR with only a note keeps its direction
        other = self.row()
        with self.verdict_author():
            rc, _out, err = run(dispatches.cmd_dispatch, [
                "verdict", other["id"], other["tip"][:6], "--concur",
                "--measured", "--note", "the naming reads well",
                "the read found work"])
        self.assertNotEqual(rc, 0, err)
        line = [ln for ln in err.splitlines()
                if ln.startswith("corrected: ")][-1]
        self.assertIn(" --concur ", line)
        self.assertIn("--note 'the naming reads well'", line)


class ACarriedFindingIsNotFiledTwiceTest(FindingsBase):
    """D2 — a later round names the same issue by its row."""

    def rounds(self):
        first = self.row()
        self.library_fix(first, findings=["the retry drops the lock"])
        carried = self.found_in(first["id"])[0]
        second = self.row(ref=self.c, task=False, supersedes=first["id"])
        return first, carried, second

    def test_a_second_round_carries_one_and_adds_one(self):
        first, carried, second = self.rounds()
        rc, out, err = self.verdict(second, "--finding-carried",
                                    carried["id"], "--finding",
                                    "the cap is off by one")
        self.assertEqual(rc, 0, err)
        titles = [r["title"] for r in tasks.rows().values()
                  if r.get("found_in")]
        self.assertEqual(sorted(titles), ["the cap is off by one",
                                          "the retry drops the lock"])
        added = self.found_in(second["id"])
        self.assertEqual([k["title"] for k in added],
                         ["the cap is off by one"])
        self.assertEqual(added[0]["continues"], self.tid)
        self.assertEqual(self.state(second["id"])["finding_count"], 2)
        self.assertEqual(self.state(second["id"])["findings_carried"],
                         [carried["id"]])
        said = tasks.rows()[carried["id"]]["comments"]
        self.assertEqual([c["text"] for c in said], [
            "carried: still open at %s, named again by the FIX on row %s"
            % (second["tip"][:12], second["id"][:12])])
        self.assertIn("helm dispatch: finding carried: %s (still open, no "
                      "second row): the retry drops the lock"
                      % carried["id"], out)
        rc, show, err = run(landreq.cmd_lr, ["show", second["id"]])
        self.assertEqual(rc, 0, err)
        self.assertIn("  carried   %-11s %-11s %s" % (
            carried["id"], "open", "the retry drops the lock"), show)
        rc, show, err = run(tasks.cmd_task, ["show", self.tid])
        self.assertEqual(rc, 0, err)
        self.assertIn("0 of 2 done", show)

    def test_the_same_words_named_again_are_carried_not_filed(self):
        _first, carried, second = self.rounds()
        rc, _out, err = self.verdict(second, "--finding",
                                     "the retry drops the lock")
        self.assertEqual(rc, 1, err)
        self.assertIn("already open as %s in this chain: name it with "
                      "--finding-carried %s" % (carried["id"], carried["id"]),
                      err)
        self.assertEqual(self.state(second["id"])["status"], "open")
        self.assertEqual([r["id"] for r in tasks.rows().values()
                          if r.get("title") == "the retry drops the lock"],
                         [carried["id"]])

    def refused(self, row, tid, why):
        rc, _out, err = self.verdict(row, "--finding-carried", tid)
        self.assertEqual(rc, 1, err)
        self.assertIn("--finding-carried %s: %s" % (tid, why), err)
        self.assertEqual(self.state(row["id"])["status"], "open")

    def test_a_finding_of_another_chain_is_not_carried(self):  # noqa: VACUOUS_ASSERTION — `refused` asserts the refusal and the row left open; the valid carry on the same round records in test_the_valid_carry_beside_the_refusals_records
        _first, _carried, second = self.rounds()
        stranger = self.row()
        self.library_fix(stranger, findings=["a stranger's finding"])
        theirs = self.found_in(stranger["id"])[0]
        self.refused(second, theirs["id"], "it was found in another chain")

    def test_a_closed_finding_is_not_carried(self):  # noqa: VACUOUS_ASSERTION — `refused` asserts the refusal and the row left open; the valid carry on the same round records in test_the_valid_carry_beside_the_refusals_records
        _first, carried, second = self.rounds()
        _row, why = tasks.close(carried["id"], "cured by hand")
        self.assertIsNone(why, why)
        self.refused(second, carried["id"], "it is closed")

    def test_a_finding_under_another_task_is_not_carried(self):  # noqa: VACUOUS_ASSERTION — `refused` asserts the refusal and the row left open; the valid carry on the same round records in test_the_valid_carry_beside_the_refusals_records
        first, _carried, second = self.rounds()
        other, why = tasks.add("another piece of work", "integrator",
                               project=PROJECT, force_new=True)
        self.assertIsNone(why, why)
        stray, why = tasks.add(
            "a finding filed under the other task", "integrator",
            project=PROJECT, continues=other["id"], force_new=True,
            found_in=first["id"], found_chain=self.chain(first))
        self.assertIsNone(why, why)
        self.refused(second, stray["id"], "it continues %s, not %s"
                     % (other["id"], self.tid))

    def test_a_task_that_is_no_finding_is_not_carried(self):  # noqa: VACUOUS_ASSERTION — `refused` asserts the refusal and the row left open; the valid carry on the same round records in test_the_valid_carry_beside_the_refusals_records
        _first, _carried, second = self.rounds()
        self.refused(second, self.tid, "it is not a review finding")

    def test_the_valid_carry_beside_the_refusals_records(self):
        """CONTROL for the refusals above: the one open finding of this chain
        under this task is carried, on the same second round."""
        _first, carried, second = self.rounds()
        rc, _out, err = self.verdict(second, "--finding-carried",
                                     carried["id"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.state(second["id"])["findings_carried"],
                         [carried["id"]])


class ClosersBase(FindingsBase):
    """A reader of this repo holds rows, and the closer arms' helpers."""

    def row(self, ref=None, task=True, **kw):
        # Legacy closer fixtures used unparented findings; a review now owes
        # a task, including when these arms test the closer independently.
        return super().row(ref=ref, task=True, **kw)

    def setUp(self):
        super().setUp()
        real = dispatches._acting_author

        def acting(action="author this dispatch"):
            if action == "hold this row":
                return REVIEWER, None
            return real(action)
        patch = mock.patch.object(dispatches, "_acting_author", acting)
        patch.start()
        self.addCleanup(patch.stop)

    def hold(self, row, tip):
        out, why = dispatches.mark_hold(row["id"], "read clean; fab Ran 5 tests OK",
                                        source_clean_tip=tip)
        self.assertIsNone(why, why)
        self.assertEqual(out["source_clean_tip"], tip)
        return out

    def retract(self, row, reads="source-clean"):
        """Retract the FIX on `row`, reading `reads` now: source-clean is the
        one reading that answers its findings (task/3862 M1)."""
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": REVIEWER}):
            out, why = dispatches.retract(
                row["id"], "the verdict read the wrong tree", reads,
                "measured", notify=False)
        self.assertIsNone(why, why)
        return out

    def status(self, tid):
        return tasks.rows()[tid]["status"]


class AFindingClosesWhenItsChainAnswersItTest(ClosersBase):
    """D3 — a source-clean hold at a descending tip, and a retraction."""

    def found_then_round(self, ref):
        first = self.row()
        self.library_fix(first, findings=["the retry drops the lock"])
        finding = self.found_in(first["id"])[0]
        second = self.row(ref=ref, task=False, supersedes=first["id"])
        return finding, second

    def test_a_hold_at_a_descending_tip_closes_the_finding(self):
        finding, second = self.found_then_round(self.c)
        out = self.hold(second, self.c)
        reason = "cured, held source-clean at %s (row %s)" % (
            self.c[:12], second["id"][:12])
        self.assertEqual(out["findings_closed"], [(finding["id"], reason)])
        got = tasks.rows()[finding["id"]]
        self.assertEqual((got["status"], got["closed_reason"]),
                         ("closed", reason))

    def test_the_hold_verb_says_what_it_closed(self):
        finding, second = self.found_then_round(self.c)
        rc, out, err = run(dispatches.cmd_dispatch, [
            "hold", second["id"], "--source-clean", self.c, "read clean; fab Ran 5 tests OK"])
        self.assertEqual(rc, 0, err)
        self.assertIn("helm dispatch: finding %s closed: cured, held "
                      "source-clean at %s (row %s)" % (
                          finding["id"], self.c[:12], second["id"][:12]),
                      out)

    def test_a_hold_at_a_tip_that_does_not_descend_closes_nothing(self):  # noqa: VACUOUS_ASSERTION — the hold is asserted recorded at its tip, and test_a_hold_at_a_descending_tip_closes_the_finding closes the same shape at a descending tip
        finding, second = self.found_then_round(self.side)
        out = self.hold(second, self.side)
        self.assertNotIn("findings_closed", out)
        self.assertEqual(tasks.rows()[finding["id"]]["status"], "open")

    def test_a_hold_at_the_FIX_tip_itself_closes_nothing(self):  # noqa: VACUOUS_ASSERTION — the hold is asserted recorded at its tip, and the descending-tip arm closes the same shape
        finding, second = self.found_then_round(self.b)
        out = self.hold(second, self.b)
        self.assertNotIn("findings_closed", out)
        self.assertEqual(tasks.rows()[finding["id"]]["status"], "open")

    def carried_then_round(self, ref):
        """A finding filed at b, carried by a FIX at c, then a round at
        `ref`: the newest FIX that named it is the one at c."""
        first = self.row()
        self.library_fix(first, findings=["the retry drops the lock"])
        finding = self.found_in(first["id"])[0]
        second = self.row(ref=self.c, task=False, supersedes=first["id"])
        self.library_fix(second, carried=[finding["id"]])
        return finding, self.row(ref=ref, task=False,
                                 supersedes=second["id"])

    def test_a_carried_finding_closes_past_the_round_that_carried_it(self):
        past = self.commit("past the carrying round")
        finding, third = self.carried_then_round(past)
        out = self.hold(third, past)
        self.assertEqual([t for t, _r in out["findings_closed"]],
                         [finding["id"]])

    def test_a_carried_finding_stays_open_off_the_carrying_round(self):  # noqa: VACUOUS_ASSERTION — the hold is asserted recorded at its tip, and the arm above closes the same carried shape at a tip past the carrying round
        # a tip off b descends from the FIX that filed the finding, not from
        # the FIX at c that found it still there
        self.git("checkout", "-q", "-b", "off-b", self.b)
        off = self.commit("off the first round")
        self.git("checkout", "-q", self.main)
        finding, third = self.carried_then_round(off)
        out = self.hold(third, off)
        self.assertNotIn("findings_closed", out)
        self.assertEqual(tasks.rows()[finding["id"]]["status"], "open")

    def test_a_retracted_FIX_closes_the_findings_it_filed(self):
        first = self.row()
        self.library_fix(first, findings=["the retry drops the lock"])
        kept = self.found_in(first["id"])[0]
        second = self.row(ref=self.c, task=False, supersedes=first["id"])
        self.library_fix(second, findings=["the cap is off by one"],
                         carried=[kept["id"]])
        filed = self.found_in(second["id"])[0]
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": REVIEWER}):
            out, why = dispatches.retract(
                second["id"], "the verdict read the wrong tree",
                "source-clean", "measured", notify=False)
        self.assertIsNone(why, why)
        reason = "retracted (row %s)" % second["id"][:12]
        self.assertEqual(out["findings_closed"], [(filed["id"], reason)])
        self.assertEqual(tasks.rows()[filed["id"]]["closed_reason"], reason)
        # the finding it only carried stays with the round that filed it
        self.assertEqual(tasks.rows()[kept["id"]]["status"], "open")


    def test_a_retracted_FIX_keeps_a_finding_a_live_round_carried(self):  # noqa: VACUOUS_ASSERTION — the open finding is paired with test_a_retracted_FIX_closes_the_findings_it_filed, the same source-clean retraction closing what it filed
        first = self.row()
        self.library_fix(first, findings=["the retry drops the lock"])
        filed = self.found_in(first["id"])[0]
        second = self.row(ref=self.c, task=False, supersedes=first["id"])
        self.library_fix(second, carried=[filed["id"]])
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": REVIEWER}):
            out, why = dispatches.retract(
                first["id"], "the verdict read the wrong tree",
                "source-clean", "measured", notify=False)
        self.assertIsNone(why, why)
        self.assertNotIn("findings_closed", out)
        self.assertEqual(tasks.rows()[filed["id"]]["status"], "open")

    def test_a_retraction_that_reads_fix_keeps_the_findings_open(self):  # noqa: VACUOUS_ASSERTION — each reading asserts its kept-open line exactly, and the source-clean control after the loop asserts its close exactly
        """task/3862 M1: a retraction that still reads FIX (or supersede, or
        unknown) says the work stands, so the findings the FIX filed stay
        open, and it says so in one line naming how to carry them."""
        for reads in ("fix", "supersede", "unknown"):
            with self.subTest(reads=reads):
                row = self.row()
                self.library_fix(row, findings=["the retry drops the lock "
                                                "(%s)" % reads])
                filed = self.found_in(row["id"])[0]
                out = self.retract(row, reads)
                self.assertEqual(self.status(filed["id"]), "open")
                self.assertNotIn("findings_closed", out)
                line = ("kept open: the retraction reads %s; carry them with "
                        "--finding-carried (%s)" % (reads, filed["id"]))
                self.assertEqual(out.get("findings_close_errors"), [line])
                self.assertIn("finding NOT closed — " + line,
                              review_findings.closed_lines(out))
        # CONTROL: a FIX retracted as source-clean closes its finding
        row = self.row()
        self.library_fix(row, findings=["the cap is off by one"])
        filed = self.found_in(row["id"])[0]
        out = self.retract(row)
        self.assertEqual(out["findings_closed"], [
            (filed["id"], "retracted (row %s)" % row["id"][:12])])


class ReviewChainsNameTheirTaskTest(FindingsBase):
    """The common writer refuses a new homeless review, not a later verdict."""

    def test_add_and_send_refuse_no_task_and_take_explicit_task(self):  # noqa: VACUOUS_ASSERTION — rejected calls must leave the preseeded snapshot unchanged; later explicit-task calls assert both positive rows
        seeded = self.row()
        before = sorted(dispatches.snapshot()[0])
        self.assertIn(seeded["id"], before)
        for verb in ("add", "send"):
            with self.subTest(verb=verb):
                if verb == "add":
                    row, err = dispatches.add(
                        REVIEWER, "unclaimed-lane", ref=self.b, repo=self.repo,
                        kind="review", new_work=True, notify=False,
                        _reason=True)
                else:
                    with mock.patch.object(seats, "dm",
                                           return_value=({"id": "dm-1"}, None)):
                        row, err, _ = dispatches.send(
                            REVIEWER, "unclaimed-lane", "read this tip", self.b,
                            repo=self.repo, kind="review", new_work=True,
                            force=True)
                self.assertIsNone(row)
                for remedy in ("--task task/N --part", "--whole",
                               "helm work claim <lane> --task task/N --part",
                               "helm task add <title> --project P"):
                    self.assertIn(remedy, err)
                self.assertEqual(sorted(dispatches.snapshot()[0]), before)
        row, err = dispatches.add(
            REVIEWER, "unclaimed-lane", ref=self.b, repo=self.repo,
            kind="review", new_work=True, notify=False, task=self.tid,
            _reason=True)
        self.assertIsNone(err, err)
        self.assertEqual(row["task"], self.tid)
        with mock.patch.object(seats, "dm",
                               return_value=({"id": "dm-1"}, None)):
            sent, err, _ = dispatches.send(
                REVIEWER, "second-unclaimed-lane", "read this tip", self.b,
                repo=self.repo, kind="review", new_work=True,
                task=self.tid, force=True)
        self.assertIsNone(err, err)
        self.assertEqual(sent["task"], self.tid)

    def test_missing_task_remedy_runs_at_the_cli_with_either_path(self):  # noqa: VACUOUS_ASSERTION — two fixed cases each assert a persisted row, then the unconditional ledger equality pins both rows
        expected = {}
        for verb, choice in (("add", "--part"), ("send", "--whole")):
            with self.subTest(verb=verb, choice=choice):
                task, why = tasks.add("remedy %s" % verb, "integrator",
                                      project=PROJECT, force_new=True)
                self.assertIsNone(why, why)
                lane = "remedy-" + verb
                args = [verb, REVIEWER, lane]
                if verb == "send":
                    args.append("read this tip")
                args += ["--ref", self.b, "--repo", self.repo, "--kind",
                         "review", "--new-work", "--task", task["id"], choice]
                with mock.patch.object(seats, "dm",
                                       return_value=({"id": "dm-1"}, None)):
                    rc, out, err = run(dispatches.cmd_dispatch, args)
                self.assertEqual(rc, 0, out + err)
                made = [r for r in dispatches.snapshot()[0].values()
                        if r.get("lane") == lane]
                self.assertEqual(len(made), 1, made)
                self.assertEqual(made[0]["task"], task["id"])
                expected[lane] = task["id"]
        self.assertEqual({r["lane"]: r["task"] for r in
                          dispatches.snapshot()[0].values()}, expected)

    def test_force_and_a_task_shaped_lane_do_not_exempt_a_review(self):
        row, err = dispatches.add(
            REVIEWER, "task-999999", ref=self.b, repo=self.repo,
            kind="review", new_work=True, notify=False, force=True,
            _reason=True)
        self.assertIsNone(row)
        self.assertIn("--task task/N", err)
        sent, err, _ = dispatches.send(
            REVIEWER, "undelivered-fake", "read this tip", self.b,
            repo=self.repo, kind="review", new_work=True, force=True,
            _relay_notice=True)
        self.assertIsNone(sent)
        self.assertIn("internal relay mint", err)
        with mock.patch.object(seats, "dm",
                               return_value=({"id": "dm-1"}, None)):
            relay, err, _ = dispatches.send(
                REVIEWER, "undelivered-fixture", "the original review was "
                "not delivered", self.b, repo=self.repo, kind="review",
                new_work=True, force=True,
                _relay_notice=dispatches._RELAY_NOTICE_MINT)
        self.assertIsNone(err, err)
        self.assertIsNone(relay.get("task"))


class AChainWithNoTaskRefusesFindingsTest(FindingsBase):
    """D4 — an old no-task chain cannot file top-level findings."""

    def test_no_task_chain_refuses_without_appending_a_verdict(self):
        row = self.row(task=False)
        before = sorted(tasks.rows())
        self.assertIn(self.tid, before)
        rc, _out, err = self.verdict(row, "--finding",
                                     "the retry drops the lock",
                                     "--note", "the naming reads well")
        self.assertEqual(rc, 1, err)
        self.assertIn("--task task/N --part", err)
        self.assertIn("helm work claim <lane> --task task/N --part", err)
        self.assertEqual(self.state(row["id"])["status"], "open")
        self.assertEqual(sorted(tasks.rows()), before)

    def test_a_homeless_task_refuses_before_append_and_on_replay(self):
        row = self.row()
        with mock.patch.object(tasks, "project_of_row", return_value=None):
            fields, err = review_findings.plan(
                row, {row["id"]: row}, "fix",
                {"findings": ["the retry drops the lock"]})
            self.assertIsNone(fields)
            self.assertIn("project", err)
            report = review_findings.file(
                {**row, "findings": ["the retry drops the lock"],
                 "findings_task": self.tid}, {row["id"]: row})
            self.assertIn("project", "; ".join(report["errors"]))
        known = tasks.rows()
        self.assertIn(self.tid, known)
        self.assertEqual([r for r in known.values()
                          if r.get("found_in") == row["id"]], [])

    def test_a_chain_with_a_task_prints_no_such_line(self):  # noqa: VACUOUS_ASSERTION — the absent line is paired with the finding filed as a sub-task on the same output
        row = self.row()
        rc, out, err = self.verdict(row, "--finding",
                                    "the retry drops the lock")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("helm dispatch: no task:", out)
        self.assertEqual(self.found_in(row["id"])[0]["continues"], self.tid)

    def test_unreadable_chain_refuses_instead_of_filing_top_level(self):  # noqa: VACUOUS_ASSERTION — the readable no-task chain files a real top-level row as the positive control
        row = self.row()
        missing = {**row, "chain_root": "a" * 32}
        fields, err = review_findings.plan(
            missing, {row["id"]: missing}, "fix",
            {"findings": ["the retry drops the lock"]})
        self.assertIsNone(fields)
        self.assertIn("UNKNOWN or unreadable", err)
        absent_task = {**row, "task": "task/999999"}
        fields, err = review_findings.plan(
            absent_task, {row["id"]: absent_task}, "fix",
            {"findings": ["the retry drops the lock"]})
        self.assertIsNone(fields)
        self.assertIn("not in the task ledger", err)
        # A readable old no-task chain must not mint a top-level finding.
        root = self.row(task=False)
        fields, err = review_findings.plan(
            root, {root["id"]: root}, "fix",
            {"findings": ["the retry drops the lock"]})
        self.assertIsNone(fields)
        self.assertIn("--task task/N --part", err)
        self.assertIn("helm work claim <lane> --task task/N --part", err)
        report = review_findings.file(
            {**root, "findings": ["the retry drops the lock"]},
            {root["id"]: root})
        self.assertIn("--task task/N --part", "; ".join(report["errors"]))
        self.assertEqual(self.found_in(root["id"]), [])

    def test_unreadable_chain_after_verdict_refuses_the_filing_retry(self):
        first = self.row()
        second = self.row(ref=self.c, supersedes=first["id"])
        current = {first["id"]: first, second["id"]: second}
        fields, err = review_findings.plan(
            second, current, "fix", {"findings": ["the retry drops the lock"]})
        self.assertIsNone(err, err)
        self.assertEqual(fields["findings_task"], self.tid)
        verdict = {**second, **fields, "reviewed_tip": self.c}
        report = review_findings.file(verdict, {second["id"]: verdict})
        self.assertIn("UNKNOWN or unreadable", "; ".join(report["errors"]))
        self.assertEqual(self.found_in(second["id"]), [])
        report = review_findings.file(verdict, current)
        self.assertEqual(report["errors"], [])
        self.assertEqual(self.found_in(second["id"])[0]["continues"], self.tid)


class ATasklessChainAttachedToATaskTest(FindingsBase):
    """D5 — task/4000: `helm dispatch attach-task <chain> --task task/N`
    records, as ONE NEW ledger event on the chain's FIRST row, that a chain
    minted with no task now serves task/N — so a LATER round of that chain
    files and carries its findings under it. The chain's history, its rows
    already verdicted, the findings they already filed and the pair room it
    already opened are never rewritten.

    THE MEASURED CASE (the simbi review stuck on this, task/4000's note):
    chain 89dea44a1662 names no task; its findings task/3913-3917 record
    found_chain = that chain and now continue story task/3993; a new --task
    chain refuses to carry them ("found in another chain"). These arms build
    that exact shape on a scratch ledger: a taskless root, a FIX filing five
    findings, the pair room opened, a successor round — then the attach.

    FAIL CLOSED, in the task note's own words: the verb refuses when the
    chain already names a DIFFERENT task, when the named task is closed,
    when the caller is neither the chain's author nor the integrator, when
    the attach already happened, and when either ledger cannot be read."""

    FINDINGS = ("the retry drops the lock", "the cap is off by one",
                "the door reads the stale snapshot", "the note loses its tip",
                "the carry cites a stranger's chain")

    def row(self, ref=None, task=True, **kw):
        if task is False and "supersedes" not in kw:
            kw.setdefault("recipient", REVIEWER)
            return self.legacy_taskless_review(ref=ref or self.b, **kw)
        return super().row(ref=ref, task=task, **kw)

    def simbi_case(self):
        """The taskless chain of the measured case: root, FIX with five
        findings filed top-level, its pair room opened, a successor round.
        -> (root, successor, findings); the findings then continue the task
        the attach will name, exactly as task/3913-3917 continue task/3993."""
        from helm import review_door
        root = self.row(task=False)
        out, why = dispatches.mark_verdict(root["id"], root["tip"],
                                           "historical review", polarity="fix")
        self.assertIsNone(why, why)
        # Before the no-task filing door, this FIX named five top-level rows.
        # Build that FROZEN historical event below today's refusing writer;
        # only later-round filing goes through today's real verdict door.
        path = dispatches.ledger_path()
        events = td.eventledger.events(path)
        with open(path, "w", encoding="utf-8") as fh:
            for event in events:
                if event.get("id") == root["id"] and event.get("event") == "verdict":
                    event.update(findings=list(self.FINDINGS), finding_count=5)
                fh.write(td.json.dumps(event, separators=(",", ":")) + "\n")
        for text in self.FINDINGS:
            finding, why = tasks.add(text, root["sender"],
                                     source=REVIEWER, origin="agent",
                                     project=PROJECT, force_new=True,
                                     found_in=root["id"],
                                     found_chain=self.chain(root))
            self.assertIsNone(why, why)
        found = self.found_in(root["id"])
        self.assertEqual([f["title"] for f in found], list(self.FINDINGS))
        self.assertTrue(all(f["continues"] is None for f in found))
        # THE PAIR ROOM, OPENED: the legacy chain-keyed room the chain's
        # rounds already talk in, which the attach must never rename under
        # them (`review_door.task_room`: an opened legacy room keeps for
        # life).
        room, key = review_door.pair_room(root)
        self.assertTrue(key.startswith("chain/"), key)
        from helm import chat
        os.makedirs(os.path.dirname(chat.room_path(room)), exist_ok=True)
        with open(chat.room_path(room), "a", encoding="utf-8") as fh:
            fh.write("")
        successor = self.row(ref=self.c, task=False, supersedes=root["id"])
        return root, successor, found, room

    def attach_target(self, found):
        """The task the attach names, and the measured continuation: the
        five findings continue it, as task/3913-3917 continue task/3993."""
        task, why = tasks.add("the story the findings continue",
                              "integrator", project=PROJECT, force_new=True)
        self.assertIsNone(why, why)
        for f in found:
            row, why = tasks.update(f["id"], continues=task["id"])
            self.assertIsNone(why, why)
        return task

    def attach(self, row, tid):
        return dispatches.attach_task(
            row["id"] if isinstance(row, dict) else row, tid)

    def test_attach_lets_later_rounds_carry_and_file_under_the_task(self):  # noqa: VACUOUS_ASSERTION — the absent `task` key and the unchanged task rows are paired with the attached_task projection, the successor's filing under it, and the finding count asserted on the same observables
        root, successor, found, room = self.simbi_case()
        task = self.attach_target(found)
        before = {r["id"]: dict(r) for r in tasks.rows().values()}
        out, why = self.attach(root, task["id"])
        self.assertIsNone(why, why)
        # THE ATTACH IS ONE EVENT ON THE CHAIN'S FIRST ROW, and the row's
        # own record is untouched: no `task` key was minted onto history,
        # and the five findings keep their original filing.
        state = self.state(root["id"])
        self.assertNotIn("task", state)
        self.assertEqual(state["attached_task"], task["id"])
        # The opener held by a caller is pre-attach; the validated CURRENT
        # projection, not that immutable old dict, decides subsequent filing.
        self.assertEqual(review_findings.chain_task(
            root, dispatches.snapshot()[0], tasks.rows()), (task["id"], None))
        after = {r["id"]: dict(r) for r in tasks.rows().values()}
        self.assertEqual(set(before), set(after),
                         "the attach filed nothing and refiled nothing")
        for tid in before:
            self.assertEqual({k: v for k, v in after[tid].items()
                              if k != "comments"},
                             {k: v for k, v in before[tid].items()
                              if k != "comments"})
        # THE PAIR ROOM THE CHAIN OPENED IS THE ROOM IT KEEPS.
        from helm import review_door
        self.assertEqual(review_door.pair_room(root)[0], room)
        self.assertEqual(review_door.pair_room(successor)[0], room)
        # A LATER ROUND CARRIES the chain's own findings under the task,
        # and files a new one beside them — the carry that the new --task
        # chain of the measured case refused as "found in another chain".
        rc, out, err = self.verdict(
            successor, "--finding-carried", found[0]["id"],
            "--finding", "the hand-off drops the reason")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.state(successor["id"])["findings_task"],
                         task["id"])
        added = self.found_in(successor["id"])
        self.assertEqual([a["title"] for a in added],
                         ["the hand-off drops the reason"])
        self.assertEqual(added[0]["continues"], task["id"])
        replay = review_findings.file(self.state(successor["id"]))
        self.assertEqual(replay["errors"], [])
        self.assertEqual(replay["task"], task["id"])
        self.assertEqual([pair[0] for pair in replay["filed"]],
                         [added[0]["id"]])
        titles = sorted(r["title"] for r in tasks.rows().values()
                        if r.get("found_in"))
        self.assertEqual(titles, sorted(list(self.FINDINGS)
                                        + ["the hand-off drops the reason"]))

    def test_forged_opener_attach_cannot_file_but_a_real_attach_can(self):
        root = self.row(task=False)
        self.forge_open(root["id"], attached_task=self.tid,
                        attached_task_by="integrator",
                        attached_task_ts="2026-10-01T00:00:00Z",
                        attach_role="integrator")
        rc, out, err = self.verdict(root, "--finding", "forged filing")
        self.assertEqual(rc, 1)
        self.assertIn("this review chain names no task", err)
        self.assertEqual(self.state(root["id"])["status"], "open")
        self.assertEqual(self.found_in(root["id"]), [])
        attached, why = self.attach(root, self.tid)
        self.assertIsNone(why, why)
        self.assertEqual(self.state(root["id"])["attached_task"], self.tid)
        rc, out, err = self.verdict(root, "--finding", "real attached filing")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.found_in(root["id"])[0]["continues"], self.tid)

    def test_attachment_without_an_opened_chain_room_uses_task_room(self):
        from helm import review_door
        root = self.row(task=False)
        successor = self.row(ref=self.c, task=False, supersedes=root["id"])
        attached, why = self.attach(root, self.tid)
        self.assertIsNone(why, why)
        room, key = review_door.pair_room(successor)
        self.assertEqual(key, self.tid)
        self.assertEqual(room, review_door.task_room(
            review_door.pair_scope(successor), self.tid))

    def test_missing_current_root_cannot_file_or_plan_a_verdict(self):
        root = self.row(task=False)
        attached, why = self.attach(root, self.tid)
        self.assertIsNone(why, why)
        projected = self.state(root["id"])
        title = "the missing root must not file work"
        fields, why = review_findings.plan(
            projected, {}, "fix", {"findings": [title]})
        self.assertIsNone(fields)
        self.assertIn("UNKNOWN or unreadable", why)
        report = review_findings.file(
            {**projected, "findings": [title], "findings_task": self.tid}, {})
        self.assertIn("UNKNOWN or unreadable", "; ".join(report["errors"]))
        self.assertEqual(self.found_in(root["id"]), [])
        self.assertEqual(self.state(root["id"])["status"], "open")
        fields, why = review_findings.plan(
            root, dispatches.snapshot()[0], "fix", {"findings": [title]})
        self.assertIsNone(why, why)
        self.assertEqual(fields["findings_task"], self.tid)
        rc, out, err = self.verdict(root, "--finding", title)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.found_in(root["id"])[0]["continues"], self.tid)

    def test_unreadable_or_contradictory_attachment_grants_no_filing_task(self):
        root = self.row(task=False)
        attached, why = self.attach(root, self.tid)
        self.assertIsNone(why, why)
        current = dispatches.snapshot()[0]
        known = tasks.rows()
        with mock.patch.object(dispatches, "snapshot",
                               return_value=(None, "PermissionError")):
            task, why = review_findings.chain_task(root, None, known)
        self.assertIsNone(task)
        self.assertIn("dispatch ledger could not be read", why)
        other, why = tasks.add("contradictory task", "integrator",
                               project=PROJECT, force_new=True)
        self.assertIsNone(why, why)
        contradictory = dict(current[root["id"]], task=other["id"])
        task, why = review_findings.chain_task(
            root, dict(current, **{root["id"]: contradictory}), known)
        self.assertIsNone(task)
        self.assertIn("contradictory", why)
        self.assertEqual(review_findings.chain_task(root, current, known),
                         (self.tid, None))

    def test_a_chain_that_names_a_task_refuses(self):  # noqa: VACUOUS_ASSERTION — the absent attached_task is paired with the positive control attach on the same observable in the same test
        row = self.row()
        out, why = self.attach(row, self.tid)
        self.assertIsNone(out)
        self.assertIn("already names", why)
        # THE SAME REFUSAL AT THE WRONG TASK: the row names self.tid, and
        # attaching another task is not a quieter spelling of the same ask.
        other, why2 = tasks.add("other work", "integrator",
                                project=PROJECT, force_new=True)
        self.assertIsNone(why2, why2)
        out, why = self.attach(row, other["id"])
        self.assertIsNone(out)
        self.assertIn("already names", why)
        self.assertNotIn("attached_task", self.state(row["id"]))
        # POSITIVE CONTROL, same observable: a taskless row of the same
        # fixture attaches and projects the field the refusals never set.
        root = self.row(task=False)
        out, why = self.attach(root, other["id"])
        self.assertIsNone(why, why)
        self.assertEqual(self.state(root["id"])["attached_task"],
                         other["id"])

    def test_a_chain_citing_a_task_literally_refuses(self):  # noqa: VACUOUS_ASSERTION — the absent attached_task is paired with the uncited control attach on the same observable
        # A LEGACY chain: today's mint stores a cited task as the row's own
        # `task`, so the fixture forges the pre-store shape — no `task` key,
        # a note that cites one — which `taskkey.join` answers LITERALLY.
        cited = self.row(task=False, note="review of %s" % self.tid)
        self.forge_open(cited["id"], task=None)
        self.assertFalse(self.state(cited["id"]).get("task"))
        other, why = tasks.add("other work", "integrator",
                               project=PROJECT, force_new=True)
        self.assertIsNone(why, why)
        out, why = self.attach(cited, other["id"])
        self.assertIsNone(out)
        self.assertIn("already serves %s" % self.tid, why)
        self.assertNotIn("attached_task", self.state(cited["id"]))
        root = self.row(task=False)
        out, why = self.attach(root, other["id"])
        self.assertIsNone(why, why)
        self.assertEqual(self.state(root["id"])["attached_task"], other["id"])

    def test_a_second_attach_refuses(self):  # noqa: VACUOUS_ASSERTION — the refusal arms run after the first attach's projection is asserted on the same field
        root = self.row(task=False)
        out, why = self.attach(root, self.tid)
        self.assertIsNone(why, why)
        self.assertEqual(self.state(root["id"])["attached_task"], self.tid)
        # THE EXACT RETRY INCLUDED: the chain's task is a fact the first
        # event recorded, and a second event would say the fact twice — or
        # worse, say it differently. Unlike a retip retry there is no lost
        # response to reconcile: the refusal NAMES the standing attach.
        out, why = self.attach(root, self.tid)
        self.assertIsNone(out)
        self.assertIn("already attached", why)
        other, why2 = tasks.add("other work", "integrator",
                                project=PROJECT, force_new=True)
        self.assertIsNone(why2, why2)
        out, why = self.attach(root, other["id"])
        self.assertIsNone(out)
        self.assertIn("already attached", why)
        self.assertEqual(self.state(root["id"])["attached_task"], self.tid)

    def test_a_closed_task_refuses(self):  # noqa: VACUOUS_ASSERTION — the absent attached_task is paired with the open-task control attach on the same observable in the same test
        done, why = tasks.add("finished work", "integrator",
                              project=PROJECT, force_new=True)
        self.assertIsNone(why, why)
        row, why = tasks.update(done["id"], status="closed",
                                closed_reason="done")
        self.assertIsNone(why, why)
        root = self.row(task=False)
        out, why = self.attach(root, done["id"])
        self.assertIsNone(out)
        self.assertIn("closed", why)
        self.assertNotIn("attached_task", self.state(root["id"]))
        # POSITIVE CONTROL: an OPEN task attaches to the same chain shape.
        out, why = self.attach(root, self.tid)
        self.assertIsNone(why, why)
        self.assertEqual(self.state(root["id"])["attached_task"], self.tid)

    def test_a_task_not_in_the_ledger_refuses(self):  # noqa: VACUOUS_ASSERTION — the absent attached_task is paired with the known-task control attach on the same observable in the same test
        root = self.row(task=False)
        out, why = self.attach(root, "task/999999")
        self.assertIsNone(out)
        self.assertIn("not in the task ledger", why)
        self.assertNotIn("attached_task", self.state(root["id"]))
        out, why = self.attach(root, self.tid)
        self.assertIsNone(why, why)
        self.assertEqual(self.state(root["id"])["attached_task"], self.tid)

    def test_a_caller_who_is_neither_author_nor_integrator_refuses(self):  # noqa: VACUOUS_ASSERTION — the absent attached_task is paired with the author control attach on the same observable in the same test
        root = self.row(task=False)
        self.assertEqual(root["sender"], "integrator")
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "seat-else"}):
            out, why = self.attach(root, self.tid)
        self.assertIsNone(out)
        self.assertIn("AUTHOR", why)
        self.assertNotIn("attached_task", self.state(root["id"]))
        # POSITIVE CONTROL: the chain's author attaches the same task.
        out, why = self.attach(root, self.tid)
        self.assertIsNone(why, why)
        self.assertEqual(self.state(root["id"])["attached_task"], self.tid)

    def test_the_integrator_attaches_another_seats_chain(self):  # noqa: VACUOUS_ASSERTION — the stranger's absent attached_task is paired with the integrator's attach asserted on the same field first
        from helm import seats_roster
        # A POPULATED roster flips the recipient door from UNKNOWN-proceed to
        # named-seats-only (DispatchBase keeps it empty on purpose), so the
        # chain's recipient and its author join the integrator in it. The
        # integrator seat is a FIXTURE name through the override seam, never
        # the fleet's real one.
        seats_roster.write_roster("seat-integ", session="sess-integ")
        seats_roster.write_roster(REVIEWER, session="sess-rev")
        seats_roster.write_roster("seat-author", session="sess-author")
        env = {"HELM_INTEGRATOR_SEAT": "seat-integ"}
        with mock.patch.dict(os.environ, dict(env, HELM_CHAT_NAME="seat-author")):
            root = self.row(task=False)
        self.assertEqual(root["sender"], "seat-author")
        with mock.patch.dict(os.environ, dict(env, HELM_CHAT_NAME="seat-integ")):
            out, why = self.attach(root, self.tid)
        self.assertIsNone(why, why)
        self.assertEqual(self.state(root["id"])["attached_task"], self.tid)
        self.assertEqual(self.state(root["id"])["attached_task_by"],
                         "seat-integ")
        self.assertEqual(self.state(root["id"])["attach_role"], "integrator")
        # A STRANGER STILL REFUSES over the same chain shape (the control
        # run second, against the attached state, reads its refusal off the
        # one field the integrator's attach set).
        other = self.row(task=False, ref=self.c)
        with mock.patch.dict(os.environ, dict(env, HELM_CHAT_NAME="seat-else")):
            out, why = self.attach(other, self.tid)
        self.assertIsNone(out)
        self.assertIn("AUTHOR", why)
        self.assertNotIn("attached_task", self.state(other["id"]))

    def test_an_unreadable_dispatch_ledger_refuses(self):  # noqa: VACUOUS_ASSERTION — the refusal under a mocked unreadable snapshot is paired with the same attach succeeding on the same observable
        root = self.row(task=False)
        with mock.patch.object(dispatches, "snapshot",
                               return_value=(None, "PermissionError")):
            out, why = self.attach(root, self.tid)
        self.assertIsNone(out)
        self.assertIn("unavailable", why)
        # POSITIVE CONTROL: the same attach succeeds with the ledger read.
        out, why = self.attach(root, self.tid)
        self.assertIsNone(why, why)
        self.assertEqual(self.state(root["id"])["attached_task"], self.tid)

    def test_an_unreadable_task_ledger_refuses(self):  # noqa: VACUOUS_ASSERTION — the refusal under a mocked unreadable task snapshot is paired with the same attach succeeding on the same observable
        root = self.row(task=False)
        with mock.patch.object(tasks, "snapshot",
                               return_value=(None, "PermissionError")):
            out, why = self.attach(root, self.tid)
        self.assertIsNone(out)
        self.assertIn("task ledger", why)
        self.assertNotIn("attached_task", self.state(root["id"]))
        out, why = self.attach(root, self.tid)
        self.assertIsNone(why, why)
        self.assertEqual(self.state(root["id"])["attached_task"], self.tid)

    def test_an_unknown_or_unreadable_chain_refuses(self):  # noqa: VACUOUS_ASSERTION — the UNKNOWN-chain refusal is paired with the healthy-chain control attach on the same observable in the same test
        out, why = self.attach("f" * 32, self.tid)
        self.assertIsNone(out)
        self.assertIn("no such dispatch", why)
        # A chain whose FIRST row is unreadable is UNKNOWN, never taskless:
        # the attach cannot prove the chain names no task.
        root = self.row(task=False)
        second = self.row(ref=self.c, task=False, supersedes=root["id"])
        self.forge_open(second["id"], chain_root="a" * 32)
        out, why = self.attach(second["id"], self.tid)
        self.assertIsNone(out)
        self.assertIn("UNKNOWN", why)
        # POSITIVE CONTROL on the same observable: the healthy chain of the
        # same fixture attaches.
        other = self.row(task=False)
        out, why = self.attach(other["id"], self.tid)
        self.assertIsNone(why, why)
        self.assertEqual(self.state(other["id"])["attached_task"], self.tid)

    def test_the_cli_spelling(self):  # noqa: VACUOUS_ASSERTION — the duplicate's refusal line is paired with the first attach's success line and projected field on the same observables
        root = self.row(task=False)
        rc, out, err = run(dispatches.cmd_dispatch, [
            "attach-task", root["id"], "--task", self.tid])
        self.assertEqual(rc, 0, err)
        self.assertIn(self.tid, out)
        self.assertEqual(self.state(root["id"])["attached_task"], self.tid)
        rc, out, err = run(dispatches.cmd_dispatch, [
            "attach-task", root["id"], "--task", self.tid])
        self.assertEqual(rc, 1)
        self.assertIn("already attached", err)


class EachCloserAndTheFilerKeepTheStoryWholeTest(ClosersBase):
    """The closers and the filer keep the story's invariants: a lane's own
    task is never closed as a finding, a finding with open sub-tasks stays
    open, a carried finding closes with the last FIX that named it,
    no open row is born under a closed parent, a repeat of a hold repairs
    the close it missed, one finding is filed once however many filers race,
    and a refused line keeps the work a model run's read or a hold named."""

    def test_a_hold_never_closes_the_lane_s_own_task(self):  # noqa: VACUOUS_ASSERTION — the open task is paired with the chain's other finding closed exactly by the same hold
        first = self.row(task=False)
        self.library_fix(first, findings=["the lane took this finding",
                                          "the cap is off by one"])
        by_title = {k["title"]: k for k in self.found_in(first["id"])}
        own = by_title["the lane took this finding"]
        other = by_title["the cap is off by one"]
        self.git("branch", "lane/" + first["lane"], first["tip"])
        _wrote, why = taskkey.record_lane(self.repo, first["lane"], own["id"])
        self.assertIsNone(why, why)
        second = self.row(ref=self.c, task=False, supersedes=first["id"])
        out = self.hold(second, self.c)
        self.assertEqual(self.status(own["id"]), "open")
        # CONTROL: the chain's other finding the same hold answers closes
        self.assertEqual([t for t, _r in out["findings_closed"]],
                         [other["id"]])

    def test_a_retraction_never_closes_the_lane_s_own_task(self):  # noqa: VACUOUS_ASSERTION — the open task is paired with the chain's other finding closed exactly by the same retraction
        first = self.row(task=False)
        self.library_fix(first, findings=["the lane took this finding",
                                          "the cap is off by one"])
        by_title = {k["title"]: k for k in self.found_in(first["id"])}
        own = by_title["the lane took this finding"]
        other = by_title["the cap is off by one"]
        self.git("branch", "lane/" + first["lane"], first["tip"])
        _wrote, why = taskkey.record_lane(self.repo, first["lane"], own["id"])
        self.assertIsNone(why, why)
        out = self.retract(first)
        self.assertEqual(self.status(own["id"]), "open")
        # CONTROL: the chain's other finding the same retraction answers
        # closes
        reason = "retracted (row %s)" % first["id"][:12]
        self.assertEqual(out["findings_closed"], [(other["id"], reason)])

    def test_a_numbered_live_lane_keeps_its_unrecorded_finding_on_hold_and_retraction(self):  # noqa: VACUOUS_ASSERTION — both fixed subtest arms assert another finding closes through the same closer
        for closer in ("hold", "retraction"):
            with self.subTest(closer=closer):
                first = self.row(task=False)
                number = "698000" if closer == "hold" else "699999"
                mine, why = tasks.add("numbered lane work " + closer,
                                      "integrator", tid=number, force_new=True,
                                      found_in=first["id"],
                                      found_chain=self.chain(first))
                self.assertIsNone(why, why)
                self.library_fix(first, findings=["other work " + closer])
                other = next(r for r in self.found_in(first["id"])
                             if r["id"] != mine["id"])
                self.git("branch", "lane/review-" + number, self.b)
                if closer == "hold":
                    second = self.row(ref=self.c, task=False,
                                      supersedes=first["id"])
                    out = self.hold(second, self.c)
                else:
                    out = self.retract(first)
                self.assertEqual(self.status(mine["id"]), "open")
                self.assertEqual([t for t, _r in out["findings_closed"]],
                                 [other["id"]])

    def test_unreadable_first_row_blocks_hold_and_retraction_closers(self):
        first = self.row(task=False)
        self.library_fix(first, findings=["the retry drops the lock"])
        found = self.found_in(first["id"])[0]
        second = self.row(ref=self.c, task=False, supersedes=first["id"])
        missing = {**second, "chain_root": first["id"],
                   "findings_carried": [found["id"]],
                   "source_clean_tip": self.c,
                   "retract_reads": "source-clean"}
        current = {second["id"]: missing}
        own, why = review_findings.lane_tasks(missing, current)
        self.assertEqual(own, set())
        self.assertIn("UNKNOWN or unreadable", why)
        closed, errors = review_findings.close_on_hold(missing, current)
        self.assertEqual(closed, [])
        self.assertIn("nothing was closed", "; ".join(errors))
        closed, errors = review_findings.close_retracted(missing, current)
        self.assertEqual(closed, [])
        self.assertIn("nothing was closed", "; ".join(errors))
        self.assertEqual(self.status(found["id"]), "open")

    def test_retracting_an_unrelated_carrier_keeps_the_finding_open(self):
        first = self.row(task=False)
        self.library_fix(first, findings=["the retry drops the lock"])
        found = self.found_in(first["id"])[0]
        other = self.row(task=False)
        self.library_fix(other, findings=["the other chain's own finding"])
        theirs = self.found_in(other["id"])[0]
        carrier = {**self.state(other["id"]),
                   "findings_carried": [found["id"]],
                   "retract_reads": "source-clean"}
        current = {first["id"]: {**self.state(first["id"]),
                                 "verdict_retracted": True},
                   other["id"]: carrier}
        closed, errors = review_findings.close_retracted(carrier, current)
        self.assertEqual(errors, [])
        self.assertEqual([tid for tid, _reason in closed], [theirs["id"]])
        self.assertEqual(self.status(found["id"]), "open")

    def test_a_finding_with_an_open_sub_task_stays_open(self):
        first = self.row()
        self.library_fix(first, findings=["the retry drops the lock"])
        finding = self.found_in(first["id"])[0]
        child, why = tasks.add("the retry's own follow-on", "integrator",
                               project=PROJECT, continues=finding["id"],
                               force_new=True)
        self.assertIsNone(why, why)
        second = self.row(ref=self.c, task=False, supersedes=first["id"])
        out = self.hold(second, self.c)
        self.assertEqual(self.status(finding["id"]), "open")
        self.assertEqual(self.status(child["id"]), "open")
        self.assertIn("open sub-task", "; ".join(
            out.get("findings_close_errors") or ()))

    def test_retracting_the_filer_then_the_carrier_closes_the_finding(self):
        first = self.row()
        self.library_fix(first, findings=["the retry drops the lock"])
        filed = self.found_in(first["id"])[0]
        second = self.row(ref=self.c, task=False, supersedes=first["id"])
        self.library_fix(second, carried=[filed["id"]])
        self.retract(first)
        self.assertEqual(self.status(filed["id"]), "open")
        out = self.retract(second)
        reason = "retracted (row %s)" % second["id"][:12]
        self.assertEqual(out["findings_closed"], [(filed["id"], reason)])
        self.assertEqual(tasks.rows()[filed["id"]]["closed_reason"], reason)

    def test_a_retry_files_no_open_finding_under_a_parent_closed_since(self):  # noqa: VACUOUS_ASSERTION — the absent child is paired with the retry's report naming the closed parent
        row = self.row()
        with mock.patch.object(tasks, "add", return_value=(
                None, "task ledger is not writable")):
            self.library_fix(row, findings=["the retry drops the lock"])
        _row, why = tasks.close(self.tid, "the owner closed it")
        self.assertIsNone(why, why)
        again = self.library_fix(row, findings=["the retry drops the lock"])
        self.assertEqual([k for k in self.found_in(row["id"])
                          if k["status"] in tasks.OPEN_STATUSES], [])
        self.assertIn("%s is closed" % self.tid,
                      "; ".join(again["findings_report"]["errors"]))

    def test_the_task_door_refuses_an_open_row_under_a_closed_parent(self):
        _row, why = tasks.close(self.tid, "the owner closed it")
        self.assertIsNone(why, why)
        got, why = tasks.add("a follow-on", "integrator", project=PROJECT,
                             continues=self.tid, force_new=True)
        self.assertIsNone(got)
        self.assertIn("%s is closed" % self.tid, why)
        # CONTROL: a tombstone under it records history, as before
        got, why = tasks.add("a follow-on", "integrator", project=PROJECT,
                             continues=self.tid, force_new=True,
                             status="closed", closed_reason="history")
        self.assertIsNone(why, why)
        self.assertEqual(got["continues"], self.tid)

    def test_an_identical_re_hold_repairs_a_close_it_missed(self):
        finding_row = self.row()
        self.library_fix(finding_row, findings=["the retry drops the lock"])
        finding = self.found_in(finding_row["id"])[0]
        second = self.row(ref=self.c, task=False,
                          supersedes=finding_row["id"])
        with mock.patch.object(tasks, "close", return_value=(
                None, "task ledger is not writable")):
            out = self.hold(second, self.c)
        self.assertIn("task ledger is not writable",
                      "; ".join(out["findings_close_errors"]))
        self.assertEqual(self.status(finding["id"]), "open")
        again = self.hold(second, self.c)
        self.assertEqual([t for t, _r in again["findings_closed"]],
                         [finding["id"]])
        self.assertEqual(self.status(finding["id"]), "closed")

    def test_one_finding_is_filed_once_by_racing_filers(self):
        first, why = tasks.add("the retry drops the lock", "integrator",
                               force_new=True, found_in="ab" * 8,
                               found_chain="cd" * 8)
        self.assertIsNone(why, why)
        second, why = tasks.add("the retry drops the lock", "integrator",
                                force_new=True, found_in="ab" * 8,
                                found_chain="cd" * 8)
        self.assertIsNone(why, why)
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(len([r for r in tasks.rows().values()
                              if r.get("found_in") == "ab" * 8]), 1)

    def test_a_counted_advisory_FIX_records_without_filing_work(self):
        row = self.row()
        proof = {"reviewer_model": "gpt-5", "reviewer_run": "run-1",
                 "author_model": "claude-sonnet-5-5",
                 "author_model_source": "declared", "recorded_by": "integrator",
                 "reviewer_family": "openai", "independence": "cross-family"}
        with mock.patch.object(dispatches, "_on_behalf_binding",
                               return_value=(proof, None)):
            out, why = dispatches.mark_verdict(
                row["id"], row["tip"], "one advisory finding", "fix",
                basis="measured", prior_relation="new", finding_count=1,
                worse_than_main_paths=["helm/placeholder.py"],
                no_patch_because="the seat must own the cure",
                reviewer_model="gpt-5", reviewer_run="run-1")
        self.assertIsNone(why, why)
        self.assertEqual(out["advisory_reads"][0]["polarity"], "fix")
        self.assertEqual(self.state(row["id"])["status"], "open")
        self.assertEqual(self.found_in(row["id"]), [])

    def test_the_cli_accepts_a_counted_advisory_FIX_without_filing_work(self):
        row = self.row()
        proof = {"reviewer_model": "gpt-5", "reviewer_run": "run-1",
                 "author_model": "claude-sonnet-5-5",
                 "author_model_source": "declared", "recorded_by": "integrator",
                 "reviewer_family": "openai", "independence": "cross-family"}
        with mock.patch.object(dispatches, "_on_behalf_binding",
                               return_value=(proof, None)):
            with self.verdict_author():
                rc, _out, err = run(dispatches.cmd_dispatch, [
                    "verdict", row["id"], row["tip"], "--fix", "--measured",
                    "--reviewer-model", "gpt-5", "--reviewer-run", "run-1",
                    "--worse-than-main", "helm/placeholder.py",
                    "--no-patch-because", "the seat owns the cure",
                    "--finding-count", "1", "--prior-relation", "new",
                    "the read found work"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.state(row["id"])["status"], "open")
        self.assertEqual(self.found_in(row["id"]), [])

    def test_a_model_run_s_read_files_nothing_and_its_line_keeps_the_work(
            self):
        row = self.row()
        with mock.patch.object(dispatches, "_on_behalf_binding",
                               side_effect=lambda r, given, current=None:
                               (given, None)) as binding:
            out, why = dispatches.mark_verdict(
                row["id"], row["tip"], "the read found work", "fix",
                reviewer_model="gpt-5", reviewer_run="run-1",
                findings=["the retry drops the lock"])
        binding.assert_called_once()
        self.assertIsNone(out)
        self.assertIn("ride a seat's own verdict", why)
        self.assertEqual(self.state(row["id"])["status"], "open")
        self.assertEqual(self.found_in(row["id"]), [])
        with self.verdict_author():
            rc, _out, err = run(dispatches.cmd_dispatch, [
                "verdict", row["id"], row["tip"], "--fix", "--measured",
                "--reviewer-model", "gpt-5", "--reviewer-run", "run-1",
                "--worse-than-main", "helm/placeholder.py",
                "--no-patch-because", "a design finding for the meld",
                "--finding", "the retry drops the lock",
                "--note", "the naming reads well", "the read found work"])
        self.assertNotEqual(rc, 0, err)
        own = [ln for ln in err.splitlines()
               if "rides a FIX of your own: helm dispatch verdict" in ln]
        self.assertEqual(len(own), 1, err)
        self.assertIn("--finding 'the retry drops the lock'", own[0])
        self.assertIn("--note 'the naming reads well'", own[0])
        self.assertNotIn("--reviewer-model", own[0])

    def test_a_hold_rewrite_keeps_the_note_as_a_comment_line(self):
        row = self.row()
        with self.verdict_author():
            rc, _out, err = run(dispatches.cmd_dispatch, [
                "verdict", row["id"], row["tip"], "--approve", "--measured",
                "--note", "the naming reads well", "the read found nothing"])
        self.assertNotEqual(rc, 0, err)
        line = [ln for ln in err.splitlines()
                if ln.startswith("corrected: ")][-1]
        self.assertIn("helm dispatch hold", line)
        self.assertIn("helm task comment %s 'the naming reads well'"
                      % self.tid, err)


class NoCloserTakesWorkItDoesNotOwnOrDropsWhatItSkippedTest(ClosersBase):
    """A finding another live lane took stays open under every closer; a
    LAND keeps what a FIX named past the landed tip; a close the ledger
    skipped is said; and a FIX closed without retraction still names its
    findings (the door read of task/3742 slice b)."""

    def two_findings(self):
        """A FIX at b filing the finding another lane takes and one more."""
        first = self.row()
        self.library_fix(first, findings=["another lane took this finding",
                                          "the cap is off by one"])
        by_title = {k["title"]: k for k in self.found_in(first["id"])}
        return (first, by_title["another lane took this finding"],
                by_title["the cap is off by one"])

    def another_lane_took(self, finding):
        """A live lane that is no row of the chain records `finding` as the
        task it serves (`helm work` on the finding)."""
        self.git("branch", "lane/finding-lane", self.b)
        _wrote, why = taskkey.record_lane(self.repo, "finding-lane",
                                          finding["id"])
        self.assertIsNone(why, why)

    def test_a_hold_never_closes_a_finding_another_live_lane_took(self):  # noqa: VACUOUS_ASSERTION — the open finding is paired with the chain's other finding closed exactly by the same hold
        first, theirs, other = self.two_findings()
        self.another_lane_took(theirs)
        second = self.row(ref=self.c, task=False, supersedes=first["id"])
        out = self.hold(second, self.c)
        self.assertEqual(self.status(theirs["id"]), "open")
        # CONTROL: the chain's other finding the same hold answers closes
        self.assertEqual([t for t, _r in out["findings_closed"]],
                         [other["id"]])

    def test_a_retraction_never_closes_a_finding_another_live_lane_took(self):  # noqa: VACUOUS_ASSERTION — the open finding is paired with the chain's other finding closed exactly by the same retraction
        first, theirs, other = self.two_findings()
        self.another_lane_took(theirs)
        out = self.retract(first)
        self.assertEqual(self.status(theirs["id"]), "open")
        # CONTROL: the chain's other finding the same retraction answers
        # closes
        self.assertEqual(out["findings_closed"], [
            (other["id"], "retracted (row %s)" % first["id"][:12])])

    def test_a_LAND_keeps_a_finding_a_FIX_named_past_the_landed_tip(self):
        first = self.row()
        self.library_fix(first, findings=["the retry drops the lock"])
        at_b = self.found_in(first["id"])[0]
        second = self.row(ref=self.c, task=False, supersedes=first["id"])
        self.library_fix(second, findings=["the cap is off by one"])
        at_c = self.found_in(second["id"])[0]
        current = dispatches.snapshot()[0]
        chain = self.chain(first)
        # a LAND of b: the FIX at c read a tree past it, so b carries no cure
        # for what it found
        kept, err = review_findings.named_at(chain, self.b, current,
                                             self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(kept, sorted([at_b["id"], at_c["id"]]))
        # CONTROL: a LAND of c keeps its own, not the one named only at
        # b. An unrelated tree proves no cure for either finding.
        kept, err = review_findings.named_at(chain, self.c, current,
                                             self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(kept, [at_c["id"]])
        kept, err = review_findings.named_at(chain, self.side, current,
                                             self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(kept, sorted([at_b["id"], at_c["id"]]))

    def test_unknown_or_unrelated_naming_tip_cannot_prove_a_land_cured_it(self):  # noqa: VACUOUS_ASSERTION — fixed UNKNOWN/unrelated arms pair with an unconditional real-Git ancestral cure control
        from helm import vcs
        first = self.row(task=False)
        self.library_fix(first, findings=["the retry drops the lock"])
        finding = self.found_in(first["id"])[0]
        current = dispatches.snapshot()[0]
        chain = self.chain(first)
        for relation in (vcs.UNKNOWN, vcs.NOT_ANCESTOR):
            with self.subTest(relation=relation), mock.patch.object(
                    vcs.backend(self.repo), "ancestry", return_value=relation):
                kept, err = review_findings.named_at(chain, self.c, current,
                                                     self.repo)
                self.assertIn(finding["id"], kept)
                if relation == vcs.UNKNOWN:
                    self.assertIn("ancestry", err)
                else:
                    self.assertIsNone(err, err)
        # CONTROL: positive ancestry from naming tip to land proves a cure.
        kept, err = review_findings.named_at(chain, self.c, current,
                                             self.repo)
        self.assertIsNone(err, err)
        self.assertNotIn(finding["id"], kept)

    def test_an_open_finding_with_no_surviving_FIX_cannot_be_landed_as_cured(self):
        first = self.row(task=False)
        self.library_fix(first, findings=["a retracted FIX still saw work"])
        finding = self.found_in(first["id"])[0]
        self.retract(first, "fix")  # real door retains work, withdraws its FIX
        self.assertEqual(self.status(finding["id"]), "open")
        current = dispatches.snapshot()[0]
        kept, err = review_findings.named_at(self.chain(first), self.c,
                                             current, self.repo)
        self.assertEqual(kept, [finding["id"]])
        self.assertIn("no surviving FIX", err)
        # CONTROL: the same ancestral land can cure a genuinely named FIX.
        second = self.row(task=False)
        self.library_fix(second, findings=["a surviving FIX saw work"])
        proven = self.found_in(second["id"])[0]
        kept, err = review_findings.named_at(
            self.chain(second), self.c, dispatches.snapshot()[0], self.repo)
        self.assertIsNone(err, err)
        self.assertNotIn(proven["id"], kept)

    def test_a_chain_with_an_open_finding_and_no_landed_tip_cannot_close_it(self):
        first = self.row(task=False)
        self.library_fix(first, findings=["the retry drops the lock"])
        finding = self.found_in(first["id"])[0]
        kept, err = review_findings.named_at(
            self.chain(first), None, dispatches.snapshot()[0], self.repo)
        self.assertEqual(kept, [finding["id"]])
        self.assertIn("landed tip", err)
        # CONTROL: no chain is legitimately no finding-closure work.
        self.assertEqual(review_findings.named_at(
            None, None, dispatches.snapshot()[0], self.repo), ([], None))

    def test_a_close_the_task_ledger_skipped_is_said(self):
        first = self.row()
        self.library_fix(first, findings=["the retry drops the lock"])
        finding = self.found_in(first["id"])[0]
        second = self.row(ref=self.c, task=False, supersedes=first["id"])
        real = review_findings._read_open

        def read_then_moved(keep):
            """The finding's owner comments on it between the closer's read
            and its close."""
            got = real(keep)
            _row, why = tasks.comment(finding["id"], "on it", by="integrator")
            self.assertIsNone(why, why)
            return got
        with mock.patch.object(review_findings, "_read_open",
                               read_then_moved):
            out = self.hold(second, self.c)
        self.assertEqual(self.status(finding["id"]), "open")
        self.assertEqual(out.get("findings_close_errors"), [
            "%s was not closed: %s changed since the caller read it — "
            "nothing was written; judge it again from a fresh read"
            % (finding["id"], finding["id"])])
        self.assertIn("finding NOT closed — %s was not closed"
                      % finding["id"],
                      "\n".join(review_findings.closed_lines(out)))
        # CONTROL: the identical re-hold, with nothing moving, closes it
        again = self.hold(second, self.c)
        self.assertEqual([t for t, _r in again["findings_closed"]],
                         [finding["id"]])

    def closed_not_retracted(self):
        """R1 FIX at b files f; R2 superseding it carries f at c; then R1 is
        closed superseded, which does not retract its verdict."""
        first = self.row()
        self.library_fix(first, findings=["the retry drops the lock"])
        filed = self.found_in(first["id"])[0]
        second = self.row(ref=self.c, task=False, supersedes=first["id"])
        self.library_fix(second, carried=[filed["id"]])
        closed = {**self.state(first["id"]), "status": "closed",
                  "close_reason": "superseded"}
        return first, second, filed, closed

    def test_a_FIX_closed_without_retraction_still_names_its_findings(self):
        first, second, filed, closed = self.closed_not_retracted()
        retracted = {**self.state(second["id"]),
                     "polarity": dispatches.RETRACTED,
                     "verdict_retracted": True,
                     "retract_reads": "source-clean"}
        current = {first["id"]: closed, second["id"]: retracted}
        self.assertEqual(review_findings.close_retracted(retracted, current),
                         ([], []))
        self.assertEqual(self.status(filed["id"]), "open")
        # CONTROL: once R1's FIX is retracted as well, the last retraction
        # closes f
        current[first["id"]] = {**closed, "polarity": dispatches.RETRACTED,
                                "verdict_retracted": True}
        closed_now, errors = review_findings.close_retracted(retracted,
                                                             current)
        self.assertEqual(errors, [])
        self.assertEqual([t for t, _r in closed_now], [filed["id"]])

    def test_a_hold_past_a_closed_FIX_s_tip_closes_what_it_named(self):
        """With its one namer closed but not retracted, a finding is still
        answered by a hold past that namer's tip, before any LAND."""
        first = self.row()
        self.library_fix(first, findings=["the retry drops the lock"])
        filed = self.found_in(first["id"])[0]
        second = self.row(ref=self.c, task=False, supersedes=first["id"])
        held = {**self.state(second["id"]), "status": "held",
                "source_clean_tip": self.c}
        current = {first["id"]: {**self.state(first["id"]),
                                 "status": "closed",
                                 "close_reason": "superseded"},
                   second["id"]: held}
        self.assertEqual(review_findings.close_on_hold(held, current), (
            [(filed["id"], "cured, held source-clean at %s (row %s)"
              % (self.c[:12], second["id"][:12]))], []))
        self.assertEqual(self.status(filed["id"]), "closed")


class ABlankFindingNeverBecomesATitleTest(unittest.TestCase):
    """task/4050: a blank first line cannot become an empty task title.

    An all-blank finding is refused; leading blank lines before real content
    are stripped. Neither shape leaves an empty title for the task writer."""

    def setUp(self):
        _tmphome.pin_live_seats(self)

    def test_a_blank_finding_is_refused_before_a_title_can_be_built(self):
        for blank in ("", "   ", "\t\n"):
            fields, err = review_findings.clean_args(findings=[blank])
            self.assertIsNone(fields, "%r produced fields" % (blank,))
            self.assertIn("finding is required", err)
        # a non-string is refused by its own door, and refused too
        fields, err = review_findings.clean_args(findings=[None])
        self.assertIsNone(fields)
        self.assertIn("is text, not NoneType", err)

    def test_a_real_finding_still_cleans_and_can_title_a_task(self):
        # A blank FIRST line followed by content is the other claimed shape;
        # strip it rather than filing an empty first line as a separate task.
        for text in ("a real finding", "\na real finding", " \n a real finding\n"):
            fields, err = review_findings.clean_args(findings=[text])
            self.assertIsNone(err)
            self.assertEqual(fields["findings"], ["a real finding"])

    def test_the_record_writer_really_does_refuse_an_empty_title(self):
        # ...and the downstream half of the claim is true, which is why the
        # door above is what matters: with no door, this refusal would fire.
        _task, err = tasks.add("", "somebody")
        self.assertIn("title", err)


if __name__ == "__main__":
    unittest.main()
