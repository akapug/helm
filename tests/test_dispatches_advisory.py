#!/usr/bin/env python3
"""The ADVISORY CLOSE: the one terminal a polarity-less verdict row can reach.

A SEPARATE MODULE BECAUSE THE SIBLING IS AT ITS CEILING. These arms belong in
tests/test_dispatches.py beside the rest of the cancel door's arms, and that
file is 1,031,227 bytes against the tree's 1.0 MiB never-track ceiling — a new
class there is refused at the commit boundary rather than reviewed. The
fixtures are IMPORTED rather than re-declared (`DispatchBase`, `OLD_TS`,
`run`), which is the file-split convention this suite already uses ten times
over (tests/test_lr_close.py from tests/test_landreq.py, and its own
descendants).

Hermetic exactly as the sibling: HELM_HOME/HELM_CHAT_DIR are tmp dirs, every
git repo is minted in setUp, and the real ledger is never written. Every
refusal arm asserts LEDGER LINES UNCHANGED — the effect, never the absence of
a complaint — and every absence assertion carries a positive control that goes
through the same door, in the same ledger.
"""
import json
from unittest import mock

from helm import dispatches, eventledger
from tests.test_dispatches import OLD_TS, DispatchBase, run


class PolaritylessVerdictDoorTest(DispatchBase):
    """A verdict that declared NO polarity gets a closing door.

    THE DEFECT, measured on a copy of the live ledger: 29 rows fold to a
    verdict with no polarity; 28 already carry a land-request terminal and ONE
    (a v1 snapshot row, opened and verdicted before polarity was a field)
    carries none. Every door refused it — `dispatch cancel` because a REVIEWED
    row is not cancelled, `helm lr close` with "no such land request" because a
    row with no exact `tip` never enters `landreq.project_raw`'s eligible set —
    while `obligation.unanswered_fixes` re-delivered its nag hourly and told
    the reader to "cancel the row".

    THE DECISION ABOUT THE TIER AXIS, taken from the code rather than by
    analogy with the word "advisory". `approval_tier_for_verdict` reads
    recorded tier evidence and the row's polarity as INDEPENDENT axes: a
    `verdict_tier_evidence.state` of `outside` denies that this reviewer's
    APPROVE authorized anything, and says nothing about whether a direction was
    declared. The class this door admits is defined by the ABSENT DIRECTION, so
    an outside-tier row that declared a polarity keeps every proof-bearing door
    that polarity earns and is refused here; an outside-tier row that declared
    none is admitted, because it is in this class for the same reason every
    other member is.
    """

    OLD = OLD_TS

    def _polarityless(self, rid, lane="advisory-lane"):
        """The live ledger's own shape: a pre-boundary v1 OPEN snapshot row and
        a pre-boundary v1 VERDICT snapshot row over it. Replay's compat arm
        never sets `polarity` on such a row, so this is the only way the state
        can exist — the strict writer has REQUIRED a polarity since the field
        became mandatory."""
        base = {"id": rid, "ts": self.OLD, "recipient": "seat-a",
                "lane": lane, "ref": self.a[:7], "note": None,
                "deadline_s": 60, "source": "old", "status": "open",
                "ack_ref": None, "verdict_ref": None, "last_updated": self.OLD}
        self.assertTrue(eventledger.append(dispatches.ledger_path(), base))
        closed = dict(base, status="verdict", verdict_ref="reviewed, no finding")
        self.assertTrue(eventledger.append(dispatches.ledger_path(), closed))
        row = dispatches.snapshot()[0][rid]
        self.assertEqual(row["status"], "verdict")
        self.assertIsNone(row.get("polarity"))
        return row

    def _verdicted(self, polarity, lane):
        """A REAL verdict row carrying `polarity`, written by the real writer.

        An APPROVE needs a verified gate token — the writer refuses an ungated
        one outright — so this borrows the file's established gate patch."""
        row = self.add(lane=lane)
        evidence = ("gate:0123456789abcdef reviewed"
                    if polarity == "approve" else "reviewed")
        with mock.patch.object(
                dispatches.gate, "bind",
                return_value=("VERIFIED", "a" * 16, "test receipt")):
            out, why = dispatches.mark_verdict(
                row["id"], row["tip"], evidence, polarity)
        self.assertIsNone(why)
        self.assertEqual(out["polarity"], polarity)
        return out

    def _lines(self):
        with open(dispatches.ledger_path(), encoding="utf-8") as f:
            return [line for line in f if line.strip()]

    def test_a_polarityless_verdict_closes_and_appends_exactly_one_event(self):
        row = self._polarityless("a1d1f0a1")
        before = len(self._lines())
        out, why = dispatches.mark_cancel(row["id"], "read it; nothing owed")
        self.assertIsNone(why)
        self.assertEqual(out["status"], "cancelled")
        self.assertIs(out.get("cancel_advisory"), True)
        self.assertEqual(
            out["cancel_reason"],
            "advisory-closed (verdict declared no polarity): "
            "read it; nothing owed")
        appended = self._lines()[before:]
        self.assertEqual(len(appended), 1, "the close appended %d events"
                                           % len(appended))
        event = json.loads(appended[0])
        self.assertEqual((event["event"], event["id"], event["advisory"]),
                         ("cancel", row["id"], True))
        # THE REPLAY AGREES WITH THE WRITER. A projection the fold does not
        # reproduce is the abandon shape: the CLI prints a terminal over a row
        # that stayed live on every later read.
        replayed = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(
            (replayed["status"], replayed.get("cancel_advisory"),
             replayed["cancel_reason"]),
            ("cancelled", True, out["cancel_reason"]))

    def test_a_fix_verdict_keeps_the_unchanged_refusal(self):
        """THE CONTROL. A declared FIX demands a cure, so closing it as advice
        would erase a claim somebody made. The refusal text is asserted whole,
        because it is the instrument that sends such an author to the
        withdrawal door."""
        row = self._verdicted("fix", "advisory-control-fix")
        before = self._lines()
        # UNCONDITIONAL POSITIVE CONTROLS ON THE TWO OBSERVABLES THIS ARM
        # ASSERTS AN ABSENCE OF, both against the real door. An empty ledger
        # would satisfy the no-append check below while proving nothing, and a
        # `mark_cancel` that returned None for EVERYTHING would satisfy the
        # refusal while refusing the cure too — so a row the door DOES admit
        # is closed first, in this same ledger and through this same call.
        self.assertTrue(before, "the fixture wrote no ledger")
        admitted = self._polarityless("a1d1f0a8", lane="advisory-fix-control")
        opened, no_why = dispatches.mark_cancel(admitted["id"], "nothing owed")
        self.assertIsNone(no_why)
        self.assertEqual(opened["status"], "cancelled")
        before = self._lines()
        out, why = dispatches.mark_cancel(row["id"], "close it as advice")
        self.assertIsNone(out)
        self.assertEqual(why, (
            "dispatch %s already has a verdict (closed) — a "
            "reviewed dispatch is not cancelled. If the verdict "
            "was a FIX and the honest answer is that the "
            "artifact should NOT exist, that is a WITHDRAWAL "
            "and not a cancel: `helm lr close %s --reason "
            "withdrawn --evidence \"<you accept the verdict, "
            "and where the refutation is recorded>\"` retires it "
            "without minting a review over nothing (git must "
            "prove the reviewed tip absent from trunk, which it "
            "can while that object still reads; once gc has "
            "pruned it the absence reads UNKNOWN and withdrawn "
            "refuses, and `helm lr close %s --reason stranded` "
            "is the door for the destroyed object once nothing "
            "live holds the work)"
            % (row["id"], row["id"][:12], row["id"][:12])))
        self.assertEqual(self._lines(), before)

    def test_an_approve_verdict_keeps_the_unchanged_refusal(self):
        """The second control. An APPROVE AUTHORIZED a land; a terminal saying
        it authorized nothing would contradict the record."""
        row = self._verdicted("approve", "advisory-control-approve")
        before = self._lines()
        self.assertTrue(before, "the fixture wrote no ledger")
        out, why = dispatches.mark_cancel(row["id"], "close it as advice")
        self.assertIsNone(out)
        self.assertIn("already has a verdict (closed) — a reviewed dispatch "
                      "is not cancelled", why)
        self.assertEqual(self._lines(), before)
        # MUST-HIT on the same observable, so the refusal above is not merely
        # this fixture failing to produce a closable row: strip the polarity
        # and the SAME state is admitted.
        self.assertEqual(dispatches.advisory_close_error(row),
                         "the verdict declares a polarity, so it is not advisory")
        undeclared = dict(row)
        undeclared.pop("polarity")
        self.assertIsNone(dispatches.advisory_close_error(undeclared))

    def test_an_outside_tier_verdict_is_a_different_class_and_is_refused(self):
        """The tier axis and the polarity axis are independent — see the class
        docstring for the decision and where it was read from."""
        row = self._verdicted("approve", "advisory-outside-tier")
        outside = dict(row, verdict_tier_evidence={
            "v": 1, "context": {}, "policy_version": "p1", "state": "outside",
            "reason": "the reviewer is outside the recorded approval tier",
            "kind": None}, verdict_tier_anchor="f" * 32)
        self.assertEqual(
            dispatches.advisory_close_error(outside),
            "the verdict declares a polarity, so it is not advisory")
        # THE CONTROL THAT MAKES THE DECISION LEGIBLE: the same outside-tier
        # evidence over a verdict that declared NO direction IS admitted, so
        # the refusal above is about the polarity and never about the tier.
        outside.pop("polarity")
        self.assertIsNone(dispatches.advisory_close_error(outside))

    def test_a_row_the_lr_ladder_already_retired_is_refused(self):
        """28 of the live ledger's 29 polarity-less verdict rows are this
        shape. A cancel appended over a `close --reason landed` would be a
        second, contradictory claim about the same work."""
        row = self._polarityless("a1d1f0a2", lane="advisory-retired")
        self.assertIsNone(dispatches.advisory_close_error(row))   # must-hit
        retired = dict(row, close_reason="landed")
        self.assertEqual(dispatches.advisory_close_error(retired),
                         "already retired by close --reason landed")

    def test_the_close_is_idempotent_and_refuses_a_different_reason(self):
        row = self._polarityless("a1d1f0a3", lane="advisory-idempotent")
        first, why = dispatches.mark_cancel(row["id"], "read it; nothing owed")
        self.assertIsNone(why)
        self.assertIs(first.get("cancel_advisory"), True)   # the must-hit
        before = self._lines()
        self.assertTrue(before, "the fixture wrote no ledger")
        again, why = dispatches.mark_cancel(row["id"], "read it; nothing owed")
        self.assertIsNone(why)
        self.assertEqual(again["cancel_reason"], first["cancel_reason"])
        self.assertEqual(self._lines(), before)
        _o, why = dispatches.mark_cancel(row["id"], "a different sentence")
        self.assertIn("already cancelled", why)
        self.assertEqual(self._lines(), before)

    def test_the_operator_reason_is_budgeted_against_the_composed_string(self):
        """THE REDUCER BUDGETS THE COMPOSED STRING, so the writer must too. A
        reason the writer accepts and the fold then drops leaves the CLI
        printing a terminal over a row every later read finds live."""
        row = self._polarityless("a1d1f0a4", lane="advisory-budget")
        fits = "x" * dispatches._ADVISORY_REASON_CAP
        over = "x" * (dispatches._ADVISORY_REASON_CAP + 1)
        _o, why = dispatches.mark_cancel(row["id"], over)
        self.assertIsNotNone(why)
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["status"],
                         "verdict")
        out, why = dispatches.mark_cancel(row["id"], fits)
        self.assertIsNone(why)
        self.assertEqual(len(out["cancel_reason"]),
                         dispatches._CANCEL_REASON_CAP)
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["status"],
                         "cancelled")

    def test_a_bare_cancel_event_over_a_verdict_row_still_drives_nothing(self):
        """The replay admits exactly what the writer admits. An event carrying
        no `advisory` marker is the shape every writer before this door wrote,
        and it must stay inert over a terminal row."""
        row = self._polarityless("a1d1f0a5", lane="advisory-bare-event")
        self.assertTrue(eventledger.append(dispatches.ledger_path(), {
            "v": 3, "event": "cancel", "seq": row["seq"] + 1, "id": row["id"],
            "ts": dispatches.pk.now_ts(), "reason": "hand-appended"}))
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["status"],
                         "verdict")
        # MUST-HIT: the SAME event with the marker is taken, so the arm above
        # measures the marker and not a fixture that could never fold.
        self.assertTrue(eventledger.append(dispatches.ledger_path(), {
            "v": 3, "event": "cancel", "seq": row["seq"] + 1, "id": row["id"],
            "ts": dispatches.pk.now_ts(), "reason": "marked", "advisory": True}))
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["status"],
                         "cancelled")

    def test_the_pipeline_reader_stops_counting_the_closed_row(self):
        """THE REAL CLASSIFIER, never a reimplementation of it:
        `obligation.unanswered_fixes` is the population owed-bot re-delivers
        and `dispatches.untriaged` is the sentence `triage <id>` prints."""
        from helm import obligation
        row = self._polarityless("a1d1f0a6", lane="advisory-pipeline")
        snap = dispatches.snapshot()[0]
        items, _forks, unavailable = obligation.unanswered_fixes(snap, None)
        self.assertIsNone(unavailable)
        before = [i for i in items if i["row"] == row["id"]]
        self.assertEqual(len(before), 1, "the fixture row never reached the "
                                         "classifier, so this arm measures "
                                         "nothing about the cure")
        self.assertEqual(before[0]["kind"], obligation.UNDECLARED_VERDICT)
        self.assertEqual(dispatches.untriaged(row, snap)[0], "VERDICT")

        _out, why = dispatches.mark_cancel(row["id"], "read it; nothing owed")
        self.assertIsNone(why)
        snap = dispatches.snapshot()[0]
        items, _forks, unavailable = obligation.unanswered_fixes(snap, None)
        self.assertIsNone(unavailable)
        self.assertEqual([i for i in items if i["row"] == row["id"]], [])
        self.assertEqual(dispatches.untriaged(snap[row["id"]], snap)[0],
                         "CANCELLED")

    def test_the_owed_bot_line_names_the_door_that_now_works(self):
        """The nag said "cancel the row" while every door refused it. It must
        name the command, the exact id, and what the close records."""
        from helm import obligation
        row = self._polarityless("a1d1f0a7", lane="advisory-nag")
        items, _forks, _u = obligation.unanswered_fixes(
            dispatches.snapshot()[0], None)
        item = next(i for i in items if i["row"] == row["id"])
        self.assertIn("helm dispatch cancel %s" % row["id"][:12], item["what"])
        self.assertIn("ADVISORY CLOSE", item["what"])
        # THE NAMED COMMAND IS THE ONE THAT WORKS, asserted by EXECUTING it
        # rather than by reading the sentence: a nag naming a refusing door is
        # the whole defect, and only the door can say which it is.
        argv = ["cancel", row["id"][:12], "read it; nothing owed"]
        rc, out, err = run(dispatches.cmd_dispatch, argv)
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("ADVISORY-CLOSED", out)

    def test_an_open_row_is_still_cancelled_without_the_advisory_prefix(self):
        """The ordinary cancel is untouched: same reason, same full budget, no
        marker — so the second admission did not leak into the first."""
        row = self.add(lane="advisory-untouched-open")
        out, why = dispatches.mark_cancel(row["id"], "recipient gone")
        self.assertIsNone(why)
        self.assertEqual(out["cancel_reason"], "recipient gone")
        self.assertIsNone(out.get("cancel_advisory"))
        replayed = dispatches.snapshot()[0][row["id"]]
        self.assertEqual((replayed["status"], replayed["cancel_reason"]),
                         ("cancelled", "recipient gone"))
        self.assertIsNone(replayed.get("cancel_advisory"))
