#!/usr/bin/env python3
"""task/3945: the argv guard was helm's most-refusing guard. Two cures.

(1) THE SUBSTITUTION REFUSAL PRINTS THE EXACT CORRECTED COMMAND. A backtick
    or `$(` inside a message body is refused (the shell would run it before
    helm sees argv). The refusal named the route in general terms only, so
    a seat rebuilt its command by hand and often met the refusal again. It
    now also prints the same command with every refused substitution made
    literal, checked against the guard (helm/argv_cure.py).

(2) PROSE THAT NAMES NO GH IS NOT A GH INVOCATION. The GitHub-Actions rung
    refused a sed state line, a task note and a store statement that said in
    words that an orchestration ran (`workflow` beside `run`) inside a quoted
    text argument. Where no gh head stands anywhere in the command, gh's
    verb words inside quoted prose and quoted heredoc bodies are not read,
    and only where every program of the command receives prose and runs
    none of it (an allow-list). Every true refusal is pinned below beside
    the cure, and so is every runtime-head shape a cross-model read found
    admitted by a deny-set, judged in real bash where this box can run it.

Every spelling is built at runtime, as the sibling argv tests do, so this
FILE holds no whole one.
"""
import contextlib
import io
import json
import os as _os
import shutil
import subprocess
import sys as _sys
import unittest
from unittest import mock

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(
    _os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-argv-cure-3945-", var="HELM_HOME")

from helm import argv_cure, chat  # noqa: E402

TICK = "`"
SUB = "$" + "("
GH = "g" + "h"
NOUN = "work" + "flow"
RUN = "run"
RERUN = "re" + RUN
POST = "helm chat post --room seat-a"
DIRECTORY = ".git" + "hub/" + NOUN + "s"
API = "api." + "github.com"


def hook(command):
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
               "session_id": "sess-3945", "cwd": "/tmp",
               "tool_use_id": "toolu_3945", "tool_input": {"command": command}}
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(_sys, "stdin", io.StringIO(json.dumps(payload))), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = chat.cmd_argv_guard([])
    return rc, out.getvalue() + err.getvalue()


def bash_prints(command):
    """What bash prints for `command`, with no environment of its own."""
    return subprocess.run(["bash", "--noprofile", "--norc", "-c", command],
                          capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "V": "vee"},
                          timeout=10).stdout


