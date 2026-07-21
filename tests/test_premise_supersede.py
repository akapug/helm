#!/usr/bin/env python3
"""supersession-chain tests — NATIVE linkage. `premise --supersede` captures
NEW, tombstones OLD (store lifecycle, offline), and appends ONE native
supersede record linking supersedes_record -> OLD's rec_hash. `premise-check
--chain` prints the attested biography. Hermetic: HELM_HOME is a tempdir and
the OPTIONAL dregg node points at a dead port."""
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest

os.environ.setdefault("HELM_HOME", tempfile.mkdtemp(prefix="helm-test-home-"))

from helm import home, pk, premise, store  # noqa: E402

DEAD = "http://127.0.0.1:1"
REC_A = "a1" * 32
REC_B = "b2" * 32


class SupBase(unittest.TestCase):
    ENV_KEYS = ("HELM_HOME", "HELM_ADOPTED_DIR", "HELM_NODE_URL",
                "HELM_CELL_PROFILE", "MELD_NODE_URL", "MELD_AGENT_PROFILE")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-sup-")
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

    def entry_raw(self, pid):
        p = os.path.join(home.global_dir(), "premises", "prior-%s.md" % pid)
        with open(p) as f:
            return f.read()

    def queue_rows(self):
        try:
            with open(premise._queue_path()) as f:
                return [json.loads(l) for l in f if l.strip()]
        except OSError:
            return []

    def capture_v1(self, extra=()):
        rc, _, _ = self.run_verb(premise.cmd_premise,
                                 ["law-v1 | truth one"] + list(extra))
        self.assertEqual(rc, 0)

    def rec_hash_of(self, pid):
        meta = pk.parse_simple_frontmatter(
            os.path.join(home.global_dir(), "premises", "prior-%s.md" % pid),
            {"attest_record": ""})
        return (meta or {}).get("attest_record") or ""


class SupersedeFlowTest(SupBase):
    def test_full_flow_links_chain(self):
        self.capture_v1()
        v1_rec = self.rec_hash_of("law-v1")
        self.assertTrue(v1_rec)
        rc, out, _ = self.run_verb(
            premise.cmd_premise,
            ["--supersede", "law-v1", "law-v2 | truth two | kw2 | dev"])
        self.assertEqual(rc, 0)
        self.assertIn("LIVE 'law-v2' [certain 1.00] - truth two", out)
        self.assertIn("supersedes 'law-v1'", out)
        self.assertIn("attested (native): record ", out)
        self.assertIn("prior record " + v1_rec[:16], out)
        # OLD: tombstoned in place, attest keys carried through the rewrite
        old = self.entry_raw("law-v1")
        self.assertIn("  status: delete_eligible", old)
        self.assertIn("  replaced_by: law-v2", old)
        self.assertIn("  attest_record: " + v1_rec, old)
        # NEW: backpointer + a PLAIN prem: digest payload + native linkage
        new = self.entry_raw("law-v2")
        self.assertIn("  supersedes: law-v1", new)
        self.assertIn("  attest_payload: " + premise.digest_payload("truth two"), new)
        self.assertIn("  attest_supersedes_record: " + v1_rec, new)
        self.assertNotIn("attest_turn:", new)   # never node-signed
        # the native chain is intact and the supersede record is op=supersede
        self.assertTrue(premise.verify_chain()[0])
        recs = premise.chain_records()
        self.assertEqual(recs[-1]["op"], "supersede")
        self.assertEqual(recs[-1]["supersedes_record"], v1_rec)

    def test_never_attested_old_starts_chain_honestly(self):
        self.capture_v1(["--no-attest"])
        rc, out, _ = self.run_verb(premise.cmd_premise,
                                   ["--supersede", "law-v1", "law-v2 | truth two"])
        self.assertEqual(rc, 0)
        self.assertIn("never attested — the chain starts here", out)
        new = self.entry_raw("law-v2")
        self.assertIn("  attest_payload: " + premise.digest_payload("truth two"), new)
        self.assertNotIn("attest_supersedes_record", new)
        self.assertIn("  replaced_by: law-v2", self.entry_raw("law-v1"))
        # the chain-start record commits op=create (no record to link), so the
        # checker VERIFIES the new premise on BOTH paths (codex re-review catch)
        self.assertEqual(premise.chain_records()[-1]["op"], "create")
        rc, out, _ = self.run_verb(premise.cmd_premise_check, ["law-v2"])
        self.assertEqual(rc, 0)
        self.assertIn("native chain: VERIFIED", out)
        rc, out, _ = self.run_verb(premise.cmd_premise_check,
                                   ["--chain", "law-v2"])
        self.assertNotEqual(rc, 1)   # v1 is unattested (absent=3) — never broken
        self.assertIn("native chain VERIFIED", out)

    def test_refusals(self):
        self.capture_v1()
        cases = (
            (["--supersede", "ghost", "law-v2 | t"], 1, "not found"),
            (["--supersede", "law-v1", "law-v1 | t"], 1, "cannot supersede itself"),
            (["--supersede", "law-v1", "law-v2 | t", "--no-attest"], 2,
             "helm store supersede"),
            (["--supersede", "law-v1"], 2, "usage"),
            (["--supersede"], 2, "usage"),
        )
        for args, want_rc, want_err in cases:
            rc, _, err = self.run_verb(premise.cmd_premise, args)
            self.assertEqual(rc, want_rc, args)
            self.assertIn(want_err, err)
        # a tip never forks
        rc, _, _ = self.run_verb(premise.cmd_premise,
                                 ["--supersede", "law-v1", "law-v2 | truth two"])
        self.assertEqual(rc, 0)
        rc, _, err = self.run_verb(premise.cmd_premise,
                                   ["--supersede", "law-v1", "law-v3 | truth three"])
        self.assertEqual(rc, 1)
        self.assertIn("never forks", err)

    def test_node_down_lifecycle_and_native_record_both_land(self):
        # the corrected design: with no node, BOTH the store lifecycle AND the
        # native supersede record land offline — only the OPTIONAL anchor queues
        self.capture_v1()
        v1_rec = self.rec_hash_of("law-v1")
        rc, out, _ = self.run_verb(premise.cmd_premise,
                                   ["--supersede", "law-v1", "law-v2 | truth two"])
        self.assertEqual(rc, 0)
        self.assertIn("external anchor: none yet", out)
        self.assertIn("  status: delete_eligible", self.entry_raw("law-v1"))
        new = self.entry_raw("law-v2")
        self.assertIn("  supersedes: law-v1", new)
        self.assertIn("  attest_supersedes_record: " + v1_rec, new)  # native landed
        # both captures queued an anchor row (best-effort external checkpoint)
        ids = [r["id"] for r in self.queue_rows()]
        self.assertEqual(sorted(ids), ["law-v1", "law-v2"])
        self.assertTrue(all(r["kind"] == "anchor" for r in self.queue_rows()))


