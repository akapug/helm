#!/usr/bin/env python3
"""corpus tests — hermetic: root globs rebound to planted tmp estates, dest a
tmp dir, real transcripts never read. Fixtures are OBVIOUSLY SYNTHETIC (no
real content, ever). The laws proven: dry copies nothing, backup is copy-only
(sources byte-identical after every run), re-runs are cheap (cursor skip),
growth re-copies into today's dir without losing yesterday's snapshot,
mtime-only churn never duplicates a body, symlinked cred-home roots dedup,
errors fail open + retry, the space guard aborts before the first byte."""
import contextlib
import io
import json
import os
import shutil
import tempfile
import time
import unittest

os.environ.setdefault("HELM_HOME", tempfile.mkdtemp(prefix="helm-test-home-"))

from helm import corpus  # noqa: E402

FAKE = '{"type":"user","message":"SYNTHETIC-FIXTURE transcript line"}\n'


def run(fn, args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = fn(args)
    return rc, out.getvalue(), err.getvalue()


class CorpusBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-corpus-")
        self.claude = os.path.join(self.tmp, "claude-projects")
        self.codex = os.path.join(self.tmp, "codex-sessions")
        self.scratch = os.path.join(self.tmp, "tmp-claude")
        self.dest = os.path.join(self.tmp, "archive")
        self.prior = (corpus.CLAUDE_ROOT_GLOBS, corpus.CODEX_ROOT_GLOBS,
                      corpus.TMP_ROOT_GLOBS, os.environ.get("HELM_CORPUS_DEST"),
                      os.environ.get("HELM_HOME"))
        corpus.CLAUDE_ROOT_GLOBS = [self.claude]
        corpus.CODEX_ROOT_GLOBS = [self.codex]
        corpus.TMP_ROOT_GLOBS = [self.scratch]
        os.environ["HELM_CORPUS_DEST"] = self.dest
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")

    def tearDown(self):
        (corpus.CLAUDE_ROOT_GLOBS, corpus.CODEX_ROOT_GLOBS,
         corpus.TMP_ROOT_GLOBS) = self.prior[:3]
        for k, v in zip(("HELM_CORPUS_DEST", "HELM_HOME"), self.prior[3:]):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def plant(self, path, body=FAKE):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        return path

    def estate(self):
        """The standard synthetic estate: a session, its subagent, a workflow
        agent, a codex rollout — plus a .flat. derivative and scratch noise
        that must stay OUT."""
        sid = "aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb"
        planted = [
            self.plant(os.path.join(self.claude, "-home-x-proj", sid + ".jsonl")),
            self.plant(os.path.join(self.claude, "-home-x-proj", sid,
                                    "subagents", "agent-a01.jsonl")),
            self.plant(os.path.join(self.claude, "-home-x-proj", sid, "subagents",
                                    "workflows", "wf_01", "agent-a02.jsonl")),
            self.plant(os.path.join(self.codex, "2026", "07", "20",
                                    "rollout-2026-07-20T01-02-03-" + sid + ".jsonl")),
            self.plant(os.path.join(self.scratch, "-cwd-slug", sid, "scratchpad",
                                    "vhome", "projects", "-slug", "deadbeef.jsonl")),
        ]
        self.plant(os.path.join(self.claude, "-home-x-proj", sid + ".flat.jsonl"))
        self.plant(os.path.join(self.scratch, "-cwd-slug", sid, "scratchpad",
                                "inject-ledger.jsonl"))
        self.plant(os.path.join(self.codex, "2026", "07", "20", "history.jsonl"))
        return planted

    def snapshot(self, root):
        out = {}
        for r, _dirs, files in os.walk(root):
            for f in files:
                p = os.path.join(r, f)
                with open(p, "rb") as fh:
                    out[p] = fh.read()
        return out

    def archived(self):
        return sorted(os.path.relpath(p, self.dest)
                      for p in self.snapshot(self.dest)
                      if ".helm-corpus" not in p)


class ScanTest(CorpusBase):
    def test_scan_finds_transcripts_only(self):
        planted = self.estate()
        rows = corpus.scan()
        self.assertEqual(sorted(r["p"] for r in rows), sorted(planted))
        labels = {r["label"] for r in rows}
        self.assertEqual(labels, {"claude", "codex", "tmp"})

    def test_symlinked_home_roots_dedup_by_inode(self):
        self.estate()
        link = os.path.join(self.tmp, "home-projects")
        os.symlink(self.claude, link)
        corpus.CLAUDE_ROOT_GLOBS = [self.claude, link]
        rows = corpus.scan()
        self.assertEqual(len([r for r in rows if r["label"] == "claude"]), 3)
        # the canonical (first-listed) root names the surviving row
        self.assertTrue(all(r["p"].startswith(self.claude)
                            for r in rows if r["label"] == "claude"))

    def test_same_relpath_across_real_roots_never_collides(self):
        a = os.path.join(self.tmp, "rootA")
        b = os.path.join(self.tmp, "rootB")
        self.plant(os.path.join(a, "-slug", "twin.jsonl"), "A" + FAKE)
        self.plant(os.path.join(b, "-slug", "twin.jsonl"), "B" + FAKE)
        corpus.CLAUDE_ROOT_GLOBS = [a, b]
        rows = corpus.scan()
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({r["rel"] for r in rows}), 2)


