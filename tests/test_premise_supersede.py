#!/usr/bin/env python3
"""supersession-chain tests — DECISION clauses 5-6: `premise --supersede`
(store lifecycle + ONE signed linking turn), `premise-check --chain` (the
attested biography), the bind-at-replay queue ordering, and the capture
guard that refuses attestation-orphaning edits. Hermetic: HELM_HOME is a
tempdir, the substrate binary is a stub or cell.send_self is mocked — the
real node, ~/.dregg and ~/.helm are never touched."""
import contextlib
import io
import json
import os
import shutil
import stat
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("HELM_HOME", tempfile.mkdtemp(prefix="helm-test-home-"))

from helm import cell, home, pk, premise, store  # noqa: E402

CELL_HEX = "ab" * 32
TURN1 = "cd" * 32
TURN2 = "ef" * 32

ENV_KEYS = ("HELM_HOME", "HELM_ADOPTED_DIR", "HELM_CELL_BIN", "MELD_CELL_BIN",
            "HELM_NODE_URL", "HELM_CELL_PROFILE", "MELD_NODE_URL",
            "MELD_AGENT_PROFILE", "STUB_LOG", "STUB_CELL", "STUB_TURN")

STUB = """#!/bin/sh
echo "argv:$@" >> "$STUB_LOG"
case "$1" in
  join) echo '{"joined":true,"cell":"'"$STUB_CELL"'","turn_hash":"tj","receipt_hash":"rj","chain_index":1}';;
  send) echo '{"sent":true,"to":"'"$STUB_CELL"'","seq":1,"bytes":89,"slots":11,"turn_hash":"'"$STUB_TURN"'","receipt_hash":"rs","chain_index":2}';;
esac
exit 0
"""

FAILING_STUB = "#!/bin/sh\necho 'node down' >&2\nexit 1\n"


class SupBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-sup-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])
        os.environ["STUB_LOG"] = os.path.join(self.tmp, "stub.log")
        os.environ["STUB_CELL"] = CELL_HEX
        os.environ["STUB_TURN"] = TURN1
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


class SupPayloadTest(SupBase):
    def test_sup_payload_form_and_budget(self):
        p = premise.sup_payload("truth two", TURN1)
        digest = premise.digest_payload("truth two")[len(premise.DIGEST_TAG):]
        self.assertEqual(p, "sup:b2b:" + digest + ":" + TURN1[:16])
        self.assertEqual(len(p.encode("utf-8")), 89)  # inside the 104B budget

    def test_payload_digest_and_ptr_cover_both_forms(self):
        prem = premise.digest_payload("x")
        sup = premise.sup_payload("x", TURN1)
        self.assertEqual(premise.payload_digest(prem), premise.payload_digest(sup))
        self.assertEqual(premise.payload_ptr(sup), TURN1[:16])
        self.assertEqual(premise.payload_ptr(prem), "")
        self.assertEqual(premise.payload_digest("garbage"), "")


