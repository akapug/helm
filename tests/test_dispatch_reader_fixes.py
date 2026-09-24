#!/usr/bin/env python3
"""The review doors teach and REQUIRE that the reader fixes what it finds.

THE PROCEDURE these doors exist to hold: a reader that finds a mechanical
defect commits the cure on a branch off the exact tip it read and returns FIX
naming that tip; the lane's author reviews the patch; agreement on the patch is
what lands the chain. Three doors were open to the failure that the reader
reports a defect it was standing next to and could have cured:

  A. a REVIEW brief may tell its reader not to edit, which removes the cure.
  B. a FIX may carry no cure and say nothing about why.
  C. a reader that found the tip NOT worse than main and cured something
     anyway had NO door: `--approve` refuses `--patch-tip` and `--imperfect`
     refused outright, so the patch travelled by chat and nothing recorded
     that the author owed it an answer.

EVERY REFUSAL ARM HERE OWES A POSITIVE CONTROL IN THE SAME METHOD — the same
call with the because-flag, or the same door on an admitted shape. A refusal
arm alone proves that something said no, never that anything says yes.

The fixtures come from `tests.test_dispatches` because this file is a NEW
module only for the byte ceiling on that one: `DispatchBase` is the shared
scratch-home, scratch-repo and author-proof fixture every dispatch arm uses.
"""
import os
import unittest
from unittest import mock

from helm import dispatches, landreq
from tests import test_dispatches as td

run = td.run


