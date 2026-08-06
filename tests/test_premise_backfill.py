#!/usr/bin/env python3
"""premise backfill tests — --attest-existing / --attest-sweep append a NATIVE
chain record for entries already in the store (the adopted corpus included),
IN PLACE. Offline + free: no computrons, no faucet, no bearer. Hermetic:
HELM_HOME + the adopted root are tempdirs; the OPTIONAL dregg node points at a
dead port (anchor fails open)."""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-home-", var="HELM_HOME")

from helm import cell, home, premise, store  # noqa: E402

TURN = "ee" * 32
DEAD = "http://127.0.0.1:1"

ADOPTED_RAW = """---
name: prior-corpus-law
description: "premise: corpus-law - The corpus   truth"
metadata:
  node_type: memory
  type: prior
  id: corpus-law
  statement: The corpus   truth with "quotes"  inside
  confidence: 1.0
  class: certain
  status: live
  keywords: corpus,lawful
---

PREMISE: hand-written body   with odd\twhitespace a rewrite would destroy
"""


class BackfillBase(unittest.TestCase):
    ENV_KEYS = ("HELM_HOME", "HELM_ADOPTED_DIR", "HELM_CELL_PROFILE",
                "MELD_AGENT_PROFILE", "HELM_NODE_URL", "MELD_NODE_URL")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-backfill-")
        self.env_prior = {k: os.environ.get(k) for k in self.ENV_KEYS}
        for k in self.ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])
        os.environ["HELM_NODE_URL"] = DEAD
        os.environ["HELM_CELL_PROFILE"] = "owner-cell"

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_verb(self, args, fn=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = (fn or premise.cmd_premise)(list(args))
        return rc, out.getvalue(), err.getvalue()

    def write_adopted(self, raw=ADOPTED_RAW, name="prior-corpus-law.md"):
        p = os.path.join(os.environ["HELM_ADOPTED_DIR"], name)
        with open(p, "w") as f:
            f.write(raw)
        return p

    def write_certain(self, pid, statement, project=None, **extra):
        e = dict({"id": pid, "statement": statement, "confidence": 1.0,
                  "stated_ts": "2026-07-01T00:00:00Z", "source": "human"},
                 **extra)
        d = os.path.join(home.project_dir(project) if project
                         else home.global_dir(), "premises")
        return store.write_prior(e, root_dir=d)

    def raw(self, path):
        with open(path) as f:
            return f.read()

    def queue_rows(self):
        try:
            with open(premise._queue_path()) as f:
                return [json.loads(l) for l in f if l.strip()]
        except OSError:
            return []


class AttestExistingTest(BackfillBase):
    def test_annotates_in_place_byte_diff_is_attest_keys_only(self):
        p = self.write_adopted()
        before = self.raw(p)
        rc, out, _ = self.run_verb(["--attest-existing", "corpus-law"])
        self.assertEqual(rc, 0)
        self.assertIn("attested existing 'corpus-law' [adopted]", out)
        after = self.raw(p)
        added = [l for l in after.split("\n") if l not in before.split("\n")]
        self.assertEqual([l.split(":")[0] for l in added],
                         ["  attest_payload", "  attest_ts", "  attest_by",
                          "  attest_record", "  attest_chain_index"])
        stripped = "\n".join(l for l in after.split("\n") if l not in added)
        self.assertEqual(stripped, before)
        self.assertIn("attest_payload: " + premise.digest_payload(
            'The corpus   truth with "quotes"  inside'), after)
        self.assertIn("attest_record: ", after)
        self.assertIn("attest_by: owner-cell", after)
        self.assertNotIn("attest_turn:", after)
        # the native record recomputes
        self.assertTrue(premise.verify_chain()[0])

    def test_never_creates_a_twin_never_trips_the_add_guard(self):
        self.write_adopted()
        n_before = len(store.load_all(types=("prior",)))
        rc, _, err = self.run_verb(["--attest-existing", "corpus-law"])
        self.assertEqual(rc, 0)
        self.assertNotIn("supersede", err)
        gdir = os.path.join(home.global_dir(), "premises")
        self.assertFalse(os.path.exists(os.path.join(gdir, "prior-corpus-law.md")))
        self.assertEqual(len(store.load_all(types=("prior",))), n_before)
        e = store._find("corpus-law")
        self.assertEqual(e["root"], "adopted")
        self.assertTrue(e["attest_record"])

    def test_digest_matches_premise_check_after_backfill(self):
        self.write_adopted()
        self.run_verb(["--attest-existing", "corpus-law"])
        rc, out, _ = self.run_verb(["corpus-law"], fn=premise.cmd_premise_check)
        self.assertEqual(rc, 0)
        self.assertIn("digest: MATCH prem:b2b:", out)
        self.assertIn("native chain: VERIFIED", out)

    def test_guards_belief_attested_ghost(self):
        store.write_prior({"id": "a-belief", "statement": "maybe so",
                           "confidence": 0.6},
                          root_dir=os.path.join(home.global_dir(), "premises"))
        self.write_certain("done-law", "already signed", attest_payload="x",
                           attest_record="rr", attest_by="owner-cell")
        rc, _, err = self.run_verb(["--attest-existing", "a-belief"])
        self.assertEqual(rc, 1)
        self.assertIn("confidence 0.60", err)
        rc, out, _ = self.run_verb(["--attest-existing", "done-law"])
        self.assertEqual(rc, 0)   # idempotent skip
        self.assertIn("already attested", out)
        rc, _, err = self.run_verb(["--attest-existing", "ghost"])
        self.assertEqual(rc, 1)
        self.assertIn("not found", err)
        rc, _, _ = self.run_verb(["--attest-existing"])
        self.assertEqual(rc, 2)


class AnnotateSafetyTest(BackfillBase):
    def test_non_utf8_file_refuses_annotation_never_raises(self):
        p = os.path.join(os.environ["HELM_ADOPTED_DIR"], "prior-latin.md")
        with open(p, "wb") as f:
            f.write(b"---\nmetadata:\n  type: prior\n  id: latin\n"
                    b"  statement: caf\xe9 truth\n  confidence: 1.0\n---\n")
        self.assertFalse(premise._annotate(p, [("attest_record", TURN)]))
        with open(p, "rb") as f:
            self.assertIn(b"caf\xe9", f.read())

    def test_crlf_file_keeps_its_line_endings(self):
        raw = ADOPTED_RAW.replace("\n", "\r\n")
        p = self.write_adopted(raw=raw, name="prior-crlf.md")
        self.assertTrue(premise._annotate(p, [("attest_record", TURN)]))
        with open(p, "rb") as f:
            data = f.read()
        self.assertIn(b"attest_record: " + TURN.encode(), data)
        self.assertEqual(data.count(b"\r\n"), raw.count("\r\n"))


class QueueSafetyTest(BackfillBase):
    def test_retry_queue_drops_already_anchored_rows_unsent(self):
        self.write_certain("done-law", "already signed", attest_record="rr",
                           attest_anchor_turn="tt", attest_by="owner-cell")
        premise._enqueue({"ts": "t", "id": "done-law", "project": None,
                          "rec_hash": "rr", "kind": "anchor", "reason": "down"})
        with mock.patch.object(cell, "anchor_submit",
                               side_effect=AssertionError("must not send")):
            rc, out, _ = self.run_verb(["--retry-queue"])
        self.assertEqual(rc, 0)
        self.assertIn("1 already-anchored row dropped", out)
        self.assertEqual(self.queue_rows(), [])

    def test_queue_rewrite_preserves_a_concurrent_enqueue(self):
        p = self.write_certain("law-a", "truth a")
        premise._annotate(p, [("attest_record", "ra")])
        premise._enqueue({"ts": "t", "id": "law-a", "project": None,
                          "rec_hash": "ra", "kind": "anchor", "reason": "down"})

        def anchor_and_race(rec_hash, memo=None, timeout=8):
            premise._enqueue({"ts": "t2", "id": "law-b", "project": None,
                              "rec_hash": "rb", "kind": "anchor",
                              "reason": "concurrent capture"})
            return TURN, None

        with mock.patch.object(cell, "anchor_submit", anchor_and_race):
            rc, _, _ = self.run_verb(["--retry-queue"])
        self.assertEqual(rc, 0)
        self.assertEqual([r["id"] for r in self.queue_rows()], ["law-b"])

    def test_torn_queue_line_never_wedges_the_replay(self):
        p = self.write_certain("law-a", "truth a")
        premise._annotate(p, [("attest_record", "ra")])
        premise._enqueue({"ts": "t", "id": "law-a", "project": None,
                          "rec_hash": "ra", "kind": "anchor", "reason": "down"})
        with open(premise._queue_path(), "a", encoding="utf-8") as f:
            f.write('{"ts": "torn mid-app')
        with mock.patch.object(cell, "anchor_submit", return_value=(TURN, None)):
            rc, out, _ = self.run_verb(["--retry-queue"])
        self.assertEqual(rc, 0)
        self.assertIn("1 anchored", out)


class SweepTest(BackfillBase):
    def seed_corpus(self):
        """adopted + global + project certains (unattested), one belief, one
        already-attested certain — the sweep must attest exactly three."""
        self.write_adopted()
        self.write_certain("global-law", "the global truth")
        self.write_certain("proj-law", "the project truth", project="p1")
        store.write_prior({"id": "a-belief", "statement": "maybe", "confidence": 0.6},
                          root_dir=os.path.join(home.global_dir(), "premises"))
        self.write_certain("done-law", "already signed", attest_record="rr",
                           attest_by="owner-cell")

    def test_sweep_attests_all_roots_then_is_idempotent(self):
        self.seed_corpus()
        rc, out, _ = self.run_verb(["--attest-sweep"])
        self.assertEqual(rc, 0)
        self.assertIn("4 live certain entries — 1 attested, 3 to attest", out)
        self.assertIn("attested 'corpus-law' [adopted]", out)
        self.assertIn("attested 'global-law' [helm-global]", out)
        self.assertIn("attested 'proj-law' [project]", out)
        self.assertIn("3 attested (native)", out)
        for pid, proj in (("corpus-law", None), ("global-law", None),
                          ("proj-law", "p1")):
            e = store._find(pid, project=proj, types=("prior",))
            self.assertTrue(e["attest_record"], pid)
        self.assertTrue(premise.verify_chain()[0])
        # idempotence: a second sweep sends nothing
        rc, out, _ = self.run_verb(["--attest-sweep"])
        self.assertEqual(rc, 0)
        self.assertIn("4 attested, 0 to attest", out)
        self.assertIn("nothing to attest", out)

    def test_sweep_dry_run_writes_nothing(self):
        self.seed_corpus()
        rc, out, _ = self.run_verb(["--attest-sweep", "--dry"])
        self.assertEqual(rc, 0)
        self.assertIn("3 to attest", out)
        self.assertIn("3 native records to append (offline, no cost)", out)
        self.assertEqual(premise.chain_records(), [])   # nothing written

    def test_sweep_limit(self):
        self.seed_corpus()
        rc, out, _ = self.run_verb(["--attest-sweep", "--limit", "1"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(premise.chain_records()), 1)
        self.assertIn("1 attested (native)", out)
        rc, _, _ = self.run_verb(["--attest-sweep", "--limit", "nope"])
        self.assertEqual(rc, 2)

    def test_sweep_prunes_stale_queue_rows(self):
        p = self.write_certain("global-law", "the global truth")
        premise._annotate(p, [("attest_record", "rg"),
                              ("attest_anchor_turn", "tt")])
        premise._enqueue({"ts": "t", "id": "global-law", "project": None,
                          "rec_hash": "rg", "kind": "anchor", "reason": "was down"})
        rc, out, _ = self.run_verb(["--attest-sweep"])
        self.assertEqual(rc, 0)
        self.assertIn("pruned 1 stale queue row", out)
        self.assertEqual(self.queue_rows(), [])


if __name__ == "__main__":
    unittest.main()
