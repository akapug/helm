"""One origin, one coordinate, and producers that keep every candidate."""

import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import panetail, seat, seat_lifecycle  # noqa: E402,F401 — `seat` is the facade the impl import owes


class NormalizeIsTheOnlyOriginTest(unittest.TestCase):
    """The coordinate space is the normalized line list and nothing else."""

    def test_ansi_is_stripped_and_the_line_count_is_preserved(self):
        tail = "\x1b[31mred\x1b[0m\nplain\n\x1b[1mbold\x1b[0m"
        self.assertEqual(panetail.normalize(tail), ["red", "plain", "bold"])

    def test_NOTHING_IS_LOWERCASED_so_no_offset_can_shift(self):
        """THE DOTTED-I REGRESSION, KILLED STRUCTURALLY RATHER THAN GUARDED.

        `str.lower()` is not length-preserving: a capital I-with-dot becomes
        TWO codepoints, so any offset taken on a lowercased copy and applied to
        the original lands one character early for every such character above
        it. That is what let a wall's end consume its own newline. This module
        never lowercases, so there is no second representation to mistranslate.
        """
        line = "İpek hit the usage limit"
        got = panetail.normalize(line)[0]
        # THE FIXTURE IS THE HAZARD: assert the character really does expand
        # under lower(), or this arm proves nothing on a build that changed it.
        self.assertEqual(len(line.lower()), len(line) + 1,
                         "the dotted capital no longer expands, so this arm "
                         "no longer exercises the hazard it was written for")
        self.assertEqual(got, line)
        self.assertEqual(len(got), len(line))

    def test_positions_are_a_TOTAL_ORDER_by_construction(self):
        """(line, col) compared lexicographically, so two events on ONE line
        still order deterministically and the invariant never rests on 'I have
        not seen them share a line'."""
        a, b, c = (panetail.Pos(1, 0), panetail.Pos(1, 7), panetail.Pos(2, 0))
        self.assertLess(a, b)
        self.assertLess(b, c)
        self.assertEqual(sorted([c, a, b]), [a, b, c])


