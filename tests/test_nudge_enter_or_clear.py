#!/usr/bin/env python3
"""A pane nudge Helm places is either SUBMITTED or REMOVED (task/1818).

The owner: "i keep going to a longtail or local seat like gemini or
qwen27 and seeing this helm seat resume sitting in their input unsent". The
measured cause: `helm beacons --post` forks the deliverer from a systemd
oneshot unit, systemd kills the unit's whole control group when the pass
exits, and the deliverer died between typing its line and pressing Enter. The
line stayed in the composer, and the leg that knew it was there was dead.

Three cures, each pinned here:
  * the deliverer takes its own line back out when it will not submit it,
    instead of leaving it and filing a `task/resume-turn-*` recovery row;
  * a pass clears the line a DEAD deliverer left (the sweep);
  * the pass that forks deliverers waits for them before it exits.

Every clear is proven first: the key goes only to a composer that holds
exactly Helm's text, so a human's draft is never touched.
"""
import contextlib
import io
import sys
import unittest
from unittest import mock

from helm import beacons, harness, resumeturn, tasks
from tests.test_resumeturn import (SID, DoorBook, ResumeTurnBase, _refused)

KILL_LINE = "\x15"       # the readline kill-to-line-start key, as a TUI reads it
END_OF_LINE = "\x05"     # the readline end-of-line key, as a TUI reads it


class Pane(harness._CLIAdapter):
    """One composer, read and written through the REAL inherited doors. It
    models what a TUI does with each key: typed text appends, an Enter
    submits (unless `swallow_enter`), and Ctrl+U empties the line (unless
    `swallow_clear`). `after_type` runs once, right after Helm's text lands:
    it is where a human types into the same composer."""
    name = "fake"

    def __init__(self, composer=""):
        self.composer, self.sent, self.reads = composer, [], 0
        self.readable = True
        self.swallow_enter = self.swallow_clear = False
        self.after_type = None
        self.handles = ["h1"]

    def list(self):
        return [{"handle": h, "title": "codex", "status": "connected",
                 "worktree": "", "last_output_at": 1} for h in self.handles]

    def read(self, handle, limit=3000, timeout=60):
        self.reads += 1
        if not self.readable:
            raise harness.HarnessError("pane went dark")
        return "\n".join(("─" * 40, "❯\xa0" + self.composer, "─" * 40,
                          "  opus-5 | ~/dev/example/repo"))

    def send(self, handle, text, enter=True):
        self.sent.append((handle, text, enter))
        if text == END_OF_LINE and not enter:
            return
        if text == KILL_LINE and not enter:
            if not self.swallow_clear:
                self.composer = ""
            return
        if enter:
            if not self.swallow_enter:
                self.composer = ""
            return
        self.composer += text
        if self.after_type is not None:
            hook, self.after_type = self.after_type, None
            hook(self)

    def keys(self, key):
        return [s for s in self.sent if s[1] == key and not s[2]]


def _recovery_rows():
    rows, unavailable = tasks.snapshot(strict=True)
    return [r for r in (rows or {}) if str(r).startswith("task/resume-turn-")]


