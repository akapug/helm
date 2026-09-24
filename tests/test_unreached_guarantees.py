#!/usr/bin/env python3
"""End-to-end mutation witnesses for previously unreachable guarantees."""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest


SOURCE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

_DESCEND_SELECTOR = (
    "tests.test_landreq.FrontierDebtTest."
    "test_descend_STOPS_at_a_hit_and_does_not_read_past_it"
)
_PARENT_SELECTOR = (
    "tests.test_dispatches.SupersededParentSweepTest."
    "test_OMITTING_the_parent_refuses_rather_than_skipping"
)

_M4 = {
    "name": "M4-descend-continues-after-hit",
    "path": "helm/landreq.py",
    "find": (
        "        if take(cid, node.get(cid)):\n"
        "            out.append(cid)\n"
        "            continue\n"
        "        stack.extend(kids.get(cid, ()))\n"
    ),
    "replace": (
        "        if take(cid, node.get(cid)):\n"
        "            out.append(cid)\n"
        "        stack.extend(kids.get(cid, ()))\n"
    ),
}
_M5 = {
    "name": "M5-missing-parent-falls-back-to-kid-repo",
    "path": "helm/dispatches.py",
    "find": (
        "    prepo = parent.get(\"repo_id\") if isinstance(parent, dict) else None\n"
    ),
    "replace": (
        "    prepo = parent.get(\"repo_id\") if isinstance(parent, dict) else repo\n"
    ),
}
_INERT = {
    "name": "inert-descend-witness-comment",
    "path": "tests/test_landreq.py",
    "find": (
        "        # every descendant qualifies, so a walk that reads past a hit returns\n"
    ),
    "replace": (
        "        # all descendants qualify, so a walk that reads past a hit returns\n"
    ),
}


class UnreachedGuaranteesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-unreached-guarantees-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.caller = os.path.join(self.tmp, "caller")
        self.env = os.environ.copy()
        self.env["GIT_CONFIG_GLOBAL"] = "/dev/null"
        self.env["GIT_CONFIG_SYSTEM"] = "/dev/null"
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        head = self._git(
            SOURCE, "rev-parse", "--verify", "HEAD^{commit}"
        ).stdout.strip()
        clone = self._run(
            ("git", "clone", "--quiet", "--no-local", "--no-checkout",
             "--single-branch", SOURCE, self.caller),
            self.tmp,
            timeout=120,
        )
        self.assertEqual(clone.returncode, 0, self._failure(clone))
        checkout = self._git(
            self.caller, "checkout", "--quiet", "-b", "matrix-caller",
            head.decode("ascii"), check=False
        )
        self.assertEqual(checkout.returncode, 0, self._failure(checkout))
        actual = self._git(
            self.caller, "rev-parse", "--verify", "HEAD^{commit}"
        ).stdout.strip()
        self.assertEqual(actual, head)

    def _run(self, argv, cwd, timeout=60):
        return subprocess.run(
            argv,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=self.env,
        )

    def _git(self, root, *args, check=True):
        proc = self._run(("git",) + args, root)
        if check:
            self.assertEqual(proc.returncode, 0, self._failure(proc))
        return proc

    @staticmethod
    def _failure(proc):
        return (proc.stdout + proc.stderr).decode("utf-8", "replace")

    def _head_identity(self):
        commit = self._git(
            self.caller, "rev-parse", "--verify", "HEAD^{commit}"
        ).stdout.strip()
        ref = self._git(
            self.caller, "symbolic-ref", "-q", "HEAD", check=False
        )
        self.assertIn(ref.returncode, (0, 1), self._failure(ref))
        return commit, ref.stdout.strip() if ref.returncode == 0 else None

    def _target_state(self, path):
        literal = ":(literal)" + path
        full = os.path.join(self.caller, *path.split("/"))
        with open(full, "rb") as handle:
            body = handle.read()
        info = os.lstat(full)
        self.assertTrue(stat.S_ISREG(info.st_mode))
        return {
            "bytes": body,
            "worktree_mode": stat.S_IMODE(info.st_mode),
            "tree_entry": self._git(
                self.caller, "ls-tree", "-z", "HEAD", "--", literal
            ).stdout,
            "index_entry": self._git(
                self.caller, "ls-files", "--stage", "-z", "--", literal
            ).stdout,
            "index_flag": self._git(
                self.caller, "ls-files", "-v", "-z", "--", literal
            ).stdout,
        }

    def _repository_state(self, paths):
        literals = tuple(":(literal)" + path for path in paths)
        return {
            "head": self._head_identity(),
            "targets": {path: self._target_state(path) for path in paths},
            "cached_diff": self._git(
                self.caller, "diff", "--no-ext-diff", "--binary", "--cached",
                "--", *literals
            ).stdout,
            "worktree_diff": self._git(
                self.caller, "diff", "--no-ext-diff", "--binary", "--",
                *literals
            ).stdout,
            "tracked_status": self._git(
                self.caller, "status", "--porcelain=v2", "--untracked-files=no",
                "-z"
            ).stdout,
        }

    def _write_spec(self, name, selectors, mutations):
        path = os.path.join(self.tmp, name + ".json")
        spec = {
            "command": [sys.executable, "-m", "unittest"] + list(selectors),
            "timeout_seconds": 60,
            "mutations": mutations,
        }
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(spec, handle, sort_keys=True)
        return path

    def _run_matrix(self, name, selectors, mutations):
        paths = tuple(dict.fromkeys(mutation["path"] for mutation in mutations))
        before = self._repository_state(paths)
        self.assertEqual(before["cached_diff"], b"")
        self.assertEqual(before["worktree_diff"], b"")
        self.assertEqual(before["tracked_status"], b"")
        for path, target in before["targets"].items():
            self.assertEqual(target["index_flag"],
                             b"H " + path.encode("utf-8") + b"\0")
        for mutation in mutations:
            body = before["targets"][mutation["path"]]["bytes"]
            self.assertEqual(body.count(mutation["find"].encode("utf-8")), 1)
            self.assertNotEqual(mutation["find"], mutation["replace"])

        spec = self._write_spec(name, selectors, mutations)
        script = os.path.join(self.caller, "scripts", "mutation_matrix.py")
        proc = self._run(
            (sys.executable, script, spec), self.caller, timeout=300
        )

        after = self._repository_state(paths)
        self.assertEqual(after, before,
                         "canonical runner changed its caller repository")
        return proc

    def test_M4_and_M5_are_killed_by_direct_primitive_contracts(self):
        proc = self._run_matrix(
            "old-guarantees",
            (_DESCEND_SELECTOR, _PARENT_SELECTOR),
            [_M4, _M5],
        )
        output = proc.stdout.decode("utf-8", "replace")
        self.assertEqual(proc.returncode, 0, self._failure(proc))
        self.assertIn("KILLED M4-descend-continues-after-hit (2 tests)", output)
        self.assertIn(
            "KILLED M5-missing-parent-falls-back-to-kid-repo (2 tests)",
            output,
        )
        self.assertIn("ALL-KILLED 2/2", output)
        self.assertNotIn("SURVIVED", output)
        self.assertNotIn("MATRIX-ERROR", output)

    def test_inert_mutation_survives_without_all_killed(self):
        proc = self._run_matrix(
            "inert-control",
            (_DESCEND_SELECTOR,),
            [_INERT],
        )
        output = proc.stdout.decode("utf-8", "replace")
        self.assertEqual(proc.returncode, 1, self._failure(proc))
        self.assertIn("SURVIVED inert-descend-witness-comment (1 tests)", output)
        self.assertNotIn("ALL-KILLED", output)
        self.assertNotIn("MATRIX-ERROR", output)


if __name__ == "__main__":
    unittest.main()
