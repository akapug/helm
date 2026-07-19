#!/usr/bin/env python3
"""evolve tests — the self-evolution composition layer. Pins the five observer
return shapes proposals() composes, the propose-only law (no mutation beyond
the documented registry-sync write/scaffold — here the observers are mocked, so
NO write at all is permitted), the drift snapshot=False contract (evolve must
never consume a pending drift report), and the steady-state quiet line.
Hermetic: tmp HELM_HOME, every observer stubbed."""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("HELM_HOME", tempfile.mkdtemp(prefix="helm-test-home-"))

from helm import drain, drift, evolve, registry, whoami  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_ADOPTED_DIR")

EMPTY_PROFILE = {"technical_level": "", "guidance": ""}


def snapshot(root):
    """{relpath: (size, mtime_ns)} for every file under root."""
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            p = os.path.join(dirpath, f)
            st = os.stat(p)
            out[os.path.relpath(p, root)] = (st.st_size, st.st_mtime_ns)
    return out


class EvolveBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-evolve-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def with_observers(self, fn, sync_new=(), classify=(), drift_lines=(),
                       profile=None, notes=""):
        """Run fn with all five observers stubbed; returns (result, seen)."""
        seen = {}

        def fake_drift_report(snapshot=True):
            seen["drift_snapshot"] = snapshot
            return list(drift_lines), None

        with mock.patch.object(registry, "sync",
                               return_value=({"projects": {}},
                                             {"new": list(sync_new)})), \
                mock.patch.object(drain, "classify", return_value=list(classify)), \
                mock.patch.object(drift, "report", fake_drift_report), \
                mock.patch.object(whoami, "load_profile",
                                  return_value=profile or dict(EMPTY_PROFILE)), \
                mock.patch.object(whoami, "load_notes", return_value=notes):
            return fn(), seen

    def run_cmd(self, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            (rc, _), seen = self.with_observers(lambda: (evolve.cmd_evolve([]), None), **kw)
        return rc, out.getvalue(), seen


class ProposalShapeTest(EvolveBase):
    def test_steady_state_is_one_quiet_line(self):
        rc, out, _ = self.run_cmd(profile={"technical_level": "expert",
                                           "guidance": "be terse"})
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "helm evolve: steady — nothing to propose.")

    def test_every_observer_yields_its_proposal(self):
        props, _ = self.with_observers(
            evolve.proposals,
            sync_new=["alpha", "beta"],
            classify=[{"op": "retype"}, {"op": "route-project"},
                      {"op": "sweep-dup"}, {"op": "keep"}],
            drift_lines=["prior-x drifted"])
        self.assertEqual([p[0] for p in props],
                         ["registry", "drain", "drain", "drift", "who"])
        for p in props:
            self.assertEqual(len(p), 3)  # (area, what, verb-or-None)
        by_area = {}
        for area, what, verb in props:
            by_area.setdefault(area, []).append((what, verb))
        self.assertIn("2 new projects discovered: alpha, beta",
                      by_area["registry"][0][0])
        self.assertIsNone(by_area["registry"][0][1])
        self.assertEqual(by_area["drain"][0],
                         ("2 raw entries routable to typed homes",
                          "helm drain --apply"))
        self.assertEqual(by_area["drain"][1][1], "helm drain --apply --sweep-dups")
        self.assertEqual(by_area["drift"][0],
                         ("1 belief drifting", "helm drift"))
        self.assertEqual(by_area["who"][0][1], "helm interview")

    def test_cmd_output_lists_verbs_and_count(self):
        rc, out, _ = self.run_cmd(sync_new=["alpha"], drift_lines=["a", "b"])
        self.assertEqual(rc, 0)
        self.assertIn("helm evolve — 3 proposals (no knowledge edited):", out)
        self.assertIn("[registry] 1 new project discovered: alpha", out)
        self.assertIn("[drift] 2 beliefs drifting  ->  helm drift", out)
        self.assertIn("[who]", out)
        self.assertIn("helm interview", out)

    def test_notes_alone_satisfy_the_warmth_leg(self):
        props, _ = self.with_observers(evolve.proposals, notes="knows the user")
        self.assertEqual(props, [])


class ProposeOnlyTest(EvolveBase):
    def test_propose_only_no_filesystem_mutation(self):
        # with the observers stubbed, the documented sync write is excluded —
        # evolve itself must write NOTHING
        os.makedirs(os.path.join(os.environ["HELM_HOME"], "_global"))
        marker = os.path.join(os.environ["HELM_HOME"], "_global", "keep.md")
        with open(marker, "w") as f:
            f.write("untouched")
        before = snapshot(self.tmp)
        rc, out, _ = self.run_cmd(sync_new=["alpha"],
                                  classify=[{"op": "retype"}],
                                  drift_lines=["x"])
        self.assertEqual(rc, 0)
        self.assertEqual(snapshot(self.tmp), before,
                         "evolve mutated the estate — propose-only broken")

    def test_drift_observer_never_consumes_the_snapshot(self):
        _, seen = self.with_observers(evolve.proposals)
        self.assertIs(seen["drift_snapshot"], False,
                      "evolve must call drift.report(snapshot=False) — anything "
                      "else eats the operator's pending drift report")


if __name__ == "__main__":
    unittest.main()
