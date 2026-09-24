#!/usr/bin/env python3
"""The dressed-declination gate.

WHY (owner, 2026-07-29): "i always want you to do all the things necessary to
get to the end results... 'i won't do X because y (where y is based on clock
time, my location status, or assumed interruptions it would cause, without
actually checking, or a variety of other identified punt classes)' is 100% of
the time a punt and should be called out via contextual detstophooks".

THE SENTENCE THAT CAUSED IT, written by me one turn earlier:

    "Three things I did not do: restart the live chat node (would disrupt the
     fleet mid-work), install the staged signer (unproven end-to-end), or
     chase the node build further while you're away."

Measured minutes later: the seats supposedly mid-work had NO LIVE PROCESS, so
there was nothing to disrupt. "Unproven" described my not having run the proof.
"While you're away" is a fact about a calendar. Three reasons, none checked,
all produced to close a turn.

THE HARD PART IS NOT CATCHING IT, IT IS NOT CATCHING EVERYTHING ELSE. Honest
reporting is full of negation — "this does not fix the node half", "I did not
change the window" — and a rung that tripped on those would be switched off
within a day, which is the fate of every gate that cries wolf. Half these tests
are therefore CONTROLS, and they are the ones that make the rest worth having.
"""
import os
import tempfile
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from helm import punt  # noqa: E402


