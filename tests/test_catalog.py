#!/usr/bin/env python3
"""catalog tests — hermetic: roots/caches point at tmp dirs, cv never invoked
(HELM_CATALOG=scanner forces the built-in scanner path)."""
import json
import os
import shutil
import tempfile
import unittest

from helm import catalog

# the audit case: a rollout uuid whose FIRST group is all digits — the greedy
# pre-fix regex ate it along with the timestamp
UUID_DIGITS = "01967342-9abc-4def-8123-456789abcdef"
UUID_HEX = "f1967342-9abc-4def-8123-456789abcdef"


class SessionIdTest(unittest.TestCase):
    def test_codex_id_survives_all_digits_first_group(self):
        p = f"/x/rollout-2026-07-11T15-30-00-{UUID_DIGITS}.jsonl"
        self.assertEqual(catalog._session_id(p, "codex"), UUID_DIGITS)

    def test_codex_id_hex_first_group(self):
        p = f"/x/rollout-2026-07-11T15-30-00-{UUID_HEX}.jsonl"
        self.assertEqual(catalog._session_id(p, "codex"), UUID_HEX)

    def test_claude_id_is_basename(self):
        self.assertEqual(
            catalog._session_id("/y/aaaa-bbbb.jsonl", "claude"), "aaaa-bbbb")

    def test_unprefixed_codex_name_passes_through(self):
        self.assertEqual(catalog._session_id(f"/y/{UUID_HEX}.jsonl", "codex"),
                         UUID_HEX)


class CacheRepairTest(unittest.TestCase):
    """A cache row minted by the greedy pre-fix regex carries a truncated id;
    the cache-hit path must repair it (cache keys on mtime/size, so the file
    itself never re-scans)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-catalog-")
        j = lambda *p: os.path.join(self.tmp, *p)
        self._orig = {k: getattr(catalog, k) for k in
                      ("CLAUDE_ROOTS", "CODEX_ROOTS", "CACHE_DIR", "CACHE", "SYN_CACHE")}
        catalog.CLAUDE_ROOTS = [j("claude-root")]
        catalog.CODEX_ROOTS = [j("codex-root")]
        catalog.CACHE_DIR = j("cache")
        catalog.CACHE = j("cache", "catalog-cache.json")
        catalog.SYN_CACHE = j("cache", "syn-cache.json")
        self._env = os.environ.get("HELM_CATALOG")
        os.environ["HELM_CATALOG"] = "scanner"

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(catalog, k, v)
        if self._env is None:
            os.environ.pop("HELM_CATALOG", None)
        else:
            os.environ["HELM_CATALOG"] = self._env
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_truncated_cached_id_repaired_on_cache_hit(self):
        d = os.path.join(catalog.CODEX_ROOTS[0], "2026")
        os.makedirs(d)
        path = os.path.join(d, f"rollout-2026-07-11T15-30-00-{UUID_DIGITS}.jsonl")
        with open(path, "w") as f:
            f.write(json.dumps({"timestamp": "2026-07-11T15:30:00Z", "cwd": "/w",
                                "text": "a codex session doing codex things"}) + "\n")
            f.write(json.dumps({"text": "y" * 300}) + "\n")
        st = os.stat(path)
        truncated = UUID_DIGITS.split("-", 1)[1]  # what the greedy regex left
        os.makedirs(catalog.CACHE_DIR)
        with open(catalog.CACHE, "w") as f:
            json.dump({path: {"mt": int(st.st_mtime), "mtns": st.st_mtime_ns,
                              "sz": st.st_size,
                              "row": {"h": "codex", "i": truncated, "c": "/w",
                                      "b": "", "t": "t", "z": st.st_size, "m": 2,
                                      "cr": "2026-07-11", "u": "2026-07-11",
                                      "mt": int(st.st_mtime), "p": path,
                                      "cwd": "/w"}}}, f)
        rows, stats = catalog.build()
        self.assertEqual(stats["rescanned"], 0)  # cache HIT — repair, not rescan
        self.assertEqual([r["i"] for r in rows if r["h"] == "codex"], [UUID_DIGITS])


class CacheRootTest(unittest.TestCase):
    """HELM_CACHE_DIR moves the catalog's cache root, as it moves
    registry.cache_root(); unset, the root is ~/.cache/helm. Read in a fresh
    interpreter, because the root is fixed when the module is imported."""

    def _root(self, env):
        import subprocess, sys
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out = subprocess.run(
            [sys.executable, "-c",
             "from helm import catalog, registry; "
             "print(catalog.CACHE_DIR); print(catalog.SYN_CACHE); "
             "print(registry.cache_root())"],
            cwd=repo, env=env, capture_output=True, text=True, check=True)
        return out.stdout.splitlines()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("HELM_CACHE_DIR", "MELD_CACHE_DIR")}
        self.env["HOME"] = os.path.join(self.tmp, "home")

    def test_override_moves_the_root_and_matches_the_registry(self):
        moved = os.path.join(self.tmp, "moved")
        cache_dir, syn, registry_root = self._root(
            dict(self.env, HELM_CACHE_DIR=moved))
        self.assertEqual(cache_dir, moved)
        self.assertEqual(syn, os.path.join(moved, "syn-cache.json"))
        self.assertEqual(cache_dir, registry_root)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "home", ".cache")))

    def test_unset_keeps_the_default_root(self):
        cache_dir, _, registry_root = self._root(self.env)
        self.assertEqual(cache_dir,
                         os.path.join(self.tmp, "home", ".cache", "helm"))
        self.assertEqual(cache_dir, registry_root)


if __name__ == "__main__":
    unittest.main()