class SupersedeFlowTest(SupBase):
    def test_full_flow_links_chain(self):
        self.write_stub()
        self.capture_v1()
        os.environ["STUB_TURN"] = TURN2  # the sup turn gets its own hash
        rc, out, _ = self.run_verb(
            premise.cmd_premise,
            ["--supersede", "law-v1", "law-v2 | truth two | kw2 | dev"])
        self.assertEqual(rc, 0)
        self.assertIn("LIVE 'law-v2' [certain 1.00] - truth two", out)
        self.assertIn("supersedes 'law-v1'", out)
        self.assertIn("attested: turn " + TURN2, out)
        self.assertIn("pointer, not a proof", out)
        # OLD: tombstoned in place, attest keys carried through the rewrite
        old = self.entry_raw("law-v1")
        self.assertIn("  status: delete_eligible", old)
        self.assertIn("  replaced_by: law-v2", old)
        self.assertIn("  attest_turn: " + TURN1, old)
        # NEW: backpointer + the sup: payload + FULL linkage in frontmatter
        new = self.entry_raw("law-v2")
        self.assertIn("  supersedes: law-v1", new)
        self.assertIn("  attest_payload: " + premise.sup_payload("truth two", TURN1),
                      new)
        self.assertIn("  attest_turn: " + TURN2, new)
        self.assertIn("  attest_supersedes_turn: " + TURN1, new)
        # the payload rode the SEND path (whisper slots), never heartbeat
        with open(os.environ["STUB_LOG"]) as f:
            log = f.read()
        self.assertIn("argv:send --profile stub-prof --to " + CELL_HEX + " "
                      + premise.sup_payload("truth two", TURN1), log)
        self.assertNotIn("heartbeat", log)

    def test_never_attested_old_starts_chain_honestly(self):
        self.write_stub()
        self.capture_v1(["--no-attest"])
        rc, out, _ = self.run_verb(premise.cmd_premise,
                                   ["--supersede", "law-v1", "law-v2 | truth two"])
        self.assertEqual(rc, 0)
        self.assertIn("never attested — the chain starts here", out)
        new = self.entry_raw("law-v2")
        self.assertIn("  attest_payload: " + premise.digest_payload("truth two"),
                      new)
        self.assertNotIn("attest_supersedes_turn", new)
        self.assertIn("  replaced_by: law-v2", self.entry_raw("law-v1"))

    def test_refusals(self):
        self.write_stub()
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
        # a tip never forks: superseding an already-superseded id is refused
        rc, _, _ = self.run_verb(premise.cmd_premise,
                                 ["--supersede", "law-v1", "law-v2 | truth two"])
        self.assertEqual(rc, 0)
        rc, _, err = self.run_verb(premise.cmd_premise,
                                   ["--supersede", "law-v1", "law-v3 | truth three"])
        self.assertEqual(rc, 1)
        self.assertIn("never forks", err)

    def test_node_down_lifecycle_lands_and_sup_row_queues(self):
        self.write_stub()
        self.capture_v1()
        self.write_stub(FAILING_STUB)
        rc, out, _ = self.run_verb(premise.cmd_premise,
                                   ["--supersede", "law-v1", "law-v2 | truth two"])
        self.assertEqual(rc, 0)
        self.assertIn("attestation pending (substrate unavailable)", out)
        self.assertIn("replayed in order", out)
        # the STORE lifecycle landed despite the outage
        self.assertIn("  status: delete_eligible", self.entry_raw("law-v1"))
        new = self.entry_raw("law-v2")
        self.assertIn("  supersedes: law-v1", new)
        self.assertNotIn("attest_payload", new)
        rows = self.queue_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "law-v2")
        self.assertEqual(rows[0]["sup_of"], "law-v1")
        self.assertEqual(rows[0]["digest"],
                         premise.payload_digest(premise.digest_payload("truth two")))
        self.assertNotIn("payload", rows[0])

    def test_replay_binds_prior_turn_in_queue_order(self):
        """The money path: BOTH the prem: and the sup: turn queued during one
        outage — replay attests the old premise first, then the sup row binds
        that fresh turn hash at send time."""
        with mock.patch.object(cell, "send_self",
                               return_value=(None, "node down")):
            self.capture_v1()
            rc, _, _ = self.run_verb(premise.cmd_premise,
                                     ["--supersede", "law-v1", "law-v2 | truth two"])
            self.assertEqual(rc, 0)
        self.assertEqual([r["id"] for r in self.queue_rows()],
                         ["law-v1", "law-v2"])
        sent = []

        def working(payload, profile):
            sent.append(payload)
            return ({"turn_hash": TURN1 if len(sent) == 1 else TURN2,
                     "receipt_hash": "rq", "chain_index": len(sent)}, None)

        with mock.patch.object(cell, "send_self", working):
            rc, out, _ = self.run_verb(premise.cmd_premise, ["--retry-queue"])
        self.assertEqual(rc, 0)
        self.assertIn("2 attested, 0 still pending", out)
        self.assertEqual(sent, [premise.digest_payload("truth one"),
                                premise.sup_payload("truth two", TURN1)])
        self.assertIn("  attest_turn: " + TURN1, self.entry_raw("law-v1"))
        new = self.entry_raw("law-v2")
        self.assertIn("  attest_payload: " + premise.sup_payload("truth two", TURN1),
                      new)
        self.assertIn("  attest_supersedes_turn: " + TURN1, new)

    def test_replay_of_sup_row_over_still_unattested_prior_falls_back(self):
        with mock.patch.object(cell, "send_self",
                               return_value=(None, "node down")):
            self.capture_v1(["--no-attest"])
            self.run_verb(premise.cmd_premise,
                          ["--supersede", "law-v1", "law-v2 | truth two"])
        with mock.patch.object(cell, "send_self",
                               return_value=({"turn_hash": TURN2,
                                              "receipt_hash": "rq",
                                              "chain_index": 1}, None)):
            rc, _, _ = self.run_verb(premise.cmd_premise, ["--retry-queue"])
        self.assertEqual(rc, 0)
        new = self.entry_raw("law-v2")
        self.assertIn("  attest_payload: " + premise.digest_payload("truth two"),
                      new)
        self.assertNotIn("attest_supersedes_turn", new)