class ReviewBriefMayNotForbidTheCureTest(td.DispatchBase):
    """A REVIEW brief carrying a DO-NOT-EDIT phrase is refused, or recorded.

    The phrase set is CLOSED and the escape is one flag, so the door never
    argues about intent: it says what tripped it, states the procedure, and
    takes a reason that rides the row.
    """

    _lane_seq = 0

    def send(self, body, *flags, kind="review"):
        """ONE LANE LABEL PER SEND. A second OPEN row reusing a label warns
        and returns 1, which would read here as the door refusing a body it
        actually admitted."""
        type(self)._lane_seq += 1
        return run(dispatches.cmd_dispatch,
                   ["send", "seat-b", "lane/read-%d" % self._lane_seq, body,
                    "--ref", self.side, "--kind", kind, "--new-work",
                    "--repo", self.repo, *flags])

    def test_every_declared_phrase_refuses_and_the_reason_admits_it(self):  # noqa: VACUOUS_ASSERTION — each refusal is paired IN THIS METHOD with the same body admitted under --read-only-because, and the sweep ends on an unconditional exact count
        seen = 0
        for phrase in dispatches.READ_ONLY_PHRASES:
            body = "review the delta at this tip: %s, then report" % phrase
            rc, _out, err = self.send(body)
            self.assertEqual(rc, 2, (phrase, err))
            self.assertIn("tells the reader not to edit", err)
            self.assertIn("--patch-tip", err)
            # THE POSITIVE CONTROL, on the same body and the same door.
            rc, out, err = self.send(
                body, "--read-only-because", "the tree is a customer mirror")
            self.assertEqual(rc, 0, (phrase, err))
            self.assertIn("REVIEW PROCEDURE", out)
            seen += 1
        self.assertEqual(seen, len(dispatches.READ_ONLY_PHRASES),
                         "the phrase sweep did not run")

    def test_the_recorded_reason_rides_the_row(self):
        rc, _out, err = self.send(
            "a read-only review, please report what you find",
            "--read-only-because", "the reader has no checkout of this repo")
        self.assertEqual(rc, 0, err)
        rows = dispatches.snapshot()[0]
        recorded = [row for row in rows.values()
                    if row.get("read_only_because")]
        self.assertEqual(len(recorded), 1, rows)
        self.assertEqual(recorded[0]["read_only_because"],
                         "the reader has no checkout of this repo")

    def test_a_BUILD_brief_saying_report_only_is_never_asked(self):  # noqa: VACUOUS_ASSERTION — the must-miss half rides an unconditional positive in the same method: the identical body under --kind review REFUSES on the same door
        body = "produce the census and report only, no code"
        rc, out, err = self.send(body, kind="build")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("tells the reader not to edit", err)
        self.assertNotIn("REVIEW PROCEDURE", out,
                         "the procedure line belongs to review sends")
        rc, _out, err = self.send(body, kind="review")
        self.assertEqual(rc, 2, err)
        self.assertIn("tells the reader not to edit", err)

    def test_the_match_is_on_words_not_on_bytes(self):  # noqa: VACUOUS_ASSERTION — the silent case is asserted beside an unconditional refusal on the phrase it is one letter away from, in this method
        rc, _out, err = self.send("chase this thread only as far as the tip")
        self.assertEqual(rc, 0, err)
        self.assertEqual(dispatches.read_only_hits("thread only"), [])
        # THE CONTROL: the same two words, this time inside a directive.
        rc, _out, err = self.send("a read only review of the tip, then say so")
        self.assertEqual(rc, 2, err)
        self.assertIn("'read-only review'", err)

    #: Sentences senders really wrote in review briefs, which DESCRIBE a
    #: measurement, a property or whose tree to leave alone. None of them
    #: tells the reader not to cure what it finds.
    DESCRIPTIONS = (
        "probed against the REAL helm home and the live ledger read-only, "
        "14,567 events",
        "my composer read only id, lane and new, and dropped the advisory",
        "headers read only to the blank line",
        "merge-tree run read-only BEFORE the rebase predicted the tree",
        "DRY RUN ON THE REAL SUBJECT, read-only, nothing removed",
        "a read-only verb that mutates a register would be a serious defect",
        "do not edit the worktree at that path; make your own checkout if "
        "you run anything",
        "the brief travels on stdin to a read-only mount",
    )
    #: And sentences that really did tell a reader to hold back its cure.
    DIRECTIVES = (
        ("Cross-family READ-ONLY review of the branch", "read-only review"),
        ("Cross-family READ-ONLY re-refute of round two", "read-only re-refute"),
        ("Hold source-clean until the token. Do not patch; findings route "
         "through me", "do not patch"),
        ("Do not edit or commit in the lane; probes go elsewhere",
         "do not edit or commit"),
        ("Source read only, no suite", "source read only"),
    )

    def test_a_description_of_a_measurement_is_never_refused(self):  # noqa: VACUOUS_ASSERTION — the silent sweep ends on an exact count and is paired in this class with test_a_real_directive_is_refused, which drives the same door to a refusal
        seen = 0
        for body in self.DESCRIPTIONS:
            with self.subTest(body=body):
                self.assertEqual(dispatches.read_only_hits(body), [])
                rc, _out, err = self.send("review the delta. " + body)
                self.assertEqual(rc, 0, err)
                seen += 1
        self.assertEqual(seen, 8)

    def test_a_real_directive_is_refused(self):
        for body, phrase in self.DIRECTIVES:
            with self.subTest(body=body):
                self.assertEqual(dispatches.read_only_hits(body), [phrase])
                rc, _out, err = self.send("review the delta. " + body)
                self.assertEqual(rc, 2, err)
                self.assertIn(repr(phrase), err)

    def test_the_two_spellings_of_one_phrase_are_one_hit(self):
        self.assertEqual(dispatches.read_only_hits("a read-only review"),
                         ["read-only review"])
        self.assertEqual(dispatches.read_only_hits("a read only review"),
                         ["read-only review"])
        self.assertEqual(
            dispatches.read_only_hits("read-only review: do not commit"),
            ["read-only review", "do not commit"])

    def test_every_review_send_prints_the_procedure(self):  # noqa: VACUOUS_ASSERTION — the must-miss half rides an unconditional positive in the same method: the review send PRINTS the line the build send omits
        rc, out, err = self.send("review the delta and say what you find")
        self.assertEqual(rc, 0, err)
        self.assertIn("REVIEW PROCEDURE", out)
        self.assertIn("--patch-tip", out)
        self.assertIn("agreement on the patch", out)
        rc, out, err = self.send("build the census", kind="build")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("REVIEW PROCEDURE", out)

    def test_the_note_on_an_ADD_row_passes_the_same_door(self):  # noqa: VACUOUS_ASSERTION — the refusal is paired with the same note admitted under the because-flag on the same verb in this method
        def argv(lane):
            return ["add", "seat-b", lane, "--ref", self.side,
                    "--kind", "review", "--new-work", "--repo", self.repo,
                    "--note", "do not edit, report only"]
        rc, _out, err = run(dispatches.cmd_dispatch, argv("lane/added-one"))
        self.assertEqual(rc, 2, err)
        self.assertIn("tells the reader not to edit", err)
        rc, _out, err = run(
            dispatches.cmd_dispatch,
            argv("lane/added-two") + ["--read-only-because", "a frozen mirror"])
        self.assertEqual(rc, 0, err)

    def test_the_library_door_refuses_a_caller_that_skips_the_cli(self):  # noqa: VACUOUS_ASSERTION — the refusal is paired with the same call admitted under read_only_because in this method
        row, why, _sent = dispatches.send(
            "seat-b", "lane/direct", "please do not edit anything here",
            self.side, repo=self.repo, kind="review", new_work=True,
            sign=False)
        self.assertIsNone(row)
        self.assertIn("tells the reader not to edit", why)
        row, why, _sent = dispatches.send(
            "seat-b", "lane/direct", "please do not edit anything here",
            self.side, repo=self.repo, kind="review", new_work=True,
            sign=False, read_only_because="the reader reviews a vendored drop")
        self.assertIsNone(why, why)
        self.assertEqual(row["read_only_because"],
                         "the reader reviews a vendored drop")

    def test_the_flags_are_advertised_where_a_sender_will_look(self):
        from helm import cli
        self.assertIn("--read-only-because", dispatches.USAGE)
        self.assertIn("--read-only-because", cli._VERB_HELP["dispatch"])


