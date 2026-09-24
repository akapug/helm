#!/usr/bin/env python3
"""Ownership is a lease on liveness: what keeps a row, and what frees it.

Every arm drives `owner_state` directly with a roster it builds, because the
predicate takes its expensive inputs — the roster, the turn-evidence source
and the descendant probe — as ARGUMENTS. That is the design rather than the
test's convenience: a liveness predicate that reaches for a process table
cannot be exercised without one, and a predicate nobody can exercise is how a
guard ships wrong.

AND THE EVIDENCE ARGUMENT IS TYPED FOR A MEASURED REASON. `last_seen` is the
mtime of `.seen`, which the BEACON POLL touches on every cycle, so a predicate
fed it measures beacon liveness while reporting turn completion. Taking no
beacon PARAMETER does not prevent that: the dependency arrives through
whichever evidence argument is accepted.
"""
import unittest

from tests._tmphome import pin_suite_guard        # noqa: F401
from helm import ownership

TRANSCRIPT = "session transcript terminal response"   # the one allowlisted
CHAT = "chat #helm"                     # a corroboration-only source
STOP = "helm Stop-dispatch allow"       # corroboration, deliberately NOT
                                        # allowlisted: helm sees its own
                                        # dispatch and nothing after it


def _roster(*seats):
    return {s: {} for s in seats}


def _ev(**kw):
    return lambda seat, row: ownership.TurnEvidence(**kw)


