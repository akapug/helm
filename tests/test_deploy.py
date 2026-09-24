#!/usr/bin/env python3
"""Focused witnesses for the immutable tracked-tree artifact deployer."""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[1]
DEPLOYER = REPO / "scripts" / "deploy.py"
MANIFEST = "artifact-manifest.json"

CLI = """import json
import os
import sys

from helm import __version__

SMOKE_OK = {smoke_ok}


def report(value):
    print(json.dumps({{
        "value": value,
        "argv0": sys.argv[0],
        "cli_file": __file__,
        "path": sys.path,
    }}, sort_keys=True))


def main():
    args = sys.argv[1:]
    if args == ["--version"]:
        if not SMOKE_OK:
            print("deliberate smoke failure", file=sys.stderr)
            return 9
        print("helm " + __version__)
        return 0
    if args == ["probe"]:
        report("probe")
        return 0
    if args == ["lazy-now"]:
        from helm.lazy import VALUE
        report(VALUE)
        return 0
    if args == ["lazy-wait"]:
        print("ready", flush=True)
        sys.stdin.readline()
        from helm.lazy import VALUE
        report(VALUE)
        return 0
    print("unknown test command", file=sys.stderr)
    return 2
"""


def _load_deployer():
    """Import scripts/deploy.py as a module.

    It is a standalone script rather than part of the package (deployment must
    work when the mutable checkout does not import), so there is no ordinary
    import path to it.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("_helm_deployer", DEPLOYER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DeployFixture(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.repo = self.base / "source"
        self.repo.mkdir()
        self._git("init")
        self._git("config", "user.email", "test@example.invalid")
        self._git("config", "user.name", "test")
        self._write_static_tree()
        self._write_runtime("A")
        self.commit_a = self._commit("artifact A")
        self.tree_a = self._git("rev-parse", self.commit_a + "^{tree}").stdout.strip()

    def tearDown(self):
        self.temp.cleanup()

    def _git(self, *args, check=True):
        return subprocess.run(
            ["git", "-C", str(self.repo)] + list(args),
            check=check,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _write(self, name, data, mode=None):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data, encoding="utf-8")
        if mode is not None:
            path.chmod(mode)
        return path

    def _write_static_tree(self):
        scripts = self.repo / "scripts"
        scripts.mkdir()
        shutil.copy2(DEPLOYER, scripts / "deploy.py")
        bin_dir = self.repo / "bin"
        bin_dir.mkdir()
        shutil.copy2(REPO / "bin" / "helm", bin_dir / "helm")
        # THE SECOND ENTRY POINT. Every generated hook command in every
        # settings file execs this wrapper, so an artifact without it fails
        # OPEN on every hook in the estate; the deployer requires it and
        # mode-checks it, and this fixture must therefore ship it.
        shutil.copy2(REPO / "bin" / "helm-hook", bin_dir / "helm-hook")
        self._write("README.md", "committed README\n")
        os.symlink("README.md", self.repo / "README-link")
        self._write("docs/VERBS.md", "# verbs\n")
        self._write("tests/test_cli_help.py", "# committed test witness\n")
        self._write(".gitignore", "ignored.txt\n")

    def _write_runtime(self, version, smoke_ok=True):
        self._write("helm/__init__.py", "__version__ = %r\n" % version)
        self._write("helm/cli.py", CLI.format(smoke_ok=repr(smoke_ok)))
        self._write("helm/lazy.py", "VALUE = %r\n" % version)

    def _commit(self, message):
        self._git("add", "-A")
        self._git("commit", "-m", message)
        return self._git("rev-parse", "HEAD").stdout.strip()

    def _commit_runtime(self, version, smoke_ok=True):
        self._write_runtime(version, smoke_ok=smoke_ok)
        return self._commit("artifact " + version)

    def _deploy(self, root, revision=None, dry_run=False, check=False,
                umask=None):
        args = [sys.executable, str(self.repo / "scripts" / "deploy.py")]
        if revision is not None:
            args.append(revision)
        args.extend(["--root", str(root)])
        if dry_run:
            args.append("--dry-run")
        previous_umask = None if umask is None else os.umask(umask)
        try:
            return subprocess.run(
                args,
                check=check,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        finally:
            if previous_umask is not None:
                os.umask(previous_umask)

    def _launcher(self, root, *args, check=False):
        return subprocess.run(
            [str(root / "bin" / "helm")] + list(args),
            check=check,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _release(self, root, commit):
        return root / "releases" / commit

    def _snapshot(self, root):
        root = Path(root)
        if not root.exists() and not root.is_symlink():
            return None
        out = {}
        pending = [root]
        while pending:
            directory = pending.pop()
            for path in sorted(directory.iterdir(), key=lambda item: item.name):
                rel = str(path.relative_to(root))
                mode = path.lstat().st_mode
                if stat.S_ISLNK(mode):
                    out[rel] = ("link", stat.S_IMODE(mode), os.readlink(path))
                elif stat.S_ISDIR(mode):
                    out[rel] = ("dir", stat.S_IMODE(mode), None)
                    pending.append(path)
                elif stat.S_ISREG(mode):
                    out[rel] = ("file", stat.S_IMODE(mode), path.read_bytes())
                else:
                    out[rel] = ("other", stat.S_IMODE(mode), None)
        out["."] = ("dir", stat.S_IMODE(root.lstat().st_mode), None)
        return out


class SourceCommitIsolationTests(DeployFixture):

    def test_archive_uses_the_selected_commit_and_excludes_untracked_dirt(self):
        commit_b = self._commit_runtime("B")
        self._write("untracked.txt", "not committed\n")
        self._write("ignored.txt", "also not committed\n")
        root = self.base / "artifacts"

        result = self._deploy(root, revision=self.commit_a)

        self.assertEqual(result.returncode, 0, result.stderr)
        release = self._release(root, self.commit_a)
        self.assertEqual((release / "README.md").read_text(), "committed README\n")
        self.assertFalse((release / "untracked.txt").exists())
        self.assertFalse((release / "ignored.txt").exists())
        self.assertFalse(self._release(root, commit_b).exists())
        manifest = json.loads((release / MANIFEST).read_text())
        self.assertEqual(manifest, {
            "schema": 1,
            "commit": self.commit_a,
            "tree": self.tree_a,
            "package_version": "A",
            "content_digest": manifest["content_digest"],
        })
        self.assertRegex(manifest["content_digest"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(os.readlink(root / "current"),
                         "releases/" + self.commit_a)
        self.assertTrue(stat.S_ISREG((root / "bin" / "helm").lstat().st_mode))

    def test_staged_and_unstaged_tracked_changes_are_refused(self):
        for label, stage in (("unstaged", False), ("staged", True)):
            with self.subTest(label=label):
                root = self.base / ("artifacts-" + label)
                self._write("README.md", label + " dirt\n")
                if stage:
                    self._git("add", "README.md")

                result = self._deploy(root)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(label + " tracked source changes", result.stderr)
                self.assertFalse(root.exists())
                self._git("reset", "--hard", "HEAD")

    def test_escaping_archive_symlink_is_refused_before_staging(self):
        root = self.base / "artifacts"
        self.assertEqual(self._deploy(root).returncode, 0)
        selected = os.readlink(root / "current")
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "sentinel").write_text("safe\n")
        os.symlink("../outside", self.repo / "escape")
        bad = self._commit("escaping symlink")

        result = self._deploy(root)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlink escapes artifact", result.stderr)
        self.assertEqual(os.readlink(root / "current"), selected)
        self.assertFalse(self._release(root, bad).exists())
        self.assertEqual((outside / "sentinel").read_text(), "safe\n")


class PublicationIntegrityTests(DeployFixture):

    def test_redeploying_identical_content_reuses_every_published_node(self):
        root = self.base / "artifacts"
        first = self._deploy(root)
        self.assertEqual(first.returncode, 0, first.stderr)
        release = self._release(root, self.commit_a)
        release_inode = release.stat().st_ino
        launcher_inode = (root / "bin" / "helm").stat().st_ino
        manifest = (release / MANIFEST).read_bytes()

        second = self._deploy(root)

        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("release=reused launcher=reused current=reused",
                      second.stdout)
        self.assertEqual(release.stat().st_ino, release_inode)
        self.assertEqual((root / "bin" / "helm").stat().st_ino,
                         launcher_inode)
        self.assertEqual((release / MANIFEST).read_bytes(), manifest)

    def test_digest_is_deterministic_and_binds_modes_bytes_and_link_targets(self):
        roots = [self.base / "digest-one", self.base / "digest-two"]
        for root in roots:
            result = self._deploy(root)
            self.assertEqual(result.returncode, 0, result.stderr)
        manifests = [json.loads((self._release(root, self.commit_a) /
                                 MANIFEST).read_text()) for root in roots]
        self.assertEqual(manifests[0]["content_digest"],
                         manifests[1]["content_digest"])

        cases = ("mode", "bytes", "link")
        for index, kind in enumerate(cases):
            with self.subTest(kind=kind):
                root = self.base / ("digest-tamper-" + kind)
                self.assertEqual(self._deploy(root).returncode, 0)
                release = self._release(root, self.commit_a)
                if kind == "mode":
                    (release / "README.md").chmod(0o555)
                elif kind == "bytes":
                    path = release / "README.md"
                    path.chmod(0o644)
                    path.write_text("changed bytes %d\n" % index)
                    path.chmod(0o444)
                else:
                    release.chmod(0o755)
                    (release / "README-link").unlink()
                    os.symlink("docs/VERBS.md", release / "README-link")
                    release.chmod(0o555)

                result = self._deploy(root)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("release digest differs", result.stderr)

    def test_tampered_same_commit_manifest_bytes_or_mode_are_refused(self):
        for kind in ("bytes", "mode"):
            with self.subTest(kind=kind):
                root = self.base / ("artifacts-manifest-" + kind)
                self.assertEqual(self._deploy(root).returncode, 0)
                selected = os.readlink(root / "current")
                path = self._release(root, self.commit_a) / MANIFEST
                if kind == "bytes":
                    path.chmod(0o644)
                    data = json.loads(path.read_text())
                    path.write_text(json.dumps(data, indent=2, sort_keys=True) +
                                    "\n")
                    path.chmod(0o444)
                else:
                    path.chmod(0o555)

                result = self._deploy(root)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("existing same-commit release manifest differs",
                              result.stderr)
                self.assertEqual(os.readlink(root / "current"), selected)

    def test_failed_smoke_cleans_staging_and_leaves_current_unchanged(self):
        root = self.base / "artifacts"
        self.assertEqual(self._deploy(root).returncode, 0)
        selected = os.readlink(root / "current")
        bad = self._commit_runtime("BAD", smoke_ok=False)

        result = self._deploy(root)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("staged bin/helm --version failed", result.stderr)
        self.assertEqual(os.readlink(root / "current"), selected)
        self.assertFalse(self._release(root, bad).exists())
        staging = [path.name for path in (root / "releases").iterdir()
                   if path.name.startswith(".staging-")]
        self.assertEqual(staging, [])
        live = self._launcher(root, "--version")
        self.assertEqual(live.stdout.strip(), "helm A")

    def test_release_modes_are_read_only_but_directories_and_entrypoint_work(self):
        root = self.base / "artifacts"
        result = self._deploy(root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("release=", result.stdout)
        release = self._release(root, self.commit_a)

        for path in [release] + list(release.rglob("*")):
            mode = path.lstat().st_mode
            if not stat.S_ISLNK(mode):
                self.assertFalse(mode & 0o222, str(path))  # noqa: VACUOUS_ASSERTION — the sweep's own control is the populated tree asserted just below: bin/ is a directory and both entry points are there and executable, so the loop had nodes to inspect
        self.assertTrue((release / "bin").is_dir())
        self.assertTrue((release / "bin" / "helm").stat().st_mode & 0o111)
        self.assertTrue((release / "bin" / "helm-hook").stat().st_mode & 0o111,
                        "published bin/helm-hook is not executable, so every "
                        "hook in the estate would exit 126")
        live = self._launcher(root, "--version")
        self.assertEqual(live.returncode, 0, live.stderr)
        self.assertEqual(live.stdout.strip(), "helm A")

    def test_restrictive_umask_does_not_change_canonical_artifact_modes(self):
        root = self.base / "artifacts-umask"

        result = self._deploy(root, umask=0o077)

        self.assertEqual(result.returncode, 0, result.stderr)
        release = self._release(root, self.commit_a)
        self.assertEqual(stat.S_IMODE(release.stat().st_mode), 0o555)
        self.assertEqual(stat.S_IMODE((release / "docs").stat().st_mode), 0o555)
        self.assertEqual(stat.S_IMODE((release / "README.md").stat().st_mode),
                         0o444)
        live = self._launcher(root, "--version")
        self.assertEqual(live.returncode, 0, live.stderr)
        self.assertEqual(live.stdout.strip(), "helm A")


class HookWrapperIsARequiredEntryPointTests(DeployFixture):
    """`bin/helm-hook` ships, executable, or the deploy refuses.

    THE WRAPPER IS LOAD-BEARING FOR EVERY HOOK IN THE ESTATE. Each generated
    hook command in each settings file execs it, and it cannot announce its
    own absence: the shell exits 127 before the first line of it runs, so the
    harness reports a hook error in its own words, reads ALLOW, and the whole
    fleet is silently unguarded. `bin/helm` was in the required set and in two
    explicit mode assertions; the file that runs every one of its hooks was in
    neither, and `git grep -n helm-hook -- scripts` returned nothing at all.

    THE ARCHIVE CHECK AND THE STAGED CHECK ARE TWO DOORS. A deploy reaches the
    archive one first, so an end-to-end arm can only ever prove that half;
    the staged belt is therefore driven directly at `_verify_required`, which
    is the check a corrupted staging directory has to fail.
    """

    STAGED = {
        "bin/helm": ("#!/bin/sh\nexit 0\n", 0o755),
        "bin/helm-hook": ("#!/bin/sh\nexit 0\n", 0o755),
        "helm/__init__.py": ("__version__ = 'A'\n", 0o644),
        "helm/cli.py": ("def main():\n    return 0\n", 0o644),
        "README.md": ("staged\n", 0o644),
        "docs/VERBS.md": ("# verbs\n", 0o644),
        "tests/test_cli_help.py": ("# witness\n", 0o644),
    }

    def _staged(self):
        """A staging directory shaped like one `_verify_required` accepts."""
        root = self.base / "staged"
        for name, (data, mode) in self.STAGED.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(data, encoding="utf-8")
            path.chmod(mode)
        return root

    def test_an_artifact_missing_the_hook_wrapper_is_refused(self):
        # CONTROL, through the same reader the refusal is read from: this very
        # tree deploys, so the rc below is about the wrapper and not about a
        # fixture that could never have succeeded.
        result = self._deploy(self.base / "control")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("release=", result.stdout)

        self._git("rm", "-q", "bin/helm-hook")
        self._commit("drop the wrapper")

        result = self._deploy(self.base / "artifacts")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("archive is missing required regular file bin/helm-hook",
                      result.stderr)

    def test_a_non_executable_hook_wrapper_is_refused_in_the_archive(self):
        result = self._deploy(self.base / "control")       # CONTROL
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("release=", result.stdout)

        (self.repo / "bin" / "helm-hook").chmod(0o644)
        self._commit("unexecutable wrapper")
        self.assertEqual(
            self._git("ls-files", "-s", "bin/helm-hook").stdout.split()[0],
            "100644", "MUST-HIT: the index still records mode 100755, so the "
                      "archive the deployer reads is unchanged")

        result = self._deploy(self.base / "artifacts")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("archive entry bin/helm-hook is not executable",
                      result.stderr)

    def test_the_staged_mode_check_names_the_hook_wrapper_too(self):
        deploy = _load_deployer()
        root = self._staged()
        deploy._verify_required(str(root))     # CONTROL: this tree passes

        (root / "bin" / "helm-hook").chmod(0o644)

        with self.assertRaises(deploy.DeployError) as caught:
            deploy._verify_required(str(root))
        self.assertIn("staged bin/helm-hook is not executable",
                      str(caught.exception))

    def test_the_required_set_and_the_mode_set_both_name_the_wrapper(self):
        """DERIVED, NOT TRANSCRIBED: the registries themselves, because an
        entry point that ships but is never mode-checked is exactly the state
        `bin/helm-hook` was already in."""
        deploy = _load_deployer()
        self.assertIn("bin/helm-hook", deploy.REQUIRED_FILES)
        self.assertIn("bin/helm-hook", deploy.EXECUTABLE_FILES)
        for name in deploy.EXECUTABLE_FILES:
            self.assertIn(name, deploy.REQUIRED_FILES,
                          "%s is mode-checked but not required, so an archive "
                          "without it never reaches the check" % name)


class ProjectTargetModeTests(DeployFixture):
    """`--project` lands the artifact inside the project whose identity it binds.

    THE MODE EXISTS BECAUSE IDENTITY FOLLOWS THE PACKAGE, NOT THE CWD.
    `dispatches.home_repo_id` resolves from the running package's own
    location, so a helm deployed under `<project>/.cache` *is* that project:
    its dispatches land locally and a sibling repository is refused by the
    ordinary foreign-repo law. These arms cover the deploy half of that seam;
    the dispatch half is `ProjectLocalRuntimeBindsIdentityE2E` in
    tests/test_dispatches.py, which drives the REAL resolver off a REAL
    deployed path rather than pinning it to a lambda.
    """

    def _project(self, name, ignore=".cache/\n"):
        """A second git working tree standing in for a consuming project."""
        project = self.base / name
        project.mkdir()
        subprocess.run(["git", "-C", str(project), "init", "-q"], check=True)
        if ignore is not None:
            (project / ".gitignore").write_text(ignore, encoding="utf-8")
        return project

    def _deploy_project(self, project, *extra, env=None):
        run_env = None
        if env is not None:
            run_env = os.environ.copy()
            run_env.update(env)
        return subprocess.run(
            [sys.executable, str(self.repo / "scripts" / "deploy.py"),
             "--project", str(project)] + list(extra),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=run_env)

    def test_hostile_git_selection_env_cannot_redirect_the_project(self):
        """GIT_DIR/GIT_WORK_TREE OVERRIDE `-C`, SO THEY COULD AIM `--project A`
        AT REPOSITORY B — with no flag involved, defeating the one invariant
        this mode exists to hold.

        Found in review, and it is the whole reason `_git_env` exists in this
        standalone file: `dispatches._repo_info` already scrubbed these before
        resolving identity, and the deployer did not.

        THE SIBLING IS DELIBERATELY DEPLOYABLE. B gets its own ignore rule, so
        a successful redirect would LAND there rather than be refused — if B
        could not accept a deploy, this arm would pass on a refusal that had
        nothing to do with the cure.
        """
        project = self._project("consumer")
        sibling = self._project("sibling")
        hostile = {"GIT_DIR": str(sibling / ".git"),
                   "GIT_WORK_TREE": str(sibling)}

        # THE CONTROL, without which this arm proves nothing: confirm the
        # environment really does redirect an unscrubbed git away from A.
        redirected = subprocess.run(
            ["git", "-C", str(project), "rev-parse", "--show-toplevel"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, **hostile})
        self.assertEqual(redirected.returncode, 0, redirected.stderr)
        self.assertEqual(Path(redirected.stdout.strip()).resolve(),
                         sibling.resolve(),
                         "the hostile env does not actually redirect git here, "
                         "so this arm cannot detect the defect it exists for")

        proc = self._deploy_project(project, env=hostile)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((project / ".cache" / "helm-artifacts" / "current").exists(),
                        "the deploy did not land in the requested project")
        self.assertFalse((sibling / ".cache").exists(),
                         "GIT_DIR redirected the deploy into a sibling "
                         "repository — the mode's invariant is bypassable")

    def test_an_ignored_cache_receives_the_artifact_under_the_project(self):
        project = self._project("consumer")
        proc = self._deploy_project(project)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.strip(),
                        "the deployer exited 0 while reporting nothing, which "
                        "a no-op would also do")
        root = project / ".cache" / "helm-artifacts"
        self.assertTrue((root / "bin" / "helm").exists(),
                        "no launcher under the project-local root")
        release = self._release(root, self.commit_a)
        self.assertTrue((release / "helm" / "__init__.py").exists(),
                        "the deployed package is not under the project")
        self.assertEqual(os.readlink(root / "current"),
                         os.path.join("releases", self.commit_a))

    def test_the_deployed_package_resolves_its_repository_to_that_project(self):
        """THE WHOLE POINT OF THE MODE, asserted against the real resolver.

        `_repo_info` is what `home_repo_id` calls on the package's own
        directory, so pointing it at the DEPLOYED package is the same question
        production asks — and it must answer with the consuming project's
        gitdir, not this deployer's. The control is the source repo: the same
        call on the source tree must answer differently, or the arm would pass
        on a resolver that returns one constant.
        """
        from helm import dispatches
        project = self._project("consumer")
        proc = self._deploy_project(project)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.strip(), "the deployer reported nothing")
        deployed = (project / ".cache" / "helm-artifacts" / "releases"
                    / self.commit_a / "helm")
        mine = dispatches._repo_info(str(deployed))
        theirs = dispatches._repo_info(str(project))
        source = dispatches._repo_info(str(self.repo))
        self.assertIsNotNone(mine, "the deployed package resolved no repository")
        # Each resolution must be a real gitdir on disk. Without this the
        # comparisons below would still hold if the resolver started handing
        # back the same meaningless value for everything.
        self.assertIsNotNone(theirs, "the project resolved nothing")
        self.assertIsNotNone(source, "the source repo resolved nothing")
        self.assertTrue(os.path.isdir(mine["repo_id"]),
                        "the deployed repo_id is not a real gitdir")
        self.assertTrue(os.path.isdir(theirs["repo_id"]),
                        "the project repo_id is not a real gitdir")
        self.assertTrue(os.path.isdir(source["repo_id"]),
                        "the source repo_id is not a real gitdir")
        self.assertEqual(mine["repo_id"], theirs["repo_id"],
                         "the deployed package does not bind the project")
        self.assertNotEqual(mine["repo_id"], source["repo_id"],
                            "identity followed the deployer, not the artifact")

    def test_being_gitignored_does_not_hide_the_package_from_discovery(self):
        """The premise the mode rests on, stated as an arm.

        It is reasonable to fear that an IGNORED path might not resolve a
        repository — it does, because gitignore governs tracking and not
        discovery. If that ever stopped being true the mode would silently
        lose its identity, so the fact is pinned here rather than assumed.
        """
        from helm import dispatches
        project = self._project("consumer")
        proc = self._deploy_project(project)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.strip(), "the deployer reported nothing")
        deployed = (project / ".cache" / "helm-artifacts" / "releases"
                    / self.commit_a / "helm")
        ignored = subprocess.run(
            ["git", "-C", str(project), "check-ignore", "-q", str(deployed)])
        self.assertEqual(ignored.returncode, 0,  # noqa: VACUOUS_ASSERTION — rc 0 IS the precondition under test; one call cannot report both ignored and not, and the must-miss below is what stops it being vacuous
                         "the fixture never actually ignored the cache, so "
                         "this arm proves nothing")
        # THE MUST-MISS. rc 0 above means nothing unless this check can also
        # say no: a tracked file in the same project must come back rc 1, or
        # `check-ignore` is answering yes to everything.
        tracked = subprocess.run(
            ["git", "-C", str(project), "check-ignore", "-q",
             str(project / ".gitignore")])
        self.assertEqual(tracked.returncode, 1,
                         "check-ignore calls everything ignored, so the "
                         "ignore assertion above carries no information")
        resolved = dispatches._repo_info(str(deployed))
        self.assertIsNotNone(resolved,
                             "an ignored path stopped resolving its repository")
        self.assertEqual(resolved["repo_id"],
                         dispatches._repo_info(str(project))["repo_id"],
                         "an ignored path resolved to the wrong repository")

    def test_an_unignored_cache_is_refused_before_anything_is_written(self):
        """A REFUSED DEPLOY LEAVES THE PROJECT EXACTLY AS IT WAS FOUND.

        The positive control carries this arm: an untouched project is what a
        BROKEN deployer produces too, so the same project is deployed into
        successfully once the ignore exists. Absence only means refusal if
        presence was reachable.
        """
        project = self._project("consumer", ignore=None)
        before = sorted(os.listdir(project))
        proc = self._deploy_project(project)
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("does not ignore it", proc.stderr)
        self.assertIn(".gitignore", proc.stderr,
                      "the refusal does not say how to fix it")
        self.assertFalse((project / ".cache").exists(),
                         "the refused deploy created the cache anyway")
        self.assertEqual(sorted(os.listdir(project)), before,
                         "the refused deploy changed the project")

        (project / ".gitignore").write_text(".cache/\n", encoding="utf-8")
        self.assertEqual(self._deploy_project(project).returncode, 0,
                         "THE CONTROL FAILED: this project cannot be deployed "
                         "into at all, so the refusal above proved nothing")
        self.assertTrue((project / ".cache").exists(),
                        "the cache path is unreachable even on success, so "
                        "its absence after the refusal proves nothing")

    def test_project_and_root_together_are_refused_not_silently_ranked(self):
        project = self._project("consumer")
        other = self.base / "elsewhere"
        proc = self._deploy_project(project, "--root", str(other))
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("pass one", proc.stderr)
        self.assertFalse(other.exists())
        self.assertFalse((project / ".cache").exists())

        # BOTH ROOTS MUST BE REACHABLE ALONE, or the two absences above are
        # just two things this fixture could never have produced.
        self.assertEqual(self._deploy(other).returncode, 0)
        self.assertTrue(other.exists(), "--root alone cannot produce a root")
        self.assertEqual(self._deploy_project(project).returncode, 0)
        self.assertTrue((project / ".cache").exists(),
                        "--project alone cannot produce a root")

    def test_a_project_outside_a_git_working_tree_is_refused(self):
        loose = self.base / "not-a-repo"
        loose.mkdir()
        proc = self._deploy_project(loose)
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("not inside a Git working tree", proc.stderr)
        self.assertFalse((loose / ".cache").exists())

        # THE SAME DIRECTORY, once it is a repository that ignores the cache,
        # must accept the deploy — so the absence above is the missing repo
        # and not something about this path.
        subprocess.run(["git", "-C", str(loose), "init", "-q"], check=True)
        (loose / ".gitignore").write_text(".cache/\n", encoding="utf-8")
        self.assertEqual(self._deploy_project(loose).returncode, 0)
        self.assertTrue((loose / ".cache").exists(),
                        "this path can never hold a cache, so the refusal "
                        "above proved nothing")

    def test_a_missing_project_is_refused(self):
        proc = self._deploy_project(self.base / "absent")
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("is not a directory", proc.stderr)

    def test_a_subdirectory_names_the_same_runtime_as_the_project_root(self):
        """One repository, one project-local runtime.

        Identity is a property of the repository, so resolving through
        `--show-toplevel` keeps `--project A/sub` and `--project A` from
        producing two artifact roots that would answer every identity question
        identically.
        """
        project = self._project("consumer")
        nested = project / "sub" / "dir"
        nested.mkdir(parents=True)
        self.assertEqual(self._deploy_project(nested).returncode, 0)
        self.assertTrue((project / ".cache" / "helm-artifacts" / "current").exists())
        self.assertFalse((nested / ".cache").exists(),
                         "a subdirectory grew its own runtime")

    def test_an_unanswerable_ignore_question_refuses_rather_than_waving_through(self):
        """rc 0 is ignored and rc 1 is not-ignored; ANY other rc is unknown.

        A guard that treats "could not tell" as "fine" refuses nothing exactly
        when it cannot see, which is the fail-open shape `home_repo_id` was
        rewritten to close on the write door. Driven at the function because
        the earlier working-tree check catches a broken repo before the CLI
        can reach this branch.
        """
        deploy = _load_deployer()
        loose = self.base / "bare"
        loose.mkdir()
        with self.assertRaises(deploy.DeployError) as caught:
            deploy._require_ignored(str(loose), str(loose / ".cache"))
        self.assertIn("cannot determine whether", str(caught.exception))
        self.assertIn("refused rather than attempted unchecked",
                      str(caught.exception))


class DryRunTests(DeployFixture):

    def test_dry_run_creates_nothing_and_does_not_mutate_an_existing_root(self):
        empty = self.base / "empty-artifacts"
        first = self._deploy(empty, dry_run=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("dry-run release=created launcher=created current=selected",
                      first.stdout)
        self.assertFalse(empty.exists())

        root = self.base / "artifacts"
        self.assertEqual(self._deploy(root).returncode, 0)
        commit_b = self._commit_runtime("B")
        before = self._snapshot(root)
        status_before = self._git("status", "--porcelain").stdout

        second = self._deploy(root, dry_run=True)

        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("dry-run release=created launcher=reused current=selected",
                      second.stdout)
        self.assertEqual(self._snapshot(root), before)
        self.assertEqual(self._git("status", "--porcelain").stdout, status_before)
        self.assertFalse(self._release(root, commit_b).exists())


class LauncherIsolationTests(DeployFixture):

    def test_launcher_executes_a_concrete_release_without_current_in_sys_path(self):
        root = self.base / "artifacts"
        self.assertEqual(self._deploy(root).returncode, 0)
        release = self._release(root, self.commit_a)

        result = self._launcher(root, "probe")

        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        entry = str(release / "bin" / "helm")
        self.assertEqual(report["argv0"], entry)
        self.assertEqual(Path(report["cli_file"]), release / "helm" / "cli.py")
        self.assertEqual(Path(report["path"][0]), release)
        self.assertNotIn(str(root / "current"), "\n".join(report["path"]))
        self.assertNotIn("/current/", report["argv0"])

    def test_later_source_mutation_cannot_change_the_published_artifact(self):
        root = self.base / "artifacts"
        self.assertEqual(self._deploy(root).returncode, 0)
        release = self._release(root, self.commit_a)
        before = self._snapshot(release)
        self._write_runtime("MUTATED")

        result = self._launcher(root, "lazy-now")

        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["value"], "A")
        self.assertEqual(self._snapshot(release), before)

    def test_inflight_lazy_import_stays_on_a_while_next_process_uses_b(self):
        root = self.base / "artifacts"
        self.assertEqual(self._deploy(root).returncode, 0)
        process = subprocess.Popen(
            [str(root / "bin" / "helm"), "lazy-wait"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(process.stdout.readline().strip(), "ready")
        commit_b = self._commit_runtime("B")
        switched = self._deploy(root)
        self.assertEqual(switched.returncode, 0, switched.stderr)

        stdout, stderr = process.communicate("\n", timeout=10)
        self.assertEqual(process.returncode, 0, stderr)
        old = json.loads(stdout.strip())
        new_result = self._launcher(root, "lazy-now")
        self.assertEqual(new_result.returncode, 0, new_result.stderr)
        new = json.loads(new_result.stdout)

        self.assertEqual(old["value"], "A")
        self.assertEqual(Path(old["path"][0]),
                         self._release(root, self.commit_a))
        self.assertEqual(new["value"], "B")
        self.assertEqual(Path(new["path"][0]),
                         self._release(root, commit_b))

    def test_launcher_writes_no_bytecode_and_does_not_mutate_release(self):
        root = self.base / "artifacts"
        self.assertEqual(self._deploy(root).returncode, 0)
        release = self._release(root, self.commit_a)
        before = self._snapshot(release)

        result = self._launcher(root, "lazy-now")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._snapshot(release), before)
        bytecode = [path for path in release.rglob("*")
                    if path.name == "__pycache__" or path.suffix == ".pyc"]
        self.assertEqual(bytecode, [])

    def test_deployer_refuses_a_symlinked_launcher_parent(self):
        root_a = self.base / "artifacts-a"
        self.assertEqual(self._deploy(root_a).returncode, 0)
        self._commit_runtime("B")
        root_b = self.base / "artifacts-b"
        root_b.mkdir()
        os.symlink(root_a / "bin", root_b / "bin")

        result = self._deploy(root_b)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("artifact path is not a real directory", result.stderr)
        self.assertFalse((root_b / "current").exists())

    def test_launcher_refuses_a_symlinked_releases_parent(self):
        root = self.base / "artifacts"
        self.assertEqual(self._deploy(root).returncode, 0)
        relocated = self.base / "relocated-releases"
        os.rename(root / "releases", relocated)
        os.symlink(relocated, root / "releases")

        result = self._launcher(root, "--version")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("releases is not a real directory", result.stderr)

    def test_launcher_refuses_outside_and_malformed_current_targets(self):
        root = self.base / "artifacts"
        self.assertEqual(self._deploy(root).returncode, 0)
        current = root / "current"

        current.unlink()
        os.symlink("../outside", current)
        outside = self._launcher(root, "--version")
        self.assertNotEqual(outside.returncode, 0)
        self.assertIn("outside releases/<commit>", outside.stderr)

        current.unlink()
        current.write_text("not a symlink\n")
        malformed = self._launcher(root, "--version")
        self.assertNotEqual(malformed.returncode, 0)
        self.assertIn("cannot read current symlink", malformed.stderr)


if __name__ == "__main__":
    unittest.main()
