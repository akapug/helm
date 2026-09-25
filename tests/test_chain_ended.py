#!/usr/bin/env python3
"""`ended` — WHETHER ANYONE STILL OWES ANYTHING, and the listing that asks it.

task/2861. `carrier` answers WHO HOLDS THIS NOW and is right to count an
approving, landed successor as holding it. This module covers its SIBLING
question, for which that same successor is the opposite answer: a chain that
reached a landed APPROVE is finished, and a reader told to chase it is sent
after work already on trunk.

IT LIVES IN ITS OWN MODULE BECAUSE `tests/test_dispatches.py` IS AT THE
NEVER-TRACK CEILING. Adding these arms there pushed it 7,578 bytes past 1.0
MiB and the guard refused the commit — which is task/2866 coming true by the
hand of the seat that filed it. The fixtures are REUSED rather than copied:
`_row` is the same classmethod the carrier arms use, so the two question's
fixtures cannot drift apart while the questions are meant to share a walk.
"""
import unittest
from unittest import mock

# THE MODULE, NEVER ITS TestCase CLASSES. `unittest discover` loads each test
# module and takes every TestCase subclass it finds in `dir(module)`, so a
# from-import BINDS those classes here and registers them a second time — same
# id both times, because an id is built from the class's own `__module__`. The
# reuse below is the point of this module and costs nothing; the BINDING is
# what doubled 31 tests, and every failure among them was counted twice on
# lane and trunk alike (a receipt read ten failures for seven tests). Reaching
# the same helpers through the module object keeps them out of `dir()` here.
from tests import test_dispatches
from tests.test_dispatches import run

from helm import dispatches

# THE SPELLINGS THE LIVE LEDGER REALLY WRITES, as three axes, in ONE place.
# Every sweep below is built from these rather than from the words a
# membership tuple happens to name, and they live at module scope so a
# vocabulary that grows cannot be updated for one sweep and missed by
# another -- which is the same drift, one layer up, that this module exists
# to cure. `closed_state` spells fifteen terminals on the live ledger and the
# chain module's membership tuples name nine, so the gap is the point.
LEDGER_STATUSES = ("open", "held", "verdict", "cancelled", "closed")
LEDGER_CLOSE_REASONS = (
    None, "landed", "carried", "withdrawn", "superseded", "stranded",
    "discharged", "subsumed", "expired", "retired_admin", "resolved",
    "abandoned", "delivered-report", "a-reason-from-next-year")
LEDGER_POLARITIES = (None, "fix", "approve", "concur", "supersede")


class EndedSemanticsTest(unittest.TestCase):
    """The predicate, on the same synthetic snapshots the carrier arms use."""

    CHAIN = test_dispatches.CarryingSemanticsTest.CHAIN
    # the SAME helper, never a copy
    _row = test_dispatches.CarryingSemanticsTest._row

    def _snap(self, *rows):
        return {r["id"]: r for r in rows}

    def test_a_LANDED_successor_ENDED_it_while_carrier_says_it_is_HELD(self):
        """THE TWO QUESTIONS DISAGREE ON PURPOSE, and this is the arm that
        says so. `carrier` is RIGHT that a landed approve holds the chain --
        something took the obligation and accounted for it. `ended` is right
        that nobody owes anything further. A listing that had only the first
        answer told its reader to chase work already on trunk."""
        snap = self._snap(
            self._row("p"),
            self._row("c", status="verdict", supersedes="p",
                      polarity="approve", close_reason="landed"))
        self.assertEqual((dispatches.carrier(snap["p"], snap) or {}).get("id"),
                         "c", "the control: carrier still sees a holder")
        self.assertEqual((dispatches.ended(snap["p"], snap) or {}).get("id"),
                         "c")

    def test_a_LIVE_successor_MOVED_the_debt_and_ENDED_nothing(self):
        """The negative that makes the positive mean something: an open
        successor took the obligation, so it is live somewhere and the row
        must keep reading as debt."""
        snap = self._snap(self._row("p"),
                          self._row("c", status="open", supersedes="p"))
        self.assertEqual((dispatches.carrier(snap["p"], snap) or {}).get("id"),
                         "c")
        self.assertIsNone(dispatches.ended(snap["p"], snap))

    def test_a_FIX_successor_is_the_NEXT_ROUND_and_ends_nothing(self):
        """A FIX is debt, not discharge. Its own docstring elsewhere says a
        finding is the next round of the same obligation."""
        snap = self._snap(
            self._row("p"),
            self._row("c", status="verdict", supersedes="p", polarity="fix"))
        # THE POSITIVE CONTROL ON THE SAME OBSERVABLE: the successor is real
        # and the walk reaches it, so the None below is this predicate's
        # answer about a FIX and not a fixture that built nothing.
        self.assertEqual((dispatches.carrier(snap["p"], snap) or {}).get("id"),
                         "c")
        self.assertIsNone(dispatches.ended(snap["p"], snap))

    def test_a_CANCELLED_successor_is_a_PASS_THROUGH_for_ended_too(self):
        """WHERE THE FILED CURE SHAPE WAS WRONG, and the arm that records it.

        task/2861 counted a cancelled successor as discharging. It is not:
        `moved_nothing` already calls cancellation a pass-through, because the
        work may have MOVED to a sibling rather than finished. So the walk
        continues through it exactly as `carrier`'s does, and what ends the
        obligation is the landing BEYOND the cancellation, never the
        cancellation itself. Over the live board the difference is 31 rows of
        121 -- rows the filed rule would have silenced while someone still
        owed them.
        """
        # The cancellation alone ends nothing...
        alone = self._snap(self._row("p"),
                           self._row("c1", status="cancelled", supersedes="p"))
        self.assertIsNone(dispatches.ended(alone["p"], alone),
                          "a cancellation was read as an ending, so a row "
                          "whose work merely MOVED would go quiet")
        # ...and the landing BEYOND it does, which is the same walk `carrier`
        # makes and the reason this is a pass-through rather than a stop.
        beyond = self._snap(
            self._row("p"),
            self._row("c1", status="cancelled", supersedes="p"),
            self._row("c2", status="verdict", supersedes="c1",
                      polarity="approve", close_reason="landed"))
        self.assertEqual((dispatches.ended(beyond["p"], beyond) or {}).get("id"),
                         "c2", "the walk stopped at the cancelled link")

    def test_a_DISCHARGED_build_successor_ENDED_it_and_a_bare_close_did_not(self):
        """THE POLARITY-LESS TERMINALS COUNT. A build row never carries a
        polarity, so its chain can only end in a `discharged` close (or a
        report in `delivered-report`); a tuple that spelled `landed` and
        `carried` alone left every such chain reading as debt forever. The
        control is a close that recorded NO reason: `closed_state` then spells
        the bare status, which says nothing about the work, and an unknown
        resolves toward visible."""
        discharged = self._snap(
            self._row("p"),
            self._row("c", status="closed", supersedes="p",
                      close_reason="discharged"))
        self.assertEqual((dispatches.ended(discharged["p"], discharged)
                          or {}).get("id"), "c")
        bare = self._snap(self._row("p"),
                          self._row("c", status="closed", supersedes="p"))
        self.assertEqual(dispatches.closed_state(bare["c"]), "closed",
                         "the control: a reason-less close spells its status")
        self.assertIsNone(dispatches.ended(bare["p"], bare))

    def test_a_FOREIGN_chain_cannot_end_this_rows_debt(self):
        """Same rule `carrier` holds: a successor of another chain took no
        obligation here, so it can neither carry it nor finish it."""
        kid = self._row("c", status="verdict", supersedes="p",
                        polarity="approve", close_reason="landed",
                        chain_root="some-other-chain")
        snap = self._snap(self._row("p"), kid)
        # THE CONTROL: the SAME successor on the SAME chain DOES end it, so
        # what the assertion below measures is the chain rule and not a row
        # that could never have discharged anything.
        same = self._snap(self._row("p"),
                          dict(kid, chain_root=self.CHAIN))
        self.assertEqual((dispatches.ended(same["p"], same) or {}).get("id"),
                         "c")
        self.assertIsNone(dispatches.ended(snap["p"], snap))

    def test_owed_ALREADY_excludes_an_ended_chain_and_must_not_be_widened(self):
        """THE FOUR CONSUMERS ARE ALREADY RIGHT, pinned so nobody "fixes" them.

        `owed()` feeds the seat counts proxywatch derives IDLE from, the stop
        ladder's does-this-seat-owe rung, `open_rows`, and idle_dispatch's
        work offer. Every one of them would be wrong if a finished chain
        counted as debt. It does not, and the reason is worth pinning rather
        than rediscovering: the discharging successor is itself a CARRIER, so
        `carrier` already removes the parent from the owed frontier.

        MEASURED on the live board before this arm was written: of the 121
        open/held rows whose chain had ended, `owed()` returned ZERO, and the
        split was 106 with a live carrier and 15 not open at all. So `ended`
        belongs to the LISTING, which judges rows `owed()` never selected, and
        widening `owed()` would change three surfaces that never asked.
        """
        snap = self._snap(
            self._row("p"),
            self._row("c", status="verdict", supersedes="p",
                      polarity="approve", close_reason="landed"))
        self.assertIsNotNone(dispatches.ended(snap["p"], snap),
                             "the fixture's chain did not end, so the "
                             "absence below would prove nothing")
        self.assertEqual([r["id"] for r in dispatches.owed(snap)], [],
                         "owed() offered a row whose chain had ended")
        # THE CONTROL: the same shape with a LIVE successor is also absent
        # from owed() -- carried, not ended -- so the emptiness above is not
        # owed() simply refusing everything.
        live = self._snap(self._row("p"),
                          self._row("c", status="open", supersedes="p"))
        self.assertEqual([r["id"] for r in dispatches.owed(live)], ["c"],
                         "owed() should still offer the LIVE successor")



