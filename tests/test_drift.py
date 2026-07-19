#!/usr/bin/env python3
"""drift tests — hermetic (the test_store pattern): every root points at a
tempdir via HELM_HOME + HELM_ADOPTED_DIR; real stores never touched."""
import os
import shutil
import tempfile
import unittest

os.environ.setdefault("HELM_HOME", tempfile.mkdtemp(prefix="helm-test-home-"))

from helm import drift, home, store  # noqa: E402

TS = "2026-07-18T00:00:00Z"


class DriftBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-drift-")
        self.adopted = os.path.join(self.tmp, "adopted")
        os.makedirs(self.adopted)
        self.env_prior = {k: os.environ.get(k)
                          for k in ("HELM_HOME", "HELM_ADOPTED_DIR")}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = self.adopted

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def seed(self, pid, conf, root_dir=None, **kw):
        e = {"id": pid, "statement": "s-" + pid, "confidence": conf,
             "stated_ts": TS, "source": "human"}
        e.update(kw)
        return store.write_prior(
            e, root_dir=root_dir or os.path.join(home.global_dir(), "premises"))


class TierCrossingTest(DriftBase):
    def test_genuine_crossing_reported_exactly_once(self):
        p = self.seed("tierx", 0.9)
        lines, _ = drift.report()          # first run: baseline, no prev
        self.assertEqual(lines, [])
        store.write_prior({"id": "tierx", "statement": "s-tierx",
                          "confidence": 0.5, "stated_ts": TS}, path=p)
        lines, _ = drift.report()
        self.assertEqual(len(lines), 1)
        self.assertIn("TIER", lines[0])
        self.assertIn("fell below auto-act", lines[0])
        lines, _ = drift.report()          # exactly once: steady on rerun
        self.assertEqual(lines, [])


class ScopeKeyedSnapshotTest(DriftBase):
    """Audit HIGH: one scope-blind snapshot let a --project run poison the
    global baseline and mint tier-crossings that never happened."""

    def _shadowed_pair(self):
        # adopted (global-visible) tierx=0.5, project override 0.9
        self.seed("tierx", 0.5, root_dir=self.adopted)
        self.seed("tierx", 0.9,
                  root_dir=os.path.join(home.project_dir("myproj"), "premises"))

    def test_project_run_does_not_poison_the_global_snapshot(self):
        self._shadowed_pair()
        lines, _ = drift.report()                   # global baseline: 0.5
        self.assertEqual(lines, [])
        lines, _ = drift.report(project="myproj")   # project run: 0.9 (shadowed)
        self.assertEqual(lines, [])
        # pre-fix, the project run overwrote the global snapshot with 0.9 and
        # this global run minted a phantom "fell below auto-act (0.90 -> 0.50)"
        lines, _ = drift.report()
        self.assertEqual(lines, [])

    def test_global_run_does_not_poison_the_project_snapshot(self):
        self._shadowed_pair()
        lines, _ = drift.report(project="myproj")   # project baseline: 0.9
        self.assertEqual(lines, [])
        lines, _ = drift.report()                   # global run: 0.5
        self.assertEqual(lines, [])
        lines, _ = drift.report(project="myproj")   # no phantom rise/fall
        self.assertEqual(lines, [])

    def test_snapshot_files_are_scope_keyed(self):
        self._shadowed_pair()
        drift.report()
        drift.report(project="myproj")
        state = os.path.join(home.global_dir(), ".state")
        self.assertTrue(os.path.isfile(os.path.join(state, "drift-snapshot.json")))
        self.assertTrue(os.path.isfile(
            os.path.join(state, "drift-snapshot-myproj.json")))

    def test_peek_never_snapshots(self):
        self.seed("tierx", 0.9)
        lines, _ = drift.report(snapshot=False)
        self.assertEqual(lines, [])
        self.assertFalse(os.path.isfile(drift._state_path()))


class FindingsFeedTest(DriftBase):
    """findings() is the structured feed evolve mints commands from — pin the
    row shapes and that report() is exactly its rendering."""

    def test_rows_carry_what_a_command_needs(self):
        self.seed("cert-x", 1.0, evidence_log=[
            {"ts": TS, "type": "contradict", "delta": -0.1,
             "reason": "agents disagree", "by": "agent"}])
        self.seed("dorm", 0.3)
        self.seed("tierx", 0.9)
        drift.report()
        self.seed("tierx", 0.5)
        rows, n = drift.findings(snapshot=False)
        self.assertEqual(n, 3)
        self.assertEqual(rows, [
            {"kind": "contradicted", "id": "cert-x", "n": 1,
             "latest": "agents disagree"},
            {"kind": "decayed", "id": "dorm", "conf": 0.3},
            {"kind": "tier", "id": "tierx", "tier": "auto-act", "dir": "fell",
             "was": 0.9, "now": 0.5}])
        lines, _ = drift.report(snapshot=False)
        self.assertEqual(lines, [drift._line(f) for f in rows])


if __name__ == "__main__":
    unittest.main()
