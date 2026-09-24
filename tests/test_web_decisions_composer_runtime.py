#!/usr/bin/env python3
"""Composer-preservation contract for the work tab's decision queue (#231,
reworked per codex chat e36096810c95).

The owner's first live session: the 45-second poll re-rendered #odqlist via
innerHTML while he was typing a comment and ERASED it. Codex's review round
then proved four sibling arms the first cut missed. The contract, six legs:

  SEND RACE      the submitted value is captured + cleared BEFORE `await post`
                 and its draft-ledger entry purged, so a poll that fires
                 mid-flight and replaces the card can never resurrect the
                 delivered text — and a FAILED send restores the text instead
                 of eating it (into the live box, or the ledger if none).
  POST-SEND EDIT a follow-up typed while the POST awaits is the owner's;
                 completion never touches the composer.
  DRAFT LEDGER   typed-but-blurred text outlives EVERY list replacement —
                 fetch-error, unavailable, empty-queue, and renders that omit
                 the card — restored into whichever later render shows it.
  ORDERING       overlapping refreshes carry a monotonic generation token; a
                 slow OLDER response (success or error) is discarded.
  FOCUS GUARD    while the active element is a .odqcomment inside the list —
                 EMPTY included (focus precedes the first keystroke/IME
                 commit) — no render replaces the list body.
  RESTORE        blurred text returns to ITS card after a redraw; a sibling
                 card's composer stays empty (the must-miss control).

The runtime legs execute the ACTUAL odqInit/odqRender/odqAct/odqTyping/
odqComposerState/odqBox functions lifted verbatim from the assembled web UI over
a DOM stub with LIVE replacement semantics — cards re-derived per innerHTML
set, focus invalidated when its element's card is replaced, and a
controllable fetch/post so the race interleavings are scenes
(tests/decisions_composer_harness.js). Requires node; runtime legs skip (not
fail) where node is unavailable — the static source contracts hold
everywhere.

MUTATION KILLS (the rework): three scenes each carry a named
mutation applied to a TEMP COPY of assembled web UI; the scene must FAIL against
the mutant or it measures nothing — (1) delete odqAct's catch-path ledger
fallback and a failed send whose card vanished mid-flight loses its text
when the card returns; (2) move odqInit's catch guard after the error-card
innerHTML replacement and a focused-empty composer is detached before the
dead focus waves the guard through; (3) delete odqComposerState's ledger
retraction and a deliberately cleared restored draft resurrects."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from helm import web_ui_loader
from tests.test_web_chat_client_runtime import _extract_fn

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HARNESS = os.path.join(HERE, "decisions_composer_harness.js")


def _ui_src():
    return web_ui_loader.read_text()


def _splice(src):
    """The harness's executable slice of assembled web UI source: the single-line
    declarations lifted verbatim plus the eight odq* functions, brace-matched.
    Shared by the green run and every mutant run so a kill exercises the SAME
    extraction path as the assertion it kills."""
    # esc / ODQ_DRAFTS / ODQ_GEN are single-line declarations, not function
    # declarations — lift their lines verbatim
    decls = []
    for pat in (r"^const esc = .+$", r"^const ODQ_DRAFTS = .+$", r"^let ODQ_GEN = .+$"):
        m = re.search(pat, src, re.M)
        assert m, "declaration not found in assembled web UI: " + pat
        decls.append(m.group(0))
    return "\n".join(decls) + "\n" + "\n".join(_extract_fn(src, fn) for fn in (
        "odqCard", "odqBadge", "odqTyping", "odqComposerState",
        "odqBox", "odqRender", "odqInit", "odqAct"))


def _scenes(src):
    """Run every harness scene over the given assembled web UI source; the one
    JSON object the node side prints, as a dict."""
    fd, path = tempfile.mkstemp(suffix=".js")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(_splice(src))
        proc = subprocess.run(
            ["node", HARNESS], capture_output=True, text=True,
            env=dict(os.environ, HELM_SPLICED_SRC=path))
        if proc.returncode != 0:
            raise AssertionError("harness failed: %s" % proc.stderr[-800:])
        return json.loads(proc.stdout)
    finally:
        os.unlink(path)


class TestComposerSourceContract(unittest.TestCase):
    """Node-free pins on the served source."""

    def test_render_guard_precedes_every_body_write(self):  # noqa: VACUOUS_ASSERTION — fn.index() RAISES on absence, so both markers are asserted present before the order comparison
        """odqRender must consult odqTyping() BEFORE its first innerHTML
        write — a guard after the write is the bug with extra steps."""
        fn = _extract_fn(_ui_src(), "odqRender")
        guard = fn.index("odqTyping()")
        first_write = fn.index("innerHTML")
        self.assertLess(guard, first_write,
                        "the typing guard runs after a body write")

    def test_error_path_is_guarded_and_parks_drafts(self):
        """odqInit's catch replaces the list body with an error card; a
        transient fetch error must neither eat a focused composer (guard)
        nor a blurred draft (ledger park) — and BOTH must run BEFORE the
        replacement: a guard on the far side measures dead focus and waves
        every redraw through (codex rework P1-2)."""
        fn = _extract_fn(_ui_src(), "odqInit")
        self.assertIn("odqTyping()", fn,
                      "the error path can still clobber a focused composer")
        self.assertIn("odqComposerState()", fn,
                      "the error path replaces the body without parking drafts")
        write = fn.index("innerHTML")
        self.assertLess(fn.index("odqTyping()"), write,
                        "the catch guard runs after the body replacement")
        self.assertLess(fn.index("odqComposerState()"), write,
                        "the catch parks drafts after the body replacement")

    def test_sent_comment_is_cleared_before_the_await(self):  # noqa: VACUOUS_ASSERTION — fn.index() RAISES on absence of either marker before the order comparison runs
        """odqAct clears the composer AND purges the draft ledger BEFORE
        `await post` — a clear on the far side of the await targets an
        element the 45s poll may have detached mid-flight (codex P1)."""
        fn = _extract_fn(_ui_src(), "odqAct")
        send = fn.index("await post")
        self.assertLess(fn.index('.value = ""'), send,
                        "sent text is cleared after the await — the race is open")
        self.assertLess(fn.index("delete ODQ_DRAFTS"), send,
                        "the draft ledger still holds the sent text across the await")

    def test_focus_alone_holds_the_render(self):  # noqa: VACUOUS_ASSERTION — the assertIn below is the unconditional positive control proving the real guard was extracted; the absence rides it
        """odqTyping must NOT require a truthy value: a focused-but-empty
        composer is owner intent — the redraw would destroy focus before
        the first keystroke/IME commit lands (codex P2)."""
        fn = _extract_fn(_ui_src(), "odqTyping")
        self.assertIn('a.classList.contains("odqcomment")', fn,
                      "the guard's composer check is gone — wrong function extracted")
        self.assertNotIn("a.value", fn,
                         "the guard still requires typed text — focus alone must hold")

    def test_refreshes_carry_a_generation_token(self):
        """Overlapping odqInit calls must be ordered — a slow older response
        may never replace a newer render (codex P1-ordering)."""
        fn = _extract_fn(_ui_src(), "odqInit")
        self.assertIn("ODQ_GEN", fn,
                      "odqInit has no generation token — stale responses can win")


@unittest.skipUnless(shutil.which("node"), "node unavailable")
class TestComposerRuntime(unittest.TestCase):
    """The spliced functions running over the live-replacement DOM stub."""

    @classmethod
    def setUpClass(cls):
        cls.out = _scenes(_ui_src())

    # -- positive control first: the instrument can SEE a redraw -------------
    def test_control_a_render_with_no_composer_state_redraws(self):  # noqa: VACUOUS_ASSERTION — this IS the unconditional positive control the hold scenes cite
        """Unconditional positive control: the instrument can SEE a redraw,
        so every zero below is a finding, not blindness."""
        self.assertGreaterEqual(self.out["control_sets"], 1)

    # -- the exact P1 repro (codex chat e36096810c95) ------------------------
    def test_p1_sent_text_never_resurrects_through_the_poll_race(self):  # noqa: VACUOUS_ASSERTION — p1_poll_replaced_card=True in the SAME scene proves the race ran; a missing scene key raises KeyError; reverting the arm flips all three "" probes (mutation-killed)
        """send -> 45s poll fires mid-await and replaces the card -> POST
        resolves: the delivered text must NOT reappear in the live composer
        (codex measured "already submitted" at both probe points)."""
        self.assertEqual(self.out["p1_cleared_at_send"], "",
                         "the composer still holds the text on the near side of the await")
        self.assertTrue(self.out["p1_poll_replaced_card"],
                        "the scene did not replace the card — the race never ran")
        self.assertEqual(self.out["p1_live_after_poll"], "",
                         "the mid-flight render resurrected the sent text")
        self.assertEqual(self.out["p1_live_after_action_refresh"], "",
                         "the post-action refresh resurrected the sent text")

    def test_mirror_followup_typed_mid_await_survives(self):  # noqa: VACUOUS_ASSERTION — equality against the non-empty needle "next thought" cannot pass on an absent observable; a missing scene key raises KeyError
        self.assertEqual(self.out["mirror_followup"], "next thought",
                         "completion cleared the owner's in-flight follow-up")

    # -- drafts across every replacement branch ------------------------------
    def test_draft_survives_the_fetch_error_card(self):  # noqa: VACUOUS_ASSERTION — branch_error_replaced=True is the in-test positive control that the branch actually rendered; the non-empty needle equality rides it
        self.assertTrue(self.out["branch_error_replaced"],
                        "the error card never rendered — the branch was not exercised")
        self.assertEqual(self.out["branch_error_value"], "parked draft")

    def test_draft_survives_the_unavailable_card(self):  # noqa: VACUOUS_ASSERTION — branch_unavail_replaced=True is the in-test positive control that the branch actually rendered; the non-empty needle equality rides it
        self.assertTrue(self.out["branch_unavail_replaced"],
                        "the unavailable card never rendered — the branch was not exercised")
        self.assertEqual(self.out["branch_unavail_value"], "parked draft")

    def test_draft_survives_the_empty_queue_card(self):  # noqa: VACUOUS_ASSERTION — branch_empty_replaced=True is the in-test positive control that the branch actually rendered; the non-empty needle equality rides it
        self.assertTrue(self.out["branch_empty_replaced"],
                        "the empty-queue card never rendered — the branch was not exercised")
        self.assertEqual(self.out["branch_empty_value"], "parked draft")

    def test_draft_survives_a_render_that_omits_its_card(self):  # noqa: VACUOUS_ASSERTION — branch_omit_gone=True + the non-empty "parked draft" equality are the positive controls; the sibling "" must-miss rides them
        self.assertTrue(self.out["branch_omit_gone"],
                        "the omitting render still showed the card — the branch was not exercised")
        self.assertEqual(self.out["branch_omit_sibling"], "",
                         "the parked draft leaked onto the surviving sibling")
        self.assertEqual(self.out["branch_omit_value"], "parked draft")
        self.assertEqual(self.out["branch_sibling_value"], "",
                         "restored text leaked onto a sibling card")

    # -- generation ordering -------------------------------------------------
    def test_a_stale_response_never_replaces_a_newer_render(self):  # noqa: VACUOUS_ASSERTION — order_shows_newer=True proves the newer render landed, so the zero delta is a finding; test_control_a proves the instrument sees repaints
        self.assertEqual(self.out["order_stale_sets"], 0,
                         "a slow older response repainted over the newer render")
        self.assertTrue(self.out["order_shows_newer"],
                        "the surface shows the stale feed, not the newest")
        self.assertTrue(self.out["order_stale_error_kept"],
                        "a slow older FAILURE painted the error card over a newer render")

    # -- P2: focus alone holds ------------------------------------------------
    def test_focused_empty_composer_holds_the_redraw(self):  # noqa: VACUOUS_ASSERTION — the zero is a finding because test_control_a proves the same instrument sees a redraw
        self.assertEqual(self.out["focusempty_sets"], 0,
                         "the redraw destroyed a focused-but-empty composer")
        self.assertTrue(self.out["focusempty_focus_alive"],
                        "focus died — the replacement detached the active element")
        self.assertIn("typing", self.out["focusempty_meta"],
                      "the hold is silent — the meta line must name it")

    # -- failed sends ----------------------------------------------------------
    def test_a_failed_send_restores_the_comment(self):  # noqa: VACUOUS_ASSERTION — three non-empty needle equalities are the positive controls; the detached-"" probe rides them
        self.assertEqual(self.out["fail_restored"], "precious",
                         "the failed send ate the comment")
        self.assertEqual(self.out["fail_restored_live"], "precious2",
                         "the restore missed the LIVE composer after a mid-flight replacement")
        self.assertEqual(self.out["fail_detached_old"], "",
                         "the restore wrote into the detached pre-replacement element")
        self.assertEqual(self.out["fail_followup_wins"], "newer intent",
                         "the failed text clobbered the owner's newer follow-up")

    def test_a_failed_send_parks_when_the_card_vanished_mid_flight(self):  # noqa: VACUOUS_ASSERTION — absent_card_gone=True + two non-empty "precious3" equalities are the positive controls; the two "" must-miss probes ride them
        """codex rework P1-1: a mid-flight poll DROPS the card, THEN the POST
        fails — no live box exists, so the text must park in the ODQ_DRAFTS
        ledger and return WITH the card on a later render."""
        self.assertTrue(self.out["absent_card_gone"],
                        "the mid-flight poll kept the card — the scene never ran the race")
        self.assertEqual(self.out["absent_parked"], "precious3",
                         "the failed send did not park the text in the ledger")
        self.assertEqual(self.out["absent_returned_value"], "precious3",
                         "the returning card lost the failed send's text")
        self.assertEqual(self.out["absent_detached_old"], "",
                         "the restore wrote into the detached pre-replacement element")
        self.assertEqual(self.out["absent_sibling"], "",
                         "the parked text leaked onto a sibling card")

    def test_fetch_error_guard_runs_before_the_catch_replacement(self):  # noqa: VACUOUS_ASSERTION — the zero is a finding because test_control_a proves the same instrument sees a redraw and test_draft_survives_the_fetch_error_card proves the same catch DOES replace when unfocused
        """codex rework P1-2: the fetch-error catch under a FOCUSED (empty)
        composer — the guard must run BEFORE the innerHTML replacement, or
        the error card detaches the input first and the dead focus waves the
        guard through."""
        self.assertEqual(self.out["focuserr_sets"], 0,
                         "the error card replaced the body under a focused composer")
        self.assertTrue(self.out["focuserr_focus_alive"],
                        "focus died — the catch replacement detached the active element")
        self.assertIn("typing", self.out["focuserr_meta"],
                      "the hold is silent — the meta line must name it")

    def test_clearing_a_restored_draft_stays_cleared(self):  # noqa: VACUOUS_ASSERTION — clear_restored_first="stale text" is the in-test positive control that the ledger round-tripped; the "" equality rides it
        """codex rework P1-3: the owner deliberately empties a restored
        parked draft — the ledger entry retracts, so the next render must
        not resurrect the deleted text."""
        self.assertEqual(self.out["clear_restored_first"], "stale text",
                         "the park+restore round-trip never happened — nothing to clear")
        self.assertEqual(self.out["clear_stays_cleared"], "",
                         "the next render resurrected the deliberately cleared draft")
        self.assertTrue(self.out["clear_ledger_empty"],
                        "the emptied box left its stale ledger entry in place")

    # -- the original three scenes, now odqInit-driven -----------------------
    def test_typing_holds_the_redraw_and_says_so(self):  # noqa: VACUOUS_ASSERTION — the zero here is a finding because test_control_a proves the same instrument sees a redraw
        self.assertEqual(self.out["typing_sets"], 0,
                         "the list body was redrawn under a typing owner")
        self.assertIn("typing", self.out["typing_meta"],
                      "the hold is silent — the meta line must name it")

    def test_blurred_text_survives_the_redraw_on_its_own_card(self):  # noqa: VACUOUS_ASSERTION — restore_sets>=1 inside this test is the positive control; the sibling emptiness rides it
        self.assertGreaterEqual(self.out["restore_sets"], 1,
                                "no redraw happened, restore proved nothing")
        self.assertEqual(self.out["restore_value"], "half a thought")
        self.assertEqual(self.out["sibling_value"], "",
                         "restored text leaked onto a sibling card")


# ---- the reviewer-named mutations, byte-exact from assembled web UI -------------
# each is applied to a TEMP COPY and its scene must FAIL against the mutant
_FALLBACK_LINE = (
    "    else if (sent && !live && !(id in ODQ_DRAFTS)) ODQ_DRAFTS[id] = sent;\n")
_RETRACTION_LINE = "    else delete ODQ_DRAFTS[el.dataset.id];\n"
_CATCH_GUARD = ('    if (odqTyping()) { $("#odqmeta").textContent = '
                '"refresh held — you are typing"; return; }\n')
_CATCH_PARK = ("    odqComposerState(); // park blurred drafts in the ledger "
               "— the error card replaces the body\n")
_CATCH_WRITE = ('    $("#odqlist").innerHTML = \'<div class="empty">'
                "✗ decision queue unreadable: ' + esc(e.message) + \"</div>\"; "
                '$("#odqmeta").textContent = "UNKNOWN";\n')


@unittest.skipUnless(shutil.which("node"), "node unavailable")
class TestComposerMutationKills(unittest.TestCase):
    """Each codex-named surviving mutation, now killed: the mutation is
    applied to a TEMP COPY of assembled web UI (never the tree) and the matching
    scene must FAIL against it — a scene its mutation cannot flip measures
    nothing. Assertions state the mutant's CONCRETE wrong effect (text lost /
    focus dead / stale text back), never the absence of a pass."""

    @classmethod
    def setUpClass(cls):
        # mutation law: clear bytecode caches before ANY mutation run — a
        # stale cache can make a same-size same-second edit invisible
        # (mutation-tests-need-pycache-cleared)
        for d in ("tests", "helm"):
            shutil.rmtree(os.path.join(ROOT, d, "__pycache__"),
                          ignore_errors=True)

    def _mutant(self, old, new):
        """Scenes run against a temp copy of assembled web UI with old->new
        applied; hard-fails unless the target exists exactly once (an edit
        that silently missed would report the GREEN source as the mutant)."""
        src = _ui_src()
        self.assertEqual(src.count(old), 1,
                         "mutation target not found exactly once in assembled web UI")
        tmp = tempfile.mkdtemp(prefix="helm-odq-mutant-")
        path = os.path.join(tmp, "web_ui.html")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(src.replace(old, new))
            with open(path, encoding="utf-8") as f:  # what ran IS the copy on disk
                mutated = f.read()
            self.assertNotEqual(mutated, src, "the mutation did not apply")
            return _scenes(mutated)
        finally:
            shutil.rmtree(tmp)

    def test_kill_deleting_the_ledger_fallback_loses_the_absent_card_text(self):  # noqa: VACUOUS_ASSERTION — absent_card_gone=True proves the mutant ran the same race; the "" equality IS the mutant's concrete wrong effect, and the green twin test pins "precious3"
        """Delete odqAct's catch-path ODQ_DRAFTS fallback: the failed send
        whose card vanished mid-flight must now LOSE its text when the card
        returns — proving scene absentfail is what kills this mutant."""
        out = self._mutant(_FALLBACK_LINE, "")
        self.assertTrue(out["absent_card_gone"],
                        "the mutant scene never ran the vanished-card race")
        self.assertEqual(out["absent_parked"], "",
                         "the ledger still parked the text — the fallback survived deletion")
        self.assertEqual(out["absent_returned_value"], "",
                         "the returning card kept the text — the mutation was not killed")

    def test_kill_moving_the_catch_guard_after_the_replacement_detaches_focus(self):  # noqa: VACUOUS_ASSERTION — sets>=1 and the False focus probe are the mutant's concrete wrong effects, the exact inversions of the green twin's 0/True
        """Move odqInit's catch guard AFTER the error-card innerHTML write:
        the replacement must now land under a focused-empty composer and
        detach it — proving scene focuserr is what kills this mutant."""
        out = self._mutant(_CATCH_GUARD + _CATCH_PARK + _CATCH_WRITE,
                           _CATCH_PARK + _CATCH_WRITE + _CATCH_GUARD)
        self.assertGreaterEqual(out["focuserr_sets"], 1,
                                "the body was not replaced — the guard still ran first")
        self.assertFalse(out["focuserr_focus_alive"],
                         "focus survived — the guard still precedes the replacement")

    def test_kill_deleting_the_ledger_retraction_resurrects_cleared_text(self):  # noqa: VACUOUS_ASSERTION — both equalities are against the non-empty needle "stale text"; the first is the same positive control the green twin pins
        """Delete odqComposerState's else-delete retraction: a deliberately
        cleared restored draft must now RESURRECT on the next render —
        proving scene clear is what kills this mutant."""
        out = self._mutant(_RETRACTION_LINE, "")
        self.assertEqual(out["clear_restored_first"], "stale text",
                         "the park+restore round-trip broke under the mutant")
        self.assertEqual(out["clear_stays_cleared"], "stale text",
                         "the cleared draft stayed cleared — the mutation was not killed")


if __name__ == "__main__":
    unittest.main()