class CorrectedCommandTest(unittest.TestCase):
    """The copy is the refused command with only its refused substitutions
    made literal, and the guard admits it."""

    def fixed(self, command):
        self.assertIsNotNone(chat.argv_guard(command), command)
        out = argv_cure.corrected_command(command)
        self.assertIsNotNone(out, command)
        self.assertIsNone(chat.argv_guard(out), out)
        return out

    def test_a_backtick_in_a_chat_body_is_escaped_in_place(self):  # noqa: VACUOUS_ASSERTION — `fixed` asserts unconditionally that the guard refuses the command and admits the copy, and the copy is compared by EQUALITY to a non-empty literal
        cmd = '%s "never run %sgit clean%s here"' % (POST, TICK, TICK)
        self.assertEqual(self.fixed(cmd), '%s "never run \\%sgit clean\\%s '
                         'here"' % (POST, TICK, TICK))

    def test_a_dollar_paren_is_escaped_and_a_plain_variable_still_expands(self):  # noqa: VACUOUS_ASSERTION — `fixed` asserts unconditionally that the guard refuses the command and admits the copy, and the copy is compared by EQUALITY to a non-empty literal
        cmd = 'helm task comment 1 "at %sgit rev-parse HEAD) for $V"' % SUB
        self.assertEqual(self.fixed(cmd), 'helm task comment 1 "at \\%sgit '
                         'rev-parse HEAD) for $V"' % SUB)

    def test_a_cat_document_word_becomes_the_stdin_heredoc_for_chat(self):  # noqa: VACUOUS_ASSERTION — `fixed` asserts unconditionally that the guard refuses the command and admits the copy, and the copy is compared by EQUALITY to a non-empty literal
        cmd = ("%s \"%scat <<'EOF'\nbody %sx%s with \"q\" and %sy)\nEOF\n)\" "
               "--measured" % (POST, SUB, TICK, TICK, SUB))
        self.assertEqual(self.fixed(cmd), (
            "%s <<'EOF' --measured\nbody %sx%s with \"q\" and %sy)\nEOF"
            % (POST, TICK, TICK, SUB)))

    def test_a_cat_document_word_becomes_a_variable_for_a_commit(self):  # noqa: VACUOUS_ASSERTION — `fixed` asserts unconditionally that the guard refuses the command and admits the copy, and the copy is compared by EQUALITY to a non-empty literal
        cmd = ("git commit -m \"%scat <<'EOF'\nsubject\n\nbody %sx%s\nEOF\n)\""
               % (SUB, TICK, TICK))
        self.assertEqual(self.fixed(cmd), (
            "helm_body=%scat <<'EOF'\nsubject\n\nbody %sx%s\nEOF\n)\n"
            "git commit -m \"$helm_body\"" % (SUB, TICK, TICK)))

    def test_an_unquoted_heredoc_body_keeps_its_variables(self):  # noqa: VACUOUS_ASSERTION — `fixed` asserts unconditionally that the guard refuses the command and admits the copy, and the copy is compared by EQUALITY to a non-empty literal
        cmd = "%s <<EOF\nrun %sx%s now $V\nEOF" % (POST, TICK, TICK)
        self.assertEqual(self.fixed(cmd), "%s <<EOF\nrun \\%sx\\%s now $V\nEOF"
                         % (POST, TICK, TICK))

    def test_only_the_refused_segment_changes(self):  # noqa: VACUOUS_ASSERTION — `fixed` asserts unconditionally that the guard refuses the command and admits the copy, and the copy is compared by EQUALITY to a non-empty literal
        cmd = 'cd /tmp && %s "a %sb%s" && echo %sdate)' % (
            POST, TICK, TICK, SUB)
        self.assertEqual(self.fixed(cmd), 'cd /tmp && %s "a \\%sb\\%s" && '
                         'echo %sdate)' % (POST, TICK, TICK, SUB))

    def test_a_bare_backtick_and_verdict_evidence_and_a_continuation(self):
        self.assertEqual(self.fixed("%s hello %sx%s" % (POST, TICK, TICK)),
                         "%s hello \\%sx\\%s" % (POST, TICK, TICK))
        verdict = ('helm dispatch verdict abc %s --fix "finding %sx%s"'
                   % ("a" * 40, TICK, TICK))
        self.assertIn('"finding \\%sx\\%s"' % (TICK, TICK),
                      self.fixed(verdict))
        joined = '%s \\\n  "a %sb%s"' % (POST, TICK, TICK)
        self.assertEqual(self.fixed(joined), '%s \\\n  "a \\%sb\\%s"'
                         % (POST, TICK, TICK))

    def test_every_hazard_of_one_command_is_cured_in_one_copy(self):  # noqa: VACUOUS_ASSERTION — `fixed` asserts unconditionally that the guard refuses the command and admits the copy, and the copy is compared by EQUALITY to a non-empty literal
        cmd = 'helm task add "t %sa%s" --note "n %sb)"' % (TICK, TICK, SUB)
        self.assertEqual(self.fixed(cmd), 'helm task add "t \\%sa\\%s" '
                         '--note "n \\%sb)"' % (TICK, TICK, SUB))

    def test_the_copy_means_what_the_author_typed_in_real_bash(self):  # noqa: VACUOUS_ASSERTION — `fixed` asserts unconditionally that the guard refuses the command and admits the copy, and the copy is compared by EQUALITY to a non-empty literal
        """The cure is judged by a shell, not by the guard alone: the body
        of the corrected word is the literal text, with `$V` expanded."""
        if not shutil.which("bash"):
            self.skipTest("no bash")
        for body, want in (
                ("never run %sgit clean%s, see $V" % (TICK, TICK),
                 "never run %sgit clean%s, see vee\n" % (TICK, TICK)),
                ("at %sgit rev-parse HEAD) and %sdate%s" % (SUB, TICK, TICK),
                 "at %sgit rev-parse HEAD) and %sdate%s\n"
                 % (SUB, TICK, TICK))):
            with self.subTest(body=body):
                out = self.fixed('%s "%s"' % (POST, body))
                self.assertEqual(bash_prints(out.replace(
                    POST, "printf '%s\\n'", 1)), want)
        cmd = ("%s \"%scat <<'EOF'\nline %sone%s\n  two $V\nEOF\n)\""
               % (POST, SUB, TICK, TICK))
        self.assertEqual(bash_prints(self.fixed(cmd).replace(POST, "cat", 1)),
                         "line %sone%s\n  two $V\n" % (TICK, TICK))
        cmd = ("git commit -m \"%scat <<'EOF'\nsubject %sx%s\nEOF\n)\""
               % (SUB, TICK, TICK))
        self.assertEqual(bash_prints(self.fixed(cmd).replace(
            "git commit -m", "printf '%s\\n'", 1)),
            "subject %sx%s\n" % (TICK, TICK))

    def test_what_it_cannot_settle_prints_no_copy(self):  # noqa: VACUOUS_ASSERTION — each None is read beside the guard's own refusal of the same command, asserted first, so the builder declining is not the guard passing
        for cmd in (
                # a document whose tag is unquoted: its body substitutes
                "%s \"%scat <<EOF\nx %sy%s\nEOF\n)\"" % (POST, SUB, TICK, TICK),
                # past the cap
                '%s "%s %sx%s"' % (POST, "p" * argv_cure.CORRECTED_CAP,
                                   TICK, TICK)):
            with self.subTest(cmd=cmd[:40]):
                self.assertIsNotNone(chat.argv_guard(cmd))
                self.assertIsNone(argv_cure.corrected_command(cmd))
        self.assertIsNone(argv_cure.corrected_command("echo ok"))


