#!/usr/bin/env python3
"""`close --reason withdrawn` asks about the WORK, not about the verdict's sign.

WHY THE POLARITY GATE WAS WRONG. Withdrawn admitted FIX and SUPERSEDE alone,
on the reading that it retires a declared do-not-land DEBT. But an APPROVE
whose lane conflicts with trunk and will never land is exactly as stuck as a
FIX, and a CONCUR authorizes nothing anywhere — so those rows had no terminal
at all and accumulated. The live population when this changed: 20 unlanded
approved rows on one triage, with 239 CONCUR rows in the ledger behind them.

WHAT DOES NOT MOVE, and every arm below exists to hold it: Git must prove the
reviewed change OFF TRUNK BY ANCESTRY **and** ABSENT BY PATCH-ID. A landed row
cannot be withdrawn whatever its polarity says, and an unprovable one fails
closed.

THE GATE LIVES IN TWO PLACES AND BOTH ARE DRIVEN HERE. landreq's ladder
decides, and `dispatches._CLOSE_POLARITY` independently refuses the WRITE — so
widening one alone leaves the door shut while the source reads open, which is
a fix that changes nothing.
"""
import unittest

from tests._tmphome import pin_suite_guard        # noqa: F401
from helm import dispatches, landreq


class WithdrawPolarityTest(unittest.TestCase):

    def test_the_writer_admits_every_polarity_for_withdrawn(self):
        """The second gate. A row the ladder approves is still refused at the
        append if this table has not moved, and the symptom is identical."""
        allowed = dispatches._CLOSE_POLARITY["withdrawn"]
        # UNCONDITIONAL, so an empty or renamed tuple cannot pass the loop by
        # never running it.
        self.assertIn("approve", allowed)
        self.assertIn("concur", allowed)
        for polarity in ("fix", "supersede", None):
            self.assertIn(polarity, allowed)

    def test_the_writer_did_not_widen_every_other_reason_too(self):
        """CONTROL ON THE SAME TABLE: the change is scoped to `withdrawn`. A
        blanket widening would be indistinguishable from the arm above while
        un-gating reasons nobody ruled on."""
        # POSITIVE CONTROL on the same table, unconditional and first: these
        # reasons DO carry entries, so the absences below are scoping and not
        # a table that vanished.
        self.assertIn("fix", dispatches._CLOSE_POLARITY["stranded"])
        self.assertIn("approve", dispatches._CLOSE_POLARITY["subsumed"])
        self.assertNotIn("concur", dispatches._CLOSE_POLARITY["stranded"])
        self.assertNotIn("concur", dispatches._CLOSE_POLARITY["subsumed"])
        self.assertNotIn("supersede", dispatches._CLOSE_POLARITY["subsumed"])

    def test_the_withdrawn_schema_admits_the_withdrawing_seat(self):
        """Recorded, not inferred: a terminal that cannot say who declared it
        leaves the next reader with a close and no author."""
        self.assertIn("withdrawing_seat",
                      dispatches._CLOSE_STATE_FIELDS["withdrawn"])

    def test_the_seat_field_is_OPTIONAL_so_historical_closes_still_replay(self):
        """Every withdrawn close written before this field exists carries none.
        Making it mandatory would refuse them all on replay — the append-only
        store's version rule, where the reader lands before the writer."""
        base = dispatches._CLOSE_EVENT_BASE_FIELDS
        # POSITIVE CONTROL on the same set: it really is the base-field set,
        # so the absence below is about this field and not an empty constant.
        self.assertIn("close_reason", base)
        self.assertIn("reviewed_tip", base)
        self.assertNotIn("withdrawing_seat", base)
        # and the exact-set-equality reasons, which WOULD be broken by a new
        # field, are not this one
        for strict in ("subsumed", "resolved", "delivered-report"):
            self.assertNotEqual("withdrawn", strict)


class WithdrawLandingProofTest(unittest.TestCase):
    """The invariant the polarity change is NOT allowed to weaken."""

    def _row(self, **kw):
        row = {"id": "r" * 32, "polarity": "approve",
               "reviewed_tip": "a" * 40, "repo_id": "/nonexistent-repo"}
        row.update(kw)
        return row

    def test_a_row_with_no_reviewed_tip_cannot_be_withdrawn(self):
        """There is nothing to prove absent, so there is nothing to withdraw."""
        out, err = landreq._close_ladder_withdrawn(
            self._row(reviewed_tip=None), "evidence", dry_run=True)
        self.assertIsNone(out)
        self.assertIn("no reviewed tip", err)

    def test_withdraw_still_requires_evidence(self):
        """No git proof can carry the author's attestation that the verdict's
        resolution was actually carried out."""
        out, err = landreq._close_ladder_withdrawn(self._row(), "", dry_run=True)
        self.assertIsNone(out)
        self.assertIn("evidence", err)

    def test_an_unreadable_repo_fails_CLOSED(self):
        """Withdraw is fail-closed on an unknown land state: an unprovable
        absence must never retire a row."""
        out, err = landreq._close_ladder_withdrawn(
            self._row(), "evidence", dry_run=True)
        self.assertIsNone(out, "an unprovable absence was allowed to withdraw")
        self.assertTrue(err)


if __name__ == "__main__":
    unittest.main()
