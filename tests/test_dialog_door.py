#!/usr/bin/env python3
"""task/3209 — ONE door answers a vendor dialog, and only one PROVEN live.

`harness._CLIAdapter.answer_dialog` is the one keystroke into a dialog. It asks
three questions at the act and presses the named option's DIGIT (never Enter)
only when all three hold:

  WITNESS  the caller's proof that THIS dialog awaits input NOW: the vendor's
           presence record saying so, or the caller's own act having opened it;
  SHAPE    after that witness, one final fresh pane read finds the kind's dialog
           STANDING at the bottom of the screen, nothing (no composer, no draft)
           below it;
  KEY      the named option is offered, and the pointer sits on it or on the
           row the dialog opens on (nobody has moved it).

Every kind gets the same five arms: answered when live, and nothing pressed
when the dialog is stale in scrollback, when a draft sits below it, when it is
another call's dialog, and when the pointer is not on the intended option.
The incident that parked the vendor escape (e490c3b41ef) is the named red.

Every screen is a pane double's read; nothing here touches a live pane.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import harness, panetail  # noqa: E402
from tests.test_panetail import ORCA_READ, exit_dialog_focused_on  # noqa: E402

#: The exit-confirm dialog as orca read it off a live pane (MEASURED; see
#: tests/fixtures/exit-confirm-dialog-orca-read.json).
EXIT_LIVE = "\n".join(ORCA_READ["tail"])

#: The usage-limit menu, TRACED rather than measured: Claude Code 2.1.283
#: draws it (`fable_overage_consent_prompt`) with the same Dialog component
#: that draws the measured exit dialog — a title row, the choices with the
#: pointer, and the same footer as the bottom row.
VENDOR_LIVE = "\n".join((
    "✻ Worked for 12m · done 3:02 AM",
    "▔" * 60,
    "   You've reached your Fable limit",
    "   ❯ 1. Yes, buy usage credits",
    "     2. Switch to Sonnet and continue",
    "     3. No, keep my current model",
    "   Enter to confirm · Esc to cancel"))

#: What a pane shows once the dialog was answered and the composer is back.
COMPOSER = "\n".join(("─" * 40, "❯", "─" * 40,
                      "  opus-5 | ~/dev/example/repo"))
#: A human's half-typed sentence in that composer.
DRAFT = "\n".join(("─" * 40,
                   "❯ half a sentence the human was writing",
                   "─" * 40, "  opus-5 | ~/dev/example/repo"))
#: The exited session's shell.
SHELL = "\n".join(("Resume this session with:",
                   "claude --resume 11111111-2222-4333-8444-555555555555",
                   "~/dev/example/repo $"))

#: THE INCIDENT THAT PARKED THE ESCAPE, planted exactly: the vendor's own option lines,
#: already answered, then a composer carrying a human's half-typed sentence
#: (the shape e490c3b41ef stopped the escape on, and test_vendorescape's
#: `drafted`). Every free-text scanner still calls it a live vendor dialog.
INCIDENT = "\n".join(("You've reached your Fable limit", "",
                      "  1. Yes, buy usage credits",
                      "  2. Switch to Sonnet and continue",
                      "  3. No, keep my current model",
                      "─" * 30,
                      "❯ half a sentence the human was writing",
                      "─" * 30, "  opus-5 | ~/dev/x"))


def _pointer_on(screen, n):
    """`screen` with Claude Code's pointer moved to option `n`."""
    return exit_dialog_focused_on(n, screen=screen)


class DoorPane(harness._CLIAdapter):
    """A pane that shows `screen` and RECORDS every key with its send bound.
    The digit in `closes_on` answers the dialog and leaves `after` on screen;
    any other key leaves the screen as it is. Inherits the real door."""

    name = "orca"

    def __init__(self, screen, closes_on=None, after=COMPOSER):
        self.screen, self.closes_on, self.after = screen, closes_on, after
        self.keys, self.timeouts = [], []

    def read(self, handle, limit=3000, timeout=60):
        return self.screen

    def send(self, handle, text, enter=True, timeout=60):
        self.keys.append((text, enter))
        self.timeouts.append(timeout)
        if text == self.closes_on and not enter:
            self.screen = self.after


def _witness(ok, why="planted witness"):
    return lambda: (ok, why)


def _vendor(kind=None):
    """The usage-limit kind with its unproven reason LIFTED, so an arm can
    drive the door's three questions on it. The SHIPPED kind stays refused;
    `ShippedTableTest` asserts that independently."""
    return (kind or panetail.VENDOR_KIND)._replace(unproven=None)


FREE = (2, "Switch to Sonnet and continue")


