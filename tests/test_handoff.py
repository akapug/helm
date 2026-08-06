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

from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-home-", var="HELM_HOME")

from helm import cli, handoff, inject, pk, record  # noqa: E402

# HELM_CHAT_NAME is SET by the attribution tests, so it belongs here: setUp
# pops this tuple and tearDown restores it, which means a key that is set but
# unlisted is never cleaned and leaks into every later test in the process
# (measured live — a sibling test was green only because it drank a leaked
# profile it never set).
ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_REMEMBER_DIR", "MELD_REMEMBER_DIR",
            "CLAUDE_SESSION_ID", "HELM_CHAT_NAME", "MELD_CHAT_NAME")

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

    def test_a_missing_section_REFUSES_the_write(self):  # noqa: VACUOUS_ASSERTION — the empty-shelf glob has its unconditional positive control two lines down: the follow-up full write asserts rc 0 and self.entry() asserts the SAME glob finds exactly one file
        """The half the warning could never be: `write` used to land the entry
        anyway, so `handoff check` reported an entry EXISTS over an empty
        done: — a parser that cannot see a case must not emit a confident
        artifact. Refusal is TOTAL: nonzero rc, the missing sections named,
        and NO file on the shelf."""
        rc, out, err = self.run_cmd(
            handoff.cmd_handoff, ["write", "--project", "proj"],
            "DONE: everything\nsome prose\n")
        self.assertEqual(rc, 1)
        self.assertIn("REFUSED", err)
        self.assertIn("REMAINING/NEXT", err)
        self.assertNotIn("journal entry landed", out)
        # assert the EFFECT, not the absence of a complaint: an empty shelf —
        # then the SAME observable goes positive under a full write, so the
        # emptiness above is the refusal and never a wrong glob
        self.assertEqual(glob.glob(os.path.join(
            os.environ["HELM_HOME"], "proj", "journal", "*")), [])
        rc, _, _ = self.run_cmd(
            handoff.cmd_handoff, ["write", "--project", "proj"], PROSE)
        self.assertEqual(rc, 0)
        self.entry()                                 # exactly one file now

    def test_help_answers_the_help_question_instead_of_reading_stdin(self):  # noqa: VACUOUS_ASSERTION — the empty-shelf glob has its unconditional positive control at the tail: the SAME prose through the SAME verb without --help lands (rc 0) and self.entry() asserts the SAME glob finds exactly one file
        """`handoff write --help` used to consume --help as CONTENT and answer
        "empty stdin — nothing to hand off": the one question a help flag asks
        is the one question that reply cannot be an answer to. Same shape as
        `gate run --help` joining the FIFO.

        THE STDIN HERE IS VALID PROSE ON PURPOSE. A guard that merely printed
        usage and fell through would still write the entry, and a test that
        only checked the output would pass over it. So the binding assertion is
        the EMPTY SHELF: help short-circuits BEFORE the read, therefore prose
        that would otherwise land does not."""
        rc, out, err = self.run_cmd(
            handoff.cmd_handoff, ["write", "--project", "proj", "--help"], PROSE)
        self.assertEqual(rc, 0, err)
        self.assertIn("usage: helm handoff", out)     # usage, on STDOUT
        self.assertNotIn("empty stdin", out + err)    # not the old wrong answer
        self.assertEqual(glob.glob(os.path.join(      # nothing was written
            os.environ["HELM_HOME"], "proj", "journal", "*")), [])
        # CONTROL, unconditional: the SAME prose through the SAME verb without
        # --help lands, so the emptiness above is the short-circuit and never a
        # wrong glob or a broken fixture.
        rc, _, _ = self.run_cmd(
            handoff.cmd_handoff, ["write", "--project", "proj"], PROSE)
        self.assertEqual(rc, 0)
        self.entry()                                 # exactly one file now

    def test_now_help_short_circuits_too(self):
        """The defect is the VERB TABLE answering help with work, so the cure
        covers `now` as well — fixing only the verb observed failing would be
        the per-case handling this repo keeps re-learning to avoid."""
        rc, out, err = self.run_cmd(handoff.cmd_now, ["capture", "--help"], "")
        self.assertEqual(rc, 0, err)
        self.assertIn("usage: helm now", out)

    def test_the_refusal_QUOTES_the_line_it_could_not_parse(self):  # noqa: VACUOUS_ASSERTION — the empty-shelf glob has its unconditional positive control at the tail: the same stdin with the separator restored lands (rc 0) and self.entry() asserts the SAME glob finds exactly one file
        """The measured shape: a DONE heading whose qualifier has
        no separator parses as nothing, and the author must be shown WHICH of
        their own lines the parser could not read — not a regex."""
        bad = ("DONE this window we landed three lanes\n"
               "REMAINING: wire the hook\nNEXT: verify\n")
        rc, _, err = self.run_cmd(
            handoff.cmd_handoff, ["write", "--project", "proj"], bad)
        self.assertEqual(rc, 1)
        self.assertIn("no DONE section parsed", err)
        self.assertIn("DONE this window we landed three lanes", err)
        self.assertEqual(glob.glob(os.path.join(
            os.environ["HELM_HOME"], "proj", "journal", "*")), [])
        # positive control on the same observable: the same stdin with the
        # separator restored lands, so the empty shelf above is the refusal
        rc, _, _ = self.run_cmd(
            handoff.cmd_handoff, ["write", "--project", "proj"],
            bad.replace("DONE this window we", "DONE this window —"))
        self.assertEqual(rc, 0)
        self.entry()                                 # exactly one file now

    def test_a_refusal_never_touches_an_EXISTING_entry(self):
        """A same-day same-session rewrite lands on the SAME path, so a
        refused rewrite must leave the good entry byte-identical — and emit no
        receipt, because a receipt for a write that did not happen is the same
        confident-artifact defect one ledger over."""
        rc, _, _ = self.run_cmd(
            handoff.cmd_handoff,
            ["write", "--project", "proj", "--session", "sess-12345678"], PROSE)
        self.assertEqual(rc, 0)
        path, _ = self.entry()
        with open(path, encoding="utf-8") as f:
            before = f.read()
        events = pk.read_events(50)
        # control: the good write left a receipt, so the unchanged-events
        # assertion below compares real ledgers and not two empty lists
        self.assertIn("handoff.write", [e["verb"] for e in events])
        rc, _, err = self.run_cmd(
            handoff.cmd_handoff,
            ["write", "--project", "proj", "--session", "sess-12345678"],
            "DONE this window three lanes\nsome prose\n")
        self.assertEqual(rc, 1)
        self.assertIn("DONE/REMAINING/NEXT", err)
        path2, _ = self.entry()                      # still exactly one file
        with open(path2, encoding="utf-8") as f:
            self.assertEqual((path2, f.read()), (path, before))
        self.assertEqual(pk.read_events(50), events)

    def test_the_measured_phrasing_writes_a_POPULATED_entry(self):
        """End to end through the CLI: the phrasing that used to land an
        empty done: (the word-run rung) now lands a populated one."""
        rc, _, err = self.run_cmd(
            handoff.cmd_handoff,
            ["write", "--project", "proj", "--session", "sess-12345678"],
            "DONE this window — THREE lanes\nREMAINING: wire it\nNEXT: verify\n")
        self.assertEqual(rc, 0, err)
        _, e = self.entry()
        self.assertEqual(e["done"], "THREE lanes")
        self.assertEqual(e["remaining"], "wire it")

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


