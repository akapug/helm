#!/usr/bin/env python3
"""working-set tests (task/4054): the recorder ring and the SessionStart(compact)
hook that hands a compacted seat back what it was holding.

Pins: a planted ring gives the hook all four planted facts (an existing path,
a verb spelling, an id, a background task with its output file) and gives
nothing to any other SessionStart or to plain `now show`; a deleted file drops
its line; 2,000 rows stay inside the ring bound and the 7,000-character
budget; a subagent's calls are never recorded and a subagent's compaction gets
nothing; a corrupt ring and a crashing builder fail open (rc 0, no output);
the recorder adds a bounded cost per call; flag values, command text and
output never reach the ring; the PreCompact nag leaves stdout for the nag file
and reaches the seat once, after the compaction. Hermetic: HELM_HOME is a tmp dir.
"""
import contextlib
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
_tmp_home(prefix="helm-test-wset-", var="HELM_HOME")

from helm import cli, handoff, hooks, inject, record, resumeturn  # noqa: E402
from helm import workingset  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_NAME", "MELD_CHAT_NAME",
            "HELM_WORKING_SET", "CLAUDE_CODE_TMPDIR", "HELM_REMEMBER_DIR",
            "CLAUDE_SESSION_ID")
SID = "5e55a0b1-1111-2222-3333-444455556666"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-wset-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_REMEMBER_DIR"] = os.path.join(self.tmp, "remember")
        os.environ["CLAUDE_CODE_TMPDIR"] = os.path.join(self.tmp, "cctmp")
        self.proj = os.path.join(self.tmp, "projects", "-slug")
        os.makedirs(self.proj)
        self.tp = os.path.join(self.proj, SID + ".jsonl")
        with open(self.tp, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user"}) + "\n")
        self.tasks = os.path.join(self.tmp, "cctmp", "claude-%d" % os.getuid(),
                                  "-slug", SID, "tasks")
        os.makedirs(self.tasks)

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- drivers ----------------------------------------------------------
    def ev(self, tool, tin=None, resp=None, failed=False, error=None, **extra):
        e = {"session_id": SID, "tool_name": tool, "cwd": self.tmp,
             "hook_event_name": "PostToolUseFailure" if failed else "PostToolUse",
             "tool_input": tin or {}, "transcript_path": self.tp}
        if resp is not None:
            e["tool_response"] = resp
        if error is not None:
            e["error"] = error
        e.update(extra)
        return e

    def rec(self, *events):
        with mock.patch.object(record, "_git_dirty", return_value=None), \
                mock.patch.object(record, "_git_root", return_value=None):
            for e in events:
                record.record(e)

    def payload(self, **kw):
        d = {"session_id": SID, "hook_event_name": "SessionStart",
             "source": "compact", "transcript_path": self.tp, "cwd": self.tmp}
        d.update(kw)
        return d

    def call(self, fn, args, stdin_text=""):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(sys, "stdin", io.StringIO(stdin_text)):
            rc = fn(list(args))
        return rc, out.getvalue(), err.getvalue()

    def hook_text(self, payload=None):
        """The installed spec's own command, driven through helm's verb
        table: the additionalContext it prints, or '' when it prints none."""
        spec = next(s for s in hooks.SPECS if s["name"] == "working-set")
        argv = spec["args"].split()
        rc, out, err = self.call(cli.VERBS[argv[0]], argv[1:],
                                 json.dumps(payload or self.payload()))
        self.assertEqual(rc, 0)
        if not out.strip():
            return ""
        env = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(env["hookEventName"], "SessionStart")
        return env["additionalContext"]

    def plant(self):
        """Four facts a compacted seat slips on: a path it edited, a verb
        spelling that worked, a task id, a background task and its output."""
        self.path = os.path.join(self.tmp, "lane", "helm", "thing.py")
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            f.write("x\n")
        with open(os.path.join(self.tasks, "bq1w2e3r4.output"), "w") as f:
            f.write("done\n")
        self.rec(
            self.ev("Edit", {"file_path": self.path}, {"ok": True}),
            self.ev("Bash", {"command": "helm chat read --room helm --limit 8"},
                    {"stdout": "rows", "stderr": ""}),
            self.ev("Bash", {"command": "helm task show 4054"},
                    {"stdout": "task/4054 open", "stderr": ""}),
            self.ev("Bash", {"command": "fab test --repo /x -- python3 -m unittest t",
                             "run_in_background": True},
                    {"stdout": "", "stderr": "", "backgroundTaskId": "bq1w2e3r4"}),
            self.ev("Read", {"file_path": self.path}, {"file": {}}),
        )


class PlantedRingTest(Base):
    def test_the_hook_hands_back_all_four_planted_facts(self):
        self.plant()
        text = self.hook_text()
        self.assertIn(self.path, text)
        self.assertIn("edited", text)
        self.assertIn("helm chat read --room --limit", text)
        self.assertIn("task/4054", text)
        self.assertIn("bq1w2e3r4", text)
        self.assertIn(os.path.join(self.tasks, "bq1w2e3r4.output"), text)
        self.assertLessEqual(len(text), workingset.MAX_CHARS)

    def test_without_the_hook_no_fact_reaches_the_seat(self):  # noqa: VACUOUS_ASSERTION — the compact payload on the same ring is the unconditional positive control, asserted last
        """The same ring, read by everything that ran on SessionStart before
        this lane: plain `now show`, and the hook on any other source. None
        of the four facts reaches the seat; the compact payload is the
        positive control on the same ring."""
        self.plant()
        rc, out, _ = self.call(handoff.cmd_now, ["show"])
        self.assertEqual(rc, 0)
        for fact in (self.path, "chat read --room", "task/4054", "bq1w2e3r4"):
            self.assertNotIn(fact, out)
        for src in ("startup", "resume", "clear", ""):
            with self.subTest(source=src):
                self.assertEqual(self.hook_text(self.payload(source=src)), "")
        self.assertIn("task/4054", self.hook_text())

    def test_the_spec_is_SessionStart_compact_3s_and_advisory(self):  # noqa: VACUOUS_ASSERTION — the spec tuple equality and the consequence line are the positives; no gate and no scope are the spec's own
        spec = next(s for s in hooks.SPECS if s["name"] == "working-set")
        self.assertEqual((spec["event"], spec["matcher"], spec["timeout"],
                          spec["args"]),
                         ("SessionStart", "compact", 3, "now show --hook-json"))
        self.assertFalse(spec.get("gate"))
        self.assertNotIn("scope", spec)
        self.assertIn("working-set", hooks._ADVISORY_LOST)

    def test_a_deleted_file_drops_its_line(self):
        self.plant()
        self.assertIn(self.path, self.hook_text())
        os.remove(self.path)
        text = self.hook_text()
        self.assertNotIn(self.path, text)
        self.assertIn("task/4054", text)     # the rest of the set still stands

    def test_newest_first_and_calls_ago(self):
        self.plant()
        rows = workingset.ring(SID)
        self.assertEqual(len(rows), 5)
        text = self.hook_text()
        self.assertIn("task/4054  (2 ago, x1)", text)
        self.assertIn("(last call, edited)", text)

    def test_a_usage_failure_is_DONT_and_names_what_worked(self):
        self.rec(
            self.ev("Bash", {"command": "helm chat read --room helm"},
                    {"stdout": "ok", "stderr": ""}),
            self.ev("Bash", {"command": "helm chat read --seat kimi"},
                    failed=True,
                    error="Exit code 2\nhelm chat read: unknown flag --seat"),
            self.ev("Bash", {"command": "grep -n nothing /etc/hostname"},
                    failed=True, error="Exit code 1"),
        )
        text = self.hook_text()
        self.assertIn("DON'T helm chat read --seat", text)
        self.assertIn("use: helm chat read --room", text)
        # an ordinary nonzero exit is not a spelling failure
        self.assertNotIn("DON'T grep", text)

    def test_a_spelling_that_worked_after_it_failed_is_not_DONT(self):  # noqa: VACUOUS_ASSERTION — the positive control is the worked line asserted present on the same text
        self.rec(
            self.ev("Bash", {"command": "helm task show 1"}, failed=True,
                    error="Exit code 2\nusage: helm task show <id>"),
            self.ev("Bash", {"command": "helm task show 1"},
                    {"stdout": "task/1", "stderr": ""}))
        text = self.hook_text()
        self.assertIn("helm task show  (x1, last call)", text)
        self.assertNotIn("DON'T", text)

    def test_a_monitor_has_no_output_file(self):
        self.rec(self.ev("Monitor", {"command": "helm chat wait --follow"},
                         {"taskId": "mon12345"}))
        text = self.hook_text()
        self.assertIn("mon12345  (Monitor, last call): no output file", text)

    def test_a_background_task_whose_output_is_gone_says_so(self):
        self.rec(self.ev("Bash", {"command": "sleep 1", "run_in_background": True},
                         {"stdout": "", "backgroundTaskId": "bgone1234"}))
        self.assertIn("bgone1234  (bash, last call): NO output file at",
                      self.hook_text())

    def test_scratch_dirs_are_listed(self):
        d = os.path.join(self.tmp, "x", "scratchpad", "build-4054")
        os.makedirs(d)
        f = os.path.join(d, "notes.txt")
        with open(f, "w") as fh:
            fh.write("n\n")
        self.rec(self.ev("Write", {"file_path": f}, {"ok": 1}))
        text = self.hook_text()
        self.assertIn("Scratch dirs:\n  " + d, text)

    def test_now_show_working_set_prints_on_demand(self):
        self.plant()
        rc, out, _ = self.call(handoff.cmd_now,
                               ["show", "--working-set", "--session", SID])
        self.assertEqual(rc, 0)
        self.assertIn("task/4054", out)
        rc, out, err = self.call(handoff.cmd_now,
                                 ["show", "--working-set", "--session", "none"])
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("no working set", err)

    def test_the_kill_switch(self):
        self.plant()
        os.environ["HELM_WORKING_SET"] = "0"
        self.assertEqual(self.hook_text(), "")
        os.environ.pop("HELM_WORKING_SET")
        self.assertIn("task/4054", self.hook_text())


class BoundTest(Base):
    def test_2000_rows_stay_inside_the_ring_and_the_budget(self):
        files = []
        for i in range(60):
            p = os.path.join(self.tmp, "f", "%s-%03d.py" % ("p" * 120, i))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").close()
            files.append(p)
        for i in range(60):
            open(os.path.join(self.tasks, "bg%06d.output" % i), "w").close()
        sd = record.session_dir(SID)
        for i in range(2000):
            workingset.append(sd, {
                "ts": 1, "t": "Bash", "ok": i % 7 != 0,
                "p": [files[i % 60]],
                "v": ["helm verb%d sub%d --flag-%d" % (i % 90, i, i)],
                "x": ["helm bad%d --nope-%d" % (i % 40, i)] if i % 7 == 0 else [],
                "id": ["task/%d" % i, "row %012x" % i],
                "bg": "bg%06d" % (i % 60), "bk": "bash"})
        size = os.path.getsize(workingset.ring_path(sd))
        self.assertLessEqual(size, workingset.TRIM_BYTES + 1024)
        rows = workingset.ring(SID)
        self.assertEqual(len(rows), workingset.RING_ROWS)
        text = self.hook_text()
        self.assertGreater(len(text), 3000, "MUST-HIT: a full set rendered")
        self.assertLessEqual(len(text), workingset.MAX_CHARS)
        for title in ("Paths that exist now:", "Spellings that worked:",
                      "Ids you touched:", "Background tasks:"):
            self.assertIn(title, text)

    def test_the_recorder_adds_a_bounded_cost_per_call(self):  # noqa: VACUOUS_ASSERTION — the counted appends equal the call count, an unconditional positive on the same observable
        """The recorder fires on every tool call, so its cost is COUNTED, not
        timed: per call note() starts no subprocess, opens the ring once to
        append, and rewrites it only past TRIM_BYTES, so a trim is amortized
        over hundreds of calls. That is the existing record hook's own
        budget law (no subprocess on the hot path, O(1) appends)."""
        sd = record.session_dir(SID)
        cmd = ("cd /tmp/lane && HELM_CHAT_NAME=x timeout 60 helm dispatch "
               "send --to kimi --task task/4054 --ref lane/x <<'EOF'\nbody\nEOF")
        ring = workingset.ring_path(sd)
        real_open, modes = open, []

        def counted(path, mode="r", *a, **k):
            if str(path) == ring:
                modes.append(mode)
            return real_open(path, mode, *a, **k)

        n = 1500
        with mock.patch("subprocess.Popen",
                        side_effect=AssertionError("a subprocess on the hot path")), \
                mock.patch("builtins.open", counted):
            for _ in range(n):
                workingset.note(sd, "Bash", {"command": cmd},
                                {"stdout": "row abcdef012345 sent"},
                                '{"stdout": "row abcdef012345 sent"}', False, cmd)
        appends = modes.count("a")
        trims = len(modes) - appends
        self.assertEqual(appends, n)
        row = os.path.getsize(ring) / len(workingset.ring(SID))
        # one trim per (TRIM_BYTES - RING_ROWS rows) bytes appended, at most
        self.assertLessEqual(trims, 1 + n * row //
                             (workingset.TRIM_BYTES - workingset.RING_ROWS * row))
        self.assertGreater(trims, 0, "MUST-HIT: the ring was trimmed")


class PrivacyTest(Base):
    def test_no_flag_value_command_text_or_output_reaches_the_ring(self):
        self.rec(
            self.ev("Bash", {"command": "helm chat post --room helm "
                                        "--body 'the owner said hush-1a2b' "
                                        "--token=sk-zz-SECRETVALUE"},
                    {"stdout": "posted hush-output-77", "stderr": ""}),
            self.ev("Bash", {"command": "curl -H 'Authorization: Bearer "
                                        "TOK3N' https://example.invalid/x"},
                    {"stdout": "{\"secret\": \"OUTPUT-SECRET\"}"}),
            self.ev("Read", {"file_path": "/etc/hostname"},
                    {"file": {"content": "FILE-CONTENTS-9"}}),
            self.ev("Bash", {"command": "gh secret set FOO -bS3cretValue123"},
                    {"stdout": ""}),
            self.ev("Bash", {"command": "git commit -mhunter2secret"},
                    {"stdout": ""}),
            self.ev("Bash", {"command": "gh auth login -pPassw0rdxyz"},
                    {"stdout": ""}))
        with open(workingset.ring_path(record.session_dir(SID))) as f:
            raw = f.read()
        self.assertIn("helm chat post --room --body --token", raw)   # control
        self.assertIn("git commit -m", raw)                          # control
        for leak in ("hush", "SECRETVALUE", "TOK3N", "Bearer", "OUTPUT-SECRET",
                     "FILE-CONTENTS", "example.invalid", "posted",
                     "S3cretValue123", "hunter2secret", "Passw0rdxyz"):
            self.assertNotIn(leak, raw)


class GitSpellingTest(unittest.TestCase):
    def test_a_git_subcommand_keeps_its_own_subcommand_and_no_value(self):
        sp = workingset.spelling
        # measured live (task/4070's replay of a lead's 400 calls): `git
        # worktree list --porcelain` came back as `git worktree --porcelain`
        self.assertEqual(sp(["git", "worktree", "list", "--porcelain"]),
                         "git worktree list --porcelain")
        self.assertEqual(sp(["git", "-C", "/x", "stash", "push", "--", "a"]),
                         "git stash push")
        # a branch, a file or a remote's name after any other subcommand is
        # a value and stays out
        self.assertEqual(sp(["git", "checkout", "main"]), "git checkout")
        self.assertEqual(sp(["git", "push", "origin", "lane"]), "git push")
        self.assertEqual(sp(["git", "remote", "-v"]), "git remote -v")


class SensitivePathTest(Base):
    def test_env_and_credential_paths_never_reach_the_ring_or_the_set(self):
        home = os.path.join(self.tmp, "home")
        planted = [os.path.join(home, ".config", "simbi", "credentials.env"),
                   os.path.join(home, ".aws", "config"),
                   os.path.join(self.tmp, "repo", ".env.local"),
                   os.path.join(self.tmp, "repo", "deploy.pem"),
                   os.path.join(self.tmp, "repo", "id.key"),
                   os.path.join(self.tmp, "repo", "my-secrets.txt")]
        keep = os.path.join(self.tmp, "repo", "helm", "thing.py")
        for p in planted + [keep]:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write("x\n")
        with mock.patch.dict(os.environ, {"HOME": home}):
            self.rec(*[self.ev("Read", {"file_path": p}, {"file": {}})
                       for p in planted + [keep]],
                     self.ev("Bash", {"command": "cat ~/.aws/credentials "
                                                 + " ".join(planted)},
                             {"stdout": "", "stderr": ""}))
            with open(workingset.ring_path(record.session_dir(SID))) as f:
                raw = f.read()
            text = self.hook_text()
        self.assertIn(keep, text)                                  # control
        for p in planted + ["~/.aws/credentials"]:
            self.assertNotIn(p, raw)
            self.assertNotIn(p, text)
        # a ring written before this rule: the builder drops it too
        self.assertEqual(workingset.build([{"p": [planted[0]], "ok": 1}]), "")


class SubagentTest(Base):
    def test_a_subagents_calls_are_never_recorded(self):
        self.rec(self.ev("Bash", {"command": "helm task show 77"},
                         {"stdout": "x"}, agent_id="agent-abc"))
        self.assertEqual(workingset.ring(SID), [])
        self.rec(self.ev("Bash", {"command": "helm task show 77"},
                         {"stdout": "x"}))
        self.assertEqual(len(workingset.ring(SID)), 1)       # control

    def test_a_subagent_compaction_gets_nothing(self):
        self.plant()
        self.assertIn("task/4054", self.hook_text())         # control
        # 1. the payload names a subagent
        self.assertEqual(self.hook_text(self.payload(agent_id="agent-1")), "")
        # 2. the PreCompact record says a subagent compacted
        resumeturn.note_precompact(SID, "auto", "agent-1", self.tp)
        self.assertEqual(self.hook_text(), "")

    def test_a_proven_child_by_transcript_gets_nothing(self):  # noqa: VACUOUS_ASSERTION — test_the_hook_hands_back_all_four_planted_facts is the positive on the same planted ring and payload
        self.plant()
        with mock.patch.object(resumeturn, "compacting_thread",
                               return_value=(resumeturn.THREAD_CHILD, "why")):
            self.assertEqual(self.hook_text(), "")

    def test_an_unproven_thread_gets_the_set_with_a_caveat(self):  # noqa: VACUOUS_ASSERTION — the set and the caveat are asserted present first; the caveat's absence follows a fresh main-thread record
        self.plant()
        sub = os.path.join(self.proj, SID, "subagents")
        os.makedirs(sub)
        open(os.path.join(sub, "agent-x.jsonl"), "w").close()
        text = self.hook_text()
        self.assertIn("task/4054", text)
        self.assertIn("main thread's set", text)
        # a fresh main-thread PreCompact record proves the lead: no caveat
        resumeturn.note_precompact(SID, "auto", None, self.tp)
        self.assertNotIn("main thread's set", self.hook_text())


class FailOpenTest(Base):
    def test_a_corrupt_ring_fails_open(self):
        sd = record.session_dir(SID)
        os.makedirs(sd, exist_ok=True)
        with open(workingset.ring_path(sd), "wb") as f:
            f.write(b"\xff\xfe{not json\n\x00\x00[1,2]\n\"str\"\n{\"t\": 5}\n")
        self.assertEqual(self.hook_text(), "")
        self.plant()                       # appends good rows after the junk
        self.assertIn("task/4054", self.hook_text())

    def test_a_crashing_builder_prints_nothing_and_exits_0(self):  # noqa: VACUOUS_ASSERTION — the same planted ring gives the full set in test_the_hook_hands_back_all_four_planted_facts; here only the builder is broken
        self.plant()
        with mock.patch.object(workingset, "build",
                               side_effect=RuntimeError("boom")):
            self.assertEqual(self.hook_text(), "")
        rc, out, _ = self.call(handoff.cmd_now, ["show", "--hook-json"],
                               "not json{{")
        self.assertEqual((rc, out), (0, ""))

    def test_a_recorder_fault_never_costs_the_counters(self):
        with mock.patch.object(workingset, "note",
                               side_effect=RuntimeError("boom")):
            self.rec(self.ev("Bash", {"command": "ls"}, {"stdout": ""}))
        self.assertEqual(record.counters(SID).get("last-tool"), "Bash")


class NagChannelTest(Base):
    def check(self, payload):
        with mock.patch.object(handoff, "_git", return_value=None), \
                mock.patch.object(inject, "project_for_cwd", return_value=None):
            return self.call(handoff.cmd_handoff, ["check", "--hook-json"],
                             json.dumps(payload))

    def test_the_precompact_nag_leaves_stdout_and_reaches_the_seat_once(self):  # noqa: VACUOUS_ASSERTION — the nag is asserted present in the hook's text before its second, empty read
        rc, out, err = self.check({"session_id": SID, "cwd": self.tmp,
                                   "hook_event_name": "PreCompact",
                                   "trigger": "auto", "transcript_path": self.tp})
        self.assertEqual((rc, out, err), (0, "", ""))
        text = self.hook_text()
        self.assertIn("The PreCompact handoff check said:", text)
        self.assertIn("NO handoff artifact", text)
        self.assertNotIn("PreCompact handoff check", self.hook_text())  # once

    def test_SessionEnd_keeps_stdout(self):  # noqa: VACUOUS_ASSERTION — the nag on stdout is the unconditional positive
        rc, out, _ = self.check({"session_id": SID, "cwd": self.tmp,
                                 "hook_event_name": "SessionEnd",
                                 "transcript_path": self.tp})
        self.assertEqual(rc, 0)
        self.assertIn("NO handoff artifact", out)
        self.assertEqual(workingset.take_nag(SID), [])

    def test_a_subagents_precompact_keeps_no_nag(self):
        rc, out, err = self.check({"session_id": SID, "cwd": self.tmp,
                                   "hook_event_name": "PreCompact",
                                   "trigger": "auto", "agent_id": "agent-9",
                                   "transcript_path": self.tp})
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertEqual(workingset.take_nag(SID), [])
        # control: the same payload from the main thread keeps the nag
        p = {"session_id": SID, "cwd": self.tmp, "hook_event_name": "PreCompact",
             "trigger": "auto", "transcript_path": self.tp}
        self.check(p)
        self.assertIn("NO handoff artifact", "\n".join(workingset.take_nag(SID)))

    def test_a_stale_nag_is_dropped(self):  # noqa: VACUOUS_ASSERTION — a fresh nag is read back whole at the end, on the same file
        workingset.put_nag(SID, "helm handoff: old")
        p = os.path.join(record.session_dir(SID), workingset.NAG_FILE)
        old = time.time() - workingset.NAG_FRESH_S - 60
        os.utime(p, (old, old))
        self.assertEqual(workingset.take_nag(SID), [])
        self.assertFalse(os.path.exists(p))
        workingset.put_nag(SID, "helm handoff: new")
        self.assertEqual(workingset.take_nag(SID), ["helm handoff: new"])


class NagBackstopTest(Base):
    """task/4070: the nag left the PreCompact stdout, so a seat must still
    get it when the SessionStart(compact) hook cannot hand it over."""
    check = NagChannelTest.check

    def precompact(self):
        rc, out, err = self.check({"session_id": SID, "cwd": self.tmp,
                                   "hook_event_name": "PreCompact",
                                   "trigger": "auto", "transcript_path": self.tp})
        self.assertEqual((rc, out, err), (0, "", ""))

    def prompt(self):
        """The already-installed UserPromptSubmit hook, as a seat whose
        settings predate the working-set hook runs it on its next prompt."""
        event = {"prompt": "carry on", "session_id": SID, "cwd": self.tmp,
                 "hook_event_name": "UserPromptSubmit",
                 "transcript_path": self.tp}
        with mock.patch("helm.inject._whisper._ledger_begin", return_value=None):
            rc, out, _ = self.call(inject.cmd_inject, ["--hook-json"],
                                   json.dumps(event))
        self.assertEqual(rc, 0)
        return out

    def test_inject_hands_a_pending_nag_to_the_next_prompt_once(self):  # noqa: VACUOUS_ASSERTION — the nag is asserted present on the first prompt before the second, empty one
        self.assertEqual(self.prompt(), "")       # control: no nag, no text
        self.precompact()
        out = self.prompt()
        self.assertIn("the handoff check before your last compaction said:", out)
        self.assertIn("NO handoff artifact", out)
        self.assertNotIn("NO handoff artifact", self.prompt())  # once

    def test_the_compact_hook_takes_it_first_and_inject_says_nothing(self):  # noqa: VACUOUS_ASSERTION — the hook's text carries the nag first; inject's empty output follows on the same file
        self.precompact()
        self.assertIn("NO handoff artifact", self.hook_text())
        self.assertEqual(self.prompt(), "")

    def test_the_kill_switch_keeps_the_nag(self):
        self.plant()
        self.precompact()
        os.environ["HELM_WORKING_SET"] = "0"
        text = self.hook_text()
        self.assertTrue(text.startswith(workingset.NAG_HEAD), text)
        self.assertIn("NO handoff artifact", text)
        self.assertNotIn("task/4054", text)       # the set itself stays off

    def test_a_crashing_builder_keeps_the_nag(self):  # noqa: VACUOUS_ASSERTION — the nag is asserted present in the hook text before the emptied file is checked
        self.plant()
        self.precompact()
        with mock.patch.object(workingset, "build", side_effect=RuntimeError):
            text = self.hook_text()
        self.assertIn("NO handoff artifact", text)
        self.assertEqual(workingset.take_nag(SID), [])   # taken, not left


if __name__ == "__main__":
    unittest.main()
