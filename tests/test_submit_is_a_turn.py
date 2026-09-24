"""`submit` — the TURN verb at the metaharness seam, and its tri-state.

THE BUG UNDER TEST (owner-reported, plural and recurring: "these keep landing
and not hitting enter"). A seat's own resume directive got typed into its
composer and never submitted, while `send(..., enter=True)` returned success
because the metaharness HAD accepted the bytes. "orca took the bytes" and "the
seat got a turn" are different claims and the gap between them is the defect.

Every pane, handle, seat name and payload below is SYNTHETIC. No live fleet
handle, seat name or transcript appears in this file.
"""
import unittest
from unittest import mock

from helm import composers, harness, resumeturn

# A real Claude composer frame, reproduced from bytes measured off a live pane
# 2026-08-05 and then made synthetic. Two details are load-bearing and neither
# is guessable: the glyph and the composer body are separated by U+00A0 (NOT a
# space), and the model/cwd status row sits BELOW the box.
_BORDER = "─" * 60
_CC2166_BORDER = "─" * 80


def _cc2166_narrow_frame(composer_body):
    """Verbatim 80-column structure measured on Claude Code 2.1.266.

    The draft overwrites the bottom rule after two glyphs; there is no `❯`
    anywhere in the frame. The two complete rules before it distinguish this
    input-box structure from a transcript line that happens to start `──`.
    """
    return "\n".join((" " * 79 + "●", _CC2166_BORDER, _CC2166_BORDER,
                      "──" + composer_body))


def _frame(composer_body="", above="", chrome=True):
    lines = []
    if above:
        lines.extend(above.splitlines())
    lines.append(_BORDER)
    lines.append("❯\xa0" + composer_body if composer_body else "❯")
    lines.append(_BORDER)
    if chrome:
        lines.append("  opus-5 | ~/dev/example/repo")
        lines.append("  ⏵⏵ bypass permissions on · 1 monitor · ← for agents")
    return "\n".join(lines)


class ObserveComposerBase(unittest.TestCase):
    """TEXT, the prompt the composer holds, and
    `assert_the_classifier_still_separates_every_state`, the positive
    control each composer arm calls first.

    THIS CLASS HOLDS NO `test_*` METHOD, and that is its contract. unittest
    collects every inherited `test_*` again under each subclass's own id, so
    an arm written here runs once per subclass. A class that needs this
    fixture subclasses THIS class; its arms go in the subclass."""

    TEXT = "Resuming after compaction. Continue your lane."

    def assert_the_classifier_still_separates_every_state(self):
        """UNCONDITIONAL POSITIVE CONTROL, run by every arm in these classes.

        Each arm asserts ONE mapping, and a classifier hard-wired to that one
        answer would satisfy it. This asserts the whole table on the same
        observable, so a stuck classifier fails every arm instead of passing
        all of them. The vacuous-assertion rung asked for exactly this and was
        right to: a single-mapping arm is a statement about one input, never
        about a classifier.

        IT PINS EVERY MEMBER OF `_STATE_RANK`, NOT A COUNT I TYPED. The first
        version was named "..._all_five" and listed five rows; adding OPAQUE
        left it asserting five of six, and a control that silently stops
        covering a state is worse than no control, because it still reads as
        exhaustive. The last assertion here is that the table's own key set is
        what was exercised, so the NEXT state added fails this arm until it is
        given an observable.
        """
        t = "Resuming after compaction. Continue your lane."
        table = {
            harness.HOLDS: _frame(t),
            harness.REPAINTING: _frame(),
            harness.PLACEHOLDER: _frame("Press up to edit queued messages"),
            harness.OPAQUE: _frame("[Pasted text #1 +12 lines]"),
            harness.FOREIGN: _frame("somebody else entirely"),
            harness.UNREADABLE: "no chrome at all\n",
        }
        self.assertEqual(
            [harness.observe_composer(frame, t) for frame in table.values()],
            list(table))
        self.assertEqual(set(table), set(harness._STATE_RANK),
                         "a state exists that this control never exercises")


