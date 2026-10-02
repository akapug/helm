#!/usr/bin/env python3
"""MELD-DIFF's FIX receipt binds an actual reader-posted diff to its pair round.

Every chat row and verdict is written under DispatchBase's synthetic HELM_HOME;
no live room, seat, credential, or dispatch ledger is consulted.
"""
import hashlib
import os
import re
import subprocess
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from helm import chat, dispatches, dispatches_tier, eventledger, gate, gateimport, home, meld, review_door, seats, store, tasks  # noqa: E402
from tests.test_dispatches import DispatchBase, run  # noqa: E402


DIFF = ("diff --git a/helm/example.py b/helm/example.py\n"
        "--- a/helm/example.py\n"
        "+++ b/helm/example.py\n"
        "@@ -1 +1 @@\n"
        "-old behavior\n"
        "+fixed behavior\n")


class DiffHandoffTest(DispatchBase):
    READER = "seat-b"
    REASON = "author applies exact reviewer diff"

    def setUp(self):
        super().setUp()
        self.row = self.add(ref=self.b, recipient=self.READER, pair_meld={})
        self.forge_open(self.row["id"], review_mode="MELD-DIFF")
        self.row = dispatches.snapshot()[0][self.row["id"]]
        self.room, _key = review_door.pair_room(self.row)
        self.assertTrue(review_door.is_pair_room(self.room))
        rows, _count, fault = chat.read_checked(self.room, 0)
        self.assertIsNone(fault)
        seeds = meld.seeds(rows)
        self.assertTrue(seeds, "dispatch must open a real pair-meld round")
        self.epoch = seeds[-1][0]
        self.assertIn("opening-row=" + self.row["id"][:12], seeds[-1][2])

    def post(self, text=DIFF, *, who=READER, epoch=None, room=None):
        return chat.post("[MELD e:%d] %s" % (
            self.epoch if epoch is None else epoch, text),
            room=room or self.room, who=who, sign=False)

    def verdict(self, handoff, *, rid=None, tip=None, polarity="fix",
                reason=REASON):
        return dispatches.mark_verdict(
            rid or self.row["id"], tip or self.row["tip"],
            "reviewer posted a focused mechanical correction",
            polarity=polarity, basis="measured", finding_count=1,
            prior_relation="new", no_patch_because=reason,
            diff_handoff=handoff)

    def assert_refused(self, handoff, *, contains=None, **kwargs):
        before = len(eventledger.events(dispatches.ledger_path()))
        out, why = self.verdict(handoff, **kwargs)
        self.assertIsNone(out, why)
        self.assertIsNotNone(why)
        if contains:
            self.assertIn(contains, why)
        self.assertEqual(len(eventledger.events(dispatches.ledger_path())), before)
        self.assertEqual(dispatches.snapshot()[0][self.row["id"]]["status"],
                         "open")
        return why

    def test_real_diff_receipt_survives_ledger_replay_and_identical_retry(self):
        msg = self.post()
        handoff = self.room + "/" + msg["id"]
        out, why = self.verdict(handoff)
        self.assertIsNone(why)
        self.assertEqual(out["status"], "verdict")
        expected = {"v": 1, "room": self.room, "msg_id": msg["id"],
                    "epoch": self.epoch, "reviewed_tip": self.row["tip"],
                    "sha256": hashlib.sha256(msg["text"].encode("utf-8")).hexdigest()}
        self.assertEqual(out["diff_handoff"], expected)
        accepted = eventledger.events(dispatches.ledger_path())
        self.assertEqual(accepted[-1]["diff_handoff"], expected)
        replay, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable)
        self.assertEqual(replay[self.row["id"]]["diff_handoff"], expected)
        self.assertTrue(dispatches._has_diff_handoff(replay[self.row["id"]]))
        retry, why = self.verdict(handoff)
        self.assertIsNone(why)
        self.assertEqual(retry["diff_handoff"], expected)
        self.assertEqual(eventledger.events(dispatches.ledger_path()), accepted)

    def test_cli_accepts_diff_handoff_and_preserves_the_exact_chat_digest(self):
        msg = self.post()
        ref = self.room + "/" + msg["id"]
        with self.verdict_author():
            rc, _out, err = run(dispatches.cmd_dispatch, [
                "verdict", self.row["id"], self.row["tip"], "--fix",
                "--measured", "--finding-count", "1", "--prior-relation",
                "new", "--worse-than-main", "state",
                "--no-patch-because", self.REASON, "--diff-handoff", ref,
                "focused diff handed to author"])
        self.assertEqual(rc, 0, err)
        receipt = dispatches.snapshot()[0][self.row["id"]]["diff_handoff"]
        self.assertEqual((receipt["room"], receipt["msg_id"], receipt["epoch"]),
                         (self.room, msg["id"], self.epoch))

    def test_malformed_or_missing_message_reference_never_mints_a_verdict(self):
        self.post()
        for ref in ("", self.room, self.room + "/", "/missing",
                    self.room + "/missing-message-id", "not-a-meld/123",
                    self.room + "/abc/extra"):
            with self.subTest(ref=ref):
                self.assert_refused(ref)

    def test_another_authors_diff_and_a_message_without_diff_are_not_handoffs(self):
        for msg in (self.post(who="stranger"),
                    self.post("I reviewed the tip; author should fix the harm."),
                    self.post("--- a/helm/example.py\n+++ b/helm/example.py\n"
                              "The following is only an example, not a patch.\n"
                              "@@ -1 +1 @@\n-old behavior\n+fixed behavior\n"),
                    self.post("--- a/helm/example.py\n+++ b/helm/example.py\n"
                              "@@ -3,2 +3,2 @@\n-old behavior\n+fixed behavior\n")):
            with self.subTest(message=msg["id"]):
                self.assert_refused(self.room + "/" + msg["id"])
        self.assertIsNone(self.verdict(self.room + "/" + self.post()["id"])[1])

    def test_another_rows_pair_meld_is_not_this_rows_receipt(self):
        work, why = tasks.add("another review handoff", "integrator",
                              project="helm", force_new=True)
        self.assertIsNone(why, why)
        other = self.add(ref=self.c, recipient=self.READER, task=work["id"])
        room, _key = review_door.pair_room(other)
        self.assertNotEqual(room, self.room)
        epoch, _who, _seed = meld.latest_seed(chat.read(room)[0])
        msg = self.post(room=room, epoch=epoch)
        self.assert_refused(room + "/" + msg["id"])

    def test_diff_in_a_different_round_cannot_answer_this_round(self):
        msg = self.post(epoch=self.epoch + 1)
        self.assert_refused(self.room + "/" + msg["id"])
        msg = self.post(epoch=self.epoch)
        self.assertIsNone(self.verdict(self.room + "/" + msg["id"])[1])

    def test_another_rows_round_in_the_same_pair_room_is_not_accepted(self):
        # A forged later seed can never lend its row's round to this row. The
        # nearest prior seed, not the room name alone, owns the message.
        other = "f" * 12
        later = self.epoch + 1
        chat.post("[MELD e:%d] PROBLEM: opening-row=%s opening-chain=%s | "
                  "chain/%s focused review row %s at %s | convener=%s "
                  "invited=%s cap=5 recv-timeout=90s | discipline [HOLD]"
                  % (later, other, self.row["chain_root"][:12],
                     self.row["chain_root"][:12], other, self.row["tip"][:12],
                     self.row["sender"], self.READER),
                  room=self.room, who=self.row["sender"], sign=False)
        msg = self.post(epoch=later)
        self.assert_refused(self.room + "/" + msg["id"])

    def test_wrong_reviewed_tip_is_refused_even_with_real_diff(self):
        msg = self.post()
        self.assert_refused(self.room + "/" + msg["id"], tip=self.c,
                            contains="stale")

    def test_unreadable_chat_refuses_instead_of_treating_room_as_empty(self):
        msg = self.post()
        with open(chat.room_path(self.room), "ab") as f:
            f.write(b"\xff\n")
        self.assert_refused(self.room + "/" + msg["id"])

    def test_forged_receipts_do_not_replay_as_verdicts(self):
        msg = self.post()
        receipt = {"v": 1, "room": self.room, "msg_id": msg["id"],
                   "epoch": self.epoch, "reviewed_tip": self.row["tip"],
                   "sha256": hashlib.sha256(msg["text"].encode()).hexdigest()}
        for bad in ({"sha256": "not-a-digest"}, {"epoch": True}):
            with self.subTest(bad=bad):
                fake = dict(receipt, **bad)
                forged = {"v": 3, "event": "verdict", "seq": 1,
                          "id": self.row["id"], "ts": self.row["ts"],
                          "polarity": "fix", "review_mode": "MELD-DIFF",
                          "reviewed_tip": self.row["tip"],
                          "verdict_ref": "forged fix claim",
                          "no_patch_because": self.REASON,
                          "diff_handoff": fake}
                self.assertFalse(dispatches._has_diff_handoff(forged))
                self.assertIs(dispatches._apply(self.row, forged), self.row)
                self.assertTrue(eventledger.append(dispatches.ledger_path(),
                                                   forged))
                replay, unavailable = dispatches.snapshot()
                self.assertIsNone(unavailable)
                self.assertEqual(replay[self.row["id"]]["status"], "open")
                self.assertFalse(dispatches._has_diff_handoff(
                    replay[self.row["id"]]))

    def test_first_direct_child_applies_exact_diff_and_mutated_tip_does_not(self):
        self.git("checkout", "-q", "-b", "first-diff-cure", self.b)
        with open(os.path.join(self.repo, "state"), encoding="utf-8") as f:
            before = f.read()
        with open(os.path.join(self.repo, "state"), "w", encoding="utf-8") as f:
            f.write(before.replace("b\n", "first applied cure\n"))
        patch = self.git("diff", "--", "state") + "\n"
        self.git("add", "state")
        self.git("commit", "-q", "-m", "first applied cure")
        tip = self.git("rev-parse", "HEAD")
        msg = self.post(patch)
        parent, why = self.verdict(self.room + "/" + msg["id"])
        self.assertIsNone(why)
        first = self.add(ref=tip, recipient=self.READER,
                         supersedes=parent["id"])
        self.assertTrue(dispatches._has_applied_diff(first, parent))
        self.assertEqual(first["diff_application"]["child_tip"], tip)
        self.assertEqual(first["diff_application"]["receipt_sha256"],
                         parent["diff_handoff"]["sha256"])
        # A tip that alters something else may descend from the same parent,
        # but cannot reverse the posted patch on its own committed tree.
        self.git("checkout", "-q", "-b", "first-wrong-cure", self.b)
        wrong = self.commit_file("different", "other change")
        self.assertIsNone(dispatches._diff_application(
            dict(first, tip=wrong), parent, dispatches.snapshot()[0]))

    def test_inline_meld_marker_in_added_content_stays_in_exact_patch(self):
        self.git("checkout", "-q", "-b", "inline-marker-cure", self.b)
        with open(os.path.join(self.repo, "state"), encoding="utf-8") as f:
            before = f.read()
        with open(os.path.join(self.repo, "state"), "w", encoding="utf-8") as f:
            f.write(before.replace("b\n", "fixed [DONE]\n"))
        patch = self.git("diff", "--", "state") + "\n"
        self.assertIn("+fixed [DONE]\n", patch)
        self.git("add", "state")
        self.git("commit", "-q", "-m", "exact inline-marker cure")
        cure_tip = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "-b", "dropped-inline-marker", self.b)
        with open(os.path.join(self.repo, "state"), "w", encoding="utf-8") as f:
            f.write(before.replace("b\n", "fixed\n"))
        self.git("add", "state")
        self.git("commit", "-q", "-m", "different cure without marker")
        wrong_tip = self.git("rev-parse", "HEAD")
        msg = self.post(patch + "[DONE]")
        parent, why = self.verdict(self.room + "/" + msg["id"])
        self.assertIsNone(why)
        wrong = dict(self.row, tip=wrong_tip, supersedes=parent["id"])
        self.assertIsNone(dispatches._diff_application(
            wrong, parent, dispatches.snapshot()[0]))
        correct = self.add(ref=cure_tip, recipient=self.READER,
                           supersedes=parent["id"])
        self.assertTrue(dispatches._has_applied_diff(correct, parent))

    def test_meld_say_inline_marker_proves_only_its_framed_diff(self):
        self.git("checkout", "-q", "-b", "inline-protocol-cure", self.b)
        with open(os.path.join(self.repo, "state"), encoding="utf-8") as f:
            before = f.read()
        with open(os.path.join(self.repo, "state"), "w", encoding="utf-8") as f:
            f.write(before.replace("b\n", "fixed [DONE]\n"))
        patch = self.git("diff", "--", "state")
        self.assertTrue(patch.endswith("+fixed [DONE]"))
        self.git("add", "state")
        self.git("commit", "-q", "-m", "framed inline cure")
        tip = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "-b", "inline-protocol-wrong", self.b)
        with open(os.path.join(self.repo, "state"), "w", encoding="utf-8") as f:
            f.write(before.replace("b\n", "fixed\n"))
        self.git("add", "state")
        self.git("commit", "-q", "-m", "wrong inline cure")
        wrong_tip = self.git("rev-parse", "HEAD")
        meld.join(self.room, seat=self.READER)
        meld.say(self.room, "YIELD", patch, seat=self.READER)
        msg = chat.read(self.room)[0][-1]
        self.assertEqual(msg["meld_marker"], "YIELD")
        self.assertTrue(msg["text"].endswith("+fixed [DONE] [YIELD]"))
        parent, why = self.verdict(self.room + "/" + msg["id"])
        self.assertIsNone(why)
        self.assertEqual(parent["diff_handoff"]["framing"], "YIELD")
        wrong = dict(self.row, tip=wrong_tip, supersedes=parent["id"])
        self.assertIsNone(dispatches._diff_application(
            wrong, parent, dispatches.snapshot()[0]))
        cure = self.add(ref=tip, recipient=self.READER,
                        supersedes=parent["id"])
        self.assertTrue(dispatches._has_applied_diff(cure, parent))

    def test_untagged_inline_marker_is_ambiguous_and_not_a_cure(self):  # noqa: VACUOUS_ASSERTION — tagged positive control in test_meld_say_inline_marker_proves_only_its_framed_diff
        self.assert_untagged_ending_proves_nothing(" [DONE]",
                                                   "fixed behavior [DONE]")

    def test_glued_untagged_marker_is_ambiguous_and_not_a_cure(self):  # noqa: VACUOUS_ASSERTION — tagged positive control in test_meld_say_inline_marker_proves_only_its_framed_diff
        # `"[MELD e:N] $(git diff)[DONE]"`: the shell drops the diff's last
        # newline, and meld._MARKER_RE still reads the glued suffix as DONE.
        self.assert_untagged_ending_proves_nothing("[DONE]",
                                                   "fixed behavior[DONE]")

    def test_untagged_marker_before_trailing_newline_is_not_a_cure(self):  # noqa: VACUOUS_ASSERTION — tagged positive control in test_meld_say_inline_marker_proves_only_its_framed_diff
        self.assert_untagged_ending_proves_nothing(" [DONE]\n",
                                                   "fixed behavior [DONE]")

    def assert_untagged_ending_proves_nothing(self, ending, literal_line):
        self.git("checkout", "-q", "-b", "untagged-inline-cure", self.b)
        with open(os.path.join(self.repo, "state"), encoding="utf-8") as f:
            before = f.read()
        with open(os.path.join(self.repo, "state"), "w", encoding="utf-8") as f:
            f.write(before.replace("b\n", "fixed behavior\n"))
        patch = self.git("diff", "--", "state")
        self.git("add", "state")
        self.git("commit", "-q", "-m", "untagged inline cure")
        tip = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "-b", "untagged-inline-literal", self.b)
        with open(os.path.join(self.repo, "state"), "w", encoding="utf-8") as f:
            f.write(before.replace("b\n", literal_line + "\n"))
        self.git("add", "state")
        self.git("commit", "-q", "-m", "literal suffix cure")
        literal_tip = self.git("rev-parse", "HEAD")
        msg = self.post(patch + ending)
        parent, why = self.verdict(self.room + "/" + msg["id"])
        self.assertIsNone(why)
        for candidate in (tip, literal_tip):
            with self.subTest(candidate=candidate):
                child = dict(self.row, tip=candidate,
                             supersedes=parent["id"])
                self.assertIsNone(dispatches._diff_application(
                    child, parent, dispatches.snapshot()[0]))

    def test_child_opener_proves_actual_patch_but_not_unrelated_direct_tip(self):  # noqa: VACUOUS_ASSERTION — source and reverse patch checks, minted cure proof and unrelated refusal are positive controls
        self.git("checkout", "-q", "-b", "diff-cure", self.b)
        with open(os.path.join(self.repo, "state"), encoding="utf-8") as f:
            before = f.read()
        with open(os.path.join(self.repo, "state"), "w", encoding="utf-8") as f:
            f.write(before.replace("b\n", "fixed behavior\n"))
        patch = self.git("diff", "--", "state") + "\n"
        self.assertIn("+fixed behavior", patch)
        self.assertTrue(dispatches._index_accepts_patch(self.repo, self.b, patch),
                        "source diff must apply to reviewed parent tree")
        with mock.patch.dict(os.environ, {
                "GIT_DIR": "/synthetic/foreign-repo",
                "GIT_INDEX_FILE": "/synthetic/foreign-index"}):
            self.assertTrue(dispatches._index_accepts_patch(
                self.repo, self.b, patch),
                "private index and repository must outrank ambient selectors")
        self.git("add", "state")
        self.git("commit", "-q", "-m", "applied exact diff")
        cure_tip = self.git("rev-parse", "HEAD")
        self.assertTrue(dispatches._index_accepts_patch(self.repo, cure_tip,
                                                       patch, reverse=True),
                        "source diff must reverse on candidate cure tree")
        self.git("checkout", "-q", "-b", "diff-unrelated", self.b)
        unrelated_tip = self.commit_file("unrelated", "different direct child")
        msg = self.post(patch)
        parent, why = self.verdict(self.room + "/" + msg["id"])
        self.assertIsNone(why)
        with mock.patch.dict(os.environ, {"GIT_DIR": "/synthetic/foreign-repo"}):
            self.assertIsNotNone(dispatches._diff_application(
                dict(self.row, tip=cure_tip, supersedes=parent["id"]),
                parent, dispatches.snapshot()[0]),
                "ancestor check must read the child repository, not GIT_DIR")
        unrelated = self.add(ref=unrelated_tip, recipient=self.READER,
                             supersedes=parent["id"])
        self.assertNotIn("diff_application", unrelated)
        self.assertFalse(dispatches._has_applied_diff(unrelated, parent))
        # A second sibling is still checked and may carry proof, but the
        # chronological first-child fold will not retroactively waive its round.
        cure = self.add(ref=cure_tip, recipient=self.READER,
                        supersedes=parent["id"], force=True)
        receipt = parent["diff_handoff"]
        expected = {"v": 1, "parent_id": parent["id"],
                    "parent_tip": parent["tip"], "child_tip": cure_tip,
                    "receipt_sha256": receipt["sha256"]}
        self.assertEqual(cure.get("diff_application"), expected)
        replay, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable)
        self.assertEqual(replay[cure["id"]]["diff_application"], expected)
        self.assertTrue(dispatches._has_applied_diff(replay[cure["id"]],
                                                     replay[parent["id"]]))
        self.assertFalse(dispatches._has_applied_diff(replay[unrelated["id"]],
                                                      replay[parent["id"]]))
        # Even a direct child claiming the right parent cannot manufacture
        # application with malformed typed bytes; the opener fold strips it.
        opener = next(e for e in eventledger.events(dispatches.ledger_path())
                      if e.get("event") == "dispatch" and e["id"] == cure["id"])
        forged = dict(opener, diff_application=dict(expected, receipt_sha256="x"))
        self.assertNotIn("diff_application", dispatches._new_state(forged))
        self.assertFalse(dispatches._has_applied_diff(forged, parent))

    def test_stripped_final_blank_context_is_not_an_exact_hunk(self):
        """The hunk counts the lone-space line. strip() deletes it, so the
        same bytes the door would see after a naive say are not a diff."""
        patch = self._blank_context_patch()
        framed = "[MELD e:1] %s [YIELD]" % patch.strip()
        self.assertFalse(dispatches._exact_diff(framed))
        self.assertTrue(dispatches._exact_diff(
            "[MELD e:1] %s\n [YIELD]" % patch.rstrip("\n")))

    def test_meld_say_keeps_final_blank_context_and_the_child_applies_it(self):
        # The reviewed tip has to already contain the blank line, or the
        # posted hunk cannot apply. setUp's row points at self.b, which
        # does not, so this round is its own row.
        self.git("checkout", "-q", "-b", "blank-apply", self.b)
        path = os.path.join(self.repo, "blankctx")
        with open(path, "w", encoding="utf-8") as f:
            f.write("old\n\n")
        self.git("add", "blankctx")
        self.git("commit", "-q", "-m", "blank context base")
        base = self.git("rev-parse", "HEAD")
        self.row = self.add(ref=base, recipient=self.READER, pair_meld={})
        self.forge_open(self.row["id"], review_mode="MELD-DIFF")
        self.row = dispatches.snapshot()[0][self.row["id"]]
        self.room, _key = review_door.pair_room(self.row)
        rows, _count, fault = chat.read_checked(self.room, 0)
        self.assertIsNone(fault)
        self.epoch = meld.seeds(rows)[-1][0]
        with open(path, "w", encoding="utf-8") as f:
            f.write("new\n\n")
        proc = subprocess.run(
            ["git", "-C", self.repo, "diff", "--", "blankctx"],
            capture_output=True, text=True, check=True)
        patch = proc.stdout
        self.assertTrue(patch.endswith(" \n"), repr(patch[-24:]))
        self.git("add", "blankctx")
        self.git("commit", "-q", "-m", "blank context cure")
        tip = self.git("rev-parse", "HEAD")
        meld.join(self.room, seat=self.READER)
        meld.say(self.room, "YIELD", patch, seat=self.READER)
        msg = chat.read(self.room)[0][-1]
        self.assertEqual(msg["meld_marker"], "YIELD")
        self.assertTrue(msg["text"].endswith("\n \n [YIELD]"), msg["text"][-40:])
        self.assertTrue(dispatches._exact_diff(msg["text"]))
        parent, why = self.verdict(self.room + "/" + msg["id"])
        self.assertIsNone(why, why)
        cure = self.add(ref=tip, recipient=self.READER, supersedes=parent["id"])
        self.assertTrue(dispatches._has_applied_diff(cure, parent))

    def _blank_context_patch(self):
        """A one-hunk cure whose last context line is a single space.

        DispatchBase.git strips stdout, which is the same deletion under
        test, so the bytes come from git directly."""
        self.git("checkout", "-q", "-b", "blank-context", self.b)
        path = os.path.join(self.repo, "blankctx")
        with open(path, "w", encoding="utf-8") as f:
            f.write("old\n\n")
        self.git("add", "blankctx")
        self.git("commit", "-q", "-m", "blank context base")
        with open(path, "w", encoding="utf-8") as f:
            f.write("new\n\n")
        proc = subprocess.run(
            ["git", "-C", self.repo, "diff", "--", "blankctx"],
            capture_output=True, text=True, check=True)
        patch = proc.stdout
        self.assertTrue(patch.endswith(" \n"), repr(patch[-24:]))
        self.assertIn("\n@@ -1,2 +1,2 @@\n-old\n+new\n \n", patch)
        return patch

    def test_meld_say_keeps_final_added_trailing_space_and_the_child_applies_it(self):
        """A final added line may end in a significant space.

        kept.strip() deletes that space and the floor marker glues on, so
        the posted hunk is a different patch. _exact_diff still returns
        true for the stripped hunk; only the child's reverse apply catches
        it. The reviewed tip has to already contain the preimage.
        """
        self.git("checkout", "-q", "-b", "added-space-apply", self.b)
        path = os.path.join(self.repo, "addedsp")
        with open(path, "w", encoding="utf-8") as f:
            f.write("old\n")
        self.git("add", "addedsp")
        self.git("commit", "-q", "-m", "added space base")
        base = self.git("rev-parse", "HEAD")
        self.row = self.add(ref=base, recipient=self.READER, pair_meld={})
        self.forge_open(self.row["id"], review_mode="MELD-DIFF")
        self.row = dispatches.snapshot()[0][self.row["id"]]
        self.room, _key = review_door.pair_room(self.row)
        rows, _count, fault = chat.read_checked(self.room, 0)
        self.assertIsNone(fault)
        self.epoch = meld.seeds(rows)[-1][0]
        with open(path, "w", encoding="utf-8") as f:
            f.write("new \n")
        # DispatchBase.git strips stdout, which is the same deletion under
        # test, so the bytes come from git directly.
        proc = subprocess.run(
            ["git", "-C", self.repo, "diff", "--", "addedsp"],
            capture_output=True, text=True, check=True)
        patch = proc.stdout
        self.assertTrue(patch.endswith("+new \n"), repr(patch[-24:]))
        self.git("add", "addedsp")
        self.git("commit", "-q", "-m", "added space cure")
        tip = self.git("rev-parse", "HEAD")
        meld.join(self.room, seat=self.READER)
        meld.say(self.room, "YIELD", patch, seat=self.READER)
        msg = chat.read(self.room)[0][-1]
        self.assertEqual(msg["meld_marker"], "YIELD")
        self.assertTrue(msg["text"].endswith("+new  [YIELD]"), msg["text"][-48:])
        self.assertTrue(dispatches._exact_diff(msg["text"]))
        parent, why = self.verdict(self.room + "/" + msg["id"])
        self.assertIsNone(why, why)
        cure = self.add(ref=tip, recipient=self.READER, supersedes=parent["id"])
        self.assertTrue(dispatches._has_applied_diff(cure, parent))

    def test_non_fix_and_non_meld_diff_rows_cannot_claim_a_diff_handoff(self):
        msg = self.post()
        handoff = self.room + "/" + msg["id"]
        self.assert_refused(handoff, polarity="approve", reason=None)
        self.assert_refused(handoff, polarity="supersede", reason=None)
        self.forge_open(self.row["id"], review_mode="PATCH")
        self.assert_refused(handoff)
        self.forge_open(self.row["id"], review_mode="MELD-DIFF")
        self.assertIsNone(self.verdict(handoff)[1])


