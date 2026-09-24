#!/usr/bin/env python3
"""Deriving a row's state from ARTIFACTS instead of from a remembered verb.

WHAT THESE TESTS DEFEND. `helm.rowstate.derive` answers a question the dispatch
ledger answers wrongly: the ledger says AWAITING_BUILD because nobody ran a
closing verb, while trunk has carried the work for a day. Every fixture below is
a REAL row and a REAL trunk binding measured on the live estate on 2026-08-05
and frozen here, so the suite keeps testing the answers the owner's own board
got wrong rather than a story invented alongside the code.

THE MUST-MISS ROWS MATTER MORE THAN THE HITS. A derivation that answers LANDED
often enough looks right on a histogram, so the cases that must NOT resolve are
pinned as hard as the ones that must: a build row whose recorded tip is its BASE
(on trunk by construction) must not read LANDED; a `concur` verdict must never
reach APPROVED; a same-family approve must not; an approve bound to a DIFFERENT
tip must not; and an absent branch must read UNKNOWN rather than UNSTARTED.

NOTHING HERE IS ALLOWED TO PASS VACUOUSLY. Every absence assertion sits beside a
control on the same structure proving the probe can see anything at all, and the
derived value is asserted to EXIST and to EQUAL the expected state — never
"did not raise".
"""
import os
import re
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import rowstate, rowworld
from tests import _release

# ---------------------------------------------------------------- fixtures
#
# Measured 2026-08-05 against this repository at
# origin/main = 19997d431198c3ec68e22bfc38d6270fb452a20f. These are immutable
# historical facts: a commit that carried a fold subject keeps carrying it.

TRUNK_HEAD = "19997d431198c3ec68e22bfc38d6270fb452a20f"
FOLD_FLEET_NOTES = "fa0915b22104382886ce42f27157c99d181b0845"
FOLD_PRE_JOIN = "92564c392c331f1d3c4eaeb3ef3301087e3cc6c5"
BASE_SPLIT = "d225f4c3b7984da5f2be64278575e7e2dbb95d03"
APPROVED_TIP = "fb4ab8a7d946b16e21537e7b93bd05a8c6ae09cc"
APPROVED_TREE = "206fe1f98ef6016051bba192aac3aa6531891424"
GATE_ID = "6b0f376a53e654f1"
LANE_HEAD = "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"

# 2026-08-04T00:00:00Z and 2026-08-05T00:00:00Z as UTC epochs.
DAY4, DAY5 = 1785801600, 1785888000


def receipt(head, tree, **over):
    """A receipt in the shape `gate.receipts()` returns."""
    row = {"id": GATE_ID, "head": head, "tree": tree, "dirty": False,
           "head_after": head, "tree_after": tree, "status": "OK",
           "suite": True, "ts": "2026-08-05T18:00:00Z"}
    row.update(over)
    return row


class _AlwaysCarried(dict):
    """The fixture default for `World.carriage`: every lane reads carried.

    A real dict subclass rather than a defaultdict so `in` still answers
    honestly for anything a test inserts, and so the name appears at the one
    place a reader looks — see `world()`."""

    def get(self, key, default=None):
        return dict.get(self, key, True)

    # KEYED BY ROW ID since the meld rework, not by lane ref — the pair the
    # answer is measured from comes from the LEDGER (chain base + carrier
    # tip), so the row is what identifies it.

    def __bool__(self):
        # AN EMPTY ONE OF THESE IS STILL MEANINGFUL, and readers spell the
        # idiom `world.carriage or {}` — which would swap an empty instance
        # for a plain dict and silently lose the default. Caught by a red arm
        # rather than reasoning: the ledger-landed row read UNKNOWN because
        # its ref resolved through a substituted `{}`.
        return True


def world(**over):
    """A World with every slot explicitly set, so a test never depends on a
    default it did not state. Trunk is TRUNK_HEAD over BASE_SPLIT, newest
    first, dated DAY5 and DAY4."""
    spec = {
        "gitdir": "/nonexistent.git", "trunk_ref": "origin/main",
        "trunk_shas": frozenset((TRUNK_HEAD, BASE_SPLIT)),
        "trunk_trees": {}, "trunk_patch_ids": {}, "trunk_tokens": {},
        "branches": {}, "branch_commits": {}, "commit_patch_ids": {},
        "commit_trees": {TRUNK_HEAD: "tree" + "0" * 36,
                         BASE_SPLIT: "tree" + "1" * 36,
                         APPROVED_TIP: APPROVED_TREE,
                         LANE_HEAD: "tree" + "2" * 36},
        "trunk_index": {TRUNK_HEAD: 0, BASE_SPLIT: 1},
        "trunk_cts": (DAY5, DAY4),
        "rows": {}, "receipts_by_head": {}, "approvals": {}, "authors": {},
        "carriers": {}, "merge_bases": {}, "trunk_reverts": {},
        "families": {}, "patch_scan": "complete", "unavailable": None,
        "receipts_unavailable": None, "receipts_skipped": 0,
        # T4's three: each names a SCAN THAT FAILED, and each defaults to
        # "the scan succeeded" so a fixture must opt in to the failure it
        # wants to test.
        "reverts_unavailable": None, "lane_scan_unavailable": None,
        "branch_walk_failed": frozenset(),
        # T15. `carriage` maps a lane ref to True/False/None, and after
        # The NEW ROOT: an affirmative True is REQUIRED for every LANDED.
        # The default therefore has to be True, or every fixture in this file
        # would be asserting the refusal path by accident — in production
        # rowworld measures this per lane and the ordinary answer for landed
        # work IS True. A fixture that cares about a refusal passes an
        # explicit dict; `_AlwaysCarried` exists so that default is VISIBLE
        # rather than hidden in a defaultdict nobody reads.
        "head_tree": {}, "carriage": _AlwaysCarried(),
        # Rows naming no repository. Empty by default so a fixture must opt
        # in to the unplaced case (addendum 4).
        "unscoped": frozenset(),
    }
    spec.update(over)
    return rowworld.World(**spec)


def build_row(rid, lane, **over):
    row = {"id": rid, "kind": "build", "lane": lane, "recipient": "codex",
           "ts": "2026-08-05T12:00:00Z", "tip": BASE_SPLIT, "status": "open"}
    row.update(over)
    return row


def review_row(rid, lane, **over):
    row = {"id": rid, "kind": "review", "lane": lane, "recipient": "ds4pro",
           "ts": "2026-08-05T12:00:00Z", "reviewed_tip": APPROVED_TIP,
           "status": "verdict"}
    row.update(over)
    return row


class SubjectTipTest(unittest.TestCase):
    """A build row's recorded tip is its BASE — the trap that would make every
    build row ever dispatched read LANDED."""

    def test_a_build_rows_subject_is_its_lane_branch_not_its_recorded_tip(self):
        row = build_row("a" * 32, "split")
        w = world(branches={"lane/split": LANE_HEAD})
        tip, evidence, why = rowstate.subject_tip(row, w)
        self.assertEqual(tip, LANE_HEAD)
        self.assertEqual(evidence.kind, "branch")
        self.assertIsNone(why)
        # CONTROL: the tip it did NOT pick is genuinely on trunk, so the test
        # is discriminating between two live answers, not between an answer
        # and an absence.
        self.assertIn(row["tip"], w.trunk_shas)

    def test_a_base_on_trunk_does_not_make_the_build_row_landed(self):
        """THE MUST-MISS. tip=BASE_SPLIT is an ancestor of trunk; the lane head
        is not. The row must not read LANDED."""
        row = build_row("b" * 32, "split")
        w = world(branches={"lane/split": LANE_HEAD},
                  branch_commits={"lane/split": (LANE_HEAD,)})
        self.assertIn(row["tip"], w.trunk_shas)      # control: base IS landed
        self.assertTrue(w.trunk_shas)
        self.assertNotIn(LANE_HEAD, w.trunk_shas)
        got = rowstate.derive(row, w)
        self.assertTrue(got.evidence)
        self.assertEqual(got.state, rowstate.BUILDING)
        self.assertNotEqual(got.state, rowstate.LANDED)

    def test_an_absent_branch_is_unknown_and_never_unstarted(self):
        """A reaped branch and one that never existed are the same absence."""
        got = rowstate.derive(build_row("c" * 32, "gone"), world())
        self.assertEqual(got.state, rowstate.UNKNOWN)
        self.assertIsNotNone(got.unknown)
        self.assertIn("lane/gone", got.unknown)

    def test_a_branch_with_nothing_past_the_merge_base_is_unstarted(self):
        w = world(branches={"lane/fresh": LANE_HEAD},
                  branch_commits={"lane/fresh": ()})
        got = rowstate.derive(build_row("d" * 32, "fresh"), w)
        self.assertEqual(got.state, rowstate.UNSTARTED)
        self.assertEqual(got.because("no-commits-past-merge-base"),
                         "lane/fresh")

    def test_a_verified_retip_rebinds_a_build_row_to_its_output(self):
        """Row f694d09aa5f2 was retipped to the exact tip the integrator
        landed. Without reading `retips` it reads UNKNOWN forever."""
        row = build_row("f694d09aa5f2b5c00a78e4862b6114fe", "mark-delivered",
                        tip=TRUNK_HEAD,
                        retips=[{"old_tip": "6d509bfee855a288fcee334ee3365e4f9",
                                 "tip": TRUNK_HEAD, "identity": "verified"}])
        got = rowstate.derive(row, world())
        self.assertTrue(got.evidence)
        self.assertEqual(got.state, rowstate.LANDED)
        self.assertEqual(got.because("ancestry"), TRUNK_HEAD)

    def test_an_unverified_retip_is_ignored(self):
        row = build_row("e" * 32, "mark-delivered", tip=TRUNK_HEAD,
                        retips=[{"tip": TRUNK_HEAD, "identity": "claimed"}])
        got = rowstate.derive(row, world())
        self.assertTrue(got.unknown)
        self.assertEqual(got.state, rowstate.UNKNOWN)


class LandedTest(unittest.TestCase):
    def test_content_identity_lands_a_rebased_lane_ancestry_cannot_see(self):
        """The rebase case: the lane's commits are patch-identical to trunk's
        under different shas, so `--is-ancestor` truthfully answers no."""
        w = world(branches={"lane/rebased": LANE_HEAD},
                  branch_commits={"lane/rebased": ("cafe" + "0" * 36,)},
                  commit_patch_ids={"cafe" + "0" * 36: "pid1"},
                  trunk_patch_ids={"pid1": TRUNK_HEAD})
        self.assertTrue(w.trunk_shas)               # control: the set is real
        self.assertNotIn(LANE_HEAD, w.trunk_shas)   # control: not an ancestor
        got = rowstate.derive(build_row("1" * 32, "rebased"), w)
        self.assertTrue(got.evidence)
        self.assertEqual(got.state, rowstate.LANDED)
        ref, count, sha = got.because("content-identity")
        self.assertEqual((ref, count, sha), ("lane/rebased", 1, TRUNK_HEAD))

    def test_one_unlanded_commit_in_a_stack_defeats_content_identity(self):
        """Landing SOME of a stack is exactly the state that must keep its
        branch."""
        w = world(branches={"lane/partial": LANE_HEAD},
                  branch_commits={"lane/partial": ("aa" + "0" * 38,
                                                   "bb" + "0" * 38)},
                  commit_patch_ids={"aa" + "0" * 38: "pid1",
                                    "bb" + "0" * 38: "pid2"},
                  trunk_patch_ids={"pid1": TRUNK_HEAD})
        got = rowstate.derive(build_row("2" * 32, "partial"), w)
        self.assertTrue(got.evidence)
        self.assertEqual(got.state, rowstate.BUILDING)

    def test_a_merge_commit_with_no_patch_id_refuses_rather_than_skips(self):
        """`git patch-id` says nothing about a merge, and a reader that treats
        its silence as agreement deletes branches holding real work."""
        w = world(branches={"lane/merged": LANE_HEAD},
                  branch_commits={"lane/merged": ("aa" + "0" * 38,
                                                  "merge" + "0" * 35)},
                  commit_patch_ids={"aa" + "0" * 38: "pid1"},
                  trunk_patch_ids={"pid1": TRUNK_HEAD})
        got = rowstate.derive(build_row("3" * 32, "merged"), w)
        self.assertEqual(got.state, rowstate.BUILDING)
        self.assertIn("no readable patch-id", got.unknown)

    def test_a_trunk_commit_naming_the_row_id_lands_a_reaped_lane(self):
        """Row a1f5aee91903 — the owner's row. Its branch is gone; trunk commit
        fa0915b22104 names it `chain a1f5aee9`. This is the ONLY proof that
        survives the branch being reaped."""
        row = build_row("a1f5aee91903308a151ae1cf4c3fd1e8", "fleet-notes")
        w = world(trunk_tokens={"a1f5aee9": FOLD_FLEET_NOTES})
        got = rowstate.derive(row, w)
        self.assertEqual(got.state, rowstate.LANDED)
        self.assertEqual(got.because("id-binding"),
                         ("a1f5aee9", FOLD_FLEET_NOTES))

    def test_a_lane_name_on_trunk_is_not_a_binding(self):
        """MUST-MISS. The lane NAME appears in fold subjects for every sibling
        round of a chain, so matching it would call a chain parent landed."""
        row = build_row("9" * 32, "fleet-notes")
        w = world(trunk_tokens={"fleet-notes": FOLD_FLEET_NOTES})
        # CONTROL: the same world DOES bind when the token is the row id, so
        # the refusal below is about the KIND of token, not a dead probe.
        control = rowstate.derive(
            row, world(trunk_tokens={"99999999": FOLD_FLEET_NOTES}))
        self.assertTrue(control.evidence)
        self.assertEqual(control.state, rowstate.LANDED)
        got = rowstate.derive(row, w)
        self.assertTrue(got.unknown)
        self.assertNotEqual(got.state, rowstate.LANDED)


