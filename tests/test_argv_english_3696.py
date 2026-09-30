#!/usr/bin/env python3
"""The GitHub-Actions rung's two MEASURED false refusals of task/3696, and
every real act the rung owes still refused beside them.

task/3696, measured on the integrator's own commands:
  (2a) a `sed -i '2i ...'` writing a plain-English state line into a scratch
       file was refused as the re-execute row: the line said "rerun
       release.py 0.3.2 dry run". Two English words, `run` and `rerun`, and
       the row asked only that both stand anywhere in the text. No gh.
  (2b) a python heredoc that builds a report page was refused as the same
       row "(in a heredoc body)": the English `run` of "ours were run as
       served", and a raw-string regex `r'<tick>([^<tick>]+)<tick>'` whose
       backtick pair the fold reads as an expansion inside the word `r`,
       leaving `r<mark>`, which the re-execute anchor reads as its own
       spelling with the mark standing for the other four letters. No gh.

THE CURE, in two parts:
  (1) the re-execute row names the GitHub HEAD it relies on: `gh`, `run`
      and `rerun`, as a set. Two English words are no longer the row.
  (2) the SCOPED rule (an anchor beside an unresolved expansion) needs more
      of the anchor than its first character. One typed letter and a mark
      is the expansion alone, which is the blanket rule the anchor exists
      to buy out. The ROW rule keeps that reading, because there the row's
      other pieces are the evidence.

Every spelling is built at runtime, as tests/test_chat_argv_guard.py does, so
this FILE holds no whole one.
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
_tmp_home(prefix="helm-test-argv-english-", var="HELM_HOME")

from helm import chat  # noqa: E402

RUN = "run"
RERUN = "re" + RUN
NOUN = "work" + "flow"
TICK = "`"
GH = "g" + "h"
ACT = "%s %s %s 123" % (GH, RUN, RERUN)
OVERRIDE = "HELM_ALLOW_GITHUB_ACTIONS=1"

# (2a), verbatim but for the scratch path, which named a session directory,
# and a seat handle, which is now "the reviewer"
STATE_LINE = (
    "17:48Z readme-035 fixes committed 500e4f2b588 (all 5 critic P1s + "
    "P2/P3 on README side; audits trunk+lane Ran 8138 OK); pushed lane "
    "branch; SENT to the reviewer's review chain 684006979aa0 (--new-work). "
    "release-cut-032: merged trunk (00316b8ea96) + 87fd463fb44 (brief "
    "--report/re-ring wording, 0.3.1-and-earlier, bullets for trains "
    "469-471, ### Docs). 0.3.1-down byte-identical to published. Release "
    "lane lands LAST: after 3643 lands, update summary + web console "
    "bullets, %s release.py 0.3.2 dry %s on exact trunk, then non-author "
    "read. Left chat.py:7271 comment alone (dated measurement against "
    "436424d9a8c, still accurate). meta: 3643 slice2 removes auto-land "
    "close-at-land; I said fine." % (RERUN, RUN))
SED_NOTE = ("sed -i '2i %s' /tmp/scratch/loop-prompt-new14.txt; echo ok"
            % STATE_LINE)

# (2b), the lines of the measured body that carry the two readings, in the
# measured command's shape: a copy, then python reading a quoted heredoc,
# then a listing piped to `cut` (the pipe stage is why the body was read)
REPORT = (
    "mkdir -p /tmp/pages && cp /tmp/report.md /tmp/pages/r.md && "
    "python3 - <<'EOF'\n"
    "import html, re\n"
    "md=open('/tmp/pages/r.md').read()\n"
    "pre=(\"- **Local models were mostly tested without tuning.** A paid "
    "vendor's team tunes its hosted models; ours were %s as served. Their "
    "weak numbers are provisional.\\n\")\n"
    "def inline(s):\n"
    "    s=html.escape(s)\n"
    "    s=re.sub(r'%s([^%s]+)%s', r'<code>\\1</code>', s)\n"
    "    return s\n"
    "open('/tmp/pages/r.html','w').write(inline(pre+md))\n"
    "EOF\n"
    "ls -la /tmp/pages/r.html | cut -c1-150" % (RUN, TICK, TICK, TICK))


class _Hook(unittest.TestCase):
    """Drives the SHIPPED hook entry with a real PreToolUse payload."""

    RULE = "never a GitHub runner"

    def hook(self, command):
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                   "session_id": "sess-3696", "cwd": "/tmp/repo",
                   "tool_use_id": "toolu_3696",
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
        self.assertIn(OVERRIDE, out, command[:200])
        return out

    def refused(self, command):
        return chat.github_actions_refusal(command=command)


class MeasuredIncidentsTest(_Hook):
    """Both measured commands pass the hook, and each stands beside the
    real act it was mistaken for, refused by the same entry."""

    def test_the_state_note_written_by_sed_passes(self):  # noqa: VACUOUS_ASSERTION — the real act in the same sed shape is asserted refused first, on the same hook entry, so a rung that answered nothing fails there
        self.assert_refused("sed -i '2i x' /tmp/f.txt; %s" % ACT)
        self.assert_allowed(SED_NOTE)
        # the same words as an unquoted echo, and with the verb first
        self.assert_allowed("echo %s release.py dry %s" % (RERUN, RUN))
        self.assert_allowed("echo the dry %s needs a %s" % (RUN, RERUN))

    def test_the_report_heredoc_passes(self):  # noqa: VACUOUS_ASSERTION — the same body with the real act appended to it is asserted refused first, on the same hook entry
        self.assert_refused(REPORT.replace(
            "import html, re\n", "import html, re, os\nos.system('%s')\n"
            % ACT))
        self.assert_allowed(REPORT)

    def test_what_each_incident_matched_was_the_english_and_the_mark(self):  # noqa: VACUOUS_ASSERTION — every arm asserts a non-empty match with the shipped matchers before the row is asserted absent
        """THE DIAGNOSIS, pinned with the shipped matchers so it cannot drift
        from the code: (2a) holds both words typed, (2b) holds the English
        `run` and the re-execute anchor only as its first letter and the
        mark. Neither holds the GitHub head."""
        row = next(p for spelling, p, _says in chat._ACTIONS_ROWS
                   if RERUN in spelling.split(" "))
        head = [m for t, m, _a in row if t == GH]
        self.assertEqual(len(head), 1, "the re-execute row names gh")
        for command in (SED_NOTE, REPORT):
            text, _code = chat._invocation_text(command, actions=True)
            readings = chat._readings(text)
            words = [m for t, m, _a in row if t in (RUN, RERUN)]
            self.assertTrue(all(m.search(readings) for m in words), command)
            self.assertIsNone(head[0].search(readings), command)
        text, _code = chat._invocation_text(REPORT, actions=True)
        lone = "r" + chat._EXPANSION_MARK
        self.assertIn(lone, chat._readings(text))


class RealActsStillRefusedTest(_Hook):
    """EVERY real act the re-execute row and its anchor refused before the
    cure is refused after it. The row is a SET, so the GitHub head stands
    anywhere: before gh's global flags, after a path, inside a wrapper's
    string, a program's argv list, or a body a shell reads."""

    def test_the_re_execute_act_in_every_spelling(self):  # noqa: VACUOUS_ASSERTION — every arm asserts a non-None refusal over a fixed non-empty tuple
        for act in (
                ACT,
                "%s -R o/r %s %s 123" % (GH, RUN, RERUN),
                "%s %s --repo o/r %s 123" % (GH, RUN, RERUN),
                "%s %s %s --failed 123" % (GH, RUN, RERUN),
                "/usr/bin/%s %s %s 1" % (GH, RUN, RERUN),
                "command %s %s %s 1" % (GH, RUN, RERUN),
                "bash -c '%s'" % ACT,
                "echo '%s' | sh" % ACT,
                "bash <<'EOF'\n%s\nEOF" % ACT,
                "g\\h %s %s 1" % (RUN, RERUN),
                "\"g\"h %s %s 1" % (RUN, RERUN),
                "g$(echo h) %s %s 1" % (RUN, RERUN),
                "alias rr='%s %s %s'" % (GH, RUN, RERUN),
                "python3 - <<'EOF'\nimport subprocess\nsubprocess.run(['%s',"
                " '%s', '%s', '1'])\nEOF" % (GH, RUN, RERUN),
                # a runtime value in a word, and a whole token beside the anchor
                "%s %s r$(echo e%s) 1" % (GH, RUN, RUN),
                "%s %s r\"$(echo e%s)\" 1" % (GH, RUN, RUN),
                "%s %s r$(echo eru)n 1" % (GH, RUN),
                "%s r$(echo 'un rer')un 1" % GH,
                "%s %s re$(echo %s) 1" % (GH, RUN, RUN),
                "$GH %s %s 1" % (RUN, RERUN),
                "%s $W %s 123" % (GH, RERUN),
                "%s %s %s 1" % (GH, RUN, RERUN.replace("e", "E"))):
            with self.subTest(act=act):
                self.assertIsNotNone(self.refused(act), act)
                self.assert_refused(act)

    def test_every_other_row_is_untouched(self):  # noqa: VACUOUS_ASSERTION — every arm asserts a non-None refusal over a fixed non-empty tuple
        for act in (
                "%s %s %s ci.yml" % (GH, NOUN, RUN),
                "%s %s enable ci.yml" % (GH, NOUN),
                "%s %s %s ci.yml" % (GH, NOUN, "$V"),
                "%s w$(echo ork)flow %s ci.yml" % (GH, RUN),
                "%s api -X POST repos/o/r/actions/%ss/1/%s" % (GH, RUN, RERUN),
                "%s api -X POST repos/o/r/actions/%ss/ci.yml/dispatches"
                % (GH, NOUN),
                "%s api -X POST repos/o/r/dispatches -f event_type=x" % GH,
                "printf 'on: push' > .git" "hub/%ss/ci.yml" % NOUN,
                "cd .git" "hub && printf x > %ss/evil.yml" % NOUN,
                "%s %s cancel 1" % (GH, RUN),
                "%s %s watch 1" % (GH, RUN)):
            with self.subTest(act=act):
                self.assertIsNotNone(self.refused(act), act)


