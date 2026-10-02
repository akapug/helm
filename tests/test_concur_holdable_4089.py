#!/usr/bin/env python3
"""A clean CONCUR at a review row's exact tip can be held source-clean.

THE STATE (task/4089). A CONCUR is the verdict that authorizes nothing — it
endorses a read, and once it is written the row is a verdict, and a verdict
row cannot be held: `mark_hold` refuses it with "only an OPEN row can be
held". So every seat whose read of a clean review came out as a CONCUR paid
a SECOND hold-only row for the same claim (six such rows measured in one
evening), even though a CONCUR at the row's own exact reviewed tip IS a
clean read of that tip.

THE RULING THESE ARMS PIN. Such a CONCUR may be held source-clean on the
same row: the hold binds the CONCUR's tip and only it, so no superseding row
is needed. The four boundaries, each refusal paired with the admission that
bounds it in the same fixture:

  1. a CONCUR at the row's own tip, held at that same tip, is ACCEPTED — the
     row is `held` and rides the land train as a source-clean car under the
     doors a clean read rides them under (the car predicate keys on the hold,
     never on the polarity, so a held CONCUR and a held clean read are the
     one same car);
  2. a CONCUR held at a DIFFERENT tip is REFUSED — the hold's claim is the
     CONCUR's read, and that tip is not the one it read;
  3. a FIX held source-clean at its own tip is REFUSED — a finding is not a
     clean read, and the only pole admitted is the one that authorizes
     nothing;
  4. a hold on an OPEN row is UNCHANGED — an ordinary hold and a
     source-clean hold on an open row pass exactly as before.

Every refusal asserts the EFFECT (nothing appended, the row still a verdict,
no hold stamp) rather than only the absence of a complaint.
"""
import unittest
from unittest import mock

from helm import (dispatches, dispatches_close, eventledger, home,
                  landreq, seats, store)
# The module, never its TestCase: tests/test_suite_collection.py says why.
from tests import test_source_clean_landed as _sc

READER = _sc.READER


def setUpModule():
    from tests._tmphome import pin_live_seats
    pin_live_seats()


REASON = "CONCUR source-clean: its clean read at its own tip"


class ConcurHoldableBase(_sc.SourceCleanBase):
    """The 3053 fixture's door machinery. Rows are pointed at a divergent
    tip, so a CONCUR can read it clean and it is not on trunk: every arm is
    the one row shape over one tip, and a refusal can never be the row being
    unholdable for some unrelated reason."""

    def concur(self, lane, tip=None, polarity="concur",
               evidence="reviewed; fab Ran 4 tests in 0.1s OK"):
        """A verdict THROUGH THE DOOR on a fresh row, pointed at `tip` (the
        fixture's `side` unless said otherwise). A CONCUR sets the row to a
        verdict, which is the state every arm below starts from."""
        tip = tip if tip is not None else self.side
        row = self.row(tip, lane)
        out, why = self.mark_verdict(row["id"], tip, evidence,
                                     polarity=polarity)
        self.assertIsNone(why, why)
        self.assertEqual(out["status"], "verdict")
        return row

    def clean_read(self, lane, tip=None):
        """THE POSITIVE CONTROL: a row still OPEN whose read is clean, held
        source-clean through the same door — so no refusal below can be a
        door that refuses everything, and the concur hold is measured
        against the clean read it must equal on the car surface."""
        tip = tip if tip is not None else self.side
        row = self.row(tip, lane)
        self.hold(row, tip)
        return row

    def concur_held(self, row, tip, reason=REASON):
        """Hold `row` source-clean at `tip` through the door, as the READER
        the 3053 fixture declares; returns the writer's answer."""
        with mock.patch.object(dispatches, "_acting_author",
                               return_value=(READER, None)), \
                mock.patch.object(home, "session_id",
                                  return_value=self.READER_SESSION):
            return dispatches.mark_hold(row["id"], reason,
                                        source_clean_tip=tip)

    def descendant_tip(self, branch):
        """One commit off the fixture's `side`, on a ref of its own — a tip
        that DESCENDS from `side` (so the lineage rung passes) but is not
        `side` itself, the shape a CONCUR's own tip has for a hold at a
        different tip. On a branch of its own, off `side`, never the trunk
        the fixture rides."""
        self.git("checkout", "-q", "side")
        self.git("checkout", "-q", "-b", branch)
        tip = self.commit("a read the CONCUR did not make", path="desc")
        self.git("checkout", "-q", self.main)
        return tip

    def verdicts_of(self, rid):
        return [e for e in eventledger.events(dispatches.ledger_path())
                if e.get("id") == rid and e.get("event") == "verdict"]

    def holds_of(self, rid):
        return [e for e in eventledger.events(dispatches.ledger_path())
                if e.get("id") == rid and e.get("event") == "hold"]