class _DoorArms(object):
    """The five arms every answerable kind owes. A subclass names the kind,
    its live screen, the option to press and the pointer-moved screen."""

    kind = live = option = moved = None

    def _answer(self, screen, awaiting=True, kind=None, option=None):
        ad = DoorPane(screen, closes_on=str((option or self.option)[0]))
        with mock.patch.dict(os.environ, {"HELM_SUBMIT_SETTLE_S": "0"}), \
                mock.patch.object(harness, "SUBMIT_VERIFY_INTERVAL_S", 0):
            state, detail = ad.answer_dialog(
                "handle", kind or self.kind, option or self.option,
                _witness(awaiting))
        return ad, state, detail

    def test_answered_when_live_with_the_options_digit_and_no_enter(self):
        ad, state, detail = self._answer(self.live)
        self.assertEqual(state, harness.DELIVERED, detail)
        self.assertEqual(ad.keys, [(str(self.option[0]), False)],
                         "the door pressed something other than the named "
                         "option's digit, once, with no Enter")
        self.assertEqual(ad.timeouts, [harness.act_send_s()],
                         "the keystroke did not carry the act send bound")
        self.assertIn("closed", detail)

    def test_nothing_pressed_when_the_dialog_is_stale_in_scrollback(self):  # noqa: VACUOUS_ASSERTION — no key IS the contract; the same call's detail must say nothing was typed, the witness here says live, and the live arm above presses on the same dialog rows
        ad, state, detail = self._answer(self.live + "\n" + COMPOSER)
        self.assertEqual(ad.keys, [])
        self.assertEqual(state, harness.NOT_DELIVERED)
        self.assertIn("nothing was typed", detail)

    def test_nothing_pressed_when_the_dialog_changes_during_the_witness(self):  # noqa: VACUOUS_ASSERTION — no key IS the contract; the witness returns True, so only a pane read after it can see that the dialog closed before the key
        """The presence read is I/O, not an atomic guard on the pane. If the
        dialog closes while that witness is being read, its earlier screen
        cannot authorize a digit into the composer that replaced it."""
        ad = DoorPane(self.live, closes_on=str(self.option[0]))

        def changed():
            ad.screen = DRAFT
            return True, "the earlier presence record still said waiting"

        with mock.patch.dict(os.environ, {"HELM_SUBMIT_SETTLE_S": "0"}), \
                mock.patch.object(harness, "SUBMIT_VERIFY_INTERVAL_S", 0):
            state, detail = ad.answer_dialog(
                "handle", self.kind, self.option, changed)
        self.assertEqual(ad.keys, [],
                         "the stale pane read typed a digit into a human draft")
        self.assertEqual(state, harness.NOT_DELIVERED)
        self.assertIn("nothing was typed", detail)

    def test_nothing_pressed_when_a_draft_sits_below_it(self):  # noqa: VACUOUS_ASSERTION — no key IS the contract; the detail must say nothing was typed, and the witness here LIES (says live) so only the shape can refuse
        ad, state, detail = self._answer(self.live + "\n" + DRAFT)
        self.assertEqual(ad.keys, [])
        self.assertEqual(state, harness.NOT_DELIVERED)
        self.assertIn("nothing was typed", detail)

    def test_nothing_pressed_when_it_is_another_calls_dialog(self):  # noqa: VACUOUS_ASSERTION — no key IS the contract; the detail must carry the witness's own refusal
        ad, state, detail = self._answer(self.live, awaiting=False)
        self.assertEqual(ad.keys, [])
        self.assertEqual(state, harness.NOT_DELIVERED)
        self.assertIn("planted witness", detail)

    def test_nothing_pressed_when_the_pointer_is_not_on_the_intended_option(self):  # noqa: VACUOUS_ASSERTION — no key IS the contract; the detail must name where the pointer sits
        ad, state, detail = self._answer(self.moved)
        self.assertEqual(ad.keys, [])
        self.assertEqual(state, harness.NOT_DELIVERED)
        self.assertIn("pointer", detail)


class ExitKindTest(_DoorArms, unittest.TestCase):
    kind = panetail.EXIT_KIND
    live = EXIT_LIVE
    option = panetail.EXIT_CONFIRM
    moved = _pointer_on(EXIT_LIVE, 3)

    def test_the_moved_fixture_really_moves_the_pointer(self):
        self.assertEqual(panetail.exit_dialog(self.moved).focus, (3, "Stay"))
        self.assertEqual(panetail.exit_dialog(self.live).focus,
                         panetail.EXIT_CONFIRM)


class VendorKindTest(_DoorArms, unittest.TestCase):
    """The usage-limit menu with its unproven reason lifted: what the door
    does on this kind once its shape is measured."""
    kind = _vendor()
    live = VENDOR_LIVE
    option = FREE
    moved = _pointer_on(VENDOR_LIVE, 3)

    def test_the_traced_screen_stands_with_the_pointer_on_its_first_row(self):
        got = panetail.vendor_dialog(VENDOR_LIVE)
        self.assertTrue(got.standing, got.why)
        self.assertEqual(got.focus, (1, "Yes, buy usage credits"))
        self.assertIn(FREE, got.options)

    def test_the_incident_that_parked_the_escape_presses_nothing_with_a_lying_witness(self):  # noqa: VACUOUS_ASSERTION — THE NAMED RED: no key IS the contract; the must-hit below proves the shipped chooser still hands back a digit for this exact screen, so the refusal is the door's and not a quiet producer
        """THE NAMED RED. The screen that parked the vendor escape: the menu
        already answered in scrollback, a human's half-typed sentence below
        it. The shipped chooser STILL offers a digit here; the witness is
        planted to LIE (it says the dialog awaits input) and the kind is
        lifted, so the only thing standing between a digit and the human's
        sentence is the door's SHAPE question."""
        from helm import seat
        self.assertEqual(seat.vendor_escape_choice(INCIDENT)[0], "2",
                         "the shipped chooser no longer offers a digit here, "
                         "so this fixture no longer exercises the incident")
        ad, state, detail = self._answer(INCIDENT, awaiting=True)
        self.assertEqual(ad.keys, [], "the door typed into the human's draft")
        self.assertEqual(state, harness.NOT_DELIVERED)
        self.assertIn("nothing was typed", detail)
        # AND WITH THE REAL WITNESS: a human typing a draft is a session whose
        # presence record reads idle, not waiting.
        ad, state, detail = self._answer(INCIDENT, awaiting=False)
        self.assertEqual(ad.keys, [])