class CaptureGuardTest(SupBase):
    def test_orphaning_edit_refused_toward_supersede(self):
        self.capture_v1()
        rc, _, err = self.run_verb(premise.cmd_premise, ["law-v1 | a NEW truth"])
        self.assertEqual(rc, 1)
        self.assertIn("orphans the attestation", err)
        self.assertIn("--supersede law-v1", err)
        self.assertIn("truth one", self.entry_raw("law-v1"))

    def test_remint_over_tombstone_starts_fresh_lifecycle(self):
        self.capture_v1()
        self.run_verb(premise.cmd_premise,
                      ["--supersede", "law-v1", "law-v2 | truth two"])
        rc, _, _ = self.run_verb(premise.cmd_premise, ["law-v1 | resurrected"])
        self.assertEqual(rc, 0)
        raw = self.entry_raw("law-v1")
        self.assertIn("  status: live", raw)
        for stale in ("replaced_by", "retired_ts", "attest_supersedes_record"):
            self.assertNotIn(stale, raw)
        self.assertEqual(raw.count("attest_payload:"), 1)
        self.assertIn("  attest_payload: " + premise.digest_payload("resurrected"), raw)


class VerifyLinkTest(SupBase):
    def link(self, *, new_payload, sup_record=REC_A, old_record=REC_A):
        old = {"id": "old", "statement": "so", "attest_record": old_record}
        new = {"id": "new", "statement": "sn", "attest_payload": new_payload,
               "attest_supersedes_record": sup_record}
        return old, new

    def test_states(self):
        good = premise.digest_payload("sn")
        cases = (
            (self.link(new_payload=good), "attested"),
            (self.link(new_payload=good, sup_record=""), "unbacked"),
            (self.link(new_payload=premise.digest_payload("WRONG")), "broken"),
            (self.link(new_payload=good, old_record=""), "broken"),
            (self.link(new_payload=good, sup_record=REC_B), "broken"),
        )
        for (old, new), want in cases:
            self.assertEqual(premise.verify_link(old, new)[0], want,
                             (new.get("attest_supersedes_record"), want))


