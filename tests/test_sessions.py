#!/usr/bin/env python3
"""helm.sessions — the catalog-as-rows lens: project grouping, one-paste resume,
and the resume-warning surface. Hermetic: the catalog and the registry are both
stubbed, so no real transcript scan or ~/.helm read ever happens."""
import io
import os
import shlex
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

    def test_paste_line_strips_child_stamp(self):
        """child-stamp-kills-seat-persistence: a resume line pasted into a
        stamped shell (CLAUDE_CODE_CHILD_SESSION + inherited SID/bridge id
        from a daemon born inside a Claude session) must not resume the
        session as a subprocess child — transcript persistence would be
        silently OFF. The line unsets the trio before the harness binary."""
        from helm import seat
        for cmd in (sessions.resume_command(_row("u1", h="claude", cwd="/p")),
                    sessions.resume_command(_row("u2", h="codex", cwd="/p"))):
            for v in seat.CHILD_STAMP_VARS:
                self.assertIn("-u " + v, cmd)
                self.assertNotIn(v + "=", cmd)  # unset, never re-exported
            # the unset rides the executed half, after the cd, before claude
            self.assertLess(cmd.index("&&"), cmd.index("-u CLAUDE_CODE_CHILD_SESSION"))
            self.assertLess(cmd.index("-u CLAUDE_CODE_CHILD_SESSION"),
                            cmd.index("resume"))


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