class ObserveComposerTest(ObserveComposerBase):
    """task/1909: ONE reading -> ONE state. A review's five residuals as arms.

    Each method below names the residual it encodes, because these are
    ACCEPTANCE CRITERIA agreed in a meld, not a patch list — the incremental
    predicate was ended by residual 4 (a repaint and a deletion are
    byte-identical), so the design has to make that undecidability VISIBLE
    rather than resolve it.

    A subclass of THIS class runs every arm below again under its own id,
    which is right only for one that changes the fixture those arms read. A
    class that only needs the fixture subclasses ObserveComposerBase."""

    def test_exactly_our_text_holds(self):  # noqa: VACUOUS_ASSERTION — a classifier returns ONE value, so this arm has no second channel of the same call for the rung to credit; the control is assert_the_classifier_still_separates_every_state, called unconditionally on the first line, which pins every mapping in _STATE_RANK and fails for a classifier stuck at any one of them
        self.assert_the_classifier_still_separates_every_state()
        self.assertEqual(harness.observe_composer(_frame(self.TEXT), self.TEXT),
                         harness.HOLDS)

    def test_an_empty_composer_is_repainting_not_an_accusation(self):  # noqa: VACUOUS_ASSERTION — a classifier returns ONE value, so this arm has no second channel of the same call for the rung to credit; the control is assert_the_classifier_still_separates_every_state, called unconditionally on the first line, which pins every mapping in _STATE_RANK and fails for a classifier stuck at any one of them
        self.assert_the_classifier_still_separates_every_state()
        # RESIDUAL 4, the one that ended the predicate: an empty composer is a
        # pane still painting OR a human who deleted everything, and one read
        # cannot tell. The state says so instead of guessing.
        self.assertEqual(harness.observe_composer(_frame(), self.TEXT),
                         harness.REPAINTING)

    def test_a_strict_prefix_of_our_text_is_repainting(self):  # noqa: VACUOUS_ASSERTION — a classifier returns ONE value, so this arm has no second channel of the same call for the rung to credit; the control is assert_the_classifier_still_separates_every_state, called unconditionally on the first line, which pins every mapping in _STATE_RANK and fails for a classifier stuck at any one of them
        self.assert_the_classifier_still_separates_every_state()
        self.assertEqual(
            harness.observe_composer(_frame("Resuming after comp"), self.TEXT),
            harness.REPAINTING)
        # POSITIVE CONTROL: the same observable at full length is HOLDS, so a
        # classifier stuck at REPAINTING cannot pass this pair.
        self.assertEqual(harness.observe_composer(_frame(self.TEXT), self.TEXT),
                         harness.HOLDS)

    def test_the_placeholder_is_its_own_state(self):  # noqa: VACUOUS_ASSERTION — a classifier returns ONE value, so this arm has no second channel of the same call for the rung to credit; the control is assert_the_classifier_still_separates_every_state, called unconditionally on the first line, which pins every mapping in _STATE_RANK and fails for a classifier stuck at any one of them
        self.assert_the_classifier_still_separates_every_state()
        # RESIDUAL 2: the post-type placeholder is real text. Reading it as
        # foreign is how an idle pane gets blamed for a human edit.
        self.assertEqual(
            harness.observe_composer(
                _frame("Press up to edit queued messages"), self.TEXT),
            harness.PLACEHOLDER)

    def test_a_strangers_draft_is_foreign(self):  # noqa: VACUOUS_ASSERTION — a classifier returns ONE value, so this arm has no second channel of the same call for the rung to credit; the control is assert_the_classifier_still_separates_every_state, called unconditionally on the first line, which pins every mapping in _STATE_RANK and fails for a classifier stuck at any one of them
        self.assert_the_classifier_still_separates_every_state()
        self.assertEqual(
            harness.observe_composer(_frame("what is going on here"), self.TEXT),
            harness.FOREIGN)

    def test_an_append_to_our_text_is_foreign(self):  # noqa: VACUOUS_ASSERTION — a classifier returns ONE value, so this arm has no second channel of the same call for the rung to credit; the control is assert_the_classifier_still_separates_every_state, called unconditionally on the first line, which pins every mapping in _STATE_RANK and fails for a classifier stuck at any one of them
        self.assert_the_classifier_still_separates_every_state()
        self.assertEqual(
            harness.observe_composer(
                _frame(self.TEXT + " and also please stop"), self.TEXT),
            harness.FOREIGN)

    def test_the_three_chip_kinds_are_three_different_answers(self):  # noqa: VACUOUS_ASSERTION — a classifier returns ONE value, so this arm has no second channel of the same call for the rung to credit; the control is assert_the_classifier_still_separates_every_state, called unconditionally on the first line, which pins every mapping in _STATE_RANK and fails for a classifier stuck at any one of them
        self.assert_the_classifier_still_separates_every_state()
        # ALL THREE KINDS IN ONE ARM, because the previous two versions of this
        # code each got one kind right and another wrong, and an arm per kind
        # never showed that they are one grammar.
        #
        # A BARE CHIP IS OPAQUE, NOT HOLDS — a review's blocker on the round this
        # replaces. I had it returning HOLDS on the reasoning that a chip is our
        # own collapsed payload; the chip renders identically whether it
        # collapsed our text or a human's paste landed during the post-type
        # window, so HOLDS spent the Enter on text nothing had identified.
        self.assertEqual(
            harness.observe_composer(_frame("[Pasted text #1 +12 lines]"),
                                     self.TEXT),
            harness.OPAQUE)
        # A chip with a remainder is an APPEND — the one reading that positively
        # implicates a person.
        self.assertEqual(
            harness.observe_composer(
                _frame("[Pasted text #1 +12 lines] wait, no"), self.TEXT),
            harness.FOREIGN)
        # AN INCOMPLETE CHIP IS A TOKEN STILL BEING DRAWN, a review's additional
        # acceptance branch: no closing bracket means the whole-token pattern
        # misses, and the body fell through to FOREIGN — latching a human-edit
        # accusation on a pane that was only mid-render.
        self.assertEqual(
            harness.observe_composer(_frame("[Pasted text #1 +12 lines"),
                                     self.TEXT),
            harness.REPAINTING)

    def test_an_opaque_chip_never_authorizes_but_a_settled_read_does(self):  # noqa: VACUOUS_ASSERTION — the fold returns ONE value; the control is assert_the_classifier_still_separates_every_state, called unconditionally on the first line
        self.assert_the_classifier_still_separates_every_state()
        # BOTH DIRECTIONS OF OPAQUE'S RANK, because getting it wrong either way
        # is a bug and one assertion cannot see both.
        # It must not be swallowed by a weaker state...
        self.assertEqual(
            harness.reduce_composer_states(
                [harness.REPAINTING, harness.OPAQUE, harness.UNREADABLE]),
            harness.OPAQUE)
        # ...and it must not outrank a composer that later settles to exactly
        # our text, or every send whose pane briefly shows a chip is refused.
        self.assertEqual(
            harness.reduce_composer_states([harness.OPAQUE, harness.HOLDS]),
            harness.HOLDS)
        # FOREIGN still latches over it.
        self.assertEqual(
            harness.reduce_composer_states([harness.FOREIGN, harness.OPAQUE]),
            harness.FOREIGN)

    def test_an_unknown_state_is_refused_not_dropped(self):  # noqa: VACUOUS_ASSERTION — the fold returns ONE value; the control is assert_the_classifier_still_separates_every_state, called unconditionally on the first line
        self.assert_the_classifier_still_separates_every_state()
        # Reject unknown reducer states. Filtering
        # them lets a name the fold does not know vanish and the result be
        # computed from the remaining reads — fail-OPEN on the surface whose
        # whole job is to withhold an Enter.
        with self.assertRaises(ValueError):
            harness.reduce_composer_states([harness.HOLDS, "SETTLED"])
        # NEGATIVE CONTROL: a legitimate sequence must still fold, or the arm
        # above would pass against a reducer that raises on everything.
        self.assertEqual(
            harness.reduce_composer_states([harness.HOLDS]), harness.HOLDS)

    def test_an_unlocatable_composer_is_unreadable_not_empty(self):  # noqa: VACUOUS_ASSERTION — a classifier returns ONE value, so this arm has no second channel of the same call for the rung to credit; the control is assert_the_classifier_still_separates_every_state, called unconditionally on the first line, which pins every mapping in _STATE_RANK and fails for a classifier stuck at any one of them
        self.assert_the_classifier_still_separates_every_state()
        self.assertEqual(harness.observe_composer("no chrome at all\n", self.TEXT),
                         harness.UNREADABLE)


class ReduceComposerStatesTest(ObserveComposerBase):
    """RESIDUAL 1: the latch, as a property of the fold.

    Subclasses ObserveComposerBase for the five-state control, which every
    arm here runs first — the fold is only meaningful if the observations
    feeding it are separable.
    """

    def test_foreign_dominates_a_later_exact_read(self):  # noqa: VACUOUS_ASSERTION — a classifier returns ONE value, so this arm has no second channel of the same call for the rung to credit; the control is assert_the_classifier_still_separates_every_state, called unconditionally on the first line, which pins every mapping in _STATE_RANK and fails for a classifier stuck at any one of them
        self.assert_the_classifier_still_separates_every_state()
        # The exact sequence a review named: foreign seen, then the composer
        # agrees, and the old code spent the Enter anyway.
        self.assertEqual(
            harness.reduce_composer_states(
                [harness.FOREIGN, harness.HOLDS, harness.HOLDS]),
            harness.FOREIGN)

    def test_holds_survives_a_window_of_repaints(self):  # noqa: VACUOUS_ASSERTION — a classifier returns ONE value, so this arm has no second channel of the same call for the rung to credit; the control is assert_the_classifier_still_separates_every_state, called unconditionally on the first line, which pins every mapping in _STATE_RANK and fails for a classifier stuck at any one of them
        self.assert_the_classifier_still_separates_every_state()
        # POSITIVE CONTROL for the latch: it must not swallow the normal case.
        self.assertEqual(
            harness.reduce_composer_states(
                [harness.REPAINTING, harness.REPAINTING, harness.HOLDS]),
            harness.HOLDS)

    def test_an_error_does_not_clear_foreign_evidence(self):  # noqa: VACUOUS_ASSERTION — a classifier returns ONE value, so this arm has no second channel of the same call for the rung to credit; the control is assert_the_classifier_still_separates_every_state, called unconditionally on the first line, which pins every mapping in _STATE_RANK and fails for a classifier stuck at any one of them
        self.assert_the_classifier_still_separates_every_state()
        # RESIDUAL 5, mixed success/error: a blind read is not a quiet pane.
        self.assertEqual(
            harness.reduce_composer_states(
                [harness.FOREIGN, harness.UNREADABLE]),
            harness.FOREIGN)

    def test_an_unreadable_window_outranks_a_repaint(self):  # noqa: VACUOUS_ASSERTION — a classifier returns ONE value, so this arm has no second channel of the same call for the rung to credit; the control is assert_the_classifier_still_separates_every_state, called unconditionally on the first line, which pins every mapping in _STATE_RANK and fails for a classifier stuck at any one of them
        self.assert_the_classifier_still_separates_every_state()
        self.assertEqual(
            harness.reduce_composer_states(
                [harness.REPAINTING, harness.UNREADABLE]),
            harness.UNREADABLE)

    def test_no_observation_at_all_is_unreadable_not_settled(self):  # noqa: VACUOUS_ASSERTION — a classifier returns ONE value, so this arm has no second channel of the same call for the rung to credit; the control is assert_the_classifier_still_separates_every_state, called unconditionally on the first line, which pins every mapping in _STATE_RANK and fails for a classifier stuck at any one of them
        self.assert_the_classifier_still_separates_every_state()
        self.assertEqual(harness.reduce_composer_states([]), harness.UNREADABLE)