class TheDeliverersOwnLineComesBackOutTest(ResumeTurnBase):
    """A deliverer that typed a line and will not submit it removes it."""

    def child(self, pane, **kw):
        with mock.patch.object(resumeturn, "_alert"):
            return resumeturn.child("codex", SID, "GO NOW", 0, adapter=pane,
                                    **kw)

    def test_an_enter_that_never_takes_leaves_no_line_and_no_row(self):  # noqa: VACUOUS_ASSERTION — the pressed Enter and the one clear key are positive controls on the same pane
        pane = Pane()
        pane.swallow_enter = True
        mode, detail = self.child(pane)
        self.assertNotEqual(mode, "resumed", detail)
        self.assertTrue([s for s in pane.sent if s[2]],
                        "fixture: no Enter was ever pressed: %r" % pane.sent)
        self.assertEqual(pane.composer, "",
                         "Helm's line was left in the composer: %s" % detail)
        self.assertEqual(len(pane.keys(KILL_LINE)), 1, pane.sent)
        self.assertNotIn("h1", resumeturn.recorded_injections(
            include_expired=True))
        self.assertEqual(_recovery_rows(), [], detail)

    def test_a_repair_refused_after_placement_takes_its_line_back(self):  # noqa: VACUOUS_ASSERTION — the asked recovery door and the drained mode are positive controls on the same child
        """The deaf leg's own recovery door refuses (the rows drained while the
        line sat there): the Enter is withheld and the line comes out."""
        pane = Pane()
        pane.swallow_enter = True
        book = DoorBook(recovery=_refused("the rows drained while it held"))
        with mock.patch.object(resumeturn, "_prepare_due", side_effect=book):
            mode, detail = self.child(pane, record_key="deaf:codex:%s" % SID)
        self.assertIn("recovery", book.asked,
                      "fixture: the recovery door was never asked")
        self.assertEqual(mode, "drained", detail)
        self.assertEqual(pane.composer, "", detail)
        self.assertEqual(_recovery_rows(), [], detail)

    def test_a_humans_edit_is_never_cleared(self):  # noqa: VACUOUS_ASSERTION — the composer is asserted EQUAL to the human's text, and the arm above drives the same child to one clear
        """THE CONTROL: a person typed after Helm's line. The composer no
        longer holds exactly Helm's text, so no key goes to it."""
        pane = Pane()

        def human(p):
            p.composer += " and my own words"
        pane.after_type = human
        mode, detail = self.child(pane)
        self.assertNotEqual(mode, "resumed", detail)
        self.assertEqual(pane.keys(KILL_LINE), [], detail)
        self.assertEqual(pane.composer, "GO NOW and my own words")
        self.assertEqual(_recovery_rows(), [], detail)


