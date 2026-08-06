#!/usr/bin/env python3
"""`concur` — the verdict polarity that authorizes NOTHING.

THE ASYMMETRY IT CURES, found by using the ledger for an ARC council on
2026-08-05: `approve` requires a verified gate (correctly, permanently), while
`fix` and `supersede` bind ungated. So the only ungated verdicts available were
the DISAPPROVING ones, and on a row whose artifact is not a landable tree a seat
that agreed had to mint a whole-suite receipt to endorse a markdown file while a
seat that objected bound for free.

WHAT THESE TESTS ACTUALLY DEFEND. "Authorizes nothing" is a claim about ABSENCE
from every authorization set, and absence is exactly the shape that rots
silently: someone adds `concur` to one door for one convenience and no test
notices. So the absence is pinned over the WHOLE door set computed at runtime —
not a hand-copied list that drifts from the real one — and every absence
assertion carries a must-HIT control on the same structure, so a suite that
stopped seeing the doors at all cannot pass.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import dispatches, verdicts


class ConcurVocabularyTest(unittest.TestCase):
    def test_it_is_a_real_polarity_the_writer_accepts(self):
        self.assertIn("concur", verdicts.POLARITIES)
        self.assertIn("--concur", verdicts.POLARITY_FLAGS)

    def test_it_is_NOT_a_work_polarity(self):
        """The three that speak about landable work stay exactly three."""
        self.assertEqual(verdicts.WORK_POLARITIES,
                         ("approve", "fix", "supersede"))
        self.assertNotIn("concur", verdicts.WORK_POLARITIES)


class ConcurAuthorizesNothingTest(unittest.TestCase):
    """The load-bearing property, pinned across every door rather than asserted."""

    def test_no_close_door_admits_it(self):
        doors = dispatches._CLOSE_POLARITY
        # MUST-HIT CONTROL FIRST: the structure is real and populated, and a
        # polarity that IS admitted somewhere reads as admitted. Without this,
        # an empty or renamed door map would satisfy the absence below.
        self.assertGreaterEqual(len(doors), 5)
        self.assertIn("approve", doors["landed"])
        admits_concur = sorted(r for r, pols in doors.items()
                               if "concur" in pols)
        self.assertEqual(admits_concur, [],
                         "concur must open no close door; it authorizes nothing")

    def test_it_does_not_terminate_a_review_spiral(self):
        """A spiral ends when the work is settled. An endorsement settles no
        work, so concur must not be read as a terminal verdict."""
        self.assertIn("approve", dispatches.SPIRAL_TERMINAL_POLARITIES)  # control
        self.assertNotIn("concur", dispatches.SPIRAL_TERMINAL_POLARITIES)

    def test_every_authorization_set_in_the_module_excludes_it(self):
        """THE ANTI-ROT ARM. Any module-level tuple/frozenset of polarity
        strings is an authorization set by construction; concur belongs to none
        of them. Computed from the live module, so a NEW set added tomorrow is
        covered without editing this test."""
        sets = {}
        for name in dir(dispatches):
            if not name.isupper():
                continue
            value = getattr(dispatches, name)
            if not isinstance(value, (tuple, frozenset, set, list)) or not value:
                continue
            if not all(isinstance(v, str) for v in value):
                continue
            if not any(v in verdicts.WORK_POLARITIES for v in value):
                continue
            # THE VOCABULARY REGISTER IS NOT AN AUTHORIZATION SET. POLARITIES
            # lists what EXISTS; a door lists what PERMITS. concur must be in
            # the first and in none of the second, so a set equal to the whole
            # vocabulary is skipped by that property rather than by its name —
            # a renamed register stays covered. (This arm caught POLARITIES on
            # its first run, which is the heuristic working.)
            if set(value) == set(verdicts.POLARITIES):
                continue
            sets[name] = tuple(value)
        # control: we actually found the sets we know exist
        self.assertIn("SPIRAL_TERMINAL_POLARITIES", sets)
        leaked = sorted(n for n, v in sets.items() if "concur" in v)
        self.assertEqual(leaked, [],
                         "concur leaked into an authorization set: %s" % leaked)


class ConcurCannotReachAbandonTest(unittest.TestCase):
    """THE HOLE MY OWN ANTI-ROT ARM COULD NOT SEE, found by doing the review
    I had asked someone else for.

    `abandon` writes off REVIEWED WORK whose commit is provably gone. Its guard
    required status=="verdict" and kind=="review" and never read POLARITY — so
    a `concur` row satisfied it, and a polarity that authorizes nothing would
    have authorized a terminal transition a verdict-less row cannot make.

    The anti-rot test enumerates module-level SETS; this was an `if` condition,
    which no set-enumeration can reach. That is the arm's real boundary and it
    is worth stating: a polarity check written inline is invisible to a check
    that reads collections."""

    def test_the_abandon_guard_reads_polarity_not_merely_status(self):
        import inspect
        src = inspect.getsource(dispatches._apply)
        head, _sep, tail = src.partition('event == "abandon"')
        self.assertTrue(_sep, "abandon arm moved — re-anchor this test")
        window = tail[:400]
        self.assertIn("_WORK_POLARITIES", window,
                      "abandon admits any polarity: concur would authorize it")
        # control on the same window: the conditions it DID read are still there
        self.assertIn('kind") == "review"', window)


class ConcurBindsUngatedTest(unittest.TestCase):
    """The whole point: endorsement must not cost a receipt."""

    def test_the_gate_requirement_names_approve_and_only_approve(self):
        """TRACED, and pinned so the rule cannot silently widen: the writer
        gates on an EXACT polarity match, not on a set, so adding a polarity
        never accidentally inherits the receipt requirement."""
        import inspect
        src = inspect.getsource(dispatches.mark_verdict)
        self.assertIn('polarity == "approve"', src)
        self.assertNotIn('polarity in ("approve"', src)


if __name__ == "__main__":
    unittest.main()