class AFixWithNoCureSaysWhyTest(td.DispatchBase):
    """A FIX verdict names the cure it committed, or says why there is none.

    The reader who found the defect is standing in the tree that carries it,
    so the default answer is YES. A DESIGN finding bound for a meld is the
    other real answer and must be RECORDABLE, not merely permitted by silence.
    """

    def verdict(self, row, *flags):
        with self.verdict_author():
            return run(dispatches.cmd_dispatch, [
                "verdict", row["id"], row["tip"], "--fix", "--measured",
                "--worse-than-main", "helm/dispatches.py", *flags,
                "the guard is inverted"])

    def test_a_FIX_with_no_cure_and_no_reason_refuses_and_the_reason_admits(self):  # noqa: VACUOUS_ASSERTION — the refusal is paired with the same call admitted under --no-patch-because in this method, and the row's state is read back off disk both times
        row = self.add(ref=self.b, recipient="seat-b")
        rc, _out, err = self.verdict(row)
        self.assertEqual(rc, 2, err)
        self.assertIn("A READER FIXES WHAT IT FINDS", err)
        self.assertIn("--patch-tip", err)
        # no such trailer exists, and the trailer rung refuses authoring lines
        self.assertNotIn("trailer", err)
        self.assertIn(self.b[:12], err,
                      "the refusal names the tip the cure branches off")
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["status"], "open")
        # THE POSITIVE CONTROL: the same call, one flag more.
        rc, out, err = self.verdict(
            row, "--no-patch-because", "a DESIGN finding; taking it to a meld")
        self.assertEqual(rc, 0, err)
        current = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(current["status"], "verdict")
        self.assertEqual(current["no_patch_because"],
                         "a DESIGN finding; taking it to a meld")
        self.assertIn("no cure committed, because", out)

    def test_a_FIX_that_names_its_cure_is_never_asked_the_question(self):  # noqa: VACUOUS_ASSERTION — the absent field is asserted beside an unconditional rc==0 and an exact patch_tip on the same replayed row
        row = self.add(ref=self.b, recipient="seat-b")
        rc, _out, err = self.verdict(row, "--patch-tip", self.c)
        self.assertEqual(rc, 0, err)
        current = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(current["patch_tip"], self.c)
        self.assertNotIn("no_patch_because", current)

    def test_naming_a_cure_AND_a_reason_there_is_none_refuses(self):  # noqa: VACUOUS_ASSERTION — the refusal is paired with each half admitted alone in this method
        row = self.add(ref=self.b, recipient="seat-b")
        rc, _out, err = self.verdict(
            row, "--patch-tip", self.c, "--no-patch-because", "a meld finding")
        self.assertEqual(rc, 1, err)
        self.assertIn("names the cure OR says why there is none", err)
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["status"], "open")
        # BOTH HALVES ADMIT ALONE, so the refusal is about the PAIR.
        rc, _out, err = self.verdict(row, "--patch-tip", self.c)
        self.assertEqual(rc, 0, err)
        other = self.add(ref=self.b, recipient="seat-b")
        rc, _out, err = self.verdict(other, "--no-patch-because", "a meld")
        self.assertEqual(rc, 0, err)

    def test_no_other_polarity_is_asked_the_cure_question(self):  # noqa: VACUOUS_ASSERTION — the loop ends on an unconditional EXACT count of the polarities swept, and each refusal is paired with the FIX admit below it
        seen = 0
        for polarity in ("approve", "supersede", "concur"):
            row = self.add(ref=self.b, lane="no-patch-" + polarity)
            written, why = dispatches.mark_verdict(
                row["id"], row["tip"], "a finding", polarity,
                no_patch_because="a meld finding")
            self.assertIsNone(written, polarity)
            self.assertIn("--no-patch-because", why)
            seen += 1
        self.assertEqual(seen, 3, "the polarity sweep did not run")
        fixed = self.add(ref=self.b, lane="no-patch-fix")
        written, why = dispatches.mark_verdict(
            fixed["id"], fixed["tip"], "a finding", "fix",
            worse_than_main_paths=["helm/dispatches.py"],
            no_patch_because="a meld finding")
        self.assertIsNone(why, why)
        self.assertEqual(written["no_patch_because"], "a meld finding")

    def test_the_reason_is_ONE_argv_token(self):  # noqa: VACUOUS_ASSERTION — the parse refusal is paired with the same reason admitted as one quoted token in this method
        row = self.add(ref=self.b, recipient="seat-b")
        with self.verdict_author():
            rc, _out, err = run(dispatches.cmd_dispatch, [
                "verdict", row["id"], row["tip"], "--fix", "--measured",
                "--worse-than-main", "helm/dispatches.py",
                "--no-patch-because", "--imperfect", "the note"])
        self.assertEqual(rc, 2, err)
        self.assertIn("ONE argv token", err)
        rc, _out, err = self.verdict(
            row, "--no-patch-because", "a design finding for a meld")
        self.assertEqual(rc, 0, err)

    def test_the_reason_is_part_of_verdict_retry_identity(self):
        row = self.add(ref=self.b, recipient="seat-b")
        rc, _out, err = self.verdict(row, "--no-patch-because", "a meld")
        self.assertEqual(rc, 0, err)
        again, why = dispatches.mark_verdict(
            row["id"], row["tip"], "the guard is inverted", "fix",
            basis="measured", worse_than_main_paths=["helm/dispatches.py"],
            no_patch_because="a meld")
        self.assertIsNone(why, why)
        self.assertEqual(again["no_patch_because"], "a meld")
        conflicting, why = dispatches.mark_verdict(
            row["id"], row["tip"], "the guard is inverted", "fix",
            basis="measured", worse_than_main_paths=["helm/dispatches.py"],
            no_patch_because="a different reason entirely")
        self.assertIsNone(conflicting)
        self.assertIn("already has a verdict", why)


