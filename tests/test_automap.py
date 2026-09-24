#!/usr/bin/env python3
"""helm.automap — raw harness observations -> the real project list: the noise
filter, cwd canonicalization (git-root fold, worktree stripping, dead-path
anchoring), name collision qualification, the repo shelf scan, and the
promotion engine. Hermetic: tmp dirs only, the noise filter stubbed where tmp
paths must register."""
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import automap  # noqa: E402

FAKE_HOME = "/nonexistent-home-xyz"          # never equals a tmp path


def _obs(cwd, harness="claude", sessions=1, days=None, last_seen=1_900_000_000.0):
    return {"cwd": cwd, "harness": harness, "sessions": sessions,
            "last_seen": last_seen, "days": days or {"2026-07-01"}, "refs": []}


class NoiseTest(unittest.TestCase):
    def test_scratch_prefixes_and_relative_paths_are_noise(self):
        for p in ("/tmp/x", "/var/tmp/y", "/dev/shm/z", "/run/w", "relative/path"):
            self.assertTrue(automap._is_noise(p), p)

    def test_real_project_paths_are_not_noise(self):
        self.assertFalse(automap._is_noise("/home/u/dev/proj"))


class StripWorktreeTest(unittest.TestCase):
    def test_worktrees_segment_folds_to_project(self):
        self.assertEqual(automap._strip_worktree("/a/proj/worktrees/x/y"), "/a/proj")

    def test_sibling_suffix_folds_to_project(self):
        self.assertEqual(automap._strip_worktree("/a/proj-worktrees/x"), "/a/proj")
        self.assertEqual(automap._strip_worktree("/a/proj-wt/x"), "/a/proj")

    def test_plain_path_unchanged(self):
        self.assertEqual(automap._strip_worktree("/a/proj/src"), "/a/proj/src")


class NameForTest(unittest.TestCase):
    def test_basename_when_free(self):
        self.assertEqual(automap._name_for("/a/b/proj", {}, FAKE_HOME), "proj")

    def test_collision_qualifies_with_parent(self):
        taken = {"proj": "/a/b/proj"}
        self.assertEqual(automap._name_for("/a/c/proj", taken, FAKE_HOME), "c-proj")

    def test_same_root_keeps_its_name(self):
        taken = {"proj": "/a/b/proj"}
        self.assertEqual(automap._name_for("/a/b/proj", taken, FAKE_HOME), "proj")


class CanonicalizeTest(unittest.TestCase):
    def test_root_and_home_never_a_project(self):
        self.assertIsNone(automap.canonicalize("/", home=FAKE_HOME))
        self.assertIsNone(automap.canonicalize(FAKE_HOME, home=FAKE_HOME))

    def test_existing_nonrepo_dir_is_a_candidate(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(automap.canonicalize(d, home=FAKE_HOME), d)

    def test_dead_nonrepo_path_anchors_to_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            # a path under an existing but non-repo ancestor has nothing to anchor to
            self.assertIsNone(automap.canonicalize(os.path.join(d, "gone", "deep"),
                                                   home=FAKE_HOME))

    def test_real_git_worktree_folds_to_main_repo(self):
        with tempfile.TemporaryDirectory() as d:
            repo = os.path.join(d, "proj")
            os.makedirs(repo)
            subprocess.run(["git", "init", "-q", repo], check=True)
            sub = os.path.join(repo, "src")
            os.makedirs(sub)
            self.assertEqual(automap.canonicalize(sub, home=FAKE_HOME),
                             os.path.realpath(repo))

    def test_dead_worktree_dir_folds_to_existing_project(self):
        with tempfile.TemporaryDirectory() as d:
            proj = os.path.join(d, "proj")
            os.makedirs(os.path.join(proj, ".git"))            # fake repo (no real git)
            wt = os.path.join(d, "proj-wt", "x")
            os.makedirs(wt)
            self.assertEqual(automap.canonicalize(wt, home=FAKE_HOME), proj)


class ScanReposTest(unittest.TestCase):
    def test_finds_git_repos_and_skips_worktree_and_dotdirs(self):
        with tempfile.TemporaryDirectory() as root:
            for name in ("proj", "other", "helm-wt", "references", ".hidden"):
                os.makedirs(os.path.join(root, name, ".git"))
            found = automap.scan_repos(roots=[root])
            self.assertEqual(set(os.path.basename(p) for p in found), {"proj", "other"})


class BuildMapTest(unittest.TestCase):
    def setUp(self):
        # tmp cwds must survive the noise filter to exercise promotion
        patch = mock.patch.object(automap, "_is_noise", lambda cwd: False)
        patch.start()
        self.addCleanup(patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _dir(self, *parts, git=False):
        p = os.path.join(self.tmp.name, *parts)
        os.makedirs(p, exist_ok=True)
        if git:
            os.makedirs(os.path.join(p, ".git"), exist_ok=True)
        return p

    def _map(self, obs):
        return automap.build_map(observations=obs, home=FAKE_HOME, now=1_900_000_100.0)

    def test_git_repo_promotes_on_a_single_session(self):
        repo = self._dir("proj", git=True)
        projs = self._map([_obs(repo, sessions=1)])
        self.assertIn("proj", projs)
        self.assertEqual(projs["proj"]["kind"], "git")
        self.assertEqual(projs["proj"]["path"], repo)

    def test_nonrepo_promotes_on_the_session_threshold(self):
        d = self._dir("busy")
        projs = self._map([_obs(d, sessions=automap.PROMOTE_SESSIONS)])
        self.assertIn("busy", projs)
        self.assertEqual(projs["busy"]["kind"], "dir")

    def test_nonrepo_promotes_on_the_days_threshold(self):
        d = self._dir("recurring")
        days = {"2026-07-0%d" % n for n in range(1, automap.PROMOTE_DAYS + 1)}
        projs = self._map([_obs(d, sessions=1, days=days)])
        self.assertIn("recurring", projs)

    def test_oneoff_nonrepo_never_registers(self):
        d = self._dir("oneoff")
        self.assertEqual(self._map([_obs(d, sessions=1, days={"2026-07-01"})]), {})

    def test_dormant_status_by_age(self):
        repo = self._dir("old", git=True)
        old = 1_900_000_100.0 - (automap.DORMANT_DAYS + 5) * 86400
        projs = self._map([_obs(repo, sessions=1, last_seen=old)])
        self.assertEqual(projs["old"]["status"], "dormant")

    def test_worktree_and_main_cwds_merge_into_one_project(self):
        proj = self._dir("proj", git=True)
        self._dir("proj-wt", "x")                       # sibling worktree checkout
        wt = os.path.join(self.tmp.name, "proj-wt", "x")
        projs = self._map([_obs(proj, sessions=2),
                           _obs(wt, harness="codex", sessions=1)])
        self.assertEqual(len(projs), 1)
        p = projs["proj"]
        self.assertIn(proj, p["cwds"])
        self.assertIn(wt, p["cwds"])                    # the worktree folded in
        self.assertEqual(set(p["sessions"]), {"claude", "codex"})


if __name__ == "__main__":
    unittest.main()