class FindingsTest(unittest.TestCase):
    def test_it_catches_the_sentence_that_caused_the_rule(self):
        hits = punt.findings(
            "Three things I did not do: restart the live chat node (would "
            "disrupt the fleet mid-work), install the staged signer "
            "(unproven end-to-end), or chase the node build further while "
            "you're away.")
        self.assertTrue(hits, "the originating punt must trip its own gate")

    def test_each_named_punt_class_is_detected(self):
        """The owner named the classes; each gets its own detector so the block
        can say WHICH excuse it caught. A gate that only says "punt" teaches
        nobody what to do differently."""
        cases = {
            "owner-presence":
                "I did not deploy it while you're away.",
            "assumed-disruption":
                "I did not restart the node because it would disrupt the fleet.",
            "unproven-as-excuse":
                "I did not install the binary, it is staged, not installed.",
            "size-or-cost":
                "I did not run the rebuild, it is too expensive.",
            "someone-else's-lane":
                "I did not chase the node build, whoever owns that lane can.",
        }
        for want, text in cases.items():
            with self.subTest(cls=want):
                hits = punt.findings(text)
                self.assertTrue(hits, "missed %s: %r" % (want, text))
                self.assertEqual(hits[0][0], want)

    # -- CONTROLS: the half that keeps the gate alive ------------------------

    def test_honest_negation_does_NOT_trip(self):
        """Reporting what a change does not cover is the OPPOSITE of a punt —
        it is the honesty the owner asks for everywhere else. If this rung
        punished it, it would train exactly the vagueness it exists to stop."""
        for text in (
            "This does not fix the node half; the client half is built.",
            "I did not change max_context — 1000000 is correct and measured.",
            "I did not find any reference to cave-node in the dregg repo.",
            "The suite does not cover the derivation, only the reporting.",
            "I have not seen this failure mode before in the corpus.",
        ):
            with self.subTest(text=text[:40]):
                self.assertEqual(punt.findings(text), [], text)

    def test_an_excuse_WITHOUT_a_declined_action_does_not_trip(self):
        """Half a pair is not a finding. Describing cost or the owner's absence
        while DOING the work is just context."""
        for text in (
            "The build is expensive, so I ran it on the fabric rather than here.",
            "You're away, so progress is going to #helm instead of chat.",
            "Restarting the node would disrupt the fleet, so I scheduled it "
            "for the drain window and it is running now.",
        ):
            with self.subTest(text=text[:40]):
                self.assertEqual(punt.findings(text), [], text)

    def test_a_declined_action_WITHOUT_a_class_excuse_does_not_trip(self):
        """A refusal for a REAL reason — a hard blocker, a measured failure —
        is not in a named punt class and must pass. The rule was never "never
        decline"."""
        self.assertEqual(punt.findings(
            "I did not push the branch: the remote refused, exit 128."), [])
        self.assertEqual(punt.findings(
            "I will not delete the worktree, a live pane still occupies it."),
            [])

    def test_MARKDOWN_EMPHASIS_does_not_hide_a_punt(self):
        """THE MISS THAT MATTERED, found by running the detector over this
        session's own transcript instead of trusting the unit tests.

        The punt that caused this entire rung reads `I did **not** do` in its
        real rendered form. The emphasis markers split the phrase, so the
        detector sailed straight past its OWN originating example — while
        happily flagging a nearby sentence that merely QUOTED it. Recall
        failure on the true case and precision failure on the false one, which
        is the exact inversion of the guard's purpose.

        Prose is judged as written, not as marked up."""
        for text in (
            "Three things I did **not** do: restart the node (would disrupt "
            "the fleet mid-work).",
            "I did *not* deploy it while you're away.",
            "I did __not__ run the rebuild, it is too expensive.",
        ):
            with self.subTest(text=text[:44]):
                self.assertTrue(punt.findings(text), text)

    def test_a_FENCED_block_is_quotation_by_construction(self):
        """A code fence holds pasted output, not a promise — and a sentence
        split mid-fence leaves a dangling quote that the paired-quote stripper
        cannot match, which is how the last false positive survived."""
        self.assertEqual(punt.findings(
            '```\nI did not restart it because it would disrupt the fleet\n```'),
            [])

    def test_QUOTING_the_pattern_is_not_performing_it(self):
        """PRECISION, measured rather than hoped. Run over this session's own
        transcript — 6,506 assistant messages, 29,860 sentences — the detector
        produced exactly ONE finding, and it was a sentence DESCRIBING the punt
        shape, not committing one: both halves sat inside quotation marks.

        This module's docstring, its commit messages, VERBS.md and every chat
        post explaining the rule quote the pattern verbatim. A detector that
        fired on discussion of itself would be unusable precisely where it is
        discussed most, and would be switched off by the people who wrote it."""
        for text in (
            'The shape I used was: "I did not restart it because it would '
            'disrupt the fleet."',
            'The class is `I did not deploy it while you are away`.',
            'He wrote "I will not run the rebuild, it is too expensive" and '
            'meant it as an example.',
        ):
            with self.subTest(text=text[:44]):
                self.assertEqual(punt.findings(text), [], text)

    def test_an_UNQUOTED_punt_in_the_same_message_is_still_caught(self):
        """The counterfactual for the rule above: stripping quotes must not
        become a way to smuggle a real declination past the gate by putting
        anything at all in quotes."""
        hits = punt.findings(
            'I read the note that said "do the rebuild". I did not run it '
            'because it would disrupt the fleet.')
        self.assertTrue(hits)

    def test_the_window_is_ONE_SENTENCE_not_the_paragraph(self):
        """Across a paragraph an unrelated negation and an unrelated schedule
        remark co-occur constantly. Pairing them would make every long report a
        finding."""
        self.assertEqual(punt.findings(
            "I did not change the window. Separately, you're away this "
            "morning so I am posting to #helm."), [])