class EveryCandidateSurvivesTest(unittest.TestCase):
    """A producer that returns only the current answer cannot be audited."""

    RESURRECTION = "\n".join([
        "Do you want to proceed?",
        "1. Yes",
        "2. No, keep going",
        "",
        "...the user answered, work happened...",
        "",
        "Pick a target to inspect:",
        "1. src/main.py",
        "2. src/util.py",
        "3. tests/",
    ])

    def test_the_SHIPPED_helper_SUPPRESSES_and_suppression_discards_evidence(self):  # noqa: VACUOUS_ASSERTION — the empty observable IS the subject, and the same call carries the control that makes it readable: option_runs still finds TWO runs in the tail the shipped reader answered nothing for, against ZERO for an empty tail
        """MUST-HIT, AND IT DRIVES THE CODE THIS MODULE SITS BESIDE.

        The shipped helper no longer resurrects the older menu — it answers
        NOTHING. That is the right refusal and it is also the whole remaining
        difference, because NOTHING is not a description of this tail. A
        consumer handed `[]` cannot tell "no dialog was ever rendered here"
        from "a dialog was rendered and a newer list suppressed it", and those
        two call for different acts: the first is a quiet pane, the second is
        a pane where something is still waiting above the noise.

        This arm asserts the suppression so the arms below stay a real
        differential rather than a restatement. If the helper ever answers
        something here again, they are comparing against a producer that
        changed and this fails first, loudly.
        """
        got = seat_lifecycle._prompt_options(self.RESURRECTION)
        self.assertEqual(got, [],
                         "the shipped helper answers %r for a tail whose "
                         "newest run does not qualify, so the difference the "
                         "arms below name is not the one being measured" % got)
        # THE SAME CALL, SHOWING WHAT SUPPRESSION COSTS: an empty tail and a
        # suppressed dialog are the SAME answer from the helper and different
        # answers here. Without this the assertion above is just agreement.
        self.assertEqual(seat_lifecycle._prompt_options(""), [])
        self.assertEqual(len(panetail.option_runs(panetail.normalize(""))), 0)
        self.assertEqual(
            len(panetail.option_runs(panetail.normalize(self.RESURRECTION))), 2,
            "both runs survive here, which is the evidence `[]` threw away")

    def test_the_newer_run_is_LAST_and_the_older_one_is_still_visible(self):
        runs = panetail.option_runs(panetail.normalize(self.RESURRECTION))
        self.assertEqual(len(runs), 2, runs)
        older, newer = runs
        self.assertEqual(older.qualification, panetail.QUALIFIED)
        self.assertEqual(newer.qualification, panetail.UNQUALIFIED)
        # ORDER IS POSITIONAL, so a consumer can see that something newer sits
        # below the actionable one instead of being handed the old one.
        self.assertLess(older.start, newer.start)
        self.assertEqual([lbl for _n, lbl in newer.options],
                         ["src/main.py", "src/util.py", "tests/"])

    def test_a_run_records_WHAT_ENDED_IT(self):
        """`ended_by` is the evidence a run is over. Without it a consumer
        cannot tell a dialog that is still drawn from one that was answered."""
        runs = panetail.option_runs(panetail.normalize(self.RESURRECTION))
        self.assertIsNotNone(runs[0].ended_by)
        self.assertIsNone(runs[-1].ended_by,
                          "the last run reaches the end of the tail")

    def test_a_blank_line_does_not_break_a_run(self):
        """A preserved recognition rule: dialogs are drawn with gaps."""
        runs = panetail.option_runs(panetail.normalize(
            "1. Yes\n\n2. No\n"))
        self.assertEqual(len(runs), 1, runs)
        self.assertEqual(runs[0].qualification, panetail.QUALIFIED)

    def test_ordinary_numbered_prose_is_UNQUALIFIED(self):
        """The yes/no discriminator is what separates an actuator from a
        keystroke generator; a plan's own steps are contiguous too."""
        runs = panetail.option_runs(panetail.normalize(
            "1. Read the file\n2. Patch it\n3. Run the tests\n"))
        self.assertEqual(len(runs), 1, runs)
        self.assertEqual(runs[0].qualification, panetail.UNQUALIFIED)

    def test_a_tail_with_no_runs_yields_none(self):
        """MUST-MISS: the producer does not manufacture a candidate."""
        self.assertEqual(
            panetail.option_runs(panetail.normalize("just prose\nmore prose")),
            [])


if __name__ == "__main__":
    unittest.main()


class SuppressionIsNotClearanceTest(unittest.TestCase):
    """The one conclusion a consumer will reach for and must not.

    A newer UNQUALIFIED run means no OLD choice is offered. It says nothing
    about whether the older dialog cleared, and a consumer that reads it that
    way has manufactured a clearance from an absence.
    """

    def test_a_suppressing_run_does_not_report_the_older_one_ENDED_BY_IT(self):
        runs = panetail.option_runs(panetail.normalize("\n".join([
            "1. Yes", "2. No", "prose that ends the run",
            "1. alpha", "2. beta",
        ])))
        older, newer = runs
        self.assertEqual(older.qualification, panetail.QUALIFIED)
        self.assertEqual(newer.qualification, panetail.UNQUALIFIED)
        # The older run's terminator is the PROSE LINE that actually closed it,
        # not the newer run — so nothing here can be read as "the newer run
        # cleared the older dialog".
        self.assertEqual(older.ended_by.line, 2)
        self.assertLess(older.ended_by, newer.start)

    def test_the_producer_reports_NO_clearance_field_at_all(self):
        """MUST-MISS, and it is structural: there is no field a consumer could
        misread as clearance, because clearance is not this producer's fact."""
        runs = panetail.option_runs(panetail.normalize("1. Yes\n2. No\n"))
        self.assertNotIn("cleared", runs[0]._fields)
        self.assertNotIn("current", runs[0]._fields)
        self.assertEqual(set(runs[0]._fields),
                         {"start", "end", "options", "qualification",
                          "ended_by"})


