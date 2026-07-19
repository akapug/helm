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


if __name__ == "__main__":
    unittest.main()