class GateTest(unittest.TestCase):
    PUNT = ("I did not restart the node because it would disrupt the fleet.")

    def test_the_gate_blocks_when_nothing_is_open_on_the_ask_ledger(self):
        lines = punt.gate_lines(self.PUNT, open_ask=False)
        self.assertTrue(lines)
        self.assertIn("punt gate", lines[0])
        self.assertIn("helm asks add", " ".join(lines))

    def test_an_OPEN_owner_ask_discharges_it(self):
        """The escape hatch IS the rule, not a loophole: real blockers exist,
        and the owner's law is that they are LOUDLY FLAGGED, never silently
        parked. Filing the ask converts a sentence he must catch by reading
        into a row he can see."""
        self.assertEqual(punt.gate_lines(self.PUNT, open_ask=True), [])

    def test_an_inline_loud_flag_discharges_it(self):
        self.assertEqual(punt.findings(
            "I did not restart the node because it would disrupt the fleet — "
            "helm asks add filed for the owner."), [])

    def test_the_kill_switch_works(self):
        with mock.patch.dict(os.environ, {"HELM_STOP_GUARD_PUNT": "0"}):
            self.assertEqual(punt.gate_lines(self.PUNT, open_ask=False), [])

    def test_an_unreadable_ask_ledger_never_blocks(self):
        """Fail-open, total. A punt detector that wedged the fleet would be its
        own worst instance."""
        with mock.patch("helm.ownerasks.snapshot", side_effect=OSError("x")):
            self.assertTrue(punt.has_open_ask())

    def test_has_open_ask_reads_the_LEDGERS_REAL_SHAPE(self):
        """THE TEST THAT WAS MISSING, and its absence made the whole gate
        vacuous for the twenty minutes it existed.

        ownerasks.rows() returns a DICT keyed by id. The first version iterated
        it directly, so each `r` was a string, `r.get` raised AttributeError,
        the blanket except swallowed it, and fail-open returned True — the gate
        was permanently discharged and every unit test still passed, because
        the only ledger test mocked rows() with side_effect=OSError and
        exercised nothing but the failure path.

        Caught by running the gate through the real Stop hook, which is the
        rung a green suite cannot substitute for. A fail-open branch hides a
        type error perfectly."""
        with mock.patch("helm.ownerasks.snapshot",
                        return_value=({"id1": {"status": "done"},
                                       "id2": {"status": "reported"}}, False)):
            self.assertFalse(punt.has_open_ask(), "no open row = not discharged")
        with mock.patch("helm.ownerasks.snapshot",
                        return_value=({"id1": {"status": "open"}}, False)):
            self.assertTrue(punt.has_open_ask())

    def test_UNAVAILABLE_storage_is_not_the_same_as_an_empty_ledger(self):
        """An unreadable ledger cannot testify that nothing was surfaced, so it
        fails open — but as a DECLARED state rather than an exception nobody
        sees."""
        with mock.patch("helm.ownerasks.snapshot", return_value=({}, True)):
            self.assertTrue(punt.has_open_ask())
        with mock.patch("helm.ownerasks.snapshot", return_value=({}, False)):
            self.assertFalse(punt.has_open_ask())


class TranscriptReadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-punt-")
        self.p = os.path.join(self.tmp, "t.jsonl")

    def _write(self, rows):
        import json
        with open(self.p, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def test_it_reads_the_LAST_assistant_message(self):
        self._write([
            {"message": {"role": "assistant",
                         "content": [{"type": "text", "text": "older"}]}},
            {"message": {"role": "user", "content": "hi"}},
            {"message": {"role": "assistant",
                         "content": [{"type": "text", "text": "newest"}]}},
        ])
        self.assertEqual(punt.last_assistant_text(self.p), "newest")

    def test_a_SIDECHAIN_message_is_never_this_turns_promise(self):
        """A subagent's prose is not the turn's commitment to the owner, and
        blocking a stop on it would punish the wrong author."""
        self._write([
            {"message": {"role": "assistant",
                         "content": [{"type": "text", "text": "mine"}]}},
            {"isSidechain": True,
             "message": {"role": "assistant",
                         "content": [{"type": "text", "text": "subagent"}]}},
        ])
        self.assertEqual(punt.last_assistant_text(self.p), "mine")

    def test_a_missing_transcript_is_empty_never_an_exception(self):
        self.assertEqual(punt.last_assistant_text(
            os.path.join(self.tmp, "nope.jsonl")), "")


if __name__ == "__main__":
    unittest.main()


class PassiveTeeTest(unittest.TestCase):
    """THE PASSIVE TEE — the class the owner caught surviving this very gate.

    The dressed declination says "I did not do X because Y". The passive tee
    declines nothing and excuses nothing: it NAMES future work and sets it
    adrift. So `_DECLINED` can never see it, and before this class the gate
    returned {"findings": []} on the owner's sentence while reporting itself
    healthy — the guard was firing correctly on nothing.

    The discriminator is deferral plus ABSENT ACCOUNTABILITY, which makes this
    the only rung here keyed on a MISSING token. That cuts both ways and both
    directions are pinned below: the same deferral words appear in an honest
    handoff, and a bare schedule remark is not a punt at all.
    """

    OWNER = "Worth a display fix eventually; filed mentally, not urgent."

    def test_the_owners_sentence_trips_the_gate(self):
        hits = punt.findings(self.OWNER)
        self.assertTrue(hits, "the sentence that caused this class must trip it")
        self.assertTrue(all(c == "passive-tee" for c, _ in hits), hits)

    def test_a_tee_WITH_a_tracker_is_a_handoff_not_a_punt(self):
        """The load-bearing negative. These carry the SAME deferral words as
        the owner's sentence — only the accountability differs, so a lexicon
        that fired on the phrases alone would flag every honest handoff."""
        for text in (
            "Worth a display fix eventually - dispatched 5d89cc79 to @kimi.",
            "Worth doing at some point, filed as ledger row 4b518d97.",
            "Should be fixed eventually on lane/punt-passive-tee.",
            "Not urgent, but it needs a pass - tracker is the board row.",
        ):
            with self.subTest(text=text):
                self.assertEqual(punt.findings(text), [], text)

    def test_deferral_words_in_a_plain_FACT_do_not_trip(self):
        """Precision: 'eventually' describing the world is not teed work. An
        intent word is required, which is what separates a schedule remark
        from an abandoned obligation."""
        for text in (
            "The daemon eventually reaps the stale socket on its own.",
            "Old generations eventually stop accepting client-hello.",
            "The buffer scrolls, so the nonce is gone at some point.",
        ):
            with self.subTest(text=text):
                self.assertEqual(punt.findings(text), [], text)

    def test_filed_mentally_alone_is_enough(self):
        """No intent word needed: 'filed' here means noted-and-abandoned."""
        self.assertTrue(punt.findings("Filed mentally, moving on."))

    def test_the_dressed_declination_class_is_UNCHANGED(self):
        """Byte-identical on the original class — the tee only speaks where the
        first-person classes are silent, so their output cannot shift."""
        original = ("Three things I did not do: restart the live chat node "
                    "(would disrupt the fleet mid-work).")
        hits = punt.findings(original)
        self.assertTrue(hits)
        self.assertEqual(hits[0][0], "assumed-disruption",
                         "the tee must not steal a dressed-declination hit")

    def test_the_gate_headline_describes_the_class_it_caught(self):
        """A block whose diagnosis contradicts its own evidence reads as a
        false positive and gets routed around. A tee declined nothing."""
        lines = punt.gate_lines(self.OWNER, open_ask=False)
        self.assertTrue(lines)
        self.assertIn("teed work", lines[0])
        self.assertNotIn("you declined work", lines[0])
        self.assertTrue(any("same sentence" in l for l in lines),
                        "the block must say how to make a tee honest")

    def test_open_ask_exemption_still_applies_to_a_tee(self):
        self.assertEqual(punt.gate_lines(self.OWNER, open_ask=True), [])

    def test_the_off_switch_still_wins(self):
        import os
        os.environ["HELM_STOP_GUARD_PUNT"] = "0"
        try:
            self.assertEqual(punt.gate_lines(self.OWNER, open_ask=False), [])
        finally:
            del os.environ["HELM_STOP_GUARD_PUNT"]


class RefactorPuntTest(unittest.TestCase):
    """The REFACTOR VERDICT with no home.

    WHY (owner, 2026-08-22): "maybe a stophook for if you ever try to punt on a
    potential refactor", read against the standing canon
    refactors-are-preapproved-land-now.

    WHY THE OTHER HALVES CANNOT SEE IT. "The seat-state IA is a failed design"
    declines nothing and schedules nothing, so the dressed-declination half and
    the passive-tee half are both silent while the sentence delivers the most
    expensive finding a turn can produce. This half needs no WHEN, only a
    VERDICT, because a refactor verdict with nowhere to live is the whole punt.

    EVERY NEGATIVE HERE CARRIES ITS OWN POSITIVE, in the same method, on the
    same observable. An `assertEqual(findings(t), [])` suite scores full marks
    against a detector that matches NOTHING AT ALL, so each control asserts
    that its own near-miss — the same sentence with the discharge removed —
    still fires. The pair is the test; either half alone is decoration.

    THE CONTROLS ARE NOT INVENTED. Every sentence in the live-population
    control below was written by a real seat in room #helm on the night this
    shipped. Measured over that population — 654 rows, 5,356 sentences, 24 of
    them carrying refactor vocabulary — the label fired ONCE.
    """

    PUNT = "The seat-state IA is a failed design and the whole thing needs refactoring."

    def fires(self, text):
        return "refactor-punt" in [c for c, _ in punt.findings(text)]

    # ---------------------------------------------------------------- fires
    def test_a_refactor_verdict_with_no_home_trips_the_gate(self):
        hits = punt.findings(self.PUNT)
        self.assertEqual([c for c, _ in hits], ["refactor-punt"], hits)

    def test_the_deferred_refactor_shapes_trip_it(self):
        shapes = (
            "This is a band-aid; we should fold it into a future refactor.",
            "The whole shape is due for a rewrite.",
            "It works, but this needs a redesign.",
            "Stop-gap for now, post-pause refactor.",
        )
        self.assertTrue(self.fires(shapes[0]), shapes[0])   # unconditional
        for text in shapes[1:]:
            with self.subTest(text=text):
                self.assertTrue(self.fires(text), text)

    # ------------------------------------------------------ home = discharge
    def test_a_refactor_WITH_a_home_is_a_filing_not_a_punt(self):
        """The load-bearing negative, each paired with the SAME verdict minus
        its home. Reuses the tee half's _TEE_TRACKER on purpose, so "has a
        home" can never mean two different things in one module."""
        pairs = (
            ("The seat-state IA needs refactoring - filed as task #1300.",
             "The seat-state IA needs refactoring."),
            ("This needs a redesign - I claimed lane/seat-state-ia.",
             "This needs a redesign."),
            ("The whole shape needs a rewrite - dispatched to @ds4pro.",
             "The whole shape needs a rewrite."),
            ("Failed design, tracked on the board row.",
             "Failed design, and I am moving on."),
        )
        self.assertTrue(punt.findings(pairs[0][1]),
                        "positive control dead: %r" % (pairs[0][1],))
        for homed, orphan in pairs:
            with self.subTest(homed=homed):
                self.assertTrue(punt.findings(orphan),
                                "positive control dead: %r" % orphan)
                self.assertEqual(punt.findings(homed), [], homed)

    def test_a_SEMICOLON_does_not_hide_the_home(self):
        """MEASURED FALSE POSITIVE, fixed here. _SENT splits on ';' as well as
        '.', so the verdict and its home land in DIFFERENT fragments and the
        gate refused a correctly-filed refactor over punctuation. The verdict
        is judged per clause; accountability is read across the sentence."""
        orphan = "This needs a redesign; I will get to it."
        homed = "This needs a redesign; I claimed lane/seat-state-ia to do it."
        self.assertTrue(punt.findings(orphan), "positive control dead")
        self.assertEqual(punt.findings(homed), [], homed)

    def test_the_cure_the_block_names_actually_OPENS_the_gate(self):
        """Assert the EFFECT: take the exact refused sentence, apply the cure
        the block prints, and the gate must go silent. A block whose stated
        cure does not clear it is the failure this module is named after."""
        self.assertTrue(punt.gate_lines(self.PUNT, open_ask=False))
        cured = self.PUNT[:-1] + " - filed as task #1300."
        self.assertEqual(punt.gate_lines(cured, open_ask=False), [])

    # ------------------------------------------------------- false-alarm bound
    def test_discussing_a_LANDED_refactor_is_a_report_not_a_punt(self):
        """Past tense is the discriminator, so each control is paired with its
        own present-tense twin — same nouns, opposite verdict."""
        pairs = (
            ("The renderer refactor landed this morning.",
             "The renderer needs refactoring this morning."),
            ("That module was already refactored, so the band-aid is gone.",
             "That module is a band-aid."),
            ("After the redesign the seat list reads from one record.",
             "The seat list needs a redesign."),
        )
        self.assertTrue(punt.findings(pairs[0][1]),
                        "positive control dead: %r" % (pairs[0][1],))
        for report, punt_shape in pairs:
            with self.subTest(report=report):
                self.assertTrue(punt.findings(punt_shape),
                                "positive control dead: %r" % punt_shape)
                self.assertEqual(punt.findings(report), [], report)

    def test_QUOTING_the_canon_is_not_breaking_it(self):
        quotes = (
            "Owner canon refactors-are-preapproved-land-now says do it in-pass.",
            'The rung fires on "the whole thing needs refactoring" with no home.',
            "Refactors are preapproved, so there is nothing to ask for.",
        )
        # the same words, unquoted and uncited, MUST still fire
        self.assertTrue(punt.findings("The whole thing needs refactoring."),
                        "positive control dead")
        for text in quotes:
            with self.subTest(text=text):
                self.assertEqual(punt.findings(text), [], text)

    def test_LIVE_POPULATION_controls_do_not_trip(self):
        """Real sentences from room #helm, 2026-08-22. Each contains refactor
        vocabulary and none is a punt; these are the shapes that would have
        made the rung unusable. The unconditional positive proves the detector
        was awake while they passed."""
        self.assertTrue(punt.findings(self.PUNT), "positive control dead")
        for text in (
            "The functional code needs no fifth algorithmic rewrite.",
            "No identity redesign inside this room lane.",
            "ORCA_TAB_ID is read from environ and the kernel never rewrites it.",
            "Rewrite the section contract to distinguish CLI boundary arms.",
            "An elegant refactor that risks the gesture stack is worth LESS.",
            "Not a rewrite; a scrub of three journal phrases.",
        ):
            with self.subTest(text=text):
                self.assertEqual(punt.findings(text), [], text)

    # ------------------------------------------------------------- discharge
    def test_an_open_owner_ask_does_NOT_discharge_a_refactor(self):
        """The deliberate asymmetry. An ask is the cure for a DECLINATION —
        it puts the thing where the owner reads it. A refactor is preapproved,
        so no ask he happens to have open is its cure, and letting one silence
        this half would kill the rung on exactly the busy nights it is for."""
        self.assertTrue(punt.gate_lines(self.PUNT, open_ask=True))

    def test_an_open_owner_ask_STILL_discharges_a_dressed_declination(self):
        """The other side of that asymmetry — the pre-existing contract for
        every other class is untouched."""
        d = "I did not restart the node because it would disrupt the fleet mid-work."
        self.assertTrue(punt.gate_lines(d, open_ask=False))
        self.assertEqual(punt.gate_lines(d, open_ask=True), [])

    # ----------------------------------------------------------- the block
    def test_the_block_names_the_canon_and_a_cure(self):
        """This arm exists because the first cut SHIPPED WITHOUT IT: making the
        legacy trailer conditional left a refactor-only block that named a
        finding and no cure at all. Caught by reading the rendered block."""
        lines = punt.gate_lines(self.PUNT, open_ask=False)
        blob = "\n".join(lines)
        self.assertIn("refactor verdict", lines[0])
        self.assertIn("refactors-are-preapproved-land-now", blob,
                      "the rung must name the canon it enforces")
        self.assertIn("helm work claim", blob, "and a cure that runs")
        self.assertIn("helm task add", blob)
        self.assertNotIn("helm asks add", blob,
                         "an ask is NOT the cure for a preapproved refactor")

    def test_a_dressed_declination_block_is_UNCHANGED(self):
        d = "I did not restart the node because it would disrupt the fleet mid-work."
        blob = "\n".join(punt.gate_lines(d, open_ask=False))
        self.assertIn("helm asks add", blob)
        self.assertNotIn("refactors-are-preapproved", blob)
        self.assertIn("--needs is required", blob)
        self.assertNotIn("100%% of the time", blob)

    def test_the_off_switch_still_wins(self):
        self.assertTrue(punt.gate_lines(self.PUNT, open_ask=False),
                        "positive control dead: it must block while ON")
        os.environ["HELM_STOP_GUARD_PUNT"] = "0"
        try:
            self.assertEqual(punt.gate_lines(self.PUNT, open_ask=False), [])
        finally:
            del os.environ["HELM_STOP_GUARD_PUNT"]

    # --------------------------------------------------------- no collateral
    def test_the_wider_home_window_did_not_change_the_PARTITION(self):
        """findings() now walks hard-sentences then semicolon clauses. That
        must reproduce _SENT's exact fragments, or the other three halves are
        silently judging different text than they were written against."""
        def split_old(t):
            return [s.strip() for s in punt._SENT.split(
                punt._FENCE.sub(" ", t)) if s.strip()]

        def split_new(t):
            out = []
            for h in [x for x in punt._HARD.split(
                    punt._FENCE.sub(" ", t)) if x.strip()]:
                out += [x.strip() for x in punt._SEMI.split(h) if x.strip()]
            return out

        # unconditional positive: the splitter is alive and really splits
        self.assertEqual(len(split_new("One. Two; three! Four? Five")), 5)
        for text in ("Worth a display fix eventually; filed mentally, not urgent.",
                     "A; B. C? D! E\nF",
                     "One. Two; three! Four? Five"):
            with self.subTest(text=text):
                self.assertEqual(split_old(text), split_new(text))


class RefactorPuntTighteningTest(unittest.TestCase):
    """The three false-negatives a review found reading the regex, and the two
    false-POSITIVES that curing them exposed.

    THE ORDER MATTERS AND IS THE LESSON. Fixing a false-negative is the safe
    direction for a stop-guard — a missed punt costs nothing, a blocked turn
    costs a person. But BOTH cures here un-masked a precision bug that the
    original looseness had been hiding, and shipping those would have traded
    two safe misses for two unsafe fires:
      * stripping the LANDED span leaves the bare noun behind, so "the band-aid
        is gone" — a REMOVAL report — read as a verdict that one exists;
      * adding the GERUND arms made "needs rewriting" matchable, and the quote
        stripper did not cover SINGLE quotes, so every review sentence
        discussing 'needs rewriting' fired on itself.
    Both were caught by re-running the label over the live room rather than by
    the arms, which is why that run is a step and not a formality.

    Every negative below carries an unconditional positive on punt.findings —
    the same call the absence assertion uses, so the vacuous-assertion rung can
    pair them by root.
    """

    def test_the_gerund_arms_fire(self):
        """refactor carried (?:ing)? and its siblings did not, so one spelling
        of the same verdict worked and two did not."""
        for text in ("This needs rewriting.", "This needs redesigning."):
            with self.subTest(text=text):
                self.assertTrue(punt.findings(text), text)
        self.assertTrue(punt.findings("This needs refactoring."),
                        "the arm that always worked must still work")

    def test_a_LANDED_report_does_not_suppress_a_LIVE_verdict(self):
        """The landed test strips its span; the verdict is sought in what is
        left. Same shape as the semicolon cure — a whole-unit test was
        answering a per-span question."""
        mixed = "That module was refactored and still needs a redesign."
        report = "That module was already refactored, so the work is done."
        self.assertTrue(punt.findings(mixed), "positive control dead")
        self.assertEqual(punt.findings(report), [], report)

    def test_the_noun_must_be_PREDICATED_not_merely_present(self):
        """Exposed by the landed cure: stripping the report left 'band-aid'
        standing in a sentence saying the band-aid was REMOVED."""
        self.assertTrue(punt.findings("That module is a band-aid."),
                        "positive control dead")
        for text in ("The band-aid is gone.",
                     "That module was already refactored, so the band-aid is gone."):
            with self.subTest(text=text):
                self.assertEqual(punt.findings(text), [], text)

    def test_SINGLE_quoted_discussion_does_not_fire_on_itself(self):
        """The module's own law, previously enforced for double quotes and
        backticks only. Measured: three of four live-population findings were
        a reviewer's prose quoting the punt shapes in single quotes."""
        self.assertTrue(punt.findings("This needs rewriting."),
                        "positive control dead")
        for text in ("Non-blocking: gerund 'needs rewriting' misses.",
                     "The rung fires on 'the whole thing needs refactoring' here."):
            with self.subTest(text=text):
                self.assertEqual(punt.findings(text), [], text)

    def test_an_APOSTROPHE_is_not_an_opening_quote(self):
        """The reason single quotes were left out originally: a naive '[^']*'
        runs from the apostrophe in it's to the next one and eats the prose
        between. The span must not be flanked by letters."""
        live = "It's a band-aid and it's staying."
        self.assertTrue(punt.findings(live),
                        "an apostrophe swallowed the verdict as if it were a quote")
        self.assertEqual(punt.findings("Don't touch it, that's settled."), [])


class PuntVerbFlagsTest(unittest.TestCase):
    """`helm punt --text` and `--transcript` were DEAD on trunk, and had been.

    cmd_punt handed guard_tail a FILTERED tail — `[a for a in args if
    a.startswith("--")]` — which strips the VALUE off a valued flag, so both
    of this verb's documented flags refused a call that supplied one:
    `helm punt --text "..."` exited 2 with "--text wants a value".

    NOT FOUND BY REVIEW OR BY THE SUITE. It surfaced 2026-08-22 because a NEW
    verb copied this idiom, shipped the identical dead flags, and its author
    ran it from trunk after landing. Every other verb in the tree passes
    `args` whole; this spelling was the outlier.
    """

    def _run(self, args):
        import contextlib
        import io as _io
        out, err = _io.StringIO(), _io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = punt.cmd_punt(args)
        return rc, out.getvalue(), err.getvalue()

    def test_the_text_flag_receives_its_value(self):
        rc, out, err = self._run(["--text", "This needs refactoring."])
        self.assertNotIn("wants a value", out + err,
                         "the VALUE was stripped before the verb saw it")
        self.assertEqual(rc, 1, "a finding exits 1")
        self.assertIn("refactor-punt", out)

    def test_a_clean_text_still_exits_0(self):
        """The paired positive: the flag works in BOTH directions, so a green
        here is not 'the verb always finds something'."""
        rc, out, err = self._run(["--text", "I rebased the lane and gated it."])
        self.assertNotIn("wants a value", out + err)
        self.assertEqual(rc, 0)
        self.assertIn("no dressed declination", out)

    def test_the_transcript_flag_receives_its_value(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl",
                                         delete=False) as fh:
            fh.write("")
            path = fh.name
        self.addCleanup(os.unlink, path)
        rc, out, err = self._run(["--transcript", path])
        # STRUCTURAL, not a scalar pin: rc==0 is "neither proof nor claim" to
        # the vacuous rung, and it is weak evidence anyway — a verb that
        # printed nothing and returned 0 would satisfy it. This is the same
        # rule this commit is about: assert the MESSAGE, not the code.
        self.assertIn("no dressed declination", out,
                      "the verb read the transcript and said so")
        self.assertNotIn("wants a value", out + err,
                         "--transcript lost its value too")
        self.assertEqual(rc, 0)