class CompactionFloorTest(CheckTest):
    """The EVENT floor — bug class `compaction-floors-on-session-start`.

    LIVE MISS: a PreCompact hook printed "contract satisfied —
    <a 37h-old handoff>" and therefore wrote no handoff for the
    compaction happening right then. The floor was SESSION start, and CC
    2.1.217+ keeps one session id and one transcript across a compaction, so
    session start never moves and the gate widens as the session ages. These
    pin the narrower, event-scoped question — and pin that it stayed scoped to
    compaction rather than becoming a blanket tightening of `check`.
    """

    def transcript(self, boundary_at=None, start="2026-07-01T00:00:00.000Z"):
        tp = os.path.join(self.tmp, "t.jsonl")
        rows = ['{"timestamp":"%s","type":"user"}' % start]
        if boundary_at:
            rows.append('{"type":"system","subtype":"compact_boundary",'
                        '"timestamp":"%s","compactMetadata":{"trigger":"auto"}}'
                        % boundary_at)
        with open(tp, "w", encoding="utf-8") as f:
            f.write("\n".join(rows) + "\n")
        return tp

    def test_last_compaction_reads_the_boundary_record(self):
        want = float(calendar.timegm((2026, 7, 27, 12, 0, 0, 0, 0, 0)))
        tp = self.transcript(boundary_at="2026-07-27T12:00:00.000Z")
        self.assertEqual(handoff.last_compaction(tp), want)
        self.assertIsNone(handoff.last_compaction(self.transcript()))
        self.assertIsNone(handoff.last_compaction("/gone/t.jsonl"))
        self.assertIsNone(handoff.last_compaction(""))

    def test_the_floor_is_the_TIGHTEST_of_its_three_legs(self):
        now = 1_000_000.0
        window = handoff.EVENT_FRESH_H * 3600
        # 1. the event window wins when session start and the last boundary are
        #    both older — the leg that fixes the live miss
        self.assertEqual(handoff.compaction_floor(now - 40 * 3600, None, now=now),
                         now - window)
        # 2. session start wins when it is NEWER than the window
        self.assertEqual(handoff.compaction_floor(now - 60, None, now=now),
                         now - 60)
        # 3. the previous compaction wins when it is newest of all
        tp = self.transcript(boundary_at="2026-07-27T12:00:00.000Z")
        boundary = float(calendar.timegm((2026, 7, 27, 12, 0, 0, 0, 0, 0)))
        self.assertEqual(
            handoff.compaction_floor(boundary - 3600, tp, now=boundary + 60),
            boundary)

    def test_a_37h_old_handoff_no_longer_satisfies_a_compaction(self):
        """The live case, end to end through the hook."""
        with mock.patch.object(inject, "project_for_cwd", return_value="proj"):
            p = self.journal_entry(age_h=37)
            tp = self.transcript()                  # session start 2026-07-01
            payload = json.dumps({"session_id": self.SID, "cwd": self.wd,
                                  "hook_event_name": "PreCompact",
                                  "transcript_path": tp, "trigger": "auto"})
            # the STANDING question still says yes — this artifact is real and
            # inside 48h; what changed is which question a compaction asks
            self.assertEqual(handoff.check(self.SID, self.wd, None), p)
            rc, out, err = self.hook_check(payload)
            self.assertEqual((rc, err), (0, ""))
            self.assertIn("predates this compaction", out)
            self.assertIn("37h old", out)
            self.assertIn(os.path.basename(p), out)
            self.assertIn("helm handoff write", out)

    def test_a_handoff_written_FOR_the_compaction_is_silent(self):
        with mock.patch.object(inject, "project_for_cwd", return_value="proj"):
            self.journal_entry()                    # written just now
            rc, out, err = self.hook_check(json.dumps(
                {"session_id": self.SID, "cwd": self.wd,
                 "hook_event_name": "PreCompact",
                 "transcript_path": self.transcript()}))
            self.assertEqual((rc, out, err), (0, "", ""))   # salience law

    def test_an_artifact_from_before_the_LAST_compaction_never_satisfies(self):
        """Inside the event window but on the far side of the previous
        boundary: that span is already discarded, so it cannot describe what
        this compaction is about to discard."""
        import time as _t
        with mock.patch.object(inject, "project_for_cwd", return_value="proj"):
            self.journal_entry(age_h=0.5)           # 30m old: inside the window
            stamp = _t.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                                _t.gmtime(_t.time() - 300))   # boundary 5m ago
            rc, out, _ = self.hook_check(json.dumps(
                {"session_id": self.SID, "cwd": self.wd,
                 "hook_event_name": "PreCompact",
                 "transcript_path": self.transcript(boundary_at=stamp)}))
            self.assertEqual(rc, 0)
            self.assertIn("predates this compaction", out)

    def test_SessionEnd_keeps_the_STANDING_question(self):
        """Scoped, not a blanket tightening: nothing is discarded mid-flight at
        SessionEnd, so the same 37h-old artifact still satisfies it."""
        with mock.patch.object(inject, "project_for_cwd", return_value="proj"):
            self.journal_entry(age_h=37)
            rc, out, err = self.hook_check(json.dumps(
                {"session_id": self.SID, "cwd": self.wd,
                 "hook_event_name": "SessionEnd",
                 "transcript_path": self.transcript()}))
            self.assertEqual((rc, out, err), (0, "", ""))

    def test_the_event_window_is_env_overridable(self):
        os.environ["HELM_HANDOFF_EVENT_FRESH_H"] = "72"
        try:
            with mock.patch.object(inject, "project_for_cwd", return_value="proj"):
                self.journal_entry(age_h=37)
                rc, out, err = self.hook_check(json.dumps(
                    {"session_id": self.SID, "cwd": self.wd,
                     "hook_event_name": "PreCompact",
                     "transcript_path": self.transcript()}))
                self.assertEqual((rc, out, err), (0, "", ""))
        finally:
            os.environ.pop("HELM_HANDOFF_EVENT_FRESH_H", None)


