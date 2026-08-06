#!/usr/bin/env python3
"""Lineage tests — everything runs against a synthetic registry under a tmp
HELM_HOME. The real ~/.helm is never read or written."""
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest

from helm import home, lineage, registry


def _proj(name, path, status="active", edges=None, external=False):
    rec = {"name": name, "path": path, "kind": "dir", "status": status,
           "sessions": {}, "edges": edges or []}
    if external:
        rec["external"] = True
        rec["status"] = "external"
    return rec


class LineageBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="helm-lineage-test-")
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        old = os.environ.get("HELM_HOME")
        os.environ["HELM_HOME"] = self.tmp

        def restore():
            if old is None:
                os.environ.pop("HELM_HOME", None)
            else:
                os.environ["HELM_HOME"] = old
        self.addCleanup(restore)
        # hard guard: every registry write in these tests lands under tmp
        self.assertTrue(home.helm_home().startswith(self.tmp))

    def _dir(self, *parts):
        p = os.path.join(self.tmp, *parts)
        os.makedirs(p, exist_ok=True)
        return p

    def _save(self, projects):
        registry.save({"version": 1, "projects": {p["name"]: p for p in projects}})


class TestApplySeed(LineageBase):
    def _seed(self):
        return {
            "edges": [
                {"src": "beta", "rel": "descends-from", "dst": "alpha", "note": "n1"},
                {"src": "beta", "rel": "forked-from", "dst": "vendor", "note": "n2"},
                {"src": "beta", "rel": "composes", "dst": "ghost", "note": ""},
            ],
            "external": [
                {"name": "vendor", "path": self._dir("src", "vendor"), "note": "prior art"},
                {"name": "missing", "path": os.path.join(self.tmp, "nope"), "note": ""},
            ],
        }

    def test_apply_and_idempotency(self):
        self._save([_proj("alpha", self._dir("src", "alpha")),
                    _proj("beta", self._dir("src", "beta"))])
        r1 = lineage.apply_seed(self._seed())
        self.assertEqual(r1["externals"], ["vendor"])
        self.assertEqual([s["name"] for s in r1["externals_skipped"]], ["missing"])
        self.assertIn("path missing", r1["externals_skipped"][0]["reason"])
        # externals apply BEFORE edges: the vendor edge lands in the same pass
        self.assertEqual(sorted(r1["edges"]),
                         ["beta -descends-from-> alpha", "beta -forked-from-> vendor"])
        self.assertEqual(r1["edges_skipped"],
                         [{"src": "beta", "rel": "composes", "dst": "ghost", "missing": ["ghost"]}])
        reg = registry.load()["projects"]
        self.assertEqual(len(reg["beta"]["edges"]), 2)
        self.assertTrue(reg["vendor"]["external"])
        self.assertNotIn("ghost", reg)

        r2 = lineage.apply_seed(self._seed())  # double-apply: no dup edges/externals
        self.assertEqual(r2["externals"], [])
        self.assertIn({"name": "vendor", "reason": "already in registry"},
                      r2["externals_skipped"])
        reg = registry.load()["projects"]
        self.assertEqual(len(reg["beta"]["edges"]), 2)
        self.assertEqual(len([n for n in reg if n == "vendor"]), 1)

    def test_seed_prefers_the_owner_home_over_the_repo(self):
        """The seed is OWNER DATA and lives in ~/.helm, not in the tree.

        This test used to open lineage.SEED_PATH and assert the owner's REAL
        graph — several private project names and their edges — as literals in
        tracked test code. So untracking the data file alone would have left the
        same private map sitting in the test suite: the leak one layer over from
        the one an independent review found in the as-public adjudication.

        The guard below caught this docstring too, on its first run, because my
        first draft explained the fix BY NAMING the projects it removes. An
        exemption for the author's own prose is how the rule dies.

        What is worth pinning is the CONTRACT, and a synthetic seed pins it
        without publishing anybody's portfolio."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            prior = os.environ.get("HELM_HOME")
            os.environ["HELM_HOME"] = tmp
            try:
                from helm import home
                owned = os.path.join(home.helm_home(), home.GLOBAL,
                                     "lineage_seed.json")
                os.makedirs(os.path.dirname(owned), exist_ok=True)
                with open(owned, "w", encoding="utf-8") as f:
                    json.dump({"edges": [{"src": "alpha", "rel": "descends-from",
                                          "dst": "beta", "note": "synthetic",
                                          "confirmed": True}],
                               "external": [{"name": "gamma"}]}, f)
                self.assertEqual(lineage.seed_path(), owned)
                seed = json.load(open(owned, encoding="utf-8"))
                for e in seed["edges"]:
                    for k in ("src", "rel", "dst", "note"):
                        self.assertIn(k, e)
                    self.assertTrue(e["confirmed"])
            finally:
                if prior is None:
                    os.environ.pop("HELM_HOME", None)
                else:
                    os.environ["HELM_HOME"] = prior

    def test_an_absent_seed_is_a_supported_state_not_a_crash(self):
        """No owner seed and no repo copy must degrade to the empty default —
        which is what a fresh clone by anyone other than the owner looks like."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            prior = os.environ.get("HELM_HOME")
            os.environ["HELM_HOME"] = tmp
            try:
                p = lineage.seed_path()
                from helm import pk
                self.assertEqual(pk.read_json(p, {"edges": [], "external": []}),
                                 {"edges": [], "external": []})
            finally:
                if prior is None:
                    os.environ.pop("HELM_HOME", None)
                else:
                    os.environ["HELM_HOME"] = prior

    def test_no_private_project_name_is_hardcoded_in_this_suite(self):
        """The regression guard for the leak this commit closes: the test file
        must not carry the owner's private project graph as literals."""
        with open(__file__, encoding="utf-8") as f:
            body = f.read()
        # names the as-public adjudication flagged; checked as whole words
        for name in ("internal-tool-beta", "sample-project", "sample-provider"):
            hits = [ln for ln in body.splitlines()
                    if name in ln and "adjudication" not in ln
                    and not ln.strip().startswith("#")
                    and "for name in" not in ln]
            self.assertEqual(hits, [], "private name %r is back in the suite" % name)


