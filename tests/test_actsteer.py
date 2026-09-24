#!/usr/bin/env python3
"""Act steers, denies and retirements (task/2980 lane 5), each proven BOTH WAYS.

Every rung here is held to the premise a-guard-is-unproven-until-it-has-failed:
a planted violation must be refused or steered, and the legitimate look-alike
beside it must pass. A rung with only the first arm is an alarm; with only the
second it is a rung that does nothing and passes.

The byte cap is part of each rung's contract, not a separate budget: a steer
is one short line (actsteer.STEER_CAP) and a refusal one line with its cure
(actsteer.DENY_CAP), so the caps are asserted on the lines the rungs really
print, including their widest interpolations.
"""
import contextlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-actsteer-", var="HELM_HOME")

from helm import actsteer, chat, pk, record, reflex, resumeturn  # noqa: E402

# The rows this lane added to chat._STEERS (the rest of the table is older).
NEW_ROWS = ("git-stat-grep", "scrub-boundary", "pane-send")


class Isolated(unittest.TestCase):
    """A private helm home and chat dir, so a latch or a reflex file written
    here can never be one the fleet reads, and no seat name leaks in."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-actsteer-")
        self._env = {k: os.environ.get(k) for k in
                     ("HELM_HOME", "HELM_CHAT_DIR", "HELM_CHAT_NAME",
                      "MELD_CHAT_NAME")}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "home")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.makedirs(os.environ["HELM_CHAT_DIR"])
        for k in ("HELM_CHAT_NAME", "MELD_CHAT_NAME"):
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def hook(self, tool, tool_input, session="s-1", cwd=None, agent=None):
        """(rc, what the agent reads on the pass path, stderr). `agent` is
        the payload's agent_id: set, the call is a subagent's."""
        out, err = io.StringIO(), io.StringIO()
        stdin = sys.stdin
        payload = {"tool_name": tool, "session_id": session,
                   "tool_input": tool_input, "cwd": cwd or self.tmp}
        if agent:
            payload["agent_id"] = agent
        sys.stdin = io.StringIO(json.dumps(payload))
        try:
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                rc = chat.cmd_argv_guard([])
        finally:
            sys.stdin = stdin
        said = ""
        if out.getvalue().strip():
            said = json.loads(out.getvalue())[
                "hookSpecificOutput"]["additionalContext"]
        return rc, said, err.getvalue()

    def ids(self, command, tool_input=None, cwd=None):
        return [sid for sid, _t in chat.argv_steers(command, cwd, tool_input)]


# ---------------------------------------------------------------------------
# DENY: pkill -f whose pattern matches its own shell
# ---------------------------------------------------------------------------
class PkillDenyTest(Isolated):

    def test_a_self_matching_kill_is_REFUSED_with_a_cure_that_works(self):  # noqa: VACUOUS_ASSERTION — the control above the loop is unconditional and drives the SAME hook to rc 2 with its cure; the loop only widens the shapes
        rc, said, err = self.hook("Bash", {"command": "pkill -f 'helm web'"},
                                  session="control")
        self.assertEqual((rc, said), (2, ""))
        self.assertIn("pkill -f '[h]elm web'", err)
        for cmd in ("pkill -f 'helm web --port 7599'",
                    "pkill -f helm-web; echo done",
                    "sudo pkill -9 -f 'node server.js'",
                    "pkill --full 'python -m http.server'",
                    "pgrep -f 'fab gate' | xargs kill",
                    "kill $(pgrep -f 'until ! timeout')"):
            with self.subTest(cmd=cmd):
                rc, said, err = self.hook("Bash", {"command": cmd})
                self.assertEqual(rc, 2, "the self-kill ran: %s" % cmd)
                self.assertEqual(said, "")
                self.assertIn("exit 144", err)
                self.assertIn("Bracket one character", err)
                self.assertLessEqual(len(err.strip()), actsteer.DENY_CAP)
                self.assertNotIn("\n", err.strip())

    def test_the_printed_cure_is_itself_admitted(self):
        """THE CURE IS PROVEN ON THE COMMAND IT PRODUCES. A refusal whose own
        rewrite would be refused again is a loop, not a cure."""
        err = actsteer.refusal("pkill -f 'helm web --port 7599'")
        cure = err.split("Bracket one character: ", 1)[1]
        self.assertEqual(cure, "pkill -f '[h]elm web --port 7599'")
        self.assertIsNone(actsteer.refusal(cure))

    def test_safe_look_alikes_PASS(self):
        """Each passes because it cannot match its own shell, not because it
        is on a list: bracketed, name-matched (no -f), a variable, anchored,
        exact (-x), a word in prose, and a heredoc body that is data."""
        # POSITIVE CONTROL on the same reader, unconditional
        self.assertIsNotNone(actsteer.refusal("pkill -f 'helm web'"))
        for cmd in ("pkill -f '[h]elm web --port 7599'",
                    "pkill helm",
                    'pkill -f "$pat"',
                    "pkill -f '^node server'",
                    "pkill -x -f 'node server'",
                    "echo pkill -f is dangerous",
                    "helm chat post --room r <<'EOF'\n"
                    "pkill -f 'helm web' kills your shell\nEOF"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(actsteer.refusal(cmd), cmd)
                rc, _said, err = self.hook("Bash", {"command": cmd},
                                           session="safe-%d" % hash(cmd))
                self.assertEqual(rc, 0, err)

    def test_a_quoted_body_that_QUOTES_a_kill_is_data(self):
        """A gh or chat body whose literal newline puts `pkill -f helm` at the
        start of a line is text, not a command line: it runs nothing. The
        same words unquoted, and a kill run by a command substitution inside
        double quotes, still refuse."""
        refused = actsteer.refusal("echo x\npkill -f helm-web")
        self.assertIsNotNone(refused, "control: an unquoted line is a command")
        self.assertIsNotNone(actsteer.refusal(
            "kill \"$(pgrep -f 'helm web')\""),
            "a substitution inside double quotes runs")
        for cmd in ("gh pr comment 3 --body 'Do not run this:\n"
                    "pkill -f helm\nit kills your shell'",
                    'helm chat post --room r "careful:\npkill -f helm web\n"'):
            with self.subTest(cmd=cmd[:40]):
                self.assertIsNone(actsteer.refusal(cmd), cmd)
                rc, _said, err = self.hook("Bash", {"command": cmd},
                                           session="quoted-%d" % hash(cmd))
                self.assertEqual(rc, 0, err)

    APOSTROPHE_DOC = "cat <<EOF\ndon't\nEOF\n"

    def test_an_apostrophe_in_a_heredoc_body_never_hides_the_kill(self):
        """Every heredoc body is data, whatever its tag: an apostrophe in an
        UNQUOTED one (`don't`) must not open a quote that hides the command
        after it. The base refused these; a quote reader with no heredoc rule
        let them through (found in review)."""
        rc, _said, err = self.hook("Bash", {
            "command": self.APOSTROPHE_DOC + "pkill -f helm-web"})
        self.assertEqual(rc, 2, "the kill after the document ran")
        self.assertIn("pkill -f '[h]elm-web'", err)
        for cmd in (self.APOSTROPHE_DOC + "pgrep -f helm-web | xargs kill",
                    "cat <<-EOF\n\tdon't\n\tEOF\npkill -f helm-web"):
            with self.subTest(cmd=cmd[-30:]):
                self.assertIsNotNone(actsteer.refusal(cmd), cmd)

    def test_a_heredoc_a_shell_runs_is_code_and_any_other_is_data(self):
        """The one body that is not data is a shell's script: `bash <<EOF`,
        `sudo bash -s <<EOF`, or a document piped into a shell."""
        self.assertIsNotNone(actsteer.refusal(
            "bash <<'EOF'\npkill -f helm-web\nEOF"),
            "a script a shell runs was read as data")
        for cmd in ("sudo bash -s <<EOF\npkill -f helm-web\nEOF",
                    "cat <<EOF | bash\npkill -f helm-web\nEOF"):
            with self.subTest(code=cmd):
                self.assertIsNotNone(actsteer.refusal(cmd), cmd)
        for cmd in ("cat > notes.md <<EOF\npkill -f helm-web kills your "
                    "shell\nEOF",
                    "python3 - <<'EOF'\nimport subprocess\n"
                    "subprocess.run(['pkill', '-f', 'x'])\nEOF",
                    "cat <<EOF | sha256sum\npkill -f helm-web\nEOF"):
            with self.subTest(data=cmd):
                self.assertIsNone(actsteer.refusal(cmd), cmd)

    def test_the_steers_read_past_an_apostrophe_heredoc_too(self):
        doc = self.APOSTROPHE_DOC
        self.assertIn("pgrep-f-matches-your-own-shell",
                      self.ids(doc + "pgrep -f helm-web", {}))
        self.assertIn("claim-without-cl",
                      self.ids(doc + "helm chat post --room r 'READY: x'", {}))
        self.assertIn("timeout-timing",
                      self.ids(doc + "time timeout 5 true", {}))
        self.assertIn("sweep-before-you-build",
                      self.ids(doc + "helm work claim x", {}))

    def test_a_pattern_bracketing_cannot_save_names_the_other_cure(self):
        err = actsteer.refusal("pkill -f 'x'")
        self.assertIn("kill a pid you resolved", err)
        self.assertNotIn("Bracket one character:", err)

    def test_a_long_pattern_never_prints_a_cut_command(self):
        err = actsteer.refusal("pkill -f '%s'" % ("verylongword" * 8))
        self.assertIsNotNone(err)
        self.assertLessEqual(len(err), actsteer.DENY_CAP)
        self.assertNotIn("Bracket one character: pkill", err)
        self.assertIn("[v]", err)

    def test_a_read_only_pgrep_is_STEERED_not_refused(self):
        """pgrep alone kills nothing, so its self-match is advice: one of the
        pids it printed is its own shell."""
        rc, said, _err = self.hook("Bash", {"command": "pgrep -f 'helm web'"})
        self.assertEqual(rc, 0)
        self.assertIn(actsteer.PGREP_STEER, said)
        rc, said, _err = self.hook("Bash", {"command": "pgrep -f '[h]elm web'"},
                                   session="s-2")
        self.assertEqual((rc, said), (0, ""))

    def test_the_old_steer_row_is_gone_from_the_table(self):
        """One rule, one place: the deny replaced the steer row."""
        ids = [sid for sid, _p, _t in chat._STEERS]
        self.assertIn("two-dot-lane-diff", ids)
        self.assertNotIn("pkill-f-matches-your-own-shell", ids)


# ---------------------------------------------------------------------------
# DENY: an AI authoring line in a gh PR or issue body
# ---------------------------------------------------------------------------
class TrailerDenyTest(Isolated):

    VIOLATIONS = (
        "gh pr create --title x --body 'Fixes it.\n\n"
        "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>'",
        "gh pr create --title x --body \"$(cat <<'EOF'\nSummary\n\n"
        "\U0001F916 Generated with [Claude Code](https://claude.com/claude-code)"
        "\nEOF\n)\"",
        "gh issue comment 5 --body 'Generated with Claude Code'",
        "gh pr merge 12 --squash --body "
        "'x\\n\\nClaude-Session: https://claude.ai/code/session_abc'",
        "gh pr comment 3 -b 'Assisted-by: codex-3'",
        "gh issue create -t t -b 'see https://claude.ai/code/session_01abc'",
    )

    def test_every_owner_named_shape_is_REFUSED(self):  # noqa: VACUOUS_ASSERTION — the control above the loop is unconditional and drives the SAME hook to rc 2 naming the line; the loop only widens the shapes
        rc, said, err = self.hook("Bash", {"command": self.VIOLATIONS[0]},
                                  session="control")
        self.assertEqual((rc, said), (2, ""))
        self.assertIn("Co-Authored-By: Claude Opus 5.5", err)
        for cmd in self.VIOLATIONS:
            with self.subTest(cmd=cmd[:50]):
                rc, said, err = self.hook("Bash", {"command": cmd})
                self.assertEqual(rc, 2, "an authoring line reached gh: %s"
                                 % cmd)
                self.assertEqual(said, "")
                self.assertIn("AI authoring line", err)
                self.assertLessEqual(len(err.strip()), actsteer.DENY_CAP)
                self.assertNotIn("\n", err.strip())

    def test_the_refusal_names_the_line_not_the_whole_command(self):
        err = actsteer.refusal(
            "gh issue comment 5 --body 'Generated with Claude Code'")
        self.assertIn("line: Generated with Claude Code.", err)

    def test_a_body_FILE_is_read(self):
        body = os.path.join(self.tmp, "body.md")
        with open(body, "w") as f:
            f.write("Summary\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n")
        self.assertIsNotNone(actsteer.refusal(
            "gh pr create -t x -F body.md", cwd=self.tmp))
        with open(body, "w") as f:
            f.write("Summary\n\nA plain body.\n")
        self.assertIsNone(actsteer.refusal(
            "gh pr create -t x --body-file body.md", cwd=self.tmp))

    def test_prose_ABOUT_the_footer_passes_and_the_footer_is_refused(self):
        """The two sentences measured in review: a footer mark is a line
        OPENER, never a substring, or the rung refuses prose about itself."""
        self.assertIsNotNone(actsteer.refusal(
            "gh issue comment 1 --body 'Generated with Claude Code'"),
            "control: the real footer must still be refused")
        for body in ("the hook now refuses the Generated with Claude Code "
                     "footer", "nothing here was generated with Claude."):
            with self.subTest(body=body):
                self.assertIsNone(actsteer.refusal(
                    "gh pr comment 1 --body '%s'" % body))

    def test_gh_api_bodies_on_issues_and_pulls_are_read(self):
        with open(os.path.join(self.tmp, "bad.json"), "w") as f:
            json.dump({"body": "Fix.\n\nCo-Authored-By: Claude "
                               "<noreply@anthropic.com>"}, f)
        with open(os.path.join(self.tmp, "ok.json"), "w") as f:
            json.dump({"body": "Fix."}, f)
        with open(os.path.join(self.tmp, "bad.md"), "w") as f:
            f.write("Fix.\n\nGenerated with Claude Code\n")
        refuse = lambda c: actsteer.refusal(c, cwd=self.tmp)
        self.assertIsNotNone(refuse(
            "gh api repos/o/r/issues/5/comments -f body='x\n\n"
            "Co-Authored-By: Claude'"), "control: an api body is not read")
        for cmd in ("gh api repos/o/r/pulls -f title=t "
                    "-f body='Assisted-by: codex-3'",
                    "gh api repos/o/r/issues/5/comments --input bad.json",
                    "gh api repos/o/r/issues/5/comments -F body=@bad.md",
                    "gh api -X PATCH repos/o/r/issues/comments/9 "
                    "--field body='Generated with Claude Code'"):
            with self.subTest(hit=cmd):
                self.assertIsNotNone(refuse(cmd), cmd)
        for cmd in ("gh api repos/o/r/issues/5/comments --input ok.json",
                    "gh api repos/o/r/issues/5/comments -f body='the hook "
                    "refuses the Generated with Claude Code footer'",
                    "gh api repos/o/r/commits -f message='Co-Authored-By: "
                    "Claude'",
                    "gh api user",
                    "helm chat post 'ran gh pr create --body x'"):
            with self.subTest(miss=cmd):
                self.assertIsNone(refuse(cmd), cmd)

    def test_a_footer_after_an_apostrophe_heredoc_is_refused(self):
        rc, _said, err = self.hook("Bash", {
            "command": "cat <<EOF\ndon't\nEOF\n"
                       "gh pr comment 1 --body 'Generated with Claude Code'"})
        self.assertEqual(rc, 2, "the footer after the document reached gh")
        self.assertIn("AI authoring line", err)

    def test_legitimate_look_alikes_PASS(self):
        """Prose about Claude, the informal sign-off a public post ends with,
        a human co-author, an indented quoted example, a gh verb with no body,
        and a git commit (the commit-msg rung's surface, not this one)."""
        # POSITIVE CONTROL on the same reader, unconditional
        self.assertIsNotNone(actsteer.refusal(self.VIOLATIONS[0]))
        for cmd in (
                "gh pr create --title x --body 'This fixes the Claude Code "
                "hook that drops PreToolUse context.'",
                "gh issue comment 5 --body 'Thanks, landed.\n\n-- Claude Opus "
                "5.5, working in helm (https://github.com/akapug/helm) for "
                "@akapug'",
                "gh pr create --title x --body "
                "'Co-Authored-By: A Person <person@example.invalid>'",
                "gh pr comment 4 --body 'The harness wants this:\n\n"
                "    Co-Authored-By: Claude <noreply@anthropic.com>\n'",
                "gh pr view 12",
                "git commit -m 'Co-Authored-By: Claude'"):
            with self.subTest(cmd=cmd[:50]):
                self.assertIsNone(actsteer.refusal(cmd), cmd)


# ---------------------------------------------------------------------------
# STEERS: each row both ways, through the real hook
# ---------------------------------------------------------------------------
class SteerRowsTest(Isolated):

    CASES = {
        # steer id: (planted commands, legitimate look-alikes)
        "git-stat-grep": (
            ("git diff --stat origin/main...HEAD | grep helm/chat.py",
             "git log --stat -3 | rg 'tests/'"),
            ("git diff --stat origin/main...HEAD",
             "git diff --name-only origin/main...HEAD | grep chat",
             "git diff --numstat | awk '{print $3}'")),
        "scrub-boundary": (
            ("git rm -r --cached secrets/",
             "git filter-repo --path x --invert-paths"),
            ("git rm old.py", "git status")),
        "pane-send": (
            ("orca terminal send --terminal t1 --text 'tests passed' --enter",
             "herdr pane run p3 'echo hi'", "tmux send-keys -t 1 'ls' Enter"),
            ("orca terminal list", "helm chat dm codex 'look at x'")),
        "timeout-timing": (
            ("time timeout 5 /bin/true",
             "hyperfine 'timeout 5 helm inject --explain'",
             "s=$(date +%s%N); timeout 5 true; e=$(date +%s%N)",
             "/usr/bin/time -v timeout 2 ls"),
            ("timeout 600 pytest -q", "time helm x --timeout 5",
             "time /bin/true",
             "helm chat post --room r 'I ran time timeout 5 true: 103 ms'",
             "git commit -m 'time timeout 5 true is slow'",
             "helm chat post 'hyperfine timeout 5 x was flat'")),
        "transcript-read": (
            ("jq -r .message ~/.claude/projects/-home-x/abc.jsonl | head",
             "rg -l train159 ~/.claude/projects/",
             "tail -3 ~/.codex/sessions/2026/09/x.jsonl"),
            ("ls ~/.claude/projects/",
             "cat ~/.claude/projects/-home-x/memory/MEMORY.md")),
        "claim-without-cl": (
            ("helm chat post --room helm 'READY for review: lane x at abc123'",
             "helm chat post --room helm <<'EOF'\nLANDED train160\nEOF",
             "helm chat reply 12 'VERDICT approve'",
             "helm chat post --room helm '[MEASURED] @seat-a READY: lane x'",
             "helm chat post --measured --room helm 'task/2980 DONE: built'",
             "helm chat dm seat-a 'SHIPPED 0.3.1'"),
            ("helm chat post --room helm 'READY for review at abc123 CL85'",
             "helm chat post --room helm 'reading the diff, done soon'",
             "helm chat post --unverified --room helm 'DONE, not re-run'",
             "helm chat post --room helm 'the READY row from yesterday is "
             "stale'",
             "helm chat post --room helm 'reading it, will post READY later'",
             "helm chat post --room helm <<'EOF'\nthe LAND train is queued\n"
             "EOF",
             "helm task comment 5 'ran: helm chat post READY'",
             "echo DONE")),
        "sweep-before-you-build": (
            ("cd /x && helm work claim act-steers --ttl 100",),
            ("helm work list", "helm chat claims",
             "helm chat post 'I will helm work claim x soon'")),
    }

    def test_every_row_fires_on_its_act_and_is_silent_on_its_look_alike(self):
        self.assertEqual(len(self.CASES), 7, "a row's arms were dropped")
        self.assertEqual(self.ids("git diff --stat HEAD~1 | grep x", {}),
                         ["git-stat-grep"], "control: the table is dead")
        for sid, (hits, misses) in self.CASES.items():
            for cmd in hits:
                with self.subTest(steer=sid, hit=cmd):
                    self.assertIn(sid, self.ids(cmd, {}))
            for cmd in misses:
                with self.subTest(steer=sid, miss=cmd):
                    self.assertNotIn(sid, self.ids(cmd, {}))

    def test_a_steer_reaches_the_agent_once_per_context(self):
        cmd = "git diff --stat HEAD~1 | grep chat"
        rc, said, _err = self.hook("Bash", {"command": cmd}, session="ctx-1")
        self.assertEqual(rc, 0)
        self.assertIn("--name-only", said)
        self.assertEqual(self.hook("Bash", {"command": cmd},
                                   session="ctx-1")[1], "",
                         "the steer repeated inside one context")
        # a context boundary re-arms it (resumeturn calls forget_steers)
        self.assertGreaterEqual(chat.forget_steers("ctx-1"), 1)
        self.assertIn("--name-only", self.hook("Bash", {"command": cmd},
                                               session="ctx-1")[1])

    def test_a_subagent_then_the_seat_each_hear_the_steer(self):
        """A subagent shares its seat's session id. Its call must not spend
        the seat's latch: the seat still hears the steer about its own act."""
        cmd = "git diff --stat HEAD~1 | grep chat"
        sub = self.hook("Bash", {"command": cmd}, session="pair-1",
                        agent="agent-1")[1]
        self.assertIn("--name-only", sub, "the subagent heard nothing")
        self.assertIn("--name-only", self.hook(
            "Bash", {"command": cmd}, session="pair-1")[1],
            "the subagent's call spent the seat's latch")
        self.assertEqual(self.hook("Bash", {"command": cmd},
                                   session="pair-1")[1], "",
                         "the seat heard it twice")

    def test_the_seat_then_a_subagent_each_hear_the_steer(self):
        cmd = "git diff --stat HEAD~1 | grep chat"
        self.assertIn("--name-only", self.hook(
            "Bash", {"command": cmd}, session="pair-2")[1])
        self.assertIn("--name-only", self.hook(
            "Bash", {"command": cmd}, session="pair-2", agent="agent-1")[1],
            "the seat's call spent the subagent's latch")
        self.assertEqual(self.hook("Bash", {"command": cmd}, session="pair-2",
                                   agent="agent-1")[1], "",
                         "the subagent heard it twice")
        self.assertIn("--name-only", self.hook(
            "Bash", {"command": cmd}, session="pair-2", agent="agent-2")[1],
            "one subagent's latch muted another")

    def test_a_context_boundary_clears_the_seat_and_its_subagents(self):
        self.assertTrue(chat.steer_unfired("ctx-2", "x"))
        self.assertTrue(chat.steer_unfired("ctx-2", "x", "agent-1"))
        self.assertTrue(chat.steer_unfired("other", "x", "agent-1"))
        self.assertEqual(chat.forget_steers("ctx-2"), 2)
        self.assertTrue(chat.steer_unfired("ctx-2", "x"))
        self.assertTrue(chat.steer_unfired("ctx-2", "x", "agent-1"))
        self.assertFalse(chat.steer_unfired("other", "x", "agent-1"),
                         "another session's subagent latch was dropped")

    def test_forget_steers_touches_only_its_own_session(self):
        self.assertTrue(chat.steer_unfired("mine", "x"))
        self.assertTrue(chat.steer_unfired("theirs", "x"))
        chat.forget_steers("mine")
        self.assertTrue(chat.steer_unfired("mine", "x"), "not re-armed")
        self.assertFalse(chat.steer_unfired("theirs", "x"),
                         "another session's latch was dropped")

    def test_a_context_boundary_re_arms_the_steers(self):
        """The wiring, not only the helper: the SessionStart hook's reset leg
        clears this session's latches, and its dry run does not."""
        self.assertTrue(chat.steer_unfired("bound-1", "x"))
        self.assertFalse(chat.steer_unfired("bound-1", "x"))
        resumeturn.hook({"source": "clear", "session_id": "bound-1",
                         "cwd": self.tmp}, dry=True)
        self.assertFalse(chat.steer_unfired("bound-1", "x"),
                         "a dry run mutated the latch")
        resumeturn.hook({"source": "clear", "session_id": "bound-1",
                         "cwd": self.tmp})
        self.assertTrue(chat.steer_unfired("bound-1", "x"),
                        "the boundary left the steer latched")


class ForegroundSteerTest(Isolated):

    # Fixture seats: family-shaped names no roster carries (seat-a is the house
    # convention; the rung reads only the family token of the name).
    PROXY_SEATS = ("codex-42", "seat-a-codex", "kimi-9")

    def test_a_long_foreground_command_on_a_non_claude_seat_is_steered(self):
        os.environ["HELM_CHAT_NAME"] = self.PROXY_SEATS[0]
        self.assertIn("foreground-long-on-proxy-seat",
                      self.ids("sleep 60", {}), "control: the rung is dead")
        for name in self.PROXY_SEATS:
            with self.subTest(seat=name):
                os.environ["HELM_CHAT_NAME"] = name
                self.assertIn("foreground-long-on-proxy-seat",
                              self.ids("fab gate --repo . -- x", {}))
                self.assertIn("foreground-long-on-proxy-seat",
                              self.ids("python3 slow.py", {"timeout": 600000}))

    def test_claude_seats_background_calls_and_short_calls_are_silent(self):
        os.environ["HELM_CHAT_NAME"] = self.PROXY_SEATS[0]
        self.assertIn("foreground-long-on-proxy-seat",
                      self.ids("sleep 60", {}), "control: the rung is dead")
        self.assertNotIn("foreground-long-on-proxy-seat",
                         self.ids("sleep 60", {"run_in_background": True}))
        self.assertNotIn("foreground-long-on-proxy-seat", self.ids("ls", {}))
        self.assertNotIn("foreground-long-on-proxy-seat",
                         self.ids("sleep 5", {}))
        claude_proxy = "seat-b-" + sorted(actsteer._CLAUDE_PROXIES)[0]
        for name in ("seat-a", claude_proxy, "seat-c-claude"):
            with self.subTest(seat=name):
                os.environ["HELM_CHAT_NAME"] = name
                self.assertNotIn("foreground-long-on-proxy-seat",
                                 self.ids("fab gate --repo . -- x", {}))
        os.environ.pop("HELM_CHAT_NAME")
        self.assertNotIn("foreground-long-on-proxy-seat",
                         self.ids("fab gate --repo . -- x", {}))

    def test_the_line_names_the_family_and_fits(self):
        widest = max(set(chat._SPAWN_FAMILIES) - actsteer._CLAUDE_PROXIES,
                     key=len)
        os.environ["HELM_CHAT_NAME"] = "seat-a-" + widest
        text = dict(chat.argv_steers("sleep 90", None, {}))[
            "foreground-long-on-proxy-seat"]
        self.assertIn(widest + " seat", text)
        self.assertLessEqual(len(actsteer.STEER_PREFIX + text),
                             actsteer.STEER_CAP)


class PublicPushSteerTest(Isolated):

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "fork")
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        for name, url in (("origin", "git@github.com:akapug/orca.git"),
                          ("upstream", "https://github.com/stablyai/orca.git")):
            subprocess.run(["git", "-C", self.repo, "remote", "add", name, url],
                           check=True)
        self.sub = os.path.join(self.repo, "src")
        os.makedirs(self.sub)

    def fires(self, cmd, cwd=None):
        return "public-push" in self.ids(cmd, {}, cwd or self.sub)

    def test_an_outward_act_on_a_repo_origin_does_not_own_is_steered(self):
        self.assertTrue(self.fires("git push upstream fix"), "control")
        for cmd in ("git push upstream fix",
                    "gh pr create --title x --body y",
                    "gh issue create -R stablyai/orca -t x",
                    "git push https://github.com/other/thing main",
                    "gh repo create foo --public",
                    "gh repo edit --visibility public"):
            with self.subTest(cmd=cmd):
                self.assertTrue(self.fires(cmd), cmd)

    def test_the_land_push_and_own_repo_acts_are_silent(self):
        self.assertTrue(self.fires("git push upstream fix"),
                        "control: the rung is dead")
        for cmd in ("git push origin main", "git push",
                    "gh pr create --repo akapug/orca --title x",
                    "gh issue create --title x",
                    "gh repo create foo --private", "git status"):
            with self.subTest(cmd=cmd):
                self.assertFalse(self.fires(cmd), cmd)

    def test_a_checkout_with_no_github_origin_is_silent(self):
        bare = os.path.join(self.tmp, "local")
        subprocess.run(["git", "init", "-q", bare], check=True)
        self.assertTrue(self.fires("git push upstream fix"), "control")
        self.assertFalse(self.fires("git push upstream fix", cwd=bare))