class UnreadableIsNotAbsentTest(CheckTest):
    """The third and fourth rungs of ONE class.

    An unreadable ENTRY and an unreadable SHELF were fixed first; `hollow` and
    `check` sat one and two layers further out and did the same thing. hollow
    returned () — which MEANS DISCHARGED — so a handoff nobody could open
    reported as SATISFIED. check returned None, so the nag said "the next
    window starts blind" about a file that may exist and be fine.

    Both directions are how a guard gets ignored: one tells an author their
    work is done when it may not be, the other tells them to write a file they
    already wrote."""

    def unreadable(self, path):
        os.chmod(path, 0)
        self.addCleanup(lambda: os.path.exists(path) and os.chmod(path, 0o644))
        if os.access(path, os.R_OK):
            self.skipTest("cannot make a file unreadable as this user")

    def test_hollow_says_UNDECIDED_instead_of_DISCHARGED(self):
        p = self.journal_entry()
        with open(p, "a", encoding="utf-8") as f:
            f.write("---\nmetadata:\n  type: handoff\n---\n\nDONE: x\n"
                    "REMAINING: y\nNEXT: z\n")
        # positive control: readable and complete -> discharged, no error
        self.assertEqual(handoff.hollow_checked(p), ((), None))
        self.unreadable(p)
        sections, err = handoff.hollow_checked(p)
        self.assertEqual(sections, ())
        self.assertIn("could not be read", err or "")
        self.assertIn("PermissionError", err or "")
        # the bare name still answers the old question for old callers
        self.assertEqual(handoff.hollow(p), ())

    def test_an_unreadable_JOURNAL_DIR_is_unknown_not_no_artifact(self):
        p = self.journal_entry()
        d = os.path.dirname(p)
        with mock.patch.object(inject, "project_for_cwd", return_value="proj"):
            found, _m, _h, err = handoff.check_checked(self.SID, self.wd, None)
            self.assertEqual(found, p,
                             "positive control: it IS found when readable")
            self.assertIsNone(err)
            os.chmod(d, 0)
            self.addCleanup(lambda: os.path.isdir(d) and os.chmod(d, 0o755))
            if os.access(d, os.R_OK):
                self.skipTest("cannot make a directory unreadable as this user")
            found, _m, _h, err = handoff.check_checked(self.SID, self.wd, None)
        self.assertIsNone(found)
        self.assertIn("journal dir", err or "")

    def test_an_ABSENT_shelf_stays_an_honest_absence(self):  # noqa: VACUOUS_ASSERTION — err IS None by product law here; the whole claim is that a missing shelf reports ABSENT and never UNREADABLE, so the negative is the contract, and the positive control below proves the same call does return a path when one exists
        """The fix must not convert "you have no handoff" into UNKNOWN, or the
        nag stops firing for everyone who genuinely has not written one."""
        with mock.patch.object(inject, "project_for_cwd", return_value="proj"):
            found, _m, _h, err = handoff.check_checked(self.SID, self.wd, None)
            self.assertIsNone(found)
            self.assertIsNone(err, "no shelf is ABSENT, never unreadable")
            # POSITIVE CONTROL on the same call: with a shelf it finds one, so
            # the double-None above is the absence and not an inert fixture.
            p = self.journal_entry()
            found, _m, _h, err = handoff.check_checked(self.SID, self.wd, None)
        self.assertIn("handoff", found)     # a real path, not another None
        self.assertEqual(found, p)
        self.assertIsNone(err)

    def test_an_unreadable_ENTRY_is_unknown_not_no_artifact(self):
        """A cross-family review mutated this handler away and my 12 tests stayed GREEN. I
        named three swallow points in the docstring and pinned one — the
        directory — so the ENTRY and the repo file were prose, not coverage.
        A test suite that describes a fix it does not exercise is the same
        laundering the fix is about."""
        p = self.journal_entry()
        d = os.path.dirname(p)
        # a SECOND entry keeps the dir listable and the SID match reachable,
        # so the failure is provably the entry read and not the enumeration
        other = os.path.join(d, "2026-07-18-handoff-other.md")
        with open(other, "w", encoding="utf-8") as f:
            f.write("session_id: someone-else\n")
        with mock.patch.object(inject, "project_for_cwd", return_value="proj"):
            found, _m, _h, err = handoff.check_checked(self.SID, self.wd, None)
            self.assertEqual(found, p, "positive control: readable IS found")
            self.unreadable(p)
            found, _m, _h, err = handoff.check_checked(self.SID, self.wd, None)
        self.assertIsNone(found)
        self.assertIn("PermissionError", err or "")
        self.assertIn(os.path.basename(p), err or "")

    def test_an_unreadable_REPO_handoff_file_is_unknown_too(self):
        """The third source, also unpinned until now: HANDOFF_NEXT_SESSION.md
        in the repo root is a second legal way to satisfy the contract, and its
        stat swallowed OSError into the same false absence."""
        rp = os.path.join(self.wd, handoff.REPO_FILE)
        with open(rp, "w", encoding="utf-8") as f:
            f.write("NEXT: keep going\n")
        with self.git_mock(toplevel=self.wd):
            found, _m, _h, err = handoff.check_checked("no-such-sid", self.wd, None)
            self.assertEqual(found, rp, "positive control: readable IS found")
            self.assertIsNone(err)
            os.chmod(self.wd, 0)
            self.addCleanup(lambda: os.path.isdir(self.wd)
                            and os.chmod(self.wd, 0o755))
            if os.access(os.path.join(self.wd, handoff.REPO_FILE), os.R_OK):
                self.skipTest("cannot make the repo file unstattable")
            found, _m, _h, err = handoff.check_checked("no-such-sid", self.wd, None)
        self.assertIsNone(found)
        self.assertIn("PermissionError", err or "")

    def test_the_HOOK_says_UNKNOWN_and_never_starts_blind(self):
        p = self.journal_entry()
        d = os.path.dirname(p)
        os.chmod(d, 0)
        self.addCleanup(lambda: os.path.isdir(d) and os.chmod(d, 0o755))
        if os.access(d, os.R_OK):
            self.skipTest("cannot make a directory unreadable as this user")
        payload = json.dumps({"session_id": self.SID, "cwd": self.wd,
                              "hook_event_name": "PreCompact"})
        with mock.patch.object(inject, "project_for_cwd", return_value="proj"):
            rc, out, _err = self.run_cmd(handoff.cmd_handoff,
                                         ["check", "--hook-json"], payload)
        self.assertEqual(rc, 0, "the hook stays fail-open TOTAL")
        self.assertIn("UNKNOWN", out)
        self.assertNotIn("starts blind", out)
        self.assertIn("not author a second handoff", out)

    def test_the_hollow_candidate_is_REMEMBERED_by_the_same_walk(self):
        """The half that makes deleting the second look possible, and a
        mutation proved it unpinned: dropping the remembering left all 75
        green, because every arm tested what the scan REJECTS and none tested
        what it CARRIES FORWARD.

        A shelf holding only a hollow entry must yield BOTH facts from one
        walk: nothing satisfies strictly, AND here is the entry that exists
        with these sections missing. Before the redesign that second fact came
        from re-scanning under a looser standard; now it rides the first."""
        d = os.path.join(os.environ["HELM_HOME"], "proj", "journal")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "2026-07-28-handoff-hollow.md")
        pk.atomic_write(p, "---\nmetadata:\n  type: handoff\n---\n\n"
                           "session_id: %s\n\nprose with no sections\n"
                           % self.SID)
        with mock.patch.object(inject, "project_for_cwd", return_value="proj"):
            found, loose, missing, unread = handoff.contract_state(
                self.SID, self.wd, None)
        self.assertIsNone(found, "a hollow entry must NOT satisfy strictly")
        self.assertEqual(loose, p, "…and must still be CARRIED FORWARD")
        self.assertIn("done", missing)
        self.assertIn("next", missing)
        self.assertIsNone(unread)

    def test_there_is_exactly_ONE_scan_and_no_second_look_to_lose_an_err(self):
        """THE SECOND LOOK IS GONE, and this arm replaces the one that pinned
        it. A cross-family review ended six rounds with the root cause: a SECOND SCAN GATED ON
        DISLIKING THE FIRST RESULT is a judgement with no stated rule, so it
        MANUFACTURES a case out of any input — the handler was generating the
        cases it was being patched for. Their recognition signal: if the object
        under review re-examines its input under a second, looser standard,
        stop counting rounds and delete the second look.

        So contract_state now calls check_checked ONCE. The hollow candidate is
        REMEMBERED during the same walk that looks for a satisfying entry,
        which is why `allow_hollow` no longer exists as a scan mode at all.

        The old arm asserted call_count == 2 — it pinned the defect. This one
        pins its absence, and a late-read failure cannot be lost between passes
        because there is no second pass to lose it in."""
        calls = [(None, (), None, "the only scan: /x/y.md (PermissionError)")]
        with mock.patch.object(handoff, "check_checked",
                               side_effect=calls) as m:
            payload = json.dumps({"session_id": self.SID, "cwd": self.wd,
                                  "hook_event_name": "PreCompact"})
            rc, out, _err = self.run_cmd(handoff.cmd_handoff,
                                         ["check", "--hook-json"], payload)
        self.assertEqual(m.call_count, 1,
                         "contract_state must scan ONCE — a second look gated "
                         "on disliking the first is the defect the review named")
        self.assertEqual(rc, 0)
        self.assertIn("UNKNOWN", out)
        self.assertIn("the only scan", out)

    def test_the_CLI_separates_UNKNOWN_from_UNMET_by_RC(self):
        """rc 1 already means the contract is unmet. A script must be able to
        tell that from "I could not find out", so unknown gets its own rc."""
        p = self.journal_entry()
        d = os.path.dirname(p)
        os.chmod(d, 0)
        self.addCleanup(lambda: os.path.isdir(d) and os.chmod(d, 0o755))
        if os.access(d, os.R_OK):
            self.skipTest("cannot make a directory unreadable as this user")
        with mock.patch.object(inject, "project_for_cwd", return_value="proj"):
            rc, _out, err = self.run_cmd(handoff.cmd_handoff,
                                         ["check", "--session", self.SID])
        self.assertEqual(rc, 2)
        self.assertIn("UNKNOWN", err)