class AuthorshipNeedsEvidenceTest(unittest.TestCase):
    """The tail cannot name who typed; it can only say the composer is busy."""

    def test_an_occupied_composer_with_NO_evidence_is_OCCUPIED_not_human(self):
        """THE DEFAULT MUST NOT BE AN ATTRIBUTION. helm's own placed text and a
        human's typing render identically, so a producer that guesses here is
        inventing an author — and the guess that matters is the one that would
        let an actuator type over a person."""
        obs = panetail.composer_observations(panetail.normalize("❯ half a sen"))
        self.assertEqual(len(obs), 1, obs)
        self.assertEqual(obs[0].ownership, panetail.OCCUPIED)
        self.assertNotEqual(obs[0].ownership, panetail.HUMAN_DRAFT)
        self.assertNotEqual(obs[0].ownership, panetail.AI)

    def test_evidence_the_caller_PROVES_attributes_to_AI(self):
        obs = panetail.composer_observations(
            panetail.normalize("❯ 2"), placed="2")
        self.assertEqual(obs[0].ownership, panetail.AI)

    def test_text_the_caller_did_NOT_place_is_a_HUMAN_DRAFT(self):
        obs = panetail.composer_observations(
            panetail.normalize("❯ half a sentence"), placed="2")
        self.assertEqual(obs[0].ownership, panetail.HUMAN_DRAFT)

    def test_an_empty_composer_is_BARE_and_needs_no_author(self):
        obs = panetail.composer_observations(panetail.normalize("❯"))
        self.assertEqual(obs[0].ownership, panetail.BARE)

    def test_a_STRUCTURED_field_keeps_its_provenance(self):
        """THE SEAM THAT CURRENTLY LOSES IT. `_CLIAdapter.read` appends the
        metaharness's own current-input field as synthetic prompt text, so
        downstream it is indistinguishable from a scraped row — and a scrollback
        row can imitate a scraped row while nothing can imitate the field."""
        obs = panetail.composer_observations(
            panetail.normalize("❯ scraped one"), structured="field one")
        kinds = {o.provenance for o in obs}
        self.assertEqual(kinds, {panetail.STRUCTURED, panetail.SCRAPED})
        field = [o for o in obs if o.provenance == panetail.STRUCTURED][0]
        self.assertEqual(field.text, "field one")
        self.assertIsNone(field.pos, "a field has no line to point at")

    def test_EVERY_composer_row_survives_not_just_the_last(self):
        """A scrollback prompt and a live one are both observations; deciding
        between them needs the other events, and that judgment is not here."""
        obs = panetail.composer_observations(panetail.normalize(
            "❯ older\nsome output\n❯ newer"))
        self.assertEqual([o.text for o in obs], ["older", "newer"])
        self.assertLess(obs[0].pos, obs[1].pos)

    def test_no_composer_shape_yields_NOTHING(self):
        """MUST-MISS: ordinary prose does not become a composer.

        The positive control rides the SAME call, because an empty result also
        describes a producer that reads nothing at all.
        """
        prose = "just prose"
        self.assertEqual(
            panetail.composer_observations(panetail.normalize(prose)), [])
        # UNCONDITIONAL POSITIVE on the same observable: the identical call,
        # one composer row added, must find it.
        found = panetail.composer_observations(
            panetail.normalize(prose + "\n❯ a draft"))
        self.assertEqual([o.text for o in found], ["a draft"])


