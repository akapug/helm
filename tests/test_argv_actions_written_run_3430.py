#!/usr/bin/env python3
"""The GitHub-Actions rung, the leaks task/3430 lists after the item-6 scope
cut (task/3382 item 6, task/3144), each measured on trunk 1dcf4989:

  (1) echo or printf text written to a file, then run in the same command
      (`echo '<act>' > x.sh; bash x.sh`), is admitted: the printer's quoted
      prose is data to the cut, and nothing asks where the printer's output
      went;
  (2) `cat > a.sh <<'EOF'` ... `EOF`, then `bash a.sh`, reads 0: the quoted
      body of cat is data, so it is cut, and an anchorless row in it
      (`gh run cancel 123`) is not evidence because no shell reads the body
      on the command line (F-A of the lane's findings, the trunk half);
  (3) `bash -c "$(< x)"` or `"$(cat x)"` after writing x is admitted;
  (4) the integrator's sed status line (tests/fixtures/
      argv_actions_sed_status.txt) was a measured false refusal;
  (5) a local seat's 160 refusals in 233 minutes on `python3 -c` test
      strings that import helm stay refused (python that imports helm can
      reach a denied primitive, so its text is code), and the cure there is
      a refusal that names the move which works, not a data reading.

THE CURE FOR (1)-(3) IS ONE RULE: a data program whose OUTPUT reaches a file
the same command RUNS keeps its words and its bodies as code. Only that
program is uncut. A commit message, a post or a grep pattern elsewhere on the
line, whose output reaches no run file, stays data, which is the friction the
abandoned lane's whole-command reading cost (F-C of its findings).

THE REVIEW OF THAT CURE (8c33ae6ae3e6) found five more, each pinned below:
F1 a trailing heredoc opener crashed the python-text hint after the act was
found, and the hook admitted; F2 the name search cost grew with the square
of the command and passed the hook's 2 s timeout, which admits; F3 a loop or
group that only READS a written file was refused; F4 a rewrite through a
data program (`cat a > b.sh`) leaked; F5 a glob or value script operand and
an interpreter's -c/-e text leaked.

Every spelling is built at runtime, as tests/test_chat_argv_guard.py does, so
this FILE holds no whole one.
"""
import contextlib
import io
import json
import os as _os
import re
import sys as _sys
import time
import unittest
from unittest import mock

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(
    _os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-actions-written-", var="HELM_HOME")

from helm import chat  # noqa: E402

NOUN = "work" + "flow"
RUN = "run"
ROW = RUN + " re" + RUN                  # the integrator's pair (fixture)
ACT = "gh %s %s ci.yml" % (NOUN, RUN)    # an anchored act
CANCEL = "gh %s can" "cel 123" % RUN     # an ANCHORLESS row: ordinary words
OVERRIDE = "HELM_ALLOW_GITHUB_ACTIONS=1"
MINT = "owner" + "_door"
FIXTURE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                        "fixtures", "argv_actions_sed_status.txt")
BUDGET = 470                   # tests/test_hook_budgets.py, refusal-ci-runner


