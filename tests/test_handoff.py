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
import fcntl
import glob
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-home-", var="HELM_HOME")

from helm import cli, handoff, hooks, inject, pk, record, resumeturn  # noqa: E402

# HELM_CHAT_NAME is SET by the attribution tests, so it belongs here: setUp
# pops this tuple and tearDown restores it, which means a key that is set but
# unlisted is never cleaned and leaks into every later test in the process
# (measured f45b3c3 — a sibling test was green only because it drank a leaked
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
        `gate run --help` joining the FIFO (#161).

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
        """The measured shape (2026-08-02): a DONE heading whose qualifier has
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
        empty done: (word-run rung, 2026-08-02) now lands a populated one."""
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


class CheckBase(HandoffBase):
    """The handoff-check fixture: a real transcript for the PreCompact
    payload (`self.tp`), `journal_entry` for a session's journal file, and
    `hook_check`, which drives `helm handoff check --hook-json` with a
    payload.

    THIS CLASS HOLDS NO `test_*` METHOD, and that is its contract. unittest
    collects every inherited `test_*` again under each subclass's own id, so
    an arm written here runs once per subclass. A class that needs this
    fixture subclasses THIS class; its arms go in the subclass."""

    SID = "aaaabbbb-1111-2222-3333-444455556666"

    def setUp(self):
        super().setUp()
        self.tp = self.transcript_fixture()

    def transcript_fixture(self):
        """A real transcript for every PreCompact payload, as the harness
        always sends one: a single turn record, so the position the
        producer records is a non-zero length and a real inode."""
        tp = os.path.join(self.tmp, "transcript.jsonl")
        with open(tp, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "message": {"role": "user",
                                                            "content": "the first turn"}}) + "\n")
        return tp

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

    def hook_check(self, payload):
        with self.git_mock():
            return self.run_cmd(handoff.cmd_handoff, ["check", "--hook-json"],
                                payload)


