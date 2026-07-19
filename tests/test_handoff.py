#!/usr/bin/env python3
"""handoff + now tests — compaction continuity (the handoff-now card).

Pins the laws: session-keyed never pane; hook mode fail-open TOTAL (rc 0,
silent-when-satisfied, garbled payload nags nobody, capture never blocks);
the 48h freshness gate on `now show` (a stale now.md actively misleads) with
the legacy ~/.remember read fallback; the 40-line newest-first cap; the
typed journal entry (DONE/REMAINING/NEXT frontmatter, same-day re-write
lands on one path, mutation receipt); check's newer-than-session-start gate
on both artifact legs; recover's argv-safe cv wrap (law 4). Hermetic:
HELM_HOME + HELM_REMEMBER_DIR are tmp dirs; git is mocked except the one
fail-open probe; the live estate is never touched."""
import calendar
import contextlib
import glob
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("HELM_HOME", tempfile.mkdtemp(prefix="helm-test-home-"))

from helm import cli, handoff, inject, pk, record  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_REMEMBER_DIR", "MELD_REMEMBER_DIR",
            "CLAUDE_SESSION_ID")

PROSE = """Session wrap.

## DONE
- landed the handoff verb + tests

REMAINING: wire the PreCompact hook fleet-wide

NEXT: run `helm handoff check` in the fresh window
"""


class HandoffBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-handoff-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_REMEMBER_DIR"] = os.path.join(self.tmp, "remember")
        self.cwd_prior = os.getcwd()
        self.wd = os.path.join(self.tmp, "repo")
        os.makedirs(self.wd)
        os.chdir(self.wd)

    def tearDown(self):
        os.chdir(self.cwd_prior)
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cmd(self, fn, args, stdin_text=None):
        out, err = io.StringIO(), io.StringIO()
        stdin = io.StringIO(stdin_text or "")
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(sys, "stdin", stdin):
            rc = fn(list(args))
        return rc, out.getvalue(), err.getvalue()

    def git_mock(self, branch="main", porcelain="", toplevel=None):
        """handoff._git canned per subcommand — no subprocess in the suite."""
        def _git(cwd, *args):
            if args[:1] == ("rev-parse",) and "--show-toplevel" in args:
                return toplevel
            if args[:1] == ("rev-parse",):
                return branch
            if args[:1] == ("status",):
                return porcelain
            return None
        return mock.patch.object(handoff, "_git", side_effect=_git)

    def read_now(self):
        try:
            with open(handoff.now_path(), encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""


class NowCaptureTest(HandoffBase):
    def test_snapshot_carries_sid_git_edits_and_reflex_state(self):
        sid = "sess-abc123def"
        pk.write_json(os.path.join(record.session_dir(sid), "counters.json"),
                      {"passive-streak": 0, "dirty-streak": 2, "stuck-streak": 0,
                       "loop-streak": 1})
        with open(os.path.join(record.session_dir(sid), "edit-targets.log"),
                  "w", encoding="utf-8") as f:
            f.write("a.py\nb.py\na.py\n")
        # first line stripped of its leading status space — exactly what _git's
        # stdout.strip() hands back live (the column-offset bug's shape)
        with self.git_mock(porcelain="M docs/A.md\n M helm/a.py\n?? tests/b.py"):
            path = handoff.capture(sid, self.wd)
        self.assertEqual(path, handoff.now_path())
        text = self.read_now()
        self.assertIn("sid=sess-abc", text)          # session-keyed, 8-char tag
        self.assertIn("branch main  dirty 3", text)
        self.assertIn("changed: docs/A.md helm/a.py tests/b.py", text)
        self.assertIn("edited: a.py b.py", text)     # deduped, newest-first tail
        self.assertIn("reflex: passive=0 dirty=2 stuck=0 loop=1", text)

    def test_newest_first_and_40_line_cap(self):
        with self.git_mock():
            for i in range(20):
                handoff.capture("sid-%03d" % i, self.wd)
        lines = self.read_now().splitlines()
        self.assertLessEqual(len(lines), handoff.NOW_LINES)
        self.assertIn("sid=sid-019", lines[0])       # newest block on top
        self.assertIn("sid=sid-018", self.read_now())

    def test_capture_fail_open_and_no_git(self):
        # a real git probe on a non-repo is None, and capture still writes
        self.assertIsNone(handoff._git(self.tmp, "rev-parse",
                                       "--abbrev-ref", "HEAD"))
        with self.git_mock(branch=None):
            self.assertEqual(handoff.capture("s", self.wd), handoff.now_path())
        self.assertIn("(no git)", self.read_now())
        with mock.patch.object(pk, "atomic_write", side_effect=OSError("full")):
            self.assertIsNone(handoff.capture("s", self.wd))  # never raises


class NowShowTest(HandoffBase):
    def write_now(self, text, age_h=0):
        pk.atomic_write(handoff.now_path(), text)
        if age_h:
            old = time.time() - age_h * 3600
            os.utime(handoff.now_path(), (old, old))

    def test_fresh_prints_stale_and_absent_are_silent(self):
        self.assertEqual(handoff.show(), "")
        self.write_now("## snap\n")
        self.assertIn("## snap", handoff.show())
        self.assertTrue(handoff.show().startswith("helm now ("))
        self.write_now("## snap\n", age_h=handoff.FRESH_H + 1)
        self.assertEqual(handoff.show(), "")         # the load-bearing gate

    def test_legacy_remember_read_fallback_until_retired(self):
        legacy = handoff.legacy_now_path()
        os.makedirs(os.path.dirname(legacy))
        with open(legacy, "w", encoding="utf-8") as f:
            f.write("north star: ship helm\n")
        self.assertIn("north star", handoff.show())  # no helm snapshot yet
        self.write_now("## helm snap\n")
        self.assertIn("## helm snap", handoff.show())  # helm's own wins
        self.write_now("", age_h=0)                  # empty helm file
        with open(legacy, "w", encoding="utf-8") as f:
            f.write("")                              # the live 0-byte case
        self.assertEqual(handoff.show(), "")

    def test_cmd_now(self):
        rc, out, _ = self.run_cmd(handoff.cmd_now, ["show"])
        self.assertEqual((rc, out), (0, ""))         # stale/absent = silent
        with self.git_mock():
            rc, out, _ = self.run_cmd(handoff.cmd_now,
                                      ["capture", "--session", "s1"])
        self.assertEqual(rc, 0)
        self.assertIn(handoff.now_path(), out)
        rc, out, _ = self.run_cmd(handoff.cmd_now, ["show"])
        self.assertIn("sid=s1", out)
        with self.git_mock():                        # hook mode: silent rc 0
            rc, out, err = self.run_cmd(handoff.cmd_now, ["capture", "--hook-json"],
                                        json.dumps({"session_id": "s2",
                                                    "cwd": self.wd}))
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertIn("sid=s2", self.read_now())
        with mock.patch.object(handoff, "capture", return_value=None):
            rc, _, err = self.run_cmd(handoff.cmd_now, ["capture"])
        self.assertEqual(rc, 1)
        self.assertIn("fail-open", err)
        rc, _, err = self.run_cmd(handoff.cmd_now, ["bogus"])
        self.assertEqual(rc, 2)
        self.assertIn("usage:", err)


class WriteTest(HandoffBase):
    def entry(self):
        files = glob.glob(os.path.join(
            os.environ["HELM_HOME"], "proj", "journal", "*handoff*.md"))
        self.assertEqual(len(files), 1)
        defaults = {"name": "", "description": "", "type": "", "session_id": "",
                    "project": "", "ts": "", "done": "", "remaining": "", "next": ""}
        return files[0], pk.parse_simple_frontmatter(files[0], defaults)

    def test_write_typed_journal_entry(self):
        rc, out, err = self.run_cmd(
            handoff.cmd_handoff,
            ["write", "--project", "proj", "--session", "sess-12345678"], PROSE)
        self.assertEqual(rc, 0, err)
        path, e = self.entry()
        self.assertIn("-handoff-sess-123", path)     # sid8 in the filename
        self.assertEqual(e["type"], "handoff")
        self.assertEqual(e["session_id"], "sess-12345678")
        self.assertEqual(e["project"], "proj")
        self.assertEqual(e["done"], "landed the handoff verb + tests")
        self.assertEqual(e["remaining"], "wire the PreCompact hook fleet-wide")
        self.assertEqual(e["next"], "run `helm handoff check` in the fresh window")
        with open(path, encoding="utf-8") as f:
            self.assertIn("Session wrap.", f.read()) # full prose is the body
        self.assertIn(path, out)
        self.assertEqual(pk.read_events(5)[-1]["verb"], "handoff.write")

    def test_rewrite_lands_on_the_same_path(self):
        for _ in range(2):
            rc, _, _ = self.run_cmd(
                handoff.cmd_handoff,
                ["write", "--project", "proj", "--session", "sess-12345678"], PROSE)
            self.assertEqual(rc, 0)
        self.entry()                                 # exactly one file

    def test_missing_section_warns_but_writes(self):
        rc, _, err = self.run_cmd(
            handoff.cmd_handoff, ["write", "--project", "proj"],
            "DONE: everything\nsome prose\n")
        self.assertEqual(rc, 0)
        self.assertIn("REMAINING/NEXT", err)
        _, e = self.entry()
        self.assertEqual(e["done"], "everything")
        self.assertEqual(e["next"], "")

    def test_empty_stdin_and_no_project_refuse(self):
        rc, _, err = self.run_cmd(handoff.cmd_handoff, ["write"], "  \n")
        self.assertEqual(rc, 2)
        rc, _, err = self.run_cmd(handoff.cmd_handoff, ["write"], PROSE)
        self.assertEqual(rc, 1)                      # empty registry: no project
        self.assertIn("--project", err)

    def test_summaries_heading_shapes_and_no_prose_false_match(self):
        s = handoff._summaries(
            "we are done with the prose test\n"      # bare word: never a section
            "## DONE\n- shipped it\n"
            "**REMAINING:** the hook\n"
            "NEXT — verify\n")
        self.assertEqual(s, {"done": "shipped it", "remaining": "the hook",
                             "next": "verify"})


class CheckTest(HandoffBase):
    SID = "aaaabbbb-1111-2222-3333-444455556666"

    def journal_entry(self, sid=None, age_h=0):
        d = os.path.join(os.environ["HELM_HOME"], "proj", "journal")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "2026-07-19-handoff-x.md")
        with open(p, "w", encoding="utf-8") as f:
            f.write("session_id: %s\n" % (sid or self.SID))
        if age_h:
            old = time.time() - age_h * 3600
            os.utime(p, (old, old))
        return p

    def test_journal_leg_needs_sid_and_freshness(self):
        with self.git_mock(), \
                mock.patch.object(inject, "project_for_cwd", return_value="proj"):
            self.assertIsNone(handoff.check(self.SID, self.wd, None))
            p = self.journal_entry()
            self.assertEqual(handoff.check(self.SID, self.wd, None), p)
            self.assertIsNone(handoff.check("other-sid", self.wd, None))
            os.remove(p)
            self.journal_entry(age_h=handoff.FRESH_H + 1)
            self.assertIsNone(handoff.check(self.SID, self.wd, None))

    def test_repo_file_leg_gated_on_session_start(self):
        rp = os.path.join(self.wd, handoff.REPO_FILE)
        with open(rp, "w", encoding="utf-8") as f:
            f.write("next: x\n")
        with self.git_mock(toplevel=self.wd):
            self.assertEqual(handoff.check(None, self.wd, None), rp)
            future = time.time() + 60                # started after the write
            self.assertIsNone(handoff.check(None, self.wd, future))

    def test_cli_check_satisfied_and_nag(self):
        rc, _, err = self.run_cmd(handoff.cmd_handoff,
                                  ["check", "--session", self.SID])
        self.assertEqual(rc, 1)
        with open(os.path.join(self.wd, handoff.REPO_FILE), "w") as f:
            f.write("next\n")
        with self.git_mock(toplevel=self.wd):
            rc, out, _ = self.run_cmd(handoff.cmd_handoff,
                                      ["check", "--session", self.SID])
        self.assertEqual(rc, 0)
        self.assertIn("contract satisfied", out)
        self.assertIn(handoff.REPO_FILE, out)

    def hook_check(self, payload):
        with self.git_mock():
            return self.run_cmd(handoff.cmd_handoff, ["check", "--hook-json"],
                                payload)

    def test_hook_mode_two_legs_one_trigger(self):
        # missing contract: nag on stdout, rc 0, AND the snapshot was taken
        rc, out, err = self.hook_check(json.dumps(
            {"session_id": self.SID, "cwd": self.wd,
             "hook_event_name": "PreCompact", "trigger": "auto"}))
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("NO handoff artifact", out)
        self.assertIn("helm handoff write", out)
        self.assertIn("helm now show", out)
        self.assertLessEqual(len(out.splitlines()), 4)   # PreCompact fires late
        self.assertIn("sid=aaaabbbb", self.read_now())   # leg 2 rode the trigger

    def test_hook_mode_silent_when_satisfied(self):
        with open(os.path.join(self.wd, handoff.REPO_FILE), "w") as f:
            f.write("next\n")
        with self.git_mock(toplevel=self.wd):
            rc, out, err = self.run_cmd(
                handoff.cmd_handoff, ["check", "--hook-json"],
                json.dumps({"session_id": self.SID, "cwd": self.wd,
                            "hook_event_name": "SessionEnd"}))
        self.assertEqual((rc, out, err), (0, "", ""))    # salience law
        self.assertIn("sid=aaaabbbb", self.read_now())   # capture still ran

    def test_hook_mode_garbled_payload_nags_nobody(self):
        rc, out, err = self.hook_check("not json{{{")
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertEqual(self.read_now(), "")            # no keyless capture

    def test_session_start_from_transcript_gates_the_repo_file(self):
        tp = os.path.join(self.tmp, "t.jsonl")
        with open(tp, "w", encoding="utf-8") as f:
            f.write('{"type":"last-prompt","sessionId":"x"}\n')
            f.write('{"timestamp":"2099-01-01T00:00:00.000Z","type":"user"}\n')
        want = float(calendar.timegm((2099, 1, 1, 0, 0, 0, 0, 0, 0)))
        self.assertEqual(handoff._session_start(tp), want)
        self.assertIsNone(handoff._session_start("/gone/t.jsonl"))
        with open(os.path.join(self.wd, handoff.REPO_FILE), "w") as f:
            f.write("old handoff\n")                     # older than 2099 start
        rc, out, _ = self.hook_check(json.dumps(
            {"session_id": self.SID, "cwd": self.wd, "transcript_path": tp}))
        self.assertEqual(rc, 0)
        self.assertIn("NO handoff artifact", out)


