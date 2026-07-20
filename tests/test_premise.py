#!/usr/bin/env python3
"""premise tests — NATIVE attestation. Hermetic: HELM_HOME is a tempdir and the
OPTIONAL dregg node points at a dead port (anchor fails open, fast). The native
hash chain is the primary proof and needs no node, no binary, no network."""
import contextlib
import hashlib
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("HELM_HOME", tempfile.mkdtemp(prefix="helm-test-home-"))

from helm import cell, home, pk, premise, store  # noqa: E402

TURN = "cd" * 32
DEAD = "http://127.0.0.1:1"


class PremiseBase(unittest.TestCase):
    ENV_KEYS = ("HELM_HOME", "HELM_ADOPTED_DIR", "HELM_NODE_URL",
                "HELM_CELL_PROFILE", "MELD_NODE_URL", "MELD_AGENT_PROFILE")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-premise-")
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

    def run_verb(self, fn, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fn(list(args))
        return rc, out.getvalue(), err.getvalue()

    def entry_path(self, pid="law-x"):
        return os.path.join(home.global_dir(), "premises", "prior-%s.md" % pid)

    def entry_raw(self, pid="law-x"):
        with open(self.entry_path(pid)) as f:
            return f.read()


class CanonTest(PremiseBase):
    def test_canonicalization_is_stable(self):
        self.assertEqual(premise.canonicalize('  Say  "yo"\n\t now '), "Say 'yo' now")
        c = premise.canonicalize('He said "x  y".')
        self.assertEqual(premise.canonicalize(c), c)
        self.assertEqual(premise.canonicalize("café"),
                         premise.canonicalize("café"))
        self.assertEqual(premise.canonicalize(None), "")

    def test_tagged_digest_deterministic(self):
        want = "prem:b2b:" + hashlib.blake2b(b"the truth", digest_size=32).hexdigest()
        self.assertEqual(premise.digest_payload("the  truth"), want)
        self.assertEqual(premise.digest_payload("the truth\n"), want)
        p = premise.digest_payload("any statement at all")
        self.assertEqual(len(p.encode("utf-8")), 73)
        self.assertTrue(p.startswith("prem:b2b:"))
        self.assertEqual(premise.payload_digest(p), p[len(premise.DIGEST_TAG):])
        self.assertEqual(premise.payload_digest("garbage"), "")


class CaptureTest(PremiseBase):
    def test_store_write_plus_native_record(self):
        rc, out, _ = self.run_verb(premise.cmd_premise,
                                   ["law-x | The X truth | xkw | dev"])
        self.assertEqual(rc, 0)
        self.assertIn("LIVE 'law-x' [certain 1.00]", out)
        self.assertIn("attested (native): record ", out)
        raw = self.entry_raw()
        self.assertIn("  confidence: 1.00", raw)
        self.assertIn("  class: certain", raw)
        self.assertIn("  source: human", raw)
        self.assertIn("  attest_payload: " + premise.digest_payload("The X truth"), raw)
        self.assertIn("  attest_by: owner-cell", raw)
        self.assertIn("  attest_record: ", raw)
        self.assertIn("  attest_chain_index: 0", raw)
        self.assertNotIn("attest_turn:", raw)   # no node-signed turn, ever
        # exactly one native record; it recomputes
        recs = premise.chain_records()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["op"], "create")
        self.assertEqual(recs[0]["premise_id"], "law-x")
        self.assertEqual(recs[0]["digest"], premise.digest_payload("The X truth"))
        self.assertTrue(premise.verify_chain()[0])
        # the store still reads it as a certain prior
        e = store._find("law-x")
        self.assertEqual((e["class"], e["confidence"]), ("certain", 1.0))

    def test_no_attest_flag(self):
        rc, out, _ = self.run_verb(premise.cmd_premise,
                                   ["law-x | The X truth", "--no-attest"])
        self.assertEqual(rc, 0)
        self.assertIn("attestation skipped", out)
        self.assertNotIn("attest_payload", self.entry_raw())
        self.assertEqual(premise.chain_records(), [])

    def test_graceful_degrade_with_no_node_never_raises(self):
        # the money path: no dregg node -> store + native record still land,
        # only the optional anchor is queued; capture succeeds and never raises
        rc, out, _ = self.run_verb(premise.cmd_premise, ["law-x | The X truth"])
        self.assertEqual(rc, 0)
        self.assertIn("external anchor: none yet", out)
        self.assertTrue(os.path.exists(self.entry_path()))
        self.assertEqual(len(premise.chain_records()), 1)
        with open(premise._queue_path()) as f:
            rows = [l for l in f if l.strip()]
        self.assertEqual(len(rows), 1)
        self.assertIn('"kind": "anchor"', rows[0])
        self.assertIn("law-x", rows[0])

    def test_anchor_landed_annotates_honestly(self):
        with mock.patch.object(cell, "anchor_submit", return_value=(TURN, None)):
            rc, out, _ = self.run_verb(premise.cmd_premise, ["law-x | The X truth"])
        self.assertEqual(rc, 0)
        self.assertIn("external anchor: dregg node", out)
        raw = self.entry_raw()
        self.assertIn("  attest_anchor_turn: " + TURN, raw)
        self.assertIn("anchored digest at turn " + TURN, raw)
        # the anchor did NOT queue (it landed)
        self.assertFalse(os.path.exists(premise._queue_path()))

    def test_default_profile_is_test_label(self):
        os.environ.pop("HELM_CELL_PROFILE", None)
        self.assertEqual(premise.attest_profile(), "helm-test")
        os.environ["HELM_CELL_PROFILE"] = "user-cell"
        self.assertEqual(premise.attest_profile(), "user-cell")

    def test_project_scoped_capture(self):
        rc, _, _ = self.run_verb(premise.cmd_premise,
                                 ["p-law | scoped truth", "--project", "p1"])
        self.assertEqual(rc, 0)
        p = os.path.join(home.project_dir("p1"), "premises", "prior-p-law.md")
        self.assertTrue(os.path.exists(p))
        self.assertEqual(premise.chain_records()[0]["project"], "p1")

    def test_idempotent_restate_skips_second_record(self):
        self.run_verb(premise.cmd_premise, ["law-x | The X truth"])
        rc, out, _ = self.run_verb(premise.cmd_premise,
                                   ["law-x | The X truth | fresh-kw"])
        self.assertEqual(rc, 0)
        self.assertIn("already attested — record", out)
        self.assertEqual(len(premise.chain_records()), 1)   # never double-records
        self.assertIn("  keywords: fresh-kw", self.entry_raw())

    def test_usage(self):
        rc, _, _ = self.run_verb(premise.cmd_premise, [])
        self.assertEqual(rc, 2)
        rc, _, _ = self.run_verb(premise.cmd_premise, ["only-an-id"])
        self.assertEqual(rc, 2)


