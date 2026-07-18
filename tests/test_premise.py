#!/usr/bin/env python3
"""premise tests — hermetic: HELM_HOME is a tempdir, the substrate binary is a
stub (HELM_CELL_BIN), the node reader is injected. The real node, ~/.dregg and
~/.helm are never touched."""
import contextlib
import hashlib
import io
import json
import os
import shutil
import stat
import tempfile
import unittest

os.environ.setdefault("HELM_HOME", tempfile.mkdtemp(prefix="helm-test-home-"))

from helm import cell, home, pk, premise, store  # noqa: E402

CELL_HEX = "ab" * 32
TURN = "cd" * 32

ENV_KEYS = ("HELM_HOME", "HELM_ADOPTED_DIR", "HELM_CELL_BIN", "MELD_CELL_BIN",
            "HELM_NODE_URL", "HELM_CELL_PROFILE", "MELD_NODE_URL",
            "MELD_AGENT_PROFILE", "STUB_LOG", "STUB_CELL", "STUB_TURN")

STUB = """#!/bin/sh
echo "argv:$@" >> "$STUB_LOG"
case "$1" in
  join) echo '{"joined":true,"cell":"'"$STUB_CELL"'","turn_hash":"tj","receipt_hash":"rj","chain_index":1}';;
  send) echo '{"sent":true,"to":"'"$STUB_CELL"'","seq":1,"bytes":73,"slots":11,"turn_hash":"'"$STUB_TURN"'","receipt_hash":"rs","chain_index":2}';;
esac
exit 0
"""

FAILING_STUB = "#!/bin/sh\necho 'node down' >&2\nexit 1\n"


class PremiseBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-premise-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])
        os.environ["STUB_LOG"] = os.path.join(self.tmp, "stub.log")
        os.environ["STUB_CELL"] = CELL_HEX
        os.environ["STUB_TURN"] = TURN
        os.environ["HELM_CELL_PROFILE"] = "stub-prof"

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_stub(self, body=STUB):
        path = os.path.join(self.tmp, "meld-stub")
        with open(path, "w") as f:
            f.write(body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        os.environ["HELM_CELL_BIN"] = path
        return path

    def run_verb(self, fn, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fn(args)
        return rc, out.getvalue(), err.getvalue()

    def entry_path(self, pid="law-x"):
        return os.path.join(home.global_dir(), "premises", "prior-%s.md" % pid)


class CanonTest(PremiseBase):
    def test_canonicalization_is_stable(self):
        self.assertEqual(premise.canonicalize('  Say  "yo"\n\t now '), "Say 'yo' now")
        # the quote-swap mirrors store.write_prior, so store round-trip is a fixpoint
        c = premise.canonicalize('He said "x  y".')
        self.assertEqual(premise.canonicalize(c), c)
        # NFC: composed and decomposed forms hash identically
        self.assertEqual(premise.canonicalize("café"),
                         premise.canonicalize("café"))
        self.assertEqual(premise.canonicalize(None), "")

    def test_tagged_digest_deterministic_and_in_budget(self):
        want = "prem:b2b:" + hashlib.blake2b(b"the truth", digest_size=32).hexdigest()
        self.assertEqual(premise.digest_payload("the  truth"), want)
        self.assertEqual(premise.digest_payload("the truth\n"), want)
        p = premise.digest_payload("any statement at all")
        self.assertEqual(len(p.encode("utf-8")), 73)  # <= the 104B whisper budget
        self.assertTrue(p.startswith("prem:b2b:"))


class CaptureTest(PremiseBase):
    def test_store_write_plus_attest_annotation(self):
        self.write_stub()
        rc, out, _ = self.run_verb(premise.cmd_premise,
                                   ["law-x | The X truth | xkw | dev"])
        self.assertEqual(rc, 0)
        self.assertIn("LIVE 'law-x' [certain 1.00]", out)
        self.assertIn("attested: turn " + TURN, out)
        with open(self.entry_path()) as f:
            raw = f.read()
        self.assertIn("  confidence: 1.00", raw)
        self.assertIn("  class: certain", raw)
        self.assertIn("  source: human", raw)
        self.assertIn("  attest_payload: " + premise.digest_payload("The X truth"), raw)
        self.assertIn("  attest_by: stub-prof", raw)
        self.assertIn("  attest_turn: " + TURN, raw)
        self.assertIn("  attest_receipt: rs", raw)
        # the store still reads the entry as a certain-prior (attest keys inert)
        e = store._find("law-x")
        self.assertEqual((e["class"], e["confidence"], e["statement"]),
                         ("certain", 1.0, "The X truth"))
        # payload rode the SEND path (whisper payload slots), never heartbeat
        with open(os.environ["STUB_LOG"]) as f:
            log = f.read()
        self.assertIn("argv:send --profile stub-prof --to " + CELL_HEX, log)
        self.assertNotIn("heartbeat", log)

    def test_no_attest_flag(self):
        self.write_stub()
        rc, out, _ = self.run_verb(premise.cmd_premise,
                                   ["law-x | The X truth", "--no-attest"])
        self.assertEqual(rc, 0)
        self.assertIn("attestation skipped", out)
        with open(self.entry_path()) as f:
            self.assertNotIn("attest_payload", f.read())
        self.assertFalse(os.path.exists(os.environ["STUB_LOG"]))

    def test_queue_on_substrate_unavailable(self):
        self.write_stub(FAILING_STUB)
        rc, out, _ = self.run_verb(premise.cmd_premise, ["law-x | The X truth"])
        self.assertEqual(rc, 0)
        self.assertIn("attestation pending (substrate unavailable)", out)
        # the premise is stored anyway
        self.assertTrue(os.path.exists(self.entry_path()))
        with open(premise._queue_path()) as f:
            rec = json.loads(f.read().strip())
        self.assertEqual(rec["id"], "law-x")
        self.assertEqual(rec["payload"], premise.digest_payload("The X truth"))
        self.assertEqual(rec["profile"], "stub-prof")
        self.assertIn("meld join failed", rec["reason"])

    def test_queue_on_missing_binary(self):
        os.environ["HELM_CELL_BIN"] = os.path.join(self.tmp, "no-such")
        rc, out, _ = self.run_verb(premise.cmd_premise, ["law-x | The X truth"])
        self.assertEqual(rc, 0)
        self.assertIn("attestation pending (substrate unavailable)", out)
        with open(premise._queue_path()) as f:
            self.assertIn("not found", json.loads(f.read().strip())["reason"])

    def test_default_profile_is_test_signed(self):
        os.environ.pop("HELM_CELL_PROFILE", None)
        self.assertEqual(premise.attest_profile(), "helm-test")
        os.environ["HELM_CELL_PROFILE"] = "user-cell"
        self.assertEqual(premise.attest_profile(), "user-cell")

    def test_project_scoped_capture(self):
        self.write_stub()
        rc, _, _ = self.run_verb(premise.cmd_premise,
                                 ["p-law | scoped truth", "--project", "p1"])
        self.assertEqual(rc, 0)
        p = os.path.join(home.project_dir("p1"), "premises", "prior-p-law.md")
        self.assertTrue(os.path.exists(p))

    def test_usage(self):
        rc, _, err = self.run_verb(premise.cmd_premise, [])
        self.assertEqual(rc, 2)
        rc, _, err = self.run_verb(premise.cmd_premise, ["only-an-id"])
        self.assertEqual(rc, 2)


class CheckTest(PremiseBase):
    def capture(self, statement="The X truth"):
        self.write_stub()
        rc, _, _ = self.run_verb(premise.cmd_premise, ["law-x | " + statement])
        self.assertEqual(rc, 0)

    def with_status(self, status, args):
        prior = premise._fetch_turn_status
        premise._fetch_turn_status = lambda turn: status
        try:
            return self.run_verb(premise.cmd_premise_check, args)
        finally:
            premise._fetch_turn_status = prior

    def test_match_quotes_attested_tier(self):
        self.capture()
        rc, out, _ = self.with_status(
            {"attested_height": 43, "consensus_final": True,
             "receipt_present": True, "receipt_hash": "rs", "turn_hash": TURN},
            ["law-x"])
        self.assertEqual(rc, 0)
        self.assertIn("digest: MATCH prem:b2b:", out)
        self.assertIn("turn: " + TURN, out)
        self.assertIn("finality tier: attested-after-next-height "
                      "(consensus_final at attested_height 43)", out)
        self.assertIn("attested by profile 'stub-prof'", out)

    def test_ingress_tier_quoted_honestly(self):
        self.capture()
        rc, out, _ = self.with_status(
            {"attested_height": None, "consensus_final": False,
             "receipt_present": True, "receipt_hash": "rs", "turn_hash": TURN},
            ["law-x"])
        self.assertEqual(rc, 0)
        self.assertIn("finality tier: ingress-immediate", out)

    def test_node_unreachable_tier(self):
        self.capture()
        rc, out, _ = self.with_status(None, ["law-x"])
        self.assertEqual(rc, 0)  # digest still matches; tier honestly unverified
        self.assertIn("finality tier: unverified (node unreachable)", out)

    def test_mismatch_on_tampered_statement(self):
        self.capture()
        path = self.entry_path()
        with open(path) as f:
            raw = f.read()
        pk.atomic_write(path, raw.replace("The X truth", "A tampered truth"))
        rc, out, _ = self.with_status({"receipt_present": True}, ["law-x"])
        self.assertEqual(rc, 1)
        self.assertIn("digest: MISMATCH", out)
        self.assertIn("attested:   prem:b2b:", out)
        self.assertIn("recomputed: prem:b2b:", out)

    def test_no_attestation_recorded_is_plain(self):
        self.write_stub()
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
