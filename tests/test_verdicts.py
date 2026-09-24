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

    #: The doors that LAND work or SETTLE it. Concur opens none of them, and
    #: they are named rather than inferred from "every door" so the law states
    #: the property it is actually about.
    LANDING_AND_SETTLEMENT = ("landed", "superseded", "subsumed",
                              "discharged", "resolved", "carried")
    #: THE EXCEPTIONS, AND THE PROPERTY THEY SHARE — they are not a list of
    #: doors somebody decided to allow, they are the doors whose admission
    #: REQUIRES GIT TO HAVE MEASURED WHERE THE WORK IS and that grant no
    #: authority whichever way the measurement came out.
    #:
    #: THIS WAS "ABSENT FROM TRUNK" AND THE NARROWER READING WAS THE BUG.
    #: Stated that way the law had a hole exactly the size of its own
    #: converse: a concur over work ON trunk could open no door at all, and
    #: that is the COMMON case rather than the edge — every one of the ten
    #: doors was measured against such a row and every one refused, each for a
    #: sound reason of its own. What actually makes an exception safe is not
    #: the direction of the measurement but that the close GRANTS NOTHING, and
    #: both directions have that property. If the work is ABSENT, retiring the
    #: row records that it will not land. If the work is PRESENT and this row
    #: authorized nothing, then this row did not put it there, so retiring it
    #: records where the authority was NOT. Neither permits a land and neither
    #: settles work, which is why the law is stated about LANDING AND
    #: SETTLEMENT doors and not about every door.
    #:
    #: `withdrawn` proves the absence in its own ladder; `expired` refuses any
    #: row whose land state is not a MEASURED ABSENT, so it never reaches
    #: polarity for a landed or an unmeasurable row at all; `endorsement-moot`
    #: is the mirror and refuses any tip that is not an ANCESTOR of a pinned
    #: trunk. EACH CARRIES ITS OWN BOUNDARY ARM asserting the population it
    #: must never touch is still refused — the two absence doors refuse a
    #: LANDED concur, the presence door refuses an ABSENT one and refuses
    #: every polarity but concur. Those boundaries are what keep an exception
    #: from becoming a way to retire work by choosing a polarity.
    ABSENCE_EXCEPTIONS = ("expired", "withdrawn")
    PRESENCE_EXCEPTION = "endorsement-moot"
    NONAUTHORIZING_EXCEPTIONS = ABSENCE_EXCEPTIONS + (PRESENCE_EXCEPTION,)
    WITHDRAWN_EXCEPTION = "withdrawn"

    def test_no_LANDING_or_SETTLEMENT_door_admits_it(self):  # noqa: VACUOUS_ASSERTION — the MUST-HIT control runs first and unconditionally: the door map is populated and a polarity that IS admitted somewhere reads as admitted, so an empty or renamed map fails before either the per-door assertNotIn or the computed-set equality is reached
        doors = dispatches._CLOSE_POLARITY
        # MUST-HIT CONTROL FIRST: the structure is real and populated, and a
        # polarity that IS admitted somewhere reads as admitted. Without this,
        # an empty or renamed door map would satisfy the absence below.
        self.assertGreaterEqual(len(doors), 5)
        self.assertIn("approve", doors["landed"])
        for reason in self.LANDING_AND_SETTLEMENT:
            if reason not in doors:
                continue
            self.assertNotIn("concur", doors[reason],
                             "%s lands or settles work; concur authorizes "
                             "nothing" % reason)
        # AND EVERY OTHER DOOR TOO, computed from the live map so a reason
        # added tomorrow is covered without editing this test. Only the one
        # named exception may admit it.
        admits = sorted(r for r, pols in doors.items() if "concur" in pols)
        self.assertEqual(admits, sorted(self.NONAUTHORIZING_EXCEPTIONS),
                         "concur opens a door that is not a NONAUTHORIZING "
                         "door — the exception is a MEASURED land state plus "
                         "a close that grants nothing, never the door's name")

    def test_expired_still_refuses_a_LANDED_concur(self):
        """THE SECOND EXCEPTION'S BOUNDARY, and it is the same boundary. The
        docstring above promises every absence door carries one; a promise
        without the arm is the rule shipping its own exception.

        `expired` never reaches polarity for a landed row — its predicate
        refuses anything whose land state is not a MEASURED ABSENT — so this
        asserts the refusal at the layer that actually decides, and asserts
        the UNMEASURABLE case too, which withdrawn's boundary has no analogue
        for."""
        from helm import landreq
        base = {"id": "d" * 32, "state": "REVIEWED", "polarity": "concur",
                "tier_kind": None, "reviewed_tip": "a" * 40,
                "verdict_ref": "advisory review recorded"}
        # UNCONDITIONAL POSITIVE CONTROL ON THE SAME DOOR, first: an ABSENT
        # concur IS admitted, so the refusals below are this boundary holding
        # rather than a predicate that refuses everything.
        state, why = landreq.expired_verdict(dict(base, land_state="ABSENT"),
                                             {}, None)
        self.assertEqual(state, landreq.EXPIRE_ADMIT, why)
        for land in ("LANDED", "MERGED_LOCAL"):
            state, why = landreq.expired_verdict(dict(base, land_state=land),
                                                 {}, None)
            self.assertEqual(state, landreq.EXPIRE_REFUSE, land)
            self.assertIn("REACHED the trunk", why)
        state, why = landreq.expired_verdict(dict(base, land_state="UNKNOWN"),
                                             {}, None)
        self.assertEqual(state, landreq.EXPIRE_UNMEASURED, why)

    def test_withdrawn_still_refuses_a_LANDED_concur(self):
        """THE BOUNDARY THAT MAKES THE EXCEPTION SAFE. Withdrawn admits concur
        only because the work is proven ABSENT; a concur row whose reviewed tip
        IS on trunk must still be refused, or the exception becomes a way to
        retire landed work by choosing a polarity."""
        from helm import landreq

        def refuse(polarity):
            row = {"id": "c" * 32, "polarity": polarity,
                   "reviewed_tip": "a" * 40, "repo_id": "/nonexistent-repo"}
            return landreq._close_ladder_withdrawn(row, "evidence",
                                                   dry_run=True)
        # UNCONDITIONAL POSITIVE CONTROL ON THE SAME DOOR, first: an
        # UNDECLARED polarity still hits the wall. Without it, an empty `err`
        # satisfies the assertNotIn below just as well as a landing proof
        # does, and so would a door that stopped refusing anything.
        _o, wall = refuse(None)
        self.assertIn("withdrawable", wall or "",
                      "the polarity wall is unreachable, so this arm is "
                      "about a string nothing produces")
        out, err = refuse("concur")
        self.assertIsNone(out)
        # THE POSITIVE FORM OF THE CLAIM: the refusal is the LANDING proof
        # failing closed on an unreadable repository, named rather than
        # inferred from the absence of the other message.
        self.assertIn("trunk", err or "",
                      "the refusal did not come from the landing proof")
        self.assertNotIn("withdrawable", err or "",
                         "concur was refused on POLARITY, so this arm never "
                         "reaches the landing proof it exists to hold")

    def test_the_presence_exception_still_refuses_an_ABSENT_concur(self):  # noqa: VACUOUS_ASSERTION — the positive control is unconditional and runs before the loop: the concur row reaches and PASSES the polarity gate (assertNotIn on the same string the loop asserts), so the loop's refusals are the gate discriminating rather than a ladder that refuses everything
        """THE THIRD EXCEPTION'S BOUNDARY, and it is the mirror of the two
        above. Those doors must never take a LANDED concur; this one must
        never take an ABSENT one, or it becomes a third absence door wearing a
        presence door's proof mode and the operator gets two answers for one
        row. The polarity leg is asserted too: this door is the only one in
        the table whose tuple is concur ALONE, because an approve over landed
        work already has `landed` and a second, weaker landing door for it is
        exactly what this exception must not become."""
        from helm import landreq
        doors = dispatches._CLOSE_POLARITY
        # MUST-HIT CONTROL FIRST: the door exists and admits concur, so the
        # refusals below are this boundary holding rather than assertions
        # about a reason nothing in the table contains.
        self.assertIn("concur", doors[self.PRESENCE_EXCEPTION])
        self.assertEqual(doors[self.PRESENCE_EXCEPTION], ("concur",),
                         "the presence door admits a polarity that already "
                         "has a landing door of its own")
        row = {"id": "e" * 32, "state": "LANDED", "polarity": "concur",
               "tier_kind": None, "reviewed_tip": "a" * 40,
               "verdict_ref": "advisory review recorded",
               "repo_id": "/nonexistent-repo"}
        # UNCONDITIONAL POSITIVE CONTROL ON THE SAME LADDER: the polarity gate
        # is REACHED and PASSED for a concur — the refusal here comes from the
        # unreadable repository, one rung LATER. Without this an arm asserting
        # a non-concur refusal would pass against a ladder that refuses
        # everything at rung one.
        _out, wall = landreq._close_ladder_endorsement_moot(
            row, "evidence", None, None, True)
        self.assertNotIn("not an endorsement", wall or "",
                         "a concur was refused on POLARITY, so the polarity "
                         "gate never discriminates anything")
        for polarity in ("approve", "fix", "supersede", None):
            _o, err = landreq._close_ladder_endorsement_moot(
                dict(row, polarity=polarity), "evidence", None, None, True)
            self.assertIn("not an endorsement", err or "", polarity)

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