class OneWallOneVerdictTest(unittest.TestCase):
    """Spelling, line and expiry are fields of one record, never three scans."""

    WALL = "You've reached your usage limit"

    def test_every_wall_occurrence_is_exposed_in_order(self):
        walls = panetail.wall_occurrences(panetail.normalize(
            "\n".join([self.WALL, "work happened", self.WALL])))
        self.assertEqual(len(walls), 2, walls)
        self.assertLess(walls[0].pos, walls[1].pos)

    def test_the_verdict_travels_with_the_line_that_produced_it(self):
        """The pairing is structural: a consumer cannot hold a verdict from one
        wall beside the text of another, because they are one record."""
        walls = panetail.wall_occurrences(panetail.normalize(self.WALL))
        self.assertEqual(len(walls), 1)
        self.assertIn("usage limit", walls[0].line)
        self.assertTrue(walls[0].force, "an in-force verdict must be named")
        self.assertTrue(walls[0].spelling, "the matching spelling is recorded")

    def test_a_line_yields_AT_MOST_ONE_wall(self):
        """MUST-HIT FIRST: prove more than one spelling really can match this
        line, or the deduplication below is asserting nothing."""
        from helm import seat_lifecycle
        import re as _re
        line = "usage balance exhausted — you've reached your usage limit"
        matched = [p for p in seat_lifecycle._wall_patterns()
                   if _re.search(p, line, _re.I)]
        self.assertGreater(len(matched), 1,
                           "only one spelling matches, so this fixture cannot "
                           "exercise the overlapping-match tie")
        self.assertEqual(len(panetail.wall_occurrences(
            panetail.normalize(line))), 1)

    def test_a_clean_tail_yields_no_walls_and_the_producer_still_reads(self):
        """MUST-MISS with its positive control on the SAME call: an empty list
        also describes a producer that scans nothing."""
        clean = "all quiet\nnothing to report"
        self.assertEqual(
            panetail.wall_occurrences(panetail.normalize(clean)), [])
        found = panetail.wall_occurrences(
            panetail.normalize(clean + "\n" + self.WALL))
        self.assertEqual(len(found), 1, found)

    def test_an_empty_pattern_table_REFUSES_rather_than_reporting_clean(self):
        """A table that lost its patterns must not answer 'no walls anywhere'."""
        from unittest import mock
        from helm import seat_lifecycle
        with mock.patch.object(seat_lifecycle, "_wall_patterns",
                               return_value=()):
            with self.assertRaises(RuntimeError):
                panetail.wall_occurrences(panetail.normalize(self.WALL))


class OneOrderedListTest(unittest.TestCase):
    """Every producer's candidates, merged on the one coordinate."""

    TAIL = "\n".join([
        "You've reached your usage limit",
        "1. Yes",
        "2. No",
        "some work the seat produced",
        "❯ a draft",
    ])

    def test_every_producer_contributes_in_positional_order(self):
        parsed = panetail.parse(self.TAIL)
        kinds = [e.kind for e in parsed.events]
        self.assertEqual(kinds, [panetail.WALL_EVENT, panetail.RUN,
                                 panetail.COMPOSER_EVENT])
        positions = [e.pos for e in parsed.events]
        self.assertEqual(positions, sorted(positions),
                         "the merged list is not ordered")

    def test_the_structured_field_is_NOT_given_a_fake_position(self):
        """It describes live state, not something rendered somewhere. Inventing
        a coordinate for it is the exact move this module removes."""
        parsed = panetail.parse("❯ scraped", structured="the live field")
        self.assertIsNotNone(parsed.structured)
        self.assertIsNone(parsed.structured.pos)
        self.assertNotIn(None, [e.pos for e in parsed.events])
        self.assertEqual([e.detail.text for e in parsed.events
                          if e.kind == panetail.COMPOSER_EVENT], ["scraped"])

    def test_parse_DECIDES_NOTHING_about_currency(self):
        """MUST-MISS, structural: the result exposes evidence and no verdict,
        so a consumer cannot read currency off it by accident."""
        parsed = panetail.parse(self.TAIL)
        # `lines` is EVIDENCE (the normalized origin the positions index), not
        # a verdict — consumers need it to attribute what sits below an event.
        # The pin is updated deliberately; what it forbids is a field a reader
        # could mistake for a decision.
        self.assertEqual(set(parsed._fields), {"events", "structured", "lines"})
        self.assertEqual(set(parsed.events[0]._fields), {"pos", "kind", "detail"})
        for banned in ("current", "in_force", "cleared", "state"):
            self.assertNotIn(banned, parsed.events[0]._fields)

    def test_a_wall_drawn_over_by_a_dialog_is_still_in_the_list(self):
        """Position is evidence about rendering, never about force. The wall
        survives the dialog being drawn after it."""
        parsed = panetail.parse(self.TAIL)
        walls = [e for e in parsed.events if e.kind == panetail.WALL_EVENT]
        runs = [e for e in parsed.events if e.kind == panetail.RUN]
        self.assertEqual(len(walls), 1)
        self.assertLess(walls[0].pos, runs[0].pos,
                        "the fixture must draw the dialog AFTER the wall or it "
                        "does not exercise the case")