class ScanCompletenessTest(unittest.TestCase):
    """BLOCKERS 8 + 9 (gate:9b5029474fb63d13): patch_scan_capped
    compared PATCH count to the cap, so a complete history with exactly cap
    patch-bearing commits read capped (token binding could then override a
    partial stack) and a truncated window holding an empty commit read
    complete (a landed patch beyond the cap was vetoed). Completeness now
    comes from COMMIT enumeration, and shallow/failed scans are NO answer."""

    def _grown(self, tmp, commits, empty_at=None):
        repo, _wt, git = _scratch_repo(tmp)
        for n in range(commits - 1):            # the scratch root is commit 1
            if empty_at is not None and n == empty_at:
                git("commit", "-q", "--allow-empty", "-m", "empty-%d" % n)
                continue
            with open(os.path.join(repo, "f%d.txt" % n), "w") as fh:
                fh.write("%d\n" % n)
            git("add", ".")
            git("commit", "-q", "-m", "c%d" % n)
        return repo, git

    def test_exactly_cap_patch_bearing_commits_is_complete_not_capped(self):
        """Blocker 8, false positive: total history == cap means every commit
        was scanned; nothing was truncated."""
        with tempfile.TemporaryDirectory() as tmp:
            repo, _git_fn = self._grown(tmp, commits=4)
            w = rowworld.snapshot(repo, trunk_ref="main", rows={}, patch_cap=4)
            self.assertEqual(w.patch_scan, "complete")

    def test_a_truncated_window_with_an_empty_commit_is_capped_not_complete(self):
        """Blocker 8, false negative: five non-merge commits, cap four, one
        of the four in-window EMPTY — the patch count says three, the honest
        answer is that one commit was never scanned."""
        with tempfile.TemporaryDirectory() as tmp:
            repo, _git_fn = self._grown(tmp, commits=5, empty_at=2)
            w = rowworld.snapshot(repo, trunk_ref="main", rows={}, patch_cap=4)
            self.assertEqual(w.patch_scan, "capped")
            # CONTROL: the same estate under a cap that fits is complete.
            w = rowworld.snapshot(repo, trunk_ref="main", rows={}, patch_cap=5)
            self.assertEqual(w.patch_scan, "complete")

    def test_a_shallow_clone_is_unavailable_not_negative(self):
        """Blocker 9: shallow history is not a smaller answer, it is NO
        answer."""
        with tempfile.TemporaryDirectory() as tmp:
            repo, _git_fn = self._grown(tmp, commits=4)
            shallow = os.path.join(tmp, "shallow")
            done = subprocess.run(("git", "clone", "-q", "--depth", "1",
                                   "file://" + repo, shallow),
                                  capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            w = rowworld.snapshot(shallow, trunk_ref="main", rows={},
                                  patch_cap=4)
            self.assertEqual(w.patch_scan, "unavailable")

    def test_an_unavailable_scan_does_not_veto_id_binding(self):
        """Blocker 9 at the derivation: 'patch absent' must not veto a
        VISIBLE id binding when the scan never ran."""
        spec = {"branches": {"lane/blind": LANE_HEAD},
                "branch_commits": {"lane/blind": ("cafe" + "0" * 36,)},
                "commit_patch_ids": {"cafe" + "0" * 36: "pid1"},
                "trunk_patch_ids": {},
                "trunk_tokens": {"8a" * 4: FOLD_FLEET_NOTES}}
        # CONTROL: with a COMPLETE scan the absence is decisive and the token
        # must NOT rescue the row (blocker 4's law).
        control = rowstate.derive(build_row("8a" * 16, "blind"),
                                  world(patch_scan="complete", **spec))
        self.assertEqual(control.state, rowstate.BUILDING)
        got = rowstate.derive(build_row("8a" * 16, "blind"),
                              world(patch_scan="unavailable", **spec))
        self.assertEqual(got.state, rowstate.LANDED)
        self.assertEqual(got.because("id-binding"),
                         ("8a8a8a8a", FOLD_FLEET_NOTES))

    def test_an_unavailable_scan_turns_building_into_unknown(self):
        """Blocker 9's terminal word: BUILDING leans on the scan; with no
        scan the state is UNKNOWN, never a confident stage."""
        spec = {"branches": {"lane/blind": LANE_HEAD},
                "branch_commits": {"lane/blind": ("cafe" + "0" * 36,)},
                "commit_patch_ids": {"cafe" + "0" * 36: "pid1"},
                "trunk_patch_ids": {}}
        control = rowstate.derive(build_row("8b" * 16, "blind"),
                                  world(patch_scan="complete", **spec))
        self.assertEqual(control.state, rowstate.BUILDING)
        got = rowstate.derive(build_row("8b" * 16, "blind"),
                              world(patch_scan="unavailable", **spec))
        self.assertEqual(got.state, rowstate.UNKNOWN)
        self.assertIn("an unread scan is not an empty one", got.unknown)


class RevertTest(unittest.TestCase):
    """BLOCKER 6 (gate:9b5029474fb63d13): 'Trunk adds then reverts
    work; a later lane re-adds it. Historical patch-id match derives LANDED
    although HEAD lacks the work and trees differ.' A clean revert's diff is
    the original's reversed, so the reverse-diff index makes the undo
    addressable under the forward id, and POSITION orders undo against land."""

    REVERT = "0ff" + "0" * 37
    RELAND = "0aa" + "0" * 37

    def _world(self, **over):
        spec = {"branches": {"lane/readd": LANE_HEAD},
                "branch_commits": {"lane/readd": ("cafe" + "0" * 36,)},
                "commit_patch_ids": {"cafe" + "0" * 36: "pid1"},
                "trunk_patch_ids": {"pid1": BASE_SPLIT},
                "trunk_shas": frozenset((TRUNK_HEAD, BASE_SPLIT, self.REVERT)),
                "trunk_index": {TRUNK_HEAD: 0, self.REVERT: 1, BASE_SPLIT: 2},
                "trunk_cts": (DAY5, DAY5, DAY4)}
        spec.update(over)
        return world(**spec)

    def test_a_reverted_patch_does_not_read_landed(self):
        # CONTROL first, same observable: WITHOUT the revert this exact world
        # derives LANDED through the very match the cure must invalidate.
        control = rowstate.derive(build_row("6a" * 16, "readd"), self._world())
        self.assertEqual(control.state, rowstate.LANDED)
        w = self._world(trunk_reverts={"pid1": self.REVERT})
        got = rowstate.derive(build_row("6a" * 16, "readd"), w)
        self.assertEqual(got.state, "BUILDING")
        self.assertNotEqual(got.state, rowstate.LANDED)
        # The REASON moved with the rung (task/744, T3). It used to be the
        # content rung's "absent from trunk by patch-id", which described the
        # symptom — the forward match had been blanked so the stack looked
        # incomplete. `_reverted` states the fact instead, and names both
        # commits, so a reader can check the ordering by hand.
        self.assertIn("was reverted by", got.unknown)
        self.assertIn(self.REVERT[:12], got.unknown)

    def test_a_reland_after_a_revert_reads_landed(self):
        """The forward index keeps the NEWEST match, so work re-landed after
        its revert wins the ordering again."""
        w = self._world(trunk_patch_ids={"pid1": self.RELAND},
                        trunk_shas=frozenset((self.RELAND, TRUNK_HEAD,
                                              self.REVERT, BASE_SPLIT)),
                        trunk_index={self.RELAND: 0, TRUNK_HEAD: 1,
                                     self.REVERT: 2, BASE_SPLIT: 3},
                        trunk_cts=(DAY5, DAY5, DAY5, DAY4),
                        trunk_reverts={"pid1": self.REVERT})
        got = rowstate.derive(build_row("6b" * 16, "readd"), w)
        self.assertEqual(got.state, "LANDED")
        self.assertEqual(got.because("content-identity")[2], self.RELAND)

    def test_an_unorderable_revert_blocks_the_positive_without_asserting_a_no(self):
        """DELIBERATE INVERSION at the reshape (task/744, T3) — flagged for
        left for review to rule on, because it reverses an arm a previous reviewer
        wrote as store law.

        It read: an undo that cannot be ORDERED decides nothing, so the
        id-binding proof below it must still speak — and it derived LANDED.
        That is the reshape's own disease in miniature. An unorderable undo is
        missing evidence about ORDER, but it is PRESENT evidence that an undo
        EXISTS; letting a different rung answer LANDED past it is exactly an
        inconclusive signal licensing a confident positive. Id binding proves
        an integrator folded this row. It does not prove trunk still CARRIES
        the work, which is the whole content of the word LANDED.

        WHAT IS NOT ASSERTED, per the guard against over-inversion: no
        negative. The row does not read SUPERSEDED or a decisive not-landed;
        it reads its own independently-proved build stage, with the ordering
        gap named in `unknown`. And in production this branch is nearly
        unreachable — `trunk_index` is built from an unbounded `git log` over
        the whole trunk ref, so both a forward match and its undo are in it
        unless that read itself failed. The cost of the strict reading is
        therefore paid only when the snapshot is already damaged.
        """
        w = self._world(trunk_reverts={"pid1": "9999" + "0" * 36},
                        trunk_tokens={"6c" * 4: FOLD_FLEET_NOTES})
        # CONTROL, unconditional: with NO undo at all this exact world reads
        # LANDED, so the refusal below is about the undo and not about a
        # fixture that could never land. It lands through CONTENT IDENTITY —
        # the fold token is present too and is simply never reached, which is
        # the rung order the docstring describes.
        control = rowstate.derive(build_row("6c" * 16, "readd"),
                                  self._world(trunk_tokens={
                                      "6c" * 4: FOLD_FLEET_NOTES}))
        self.assertEqual(control.state, "LANDED")
        self.assertEqual(control.because("content-identity")[0], "lane/readd")
        got = rowstate.derive(build_row("6c" * 16, "readd"), w)
        self.assertEqual(got.state, "BUILDING")
        self.assertNotEqual(got.state, rowstate.LANDED)
        self.assertIn("cannot be ordered", got.unknown)
        # ...and it is INCONCLUSIVE, not a claim of absence: the decisive
        # wording belongs to the ordered case and must not appear here.
        self.assertNotIn("was reverted by", got.unknown)

    def test_an_ordered_revert_refuses_the_id_binding_token(self):
        """The veto law from blocker 4, kept as its own arm now that the
        unorderable case above no longer carries it."""
        vetoed = self._world(trunk_reverts={"pid1": self.REVERT},
                             trunk_tokens={"6c" * 4: FOLD_FLEET_NOTES})
        got = rowstate.derive(build_row("6c" * 16, "readd"), vetoed)
        self.assertEqual(got.state, "BUILDING")
        self.assertNotEqual(got.state, rowstate.LANDED)

    # -------------------------------------- T3: every rung, not just content
    #
    # Blocker 6 asked the revert question INSIDE the content-identity rung,
    # so it guarded one of the four proofs. The other three each stayed true
    # after a revert without being any help: ancestry does not unreach a
    # commit, a tree match describes the commit it matched and not HEAD, and
    # a fold subject is not rewritten when the work is taken back. Each arm
    # below reaches LANDED through ONE of those rungs in its control, then
    # adds the revert and nothing else.

    def test_an_ancestry_land_later_reverted_does_not_read_landed(self):
        spec = {"branches": {"lane/anc": TRUNK_HEAD},
                "branch_commits": {"lane/anc": (TRUNK_HEAD,)},
                "commit_patch_ids": {TRUNK_HEAD: "pidA"},
                "trunk_patch_ids": {"pidA": TRUNK_HEAD},
                "trunk_shas": frozenset((TRUNK_HEAD, self.REVERT, BASE_SPLIT)),
                "trunk_index": {self.REVERT: 0, TRUNK_HEAD: 1, BASE_SPLIT: 2}}
        row = build_row("7a" * 16, "anc")
        control = rowstate.derive(row, world(**spec))
        self.assertEqual(control.state, "LANDED")
        self.assertEqual(control.because("ancestry"), TRUNK_HEAD)
        got = rowstate.derive(row, world(trunk_reverts={"pidA": self.REVERT},
                                         **spec))
        self.assertEqual(got.state, "BUILDING")
        self.assertNotEqual(got.state, rowstate.LANDED)
        self.assertIn("was reverted by", got.unknown)

    def test_a_tree_identity_land_later_reverted_does_not_read_landed(self):
        """The squash case. The folded commit's tree WAS the lane's final
        state byte for byte — and a revert after it is precisely the event
        that makes that no longer describe trunk."""
        lane_tree = "tree" + "9" * 36
        squash = "5c" + "1" * 38
        spec = {"branches": {"lane/squash": LANE_HEAD},
                "branch_commits": {"lane/squash": (LANE_HEAD,)},
                "commit_patch_ids": {LANE_HEAD: "pidS"},
                "commit_trees": {LANE_HEAD: lane_tree, squash: lane_tree,
                                 BASE_SPLIT: "tree" + "1" * 36},
                "trunk_trees": {lane_tree: squash},
                "merge_bases": {"lane/squash": BASE_SPLIT},
                "trunk_shas": frozenset((squash, self.REVERT, BASE_SPLIT)),
                "trunk_index": {self.REVERT: 0, squash: 1, BASE_SPLIT: 2}}
        row = build_row("7b" * 16, "squash")
        control = rowstate.derive(row, world(**spec))
        self.assertEqual(control.state, "LANDED")
        self.assertEqual(control.because("tree-identity"), (squash, lane_tree))
        # The undo is addressable under the LANE COMMIT's forward id, which is
        # what a revert of the squash collides with.
        got = rowstate.derive(row, world(trunk_patch_ids={"pidS": squash},
                                         trunk_reverts={"pidS": self.REVERT},
                                         **spec))
        self.assertEqual(got.state, "BUILDING")
        self.assertNotEqual(got.state, rowstate.LANDED)
        self.assertIn("was reverted by", got.unknown)

    def test_a_visible_revert_with_no_visible_landing_is_decisive(self):
        """THE CAP EDGE. Both scans read the same newest-first `-n <cap>`
        window, so a re-land newer than a visible undo is inside it too. An
        undo the scan CAN see, with no forward match at all, therefore means
        the work is gone — not "the cap might be hiding something", which is
        what a capped scan used to downgrade it to."""
        row = build_row("7c" * 16, "readd")
        w = self._world(trunk_patch_ids={},            # match outside the cap
                        trunk_reverts={"pid1": self.REVERT},
                        patch_scan="capped")
        got = rowstate.derive(row, w)
        self.assertEqual(got.state, "BUILDING")
        self.assertNotEqual(got.state, rowstate.LANDED)
        self.assertIn("cannot hide a re-land newer than itself", got.unknown)


class CurrentCarriageTest(unittest.TestCase):
    """T15 (task/744) — the finding that refuted the whole
    shape's carriage enforcement, and the arm that proves the replacement.

    Every other landing proof in this module is HISTORICAL: a patch-id, a
    tree hash, a fold subject. None survives an ORDINARY LATER EDIT — no
    revert commit, no marker, just somebody changing the same lines back —
    so "it appeared and I know of no revert" is absence of contradiction,
    never current carriage. The exact probe: WORK replaced by OTHER
    derived LANDED through BOTH the ancestry and tree-identity paths, with
    HEAD_has_WORK False and no recognized revert anywhere.

    `rowworld._carriage` answers the positive question instead: replay the
    lane's change onto HEAD with an EXPLICIT base and ask whether the result
    IS HEAD. These arms run it against a REAL repository, because the claim
    is about what git computes."""

    def carriage(self, repo, base, tip):
        """The tri-state answer, BY NAME — "True" / "False" / "None".

        A name rather than the value, and asserted in the test BODY rather
        than inside a helper, for one reason each. `assertIs(x, False)` is a
        positive claim about a tri-state that any data-flow reader must treat
        as an emptiness check, and an assertion buried in a helper is
        invisible to the arm that owns it. `"False"` and `"None"` are
        different strings, so the two answers this relation must never
        confuse cannot satisfy each other's assertion."""
        return str(rowworld._carriage(repo, base, tip, "main"))

    def _repo(self, tmp, head_content):
        """base -> WORK on a lane; trunk head gets `head_content`.
        -> (repo, base, lane tip)"""
        repo, _wt, git = _scratch_repo(tmp)
        with open(os.path.join(repo, "f.txt"), "w") as fh:
            fh.write("base\n")
        git("add", "f.txt")
        git("commit", "-q", "-m", "base")
        base = git("rev-parse", "HEAD")
        git("checkout", "-q", "-b", "lane/work")
        with open(os.path.join(repo, "f.txt"), "w") as fh:
            fh.write("base\nWORK\n")
        git("add", "f.txt")
        git("commit", "-q", "-m", "the work")
        tip = git("rev-parse", "HEAD")
        git("checkout", "-q", "main")
        with open(os.path.join(repo, "f.txt"), "w") as fh:
            fh.write(head_content)
        git("add", "f.txt")
        git("commit", "-q", "-m", "trunk moves")
        return repo, base, tip

    def test_head_carrying_the_work_is_affirmed(self):
        """THE CONTROL, and it is the arm that makes the refusal below
        meaningful: a relation that answered False for everything would pass
        the refusal and prove nothing."""
        with tempfile.TemporaryDirectory() as tmp:
            repo, base, tip = self._repo(tmp, "base\nWORK\n")
            self.assertEqual(self.carriage(repo, base, tip), "True")

    def test_work_replaced_by_an_ordinary_edit_is_NOT_affirmed(self):
        """The specimen, and the assertion moved from "False" to "None"
        under their revised ruling. The relation never claims ABSENCE now:
        no witness affirms, so the answer is UNKNOWN. Absence needs its own
        POSITIVE artifact — a recognized revert — because a non-affirming
        witness cannot tell "the work is gone" from "I cannot see it", and
        the population proved how often it is the second (98 of 105
        known-landed rows answered non-affirming)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo, base, tip = self._repo(tmp, "base\nOTHER\n")
            self.assertEqual(self.carriage(repo, base, tip), "None")
            self.assertNotEqual(self.carriage(repo, base, tip), "True")

    def test_a_DISTANT_follow_on_that_keeps_the_work_is_affirmed(self):
        """WHERE THE REPLAY BEATS A PATH MANIFEST, measured rather than
        assumed. A postimage-blob comparison rejects ANY later commit that
        touches the same file, even one that keeps every line of the work.
        The replay admits it — as long as the follow-on is not adjacent."""
        with tempfile.TemporaryDirectory() as tmp:
            repo, _wt, git = _scratch_repo(tmp)
            with open(os.path.join(repo, "f.txt"), "w") as fh:
                fh.write("a\nb\nc\nd\ne\nf\ng\nh\ni\nj\n")
            git("add", "f.txt")
            git("commit", "-q", "-m", "base")
            base = git("rev-parse", "HEAD")
            git("checkout", "-q", "-b", "lane/work")
            with open(os.path.join(repo, "f.txt"), "w") as fh:
                fh.write("a\nWORK\nb\nc\nd\ne\nf\ng\nh\ni\nj\n")
            git("add", "f.txt")
            git("commit", "-q", "-m", "the work")
            tip = git("rev-parse", "HEAD")
            git("checkout", "-q", "main")
            with open(os.path.join(repo, "f.txt"), "w") as fh:
                fh.write("a\nWORK\nb\nc\nd\ne\nf\ng\nh\ni\nj\n"
                         "FOLLOW-ON\n")
            git("add", "f.txt")
            git("commit", "-q", "-m", "landed plus a distant follow-on")
            self.assertEqual(self.carriage(repo, base, tip), "True")

    def test_an_ADJACENT_follow_on_is_refused_and_that_is_a_real_limit(self):
        """MEASURED, AND IT CONTRADICTS THE REASON THIS RELATION WAS CHOSEN.

        The replay was proposed over a path manifest because it "admits
        follow-ons that retain the work". For a follow-on on the line
        IMMEDIATELY AFTER the work, it does not: both sides added different
        content after the same base line, which is an ordinary 3-way
        CONFLICT — so that witness cannot speak, and with no other witness
        affirming, the relation answers None. (It said "answers False" until
        the revised ruling; a non-affirming witness is silence, never a
        claim that the work is gone.)

        The answer is still CONSERVATIVE — it refuses to claim carriage
        rather than inventing it — so nothing is unsafe here. But an
        adjacent follow-on is routine in this codebase (land a lane, then
        edit the same function), so the practical cost is higher than the
        proposal assumed. Pinned as a KNOWN LIMIT so nobody rediscovers it
        as a bug, and reported back rather than shipped quietly."""
        with tempfile.TemporaryDirectory() as tmp:
            repo, base, tip = self._repo(tmp, "base\nWORK\nFOLLOW-ON\n")
            self.assertEqual(self.carriage(repo, base, tip), "None")

    def test_an_EMPTY_COMMIT_pair_cannot_affirm_anything(self):
        """The blocker, and it was UNARMED until it was mutation-tested:
        replacing `_nonempty_delta`'s tree comparison with `return True`
        left the whole class passing 11/11. The check was built without
        the arm that can kill it.

        THE SHAPE: `git commit --allow-empty` gives B != T — different ids,
        so the base==tip rung waves it through — with the SAME TREE. The
        delta is empty, so the replay reproduces whatever HEAD it is handed
        and the touched-path set is satisfied trivially. Both witnesses
        affirm, against a HEAD the work has never been near.

        The fixture is a REAL empty commit against an UNRELATED head, so a
        mutation that drops the tree comparison turns this red."""
        with tempfile.TemporaryDirectory() as tmp:
            repo, _wt, git = _scratch_repo(tmp)
            with open(os.path.join(repo, "f.txt"), "w") as fh:
                fh.write("base\n")
            git("add", "f.txt")
            git("commit", "-q", "-m", "base")
            base = git("rev-parse", "HEAD")
            git("commit", "-q", "--allow-empty", "-m", "an empty round")
            empty = git("rev-parse", "HEAD")
            # CONTROLS, unconditional: the two commits really are DISTINCT
            # ids sharing ONE tree — the exact shape base==tip cannot catch.
            self.assertNotEqual(base, empty)
            self.assertEqual(git("rev-parse", base + "^{tree}"),
                             git("rev-parse", empty + "^{tree}"))
            # ...and HEAD is somewhere else entirely.
            git("checkout", "-q", "-b", "elsewhere", base)
            with open(os.path.join(repo, "g.txt"), "w") as fh:
                fh.write("unrelated\n")
            git("add", "g.txt")
            git("commit", "-q", "-m", "unrelated head")
            self.assertEqual(
                str(rowworld._carriage(repo, base, empty, "elsewhere")),
                "None")

    def test_an_unaskable_carriage_question_is_None_not_False(self):
        """THREE ANSWERS, NOT TWO — and after the revised ruling the
        third is the ordinary one. False is reserved for a POSITIVE
        anti-carriage artifact, which this relation does not currently mint.
        None means NO WITNESS AFFIRMED, covering both "the question could not
        be asked" and "no witness could see it". They must not collapse into
        each other or into False: a refusal invented from an unrunnable probe
        is the false negative this whole round is about."""
        with tempfile.TemporaryDirectory() as tmp:
            repo, base, tip = self._repo(tmp, "base\nWORK\n")
            # CONTROL FIRST: with both ends present this repo answers True,
            # so the Nones below are about the missing end and not about a
            # relation that cannot speak at all.
            self.assertEqual(self.carriage(repo, base, tip), "True")
            self.assertEqual(self.carriage(repo, base, ""), "None")
            self.assertEqual(self.carriage(repo, "", tip), "None")

    def test_a_base_recomputed_against_HEAD_would_affirm_ANY_head(self):
        """THE VACUITY CAUGHT BEFORE THE BUILD (meld e:1786207689).

        I proposed keying the replay on merge-base(tip, current trunk). Once
        the work lands directly, that merge-base BECOMES the tip — the replay
        is EMPTY, and an empty replay reproduces every possible HEAD. The
        check would pass hardest exactly where it can see least, and it is
        not an edge case: work that landed HAS a base that advanced.

        Measured against real git rather than argued. The chain-bound base
        DOES NOT AFFIRM the work-replaced HEAD, while the recomputed base
        would have affirmed anything at all — and after the revised
        ruling the difference is None-versus-True rather than False-versus-
        True, because the relation no longer claims ABSENCE. That is a
        weaker-looking pair of words carrying the same load: a witness that
        does not speak cannot authorize LANDED, which is the whole
        guarantee."""
        with tempfile.TemporaryDirectory() as tmp:
            repo, chain_base, tip = self._repo(tmp, "base\nOTHER\n")
            # The chain-bound base does not affirm: no witness can see it.
            self.assertEqual(self.carriage(repo, chain_base, tip), "None")
            # The RECOMPUTED base is the tip itself once the work is on
            # trunk's side of the fork — replay nothing, reproduce anything.
            rc, recomputed = rowworld._git(repo, "merge-base", "main", tip)
            self.assertEqual(rc, 0)
            # STRENGTHENED by task/756, which found the SAME vacuity through
            # another door — a review row whose chain has no build row fell
            # back to a base that IS its own tip. The degenerate pair is now
            # refused DEFENSIVELY in the relation itself, so a future rewiring
            # of B cannot resurrect the hole: the answer is None (unaskable),
            # never True. What this arm pins is therefore no longer "the
            # vacuity exists" but "the vacuity is refused at the source".
            self.assertEqual(self.carriage(repo, tip, tip), "None",
                             "an empty replay must never affirm: it "
                             "reproduces whatever HEAD it is handed")
            self.assertGreater(len(recomputed.strip()), 6)

    def test_the_work_pair_never_uses_a_build_rows_own_tip_as_its_work(self):
        """The OTHER half of the same ruling. A build row's recorded tip IS
        its base — 48 of 52 live build refs are already ancestors of trunk —
        so spending it as T would affirm carriage for every build row ever
        dispatched, through the very rung added to stop that."""
        build = build_row("f8" * 16, "solo")
        # BOUND AS ONE PAIR so the positive and the absence sit on the SAME
        # observable: asserting `base` proves nothing about a `tip` the
        # reader cannot connect to it, and a pair that came back empty would
        # satisfy the absence alone.
        pair = rowworld._work_pair(build, {build["id"]: build}, {})
        # The length pin is the POSITIVE the rung can read: comparing against
        # a module CONSTANT compares two names, and proves nothing if the
        # constant is empty — the same shape already fixed on the state
        # words elsewhere in this file.
        self.assertGreater(len(pair[0] or ""), 6)
        self.assertEqual(pair[0], BASE_SPLIT)      # its tip IS the base
        self.assertIsNone(pair[1], "a build row with no carrier and no "
                                   "verified retip has no immutable work "
                                   "tip, and must read UNKNOWN rather than "
                                   "borrow its base")
        # CONTROL: give it a carrier that reviewed real work, and the pair
        # completes — so the None above is about the missing carrier.
        review = review_row("f9" * 16, "solo", reviewed_tip=TRUNK_HEAD)
        base2, tip2 = rowworld._work_pair(
            build, {build["id"]: build, review["id"]: review},
            {build["id"]: review["id"]})
        self.assertEqual(base2, BASE_SPLIT)
        self.assertEqual(tip2, TRUNK_HEAD)

    def test_the_LEDGER_landed_close_is_NOT_re_measured(self):
        """INVERTED by the revised ruling (task/756), and the inversion
        is the whole lesson of the round.

        The first shape required affirmative carriage at EVERY door
        including this one. Then the population said what that costs: of the
        196 rows the ledger itself closed as `landed` — proof fields,
        gate-verified — 98 of 105 askable came back non-affirming and 91 had
        no askable pair at all. Requiring a live re-measurement there makes
        one scalar state pretend a RECORDED EVENT never happened, because a
        present-content question about old work usually cannot be answered.

        A structured close is a MONOTONIC LIFECYCLE PROOF: landing OCCURRED,
        written under the fold's own gates when the artifacts still supported
        it. Current carriage answers a different question and belongs on the
        INFERRED candidates — ancestry, tree, content, id binding — and on
        the explicitly-current `carried` verb, which is where a stale claim
        can actually be minted."""
        spec = {"branches": {"lane/led": TRUNK_HEAD},
                "branch_commits": {"lane/led": (TRUNK_HEAD,)}}
        row = build_row("d5" * 16, "led", status="closed",
                        closed_by_landing=True, landing_trunk_sha=TRUNK_HEAD)
        # THE RECORDED CLOSE STANDS WHATEVER THE LIVE MEASUREMENT SAYS —
        # including when it cannot be made at all.
        # UNCONDITIONAL first: the fixture really does reach LANDED at all.
        self.assertEqual(rowstate.derive(row, world(**spec)).state, "LANDED")
        for answer in (True, False, None):
            got = rowstate.derive(row, world(carriage={"d5" * 16: answer},
                                             **spec))
            self.assertEqual(got.state, "LANDED", answer)
        # ...and the CONTRAST that keeps this from being a blanket bypass: an
        # INFERRED candidate on the same world still needs the affirmation.
        inferred = rowstate.derive(build_row("d8" * 16, "led"),
                                   world(carriage={"d8" * 16: False}, **spec))
        self.assertNotEqual(inferred.state, rowstate.LANDED)

    def test_a_CARRIED_closure_does_NOT_self_exempt(self):
        """The direct pure-derive probe on this tip, and it is the
        circular case my first cut let through.

        The recorded-landing exemption is for proofs INDEPENDENT of the
        question. A `landed` close records that LANDING OCCURRED, which no
        later measurement can un-happen. A `carried` close's ENTIRE CONTENT
        is a current-carriage claim — so exempting it from current-carriage
        re-measurement means the row asserts LANDED from a record of a
        measurement nobody is allowed to re-take. I spelled the exemption
        "any closure", and that let the one dependent closure through."""
        spec = {"branches": {"lane/x": TRUNK_HEAD},
                "branch_commits": {"lane/x": (TRUNK_HEAD,)}}
        rid = "e7" * 16
        row = build_row(rid, "x", status="closed", close_reason="carried")
        self.assertEqual(                            # control: it CAN land
            rowstate.derive(row, world(carriage={rid: True}, **spec)).state,
            "LANDED")
        for answer in (False, None):
            got = rowstate.derive(row, world(carriage={rid: answer}, **spec))
            self.assertEqual(got.state, "UNKNOWN", answer)
        # ...and the CONTRAST that keeps the exemption alive where it belongs:
        # a `landed` close with the same unaskable carriage stays LANDED.
        landed = build_row("e8" * 16, "x", status="closed",
                           close_reason="landed", landing_trunk_sha=TRUNK_HEAD)
        self.assertEqual(
            rowstate.derive(landed,
                            world(carriage={"e8" * 16: None}, **spec)).state,
            "LANDED")

    def test_a_chain_hop_also_needs_affirmative_carriage(self):
        """The second bypass. `_hop_lands` returned a successor's
        `closed_by_landing` closure BEFORE asking `_landed`, so a successor
        whose work trunk no longer carries still discharged its parent."""
        pid, kid = "d6" * 16, "d7" * 16
        parent = build_row(pid, "par", superseded_by=kid)
        successor = build_row(kid, "kid", status="closed",
                              closed_by_landing=True,
                              landing_trunk_sha=TRUNK_HEAD)
        spec = {"rows": {kid: successor}, "carriers": {pid: kid},
                "branches": {"lane/kid": TRUNK_HEAD},
                "branch_commits": {"lane/kid": (TRUNK_HEAD,)}}
        yes = rowstate.derive(parent, world(carriage={"d7" * 16: True},
                                            **spec))
        self.assertEqual(yes.state, "LANDED")           # control
        for answer in (False, None):
            got = rowstate.derive(parent, world(carriage={"d7" * 16: answer},
                                                **spec))
            self.assertNotEqual(got.state, rowstate.LANDED, answer)

    def test_derive_refuses_LANDED_when_carriage_is_refused(self):
        """The rung, not just the reader. Ancestry alone used to authorize
        LANDED here; now it is a CANDIDATE that only an affirming witness can
        promote — and a non-affirming one leaves the row at UNKNOWN rather
        than asserting the work is absent."""
        spec = {"branches": {"lane/c": TRUNK_HEAD},
                "branch_commits": {"lane/c": (TRUNK_HEAD,)}}
        row = build_row("c9" * 16, "c")
        # CONTROL: the same world with carriage AFFIRMED still lands.
        yes = rowstate.derive(row, world(carriage={"c9" * 16: True}, **spec))
        self.assertEqual(yes.state, "LANDED")
        # BOTH REFUSALS END AT UNKNOWN NOW, not at a build stage. The rung
        # that used to live inside `_landed` let the ladder carry on to the
        # gate ladder and report BUILDING; the NEW ROOT moved the check
        # to `_enforce`, which is a DOWNGRADE to UNKNOWN by construction. One
        # place, one answer — and the reason matters more than the word: a
        # row whose carriage is refused is not "still building", it is a row
        # nobody can currently place.
        for answer in (False, None):
            got = rowstate.derive(row, world(carriage={"c9" * 16: answer},
                                             **spec))
            self.assertEqual(got.state, "UNKNOWN", answer)
            self.assertNotEqual(got.state, rowstate.LANDED, answer)
            self.assertIn("does not affirm", got.unknown, answer)


class MandatoryGateTest(unittest.TestCase):
    """THE SECOND ROOT (L1/L4/L5/L6, task/744). The first cure took the
    per-case shape INSIDE the ladder and left the ladder's own doors uncured:
    several rungs returned a confident state before the invariants that could
    refute it had run. `_lifecycle`'s terminal answered LANDED at the very
    top — three lines after a docstring saying no rung may answer before a
    higher-precedence fact has been consulted.

    So the arms here do not test four behaviours. They test ONE property from
    four directions: no confident state is reachable without passing
    `_enforce`. The last arm asserts that property structurally, so a fifth
    door added later cannot quietly reintroduce the class."""

    REVERT = "0ff" + "0" * 37

    def _reverted_world(self, **over):
        """A lane whose work landed and was then REVERTED — so every
        historical proof still says yes and current carriage says no."""
        spec = {"branches": {"lane/gone": TRUNK_HEAD},
                "branch_commits": {"lane/gone": (TRUNK_HEAD,)},
                "commit_patch_ids": {TRUNK_HEAD: "pidG"},
                "trunk_patch_ids": {"pidG": TRUNK_HEAD},
                "trunk_reverts": {"pidG": self.REVERT},
                "trunk_shas": frozenset((TRUNK_HEAD, self.REVERT, BASE_SPLIT)),
                "trunk_index": {self.REVERT: 0, TRUNK_HEAD: 1, BASE_SPLIT: 2}}
        spec.update(over)
        return world(**spec)

    def test_L5_a_landed_closure_does_not_survive_a_later_revert(self):
        """The ledger RECORDED a landing, and that record is true history.
        LANDED is a claim about HEAD, and history is not HEAD."""
        row = build_row("9a" * 16, "gone", status="closed",
                        closed_by_landing=True,
                        landing_trunk_sha=TRUNK_HEAD)
        # CONTROL: with no revert, this exact row reads LANDED off the ledger
        # alone — so the arm below is about the revert, not the closure.
        control = rowstate.derive(row, self._reverted_world(trunk_reverts={}))
        self.assertEqual(control.state, "LANDED")
        got = rowstate.derive(row, self._reverted_world())
        self.assertEqual(got.state, "UNKNOWN")
        self.assertNotEqual(got.state, rowstate.LANDED)
        self.assertIn("reverted", got.unknown)

    def test_L1_a_withdrawn_close_does_not_hide_a_contrary_landing(self):
        """`withdrawn` says nobody owes anything further. If the work landed
        ANYWAY under a change-demanding verdict, that is a disagreement, and
        CANCELLED reports it as tidy."""
        landed = {"branches": {"lane/gone": TRUNK_HEAD},
                  "branch_commits": {"lane/gone": (TRUNK_HEAD,)}}
        row = review_row("9b" * 16, "gone", status="closed",
                         close_reason="withdrawn", polarity="fix",
                         reviewed_tip=TRUNK_HEAD)
        # CONTROL: the same closure with the work NOT on trunk reads
        # CANCELLED, so the arm is about the contrary and not the reason.
        control = rowstate.derive(
            review_row("9b" * 16, "gone", status="closed",
                       close_reason="withdrawn", polarity="fix",
                       reviewed_tip=LANE_HEAD), world(**landed))
        self.assertEqual(control.state, "CANCELLED")
        got = rowstate.derive(row, world(
            trunk_tokens={"9b" * 4: FOLD_PRE_JOIN}, **landed))
        self.assertEqual(got.state, "UNKNOWN")
        self.assertNotEqual(got.state, rowstate.CANCELLED)
        self.assertEqual(got.because("contrary")[0], "fix")

    def test_L1_a_withdrawn_close_with_NO_verdict_still_surfaces_a_contrary(self):
        """The L1 REFINEMENT, and it named a gap the first L1 arm could
        not see. Close-over-polarity IS valid — a close concludes the row —
        but close-over-LATER-ARTIFACT is not.

        My first cut asked only about a VERDICT'S polarity, so the arm above
        passes (withdrawn close + `fix` verdict + work on trunk => UNKNOWN)
        while THIS case did not: a row closed `withdrawn` with NO verdict at
        all, whose work then landed, read CANCELLED — indistinguishable from
        the same close with the work ABSENT. The close ITSELF is the
        change-demanding statement: it says nobody owes anything and nothing
        is coming, and trunk carrying the work contradicts it.

        The control is the whole point of the arm: the SAME row with the work
        NOT on trunk must stay CANCELLED, or the cure would simply have
        broken every honest withdrawal."""
        landed = {"branches": {"lane/w": TRUNK_HEAD},
                  "branch_commits": {"lane/w": (TRUNK_HEAD,)}}
        row = build_row("a9" * 16, "w", status="closed",
                        close_reason="withdrawn")
        # CONTROL, unconditional: an honest withdrawal — nothing on trunk —
        # is still a plain CANCELLED and must not be disturbed.
        honest = rowstate.derive(build_row("a9" * 16, "nope", status="closed",
                                           close_reason="withdrawn"), world())
        self.assertEqual(honest.state, "CANCELLED")
        got = rowstate.derive(row, world(**landed))
        self.assertEqual(got.state, "UNKNOWN")
        self.assertNotEqual(got.state, rowstate.CANCELLED)
        self.assertEqual(got.because("contrary")[0], "close:withdrawn")

    def test_the_non_delivery_set_is_derived_from_the_terminal_table(self):
        """NOT RETYPED. A reason means "nothing is coming" exactly when its
        conservative terminal is CANCELLED, so a ninth close reason added to
        `_CLOSE_TERMINAL` arrives here already classified. Two hand-kept
        lists would drift, and the drift would be silent in the direction
        that hides a contradiction."""
        cancelling = {r for r, s in rowstate._CLOSE_TERMINAL.items()
                      if s == rowstate.CANCELLED}
        self.assertIn("withdrawn", cancelling)          # MUST-HIT
        self.assertGreater(len(cancelling), 1)
        for reason in cancelling:
            self.assertEqual(
                rowstate._non_delivery({"close_reason": reason}),
                "close:" + reason)
        for reason in set(rowstate._CLOSE_TERMINAL) - cancelling:
            self.assertIsNone(rowstate._non_delivery({"close_reason": reason}),
                              reason)

    def test_L6_an_open_review_whose_obligation_moved_is_not_reviewing(self):
        """REVIEWING bills a named reviewer. A row whose carrier resolved to a
        successor is billing them for work that moved."""
        rid = "9c" * 16
        row = review_row(rid, "moved", status="open")
        # CONTROL: with NO carrier the same row is genuinely REVIEWING.
        control = rowstate.derive(row, world())
        self.assertEqual(control.state, "REVIEWING")
        got = rowstate.derive(row, world(carriers={rid: "9d" * 16}))
        self.assertEqual(got.state, "SUPERSEDED")
        self.assertNotEqual(got.state, rowstate.REVIEWING)

    def test_L4_a_held_row_is_not_a_review_in_progress(self):
        """HELD is an acknowledged pause with an external dependency, and
        `helm dispatch release` is the transition that reopens it. Reading it
        as REVIEWING bills a reviewer who has already said why they cannot."""
        control = rowstate.derive(review_row("9e" * 16, "paused",
                                             status="open"), world())
        self.assertEqual(control.state, "REVIEWING")
        got = rowstate.derive(review_row("9e" * 16, "paused", status="held"),
                              world())
        self.assertEqual(got.state, "UNKNOWN")
        self.assertNotEqual(got.state, rowstate.REVIEWING)
        self.assertIn("HELD", got.unknown)

    def test_enforce_only_ever_downgrades_to_UNKNOWN(self):
        """Supplement 1, and it caught this property asserted in a
        commit message while the code broke it three screens below: the gate
        turned REVIEWING into SUPERSEDED, MINTING a new confident word.

        Carrier resolution moved into candidate production, where it belongs,
        and this arm pins the invariant itself rather than the one instance:
        for EVERY state the ladder can produce, `_enforce` returns either
        that same state or UNKNOWN. A gate that can promote is a second
        ladder wearing a gate's name."""
        row = review_row("e9" * 16, "any", status="open")
        # UNCONDITIONAL: the loop below is the whole arm, so an empty
        # vocabulary would pass it without testing anything.
        self.assertGreater(len(rowstate.STATES), 10)
        for state in rowstate.STATES:
            for facts_world in (world(), world(carriers={"e9" * 16: "ea" * 16})):
                facts = rowstate._Mandatory(row, facts_world, None)
                got = rowstate._enforce(
                    rowstate.Derivation(state, ()), row, facts)
                self.assertIn(got.state, (state, rowstate.UNKNOWN),
                              "_enforce promoted %s to %s" % (state, got.state))

    def test_the_gate_is_the_only_door_to_a_confident_state(self):
        """THE STRUCTURAL ARM, and the reason the four above are not enough.

        Each of L1/L4/L5/L6 was one rung that returned early; fixing four
        rungs leaves the FIFTH door — the one somebody adds next month —
        wide open. `derive` is therefore a wrapper with exactly one path to a
        confident answer, and this asserts that shape rather than its
        symptoms: the ladder is a separate function, it does not answer
        callers, and every confident word it can produce goes through
        `_enforce`.

        MUTATION-PROOF, and the measurement corrected my guess about it. I
        expected this arm to survive a gutted `_enforce` and catch only a
        `derive` that stopped CALLING it — a clean division of labour with
        the four behaviour arms. Measured: gutting `_enforce` to `return
        candidate` kills all FIVE, because the last assertion below drives the
        door directly and watches it downgrade. That is a stronger result
        than the one I wrote down, and the sentence is corrected rather than
        the test: a rationale that survives its own disproof is the trap this
        whole round is about."""
        import inspect
        source = inspect.getsource(rowstate.derive)
        self.assertIn("_ladder(", source)
        self.assertIn("_enforce(", source)
        # The ladder is not reachable as the public answer: `derive` is what
        # callers import, and it cannot return a confident candidate without
        # `_enforce` having seen it.
        self.assertEqual(source.count("return _enforce("), 1)
        confident = [line for line in source.splitlines()
                     if "return Derivation(" in line]
        for line in confident:
            self.assertIn("UNKNOWN", line,
                          "derive returns a confident state directly, "
                          "bypassing the gate: %r" % line.strip())
        # ...and the door really downgrades, measured rather than asserted
        # from its shape: a LANDED candidate whose work trunk TOOK BACK loses.
        #
        # THIS ASSERTION USED TO DRIVE THE OTHER RULE — a candidate with
        # nothing carrying it — and that rule was wrong in the opposite
        # direction to L5. Refusing LANDED for MISSING evidence discards a
        # machine-written terminal whenever the branch has been reaped, and
        # it took six chain arms and one ledger arm red to say so. L5 names a
        # POSITIVE contradiction, so the door refuses on one.
        row = build_row("9f" * 16, "undone")
        undone = world(
            branches={"lane/undone": TRUNK_HEAD},
            branch_commits={"lane/undone": (TRUNK_HEAD,)},
            commit_patch_ids={TRUNK_HEAD: "pidX"},
            trunk_patch_ids={"pidX": TRUNK_HEAD},
            trunk_reverts={"pidX": self.REVERT},
            trunk_shas=frozenset((TRUNK_HEAD, self.REVERT, BASE_SPLIT)),
            trunk_index={self.REVERT: 0, TRUNK_HEAD: 1, BASE_SPLIT: 2})
        facts = rowstate._Mandatory(row, undone, TRUNK_HEAD)
        self.assertTrue(facts.reverted)       # control: the undo is measured
        got = rowstate._enforce(
            rowstate.Derivation(rowstate.LANDED, ()), row, facts)
        self.assertEqual(got.state, "UNKNOWN")


class ScanFailureTest(unittest.TestCase):
    """T4 (task/744): three scans in `rowworld.build` dropped their error on
    the floor, so the map each failed to fill was indistinguishable from a map
    with nothing to put in it. Every consumer then read that silence as a
    POSITIVE fact about the repository — nothing was reverted, this lane has
    no commits — which is the same false-certainty shape as the rungs above,
    one layer down.

    Each arm proves the failure changes the answer, against a control on the
    same world with the scan succeeding."""

    def _landing(self, **over):
        """A build row whose lane head is on trunk: LANDED by ancestry, and
        therefore gated by the revert index after T3."""
        spec = {"branches": {"lane/scan": TRUNK_HEAD},
                "branch_commits": {"lane/scan": (TRUNK_HEAD,)},
                "commit_patch_ids": {TRUNK_HEAD: "pidZ"},
                "trunk_patch_ids": {"pidZ": TRUNK_HEAD}}
        spec.update(over)
        return build_row("8a" * 16, "scan"), world(**spec)

    def test_an_unreadable_revert_index_is_not_a_clean_bill_of_health(self):
        row, clean = self._landing()
        # CONTROL: with the scan readable and empty, this world DOES land.
        control = rowstate.derive(row, clean)
        self.assertEqual(control.state, "LANDED")
        _row, broken = self._landing(
            reverts_unavailable="git patch-id failed while indexing patch-ids")
        got = rowstate.derive(row, broken)
        self.assertNotEqual(got.state, rowstate.LANDED)
        self.assertIn("revert index could not be built", got.unknown)

    def test_an_unwalkable_branch_is_not_an_empty_branch(self):
        """UNSTARTED and BUILDING are both positive claims about what a lane
        CONTAINS, and a failed `rev-list` leaves the ref absent from
        `branch_commits` exactly as an empty branch does."""
        # CONTROL: an ACTUALLY empty branch reads UNSTARTED, so the arm below
        # is about the walk failing and not about the ref being absent.
        empty = rowstate.derive(
            build_row("8b" * 16, "scan"),
            world(branches={"lane/scan": LANE_HEAD},
                  branch_commits={"lane/scan": ()}))
        self.assertEqual(empty.state, "UNSTARTED")
        got = rowstate.derive(
            build_row("8b" * 16, "scan"),
            world(branches={"lane/scan": LANE_HEAD},
                  branch_walk_failed=frozenset(("lane/scan",))))
        self.assertEqual(got.state, "UNKNOWN")
        self.assertNotEqual(got.state, rowstate.UNSTARTED)
        self.assertNotEqual(got.state, rowstate.BUILDING)
        self.assertIn("unwalkable branch is not an empty one", got.unknown)

    def test_a_failed_lane_scan_does_not_blame_the_commits(self):
        """The commits are fine; nobody could run `git log -p` over them. The
        state is inconclusive either way — what changes is whether the reason
        sends a reader to inspect the branch or to fix the scan."""
        spec = {"branches": {"lane/scan": LANE_HEAD},
                "branch_commits": {"lane/scan": (LANE_HEAD,)}}
        blamed = rowstate.derive(build_row("8c" * 16, "scan"), world(**spec))
        self.assertEqual(blamed.state, "BUILDING")
        self.assertIn("have no readable patch-id", blamed.unknown)
        self.assertNotIn("scan failed", blamed.unknown)
        got = rowstate.derive(build_row("8c" * 16, "scan"), world(
            lane_scan_unavailable="git log failed while indexing patch-ids",
            **spec))
        self.assertEqual(got.state, "BUILDING")
        self.assertIn("lane patch-id scan failed", got.unknown)
        self.assertIn("through no fault of their own", got.unknown)


class DatingTest(unittest.TestCase):
    """BLOCKER 5 (gate:9b5029474fb63d13): 'Strict mode fabricates
    historical ref membership from commit timestamps... Current history cannot
    reconstruct past ref membership this way; missing evidence must remain
    UNKNOWN.' The cure proves ONE direction only — a tip whose committer
    instant postdates the dispatch did not exist to be on trunk earlier — and
    treats everything else as inconclusive."""

    def test_an_old_stamped_neighbour_no_longer_decides_anything(self):
        """T10's CONSEQUENCE arm — INVERTED at the reshape (task/744).

        This arm was built against the dating reconstruction: an old-stamped
        commit at a NEWER position (created early, merged late — it keeps its
        committer stamp) dragged 'head at dispatch time' to position 0, so
        every tip read as predating and a successor's real post-dispatch land
        was vetoed into SUPERSEDED. It asserted LANDED to prove the veto gone.

        The reshape deleted the mechanism instead of repairing it, so BOTH
        readings of this fixture are now wrong. Committer stamps are forgeable
        (a rebase rewrites them), and the successor here is a REVIEW row —
        whose landing may only be proven by content identity or id binding,
        never by ancestry. So its tip on trunk proves nothing about it, and
        the parent's honest state is SUPERSEDED: the work moved to a successor
        and where that successor went is the successor's own question.

        SUPERSEDED here is NOT the old veto wearing the same word. The veto
        was a positive claim built from a fabricated ordering; this is the
        conservative rung naming what it could not establish, and the arm
        asserts that REASON so the two can never be confused again."""
        merged_late = "ab" + "1" * 38     # ct DAY4, sits at position 0
        landed_tip = "cd" + "1" * 38      # ct DAY5, position 1 — the real land
        parent = build_row("4a" * 16, "dated", superseded_by="4b" * 16)
        successor = review_row("4b" * 16, "dated-2", reviewed_tip=landed_tip,
                               ts="2026-08-04T12:00:00Z")
        w = world(rows={successor["id"]: successor},
                  carriers={parent["id"]: successor["id"]},
                  trunk_shas=frozenset((merged_late, landed_tip, BASE_SPLIT)),
                  trunk_index={merged_late: 0, landed_tip: 1, BASE_SPLIT: 2},
                  trunk_cts=(DAY4, DAY5, DAY4))
        # CONTROL: the fixture STILL supplies the dating story in full — the
        # successor's tip is stamped DAY5, well past its DAY4-noon dispatch,
        # and the neighbour is old-stamped at the newer position. The point of
        # keeping it is that NONE of it reaches a decision any more.
        #
        # The old `assertGreater(DAY5, DAY4 + 12 * 3600)` that sat here is
        # gone: it compared two module constants and would hold with this
        # module deleted. An assertion about arithmetic is not a control on a
        # derivation, and it read as one.
        self.assertEqual(w.trunk_index[merged_late], 0)
        self.assertIn(landed_tip, w.trunk_shas)       # control: it IS on trunk
        got = rowstate.derive(parent, w)
        self.assertEqual(got.state, "SUPERSEDED")
        self.assertNotEqual(got.state, rowstate.LANDED)
        # ...and SUPERSEDED for the conservative reason, not the fabricated
        # one. Without this the arm would pass under a restored veto.
        self.assertIn("the successor is not bound to trunk", got.unknown)
        # THE EVIDENCE IS NAMED POSITIVELY BEFORE THE ABSENCE IS CLAIMED. A
        # filtered comprehension asserted `== []` reads as a check and proves
        # nothing if `got.evidence` is empty outright — the same shape as
        # asserting a grep found no match without proving the grep ran.
        kinds = [e.kind for e in got.evidence]
        self.assertIn("superseded-by", kinds)
        self.assertNotIn("supersession-chain", kinds,
                         "a chain that LANDED the parent would mean ancestry "
                         "still lands a review row through the back door")

    def test_an_undatable_tip_on_trunk_does_not_land_an_open_review(self):
        """MUST-MISS, ordinary path: an OPEN review of the BASE — on trunk
        since before the dispatch — must not read LANDED; the truthful state
        is REVIEWING with the dating gap named."""
        row = review_row("4c" * 16, "based", status="open",
                         reviewed_tip=BASE_SPLIT,
                         ts="2026-08-05T12:00:00Z")
        w = world()
        self.assertIn(BASE_SPLIT, w.trunk_shas)      # control: it IS on trunk
        got = rowstate.derive(row, w)
        self.assertNotEqual(got.state, rowstate.LANDED)
        self.assertEqual(got.state, "REVIEWING")
        # THE STATE ASSERTIONS ABOVE WERE ALREADY RIGHT; ONLY THE REASON MOVED
        # (task/744, T10). This asserted "cannot be dated after dispatch",
        # which named the DATING GAP — and the dating inference is gone, so
        # that sentence would pin a model the code no longer holds. The row
        # does not land because ancestry is a BUILD-row proof, full stop: a
        # review row names the object it looked at, not its own work.
        self.assertIn("ancestry alone proves nothing here", got.unknown)
        self.assertNotIn("dated after dispatch", got.unknown,
                         "the committer-date rationale must not survive the "
                         "rung that stopped consulting committer dates")

    def test_a_dated_land_does_NOT_land_an_open_review(self):
        """INVERTED by task/744, and the pair above is why it had to be.

        This arm asserted that an open review whose tip was "PROVEN created
        after dispatch" reads LANDED — proven by the COMMITTER DATE, which a
        rebase rewrites at will. It is not proof of anything, and the sibling
        immediately below already asserted REVIEWING for the case where the
        stamp could not be parsed. So the suite encoded the exact
        false-certainty shape: UNREADABLE forgeable evidence answered
        honestly, READABLE forgeable evidence minted confidence.

        THE REVIEWER'S OBLIGATION DOES NOT EVAPORATE BECAUSE THE AUTHOR'S
        WORK MERGED. An open review row is owed a verdict whether or not its
        subject tip reached trunk; the tip on trunk is somebody else's
        landing. That is rung 3 outranking the landed shortcut.

        WHAT IS NOT ERASED, per the guard against over-inversion: the
        row still reports its stage from the evidence that DOES prove one.
        REVIEWING is an affirmative machine-recorded state, not a shrug —
        default-UNKNOWN removes the authority of forgeable evidence, never
        the row's independently-proved stage."""
        row = review_row("4d" * 16, "based", status="open",
                         reviewed_tip=TRUNK_HEAD,
                         ts="2026-08-04T06:00:00Z")
        got = rowstate.derive(row, world())
        self.assertEqual(got.state, "REVIEWING")
        self.assertNotEqual(got.state, rowstate.LANDED,
                            "a committer date is not proof that a still-open "
                            "review's work is the landing on trunk")

    def test_an_unparseable_dispatch_stamp_is_inconclusive_not_a_landing(self):
        row = review_row("4e" * 16, "based", status="open",
                         reviewed_tip=TRUNK_HEAD, ts="not-a-stamp")
        got = rowstate.derive(row, world())
        self.assertNotEqual(got.state, rowstate.LANDED)
        self.assertEqual(got.state, rowstate.REVIEWING)


class TreeIdentityTest(unittest.TestCase):
    """BLOCKER 7 (gate:9b5029474fb63d13): 'A two-commit lane
    squash-landed to an equal trunk tree with subject `dispatch <id>` returned
    BUILDING because individual lane patch IDs were absent.' The squash proof
    is the TREE: the folded commit points at the lane's final state byte for
    byte, positioned after the fork."""

    SQUASH_TREE = "5eaf" + "0" * 36

    def _squash_world(self, **over):
        spec = {
            "branches": {"lane/squash": LANE_HEAD},
            "branch_commits": {"lane/squash": ("aa" + "0" * 38,
                                               "bb" + "0" * 38)},
            "commit_patch_ids": {"aa" + "0" * 38: "pid1",
                                 "bb" + "0" * 38: "pid2"},
            "trunk_patch_ids": {},          # the squash left NO per-commit ids
            "commit_trees": {TRUNK_HEAD: self.SQUASH_TREE,
                             BASE_SPLIT: "tree" + "1" * 36,
                             LANE_HEAD: self.SQUASH_TREE},
            "trunk_trees": {self.SQUASH_TREE: TRUNK_HEAD,
                            "tree" + "1" * 36: BASE_SPLIT},
            "merge_bases": {"lane/squash": BASE_SPLIT},
        }
        spec.update(over)
        return world(**spec)

    def test_a_squash_land_with_an_equal_tree_lands(self):
        got = rowstate.derive(build_row("5a" * 16, "squash"),
                              self._squash_world())
        self.assertEqual(got.state, rowstate.LANDED)
        self.assertNotEqual(got.state, rowstate.BUILDING)
        self.assertEqual(got.because("tree-identity"),
                         (TRUNK_HEAD, self.SQUASH_TREE))

    def test_a_merge_only_branch_with_an_equal_tree_lands(self):
        """'Empty/trivial merge commits have the related no-patch-id shape' —
        a branch of merge commits has no patch-ids to match, but its tip tree
        reproduced on trunk is the same squash proof."""
        w = self._squash_world(
            branch_commits={"lane/squash": ("beef" + "0" * 36,)},
            commit_patch_ids={})
        got = rowstate.derive(build_row("5b" * 16, "squash"), w)
        self.assertEqual(got.state, rowstate.LANDED)
        self.assertEqual(got.because("tree-identity")[0], TRUNK_HEAD)

    def test_a_net_zero_branch_matching_its_own_base_does_not_land(self):
        """MUST-MISS, the strict `<` clause: a branch whose commits cancel out
        points at its BASE's tree; the only trunk match is the merge-base
        itself, which is not a landing."""
        base_tree = "tree" + "1" * 36
        w = self._squash_world(
            commit_trees={TRUNK_HEAD: self.SQUASH_TREE,
                          BASE_SPLIT: base_tree,
                          LANE_HEAD: base_tree},       # net zero: tip == base
            trunk_trees={self.SQUASH_TREE: TRUNK_HEAD,
                         base_tree: BASE_SPLIT})
        got = rowstate.derive(build_row("5c" * 16, "squash"), w)
        self.assertNotEqual(got.state, rowstate.LANDED)
        self.assertEqual(got.state, rowstate.BUILDING)

    def test_a_review_row_never_tree_lands(self):
        """MUST-MISS, the kind clause: a review row's subject can BE a trunk
        commit, whose tree trivially matches itself."""
        row = review_row("5d" * 16, "squash", status="open",
                         reviewed_tip=TRUNK_HEAD)
        # Give the reviewed tip's tree a trunk match and the lane commits —
        # everything the build arm would need — and require the refusal to
        # come from the row KIND alone. (TRUNK_HEAD is also an ancestor, so
        # strip it from trunk_shas to isolate the tree rung.)
        w = self._squash_world(trunk_shas=frozenset((BASE_SPLIT,)))
        got = rowstate.derive(row, w)
        self.assertIsNone(got.because("tree-identity"))
        self.assertNotEqual(got.state, rowstate.LANDED)


class SupersessionTest(unittest.TestCase):
    """A superseded parent stays `open`, so the ledger bills a builder for a
    row nobody owes anything on. The walk reads the CARRIER topology
    (blocker 3) with a visited set and NO numeric cap (blocker 13), and judges
    every hop with the full landed predicate (blocker 4)."""

    def test_a_chain_reaching_trunk_lands_the_parent(self):
        """Row 40f04430bbab -> successor e6f7e87081ae, which trunk names."""
        parent = build_row("40f04430bbabfd494e0dbd28ab58e20c", "pre-join",
                           superseded_by="e6f7e87081ae141e5c78fda4e6fe8daa")
        successor = build_row("e6f7e87081ae141e5c78fda4e6fe8daa", "pre-join-2")
        w = world(rows={successor["id"]: successor},
                  carriers={parent["id"]: successor["id"]},
                  trunk_tokens={"e6f7e87081ae": FOLD_PRE_JOIN})
        got = rowstate.derive(parent, w)
        self.assertEqual(got.state, rowstate.LANDED)
        hops, evidence = got.because("supersession-chain")
        self.assertEqual(hops, ("e6f7e87081ae141e5c78fda4e6fe8daa",))
        self.assertEqual(evidence.detail, ("e6f7e87081ae", FOLD_PRE_JOIN))

    def test_a_successor_not_bound_to_trunk_is_superseded_not_landed(self):
        """MUST-MISS. Rows b73f16ca721a / 7e2219140e90 / 669393cb4d8a were each
        superseded by a review round whose reviewed tip was ALREADY on trunk —
        the base. The obligation moved; the work did not land here."""
        parent = build_row("b73f16ca721a771f1d005fc6cb2c4521", "split-landreq",
                           superseded_by="898634a59d959ac7d492f1d45f45933b")
        successor = review_row("898634a59d959ac7d492f1d45f45933b",
                               "split-landreq", reviewed_tip=BASE_SPLIT,
                               polarity="concur",
                               ts="2026-08-05T18:30:00Z")
        w = world(rows={successor["id"]: successor},
                  carriers={parent["id"]: successor["id"]})
        # CONTROL: the successor's reviewed tip really is on trunk, so the
        # refusal is about DATING it, not about failing to look.
        self.assertIn(BASE_SPLIT, w.trunk_shas)
        got = rowstate.derive(parent, w)
        self.assertEqual(got.state, rowstate.SUPERSEDED)
        self.assertEqual(got.because("superseded-by"),
                         ("898634a59d959ac7d492f1d45f45933b",))

    # ---------------------------------------------- T2: the hop's own state
    #
    # The walk used to ask `_landed` of each hop and nothing else, so a
    # successor that had been CANCELLED, closed `withdrawn`, or given a FIX
    # verdict still landed its parent the moment its tip touched trunk. Git
    # cannot see a verdict, so a git-only hop test cannot help but do this.
    #
    # `discharging_successor` below is the POSITIVE fixture all four negative
    # arms are cut from: change exactly one field of the successor's
    # lifecycle and the parent must stop landing. That shared shape is what
    # makes the negatives non-vacuous — each one differs from a proven
    # positive by the single fact under test.

    @staticmethod
    def discharging_successor(**over):
        """A parent whose successor genuinely landed: an APPROVED review whose
        landing is proven by ID BINDING — a fold naming the row on trunk —
        which is non-forgeable and survives the removal of committer dating."""
        parent = build_row("4" * 32, "record-finality", superseded_by="5" * 32)
        successor = review_row("5" * 32, "record-finality-2",
                               reviewed_tip=TRUNK_HEAD, polarity="approve")
        successor.update(over)
        return parent, world(rows={successor["id"]: successor},
                             carriers={parent["id"]: successor["id"]},
                             trunk_tokens={"5" * 8: FOLD_PRE_JOIN})

    def test_a_successor_that_landed_does_discharge_the_parent(self):
        """THE CONTROL for every T2 arm. Formerly this proved the parent
        landed because the successor's tip was COMMITTED after the dispatch —
        a committer date, which a rebase rewrites at will. Upgraded to the
        witness type the reshape accepts: an approve verdict plus a fold on
        trunk naming the successor by id."""
        parent, w = self.discharging_successor()
        got = rowstate.derive(parent, w)
        self.assertTrue(got.evidence)
        self.assertEqual(got.state, "LANDED")

    def test_a_fix_verdict_on_the_successor_does_not_discharge_the_parent(self):
        """T2's motivating specimen. The successor's own reviewer demanded
        further work; the parent reading LANDED says that work was
        delivered."""
        parent, w = self.discharging_successor(polarity="fix")
        got = rowstate.derive(parent, w)
        self.assertEqual(got.state, "SUPERSEDED")
        self.assertNotEqual(got.state, rowstate.LANDED)

    def test_a_cancelled_successor_does_not_discharge_the_parent(self):
        parent, w = self.discharging_successor(status="cancelled",
                                               polarity=None)
        got = rowstate.derive(parent, w)
        self.assertEqual(got.state, "SUPERSEDED")
        self.assertNotEqual(got.state, rowstate.LANDED)

    def test_a_withdrawn_successor_does_not_discharge_the_parent(self):
        parent, w = self.discharging_successor(status="closed", polarity=None,
                                               close_reason="withdrawn")
        got = rowstate.derive(parent, w)
        self.assertEqual(got.state, "SUPERSEDED")
        self.assertNotEqual(got.state, rowstate.LANDED)

    def test_a_successor_still_owed_a_verdict_does_not_discharge_the_parent(self):
        """The successor's tip is on trunk and a fold names it, but nobody has
        reviewed it yet. That landing is its AUTHOR's; the review it exists to
        record has not happened, so the parent's obligation has moved, not
        ended."""
        parent, w = self.discharging_successor(status="open", polarity=None)
        got = rowstate.derive(parent, w)
        self.assertEqual(got.state, "SUPERSEDED")
        self.assertNotEqual(got.state, rowstate.LANDED)

    def test_a_successor_the_snapshot_cannot_read_is_still_judged_on_artifacts(self):
        """THE OVER-INVERSION GUARD. A successor missing from the snapshot has
        no lifecycle to consult — and the rungs above must not read that
        absence as a negative verdict. Its id binding on trunk still lands it,
        exactly as before T2."""
        parent = build_row("4" * 32, "record-finality", superseded_by="5" * 32)
        w = world(rows={},                       # the successor is NOT here
                  carriers={parent["id"]: "5" * 32},
                  trunk_tokens={"5" * 8: FOLD_PRE_JOIN})
        got = rowstate.derive(parent, w)
        self.assertEqual(got.state, "LANDED")

    def test_a_supersession_cycle_terminates(self):
        a = build_row("6" * 32, "loop-a", superseded_by="7" * 32)
        b = build_row("7" * 32, "loop-b", superseded_by="6" * 32)
        w = world(rows={a["id"]: a, b["id"]: b},
                  carriers={a["id"]: b["id"], b["id"]: a["id"]})
        got = rowstate.derive(a, w)
        self.assertTrue(got.evidence)
        self.assertEqual(got.state, rowstate.SUPERSEDED)

    def test_a_stale_pointer_to_a_corpse_does_not_hide_the_landed_sibling(self):
        """BLOCKER 3: `superseded_by` names the FIRST successor forever; when
        it is cancelled and a SIBLING carried the work to trunk, the frozen
        pointer read SUPERSEDED while the authoritative carrier proves LANDED.
        The carrier map here is built by rowworld._carriers THROUGH
        dispatches.carrier — the real topology read, not test plumbing."""
        pid = "ab" * 16
        parent = build_row(pid, "stale-ptr", superseded_by="c0" * 16)
        corpse = review_row("c0" * 16, "stale-ptr", status="cancelled",
                            supersedes=pid, chain_root=pid)
        sibling = build_row("d0" * 16, "stale-ptr-2",
                            supersedes=pid, chain_root=pid)
        rows = {parent["id"]: parent, corpse["id"]: corpse,
                sibling["id"]: sibling}
        carriers = rowworld._carriers(rows)
        # CONTROL: the topology read really resolved the SIBLING, not the
        # frozen pointer's corpse.
        self.assertEqual(carriers.get(pid), sibling["id"])
        w = world(rows=rows, carriers=carriers,
                  trunk_tokens={"d0" * 4: FOLD_PRE_JOIN})
        got = rowstate.derive(parent, w)
        self.assertEqual(got.state, rowstate.LANDED)
        hops, evidence = got.because("supersession-chain")
        self.assertEqual(hops, (sibling["id"],))

    def test_L3_a_structurally_withdrawn_sibling_is_a_pass_through(self):
        """L3. `moved_nothing` learned that a withdrawal is recorded
        as a FLAG and stopped there — but a STRUCTURED close writes
        `close_reason="withdrawn"` with its own proof fields and NO flag, and
        that spelling still read as CARRYING.

        With siblings the consequence is worse than one hidden parent: the
        walk's answer depended on which successor it reached FIRST, so the
        same population resolved SUPERSEDED or LANDED by insertion order.
        Both orderings are asserted here, because a single ordering can pass
        against the bug half the time."""
        pid = "f1" * 16
        dead = review_row("f2" * 16, "sib", status="closed",
                          close_reason="withdrawn", supersedes=pid,
                          chain_root=pid)
        live = build_row("f3" * 16, "sib-2", supersedes=pid, chain_root=pid)
        # UNCONDITIONAL, outside the loop: every assertion below lives inside
        # it, so a loop that never ran would pass the whole arm. This pins
        # that the two siblings are distinct rows in the first place.
        self.assertNotEqual(dead["id"], live["id"])
        self.assertGreater(len(live["id"]), 30)
        seen = []
        for order in (("dead", "live"), ("live", "dead")):
            parent = build_row(pid, "sib", superseded_by="f2" * 16)
            rows = {parent["id"]: parent}
            for which in order:
                row = dead if which == "dead" else live
                rows[row["id"]] = row
            carriers = rowworld._carriers(rows)
            self.assertEqual(carriers.get(pid), live["id"],
                             "the carrier walk picked by insertion order "
                             "(%s first)" % order[0])
            w = world(rows=rows, carriers=carriers,
                      trunk_tokens={"f3" * 4: FOLD_PRE_JOIN})
            got = rowstate.derive(parent, w)
            self.assertEqual(got.state, "LANDED", order[0])
            seen.append(got.state)
        self.assertGreater(len(seen), 1)      # both orderings really ran

    def test_a_pointer_to_a_corpse_with_no_carrier_stays_visible(self):
        """The complementary law from dispatches.carrier: every unknown
        resolves toward VISIBLE. A dead first successor with no sibling means
        NOBODY carries the row, and it must show its own state — not hide
        behind SUPERSEDED."""
        pid = "ba" * 16
        parent = build_row(pid, "orphaned", superseded_by="c1" * 16)
        corpse = review_row("c1" * 16, "orphaned", status="cancelled",
                            supersedes=pid, chain_root=pid)
        rows = {parent["id"]: parent, corpse["id"]: corpse}
        carriers = rowworld._carriers(rows)
        self.assertNotIn(pid, carriers)
        w = world(rows=rows, carriers=carriers,
                  branches={"lane/orphaned": LANE_HEAD},
                  branch_commits={"lane/orphaned": (LANE_HEAD,)})
        got = rowstate.derive(parent, w)
        self.assertEqual(got.state, rowstate.BUILDING)
        self.assertNotEqual(got.state, rowstate.SUPERSEDED)

    def test_a_successor_token_does_not_outrank_its_own_partial_stack(self):
        """BLOCKER 4: a successor with commits A+B, only A on trunk, and a
        trunk token naming the successor must NOT land the parent — the
        decisive partial-stack veto applies to every hop."""
        parent = build_row("ad" * 16, "partial-succ",
                           superseded_by="ae" * 16)
        successor = build_row("ae" * 16, "partial-succ-2")
        w = world(rows={successor["id"]: successor},
                  carriers={parent["id"]: successor["id"]},
                  branches={"lane/partial-succ-2": LANE_HEAD},
                  branch_commits={"lane/partial-succ-2": ("aa" + "0" * 38,
                                                          "bb" + "0" * 38)},
                  commit_patch_ids={"aa" + "0" * 38: "pid1",
                                    "bb" + "0" * 38: "pid2"},
                  trunk_patch_ids={"pid1": TRUNK_HEAD},
                  trunk_tokens={"ae" * 4: FOLD_PRE_JOIN})
        # CONTROL on the same observable: with the stack COMPLETE on trunk,
        # the same worlds' token/content DO land the parent.
        complete = world(rows={successor["id"]: successor},
                         carriers={parent["id"]: successor["id"]},
                         branches={"lane/partial-succ-2": LANE_HEAD},
                         branch_commits={"lane/partial-succ-2": ("aa" + "0" * 38,
                                                                 "bb" + "0" * 38)},
                         commit_patch_ids={"aa" + "0" * 38: "pid1",
                                           "bb" + "0" * 38: "pid2"},
                         trunk_patch_ids={"pid1": TRUNK_HEAD,
                                          "pid2": TRUNK_HEAD},
                         trunk_tokens={"ae" * 4: FOLD_PRE_JOIN})
        control = rowstate.derive(parent, complete)
        self.assertEqual(control.state, rowstate.LANDED)
        got = rowstate.derive(parent, w)
        self.assertEqual(got.state, rowstate.SUPERSEDED)
        self.assertNotEqual(got.state, rowstate.LANDED)

    def test_a_fourteen_hop_chain_is_not_called_superseded_by_a_cap(self):
        """BLOCKER 13: the old 12-hop cap turned truncation into a positive
        SUPERSEDED. dispatches.py removed numeric caps after a legitimate
        66-link chain; the visited set is what terminates."""
        ids = ["%02d" % n * 16 for n in range(15)]
        rows, carriers = {}, {}
        for n in range(1, 15):
            nxt = ids[n + 1] if n + 1 < 15 else None
            rows[ids[n]] = build_row(ids[n], "hop-%d" % n, superseded_by=nxt)
            carriers[ids[n - 1]] = ids[n]
        parent = build_row(ids[0], "hop-0", superseded_by=ids[1])
        w = world(rows=rows, carriers=carriers,
                  trunk_tokens={ids[14][:8]: FOLD_PRE_JOIN})
        got = rowstate.derive(parent, w)
        self.assertEqual(got.state, rowstate.LANDED)
        hops, _evidence = got.because("supersession-chain")
        self.assertEqual(len(hops), 14)


class LifecycleTest(unittest.TestCase):
    """BLOCKER 2 (gate:9b5029474fb63d13): 'Cancelled, FIX, SUPERSEDE,
    closure reason, and contrary inclusion do not receive lifecycle
    precedence' — measured live, 128 terminal FIX/SUPERSEDE rows and 96
    cancelled rows derived LANDED, open reviews became BUILDING and completed
    reviews became GATED. The ledger's own recorded terminals outrank any
    progress inferred from git."""

    def _landable(self, **over):
        """A build row whose lane branch content-identity PROVES landing —
        the strongest artifact signal there is, so each lifecycle state below
        is shown BEATING it, not merely winning a quiet default."""
        row = build_row("ca" * 16, "cured", **over)
        w = world(branches={"lane/cured": LANE_HEAD},
                  branch_commits={"lane/cured": ("cafe" + "0" * 36,)},
                  commit_patch_ids={"cafe" + "0" * 36: "pid1"},
                  trunk_patch_ids={"pid1": TRUNK_HEAD})
        return row, w

    def test_a_cancelled_row_never_reads_landed(self):
        row, w = self._landable(status="cancelled", cancel_reason="stood down")
        # CONTROL on the same observable: without the cancel, this exact row
        # and world DO read LANDED — the artifact signal is alive.
        control_row, control_w = self._landable()
        control = rowstate.derive(control_row, control_w)
        self.assertEqual(control.state, rowstate.LANDED)
        got = rowstate.derive(row, w)
        self.assertEqual(got.state, rowstate.CANCELLED)
        self.assertNotEqual(got.state, rowstate.LANDED)
        self.assertEqual(got.because("status"), ("cancelled", "stood down"))

    def test_a_fix_verdict_reads_reviewed_never_landed(self):
        """The 128-row class: a change-DEMANDING verdict whose reviewed tip
        sits on trunk is somebody else's landing."""
        for polarity in ("fix", "supersede"):
            row = review_row("fe" * 16, "cured", polarity=polarity,
                             reviewed_tip=TRUNK_HEAD)
            got = rowstate.derive(row, world())
            self.assertEqual(got.state, rowstate.REVIEWED, polarity)
            self.assertNotEqual(got.state, rowstate.LANDED, polarity)
            self.assertEqual(got.because("verdict"),
                             (polarity, TRUNK_HEAD))
        # CONTROL: the same row with an APPROVE verdict does read LANDED, so
        # the refusals above are about POLARITY and not about a fixture that
        # could never land anything. Its proof is a fold on trunk naming the
        # row by id — this control used to lean on the tip being COMMITTED
        # after the dispatch, and T10 removed the authority of committer
        # dates, which would have left every arm above passing vacuously.
        approve = review_row("fe" * 16, "cured", polarity="approve",
                             reviewed_tip=TRUNK_HEAD)
        control = rowstate.derive(approve, world(
            trunk_tokens={"fe" * 4: FOLD_PRE_JOIN}))
        self.assertEqual(control.state, "LANDED")

    def test_a_concur_or_undeclared_verdict_reads_reviewed(self):
        for polarity in ("concur", None):
            row = review_row("dd" * 16, "cured", polarity=polarity)
            got = rowstate.derive(row, world())
            self.assertEqual(got.state, rowstate.REVIEWED, polarity)
        self.assertEqual(rowstate.derive(
            review_row("dd" * 16, "cured", polarity=None),
            world()).because("verdict"), ("UNDECLARED", APPROVED_TIP))

    def test_an_open_review_reads_reviewing_not_building(self):
        row = review_row("ee" * 16, "cured", status="open")
        got = rowstate.derive(row, world())
        self.assertEqual(got.state, rowstate.REVIEWING)
        self.assertNotEqual(got.state, rowstate.BUILDING)
        self.assertEqual(got.because("open-review"), "open")

    def test_a_completed_review_reads_reviewed_not_gated(self):
        """The 297-row class: the receipt on the reviewed tip describes the
        BUILD's progress, not the review's."""
        row = review_row("ab" * 16, "cured", polarity="fix")
        w = world(receipts_by_head={APPROVED_TIP: (receipt(APPROVED_TIP,
                                                           APPROVED_TREE),)},
                  branches={"lane/cured": APPROVED_TIP},
                  branch_commits={"lane/cured": (APPROVED_TIP,)})
        # CONTROL, unconditional: the SAME receipt gates the BUILD row on this
        # lane, so the refusal below is about row kind, not a dead receipt.
        control = rowstate.derive(build_row("ab" * 16, "cured"), w)
        self.assertEqual(control.state, rowstate.GATED)
        got = rowstate.derive(row, w)
        self.assertEqual(got.state, rowstate.REVIEWED)
        self.assertNotEqual(got.state, rowstate.GATED)

    def test_an_unlanded_approve_reads_reviewed_with_the_reason(self):
        row = review_row("ac" * 16, "cured", polarity="approve")
        got = rowstate.derive(row, world())     # APPROVED_TIP is NOT on trunk
        self.assertEqual(got.state, rowstate.REVIEWED)
        self.assertEqual(got.because("verdict"), ("approve", APPROVED_TIP))

    def test_a_landed_closure_reads_landed_from_the_ledger_alone(self):
        row = build_row("ad" * 16, "gone-branch", status="closed",
                        close_reason="landed",
                        landing_trunk_sha=FOLD_FLEET_NOTES)
        got = rowstate.derive(row, world())     # no branch, no tokens at all
        self.assertEqual(got.state, rowstate.LANDED)
        self.assertEqual(got.because("closure"),
                         ("landed", FOLD_FLEET_NOTES))

    def test_a_delivered_report_closure_reads_built(self):
        row = build_row("ae" * 16, "report", status="closed",
                        close_reason="delivered-report",
                        close_evidence="report ref")
        got = rowstate.derive(row, world())
        self.assertEqual(got.state, rowstate.BUILT)
        self.assertEqual(got.because("closure"),
                         ("delivered-report", "report ref"))

    def test_a_discharged_closure_reads_superseded_not_reviewed(self):
        """[170], and the FIXTURE was the reason it survived: this arm
        used a REVIEW row, a shape `dispatches._fold` never admits for this
        reason — it takes `discharged` only for an OPEN BUILD with NO verdict,
        proven by a SUCCESSOR's landed gate-verified APPROVE
        (`landreq._close_ladder_discharged`). Testing an unreachable shape
        masked the contract error: REVIEWED claims a verdict was recorded,
        and for this closure a verdict cannot exist.

        SUPERSEDED is the honest word — the obligation ended because somebody
        ELSE's row carried it. LANDED is the tempting one and it is wrong:
        the landing proven belongs to the successor."""
        row = build_row("af" * 16, "cured", status="closed", polarity=None,
                        close_reason="discharged", close_evidence="proof")
        got = rowstate.derive(row, world())
        self.assertEqual(got.state, "SUPERSEDED")
        self.assertNotEqual(got.state, rowstate.REVIEWED)
        self.assertEqual(got.because("closure"), ("discharged", "proof"))

    def test_an_abandoned_flag_ends_the_row_even_at_status_verdict(self):
        """L2. TERMINALITY IS NOT ALWAYS A STATUS: `dispatches`
        records an abandonment as `status="verdict"` plus an `abandoned=True`
        FLAG — its own comment notes that two of the three status spellings it
        once used were UNREACHABLE for exactly this reason. `_lifecycle` read
        statuses and close reasons only, so an abandoned row fell through to
        the artifact arms and derived a stage for an obligation nobody holds.
        """
        spec = {"branches": {"lane/ab": LANE_HEAD},
                "branch_commits": {"lane/ab": (LANE_HEAD,)}}
        # CONTROL: without the flag the same row derives a BUILD STAGE — an
        # obligation somebody is billed for. That is what the flag has to be
        # able to end.
        control = rowstate.derive(build_row("c1" * 16, "ab", status="verdict"),
                                  world(**spec))
        self.assertEqual(control.state, "REVIEWED")
        got = rowstate.derive(build_row("c1" * 16, "ab", status="verdict",
                                        abandoned=True), world(**spec))
        self.assertEqual(got.state, "CANCELLED")
        self.assertNotEqual(got.state, rowstate.REVIEWED)
        self.assertEqual(got.because("flag"), ("abandoned", "verdict"))

    def test_a_withdrawn_FLAG_is_not_a_withdrawn_CLOSE(self):
        """THE DISTINCTION L2 COULD HAVE ERASED. `withdrawn=True` ANNOTATES a
        verdict without rewriting it, so the row stays at its verdict's word;
        `close_reason="withdrawn"` is a structured close with its own proof
        fields and ends the row. Two spellings, two meanings — collapsing
        them would CANCEL every row whose reviewer merely retracted an
        objection."""
        flagged = review_row("c2" * 16, "cured", polarity="fix",
                             withdrawn=True, reviewed_tip=LANE_HEAD)
        self.assertEqual(rowstate.derive(flagged, world()).state, "REVIEWED")
        closed = review_row("c2" * 16, "cured", status="closed",
                            close_reason="withdrawn", reviewed_tip=LANE_HEAD)
        self.assertEqual(rowstate.derive(closed, world()).state, "CANCELLED")

    def test_a_withdrawn_fix_still_reads_reviewed(self):
        """Contrary inclusion: withdraw/discharge annotate a verdict without
        rewriting it, and the row stays at its verdict's word."""
        row = review_row("ba" * 16, "cured", polarity="fix", withdrawn=True,
                         reviewed_tip=TRUNK_HEAD)
        # CONTROL, unconditional: the same row under an approve verdict DOES
        # read LANDED, so the refusal below is about the fix polarity. Proven
        # by an id binding rather than by the tip's committer date, which T10
        # stopped treating as proof — see the sibling control above.
        control = rowstate.derive(review_row("ba" * 16, "cured",
                                             polarity="approve",
                                             reviewed_tip=TRUNK_HEAD),
                                  world(trunk_tokens={"ba" * 4: FOLD_PRE_JOIN}))
        self.assertEqual(control.state, "LANDED")
        got = rowstate.derive(row, world())
        self.assertEqual(got.state, "REVIEWED")
        self.assertNotEqual(got.state, rowstate.LANDED)

    # ------------------------------------------------ T1: the ledger terminal
    #
    # Every arm below runs on ONE fixture — a build row whose lane branch IS
    # trunk head — because that is the only fixture where the artifact arms
    # have something to say. The previous unrecognized-closure arm used a row
    # with no branch at all, so UNKNOWN was the answer down every path in the
    # module and the arm proved nothing about precedence. A rung that outranks
    # inference can only be tested against inference that would otherwise
    # WIN.

    @staticmethod
    def closed_on_trunk(rid, **over):
        """A build row whose work is unambiguously on trunk, plus a World that
        says so. Bare, it derives LANDED — see the control arm below."""
        return build_row(rid, "onmain", **over), world(
            branches={"lane/onmain": TRUNK_HEAD},
            branch_commits={"lane/onmain": (TRUNK_HEAD,)})

    def test_the_ledger_terminal_fixture_really_does_derive_landed(self):
        """THE CONTROL every T1 arm leans on. Without it each of them could
        pass because the fixture is inert rather than because the rung
        outranks it."""
        row, w = self.closed_on_trunk("b0" * 16)
        got = rowstate.derive(row, w)
        self.assertEqual(got.state, "LANDED")

    def test_a_superseded_closure_outranks_the_work_being_on_trunk(self):
        """T1, the motivating specimen: a row the ledger closed `superseded`
        derived LANDED, because `_lifecycle` had no word for the reason and
        the artifact arms then answered a question the ledger had already
        closed. SUPERSEDED is the conservative reading — the work moved to a
        successor, and whether THAT landed is the successor's question."""
        row, w = self.closed_on_trunk("b1" * 16, status="closed",
                                      close_reason="superseded",
                                      close_evidence="4b" * 16)
        got = rowstate.derive(row, w)
        self.assertEqual(got.state, "SUPERSEDED")
        self.assertNotEqual(got.state, rowstate.LANDED)
        self.assertEqual(got.because("closure"), ("superseded", "4b" * 16))

    def test_a_withdrawn_closure_over_a_trunk_landing_is_CONTRARY(self):
        """REWRITTEN by the L1 refinement. This asserted CANCELLED, and
        the refinement is that a close saying NOTHING IS COMING, met by work
        on trunk, is a contradiction rather than a tidy ending — see
        MandatoryGateTest. The plain-CANCELLED case is the arm below, where
        there is nothing on trunk to contradict it."""
        row, w = self.closed_on_trunk("b2" * 16, status="closed",
                                      close_reason="withdrawn")
        got = rowstate.derive(row, w)
        self.assertEqual(got.state, "UNKNOWN")
        self.assertNotEqual(got.state, rowstate.LANDED)
        self.assertEqual(got.because("contrary")[0], "close:withdrawn")

    def test_a_withdrawn_closure_with_nothing_on_trunk_reads_cancelled(self):
        got = rowstate.derive(build_row("b6" * 16, "gone", status="closed",
                                        close_reason="withdrawn"), world())
        self.assertEqual(got.state, "CANCELLED")

    def test_a_subsumed_closure_reads_superseded_not_landed(self):
        row, w = self.closed_on_trunk("b3" * 16, status="closed",
                                      close_reason="subsumed")
        got = rowstate.derive(row, w)
        self.assertEqual(got.state, "SUPERSEDED")

    def test_a_resolved_closure_reads_superseded_not_landed(self):
        """[170] moved this word too. `resolved` is the confirmation
        shape one step over from `discharged`: a confirming ROUND retires a
        contrary, so the obligation ended in somebody else's row. REVIEWED
        claimed a verdict on THIS row that its closure never recorded."""
        row, w = self.closed_on_trunk("b4" * 16, status="closed",
                                      close_reason="resolved")
        got = rowstate.derive(row, w)
        self.assertEqual(got.state, "SUPERSEDED")
        self.assertNotEqual(got.state, rowstate.LANDED)

    def test_a_stranded_closure_over_a_trunk_landing_is_CONTRARY(self):
        """Same refinement, second non-delivery reason — and the pair is why
        the non-delivery set is DERIVED from `_CLOSE_TERMINAL` rather than
        hand-listed: both reasons behave alike because both mean the same
        thing, not because somebody remembered to add the second one."""
        row, w = self.closed_on_trunk("b5" * 16, status="closed",
                                      close_reason="stranded")
        got = rowstate.derive(row, w)
        self.assertEqual(got.state, "UNKNOWN")
        self.assertEqual(got.because("contrary")[0], "close:stranded")

    def test_a_stranded_closure_with_nothing_on_trunk_reads_cancelled(self):
        got = rowstate.derive(build_row("b7" * 16, "gone", status="closed",
                                        close_reason="stranded"), world())
        self.assertEqual(got.state, "CANCELLED")

    def test_an_unrecognized_closure_stays_unknown_never_a_guess(self):
        """Store law: a skipped case must yield UNKNOWN, never a confident
        terminal. DE-VACUIFIED at the reshape — this used a branchless row,
        so UNKNOWN was the answer whether or not the rung existed."""
        row, w = self.closed_on_trunk("bb" * 16, status="closed",
                                      close_reason="some-future-reason")
        got = rowstate.derive(row, w)
        self.assertEqual(got.state, "UNKNOWN")
        self.assertNotEqual(got.state, rowstate.LANDED)
        self.assertIn("cannot name", got.unknown)

    def test_a_closed_row_with_no_reason_at_all_is_also_unreadable(self):
        """The other spelling of the same fact: `closed` with nothing to read
        is a recorded ending we cannot name, not an open row."""
        row, w = self.closed_on_trunk("bc" * 16, status="closed")
        got = rowstate.derive(row, w)
        self.assertEqual(got.state, "UNKNOWN")

    def test_the_state_vocabulary_is_the_exact_word_it_claims(self):
        """WHY THE ARMS ABOVE ASSERT LITERALS. `assertEqual(got.state,
        rowstate.SUPERSEDED)` compares two names: if the constant were ever
        `None` or `""` AND the derivation returned the same, the arm passes
        having proven nothing — the vacuous-assertion rung flags exactly that
        shape, and it is right to. Pinning the literal word instead makes each
        arm self-sufficient, and this arm keeps the constants honest so the
        two spellings cannot drift apart. The words are also the CONTRACT —
        they are what the owner reads on his board — so a rename SHOULD break
        a test rather than silently re-label his rows."""
        self.assertEqual(rowstate.UNSTARTED, "UNSTARTED")
        self.assertEqual(rowstate.BUILDING, "BUILDING")
        self.assertEqual(rowstate.BUILT, "BUILT")
        self.assertEqual(rowstate.GATED, "GATED")
        self.assertEqual(rowstate.APPROVED, "APPROVED")
        self.assertEqual(rowstate.REVIEWING, "REVIEWING")
        self.assertEqual(rowstate.REVIEWED, "REVIEWED")
        self.assertEqual(rowstate.LANDED, "LANDED")
        self.assertEqual(rowstate.SUPERSEDED, "SUPERSEDED")
        self.assertEqual(rowstate.CANCELLED, "CANCELLED")
        self.assertEqual(rowstate.UNKNOWN, "UNKNOWN")
        # and the tuple is the whole vocabulary, not a subset of it
        self.assertEqual(len(rowstate.STATES), 11)
        self.assertEqual(len(set(rowstate.STATES)), len(rowstate.STATES))

    def test_every_recorded_close_reason_has_a_terminal(self):
        """THE BINDING between this module's meanings and the ledger's
        grammar. `_CLOSE_TERMINAL` is written out rather than imported —
        `dispatches` holds field lists, not states, so there is nothing to
        import — which means the only thing keeping the two in step is this
        arm. A ninth reason added to the fold turns the suite red here,
        instead of silently reaching the artifact arms in production."""
        from helm import dispatches
        grammar = set(dispatches._CLOSE_STATE_FIELDS)
        self.assertGreaterEqual(len(grammar), 8)        # it is not empty
        self.assertIn("landed", grammar)                # MUST-HIT
        self.assertEqual(
            grammar - set(rowstate._CLOSE_TERMINAL), set(),
            "a close reason the fold will WRITE has no terminal here, so a "
            "row closed for it would fall through to artifact inference")
        self.assertEqual(
            set(rowstate._CLOSE_TERMINAL) - grammar, set(),
            "a terminal here names a close reason the fold cannot write")
        for reason, state in rowstate._CLOSE_TERMINAL.items():
            self.assertIn(state, rowstate.STATES, reason)


def gated_build_row(rid):
    """A BUILD row whose lane head is APPROVED_TIP — the shape that can reach
    GATED. A review row cannot: its recorded verdict ends it at REVIEWED and
    an open one is REVIEWING (blocker 2), so the receipt-judging tests run on
    the row kind whose progress a receipt actually describes."""
    return build_row(rid, "split")


def gated_world(**over):
    spec = {"branches": {"lane/split": APPROVED_TIP},
            "branch_commits": {"lane/split": (APPROVED_TIP,)}}
    spec.update(over)
    return world(**spec)


class GateTest(unittest.TestCase):
    def test_a_receipt_binding_the_commits_exact_tree_gates_the_tip(self):
        w = gated_world(receipts_by_head={APPROVED_TIP: (receipt(APPROVED_TIP,
                                                                 APPROVED_TREE),)})
        got = rowstate.derive(gated_build_row("8" * 32), w)
        self.assertEqual(got.state, rowstate.GATED)
        self.assertEqual(got.because("gate-receipt"), (GATE_ID, APPROVED_TREE))

    def test_a_receipt_binding_a_tree_no_commit_has_is_refused(self):
        """MUST-MISS. A gate run snapshots the WORKING tree, so untracked files
        produce a tree object no commit points at. Trusting head alone lets a
        run over uncommitted work vouch for the commit."""
        phantom = "9999" + "0" * 36
        w = gated_world(receipts_by_head={APPROVED_TIP: (receipt(APPROVED_TIP,
                                                                 phantom),)})
        got = rowstate.derive(gated_build_row("a" * 8 + "0" * 24), w)
        self.assertNotEqual(got.state, rowstate.GATED)
        self.assertEqual(got.state, rowstate.BUILDING)
        self.assertIn("refusing rather than assuming", got.unknown)

    def test_a_dirty_or_moving_or_failed_receipt_does_not_gate(self):
        for label, over in (("dirty", {"dirty": True}),
                            ("moved", {"head_after": TRUNK_HEAD}),
                            ("failed", {"status": "FAILED"})):
            w = gated_world(receipts_by_head={
                APPROVED_TIP: (receipt(APPROVED_TIP, APPROVED_TREE, **over),)})
            got = rowstate.derive(gated_build_row("b" * 32), w)
            self.assertNotEqual(got.state, rowstate.GATED, label)
        # CONTROL: the same receipt without the defect DOES gate.
        w = gated_world(receipts_by_head={APPROVED_TIP: (receipt(APPROVED_TIP,
                                                                 APPROVED_TREE),)})
        control = rowstate.derive(gated_build_row("b" * 32), w)
        self.assertTrue(control.evidence)
        self.assertEqual(control.state, rowstate.GATED)

    def test_a_clean_receipt_is_found_behind_a_defective_one(self):
        w = gated_world(receipts_by_head={APPROVED_TIP: (
            receipt(APPROVED_TIP, APPROVED_TREE, id="bad0", dirty=True),
            receipt(APPROVED_TIP, APPROVED_TREE, id="good"))})
        got = rowstate.derive(gated_build_row("c" * 32), w)
        self.assertEqual(got.state, rowstate.GATED)
        self.assertEqual(got.because("gate-receipt")[0], "good")


class ApprovalTest(unittest.TestCase):
    """Cross-family, exact-tip, gate-bound — and `concur` authorizes nothing."""

    def _world(self, polarity="approve", tip=APPROVED_TIP, seat="helm-claude-2",
               gate=GATE_ID, families=None, receipts=None):
        approval = review_row("aprv" + "0" * 28, "split", recipient=seat,
                              polarity=polarity, reviewed_tip=tip, gate=gate)
        return world(
            receipts_by_head={APPROVED_TIP: receipts or (
                receipt(APPROVED_TIP, APPROVED_TREE),)},
            branches={"lane/split": APPROVED_TIP},
            branch_commits={"lane/split": (APPROVED_TIP,)},
            approvals={"split": (approval,)},
            authors={"split": frozenset(("codex",))},
            families=families or {"codex": frozenset(("codex",)),
                                  "helm-claude-2": frozenset(("claude",)),
                                  "ds4pro": frozenset(("ds4pro",))})

    def test_a_cross_family_approve_on_the_exact_tip_approves(self):
        got = rowstate.derive(build_row("d" * 32, "split"), self._world())
        self.assertEqual(got.state, rowstate.APPROVED)
        rid, seat, families, gate = got.because("approve")
        self.assertEqual(seat, "helm-claude-2")
        self.assertEqual(families, ["claude"])
        self.assertEqual(gate, GATE_ID)

    def test_concur_authorizes_nothing(self):
        """MUST-MISS. `concur` is absent from WORK_POLARITIES by construction so
        that endorsement has a word promising no landing."""
        got = rowstate.derive(build_row("e" * 32, "split"),
                              self._world(polarity="concur"))
        self.assertTrue(got.evidence)
        self.assertNotEqual(got.state, rowstate.APPROVED)
        self.assertEqual(got.state, rowstate.GATED)

    def test_fix_and_supersede_do_not_approve(self):
        # CONTROL first, unconditionally: the same lane with `approve` DOES
        # reach APPROVED, so the refusals below are about polarity.
        control = rowstate.derive(build_row("f" * 32, "split"), self._world())
        self.assertTrue(control.evidence)
        self.assertEqual(control.state, rowstate.APPROVED)
        for polarity in ("fix", "supersede"):
            got = rowstate.derive(build_row("f" * 32, "split"),
                                  self._world(polarity=polarity))
            self.assertNotEqual(got.state, rowstate.APPROVED, polarity)

    def test_a_same_family_approve_does_not_approve(self):
        """MUST-MISS. The whole point of cross-family review."""
        got = rowstate.derive(
            build_row("0" * 32, "split"),
            self._world(seat="codex-2",
                        families={"codex": frozenset(("codex",)),
                                  "codex-2": frozenset(("codex",))}))
        self.assertTrue(got.evidence)
        self.assertNotEqual(got.state, rowstate.APPROVED)
        self.assertEqual(got.state, rowstate.GATED)

    def test_family_never_comes_from_the_seat_label(self):
        """`codex-2` and `codex` share a family; `ds4pro` does not. The names
        look equally different, so only the resolved family can decide."""
        cross = rowstate.derive(build_row("1" * 32, "split"),
                                self._world(seat="ds4pro"))
        self.assertTrue(cross.evidence)
        self.assertEqual(cross.state, rowstate.APPROVED)
        same = rowstate.derive(build_row("1" * 32, "split"),
                               self._world(seat="codex-2",
                                           families={"codex": frozenset(("codex",)),
                                                     "codex-2": frozenset(("codex",))}))
        self.assertNotEqual(same.state, rowstate.APPROVED)

    def test_an_approve_on_a_different_tip_does_not_approve_this_head(self):
        """MUST-MISS. Work continued past the approval."""
        got = rowstate.derive(build_row("2" * 32, "split"),
                              self._world(tip=BASE_SPLIT))
        self.assertTrue(got.evidence)
        self.assertNotEqual(got.state, rowstate.APPROVED)

    def test_an_unresolvable_family_refuses_rather_than_guesses(self):
        got = rowstate.derive(
            build_row("3" * 32, "split"),
            self._world(families={"codex": frozenset(("codex",)),
                                  "helm-claude-2": None}))
        self.assertNotEqual(got.state, rowstate.APPROVED)
        self.assertIn("no verified family evidence", got.unknown)

    def test_an_ungated_approve_does_not_approve(self):
        got = rowstate.derive(build_row("4" * 32, "split"),
                              self._world(gate=""))
        self.assertNotEqual(got.state, rowstate.APPROVED)
        self.assertIn("no gate token", got.unknown)

    # ------------------------------- T14: order must not decide the answer
    #
    # The exact probe: the pair `(missing-gate ds4pro, valid
    # helm-claude-2)` derived GATED and the SAME PAIR REVERSED derived
    # APPROVED. One row, two answers, decided by nothing but the order the
    # approvals happened to sit in — every refusal RETURNED, so the first
    # unusable approve ended the search before the good one was reached.

    def _pair(self, order):
        """Two approvals on the same tip: one unusable, one valid."""
        bad = review_row("bad0" + "0" * 28, "split", recipient="ds4pro",
                         polarity="approve", reviewed_tip=APPROVED_TIP,
                         gate="")                       # no gate token
        good = review_row("good" + "0" * 28, "split", recipient="helm-claude-2",
                          polarity="approve", reviewed_tip=APPROVED_TIP,
                          gate=GATE_ID)
        pair = (bad, good) if order == "bad-first" else (good, bad)
        return world(
            receipts_by_head={APPROVED_TIP: (receipt(APPROVED_TIP,
                                                     APPROVED_TREE),)},
            branches={"lane/split": APPROVED_TIP},
            branch_commits={"lane/split": (APPROVED_TIP,)},
            approvals={"split": pair},
            authors={"split": frozenset(("codex",))},
            families={"codex": frozenset(("codex",)),
                      "helm-claude-2": frozenset(("claude",)),
                      "ds4pro": frozenset(("ds4pro",))})

    def test_a_valid_approve_is_found_whichever_order_it_sits_in(self):
        good_first = rowstate.derive(build_row("e1" * 16, "split"),
                                     self._pair("good-first"))
        bad_first = rowstate.derive(build_row("e1" * 16, "split"),
                                    self._pair("bad-first"))
        self.assertEqual(good_first.state, "APPROVED")
        self.assertEqual(bad_first.state, "APPROVED")
        self.assertEqual(good_first.state, bad_first.state)
        # ...and it is the SAME approve that carries it, not merely the same
        # word: an order-dependent search could reach APPROVED twice by
        # crediting different evidence.
        self.assertEqual(good_first.because("approve"),
                         bad_first.because("approve"))

    def test_the_credited_approve_is_canonical_not_the_first_found(self):
        """Supplement 2. T14 made the STATE order-independent and left
        the EVIDENCE order-dependent: two valid cross-family approvals with
        distinct valid receipts both reach APPROVED while crediting DIFFERENT
        rows, so the same population explains itself differently depending on
        tuple order — and a reader checking the derivation gets a different
        answer than the one who checked it yesterday."""
        second = receipt(APPROVED_TIP, APPROVED_TREE, id=self.SECOND_GATE)
        one = review_row("aa01" + "0" * 28, "split", recipient="helm-claude-2",
                         polarity="approve", reviewed_tip=APPROVED_TIP,
                         gate=GATE_ID)
        two = review_row("bb02" + "0" * 28, "split", recipient="ds4pro",
                         polarity="approve", reviewed_tip=APPROVED_TIP,
                         gate=self.SECOND_GATE)

        def derived(pair):
            w = self._world(receipts=(receipt(APPROVED_TIP, APPROVED_TREE),
                                      second))
            return rowstate.derive(build_row("e3" * 16, "split"),
                                   rowworld.World(**dict(
                                       {f: getattr(w, f)
                                        for f in rowworld.World.__slots__},
                                       approvals={"split": pair})))

        forward, reverse = derived((one, two)), derived((two, one))
        self.assertEqual(forward.state, "APPROVED")     # control: both land
        self.assertEqual(reverse.state, "APPROVED")
        self.assertEqual(forward.because("approve"), reverse.because("approve"),
                         "the credited approve depends on tuple order")

    def test_an_exhausted_candidate_set_still_reports_why(self):
        """NOT LOOSENED. Collecting refusals instead of returning on the first
        must not lose the reason — a GATED row with no explanation is the
        false negative wearing a different coat."""
        only_bad = world(
            receipts_by_head={APPROVED_TIP: (receipt(APPROVED_TIP,
                                                     APPROVED_TREE),)},
            branches={"lane/split": APPROVED_TIP},
            branch_commits={"lane/split": (APPROVED_TIP,)},
            approvals={"split": (review_row("bad0" + "0" * 28, "split",
                                            recipient="ds4pro",
                                            polarity="approve",
                                            reviewed_tip=APPROVED_TIP,
                                            gate=""),)},
            authors={"split": frozenset(("codex",))},
            families={"codex": frozenset(("codex",)),
                      "ds4pro": frozenset(("ds4pro",))})
        got = rowstate.derive(build_row("e2" * 16, "split"), only_bad)
        self.assertEqual(got.state, "GATED")
        self.assertIn("no gate token", got.unknown)

    # ------------------------------- T6: the receipt the approval CITES
    #
    # One tip legitimately carries several receipts — a re-run after a flake,
    # a second host, a re-gate on the same tree — and `_gated` returns
    # whichever binding one it reaches first. The approval check used to
    # compare the reviewer's cited gate against THAT pick, so an approve
    # against a perfectly good second receipt was refused and the row read
    # GATED with an approve sitting on it. A false negative produced entirely
    # by iteration order, and visible to the owner as an unreviewed row.

    SECOND_GATE = "7c1de3a90b64f2e5"

    def _two_receipts(self, gate, second=None):
        """A tip carrying TWO receipts, with the approval citing `gate`."""
        return self._world(gate=gate, receipts=(
            receipt(APPROVED_TIP, APPROVED_TREE),
            second or receipt(APPROVED_TIP, APPROVED_TREE,
                              id=self.SECOND_GATE)))

    def test_an_approve_citing_the_other_clean_receipt_still_approves(self):
        """T6's motivating specimen."""
        # CONTROL, same fixture: citing the receipt `_gated` picks DOES reach
        # APPROVED, so the arm below measures the citation and nothing else.
        control = rowstate.derive(build_row("5" * 32, "split"),
                                  self._two_receipts(GATE_ID))
        self.assertEqual(control.state, "APPROVED")
        got = rowstate.derive(build_row("5" * 32, "split"),
                              self._two_receipts(self.SECOND_GATE))
        self.assertEqual(got.state, "APPROVED")
        self.assertNotEqual(got.state, rowstate.GATED)
        self.assertEqual(got.because("approve")[3], self.SECOND_GATE)

    def test_an_approve_citing_a_gate_this_tip_never_carried_does_not_approve(self):
        """NOT LOOSENED. The cited receipt must EXIST for this tip."""
        got = rowstate.derive(build_row("6" * 32, "split"),
                              self._two_receipts("dead0000beef1111"))
        self.assertEqual(got.state, "GATED")
        self.assertNotEqual(got.state, rowstate.APPROVED)
        self.assertIn("not among the receipts recorded for this tip",
                      got.unknown)

    def test_an_approve_citing_a_failed_receipt_does_not_approve(self):
        """NOT LOOSENED. The cited receipt faces `_judge_receipt` exactly as
        every other receipt does — and the refusal now says WHICH receipt
        failed and why, instead of reporting a mismatch against a pick."""
        failed = receipt(APPROVED_TIP, APPROVED_TREE, id=self.SECOND_GATE,
                         status="FAILED")
        got = rowstate.derive(build_row("7" * 32, "split"),
                              self._two_receipts(self.SECOND_GATE,
                                                 second=failed))
        self.assertEqual(got.state, "GATED")
        self.assertNotEqual(got.state, rowstate.APPROVED)
        self.assertIn("does not bind this tip", got.unknown)
        self.assertIn("FAILED", got.unknown)

    def test_an_approve_citing_a_receipt_bound_to_another_tree_does_not_approve(self):
        """NOT LOOSENED, and this is the one that matters most: a receipt
        recorded under this HEAD whose TREE is somebody else's. The tree
        comparison is what stops a run over uncommitted work vouching for a
        commit that never contained it, and citing a second receipt must not
        route around it."""
        wrong_tree = receipt(APPROVED_TIP, "tree" + "7" * 36,
                             id=self.SECOND_GATE)
        wrong_tree["tree_after"] = "tree" + "7" * 36
        got = rowstate.derive(build_row("8" * 32, "split"),
                              self._two_receipts(self.SECOND_GATE,
                                                 second=wrong_tree))
        self.assertEqual(got.state, "GATED")
        self.assertNotEqual(got.state, rowstate.APPROVED)
        self.assertIn("refusing rather than assuming", got.unknown)


class FoldGrammarTest(unittest.TestCase):
    """BLOCKER 12 (gate:9b5029474fb63d13): '_trunk_tokens reads %B
    across arbitrary commits rather than requiring a fold subject. Quoted
    prose `dispatch deadbeef` can land a reaped row.' Measured on this
    estate: 19 of 198 body tokens were review-round citations."""

    def _tokens(self, tmp, *messages):
        """Commit each message in turn and harvest the whole log.

        SEVERAL MESSAGES IN ONE REPO IS THE POINT, not a convenience: an arm
        that only asserts a token is ABSENT cannot tell a working grammar
        check from a scan that read nothing at all. Committing a genuine
        `fold:` beside the prose puts a MUST-HIT in the same dict as the
        must-miss, on the same observable, from the same call."""
        repo, _wt, git = _scratch_repo(tmp)
        for n, message in enumerate(messages):
            name = "b%d.txt" % n
            with open(os.path.join(repo, name), "w") as fh:
                fh.write("x\n")
            git("add", name)
            git("commit", "-q", "-m", message)
        return rowworld._trunk_tokens(repo, "main", 50)

    def test_prose_in_an_ordinary_body_does_not_bind(self):
        with tempfile.TemporaryDirectory() as tmp:
            tokens = self._tokens(
                tmp, "test(seats): re-run the coverage claim\n\n"
                     "@codex-2's FIX on dispatch deadbeef told me so")
            self.assertNotIn("deadbeef", tokens)
            self.assertEqual(tokens, {})

    def test_a_fold_subject_binds_from_subject_and_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            tokens = self._tokens(
                tmp, "fold: my-lane at 123abc (chain deadbeef)\n\n"
                     "second row folded alongside (dispatch beefdead)")
            self.assertEqual(set(tokens), {"deadbeef", "beefdead"})

    def test_a_land_subject_binds_its_per_lane_body_listing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tokens = self._tokens(
                tmp, "land: roster halves\n\n"
                     "helm-claude f45b3c3 ds4pro APPROVE (dispatch cafebabe)")
            self.assertIn("cafebabe", tokens)

    def test_the_merge_for_land_subject_binds(self):
        """The one measured nonstandard genuine binding: `merge … for land
        (dispatch …)` carries its token in the SUBJECT.

        RENAMED at the reshape (task/744, T5). This was
        `test_a_subject_token_binds_on_any_commit`, and the name claimed the
        broad rule while the body only ever exercised this one grammar — so
        the arm went on passing when the rule under it narrowed, and a reader
        would have taken its name for the contract."""
        with tempfile.TemporaryDirectory() as tmp:
            tokens = self._tokens(
                tmp, "merge trunk 123abc into lane for land "
                     "(dispatch feedf00d)")
            self.assertIn("feedf00d", tokens)

    def test_prose_in_an_ordinary_SUBJECT_does_not_bind(self):
        """T5. Blocker 12 fixed the body and left the subject binding on ANY
        commit, so a commit that merely NAMES a row in its first line landed
        it — `docs: explain dispatch <rowid>` being the specimen. Re-measured
        over the full trunk: every subject that carries a token is `fold:`,
        `land:`, or the one merge-for-land, so requiring the grammar costs no
        real binding."""
        with tempfile.TemporaryDirectory() as tmp:
            # ONE REPO, BOTH COMMITS. The fold subject is the MUST-HIT that
            # proves this scan read subjects at all; without it, a scan that
            # returned nothing would produce the same reassuring absence.
            tokens = self._tokens(
                tmp,
                "fold: my-lane at 123abc (dispatch feedf00d)",
                "docs: explain dispatch deadbeef in the guide")
            self.assertIn("feedf00d", tokens)
            self.assertNotIn("deadbeef", tokens)

    def test_a_merge_that_only_mentions_landing_does_not_bind(self):
        """The merge form is matched on `merge` AND `for land` together, so
        widening the grammar did not open a prose door beside the one it
        closed."""
        with tempfile.TemporaryDirectory() as tmp:
            tokens = self._tokens(
                tmp,
                "merge trunk 123abc into lane for land (dispatch feedf00d)",
                "chore: notes for landing dispatch deadbeef later")
            self.assertIn("feedf00d", tokens)      # MUST-HIT: merge form works
            self.assertNotIn("deadbeef", tokens)


class PatchIdPinningTest(unittest.TestCase):
    """Defect 15: the prefix pin left rename detection
    unpinned — a rename-plus-edit commit hashed to DIFFERENT ids under
    diff.renames=true versus false, so the 'same on every box' claim was
    unproven. --no-renames makes every box hash the same bytes."""

    def test_a_rename_plus_edit_hashes_identically_across_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, _wt, git = _scratch_repo(tmp)
            with open(os.path.join(repo, "old.txt"), "w") as fh:
                fh.write("line one\nline two\nline three\nline four\n")
            git("add", "old.txt")
            git("commit", "-q", "-m", "seed")
            git("mv", "old.txt", "new.txt")
            with open(os.path.join(repo, "new.txt"), "a") as fh:
                fh.write("edited\n")
            git("add", "new.txt")
            git("commit", "-q", "-m", "rename plus edit")
            head = git("rev-parse", "HEAD").lower()
            ids = {}
            for value in ("true", "false"):
                git("config", "diff.renames", value)
                index, err = rowworld._patch_ids(repo, ("-n", "5", "main"))
                self.assertIsNone(err)
                self.assertIn(head, index)
                ids[value] = index[head]
            self.assertEqual(ids["true"], ids["false"])
    """BLOCKER 11 (codex-2, gate:9b5029474fb63d13): 'A malformed sole receipt
    or unreadable ledger publishes BUILDING/UNSTARTED with success instead of
    UNKNOWN/refusal.' 'No receipt binds this tip' is a claim about the WHOLE
    ledger; a ledger with holes cannot back it."""

    _SPEC = {"branches": {"lane/blind": LANE_HEAD},
             "branch_commits": {"lane/blind": ("cafe" + "0" * 36,)},
             "commit_patch_ids": {"cafe" + "0" * 36: "pid1"}}

    def test_an_unreadable_ledger_turns_building_into_unknown(self):
        control = rowstate.derive(build_row("9a" * 16, "blind"),
                                  world(**self._SPEC))
        self.assertEqual(control.state, rowstate.BUILDING)
        got = rowstate.derive(build_row("9a" * 16, "blind"),
                              world(receipts_unavailable="disk gone",
                                    **self._SPEC))
        self.assertEqual(got.state, rowstate.UNKNOWN)
        self.assertIn("disk gone", got.unknown)

    def test_a_skipped_receipt_turns_unstarted_into_unknown(self):
        spec = {"branches": {"lane/blind": LANE_HEAD},
                "branch_commits": {"lane/blind": ()}}
        control = rowstate.derive(build_row("9b" * 16, "blind"), world(**spec))
        self.assertEqual(control.state, rowstate.UNSTARTED)
        got = rowstate.derive(build_row("9b" * 16, "blind"),
                              world(receipts_skipped=3, **spec))
        self.assertEqual(got.state, rowstate.UNKNOWN)
        self.assertIn("3 receipt rows could not be judged", got.unknown)

    def test_a_receipt_that_did_bind_stays_positive_beside_skipped_rows(self):
        """Positive evidence is unaffected: only the states that lean on
        receipt ABSENCE become unknowable."""
        got = rowstate.derive(
            gated_build_row("9c" * 16),
            gated_world(receipts_skipped=2,
                        receipts_by_head={APPROVED_TIP: (receipt(
                            APPROVED_TIP, APPROVED_TREE),)}))
        self.assertEqual(got.state, rowstate.GATED)

    def test_cmd_derive_refuses_an_unreadable_receipt_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, _wt, _git_fn = _scratch_repo(tmp)
            real_receipts = rowworld.gate.receipts
            real_snapshot = rowworld.dispatches.snapshot_with_verdicts
            rowworld.gate.receipts = \
                lambda: ([], "receipts.jsonl: unreadable", 0)
            rowworld.dispatches.snapshot_with_verdicts = \
                lambda: ({}, {}, None)
            try:
                rc = rowworld.cmd_derive(["--repo", repo, "--trunk", "main"])
            finally:
                rowworld.gate.receipts = real_receipts
                rowworld.dispatches.snapshot_with_verdicts = real_snapshot
            self.assertEqual(rc, 2)
            # CONTROL: the same estate with a READABLE (empty) ledger prints
            # its rows and exits 0.
            rowworld.gate.receipts = lambda: ([], None, 0)
            rowworld.dispatches.snapshot_with_verdicts = \
                lambda: ({}, {}, None)
            try:
                rc = rowworld.cmd_derive(["--repo", repo, "--trunk", "main"])
            finally:
                rowworld.gate.receipts = real_receipts
                rowworld.dispatches.snapshot_with_verdicts = real_snapshot
            self.assertEqual(rc, 0)


class VerbatimPatchIdTest(unittest.TestCase):
    """T15 (task/744). `git patch-id --stable` deliberately
    NORMALISES WHITESPACE so a patch keeps its id across reformatting — a
    virtue when tracking a patch through rebases, and a hole when the id is
    being spent as proof that trunk carries this exact work.

    Measured on a real scratch repo rather than asserted: two commits that
    differ only by a trailing space. The arm is the whole finding, because
    the claim is about what GIT does, not about what this module does with
    it."""

    ONE = "base\nADDED \n"          # trailing space
    TWO = "base\nADDED\n"           # without

    def _ids(self, tmp, mode):
        repo, _wt, git = _scratch_repo(tmp)
        root = subprocess.run(("git", "rev-parse", "HEAD"), cwd=repo,
                              capture_output=True, text=True).stdout.strip()
        out = []
        for content in (self.ONE, self.TWO):
            git("checkout", "-q", root)
            with open(os.path.join(repo, "a.txt"), "w") as fh:
                fh.write(content)
            git("add", "a.txt")
            git("commit", "-q", "-m", "variant")
            index, err = rowworld._patch_ids(repo, ("-n", "1", "HEAD"),
                                             mode=mode)
            # THE POSITIVE IS THE ERROR CHECK. A failed scan returns {}, so
            # "exactly one id came back" says everything `assertIsNone(err)`
            # said and is an assertion about DATA rather than about the
            # absence of a complaint.
            self.assertEqual(len(index), 1, err)
            out.append(next(iter(index.values())))
        return out

    def test_stable_equates_whitespace_and_verbatim_does_not(self):  # noqa: VACUOUS_ASSERTION — the --stable EQUALITY is this arm's control: it can only hold if both ids were really computed, so an inert probe fails it rather than passing everything
        """THE MEASUREMENT, both halves in one arm. The `--stable` equality is
        the CONTROL that makes the `--verbatim` inequality readable: without
        it, a probe that simply failed to hash anything would produce the
        same reassuring difference."""
        with tempfile.TemporaryDirectory() as tmp:
            loose = self._ids(tmp, "--stable")
        with tempfile.TemporaryDirectory() as tmp:
            exact = self._ids(tmp, "--verbatim")
        # POSITIVE FIRST, on both observables: these are real 40-hex ids and
        # not two empty strings, which would satisfy the equality below and
        # be defeated by the inequality for the wrong reason.
        # UNCONDITIONAL positives on both observables, outside any loop: a
        # control that only runs when the collection is non-empty proves
        # nothing about the empty case it was meant to rule out.
        self.assertGreater(len(loose), 1)
        self.assertGreater(len(exact), 1)
        for ids in (loose, exact):
            for pid in ids:
                self.assertRegex(pid, r"\A[0-9a-f]{40}\Z")
        self.assertEqual(loose[0], loose[1],
                         "the premise of this finding is that --stable "
                         "equates these; if git changed, re-measure")
        self.assertNotEqual(exact[0], exact[1])

    def test_the_world_is_built_on_the_byte_exact_hash(self):
        """The default is what `rowworld.build` actually spends, so the arm
        pins the DEFAULT rather than a mode a caller has to remember."""
        import inspect
        sig = inspect.signature(rowworld._patch_ids)
        self.assertEqual(sig.parameters["mode"].default, "--verbatim")
        with tempfile.TemporaryDirectory() as tmp:
            defaulted = self._ids(tmp, "--verbatim")
        with tempfile.TemporaryDirectory() as tmp:
            repo, _wt, git = _scratch_repo(tmp)
            with open(os.path.join(repo, "a.txt"), "w") as fh:
                fh.write(self.ONE)
            git("add", "a.txt")
            git("commit", "-q", "-m", "variant")
            index, err = rowworld._patch_ids(repo, ("-n", "1", "HEAD"))
            self.assertEqual(len(index), 1, err)
        self.assertEqual(len(defaulted[0]), 40)
        self.assertEqual(next(iter(index.values())), defaulted[0])


class UnscopedRowTest(unittest.TestCase):
    """Addendum 4 (task/744). A row with no `repo_id` enters
    EVERY repository's snapshot, and once inside it was judged against THAT
    repository's artifacts — the probe: repo A carries `fold: dispatch
    <legacy-id>`, a notionally foreign legacy row lands in A's snapshot, and
    A's own id-binding derives LANDED for work that may have nothing to do
    with A.

    Both choices are claims. Excluding the row invents "this row is not
    ours"; including it silently invented "this row IS ours" — and only the
    second one mints confidence. It stays visible and unplaced."""

    def test_a_repo_less_row_is_not_landed_by_this_repos_fold_token(self):
        rid = "e5" * 16
        w = world(trunk_tokens={"e5" * 4: FOLD_PRE_JOIN})
        # CONTROL, unconditional: the SAME row and the SAME token, once the
        # row is placed in this repository, DOES derive LANDED. So the
        # refusal below is about the missing ownership and nothing else.
        placed = rowstate.derive(build_row(rid, "legacy"), w)
        self.assertEqual(placed.state, "LANDED")
        self.assertEqual(placed.because("id-binding")[1], FOLD_PRE_JOIN)
        got = rowstate.derive(build_row(rid, "legacy"),
                              world(trunk_tokens={"e5" * 4: FOLD_PRE_JOIN},
                                    unscoped=frozenset((rid,))))
        self.assertEqual(got.state, "UNKNOWN")
        self.assertNotEqual(got.state, rowstate.LANDED)
        self.assertIn("names no repository", got.unknown)

    def test_the_unscoped_set_is_exactly_the_rows_with_no_repo_id(self):
        """The reader, not just the refusal — a set built wrongly would make
        the rung above either vacuous or catastrophic."""
        rows = {"a" * 32: {"id": "a" * 32, "repo_id": "/repo/.git"},
                "b" * 32: {"id": "b" * 32},
                "c" * 32: {"id": "c" * 32, "repo_id": ""},
                "d" * 32: "not a row"}
        got = rowworld._unscoped(rows)
        # UNCONDITIONAL POSITIVE FIRST: an empty result satisfies "a is not
        # in it" and would make the rung above vacuous rather than wrong.
        self.assertGreater(len(got), 1)
        self.assertEqual(got, frozenset(("b" * 32, "c" * 32)))
        self.assertNotIn("a" * 32, got)        # placed rows stay placed


class TextconvPinTest(unittest.TestCase):
    """Addendum 5 (task/744). A repository can declare
    `diff=<driver>` in `.gitattributes` and point that driver at a textconv,
    and `git log -p` then hashes the DRIVER'S OUTPUT instead of the file's
    bytes — so a patch-id becomes a property of the checkout's config rather
    than of the work. `--verbatim` cannot save a stream that was already
    rewritten before hashing."""

    def _collision(self, tmp):
        """The fixture, and it is strictly stronger than the one it
        replaces. BASE -> LANE on one side, BASE -> TRUNK on the other, with
        a textconv that renders both LANE and TRUNK as SAME: two NONEMPTY
        diffs whose text is identical, so two byte-distinct commits collide
        on one patch-id. My own repro produced an EMPTY patch instead — the
        same live axis, but a weaker outcome that could be mistaken for the
        scan simply failing. -> (repo, lane sha, trunk sha)"""
        repo, _wt, git = _scratch_repo(tmp)
        with open(os.path.join(repo, ".gitattributes"), "w") as fh:
            fh.write("a.txt diff=mask\n")
        git("config", "diff.mask.textconv",
            "sed -e s/^LANE$/SAME/ -e s/^TRUNK$/SAME/")
        with open(os.path.join(repo, "a.txt"), "w") as fh:
            fh.write("BASE\n")
        git("add", "-A")
        git("commit", "-q", "-m", "base")
        base = git("rev-parse", "HEAD")
        git("checkout", "-q", "-b", "lane/work")
        with open(os.path.join(repo, "a.txt"), "w") as fh:
            fh.write("LANE\n")
        git("add", "-A")
        git("commit", "-q", "-m", "lane")
        lane = git("rev-parse", "HEAD")
        git("checkout", "-q", "main")
        with open(os.path.join(repo, "a.txt"), "w") as fh:
            fh.write("TRUNK\n")
        git("add", "-A")
        git("commit", "-q", "-m", "trunk")
        trunk = git("rev-parse", "HEAD")
        self.assertNotEqual(base, lane)
        return repo, lane, trunk

    def _unpinned_id(self, repo, sha):
        """The spelling this module used before the pin — textconv LIVE."""
        rc, patches = rowworld._git_bytes(
            repo, "log", "-p", "-1", "--no-merges", "--no-renames",
            "--src-prefix=a/", "--dst-prefix=b/", "--format=commit %H", sha)
        self.assertEqual(rc, 0)
        rc2, listing = rowworld._git(repo, "patch-id", "--verbatim",
                                     stdin=patches)
        self.assertEqual(rc2, 0)
        return listing.split()[0] if listing.split() else ""

    def _pinned_id(self, repo, sha):
        index, err = rowworld._patch_ids(repo, ("-n", "1", sha))
        self.assertEqual(len(index), 1, err)
        return next(iter(index.values()))

    def test_two_byte_distinct_commits_COLLIDE_without_the_pin(self):
        """THE MUST-COLLIDE ARM. Without it the pin has no witness: a fix
        that stops a collision nobody demonstrated is a fix for nothing."""
        with tempfile.TemporaryDirectory() as tmp:
            repo, lane, trunk = self._collision(tmp)
            lane_blob = rowworld._git(repo, "rev-parse", lane + ":a.txt")[1]
            trunk_blob = rowworld._git(repo, "rev-parse", trunk + ":a.txt")[1]
            self.assertGreater(len(lane_blob.strip()), 6)
            self.assertNotEqual(lane_blob, trunk_blob)   # BYTE-DISTINCT
            unpinned_lane = self._unpinned_id(repo, lane)
            self.assertGreater(len(unpinned_lane), 6)
            self.assertEqual(unpinned_lane, self._unpinned_id(repo, trunk),
                             "the premise of this finding is that a textconv "
                             "collapses these to one id; if git changed, "
                             "re-measure rather than deleting the arm")

    def test_the_pin_keeps_them_apart(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, lane, trunk = self._collision(tmp)
            pinned_lane = self._pinned_id(repo, lane)
            self.assertGreater(len(pinned_lane), 6)
            self.assertNotEqual(pinned_lane, self._pinned_id(repo, trunk))

    def test_the_pin_is_present_in_the_argv(self):
        """A pin nobody can see is a pin nobody keeps. Asserted on the source
        as well as through behaviour, so removing a flag fails here even in a
        git version where the behaviour happens not to differ."""
        import inspect
        source = inspect.getsource(rowworld._patch_ids)
        self.assertGreater(len(source), 500)
        for flag in ("--no-textconv", "--no-ext-diff", "--binary",
                     "--no-renames", "--src-prefix=a/"):
            self.assertIn(flag, source)


class AmbientGitEnvTest(unittest.TestCase):
    """T13 (task/744). `git -C <dir>` does NOT beat an ambient
    `GIT_DIR`: the environment selects the REPOSITORY and `-C` only sets the
    working directory. So an explicit `--repo A` derived repository B's rows
    and reported them as A's — every artifact under the answer belonging to
    the wrong repository.

    `dispatches` has scrubbed this since the write side of the mapping was
    found to be the identity; `rowworld` is the read side, and it was left
    exposed."""

    def test_an_ambient_git_dir_does_not_redirect_an_explicit_repo(self):
        with tempfile.TemporaryDirectory() as tmp_a, \
                tempfile.TemporaryDirectory() as tmp_b:
            repo_a, _wt, _git_a = _scratch_repo(tmp_a)
            repo_b, _wt2, _git_b = _scratch_repo(tmp_b)
            # CONTROL, unconditional: with a clean environment, A resolves to
            # A and B to B — so the arm below measures the redirect and not a
            # broken fixture.
            ident_a, err_a = rowworld.repo_identity(repo_a)
            ident_b, err_b = rowworld.repo_identity(repo_b)
            # POSITIVE FIRST, on the observable the arm judges. Two Nones
            # also compare unequal to nothing and satisfy every assertion
            # below; naming what each identity IS is what makes the
            # inequalities mean something.
            self.assertIn(".git", ident_a or "")
            self.assertIn(".git", ident_b or "")
            self.assertIsNone(err_a)
            self.assertIsNone(err_b)
            self.assertNotEqual(ident_a, ident_b)
            real = os.environ.get("GIT_DIR")
            os.environ["GIT_DIR"] = os.path.join(repo_b, ".git")
            try:
                hijacked, err = rowworld.repo_identity(repo_a)
            finally:
                if real is None:
                    os.environ.pop("GIT_DIR", None)
                else:
                    os.environ["GIT_DIR"] = real
            self.assertIn(".git", hijacked or "")
            self.assertIsNone(err)
            self.assertEqual(hijacked, ident_a)
            self.assertNotEqual(hijacked, ident_b,
                                "an ambient GIT_DIR redirected an explicit "
                                "--repo read to another repository")

    def test_the_scrub_list_is_the_one_dispatches_writes_under(self):
        """IMPORTED, NOT RETYPED. Two copies of a security-relevant variable
        list drift, and the drift is invisible: the read side would keep
        honouring a variable the write side had learned to fear."""
        from helm import dispatches
        scrubbed = rowworld._scrubbed_env()
        self.assertIn("GIT_DIR", dispatches._GIT_SELECTION_ENV)   # MUST-HIT
        self.assertGreaterEqual(len(dispatches._GIT_SELECTION_ENV), 8)
        # AND IT IS A REMOVAL, NOT AN OMISSION. `vcs.run`'s env OVERLAYS the
        # ambient environment, so a dict that merely leaves GIT_DIR out
        # removes nothing — the first cut of this fix did exactly that and
        # measured green while changing nothing at all. `None` is the seam's
        # spelling for "remove this one", and asserting the VALUES is what
        # separates the two shapes; asserting only the keys cannot.
        self.assertEqual({k: v for k, v in scrubbed.items()
                          if k in dispatches._GIT_SELECTION_ENV},
                         {k: None for k in dispatches._GIT_SELECTION_ENV},
                         "a selection variable is no longer REMOVED")
        # THE OVERLAY ALSO PINS THE HISTORY VIEW — BOTH REWRITERS A READER CAN
        # SWITCH OFF — and the difference is asserted as a SET so the overlay
        # cannot grow a variable nothing here declares. Replacement objects off
        # is what makes an answer about a commit id an answer about the object
        # that id names; grafts off is the half `--no-replace-objects` does NOT
        # cover, and its spelling is IMPORTED from `landreq._object_view` rather
        # than retyped, which is the equality below.
        from helm import landreq
        self.assertEqual(set(scrubbed) - set(dispatches._GIT_SELECTION_ENV),
                         set(rowworld._history_view_env()))
        self.assertEqual(scrubbed["GIT_NO_REPLACE_OBJECTS"], "1")
        self.assertEqual(scrubbed["GIT_GRAFT_FILE"],
                         landreq._NO_GRAFTS["GIT_GRAFT_FILE"],
                         "the graft overlay is a second spelling of the one "
                         "`landreq._object_view` already measured")
        self.assertEqual(scrubbed["GIT_GRAFT_FILE"], os.devnull)


class JsonPurityTest(unittest.TestCase):
    """T7 (task/744): `helm derive --json` promises a document on stdout, and
    the skipped-receipt caveat printed onto that same stream BEFORE the JSON
    branch. `helm derive --json | jq` was fed a prose line ahead of the
    document and failed to parse — the warning broke exactly the consumers it
    existed to warn."""

    def _run(self, args, skipped=0):
        """cmd_derive against a scratch repo -> (rc, stdout, stderr)."""
        import io
        import contextlib
        with tempfile.TemporaryDirectory() as tmp:
            repo, _wt, _git_fn = _scratch_repo(tmp)
            real_receipts = rowworld.gate.receipts
            real_snapshot = rowworld.dispatches.snapshot_with_verdicts
            rowworld.gate.receipts = lambda: ([], None, skipped)
            rowworld.dispatches.snapshot_with_verdicts = lambda: ({}, {}, None)
            out, err = io.StringIO(), io.StringIO()
            try:
                with contextlib.redirect_stdout(out), \
                        contextlib.redirect_stderr(err):
                    rc = rowworld.cmd_derive(["--repo", repo, "--trunk", "main"]
                                             + list(args))
            finally:
                rowworld.gate.receipts = real_receipts
                rowworld.dispatches.snapshot_with_verdicts = real_snapshot
        return rc, out.getvalue(), err.getvalue()

    def test_stdout_is_parseable_json_even_when_receipts_were_skipped(self):
        import json
        # CONTROL, unconditional: with NOTHING skipped the document parses, so
        # the arm below measures the caveat and not the verb.
        rc, clean, _err = self._run(["--json"], skipped=0)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(clean)["trunk"], "main")
        rc, stdout, stderr = self._run(["--json"], skipped=3)
        self.assertEqual(rc, 0)
        parsed = json.loads(stdout)          # THE ASSERTION: it still parses
        self.assertEqual(parsed["trunk"], "main")
        # THE CAVEAT IS NOT LOST, only moved. Routing a warning off a channel
        # must never take it out of reach of the reader who is on it.
        self.assertEqual(parsed["receipts_skipped"], 3)
        self.assertIn("3 receipt rows could not be judged", stderr)
        self.assertNotIn("could not be judged", stdout)

    def test_the_human_report_still_carries_the_caveat(self):
        rc, stdout, stderr = self._run([], skipped=3)
        self.assertEqual(rc, 0)
        self.assertIn("rows read from the artifacts", stdout)
        self.assertIn("3 receipt rows could not be judged", stderr)


class FamilySeatsTest(unittest.TestCase):
    """BLOCKER 10 (gate:9b5029474fb63d13): '--families omits authors
    required by the approval predicate. An author of one family approved by
    another resolves only the reviewer family and remains GATED for missing
    author-family evidence.' Cross-family is a relation; the resolver needs
    BOTH sides."""

    def test_the_resolver_input_carries_authors_and_reviewers(self):
        gitdir = "/repo/.git"
        rows = {
            "a" * 32: build_row("a" * 32, "split", recipient="codex",
                                repo_id=gitdir),
            "b" * 32: review_row("b" * 32, "split", recipient="ds4pro",
                                 polarity="approve", repo_id=gitdir),
        }
        seats = rowworld._family_seats(rows, gitdir)
        self.assertIn("ds4pro", seats)     # the reviewer was always resolved
        self.assertIn("codex", seats)      # the AUTHOR is the cured half
        self.assertEqual(seats, {"codex", "ds4pro"})

    def test_non_approve_reviewers_and_foreign_repos_cost_no_canary(self):
        gitdir = "/repo/.git"
        rows = {
            "a" * 32: build_row("a" * 32, "split", recipient="codex",
                                repo_id=gitdir),
            "c" * 32: review_row("c" * 32, "split", recipient="kimi",
                                 polarity="fix", repo_id=gitdir),
            "d" * 32: build_row("d" * 32, "other", recipient="gemini",
                                repo_id="/elsewhere/.git"),
        }
        seats = rowworld._family_seats(rows, gitdir)
        self.assertEqual(seats, {"codex"})


class PurityTest(unittest.TestCase):
    def test_derive_is_deterministic_and_touches_nothing(self):
        row = build_row("5" * 32, "split")
        w = world(branches={"lane/split": LANE_HEAD},
                  branch_commits={"lane/split": (LANE_HEAD,)})
        first = rowstate.derive(row, w)
        second = rowstate.derive(row, w)
        self.assertTrue(first.evidence)
        self.assertEqual(first, second)
        self.assertEqual(first.state, rowstate.BUILDING)

    def test_the_pure_module_imports_no_reader(self):
        """The purity boundary is structural, not a promise in a docstring:
        every artifact read lives in `rowworld`."""
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "helm", "rowstate.py")
        with open(path) as handle:
            source = handle.read()
        for banned in ("import subprocess", "import os", "from . import gate",
                       "from . import dispatches", "open("):
            self.assertNotIn(banned, source, banned)
        # CONTROL: the probe can see imports that ARE there.
        self.assertIn("import re", source)

    def test_a_missing_world_is_unknown_not_a_claim(self):
        got = rowstate.derive(build_row("6" * 32, "split"), None)
        self.assertTrue(got.unknown)
        self.assertEqual(got.state, rowstate.UNKNOWN)

    def test_a_non_row_is_unknown(self):
        # CONTROL, unconditional: a REAL row through the same call is not
        # UNKNOWN, so the loop below is not passing on a dead derive().
        control = rowstate.derive(build_row("7" * 32, "split"),
                                  world(trunk_tokens={"77777777": TRUNK_HEAD}))
        self.assertTrue(control.evidence)
        self.assertEqual(control.state, rowstate.LANDED)
        for junk in (None, {}, {"id": "not-hex"}, "string"):
            got = rowstate.derive(junk, world())
            self.assertTrue(got.unknown)
            self.assertEqual(got.state, rowstate.UNKNOWN)