class LostEnterThenAppendIsNotDelivered(unittest.TestCase):
    """A lost Enter leaves the collapsed directive in the composer as a chip
    and a human appends to it. The PRE-Enter classifier rightly calls that
    FOREIGN. The POST-Enter reader, verify_submitted, asks a different
    question — is text still SITTING THERE — and its False means DELIVERED.
    A composer_holds that answers False for a chip with an append makes the
    post-check report our turn delivered over a human's edit; True withholds
    it. Presence and ownership are two questions; this arm pins the post-Enter
    one END TO END through verify_submitted on that exact body.
    """

    TEXT = "Resuming after compaction. Continue your lane."
    BODY = "[Pasted text #1 +12 lines] wait, no"

    def test_a_chip_with_an_append_after_enter_withholds_delivered(self):  # noqa: VACUOUS_ASSERTION — the positive control on the same call (an empty composer after Enter IS delivered) runs first, and the refusal half asserts the verifier consumed exactly the chip frame before asserting it withheld DELIVERED
        # THE FRAME GOES IN `pre`, NOT `tails`: verify_submitted is called
        # directly here, so no submit() pre-read consumes the fixture's default
        # clean frame, and the verifier's FIRST read is that default. Scripted
        # as tails[0] the chip is never read: both halves see the same empty
        # composer, so the control passes and the refusal fails for one reason.
        # POSITIVE CONTROL on the same call: an EMPTY composer after Enter IS
        # delivered, so the refusal below is discriminating.
        ad = _FakeAdapter([], pre=_frame())
        with _no_sleep():
            state, _ = ad.verify_submitted("pane-1", self.TEXT, reads=1,
                                           interval=0, dirty=False)
        self.assertEqual(state, harness.DELIVERED)
        ad = _FakeAdapter([], pre=_frame(self.BODY))
        with _no_sleep():
            state, detail = ad.verify_submitted("pane-1", self.TEXT, reads=1,
                                                interval=0, dirty=False)
        self.assertEqual(ad.reads, 1, "the verifier must have read the chip frame")
        self.assertNotEqual(state, harness.DELIVERED, detail)
        # and the two questions stay separate on the same body
        self.assertIs(harness.composer_holds(_frame(self.BODY), self.TEXT), True)
        self.assertEqual(harness.observe_composer(_frame(self.BODY), self.TEXT),
                         harness.FOREIGN)


class ComposerHoldsTest(unittest.TestCase):
    """The discriminator: is OUR text still sitting in the composer?"""

    def test_held_text_matching_our_payload_is_held(self):
        tail = _frame("Resuming after compaction. Continue your lane.")
        self.assertIs(harness.composer_holds(
            tail, "Resuming after compaction. Continue your lane."), True)

    def test_empty_composer_is_not_held(self):
        text = "anything at all"
        self.assertIs(harness.composer_holds(_frame(), text), False)
        # POSITIVE CONTROL on the same observable: a detector stuck at False
        # would pass the line above and report every stranded pane as fine.
        self.assertIs(harness.composer_holds(_frame(text), text), True)

    def test_a_placeholder_is_not_our_text_and_so_not_held(self):
        # Measured live: after a successful submit the composer can render
        # "Press up to edit queued messages". Non-empty, but not OUR payload —
        # comparing against emptiness would have called this a failed send.
        text = "Resuming after compaction."
        tail = _frame("Press up to edit queued messages")
        self.assertIs(harness.composer_holds(tail, text), False)
        self.assertIs(harness.composer_holds(_frame(text), text), True)

    def test_a_different_agents_draft_is_not_our_text(self):
        text = "Resuming after compaction."
        tail = _frame("half a sentence somebody was still typing")
        self.assertIs(harness.composer_holds(tail, text), False)
        self.assertIs(harness.composer_holds(_frame(text), text), True)

    def test_width_truncated_composer_still_matches_by_prefix(self):
        # Claude wraps rather than truncates, so the visible row is a PREFIX of
        # the first logical line. Requiring equality would miss every long
        # directive — which is every real one.
        full = "Resuming after compaction. Continue your assigned lane " \
               "and do not wait for a human before proceeding."
        self.assertIs(harness.composer_holds(
            _frame("Resuming after compaction. Continue your assign"), full),
            True)

    def test_multiline_payload_is_compared_on_its_first_line(self):
        payload = "Resuming after compaction.\nDo not wait for a human."
        self.assertIs(harness.composer_holds(
            _frame("Resuming after compaction."), payload), True)

    def test_collapsed_paste_chip_counts_as_held(self):
        # A big multi-line paste renders as a CHIP, not as the text. Reading
        # "not our text, therefore it submitted" would mint a false DELIVERED
        # on exactly the largest directives.
        payload = "\n".join("line %d" % i for i in range(40))
        self.assertIs(harness.composer_holds(
            _frame("[Pasted text #1 +40 lines]"), payload), True)

    def test_unreadable_pane_is_None_never_False(self):
        # A blind pane's empty tail must not read as "the composer is empty,
        # therefore it went in". None is what becomes UNKNOWN upstream.
        self.assertIsNone(harness.composer_holds("", "Resuming."))
        # …and the SAME call on a readable pane answers a real boolean, so the
        # None above is a judgement about the pane, not a dead function.
        self.assertIs(harness.composer_holds(_frame(), "Resuming."), False)
        self.assertIs(harness.composer_holds(_frame("Resuming."), "Resuming."),
                      True)

    def test_prompt_buried_under_newer_output_is_None(self):
        # A `❯` preserved in SCROLLBACK is history, not the live composer.
        buried = _frame("Resuming.") + "\n● the agent said something newer"
        self.assertIsNone(harness.composer_holds(buried, "Resuming."))
        # The identical frame WITHOUT the newer line answers True — so the None
        # is caused by the buried prompt and nothing else.
        self.assertIs(harness.composer_holds(_frame("Resuming."), "Resuming."),
                      True)

    def test_model_cwd_status_row_does_not_hide_the_composer(self):
        # The regression that blinded this on 7 of 14 readable panes: the
        # `opus-5 | ~/path` row below the box read as "newer semantic content"
        # and the composer became unresolvable — so BOTH answers went UNKNOWN,
        # not just one. Pin both.
        self.assertIs(harness.composer_holds(_frame(chrome=True), "Resuming."),
                      False)
        self.assertIs(harness.composer_holds(_frame("Resuming.", chrome=True),
                                             "Resuming."), True)

    def test_cc2166_narrow_rule_draft_is_a_nonempty_composer(self):  # noqa: VACUOUS_ASSERTION — exact canonical line plus held/exact True positively prove the no-prompt fixture is parsed
        text = "Run `helm seat boot-brief` and follow it."
        tail = _cc2166_narrow_frame(text)
        self.assertNotIn("❯", tail, "fixture grew the old prompt grammar")
        self.assertEqual(harness._prompt_line(tail), "❯\xa0" + text)
        self.assertIs(harness.composer_holds(tail, text), True,
                      "the measured non-empty draft was laundered into clear")
        self.assertIs(harness.composer_exactly_holds(tail, text), True,
                      "the exact typed wire never earns its guarded Enter")

    def test_rule_prefixed_transcript_prose_is_not_a_composer(self):  # noqa: VACUOUS_ASSERTION — the sibling exact-frame arm positively matches the same parser; these three one-structure-missing mutants must stay None
        text = "Run `helm seat boot-brief` and follow it."
        for tail in ("──" + text,
                     _CC2166_BORDER + "\n──" + text,
                     _cc2166_narrow_frame(text) + "\n● semantic output",
                     _cc2166_narrow_frame(text) +
                     "\n  ⏵⏵ bypass permissions on",
                     _cc2166_narrow_frame(text) + "\n" + _CC2166_BORDER):
            with self.subTest(tail=tail):
                self.assertIsNone(harness._prompt_line(tail))
                self.assertIsNone(harness.composer_holds(tail, text))


