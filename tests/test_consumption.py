"""The Reading type's refusals, and the one they exist for.

EVERY REFUSAL ARM HERE CHECKS THE REASON, not just that something was raised.
A constructor with several validations refuses a malformed object for whatever
it happens to hit first, so an arm that only asserts `ValueError` passes while
the check it was written for is dead. That is the same-refusal-different-gate
trap, and it is cheap to close: assert on the sentence.
"""
import unittest

from helm.consumption import (PROVEN, REFUTED, UNKNOWN, Evidence, Reading,
                              proven, refuted, unknown)

SAMPLE = "did this pass read the room the alarm was raised on"
DRAINED = "is the row the alarm was raised on consumed"

ROOM = Evidence("scanned-room", "helm")
ROWS = Evidence("rows-seen", ())
CURSOR = Evidence("cursor", 4096)


class ReadingRefusesMalformed(unittest.TestCase):

    def test_UNKNOWN_must_name_what_it_lacked(self):
        with self.assertRaises(ValueError) as caught:
            Reading(SAMPLE, None, UNKNOWN)
        self.assertIn("without naming what it lacked", str(caught.exception))

    def test_an_UNKNOWN_that_names_it_is_accepted(self):
        """The control for the arm above: the refusal is about the MISSING
        sentence, not about UNKNOWN being unwelcome."""
        r = Reading(SAMPLE, None, UNKNOWN, (), "the room rotation never reached it")
        self.assertEqual(r.confidence, UNKNOWN)
        self.assertEqual(r.lacked, "the room rotation never reached it")

    def test_a_claim_with_nothing_looked_at_is_refused(self):
        # The unconditional control first: the same call WITH evidence is
        # accepted, so the refusals below are about the empty evidence and not
        # about the constructor rejecting everything.
        self.assertTrue(Reading(DRAINED, True, PROVEN, (ROOM,)).proven)
        for confidence in (PROVEN, REFUTED):
            with self.subTest(confidence=confidence):
                with self.assertRaises(ValueError) as caught:
                    Reading(DRAINED, True, confidence)
                self.assertIn("with nothing looked at", str(caught.exception))

    def test_a_grounded_claim_that_also_names_a_gap_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            Reading(DRAINED, True, PROVEN, (ROOM,), "the cursor was corrupt")
        self.assertIn("either grounded or it is not", str(caught.exception))

    def test_an_unlisted_confidence_is_refused_by_name(self):
        with self.assertRaises(ValueError) as caught:
            Reading(DRAINED, True, "probably", (ROOM,))
        self.assertIn("confidence must be one of", str(caught.exception))

    def test_a_reading_with_no_question_cannot_be_read_back(self):
        with self.assertRaises(ValueError) as caught:
            Reading("   ", True, PROVEN, (ROOM,))
        self.assertIn("no question", str(caught.exception))

    def test_evidence_with_no_kind_says_nothing_was_looked_at(self):
        with self.assertRaises(ValueError) as caught:
            Evidence("  ", "detail")
        self.assertIn("nothing was looked at", str(caught.exception))

    def test_a_shape_that_is_not_evidence_is_refused_rather_than_coerced(self):
        with self.assertRaises(TypeError) as caught:
            Reading(DRAINED, True, PROVEN, ("scanned-room", "helm", "extra"))
        self.assertIn("(kind, detail) pair", str(caught.exception))

    def test_a_bare_pair_is_admitted_as_evidence(self):
        r = Reading(DRAINED, True, PROVEN, [("scanned-room", "helm")])
        self.assertEqual(r.evidence, (ROOM,))