class ACleanReadThatCarriesACureHasADoorTest(td.DispatchBase):
    """`--fix --imperfect --patch-tip` — the reader found the tip NOT worse
    than main on any path it touches AND committed a real improvement.

    WITHOUT A PATCH the refusal stands unchanged, and that is the point of the
    split: findings that remain true on a tip no worse than main are APPROVE
    plus separately filed remainder. What had no door is the CURE, and a cure
    with no door travels by chat, where nothing records that the author owes
    an answer to it.
    """

    def verdict(self, row, *flags):
        with self.verdict_author():
            return run(dispatches.cmd_dispatch, [
                "verdict", row["id"], row["tip"], "--fix", "--measured",
                "--imperfect", *flags,
                "not worse than main; moved the port off a live socket"])

    def test_imperfect_WITH_a_patch_records_fix_imperfect_and_the_cure(self):
        row = self.add(ref=self.b, recipient="seat-b")
        rc, out, err = self.verdict(row, "--patch-tip", self.c)
        self.assertEqual(rc, 0, err)
        # REPLAYED OFF DISK: the canonical reducer is what every reader runs.
        current = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(current["polarity"], "fix")
        self.assertEqual(current["exit_answer"], "imperfect")
        self.assertEqual(current["patch_tip"], self.c)
        self.assertEqual(current["patch_author"], "seat-b")
        self.assertNotIn("worse_than_main_paths", current)
        self.assertIn("IMPERFECT, CURED", out)
        self.assertIn(self.c[:12], out)

    def test_imperfect_WITHOUT_a_patch_is_still_not_a_block(self):  # noqa: VACUOUS_ASSERTION — the refusal is paired with the same call admitted under --patch-tip in this method, and the row's status is read off disk both times
        row = self.add(ref=self.b, recipient="seat-b")
        rc, _out, err = self.verdict(row)
        self.assertEqual(rc, 2, err)
        self.assertIn("IMPERFECT IS NOT A BLOCK", err)
        self.assertIn("file the remaining findings as dispatch rows", err)
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["status"], "open")
        # THE POSITIVE CONTROL: the same call carrying a cure.
        rc, _out, err = self.verdict(row, "--patch-tip", self.c)
        self.assertEqual(rc, 0, err)
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["status"],
                         "verdict")

    def test_the_refusal_teaches_the_pair_that_does_admit(self):
        row = self.add(ref=self.b, recipient="seat-b")
        rc, _out, err = self.verdict(row)
        self.assertEqual(rc, 2, err)
        self.assertIn("--imperfect --patch-tip SHA", err)
        self.assertIn("agreement on that patch", err)

    def test_a_verdict_answers_the_exit_question_once(self):  # noqa: VACUOUS_ASSERTION — the refusal is paired with each answer admitted alone in this method
        row = self.add(ref=self.b, recipient="seat-b")
        rc, _out, err = self.verdict(
            row, "--worse-than-main", "helm/dispatches.py",
            "--patch-tip", self.c)
        self.assertEqual(rc, 2, err)
        self.assertIn("choose exactly one exit answer", err)
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["status"], "open")
        rc, _out, err = self.verdict(row, "--patch-tip", self.c)
        self.assertEqual(rc, 0, err)
        other = self.add(ref=self.b, recipient="seat-b")
        with self.verdict_author():
            rc, _out, err = run(dispatches.cmd_dispatch, [
                "verdict", other["id"], other["tip"], "--fix", "--measured",
                "--worse-than-main", "helm/dispatches.py",
                "--no-patch-because", "a design finding for a meld",
                "it regresses"])
        self.assertEqual(rc, 0, err)

    def test_imperfect_belongs_only_to_the_exit_question_polarities(self):  # noqa: VACUOUS_ASSERTION — the loop ends on an unconditional EXACT count, and FIX admits the same shape below it
        seen = 0
        for polarity in ("approve", "concur"):
            row = self.add(ref=self.b, lane="imperfect-" + polarity)
            written, why = dispatches.mark_verdict(
                row["id"], row["tip"], "a finding", polarity,
                imperfect=True, patch_tip=self.c)
            self.assertIsNone(written, polarity)
            seen += 1
        self.assertEqual(seen, 2, "the polarity sweep did not run")
        fixed = self.add(ref=self.b, recipient="seat-b", lane="imperfect-fix")
        written, why = dispatches.mark_verdict(
            fixed["id"], fixed["tip"], "a finding", "fix",
            imperfect=True, patch_tip=self.c)
        self.assertIsNone(why, why)
        self.assertEqual(written["exit_answer"], "imperfect")

    def test_a_stored_imperfect_answer_with_no_patch_reads_UNMARKED(self):  # noqa: VACUOUS_ASSERTION — the fail-closed read is asserted beside an unconditional positive on the SAME function and the SAME replayed row in this method
        row = self.add(ref=self.b, recipient="seat-b")
        rc, _out, err = self.verdict(row, "--patch-tip", self.c)
        self.assertEqual(rc, 0, err)
        current = dispatches.snapshot()[0][row["id"]]
        self.assertIn("IMPERFECT, CURED",
                      dispatches.verdict_exit_answer(current))
        forged = dict(current)
        forged.pop("patch_tip")
        self.assertEqual(dispatches.verdict_exit_answer(forged), "UNMARKED",
                         "an imperfect answer carrying no cure claims nothing")
        short = dict(current, patch_tip=self.c[:12])
        self.assertEqual(dispatches.verdict_exit_answer(short), "UNMARKED")
        # AND AN ANSWER OUTSIDE THE DECLARED SET IS NOT READ AT ALL.
        self.assertEqual(
            dispatches.verdict_exit_answer(dict(current, exit_answer="later")),
            "UNMARKED")
        self.assertEqual(dispatches.EXIT_ANSWERS,
                         ("worse-than-main", "imperfect"))

    def test_replay_never_turns_an_imperfect_answer_into_a_block(self):
        """THE PROJECTION MUST READ THE ROW'S OWN ANSWER. A branch that fires
        on "is the answer readable" and then writes one hardcoded name turns
        every other answer into the blocking one on the next fold."""
        row = self.add(ref=self.b, recipient="seat-b")
        rc, _out, err = self.verdict(row, "--patch-tip", self.c)
        self.assertEqual(rc, 0, err)
        current = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(current["exit_answer"], "imperfect")
        self.assertNotIn("worse_than_main_paths", current)
        # THE OTHER ANSWER STILL REPLAYS AS ITSELF, on the same reducer.
        other = self.add(ref=self.b, recipient="seat-b")
        with self.verdict_author():
            rc, _out, err = run(dispatches.cmd_dispatch, [
                "verdict", other["id"], other["tip"], "--fix", "--measured",
                "--worse-than-main", "helm/dispatches.py",
                "--patch-tip", self.c, "it regresses"])
        self.assertEqual(rc, 0, err)
        blocking = dispatches.snapshot()[0][other["id"]]
        self.assertEqual(blocking["exit_answer"], "worse-than-main")
        self.assertEqual(tuple(blocking["worse_than_main_paths"]),
                         ("helm/dispatches.py",))


