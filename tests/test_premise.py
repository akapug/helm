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
                               return_value=(True, "turn present on node "
                                             "(payload binding unavailable)")):
            rc, out, _ = self.run_verb(premise.cmd_premise_check, ["law-x"])
        self.assertEqual(rc, 0)
        # A1: turn OBSERVED, never CONFIRMED — existence, not payload binding
        self.assertIn("external anchor: turn OBSERVED", out)
        self.assertNotIn("CONFIRMED", out)

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

    def test_no_attestation_recorded_is_nonzero_and_distinct(self):
        # B2: absence of the primary proof MUST NOT read as success. It exits on
        # its own contract (EXIT_NO_NATIVE_PROOF), distinct from broken (1).
        self.run_verb(premise.cmd_premise, ["law-x | The X truth", "--no-attest"])
        rc, out, _ = self.run_verb(premise.cmd_premise_check, ["law-x"])
        self.assertEqual(rc, premise.EXIT_NO_NATIVE_PROOF)
        self.assertNotEqual(rc, 0)
        self.assertIn("NOT ATTESTED", out)

    def test_unknown_id(self):
        rc, _, err = self.run_verb(premise.cmd_premise_check, ["ghost"])
        self.assertEqual(rc, 1)
        self.assertIn("not found", err)
        rc, _, _ = self.run_verb(premise.cmd_premise_check, [])
        self.assertEqual(rc, 2)


class BindingTest(PremiseBase):
    """B1 — the native record must BIND to the exact premise. A record that is
    internally valid but commits a DIFFERENT claim (a foreign premise's record,
    or the OLD record after the statement changed) is BROKEN, never VERIFIED,
    and premise-check exits non-zero — even when the mutable frontmatter
    (attest_payload) was ALSO tampered to match the new statement."""

    def two(self):
        self.run_verb(premise.cmd_premise, ["law-x | The X truth"])
        self.run_verb(premise.cmd_premise, ["law-y | The Y truth"])

    def meta(self, pid):
        return pk.parse_simple_frontmatter(
            self.entry_path(pid),
            {"attest_payload": "", "attest_record": ""}) or {}

    def test_foreign_record_verifies_broken_and_check_nonzero(self):
        self.two()
        y_rec = self.meta("law-y")["attest_record"]
        mx = self.meta("law-x")
        self.assertTrue(y_rec and mx["attest_record"] and y_rec != mx["attest_record"])
        evil = "evil injected truth"
        raw = self.entry_raw("law-x")
        raw = raw.replace("The X truth", evil)                       # statement
        raw = raw.replace(mx["attest_payload"], premise.digest_payload(evil))  # payload
        raw = raw.replace("attest_record: " + mx["attest_record"],
                          "attest_record: " + y_rec)                 # foreign record
        pk.atomic_write(self.entry_path("law-x"), raw)
        rc, out, _ = self.run_verb(premise.cmd_premise_check, ["law-x"])
        self.assertEqual(rc, 1)                     # present-but-broken, not absent
        self.assertIn("digest: MATCH", out)         # the frontmatter forgery "worked"
        self.assertIn("native chain: BROKEN", out)  # the record binding catches it

    def test_stale_old_record_after_statement_change_broken(self):
        self.run_verb(premise.cmd_premise, ["law-x | The X truth"])
        m = self.meta("law-x")
        evil = "evil injected truth"
        raw = self.entry_raw("law-x")
        raw = raw.replace("The X truth", evil)
        raw = raw.replace(m["attest_payload"], premise.digest_payload(evil))
        # attest_record UNCHANGED — the OLD record still commits the old digest
        pk.atomic_write(self.entry_path("law-x"), raw)
        rc, out, _ = self.run_verb(premise.cmd_premise_check, ["law-x"])
        self.assertEqual(rc, 1)
        self.assertIn("digest: MATCH", out)
        self.assertIn("native chain: BROKEN", out)

    def test_verify_record_binding_rejects_foreign_accepts_own(self):
        self.two()
        ex = store._find("law-x")
        ok, detail = premise.verify_record(self.meta("law-y")["attest_record"],
                                           premise._record_expect(ex, None))
        self.assertFalse(ok)
        self.assertIn("foreign or stale", detail)
        ok2, _ = premise.verify_record(self.meta("law-x")["attest_record"],
                                       premise._record_expect(ex, None))
        self.assertTrue(ok2)

    def test_payload_without_native_record_is_not_success(self):
        # B2: a matching payload but NO native record hash is not verification —
        # absence of the primary proof exits non-zero (distinct: NOT ATTESTED).
        self.run_verb(premise.cmd_premise, ["law-x | The X truth", "--no-attest"])
        e = store._find("law-x")
        premise._annotate(e["path"],
                          [("attest_payload", premise.digest_payload("The X truth"))])
        rc, out, _ = self.run_verb(premise.cmd_premise_check, ["law-x"])
        self.assertEqual(rc, premise.EXIT_NO_NATIVE_PROOF)
        self.assertIn("digest: MATCH", out)   # the payload matches the statement...
        self.assertIn("NOT ATTESTED", out)    # ...but there is no primary proof


