#!/usr/bin/env python3
"""who tests — the pid→cred attribution table. Hermetic: /proc is a tmp
fixture tree (who.PROC patched), accounts are canned; no real process table,
home, or provider is ever touched."""
import contextlib
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from helm import who

SID = "aaaaaaaa-1111-2222-3333-444444444444"
SID2 = "bbbbbbbb-1111-2222-3333-444444444444"


class WhoBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-who-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.proc = os.path.join(self.tmp, "proc")
        os.makedirs(self.proc)
        patcher = mock.patch.object(who, "PROC", self.proc)
        patcher.start()
        self.addCleanup(patcher.stop)

    def plant_proc(self, pid, comm, ppid=1, start=1000, env=b"", cwd=None, fds=()):
        d = os.path.join(self.proc, str(pid))
        os.makedirs(d)
        with open(os.path.join(d, "comm"), "w") as f:
            f.write(comm + "\n")
        with open(os.path.join(d, "environ"), "wb") as f:
            f.write(env)
        with open(os.path.join(d, "stat"), "w") as f:
            f.write("%d (%s) S %d %s %d 0" % (pid, comm, ppid, "0 " * 17, start))
        if cwd:
            os.symlink(cwd, os.path.join(d, "cwd"))
        for i, target in enumerate(fds):
            os.makedirs(os.path.join(d, "fd"), exist_ok=True)
            os.symlink(target, os.path.join(d, "fd", str(3 + i)))
        return d

    def claude_home(self, name, cwd, sids=(), fresh=True):
        slug = cwd.replace("/", "-").replace(".", "-")
        home = os.path.join(self.tmp, name)
        d = os.path.join(home, "projects", slug)
        os.makedirs(d)
        for i, sid in enumerate(sids):
            p = os.path.join(d, sid + ".jsonl")
            with open(p, "w") as f:
                f.write("{}\n")
            if not fresh:
                old = time.time() - 3600
                os.utime(p, (old, old))
            elif i:  # distinct mtimes so newest-first ordering is stable
                t = time.time() - i
                os.utime(p, (t, t))
        return home


class StatTest(WhoBase):
    def test_ppid_and_starttime_parse_after_last_paren(self):
        d = os.path.join(self.proc, "42")
        os.makedirs(d)
        with open(os.path.join(d, "stat"), "w") as f:  # comm with spaces + parens
            f.write("42 (we ird) name) S 7 %s 5555 0" % ("0 " * 17))
        self.assertEqual(who.ppid_of(42), 7)
        self.assertEqual(who.starttime_of(42), 5555)

    def test_unreadable_stat_defaults(self):
        self.assertEqual(who.ppid_of(999), 0)
        self.assertEqual(who.starttime_of(999), float("inf"))

    def test_read_environ_splits_read_ok_from_unreadable(self):
        self.plant_proc(50, "claude", env=b"A=1\0CLAUDE_CONFIG_DIR=/x/home\0B=2\0")
        env = who.read_environ(50)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/x/home")
        self.assertNotIn("MISSING", env)          # read OK, key absent -> dict
        self.assertIsNone(who.read_environ(999))  # UNREADABLE -> None, never {}


class SessionAttributionTest(WhoBase):
    def test_codex_fd_scan_yields_exact_uuid(self):
        self.plant_proc(60, "codex",
                        fds=("/store/rollout-2026-07-01T18-12-51-%s.jsonl" % SID,
                             "/dev/null"))
        self.assertEqual(who.codex_session_from_fds(60), SID)

    def test_claude_single_live_candidate_is_exact(self):
        home = self.claude_home("h1", "/work/alpha", [SID])
        exact, cands = who.claude_sessions(home, "/work/alpha")
        self.assertEqual(exact, SID)
        self.assertEqual(cands, [SID])

    def test_claude_multiple_live_candidates_not_exact(self):
        home = self.claude_home("h2", "/work/alpha", [SID, SID2])
        exact, cands = who.claude_sessions(home, "/work/alpha")
        self.assertIsNone(exact)
        self.assertEqual(cands, [SID, SID2])  # newest first

    def test_claude_stale_candidates_listed_but_never_exact(self):
        home = self.claude_home("h3", "/work/alpha", [SID], fresh=False)
        exact, cands = who.claude_sessions(home, "/work/alpha")
        self.assertIsNone(exact)
        self.assertEqual(cands, [SID])

    def test_claude_missing_project_dir(self):
        self.assertEqual(who.claude_sessions("/no/home", "/work/x"), (None, []))


