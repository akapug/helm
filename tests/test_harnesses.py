#!/usr/bin/env python3
"""helm.harnesses — the auto-map's eyes: per-harness session scanners + the
codex cwd sidecar. Hermetic: every root is a tmp dir, HELM_CACHE_DIR points the
sidecar at tmp, so the real ~/.claude, ~/.codex and ~/.cache are never read or
written."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import harnesses  # noqa: E402


class ClaudeScanTest(unittest.TestCase):
    def _sess(self, root, slug, lines, name="sess.jsonl"):
        d = os.path.join(root, slug)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, name)
        with open(p, "w") as f:
            for ln in lines:
                f.write(json.dumps(ln) + "\n")
        return p

    def test_decodes_real_cwd_never_the_slug(self):
        with tempfile.TemporaryDirectory() as root:
            # the slug is a lossy jail; the cwd is decoded from file content
            self._sess(root, "-home-u-dev-proj", [{"cwd": "/home/u/dev/proj"}, {"x": 1}])
            obs = harnesses.claude_observations(claude_root=root)
            self.assertEqual(len(obs), 1)
            self.assertEqual(obs[0]["cwd"], "/home/u/dev/proj")
            self.assertEqual(obs[0]["harness"], "claude")
            self.assertEqual(obs[0]["sessions"], 1)
            self.assertEqual(obs[0]["refs"], ["-home-u-dev-proj"])

    def test_counts_every_session_file_in_a_slug(self):
        with tempfile.TemporaryDirectory() as root:
            self._sess(root, "slug", [{"cwd": "/home/u/dev/proj"}], name="a.jsonl")
            self._sess(root, "slug", [{"cwd": "/home/u/dev/proj"}], name="b.jsonl")
            obs = harnesses.claude_observations(claude_root=root)
            self.assertEqual(obs[0]["sessions"], 2)

    def test_slug_without_cwd_is_dropped(self):
        with tempfile.TemporaryDirectory() as root:
            self._sess(root, "slug", [{"nope": 1}])
            self.assertEqual(harnesses.claude_observations(claude_root=root), [])

    def test_absent_root_is_empty(self):
        self.assertEqual(harnesses.claude_observations(claude_root="/no/such/root"), [])


class OpencodeScanTest(unittest.TestCase):
    def _proj(self, root, name, worktree, updated_ms=1_900_000_000_000):
        os.makedirs(root, exist_ok=True)
        p = os.path.join(root, name + ".json")
        with open(p, "w") as f:
            json.dump({"worktree": worktree, "time": {"updated": updated_ms}}, f)
        return p

    def test_worktree_is_the_real_path(self):
        with tempfile.TemporaryDirectory() as root:
            self._proj(root, "abc", "/home/u/dev/oc")
            obs = harnesses.opencode_observations(storage=root)
            self.assertEqual(len(obs), 1)
            self.assertEqual(obs[0]["cwd"], "/home/u/dev/oc")
            self.assertEqual(obs[0]["harness"], "opencode")

    def test_global_json_and_worktreeless_skipped(self):
        with tempfile.TemporaryDirectory() as root:
            self._proj(root, "global", "/should/skip")   # global.json ignored
            os.makedirs(root, exist_ok=True)
            with open(os.path.join(root, "empty.json"), "w") as f:
                json.dump({"time": {}}, f)               # no worktree
            self.assertEqual(harnesses.opencode_observations(storage=root), [])


class CodexSidecarTest(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.TemporaryDirectory()
        self.addCleanup(self.cache.cleanup)
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        prev = os.environ.get("HELM_CACHE_DIR")
        os.environ["HELM_CACHE_DIR"] = self.cache.name

        def restore():
            if prev is None:
                os.environ.pop("HELM_CACHE_DIR", None)
            else:
                os.environ["HELM_CACHE_DIR"] = prev
        self.addCleanup(restore)

    def _rollout(self, name, cwd, sub=("2026", "07", "20")):
        d = os.path.join(self.root.name, *sub)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "rollout-2026-07-20T10-00-00-%s.jsonl" % name)
        payload = {"payload": {"cwd": cwd}} if cwd else {"payload": {}}
        with open(p, "w") as f:
            f.write(json.dumps(payload) + "\n")
        return p

    def _scan(self):
        return harnesses.codex_observations(roots=[self.root.name])

    def _cache(self):
        with open(harnesses._codex_cache_path()) as f:
            return json.load(f)

    def test_aggregates_sessions_and_days_per_cwd(self):
        a = self._rollout("a", "/home/u/dev/proj")
        b = self._rollout("b", "/home/u/dev/proj", sub=("2026", "07", "19"))
        self._rollout("c", "/home/u/dev/other")
        # the "day" is the file's mtime day, not the path — set two apart
        os.utime(a, (1_900_000_000, 1_900_000_000))
        os.utime(b, (1_900_000_000 - 2 * 86400, 1_900_000_000 - 2 * 86400))
        obs = {o["cwd"]: o for o in self._scan()}
        self.assertEqual(obs["/home/u/dev/proj"]["sessions"], 2)
        self.assertEqual(len(obs["/home/u/dev/proj"]["days"]), 2)
        self.assertEqual(obs["/home/u/dev/other"]["sessions"], 1)

    def test_sidecar_written_and_second_scan_never_resniffs(self):
        self._rollout("a", "/home/u/dev/proj")
        self._scan()
        cache = self._cache()
        self.assertEqual(len(cache), 1)
        # a matching stat-signature must skip the read entirely
        with mock.patch.object(harnesses, "_sniff_cwd",
                               side_effect=AssertionError("re-sniffed a cached file")):
            obs = self._scan()
        self.assertEqual(obs[0]["cwd"], "/home/u/dev/proj")

    def test_cwdless_file_is_negatively_cached(self):
        p = self._rollout("a", None)
        self.assertEqual(self._scan(), [])
        cache = self._cache()
        self.assertIsNone(cache[p][2])               # null cwd cached
        with mock.patch.object(harnesses, "_sniff_cwd",
                               side_effect=AssertionError("re-sniffed a cwd-less file")):
            self.assertEqual(self._scan(), [])       # negative cache holds

    def test_stat_change_invalidates_the_cache(self):
        p = self._rollout("a", "/home/u/dev/proj")
        self._scan()
        with open(p, "w") as f:                       # rewrite: new cwd + new size
            f.write(json.dumps({"payload": {"cwd": "/home/u/dev/moved-somewhere-else"}}) + "\n")
        os.utime(p, (2_000_000_000, 2_000_000_000))   # bump mtime past the cache
        obs = self._scan()
        self.assertEqual(obs[0]["cwd"], "/home/u/dev/moved-somewhere-else")

    def test_inode_dedup_across_roots(self):
        p = self._rollout("a", "/home/u/dev/proj")
        alt = tempfile.TemporaryDirectory()
        self.addCleanup(alt.cleanup)
        d = os.path.join(alt.name, "2026", "07", "20")
        os.makedirs(d)
        os.symlink(p, os.path.join(d, os.path.basename(p)))  # same inode, 2nd root
        obs = harnesses.codex_observations(roots=[self.root.name, alt.name])
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0]["sessions"], 1)       # counted once, not twice

    def test_vanished_files_pruned_from_sidecar(self):
        p = self._rollout("a", "/home/u/dev/proj")
        self._scan()
        os.remove(p)
        self._scan()
        self.assertEqual(self._cache(), {})

    def test_cache_read_failure_falls_back_to_full_sniff(self):
        self._rollout("a", "/home/u/dev/proj")
        with open(harnesses._codex_cache_path(), "w") as f:
            f.write("{ this is not json")             # corrupt sidecar
        obs = self._scan()                            # degrades, never raises
        self.assertEqual(obs[0]["cwd"], "/home/u/dev/proj")


class AllObservationsTest(unittest.TestCase):
    def test_concatenates_the_three_scanners(self):
        with mock.patch.object(harnesses, "claude_observations", return_value=[{"harness": "claude"}]), \
             mock.patch.object(harnesses, "codex_observations", return_value=[{"harness": "codex"}]), \
             mock.patch.object(harnesses, "opencode_observations", return_value=[{"harness": "opencode"}]):
            out = harnesses.all_observations()
        self.assertEqual({o["harness"] for o in out}, {"claude", "codex", "opencode"})


if __name__ == "__main__":
    unittest.main()
