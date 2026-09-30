#!/usr/bin/env python3
"""The GitHub-Actions rung's two MEASURED false refusals of task/3144, each
cured behind a fail-closed condition, and everything outside them kept
byte-for-byte as trunk (the integrator's SCOPE CUT of the abandoned lane
lane/argv-guard-quoted-words).

task/3144, verbatim: the argv-guard's GitHub-Actions rule fired on
  (1) a jq capture filter that names a Claude Code tool word beside a shell
      variable — `jq -c 'select(.name=="<Tool>")' "$F"`, which blocked two of
      a seat's capture commands; and
  (2) a MENTION of the Actions workflow directory inside quoted prose — which
      blocked a seat's own task-add text describing (1).

CURE A (jq): jq's program text is DATA only when jq's stdout AND stderr are
neither redirected, piped nor captured. jq runs nothing and writes no file, so
text that only reaches the terminal cannot run; a shell can only run what jq's
output is piped, redirected or captured into. Otherwise read as trunk.

CURE B (messages): the quoted prose argument of a message-taking verb
(`helm task add <text>`, `helm chat post <text>`, `git commit -m/-am/-qm
<msg>`) is DATA even when it names the workflow directory — the directory belt
exists for paths a program WRITES, and a message writes none — but ONLY when
EVERY sibling simple command on the line is on the NON-RUNNER ALLOWLIST (cd,
git add, git commit, git tag, helm chat post, helm chat ack, helm task add).
Anything else anywhere on the line: the whole line is read as trunk. An
allowlist fails closed where a runner list failed open.

NO LETTER-CASE CURE: `declare -l`, `${W,,}` and `$(echo <Tool> | tr A-Z a-z)`
each hand a real lowercase noun to gh, so trunk refuses all three and so do we.

Every spelling is built at runtime, as tests/test_chat_argv_guard.py does, so
this FILE holds no whole one — a seat that greps it inside a Bash command must
not be refused by the very rung this file measures.
"""
import contextlib
import io
import json
import os as _os
import sys as _sys
import unittest
from unittest import mock

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(
    _os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-actions-cut-", var="HELM_HOME")

from helm import chat  # noqa: E402

NOUN = "work" + "flow"
TOOL = "Work" + "flow"                  # the Claude Code tool name (task/3144)
RUN = "run"
ROW = RUN + " re" + RUN                 # two of the re-execute row's pieces
ACT = "gh %s %s ci.yml" % (NOUN, RUN)   # a real Actions invocation
RERUN = "gh %s 123" % ROW
DIRECTORY = ".git" + "hub/" + NOUN + "s"    # the workflow directory
OVERRIDE = "HELM_ALLOW_GITHUB_ACTIONS=1"


class _Hook(unittest.TestCase):
    """Drives the SHIPPED hook entry with a real PreToolUse payload."""

    RULE = "never a GitHub runner"
    PREMISE = "ci-runs-on-the-local-fabric-never-github-actions"

    def hook(self, command):
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                   "session_id": "sess-3144", "cwd": "/tmp/repo",
                   "tool_use_id": "toolu_3144",
                   "tool_input": {"command": command}}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(_sys, "stdin",
                               io.StringIO(json.dumps(payload))), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = chat.cmd_argv_guard([])
        return rc, out.getvalue() + err.getvalue()

    def assert_allowed(self, command):
        rc, out = self.hook(command)
        self.assertEqual(rc, 0, (command[:200], out))
        self.assertNotIn("BLOCKED", out, command[:200])

    def assert_refused(self, command):
        rc, out = self.hook(command)
        self.assertEqual(rc, 2, (command[:200], out))
        self.assertIn("[helm argv-guard] BLOCKED", out, command[:200])
        self.assertIn(self.RULE, out, command[:200])
        self.assertIn(self.OVERRIDE, out, command[:200])
        return out

    OVERRIDE = OVERRIDE


