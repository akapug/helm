#!/usr/bin/env python3
"""An escalation resolves its addressee, or says it could not.

THE DEFECT THIS FILE PINS: a mention spelled into a send is a bet that the
roster still carries that name. The send door accepts any token and reports
success, so the row is written, reaches nobody, and is reported as delivered.
The failure direction is silent-on-danger, which is the one an escalation
must never take.
"""
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import hardcode, seats_integrator


def _roster(*names):
    return {n: {"seat": n} for n in names}


class TheIntegratorIsResolvedNeverGuessedTest(unittest.TestCase):

    def setUp(self):
        for v in ("HELM_INTEGRATOR_SEAT", "MELD_INTEGRATOR_SEAT"):
            self.addCleanup(os.environ.pop, v, None)
            os.environ.pop(v, None)

    def test_a_rostered_default_resolves_to_itself(self):
        seat, why = seats_integrator.integrator_seat(
            snapshot=_roster(seats_integrator.INTEGRATOR_SEAT_DEFAULT, "seat-a"))
        self.assertEqual(seat, seats_integrator.INTEGRATOR_SEAT_DEFAULT)
        self.assertIsNone(why)

    def test_a_SOLE_suffix_candidate_is_inferred_when_the_default_is_gone(self):
        seat, why = seats_integrator.integrator_seat(
            snapshot=_roster("seat-a-integrator", "seat-c"))
        self.assertEqual(seat, "seat-a-integrator")
        self.assertIsNone(why)

    def test_TWO_candidates_is_a_question_the_roster_cannot_answer(self):
        seat, why = seats_integrator.integrator_seat(
            snapshot=_roster("seat-a-integrator", "seat-b-integrator"))
        self.assertIsNone(seat, "two candidates must not be resolved by luck")
        self.assertIn("2 candidate", why)
        self.assertIn("seat-a-integrator", why)
        self.assertIn("seat-b-integrator", why)

    def test_no_candidate_at_all_refuses_and_says_the_count_is_zero(self):
        seat, why = seats_integrator.integrator_seat(
            snapshot=_roster("seat-c"))
        self.assertIsNone(seat)
        self.assertIn("none", why)

    def test_an_override_that_does_NOT_resolve_REFUSES_beside_a_sole_candidate(self):
        """THE ARM THAT SEPARATES A REFUSAL FROM A SUBSTITUTION.

        A sole `-integrator` candidate is present, so the inference below
        WOULD succeed. An explicit override that misses is a typo, not a
        request to infer a substitute — falling through here hands the
        operator a DIFFERENT seat and makes the inference look like a
        resolution."""
        os.environ["HELM_INTEGRATOR_SEAT"] = "seat-a-integratr"
        snap = _roster("seat-a-integrator", "seat-c")
        # POSITIVE CONTROL FIRST, same snapshot, same call: without the
        # override this snapshot resolves. So a refusal below is about the
        # override and not about an unresolvable roster.
        del os.environ["HELM_INTEGRATOR_SEAT"]
        seat, why = seats_integrator.integrator_seat(snapshot=snap)
        self.assertEqual(seat, "seat-a-integrator")
        os.environ["HELM_INTEGRATOR_SEAT"] = "seat-a-integratr"
        seat, why = seats_integrator.integrator_seat(snapshot=snap)
        self.assertIsNone(seat, "a typo'd override silently became a "
                                "different seat")
        self.assertIn("does not resolve", why)
        self.assertIn("not a request", why)

    def test_a_MALFORMED_row_refuses_by_EITHER_path_not_just_the_override(self):
        """ONE NAME MUST NOT GET TWO ANSWERS DEPENDING ON HOW IT WAS REACHED.

        The direct lookup shape-checks the row. The suffix inference beside it
        searches for names ending in the suffix — and THE DEFAULT NAME ENDS IN
        THAT SUFFIX, so without the same check the inference re-admits exactly
        the seat the lookup just rejected. The control is the override arm
        below it: same malformed row, same seat name, and it refuses there.
        A module that promises a name it cannot confirm produces a refusal
        must not produce that name in its own default configuration."""
        default = seats_integrator.INTEGRATOR_SEAT_DEFAULT
        self.assertTrue(default.endswith(seats_integrator.INTEGRATOR_SUFFIX),
                        "this arm is only about a default that the inference "
                        "can also reach; the premise no longer holds")
        # POSITIVE CONTROL: a WELL-FORMED row of that same name resolves, so a
        # refusal below is about the row's SHAPE and not about the name.
        seat, why = seats_integrator.integrator_seat(
            snapshot={default: {"seat": default}})
        self.assertEqual(seat, default)
        self.assertIsNone(why)
        bad = {default: None}
        seat, why = seats_integrator.integrator_seat(snapshot=bad)
        self.assertIsNone(seat, "a malformed row resolved because the "
                                "inference re-admitted the name the direct "
                                "lookup had just rejected")
        self.assertIn("does not resolve", why)
        # THE CONTROL THAT MAKES IT A FINDING: identical row, identical name,
        # reached by an OVERRIDE instead of by the default.
        os.environ["HELM_INTEGRATOR_SEAT"] = default
        seat2, why2 = seats_integrator.integrator_seat(snapshot=bad)
        self.assertIsNone(seat2)
        self.assertIn("does not resolve", why2)

    def test_an_override_that_resolves_wins(self):
        os.environ["HELM_INTEGRATOR_SEAT"] = "seat-b-integrator"
        seat, why = seats_integrator.integrator_seat(
            snapshot=_roster("seat-b-integrator", "seat-a-integrator"))
        self.assertEqual(seat, "seat-b-integrator")
        self.assertIsNone(why)

    def test_an_UNREADABLE_roster_is_not_evidence_that_a_seat_is_gone(self):
        with mock.patch("helm.seats_roster.roster_checked",
                        return_value=({}, True)):
            seat, why = seats_integrator.integrator_seat()
        self.assertIsNone(seat)
        self.assertIn("unreadable", why)
        # AND IT MUST NOT WEAR THE ABSENT SENTENCE: a failed probe and a
        # proven-absent seat are opposite facts about the world.
        self.assertNotIn("does not resolve in the roster", why)

    def test_or_default_NEVER_returns_None_and_is_LOUD_when_it_substitutes(self):
        seats_integrator._warn_once.__globals__["_FOREIGN_WARNED"].clear()
        err = io.BytesIO()
        with mock.patch("helm.seats_integrator.integrator_seat",
                        return_value=(None, "roster unreadable")), \
             mock.patch("os.write",
                        side_effect=lambda fd, b: err.write(b) if fd == 2
                        else len(b)):
            got = seats_integrator.integrator_seat_or_default()
        self.assertEqual(got, seats_integrator.INTEGRATOR_SEAT_DEFAULT)
        said = err.getvalue().decode("utf-8", "replace")
        self.assertIn("could not be proven live", said)
        self.assertIn("roster unreadable", said)
        # CONTROL: a RESOLVED integrator says nothing at all.
        err2 = io.BytesIO()
        with mock.patch("helm.seats_integrator.integrator_seat",
                        return_value=("seat-a-integrator", None)), \
             mock.patch("os.write",
                        side_effect=lambda fd, b: err2.write(b) if fd == 2
                        else len(b)):
            self.assertEqual(seats_integrator.integrator_seat_or_default(),
                             "seat-a-integrator")
        self.assertEqual(err2.getvalue(), b"",
                         "a proven-live integrator and a substituted default "
                         "must not be the same observable")

    def test_the_MENTION_carries_its_trailing_space_or_is_EMPTY_with_a_reason(self):
        with mock.patch("helm.seats_integrator.integrator_seat",
                        return_value=("seat-a-integrator", None)):
            mention, why = seats_integrator.integrator_mention()
        self.assertEqual(mention, "@seat-a-integrator ")
        self.assertIsNone(why)
        with mock.patch("helm.seats_integrator.integrator_seat",
                        return_value=(None, "nobody is rostered")):
            mention, why = seats_integrator.integrator_mention()
        self.assertEqual(mention, "", "an unresolved addressee must not "
                                      "render a mention at all")
        self.assertEqual(why, "nobody is rostered")