class RefusalPrintsTheCopyTest(unittest.TestCase):
    """The shipped hook entry: still exit 2, still the general route, and
    now the copy after it."""

    def test_the_chat_refusal_carries_the_copy(self):
        cmd = '%s "never run %sgit clean%s"' % (POST, TICK, TICK)
        rc, out = hook(cmd)
        self.assertEqual(rc, 2, out)
        self.assertIn("<<'EOF'", out)                      # the route
        self.assertIn("ready to run as is", out)
        self.assertTrue(out.rstrip("\n").endswith(
            '\n%s%s "never run \\%sgit clean\\%s"'
            % (argv_cure.CORRECTED, POST, TICK, TICK)), out)

    def test_the_copy_line_is_the_fleets_corrected_line(self):
        from helm import review_done
        self.assertEqual(argv_cure.CORRECTED, review_done.CORRECTED)

    def test_the_commit_and_verdict_refusals_carry_theirs(self):
        rc, out = hook('git commit -m "the test is %sx%s"' % (TICK, TICK))
        self.assertEqual(rc, 2, out)
        self.assertIn("git commit -F", out)
        self.assertIn('git commit -m "the test is \\%sx\\%s"' % (TICK, TICK),
                      out)
        rc, out = hook('helm dispatch verdict abcdef12 %s --fix "f %sx%s"'
                       % ("a" * 40, TICK, TICK))
        self.assertEqual(rc, 2, out)
        self.assertIn("evidence=$(cat <<'EOF'", out)
        self.assertIn('"f \\%sx\\%s"' % (TICK, TICK), out)

    def test_a_defect_in_the_builder_costs_the_copy_not_the_refusal(self):
        cmd = '%s "x %sy%s"' % (POST, TICK, TICK)
        with mock.patch.object(argv_cure, "corrected_command",
                               side_effect=RuntimeError("boom")):
            rc, out = hook(cmd)
        self.assertEqual(rc, 2, out)
        self.assertIn("BLOCKED", out)
        self.assertNotIn(argv_cure.CORRECTED, out)


