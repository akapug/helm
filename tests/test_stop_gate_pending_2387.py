#!/usr/bin/env python3
"""The stop guard's gate exemption answers BLOCK on what it cannot measure
(task/2387, the stop-guard item: lane/stop-guard-fix-round2 re-implemented
on current code; task/2812 family).

`seats_gate_exemption._gate_pending` GRANTS a stop exemption: a lease whose lane
is parked on someone else's verb (a review pending, an approve awaiting the
land) may stop and keep its lease. Its own docstring says UNKNOWN blocks at
every rung. Three rungs on trunk d7a57105 answered YES on UNKNOWN:

  1. THE OWED FRONTIER. When `dispatches.owed` raised, or the threaded
     snapshot was not a mapping it can index, every open row stayed
     ELIGIBLE, under a comment calling that the safe direction. Here an
     eligible row is what GRANTS the exemption, so it was fail-open: a lane
     read as in gate because the supersession walk threw.
  2. THE REPOSITORY. The docstring promised "rows from ANY OTHER repo all
     return None" while the code compared no repository. The ledger is
     global; a sibling clone's row at the same branch and commit carries
     the same lane label and the same ref, and granted the exemption here.
  3. (the other direction, measured on a live lane) A row whose typed LABEL
     is not the lease's lane family (`-port` is not a round suffix) but
     whose bound `ref_branch` names the lane exactly was read as no
     correspondence, so an approved lane was told to finish its work.

Carried by trunk already, and not rebuilt here: the stop seam's receipt
placement (gateimport's canonical binding, `receipt_repo_scope`), and the
stop-seam test runner at the bottom of its file.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-gatepending-", var="HELM_HOME")

from helm import dispatches, seats_gate_exemption  # noqa: E402


def _git(*args, cwd):
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                           *args], cwd=cwd, capture_output=True, text=True,
                          check=True, timeout=30).stdout.strip()


class _Lanes(unittest.TestCase):
    """A real repository with two lane rooms under its `-wt` container."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-gatepending-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        _git("init", "-q", "-b", "main", cwd=self.root)
        _git("commit", "-q", "--allow-empty", "-m", "tip", cwd=self.root)
        for lane in ("lane-u", "lane-u-port"):
            _git("worktree", "add", "-q", "-b", "lane/" + lane,
                 self.root + "-wt/" + lane, cwd=self.root)
        self.head = _git("rev-parse", "HEAD", cwd=self.root + "-wt/lane-u")
        self.common = os.path.realpath(os.path.join(self.root, ".git"))

    def row(self, rid="a" * 32, **over):
        row = {"id": rid, "kind": "review", "lane": "lane-u",
               "status": "sent", "ref": self.head, "recipient": "rev",
               "repo_id": self.common,
               "ref_branch": "refs/heads/lane/lane-u"}
        row.update(over)
        return row

    def approved(self, rid="b" * 32, **over):
        return self.row(rid, status="verdict", polarity="approve",
                        gate="c0ffee12", **over)

    def pending(self, snap, lane="lane-u", owed=None):
        rows = snap.values() if isinstance(snap, dict) else snap
        with mock.patch.object(
                dispatches, "owed",
                side_effect=owed or (lambda s, index=None: list(rows))):
            return seats_gate_exemption._gate_pending(
                "worktree:proj:" + lane, snap=snap, cwd=self.root)


class TheOwedFrontierIsOneAuthorityInputTest(_Lanes):
    """(1) an unmeasured frontier grants nothing."""

    def test_an_unreadable_frontier_grants_no_exemption(self):
        open_row = self.row()
        snap = {open_row["id"]: open_row}
        # control: the same row, the frontier measured, is a pending gate
        self.assertEqual(self.pending(snap)[2], "pending")

        def raises(*_a, **_k):
            raise RuntimeError("the supersession walk threw")
        self.assertIsNone(self.pending(snap, owed=raises))

    def test_a_snapshot_that_is_not_a_mapping_grants_no_exemption(self):
        open_row = self.row()
        self.assertEqual(self.pending({open_row["id"]: open_row})[2],
                         "pending")
        self.assertIsNone(self.pending([open_row]))

    def test_a_measured_frontier_still_decides(self):
        open_row, done = self.row(), self.approved()
        # a measured EMPTY frontier: the open row is carried elsewhere, and
        # the approve alone is the lane's gate
        snap = {open_row["id"]: open_row, done["id"]: done}
        self.assertEqual(
            self.pending(snap, owed=lambda s, index=None: [])[2], "approved")
        # both owed: two live gates for one head is ambiguous, as before
        self.assertIsNone(self.pending(snap))
        # an unreadable frontier over the same population grants nothing
        self.assertIsNone(self.pending(
            snap, owed=mock.Mock(side_effect=ValueError("unreadable"))))


class TheRowMustBeThisRepositorysTest(_Lanes):
    """(2) a row another repository wrote corresponds to nothing here."""

    def test_a_sibling_clones_row_grants_no_exemption(self):  # noqa: VACUOUS_ASSERTION — each stage first asserts THIS repository's row grants that stage, on the same predicate, before any absence is read
        for make, stage in ((self.row, "pending"),
                            (self.approved, "approved")):
            with self.subTest(stage=stage):
                mine = make()
                self.assertEqual(self.pending({mine["id"]: mine})[2], stage)
                theirs = make(repo_id=os.path.join(self.tmp, "clone", ".git"))
                self.assertIsNone(self.pending({theirs["id"]: theirs}))
                bare = make()
                del bare["repo_id"]
                self.assertIsNone(self.pending({bare["id"]: bare}))


class TheBoundBranchNamesTheLaneTest(_Lanes):
    """(3) the branch git found at the reviewed commit names the lane even
    when the typed label does not."""

    def test_a_bound_branch_corresponds_where_the_label_does_not(self):  # noqa: VACUOUS_ASSERTION — the bound-branch row is asserted to grant "approved" unconditionally before any absence is read
        port = _git("rev-parse", "HEAD", cwd=self.root + "-wt/lane-u-port")
        self.assertEqual(port, self.head)
        row = self.approved(ref_branch="refs/heads/lane/lane-u-port")
        self.assertEqual(self.pending({row["id"]: row}, lane="lane-u-port"),
                         (row["id"], "rev", "approved"))
        for branch in ("refs/heads/lane/other-lane", "lane/lane-u-port", ""):
            with self.subTest(ref_branch=branch):
                other = self.approved(ref_branch=branch)
                self.assertIsNone(self.pending({other["id"]: other},
                                               lane="lane-u-port"))


if __name__ == "__main__":
    unittest.main()
