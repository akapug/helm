#!/usr/bin/env python3
"""sweep tests — the lineage-driven supersession sweep. Pins the double gate
(a directed succession edge AND same-slug/high-overlap), the propose-only law
(dry-run mutates nothing), the --apply path through store.mark_superseded
(tombstone + file kept + idempotent), the weak-signal refusals (term-mention
only, no edge, below-overlap, both-era entries), rel selection (checkout-of
excluded, descends-from included), ranking, and fail-open on an empty world.
Hermetic: tmp HELM_HOME + HELM_ADOPTED_DIR throughout."""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("HELM_HOME", tempfile.mkdtemp(prefix="helm-test-home-"))

from helm import home, pk, registry, store, sweep  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_ADOPTED_DIR")


def _proj(name, edges=None, status="active"):
    return {"name": name, "path": "/x/" + name, "kind": "dir", "status": status,
            "sessions": {}, "edges": edges or []}


def _edge(rel, to):
    return {"rel": rel, "to": to, "note": "", "confirmed": True}


def _snapshot(root):
    out = {}
    for dp, _d, files in os.walk(root):
        for f in files:
            p = os.path.join(dp, f)
            st = os.stat(p)
            out[os.path.relpath(p, root)] = (st.st_size, st.st_mtime_ns)
    return out


class SweepBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-sweep-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.adopted = os.path.join(self.tmp, "adopted")
        os.environ["HELM_ADOPTED_DIR"] = self.adopted
        os.makedirs(self.adopted)

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _registry(self, *projects):
        registry.save({"version": 1, "projects": {p["name"]: p for p in projects}})

    def _prior(self, eid, statement, keywords=""):
        store.write_prior({"id": eid, "statement": statement, "keywords": keywords,
                           "confidence": ""}, root_dir=self.adopted)

    def _reference(self, eid, statement, keywords=""):
        store.write_reference({"id": eid, "statement": statement, "keywords": keywords},
                              root_dir=self.adopted)

    def _run(self, args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = sweep.cmd_sweep(args)
        return rc, buf.getvalue()

    def _by_slug(self, eid, include_retired=True):
        want = pk.slug(eid)
        for e in store.load_all(include_retired=include_retired):
            if pk.slug(str(e["id"])) == want:
                return e
        return None


class ProposeTest(SweepBase):
    def test_same_slug_supersedes_across_a_supersedes_edge(self):
        self._registry(_proj("sesh"), _proj("helm", [_edge("supersedes", "sesh")]))
        self._prior("sesh-rollover-policy", "on rollover re-home the sesh session")
        self._prior("helm-rollover-policy", "on rollover helm re-homes the session")
        props = sweep.propose()
        self.assertEqual(len(props), 1)
        p = props[0]
        self.assertEqual((p["old_id"], p["new_id"]), ("sesh-rollover-policy",
                                                      "helm-rollover-policy"))
        self.assertEqual(p["signal"], "same-slug")
        self.assertEqual((p["successor"], p["rel"], p["ancestor"]),
                         ("helm", "supersedes", "sesh"))
        self.assertIn("same base slug 'rollover-policy'", p["reason"])
        self.assertTrue(p["old_stmt"] and p["new_stmt"])   # quoted evidence carried

    def test_descends_from_edge_also_drives_the_sweep(self):
        self._registry(_proj("sesh"), _proj("helm", [_edge("descends-from", "sesh")]))
        self._prior("sesh-token-refresh", "refresh the sesh token")
        self._prior("helm-token-refresh", "helm refreshes the token")
        props = sweep.propose()
        self.assertEqual([p["old_id"] for p in props], ["sesh-token-refresh"])
        self.assertEqual(props[0]["rel"], "descends-from")

    def test_high_overlap_pairs_when_base_differs(self):
        kw = "rollover,account,freshest,headroom,session"
        self._registry(_proj("sesh"), _proj("helm", [_edge("supersedes", "sesh")]))
        self._prior("sesh-token-refresh", "swap when the account runs dry", kw)
        self._prior("helm-account-swap", "swap when the account runs dry", kw)
        props = sweep.propose()
        self.assertEqual(len(props), 1)
        self.assertEqual(props[0]["signal"], "high-overlap")
        self.assertIn("keyword overlap", props[0]["reason"])

    def test_reference_type_is_supersedable_too(self):
        self._registry(_proj("sesh"), _proj("helm", [_edge("supersedes", "sesh")]))
        self._reference("sesh-doc-link", "the sesh handbook")
        self._reference("helm-doc-link", "the helm handbook")
        props = sweep.propose()
        self.assertEqual(len(props), 1)
        self.assertEqual(props[0]["type"], "reference")

    def test_same_slug_ranks_before_high_overlap(self):
        kw = "rollover,account,freshest,headroom,session"
        self._registry(_proj("sesh"), _proj("helm", [_edge("supersedes", "sesh")]))
        self._prior("sesh-rollover-policy", "sesh rollover", "rollover,policy")
        self._prior("helm-rollover-policy", "helm rollover", "rollover,policy")
        self._prior("sesh-token-refresh", "swap when dry", kw)
        self._prior("helm-account-swap", "swap when dry", kw)
        props = sweep.propose()
        self.assertEqual([p["signal"] for p in props], ["same-slug", "high-overlap"])


class WeakSignalTest(SweepBase):
    def test_no_edge_no_proposal_even_with_same_slug(self):
        self._registry(_proj("sesh"), _proj("helm"))   # NO succession edge
        self._prior("sesh-rollover-policy", "sesh rollover")
        self._prior("helm-rollover-policy", "helm rollover")
        self.assertEqual(sweep.propose(), [])

    def test_term_mention_without_a_twin_never_fires(self):
        self._registry(_proj("sesh"), _proj("helm", [_edge("supersedes", "sesh")]))
        self._prior("sesh-only-note", "a lone sesh-era note, no successor twin")
        self._prior("helm-unrelated-thing", "a totally different helm topic")
        self.assertEqual(sweep.propose(), [])

    def test_below_overlap_threshold_does_not_fire(self):
        self._registry(_proj("sesh"), _proj("helm", [_edge("supersedes", "sesh")]))
        self._prior("sesh-widget", "one", "apple,banana,cherry,date,elder")
        self._prior("helm-gadget", "two", "apple,fig,grape,kiwi,lemon")
        self.assertEqual(sweep.propose(), [])

    def test_entry_tagged_with_both_eras_is_excluded(self):
        self._registry(_proj("sesh"), _proj("helm", [_edge("supersedes", "sesh")]))
        # mentions BOTH harnesses -> ambiguous era -> never an old candidate
        self._prior("helm-uses-sesh-bridge", "helm still reads the sesh bridge")
        self._prior("helm-uses-x-bridge", "helm reads the x bridge")
        props = sweep.propose()
        self.assertNotIn("helm-uses-sesh-bridge", [p["old_id"] for p in props])

    def test_checkout_of_is_not_a_succession(self):
        self._registry(_proj("polyana"),
                       _proj("polyanna", [_edge("checkout-of", "polyana")]))
        self._prior("polyana-run-loop", "the polyana run loop")
        self._prior("polyanna-run-loop", "the polyanna run loop")
        self.assertEqual(sweep.propose(), [])


class ApplyTest(SweepBase):
    def _seed_one(self):
        self._registry(_proj("sesh"), _proj("helm", [_edge("supersedes", "sesh")]))
        self._prior("sesh-rollover-policy", "sesh rollover")
        self._prior("helm-rollover-policy", "helm rollover")

    def test_apply_tombstones_old_keeps_file_and_is_idempotent(self):
        self._seed_one()
        r = sweep.apply(sweep.propose())
        self.assertEqual((r["applied"], r["errors"]), (1, 0))
        old = self._by_slug("sesh-rollover-policy")
        self.assertEqual(old["status"], store.STATUS_DELETE_ELIGIBLE)
        self.assertEqual(pk.slug(str(old["replaced_by"])), pk.slug("helm-rollover-policy"))
        self.assertTrue(os.path.isfile(
            os.path.join(self.adopted, "prior-sesh-rollover-policy.md")))  # never deleted
        # superseded old drops out of the live view -> a second sweep is empty
        self.assertEqual(sweep.propose(), [])

    def test_dry_run_mutates_nothing(self):
        self._seed_one()
        before = _snapshot(self.tmp)
        rc, out = self._run([])
        self.assertEqual(rc, 0)
        self.assertIn("PROPOSE-ONLY", out)
        self.assertIn("DRY-RUN", out)
        self.assertIn("helm store supersede", out)
        self.assertIn('old: "', out)
        self.assertEqual(_snapshot(self.tmp), before)   # not one byte written

    def test_cmd_apply_reports_receipts(self):
        self._seed_one()
        rc, out = self._run(["--apply"])
        self.assertEqual(rc, 0)
        self.assertIn("APPLIED 1 supersession", out)
        self.assertIn("sesh-rollover-policy -> helm-rollover-policy", out)
        self.assertNotIn("DRY-RUN", out)


class FailOpenTest(SweepBase):
    def test_no_registry_is_a_clean_line(self):
        rc, out = self._run([])
        self.assertEqual(rc, 0)
        self.assertIn("clean against the family tree", out)

    def test_edges_but_empty_store(self):
        self._registry(_proj("sesh"), _proj("helm", [_edge("supersedes", "sesh")]))
        self.assertEqual(sweep.propose(), [])
        rc, out = self._run([])
        self.assertEqual(rc, 0)
        self.assertIn("no lineage-superseded entries", out)

    def test_project_flag_needs_a_name(self):
        rc, _ = self._run(["--project"])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