class WriteSteerTest(Isolated):

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, "pkg"))
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        # the fixture lives under the system tmp dir, which the rung treats
        # as scratch; the scratch list is narrowed for these arms only
        self._scratch = mock.patch.object(actsteer, "_SCRATCH", ("/.claude/",))
        self._scratch.start()

    def tearDown(self):
        self._scratch.stop()
        super().tearDown()

    def steers(self, tool, path):
        return [sid for sid, _t in actsteer.write_steers(
            tool, {"file_path": path})]

    def test_a_new_source_module_is_steered_to_sweep_first(self):
        path = os.path.join(self.repo, "pkg", "newmod.py")
        self.assertEqual(self.steers("Write", path), ["sweep-before-you-build"])
        rc, said, _err = self.hook("Write", {"file_path": path, "content": ""})
        self.assertEqual(rc, 0)
        self.assertIn(actsteer.SWEEP_STEER, said)

    def test_tests_docs_existing_files_and_non_repo_paths_are_not_modules(self):
        self.assertEqual(self.steers(
            "Write", os.path.join(self.repo, "pkg", "fresh.py")),
            ["sweep-before-you-build"], "control: the rung is dead")
        existing = os.path.join(self.repo, "pkg", "old.py")
        with open(existing, "w") as f:
            f.write("x = 1\n")
        for path in (os.path.join(self.repo, "tests", "test_x.py"),
                     os.path.join(self.repo, "pkg", "thing_test.go"),
                     os.path.join(self.repo, "README.md"),
                     existing,
                     os.path.join(self.tmp, "outside", "x.py")):
            with self.subTest(path=path):
                self.assertEqual(self.steers("Write", path), [])

    def test_the_real_scratch_list_holds_tmp(self):  # noqa: VACUOUS_ASSERTION — `narrowed` is the unconditional positive on the same reader and the same path; only the scratch list differs
        path = os.path.join(self.repo, "pkg", "newmod.py")
        narrowed = self.steers("Write", path)
        self._scratch.stop()
        try:
            real = self.steers("Write", path)
        finally:
            self._scratch.start()
        self.assertEqual(narrowed, ["sweep-before-you-build"],
                         "control: the fixture path is not a new module")
        self.assertEqual(real, [],
                         "a module written under the tmp dir was called new")

    def test_editing_a_script_a_shell_is_running_is_steered(self):  # noqa: VACUOUS_ASSERTION — the found steer is asserted unconditionally after the run; the two absences bracket it on the same reader
        script = os.path.join(self.repo, "run.sh")
        with open(script, "w") as f:
            f.write("#!/bin/bash\nsleep 30\necho done\n")
        os.chmod(script, 0o755)
        self.assertEqual(self.steers("Edit", script), [],
                         "nothing runs it yet, so nothing may be said")
        # ITS OWN PROCESS GROUP, killed whole: killing bash alone orphans the
        # `sleep` it started, which outlives the test on the build node
        proc = subprocess.Popen(["bash", script], start_new_session=True)
        try:
            time.sleep(0.3)
            out = actsteer.write_steers("Edit", {"file_path": script})
        finally:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        self.assertEqual([sid for sid, _t in out], ["live-script-edit"])
        self.assertIn("pid %d" % proc.pid, out[0][1])
        self.assertLessEqual(len(actsteer.STEER_PREFIX + out[0][1]),
                             actsteer.STEER_CAP)
        self.assertEqual(self.steers("Edit", script), [],
                         "a finished run still read as live")

    def test_a_reader_holding_the_name_is_not_a_runner(self):  # noqa: VACUOUS_ASSERTION — admission is silence by construction; the positive neighbour on the SAME reader is test_editing_a_script_a_shell_is_running_is_steered, where a bash holding the same path is found
        script = os.path.join(self.repo, "run.sh")
        with open(script, "w") as f:
            f.write("#!/bin/bash\necho hi\n")
        proc = subprocess.Popen(["tail", "-f", script],
                                stdout=subprocess.DEVNULL,
                                start_new_session=True)
        try:
            time.sleep(0.2)
            held = self.steers("Edit", script)
        finally:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        self.assertEqual(held, [])


