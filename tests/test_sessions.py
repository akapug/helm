#!/usr/bin/env python3
"""helm.sessions — the catalog-as-rows lens: project grouping, one-paste resume,
and the resume-warning surface. Hermetic: the catalog and the registry are both
stubbed, so no real transcript scan or ~/.helm read ever happens."""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import sessions  # noqa: E402


def _row(i, h="claude", cwd="/home/u/dev/proj", t="work session", **kw):
    r = {"h": h, "i": i, "cwd": cwd, "c": cwd, "t": t, "z": 4096, "m": 10,
         "u": "2026-07-19", "mt": 1_900_000_000, "syn": False, "xl": False}
    r.update(kw)
    return r


def _reg(*projects):
    """A registry whose projects carry cv_scope prefixes (the lens source)."""
    return {"projects": {p["name"]: p for p in projects}}


def _proj(name, path, prefixes=None, **kw):
    p = {"name": name, "path": path,
         "cv_scope": {"cwd_prefixes": prefixes or [path]}}
    p.update(kw)
    return p


class LensTest(unittest.TestCase):
    def _lens(self, reg):
        with mock.patch.object(sessions.registry, "load", return_value=reg):
            return sessions._project_lens()

    def test_lens_longest_prefix_first(self):
        lens = self._lens(_reg(
            _proj("outer", "/home/u/dev"),
            _proj("inner", "/home/u/dev/proj")))
        # nested repo's prefix is longer, so it sorts before its parent
        self.assertEqual(lens[0][0], "inner")
        self.assertGreater(len(lens[0][1]), len(lens[1][1]))

    def test_lens_skips_external_and_pathless(self):
        lens = self._lens(_reg(
            _proj("real", "/home/u/dev/proj"),
            _proj("ext", "/x", external=True),
            {"name": "nopath", "path": ""}))
        self.assertEqual([n for n, _ in lens], ["real"])

    def test_lens_expands_every_cv_scope_prefix(self):
        lens = self._lens(_reg(_proj(
            "proj", "/home/u/dev/proj",
            prefixes=["/home/u/dev/proj", "/home/u/dev/proj-wt-x"])))
        self.assertEqual({p for _, p in lens},
                         {"/home/u/dev/proj", "/home/u/dev/proj-wt-x"})

    def test_project_for_matches_root_and_children_not_siblings(self):
        lens = [("proj", "/home/u/dev/proj")]
        self.assertEqual(sessions._project_for("/home/u/dev/proj", lens), "proj")
        self.assertEqual(sessions._project_for("/home/u/dev/proj/sub", lens), "proj")
        self.assertIsNone(sessions._project_for("/home/u/dev/proj-other", lens))
        self.assertIsNone(sessions._project_for("/home/u/dev/elsewhere", lens))


class RowsForTest(unittest.TestCase):
    def _rows_for(self, rows, reg, **kw):
        cat = {"rows": rows}
        with mock.patch("helm.transcripts.get_catalog", return_value=cat), \
             mock.patch.object(sessions.registry, "load", return_value=reg):
            return sessions.rows_for(**kw)

    def test_annotates_project_and_filters_synthetic_by_default(self):
        rows = [_row("a1", cwd="/home/u/dev/proj"),
                _row("s1", cwd="/home/u/dev/proj", syn=True),
                _row("o1", cwd="/tmp/scratch")]
        reg = _reg(_proj("proj", "/home/u/dev/proj"))
        out = self._rows_for(rows, reg)
        ids = [r["i"] for r in out]
        self.assertEqual(ids, ["a1", "o1"])          # synthetic dropped
        self.assertEqual(out[0]["project"], "proj")
        self.assertIsNone(out[1]["project"])          # /tmp matches no project

    def test_include_synthetic_and_project_filter(self):
        rows = [_row("a1", cwd="/home/u/dev/proj"),
                _row("s1", cwd="/home/u/dev/proj", syn=True),
                _row("b1", cwd="/home/u/dev/other")]
        reg = _reg(_proj("proj", "/home/u/dev/proj"),
                   _proj("other", "/home/u/dev/other"))
        out = self._rows_for(rows, reg, include_synthetic=True, project="proj")
        self.assertEqual({r["i"] for r in out}, {"a1", "s1"})

    def test_limit_caps_output(self):
        rows = [_row("a%d" % n) for n in range(10)]
        reg = _reg(_proj("proj", "/home/u/dev/proj"))
        self.assertEqual(len(self._rows_for(rows, reg, limit=3)), 3)

    def test_does_not_mutate_cached_rows(self):
        row = _row("a1", cwd="/home/u/dev/proj")
        reg = _reg(_proj("proj", "/home/u/dev/proj"))
        self._rows_for([row], reg)
        self.assertNotIn("project", row)              # the cache row stays clean

    def test_c_fallback_when_cwd_absent(self):
        # a row carrying only `c` (never `cwd`) still resolves its project
        rows = [{"h": "codex", "i": "z9", "c": "/home/u/dev/proj", "t": "t",
                 "z": 1, "m": 1, "u": "2026-07-19", "mt": 0, "syn": False, "xl": False}]
        reg = _reg(_proj("proj", "/home/u/dev/proj"))
        self.assertEqual(self._rows_for(rows, reg)[0]["project"], "proj")


