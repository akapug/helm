"""CV fleet-root integration: every Helm seat transcript reaches CV calls."""
import os
import shutil
import tempfile
import unittest
from unittest import mock

from helm import catalog, home, session


class CvRootsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-cv-roots-")
        self.old_home = os.environ.get("HELM_HOME")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")

    def tearDown(self):
        if self.old_home is None:
            os.environ.pop("HELM_HOME", None)
        else:
            os.environ["HELM_HOME"] = self.old_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def root(self, *parts):
        p = os.path.join(home.global_dir(), "seats", *parts, "claude", "projects")
        os.makedirs(p, exist_ok=True)
        return os.path.realpath(p)

    def test_family_and_instance_roots_join_existing_cv_env(self):
        family = self.root("codex")
        instance = self.root("codex", "instances", "codex-2")
        e = home.cv_env({"PATH": "/bin", "CLUSTERVISION_CLAUDE_ROOTS": family})
        self.assertEqual(e["PATH"], "/bin")
        self.assertEqual(e["CLUSTERVISION_CLAUDE_ROOTS"].split(os.pathsep),
                         [family, instance])

    def test_session_wrapper_always_passes_fleet_roots(self):
        root = self.root("kimi")
        done = mock.Mock(returncode=0, stdout="[]", stderr="")
        with mock.patch.object(session.subprocess, "run", return_value=done) as run:
            rc, _, _ = session._cv("ls", "--json", env={"PATH": "/bin"})
        self.assertEqual(rc, 0)
        self.assertEqual(run.call_args.kwargs["env"]["CLUSTERVISION_CLAUDE_ROOTS"],
                         root)

    def test_catalog_cv_call_carries_fleet_roots(self):
        root = self.root("codex", "instances", "codex-3")
        done = mock.Mock(returncode=0, stdout="[]", stderr="")
        with mock.patch.object(catalog.subprocess, "run", return_value=done) as run:
            rows, stats = catalog._build_from_cv()
        self.assertEqual(rows, [])
        self.assertEqual(stats["source"], "cv ls --json")
        self.assertEqual(run.call_args.kwargs["env"]["CLUSTERVISION_CLAUDE_ROOTS"],
                         root)


if __name__ == "__main__":
    unittest.main()