class _FakeAdapter(harness._CLIAdapter):
    """A metaharness that accepts every byte — the exact liar under test.

    `sent` records (text, enter) so the SPLIT is provable, and `tails` is the
    scripted sequence of pane reads the verifier will see.
    """
    name = "fake"
    bin = "fake"

    def __init__(self, tails, refuse_text=False, refuse_enter=False,
                 read_raises=False, pre=None, before_enter=None):
        super().__init__("/fake/bin/fake")
        self.sent = []
        # `submit` PRE-READS the composer before typing (see its docstring: a
        # composer that was already dirty raises the bar to EMPTY). Fixtures
        # get a clean pre-read by default so each test scripts only the reads
        # it is actually about.
        self.tails = [_frame() if pre is None else pre] + list(tails)
        self.refuse_text = refuse_text
        self.refuse_enter = refuse_enter
        self.read_raises = read_raises
        self.reads = 0
        self.timeouts = []
        self.typed = None
        self.enter_attempted = False
        self.pre_enter_read = False
        self.before_enter = before_enter

    def send(self, handle, text, enter=True):
        if enter:
            self.enter_attempted = True
            if self.refuse_enter:
                raise harness.HarnessError("fake: enter refused")
        elif self.refuse_text:
            raise harness.HarnessError("fake: text refused")
        else:
            self.typed = text
        self.sent.append((text, enter))

    def read(self, handle, limit=3000, timeout=60):
        self.reads += 1
        self.timeouts.append(timeout)
        if self.read_raises:
            raise harness.HarnessError("fake: pane unreadable")
        if self.typed and not self.enter_attempted and not self.pre_enter_read:
            self.pre_enter_read = True
            value = (self.before_enter if self.before_enter is not None
                     else _frame(self.typed))
            # An Exception here RAISES, exactly as one scripted in `tails` does.
            # Without this the fixture could not express "the first post-type
            # read FAILED", which is why the all-reads-failed path went armless
            # long enough for a refusal to name a human for an unreadable pane.
            if isinstance(value, Exception):
                raise value
            return value
        value = self.tails.pop(0) if self.tails else ""
        if isinstance(value, Exception):
            raise value
        return value


def _no_sleep():
    return mock.patch.object(harness.time, "sleep", lambda *_a, **_k: None)


