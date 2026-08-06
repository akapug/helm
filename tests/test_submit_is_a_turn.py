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

from helm import composers, harness

# A real Claude composer frame, reproduced from bytes measured off a live pane
# 2026-08-05 and then made synthetic. Two details are load-bearing and neither
# is guessable: the glyph and the composer body are separated by U+00A0 (NOT a
# space), and the model/cwd status row sits BELOW the box.
_BORDER = "─" * 60


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


class _FakeAdapter(harness._CLIAdapter):
    """A metaharness that accepts every byte — the exact liar under test.

    `sent` records (text, enter) so the SPLIT is provable, and `tails` is the
    scripted sequence of pane reads the verifier will see.
    """
    name = "fake"
    bin = "fake"

    def __init__(self, tails, refuse_text=False, refuse_enter=False,
                 read_raises=False, pre=None):
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

    def send(self, handle, text, enter=True):
        if enter and self.refuse_enter:
            raise harness.HarnessError("fake: enter refused")
        if not enter and self.refuse_text:
            raise harness.HarnessError("fake: text refused")
        self.sent.append((text, enter))

    def read(self, handle, limit=3000, timeout=60):
        self.reads += 1
        if self.read_raises:
            raise harness.HarnessError("fake: pane unreadable")
        return self.tails.pop(0) if self.tails else self.tails and "" or ""


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

    def test_pane_that_advanced_is_DELIVERED(self):
        ad = _FakeAdapter([_frame()])
        with _no_sleep():
            state, detail = ad.submit("pane-1", self.TEXT)
        self.assertEqual(state, harness.DELIVERED)
        self.assertIn("advanced", detail)

    def test_pane_still_holding_our_text_is_NOT_DELIVERED(self):
        # THE OWNER'S PANE. Every byte was accepted; the seat got no turn.
        ad = _FakeAdapter([_frame(self.TEXT)] * harness.SUBMIT_VERIFY_READS)
        with _no_sleep():
            state, detail = ad.submit("pane-1", self.TEXT)
        self.assertEqual(state, harness.NOT_DELIVERED)
        self.assertIn("typed, never submitted", detail)

    def test_unreadable_pane_is_UNKNOWN_and_never_DELIVERED(self):
        ad = _FakeAdapter([], read_raises=True)
        with _no_sleep():
            state, detail = ad.submit("pane-1", self.TEXT)
        self.assertEqual(state, harness.UNKNOWN)
        self.assertNotEqual(state, harness.DELIVERED)
        self.assertIn("re-read failed", detail)

    def test_blind_pane_empty_tail_is_UNKNOWN_not_DELIVERED(self):
        # The 12-of-26 case: connected, writable, not orphaned, and every read
        # comes back empty. An empty tail is NOT an empty composer.
        ad = _FakeAdapter(["", "", ""])
        with _no_sleep():
            state, _ = ad.submit("pane-1", self.TEXT)
        self.assertEqual(state, harness.UNKNOWN)
        # POSITIVE CONTROL: the identical adapter whose pane DOES answer
        # returns DELIVERED, so UNKNOWN above is about the blind tail and not
        # a submit that can never succeed.
        ok = _FakeAdapter([_frame()])
        with _no_sleep():
            self.assertEqual(ok.submit("pane-1", self.TEXT)[0],
                             harness.DELIVERED)

    def test_a_late_repaint_still_counts_as_DELIVERED(self):
        # Held on the first read, clear on the second: the bounded repaint
        # window exists so a slow frame is not called a failure.
        ad = _FakeAdapter([_frame(self.TEXT), _frame()])
        with _no_sleep():
            state, _ = ad.submit("pane-1", self.TEXT)
        self.assertEqual(state, harness.DELIVERED)
        self.assertEqual(ad.reads, 3, "one pre-read plus two verify reads")

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
        ad = _FakeAdapter([_frame(self.TEXT)] * harness.SUBMIT_VERIFY_READS,
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
        self.assertIn("Enter call itself errored", detail)

    def test_a_composer_dirty_BEFORE_the_send_cannot_answer_DELIVERED_weakly(self):
        # FOUND ON A LIVE PANE, not reasoned about. When the composer already
        # holds someone else's draft, Claude appends at the cursor — so our
        # text is no longer the first line, and "the composer does not start
        # with our text" would answer DELIVERED over a pane where nothing was
        # submitted at all. With a dirty pre-read, only EMPTY counts.
        dirty = _frame("half a draft somebody was typing")
        ad = _FakeAdapter([dirty] * 3, pre=dirty)
        with _no_sleep():
            state, detail = ad.submit("pane-1", self.TEXT)
        self.assertEqual(state, harness.UNKNOWN)
        self.assertIn("held foreign text before the send", detail)

    def test_a_dirty_composer_that_goes_EMPTY_is_DELIVERED(self):
        # The positive half: the bar rose to "empty", and clearing it clears
        # the bar. Without this the arm above could be a blanket refusal.
        ad = _FakeAdapter([_frame()],
                          pre=_frame("half a draft somebody was typing"))
        with _no_sleep():
            state, detail = ad.submit("pane-1", self.TEXT)
        self.assertEqual(state, harness.DELIVERED)
        self.assertIn("EMPTY composer", detail)

    def test_an_unreadable_PRE_read_also_raises_the_bar(self):
        # helm could not see the composer before typing, so it cannot later
        # credit anything it typed for the composer looking clean.
        ad = _FakeAdapter([_frame("someone elses text")] * 3, pre="")
        with _no_sleep():
            state, _ = ad.submit("pane-1", self.TEXT)
        self.assertEqual(state, harness.UNKNOWN)

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
        tails = {"p-held": _frame("Continue the lane."), "p-clear": _frame()}

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
                               "p-blind": composers.CANNOT_TELL})

    def test_scan_never_reads_a_blind_pane(self):
        # Reading it would raise or return "" and cost a subprocess to learn
        # nothing; `last_output_at` already answered.
        touched = []

        class Ad:
            name = "fake"

            def list(self):
                return [{"handle": "p-blind", "last_output_at": None,
                         "title": "", "worktree": ""}]

            def read(self, handle, limit=3000):
                touched.append(handle)
                return ""

        composers.scan(adapter=Ad())
        self.assertEqual(touched, [])
        # POSITIVE CONTROL: the same scan against a pane the metaharness HAS
        # seen output from does read it, so the empty list above is a decision
        # and not a scan that reads nothing at all.
        Ad.list = lambda self: [{"handle": "p-live", "last_output_at": 7,
                                 "title": "", "worktree": ""}]
        composers.scan(adapter=Ad())
        self.assertEqual(touched, ["p-live"])

    def test_absent_metaharness_is_an_honest_error_not_an_empty_fleet(self):
        with mock.patch.object(harness, "detect", return_value=None):
            rows, err = composers.scan()
        self.assertEqual(rows, [])
        self.assertTrue(err)


if __name__ == "__main__":
    unittest.main()