class TheOrderedRetiredByQuestionTest(unittest.TestCase):
    """ONE TABLE, TWO READINGS (task/2858).

    `closed_state` wants the STATE a retirement leaves a row in;
    `_close_retired_by` wants the VERB that did it. They have to agree about
    the ORDER, and they did not: `closed_state` walked
    `_NON_CARRYING_FLAGS`, which answers a different question -- which
    SUCCESSORS carry nothing -- and so missed the two retiring facts that
    arrive with no `close_reason` at all.
    """

    def test_a_DISCHARGED_row_says_discharged_and_not_its_bare_status(self):
        """THE FILED REPRO. A discharge writes no close_reason, so the old
        walk fell through to the raw status and said "verdict" -- sending its
        reader after a review that was never written, which is the failure
        `closed_state`'s own docstring warns about. Measured on the live
        board: 47 rows."""
        row = {"id": "d", "status": "verdict", "discharged": True}
        self.assertEqual(dispatches.closed_state(row), "discharged")
        self.assertEqual(dispatches._close_retired_by(row), "discharge")

    def test_a_LANDING_close_says_landed_and_not_its_bare_status(self):
        """The other absent fact, 4 rows on the live board."""
        row = {"id": "l", "status": "verdict", "closed_by_landing": True}
        self.assertEqual(dispatches.closed_state(row), "landed")
        self.assertEqual(dispatches._close_retired_by(row), "close-landed")

    def test_the_two_readings_walk_the_SAME_table_in_the_SAME_order(self):  # noqa: VACUOUS_ASSERTION — every assertion here is an unconditional assertEqual on a POSITIVE observable (the state and the verb for each table entry, then both readings of a row carrying every flag); the closing assertEqual on `seen` is the must-hit that a shrunken table cannot satisfy
        """THE PROPERTY, not the instances. A row carrying SEVERAL retiring
        facts must resolve the same way on both surfaces, or a refusal names
        one cause while a state names another. Derived from the table rather
        than hand-listed, so a future entry is covered on the day it is
        added."""
        seen = []
        for field, state, verb in dispatches._RETIRED_BY:
            row = {"id": "x", "status": "verdict", field: True}
            self.assertEqual(dispatches.closed_state(row), state, field)
            self.assertEqual(dispatches._close_retired_by(row), verb, field)
            seen.append(field)
        # THE MUST-HIT: every assertion above is inside the loop, so an empty
        # or shrunken table would satisfy this arm having tested nothing.
        self.assertEqual(seen, ["discharged", "withdrawn",
                                "closed_by_landing", "abandoned",
                                "verdict_retracted"],
                         "the ordered table is not the one this arm pins")
        # AND THE ORDER ITSELF: a row with EVERY flag set resolves to the
        # first entry on both, which is what "the same ordered question"
        # means and what a second hand-written list could not promise.
        every = {"id": "y", "status": "verdict"}
        every.update({f: True for f, _s, _v in dispatches._RETIRED_BY})
        first_field, first_state, first_verb = dispatches._RETIRED_BY[0]
        self.assertEqual(dispatches.closed_state(every), first_state)
        self.assertEqual(dispatches._close_retired_by(every), first_verb)

    def test_a_row_retired_by_NOTHING_still_reads_its_status(self):
        """The control: the fall-through is not removed, only reached less
        often. A row nothing retired has no state to name and says what it
        is."""
        row = {"id": "z", "status": "open"}
        self.assertEqual(dispatches.closed_state(row), "open")
        self.assertIsNone(dispatches._close_retired_by(row))


