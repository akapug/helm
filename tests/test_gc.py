#!/usr/bin/env python3
"""gc tests — hermetic (the test_store pattern): HELM_HOME + HELM_CACHE_DIR +
HELM_ADOPTED_DIR all point at a tempdir; real estate never touched. Exhaust is
PLANTED (oversized ledgers, stale session files, dated backups) and the laws
proven: dry-run reaps nothing, apply reaps only class-exhaust, authored bytes
survive byte-identical, premise-check survives a gc pass."""
import contextlib
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("HELM_HOME", tempfile.mkdtemp(prefix="helm-test-home-"))

from helm import gc, home, pk, premise, store  # noqa: E402

OLD = 45 * gc.DAY   # comfortably past every prune budget
BIG = 6 * gc.MB     # comfortably past every size budget


def run(fn, args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = fn(args)
    return rc, out.getvalue(), err.getvalue()


class GcBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-gc-")
        # Hermetic cwd: the work-worktrees stream reads the AMBIENT git repo
        # (find_root walks up from cwd, no HELM_HOME seam) — a non-repo cwd
        # isolates it so an empty estate stays empty regardless of live lanes.
        self.cwd_prior = os.getcwd()
        os.chdir(self.tmp)
        self.env_prior = {k: os.environ.get(k) for k in (
            "HELM_HOME", "HELM_CACHE_DIR", "MELD_CACHE_DIR", "HELM_ADOPTED_DIR")}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CACHE_DIR"] = os.path.join(self.tmp, "cache")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.environ.pop("MELD_CACHE_DIR", None)
        for d in ("cache", "adopted"):
            os.makedirs(os.path.join(self.tmp, d))

    def tearDown(self):
        os.chdir(self.cwd_prior)
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def plant(self, path, body="x\n", days_old=0):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        if days_old:
            t = time.time() - days_old * gc.DAY
            os.utime(path, (t, t))
        return path

    def snapshot(self, root):
        out = {}
        for r, _dirs, files in os.walk(root):
            for f in files:
                p = os.path.join(r, f)
                with open(p, "rb") as fh:
                    out[p] = fh.read()
        return out

    def row(self, rows, stream):
        return next(r for r in rows if r["stream"] == stream)


class ScanTest(GcBase):
    def test_empty_estate_all_in_budget(self):
        # The suite itself may run inside a legitimately leased worktree. The
        # temp HELM_HOME intentionally hides that real lease registry, so an
        # unmocked host scan would misclassify the test runner as an orphan.
        with mock.patch("helm.work.gc_orphans", return_value=[]):
            rc, out, _ = run(gc.cmd_gc, [])
        self.assertEqual(rc, 0)
        self.assertIn("every stream in budget", out)
        self.assertIn("(%d declared)" % len(gc.POLICIES), out)

    def test_bad_flag_is_usage(self):
        rc, _, err = run(gc.cmd_gc, ["--force"])
        self.assertEqual(rc, 2)
        self.assertIn("usage", err)
        rc, _, _ = run(gc.cmd_gc, ["--dry", "--apply"])
        self.assertEqual(rc, 2)

    def test_size_age_and_count_budgets_measure(self):
        self.plant(gc._cache("keepalive-log.jsonl"), "x" * BIG)
        self.plant(gc._state("inject-seen", "old.json"), days_old=OLD // gc.DAY)
        self.plant(gc._state("inject-seen", "new.json"))
        rows = gc.scan()
        self.assertTrue(self.row(rows, "keepalive-log")["over"])
        seen = self.row(rows, "inject-seen")
        self.assertTrue(seen["over"])
        self.assertEqual([os.path.basename(v) for v in seen["victims"]],
                         ["old.json"])
        self.assertFalse(self.row(rows, "events")["over"])

    def test_stamp_beats_mtime_for_backup_age(self):
        # old stamp + fresh mtime -> stale; fresh stamp + old mtime -> kept
        # (copy2/move carry the ORIGIN's mtime — the wrong axis)
        stale = time.strftime("%Y%m%dT%H%M%S", time.gmtime(time.time() - OLD))
        fresh = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        self.plant(gc._cache("config-backups", stale + "-0__a.json"))
        self.plant(gc._cache("config-backups", fresh + "-0__b.json"),
                   days_old=OLD // gc.DAY)
        victims = self.row(gc.scan(), "config-backups")["victims"]
        self.assertEqual([os.path.basename(v) for v in victims],
                         [stale + "-0__a.json"])

    def test_active_session_dir_stays_fresh(self):
        # dir mtime old, one file inside fresh -> newest-mtime-within keeps it
        d = gc._state("reflex-state", "sid1")
        self.plant(os.path.join(d, "counters.json"), "{}")
        t = time.time() - OLD
        os.utime(d, (t, t))
        self.assertFalse(self.row(gc.scan(), "reflex-state")["over"])

    def test_fail_open_stream_error_continues(self):
        os.makedirs(gc._state())
        self.plant(gc._state("inject-seen"))  # a FILE where a dir belongs
        self.plant(gc._cache("keepalive-log.jsonl"), "x" * BIG)
        rows = gc.scan()
        self.assertTrue(self.row(rows, "keepalive-log")["over"])  # sweep went on
        rc, out, _ = run(gc.cmd_gc, [])
        self.assertEqual(rc, 0)


class DryRunTest(GcBase):
    def test_dry_default_reaps_nothing(self):
        self.plant(gc._cache("keepalive-log.jsonl"), "x" * BIG)
        self.plant(gc._state("inject-seen", "old.json"), days_old=OLD // gc.DAY)
        self.plant(gc._state("attest-queue.jsonl"), "q" * (300 * 1024))
        before = self.snapshot(self.tmp)
        for args in ([], ["--dry"]):
            rc, out, _ = run(gc.cmd_gc, args)
            self.assertEqual(rc, 0)
            self.assertIn("would", out)
            self.assertIn("nothing touched", out)
        self.assertEqual(self.snapshot(self.tmp), before)


class ApplyTest(GcBase):
    def test_rotate_is_atomic_dated_and_byte_preserving(self):
        body = "x" * BIG
        live = self.plant(gc._cache("keepalive-log.jsonl"), body)
        rc, out, _ = run(gc.cmd_gc, ["--apply"])
        self.assertEqual(rc, 0)
        self.assertIn("rotated", out)
        self.assertFalse(os.path.exists(live))
        gens = [p for p in os.listdir(gc._cache())
                if p.startswith("keepalive-log.jsonl.")]
        self.assertEqual(len(gens), 1)
        with open(gc._cache(gens[0]), encoding="utf-8") as f:
            self.assertEqual(f.read(), body)

    def test_prune_reaps_stale_keeps_fresh(self):
        old = self.plant(gc._state("inject-seen", "old.json"),
                         days_old=OLD // gc.DAY)
        new = self.plant(gc._state("inject-seen", "new.json"))
        gen = self.plant(gc._state("events.jsonl.1"), days_old=OLD // gc.DAY)
        sdir = gc._state("reflex-state", "dead")
        self.plant(os.path.join(sdir, "counters.json"), "{}",
                   days_old=OLD // gc.DAY)
        t = time.time() - OLD
        os.utime(sdir, (t, t))
        cache = self.plant(gc._cache("store-cache-abc123.json"),
                           days_old=OLD // gc.DAY)
        rc, out, _ = run(gc.cmd_gc, ["--apply"])
        self.assertEqual(rc, 0)
        for gone in (old, gen, sdir, cache):
            self.assertFalse(os.path.exists(gone), gone)
        self.assertTrue(os.path.exists(new))

    def test_apply_writes_one_receipt(self):
        self.plant(gc._state("inject-seen", "old.json"), days_old=OLD // gc.DAY)
        run(gc.cmd_gc, ["--apply"])
        events = pk.read_events(10)
        self.assertEqual([e["verb"] for e in events], ["gc"])
        self.assertIn("reaped 1 item", events[0]["summary"])

    def test_report_only_streams_never_reaped(self):
        queue = self.plant(gc._state("attest-queue.jsonl"), "q" * (300 * 1024))
        trash = self.plant(gc._cache("skills-trash", "20250101T000000-0-old.md"))
        drain = self.plant(os.path.join(os.environ["HELM_ADOPTED_DIR"],
                                        "archive", "drain-2025010100", "raw.md"),
                           days_old=200)
        rc, out, _ = run(gc.cmd_gc, ["--apply"])
        self.assertEqual(rc, 0)
        for kept in (queue, trash, drain):
            self.assertTrue(os.path.exists(kept), kept)
        self.assertIn("report-only", out)
        self.assertIn("--retry-queue", out)
        self.assertIn("owner reaps", out)


class AuthoredSafetyTest(GcBase):
    def test_apply_never_touches_authored_content(self):
        home.scaffold_global()
        store.write_prior({"id": "law1", "statement": "authored survives gc",
                           "confidence": 0.9, "stated_ts": pk.now_ts()})
        self.plant(os.path.join(home.global_dir(), "heuristics", "h.md"), "# h\n",
                   days_old=OLD // gc.DAY)
        self.plant(os.path.join(home.project_dir("proj"), "premises", "p.md"),
                   "# p\n", days_old=OLD // gc.DAY)
        authored = {p: b for p, b in self.snapshot(self.tmp).items()
                    if ".state" not in p and os.sep + "cache" + os.sep not in p}
        self.assertGreaterEqual(len(authored), 3)
        self.plant(gc._cache("keepalive-log.jsonl"), "x" * BIG)
        self.plant(gc._state("inject-seen", "old.json"), days_old=OLD // gc.DAY)
        run(gc.cmd_gc, ["--apply"])
        after = self.snapshot(self.tmp)
        for p, body in authored.items():
            self.assertEqual(after.get(p), body, p)

    def test_premise_check_survives_a_gc_pass(self):
        # the card's declared risk test: gc must never break digest verification
        stmt = "gc reaps exhaust, never authored bytes"
        # a REAL attested entry (native record lands) — the risk test is that gc
        # must never break the primary proof, so it must be a proof, not a
        # payload-only stub (which is now honestly NOT ATTESTED).
        rc, _, _ = run(premise.cmd_premise, ["gc-law", "|", stmt])
        self.assertEqual(rc, 0)
        self.plant(gc._cache("keepalive-log.jsonl"), "x" * BIG)
        self.plant(gc._state("inject-seen", "old.json"), days_old=OLD // gc.DAY)
        rc, out, _ = run(gc.cmd_gc, ["--apply"])
        self.assertEqual(rc, 0)
        rc, out, _ = run(premise.cmd_premise_check, ["gc-law"])
        self.assertEqual(rc, 0)
        self.assertIn("MATCH", out)


class TableTest(GcBase):
    def test_every_policy_declares_one_budget_axis(self):
        for p in gc.POLICIES:
            axes = [k for k in ("size", "age", "count") if k in p]
            self.assertEqual(len(axes), 1, p["stream"])
            self.assertIn(p["cls"], ("exhaust", "source", "archive", "state"))
            self.assertIn(p["act"], ("rotate", "prune", "report"))

    def test_only_exhaust_is_ever_reapable(self):
        for p in gc.POLICIES:
            if p["cls"] != "exhaust":
                self.assertEqual(p["act"], "report", p["stream"])
            row = {"over": True, "cls": p["cls"]}
            self.assertEqual(gc._reapable(row), p["cls"] == "exhaust")


if __name__ == "__main__":
    unittest.main()