class ThePatchIsWhatIsOwedNotTheTipTest(td.DispatchBase):
    """`helm lr` on an IMPERFECT+patch row reads AUTHOR AGREEMENT OWED ON THE
    PATCH, never as a block on the tip the reader found no worse than main.

    Both surfaces, because the list line is where an author sees the row first
    and the detail page is what the integrator opens before a land.
    """

    def setUp(self):
        super().setUp()
        # OFF TRUNK, DELIBERATELY. The base fixture's `b` and `c` sit on its
        # trunk, so a row pinned there reads LANDED and every readiness rung
        # below would be answering about a landing instead of a review.
        self.git("checkout", "-q", "side")
        self.cure = self.commit_file("g", "the reviewer's own cure")
        self.git("checkout", "-q", self.main)
        landreq._CHAIN_CONTRIB_MEMO.clear()
        landreq._LEDGER_FOLD_MEMO.clear()
        landreq._GATE_INDEX_MEMO.clear()
        self.addCleanup(landreq._CHAIN_CONTRIB_MEMO.clear)
        self.addCleanup(landreq._LEDGER_FOLD_MEMO.clear)
        self.addCleanup(landreq._GATE_INDEX_MEMO.clear)

    def cured_row(self):
        row = self.add(ref=self.side, recipient="seat-b")
        with self.verdict_author():
            rc, _out, err = run(dispatches.cmd_dispatch, [
                "verdict", row["id"], row["tip"], "--fix", "--measured",
                "--imperfect", "--patch-tip", self.cure,
                "not worse than main; moved the port off a live socket"])
        self.assertEqual(rc, 0, err)
        lr, why = landreq.get(row["id"])
        self.assertIsNone(why, why)
        return lr

    def blocking_row(self):
        row = self.add(ref=self.side, recipient="seat-b")
        with self.verdict_author():
            rc, _out, err = run(dispatches.cmd_dispatch, [
                "verdict", row["id"], row["tip"], "--fix", "--measured",
                "--worse-than-main", "helm/dispatches.py",
                "--patch-tip", self.cure, "it regresses on that path"])
        self.assertEqual(rc, 0, err)
        lr, why = landreq.get(row["id"])
        self.assertIsNone(why, why)
        return lr

    def test_the_lr_row_carries_the_exit_answer(self):
        lr = self.cured_row()
        self.assertEqual(lr["exit_answer"], "imperfect")
        self.assertEqual(lr["patch_tip"], self.cure)
        self.assertEqual(lr["polarity"], "fix")

    def test_the_list_line_says_what_is_owed(self):  # noqa: VACUOUS_ASSERTION — the must-miss half rides an unconditional positive in the same method: the cured row PRINTS the mark the blocking row omits
        line = landreq._line(self.cured_row())
        self.assertIn("AUTHOR AGREEMENT OWED ON THE PATCH", line)
        self.assertIn(self.cure[:12], line)
        self.assertIn("not a block", line)
        # A BLOCKING FIX ON THE SAME DOOR MUST NOT BORROW THE SENTENCE.
        self.assertNotIn("AUTHOR AGREEMENT OWED",
                         landreq._line(self.blocking_row()))

    def test_the_detail_page_says_what_is_owed(self):  # noqa: VACUOUS_ASSERTION — the must-miss half rides an unconditional positive in the same method: the cured row RENDERS the exit line the blocking row omits
        shown = landreq._render_show(self.cured_row())
        self.assertIn("AUTHOR AGREEMENT OWED ON THE PATCH", shown)
        self.assertIn("NOT a block on %s" % self.side[:12], shown)
        self.assertIn("record an agreeing verdict on it", shown)
        blocking = landreq._render_show(self.blocking_row())
        self.assertNotIn("AUTHOR AGREEMENT OWED", blocking)
        self.assertIn("patch     %s" % self.cure[:12], blocking,
                      "the patch itself is still rendered on a blocking FIX")

    def test_the_recorded_no_patch_reason_reaches_the_detail_page(self):
        row = self.add(ref=self.side, recipient="seat-b")
        with self.verdict_author():
            rc, _out, err = run(dispatches.cmd_dispatch, [
                "verdict", row["id"], row["tip"], "--fix", "--measured",
                "--worse-than-main", "helm/dispatches.py",
                "--no-patch-because", "a DESIGN finding, bound for a meld",
                "the shape is wrong"])
        self.assertEqual(rc, 0, err)
        lr, why = landreq.get(row["id"])
        self.assertIsNone(why, why)
        self.assertIn("a DESIGN finding, bound for a meld",
                      landreq._render_show(lr))


