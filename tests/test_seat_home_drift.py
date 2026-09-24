#!/usr/bin/env python3
"""A REUSED seat home says when it is behind trunk — a seat's finding.

A seat home is provisioned ONCE, at whatever trunk happened to be, and the reuse
leg used to hand it back with no further word. The first repair printed an exact
`git rebase` command. Three authors then obeyed, gated, and reported a true
ancestry check that another land invalidated before the report arrived. The
instruction was the defect: only the integrator can serialize landing order.

The pins here are deliberately about what it does NOT do as much as what it does:
it must not move, rebase, or check out anything, and it must not report drift it
could not actually measure.
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import harness                                        # noqa: E402


class SeatHomeDriftTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-home-drift-")
        self.repo = os.path.join(self.tmp, "proj")
        os.makedirs(self.repo)
        self._git("init", "-q", "-b", "main", ".")
        self._git("config", "user.email", "t@t")
        self._git("config", "user.name", "t")
        self._commit("a.txt", "one")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _git(self, *args, where=None):
        return subprocess.run(["git", "-C", where or self.repo] + list(args),
                              capture_output=True, text=True)

    def _commit(self, name, body):
        with open(os.path.join(self.repo, name), "w") as f:
            f.write(body + "\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "add " + name)

    def _provision(self):
        return harness.ensure_home_worktree("cd", self.repo)

    # --- measurement ---------------------------------------------------------

    def test_a_fresh_home_is_not_behind(self):
        self._provision()
        self.assertEqual(harness.home_drift(self.repo, "cd"), (0, 0))

    def test_trunk_moving_makes_the_home_behind_by_exactly_that_much(self):
        self._provision()
        self._commit("b.txt", "two")
        self._commit("c.txt", "three")
        self.assertEqual(harness.home_drift(self.repo, "cd"), (2, 0))

    def test_unmeasurable_drift_is_None_never_zero(self):
        """A measurement we could not take is not a measurement of zero — the
        same vacuous-pass law the marker probe enforces on the filesystem."""
        self._provision()
        self.assertIsNone(harness.home_drift(self.repo, "cd",
                                             base="no-such-ref"))
        self.assertIsNone(harness.home_drift(self.repo, "never-a-seat"))

    # --- reporting -----------------------------------------------------------

    def test_reuse_reports_the_drift_loudly(self):
        self._provision()
        self._commit("b.txt", "two")
        err = io.StringIO()
        with redirect_stderr(err):
            path = self._provision()          # the REUSE leg
        text = err.getvalue()
        self.assertIn("BEHIND", text)
        self.assertIn("1 commit ", text)      # singular, not "1 commits"
        self.assertIn("DO NOT rebase", text)
        self.assertIn("integrator", text)
        self.assertIn("landing order", text)
        self.assertNotIn("git -C", text,
                         "the old author-side rebase command recreates the race")

    def test_a_current_home_says_nothing(self):
        """Not overbearing: silence is the correct output for a fresh home, or
        agents learn to ignore the channel."""
        self._provision()
        err = io.StringIO()
        with redirect_stderr(err):
            self._provision()
        self.assertEqual(err.getvalue(), "")

    def test_unmeasurable_drift_prints_nothing(self):
        self._provision()
        err = io.StringIO()
        with redirect_stderr(err):
            harness.report_home_drift(self.repo, "cd", base="no-such-ref")
        self.assertEqual(err.getvalue(), "")

    # --- the half that matters most: it must not MOVE anything --------------

    def test_reporting_moves_nothing_at_all(self):
        """Auto-rebasing is the one thing this must never do — a rebase can
        conflict, and a tree helm does not own is not helm's to resolve. Same law
        that makes `git stash` unsafe on a shared checkout."""
        path = self._provision()
        self._commit("b.txt", "two")
        before_home = self._git("rev-parse", "HEAD", where=path).stdout.strip()
        before_trunk = self._git("rev-parse", "HEAD").stdout.strip()
        before_status = self._git("status", "--porcelain", where=path).stdout
        with redirect_stderr(io.StringIO()):
            self._provision()
        self.assertEqual(self._git("rev-parse", "HEAD",
                                   where=path).stdout.strip(), before_home,
                         "the seat's home HEAD moved — it must only be measured")
        self.assertEqual(self._git("rev-parse", "HEAD").stdout.strip(),
                         before_trunk)
        self.assertEqual(self._git("status", "--porcelain",
                                   where=path).stdout, before_status)

    def test_a_home_with_its_own_commits_is_still_only_reported(self):
        """A seat AHEAD of trunk is the normal case for one mid-task. It must be
        reported (if also behind) and never rebased out from under its owner."""
        path = self._provision()
        with open(os.path.join(path, "seat-work.txt"), "w") as f:
            f.write("mid-task\n")
        self._git("add", "-A", where=path)
        self._git("commit", "-qm", "seat work", where=path)
        self._commit("b.txt", "two")
        drift = harness.home_drift(self.repo, "cd")
        self.assertEqual(drift, (1, 1))
        head = self._git("rev-parse", "HEAD", where=path).stdout.strip()
        with redirect_stderr(io.StringIO()):
            self._provision()
        self.assertEqual(self._git("rev-parse", "HEAD",
                                   where=path).stdout.strip(), head)


if __name__ == "__main__":
    unittest.main()
