"""A reviewed tip outlives its branch: the verdict pins it against `git gc`.

The tip a verdict names is the review's evidence. Before the pin, the lane
branch was the only thing keeping that commit alive, so deleting the branch
and pruning left a row naming a sha its own repository could not resolve.
Every arm here runs in a real temporary repository and prunes for real.
"""
import json
import os
import subprocess
import unittest
from unittest import mock

from helm import dispatches, eventledger, landreq, pk
# The module, never its TestCase: tests/test_suite_collection.py says why.
from tests import test_lr_close as _close


class ReviewedTipsArePinnedTest(_close.CloseBase):

    def doomed_tip(self, branch="doomed"):
        """One commit on a throwaway branch off trunk; trunk stays checked out."""
        self.git("checkout", "-q", "-b", branch, self.main)
        tip = self.commit("work only %s holds" % branch, path=branch + ".txt")
        self.git("checkout", "-q", self.main)
        return tip

    def resolves(self, sha):
        p = subprocess.run(["git", "--git-dir", self.gitdir(), "cat-file",
                            "-e", sha + "^{commit}"], capture_output=True)
        return p.returncode == 0

    def ref(self, name):
        p = subprocess.run(["git", "-C", self.repo, "rev-parse", "--verify",
                            "-q", name], capture_output=True, text=True)
        return p.stdout.strip() if p.returncode == 0 else ""

    def gc(self, *branches):
        for branch in branches:
            self.git("branch", "-D", branch)
        self.git("reflog", "expire", "--expire=now", "--all")
        self.git("gc", "--prune=now", "--quiet")

    def pin_events(self):
        try:
            with open(pk.events_path(), encoding="utf-8") as fh:
                rows = [json.loads(line) for line in fh if line.strip()]
        except FileNotFoundError:
            return []
        return [r for r in rows if r.get("verb") == "dispatch-pin-failed"]

    def test_control_an_unpinned_tip_IS_pruned(self):
        """The must-hit: without a verdict the same recipe destroys the tip,
        so the survival below is the pin's doing and not the recipe's."""
        tip = self.doomed_tip()
        self.gc("doomed")
        self.assertFalse(self.resolves(tip))

    def test_a_verdicted_tip_survives_branch_delete_and_gc(self):
        tip = self.doomed_tip()
        row = self.verdict_row("fix", ref=tip, lane="lane/pinned")
        self.assertEqual(self.ref("refs/helm-reviewed/" + row["id"]), tip)
        self.gc("doomed")
        self.assertTrue(self.resolves(tip), "the reviewed tip was pruned")
        self.assertEqual(self.ref("refs/helm-reviewed/" + row["id"]), tip)
        self.assertEqual(self.pin_events(), [])

    def test_close_moves_the_pin_to_the_retired_namespace(self):
        tip = self.doomed_tip()
        row = self.verdict_row("concur", ref=tip, lane="lane/retiring")
        self.assertEqual(self.ref("refs/helm-reviewed/" + row["id"]), tip)
        _out, err = landreq.close(row["id"], "expired", repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(self.ref("refs/helm-reviewed/" + row["id"]), "")
        self.assertEqual(
            self.ref("refs/helm-retired/reviewed/" + row["id"]), tip)
        self.gc("doomed")
        self.assertTrue(self.resolves(tip), "a retired pin let the tip go")
        # A second move has nothing to move and writes nothing.
        self.assertIsNone(dispatches.retire_review_pins(
            dispatches.snapshot()[0][row["id"]]))
        self.assertEqual(
            self.ref("refs/helm-retired/reviewed/" + row["id"]), tip)

    def test_a_failed_pin_warns_once_and_the_verdict_still_records(self):
        tip = self.doomed_tip()
        with mock.patch.object(dispatches, "_pin_write",
                               return_value="cannot lock ref (planted)"):
            row, err = self.send_and_verdict(tip, "lane/unpinnable")
        self.assertIsNone(err, err)
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["status"],
                         "verdict")
        self.assertIn("NOT pinned", row.get("pin_warning") or "")
        self.assertIn("cannot lock ref (planted)", row["pin_warning"])
        self.assertEqual(len(self.pin_events()), 1)
        self.assertEqual(self.ref("refs/helm-reviewed/" + row["id"]), "")

    def test_a_pin_that_raises_is_a_warning_never_a_traceback(self):
        tip = self.doomed_tip()
        with mock.patch.object(dispatches, "_pin_read",
                               side_effect=RuntimeError("planted")):
            row, err = self.send_and_verdict(tip, "lane/raising")
        self.assertIsNone(err, err)
        self.assertIn("raised RuntimeError", row.get("pin_warning") or "")

    def test_re_pinning_the_same_tip_is_a_no_op(self):
        tip = self.doomed_tip()
        row = self.verdict_row("fix", ref=tip, lane="lane/twice")
        folded = dispatches.snapshot()[0][row["id"]]
        with mock.patch.object(dispatches, "_pin_write") as write:
            self.assertIsNone(dispatches.pin_reviewed_tips(
                folded, [("reviewed", tip)]))
        write.assert_not_called()
        self.assertEqual(self.ref("refs/helm-reviewed/" + row["id"]), tip)

    def test_a_moved_pin_retires_the_old_commit_first(self):
        """Another tip for the same role never drops the one already pinned."""
        first, second = self.doomed_tip("one"), self.doomed_tip("two")
        row = {"id": "a1b2c3d4e5f6", "repo_root": self.repo}
        # The shape the ledger writes: the checkout AND the repository
        # identity the pin is measured against (task/3627).
        row["repo_id"] = self.gitdir()
        self.assertIsNone(dispatches.pin_reviewed_tips(
            row, [("source-clean", first)]))
        self.assertIsNone(dispatches.pin_reviewed_tips(
            row, [("source-clean", second)]))
        self.assertEqual(
            self.ref("refs/helm-reviewed/a1b2c3d4e5f6-source-clean"), second)
        self.assertEqual(self.ref(
            "refs/helm-retired/reviewed/a1b2c3d4e5f6-source-clean"), first)
        self.gc("one", "two")
        self.assertTrue(self.resolves(first) and self.resolves(second))

    # THE PIN IS WRITTEN BEFORE THE EVENT IS DURABLE (task/2383). Between
    # the append and any step after it lie the lock release and the
    # checkpoint advance (a cold fold, measured at 102 s after a land). A pin
    # written in that window can be beaten by a process exit, or by a branch
    # delete plus `git gc --prune=now`, which leaves a recorded verdict with
    # no pin and no receipt; a pruned object can never be pinned again. Each
    # arm below stops or prunes at one step of that window and reads what the
    # repository and the ledger then hold.

    def test_an_append_that_fails_after_the_pin_leaves_the_pin_and_no_verdict(self):  # noqa: VACUOUS_ASSERTION — the absent verdict event sits beside the pin ref asserted EQUAL to the tip on the same run
        """The harmless side of the order: the pin is an orphan that keeps one
        commit alive, and nothing claims a verdict was recorded."""
        for how in ("unwritable", "died"):
            with self.subTest(how=how):
                tip = self.doomed_tip("crash-" + how)
                row = self.send(tip, "lane/crash-" + how)

                def fail(_event):
                    if how == "died":
                        raise _Died()
                    return False
                with self.at_append("verdict", fail):
                    if how == "died":
                        with self.assertRaises(_Died):
                            self.verdict(row, tip)
                    else:
                        _out, err = self.verdict(row, tip)
                        self.assertIn("verdict NOT recorded", err or "")
                self.assertEqual(self.ledger_events(row["id"], "verdict"), [])
                self.assertNotEqual(
                    dispatches.snapshot()[0][row["id"]]["status"], "verdict")
                self.assertEqual(self.ref("refs/helm-reviewed/" + row["id"]),
                                 tip, "the pin must precede the append")

    def test_the_pin_resolves_before_the_verdict_is_durable(self):  # noqa: VACUOUS_ASSERTION — the empty patch ref is the second half of one tuple whose first half is asserted EQUAL to the tip
        tip = self.doomed_tip()
        row = self.send(tip, "lane/ordered")
        seen = []
        with self.at_append("verdict", lambda _event: seen.append(
                (self.ref("refs/helm-reviewed/" + row["id"]),
                 self.ref("refs/helm-reviewed/" + row["id"] + "-patch")))):
            _out, err = self.verdict(row, tip)
        self.assertIsNone(err, err)
        self.assertEqual(seen, [(tip, "")])

    def test_a_cured_verdict_pins_its_patch_before_the_append(self):  # noqa: VACUOUS_ASSERTION — the observable is the patch pin asserted EQUAL to the cure, and both commits asserted to resolve
        tip = self.doomed_tip()
        row = self.send(tip, "lane/cured")
        self.git("checkout", "-q", "doomed")
        cure = self.commit("the reviewer's cure", path="cure.txt")
        self.git("checkout", "-q", self.main)
        seen = []
        with self.at_append("verdict", lambda _event: seen.append(
                self.ref("refs/helm-reviewed/" + row["id"] + "-patch"))):
            _out, err = self.mark_verdict(row["id"], tip, "findings",
                                          polarity="fix", patch_tip=cure)
        self.assertIsNone(err, err)
        self.assertEqual(seen, [cure])
        self.gc("doomed")
        self.assertTrue(self.resolves(tip) and self.resolves(cure))

    def test_a_process_exit_after_the_append_leaves_a_pinned_verdict(self):
        """The verdict is durable and the process dies in the checkpoint
        advance, before any step that runs after the append."""
        tip = self.doomed_tip()
        row = self.send(tip, "lane/exited")
        with mock.patch.object(dispatches, "_advance_checkpoint",
                               side_effect=_Died()):
            with self.assertRaises(_Died):
                self.verdict(row, tip)
        self.assertEqual(len(self.ledger_events(row["id"], "verdict")), 1)
        self.assertEqual(self.ref("refs/helm-reviewed/" + row["id"]), tip)
        self.gc("doomed")
        self.assertTrue(self.resolves(tip), "a durable verdict lost its tip")

    def test_a_prune_in_the_checkpoint_window_leaves_the_tip_resolvable(self):  # noqa: VACUOUS_ASSERTION — the absent warning and receipts sit beside the tip asserted to resolve inside the window and after it
        tip = self.doomed_tip()
        row = self.send(tip, "lane/window")
        real, seen = dispatches._advance_checkpoint, []

        def prune_then_advance():
            self.gc("doomed")
            seen.append(self.resolves(tip))
            real()
        with mock.patch.object(dispatches, "_advance_checkpoint",
                               side_effect=prune_then_advance):
            out, err = self.verdict(row, tip)
        self.assertIsNone(err, err)
        self.assertEqual(seen, [True], "the prune in the window took the tip")
        self.assertTrue(self.resolves(tip))
        self.assertNotIn("pin_warning", out)
        self.assertEqual(self.pin_events(), [])

    def test_a_pin_that_fails_before_the_append_still_lets_the_verdict_record(self):
        """Today's contract, kept at the new position: the failed pin is a
        receipt written before the event, and the event is still written."""
        tip = self.doomed_tip()
        row = self.send(tip, "lane/failed-first")
        receipts = []
        with mock.patch.object(dispatches, "_pin_write",
                               return_value="cannot lock ref (planted)"), \
                self.at_append("verdict", lambda _event: receipts.append(
                    len(self.pin_events()))):
            out, err = self.verdict(row, tip)
        self.assertIsNone(err, err)
        self.assertEqual(receipts, [1])
        self.assertEqual(len(self.ledger_events(row["id"], "verdict")), 1)
        self.assertIn("cannot lock ref (planted)", out.get("pin_warning", ""))

    def test_a_source_clean_pin_resolves_before_the_hold_is_durable(self):  # noqa: VACUOUS_ASSERTION — the absent warning sits beside the source-clean pin asserted EQUAL to the tip at the append
        tip = self.doomed_tip()
        row = self.send(tip, "lane/held-clean")
        seen = []
        with self.at_append("hold", lambda _event: seen.append(self.ref(
                "refs/helm-reviewed/" + row["id"] + "-source-clean"))):
            out, err = self.hold_clean(row, tip)
        self.assertIsNone(err, err)
        self.assertEqual(seen, [tip])
        self.assertNotIn("pin_warning", out)

    def test_a_hold_that_fails_after_the_pin_leaves_the_pin_and_no_hold(self):  # noqa: VACUOUS_ASSERTION — the absent hold event sits beside the source-clean pin asserted EQUAL to the tip
        tip = self.doomed_tip()
        row = self.send(tip, "lane/held-failed")
        with self.at_append("hold", lambda _event: False):
            _out, err = self.hold_clean(row, tip)
        self.assertIn("hold NOT recorded", err or "")
        self.assertEqual(self.ledger_events(row["id"], "hold"), [])
        self.assertEqual(self.ref(
            "refs/helm-reviewed/" + row["id"] + "-source-clean"), tip)

    def at_append(self, kind, react):
        """Call `react(event)` when the dispatch-ledger append of a `kind`
        event begins. False from it stands for an unwritable ledger and
        writes nothing; anything else lets the real append run."""
        real, ledger = eventledger.append_unlocked, dispatches.ledger_path()

        def append(path, event):
            if path == ledger and event.get("event") == kind \
                    and react(event) is False:
                return False
            return real(path, event)
        return mock.patch.object(dispatches.eventledger, "append_unlocked",
                                 side_effect=append)

    def ledger_events(self, rid, kind):
        return [e for e in eventledger.events(dispatches.ledger_path())
                if e.get("id") == rid and e.get("event") == kind]

    def send(self, tip, lane):
        row, why, _sent = dispatches.send(
            "codex-3", lane, "review " + lane, tip, repo=self.repo,
            key="key-" + lane, sign=False, new_work=True,
            task=self.review_task["id"])
        self.assertIsNone(why)
        return row

    def verdict(self, row, tip):
        return self.mark_verdict(row["id"], tip, "findings", polarity="fix",
                                 no_patch_because="a design finding")

    def hold_clean(self, row, tip):
        """A source-clean hold by the row's own recipient (task/3053)."""
        with mock.patch.object(dispatches, "_acting_author",
                               return_value=(row["recipient"], None)):
            return dispatches.mark_hold(row["id"], "awaiting the land gate; fab Ran 5 tests OK",
                                        source_clean_tip=tip)

    def send_and_verdict(self, tip, lane):
        return self.verdict(self.send(tip, lane), tip)


class _Died(BaseException):
    """The process stopping at this line: nothing after it runs, and no
    `except Exception` on the way out can mistake it for an error."""


if __name__ == "__main__":
    unittest.main()
