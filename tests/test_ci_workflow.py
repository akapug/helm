#!/usr/bin/env python3
"""The tree carries no GitHub Actions workflow, and a test says so.

CI, builds and releases for every local project run on the local fabric —
`helm gate run --repo .`, the fab gate on a fabric node — never on GitHub
Actions, and Actions is disabled on both helm remotes. A workflow file under
`.github/workflows/` would be a second instrument claiming green about a
commit the fab gate never saw, and a `push:` trigger there fires on every lane
branch of a repository that runs dozens of live lane worktrees at once. The
Python floor a hosted matrix would measure is declared in `scripts/install.sh`
and the README; the gate box runs a newer interpreter, so the floor is
measured only when a gate is run under a 3.9 interpreter.

`helm` is stdlib-only, so this walks the tree with `os` rather than reading a
manifest — a test that needed provisioning would contradict the thing it is
guarding.
"""
import os
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW_DIR = os.path.join(".github", "workflows")
WORKFLOW_SUFFIXES = (".yml", ".yaml")


def _workflow_files(root):
    """Every GitHub Actions workflow file under root's `.github/workflows/`,
    as paths relative to root. Actions reads only that directory, and only
    YAML in it, so this is the exact population a hosted runner would launch.
    """
    base = os.path.join(root, WORKFLOW_DIR)
    if not os.path.isdir(base):
        return []
    return sorted(os.path.join(WORKFLOW_DIR, name)
                  for name in os.listdir(base)
                  if name.lower().endswith(WORKFLOW_SUFFIXES))


class TheScannerSeesAWorkflowWhenOneExists(unittest.TestCase):
    """Positive control on the same scanner the tree assertion uses: a planted
    workflow in a throwaway root is FOUND, so an empty answer over the real
    tree is a fact about the tree and never about a walk that matched nothing.
    """

    def test_a_planted_workflow_is_found_and_a_non_yaml_neighbour_is_not(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, WORKFLOW_DIR))
            with open(os.path.join(root, WORKFLOW_DIR, "ci.yml"), "w",
                      encoding="utf-8") as fh:
                fh.write("name: ci\non:\n  push:\n")
            with open(os.path.join(root, WORKFLOW_DIR, "README.md"), "w",
                      encoding="utf-8") as fh:
                fh.write("not a workflow\n")
            self.assertEqual(_workflow_files(root),
                             [os.path.join(WORKFLOW_DIR, "ci.yml")])

    def test_an_absent_directory_is_an_empty_answer_not_an_error(self):  # noqa: VACUOUS_ASSERTION — the empty list IS the contract for a root with no .github; the planted arm above proves the same scanner returns a non-empty answer when a workflow exists
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(_workflow_files(root), [])


class TheTreeCarriesNoGitHubActionsWorkflow(unittest.TestCase):

    def test_root_resolves_to_the_helm_checkout(self):
        """The control for the arms below: ROOT is the repository, not some
        directory this file happened to be copied into."""
        self.assertTrue(os.path.exists(os.path.join(ROOT, "bin", "helm")), ROOT)
        self.assertTrue(os.path.isdir(os.path.join(ROOT, "helm")), ROOT)

    def test_no_workflow_file_exists(self):  # noqa: VACUOUS_ASSERTION — absence is the owner rule under test; the planted-root arm proves the scanner finds a workflow when one exists, and the root arm proves it scanned this checkout
        found = _workflow_files(ROOT)
        self.assertEqual(found, [],
                         "GitHub Actions workflow(s) in the tree — CI runs on "
                         "the local fabric, never on Actions: %r" % found)

    def test_no_workflows_directory_exists(self):  # noqa: VACUOUS_ASSERTION — stricter than the file scan: the DIRECTORY is absent, so a workflow of any extension cannot arrive without this arm noticing; the root arm proves the path is anchored in this checkout
        self.assertFalse(os.path.exists(os.path.join(ROOT, WORKFLOW_DIR)),
                         "%s exists in the tree" % WORKFLOW_DIR)


if __name__ == "__main__":
    unittest.main()
