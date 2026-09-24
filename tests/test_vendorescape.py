#!/usr/bin/env python3
"""The vendor-dialog escape: what it presses, what it refuses, what it counts.

Every arm here drives the shipped `consider()` rather than a re-description of
it, and the dialogs are built from the producer's own option labels. The
delivery is the one thing doubled, because a test must never type into a real
pane — and the double is asserted on, so "nothing was delivered" is a measured
fact rather than the silence of a path that never ran.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# `seat` is the FACADE; seat_lifecycle is an implementation module
# behind it, and the tree refuses an impl import without it.
from helm import harness, orcaadopt, panetail, seat, vendorescape  # noqa: E402

VENDOR = "BLOCKED_ON_VENDOR_PROMPT"


def dialog(*options):
    return ("\n".join(["You've reached your Fable limit", ""] +
                      ["%d. %s" % (n, t) for n, t in enumerate(options, 1)])
            + "\n")


# The decisions are produced by the SHIPPED chooser over producer-shaped
# dialogs, never hand-written: an invented decision would test a contract
# nothing generates.
FREE = seat.vendor_escape_choice(
    dialog("Yes, buy usage credits", "Switch to Sonnet and continue",
           "No, keep my current model"))
SPEND_ONLY = seat.vendor_escape_choice(
    dialog("Yes, re-enable and continue", "No, keep my current model"))
FREE = {"digit": FREE[0], "kind": FREE[1], "seen": FREE[2]}
SPEND_ONLY = {"digit": SPEND_ONLY[0], "kind": SPEND_ONLY[1],
              "seen": SPEND_ONLY[2]}


# THE REAL MODAL, AND THE DEFECT THE EARLIER FIXTURE CARRIED. A vendor dialog
# OWNS THE SCREEN: numbered choices and NO COMPOSER. Substituting an idle
# composer pane here sent the delivery down the composer protocol — which is
# exactly the path that refuses a real dialog before typing — so those arms
# passed over a shape the producer never shows.
PANE = "\n".join(("You've reached your Fable limit",
                  "",
                  "  1. Yes, buy usage credits",
                  "  2. Switch to Sonnet and continue",
                  "  3. No, keep my current model"))
#: The same producer shape whose ONLY exit costs the owner money.
SPEND_PANE = "\n".join(("You've reached your Fable limit", "",
                        "  1. Yes, re-enable and continue",
                        "  2. No, keep my current model"))
#: What the pane reads AFTER the choice lands: the dialog is gone.
CLEARED = "\n".join(("─" * 40, "❯", "─" * 40, "  opus-5 | ~/dev/x",
                     "  ⏵⏵ bypass permissions on"))


class Ad(harness._CLIAdapter):
    """Inherits the REAL choose_in_modal and the REAL submit, so the arms below
    drive the shipped transaction rather than a second implementation that
    would agree with itself. Only the three metaharness calls are answered.

    `after` is what the NEXT read returns, which is how a dialog that repaints,
    reorders or closes between assessment and delivery is modelled — the case
    the whole re-derivation exists for.
    """

    name = "orca"

    def __init__(self, pane=None, cleared=None, dead=(), unreadable=False,
                 sticky=False, handles=("h1",), after=None, send_error=None):
        self.sent, self.reads = [], 0
        self.pane = PANE if pane is None else pane
        self.cleared = CLEARED if cleared is None else cleared
        self.after, self.chosen, self.sticky = after, False, sticky
        self.dead, self.unreadable = set(dead), unreadable
        self.handles, self.send_error = list(handles), send_error

    def list(self):
        return [{"handle": h, "status": "connected", "orphaned": False}
                for h in self.handles]

    def resolve_pane(self, key):
        return {} if key in self.dead else {"handle": "handle-of-" + key}

    def read(self, handle, limit=3000, timeout=60):
        self.reads += 1
        if self.unreadable:
            raise harness.HarnessError("pane unreadable")
        if self.chosen and not self.sticky:
            return self.cleared
        # THE SECOND READ IS THE DELIVERY-TIME READ. Liveness reads first and
        # `choose_in_modal` reads again just before typing, so a fixture that
        # sets `after` is a pane that CHANGED in that window.
        if self.after is not None and self.reads > 1:
            return self.after
        return self.pane

    def send(self, handle, text, enter=True):
        if self.send_error is not None:
            raise self.send_error
        # ENTER IS NEVER SPENT ON A MODAL, and the arms assert it: Enter is
        # what submits a human's draft, and the modal door exists precisely so
        # that keystroke is never needed. `enter` is recorded, not ignored.
        if text:
            self.sent.append((handle, text, enter))
            self.chosen = True
        return True


class Spy:
    """Stands in for `resumeturn.deliver` and MODELS ITS CONTRACT.

    The real delivery mints a GENERATION at the moment text is typed into the
    pane and hands it to `on_submit`; a refusal upstream of the keystroke never
    reaches that callback. A double that skipped the callback would report
    "nothing was typed" for every arm and quietly invert them, so this one
    fires it — and `typed=False` is how an arm asks for the refused-before-
    typing shape without reaching for the real authorizer.
    """

    class _Chose(object):
        """The one adapter method an `operation` calls, doubled faithfully.

        The operation `consider` builds is a CLOSURE over the placed-text
        capture, so a Spy that accepted `operation` and never called it would
        leave that capture untouched and every row would report an empty
        `sent=` — testing a delivery shape that no longer exists. Driving the
        real closure through this one method keeps the Spy a double of
        `deliver` and not of the thing deliver calls.
        """

        def __init__(self, placed):
            self.placed = placed

        def choose_in_modal(self, handle, intent, on_typed=None, settle=None):
            if on_typed is not None:
                on_typed(handle, self.placed)
            return harness.DELIVERED, "spy placed %r" % (self.placed,)

    def __init__(self, mode="resumed", proof="pane advanced", typed=True,
                 placed=None):
        self.calls = []
        self.mode = mode
        self.proof = proof
        self.typed = typed
        #: What the delivery reports it actually TYPED. Defaults to the text it
        #: was handed, which is what a delivery with no operation places.
        self.placed = placed

    def __call__(self, seat, text, session, adapter=None, pids=None,
                 on_submit=None, operation=None):
        # THE OPERATION IS PART OF THE CONTRACT NOW AND IS RECORDED, NOT
        # SWALLOWED. `consider` must hand the delivery a modal-choice operation
        # on every escape: without one the delivery would type the assessed
        # digit through the generic composer path, which is the exact fallback
        # the choice verb exists to make unreachable. A double that accepted
        # and ignored it would let that regression pass every arm here.
        self.calls.append({"seat": seat, "text": text, "session": session,
                           "pids": pids, "operation": operation})
        if self.typed and operation is not None:
            # A DELIVERY THAT TYPED SAYS WHAT IT TYPED. The real one reports it
            # through the operation's own callback, so this does too.
            placed = self.placed if self.placed is not None else text
            operation(self._Chose(placed), "h", None)
        if self.typed and on_submit is not None:
            on_submit(None, "h", self.mode, self.proof, len(self.calls))
        return self.mode, self.proof


class EscapeBase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-vendor-escape-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        p = mock.patch.object(vendorescape, "state_path",
                              return_value=os.path.join(self.tmp, "s.json"))
        p.start()
        self.addCleanup(p.stop)
        e = mock.patch.object(vendorescape, "enabled", return_value=True)
        e.start()
        self.addCleanup(e.stop)


class OnlyAVendorDialogIsEscapableTest(EscapeBase):

    def test_the_vendor_state_presses_and_the_others_do_not(self):
        """The gate and its must-hit in one arm: the same free dialog that DOES
        get pressed under the vendor state must not be pressed under any other,
        so the refusals below are about the state and not about a function that
        never presses anything."""
        spy = Spy()
        outcome, _line = vendorescape.consider("seat-a", VENDOR, FREE, handle="h1", session="sid-a", pids=[1], deliver=spy)
        self.assertEqual(outcome, vendorescape.ESCAPED)
        self.assertEqual(len(spy.calls), 1)

        # The SAME spy carries through every other state: its count must not
        # move, which is a stronger statement than a fresh spy staying empty.
        for state in ("RUNNING", "IDLE", "BLOCKED_ON_QUOTA", "BLOCKED_ON_HUMAN"):
            with self.subTest(state=state):
                outcome, line = vendorescape.consider("seat-b", state, FREE, handle="h2", session="sid-a", pids=[1], deliver=spy)
                self.assertEqual(outcome, vendorescape.HELD)
                self.assertEqual(len(spy.calls), 1,
                                 "a non-vendor state was pressed")
                self.assertIn(state, line)

    def test_a_quota_wall_is_never_pressed_even_carrying_dialog_text(self):
        """The dangerous overlap: a wall pane can print option-shaped lines.
        The classifier's verdict governs, and this module does not re-inspect
        the tail to talk itself into a press."""
        # UNCONDITIONAL POSITIVE CONTROL ON THE SAME SPY: one press through
        # this exact fixture, so every "the count did not move" below is a
        # statement about the refusal rather than about a dead path. A separate
        # seat name keeps the control out of the budget this arm measures.
        spy = Spy()
        vendorescape.consider("seat-control", VENDOR, FREE, handle="h", session="sid-a", pids=[1],
                              deliver=spy)
        self.assertEqual(len(spy.calls), 1)
        outcome, _line = vendorescape.consider("seat-a", "BLOCKED_ON_QUOTA", FREE, handle="h1", session="sid-a", pids=[1], deliver=spy)
        self.assertEqual(outcome, vendorescape.HELD)
        self.assertEqual(len(spy.calls), 1)


class OnlyTheFreeOptionIsPressedTest(EscapeBase):

    def test_a_spend_only_dialog_is_held_and_nothing_is_delivered(self):
        # UNCONDITIONAL POSITIVE CONTROL ON THE SAME SPY: one press through
        # this exact fixture, so every "the count did not move" below is a
        # statement about the refusal rather than about a dead path. A separate
        # seat name keeps the control out of the budget this arm measures.
        spy = Spy()
        vendorescape.consider("seat-control", VENDOR, FREE, handle="h", session="sid-a", pids=[1],
                              deliver=spy)
        self.assertEqual(len(spy.calls), 1)
        outcome, line = vendorescape.consider("seat-a", VENDOR, SPEND_ONLY, handle="h1", session="sid-a", pids=[1], deliver=spy)
        self.assertEqual(outcome, vendorescape.HELD)
        self.assertEqual(len(spy.calls), 1, "a purchase option was pressed")
        self.assertIn("money", line)

    def test_every_escape_hands_the_delivery_a_MODAL_CHOICE_operation(self):
        """THE FALLBACK THAT MUST STAY UNREACHABLE. `deliver` types its `text`
        through the composer path unless it is given an operation, so an escape
        that forgot one would send a bare digit into whatever the pane happens
        to be — the hazard the choice verb was built to remove. This asserts
        the operation is passed on the SAME call the other arms measure."""
        spy = Spy()
        vendorescape.consider("seat-a", VENDOR, FREE, handle="h",
                              session="sid-a", pids=[1], deliver=spy)
        self.assertTrue(spy.calls, "the delivery was never reached")
        self.assertIsNotNone(spy.calls[0]["operation"],
                             "the escape fell back to generic submission")
        # MUST-HIT: a HELD decision reaches no delivery at all, so the arm
        # above is measuring a real call and not an empty list.
        held = Spy()
        vendorescape.consider("seat-b", VENDOR, SPEND_ONLY, handle="h",
                              session="sid-a", pids=[1], deliver=held)
        self.assertEqual(held.calls, [])

    def test_the_digit_sent_is_the_producers_own_number(self):
        """The free option is SECOND in this dialog. A hardcoded "1" would have
        bought credits, so this asserts the exact text delivered."""
        spy = Spy()
        vendorescape.consider("seat-a", VENDOR, FREE, handle="h1",
                              session="sid-1", pids=[7], deliver=spy)
        self.assertEqual(spy.calls[0]["text"], "2")
        self.assertEqual(spy.calls[0]["seat"], "seat-a")
        self.assertEqual(spy.calls[0]["pids"], [7])


class TheDeliveryHasThreeOutcomesTest(EscapeBase):

    def test_each_delivery_mode_gets_its_own_outcome(self):
        # UNCONDITIONAL POSITIVE CONTROL ON THE SAME OBSERVABLE: this
        # fixture CAN deliver, so every "the spy was not called" below is a
        # statement about the refusal and not about a dead path. A separate
        # seat name keeps it out of the budget this arm measures.
        control = Spy()
        vendorescape.consider("seat-control", VENDOR, FREE, handle="h", session="sid-a", pids=[1],
                              deliver=control)
        self.assertEqual(len(control.calls), 1)
        cases = {"resumed": vendorescape.ESCAPED,
                 "manual": vendorescape.STRANDED,
                 "unverified": vendorescape.UNVERIFIED}
        checked = 0
        for mode, expected in cases.items():
            with self.subTest(mode=mode):
                outcome, line = vendorescape.consider(
                    "seat-%d" % checked, VENDOR, FREE, handle="h", session="sid-a", pids=[1],
                    deliver=Spy(mode=mode))
                self.assertEqual(outcome, expected)
                self.assertIn("2", line)
                checked += 1
        self.assertEqual(checked, 3)
        # Unconditional on the SAME roots the loop asserts: one more press
        # outside any branch, so `outcome` and `line` are provably produced.
        outcome, line = vendorescape.consider(
            "seat-outside", VENDOR, FREE, handle="h", session="sid-a", pids=[1], deliver=Spy())
        self.assertEqual(outcome, vendorescape.ESCAPED)
        self.assertIn("2", line)

    def test_stranded_is_not_spelled_as_a_weaker_unverified(self):
        """`manual` means the pane WAS read and the key was not submitted — the
        dialog is still up with a stray digit in front of it, which is worse
        than never pressing and must not read as an unproven success."""
        outcome, line = vendorescape.consider("seat-a", VENDOR, FREE, handle="h", session="sid-a", pids=[1], deliver=Spy(mode="manual"))
        self.assertEqual(outcome, vendorescape.STRANDED)
        self.assertIn("NOT", line)
        self.assertIn("human", line)


class TheBudgetIsItsOwnTest(EscapeBase):

    def _press(self, seat, at, spy=None):
        return vendorescape.consider(seat, VENDOR, FREE, handle="h",
                                     session="sid-a", pids=[1],
                                     now=at, deliver=spy or Spy())

    def test_an_unproven_press_still_spends_budget(self):
        """Otherwise an unreadable pane could be pressed without limit — the
        press is the thing being rationed, not the confirmation."""
        self._press("seat-a", 0.0, Spy(mode="unverified"))
        entry = vendorescape._peek("seat-a")
        self.assertEqual(len(entry["at"]), 1)

    def test_the_cap_holds_after_its_max(self):
        for i in range(vendorescape.MAX_ESCAPES):
            outcome, _ = self._press("seat-a", i * 4000.0)
            self.assertEqual(outcome, vendorescape.ESCAPED)
        spy = Spy()
        # Unconditional positive on this spy BEFORE the capped seat touches it.
        outcome, _ = self._press("seat-fresh", 0.0, spy)
        self.assertEqual(outcome, vendorescape.ESCAPED)
        self.assertEqual(len(spy.calls), 1)
        outcome, line = self._press("seat-a", vendorescape.MAX_ESCAPES * 4000.0,
                                    spy)
        self.assertEqual(outcome, vendorescape.HELD)
        self.assertEqual(len(spy.calls), 1)
        self.assertIn("capped", line)

    def test_a_relapse_holds_because_the_switch_did_not_hold(self):
        self._press("seat-a", 0.0)
        spy = Spy()
        outcome, _ = self._press("seat-fresh", 0.0, spy)
        self.assertEqual(outcome, vendorescape.ESCAPED)
        self.assertEqual(len(spy.calls), 1)
        outcome, line = self._press("seat-a", 300.0, spy)
        self.assertEqual(outcome, vendorescape.HELD)
        self.assertEqual(len(spy.calls), 1)
        self.assertIn("relapse", line)

    def test_one_seats_budget_does_not_ration_another(self):
        for i in range(vendorescape.MAX_ESCAPES):
            self._press("seat-a", i * 4000.0)
        outcome, _ = self._press("seat-b", vendorescape.MAX_ESCAPES * 4000.0)
        self.assertEqual(outcome, vendorescape.ESCAPED)


class TheKillSwitchIsTwoSwitchesTest(unittest.TestCase):

    def test_disarming_the_borrowed_injector_disarms_this_leg(self):
        """This leg types through resumeturn's transaction, so a fleet that has
        disarmed THAT has disarmed every helm keystroke into a pane. Honouring
        only the local knob would leave a disabled injector still injecting
        under another name."""
        with mock.patch.object(vendorescape.home, "env", return_value="1"), \
                mock.patch.object(vendorescape.resumeturn, "enabled",
                                  return_value=False):
            self.assertFalse(vendorescape.enabled())
        # MUST-HIT: with both armed it is enabled, so the False above is the
        # borrowed switch and not a function that always refuses.
        with mock.patch.object(vendorescape.home, "env", return_value="1"), \
                mock.patch.object(vendorescape.resumeturn, "enabled",
                                  return_value=True):
            self.assertTrue(vendorescape.enabled())

    def test_the_local_knob_disarms_it_alone(self):
        # UNCONDITIONAL POSITIVE CONTROL ON THE SAME OBSERVABLE: the same
        # doubles with the knob armed report True, so the False below is the
        # knob and not a predicate that always refuses.
        with mock.patch.object(vendorescape.home, "env", return_value="1"), \
                mock.patch.object(vendorescape.resumeturn, "enabled",
                                  return_value=True):
            self.assertTrue(vendorescape.enabled())
        with mock.patch.object(vendorescape.home, "env", return_value="off"), \
                mock.patch.object(vendorescape.resumeturn, "enabled",
                                  return_value=True):
            self.assertFalse(vendorescape.enabled())


class EveryOutcomeIsLoggedWithItsEvidenceTest(EscapeBase):

    def test_the_row_carries_pane_what_was_seen_and_what_was_sent(self):
        """The pane is a viewport, so this row is the only place the evidence
        survives. A row that says only "escaped" cannot be audited later."""
        _outcome, line = vendorescape.consider("seat-a", VENDOR, FREE, handle="pane-77", session="sid-a", pids=[1], deliver=Spy())
        self.assertIn("seat=seat-a", line)
        self.assertIn("pane=pane-77", line)
        self.assertIn("Switch to Sonnet and continue", line)
        self.assertIn("sent='2'", line)

    def test_a_refusal_row_still_names_what_the_dialog_offered(self):
        _outcome, line = vendorescape.consider("seat-a", VENDOR, SPEND_ONLY, handle="pane-77", session="sid-a", pids=[1], deliver=Spy())
        self.assertIn("Yes, re-enable and continue", line)
        self.assertIn("sent=''", line)


if __name__ == "__main__":
    unittest.main()


class TheSweepRidesAnExistingWakeTest(EscapeBase):
    """`sweep` is the live consumer: without it `consider` is a function nobody
    calls. These arms drive the shipped sweep over a fake liveness reader."""

    def _reader(self, rows):
        def read(name, repair=True):
            info = rows[name]
            if isinstance(info, Exception):
                raise info
            return info
        return read

    def test_only_a_parked_seat_produces_a_row(self):
        """A line per healthy seat is the noise that hides the real one."""
        rows = {"seat-a": {"state": "RUNNING"},
                "seat-b": {"state": "IDLE"},
                "seat-c": {"state": "BLOCKED_ON_VENDOR_PROMPT",
                           "escape": FREE, "handle": "h3",
                           "session": "sid-a", "pids": [1]}}
        spy = Spy()
        lines = vendorescape.sweep(
            ["seat-a", "seat-b", "seat-c"], apply=True,
            liveness=self._reader(rows),
            consider_fn=lambda *a, **k: vendorescape.consider(
                *a, deliver=spy, **k))
        self.assertEqual(len(lines), 1)
        self.assertIn("seat-c", lines[0])
        self.assertEqual(len(spy.calls), 1)

    def test_a_dry_run_states_the_decision_and_presses_nothing(self):
        rows = {"seat-a": {"state": "BLOCKED_ON_VENDOR_PROMPT",
                           "escape": FREE, "handle": "h",
                           "session": "sid-a", "pids": [1]}}
        spy = Spy()
        # UNCONDITIONAL POSITIVE CONTROL ON THE SAME SPY: an applied sweep over
        # the same fixture DOES press, so the empty list below is the dry run
        # and not a wiring that never delivers.
        vendorescape.sweep(
            ["seat-a"], apply=True,
            liveness=self._reader({"seat-a": dict(rows["seat-a"])}),
            consider_fn=lambda *a, **k: vendorescape.consider(
                *a, deliver=spy, **k))
        self.assertEqual(len(spy.calls), 1)
        spy.calls.clear()
        lines = vendorescape.sweep(
            ["seat-b"], apply=False,
            liveness=self._reader({"seat-b": dict(rows["seat-a"])}),
            consider_fn=lambda *a, **k: vendorescape.consider(
                *a, deliver=spy, **k))
        self.assertEqual(len(lines), 1)
        self.assertIn("DRY RUN", lines[0])
        self.assertEqual(spy.calls, [], "a dry run pressed a key")
        # The budget must be untouched too — a dry run that spent a press
        # would ration the real one that follows it.
        self.assertIsNone(vendorescape._peek("seat-b"))

    def test_an_unreadable_seat_is_named_and_does_not_stop_the_sweep(self):
        """A failed read is a fact about the INSTRUMENT. Reported as nothing it
        reads as "this seat is fine", and swallowing the sweep after it would
        leave every later seat unexamined with no sign that happened."""
        rows = {"seat-a": OSError("pane gone"),
                "seat-b": {"state": "BLOCKED_ON_VENDOR_PROMPT",
                           "escape": FREE, "handle": "h",
                           "session": "sid-a", "pids": [1]}}
        lines = vendorescape.sweep(
            ["seat-a", "seat-b"], apply=True, liveness=self._reader(rows),
            consider_fn=lambda *a, **k: vendorescape.consider(
                *a, deliver=Spy(), **k))
        self.assertEqual(len(lines), 2)
        self.assertIn("UNKNOWN", lines[0])
        self.assertIn("pane gone", lines[0])
        self.assertIn("ESCAPED", lines[1])

    def test_a_vendor_row_without_a_decision_is_refused_not_inferred(self):
        """The row carries `escape` on exactly this state, so its absence means
        the row was built by something that does not speak this contract."""
        spy = Spy()
        # UNCONDITIONAL POSITIVE CONTROL ON THE SAME SPY: the identical row
        # WITH a decision presses, so the empty list below is the missing
        # decision and not a sweep that never reaches the delivery.
        vendorescape.sweep(
            ["seat-z"], apply=True,
            liveness=self._reader({"seat-z": {
                "state": "BLOCKED_ON_VENDOR_PROMPT", "escape": FREE,
                "handle": "h", "session": "sid-a", "pids": [1]}}),
            consider_fn=lambda *a, **k: vendorescape.consider(
                *a, deliver=spy, **k))
        self.assertEqual(len(spy.calls), 1)
        spy.calls.clear()
        rows = {"seat-a": {"state": "BLOCKED_ON_VENDOR_PROMPT", "handle": "h"}}
        lines = vendorescape.sweep(
            ["seat-a"], apply=True, liveness=self._reader(rows),
            consider_fn=lambda *a, **k: vendorescape.consider(
                *a, deliver=spy, **k))
        self.assertEqual(spy.calls, [])
        self.assertIn("HELD", lines[0])
        self.assertIn("nothing here was measured", lines[0])


class TheRealProducerReachesTheRealAuthorizerTest(EscapeBase):
    """THE ARMS THAT REPLACE THE SPY, because a Spy in the delivery slot and an
    integer-pid fixture in the resolve slot bypass EXACTLY the two owners that
    decide whether a key is ever typed.

    Doubles sit only at the process boundary (`claude_processes`), the roster,
    and the metaharness adapter. Everything between — `resumeturn.deliver`,
    `orcaadopt.send_to_pane`, `authorized_handle` and its birth-stamp rung —
    runs for real, which is the only way a false STRANDED can be caught: it is
    produced by an owner refusing, and a double never refuses.
    """

    def _proc(self, pid, seat=None, pane_key="pk", sid="sid-a", start="1000"):
        return {"pid": pid, "start": start, "seat": seat, "pane_key": pane_key,
                "resume_sid": sid, "worktree_id": None}

    def _drive(self, pids, ad, procs=None, seat="seat-a"):
        """consider() through the REAL delivery, with only the boundaries doubled."""
        procs = procs if procs is not None else [
            self._proc(4242, seat=seat)]
        roster = {seat: {"session": "sid-a"}}
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=(procs, [])), \
                mock.patch("helm.seats.roster_checked",
                           return_value=(roster, False)), \
                mock.patch("helm.sessions.live_sids", return_value={}), \
                mock.patch.object(harness, "detect", return_value=ad):
            return vendorescape.consider(seat, VENDOR, FREE, handle="h",
                                         session="sid-a", pids=pids,
                                         adapter=ad)

    def test_a_stamped_identity_REACHES_THE_DOOR_and_the_door_refuses(self):
        """THE ADOPTED POSITIVE, and what it proves changed with the land cut.

        It never proved a keystroke was correct — it proved the AUTHORIZER let
        one through: a stamped identity resolves, the TOCTOU guard passes, and
        the delivery arrives at the pane's door holding an authorized handle.
        That is still exactly what it measures. The door now refuses there,
        because a tail cannot establish that a visible offer awaits input, so
        the arm's own assertion moves from "a key landed" to "the authorized
        delivery got as far as the door and stopped".

        The refusal text is asserted because it is the DIFFERENCE between this
        and every refusal above it: those stop at identity and name identity;
        this one reaches the dialog and names the dialog.
        """
        ad = Ad(PANE, CLEARED, sticky=True)
        outcome, line = self._drive([orcaadopt.ProcIdent(4242, "1000")], ad)
        self.assertEqual(ad.sent, [], "the land decision typed into a dialog")
        self.assertEqual(outcome, vendorescape.HELD)
        self.assertIn("showing a vendor dialog", line,
                      "the delivery did not reach the dialog: %s" % line)
        self.assertIn("2386", line)
        self.assertIsNone(vendorescape._peek("seat-a"),
                          "a refusal before typing spent budget")

    def test_with_the_decision_lifted_that_same_identity_reaches_the_pane(self):
        """THE OTHER HALF, so the arm above cannot pass on a build where the
        authorizer refuses for some unrelated reason: lift the land decision
        and the SAME stamped identity puts the producer's own digit on the
        pane. Without this, "nothing was typed" would be satisfied by a broken
        authorizer just as well as by the door."""
        ad = Ad(PANE, CLEARED)
        with mock.patch.object(harness, "ESCAPE_TYPES_NOTHING", False):
            outcome, line = self._drive([orcaadopt.ProcIdent(4242, "1000")], ad)
        self.assertTrue(ad.sent, "nothing reached the pane: %s" % line)
        self.assertEqual(ad.sent[-1][1], "2")
        self.assertIn(outcome, (vendorescape.ESCAPED, vendorescape.UNVERIFIED))
        self.assertIsNotNone(vendorescape._peek("seat-a"),
                             "a real press spent no budget")

    def test_an_unstamped_pid_refuses_before_typing_and_costs_nothing(self):
        """THE REGRESSION. `authorized_handle` refuses a pid with no birth
        stamp — a recycled slot cannot be told from the process this send
        decided about — and that refusal lands BEFORE anything is typed. The
        old code called it STRANDED and charged budget for it."""
        ad = Ad(PANE, CLEARED)
        outcome, line = self._drive([4242], ad)          # bare int, unstamped
        self.assertEqual(ad.sent, [], "a key was typed without authority")
        self.assertEqual(outcome, vendorescape.HELD)
        self.assertNotIn("STRANDED", line)
        self.assertIn("before anything was typed", line)
        self.assertIsNone(vendorescape._peek("seat-a"),
                          "a refused-before-typing row spent budget")

    def test_a_recycled_identity_refuses_before_typing(self):
        """Same rung, the other half: the stamp is PRESENT and does not match
        any live process, so the pid names a different incarnation."""
        ad = Ad(PANE, CLEARED)
        outcome, line = self._drive([orcaadopt.ProcIdent(4242, "9999")], ad)
        self.assertEqual(ad.sent, [])
        self.assertEqual(outcome, vendorescape.HELD)
        self.assertIsNone(vendorescape._peek("seat-a"))

    def test_an_unreadable_pane_refuses_before_typing(self):
        """A failed pre-read precedes the typing callback, so it is a
        no-type — not an unproven press."""
        ad = Ad(PANE, CLEARED, unreadable=True)
        outcome, line = self._drive([orcaadopt.ProcIdent(4242, "1000")], ad)
        self.assertEqual(ad.sent, [])
        self.assertEqual(outcome, vendorescape.HELD)
        self.assertIsNone(vendorescape._peek("seat-a"))

    def test_an_unresolvable_pane_refuses_before_typing(self):
        ad = Ad(PANE, CLEARED, dead=["pk"])
        outcome, _line = self._drive([orcaadopt.ProcIdent(4242, "1000")], ad)
        self.assertEqual(ad.sent, [])
        self.assertEqual(outcome, vendorescape.HELD)
        self.assertIsNone(vendorescape._peek("seat-a"))

    def test_a_genuinely_typed_but_unconfirmed_send_IS_stranded(self):
        """THE CONTROL THAT KEEPS STRANDED MEANINGFUL. Every arm above asserts
        the word is absent; this one proves it still appears when a key really
        was typed and its submission could not be confirmed — otherwise the
        cure could be 'never say STRANDED' and every arm would pass."""
        outcome, line = vendorescape.consider(
            "seat-typed", VENDOR, FREE, handle="h", session="sid-a",
            pids=[orcaadopt.ProcIdent(4242, "1000")],
            deliver=_typed_then_manual)
        self.assertEqual(outcome, vendorescape.STRANDED)
        self.assertIn("WAS typed", line)
        self.assertIsNotNone(vendorescape._peek("seat-typed"),
                             "a real keystroke spent no budget")


class TheRecognizedModalIsNamedAndNotPressedTest(unittest.TestCase):
    """LIVENESS TO DELIVERY, on the shape the producer really shows.

    The arms above supply `state`, `escape`, `handle`, `session` and `pids` by
    hand, which proves the authorizer refuses what it should but says nothing
    about whether the seat that is ACTUALLY parked can be freed: the row those
    values imitate is built by a pane read, and the pane it reads is a modal —
    numbered choices, no composer. `submit`'s pre-read exists to protect a
    human's half-typed draft and refused that shape before typing, so every
    recognised dialog was unescapable through the door the sweep uses.

    Here nothing between the pane and the keystroke is imitated. `sweep` reads
    liveness for real, liveness classifies the real tail and computes the real
    escape decision, `consider` gates on it and `resumeturn.deliver` carries it
    to `_CLIAdapter.submit`. Doubles sit only where the metaharness and the
    process table would be. Both delivery legs are driven, because they are two
    different transactions that happen to share one `submit`: a REGISTERED seat
    goes through the spawn register under its lifecycle lock, an ADOPTED seat
    through orcaadopt's pid-token guard, and a cure that reached only one of
    them would leave half the fleet parked.
    """

    SID = "sid-modal-a"
    #: A family that exists only here, so the registered arms can resolve a
    #: seat name without writing a LIVE seat's identity into the tree.
    FAMILY = "zzsynth"
    SEAT = FAMILY + "-1"
    OTHER = FAMILY + "-2"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-modal-escape-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._env = {k: os.environ.get(k) for k in
                     ("HELM_HOME", "HELM_SUBMIT_SETTLE_S")}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm-home")
        # `submit` settles between the keystroke and its read-back; a real
        # sleep per arm is suite poison and the settle is not what is measured.
        os.environ["HELM_SUBMIT_SETTLE_S"] = "0"

        def restore():
            for k, v in self._env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(restore)
        p = mock.patch.object(vendorescape, "state_path",
                              return_value=os.path.join(self.tmp, "s.json"))
        p.start()
        self.addCleanup(p.stop)
        e = mock.patch.object(vendorescape, "enabled", return_value=True)
        e.start()
        self.addCleanup(e.stop)
        # MUTATED IN PLACE, NOT REBOUND, AND THE DIFFERENCE DECIDES WHETHER
        # THIS WORKS AT ALL. `seat.FAMILIES`, `seat_lifecycle_sessions.FAMILIES`
        # and the catalog's are ONE object under three names, so
        # `patch.object(seat, "FAMILIES", ...)` rebinds the facade's name only
        # and the resolver `_seat_family` actually reads would never see it —
        # the registered arms would resolve nothing and fail for a reason that
        # has nothing to do with what they test. Adding to the shared set
        # reaches every alias, and the cleanup removes exactly what was added.
        seat.FAMILIES[self.FAMILY] = dict(seat.FAMILIES["codex"])
        self.addCleanup(seat.FAMILIES.pop, self.FAMILY, None)

    def _register(self, seat_name, handle="h1"):
        """Write the spawn register a REGISTERED seat's delivery re-proves."""
        family, err = seat._seat_family(seat_name)
        self.assertIsNone(err, err)
        d = seat._instance_dir(family, seat_name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "spawn.json"), "w", encoding="utf-8") as f:
            json.dump({"v": 1, "seat": seat_name, "harness": "orca",
                       "handle": handle, "session": self.SID,
                       "worktree": self.tmp}, f)

    def _sweep(self, seat_name, ad, procs=()):
        """The SHIPPED sweep, with only the metaharness and process table
        answered. `apply=True` because a dry run never reaches delivery."""
        with mock.patch.object(harness, "detect", return_value=ad), \
                mock.patch.object(orcaadopt, "claude_processes",
                                  return_value=(list(procs), [])), \
                mock.patch("helm.seats.roster_checked",
                           return_value=({seat_name: {"session": self.SID}},
                                         False)), \
                mock.patch("helm.sessions.live_sids", return_value={}):
            return vendorescape.sweep([seat_name], apply=True)

    #: LIFTS THE LAND DECISION, AND EVERY ARM THAT USES IT SAYS WHY. The
    #: shipped build types nothing into a dialog (harness.ESCAPE_TYPES_NOTHING)
    #: because a tail cannot prove an offer awaits input. The legs BELOW that
    #: refusal — positive clearance, the uncertain-send split — are kept
    #: reachable for task/2386, so they are exercised with the decision lifted.
    #: The arm directly below proves the SHIPPED DEFAULT independently, so this
    #: affordance cannot hide a build that types when it should not.
    def _armed(self):
        return mock.patch.object(harness, "ESCAPE_TYPES_NOTHING", False)

    # ---- what this build actually does -------------------------------------

    def test_the_SHIPPED_sweep_names_a_modal_and_types_NOTHING(self):
        """THE LAND DECISION, driven end to end with nothing patched.

        Liveness reads a real dialog, classifies it, computes a real escape
        decision, and the delivery reaches the pane through the authorized
        handle — and no key is sent. The seat stays parked and VISIBLE, which
        is the outcome this lane exists for; what it does not do is guess that
        an offer it can see is an offer awaiting input.
        """
        ad = Ad(sticky=True)
        self._register(self.SEAT)
        lines = self._sweep(self.SEAT, ad)
        self.assertEqual(ad.sent, [], "the shipped build typed into a dialog")
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("HELD", lines[0])
        self.assertIn("2386", lines[0], "the row does not say what unblocks it")
        self.assertIsNone(vendorescape._peek(self.SEAT),
                          "a refusal before typing spent budget")

    def test_an_ENDED_option_run_above_a_HUMAN_DRAFT_types_nothing(self):
        """THE CASE THAT KILLED THE KEYSTROKE, and it must stay dead.

        The producer's own option lines, then a composer carrying a half-typed
        human sentence. The classifier still calls this a vendor dialog and the
        chooser still returns its digit — that is the defect, and it is why the
        door refuses rather than why the scanner was tuned. A build that typed
        here would put a digit into someone's unsent message.
        """
        drafted = "\n".join((PANE, "─" * 30,
                              "❯ half a sentence the human was writing",
                              "─" * 30, "  opus-5 | ~/dev/x"))
        # THE FIXTURE IS THE HAZARD, so it is asserted rather than assumed:
        # this tail really does read as a live dialog offering a free option.
        self.assertEqual(seat._classify_pane_tail(drafted)[0], VENDOR)
        self.assertEqual(seat.vendor_escape_choice(drafted)[1],
                         seat.ESCAPE_CONTINUE)
        ad = Ad(pane=drafted, sticky=True)
        self._register(self.SEAT)
        lines = self._sweep(self.SEAT, ad)
        self.assertEqual(ad.sent, [], "typed into a human's draft")
        self.assertIn("HELD", lines[0])
        self.assertIsNone(vendorescape._peek(self.SEAT))

    def test_the_row_never_reports_a_digit_as_sent_when_none_was(self):
        """The ledger and the row report what the pane RECEIVED. Under a
        refusal that is nothing, and an operator reading "sent=" must never
        find an option there that no pane was given."""
        ad = Ad(sticky=True)
        self._register(self.SEAT)
        line = self._sweep(self.SEAT, ad)[0]
        self.assertIn("sent=''", line, "a digit was reported as sent: %s" % line)

    # ---- the legs task/2386 re-enables, exercised with the decision lifted --

    def test_with_the_decision_lifted_a_registered_seat_is_freed(self):
        """THE REGISTERED LEG still composes: spawn register, lifecycle lock,
        the authorized handle, the option derived at the keystroke."""
        ad = Ad()
        self._register(self.SEAT)
        with self._armed():
            lines = self._sweep(self.SEAT, ad)
        self.assertTrue(ad.sent, "no key reached the modal: %s" % lines)
        handle, digit, enter = ad.sent[-1]
        self.assertEqual((handle, digit), ("h1", "2"))
        self.assertIs(enter, False, "Enter was spent on a modal")
        self.assertIn("ESCAPED", lines[0])

    def test_with_the_decision_lifted_an_adopted_seat_is_freed(self):
        """THE ADOPTED LEG: no family, so liveness carries pids, and those
        route the delivery through orcaadopt's TOCTOU guard instead."""
        ad = Ad(handles=["handle-of-pk"])
        procs = [{"pid": 4242, "start": "1000", "seat": "seat-a",
                  "pane_key": "pk", "resume_sid": self.SID,
                  "worktree_id": None}]
        with self._armed():
            lines = self._sweep("seat-a", ad, procs=procs)
        self.assertTrue(ad.sent, "no key reached the modal: %s" % lines)
        self.assertEqual(ad.sent[-1][0], "handle-of-pk")
        self.assertIn("ESCAPED", lines[0])

    def test_with_the_decision_lifted_the_digit_is_the_CURRENT_option(self):
        """The re-derivation, and the row that reports it. A dialog that
        reordered between assessment and delivery must be pressed on its
        CURRENT numbering — and the ledger must say what was pressed, not what
        was assessed, or the operator row contradicts the proof beside it."""
        moved = "\n".join(("You've reached your Fable limit", "",
                            "  1. No, keep my current model",
                            "  2. Yes, buy usage credits",
                            "  3. Switch to Sonnet and continue"))
        ad = Ad(after=moved)
        self._register(self.SEAT)
        with self._armed():
            lines = self._sweep(self.SEAT, ad)
        self.assertEqual(ad.sent[-1][1], "3", "pressed the ASSESSED index")
        self.assertIn("sent='3'", lines[0],
                      "the row reported the assessed digit: %s" % lines[0])
        self.assertIn("ESCAPED", lines[0])

    def test_with_the_decision_lifted_an_EMPTY_readback_is_unverified(self):
        """`read` turns a failed read into "", so an empty tail is the one
        shape that must never read as "the dialog is gone"."""
        ad = Ad(cleared="")
        self._register(self.SEAT)
        with self._armed():
            lines = self._sweep(self.SEAT, ad)
        self.assertTrue(ad.sent)
        self.assertIn("UNVERIFIED", lines[0])
        self.assertNotIn("ESCAPED", lines[0])

    def test_with_the_decision_lifted_an_AMBIGUOUS_send_is_uncertain(self):
        """A timeout can follow input the pane already accepted, so it cannot
        be spelled "no key reached the pane" — and it spends budget."""
        ad = Ad(send_error=harness.HarnessError("orca terminal send: timeout"))
        self._register(self.SEAT)
        with self._armed():
            lines = self._sweep(self.SEAT, ad)
        self.assertEqual(ad.sent, [])
        self.assertIn("UNCERTAIN", lines[0])
        # AND THE ROW DOES NOT CLAIM NOTHING WAS PLACED. An empty `sent` reads
        # as "no key went out", which on an ambiguous send is as unfounded as
        # naming a digit: the pane may have taken it before the reply failed.
        self.assertIn("sent='UNKNOWN'", lines[0],
                      "an uncertain send rendered as nothing-sent: %s"
                      % lines[0])
        self.assertIsNotNone(vendorescape._peek(self.SEAT))
        # MUST-HIT: a REFUSAL on the same surface does say nothing was sent,
        # so the two states are distinguishable rather than both hedged.
        held = Ad(sticky=True)
        self._register(self.OTHER)
        self.assertIn("sent=''", self._sweep(self.OTHER, held)[0])

    def test_with_the_decision_lifted_a_STILL_DIALOGGED_pane_is_NOT_cleared(self):  # noqa: VACUOUS_ASSERTION — the same call carries two unconditional positives: the send count, and the UNVERIFIED spelling the row must render
        """THE FULL ACTUATION TRANSITION, not a classifier reading.

        The precondition matters and is why this arm exists here rather than
        over the parser alone: a walled-plus-dialog tail used as the BEFORE
        read is refused by ADMISSION before anything is sent, so it can never
        demonstrate a clearance defect. The BEFORE read is therefore an
        admissible vendor modal whose safe option matches the intent; only then
        does the AFTER read reach the clearance ladder at all.

        The AFTER read is walled AND still showing a dialog. Classifying it
        scalar-wise answers BLOCKED_ON_HUMAN, which is not MODAL_STATE — so a
        ladder keyed on "the state is no longer MODAL" would certify the dialog
        gone while it is up. Clearance asks the dialog about ITSELF instead.
        """
        after = "\n".join((
            "You've reached your usage limit",
            "Do you want to proceed?",
            "  1. Yes",
            "  2. No",
        ))
        # MUST-HIT: the hazard is that this tail does NOT read as a modal. If
        # it ever does, a scalar check would refuse anyway and this arm proves
        # nothing — so it fails HERE rather than passing quietly below.
        self.assertNotEqual(seat._classify_pane_tail(after)[0],
                            harness.MODAL_STATE,
                            "the after-tail now classifies as a modal, so this "
                            "fixture no longer exercises the clearance hazard")
        ad = Ad(cleared=after)
        self._register(self.SEAT)
        with self._armed():
            lines = self._sweep(self.SEAT, ad)
        self.assertEqual(len(ad.sent), 1, "the fixture never reached the send")
        # The two words are the WHOLE owner-visible difference, and they are
        # derived from the module's own constants rather than transcribed:
        # "DELIVERED" is a harness constant that never reaches this surface, so
        # asserting its absence here would have been vacuous — it cannot appear
        # in any row this renderer can emit. UNKNOWN is likewise the wrong
        # word: vendorescape spends it on a pane it could not read AT ALL,
        # which is a different failure wearing a similar hedge.
        self.assertNotIn("vendor-escape %s " % vendorescape.ESCAPED.upper(),
                         lines[0],
                         "a pane still showing a dialog was certified escaped: "
                         "%s" % lines[0])
        self.assertIn("vendor-escape %s " % vendorescape.UNVERIFIED.upper(),
                      lines[0], lines[0])

    def test_with_the_decision_lifted_a_QUIET_pane_IS_cleared(self):
        """MUST-HIT PAIR for the arm above: without it, that assertion passes
        on a build where NOTHING is ever certified delivered, which is a
        different defect wearing the same green."""
        ad = Ad(cleared="the dialog is gone\n\u276f ")
        self._register(self.OTHER)
        with self._armed():
            lines = self._sweep(self.OTHER, ad)
        self.assertEqual(len(ad.sent), 1, "the fixture never reached the send")
        self.assertIn("vendor-escape %s " % vendorescape.ESCAPED.upper(),
                      lines[0], lines[0])
        self.assertNotIn("vendor-escape %s " % vendorescape.UNVERIFIED.upper(),
                         lines[0], lines[0])

    def test_with_the_decision_lifted_a_WALL_BELOW_the_dialog_refuses(self):
        """POSITION ROUTES AND DOES NOT CONCLUDE, and this arm exists because
        the obvious rule is wrong in both directions. A quota wall in force
        plus a dialog is the NORMAL shape here, so "refuse while walled"
        refuses the feature. And a wall on its own line under the choices is
        not proof a newer screen arrived and took the keys — it is a placement
        the tail cannot attribute, indistinguishable from the wrapped-label
        case the arm below renders.

        So this pane refuses and the refusal is a MEASUREMENT: the wall
        spelling, where it sits, where the choices sit, and UNKNOWN for which
        of them holds the keys. Refusing is the same decision this build takes
        for any dialog it cannot vouch for, so no key is at risk either way.
        """
        after = "\n".join((
            "  1. Yes, buy usage credits",
            "  2. Switch to Sonnet and continue",
            "  3. No, keep my current model",
            "You've reached your Fable limit",
        ))
        ad = Ad(after=after)
        self._register(self.SEAT)
        with self._armed():
            lines = self._sweep(self.SEAT, ad)
        self.assertEqual(ad.sent, [], "a key was typed under a newer wall: %s"
                         % lines[0])
        self.assertIn("UNKNOWN", lines[0], lines[0])
        self.assertIn("lines 0 to 2", lines[0],
                      "the row does not say where the choices were: %s"
                      % lines[0])
        # SAME-CALL POSITIVE CONTROL, and it guards the gate rather than the
        # outcome: an empty `sent` is also what a refusal ANYWHERE ABOVE this
        # door produces, so without proof the delivery-time read happened this
        # arm would go green on a build that never reached admission at all.
        self.assertGreaterEqual(ad.reads, 2,
                                "the pane was never re-read, so this refusal "
                                "came from an earlier gate: %s" % lines[0])

    def test_with_the_decision_lifted_the_WALL_row_quotes_the_PANE_not_the_recognizer(self):
        """THE ROW REPORTS WHAT THE PANE SAID, and a recognizer is not that.

        `Wall` carries two different strings — `spelling`, the pattern out of
        the recognizer table that matched, and `line`, the pane line the match
        was found in. Printing the first one under the words "carries
        quota-wall text" hands an operator regex syntax as if it were the
        screen, and it DROPS the only part they can act on: the vendor's own
        wording. The wrapped-label arm below cannot see this, because the
        pattern that matches its fixture happens to be the literal
        `usage balance exhausted` and the two strings read alike.

        This fixture is the distinguishing one: the pattern behind
        `You've reached your Fable limit` is NONLITERAL, so a row built from
        the pattern says "you(?:'|’)?ve reached your \\w+ limit\\b" and
        never says Fable. So the assertions are a pair — the vendor's words
        are present, and no recognizer metacharacter is.
        """
        after = "\n".join((
            "  1. Yes, buy usage credits",
            "  2. Switch to Sonnet and continue",
            "  3. No, keep my current model",
            "You've reached your Fable limit",
        ))
        # MUST-HIT ON THE FIXTURE: this arm is worth nothing unless the wall
        # the real recognizer finds here is one whose pattern DIFFERS from the
        # text it matched. A table edit that turns this spelling literal must
        # redden here rather than leave the arm green over a row that could no
        # longer tell the two strings apart.
        wall = panetail.wall_standing(panetail.parse(after))
        self.assertEqual(wall.state, panetail.IN_FORCE, wall.why)
        self.assertNotEqual(wall.occurrence.detail.spelling.lower(),
                            wall.occurrence.detail.line.strip().lower(),
                            "the recognizer for this fixture is now a literal "
                            "equal to the pane text, so this arm can no longer "
                            "tell a pattern from an observation")
        ad = Ad(after=after)
        self._register(self.SEAT)
        with self._armed():
            lines = self._sweep(self.SEAT, ad)
        self.assertIn("Fable limit", lines[0],
                      "the row does not carry the words the pane showed: %s"
                      % lines[0])
        for metacharacter in ("(?:", "\\w", "\\b"):
            self.assertNotIn(metacharacter, lines[0],
                             "the row printed recognizer syntax %r as the "
                             "text observed on the pane: %s"
                             % (metacharacter, lines[0]))

    def test_with_the_decision_lifted_a_WALL_ABOVE_the_dialog_still_admits(self):
        """MUST-HIT PAIR for the arm above, and it guards a defect I shipped
        into this file once: a wall-in-force check with no position test went
        green on its own arm while refusing nine others, because the wall
        banner that RAISES a vendor dialog is rendered above it. If this ever
        reddens, the wall rule has eaten the feature it was meant to protect.
        """
        ad = Ad(after=PANE)
        self._register(self.SEAT)
        with self._armed():
            lines = self._sweep(self.SEAT, ad)
        self.assertEqual(len(ad.sent), 1,
                         "the wall banner above its own dialog blocked the "
                         "escape: %s" % lines[0])

    def test_with_the_decision_lifted_a_WALL_INSIDE_an_option_label_still_admits(self):  # noqa: VACUOUS_ASSERTION — the must-hit that the parser really overlaps the two events, and the digit the door must still offer, ride the same fixture
        """THE OVERLAP THE TWO ARMS ABOVE CANNOT SEE, because each of them
        renders the wall on its OWN line and the defect lives on ONE line.

        The producer spells the reason inside the option it is offering:
        `1. Yes, buy usage credits (usage balance exhausted)`. The wall
        occurrence then sits at a LATER COLUMN of the run's FIRST line, so a
        comparison against the run's start position reads it as a wall drawn
        after the dialog and refuses the pane for a screen that never arrived.
        A wall cannot arrive after a run it is rendered inside; the frontier is
        the run's LAST option row.
        """
        after = "\n".join((
            "  1. Yes, buy usage credits (usage balance exhausted)",
            "  2. Switch to Sonnet and continue",
            "  3. No, keep my current model",
        ))
        # MUST-HIT ON THE FIXTURE, not on the door: this arm is worth nothing
        # unless the real parser genuinely places a wall-in-force INSIDE the
        # run's span. A vendor rewording or a pattern-table change that stops
        # the overlap happening must fail HERE, loudly, rather than leave the
        # arm passing over a pane with no wall in it at all.
        parsed = panetail.parse(after)
        run = panetail.modal_standing(parsed).occurrence.detail
        wall = panetail.wall_standing(parsed)
        self.assertEqual(wall.state, panetail.IN_FORCE, wall.why)
        self.assertGreater(wall.occurrence.pos, run.start,
                           "the wall no longer out-positions the run's start, "
                           "so this fixture no longer reproduces the overlap")
        self.assertLessEqual(wall.occurrence.pos.line, run.end.line,
                             "the wall is no longer rendered inside the run's "
                             "span, so this is the separate-line case already "
                             "covered above")
        ad = Ad(after=after)
        self._register(self.SEAT)
        with self._armed():
            lines = self._sweep(self.SEAT, ad)
        self.assertNotIn("holds the keys is UNKNOWN", lines[0],
                         "a wall spelled inside option 1's own label left the "
                         "dialog's ownership unmeasured: %s" % lines[0])
        self.assertEqual(len(ad.sent), 1,
                         "the dialog's own reason text blocked its escape: %s"
                         % lines[0])

    def test_with_the_decision_lifted_a_WRAPPED_final_option_is_UNKNOWN_not_a_wall_below(self):  # noqa: VACUOUS_ASSERTION — the fixture must-hits (where the parser closes the run, where the wall lands, that it is in force) and the two facts the row must carry ride one call, because they are all about the same single parse
        """THE LINE UNDER THE LAST NUMBERED ROW SETTLES NOTHING, which is why
        the wall rule reports a measurement here instead of a chronology.

        A run ends at its LAST NUMBERED ROW, because a line that does not match
        `OPTION` is what closes it. So the producer wrapping its final label
        over two rows puts wall text on the line AFTER the run:

            3. No, keep my current model (reason:
               usage balance exhausted)

        The wall is then below the run by position and inside the dialog by
        authorship, and no pane tail separates those two readings — a newer
        screen and a continuation line render identically. Reporting either one
        as fact is a guess, so the door reports the measurement, names the
        chronology UNKNOWN, and types nothing. The pane-shape assertions below
        are the fixture's must-hits: this arm is worth nothing unless the real
        parser really closes the run above the wall.
        """
        after = "\n".join((
            "  1. Yes, buy usage credits",
            "  2. Switch to Sonnet and continue",
            "  3. No, keep my current model (reason:",
            "     usage balance exhausted)",
        ))
        parsed = panetail.parse(after)
        run = panetail.modal_standing(parsed).occurrence.detail
        wall = panetail.wall_standing(parsed)
        self.assertEqual(wall.state, panetail.IN_FORCE, wall.why)
        self.assertEqual(run.end.line, 2,
                         "the run no longer closes at its last NUMBERED row, "
                         "so this fixture no longer reproduces the wrap")
        self.assertEqual(wall.occurrence.pos.line, 3,
                         "the wall is no longer on the continuation line, so "
                         "this fixture no longer reproduces the wrap")
        ad = Ad(after=after)
        self._register(self.SEAT)
        with self._armed():
            lines = self._sweep(self.SEAT, ad)
        # THE UNSUPPORTED SENTENCE, NAMED SO IT CANNOT COME BACK. "drew a
        # quota wall BELOW the dialog ... the choices no longer own input" is
        # two claims — which screen arrived first, and who holds the keys — and
        # a wall one line under the choices is evidence for neither.
        self.assertNotIn("BELOW the dialog", lines[0], lines[0])
        self.assertIn("holds the keys is UNKNOWN", lines[0],
                      "the row does not say the chronology was unmeasured: %s"
                      % lines[0])
        self.assertIn("on line 3", lines[0],
                      "the row does not say where the wall text was: %s"
                      % lines[0])
        self.assertEqual(ad.sent, [],
                         "a key was typed into a dialog whose ownership is "
                         "unmeasured: %s" % lines[0])
        # SAME-CALL POSITIVE CONTROL: an empty `sent` is also what any refusal
        # ABOVE this door produces, so without proof the delivery-time read
        # happened this arm would pass on a build that never reached admission.
        self.assertGreaterEqual(ad.reads, 2,
                                "the pane was never re-read, so this refusal "
                                "came from an earlier gate: %s" % lines[0])

    def test_with_the_decision_lifted_a_dialog_ABOVE_A_HUMAN_DRAFT_types_nothing(self):  # noqa: VACUOUS_ASSERTION — three unconditional positives ride the same call: the must-hit that the shipped chooser DOES hand back a digit here, the refusal text the row must render, and the delivery-time read count
        """THE HAZARD THE WHOLE LANE EXISTS FOR, AND IT SURVIVED THE MENU CURE.

        A qualifying vendor dialog with a human's half-typed draft rendered
        BELOW it is not a dialog awaiting input — the keystrokes went to the
        composer. The shipped readers cannot see that: `_prompt_options` has
        no composer notion and returns all three choices, the chooser turns
        that into a digit, and `_classify_pane_tail` calls the pane a live
        vendor prompt. Typing here types into the human's sentence.

        This replaced an arm pointed at the stale-menu fall-through, which a
        landed cure removed from the shipped helper. Pinning a defect someone
        else fixed measures nothing; the composer case is the difference that
        is still real, and it is the one the actuator's own flag names as its
        reason for refusing to type at all.
        """
        after = "\n".join((
            "You've reached your Fable limit",
            "  1. Yes, buy usage credits",
            "  2. Switch to Sonnet and continue",
            "  3. No, keep my current model",
            "\u276f half a sentence the human was writing",
        ))
        # MUST-HIT: the shipped chooser STILL hands back a digit for this tail.
        # The moment it stops, this arm is comparing against a producer that
        # changed and must fail HERE rather than pass quietly below.
        self.assertEqual(seat.vendor_escape_choice(after)[0], "2",
                         "the shipped chooser no longer offers a digit above a "
                         "human draft, so this fixture no longer exercises the "
                         "hazard")
        ad = Ad(after=after)
        self._register(self.SEAT)
        with self._armed():
            lines = self._sweep(self.SEAT, ad)
        self.assertEqual(ad.sent, [],
                         "a digit was typed into a pane whose composer holds a "
                         "human's draft: %s" % lines[0])
        # THE REASON, NOT THE VERDICT. Three different facts can make this
        # door refuse, and only one of them is the hazard this arm names — so
        # asserting the refusal alone would pass on a build that refused for
        # the wrong reason entirely.
        self.assertIn("a composer holding text is rendered below the run",
                      lines[0], lines[0])
        # SAME-CALL POSITIVE CONTROL ON THE GATE, not the outcome: an empty
        # `sent` is also what a refusal anywhere above this door produces.
        self.assertGreaterEqual(ad.reads, 2,
                                "the pane was never re-read, so this refusal "
                                "came from an earlier gate: %s" % lines[0])

    def test_with_the_decision_lifted_a_PROVEN_absent_send_costs_nothing(self):
        """request_absent proves the request never left the box: a real
        no-key, which must not spend budget."""
        ad = Ad(send_error=harness.HarnessError("orca: no such file",
                                                request_absent=True))
        self._register(self.SEAT)
        with self._armed():
            lines = self._sweep(self.SEAT, ad)
        self.assertEqual(ad.sent, [])
        self.assertIn("HELD", lines[0])
        self.assertIsNone(vendorescape._peek(self.SEAT))

    # ---- the safety that holds in BOTH builds ------------------------------

    def test_a_composer_holding_a_human_draft_is_refused_either_way(self):
        """Not a modal at all, so it never reaches the door — with the land
        decision in place or lifted."""
        draft = "\n".join(("─" * 30, "❯ half a sentence the human was writing",
                            "─" * 30, "  opus-5 | ~/dev/x"))
        for lifted in (False, True):
            with self.subTest(lifted=lifted):
                ad = Ad(pane=draft, sticky=True)
                self._register(self.SEAT)
                ctx = self._armed() if lifted else _nothing()
                with ctx:
                    lines = self._sweep(self.SEAT, ad)
                self.assertEqual(ad.sent, [], "typed over a human's draft")
                self.assertEqual(lines, [], lines)

    def test_submit_still_REFUSES_a_modal_so_ordinary_text_cannot_reach_one(self):
        """`seat` sends boot and re-arm prose through `submit`; submit must
        refuse a modal exactly as it did before this lane existed."""
        ad = Ad(sticky=True)
        state, proof = ad.submit("h1", "please re-arm your beacon")
        self.assertEqual(state, harness.UNKNOWN)
        self.assertEqual(ad.sent, [], "ordinary text reached a vendor dialog")
        self.assertIn("composer", str(proof))
        ok = Ad(pane=CLEARED, sticky=True)
        ok.submit("h1", "hello")
        self.assertTrue(ok.sent, "submit typed nothing into a clean composer")

    def test_a_spend_only_modal_is_refused_before_the_land_decision(self):
        """The whitelist is upstream of the door, so a dialog whose only exit
        costs money is refused for THAT reason and not as a side effect."""
        ad = Ad(pane=SPEND_PANE, sticky=True)
        self._register(self.SEAT)
        with self._armed():
            lines = self._sweep(self.SEAT, ad)
        self.assertEqual(ad.sent, [], "spent the owner's money")
        self.assertIn("HELD", lines[0])
        self.assertIn("money", lines[0])
        self.assertIsNone(vendorescape._peek(self.SEAT))

    def test_an_unreadable_pane_never_reaches_the_door(self):
        ad = Ad(unreadable=True)
        self._register(self.SEAT)
        lines = self._sweep(self.SEAT, ad)
        self.assertEqual(ad.sent, [])
        self.assertEqual(lines, [], lines)
        self.assertIsNone(vendorescape._peek(self.SEAT))


import contextlib


@contextlib.contextmanager
def _nothing():
    yield



def _typed_then_manual(name, text, session, adapter=None, pids=None,
                       on_submit=None, operation=None):
    """A delivery that TYPED and could not confirm: the owner mints a
    generation and then reports `manual`. Only this shape earns STRANDED."""
    if on_submit is not None:
        on_submit(None, "h", "NOT_DELIVERED", "composer still holds it", 7)
    return "manual", "composer still holds it"


if __name__ == "__main__":
    unittest.main()