# ---------------------------------------------------------------------------
# THE CAP: every line this lane can print
# ---------------------------------------------------------------------------
class ByteCapTest(unittest.TestCase):

    def lines(self):
        rows = {sid: text for sid, _p, text in chat._STEERS}
        out = [rows[sid] for sid in NEW_ROWS]
        out += [actsteer.PGREP_STEER, actsteer.TRANSCRIPT_STEER,
                actsteer.TIMEOUT_STEER,
                actsteer.CL_STEER, actsteer.PUBLIC_STEER,
                actsteer.SWEEP_STEER,
                actsteer.FOREGROUND_STEER % max(chat._SPAWN_FAMILIES, key=len),
                # the widest basename the line prints and the widest pid
                # Linux issues (pid_max is 2**22)
                actsteer.LIVE_SCRIPT_STEER % ("x" * 40, str(2 ** 22))]
        return out

    def test_every_steer_is_one_short_line(self):  # noqa: VACUOUS_ASSERTION — the widest-line assertion before the loop is the unconditional control on the same lines
        lines = self.lines()
        self.assertEqual(len(lines), 11, "a steer went unmeasured")
        self.assertLessEqual(len(actsteer.STEER_PREFIX + max(lines, key=len)),
                             actsteer.STEER_CAP, "the widest line is a wall")
        for text in lines:
            with self.subTest(text=text[:40]):
                line = actsteer.STEER_PREFIX + text
                self.assertLessEqual(len(line), actsteer.STEER_CAP)
                self.assertGreater(len(line), 60, "an empty steer says nothing")
                self.assertNotIn("\n", line)

    def test_the_caps_are_the_design_numbers(self):  # noqa: VACUOUS_ASSERTION — a constant pin: the two numbers are the design's (section 5.2), and the arms that print real lines are the neighbours above
        """Design 5.2: act advice <=250 B, a deny reason <=300 B."""
        self.assertEqual((actsteer.STEER_CAP, actsteer.DENY_CAP), (250, 300))