class SubmitTriStateTest(unittest.TestCase):
    TEXT = "Resuming after compaction. Continue your lane."

    def test_the_submit_is_split_into_two_acts_text_then_bare_enter(self):
        ad = _FakeAdapter([_frame()])
        with _no_sleep():
            ad.submit("pane-1", self.TEXT)
        # NECESSARY BUT NOT SUFFICIENT on its own — the behaviour tests below
        # are what prove the verb. This one pins the SHAPE: the text carries no
        # Enter, and the Enter is its own act with an empty payload.
        self.assertEqual(ad.sent, [(self.TEXT, False), ("", True)])

    def test_human_edit_between_type_and_Enter_is_refused(self):
        ad = _FakeAdapter(
            [], before_enter=_frame(self.TEXT + " plus my unfinished draft"))
        with _no_sleep():
            state, detail = ad.submit("pane-1", self.TEXT)
        self.assertEqual(state, harness.UNKNOWN)
        self.assertIn("refusing Enter", detail)
        self.assertEqual(ad.sent, [(self.TEXT, False)],
                         "a changed composer must spend zero Enters")

    def test_a_composer_that_settles_late_still_earns_its_Enter(self):
        """THE task/1900 ARM: the post-type check polls, it does not snap.

        `settle` is a fixed 0.4s floor, and a long payload can still be
        repainting when it expires. The check used to read ONCE at that instant
        and refuse, so a slow render was indistinguishable from a human edit —
        measured 4/4 on `helm seat spawn`, and a seat that never receives its
        Enter never arms its beacon, so nothing can reach it again.

        Scripted here as: first post-type read shows no composer at all, second
        shows our text intact. Against the one-shot check this is UNKNOWN and
        zero Enters; the bounded poll must reach DELIVERED.
        """
        ad = _FakeAdapter([_frame(self.TEXT), _frame()],
                          before_enter="repainting pane")
        with _no_sleep():
            state, detail = ad.submit("pane-1", self.TEXT)
        self.assertEqual(state, harness.DELIVERED, detail)
        self.assertEqual(ad.sent, [(self.TEXT, False), ("", True)],
                         "a late repaint must still spend exactly one Enter")

    def test_an_unsettled_composer_is_never_blamed_on_a_human(self):
        """Refusal stays total; the ACCUSATION has to be earned.

        No read ever identifies a composer, so nothing was observed that could
        be a human edit. The keystroke is still withheld — that half is
        unchanged — but naming an absent person for our own unreadable pane is
        what made this outage take eleven days to read.
        """
        ad = _FakeAdapter([], before_enter="repainting pane")
        with _no_sleep():
            state, detail = ad.submit("pane-1", self.TEXT)
        self.assertEqual(state, harness.UNKNOWN)
        self.assertEqual(ad.sent, [(self.TEXT, False)],
                         "an unreadable composer must still spend zero Enters")
        self.assertIn("NOT because anyone edited it", detail)
        self.assertNotIn("a human may have edited", detail)

    def test_one_differing_read_still_accuses_even_if_later_reads_go_blind(self):
        """Polling must not WEAKEN the old check.

        A composer positively seen holding someone else's text is the strongest
        thing this poll ever learns, so it latches: a later unreadable read
        cannot downgrade real evidence of an edit into "could not read".
        """
        ad = _FakeAdapter(
            ["repainting pane", "repainting pane"],
            before_enter=_frame(self.TEXT + " plus my unfinished draft"))
        with _no_sleep():
            state, detail = ad.submit("pane-1", self.TEXT)
        self.assertEqual(state, harness.UNKNOWN)
        self.assertIn("a human may have edited", detail)
        self.assertEqual(ad.sent, [(self.TEXT, False)])

    def test_an_EMPTY_composer_repaint_is_not_a_human_edit(self):
        """The finding, arm one. An empty composer is not a person.

        `composer_exactly_holds` answers False for an EMPTY composer exactly as
        it does for a stranger's draft, so round one's latch fired on a pane
        that had simply not painted our text yet and told the operator a human
        may have edited it — re-creating the accusation the whole cure exists to
        delete. Reproduced by direct probe on the landed tree before this arm
        existed.
        """
        ad = _FakeAdapter(["repainting pane"], before_enter=_frame())
        with _no_sleep():
            state, detail = ad.submit("pane-1", self.TEXT)
        self.assertEqual(state, harness.UNKNOWN)
        self.assertEqual(ad.sent, [(self.TEXT, False)])
        self.assertNotIn("a human may have edited", detail)

    def test_a_PREFIX_repaint_is_not_a_human_edit(self):
        """The finding, arm two — our OWN text, caught mid-render.

        The composer shows a leading slice of the very payload we just typed.
        That is the single most likely thing to see when a long brief is still
        painting, and it is the reading that stranded the seats: False from the
        exact predicate, latched as a foreign draft. FOREIGN must mean POSITIVE
        foreign evidence, so a body that is a prefix of our own text can never
        earn the accusation.
        """
        ad = _FakeAdapter(["repainting pane"],
                          before_enter=_frame(self.TEXT[:18]))
        with _no_sleep():
            state, detail = ad.submit("pane-1", self.TEXT)
        self.assertEqual(state, harness.UNKNOWN)
        self.assertEqual(ad.sent, [(self.TEXT, False)])
        self.assertNotIn("a human may have edited", detail)

    def test_an_APPENDED_draft_is_still_a_human_edit(self):
        """The other side of the same predicate — do not cure by going blind.

        `composer_holds` calls want-is-a-prefix-of-body HELD, so a human who
        appends to Helm's text looks "held" to the lenient predicate. Positive
        foreign evidence must still catch it, or this fix trades a false
        accusation for a missed one.
        """
        ad = _FakeAdapter(["repainting pane"],
                          before_enter=_frame(self.TEXT + " and my own words"))
        with _no_sleep():
            state, detail = ad.submit("pane-1", self.TEXT)
        self.assertEqual(state, harness.UNKNOWN)
        self.assertEqual(ad.sent, [(self.TEXT, False)])
        self.assertIn("a human may have edited", detail)

    def test_every_post_type_read_failing_reports_unreadable_not_an_edit(self):
        """The all-error path a review asked for: no read ever succeeded.

        Nothing was observed, so nothing can be attributed. The refusal must say
        the pane could not be read and must carry the underlying error rather
        than naming a person.
        """
        boom = harness.HarnessError("fake: pane unreadable")
        ad = _FakeAdapter([boom] * (harness.SUBMIT_VERIFY_READS - 1),
                          before_enter=boom)
        with _no_sleep():
            state, detail = ad.submit("pane-1", self.TEXT)
        self.assertEqual(state, harness.UNKNOWN)
        self.assertEqual(ad.sent, [(self.TEXT, False)])
        self.assertIn("unreadable", detail)
        self.assertNotIn("a human may have edited", detail)

    def test_late_post_type_repaint_spends_exactly_the_expected_reads(self):
        """The read-count pin, carried from 3e773997d.

        My own arms would pass a one-read implementation that got lucky on
        fixture ordering; this one would not.
        """
        ad = _FakeAdapter([_frame(self.TEXT), _frame()],
                          before_enter="repainting pane")
        with _no_sleep():
            state, detail = ad.submit("pane-1", self.TEXT)
        self.assertEqual(state, harness.DELIVERED, detail)
        self.assertEqual(ad.reads, 4,
                         "clean pre-read, blind repaint, exact retry, verify")

    def test_a_refusal_consumes_the_whole_bounded_window(self):
        """The second read-count pin, carried from 3e773997d.

        A refusal must not short-circuit the poll: the window is the thing that
        gives a late composer its chance.
        """
        changed = _frame(self.TEXT + " plus my unfinished draft")
        ad = _FakeAdapter([changed] * (harness.SUBMIT_VERIFY_READS - 1),
                          before_enter=changed)
        with _no_sleep():
            state, detail = ad.submit("pane-1", self.TEXT)
        self.assertEqual(state, harness.UNKNOWN)
        # POSITIVE CONTROL on the same observables, restored after the rung
        # caught me dropping it when I carried this arm over: the text WAS
        # typed and exactly zero Enters were spent, and the refusal names the
        # edit. Without these the read-count could be satisfied by a submit
        # that never got as far as the composer at all.
        self.assertEqual(ad.sent, [(self.TEXT, False)],
                         "a foreign composer must spend zero Enters")
        self.assertIn("a human may have edited", detail)
        self.assertEqual(ad.reads, harness.SUBMIT_VERIFY_READS + 1,
                         "the refusal must consume the whole bounded window")

    def test_wrapped_prefix_never_earns_the_initial_enter(self):
        long_text = self.TEXT + " with enough continuation to wrap in a pane"
        ad = _FakeAdapter([], before_enter=_frame(long_text[:24]))
        with _no_sleep():
            state, _ = ad.submit("pane-1", long_text)
        self.assertEqual(state, harness.UNKNOWN)
        self.assertEqual(ad.sent, [(long_text, False)])

    def test_known_placeholder_is_clean_for_initial_submission(self):
        ad = _FakeAdapter(
            [_frame()], pre=_frame("Press up to edit queued messages"))
        with _no_sleep():
            state, detail = ad.submit("pane-1", self.TEXT)
        self.assertEqual(state, harness.DELIVERED, detail)
        self.assertEqual(ad.sent, [(self.TEXT, False), ("", True)])

    def test_transient_pre_read_failure_then_clean_composer_submits(self):
        err = harness.HarnessError("fake: repaint read failed")
        ad = _FakeAdapter([_frame(), _frame()], pre=err)
        with _no_sleep():
            state, detail = ad.submit("pane-1", self.TEXT)
        self.assertEqual(state, harness.DELIVERED, detail)
        self.assertEqual(ad.sent, [(self.TEXT, False), ("", True)])

    def test_transient_no_composer_then_clean_composer_submits(self):
        ad = _FakeAdapter([_frame(), _frame()], pre="repainting pane")
        with _no_sleep():
            state, detail = ad.submit("pane-1", self.TEXT)
        self.assertEqual(state, harness.DELIVERED, detail)
        self.assertEqual(ad.sent, [(self.TEXT, False), ("", True)])

    def test_pane_that_advanced_is_DELIVERED(self):
        ad = _FakeAdapter([_frame()])
        with _no_sleep():
            state, detail = ad.submit("pane-1", self.TEXT)
        self.assertEqual(state, harness.DELIVERED)
        self.assertIn("advanced", detail)

    def test_pane_still_holding_our_text_is_NOT_DELIVERED(self):
        # THE OWNER'S PANE. Every byte was accepted; the seat got no turn.
        ad = _FakeAdapter([_frame(self.TEXT)] *
                          harness.SUBMIT_VERIFY_READS)
        with _no_sleep():
            state, detail = ad.submit("pane-1", self.TEXT)
        self.assertEqual(state, harness.NOT_DELIVERED)
        self.assertIn("typed, never submitted", detail)
        self.assertEqual(ad.sent, [(self.TEXT, False), ("", True)],
                         "observation one must not bypass persistence")

    def test_a_stuck_first_Enter_retries_once_and_delivers(self):
        """The retained trunk symbol proves one snapshot cannot trigger retry."""
        ad = _FakeAdapter([_frame(self.TEXT)] * harness.SUBMIT_VERIFY_READS)
        with _no_sleep():
            state, detail = ad.submit("pane-1", self.TEXT)
        self.assertEqual(state, harness.NOT_DELIVERED)
        self.assertNotIn("retry", detail)
        self.assertEqual(ad.sent, [(self.TEXT, False), ("", True)])

    def test_unreadable_pane_is_UNKNOWN_and_never_DELIVERED(self):
        ad = _FakeAdapter([], read_raises=True)
        with _no_sleep():
            state, detail = ad.submit("pane-1", self.TEXT)
        self.assertEqual(state, harness.UNKNOWN)
        self.assertNotEqual(state, harness.DELIVERED)
        self.assertIn("read failed during composer pre-read", detail)
        self.assertIn("fake: pane unreadable", detail)
        self.assertEqual(ad.reads, harness.SUBMIT_VERIFY_READS)
        self.assertEqual(ad.timeouts,
                         [harness.SUBMIT_READ_TIMEOUT_S] * ad.reads)
        self.assertEqual(ad.sent, [])

    def test_blind_pane_empty_tail_is_UNKNOWN_not_DELIVERED(self):  # noqa: VACUOUS_ASSERTION — the answering adapter below positively controls delivery on the same observable
        # The 12-of-26 case: connected, writable, not orphaned, and every read
        # comes back empty. An empty tail is NOT an empty composer.
        ad = _FakeAdapter([""] * (harness.SUBMIT_VERIFY_READS - 1), pre="")
        with _no_sleep():
            state, detail = ad.submit("pane-1", self.TEXT)
        self.assertEqual(state, harness.UNKNOWN)
        self.assertIn("no live composer located", detail)
        self.assertIn("classified pane state: UNKNOWN", detail)
        self.assertEqual(ad.reads, harness.SUBMIT_VERIFY_READS)
        self.assertEqual(ad.sent, [])
        # POSITIVE CONTROL: the identical adapter whose pane DOES answer
        # returns DELIVERED, so UNKNOWN above is about the blind tail and not
        # a submit that can never succeed.
        ok = _FakeAdapter([_frame()])
        with _no_sleep():
            self.assertEqual(ok.submit("pane-1", self.TEXT)[0],
                             harness.DELIVERED)

    #: A pane that has emitted nothing since it was attached: a session recap
    #: and no composer line. Byte-identical on every read BY CONSTRUCTION,
    #: which is the whole condition under test.
    STALE = ("Session recap\n"
             "  · resumed from 755d60e2\n"
             "  · 289 messages restored")

    def test_identical_reads_are_named_ONE_observation_not_N(self):
        """A capture that could not refresh is not N independent looks.

        task/1977, measured on a freshly resumed pane: the screen read
        returned the same lines with no composer TWICE, minutes apart, and
        the composer appeared only once a real turn produced output. The box
        was there the whole time. The refusal is still a refusal — nothing
        here authorises a keystroke — but it must not report one frozen frame
        as corroboration.
        """
        ad = _FakeAdapter([self.STALE] * (harness.SUBMIT_VERIFY_READS - 1),
                          pre=self.STALE)
        with _no_sleep():
            state, detail = ad.submit("pane-1", self.TEXT)
        self.assertEqual(state, harness.UNKNOWN)
        self.assertIn("no live composer located", detail)
        self.assertIn("ONE observation repeated", detail)
        self.assertIn("%d reads" % harness.SUBMIT_VERIFY_READS, detail)
        self.assertIn("emitted nothing since it was attached", detail)
        # THE REFUSAL IS UNCHANGED: naming the staleness must not become
        # permission to type into a pane whose composer was never seen.
        self.assertEqual(ad.sent, [])

    def test_a_classification_from_a_frame_the_pane_STOPPED_confirming_says_so(self):
        """THE REFUSAL NAMED A STATE THE PANE HAD STOPPED CONFIRMING.

        `last_tail` survives later failed reads, so a pane that was readable
        and then went dark still produced a confident "classified pane state:
        X" — with nothing to tell an operator that the classification belongs
        to an earlier frame and that the pane has since refused to be read.
        The state is still the best evidence available and is kept; what was
        missing was that it could not be told apart from a reading of the pane
        as it now stands.
        """
        ad = _FakeAdapter([harness.HarnessError("fake: pane went dark")],
                          pre=self.STALE)
        with _no_sleep():
            state, detail = ad.submit("pane-1", self.TEXT)
        self.assertEqual(state, harness.UNKNOWN)
        self.assertIn("no live composer located", detail)
        self.assertIn("LATER READ(S) FAILED", detail)
        self.assertIn("pane went dark", detail)
        self.assertEqual(ad.sent, [], "typed into a pane that went dark")
        # MUST-HIT: the same refusal with every read SUCCEEDING carries no
        # such clause, so this arm measures the failed read and not a string
        # the refusal always prints.
        clean = _FakeAdapter([self.STALE + "\n  · one"], pre=self.STALE)
        with _no_sleep():
            _state, clean_detail = clean.submit("pane-1", self.TEXT)
        self.assertIn("no live composer located", clean_detail)
        self.assertNotIn("LATER READ(S) FAILED", clean_detail)

    def test_reads_that_DID_differ_make_no_staleness_claim(self):
        """The counterfactual, and it is what makes the clause a measurement.

        Same refusal, same absent composer, same read count — only the BYTES
        differ between reads. A pane that really is repainting and really has
        no composer must still be reported as exactly that, or the clause
        would be decoration attached to every negative.
        """
        ad = _FakeAdapter([self.STALE + "\n  · one", self.STALE + "\n  · two"],
                          pre=self.STALE)
        with _no_sleep():
            state, detail = ad.submit("pane-1", self.TEXT)
        self.assertEqual(state, harness.UNKNOWN)
        self.assertIn("no live composer located", detail)
        self.assertNotIn("ONE observation repeated", detail)
        self.assertEqual(ad.reads, harness.SUBMIT_VERIFY_READS)
        self.assertEqual(ad.sent, [])

    def test_the_POST_TYPE_window_names_a_frozen_capture_too(self):
        """The same defect lives in the verification window, so the cure does.

        After typing, the window reads the pane N times looking for Helm's own
        text. A pane frozen since the attach hands back one frame N times
        there as well, and the refusal counted them as N read(s) exactly as
        the pre-read did. THE NOTE GOES ON THE NEGATIVE ONLY: a frozen frame
        that SHOWS foreign text is still a real sighting of foreign text — a
        positive needs one look — while 'never showed' is a claim about
        everything the reads did not contain.
        """
        ad = _FakeAdapter([self.STALE] * (harness.SUBMIT_VERIFY_READS - 1),
                          before_enter=self.STALE)
        with _no_sleep():
            state, detail = ad.submit("pane-1", self.TEXT)
        self.assertEqual(state, harness.UNKNOWN)
        self.assertIn("never showed an identifiable composer", detail)
        self.assertIn("ONE observation repeated", detail)
        # THE TEXT WAS TYPED AND ENTER WAS NOT SPENT: this window sits after
        # the keystrokes it is deciding about, so the refusal it carries is
        # about Enter and the arm must show which half ran.
        self.assertEqual(ad.typed, self.TEXT)
        self.assertFalse(ad.enter_attempted)

    def test_a_SINGLE_read_claims_nothing_about_independence(self):
        """One read is not two, so it cannot be a repeat of anything."""
        self.assertEqual(harness._frozen_capture_note([self.STALE]), "")
        self.assertEqual(harness._frozen_capture_note([]), "")
        # AND THE POSITIVE on the same instrument, so the emptiness above is
        # about the count and not about a helper that never speaks.
        self.assertIn("ONE observation repeated",
                      harness._frozen_capture_note([self.STALE] * 2))

    def test_a_late_repaint_still_counts_as_DELIVERED(self):
        # Held on the first read, clear on the second: the bounded repaint
        # window exists so a slow frame is not called a failure.
        ad = _FakeAdapter([_frame(self.TEXT), _frame()])
        with _no_sleep():
            state, _ = ad.submit("pane-1", self.TEXT)
        self.assertEqual(state, harness.DELIVERED)
        self.assertEqual(ad.reads, 4,
                         "clean pre-read, exact pre-Enter read, two verifies")

    def test_refused_text_is_NOT_DELIVERED_and_no_enter_is_fired(self):
        ad = _FakeAdapter([_frame()], refuse_text=True)
        with _no_sleep():
            state, detail = ad.submit("pane-1", self.TEXT)
        self.assertEqual(state, harness.NOT_DELIVERED)
        self.assertIn("refused the text", detail)
        # A bare Enter into a pane we never typed into would submit whatever
        # a human left half-typed there.
        self.assertEqual(ad.sent, [])

    def test_a_BLOCKED_submit_reports_the_truth_from_the_pane(self):
        # THE ACCEPTANCE ARM: break the submit leg specifically. The text lands
        # in the composer, the Enter never fires, and the verdict comes from
        # READING THE PANE — not from the fact that an exception was raised.
        ad = _FakeAdapter([_frame(self.TEXT)] *
                          harness.SUBMIT_VERIFY_READS,
                          refuse_enter=True)
        with _no_sleep():
            state, detail = ad.submit("pane-1", self.TEXT)
        self.assertEqual(state, harness.NOT_DELIVERED)
        self.assertIn("Enter call itself errored", detail)

    def test_blocked_submit_on_an_UNREADABLE_pane_is_UNKNOWN(self):
        # Same broken submit, but now nothing can be read back. helm must say
        # it does not know — the failure it can see is not proof of the outcome
        # it cannot.
        ad = _FakeAdapter([], refuse_enter=True, read_raises=True)
        with _no_sleep():
            state, detail = ad.submit("pane-1", self.TEXT)
        self.assertEqual(state, harness.UNKNOWN)
        self.assertIn("read failed during composer pre-read", detail)
        self.assertIn("fake: pane unreadable", detail)
        self.assertEqual(ad.sent, [])

    def test_a_composer_dirty_BEFORE_the_send_cannot_answer_DELIVERED_weakly(self):
        # FOUND ON A LIVE PANE, not reasoned about. Verification after Enter is
        # too late: the human draft would already have been submitted. The
        # pre-read therefore refuses before typing, not merely before claiming
        # DELIVERED.
        dirty = _frame("half a draft somebody was typing")
        ad = _FakeAdapter([dirty] * 3, pre=dirty)
        with _no_sleep():
            state, detail = ad.submit("pane-1", self.TEXT)
        self.assertEqual(state, harness.UNKNOWN)
        self.assertIn("composer holds", detail)
        self.assertIn("half a draft somebody was typing", detail)
        self.assertEqual(ad.sent, [],
                         "a human draft must be refused before any keystroke")

    def test_preexisting_matching_text_never_earns_an_Enter_retry(self):  # noqa: VACUOUS_ASSERTION — matching held frame and UNKNOWN positively control zero sends/provenance
        held = _frame(self.TEXT)
        ad = _FakeAdapter([held] * harness.SUBMIT_VERIFY_READS, pre=held)
        owned = []
        with _no_sleep():
            state, _ = ad.submit(
                "pane-1", self.TEXT,
                on_typed=lambda handle, text: owned.append((handle, text)))
        self.assertEqual(state, harness.UNKNOWN)
        self.assertEqual(ad.sent, [],
                         "pre-existing matching text is still a human draft")
        self.assertEqual(owned, [],
                         "pre-existing text must not acquire Helm provenance")

    def test_a_dirty_composer_that_goes_EMPTY_is_DELIVERED(self):  # noqa: VACUOUS_ASSERTION — retained trunk symbol; dirty pre-frame positively controls the zero-send refusal
        """The retained trunk symbol now guards refusal of the unsafe behavior."""
        # Safety is decided BEFORE typing. A later empty frame cannot retroactively
        # make it acceptable to have submitted a human draft.
        ad = _FakeAdapter([_frame()],
                          pre=_frame("half a draft somebody was typing"))
        with _no_sleep():
            state, _ = ad.submit("pane-1", self.TEXT)
        self.assertEqual(state, harness.UNKNOWN)
        self.assertEqual(ad.sent, [])

    def test_an_unreadable_PRE_read_also_raises_the_bar(self):  # noqa: VACUOUS_ASSERTION — explicit blind pre-read positively controls the zero-send UNKNOWN
        # helm could not see the composer before typing, so it cannot later
        # credit anything it typed for the composer looking clean.
        ad = _FakeAdapter([_frame("someone elses text")] * 3, pre="")
        with _no_sleep():
            state, _ = ad.submit("pane-1", self.TEXT)
        self.assertEqual(state, harness.UNKNOWN)
        self.assertEqual(ad.sent, [])

    def test_later_recovery_sends_only_bare_Enter_for_an_exact_match(self):
        ad = _FakeAdapter([_frame()], pre=_frame(self.TEXT))
        with _no_sleep():
            state, detail = ad.retry_held_submission(
                "pane-1", self.TEXT, attempts=2, backoff=0)
        self.assertEqual(state, harness.DELIVERED, detail)
        self.assertEqual(ad.sent, [("", True)],
                         "recovery must never retype the directive")

    def test_human_draft_never_earns_a_recovery_keystroke(self):
        draft = self.TEXT + " plus a human addition"
        ad = _FakeAdapter([], pre=_frame(draft))
        with _no_sleep():
            state, detail = ad.retry_held_submission(
                "pane-1", self.TEXT, attempts=2, backoff=0)
        self.assertEqual(state, harness.UNKNOWN)
        self.assertIn("does not exactly equal", detail)
        self.assertEqual(ad.sent, [], "foreign text must spend zero Enters")

    def test_wrapped_prefix_is_unknown_for_recovery_not_owned(self):  # noqa: VACUOUS_ASSERTION — readable wrapped prefix positively controls zero recovery keystrokes
        ad = _FakeAdapter([], pre=_frame(self.TEXT[:20]))
        with _no_sleep():
            state, _ = ad.retry_held_submission(
                "pane-1", self.TEXT, attempts=1, backoff=0)
        self.assertEqual(state, harness.UNKNOWN)
        self.assertEqual(ad.sent, [])

    def test_unreadable_recovery_spends_zero_keystrokes(self):  # noqa: VACUOUS_ASSERTION — explicit read failure positively controls the zero-keystroke UNKNOWN
        ad = _FakeAdapter([], read_raises=True)
        with _no_sleep():
            state, _ = ad.retry_held_submission(
                "pane-1", self.TEXT, attempts=1, backoff=0)
        self.assertEqual(state, harness.UNKNOWN)
        self.assertEqual(ad.sent, [])

    def test_both_shipped_adapters_inherit_the_same_submit(self):
        # "In BOTH adapters so every caller inherits it" — one definition on
        # the shared base, not two drifting copies. The positive control is
        # that `send` DOES differ per adapter (orca appends --enter, herdr
        # picks `pane run` vs `pane send-text`), so identity above is a real
        # measurement and not an artifact of every attribute matching.
        self.assertIs(harness.OrcaAdapter.submit, harness._CLIAdapter.submit)
        self.assertIs(harness.HerdrAdapter.submit, harness._CLIAdapter.submit)
        self.assertIsNot(harness.OrcaAdapter.send, harness.HerdrAdapter.send)