def _scratch_repo(tmp, *, worktree=False):
    """A tiny REAL repository (and optionally a linked worktree), because the
    defects these tests pin live in how git is asked, not in what a synthetic
    World says. -> (repo_path, worktree_path_or_None)."""
    repo = os.path.join(tmp, "repo")
    os.makedirs(repo)

    def git(*args, cwd=repo):
        done = subprocess.run(("git",) + args, cwd=cwd, capture_output=True,
                              text=True,
                              env=dict(os.environ,
                                       GIT_AUTHOR_NAME="t", GIT_COMMITTER_NAME="t",
                                       GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_EMAIL="t@t"))
        assert done.returncode == 0, (args, done.stderr)
        return done.stdout.strip()

    git("init", "-q", "-b", "main")
    with open(os.path.join(repo, "a.txt"), "w") as fh:
        fh.write("one\n")
    git("add", "a.txt")
    git("commit", "-q", "-m", "root")
    wt = None
    if worktree:
        wt = os.path.join(tmp, "wt")
        git("worktree", "add", "-q", "-b", "lane/wt", wt)
    return repo, wt, git


class RepoIdentityTest(unittest.TestCase):
    """BLOCKER 1 (gate:9b5029474fb63d13): 'cmd_derive resolves
    --absolute-git-dir, which in a worktree is .git/worktrees/<name>; dispatch
    rows store the common .git directory. Live measurement: 1,491 rows matched
    the common gitdir; zero matched the worktree gitdir.'"""

    def test_a_worktree_resolves_to_the_common_gitdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, wt, git = _scratch_repo(tmp, worktree=True)
            common = os.path.realpath(git("rev-parse", "--path-format=absolute",
                                          "--git-common-dir"))
            # CONTROL: from inside the worktree, the PER-WORKTREE gitdir is a
            # DIFFERENT path — the exact value the old code filtered on.
            per_worktree = git("rev-parse", "--absolute-git-dir", cwd=wt)
            self.assertNotEqual(os.path.realpath(per_worktree), common)
            got, err = rowworld.repo_identity(wt)
            self.assertIsNone(err)
            self.assertEqual(got, common)
            # And the main checkout answers the SAME identity.
            main_got, main_err = rowworld.repo_identity(repo)
            self.assertIsNone(main_err)
            self.assertEqual(main_got, common)

    def test_a_snapshot_from_a_worktree_keeps_canonical_rows(self):
        """The population question itself: rows stamped with the COMMON gitdir
        must survive a snapshot invoked with the WORKTREE's path."""
        with tempfile.TemporaryDirectory() as tmp:
            repo, wt, git = _scratch_repo(tmp, worktree=True)
            common = os.path.realpath(git("rev-parse", "--path-format=absolute",
                                          "--git-common-dir"))
            rows = {"a" * 32: {"id": "a" * 32, "kind": "build", "lane": "wt",
                               "repo_id": common, "status": "open",
                               "ts": "2026-08-05T12:00:00Z"},
                    "b" * 32: {"id": "b" * 32, "kind": "build", "lane": "other",
                               "repo_id": "/somewhere/else/.git",
                               "status": "open",
                               "ts": "2026-08-05T12:00:00Z"}}
            w = rowworld.snapshot(wt, trunk_ref="main", rows=rows)
            self.assertIn("a" * 32, w.rows)          # canonical row survives
            self.assertNotIn("b" * 32, w.rows)       # foreign repo filtered

    def test_an_unreadable_repo_refuses_with_a_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            got, err = rowworld.repo_identity(os.path.join(tmp, "nope"))
            self.assertIsNone(got)
            self.assertTrue(err)