# ---------------------------------------------------------------------------
# RETIRE: the prompt-lane triggers the rungs replace
# ---------------------------------------------------------------------------
class RetireMovedTest(unittest.TestCase):

    def entry(self, eid, cells):
        return {"id": eid, "keywords": ",".join(cells)}

    def test_a_row_applies_once_and_never_over_an_edit(self):
        row = actsteer.MOVED[0]
        kept = ["truncates"]
        calls = []

        def apply(r):
            calls.append(r["id"])
            return {}, None

        full = self.entry(row["id"], list(row["drop"]) + kept)
        self.assertEqual(actsteer.retire_moved([full], apply)[0],
                         (row["id"], "applied"))
        self.assertEqual(calls, [row["id"]])
        done = self.entry(row["id"], kept)
        partial = self.entry(row["id"], list(row["drop"])[1:] + kept)
        self.assertEqual(actsteer.retire_moved([done], apply)[0],
                         (row["id"], "done"))
        self.assertEqual(actsteer.retire_moved([partial], apply)[0],
                         (row["id"], "held"))
        self.assertEqual(calls, [row["id"]], "an edited entry was rewritten")

    def test_a_refused_write_is_held_and_a_missing_entry_is_absent(self):
        row = actsteer.MOVED[0]
        out = dict(actsteer.retire_moved(
            [self.entry(row["id"], row["drop"])],
            lambda r: (None, "refused")))
        self.assertEqual(out[row["id"]], "held")
        self.assertEqual(out[actsteer.MOVED[1]["id"]], "absent")

    def test_every_row_keeps_a_probe_and_names_its_rung(self):  # noqa: VACUOUS_ASSERTION — the count and first-row assertions before the loop are unconditional on the same table
        """retag refuses an entry left with no keywords, so a row that dropped
        every cell would be held forever: each must leave or add one."""
        chat_rows = {sid for sid, _p, _t in chat._STEERS}
        computed = {"live-script-edit", "foreground-long-on-proxy-seat",
                    "timeout-timing",
                    "pkill-f deny"}
        self.assertEqual(len(actsteer.MOVED), 6)
        self.assertIn(actsteer.MOVED[0]["rung"], chat_rows)
        for row in actsteer.MOVED:
            with self.subTest(row=row["id"]):
                self.assertEqual(row["id"], pk.slug(row["id"]))
                self.assertTrue(row["drop"])
                self.assertIn(row["rung"], chat_rows | computed)

    def test_retag_really_removes_the_cells_from_a_store_entry(self):
        """End to end on a scratch store: the default apply is store.retag,
        and the entry keeps its probes and gains the added ones."""
        from helm import store
        tmp = tempfile.mkdtemp(prefix="helm-test-actsteer-store-")
        env = {k: os.environ.get(k) for k in ("HELM_HOME", "HELM_ADOPTED_DIR")}
        try:
            os.environ["HELM_HOME"] = os.path.join(tmp, "home")
            os.environ["HELM_ADOPTED_DIR"] = os.path.join(tmp, "adopted")
            os.makedirs(os.environ["HELM_ADOPTED_DIR"])
            row = actsteer.MOVED[-1]           # pgrep: drops and adds
            store.write_heuristic({"id": row["id"],
                                   "move": "pgrep -f matches your own shell.",
                                   "keywords": ",".join(row["drop"]),
                                   "stated_ts": "2026-09-24T00:00:00Z",
                                   "source": "human"})
            planted = store._find(row["id"])
            first = dict(actsteer.retire_moved())
            after = store._find(row["id"])
            again = dict(actsteer.retire_moved())
        finally:
            for k, v in env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(planted["type"], "heuristic",
                         "control: the fixture entry did not land")
        self.assertEqual(first[row["id"]], "applied")
        self.assertEqual({c.strip() for c in str(after["keywords"]).split(",")},
                         set(row["add"]))
        self.assertEqual(again[row["id"]], "done")