class CureAJqIsDataTest(_Hook):
    """CURE A: a jq filter whose output reaches the terminal is data."""

    def test_the_capture_specimen_and_its_kin_are_data(self):  # noqa: VACUOUS_ASSERTION — an unconditional assert_refused of a real act runs first on the same hook entry, so an empty observable fails there before any allow is reached
        # the same hook still refuses a real act at all
        self.assert_refused(ACT)
        for command in (
                # task/3144 (1): the capture specimen, output to the terminal
                "jq -c 'select(.name==\"%s\")' \"$F\"" % TOOL,
                "jq -r --arg t '%s' '.[] | select(.tool == $t)' $F" % TOOL,
                # jq as the LAST stage of a pipe: its own stdout is the
                # terminal, so its filter is data. The filter holds the whole
                # act: the re-execute row names gh since task/3696, and the
                # two words alone passed with or without the cut
                "helm chat read --json | jq -r '.[] | \"%s \\(.id)\"'"
                % RERUN,
                # a bare terminal jq naming the noun beside a hole
                "jq -rn '\"%s enable\"' \"$V\"" % NOUN):
            with self.subTest(command=command[:80]):
                self.assert_allowed(command)


class CureAJqThatRunsStaysRefusedTest(_Hook):
    """CURE A fails closed: jq whose stdout or stderr is piped, redirected or
    captured keeps every word, exactly as trunk reads it."""

    def test_a_jq_a_shell_runs_is_still_code(self):  # noqa: VACUOUS_ASSERTION — an unconditional assert_allowed runs first on the same hook entry, so a spurious refusal fails there before any refuse is reached
        self.assert_allowed("echo hi")
        for command in (
                # piped into a shell
                "jq -rn '\"%s\"' | sh" % ACT,
                "jq -rn '\"%s\"' | bash" % ACT,
                # captured by a substitution given to a program
                "gh $(jq -rn '\"%s\"')" % ACT,
                "bash -c \"$(jq -rn '\\\"%s\\\"')\"" % ACT,
                # a process substitution runs the output as a script
                "bash <(jq -rn '\"%s\"')" % ACT,
                "jq -rn '\"%s\"' > >(bash)" % ACT,
                # redirected to a file, then run
                "jq -rn '\"%s\"' > x.sh && bash x.sh" % ACT,
                "jq -rn '\"%s\"' > x.sh; . x.sh" % ACT,
                # a plain redirect of stdout is not the terminal
                "jq -rn '\"%s\"' > x.sh" % ACT):
            with self.subTest(command=command[:80]):
                self.assert_refused(command)


class CureBMessageMentionIsDataTest(_Hook):
    """CURE B: a workflow-directory MENTION inside a message verb's prose is
    data when every sibling command is on the non-runner allowlist."""

    def test_a_directory_mention_in_a_message_is_data(self):  # noqa: VACUOUS_ASSERTION — an unconditional assert_refused of a real act runs first on the same hook entry, so an empty observable fails there before any allow is reached
        self.assert_refused(ACT)
        for command in (
                # task/3144 (2): meta-claude's own task-add text
                "helm task add 'the rung fires on a MENTION of %s inside "
                "quoted prose'" % DIRECTORY,
                "helm chat post 'nothing here writes %s; it reads it'"
                % DIRECTORY,
                "git commit -m 'docs: %s is read-only here'" % DIRECTORY,
                "git commit --message='docs: %s stays empty'" % DIRECTORY,
                "git commit -qm 'docs: %s stays empty'" % DIRECTORY,
                "git commit -am 'docs: %s stays empty'" % DIRECTORY,
                # a sibling on the allowlist keeps the whole line safe
                "git add X && git commit -m 'docs: %s read-only'" % DIRECTORY,
                "cd /repo && helm chat post 'mentions %s here'" % DIRECTORY,
                "cd /repo && helm task add 'about %s only'" % DIRECTORY):
            with self.subTest(command=command[:80]):
                self.assert_allowed(command)


