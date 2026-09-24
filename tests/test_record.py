#!/usr/bin/env python3
"""record tests — the session-keyed tool-outcome recorder (PostToolUse).

Pins the laws: session_id-keyed state (never pane), fail-open + zero output on
every payload shape, NO subprocess on the passive hot path (git probed only on
dirtying tools, in the tool's workdir), the conservative stuck gate (reads
never arm it), the loop-thrash hash chain, the two verify-grounding artifacts
(command-log.jsonl with REAL exit codes, digests never raw command lines;
edit-targets.log basenames), O(1) append rotation, the apostrophe-payload
regression (the ancestor's python-c silent-noop class), and the claude wiring
(merge-preserving PostToolUse install + status + doctor rows). Hermetic:
HELM_HOME is a tmp dir; install tests re-point homes/configs at tmp roots
(the test_hooks pattern) — the live ~/.claude is never touched."""
import contextlib
import io
import json
import os
import shlex
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import seats_stop_signals

from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-home-", var="HELM_HOME")

from helm import configs, doctor, homes, hooks, pk, record  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME")


class RecordBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-record-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ev(self, tool="Read", sid="sess-1", tin=None, resp=None, **extra):
        e = {"session_id": sid, "tool_name": tool, "cwd": self.tmp,
             "hook_event_name": "PostToolUse"}
        if tin is not None:
            e["tool_input"] = tin
        if resp is not None:
            e["tool_response"] = resp
        e.update(extra)
        return e

    def run_cmd(self, args, stdin_text=None):
        out, err = io.StringIO(), io.StringIO()
        stdin = io.StringIO(stdin_text or "")
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(sys, "stdin", stdin):
            rc = record.cmd_record(list(args))
        return rc, out.getvalue(), err.getvalue()

    def counters(self, sid="sess-1"):
        return record.counters(sid)

    def artifact(self, name, sid="sess-1"):
        try:
            with open(os.path.join(record.session_dir(sid), name),
                      encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""


class PassiveAndDirtyTest(RecordBase):
    def test_passive_streak_and_no_subprocess_on_passive_ops(self):
        probe = mock.Mock(return_value=False)
        with mock.patch.object(record, "_git_dirty", probe):
            for tool in ("Read", "Grep", "Glob"):
                record.record(self.ev(tool=tool))
            self.assertEqual(self.counters()["passive-streak"], 3)
            probe.assert_not_called()  # the <5ms hot path never forks
            record.record(self.ev(tool="Edit", tin={"file_path": "/x/y.py"}))
        self.assertEqual(self.counters()["passive-streak"], 0)

    def test_git_probed_only_on_dirtying_tools_in_tool_workdir(self):
        probe = mock.Mock(return_value=True)
        with mock.patch.object(record, "_git_dirty", probe):
            record.record(self.ev(tool="Bash",
                                  tin={"command": "touch x", "cwd": "/wt/a"}))
            probe.assert_called_once_with("/wt/a")
            self.assertEqual(self.counters()["last-dirty"], 1)
            self.assertEqual(self.counters()["dirty-streak"], 1)
            record.record(self.ev(tool="Read"))  # cached last-dirty, no re-probe
            self.assertEqual(probe.call_count, 1)
            self.assertEqual(self.counters()["dirty-streak"], 2)
            record.record(self.ev(tool="Bash",
                                  tin={"command": "git commit -m x"}))
        self.assertEqual(self.counters()["dirty-streak"], 0)  # commit resets
        self.assertEqual(self.counters()["passive-streak"], 0)  # commit = forward
        probe2 = mock.Mock(return_value=False)
        with mock.patch.object(record, "_git_dirty", probe2):
            record.record(self.ev(tool="Bash", tin={"command": "ls"}))
        self.assertEqual(self.counters()["last-dirty"], 0)
        self.assertEqual(self.counters()["dirty-streak"], 0)

    def test_workdir_falls_back_to_event_cwd(self):
        probe = mock.Mock(return_value=False)
        with mock.patch.object(record, "_git_dirty", probe):
            record.record(self.ev(tool="Write", tin={"file_path": "/a/b.py"}))
        probe.assert_called_once_with(self.tmp)


class SignalTest(RecordBase):
    def test_stuck_gated_to_action_tools_reads_never_arm(self):
        with mock.patch.object(record, "_git_dirty", return_value=False):
            record.record(self.ev(tool="Bash", tin={"command": "x"},
                                  resp={"stdout": "API Error: rate limit hit"}))
            self.assertEqual(self.counters()["stuck-signal"], 1)
            self.assertEqual(self.counters()["stuck-streak"], 1)
            record.record(self.ev(tool="Bash", tin={"command": "y"},
                                  resp="permission denied"))  # string resp tolerated
            self.assertEqual(self.counters()["stuck-streak"], 2)
            # a Read whose CONTENT mentions errors is data, not a signal
            record.record(self.ev(tool="Read",
                                  resp={"file": "docs on rate limits: API Error"}))
            self.assertEqual(self.counters()["stuck-signal"], 1)
            self.assertEqual(self.counters()["stuck-streak"], 2)
            record.record(self.ev(tool="Bash", tin={"command": "z"},
                                  resp={"stdout": "all good"}))
        self.assertEqual(self.counters()["stuck-signal"], 0)
        self.assertEqual(self.counters()["stuck-streak"], 0)

    def test_loop_thrash_hash_chain(self):
        with mock.patch.object(record, "_git_dirty", return_value=False):
            record.record(self.ev(tool="Bash", tin={"command": "make build"}))
            self.assertEqual(self.counters()["loop-streak"], 0)
            record.record(self.ev(tool="Bash", tin={"command": "make  build"}))
            self.assertEqual(self.counters()["loop-streak"], 1)  # ws-normalized
            record.record(self.ev(tool="Bash", tin={"command": "ls"}))
            self.assertEqual(self.counters()["loop-streak"], 0)
            record.record(self.ev(tool="Bash", tin={"command": "make build"}))
            self.assertEqual(self.counters()["loop-streak"], 1)  # A-B-A thrash
            for _ in range(record.HASH_WINDOW + 2):
                record.record(self.ev(tool="Bash", tin={"command": "ls"}))
        self.assertLessEqual(len(self.counters()["cmd-hash-chain"]),
                             record.HASH_WINDOW)


class ArtifactTest(RecordBase):
    def test_command_log_real_exit_codes_digests_never_raw(self):
        with mock.patch.object(record, "_git_dirty", return_value=False):
            record.record(self.ev(
                tool="Bash",
                tin={"command": "python3 -W error::ResourceWarning -m unittest "
                                "discover -s tests"},
                resp={"exitCode": 1, "stdout": "FAILED"}))
            record.record(self.ev(tool="Bash",
                                  tin={"command": "pytest -q secret_arg"},
                                  resp={"exitCode": 0}))
            # claude's success shape carries NO exit key: success IS exit 0
            # by construction (nonzero fires PostToolUseFailure, probed live)
            record.record(self.ev(tool="Bash", tin={"command": "just check"},
                                  resp={"stdout": "ok"}))
            record.record(self.ev(tool="Bash", tin={"command": "ls -la"},
                                  resp={"exitCode": 0}))  # not a runner
        rows = [json.loads(l) for l in
                self.artifact("command-log.jsonl").splitlines()]
        self.assertEqual([r["exit"] for r in rows], [1, 0, 0])
        self.assertIn("unittest", rows[0]["token"])
        self.assertEqual(rows[1]["token"], "pytest")
        self.assertEqual(rows[2]["token"], "just check")
        raw = self.artifact("command-log.jsonl")
        self.assertNotIn("secret_arg", raw)          # digest, never the raw line
        self.assertNotIn("discover -s tests", raw)
        self.assertTrue(all(len(r["digest"]) == 12 for r in rows))

    def test_command_log_persists_record_time_cwd_and_semantic_identity(self):
        root = os.path.join(self.tmp, "repo")
        script = os.path.join(root, "fab", "test", "focused-routing.sh")
        os.makedirs(os.path.dirname(script))
        with open(script, "w") as f:
            f.write("#!/bin/sh\n")
        event = self.ev(tool="Bash", tin={
            "command": "bash fab/test/focused-routing.sh", "cwd": root},
            resp={"exitCode": 1})
        with mock.patch.object(record, "_git_dirty", return_value=False):
            record.record(event)
        row = json.loads(self.artifact("command-log.jsonl"))
        identity = record.runner_identity("fab/test/focused-routing.sh", root)
        self.assertEqual(row["identity"], identity)
        self.assertEqual(row["cwd"], record._home_relative(root))
        shutil.rmtree(root)
        self.assertEqual(row["identity"],
                         record.runner_identity("bash fab/test/focused-routing.sh",
                                                root))

    def test_runner_identity_collapses_args_but_not_distinct_operations(self):
        root = os.path.join(self.tmp, "repo")
        self.assertEqual(record.runner_identity("pytest -q", root),
                         record.runner_identity("pytest tests", root))
        self.assertEqual(
            record.runner_identity("python3 -W error::ResourceWarning -m unittest x",
                                   root),
            record.runner_identity("python3 -u -m unittest y", root))
        self.assertNotEqual(record.runner_identity("python -m unittest", root),
                            record.runner_identity("python3 -m unittest", root))
        self.assertNotEqual(record.runner_identity("just check", root),
                            record.runner_identity("just test", root))
        self.assertNotEqual(record.runner_identity("cargo test", root),
                            record.runner_identity("cargo nextest", root))
        self.assertNotEqual(record.runner_identity("npm run zzz:qqq", root),
                            record.runner_identity("npm run aaa:bbb", root))
        self.assertNotEqual(record.runner_identity("npm run test:unit", root),
                            record.runner_identity("npm run test:e2e", root))
        self.assertEqual(record.runner_identity("cargo test --lib", root),
                         record.runner_identity("cargo test --doc", root))

    def test_edit_targets_basenames(self):
        with mock.patch.object(record, "_git_dirty", return_value=False):
            record.record(self.ev(tool="Edit", tin={"file_path": "/a/b/c.py"}))
            record.record(self.ev(tool="Write", tin={"file_path": "/d/e.md"}))
            record.record(self.ev(tool="Read", tin={"file_path": "/f/g.py"}))
        self.assertEqual(self.artifact("edit-targets.log"), "c.py\ne.md\n")

    def test_append_rotation_is_one_generation(self):
        with mock.patch.object(record, "LOG_MAX", 64), \
                mock.patch.object(record, "_git_dirty", return_value=False):
            for _ in range(4):
                record.record(self.ev(tool="Bash", tin={"command": "pytest"},
                                      resp={"exitCode": 0}))
        d = record.session_dir("sess-1")
        self.assertTrue(os.path.exists(os.path.join(d, "command-log.jsonl.1")))


class FailureEventTest(RecordBase):
    """The PostToolUseFailure leg (contract probed live 2026-07-19): nonzero
    Bash and failed tools land here with a top-level error, NO tool_response.
    A success-only recorder was blind to every failed run — every logged exit
    read -1 and stuck never armed off a failure."""

    def fev(self, tool="Bash", sid="sess-1", tin=None, error="Exit code 1",
            **extra):
        e = {"session_id": sid, "tool_name": tool, "cwd": self.tmp,
             "hook_event_name": record.FAIL_EVENT, "error": error,
             "is_interrupt": False}
        if tin is not None:
            e["tool_input"] = tin
        e.update(extra)
        return e

    def test_failed_runner_records_the_real_nonzero_exit(self):
        with mock.patch.object(record, "_git_dirty", return_value=False):
            record.record(self.fev(tin={"command": "pytest -q"},
                                   error="Exit code 2"))
            record.record(self.ev(tool="Bash", tin={"command": "pytest -q"},
                                  resp={"stdout": "ok"}))
        rows = [json.loads(l) for l in
                self.artifact("command-log.jsonl").splitlines()]
        self.assertEqual([r["exit"] for r in rows], [2, 0])

    def test_unparseable_failure_reads_exit_1_interrupt_reads_unknown(self):
        with mock.patch.object(record, "_git_dirty", return_value=False):
            record.record(self.fev(tin={"command": "pytest"},
                                   error="tool blew up"))
            record.record(self.fev(tin={"command": "pytest"},
                                   error="killed", is_interrupt=True))
        rows = [json.loads(l) for l in
                self.artifact("command-log.jsonl").splitlines()]
        self.assertEqual([r["exit"] for r in rows], [1, -1])

    def test_failure_error_text_arms_stuck(self):
        with mock.patch.object(record, "_git_dirty", return_value=False):
            record.record(self.fev(tin={"command": "gh api /x"},
                                   error="HTTP 401: not authenticated"))
        self.assertEqual(self.counters()["stuck-signal"], 1)
        self.assertEqual(self.counters()["stuck-streak"], 1)

    def test_failed_edit_is_not_forward_progress_and_lands_no_target(self):
        with mock.patch.object(record, "_git_dirty", return_value=False):
            record.record(self.ev(tool="Read"))
            record.record(self.fev(tool="Edit", tin={"file_path": "/a/b.py"},
                                   error="String to replace not found"))
        self.assertEqual(self.counters()["passive-streak"], 2)
        self.assertEqual(self.artifact("edit-targets.log"), "")

    def test_failed_commit_gets_no_commit_credit(self):
        with mock.patch.object(record, "_git_dirty", return_value=True):
            record.record(self.ev(tool="Edit", tin={"file_path": "/x.py"}))
            self.assertEqual(self.counters()["dirty-streak"], 1)
            record.record(self.fev(tin={"command": "git commit -m x"},
                                   error="Exit code 1"))
        self.assertEqual(self.counters()["dirty-streak"], 2)   # still dirty
        self.assertEqual(self.counters()["passive-streak"], 1)  # no forward credit


class CoordinationWriteTest(RecordBase):
    """task/2970: a lead's output is a filed task, a posted row, a revised
    store entry or a verdict, so those helm writes are FORWARD ops. Measured
    on a live lead: stalled-driver told it that it was circling after turns that filed four tasks, posted to chat and revised the store,
    because every one of those Bash calls counted as passive."""

    WRITES = (
        "helm task add 'cap the steer' --owner fixture-seat",
        "cd /tmp/fixture && helm chat post --room fixture-room <<'EOF'\n"
        "the lane is green\nEOF",
        "helm store revise fixture-id the corrected statement",
        "HELM_CHAT_NAME=fixture-seat helm dispatch verdict abc123 "
        "0123abcd --fix --measured evidence",
        "helm premise fixture-premise | a statement",
        "/opt/fixture/bin/helm task comment 12 a note",
        "helm chat dm peer-seat the fix is in",
        "helm reflex retire fixture-reflex",
    )
    READS = (
        "helm task show 12",
        "helm chat read --room fixture-room",
        "helm store get fixture-id",
        "helm dispatch list --open",
        "echo helm task add something",
        "grep -n 'helm chat post' notes.md",
        "helm premise-check fixture-id",
    )

    def test_a_coordination_write_is_a_forward_op(self):
        with mock.patch.object(record, "_git_dirty", return_value=False):
            record.record(self.ev(tool="Read"))
            self.assertEqual(self.counters()["passive-streak"], 1)  # unconditional
            record.record(self.ev(tool="Edit", tin={"file_path": "/x/y.py"}))
        self.assertEqual(self.counters()["passive-streak"], 0)
        for cmd in self.WRITES:
            with self.subTest(cmd=cmd[:40]), \
                    mock.patch.object(record, "_git_dirty", return_value=False):
                record.record(self.ev(tool="Read"))
                record.record(self.ev(tool="Read"))
                self.assertEqual(self.counters()["passive-streak"], 2)
                record.record(self.ev(tool="Bash", tin={"command": cmd}))
                self.assertEqual(self.counters()["passive-streak"], 0)

    def test_a_read_verb_or_a_mention_is_not(self):  # noqa: VACUOUS_ASSERTION — each count is asserted EQUAL to a rising positive n, which a dead counter fails
        with mock.patch.object(record, "_git_dirty", return_value=False):
            # CONTROL on the same observable: a write resets, so each
            # increment below is the verb table declining, not a dead rung.
            record.record(self.ev(tool="Bash", tin={"command": self.WRITES[0]}))
            self.assertEqual(self.counters()["passive-streak"], 0)
            for n, cmd in enumerate(self.READS, 1):
                with self.subTest(cmd=cmd):
                    record.record(self.ev(tool="Bash", tin={"command": cmd}))
                    self.assertEqual(self.counters()["passive-streak"], n)

    def test_a_failed_coordination_write_earns_nothing(self):
        with mock.patch.object(record, "_git_dirty", return_value=False):
            record.record(self.ev(tool="Read"))
            record.record({"session_id": "sess-1", "tool_name": "Bash",
                           "cwd": self.tmp, "hook_event_name": record.FAIL_EVENT,
                           "error": "Exit code 2", "is_interrupt": False,
                           "tool_input": {"command": self.WRITES[0]}})
        self.assertEqual(self.counters()["passive-streak"], 2)


class CounterLockSkipIsLoudTest(RecordBase):
    """A counters write skipped because the session dir's lock timed out or
    could not be taken leaves the swallow breadcrumb, and a timeout also
    marks the hook's latency span. A skipped write that nobody can see is a
    lost count nobody can diagnose."""

    def test_a_lock_timeout_leaves_a_breadcrumb_and_a_mark(self):
        import fcntl
        with mock.patch.object(record, "_git_dirty", return_value=False):
            record.record(self.ev(tool="Read"))
        self.assertEqual(self.counters()["passive-streak"], 1)   # control
        fd = os.open(record.session_dir("sess-1"), os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        with mock.patch.object(record, "COUNTERS_LOCK_WAIT_S", 0.05), \
                mock.patch("helm.hooklatency.mark") as mark:
            record.record(self.ev(tool="Read"))
        self.assertEqual(self.counters()["passive-streak"], 1,
                         "the write is skipped, never made unserialized")
        rows = [r for r in record.swallows()
                if r.get("where") == "record._counters_locked"]
        self.assertEqual([r["exc"] for r in rows], ["TimeoutError"])
        mark.assert_any_call("timeout")

    def test_a_lock_failure_leaves_a_breadcrumb(self):
        sd = record.session_dir("sess-lockfail")
        os.makedirs(os.path.dirname(sd), exist_ok=True)
        with open(sd, "w") as f:                     # a file where the dir goes
            f.write("x")
        record.record(self.ev(tool="Read", sid="sess-lockfail"))
        rows = [r for r in record.swallows()
                if r.get("where") == "record._counters_locked"]
        self.assertEqual(len(rows), 1)
        self.assertIn(rows[0]["exc"], ("FileExistsError", "NotADirectoryError"))


class ParsedForwardOpTest(RecordBase):
    """task/2970: forward credit is decided by the PARSED operation, the tool
    name for a native tool and the git subcommand after git's global options
    for a shell command, never by a loose text search. Native task writes and
    a lease claim are durable work; `git grep ... commit` is a read."""

    def streaks(self, *events):
        """(passive-streak, dirty-streak) of a FRESH session after an edit,
        two reads and `events`."""
        self.n = getattr(self, "n", 0) + 1
        sid = "sess-parsed-%d" % self.n
        with mock.patch.object(record, "_git_dirty", return_value=True):
            record.record(self.ev(tool="Edit", sid=sid,
                                  tin={"file_path": "/x/y.py"}))
            record.record(self.ev(tool="Read", sid=sid))
            record.record(self.ev(tool="Read", sid=sid))
            for e in events:
                record.record(dict(e, session_id=sid))
        c = self.counters(sid)
        return c["passive-streak"], c["dirty-streak"]

    def test_native_task_writes_are_forward_ops(self):
        self.assertEqual(self.streaks(), (2, 3))    # control: two reads after an edit
        for tool in ("TaskCreate", "TaskUpdate"):
            with self.subTest(tool=tool):
                self.assertEqual(self.streaks(self.ev(
                    tool=tool, tin={"subject": "cap the steer"}))[0], 0)

    def test_a_lease_claim_is_a_forward_op(self):
        self.assertEqual(self.streaks(), (2, 3))    # control: two reads after an edit
        self.assertEqual(self.streaks(self.ev(tool="Bash", tin={
            "command": "helm chat claim lane/fixture --ttl 600"}))[0], 0)

    def test_a_git_grep_for_commit_is_not_a_commit(self):
        self.assertEqual(self.streaks(), (2, 3))    # control: two reads after an edit
        passive, dirty = self.streaks(self.ev(tool="Bash", tin={
            "command": "git grep -n commit -- docs"}))
        self.assertEqual((passive, dirty), (3, 4))

    def test_a_commit_behind_git_global_options_is_a_commit(self):
        """CONTROL for the arm above: the parse finds the real subcommand."""
        self.assertEqual(self.streaks(), (2, 3))    # control: two reads after an edit
        for cmd in ("git -c user.name=akapug -c user.email=a@c.example commit -q -F m",
                    "cd /tmp/fixture && git -C /tmp/fixture commit -m x",
                    "git --no-pager commit --amend --no-edit"):
            with self.subTest(cmd=cmd[:30]):
                self.assertEqual(self.streaks(self.ev(
                    tool="Bash", tin={"command": cmd})), (0, 0))


class CommitShapesTest(RecordBase):
    """task/2980 lane 5, L0: the commit shapes the fleet really types are
    commits, and text that only NAMES a commit is not.

    The per-line parser before this credited 62 of 165 real local commits in
    one window of the fleet's transcripts: any quoted argument that
    spans lines (`git commit -q -m "subject<NL><NL>body"`, the ordinary form)
    made its first line unreadable. It also credited heredoc BODIES as
    commands, so a script written with `cat > x.sh <<'EOF'` that names
    `git commit` read as a commit. Each arm here is a shape from that corpus.
    """

    # the same fresh-session reader the forward-op arms use, not a subclass,
    # so their tests do not run twice
    streaks = ParsedForwardOpTest.streaks

    MUST_HIT = (
        'cd /tmp/fixture && git add X && GIT_AUTHOR_NAME="A B" '
        'git commit -q -m "subject\n\nbody line"',
        "export GIT_AUTHOR_NAME=x\ngit add a b &&\n"
        "  git commit -q -m \"subject\n\nbody\"",
        "git commit -m one -m 'two\nlines'",
        "git add f && printf '%s\\n\\n%s\\n' 'subject' 'body\nmore' "
        "| git commit -F -",
        "git commit -F - <<'EOF'\nsubject with a don't in it\nEOF",
        "git commit -m \"$(cat <<'EOF'\nsubject\n\nit's (really) done, "
        "a 5\" screen\nEOF\n)\"",
        "git -c user.name=akapug -c user.email=a@b.c commit -q -F m",
        "git commit --amend --no-edit",
        "git -C /tmp/fixture commit --file=msg.txt",
        "env -u GIT_AUTHOR_NAME -u GIT_COMMITTER_NAME git commit -q -F - "
        "<<'EOF' 2>&1 | tail -3\nsubject\nEOF",
        'echo "ok $(git log -1 --format="%h %s")"; git commit -m x',
        "# don't forget the body\ngit commit -m x",
    )
    MUST_MISS = (
        "git grep -n commit",
        "git log --grep=commit",
        "cat > notes.md <<'EOF'\nthen git commit it\nEOF",
        "cat > run.sh <<EOF\ngit commit -q -F m\nEOF",
        'echo "git commit"',
        "helm chat post --room r 'ran git commit -m x'",
        'echo "unclosed git commit',
        "git commit -q -m probe --dry-run",
        "git commit-tree $T -p $B -F m",
        "ssh host 'cd x && git commit -q -m y'",
    )

    def test_every_real_shape_is_a_commit(self):
        self.assertTrue(record.git_commit(self.MUST_HIT[0]),
                        "control: the multi-line -m commit is not credited")
        self.assertEqual(len(self.MUST_HIT), 12, "a shape's arm was dropped")
        missed = [c for c in self.MUST_HIT if not record.git_commit(c)]
        self.assertEqual(missed, [], "real commits earned no credit")

    def test_text_that_only_names_a_commit_is_not_one(self):
        self.assertTrue(record.git_commit("git commit -m x"),
                        "control: a plain commit is not credited")
        self.assertEqual(len(self.MUST_MISS), 10, "a look-alike was dropped")
        credited = [c for c in self.MUST_MISS if record.git_commit(c)]
        self.assertEqual(credited, [], "a non-commit earned commit credit")

    def test_a_deeply_nested_substitution_never_raises(self):
        """The substitution walk keeps its own stack: 5,000 nested `$(`
        raised RecursionError in the recursive version."""
        self.assertIs(record.git_commit(
            "git commit -m " + "$(" * 5000 + "x" + ")" * 5000), True)
        self.assertIs(record.git_commit(
            "echo " + "$(" * 5000 + "git log" + ")" * 5000), False)

    def test_a_multi_line_commit_resets_the_dirty_streak(self):
        """End to end, through the recorder: the counter the
        uncommitted-drift reflex reads."""
        self.assertEqual(self.streaks(), (2, 3))    # control: reads after an edit
        self.assertEqual(self.streaks(self.ev(tool="Bash", tin={
            "command": self.MUST_HIT[0]})), (0, 0))
        self.assertEqual(self.streaks(self.ev(tool="Bash", tin={
            "command": self.MUST_MISS[2]})), (3, 4))


class CounterRaceTest(RecordBase):
    """task/2970: turn_open (the per-turn hook) and
    _record (every tool call, a subagent's included) both read, change and
    rewrite counters.json. Unserialized, a prompt arriving while a subagent's
    edit lands can erase that edit's credit and add a stalled turn that never
    happened, or erase the turn boundary itself.

    Each arm RACES THE TWO REAL WRITERS: at the moment the first writer is
    about to write, the second starts in a thread and gets 0.3s to finish
    first. Unserialized, it does, and the first writer's stale copy lands on
    top; serialized, the second waits for the first and reads its result."""

    def race(self, victim, racer):
        real = pk.write_json
        state = {"armed": True, "thread": None}

        def wrapped(path, obj):
            if state["armed"] and path.endswith("counters.json"):
                state["armed"] = False
                t = threading.Thread(target=racer)
                t.start()
                state["thread"] = t
                t.join(0.3)
            return real(path, obj)
        with mock.patch.object(pk, "write_json", wrapped), \
                mock.patch.object(record, "_git_dirty", return_value=False):
            victim()
            self.assertIsNotNone(state["thread"], "the race never started")
            state["thread"].join(5)
        self.assertFalse(state["thread"].is_alive())

    def ev_as(self, tool, agent_id=None):
        e = self.ev(tool=tool, tin={"file_path": "/x/y.py"})
        if agent_id:
            e["agent_id"] = agent_id
        return e

    def test_a_prompt_racing_a_subagents_edit_keeps_the_credit(self):
        with mock.patch.object(record, "_git_dirty", return_value=False):
            record.record(self.ev_as("Edit"))
            record.turn_open("sess-1", "typed turn")
            for _ in range(5):
                record.record(self.ev_as("Read"))
                record.turn_open("sess-1", "typed turn")
            record.record(self.ev_as("Read"))
        self.assertEqual(self.counters()["stalled-turns"], 5)   # control
        self.race(lambda: record.turn_open("sess-1", "typed turn"),
                  lambda: record.record(self.ev_as("Edit", "afixture01")))
        self.assertEqual(self.counters()["stalled-turns"], 0,
                         "a forward edit landed during the boundary; its "
                         "credit must survive and no stalled turn be added")

    def test_a_call_racing_a_prompt_keeps_the_turn_boundary(self):
        with mock.patch.object(record, "_git_dirty", return_value=False):
            record.record(self.ev_as("Edit"))
            record.turn_open("sess-1", "typed turn")
            record.record(self.ev_as("Read"))
        self.assertEqual(self.counters()["stalled-turns"], 0)   # control
        self.race(lambda: record.record(self.ev_as("Read")),
                  lambda: record.turn_open("sess-1", "typed turn"))
        self.assertEqual(self.counters()["stalled-turns"], 1,
                         "the turn that closed during the call must count")


class GitProbeUnknownTest(RecordBase):
    def test_git_probe_failure_keeps_the_cached_dirty_state(self):
        # timeout/git-error is UNKNOWN, not clean — the cache stands
        with mock.patch.object(record, "_git_dirty", return_value=True):
            record.record(self.ev(tool="Edit", tin={"file_path": "/x.py"}))
        self.assertEqual(self.counters()["last-dirty"], 1)
        with mock.patch.object(record, "_git_dirty", return_value=None):
            record.record(self.ev(tool="Bash", tin={"command": "ls"}))
        self.assertEqual(self.counters()["last-dirty"], 1)
        self.assertEqual(self.counters()["dirty-streak"], 2)


class KeyingAndFailOpenTest(RecordBase):
    def test_state_keyed_by_session_id(self):
        with mock.patch.object(record, "_git_dirty", return_value=False):
            record.record(self.ev(tool="Read", sid="alpha"))
            record.record(self.ev(tool="Read", sid="alpha"))
            record.record(self.ev(tool="Read", sid="beta"))
        self.assertEqual(record.counters("alpha")["passive-streak"], 2)
        self.assertEqual(record.counters("beta")["passive-streak"], 1)
        self.assertEqual(record.session_key("a/b c!"), "a_b_c_")

    def test_parse_event_gates_keyless_payloads(self):
        self.assertIsNone(record.parse_event("not json{"))
        self.assertIsNone(record.parse_event('["list"]'))
        self.assertIsNone(record.parse_event(json.dumps({"tool_name": "Read"})))
        self.assertIsNone(record.parse_event(json.dumps({"session_id": "s"})))
        e = record.parse_event(json.dumps(self.ev()))
        self.assertEqual(e["tool_name"], "Read")

    def test_cmd_record_silent_rc0_on_any_payload(self):
        rc, out, err = self.run_cmd(["--hook-json"], "garbled{{{")
        self.assertEqual((rc, out, err), (0, "", ""))
        rc, out, err = self.run_cmd([], json.dumps(self.ev(tool="Grep")))
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertEqual(self.counters()["passive-streak"], 1)

    def test_apostrophe_payload_records_clean(self):
        # the ancestor's bug class: tool content with quotes broke a shell
        # python -c wrapper into a silent noop. Pure argv verb: must record.
        cmd = "echo 'it'\\''s \"quoted\" — $weird `stuff`'"
        with mock.patch.object(record, "_git_dirty", return_value=False):
            rc, out, err = self.run_cmd(
                ["--hook-json"], json.dumps(self.ev(tool="Bash",
                                                    tin={"command": cmd})))
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertEqual(self.counters()["last-tool"], "Bash")

    def test_fail_open_when_state_unwritable(self):
        with mock.patch.object(pk, "write_json", side_effect=OSError("disk full")):
            record.record(self.ev())  # must not raise
        rc, out, err = self.run_cmd([], json.dumps(self.ev()))
        self.assertEqual(rc, 0)

    def test_unknown_subverb_usage(self):
        rc, _, err = self.run_cmd(["bogus"])
        self.assertEqual(rc, 2)
        self.assertIn("usage:", err)


class WiringBase(RecordBase):
    """test_hooks' hermetic homes/configs re-pointing, for install/status/doctor."""

    def setUp(self):
        super().setUp()
        j = lambda *p: os.path.join(self.tmp, *p)
        self._homes_orig = {k: getattr(homes, k) for k in ("ROOTS", "DEFAULTS")}
        homes.ROOTS = {"claude": j("claude-homes"), "codex": j("codex-homes")}
        homes.DEFAULTS = {"claude": j("default-claude"), "codex": j("default-codex")}
        os.makedirs(homes.ROOTS["claude"])
        os.makedirs(homes.DEFAULTS["claude"])
        self._cfg_orig = (configs.HOME_ROOTS, configs.BACKUP_DIR)
        configs.HOME_ROOTS = [homes.DEFAULTS["claude"]]
        configs.BACKUP_DIR = j("backups")

    def tearDown(self):
        for k, v in self._homes_orig.items():
            setattr(homes, k, v)
        configs.HOME_ROOTS, configs.BACKUP_DIR = self._cfg_orig
        super().tearDown()

    def mk_home(self, name, settings=None):
        d = os.path.join(homes.ROOTS["claude"], name)
        os.makedirs(d, exist_ok=True)
        configs.HOME_ROOTS.append(d)
        if settings is not None:
            with open(os.path.join(d, "settings.json"), "w") as f:
                json.dump(settings, f, indent=2)
        return d

    def read_settings(self, d):
        with open(os.path.join(d, "settings.json"), encoding="utf-8") as f:
            return json.load(f)


class WiringTest(WiringBase):
    def test_hook_command_fail_open_and_resolvable(self):
        cmd = record.hook_command()
        self.assertEqual(shlex.split(cmd)[0], hooks.wrapper_bin(), cmd)
        # the CONTRACT (never blocks), not the idiom it was spelled with: the
        # wrapper's KIND operand decides it, and only `gate` propagates rc 2.
        self.assertEqual(shlex.split(cmd)[1], "lane", cmd)
        self.assertTrue(hooks._fail_open(cmd), cmd)
        self.assertIn("record --hook-json", cmd)
        self.assertIn(hooks.helm_bin(), cmd)
        self.assertTrue(record._resolvable(cmd))
        self.assertFalse(record._resolvable("/gone/helm record --hook-json"))

    def test_install_covers_homes_idempotent_merge_preserving(self):
        a = self.mk_home("a-user-example", settings={
            "model": "opus",
            "hooks": {
                "UserPromptSubmit": [{"hooks": [
                    {"type": "command", "command": hooks.hook_command()}]}],
                "PostToolUse": [{"matcher": "Bash", "hooks": [
                    {"type": "command", "command": "echo post"}]}]}})
        rc, out, err = self.run_cmd(["install"])
        self.assertEqual(rc, 0, err)
        self.assertIn("2 of 2 claude homes covered", out)
        got = self.read_settings(a)
        self.assertEqual(got["model"], "opus")  # foreign keys survive
        self.assertEqual(got["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"],
                         hooks.hook_command())  # inject entry survives
        cmds = record._event_cmds(got)
        self.assertIn("echo post", cmds)        # foreign PostToolUse survives
        self.assertIn(record.hook_command(), cmds)
        self.assertEqual(got["hooks"]["PostToolUse"][0]["matcher"], "Bash")
        rc, out, _ = self.run_cmd(["install"])  # idempotent
        self.assertEqual(rc, 0)
        self.assertIn("hook up to date", out)
        self.assertEqual(len([c for c in record._event_cmds(self.read_settings(a))
                              if record._ours(c)]), 1)

    def test_install_wires_both_event_legs(self):
        a = self.mk_home("a-user-example")
        rc, _, err = self.run_cmd(["install"])
        self.assertEqual(rc, 0, err)
        got = self.read_settings(a)
        for ev in record.HOOK_EVENTS:
            self.assertIn(record.hook_command(ev), record._event_cmds(got, ev))
        # a success-only home is a coverage gap the installer closes
        del got["hooks"][record.FAIL_EVENT]
        with open(os.path.join(a, "settings.json"), "w") as f:
            json.dump(got, f, indent=2)
        row = next(r for r in record.status_rows() if r["path"] == a)
        self.assertFalse(row["hook"])
        rc, _, _ = self.run_cmd(["install"])
        self.assertEqual(rc, 0)
        row = next(r for r in record.status_rows() if r["path"] == a)
        self.assertTrue(row["hook"])

    def test_install_rederives_both_legs_after_foreign_conflict(self):
        a = self.mk_home("a-user-example", settings={"foreign": {"before": True}})
        real = configs.write_file
        calls = []

        def conflict_once(path, content, expected_revision=None):
            calls.append(expected_revision)
            if len(calls) == 1:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                data["foreignAfterPlan"] = [1, 2, 3]
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                return {"error": "conflict", "code": "conflict"}
            return real(path, content, expected_revision=expected_revision)

        with mock.patch.object(configs, "write_file", side_effect=conflict_once):
            action, detail = record.install_home(a)
        self.assertEqual(action, "add")
        self.assertIn("CAS attempts: 2", detail)
        got = self.read_settings(a)
        self.assertEqual(got["foreign"], {"before": True})
        self.assertEqual(got["foreignAfterPlan"], [1, 2, 3])
        for ev in record.HOOK_EVENTS:
            self.assertIn(record.hook_command(ev), record._event_cmds(got, ev))

    def test_install_dry_writes_nothing(self):
        self.mk_home("a-user-example")
        rc, out, _ = self.run_cmd(["install", "--dry"])
        self.assertEqual(rc, 0)
        self.assertIn("dry — nothing written", out)
        self.assertIn("record --hook-json", out)
        self.assertFalse(os.path.exists(
            os.path.join(homes.DEFAULTS["claude"], "settings.json")))

    def test_status_and_doctor_rows(self):
        self.mk_home("a-user-example")
        rows = record.doctor_rows()
        self.assertEqual(rows[0][0], doctor.WARN)
        self.assertIn("record coverage: 0 of 2", rows[0][1])
        self.assertIn("helm record install", rows[0][1])
        rc, _, _ = self.run_cmd(["install"])
        self.assertEqual(rc, 0)
        rows = record.doctor_rows()
        self.assertEqual(rows[0], (doctor.OK, "record coverage: 2 of 2 claude homes"))
        self.assertEqual(rows[1][0], doctor.WARN)  # wired but never fired
        self.assertIn("no PostToolUse event", rows[1][1])
        with mock.patch.object(record, "_git_dirty", return_value=False):
            record.record(self.ev(tool="Read", sid="live-sess"))
        rows = record.doctor_rows()
        self.assertEqual(rows[1][0], doctor.OK)
        self.assertIn("reflex-state: 1 session", rows[1][1])
        rc, out, _ = self.run_cmd(["status"])
        self.assertEqual(rc, 0)
        self.assertIn("wiring: 2 of 2 claude homes", out)
        self.assertIn("live-sess", out)


class RecorderLadderCensusTest(WiringBase):
    """THE RECORDER'S OWN COVERAGE WAS BLIND TO THE FILE IT EXECS.

    `record.hook_command()` renders `… bin/helm-hook lane record PostToolUse
    10 '…' … bin/helm record --hook-json`, so every PostToolUse recorder in
    the estate runs through that wrapper. `status_rows` built its row from
    command, resolvability and the fail-open tail — all three of which a home
    whose `bin/helm-hook` is GONE still satisfies — and `coverage()` counted
    exactly those three. Measured before the cure, in a temp HOME with the
    ladder moved aside: `hooks.inject_covered` correctly read 0 of 2 while
    `record.coverage()` read 2 of 2 and `helm doctor` printed `[OK] record
    coverage: 2 of 2 claude homes` over an estate where every recorder exits
    127 and the harness reads ALLOW.

    ONE MEASUREMENT, NOT A SECOND COPY: the row calls `hooks._wrapper_state`,
    the same function `hooks._gap_row` calls, so the two censuses cannot
    answer differently about one home again.

    THE LADDER IS A REAL FILE HERE. `hooks.helm_bin` is pinned to a planted
    checkout so the installed commands name a wrapper this arm owns, and the
    wrapper is copied, removed and chmodded on disk — nothing about the
    measurement is mocked.
    """

    WRAPPER = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "bin", "helm-hook")

    def setUp(self):
        super().setUp()
        self.checkout = os.path.join(self.tmp, "checkout", "bin")
        os.makedirs(self.checkout)
        self.helm = os.path.join(self.checkout, "helm")
        with open(self.helm, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(self.helm, 0o755)
        patch = mock.patch.object(hooks, "helm_bin", return_value=self.helm)
        patch.start()
        self.addCleanup(patch.stop)
        self.wrapper = os.path.join(self.checkout, hooks.HOOK_WRAPPER)
        self.ladder(True)
        self.mk_home("a-user-example")
        rc, _, err = self.run_cmd(["install"])
        self.assertEqual(rc, 0, err)

    def ladder(self, present, mode=0o755):
        if present:
            shutil.copy2(self.WRAPPER, self.wrapper)
            os.chmod(self.wrapper, mode)
        elif os.path.exists(self.wrapper):
            os.remove(self.wrapper)

    def test_a_home_whose_ladder_is_gone_is_not_recorder_coverage(self):
        rows = record.status_rows()
        self.assertEqual(len(rows), 2, "MUST-HIT: the census saw no homes")
        # CONTROL, on the same homes and the same reader: with the ladder on
        # disk every one of them counts, so the zeros below are about the file.
        self.assertEqual([r["wrapper"] for r in rows], [True, True])
        self.assertEqual(record.coverage(), (2, 2))

        self.ladder(False)
        self.assertEqual([r["wrapper"] for r in record.status_rows()],
                         [False, False],
                         "the row cannot see a ladder that is not on disk")
        self.assertEqual(record.coverage(), (0, 2),
                         "a home whose every recorder exits 127 counted as "
                         "recorder coverage")

        self.ladder(True, mode=0o644)
        self.assertEqual(record.coverage(), (0, 2),
                         "a ladder that is PRESENT and not executable exits "
                         "126, which is no more covered than 127")

        self.ladder(True)
        self.assertEqual(record.coverage(), (2, 2),
                         "restoring the ladder did not restore the count, so "
                         "the reading is not about the file")

    def test_doctor_stops_saying_OK_and_names_the_repair_it_cannot_make(self):
        self.assertEqual(record.doctor_rows()[0],
                         (doctor.OK, "record coverage: 2 of 2 claude homes"))

        self.ladder(False)
        rows = record.doctor_rows()

        self.assertEqual(rows[0][0], doctor.WARN)
        self.assertIn("0 of 2", rows[0][1])
        said = [m for lvl, m in rows
                if lvl == doctor.WARN and hooks.HOOK_WRAPPER in m]
        self.assertTrue(said, "doctor's record rung is silent about the "
                              "missing ladder: %r" % (rows,))
        self.assertIn("a-user-example", said[0],
                      "the sentence does not name the home to repair")
        # `helm record install` REWRITES ENTRIES; it cannot put back a file
        # that is not in the checkout those entries name, so the reader is
        # owed the repair that works rather than only the one that does not.
        self.assertIn("helm hooks install", said[0])

    def test_status_empty_state(self):
        rc, out, _ = self.run_cmd(["status"])
        self.assertEqual(rc, 0)
        self.assertIn("no reflex-state yet", out)


if __name__ == "__main__":
    unittest.main()


class EditPathsCarryTheDirectoryTest(RecordBase):
    """A basename cannot say WHERE a write went, and that is the only question
    a tool-boundary whisper needs. Live 2026-07-26: five durable lessons went
    into a private memory dir instead of `helm store`, the same lesson was
    taught to a teammate twice because the fleet never saw them, and nothing
    could notice — every write had been reduced to a filename before any
    watcher saw it."""

    def _write(self, path, failed=False):
        e = self.ev(tool="Write", tin={"file_path": path},
                    resp={} if not failed else None)
        if failed:
            e["hook_event_name"] = record.FAIL_EVENT
            e["error"] = "Exit code 1"
        rc, _o, _r = self.run_cmd(["--hook-json"], json.dumps(e))
        self.assertEqual(rc, 0)

    def _lines(self, name, sid="sess-1"):
        fp = os.path.join(record.session_dir(sid), name)
        if not os.path.exists(fp):
            return []
        return [l for l in open(fp).read().splitlines() if l.strip()]

    def test_the_directory_survives_into_edit_paths(self):
        p = os.path.join(self.tmp, "sub", "deep", "lesson.md")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        self._write(p)
        got = self._lines("edit-paths.log")
        self.assertTrue(got, "edit-paths.log was not written")
        self.assertIn("deep", got[-1], "the directory was discarded")
        self.assertIn("lesson.md", got[-1])

    def test_edit_targets_is_UNCHANGED_basenames_only(self):
        """Two live readers (seats.py's verify rung, handoff.py's snapshot)
        expect bare basenames; widening that file would break both."""
        p = os.path.join(self.tmp, "sub", "deep", "lesson.md")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        self._write(p)
        got = self._lines("edit-targets.log")
        self.assertEqual(got[-1], "lesson.md")
        self.assertNotIn("/", got[-1])

    def test_a_home_path_records_tilde_relative(self):
        self.assertTrue(
            record._home_relative(os.path.join(os.path.expanduser("~"), "x.md"))
            .startswith("~/"),
            "a home path must record tilde-relative, never absolute")

    def test_a_FAILED_edit_records_neither(self):
        """A failed edit landed nowhere and must not ground anything."""
        p = os.path.join(self.tmp, "sub", "nope.md")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        self._write(p, failed=True)
        self.assertEqual(self._lines("edit-paths.log"), [])
        self.assertEqual(self._lines("edit-targets.log"), [])


class SwallowLedgerAbsentVersusUnreadableTest(RecordBase):
    """The swallow ledger's own failure: an instrument that went 100 percent
    noise, and the arms that keep it able to go loud again.

    WHAT WAS MEASURED. The live ledger held 3,976 records. Every one of them
    was a FileNotFoundError, from exactly two sites — `_runner_latest` and
    `_edited_code` — on the per-session state files a seat has simply not
    written yet. Behind it sat a rotated file of 4,379 more in the same
    shape. So the census was not a real signal buried under noise; there was
    NO SIGNAL LEFT, and every future eaten error would land in a list already
    entirely full. Two readers polled on every stop of every seat, and a
    normal negative was spelled the same as a defect.

    WHY BOTH ARMS RUN IN ONE CALL. "The ledger stayed empty" is the exact
    assertion a ledger that records NOTHING AT ALL also passes — a cure that
    broke `swallow` outright, or a fixture whose home was pointed somewhere
    unwritable, would look identical to a cure that worked. So the silence
    arm is worthless alone and is never written alone: the unreadable arm
    below is an UNCONDITIONAL positive control on the SAME observable, read
    through the same `record.swallows`, in the same test.

    WHY THE UNREADABLE ARM USES A DIRECTORY rather than chmod. A permission
    bit is not a fact about the filesystem when the suite runs as root, where
    a 000 file opens fine and the arm passes vacuously. A directory standing
    where a file belongs refuses to open for every uid, so the arm measures
    the same thing for every runner.
    """

    def _read_both(self, session):
        """Both cured readers, so one call covers both sites."""
        return (seats_stop_signals._runner_latest(session),
                seats_stop_signals._edited_code(session))

    def test_absent_leaves_no_breadcrumb_while_unreadable_still_records(self):  # noqa: VACUOUS_ASSERTION — ARM 2 is the unconditional control for latest/edited (populated session, non-empty returns) and ARM 3 for record.swallows (unreadable state, rows recorded); no absence here is unpaired
        # ARM 1 — NEGATIVE: a session that has written no state yet. This is
        # the ordinary condition of every fresh seat, and of a read-only seat
        # forever. It must cost the defect channel nothing.
        latest, edited = self._read_both("sess-never-wrote")
        self.assertEqual(latest, {})
        self.assertEqual(edited, [])
        self.assertEqual(record.swallows(limit=50), [],
                         "an expected absence must leave no swallow row")

        # ARM 2 — POSITIVE CONTROL FOR THE RETURN VALUES. `latest` and
        # `edited` are asserted EMPTY in every other arm, and an assertion that
        # only ever reads empty is satisfied by a reader that has stopped being
        # able to read anything at all. So: one session whose state IS there
        # and IS readable, which also pins that the cure left the happy path
        # alone. The ledger must STILL be silent — a readable file is neither
        # an absence nor a defect.
        d = record.session_dir("sess-wrote")
        os.makedirs(d)
        token = "fab/test/run.sh"
        with open(os.path.join(d, "command-log.jsonl"), "w") as f:
            f.write(json.dumps({"token": token, "exit": 0, "digest": "d1",
                                "identity": record.runner_identity(token)})
                    + "\n")
        with open(os.path.join(d, "edit-targets.log"), "w") as f:
            f.write("record.py\n")
        latest, edited = self._read_both("sess-wrote")
        self.assertEqual(len(latest), 1)
        self.assertEqual(next(iter(latest.values()))["exit"], 0)
        self.assertEqual(edited, ["record.py"])
        self.assertEqual(record.swallows(limit=50), [],
                         "a state file that reads fine is not a swallow")

        # ARM 3 — POSITIVE CONTROL FOR THE LEDGER, unconditional, same call.
        # The two paths now EXIST and cannot be read. If this arm is silent,
        # arm 1 proved nothing: a ledger that records NOTHING AT ALL — a broken
        # `swallow`, a home pointed somewhere unwritable — passes arm 1 too.
        d = record.session_dir("sess-unreadable")
        os.makedirs(os.path.join(d, "command-log.jsonl"))
        os.makedirs(os.path.join(d, "edit-targets.log"))
        latest, edited = self._read_both("sess-unreadable")
        # THE RETURN VALUE IS DELIBERATELY UNCHANGED. Both rungs stay
        # fail-closed; only the LEDGER learned the difference, so an
        # unreadable log must not start arming a stop rung it never armed.
        self.assertEqual(latest, {})
        self.assertEqual(edited, [])
        # READ `record.swallows` INLINE, not through a local. The rung pairs a
        # control to an absence by the observable ROOT each assertion names,
        # and binding the rows to a variable first leaves the control naming
        # `rows` while the silence arm above names `record` — a real control
        # that no longer reads as one.
        self.assertEqual(
            sorted(r["where"] for r in record.swallows(limit=50)),
            ["seats_stop_signals._edited_code",
             "seats_stop_signals._runner_latest"],
            "an unreadable state file is a DEFECT and must still record")
        self.assertEqual(
            sorted({r["exc"] for r in record.swallows(limit=50)}),
            ["IsADirectoryError"])

    def test_unwritten_state_answers_true_ONLY_for_nothing_is_there(self):  # noqa: VACUOUS_ASSERTION — the assertIs(..., True) arm on FileNotFoundError is the unconditional control, same predicate, same call
        """The predicate is narrow ON PURPOSE, and the cost is asymmetric: too
        WIDE loses a real eaten error forever, too NARROW costs one noisy row.
        So every neighbour of ENOENT is pinned to False, including the OSError
        siblings that are the tempting ones to wave through."""
        # THE POSITIVE CONTROL COMES FIRST HERE and is unconditional: if the
        # predicate answered False for everything the False loop below would
        # pass vacuously, so the True arm is what gives it meaning.
        self.assertIs(record.unwritten_state(
            FileNotFoundError(2, "No such file or directory")), True)
        for exc in (IsADirectoryError(21, "Is a directory"),
                    PermissionError(13, "Permission denied"),
                    NotADirectoryError(20, "Not a directory"),
                    OSError(5, "Input/output error"),
                    ValueError("almost always ours"),
                    TypeError("almost always ours"),
                    KeyError("shape drift"),
                    UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")):
            self.assertIs(record.unwritten_state(exc), False,
                          "%s is UNREADABLE, not absent" % type(exc).__name__)


class SwallowBreadcrumbTest(RecordBase):
    """The nine WIDE silent handlers, and the trace they now leave.

    THE INCIDENT: a 2-tuple returned against a 3-tuple unpack raised
    ValueError inside _spiral_gate, a broad handler ate it, the spiral BLOCK
    came back EMPTY, and a fleet guard was disarmed for twenty minutes with a
    green suite and no record anywhere. The swallow is CORRECT — a stop-whisper
    that crashes a seat's stop is worse than one that goes quiet — so the cure
    is not to raise. It is to leave a trace.
    """

    def test_swallow_writes_the_ROW_not_merely_a_silent_success(self):
        """assert-the-effect: an except-pass around a write plus an assertion
        that nothing raised is a guaranteed vacuous pass, because the swallow
        makes not-raising unconditional. So this reads the row back."""
        from helm import record
        try:
            a, b, c = (1, 2)
        except Exception as e:
            record.swallow("probe.site_one", e)
        rows = record.swallows(limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["where"], "probe.site_one")
        self.assertEqual(rows[0]["exc"], "ValueError")
        self.assertIn("unpack", rows[0]["msg"])

    def test_a_long_swallow_message_is_cut_SAYING_SO(self):
        """The breadcrumb is the ONLY copy of an eaten exception. A prefix
        that reads as the whole message sends a diagnosis wrong on the one
        file written precisely because nothing else was recorded."""
        from helm import record
        msg = "the adapter returned a row it will not name " * 20
        where = "probe." + "deep.nested.call." * 20
        record.swallow(where, ValueError(msg))
        row = record.swallows(limit=10)[0]
        self.assertEqual(row["msg"], pk.cut_marked(msg, 200))
        self.assertIn("[cut: 200 of %d chars]" % len(msg), row["msg"])
        self.assertIn("[cut: 120 of %d chars]" % len(where), row["where"])

    def test_a_short_swallow_message_carries_no_mark(self):
        """The must-hit control: under the bound both fields are exactly what
        the handler was handed, so a mark means a real loss."""
        from helm import record
        record.swallow("probe.site", ValueError("real"))
        row = record.swallows(limit=10)[0]
        self.assertEqual(row["where"], "probe.site")
        self.assertEqual(row["msg"], "real")

    def test_swallow_NEVER_raises_because_it_runs_inside_an_except_handler(self):
        """A breadcrumb that can raise converts a silently-swallowed bug into a
        crash ON THE STOP PATH — strictly worse than the disease. The hostile
        input is an exception whose __str__ itself raises."""
        from helm import record

        class Unstringable(Exception):
            def __str__(self):
                raise RuntimeError("cannot render")

        record.swallow("probe.hostile", Unstringable())     # must not raise
        # POSITIVE CONTROL on the same observable: a NORMAL swallow still
        # records, so the arm above cannot pass by the writer being inert.
        record.swallow("probe.normal", ValueError("real"))
        rows = record.swallows(limit=10)
        self.assertEqual([r["where"] for r in rows], ["probe.normal"])

    def test_a_missing_log_reads_OK_but_an_unreadable_one_reads_UNKNOWN(self):
        """missing-evidence-is-not-evidence-against: absence and cannot-look
        are different facts, and collapsing them makes an unreadable log
        indistinguishable from a clean estate."""
        from helm import record
        levels = [lvl for lvl, _ in record._swallow_rows()]
        self.assertEqual(levels, ["OK"])                      # nothing written yet
        record.swallow("probe.site", ValueError("x"))
        rows = record._swallow_rows()
        self.assertEqual([lvl for lvl, _ in rows], ["WARN"])
        self.assertIn("probe.site", rows[0][1])

    def test_every_WIDE_silent_handler_speaks_and_the_NARROW_ones_stay_silent(self):
        """THE STRUCTURAL PIN, and it is exact rather than a floor.

        A floor is a lower bound, so it cannot notice a handler that goes
        silent again — the same blindness that let a name-keyed census miss
        four call shapes on another lane the same day. Both numbers are pinned,
        in both directions: a WIDE handler losing its trace reddens, and so
        does someone wiring the eight NARROW ones, whose silence is CORRECT
        and whose instrumentation would bury the twelve that matter under eight
        that do not. Ambient Expired propagation makes three handlers wide by
        adding a second control-flow arm, so they leave breadcrumbs like every
        other wide handler.
        """
        import ast
        # RESOLVED FROM THIS FILE, never from cwd. The first version opened
        # "helm/seats_stop_signals.py" relative to the working directory, which
        # is the repo root when a human runs it and something else entirely on
        # the fab — the gate ERRORed with FileNotFoundError while the same arm
        # passed locally. A path relative to cwd is a claim about the caller,
        # not about the tree under test.
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        wide_silent = narrow_silent = wired = 0
        for rel in ("helm/seats_stop_signals.py", "helm/seats_work_offer.py"):
            p = os.path.join(root, rel)
            tree = ast.parse(open(p, encoding="utf-8").read())
            for tr in ast.walk(tree):
                if not isinstance(tr, ast.Try):
                    continue
                for h in tr.handlers:
                    nm = ("<bare>" if h.type is None else
                          getattr(h.type, "id", "")
                          if isinstance(h.type, ast.Name) else "")
                    if nm not in ("<bare>", "Exception", "BaseException"):
                        continue
                    calls = [c for c in ast.walk(h) if isinstance(c, ast.Call)]
                    speaks = any("swallow" in ast.unparse(c.func) for c in calls)
                    stmts = sum(1 for n in ast.walk(tr) if isinstance(n, ast.stmt))
                    unpacks = [n for n in ast.walk(tr) if isinstance(n, ast.Assign)
                               and any(isinstance(x, ast.Tuple) for x in n.targets)]
                    if speaks:
                        wired += 1
                    elif stmts >= 6 or unpacks:
                        wide_silent += 1
                    else:
                        narrow_silent += 1
        self.assertEqual(wired, 12, "a WIDE handler stopped leaving a trace")
        self.assertEqual(wide_silent, 0, "a WIDE silent handler is back")
        self.assertEqual(narrow_silent, 8,
                         "the NARROW handlers changed — instrumenting them "
                         "buries the twelve that can hide a bug")


class RecordCoverageReadsTheContract(unittest.TestCase):
    """record.coverage() counted the `|| true` IDIOM inline rather than asking
    hooks._fail_open for the contract, so the moment hook_command generated an
    rc-case wrapper every home would have read as NOT fail-open and coverage
    would have reported 0 of N. A third copy of a predicate is a third place to
    forget."""

    def test_the_generated_hook_counts_as_fail_open(self):
        from helm import hooks, record
        cmd = record.hook_command()
        # MUST-MISS control: the shape it used to test for is GONE, so a
        # predicate still keyed on the idiom cannot pass this.
        self.assertNotIn("|| true", cmd)
        self.assertTrue(hooks._fail_open(cmd),
                        "the generated record hook must read as never-blocking")

    def test_status_rows_do_not_carry_their_own_predicate(self):
        """The defect was a duplicated test, so the arm is about the SOURCE."""
        import inspect
        from helm import record
        src = inspect.getsource(record.status_rows)
        self.assertIn("_fail_open", src)
        self.assertNotIn('"|| true" in cmd', src)


class TheInstalledRecorderCarriesItsDeadline(WiringBase):
    """The outer deadline must reach the ENTRIES install_home writes.

    record installs through _merge_hook, NOT hooks._canonical_entry, and
    _merge_hook wrote only {type, command}. So every installed recorder leg
    carried no harness deadline and its rc-124 alarm could be killed before it
    spoke — on the hook that fires on EVERY tool call. An earlier arm asserted
    hooks._canonical_entry(record.HOOK_SPECS[0]), which is a representation no
    installer uses and a budget (inner 5) no deployed hook runs: it passed
    while the installed seam was broken. A review found that on exact
    review; this reads the file instead.
    """

    def _entries(self, d, event):
        got = self.read_settings(d)
        return [h for g in (got.get("hooks") or {}).get(event, [])
                for h in (g.get("hooks") or [])
                if record._ours(str(h.get("command") or ""))]

    def test_a_fresh_install_writes_the_deadline_on_both_legs(self):
        d = self.mk_home("rec-fresh")
        action, detail = record.install_home(d)
        self.assertNotEqual(action, "fail", detail)
        for event in record.HOOK_EVENTS:
            hooks_ = self._entries(d, event)
            self.assertTrue(hooks_, "no recorder entry on %s" % event)
            for h in hooks_:
                self.assertEqual(h.get("timeout"), record.outer_deadline(event),
                                 event)
                # the deadline must OUTLIVE the inner budget or it is decoration
                self.assertGreater(h["timeout"],
                                   record.deployed_spec(event)["timeout"], event)

    def test_a_missing_deadline_is_repaired_not_reported_ok(self):
        """MUST-MISS: an entry written before this rail carries the right
        command and NO timeout. Re-installing is the standard cure for a stale
        entry, so it has to reach this one — `ok` here would mean the fleet
        keeps a dead alarm forever."""
        cmd = record.hook_command()
        stale = {"hooks": {ev: [{"hooks": [{"type": "command", "command": cmd}]}]
                           for ev in record.HOOK_EVENTS}}
        d = self.mk_home("rec-stale", stale)
        # CONTROL: the fixture really is missing the deadline going in
        for ev in record.HOOK_EVENTS:
            self.assertIsNone(self._entries(d, ev)[0].get("timeout"), ev)
        action, detail = record.install_home(d)
        self.assertNotEqual(action, "fail", detail)
        self.assertNotEqual(action, "ok",
                            "a missing deadline must be an UPDATE, not ok")
        # UNCONDITIONAL, outside the loop: the first leg really carries the
        # deadline now. A loop over HOOK_EVENTS cannot witness its own
        # emptiness, and every assertion below lives inside one.
        self.assertEqual(
            self._entries(d, record.HOOK_EVENTS[0])[0].get("timeout"),
            record.outer_deadline(record.HOOK_EVENTS[0]))
        for ev in record.HOOK_EVENTS:
            self.assertEqual(self._entries(d, ev)[0].get("timeout"),
                             record.outer_deadline(ev), ev)

    def test_a_stale_deadline_is_corrected(self):
        """The other half: present but WRONG. A value that is merely non-None
        must not satisfy the rail."""
        cmd = record.hook_command()
        wrong = record.outer_deadline() - 3
        stale = {"hooks": {ev: [{"hooks": [{"type": "command", "command": cmd,
                                            "timeout": wrong}]}]
                           for ev in record.HOOK_EVENTS}}
        d = self.mk_home("rec-wrong", stale)
        self.assertEqual(self._entries(d, record.HOOK_EVENTS[0])[0]["timeout"],
                         wrong)                       # control: really stale
        action, detail = record.install_home(d)
        self.assertNotEqual(action, "fail", detail)
        # UNCONDITIONAL: the stale 12 really became the right value, read once
        # outside the loop so the arm cannot pass over an empty event list.
        first = record.HOOK_EVENTS[0]
        self.assertEqual(self._entries(d, first)[0].get("timeout"),
                         record.outer_deadline(first))
        self.assertNotEqual(self._entries(d, first)[0].get("timeout"), wrong)
        for ev in record.HOOK_EVENTS:
            self.assertEqual(self._entries(d, ev)[0].get("timeout"),
                             record.outer_deadline(ev), ev)

    def test_the_deployed_inner_budget_is_ten_and_does_not_move(self):
        """HOOK_SPECS says 5 for the in-process dispatcher; the shell hook has
        always run at 10. deployed_spec is the one place that answers."""
        self.assertEqual(record.deployed_spec()["timeout"], hooks.TIMEOUT_S)
        self.assertEqual(record.deployed_spec()["timeout"], 10)
        self.assertEqual(shlex.split(record.hook_command())[4], "10")
        self.assertEqual(record.outer_deadline(), 15)


class RecorderStatusReadsTheWholeContract(WiringBase):
    """status/coverage must fail a home whose deadline died AFTER install.

    status_rows found recorder entries through _event_cmds, which yields
    command STRINGS — type and timeout are gone before any caller can look.
    So a home whose installed deadline was deleted or drifted post-install
    reported FULLY COVERED while its rc-124 alarm was dead again. Install-time
    correctness is not a standing guarantee; status is the surface that has to
    keep asking. A review named this on exact re-review after the add/update
    half was already cured — the same seam, the other direction.
    """

    def write_settings(self, d, settings):
        """WiringBase gives read_settings but no writer — these arms have to
        damage an INSTALLED file, which is the whole point of them."""
        with open(os.path.join(d, "settings.json"), "w") as f:
            json.dump(settings, f, indent=2)

    def _install(self, name):
        d = self.mk_home(name)
        action, detail = record.install_home(d)
        self.assertNotEqual(action, "fail", detail)
        return d

    def _mutate_legs(self, d, fn):
        s = self.read_settings(d)
        touched = 0
        for ev in record.HOOK_EVENTS:
            for g in (s.get("hooks") or {}).get(ev, []):
                for h in (g.get("hooks") or []):
                    if record._ours(str(h.get("command") or "")):
                        fn(h); touched += 1
        self.assertGreater(touched, 0, "mutated nothing — the arm is vacuous")
        self.write_settings(d, s)
        return touched

    def _covered(self, home):
        """`claude_homes()` yields REALPATHS, so a symlinked tmp would make a
        literal path compare match nothing and StopIteration would read as a
        confusing error rather than an uncovered home."""
        want = os.path.realpath(home)
        rows = [r for r in record.status_rows()
                if os.path.realpath(r["path"]) == want]
        self.assertEqual(len(rows), 1, "no status row for %s" % home)
        return rows[0]["hook"]

    def test_a_fully_installed_home_reads_covered(self):
        """The unconditional positive control every arm below leans on: this
        surface CAN say covered, so a False elsewhere means something."""
        d = self._install("st-good")
        self.assertTrue(self._covered(d))

    def test_a_deleted_deadline_stops_reading_covered(self):
        d = self._install("st-deleted")
        self.assertTrue(self._covered(d))              # control: covered first
        self._mutate_legs(d, lambda h: h.pop("timeout", None))
        self.assertFalse(self._covered(d),
                         "a home whose deadline was deleted after install must "
                         "not report covered — its rc-124 alarm is dead")

    def test_a_drifted_deadline_stops_reading_covered(self):
        """MUST-MISS: merely-present is not enough. A wrong number would
        satisfy any check that only asked whether a timeout exists."""
        d = self._install("st-drifted")
        self.assertTrue(self._covered(d))
        wrong = record.outer_deadline() - 3
        self._mutate_legs(d, lambda h: h.__setitem__("timeout", wrong))
        self.assertFalse(self._covered(d))

    def test_one_healthy_leg_does_not_carry_the_other(self):
        """Success-only wiring is the gap the both-legs rule exists for."""
        d = self._install("st-oneleg")
        self.assertTrue(self._covered(d))
        s = self.read_settings(d)
        for g in (s.get("hooks") or {}).get(record.HOOK_EVENTS[1], []):
            for h in (g.get("hooks") or []):
                h.pop("timeout", None)
        self.write_settings(d, s)
        self.assertFalse(self._covered(d))

    def test_coverage_counts_the_same_contract_status_reports(self):
        """coverage() and status_rows() must not answer differently — two
        lists answering one question is how a claim outlives its audit."""
        good = self._install("st-cov-good")
        bad = self._install("st-cov-bad")
        n_before, total = record.coverage()
        # UNCONDITIONAL: coverage CAN count, so the decrement below is a
        # decision about the damaged home and not a counter stuck at zero.
        self.assertGreaterEqual(n_before, 2, "both installed homes must count")
        self.assertGreater(total, 0)
        self._mutate_legs(bad, lambda h: h.pop("timeout", None))
        n_after, total_after = record.coverage()
        self.assertEqual(total, total_after)
        self.assertEqual(n_after, n_before - 1,
                         "coverage must drop by exactly the home status failed")
        self.assertTrue(self._covered(good))