class OneLetterAnchorTest(_Hook):
    """THE SCOPED RULE NEEDS MORE OF THE ANCHOR THAN ITS FIRST CHARACTER.
    A word that begins with the anchor's first letter and continues in an
    expansion (`r$(date)`, `w$(date)`) is that expansion and one letter,
    and the rule refused it as the anchor beside a hole."""

    def test_one_letter_and_a_mark_is_not_an_anchor(self):  # noqa: VACUOUS_ASSERTION — the same words with the anchor typed past its first letter are asserted refused first, on the same hook entry
        for typed in ("echo re$(date)", "echo wo$(date)",
                      "ls -la re${X}"):
            self.assert_refused(typed)
        for command in ("echo r$(date)", "echo w$(date)", "ls -la r${X}",
                        "bash <<'EOF'\ns=re.sub(r'%s([^%s]+)%s', '', s)\nEOF"
                        % (TICK, TICK, TICK)):
            with self.subTest(command=command):
                self.assertIsNone(self.refused(command), command)
                self.assert_allowed(command)

    def test_the_row_rule_still_reads_one_letter_beside_the_rest_of_its_row(self):  # noqa: VACUOUS_ASSERTION — every arm asserts a non-None refusal over a fixed non-empty tuple
        for act in ("%s %s r$(echo e%s) 1" % (GH, RUN, RUN),
                    "%s w$(echo orkflow) enable ci.yml" % GH,
                    "%s w$(echo orkflow) %s ci.yml" % (GH, RUN)):
            with self.subTest(act=act):
                self.assertIsNotNone(self.refused(act), act)


