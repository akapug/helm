"""Authority-v2 keeps the tested repository separate from its Helm runner."""
import copy
import json
import os
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from helm import gate, gateauthority, gateimport
from scripts import deploy
from tests.test_gateauthority import _receipt


def _git(repo, *args):
    proc = subprocess.run(["git", "-C", repo] + list(args),
                          capture_output=True, check=True)
    return proc.stdout.decode("utf-8").strip()


def _write(path, raw, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as stream:
        stream.write(raw)
    os.chmod(path, mode)


def _repo(root, artifact=False, extra=()):
    repo = os.path.join(root, "runner")
    os.makedirs(repo)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    names = ("gatetestrecord.py", "gatechild.py") + tuple(extra)
    declaration = "STANDALONE_DEPENDENCIES = %r\n" % (names,)
    files = {
        "helm/gateshard.py": declaration.encode("utf-8"),
        "helm/gatetestrecord.py": b"VALUE = 'record'\n",
        "helm/gatechild.py": b"VALUE = 'child'\n",
    }
    for name in extra:
        files["helm/" + name] = ("VALUE = %r\n" % name).encode("utf-8")
    if artifact:
        files.update({
            "bin/helm": (b"#!/usr/bin/env python3\n"
                         b"print('helm 0.0')\n"),
            # THE SECOND ENTRY POINT the deployer requires and mode-checks:
            # every generated hook command execs it, so an artifact without it
            # would silently unguard the estate and is refused.
            "bin/helm-hook": b"#!/bin/sh\nexit 0\n",
            "helm/__init__.py": b"__version__ = '0.0'\n",
            "helm/cli.py": b"# fixture\n",
            "README.md": b"fixture\n",
            "docs/fixture.md": b"fixture\n",
            "tests/__init__.py": b"",
        })
    for path, raw in files.items():
        _write(os.path.join(repo, path), raw,
               0o755 if path.startswith("bin/") else 0o644)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixture")
    commit = _git(repo, "rev-parse", "HEAD^{commit}")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    bundle = {path: raw for path, raw in files.items()
              if path == gateauthority.RUNNER_PATH
              or path in gateauthority.runner_dependency_paths(
                  files[gateauthority.RUNNER_PATH])}
    return repo, commit, tree, bundle


def _adopter(root):
    repo = os.path.join(root, "adopter")
    os.makedirs(repo)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    _write(os.path.join(repo, "tests", "test_app.py"), b"# adopter\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "adopter")
    return repo, _git(repo, "rev-parse", "HEAD^{commit}"), \
        _git(repo, "rev-parse", "HEAD^{tree}")


def _row(repo, commit, tree, files, root="/remote/helm-release",
         content=None, repo_id=None):
    row, _raw = _receipt()
    manifest = gateauthority.runner_bundle_manifest(files)
    entry = next(item for item in manifest
                 if item["path"] == gateauthority.RUNNER_PATH)
    executable = row["interpreter"]["executable"]
    argv = [executable, os.path.join(root, "helm", "gateshard.py")]
    row["argv"] = argv
    row["sharded_authority"]["v"] = 2
    row["sharded_authority"]["runner"] = {
        "repo_id": repo if repo_id is None else repo_id,
        "commit": commit,
        "tree": tree,
        "checkout_root": root,
        "path": gateauthority.RUNNER_PATH,
        "blob": entry["blob"],
        "sha256": entry["sha256"],
        "argv": argv,
        "content_digest": content,
        "files": manifest,
    }
    row["id"] = gate._receipt_id(row)
    return row


def _make_writable(root):
    for current, dirs, files in os.walk(root):
        os.chmod(current, 0o755)
        for name in dirs:
            os.chmod(os.path.join(current, name), 0o755)
        for name in files:
            path = os.path.join(current, name)
            if not os.path.islink(path):
                os.chmod(path, 0o644)


class AuthorityV2ReaderTest(unittest.TestCase):
    def test_v1_is_exact_and_v2_has_its_own_exact_grammar(self):
        legacy, _raw = _receipt()
        self.assertIsNone(gateauthority.receipt_refusal(legacy))
        changed = copy.deepcopy(legacy)
        changed["sharded_authority"]["runner"]["repo_id"] = "/extra"
        self.assertIn("fields", gateauthority.receipt_refusal(changed))
        with tempfile.TemporaryDirectory() as tmp:
            repo, commit, tree, files = _repo(tmp)
            row = _row(repo, commit, tree, files)
            self.assertIsNone(gateauthority.receipt_refusal(row))
            for defect in ("extra", "order", "entry"):
                broken = copy.deepcopy(row)
                runner = broken["sharded_authority"]["runner"]
                if defect == "extra":
                    runner["foreign"] = True
                elif defect == "order":
                    runner["files"].reverse()
                else:
                    runner["blob"] = "f" * 40
                self.assertIsNotNone(gateauthority.receipt_refusal(broken))

    def test_adopter_without_helm_places_against_a_separate_runner_repo(self):  # noqa: VACUOUS_ASSERTION — both repositories resolve nonempty exact trees before the test proves placement succeeds without a runner file in the tested tree
        with tempfile.TemporaryDirectory() as tmp:
            runner, commit, tree, files = _repo(tmp)
            adopter, head, tested_tree = _adopter(tmp)
            row = _row(runner, commit, tree, files)
            row.update(repo_id="/remote/adopter", head=head, tree=tested_tree,
                       head_after=head, tree_after=tested_tree)
            row["id"] = gate._receipt_id(row)
            self.assertFalse(os.path.exists(
                os.path.join(adopter, gateauthority.RUNNER_PATH)))
            _write(os.path.join(adopter, "tests", "test_app.py"), b"# replaced\n")
            _git(adopter, "add", ".")
            _git(adopter, "commit", "-qm", "replacement")
            replacement = _git(adopter, "rev-parse", "HEAD^{commit}")
            _git(adopter, "replace", head, replacement)
            self.assertNotEqual(tested_tree,
                                _git(adopter, "rev-parse", head + "^{tree}"))
            with mock.patch.object(gateauthority, "_source_candidates",
                                   return_value=[runner]):
                self.assertIsNone(gateimport.placement_err(row, adopter))

    def test_receipt_repo_id_cannot_select_its_own_git_verifier(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, commit, tree, files = _repo(tmp)
            row = _row(repo, commit, tree, files, repo_id=repo)
            state = gateauthority.runner_verification(row, releases=[])
            self.assertFalse(state["verified"])
            self.assertEqual("REFUSED", state["git"]["result"])
            self.assertNotIn(repo, state["git"]["detail"])
            trusted = gateauthority.runner_verification(
                row, repos=[repo], releases=[])
            self.assertTrue(trusted["verified"])

    def test_absent_runner_commit_is_named_as_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, commit, tree, files = _repo(tmp)
            row = _row(repo, commit, tree, files)
            row["sharded_authority"]["runner"]["commit"] = "f" * 40
            row["id"] = gate._receipt_id(row)
            state = gateauthority.runner_verification(
                row, repos=[repo], releases=[])
            self.assertEqual("REFUSED", state["git"]["result"])
            self.assertIn("commit is unavailable", state["git"]["detail"])
            self.assertNotIn("different runner tree", state["git"]["detail"])

    def test_runner_commit_with_a_different_tree_names_the_contradiction(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, commit, tree, files = _repo(tmp)
            row = _row(repo, commit, tree, files)
            row["sharded_authority"]["runner"]["tree"] = "f" * 40
            row["id"] = gate._receipt_id(row)
            state = gateauthority.runner_verification(
                row, repos=[repo], releases=[])
            self.assertEqual("REFUSED", state["git"]["result"])
            self.assertIn("different runner tree", state["git"]["detail"])
            self.assertNotIn("commit is unavailable", state["git"]["detail"])

    def test_remote_repo_id_is_proven_by_an_independent_local_object_store(self):  # noqa: VACUOUS_ASSERTION — the Git channel positively verifies the exact bundle while the separately asserted artifact channel remains intentionally uninvoked
        with tempfile.TemporaryDirectory() as tmp:
            repo, commit, tree, files = _repo(tmp)
            row = _row(repo, commit, tree, files,
                       repo_id="ssh://example.invalid/helm.git")
            _write(os.path.join(repo, gateauthority.RUNNER_PATH),
                   b"STANDALONE_DEPENDENCIES = ('other.py',)\n")
            _write(os.path.join(repo, "helm", "other.py"), b"other\n")
            _git(repo, "add", ".")
            _git(repo, "commit", "-qm", "replacement")
            replacement = _git(repo, "rev-parse", "HEAD^{commit}")
            _git(repo, "replace", commit, replacement)
            self.assertNotEqual(tree, _git(repo, "rev-parse", commit + "^{tree}"))
            state = gateauthority.runner_verification(
                row, repos=[repo], releases=[])
            self.assertTrue(state["verified"])
            self.assertEqual("VERIFIED", state["git"]["result"])
            self.assertEqual("NOT_RUN", state["artifact"]["result"])
            drift = copy.deepcopy(row)
            drift["sharded_authority"]["runner"]["content_digest"] = \
                "sha256:" + "f" * 64
            drift["id"] = gate._receipt_id(drift)
            refused = gateauthority.runner_verification(
                drift, repos=[repo], releases=[])
            self.assertFalse(refused["verified"])
            self.assertIn("content digest", refused["git"]["detail"])

    def test_loaded_runner_root_ignores_the_process_working_directory(self):  # noqa: VACUOUS_ASSERTION — the expected concrete root is derived from the loaded module before cwd changes and exact equality is asserted afterwards
        expected = os.path.dirname(os.path.dirname(
            os.path.realpath(gateauthority.gateshard.__file__)))
        with tempfile.TemporaryDirectory() as tmp:
            previous = os.getcwd()
            try:
                os.chdir(tmp)
                self.assertEqual(expected, gateauthority.loaded_runner_root())
            finally:
                os.chdir(previous)

    def test_dependency_declaration_grows_the_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            _repo(tmp)
            base = {
                gateauthority.RUNNER_PATH:
                    b"STANDALONE_DEPENDENCIES = ('gatetestrecord.py', 'gatechild.py')\n",
                "helm/gatetestrecord.py": b"record\n",
                "helm/gatechild.py": b"child\n",
            }
            first = gateauthority.runner_bundle_manifest(base)
            grown = dict(base)
            grown[gateauthority.RUNNER_PATH] = (
                b"STANDALONE_DEPENDENCIES = "
                b"('gatetestrecord.py', 'gatechild.py', 'fake.py')\n")
            grown["helm/fake.py"] = b"fake\n"
            second = gateauthority.runner_bundle_manifest(grown)
            self.assertEqual(3, len(first))
            self.assertEqual(4, len(second))
            self.assertIn("helm/fake.py", [item["path"] for item in second])

    def test_verification_attempts_are_explicit_and_neither_is_not_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, commit, tree, files = _repo(tmp)
            row = _row(repo, commit, tree, files,
                       repo_id="ssh://example.invalid/helm.git")
            state = gateauthority.runner_verification(
                row, repos=[], releases=[])
            self.assertEqual({"attempted": False, "result": "NOT_RUN",
                              "detail": "no verifier input"}, state["git"])
            self.assertEqual("NOT_RUN", state["artifact"]["result"])
            refusal = gateauthority.runner_identity_refusal(
                row, repos=[], releases=[])
            self.assertIn("neither Git nor artifact verifier ran", refusal)
            measured = gateauthority.runner_verification(
                row, repos=[os.path.join(tmp, "missing")], releases=[])
            self.assertTrue(measured["git"]["attempted"])
            self.assertEqual("REFUSED", measured["git"]["result"])
            refusal = gateauthority.runner_identity_refusal(
                row, repos=[os.path.join(tmp, "missing")], releases=[])
            self.assertIn("runner identity REFUSED", refusal)
            self.assertNotIn("runner identity UNKNOWN", refusal)

    def test_default_artifact_selector_finds_only_the_canonical_release(self):  # noqa: VACUOUS_ASSERTION — deploy creates the exact canonical release before the default selector must discover and positively verify that concrete path
        with tempfile.TemporaryDirectory() as tmp:
            repo, commit, tree, files = _repo(tmp, artifact=True)
            data = os.path.join(tmp, "data")
            root = os.path.join(data, "helm", "artifacts")
            deploy.deploy(repo, commit, root, False)
            release = os.path.join(root, "releases", commit)
            with open(os.path.join(release, deploy.MANIFEST),
                      encoding="utf-8") as stream:
                content = json.load(stream)["content_digest"]
            row = _row(repo, commit, tree, files,
                       root="/remote/releases/" + commit,
                       content=content,
                       repo_id="ssh://example.invalid/helm.git")
            try:
                with mock.patch.dict(os.environ, {"XDG_DATA_HOME": data}):
                    state = gateauthority.runner_verification(row, repos=[])
                self.assertTrue(state["verified"])
                self.assertEqual("VERIFIED", state["artifact"]["result"])
                self.assertEqual(os.path.realpath(release),
                                 state["artifact"]["detail"])
            finally:
                _make_writable(root)

    def test_exact_artifact_verifies_and_a_mutated_sibling_refuses(self):  # noqa: VACUOUS_ASSERTION — the try body first positively verifies both immutable Git and artifact routes before the same concrete sibling bytes are mutated
        with tempfile.TemporaryDirectory() as tmp:
            repo, commit, tree, files = _repo(tmp, artifact=True)
            root = os.path.join(tmp, "artifacts")
            deploy.deploy(repo, commit, root, False)
            release = os.path.join(root, "releases", commit)
            with open(os.path.join(release, deploy.MANIFEST),
                      encoding="utf-8") as stream:
                content = json.load(stream)["content_digest"]
            row = _row(repo, commit, tree, files,
                       root="/remote/releases/" + commit,
                       content=content,
                       repo_id="ssh://example.invalid/helm.git")
            try:
                git = gateauthority.runner_verification(
                    row, repos=[repo], releases=[])
                self.assertTrue(git["verified"])
                self.assertEqual("VERIFIED", git["git"]["result"])
                state = gateauthority.runner_verification(
                    row, repos=[], releases=[release])
                self.assertTrue(state["verified"])
                self.assertEqual("VERIFIED", state["artifact"]["result"])
                sibling = os.path.join(release, "helm", "gatechild.py")
                os.chmod(sibling, 0o644)
                with open(sibling, "ab") as stream:
                    stream.write(b"drift\n")
                os.chmod(sibling, 0o444)
                changed = gateauthority.runner_verification(
                    row, repos=[], releases=[release])
                self.assertFalse(changed["verified"])
                self.assertEqual("REFUSED", changed["artifact"]["result"])
            finally:
                _make_writable(root)


if __name__ == "__main__":
    unittest.main()
