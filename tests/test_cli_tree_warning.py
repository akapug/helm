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
import functools
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

from helm import cli, selfrepo  # noqa: E402

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


class HelmRepoScopeTest(unittest.TestCase):
    """TASK/2442 — the maker warning is about HELM'S OWN SOURCE, never about a
    project repo helm is being used ON.

    A team USING helm must never need to know how helm is made. This surface
    exists for one mistake only: a maker edits helm/, runs the `helm` on PATH,
    and exercises a different helm tree. An adopter standing in their own
    project cannot make that mistake — there is no helm code in their tree to
    exercise — so the warning is noise there, and noise is how a warning stops
    being read.

    THE FALSE POSITIVE WAS THE ENTRY SCRIPT. `_is_helm_checkout` accepted a
    tree holding `bin/helm`, which is the natural one-line wrapper for a
    project that wants `./bin/helm` on its own PATH. Measured against the
    shipped CLI: a scratch project repo carrying only such a
    wrapper printed the full "you ran helm@… — this command exercised the
    FIRST tree" line on `helm version`.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-treewarn-scope-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _git(self, where, *args):
        out = subprocess.run(["git", "-C", where] + list(args),
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out

    def _repo(self, name, helm_source, package=("__init__.py", "cli.py",
                                                "seat.py")):
        """A real git repo, with REAL bytes: every tree gets the true `bin/helm`
        entry script (what an adopter copies), and a helm-source tree also gets
        the running package's own modules at its root. No skeleton whose
        contents nothing produced. `package` is which of them the tree carries,
        so a caller can fixture a checkout mid-refactor.

        THE DEFAULT CARRIES TWO MODULES BESIDE THE PACKAGE ROOT because that is
        what a helm checkout is, and because the DAMAGED state is now keyed on
        helm-source PROVENANCE: `helm/` holding helm's own modules, not merely
        holding a `.py`. A one-module fixture would have been a tree stripped to
        a name, which is not the state a maker's tree mid-move is in."""
        root = os.path.join(self.tmp, name)
        os.makedirs(os.path.join(root, "bin"))
        shutil.copy2(os.path.join(REPO, "bin", "helm"),
                     os.path.join(root, "bin", "helm"))
        if helm_source:
            os.makedirs(os.path.join(root, "helm"))
            for mod in package:
                shutil.copy2(os.path.join(REPO, "helm", mod),
                             os.path.join(root, "helm", mod))
        self._git(root, "init", "-q", "-b", "main", ".")
        self._git(root, "config", "user.email", "t@t")
        self._git(root, "config", "user.name", "t")
        # THE SEED CARRIES THE REPO'S NAME so two independently built repos
        # never land on the same commit sha — identical content, message and
        # author in the same second collided, and a collision reads as "the
        # same commit", which is this surface's silence condition.
        with open(os.path.join(root, "a.txt"), "w") as f:
            f.write("one %s\n" % name)
        self._git(root, "add", "-A")
        self._git(root, "commit", "-qm", "one")
        return root

    def _diverged_worktree(self, root, name):
        """A worktree of `root` whose commit DIFFERS from root's, so the only
        thing that can keep the warning quiet is the helm-repo scope test."""
        other = os.path.join(self.tmp, name)
        self._git(root, "worktree", "add", "-q", "--detach", other)
        with open(os.path.join(root, "a.txt"), "w") as f:
            f.write("two\n")
        self._git(root, "commit", "-qam", "two")
        return other

    def test_silent_from_a_worktree_of_a_PROJECT_repo(self):
        """RED before the cure: the project's `bin/helm` wrapper alone made the
        tree read as a helm checkout, and the warning fired on every command."""
        binary = self._repo("helm-clone", helm_source=True)
        project = self._repo("adopter-project", helm_source=False)
        room = self._diverged_worktree(project, "adopter-lane")
        self.assertTrue(os.path.isfile(os.path.join(room, "bin", "helm")),
                        "fixture: the wrapper that used to trigger it is there")
        self.assertIsNone(
            cli.which_helm_warning(cwd=room,
                                   package_dir=os.path.join(binary, "helm")),
            "a lane of an adopter's project holds no helm code to exercise")

    def test_still_fires_from_a_worktree_of_the_HELM_repo(self):
        """MUST-HIT CONTROL. The silence above must come from the scope test,
        not from a predicate that stopped answering: the same shape over a tree
        that really does carry helm's package still names both trees."""
        helm_repo = self._repo("helm-main", helm_source=True)
        room = self._diverged_worktree(helm_repo, "helm-lane")
        warning = cli.which_helm_warning(
            cwd=room, package_dir=os.path.join(helm_repo, "helm"))
        self.assertIsNotNone(warning, "a helm worktree at another commit is "
                                      "exactly what this surface is for")
        self.assertIn("exercised the FIRST tree", warning)
        self.assertIn(room, warning)

    def test_the_entry_script_ALONE_is_not_a_helm_checkout(self):
        """The predicate itself, at its own layer: a wrapper LAUNCHES helm, it
        is not helm's source."""
        project = self._repo("wrapper-only", helm_source=False)
        source = self._repo("real-source", helm_source=True)
        self.assertFalse(selfrepo.is_helm_source_tree(project))
        self.assertTrue(selfrepo.is_helm_source_tree(source))

    def test_still_fires_when_the_tree_holds_the_package_root_ALONE(self):
        """A HELM WORKTREE MID-REFACTOR STILL GETS THE WARNING. The predicate's
        first shape required `helm/cli.py` beside `helm/__init__.py`, so a tree
        in which cli.py is deleted, moved or renamed stopped reading as helm
        source — and the maker warning went silent in exactly the tree whose own
        import would FAIL, which is why a successful invocation of ANOTHER
        tree's binary there needs saying out loud. RED before the cure: silent.
        """
        helm_repo = self._repo("helm-mid-refactor", helm_source=True,
                               package=("__init__.py",))
        # POSITIVE CONTROL FIRST: the package ROOT is present, which is what
        # the predicate now keys on, so the absence below is a tree mid-move
        # and not an empty directory.
        self.assertTrue(
            os.path.isfile(os.path.join(helm_repo, "helm", "__init__.py")),
            "fixture: the package root the predicate keys on is there")
        self.assertFalse(
            os.path.exists(os.path.join(helm_repo, "helm", "cli.py")),
            "fixture: the module the old conjunction required is gone")
        room = self._diverged_worktree(helm_repo, "refactor-lane")
        binary = self._repo("helm-binary", helm_source=True)
        warning = cli.which_helm_warning(
            cwd=room, package_dir=os.path.join(binary, "helm"))
        self.assertIsNotNone(warning, "a helm tree missing a module is still a "
                                      "helm tree a maker is editing")
        self.assertIn("exercised the FIRST tree", warning)
        self.assertIn(room, warning)
        self.assertIn(binary, warning)

    def test_the_package_ROOT_decides_and_a_project_repo_has_none(self):
        """MUST-HIT CONTROL for the loosening. `__init__.py` IS the package —
        what `import helm` binds — so it alone answers True; a project repo
        carrying only the wrapper still answers False, which is the silence
        task/2442 exists for."""
        root_only = self._repo("root-only", helm_source=True,
                               package=("__init__.py",))
        project = self._repo("adopter", helm_source=False)
        self.assertTrue(selfrepo.is_helm_source_tree(root_only))
        self.assertFalse(selfrepo.is_helm_source_tree(project))

    def test_a_project_repo_with_no_package_at_all_stays_silent(self):
        """MUST-HIT CONTROL at the warning surface: the loosened predicate must
        not have started answering True to everyone. A lane of an adopter's
        project — wrapper and no helm package — is silent at a diverged
        commit, where the scope test is the only thing that can quiet it."""
        binary = self._repo("helm-src", helm_source=True)
        package_dir = os.path.join(binary, "helm")
        # POSITIVE CONTROL ON THE SAME OBSERVABLE, UNCONDITIONAL: the same call
        # against a diverged worktree of a HELM tree speaks. The two differ by
        # one thing — whether the tree carries helm's package — so the silence
        # below is the scope test and not a surface that stopped answering.
        source = self._repo("helm-elsewhere", helm_source=True)
        spoke = cli.which_helm_warning(
            cwd=self._diverged_worktree(source, "helm-elsewhere-lane"),
            package_dir=package_dir)
        self.assertIn("exercised the FIRST tree", spoke)
        project = self._repo("project", helm_source=False)
        room = self._diverged_worktree(project, "project-lane")
        silent = cli.which_helm_warning(cwd=room, package_dir=package_dir)
        self.assertIsNone(silent)

    def test_the_predicate_answers_about_a_repository_however_it_is_spelled(self):
        """FINDING 1 AT THE PREDICATE'S OWN LAYER. helm's stores name a
        repository by its GITDIR, and a predicate that looked for
        `<given>/helm/__init__.py` answered False about helm's own checkout when
        handed `<root>/.git` — which is what a land close hands the guard's
        drift reader. `checkout_root` normalizes first: a checkout root, a
        linked worktree root, and either one's gitdir are one repository."""
        source = self._repo("spellings-source", helm_source=True)
        room = self._diverged_worktree(source, "spellings-lane")
        gitdir = os.path.join(source, ".git")
        wt_gitdir = os.path.join(gitdir, "worktrees", os.path.basename(room))
        self.assertTrue(os.path.isdir(wt_gitdir),
                        "fixture: the linked worktree's own gitdir")
        # POSITIVE CONTROL FIRST, UNCONDITIONAL: the plain root answers True,
        # so the spellings below are about normalization and not about a
        # predicate that answers True to everything.
        self.assertTrue(selfrepo.is_helm_source_tree(source))
        for spelling in (gitdir, room, wt_gitdir):
            self.assertTrue(selfrepo.is_helm_source_tree(spelling), spelling)
        # A ROOT IS ITS OWN ANSWER, AND A GITDIR NAMES **ITS OWN** TREE. A
        # linked worktree root carries the package itself, so it normalizes to
        # itself; the main checkout's gitdir names the main checkout; and the
        # linked worktree's PRIVATE gitdir names the LINKED worktree, which is
        # the tree git records for it. Naming the main checkout there was the
        # defect — two worktrees hold different content, and a lane beside a
        # sparse shared checkout is the ordinary case in this repo.
        self.assertEqual(selfrepo.checkout_root(room), room)
        self.assertEqual(selfrepo.checkout_root(gitdir), source)
        self.assertEqual(selfrepo.checkout_root(wt_gitdir), room)

    def test_normalization_never_reaches_into_ANOTHER_repository(self):
        """MUST-HIT CONTROL. Running one clone's binary while standing in
        another is the maker mistake this whole surface exists for, so the
        normalization walks UP the path it was given and never sideways: a
        second clone's gitdir names the SECOND clone, and a project repo's
        gitdir still answers False."""
        first = self._repo("clone-one", helm_source=True)
        second = self._repo("clone-two", helm_source=True)
        project = self._repo("adopter-two", helm_source=False)
        self.assertEqual(selfrepo.checkout_root(os.path.join(second, ".git")),
                         second)
        self.assertNotEqual(
            selfrepo.checkout_root(os.path.join(second, ".git")), first)
        # POSITIVE CONTROL ON THE SAME OBSERVABLE, UNCONDITIONAL: a helm
        # clone's gitdir answers True, so False here is this repo's contents.
        self.assertTrue(selfrepo.is_helm_source_tree(
            os.path.join(first, ".git")))
        self.assertFalse(selfrepo.is_helm_source_tree(
            os.path.join(project, ".git")))
        # AND THE WARNING STILL FIRES BETWEEN THE TWO CLONES.
        warning = cli.which_helm_warning(
            cwd=self._diverged_worktree(second, "clone-two-lane"),
            package_dir=os.path.join(first, "helm"))
        self.assertIn("exercised the FIRST tree", warning)

    def test_an_unanswerable_predicate_SPEAKS(self):
        """WHICH WAY THIS SURFACE FAILS. "Not there" and "cannot look" are
        different facts; an advisory line that changes no exit code is cheaper
        than a dogfood passing against another tree's code, so an OSError out
        of the predicate is treated as a helm checkout. Driven by making the
        real seam raise, never by reconstructing its arguments."""
        with mock.patch.object(selfrepo, "is_helm_tree_a_maker_edits",
                               side_effect=OSError("cannot look")):
            self.assertTrue(cli._is_helm_checkout("/anywhere"))

    def test_a_LINKED_worktree_answers_for_ITS_OWN_content(self):
        """ROUND-THREE FINDING 2, THE HALF SHAPE-MATCHING GOT WRONG. A linked
        worktree's private gitdir was mapped to the MAIN checkout, so the
        predicate answered about a tree the identity does not name. The two
        trees hold different content whenever they are at different states —
        and this fixture makes them differ in the one file the predicate reads,
        which is the only way to prove WHICH tree answered.

        The worktree is a real one, made by `git worktree add`; the package is
        then moved out of the MAIN checkout alone."""
        source = self._repo("wt-content-source", helm_source=True)
        room = self._diverged_worktree(source, "wt-content-lane")
        wt_gitdir = os.path.join(source, ".git", "worktrees",
                                 os.path.basename(room))
        self.assertTrue(os.path.isdir(wt_gitdir),
                        "fixture: git worktree add really made a private gitdir")
        # THE TWO TREES NOW DISAGREE: the lane carries helm's package, the main
        # checkout does not. A reader that resolves to the wrong one says leak
        # about a lane that is helm's own source.
        shutil.move(os.path.join(source, "helm"),
                    os.path.join(self.tmp, "wt-content-stash"))
        self.assertFalse(os.path.exists(os.path.join(source, "helm")),
                         "fixture: the main checkout no longer holds the package")
        self.assertTrue(
            os.path.isfile(os.path.join(room, "helm", "__init__.py")),
            "fixture: the LINKED worktree does hold the package root")
        # POSITIVE CONTROL FIRST, UNCONDITIONAL, ON THE SAME READER: the main
        # checkout's OWN gitdir still names the main checkout, so the answer
        # below is which tree an identity denotes and not a reader that started
        # returning the last path it saw.
        self.assertEqual(selfrepo.checkout_root(os.path.join(source, ".git")),
                         source)
        self.assertFalse(selfrepo.is_helm_source_tree(
            os.path.join(source, ".git")))
        # THE ARM. RED before the cure: wt_gitdir resolved to `source`, whose
        # package is gone, so a lane of helm's own source read as a project repo.
        self.assertEqual(selfrepo.checkout_root(wt_gitdir), room)
        self.assertTrue(selfrepo.is_helm_source_tree(wt_gitdir))

    def _separate_gitdir(self, source, name):
        """A real `git clone --separate-git-dir` of `source`: the checkout and
        the metadata directory git put outside it."""
        tree = os.path.join(self.tmp, name + "-checkout")
        gitdir = os.path.join(self.tmp, name + "-common.git")
        out = subprocess.run(["git", "clone", "-q", "--separate-git-dir",
                              gitdir, source, tree],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(os.path.isfile(os.path.join(tree, ".git")),
                        "fixture: `.git` is a FILE pointing outside the tree")
        return tree, gitdir

    def test_a_SEPARATE_gitdir_is_answered_by_its_CHECKOUT_or_not_at_all(self):
        """ROUND-FIVE FINDING 1, AND ROUND-THREE FINDING 2 UNDER IT. The
        identity helm's stores keep for a repository is its gitdir, and for a
        separate gitdir (`git clone --separate-git-dir` — the shape
        `tests/test_dispatches.py` builds, so a supported input) the metadata
        lives outside the tree. Shape-matching looked for the package root
        INSIDE that directory, and answering it with what the repository TRACKS,
        `cat-file -e HEAD:helm/__init__.py`, answers a DIFFERENT QUESTION. This
        fixture is the disagreement that makes them two answers: HEAD carries no
        package root and the WORKING TREE does (restored, uncommitted — the
        ordinary mid-move state), so the tracked reading says "project repo"
        about a tree that is helm's own source, and the rail that tree has armed
        compares STALE.

        MEASURED with git 2.53.0: the clone leaves `core.worktree` UNSET, so the
        link is one-directional and nothing in the gitdir names its checkout —
        the same measurement `helm/obligation._root_for_repo` carries. So the
        answer is UNKNOWN, and the boolean predicates RAISE rather than
        returning a False that would arm the leak legs.

        RED before the cure: `other`, and `is_helm_source_tree` returned False.
        """
        source = self._repo("sep-mid-move", helm_source=True,
                            package=("cli.py", "seat.py"))
        tree, gitdir = self._separate_gitdir(source, "sep-mid-move")
        with open(os.path.join(tree, "helm", "__init__.py"), "w") as f:
            f.write("# restored in the working tree, not committed\n")
        # FIXTURE PRECONDITIONS, MEASURED FROM GIT ITSELF: HEAD does not carry
        # the package root, the working tree does, and the gitdir names no
        # worktree. Each one is the fact one of the arms turns on.
        self.assertNotEqual(
            subprocess.run(["git", "-C", tree, "cat-file", "-e",
                            "HEAD:helm/__init__.py"],
                           capture_output=True).returncode, 0,
            "fixture: the commit carries no package root")
        self.assertTrue(os.path.isfile(os.path.join(tree, "helm",
                                                    "__init__.py")))
        self.assertNotEqual(
            subprocess.run(["git", "-C", gitdir, "config", "--get",
                            "core.worktree"], capture_output=True).returncode,
            0, "fixture: git records no working tree for this gitdir")
        # POSITIVE CONTROL FIRST, UNCONDITIONAL, ON THE SAME READER: the
        # CHECKOUT spelling of this repository is SOURCE — the tree decides, not
        # HEAD — so the UNKNOWN below is about an identity that cannot reach a
        # tree, and not a reader that stopped recognising helm's source.
        self.assertEqual(selfrepo.repository_state(tree), selfrepo.SOURCE)
        self.assertTrue(selfrepo.is_helm_source_tree(tree))
        # THE ARM.
        self.assertEqual(selfrepo.repository_state(gitdir), selfrepo.UNKNOWN)
        self.assertRaises(selfrepo.UnreachableCheckout,
                          selfrepo.is_helm_source_tree, gitdir)
        self.assertRaises(selfrepo.UnreachableCheckout,
                          selfrepo.is_helm_tree_a_maker_edits, gitdir)
        # AND IT IS AN OSError, which is the contract both callers already
        # declared a direction for — a new hierarchy would have crashed the
        # surface that runs in front of every helm invocation.
        self.assertTrue(issubclass(selfrepo.UnreachableCheckout, OSError))

    def test_a_separate_gitdir_that_RECORDS_its_checkout_is_answered_by_it(self):
        """MUST-HIT CONTROL for the UNKNOWN above: it is about reachability, not
        about the spelling. `core.worktree` is git's own record of which tree a
        gitdir belongs to, and with it recorded the resolver reaches that tree —
        MEASURED: `rev-parse --show-toplevel` asked FROM such a gitdir prints
        the worktree. The state then comes from the CHECKOUT, which is this
        finding's invariant: the same fixture whose HEAD carries no package root
        answers SOURCE, because the tree does.

        Blast radius: one `git config` from the arm above; both fixtures are
        built by the same `git clone --separate-git-dir`."""
        source = self._repo("sep-recorded", helm_source=True,
                            package=("cli.py", "seat.py"))
        tree, gitdir = self._separate_gitdir(source, "sep-recorded")
        with open(os.path.join(tree, "helm", "__init__.py"), "w") as f:
            f.write("# restored in the working tree, not committed\n")
        self.assertEqual(selfrepo.repository_state(gitdir), selfrepo.UNKNOWN)
        self._git(gitdir, "config", "core.worktree", tree)
        self.assertEqual(selfrepo.checkout_root(gitdir), tree)
        self.assertEqual(selfrepo.repository_state(gitdir), selfrepo.SOURCE)
        self.assertTrue(selfrepo.is_helm_source_tree(gitdir))
        # AND A PROJECT REPO REACHED THE SAME WAY IS STILL OTHER, so resolving
        # this spelling did not start calling every identity helm's own source.
        project = self._repo("sep-adopter", helm_source=False)
        proj_tree, proj_gitdir = self._separate_gitdir(project, "sep-adopter")
        self._git(proj_gitdir, "config", "core.worktree", proj_tree)
        self.assertEqual(selfrepo.checkout_root(proj_gitdir), proj_tree)
        self.assertEqual(selfrepo.repository_state(proj_gitdir), selfrepo.OTHER)
        self.assertFalse(selfrepo.is_helm_source_tree(proj_gitdir))

    def test_GIT_DIR_in_the_environment_decides_nothing(self):
        """ROUND-THREE FINDING 2, THE OTHER WAY A GIT DIRECTORY GETS RESOLVED
        WRONG. `GIT_DIR` exported in the environment OVERRIDES `git -C <path>`
        — MEASURED — and helm runs inside hooks, gates and land closes, every
        one of which exports it. An unscrubbed question therefore answers about
        whichever repository the caller's environment last named.

        The control is the SAME call with the scrub removed, which is the only
        way to show the scrub is load-bearing rather than decoration."""
        source = self._repo("env-source", helm_source=True)
        project = self._repo("env-adopter", helm_source=False)
        gitdir = os.path.join(source, ".git")
        with mock.patch.dict(os.environ,
                             {"GIT_DIR": os.path.join(project, ".git")}):
            # THE ARM: the identity given decides, not the environment.
            self.assertEqual(selfrepo.checkout_root(gitdir), source)
            self.assertTrue(selfrepo.is_helm_source_tree(gitdir))
            # MUST-HIT CONTROL on the same call with the scrub disabled. Blast
            # radius: `selfrepo._git_env` only, restored when the patch exits,
            # so no other arm in this suite can see it.
            with mock.patch.object(selfrepo, "_git_env",
                                   side_effect=lambda: dict(os.environ)):
                self.assertNotEqual(selfrepo.checkout_root(gitdir), source)

    def test_every_git_question_this_classifier_asks_goes_through_the_seam(self):  # noqa: VACUOUS_ASSERTION — the empty-calls assertion is itself a control on the spy, and the observable's unconditional positive is the assertTrue on the SAME list after the resolver is asked a spelling only git can answer
        """The gate audit (`tests/test_vcs.DirectSpawnAuditTest`) pins which
        modules still spawn git directly, and this classifier is not one of
        them: both verbs it asks — `rev-parse` and `cat-file` — and the env
        overlay that UNSETS `GIT_DIR` are things `helm/vcs.py` already
        expresses, so the question is asked there.

        The spy WRAPS the real seam method, so the answers below are git's own
        and not a reconstruction of the argv."""
        from helm import vcs
        source = self._repo("seam-source", helm_source=True)
        calls = []
        real = vcs.GitVcs.text

        def spy(backend, cwd, *args, **kw):
            calls.append((cwd, args, kw.get("env")))
            return real(backend, cwd, *args, **kw)

        # POSITIVE CONTROL FIRST, UNCONDITIONAL: a root carrying `.git` is
        # answered by stats alone, which is the cost claim in front of every
        # helm invocation — so a seam call recorded here would be a regression
        # and zero of them proves the spy is not counting unrelated traffic.
        # Blast radius: `vcs.GitVcs.text` for the duration of this arm only.
        with mock.patch.object(vcs.GitVcs, "text", spy):
            self.assertEqual(selfrepo.checkout_root(source), source)
            self.assertEqual(calls, [], "the stats fast path asked git nothing")
            # THE ARM. A path INSIDE the checkout is a spelling only git can
            # resolve. RED before the cure: the answer was identical and the
            # seam saw NOTHING, because this module spawned git itself.
            inside = os.path.join(source, "helm")
            self.assertEqual(selfrepo.checkout_root(inside), source)
        self.assertTrue(calls, "the resolver's git question never reached the "
                               "vcs seam")
        scrub = {name: None for name in selfrepo._GIT_ENV_OVERRIDES}
        for cwd, args, env in calls:
            self.assertEqual(env, scrub, args)
            self.assertNotIn("-C", args, "the seam takes the cwd as its first "
                                         "argument, so `-C` here would ask "
                                         "about a directory twice: %r" % (args,))

    def test_a_DELETED_package_root_still_gets_the_warning(self):
        """ROUND-THREE FINDING 3. Delete or move the tracked `helm/__init__.py`
        in a real helm worktree — `cli.py` and the rest still under `helm/` —
        and the narrow predicate answered False, so this warning went SILENT in
        the one tree whose own `import helm` cannot succeed. The command that
        DID work therefore ran another tree's code and nothing said so. The
        absence of the package root in a tree that still carries the package is
        a damaged helm checkout, never an adopter's project."""
        damaged = self._repo("damaged-source", helm_source=True)
        binary = self._repo("damaged-binary", helm_source=True)
        os.unlink(os.path.join(damaged, "helm", "__init__.py"))
        # POSITIVE CONTROL FIRST, UNCONDITIONAL: the modules are still there
        # and git SEES the tracked deletion, so this is a maker's tree mid-move
        # and not an empty directory.
        self.assertTrue(os.path.isfile(os.path.join(damaged, "helm", "cli.py")),
                        "fixture: the package's modules are still here")
        status = self._git(damaged, "status", "--porcelain", "--", "helm")
        self.assertIn("helm/__init__.py", status.stdout,
                      "fixture: the deletion is of a TRACKED file")
        # THE ARM. RED before the cure: None.
        warning = cli.which_helm_warning(
            cwd=damaged, package_dir=os.path.join(binary, "helm"))
        self.assertIsNotNone(warning, "the tree whose import FAILS is exactly "
                                      "the tree that cannot have served this")
        self.assertIn("exercised the FIRST tree", warning)
        self.assertIn(damaged, warning)
        # AND THE GUARD KEEPS THE NARROW READING. The two surfaces fail in
        # opposite directions on purpose: arming helm's shared-checkout rail in
        # a repo that may be an adopter's is the worse failure there.
        self.assertTrue(selfrepo.is_helm_tree_a_maker_edits(damaged))
        self.assertFalse(selfrepo.is_helm_source_tree(damaged))
        self.assertEqual(selfrepo.repository_state(damaged), selfrepo.DAMAGED)

    def test_an_adopter_repo_with_no_package_DIRECTORY_is_still_silent(self):
        """MUST-HIT CONTROL for the arm above, and the one that keeps task/2442
        cured. The loosening is "the package root is missing from a tree that
        still holds the package" — an adopter repo holds no `helm/` directory at
        all, so it is OTHER in one listdir and this surface stays quiet.

        Blast radius: this arm shares only `_repo` with the positive above; the
        single difference between the two fixtures is whether `helm/` exists."""
        project = self._repo("no-package-dir", helm_source=False)
        binary = self._repo("no-package-binary", helm_source=True)
        self.assertFalse(os.path.exists(os.path.join(project, "helm")),
                         "fixture: no helm package directory whatsoever")
        self.assertTrue(os.path.isfile(os.path.join(project, "bin", "helm")),
                        "fixture: the wrapper that used to trigger it IS there")
        self.assertEqual(selfrepo.repository_state(project), selfrepo.OTHER)
        self.assertFalse(selfrepo.is_helm_tree_a_maker_edits(project))
        self.assertIsNone(cli.which_helm_warning(
            cwd=project, package_dir=os.path.join(binary, "helm")))

    def test_an_EMPTY_package_directory_is_not_a_damaged_checkout(self):
        """MUST-HIT CONTROL on the discriminator itself. DAMAGED is "the modules
        are still here"; a bare `helm/` directory with no module in it is
        evidence of nothing, and reading it as helm source would make any
        project that happens to own that directory name a maker's tree.

        Blast radius: one `mkdir` from the positive above."""
        project = self._repo("empty-package-dir", helm_source=False)
        os.makedirs(os.path.join(project, "helm"))
        with open(os.path.join(project, "helm", "NOTES.md"), "w") as f:
            f.write("not a module\n")
        self.assertEqual(selfrepo.repository_state(project), selfrepo.OTHER)
        # POSITIVE CONTROL ON THE SAME READER, UNCONDITIONAL — and it is not a
        # fabricated module any more. What makes that directory helm's package
        # with its root missing is HELM'S OWN modules in it, so this copies the
        # running package's real bytes. The old control wrote a one-line file
        # named `cli.py`, which PINNED the overbroad predicate it was meant to
        # bound: it asserted that any project's `helm/cli.py` is a maker's tree.
        for mod in ("cli.py", "seat.py"):
            shutil.copy2(os.path.join(REPO, "helm", mod),
                         os.path.join(project, "helm", mod))
        self.assertEqual(selfrepo.repository_state(project), selfrepo.DAMAGED)

    def test_an_adopters_OWN_helm_directory_is_not_a_damaged_checkout(self):
        """ROUND-FIVE FINDING 3. `helm` is one of the most-taken directory names
        there is, and "any `.py` under `helm/`" convicted an adopter's own chart
        directory of being a damaged helm checkout: `helm/Chart.yaml` beside
        `helm/render.py`, no package root, no helm CLI in the tree at all. The
        maker-only wrong-tree warning then fired there on every command and
        advised a `./bin/helm` that does not exist — the task/2442 failure
        through a new door, and main was SILENT about this tree. RED before the
        cure: DAMAGED, and a warning.

        A MODULE NAME IS NOT OWNERSHIP EITHER, so this adopter also has its own
        `helm/cli.py`: `cli.py` is a name any project's package can hold, and a
        predicate keyed on one name would have convicted this tree too."""
        project = self._repo("adopter-chart", helm_source=False)
        os.makedirs(os.path.join(project, "helm"))
        for rel, body in (("Chart.yaml", "name: adopter\n"),
                          ("render.py", "print('chart')\n"),
                          ("cli.py", "# the adopter's OWN cli\n")):
            with open(os.path.join(project, "helm", rel), "w") as f:
                f.write(body)
        # FIXTURE PRECONDITIONS: the tree really does carry the wrapper (the
        # thing task/2442 was about) and a `.py` under `helm/` (what the old
        # predicate keyed on), so the silence below is the provenance test.
        self.assertTrue(os.path.isfile(os.path.join(project, "bin", "helm")))
        self.assertTrue(os.path.isfile(os.path.join(project, "helm",
                                                   "render.py")))
        binary = self._repo("adopter-chart-binary", helm_source=True)
        package_dir = os.path.join(binary, "helm")
        self.assertEqual(selfrepo.repository_state(project), selfrepo.OTHER)
        self.assertFalse(selfrepo.is_helm_tree_a_maker_edits(project))
        self.assertIsNone(cli.which_helm_warning(cwd=project,
                                                 package_dir=package_dir))
        # MUST-HIT CONTROL ON THE SAME FIXTURE AND THE SAME CALL: put helm's OWN
        # modules — the running package's real bytes — into that same directory
        # and both the state and the warning come back. So the silence above is
        # provenance and not a surface that stopped answering, and the cure did
        # not silence the damaged-checkout case it had to keep. Blast radius:
        # this arm's own fixture; two files copied over two of its own.
        for mod in ("cli.py", "seat.py"):
            shutil.copy2(os.path.join(REPO, "helm", mod),
                         os.path.join(project, "helm", mod))
        self.assertEqual(selfrepo.repository_state(project), selfrepo.DAMAGED)
        self.assertIsNotNone(cli.which_helm_warning(cwd=project,
                                                    package_dir=package_dir))


class StaleTreeLineTest(unittest.TestCase):
    """THE BINARY'S OWN TREE, BEHIND TRUNK (task/3024).

    `which_helm_warning` is silent when the cwd is inside the binary's own
    tree, which is right for the question it asks. A seat ran `./bin/helm`
    from such a tree 238 commits behind trunk: its fold could not read an
    event kind trunk had since added, and the verdicts it wrote collided with
    that event's seq and were dropped. `stale_tree_warning` is the line that
    was missing, and the SILENT cells are pinned as hard as the speaking one,
    for the reason `SameCommitIsSilentTest` gives.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-stale-tree-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # `create=True` so a binary without the line still reaches each arm's
        # own assertion (the end-to-end one is then red on what it prints)
        # instead of every arm erroring here.
        said = mock.patch.object(cli, "_STALE_TREE_SAID", [], create=True)
        said.start()
        self.addCleanup(said.stop)
        self.root = os.path.join(self.tmp, "bintree")
        os.makedirs(os.path.join(self.root, "bin"))
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "t@t")
        self._git("config", "user.name", "t")
        shutil.copytree(os.path.join(REPO, "helm"),
                        os.path.join(self.root, "helm"))
        shutil.copy2(os.path.join(REPO, "bin", "helm"),
                     os.path.join(self.root, "bin", "helm"))
        self.base = self._commit("base")

    def _git(self, *args):
        out = subprocess.run(["git", "-C", self.root] + list(args),
                             capture_output=True, text=True, check=True)
        return out.stdout.strip()

    def _commit(self, name):
        with open(os.path.join(self.root, name), "w") as f:
            f.write(name + "\n")
        self._git("add", "-A")
        self._git("commit", "-qm", name)
        return self._git("rev-parse", "HEAD")

    def _trunk_ahead_by(self, n):
        """origin/main n commits past HEAD, which stays where it is — exactly a
        seat tree nobody fast-forwarded."""
        self._git("checkout", "-q", "-b", "trunk")
        for i in range(n):
            tip = self._commit("trunk-%d" % i)
        self._git("update-ref", "refs/remotes/origin/main", tip)
        self._git("checkout", "-q", "main")

    def _line(self, cwd=None):
        return cli.stale_tree_warning(
            cwd=cwd or self.root, package_dir=os.path.join(self.root, "helm"))

    def test_a_tree_behind_trunk_names_itself_the_distance_and_the_cure(self):
        self._trunk_ahead_by(3)
        line = self._line(cwd=os.path.join(self.root, "helm"))
        self.assertIsNotNone(line, "a tree behind trunk said nothing")
        self.assertIn(self.root, line)
        self.assertIn("3 commits behind origin/main", line)
        self.assertIn("git -C %s merge --ff-only origin/main" % self.root, line)
        self.assertEqual(len(line.splitlines()), 1, "one line, not a paragraph")

    def test_a_lane_with_its_own_commits_is_told_to_rebase(self):
        self._trunk_ahead_by(1)
        self._commit("lane-work")
        line = self._line()
        self.assertIn("1 commit behind origin/main", line)
        self.assertIn("1 commit of its own, so rebase it", line)
        self.assertNotIn("--ff-only", line, "a fast-forward cannot apply")

    def _speaks_first(self):
        """THE CONTROL EVERY SILENT CELL RUNS FIRST: this tree, behind trunk,
        speaks — so the silence that follows is the state, not a function
        that stopped answering. The once-per-process memo is then cleared."""
        self._trunk_ahead_by(1)
        self.assertIsNotNone(self._line(), "behind trunk said nothing")
        cli._STALE_TREE_SAID.clear()

    def test_at_trunk_is_silent(self):  # noqa: VACUOUS_ASSERTION — _speaks_first drives this same tree to a line, unconditionally, before the state that silences it is set
        self._speaks_first()
        self._git("update-ref", "refs/remotes/origin/main", self.base)
        self.assertIsNone(self._line())

    def test_ahead_of_trunk_is_silent(self):  # noqa: VACUOUS_ASSERTION — _speaks_first drives this same tree to a line, unconditionally, before the state that silences it is set
        self._speaks_first()
        self._git("merge", "-q", "--ff-only", "refs/remotes/origin/main")
        self._commit("ahead")
        self.assertIsNone(self._line())

    def test_no_trunk_ref_is_silent(self):  # noqa: VACUOUS_ASSERTION — _speaks_first drives this same tree to a line, unconditionally, before the state that silences it is set
        """A checkout with no origin/main cannot be measured, so nothing is
        claimed about it."""
        self._speaks_first()
        self._git("update-ref", "-d", "refs/remotes/origin/main")
        self.assertIsNone(self._line())

    def test_outside_the_binarys_tree_is_silent_and_asks_git_nothing(self):  # noqa: VACUOUS_ASSERTION — _speaks_first drives this same tree to a line, unconditionally, before the state that silences it is set
        self._speaks_first()
        plain = os.path.join(self.tmp, "not-a-repo")
        os.makedirs(plain)
        with mock.patch.object(cli, "_trunk_distance",
                               side_effect=AssertionError("asked git")):
            self.assertIsNone(self._line(cwd=plain))

    def test_it_speaks_at_most_once_per_process(self):
        self._trunk_ahead_by(1)
        self.assertIsNotNone(self._line())
        self.assertIsNone(self._line(), "a second line in one process")

    def test_the_gate_still_reads_this_tree_as_its_own(self):  # noqa: VACUOUS_ASSERTION — the control is the stale line speaking on the same tree, so the tree IS behind; which_helm_warning's own speaking cells are TreeWarningTest's
        """`gate._cross_tree_refusal` reads `which_helm_warning`'s truthiness
        as "another tree", so the stale line must not become its return: a
        lane behind trunk still gates its own tip."""
        self._trunk_ahead_by(2)
        self.assertIsNone(cli.which_helm_warning(
            cwd=self.root, package_dir=os.path.join(self.root, "helm")))
        self.assertIsNotNone(self._line(), "the control: this tree IS behind")

    def test_end_to_end_the_command_warns_and_still_works(self):  # noqa: VACUOUS_ASSERTION — the line is asserted PRESENT on the same command's stderr, unconditionally, before the hatch is shown to remove it
        """THROUGH THE REAL ENTRY SCRIPT, from inside the stale tree: the line
        is on stderr, the command's output and exit code are untouched, and
        the hook escape hatch silences it."""
        self._trunk_ahead_by(2)
        run = functools.partial(
            subprocess.run, [os.path.join(self.root, "bin", "helm"),
                             "--version"],
            cwd=self.root, capture_output=True, text=True, timeout=60)
        out = run()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("helm ", out.stdout)
        self.assertIn("2 commits behind origin/main", out.stderr)
        quiet = run(env=dict(os.environ, HELM_NO_TREE_WARNING="1"))
        self.assertEqual(quiet.returncode, 0, quiet.stderr)
        self.assertNotIn("behind origin/main", quiet.stderr)
