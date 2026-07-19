#!/usr/bin/env python3
"""capsule tests — era-sha resolution (session last-activity -> git commit via
rev-list --before) and every degradation path. Hermetic: a tiny planted git
repo in a tempdir, the catalog row injected via transcripts._resolve_sid; no
real catalog, repo or session store is touched."""
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("HELM_HOME", tempfile.mkdtemp(prefix="helm-test-home-"))

from helm import capsule, transcripts  # noqa: E402

SID = "aaaaaaaa-1111-2222-3333-444444444444"
T1 = 1700000000  # commit 1
T2 = 1750000000  # commit 2


def _git(cwd, *args, env=None):
    e = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null",
             **(env or {}))
    r = subprocess.run(["git", "-C", cwd] + list(args), env=e,
                       capture_output=True, text=True, check=True)
    return r.stdout.strip()


class CapsuleBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="helm-test-capsule-")
        cls.repo = os.path.join(cls.tmp, "repo")
        os.makedirs(cls.repo)
        _git(cls.repo, "init", "-q", "-b", "main")
        _git(cls.repo, "config", "user.email", "t@t")
        _git(cls.repo, "config", "user.name", "t")
        for name, ts in (("one", T1), ("two", T2)):
            with open(os.path.join(cls.repo, name + ".txt"), "w") as f:
                f.write(name)
            _git(cls.repo, "add", "-A")
            _git(cls.repo, "commit", "-q", "-m", name,
                 env={"GIT_AUTHOR_DATE": "%d +0000" % ts,
                      "GIT_COMMITTER_DATE": "%d +0000" % ts})
        cls.sha1 = _git(cls.repo, "rev-list", "--max-count=1", "HEAD~1")
        cls.sha2 = _git(cls.repo, "rev-list", "--max-count=1", "HEAD")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_capsule(self, row, args=None):
        with mock.patch.object(transcripts, "_resolve_sid",
                               return_value=(row, None)):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = capsule.cmd_capsule(args or [SID[:8]])
        return rc, out.getvalue(), err.getvalue()

    def row(self, **over):
        base = {"i": SID, "h": "claude", "t": "capsule work",
                "cwd": self.repo, "mt": (T1 + T2) // 2, "b": "main"}
        base.update(over)
        return base


class EraShaTest(CapsuleBase):
    def test_mid_history_activity_resolves_the_older_commit(self):
        rc, out, _ = self.run_capsule(self.row())  # mt between the two commits
        self.assertEqual(rc, 0)
        self.assertIn("era: %s on main" % self.sha1[:12], out)
        self.assertIn("git -C %s worktree add" % self.repo, out)
        self.assertIn("%s-capsules/%s-%s" % (self.repo, SID[:8], self.sha1[:8]), out)
        self.assertIn("helm sessions resume " + SID[:8], out)
        self.assertIn("helm rehome " + SID[:8], out)

    def test_activity_after_head_resolves_the_newest_commit(self):
        rc, out, _ = self.run_capsule(self.row(mt=T2 + 5000))
        self.assertEqual(rc, 0)
        self.assertIn("era: %s on main" % self.sha2[:12], out)

    def test_undated_row_falls_back_to_ref_tip(self):
        rc, out, _ = self.run_capsule(self.row(mt=None))
        self.assertEqual(rc, 0)
        self.assertIn("era: %s on main (undated)" % self.sha2[:12], out)

    def test_head_branch_marker_falls_back_to_HEAD(self):
        rc, out, _ = self.run_capsule(self.row(b="HEAD"))
        self.assertEqual(rc, 0)
        self.assertIn("era: %s on HEAD" % self.sha1[:12], out)
        rc, out, _ = self.run_capsule(self.row(b=None))
        self.assertIn("on HEAD", out)


class DegradationTest(CapsuleBase):
    def test_non_git_cwd_degrades_to_plain_resume(self):
        plain = os.path.join(self.tmp, "not-a-repo")
        os.makedirs(plain, exist_ok=True)
        rc, out, _ = self.run_capsule(self.row(cwd=plain))
        self.assertEqual(rc, 0)
        self.assertIn("session cwd is not a git repo", out)
        self.assertIn("resume:  helm sessions resume " + SID[:8], out)
        self.assertNotIn("era:", out)

    def test_vanished_cwd_degrades_the_same_way(self):
        rc, out, _ = self.run_capsule(self.row(cwd=os.path.join(self.tmp, "gone")))
        self.assertEqual(rc, 0)
        self.assertIn("not a git repo (or vanished)", out)

    def test_no_commit_before_activity(self):
        rc, out, _ = self.run_capsule(self.row(mt=T1 - 10 ** 6))
        self.assertEqual(rc, 0)
        self.assertIn("no commit on main before the session's last activity", out)
        self.assertIn("resume:  helm sessions resume " + SID[:8], out)

    def test_unknown_branch_ref_degrades_not_crashes(self):
        rc, out, _ = self.run_capsule(self.row(b="ghost-branch"))
        self.assertEqual(rc, 0)
        self.assertIn("no commit on ghost-branch", out)

    def test_unresolved_sid_is_an_error(self):
        with mock.patch.object(transcripts, "_resolve_sid",
                               return_value=(None, {"error": "no session id"})):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = capsule.cmd_capsule(["zzzz99"])
        self.assertEqual(rc, 1)
        self.assertIn("no session id", err.getvalue())

    def test_usage(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = capsule.cmd_capsule([])
        self.assertEqual(rc, 2)
        self.assertIn("usage:", err.getvalue())


if __name__ == "__main__":
    unittest.main()