class CredHomeTest(unittest.TestCase):
    """sid -> owning credential home. Hermetic: a fake ~/.claude-homes tree with
    the same shape as the real one (short-name symlink beside each real home,
    projects/ symlinked into the shared dir) so the aliasing and the
    path-says-nothing property are both exercised, never assumed."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.shared = os.path.join(self.tmp, ".claude", "projects")
        os.makedirs(self.shared)
        self.homes = os.path.join(self.tmp, ".claude-homes")
        for real, alias in (("acct-one-com", "one"), ("acct-two-com", "two")):
            h = os.path.join(self.homes, real)
            os.makedirs(os.path.join(h, "session-env"))
            os.makedirs(os.path.join(h, "sessions"))
            os.symlink(self.shared, os.path.join(h, "projects"))
            os.symlink(h, os.path.join(self.homes, alias))
        self.bindings = os.path.join(self.tmp, "bindings.tsv")
        self.p = [
            mock.patch.object(sessions, "BINDINGS", self.bindings),
            mock.patch.object(sessions, "DEFAULT_HOME",
                              os.path.join(self.tmp, ".claude")),
            mock.patch.object(sessions, "cred_homes", lambda: [
                os.path.realpath(os.path.join(self.tmp, ".claude")),
                os.path.join(self.homes, "acct-one-com"),
                os.path.join(self.homes, "acct-two-com")]),
        ]
        for p in self.p:
            p.start()
        os.makedirs(os.path.join(self.tmp, ".claude", "session-env"), exist_ok=True)

    def tearDown(self):
        for p in self.p:
            p.stop()

    def _env(self, home, sid):
        open(os.path.join(self.homes, home, "session-env", sid), "w").close()

    def test_resolves_the_home_that_ran_it(self):
        self._env("acct-two-com", "sid-a")
        self.assertTrue(sessions.credhome_for("sid-a").endswith("acct-two-com"))

    def test_unknown_session_is_none_not_a_guess(self):
        # a wrong home silently resumes on the wrong account, so None (which
        # makes the caller say UNPINNED) is the only safe answer
        self.assertIsNone(sessions.credhome_for("never-ran"))

    def test_alias_symlink_does_not_double_count_an_account(self):
        # cred_homes must dereference: 2 real homes behind 4 names
        with mock.patch.object(sessions, "cred_homes",
                               sessions.__dict__["cred_homes"]):
            with mock.patch.object(os.path, "expanduser",
                                   lambda p: p.replace("~", self.tmp)):
                got = sessions.cred_homes()
        reals = [g for g in got if ".claude-homes" in g]
        self.assertEqual(sorted(os.path.basename(g) for g in reals),
                         ["acct-one-com", "acct-two-com"])

    def test_named_home_beats_the_default_catch_all(self):
        # ~/.claude accumulates entries for sessions owned by a named home;
        # preferring it would mis-attribute them to the default account
        self._env("acct-one-com", "sid-b")
        open(os.path.join(self.tmp, ".claude", "session-env", "sid-b"), "w").close()
        self.assertTrue(sessions.credhome_for("sid-b").endswith("acct-one-com"))

    def test_lookup_latches_so_the_answer_survives_pruning(self):
        self._env("acct-one-com", "sid-c")
        self.assertTrue(sessions.credhome_for("sid-c").endswith("acct-one-com"))
        os.remove(os.path.join(self.homes, "acct-one-com", "session-env", "sid-c"))
        # session-env is gone (claude prunes it); the frozen binding still answers
        self.assertTrue(sessions.credhome_for("sid-c").endswith("acct-one-com"))

    def test_latch_live_uses_the_authoritative_pid_record(self):
        import json
        rec = {"pid": 4242, "sessionId": "sid-live"}
        with open(os.path.join(self.homes, "acct-two-com", "sessions", "4242.json"), "w") as f:
            json.dump(rec, f)
        self.assertEqual(sessions.latch_live(), 1)
        self.assertTrue(sessions.credhome_for("sid-live").endswith("acct-two-com"))
        self.assertEqual(sessions.latch_live(), 0)   # idempotent, no duplicate rows

    def test_binding_write_failure_never_breaks_the_read(self):
        self._env("acct-one-com", "sid-d")
        with mock.patch.object(sessions, "BINDINGS", "/proc/nope/cannot-write.tsv"):
            self.assertTrue(sessions.credhome_for("sid-d").endswith("acct-one-com"))


class ResumePinTest(unittest.TestCase):
    """The resume line must PIN the account. Without the pin it does not fail on
    the wrong cred — it silently succeeds on it, because the shared projects/
    symlink resolves the transcript from any home."""

    def test_command_pins_the_owning_home(self):
        with mock.patch.object(sessions, "credhome_for", return_value="/h/acct"):
            cmd = sessions.resume_command(_row("uuid-9", h="claude", cwd="/p"))
        self.assertIn("CLAUDE_CONFIG_DIR=/h/acct", cmd)
        self.assertIn("--resume uuid-9", cmd)

    def test_unresolved_home_leaves_the_line_unpinned_not_wrong(self):
        with mock.patch.object(sessions, "credhome_for", return_value=None):
            cmd = sessions.resume_command(_row("uuid-8", h="claude", cwd="/p"))
        self.assertNotIn("CLAUDE_CONFIG_DIR", cmd)

    def test_codex_rows_are_untouched(self):
        with mock.patch.object(sessions, "credhome_for", return_value="/h/acct"):
            cmd = sessions.resume_command(_row("uuid-7", h="codex", cwd="/p"))
        self.assertNotIn("CLAUDE_CONFIG_DIR", cmd)
        self.assertIn("codex resume uuid-7", cmd)

    def test_minted_script_hides_the_line_behind_a_path(self):
        # TOKEN LAW: a pane command is visible in adapter listings and logs
        d = tempfile.mkdtemp()
        with mock.patch.object(sessions, "RESUME_DIR", d), \
             mock.patch.object(sessions, "credhome_for", return_value="/h/acct"):
            p = sessions.mint_resume_script(_row("uuid-6", h="claude", cwd="/p"))
        self.assertTrue(os.access(p, os.X_OK))
        self.assertIn("CLAUDE_CONFIG_DIR=/h/acct", open(p).read())


class MintedScriptTest(unittest.TestCase):
    """The minted script is the whole delivery. It is not exercised by any unit
    that only inspects strings, so these tests assert the SHELL SEMANTICS —
    every bug found here was found by running it, not by reading it."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = [mock.patch.object(sessions, "RESUME_DIR", self.d),
                  mock.patch.object(sessions, "credhome_for", return_value="/h/acct")]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()

    def test_exec_never_binds_to_the_cd(self):
        # REGRESSION: the script was once `exec cd X && claude …`. exec binds to
        # cd — a shell BUILTIN — so the exec fails, the && chain never runs, the
        # pane dies on arrival, and spawn still returns a handle. A resume that
        # reports success and delivers nothing.
        body = open(sessions.mint_resume_script(
            _row("uuid-5", h="claude", cwd="/tmp"))).read()
        self.assertNotIn("exec cd", body)
        execs = [l for l in body.splitlines() if l.startswith("exec ")]
        self.assertEqual(len(execs), 1)
        self.assertTrue(execs[0].startswith("exec env "))

    def test_script_is_valid_posix_sh(self):
        import subprocess
        p = sessions.mint_resume_script(_row("uuid-4", h="claude", cwd="/tmp"))
        r = subprocess.run(["sh", "-n", p], capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr.decode())

    def test_a_vanished_cwd_fails_loud_instead_of_forking(self):
        # claude resume is cwd-scoped: from the wrong directory it does not
        # error, it starts a FRESH session. Silently falling back to $PWD would
        # look like a resume and lose the history, so the cd must be fatal.
        import subprocess
        p = sessions.mint_resume_script(
            _row("uuid-3", h="claude", cwd="/definitely/not/here"))
        r = subprocess.run(["sh", p], capture_output=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn(b"cwd is gone", r.stderr)

    def test_cwd_with_spaces_survives_quoting(self):
        import subprocess
        d = os.path.join(self.d, "a dir; echo pwned")
        os.makedirs(d)
        p = sessions.mint_resume_script(_row("uuid-2", h="claude", cwd=d))
        self.assertEqual(subprocess.run(["sh", "-n", p], capture_output=True).returncode, 0)
        self.assertIn(shlex.quote(d), open(p).read())


class PreflightTest(unittest.TestCase):
    """The two ways a resume reports success and delivers nothing. Both were
    found by RUNNING it — neither is visible to a test that only reads strings."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _home(self, name, projects):
        import json
        h = os.path.join(self.tmp, name)
        os.makedirs(h)
        with open(os.path.join(h, ".claude.json"), "w") as f:
            json.dump({"projects": projects}, f)
        return h

    def test_default_home_is_never_pinned(self):
        # pinning CLAUDE_CONFIG_DIR=~/.claude sends claude looking for the
        # onboarding marker at ~/.claude/.claude.json (a stub) instead of
        # ~/.claude.json, and it opens the FIRST-RUN WIZARD instead of the session
        with mock.patch.object(sessions, "DEFAULT_HOME", os.path.join(self.tmp, "d")):
            os.makedirs(os.path.join(self.tmp, "d"))
            self.assertFalse(sessions.is_pinnable(os.path.join(self.tmp, "d")))
            self.assertTrue(sessions.is_pinnable(os.path.join(self.tmp, "named")))
        self.assertFalse(sessions.is_pinnable(None))

    def test_untrusted_cwd_is_detected_before_spawning(self):
        h = self._home("acct", {"/w": {"hasTrustDialogAccepted": False}})
        self.assertEqual(sessions.trust_blocked(_row("s", cwd="/w"), h), h)

    def test_trusted_cwd_is_not_blocked(self):
        h = self._home("acct", {"/w": {"hasTrustDialogAccepted": True}})
        self.assertIsNone(sessions.trust_blocked(_row("s", cwd="/w"), h))

    def test_unknown_cwd_is_not_blocked(self):
        # a directory claude has never seen prompts on FIRST use, but we have no
        # recorded evidence either way — refusing on absence would block every
        # genuinely-new resume, so absence is not treated as a denial
        h = self._home("acct", {})
        self.assertIsNone(sessions.trust_blocked(_row("s", cwd="/w"), h))

    def test_skip_permissions_reaches_the_script(self):
        with mock.patch.object(sessions, "credhome_for", return_value="/h/a"):
            plain = sessions.resume_exec(_row("s", cwd="/w"))
            skip = sessions.resume_exec(_row("s", cwd="/w"), skip_permissions=True)
        self.assertNotIn("--dangerously-skip-permissions", plain)
        self.assertIn("--dangerously-skip-permissions", skip)
