#!/usr/bin/env python3
"""Shared MELD-DIFF cure-round counting through the real dispatch ledger fold."""
import os
import shutil
import tempfile
import time
import unittest
import uuid

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from helm import dispatches, eventledger  # noqa: E402


class DiffCureRoundTest(unittest.TestCase):
    SENDER = "spiral-author"
    READER = "spiral-reader"
    CHAIN = "a" * 32
    FIRST = "1" * 40
    CURE = "2" * 40
    NEXT = "3" * 40
    PATCH = "4" * 40

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-diff-spiral-")
        self.home = os.environ.get("HELM_HOME")
        self.adopted = os.environ.get("HELM_ADOPTED_DIR")
        os.environ["HELM_HOME"] = self.tmp
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.makedirs(os.path.dirname(dispatches.ledger_path()), exist_ok=True)
        self.rows = []
        self.tick = 0

    def tearDown(self):
        if self.home is None:
            os.environ.pop("HELM_HOME", None)
        else:
            os.environ["HELM_HOME"] = self.home
        if self.adopted is None:
            os.environ.pop("HELM_ADOPTED_DIR", None)
        else:
            os.environ["HELM_ADOPTED_DIR"] = self.adopted
        shutil.rmtree(self.tmp)

    def record(self, event):
        self.assertTrue(eventledger.append(dispatches.ledger_path(), event))
        self.tick += 1

    def send(self, tip, parent=None, mode=None, sender=SENDER, sent_at=None,
             applied=False):
        rid = self.CHAIN if parent is None else uuid.uuid4().hex
        row = {"v": 3, "seq": 0, "event": "dispatch", "status": "open",
               "id": rid, "ts": sent_at or time.strftime(
                   "%Y-%m-%dT%H:%M:%SZ",
                   time.gmtime(time.time() - 300 + self.tick * 10)),
               "tip": tip, "kind": "review", "chain_root": self.CHAIN,
               "sender": sender, "recipient": self.READER, "lane": "diff-cure",
               "deadline_s": 2700}
        if parent:
            row["supersedes"] = parent
        if mode:
            row["review_mode"] = mode
        if applied:
            parent_row = self.observed()[parent]
            row["diff_application"] = {
                "v": 1, "parent_id": parent,
                "parent_tip": parent_row["tip"], "child_tip": tip,
                "receipt_sha256": parent_row["diff_handoff"]["sha256"]}
        self.record(row)
        self.rows.append(row)
        return row

    def fix(self, row, *, patch=None, reason=None, handoff=None):
        event = {"v": 3, "seq": 1, "event": "verdict", "id": row["id"],
                 "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                     time.gmtime(time.time() - 300 + self.tick * 10)),
                 "reviewed_tip": row["tip"], "polarity": "fix",
                 "verdict_ref": "diff-cure:0123456789abcdef | finding"}
        if reason is not None:
            event["no_patch_because"] = reason
        if handoff:
            event["review_mode"] = "MELD-DIFF"
            event["diff_handoff"] = {
                "v": 1, "room": "meld-0-pair-local-test", "msg_id": "f" * 12,
                "epoch": 1, "reviewed_tip": row["tip"], "sha256": "a" * 64}
        if patch:
            event.update(patch_tip=patch, patch_author=self.READER)
        self.record(event)
        projected, err = dispatches.snapshot()
        self.assertIsNone(err)
        self.assertEqual(projected[row["id"]]["status"], "verdict")
        return projected

    def clean(self, row):
        self.record({"v": 3, "seq": 1, "event": "hold", "id": row["id"],
                     "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "source_clean_tip": row["tip"],
                     "hold_actor": row["recipient"], "reason": "clean read"})

    def observed(self):
        projected, err = dispatches.snapshot()
        self.assertIsNone(err)
        self.assertEqual(len(projected), len(self.rows))
        return projected

    def door(self, parent, tip, sender=SENDER):
        info, err = dispatches.chain_rounds(sender, parent["id"], tip,
                                            snap=(self.observed(), None))
        self.assertIsNone(err)
        return info

    def stop(self):
        info, err = dispatches.review_spiral(self.SENDER,
                                             snap=(self.observed(), None))
        self.assertIsNone(err)
        return info

    def test_mode_diff_requires_the_authors_advancing_direct_successor(self):
        first = self.send(self.FIRST, mode="MELD-DIFF")
        self.fix(first, reason="author applies the exact mechanical diff", handoff=True)
        self.assertEqual(self.door(first, self.NEXT)["rounds_after"], 2)
        self.assertEqual(self.door(first, self.FIRST)["rounds_after"], 1)
        self.assertFalse(self.door(first, self.CURE, sender="another-author")
                         ["adopted_patch"], "an unsent tip has no application proof")
        self.assertIsNone(self.stop(), "a FIX alone must not invent a cure tip")
        cure = self.send(self.CURE, parent=first["id"], applied=True)
        self.assertEqual((self.door(first, self.NEXT)["rounds_after"],
                          self.door(cure, self.NEXT)["rounds_after"]), (2, 2))
        self.assertEqual(self.door(cure, self.CURE)["rounds_after"], 1)
        self.assertEqual(self.door(cure, self.CURE)["fan_out"], True)
        self.assertIsNone(self.stop(), "the open cure confirmation is not round 2")
        # Two earlier answered rounds establish a live spiral. The shared door
        # and stop views must then both exempt the SAME direct cure send.
        self.fix(cure, reason="still broken")
        self.assertEqual(self.door(cure, self.NEXT)["rounds_after"], 3)
        self.assertEqual(self.stop()["rounds"], 2,
                         "a FIX on the cure restores its real round")

    def test_child_dispatched_before_fix_is_not_retroactive_cure(self):
        first = self.send(self.FIRST, mode="MELD-DIFF")
        child = self.send(self.CURE, parent=first["id"])
        self.fix(first, reason="later exact diff", handoff=True)
        self.clean(child)
        self.assertFalse(self.door(child, self.CURE)["adopted_patch"])
        self.assertEqual(self.door(child, self.CURE)["rounds_after"], 2)
        self.assertNotIn(self.CURE.lower(), dispatches._adopted_patch_tips(
            {r["tip"]: [r] for r in self.observed().values()}))

    def test_same_second_child_fails_closed_without_cross_row_order(self):
        first = self.send(self.FIRST, mode="MELD-DIFF")
        projected = self.fix(first, reason="exact diff", handoff=True)
        child = self.send(self.CURE, parent=first["id"],
                          sent_at=projected[first["id"]]["verdict_ts"])
        self.clean(child)
        self.assertFalse(self.door(child, self.CURE)["adopted_patch"])
        self.assertEqual(self.door(child, self.CURE)["rounds_after"], 2)

    def test_same_second_proven_child_is_a_cure(self):
        first = self.send(self.FIRST, mode="MELD-DIFF")
        projected = self.fix(first, reason="exact diff", handoff=True)
        child = self.send(self.CURE, parent=first["id"], applied=True,
                          sent_at=projected[first["id"]]["verdict_ts"])
        self.clean(child)
        self.assertTrue(dispatches._has_applied_diff(
            self.observed()[child["id"]], self.observed()[first["id"]]))
        self.assertTrue(self.door(child, self.CURE)["adopted_patch"])
        self.assertEqual(self.door(child, self.CURE)["rounds_after"], 1)

    def test_two_real_rounds_diff_cure_then_fix_restores_third(self):
        first = self.send(self.FIRST, mode="MELD-DIFF")
        self.fix(first, reason="author applies the diff", handoff=True)
        second = self.send(self.CURE, parent=first["id"], mode="MELD-DIFF",
                           applied=True)
        self.fix(second, reason="author applies a second diff", handoff=True)
        info = self.door(second, self.NEXT)
        self.assertEqual((info["rounds_before"], info["rounds_after"],
                          info["adopted_patch"], info["new_round"]),
                         (2, 3, False, True))
        third = self.send(self.NEXT, parent=second["id"], mode="MELD-DIFF",
                          applied=True)
        self.assertEqual(self.stop()["rounds"], 2)
        self.fix(third, reason="third fix", handoff=True)
        self.assertEqual(self.stop()["rounds"], 3)
        self.assertEqual(self.door(third, self.PATCH)["rounds_after"], 4)

    def test_a_second_advancing_sibling_is_not_another_free_cure(self):
        first = self.send(self.FIRST, mode="MELD-DIFF")
        self.fix(first, reason="author applies a single cure", handoff=True)
        self.send(self.CURE, parent=first["id"], applied=True)
        info = self.door(first, self.NEXT)
        self.assertFalse(info["adopted_patch"])
        self.assertEqual(info["rounds_after"], 2)
        self.send(self.NEXT, parent=first["id"])
        self.assertEqual(self.stop()["rounds"], 2)

    def test_patch_and_unrelated_no_patch_remain_unchanged(self):
        first = self.send(self.FIRST, mode="PATCH")
        self.fix(first, patch=self.PATCH)
        self.assertEqual(self.door(first, self.PATCH)["rounds_after"], 1)
        self.assertEqual(self.door(first, self.CURE)["rounds_after"], 2)
        cure = self.send(self.CURE, parent=first["id"])
        self.assertEqual(self.stop()["rounds"], 2)
        self.assertFalse(self.door(cure, self.NEXT)["adopted_patch"])

    def test_non_diff_no_patch_and_unproven_diff_do_not_exempt(self):
        first = self.send(self.FIRST, mode="PATCH")
        self.fix(first, reason="design finding without a patch")
        self.assertEqual(self.door(first, self.CURE)["rounds_after"], 2)
        self.assertFalse(self.door(first, self.CURE)["adopted_patch"])
        child = self.send(self.CURE, parent=first["id"])
        self.assertEqual(self.stop()["rounds"], 2)
        self.fix(child)  # no no_patch_because cannot assert an applied cure
        self.assertFalse(self.door(child, self.NEXT)["adopted_patch"])
        self.assertEqual(self.door(child, self.NEXT)["rounds_after"], 3)

    def test_diff_no_cure_reason_does_not_exempt_child(self):
        first = self.send(self.FIRST, mode="MELD-DIFF")
        self.fix(first, reason="could not produce a cure; no diff exists")
        child = self.send(self.CURE, parent=first["id"])
        self.clean(child)
        self.assertFalse(self.door(child, self.CURE)["adopted_patch"])
        self.assertEqual(self.door(child, self.CURE)["rounds_after"], 2)

    def test_diff_without_no_patch_reason_has_no_author_applied_cure(self):
        first = self.send(self.FIRST, mode="MELD-DIFF")
        self.fix(first)
        self.assertFalse(self.door(first, self.CURE)["adopted_patch"])
        cure = self.send(self.CURE, parent=first["id"])
        self.assertEqual(self.stop()["rounds"], 2)
        self.fix(cure)
        self.assertEqual(self.door(cure, self.NEXT)["rounds_after"], 3)

    def test_unrelated_first_direct_child_cannot_become_an_applied_cure(self):
        first = self.send(self.FIRST, mode="MELD-DIFF")
        self.fix(first, reason="reader posted a diff", handoff=True)
        unrelated = self.send(self.CURE, parent=first["id"])
        self.clean(unrelated)
        self.assertFalse(self.door(unrelated, self.CURE)["adopted_patch"])
        self.assertEqual(self.door(unrelated, self.CURE)["rounds_after"], 2)
        later = self.send(self.NEXT, parent=first["id"], applied=True)
        self.assertFalse(self.door(later, self.NEXT)["adopted_patch"],
                         "a second child cannot replace an unproven first")

    def test_cross_sender_direct_cure_is_shared_but_indirect_tip_is_not(self):
        first = self.send(self.FIRST, mode="MELD-DIFF")
        self.fix(first, reason="author applies the diff", handoff=True)
        transferred = self.send(self.CURE, parent=first["id"],
                                sender="another-author", applied=True)
        self.assertTrue(self.door(transferred, self.CURE,
                                  sender="another-author")["adopted_patch"])
        self.assertEqual(self.door(first, self.NEXT)["rounds_after"], 2,
                         "a second direct child is not another free cure")
        self.assertIsNone(self.stop(), "the prior sender did not open a new round")
        indirect = self.send(self.NEXT, parent=transferred["id"])
        self.assertEqual(self.stop()["rounds"], 2,
                         "an indirect send cannot borrow the first FIX")
        self.assertFalse(self.door(indirect, self.PATCH)["adopted_patch"])


if __name__ == "__main__":
    unittest.main()