class OnePieceRowTest(_Hook):
    """A ONE-PIECE ROW HAS NO OTHER PIECE TO BE THE EVIDENCE, so its piece
    takes the scoped rule's reading: a lone dot and an expansion mark is not
    the composite-action directory. Measured over this project's own 295,026
    distinct Bash and Monitor commands: after the re-execute cure, every one
    of the 18 composite-directory refusals the rung still made stood only on
    that reading, and none was an act."""

    DIRECTORY = ".git" + "hub/act" + "ions"

    def test_a_dot_and_an_expansion_is_not_the_directory(self):  # noqa: VACUOUS_ASSERTION — the directory typed past its dot, beside the same expansions, is asserted refused first on the same hook entry
        for act in ("mkdir -p %s/build" % self.DIRECTORY,
                    "cp a .$X/act" "ions/build/action.yml",
                    "cp a .git${H}ub/act" "ions/x.yml",
                    'cp a "$D/%s/x.yml"' % self.DIRECTORY):
            with self.subTest(act=act):
                self.assertIsNotNone(self.refused(act), act)
                self.assert_refused(act)
        for command in ('cp a "$D/.$N"', "ls -la $HOME/.${APP}",
                        "cat $S/.$(date +%s)"):
            with self.subTest(command=command):
                self.assertIsNone(self.refused(command), command)
                self.assert_allowed(command)