class AHostileRosterKeyLosesItsControlBytesTest(unittest.TestCase):
    """THE ARM THE SECTION-B SWEEP WOULD HAVE BEEN, kept beside the module.

    The display-launder tripwire allowlists this module on a written claim:
    every roster-sourced string that leaves it goes through _seat_label. A
    claim in an allowlist is prose until something drives it, and that sweep
    drives named verbs rather than a table, so the driver lives here.
    """

    ESC = "\x1b"
    BIDI = "\u202e"

    def setUp(self):
        for v in ("HELM_INTEGRATOR_SEAT", "MELD_INTEGRATOR_SEAT"):
            self.addCleanup(os.environ.pop, v, None)
            os.environ.pop(v, None)

    def _hostile(self):
        return "seat" + self.ESC + "[2J" + self.BIDI + "-integrator"

    def test_a_hostile_key_is_laundered_in_BOTH_emissions(self):
        hostile = self._hostile()
        # CONTROL FIRST, ON THE SAME TWO OBSERVABLES: a LEGITIMATE key passes
        # through byte-identical, so a difference below is the laundering and
        # not the renderer mangling every name it touches.
        clean = "seat-a-integrator"
        seat, why = seats_integrator.integrator_seat(
            snapshot=_roster(clean))
        self.assertEqual(seat, clean)
        with mock.patch.object(seats_integrator, "integrator_seat",
                               return_value=(clean, None)):
            self.assertEqual(seats_integrator.integrator_mention()[0],
                             "@%s " % clean)
        # THE MENTION, which reaches a room and therefore a terminal.
        with mock.patch.object(seats_integrator, "integrator_seat",
                               return_value=(hostile, None)):
            mention, _why = seats_integrator.integrator_mention()
        self.assertNotIn(self.ESC, mention,
                         "an ESC from a roster key reached a room post")
        self.assertNotIn(self.BIDI, mention,
                         "a bidi override from a roster key reached a room "
                         "post")
        self.assertTrue(mention.startswith("@"), mention)
        # THE REFUSAL TEXT, which names every candidate on an ambiguity.
        _seat, why = seats_integrator.integrator_seat(
            snapshot=_roster(hostile, "seat-b-integrator"))
        self.assertIsNone(_seat, "two candidates resolved")
        self.assertNotIn(self.ESC, why,
                         "an ESC reached the ambiguity refusal")
        self.assertNotIn(self.BIDI, why,
                         "a bidi override reached the ambiguity refusal")