class ConcurHoldAdmittedTest(ConcurHoldableBase):
    """Arm 1: a CONCUR at the row's own tip held at that tip is the clean
    read the land train always took — one row, no second hold-only row,
    riding under the same doors as any clean read."""

    def test_a_concur_at_its_own_tip_is_held_source_clean(self):  # noqa: VACUOUS_ASSERTION — the clean_read control passes through the same door on the same fixture before the claim, so the hold reaching the door is proven, and the status+tip+polarity+approval reads plus the car predicate are the effect, not a door that admits everything
        # THE CONTROL: the same door on a still-open row in this fixture,
        # so a refusal below is the concur boundary holding, not a door
        # that refuses everything.
        control = self.clean_read("lane/concur4089-control")
        self.assertEqual(self.state(control["id"])["status"], "held")
        # THE CLAIM: a CONCUR at its own tip, held at that tip, is admitted.
        row = self.concur("lane/concur4089-own-tip")
        out, why = self.concur_held(row, self.side)
        self.assertIsNone(why,
                          "a CONCUR at its own exact tip held at that tip was "
                          "refused: %s" % why)
        folded = self.state(row["id"])
        self.assertEqual(folded["status"], "held",
                         "the fold dropped the standing CONCUR's hold: the "
                         "row is still a verdict, so a hold the door admits "
                         "would not land")
        self.assertEqual(folded.get("source_clean_tip"), self.side)
        self.assertEqual(folded.get("polarity"), "concur",
                         "the hold folded over the polarity: the row is "
                         "still the CONCUR that authorizes nothing")
        self.assertIn("hold_approval", folded,
                      "the hold's proof did not ride with the claim, as a "
                      "clean read's does")
        # THE ONE CAR SIGNAL: the car predicate keys on the hold, never the
        # polarity — so the held CONCUR rides the train under the same doors
        # as the control's clean read, and no second row exists. Both are
        # asked through the projection the train reads, on the same row.
        projected, why2 = landreq.project()
        self.assertIsNone(why2, why2)
        self.assertEqual(landreq.source_clean_car(projected[control["id"]]),
                         (self.side, None))
        self.assertEqual(landreq.source_clean_car(projected[row["id"]]),
                         (self.side, None))