class ProseNamesNoGhTest(unittest.TestCase):
    """The measured false refusals pass; every true refusal stays."""

    def refused(self, command):
        return chat.github_actions_refusal(command=command)

    PHRASE = "the %s %s finished green" % (NOUN, RUN)

    def test_the_three_measured_places_pass(self):  # noqa: VACUOUS_ASSERTION — each pass is read beside test_every_true_refusal_still_refuses on the same rung, whose unquoted and gh-bearing spellings of the same words are refused
        for cmd in (
                "sed -i 's/old/%s/' /tmp/scratch/state.txt" % self.PHRASE,
                "helm store add premise x | '%s'" % self.PHRASE,
                # the two `$(date)` places moved to refused in round 4
                # (ALiveSubstitutionIsNeverProseTest): a live substitution
                # is never prose
                "cat > /tmp/scratch/notes.txt <<'EOF'\n%s, then %s %s\nEOF"
                % (self.PHRASE, RUN, "cancel"),
                "echo \"the %s %s happened\"" % (RUN, "cancel")):
            with self.subTest(cmd=cmd[:50]):
                self.assertIsNone(self.refused(cmd))

    def test_every_true_refusal_still_refuses(self):  # noqa: VACUOUS_ASSERTION — the loop runs over a non-empty literal tuple of commands, each asserted refused (not None) unconditionally inside it
        for cmd in (
                "%s %s %s ci.yml" % (GH, NOUN, RUN),
                "%s %s cancel 12" % (GH, RUN),
                "%s %s %s 12" % (GH, RUN, RERUN),
                "bash -c '%s %s %s ci.yml'" % (GH, NOUN, RUN),
                "ssh host \"%s %s cancel 1\"" % (GH, RUN),
                "%s.exe %s enable ci.yml" % (GH, NOUN),
                "/usr/bin/%s %s %s ci.yml" % (GH, NOUN, RUN),
                "g\\h %s %s ci.yml" % (NOUN, RUN),
                "python3 -c \"import subprocess; subprocess.run(['%s', '%s',"
                " '%s', 'x'])\"" % (GH, NOUN, RUN),
                "bash <<'EOF'\n%s %s %s ci.yml\nEOF" % (GH, NOUN, RUN),
                # a program that runs text keeps its prose read: it can
                # build the head from pieces the fold cannot rejoin
                "printf '%%sh %s cancel 1' g | sh" % RUN,
                "python3 x.py 'the %s %s'" % (NOUN, RUN),
                "eval \"echo; %s list\"" % NOUN,
                # a program the cure does not know may run what it is
                # handed, so its prose is read as before (the allow-list)
                "seat-a-tool <<'EOF'\n%s, then %s %s\nEOF"
                % (self.PHRASE, RUN, "cancel"),
                "seat-a-tool '%s'" % self.PHRASE,
                # a head from a runtime value on the command line
                "$B %s %s ci.yml" % (NOUN, RUN),
                "$B %s %s 1" % (RUN, RERUN),
                "%s %s $V ci.yml" % (GH, NOUN),
                # gh anywhere keeps the prose read: the cost of this cure
                "%s pr view 1; sed -i 's/x/%s/' /tmp/scratch/state.txt"
                % (GH, self.PHRASE),
                # an unquoted word is never prose
                "echo the %s %s happened" % (RUN, "cancel"),
                # the rows that are not gh's verb grammar read as before
                "helm task note 1 'see %s/ci.yml now'" % DIRECTORY,
                "curl -X POST 'https://%s/repos/o/r/actions/x y'" % API):
            with self.subTest(cmd=cmd[:50]):
                self.assertIsNotNone(self.refused(cmd))

    def test_the_blank_keeps_every_position(self):
        text = "a '%s' \"b %s\" c" % (self.PHRASE, RERUN)
        out = chat._blank_quoted_prose(text)
        self.assertEqual(len(out), len(text))
        self.assertNotIn(NOUN, out)
        self.assertNotIn(RERUN, out)
        self.assertIn("finished green", out)


def bash_in(command, cwd, env=None):
    """What bash prints for `command` run in `cwd`."""
    return subprocess.run(["bash", "--noprofile", "--norc", "-c", command],
                          capture_output=True, text=True, cwd=cwd,
                          env=dict({"PATH": "/usr/bin:/bin"}, **(env or {})),
                          timeout=10).stdout


def bash_lines(command, cwd):
    """The argv lines `printf '<%s>\n'` prints in place of the chat verb."""
    return bash_in(command.replace(POST, "printf '<%s>\\n'", 1),
                   cwd).splitlines()


class TheCopyKeepsTheShellsReadingTest(unittest.TestCase):
    """A backslash in front of a refused `$(` makes it literal, but the
    double quotes INSIDE that substitution then close and open the word
    instead: the copy splits commands and words where the original did
    not. Such a command gets no copy. Judged in real bash."""

    def setUp(self):
        if not shutil.which("bash"):
            self.skipTest("no bash")
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="helm-3945-copy-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_a_nested_quote_never_runs_its_inner_text(self):  # noqa: VACUOUS_ASSERTION — the original's one argv line and absent marker are asserted unconditionally first, and the copy is then asserted None unconditionally
        marker = _os.path.join(self.tmp, "PWNED")
        cmd = '%s "%secho "; touch %s; echo ")"' % (POST, SUB, marker)
        self.assertIsNotNone(chat.argv_guard(cmd))
        self.assertEqual(len(bash_lines(cmd, self.tmp)), 1)
        self.assertFalse(_os.path.exists(marker))
        out = argv_cure.corrected_command(cmd)
        if out is not None:
            lines = bash_lines(out, self.tmp)
            self.assertFalse(_os.path.exists(marker),
                             "the copy RAN its inner text: %r" % out)
            self.assertEqual(len(lines), 1, out)
        self.assertIsNone(out)

    def test_a_nested_quoted_format_keeps_its_one_word(self):  # noqa: VACUOUS_ASSERTION — the original's one argv line is asserted unconditionally first, and the copy is then asserted None unconditionally
        cmd = '%s "status: %sgit log -1 --format="%%h %%s")"' % (POST, SUB)
        self.assertIsNotNone(chat.argv_guard(cmd))
        self.assertEqual(len(bash_lines(cmd, self.tmp)), 1)
        out = argv_cure.corrected_command(cmd)
        if out is not None:
            self.assertEqual(len(bash_lines(out, self.tmp)), 1,
                             "the copy splits into more argv words: %r" % out)
        self.assertIsNone(out)

    def test_an_ordinary_shape_keeps_its_copy(self):
        cmd = '%s "at %sgit rev-parse HEAD) and %sx%s for $V"' % (
            POST, SUB, TICK, TICK)
        out = argv_cure.corrected_command(cmd)
        self.assertIsNotNone(out)
        self.assertIsNone(chat.argv_guard(out))
        self.assertEqual(bash_lines(out, self.tmp), [
            "<at %sgit rev-parse HEAD) and %sx%s for >" % (SUB, TICK, TICK)])

    def test_a_command_the_reader_cannot_settle_gets_no_copy(self):
        cmd = '%s "a %sb%s"' % (POST, TICK, TICK)
        self.assertIsNotNone(argv_cure.corrected_command(cmd))
        with mock.patch.object(chat._ShellReader, "read",
                               side_effect=chat._Unsettled("x")):
            self.assertIsNone(argv_cure.corrected_command(cmd))