class PrimitiveContractTest(unittest.TestCase):
    """The promise is only as honest as the primitive underneath it.

    task/344 (`reuse-inherits-the-primitives-silence`): three helm primitives
    were found returning unavailability as a VALUE their callers discarded, and
    an `except` arm wrapped around a call that swallows internally is a branch
    that CANNOT FIRE. `verify_submitted` wraps `self.read` in exactly such an
    arm and answers UNKNOWN from it, so the arm's liveness is part of this
    lane's contract — not an assumption about somebody else's code.
    """

    def _orca(self, stdout, rc=0, stderr=""):
        class P:
            def __init__(s):
                s.stdout, s.returncode, s.stderr = stdout, rc, stderr
        return mock.patch.object(harness.subprocess, "run", return_value=P())

    def test_the_read_primitive_RAISES_it_does_not_swallow(self):
        ad = harness.OrcaAdapter("/fake/bin/orca")
        for label, stdout, rc in (("nonzero rc", "", 1),
                                  ("unparseable JSON", "not json", 0),
                                  ("ok:false", '{"id":"x","ok":false}', 0)):
            with self.subTest(failure=label):
                with self._orca(stdout, rc=rc):
                    with self.assertRaises(harness.HarnessError):
                        ad.read("pane-1")

    def test_the_UNKNOWN_arm_is_reachable_from_a_REAL_adapter_failure(self):
        # Not a fake that raises on command — the SHIPPED OrcaAdapter, failing
        # the way its CLI actually fails, reaching UNKNOWN through the except
        # arm. This is what proves the branch is live rather than decorative.
        ad = harness.OrcaAdapter("/fake/bin/orca")
        with self._orca("", rc=1, stderr="terminal_handle_stale"):
            state, detail = ad.verify_submitted("pane-1", "a directive",
                                                reads=1, interval=0)
        self.assertEqual(state, harness.UNKNOWN)
        self.assertIn("re-read failed", detail)

    def test_a_reply_with_NO_tail_field_also_reaches_UNKNOWN(self):
        # The other half, and the one the live fleet actually takes: rc 0, a
        # well-formed reply, and no tail. `read` answers "" for that, which is
        # a SILENT unavailability — so the arm that catches it is the value
        # check, not the except.
        ad = harness.OrcaAdapter("/fake/bin/orca")
        with self._orca('{"id":"x","ok":true,"result":{"terminal":{}}}'):
            self.assertEqual(ad.read("pane-1"), "")
            state, _ = ad.verify_submitted("pane-1", "a directive", reads=1,
                                           interval=0)
        self.assertEqual(state, harness.UNKNOWN)


