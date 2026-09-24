#!/usr/bin/env python3
"""The seat that FOLDS is a role, resolved at the moment of use.

THE DEFECT A DEFAULT STRING BUILDS IN. `os.environ.get("HELM_LANDER") or
"<a seat name>"` keeps naming that literal after the roster stops carrying
it, and nothing fails loudly: the string is truthy, the DM is addressed, the
room-post fallback is addressed, and both report a delivery neither made.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import dispatches, seats_integrator, seats_lander  # noqa: E402


class TheLanderIsAskedNotSpelledTest(unittest.TestCase):

    def setUp(self):
        self._prior = os.environ.pop("HELM_LANDER", None)
        if self._prior is not None:
            self.addCleanup(os.environ.__setitem__, "HELM_LANDER", self._prior)

    def test_with_no_override_the_lander_IS_the_integrator_question(self):
        """NOT A PARALLEL COPY OF THE RESOLUTION. A second implementation of
        "which seat is this role" starts at zero on every edge the first
        already handles — an unreadable roster, a malformed row, an ambiguous
        suffix — and the two drift apart in the direction nobody watches. So
        with no override this asks the integrator door and returns what it
        returns, refusal included."""
        with mock.patch.object(seats_integrator, "integrator_seat",
                               return_value=("seat-a-integrator", None)) as ask:
            self.assertEqual(seats_lander.lander_seat(),
                             ("seat-a-integrator", None))
        ask.assert_called_once()

        # THE REFUSAL TRAVELS TOO, which is the half a copy would have lost:
        # the lander does not resolve where the integrator could not.
        with mock.patch.object(seats_integrator, "integrator_seat",
                               return_value=(None, "roster unreadable")):
            seat, why = seats_lander.lander_seat()
        self.assertIsNone(seat)
        self.assertIn("roster unreadable", why)

    def test_an_override_is_OBEYED_because_it_IS_the_escape_hatch(self):
        """HELM_LANDER EXISTS FOR THE CASE WHERE THE ORDINARY RESOLUTION IS
        WRONG — including where the ROSTER is wrong — so confirming it against
        the roster before honouring it takes the hatch away at exactly the
        moment it is needed. An operator who types a seat name has made a
        decision, and this module is not the authority that overrules it.

        A TYPO IS NOT SILENT: the SEND door resolves every addressee and says
        "ABSENT — no current roster row" for a name nothing answers to. Asking
        that question again here would put a second, weaker copy in front of
        the one that is already right."""
        os.environ["HELM_LANDER"] = "seat-b"
        self.addCleanup(os.environ.pop, "HELM_LANDER", None)

        # A ROSTER THAT DOES NOT CARRY THE NAME CHANGES NOTHING, which is the
        # property. Both snapshots are passed and neither is consulted here.
        self.assertEqual(seats_lander.lander_seat({"seat-b": {"session": "s"}}),
                         ("seat-b", None))
        self.assertEqual(seats_lander.lander_seat({"seat-a": {"session": "s"}}),
                         ("seat-b", None))

    def test_an_override_does_NOT_reach_the_integrator_door(self):
        """An explicit override is an ANSWER, not a starting point. Consulting
        the integrator alongside it would make the fold's addressee depend on
        a question the operator already settled."""
        os.environ["HELM_LANDER"] = "seat-b"
        self.addCleanup(os.environ.pop, "HELM_LANDER", None)
        with mock.patch.object(seats_integrator, "integrator_seat",
                               return_value=("seat-a-integrator", None)) as ask:
            # UNCONDITIONAL CONTROL ON THE SAME MOCK: with the override
            # REMOVED the integrator door IS asked and its answer is what
            # comes back, so the zero below is about the short-circuit and not
            # about a patch wired to nothing.
            del os.environ["HELM_LANDER"]
            self.assertEqual(seats_lander.lander_seat(),
                             ("seat-a-integrator", None))
            self.assertEqual(ask.call_count, 1)

            os.environ["HELM_LANDER"] = "seat-b"
            self.assertEqual(seats_lander.lander_seat(), ("seat-b", None))
        self.assertEqual(ask.call_count, 1,
                         "an override consulted the integrator anyway")

    def test_this_module_never_reads_the_roster_itself(self):  # noqa: VACUOUS_ASSERTION — the unconditional control is the assertIn on the DELEGATE's source in the same method: seats_integrator does contain roster_checked, so the absence above is about WHERE the read lives and not about the string being unfindable anywhere
        """THE READ BELONGS TO THE DOOR THAT OWNS THE QUESTION. A second
        roster consumer is a second place a roster KEY can reach a sink
        unlaundered, and the tripwire that guards that population is right to
        want one reason per module. This module has no reason: with an
        override it answers from the environment, and without one it hands the
        whole question — snapshot included — to seats_integrator."""
        import inspect
        src = inspect.getsource(seats_lander)
        self.assertNotIn("roster_checked", src)
        self.assertNotIn("roster()", src)
        # POSITIVE CONTROL: the door it delegates to DOES read, so the absence
        # above is about where the read lives and not about nobody reading.
        self.assertIn("roster_checked", inspect.getsource(seats_integrator))

    def test_the_or_default_door_NEVER_returns_empty_and_says_when_it_guessed(
            self):
        """`dispatches._nudge` falls back to a ROOM POST addressed to this
        name, and an unaddressed room post is the exact defect that fallback
        exists to cure. So this may not refuse — what it must not do instead
        is substitute INVISIBLY."""
        # POSITIVE CONTROL: a resolvable lander comes back unchanged and
        # nothing is announced, so the warning below is about the refusal.
        with mock.patch.object(seats_lander, "lander_seat",
                               return_value=("seat-a-integrator", None)), \
             mock.patch.object(seats_lander, "_warn_once") as quiet:
            self.assertEqual(seats_lander.lander_seat_or_default(),
                             "seat-a-integrator")
        self.assertEqual(quiet.call_count, 0)

        with mock.patch.object(seats_lander, "lander_seat",
                               return_value=(None, "nothing resolves")), \
             mock.patch.object(seats_integrator, "integrator_seat_or_default",
                               return_value="seat-b-integrator") as handoff, \
             mock.patch.object(seats_lander, "_warn_once") as loud:
            got = seats_lander.lander_seat_or_default()
        self.assertTrue(got, "the fold was left with no addressee at all")
        # THE DEGRADED PATH HANDS OFF; IT DOES NOT NAME A CONSTANT. A literal
        # here reproduces this module's own thesis inside its cure — the
        # fallback would name a seat the roster does not carry, and `_nudge`
        # addresses a ROOM POST to it.
        self.assertEqual(got, "seat-b-integrator")
        self.assertEqual(handoff.call_count, 1)
        self.assertEqual(loud.call_count, 1)
        self.assertIn("nothing resolves", loud.call_args[0][1])

    def test_the_dispatch_door_asks_this_module_rather_than_a_literal(self):
        """The seam, driven rather than read: moving the resolver's answer
        moves the name every nudge addresses. No literal can do that, and an
        arm pinning the current seat name would pass while the module still
        carried one."""
        # UNCONDITIONAL CONTROL OUTSIDE THE LOOP: the door answers at all, and
        # with something non-empty — `_nudge` addresses its fallback with this
        # value, so an empty answer is the defect that fallback exists to cure.
        # Every assertion below is inside an iteration, so a loop that stopped
        # iterating would take the whole arm quiet with it.
        self.assertTrue(dispatches._default_lander())

        for who in ("seat-a-integrator", "seat-b-integrator"):
            with self.subTest(lander=who):
                with mock.patch.object(seats_lander, "lander_seat_or_default",
                                       return_value=who):
                    self.assertEqual(dispatches._default_lander(), who)


if __name__ == "__main__":
    unittest.main()