class ResumeCommandTest(unittest.TestCase):
    def test_claude_carries_cd_and_resume(self):
        cmd = sessions.resume_command(_row("uuid-1", h="claude", cwd="/home/u/dev/proj"))
        self.assertIn("claude --resume uuid-1", cmd)
        self.assertIn("/home/u/dev/proj", cmd)

    def test_codex_is_global_by_uuid(self):
        cmd = sessions.resume_command(_row("uuid-2", h="codex", cwd="/home/u/dev/proj"))
        self.assertIn("codex resume uuid-2", cmd)

    def test_missing_cwd_falls_back_to_dot(self):
        cmd = sessions.resume_command(_row("uuid-3", h="claude", cwd=""))
        self.assertIn("'.'", cmd)


class ResumeWarningsTest(unittest.TestCase):
    def test_clean_session_has_no_warnings(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(sessions.resume_warnings(_row("a", cwd=d)), [])

    def test_oversized_warns_and_outranks_synthetic(self):
        with tempfile.TemporaryDirectory() as d:
            w = sessions.resume_warnings(
                _row("a", cwd=d, xl=True, syn=True, z=210_000_000, m=3))
            self.assertEqual(len(w), 1)               # xl wins, syn suppressed
            self.assertIn("OVERSIZED", w[0])

    def test_synthetic_warns_when_not_oversized(self):
        with tempfile.TemporaryDirectory() as d:
            w = sessions.resume_warnings(_row("a", cwd=d, syn=True))
            self.assertTrue(any("REFERENCE" in x for x in w))

    def test_claude_missing_cwd_warns(self):
        w = sessions.resume_warnings(_row("a", h="claude", cwd=""))
        self.assertTrue(any("no recorded cwd" in x for x in w))

    def test_claude_nonexistent_cwd_warns(self):
        w = sessions.resume_warnings(_row("a", h="claude", cwd="/no/such/dir/xyz"))
        self.assertTrue(any("no longer exists" in x for x in w))

    def test_codex_absent_cwd_is_not_a_warning(self):
        # codex resume is global-by-UUID — a missing cwd never blocks it
        self.assertEqual(sessions.resume_warnings(_row("a", h="codex", cwd="")), [])


class CmdSessionsTest(unittest.TestCase):
    def _run(self, args, rows, reg):
        cat = {"rows": rows}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("helm.transcripts.get_catalog", return_value=cat), \
             mock.patch.object(sessions.registry, "load", return_value=reg), \
             redirect_stdout(out), redirect_stderr(err):
            rc = sessions.cmd_sessions(args)
        return rc, out.getvalue(), err.getvalue()

    def test_list_renders_rows(self):
        rows = [_row("aaaa1111", cwd="/home/u/dev/proj", t="fix the thing")]
        reg = _reg(_proj("proj", "/home/u/dev/proj"))
        rc, out, _ = self._run([], rows, reg)
        self.assertEqual(rc, 0)
        self.assertIn("aaaa1111", out)
        self.assertIn("proj", out)
        self.assertIn("fix the thing", out)

    def test_resume_prints_command_on_stdout_warnings_on_stderr(self):
        with tempfile.TemporaryDirectory() as d:
            rows = [_row("bbbb2222", cwd=d, xl=True, z=210_000_000, m=2)]
            reg = _reg(_proj("proj", d))
            rc, out, err = self._run(["resume", "bbbb2222"], rows, reg)
        self.assertEqual(rc, 0)
        self.assertIn("claude --resume bbbb2222", out)   # command: pure stdout
        self.assertNotIn("OVERSIZED", out)               # warning never on stdout
        self.assertIn("OVERSIZED", err)                  # warning: stderr

    def test_resume_ambiguous_prefix_disambiguates(self):
        rows = [_row("dup00001"), _row("dup00002")]
        reg = _reg(_proj("proj", "/home/u/dev/proj"))
        rc, out, _ = self._run(["resume", "dup"], rows, reg)
        self.assertEqual(rc, 1)
        self.assertIn("disambiguate", out)

    def test_resume_unknown_prefix_reports_none(self):
        rows = [_row("aaaa1111")]
        reg = _reg(_proj("proj", "/home/u/dev/proj"))
        rc, out, _ = self._run(["resume", "zzzz"], rows, reg)
        self.assertEqual(rc, 1)
        self.assertIn("no session id starts with", out)

    def test_resume_without_prefix_is_usage_error(self):
        rc, out, _ = self._run(["resume"], [], _reg())
        self.assertEqual(rc, 2)
        self.assertIn("usage", out)


if __name__ == "__main__":
    unittest.main()