class SwallowClosureAuditTest(unittest.TestCase):
    """THE ENUMERATION, pinned — so the next bare call reddens by construction.

    Three review rounds each found ONE MORE unpinned consumer of the checked
    seams, and every fix was
    correct and incomplete. That is the per-case spiral: a per-case handler can
    never be completed by adding cases, because the next case is always outside
    the set you just enumerated. The cure is to stop enumerating INSTANCES and
    pin the ENUMERATION itself.

    WHAT THIS GUARANTEES: no surface in cmd_handoff calls the collapsing
    wrappers `check`/`hollow` at all, and the checked seams have exactly the
    consumers declared below. A NEW consumer fails until it is routed through
    contract_state or declared here with a reason. WHAT IT DOES NOT: see a call
    reached through getattr, a re-export, or data flow. The bound is stated
    rather than advertised as total (DirectSpawnAuditTest's law)."""

    # Every production caller of the CHECKED seams, and why it is allowed to be
    # one. Anything else must go through contract_state.
    # (consumer, SEAM) -> why that EDGE exists. Keyed by the edge, never by the
    # consumer alone: a cross-family mutation killed the consumer-keyed
    # version with a mutation that rewired check() — the (path) view — onto
    # hollow_checked and returned None. A union over both seams cannot see that,
    # because the swap moves a name from one side of the union to the other and
    # the union is what the audit compared. WHICH SEAM a consumer uses IS the
    # contract; an audit that only asks WHO consumes them certifies a wrapper
    # that silently changed its meaning.
    DECLARED = {
        ("check", "check_checked"):
            "the collapsing back-compat wrapper — it IS the (path) view and "
            "exists so old callers do not move; it may call the checked form "
            "and drop the err, that is its whole job",
        ("hollow", "hollow_checked"):
            "the same, for the (sections) view",
        ("contract_state", "check_checked"):
            "THE surface seam: asks ONCE and folds every err plus the hollow "
            "classification the scan already computed, so no caller drops one",
        ("check_checked", "hollow_checked"):
            "the scan classifies each candidate DURING its own walk — this is "
            "the edge that lets contract_state never re-read the entry, so it "
            "is required, not merely tolerated",
    }

    SEAMS = ("check_checked", "hollow_checked")

    def _tree(self):
        # `with`, because the previous version leaked this handle on every
        # call and the audit calls it per seam.
        import ast
        with open(handoff.__file__, encoding="utf-8") as f:
            return ast.parse(f.read())

    def _calls(self, name):
        """{enclosing function -> count} for every call to `name` in handoff.

        BARE NAME *AND* ATTRIBUTE. The first version matched only ast.Name, so
        `handoff.check_checked(...)` — or any `import … as h; h.check_checked()`
        — was invisible to an audit whose entire job is knowing who reaches
        these seams. The review named that escape class alongside getattr and
        aliasing; those two cannot be resolved by an AST sweep at all, so they
        are refused outright rather than silently missed (see
        test_no_DYNAMIC_route_to_a_seam_exists)."""
        import ast
        tree = self._tree()
        out = {}
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                hit = ((isinstance(node.func, ast.Name)
                        and node.func.id == name)
                       or (isinstance(node.func, ast.Attribute)
                           and node.func.attr == name))
                if hit:
                    out[fn.name] = out.get(fn.name, 0) + 1
        return out

    def test_no_surface_calls_the_COLLAPSING_wrappers(self):
        """cmd_handoff is where every swallow was found. It must not reach the
        wrappers that discard the err — not once, not for a 'second look'."""
        for bare in ("check", "hollow"):
            callers = self._calls(bare)
            self.assertNotIn("cmd_handoff", callers,
                             "cmd_handoff calls the collapsing %s() — route it "
                             "through contract_state; that bare call is the "
                             "defect this lane closed three times" % bare)

    def _edges(self):
        """{(consumer, seam)} — every call edge into either checked seam."""
        return {(who, seam) for seam in self.SEAMS
                for who in self._calls(seam)}

    def _edge_counts(self):
        """{(consumer, seam) -> how many times}. The SET form above cannot see
        a SECOND call on an edge it already knows, and a second call is not a
        cosmetic duplicate here — it is THE defect this lane closed: two reads
        of the same entry can disagree, and the renderer prints a rewrite
        instruction for a handoff that is already fine."""
        return {(who, seam): n for seam in self.SEAMS
                for who, n in self._calls(seam).items()}

    def test_no_edge_is_walked_TWICE(self):  # noqa: VACUOUS_ASSERTION — emptiness IS the claim; assertEqual(len(counts), 4) is the unconditional control on the same sweep
        """An independent review found: "duplicate same-edge calls ... leave
        the audit green". They did — the edge set collapsed them. Each declared
        edge is ONE call, and a second one on the SAME edge is exactly the
        re-scan this lane exists to prevent, arriving where the audit was
        blindest: inside an edge it had already approved."""
        counts = self._edge_counts()
        # Positive control on the same observable: an empty count map would
        # make the emptiness below vacuous, which is the failure mode this
        # whole class keeps re-learning.
        self.assertEqual(len(counts), 4, sorted(counts))
        extra = sorted((e, n) for e, n in counts.items() if n != 1)
        self.assertEqual(extra, [], "an edge is walked more than once — a "
                         "second read of the same entry can disagree with the "
                         "first: %r" % extra)

    def test_no_DYNAMIC_route_to_a_seam_exists(self):  # noqa: VACUOUS_ASSERTION — emptiness IS the claim; assertEqual(len(self._edges()), 4) is the unconditional control on the same sweep
        """getattr and aliasing CANNOT be resolved by an AST sweep, so this
        audit refuses them instead of missing them (the review's escape list).
        A seam name reached through getattr(), or bound to another name and
        called through that, would leave every edge assertion above green
        while the call still happens."""
        import ast
        tree, bad = self._tree(), []
        # Positive control FIRST: the sweep parsed a real module and can see
        # the seams. Without it, a parse that returned nothing would report
        # "no dynamic routes" about a file it never read.
        self.assertEqual(len(self._edges()), 4)
        for node in ast.walk(tree):
            # getattr(x, "check_checked") — a route no static reader can follow
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "getattr"
                    and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant)
                    and node.args[1].value in self.SEAMS):
                bad.append("getattr(..., %r)" % node.args[1].value)
            # alias = check_checked  (bare reference, not a call)
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Name) \
                    and node.value.id in self.SEAMS:
                bad.append("alias binding of %s" % node.value.id)
        self.assertEqual(bad, [], "a seam is reachable by a route this audit "
                         "cannot follow — call it directly or the edge map "
                         "below is fiction: %r" % bad)

    def test_the_checked_seams_have_exactly_the_declared_EDGES(self):
        edges = self._edges()
        # positive control: the sweep found the graph at all. Without this, a
        # parser that silently returned nothing would make BOTH set-diffs empty
        # and this audit would certify every future rewiring.
        self.assertEqual(len(edges), 4, "expected the four declared edges; the "
                         "sweep found %r" % sorted(edges))
        undeclared = sorted(edges - set(self.DECLARED))
        self.assertEqual(undeclared, [], "a consumer reached a seam it does "
                         "not declare — route it through contract_state, or "
                         "declare the EDGE here with a reason: %r" % undeclared)
        stale = sorted(set(self.DECLARED) - edges)
        self.assertEqual(stale, [], "these edges are gone — drop them from "
                         "DECLARED so the list cannot rot into a rule nobody "
                         "enforces: %r" % stale)
        for edge, why in self.DECLARED.items():
            self.assertGreater(len(why), 20, edge)

    def test_a_consumer_SWAPPING_seams_is_caught(self):
        """The review's mutation, pinned as its own arm so the reason survives the
        next refactor: rewiring check() — the (path) view — onto hollow_checked
        keeps it a consumer of "the checked seams" and passed the union audit.
        The edge audit must call that undeclared in BOTH directions: the new
        edge appears AND the declared one goes missing."""
        swapped = {e for e in self._edges() if e[0] != "check"}
        swapped.add(("check", "hollow_checked"))
        self.assertTrue(sorted(swapped - set(self.DECLARED)),
                        "the swapped edge must read as undeclared")
        self.assertIn(("check", "check_checked"),
                      set(self.DECLARED) - swapped,
                      "and the abandoned edge must read as stale — one "
                      "direction alone would let a swap hide in the other")

    def test_the_classification_RIDES_the_scan_and_is_never_re_read(self):
        """A cross-family review caught this: my previous pass claimed to eliminate
        the renderers' re-read and only MOVED it — out of the two renderers and
        into contract_state, where it was equally unpinned: deleting that edge
        left the audit and 74 handoff tests green.

        A SECOND READ IS NOT A TIDINESS QUESTION, it is a race. The file can
        change between the two reads, and an entry COMPLETED in that window
        yields an empty missing-set, so the renderer prints "carries no " with
        nothing named — a REWRITE instruction for a handoff that is already
        fine. Pinned structurally: contract_state must not call hollow_checked
        at all, because check_checked already computed that answer while
        deciding whether to skip the entry."""
        callers = self._calls("hollow_checked")
        self.assertNotIn("contract_state", callers,
                         "contract_state re-reads the entry to classify it — "
                         "check_checked already has that answer from the scan; "
                         "a second read can disagree with the first")
        # positive control: the scan itself DOES classify, or the assertion
        # above would pass by the classification having vanished entirely
        self.assertIn("check_checked", callers,
                      "check_checked must classify during its own scan")

    def test_the_SEAMS_NEVER_LEAVE_THIS_MODULE(self):  # noqa: VACUOUS_ASSERTION — the EMPTY stray list IS the claim; assertGreater(scanned, 10) and assertIn(handoff.py) are unconditional controls on the same sweep
        """A cross-family review caught this: every arm above parses helm/handoff.py
        and ONLY that file, so the edge map describes ONE module while claiming
        to describe the seams. The day any other module reaches them the map
        silently stops being true AND STAYS GREEN — the same blindness as the
        duplicate-edge case, one scope out.

        Measured zero at this tip, which is exactly when to pin it: the arm
        cannot be satisfied by an incident, only by a future one being caught.
        Matching the NAME rather than a call node is deliberate — it catches
        the attribute form, an import-as alias, and a getattr constant alike,
        which are precisely the routes an AST call-sweep cannot follow."""
        import os
        pkg = os.path.dirname(handoff.__file__)
        mine = os.path.realpath(handoff.__file__)
        stray, scanned, nested = [], 0, 0
        # os.WALK, NOT LISTDIR. The first version read only the package's TOP
        # LEVEL, so the 42 modules under helm/work, helm/configs and their
        # siblings were invisible — the review proved it by planting
        # `from ..handoff import check_checked` in helm/work/__init__.py and
        # watching this arm stay GREEN. I had fixed "one module" to "one
        # DIRECTORY" and stopped one scope short of the claim I was making.
        for root, dirs, names in os.walk(pkg):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in sorted(names):
                if not name.endswith(".py"):
                    continue
                full = os.path.realpath(os.path.join(root, name))
                if full == mine:
                    continue
                scanned += 1
                if os.path.dirname(full) != os.path.realpath(pkg):
                    nested += 1
                with open(full, encoding="utf-8") as f:
                    body = f.read()
                for seam in self.SEAMS:
                    if seam in body:
                        stray.append("%s names %s"
                                     % (os.path.relpath(full, pkg), seam))
        # TWO controls, because the first version had ONE and it passed while
        # blind: the sweep must have read a real package AND must have
        # DESCENDED into subpackages. `scanned > 10` alone was satisfied by
        # the top level, which is exactly how the gap survived.
        self.assertGreater(scanned, 10, "the sweep read almost nothing")
        self.assertGreater(nested, 5, "the sweep never left the top level — "
                           "every helm/*/ subpackage would be invisible")
        self.assertTrue(os.path.exists(mine))
        self.assertEqual(stray, [], "a checked seam is named OUTSIDE its own "
                         "module — every edge assertion in this class parses "
                         "only %s, so the map no longer describes the "
                         "package: %r" % (mine, stray))

    def test_the_audit_SEES_a_planted_bare_call(self):
        """The control. A sweep that silently stopped parsing would certify a
        clean module forever, which is the same green as a clean one."""
        import ast
        planted = ast.parse("def cmd_handoff(a):\n    return check(a)\n")
        found = [n.name for n in ast.walk(planted)
                 if isinstance(n, ast.FunctionDef)
                 for c in ast.walk(n)
                 if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                 and c.func.id == "check"]
        self.assertEqual(found, ["cmd_handoff"])


