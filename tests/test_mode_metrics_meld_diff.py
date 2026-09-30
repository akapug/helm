#!/usr/bin/env python3
"""D2 MELD-DIFF confirmation sends in the mode census and dispatch ledger."""
import os
import shutil
import tempfile
import time
import unittest
import uuid

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from helm import dispatches, eventledger, review_door  # noqa: E402


class ModeMetricsMeldDiffTest(unittest.TestCase):
    CHAIN = "a" * 32
    FIRST = "1" * 40
    CURE = "2" * 40
    SIBLING = "3" * 40
    NEXT = "4" * 40

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-mode-diff-")
        self.home = os.environ.get("HELM_HOME")
        self.adopted = os.environ.get("HELM_ADOPTED_DIR")
        os.environ["HELM_HOME"] = self.tmp
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.makedirs(os.path.dirname(dispatches.ledger_path()), exist_ok=True)
        self.current, self.accepted = {}, {}
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

    def send(self, tip, parent=None, mode=None, recipient="reader", ledger=False,
             sent_at=None, applied=False):
        rid = self.CHAIN if parent is None else uuid.uuid4().hex
        row = {"v": 3, "seq": 0, "event": "dispatch", "status": "open",
               "id": rid, "ts": sent_at or time.strftime(
                   "%Y-%m-%dT%H:%M:%SZ",
                   time.gmtime(time.time() - 300 + self.tick * 10)),
               "kind": "review", "chain_root": self.CHAIN, "repo_id": "/repo",
               "sender": "author", "recipient": recipient, "lane": "mode-diff",
               "deadline_s": 2700, "tip": tip}
        if parent:
            row["supersedes"] = parent["id"]
        if mode:
            row["review_mode"] = mode
        if applied:
            receipt = self.current[parent["id"]]["diff_handoff"]
            row["diff_application"] = {
                "v": 1, "parent_id": parent["id"],
                "parent_tip": parent["tip"], "child_tip": tip,
                "receipt_sha256": receipt["sha256"]}
        self.current[rid] = row
        self.accepted[rid] = [row]
        self.tick += 1
        if ledger:
            self.assertTrue(eventledger.append(dispatches.ledger_path(), row))
        return row

    def fix(self, row, *, reason="author applies the exact diff", ledger=False,
            handoff=None):
        event = {"v": 3, "seq": 1, "event": "verdict", "id": row["id"],
                 "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                     time.gmtime(time.time() - 300 + self.tick * 10)),
                 "reviewed_tip": row["tip"], "polarity": "fix",
                 "verdict_ref": "mode-diff:0123456789abcdef | finding"}
        if reason is not None:
            event["no_patch_because"] = reason
        if handoff is None:
            handoff = row.get("review_mode") == "MELD-DIFF"
        if handoff and reason is not None:
            event["review_mode"] = "MELD-DIFF"
            event["diff_handoff"] = {
                "v": 1, "room": "meld-0-pair-local-test", "msg_id": "f" * 12,
                "epoch": 1, "reviewed_tip": row["tip"], "sha256": "a" * 64}
        if ledger:
            self.assertTrue(eventledger.append(dispatches.ledger_path(), event))
        self.current[row["id"]] = dispatches._apply(row, event)
        self.accepted[row["id"]].append(event)
        self.tick += 1

    def clean(self, row, *, ledger=False):
        event = {"v": 3, "seq": 1, "event": "hold", "id": row["id"],
                 "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                     time.gmtime(time.time() - 300 + self.tick * 10)),
                 "source_clean_tip": row["tip"], "hold_actor": row["recipient"],
                 "reason": "clean confirmation"}
        if ledger:
            self.assertTrue(eventledger.append(dispatches.ledger_path(), event))
        self.current[row["id"]] = dispatches._apply(row, event)
        self.accepted[row["id"]].append(event)
        self.tick += 1

    def metrics(self, current=None, accepted=None):
        result = review_door.mode_metrics(
            self.current if current is None else current,
            self.accepted if accepted is None else accepted, cutoff=0)
        self.assertEqual(len(result["chains"]), 1)
        return result["chains"][0]

    def test_synthetic_direct_cure_and_fanout_confirm_not_another_round(self):
        first = self.send(self.FIRST, mode="MELD-DIFF")
        self.fix(first)
        cure = self.send(self.CURE, first, applied=True)  # verified cure
        cure["sender"] = "transferred-author"  # continuation can change hands
        self.send(self.CURE, first)  # another send at the same tip is fan-out
        self.clean(cure)  # the confirmation is answered, but is not a new round
        result = self.metrics()
        self.assertEqual((result["mode"], result["active_review_rounds"],
                          result["cure_cycles"]), ("MELD-DIFF", 1, 1))
        self.assertEqual(result["enrollment_round"], 1)

    def test_synthetic_cure_fix_is_a_real_round_and_second_cure(self):
        first = self.send(self.FIRST, mode="MELD-DIFF")
        self.fix(first)
        cure = self.send(self.CURE, first, applied=True)
        self.fix(cure)
        result = self.metrics()
        self.assertEqual((result["active_review_rounds"], result["cure_cycles"]),
                         (2, 2))

    def test_synthetic_second_sibling_and_indirect_successor_are_not_free(self):
        first = self.send(self.FIRST, mode="MELD-DIFF")
        self.fix(first)
        cure = self.send(self.CURE, first, applied=True)
        sibling = self.send(self.SIBLING, first)
        self.fix(sibling)
        indirect = self.send(self.NEXT, cure)
        self.fix(indirect)
        result = self.metrics()
        self.assertEqual((result["active_review_rounds"], result["cure_cycles"]),
                         (3, 3))

    def test_synthetic_no_cure_reason_does_not_exempt_answered_child(self):
        first = self.send(self.FIRST, mode="MELD-DIFF")
        self.fix(first, reason="could not produce a cure; no diff exists",
                 handoff=False)
        child = self.send(self.CURE, first)
        self.clean(child)
        self.assertEqual(self.metrics()["active_review_rounds"], 2)

    def test_synthetic_unproven_diff_and_independent_fixture_fix(self):
        first = self.send(self.FIRST, mode="MELD-DIFF")
        self.fix(first, reason=None)
        unproven = self.send(self.CURE, first)
        self.fix(unproven)
        self.assertEqual(self.metrics()["active_review_rounds"], 2)

        # An independent reader's fixture FIX at the confirmation tip must
        # not make the enrolled reader's clean confirmation a second round.
        self.current.clear()
        self.accepted.clear()
        first = self.send(self.FIRST, mode="MELD-DIFF")
        self.fix(first)
        cure = self.send(self.CURE, first, applied=True)
        self.clean(cure)
        other = self.send(self.CURE, first, recipient="another-reader")
        self.fix(other)
        result = self.metrics()
        self.assertEqual((result["active_review_rounds"], result["cure_cycles"]),
                         (1, 1))

    def test_retracted_fix_does_not_exempt_the_answered_successor(self):
        first = self.send(self.FIRST, mode="MELD-DIFF")
        self.fix(first)
        self.current[first["id"]]["verdict_retracted"] = True
        cure = self.send(self.CURE, first)
        self.clean(cure)
        result = self.metrics()
        self.assertEqual((result["active_review_rounds"], result["cure_cycles"]),
                         (1, 0))

    def test_successor_from_another_repo_is_not_a_cure(self):
        first = self.send(self.FIRST, mode="MELD-DIFF")
        self.fix(first)
        cure = self.send(self.CURE, first)
        cure["repo_id"] = "/another-repo"
        self.clean(cure)
        result = self.metrics()
        self.assertEqual((result["active_review_rounds"], result["cure_cycles"]),
                         (2, 1))

    def test_ledger_child_before_fix_remains_second_active_round(self):
        first = self.send(self.FIRST, mode="MELD-DIFF", ledger=True)
        child = self.send(self.CURE, first, ledger=True)
        self.fix(first, ledger=True)
        self.clean(child, ledger=True)
        state, _raw, accepted, _verdicts, unavailable = \
            dispatches.snapshot_and_events()
        self.assertIsNone(unavailable)
        self.assertEqual(self.metrics(state, accepted)["active_review_rounds"], 2)
        info, err = dispatches.chain_rounds("author", child["id"], self.CURE,
                                           snap=(state, None))
        self.assertIsNone(err)
        self.assertFalse(info["adopted_patch"])
        self.assertEqual(info["rounds_after"], 2)

    def test_unrelated_direct_child_without_application_proof_is_another_round(self):
        first = self.send(self.FIRST, mode="MELD-DIFF", ledger=True)
        self.fix(first, ledger=True)
        unrelated = self.send(self.CURE, first, ledger=True)
        self.clean(unrelated, ledger=True)
        state, _raw, accepted, _verdicts, unavailable = \
            dispatches.snapshot_and_events()
        self.assertIsNone(unavailable)
        self.assertNotIn("diff_application", state[unrelated["id"]])
        self.assertEqual(self.metrics(state, accepted)["active_review_rounds"], 2)

    def test_synthetic_same_second_child_is_not_a_proven_cure(self):
        first = self.send(self.FIRST, mode="MELD-DIFF")
        self.fix(first)
        child = self.send(self.CURE, first,
                          sent_at=self.current[first["id"]]["verdict_ts"])
        self.clean(child)
        self.assertEqual(self.metrics()["active_review_rounds"], 2)

    def test_same_second_child_with_proof_is_not_another_mode_round(self):
        first = self.send(self.FIRST, mode="MELD-DIFF", ledger=True)
        self.fix(first, ledger=True)
        cure = self.send(self.CURE, first, applied=True, ledger=True,
                         sent_at=self.current[first["id"]]["verdict_ts"])
        self.clean(cure, ledger=True)
        state, _raw, accepted, _verdicts, unavailable = \
            dispatches.snapshot_and_events()
        self.assertIsNone(unavailable)
        self.assertTrue(dispatches._has_applied_diff(
            state[cure["id"]], state[first["id"]]))
        self.assertEqual(self.metrics(state, accepted)["active_review_rounds"], 1)
        info, err = dispatches.chain_rounds("author", cure["id"], self.CURE,
                                           snap=(state, None))
        self.assertIsNone(err)
        self.assertEqual(info["rounds_after"], 1)

    def test_ledger_snapshot_agrees_with_answered_direct_cure(self):
        first = self.send(self.FIRST, mode="MELD-DIFF", ledger=True)
        self.fix(first, ledger=True)
        cure = self.send(self.CURE, first, ledger=True, applied=True)
        self.clean(cure, ledger=True)
        state, _raw, accepted, _verdicts, unavailable = \
            dispatches.snapshot_and_events()
        self.assertIsNone(unavailable)
        result = self.metrics(state, accepted)
        self.assertEqual((result["active_review_rounds"], result["cure_cycles"]),
                         (1, 1))
        info, err = dispatches.chain_rounds("author", first["id"], self.CURE,
                                           snap=(state, None))
        self.assertIsNone(err)
        self.assertTrue(info["adopted_patch"])
        self.assertEqual(info["rounds_after"], 1)


if __name__ == "__main__":
    unittest.main()
