#!/usr/bin/env python3
"""premise --retry-queue tests — the OPTIONAL external-anchor recovery path.
The native record is the primary proof and always lands at capture; only the
best-effort dregg anchor ever queues, and --retry-queue re-attempts it once a
node appears. Hermetic: HELM_HOME is a tempdir, the node points at a dead port,
cell.anchor_submit is mocked for the replay."""
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

from helm import cell, home, pk, premise  # noqa: E402

TURN = "cd" * 32
DEAD = "http://127.0.0.1:1"


class RetryQueueBase(unittest.TestCase):
    ENV_KEYS = ("HELM_HOME", "HELM_ADOPTED_DIR", "HELM_NODE_URL",
                "HELM_CELL_PROFILE", "MELD_AGENT_PROFILE")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-premretry-")
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

    def run_verb(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = premise.cmd_premise(list(args))
        return rc, out.getvalue(), err.getvalue()

    def enqueue_two(self):
        """Capture two premises against a dead node -> the native records land,
        two anchor rows queue."""
        rc, out, _ = self.run_verb(["law-a | truth A"])
        self.assertEqual(rc, 0)
        self.assertIn("external anchor: none yet", out)
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

    def rec_hash_of(self, pid):
        meta = pk.parse_simple_frontmatter(
            os.path.join(home.global_dir(), "premises", "prior-%s.md" % pid),
            {"attest_record": ""})
        return (meta or {}).get("attest_record") or ""


class RetryQueueTest(RetryQueueBase):
    def test_full_replay_anchors_and_drains(self):
        self.enqueue_two()
        # native proof is already recorded; only the anchor is missing
        self.assertIn("attest_record", self.entry_raw("law-a"))
        self.assertNotIn("attest_anchor_turn", self.entry_raw("law-a"))
        sent = []

        def anchor(rec_hash, memo=None, timeout=8):
            sent.append(rec_hash)
            return TURN, None

        with mock.patch.object(cell, "anchor_submit", anchor):
            rc, out, _ = self.run_verb(["--retry-queue"])
        self.assertEqual(rc, 0)
        self.assertIn("anchored 'law-a'", out)
        self.assertIn("anchored 'law-b'", out)
        self.assertIn("2 anchored, 0 still pending", out)
        self.assertEqual(self.queue_rows(), [])
        self.assertEqual(sorted(sent),
                         sorted([self.rec_hash_of("law-a"), self.rec_hash_of("law-b")]))
        for pid in ("law-a", "law-b"):
            raw = self.entry_raw(pid)
            self.assertIn("  attest_anchor_turn: " + TURN, raw)
            self.assertIn("anchored digest at turn " + TURN, raw)

    def test_partial_replay_keeps_the_failing_row(self):
        self.enqueue_two()
        good = self.rec_hash_of("law-a")

        def half_up(rec_hash, memo=None, timeout=8):
            return (TURN, None) if rec_hash == good else (None, "still down")

        with mock.patch.object(cell, "anchor_submit", half_up):
            rc, out, _ = self.run_verb(["--retry-queue"])
        self.assertEqual(rc, 1)
        self.assertIn("1 anchored, 1 still pending", out)
        self.assertIn("attest_anchor_turn", self.entry_raw("law-a"))
        self.assertNotIn("attest_anchor_turn", self.entry_raw("law-b"))
        kept = self.queue_rows()
        self.assertEqual([r["id"] for r in kept], ["law-b"])
        self.assertEqual(kept[0]["reason"], "still down")
        # a second replay once the node is fully back drains the remainder
        with mock.patch.object(cell, "anchor_submit", return_value=(TURN, None)):
            rc, out, _ = self.run_verb(["--retry-queue"])
        self.assertEqual(rc, 0)
        self.assertIn("1 anchored, 0 still pending", out)
        self.assertEqual(self.queue_rows(), [])

    def test_vanished_entry_stays_queued_with_reason(self):
        self.enqueue_two()
        os.remove(os.path.join(home.global_dir(), "premises", "prior-law-b.md"))
        with mock.patch.object(cell, "anchor_submit", return_value=(TURN, None)):
            rc, out, _ = self.run_verb(["--retry-queue"])
        self.assertEqual(rc, 1)
        self.assertIn("1 anchored, 1 still pending", out)
        kept = self.queue_rows()
        self.assertEqual([r["id"] for r in kept], ["law-b"])
        self.assertEqual(kept[0]["reason"], "entry no longer in the store")

    def test_already_anchored_row_dropped_unsent(self):
        self.enqueue_two()
        # law-a gets anchored out of band; its queue row must drop unsent
        with mock.patch.object(cell, "anchor_submit", return_value=(TURN, None)):
            self.run_verb(["--retry-queue"])   # anchors both, drains queue
        # re-queue a stale row for the already-anchored law-a
        premise._enqueue({"ts": "t", "id": "law-a", "project": None,
                          "rec_hash": self.rec_hash_of("law-a"),
                          "kind": "anchor", "reason": "stale"})
        with mock.patch.object(cell, "anchor_submit",
                               side_effect=AssertionError("must not re-anchor")):
            rc, out, _ = self.run_verb(["--retry-queue"])
        self.assertEqual(rc, 0)
        self.assertIn("1 already-anchored row dropped", out)
        self.assertEqual(self.queue_rows(), [])

    def test_empty_queue_is_quiet(self):
        with mock.patch.object(cell, "anchor_submit",
                               side_effect=AssertionError("must not send")):
            rc, out, _ = self.run_verb(["--retry-queue"])
        self.assertEqual(rc, 0)
        self.assertIn("attest queue empty", out)


if __name__ == "__main__":
    unittest.main()