class ARuntimeHeadRunnerIsReadWholeTest(unittest.TestCase):
    """The prose cure reads quoted text as prose only where every program
    of the command is one that receives prose. A runtime-head Actions call
    through a wrapper option, a multiplexer, a lock, a git alias or the
    fabric itself is refused, as on main."""

    def refused(self, command):
        return chat.github_actions_refusal(command=command)

    def shapes(self):
        act = "$G %s %s ci.yml" % (NOUN, RUN)
        return (
            'env -S "%s"' % act,
            'sudo -s "%s"' % act,
            'tmux new-window "%s"' % act,
            'screen -dm sh -c "$G %s cancel 1"' % RUN,
            'flock ./l -c "%s"' % act,
            "git -c 'alias.z=!%s' z" % act,
            'fab test --repo . -- sh -c "%s"' % act)

    def test_the_seven_shapes_are_refused(self):  # noqa: VACUOUS_ASSERTION — the loop runs over a non-empty literal tuple, each asserted refused unconditionally
        for cmd in self.shapes():
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(self.refused(cmd))

    def test_the_shapes_bash_can_run_here_run_the_head(self):  # noqa: VACUOUS_ASSERTION — assertGreater(ran, 0) after the loop proves at least one shape ran its stub head in real bash
        """Judged in real bash with a stub head: each shape RUNS the value
        it is handed as a program, so it is an Actions call."""
        if not shutil.which("bash"):
            self.skipTest("no bash")
        import tempfile
        tmp = tempfile.mkdtemp(prefix="helm-3945-head-")
        self.addCleanup(shutil.rmtree, tmp, True)
        stub = _os.path.join(tmp, "head")
        with open(stub, "w") as f:
            f.write('#!/bin/sh\necho "STUB $*"\n')
        _os.chmod(stub, 0o755)
        want = "STUB %s %s ci.yml\n" % (NOUN, RUN)
        ran = 0
        for cmd in self.shapes():
            tool = cmd.split()[0]
            # env -S is left to the guard: this box's env does not split
            # a separate -S word the way GNU's does
            if tool not in ("flock", "git") or not shutil.which(tool):
                continue
            with self.subTest(cmd=cmd):
                self.assertEqual(bash_in(cmd, tmp, {"G": stub}), want)
                ran += 1
                self.assertIsNotNone(self.refused(cmd))
        self.assertGreater(ran, 0)

    def test_every_control_stays_refused(self):  # noqa: VACUOUS_ASSERTION — the loop runs over a non-empty literal tuple, each asserted refused unconditionally
        act = "$G %s %s ci.yml" % (NOUN, RUN)
        for cmd in (
                'ssh host "%s"' % act,
                'bash -c "%s"' % act,
                'echo "%s" | sh' % act,
                "$B %s %s ci.yml" % (NOUN, RUN),
                "%s %s %s ci.yml" % (GH, NOUN, RUN),
                'helm task note 1 "x" && seat-a-tool "%s"' % act,
                # a runner in a group, a subshell or a substitution
                '(sh -c "%s")' % act,
                '{ sh -c "%s"; }' % act,
                'echo "%s" | (sh)' % act,
                'cat <(sh -c "%s")' % act,
                'echo "x %ssh -c "%s")"' % (SUB, act),
                # GNU sed reads an option after its first operand
                "sed state.txt -e 'e $G %s cancel 1'" % RUN):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(self.refused(cmd))

    def test_the_measured_prose_still_passes(self):  # noqa: VACUOUS_ASSERTION — read beside test_the_seven_shapes_are_refused on the same rung, whose runtime-head spellings are refused
        phrase = "the %s %s finished green" % (NOUN, RUN)
        for cmd in (
                "sed -i 's/old/%s/' /tmp/scratch/state.txt" % phrase,
                "helm store add premise x | '%s'" % phrase,
                "cd /tmp && helm task note 1 '%s'" % phrase,
                "git commit -m '%s'" % phrase):
            with self.subTest(cmd=cmd[:50]):
                self.assertIsNone(self.refused(cmd))

    def test_a_sed_script_that_executes_is_read_whole(self):  # noqa: VACUOUS_ASSERTION — the loop runs over a non-empty literal tuple, each asserted refused unconditionally
        """GNU sed runs text through its `e` command and the `e` flag of
        `s`, so those scripts are not prose."""
        for cmd in (
                "echo x | sed 's/.*/h %s cancel 1/e'" % RUN,
                "echo x | sed -e '1e echo %s %s' -e p" % (NOUN, RUN),
                "sed -n \"s/x/$G %s %s ci.yml/ep\" f" % (NOUN, RUN),
                "sed --expression='e $G %s cancel 1' f" % RUN,
                "sed -f s.sed -e 's/a/%s %s/' f" % (NOUN, RUN)):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(self.refused(cmd))