class WorldReadsRealArtifactsTest(unittest.TestCase):
    """The anti-vacuous guard. Every test above runs on a synthetic World, so
    the suite would still pass if `rowworld` could not read a repository at
    all. This one builds a World from THIS checkout and proves the readers see
    real artifacts.

    THE RECEIPT IT READS IS MINTED HERE, INTO THE ISOLATED LEDGER (an xrev
    round, defect 14): the previous version asserted on whatever the AMBIENT
    gate ledger happened to hold, so under `tests/__init__`'s planted
    HELM_HOME — an empty estate — it failed, and on a box with a live estate
    it passed while proving nothing about which row was read. A receipt whose
    content-id `gate.receipts()` must RECOMPUTE, written by this class and
    then found bound to this checkout's own HEAD, is the assertion the old
    test only implied."""

    @classmethod
    def setUpClass(cls):
        from helm import gate as _gate
        cls.root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        head = subprocess.run(("git", "-C", cls.root, "rev-parse", "HEAD"),
                              capture_output=True, text=True)
        if head.returncode != 0:
            raise unittest.SkipTest("not a git checkout: %s" % head.stderr)
        cls.head = head.stdout.strip()
        if os.path.realpath(_gate.receipts_path()).startswith(
                os.path.realpath(os.path.expanduser("~/.helm")) + os.sep):
            raise unittest.SkipTest(
                "receipt ledger is the LIVE estate — tests/__init__ did not "
                "plant HELM_HOME; refusing to write a fixture receipt there")
        tree = subprocess.run(("git", "-C", cls.root, "rev-parse", "HEAD^{tree}"),
                              capture_output=True, text=True).stdout.strip()
        row = {"v": 1, "ts": "2026-08-07T00:00:00Z", "head": cls.head,
               "tree": tree, "dirty": False, "status": "OK", "ran": 1,
               "skipped": 0, "argv": ["true"], "repo_id": None,
               "interpreter": {"name": "cpython", "language": "py",
                               "executable": "/usr/bin/true"}}
        row["id"] = _gate._receipt_id(row)
        os.makedirs(os.path.dirname(_gate.receipts_path()), exist_ok=True)
        with open(_gate.receipts_path(), "a", encoding="utf-8") as fh:
            fh.write(__import__("json").dumps(row) + "\n")
        gitdir = subprocess.run(
            ("git", "-C", cls.root, "rev-parse", "--absolute-git-dir"),
            capture_output=True, text=True)
        cls.world = rowworld.snapshot(gitdir.stdout.strip(), trunk_ref="HEAD",
                                      rows={}, patch_cap=20, message_cap=50)

    def test_it_reads_this_checkouts_history(self):
        shas = self.world.trunk_shas
        self.assertTrue(shas)
        self.assertIn(self.head, shas)
        # The world read EVERY commit git counts from HEAD, on any history.
        count = _release.commit_count()
        self.assertEqual(len(shas), count)
        if count <= 100:
            self.skipTest("%d commit(s) reachable from HEAD, too few for the "
                          ">100 read this guard asks of a development "
                          "checkout; the world read all of them — a release "
                          "export or shallow clone (tests/_release.py)" % count)
        self.assertGreater(len(shas), 100)

    def test_it_reads_trees_and_orders_trunk(self):
        trees, index, dates = (self.world.commit_trees, self.world.trunk_index,
                               self.world.trunk_cts)
        self.assertTrue(trees)
        self.assertTrue(index)
        self.assertTrue(dates)
        self.assertIn(self.head, trees)
        self.assertEqual(index[self.head], 0)
        self.assertEqual(len(dates), len(index))

    def test_it_computes_patch_ids_for_real_commits(self):
        pids = self.world.trunk_patch_ids
        self.assertTrue(pids)
        wellformed = [pid for pid in pids
                      if re.fullmatch(r"[0-9a-f]{40}", pid)]
        self.assertTrue(wellformed)
        self.assertEqual(len(wellformed), len(pids))
        # setUpClass hashes the newest 20 non-merge commits (patch_cap=20).
        bearing = _release.commit_count("--no-merges", "--max-count=20")
        if bearing <= 5:
            self.skipTest("%d patch-bearing commit(s) in the window, too few "
                          "for the >5 this guard asks of a development "
                          "checkout — a release export or shallow clone "
                          "(tests/_release.py)" % bearing)
        self.assertGreater(len(pids), 5)

    def test_it_reads_the_gate_receipt_ledger(self):
        heads = self.world.receipts_by_head
        self.assertIsInstance(heads, dict)
        # THE ROW MINTED IN setUpClass, found by content — not "some ledger
        # somewhere was non-empty". Its id survived gate.receipts()'s
        # recomputation and it is bound to this checkout's own HEAD.
        self.assertIn(self.head, heads)
        self.assertEqual(heads[self.head][0].get("status"), "OK")
        # Stated positively on purpose: `unavailable is None` is the clean-read
        # fact, and asserting the fact beats asserting the absence of a moan.
        clean_read = self.world.unavailable is None
        self.assertTrue(clean_read)


