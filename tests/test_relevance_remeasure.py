"""The gate on the claim (helm/relevance_remeasure.py).

A project that may leave the LAN runs local-only by choice only after the
frozen head has been re-scored on a held-out split of the evaluator's labels
and PASSED. The split is stable and never holds an excluded turn; a head that
agrees with the evaluator passes and one that does not fails; the receipt binds
the head it measured; a sample run never passes. Labels here are synthetic.
"""
import contextlib
import hashlib
import io
import json
import os
import random
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-home-", var="HELM_HOME")

from helm import home, relevance, relevance_remeasure as rm  # noqa: E402


class RemeasureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-remeasure-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        prior = os.environ.get("HELM_HOME")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.addCleanup(lambda: os.environ.__setitem__("HELM_HOME", prior) if prior
                        else os.environ.pop("HELM_HOME", None))
        rng = random.Random(7)
        self.items, self.labels, self.train = [], [], []
        for t in range(400):
            turn = "turn-%03d" % t
            p = {}
            for j in range(5):
                pair = "%s#e%d" % (turn, j)
                self.items.append({"pair": pair, "turn": turn, "content": "synthetic %d" % t,
                                   "line": "PREMISE e%d: note %d" % (j, j)})
                p[pair] = round(rng.random(), 3)
            self.labels.append({"turn": turn, "p": p})
            if t % 10 == 0:
                self.train.append({"turn": turn})
        self.items_path = self.write("items.jsonl", self.items)
        self.labels_path = self.write("labels.jsonl", self.labels)
        self.train_path = self.write("train.jsonl", self.train)
        self.flat = {k: v for r in self.labels for k, v in r["p"].items()}

    def write(self, name, rows):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as f:
            f.writelines(json.dumps(r) + "\n" for r in rows)
        return path

    def teacher(self, text, rows):
        """A head that reproduces the evaluator exactly."""
        return {r["id"]: self.flat[r["id"]] for r in rows}, "head-x", 0.32

    def coin(self, text, rows):
        """A head that ignores the evaluator."""
        return {r["id"]: int(hashlib.sha256(r["id"].encode()).hexdigest()[:6], 16) / float(1 << 24)
                for r in rows}, "head-x", 0.32

    def run_gate(self, score, **crit):
        crit = dict({"min_turns": 50, "boot": 50, "workers": 1}, **crit)
        return rm.run(self.items_path, self.labels_path, [self.train_path], crit,
                      score=score, health=lambda: {"model": "head-x"})

    def test_the_split_is_stable_and_never_holds_an_excluded_turn(self):
        turns = rm.split(self.items_path, rm.load_labels(self.labels_path), [self.train_path], 0.3)
        again = rm.split(self.items_path, rm.load_labels(self.labels_path), [self.train_path], 0.3)
        self.assertEqual(sorted(turns), sorted(again))
        self.assertTrue(80 < len(turns) < 160, len(turns))
        self.assertEqual({t for t in turns} & {r["turn"] for r in self.train}, set())
        self.assertTrue(all(rm.heldout(t, 0.3) for t in turns))
        self.assertEqual(turns[sorted(turns)[0]]["rows"][0]["text"], "e0: note 0")

    def test_a_head_that_agrees_passes_and_one_that_does_not_fails(self):
        good = self.run_gate(self.teacher)
        self.assertTrue(good["pass"], good["fails"])
        self.assertEqual((good["head"], good["metrics"]["auc"]), ("head-x", 1.0))
        self.assertEqual(good["metrics"]["recall_of_evaluator_keeps"], 1.0)
        bad = self.run_gate(self.coin)
        self.assertFalse(bad["pass"])
        self.assertTrue(any("AUC 90% lower bound" in f for f in bad["fails"]), bad["fails"])

    def test_too_few_turns_never_passes(self):
        rec = self.run_gate(self.teacher, min_turns=10000)
        self.assertFalse(rec["pass"])
        self.assertIn("fewer than 10000", rec["fails"][0])

    def test_a_head_change_mid_run_is_refused(self):
        with self.assertRaises(RuntimeError):
            rm.run(self.items_path, self.labels_path, [], {"min_turns": 1, "boot": 5, "workers": 1},
                   score=self.teacher, health=lambda: {"model": "head-y"})

    def test_the_verb_writes_a_receipt_the_gate_reads_and_a_sample_never_passes(self):  # noqa: ORPHANED_MOCK — the doubles are reached through relevance_remeasure.cmd -> run -> relevance.local_scores/local_health, a cross-module path the walker does not follow
        cfg = {"min_turns": "50", "boot": "20"}
        argv = ["--items", self.items_path, "--labels", self.labels_path,
                "--exclude", self.train_path, "--min-turns", cfg["min_turns"],
                "--boot", cfg["boot"], "--workers", "1", "--apply"]
        with mock.patch.object(relevance, "local_scores",
                               lambda text, cands, c: self.teacher(text, [{"id": x["id"]} for x in cands])), \
                mock.patch.object(relevance, "local_health", lambda c: {"model": "head-x"}), \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()):
            rc = rm.cmd(argv)
            self.assertEqual((rc, relevance.choice_gate("head-x")[0]), (0, True))
            self.assertFalse(relevance.choice_gate("head-z")[0])
            rc = rm.cmd(argv + ["--limit-turns", "5"])
        self.assertEqual(rc, 3)
        self.assertIn("PASS", out.getvalue())
        rec = json.load(open(relevance.receipt_path()))
        self.assertEqual((rec["pass"], rec["head"]), (False, "head-x"))
        self.assertFalse(relevance.choice_gate("head-x")[0])
        self.assertTrue(os.path.dirname(relevance.receipt_path()).startswith(home.global_dir()))


if __name__ == "__main__":
    unittest.main()