class RecoverTest(HandoffBase):
    def test_wraps_the_one_recall_index(self):
        run = mock.Mock(return_value=mock.Mock(returncode=0))
        with mock.patch("subprocess.run", run):
            rc, _, _ = self.run_cmd(handoff.cmd_handoff, ["recover", "abc123"])
        self.assertEqual(rc, 0)
        self.assertEqual(run.call_args[0][0],
                         ["cv", "show", "abc123", "--pre-compaction"])

    def test_argv_safety_and_missing_cv(self):
        rc, _, err = self.run_cmd(handoff.cmd_handoff, ["recover", "--evil"])
        self.assertEqual(rc, 2)
        rc, _, err = self.run_cmd(handoff.cmd_handoff, ["recover"])
        self.assertEqual(rc, 2)
        with mock.patch("subprocess.run", side_effect=FileNotFoundError()):
            rc, _, err = self.run_cmd(handoff.cmd_handoff, ["recover", "abc123"])
        self.assertEqual(rc, 1)
        self.assertIn("cv show abc123 --pre-compaction", err)


class CliWiringTest(HandoffBase):
    def test_verbs_and_help_wired(self):
        for verb in ("handoff", "now"):
            self.assertIn(verb, cli.VERBS)
            self.assertIn(verb, cli._VERB_HELP)
        rc, _, err = self.run_cmd(cli.main, ["handoff"])
        self.assertEqual(rc, 2)
        self.assertIn("usage:", err)
        rc, out, _ = self.run_cmd(cli.main, ["now", "--help"])
        self.assertEqual(rc, 0)
        self.assertIn("48h freshness gate", out)


if __name__ == "__main__":
    unittest.main()