class ConcurHoldRefusedTest(ConcurHoldableBase):
    """Arms 2 and 3: the hold's claim is the CONCUR's read, so a tip the
    CONCUR did not make is refused, and the one pole admitted is the one
    that authorizes nothing."""

    def test_a_concur_held_at_a_different_tip_is_refused(self):  # noqa: VACUOUS_ASSERTION — the admitting control is the SAME row shape held at the tip the CONCUR actually read, which passes through this door first; the refusal below is the tip boundary discriminating on the same fixture
        # THE ADMITTING CONTROL: the same row shape held at the tip the
        # CONCUR read passes, so the refusal that follows is the tip
        # boundary, not a door that refuses everything.
        control = self.concur("lane/concur4089-tip-control")
        out, why = self.concur_held(control, self.side)
        self.assertIsNone(why, "the control hold was refused: %s" % why)
        # THE CLAIM: a CONCUR whose read is `side` (the row's own tip), held
        # at a tip that descends from `side` but is not it (lineage passes,
        # so the refusal is the tip binding, not the lineage rung), is
        # refused — the hold's claim is the CONCUR's read, and that tip is
        # not one.
        desc = self.descendant_tip("desc-diff")
        row = self.concur("lane/concur4089-diff-tip")
        before = self.history()
        out, why = self.concur_held(row, desc)
        self.assertIsNone(out)
        self.assertIsNotNone(why,
                             "a CONCUR at %s held at %s was admitted: the "
                             "hold's claim is the CONCUR's read, and that "
                             "tip is not one" % (self.side[:12], desc[:12]))
        # THE EFFECT, not only the complaint: nothing moved on the row.
        self.assertEqual(self.history(), before,
                         "the refused hold appended an event")
        folded = self.state(row["id"])
        self.assertEqual(folded["status"], "verdict",
                         "a refused hold moved the row: %r"
                         % folded["status"])
        self.assertEqual(len(self.verdicts_of(row["id"])), 1)
        self.assertEqual(self.holds_of(row["id"]), [])

    def test_a_fix_held_source_clean_is_refused(self):  # noqa: VACUOUS_ASSERTION — the admitting control is a CONCUR held at its own tip through this same door in the same fixture; the refusal below is the polarity boundary discriminating on the one pole that authorizes something
        # THE ADMITTING CONTROL ON THE SAME DOOR: a CONCUR held at its own
        # tip passes, so the refusal that follows is the polarity boundary,
        # not a door that refuses everything.
        control = self.concur("lane/concur4089-pole-control")
        out, why = self.concur_held(control, self.side)
        self.assertIsNone(why, "the control hold was refused: %s" % why)
        # THE CLAIM: a FIX at its own tip (lineage passes), held source-clean,
        # is refused — a finding is not a clean read, and the only pole
        # admitted is the one that authorizes nothing.
        row = self.concur("lane/concur4089-fix", polarity="fix",
                          evidence="a finding")
        before = self.history()
        out, why = self.concur_held(row, self.side)
        self.assertIsNone(out)
        self.assertIsNotNone(why,
                             "a FIX held source-clean was admitted: a "
                             "finding is not a clean read, and the only "
                             "pole admitted is the one that authorizes "
                             "nothing")
        self.assertEqual(self.history(), before,
                         "the refused hold appended an event")
        folded = self.state(row["id"])
        self.assertEqual(folded["status"], "verdict")
        self.assertEqual(folded.get("polarity"), "fix")
        self.assertEqual(self.holds_of(row["id"]), [])


class OpenRowHoldUnchangedTest(ConcurHoldableBase):
    """Arm 4: a hold on an OPEN row passes exactly as before — the carve-out
    reaches no row the door never refused."""

    def test_a_source_clean_hold_on_an_open_row_is_unchanged(self):  # noqa: VACUOUS_ASSERTION — the arm drives a real write and reads the status, tip and holder back from the fold; an inert door fails the held-status read
        row = self.clean_read("lane/concur4089-open")
        folded = self.state(row["id"])
        self.assertEqual(folded["status"], "held")
        self.assertEqual(folded.get("source_clean_tip"), self.side)
        self.assertIn("hold_approval", folded)
        # AND THE ORDINARY (no claim) HOLD on an open row, which the
        # boundary must not touch either.
        plain = self.row(self.side, "lane/concur4089-plain")
        with mock.patch.object(dispatches, "_acting_author",
                               return_value=(READER, None)), \
                mock.patch.object(home, "session_id",
                                  return_value=self.READER_SESSION):
            out, why = dispatches.mark_hold(plain["id"], "waiting on a "
                                             "build box")
        self.assertIsNone(why, "an ordinary open-row hold was refused: %s"
                         % why)
        self.assertEqual(out["status"], "held")
        self.assertNotIn("source_clean_tip", self.state(plain["id"]))