class OwnerStateTest(unittest.TestCase):
    NOW = 1_700_000_000.0
    OLD = NOW - 99 * 3600

    def state(self, seat, rows, evidence, descendants=lambda s: False):
        return ownership.owner_state(seat, rows, evidence, descendants,
                                     now=self.NOW)

    # ---- the completion-semantics gate, which is the whole revision --------

    def test_a_source_that_does_not_prove_completion_can_never_reach_DARK(self):
        """Old posts and absent posts are equally consistent with a seat
        working silently, so a corroboration-only source may support a LIVE
        answer and never a revert."""
        rows = _roster("seat-a")
        # POSITIVE CONTROL, unconditional and first: an ALLOWLISTED writer
        # with the SAME timestamp and the SAME probe DOES reach DARK, so the
        # UNKNOWN below is the source's semantics and not a predicate that
        # never reverts anything.
        self.assertEqual(ownership.DARK, self.state(
            "seat-a", rows,
            _ev(ts=self.OLD, source=TRANSCRIPT, proves_terminal_response=True))[0])
        verdict, why = self.state("seat-a", rows, _ev(ts=self.OLD, source=CHAT))
        self.assertEqual(ownership.UNKNOWN, verdict)
        self.assertIn("is not a TERMINAL RESPONSE", why)

    def test_a_caller_cannot_declare_its_own_source_a_completion_producer(self):
        """The flag binds to the WRITER, not to what a caller passes. A source
        believed on the strength of what it calls itself is the failure this
        type exists to prevent, and it must not be re-openable by one
        keyword."""
        liar = ownership.TurnEvidence(ts=self.OLD, source="my honest clock",
                                      proves_terminal_response=True)
        honest = ownership.TurnEvidence(ts=self.OLD, source=TRANSCRIPT,
                                        proves_terminal_response=True)
        self.assertTrue(honest.proves_terminal_response,
                        "the allowlisted writer was refused, so the assertion "
                        "below would be about the allowlist being empty")
        self.assertFalse(liar.proves_terminal_response)
        self.assertEqual(ownership.UNKNOWN,
                         self.state("seat-a", _roster("seat-a"),
                                    lambda s, r: liar)[0])

    def test_the_reason_never_claims_completion_for_a_corroborating_source(self):
        """A recent corroborating signal is inconsistent with dead, which is a
        HOLDING — but calling it a terminal response relabels the evidence
        exactly as `.seen` did, and the next reader quotes the sentence."""
        rows = _roster("seat-a")
        _v, chat_why = self.state("seat-a", rows,
                                  _ev(ts=self.NOW - 60, source=CHAT))
        _v2, proven_why = self.state("seat-a", rows,
                                    _ev(ts=self.NOW - 60, source=TRANSCRIPT,
                                        proves_terminal_response=True))
        self.assertIn("last terminal response", proven_why)
        self.assertNotIn("last terminal response", chat_why)
        self.assertIn("not a terminal response", chat_why)

    def test_a_PENDING_source_is_UNKNOWN_and_never_holds_or_reverts(self):
        """SEEN BUT NOT SETTLED is its own outcome. The producer found a
        terminal record too young for the harness write order to be
        decidable — a Stop hook may still be running and about to veto. Both
        confident answers are wrong here: HOLDING certifies a turn that may
        have been refused, and DARK reverts on an absence nobody established.
        """
        rows = _roster("seat-a")
        # POSITIVE CONTROLS, unconditional and first, at BOTH poles: the same
        # source with the same shape reaches HOLDING when fresh and DARK when
        # old, so the UNKNOWN below is the pending flag and not a predicate
        # that answers UNKNOWN to everything.
        self.assertEqual(ownership.HOLDING, self.state(
            "seat-a", rows, _ev(ts=self.NOW - 60, source=TRANSCRIPT,
                                proves_terminal_response=True))[0])
        self.assertEqual(ownership.DARK, self.state(
            "seat-a", rows, _ev(ts=self.OLD, source=TRANSCRIPT,
                                proves_terminal_response=True))[0])
        for ts in (self.NOW - 60, self.OLD, None):
            verdict, why = self.state(
                "seat-a", rows,
                _ev(ts=ts, source=TRANSCRIPT, proves_terminal_response=True,
                    pending="a terminal record 5s old with nothing after it"))
            self.assertEqual(ownership.UNKNOWN, verdict, "ts=%r" % (ts,))
            self.assertIn("not settled yet", why)

    # ---- coverage, which is not the same as absence ------------------------

    def test_a_source_that_cannot_see_the_seat_is_UNKNOWN(self):
        """Every proxy-family seat runs no Stop hook, so the stamp cannot
        cover it. Reading "this source has no record" as "this seat has done
        nothing" is the beacon error with its sign flipped."""
        rows = _roster("seat-a")
        self.assertEqual(ownership.DARK, self.state(
            "seat-a", rows,
            _ev(ts=self.OLD, source=TRANSCRIPT, proves_terminal_response=True))[0])
        verdict, why = self.state("seat-a", rows,
                                  _ev(covered=False, source=TRANSCRIPT))
        self.assertEqual(ownership.UNKNOWN, verdict)
        self.assertIn("does not cover", why)

    def test_an_unreadable_source_is_UNKNOWN_and_names_itself(self):
        verdict, why = self.state("seat-a", _roster("seat-a"),
                                  _ev(err="store unreadable", source=TRANSCRIPT))
        self.assertEqual(ownership.UNKNOWN, verdict)
        self.assertIn("store unreadable", why)
        self.assertIn(TRANSCRIPT, why)

    # ---- the conjunction ---------------------------------------------------

    def test_live_descendant_work_keeps_a_silent_seat_holding(self):
        """A seat waiting 34 minutes on a remote gate has completed no turn
        and is emphatically working."""
        rows = _roster("seat-a")
        ev = _ev(ts=self.OLD, source=TRANSCRIPT, proves_terminal_response=True)
        self.assertEqual(ownership.DARK, self.state("seat-a", rows, ev)[0])
        verdict, why = self.state("seat-a", rows, ev, lambda s: True)
        self.assertEqual(ownership.HOLDING, verdict)
        self.assertIn("descendant", why)

    def test_a_MISSING_timestamp_does_not_skip_the_descendant_half(self):
        """Returning DARK without calling the probe reverts a seat that has
        recorded no turn but holds live work, out from under itself."""
        called = []

        def probe(seat):
            called.append(seat)
            return True
        verdict, why = self.state("seat-a", _roster("seat-a"),
                                  _ev(ts=None, source=TRANSCRIPT,
                                      proves_terminal_response=True), probe)
        self.assertEqual(["seat-a"], called, "the probe was never called")
        self.assertEqual(ownership.HOLDING, verdict)
        self.assertIn("descendant", why)

    def test_an_unreadable_descendant_answer_is_UNKNOWN_not_free(self):
        rows = _roster("seat-a")
        ev = _ev(ts=self.OLD, source=TRANSCRIPT, proves_terminal_response=True)
        self.assertEqual(ownership.DARK, self.state("seat-a", rows, ev)[0])
        self.assertEqual(ownership.UNKNOWN,
                         self.state("seat-a", rows, ev, lambda s: None)[0])

    def test_a_descendant_probe_that_raises_is_UNKNOWN(self):
        def boom(seat):
            raise OSError("no process table")
        verdict, why = self.state("seat-a", _roster("seat-a"),
                                  _ev(ts=self.OLD, source=TRANSCRIPT,
                                      proves_terminal_response=True), boom)
        self.assertEqual(ownership.UNKNOWN, verdict)
        self.assertIn("OSError", why)

    def test_inside_the_threshold_holds_without_probing_descendants(self):
        called = []

        def probe(seat):
            called.append(seat)
            return False
        # POSITIVE CONTROL, unconditional and first: the same recorder DOES
        # fill when the predicate reaches for it, so the empty list below is a
        # probe not called rather than a recorder that was never wired.
        self.state("seat-a", _roster("seat-a"),
                   _ev(ts=self.OLD, source=TRANSCRIPT, proves_terminal_response=True),
                   probe)
        self.assertEqual(["seat-a"], called)
        del called[:]
        verdict, why = self.state(
            "seat-a", _roster("seat-a"),
            _ev(ts=self.NOW - 60, source=TRANSCRIPT, proves_terminal_response=True), probe)
        self.assertEqual(ownership.HOLDING, verdict)
        self.assertIn("last terminal response", why)
        self.assertEqual([], called)

    # ---- identity ----------------------------------------------------------

    def test_identity_is_casefold_exact_not_case_sensitive(self):
        """Roster `seat-a` queried as `Seat-A` read "no roster row" and went
        DARK — a live seat declared absent by a lookup."""
        rows = _roster("seat-a")
        ev = _ev(ts=self.NOW - 60, source=TRANSCRIPT, proves_terminal_response=True)
        self.assertEqual(ownership.HOLDING, self.state("seat-a", rows, ev)[0])
        self.assertEqual(ownership.HOLDING, self.state("Seat-A", rows, ev)[0])

    def test_a_name_with_no_roster_row_is_dark_by_definition(self):
        """There is no seat behind the name to be working. This is the largest
        class on the live ledgers, and the only one reaching DARK on its own
        evidence."""
        rows = _roster("seat-a")
        ev = _ev(ts=self.NOW - 60, source=TRANSCRIPT, proves_terminal_response=True)
        self.assertEqual(ownership.HOLDING, self.state("seat-a", rows, ev)[0])
        verdict, why = self.state("ghost", rows, ev)
        self.assertEqual(ownership.DARK, verdict)
        self.assertIn("no roster row", why)

    # ---- reads that cannot answer -----------------------------------------

    def test_an_unreadable_roster_is_UNKNOWN_and_never_dark(self):
        """Judging a live seat dead gives one piece of work two owners.
        seats_work_offer._live_seats paid for this already — a fail-open
        roster read made eleven live seats look absent at once."""
        ev = _ev(ts=self.OLD, source=TRANSCRIPT, proves_terminal_response=True)
        self.assertEqual(ownership.DARK,
                         self.state("seat-a", _roster("seat-a"), ev)[0])
        verdict, why = self.state("seat-a", None, ev)
        self.assertEqual(ownership.UNKNOWN, verdict)
        self.assertIn("roster did not read", why)

    def test_a_malformed_timestamp_is_UNKNOWN_not_a_crash_and_not_holding(self):
        """A roster row is JSON anybody can hand us. Subtracting a string
        crashed the census; treating NaN as a quiet duration is worse, because
        every comparison against NaN is False and the seat reads HOLDING."""
        rows = _roster("seat-a")
        # POSITIVE CONTROL on the same call shape: a FINITE timestamp through
        # the same source reaches a real verdict, so the UNKNOWNs below are
        # about the values and not about the arm's wiring.
        self.assertEqual(ownership.DARK, self.state(
            "seat-a", rows,
            _ev(ts=self.OLD, source=TRANSCRIPT, proves_terminal_response=True))[0])
        for bad in ("not-a-time", float("nan"), float("inf"), 10 ** 400):
            verdict, why = self.state(
                "seat-a", rows,
                _ev(ts=bad, source=TRANSCRIPT, proves_terminal_response=True))
            self.assertEqual(ownership.UNKNOWN, verdict, "ts=%r" % (bad,))
            self.assertIn("not a finite timestamp", why)

    def test_an_empty_seat_name_is_UNKNOWN(self):
        ev = _ev(ts=self.NOW, source=TRANSCRIPT, proves_terminal_response=True)
        self.assertEqual(ownership.UNKNOWN, self.state("", _roster(), ev)[0])
        self.assertEqual(ownership.UNKNOWN, self.state(None, _roster(), ev)[0])


if __name__ == "__main__":
    unittest.main()