class LatestEntryTest(CheckTest):
    """`attribute_entry` / `latest_entry` — what the resume leg reads to know
    what to resume ON, and WHOSE it is.

    The shelf is per-PROJECT and every seat on the project writes to it, so
    "the freshest entry" and "your entry" are different questions. This suite
    exists because the first version answered the second with the first: the
    fixture below used to name its foreign row `theirs` and then ASSERT that
    `latest_entry` returned it."""

    def shelf(self, *rows):
        """(name, sid, seat, next_line, age_s) … -> the entries on disk."""
        d = os.path.join(os.environ["HELM_HOME"], "proj", "journal")
        os.makedirs(d, exist_ok=True)
        for name, sid, seat, next_line, age in rows:
            p = os.path.join(d, name)
            pk.atomic_write(p, "\n".join([
                "---", "metadata:", "  session_id: " + sid,
                "  seat: " + seat, "  next: " + next_line, "---", "", "x", ""]))
            t = time.time() - age
            os.utime(p, (t, t))

    def test_a_NEWER_foreign_entry_never_outranks_your_own(self):
        """The live near-miss, at the policy seam. Ordering by
        freshness alone hands the resuming seat whichever seat compacted last,
        and the caller prints "Your own handoff" over it."""
        os.environ["HELM_CHAT_NAME"] = "helm-claude"
        self.shelf(("2026-07-28-handoff-aaaabbbb.md", self.SID,
                    "helm-claude", "mine", 60),
                   ("2026-07-28-handoff-ffff0000.md", "other",
                    "helm-claude-2", "theirs", 5))
        p, meta, why = handoff.attribute_entry("proj", sid=self.SID)
        self.assertEqual(meta["next"], "mine")
        self.assertEqual(why, handoff.BY_SID)
        self.assertIn("session", why)     # positive control on `why` itself:
        self.assertIn("aaaabbbb", p)      # a constant-vs-constant equality is
        # satisfiable by two empty strings, so the proof label is also pinned
        # against a literal the reader can read.

    def test_a_FORKED_sid_is_still_yours_by_seat(self):
        """What the sid-as-preference fallback was protecting, kept without the
        lottery: the id no longer matches, the seat still does."""
        os.environ["HELM_CHAT_NAME"] = "helm-claude"
        self.shelf(("2026-07-28-handoff-aaaabbbb.md", "pre-fork-id",
                    "helm-claude", "mine", 60))
        _p, meta, why = handoff.attribute_entry("proj", sid="post-fork-id")
        self.assertEqual((meta["next"], why), ("mine", handoff.BY_SEAT))

    def test_the_NEWEST_of_your_own_wins_even_when_an_older_one_matches_the_sid(self):
        """Both entries are honestly this seat's, which is reachable in exactly
        the case the seat key exists for: a forked session id writes a SECOND
        file (the name carries sid8), so the shelf holds a pre-fork entry and a
        post-fork one. Ranking by proof TYPE — sid above seat — would resume
        the seat on the older of its own plans."""
        os.environ["HELM_CHAT_NAME"] = "helm-claude"
        self.shelf(("2026-07-28-handoff-aaaabbbb.md", self.SID,
                    "helm-claude", "the plan I had an hour ago", 3600),
                   ("2026-07-28-handoff-ccccdddd.md", "post-fork-id",
                    "helm-claude", "the plan I have now", 5))
        _p, meta, why = handoff.attribute_entry("proj", sid=self.SID)
        self.assertEqual(meta["next"], "the plan I have now")
        self.assertEqual(why, handoff.BY_SEAT)
        self.assertIn("seat", why)        # positive control on `why` itself

    def test_a_shelf_of_only_foreign_entries_is_FOREIGN_ONLY_not_empty(self):
        """"nothing was written" and "something was written and it is not
        yours" are different facts, and the caller says a different sentence
        for each. Collapsing them is how a reader reports a false absence."""
        os.environ["HELM_CHAT_NAME"] = "helm-claude"
        self.shelf(("2026-07-28-handoff-ffff0000.md", "other",
                    "helm-claude-2", "theirs", 5))
        self.assertEqual(handoff.attribute_entry("proj", sid=self.SID),
                         (None, None, handoff.FOREIGN_ONLY))
        # …and the back-compat 2-tuple still collapses to a plain miss
        self.assertEqual(handoff.latest_entry("proj", sid=self.SID),
                         (None, None))

    def test_an_UNSTAMPED_entry_is_unattributed_not_foreign(self):
        """A pre-seat-key entry proves nothing in either direction. It is
        still refused — the sentence built from it asserts ownership — but with
        its own reason, and it must NOT be reported as another seat's."""
        os.environ["HELM_CHAT_NAME"] = "helm-claude"
        self.shelf(("2026-07-28-handoff-ffff0000.md", "other", "", "x", 5))
        self.assertEqual(handoff.attribute_entry("proj", sid=self.SID),
                         (None, None, handoff.UNATTRIBUTED))

    def test_an_unstamped_entry_is_still_yours_when_the_SID_matches(self):
        """The stamp is a second proof, never a new requirement: entries
        written before the seat key existed still resume their own author."""
        os.environ["HELM_CHAT_NAME"] = "helm-claude"
        self.shelf(("2026-07-28-handoff-aaaabbbb.md", self.SID, "", "mine", 5))
        _p, meta, why = handoff.attribute_entry("proj", sid=self.SID)
        self.assertEqual((meta["next"], why), ("mine", handoff.BY_SID))

    def test_the_floor_excludes_everything_beneath_it(self):
        os.environ["HELM_CHAT_NAME"] = "helm-claude"
        self.shelf(("2026-07-28-handoff-aaaabbbb.md", self.SID,
                    "helm-claude", "mine", 60))
        self.assertEqual(handoff.attribute_entry("proj", floor=time.time()),
                         (None, None, handoff.NONE_FRESH))

    def test_an_UNREADABLE_entry_is_never_reported_as_none_written(self):
        """The review's fix, reproduced exactly: a fresh
        matching handoff at chmod 000 returned (None, None, none-fresh), and
        the resume line then said NO HANDOFF WAS WRITTEN. It was written; it
        could not be READ — and it was this seat's own.

        Which is the defect this whole lane exists to close, committed inside
        the fix for it: a true reading (no parseable entry) bound to a false
        claim (none exists). An unreadable entry cannot be counted fresh, so
        it fell straight through to the emptiest answer available."""
        os.environ["HELM_CHAT_NAME"] = "helm-claude"
        self.shelf(("2026-07-28-handoff-aaaabbbb.md", self.SID,
                    "helm-claude", "mine", 5))
        p = os.path.join(os.environ["HELM_HOME"], "proj", "journal",
                         "2026-07-28-handoff-aaaabbbb.md")
        # positive control FIRST: readable, this entry resumes normally.
        _p, meta, why = handoff.attribute_entry("proj", sid=self.SID)
        self.assertEqual(meta["next"], "mine")
        self.assertEqual(why, handoff.BY_SID)
        os.chmod(p, 0)
        # tolerant: cleanups run AFTER tearDown rmtree's the tmp home
        self.addCleanup(lambda: os.path.exists(p) and os.chmod(p, 0o644))
        if os.access(p, os.R_OK):        # root ignores the mode bit
            self.skipTest("cannot make a file unreadable as this user")
        self.assertEqual(handoff.attribute_entry("proj", sid=self.SID),
                         (None, None, handoff.UNREADABLE))

    def test_an_unreadable_entry_never_hides_a_handoff_this_seat_CAN_read(self):
        """Unreadable outranks the other empty answers, but it must not
        outrank a real one — the seat resumes on what it can prove and hears
        about the rest only when there is nothing to resume on."""
        os.environ["HELM_CHAT_NAME"] = "helm-claude"
        self.shelf(("2026-07-28-handoff-aaaabbbb.md", self.SID,
                    "helm-claude", "mine", 5),
                   ("2026-07-28-handoff-ffff0000.md", "other", "", "x", 5))
        bad = os.path.join(os.environ["HELM_HOME"], "proj", "journal",
                           "2026-07-28-handoff-ffff0000.md")
        os.chmod(bad, 0)
        self.addCleanup(lambda: os.path.exists(bad) and os.chmod(bad, 0o644))
        if os.access(bad, os.R_OK):
            self.skipTest("cannot make a file unreadable as this user")
        _p, meta, why = handoff.attribute_entry("proj", sid=self.SID)
        self.assertEqual(meta["next"], "mine")
        self.assertEqual(why, handoff.BY_SID)
        self.assertIn("session", why)

    def test_the_SHELF_ITSELF_has_three_arms_readable_unreadable_absent(self):
        """The review's second fix, and it is the SAME
        false absence one directory up. `glob.glob` on a chmod-000 directory
        swallows the PermissionError and returns [] — byte-identical to an
        empty shelf — so an unreadable JOURNAL DIR reported as "no handoff was
        written" exactly the way an unreadable ENTRY did before the first fix.
        glob's silence is what hid it, which is why enumeration is now explicit.

        Three arms, because two of them are the ones that get confused:
        readable-with-an-entry, present-but-unreadable, and absent."""
        os.environ["HELM_CHAT_NAME"] = "helm-claude"
        self.shelf(("2026-07-28-handoff-aaaabbbb.md", self.SID,
                    "helm-claude", "mine", 5))
        d = os.path.join(os.environ["HELM_HOME"], "proj", "journal")
        # ARM 1 — readable: the entry resumes. Positive control for both below.
        _p, meta, why = handoff.attribute_entry("proj", sid=self.SID)
        self.assertEqual(meta["next"], "mine")
        self.assertEqual(why, handoff.BY_SID)
        # ARM 2 — present but UNREADABLE: never "none written".
        # try/finally, NOT addCleanup: cleanups run AFTER tearDown, and
        # tearDown rmtree's with ignore_errors=True, which cannot remove an
        # unlistable directory. An assertion failure inside this arm would
        # therefore leave a 000 directory behind for the rest of the process.
        # The mode must come back before this method returns, on every path.
        os.chmod(d, 0)
        try:
            if os.access(d, os.R_OK):
                self.skipTest("cannot make a directory unreadable as this user")
            self.assertEqual(handoff.attribute_entry("proj", sid=self.SID),
                             (None, None, handoff.UNREADABLE))
        finally:
            if os.path.isdir(d):
                os.chmod(d, 0o755)
        # ARM 3 — ABSENT: an honest absence, and it must NOT read as unreadable
        # or the fix would cry wolf on every project that has no shelf yet.
        shutil.rmtree(d)
        self.assertEqual(handoff.attribute_entry("proj", sid=self.SID),
                         (None, None, handoff.NO_SHELF))

    def test_a_file_that_VANISHES_mid_scan_is_not_an_unreadable_shelf(self):
        """glob-then-stat is two syscalls, so an entry can be REMOVED between
        them. That is a routine race — a cleanup, a reaped tmp home — not a
        handoff being hidden, and calling it unreadable would make an ordinary
        deletion announce UNKNOWN at every compaction. Gone and unreadable are
        different facts in the same `except` clause, which is why the narrow
        FileNotFoundError arm comes first."""
        os.environ["HELM_CHAT_NAME"] = "helm-claude"
        self.shelf(("2026-07-28-handoff-ffff0000.md", "other",
                    "helm-claude-2", "theirs", 5))
        # positive control: with the file present this shelf reads FOREIGN_ONLY
        self.assertEqual(handoff.attribute_entry("proj", sid=self.SID)[2],
                         handoff.FOREIGN_ONLY)
        real = os.stat

        def vanished(path, *a, **k):
            if str(path).endswith("ffff0000.md"):
                raise FileNotFoundError(2, "No such file or directory", str(path))
            return real(path, *a, **k)

        with mock.patch("os.stat", side_effect=vanished):
            reason = handoff.attribute_entry("proj", sid=self.SID)[2]
        self.assertEqual(handoff.NONE_FRESH, "none-fresh")   # pinned literal
        self.assertEqual(reason, handoff.NONE_FRESH)

    def test_a_file_with_NO_frontmatter_is_not_reported_as_unreadable(self):
        """`parse_simple_frontmatter` returns a defaults dict for a file it can
        READ but that carries no frontmatter, and None only when the open
        raised. Conflating the two would invert the fix: every stray .md on the
        shelf would announce an unreadable shelf."""
        d = os.path.join(os.environ["HELM_HOME"], "proj", "journal")
        os.makedirs(d, exist_ok=True)
        pk.atomic_write(os.path.join(d, "2026-07-28-handoff-plain.md"),
                        "just prose, no fence\n")
        self.assertEqual(handoff.UNATTRIBUTED, "unattributed")  # pinned literal
        self.assertEqual(handoff.attribute_entry("proj", sid=self.SID)[2],
                         handoff.UNATTRIBUTED)

    def test_no_shelf_is_not_an_error(self):
        self.assertEqual(handoff.latest_entry("nosuchproject"), (None, None))
        self.assertEqual(handoff.latest_entry(None), (None, None))
        self.assertEqual(handoff.attribute_entry(None)[2], handoff.NO_SHELF)

    def test_write_entry_STAMPS_the_declared_seat_and_only_that(self):
        """The stamp is `own_name()` — the DECLARED name — never the auto-name
        floor, which bottoms out at the bare family word every claude seat
        shares. A stamp five seats can satisfy is not attribution."""
        os.environ["HELM_CHAT_NAME"] = "helm-claude"
        p, _missing = handoff.write_entry(PROSE, "proj", self.SID)
        with open(p, encoding="utf-8") as f:
            self.assertIn("seat: helm-claude", f.read())
        os.environ.pop("HELM_CHAT_NAME")
        p2, _m2 = handoff.write_entry(PROSE, "proj", "other-session-id")
        with open(p2, encoding="utf-8") as f:
            body = f.read()
        self.assertIn("seat: \n", body, "an undeclared writer stamps nothing, "
                      "which reads as unproven rather than as a false match")


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