class ReflexRekeyTest(Isolated):

    LIVE = {r["id"]: r for r in reflex.REKEYED}

    def plant(self, rid):
        was_signal, was_pattern = self.LIVE[rid]["was"]
        path = reflex.write({"id": rid, "steer": "the rule, whole.",
                             "signal": was_signal, "pattern": was_pattern})
        return path

    def moved_off(self, rid, trigger):
        """(fired before, (signal, pattern) after, fired after)."""
        self.plant(rid)
        before = rid in [e["id"] for e in reflex.fire(trigger)]
        reflex.seed_defaults()
        e = next(x for x in reflex.load_all() if x["id"] == rid)
        return before, (e["signal"], e["pattern"]), \
            rid in [x["id"] for x in reflex.fire(trigger)]

    def test_publication_boundary_moves_off_the_prompt_lane(self):
        before, now, after = self.moved_off("publication-boundary",
                                            "please scrub the leak")
        self.assertTrue(before, "control: the legacy trigger does not fire")
        self.assertEqual(now, (reflex.ACT_SIGNAL, ""))
        self.assertFalse(after)

    def test_sweep_before_you_build_moves_off_the_prompt_lane(self):
        before, now, after = self.moved_off("sweep-before-you-build",
                                            "let's build a new module")
        self.assertTrue(before, "control: the legacy trigger does not fire")
        self.assertEqual(now, (reflex.ACT_SIGNAL, ""))
        self.assertFalse(after)

    def test_cv_first_keeps_the_question_and_loses_the_machine_words(self):
        self.plant("cv-first-lookup")
        reflex.seed_defaults()
        fired = lambda t: "cv-first-lookup" in [e["id"] for e in reflex.fire(t)]
        self.assertTrue(fired("what did we decide about the footer?"),
                        "control: the re-keyed trigger fires on nothing")
        for text in ("what did we decide about the footer?",
                     "itw as yesterday you might forgot, cv transcript may "
                     "show",
                     "where was this discussed"):
            with self.subTest(hit=text):
                self.assertTrue(fired(text))
        for text in ("Monitor event: look up the recover path in the "
                     "transcript", "where was the lane when it failed"):
            with self.subTest(miss=text):
                self.assertFalse(fired(text))

    def test_an_operator_edited_trigger_is_never_rekeyed(self):  # noqa: VACUOUS_ASSERTION — the pin is that an EDIT survives; the re-key of the exact legacy trigger is the neighbour arm, on the same seed_defaults
        reflex.write({"id": "publication-boundary", "steer": "mine.",
                      "signal": "prompt", "pattern": r"\bscrub\b"})
        reflex.seed_defaults()
        e = next(x for x in reflex.load_all() if x["id"] ==
                 "publication-boundary")
        self.assertEqual((e["signal"], e["pattern"]), ("prompt", r"\bscrub\b"))


class DriftMainThreadTest(Isolated):
    """uncommitted-drift tells the SEAT to checkpoint; a subagent's dirt is
    not the seat's (104 of 119 measured streaks were subagents')."""

    def ev(self, agent=None, tool="Write"):
        e = {"session_id": "drift-1", "tool_name": tool, "cwd": self.tmp,
             "hook_event_name": "PostToolUse",
             "tool_input": {"file_path": os.path.join(self.tmp, "x.py")}}
        if agent:
            e["agent_id"] = agent
        return e

    def test_a_subagents_dirty_calls_do_not_move_the_seats_streak(self):
        probe = mock.Mock(return_value=True)
        with mock.patch.object(record, "_git_dirty", probe):
            record.record(self.ev())
            self.assertEqual(record.counters("drift-1")["dirty-streak"], 1)
            for _ in range(5):
                record.record(self.ev(agent="a-1"))
            self.assertEqual(record.counters("drift-1")["dirty-streak"], 1)
            self.assertEqual(probe.call_count, 1,
                             "a subagent's call paid the git probe")
            record.record(self.ev())
        self.assertEqual(record.counters("drift-1")["dirty-streak"], 2)


if __name__ == "__main__":
    unittest.main()