class VerbSurfaceTest(unittest.TestCase):
    """The derivation reaches the owner. A module nothing can run is not a
    cure for a board he reads."""

    def test_the_verb_is_registered_and_documented(self):
        from helm import cli
        self.assertIn("derive", cli.VERBS)
        self.assertTrue(callable(cli.VERBS["derive"]))
        help_text = cli._VERB_HELP.get("derive")
        self.assertTrue(help_text)
        for promised in rowstate.STATES:
            self.assertIn(promised, help_text)

    def test_the_loader_exposes_the_pure_function(self):
        """rowworld imports rowstate, which is the edge that makes the pure
        module reachable from the CLI entry point."""
        self.assertTrue(hasattr(rowworld, "derive_all"))
        self.assertTrue(hasattr(rowworld, "cmd_derive"))
        self.assertIs(rowworld.rowstate, rowstate)

    def test_flag_reads_the_value_after_the_name(self):
        self.assertEqual(rowworld._flag(["--repo", "/x", "--json"], "--repo"),
                         "/x")
        self.assertIsNone(rowworld._flag(["--repo"], "--repo"))
        self.assertIsNone(rowworld._flag([], "--repo"))

    def test_evidence_detail_renders_as_json_safe_data(self):
        nested = rowstate.Evidence("id-binding", ("a1f5aee9", TRUNK_HEAD))
        plain = rowworld._plain(rowstate.Evidence("supersession-chain",
                                                  (("hop",), nested)).detail)
        self.assertEqual(plain, [["hop"], ["id-binding",
                                           ["a1f5aee9", TRUNK_HEAD]]])


if __name__ == "__main__":
    unittest.main()