class UnattributedIsNotWorkTest(unittest.TestCase):
    """A gap in evidence may hide activity; it may never manufacture it."""

    def kinds(self, tail):
        lines = panetail.normalize(tail)
        obs = panetail.composer_observations(lines)
        return [panetail.attribute(lines, i, composers=obs)
                for i in range(len(lines))]

    def test_unrecognised_prose_is_UNATTRIBUTED_not_work(self):
        self.assertEqual(self.kinds("some sentence nobody classified"),
                         [panetail.UNATTRIBUTED])

    def test_a_wall_restated_is_not_a_reply_to_itself(self):
        self.assertEqual(self.kinds("You've reached your usage limit"),
                         [panetail.NON_WORK])

    def test_a_BARE_composer_under_a_wall_is_the_parked_state_not_progress(self):
        self.assertEqual(self.kinds("❯"), [panetail.NON_WORK])

    def test_a_composer_carrying_TEXT_is_work(self):
        """A submission is real evidence the seat moved, and this is the one
        positive attribution the tail genuinely supports."""
        self.assertEqual(self.kinds("❯ a human typed this"), [panetail.WORK])

    def test_the_three_answers_are_DISTINCT_on_one_tail(self):
        """MUST-HIT: if the fixture ever collapses to two kinds, every arm
        above is asserting a distinction the parser stopped making."""
        got = set(self.kinds("\n".join([
            "You've reached your usage limit",
            "some unrecognised prose",
            "❯ a human typed this",
        ])))
        self.assertEqual(got, {panetail.NON_WORK, panetail.UNATTRIBUTED,
                               panetail.WORK})


class StandingIsPerTypeTest(unittest.TestCase):
    """Several things can be in force at once; none out-ranks another here."""

    WALL = "You've reached your usage limit"

    def test_a_wall_under_a_dialog_is_STILL_IN_FORCE(self):
        """THE CASE THAT DROVE THE LIFECYCLE. Position says the dialog was
        drawn later; it does not say the wall stopped binding."""
        parsed = panetail.parse("\n".join([self.WALL, "1. Yes", "2. No"]))
        self.assertEqual(panetail.wall_standing(parsed).state,
                         panetail.IN_FORCE)
        # And the modal is judged on its own events, not against the wall.
        self.assertIn(panetail.modal_standing(parsed).state,
                      (panetail.IN_FORCE, panetail.UNDETERMINED))

    def test_UNATTRIBUTED_prose_below_a_wall_does_NOT_clear_it(self):
        """The contract reaches the consumer: a gap in evidence may
        sustain a wall and may never lift one."""
        parsed = panetail.parse("\n".join([self.WALL, "some unrecognised prose"]))
        got = panetail.wall_standing(parsed)
        self.assertEqual(got.state, panetail.IN_FORCE, got.why)

    def test_ATTRIBUTED_work_below_a_wall_DOES_clear_it(self):
        """MUST-HIT PAIR: without this the arm above passes on a build where
        NOTHING ever clears a wall, which is a different defect."""
        parsed = panetail.parse("\n".join([self.WALL, "❯ a human typed this"]))
        got = panetail.wall_standing(parsed)
        self.assertEqual(got.state, panetail.ENDED, got.why)

    def test_there_is_NO_parameter_to_turn_the_contract_off(self):
        """An option that restores the looser reading is the contract being
        optional. If it proves too strict that is a measured finding, not a
        flag."""
        import inspect
        params = inspect.signature(panetail.wall_standing).parameters
        self.assertEqual(list(params), ["parsed"])

    def test_a_newest_UNQUALIFIED_run_leaves_the_modal_UNDETERMINED(self):
        """Suppression is not clearance: no old choice is offered, and nothing
        here says the older dialog went away."""
        parsed = panetail.parse("\n".join([
            "1. Yes", "2. No", "prose", "1. alpha", "2. beta"]))
        got = panetail.modal_standing(parsed)
        self.assertEqual(got.state, panetail.UNDETERMINED, got.why)
        self.assertNotEqual(got.state, panetail.ENDED)


