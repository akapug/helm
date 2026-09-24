"""A verdict from a seat that is not the row's recipient names the recipient.

task/2992: a land-authorizing verdict binds its author to the row's RECIPIENT.
When another seat recorded one, the door refused with "no exact runtime record
for author session ...", which sent two seats hunting a runtime problem that
did not exist. The refusal itself is correct and stays; only its words change.
"""
import os
from unittest import mock

from helm import dispatches
from tests.test_dispatches import DispatchBase

UNAVAILABLE = "verdict author session/family proof is unavailable: "


class NonRecipientVerdictMessageTest(DispatchBase):
    def refuse(self, caller):
        row = self.add(recipient="reviewer", ref=self.side)
        env = {"CLAUDE_CODE_SESSION_ID": "caller-session-without-a-record"}
        with mock.patch.dict(os.environ, env):
            if caller is None:
                os.environ.pop("HELM_CHAT_NAME", None)
            else:
                os.environ["HELM_CHAT_NAME"] = caller
            verdict, err = dispatches.mark_verdict(
                row["id"], self.side, "reviewed exact tip", polarity="approve",
                basis="measured", bind_author=True)
        self.assertIsNone(verdict)
        self.assertIsInstance(err, str)
        return row, err

    def test_a_non_recipient_is_told_whose_row_it_is(self):
        row, err = self.refuse("other-seat")
        self.assertTrue(err.startswith(
            "this row is addressed to @reviewer, not @other-seat"), err)
        self.assertIn("--supersedes %s" % row["id"], err)
        self.assertIn(dispatches._REVIEWER_MODEL_FLAG, err)
        # The real reason is kept, never replaced.
        self.assertIn("Underlying: " + UNAVAILABLE, err)

    def test_the_recipient_keeps_the_underlying_reason(self):
        _row, err = self.refuse("reviewer")
        self.assertTrue(err.startswith(UNAVAILABLE), err)
        self.assertNotIn("addressed to", err)

    def test_recipient_match_ignores_case(self):
        _row, err = self.refuse("Reviewer")
        self.assertTrue(err.startswith(UNAVAILABLE), err)

    def test_an_unknown_caller_keeps_the_underlying_reason(self):
        _row, err = self.refuse(None)
        self.assertTrue(err.startswith(UNAVAILABLE), err)
        self.assertNotIn("addressed to", err)

    def test_the_recipient_with_a_proof_still_records(self):
        row = self.add(recipient="reviewer", ref=self.side)
        with self.verdict_author(), mock.patch.dict(
                os.environ, {"HELM_CHAT_NAME": "other-seat"}), \
                mock.patch.object(dispatches.gate, "bind",
                                  return_value=("VERIFIED", "b" * 16, "test receipt")):
            verdict, err = dispatches.mark_verdict(
                row["id"], self.side, "reviewed exact tip", polarity="approve",
                basis="measured", bind_author=True)
        # The session proof decides authority, not the chat name: a passing
        # proof records exactly as before this change.
        self.assertIsNone(err, err)
        self.assertEqual(verdict["verdict_version"], 4)