class RoundThreeGapsTest(unittest.TestCase):
    """Three gaps a fresh read of 1bdc92b2 found WORSE than main — each
    admitted by the lane and refused by main — and the heredoc-terminator
    exploit that made the copy run a body line as a command (task/3945)."""

    def refused(self, command):
        return chat.github_actions_refusal(command=command)

    def test_a_runner_inside_a_substitution_is_refused(self):  # noqa: VACUOUS_ASSERTION — the loop runs over a non-empty literal tuple, each asserted refused unconditionally
        """GAP 1: a program inside a substitution or process substitution
        inherits the enclosing pipe's or redirect's stdin, so a shell there
        RUNS the text the fold would blank, even with no quote in the
        substitution. The lane skipped a quote-free substitution."""
        h = "printf '%%sh %s %s ci.yml\\n' g" % (NOUN, RUN)
        for cmd in (
                "%s > >(sh)" % h,
                "%s | echo $(sh)" % h,
                "%s | tee >(sh)" % h,
                "%s | `cat`" % ("printf '%%sh %s %s ci.yml' g" % (NOUN, RUN)),
                "echo $(sh) < <(printf '%%sh %s cancel 1' g)" % RUN,
                "%s | helm task note 1 $(sh)" % h,
                "%s | xargs -I{} sh -c {}" % h):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(self.refused(cmd))

    # GAP 1's `$(date)`-beside-prose allowance was given up in round 4: a
    # live substitution is never prose (ALiveSubstitutionIsNeverProseTest).

    def test_a_value_program_word_is_refused(self):  # noqa: VACUOUS_ASSERTION — the loop runs over a non-empty literal tuple, each asserted refused unconditionally
        """GAP 2: a program word that is a runtime VALUE may be a shell, so
        it runs the text the fold would blank. A quoted-literal shell runs it
        too. The lane answered a value program word prose-only."""
        act = "$G %s %s ci.yml" % (NOUN, RUN)
        for cmd in (
                '$SH -c "%s"' % act,
                "$E sh -c '%s'" % act,
                '"$SH" -c \'%s\'' % act,
                '${SH} -c \'%s\'' % act,
                "echo '%s' | $SHELL" % act,
                "echo '%s' | $(echo sh)" % act,
                'V="%s %s ci.yml"; $G $V' % (NOUN, RUN),
                "read -r V <" + "<IN\n%s %s ci.yml\nIN\n$G $V" % (NOUN, RUN),
                'A=("%s %s" ci.yml); $G ${A[0]} ci.yml' % (NOUN, RUN),
                'echo "%s %s ci.yml" > /tmp/a; $G $(cat /tmp/a)'
                % (NOUN, RUN),
                "'sh' -c \"%s\"" % act,
                '"sh" -c "%s"' % act,
                # the given-up pin: a value program word is no longer prose,
                # so this moves to refused (main refuses it too)
                "E=\"env A=1\"; $E helm task note 3945 'the %s %s done'"
                % (NOUN, RUN)):
            with self.subTest(cmd=cmd[:50]):
                self.assertIsNotNone(self.refused(cmd))

    def test_a_bogus_prose_program_at_a_pipe_still_passes(self):  # noqa: VACUOUS_ASSERTION — the positive control is test_a_value_program_word_is_refused on the same rung, whose runtime-value and quoted-literal-shell program words are refused
        """GAP 2 does not over-reach: a quoted literal that spells no runner
        is prose standing where a program would, and stays admitted."""
        phrase = "the %s %s finished green" % (NOUN, RUN)
        self.assertIsNone(self.refused(
            "helm store add premise x | '%s'" % phrase))


