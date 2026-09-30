#!/usr/bin/env python3
"""MELD-DIFF's FIX receipt binds an actual reader-posted diff to its pair round.

Every chat row and verdict is written under DispatchBase's synthetic HELM_HOME;
no live room, seat, credential, or dispatch ledger is consulted.
"""
import hashlib
import os
import subprocess
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from helm import chat, dispatches, eventledger, meld, review_door  # noqa: E402
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
        other = self.add(ref=self.c, recipient=self.READER)
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


if __name__ == "__main__":
    unittest.main()