class ConcurHoldReleasesToItsVerdictTest(ConcurHoldableBase):
    """Every move off a hold over a CONCUR returns that CONCUR: a release
    restores the verdict (never an OPEN row a second verdict could be written
    over), and a cancel is refused as it is for any reviewed row."""

    def release(self, row):
        with mock.patch.object(dispatches, "_acting_author",
                               return_value=(READER, None)):
            return dispatches.mark_release(row["id"])

    def test_a_release_returns_the_concur_and_a_fix_cannot_follow(self):  # noqa: VACUOUS_ASSERTION — the open-row control releases to OPEN through the same door first, and the concur row's released status, polarity and refused re-verdict are read back from the fold
        # THE CONTROL: an open row's hold still releases to OPEN.
        control = self.clean_read("lane/concur4089-release-control")
        _out, why = self.release(control)
        self.assertIsNone(why, why)
        self.assertEqual(self.state(control["id"])["status"], "open")
        # THE CLAIM: a hold over a CONCUR releases to that CONCUR.
        row = self.concur("lane/concur4089-release")
        _out, why = self.concur_held(row, self.side)
        self.assertIsNone(why, why)
        out, why = self.release(row)
        self.assertIsNone(why, why)
        self.assertEqual(out["status"], "verdict",
                         "the writer released a CONCUR to %r" % out["status"])
        folded = self.state(row["id"])
        self.assertEqual(folded["status"], "verdict",
                         "the fold released a CONCUR to %r: an OPEN row "
                         "takes a second verdict over the first"
                         % folded["status"])
        self.assertEqual(folded.get("polarity"), "concur")
        self.assertNotIn("source_clean_tip", folded)
        # AND THE VERDICT STAYS IMMUTABLE: a FIX over it is refused.
        before = self.history()
        _out, why = self.mark_verdict(row["id"], self.side, "a finding",
                                      polarity="fix")
        self.assertIsNotNone(why, "a FIX was written over the released "
                                  "CONCUR")
        self.assertEqual(self.history(), before)
        self.assertEqual(len(self.verdicts_of(row["id"])), 1)
        # AND IT MAY BE HELD AGAIN AT ITS OWN TIP.
        _out, why = self.concur_held(row, self.side)
        self.assertIsNone(why, why)
        self.assertEqual(self.state(row["id"])["status"], "held")

    def test_a_cancel_of_a_hold_over_a_concur_is_refused(self):  # noqa: VACUOUS_ASSERTION — the open-row control cancels through the same door first, so the refusal is the concur boundary, and the effect is read back from the ledger and the fold
        control = self.clean_read("lane/concur4089-cancel-control")
        _out, why = dispatches.mark_cancel(control["id"], "moot")
        self.assertIsNone(why, why)
        self.assertEqual(self.state(control["id"])["status"], "cancelled")
        row = self.concur("lane/concur4089-cancel")
        _out, why = self.concur_held(row, self.side)
        self.assertIsNone(why, why)
        before = self.history()
        out, why = dispatches.mark_cancel(row["id"], "moot")
        self.assertIsNone(out)
        self.assertIn("HELD over its own CONCUR", why or "")
        self.assertEqual(self.history(), before,
                         "the refused cancel appended an event")
        self.assertEqual(self.state(row["id"])["status"], "held")

    def test_the_fold_ignores_a_cancel_appended_over_a_held_concur(self):  # noqa: VACUOUS_ASSERTION — the appended event is the shape the open-row cancel writes, and the held status and CONCUR polarity are read back from the fold
        row = self.concur("lane/concur4089-cancel-fold")
        _out, why = self.concur_held(row, self.side)
        self.assertIsNone(why, why)
        state = self.state(row["id"])
        self.assertTrue(eventledger.append(dispatches.ledger_path(), {
            "v": 3, "event": "cancel", "seq": int(state["seq"]) + 1,
            "id": row["id"], "ts": dispatches.pk.now_ts(),
            "reason": "moot"}))
        folded = self.state(row["id"])
        self.assertEqual(folded["status"], "held")
        self.assertEqual(folded.get("polarity"), "concur")