class TheSweepClearsADeadDeliverersLineTest(ResumeTurnBase):
    """The deliverer died between typing and Enter: the measured incident."""

    def record(self, pane, text="Run `helm seat resume-turn --show 0123456789abcdef`",
               deliverer="999999999:1", age=0):
        pane.composer = text
        gen = resumeturn._record_injection("codex", SID, "h1", text,
                                           adapter="fake")
        self.assertTrue(gen, "fixture: the record was not written")

        def stamp(entry):
            entry["injection"]["recorded_at"] -= age
            if deliverer is None:
                entry["injection"].pop("deliverer", None)
            else:
                entry["injection"]["deliverer"] = deliverer
            return None, True
        resumeturn._mutate_entry("codex", stamp)
        return text

    def test_a_dead_deliverers_line_is_cleared_and_forgotten(self):
        pane = Pane()
        self.record(pane)
        swept, err = resumeturn.sweep_stranded(adapter=pane)
        self.assertIsNone(err, err)
        self.assertEqual([(h, s) for h, s, _d in swept],
                         [("h1", harness.CLEARED)], swept)
        self.assertEqual(pane.composer, "")
        self.assertEqual(len(pane.keys(KILL_LINE)), 1, pane.sent)
        self.assertNotIn("h1", resumeturn.recorded_injections(
            include_expired=True))

    def test_a_live_deliverers_line_is_left_to_it(self):  # noqa: VACUOUS_ASSERTION — the dead-deliverer arm above drives the same record and pane to one clear
        pane = Pane()
        text = self.record(pane, deliverer=resumeturn._self_ident())
        swept, err = resumeturn.sweep_stranded(adapter=pane)
        self.assertEqual((swept, err), ([], None))
        self.assertEqual((pane.reads, pane.sent), (0, []))
        self.assertEqual(pane.composer, text)

    def test_a_record_with_no_stamp_is_orphaned_only_when_old(self):
        pane = Pane()
        self.record(pane, deliverer=None)
        self.assertEqual(resumeturn.sweep_stranded(adapter=pane), ([], None))
        pane = Pane()
        self.record(pane, deliverer=None, age=resumeturn.DELIVERER_S + 1)
        swept, _err = resumeturn.sweep_stranded(adapter=pane)
        self.assertEqual([s for _h, s, _d in swept], [harness.CLEARED])

    def test_a_humans_draft_over_the_record_gets_no_key(self):  # noqa: VACUOUS_ASSERTION — the sweep's answer is asserted EQUAL to not-ours and the composer EQUAL to the draft
        pane = Pane()
        text = self.record(pane)
        pane.composer = text + " but also this"
        swept, _err = resumeturn.sweep_stranded(adapter=pane)
        self.assertEqual([s for _h, s, _d in swept], [harness.NOT_OURS])
        self.assertEqual(pane.sent, [])
        self.assertEqual(pane.composer, text + " but also this")

    def test_an_unreadable_pane_keeps_the_record_for_the_next_pass(self):  # noqa: VACUOUS_ASSERTION — the sweep's answer is asserted EQUAL to unknown and the record asserted present
        pane = Pane()
        self.record(pane)
        pane.readable = False
        swept, _err = resumeturn.sweep_stranded(adapter=pane)
        self.assertEqual([s for _h, s, _d in swept], [harness.UNKNOWN])
        self.assertEqual(pane.sent, [])
        self.assertIn("h1", resumeturn.recorded_injections(
            include_expired=True))

    def test_a_row_an_older_helm_filed_closes_with_its_record(self):
        """The rows already in a ledger are moot once their record resolves:
        the sweep that forgets the record closes its open row."""
        pane = Pane()
        self.record(pane)
        pane.composer = ""
        inj = resumeturn.recorded_injections(include_expired=True)["h1"]
        ident = resumeturn._recovery_task_identity("h1", inj["generation"])
        row, err = tasks.add(
            ident["title"], None, note="filed by an older helm",
            refs=[ident["ref"]], source=ident["source"], tid=ident["id"],
            status="open", origin="agent", project=tasks.current_project(),
            force_new=True)
        self.assertIsNone(err, err)
        swept, _err = resumeturn.sweep_stranded(adapter=pane)
        self.assertEqual([s for _h, s, _d in swept], [harness.GONE])
        closed = tasks.get(ident["id"])
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["closed_reason"],
                         "its text is no longer in the composer")

    def test_stale_row_closes_even_without_an_orphaned_injection(self):
        ident = resumeturn._recovery_task_identity("h1", "overwritten")
        row, err = tasks.add(
            ident["title"], None, note="older Helm recovery",
            refs=[ident["ref"]], source=ident["source"], tid=ident["id"],
            status="open", origin="agent", project=tasks.current_project(),
            force_new=True)
        self.assertIsNone(err, err)
        self.assertEqual(row["id"], ident["id"])
        pane = Pane()
        self.assertEqual(resumeturn.sweep_stranded(adapter=pane), ([], None))
        self.assertEqual(tasks.get(ident["id"])["status"], "closed")
        self.assertEqual((pane.reads, pane.sent), (0, []))

    def test_expired_live_record_protects_its_recovery_row(self):
        pane = Pane()
        self.record(pane, deliverer=resumeturn._self_ident())
        inj = resumeturn.recorded_injections(include_expired=True)["h1"]
        ident = resumeturn._recovery_task_identity("h1", inj["generation"])
        row, err = tasks.add(
            ident["title"], None, note="live Helm recovery",
            refs=[ident["ref"]], source=ident["source"], tid=ident["id"],
            status="open", origin="agent", project=tasks.current_project(),
            force_new=True)
        self.assertIsNone(err, err)
        self.assertEqual(row["id"], ident["id"])
        def expire(entry):
            entry["injection"]["expires_at"] = 1
            return None, True
        resumeturn._mutate_entry("codex", expire)
        self.assertEqual(resumeturn.sweep_stranded(adapter=pane), ([], None))
        self.assertEqual(tasks.get(ident["id"])["status"], "open")
        self.assertEqual((pane.reads, pane.sent), (0, []))

    def test_another_metaharnesss_record_is_left_alone(self):  # noqa: VACUOUS_ASSERTION — the dead-deliverer arm drives the same record through this adapter to one clear
        pane = Pane()
        text = self.record(pane)

        def other(entry):
            entry["injection"]["adapter"] = "herdr"
            return None, True
        resumeturn._mutate_entry("codex", other)
        self.assertEqual(resumeturn.sweep_stranded(adapter=pane), ([], None))
        self.assertEqual((pane.reads, pane.sent, pane.composer), (0, [], text))
        self.assertIn("h1", resumeturn.recorded_injections(
            include_expired=True))

    def test_a_gone_panes_record_is_forgotten_without_a_key(self):  # noqa: VACUOUS_ASSERTION — the sweep's answer is asserted EQUAL to gone; the dead-deliverer arm drives the same record to one clear
        pane = Pane()
        self.record(pane)
        pane.handles = ["h2"]
        swept, _err = resumeturn.sweep_stranded(adapter=pane)
        self.assertEqual([s for _h, s, _d in swept], [harness.GONE])
        self.assertEqual(pane.sent, [])
        self.assertNotIn("h1", resumeturn.recorded_injections(
            include_expired=True))