class TheHeredocTerminatorExploitTest(unittest.TestCase):
    """GAP 3: `_CAT_DOCUMENT` accepted a tag line with a trailing space (and a
    tab-led tag under plain `<<`) as the end of the document. Bash does not,
    so the copy posted a short body and ran the rest as commands. Judged in
    real bash with a stub verb (task/3945)."""

    def setUp(self):
        if not shutil.which("bash"):
            self.skipTest("no bash")
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="helm-3945-term-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _creates_pwned(self, command):
        marker = _os.path.join(self.tmp, "PWNED")
        if _os.path.exists(marker):
            _os.remove(marker)
        subprocess.run(["bash", "--noprofile", "--norc", "-c",
                        command.replace(POST, "cat", 1)],
                       cwd=self.tmp, env={"PATH": "/usr/bin:/bin"},
                       capture_output=True, text=True, timeout=10)
        return _os.path.exists(marker)

    def test_a_trailing_space_tag_is_not_a_terminator(self):  # noqa: VACUOUS_ASSERTION — test_an_exact_tag_still_gets_its_copy is the positive control: it asserts a non-None copy whose bash body equals a literal; here the copy must be None or bash-safe, RED at 1bdc92b2 where the copy ran `touch`
        """The task's measured exploit: `EOF ` with a trailing space is not a
        bash terminator, so the original runs no `touch`; the lane's copy
        did. The copy must either be None or match bash (no `touch`)."""
        marker = _os.path.join(self.tmp, "PWNED")
        cmd = ("%s \"%scat <<'EOF'\nA %sx%s\nEOF \n)\"\ntouch %s; echo \"\n"
               "EOF\n)\"" % (POST, SUB, TICK, TICK, marker))
        self.assertIsNotNone(chat.argv_guard(cmd))
        self.assertFalse(self._creates_pwned(cmd))        # the original
        out = argv_cure.corrected_command(cmd)
        if out is not None:
            self.assertFalse(self._creates_pwned(out),
                             "the copy RAN a body line: %r" % out)

    def test_a_tab_led_tag_under_plain_heredoc_gets_no_copy(self):  # noqa: VACUOUS_ASSERTION — test_an_exact_tag_still_gets_its_copy is the positive control: it asserts a non-None copy whose bash body equals a literal; here the copy must be None or bash-safe, RED at 1bdc92b2 where the copy ran `touch`
        """A tab before the tag ends the document only under `<<-`; under
        plain `<<` bash reads the tab-led line as body, so the lane's copy
        posted a short body and ran the rest — the copy must be None or match
        bash (no `touch`)."""
        marker = _os.path.join(self.tmp, "PWNED")
        cmd = ("%s \"%scat <<'EOF'\nA %sx%s\n\tEOF\n)\"\ntouch %s; echo \"\n"
               "EOF\n)\"" % (POST, SUB, TICK, TICK, marker))
        self.assertIsNotNone(chat.argv_guard(cmd))
        self.assertFalse(self._creates_pwned(cmd))        # the original
        out = argv_cure.corrected_command(cmd)
        if out is not None:
            self.assertFalse(self._creates_pwned(out),
                             "the copy RAN a body line: %r" % out)

    def test_an_exact_tag_still_gets_its_copy(self):  # noqa: VACUOUS_ASSERTION — the copy is asserted non-None and guard-admitted unconditionally, then its bash body is compared by equality
        """The cure keeps the ordinary copy: an exact tag line still moves
        the document to a stdin heredoc."""
        cmd = ("%s \"%scat <<'EOF'\nbody %sx%s line two\nEOF\n)\""
               % (POST, SUB, TICK, TICK))
        out = argv_cure.corrected_command(cmd)
        self.assertIsNotNone(out)
        self.assertIsNone(chat.argv_guard(out))
        self.assertEqual(
            subprocess.run(["bash", "--noprofile", "--norc", "-c",
                            out.replace(POST, "cat", 1)],
                           cwd=self.tmp, env={"PATH": "/usr/bin:/bin"},
                           capture_output=True, text=True, timeout=10).stdout,
            "body %sx%s line two\n" % (TICK, TICK))

    def test_a_command_with_its_own_stdin_heredoc_gets_no_copy(self):  # noqa: VACUOUS_ASSERTION — the base copy is asserted non-None unconditionally, then the same document beside an outer stdin heredoc is asserted None unconditionally
        """When the verb already opens a heredoc of its own, the stdin move
        would post the wrong body — the lane produced a two-heredoc copy whose
        stdin is the OTHER body — so none is printed. The same document with no
        outer heredoc still gets its copy."""
        base = ("%s \"%scat <<'DOC'\nbody %sx%s\nDOC\n)\""
                % (POST, SUB, TICK, TICK))
        self.assertIsNotNone(argv_cure.corrected_command(base))
        withstdin = ("%s \"%scat <<'DOC'\nbody %sx%s\nDOC\n)\" <" + "<'IN'\n"
                     "stdin body\nIN") % (POST, SUB, TICK, TICK)
        self.assertIsNotNone(chat.argv_guard(withstdin))
        self.assertIsNone(argv_cure.corrected_command(withstdin))