class WindowsBinaryNameTest(_Hook):
    """THE GITHUB HEAD HAS A SECOND NAME THAT IS ITS OWN: `gh.exe`, the
    GitHub CLI's binary on Windows, which a seat in WSL or Git Bash types to
    reach it. The head is a whole piece, so its word edge read the `.exe` as
    the middle of a longer word, and the re-execute act spelled with it
    passed where the trunk before task/3696 refused it, while the workflow
    and cancel rows, which name no head, still refused the same spelling
    (a fresh-context review of the lane). The same piece opens the bare
    REST-segment row, which read `gh.exe api` as no gh either."""

    def test_the_windows_binary_name_is_the_head(self):  # noqa: VACUOUS_ASSERTION — every arm asserts a non-None refusal and rc 2 over a fixed non-empty tuple, and the control asserts rc 0 on the same hook entry
        exe = GH + ".exe"
        for act in (
                "%s %s %s 123" % (exe, RUN, RERUN),
                "%s -R o/r %s %s 1" % (exe, RUN, RERUN),
                "/mnt/c/Program\\ Files/GitHub\\ CLI/%s %s %s 1"
                % (exe, RUN, RERUN),
                "/c/Program\\ Files/GitHub\\ CLI/%s %s %s 1"
                % (exe, RUN, RERUN),
                "%s %s %s 1" % (exe.upper(), RUN, RERUN),
                "python3 -c \"import subprocess; subprocess.run(['%s', "
                "'%s', '%s', '1'])\"" % (exe, RUN, RERUN),
                "%s api -X PUT \"$P/act" "ions/permissions\"" % exe):
            with self.subTest(act=act):
                self.assertIsNotNone(self.refused(act), act)
                self.assert_refused(act)
        # CONTROL: a longer word that only begins with the name is not it
        self.assert_allowed("echo notes on %s.executor; %s the dry %s"
                            % (GH, RERUN, RUN))


class StatedLimitsTest(_Hook):
    """WHAT THE CURE GIVES UP, asserted so nobody reads the rule as wider
    than it is. Each is a real act that the text alone no longer proves:
    the GitHub head supplied by a runtime value or a wrapper the rung does
    not read, with the anchor not typed past its first letter; or a program
    handed the two words with no gh WORD in the text at all — a wrapper, or
    a name the fold cannot rejoin (a glob, a printf format)."""

    def test_a_head_the_fold_cannot_rejoin_passes(self):  # noqa: VACUOUS_ASSERTION — each allowed spelling stands beside its refused twin with the head typed whole, and beside the cancel row in the same spelling, both asserted first
        for twin, passes in (
                ("/usr/bin/%s %s %s 1" % (GH, RUN, RERUN),
                 "/usr/bin/g? %s %s 1" % (RUN, RERUN)),
                ("printf '%%s %s %s 1' %s | sh" % (RUN, RERUN, GH),
                 "printf '%%sh %s %s 1' g | sh" % (RUN, RERUN)),
                ("./%s-retry %s %s $V" % (GH, RUN, RERUN),
                 "./%s-retry %s %s 1" % (GH, RUN, RERUN))):
            with self.subTest(passes=passes):
                self.assertIsNotNone(self.refused(twin), twin)
                # the rows that name no head refuse the same spelling
                self.assertIsNotNone(self.refused(
                    passes.replace(RERUN, "cancel")), passes)
                self.assertIsNone(self.refused(passes), passes)

    def test_the_double_hole_and_the_wrapper_pass(self):  # noqa: VACUOUS_ASSERTION — each allowed spelling stands beside its refused twin with one more letter typed, asserted first
        for twin, passes in (
                ("%s $W re$(echo %s) 1" % (GH, RUN),
                 "%s $W r$(echo e%s) 1" % (GH, RUN)),
                # a variable NAMED for gh folds to `$gh`, which holds the
                # head, so the limit is a value the text never names
                ("$B %s re$(echo %s) 1" % (RUN, RUN),
                 "$B %s r$(echo e%s) 1" % (RUN, RUN)),
                ("./wrap %s %s $V" % (RUN, RERUN),
                 "./wrap %s %s 1" % (RUN, RERUN)),
                # a value that supplies the whole composite-action path
                ("mkdir -p .$X/act" "ions", "mkdir -p .$X")):
            with self.subTest(passes=passes):
                self.assertIsNotNone(self.refused(twin), twin)
                self.assertIsNone(self.refused(passes), passes)


if __name__ == "__main__":
    unittest.main()