class NativeChainTest(PremiseBase):
    def three(self):
        for i in range(3):
            self.run_verb(premise.cmd_premise, ["law-%d | truth %d" % (i, i)])

    def test_full_chain_integrity(self):
        self.three()
        ok, detail = premise.verify_chain()
        self.assertTrue(ok, detail)
        self.assertIn("3 records", detail)
        # every record links to its predecessor
        recs = premise.chain_records()
        self.assertEqual(recs[0]["prev"], "")
        self.assertEqual(recs[1]["prev"], recs[0]["rec_hash"])
        self.assertEqual(recs[2]["prev"], recs[1]["rec_hash"])

    def test_tampered_record_fails_verification(self):
        self.three()
        recs = premise.chain_records()
        target = recs[1]["rec_hash"]
        # mutate a hashed CORE field (digest) of record 1, leaving rec_hash — so
        # it no longer recomputes to its stored hash
        import json
        path = premise._chain_path()
        with open(path) as f:
            lines = f.read().splitlines()
        rec = json.loads(lines[1])
        rec["digest"] = premise.digest_payload("a different truth entirely")
        lines[1] = json.dumps(rec)
        pk.atomic_write(path, "\n".join(lines) + "\n")
        ok, _ = premise.verify_chain()
        self.assertFalse(ok)
        rok, _ = premise.verify_record(target)
        self.assertFalse(rok)