class TheClearDoorTest(unittest.TestCase):
    """`_CLIAdapter.clear_placed`: proven first, then one key, then read back."""

    def setUp(self):
        gap = mock.patch.object(harness, "SUBMIT_VERIFY_INTERVAL_S", 0)
        gap.start()
        self.addCleanup(gap.stop)

    def test_exactly_helms_line_is_cleared_with_scroll_then_key_and_no_enter(self):
        pane = Pane("GO NOW")
        state, detail = pane.clear_placed("h1", "GO NOW")
        self.assertEqual(state, harness.CLEARED, detail)
        self.assertEqual(pane.sent, [("h1", END_OF_LINE, False),
                                     ("h1", KILL_LINE, False)])

    def test_an_empty_composer_needs_no_key(self):  # noqa: VACUOUS_ASSERTION — the door's answer is asserted EQUAL to gone; the arm above sends exactly one key
        pane = Pane("")
        self.assertEqual(pane.clear_placed("h1", "GO NOW")[0], harness.GONE)
        self.assertEqual(pane.sent, [])

    def test_a_key_that_does_not_take_is_unknown_not_cleared(self):
        pane = Pane("GO NOW")
        pane.swallow_clear = True
        state, detail = pane.clear_placed("h1", "GO NOW")
        self.assertEqual(state, harness.UNKNOWN, detail)
        self.assertIn("still holds", detail)

    def test_part_of_helms_line_is_undecidable_and_gets_no_key(self):
        """A prefix is our line still painting or someone deleting back into
        it; the door cannot tell which, so it sends nothing."""
        pane = Pane("GO N")
        state, _detail = pane.clear_placed("h1", "GO NOW")
        self.assertEqual(state, harness.UNKNOWN)
        self.assertEqual(pane.sent, [])

    def test_a_multiline_text_is_never_proven_by_one_line(self):
        pane = Pane("GO NOW")
        state, _detail = pane.clear_placed("h1", "GO NOW\nand more")
        self.assertEqual(state, harness.UNKNOWN)
        self.assertEqual(pane.sent, [])

    def test_an_owner_append_during_cursor_move_is_not_cleared(self):
        class OwnerEditsDuringMove(Pane):
            def send(self, handle, text, enter=True):
                super().send(handle, text, enter=enter)
                if text == END_OF_LINE and not enter:
                    self.composer += " owner's draft"

        pane = OwnerEditsDuringMove("GO NOW")
        state, detail = pane.clear_placed("h1", "GO NOW")
        self.assertEqual(state, harness.NOT_OURS, detail)
        self.assertEqual(pane.composer, "GO NOW owner's draft")
        self.assertEqual(pane.keys(KILL_LINE), [], pane.sent)

    def test_a_failed_cursor_move_does_not_clear_from_an_unknown_position(self):
        class MoveFails(Pane):
            def send(self, handle, text, enter=True):
                if text == END_OF_LINE and not enter:
                    raise harness.HarnessError("cursor move failed")
                return super().send(handle, text, enter=enter)

        pane = MoveFails("GO NOW")
        state, detail = pane.clear_placed("h1", "GO NOW")
        self.assertEqual(state, harness.UNKNOWN, detail)
        self.assertEqual(pane.composer, "GO NOW")
        self.assertEqual(pane.keys(KILL_LINE), [], pane.sent)

    def test_a_key_before_the_clear_moves_the_cursor_to_the_end(self):
        """THE F2 GUARD: before the clear key the line is scrolled to its end,
        so a cursor that sat inside the line can no longer leave the rest of
        it behind after the clear."""
        pane = Pane("GO NOW")
        state, detail = pane.clear_placed("h1", "GO NOW")
        self.assertEqual(state, harness.CLEARED, detail)
        self.assertEqual(pane.sent[0], ("h1", END_OF_LINE, False))
        self.assertEqual(pane.sent[1], ("h1", KILL_LINE, False))
        # The clear key is the only key that empties the composer, and Enter
        # is never pressed.
        self.assertEqual(pane.keys(KILL_LINE), [("h1", KILL_LINE, False)])
        self.assertNotIn(("h1", "", True), pane.sent)