class AnnotationDurabilityTest(PremiseBase):
    """B5 — a failed in-place annotation must NEVER orphan the native record: a
    durable reconciliation row is queued and --retry-queue re-annotates it."""

    def test_capture_annotation_failure_queues_reconciliation(self):
        # force _annotate to refuse (as a shape surprise would)
        with mock.patch.object(premise._capture, "_annotate", return_value=False):
            rc, out, _ = self.run_verb(premise.cmd_premise, ["law-x | The X truth"])
        self.assertEqual(rc, 0)
        self.assertIn("WARNING: file shape refused", out)
        self.assertEqual(len(premise.chain_records()), 1)     # record STANDS
        rows = [r for r in _queue(premise) if r.get("kind") == "annotate"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "law-x")
        # the native record is orphaned RIGHT NOW (no pointer) — check reflects it
        rc, out, _ = self.run_verb(premise.cmd_premise_check, ["law-x"])
        self.assertEqual(rc, premise.EXIT_NO_NATIVE_PROOF)
        # --retry-queue reconciles the annotation (record already stands)
        rc, out, _ = self.run_verb(premise.cmd_premise, ["--retry-queue"])
        self.assertIn("reconciled 'law-x'", out)
        self.assertIn("attest_record: ", self.entry_raw("law-x"))
        rc, out, _ = self.run_verb(premise.cmd_premise_check, ["law-x"])
        self.assertEqual(rc, 0)
        self.assertIn("native chain: VERIFIED", out)

    def test_replay_never_drops_a_landed_anchor_on_annotation_failure(self):
        self.run_verb(premise.cmd_premise, ["law-x | The X truth"])   # queues anchor
        # anchor lands on replay, but annotation refuses -> the row is KEPT with
        # the turn (never dropped, never re-anchored), then reconciled next pass.
        calls = {"n": 0}

        def anchor_once(rec_hash, memo=None, timeout=8):
            calls["n"] += 1
            return TURN, None

        with mock.patch.object(cell, "anchor_submit", anchor_once), \
                mock.patch.object(premise._capture, "_annotate", return_value=False):
            rc, out, _ = self.run_verb(premise.cmd_premise, ["--retry-queue"])
        self.assertEqual(rc, 1)   # still pending (annotation deferred)
        row = [r for r in _queue(premise) if r.get("id") == "law-x"][0]
        self.assertEqual(row["anchor_turn"], TURN)   # the landed turn is retained
        # second pass: annotation now works, no RE-anchor (turn reused)
        with mock.patch.object(cell, "anchor_submit",
                               side_effect=AssertionError("must not re-anchor")):
            rc, out, _ = self.run_verb(premise.cmd_premise, ["--retry-queue"])
        self.assertEqual(rc, 0)
        self.assertEqual(calls["n"], 1)   # anchored exactly once, ever
        self.assertIn("attest_anchor_turn: " + TURN, self.entry_raw("law-x"))


def _queue(premise_mod):
    import json as _json
    try:
        with open(premise_mod._queue_path()) as f:
            return [_json.loads(l) for l in f if l.strip()]
    except OSError:
        return []


if __name__ == "__main__":
    unittest.main()