class TheCopyCannotOpenTheRefusalTest(unittest.TestCase):
    """The copy's module loads on the refusal path inside the hook's outer
    fail-open try: a failed import must cost the copy, never the refusal."""

    def test_a_failed_import_still_refuses(self):
        cmd = '%s "x %sy%s"' % (POST, TICK, TICK)
        with mock.patch.dict(_sys.modules, {"helm.argv_cure": None}):
            rc, out = hook(cmd)
        self.assertEqual(rc, 2, out)
        self.assertIn("BLOCKED", out)
        self.assertNotIn(argv_cure.CORRECTED, out)


class ALiveSubstitutionIsNeverProseTest(unittest.TestCase):
    """Round 4 (task/3945): the prose cure blanked a double-quoted argument
    that held a LIVE substitution, so a helm verb outside the argv guard's
    body verbs (task note, lr, handoff, asks) carried `$(rm -rf ~)` past the
    Actions rung that main refused it at. A double-quoted argument with a
    live substitution is now never blanked: it is read exactly as main reads
    it. Each refusing arm below was admitted at 7cd11e17."""

    PROSE = "the %s %s broke: " % (NOUN, RUN)
    VERBS = ("helm task note 1", "helm lr", "helm handoff seat-a",
             "helm asks")
    PAYLOADS = (SUB + "rm -rf ~)", TICK + "rm -rf ~" + TICK,
                SUB + "touch /tmp/helm-3945-pwned)")

    def refused(self, command):
        return chat.github_actions_refusal(command=command)

    def assert_refused_end_to_end(self, cmd):
        self.assertIsNotNone(self.refused(cmd))
        rc, out = hook(cmd)
        self.assertEqual(rc, 2, out)
        self.assertIn("BLOCKED", out)

    def test_a_live_substitution_inside_prose_is_refused(self):  # noqa: VACUOUS_ASSERTION — the nested loops run over non-empty literal tuples, each arm asserted refused unconditionally at the rung and through the hook
        for verb in self.VERBS:
            for payload in self.PAYLOADS:
                cmd = '%s "%s%s"' % (verb, self.PROSE, payload)
                with self.subTest(cmd=cmd):
                    self.assert_refused_end_to_end(cmd)

    def test_the_date_beside_prose_allowance_is_given_up(self):  # noqa: VACUOUS_ASSERTION — the loop runs over a non-empty literal tuple, each asserted refused unconditionally
        """The scope cut's price: prose naming a workflow run beside a live
        substitution, even one that only prints, is refused, as on main."""
        phrase = "the %s %s is green" % (NOUN, RUN)
        for cmd in (
                'helm task note 1 "ran at %sdate): %s"' % (SUB, phrase),
                'git commit -m "%sdate): %s %s merged"' % (SUB, NOUN, RUN),
                'helm task comment 1 "%s at %sdate -u +%%H:%%M)"'
                % (phrase, SUB),
                'helm task note 1 "a Workflow agent %s at %sdate)"'
                % (RERUN, SUB)):
            with self.subTest(cmd=cmd[:50]):
                self.assertIsNotNone(self.refused(cmd))

    def test_text_with_no_live_substitution_still_passes(self):  # noqa: VACUOUS_ASSERTION — read beside the two refusing tests above on the same rung, whose live-substitution spellings of the same prose are refused
        """Single-quoted text and an escaped `$(` run nothing, so their
        prose stays blankable."""
        for cmd in (
                "helm task note 1 '%s%srm -rf ~)'" % (self.PROSE, SUB),
                'helm task note 1 "%s\\%srm -rf ~)"' % (self.PROSE, SUB),
                "helm lr '%s%srm -rf ~%s'" % (self.PROSE, TICK, TICK)):
            with self.subTest(cmd=cmd[:50]):
                self.assertIsNone(self.refused(cmd))


if __name__ == "__main__":
    unittest.main()