class DiffAppliedTest(DiffHandoffTest):
    """`helm dispatch applied <parent> <tip>` — task/3937, 0.3.3 gate 3.

    An author whose applied MELD-DIFF cure is UNCHANGED (patch-id equal to the
    posted diff, tip^ the reviewed tip, an OK focused fab receipt) records ONE
    `diff-applied` event on the PARENT row: no child row. The pair-meld wake
    is suppressed only for current, proven reviewer authority. A CHANGED cure
    refuses and prints the delta child instead.
    """

    def _cure(self, replacement="fixed behavior", message="applied exact diff"):
        """Commit the posted exact diff off the reviewed tip; -> (tip, patch)."""
        self.git("checkout", "-q", "-b", "applied-cure", self.b)
        with open(os.path.join(self.repo, "state"), encoding="utf-8") as f:
            before = f.read()
        with open(os.path.join(self.repo, "state"), "w", encoding="utf-8") as f:
            f.write(before.replace("b\n", replacement + "\n"))
        patch = self.git("diff", "--", "state") + "\n"
        self.git("add", "state")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD"), patch

    def _fab_receipt(self):
        return {"status": "OK", "focused": True}

    def applied(self, parent, tip, *, fab="fab-receipt-1"):
        return dispatches.mark_applied(parent["id"], tip, fab_receipt=fab)

    def test_focused_binding_without_witnessed_run_cannot_record_applied_authority(self):
        cure_tip, patch = self._cure()
        msg = self.post(patch)
        parent, why = self.verdict(self.room + "/" + msg["id"])
        self.assertIsNone(why, why)
        before = eventledger.events(dispatches.ledger_path())
        focused = {"v": gate.FOCUSED_VERSION, "head": cure_tip}
        with mock.patch.object(gate, "bind", return_value=(
                "VERIFIED", "assembled", "bound")), \
                mock.patch.object(gate, "by_id", return_value=(focused, None)), \
                mock.patch.object(gateimport, "land_provenance", return_value=(
                    False, "no authenticated door placed it")):
            out, why = self.applied(parent, cure_tip)
        self.assertIsNone(out)
        self.assertIn("NEED_FOCUSED binding alone cannot prove a run", why)
        self.assertEqual(eventledger.events(dispatches.ledger_path()), before)
        for origin in ("local mint: helm's own runner", "generic import",
                       "unrecognized origin"):
            with self.subTest(origin=origin), \
                    mock.patch.object(gate, "bind", return_value=(
                        "VERIFIED", "assembled", "bound")), \
                    mock.patch.object(gate, "by_id", return_value=(focused, None)), \
                    mock.patch.object(gateimport, "land_provenance",
                                      return_value=(True, origin)):
                out, why = self.applied(parent, cure_tip)
                self.assertIsNone(out)
                self.assertIn("no witnessed Fab completion or routed run", why)
        self.assertEqual(eventledger.events(dispatches.ledger_path()), before)
        with mock.patch.object(gate, "bind", return_value=(
                "VERIFIED", "witnessed", "bound")), \
                mock.patch.object(gate, "by_id", return_value=(focused, None)), \
                mock.patch.object(gateimport, "land_provenance", return_value=(
                    True, gateimport.LAND_FAB + ": job observed")):
            out, why = self.applied(parent, cure_tip)
        self.assertIsNone(why, why)
        self.assertEqual(out["diff_applied"]["tip"], cure_tip)
        self.assertEqual(len(eventledger.events(dispatches.ledger_path())),
                         len(before) + 1)

    def test_an_unchanged_applied_cure_records_one_event_on_the_parent_mints_no_child_row_and_the_reviewers_mention_count_is_unchanged(self):
        cure_tip, patch = self._cure()
        msg = self.post(patch)
        parent, why = self.verdict(self.room + "/" + msg["id"])
        self.assertIsNone(why, why)
        room_posts = chat.read(self.room)[0]
        mentions = [m for m in room_posts
                    if "@" + self.READER in (m.get("text") or "")]
        ledger_before = eventledger.events(dispatches.ledger_path())
        with mock.patch.object(dispatches, "_focused_fab_receipt",
                               return_value=(self._fab_receipt(), None)):
            out, why = self.applied(parent, cure_tip)
        self.assertIsNone(why, why)
        self.assertEqual(out["id"], parent["id"])
        # ONE event on the PARENT row, and it is the only new ledger row.
        after = eventledger.events(dispatches.ledger_path())
        self.assertEqual(len(after), len(ledger_before) + 1)
        event = after[-1]
        self.assertEqual(event["event"], "diff-applied")
        self.assertEqual(event["id"], parent["id"])
        self.assertEqual(event["tip"], cure_tip)
        self.assertEqual(event["receipt_sha256"],
                         parent["diff_handoff"]["sha256"])
        self.assertTrue(re.fullmatch(r"[0-9a-f]{40}\Z", event["patch_id"]))
        self.assertEqual(event["fab"], "fab-receipt-1")
        # NO child row minted: the ledger holds no new dispatch event.
        self.assertEqual(
            [e["id"] for e in after if e.get("event") == "dispatch"],
            [e["id"] for e in ledger_before if e.get("event") == "dispatch"])
        # THE REVIEWER'S MENTION COUNT IS UNCHANGED.
        room_posts = chat.read(self.room)[0]
        self.assertEqual(
            [m["id"] for m in room_posts
             if "@" + self.READER in (m.get("text") or "")],
            [m["id"] for m in mentions])
        # THE EVENT SURVIVES REPLAY and the parent stays verdicted, not held.
        replay, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable)
        row = replay[parent["id"]]
        self.assertEqual(row["status"], "verdict")
        self.assertEqual(row["diff_applied"]["tip"], cure_tip)
        self.assertEqual(row["diff_applied"]["patch_id"], event["patch_id"])
        self.assertEqual(row["diff_applied"]["receipt_sha256"],
                         parent["diff_handoff"]["sha256"])
        self.assertEqual(row["diff_applied"]["fab"], "fab-receipt-1")

    def test_a_diff_applied_event_binds_the_witnessed_fab_receipt_own_id(self):  # noqa: VACUOUS_ASSERTION — the unchanged-cure test above is the positive control on the same event record
        """task/4114 item 6: the diff-applied event records the focused fab
        receipt's OWN content id, not just the citing ref, so the event names
        WHICH witnessed run it spent. With `gate.by_id` resolving a receipt that
        carries an `id`, that id lands on the event and survives replay into the
        row's `diff_applied`."""
        cure_tip, patch = self._cure()
        msg = self.post(patch)
        parent, why = self.verdict(self.room + "/" + msg["id"])
        self.assertIsNone(why, why)
        focused = {"v": gate.FOCUSED_VERSION, "head": cure_tip,
                   "id": "abc123def456"}
        with mock.patch.object(dispatches, "_focused_fab_receipt",
                               return_value=({"v": gate.FOCUSED_VERSION,
                                              "status": "OK", "focused": True,
                                              "ref": "gate:" + msg["id"],
                                              "id": "abc123def456"}, None)), \
                mock.patch.object(gate, "by_id",
                                  return_value=(focused, None)):
            out, why = self.applied(parent, cure_tip)
        self.assertIsNone(why, why)
        self.assertEqual(out["diff_applied"]["receipt_id"], "abc123def456")
        # POSITIVE CONTROL on the same record: the event also recorded normally.
        self.assertTrue(re.fullmatch(r"[0-9a-f]{40}\Z",
                                     out["diff_applied"]["patch_id"]))
        self.assertEqual(out["diff_applied"]["fab"], "fab-receipt-1")
        # THE RECEIPT ID SURVIVES REPLAY into the row's diff_applied record.
        replay = dispatches.snapshot()[0][parent["id"]]
        self.assertEqual(replay["diff_applied"]["receipt_id"], "abc123def456")

    def test_a_forged_opener_carrying_diff_applied_projects_no_applied_cure(self):  # noqa: VACUOUS_ASSERTION — the real event replayed on the same row is the positive control on the same observable
        """Only a validated `diff-applied` EVENT may project `diff_applied`.
        A hand-edited opener carrying the key must neither confirm an applied
        tip nor block the author's real record (the chain-task precedent)."""
        cure_tip, patch = self._cure()
        msg = self.post(patch)
        self.forge_open(self.row["id"], diff_applied={
            "tip": cure_tip, "patch_id": "0" * 40, "receipt_sha256": "0" * 64,
            "fab": "forged"})
        parent, why = self.verdict(self.room + "/" + msg["id"])
        self.assertIsNone(why, why)
        replayed = dispatches.snapshot()[0][parent["id"]]
        self.assertNotIn("diff_applied", replayed)
        self.assertEqual(dispatches.applied_tip(replayed), "")
        with mock.patch.object(dispatches, "_focused_fab_receipt",
                               return_value=(self._fab_receipt(), None)):
            out, why = self.applied(parent, cure_tip)
        self.assertIsNone(why, why)
        self.assertEqual(out["diff_applied"]["fab"], "fab-receipt-1")
        self.assertEqual(dispatches.snapshot()[0][parent["id"]]
                         ["diff_applied"]["receipt_sha256"],
                         parent["diff_handoff"]["sha256"])

    def test_repeating_an_applied_cure_does_not_append_an_unreplayable_event(self):  # noqa: VACUOUS_ASSERTION — the first event and replayed proof are positive controls on the same ledger
        cure_tip, patch = self._cure()
        msg = self.post(patch)
        parent, why = self.verdict(self.room + "/" + msg["id"])
        self.assertIsNone(why, why)
        with mock.patch.object(dispatches, "_focused_fab_receipt",
                               return_value=(self._fab_receipt(), None)):
            first, why = self.applied(parent, cure_tip)
            self.assertIsNone(why, why)
            before = eventledger.events(dispatches.ledger_path())
            second, why = self.applied(parent, cure_tip)
        self.assertIsNone(second)
        self.assertIn("already records", why)
        self.assertEqual(eventledger.events(dispatches.ledger_path()), before)
        replay, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable)
        self.assertEqual(replay[parent["id"]]["diff_applied"],
                         first["diff_applied"])

    def test_a_cure_with_one_extra_line_refuses_and_names_the_delta_child(self):
        _cure_tip, patch = self._cure()
        msg = self.post(patch)
        parent, why = self.verdict(self.room + "/" + msg["id"])
        self.assertIsNone(why, why)
        # THE SAME CURE PLUS ONE EXTRA LINE: the diff no longer matches.
        self.git("checkout", "-q", "-b", "changed-cure", self.b)
        with open(os.path.join(self.repo, "state"), encoding="utf-8") as f:
            before = f.read()
        with open(os.path.join(self.repo, "state"), "w", encoding="utf-8") as f:
            f.write(before.replace("b\n", "fixed behavior\nextra line\n"))
        self.git("add", "state")
        self.git("commit", "-q", "-m", "changed cure")
        changed_tip = self.git("rev-parse", "HEAD")
        ledger_before = eventledger.events(dispatches.ledger_path())
        with mock.patch.object(dispatches, "_focused_fab_receipt",
                               return_value=(self._fab_receipt(), None)):
            out, why = self.applied(parent, changed_tip)
        self.assertIsNone(out)
        self.assertIsNotNone(why)
        self.assertIn("dispatch send", why)
        self.assertIn("range-diff", why)
        # NOTHING was recorded: no diff-applied event, no child row.
        self.assertEqual(eventledger.events(dispatches.ledger_path()),
                         ledger_before)
        # POSITIVE CONTROL on the same observable: the unchanged cure DOES
        # record its one event, so the refusal above is discrimination, not a
        # verb that never writes.
        with mock.patch.object(dispatches, "_focused_fab_receipt",
                               return_value=(self._fab_receipt(), None)):
            out, why = self.applied(parent, _cure_tip)
        self.assertIsNone(why, why)
        self.assertEqual(out["diff_applied"]["tip"], _cure_tip)

    def test_applied_refuses_a_tip_that_does_not_descend_directly_from_the_reviewed_tip(self):
        _cure_tip, patch = self._cure()
        msg = self.post(patch)
        parent, why = self.verdict(self.room + "/" + msg["id"])
        self.assertIsNone(why, why)
        # THE EXACT DIFF, but committed on a second commit, so tip^ != reviewed.
        self.git("checkout", "-q", "-b", "stacked-cure", self.b)
        self.commit_file("unrelated", "an unrelated intermediate commit")
        with open(os.path.join(self.repo, "state"), encoding="utf-8") as f:
            before = f.read()
        with open(os.path.join(self.repo, "state"), "w", encoding="utf-8") as f:
            f.write(before.replace("b\n", "fixed behavior\n"))
        self.git("add", "state")
        self.git("commit", "-q", "-m", "cure stacked on a second commit")
        stacked_tip = self.git("rev-parse", "HEAD")
        ledger_before = eventledger.events(dispatches.ledger_path())
        with mock.patch.object(dispatches, "_focused_fab_receipt",
                               return_value=(self._fab_receipt(), None)):
            out, why = self.applied(parent, stacked_tip)
        self.assertIsNone(out)
        self.assertIsNotNone(why)
        self.assertEqual(eventledger.events(dispatches.ledger_path()),
                         ledger_before)

    def test_applied_refuses_a_cure_that_adds_a_file_the_posted_diff_never_names(self):  # noqa: VACUOUS_ASSERTION — the unchanged-cure test records on the same fixture, the positive control on the ledger observable
        """An applied commit carrying the posted `state` hunk PLUS one extra
        unrelated file is not the posted cure. The applied diff names the
        extra file, so its patch-id already differs and the door refuses on
        that clause; the same-patch-id, different-tree case is
        test_a_same_patch_id_cure_on_a_different_tree_refuses_by_name."""
        _cure_tip, patch = self._cure()
        msg = self.post(patch)
        parent, why = self.verdict(self.room + "/" + msg["id"])
        self.assertIsNone(why, why)
        # THE SAME `state` HUNK, plus one extra file the posted diff never
        # touches.
        self.git("checkout", "-q", "-b", "wrong-tree-cure", self.b)
        with open(os.path.join(self.repo, "state"), encoding="utf-8") as f:
            before = f.read()
        with open(os.path.join(self.repo, "state"), "w", encoding="utf-8") as f:
            f.write(before.replace("b\n", "fixed behavior\n"))
        with open(os.path.join(self.repo, "other"), "w", encoding="utf-8") as f:
            f.write("an unrelated line the posted diff never mentions\n")
        self.git("add", "state", "other")
        self.git("commit", "-q", "-m", "cure with an extra unrelated file")
        wrong_tree_tip = self.git("rev-parse", "HEAD")
        ledger_before = eventledger.events(dispatches.ledger_path())
        with mock.patch.object(dispatches, "_focused_fab_receipt",
                               return_value=(self._fab_receipt(), None)):
            out, why = self.applied(parent, wrong_tree_tip)
        self.assertIsNone(out)
        self.assertIsNotNone(why)
        self.assertIn("dispatch send", why)
        # NOTHING was recorded.
        self.assertEqual(eventledger.events(dispatches.ledger_path()),
                         ledger_before)

    def test_a_same_patch_id_cure_on_a_different_tree_refuses_by_name(self):  # noqa: VACUOUS_ASSERTION — the exact cure recording on the same row is the positive control on the same ledger observable
        """task/4114 item 1, the case the door exists for: a patch-id ignores
        hunk POSITIONS, so a hunk whose 3-line context repeats in the file can
        land on the OTHER copy with a byte-identical patch-id and a DIFFERENT
        tree. The door must refuse it as a changed cure (the delta child), as
        a 2-tuple like every other refusal, and record nothing. The exact cure
        on the same row records: the private-index tree witness opens for the
        posted bytes."""
        # No line starts with a letter, so no hunk header carries a function
        # context: the two diffs then differ ONLY in line numbers and the
        # index line, which every git version's patch-id ignores.
        block = "#p\n#q\n#r\n=b\n#s\n#t\n#u\n"
        first = block.replace("=b\n", "=fixed\n") + block
        second = block + block.replace("=b\n", "=fixed\n")
        # commit_file appends its own newline: base holds block+block exactly.
        base = self.commit_file("dup", (block + block)[:-1])
        work, why = tasks.add("a duplicated block review", "integrator",
                              project="helm", force_new=True)
        self.assertIsNone(why, why)
        other = self.add(ref=base, recipient=self.READER, task=work["id"],
                         pair_meld={})
        self.forge_open(other["id"], review_mode="MELD-DIFF")
        other = dispatches.snapshot()[0][other["id"]]
        room, _key = review_door.pair_room(other)
        epoch, _who, _seed = meld.latest_seed(chat.read(room)[0])

        def commit_dup(branch, text):
            self.git("checkout", "-q", "-b", branch, base)
            with open(os.path.join(self.repo, "dup"), "w",
                      encoding="utf-8") as f:
                f.write(text)
            patch = self.git("diff", "--", "dup") + "\n"
            self.git("add", "dup")
            self.git("commit", "-q", "-m", branch)
            return self.git("rev-parse", "HEAD"), patch

        exact_tip, patch = commit_dup("dup-exact", first)
        other_tip, other_patch = commit_dup("dup-other-copy", second)
        # THE PREMISE, measured: same patch-id, different tree.
        self.assertEqual(dispatches._verbatim_patch_id(self.repo, patch),
                         dispatches._verbatim_patch_id(self.repo, other_patch))
        self.assertNotEqual(self.git("rev-parse", exact_tip + "^{tree}"),
                            self.git("rev-parse", other_tip + "^{tree}"))
        msg = self.post(patch, room=room, epoch=epoch)
        parent, why = self.verdict(room + "/" + msg["id"], rid=other["id"],
                                   tip=other["tip"])
        self.assertIsNone(why, why)
        ledger_before = eventledger.events(dispatches.ledger_path())
        with mock.patch.object(dispatches, "_focused_fab_receipt",
                               return_value=(self._fab_receipt(), None)):
            out, why = self.applied(parent, other_tip)
        self.assertIsNone(out)
        self.assertIn("the resolved tree is not the posted diff", why or "")
        self.assertIn("dispatch send", why)
        self.assertEqual(eventledger.events(dispatches.ledger_path()),
                         ledger_before)
        # POSITIVE CONTROL on the same row: the posted bytes' own tree records,
        # and the event carries the id the door measured.
        with mock.patch.object(dispatches, "_focused_fab_receipt",
                               return_value=(self._fab_receipt(), None)):
            out, why = self.applied(parent, exact_tip)
        self.assertIsNone(why, why)
        self.assertEqual(out["diff_applied"]["tip"], exact_tip)
        self.assertEqual(out["diff_applied"]["patch_id"],
                         dispatches._verbatim_patch_id(self.repo, patch))

    def test_a_user_diff_external_still_records_unchanged_applied_cure(self):  # noqa: VACUOUS_ASSERTION — a clean-applied control (no config) records on the same fixture when the external is unset
        """task/4073 item 3: the applied-cure diff is hashed as posted. A
        user's `diff.external`, `textconv` or `color.diff=always` must not
        change the bytes that reach `_verbatim_patch_id`: both `git diff`
        calls in `mark_applied` now pass --no-ext-diff --no-textconv
        --no-color, so a user hook still records an unchanged cure instead of
        refusing it.

        RED CONTROL: with `diff.external` set and the no-flag the same
        unchanged cure still records its one diff-applied event; the
        unchanged-cure path is the control, so the failure above is
        discrimination, not a verb that never writes."""
        cure_tip, patch = self._cure()
        msg = self.post(patch)
        parent, why = self.verdict(self.room + "/" + msg["id"])
        self.assertIsNone(why, why)
        self.git("config", "diff.external", "echo external-diff-called")
        self.git("config", "core.textConv", "md5sum")
        self.git("config", "color.diff", "always")
        with mock.patch.object(dispatches, "_focused_fab_receipt",
                               return_value=(self._fab_receipt(), None)):
            out, why = self.applied(parent, cure_tip)
        self.assertIsNone(why, why)
        self.assertEqual(out["diff_applied"]["tip"], cure_tip)

    def test_a_whitespace_only_change_to_the_cure_is_not_unchanged(self):  # noqa: VACUOUS_ASSERTION — the byte-exact cure recording its tip after the loop is the unconditional positive control on the same mark_applied result and ledger
        """`git patch-id --stable` strips EVERY whitespace byte, so a cure
        that re-indents a Python line, or changes the spaces inside a string,
        hashed equal to the posted diff and recorded as UNCHANGED, and the
        reviewer was never woken for a semantic change. Both sides are
        hashed --verbatim: such a cure refuses and names the delta child."""
        cure_tip, patch = self._cure()
        parent, why = self.verdict(self.room + "/" + self.post(patch)["id"])
        self.assertIsNone(why, why)
        for n, changed in enumerate(("    fixed behavior", "fixed  behavior")):
            with self.subTest(changed=changed):
                self.git("checkout", "-q", "-b", "whitespace-cure-%d" % n,
                         self.b)
                path = os.path.join(self.repo, "state")
                with open(path, encoding="utf-8") as f:
                    before = f.read()
                with open(path, "w", encoding="utf-8") as f:
                    f.write(before.replace("b\n", changed + "\n"))
                self.git("add", "state")
                self.git("commit", "-q", "-m", "whitespace-changed cure")
                tip = self.git("rev-parse", "HEAD")
                ledger_before = eventledger.events(dispatches.ledger_path())
                with mock.patch.object(dispatches, "_focused_fab_receipt",
                                       return_value=(self._fab_receipt(),
                                                     None)):
                    out, why = self.applied(parent, tip)
                self.assertIsNone(out, "a whitespace-changed cure recorded "
                                       "as the posted diff")
                self.assertIn("patch-id differs", str(why))
                self.assertIn("dispatch send", str(why))
                self.assertEqual(eventledger.events(dispatches.ledger_path()),
                                 ledger_before)
        # POSITIVE CONTROL on the same observable: the byte-exact cure still
        # records, so the refusals above are discrimination, not a door that
        # stopped opening once both sides hash verbatim.
        with mock.patch.object(dispatches, "_focused_fab_receipt",
                               return_value=(self._fab_receipt(), None)):
            out, why = self.applied(parent, cure_tip)
        self.assertIsNone(why, why)
        self.assertEqual(out["diff_applied"]["tip"], cure_tip)

    def test_a_retired_row_takes_no_diff_applied(self):  # noqa: VACUOUS_ASSERTION — the live row TAKING the same event (assertIsNot plus the folded tip, by value) is asserted unconditionally before the retired arm's identity
        """THE REDUCER'S RETIREMENT GUARD COVERS THE KIND. `diff-applied` sits
        outside test_lr_retire's transplant matrix (its event binds this
        row's handoff receipt, which no fresh twin carries), so its
        inertness after a retirement is pinned here: the same event the
        live row takes is returned unapplied once the row reads retired."""
        cure_tip, patch = self._cure()
        parent, why = self.verdict(self.room + "/" + self.post(patch)["id"])
        self.assertIsNone(why, why)
        state = dispatches.snapshot()[0][parent["id"]]
        with mock.patch.object(dispatches, "_focused_fab_receipt",
                               return_value=(self._fab_receipt(), None)):
            _out, why = self.applied(parent, cure_tip)
        self.assertIsNone(why, why)
        event = [e for e in eventledger.events(dispatches.ledger_path())
                 if e.get("id") == parent["id"]
                 and e.get("event") == "diff-applied"][-1]
        taken = dispatches._apply(state, event)
        self.assertIsNot(taken, state, "the live row refused the event, so "
                                       "the retired arm below measures nothing")
        self.assertEqual(taken["diff_applied"]["tip"], cure_tip)
        with mock.patch.object(dispatches, "_retired_admin_by",
                               return_value="author-unresolvable"):
            self.assertIs(dispatches._apply(state, event), state)
        self.assertIn("diff-applied", dispatches._ACTIVE_ONLY_EVENTS)

    def test_applied_refuses_without_an_ok_focused_fab_receipt(self):
        cure_tip, patch = self._cure()
        msg = self.post(patch)
        parent, why = self.verdict(self.room + "/" + msg["id"])
        self.assertIsNone(why, why)
        ledger_before = eventledger.events(dispatches.ledger_path())
        for bad in ({"status": "FAILED", "focused": True},
                    {"status": "OK", "focused": False}):
            with self.subTest(bad=bad):
                with mock.patch.object(dispatches, "_focused_fab_receipt",
                                       return_value=(bad, None)):
                    out, why = self.applied(parent, cure_tip)
                self.assertIsNone(out)
                self.assertIsNotNone(why)
        self.assertEqual(eventledger.events(dispatches.ledger_path()),
                         ledger_before)
        # POSITIVE CONTROL: the OK focused receipt is admitted.
        with mock.patch.object(dispatches, "_focused_fab_receipt",
                               return_value=(self._fab_receipt(), None)):
            out, why = self.applied(parent, cure_tip)
        self.assertIsNone(why, why)
        self.assertEqual(out["diff_applied"]["tip"], cure_tip)

    def test_applied_refuses_a_parent_with_no_meld_diff_handoff(self):
        plain = self.add(ref=self.b, recipient=self.READER)
        cure_tip, _patch = self._cure()
        ledger_before = eventledger.events(dispatches.ledger_path())
        with mock.patch.object(dispatches, "_focused_fab_receipt",
                               return_value=(self._fab_receipt(), None)):
            out, why = self.applied(plain, cure_tip)
        self.assertIsNone(out)
        self.assertIsNotNone(why)
        self.assertEqual(eventledger.events(dispatches.ledger_path()),
                         ledger_before)

    def test_a_pair_meld_yield_with_no_frozen_approval_still_wakes_the_reviewer(self):
        cure_tip, patch = self._cure()
        msg = self.post(patch)
        parent, why = self.verdict(self.room + "/" + msg["id"])
        self.assertIsNone(why, why)
        with mock.patch.object(dispatches, "_focused_fab_receipt",
                               return_value=(self._fab_receipt(), None)):
            out, why = self.applied(parent, cure_tip)
        self.assertIsNone(why, why)
        author = str(parent.get("sender") or self.row["sender"])
        # The reviewer joins the round (the convener's state already exists);
        # the author's YIELD then decides the reviewer's wake.
        meld.join(self.room, seat=self.READER)
        meld.say(self.room, "YIELD", "cure applied at %s" % cure_tip,
                 seat=author)
        rows = chat.read(self.room)[0]
        yield_row = rows[-1]
        self.assertIn("[YIELD]", yield_row["text"])
        self.assertNotIn("hold_approval", out["diff_applied"])
        self.assertIn("@" + self.READER, yield_row["text"])
        # POSITIVE CONTROL on the same observable: a YIELD naming a tip the
        # verb has NOT confirmed still wakes the reviewer.
        other_tip = self.commit_file("other", "an unconfirmed tip")
        meld.say(self.room, "YIELD", "cure applied at %s" % other_tip,
                 seat=author)
        unconfirmed = chat.read(self.room)[0][-1]
        self.assertIn("@" + self.READER, unconfirmed["text"])
        # A previous confirmation cannot suppress a different tip in the same
        # message, even if the author's note includes both full hashes.
        meld.say(self.room, "YIELD", "old %s, changed %s" % (
            cure_tip, other_tip), seat=author)
        mixed = chat.read(self.room)[0][-1]
        self.assertIn("@" + self.READER, mixed["text"])


