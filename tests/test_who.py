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
        self.plant_proc(400, "codex", ppid=1, start=30)  # no process HOME
        self.plant_proc(500, "vim", ppid=1)              # never an agent row
        accounts = [{"name": "seat@x.example", "provider": "anthropic", "home": home}]
        status = {}
        rows = {r["pid"]: r for r in who.scan(accounts, status=status)}
        self.assertEqual(set(rows), {100, 300, 400})
        top = rows[100]
        self.assertEqual((top["provider"], top["account"], top["child"]),
                         ("anthropic", "seat@x.example", False))
        self.assertEqual(top["home"], home)
        self.assertEqual(top["cwd"], cwd)
        self.assertTrue(rows[300]["child"])  # found through the ancestor chain
        cx = rows[400]
        self.assertIsNone(cx["home"])  # inspector ~/.codex is not process evidence
        self.assertIsNone(cx["account"])
        self.assertEqual(cx["attribution"], "home-unproven")
        self.assertIn(400, status["failed_pids"])
        self.assertEqual(top["attribution"], "env")

    def test_explicit_home_preserves_public_spelling_but_matches_canonically(self):
        cwd = os.path.join(self.tmp, "work-spelling")
        os.makedirs(cwd)
        home = self.claude_home("spelled-home", cwd, [SID])
        os.makedirs(os.path.join(home, "alias"))
        spelled = os.path.join(home, "alias", "..")
        self.plant_proc(101, "claude", ppid=1, start=10,
                        env=b"CLAUDE_CONFIG_DIR=" + spelled.encode() + b"\0",
                        cwd=cwd)
        row = who.scan([{"name": "seat@x.example", "provider": "anthropic",
                         "home": home}])[0]
        self.assertEqual(row["home"], spelled)
        self.assertEqual(row["account"], "seat@x.example")

    def test_a_VERSION_NAMED_agent_is_scanned_and_cred_attributed(self):
        """THE P0 AT THIS SURFACE. comm is the kernel's copy of the executable
        BASENAME, and the launcher exec's `claude/versions/<release>` — so the
        `comm == "claude"` branch, which ALSO picks the cred home here, stopped
        matching every agent on the estate and `helm who` answered "no live
        claude/codex agent processes" on a box running twenty-one.

        THE CRED ATTRIBUTION IS THE HALF THAT MAKES THIS MORE THAN A COUNT: a
        row that never enters the anthropic branch never resolves
        CLAUDE_CONFIG_DIR, so it is not merely missing — it cannot be charged to
        an account."""
        cwd = os.path.join(self.tmp, "wt")
        os.makedirs(cwd, exist_ok=True)
        home = self.claude_home("seat", cwd)
        d = self.plant_proc(701, "claude", start=10,
                            env=b"CLAUDE_CONFIG_DIR=%s\0" % home.encode(), cwd=cwd)
        rows = {r["pid"]: r for r in who.scan(
            [{"name": "seat@x.example", "provider": "anthropic", "home": home}])}
        self.assertEqual(rows[701]["provider"], "anthropic",
                         "POSITIVE CONTROL: the legacy comm must still resolve, "
                         "or the versioned assertion below measures a dead scan")
        d = self.plant_proc(702, "1.2.3", start=11,
                            env=b"CLAUDE_CONFIG_DIR=%s\0" % home.encode(), cwd=cwd)
        os.symlink("/x/.local/share/claude/versions/1.2.3",
                   os.path.join(d, "exe"))
        rows = {r["pid"]: r for r in who.scan(
            [{"name": "seat@x.example", "provider": "anthropic", "home": home}])}
        self.assertIn(702, rows)
        self.assertEqual(rows[702]["provider"], "anthropic")
        self.assertEqual(rows[702]["home"], home)

    def test_an_UNDECIDABLE_pid_marks_the_scan_and_is_never_a_silent_NO(self):
        """procid returns None for a pid whose comm LOOKS versioned and whose
        exe we were not PERMITTED to read — 450 of 870 readable-comm pids on
        this box refuse that read. A first draft of this cure put that call in
        an `or`, so None fell out of the truthy branch as "not an agent", the
        pid vanished, and the surface printed a confident empty estate over it.

        WHAT BUILD FAILS THIS: that draft. `failed_pids` stays empty there and
        the row simply is not present, which is indistinguishable from a box
        with nothing running."""
        self.plant_proc(704, "claude", start=13)       # decidable, same scan
        self.plant_proc(703, "1.2.3", start=12)        # versioned comm, NO exe
        status = {}
        rows = {r["pid"]: r for r in who.scan([], status=status)}
        self.assertIn(704, rows,
                      "POSITIVE CONTROL on the SAME observable: the scan must "
                      "still produce rows, or 703's absence is just an empty "
                      "result set")
        self.assertNotIn(703, rows, "undecidable is not an attributed row")
        self.assertIn(703, status["failed_pids"],
                      "but it MUST mark the scan incomplete, or an empty "
                      "result reads as a proven-empty estate")

    def _who(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            who.cmd_who(list(argv))
        return out.getvalue(), err.getvalue()

    def test_the_RENDERER_never_certifies_an_empty_fleet_it_could_not_see(self):
        """THE LAST INCH, and I lost the tri-state here after restoring it twice.
        `cmd_who` called `scan()` with NO status dict, so every pid it could not
        classify went into a bucket nobody asked for and the renderer printed
        "no live claude/codex agent processes" over it — the same sentence the
        outage was reported through, one layer above the predicate that caused
        it.

        THE PAIR IS ON ONE OBSERVABLE: the same rendered stdout, with and
        without an undecidable pid in the table. A build that always hedges
        fails the first half; the build I shipped fails the second."""
        empty_out, empty_err = self._who()
        self.assertIn("no live claude/codex agent processes", empty_out,
                      "POSITIVE CONTROL: a genuinely empty table must still say "
                      "so plainly, or the hedge below is unconditional")
        self.assertEqual(empty_err, "")
        self.plant_proc(705, "1.2.3", start=14)        # versioned comm, NO exe
        out, err = self._who()
        self.assertNotIn("no live claude/codex agent processes", out)
        self.assertIn("NOT a proven-empty fleet", out)
        self.assertIn("SCAN INCOMPLETE", err,
                      "and the incompleteness must reach stderr on BOTH paths, "
                      "because --json prints a bare array either way")

    def test_the_JSON_STDOUT_ITSELF_distinguishes_empty_from_unseen(self):  # noqa: VACUOUS_ASSERTION — the empty `rows` lists are the SHARED half of the pair, not the finding: both scans are asserted to produce `rows == []` precisely so that the difference lands entirely in `scan.complete`, and the closing assertNotEqual on the two raw stdout payloads is unconditional and cannot pass on a build that emits one byte-string for both states
        """THE MACHINE CONTRACT, and the arm that used to sit here CODIFIED THE
        DEFECT. It asserted the body stays a bare array and the warning goes to
        stderr — which reads as a design note and is not one: a consumer running
        `helm who --json 2>/dev/null`, the ordinary shape, gets BYTE-IDENTICAL
        STDOUT for a complete empty scan and an incomplete one. Identical bytes,
        opposite meanings, and the second is the confident-empty claim this lane
        exists to end. A test that pins a gap defends it.

        SO THE ASSERTION IS ON STDOUT ALONE, with stderr deliberately not
        consulted: the two payloads must DIFFER."""
        empty_out, _ = self._who("--json")
        empty = json.loads(empty_out)
        self.assertEqual(empty["rows"], [])
        self.assertIs(empty["scan"]["complete"], True,
                      "POSITIVE CONTROL: a scan that saw everything must SAY so, "
                      "or 'complete' carries no information in either state")
        self.plant_proc(707, "1.2.3", start=16)        # versioned comm, NO exe
        unseen_out, _ = self._who("--json")
        unseen = json.loads(unseen_out)
        self.assertEqual(unseen["rows"], [])           # same rows...
        self.assertIs(unseen["scan"]["complete"], False)   # ...different claim
        self.assertEqual(unseen["scan"]["unclassified"], [707])
        self.assertNotEqual(empty_out, unseen_out,
                            "STDOUT ITSELF must differ — this is the assertion "
                            "the stderr-only version could not make")

    def test_an_UNREADABLE_PROCESS_TABLE_is_not_a_COMPLETE_scan(self):  # noqa: VACUOUS_ASSERTION — the empty `rows` is the SHARED half of the pair by design: both runs are asserted to produce `rows == []` so the entire difference lands in `scan`, and the closing assertNotEqual on the two raw stdout payloads is unconditional
        """THE WORST CASE, AND MY DRAFT CALLED IT COMPLETE. `scan()` writes
        exactly TWO completeness channels — `failed_pids` (this pid could not be
        classified) and `listing_failed` (the process table itself could not be
        READ) — and I read only the first. So `os.listdir(PROC)` raising, which
        means WE SAW NOTHING AT ALL, arrived with failed_pids empty and reported
        `complete: true, unclassified: []`.

        An unreadable table certifying a complete scan is the confident-empty
        claim in its purest form. I found it by enumerating every write to
        `status` rather than the one channel I already knew about, which is the
        habit this whole lane keeps teaching me.

        THE PAIR IS ON STDOUT ITSELF: a readable-but-empty table and an
        unreadable one both yield `rows: []`, and must not yield the same
        payload."""
        empty = json.loads(self._who("--json")[0])
        self.assertEqual(empty["rows"], [])
        self.assertIs(empty["scan"]["complete"], True,
                      "POSITIVE CONTROL: a readable, genuinely empty table must "
                      "still report COMPLETE, or 'complete' means nothing")
        self.assertIs(empty["scan"]["listing_failed"], False)
        with mock.patch.object(who, "PROC", os.path.join(self.tmp, "no-such")):
            blind_out, blind_err = self._who("--json")
        blind = json.loads(blind_out)
        self.assertEqual(blind["rows"], [])                 # same rows...
        self.assertIs(blind["scan"]["complete"], False)     # ...opposite claim
        self.assertIs(blind["scan"]["listing_failed"], True)
        self.assertNotEqual(empty["scan"], blind["scan"],
                            "STDOUT ITSELF must distinguish an empty fleet from "
                            "an unseen one")
        self.assertIn("PROCESS TABLE COULD NOT BE READ", blind_err)

    def test_unreadable_environ_is_visible_but_never_default_attributed(self):
        d = self.plant_proc(600, "codex", start=40)
        os.unlink(os.path.join(d, "environ"))  # pid persists; probe failed
        status = {}
        rows = {r["pid"]: r for r in who.scan([], status=status)}
        r = rows[600]
        self.assertIsNone(r["home"])     # NOT ~/.codex — no evidence is no home
        self.assertIsNone(r["account"])
        self.assertEqual(r["attribution"], "environ-unreadable")
        self.assertIn(600, status["failed_pids"])

    def test_default_home_belongs_to_the_process_not_the_inspector(self):
        alt = os.path.join(self.tmp, "alternate-home")
        self.plant_proc(610, "claude", start=41,
                        env=("HOME=" + alt).encode() + b"\0", cwd=self.tmp)
        rows = {r["pid"]: r for r in who.scan([])}
        self.assertEqual(rows[610]["home"], os.path.join(alt, ".claude"))

    def test_missing_process_home_is_unproven_not_inspector_default(self):
        self.plant_proc(611, "claude", start=42, env=b"A=1\0", cwd=self.tmp)
        status = {}
        rows = {r["pid"]: r for r in who.scan([], status=status)}
        self.assertIsNone(rows[611]["home"])
        self.assertEqual(rows[611]["attribution"], "home-unproven")
        self.assertIn(611, status["failed_pids"])

    def test_unreadable_cwd_reaches_the_completeness_channel(self):
        self.plant_proc(612, "claude", start=43,
                        env=b"HOME=/tmp/process-home\0", cwd=None)
        status = {}
        rows = {r["pid"]: r for r in who.scan([], status=status)}
        self.assertIsNone(rows[612]["cwd"])
        self.assertIn(612, status["failed_pids"])

    def test_unreadable_live_stat_reaches_the_completeness_channel(self):
        d = self.plant_proc(620, "claude", start=42)
        os.unlink(os.path.join(d, "stat"))
        status = {}
        self.assertEqual(who.scan([], status=status), [])
        self.assertIn(620, status["failed_pids"])
        self.assertFalse(status["listing_failed"])

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
        accounts = [{"name": "seat@x.example", "provider": "anthropic", "home": home}]
        rc, out = self.run_cmd([], accounts)
        self.assertEqual(rc, 0)
        self.assertIn("helm who: 1 agent process (pid → cred)", out)
        self.assertIn("seat@x.example", out)
        self.assertIn(SID, out)
        self.assertNotIn("[child]", out)
        rc, out = self.run_cmd(["--json"], accounts)
        # The body is an OBJECT since the completeness cure: `rows` is the same
        # list it always was, and `scan` is the fact a consumer needs before
        # treating an empty `rows` as a proven-empty fleet. This arm IS the
        # canonical consumer, and it is why "no consumers" was the wrong answer
        # to "is this contract change safe" — I had grepped for the string
        # `who --json` and it calls cmd_who(["--json"]).
        body = json.loads(out)
        self.assertEqual(body["scan"], {"complete": True,
                                        "listing_failed": False,
                                        "unclassified": []})
        rows = body["rows"]
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
