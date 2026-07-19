"""Hermetic tests for helm.transcripts — catalog roots, caches, overrides and
the claude projects dir all point at tmp dirs; cv is never invoked (the scanner
catalog path is forced and cv-touching seams are stubbed). Real stores untouched."""
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from helm import catalog, transcripts

SID_A = "aaaaaaaa-1111-2222-3333-444444444444"   # claude, project alpha
SID_B = "bbbbbbbb-1111-2222-3333-444444444444"   # claude, project beta
SID_SYN = "cccccccc-1111-2222-3333-444444444444"  # synthetic summarizer, alpha
SID_CX = "dddddddd-1111-2222-3333-444444444444"   # codex


class TranscriptsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-transcripts-")
        j = lambda *p: os.path.join(self.tmp, *p)
        self._cat = {k: getattr(catalog, k) for k in
                     ("CLAUDE_ROOTS", "CODEX_ROOTS", "CACHE_DIR", "CACHE", "SYN_CACHE")}
        self._tr = {k: getattr(transcripts, k) for k in
                    ("OVERRIDES_PATH", "MINTS_PATH", "CLAUDE_PROJECTS")}
        catalog.CLAUDE_ROOTS = [j("claude-root")]
        catalog.CODEX_ROOTS = [j("codex-root")]
        catalog.CACHE_DIR = j("cache")
        catalog.CACHE = j("cache", "catalog-cache.json")
        catalog.SYN_CACHE = j("cache", "syn-cache.json")
        transcripts.OVERRIDES_PATH = j("cache", "cwd-overrides.json")
        transcripts.MINTS_PATH = j("cache", "mints.jsonl")
        transcripts.CLAUDE_PROJECTS = j("claude-projects")
        self._env = os.environ.get("HELM_CATALOG")
        os.environ["HELM_CATALOG"] = "scanner"  # never shell out to cv for the catalog
        transcripts._state.clear()
        transcripts._cwd_overrides = {}
        self.alpha = j("work", "alpha")
        self.beta = j("work", "beta")
        os.makedirs(self.alpha)
        os.makedirs(self.beta)

    def tearDown(self):
        for k, v in self._cat.items():
            setattr(catalog, k, v)
        for k, v in self._tr.items():
            setattr(transcripts, k, v)
        if self._env is None:
            os.environ.pop("HELM_CATALOG", None)
        else:
            os.environ["HELM_CATALOG"] = self._env
        transcripts._state.clear()
        transcripts._cwd_overrides = {}
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- fixtures ----------------------------------------------------------
    def _plant_claude(self, sid, cwd, title, body_lines=(), pad=True):
        d = os.path.join(catalog.CLAUDE_ROOTS[0], "slug-" + sid[:8])
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, sid + ".jsonl")
        lines = [{"cwd": cwd, "gitBranch": "main", "timestamp": "2026-07-01T10:00:00Z",
                  "message": {"role": "user", "content": title}}]
        lines += [{"message": {"role": "assistant", "content": t}} for t in body_lines]
        if pad:  # the scanner ignores files under 200 bytes
            lines.append({"message": {"role": "assistant", "content": "x" * 300}})
        with open(path, "w") as f:
            for l in lines:
                f.write(json.dumps(l) + "\n")
        return path

    def _plant_codex(self, sid, cwd):
        d = os.path.join(catalog.CODEX_ROOTS[0], "2026")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"rollout-2026-07-01T10-00-00-{sid}.jsonl")
        with open(path, "w") as f:
            f.write(json.dumps({"timestamp": "2026-07-01T10:00:00Z", "cwd": cwd,
                                "text": "a codex session doing codex things"}) + "\n")
            f.write(json.dumps({"text": "y" * 300}) + "\n")
        return path

    def _fresh_catalog(self):
        return transcripts.get_catalog(refresh=True)

    # -- deep_search (scoped grep — the cv-free path) ----------------------
    def test_scoped_search_finds_work_and_hides_synthetic(self):
        self._plant_claude(SID_A, self.alpha, "fix the flux capacitor wiring")
        self._plant_claude(SID_B, self.beta, "unrelated beta work")
        self._plant_claude(SID_SYN, self.alpha,
                           "You are summarizing a Claude Code session about the flux capacitor")
        self._fresh_catalog()
        res = transcripts.deep_search("flux capacitor", scope="alpha")
        self.assertNotIn("error", res)
        self.assertEqual([h["sid"] for h in res["hits"]], [SID_A])
        self.assertTrue(res["source"].startswith("scoped-grep"))
        self.assertIn("flux capacitor", res["hits"][0]["snippet"])
        # --refs / include_synthetic surfaces the reference transcript too
        res = transcripts.deep_search("flux capacitor", scope="alpha", include_synthetic=True)
        self.assertEqual(sorted(h["sid"] for h in res["hits"]), sorted([SID_A, SID_SYN]))
        syn = next(h for h in res["hits"] if h["sid"] == SID_SYN)
        self.assertTrue(syn["syn"])
        # scope that matches nothing
        self.assertEqual(transcripts.deep_search("flux", scope="nosuchproj")["hits"], [])

    # -- argument-injection guards (audit: grep/cv argv without `--`) ------
    def test_scoped_search_dash_query_is_literal_not_a_flag(self):
        # a query beginning with '-' must reach grep as a PATTERN (after `--`),
        # never as a flag; pre-fix grep errored on "unrecognized option" and the
        # search silently returned nothing (or worse: -f/path read a file)
        self._plant_claude(SID_A, self.alpha, "note the --evil-flag marker here")
        self._fresh_catalog()
        res = transcripts.deep_search("--evil-flag", scope="alpha")
        self.assertNotIn("error", res)
        self.assertEqual([h["sid"] for h in res["hits"]], [SID_A])
        self.assertIn("--evil-flag", res["hits"][0]["snippet"])

    def test_cv_search_query_rides_after_double_dash(self):
        done = mock.Mock(returncode=0, stdout="[]", stderr="")
        with mock.patch("subprocess.run", return_value=done) as run:
            transcripts.deep_search("--limit")
        argv = run.call_args[0][0]
        self.assertEqual(argv[:2], ["cv", "search"])
        self.assertIn("--", argv)
        # the user query is positional AFTER the `--` terminator
        self.assertEqual(argv[argv.index("--") + 1], "--limit")

    def test_cv_show_rejects_flag_shaped_sid_and_harness(self):
        with self.assertRaises(transcripts.ProviderError):
            transcripts._cv_show("--rm-everything")
        with self.assertRaises(transcripts.ProviderError):
            transcripts._cv_show("okayid-123456", harness="--json")
        # a flag-shaped sid via get_session surfaces as an error dict, not argv
        self._fresh_catalog()
        res = transcripts.get_session("--harness=evil")
        self.assertIn("error", res)
        # a legit sid builds argv with the sid after `--`
        done = mock.Mock(returncode=0, stdout='{"messages": []}', stderr="")
        with mock.patch("subprocess.run", return_value=done) as run:
            transcripts._cv_show("deadbeef-1234", rng="0-1", harness="hermes")
        argv = run.call_args[0][0]
        self.assertEqual(argv, ["cv", "show", "--json", "--range", "0-1",
                                "--harness", "hermes", "--", "deadbeef-1234"])

    # -- _resolve_sid ------------------------------------------------------
    def test_resolve_sid_prefix_rules(self):
        self._plant_claude(SID_A, self.alpha, "alpha work")
        self._plant_claude(SID_B, self.beta, "beta work")
        self._fresh_catalog()
        row, err = transcripts._resolve_sid(SID_A[:12])
        self.assertIsNone(err)
        self.assertEqual(row["i"], SID_A)
        _, err = transcripts._resolve_sid("aaaa")           # too short
        self.assertIn("6+ chars", err["error"])
        _, err = transcripts._resolve_sid("eeeeeee-none")   # unknown
        self.assertIn("unknown session", err["error"])
        self._plant_claude("aaaaaaaa-9999-2222-3333-444444444444", self.beta, "twin")
        self._fresh_catalog()
        _, err = transcripts._resolve_sid("aaaaaaaa")       # now ambiguous
        self.assertIn("ambiguous", err["error"])

    # -- get_session: windowing + centered find (cv stubbed) ---------------
    def _fake_ir(self, n=10, target_at=3):
        msgs = []
        for i in range(n):
            text = ("here is the target payload" if i == target_at
                    else "filler item %d" % i)
            msgs.append({"role": "user" if i % 2 == 0 else "assistant",
                         "timestamp": "2026-07-01T10:0%d:00Z" % (i % 10),
                         "content": [{"kind": "text", "text": text}]})
        def fake_cv_show(sid, rng=None, harness=None):
            a, b = (int(x) for x in rng.split("-"))
            return {"messages": msgs[a:b], "title": "stub title", "cwd": self.alpha}
        return msgs, fake_cv_show

    def test_get_session_tail_window(self):
        self._plant_claude(SID_A, self.alpha, "alpha work",
                           body_lines=["l%d" % i for i in range(8)], pad=False)
        self._fresh_catalog()
        _, fake = self._fake_ir(n=9)  # file has 9 lines -> line_bound 9
        with mock.patch.object(transcripts, "_cv_show", fake):
            res = transcripts.get_session(SID_A, limit=4)
        self.assertNotIn("error", res)
        self.assertEqual((res["total"], res["start"], res["end"]), (9, 5, 9))
        self.assertEqual(len(res["messages"]), 4)
        self.assertTrue(res["session"]["resumable"])
        self.assertEqual(res["session"]["harness"], "claude")

    def test_get_session_find_centers_on_match(self):
        self._plant_claude(SID_A, self.alpha, "alpha work",
                           body_lines=["l%d" % i for i in range(8)], pad=False)
        self._fresh_catalog()
        _, fake = self._fake_ir(n=9, target_at=3)
        with mock.patch.object(transcripts, "_cv_show", fake):
            res = transcripts.get_session(SID_A, limit=4, find="target")
        self.assertNotIn("error", res)
        self.assertEqual((res["start"], res["end"]), (1, 5))
        self.assertEqual(res["match"], {"index": 2})  # abs 3 inside window starting at 1
        self.assertIn("target", res["messages"][2]["items"][0]["t"])
        # no match: tail window + note
        with mock.patch.object(transcripts, "_cv_show", fake):
            res = transcripts.get_session(SID_A, limit=4, find="zzz-absent")
        self.assertIsNone(res["match"])
        self.assertIn("no match", res["note"])

    # -- rehome: override + claude slug symlink ----------------------------
    def test_rehome_claude_symlinks_and_overrides(self):
        real = self._plant_claude(SID_A, self.alpha, "alpha work")
        self._fresh_catalog()
        new_cwd = os.path.join(self.tmp, "newhome")
        os.makedirs(new_cwd)
        res = transcripts.cwd_override({"sid": SID_A[:12], "cwd": new_cwd})
        self.assertNotIn("error", res)
        self.assertTrue(res["ok"] and res["linked"])
        slug = new_cwd.replace("/", "-").replace(".", "-")
        target = os.path.join(transcripts.CLAUDE_PROJECTS, slug, SID_A + ".jsonl")
        self.assertTrue(os.path.islink(target))
        self.assertEqual(os.path.realpath(target), os.path.realpath(real))
        self.assertEqual(json.load(open(transcripts.OVERRIDES_PATH)), {SID_A: new_cwd})
        # applied at read time
        row = next(r for r in transcripts.get_catalog()["rows"] if r["i"] == SID_A)
        self.assertEqual(row["cwd"], new_cwd)
        self.assertTrue(row["cwdOverride"])
        # idempotent re-point
        self.assertTrue(transcripts.cwd_override({"sid": SID_A, "cwd": new_cwd})["ok"])
        # reset removes the override, leaves the symlink (harmless)
        res = transcripts.cwd_override({"sid": SID_A, "cwd": None})
        self.assertTrue(res["ok"])
        self.assertEqual(json.load(open(transcripts.OVERRIDES_PATH)), {})
        self.assertTrue(os.path.islink(target))

    def test_rehome_refuses_clobber_and_bad_cwd(self):
        self._plant_claude(SID_A, self.alpha, "alpha work")
        self._fresh_catalog()
        self.assertIn("error", transcripts.cwd_override({"sid": SID_A, "cwd": "rel/path"}))
        self.assertIn("error", transcripts.cwd_override(
            {"sid": SID_A, "cwd": os.path.join(self.tmp, "does-not-exist")}))
        new_cwd = os.path.join(self.tmp, "clobber")
        os.makedirs(new_cwd)
        slug = new_cwd.replace("/", "-").replace(".", "-")
        proj = os.path.join(transcripts.CLAUDE_PROJECTS, slug)
        os.makedirs(proj)
        with open(os.path.join(proj, SID_A + ".jsonl"), "w") as f:
            f.write("real file, not a link\n")
        res = transcripts.cwd_override({"sid": SID_A, "cwd": new_cwd})
        self.assertIn("refusing to clobber", res["error"])

    def test_rehome_codex_no_symlink(self):
        self._plant_codex(SID_CX, self.beta)
        self._fresh_catalog()
        new_cwd = os.path.join(self.tmp, "cxhome")
        os.makedirs(new_cwd)
        res = transcripts.cwd_override({"sid": SID_CX, "cwd": new_cwd})
        self.assertTrue(res["ok"])
        self.assertFalse(res["linked"])
        self.assertIn("global-by-UUID", res["note"])
        self.assertFalse(os.path.isdir(transcripts.CLAUDE_PROJECTS))  # nothing linked

    # -- prune studio ------------------------------------------------------
    def test_prune_dry_run_and_guards(self):
        self._plant_claude(SID_A, self.alpha, "alpha work")
        self._plant_codex(SID_CX, self.beta)
        self._fresh_catalog()
        with mock.patch.object(transcripts, "_cv_prune_help", lambda: "--thinking --window"):
            res = transcripts.prune_session(SID_A[:12], preset="lean", dry=True)
            self.assertEqual(res["willRun"], f"cv prune {SID_A} --thinking")
            self.assertEqual(res["sid"], SID_A)
            self.assertIn("beforeBytes", res["estimate"])
            # claude-only
            self.assertIn("claude sessions only",
                          transcripts.prune_session(SID_CX, dry=True)["error"])
            # unknown preset
            self.assertIn("unknown preset",
                          transcripts.prune_session(SID_A, preset="bogus", dry=True)["error"])
            # window preset clamps tokens
            res = transcripts.prune_session(SID_A, preset="window", tokens=999999, dry=True)
            self.assertIn("--window 180000", res["willRun"])
        # feature-detect: preset flag missing from installed cv
        with mock.patch.object(transcripts, "_cv_prune_help", lambda: ""):
            res = transcripts.prune_session(SID_A, preset="lean", dry=True)
            self.assertIn("--thinking", res["error"])

    def test_prune_executes_and_reports_new_sid(self):
        self._plant_claude(SID_A, self.alpha, "alpha work")
        self._fresh_catalog()
        new_sid = "eeeeeeee-2222-3333-4444-555555555555"
        report = ("✦ snipped 12 payloads\n"
                  f"new session: /tmp/nowhere/{new_sid}.jsonl\n"
                  f"resume with: claude --resume {new_sid}\n")
        done = mock.Mock(returncode=0, stdout="", stderr=report)
        with mock.patch.object(transcripts, "_cv_prune_help", lambda: "--thinking"), \
             mock.patch("subprocess.run", return_value=done) as run:
            res = transcripts.prune_session(SID_A, preset="lean")
        self.assertTrue(res["ok"])
        self.assertEqual(res["newSid"], new_sid)
        self.assertEqual(run.call_args[0][0], ["cv", "prune", SID_A, "--thinking"])
        self.assertIn("snipped 12 payloads", res["note"])
        # original untouched
        row, _ = transcripts._resolve_sid(SID_A)
        self.assertTrue(os.path.exists(row["p"]))

    # -- CLI legs ----------------------------------------------------------
    def test_cmd_search_and_rehome_smoke(self):
        self._plant_claude(SID_A, self.alpha, "fix the flux capacitor wiring")
        self._fresh_catalog()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = transcripts.cmd_search(["flux", "capacitor", "--scope", "alpha"])
        self.assertEqual(rc, 0)
        self.assertTrue(out.getvalue().startswith("helm search: 1 hit"))
        self.assertIn(SID_A[:8], out.getvalue())
        new_cwd = os.path.join(self.tmp, "newhome")
        os.makedirs(new_cwd)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = transcripts.cmd_rehome([SID_A[:12], new_cwd])
        self.assertEqual(rc, 0)
        self.assertTrue(out.getvalue().startswith("helm rehome:"))
        self.assertIn(new_cwd, out.getvalue())

    def test_cmd_transcript_smoke(self):
        self._plant_claude(SID_A, self.alpha, "alpha work",
                           body_lines=["l%d" % i for i in range(8)], pad=False)
        self._fresh_catalog()
        _, fake = self._fake_ir(n=9, target_at=3)
        out = io.StringIO()
        with mock.patch.object(transcripts, "_cv_show", fake), \
             contextlib.redirect_stdout(out):
            rc = transcripts.cmd_transcript([SID_A[:12], "--find", "target", "--limit", "4"])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertTrue(text.startswith("helm transcript:"))
        self.assertIn("»[", text)              # the centered match is marked
        self.assertIn("target payload", text)

    def test_cmd_prune_dry_smoke(self):
        self._plant_claude(SID_A, self.alpha, "alpha work")
        self._fresh_catalog()
        out = io.StringIO()
        with mock.patch.object(transcripts, "_cv_prune_help", lambda: "--thinking"), \
             contextlib.redirect_stdout(out):
            rc = transcripts.cmd_prune([SID_A[:12], "--dry"])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertTrue(text.startswith("helm prune:"))
        self.assertIn("will run: cv prune", text)


if __name__ == "__main__":
    unittest.main()
