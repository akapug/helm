#!/usr/bin/env python3
"""Registry-split tests — the projection (registry.json) / authored
(registry-authored.json) split. Hermetic: tmp HELM_HOME, observations injected,
scan roots pinned to an empty dir, the tmp-cwd noise filter stubbed so tmp
paths can register. The real ~/.helm is never read or written."""
import os
import tempfile
import unittest
from unittest import mock

from helm import automap, home, pk, registry

EDGE = {"rel": "forked-from", "to": "beta", "note": "", "confirmed": True}


def _obs(cwd):
    return {"cwd": cwd, "harness": "claude", "sessions": 3,
            "last_seen": 1900000000.0, "refs": [], "days": {"2026-07-01"}}


class RegistryBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="helm-registry-test-")
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        env = {k: os.environ.get(k) for k in ("HELM_HOME", "MELD_HOME", "HELM_SCAN_ROOTS")}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm-home")
        os.environ["HELM_SCAN_ROOTS"] = self._dir("scan-root")  # empty: no shelf tier
        os.environ.pop("MELD_HOME", None)

        def restore():
            for k, v in env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(restore)
        # hard guard: every write in these tests lands under tmp
        self.assertTrue(home.helm_home().startswith(self.tmp))
        noise = mock.patch.object(automap, "_is_noise", lambda cwd: False)
        noise.start()
        self.addCleanup(noise.stop)

    def _dir(self, *parts):
        p = os.path.join(self.tmp, *parts)
        os.makedirs(p, exist_ok=True)
        return p

    def _repo(self, *parts):
        p = self._dir(*parts)
        os.makedirs(os.path.join(p, ".git"), exist_ok=True)  # promotes without real git
        return p

    def _bytes(self, path):
        with open(path, "rb") as f:
            return f.read()


class TestSplit(RegistryBase):
    def test_save_splits_load_merges_shape_unchanged(self):
        path = self._dir("src", "alpha")
        rec = {"name": "alpha", "path": path, "kind": "git", "status": "active",
               "sessions": {}, "notes": "born of beta", "aliases": ["al"],
               "edges": [dict(EDGE)]}
        registry.save({"version": 1, "projects": {"alpha": dict(rec)}})
        raw = pk.read_json(home.registry_path())["projects"]["alpha"]
        for k in registry.AUTHORED_FIELDS:
            self.assertNotIn(k, raw)  # projection file carries nothing authored
        entry = pk.read_json(home.authored_path())["projects"]["alpha"]
        self.assertEqual(entry["edges"], rec["edges"])
        self.assertEqual(entry["path"], path)  # path-stamped for the collision guard
        merged = registry.load()["projects"]["alpha"]
        self.assertEqual(merged, rec)  # public API shape: exactly the pre-split record

    def test_partial_save_never_deletes_other_entries(self):
        registry.save({"version": 1, "projects": {
            "alpha": {"name": "alpha", "path": "/a", "notes": "keep me"}}})
        registry.save({"version": 1, "projects": {
            "beta": {"name": "beta", "path": "/b", "notes": "new"}}})
        entries = pk.read_json(home.authored_path())["projects"]
        self.assertEqual(entries["alpha"]["notes"], "keep me")
        self.assertEqual(entries["beta"]["notes"], "new")


class TestMigration(RegistryBase):
    def test_moves_once_idempotent_and_faithful(self):
        path = self._dir("src", "alpha")
        pk.write_json(home.registry_path(), {"version": 1, "projects": {"alpha": {
            "name": "alpha", "path": path, "kind": "git", "status": "active",
            "sessions": {}, "notes": "née melds — ünïcode", "retired": False,
            "edges": [dict(EDGE)]}}})
        self.assertFalse(os.path.exists(home.authored_path()))
        merged = registry.load()["projects"]["alpha"]
        self.assertEqual(merged["notes"], "née melds — ünïcode")
        self.assertEqual(merged["edges"], [EDGE])
        raw = pk.read_json(home.registry_path())["projects"]["alpha"]
        for k in registry.AUTHORED_FIELDS:
            self.assertNotIn(k, raw)
        entry = pk.read_json(home.authored_path())["projects"]["alpha"]
        self.assertEqual(entry["notes"], "née melds — ünïcode")
        self.assertIs(entry["retired"], False)  # falsy values move too, verbatim
        frozen = (self._bytes(home.registry_path()), self._bytes(home.authored_path()))
        registry.load()  # second load: byte-identical files, no re-migration
        self.assertEqual(
            (self._bytes(home.registry_path()), self._bytes(home.authored_path())), frozen)

    def test_never_clobbers_already_authored_values(self):
        path = self._dir("src", "alpha")
        pk.write_json(home.authored_path(), {"version": 1, "projects": {
            "alpha": {"path": path, "notes": "the live note"}}})
        pk.write_json(home.registry_path(), {"version": 1, "projects": {"alpha": {
            "name": "alpha", "path": path, "notes": "stale backup note",
            "edges": [dict(EDGE)]}}})
        merged = registry.load()["projects"]["alpha"]
        self.assertEqual(merged["notes"], "the live note")  # authored file outranks
        self.assertEqual(merged["edges"], [EDGE])           # new field still migrates

    def test_path_mismatch_never_grafts(self):
        # a same-name entry authored against another path: overlay + migration both refuse
        pk.write_json(home.authored_path(), {"version": 1, "projects": {
            "proj": {"path": "/somewhere/else/proj", "notes": "not yours",
                     "edges": [dict(EDGE)]}}})
        pk.write_json(home.registry_path(), {"version": 1, "projects": {"proj": {
            "name": "proj", "path": "/fresh/proj", "kind": "git", "sessions": {},
            "notes": "mine"}}})
        merged = registry.load()["projects"]["proj"]
        self.assertEqual(merged["notes"], "mine")
        self.assertNotIn("edges", merged)
        entry = pk.read_json(home.authored_path())["projects"]["proj"]
        self.assertEqual(entry["notes"], "not yours")  # foreign entry untouched