class ThePassWaitsForItsDeliverersTest(unittest.TestCase):
    """A oneshot unit kills what its pass leaves running, so the pass waits."""

    def test_await_children_returns_once_the_forked_deliverer_exits(self):  # noqa: VACUOUS_ASSERTION — the forked list is asserted non-empty and every child asserted exited
        resumeturn.spawn_child(
            [sys.executable, "-c", "import time; time.sleep(0.5)"])
        forked = list(resumeturn._CHILDREN)
        self.assertTrue(forked, "spawn_child kept no handle to wait on")
        self.assertEqual(resumeturn.await_children(30), 0)
        self.assertTrue(all(p.poll() is not None for p in forked))

    def test_beacons_post_sweeps_first_and_waits_last(self):
        order = []
        rep = {"unreachable": [], "vacant": [], "ghosts": []}
        sent = {"nudged": [], "rearmed": [], "alarms": 0, "chat": True,
                "push": True}
        with mock.patch.object(beacons, "census", return_value=rep), \
                mock.patch.object(beacons, "unenrolled_panes",
                                  return_value=[]), \
                mock.patch.object(beacons, "attend",
                                  return_value={"error": None,
                                                "transitions": []}), \
                mock.patch.object(beacons, "_print_census"), \
                mock.patch.object(beacons, "_prompt_stall_leg",
                                  return_value=None), \
                mock.patch("helm.ownerasks.flush_unreached",
                           return_value=(0, None)), \
                mock.patch.object(beacons, "escalate",
                                  side_effect=lambda *a, **k:
                                  order.append("escalate") or sent) as escalated, \
                mock.patch.object(resumeturn, "sweep_stranded", create=True,
                                  side_effect=lambda *a, **k:
                                  order.append("sweep") or ([], None)), \
                mock.patch.object(resumeturn, "await_children", create=True,
                                  side_effect=lambda *a, **k:
                                  order.append("await") or 0), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = beacons.cmd_beacons(["--post"])
        self.assertEqual(rc, 0)
        self.assertEqual(order, ["sweep", "escalate", "await"])
        self.assertFalse(escalated.call_args.kwargs["complete"],
                         "a fake census without probe results is not complete")


if __name__ == "__main__":
    unittest.main()