class AHeldConcurClosesWhenItLandsTest(ConcurHoldableBase):
    """The land's half of the held CONCUR. Once the train ships the held tip,
    `source-clean-landed` is the one door that ends the row: the CONCUR's
    `(None,)`-only domain is untouched, and the door reads a row HELD over
    its own standing CONCUR at that CONCUR's exact tip as the source-clean
    hold it is (`_concur_held_source_clean`). Every other door still sees a
    CONCUR, so a held CONCUR whose hold names another tip, or a CONCUR that
    is not held, is refused exactly as before."""

    def forged_for(self, real, row, tip):
        """The real writer's close re-aimed at `row` with `tip` as its read
        and a re-sealed anchor -- the best forgery there is, so the only
        thing a refusal of it can be is the row it was aimed at."""
        state = self.state(row["id"])
        event = dict(real, id=row["id"], seq=int(state["seq"]) + 1,
                     ts=dispatches.pk.now_ts(), reviewed_tip=tip,
                     source_clean_hold_actor=state["hold_actor"],
                     source_clean_hold_ts=state["hold_ts"])
        event.pop("close_actor", None)
        event["source_clean_anchor"] = \
            dispatches_close._source_clean_anchor(event)
        return event

    def test_a_held_concur_closes_source_clean_landed_once_its_tip_lands(self):  # noqa: VACUOUS_ASSERTION — the plain clean read rehearses through the same door first, the ledger count is asserted to grow by exactly one, and the closed status, reason, gate and unchanged verdict count are read back from the fold
        token = self.mint(self.c)
        # THE CONTROL: a plain clean read held at `b` qualifies through the
        # same door against the same receipt.
        self.control(token)
        row = self.concur("lane/concur4089-landed", tip=self.b)
        _out, why = self.concur_held(row, self.b)
        self.assertIsNone(why, why)
        before = self.history()
        out, err = self.close(row, token)
        self.assertIsNone(err, "a CONCUR held source-clean at its own tip, "
                               "landed under a verified whole-suite gate, "
                               "could not close: %s" % err)
        self.assertEqual(self.history(), before + 1)
        event = self.close_event(row["id"])
        self.assertEqual(event["close_reason"], _sc.REASON)
        self.assertEqual(event["reviewed_tip"], self.b)
        self.assertEqual("gate:" + event["source_clean_gate"], token)
        folded = self.state(row["id"])
        self.assertEqual(folded["status"], "closed")
        self.assertEqual(folded["close_reason"], _sc.REASON)
        # NOTHING ELSE MOVED: still the one CONCUR, no verdict minted.
        self.assertEqual(folded.get("polarity"), "concur")
        self.assertEqual(len(self.verdicts_of(row["id"])), 1)
        lr, lerr = landreq.get(row["id"])
        self.assertIsNone(lerr, lerr)
        self.assertTrue(lr["terminal"])
        self.assertEqual(lr["owed_by"], "nobody")

    def test_a_held_concur_whose_hold_names_another_tip_is_refused(self):  # noqa: VACUOUS_ASSERTION — the same forged event aimed at the held CONCUR at its own tip binds through the same validator first, so the refusal is the tip binding and nothing else
        token = self.mint(self.c)
        done = self.concur("lane/concur4089-template", tip=self.b)
        _out, why = self.concur_held(done, self.b)
        self.assertIsNone(why, why)
        _out, err = self.close(done, token)
        self.assertIsNone(err, err)
        real = self.close_event(done["id"])
        victim = self.concur("lane/concur4089-victim", tip=self.b)
        _out, why = self.concur_held(victim, self.b)
        self.assertIsNone(why, why)
        rows, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable, unavailable)
        state = rows[victim["id"]]
        # THE POSITIVE CONTROL: aimed at the CONCUR's own tip, it binds.
        self.assertIsNone(dispatches._close_event_error(
            self.forged_for(real, victim, self.b), state, current=rows))
        # THE CLAIM: the same row whose hold names a tip the CONCUR did not
        # read. Neither the hold door nor the fold can produce it, so the
        # state is the folded row with only the hold's tip moved, and the
        # event names that tip, so the tip-equality rung passes and the
        # refusal is the polarity the door now reads as a CONCUR.
        other = self.c
        moved = dict(state, source_clean_tip=other)
        why = dispatches._close_event_error(
            self.forged_for(real, victim, other), moved, current=rows)
        self.assertIn("polarity outside --reason source-clean-landed", why or "")
        self.assertFalse(dispatches._concur_held_source_clean(moved))
        self.assertTrue(dispatches._concur_held_source_clean(state))

    def test_a_concur_that_is_not_held_keeps_its_own_door(self):  # noqa: VACUOUS_ASSERTION — the refused close is asserted to append nothing and leave the verdict standing, and endorsement-moot then closes the SAME row, so the refusal is the hold rung and not a dead row
        token = self.mint(self.c)
        self.control(token)
        row = self.concur("lane/concur4089-unheld", tip=self.b)
        before = self.history()
        out, err = self.close(row, token)
        self.assertIsNone(out)
        self.assertIn("carries no source-clean hold", err or "")
        self.assertEqual(self.history(), before)
        self.assertEqual(self.state(row["id"])["status"], "verdict")
        self.assertFalse(dispatches._concur_held_source_clean(
            self.state(row["id"])))
        # ITS DOOR IS STILL endorsement-moot, exactly as on main.
        _out, err = landreq.close(row["id"], "endorsement-moot",
                                  repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(self.state(row["id"])["close_reason"],
                         "endorsement-moot")


if __name__ == "__main__":
    unittest.main()