class CaptureGuardTest(SupBase):
    def test_orphaning_edit_refused_toward_supersede(self):
        self.write_stub()
        self.capture_v1()
        rc, _, err = self.run_verb(premise.cmd_premise, ["law-v1 | a NEW truth"])
        self.assertEqual(rc, 1)
        self.assertIn("orphans the attestation", err)
        self.assertIn("--supersede law-v1", err)
        self.assertIn("truth one", self.entry_raw("law-v1"))  # untouched

    def test_idempotent_restate_skips_the_second_turn(self):
        self.write_stub()
        self.capture_v1()
        rc, out, _ = self.run_verb(premise.cmd_premise,
                                   ["law-v1 | truth one | fresh-kw"])
        self.assertEqual(rc, 0)
        self.assertIn("already attested — turn " + TURN1, out)
        with open(os.environ["STUB_LOG"]) as f:
            self.assertEqual(f.read().count("argv:send"), 1)  # ONE turn ever
        raw = self.entry_raw("law-v1")
        self.assertIn("  keywords: fresh-kw", raw)
        self.assertEqual(raw.count("attest_turn:"), 1)  # never double-annotated

    def test_remint_over_tombstone_starts_fresh_lifecycle(self):
        self.write_stub()
        self.capture_v1()
        self.run_verb(premise.cmd_premise,
                      ["--supersede", "law-v1", "law-v2 | truth two"])
        rc, _, _ = self.run_verb(premise.cmd_premise, ["law-v1 | resurrected"])
        self.assertEqual(rc, 0)
        raw = self.entry_raw("law-v1")
        self.assertIn("  status: live", raw)
        for stale in ("replaced_by", "retired_ts", "attest_supersedes_turn"):
            self.assertNotIn(stale, raw)
        # freshly re-attested: exactly one attest block, the new statement's
        self.assertEqual(raw.count("attest_payload:"), 1)
        self.assertIn("  attest_payload: " + premise.digest_payload("resurrected"),
                      raw)


class VerifyLinkTest(SupBase):
    _n = 0

    def entries(self, new_payload, old_turn=TURN1, full=None):
        VerifyLinkTest._n += 1  # unique file per case — cases build eagerly
        path = os.path.join(self.tmp, "x", "prior-new-%d.md" % VerifyLinkTest._n)
        lines = ["---", "metadata:", "  id: new"]
        if full:
            lines.append("  attest_supersedes_turn: " + full)
        lines += ["---", ""]
        pk.atomic_write(path, "\n".join(lines))
        old = {"id": "old", "statement": "so", "attest_turn": old_turn}
        new = {"id": "new", "statement": "sn", "attest_payload": new_payload,
               "path": path}
        return old, new

    def test_states(self):
        good = premise.sup_payload("sn", TURN1)
        cases = (
            (self.entries(good), "attested"),
            (self.entries(good, full=TURN1), "attested"),
            (self.entries(premise.digest_payload("sn")), "unbacked"),
            (self.entries(""), "unbacked"),
            (self.entries(premise.sup_payload("WRONG stmt", TURN1)), "broken"),
            (self.entries(good, old_turn=""), "broken"),
            (self.entries(premise.sup_payload("sn", TURN2)), "broken"),
            (self.entries(good, full=TURN2), "broken"),
        )
        for (old, new), want in cases:
            self.assertEqual(premise.verify_link(old, new)[0], want,
                             (new["attest_payload"], want))


