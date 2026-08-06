#!/usr/bin/env python3
"""helm cli — the which-tree-did-I-just-run honesty surface.

Slice 0 gave every seat its own TREE, but `helm` on PATH still resolves to
whichever checkout installed it. So an agent can dogfood its own change and
actually exercise a different tree. Two silent directions: the change looks
broken when it is fine, or — the dangerous one — the dogfood PASSES because
the other tree already has an equivalent fix, and a broken version ships
behind a green demo.

DETECT AND REPORT: stderr, no refusal, no exit-code change, and it must fire
on a PASSING command, because passing-for-the-wrong-reason is the case it
exists for.
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-treewarn-", var="HELM_HOME")

from helm import cli  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TreeWarningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-treewarn-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _binary_repo(self):
        """A throwaway git repo holding a COPY of the helm package + entry script."""
        root = self._repo("bintree", with_helm=True)
        os.makedirs(os.path.join(root, "bin"), exist_ok=True)
        shutil.copy2(os.path.join(REPO, "bin", "helm"),
                     os.path.join(root, "bin", "helm"))
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "helm"], cwd=root, check=True)
        return root

    def _repo(self, name, with_helm=True):
        root = os.path.join(self.tmp, name)
        os.makedirs(root)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
        for cmd in (["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            subprocess.run(["git"] + cmd, cwd=root, check=True)
        if with_helm:
            shutil.copytree(os.path.join(REPO, "helm"), os.path.join(root, "helm"))
        open(os.path.join(root, "f"), "w").close()
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", name], cwd=root, check=True)
        return root

    def test_silent_when_standing_in_the_binarys_own_tree(self):
        """THE COMMON CASE — main, or a seat correctly using ./bin/helm. It
        must cost nothing and say nothing."""
        pkg = os.path.join(REPO, "helm")
        self.assertIsNone(cli.which_helm_warning(cwd=REPO, package_dir=pkg))
        self.assertIsNone(cli.which_helm_warning(cwd=os.path.join(REPO, "tests"),
                                                 package_dir=pkg))

    def test_silent_when_standing_in_a_non_helm_git_checkout(self):
        """Task #183: An adopter standing in their own non-helm git repository using a
        global helm binary should NOT see a tree mismatch warning.
        # noqa: VACUOUS_ASSERTION — assertIsNone asserts silence on normal non-helm adopter repository"""
        a = self._binary_repo()
        adopter_repo = self._repo("adopter-project", with_helm=False)
        self.assertIsNone(
            cli.which_helm_warning(cwd=adopter_repo,
                                   package_dir=os.path.join(a, "helm")),
            "standing in a non-helm git repository is normal usage, not a mismatch",
        )

    def test_warns_and_names_BOTH_trees_when_they_differ(self):
        a, b = self._repo("bintree"), self._repo("cwdtree")
        warn = cli.which_helm_warning(cwd=b, package_dir=os.path.join(a, "helm"))
        self.assertIsNotNone(warn)
        self.assertIn(a, warn)                 # which binary actually ran
        self.assertIn(b, warn)                 # where the operator is standing
        self.assertIn("PASSES", warn)          # names the dangerous direction

    def test_silent_outside_any_checkout(self):
        a = self._repo("bintree")
        plain = os.path.join(self.tmp, "not-a-repo")
        os.makedirs(plain)
        self.assertIsNone(
            cli.which_helm_warning(cwd=plain, package_dir=os.path.join(a, "helm")))

    def test_silent_when_the_binary_is_not_in_a_checkout(self):
        b = self._repo("cwdtree")
        loose = os.path.join(self.tmp, "installed-elsewhere")
        os.makedirs(loose)
        self.assertIsNone(cli.which_helm_warning(cwd=b, package_dir=loose))

    def test_a_deleted_cwd_never_raises(self):
        """_self_seat's eager-getcwd crash class: this runs in FRONT of every
        invocation, so it must fail open rather than take the CLI down."""
        gone = os.path.join(self.tmp, "gone")
        os.makedirs(gone)
        saved = os.getcwd()
        self.addCleanup(os.chdir, saved)
        os.chdir(gone)
        os.rmdir(gone)
        self.assertRaises(OSError, os.getcwd)          # the precondition
        self.assertIsNone(cli.which_helm_warning())    # named, not crashed

    def test_it_fires_on_a_PASSING_command_and_changes_no_exit_code(self):
        """A warning that only appears on errors would miss exactly the case
        it exists for. End-to-end through the real CLI."""
        a, b = self._binary_repo(), self._repo("cwdtree")
        out = subprocess.run([os.path.join(a, "bin", "helm"), "--version"],
                             cwd=b, capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0)            # never a refusal
        self.assertIn("helm ", out.stdout)             # the command still worked
        self.assertIn("you ran", out.stderr)           # and it warned anyway
        self.assertIn(a, out.stderr)

    def test_the_escape_hatch_silences_it(self):
        a, b = self._binary_repo(), self._repo("cwdtree")
        env = dict(os.environ, HELM_NO_TREE_WARNING="1")
        out = subprocess.run([os.path.join(a, "bin", "helm"), "--version"],
                             cwd=b, capture_output=True, text=True, timeout=30,
                             env=env)
        self.assertEqual(out.returncode, 0)
        self.assertNotIn("you ran", out.stderr)


