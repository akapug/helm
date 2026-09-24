#!/usr/bin/env python3
"""promptshape — which turns a notice began, and which notice bytes carry
content (task/2970, task/2972).

The fixtures are notice prompts in the harness's own shape with invented
content (tests/_notices.py). Each arm imports the module inside itself, so on
a tree without it every arm fails on its own line rather than the file
failing once."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import _notices as N  # noqa: E402


def shape():
    from helm import promptshape
    return promptshape


class IsNoticeTest(unittest.TestCase):
    def test_every_notice_kind_is_a_notice(self):
        self.assertTrue(shape().is_notice(N.wake("x")))      # unconditional
        for label, text in (("chat wake", N.wake("ready for a look")),
                            ("agent done", N.agent_done("all green")),
                            ("command done", N.command_done()),
                            ("leading blank", "\n  " + N.command_done())):
            with self.subTest(kind=label):
                self.assertTrue(shape().is_notice(text))

    def test_a_typed_prompt_is_not_a_notice_even_when_it_quotes_one(self):
        # CONTROL on the same predicate: the quoted envelope IS a notice when
        # it opens the prompt, so the False below is its position deciding.
        quoted = N.command_done()
        self.assertTrue(shape().is_notice(quoted))
        for text in ("review the parser change",
                     "why did this fire? " + quoted,
                     "", None, 42):
            with self.subTest(text=text):
                self.assertFalse(shape().is_notice(text))


class SubstanceTest(unittest.TestCase):
    def test_a_chat_wake_keeps_author_and_row_text_only(self):
        text = N.wake("the parser rewrite is ready for a look", waiting=2)
        got = shape().substance(text)
        self.assertEqual(got, "peer-seat: the parser rewrite is ready for a look")

    def test_the_fixed_wrapper_words_are_gone(self):
        got = shape().substance(N.wake("ready"))
        for fixed in ("Monitor event", "PushNotification", "benign",
                      "task-id", "bfixture01", "inbox beacon", "[helm chat",
                      "waiting"):
            with self.subTest(fixed=fixed):
                self.assertNotIn(fixed, got)
        self.assertIn("ready", got)                       # control

    def test_an_agent_result_is_kept_and_its_envelope_dropped(self):  # noqa: VACUOUS_ASSERTION — asserted EQUAL to a non-empty body, which an empty result fails
        body = "Reviewed the lane.\nTwo findings: the cap and the latch."
        got = shape().substance(N.agent_done(body, desc="xrev of the cap"))
        self.assertEqual(got, body)

    def test_a_finished_command_has_no_substance(self):  # noqa: VACUOUS_ASSERTION — the positive twin is test_an_agent_result_is_kept_and_its_envelope_dropped on the same function; an empty result IS the claim here
        self.assertEqual(shape().substance(N.command_done("run the suite")), "")

    def test_fixed_event_lines_are_dropped_and_real_lines_kept(self):
        event = "\n".join((N.EXPIRED, N.MORE_PENDING,
                           N.wake_line("first row"), "gate: 12 modules OK"))
        got = shape().substance(N.monitor(event, desc="gate watcher"))
        self.assertEqual(got, "peer-seat: first row\ngate: 12 modules OK")

    def test_an_expiry_alone_leaves_nothing(self):  # noqa: VACUOUS_ASSERTION — an empty result IS the claim; test_fixed_event_lines_are_dropped_and_real_lines_kept is the positive control on the same event path
        self.assertEqual(shape().substance(N.monitor(N.EXPIRED)), "")

    def test_text_after_the_envelope_is_kept(self):
        text = N.command_done() + "\nalso: check the cap"
        self.assertEqual(shape().substance(text), "also: check the cap")

    def test_a_reaction_wake_keeps_its_body(self):
        line = ("[helm chat reaction #fixture-room → fixture-seat "
                "@2026-09-23T12:00:00Z] fixture-seat@2026-09-23T11:59:00Z "
                "← owner reacted +1")
        got = shape().substance(N.monitor(line))
        self.assertEqual(got, "fixture-seat@2026-09-23T11:59:00Z ← owner reacted +1")

    def test_a_dm_wake_loses_its_header(self):
        line = "[helm chat dm → fixture-seat @2026-09-23T12:00:00Z] boss: go"
        self.assertEqual(shape().substance(N.monitor(line)), "boss: go")

    def test_a_typed_prompt_comes_back_byte_identical(self):  # noqa: VACUOUS_ASSERTION — assertIs against the non-empty input object, which a rewritten or empty result fails
        """THE CONTROL THE WHOLE CHANGE IS MEASURED AGAINST."""
        for text in ("review the parser change\n\n  with care  ",
                     "why did this fire? " + N.wake("x"),
                     "", "<result>typed by a person</result>"):
            with self.subTest(text=text[:30]):
                self.assertIs(shape().substance(text), text)

    def test_a_typed_remainder_quoting_the_tag_survives_verbatim(self):  # noqa: VACUOUS_ASSERTION — asserted EQUAL to a non-empty expected string, which an empty or absent result fails
        """Only LEADING envelopes are the harness's. A typed instruction
        queued behind one may quote the tag, and must not be read as a second
        envelope and cut off at it."""
        typed = ("then grep the log for <task-notification> and report "
                 "every hit with its line")
        self.assertEqual(shape().substance(N.command_done() + "\n" + typed),
                         typed)

    def test_a_typed_remainder_quoting_a_whole_envelope_survives_verbatim(self):  # noqa: VACUOUS_ASSERTION — asserted EQUAL to a non-empty expected string, which an empty or absent result fails
        typed = ("quote this back: <task-notification><result>q</result>"
                 "</task-notification> and stop")
        self.assertEqual(
            shape().substance(N.agent_done("the body") + "\n" + typed),
            "the body\n" + typed)

    def test_back_to_back_leading_envelopes_are_all_set_aside(self):
        text = N.wake("first row") + "\n" + N.agent_done("second body")
        self.assertEqual(shape().substance(text),
                         "peer-seat: first row\nsecond body")

    def test_a_truncated_notice_still_loses_its_envelope(self):
        text = N.agent_done("kept body")
        cut = text[:text.index("<usage>")]
        self.assertEqual(shape().substance(cut), "kept body")


class FixedBodyTextTest(unittest.TestCase):
    """task/2978: the harness also writes fixed sentences INSIDE the bodies
    substance keeps. MEASURED on 1,284 joined turns (E2 audit): 90 agent
    notices carried only "This agent's report was delivered to you ..." and
    27 only "This agent has not reported yet ...", and 14 Monitor events only
    "[N events suppressed ...]". Each is the same bytes on every delivery, so
    a store entry keyed on "delivered" or "report" fired on all of them."""

    def test_the_report_delivered_sentence_is_not_substance(self):  # noqa: VACUOUS_ASSERTION — an empty result IS the claim; test_a_real_result_beside_a_fixed_sentence_is_kept is the positive twin on the same result path
        self.assertEqual(shape().substance(N.agent_done(N.DELIVERED)), "")

    def test_the_not_reported_yet_sentence_is_not_substance(self):  # noqa: VACUOUS_ASSERTION — an empty result IS the claim; test_a_real_result_beside_a_fixed_sentence_is_kept is the positive twin on the same result path
        self.assertEqual(shape().substance(N.agent_done(N.NOT_YET)), "")

    def test_a_real_result_beside_a_fixed_sentence_is_kept(self):  # noqa: VACUOUS_ASSERTION — asserted EQUAL to a non-empty body, which an empty result fails
        body = "Two findings: the cap and the latch."
        got = shape().substance(N.agent_done(N.DELIVERED + "\n" + body))
        self.assertEqual(got, body)

    def test_a_fixed_sentence_quoted_mid_line_is_content(self):  # noqa: VACUOUS_ASSERTION — asserted EQUAL to a non-empty body, which an empty result fails
        """Anchored at the line start: an agent QUOTING the sentence wrote it."""
        body = "the harness said: " + N.DELIVERED
        self.assertEqual(shape().substance(N.agent_done(body)), body)

    def test_a_suppressed_events_line_is_not_substance(self):  # noqa: VACUOUS_ASSERTION — the first assert is EQUAL to a non-empty wake line through the same substance call; the empty result after it IS the claim
        got = shape().substance(N.monitor(N.SUPPRESSED + "\n"
                                          + N.wake_line("first row")))
        self.assertEqual(got, "peer-seat: first row")
        self.assertEqual(shape().substance(N.monitor(N.SUPPRESSED)), "")


class HandbackTest(unittest.TestCase):
    """task/2978: a subagent's hand-back arrives as <agent-message>, a second
    harness envelope (50 of 1,284 joined turns). Its frame paragraph is the
    same 520 bytes every time; the report under it is the substance."""

    def test_the_frame_is_dropped_and_the_report_kept(self):  # noqa: VACUOUS_ASSERTION — asserted EQUAL to a non-empty body, which an empty result fails
        report = "## Result\nThe cap moved to six.\n\nLane: fixture-lane"
        self.assertEqual(shape().substance(N.handback(report)), report)

    def test_a_plain_agent_message_keeps_its_body(self):
        self.assertEqual(shape().substance(N.agent_message("CLEAN at the tip")),
                         "CLEAN at the tip")

    def test_text_after_the_envelope_is_kept_verbatim(self):
        text = N.handback("report body") + "\nalso: check the cap"
        self.assertEqual(shape().substance(text),
                         "report body\nalso: check the cap")

    def test_a_typed_prompt_quoting_a_handback_is_byte_identical(self):  # noqa: VACUOUS_ASSERTION — assertIs against the non-empty input object, which a rewritten or empty result fails
        text = "why did this arrive? " + N.handback("x")
        self.assertIs(shape().substance(text), text)


class NoticeKindTest(unittest.TestCase):
    """task/2978: a notice whose substance is empty can still MEAN something
    fixed. A Monitor expiry is the check-in tick, and the rule about it
    (re-arm) must reach the seat by the notice's kind, never by whichever
    entry happens to carry the words "monitor" or "expired"."""

    def test_a_monitor_expiry_is_a_kind(self):  # noqa: VACUOUS_ASSERTION — asserted EQUAL to a one-member frozenset, which an empty result fails
        self.assertEqual(shape().notice_kinds(N.monitor(N.EXPIRED)),
                         frozenset({shape().MONITOR_EXPIRED}))
        busy = N.monitor(N.wake_line("first row") + "\n" + N.EXPIRED)
        self.assertEqual(shape().notice_kinds(busy),
                         frozenset({shape().MONITOR_EXPIRED}))

    def test_other_turns_have_no_kind(self):
        self.assertTrue(shape().notice_kinds(N.monitor(N.EXPIRED)))  # control
        for text in (N.wake("the cap is ready"), N.agent_done("x"),
                     N.command_done(), N.handback("x"),
                     "why? " + N.monitor(N.EXPIRED), "", None):
            with self.subTest(text=str(text)[:30]):
                self.assertEqual(shape().notice_kinds(text), frozenset())


if __name__ == "__main__":
    unittest.main()
