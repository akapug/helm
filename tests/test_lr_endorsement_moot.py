#!/usr/bin/env python3
"""`endorsement-moot` — the door for a CONCUR over work that LANDED.

THE HOLE THIS DOOR FILLS, and why a "it closes" arm alone would prove
nothing. A concur is the one verdict that authorizes nothing, so only the two
ABSENCE doors ever admitted it, and each requires Git to prove the reviewed
work OFF trunk. A concur whose work is ON trunk was therefore refused by all
ten doors — and an arm showing the eleventh closes such a row passes equally
against a door that closes everything. Every arm here that asserts an
admission is paired with the refusal that bounds it, measured in the same
fixture and usually in the same call.

Hermetic through `CloseBase`, whose two disciplines carry here: a refusal arm
asserts the EFFECT rather than the absence of a complaint, and an absence
claim is preceded by an unconditional positive control on the same observable.
"""
import unittest

from helm import dispatches, eventledger, landreq, rowstate
# The module, never its TestCase: tests/test_suite_collection.py says why.
from tests import test_lr_close as _close


class EndorsementMootBase(_close.CloseBase):
    """Rows on both sides of the one fact this door measures.

    `self.b` is a commit ON the fixture's trunk and `self.side` is the
    fixture's divergent tip, which no trunk reaches. Every arm below is the
    same row shape over one of those two tips, so a refusal can never be the
    row being unclosable for some unrelated reason.
    """

    def history(self):
        return len(list(eventledger.events(dispatches.ledger_path())))

    def row_at(self, tip, polarity="concur", lane="lane/moot"):
        row = self.dispatch(ref=tip, lane=lane, kind="review")
        self.mark_verdict(row["id"], tip, "reviewed", polarity=polarity)
        return row

    def landed_concur(self, lane="lane/moot"):
        return self.row_at(self.b, "concur", lane)

    def absent_concur(self, lane="lane/moot-absent"):
        return self.row_at(self.side, "concur", lane)