class _Hook(unittest.TestCase):
    """Drives the SHIPPED hook entry with a real PreToolUse payload."""

    RULE = "never a GitHub runner"

    def hook(self, command):
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                   "session_id": "sess-3430", "cwd": "/tmp/repo",
                   "tool_use_id": "toolu_3430",
                   "tool_input": {"command": command}}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(_os.environ, {
                "HELM_HOME": _os.environ["HELM_HOME"],
                "HELM_ADOPTED_DIR": _os.path.join(_os.environ["HELM_HOME"],
                                                  "adopted"),
        }), mock.patch.object(_sys, "stdin",
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
        self.assertIn(OVERRIDE, out, command[:200])
        return out


class WrittenThenRunIsCodeTest(_Hook):
    """(1)-(3): text a data program writes to a file the same command runs
    is that file's script, so it is code, whatever route runs the file."""

    def test_a_printer_writing_a_file_the_command_runs(self):  # noqa: VACUOUS_ASSERTION — an unconditional assert_allowed runs first on the same hook entry, so a spurious refusal fails there before any refuse is reached
        self.assert_allowed("echo hi > x.sh; bash x.sh")
        for command in (
                # task/3430 (1), verbatim shapes
                "echo '%s' > x.sh; bash x.sh" % ACT,
                "printf '%%s\\n' '%s' > /tmp/x.sh && sh /tmp/x.sh" % ACT,
                # appended, made executable and run by path, fed on stdin
                "echo '%s' >> x.sh; bash x.sh" % ACT,
                "echo '%s' > x.sh; chmod +x x.sh; ./x.sh" % ACT,
                "echo '%s' > x.sh; bash < x.sh" % ACT,
                # the output reaches the file through a later pipe stage
                "echo '%s' | tee x.sh; bash x.sh" % ACT,
                "echo '%s' | cat > x.sh; bash x.sh" % ACT,
                # a group or exec moves the printer's stdout to the file
                "{ echo '%s'; } > x.sh; bash x.sh" % ACT,
                "exec > x.sh; echo '%s'; bash x.sh" % ACT,
                # python writes the file itself, no redirect to see
                "python3 -c \"open('x.sh', 'w').write('%s')\"; bash x.sh"
                % ACT):
            with self.subTest(command=command[:80]):
                self.assert_refused(command)

    def test_a_quoted_body_written_to_a_file_the_command_runs(self):  # noqa: VACUOUS_ASSERTION — an unconditional assert_allowed runs first on the same hook entry, so a spurious refusal fails there before any refuse is reached
        self.assert_allowed("cat > a.sh <<'EOF'\necho hi\nEOF\nbash a.sh")
        for command in (
                # task/3430 (2): the anchorless row, and an anchored one
                "cat > a.sh <<'EOF'\n%s\nEOF\nbash a.sh" % CANCEL,
                "cat > a.sh <<'EOF'\n%s\nEOF\nbash a.sh" % ACT,
                "cat <<'EOF' > a.sh\n%s\nEOF\nbash a.sh" % CANCEL,
                "tee a.sh <<'EOF'\n%s\nEOF\nsh a.sh" % CANCEL,
                # a python script that starts a process, written then run
                "cat > x.py <<'EOF'\nimport subprocess\nsubprocess.run('%s', "
                "shell=True)\nEOF\npython3 x.py" % ACT):
            with self.subTest(command=command[:80]):
                self.assert_refused(command)

    def test_a_written_file_read_back_into_a_shell(self):  # noqa: VACUOUS_ASSERTION — an unconditional assert_allowed runs first on the same hook entry, so a spurious refusal fails there before any refuse is reached
        self.assert_allowed("echo hi > x; bash -c \"$(< x)\"")
        for command in (
                # task/3430 (3)
                "echo '%s' > x; bash -c \"$(< x)\"" % ACT,
                "echo '%s' > x; bash -c \"$(cat x)\"" % ACT,
                "echo '%s' > x; sh -c \"`cat x`\"" % ACT,
                "echo '%s' > /tmp/x.sh; sh -c '. /tmp/x.sh; true'" % ACT,
                "echo '%s' > x; bash <<EOF\n$(cat x)\nEOF" % ACT,
                # stdin routes the reviewer measured with real bash
                "echo '%s' > x; { bash; } < x" % ACT,
                "echo '%s' > x; (bash) < x" % ACT,
                "echo '%s' > x; exec < x; bash" % ACT,
                "echo '%s' > x; exec 3< x; bash <&3" % ACT,
                "echo '%s' > x; while read -r l; do bash -c \"$l\"; done < x"
                % ACT):
            with self.subTest(command=command[:80]):
                self.assert_refused(command)

    def test_a_written_file_renamed_or_run_by_another_runner(self):  # noqa: VACUOUS_ASSERTION — an unconditional assert_allowed runs first on the same hook entry, so a spurious refusal fails there before any refuse is reached
        self.assert_allowed("echo hi > x; mv x y.sh; bash y.sh")
        for command in (
                "echo '%s' > x; mv x y.sh; bash y.sh" % ACT,
                "echo '%s' > x; cp x y.sh; bash y.sh" % ACT,
                "echo '%s' > x.sh; find . -name x.sh -exec sh {} \\;" % ACT,
                "echo '%s' > x.sh; xargs -a x.sh -I{} sh -c {}" % ACT,
                "echo '%s' > x; awk '{system($0)}' x" % ACT,
                "printf 'all:\\n\\t%s\\n' > Makefile; make" % ACT,
                "echo '%s' > x.sh; busybox sh x.sh" % ACT,
                "echo '%s' > x.sh; git -c alias.x='!sh x.sh' x" % ACT,
                "echo '%s' > x.sh; $SH x.sh" % ACT):
            with self.subTest(command=command[:80]):
                self.assert_refused(command)


class DataThatReachesNoRunFileStaysDataTest(_Hook):
    """The friction the whole-command reading cost (F-C): only the program
    whose output reaches a run file is uncut. Every shape here is admitted on
    trunk and must stay admitted."""

    def test_data_beside_a_run_it_never_reaches(self):  # noqa: VACUOUS_ASSERTION — an unconditional assert_refused of a real act runs first on the same hook entry, so an empty observable fails there before any allow is reached
        self.assert_refused(ACT)
        for command in (
                "echo 'note: %s' > notes.txt; cat notes.txt" % ACT,
                "echo '%s' > x.sh; cat x.sh" % ACT,
                "cat > notes.md <<'EOF'\n%s\nEOF\ngit add notes.md" % ACT,
                "git commit -qm 'never %s'; bash ./check.sh" % ACT,
                "helm chat post 'do not %s'; bash ./check.sh" % ACT,
                # a written file handed to a script as an ARGUMENT is data
                # to that script, never its script
                "rg -n '%s' docs > hits; bash report.sh hits" % ACT,
                "cat > notes.md <<'EOF'\n%s\nEOF\nbash ./publish.sh notes.md"
                % ACT,
                # a syntax check and a byte-compile run nothing
                "echo '%s' > x.sh; bash -n x.sh" % ACT,
                "cat > x.py <<'EOF'\nprint('%s')\nEOF\npython3 -m py_compile "
                "x.py" % ACT,
                # an INERT python script written then run prints its text
                "cat > p.py <<'EOF'\nprint('%s')\nEOF\npython3 p.py" % ACT):
            with self.subTest(command=command[:80]):
                self.assert_allowed(command)


    def test_the_corpus_shapes_a_written_file_never_runs_in(self):  # noqa: VACUOUS_ASSERTION — an unconditional assert_refused of a real act runs first on the same hook entry, so an empty observable fails there before any allow is reached
        """Measured over 14,938 real commands from ten days of transcripts:
        each shape here read the written file as run under a first cut of
        this rule and was refused, and none of them runs it."""
        self.assert_refused(ACT)
        for command in (
                # a file handed to a program as an ARGUMENT, inside a -c text
                # the reader settles: helm is not a shell
                "cat > ci.txt <<'EOF'\n%s\nEOF\nbash -c 'helm task add x "
                "\"$(cat ci.txt)\"'" % ACT,
                # xargs feeding a file's words to a program that is no shell
                "cat > n.txt <<'EOF'\n%s\nEOF\nxargs -0 -a n.txt helm task "
                "close 1" % ACT,
                # python writes notes.md; its text only MENTIONS the script
                "python3 - <<'PY'\nopen('notes.md', 'w').write('run "
                "tools/check.sh after %s')\nPY\nbash tools/check.sh" % ACT,
                # identity config is not a git that runs a command
                "python3 - <<'EOF'\np = 't.py'\ns = open(p).read()\n"
                "open(p, 'w').write(s.replace('a', '%s'))\nEOF\n"
                "git -c user.name=a -c user.email=b commit -qam x" % ACT,
                # a loop elsewhere on the line redirects nothing
                "for d in a b; do echo $d; done; cat > docs/x.md <<'EOF'\n"
                "%s\nEOF\ncat > t.sh <<'EOF'\necho hi\nEOF\nbash t.sh" % ACT,
                # a script an INTERPRETER runs is read as a non-inert python
                # body is: its anchorless rows are not evidence
                "cat > p.py <<'EOF'\nimport subprocess\n# %s is a note\n"
                "EOF\npython3 p.py" % CANCEL):
            with self.subTest(command=command[:80]):
                self.assert_allowed(command)

    def test_python_that_names_the_run_file_as_a_path_writes_it(self):  # noqa: VACUOUS_ASSERTION — an unconditional assert_allowed runs first on the same hook entry, so a spurious refusal fails there before any refuse is reached
        self.assert_allowed("python3 - <<'EOF'\np = '/tmp/x.sh'\n"
                            "open(p, 'w').write('echo hi')\nEOF\n"
                            "bash /tmp/x.sh")
        for command in (
                "python3 - <<'EOF'\np = '/tmp/x.sh'\nopen(p, 'w').write("
                "'%s')\nEOF\nbash /tmp/x.sh" % ACT,
                "python3 -c \"open(f'{1}/x.sh', 'w').write('%s')\"; "
                "bash ./x.sh" % ACT):
            with self.subTest(command=command[:80]):
                self.assert_refused(command)


class TheSedStatusLineTest(_Hook):
    """(4): the integrator's refused status line, reconstructed. It is
    admitted on trunk 1dcf4989 (task/3696 made the re-execute row name gh),
    and this pins it."""

    def test_the_integrators_sed_status_line_is_admitted(self):  # noqa: VACUOUS_ASSERTION — an unconditional assert_refused of a real act runs first on the same hook entry
        self.assert_refused(ACT)
        with open(FIXTURE, encoding="utf-8") as fh:
            command = fh.read().rstrip("\n").replace("{ROW}", ROW)
        self.assertIn(ROW + " alone on one build host", command)
        self.assert_allowed(command)


class PythonTextRefusalNamesTheMoveTest(_Hook):
    """(5): python text that imports helm is code, so an Actions spelling in
    a test string there stays refused. The refusal says so and names the
    move that works, and that move is admitted."""

    PY = "python3 -c \"from helm import chat; print(chat." \
         "github_actions_refusal('%s'))\""
    MOVE = "in pieces"

    def test_the_refusal_names_the_move_and_the_move_works(self):  # noqa: VACUOUS_ASSERTION — assert_refused runs first and unconditionally on the same hook entry, so an empty observable fails there before the allow is reached
        out = self.assert_refused(self.PY % ACT)
        self.assertIn(self.MOVE, out)
        line = [l for l in out.splitlines() if "BLOCKED" in l][0]
        self.assertLessEqual(len(line), BUDGET, line)
        heredoc = ("python3 - <<'EOF'\nfrom helm import chat\n"
                   "print(chat.github_actions_refusal('%s'))\nEOF" % ACT)
        self.assertIn(self.MOVE, self.assert_refused(heredoc))
        # the move, spelled as the refusal spells it, is admitted
        self.assertIn("'%s'+'%s'" % (NOUN[:4], NOUN[4:]), out)
        self.assert_allowed(self.PY % ("gh '+'" + NOUN[:4] + "'+'"
                                       + NOUN[4:] + " " + RUN + " ci.yml"))

    def test_the_hint_is_only_for_python_text(self):  # noqa: VACUOUS_ASSERTION — each absence is read off a refusal assert_refused has just proved non-empty
        # an act on the command line beside python text gets no python hint
        out = self.assert_refused("%s; python3 -c \"import helm\"" % ACT)
        self.assertNotIn(self.MOVE, out)
        out = self.assert_refused(ACT)
        self.assertNotIn(self.MOVE, out)


class ATrailingHeredocOpenerTest(_Hook):
    """Review F1 (HIGH): an act, then a heredoc opener on the LAST line with
    no newline after it. The python-text hint indexed the body's start one
    line past the end, raised IndexError after the act was found, and the
    hook admitted all three shapes, which trunk refuses. A defect in the
    hint must cost the hint, never the refusal."""

    def test_a_trailing_opener_with_no_body_is_refused(self):  # noqa: VACUOUS_ASSERTION — every member asserts rc 2 and the BLOCKED line through assert_refused over a fixed non-empty tuple
        for command in (
                "%s; cat <<'EOF'" % ACT,
                "%s <<'EOF'" % ACT,
                "python3 -c \"import helm; x='%s'\" <<'EOF'" % ACT):
            with self.subTest(command=command[:80]):
                self.assert_refused(command)
                self.assertIs(chat._python_test_text(command), False)

    def test_a_defect_in_the_hint_costs_the_hint_not_the_refusal(self):  # noqa: VACUOUS_ASSERTION — assert_refused first asserts rc 2 and the BLOCKED line on the same output the absence is read from
        with mock.patch.object(chat, "_python_test_text",
                               side_effect=IndexError("a reader defect")):
            out = self.assert_refused(ACT)
        self.assertNotIn("in pieces", out)


class TheCostIsLinearTest(unittest.TestCase):
    """Review F2 (MEDIUM): the name search compiled one regex per (written
    file, text) pair, and past the re module's 512-pattern cache every one
    was compiled again: 800 writes and one run took 19.9 s, and the hook's
    2 s timeout ADMITS. The work is counted, not timed: at most one name
    pattern is compiled per call. The wall bound is only a backstop against
    the 19.9 s regression, not a speed grade (see WALL_S)."""

    N = 800
    EDGE = r"(?<![\w.-])"          # every name pattern starts here
    # THE BACKSTOP IS NOT THE CONTRACT (task/4151). The count above is the
    # claim -- at most one name pattern compiled per call -- and it holds on
    # any host. This bound only catches a REGRESSION back to 800 compiles,
    # which took 19.9 s; it is not a speed grade. Halved from the hook's 2 s
    # budget it still flaked in a loaded whole-suite gate, which is the same
    # mistake the sibling arms made in the other direction: a threshold
    # inside a gap too narrow to hold the two cases apart. 8 s sits well
    # above a loaded run of the correct shape and far below the 19.9 s the
    # defect produces, so it discriminates without asking WAS THE BOX FAST.
    WALL_S = 8.0

    def measure(self, command):
        compiler = getattr(re, "_compiler", None)
        if compiler is None:                # before 3.11
            import sre_compile as compiler
        real, compiled = compiler.compile, []

        def counting(pattern, flags=0):
            if isinstance(pattern, str) and self.EDGE in pattern:
                compiled.append(pattern)
            return real(pattern, flags)
        started = time.perf_counter()
        with mock.patch.object(compiler, "compile", counting):
            got = chat.github_actions_refusal(command)
        return got, len(compiled), time.perf_counter() - started

    def command(self, name, act_file=False):
        writes = ["echo hi > %s" % (name % i) for i in range(self.N)]
        if act_file:
            writes[-1] = "echo '%s' > %s" % (ACT, name % (self.N - 1))
        return "%s; bash %s; git commit -qm 'never %s'" % (
            "; ".join(writes), name % (self.N - 1), ACT)

    def test_eight_hundred_writes_compile_at_most_one_name_pattern(self):  # noqa: VACUOUS_ASSERTION — each member first asserts the rung's answer is the expected refusal or pass; the compile ceiling is the contract
        for name, act_file, refused in (
                ("f%d.sh", False, False), ("f%d.sh", True, True),
                # a name holding a blank takes the alternation, compiled once
                ("'f %d.sh'", False, False)):
            with self.subTest(name=name, act_file=act_file):
                got, compiled, wall = self.measure(self.command(name,
                                                                act_file))
                self.assertEqual(got is not None, refused, got)
                self.assertLessEqual(compiled, 1)
                self.assertLess(wall, self.WALL_S)


class ACompoundThatOnlyReadsTheFileTest(_Hook):
    """Review F3 (LOW): a redirect into `done`, `}`, `fi` or a subshell's
    close counted as a shell run whatever the compound held. Its stdin is
    run only where a command INSIDE it runs text: a shell, an interpreter,
    an evaluator, a runner, or a program word that is a value."""

    def test_a_compound_that_only_reads_the_file_is_admitted(self):  # noqa: VACUOUS_ASSERTION — an unconditional assert_refused of the same file into a group holding a shell runs first on the same hook entry
        self.assert_refused("echo '%s' > x; { bash; } < x" % ACT)
        for command in (
                "echo '%s' > x; while read -r l; do echo \"$l\"; done < x"
                % ACT,
                "echo '%s' > x; { read a; } < x" % ACT,
                "echo '%s' > x; (read a; echo \"$a\") < x" % ACT,
                "echo '%s' > x; if true; then cat; fi < x" % ACT):
            with self.subTest(command=command[:80]):
                self.assert_allowed(command)

    def test_a_compound_holding_a_runner_still_runs_its_stdin(self):  # noqa: VACUOUS_ASSERTION — every member asserts rc 2 and the BLOCKED line through assert_refused over a fixed non-empty tuple
        for command in (
                "echo '%s' > x; while read -r l; do eval \"$l\"; done < x"
                % ACT,
                "echo '%s' > x; while read -r l; do $l; done < x" % ACT,
                "echo '%s' > x; { read a; bash; } < x" % ACT,
                "echo '%s' > x; (python3) < x" % ACT,
                "echo '%s' > x; for i in 1; do sh; done < x" % ACT,
                "echo '%s' > x; if true; then { sh; }; fi < x" % ACT):
            with self.subTest(command=command[:80]):
                self.assert_refused(command)


class ARewriteThroughADataProgramTest(_Hook):
    """Review F4 (LOW): only cp, mv, ln, install and rsync passed "written"
    on, so a file rewritten by any other program, then run, leaked. A
    program whose output reaches a file and that reads a written file (an
    operand or its stdin) passes it on."""

    def test_a_rewrite_then_run_is_refused(self):  # noqa: VACUOUS_ASSERTION — an unconditional assert_allowed of the same rewrite with nothing run comes first on the same hook entry
        self.assert_allowed("echo '%s' > a; cat a > b.sh; cat b.sh" % ACT)
        for command in (
                "echo '%s' > a; cat a > b.sh; bash b.sh" % ACT,
                "echo '%s' > a; sed 's/x/y/' a > b.sh; bash b.sh" % ACT,
                "echo '%s' > a; cat < a > b.sh; bash b.sh" % ACT,
                "echo '%s' > a; cat a | tee b.sh; bash b.sh" % ACT,
                "echo '%s' > a; cat a | sort > b.sh; bash b.sh" % ACT,
                "echo '%s' > a; cat a > b; cp b c.sh; bash c.sh" % ACT,
                "echo '%s' > a; cat a > b; cat b > c; cat c > d.sh; "
                "bash d.sh" % ACT):
            with self.subTest(command=command[:80]):
                self.assert_refused(command)

    def test_an_ordinary_read_does_not_flow_into_its_redirected_stdout(self):  # noqa: VACUOUS_ASSERTION — the direct anchored act is unconditionally refused; fixed subtests prove reads stay data
        self.assert_refused(ACT)
        for command in (
                "echo '%s' > notes.md; git add notes.md > report.sh; "
                "bash report.sh" % ACT,
                "echo '%s' > notes.md; git -C repo add notes.md > report.sh; "
                "bash report.sh" % ACT,
                "echo '%s' > notes.md; git commit -qF notes.md > report.sh; "
                "bash report.sh" % ACT,
                "echo '%s' > notes.md; git --no-pager commit -qF notes.md "
                "> report.sh; bash report.sh" % ACT,
                "echo '%s' > notes.md; git commit --quiet --file notes.md "
                "> report.sh; bash report.sh" % ACT,
                "echo '%s' > notes.md; python3 -m py_compile notes.md > "
                "report.sh; bash report.sh" % ACT,
                "echo '%s' > notes.md; python3 -B -m py_compile notes.md "
                "> report.sh; bash report.sh" % ACT):
            with self.subTest(command=command[:80]):
                self.assert_allowed(command)

    def test_interactive_git_add_and_unquiet_commit_can_print_read_data(self):  # noqa: VACUOUS_ASSERTION — each fixed subtest asserts a real hook refusal, and the adjacent quiet/noninteractive test asserts allowance
        for command in (
                "echo '%s' > notes.md; git commit -F notes.md > report.sh; "
                "bash report.sh" % ACT,
                "echo '%s' > notes.md; git --no-pager commit -F notes.md "
                "> report.sh; bash report.sh" % ACT,
                "echo '%s' > notes.md; git add -p notes.md </dev/null "
                "> report.sh; bash report.sh" % ACT,
                "echo '%s' > notes.md; git -C repo add --patch notes.md "
                "</dev/null > report.sh; bash report.sh" % ACT,
                "echo '%s' > notes.md; git add -i notes.md </dev/null "
                "> report.sh; bash report.sh" % ACT,
                "echo '%s' > notes.md; git add -Ap notes.md </dev/null "
                "> report.sh; bash report.sh" % ACT,
                "echo '%s' > notes.md; git -c color.ui=auto commit "
                "-F notes.md > report.sh; bash report.sh" % ACT):
            with self.subTest(command=command[:80]):
                self.assert_refused(command)

    def test_a_file_that_is_read_but_never_written_stays_data(self):  # noqa: VACUOUS_ASSERTION — an unconditional assert_refused of a real act runs first on the same hook entry
        self.assert_refused(ACT)
        for command in (
                "echo '%s' > notes.md; cat notes.md > copy.md; "
                "bash ./check.sh" % ACT,
                "rg -n '%s' tools/x.sh > hits.txt; bash tools/x.sh" % ACT):
            with self.subTest(command=command[:80]):
                self.assert_allowed(command)


class MoreWriteThenRunSpellingsTest(_Hook):
    """Review F5 (LOW): a glob or value script operand named no written
    file, and an interpreter's -c or -e text was skipped. A glob counts the
    written files it matches, a value counts every one whose name it could
    be, and interpreter text counts the written files it names and, for
    python, the modules it imports (`-m` too)."""

    PY_SPAWN = "import subprocess\nsubprocess.run('%s', shell=True)" % ACT

    def test_more_spellings_of_write_then_run_are_refused(self):  # noqa: VACUOUS_ASSERTION — every member asserts rc 2 and the BLOCKED line through assert_refused over a fixed non-empty tuple
        for command in (
                "echo '%s' > x.sh; bash *.sh" % ACT,
                "echo '%s' > x.sh; bash ./x*.sh" % ACT,
                "echo '%s' > x.sh; for f in *.sh; do bash \"$f\"; done" % ACT,
                "echo '%s' > x.sh; bash < \"$f\"" % ACT,
                "echo '%s' > x.sh; bash {x,y}.sh" % ACT,
                "cat > mod_x.py <<'EOF'\n%s\nEOF\npython3 -c 'import mod_x'"
                % self.PY_SPAWN,
                "cat > mod_x.py <<'EOF'\n%s\nEOF\npython3 -m mod_x"
                % self.PY_SPAWN,
                "cat > x.py <<'EOF'\n%s\nEOF\npython3 -c \"exec(open('x.py')"
                ".read())\"" % self.PY_SPAWN,
                "echo '%s' > x.sh; perl -e 'system(\"sh x.sh\")'" % ACT):
            with self.subTest(command=command[:80]):
                self.assert_refused(command)

    def test_what_the_new_spellings_cannot_name_stays_data(self):  # noqa: VACUOUS_ASSERTION — an unconditional assert_refused of a real act runs first on the same hook entry
        self.assert_refused(ACT)
        for command in (
                # a glob that cannot match, and a value whose name is literal
                "echo '%s' > x.sh; bash *.txt" % ACT,
                "echo '%s' > notes.md; bash \"$HOME/bin/check.sh\"" % ACT,
                # inert python imported prints its text
                "cat > p.py <<'EOF'\nprint('%s')\nEOF\npython3 -c 'import p'"
                % ACT,
                # a byte-compile runs nothing, whatever the file holds
                "cat > x.py <<'EOF'\n%s\nEOF\npython3 -m py_compile x.py"
                % self.PY_SPAWN):
            with self.subTest(command=command[:80]):
                self.assert_allowed(command)


class TheShapesTheReviewConfirmedTest(_Hook):
    """The review's confirmed shapes on the tip, pinned so no cure round moves
    them: each write-then-run spelling stays refused, and each read of a
    written file stays admitted."""

    def test_the_confirmed_refusals_stay_refused(self):  # noqa: VACUOUS_ASSERTION — every member asserts rc 2 and the BLOCKED line through assert_refused over a fixed non-empty tuple
        for command in (
                "echo '%s' | tee -a x.sh >/dev/null; bash x.sh" % ACT,
                "printf '%%s\\n' '%s' > x.sh; . x.sh" % CANCEL,
                "cat <<EOF > x.sh\n%s\nEOF\nsh x.sh" % CANCEL,
                "echo '%s' > x.sh; . ./x.sh" % ACT,
                "echo '%s' > x.sh; source x.sh" % ACT,
                "cat <<EOF > x.sh\n%s\nEOF\nchmod +x x.sh && ./x.sh" % ACT,
                "echo '%s' > x.sh; cat x.sh | bash" % ACT,
                "echo '%s' > x.sh; bash <(cat x.sh)" % ACT,
                "echo '%s' > x.sh; nohup bash x.sh &" % ACT,
                "echo '%s' > x.sh; timeout 5 sh x.sh" % ACT,
                "echo '%s' > x.sh; env -i bash x.sh" % ACT,
                "echo '%s' | sudo tee x.sh; sudo bash x.sh" % ACT,
                "echo '%s' > x.txt; mv x.txt x.sh && chmod +x x.sh && ./x.sh"
                % ACT,
                "echo '%s' > x.txt; cp x.txt x.sh && sh x.sh" % ACT):
            with self.subTest(command=command[:80]):
                self.assert_refused(command)

    def test_the_confirmed_reads_stay_admitted(self):  # noqa: VACUOUS_ASSERTION — an unconditional assert_refused of a real act runs first on the same hook entry
        self.assert_refused(ACT)
        for command in (
                "cat > notes.md <<'EOF'\n%s\nEOF\ngit add notes.md && git "
                "commit -qm notes" % ACT,
                "echo '%s' > x.sh; cat x.sh" % ACT,
                "cat > /tmp/brief.txt <<'EOF'\n%s\nEOF\nhelm dispatch send x "
                "< /tmp/brief.txt" % ACT,
                "cat > /tmp/msg.txt <<'EOF'\nfix: %s\nEOF\ngit commit -qF "
                "/tmp/msg.txt" % ACT,
                "echo '%s' > x.sh; bash -n x.sh" % ACT,
                # -m runs the module it names; a test module a runner loads
                # from its ARGUMENTS is outside the rule (HOOKS.md)
                "cat > tests/test_x.py <<'EOF'\nimport unittest, subprocess\n"
                "X = '%s'\nEOF\npython3 -m unittest tests/test_x.py" % ACT,
                "cat > /tmp/gen.py <<'EOF'\nfrom pathlib import Path\n"
                "print('%s')\nEOF\npython3 /tmp/gen.py" % ACT):
            with self.subTest(command=command[:80]):
                self.assert_allowed(command)


class TheOwnerPostureRungAsksTheSameQuestionTest(unittest.TestCase):
    """The owner-posture rung cuts with the same fold, so a mint written to a
    file the command runs is a mint there too."""

    def test_a_written_then_run_mint_is_a_mint(self):  # noqa: VACUOUS_ASSERTION — every member asserts equality with a non-empty tuple over a fixed non-empty tuple
        body = "python3 -c 'from helm.ownerasks import %s; %s(1)'" % (
            MINT, MINT)
        for command in (
                "echo \"%s\" > x.sh; bash x.sh" % body,
                "cat > x.sh <<'EOF'\n%s\nEOF\nbash x.sh" % body):
            with self.subTest(command=command[:80]):
                self.assertEqual(chat.owner_posture_forge_refusal(command),
                                 ("mint", None))
        self.assertIsNone(chat.owner_posture_forge_refusal(
            "echo \"%s\" > notes.md" % body))


if __name__ == "__main__":
    unittest.main()