class DryRunTest(CorpusBase):
    def test_dry_reports_all_copies_nothing(self):
        planted = self.estate()
        before = self.snapshot(self.tmp)
        rc, out, _ = run(corpus.cmd_corpus, ["backup", "--dry"])
        self.assertEqual(rc, 0)
        self.assertIn("nothing copied (--dry)", out)
        self.assertIn("%d would be copied" % len(planted), out)
        self.assertFalse(os.path.exists(self.dest))
        self.assertEqual(self.snapshot(self.tmp), before)


class BackupTest(CorpusBase):
    def test_backup_copies_dated_layout_sources_untouched(self):
        planted = self.estate()
        before = {p: b for p, b in self.snapshot(self.tmp).items()}
        rc, out, _ = run(corpus.cmd_corpus, ["backup"])
        self.assertEqual(rc, 0)
        self.assertIn("copied %d" % len(planted), out)
        date = time.strftime("%Y-%m-%d")
        rels = self.archived()
        self.assertEqual(len(rels), len(planted))
        self.assertTrue(all(r.startswith(date + os.sep) for r in rels))
        self.assertTrue(any(os.sep + "claude" + os.sep in r for r in rels))
        self.assertTrue(any("subagents" in r for r in rels))
        self.assertTrue(any("workflows" in r for r in rels))
        self.assertTrue(any(os.sep + "codex" + os.sep in r for r in rels))
        # copy-only law: every source byte-identical
        for p, body in before.items():
            if self.dest not in p:
                with open(p, "rb") as f:
                    self.assertEqual(f.read(), body, p)
        # archived bodies match, provenance mtime carried
        for r in corpus.scan():
            ent = corpus.load_manifest(self.dest)[r["p"]]
            dst = os.path.join(self.dest, ent["dest"])
            with open(r["p"], "rb") as a, open(dst, "rb") as b:
                self.assertEqual(a.read(), b.read())
            self.assertEqual(os.stat(dst).st_mtime_ns, r["mtns"])

    def test_rerun_is_noop(self):
        self.estate()
        run(corpus.cmd_corpus, ["backup"])
        arch = self.archived()
        rc, out, _ = run(corpus.cmd_corpus, ["backup"])
        self.assertEqual(rc, 0)
        self.assertIn("copied 0", out)
        self.assertEqual(self.archived(), arch)

    def test_grown_source_recopies_keeps_old_snapshot(self):
        planted = self.estate()
        old_day = time.time() - 3 * 86400
        rep = corpus.backup(self.dest, now=old_day)
        self.assertEqual(rep["copied"], len(planted))
        with open(planted[0], "a", encoding="utf-8") as f:
            f.write(FAKE)  # the append-mostly transcript grows
        rc, out, _ = run(corpus.cmd_corpus, ["backup"])
        self.assertEqual(rc, 0)
        self.assertIn("copied 1", out)
        old_date = time.strftime("%Y-%m-%d", time.localtime(old_day))
        kept = [r for r in self.archived() if r.startswith(old_date)]
        self.assertEqual(len(kept), len(planted))  # yesterday's snapshot stays
        ent = corpus.load_manifest(self.dest)[planted[0]]
        self.assertTrue(ent["dest"].startswith(time.strftime("%Y-%m-%d")))

    def test_mtime_only_churn_refreshes_without_second_body(self):
        planted = self.estate()
        corpus.backup(self.dest)
        arch = self.archived()
        os.utime(planted[0])  # touched, bytes identical
        rc, out, _ = run(corpus.cmd_corpus, ["backup"])
        self.assertEqual(rc, 0)
        self.assertIn("refreshed 1", out)
        self.assertEqual(self.archived(), arch)
        self.assertEqual(corpus.plan(corpus.scan(),
                                     corpus.load_manifest(self.dest))[0], [])

    @unittest.skipIf(os.geteuid() == 0, "chmod-based denial is a no-op as root")
    def test_unreadable_source_fails_open_and_retries(self):
        planted = self.estate()
        os.chmod(planted[0], 0)
        rc, out, _ = run(corpus.cmd_corpus, ["backup"])
        self.assertEqual(rc, 1)
        self.assertIn("ERR", out)
        self.assertIn("copied %d" % (len(planted) - 1), out)
        man = corpus.load_manifest(self.dest)
        self.assertNotIn(planted[0], man)  # stays pending -> retries next run
        os.chmod(planted[0], 0o600)
        rc, out, _ = run(corpus.cmd_corpus, ["backup"])
        self.assertEqual(rc, 0)
        self.assertIn("copied 1", out)

    def test_space_guard_aborts_before_first_byte(self):
        self.estate()
        orig = corpus._free_bytes
        corpus._free_bytes = lambda path: 10
        try:
            rc, out, _ = run(corpus.cmd_corpus, ["backup"])
        finally:
            corpus._free_bytes = orig
        self.assertEqual(rc, 1)
        self.assertIn("ABORTED before any copy", out)
        self.assertEqual(self.archived(), [])

    def test_dest_flag_beats_env(self):
        self.estate()
        other = os.path.join(self.tmp, "other-archive")
        rc, _, _ = run(corpus.cmd_corpus, ["backup", "--dest", other])
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self.dest))
        self.assertTrue(corpus.load_manifest(other))


class StatusTest(CorpusBase):
    def test_status_coverage_and_last_run(self):
        planted = self.estate()
        rc, out, _ = run(corpus.cmd_corpus, ["status"])
        self.assertEqual(rc, 0)
        self.assertIn("archived: 0 of %d" % len(planted), out)
        self.assertIn("last run: never", out)
        run(corpus.cmd_corpus, ["backup"])
        os.remove(planted[-1])  # a source retired upstream — archive keeps it
        rc, out, _ = run(corpus.cmd_corpus, ["status"])
        self.assertEqual(rc, 0)
        self.assertIn("archived: %d of %d (100.0%%)"
                      % (len(planted) - 1, len(planted) - 1), out)
        self.assertIn("1 retired sources kept in archive", out)
        self.assertIn("copied %d" % len(planted), out)

    def test_usage(self):
        rc, _, err = run(corpus.cmd_corpus, [])
        self.assertEqual(rc, 2)
        self.assertIn("usage", err)
        rc, _, err = run(corpus.cmd_corpus, ["backup", "extra"])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