class TheDoorOpensTest(EndorsementMootBase):
    """THE ARMS THAT DRIVE A REAL CLOSE.

    A dry run cannot reach the persistence table, the polarity table, the
    event validator or the retry identity — `_CLOSE_STATE_FIELDS` decides
    which keys the writer copies onto the event, and a reason missing from it
    persists NOTHING while every dry-run assertion stays green. These arms
    read the PERSISTED event back off the ledger, which is the only thing that
    can tell.
    """

    def test_a_landed_concur_closes_and_persists_its_whole_proof(self):  # noqa: VACUOUS_ASSERTION — the assertIsNone on err is a precondition, not the claim; the unconditional positive controls are the assertGreater on the ledger count and the four persisted-field reads off the event, each of which a missing _CLOSE_STATE_FIELDS entry makes fail
        row = self.landed_concur()
        before = self.history()
        out, err = landreq.close(row["id"], "endorsement-moot", repo=self.repo)
        self.assertIsNone(err, err)
        self.assertGreater(self.history(), before, "the close appended no event")
        event = self.close_event(row["id"])
        # EACH FIELD IS ONE `_CLOSE_STATE_FIELDS` ADMITS, and a reason absent
        # from that table appends an event carrying none of them.
        self.assertEqual(event.get("close_proof_mode"),
                         "nonauthorizing-verdict-landed-tip")
        self.assertTrue(event.get("closing_repo_id"), event)
        self.assertTrue(event.get("closing_trunk_ref"), event)
        self.assertTrue(event.get("closing_trunk_sha"), event)
        # THE NONAUTHORIZATION HALF, stamped by the LOCK and not by the
        # ladder. Without it the record claims a tip on trunk and claims
        # nothing about the verdict, which is `landed` wearing another name.
        self.assertEqual(event.get("close_hold_kind"), "advisory")

    def test_the_persisted_evidence_is_the_measurement_not_a_caller_sentence(self):  # noqa: VACUOUS_ASSERTION — the claim is four unconditional assertIn checks on the PERSISTED evidence string; the assertIsNone on err only guards that the close happened at all
        row = self.landed_concur()
        out, err = landreq.close(row["id"], "endorsement-moot", repo=self.repo)
        self.assertIsNone(err, err)
        evidence = self.close_event(row["id"]).get("close_evidence") or ""
        self.assertIn(row["id"], evidence)
        self.assertIn("advisory", evidence)
        self.assertIn(self.b[:12], evidence)
        self.assertIn("authorizes nothing", evidence)

    def test_an_identical_retry_reconciles_instead_of_refusing(self):  # noqa: VACUOUS_ASSERTION — `after_first` is READ FROM THE LEDGER after a real close, so the equality is anchored to a count the first write is proven to have moved; a writer that appended nothing at all fails the first close's own assertIsNone
        """THE RETRY DOOR, which `chain-proof` and `expired` both shipped
        without: without a `_close_idempotent` branch an identical re-run of a
        proven close reads as a DIFFERENT closure and takes the retired-once
        refusal, so a caller whose write succeeded and whose answer was lost
        can never reconcile."""
        row = self.landed_concur()
        first, err = landreq.close(row["id"], "endorsement-moot", repo=self.repo)
        self.assertIsNone(err, err)
        after_first = self.history()
        again, err = landreq.close(row["id"], "endorsement-moot", repo=self.repo)
        self.assertIsNone(err, "an honest retry was refused: %s" % err)
        self.assertEqual(self.history(), after_first,
                         "the retry appended a second close event")

    def test_the_terminal_renders_SUPERSEDED_and_never_LANDED(self):
        """WHAT THE OPERATOR READS, and the one word that would launder the
        endorsement. The tip really is on trunk, so LANDED is the tempting
        rendering — and it would let every board and burn-down read a concur
        as an approved landing under this row's authority, which is exactly
        what the reason refuses to claim."""
        self.assertEqual(rowstate._CLOSE_TERMINAL["endorsement-moot"],
                         rowstate.SUPERSEDED)
        # MUST-HIT CONTROL: the table really does say LANDED for a door that
        # claims one, so the assertion above is a distinction and not a table
        # that says SUPERSEDED for everything.
        self.assertEqual(rowstate._CLOSE_TERMINAL["landed"], rowstate.LANDED)

    def test_it_is_registered_everywhere_a_reason_must_be(self):
        """The registration checklist as an arm. Five of these raise, drop or
        silently accept at the WRITE and are unreachable from a dry run."""
        self.assertIn("endorsement-moot", landreq.CLOSE_CLI_REASONS)
        self.assertIn("endorsement-moot", dispatches.CLOSE_REASONS)
        self.assertIn("endorsement-moot", dispatches._CLOSE_POLARITY)
        self.assertIn("endorsement-moot", dispatches._CLOSE_STATE_FIELDS)
        self.assertIn("endorsement-moot", rowstate._CLOSE_TERMINAL)
        self.assertIn("endorsement-moot", landreq.REPO_TRUNK_REASONS)
        self.assertEqual(dispatches.CLOSE_EXACT_PROOF_MODE["endorsement-moot"],
                         "nonauthorizing-verdict-landed-tip")
        self.assertTrue(callable(landreq._close_ladder_endorsement_moot))