class ChainCheckTest(SupBase):
    STATUS = {"attested_height": 43, "consensus_final": True,
              "receipt_present": True}

    def build_chain(self):
        """law-a -> law-b -> law-c, each hop signed with its own turn."""
        self.write_stub()
        rc, _, _ = self.run_verb(premise.cmd_premise, ["law-a | truth one"])
        self.assertEqual(rc, 0)
        os.environ["STUB_TURN"] = TURN2
        rc, _, _ = self.run_verb(premise.cmd_premise,
                                 ["--supersede", "law-a", "law-b | truth two"])
        self.assertEqual(rc, 0)
        os.environ["STUB_TURN"] = "77" * 32
        rc, _, _ = self.run_verb(premise.cmd_premise,
                                 ["--supersede", "law-b", "law-c | truth three"])
        self.assertEqual(rc, 0)

    def check_chain(self, pid, status=STATUS):
        prior = premise._fetch_turn_status
        premise._fetch_turn_status = lambda turn: status
        try:
            return self.run_verb(premise.cmd_premise_check, ["--chain", pid])
        finally:
            premise._fetch_turn_status = prior

    def test_walks_whole_chain_from_any_link(self):
        self.build_chain()
        for pid in ("law-a", "law-b", "law-c"):
            rc, out, _ = self.check_chain(pid)
            self.assertEqual(rc, 0, out)
            self.assertIn("3 links through '%s', origin first" % pid, out)
            self.assertIn("1. law-a [delete_eligible] - truth one", out)
            self.assertIn("3. law-c [live] - truth three", out)
            self.assertEqual(out.count("digest MATCH"), 3)
            self.assertIn("link 1->2 ATTESTED — full linkage in frontmatter", out)
            self.assertIn("link 2->3 ATTESTED", out)
            self.assertIn("attested-after-next-height", out)
            self.assertIn("held 'truth one' until", out)
            self.assertIn("then 'truth three' — LIVE now", out)
            self.assertIn("pointer, not a proof", out)

    def test_tampered_statement_fails_the_chain(self):
        self.build_chain()
        p = os.path.join(home.global_dir(), "premises", "prior-law-b.md")
        with open(p) as f:
            raw = f.read()
        pk.atomic_write(p, raw.replace("truth two", "tampered two"))
        rc, out, _ = self.check_chain("law-a")
        self.assertEqual(rc, 1)
        self.assertIn("digest MISMATCH", out)
        # b's sup: digest no longer matches its stored statement -> 1->2 breaks;
        # the b->c pointer rides turn hashes, so 2->3 stays attested
        self.assertIn("link 1->2 BROKEN", out)
        self.assertIn("link 2->3 ATTESTED", out)
        rc2, _, _ = self.check_chain("law-c")
        self.assertEqual(rc2, 1)

    def test_store_only_hop_reads_unbacked_but_not_broken(self):
        self.build_chain()
        rc, _, _ = self.run_verb(store.cmd_store,
                                 ["add", "premise", "law-d | truth four"])
        self.assertEqual(rc, 0)
        _e, err = store.mark_superseded("law-c", "law-d", pk.now_ts())
        self.assertIsNone(err)
        rc, out, _ = self.check_chain("law-a")
        self.assertEqual(rc, 0)  # stated design state, not corruption
        self.assertIn("4 links", out)
        self.assertIn("link 3->4 UNBACKED", out)

    def test_single_entry_chain(self):
        self.write_stub()
        self.run_verb(premise.cmd_premise, ["solo | alone"])
        rc, out, _ = self.check_chain("solo")
        self.assertEqual(rc, 0)
        self.assertIn("1 link through 'solo'", out)
        self.assertIn("no supersession links", out)

    def test_unknown_id(self):
        rc, _, err = self.run_verb(premise.cmd_premise_check, ["--chain", "ghost"])
        self.assertEqual(rc, 1)
        self.assertIn("not found", err)


class CheckSupPayloadTest(SupBase):
    def test_single_check_reads_sup_form_and_states_the_pointer(self):
        self.write_stub()
        self.run_verb(premise.cmd_premise, ["law-a | truth one"])
        os.environ["STUB_TURN"] = TURN2
        self.run_verb(premise.cmd_premise,
                      ["--supersede", "law-a", "law-b | truth two"])
        prior = premise._fetch_turn_status
        premise._fetch_turn_status = lambda turn: {"receipt_present": True}
        try:
            rc, out, _ = self.run_verb(premise.cmd_premise_check, ["law-b"])
        finally:
            premise._fetch_turn_status = prior
        self.assertEqual(rc, 0)
        self.assertIn("digest: MATCH sup:b2b:", out)
        self.assertIn("chain: supersedes prior turn " + TURN1[:16], out)
        self.assertIn("full hash " + TURN1, out)
        self.assertIn("helm premise-check --chain law-b", out)


if __name__ == "__main__":
    unittest.main()