class CheckTest(PremiseBase):
    def capture(self, statement="The X truth"):
        rc, _, _ = self.run_verb(premise.cmd_premise, ["law-x | " + statement])
        self.assertEqual(rc, 0)

    def test_native_verified_anchor_none(self):
        self.capture()
        rc, out, _ = self.run_verb(premise.cmd_premise_check, ["law-x"])
        self.assertEqual(rc, 0)
        self.assertIn("digest: MATCH prem:b2b:", out)
        self.assertIn("native chain: VERIFIED", out)
        self.assertIn("external anchor: none (native-only)", out)
        self.assertIn("recorded by 'owner-cell'", out)
        self.assertIn("not a cell signer", out)

    def test_anchor_confirmed_when_node_shows_it(self):
        with mock.patch.object(cell, "anchor_submit", return_value=(TURN, None)):
            self.capture()
        with mock.patch.object(cell, "verify_anchor",
                               return_value=(True, "proof present on node")):
            rc, out, _ = self.run_verb(premise.cmd_premise_check, ["law-x"])
        self.assertEqual(rc, 0)
        self.assertIn("external anchor: CONFIRMED", out)

    def test_anchor_unverified_when_node_absent(self):
        with mock.patch.object(cell, "anchor_submit", return_value=(TURN, None)):
            self.capture()
        with mock.patch.object(cell, "verify_anchor",
                               return_value=(False, "node unreachable")):
            rc, out, _ = self.run_verb(premise.cmd_premise_check, ["law-x"])
        self.assertEqual(rc, 0)   # native proof stands; anchor honestly unverified
        self.assertIn("external anchor: unverified", out)

    def test_mismatch_on_tampered_statement(self):
        self.capture()
        path = self.entry_path()
        with open(path) as f:
            raw = f.read()
        pk.atomic_write(path, raw.replace("The X truth", "A tampered truth"))
        rc, out, _ = self.run_verb(premise.cmd_premise_check, ["law-x"])
        self.assertEqual(rc, 1)
        self.assertIn("digest: MISMATCH", out)
        self.assertIn("attested:   prem:b2b:", out)
        self.assertIn("recomputed: prem:b2b:", out)

    def test_tampered_native_record_breaks_check(self):
        self.capture()
        # corrupt a hashed core field (ts) of the record; frontmatter still
        # points at the (unchanged) rec_hash, so the check finds a record that
        # no longer recomputes -> native chain BROKEN
        import json
        path = premise._chain_path()
        with open(path) as f:
            rec = json.loads(f.read().strip())
        rec["ts"] = "1999-01-01T00:00:00Z"
        pk.atomic_write(path, json.dumps(rec) + "\n")
        rc, out, _ = self.run_verb(premise.cmd_premise_check, ["law-x"])
        self.assertEqual(rc, 1)
        self.assertIn("native chain: BROKEN", out)

    def test_no_attestation_recorded_is_plain(self):
        self.run_verb(premise.cmd_premise, ["law-x | The X truth", "--no-attest"])
        rc, out, _ = self.run_verb(premise.cmd_premise_check, ["law-x"])
        self.assertEqual(rc, 0)
        self.assertIn("no attestation recorded", out)

    def test_unknown_id(self):
        rc, _, err = self.run_verb(premise.cmd_premise_check, ["ghost"])
        self.assertEqual(rc, 1)
        self.assertIn("not found", err)
        rc, _, _ = self.run_verb(premise.cmd_premise_check, [])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