class TheDoorStillRefusesTest(EndorsementMootBase):
    """THE OTHER DIRECTION, which is the half that makes the door mean
    something. Each arm runs the ADMITTING case first, in the same fixture and
    against the same ladder, so a refusal can never be a door that refuses
    everything."""

    def door(self, row):
        lr, err = landreq.get(row["id"])
        self.assertIsNone(err, err)
        return landreq._close_ladder_endorsement_moot(
            lr, None, self.repo, self.main, True)

    def control(self):
        """THE UNCONDITIONAL POSITIVE CONTROL: a landed concur dry-runs CLEAN
        through this exact ladder. Every refusal below is measured against the
        same call on the same fixture, so none of them can be a door that
        refuses everything."""
        ok, why = self.door(self.landed_concur(lane="lane/moot-control"))
        self.assertIsNotNone(ok, "the control row did not close: %s" % why)
        self.assertEqual(ok["proof_mode"], "nonauthorizing-verdict-landed-tip")

    def refuse(self, row):
        return self.door(row)

    def test_a_concur_whose_work_is_NOT_on_trunk_is_refused(self):
        """THE ARM THE BRIEF NAMES. `self.side` is on no trunk, so this row is
        the population `withdrawn` and `expired` exist for; admitting it here
        would make this a third absence door wearing a presence door's proof
        mode."""
        self.control()
        out, err = self.refuse(self.absent_concur())
        self.assertIsNone(out)
        self.assertIn("NOT on trunk", err)
        # THE REFUSAL NAMES WHERE THE ROW BELONGS, so the operator is not sent
        # back around the same doors.
        self.assertIn("withdrawn", err)

    def test_a_LANDED_APPROVE_is_refused_on_the_verdict_and_not_the_git(self):
        """THE POLARITY BOUNDARY. This row's tip is the SAME trunk commit the
        control closed on, so git cannot be what refuses it — the only thing
        that differs is the verdict, which is the whole discriminator."""
        self.control()
        out, err = self.refuse(self.row_at(self.b, "approve",
                                           lane="lane/moot-approve"))
        self.assertIsNone(out)
        self.assertIn("not an endorsement", err)
        self.assertIn("landed's row", err)
        self.assertNotIn("NOT on trunk", err,
                         "the approve was refused by the LANDING rung, so "
                         "this arm never reached the polarity gate it is for")

    def test_a_LANDED_FIX_is_refused_and_sent_to_its_own_door(self):
        self.control()
        out, err = self.refuse(self.row_at(self.b, "fix",
                                           lane="lane/moot-fix"))
        self.assertIsNone(out)
        self.assertIn("not an endorsement", err)
        self.assertIn("resolved", err)

    def test_an_unreadable_trunk_object_closes_nothing(self):  # noqa: ORPHANED_MOCK — `_object_exists` is called by the ladder under test, one rung after the trunk resolve; the walker does not follow the helper method this arm enters through. noqa: VACUOUS_ASSERTION — self.control() is the unconditional positive control: the same ladder admits an unpatched row in the same fixture
        """THE MASS-TERMINATION GUARD, inherited from `stranded`: a repository
        that cannot produce its own trunk object proves nothing about what
        reached it, in either direction."""
        from unittest import mock
        self.control()
        lr, _ = landreq.get(self.landed_concur(lane="lane/moot-blind")["id"])
        with mock.patch.object(landreq, "_object_exists", return_value=False):
            out, err = landreq._close_ladder_endorsement_moot(
                lr, None, self.repo, self.main, True)
        self.assertIsNone(out)
        self.assertIn("cannot prove its own trunk object", err)

    def test_the_writer_refuses_a_forged_hold_kind(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control is the assertIsNone on the REAL WRITER'S OWN event through the same validator, one line above the forgery; without it a validator refusing every event would satisfy the assertIsNotNone
        """THE CAPTURE MUST BIND THE RECORD, not merely name an allowed word.
        Without this the polarity law would hold only at the CLI: an event
        naming `advisory` over a stamped APPROVE would replay as a terminal.
        """
        row = self.landed_concur(lane="lane/moot-forge")
        # THE STATE THE VALIDATOR JUDGES AGAINST IS THE PRE-CLOSE ROW, read
        # from the real snapshot rather than invented — a hand-built state
        # proves only that the validator refuses a shape the test author made
        # up, which is the one shape no forger has to use.
        rows, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable, unavailable)
        state = rows[row["id"]]
        out, err = landreq.close(row["id"], "endorsement-moot", repo=self.repo)
        self.assertIsNone(err, err)
        event = self.close_event(row["id"])
        # UNCONDITIONAL POSITIVE CONTROL, ON THE SAME CALL: the event the REAL
        # WRITER wrote passes this validator. Without it an arm asserting a
        # forgery is refused would pass against a validator that refuses every
        # endorsement-moot event, including the honest ones.
        self.assertIsNone(dispatches._close_event_error(dict(event), state),
                          "the real writer's own event fails replay")
        forged = dict(event, close_hold_kind="authorization-held")
        self.assertIsNotNone(
            dispatches._close_event_error(forged, state),
            "the validator admitted a hold kind the row's own verdict "
            "evidence contradicts")


if __name__ == "__main__":
    unittest.main()