class CureBFailsClosedTest(_Hook):
    """CURE B fails closed: a runner sibling, a re-executor of the message, or
    a pathspec keeps the line refused exactly as trunk."""

    def test_a_re_executed_message_stays_refused(self):  # noqa: VACUOUS_ASSERTION — an unconditional assert_allowed runs first on the same hook entry, so a spurious refusal fails there before any refuse is reached
        self.assert_allowed("echo hi")
        for command in (
                # the message is recorded and replayed through a shell — the
                # re-executor class (`| sh` is not on the allowlist)
                "helm chat post '%s'; helm chat read --last 1 | sh" % ACT,
                "git commit -qm '%s'; git log -1 --format=%%B | sh" % ACT,
                # a double-quoted -m holding a substitution the shell runs
                "git commit -m \"landed $(gh %s %s 1)\"" % (NOUN, RUN),
                "git commit -m \"landed `gh %s %s 1`\"" % (NOUN, RUN),
                # a PATHSPEC after the message names a real path, belt kept
                "git commit -m 'clean' -- '%s/c i.yml'" % DIRECTORY,
                "git commit -m 'clean' '%s/c i.yml'" % DIRECTORY,
                # a non-allowlisted sibling anywhere reads the whole line trunk
                "helm task add 'about %s' && bash x.sh" % DIRECTORY,
                "python3 x.py; helm chat post 'about %s'" % DIRECTORY):
            with self.subTest(command=command[:80]):
                self.assert_refused(command)


class NoLetterCaseCureTest(_Hook):
    """The three letter-case shapes hand gh a real lowercase noun; trunk
    refuses all three and so must this lane."""

    def test_letter_case_shapes_stay_refused(self):  # noqa: VACUOUS_ASSERTION — an unconditional assert_allowed runs first on the same hook entry, so a spurious refusal fails there before any refuse is reached
        self.assert_allowed("echo hi")
        for command in (
                "declare -l W=%s; gh $W enable ci.yml" % TOOL,
                "W=%s; gh ${W,,} enable ci.yml" % TOOL,
                "gh $(echo %s | tr A-Z a-z) enable ci.yml" % TOOL):
            with self.subTest(command=command[:80]):
                self.assert_refused(command)


class CureAJqInCompoundOrRedirectStaysRefusedTest(_Hook):
    """CURE A, F1-narrowed (meta-claude's approval-tier read of 92b4aa4):
    jq's text is data ONLY when jq is a TOP-LEVEL simple command AND no command
    on the whole line carries a redirect target. A subshell, brace group or
    loop whose OUTPUT is redirected to a script, or an `exec` that moves the
    shell's stdout to a file BEFORE jq writes it, sends jq's noun into a file
    the line then runs — so every such shape keeps jq's words as trunk reads
    them, exactly as `jq -rn '<act>' | sh` already does."""

    def test_a_captured_or_compound_jq_is_still_code(self):  # noqa: VACUOUS_ASSERTION — an unconditional assert_allowed of a plain command runs first on the same hook entry, so a spurious refusal fails there before any refuse is reached
        self.assert_allowed("echo hi")
        jq = "jq -rn '\"%s\"'" % ACT      # jq that prints the act to its stdout
        for command in (
                # a subshell whose stdout is redirected to a script, then run
                "( %s ) > /tmp/x.sh; bash /tmp/x.sh" % jq,
                # a brace group redirected to a script, then run
                "{ %s; } > /tmp/x.sh; bash /tmp/x.sh" % jq,
                # a loop redirected to a script, then run
                "for i in 1; do %s; done > /tmp/x.sh; bash /tmp/x.sh" % jq,
                # exec moves the shell's stdout to a file BEFORE jq writes it
                "exec > /tmp/x.sh; %s" % jq):
            with self.subTest(command=command[:80]):
                self.assert_refused(command)