class UnanchoredWallIsUnknownNotAWallTest(unittest.TestCase):
    """A differential, as a test: the pool refusal, no anchor.

    THE MEASUREMENT THIS PINS. On the same pane tail and the same clock, main
    read the tail as ENDED — the pool spelling was not in the table, so there
    was no wall at all — and this branch read IN-FORCE with force=UNANCHORED,
    and it stayed in force forever, because an UNANCHORED verdict has no clock
    running and so can never expire.

    UNANCHORED means the line dates its reset from an observation instant this
    reader was not given. That is UNKNOWN. Unknown is not a wall and it is not
    a clearance either, so the answer is UNDETERMINED — the state this module
    already keeps for a tail that carries no evidence either way.
    """

    #: A real producer spelling, copied from a seat proxy log.
    POOL = ("API Error: Request rejected (429) · no available credential for "
            "gpt-5.6-sol via provider codex: 6 cooling down "
            "(reset in 2h27m37s)")
    #: The vendor's own dated wall: its expiry is IN the line, so it needs no
    #: anchor, and it is the positive control for every arm below.
    DATED = "You've hit your weekly limit · resets Sep 17, 12am (UTC)"
    #: The clock, pinned before both resets.
    NOW = datetime.datetime(2026, 9, 16, 3, 31, 45)

    def test_the_fixture_really_IS_a_wall_line_on_this_branch(self):
        """MUST-HIT. Every arm below is about a tail the table recognises; if
        the spelling stopped matching they would all pass over no wall at all,
        which is a different build and a different answer."""
        walls = panetail.wall_occurrences(panetail.normalize(self.POOL))
        self.assertEqual(len(walls), 1, walls)
        self.assertIn("cooling down", walls[0].line)
        self.assertEqual(walls[0].force, seat.WALL_UNANCHORED)

    def test_an_unanchored_wall_is_NOT_in_force(self):
        """The branch half of the differential: the reading that was IN-FORCE
        forever must not be in force."""
        got = panetail.wall_standing(panetail.parse(self.POOL, now=self.NOW))
        self.assertNotEqual(got.state, panetail.IN_FORCE, got.why)
        self.assertEqual(got.state, panetail.UNDETERMINED, got.why)
        self.assertIn("UNKNOWN", got.why)

    def test_the_main_equivalent_reading_of_the_same_tail_is_not_walled(self):
        """The other half, on the SAME tail and the SAME clock: with the pool
        spelling out of the table — which is what main had — the tail carries
        no wall, and the answer is ENDED."""
        from unittest import mock
        full = seat_lifecycle._wall_patterns()
        without = tuple(p for p in full if "cooling down" not in p)
        self.assertEqual(  # noqa: VACUOUS_ASSERTION — the length delta IS the control: it proves the shipped table lost exactly the pool spelling
            len(without), len(full) - 1,
            "the pool spelling is not in the table, so this is not the "
            "main-equivalent table and the differential is comparing a "
            "build against itself")
        with mock.patch.object(seat_lifecycle, "_wall_patterns",
                               return_value=without):
            got = panetail.wall_standing(panetail.parse(self.POOL,
                                                        now=self.NOW))
            native = panetail.wall_standing(panetail.parse(self.DATED,
                                                           now=self.NOW))
        self.assertEqual(got.state, panetail.ENDED, got.why)
        self.assertIn("no quota wall", got.why)
        # UNCONDITIONAL POSITIVE CONTROL ON THE SAME TABLE: the main-equivalent
        # table still walls the vendor's own spelling, so ENDED above is about
        # the pool line and not about a table that was blinded.
        self.assertEqual(native.state, panetail.IN_FORCE, native.why)
        self.assertIn("not expired", native.why)

    def test_UNKNOWN_is_rendered_DISTINCTLY_from_ENDED(self):
        """A reader told a wall ENDED acts on it; a reader told UNKNOWN goes
        and measures. Collapsing the two loses the only instruction the
        sentence carries."""
        unknown = panetail.wall_standing(panetail.parse(self.POOL,
                                                        now=self.NOW))
        self.assertIn("UNKNOWN", unknown.why)
        ended = panetail.wall_standing(panetail.parse(
            "\n".join([self.DATED, "❯ a human typed this"]), now=self.NOW))
        self.assertEqual(ended.state, panetail.ENDED, ended.why)
        self.assertIn("attributed work", ended.why)
        self.assertNotIn("UNKNOWN", ended.why)

    def test_a_dated_wall_under_a_pinned_clock_is_STILL_IN_FORCE(self):
        """THE POSITIVE CONTROL for every arm above: the same call, on a wall
        whose expiry is in its own line and in the future, still answers
        IN_FORCE. Without it a build where NOTHING walls passes all of them."""
        got = panetail.wall_standing(panetail.parse(self.DATED, now=self.NOW))
        self.assertEqual(got.state, panetail.IN_FORCE, got.why)
        self.assertIn("not expired", got.why)

    def test_attributed_work_below_an_unanchored_wall_still_CLEARS_it(self):
        """UNKNOWN does not outrank evidence. Work rendered below the line is
        a real clearance, so the position scan is asked BEFORE the unknown
        verdict and ENDED still wins."""
        got = panetail.wall_standing(panetail.parse(
            "\n".join([self.POOL, "❯ a human typed this"]), now=self.NOW))
        self.assertEqual(got.state, panetail.ENDED, got.why)
        self.assertIn("attributed work", got.why)

    def test_UNANCHORED_is_about_the_missing_instant_not_a_harmless_line(self):
        """The same line, handed an observation instant, IS a wall. So the
        UNDETERMINED answer above is a statement about this reader's evidence
        and never a claim that the pool refusal does not matter."""
        # THE FIXTURE CARRIES THE HAZARD: the line states a DURATION, which is
        # the whole reason it needs an instant to be dated from.
        self.assertIn("reset in 2h27m37s", self.POOL)
        later = self.NOW + datetime.timedelta(hours=1)
        self.assertEqual(seat._wall_in_force(self.POOL, later,
                                             observed=self.NOW),
                         seat.WALL_IN_FORCE)
        # The same line, the same clock, the anchor withheld: this is the whole
        # difference, and it is the verdict the pane judge is stuck with.
        self.assertEqual(seat._wall_in_force(self.POOL, later),
                         seat.WALL_UNANCHORED)