class TheAuthorsAgreementOnThePatchLandsTheChainTest(td.DispatchBase):
    """THE LADDER, END TO END, THROUGH THE SHIPPED WRITERS.

    Round one: the author dispatches a review at the tip; the reader finds it
    no worse than main, commits a cure and returns FIX/IMPERFECT naming it.
    Round two: an ordinary review row on the PATCH TIP, continuing the same
    chain, addressed to the lane's AUTHOR, who agrees. The chain reaches READY
    on the ordinary machinery — nothing in the readiness ladder keys on
    `patch_tip`, and this arm is what says so rather than a reading of it.

    THE OUTSIDER RUNG IS UNTOUCHED AND IS ASSERTED HERE AS ITSELF: the author
    wrote the chain, so the author's own agreement makes the row READY and
    `ready_word` READY-SELF-REVIEW. One re-read of the composed tip by a seat
    that wrote none of it is what turns the word plain, and the last arm walks
    that final step so the ladder is proven to its top.
    """

    def setUp(self):
        super().setUp()
        # OFF TRUNK, DELIBERATELY. The base fixture's `b` and `c` sit on its
        # trunk, so a row pinned there reads LANDED and every readiness rung
        # below would be answering about a landing instead of a review.
        self.git("checkout", "-q", "side")
        self.cure = self.commit_file("g", "the reviewer's own cure")
        self.git("checkout", "-q", self.main)
        landreq._CHAIN_CONTRIB_MEMO.clear()
        landreq._LEDGER_FOLD_MEMO.clear()
        landreq._GATE_INDEX_MEMO.clear()
        self.addCleanup(landreq._CHAIN_CONTRIB_MEMO.clear)
        self.addCleanup(landreq._LEDGER_FOLD_MEMO.clear)
        self.addCleanup(landreq._GATE_INDEX_MEMO.clear)
        # THE GATE RUNG SITS BELOW THIS ONE. These synthetic side branches
        # have no suite to run, so a gate-capable writer would answer
        # UNVERIFIED for every approve here and every arm would be measuring
        # the receipt rung instead of the readiness ladder. Same pin
        # `LandReqBase` carries, and for the same stated reason.
        policy = mock.patch.object(dispatches, "GATE_CAPS", ())
        policy.start()
        self.addCleanup(policy.stop)

    def author_sends(self, lane, recipient, ref, supersedes=None):
        row, why, _sent = dispatches.send(
            recipient, lane, "review the delta at " + ref[:12], ref,
            repo=self.repo, kind="review", sign=False,
            key="key-" + lane, new_work=supersedes is None,
            supersedes=supersedes)
        self.assertIsNone(why, why)
        return row

    def agrees(self, row, note):
        with self.verdict_author():
            written, why = dispatches.mark_verdict(
                row["id"], row["tip"], note, "approve", basis="measured",
                bind_author=True)
        self.assertIsNone(why, why)
        return written

    def lr(self, row):
        out, why = landreq.get(row["id"])
        self.assertIsNone(why, why)
        return out

    def sends_as(self, seat, *args, **kwargs):
        """WHO DISPATCHES the round on the patch tip. `send` refuses
        self-delivery, so the round asking the lane's AUTHOR to read the patch
        cannot be minted by that author — the reader who committed the cure is
        who hands it over. Scoped, so the seat name never outlives the call."""
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": seat}):
            return self.author_sends(*args, **kwargs)

    def cured_round(self):
        first = self.author_sends("lane/r1", "seat-b", self.side)
        with self.verdict_author():
            written, why = dispatches.mark_verdict(
                first["id"], first["tip"],
                "not worse than main; moved the port off a live socket",
                "fix", basis="measured", bind_author=True,
                imperfect=True, patch_tip=self.cure)
        self.assertIsNone(why, why)
        self.assertEqual(written["patch_tip"], self.cure,
                         "fixture premise: the chain must RECORD the cure, or "
                         "no arm below is about anything")
        return first

    def test_the_authors_agreement_on_the_patch_tip_makes_the_chain_READY(self):
        first = self.cured_round()
        self.assertEqual(self.lr(first)["state"], "CHANGES_REQUESTED",
                         "the pole this arm is measured against")
        # THE PATCH TIP IS THE ARTIFACT OF THE NEXT ROUND, and the lane's
        # author is who reads it. The reader that wrote the cure sends it.
        second = self.sends_as("seat-b", "lane/r2", "integrator", self.cure,
                               supersedes=first["id"])
        self.agrees(second, "I read the patch; it is complete")
        lr = self.lr(second)
        self.assertEqual(lr["state"], "READY")
        self.assertEqual(lr["reviewed_tip"], self.cure,
                         "READY is bound to the PATCH tip, not the first tip")
        self.assertEqual(lr["chain_root"], first["chain_root"] or first["id"])

    def test_the_outsider_rung_still_answers_about_the_composed_tip(self):
        """THE HONEST HALF: the author wrote the chain, so the author's own
        agreement reaches READY and the WORD says self-review. A seat that
        wrote none of it turns the word plain, on the very same tip."""
        first = self.cured_round()
        second = self.sends_as("seat-b", "lane/r2", "integrator", self.cure,
                               supersedes=first["id"])
        self.agrees(second, "I read the patch; it is complete")
        independent, why = landreq.independent_review(self.lr(second))
        self.assertIs(independent, False)
        self.assertIn("outsider approval missing", why)
        third = self.author_sends("lane/r3", "seat-c", self.cure,
                                  supersedes=second["id"])
        self.agrees(third, "read the composed tip; wrote none of it")
        lr = self.lr(third)
        self.assertEqual(lr["state"], "READY")
        self.assertIs(landreq.independent_review(lr)[0], True)

    def test_both_authors_are_credited_on_the_cured_round(self):  # noqa: VACUOUS_ASSERTION — the absent second name on the control is asserted beside an unconditional two-name credit list on the cured row in this method
        first = self.cured_round()
        credits = landreq.chain_credits(self.lr(first))
        self.assertEqual(credits, ["integrator", "seat-b"],
                         "the reader who committed the cure wrote part of "
                         "what lands and the record has to say so")
        # THE CONTROL: a round with no cure credits its author alone.
        plain = self.author_sends("lane/plain", "seat-c", self.side)
        with self.verdict_author():
            _w, why = dispatches.mark_verdict(
                plain["id"], plain["tip"], "it regresses", "fix",
                basis="measured", bind_author=True,
                worse_than_main_paths=["helm/dispatches.py"],
                no_patch_because="a DESIGN finding, bound for a meld")
        self.assertIsNone(why, why)
        self.assertEqual(landreq.chain_credits(self.lr(plain)), ["integrator"])


if __name__ == "__main__":
    unittest.main()