class ScanTest(WhoBase):
    def test_scan_attributes_home_account_child_and_default(self):
        cwd = os.path.join(self.tmp, "work")
        os.makedirs(cwd)
        home = self.claude_home("seat-home", cwd, [SID])
        self.plant_proc(100, "claude", ppid=1, start=10,
                        env=b"CLAUDE_CONFIG_DIR=" + home.encode() + b"\0", cwd=cwd)
        # a subagent spawned THROUGH an intermediate shell: 300 -> 250 -> 100
        self.plant_proc(250, "node", ppid=100)
        self.plant_proc(300, "claude", ppid=250, start=20,
                        env=b"CLAUDE_CONFIG_DIR=" + home.encode() + b"\0", cwd=cwd)
        self.plant_proc(400, "codex", ppid=1, start=30)  # no env -> default home
        self.plant_proc(500, "vim", ppid=1)              # never an agent row
        accounts = [{"name": "seat@x.com", "provider": "anthropic", "home": home}]
        rows = {r["pid"]: r for r in who.scan(accounts)}
        self.assertEqual(set(rows), {100, 300, 400})
        top = rows[100]
        self.assertEqual((top["provider"], top["account"], top["child"]),
                         ("anthropic", "seat@x.com", False))
        self.assertEqual(top["home"], home)
        self.assertEqual(top["cwd"], cwd)
        self.assertTrue(rows[300]["child"])  # found through the ancestor chain
        cx = rows[400]
        self.assertEqual(cx["home"], os.path.expanduser("~/.codex"))
        self.assertIsNone(cx["account"])     # default home unmapped -> never guessed
        self.assertEqual(cx["attribution"], "default")  # environ READ, key absent
        self.assertEqual(top["attribution"], "env")

    def test_unreadable_environ_is_visible_but_never_default_attributed(self):
        d = self.plant_proc(600, "codex", start=40)
        os.unlink(os.path.join(d, "environ"))  # pid died / permissions / race
        rows = {r["pid"]: r for r in who.scan([])}
        r = rows[600]
        self.assertIsNone(r["home"])     # NOT ~/.codex — no evidence is no home
        self.assertIsNone(r["account"])
        self.assertEqual(r["attribution"], "environ-unreadable")

    def test_pid_gone_between_comm_and_environ_is_skipped(self):
        d = self.plant_proc(700, "claude", start=50)
        os.unlink(os.path.join(d, "stat"))     # died right after the comm read
        os.unlink(os.path.join(d, "environ"))
        self.assertEqual([r for r in who.scan([]) if r["pid"] == 700], [])

    def test_starttime_change_mid_scan_discards_row(self):
        d = self.plant_proc(800, "codex", start=60)
        real = who.read_environ

        def racy(pid):  # pid reused mid-scan: starttime moves under the reads
            if pid == 800:
                with open(os.path.join(d, "stat"), "w") as f:
                    f.write("800 (codex) S 1 %s 61 0" % ("0 " * 17))
            return real(pid)

        with mock.patch.object(who, "read_environ", side_effect=racy):
            rows = who.scan([])
        self.assertEqual([r for r in rows if r["pid"] == 800], [])

    def test_shared_session_oldest_owns_rest_demote(self):
        rollout = "/store/rollout-2026-07-01T18-12-51-%s.jsonl" % SID
        self.plant_proc(210, "codex", start=100, fds=(rollout,))  # older -> owner
        self.plant_proc(220, "codex", start=200, fds=(rollout,))
        rows = {r["pid"]: r for r in who.scan([])}
        self.assertEqual(rows[210]["session"], SID)
        self.assertIsNone(rows[220]["session"])
        self.assertEqual(rows[220]["session_candidates"][0], SID)
        self.assertTrue(rows[210]["session_shared"])
        self.assertTrue(rows[220]["session_shared"])

    def test_two_sessions_no_dedupe(self):
        self.plant_proc(210, "codex", start=100,
                        fds=("/s/rollout-2026-07-01T00-00-00-%s.jsonl" % SID,))
        self.plant_proc(220, "codex", start=200,
                        fds=("/s/rollout-2026-07-01T00-00-00-%s.jsonl" % SID2,))
        rows = {r["pid"]: r for r in who.scan([])}
        self.assertEqual(rows[210]["session"], SID)
        self.assertEqual(rows[220]["session"], SID2)
        self.assertFalse(rows[210]["session_shared"])


class CmdTest(WhoBase):
    def run_cmd(self, args, accounts=()):
        out = io.StringIO()
        with mock.patch.object(who, "_accounts", return_value=list(accounts)), \
                contextlib.redirect_stdout(out):
            rc = who.cmd_who(list(args))
        return rc, out.getvalue()

    def test_empty_table(self):
        rc, out = self.run_cmd([])
        self.assertEqual(rc, 0)
        self.assertIn("no live claude/codex agent processes", out)

    def test_table_and_json(self):
        cwd = os.path.join(self.tmp, "work")
        os.makedirs(cwd)
        home = self.claude_home("seat-home", cwd, [SID])
        self.plant_proc(100, "claude", start=10,
                        env=b"CLAUDE_CONFIG_DIR=" + home.encode() + b"\0", cwd=cwd)
        accounts = [{"name": "seat@x.com", "provider": "anthropic", "home": home}]
        rc, out = self.run_cmd([], accounts)
        self.assertEqual(rc, 0)
        self.assertIn("helm who: 1 agent process (pid → cred)", out)
        self.assertIn("seat@x.com", out)
        self.assertIn(SID, out)
        self.assertNotIn("[child]", out)
        rc, out = self.run_cmd(["--json"], accounts)
        rows = json.loads(out)
        self.assertEqual(rows[0]["pid"], 100)
        self.assertEqual(rows[0]["session"], SID)

    def test_unattributed_row_renders_loud(self):
        d = self.plant_proc(600, "codex", start=40)
        os.unlink(os.path.join(d, "environ"))
        rc, out = self.run_cmd([])
        self.assertEqual(rc, 0)
        self.assertIn("[ENVIRON-UNREADABLE]", out)

    def test_shared_marker_renders(self):
        rollout = "/s/rollout-2026-07-01T00-00-00-%s.jsonl" % SID
        self.plant_proc(210, "codex", start=100, fds=(rollout,))
        self.plant_proc(220, "codex", start=200, fds=(rollout,))
        rc, out = self.run_cmd([])
        self.assertEqual(rc, 0)
        self.assertIn("[SHARED]", out)


if __name__ == "__main__":
    unittest.main()