class ClearanceAsksTheDialogNotTheScreenTest(unittest.TestCase):
    """The full actuation transition, driven — not a classifier reading.

    The precondition, kept explicit: a wall-plus-human-dialog tail used
    as the BEFORE read is refused by admission before anything is sent, so it
    cannot demonstrate a clearance defect. The BEFORE read must be an
    admissible vendor modal whose safe option matches the caller's intent; only
    then does the AFTER read reach the clearance ladder at all.
    """

    MODAL = "\n".join([
        "Your credit balance is too low",
        "1. Continue with the free model",
        "2. Buy more credits",
    ])
    #: The AFTER read: walled, and a dialog is STILL on screen.
    AFTER = "\n".join([
        "You've reached your usage limit",
        "Do you want to proceed?",
        "1. Yes",
        "2. No",
    ])

    def test_the_AFTER_tail_does_not_classify_as_a_modal(self):  # noqa: VACUOUS_ASSERTION — this arm IS the positive control for the pair below it; it exists to fail first when the fixture stops exercising the hazard, so it has no observable of its own to control
        """MUST-HIT: the whole hazard is that a scalar reads this as NOT a
        dialog. If it ever classifies as MODAL_STATE the arm below is proving
        nothing, and this fails first."""
        from helm import harness, seat_lifecycle
        state = seat_lifecycle._classify_pane_tail(self.AFTER)[0]
        self.assertNotEqual(state, harness.MODAL_STATE,
                            "the after-tail now reads as a modal, so a scalar "
                            "check would already refuse and this fixture no "
                            "longer exercises the clearance hazard")

    def test_a_STILL_ACTIVE_dialog_is_not_certified_as_cleared(self):
        """The dialog is asked about itself: a replacement or still-live dialog
        answers UNDETERMINED, which is not clearance."""
        parsed = panetail.parse(self.AFTER)
        standing = panetail.modal_standing(parsed)
        # The POSITIVE value, not the absence of the wrong one: this function
        # answers UNDETERMINED or ENDED and nothing else, so naming the answer
        # it must give is exact and carries its own control.
        self.assertEqual(standing.state, panetail.UNDETERMINED, standing.why)

    def test_a_pane_that_really_went_quiet_DOES_clear(self):
        """MUST-HIT PAIR: without this, the arm above passes on a build where
        NOTHING is ever certified cleared, which is a different defect."""
        parsed = panetail.parse("the dialog is gone\n❯ a human typed this")
        self.assertEqual(panetail.modal_standing(parsed).state, panetail.ENDED)
