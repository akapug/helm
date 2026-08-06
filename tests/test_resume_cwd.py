#!/usr/bin/env python3
"""Where a RESUMED seat lands — the half of slice 0 that spawn-only missed.

Slice 0 gave `helm seat spawn` a per-seat home worktree. Resume kept
`cwd=sess_cwd or safe_cwd()`, the cwd SNIFFED from the seat's newest session, so
resuming a seat that had been working in the shared checkout put it right back
into the shared checkout. Every seat in this fleet is a long-lived RESUME, so a
spawn-only default isolated almost nothing.

THE PINS BELOW EXIST IN BOTH DIRECTIONS, deliberately: refusing a degraded cwd is
only half the contract. A seat resumed out of a real LANE worktree must come
BACK to it — that is its task, mid-flight — so "always go home" would be a
regression dressed as a fix, and there is a pin that fails if anyone writes it.

`seats.TEMP_ROOTS` is PATCHED in every fixture rather than trusted: these tests
build their repos under tempfile's own directory, which is normally /tmp, so an
unpatched run would classify the fixture repo itself as a throwaway cwd and
every assertion below would pass for the wrong reason.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import seat, seats                                   # noqa: E402


class ResumeCwdTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-resume-cwd-")
        self.repo = os.path.join(self.tmp, "proj")
        os.makedirs(self.repo)
        self._git("init", "-q", "-b", "main", ".")
        self._git("config", "user.email", "t@t")
        self._git("config", "user.name", "t")
        with open(os.path.join(self.repo, "a.txt"), "w") as f:
            f.write("hi\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "init")
        self.home = os.path.join(self.tmp, "proj-wt", "seats", "cd")
        # the fixture tree is under tempfile's root; without this the repo would
        # itself read as "throwaway" and the tests would be vacuous
        self.roots = mock.patch.object(seats, "TEMP_ROOTS", ("/dev/shm",))
        self.roots.start()
        self.addCleanup(self.roots.stop)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _git(self, *args, where=None):
        return subprocess.run(["git", "-C", where or self.repo] + list(args),
                              capture_output=True, text=True)

    # --- the degraded locations a seat must never be resumed into ------------

    def test_the_shared_main_checkout_is_refused_for_the_seats_own_home(self):
        """The bug, exactly: console-design's session sniffed to the shared
        checkout, so every resume put it back where all nine seats collide."""
        got = seat._resume_cwd("cd", self.repo)
        self.assertEqual(got, self.home)
        self.assertTrue(os.path.isdir(got))
        self.assertEqual(
            self._git("rev-parse", "--abbrev-ref", "HEAD",
                      where=got).stdout.strip(), "seat/cd")

    def test_a_throwaway_temp_cwd_is_refused_too(self):
        """The other degraded shape — a row whose cwd read /tmp, which is how a
        seat's roster row came to describe a dead ephemeral child."""
        junk = os.path.join(self.tmp, "junk")
        os.makedirs(junk)
        # safe_cwd MUST be pinned to the fixture. A temp cwd names no repo, so
        # this path resolves one from the invoking process — and an unpinned run
        # resolves the REAL helm checkout and provisions a worktree in it. The
        # first version of this test did exactly that (tests reaching live
        # state), which is the same class as a unit test reaching a live daemon.
        with mock.patch.object(seats, "TEMP_ROOTS", (self.tmp,)), \
             mock.patch.object(seats, "safe_cwd", return_value=self.repo):
            got = seat._resume_cwd("cd", junk)
        self.assertEqual(got, self.home)

    def test_a_temp_cwd_with_no_repo_in_reach_is_left_alone(self):
        """Nothing better exists, so it must not invent a destination."""
        junk = os.path.join(self.tmp, "junk2")
        os.makedirs(junk)
        outside = os.path.join(self.tmp, "norepo")
        os.makedirs(outside)
        with mock.patch.object(seats, "TEMP_ROOTS", (self.tmp,)), \
             mock.patch.object(seats, "safe_cwd", return_value=outside):
            self.assertEqual(seat._resume_cwd("cd", junk), junk)

    # --- the continuity half: a real room is KEPT ---------------------------

    def test_a_real_lane_worktree_is_KEPT_not_yanked_home(self):
        """If this ever fails because someone made resume always go home, the
        fix regressed: a seat mid-task in a lane room would be pulled out of it.
        Refusing a DEGRADED cwd is the contract, not centralising every seat."""
        lane = os.path.join(self.tmp, "proj-wt", "lane-x")
        rc = self._git("worktree", "add", "-q", "-b", "lane/x", lane)
        self.assertEqual(rc.returncode, 0, rc.stderr)
        self.assertEqual(seat._resume_cwd("cd", lane), lane)
        self.assertFalse(os.path.isdir(self.home),
                         "keeping a real lane room must not provision a home "
                         "as a side effect")

    def test_a_directory_outside_any_checkout_is_kept(self):
        outside = os.path.join(self.tmp, "elsewhere")
        os.makedirs(outside)
        self.assertEqual(seat._resume_cwd("cd", outside), outside)

    def test_no_sniffed_cwd_keeps_the_old_safe_cwd_behaviour(self):
        with mock.patch.object(seats, "safe_cwd", return_value="/some/where"):
            self.assertEqual(seat._resume_cwd("cd", None), "/some/where")
            self.assertEqual(seat._resume_cwd("cd", ""), "/some/where")

    def test_an_unnamed_seat_changes_nothing(self):
        self.assertEqual(seat._resume_cwd(None, self.repo), self.repo)

    # --- the fallback must not land in the INVOKER's tree -------------------

    def test_home_worktree_failure_degrades_to_the_SEATS_tree_not_the_callers(self):
        """`_seat_home_cwd` failed open to safe_cwd() — the INVOKING process's
        cwd. Right for spawn (the operator's cwd is a new seat's only
        reference), wrong for resume: it would drop another agent's pane into
        whatever tree the integrator happened to be standing in."""
        from helm import harness
        with mock.patch.object(harness, "ensure_home_worktree",
                               side_effect=harness.HarnessError("no disk")), \
             mock.patch.object(harness, "detect", return_value=None), \
             mock.patch.object(seats, "safe_cwd",
                               return_value="/the/integrators/tree"):
            got = seat._resume_cwd("cd", self.repo)
        self.assertEqual(got, self.repo)
        self.assertNotEqual(got, "/the/integrators/tree")

    def test_seat_home_cwd_base_resolves_the_repo_from_base_not_the_process(self):
        """The `base` parameter is what makes the above possible: resume has to
        resolve the seat's repo from the SEAT's directory, because the invoking
        integrator may not even be inside the same checkout."""
        with mock.patch.object(seats, "safe_cwd", return_value="/nowhere/real"):
            self.assertEqual(seat._seat_home_cwd("cd", base=self.repo),
                             self.home)

    # --- the record and the spawn must not disagree -------------------------

    def test_the_spawn_record_cannot_go_back_to_naming_the_sniffed_cwd(self):
        """A source pin, because the divergence is invisible at runtime: the
        pane would be in the home worktree while its spawn record named the
        sniffed cwd, so every reader of the register would be told the seat is
        in a tree it is not in. Same family as the roster rows that described a
        process that had already exited."""
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "helm", "seat.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn('"worktree": sess_cwd', src,
                         "the resume spawn RECORD must carry the cwd the pane "
                         "was actually given (resume_cwd), never the raw "
                         "sniffed cwd")
        self.assertIn('"worktree": resume_cwd', src)


if __name__ == "__main__":
    unittest.main()