class TestSyncSurvival(RegistryBase):
    def test_authored_survives_projection_wipe_and_resync(self):
        repo = self._repo("src", "alpha")
        registry.sync(observations=[_obs(repo)])
        registry.add_edge("alpha", "forked-from", "beta", "the ancestry")
        registry.add_external("vendor", self._dir("src", "vendor"), "prior art")
        os.remove(home.registry_path())
        vendor = registry.load()["projects"]["vendor"]  # external anchor materializes
        self.assertTrue(vendor["external"])
        self.assertEqual(vendor["notes"], "prior art")
        reg, _ = registry.sync(observations=[_obs(repo)])
        self.assertEqual(reg["projects"]["alpha"]["edges"][0]["to"], "beta")
        self.assertTrue(reg["projects"]["vendor"]["external"])
        raw = pk.read_json(home.registry_path())["projects"]
        self.assertNotIn("edges", raw["alpha"])  # regenerated projection stays pure

    def test_collision_mints_fresh_name_cannot_inherit(self):
        a = self._repo("one", "proj")
        b = self._repo("two", "proj")
        registry.sync(observations=[_obs(a)])
        registry.add_edge("proj", "forked-from", "elder", "authored on the incumbent")
        reg, report = registry.sync(observations=[_obs(b)])
        by_path = {r["path"]: n for n, r in reg["projects"].items()}
        minted = by_path[b]
        self.assertEqual(by_path[a], "proj")
        self.assertNotEqual(minted, "proj")
        self.assertIn(minted, report["new"])
        self.assertFalse(reg["projects"][minted].get("edges"))
        self.assertEqual(reg["projects"]["proj"]["edges"][0]["to"], "elder")
        self.assertEqual(sorted(pk.read_json(home.authored_path())["projects"]), ["proj"])


class TestReviewHardening(RegistryBase):
    """Cross-family (codex-seat) review findings, 2026-07-19 — pinned."""

    def test_save_never_clobbers_path_mismatched_authored_entry(self):
        # incumbent authored at path A; a merged view carrying same-NAME
        # authored fields at path B must not delete A's entry on save
        pk.write_json(home.authored_path(), {"version": 1, "projects": {
            "proj": {"path": "/elsewhere/proj", "notes": "the unrebuildable note"}}})
        reg = {"version": 1, "projects": {
            "proj": {"name": "proj", "path": "/here/proj", "kind": "git",
                     "status": "active", "sessions": {},
                     "edges": [{"rel": "forked-from", "to": "elder"}]}}}
        registry.save(reg)
        auth = pk.read_json(home.authored_path())["projects"]
        self.assertEqual(auth["proj"]["notes"], "the unrebuildable note")  # survived
        q = registry._qualified("proj", "/here/proj")
        self.assertEqual(auth[q]["edges"][0]["to"], "elder")  # newcomer parked
        # and load() resolves the newcomer via the qualified key
        merged = registry.load()["projects"]["proj"]
        self.assertEqual(merged["edges"][0]["to"], "elder")

    def test_corrupt_authored_file_backed_up_before_any_save(self):
        os.makedirs(os.path.dirname(home.authored_path()), exist_ok=True)
        with open(home.authored_path(), "w") as f:
            f.write("{ this is not json")
        registry.save({"version": 1, "projects": {}})
        siblings = [n for n in os.listdir(os.path.dirname(home.authored_path()))
                    if n.startswith(os.path.basename(home.authored_path()) + ".corrupt-")]
        self.assertEqual(len(siblings), 1)  # the garbled bytes survive for repair
        bak = os.path.join(os.path.dirname(home.authored_path()), siblings[0])
        with open(bak) as f:
            self.assertIn("this is not json", f.read())
        self.assertIsInstance(pk.read_json(home.authored_path()), dict)  # rewritten clean


if __name__ == "__main__":
    unittest.main()