class CheckTest(CheckBase):
    """The handoff-check arms, on CheckBase's fixture.

    A subclass of THIS class runs every arm below again under its own id,
    which is right only for one that changes the fixture those arms read. A
    class that only needs the fixture subclasses CheckBase."""

    def test_seat_fallback_fires_for_own_session_not_foreign(self):  # noqa: VACUOUS_ASSERTION — ARM 1 (assertEqual above) is the unconditional positive control proving check_checked DOES find entries here; ARM 2's assertIsNone is the other pole of the SAME observable on the SAME fixture
        """task/412 regression: seat-key fallback finds an old-sid entry
        for our own session (compaction fork), but foreign --session refuses.
        Both broken 38cd (foreign accepted) and df6 (fork rejected) passed
        11,180-test gates — this pin closes the blind spot."""
        d = os.path.join(os.environ["HELM_HOME"], "proj", "journal")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "2026-07-19-handoff-old.md")
        # A proper YAML-frontmatter entry matching write_entry()'s
        # metadata-nested shape. The old session id differs from our
        # current SID (compaction fork: new session, old entry), and
        # the seat stamp under metadata: matches.
        with open(p, "w", encoding="utf-8") as f:
            f.write("---\n")
            f.write("name: 2026-07-19-handoff-old\n")
            f.write('description: "handoff: verify"\n')
            f.write("metadata:\n")
            f.write("  session_id: old-compacted-session-id\n")
            f.write("  seat: seat-a\n")
            f.write("---\n")
            f.write("\n## DONE\n- shipped it\n\n## REMAINING\n- the hook\n"
                    "\n## NEXT\n- verify\n")
        os.environ["HELM_CHAT_NAME"] = "seat-a"
        # home.session_id() must return self.SID so the foreign gate
        # fires on an explicit different sid. Mock it rather than
        # setting env vars that Cede to the real CLAUDE_CODE_SESSION_ID.
        with mock.patch.object(handoff.home, "session_id",
                               return_value=self.SID), \
                self.git_mock(), \
                mock.patch.object(inject, "project_for_cwd",
                                  return_value="proj"):
            # ARM 1: our session, old entry's sid, seat match — fallback
            # finds the entry (compaction fork: new sid, old seat stamp).
            found, _m, _h, _e = handoff.check_checked(self.SID, self.wd, None)
            self.assertEqual(found, p,
                             "own session + old-sid entry + seat match = "
                             "found via seat-key fallback (compaction fork)")
            # ARM 2: foreign --session, same old entry, seat match — MUST
            # NOT find it. The seat fallback is a compaction-fork safety
            # net, not a --session override.
            found, _m, _h, _e = handoff.check_checked(
                "foreign-session-id", self.wd, None)
            self.assertIsNone(found,
                              "foreign --session + old-sid entry + seat match "
                              "= REJECTED (seat fallback must not override "
                              "explicit --session)")

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

    def test_same_seat_evidence_is_not_a_handoff_but_legacy_handoff_is(self):  # noqa: VACUOUS_ASSERTION — legacy entry is the positive control
        """task/2676: seat fallback proves ownership, never artifact type."""
        d = os.path.join(os.environ["HELM_HOME"], "proj", "journal")
        os.makedirs(d, exist_ok=True)
        evidence = os.path.join(d, "2026-07-20-handoff-proof.md")
        with open(evidence, "w", encoding="utf-8") as f:
            f.write("---\nmetadata:\n  type: evidence\n  seat: seat-a\n---\n"
                    "\nDONE: x\nREMAINING: y\nNEXT: z\n")
        os.environ["HELM_CHAT_NAME"] = "seat-a"
        with mock.patch.object(handoff.home, "session_id",
                               return_value=self.SID), \
                mock.patch.object(inject, "project_for_cwd",
                                  return_value="proj"):
            found, _m, _h, err = handoff.check_checked(
                self.SID, self.wd, None)
            self.assertIsNone(found)
            self.assertIsNone(err)
            legacy = self.journal_entry()
            found, _m, _h, err = handoff.check_checked(
                self.SID, self.wd, None)
        self.assertEqual(found, legacy)
        self.assertIsNone(err)

    def test_explicit_evidence_at_eof_never_falls_back_to_legacy_name(self):  # noqa: VACUOUS_ASSERTION — rewritten handoff is the positive control
        """A valid closing fence needs no trailing body or newline."""
        d = os.path.join(os.environ["HELM_HOME"], "proj", "journal")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "2026-07-20-handoff-proof.md")
        with open(p, "w", encoding="utf-8") as f:
            f.write("---\nmetadata:\n  type: evidence\n"
                    "  session_id: %s\n  seat: seat-a\n---" % self.SID)
        with mock.patch.object(inject, "project_for_cwd",
                               return_value="proj"):
            found, _m, _h, err = handoff.check_checked(
                self.SID, self.wd, None)
        self.assertIsNone(found)
        self.assertIsNone(err)
        with open(p, "w", encoding="utf-8") as f:
            f.write("---\nmetadata:\n  type: handoff\n"
                    "  session_id: %s\n  seat: seat-a\n---\n"
                    "DONE: x\nREMAINING: y\nNEXT: z\n" % self.SID)
        with mock.patch.object(inject, "project_for_cwd",
                               return_value="proj"):
            found, _m, _h, err = handoff.check_checked(
                self.SID, self.wd, None)
        self.assertEqual(found, p)
        self.assertIsNone(err)

    def test_an_entry_replaced_during_the_scan_is_unknown(self):
        """Type, owner and sections must come from one stable snapshot."""
        d = os.path.join(os.environ["HELM_HOME"], "proj", "journal")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "2026-07-20-handoff-race.md")
        original = ("---\nmetadata:\n  type: handoff\n"
                    "  session_id: old-session\n  seat: seat-a\n---\n"
                    "DONE: x\nREMAINING: y\nNEXT: z\n")
        with open(p, "w", encoding="utf-8") as f:
            f.write(original)
        classify = handoff._journal_handoff

        def replace(path, text):
            answer = classify(path, text)
            swap = path + ".swap"
            with open(swap, "w", encoding="utf-8") as f:
                f.write("---\nmetadata:\n  type: evidence\n"
                        "  session_id: old-session\n  seat: seat-a\n---\n")
            os.replace(swap, path)
            return answer

        os.environ["HELM_CHAT_NAME"] = "seat-a"
        with mock.patch.object(handoff.home, "session_id",
                               return_value=self.SID), \
                mock.patch.object(inject, "project_for_cwd",
                                  return_value="proj"), \
                mock.patch.object(handoff, "_journal_handoff",
                                  side_effect=replace), \
                mock.patch.object(handoff, "_matches_own_seat",
                                  wraps=handoff._matches_own_seat) as seat_read, \
                mock.patch.object(handoff, "hollow_checked",
                                  wraps=handoff.hollow_checked) as hollow_read:
            found, _m, _h, err = handoff.check_checked(
                self.SID, self.wd, None)
        self.assertIsNone(found)
        self.assertIn("changed during handoff scan", err or "")
        self.assertEqual(seat_read.call_args.args[2], original)
        self.assertEqual(hollow_read.call_args.args[1], original)

    def test_same_inode_same_size_rewrite_with_restored_mtime_is_unknown(self):  # noqa: VACUOUS_ASSERTION — changed-path error is the observable
        """Identity includes ctime, the only remaining witness for this rewrite.

        The hostile writer changes a valid handoff to a non-handoff in place,
        preserves inode and byte count, then restores mtime exactly. Without
        ctime in the identity tuple, the scan returns the classification of
        bytes that are no longer at the path.
        """
        d = os.path.join(os.environ["HELM_HOME"], "proj", "journal")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "2026-07-20-handoff-in-place.md")
        original = ("---\nmetadata:\n  type: handoff\n"
                    "  session_id: %s\n---\n"
                    "DONE: x\nREMAINING: y\nNEXT: z\n" % self.SID)
        replacement = original.replace("type: handoff", "type: noteoff")
        self.assertEqual(len(replacement), len(original))
        pk.atomic_write(p, original)
        identity = os.stat(p)
        classify = handoff.hollow_checked
        rewrote = []

        def rewrite_in_place(path, text=None):
            answer = classify(path, text)
            if not rewrote:
                with open(path, "r+", encoding="utf-8") as f:
                    f.write(replacement)
                    f.truncate()
                os.utime(path, ns=(identity.st_atime_ns, identity.st_mtime_ns))
                after = os.stat(path)
                self.assertEqual((after.st_dev, after.st_ino, after.st_size,
                                  after.st_mtime_ns),
                                 (identity.st_dev, identity.st_ino,
                                  identity.st_size, identity.st_mtime_ns))
                self.assertNotEqual(after.st_ctime_ns, identity.st_ctime_ns)
                rewrote.append(True)
            return answer

        with mock.patch.object(inject, "project_for_cwd", return_value="proj"), \
                mock.patch.object(handoff, "hollow_checked",
                                  side_effect=rewrite_in_place):
            found, _m, _h, err = handoff.check_checked(
                self.SID, self.wd, None)
        self.assertTrue(rewrote)
        self.assertIsNone(found)
        self.assertIn("changed during handoff scan", err or "")

    def test_frontmatter_type_uses_the_store_scalar_grammar(self):
        """A hash is data here, not a YAML comment."""
        path = "2026-07-20-handoff-proof.md"
        for value in ("handoff#draft", '"handoff # draft"'):
            with self.subTest(value):
                text = "---\nmetadata:\n  type: %s\n---\n" % value
                self.assertFalse(handoff._journal_handoff(path, text))
        self.assertTrue(handoff._journal_handoff(
            path, '---\nmetadata:\n  type: "handoff"\n---\n'))

    def test_frontmatter_keys_match_store_case_and_last_wins_semantics(self):
        """The snapshot reader is a second door onto pk's frontmatter grammar.

        Mixed-case duplicate keys are hostile on purpose: the first values make
        the entry evidence, foreign-session and foreign-seat, while the final
        values make it this authored handoff. A first-wins or case-sensitive
        transcription disagrees with ``pk.parse_simple_frontmatter`` on every
        identity field that decides admission.
        """
        d = os.path.join(os.environ["HELM_HOME"], "proj", "journal")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "2026-07-20-handoff-case.md")
        text = ("---\nmetadata:\n"
                "  type: evidence\n  TYPE: handoff\n"
                "  session_id: someone-else\n  SESSION_ID: %s\n"
                "  seat: seat-b\n  SEAT: seat-a\n---\n"
                "DONE: x\nREMAINING: y\nNEXT: z\n" % self.SID)
        pk.atomic_write(p, text)
        expected = pk.parse_simple_frontmatter(
            p, {"type": "", "session_id": "", "seat": ""})
        self.assertEqual(expected, {"type": "handoff", "session_id": self.SID,
                                    "seat": "seat-a"})
        for key in ("type", "session_id", "seat"):
            with self.subTest(key):
                self.assertEqual(handoff._frontmatter_value(text, key),
                                 (True, expected[key]))
        self.assertTrue(handoff._journal_handoff(p, text))
        self.assertEqual(handoff._journal_session(text), (True, self.SID))
        self.assertTrue(handoff._matches_own_seat(p, "SEAT-A", text))
        self.assertEqual(handoff._hollow_text(text), ())
        with mock.patch.object(inject, "project_for_cwd", return_value="proj"):
            found, _m, _h, err = handoff.check_checked(
                self.SID, self.wd, None)
        self.assertEqual(found, p)
        self.assertIsNone(err)

    def test_uppercase_or_last_duplicate_evidence_type_never_falls_back_to_name(self):  # noqa: VACUOUS_ASSERTION — each negative is anchored by the store-parser and value assertions on the same bytes
        """The false-success direction: explicit evidence overrides the name.

        Both files have the exact legacy handoff filename and complete sections,
        so ignoring uppercase keys or taking the first duplicate would admit
        them. The shared store grammar says their final explicit type is
        evidence, which must refuse compatibility fallback.
        """
        d = os.path.join(os.environ["HELM_HOME"], "proj", "journal")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "2026-07-20-handoff-proof.md")
        for name, type_rows in (
                ("uppercase", "  TYPE: evidence\n"),
                ("last duplicate", "  type: handoff\n  TYPE: evidence\n")):
            with self.subTest(name):
                text = ("---\nmetadata:\n%s  session_id: %s\n---\n"
                        "DONE: x\nREMAINING: y\nNEXT: z\n"
                        % (type_rows, self.SID))
                pk.atomic_write(p, text)
                parsed = pk.parse_simple_frontmatter(p, {"type": ""})
                self.assertEqual(parsed["type"], "evidence")
                self.assertEqual(handoff._frontmatter_value(text, "type"),
                                 (True, "evidence"))
                self.assertFalse(handoff._journal_handoff(p, text))
                with mock.patch.object(inject, "project_for_cwd",
                                       return_value="proj"):
                    found, _m, _h, err = handoff.check_checked(
                        self.SID, self.wd, None)
                self.assertIsNone(found)
                self.assertIsNone(err)

    def test_typed_session_match_is_exact_not_a_body_substring(self):  # noqa: VACUOUS_ASSERTION — rewritten session is the positive control
        d = os.path.join(os.environ["HELM_HOME"], "proj", "journal")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "2026-07-20-handoff-proof.md")
        with open(p, "w", encoding="utf-8") as f:
            f.write("---\nmetadata:\n  type: handoff\n"
                    "  session_id: old-session\n---\n"
                    "DONE: x\nREMAINING: mentions %s\nNEXT: z\n" % self.SID)
        with mock.patch.object(inject, "project_for_cwd",
                               return_value="proj"):
            found, _m, _h, err = handoff.check_checked(
                self.SID, self.wd, None)
        self.assertIsNone(found)
        self.assertIsNone(err)
        with open(p, "w", encoding="utf-8") as f:
            f.write("---\nmetadata:\n  type: handoff\n"
                    "  session_id: %s\n---\n"
                    "DONE: x\nREMAINING: y\nNEXT: z\n" % self.SID)
        with mock.patch.object(inject, "project_for_cwd",
                               return_value="proj"):
            found, _m, _h, err = handoff.check_checked(
                self.SID, self.wd, None)
        self.assertEqual(found, p)
        self.assertIsNone(err)

    def test_freshness_comes_from_the_opened_snapshot(self):  # noqa: VACUOUS_ASSERTION — absent result is checked below
        d = os.path.join(os.environ["HELM_HOME"], "proj", "journal")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "2026-07-20-handoff-proof.md")
        stale = p + ".stale"
        body = ("---\nmetadata:\n  type: handoff\n"
                "  session_id: %s\n---\n"
                "DONE: x\nREMAINING: y\nNEXT: z\n" % self.SID)
        for path in (p, stale):
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)
        old = time.time() - (handoff.FRESH_H + 1) * 3600
        os.utime(stale, (old, old))
        real_open, swapped = open, []

        def replace_before_open(path, *args, **kwargs):
            if os.fspath(path) == p and not swapped:
                os.replace(stale, p)
                swapped.append(True)
            return real_open(path, *args, **kwargs)

        with mock.patch.object(inject, "project_for_cwd",
                               return_value="proj"), \
                mock.patch("builtins.open", side_effect=replace_before_open):
            found, _m, _h, err = handoff.check_checked(
                self.SID, self.wd, None)
        self.assertTrue(swapped)
        self.assertIsNone(found)
        self.assertIsNone(err)

    def test_stale_path_stat_cannot_turn_an_unreadable_entry_into_absence(self):  # noqa: VACUOUS_ASSERTION — unreadable-path error is the observable
        """The unreadable fd has no trustworthy freshness classification.

        The pathname currently looks stale, but using that pre-open stat to
        dismiss the failure races a fresh replacement before open. UNKNOWN is
        the only sound answer; the following test proves the replacement arm.
        """
        d = os.path.join(os.environ["HELM_HOME"], "proj", "journal")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "2026-07-20-handoff-unreadable.md")
        pk.atomic_write(p, "unreadable bytes\n")
        floor = time.time() - 60
        os.utime(p, (floor - 60, floor - 60))
        self.assertLess(os.stat(p).st_mtime, floor,
                        "positive control: the pathname appears stale")
        real_open = open

        def unreadable(path, *args, **kwargs):
            if os.fspath(path) == p:
                raise PermissionError(13, "denied", p)
            return real_open(path, *args, **kwargs)

        with mock.patch.object(inject, "project_for_cwd", return_value="proj"), \
                mock.patch("builtins.open", side_effect=unreadable):
            found, _m, _h, err = handoff.check_checked(
                self.SID, self.wd, floor)
        self.assertIsNone(found)
        self.assertIn("PermissionError", err or "")

    def test_a_stale_FIFO_is_unknown_promptly_instead_of_blocking_the_hook(self):
        """The scanner refuses nonregular entries before their read can block.

        This runs the real scanner in a child with a hard timeout. The old plain
        ``open`` waits forever for a FIFO writer; ``pk.open_regular`` opens it
        nonblocking, identifies the mode, closes it, and reports UNKNOWN.
        """
        d = os.path.join(os.environ["HELM_HOME"], "proj", "journal")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "2026-07-20-handoff-fifo.md")
        os.mkfifo(p)
        floor = time.time() - 60
        os.utime(p, (floor - 60, floor - 60))
        root = os.path.dirname(os.path.dirname(handoff.__file__))
        env = os.environ.copy()
        env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"]
                                         if env.get("PYTHONPATH") else "")
        env["HANDOFF_TEST_CWD"] = self.wd
        env["HANDOFF_TEST_FLOOR"] = str(floor)
        code = (
            "import os\n"
            "from helm import handoff\n"
            "handoff._project = lambda cwd: 'proj'\n"
            "handoff._git = lambda *args: None\n"
            "answer = handoff.check_checked('fifo-session', "
            "os.environ['HANDOFF_TEST_CWD'], "
            "float(os.environ['HANDOFF_TEST_FLOOR']))\n"
            "print(answer[3] or '')\n")
        try:
            run = subprocess.run(
                [sys.executable, "-c", code], cwd=self.wd, env=env,
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=5, check=False)
        except subprocess.TimeoutExpired:
            self.fail("handoff scan blocked opening a stale FIFO")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("NotRegularFile", run.stdout)
        self.assertIn(os.path.basename(p), run.stdout)

    def test_a_stale_pre_stat_cannot_hide_a_fresh_opened_replacement(self):  # noqa: VACUOUS_ASSERTION — returned fresh path is asserted below
        """Freshness belongs to the opened fd, never an earlier path stat.

        The path is stale until ``open`` replaces it with a fresh authored
        handoff. Skipping it from the stale pre-stat loses the fresh file; this
        is why even an apparently stale unreadable path must be attempted and
        can make the answer UNKNOWN rather than being dismissed as irrelevant.
        """
        d = os.path.join(os.environ["HELM_HOME"], "proj", "journal")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "2026-07-20-handoff-proof.md")
        fresh = p + ".fresh"
        body = ("---\nmetadata:\n  type: handoff\n"
                "  session_id: %s\n---\n"
                "DONE: x\nREMAINING: y\nNEXT: z\n" % self.SID)
        for path in (p, fresh):
            pk.atomic_write(path, body)
        old = time.time() - (handoff.FRESH_H + 1) * 3600
        os.utime(p, (old, old))
        floor = time.time() - handoff.FRESH_H * 3600
        self.assertLess(os.stat(p).st_mtime, floor,
                        "positive control: the pathname starts stale")
        self.assertGreater(os.stat(fresh).st_mtime, floor)
        real_open, swapped = open, []

        def replace_at_open(path, *args, **kwargs):
            if os.fspath(path) == p and not swapped:
                os.replace(fresh, p)
                swapped.append(True)
            return real_open(path, *args, **kwargs)

        with mock.patch.object(inject, "project_for_cwd", return_value="proj"), \
                mock.patch.object(os, "listdir",
                                  return_value=[os.path.basename(p)]), \
                mock.patch("builtins.open", side_effect=replace_at_open):
            found, _m, _h, err = handoff.check_checked(
                self.SID, self.wd, floor)
        self.assertTrue(swapped, "the candidate must reach open despite pre-stat")
        self.assertEqual(found, p)
        self.assertIsNone(err)

    def test_ordering_uses_each_opened_snapshot_not_an_earlier_stat(self):  # noqa: VACUOUS_ASSERTION — winner path is asserted below
        d = os.path.join(os.environ["HELM_HOME"], "proj", "journal")
        os.makedirs(d, exist_ok=True)
        a = os.path.join(d, "2026-07-20-handoff-a.md")
        b = os.path.join(d, "2026-07-20-handoff-b.md")
        newer = b + ".swap"
        body = ("---\nmetadata:\n  type: handoff\n"
                "  session_id: %s\n---\n"
                "DONE: x\nREMAINING: y\nNEXT: z\n" % self.SID)
        for path in (a, b, newer):
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)
        now = time.time()
        os.utime(a, (now, now))
        os.utime(b, (now - 60, now - 60))
        os.utime(newer, (now + 60, now + 60))
        real_open, swapped = open, []

        def replace_b_while_opening_a(path, *args, **kwargs):
            if os.fspath(path) == a and not swapped:
                os.replace(newer, b)
                swapped.append(True)
            return real_open(path, *args, **kwargs)

        with mock.patch.object(inject, "project_for_cwd",
                               return_value="proj"), \
                mock.patch.object(os, "listdir",
                                  return_value=[os.path.basename(a),
                                                os.path.basename(b)]), \
                mock.patch("builtins.open",
                           side_effect=replace_b_while_opening_a):
            found, _m, _h, err = handoff.check_checked(
                self.SID, self.wd, None)
        self.assertTrue(swapped)
        self.assertEqual(found, b)
        self.assertIsNone(err)

    def test_winner_is_revalidated_without_a_loser_opening_a_later_window(self):  # noqa: VACUOUS_ASSERTION — winner path and stat counts are asserted
        """Select newest A, validate A, and return before revalidating older B.

        The old all-candidate validation checked A, then checked B; that later
        stat replaces A and leaves the already-approved stale winner in its
        stable list. Counting both paths also falsifies a cure that merely drops
        all final validation instead of revalidating the selected winner.
        """
        d = os.path.join(os.environ["HELM_HOME"], "proj", "journal")
        os.makedirs(d, exist_ok=True)
        a = os.path.join(d, "2026-07-20-handoff-a.md")
        b = os.path.join(d, "2026-07-20-handoff-b.md")
        swap = a + ".swap"
        body = ("---\nmetadata:\n  type: handoff\n"
                "  session_id: %s\n---\n"
                "DONE: x\nREMAINING: y\nNEXT: z\n" % self.SID)
        for path in (a, b):
            pk.atomic_write(path, body)
        pk.atomic_write(swap, "---\nmetadata:\n  type: evidence\n---\n")
        now = time.time()
        os.utime(a, (now, now))
        os.utime(b, (now - 60, now - 60))
        real_stat, calls, swapped = os.stat, {a: 0, b: 0}, []

        def replace_a_on_loser_revalidation(path, *args, **kwargs):
            path = os.fspath(path)
            if path in calls:
                calls[path] += 1
            if path == b and calls[b] == 2:
                os.replace(swap, a)
                swapped.append(True)
            return real_stat(path, *args, **kwargs)

        with mock.patch.object(inject, "project_for_cwd", return_value="proj"), \
                mock.patch.object(os, "listdir",
                                  return_value=[os.path.basename(a),
                                                os.path.basename(b)]), \
                mock.patch.object(os, "stat",
                                  side_effect=replace_a_on_loser_revalidation):
            found, _m, _h, err = handoff.check_checked(
                self.SID, self.wd, None)
        self.assertEqual(calls[a], 2,
                         "the selected winner needs a post-selection stat")
        self.assertEqual(calls[b], 1,
                         "no loser stat may follow the winner's validation")
        self.assertFalse(swapped)
        self.assertEqual(found, a)
        self.assertIsNone(err)

    def test_hollow_winner_is_revalidated_after_the_repo_probe(self):
        """A loose path plus missing tuple cannot survive replacement in _git."""
        d = os.path.join(os.environ["HELM_HOME"], "proj", "journal")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "2026-07-20-handoff-hollow-race.md")
        swap = p + ".swap"
        hollow_body = ("---\nmetadata:\n  type: handoff\n"
                       "  session_id: %s\n---\n"
                       "prose without contract sections\n" % self.SID)
        pk.atomic_write(p, hollow_body)
        pk.atomic_write(swap, "---\nmetadata:\n  type: evidence\n---\n")
        with mock.patch.object(inject, "project_for_cwd", return_value="proj"), \
                mock.patch.object(handoff, "_git", return_value=None):
            found, _m, loose, err = handoff.check_checked(
                self.SID, self.wd, None)
        self.assertIsNone(found)
        self.assertEqual(loose[0], p,
                         "positive control: the unchanged hollow is returned")
        self.assertIn("done", loose[1])
        self.assertIsNone(err)
        replaced = []

        def replace_during_repo_probe(*_args):
            os.replace(swap, p)
            replaced.append(True)
            return None

        with mock.patch.object(inject, "project_for_cwd", return_value="proj"), \
                mock.patch.object(handoff, "_git",
                                  side_effect=replace_during_repo_probe):
            found, _m, loose, err = handoff.check_checked(
                self.SID, self.wd, None)
        self.assertTrue(replaced)
        self.assertIsNone(found)
        self.assertIsNone(loose,
                          "stale missing sections must not escape after _git")
        self.assertIn("changed during handoff scan", err or "")

    def test_same_seat_handoffs_are_ordered_by_mtime_not_filename(self):  # noqa: VACUOUS_ASSERTION — newer handoff path is asserted below
        """A lexical date/name is not the rewrite or compaction-fork order."""
        d = os.path.join(os.environ["HELM_HOME"], "proj", "journal")
        os.makedirs(d, exist_ok=True)

        def entry(name):
            p = os.path.join(d, name)
            with open(p, "w", encoding="utf-8") as f:
                f.write("---\nmetadata:\n  type: handoff\n"
                        "  session_id: old-session\n  seat: seat-a\n---\n"
                        "\nDONE: x\nREMAINING: y\nNEXT: z\n")
            return p

        lexical_newest = entry("2026-07-20-handoff-z.md")
        actual_newest = entry("2026-07-19-handoff-a.md")
        now = time.time()
        os.utime(lexical_newest, (now - 60, now - 60))
        os.utime(actual_newest, (now, now))
        os.environ["HELM_CHAT_NAME"] = "seat-a"
        with mock.patch.object(handoff.home, "session_id",
                               return_value=self.SID), \
                mock.patch.object(inject, "project_for_cwd",
                                  return_value="proj"):
            found, _m, _h, err = handoff.check_checked(
                self.SID, self.wd, None)
        self.assertEqual(found, actual_newest)
        self.assertIsNone(err)

    def test_ordering_uses_mtime_ns_when_float_timestamps_tie(self):  # noqa: VACUOUS_ASSERTION — newer path is the positive control
        """Nanoseconds decide before the filename when float mtimes collapse."""
        d = os.path.join(os.environ["HELM_HOME"], "proj", "journal")
        os.makedirs(d, exist_ok=True)
        newer = os.path.join(d, "2026-07-20-handoff-a.md")
        older = os.path.join(d, "2026-07-20-handoff-z.md")
        body = ("---\nmetadata:\n  type: handoff\n"
                "  session_id: %s\n---\n"
                "DONE: x\nREMAINING: y\nNEXT: z\n" % self.SID)
        for path in (newer, older):
            pk.atomic_write(path, body)
        base = ((time.time_ns() // 1_000_000_000) - 5) * 1_000_000_000 \
            + 123_456_789
        os.utime(older, ns=(base, base))
        os.utime(newer, ns=(base + 1, base + 1))
        newer_stat, older_stat = os.stat(newer), os.stat(older)
        self.assertGreater(newer_stat.st_mtime_ns, older_stat.st_mtime_ns)
        self.assertEqual(newer_stat.st_mtime, older_stat.st_mtime,
                         "the hostile pair must tie under float ranking")
        with mock.patch.object(inject, "project_for_cwd", return_value="proj"):
            found, _m, _h, err = handoff.check_checked(
                self.SID, self.wd, None)
        self.assertEqual(found, newer,
                         "lexically-larger older file must not win the float tie")
        self.assertIsNone(err)

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

    def test_hook_mode_two_legs_one_trigger(self):
        # missing contract: nag on stdout, rc 0, AND the snapshot was taken
        rc, out, err = self.hook_check(json.dumps(
            {"session_id": self.SID, "cwd": self.wd,
             "hook_event_name": "PreCompact", "trigger": "auto",
             "transcript_path": self.tp}))
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("NO handoff artifact", out)
        self.assertIn("helm handoff write", out)
        self.assertIn("helm now show", out)
        self.assertLessEqual(len(out.splitlines()), 4)   # PreCompact fires late
        self.assertIn("sid=aaaabbbb", self.read_now())   # leg 2 rode the trigger

    def precompact_record(self):
        """The record the RESUME LEG reads: `resumeturn.native_autocompaction`
        looks it up under `compaction_key(own_name, sid)`, and this process
        declares no HELM_CHAT_NAME (ENV_KEYS pops it), so the key is the
        session prefix — the same key the SessionStart hook derives."""
        entry = resumeturn._peek(resumeturn.compaction_key(None, self.SID))
        return (entry or {}).get("precompact")

    def fifo_path(self):
        """A FIFO where a transcript would be. It stats like a file and
        reports a size, so nothing but an S_ISREG check tells it apart, and
        a blocking open of it with no writer never returns (task/2530's
        shape, one door earlier than the store readers that met it)."""
        p = os.path.join(self.tmp, "fifo.jsonl")
        if not os.path.exists(p):
            os.mkfifo(p)
        return p

    def test_a_PreCompact_with_trigger_auto_records_auto_for_the_resume_leg(self):
        """(a) The third thing the PreCompact hook does, and the one the
        SessionStart resume leg cannot learn from its own payload: which KIND
        of compaction is running. Claude Code's payload says
        `trigger: "auto"` for its native autocompaction; the record carries
        the producer's spelling, the raw session id, and a stamp taken now."""
        before = time.time()
        rc, out, err = self.hook_check(json.dumps(
            {"session_id": self.SID, "cwd": self.wd,
             "hook_event_name": "PreCompact", "trigger": "auto",
             "transcript_path": self.tp}))
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("NO handoff artifact", out)   # the nag legs still ran
        rec = self.precompact_record()
        self.assertEqual(rec["trigger"], "auto")
        self.assertEqual(rec["session"], self.SID)
        self.assertIsNone(rec["agent"])             # the main thread
        self.assertGreaterEqual(rec["at"], before)
        self.assertLessEqual(rec["at"], time.time())
        # and WHERE the transcript stood: the position the SessionStart
        # consumer binds the record to (resumeturn, clause 5), read from
        # the real file, never a mocked stat
        st = os.stat(self.tp)
        self.assertGreater(st.st_size, 0)                       # MUST-HIT
        self.assertEqual((rec["transcript_len"], rec["transcript_ino"],
                          rec["transcript_dev"]),
                         (st.st_size, st.st_ino, st.st_dev))

    def test_a_PreCompact_without_a_transcript_path_records_no_position_and_says_so(self):  # noqa: VACUOUS_ASSERTION — the recorded trigger and the one stderr line inside each subTest are the positives, and the unconditional control after the loop re-records WITH the file and asserts the position present
        """(a, the negative) A PreCompact payload naming no transcript,
        one naming a path that cannot be stat'ed, and one naming a FIFO
        (which stats fine and reports a size, and whose blocking open with
        no writer never returns, so no position may be recorded over it —
        the consumer would be the one asked to open it, and this hook is on
        a 5s clock): the trigger is still recorded (it is evidence the
        resume leg reports), the position is
        NOT — no guessed length, no zero — and one stderr line names which
        proof is missing; the record then never vouches (the resume leg's
        own suite drives that). Control: arm (a), the same payload with
        the file, whose record carries all three fields and whose stderr is
        empty."""
        for name, needle, payload in (
                ("no transcript_path", "carries no transcript_path",
                 {"session_id": self.SID, "cwd": self.wd,
                  "hook_event_name": "PreCompact", "trigger": "auto"}),
                ("unstat-able transcript_path", "cannot be stat'ed",
                 {"session_id": self.SID, "cwd": self.wd,
                  "hook_event_name": "PreCompact", "trigger": "auto",
                  "transcript_path": os.path.join(self.tmp, "gone.jsonl")}),
                ("a FIFO transcript_path", "not a regular file",
                 {"session_id": self.SID, "cwd": self.wd,
                  "hook_event_name": "PreCompact", "trigger": "auto",
                  "transcript_path": self.fifo_path()}),
                # A NUL in the path is refused by the kernel binding as
                # ValueError, not OSError; uncaught it left note_precompact
                # and aborted the whole PreCompact branch, record and nag
                # both (sibling of review CL97 on the consumer side).
                ("a NUL in transcript_path", "cannot be stat'ed",
                 {"session_id": self.SID, "cwd": self.wd,
                  "hook_event_name": "PreCompact", "trigger": "auto",
                  "transcript_path": self.tp + "\x00"})):
            with self.subTest(name):
                rc, _out, err = self.hook_check(json.dumps(payload))
                self.assertEqual(rc, 0)
                lines = err.splitlines()
                self.assertEqual(len(lines), 1, err)
                self.assertIn("PreCompact transcript position not recorded",
                              lines[0])
                self.assertIn(needle, lines[0])
                self.assertIn("cannot vouch", lines[0])
                rec = self.precompact_record()
                self.assertEqual(rec["trigger"], "auto")           # MUST-HIT
                for field in ("transcript_len", "transcript_ino",
                              "transcript_dev"):
                    self.assertNotIn(field, rec)
        rc, _out, err = self.hook_check(json.dumps(
            {"session_id": self.SID, "cwd": self.wd,
             "hook_event_name": "PreCompact", "trigger": "auto",
             "transcript_path": self.tp}))
        self.assertEqual((rc, err), (0, ""))                      # control
        self.assertIn("transcript_len", self.precompact_record())

    def test_a_PreCompact_inside_a_subagent_records_its_agent_id(self):
        """A subagent's PreCompact lands under the seat's own key (same
        name, same session), so the record must say WHICH thread wrote it:
        the payload's agent_id, as Claude Code sets it inside a subagent.
        Control: arm (a), the main thread's record carries None."""
        rc, _out, err = self.hook_check(json.dumps(
            {"session_id": self.SID, "cwd": self.wd, "agent_id": "a1b2",
             "hook_event_name": "PreCompact", "trigger": "auto",
             "transcript_path": self.tp}))
        self.assertEqual((rc, err), (0, ""))
        rec = self.precompact_record()
        self.assertEqual((rec["trigger"], rec["agent"]), ("auto", "a1b2"))

    @contextlib.contextmanager
    def state_lock_held(self, release_after=None):
        """THE REAL FAILURE DOOR of the producer's write: `_mutate_entry`
        takes flock(LOCK_EX | LOCK_NB) on `state_path() + ".lock"`, and a
        second open file description holding LOCK_EX makes that raise
        BlockingIOError. With `release_after`, a timer thread releases the
        hold after that many seconds -- a deliverer child's stamp, held
        then gone. Nothing mocked."""
        import threading
        path = resumeturn.state_path() + ".lock"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            timer = threading.Timer(release_after or 0, fcntl.flock,
                                    (lock.fileno(), fcntl.LOCK_UN))
            if release_after is not None:
                timer.start()
            try:
                yield
            finally:
                timer.cancel()
                if release_after is not None:
                    timer.join()
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def state_files(self):
        root = os.path.dirname(resumeturn.state_path())
        return sorted(os.path.relpath(os.path.join(d, f), root)
                      for d, _dirs, files in os.walk(root) for f in files)

    # The producer's bound for the two arms below, scaled from the shipped
    # 2.0s with the hold that tests it (1:4, as 0.5s was to 2.0s). A refusal
    # really waits the whole bound on a real flock; the shipped value is
    # checked against the hook's timeout without being waited out.
    PRECOMPACT_WAIT_S = 0.4

    def test_a_PreCompact_waits_out_a_short_hold_on_the_state_lock_and_records(self):
        """(b at the producer) The lock held for a quarter of the producer's
        bound and released by a timer: the write waits, inside its bound, and
        lands. Control: the arm below, the same hold never released."""
        self.hook_check(json.dumps(
            {"session_id": self.SID, "cwd": self.wd,
             "hook_event_name": "PreCompact", "trigger": "auto",
             "transcript_path": self.tp}))
        hold = self.PRECOMPACT_WAIT_S / 4
        started = time.monotonic()
        with mock.patch.object(resumeturn, "PRECOMPACT_WAIT_S",
                               self.PRECOMPACT_WAIT_S), \
                self.state_lock_held(release_after=hold):
            rc, _out, err = self.hook_check(json.dumps(
                {"session_id": self.SID, "cwd": self.wd,
                 "hook_event_name": "PreCompact", "trigger": "manual",
                 "transcript_path": self.tp}))
        elapsed = time.monotonic() - started
        self.assertEqual((rc, err), (0, ""))
        self.assertGreaterEqual(elapsed, 0.8 * hold)             # it waited
        self.assertLess(elapsed, self.PRECOMPACT_WAIT_S)
        self.assertEqual(self.precompact_record()["trigger"], "manual")

    def test_a_PreCompact_refused_past_its_bound_says_so_and_writes_nothing(self):
        """(c at the producer) The lock held past PRECOMPACT_WAIT_S: the
        write gives up inside the bound -- measured under the hook's own
        timeout, read from hooks.SPECS -- prints one stderr line naming the
        lock's refusal, still exits 0, and writes NOTHING: the record
        standing before it is untouched and no file appears beside the
        store. Control: the arm above, and arm (b), the same manual
        PreCompact with the lock free, which overwrites."""
        self.hook_check(json.dumps(
            {"session_id": self.SID, "cwd": self.wd,
             "hook_event_name": "PreCompact", "trigger": "auto",
             "transcript_path": self.tp}))
        before, files = self.precompact_record(), self.state_files()
        timeout = next(sp["timeout"] for sp in hooks.SPECS
                       if sp["name"] == "handoff-precompact")
        self.assertLess(resumeturn.PRECOMPACT_WAIT_S, timeout,
                        "the shipped bound no longer fits under the hook")
        started = time.monotonic()
        with mock.patch.object(resumeturn, "PRECOMPACT_WAIT_S",
                               self.PRECOMPACT_WAIT_S), \
                self.state_lock_held():
            rc, _out, err = self.hook_check(json.dumps(
                {"session_id": self.SID, "cwd": self.wd,
                 "hook_event_name": "PreCompact", "trigger": "manual",
                 "transcript_path": self.tp}))
        elapsed = time.monotonic() - started
        self.assertEqual(rc, 0)                                # fail-open
        self.assertGreaterEqual(elapsed, self.PRECOMPACT_WAIT_S)
        self.assertLess(elapsed, timeout)
        self.assertEqual(len(err.splitlines()), 1, err)
        self.assertIn("PreCompact trigger not recorded after", err)
        self.assertIn("BlockingIOError", err)                  # the door named
        self.assertEqual(self.precompact_record(), before)     # stale, untouched
        self.assertEqual(self.state_files(), files)            # nothing beside it
        rc, _out, err = self.hook_check(json.dumps(
            {"session_id": self.SID, "cwd": self.wd,
             "hook_event_name": "PreCompact", "trigger": "manual",
             "transcript_path": self.tp}))
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.precompact_record()["trigger"], "manual")

    def test_a_PreCompact_with_trigger_manual_records_manual_over_an_auto(self):
        """(b) A typed or helm-injected /compact says `trigger: "manual"`, and
        it OVERWRITES whatever the last PreCompact recorded: the SessionStart
        that follows reads the PreCompact that preceded it. (The resume leg
        consumes the record it acts on, and a write refused past its bound
        leaves the last record standing -- the arm above -- so an overwrite
        that lands is the common case, not the only guard.)"""
        self.hook_check(json.dumps(
            {"session_id": self.SID, "cwd": self.wd,
             "hook_event_name": "PreCompact", "trigger": "auto",
             "transcript_path": self.tp}))
        self.assertEqual(self.precompact_record()["trigger"], "auto")
        rc, _out, err = self.hook_check(json.dumps(
            {"session_id": self.SID, "cwd": self.wd,
             "hook_event_name": "PreCompact", "trigger": "manual",
             "transcript_path": self.tp}))
        self.assertEqual((rc, err), (0, ""))
        rec = self.precompact_record()
        self.assertEqual(rec["trigger"], "manual")
        self.assertEqual(rec["session"], self.SID)

    def test_a_SessionEnd_writes_no_PreCompact_record(self):  # noqa: VACUOUS_ASSERTION — the now-snapshot proves the hook ran; SessionEnd owes no record by design
        """The negative on an otherwise-valid payload: the same verb rides
        SessionEnd, and a session that ends is not compacting. A record
        written here would vouch for a compaction that never happened."""
        rc, _out, err = self.hook_check(json.dumps(
            {"session_id": self.SID, "cwd": self.wd,
             "hook_event_name": "SessionEnd", "reason": "exit"}))
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("sid=aaaabbbb", self.read_now())   # MUST-HIT: it ran
        self.assertIsNone(self.precompact_record())

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


class CompactionFloorTest(CheckBase):
    """The EVENT floor — bug class `compaction-floors-on-session-start`.

    LIVE MISS, 2026-07-28: a PreCompact hook printed "contract satisfied —
    2026-07-27-handoff-….md (37h old)" and therefore wrote no handoff for the
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


class UnreadableIsNotAbsentTest(CheckBase):
    """The third and fourth rungs of ONE class (codex, gate dcf603c8b0bef585).

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
        """codex mutated this handler away and my 12 tests stayed GREEN. I
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
        it. codex ended six rounds with the root cause: a SECOND SCAN GATED ON
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
                         "on disliking the first is the defect codex named")
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
    seams (codex, gates dcf603c8 / d56a646e / 4ef1aeee), and every fix was
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
    # consumer alone: codex (gate 65a9f5ba0770bef9) killed the consumer-keyed
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
        # call and the audit calls it per seam (codex, gate 3e543d9036bd8435).
        import ast
        with open(handoff.__file__, encoding="utf-8") as f:
            return ast.parse(f.read())

    def _calls(self, name):
        """{enclosing function -> count} for every call to `name` in handoff.

        BARE NAME *AND* ATTRIBUTE. The first version matched only ast.Name, so
        `handoff.check_checked(...)` — or any `import … as h; h.check_checked()`
        — was invisible to an audit whose entire job is knowing who reaches
        these seams. codex named that escape class alongside getattr and
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
        """codex, gate 3e543d9036bd8435: "duplicate same-edge calls ... leave
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
        audit refuses them instead of missing them (codex's escape list).
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
        """codex's mutation, pinned as its own arm so the reason survives the
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
        """codex, gate 73b7f2cecb441f8f. My previous pass claimed to eliminate
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
        """codex, gate c2a8856452c1ff43. Every arm above parses helm/handoff.py
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
        # siblings were invisible — codex proved it by planting
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


class LatestEntryTest(CheckBase):
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
        """The live 2026-07-31 near-miss, at the policy seam. Ordering by
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
        """codex's FIX at gate c8fd53acb0cc435f, reproduced exactly: a fresh
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
        """codex's second FIX at gate 0b18e133f272a1e3, and it is the SAME
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
        # WHO IS ASKING IS NOT THIS ARM'S SUBJECT, so declare it and let the
        # answer turn on the FILE. Left undeclared, the reason returned is
        # about the READER (no-identity, task/1688) and this arm would be
        # silently measuring identity handling instead of frontmatter parsing.
        os.environ["HELM_CHAT_NAME"] = "seat-a"
        self.assertEqual(handoff.UNATTRIBUTED, "unattributed")  # pinned literal
        reason = handoff.attribute_entry("proj", sid=self.SID)[2]
        # THE SUBJECT, stated directly rather than implied by the equality
        # below: a readable file with no frontmatter must not make the shelf
        # announce itself unreadable.
        self.assertNotEqual(reason, handoff.UNREADABLE)
        self.assertEqual(reason, handoff.UNATTRIBUTED)

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



    def test_an_ANONYMOUS_reader_is_told_THAT_not_that_nothing_is_its_own(self):
        """task/1688, measured live on seat hc2 2026-08-27. A process with no
        HELM_CHAT_NAME was told "no fresh entry there can be proven yours"
        while an entry carrying its OWN session id sat on the shelf.

        The mechanism is that `seats.foreign_seat()` answers False for EVERY
        name when own is "", so `foreign` is structurally 0 and the old
        fall-through reported UNATTRIBUTED — a claim ABOUT THE ENTRIES made by
        a reader in no position to make it. It cannot prove them foreign
        either: an entry stamped for another seat may still be this caller's
        under a forked session id. The only true statement is about the
        READER."""
        os.environ.pop("HELM_CHAT_NAME", None)
        self.shelf(("2026-07-28-handoff-ffff0000.md", "other-session",
                    "seat-b", "theirs", 5))
        why = handoff.attribute_entry("proj", sid=self.SID)[2]
        self.assertEqual(why, handoff.NO_IDENTITY)
        self.assertNotEqual(handoff.NO_IDENTITY, handoff.UNATTRIBUTED,
                            "the two reasons must stay distinct values or the "
                            "renderer cannot tell them apart")
        self.assertNotEqual(why, handoff.FOREIGN_ONLY)
        self.assertIn("identity", why)   # positive control on the
        # reason ITSELF: a constant-vs-constant equality is satisfiable
        # by two empty strings, so pin the value against a literal too.

    def test_a_NAMED_reader_on_the_same_shelf_still_reads_foreign_only(self):
        """THE MUST-MISS. Byte-identical shelf to the arm above; the ONLY
        difference is that this reader declares a name. If NO_IDENTITY can
        fire here it is not measuring anonymity, it is measuring the shelf."""
        os.environ["HELM_CHAT_NAME"] = "seat-a"
        self.shelf(("2026-07-28-handoff-ffff0000.md", "other-session",
                    "seat-b", "theirs", 5))
        why = handoff.attribute_entry("proj", sid=self.SID)[2]
        self.assertEqual(why, handoff.FOREIGN_ONLY)
        self.assertNotEqual(why, handoff.NO_IDENTITY)
        self.assertIn("foreign", why)    # positive control, as above

    def test_anonymity_never_costs_a_reader_its_OWN_sid_match(self):
        """Anonymity degrades the FOREIGN judgement and nothing else. The
        session id is the primary key and does not depend on a declared seat,
        so an unnamed process must still be handed its own entry — otherwise
        this fix would have traded a false sentence for a lost resume."""
        os.environ.pop("HELM_CHAT_NAME", None)
        self.shelf(("2026-07-28-handoff-aaaabbbb.md", self.SID,
                    "", "mine", 5))
        p, meta, why = handoff.attribute_entry("proj", sid=self.SID)
        self.assertEqual(why, handoff.BY_SID)
        self.assertEqual(meta["next"], "mine")
        self.assertIn("aaaabbbb", p)

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


class CanonicalSidTest(HandoffBase):
    """A TRUNCATED `--session` used to mint an entry `check` never matches.

    The stamp is the ADDRESS: `write_entry` wrote the caller's spelling into
    the frontmatter and `check_checked` looks for `home.session_id()`, so the
    two only meet when the caller typed the id in full. Measured 2026-08-07 on
    this project's own shelf — four complete entries written with a truncated
    `--session` were invisible to `handoff check`, which reported a 42h-old
    entry beside them as satisfying the contract."""
    FULL = "f0ad7476-3178-4c38-93e2-f8720f223f26"
    SHORT = FULL[:8]

    def test_a_prefix_of_the_running_session_expands(self):
        with mock.patch.object(handoff.home, "session_id",
                               return_value=self.FULL):
            # MUST-HIT CONTROL: the patch is what the function reads, so prove
            # it is in effect before reading anything into the arms below.
            self.assertEqual(handoff.home.session_id(), self.FULL)
            self.assertEqual(handoff.canonical_sid(self.SHORT), self.FULL)
            self.assertEqual(handoff.canonical_sid(self.FULL[:1]), self.FULL)
            # exact spelling is already canonical and must not be touched
            self.assertEqual(handoff.canonical_sid(self.FULL), self.FULL)
            # MUST-NOT-CLOBBER: another session's id is a deliberate act
            self.assertEqual(handoff.canonical_sid("other-sid"), "other-sid")
            # a LONGER id that merely starts the same way is not a prefix OF
            # the current session and stays as typed
            self.assertEqual(handoff.canonical_sid(self.FULL + "x"),
                             self.FULL + "x")
            for empty in ("", None):
                self.assertEqual(handoff.canonical_sid(empty), empty)

    def test_no_running_session_leaves_every_spelling_alone(self):  # noqa: VACUOUS_ASSERTION — the empty observable the rung sees is the INPUT (session_id None/""), not the assertion; the unconditional positive control is the first arm, which calls canonical_sid on the SAME input and requires it to MOVE, so a do-nothing function fails this test before reaching the loop
        """UNCHANGED is the whole claim here, and `unchanged` is what a
        do-nothing function returns for every input — so this arm is worthless
        without proof that the SAME call, on the SAME input, moves when a
        session IS running. The control is unconditional and runs first."""
        with mock.patch.object(handoff.home, "session_id",
                               return_value=self.FULL):
            self.assertEqual(handoff.canonical_sid(self.SHORT), self.FULL,
                             "positive control: with a session, this input "
                             "MOVES — so the arms below are not measuring a "
                             "function that never does anything")
        for cur in (None, ""):
            with mock.patch.object(handoff.home, "session_id",
                                   return_value=cur):
                self.assertEqual(handoff.canonical_sid(self.SHORT), self.SHORT)

    def written_entry(self, args):
        """Drive the real CLI write door and hand back (rc, path, frontmatter)."""
        rc, out, err = self.run_cmd(handoff.cmd_handoff, args, PROSE)
        self.assertEqual(rc, 0, err)
        files = glob.glob(os.path.join(
            os.environ["HELM_HOME"], "proj", "journal", "*handoff*.md"))
        self.assertEqual(len(files), 1, files)
        defaults = {"session_id": "", "done": "", "remaining": "", "next": ""}
        return rc, files[0], pk.parse_simple_frontmatter(files[0], defaults)

    def test_an_entry_written_short_is_found_by_check(self):  # noqa: VACUOUS_ASSERTION — the absence assertions (no "contract satisfied", no "carries no") sit AFTER an unconditional positive control on the same two observables: the first half asserts rc 0 with "contract satisfied" and the path in stdout, and the mutated half asserts "NO handoff artifact" IS present in the same stream
        """END TO END, BOTH DIRECTIONS — the arm the defect is named for.

        The prefix is exactly 8 chars, which is what the FILENAME carries
        either way (`<date>-handoff-<sid8>.md`), so the file's NAME is
        identical under cure and mutation and cannot be what decides the
        result. The frontmatter is the only thing that moves."""
        with mock.patch.object(handoff.home, "session_id",
                               return_value=self.FULL), \
                self.git_mock(), \
                mock.patch.object(inject, "project_for_cwd",
                                  return_value="proj"):
            _rc, path, e = self.written_entry(
                ["write", "--project", "proj", "--session", self.SHORT])
            # the STAMP is canonical even though the caller typed 8 chars
            self.assertEqual(e["session_id"], self.FULL)
            self.assertIn("-handoff-" + self.SHORT, path)
            self.assertTrue(e["next"], "the entry is not hollow")
            # and the checker — resolving the FULL id, as it always did — now
            # returns that exact file
            rc, out, err = self.run_cmd(handoff.cmd_handoff, ["check"])
            self.assertEqual(rc, 0, err)
            self.assertIn("contract satisfied", out)
            self.assertIn(path, out)

            # MUTATION: remove the cure and NOTHING ELSE. The same command in
            # the same shelf must stop finding the same file, or the arm above
            # proves nothing about the cure.
            os.remove(path)
            with mock.patch.object(handoff, "canonical_sid",
                                   side_effect=lambda s: s):
                _rc, mpath, me = self.written_entry(
                    ["write", "--project", "proj", "--session", self.SHORT])
                self.assertEqual(me["session_id"], self.SHORT)
                self.assertEqual(os.path.basename(mpath),
                                 os.path.basename(path),
                                 "same filename: the NAME is not the variable")
            rc, out, err = self.run_cmd(handoff.cmd_handoff, ["check"])
            self.assertEqual(rc, 1, "the unfindable entry must NOT satisfy")
            self.assertNotIn("contract satisfied", out)
            # rc 1 IS OVERLOADED — absent and hollow both return it — so the
            # code alone does not say which failure the mutation produced.
            # Name it: the entry must be reported UNSEEN, not seen-and-empty.
            self.assertIn("NO handoff artifact", out)
            self.assertNotIn("carries no", err)

    def test_a_foreign_session_is_written_and_said_out_loud(self):  # noqa: VACUOUS_ASSERTION — the closing assertNotIn is itself the second half of a deliberate pair: the SAME observable (this door's stderr) is asserted PRESENT unconditionally earlier in the same test, which is what proves the note fires selectively rather than never
        """A sid that is NOT this session's prefix is a deliberate act: the
        entry is written whole, and the half that is unmet — this window will
        never count it — is handed back rather than left under the success
        line."""
        with mock.patch.object(handoff.home, "session_id",
                               return_value=self.FULL), \
                self.git_mock(), \
                mock.patch.object(inject, "project_for_cwd",
                                  return_value="proj"):
            rc, out, err = self.run_cmd(
                handoff.cmd_handoff,
                ["write", "--project", "proj", "--session", "someone-else"],
                PROSE)
            self.assertEqual(rc, 0, err)
            self.assertIn("journal entry landed", out)
            self.assertIn("someone-else", err)
            self.assertIn("will not count this entry", err)
            # POSITIVE CONTROL ON THE NOTE ITSELF: the same door, same stream,
            # with a canonicalisable sid says nothing — so the assertion above
            # is reading a note that fires selectively, not one always present.
            os.remove(glob.glob(os.path.join(
                os.environ["HELM_HOME"], "proj", "journal", "*handoff*.md"))[0])
            _rc2, _out2, err2 = self.run_cmd(
                handoff.cmd_handoff,
                ["write", "--project", "proj", "--session", self.SHORT], PROSE)
            self.assertNotIn("will not count this entry", err2)


class AHandoffDescriptionSaysWhenItIsShortTest(HandoffBase):
    """The shelf's `description:` is a GLANCE at an entry whose whole text is
    in the body below it. Cutting it is right; cutting it silently made a
    glance indistinguishable from a whole next-step."""

    SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    def entry(self, nxt):
        text = "## DONE\n- a\n\nREMAINING: b\n\nNEXT: %s\n" % nxt
        path, missing = handoff.write_entry(text, "proj", self.SID)
        self.assertEqual(missing, [])
        return path, pk.parse_simple_frontmatter(
            path, {"name": "", "description": ""})["description"], text

    def test_a_long_next_step_is_cut_in_the_description_and_says_so(self):
        nxt = "run the whole ladder and then " * 20
        path, desc, text = self.entry(nxt)
        self.assertIn("[cut: 150 of ", desc)
        with open(path, encoding="utf-8") as fh:
            whole = fh.read()
        self.assertIn(text.rstrip(), whole)   # the value itself is recoverable

    def test_a_short_next_step_carries_no_mark(self):
        """The must-hit control on the same writer: under the width the
        description is the lead line and nothing else."""
        _path, desc, _text = self.entry("ship it")
        self.assertEqual(desc, "handoff: ship it")


if __name__ == "__main__":
    unittest.main()