class AClosedRowOwesNoDeliveryLegTest(test_dispatches.DispatchBase):
    """The chase leg asks BOTH ways a row can owe nothing.

    Found by reading this listing's own output half an hour after the chain
    cure landed: rows printing "VERDICT approve / CLOSED (LANDED)" beside
    "YOURS TO CHASE (you sent it)". `ended` asks about a SUCCESSOR, and a
    closed row has none to find -- IT IS THE DISCHARGE -- so it answered None
    and the marker survived on 1,260 rows that owed nothing. task/2861's own
    note had named this sibling.
    """

    def setUp(self):
        super().setUp()
        self.as_seat("seat-b")
        self.live = self.add(recipient="seat-a", lane="still-owed")["id"]
        self.as_seat("seat-b")

    as_seat = test_dispatches.AnAuthorsOwnOpenRowsAreObligationsTooTest.as_seat
    list_ = test_dispatches.AnAuthorsOwnOpenRowsAreObligationsTooTest.list_

    def test_a_row_that_is_NO_LONGER_AWAITING_is_not_YOURS_TO_CHASE(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive runs FIRST on the same observable and the same rendering call: the LIVE control row is asserted to CARRY the marker before the finished row is asserted not to, and the precondition asserts the row stopped being _open
        """A row that has stopped awaiting anything has no DELIVERY LEG left
        to own, which is what the marker's own legend says it means.

        The question is asked POSITIVELY on purpose. A first cut asked `not
        retired` and still marked 222 of 650 rows one seat had sent -- 131
        verdicted, 80 cancelled -- because each way a row stops being live
        needs its own clause and the next one is always missing."""
        self.as_seat("seat-b")
        row = self.add(recipient="seat-a", lane="already-closed")
        done = row["id"]
        with mock.patch.object(dispatches, "GATE_CAPS", ()):
            _out, why = dispatches.mark_verdict(done, row["tip"],
                                                "reviewed", "approve")
        self.assertIsNone(why, why)
        # THE PRECONDITION: the row really has stopped awaiting anything, or
        # the absence below is about something else entirely. A verdict ends
        # the waiting; `_open` is the producer's own word for it.
        snap, _ = dispatches.snapshot()
        self.assertFalse(dispatches._open(snap[done]),
                         "the fixture's row is still awaiting a verdict, so "
                         "it legitimately owes a delivery leg")
        rc, out, err = self.list_("--mine", "--issued")
        self.assertEqual((rc, err), (0, ""))
        live_line = next(l for l in out.splitlines() if self.live in l)
        self.assertIn("YOURS TO CHASE", live_line,
                      "the control row lost the marker, so its absence "
                      "below says nothing about being closed")
        done_line = next((l for l in out.splitlines() if done in l), None)
        self.assertIsNotNone(done_line, "the closed row vanished")
        self.assertNotIn("YOURS TO CHASE", done_line)


class TheListingAsksBeforeItAccusesTest(test_dispatches.DispatchBase):
    """The two legs, on the fixture the YOURS TO CHASE arm already uses."""

    def setUp(self):
        super().setUp()
        self.as_seat("seat-b")
        self.sent = self.add(recipient="seat-a")["id"]
        self.as_seat("seat-b")

    as_seat = test_dispatches.AnAuthorsOwnOpenRowsAreObligationsTooTest.as_seat
    list_ = test_dispatches.AnAuthorsOwnOpenRowsAreObligationsTooTest.list_

    def test_a_row_whose_CHAIN_ENDED_is_not_CHASED_and_says_so(self):
        """task/2861, THE MEASURED COST. A listing that manufactures false
        debt makes its readers write false closes.

        Row a977d87916c5 showed `YOURS TO CHASE` at 542 minutes against a
        45-minute deadline. Its successor was LANDED and the reviewer's patch
        was already an ancestor of trunk. The reader went to close a row that
        was never hanging -- and the only thing that stopped a false close was
        checking by hand what the listing should have said.

        THE ROW STAYS VISIBLE. Hiding it would be the opposite failure: a row
        shown in error has a reader who reconciles it, one hidden has nobody.
        What changes is the SENTENCE -- it has not closed, and nothing is
        owed -- because "yours to chase" is a claim about a delivery leg that
        no longer exists.
        """
        self.as_seat("seat-b")
        finished = self.add(recipient="seat-a", lane="chain-that-ended")["id"]
        self.as_seat("integrator")
        # THE SUCCESSOR THAT ENDS IT, on the same chain, through the REAL
        # doors: `add` mints it and `mark_verdict` gives it the APPROVE that
        # discharges. Stamping polarity onto the row by hand would have been a
        # fixture inventing a shape the writer never produces.
        kid = self.add(recipient="seat-a", lane="chain-that-ended",
                       supersedes=finished)
        # GATE_CAPS EMPTIED, the same way the land-request fixtures do it and
        # for the same reason: a GATE-CAPABLE writer's approve must carry a
        # minted receipt, and this arm's subject is the LISTING, not the gate.
        # It disables the writer's capability, never the record-time authority.
        with mock.patch.object(dispatches, "GATE_CAPS", ()):
            out, why = dispatches.mark_verdict(kid["id"], kid["tip"],
                                               "reviewed", "approve")
        self.assertIsNone(why, why)
        self.assertEqual((out or {}).get("polarity"), "approve",
                         "the successor never reached APPROVE, so the chain "
                         "under test did not actually end")
        self.as_seat("seat-b")
        # `--issued` WITHOUT `--open`, and the distinction is the finding.
        # `--open` selects through `owed()`, which already drops this row --
        # the discharging successor is itself a CARRIER, so the parent left
        # the owed frontier. The false marker therefore never appeared there;
        # it appears on the listing that shows a row by DIRECTION alone, which
        # is the one the measured incident was read from. Both surfaces are
        # asserted below so neither can drift.
        rc, out, err = self.list_("--mine", "--issued")
        self.assertEqual((rc, err), (0, ""))
        # THE CONTROL FIRST, in this same listing and this same rendering
        # call: a row whose chain is LIVE still carries the marker, so the
        # absence below is about the chain and not about the marker breaking.
        sent_line = next(l for l in out.splitlines() if self.sent in l)
        self.assertIn("YOURS TO CHASE", sent_line)
        ended_line = next((l for l in out.splitlines() if finished in l), None)
        self.assertIsNotNone(ended_line,
                             "the finished row vanished from the listing -- "
                             "hiding it is the opposite defect")
        self.assertNotIn("YOURS TO CHASE", ended_line)
        self.assertIn("CHAIN ENDED", ended_line)
        self.assertIn("you owe nothing", ended_line)
        # AND THE OTHER SURFACE IS UNCHANGED: `--open` never offered this row,
        # because `owed()` had already dropped it. Pinned here so a later
        # widening of `owed()` -- the cure this deliberately did NOT make --
        # shows up as a failure rather than as a silent change of meaning.
        o_rc, o_out, o_err = self.list_("--open", "--mine", "--issued")
        self.assertEqual((o_rc, o_err), (0, ""))
        self.assertNotIn(finished, o_out)
        self.assertIn(self.sent, o_out,
                      "the --open listing showed nothing at all, so its "
                      "silence about the finished row proves nothing")



class TheChaseMarkerNamesTheRowThatHoldsItTest(test_dispatches.DispatchBase):
    """One obligation, one marker -- not one per link of the chain.

    The two clauses already here ask whether a row is still AWAITING and
    whether its chain has ENDED. Neither asks whether the one who owes is
    THIS ROW. A live chain therefore keeps every open link marked, so a seat
    reading its own listing sees one obligation several times and an owed
    count that grows with the length of its chains.

    Measured on the live board before the cure: 18 rows marked YOURS TO
    CHASE, only 2 of them chain tips, and 4 chains carrying the marker on BOTH
    a row and its successor -- one row managing to be both a marked row and a
    marked row's successor. After: 3, with CHAIN ENDED unmoved at 246.
    """

    def setUp(self):
        super().setUp()
        self.as_seat("seat-b")
        self.live = self.add(recipient="seat-a", lane="still-owed")["id"]
        self.as_seat("seat-b")

    as_seat = test_dispatches.AnAuthorsOwnOpenRowsAreObligationsTooTest.as_seat
    list_ = test_dispatches.AnAuthorsOwnOpenRowsAreObligationsTooTest.list_

    def _rendered(self):
        """The listing's lines, after proving the listing renders at all."""
        rc, out, err = self.list_("--mine", "--issued")
        self.assertEqual((rc, err), (0, ""))
        lines = out.splitlines()
        # THE UNCONDITIONAL POSITIVE, on the same rendering call every arm
        # below reads: the control row is a live tip and MUST be marked, so
        # any absence found afterwards is about supersession and not about
        # the listing coming back empty, unrendered or filtered to nothing.
        live_line = next((l for l in lines if self.live in l), None)
        self.assertIsNotNone(live_line, "the control row vanished from the "
                                        "listing, so nothing below is about "
                                        "the marker")
        self.assertIn("YOURS TO CHASE", live_line,
                      "the live control row lost the marker, so an absence "
                      "below says nothing about who holds the obligation")
        return lines

    def _line(self, lines, rid):
        line = next((l for l in lines if rid in l), None)
        self.assertIsNotNone(line, "row %s vanished from the listing; the "
                                   "marker is meant to come off the row, "
                                   "never to hide the row" % rid[:12])
        return line

    def test_a_row_whose_LIVE_successor_took_the_work_is_not_chased(self):
        """The obligation moved, so the marker moves with it.

        Both rows stay VISIBLE -- the successor is the one told to chase.
        Hiding the parent would be a different and worse cure: this whole
        class of defect is rows that stopped being visible while still being
        owed.
        """
        parent = self.add(recipient="seat-a", lane="handed-on")["id"]
        self.as_seat("seat-b")
        child = self.add(recipient="seat-a", lane="handed-on",
                         supersedes=parent)["id"]
        lines = self._rendered()
        self.assertNotIn("YOURS TO CHASE", self._line(lines, parent),
                         "the parent is still chased although a live "
                         "successor holds the work -- one obligation "
                         "wearing two markers")
        self.assertIn("YOURS TO CHASE", self._line(lines, child),
                      "the successor that DOES hold the obligation lost the "
                      "marker, so the cure silenced the chain instead of "
                      "moving the marker along it")

    def test_a_CANCELLED_successor_leaves_the_parent_CHASED(self):
        """THE MUST-HIT, and the arm that refuses the obvious cure.

        "Superseded means not mine" is wrong, and this is where it breaks. A
        cancelled, withdrawn or abandoned successor MOVED NOTHING: the work
        may have gone to a sibling or nowhere at all, so the parent really
        does still owe its delivery leg. `carrier` already treats such a
        successor as a PASS-THROUGH, which is precisely why the new clause
        asks carrier rather than testing whether a successor row exists.

        Without this arm the cheap cure passes the arm above and silently
        stops chasing live obligations -- the same failure mode being cured,
        with the marker now missing instead of doubled.
        """
        parent = self.add(recipient="seat-a", lane="cancelled-child")["id"]
        self.as_seat("seat-b")
        child = self.add(recipient="seat-a", lane="cancelled-child",
                         supersedes=parent)["id"]
        argv = ["cancel", child, "reviewer", "stood", "down"]
        rc, _out, err = run(dispatches.cmd_dispatch, argv)
        self.assertEqual(rc, 0, err)
        snap, _ = dispatches.snapshot()
        # THE PRECONDITION: the successor really did reach that status, or
        # the marker surviving below is about something else entirely.
        self.assertEqual("cancelled", str(snap[child].get("status") or ""),
                         "the fixture's successor never reached that status, "
                         "so this arm is not testing the pass-through at all")
        lines = self._rendered()
        self.assertIn("YOURS TO CHASE", self._line(lines, parent),
                      "a successor that moved nothing was read as having "
                      "taken the work, so a row that still owes its delivery "
                      "leg went quiet -- the pass-through rule carrier "
                      "already applies")


class AConcurSuccessorEndorsesAndAccountsForNothingTest(unittest.TestCase):
    """The third state the chain walks had no name, so it was read as HOLDS.

    THE DEFECT, reproduced on two holders before the cure. `helm dispatch
    list --open --to <seat> --all-projects --json` returned `[]` for a seat
    whose unfiltered listing held three OPEN rows. So `--open` was not a
    filter that narrowed to open rows; it dropped some of them, and an empty
    answer from the flag a reader uses to ask WHAT IS STILL OUTSTANDING reads
    as "nothing is owed here".

    THE MECHANISM IS NOT A STATUS SET. Every status word the ledger spells is
    admitted by the status predicates, in both directions, and the class sweep
    at the bottom of this module keeps saying so. The drop happens one layer
    down, in `carrier`: a single yes/no per successor -- is this a
    pass-through? -- reads its NO as "it holds the debt", so a successor that
    took the obligation and AUTHORISES NOTHING FURTHER answers "I hold it"
    while being owed by nobody.

    CONCUR IS THAT SUCCESSOR, AND IT IS THE ONLY ONE. `concur` sits outside
    WORK_POLARITIES by construction so that endorsement has a word promising
    no landing, and no close door admits it. A FIX is the opposite case and
    the arms above already pin it: a finding is the NEXT ROUND of the same
    obligation, so a FIX successor really does hold it, and billing the
    parent again would turn one broken chain into two people fixing one lane.

    MEASURED over the live ledger at the time of the cure: 199 open rows, of
    which the owed frontier admitted 8. Eleven were open rows hidden behind a
    CONCUR verdict. The other 180 are accounted for honestly -- a successor
    holds them, or a discharging one ended them."""

    CHAIN = test_dispatches.CarryingSemanticsTest.CHAIN
    # the SAME helper, never a copy
    _row = test_dispatches.CarryingSemanticsTest._row

    def _snap(self, *rows):
        return {r["id"]: r for r in rows}

    def test_the_disposition_vocabulary_is_four_names_and_two_account(self):
        """UNCONDITIONAL CONTROL, before anything reads the classifier.

        Every arm below distinguishes "hidden" from "shown" through
        ACCOUNTED_DISPOSITIONS. Emptying it would make `carrier` return None
        for everything and turn each of those arms green while meaning the
        opposite, so the tuple is pinned by value here rather than by use."""
        self.assertEqual(
            {dispatches.PASS_THROUGH, dispatches.HOLDS,
             dispatches.ENDS, dispatches.DEAD_END},
            {"pass-through", "holds", "ends", "dead-end"})
        self.assertEqual(4, len({dispatches.PASS_THROUGH, dispatches.HOLDS,
                                 dispatches.ENDS, dispatches.DEAD_END}),
                         "two dispositions collapsed to one name")
        self.assertEqual(tuple(dispatches.ACCOUNTED_DISPOSITIONS),
                         (dispatches.HOLDS, dispatches.ENDS))
        self.assertEqual(tuple(dispatches.NON_CARRYING_POLARITY), ("concur",),
                         "the non-carrying polarity set grew or shrank; every "
                         "arm below is about which polarities carry")

    def test_every_live_terminal_lands_in_exactly_one_named_arm(self):
        """THE CLASSIFIER IS TOTAL, asserted over the shapes the ledger really
        writes rather than over the ones the tuples happen to name.

        THE FOUR ARMS ARE PINNED BEFORE THE TABLE IS WALKED, unconditionally
        and off the loop. Every assertion in the table runs inside `for`, so a
        table that shrank to nothing would walk zero rows and take the whole
        classifier with it silently. These four cannot be skipped, and between
        them they name every disposition, so a classifier collapsed onto ANY
        single answer dies here rather than in the visibility arms."""
        one = lambda **kw: dispatches.successor_disposition(
            self._row("c", supersedes="p", **kw))
        self.assertEqual(dispatches.HOLDS, one(status="open"))
        self.assertEqual(dispatches.PASS_THROUGH, one(status="cancelled"))
        self.assertEqual(dispatches.ENDS,
                         one(status="verdict", polarity="approve"))
        self.assertEqual(dispatches.DEAD_END,
                         one(status="verdict", polarity="concur"))
        cases = [
            # (kw, expected disposition)
            ({"status": "open"}, dispatches.HOLDS),
            ({"status": "held"}, dispatches.HOLDS),
            ({"status": "cancelled"}, dispatches.PASS_THROUGH),
            ({"status": "verdict", "withdrawn": True}, dispatches.PASS_THROUGH),
            ({"status": "verdict", "close_reason": "stranded"},
             dispatches.PASS_THROUGH),
            ({"status": "open", "retired_admin": True},
             dispatches.PASS_THROUGH),
            ({"status": "verdict", "polarity": "approve"}, dispatches.ENDS),
            ({"status": "verdict", "close_reason": "landed"},
             dispatches.ENDS),
            ({"status": "closed", "close_reason": "discharged"},
             dispatches.ENDS),
            ({"status": "verdict", "polarity": "concur"}, dispatches.DEAD_END),
            # THE POLARITIES THAT TOOK THE WORK. A FIX is the next round and a
            # supersede moves it to another artifact; neither discharges, and
            # both are still a live answer to "who holds this now".
            ({"status": "verdict", "polarity": "fix"}, dispatches.HOLDS),
            ({"status": "verdict", "polarity": "supersede"}, dispatches.HOLDS),
            ({"status": "verdict"}, dispatches.HOLDS),
            # A CONCUR STAYS A DEAD END WHATEVER CLOSE REASON RIDES WITH IT,
            # including the reasons neither membership tuple names.
            ({"status": "verdict", "close_reason": "superseded",
              "polarity": "concur"}, dispatches.DEAD_END),
            ({"status": "verdict", "close_reason": "expired",
              "polarity": "concur"}, dispatches.DEAD_END),
            # ...and the reason nobody has invented yet, which TOOK the work.
            ({"status": "verdict", "close_reason": "a-reason-from-next-year"},
             dispatches.HOLDS),
        ]
        seen = set()
        for kw, want in cases:
            row = self._row("c", supersedes="p", **kw)
            got = dispatches.successor_disposition(row)
            self.assertEqual(want, got, "%r classified %s" % (kw, got))
            seen.add(want)
        self.assertEqual(seen, {dispatches.HOLDS, dispatches.PASS_THROUGH,
                                dispatches.ENDS, dispatches.DEAD_END},
                         "a shrunken table stopped exercising an arm")

    def test_a_row_this_module_cannot_read_is_a_DEAD_END_not_a_holder(self):
        """Every unknown resolves toward visible, and an unreadable successor
        is the plainest unknown there is.

        BOTH DIRECTIONS, UNCONDITIONALLY. The loop below is the sweep, but a
        classifier that answered DEAD_END to everything would satisfy it
        completely while destroying the only thing DEAD_END means here. So a
        readable row that is NOT a dead end is asserted first, off the loop,
        and one unreadable value is asserted beside it."""
        self.assertEqual(dispatches.HOLDS,
                         dispatches.successor_disposition(
                             self._row("c", supersedes="p", status="open")))
        self.assertEqual(dispatches.DEAD_END,
                         dispatches.successor_disposition(None))
        for junk in ("", 7, ["c"], object(), ("c",), {"no": "id"}):
            self.assertEqual(dispatches.DEAD_END,
                             dispatches.successor_disposition(junk),
                             "%r was not resolved toward visible" % (junk,))

    def test_no_row_the_ledger_can_write_escapes_the_four_names(self):
        """TOTALITY OVER THE REAL CROSS-PRODUCT, which is what makes the
        default arm a guarantee instead of a hope. Every combination of the
        three spellings the ledger writes is classified, and the answer is
        always one of the four names -- never None, never a raise, never a
        fifth word a consumer's else-arm would have to invent a meaning for.

        THE CONTROL IS THAT THE SWEEP DISCRIMINATES. A classifier returning
        one constant would satisfy "always one of four" perfectly, so the
        sweep also requires at least three distinct answers across the
        product, and two arms are pinned by hand before it runs."""
        self.assertEqual(dispatches.HOLDS,
                         dispatches.successor_disposition(
                             self._row("c", supersedes="p", status="open")))
        self.assertEqual(dispatches.DEAD_END,
                         dispatches.successor_disposition(
                             self._row("c", supersedes="p", status="verdict",
                                       polarity="concur")))
        # THE THREE AXES ARE SHOWN TO HOLD CONTENT before they are multiplied
        # out: an empty axis makes the product empty, the loop body never
        # runs, and the count below agrees with itself at zero.
        self.assertEqual(5, len(LEDGER_STATUSES))
        self.assertIn("landed", LEDGER_CLOSE_REASONS)
        self.assertIn("concur", LEDGER_POLARITIES)
        names = {dispatches.PASS_THROUGH, dispatches.HOLDS,
                 dispatches.ENDS, dispatches.DEAD_END}
        answers, checked = set(), 0
        for status in LEDGER_STATUSES:
            for reason in LEDGER_CLOSE_REASONS:
                for polarity in LEDGER_POLARITIES:
                    kw = {"status": status}
                    if reason is not None:
                        kw["close_reason"] = reason
                    if polarity is not None:
                        kw["polarity"] = polarity
                    got = dispatches.successor_disposition(
                        self._row("c", supersedes="p", **kw))
                    self.assertIn(got, names, "%r escaped the vocabulary" % kw)
                    answers.add(got)
                    checked += 1
        self.assertEqual(checked, len(LEDGER_STATUSES)
                         * len(LEDGER_CLOSE_REASONS) * len(LEDGER_POLARITIES))
        self.assertGreaterEqual(len(answers), 3,
                                "the sweep got one answer for everything, so "
                                "it proves nothing about the arms")

    def test_a_parent_is_hidden_ONLY_behind_an_accounted_disposition(self):
        """THE INVARIANT THAT MAKES UNDER-REPORTING STRUCTURALLY IMPOSSIBLE.
        Whatever `carrier` hands back becomes the reason a parent leaves the
        owed frontier, so the property that must hold over every shape the
        ledger can write is that it NEVER hands back a successor that holds
        no debt and ended none.

        Swept over the same real cross-product, with the discriminating
        control alongside: the walk must still hide a parent behind a genuine
        holder, or 'never hides anything' would satisfy this vacuously."""
        live = self._snap(self._row("p"),
                          self._row("c", status="open", supersedes="p"))
        self.assertEqual("c",
                         (dispatches.carrier(live["p"], live) or {}).get("id"),
                         "nothing is ever hidden, so the sweep below is "
                         "true of a walk that simply gave up")
        self.assertIn(dispatches.successor_disposition(live["c"]),
                      dispatches.ACCOUNTED_DISPOSITIONS,
                      "the control's holder is not an accounted one, so the "
                      "sweep's assertion is about a different property")
        hidden = 0
        for status in LEDGER_STATUSES:
            for reason in LEDGER_CLOSE_REASONS:
                for polarity in LEDGER_POLARITIES:
                    kw = {"status": status}
                    if reason is not None:
                        kw["close_reason"] = reason
                    if polarity is not None:
                        kw["polarity"] = polarity
                    snap = self._snap(self._row("p"),
                                      self._row("c", supersedes="p", **kw))
                    kid = dispatches.carrier(snap["p"], snap)
                    if kid is None:
                        continue      # shown: always the safe answer
                    hidden += 1
                    self.assertIn(
                        dispatches.successor_disposition(kid),
                        dispatches.ACCOUNTED_DISPOSITIONS,
                        "%r hid its parent while holding no debt and ending "
                        "none" % kw)
        self.assertGreater(hidden, 0,
                           "no shape hid its parent, so the assertion inside "
                           "the sweep never ran")

    def test_a_bare_CONCUR_verdict_leaves_the_parent_OWED(self):
        """THE REPRO IN MINIATURE, with the exclusion control in the same
        call. CONCUR authorizes nothing BY CONSTRUCTION -- it is absent from
        every close allowlist -- so a chain that reached one reached nothing,
        and the debt is back on the parent. Every one of the eleven rows the
        live ledger was hiding sat behind exactly this.

        THE OTHER DIRECTION, on the same observable: the filter must still
        EXCLUDE a parent whose chain genuinely ended, or this arm would pass
        against a `carrier` that had simply stopped hiding anything."""
        dead = self._snap(
            self._row("p"),
            self._row("c", status="verdict", supersedes="p",
                      polarity="concur"))
        self.assertIsNone(dispatches.carrier(dead["p"], dead),
                          "an endorsement that authorised nothing was read "
                          "as holding the debt, so the parent went invisible")
        self.assertEqual(["p"], [r["id"] for r in dispatches.owed(dead)],
                         "the parent is open and owed by nobody else")
        done = self._snap(
            self._row("p"),
            self._row("c", status="verdict", supersedes="p",
                      polarity="approve", close_reason="landed"))
        self.assertEqual((dispatches.carrier(done["p"], done) or {}).get("id"),
                         "c")
        self.assertEqual([], [r["id"] for r in dispatches.owed(done)],
                         "owed() now offers everything, so the arm above "
                         "proves nothing about dead ends")

    def test_a_LIVE_concur_row_still_holds_the_debt(self):
        """THE POLARITY IS NOT THE WHOLE ANSWER, and this is the pair that
        says so. Only a TERMINAL concur is a dead end: a concur written onto
        a row that is still OPEN is a live obligation like any other, and
        reading the polarity before the status would strand it."""
        live = self._snap(
            self._row("p"),
            self._row("c", status="open", supersedes="p", polarity="concur"))
        self.assertEqual((dispatches.carrier(live["p"], live) or {}).get("id"),
                         "c", "a LIVE successor must still hold the debt")
        self.assertEqual(["c"], [r["id"] for r in dispatches.owed(live)])

    def test_a_FIX_successor_is_NOT_a_dead_end_and_still_carries(self):
        """THE BOUNDARY OF THE CURE, pinned so a later widening cannot pass
        quietly. A FIX is the next round of the same obligation: somebody
        holds it, so the parent must NOT come back as pickup work. The
        neighbouring arm in this module already requires that a FIX ends
        nothing; this one requires that it CARRIES, and the two together are
        what stop one broken chain being billed as two debts."""
        fix = self._snap(
            self._row("p"),
            self._row("c", status="verdict", supersedes="p", polarity="fix"))
        self.assertEqual(dispatches.HOLDS,
                         dispatches.successor_disposition(fix["c"]))
        self.assertEqual((dispatches.carrier(fix["p"], fix) or {}).get("id"),
                         "c", "a FIX stopped carrying, so its parent is now "
                              "billed a second time for one broken chain")
        # ...AND THE FRONTIER IS EMPTY, which is the honest reading: the FIX
        # row is terminal, so it is not OPEN and `owed` cannot offer it, and
        # the parent is accounted for by it. What this arm pins is that the
        # parent does NOT come back — a second debt for one broken chain.
        self.assertEqual([], [r["id"] for r in dispatches.owed(fix)],
                         "the parent came back as a second debt for one "
                         "broken chain")
        # THE CONTRAST, same shape, one field different.
        con = self._snap(
            self._row("p"),
            self._row("c", status="verdict", supersedes="p",
                      polarity="concur"))
        self.assertEqual(dispatches.DEAD_END,
                         dispatches.successor_disposition(con["c"]))
        self.assertEqual(["p"], [r["id"] for r in dispatches.owed(con)])

    def test_a_DEAD_END_is_a_PASS_THROUGH_so_the_walk_reaches_what_follows(self):
        """WORK MOVES ON PAST A DEAD END, exactly as it does past a
        cancellation. Stopping at the first one would hand the parent back as
        fresh pickup work while a successor was mid-flight, which is the
        opposite defect and just as expensive.

        Both continuations are asserted in one call so neither can be the
        accidental answer: past the dead end lies a LIVE row in one snapshot
        and a LANDED APPROVE in the other, and the parent is accounted for by
        each."""
        chain = self._snap(
            self._row("p"),
            self._row("c1", status="verdict", supersedes="p",
                      polarity="concur"),
            self._row("c2", status="open", supersedes="c1"))
        self.assertEqual((dispatches.carrier(chain["p"], chain) or {}).get("id"),
                         "c2", "the walk stopped at the dead-end link")
        self.assertEqual(["c2"], [r["id"] for r in dispatches.owed(chain)],
                         "the parent came back as pickup work while its "
                         "successor was live")
        landed = self._snap(
            self._row("p"),
            self._row("c1", status="verdict", supersedes="p",
                      polarity="concur"),
            self._row("c2", status="verdict", supersedes="c1",
                      polarity="approve", close_reason="landed"))
        self.assertEqual((dispatches.carrier(landed["p"], landed)
                          or {}).get("id"), "c2")
        self.assertEqual([], [r["id"] for r in dispatches.owed(landed)])

    def test_ended_walks_through_a_dead_end_to_the_discharge_beyond_it(self):
        """THE TWO WALKS READ ONE CLASSIFICATION, so they cannot drift.
        Stopping at a dead end would assert the chain ended at a row that
        ended nothing, so whether the chain ended is a question about what
        lies beyond it.

        The control is the same chain with nothing beyond the dead end, which
        still ends nothing."""
        beyond = self._snap(
            self._row("p"),
            self._row("c1", status="verdict", supersedes="p",
                      polarity="concur"),
            self._row("c2", status="verdict", supersedes="c1",
                      polarity="approve", close_reason="landed"))
        self.assertEqual((dispatches.ended(beyond["p"], beyond) or {}).get("id"),
                         "c2")
        stops = self._snap(
            self._row("p"),
            self._row("c1", status="verdict", supersedes="p",
                      polarity="concur"))
        self.assertEqual(dispatches.successor_disposition(stops["c1"]),
                         dispatches.DEAD_END,
                         "the control's link is not a dead end, so the None "
                         "below is about something else")
        self.assertIsNone(dispatches.ended(stops["p"], stops))


class TheOpenListingShowsARowNoSuccessorAccountsForTest(
        test_dispatches.DispatchBase):
    """The SURFACE arm: `--open` at the CLI, both directions, one call.

    The predicate arms above are pure. This one drives the verb the measured
    incident was read from, because the defect was only ever visible there:
    a reader asked what was outstanding and was told nothing was."""

    # NO setUp OVERRIDE. `DispatchBase.setUp` already names this seat
    # `integrator` and its tearDown restores the variable; setting it a second
    # time here changed nothing and made this module a writer of an env var it
    # has no restore for, which the per-module hygiene scan reads -- correctly
    # -- as a leak, because it cannot see the sibling module's tearDown.

    def list_(self, *args):
        return run(dispatches.cmd_dispatch, ["list", *args])

    def concur(self, row_id, note):
        """One CONCUR verdict through the real verb.

        NO EXIT ANSWER RIDES WITH IT, and the verb refuses one: the exit
        question answers FIX/SUPERSEDE, because only those can block. CONCUR
        is non-blocking endorsement, which is precisely the property that
        makes it account for nothing.

        THE REVIEWED TIP IS READ OFF THE ROW, never named by hand. A verdict
        BINDS the tip its row was dispatched at, so a literal from the fixture
        repo is right only while `add` happens to dispatch at that same
        commit; when it does not, every call here dies on a staleness refusal
        that is about the fixture and says nothing about the chain under
        test.

        AND THE AUTHOR PROOF IS THE SAME ONE EVERY CLI VERDICT FIXTURE USES.
        The write door binds a verdict to one exact runtime session, so the
        verb refuses a bare call before it ever reaches the chain this module
        is about. `verdict_author` supplies the runtime evidence and leaves
        tier capture and validation entirely real -- the refusal being stepped
        around is about WHO wrote it, never about what it says."""
        snap, _ = dispatches.snapshot()
        with self.verdict_author():
            rc, _out, err = run(dispatches.cmd_dispatch,
                                ["verdict", row_id, snap[row_id]["tip"],
                                 "--concur", "--measured", note])
        self.assertEqual(rc, 0, err)

    def test_open_shows_the_row_behind_a_dead_end_and_still_hides_a_closed_one(self):
        """ONE LISTING, TWO ROWS, OPPOSITE ANSWERS -- which is what makes
        either answer mean anything. The first row's successor reached a
        CONCUR verdict and authorised nothing, so the parent is still owed
        and must appear. The second row reached a verdict itself, so it is
        closed and must not. A filter that returned everything would fail the
        second assertion; the filter that shipped failed the first."""
        parent = self.add(recipient="seat-a", lane="dead-end-lane")["id"]
        child = self.add(recipient="seat-a", lane="dead-end-lane",
                         supersedes=parent)["id"]
        self.concur(child, "endorsed, and it authorises no landing")
        closed = self.add(recipient="seat-a", lane="finished-lane")["id"]
        self.concur(closed, "this row is the exclusion control")
        snap, _ = dispatches.snapshot()
        # THE PRECONDITIONS, so neither answer below can be about a fixture
        # that never reached the state it names.
        self.assertEqual("open", str(snap[parent].get("status") or ""))
        self.assertEqual(dispatches.DEAD_END,
                         dispatches.successor_disposition(snap[child]))
        self.assertEqual("verdict", str(snap[closed].get("status") or ""))

        rc, out, err = self.list_("--open", "--all-projects")
        self.assertEqual((rc, err), (0, ""))
        self.assertIn(parent, out,
                      "--open dropped an OPEN row that no successor holds "
                      "and no successor ended, so the flag that means SHOW "
                      "ME WHAT IS OUTSTANDING under-reported")
        self.assertNotIn(closed, out,
                         "--open listed a verdicted row, so its answer above "
                         "is not a filter's answer at all")

    def test_the_listing_computes_the_cycle_map_once_whatever_its_length(self):
        """ONE CYCLE MAP PER LISTING, not one per row. `ended` and `carrier`
        build the whole-graph cycle map from the index when none is passed,
        so the listing's per-row calls re-ran an O(V+E) pass once per row,
        and a listing over a real ledger spent minutes in
        `_cycle_components`. The count must not grow with
        the number of rows, which is what separates once-per-listing from
        once-per-row; an absolute bound would also pass a listing that
        happened to be short."""
        real = dispatches._cycle_components

        def calls_for(n, tag):
            for i in range(n):
                parent = self.add(recipient="seat-a",
                                  lane="cycle-%s-%d" % (tag, i))["id"]
                self.add(recipient="seat-a", lane="cycle-%s-%d" % (tag, i),
                         supersedes=parent)
            with mock.patch.object(dispatches, "_cycle_components",
                                   side_effect=real) as spy:
                rc, _out, err = self.list_("--all-projects")
            self.assertEqual((rc, err), (0, ""))
            return spy.call_count

        few = calls_for(1, "few")
        many = calls_for(6, "many")
        # THE CONTROL: the listing reached the map at all, so an equal count
        # below is about how often it is built and not about a path that
        # never asks.
        self.assertGreater(few, 0)
        self.assertEqual(many, few,
                         "seven chains cost %d cycle maps and one chain cost "
                         "%d: the listing rebuilds the map per row" % (many, few))

    def test_the_open_clause_names_the_chain_axis_it_narrows_on(self):
        """AN EMPTY ANSWER MUST SAY WHICH QUESTION WAS ASKED. A clause that
        names only the STATUS axis, while most exclusions are CHAIN ones, lets
        a reader take "no matching rows" for "no such row exists" instead of
        "each one is accounted for somewhere else"."""
        parent = self.add(recipient="seat-a", lane="carried-lane")["id"]
        child = self.add(recipient="seat-a", lane="carried-lane",
                         supersedes=parent)["id"]
        rc, out, err = self.list_("--open", "--all-projects")
        self.assertEqual((rc, err), (0, ""))
        # THE CONTROL: the live successor IS offered, so the clause below is
        # attached to a listing that selected something.
        self.assertIn(child, out)
        self.assertNotIn(parent, out)
        self.assertIn("a successor holds", out)
        self.assertIn("a discharging one ended", out)

    def test_overdue_reads_the_same_frontier_so_the_cure_reaches_it_too(self):
        """THE OTHER CONSUMER ON THIS VERB, swept because the incident asked
        for the whole class. `--overdue` is not a second state -- it is
        `--open` plus a clock, selecting through the SAME owed frontier -- so
        a row the frontier dropped was invisible to it for the same reason
        and with the same silence.

        Both directions in one call: the aged dead-end parent must appear,
        and a FRESH dead-end parent must not, or the arm would pass against
        an `--overdue` that had simply stopped applying its clock."""
        late = self.add(recipient="seat-a", lane="late-lane",
                        deadline_s=60)["id"]
        late_kid = self.add(recipient="seat-a", lane="late-lane",
                            supersedes=late)["id"]
        self.concur(late_kid, "endorsed; nothing further is authorised")
        fresh = self.add(recipient="seat-a", lane="fresh-lane",
                         deadline_s=86400)["id"]
        fresh_kid = self.add(recipient="seat-a", lane="fresh-lane",
                             supersedes=fresh)["id"]
        self.concur(fresh_kid, "endorsed here too, and just as recently")
        self.age(late, 7200)

        snap, _ = dispatches.snapshot()
        # THE PRECONDITION: both parents are owed, so whatever `--overdue`
        # does below is about the CLOCK and not about the frontier.
        owed = {r["id"] for r in dispatches.owed(snap)}
        self.assertIn(late, owed)
        self.assertIn(fresh, owed)

        rc, out, err = self.list_("--overdue", "--all-projects")
        self.assertEqual((rc, err), (0, ""))
        self.assertIn(late, out,
                      "--overdue dropped an aged row that no successor holds "
                      "and no successor ended, so the LATE question "
                      "under-reported exactly as the OPEN question did")
        self.assertNotIn(fresh, out,
                         "--overdue listed a row inside its deadline, so its "
                         "answer above is not a clock's answer at all")


class EveryStatusFilterOnThisVerbAdmitsTheWholeVocabularyTest(unittest.TestCase):
    """THE CLASS SWEEP the incident asked for, in both directions.

    The row this lane cures was found through `--open`, and the obvious next
    worry is that the same closed-set mistake sits under the verb's OTHER
    status filters where it would be invisible in exactly the same way. So
    each one is checked against the vocabulary the STORE really holds rather
    than against the words its own tuple happens to name.

    THE ANSWER SPLITS IN TWO, AND THE SPLIT IS THE FINDING. The filters that
    key on the STATUS WORD -- `--held` and `--source-clean` through
    `query_is_held`, `--open`'s own openness rung through
    `query_is_unheld_open` -- read a vocabulary that is genuinely CLOSED: the
    ledger spells five status words and nothing writes a sixth without a
    writer change. Each predicate answers correctly for every one. They were
    never the mechanism, and this class exists to keep saying so.

    THE CHAIN PREDICATES ARE THE OPEN ONES, and that axis is covered by the
    disposition arms above. This class pins the boundary between the two so a
    later reader does not re-litigate which layer to look at."""

    def test_the_status_predicates_partition_the_whole_live_vocabulary(self):
        """EVERY WORD, BOTH PREDICATES, BOTH ANSWERS. A row is strictly open,
        or strictly held, or neither -- never both, and never unclassified.
        Asserting only the trues would pass against a predicate that said yes
        to everything, so each word states BOTH answers."""
        from helm import query
        want = {
            # status      (unheld_open, held)
            "open":       (True,  False),
            "held":       (False, True),
            "verdict":    (False, False),
            "cancelled":  (False, False),
            "closed":     (False, False),
        }
        # THE PINNED VOCABULARY IS ASSERTED POSITIVELY FIRST, so the
        # cross-check below compares two things each shown to hold content.
        # Two empty collections agree with each other perfectly.
        self.assertEqual(5, len(LEDGER_STATUSES))
        self.assertIn("open", LEDGER_STATUSES)
        self.assertIn("held", LEDGER_STATUSES)
        self.assertEqual(sorted(want), sorted(LEDGER_STATUSES),
                         "the table and the pinned vocabulary disagree")
        row = {"id": "r", "status": "open"}
        self.assertTrue(query.query_is_unheld_open(row),
                        "the control: the canonical OPEN row is admitted")
        row = {"id": "r", "status": "held"}
        self.assertTrue(query.query_is_held(row),
                        "the control: the canonical HELD row is admitted")
        for status, (is_open, is_held) in want.items():
            row = {"id": "r", "status": status}
            self.assertEqual(is_open, query.query_is_unheld_open(row),
                             "query_is_unheld_open(%s)" % status)
            self.assertEqual(is_held, query.query_is_held(row),
                             "query_is_held(%s)" % status)
            self.assertNotEqual(
                (True, True),
                (query.query_is_unheld_open(row), query.query_is_held(row)),
                "%s read as BOTH open and held" % status)

    def test_retirement_overrides_every_status_word_in_both_predicates(self):
        """THE FIELD THAT IS NOT A STATUS, swept the same way. Retirement
        preserves `status`, so a predicate reading the word alone keeps a
        retired row live -- the same shape as the chain defect one layer up.
        Each word is asserted retired AND unretired, because only the pair
        shows the flag is what moved the answer."""
        from helm import query
        self.assertTrue(query.query_is_unheld_open({"status": "open"}))
        self.assertFalse(query.query_is_unheld_open({"status": "open",
                                                     "retired_admin": True}))
        for status in LEDGER_STATUSES:
            retired = {"id": "r", "status": status, "retired_admin": True}
            self.assertFalse(query.query_is_unheld_open(retired),
                             "retired %s still read as open" % status)
            self.assertFalse(query.query_is_held(retired),
                             "retired %s still read as held" % status)

    def test_a_status_word_this_module_never_saw_is_not_open_and_not_held(self):
        """THE ELSE-ARM, STATED. The status axis is closed today and the
        predicates are allowlists, so an unrecognised word reads as neither --
        and that is the SAFE direction here for a reason worth writing down:
        on the CHAIN axis an unknown must resolve toward VISIBLE, but on the
        STATUS axis an unknown row is not evidence that anybody owes anything,
        and `--open` would be inventing an obligation. The two defaults point
        opposite ways on purpose."""
        from helm import query
        self.assertTrue(query.query_is_unheld_open({"status": "open"}),
                        "the control: a known word still classifies")
        for junk in ("a-status-from-next-year", "", None, "OPEN-ish"):
            row = {"id": "r", "status": junk}
            self.assertFalse(query.query_is_unheld_open(row), repr(junk))
            self.assertFalse(query.query_is_held(row), repr(junk))
        # ...and the canonical spelling survives case and padding, so the
        # falses above are about the WORD and not about normalisation.
        self.assertTrue(query.query_is_unheld_open({"status": "  OPEN  "}))
        self.assertTrue(query.query_is_held({"status": "  HELD  "}))