class ResumeTurnModeTest(unittest.TestCase):
    """The tri-state must survive the trip into resume-turn's mode strings."""

    def test_each_state_maps_to_its_own_mode(self):
        from helm import resumeturn
        self.assertEqual(resumeturn._mode_for(harness.DELIVERED, "p")[0],
                         "resumed")
        self.assertEqual(resumeturn._mode_for(harness.NOT_DELIVERED, "p")[0],
                         "manual")
        self.assertEqual(resumeturn._mode_for(harness.UNKNOWN, "p")[0],
                         "unverified")

    def test_UNKNOWN_never_reports_the_success_mode(self):
        from helm import resumeturn
        mode, detail = resumeturn._mode_for(harness.UNKNOWN, "could not read")
        self.assertNotEqual(mode, "resumed")
        self.assertIn("could not read", detail)


class FleetProbeTest(unittest.TestCase):
    """The probe: held / clear / CANNOT-TELL, and never two of the three."""

    def test_held_composer_is_reported_held_with_its_text(self):
        pane = {"handle": "pane-1", "last_output_at": 1}
        state, body, _ = composers.classify(pane, _frame("Continue the lane."))
        self.assertEqual(state, composers.HELD)
        self.assertEqual(body, "Continue the lane.")

    def test_recorded_match_needs_a_later_persistent_read(self):  # noqa: VACUOUS_ASSERTION — pending and stranded are positive controls on the same exact match
        pane = {"handle": "pane-1", "last_output_at": 1}
        inj = {"text": "Continue the lane.", "held_at": None}
        state, _, _ = composers.classify(
            pane, _frame(inj["text"]), injection=inj, now=100)
        self.assertEqual(state, composers.HELM_PENDING)
        inj["held_at"] = 100
        with mock.patch.object(resumeturn, "recovery_persist_s",
                               return_value=30):
            early = composers.classify(
                pane, _frame(inj["text"]), injection=inj, now=120)[0]
            late = composers.classify(
                pane, _frame(inj["text"]), injection=inj, now=131)[0]
        self.assertEqual(early, composers.HELM_PENDING)
        self.assertEqual(late, composers.HELM_STRANDED)

    def test_recorded_prefix_with_a_human_append_remains_unowned(self):
        pane = {"handle": "pane-1", "last_output_at": 1}
        inj = {"text": "Continue the lane.", "held_at": 1}
        state, _, why = composers.classify(
            pane, _frame("Continue the lane. and my draft"),
            injection=inj, now=999)
        self.assertEqual(state, composers.HELD)
        self.assertIn("human", why)

    def test_empty_composer_is_clear(self):
        pane = {"handle": "pane-1", "last_output_at": 1}
        self.assertEqual(composers.classify(pane, _frame())[0], composers.CLEAR)
        # POSITIVE CONTROL: a classifier stuck at CLEAR would pass the line
        # above and report a stranded fleet as healthy — which IS the bug.
        self.assertEqual(composers.classify(pane, _frame("Continue."))[0],
                         composers.HELD)

    def test_known_placeholder_is_clear_not_held(self):
        pane = {"handle": "pane-1", "last_output_at": 1}
        self.assertEqual(composers.classify(
            pane, _frame("Press up to edit queued messages"))[0],
            composers.CLEAR)
        # An UNRECOGNISED non-empty composer is still HELD — the placeholder
        # list suppresses what was measured, never everything non-empty.
        self.assertEqual(composers.classify(
            pane, _frame("press on with the lane"))[0], composers.HELD)

    def test_pane_the_metaharness_never_saw_output_from_is_CANNOT_TELL(self):
        # The 12-of-26 blind population. Folding these into `clear` is how a
        # probe reports an all-clear over half a fleet nobody looked at.
        pane = {"handle": "pane-1", "last_output_at": None}
        state, _, why = composers.classify(pane, "")
        self.assertEqual(state, composers.CANNOT_TELL)
        self.assertNotEqual(state, composers.CLEAR)
        self.assertIn("lastOutputAt", why)

    def test_read_failure_is_CANNOT_TELL(self):
        pane = {"handle": "pane-1", "last_output_at": 1}
        state, _, why = composers.classify(pane, None, read_error="boom")
        self.assertEqual(state, composers.CANNOT_TELL)
        self.assertIn("boom", why)

    def test_unresolvable_composer_is_CANNOT_TELL(self):
        pane = {"handle": "pane-1", "last_output_at": 1}
        buried = _frame("Continue.") + "\n● newer output below the prompt"
        self.assertEqual(composers.classify(pane, buried)[0],
                         composers.CANNOT_TELL)
        # The same frame without the newer line classifies HELD, so the
        # cannot-tell is caused by the buried prompt and not by a dead arm.
        self.assertEqual(composers.classify(pane, _frame("Continue."))[0],
                         composers.HELD)

    def test_scan_counts_all_three_buckets_over_a_mixed_fleet(self):
        panes = [{"handle": "p-held", "last_output_at": 1, "title": "",
                  "worktree": ""},
                 {"handle": "p-clear", "last_output_at": 1, "title": "",
                  "worktree": ""},
                 {"handle": "p-blind", "last_output_at": None, "title": "",
                  "worktree": ""}]
        # THE BLIND PANE NOW HAS A TAIL, and its absence here used to be the
        # fixture agreeing with the defect: `scan` never read that handle, so
        # nobody had to say what a read would return. It returns text — a pane
        # that has never turned is exactly the pane still holding an unsent
        # brief — so the fixture supplies one and the census classifies it.
        tails = {"p-held": _frame("Continue the lane."), "p-clear": _frame(),
                 "p-blind": _frame("Run `helm seat boot-brief --rearm`.")}

        class Ad:
            name = "fake"

            def list(self):
                return panes

            def read(self, handle, limit=3000):
                return tails[handle]

        rows, err = composers.scan(adapter=Ad())
        self.assertIsNone(err)
        got = {r["handle"]: r["state"] for r in rows}
        self.assertEqual(got, {"p-held": composers.HELD,
                               "p-clear": composers.CLEAR,
                               "p-blind": composers.HELD})

    def test_scan_READS_a_blind_pane_because_the_metadata_is_stale(self):
        """INVERTED, and the old premise is why. This arm asserted `touched ==
        []` under the comment "reading it would raise or return '' and cost a
        subprocess to learn nothing; `last_output_at` already answered."

        THAT PREMISE IS FALSE, MEASURED (task/1906, 2026-09-09): handle
        term_fb5a9ffd appeared in the census as "the metaharness has never
        observed output from this pane", and a direct read on THAT SAME HANDLE
        returned `draft: "Run `helm seat boot-brief --rearm` and follow it."`.
        A readable pane with an informative tail. `last_output_at` null is
        STALE METADATA — a reboot clears it — never a read failure, and the
        guard assumed what a read would return and used that assumption to
        skip the read.

        IT SKIPPED EXACTLY THE TARGET CLASS. A pane holding an unsent brief has
        never turned, so it has never produced output, so `last_output_at` is
        null. `helm seat composers` answered "0 held" twice, eleven minutes
        apart, while five panes provably held Helm's own onboarding brief. Nor
        was it self-correcting: `last_output_at` cannot clear until the seat
        emits output, and the seat cannot emit output until someone presses the
        Enter this census exists to find.
        """
        touched = []

        class Ad:
            name = "fake"

            def list(self):
                return [{"handle": "p-blind", "last_output_at": None,
                         "title": "", "worktree": ""}]

            def read(self, handle, limit=3000):
                touched.append(handle)
                return _frame("Run `helm seat boot-brief --rearm`.")

        rows, _err = composers.scan(adapter=Ad())
        self.assertEqual(touched, ["p-blind"],
                         "the census skipped the read on exactly the pane it "
                         "exists to find")
        # AND THE READ IS USED, not merely performed: a scan that read the pane
        # and still reported CANNOT_TELL would satisfy the line above while
        # leaving the defect in place.
        self.assertEqual([r["state"] for r in rows], [composers.HELD])

    def test_absent_metaharness_is_an_honest_error_not_an_empty_fleet(self):
        with mock.patch.object(harness, "detect", return_value=None):
            rows, err = composers.scan()
        self.assertEqual(rows, [])
        self.assertTrue(err)


if __name__ == "__main__":
    unittest.main()