class ABakedAddresseeIsRefusedAtTheDoorTest(unittest.TestCase):
    """The rung fires on the ACT of sending to a literal, not on a name.

    THE SEAT-NAME RUNG BESIDE IT CATCHES THE SPELLINGS THAT WORK. A role
    token matches none of its alternatives, so the addressee that reaches
    nobody was the one it could not see.
    """

    # The exact python send that shipped. Every arm below runs this FIRST and
    # unconditionally: an absence and a loop that never iterated are both
    # indistinguishable from a blind instrument, and this is the one payload
    # that proves the instrument can see at all inside THIS test.
    LIVE = b'                chat.post("@integrator " + note)\n'

    def _fires(self, payload):
        return [c for c, _why, _ev in hardcode._hits(payload)]

    def _instrument_is_live(self):
        self.assertIn("baked-addressee", self._fires(self.LIVE),
                      "the rung did not fire on the send that shipped, so "
                      "every other reading in this test is about a blind "
                      "instrument rather than about its payload")

    def test_the_THREE_SHAPES_THAT_SHIPPED_all_fire(self):
        self._instrument_is_live()
        # Byte-for-byte the three sends that addressed a name no roster
        # carried: one python call and two shell lines from an installed hook.
        shapes = (
            self.LIVE,
            b'    command -v helm >/dev/null 2>&1 && helm chat post \\\n'
            b'      "@integrator guard healed \'checkout -b $branch\'"\n',
            b'    command -v helm >/dev/null 2>&1 && helm chat post \\\n'
            b'      "@integrator shared checkout switched off $base"\n')
        seen = 0
        for shape in shapes:
            with self.subTest(shape=shape[:40]):
                self.assertIn("baked-addressee", self._fires(shape),
                              "the rung cannot see the shape it exists for")
            seen += 1
        self.assertEqual(seen, 3, "the loop did not reach every shape, so "
                                  "this arm is quieter than it reads")

    def test_the_CURED_shapes_do_NOT_fire(self):  # noqa: VACUOUS_ASSERTION — _instrument_is_live() runs FIRST and unconditionally in this arm and asserts the rung fires on the send that shipped, which is the same observable; the rung reads it as absent because the control is a helper call rather than an inline assertion
        self._instrument_is_live()
        cured = (
            b'                chat.post(mention + note if mention else\n',
            b'    command -v helm >/dev/null 2>&1 && helm chat post '
            b'--to-integrator \\\n      "shared checkout switched off '
            b'$base"\n')
        seen = 0
        for shape in cured:
            with self.subTest(shape=shape[:40]):
                self.assertNotIn("baked-addressee", self._fires(shape),
                                 "the cure reddens its own rung")
            seen += 1
        self.assertEqual(seen, 2)

    def test_a_DM_to_a_literal_mention_fires_too(self):  # noqa: VACUOUS_ASSERTION — this arm asserts a POSITIVE, not an absence, and _instrument_is_live() runs first and unconditionally besides; the rung reads no structural assertion because both live in helper calls rather than inline
        self._instrument_is_live()
        self.assertIn("baked-addressee",
                      self._fires(b'    seats.dm("@seat-a-integrator", body)\n'),
                      "a dm is a send; only the room verb was covered")

    def test_PROSE_naming_a_seat_is_untouched_because_a_send_is_not_prose(self):  # noqa: VACUOUS_ASSERTION — _instrument_is_live() runs FIRST and unconditionally in this arm and asserts the rung fires on the send that shipped, which is the same observable; the rung reads it as absent because the control is a helper call rather than an inline assertion
        self._instrument_is_live()
        prose = (b'# the same nag misbilled @seat-a-integrator twice, and the\n'
                 b'# @integrator digest rode with it\n')
        self.assertNotIn("baked-addressee", self._fires(prose),
                         "a comment is not a send; scanning prose would make "
                         "this rung unusable and it would be turned off")


if __name__ == "__main__":
    unittest.main()