class TestRender(LineageBase):
    def test_tree_connectors_tags_unmapped(self):
        e_kid = [
            {"rel": "descends-from", "to": "origin", "note": "the note", "confirmed": True},
            {"rel": "forked-from", "to": "vendor", "note": "", "confirmed": True},
            {"rel": "supersedes", "to": "solo", "note": "old brand", "confirmed": True},
        ]
        self._save([
            _proj("origin", self._dir("src", "origin"), status="dormant"),
            _proj("kid", self._dir("src", "kid"), edges=e_kid),
            _proj("vendor", self._dir("src", "vendor"), external=True),
            _proj("solo", self._dir("src", "solo")),
        ])
        out = lineage.render()
        self.assertIn("origin [dormant]", out)
        self.assertIn("─ kid (descends-from) — the note", out)
        self.assertIn("vendor [external]", out)
        self.assertIn("· supersedes solo — old brand", out)
        self.assertIn("(see above)", out)  # kid appears under both origin and vendor
        self.assertIn("unmapped (no lineage edges):", out)
        self.assertIn("  solo", out)
        with self.assertRaises(ValueError):
            lineage.render(fmt="dot")


class TestArchiveReport(LineageBase):
    def test_ranking_and_verdicts(self):
        live = self._dir("src", "livewire")
        dupe = self._dir("src", "dupe")
        dusty = self._dir("src", "dusty")
        elder = self._dir("src", "elder")
        subprocess.run(["git", "init", "-q", dusty], check=True)
        with open(os.path.join(dusty, "stray.txt"), "w") as f:
            f.write("uncommitted\n")
        self._save([
            _proj("livewire", live,
                  edges=[{"rel": "descends-from", "to": "elder", "note": "", "confirmed": True}]),
            _proj("dupe", dupe, status="dormant",
                  edges=[{"rel": "checkout-of", "to": "livewire", "note": "", "confirmed": True}]),
            _proj("dusty", dusty, status="dormant"),
            _proj("elder", elder, status="dormant"),
            _proj("vendorx", self._dir("src", "vendorx"), external=True),
        ])
        rows = lineage.archive_report()
        by_name = {r["name"]: r for r in rows}
        self.assertNotIn("livewire", by_name)  # active: never a candidate
        self.assertNotIn("vendorx", by_name)   # external: never a candidate

        self.assertEqual(rows[0]["name"], "dupe")  # checkout dup of an active = safest
        self.assertEqual(rows[0]["verdict"], "safe")
        self.assertTrue(any("checkout duplicate of active 'livewire'" in w
                            for w in rows[0]["why_safe"]))
        self.assertIsInstance(rows[0]["disk_mb"], int)

        self.assertEqual(by_name["dusty"]["verdict"], "hold")
        self.assertTrue(any("uncommitted changes" in w for w in by_name["dusty"]["why_not"]))

        self.assertEqual(by_name["elder"]["verdict"], "hold")  # lineage anchor of livewire
        self.assertTrue(any("livewire" in w for w in by_name["elder"]["why_not"]))


class TestCmd(LineageBase):
    def _run(self, args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = lineage.cmd_lineage(args)
        return rc, buf.getvalue()

    def test_verbs(self):
        self._save([_proj("alpha", self._dir("src", "alpha")),
                    _proj("beta", self._dir("src", "beta"), status="dormant")])
        rc, out = self._run([])
        self.assertEqual(rc, 0)
        self.assertIn("unmapped", out)

        rc, out = self._run(["add", "beta", "launched-as", "alpha", "public", "launch"])
        self.assertEqual(rc, 0)
        e = registry.load()["projects"]["beta"]["edges"][0]
        self.assertEqual((e["rel"], e["to"], e["note"]), ("launched-as", "alpha", "public launch"))

        ext = self._dir("src", "ext")
        rc, out = self._run(["external", "extra", ext, "vendored"])
        self.assertEqual(rc, 0)
        self.assertTrue(registry.load()["projects"]["extra"]["external"])

        rc, out = self._run(["archive-report"])
        self.assertEqual(rc, 0)
        self.assertIn("READ-ONLY", out)
        self.assertIn("beta", out)

        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(lineage.cmd_lineage(["bogus"]), 2)
            self.assertEqual(lineage.cmd_lineage(["add", "beta"]), 2)


if __name__ == "__main__":
    unittest.main()