if __name__ == "__main__":
    unittest.main()


class SameCommitIsSilentTest(unittest.TestCase):
    """The pin that was missing, and the reason the bug shipped.

    The original suite exercised only the case where the two trees DIFFER, where
    the warning correctly fires. Nobody exercised the case where they are at the
    SAME COMMIT — identical code, nothing to warn about — so the first version
    compared only tree PATHS and fired from every worktree on every invocation.
    Author, cross-family reviewer and integrator all passed it.

    A warning that fires when there is nothing wrong is worse than no warning:
    agents learn to skip it, and then it is on screen during the one invocation
    that mattered and nobody reads it. So the SILENT cases are pinned as hard as
    the speaking one.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-treewarn-same-")
        self.repo = os.path.join(self.tmp, "proj")
        os.makedirs(self.repo)
        self._git(self.repo, "init", "-q", "-b", "main", ".")
        self._git(self.repo, "config", "user.email", "t@t")
        self._git(self.repo, "config", "user.name", "t")
        shutil.copytree(os.path.join(REPO, "helm"), os.path.join(self.repo, "helm"))
        with open(os.path.join(self.repo, "a.txt"), "w") as f:
            f.write("one\n")
        self._git(self.repo, "add", "-A")
        self._git(self.repo, "commit", "-qm", "one")
        self.other = os.path.join(self.tmp, "other")
        self._git(self.repo, "worktree", "add", "-q", "--detach", self.other)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _git(self, where, *args):
        return subprocess.run(["git", "-C", where] + list(args),
                              capture_output=True, text=True)

    def _warn(self):
        return cli.which_helm_warning(
            cwd=self.other, package_dir=os.path.join(self.repo, "helm"))

    def test_two_worktrees_at_the_SAME_commit_say_nothing(self):
        self.assertIsNone(self._warn(), "identical code in two paths is not a "
                                        "mismatch — this is the noise case")

    def test_it_speaks_once_the_commits_actually_diverge(self):
        """The other half: silence must not be achieved by breaking detection."""
        with open(os.path.join(self.repo, "a.txt"), "w") as f:
            f.write("two\n")
        self._git(self.repo, "commit", "-qam", "two")
        warning = self._warn()
        self.assertIsNotNone(warning, "a real divergence must still be reported")
        self.assertIn("exercised the", warning)

    def test_an_unreadable_head_says_it_could_not_compare(self):
        """could-not-look is never reported as a fact: it still speaks, because
        missing a real mismatch is the worse failure on an advisory surface, but
        it must not claim a difference it never measured."""
        with mock.patch.object(cli, "_tree_state",
                               return_value=("?", True)):
            warning = self._warn()
        self.assertIsNotNone(warning)
        self.assertIn("could not compare", warning)

    def test_same_commit_but_DIRTY_helm_still_speaks(self):
        """The false NEGATIVE my first fix introduced, and the likeliest real
        shape of the bug: matching HEADs do not prove matching code. An agent
        edits helm/ in its lane, runs plain `helm`, and exercises the other
        tree — silencing that would make the guard quiet exactly where it
        matters. Bind the true provenance; do not just relax the predicate."""
        os.makedirs(os.path.join(self.other, "helm"), exist_ok=True)
        with open(os.path.join(self.other, "helm", "cli.py"), "w") as f:
            f.write("# uncommitted edit in the tree I am standing in\n")
        warning = self._warn()
        self.assertIsNotNone(
            warning, "a dirty helm/ at the same commit is still a code "
                     "difference — it must not be silenced")

    def test_a_dirty_file_OUTSIDE_helm_stays_silent(self):
        """Scoped deliberately: an edit to docs or tests cannot change which
        behaviour an invocation ran, and warning on it would rebuild the noise
        problem from the other side."""
        with open(os.path.join(self.other, "NOTES.md"), "w") as f:
            f.write("scratch\n")
        self.assertIsNone(self._warn())
