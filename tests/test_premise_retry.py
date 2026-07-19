#!/usr/bin/env python3
"""premise --retry-queue tests — the attestation recovery path (the ONLY
mechanism that ever drains attest-queue.jsonl once the ledger node returns).
Hermetic: HELM_HOME is a tempdir and cell.send_self is mocked — the real node,
~/.dregg and ~/.helm are never touched."""
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

os.environ.setdefault("HELM_HOME", tempfile.mkdtemp(prefix="helm-test-home-"))

from helm import cell, home, premise  # noqa: E402

TURN = "cd" * 32

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_ADOPTED_DIR", "HELM_CELL_BIN",
            "MELD_CELL_BIN", "HELM_CELL_PROFILE", "MELD_AGENT_PROFILE")

DOWN = (None, "meld join failed: node down")


def ok_info(turn=TURN, chain=7):
    return ({"turn_hash": turn, "receipt_hash": "rq", "chain_index": chain}, None)


class RetryQueueBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-premretry-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])
        os.environ["HELM_CELL_PROFILE"] = "stub-prof"

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_verb(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = premise.cmd_premise(list(args))
        return rc, out.getvalue(), err.getvalue()

    def enqueue_two(self):
        """Capture two premises against a down substrate -> queue of 2."""
        with mock.patch.object(cell, "send_self", return_value=DOWN):
            rc, out, _ = self.run_verb(["law-a | truth A"])
            self.assertEqual(rc, 0)
            self.assertIn("attestation pending", out)
            rc, out, _ = self.run_verb(["law-b | truth B"])
            self.assertEqual(rc, 0)
        self.assertEqual(len(self.queue_rows()), 2)

    def queue_rows(self):
        try:
            with open(premise._queue_path()) as f:
                return [json.loads(l) for l in f if l.strip()]
        except OSError:
            return []

    def entry_raw(self, pid):
        p = os.path.join(home.global_dir(), "premises", "prior-%s.md" % pid)
        with open(p) as f:
            return f.read()


class RetryQueueTest(RetryQueueBase):
    def test_full_replay_annotates_and_drains(self):
        self.enqueue_two()
        self.assertNotIn("attest_turn", self.entry_raw("law-a"))
        sent = []

        def working(payload, profile):
            sent.append((payload, profile))
            return ok_info()

        with mock.patch.object(cell, "send_self", working):
            rc, out, _ = self.run_verb(["--retry-queue"])
        self.assertEqual(rc, 0)
        self.assertIn("attested 'law-a' from queue", out)
        self.assertIn("attested 'law-b' from queue", out)
        self.assertIn("2 attested, 0 still pending", out)
        self.assertEqual(self.queue_rows(), [])
        for pid, stmt in (("law-a", "truth A"), ("law-b", "truth B")):
            raw = self.entry_raw(pid)
            self.assertIn("  attest_turn: " + TURN, raw)
            self.assertIn("  attest_receipt: rq", raw)
            self.assertIn("  attest_payload: " + premise.digest_payload(stmt), raw)
            self.assertIn("  attest_by: stub-prof", raw)
        # the replay resent the QUEUED payloads under the QUEUED profile
        self.assertEqual(sorted(sent), sorted(
            [(premise.digest_payload("truth A"), "stub-prof"),
             (premise.digest_payload("truth B"), "stub-prof")]))

    def test_partial_replay_keeps_the_failing_row(self):
        self.enqueue_two()
        good = premise.digest_payload("truth A")

        def half_up(payload, profile):
            return ok_info() if payload == good else (None, "still down")

        with mock.patch.object(cell, "send_self", half_up):
            rc, out, _ = self.run_verb(["--retry-queue"])
        self.assertEqual(rc, 1)  # something still pending -> nonzero
        self.assertIn("1 attested, 1 still pending", out)
        self.assertIn("attest_turn", self.entry_raw("law-a"))
        self.assertNotIn("attest_turn", self.entry_raw("law-b"))
        kept = self.queue_rows()
        self.assertEqual([r["id"] for r in kept], ["law-b"])
        self.assertEqual(kept[0]["reason"], "still down")
        self.assertEqual(kept[0]["payload"], premise.digest_payload("truth B"))
        # a second replay once the node is fully back drains the remainder
        with mock.patch.object(cell, "send_self", return_value=ok_info()):
            rc, out, _ = self.run_verb(["--retry-queue"])
        self.assertEqual(rc, 0)
        self.assertIn("1 attested, 0 still pending", out)
        self.assertEqual(self.queue_rows(), [])
        self.assertIn("attest_turn", self.entry_raw("law-b"))

    def test_vanished_entry_stays_queued_with_reason(self):
        self.enqueue_two()
        os.remove(os.path.join(home.global_dir(), "premises", "prior-law-b.md"))
        with mock.patch.object(cell, "send_self", return_value=ok_info()):
            rc, out, _ = self.run_verb(["--retry-queue"])
        self.assertEqual(rc, 1)
        self.assertIn("1 attested, 1 still pending", out)
        kept = self.queue_rows()
        self.assertEqual([r["id"] for r in kept], ["law-b"])
        self.assertEqual(kept[0]["reason"], "entry no longer in the store")

    def test_empty_queue_is_quiet(self):
        with mock.patch.object(cell, "send_self",
                               side_effect=AssertionError("must not send")):
            rc, out, _ = self.run_verb(["--retry-queue"])
        self.assertEqual(rc, 0)
        self.assertIn("attest queue empty", out)


if __name__ == "__main__":
    unittest.main()