class DeriveRefusesManufacturedCertainty(unittest.TestCase):
    """The one door. An UNKNOWN prior may not become a firm answer downstream
    on the strength of the prior's own evidence."""

    def setUp(self):
        self.gap = unknown(SAMPLE, "the room rotation never reached it", ROWS)

    def test_an_unknown_prior_cannot_yield_PROVEN_on_the_priors_evidence(self):
        with self.assertRaises(ValueError) as caught:
            self.gap.derive(DRAINED, True, PROVEN, (ROWS,))
        self.assertIn("reaching certainty over an unanswered prior in silence",
                      str(caught.exception))
        # THE REFUSAL NAMES BOTH WAYS OUT, because a refusal that names none
        # is read as a wall and worked around somewhere else.
        self.assertIn("pass it as evidence", str(caught.exception))
        self.assertIn("`despite`", str(caught.exception))

    def test_an_unknown_prior_cannot_yield_REFUTED_either(self):
        """F1 AND F2 ARE ONE DEFECT AND THIS IS THE ARM THAT SAYS SO. The same
        unread room produced 'the alarm cannot be cleared' at one stage and 'the
        alarm is drained, cancel the repair' at the next. Both are the same
        unmeasured fact being read for whatever the reader hoped; a rule that
        banned only the reassuring direction would leave half the class."""
        with self.assertRaises(ValueError) as caught:
            self.gap.derive(DRAINED, False, REFUTED, (ROWS,))
        self.assertIn("reaching certainty over an unanswered prior in silence",
                      str(caught.exception))

    def test_a_DIFFERENT_question_may_be_answered_on_the_same_evidence(self):
        """The task/2463 answer (c), and it was right.

        Testing EVIDENCE NOVELTY at this door is a proxy that breaks the moment
        the proposition changes. This prior is
        UNKNOWN about whether the owed room drained — but it CARRIES the rooms
        the sample passed through, so "did this sample reach room X" is proven
        by exactly the evidence already in hand, with nothing new to add. The
        old rule refused this and would have admitted any irrelevant fresh
        Evidence at all, which is the test being wrong in both directions at
        once."""
        answered = self.gap.derive(
            "did this sample pass through the rooms it names", True, PROVEN,
            (ROWS,),
            despite="what was missing is WHICH room the owed row sits in; this "
                    "question is about what the sample covered, which the same "
                    "evidence states outright")
        self.assertTrue(answered.proven)
        self.assertIn("this question is about what the sample covered",
                      answered.despite)
        # AND IT IS DISCLOSED, NOT MERELY PERMITTED. A certainty reached over
        # an unanswered prior is exactly the reading a later reader should be
        # able to argue with, so the grounds ride in the generated sentence.
        self.assertIn("answered over an unanswered prior", answered.why())
        self.assertIn("the room rotation never reached it", answered.why())

    def test_despite_must_say_something(self):
        """An empty string is a flag wearing prose, and a flag is the bypass
        this argument exists NOT to be."""
        for blank in ("", "   "):
            with self.subTest(blank=repr(blank)):
                with self.assertRaises(ValueError):
                    self.gap.derive(DRAINED, True, PROVEN, (ROWS,),
                                    despite=blank)

    def test_a_reading_that_needed_no_escape_records_none(self):
        """THE CONTROL for the two arms above: `despite` must be absent when it
        was not used, or a reader cannot tell the escape from the ordinary
        case, and every derived reading would look like a reach."""
        fresh = self.gap.derive(DRAINED, True, PROVEN,
                                (("read-the-room", "helm, no such row"),),
                                despite="this should be dropped, not honoured")
        self.assertTrue(fresh.proven)
        self.assertIsNone(fresh.despite,
                          "evidence was supplied, so nothing was reached over")
        self.assertNotIn("unanswered prior", fresh.why())

    def test_the_refusal_names_the_fact_the_prior_lacked(self):
        with self.assertRaises(ValueError) as caught:
            self.gap.derive(DRAINED, True, PROVEN, (ROWS,))
        self.assertIn("the room rotation never reached it", str(caught.exception))

    def test_a_stage_that_measures_for_itself_may_answer(self):
        """The control. Without this the rule above could be a blanket ban on
        ever resolving an UNKNOWN, which would be a different and worse bug."""
        r = self.gap.derive(DRAINED, True, PROVEN, (ROWS, ROOM))
        self.assertTrue(r.proven)
        self.assertIs(r.prior, self.gap)

    def test_a_grounded_prior_needs_no_fresh_evidence(self):
        sampled = proven(SAMPLE, True, ROOM)
        r = sampled.derive(DRAINED, True, PROVEN, (ROOM,))
        self.assertTrue(r.proven)

    def test_deriving_another_unknown_is_always_allowed(self):
        r = self.gap.derive(DRAINED, None, UNKNOWN, (), "and neither did this one")
        self.assertTrue(r.unknown)
        self.assertEqual(r.lacked, "and neither did this one")