class DiffAppliedLandAuthorityTest(DiffAppliedTest):
    """An unchanged applied cure is landable ONLY through the reviewer's
    approval-tier proof frozen at ITS hold (task/3937 cure round, reusing
    task/3976's proof machinery), never through the applied event alone.

    The parent FIX was verdicted with `bind_author`: its record-time tier
    evidence froze WHO the reviewer was (exact session, resolved family and
    model) and WHICH certain owner policy admitted that hand at the hold.
    `dispatch applied` spends that frozen authority onto the `diff-applied`
    event as one anchored proof. A cure whose reviewer was never tier-admitted
    at the hold — a pre-tier row, an outside-policy reviewer, a model the
    owner demoted — refuses, and the applied event mints nothing.
    """

    SESSION = "session-applied-authority"

    def setUp(self):
        super().setUp()
        self.policy(["family:claude"])

    def policy(self, members):
        return store.write_prior({
            "id": "applied-approval-tier",
            "statement": "Only the declared tier's reads carry land authority.",
            "confidence": 1.0, "stated_ts": "2026-07-29T00:00:00Z",
            "source": "human", "policy_kind": "approval-tier",
            "policy_members": members,
            "policy_reason": "owner approval rule"},
            root_dir=os.path.join(home.global_dir(), "premises"))

    def rostered_verdict(self, ref, *, model=None, members=None):
        """A MELD-DIFF FIX whose reviewer is rostered with an exact native
        session, so the verdict freezes real author and tier proof."""
        with mock.patch.dict(os.environ,
                             {"HELM_CHAT_NAME": self.READER,
                              "CLAUDE_CODE_SESSION_ID": self.SESSION}):
            seats.write_roster(
                self.READER, session=self.SESSION,
                runtime={"family": "claude", "agent_harness": "claude",
                         "backend": "native",
                         "model": model or "claude-opus-5-5"},
                presence_beat=False)
            out, why = dispatches.mark_verdict(
                self.row["id"], self.row["tip"],
                "reviewer posted a focused mechanical correction",
                polarity="fix", basis="measured", finding_count=1,
                prior_relation="new", no_patch_because=self.REASON,
                diff_handoff=ref, bind_author=True)
        self.assertIsNone(why, why)
        self.assertEqual(out["verdict_author_session"], self.SESSION)
        return out

    def applied_event(self, rid):
        return [e for e in eventledger.events(dispatches.ledger_path())
                if e.get("id") == rid and e.get("event") == "diff-applied"]

    def proven_cure(self):
        """(author, cure_tip): an applied cure whose frozen reviewer proof
        passes the CURRENT tier, so every wake arm that follows is about WHO
        speaks and WHO is addressed, never about the proof."""
        cure_tip, patch = self._cure()
        parent = self.rostered_verdict(self.room + "/" + self.post(patch)["id"])
        with mock.patch.object(dispatches, "_focused_fab_receipt",
                               return_value=(self._fab_receipt(), None)):
            _out, why = self.applied(parent, cure_tip)
        self.assertIsNone(why, why)
        ok, why = dispatches_tier.applied_approval(
            dispatches.snapshot()[0][parent["id"]], self.repo)
        self.assertTrue(ok, why)
        return str(parent.get("sender") or self.row["sender"]), cure_tip

    def test_a_reviewer_naming_the_proven_tip_still_wakes_the_author(self):
        """THE PROOF IS ONE REVIEWER'S ON ONE ROW (`proof.actor` is the row's
        recipient): it answers only its author's YIELD to that reviewer. A
        YIELD the REVIEWER says is the author's owed row, and it woke nobody
        when any proven tip in the room silenced every mention."""
        author, cure_tip = self.proven_cure()
        meld.join(self.room, seat=self.READER)

        def posted(text, seat):
            meld.say(self.room, "YIELD", text, seat=seat)
            return chat.read(self.room)[0][-1]["text"]

        # POSITIVE CONTROL on the same observable: the author speaking to
        # the proven reviewer drops that wake.
        self.assertNotIn("@" + self.READER,
                         posted("approved %s" % cure_tip, author))
        self.assertIn("@" + author,
                      posted("confirmed %s" % cure_tip, self.READER))

    def test_another_peer_in_the_round_is_still_woken(self):
        """A THIRD READER IN THE ROUND holds no proof on this row: only the
        proven reviewer's @mention drops, and the other peer's stands."""
        third = "seat-c"
        author, cure_tip = self.proven_cure()
        meld.invite("%s %s" % (self.READER, third), "a third read of the cure",
                    seat=author, room=self.room)
        meld.join(self.room, seat=self.READER)
        meld.join(self.room, seat=third)
        meld.say(self.room, "YIELD", "approved %s" % cure_tip, seat=author)
        text = chat.read(self.room)[0][-1]["text"]
        self.assertIn("[YIELD]", text)
        # POSITIVE CONTROL on the same row: the proven reviewer's wake drops.
        self.assertNotIn("@" + self.READER, text)
        self.assertIn("@" + third, text)

    def test_pair_yield_suppression_requires_current_frozen_approval(self):
        cure_tip, patch = self._cure()
        parent = self.rostered_verdict(self.room + "/" + self.post(patch)["id"])
        with mock.patch.object(dispatches, "_focused_fab_receipt",
                               return_value=(self._fab_receipt(), None)):
            out, why = self.applied(parent, cure_tip)
        self.assertIsNone(why, why)
        self.assertIn("hold_approval", out["diff_applied"])
        author = str(parent.get("sender") or self.row["sender"])
        meld.join(self.room, seat=self.READER)

        def posted(text):
            meld.say(self.room, "YIELD", text, seat=author)
            return chat.read(self.room)[0][-1]["text"]

        # An admitted same-tip proof saves the wake, but a changed tip does not.
        self.assertNotIn("@" + self.READER, posted("approved %s" % cure_tip))
        changed = self.commit_file("other", "changed tip")
        self.assertIn("@" + self.READER, posted("changed %s" % changed))
        self.assertIn("@" + self.READER,
                      posted("old %s, changed %s" % (cure_tip, changed)))

        # The owner may demote the frozen reviewer after the hold.
        self.policy(["family:codex"])
        self.assertIn("@" + self.READER, posted("demoted %s" % cure_tip))
        self.policy(["family:claude"])
        self.assertNotIn("@" + self.READER, posted("restored %s" % cure_tip))

        # An unreadable current policy and a damaged frozen anchor fail open.
        with mock.patch.object(dispatches, "_hold_policy",
                               side_effect=OSError("unreadable")):
            self.assertIn("@" + self.READER,
                          posted("unreadable %s" % cure_tip))
        rows, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable)
        original = rows[parent["id"]]
        proof = original["diff_applied"]["hold_approval"]
        damaged = dict(original, diff_applied=dict(
            original["diff_applied"],
            hold_approval=dict(proof, actor="unproven-reviewer")))
        ok, why = dispatches_tier.applied_approval(damaged, self.repo)
        self.assertFalse(ok)
        self.assertIn("anchor", why)
        with mock.patch.object(dispatches, "snapshot", return_value=(
                dict(rows, **{parent["id"]: damaged}), None)):
            self.assertIn("@" + self.READER,
                          posted("damaged %s" % cure_tip))

    def test_an_unchanged_cure_freezes_the_reviewers_hold_tier_proof_on_the_event(self):  # noqa: VACUOUS_ASSERTION — this arm IS the positive control: the proof, its anchor and the admitted re-judgment are all asserted BY VALUE on the same observables the three refusal arms below assert absent
        cure_tip, patch = self._cure()
        parent = self.rostered_verdict(self.room + "/" + self.post(patch)["id"])
        self.assertEqual(
            parent["verdict_tier_evidence"]["state"], "ok",
            "the fixture's policy must admit this reviewer at the hold, or "
            "the refusal arms below measure a door that never opens")
        with mock.patch.object(dispatches, "_focused_fab_receipt",
                               return_value=(self._fab_receipt(), None)):
            out, why = self.applied(parent, cure_tip)
        self.assertIsNone(why, why)
        proof = out["diff_applied"].get("hold_approval")
        self.assertIsInstance(proof, dict,
                              "the applied event carries no frozen proof: %r"
                              % out["diff_applied"])
        self.assertEqual(proof["actor"], self.READER)
        self.assertEqual(proof["tip"], cure_tip)
        self.assertEqual(proof["family"], "claude")
        self.assertEqual(proof["model"], "claude-opus-5-5")
        self.assertEqual(proof["anchor"],
                         dispatches._proof_anchor(
                             "diff-applied-holder-v1",
                             {k: v for k, v in proof.items()
                              if k != "anchor"}))
        # THE EVENT ITSELF CARRIES IT, so replay keeps it (positive control
        # on the same observable the refusal arms assert absent).
        events = self.applied_event(parent["id"])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["hold_approval"], proof)
        replay, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable)
        self.assertEqual(replay[parent["id"]]["diff_applied"]["hold_approval"],
                         proof)
        # THE PROOF RE-JUDGES CLEAN through task/3976's reader, against the
        # same current policy.
        ok, why = dispatches_tier.applied_approval(replay[parent["id"]],
                                                   self.repo)
        self.assertTrue(ok, why)
        self.assertIsNone(why)

    def test_a_retracted_fix_voids_the_applied_cure_authority(self):
        """task/4114 item 4: retraction projects polarity RETRACTED and the
        `verdict_retracted` flag; either spelling voids the frozen proof, so a
        reader that sees only the polarity still refuses. The live FIX on the
        same row is the positive control."""
        _author, _cure_tip = self.proven_cure()
        row = dispatches.snapshot()[0][self.row["id"]]
        ok, why = dispatches_tier.applied_approval(row, self.repo)
        self.assertTrue(ok, why)
        for projected in (dict(row, polarity=dispatches.RETRACTED,
                               verdict_retracted=True),
                          dict(row, polarity=dispatches.RETRACTED),
                          dict(row, verdict_retracted=True)):
            with self.subTest(polarity=projected.get("polarity"),
                              flag=projected.get("verdict_retracted")):
                ok, why = dispatches_tier.applied_approval(projected,
                                                           self.repo)
                self.assertFalse(ok)
                self.assertIn("no longer a live FIX", why)

    def test_a_pre_tier_parents_cure_records_no_land_authority(self):  # noqa: VACUOUS_ASSERTION — the absence of hold_approval and the named refusal are believed beside test_an_unchanged_cure_freezes_the_reviewers_hold_tier_proof_on_the_event, which asserts the same observables present and admitted in this same fixture
        """A FIX verdicted without author proof (the existing fixture's own
        shape) has no frozen hold authority to spend: the event records the
        patch identity, but no land-approval proof rides it, and the reader
        says so by name."""
        cure_tip, patch = self._cure()
        parent, why = self.verdict(self.room + "/" + self.post(patch)["id"])
        self.assertIsNone(why, why)
        self.assertNotIn("verdict_tier_evidence", parent)
        with mock.patch.object(dispatches, "_focused_fab_receipt",
                               return_value=(self._fab_receipt(), None)):
            out, why = self.applied(parent, cure_tip)
        self.assertIsNone(why, why)
        self.assertNotIn("hold_approval", out["diff_applied"])
        replay, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable)
        row = replay[parent["id"]]
        self.assertNotIn("hold_approval", row["diff_applied"])
        # THE READER FAILS CLOSED BY NAME, and the proven arm above is the
        # positive control on the same observable.
        ok, why = dispatches_tier.applied_approval(row, self.repo)
        self.assertFalse(ok)
        self.assertIn("no proven approval-tier reviewer", why)

    def test_a_reviewer_the_policy_never_admitted_mints_no_land_authority(self):  # noqa: VACUOUS_ASSERTION — the current-policy refusal is believed beside the same fixture's admitted re-judgment in test_an_unchanged_cure_freezes_the_reviewers_hold_tier_proof_on_the_event
        cure_tip, patch = self._cure()
        parent = self.rostered_verdict(self.room + "/" + self.post(patch)["id"])
        # THE OWNER DEMOTES THE REVIEWER'S FAMILY AFTER THE HOLD: the frozen
        # policy admitted it, the current one does not. task/3976's reader
        # keeps the current policy a separate veto over the frozen identity.
        self.policy(["family:codex"])
        with mock.patch.object(dispatches, "_focused_fab_receipt",
                               return_value=(self._fab_receipt(), None)):
            out, why = self.applied(parent, cure_tip)
        self.assertIsNone(why, why)
        replay, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable)
        ok, why = dispatches_tier.applied_approval(replay[parent["id"]],
                                                   self.repo)
        self.assertFalse(ok)
        self.assertIn("current approval tier", why)

    def test_a_forged_event_without_the_anchor_never_replays_as_proof(self):  # noqa: VACUOUS_ASSERTION — the anchor mismatch is asserted NOT-EQUAL unconditionally before the refusal, and the matching proof's admission is asserted by value in test_an_unchanged_cure_freezes_the_reviewers_hold_tier_proof_on_the_event
        cure_tip, patch = self._cure()
        parent = self.rostered_verdict(self.room + "/" + self.post(patch)["id"])
        with mock.patch.object(dispatches, "_focused_fab_receipt",
                               return_value=(self._fab_receipt(), None)):
            out, why = self.applied(parent, cure_tip)
        self.assertIsNone(why, why)
        event = self.applied_event(parent["id"])[0]
        forged = dict(event)
        forged["hold_approval"] = dict(event["hold_approval"],
                                       actor="seat-stranger")
        # A proof whose actor was rewritten no longer matches its anchor.
        self.assertNotEqual(
            forged["hold_approval"]["anchor"],
            dispatches._proof_anchor(
                "diff-applied-holder-v1",
                {k: v for k, v in forged["hold_approval"].items()
                 if k != "anchor"}))
        replayed = dispatches._apply(
            dispatches.snapshot()[0][parent["id"]], forged)
        folded = replayed["diff_applied"]
        ok, why = dispatches_tier.applied_approval(
            dict(replayed, diff_applied=forged), self.repo)
        self.assertFalse(ok)
        self.assertIsNotNone(why)


if __name__ == "__main__":
    unittest.main()
