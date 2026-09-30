#!/usr/bin/env python3
"""Review-done carries an exact MELD-DIFF receipt through the existing verdict door.

Every dispatch, meld post and verdict lives in DispatchBase's synthetic home.
"""
import os
import shlex
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import chat, dispatches, eventledger, meld, review_done, review_door  # noqa: E402
from tests.test_dispatches import DispatchBase, run  # noqa: E402


DIFF = ("diff --git a/helm/example.py b/helm/example.py\n"
        "--- a/helm/example.py\n"
        "+++ b/helm/example.py\n"
        "@@ -1 +1 @@\n"
        "-old behavior\n"
        "+fixed behavior\n")


class ReviewDoneDiffHandoffTest(DispatchBase):
    def setUp(self):
        super().setUp()
        self.row = self.add(ref=self.b, recipient="seat-b", pair_meld={})
        self.forge_open(self.row["id"], review_mode="MELD-DIFF")
        self.row = dispatches.snapshot()[0][self.row["id"]]
        self.room, _key = review_door.pair_room(self.row)
        seeds = meld.seeds(chat.read_checked(self.room, 0)[0])
        self.assertTrue(seeds)
        self.epoch = seeds[-1][0]

    def post(self, text=DIFF, who="seat-b"):
        message = chat.post("[MELD e:%d] %s" % (self.epoch, text),
                            room=self.room, who=who, sign=False)
        return self.room + "/" + message["id"]

    def done(self, outcome, *flags):
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "seat-b"}), \
                self.verdict_author():
            return run(review_done.cmd_review, [
                "done", self.row["id"], outcome, "focused reviewer finding",
                *flags])

    def events(self):
        return [e for e in eventledger.events(dispatches.ledger_path())
                if e.get("id") == self.row["id"] and e.get("event") == "verdict"]

    def assert_refused(self, outcome, *flags, contains):
        before = len(eventledger.events(dispatches.ledger_path()))
        rc, _out, err = self.done(outcome, *flags)
        self.assertIn(rc, (1, 2), err)
        self.assertIn(contains, err)
        self.assertEqual(len(eventledger.events(dispatches.ledger_path())), before)
        self.assertEqual(self.events(), [])
        return err

    def fix_flags(self, ref):
        return ("--finding-count", "1", "--prior-relation", "new",
                "--worse-than-main", "state", "--no-patch-because",
                "author applies exact reviewer diff", "--diff-handoff", ref)

    def test_fix_forwards_exact_post_and_preserves_receipt_on_replay(self):
        ref = self.post()
        rc, _out, err = self.done("fix", *self.fix_flags(ref))
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.events()), 1)
        stored = self.events()[0]["diff_handoff"]
        self.assertEqual((stored["room"], stored["msg_id"],
                          stored["reviewed_tip"]),
                         (self.room, ref.rsplit("/", 1)[1], self.row["tip"]))
        replay = dispatches.snapshot()[0][self.row["id"]]
        self.assertEqual(replay["diff_handoff"], stored)
        self.assertTrue(dispatches._has_diff_handoff(replay))

    def test_missing_or_malformed_post_ref_is_refused_by_verdict_door(self):
        for ref in (self.room + "/missing-message-id", "not-a-meld/123"):
            with self.subTest(ref=ref):
                self.assert_refused("fix", *self.fix_flags(ref),
                                    contains="--diff-handoff")
        ref = self.post("not a unified diff")
        err = self.assert_refused("fix", *self.fix_flags(ref),
                                  contains="post contains no exact unified diff")
        self.assertIn("--diff-handoff '<PAIR-ROOM/MSGID>'",
                      err.rsplit("corrected: ", 1)[-1])

    def test_clean_does_not_record_a_citation_or_turn_it_into_hold_prose(self):
        ref = self.post()
        err = self.assert_refused("clean", "--diff-handoff", ref,
                                  contains="clean records a source-clean hold")
        self.assertNotIn("--diff-handoff", err.rsplit("corrected: ", 1)[-1])
        self.assertEqual(dispatches.snapshot()[0][self.row["id"]]["status"],
                         "open")

    def test_concur_and_patch_fix_cannot_spend_a_diff_handoff(self):
        ref = self.post()
        self.assert_refused("concur", "--diff-handoff", ref,
                            contains="--diff-handoff requires FIX")
        self.assert_refused("fix", "--finding-count", "1", "--prior-relation",
                            "new", "--worse-than-main", "state", "--patch-tip",
                            self.c, "--diff-handoff", ref,
                            contains="--diff-handoff requires FIX")

    def test_patch_mode_cannot_gain_meld_diff_receipt_through_review_done(self):
        ref = self.post()
        self.forge_open(self.row["id"], review_mode="PATCH")
        self.assert_refused("fix", *self.fix_flags(ref),
                            contains="requires this row's MELD-DIFF mode")

    def test_duplicate_diff_flag_refuses_before_any_verdict_write(self):
        ref = self.post()
        err = self.assert_refused("fix", *self.fix_flags(ref),
                                  "--diff-handoff", ref,
                                  contains="--diff-handoff may be given once")
        line = err.rsplit("corrected: ", 1)[-1].strip()
        self.assertIn("--diff-handoff '<PAIR-ROOM/MSGID>'", line)
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "seat-b"}), \
                self.verdict_author():
            rc, _out, err = run(review_done.cmd_review, shlex.split(line)[2:])
        self.assertEqual(rc, 2, err)
        self.assertIn("still a placeholder", err)
        self.assertEqual(self.events(), [])

    def test_correction_after_missing_count_keeps_the_receipt_and_pastes(self):
        ref = self.post()
        err = self.assert_refused(
            "fix", "--worse-than-main", "state", "--no-patch-because",
            "author applies exact reviewer diff", "--diff-handoff", ref,
            contains="finding-count")
        line = err.rsplit("corrected: ", 1)[-1].strip()
        self.assertIn("--diff-handoff " + shlex.quote(ref), line)
        line = line.replace("'<N>'", "1").replace(
            "'<new|uncured|regression-of-cure>'", "new")
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "seat-b"}), \
                self.verdict_author():
            rc, _out, err = run(review_done.cmd_review, shlex.split(line)[2:])
        self.assertEqual(rc, 0, err)
        self.assertTrue(dispatches._has_diff_handoff(
            dispatches.snapshot()[0][self.row["id"]]))

    def test_dispatch_verdict_correction_does_not_drop_typed_receipt(self):
        ref = self.post()
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "seat-b"}), \
                self.verdict_author():
            rc, _out, err = run(dispatches.cmd_dispatch, [
                "verdict", self.row["id"], self.row["tip"], "--fix",
                "--measured", "--worse-than-main", "state",
                "--no-patch-because", "author applies exact reviewer diff",
                "--diff-handoff", ref, "focused reviewer finding"])
        self.assertEqual(rc, 2, err)
        self.assertIn("--diff-handoff " + shlex.quote(ref), err)
        self.assertEqual(self.events(), [])


if __name__ == "__main__":
    unittest.main()