class CarryKeepsTheOriginalMissingFact(unittest.TestCase):

    def test_carry_keeps_the_fact_rather_than_restating_it(self):
        gap = unknown(SAMPLE, "the pending census could not be taken", ROWS)
        self.assertEqual(gap.carry(DRAINED).lacked,
                         "the pending census could not be taken")

    def test_carry_keeps_the_priors_evidence_and_the_link(self):
        gap = unknown(SAMPLE, "the pending census could not be taken", ROWS)
        carried = gap.carry(DRAINED)
        self.assertEqual(carried.evidence, (ROWS,))
        self.assertIs(carried.prior, gap)

    def test_carry_on_a_grounded_reading_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            proven(SAMPLE, True, ROOM).carry(DRAINED)
        self.assertIn("carry propagates a refusal", str(caught.exception))


class DisclosureIsGeneratedFromTheObject(unittest.TestCase):

    def test_chain_is_oldest_first_and_keeps_every_link(self):
        a = proven(SAMPLE, True, ROOM)
        b = a.derive(DRAINED, False, REFUTED, (ROWS,))
        c = b.derive("is a repair owed", True, PROVEN, (CURSOR,))
        self.assertEqual([r.question for r in c.chain()],
                         [SAMPLE, DRAINED, "is a repair owed"])

    def test_a_chain_discloses_an_unknown_two_stages_back(self):
        gap = unknown(SAMPLE, "the room rotation never reached it")
        end = gap.carry(DRAINED).derive(
            "is a repair owed", True, PROVEN, (CURSOR,))
        self.assertEqual([r.confidence for r in end.chain()],
                         [UNKNOWN, UNKNOWN, PROVEN])

    def test_why_changes_with_the_answer_it_describes(self):
        """Prose written BESIDE an object drifts from it; prose computed FROM
        it cannot. Two readings differing only in their answer must not produce
        the same sentence."""
        yes = proven(DRAINED, True, ROOM).why()
        no = proven(DRAINED, False, ROOM).why()
        self.assertIn("True", yes)
        self.assertIn("False", no)
        self.assertNotEqual(yes, no)

    def test_why_on_an_unknown_names_the_missing_fact(self):
        self.assertIn("the room rotation never reached it",
                      unknown(SAMPLE, "the room rotation never reached it").why())

    def test_because_reads_evidence_by_kind(self):
        r = proven(SAMPLE, True, ROOM, CURSOR)
        self.assertEqual(r.because("cursor"), 4096)
        self.assertIsNone(r.because("never-looked-at"))


class ConstructorsAreTheSameType(unittest.TestCase):

    def test_refuted_defaults_its_answer_to_the_fact_it_states(self):
        r = refuted(DRAINED, ROOM)
        self.assertTrue(r.refuted)
        self.assertEqual(r.evidence, (ROOM,))
        self.assertIs(r.answer, False)

    def test_refuted_accepts_a_richer_negative(self):
        r = refuted(DRAINED, ROOM, answer=("helm", "row-7"))
        self.assertEqual(r.answer, ("helm", "row-7"))
        self.assertTrue(r.refuted)

    def test_readings_compare_by_value(self):  # noqa: VACUOUS_ASSERTION — the unconditional assertEqual on the first line is the positive
        # control; the assertNotEqual pair below is what proves the
        # equality is by VALUE rather than a comparison that ignores
        # fields, so the absence assertions are the point of the arm.
        self.assertEqual(proven(SAMPLE, True, ROOM), proven(SAMPLE, True, ROOM))
        # And the control that makes the equality above mean something: value
        # equality that ignored a field would satisfy the first assertion too.
        self.assertNotEqual(proven(SAMPLE, True, ROOM),
                            proven(SAMPLE, True, CURSOR))
        self.assertNotEqual(proven(SAMPLE, True, ROOM),
                            proven(SAMPLE, False, ROOM))


if __name__ == "__main__":
    unittest.main()