class ShippedTableTest(unittest.TestCase):
    """The door's table is the rule, stated once."""

    def test_the_table_is_the_rule(self):
        """The old global flag is gone (the tree's retired-name rung refuses
        its spelling anywhere), and this table replaces it: one row per kind
        the door may answer, each proven or refused by name."""
        names = [k.name for k in panetail.DIALOG_KINDS]
        self.assertEqual(names, ["exit-confirm", "vendor-limit"])
        self.assertIsNone(panetail.EXIT_KIND.unproven)
        self.assertTrue(panetail.VENDOR_KIND.unproven)

    def test_the_shipped_vendor_kind_is_refused_by_name_before_any_read(self):  # noqa: VACUOUS_ASSERTION — no key IS the contract; the detail must carry the kind's own enabling recipe, and the lifted twin in VendorKindTest presses on the same screen
        ad = DoorPane(VENDOR_LIVE, closes_on="2")
        state, detail = ad.answer_dialog("handle", panetail.VENDOR_KIND, FREE,
                                         _witness(True))
        self.assertEqual(ad.keys, [])
        self.assertEqual(state, harness.NOT_DELIVERED)
        self.assertIn("vendor-limit", detail)
        self.assertIn("live pane", detail)

    def test_every_recognised_dialog_not_in_the_door_says_why(self):  # noqa: VACUOUS_ASSERTION — the loop is over a fixed four-name list, so every assertion in it runs
        rows = dict(panetail.NOT_ANSWERED)
        for name in ("permission prompt", "plan-execution prompt",
                     "trust prompt", "bypass-permissions warning"):
            self.assertIn(name, rows)
            self.assertGreater(len(rows[name]), 40, name)


class PresenceWitnessTest(unittest.TestCase):
    """The vendor's own presence record as the WITNESS: <config>/sessions/
    <pid>.json, read through helm's bracketed reader."""

    PID, START = 4242, "88401"
    SID = "11111111-2222-4333-8444-555555555555"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-dialog-door-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "claude-config")
        os.makedirs(os.path.join(self.root, "sessions"), mode=0o700)
        proc = os.path.join(self.tmp, "proc", str(self.PID))
        os.makedirs(proc)
        with open(os.path.join(proc, "environ"), "wb") as f:
            f.write(("CLAUDE_CONFIG_DIR=%s\0HOME=%s\0"
                     % (self.root, self.tmp)).encode())
        patch = mock.patch.dict(os.environ,
                                {"HELM_PROC": os.path.join(self.tmp, "proc")})
        patch.start()
        self.addCleanup(patch.stop)

    def _record(self, **fields):
        rec = {"pid": self.PID, "procStart": self.START, "sessionId": self.SID,
               "statusUpdatedAt": 1790383066237}
        rec.update(fields)
        path = os.path.join(self.root, "sessions", "%d.json" % self.PID)
        with open(path, "w") as f:
            json.dump(rec, f)
        os.chmod(path, 0o600)

    def _ask(self, want="dialog open"):
        return harness.presence_witness(self.PID, self.START, want)()

    def test_waiting_for_the_dialog_is_a_witness(self):
        self._record(status="waiting", waitingFor="dialog open")
        ok, why = self._ask()
        self.assertIs(ok, True, why)
        self.assertIn("dialog open", why)

    def test_a_session_that_is_not_waiting_is_a_refusal(self):
        self._record(status="shell")
        ok, why = self._ask()
        self.assertIs(ok, False, why)
        self.assertIn("shell", why)

    def test_waiting_for_another_dialog_is_another_calls_dialog(self):
        self._record(status="waiting", waitingFor="permission prompt")
        ok, why = self._ask()
        self.assertIs(ok, False, why)
        self.assertIn("permission prompt", why)

    def test_an_unreadable_record_is_unknown_never_a_witness(self):  # noqa: VACUOUS_ASSERTION — UNKNOWN is the contract; the same witness, over the same world with a record planted, answers True unconditionally below
        ok, why = self._ask()
        self.assertIsNone(ok, why)
        self.assertIn("could not be read", why)
        # CONTROL: the same world with a record answers.
        self._record(status="waiting", waitingFor="dialog open")
        self.assertIs(self._ask()[0], True)


if __name__ == "__main__":
    unittest.main()