class CureBWrapperOrEditorStaysRefusedTest(_Hook):
    """CURE B, F2-narrowed (meta-claude's approval-tier read of 92b4aa4): a
    message verb is on the non-runner allowlist ONLY when its program is
    WORD 0 — no assignment prefix, no env/command/timeout/... wrapper — and a
    `git commit` on it carries no -e/--edit and no short cluster holding e.
    A wrapper can strip GIT_EDITOR and inject core.editor, and -e then opens
    that editor on the message git RUNS; either way the whole line is trunk."""

    def test_a_wrapper_or_editor_commit_keeps_the_directory_belt(self):  # noqa: VACUOUS_ASSERTION — an unconditional assert_allowed of a plain command runs first on the same hook entry, so a spurious refusal fails there before any refuse is reached
        self.assert_allowed("echo hi")
        for command in (
                # the env wrapper strips GIT_EDITOR and injects core.editor=sh,
                # then -e runs that editor on the message (meta-claude F2)
                "env -u GIT_EDITOR GIT_CONFIG_COUNT=1 "
                "GIT_CONFIG_KEY_0=core.editor GIT_CONFIG_VALUE_0=sh "
                "git commit -e -m 'cp evil.yml %s/'" % DIRECTORY,
                # -e / --edit / an e-cluster opens the editor on the message
                "git commit -e -m 'docs: %s note'" % DIRECTORY,
                "git commit --edit -m 'docs: %s note'" % DIRECTORY,
                "git commit -em 'docs: %s note'" % DIRECTORY,
                "git commit -ae -m 'docs: %s note'" % DIRECTORY,
                # an unambiguous prefix of --edit opens it too
                "git commit --ed -m 'docs: %s note'" % DIRECTORY,
                # an assignment prefix or a wrapper is not word 0
                "A=b git commit -m 'docs: %s note'" % DIRECTORY,
                "env git commit -m 'docs: %s note'" % DIRECTORY,
                "command git commit -m 'docs: %s note'" % DIRECTORY,
                "timeout 5 git commit -m 'docs: %s note'" % DIRECTORY,
                # already refused by the rebinding guard; pinned here so the
                # allowlist and the rebinding guard agree on it
                "GIT_EDITOR=x git commit -m 'docs: %s note'" % DIRECTORY):
            with self.subTest(command=command[:80]):
                self.assert_refused(command)


class F2AllowlistGreenPinsTest(_Hook):
    """CURE B still lifts the directory belt for a PLAIN word-0 message verb
    with no editor: git commit -m/-am/-qm naming the directory, git -C /r
    commit, and a cd or git-add sibling. The e-check and the word-0 check must
    not touch these — they are the specimens F2 keeps green."""

    def test_a_plain_directory_message_is_still_data(self):  # noqa: VACUOUS_ASSERTION — an unconditional assert_refused of a real act runs first on the same hook entry, so an empty observable fails there before any allow is reached
        self.assert_refused(ACT)
        for command in (
                "git -C /r commit -m 'docs: %s only'" % DIRECTORY,
                "git commit -am 'docs: %s only'" % DIRECTORY,
                "git commit -qm 'docs: %s only'" % DIRECTORY,
                "git add x && git commit -m 'docs: %s only'" % DIRECTORY):
            with self.subTest(command=command[:80]):
                self.assert_allowed(command)


class ActionsOnlyCutTest(unittest.TestCase):
    """The two cuts are ASKED FOR BY NAME (`actions=True`) and no other rung
    asks: the DEFAULT invocation text still holds the anchor the Actions rung
    then cuts, so the owner-posture and delegate-authority rungs read exactly
    what they read before."""

    def test_the_default_cut_keeps_what_the_actions_cut_removes(self):  # noqa: VACUOUS_ASSERTION — every arm asserts a non-empty anchor/row in the default cut and its absence in the actions cut, over a fixed non-empty tuple
        for command in (
                "jq -c 'select(.name==\"%s\")' \"$F\"" % TOOL,
                "helm task add 'a MENTION of %s'" % DIRECTORY,
                "git commit -m 'docs: %s only' " % DIRECTORY):
            with self.subTest(command=command[:80]):
                default, _c = chat._invocation_text(command)
                rows, anchor = chat._actions_evidence(chat._readings(default))
                self.assertTrue(rows or anchor is not None, command[:80])
                cut, _c2 = chat._invocation_text(command, actions=True)
                self.assertNotEqual(cut, default, command[:80])

    def test_the_owner_posture_rung_still_reads_jq_and_messages_as_code(self):  # noqa: VACUOUS_ASSERTION — every member asserts equality with a non-empty tuple over a fixed non-empty tuple
        mint = "owner" + "_door"
        for command in (
                "jq -rn '\"ownerasks.%s(1)\"' > notes.md" % mint,
                "python3 -c 'from helm.ownerasks import %s; %s(\"web\")'"
                % (mint, mint)):
            with self.subTest(command=command[:80]):
                self.assertEqual(chat.owner_posture_forge_refusal(command),
                                 ("mint", None))


if __name__ == "__main__":
    unittest.main()