class ChainCheckTest(SupBase):
    def build_chain(self):
        """law-a -> law-b -> law-c via native supersede records."""
        rc, _, _ = self.run_verb(premise.cmd_premise, ["law-a | truth one"])
        self.assertEqual(rc, 0)
        rc, _, _ = self.run_verb(premise.cmd_premise,
                                 ["--supersede", "law-a", "law-b | truth two"])
        self.assertEqual(rc, 0)
        rc, _, _ = self.run_verb(premise.cmd_premise,
                                 ["--supersede", "law-b", "law-c | truth three"])
        self.assertEqual(rc, 0)

    def test_walks_whole_chain_from_any_link(self):
        self.build_chain()
        for pid in ("law-a", "law-b", "law-c"):
            rc, out, _ = self.run_verb(premise.cmd_premise_check, ["--chain", pid])
            self.assertEqual(rc, 0, out)
            self.assertIn("3 links through '%s', origin first" % pid, out)
            self.assertIn("1. law-a [delete_eligible] - truth one", out)
            self.assertIn("3. law-c [live] - truth three", out)
            self.assertEqual(out.count("digest MATCH"), 3)
            self.assertEqual(out.count("native chain VERIFIED"), 3)
            self.assertIn("link 1->2 ATTESTED — native record linkage", out)
            self.assertIn("link 2->3 ATTESTED", out)
            self.assertIn("held 'truth one' until", out)
            self.assertIn("then 'truth three' — LIVE now", out)
            self.assertIn("primary proof", out)

    def test_tampered_statement_fails_the_chain(self):
        self.build_chain()
        p = os.path.join(home.global_dir(), "premises", "prior-law-b.md")
        with open(p) as f:
            raw = f.read()
        pk.atomic_write(p, raw.replace("truth two", "tampered two"))
        rc, out, _ = self.run_verb(premise.cmd_premise_check, ["--chain", "law-a"])
        self.assertEqual(rc, 1)
        self.assertIn("digest MISMATCH", out)
        # b's stored statement no longer hashes to its attest_payload -> the
        # b hop's digest breaks; the a->b link also breaks (digest check)
        self.assertIn("link 1->2 BROKEN", out)

    def test_store_only_hop_reads_unbacked_but_not_broken(self):
        self.build_chain()
        rc, _, _ = self.run_verb(store.cmd_store,
                                 ["add", "premise", "law-d | truth four"])
        self.assertEqual(rc, 0)
        _e, err = store.mark_superseded("law-c", "law-d", pk.now_ts())
        self.assertIsNone(err)
        rc, out, _ = self.run_verb(premise.cmd_premise_check, ["--chain", "law-a"])
        # Store-only hop is UNBACKED, not corrupt: the corrected B2 contract maps
        # missing native proof to EXIT_NO_NATIVE_PROOF (3) — distinct from broken
        # (1) and from a fully-attested chain (0). Not-attested must never read 0.
        self.assertEqual(rc, premise.EXIT_NO_NATIVE_PROOF)
        self.assertIn("4 links", out)
        self.assertIn("link 3->4 UNBACKED", out)

    def test_single_entry_chain(self):
        self.run_verb(premise.cmd_premise, ["solo | alone"])
        rc, out, _ = self.run_verb(premise.cmd_premise_check, ["--chain", "solo"])
        self.assertEqual(rc, 0)
        self.assertIn("1 link through 'solo'", out)
        self.assertIn("no supersession links", out)

    def test_unknown_id(self):
        rc, _, err = self.run_verb(premise.cmd_premise_check, ["--chain", "ghost"])
        self.assertEqual(rc, 1)
        self.assertIn("not found", err)


class CheckSupRecordTest(SupBase):
    def test_single_check_reads_sup_record(self):
        self.run_verb(premise.cmd_premise, ["law-a | truth one"])
        a_rec = self.rec_hash_of("law-a")
        self.run_verb(premise.cmd_premise,
                      ["--supersede", "law-a", "law-b | truth two"])
        rc, out, _ = self.run_verb(premise.cmd_premise_check, ["law-b"])
        self.assertEqual(rc, 0)
        self.assertIn("digest: MATCH prem:b2b:", out)
        self.assertIn("supersedes prior record " + a_rec[:16], out)
        self.assertIn("helm premise-check --chain law-b", out)


if __name__ == "__main__":
    unittest.main()
