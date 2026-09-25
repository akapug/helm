#!/usr/bin/env python3
"""helm chat argv-guard — the shell-substitution gate, born from a git clean.

Composing a chat/dispatch body as a double-quoted shell argument lets the
SHELL execute any backticked or $(...) content and splice its stdout into the
message before helm ever sees argv. Three live incidents in two days: a gate
request truncated at its load-bearing token, a fleet post mangled, and a
backticked `git clean` that RAN in the shared checkout from inside the very
message warning about it — refused only by clean.requireForce. The advisory
warning failed every time, including on its own author. Enforce, don't advise.

Helm cannot guard this at receive time — the substitution consumes the
backticks before argv exists. The ONE place they are still visible is the
Bash tool's command string, so the guard is a PreToolUse gate (gate=True,
the stop-guard's propagation law: rc 2 reaches the harness, everything else
fails open).

A fourth incident moved the same hazard one surface out: a
`git commit -m` message lost its backticked phrase to the shell and LANDED
mangled. That class inverts the polarity — the body is a flag VALUE, not
positional — and gets its own test class below (MsgFlagClassTest).
"""
import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-argvguard-", var="HELM_HOME")

# hooks import lazily below — they transitively import helm.configs, whose
# roots freeze at first import
from helm import chat  # noqa: E402
from helm.verdicts import POLARITIES  # noqa: E402


class GuardMatrixTest(unittest.TestCase):
    """The block/pass matrix — every row is a real incident or a real usage
    pattern measured from this fleet's own command history."""

    def blocks(self, cmd):
        return chat.argv_guard(cmd) is not None

    def test_the_three_incidents_all_block(self):
        # the weekend truncation: `reset` executed, stdout spliced in
        self.assertTrue(self.blocks(
            'helm chat post "grepping surfaced the line `reset`" --room x'))
        # the git clean that ran in the shared checkout
        self.assertTrue(self.blocks(
            'helm chat post "do NOT run `git clean` in this tree"'))
        # the dispatch-body variant that ate a gate request
        self.assertTrue(self.blocks(
            'helm dispatch send kimi lane "gate at `git cherry main`" --ref a'))

    def test_dollar_paren_in_a_double_quoted_body_blocks(self):
        self.assertTrue(self.blocks('helm chat post "count: $(ls | wc -l)"'))

    def test_every_messaging_verb_is_covered(self):
        for verb in ('chat post "x `y`"', 'chat reply 3 "x `y`"',
                     'chat dm codex "x `y`"',
                     'chat standup say r --marker YIELD "x `y`"',
                     'chat meld say r --marker DONE "x `y`"',
                     'chat council say r "x `y`"',
                     'dispatch send s l "x `y`" --ref a'):
            self.assertTrue(self.blocks("helm " + verb), verb)

    def test_the_task_ledger_is_prose_too_positionally_and_by_flag(self):  # noqa: VACUOUS_ASSERTION — every finite tuple arm executes, followed by unconditional controls
        """MEASURED 2026-09-09, and the second half is the one that bit.

        `helm task add ... --note "...`fab edit <script>`... `deploy-...sh`"`
        had BOTH backticked phrases executed by the shell and stored as gaps;
        the row read as complete prose afterwards and nothing errored where
        anyone would look. A task row outlives most chat, so the durability
        argument is stronger here than on the surfaces already covered.

        The FLAG half needs its own arms because the positional rule
        deliberately EXEMPTS flag values — that exemption is why --owner and
        --room stay computable — and the mangled note arrived through --note.
        I first fixed this with a separate table entry and the probe caught
        that it never fires: the earliest verb owns the segment, so the helm
        positional grammar claims `helm task ...` and a later task grammar is
        unreachable. The prose flags are an exception INSIDE the positional
        rule for that reason."""
        for cmd in ('task add "x `y`" --owner s',
                    'task comment 12 "x `y`"',
                    'task add t --owner s --note "x `y`"',
                    'task update 12 --note "x `y`"',
                    'task comment 12 --note "x `y`"',
                    'task add t --owner s --note "x $(y)"',
                    # The two doors a review found uncovered on
                    # the first cut: a close REASON is positional prose, and
                    # --posture-na carries a REASON through a flag.
                    'task close 12 "x `y`"',
                    'task close 12 "done: $(y)"',
                    'task add t --owner s --posture-na "x `y`"',
                    'task update 12 --posture-na "x $(y)"',
                    # Boolean flags consume no value: the quote following one
                    # still opens the title and must stay protected.
                    'task add --mine "x `y`"',
                    'task add --owner-asked "x $(y)"',
                    # Rejected attached values still substitute before task
                    # parsing, so boolean flags never open an exemption.
                    'task add --mine="x $(y)"',
                    'task add --owner-asked="x `y`"'):
            self.assertTrue(self.blocks("helm " + cmd), cmd)
        # CONTROL ON THE SAME SURFACE: the exemption the flag rule punches
        # through must still hold for every OTHER flag, or this cure has
        # quietly made ordinary scripting unstateable.
        self.assertFalse(self.blocks('helm task add t --owner "$SEAT"'),
                         "an ordinary computed flag value is correct shell")
        self.assertFalse(self.blocks('helm task show "$id"'),
                         "a READ verb takes computed arguments")
        for cmd in ('task close "$(id)" "clean reason"',
                    'task comment "$(id)" "clean text"',
                    'task update "$(id)" --owner s'):
            self.assertFalse(self.blocks("helm " + cmd),
                             "the first task identity operand is computable")
        self.assertTrue(self.blocks(
            'helm task close "$(id)" "reason `whoami`"'),
            "computed identity must not exempt the following prose")
        self.assertFalse(self.blocks("helm task add t --owner s --note 'x `y`'"),
                         "single quotes substitute nothing")

    def test_single_quotes_are_safe_and_pass(self):
        """No substitution happens in single quotes — prose ABOUT commands
        must stay sendable (the never-scan law's spirit)."""
        self.assertFalse(self.blocks(
            "helm chat post 'prose about `git clean` stays literal'"))

    def test_the_quoted_heredoc_is_the_blessed_route_and_passes(self):
        self.assertFalse(self.blocks(
            "helm chat post --room helm <<'EOF'\n"
            "body with `anything` and $(everything)\nEOF"))

    def test_an_UNQUOTED_heredoc_still_blocks_because_it_still_substitutes(self):
        self.assertTrue(self.blocks(
            "helm chat post --room helm <<EOF\nbody with `cmd`\nEOF"))

    def test_substitution_BEFORE_the_verb_is_normal_scripting(self):
        self.assertFalse(self.blocks(
            "TIP=$(git rev-parse HEAD) && helm dispatch add k l --ref $TIP"))

    def test_unquoted_dollar_paren_AFTER_the_verb_is_a_computed_argument(self):
        """--ref $(git rev-parse HEAD) substitutes into an argument the
        author MEANT to compute — blocking it would break half the fleet's
        real dispatch calls."""
        self.assertFalse(self.blocks(
            'helm dispatch send k l "clean body" --ref $(git rev-parse HEAD)'))

    def test_non_messaging_commands_never_match(self):
        self.assertFalse(self.blocks("git log `whoami`"))
        self.assertFalse(self.blocks("helm chat read --room helm"))
        self.assertFalse(self.blocks("helm chat catchup --apply"))

    def test_a_separator_ends_the_scan_region(self):
        """A later && segment's substitution is someone else's business.

        The probe puts $( inside a DOUBLE-QUOTED region after the separator
        — the first version used an unquoted one, which passes with or
        without the region boundary, so removing the boundary killed
        nothing. A probe that cannot say otherwise is not a measurement."""
        self.assertFalse(self.blocks(
            'helm chat post "clean body" && echo "note $(date)"'))
        self.assertFalse(self.blocks(
            'helm chat post "clean body" && TIP=$(git rev-parse HEAD)'))

    def test_a_QUOTED_substitution_in_a_flag_VALUE_is_correct_shell(self):
        """Measured on the integrator minutes after the land:
        --ref "$(git rev-parse HEAD)" is the CORRECT habit — unquoted, the
        value word-splits on whitespace. A guard that blocks it teaches
        people to drop the quotes, the same shape as the bug it prevents.
        The hazard is the BODY (positional); flag values are computed by
        intent. Bare `--` is the end-of-flags marker, so what follows it
        is positional again and stays guarded."""
        self.assertFalse(self.blocks(
            'helm dispatch send ds4pro xrev-proxy-config '
            '--ref "$(git rev-parse HEAD)" --kind review'))
        self.assertFalse(self.blocks(
            'helm dispatch send k l "clean body" --ref="$(git rev-parse HEAD)"'))
        # the exemption is the VALUE slot only — a hazardous BODY beside a
        # clean flag value still blocks, in either argument order
        self.assertTrue(self.blocks(
            'helm dispatch send k l "gate at `git cherry`" '
            '--ref "$(git rev-parse HEAD)"'))
        self.assertTrue(self.blocks(
            'helm chat post --room helm -- "body `x`"'))

    def test_a_body_after_a_SELF_VALUED_flag_is_positional(self):
        """Found live post-land: --room=helm already carries its
        value, so the NEXT quoted region is the BODY — but the lookback saw
        a --word and exempted it, letting the backticks execute. The pair:
        the attached-value form (--ref="$(...)"), where the quote opens the
        value itself, must keep passing — that half is pinned by
        test_a_QUOTED_substitution_in_a_flag_VALUE_is_correct_shell."""
        self.assertTrue(self.blocks(
            'helm chat post --room=helm "body `cmd`"'))
        self.assertTrue(self.blocks(
            'helm dispatch send k l --kind=review "gate at $(git cherry)"'))

    def test_a_quoted_heredoc_BODY_containing_the_verb_is_data(self):
        """Found live, post-land: writing a hook-payload fixture — a file
        ABOUT a blocked command — tripped the guard three times, because
        the verb search ran over the raw string and matched inside the
        heredoc body. A quoted-tag body never substitutes; it is data.
        The discriminating pair: the SAME command with an unquoted tag
        still blocks, because there the body's backticks genuinely
        execute when the shell feeds the heredoc."""
        incident = ("cat > argv-probe.json <<'JSON'\n"
                    '{"tool_input":{"command":"helm chat post \\"probe `true`\\""}}\n'
                    "JSON\n"
                    "helm chat argv-guard --hook-json < argv-probe.json")
        self.assertFalse(self.blocks(incident))
        self.assertTrue(self.blocks(incident.replace("<<'JSON'", "<<JSON")))

    def test_the_strictly_weaker_pattern_blocks_and_the_stdin_route_passes(self):
        """Premise payload-mangling-is-a-grammar-gap-not-a-boundary-guard:
        `"$(cat <<'EOF'…)"` protects the heredoc body but still passes an
        argv word — the pattern a corrected author ADOPTED believing it the
        fix. The guard must catch it (the $( sits in a double-quoted region)
        so the block teaches the genuinely safe route. Its UNQUOTED cousin
        passes: word-splitting can mangle but nothing in the body executes,
        and blocking unquoted $() breaks every computed argument."""
        self.assertTrue(self.blocks(
            "helm chat post --room helm \"$(cat <<'EOF'\nbody `x`\nEOF\n)\""))
        self.assertFalse(self.blocks(
            "helm chat post --room helm $(cat <<'EOF'\nbody\nEOF\n)"))

    def test_total_on_garbage(self):
        for cmd in (None, "", "helm chat post", "helm chat post \"unclosed",
                    "helm chat post \\", "x" * 10000):
            chat.argv_guard(cmd)        # named by not raising


class MsgFlagClassTest(unittest.TestCase):
    """The git commit/tag class — the same hazard one surface further out,
    with the POLARITY INVERTED: for messaging verbs the body is positional
    and flag values are exempt; for git commit the body IS a flag value
    (-m/--message/-am), the exact region the flag-value exemption skips.
    Adding git to _BODY_VERBS would exempt precisely the hazard — hence a
    second verb class whose region law inverts."""

    def blocks(self, cmd):
        return chat.argv_guard(cmd) is not None

    def test_the_landed_mangled_commit_incident_blocks(self):
        # the incident: the shell executed `recip in live` before git saw
        # argv, and the LANDED message read "the assignment test is , and"
        self.assertTrue(self.blocks(
            'git commit -m "board: the assignment test is `recip in live`, '
            'and live means the pane exists"'))

    def test_dollar_paren_in_a_double_quoted_m_value_blocks(self):
        self.assertTrue(self.blocks('git commit -m "landed $(git log -1)"'))
        # the attached long form opens the value at the quote (the
        # equals-law, inherited)
        self.assertTrue(self.blocks(
            'git commit --message="landed $(git log -1)"'))

    def test_the_m_flag_family_is_covered(self):
        for cmd in ('git commit -am "fix `date`"',
                    'git commit --message "fix `date`"',
                    'git commit --amend -m "fix `date`"',
                    'git tag -a v1 -m "notes `x`"',
                    'git -C /tmp/wt commit -m "fix `date`"'):
            self.assertTrue(self.blocks(cmd), cmd)

    def test_single_quoted_m_values_pass_no_substitution_happens(self):
        """The discriminating half of the incident pair: the SAME message
        single-quoted is safe shell, and prose about commands must stay
        commitable (the messaging class's single-quote law, same spirit)."""
        self.assertFalse(self.blocks(
            "git commit -m 'board: the assignment test is `recip in live`'"))

    def test_the_file_route_is_the_cure_and_passes(self):
        self.assertFalse(self.blocks("git commit -F /tmp/msg.txt"))
        self.assertFalse(self.blocks(
            "git commit -F - <<'EOF'\nbody with `anything`\nEOF"))

    def test_multiple_m_flags_are_each_checked(self):
        self.assertTrue(self.blocks(
            'git commit -m "clean subject" -m "body cites `git cherry`"'))
        self.assertFalse(self.blocks(
            'git commit -m "clean subject" -m "clean body"'))

    def test_non_message_slots_keep_normal_rules(self):
        """Only the MESSAGE flag's value is the body. Other double-quoted
        slots are computed by intent (--author "$(git config user.name)"),
        and unquoted substitutions are ordinary computed arguments — the
        messaging class's bare-backtick rule does NOT apply here."""
        self.assertFalse(self.blocks(
            'git commit -m \'clean\' --author "$(git config user.name) <x@y>"'))
        self.assertFalse(self.blocks("git tag v$(cat VERSION) -m 'release'"))
        self.assertFalse(self.blocks("git tag v`cat VERSION` -m 'release'"))

    def test_plain_git_commands_pass(self):
        self.assertFalse(self.blocks("git commit"))
        self.assertFalse(self.blocks("git commit -a --amend --no-edit"))
        self.assertFalse(self.blocks('git commit -m "clean message"'))
        self.assertFalse(self.blocks("git tag v1.2"))

    def test_the_messaging_class_is_untouched_by_the_second_class(self):
        """The r4 exemption keeps exempting messaging-verb flag VALUES —
        the inversion lives in the git class only."""
        self.assertFalse(self.blocks(
            'helm dispatch send ds4pro lane '
            '--ref "$(git rev-parse HEAD)" --kind review'))

    def test_a_messaging_verb_claims_the_command_so_prose_stays_sendable(self):
        """A chat post ABOUT this very incident quotes the hazardous
        command inside a single-quoted body — the git class must not
        reach into it (the messaging match wins, and its single-quote
        law already cleared the prose). The cost: a compound command's
        git leg goes unscanned — a MISS, the allowed fail direction."""
        self.assertFalse(self.blocks(
            "helm chat post 'the agent ran git commit -m "
            '"x `recip`" and the shell ate it\''))

    def test_total_on_git_shaped_garbage(self):
        for cmd in ("git commit -m", "git commit -m \"unclosed",
                    "git -C", "git commit \\", 'git commit -m"',
                    "git commit " + "x" * 10000):
            chat.argv_guard(cmd)        # named by not raising


class VerdictFlagClassTest(unittest.TestCase):
    """Dispatch verdict evidence is protected polarity-flag prose."""

    def blocks(self, cmd):
        return chat.argv_guard(cmd) is not None

    def test_the_immutable_mangled_verdict_incident_blocks(self):
        self.assertTrue(self.blocks(
            'helm dispatch verdict abcdef12 ' + 'a' * 40 +
            ' --fix "probe prescribes `helm seat resume`; unsafe"'))

    def test_every_polarity_is_covered(self):
        tails = ('--approve "gate:abc $(git log -1)"',
                 '--supersede "replaced by $(git rev-parse HEAD)"')
        for tail in tails:
            with self.subTest(tail=tail):
                cmd = 'helm dispatch verdict abcdef12 %s %s' % ('a' * 40, tail)
                self.assertTrue(self.blocks(cmd), cmd)

    def test_evidence_follows_the_parser_not_flag_adjacency(self):
        sha = 'a' * 40
        commands = (
            'helm dispatch verdict abcdef12 %s '
            '"evidence $(printf PWNED)" --fix' % sha,
            'helm dispatch verdict abcdef12 %s --fix '
            '"safe prefix" "hazard $(printf PWNED)"' % sha,
        )
        for cmd in commands:
            with self.subTest(command=cmd):
                self.assertTrue(self.blocks(cmd))

    def test_rejected_attached_flag_still_blocks_before_parser_refusal(self):
        cmd = ('helm dispatch verdict abcdef12 %s '
               '--fix="finding `helm seat resume`"' % ('a' * 40))
        self.assertTrue(self.blocks(cmd))
        from helm import dispatches
        with contextlib.redirect_stderr(io.StringIO()), \
             mock.patch.object(dispatches, "mark_verdict") as mark:
            self.assertEqual(dispatches.cmd_dispatch([
                "verdict", "abcdef12", "a" * 40,
                "--fix=finding `helm seat resume`"]), 2)
        mark.assert_not_called()

    def test_single_quoted_evidence_passes_without_substitution(self):
        self.assertFalse(self.blocks(
            "helm dispatch verdict abcdef12 %s --fix "
            "'finding cites `helm seat resume`'" % ('a' * 40)))

    def test_non_evidence_slots_keep_normal_computed_argument_rules(self):
        self.assertFalse(self.blocks(
            'helm dispatch verdict abcdef12 "$(git rev-parse HEAD)" '
            '--fix "plain evidence"'))
        self.assertFalse(self.blocks(
            'helm dispatch verdict abcdef12 `git rev-parse HEAD` '
            '--fix "plain evidence"'))

    def test_earlier_verdict_verb_owns_evidence_that_mentions_git_commit(self):
        self.assertTrue(self.blocks(
            'helm dispatch verdict abcdef12 %s --fix '
            '"review cites git commit -m $(date)"' % ('a' * 40)))

    def test_each_compound_command_leg_is_guarded(self):
        self.assertTrue(self.blocks(
            "helm chat post 'review complete' && "
            'helm dispatch verdict abcdef12 %s --fix '
            '"evidence $(printf PWNED)"' % ('a' * 40)))

    def test_single_quoted_command_example_is_data(self):
        self.assertFalse(self.blocks(
            "printf '%%s\\n' 'helm dispatch verdict abcdef12 %s --fix "
            '"evidence $(date)"\'' % ('a' * 40)))

    def test_clean_verdicts_pass(self):
        for polarity in POLARITIES:
            with self.subTest(polarity=polarity):
                self.assertFalse(self.blocks(
                    'helm dispatch verdict abcdef12 %s --%s "plain evidence"'
                    % ('a' * 40, polarity)))

    def test_a_chat_body_about_verdicts_stays_sendable(self):
        self.assertFalse(self.blocks(
            "helm chat post 'the command helm dispatch verdict x y --fix "
            '"bad `cmd`" was mangled\''))


class UnquotedHeredocClassTest(unittest.TestCase):
    """A bare heredoc tag leaves its body substitution-active."""

    def blocks(self, cmd):
        return chat.argv_guard(cmd) is not None

    def test_verdict_unquoted_heredoc_blocks_both_substitution_forms(self):
        prefix = 'helm dispatch verdict abcdef12 %s --fix <<BODY\n' % ('a' * 40)
        for body in ('finding `printf PWNED`', 'finding $(printf PWNED)'):
            with self.subTest(body=body):
                self.assertTrue(self.blocks(prefix + body + '\nBODY'))

    def test_quoted_delimiter_is_the_literal_stdin_route(self):
        self.assertFalse(self.blocks(
            "helm dispatch verdict abcdef12 %s --fix <<'BODY'\n"
            "finding `printf PWNED` and $(printf PWNED)\nBODY" % ('a' * 40)))

    def test_commit_file_stdin_requires_a_quoted_delimiter_too(self):
        self.assertTrue(self.blocks(
            "git commit -F - <<BODY\nmessage cites `git cherry`\nBODY"))
        self.assertFalse(self.blocks(
            "git commit -F - <<'BODY'\nmessage cites `git cherry`\nBODY"))

    def test_control_operators_inside_the_body_do_not_split_away_the_hazard(self):
        self.assertTrue(self.blocks(
            "helm dispatch verdict abcdef12 %s --fix <<BODY\n"
            "pipeline: false || printf x; value=$(printf PWNED)\nBODY"
            % ('a' * 40)))

    def test_backslash_continued_opener_keeps_its_heredoc_ownership(self):
        self.assertTrue(self.blocks(
            "helm dispatch verdict abcdef12 %s \\\n"
            "  --fix <<BODY\nfinding $(printf PWNED)\nBODY" % ('a' * 40)))

    def test_trailing_space_does_not_fake_a_heredoc_terminator(self):
        self.assertTrue(self.blocks(
            "helm dispatch verdict abcdef12 %s --fix <<BODY\n"
            "BODY \nstill body: $(printf PWNED)\nBODY" % ('a' * 40)))

    def test_escaped_substitution_operators_stay_literal(self):
        self.assertFalse(self.blocks(
            "helm dispatch verdict abcdef12 %s --fix <<BODY\n"
            "literal \\`cmd\\` and \\$(cmd)\nBODY" % ('a' * 40)))

    def test_unrelated_heredoc_scripting_remains_out_of_scope(self):
        self.assertFalse(self.blocks(
            "cat <<BODY\nordinary computed value: $(date)\nBODY"))

    def test_dispatch_help_requires_a_quoted_delimiter(self):
        from helm import cli, dispatches
        self.assertIn("QUOTED-delimiter heredoc", dispatches.USAGE)
        self.assertIn("UNQUOTED heredocs", cli._VERB_HELP["dispatch"])


class HookPlumbingTest(unittest.TestCase):
    def run_hook(self, payload):
        out, err = io.StringIO(), io.StringIO()
        stdin = sys.stdin
        sys.stdin = io.StringIO(json.dumps(payload) if isinstance(payload, dict)
                                else payload)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = chat.cmd_argv_guard([])
        finally:
            sys.stdin = stdin
        return rc, out.getvalue() + err.getvalue()

    def test_a_matching_bash_call_blocks_with_the_cure(self):
        rc, out = self.run_hook({"tool_name": "Bash", "tool_input": {
            "command": 'helm chat post "never run `git clean`"'}})
        self.assertEqual(rc, 2)
        self.assertIn("<<'EOF'", out)          # points at the safe route
        self.assertIn("runs on your box", out)  # says why it matters

    def test_a_DENY_rung_speaks_before_this_cure_and_that_is_a_COST(self):
        """THE RUNG ORDER IS A COST AND IT IS ASSERTED, not discovered. The
        Actions rung runs before this gate, so a message body that holds a
        protected spelling AND a backtick gets the Actions refusal and not
        the heredoc cure this gate exists to hand out. The operator's route
        out is the word at the front of that command, which the refusal
        names.

        THE POPULATION OF THAT COST IS THE SCOPED RULE'S, and it moved. While
        that rule fired on ANY piece, the arm below — `never run `git
        clean``, the incident this whole gate was built for — was one of
        them, because `run` is a piece; a refusal naming the wrong cure is
        worse than none, and narrowing the rule to the rows' ANCHORS gave
        that witness its own cure back, because `run` is an ordinary English
        word and no row's anchor. What still lands here is a body that names
        the ACT: a row, or an anchor beside a hole."""
        rc, out = self.run_hook({"tool_name": "Bash", "tool_input": {
            "command": 'helm chat post "the .github/workflows dir is '
                       '`local`"'}})
        self.assertEqual(rc, 2)
        self.assertIn("is the Actions workflow directory", out)
        self.assertNotIn("<<'EOF'", out)
        # …and the incident body this gate was built for keeps ITS cure
        rc, out = self.run_hook({"tool_name": "Bash", "tool_input": {
            "command": 'helm chat post "never run `git clean`"'}})
        self.assertEqual(rc, 2)
        self.assertIn("<<'EOF'", out)

    def test_a_hazardous_commit_blocks_with_the_FILE_cure(self):
        """Each class's block names the safe route for ITS surface: a
        quoted-delimiter stdin heredoc cures a chat post, not a commit — the
        commit cure is the -F file route (or Write producing the file)."""
        rc, out = self.run_hook({"tool_name": "Bash", "tool_input": {
            "command": 'git commit -m "the test is `recip in live`, and"'}})
        self.assertEqual(rc, 2)
        self.assertIn("git commit -F", out)     # the file route, named
        self.assertIn("runs on your box", out)
        self.assertNotIn("helm chat post", out)  # not the chat cure

    def test_a_hazardous_verdict_blocks_with_its_value_cure(self):
        rc, out = self.run_hook({"tool_name": "Bash", "tool_input": {
            "command": 'helm dispatch verdict abcdef12 %s --fix '
                       '"finding `helm seat resume`"' % ('a' * 40)}})
        self.assertEqual(rc, 2)
        self.assertIn("evidence would be mangled", out)
        self.assertIn("evidence=$(cat <<'EOF'", out)
        self.assertIn('--fix "$evidence"', out)
        self.assertNotIn("git commit -F", out)

    def test_a_clean_bash_call_passes(self):
        rc, _ = self.run_hook({"tool_name": "Bash", "tool_input": {
            "command": "helm chat post --room helm <<'EOF'\nsafe\nEOF"}})
        self.assertEqual(rc, 0)

    def test_non_bash_tools_pass_untouched(self):
        """The substitution scan is Bash-only; an Edit whose payload happens
        to carry a command key takes the GitHub-Actions path-only rung and
        nothing else (its file_path is not a workflow file)."""
        rc, _ = self.run_hook({"tool_name": "Edit", "tool_input": {
            "command": 'helm chat post "`x`"', "file_path": "x.py"}})
        self.assertEqual(rc, 0)
        rc, _ = self.run_hook({"tool_name": "Read", "tool_input": {
            "command": 'helm chat post "`x`"'}})
        self.assertEqual(rc, 0)

    def test_fails_OPEN_on_garbage_payloads(self):
        """It runs in front of EVERY Bash call; a broken guard that blocks
        everything wedges the entire fleet — the stop-guard's law."""
        for payload in ("not json", "{}", '{"tool_name":"Bash"}', ""):
            rc, _ = self.run_hook(payload)
            self.assertEqual(rc, 0, repr(payload))



class DurableProseVerbsTest(unittest.TestCase):
    """THE STORE IS WHERE A LESSON GOES TO SURVIVE COMPACTION, AND IT WAS THE
    ONE PROSE SURFACE THE GUARD DID NOT COVER.

    MEASURED: `helm store add premise "... NOT a top-level
    `terminals` key ..."` stored as "NOT a top-level key". The backticks ran
    as command substitution inside the double-quoted argv, bash printed
    "terminals: command not found", and the verb EXITED 0 with the word
    deleted. `chat post` and `dispatch send` block that exact shape — the
    store verbs were simply never added to the grammar table.

    A body corrupted on the way in is worse here than on a chat line: a chat
    reader sees the mangling in the room, while a premise that lost a word
    gives every future reader no signal that anything is missing."""

    def _refusal(self, cmd):
        """The guard's own (cure, reason) — asserted as TEXT, never boolean.

        A boolean says only that something refused; the text says WHICH hazard
        and WHICH cure, so a guard that blocks for the wrong reason fails
        here instead of passing green."""
        return chat._argv_guard_segment(cmd)

    def test_the_store_body_that_lost_a_word_is_now_refused(self):
        cure, reason = self._refusal('helm store add premise "a `whoami` body"')
        self.assertIn("backtick", reason)
        self.assertIn("EXECUTES", reason)
        self.assertTrue(cure, "a refusal must name the cure, not just refuse")
        _cure2, reason2 = self._refusal('helm store add premise "$(id) body"')
        self.assertIn("$(", reason2)

    def test_premise_and_coach_write_prose_too(self):
        _c, r1 = self._refusal('helm premise x "a `date` statement"')
        self.assertIn("backtick", r1)
        _c2, r2 = self._refusal('helm coach "a `hostname` lesson"')
        self.assertIn("backtick", r2)

    def test_the_read_verbs_stay_computable(self):
        """A guard that cried wolf on reads would push people back to argv
        bodies for the writes too. `premise\\s` not `premise\\b` keeps
        premise-check — a READ verb — out of the grammar."""
        # POSITIVE CONTROL, unconditional, SAME predicate and same shape: the
        # WRITE form refuses with a real reason, so the Nones below are the
        # verb boundary and not a guard that stopped matching helm entirely.
        _c, reason = self._refusal('helm premise x "a `date` statement"')
        self.assertIn("backtick", reason)
        self.assertIsNone(self._refusal('helm premise-check some-id'))
        self.assertIsNone(self._refusal('helm store resolve "why did `x` fail"'))
        self.assertIsNone(self._refusal('helm store list'))

    def test_variable_expansion_still_passes(self):
        """The cure we tell everyone to use — read -r -d '' BODY <<'EOF' then
        pass "$BODY" — must not itself be blocked, or the guidance is
        unfollowable."""
        _c, reason = self._refusal('helm store add premise "`x`"')
        self.assertIn("backtick", reason)
        self.assertIsNone(self._refusal('helm store add premise "$BODY"'))

    def test_a_revised_statement_is_prose_too(self):
        """`store revise <id> "<statement>"` rewrites a canon statement, and a
        census of recorded commands found one whose double-quoted statement
        held a backticked `git stash pop`: the shell ran it in the shared
        checkout before helm saw the text. The id stays computable."""
        _c, reason = self._refusal(
            'helm store revise stash-reach "the `git stash pop` idiom"')
        self.assertIn("backtick", reason)
        _c, reason = self._refusal('helm store revise x "a $(id) statement"')
        self.assertIn("$(", reason)
        self.assertIsNone(self._refusal('helm store revise x "$STATEMENT"'))
        self.assertIsNone(self._refusal('helm store revise $(cat id) "text"'))

class SpecWiringTest(unittest.TestCase):
    def spec(self):
        from helm import hooks
        return next(s for s in hooks.SPECS if s["name"] == "argv-guard")

    def test_it_is_a_GATE_on_PreToolUse_matching_Bash_Monitor_Write_Edit_and_Agent(self):
        """Monitor joined Bash for task/2542: a subagent arms the seat's
        beacon with a Monitor call, and a Bash-only matcher never ran the
        guard for one. Write and Edit joined for task/2566: a workflow file
        lands through the Write tool, which a Bash|Monitor group never
        showed to any helm hook. Agent joined for the agent-model rung: a
        group that does not name it never shows an Agent call to the guard,
        so the refusal would exist in code and fire nowhere."""
        s = self.spec()
        self.assertEqual(s["event"], "PreToolUse")
        self.assertEqual(s["matcher"], "Bash|Monitor|Write|Edit|Agent")
        self.assertIn("Agent", s["matcher"].split("|"))
        self.assertTrue(s.get("gate"),
                        "without gate=True the rc-2 block is swallowed by "
                        "|| true — the exact bug that disarmed the stop-guard "
                        "for its entire life")

    def test_the_generated_command_propagates_rc_2(self):
        from helm import hooks
        cmd = hooks.spec_command(self.spec())
        self.assertNotIn("|| true", cmd)

    def test_seats_get_it_too(self):
        """The incidents were SEAT posts — a guard only on the home config
        would miss every one of them."""
        from helm import hooks
        self.assertIn("argv-guard",
                      [s["name"] for s in hooks.SEAT_SPECS])

    def test_it_stays_unscoped_so_the_agent_model_rung_holds_fleet_wide(self):  # noqa: VACUOUS_ASSERTION — the scoped tuple is first asserted to hold stop-guard's args, so argv-guard's absence from it is the spec's own and not an empty tuple
        """The agent-model fact holds on every seat in every project, so the
        spec carries no helm scope: a scoped hook skips itself outside helm's
        own checkouts, which would leave the flag refused here and ignored in
        silence everywhere else."""
        from helm import hooks
        s = self.spec()
        scoped = hooks.fleet_scoped_args()
        self.assertIn("chat stop-guard --hook-json", scoped)   # control
        self.assertIn("Agent", s["matcher"].split("|"))
        self.assertIsNone(s.get("scope"))
        self.assertNotIn(s["args"], scoped)


class AgentModelRungTest(unittest.TestCase):
    """The agent-model rung: an Agent call that names a model is refused.

    The owner's ruling: the Agent tool's `model` argument never changes the
    model a subagent runs on; the subagent runs as the launching seat's model
    whatever the flag says. A different model is reached only through the
    Workflow tool's per-agent model or a seat launched on that model, and the
    refusal names those doors after the first one, which is to drop the flag.

    Every arm drives the shipped hook entry (`cmd_argv_guard` reading a real
    PreToolUse payload on stdin), never a copy of its predicate.
    """

    MODELS = ("haiku", "opus", "fable", "sonnet")
    DOORS = ("Drop the flag", "the Workflow tool, agent(prompt, {model})",
             "`helm launch --seat S --model M`")

    def guard(self, tool_input, tool="Agent", agent_id=None, raw=None):
        """(rc, stdout, stderr) of the installed PreToolUse hook verb."""
        payload = {"hook_event_name": "PreToolUse", "tool_name": tool,
                   "session_id": "sess-agent-model", "cwd": "/tmp/repo",
                   "tool_use_id": "toolu_agent_model",
                   "tool_input": tool_input}
        if agent_id is not None:
            payload["agent_id"] = agent_id
            payload["agent_type"] = "general-purpose"
        out, err = io.StringIO(), io.StringIO()
        body = raw if raw is not None else json.dumps(payload)
        with mock.patch.object(sys, "stdin", io.StringIO(body)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = chat.cmd_argv_guard([])
        return rc, out.getvalue(), err.getvalue()

    def agent_input(self, **extra):
        return dict({"description": "a delegated slice",
                     "prompt": "Read the module and report.",
                     "subagent_type": "general-purpose"}, **extra)

    def test_every_model_name_is_refused_and_the_doors_come_in_order(self):  # noqa: VACUOUS_ASSERTION — every tuple arm asserts rc 2 and positive substrings on the refusal it produced
        for model in self.MODELS:
            with self.subTest(model=model):
                rc, out, err = self.guard(self.agent_input(model=model))
                self.assertEqual(rc, 2, err)
                self.assertEqual(out, "", "a refusal speaks on stderr only")
                self.assertIn("[helm argv-guard] BLOCKED", err)
                self.assertIn("model='%s'" % model, err)
                self.assertIn("run as the launching seat's model whatever "
                              "the flag says", err)
                at = [err.find(door) for door in self.DOORS]
                self.assertNotIn(-1, at, "a door is missing: %r" % err)
                self.assertEqual(at, sorted(at),
                                 "the doors are out of order: %r" % err)
                self.assertIn("helm store get "
                              "heuristic:fable-via-workflow-not-agent-tool",
                              err)

    def test_a_subagent_naming_a_model_is_refused_too(self):
        """Every caller, every seat: a subagent's own Agent call inherits
        the same seat model, so its flag is ignored in the same silence."""
        rc, _out, err = self.guard(self.agent_input(model="opus"),
                                   agent_id="a1b2")
        self.assertEqual(rc, 2, err)
        self.assertIn("model='opus'", err)

    def test_no_model_an_empty_model_and_a_null_model_are_admitted(self):
        # POSITIVE CONTROL on the same door and the same input shape: a named
        # model is refused, so the admissions below are the rung declining
        # and not a guard that never ran.
        rc, _out, err = self.guard(self.agent_input(model="opus"))
        self.assertEqual(rc, 2, err)
        for label, tool_input in (("no key", self.agent_input()),
                                  ("empty", self.agent_input(model="")),
                                  ("null", self.agent_input(model=None))):
            with self.subTest(case=label):
                rc, out, err = self.guard(tool_input)
                self.assertEqual((rc, out, err), (0, "", ""),
                                 "an Agent call without a model is admitted "
                                 "unchanged and silently")

    def test_an_agent_prompt_is_never_read_as_a_shell_command(self):
        """The Agent rung stands alone: a prompt is prose for a subagent, so
        text that every Bash rung refuses passes when it is an Agent's
        prompt, from the main thread and from a subagent alike."""
        texts = ('helm chat post "never run `git clean`"',
                 "gh " + "workflow" + " run ci.yml",
                 "helm chat " + "wait --seat s1 " + "--fol" + "low")
        # UNCONDITIONAL CONTROL on the same hook entry: the first text is
        # refused as a Bash command, so the door is live before any pass.
        rc, _o, err = self.guard({"command": texts[0]}, tool="Bash")
        self.assertEqual(rc, 2, err)
        self.assertIn("[helm argv-guard] BLOCKED", err)
        for text in texts:
            with self.subTest(text=text):
                # CONTROL: the same text as a Bash command is refused, by the
                # subagent door too, so the pass below is the Agent rung
                # not reading it, and not text no rung would refuse.
                rc, _o, err = self.guard({"command": text}, tool="Bash",
                                         agent_id="a1b2")
                self.assertEqual(rc, 2, err)
                for agent_id in (None, "a1b2"):
                    rc, out, err = self.guard(
                        self.agent_input(prompt=text, command=text),
                        agent_id=agent_id)
                    self.assertEqual((rc, out, err), (0, "", ""))

    def test_a_model_key_on_another_tool_is_judged_by_that_tools_rungs(self):  # noqa: VACUOUS_ASSERTION — the git-commit arm is an unconditional rc-2 refusal from the same hook entry naming its Bash cure, so each absence is that entry declining on its own input
        """A `model` key means something only on an Agent call. A Bash call
        whose command mentions model=haiku, and whose input carries a model
        key, is judged by the Bash rungs alone: clean, it passes; carrying a
        substitution, it gets the Bash cure and never the agent refusal."""
        rc, out, err = self.guard({"command": "echo model=haiku",
                                   "model": "haiku"}, tool="Bash")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("BLOCKED", out + err)
        rc, _out, err = self.guard(
            {"command": 'git commit -m "model=haiku `date`"',
             "model": "haiku"}, tool="Bash")
        self.assertEqual(rc, 2, err)
        self.assertIn("git commit -F", err)
        self.assertNotIn("Agent called with", err)
        for tool, tool_input in (
                ("Monitor", {"command": "echo model=haiku", "model": "haiku"}),
                ("Write", {"file_path": "notes.txt", "content": "x",
                           "model": "haiku"}),
                ("Edit", {"file_path": "notes.txt", "old_string": "a",
                          "new_string": "b", "model": "haiku"}),
                ("Read", {"file_path": "notes.txt", "model": "haiku"})):
            with self.subTest(tool=tool):
                rc, out, err = self.guard(tool_input, tool=tool)
                self.assertEqual(rc, 0, err)
                self.assertNotIn("Agent called with", out + err)

    def test_malformed_agent_input_fails_open_like_every_other_payload(self):
        """The handler's law for input it cannot read is exit 0, and the
        Agent rung keeps it: a guard in front of a whole tool that blocked on
        garbage would wedge every delegation on the fleet."""
        rc, _out, err = self.guard(self.agent_input(model="opus"))
        self.assertEqual(rc, 2, err)                        # control
        for label, kwargs in (
                ("no tool_input", {"raw": '{"tool_name": "Agent"}'}),
                ("tool_input a list", {"tool_input": ["model", "opus"]}),
                ("tool_input a string", {"tool_input": "model=opus"}),
                ("model a number", {"tool_input": self.agent_input(model=5)}),
                ("model a list", {"tool_input":
                                  self.agent_input(model=["opus"])}),
                ("not json", {"raw": "not json"})):
            with self.subTest(case=label):
                tool_input = kwargs.pop("tool_input", {})
                rc, _out, err = self.guard(tool_input, **kwargs)
                self.assertEqual(rc, 0, err)

    def test_the_echo_is_bounded_whatever_the_value(self):
        """The value is shown so a reader sees what was refused; it is cut
        and every character outside a model name's alphabet is shown as ?,
        so one character in is one character out."""
        long_model = "claude-" + "x" * 200
        rc, _out, err = self.guard(self.agent_input(model=long_model))
        self.assertEqual(rc, 2, err)
        self.assertIn("model='%s'" % long_model[:chat.AGENT_MODEL_ECHO], err)
        self.assertNotIn(long_model[:chat.AGENT_MODEL_ECHO + 1], err)
        rc, _out, err = self.guard(self.agent_input(model="op\x00us‮"))
        self.assertEqual(rc, 2, err)
        self.assertIn("model='op?us?'", err)


class NestedSpawnRungTest(unittest.TestCase):
    """task/2971: the argv-guard is the WRONG seam for the
    nested-spawn reflex, and says nothing at a subagent's Agent call.

    PreToolUse additionalContext reaches the model on its NEXT step, after
    the Agent call it rode has already run, so a steer said here arrives one
    spawn late by construction. The steer rides SubagentStart instead
    (helm/saguide.py, tests/test_saguide.py), which the harness hands to the
    subagent before its first step; the denial of the call itself is
    task/1775's.

    Each arm gets its own home, so the reflex store never leaks between arms
    or into the module's shared home."""

    STEER = "a fixture steer: a bounded subagent does its own work"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-nested-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        prior = {k: _os.environ.get(k) for k in ("HELM_HOME", "HELM_CHAT_DIR")}

        def restore():
            for k, v in prior.items():
                if v is None:
                    _os.environ.pop(k, None)
                else:
                    _os.environ[k] = v
        self.addCleanup(restore)
        _os.environ["HELM_HOME"] = _os.path.join(self.tmp, "helm")
        _os.environ.pop("HELM_CHAT_DIR", None)
        _os.makedirs(chat.chat_dir(), exist_ok=True)

    def plant(self, **extra):
        from helm import reflex
        return reflex.write(dict({"id": "subagent-fanout", "steer": self.STEER,
                                  "signal": "nested-spawn"}, **extra))

    def guard(self, agent_id=None, **tool_input):
        return AgentModelRungTest.guard(
            self, dict({"description": "a delegated slice",
                        "prompt": "Read the module and report.",
                        "subagent_type": "general-purpose"}, **tool_input),
            agent_id=agent_id)

    def test_a_subagents_agent_call_says_nothing(self):
        from helm import reflex
        self.plant()
        # CONTROL: the reflex is live and renders, so the silence below is
        # the seam declining and not an empty store.
        self.assertEqual(reflex.spawn_steers(),
                         [("subagent-fanout", "REFLEX: " + self.STEER)])
        for agent_id in ("afixture01", None):
            with self.subTest(agent_id=agent_id):
                self.assertEqual(self.guard(agent_id=agent_id), (0, "", ""))

    def test_the_model_refusal_still_comes_first(self):
        self.plant()
        rc, out, err = self.guard(agent_id="afixture01", model="opus")
        self.assertEqual(rc, 2, err)
        self.assertEqual(out, "")
        self.assertIn("model='opus'", err)


class GitHubActionsRungTest(unittest.TestCase):
    """task/2566: a command whose TEXT holds a GitHub-Actions spelling is
    refused on a local project unless the command carries
    HELM_ALLOW_GITHUB_ACTIONS=1 at its front.

    THE INCIDENT. A workflow agent ranked turn-on-GitHub-Actions as blocker
    1, and the seat ran `gh api -X PUT repos/<org>/<repo>/actions/permissions
    -F enabled=true`; the standing owner rule lived in a store premise and a
    whisper, so nothing fired at either moment, and the owner caught it
    twenty minutes later by luck.

    WHY THESE ARMS MEASURE PRESENCE AND NOTHING ELSE. Nine spellings were
    measured admitted while this rung parsed, and each was a reader being
    wrong about which word owned a character: an override boundary, a
    backslash or quote inside a token, gh's global options past a fixed gap,
    a heredoc excision that hid an arm, an Actions API spelling the table had
    not learned, a path that reached the directory by traversal, a
    substitution whose interior was quoted, a grant a receiver stage
    inherited. Same-class recurrence changes the design: the rung folds the
    whole command once and refuses on the PRESENCE of a protected spelling
    anywhere in it. So every spelling a parser had to argue about is one
    refusal here, and no arm in this class measures word-splitting.

    THE COST IS ASSERTED, NOT ONLY WRITTEN DOWN. A command that merely
    mentions a spelling is refused too, and the arms below pin that in both
    directions: the refusal names the cure, and the cure is one word at the
    front of that same command.

    Every arm drives the shipped hook entry (`cmd_argv_guard` reading a real
    PreToolUse payload on stdin), never a copy of its matcher. The override
    is read from the COMMAND TEXT only: the ambient-environment arm sets
    os.environ and is still refused, which is the whole design (an exported
    variable would silence the guard for every act that follows).
    """

    # THE CONTRACT, NOT THE PROSE. These pin what a refused reader must be
    # able to DO — read the rule, and send the act through — so the wording
    # can be shortened without the arms going red for a reason unrelated to
    # the decision they measure. The rung's DECISION is pinned by rc 2 and by
    # the allow controls, never by a sentence.
    RULE = "never a GitHub runner"
    PREMISE = "ci-runs-on-the-local-fabric-never-github-actions"
    OVERRIDE = "HELM_ALLOW_GITHUB_ACTIONS=1"
    # the spellings, built at runtime so this FILE does not carry a whole
    # one: the rung reads text, and a seat that greps a file holding one in
    # a Bash command would be refused by its own guard
    VERB = "workflow" + " enable"
    ENABLE = "gh " + VERB
    RERUN = "gh " + "run" + " rerun"
    WORKFLOWS = ".github/" + "workflows"
    # the same directory as the table SPELLS it: two pieces, because the
    # pieces need not touch for the row to be present
    DIRECTORY = ".github" + " " + "workflows"

    def hook(self, tool, agent_id=None, **tool_input):
        payload = {"hook_event_name": "PreToolUse", "tool_name": tool,
                   "session_id": "sess-2566", "cwd": "/tmp/repo",
                   "tool_use_id": "toolu_2566", "tool_input": tool_input}
        if agent_id:                     # a SUBAGENT's payload; absent is
            payload["agent_id"] = agent_id      # the main thread's

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = chat.cmd_argv_guard([])
        return rc, out.getvalue() + err.getvalue()

    def assert_refused(self, command):
        rc, out = self.hook("Bash", command=command)
        self.assertEqual(rc, 2, (command, out))
        self.assertIn("[helm argv-guard] BLOCKED", out, command)
        self.assertIn(self.RULE, out, command)
        self.assertIn(self.PREMISE, out, command)
        self.assertIn(self.OVERRIDE, out, command)
        return out

    def assert_allowed(self, command):
        rc, out = self.hook("Bash", command=command)
        self.assertEqual(rc, 0, (command, out))
        self.assertNotIn("BLOCKED", out, command)

    def test_the_incident_command_is_refused_with_the_rule_and_the_override(self):  # noqa: VACUOUS_ASSERTION — assert_refused runs first and unconditionally on the same hook output (rc 2 plus four required substrings), so an empty observable fails there before any allow control is reached
        out = self.assert_refused(
            "gh api -X PUT repos/o/r/actions/permissions -F enabled=true")
        self.assertIn("/actions at character", out)
        self.assertIn("ci-runs-on-the-local-fabric-never-github-actions", out)
        self.assertIn("local fabric", out)
        # WHERE the override must stand is the load-bearing half — anywhere
        # else in the command grants nothing — so the arm pins that the
        # refusal still SAYS where, not the capitals it once said it in.
        self.assertIn("first in the command", out)

    # the commands the table rows are measured through; the coverage arm
    # below reads the SHIPPED table and fails if a row no arm reaches is
    # ever added to it
    TABLE = ("gh workflow enable ci.yml",
             "gh workflow disable ci.yml",
             "gh workflow run ci.yml",
             "gh workflow view ci.yml",
             "gh workflow list",
             "gh run rerun 123",
             "gh run watch 123",
             "gh run cancel 123",
             "gh run download 123 -n artifact",
             "gh api -X PUT repos/a/b/actions/permissions",
             "gh api -X PUT repos/a/b/actions/workflows/123/enable",
             "gh api repos/a/b/actions/workflows/ci.yml/dispatches -f ref=main",
             "gh api -X POST repos/a/b/actions/runs/9/rerun",
             "gh api -X POST repos/a/b/actions/jobs/5/rerun",
             "gh api --method PATCH repos/a/b/actions",
             "gh api -XPUT orgs/akapug/actions/permissions",
             "gh api -X POST repos/o/r/dispatches -f event_type=deploy",
             "curl -X PUT https://api.github.com/repos/o/r/actions/permissions",
             "touch .github/workflows/ci.yml",
             "tee .github/workflows/ci.yml < x.yml",
             "touch .github/./workflows/x.yml",
             "touch .github//workflows/x.yml",
             "cp x.yml .github/actions/x/action.yml")

    def test_every_row_of_the_table_is_refused_and_its_neighbours_pass(self):  # noqa: VACUOUS_ASSERTION — every tuple arm executes an unconditional assertion in both directions
        """Every protected spelling, and the neighbouring commands that hold
        none. Every gh api entry carries a method or a field, because a gh
        api GET is a READ and passes whatever its path says (task/2973, the
        invocation text); curl's method is not read, so a curl on an Actions
        path is refused whatever it sends. A command that only READS the
        workflow directory passes too (measured in
        `test_the_workflow_directory_may_be_READ_and_never_WRITTEN`), and
        every other command that names it is refused with the write. The
        controls below are what keeps this a superset rather than a wall:
        they are ordinary commands a seat runs all day."""
        for cmd in self.TABLE:
            self.assert_refused(cmd)
        for cmd in ("gh pr list", "gh issue view 12", "git status",
                    "cat .github/workflows/ci.yml", "ls .github/workflows",
                    "cat docs/notes.md", "gh pr create -F body.md",
                    "gh api user", "gh run list", "gh run view 123 --log",
                    "gh api repos/a/b/actions/runs",
                    "gh api repos/o/r/contents/.github/workflows/ci.yml",
                    "git add .github",
                    "touch .github/ISSUE_TEMPLATE/bug.md",
                    "cp x.md docs/github/workflows/notes.md",
                    "helm work list --repo .",
                    "fab test --repo . -- python3 -m unittest tests.test_x"):
            self.assert_allowed(cmd)

    def test_no_row_of_the_shipped_table_is_untested(self):  # noqa: VACUOUS_ASSERTION — the observable is the COVERED list compared to the whole shipped table, and the table is asserted non-empty first, so an unreadable table fails here
        """DERIVED, never transcribed: the arms above are written from gh's
        own documented verbs and the Actions REST paths, and this reads the
        module's table back. A row added later that no arm reaches fails
        here rather than shipping unmeasured.

        The observable is the COVERED list compared to the whole table, not
        an empty list of misses: an empty covered list — a table this arm
        cannot read at all — fails here rather than passing quietly.

        A ROW IS A SET OF PIECES, so coverage asks whether ONE command above
        holds every piece of the row — not whether the joined text of all of
        them does, which a two-piece row would satisfy with its pieces in
        two different commands."""
        table = [spelling for spelling, _pieces, _says in chat._ACTIONS_ROWS]
        self.assertTrue(table, "the shipped table has no rows to cover")
        covered = [row for row in table
                   if any(all(piece in cmd.casefold()
                              for piece in row.split(" "))
                          for cmd in self.TABLE)]
        self.assertEqual(covered, table)

    def test_the_refusal_names_the_spelling_and_the_character_it_stands_at(self):  # noqa: VACUOUS_ASSERTION — every tuple arm executes an unconditional assertion on the rendered position
        """A rung that refuses on text owes the reader the exact bytes it
        objected to: the spelling as the fold left it and where it stands,
        so an operator can look at that character and see what the guard
        saw. The character counts in the FOLDED command — continuations
        joined, escapes decoded, quotes and backslashes dropped, whitespace
        collapsed, paths resolved — which is the text the scan reads, and
        the refusal says so rather than quoting a number against bytes it
        never looked at.

        A ROW OF TWO PIECES stands where its EARLIEST piece does, because
        that is the character an operator looks at first, and the expected
        number is DERIVED from the pieces rather than transcribed: each
        command below is already its own folded text, so the position is the
        smallest index any piece of the row has in it."""
        for command, row, phrase in (
                (self.ENABLE + " ci.yml", self.VERB, "enables GitHub Actions"),
                ("gh workflow run ci.yml", "workflow run", "runs a workflow"),
                (self.RERUN + " 7", "run rerun", "runs a workflow"),
                ("touch %s/ci.yml" % self.WORKFLOWS, self.DIRECTORY,
                 "workflow directory"),
                ("gh api -X PUT repos/a/b/actions/permissions", "/actions",
                 "Actions REST API")):
            out = self.assert_refused(command)
            at = min(command.index(piece) for piece in row.split(" "))
            self.assertIn("%s at character %d of the folded command"
                          % (row, at), out)
            self.assertIn(phrase, out)
        # a spelling further in reports ITS position, not zero — which is
        # what proves the number is measured and not a constant
        out = self.assert_refused("echo hi && " + self.ENABLE + " ci.yml")
        self.assertIn("%s at character 14" % self.VERB, out)
        # A CONTINUED ACT reports the JOINED spelling at the joined position:
        # the raw bytes `gh \<newline>  workflow` are three characters longer
        # than what the shell will run, and a position counted in them points
        # at the wrong place in the only text the scan read. The refusal
        # names the fold so the operator can redo it.
        out = self.assert_refused("gh \\\n  workflow enable ci.yml")
        self.assertIn("%s at character 3 of the folded command" % self.VERB,
                      out)
        self.assertIn("of the folded command", out)

    def test_the_nine_admission_classes_are_refused(self):  # noqa: VACUOUS_ASSERTION — every tuple arm executes an unconditional rc-2 assertion and each class is named beside its command
        """THE POLES. One command per class of false ALLOW measured out of
        the parsing design, each one admitted by the rung as it stood when
        the class was found and each one refused by presence over the folded
        text. They are listed with the class they
        stand for because a reader who cannot see WHICH hole an arm closes
        cannot tell whether the design closed it or the arm was written
        around the cure."""
        for why, cmd in (
                ("override boundary: a grant that is not at the front",
                 "true; HELM_ALLOW_GITHUB_ACTIONS=1 " + self.ENABLE),
                ("quoting and escape spellings: ANSI-C hex",
                 "$'\\x67\\x68 " + self.VERB + "' ci.yml"),
                ("gh global options past a fixed gap",
                 "gh --repo o/r --json number,title,author,labels,state,"
                 "createdAt,updatedAt,url,body,closedAt " + self.VERB),
                ("heredoc excision hiding the act: a script body a shell "
                 "reads, the delimiter quoted in its middle",
                 "bash <<'DATA'-END\n" + self.ENABLE + "\nDATA-END"),
                ("heredoc excision hiding the act: the act after a document "
                 "that delimiter ends",
                 "cat <<'DATA'-END\nhello\nDATA-END\n" + self.ENABLE),
                ("an Actions API spelling the table had not learned",
                 "gh api -X POST repos/o/r/actions/jobs/5/rerun"),
                ("lexical .github traversal",
                 "touch .github/x/../workflows/ci.yml"),
                ("substitution-interior quoting",
                 "cat <<OUT\n$(printf '%s\\n' ') $(" + self.ENABLE
                 + ")')\nOUT"),
                ("receiver-stage grant inheritance",
                 "bash -c 'HELM_ALLOW_GITHUB_ACTIONS=1 " + self.ENABLE + "'"),
                ("a hex-escaped path segment",
                 "touch .git\\x68ub/workflows/ci.yml")):
            self.assert_refused(cmd)
            self.assertIsNotNone(why)

    def test_every_spelling_a_parser_must_argue_about_is_refused(self):  # noqa: VACUOUS_ASSERTION — every tuple arm executes an unconditional rc-2 or rc-0 assertion on the same hook entry
        """THE SHAPE, MEASURED. Each of these is a spelling a shell parser
        must argue about: a dashed and partly quoted delimiter, a document
        whose text is a heredoc example, a substitution whose interior is
        single-quoted data, a delimiter continued across a backslash-newline,
        an fd behind a quote, a comment that swallows a command break. Each
        needs its own answer from a parser and says nothing about the next.

        WHERE THE TEXT RUNS, presence answers all of them the same way: a
        body a SHELL reads, an unquoted body's substitution, data piped into
        a shell, and a command after a document are refused however the
        delimiter is spelled.

        WHERE IT IS DATA, it passes (task/2973, the integrator's ruling): the
        same bodies handed to `cat` or `grep`, and the same act as a quoted
        argument to `printf`, are text a program reads and runs none of. The
        two lists stand side by side so a reader sees the boundary."""
        act = self.ENABLE + " ci.yml"
        for cmd in (
                # a delimiter quoted in its MIDDLE, the body a shell's script
                "bash <<'DATA'-END\ncat <<'EXAMPLE'\nhello\nEXAMPLE\n"
                + act + "\nDATA-END",
                # an unquoted body's substitution RUNS, whoever reads the body
                "cat <<OUT\n$(" + act + ")\nOUT",
                # a dashed delimiter whose prefix is an identifier, then a
                # shell's body
                "cat <<DATA-END\nhello\nDATA-END\nbash <<'EOF'\n" + act + "\nEOF",
                # a delimiter that never arrives: the rest is an unquoted body
                "cat <<NOPE\n" + act + "\ncat <<'EOF'\nhello\nEOF",
                # the delimiter word forms bash accepts, under a shell
                "bash <<\\EOF\n" + act + "\nEOF",
                'bash <<"E"OF\n' + act + "\nEOF",
                "cat <<E-O_F.1\n" + act + "\nE-O_F.1",
                # an fd spelled with and without a quote
                "bash -s <<'EOF' 2 < /dev/null\n" + act + "\nEOF",
                'bash -s <<\'EOF\' ""2</dev/null\n' + act + "\nEOF",
                # an apostrophe in a comment above the opener
                "# Don't execute the example.\nbash <<'EOF'\n" + act + "\nEOF",
                # a heredoc whose stdin is taken away afterwards
                "bash <<'EOF' < /dev/null\n" + act + "\nEOF",
                # data piped into a shell is the shell's script
                "printf '%s\\n' '" + act + "' | bash",
                "grep -v x <<'EOF' | sh\n" + act + "\nEOF",
                # a quoted operator beside a workflow path
                "printf '%s\\n' '>' '" + self.WORKFLOWS + "/ci.yml'",
                # a READ of a workflow file through a writer utility
                "cp %s/ci.yml docs/ci-reference.yml" % self.WORKFLOWS,
                "touch -ar %s/ci.yml stamp.txt" % self.WORKFLOWS,
                # sed's optional attached suffix, whose PROGRAM happens to
                # spell the directory
                "sed -i.foo 's/.github/workflows/g' notes.txt",
                # a path that leaves the directory — the TEXT still names
                # it, and text is what this rung reads
                "touch %s/../ISSUE_TEMPLATE/bug.md" % self.WORKFLOWS,
                # …and one that reaches it, which the uncollapsed reading
                # alone would miss
                "touch .git/../.github/workflows/ci.yml"):
            self.assert_refused(cmd)
        # DATA: the same text where no program runs it
        for cmd in (
                "cat <<'DATA'-END\ncat <<'EXAMPLE'\nhello\nEXAMPLE\n"
                + act + "\nDATA-END",
                "cat <<'OUT'\n$(" + act + ")\nOUT",
                "cat <<\\EOF\n" + act + "\nEOF",
                'cat <<"E"OF\n' + act + "\nEOF",
                "# Don't execute the example.\ncat <<'EOF'\n" + act + "\nEOF",
                "grep -v bash <<'EOF'\n" + act + "\nEOF",
                "printf '%s\\n' bash -c '" + act + "'"):
            self.assert_allowed(cmd)

    def test_the_fold_is_read_both_ways_where_one_reading_cannot_serve(self):  # noqa: VACUOUS_ASSERTION — the first line is an unconditional refusal control through assert_refused (rc 2 plus four required substrings) on the same hook entry, and every allow arm asserts rc == 0, the allow VALUE
        """TWO DISAGREEMENTS, both resolved toward refusal.

        A LETTER ESCAPE is a deleted backslash outside $'…' and a character
        inside it. `gh work\\flow enable` needs the deletion — the f is a
        letter of the word — and $'workflow\\nenable' needs the decode,
        where the escape is the space between two words. One reading cannot
        be both, so both are read.

        A PATH COLLAPSE can only ever REMOVE text, so the uncollapsed
        reading is kept beside it: `.github/x/../workflows` reaches the
        directory only after the collapse, and
        `.github/workflows/../ISSUE_TEMPLATE` names it only before.

        The control is the same command with neither escape nor traversal,
        which must pass — otherwise these arms would read as refusals of
        everything."""
        # unconditional positive control: this harness can see a refusal at
        # all, before any loop decides whether a folding found one
        self.assertIn("folded command", self.assert_refused(self.ENABLE))
        for cmd in ("gh work\\flow enable ci.yml",
                    "gh workflow en\\able ci.yml",
                    "$'" + self.VERB.replace(" ", "\\n") + "'",
                    "$'\\167orkflow enable'",
                    "touch .github/x/../workflows/ci.yml",
                    "touch %s/../ISSUE_TEMPLATE/bug.md" % self.WORKFLOWS):
            self.assert_refused(cmd)
        for cmd in ("gh pr list", "printf 'a\\nb\\n' > notes.txt",
                    "cp x.md docs/github/workflows/notes.md"):
            self.assert_allowed(cmd)

    def test_an_ESCAPE_takes_the_digits_BASH_gives_it(self):  # noqa: VACUOUS_ASSERTION — every spelling runs assert_refused unconditionally (rc 2 plus four required substrings) before the allow control is reached
        """A DIGIT COUNT IS A FACT ABOUT THE SHELL, not a choice, and this
        fold had three of them wrong. Measured against bash 5.3 itself:
        `$'\\u67\\u68'` and `$'\\U67\\U68'` are both `gh`, so `\\u` takes ONE
        to four digits and `\\U` one to EIGHT; and an octal escape is a
        BYTE — `$'\\547'` is `g`, `$'\\777'` is `\\xff`, `$'\\400'` is the
        empty string. The fold read `\\u` at exactly four, `\\U` at exactly
        eight and an octal unmasked (`chr(0o547)`, which is `ŧ` and spells
        no row), so each spelling below stood undecoded in every reading and
        the hook returned 0 while bash ran the act.

        AND THE `\\U` BRANCH WAS DEAD, which only running it showed: one
        IGNORECASE flag over the whole alternation let the `u` branch match
        `\\U`, eat four of its eight digits and return a NUL, so the
        eight-digit form — the one the fold's own comment named — decoded to
        neither the character nor its own text. The introducer letter is
        case-SENSITIVE in bash (`$'\\X67'` is four literal characters there,
        measured) and the digits are not, so the pattern says that and the
        upper-case `\\X` control below stays allowed, as bash reads it.

        THE SPELLINGS ARE DERIVED from the protected word, never
        transcribed: a table of hand-typed digits is a table of my own
        arithmetic, and the arithmetic is the thing under test.

        BOTH CONTROLS MATTER. Digits that decode to a word ONE LETTER off
        the row pass through the same decoder and are allowed — which is
        what proves these refusals came from the decode rather than from the
        shape of an escape. And the same digits with NO `$'` introducer,
        which bash does not decode at all (measured: `'\\147\\150'` stays
        six characters), are refused anyway: the fold decodes
        unconditionally because it reads no context, and a `never` rule
        resolves that disagreement toward the refusal."""
        noun = self.VERB.split(" ")[0]
        spelled = {
            "short \\u": "".join("\\u%x" % ord(c) for c in noun),
            "short \\U": "".join("\\U%x" % ord(c) for c in noun),
            "octal above 255": "".join("\\%o" % (ord(c) + 0o400) for c in noun),
            # the widths that were already decoded, kept so a narrowing
            # cannot pass unseen
            "four-digit \\u": "".join("\\u%04x" % ord(c) for c in noun),
            "eight-digit \\U": "".join("\\U%08x" % ord(c) for c in noun),
            "octal under 255": "".join("\\%o" % ord(c) for c in noun),
        }
        for why, digits in spelled.items():
            out = self.assert_refused("gh $'%s' enable ci.yml" % digits)
            self.assertIn(self.VERB, out, why)
        # bash decodes none of these — there is no `$'` in front of them —
        # and the fold decodes them anyway, which is the safe direction
        # (handed to a program the invocation reader does not know, so the
        # quoted argument is read and not cut as data)
        self.assert_refused("true '%s enable'" % spelled["octal above 255"])
        # CONTROL: one letter off the row, through the same decoder
        self.assert_allowed("gh $'%s' enable ci.yml"
                            % "".join("\\u%x" % ord(c) for c in noun[:-1] + "x"))
        # CONTROL: an introducer bash does not read is not read here either
        self.assert_allowed("gh $'%s' enable ci.yml"
                            % "".join("\\X%x" % ord(c) for c in noun))

    def test_an_UNRESOLVED_EXPANSION_inside_a_token_is_refused(self):  # noqa: VACUOUS_ASSERTION — the first line is an unconditional refusal control through assert_refused (rc 2 plus four required substrings) on the same hook entry, and every arm below asserts rc 2 or rc 0, the allow VALUE
        """A HOLE IN THE TEXT IS STILL TEXT AROUND THE HOLE. A `$name`,
        `${…}`, `$(…)` or backtick span inside a protected token executed
        the protected argv and the rung returned 0: `acti${x}ons/permissions`
        is the incident's API path, and `gh work$(echo flow) enable` is the
        act with four characters of it computed. So the fold reads one more
        time with every expansion DELETED, and where the deleted span stood
        INSIDE a word it leaves a mark a row piece may span, because the
        text that was removed is unknown and could be the rest of the piece.

        WHY NOT REFUSE EVERY COMMAND CARRYING AN EXPANSION. That rule's
        refusal population is nearly every command the fleet runs — a guard
        that stands on every Bash call cannot cost that — so the mark never
        stands for a WHOLE piece, only for part of one, and a span that is a
        whole token leaves no mark at all.

        AND A MARK MAY FINISH A PIECE, NEVER START ONE, which is MEASURED
        and not tasteful: with the mark allowed to open a piece, a run over
        this repository's own shell scripts and doc command blocks (9,035
        lines, 3,193 carrying a `$` or a backtick) newly refused three lines
        of ordinary prose, all the same shape — a backticked code span
        pluralised by the letter after it, where the mark stood for
        `workflow` and the `s` finished `workflows`. One of those lines is
        an allow control below, copied from this repo. With the boundary in,
        the refusal population over that corpus is UNCHANGED by this whole
        lane: two lines before, the same two after.

        THE LIMIT THIS ARM ONCE MEASURED AS AN ALLOW — a token supplied
        WHOLE by a runtime value (`gh workflow $VERB`), or a token whose
        BEGINNING is one (`$Wflow enable`) — is NARROWED by the scoped rule
        (`test_an_ANCHOR_beside_an_EXPANSION_is_refused`), which asks a
        different question about the same text: not whether the row stands
        in it, but whether an ANCHOR stands beside a hole. The witnesses stay
        here, on the side the design put them on, so a change to either rule
        cannot pass unseen: `workflow` is the anchor of its rows and
        `gh workflow $VERB ci.yml` is refused; `enable` is an ordinary
        English word, its row has no anchor, and `gh $W enable ci.yml` is the
        limit that is still open.

        The controls are the commands this would otherwise cost: an
        expansion standing as its own word, which is how the fleet writes
        nearly all of them."""
        self.assertIn("/actions", self.assert_refused(
            "gh api -X PUT repos/o/r/actions/permissions"))
        for cmd in ("gh api -X PUT repos/o/r/acti${x}ons/permissions",
                    "gh api -X PUT repos/o/r/acti$IT/ons/permissions",
                    "gh work$(echo flow) enable ci.yml",
                    "gh work$(echo $(printf flow)) enable ci.yml",
                    "gh work${pick:-$(echo flow)} enable ci.yml",
                    "gh work`echo flow` enable ci.yml",
                    "touch .git${h}ub/workflows/ci.yml",
                    "printf 'on: push' > .github/work$(echo flows)/evil.yml"):
            self.assert_refused(cmd)
        # THE LIMIT THIS ARM ONCE ASSERTED AS AN ALLOW is narrowed by the
        # SCOPED rule, and the arm below owns it: a whole token from a
        # runtime value beside the ANCHOR the operator did write
        for cmd in ("gh workflow $VERB ci.yml", "gh workflow $VERB"):
            self.assert_refused(cmd)
        # …and the half of it the anchor leaves open, asserted here so it is
        # read as a limit and not as coverage: the NOUN from a runtime value
        # beside an ordinary English verb
        for cmd in ("gh $W enable ci.yml", "gh $Wflow enable ci.yml"):
            self.assert_allowed(cmd)
        # CONTROLS: the population a blanket rule would have cost, and the
        # prose line from this repo's own docs that a mark allowed to OPEN a
        # piece refused (measured over 9,035 corpus lines)
        for cmd in ("and one of its own `git worktree`s declaring a real "
                    "suite are ONE repository, so",
                    "echo $HOME",
                    "ls $(pwd)",
                    "git log $(git rev-parse HEAD)",
                    "cd $HOME/dev/akapug/helm && git status --short",
                    "fab test --repo $WT -- python3 -m unittest tests.test_x",
                    "helm task note ${id} --text done",
                    "printf '%s\\n' \"$(date)\" 'a `b` c'",
                    "echo \\$HOME is not an expansion",
                    "rg -n \"$PATTERN\" -- helm/"):
            self.assert_allowed(cmd)

    def test_a_QUOTE_beside_an_expansion_is_not_a_WORD_BREAK(self):  # noqa: VACUOUS_ASSERTION — the first line is an unconditional refusal control through assert_refused (rc 2 plus four required substrings) on the same hook entry, and every arm below asserts rc 2 or rc 0, the allow VALUE
        """A QUOTE MARK IS NOT WHITESPACE, and reading it as one deleted the
        mark outright. `gh work"$(echo flow)" enable ci.yml` folded to
        `gh work$(echo flow) enable` and `gh work enable` — neither holds
        the noun — and the hook returned 0 while bash joined the quoted
        halves into ONE word and handed gh the row (measured against real
        bash with a fake `gh` recording its argv; eight spellings, the file
        door among them, and the REST path `acti"$(echo ons)"/permissions`
        is the incident's own).

        The mark decision reads the first character on either side that is
        not part of a quote mark — `\'`, `"`, and the two-character `$\'`
        and `$"` — and never reads PAST whitespace, because whitespace IS
        bash's word break. So `gh "work" "$(echo flow)" ci.yml`, which bash
        splits into two words of argv, leaves no mark, exactly as the
        unquoted spelling with a space in it does.

        THE MARK IS EMITTED BEFORE THE QUOTES ARE DELETED, which is the
        other half of the cure: the fold marks the raw text and then deletes
        quote characters around the mark, so `acti"${x}"ons` reads
        `acti<mark>ons` rather than losing the hole to the deletion."""
        self.assertIn("/actions", self.assert_refused(
            "gh api -X PUT repos/o/r/actions/permissions"))
        for cmd in ('gh work"$(echo flow)" enable ci.yml',
                    'gh api -X PUT repos/o/r/acti"$(echo ons)"/permissions'
                    ' -F enabled=true',
                    'gh work"$FLOW" enable ci.yml',
                    'gh work""$(echo flow) enable ci.yml',
                    'gh "work""$FLOW" enable ci.yml',
                    'gh work"`echo flow`" enable ci.yml',
                    'gh api -X PUT repos/o/r/acti"${x}"ons/permissions',
                    "gh 'work'$(echo flow) enable ci.yml",
                    'printf \'on: push\' > .git"$(echo h)"ub/workflows/ci.yml'):
            self.assert_refused(cmd)
        # the FILE door takes the same fold, and missed the same way
        for tool in ("Write", "Edit"):
            rc, out = self.hook(tool, file_path='.git"$(echo h)"ub/workflows'
                                               '/ci.yml', content="on: push",
                                old_string="a", new_string="b")
            self.assertEqual(rc, 2, out)
            self.assertIn("BLOCKED", out)
        # CONTROL: WHITESPACE is still the word break the mark stops at —
        # two quoted words are two words of argv, and `work` and `flow`
        # reach gh as separate operands, which is not the act
        self.assert_allowed('gh "work" "$(echo flow)" ci.yml')
        self.assert_allowed("gh api repos/o/r/acti \"$(echo ons)\"/perms")
        # CONTROL: the quoted expansions the fleet writes all day
        for cmd in ('echo "$HOME" is home',
                    'rg -n "$PATTERN" -- helm/',
                    'printf \'%s\\n\' "$(date)"'):
            self.assert_allowed(cmd)

    def test_a_CONTROL_ESCAPE_takes_the_CHARACTER_BASH_gives_it(self):  # noqa: VACUOUS_ASSERTION — the first line is an unconditional refusal control through assert_refused (rc 2 plus four required substrings) on the same hook entry, and every arm below asserts rc 2 or rc 0, the allow VALUE
        """BASH HAS TWO SPELLINGS FOR A CONTROL CHARACTER and the fold knew
        one. `$\'\\cI\'` is a TAB and `$\'\\cJ\'` a NEWLINE, so
        `eval $\'gh workflow\\cIenable ci.yml\'` is word-split back into the
        row and gh runs it — measured, with both protected words standing
        as characters of the command text and no runtime value anywhere in
        it, so this is NOT the whole-token limit. The fold merely deleted
        the backslash, glued `workflow` to `enable` through a surviving
        `ci`, and returned 0.

        THE ARITHMETIC IS MEASURED, not recalled: `\\cI` is \\x09, `\\c[`
        \\x1b, `\\c0` \\x10 and `\\cx` \\x18, which is `toupper(c) & 0x1f`
        and not the `^ 0x40` a letter table suggests — `\\c0` would be `p`
        under that. `\\c?` is DEL, the one exception.

        A SEPARATOR IS NOT ALWAYS AN ACT, which is what keeps these
        witnesses honest: the same escape inside a REST PATH splits the path
        into two operands (`repos/o/r/acti` and `ons/permissions`), so bash
        performs nothing there and this arm does not call it a witness. The
        witnesses are the spellings where the split RE-MAKES the argv the
        act needs.

        `\\c@` IS A ZERO BYTE AND IS DROPPED, and the control below is what
        that is FOR: the fold's expansion mark is a NUL, and a decode that
        produced one would let a command FORGE a mark — `echo work$\'\\c@\'
        enable` would read `work<mark> enable`, where the mark finishes
        `workflow` and the row stands. bash drops the NUL and hands the
        word `work` on, so the fold drops it too and the command passes."""
        self.assertIn("/actions", self.assert_refused(
            "gh api -X PUT repos/o/r/actions/permissions"))
        noun, verb = self.VERB.split(" ")
        for cmd in ("eval $'gh %s\\cI%s ci.yml'" % (noun, verb),
                    "bash -c $'gh %s\\cI%s ci.yml'" % (noun, verb),
                    "eval $'gh %s %s\\cJtrue'" % (noun, verb),
                    "V=$'%s\\cI%s'; gh $V ci.yml" % (noun, verb)):
            self.assert_refused(cmd)
        # THE OVER-REFUSALS, measured and kept: bash splits a word on a TAB
        # and a NEWLINE and on NOTHING ELSE, so `\cM` (a CR) reaches gh as
        # one word and `$'work\c@flow'` reaches it as `work` — bash
        # truncates the rest of that string at the zero byte. The fold
        # collapses the CR as whitespace and drops the zero byte, and
        # refuses both; a `never` rule resolves a disagreement toward the
        # refusal, and the fold reads no quote context to truncate by.
        for cmd in ("eval $'gh %s\\cM%s ci.yml'" % (noun, verb),
                    "gh $'%s\\c@%s' %s ci.yml" % (noun[:4], noun[4:], verb)):
            self.assert_refused(cmd)
        # CONTROL: the mark stays UNFORGEABLE — no reading of a command
        # carrying `\c@` holds the mark character, and the row a forged one
        # would complete is not refused
        readings = chat.folded_commands("echo %s$'\\c@'" % noun[:4])
        self.assertEqual([r for r in readings if "\x00" in r], [])
        self.assert_allowed("echo %s$'\\c@' %s" % (noun[:4], verb))
        # CONTROL: one letter off the row, through the same decoder
        self.assert_allowed("eval $'gh %sx\\cI%s ci.yml'" % (noun, verb))

    def test_an_ANCHOR_beside_an_EXPANSION_is_refused(self):  # noqa: VACUOUS_ASSERTION — the first line is an unconditional refusal control through assert_refused (rc 2 plus four required substrings) on the same hook entry, and every arm below asserts rc 2 or rc 0, the allow VALUE
        """THE SCOPED RULE. A token supplied WHOLE by a runtime value
        performs the act with a word the text does not hold: `gh workflow $V
        ci.yml` runs it, and no reading of that text can hold a piece nobody
        wrote. The blanket cure — refuse every command carrying an expansion
        — costs nearly every command the fleet runs, so the rule is scoped:
        an ANCHOR plus a hole is a row.

        AN ANCHOR IS THE NOUN, NEVER THE VERB. The anchor of a row is the
        piece that means nothing outside GitHub Actions — `workflow`,
        `rerun`, `.github`, `/actions`, `/dispatches` — and a row that has
        none (`run watch`, `run cancel`, `run download`) gets no scoped rule
        at all. `run`, `list`, `view`, `watch`, `cancel`, `download`,
        `enable` and `disable` are ordinary English this fleet types every
        day; beside a hole they say nothing about GitHub.

        A BASE-RATE THRESHOLD STOOD HERE AND IT WAS THE WRONG PROPERTY.
        Rarity is not what makes a command an Actions command. Measured over
        the 120,275 distinct Bash and Monitor commands this project's own
        transcripts hold, only `/dispatches`, `enable`, `/actions` and
        `download` fall below one in two thousand — so the threshold ALLOWED
        `gh workflow $V ci.yml` and REFUSED `gh $W enable ci.yml`, exactly
        backwards from what protects the repository. The rates still decide
        what the anchor set COSTS (module comment), never which pieces it
        holds.

        The controls are the four the design owes: an expansion beside no
        piece, an expansion beside an ordinary word, an anchor with no
        expansion at all, and the rows whose anchor was dropped."""
        self.assertIn("/actions", self.assert_refused(
            "gh api -X PUT repos/o/r/actions/permissions"))
        for cmd in ("gh workflow $V ci.yml",
                    "gh $W rerun 123",
                    "gh work$(echo flow) $V ci.yml",
                    "git add .github/$NAME",
                    "printf 'on: push' > $WT/.github/ci.yml"):
            out = self.assert_refused(cmd)
            self.assertIn("beside an unresolved expansion", out)
        # a ONE-PIECE row answers through the ROW rule whether a hole stands
        # beside it or not, so its anchor flag never decides anything
        self.assertIn("triggers repository_dispatch", self.assert_refused(
            "curl -X POST $URL/dispatches -d @body.json"))
        # the GRANT lifts the scoped rule exactly as it lifts a whole row
        self.assert_allowed(self.OVERRIDE + " gh workflow $V ci.yml")
        # CONTROL: an expansion beside NO piece of any row
        for cmd in ("echo $HOME",
                    "ls $(pwd)",
                    "cd $HOME/dev/akapug/helm && git status --short",
                    "fab test --repo $WT -- python3 -m unittest tests.test_x",
                    "helm task note ${id} --text done",
                    "rg -n \"$PATTERN\" -- helm/",
                    "git commit -F $MSG"):
            self.assert_allowed(cmd)
        # CONTROL: an expansion beside an ORDINARY WORD — the 12,588
        # commands the wide rule refused, which is what the anchor gives back
        for cmd in ("gh pr view $(git branch --show-current)",
                    "helm gate run --repo $WT --focus",
                    "helm chat list --room $R",
                    "helm task list | rg \"$ID\"",
                    "gh run view $ID"):
            self.assert_allowed(cmd)
        # CONTROL: an ANCHOR with NO expansion stays allowed, which is the
        # mention this rung has always let through
        for cmd in ("echo the workflow is local",
                    "git add .github",
                    "echo rerun the fabric gate"):
            self.assert_allowed(cmd)
        # THE LIMIT THIS LEAVES OPEN, asserted so nobody reads the scope as
        # wider than it is: the rows with NO anchor have no scoped rule, so a
        # whole token from a value beside their ordinary verbs passes
        for cmd in ("gh $W enable ci.yml",
                    "gh $W download 12",
                    "gh $W en$(echo able) ci.yml",
                    "curl -o out $URL && gh $X download 5"):
            self.assert_allowed(cmd)

    def test_the_ANCHOR_flag_is_the_tables_own_and_the_rule_reads_it(self):  # noqa: VACUOUS_ASSERTION — every assertion is unconditional over a table asserted non-empty first, and each piece is driven through the shipped hook in both directions
        """DERIVED FROM THE SHIPPED TABLE, never transcribed. The scoped
        rule's population is the `+` flags in `_ACTIONS_TOKENS`, so this arm
        reads them back and drives EVERY piece of the table through the hook
        in the shape the rule decides: the piece, and an expansion beside it.

        ONE FLAG PER PIECE. A piece stands in several rows (`workflow` in
        five, `run` in four), and a piece that is an anchor in one row and an
        ordinary word in the next is two readings of one table: the scoped
        rule would then refuse or allow the same text depending on which row
        the scan reached first. The whole point of one table is that it
        cannot, so it is asserted here rather than left to the eye.

        AT MOST ONE ANCHOR PER ROW. The anchor is what makes the row's act an
        Actions act; a row with two of them is a row whose author did not
        decide which piece that is. A row may have NONE, and then it has no
        scoped rule at all — `run watch`, `run cancel` and `run download` are
        two ordinary English words each.

        A ONE-PIECE ROW ANSWERS THROUGH THE ROW RULE — `/actions` and
        `/dispatches` are whole rows on their own and refuse with no
        expansion needed — so its flag decides nothing, which this arm shows
        by asserting it refuses either way."""
        flags = {}
        for _spelling, pieces, _says in chat._ACTIONS_ROWS:
            for text, _matcher, anchor in pieces:
                self.assertEqual(flags.setdefault(text, anchor), anchor, text)
        self.assertTrue(flags, "the shipped table has no pieces")
        self.assertEqual(
            sorted(p for p, on in flags.items() if on),
            [".github", "/actions", "/dispatches", "rerun", "workflow"])
        for spelling, pieces, _says in chat._ACTIONS_ROWS:
            self.assertLessEqual(
                len([1 for _t, _m, anchor in pieces if anchor]), 1, spelling)
        for _spelling, pieces, _says in chat._ACTIONS_ROWS:
            alone = len(pieces) == 1
            for text, _matcher, anchor in pieces:
                witness = "echo %s $VALUE" % text
                if anchor or alone:
                    self.assertIn("BLOCKED", self.assert_refused(witness))
                else:
                    self.assert_allowed(witness)
                # and with NO expansion, only a one-piece row answers
                mention = "echo %s is local" % text
                if alone:
                    self.assert_refused(mention)
                else:
                    self.assert_allowed(mention)

    def test_a_ROW_is_counted_over_the_READINGS_TOGETHER(self):  # noqa: VACUOUS_ASSERTION — the zero is one half of a single unconditional assertion whose OTHER half is the positive control on the same observable (every reading holds SOME piece, and the reading count was asserted greater than one first), and assert_refused below is a second unconditional positive assertion on the hook's answer for this witness
        """A SET OF PIECES IS NOT EVIDENCE UNTIL IT IS COUNTED TOGETHER. The
        fold yields several readings because two of its steps disagree with
        themselves, and each reading was scanned ALONE — so a row whose
        pieces stood in different readings was discarded piece by piece and
        the hook returned 0. `work\\flow` spells the noun only where the
        backslash is DELETED, and `ci$'\\n'enable` spells the verb only
        where the escape is DECODED (deleted, it reads `cinenable`, and a
        piece is a whole piece).

        The readings are joined by a newline, which no folded reading holds
        and no piece can span, so the join adds no spelling that no reading
        held.

        THE SPLIT IS ASSERTED, NOT ASSUMED, with the SHIPPED piece matchers:
        no single reading holds the whole row, and every piece stands in one
        of them. Without that, an arm like this passes for the wrong reason
        the day one reading starts holding both."""
        witness = "gh work\\flow ci$'\\n'enable"
        pieces = [matcher for _text, matcher, _rare
                  in next(p for spelling, p, _says in chat._ACTIONS_ROWS
                          if spelling == self.VERB)]
        readings = chat.folded_commands(witness)
        self.assertEqual(len(pieces), len(self.VERB.split(" ")))
        self.assertGreater(len(readings), 1, readings)
        # ONE unconditional assertion over ONE observable, and it is
        # positive in its first half: every reading holds SOME piece of the
        # row, and none of them holds the row whole.
        holding = [len([r for r in readings
                        if any(piece.search(r) for piece in pieces)]),
                   len([r for r in readings
                        if all(piece.search(r) for piece in pieces)])]
        self.assertEqual(holding, [len(readings), 0], readings)
        self.assert_refused(witness)
        # CONTROL: one piece and no other, in the same spelling, still
        # passes — the union counts pieces, it does not refuse readings
        self.assert_allowed("gh work\\flow ci.yml")
        self.assert_allowed("echo ci$'\\n'enable")

    def test_the_GRANT_ends_where_BASH_ends_the_assignment_word(self):  # noqa: VACUOUS_ASSERTION — every tuple arm executes an unconditional assertion in both directions on the same hook entry
        """AN ASSIGNMENT WORD ENDS AT WHITESPACE OR AT THE END OF THE
        COMMAND, and the lookahead that also admitted `;`, `&`, `|`, `(` and
        `)` glued to the value read an operator as if it ended the word in
        the command's favour. Bash does the opposite with the operators that
        end the COMMAND: `HELM_ALLOW_GITHUB_ACTIONS=1;gh workflow enable` is
        a bare assignment and THEN a separate command, and the separate
        command does not carry the variable at all (measured: its
        environment does not hold it) — while the rung granted the whole
        line. That is a HOLE, and it is closed here.

        THE OTHER DIRECTION IS A COST, taken deliberately. A redirection
        glued to the value — `…=1<input gh workflow enable`, which bash
        assigns exactly 1 for (measured) — is refused by the same rule, and
        the cure is the space every writer of this word puts there anyway. A
        `never` rule resolves toward the refusal.

        AFTER THE WHITESPACE, EVERYTHING IS THE COMMAND, redirections and
        operators included: the grant is whole-command, so a `<`, `>`, `2>`,
        `|` or `;` LATER in the line changes nothing about it.

        AND BASH'S WHITESPACE IS NOT PYTHON'S, which reopened the hole
        through the class that closed it. `\\s` is ` \\t\\n\\r\\f\\v`;
        bash's blanks are the space and the TAB, and a NEWLINE is not a
        blank there but a command TERMINATOR — the `;` this arm already
        refuses. Measured: `…=1<newline>gh workflow enable ci.yml` ran the
        act with the variable UNSET in gh's environment (the fake gh
        recorded `<unset>`; the space-separated spelling of the same command
        recorded `1`), and the rung returned 0 — in the commonest shape
        there is, a multi-line command whose first line is the word.

        SO THE GRANT IS: the word, a blank, AND A COMMAND AFTER IT ON THAT
        LINE. The last clause is what makes this one rule instead of a list
        of terminators: `…=1 ; gh …`, `…=1 > out<newline>gh …` and
        `…=1 # note<newline>gh …` are the same bare assignment spelled three
        more ways, all measured running the act unassigned.

        A BACKSLASH-NEWLINE IS THE MIRROR, and it is a GRANT: bash removes a
        continuation before it reads anything, so `…=1\\<newline> gh
        workflow enable` assigns exactly 1 (measured) while the raw text put
        a backslash where the blank had to be. The grant is matched against
        the continuation-joined command, the same join the fold does.

        AND `A COMMAND` IS NOT ONE CHARACTER, which is the hole this arm was
        written around and did not close. Bash's COMMAND PREFIX stands
        between the blank and the command word — more assignment words and
        redirections, any number, any order — and a rule that asked only
        whether the next CHARACTER could open a command word took the prefix
        itself for the command. Measured against bash with a fake gh
        recording its own environment: `…=1 2>/dev/null gh workflow enable
        ci.yml` and `…=1 X=2 gh …` assign exactly 1 and must stay GRANTS,
        while `…=1 2>/dev/null<newline>gh …` and `…=1 X=2<newline>gh …` ran
        the act with the name UNSET — the bare-assignment hole one token
        further along, and the file descriptor's digit was admitted although
        the bare `>` beside it was not. So the rule walks the prefix and
        asks for a word that is no part of it before the line ends.

        ONE MEASURED REFUSAL IS GIVEN BACK BY THAT WALK: `…=1 >out.txt gh
        workflow enable ci.yml` assigns 1 (measured) and is a grant now,
        where the one-character rule refused it. A redirection GLUED to the
        value is still refused, because the word did not end at a blank."""
        act = self.ENABLE + " ci.yml"
        for tail in ("< input", "> out", "2> err", "| cat", "; true",
                     "&& echo done", "</dev/null", ">/dev/null 2>&1"):
            self.assert_allowed("%s %s %s" % (self.OVERRIDE, act, tail))
        self.assert_allowed(self.OVERRIDE + " " + act)
        self.assert_allowed(self.OVERRIDE + "\t" + act)
        for glued in (";", "&", "|", "<input ", ">out ", "&&"):
            self.assert_refused(self.OVERRIDE + glued + act)
        # A NEWLINE ENDS THE COMMAND, so the word prefixes nothing — and
        # neither does a spaced separator, a redirection or a comment
        for after in ("\n", " \n", "\nset -e\n", " ; ", " # note\n",
                      " > out\n", "\r", "\v", "\f"):
            self.assert_refused(self.OVERRIDE + after + act)
        # THE PREFIX WALK, both directions. A prefix that ENDS with the line
        # grants nothing: bash ran the act with the name unset in every one
        # of these (measured, `<unset>` in gh's environment).
        for prefix in (" 2>/dev/null\n", " X=2\n", " 1>out.txt\n",
                       " 2>&1\n", " 0<in\n", " X=$PWD\n", " X=2 ; ",
                       " 2>/dev/null ; ", " X=2 | ", " X=2 # note\n"):
            self.assert_refused(self.OVERRIDE + prefix + act)
        # …and the same prefix with the command ON THE LINE is a grant, for
        # which bash assigns exactly 1 (measured, the same fake gh)
        for prefix in (" 2>/dev/null ", " <in ", " >out.txt ", " X=2 ",
                       " X=2 Y=3 ", " X='a b' ", " 2>&1 ", " X=2 2>/dev/null ",
                       " 2> err "):
            self.assert_allowed(self.OVERRIDE + prefix + act)
        # A CONTINUATION IS A BLANK TO BASH, so it is a grant here
        self.assert_allowed(self.OVERRIDE + "\\\n " + act)
        self.assert_allowed(self.OVERRIDE + "\\\n\t" + act)
        self.assert_refused("(%s)%s" % (self.OVERRIDE, act))
        # the forged words, refused before and after: none of them assigns
        # this name to this value
        for forged in ("x ", "1 ", '=fake ', "-fake "):
            self.assert_refused(self.OVERRIDE + forged + act)
        self.assert_refused(self.OVERRIDE[:-1] + '"1" ' + act)

    def test_the_spellings_a_literal_scan_walked_past_are_refused(self):  # noqa: VACUOUS_ASSERTION — every tuple arm executes an unconditional rc-2 assertion
        """THE WITNESSES, each measured admitted by a scan that read the RAW
        string. A literal match is defeated by the characters a shell
        deletes on its way to the words, and the shell runs the act either
        way: `g\\h` is gh, `actions/'permissions'` is the same API path, and
        a continuation puts a line break through the middle of any of
        them."""
        for cmd in (
                "g\\h workflow enable ci.yml",
                "gh run re\\run 123",
                "touch .git\\hub/workflows/ci.yml",
                "touch .github/work\\flows/ci.yml",
                "gh api -X PUT repos/o/r/actions/'permissions' -F enabled=true",
                'gh api -X PUT "repos/o/r/actions/permissions"',
                "touch .github/'workflows'/ci.yml",
                'touch ".github"/workflows/ci.yml',
                "gh 'workflow' enable ci.yml",
                'gh "workflow" "enable" ci.yml',
                "gh \\\n  workflow \\\n  enable ci.yml",
                "touch .github/\\\nworkflows/ci.yml",
                "gh run \\\n  rerun 123",
                "gh\t workflow   enable ci.yml",
                # CASE is folded: a shell cares, a never rule does not
                "GH WORKFLOW ENABLE ci.yml"):
            self.assert_refused(cmd)

    def test_a_quote_MARK_is_two_characters_where_bash_says_it_is(self):  # noqa: VACUOUS_ASSERTION — the first line is an unconditional refusal control through assert_refused (rc 2 plus four required substrings) on the same hook entry, and every arm below asserts rc 2 or rc 0, the allow VALUE
        """TWO CHARACTERS TYPED INTO ANY TOKEN defeated this rung, because
        bash's ANSI-C and locale quoting open with `$'` and `$"` and the fold
        deleted only the quote. `acti$''ons` folded to `acti$ons` — the `$`
        left standing INSIDE the word — which holds no protected spelling,
        while the shell hands gh the word `actions`. Measured with bash
        itself: the incident command spelled that way passes `[api] [-X]
        [PUT] [repos/o/r/actions/permissions]` to argv, and
        `printf 'on: push' > .github$''/workflows/evil.yml` created the
        workflow file in a scratch tree.

        The controls are the same commands without the two characters, which
        were refused throughout, and the grant, which still sends them."""
        # unconditional positive control: the untouched incident command
        self.assertIn("/actions", self.assert_refused(
            "gh api -X PUT repos/o/r/actions/permissions -F enabled=true"))
        for cmd in ("gh api -X PUT repos/o/r/acti$''ons/permissions "
                    "-F enabled=true",
                    "gh api --method PUT repos/o/r/acti$''ons/permissions",
                    'curl -X PUT https://api.github.com/repos/o/r/'
                    'acti$""ons/permissions',
                    "gh workflow$'' enable ci.yml",
                    "gh $'work'$'flow' enable ci.yml",
                    "gh run$'' rerun 123",
                    "printf 'on: push' > .github$''/workflows/evil.yml",
                    'touch .git$""hub/workflows/ci.yml'):
            self.assert_refused(cmd)
        # the plain empty-quote spellings, refused before and after
        self.assert_refused('touch .github""/workflows/ci.yml')
        self.assert_refused("gh 'workflow'' enable' ci.yml")
        # and the cure is unchanged by the spelling
        self.assert_allowed("HELM_ALLOW_GITHUB_ACTIONS=1 gh api -X PUT "
                            "repos/o/r/acti$''ons/permissions")
        # the edge: a `$` that opens no quote is a `$`, and a command that
        # spells no row still passes however its quotes are written
        for cmd in ("echo $'hello there'", "printf '%s\\n' \"$(date)\"",
                    "gh pr list --json $'number'"):
            self.assert_allowed(cmd)

    def test_the_directory_named_across_TWO_WORDS_is_refused(self):  # noqa: VACUOUS_ASSERTION — the first line is an unconditional refusal control through assert_refused (rc 2 plus four required substrings) on the same hook entry, and every arm below asserts rc 2 or rc 0, the allow VALUE
        """A cd AND A RELATIVE REDIRECT write a workflow file while the text
        holds no `.github/workflows` anywhere: `cd .github && printf 'on:
        push' > workflows/evil.yml` performs the act (measured in a scratch
        tree — the file was created), and the rung's declared cost is
        over-refusal, never a real write admitted. The row is TWO PIECES, so
        the split that hid it is not a hiding place.

        THE LIMIT, stated rather than papered over: this rung reads ONE
        command's text, and the working directory is not in it. A `cd` in an
        EARLIER call, whose directory the next call inherits, leaves a
        relative `workflows/evil.yml` holding one piece of the row, and that
        is allowed — asserted below so the boundary is measured and not
        assumed."""
        self.assertIn(self.DIRECTORY, self.assert_refused(
            "touch %s/ci.yml" % self.WORKFLOWS))
        for cmd in ("cd .github && printf 'on: push' > workflows/evil.yml",
                    "cd .github; tee workflows/ci.yml < /dev/null",
                    "cd .github/workflows && tee ci.yml < /dev/null",
                    "(cd .github && mkdir -p workflows)"):
            self.assert_refused(cmd)
        for cmd in ("printf 'on: push' > workflows/evil.yml",
                    "cd src && cp a.yml workflows/b.yml",
                    "cd .github && cat ISSUE_TEMPLATE/bug.md",
                    "HELM_ALLOW_GITHUB_ACTIONS=1 cd .github && printf "
                    "'on: push' > workflows/evil.yml"):
            self.assert_allowed(cmd)

    # THE READS the ruling names — cat, ls, grep, git grep, git show, and a
    # sed -n, head or tail — each in the shape the fleet actually typed it
    # (the labelled suite's reads: a `cd` first, a `2>/dev/null`, a pipe
    # into `head`, an `echo ---` between two reads, the directory named
    # across two words)
    DIRECTORY_READS = (
        "cat {W}/ci.yml",
        "ls {W}",
        "ls -la ./{W}/ 2>/dev/null",
        "ls {W}/ 2>&1; echo ---; grep -rln rspec {W}/ 2>/dev/null",
        "grep -n 'rubocop' -B3 -A4 {W}/ci.yml | head -30",
        "grep -c on: < {W}/ci.yml",
        "rg -n rspec {W}",
        "/usr/bin/rg -n rspec {W}",
        "git grep -n rspec -- {W}",
        "git grep -n '{W}' -- docs/",
        "git show HEAD:{W}/ci.yml",
        "git -C /tmp/repo --no-pager show HEAD -- {W}/ci.yml | head -80",
        "sed -n '25,80p' {W}/ci.yml",
        "sed -n '/export-specs/,/^  [a-z]/p' {W}/ci.yml",
        "sed -n -e 1p -e '$p' {W}/ci.yml",
        "sed -nE '/^on:/,/^jobs:/p' {W}/ci.yml",
        "head -30 {W}/ci.yml",
        "tail -n 20 {W}/ci.yml",
        "cd /tmp/repo && sed -n 1,5p {W}/ci.yml; echo ---; tail -3 {W}/ci.yml",
        "cd .github && ls workflows")

    # THE WRITES, by the ruling's list — a shell redirect of both kinds, cp
    # and mv into the directory, git add of a path under it, tee, and an
    # editor — plus each way a READER's own spelling can write or run a
    # program, which is what an allowlist of verbs must not let through
    DIRECTORY_WRITES = (
        "cat x.yml > {W}/ci.yml",
        "cat x.yml >> {W}/ci.yml",
        "cat x.yml 1>{W}/ci.yml",
        "cat x.yml &> {W}/ci.yml",
        "cat x.yml >| {W}/ci.yml",
        "printf 'on: push' > {W}/evil.yml",
        "echo 'on: push' >> {W}/ci.yml",
        "sed -n 1p x.yml > {W}/ci.yml",
        "git show HEAD:x.yml > {W}/ci.yml",
        "cd {W} && cat ../../x.yml > ci.yml",
        "cp x.yml {W}/ci.yml",
        "mv x.yml {W}/ci.yml",
        "cd .github && cp ../x.yml workflows/",
        "git add {W}/ci.yml",
        "git -C /tmp/repo add {W}/ci.yml",
        "cat x.yml | tee {W}/ci.yml",
        "ls {W}; tee -a {W}/ci.yml < x.yml",
        "vi {W}/ci.yml",
        "nano {W}/ci.yml",
        "sed -i 's/a/b/' {W}/ci.yml",
        "sed -n -i 's/a/b/p' {W}/ci.yml",
        "sed -ni '1p' {W}/ci.yml",
        "sed -n 1p {W}/ci.yml -i",
        "sed --in-place=.bak -n 1p {W}/ci.yml",
        "sed -n '1w {W}/x.yml' ci.yml",
        "sed -n 's/a/b/w {W}/x.yml' ci.yml",
        "sed -n -f edit.sed {W}/ci.yml",
        "git -c core.pager='tee {W}/x.yml' show HEAD",
        "git grep --open-files-in-pager=vi on -- {W}",
        "git show --output={W}/x.yml HEAD",
        "rg --pre ./w.sh on {W}",
        "grep -l on {W}/*.yml | xargs sed -i s/a/b/",
        "cat > {W}/ci.yml <<'EOF'\non: push\nEOF",
        "cat x.yml > {W}/$NAME",
        "cat x.yml > \"{W}/$NAME\"",
        "cat \"it's\" x.yml > {W}/$NAME",
        "cat x.yml > {W}/$'ci.yml'",
        "cat x.yml \\' > {W}/ci.yml '",
        "cat x.yml >&{W}/ci.yml",
        "cat `tee {W}/x.yml`",
        "cat \"it's\" `tee {W}/x.yml`",
        "cat $(tee {W}/x.yml)",
        "cat <(tee {W}/x.yml < x.yml)",
        "ls {W}; `echo cp` x.yml {W}/ci.yml",
        "(cat x.yml > {W}/ci.yml)",
        "{{ cat x.yml; }} > {W}/ci.yml",
        "X=1 cat x.yml > {W}/ci.yml",
        "./cat x.yml {W}/ci.yml")

    def test_the_workflow_directory_may_be_READ_and_never_WRITTEN(self):  # noqa: VACUOUS_ASSERTION — the first line is an unconditional refusal control through assert_refused (rc 2 plus four required substrings), and every arm below asserts rc 2 or rc 0, the allow VALUE
        """READING THE DIRECTORY RUNS NOTHING (task/2973, the integrator's
        ruling). The rule is about WHERE CI RUNS, so a command whose only
        row is the workflow directory passes when every simple command in it
        is one of the named readers and nothing in it can write: `cat`,
        `ls`, `grep` (and `rg`), `git grep`, `git show`, and a `sed -n`,
        `head` or `tail` read — with `cd` and `echo` beside them, which
        write nothing. Writing into that directory and running Actions stay
        refused: every write arm below is refused, and so is a read standing
        beside an Actions act, because the exemption lifts the directory's
        row and no other.

        THE ONE REDIRECT A READ MAY CARRY IS INTO /dev/null (or onto another
        descriptor): a `cd` earlier in the same command can put the working
        directory INSIDE the workflow directory, so a redirect to any other
        target is a write whose destination the text does not settle."""
        self.assertIn(self.DIRECTORY, self.assert_refused(
            "touch %s/ci.yml" % self.WORKFLOWS))
        for cmd in self.DIRECTORY_READS:
            self.assert_allowed(cmd.format(W=self.WORKFLOWS))
        for cmd in self.DIRECTORY_WRITES:
            self.assert_refused(cmd.format(W=self.WORKFLOWS))
        # a READ beside any other row keeps that row's refusal: the
        # exemption lifts the directory's row and no other, whichever row
        # stands first and at any distance — the last two put the other row
        # AFTER the directory's, the second of them past the prose window
        for cmd in ("ls {W} && " + self.ENABLE + " ci.yml",
                    "cat {W}/ci.yml; gh api -X PUT repos/o/r/actions/permissions",
                    "ls {W}; echo " + self.VERB.split(" ")[0] + " "
                    + "word " * 14 + "list"):
            self.assert_refused(cmd.format(W=self.WORKFLOWS))
        # …and a grep PATTERN that names an act is data (task/2973), so a read
        # that searches for one is still only a read
        for cmd in ("grep -n '" + self.ENABLE + "' {W}/ci.yml",
                    "grep -n on: {W}/ci.yml; grep -rn '" + self.ENABLE
                    + "' docs/"):
            self.assert_allowed(cmd.format(W=self.WORKFLOWS))

    def test_the_repository_dispatch_trigger_is_refused(self):  # noqa: VACUOUS_ASSERTION — the first line is an unconditional refusal control through assert_refused (rc 2 plus four required substrings) on the same hook entry, and every arm below asserts rc 2 or rc 0, the allow VALUE
        """`repos/<o>/<r>/dispatches` is the documented door that starts a
        run from outside and spells no `/actions` segment — it fires
        wherever a workflow subscribes to `on: repository_dispatch`. It is
        the same class as the Actions API spelling the table had not learned,
        and it is closed the same way: one more row.

        The controls are the spelling that WAS covered (the workflow
        dispatch path, which carries `/actions`) and helm's own `dispatch`
        verb, which is not this row: the piece carries its leading slash."""
        self.assertIn("/actions", self.assert_refused(
            "gh api repos/a/b/actions/workflows/ci.yml/dispatches -f ref=main"))
        for cmd in ("gh api -X POST repos/o/r/dispatches -f event_type=deploy",
                    "curl -X POST https://api.github.com/repos/o/r/dispatches",
                    "gh api --method POST repos/o/r/dispatches"):
            out = self.assert_refused(cmd)
            self.assertIn("repository_dispatch", out)
        for cmd in ("helm dispatch verdict 12 TIP --approve",
                    "helm dispatch list --repo .",
                    "gh pr list",
                    "HELM_ALLOW_GITHUB_ACTIONS=1 gh api -X POST "
                    "repos/o/r/dispatches -f event_type=deploy"):
            self.assert_allowed(cmd)

    def test_gh_options_cannot_hide_the_verb_in_EITHER_gap(self):  # noqa: VACUOUS_ASSERTION — the first line is an unconditional refusal control through assert_refused (rc 2 plus four required substrings) on the same hook entry, and every allow arm asserts rc == 0, the allow VALUE
        """gh takes options in TWO places, and a reading that closed one gap
        left the other open.

        BEFORE THE NOUN is the documented global form (`gh -R o/r workflow
        enable`), and a pattern that allowed a bounded run of characters
        there admitted every spelling past the bound (measured).

        BETWEEN THE NOUN AND THE VERB is the same act again: gh strips flags
        before it resolves a subcommand, so `gh workflow --repo o/r enable
        ci.yml`, `gh workflow -R o/r enable 1234` and `gh workflow
        --repo=o/r enable ci.yml` all resolve to `gh workflow enable`
        (measured against gh itself: each prints the enable help). A table of
        adjacent PAIRS is a position assumption wearing a different hat, and
        it admitted all six spellings below. A ROW IS A SET OF PIECES now, so
        neither gap is a question this rung asks.

        The controls are what the row still costs: ONE piece of a row is not
        the row, so `gh pr list` beside an unrelated `run` or `workflow` word
        passes — and a command holding BOTH pieces does not, whatever stands
        between them, which the cost arm below pins."""
        # unconditional positive control: the row with NOTHING between the
        # pieces, which is the case every arm below varies the distance of
        self.assertIn(self.VERB, self.assert_refused(self.ENABLE))
        for cmd in ("gh -R o/r workflow enable ci.yml",
                    "gh --repo o/r workflow run ci.yml",
                    "gh -R o/r run rerun 123",
                    "gh --hostname github.com -R o/r workflow enable 42",
                    "gh pr list --json number,title,author,labels,state,"
                    "createdAt,updatedAt,url,body,closedAt,milestone; echo "
                    + self.VERB,
                    "gh --help; gh workflow run ci.yml",
                    # THE SECOND GAP, measured admitted by the pair table
                    "gh workflow --repo o/r enable ci.yml",
                    "gh workflow -R o/r enable 1234",
                    "gh workflow --repo=o/r enable ci.yml",
                    "gh workflow --repo o/r disable ci.yml",
                    "gh run --repo o/r rerun 123",
                    "gh run -R o/r watch 123"):
            self.assert_refused(cmd)
        for cmd in ("gh --help; gh pr list", "gh pr list; echo done",
                    "echo the run finished; echo the build is local",
                    "echo the workflow is local"):
            self.assert_allowed(cmd)

    def test_a_row_costs_BOTH_pieces_wherever_they_stand(self):  # noqa: VACUOUS_ASSERTION — every tuple arm executes an unconditional assertion, and the refusing and allowing arms are the two sides of one boundary
        """THE PRICE OF CLOSING THE SECOND GAP, asserted rather than only
        written down. A row whose pieces need not touch is present in prose
        that happens to use both words, so `echo the run finished; echo the
        workflow is local` — an ALLOW control before the pieces were a set —
        is refused now. That is the same accepted cost as a mention of any
        other spelling, and the same cure: the grant at the front.

        The boundary is asserted in both directions, because a rung that
        refused every command holding EITHER word would be a wall: one piece
        alone passes, and the pieces are whole pieces, so `rerun` is not the
        word `run` and `workflows` is not the word `workflow`."""
        for cmd in ("echo the run finished; echo the workflow is local",
                    "echo the workflow failed && echo the list is stale",
                    "git log --oneline | tee workflow ; echo run it again"):
            self.assert_refused(cmd)
        # …while the same words as a PATTERN are data (task/2973): rg reads
        # them and runs nothing
        self.assert_allowed("git log --oneline | rg 'workflow' ; echo 'run it'")
        for cmd in ("HELM_ALLOW_GITHUB_ACTIONS=1 echo the run finished; "
                    "echo the workflow is local",
                    "echo the run finished; echo the build is local",
                    "echo the workflow is local",
                    "echo rerun the fabric gate",
                    "cp x.md docs/workflows/notes.md",
                    "echo the runs are local"):
            self.assert_allowed(cmd)

    def test_a_command_holding_no_spelling_passes_however_it_is_quoted(self):  # noqa: VACUOUS_ASSERTION — every tuple arm executes an unconditional rc-0 assertion
        """The other direction of the superset, and the one that matters for
        a guard on EVERY Bash call: heavy quoting, heredocs, substitutions
        and continuations are not what this rung objects to. Only a
        protected spelling is — OR, since the scoped rule, a single piece
        standing beside an unresolved expansion, which is asserted at the
        end of this arm because it is the cost that reaches commands like
        these."""
        for cmd in ("echo hi",
                    "git commit -F msg.txt",
                    "printf '%s\\n' 'a `b` c' \"$(date)\"",
                    "cat <<'EOF'\nprintf '%s\\n' \"$(git rev-parse HEAD)\"\nEOF",
                    "cat <<OUT\n$(gh pr list)\nOUT",
                    "helm chat post --room r <<'EOF'\nthe fabric runs CI\nEOF",
                    "gh \\\n  pr \\\n  list",
                    "bash -c 'gh pr list; gh issue list'",
                    "sed -i.foo s/a/b/ notes.txt",
                    "cp notes.md docs/",
                    "rg -n 'right through the night' notes.md"):
            self.assert_allowed(cmd)
        # THE COST, in this arm's own idiom: an ANCHOR beside the same
        # substitution is refused. `gh workflow list` alone is a whole row
        # and refused anyway; `cat .github/CODEOWNERS` alone passes, and
        # with a hole in the line it is the anchor plus the rest of a row.
        # `gh release download v1` and `gh pr list` are the controls the
        # anchor bought back — `download` and `list` are ordinary English
        # words and no row's anchor, quoted or not.
        self.assertIn("beside an unresolved expansion",
                      self.assert_refused("cat <<OUT\n$(cat .github/"
                                          "CODEOWNERS)\nOUT"))
        self.assert_allowed("cat .github/CODEOWNERS")
        self.assert_allowed("cat <<OUT\n$(gh release download v1)\nOUT")
        self.assert_allowed("gh release download v1")
        self.assert_allowed("gh pr list")

    def test_the_grant_is_the_front_of_the_command_and_nowhere_else(self):  # noqa: VACUOUS_ASSERTION — every tuple arm executes an unconditional assertion in both directions
        """ONE WHOLE-COMMAND GRANT. The override word FIRST in the raw
        command — leading whitespace allowed, nothing else in front of it —
        grants that whole command, and the same word anywhere else grants
        nothing.

        WHY THE FRONT AND NOT ANYWHERE. A grant read loosely is a hole, and
        every loose reading this rung shipped grew one: a forged word
        (`XHELM_ALLOW_GITHUB_ACTIONS=1`, `…=1-fake`) that no shell assigns,
        a word buried mid-command lifting a compound whose other half the
        operator never looked at, and a receiver stage inheriting it through
        `bash -c`. The front of the command is the one position a reader and
        a writer agree on.

        THE REVOCATION GRAMMAR IS GONE WITH THE NESTING IT SERVED. An outer
        grant with an inner `=0` now ALLOWS: the whole command is granted,
        and there is no second scope for the reset to belong to. That is the
        design, asserted here so a later reader does not read it as a
        regression."""
        for cmd in ("HELM_ALLOW_GITHUB_ACTIONS=1 " + self.ENABLE + " ci.yml",
                    "  HELM_ALLOW_GITHUB_ACTIONS=1 " + self.ENABLE,
                    "HELM_ALLOW_GITHUB_ACTIONS=1 gh api -X PUT "
                    "repos/a/b/actions/permissions -F enabled=true",
                    "HELM_ALLOW_GITHUB_ACTIONS=1 tee %s/ci.yml "
                    "<<'EOF'\nx\nEOF" % self.WORKFLOWS,
                    "HELM_ALLOW_GITHUB_ACTIONS=1 bash -c '" + self.ENABLE + "'",
                    # the whole command, including a second act after a
                    # break: granted at the front is granted whole
                    "HELM_ALLOW_GITHUB_ACTIONS=1 true; " + self.ENABLE,
                    # an outer grant with an inner reset: ALLOWED by design
                    "HELM_ALLOW_GITHUB_ACTIONS=1 bash -c "
                    "'HELM_ALLOW_GITHUB_ACTIONS=0 " + self.ENABLE + "'"):
            self.assert_allowed(cmd)
        # CONTROLS: a word anywhere but the front grants nothing, and a
        # forged word is not the word
        for cmd in ("cd /tmp/repo && HELM_ALLOW_GITHUB_ACTIONS=1 "
                    + self.ENABLE,
                    "env HELM_ALLOW_GITHUB_ACTIONS=1 " + self.ENABLE,
                    "true;HELM_ALLOW_GITHUB_ACTIONS=1 " + self.ENABLE,
                    'HELM_ALLOW_GITHUB_ACTIONS="1" ' + self.ENABLE,
                    "XHELM_ALLOW_GITHUB_ACTIONS=1 " + self.ENABLE,
                    "HELM_ALLOW_GITHUB_ACTIONS=1-fake " + self.ENABLE,
                    "MY_HELM_ALLOW_GITHUB_ACTIONS=1 " + self.ENABLE,
                    "--HELM_ALLOW_GITHUB_ACTIONS=1 " + self.ENABLE,
                    "HELM_ALLOW_GITHUB_ACTIONS=1x " + self.ENABLE,
                    "HELM_ALLOW_GITHUB_ACTIONS=0 " + self.ENABLE,
                    "HELM_ALLOW_GITHUB_ACTIONS= " + self.ENABLE,
                    "HELM_ALLOW_GITHUB_ACTIONS=11 " + self.ENABLE,
                    self.ENABLE + " ci.yml"):
            self.assert_refused(cmd)

    def test_the_ambient_environment_never_counts(self):  # noqa: VACUOUS_ASSERTION — assert_refused runs first and unconditionally on the same hook output (rc 2 plus four required substrings), so an empty observable fails there before any allow control is reached
        """The override is per act, so it is read from the command text and
        never from the process environment: a seat that exported the word
        once would otherwise be unguarded for the rest of its life."""
        with mock.patch.dict(_os.environ, {"HELM_ALLOW_GITHUB_ACTIONS": "1"}):
            out = self.assert_refused(self.ENABLE + " ci.yml")
            self.assertIn("nor does the environment", out)
            # control: the same environment plus the word at the front
            self.assert_allowed(
                "HELM_ALLOW_GITHUB_ACTIONS=1 " + self.ENABLE + " ci.yml")

    def test_prose_about_the_act_is_data_and_the_act_itself_is_refused(self):  # noqa: VACUOUS_ASSERTION — every allow arm stands beside refusals of the same words where a shell runs them, all unconditional on the same hook entry
        """task/2973, THE INTEGRATOR'S RULING. A note, a post, a comment, a
        commit message or a search that MENTIONS the act is data: the words
        are an argument or a stdin that a program records, prints or searches,
        and none of those programs runs them. Read as the act, they were 141
        of meta-claude's 212 labelled refusals, and none of the 212 ran CI.

        THE SAME WORDS WHERE A SHELL RUNS THEM are refused: the note fed to
        `bash`, the post piped into one, the act spelled bare, and a message
        git hands to an EDITOR (`-e`), which is a program of its own. The
        grant still sends each. A substitution inside a post is refused too,
        because the shell runs it before helm sees a word."""
        note = ("cat > notes.md <<'EOF'\nnever run " + self.ENABLE
                + "; write no %s/ file\nEOF" % self.WORKFLOWS)
        post = "helm chat post --room r 'do not " + self.ENABLE + "'"
        for cmd in (note, post,
                    "git grep -n '%s' -- docs/" % self.ENABLE,
                    "git commit -m 'never %s here'" % self.ENABLE,
                    "echo 'never %s here'" % self.ENABLE,
                    "helm task comment 12 'we never %s'" % self.ENABLE):
            self.assert_allowed(cmd)
        for cmd in (note.replace("cat > notes.md", "bash"),
                    post + " | bash",
                    "echo never " + self.ENABLE,
                    "git commit -e -m 'never %s here'" % self.ENABLE):
            self.assertIn("at character", self.assert_refused(cmd))
            self.assert_allowed("HELM_ALLOW_GITHUB_ACTIONS=1 " + cmd)
        self.assert_refused("helm chat post --room r \"see $(%s)\""
                            % self.ENABLE)
        # a message that names no spelling needs nothing
        self.assert_allowed("git commit -F msg.txt")

    # the two ANCHORLESS rows the document rule turns on, built at runtime
    # for the same reason every other spelling in this file is: a seat that
    # greps this file inside a Bash command must not be refused by the rung
    # the file measures
    RUN_NOUN = "run"
    RUN_VERB = "can" + "cel"
    RUN_ACT = "gh %s %s 123" % (RUN_NOUN, RUN_VERB)

    def test_a_DOCUMENT_a_shell_never_runs_is_not_the_command(self):  # noqa: VACUOUS_ASSERTION — every arm asserts rc 0 or rc 2 unconditionally on the same shipped hook entry, and a refusing control stands beside each allow, so a rung that answered nothing fails on the control
        """task/2870, MEASURED three times in one morning by the seat that
        filed it. A quoted-tag heredoc BODY is a span of a command that a
        shell neither runs nor hands over as an argument: it copies those
        bytes, with no expansion of any kind, onto a program's stdin. The
        rung refused two of them —

        ONE, a test file written through `cat > x.py <<'PY'`, whose python
        source calls a helper on an argv list whose first element is the
        verb, so the helper name and the verb sit adjacent in the folded
        whole. Nothing on that line is a shell command; it is payload bound
        for a file. The PATCH SCRIPT written to work around that refusal was
        then refused by the same rule, which is the tell.

        TWO, a `helm dispatch send` whose brief said the noun in one
        sentence and the verb in another.

        The controls are the whole point of the rule's shape: every act in a
        body a SHELL reads is refused, an UNQUOTED body still substitutes and
        is read, and a row on the command line beside a document is
        untouched. The same anchored acts written through `cat` are data
        (task/2973): cat reads its stdin and runs none of it."""
        helper = ("cat > tests/t.py <<'PY'\n"
                  "    def test_one(self):\n"
                  "        self.assertEqual(%s([\"%s\", \"--task\", \"1\"]), 0)\n"
                  "PY\n" % (self.RUN_NOUN, self.RUN_VERB))
        brief = ("helm dispatch send --to lane <<'MSG'\n"
                 "based on current trunk, %s the gate before you land\n"
                 "and %s the stale row once it is green\n"
                 "MSG\n" % (self.RUN_NOUN, self.RUN_VERB))
        self.assert_allowed(helper)
        self.assert_allowed(brief)
        # …and the patch script that was written to work around the first
        self.assert_allowed("python3 - <<'PY'\nopen('t.py').read()\n"
                            "# the %s %s argv the helper takes\nPY\n"
                            % (self.RUN_NOUN, self.RUN_VERB))
        act = self.ENABLE + " ci.yml"
        for cmd in (
                # an act in a body a shell reads
                "bash <<'MSG'\n" + act + "\nMSG",
                "bash <<'EOF'\n" + self.RERUN + " 7\nEOF",
                "cat <<'EOF' | bash\ngh api -X POST repos/o/r/actions/runs/1"
                "/rerun\nEOF",
                "bash <<'EOF'\n" + self.RUN_ACT + "\nEOF",
                # an UNQUOTED tag: the body substitutes, so it is still read
                "bash <<EOF\n" + self.RUN_ACT + "\nEOF",
                # the same words on the COMMAND LINE beside a document
                "cat <<'EOF'\nhello\nEOF\n" + self.RUN_ACT,
                # …and with no document anywhere
                self.RUN_ACT):
            self.assert_refused(cmd)
        # the same anchored acts written INTO a file or printed: data
        for cmd in ("cat > tests/t.py <<'PY'\n" + act + "\nPY",
                    "cat <<'EOF'\n" + self.RERUN + " 7\nEOF",
                    "cat <<'EOF'\ngh api repos/o/r/actions/runs/1\nEOF",
                    "cat <<'EOF'\ntouch %s/ci.yml\nEOF" % self.WORKFLOWS):
            self.assert_allowed(cmd)

    def test_the_document_rule_gives_up_exactly_three_spellings(self):  # noqa: VACUOUS_ASSERTION — each loop arm asserts rc 0 for the named residual and rc 2 for each of its twins, all unconditional on the same hook entry
        """WHAT THE CURE CANNOT SEE, asserted so it cannot be forgotten and
        cannot quietly grow. An ANCHORLESS row — the run noun with `cancel`,
        `watch` or `download`, and no other spelling in the table — written
        inside a QUOTED heredoc whose program this rung does not know passes.
        Those three rows are two ordinary English words each; the rung's own
        corpus holds the noun in one command of every twelve.

        A SHELL'S BODY IS NOT IN THAT RESIDUAL (task/2973): `bash <<'EOF'`
        and `cat <<'EOF' | sh` run every line of the body, so the same three
        rows there are refused. Every arm pairs the residual with its refused
        twins — the anchored act in the identical shape, and the same row
        under a shell — so a reader sees the boundary rather than a list of
        holes, and a later change that widens the carve-out goes red here."""
        box = "docker exec -i box sh <<'EOF'\n%s\nEOF"
        for verb in (self.RUN_VERB, "watch", "download"):
            row = "gh %s %s 123" % (self.RUN_NOUN, verb)
            self.assert_allowed(box % row)
            self.assert_refused(box % (self.ENABLE + " ci.yml"))
            self.assert_refused("bash <<'EOF'\n%s\nEOF" % row)
            self.assert_refused("cat <<'EOF' | sh\n%s\nEOF" % row)
        # the anchored verbs of the same family are NOT given up
        self.assert_refused(box % (self.RERUN + " 123"))
        self.assert_refused(box % ("gh workflow %s ci.yml" % self.RUN_NOUN))

    # THE REPORTED FALSE REFUSAL, in the shape the seat that hit it wrote: a
    # chat post whose quoted document says `rerun` as prose, then a separate
    # command that echoes the exit status. The prose holds no run noun of its
    # own, so no row stands anywhere and only the scoped rule can answer.
    POSTED = ("helm chat post --room R <<'EOF'\n"
              "landed the fix; please rerun the gate on your lane\n"
              "EOF\n"
              'echo "posted rc=$?"')

    def test_an_ANCHOR_in_a_DOCUMENT_is_not_beside_an_expansion_OUTSIDE_it(self):  # noqa: VACUOUS_ASSERTION — the reported shape's refusing twin (the same document read by a shell, with the expansion moved INTO it) runs assert_refused unconditionally on the same hook entry, and every allow arm asserts rc 0, the allow VALUE
        """AN ANCHOR AND AN EXPANSION ARE A ROW ONLY IN ONE REGION. The
        scoped rule took its anchor from anywhere in the folded command and
        its expansion from anywhere in the raw one, so the reported post was
        refused: `rerun` stood in a quoted document, `$?` stood in a later
        command, and the rung called them beside each other. They cannot be.
        The outer shell never expands a quoted-tag body, so an expansion on
        another line cannot supply a word inside it; an expansion inside one
        is expanded only by a program that executes that body, and then only
        beside what that body and its opener line hold (the next arm).

        So a REGION is the executed text (the command minus its quoted
        bodies) or ONE quoted body with its opener line, and every allow
        here is an anchor in one region and an expansion in another. The
        refusing twin is the same document read by a SHELL with the
        expansion moved inside it. The post's own body with the expansion in
        it passes: a recording program's body is data (task/2973), and bash
        expands nothing in a quoted one."""
        self.assert_allowed(self.POSTED)
        with_hole = self.POSTED.replace("please rerun", "please rerun $V")
        self.assert_allowed(with_hole)
        self.assert_refused(with_hole.replace("helm chat post --room R",
                                              "bash"))
        # an anchor OUTSIDE, an expansion only inside a quoted body: the body
        # never expands for the outer shell, and it holds no anchor of its own
        for cmd in ("git add .github\nhelm chat post --room R <<'EOF'\n"
                    "rc was $?\nEOF",
                    "echo the workflow is local\ncat <<'EOF'\n$V\nEOF"):
            self.assert_allowed(cmd)
        # an anchor in ONE quoted body, an expansion in ANOTHER — on two
        # lines, and opened on one line, which the shell reads in order
        for cmd in ("cat <<'A'\ngh workflow ci.yml\nA\ncat <<'B'\necho $V\nB",
                    "cat <<'A' - 3<<'B'\ngh workflow ci.yml\nA\necho $V\nB",
                    "node - <<'A'\ngh workflow ci.yml\nA\nnode - <<'B'\n"
                    "echo $V\nB"):
            self.assert_allowed(cmd)
        # an expansion in an UNQUOTED body is executed text, and executed
        # text is still outside the quoted body that holds the anchor
        self.assert_allowed(
            "node - <<'A'\nplease rerun it\nA\ncat <<EOF\n$V\nEOF")

    # a body that names the noun and takes the verb from its environment
    FROM_ENV = ("node - <<'EOF'\n"
                "const {execFileSync} = require('child_process')\n"
                "execFileSync('gh', ['workflow', process.env.V])\nEOF")

    def test_the_OPENER_LINE_belongs_to_every_body_it_opens(self):  # noqa: VACUOUS_ASSERTION — every refusing arm runs assert_refused unconditionally (rc 2 plus four required substrings) on the same hook entry, and each allow arm is a refused body with its expansion moved off the opener line
        """THE PROGRAM THAT CONSUMES A QUOTED BODY STANDS ON ITS OPENER LINE,
        and that line's words reach it: as its ARGV (`node - "$V" <<'EOF'`
        reading `process.argv[2]`, `bash -s workflow <<'EOF'` reading `$1`)
        and as its ENVIRONMENT PREFIX (`V=$X node - <<'EOF'` reading
        `process.env`). So the opener line belongs to the region of every
        body it opens — both bodies when one line opens two — and it is read
        whole, so a `$?` standing before a `;` on it counts too.

        The consumer here is a program this rung does not know (node), whose
        body is read. A `python3 -` body is DATA by the task/2973 ruling and
        is not read at all — asserted at the end, beside the node body it
        otherwise matches.

        A DIFFERENT line reaches the body through no such door, which is
        what keeps the reported post passing: its `$?` is on a later line.
        What that gives up is asserted last, so it cannot quietly grow: a
        body that reads what an EARLIER line exported passes."""
        argv = ("node - \"$V\" <<'EOF'\n"
                "const {execFileSync} = require('child_process')\n"
                "execFileSync('gh', ['workflow', process.argv[2]])\nEOF")
        for cmd in (argv,
                    "V=$X " + self.FROM_ENV,
                    # a continuation makes the line above part of the opener
                    "V=$X \\\n" + self.FROM_ENV,
                    # a word the opener hands the body as argv is the NOUN
                    "bash -s workflow <<'EOF'\ngh $1 ci.yml\nEOF",
                    # read whole: the status before a `;` joins the body too
                    "echo $? ; " + self.FROM_ENV,
                    # the body asking the SHELL for the value
                    "bash -s \"$V\" <<'EOF'\ngh workflow \"$1\" ci.yml\nEOF"):
            with self.subTest(cmd=cmd):
                self.assertIn("beside an unresolved expansion",
                              self.assert_refused(cmd))
        # ONE LINE OPENING TWO DOCUMENTS joins its expansion to BOTH
        two = "node - \"$V\" <<'A' 3<<'B'\n%s\nA\n%s\nB"
        noun = "gh workflow ci.yml"
        for cmd in (two % (noun, "hello"), two % ("hello", noun)):
            with self.subTest(cmd=cmd):
                self.assertIn("beside an unresolved expansion",
                              self.assert_refused(cmd))
        # …and the same two documents with the expansion on a later line
        self.assert_allowed(two.replace(" \"$V\"", "") % (noun, "hello")
                            + "\necho $V")
        # a DIFFERENT line from the opener: the reported post, a later
        # expansion, and an earlier one that does not continue onto it
        self.assert_allowed(self.POSTED)
        self.assert_allowed(self.FROM_ENV + "\necho $V")
        self.assert_allowed("echo $V\n" + self.FROM_ENV)
        # WHAT IT GIVES UP: a value an EARLIER line exported for the body
        self.assert_allowed("export V=$X\n" + self.FROM_ENV)
        # a python body is data (task/2973), whatever its opener line holds
        self.assert_allowed(argv.replace("node -", "python3 -"))

    def test_an_ANCHOR_and_an_EXPANSION_in_ONE_REGION_are_still_a_row(self):  # noqa: VACUOUS_ASSERTION — every arm runs assert_refused unconditionally (rc 2 plus four required substrings) on the same hook entry, and each names the region it stands in
        """WHAT THE REGION RULE MUST NOT GIVE BACK. The anchor stays evidence
        inside a quoted document (a program such as `bash <<'EOF'` or
        `python3 - <<'EOF'` executes that body), so a body holding BOTH an
        anchor and an expansion is still a row; the command line beside a
        document is its own region and is read exactly as a command with no
        document is; and an UNQUOTED body substitutes, so it belongs to the
        executed text and never to a region of its own.

        The refusal names the anchor of the region that decided — never an
        earlier anchor standing inert in a document."""
        document = ("helm chat post --room R <<'EOF'\n"
                    "please rerun the gate\nEOF\n")
        # BOTH OUTSIDE, with a quoted document in the same command
        for cmd in (document + "gh workflow $V ci.yml",
                    document + "gh $W rerun 123",
                    "gh workflow $V ci.yml <<'EOF'\nhello\nEOF"):
            self.assertIn("beside an unresolved expansion",
                          self.assert_refused(cmd))
        self.assertIn("workflow at character", self.assert_refused(
            document + "gh workflow $V ci.yml"))
        # a region piece the fold of the WHOLE does not hold — the cut body
        # sat between the two halves of a continuation — still writes its
        # refusal; a position lookup that raised here would fail open
        self.assertIn("workflow at character", self.assert_refused(
            "cat <<'EOF' work\\\nEOF\nflow $V\necho .github"))
        # BOTH INSIDE ONE quoted body that a program executes
        for cmd in ("bash <<'EOF'\ngh workflow $V ci.yml\nEOF",
                    "bash <<'EOF'\ngh $W rerun 123\nEOF",
                    "echo hi\nbash <<'EOF'\ngit add .github/$NAME\nEOF",
                    "cat <<'A'\nhello\nA\nbash <<'B'\ngh workflow $V\nB"):
            self.assertIn("beside an unresolved expansion",
                          self.assert_refused(cmd))
        # an UNQUOTED body is the executed text, alone, beside the line, and
        # beside a quoted document that splits the command into regions
        for cmd in ("cat <<EOF\ngh workflow $V\nEOF",
                    "echo the workflow is local; cat <<EOF\n$V\nEOF",
                    "cat <<EOF\n$V\nEOF\necho the workflow is local",
                    "cat <<'A'\nhello\nA\ncat <<EOF\ngh workflow $V\nEOF",
                    "echo the workflow is local; cat <<'A'\nhello\nA\n"
                    "cat <<EOF\n$V\nEOF"):
            self.assertIn("beside an unresolved expansion",
                          self.assert_refused(cmd))

    def test_a_piece_is_a_WHOLE_WORD_and_never_part_of_a_longer_one(self):  # noqa: VACUOUS_ASSERTION — each allow arm stands beside the refusal of the same row spelled as whole words in the same shape, so a rung that answered nothing fails on the refusing half
        """THE FILING SAID THE NOUN MATCHED INSIDE `trunk`. It does not, and
        this arm is why the report was wrong rather than the rung: every
        piece carries the edge that stops it running on into a longer word,
        so `trunk`, `rerun`, `runs` and `runner` supply no run noun, and
        `workflows` supplies no `workflow`. The number in the refusal counts
        in the FOLDED command, the author counted it in the command they
        typed, and the word they landed on was innocent — which is the
        diagnosis cost the document clause of `_row_refusal` exists to
        remove.

        THIS ARM IS GREEN ON BOTH SIDES OF THE CURE and that is its value:
        the edges shipped five days before the filing, so the report was a
        mis-derived offset and not a defect, and nothing in this round
        touched them. It is here so a later round that reaches for a looser
        matcher pays for it at the moment it does.

        The refusing half of each pair is the control: the same row spelled
        as whole words in the same command shape IS refused, so an arm that
        passed because the rung answered nothing fails here. A trailing
        letter defeats the edge from the other side too, which is why the
        control spells the verb as its own word — `%sled` is not the piece
        either, and an arm whose CONTROL cannot refuse measures nothing."""
        line = "echo the %s finished; %s the stale row"
        for innocent in ("trunk", "rerun", "runs", "runner", "prerun"):
            self.assert_allowed(line % (innocent, self.RUN_VERB))
        self.assert_refused(line % (self.RUN_NOUN, self.RUN_VERB))
        # the verb carries the same edge behind it, measured the same way
        self.assert_allowed("echo the %s finished; %sled the stale row"
                            % (self.RUN_NOUN, self.RUN_VERB))
        for innocent in ("workflows", "reworkflow"):
            self.assert_allowed("echo the %s enable step" % innocent)
        self.assert_refused("echo the workflow enable step")

    # the re-execute row, and a Python raw-string regex holding a backtick
    # pair: the SHAPE of both commands a seat filed in task/2855
    RERUN_ROW = "run re" + "run"
    TICK_REGEXES = ('cited = re.findall(r"`+([^`\\n]{8,})`+", f)',
                    'bt = re.compile(r"`+([^`\\n]{3,}|d)")')

    def test_ONE_SPAN_is_never_TWO_PIECES_of_a_row(self):  # noqa: VACUOUS_ASSERTION — the first loop asserts POSITIVELY, with the shipped matchers, that both pieces stand in the witness's readings before it asserts the row does not, and every hook arm runs assert_refused unconditionally (rc 2 plus four required substrings) before any absence is read
        """task/2855: a Python file written through `cat > x.py <<'PY'` was
        refused as the re-execute row TWICE in one session. It names neither
        word. The fold reads its raw-string regex `r"`+([^`\\n]…` as shell:
        the backtick pair is an expansion, the deleted quote leaves the
        raw-string `r` in front of it, and the mark then stands inside the
        word `r<mark>n` (the `n` of `\\n`, its backslash deleted) and, in
        the escape-decoded reading, `r<mark>`. The mark may stand for a run
        of a piece's own characters, so ONE three-character span held BOTH
        pieces, `run` (the mark as `u`) and `rerun` (the mark as `eru`).

        A SPAN THAT READS AS TWO PIECES OF ONE ROW IS NEITHER OF THEM. Only
        asking that the chosen hits not OVERLAP is not the cure and was
        measured not to be: the readings are joined, so the one source word
        stands once in each of two readings, and `run` from the first beside
        `rerun` from the second are two disjoint spans of one ambiguous
        word. This witness is that shape, so the overlap-only cure goes red
        here too.

        WHAT IS NOT CURED, and the arm pins it rather than hiding it: the
        same span still holds the ANCHOR `rerun` beside an unresolved
        expansion in one region, so the text is still refused where a shell
        reads it — by the scoped rule, which names the anchor and not a row
        the text never held. Written to a FILE through `cat`, the body is
        data (task/2973) and passes. Taking the anchor away too needs the mark to demand more
        than one typed letter, and that was measured and not built: it
        admits `gh run r$(echo erun) 1` and six more real invocations.

        THE ACTS SPELLED AROUND THE MARK ARE ALL STILL REFUSED, which is what
        this rule may never give up: the row where its pieces stand apart,
        and the scoped rule where one expansion supplies both."""
        row = next(p for spelling, p, _says in chat._ACTIONS_ROWS
                   if spelling == self.RERUN_ROW)
        for regex in self.TICK_REGEXES:
            written = "cat > $SP/t.py <<'PY'\nimport re\n%s\nPY" % regex
            self.assert_allowed(written)
            witness = written.replace("cat > $SP/t.py", "bash")
            readings = chat._readings(witness)
            self.assertEqual(
                [bool(matcher.search(readings)) for _t, matcher, _a in row]
                + [chat._row_stands(row, readings)],
                [True, True, False], readings)
            out = self.assert_refused(witness)
            self.assertIn("re" + "run at character", out)
            self.assertIn("beside an unresolved expansion", out)
            self.assertNotIn(self.RERUN_ROW + " at character", out)
        # the MUST-HITS, each still named by its row
        for act, named in (("gh %s 123" % self.RERUN_ROW, self.RERUN_ROW),
                           ("gh workflow run ci.yml", "workflow run"),
                           ("gh workflow enable x", self.VERB),
                           ("bash <<'EOF'\ngh workflow run ci.yml\nEOF",
                            "workflow run")):
            self.assertIn(named + " at character", self.assert_refused(act))
        # …and the act spelled AROUND the mark, where one expansion supplies
        # both pieces or the span is shared, refused by the scoped rule
        for act in ("gh run r$(echo e%s) 1" % self.RUN_NOUN,
                    "gh run r$(echo eru)n 1",
                    "gh r$(echo 'un rer')un 1",
                    'gh run r"$(echo erun)" 1',
                    "gh run re$(echo %s) 1" % self.RUN_NOUN):
            self.assert_refused(act)

    class _Span:
        """A match stand-in that counts every read of its bounds."""
        reads = 0

        def __init__(self, start, end):
            self._s, self._e = start, end

        def start(self):
            type(self).reads += 1
            return self._s

        def end(self):
            type(self).reads += 1
            return self._e

    def test_the_overlap_filter_agrees_with_the_pairwise_definition(self):
        """`_clear_of` keeps a hit exactly when NO other hit shares a
        character with it. Checked against the definition itself over many
        placements of short spans on a short line, touching edges included:
        `[0,3)` and `[3,6)` share nothing."""
        spans = [(s, s + n) for s in range(8) for n in (1, 2, 3, 5)]
        for i, mine in enumerate(spans):
            others = spans[i::7] + spans[(i * 3) % 11::13]
            others = [o for o in others if o != mine] + [(mine[1], mine[1] + 2)]
            want = [mine] if not any(mine[0] < o[1] and o[0] < mine[1]
                                     for o in others) else []
            got = chat._clear_of([self._Span(*mine)],
                                 [self._Span(*o) for o in others])
            self.assertEqual([(g._s, g._e) for g in got], want, (mine, others))

    def test_the_overlap_filter_is_not_pairwise(self):
        """A cross-model read of task/2855: the filter compared
        every hit against every other piece's hit, and it runs in the
        PreToolUse hook, whose 2s deadline fails OPEN. 6,400 repeats of a
        marked `r$(x)n` took 2.16s through the installed hook, so a guarded
        act trailing that padding would be admitted by the timeout.

        A WALL-CLOCK ARM IS THE WRONG ARM here (see HookEntryBudgetTest: the
        hub's load swings the same code 40x), so this counts bound reads,
        which load cannot move: pairwise reads grow with N squared, the
        sweep with N. At N = 2,000 the pairwise filter reads about 16
        million bounds; the arm allows 40 per hit."""
        n = 2000
        mine = [self._Span(3 * k, 3 * k + 2) for k in range(n)]
        others = [self._Span(3 * k + 2, 3 * k + 3) for k in range(n)]
        self._Span.reads = 0
        kept = chat._clear_of(mine, others)
        self.assertEqual(len(kept), n)
        self.assertLess(self._Span.reads, 40 * 2 * n)

    def test_the_2855_padding_is_still_refused_at_scale(self):  # noqa: VACUOUS_ASSERTION — assert_refused asserts rc 2 plus its required substrings unconditionally
        """The shape a probe timed, with the real act after it: the
        padding must not change the verdict on what trails it."""
        act = "gh %s 1" % self.RERUN_ROW
        self.assert_refused("echo " + " ".join(["r$(x)n"] * 6400) + " ; " + act)

    def test_the_task_2855_filings_each_get_the_answer_the_design_owes(self):  # noqa: VACUOUS_ASSERTION — every allow arm stands beside a refusal of the same words where a shell runs them, both unconditional on the same hook entry
        """The six false refusals a seat filed, and the integrator's
        minimal case, each reduced to its shape, with what the rung answers
        NOW. Two are the span defect above. One was cured by the document
        rule. The rest are cured by task/2973: the filing's own proposal —
        count the Actions words only where a shell RUNS them — is the
        integrator's ruling, and the invocation reader builds it.

        WHAT THAT GIVES UP, asserted elsewhere in this class and named here:
        a python body that builds a gh Actions argv in its own source is data
        and passes, and so does a script written by `cat` and run by path in
        a later command. `echo '<act>' | bash`, `find -exec gh`, `parallel
        gh`, a wrapper script handed the words as argv, and a make variable
        are all still refused, because each hands the words to a program
        that runs them or spells them unquoted."""
        run_tail = "then %s the controlled A/B." % self.RUN_NOUN
        # (3) an anchorless row in a quoted body a shell does not run
        self.assert_allowed(
            "cat > $SP/chain.sh <<'EOS'\n#!/bin/bash\n# Wait for the "
            "download to finish, %s\nsleep 1\nEOS" % run_tail)
        # (4) both words as prose in a body a PYTHON interpreter is handed
        doc = ("python3 - <<'PY'\nnew = '''a narrow loss would need the Q5 "
               "re%s; it does not %s here'''\nPY"
               % (self.RUN_NOUN, self.RUN_NOUN))
        self.assert_allowed(doc)
        self.assertIn(self.RERUN_ROW + " at character", self.assert_refused(
            doc.replace("python3 -", "bash")))
        # (5) a read-only search whose pattern is the rung's own wording
        search = 'git grep -n -E "runs a workflow|workflow list" -- helm/'
        self.assert_allowed(search)
        self.assert_refused(search.replace("git grep -n -E", "eval"))
        # (6) a task comment whose ARGUMENT quotes the rows it reports
        self.assert_allowed(
            "helm task comment task/2855 \"refused as 'workflow run' and "
            "'workflow list'\"")
        # the integrator's minimal case: the two words alone in a document
        body = "<<'EOF'\n%s\nEOF" % self.RERUN_ROW
        self.assert_allowed("cat " + body)
        self.assert_allowed("helm chat post --room R " + body)
        self.assert_refused("sh " + body)
        for act in ("echo 'gh %s 1' | bash" % self.RERUN_ROW,
                    "find . -name x -exec gh %s 1 \\;" % self.RERUN_ROW,
                    "parallel gh %s ::: 1 2" % self.RERUN_ROW,
                    "make CMD='gh %s 1'" % self.RERUN_ROW):
            self.assert_refused(act)

    def test_the_refusal_says_WHERE_a_document_match_landed(self):  # noqa: VACUOUS_ASSERTION — the absence assertion's observable is the SAME hook output that assert_refused has already read unconditionally and positively on the line above it (rc 2 plus four required substrings), so an empty or silent refusal fails there before the assertNotIn is reached, and the presence half of the same clause is asserted on the document render three lines up
        """A CHARACTER NUMBER ALONE WAS DIAGNOSED WRONG (task/2870): the
        offset counts in the folded command — continuations joined, quotes
        and backslashes deleted, whitespace collapsed, readings joined — and
        the author never typed that text, so the filing counted it in the
        raw command and blamed an innocent word. The one thing a reader
        cannot derive is whether the bytes the rung objected to are a
        command or a DOCUMENT, and the refusal now says so.

        IT SAYS IT IN TWENTY CHARACTERS, and the arm pins that too: this
        line's length is budgeted (`PreToolUse/refusal-ci-runner`), quoting
        the folded text around the match does not fit, and raising the
        number is what that pin exists to refuse. So the clause is asserted
        BESIDE the budget, because a later round that makes it say more must
        see the ceiling it is spending."""
        act = self.ENABLE + " ci.yml"
        inside = self.assert_refused("bash <<'EOF'\n" + act + "\nEOF")
        self.assertIn("heredoc body", inside)
        # the MUST-MISS: the same act on the command line says nothing of
        # the kind, so the clause is measured and not a constant
        outside = self.assert_refused(act)
        self.assertNotIn("heredoc body", outside)
        # …and the document render stays inside the pinned refusal budget
        document = self.assert_refused(
            "bash <<'EOF'\ngh workflow %s ci.yml\nEOF" % self.RUN_NOUN)
        line = [l for l in document.splitlines() if "BLOCKED" in l][0]
        self.assertLessEqual(len(line), 470, line)

    # A PROSE SINK'S BODY holding a COMPLETE anchored row — the noun beside
    # its verb, in English — and the four sinks as their opener lines take
    # them. Built at runtime, like every other spelling in this file.
    SINK_BODY = "we never %s ci.yml here; the fabric runs CI" % ENABLE
    SINKS = ("helm chat post --room R",
             "helm dispatch send --to lane --kind review",
             "helm asks add --needs x",
             "git commit -q -F -")

    def doc(self, line, body=None, tag="EOF"):
        """`line`, which opens the heredoc, then its body and terminator."""
        return "%s\n%s\n%s" % (line, self.SINK_BODY if body is None else body,
                               tag)

    def test_a_PROSE_SINK_takes_a_quoted_body_the_row_check_does_not_read(self):  # noqa: VACUOUS_ASSERTION — the refusing half runs assert_refused unconditionally (rc 2 plus four required substrings) on the same hook entry and the same body, so a rung that answered nothing fails there
        """A QUOTED BODY WHOSE PROGRAM READS IT AS DATA IS NOT READ. `helm
        chat post`, `helm dispatch send`, `helm asks add` and `git commit -F
        -` post, send, record or commit their stdin and run none of it, and
        by the task/2973 ruling so do `cat`, `python` and `grep`; a
        complete row there is a sentence about an act. It was refused as
        one, while the Write tool wrote the same bytes.

        The refusing half is the point of the shape: the SAME body is
        refused under every consumer that runs it — a shell reading stdin,
        and a data program piped into one — and on the command line, where
        it is the act itself."""
        for line in [s + " <<'EOF'" for s in self.SINKS] + [
                "bin/helm chat post <<'EOF'",
                "./bin/helm chat post <<'EOF'",
                "/home/u/.local/bin/helm chat post --room R <<\"EOF\"",
                "python3 - <<'EOF'", "cat > f <<'EOF'"]:
            with self.subTest(admitted=line):
                self.assert_allowed(self.doc(line))
        for line in ("bash <<'EOF'", "sh -s <<'EOF'", "cat <<'EOF' | bash",
                     "ssh box <<'EOF'"):
            with self.subTest(refused=line):
                self.assertIn("(in a heredoc body)",
                              self.assert_refused(self.doc(line)))
        self.assert_refused(self.SINK_BODY)
        # the two doors agree about the bytes: the Write tool takes them
        rc, out = self.hook("Write", file_path="notes/reply.md",
                            content=self.SINK_BODY)
        self.assertEqual(rc, 0, out)

    def test_a_SINK_whose_opener_line_does_more_is_read_as_before(self):  # noqa: VACUOUS_ASSERTION — every refusing arm runs assert_refused unconditionally (rc 2 plus four required substrings) on the same hook entry, and each admitting arm is the same body under the same program
        """THE OPENER LINE MAY DO MORE, and the body stays data (task/2973):
        a pipe into a filter that runs nothing, a list operator, a
        redirection, a second heredoc, an expansion or a substitution in an
        argument, an assignment prefix, and the wrappers bash strips (`env`,
        `timeout`, `command`) each leave the program reading its stdin as
        data. The reader follows bash, so it names the program the line
        actually runs.

        WHAT KEEPS THE BODY READ: a pipe into a shell, an unquoted tag (its
        body substitutes), a program named by a `$CMD` the text does not
        settle, and a name the recording set does not trust — `helm post
        chat`, `./helm`, `~/bin/helm`."""
        for sink in self.SINKS:
            rest = sink.split(" ", 1)[1]
            # a substitution in an argument leaves the body data for THIS
            # rung; the substitution gate answers for the argument itself
            # (a messaging verb's `$(…)` runs before helm sees it)
            for line in (sink + " --note \"$(date)\" <<'EOF'",
                         sink + " --note `date` <<'EOF'"):
                with self.subTest(actions_rung=line):
                    self.assertIsNone(chat.github_actions_refusal(
                        command=self.doc(line)))
                    self.assertIsNotNone(chat.github_actions_refusal(
                        command=self.doc(line.replace("<<'EOF'", "<<EOF"))))
            for line in (sink + " <<'EOF' | tail -2",
                         sink + " <<'EOF' && true",
                         "true; " + sink + " <<'EOF'",
                         sink + " --note $R <<'EOF'",
                         sink + " <<'EOF' > out",
                         sink + " <<'EOF' 2>&1",
                         "env A=1 " + sink + " <<'EOF'",
                         "A=1 " + sink + " <<'EOF'",
                         "timeout 60 " + sink + " <<'EOF'",
                         "command " + sink + " <<'EOF'",
                         sink + " --note " + self.VERB.split()[0] + " <<'EOF'"):
                with self.subTest(admitted=line):
                    self.assert_allowed(self.doc(line))
            for line in (sink + " <<'EOF' | bash",
                         sink + " <<EOF",
                         "$CMD " + rest + " <<'EOF'"):
                with self.subTest(refused=line):
                    self.assert_refused(self.doc(line))
            with self.subTest(second_heredoc=sink):
                self.assert_allowed("%s <<'EOF' 3<<'B'\n%s\nEOF\nhi\nB"
                                    % (sink, self.SINK_BODY))
        for line in ("git commit -q -F msg.txt <<'EOF'",
                     "git -C x commit -F - <<'EOF'",
                     "helm chat read <<'EOF'"):
            with self.subTest(admitted=line):
                self.assert_allowed(self.doc(line))
        for line in ("helm post chat <<'EOF'",
                     "./helm chat post <<'EOF'",
                     "~/bin/helm chat post <<'EOF'"):
            with self.subTest(words=line):
                self.assert_refused(self.doc(line))

    def test_a_SINK_is_trusted_only_where_bash_reads_its_opener_as_the_walker_does(self):  # noqa: VACUOUS_ASSERTION — every refusing arm runs assert_refused unconditionally (rc 2 plus four required substrings) on the same hook entry, and the admitting arms are shapes bash reads as the walker does
        """THE WALKER THAT FINDS A BODY AND THE SHELL THAT RUNS THE COMMAND
        MUST AGREE ABOUT IT, or the reader hands a line bash executes to a
        check that does not read it. Each refused arm below is a shape where
        they disagree — measured against bash, where the line the walker
        calls the body RAN as a command:

          an escaped operator: `\\<<'EOF'` is a literal `<` and a
          redirection from a file named EOF, so no heredoc opens;
          an ANSI-C quote on an EARLIER line, `$'x\\'`, whose escaped quote
          keeps it open across the opener, so bash never sees the `<<`;
          an EARLIER body with an indented terminator, which ends there for
          the walker and not for bash;
          a quote left open on the opener line.

        In each the invocation reader, which follows bash, disagrees with the
        walker and cuts NOTHING, so the body is read as it always was.

        WHERE THEY AGREE, the body is data wherever it stands: after a `cd`
        on the line above, after a continuation, before a comment, and under
        a CRLF delimiter, which bash reads as a word ending in a carriage
        return and so never finds — the whole rest is the post's body."""
        act = self.ENABLE + " ci.yml"
        post = "helm chat post --room R <<'EOF'"
        for cmd in ("helm chat post \\<<'EOF'\n%s\nEOF" % act,
                    "echo $'x\\'\n%s\n'; %s\nEOF" % (post, act),
                    "cat <<'A'\n  A\n%s\n%s\nEOF" % (post, act),
                    self.doc(post + " 'x")):
            with self.subTest(refused=cmd):
                self.assert_refused(cmd)
        for cmd in ("cd /tmp\n" + self.doc(post),
                    "helm chat \\\npost <<'EOF'\n%s\nEOF" % self.SINK_BODY,
                    self.doc(post + " # reply"),
                    self.doc(post).replace("\n", "\r\n")):
            with self.subTest(admitted=cmd):
                self.assert_allowed(cmd)

    def test_a_SINK_body_with_an_anchor_and_an_expansion_is_the_region_rule(self):  # noqa: VACUOUS_ASSERTION — every refusing arm runs assert_refused unconditionally (rc 2 plus four required substrings) on the same hook entry, and each admitting arm is the same body posted
        """A RECORDING PROGRAM'S BODY IS DATA even beside a `$` or a
        backtick: bash expands nothing in a quoted body, and helm posts the
        bytes (task/2973). The same body read by a SHELL is its script, so
        it is refused there: by its row where it holds a complete one, and
        by the region rule, naming the anchor, where an anchor stands beside
        an expansion and no row does."""
        post = "helm chat post --room R <<'EOF'"
        for body, why in (("please rerun the gate at $SHA",
                           "beside an unresolved expansion"),
                          (self.SINK_BODY + " at $SHA", "(in a heredoc body)"),
                          ("the `gh workflow` verbs stay local",
                           "beside an unresolved expansion")):
            with self.subTest(body=body):
                self.assert_allowed(self.doc(post, body))
                self.assertIn(why, self.assert_refused(
                    self.doc("bash <<'EOF'", body)))
        self.assert_allowed(self.POSTED)

    def test_TWO_bodies_are_decided_one_consumer_at_a_time(self):  # noqa: VACUOUS_ASSERTION — the refusing arms run assert_refused unconditionally (rc 2 plus four required substrings) on the same hook entry, beside the admitting ones
        """A post's body and a shell's body in one command: the row in the
        SHELL's body is refused and names the document, and the same row in
        the POST's body passes — in either order, because each body is
        decided by the program that reads it."""
        both = "helm chat post --room R <<'A'\n%s\nA\nbash <<'B'\n%s\nB"
        act = self.ENABLE + " ci.yml"
        self.assertIn("(in a heredoc body)",
                      self.assert_refused(both % ("landed; nothing to do", act)))
        self.assert_allowed(both % (self.SINK_BODY, "echo done"))
        self.assert_allowed("bash <<'B'\necho done\nB\n"
                            "helm chat post --room R <<'A'\n%s\nA"
                            % self.SINK_BODY)
        self.assert_refused("helm chat post --room R <<'A'\nhello\nA\n"
                            "bash <<'B'\n%s\nB" % act)

    def test_the_data_sets_and_the_pipe_fence_are_each_load_bearing(self):
        """MUTATIONS, run in the suite so they keep running (task/2973). Each
        closed set the invocation reader names decides something no other
        rule does:

          * `_STDIN_DATA` — taking `cat` out of it refuses a `cat > f <<'EOF'`
            body that names the act in English;
          * `_HELM_PROSE` — taking `chat` out of it refuses a chat post whose
            ARGUMENT quotes the act;
          * `_SAFE_FILTERS` — adding `bash` to it admits that same post piped
            into `bash`, which the pipe fence exists to refuse;
          * `_STDIN_SHELLS` — a body piped into a shell is code even when the
            fence is mutated open, so admitting `helm chat post <<'EOF' | bash`
            takes BOTH mutations: the shell set is a second layer, not a
            restatement of the fence."""
        def admits(cmd):
            return self.hook("Bash", command=cmd)[0] == 0
        written = self.doc("cat > f <<'EOF'")
        post = "helm chat post --room R '%s'" % self.SINK_BODY
        piped = post + " | bash"
        body_piped = self.doc("helm chat post <<'EOF' | bash")
        # unmutated: the data passes, and both pipes into a shell are refused
        self.assertEqual([admits(written), admits(post), admits(piped),
                          admits(body_piped)], [True, True, False, False])
        with mock.patch.object(chat, "_STDIN_DATA", chat._STDIN_DATA - {"cat"}):
            self.assertFalse(admits(written))
        with mock.patch.object(chat, "_HELM_PROSE", chat._HELM_PROSE - {"chat"}):
            self.assertFalse(admits(post))
        with mock.patch.object(chat, "_SAFE_FILTERS",
                               chat._SAFE_FILTERS | {"bash"}):
            self.assertTrue(admits(piped))
            self.assertFalse(admits(body_piped))
            with mock.patch.object(chat, "_STDIN_SHELLS",
                                   chat._STDIN_SHELLS - {"bash"}):
                self.assertTrue(admits(body_piped))

    def test_a_Monitor_carrying_the_act_is_refused_exactly_as_Bash(self):
        """A Monitor's command runs as a shell command, so the rung reads it;
        a Monitor with the same command must answer as a Bash call does."""
        incident = "gh api -X PUT repos/o/r/actions/permissions -F enabled=true"
        rc, out = self.hook("Monitor", command=incident, until="x")
        self.assertEqual(rc, 2, out)
        self.assertIn(self.RULE, out)
        self.assertIn("/actions at character", out)
        rc, out = self.hook("Monitor", command=self.ENABLE + " ci.yml")
        self.assertEqual(rc, 2, out)
        # CONTROLS: a Monitor that names nothing, and the same grant
        rc, out = self.hook("Monitor", command="gh pr list")
        self.assertEqual(rc, 0, out)
        rc, out = self.hook("Monitor", command="helm chat wait --seat s --follow")
        self.assertEqual(rc, 0, out)         # no agent_id: the sidechain rung is silent
        rc, out = self.hook("Monitor", command="HELM_ALLOW_GITHUB_ACTIONS=1 " + incident)
        self.assertEqual(rc, 0, out)

    def test_the_file_door_and_the_command_door_are_ONE_table(self):  # noqa: VACUOUS_ASSERTION — the first assertion of each tool's arm is unconditional and positive (rc 2 plus the refusal naming the path and the row) on the same hook entry the rc-0 paths below are read through, so a door that answered nothing fails before any allow control is reached
        """THE FILE SIDE READS THE SAME FOLD AND THE SAME TABLE, because two
        guards for one property disagree sooner or later and these two did:
        the file door consulted a private matcher that knew only
        `.github/workflows` and never folded case, so a Write of
        `.github/actions/build/action.yml` and of `.GITHUB/WORKFLOWS/ci.yml`
        was ADMITTED (measured) while the identical act through Bash — the
        same path after a `>` — was refused, and `.github/actions` is a
        shipped refusal row of the command table.

        WHAT UNIFYING COSTS: a path whose TEXT names the directory is
        refused even where its `..` leaves it, so
        `.github/workflows/../ISSUE_TEMPLATE/bug.md` — an allow control
        while the file door normalised first — is a refusal now. The cure is
        to spell the path it resolves to, which is the path a reader of the
        tool call would expect anyway.

        The allow side is the same superset's edge: a `workflows` directory
        that is not GitHub's, a `github` directory that is not `.github`,
        and every other file in the repo."""
        for tool in ("Write", "Edit"):
            rc, out = self.hook(tool, file_path="%s/x.yml" % self.WORKFLOWS,
                                content="on: push")
            self.assertEqual(rc, 2, (tool, out))
            self.assertIn("%s/x.yml, whose path holds %s"
                          % (self.WORKFLOWS, self.DIRECTORY), out)
            self.assertIn(self.RULE, out)
            self.assertIn("%s cannot carry the override word" % tool, out)
            self.assertIn(self.OVERRIDE, out)
            # the composite Actions directory, which the private matcher
            # never knew, and the case the command door already folded
            rc, out = self.hook(tool, file_path=".github/actions/build/action.yml",
                                content="runs:")
            self.assertEqual(rc, 2, (tool, out))
            self.assertIn("/actions", out)
            for path in ("/srv/x/%s/ci.yml" % self.WORKFLOWS,
                         ".github/./workflows/x.yml",
                         ".github/x/../workflows/x.yml",
                         "%s/../ISSUE_TEMPLATE/bug.md" % self.WORKFLOWS,
                         ".github/actions/build/entrypoint.sh",
                         ".GITHUB/WORKFLOWS/ci.yml",
                         "src/store/actions/user.js"):
                rc, out = self.hook(tool, file_path=path, old_string="a",
                                    new_string="b")
                self.assertEqual(rc, 2, (tool, path, out))
                self.assertIn(path, out)
            for path in (".github/ISSUE_TEMPLATE/bug.md", ".github/CODEOWNERS",
                         "docs/github/workflows.md", "src/workflows/a.py",
                         "docs/notes.md", "tests/test_chat_argv_guard.py"):
                rc, out = self.hook(tool, file_path=path, content="x")
                self.assertEqual(rc, 0, (tool, path, out))
        # the act the file door admitted, through the door that refused it:
        # both are refused now, which is the property this arm names
        self.assert_refused("printf 'runs:' > .github/actions/build/action.yml")
        # the ambient word buys a Write nothing either
        with mock.patch.dict(_os.environ, {"HELM_ALLOW_GITHUB_ACTIONS": "1"}):
            rc, _ = self.hook("Write", file_path="%s/x.yml" % self.WORKFLOWS,
                              content="x")
            self.assertEqual(rc, 2)

    def test_the_rung_fails_open_on_garbage(self):
        """A guard on EVERY Bash call fails open or it wedges the fleet. The
        control is the same reader on a payload that MUST refuse, so an rc 0
        that comes from a broken reader rather than from fail-open is
        visible here."""
        self.assertEqual(self.hook("Bash", command=self.ENABLE)[0], 2,
                         "control: this harness can see a refusal")
        for payload in ({"tool_name": "Write"}, {"tool_name": "Write", "tool_input": {}},
                        {"tool_name": "Edit", "tool_input": {"file_path": None}},
                        {"tool_name": "Bash", "tool_input": {"command": "gh api '"}},
                        {"tool_name": "Bash", "tool_input": {"command": "gh"}},
                        {"tool_name": "Bash", "tool_input": {"command": "gh api -X"}},
                        {"tool_name": "Bash", "tool_input": {"command": "printf '\\x'"}},
                        {"tool_name": "Bash", "tool_input": {"command": "echo $'\\Uffffffff'"}}):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = chat.cmd_argv_guard([])
            self.assertEqual(rc, 0, payload)

    # THE OWNER'S OWN WORD (task/2948). The canonical review fallback is "a
    # fable 1-agent" run of the noun this table anchors on, and the task body
    # filing that directive was refused as the noun's `list` row at character
    # 394: the noun in one sentence, `helm seat list` in another. Built at
    # runtime like every spelling in this file.
    NOUN = "work" + "flow"
    FILED = ("helm task add 'no reviewer is never a blocker' --note 'even if "
             "all were out, launch a fable 1-agent %s to get xrev via xmodel "
             "rather than xfam. this is canonical in helm; make sure OI has "
             "fully updated helm to reflect it for all projects. GAPS MEASURED "
             "TODAY: (1) the dispatch door refuses an unusable recipient; (2) "
             "helm seat list at 19:11Z still showed kimi PROXY-COOLDOWN'")

    def test_the_owners_word_in_prose_is_not_a_row(self):  # noqa: VACUOUS_ASSERTION — every allow arm stands beside assert_refused on the SAME prose with the verb moved inside the window or the expansion moved beside the noun, so a rung that answered nothing fails on the refusing half
        """THE NARROWING, both halves. The filing itself is a `helm task add`,
        whose quoted arguments are data (task/2973), so it passes whatever it
        says. The same prose handed to a program this rung does not know is
        read through the window: the noun and a verb far apart are no row,
        and the same prose with the verb moved beside the noun is one. The
        distance is counted in WORDS a flag does not add to, so the gh
        spellings with options between the noun and the verb stay rows."""
        filed = self.FILED % self.NOUN
        near = filed.replace("%s to get" % self.NOUN,
                             "%s list to get" % self.NOUN)
        self.assert_allowed(filed)
        self.assert_allowed(near)
        other = filed.replace("helm task add", "python3 x.py")
        self.assert_allowed(other)
        self.assert_refused(near.replace("helm task add", "python3 x.py"))
        # a hole far from the noun in the EXECUTED text is no row either —
        # the filing as it was typed, captured into a variable and echoed; a
        # substitution is read whole, so the window is what passes it
        captured = 'out=$(%s 2>&1); echo "$out" | tail -2' % filed
        self.assert_allowed(captured)
        self.assert_refused(captured.replace("%s to get" % self.NOUN,
                                             "%s $V to get" % self.NOUN))

    def test_the_window_keeps_every_spelling_gh_runs(self):  # noqa: VACUOUS_ASSERTION — every arm runs assert_refused unconditionally (rc 2 plus four required substrings)
        """What the window may never give up: options in either gap, the verb
        on either side within reach, a hole where the verb goes."""
        for cmd in ("gh %s -R o/r --repo=o/r -R o/r -R o/r -R o/r -R o/r "
                    "-R o/r -R o/r enable ci.yml" % self.NOUN,
                    "gh %s --verbose enable ci.yml" % self.NOUN,
                    "echo the run finished; echo the %s is local" % self.NOUN,
                    "subprocess.run(['gh', '%s', '--repo', r, 'enable'])"
                    % self.NOUN,
                    "gh %s -R o/r $V ci.yml" % self.NOUN):
            self.assert_refused(cmd)

    def test_an_EXECUTED_invocation_has_no_distance_cap(self):  # noqa: VACUOUS_ASSERTION — every arm runs assert_refused unconditionally (rc 2 plus four required substrings), and each allow arm is the same words moved inside one quoted string, beside its refused twin
        """THE CROSS-FAMILY FINDING ON THE FIRST CUT (task/2948):
        a real executable wrapper whose noun and verb stand as SEPARATE argv
        words thirteen words apart was admitted, which the unwindowed rung
        refused. The ruling: an EXECUTED invocation gets no distance cap. Words
        the shell hands a program as separate argv, heredoc bodies of a
        program this rung does not know included, are read at any distance;
        gh itself is parsed structurally, the noun then its first positional
        with flags and their values skipped, at any distance and inside any
        string. The window is for prose only: the words of ONE quoted string
        that holds a blank, given to a program the invocation reader does not
        know — a recording program's quoted argument is data and not read at
        all (task/2973)."""
        pad = " ".join(["step"] * (chat._ANCHOR_WINDOW + 1))
        flags = " ".join(["-R o/r"] * 20)
        for cmd in (
                # the must-refuse row: helm-codex's 13-word wrapper
                "python3 x.py %s %s enable" % (self.NOUN, pad),
                # the noun assigned far from the hole that runs it
                "A=%s; %s; gh $A $V ci.yml"
                % (self.NOUN, "; ".join("X%d=%d" % (k, k) for k in range(14))),
                # gh's own grammar at any distance, bare and inside a string
                "gh %s %s enable ci.yml" % (self.NOUN, flags),
                "bash -c 'gh %s %s enable ci.yml'" % (self.NOUN, flags),
                "gh -R o/r %s --json %s view 12" % (
                    self.NOUN, ",".join("f%d" % k for k in range(40))),
                # a program body that builds the argv far apart
                "node - <<'EOF'\nconst args = ['gh', '%s']\n// %s\n"
                "require('child_process').execFileSync(args[0], "
                "[args[1], 'enable'])\nEOF" % (self.NOUN, pad),
                # the scoped rule inside a quoted body keeps no window
                "node - \"$V\" <<'EOF'\n// %s\nrequire('child_process')"
                ".execFileSync('gh', ['%s', process.argv[2]])\nEOF"
                % (pad, self.NOUN),
                # every other anchor still refuses at ANY distance
                "echo rerun %s; echo $V" % pad):
            with self.subTest(cmd=cmd[:60]):
                self.assert_refused(cmd)
        # PROSE, the one place the window reads: the same far words inside
        # ONE quoted string pass, and moved beside each other they refuse
        prose = "python3 x.py 'the %s %s enable'" % (self.NOUN, pad)
        self.assert_allowed(prose)
        self.assert_refused(prose.replace(pad, "step"))
        # …and a recording program's quoted argument is data at any distance
        self.assert_allowed(prose.replace(pad, "step").replace(
            "python3 x.py", "helm chat post --room R"))

    def test_the_dispatches_anchor_is_the_API_path_not_helms_module(self):  # noqa: VACUOUS_ASSERTION — every allow arm stands beside assert_refused on the documented trigger, in both the path and the variable spelling
        """task/2973, a survey: 21 of 212 refusals of this rung were
        helm's OWN module paths (`helm/dispatches*.py`, a `.../dispatches/`
        directory), and none of the 212 was a real Actions act. The trigger is
        `repos/<o>/<r>/dispatches`, the LAST segment of an API path, so the
        piece now ends where that segment ends and a glob or a deeper path
        segment is not it."""
        for cmd in ("git grep -n x -- helm/dispatches*.py",
                    "ls helm/dispatches*.py tests/test_dispatches*.py",
                    "cat notes/turnstamp/dispatches/ownership.md"):
            self.assert_allowed(cmd)
        for cmd in ("gh api -X POST repos/o/r/dispatches -f event_type=deploy",
                    "curl -X POST $URL/dispatches -d @body.json",
                    "curl -X POST https://api.github.com/repos/o/r/dispatches/",
                    "curl -X POST 'https://api.github.com/repos/o/r/dispatches?x=1'"):
            self.assertIn("repository_dispatch", self.assert_refused(cmd))

    # task/2973's EVIDENCE: seven commands the rung refused although none of
    # them runs CI, each in the shape it was typed (paths and prose shortened,
    # every operator, quote, prefix and pipe kept). Built at runtime, like
    # every spelling in this file.
    V2973, D2973, N2973 = "run", "down" + "load", "work" + "flow"
    EVIDENCE_2973 = (
        ("1 prose in a double-quoted chat post body, the verbs apart",
         "cd ~/dev/akapug/infra && HELM_CHAT_NAME=infra-claude-2 helm chat"
         " post --room helm \"Starting now on drowsy: a 147.5 GB %s to "
         "/mnt/datadrive1. The ruler %s later stops coder30.\" 2>&1 | tail -1;"
         " ssh drowsy 'nproc; ls ~/b70-lane/ 2>/dev/null | head'"
         % (D2973, V2973)),
        ("2 a gh api GET of another repository's dot-github contents",
         "timeout 30 gh api repos/syv-ai/HyperQwen/contents/.github --jq "
         "'.[].name' 2>&1 | tr '\\n' ' '; for f in PULL_REQUEST_TEMPLATE.md "
         "CONTRIBUTING.md; do timeout 20 gh api repos/syv-ai/HyperQwen/"
         "contents/.github/$f --jq '.content' 2>/dev/null | base64 -d | "
         "head -40; done"),
        ("3 a grep PATTERN over path names, beside an expansion",
         "mb=$(git merge-base origin/main HEAD); git diff --name-only $mb HEAD"
         " | /usr/bin/grep -E 'hooks.py|\\.github'"),
        ("4 grep -c of a docs phrase naming the noun, beside a backtick",
         "cd /var/tmp/helm-train148 && /usr/bin/grep -c 'which `--shared` "
         "silences' docs/HOOKS.md; /usr/bin/grep -c 'One mention passes: a "
         "READ of the %s directory' docs/HOOKS.md; nohup bash /var/tmp/x.sh "
         "> /var/tmp/x.log 2>&1 &" % N2973),
        ("5 a task comment whose quoted text holds the pair",
         "export HELM_CHAT_NAME=opus-integrator; timeout 100 helm task comment"
         " 2973 'the cure: match the verbs only in an executed gh invocation "
         "(argv0..1 = gh %s / gh %s %s), never in a quoted string' 2>&1 | "
         "tail -1" % (V2973, N2973, V2973)),
        ("6 a python heredoc whose string literal names the pair",
         "python3 - <<'EOF'\np = open('prompt.txt').read()\np = p.replace("
         "'MY JOB', 'MY JOB: launch a fable %s %s for the review')\n"
         "open('prompt.txt', 'w').write(p)\nEOF" % (N2973, V2973)),
        ("7 a task add whose --note names the verbs",
         "HELM_CHAT_NAME=opus-integrator timeout 100 helm task add 'An "
         "on-behalf verdict earns a land tier once helm verifies the run' "
         "--owner helm-claude-2 --note 'it rises only when helm reads the "
         "named run (the %s transcript dir, or gh %s or gh %s %s output)' "
         "2>&1 | /usr/bin/grep -o -E 'filed task/[0-9]+'"
         % (N2973, V2973, V2973, D2973)),
    )
    # …and the acts the ruling keeps refused: gh as the program, directly or
    # through a wrapper; data a shell then runs; a write into the directory;
    # the Actions API written to
    W2973 = ".git" + "hub/" + N2973 + "s"
    ACTS_2973 = (
        ("gh run rerun", "gh %s re%s 123" % (V2973, V2973)),
        ("gh run download", "gh %s %s 123" % (V2973, D2973)),
        ("gh workflow run", "gh %s %s ci.yml" % (N2973, V2973)),
        ("gh workflow enable", "gh %s enable ci.yml" % N2973),
        ("through sudo", "sudo gh %s %s ci.yml" % (N2973, V2973)),
        ("through env", "env A=1 gh %s %s ci.yml" % (N2973, V2973)),
        ("through timeout", "timeout 30 gh %s %s ci.yml" % (N2973, V2973)),
        ("through bash -c", "bash -c 'gh %s %s ci.yml'" % (N2973, V2973)),
        ("through eval", "eval \"gh %s %s ci.yml\"" % (N2973, V2973)),
        ("through ssh", "ssh box 'gh %s %s ci.yml'" % (N2973, V2973)),
        ("through xargs", "printf '%s %s ci.yml' | xargs gh" % (N2973, V2973)),
        ("data piped into bash", "echo 'gh %s %s ci.yml' | bash"
         % (N2973, V2973)),
        ("a loop's output piped into bash",
         "for x in 1; do echo 'gh %s %s ci.yml'; done | bash" % (N2973, V2973)),
        ("data sent to a process substitution",
         "echo 'gh %s %s ci.yml' > >(bash)" % (N2973, V2973)),
        ("a substitution inside a post",
         "helm chat post \"note $(gh %s %s ci.yml)\"" % (N2973, V2973)),
        ("echo redefined in the same command",
         "echo() { eval \"$@\"; }; echo 'gh %s %s ci.yml'" % (N2973, V2973)),
        ("helm found through a PATH set in the same command",
         "PATH=./x:$PATH helm chat post 'gh %s %s ci.yml'" % (N2973, V2973)),
        ("a message git hands an editor",
         "git commit -e -m 'gh %s %s ci.yml'" % (N2973, V2973)),
        ("a shell's heredoc", "bash <<'EOF'\ngh %s %s ci.yml\nEOF"
         % (N2973, V2973)),
        ("an anchorless row in a shell's heredoc",
         "bash <<'EOF'\ngh %s cancel 123\nEOF" % V2973),
        ("an anchorless row piped into a shell",
         "cat <<'EOF' | sh\ngh %s watch 123\nEOF" % V2973),
        ("the 13-word wrapper", "python3 x.py %s %s enable"
         % (N2973, " ".join(["step"] * 13))),
        ("a git push of a workflow file",
         "git add %s/ci.yml && git commit -m 'add ci' && git push" % W2973),
        ("tee into the directory", "tee %s/ci.yml < x.yml" % W2973),
        ("cp into the directory", "cp x.yml %s/ci.yml" % W2973),
        ("mv into the directory", "mv x.yml %s/ci.yml" % W2973),
        ("sed -i in the directory", "sed -i 's/a/b/' %s/ci.yml" % W2973),
        ("a redirect into the directory", "echo 'on: push' > %s/ci.yml"
         % W2973),
        ("cat's heredoc into the directory",
         "cat > %s/ci.yml <<'EOF'\non: push\nEOF" % W2973),
        ("curl POST to the dispatch trigger",
         "curl -X POST https://api.github.com/repos/o/r/dis" "patches -d '{}'"),
        ("curl POST to a workflow's dispatch",
         "curl -X POST https://api.github.com/repos/o/r/acti" "ons/"
         + N2973 + "s/ci.yml/dis" "patches -d '{\"ref\":\"main\"}'"),
        ("gh api PUT", "gh api -X PUT repos/o/r/acti" "ons/permissions "
         "-F enabled=true"),
        ("gh api with a field", "gh api repos/o/r/dis" "patches -f "
         "event_type=x"),
        ("gh api with a body file", "gh api repos/o/r/acti" "ons/runs/1/re"
         + V2973 + " --input body.json"),
        ("gh api writing a workflow file",
         "gh api -X PUT repos/o/r/contents/%s/ci.yml -f message=x -f "
         "content=eA==" % W2973),
        # THE CROSS-MODEL READ OF THIS LANE (Fable, BLOCK): each of these was
        # admitted by the first cut and ran the act under real bash with a
        # stub gh; the last six were admitted on the trunk as well
        ("printf -v assigns the act, then runs it",
         "printf -v c 'gh %s %s ci.yml'; $c" % (N2973, V2973)),
        ("printf -v through a format",
         "printf -v c '%%s' 'gh %s %s ci.yml'; $c" % (N2973, V2973)),
        ("printf -v, an anchorless row",
         "printf -v c 'gh %s %s 1'; $c" % (V2973, D2973)),
        ("eval defines echo, then echo runs the act",
         "eval 'echo() { eval \"$@\"; }'; echo 'gh %s %s ci.yml'"
         % (N2973, V2973)),
        ("a sourced heredoc defines echo",
         ". /dev/stdin <<'EOF'\necho() { eval \"$@\"; }\nEOF\n"
         "echo 'gh %s %s ci.yml'" % (N2973, V2973)),
        ("a DEBUG trap defines echo",
         "trap 'echo() { eval \"$@\"; }' DEBUG; echo 'gh %s %s ci.yml'"
         % (N2973, V2973)),
        ("eval of a printf-built definition",
         "eval \"$(printf 'echo(){ eval \\\"$@\\\"; }')\"; "
         "echo 'gh %s %s ci.yml'" % (N2973, V2973)),
        ("the function keyword inside eval",
         "eval 'function echo { eval \"$@\"; }'; echo 'gh %s %s ci.yml'"
         % (N2973, V2973)),
        ("builtin eval defines echo",
         "builtin eval 'echo() { eval \"$@\"; }'; echo 'gh %s %s ci.yml'"
         % (N2973, V2973)),
        ("eval re-reads a recorded message",
         "git commit --allow-empty -q -m 'gh %s %s ci.yml' && eval "
         "\"$(git log -1 --format=%%s)\"" % (N2973, V2973)),
        ("a recorded message run as a command word",
         "git commit --allow-empty -q -m 'gh %s %s ci.yml' && "
         "$(git log -1 --format=%%s)" % (N2973, V2973)),
        ("git log --output writes into the directory through a blank",
         "git log -1 --format='format:on: push' --output='%s/ci .yml'"
         % W2973),
        ("gh release download -O writes into the directory",
         "gh release download v1 -p 'ci.yml' -O '%s/ci .yml'" % W2973),
        ("a clone into the directory, by the belt",
         "gh repo clone o/r '%s/x y'" % W2973),
        # …and each rule where it is the ONLY one that refuses: no value runs
        # the printf variable, and no `.github` stands in the output path
        ("printf -v, the variable handed to bash -c",
         "printf -v c 'gh %s %s ci.yml'; bash -c \"$c\"" % (N2973, V2973)),
        ("git log --output after a cd into .github",
         "cd .git" "hub && git log -1 --format='format:on: push' "
         "--output='%ss/ci .yml'" % N2973),
        ("gh release download -O after a cd into .github",
         "cd .git" "hub && gh release download v1 -O '%ss/ci .yml'" % N2973),
        ("eval re-reads a recorded body, an anchorless row",
         "git commit -q -F - <<'EOF'\ngh %s cancel 1\nEOF\neval "
         "\"$(git log -1 --format=%%B)\"" % V2973),
        ("a body piped into a subshell, an anchorless row",
         "cat <<'EOF' | (sh)\ngh %s cancel 1\nEOF" % V2973),
        ("sudo -s reads a body", "sudo -s <<'EOF'\ngh %s cancel 1\nEOF"
         % V2973),
        ("sudo -i reads a body", "sudo -i <<'EOF'\ngh %s watch 1\nEOF"
         % V2973),
        ("a read loop runs each line as a command",
         "while read -r l; do $l; done <<'EOF'\ngh %s cancel 1\nEOF"
         % V2973),
        ("a read loop hands each line to bash -c",
         "while read -r l; do bash -c \"$l\"; done <<'EOF'\n"
         "gh %s cancel 1\nEOF" % V2973),
    )

    def test_task_2973_EVIDENCE_is_data_and_the_ACTS_are_still_refused(self):  # noqa: VACUOUS_ASSERTION — every evidence arm asserts rc 0 and every act asserts rc 2 plus four required substrings, all unconditional on the same hook entry
        """THE RULING (task/2973): an Actions verb is an invocation only where
        a shell RUNS it — gh as the program, directly or through a wrapper.
        Each EVIDENCE row was refused by the rung while it read every word
        of a command alike, and each performs nothing: a post, a comment, a
        note, a grep pattern, a gh api GET and a python heredoc's string
        literal are data a program reads. Each ACT is a spelling that does
        reach GitHub Actions or the workflow directory, or would through a
        name the same command rebinds, and each stays refused — the ones the
        cross-model read of this lane found among them: `printf -v`, a name
        rebound or a message re-read through `eval`, `source`, `.` or `trap`,
        a write through a quoted output path, and shells reached through a
        subshell, `sudo -s` or a read loop. The rows are asserted by name, so
        a failure says which one moved."""
        for why, cmd in self.EVIDENCE_2973:
            with self.subTest(evidence=why):
                self.assert_allowed(cmd)
        for why, cmd in self.ACTS_2973:
            with self.subTest(act=why):
                self.assert_refused(cmd)

    def test_task_2973_a_READER_DEFECT_reads_the_command_whole(self):  # noqa: VACUOUS_ASSERTION — the observable is the invocation text compared to the WHOLE command, and assert_refused (rc 2 plus four required substrings) runs unconditionally on the same hook entry under the patch
        """The invocation reader stands on every Bash call and the hook
        fails OPEN on an exception, so a defect in the reader must cost the
        refusals it lifts and never one the rung owes: made to raise, it
        leaves the command read whole, data and acts alike."""
        cmd = "helm chat post 'the %s %s ran' ; gh %s %s ci.yml" % (
            self.N2973, self.V2973, self.N2973, self.V2973)
        post = "helm chat post 'the %s %s ran'" % (self.N2973, self.V2973)
        self.assert_allowed(post)
        with mock.patch.object(chat, "_ShellReader",
                               side_effect=RuntimeError("reader defect")):
            self.assertEqual(chat._invocation_text(cmd), (cmd, frozenset()))
            self.assert_refused(cmd)
            self.assert_refused(post)


class SharedDataPredicateTest(unittest.TestCase):
    """The data predicate the GitHub-Actions, owner-posture and delegate
    authority rungs cut with (`chat._command_data`, task/3060). A process
    reader's arguments, python's -c text, and a git prose subcommand after a
    `-c` that names nothing git runs are data. Python text that starts a
    process, names the workflow directory or is a script's argv, a `-c` that
    names a program git would run, and a substitution in any of them are
    not. Asked through the Actions rung, which reads nothing but the cut."""

    # built at runtime, so this FILE holds no whole spelling (see the rung
    # class above)
    ENABLE = "gh " + "workflow" + " enable" + " ci.yml"
    WORKFLOWS = ".github/" + "workflows"

    def refused(self, command):
        return chat.github_actions_refusal(command=command)

    def test_what_names_an_act_as_data_passes(self):  # noqa: VACUOUS_ASSERTION — the bare act is asserted refused through the same function before each data spelling is asserted to pass
        self.assertIsNotNone(self.refused(self.ENABLE))
        for command in (
                "pgrep -f '%s'" % self.ENABLE,
                "pkill -f '%s'" % self.ENABLE,
                "ps -o pid= -C '%s'" % self.ENABLE,
                "python3 -c \"print('%s')\"" % self.ENABLE,
                "python3 -P -c \"print('%s')\"" % self.ENABLE,
                "git -c user.name=a -c user.email=b commit -m '%s'"
                % self.ENABLE):
            with self.subTest(command=command):
                self.assertIsNone(self.refused(command), command)

    def test_what_may_run_or_write_stays_code(self):
        self.assertIsNotNone(self.refused(self.ENABLE))
        self.assertIsNone(self.refused("pgrep -f '%s'" % self.ENABLE))
        for command in (
                "python3 -c \"import subprocess; subprocess.run('%s', "
                "shell=True)\"" % self.ENABLE,
                "python3 -c \"import os; os.system('%s')\"" % self.ENABLE,
                "python3 -c \"open('%s/ci.yml', 'w').write('x')\""
                % self.WORKFLOWS,
                "python3 script.py '%s'" % self.ENABLE,
                "python3 -m tool '%s'" % self.ENABLE,
                "git -c core.editor=vi commit -m '%s'" % self.ENABLE,
                "git -c alias.c='!sh' c -m '%s'" % self.ENABLE,
                "pgrep -f \"$(%s)\"" % self.ENABLE):
            with self.subTest(command=command):
                self.assertIsNotNone(self.refused(command), command)


class SteerRungTest(unittest.TestCase):
    """PTU steers — advisory redirections riding the hook we already pay for.

    MEASURED BEFORE BUILDING, because the budget decides the shape: every
    Bash call already costs 211ms of hooks (argv-guard 82ms Pre + chat
    deliver 129ms Post), ~55ms of it interpreter+import. A NEW hook costs a
    whole extra process; a rung inside one we already pay for costs its own
    regex. Measured after: +6.7ms on a FIRE (latch write included), and the
    no-match path is inside the noise of baseline.
    """

    def run_hook(self, command, session="sess-abc"):
        out, err = io.StringIO(), io.StringIO()
        stdin = sys.stdin
        sys.stdin = io.StringIO(json.dumps(
            {"tool_name": "Bash", "session_id": session,
             "tool_input": {"command": command}}))
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = chat.cmd_argv_guard([])
        finally:
            sys.stdin = stdin
        return rc, out.getvalue() + err.getvalue()

    def run_hook_streams(self, command, session="sess-abc"):
        """(rc, stdout, stderr) KEPT APART. `run_hook` above joins them, and a
        joined reading cannot tell the stream the harness hands to the agent
        from the one it sends to a debug log."""
        out, err = io.StringIO(), io.StringIO()
        stdin = sys.stdin
        sys.stdin = io.StringIO(json.dumps(
            {"tool_name": "Bash", "session_id": session,
             "tool_input": {"command": command}}))
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = chat.cmd_argv_guard([])
        finally:
            sys.stdin = stdin
        return rc, out.getvalue(), err.getvalue()

    def test_a_steer_reaches_the_AGENT_and_not_only_the_debug_log(self):
        """The harness gives an agent exactly one thing from a hook that exits
        0: the JSON envelope on STDOUT. Stderr at exit 0 is a debug log."""
        rc, out, err = self.run_hook_streams(
            "git diff --name-only origin/main..HEAD", session="heard-1")
        self.assertEqual(rc, 0)
        spec = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(spec["hookEventName"], "PreToolUse")
        self.assertIn("[helm steer]", spec["additionalContext"])
        self.assertIn("TWO-DOT DIFF", spec["additionalContext"])
        self.assertIn("origin/main...HEAD", spec["additionalContext"])
        self.assertIn("TWO-DOT DIFF", err, "the debug-log copy went missing")
        # ordinary correct work says NOTHING on the agent's stream
        rc, out, _err = self.run_hook_streams(
            "git diff --name-only origin/main...HEAD", session="heard-2")
        self.assertEqual((rc, out), (0, ""))

    def test_a_steer_and_a_tree_line_share_ONE_envelope(self):
        """One JSON document per hook process: a second one is not parsed, so
        whichever line printed second would be lost."""
        with mock.patch.object(
                chat, "tree_steers",
                return_value=[("foreign-tree-zeta", "[helm] a tree line")]):
            rc, out, _err = self.run_hook_streams(
                "git diff origin/main..HEAD", session="both-1")
        self.assertEqual(rc, 0)
        said = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("TWO-DOT DIFF", said)
        self.assertIn("[helm] a tree line", said)
        self.assertEqual(len(out.strip().splitlines()), 1, out)

    def test_a_failing_tree_rung_never_silences_a_steer(self):
        with mock.patch.object(chat, "tree_steers",
                               side_effect=RuntimeError("the reader broke")):
            rc, out, _err = self.run_hook_streams(
                "git diff origin/main..HEAD", session="broke-1")
        self.assertEqual(rc, 0)
        self.assertIn("TWO-DOT DIFF",
                      json.loads(out)["hookSpecificOutput"]["additionalContext"])

    def test_a_BLOCKED_call_says_nothing_on_the_agents_pass_stream(self):
        """A refusal is read from stderr at exit 2. An envelope beside it
        would be a second voice on a call that is not going to run."""
        rc, out, err = self.run_hook_streams(
            "helm chat post \"see `git diff origin/main..HEAD`\"",
            session="blocked-1")
        self.assertEqual(rc, 2, err)
        self.assertEqual(out, "")
        self.assertIn("BLOCKED", err)
        # POSITIVE CONTROL, same helper, same session shape: the call that
        # PASSES does write the agent's stream, so the empty stdout above is
        # the block's doing and not a helper that cannot see stdout.
        rc, out, _err = self.run_hook_streams(
            "git diff --name-only origin/main..HEAD", session="blocked-1")
        self.assertEqual(rc, 0)
        self.assertEqual(len(out.strip().splitlines()), 1, out)
        self.assertIn("additionalContext", out)

    # -- a message that NAMES a command is not running it -------------------

    def test_a_steer_never_fires_on_the_body_of_a_quoted_heredoc(self):
        """CONTROL FIRST: the same words ON the command line do fire, so the
        silence below is the body's doing."""
        self.assertEqual([sid for sid, _t in chat.argv_steers(
            "git diff --name-only origin/main..HEAD")], ["two-dot-lane-diff"])
        body = ("helm chat post --room r <<'EOF'\n"
                "I ran git diff --name-only origin/main..HEAD and it lied\n"
                "EOF")
        self.assertEqual(chat.argv_steers(body), [])
        rc, out, _err = self.run_hook_streams(body, session="body-1")
        self.assertEqual((rc, out), (0, ""))

    def test_an_UNQUOTED_heredoc_body_is_still_read(self):
        """The shell substitutes inside an unquoted body, so it is not inert
        and the steer keeps reading it."""
        body = ("bash <<EOF\n"
                "git diff --name-only origin/main..HEAD\n"
                "EOF")
        self.assertEqual([sid for sid, _t in chat.argv_steers(body)],
                         ["two-dot-lane-diff"])

    def test_the_command_line_around_a_quoted_body_is_still_read(self):
        body = ("git diff --name-only origin/main..HEAD > /tmp/x; cat <<'EOF'\n"
                "some prose\n"
                "EOF")
        self.assertEqual([sid for sid, _t in chat.argv_steers(body)],
                         ["two-dot-lane-diff"])

    def test_a_reader_that_raises_leaves_the_steer_reading_everything(self):
        with mock.patch.object(chat, "_excise_quoted_heredocs",
                               side_effect=RuntimeError("the reader broke")):
            self.assertEqual([sid for sid, _t in chat.argv_steers(
                "git diff --name-only origin/main..HEAD")], ["two-dot-lane-diff"])

    # -- the envelope has a ceiling, and the ceiling never costs a steer ----

    def every_steer(self):
        return [(sid, text) for sid, _pattern, text in chat._STEERS]

    def said_by(self, command, session):
        rc, out, _err = self.run_hook_streams(command, session=session)
        self.assertEqual(rc, 0)
        if not out.strip():
            return ""
        self.assertEqual(len(out.strip().splitlines()), 1, out)
        return json.loads(out)["hookSpecificOutput"]["additionalContext"]

    def test_one_call_is_never_handed_more_than_the_envelope_budget(self):
        """Every steer tripping at once is the worst call there is. The
        control is the same table joined with no ceiling, which must NOT fit:
        otherwise this arm passes because the table is small."""
        whole = "\n".join("[helm steer] " + t for _sid, t in self.every_steer())
        self.assertGreater(len(whole), chat.ENVELOPE_BUDGET,
                           "control: the table fits whole, so nothing is tested")
        with mock.patch.object(chat, "argv_steers", return_value=self.every_steer()), \
                mock.patch.object(chat, "tree_steers", return_value=[]):
            said = self.said_by("true", "ceiling-1")
        self.assertLessEqual(len(said), chat.ENVELOPE_BUDGET)
        self.assertGreaterEqual(said.count("[helm steer]"), 2,
                                "the ceiling holds two of the widest lines")

    def test_a_line_that_did_not_fit_is_said_on_the_NEXT_call(self):
        """DEFERRED, NEVER LOST. A line the budget turned away keeps its
        latch, so across calls every steer is said exactly once and then the
        session is silent."""
        heard = []
        with mock.patch.object(chat, "argv_steers", return_value=self.every_steer()), \
                mock.patch.object(chat, "tree_steers", return_value=[]):
            for _call in range(len(chat._STEERS) + 1):
                said = self.said_by("true", "deferred-1")
                self.assertLessEqual(len(said), chat.ENVELOPE_BUDGET)
                heard += [l for l in said.splitlines() if l]
            self.assertEqual(self.said_by("true", "deferred-1"), "",
                             "a steer was said twice in one session")
        self.assertEqual(sorted(heard),
                         sorted("[helm steer] " + t for _sid, t in self.every_steer()))

    def test_the_first_line_rides_even_when_it_alone_is_over_budget(self):
        """A budget able to silence every line is the defect the envelope
        was built to end."""
        self.assertEqual(chat.admit("alone-1", [("wide-one", "x" * 50)], budget=10),
                         ["x" * 50])
        self.assertEqual(chat.admit("alone-2", [("a", "x" * 8), ("b", "y" * 8)],
                                    budget=10), ["x" * 8])
        self.assertEqual(chat.admit("alone-2", [("a", "x" * 8), ("b", "y" * 8)],
                                    budget=10), ["y" * 8],
                         "the line turned away lost its latch")

    def test_a_tree_line_waits_behind_the_steers_and_is_not_lost(self):
        # WIDE ON PURPOSE: a short line slips in beside two steers on the
        # first call and never waits, so it would test nothing about waiting.
        line = "[helm] a tree line " + "t" * 300
        tree = [("foreign-tree-eta", line)]
        with mock.patch.object(chat, "argv_steers", return_value=self.every_steer()), \
                mock.patch.object(chat, "tree_steers", return_value=tree):
            calls = [self.said_by("true", "tree-waits-1")
                     for _call in range(len(chat._STEERS) + 2)]
        self.assertNotIn(line, calls[0], "control: the line never had to wait")
        heard = "".join(calls)
        self.assertEqual(heard.count(line), 1)

    # -- the acceptance test the review bound, BOTH arms -------------------

    def test_a_two_dot_lane_diff_is_HEARD_before_it_is_acted_on(self):
        """ARM ONE. The signal that cost real time today: a 6-file lane read
        as 8 because `git diff A..B` on a branch BEHIND its base renders the
        base's own commits as differences, and the 2 extras were another
        seat's live work."""
        rc, out = self.run_hook("git diff --name-only origin/main..HEAD",
                                session="two-dot-1")
        self.assertIn("TWO-DOT DIFF", out, "the steer did not fire")
        self.assertIn("origin/main...HEAD", out, "no cure named")
        self.assertEqual(rc, 0, "a STEER blocked the call; it is advisory")

    def test_ordinary_correct_work_hears_NOTHING(self):
        """ARM TWO, and it is the half that decides whether this is usable.
        A rung that always fires is noise, and noise on the highest-frequency
        surface helm owns trains every seat to skim the one that mattered.

        The three-dot form is here on purpose: the CORRECT spelling of the
        very command the first arm catches must be silent, or the steer is
        just an anti-git-diff alarm."""
        # POSITIVE CONTROL ON THE SAME HARNESS, unconditional and first: the
        # rig must be able to SEE a fire, or every silence below is the
        # silence of a broken probe rather than of a well-behaved steer.
        live = self.run_hook("git diff origin/main..HEAD", session="live-probe")[1]
        self.assertIn("[helm steer]", live,
                      "the harness cannot detect a fire, so the silences "
                      "asserted below prove nothing")

        for cmd in ("git diff --name-only origin/main...HEAD",
                    "git log origin/main..HEAD --oneline",
                    "python3 -m pytest tests/test_chat.py -q",
                    "helm dispatch verdict \"$ID\" \"$TIP\" --approve ok",
                    "pytest -q | tail -3",
                    "ls -la /tmp"):
            rc, out = self.run_hook(cmd, session="quiet-%d" % hash(cmd))
            self.assertEqual(rc, 0, cmd)
            self.assertNotIn(  # noqa: VACUOUS_ASSERTION — P2 kills this arm
                "[helm steer]", out,
                "ordinary correct work was interrupted: %s" % cmd)

    # -- budget -------------------------------------------------------------

    def test_a_steer_speaks_ONCE_per_session(self):
        """The attention budget is the hard constraint here. Starting quiet
        can be loosened; starting loud cannot be undone, because the seats
        will already have learned to skim."""
        first = self.run_hook("git diff origin/main..HEAD", session="latch-1")[1]
        self.assertIn("TWO-DOT DIFF", first)
        again = self.run_hook("git diff origin/main..HEAD", session="latch-1")[1]
        self.assertNotIn(  # noqa: VACUOUS_ASSERTION — P1b/P3 kill this arm
            "[helm steer]", again, "the steer repeated in-session")
        other = self.run_hook("git diff origin/main..HEAD", session="latch-2")[1]
        self.assertIn("TWO-DOT DIFF", other,
                      "the latch leaked across sessions — a new seat would "
                      "never hear it")

    def test_a_steer_never_fires_on_the_CURE_ITS_OWN_TEXT_RECOMMENDS(self):
        """`set -o pipefail; pytest -q | tail -3 ; rc=$?` still fired
        pipeline-exit-status — and pipefail is the cure that rung's own text
        names. A steer that punishes its own fix teaches seats to ignore it,
        which is the failure this whole surface is budgeted against.

        THE SUPPRESSOR IS A SEPARATE PASS, not a cleverer trigger. My first
        cure put a negative lookahead inside the regex and it STILL fired,
        because `search` retries at later offsets where the earlier token sits
        behind the cursor and the lookahead trivially succeeds."""
        for cmd in ("set -o pipefail; pytest -q | tail -3 ; rc=$?",
                    "set -o pipefail\npytest -q | tail -3 ; rc=$?"):
            self.assertEqual(
                [s for s, _ in chat.argv_steers(cmd)], [],
                "the rung fired on its own documented cure: %r" % cmd)
        # POSITIVE CONTROL: without pipefail it must still fire, or the
        # suppressor has simply disabled the rung.
        self.assertIn("pipeline-exit-status",
                      [s for s, _ in chat.argv_steers("pytest -q | tail -3 ; rc=$?")],
                      "the suppressor disabled the rung outright")

    def test_a_PATH_after_double_dash_is_not_a_revision_range(self):
        """`git diff -- src/generated..bak` fired two-dot-lane-diff,
        but everything after `--` is a PATHSPEC — that token names a FILE.
        Firing there tells a seat their correct command is wrong."""
        self.assertEqual(
            [s for s, _ in chat.argv_steers("git diff -- src/generated..bak")],
            [], "a pathspec containing dots was read as a rev range")
        # POSITIVE CONTROL: a real two-dot RANGE still fires.
        self.assertIn("two-dot-lane-diff",
                      [s for s, _ in chat.argv_steers(
                          "git diff --name-only origin/main..HEAD")],
                      "the `--` guard disabled the rung outright")

    def test_two_sessions_sharing_a_prefix_get_their_OWN_latch(self):
        """The latch keyed on an 8-CHAR PREFIX of the session id, so
        two sessions sharing it collapsed to one file and the SECOND seat was
        silently muted — a steer that does not fire is indistinguishable from
        correct work, which makes this the worst failure this surface has.

        I have a premise about never truncating an identity to a prefix and
        wrote the truncation anyway. The key is now a hash of the FULL id:
        bounded filename, no identity discarded."""
        a = "cf8ce076-aaaa-4444-8888-000000000001"
        b = "cf8ce076-bbbb-4444-8888-000000000002"   # same 8-char prefix
        self.assertEqual(a[:8], b[:8], "the fixture no longer shares a prefix")

        first = self.run_hook("git diff origin/main..HEAD", session=a)[1]
        self.assertIn("[helm steer]", first)
        other = self.run_hook("git diff origin/main..HEAD", session=b)[1]
        self.assertIn("[helm steer]", other,
                      "a session sharing the first 8 characters was muted by "
                      "the other's latch")
        # and each still latches on ITSELF
        again = self.run_hook("git diff origin/main..HEAD", session=b)[1]
        self.assertNotIn("[helm steer]", again,  # noqa: VACUOUS_ASSERTION — P3 kills this
                         "the latch stopped working per-session")

    def test_pipefail_suppresses_only_when_it_ACTUALLY_PROTECTS(self):
        """The first suppressor was broken by MOVING the token. It keyed on
        `pipefail` appearing anywhere, so three shapes silenced a real hazard:
        set AFTER the pipeline, DISABLED with `set +o`, or set inside a
        SUBSHELL that has already closed.

        A FALSE NEGATIVE IS WORSE THAN THE FALSE POSITIVE IT CURED — a steer
        that does not fire is indistinguishable from correct work."""
        # UNCONDITIONAL POSITIVE CONTROL, outside every branch and loop.
        # Without it this test's only guaranteed assertion is the ABSENCE one
        # at the bottom, and an absence assertion passes perfectly on a build
        # where argv_steers returns [] for everything — including the three
        # hazards below, whose assertions all live inside a for-loop that an
        # empty dict would skip in silence.
        self.assertIn("pipeline-exit-status",
                      [s for s, _ in chat.argv_steers(
                          "pytest -q | tail -3 ; rc=$?")],
                      "the rung does not fire on the bare hazard, so every "
                      "suppression result below is meaningless")

        must_fire = {
            "pipefail set AFTER the pipeline":
                "pytest -q | tail -3 ; rc=$? ; set -o pipefail",
            "pipefail explicitly DISABLED":
                "set +o pipefail; pytest -q | tail -3 ; rc=$?",
            "pipefail set inside a CLOSED subshell":
                "(set -o pipefail; true); pytest -q | tail -3 ; rc=$?",
        }
        self.assertEqual(len(must_fire), 3, "a shape was dropped from the "
                         "table, so its arm silently stopped running")
        for why, cmd in must_fire.items():
            self.assertIn("pipeline-exit-status",
                          [s for s, _ in chat.argv_steers(cmd)],
                          "the hazard was muted by %s" % why)
        # NEGATIVE CONTROL: the REAL cure still silences it, or the fix has
        # simply reverted to firing on its own documented remedy.
        self.assertEqual(
            [s for s, _ in chat.argv_steers(
                "set -o pipefail; pytest -q | tail -3 ; rc=$?")], [],
            "the genuine cure no longer suppresses the rung")

    def test_a_pipeline_branched_on_WITHOUT_naming_its_status_fires(self):
        """THE IMPLICIT HALF OF THE SAME HAZARD, and the one the incident
        actually took. The rung shipped keyed on `$?` — an EXPLICIT read — so
        `pytest | tail || echo fail` sailed through, even though the `||` is
        branching on TAIL's status and therefore cannot fire when pytest is
        what failed. A fallback placed where it can never run is worse than
        no fallback: it reads as handled.

        Found by writing a positive control for the test above and binding it
        to the wrong observable — the bare pipeline. That failure was correct
        and it was informative: the rung's silence there was RIGHT (nothing
        had read the status yet), and chasing why exposed the branch form it
        was silent on for real."""
        # UNCONDITIONAL POSITIVE CONTROL — and the rung flagged THIS test for
        # wanting one, which is the joke landing twice: I wrote the loop below
        # while curing the identical shape one test up. Every positive here
        # lived inside the for; the only guaranteed assertions were the two
        # ABSENCE controls, and those pass on a build that flags nothing.
        self.assertIn("pipeline-exit-status",
                      [s for s, _ in chat.argv_steers(
                          "pytest -q | tail -3 || echo fail")],
                      "the implicit-branch form does not fire at all")

        for cmd in ("pytest -q | tail -3 || echo fail",
                    "make build | tail -1 && deploy.sh",
                    "cargo test | sed -n 1p || exit 1"):
            self.assertIn("pipeline-exit-status",
                          [s for s, _ in chat.argv_steers(cmd)],
                          "a pipeline branched on implicitly went unflagged: "
                          "%s" % cmd)

        # NEGATIVE CONTROL 1: grep is EXCLUDED from this alternative on
        # purpose. `| grep -q x && ...` branches on grep's status BY DESIGN
        # and is correct shell; flagging it would train the rung away.
        self.assertEqual(
            [s for s, _ in chat.argv_steers("pytest -q | grep -q PASS && ok")],
            [], "an intentional grep-status branch was flagged as a hazard")

        # NEGATIVE CONTROL 2: the suppressor still applies to the new form,
        # or the widening reintroduced the false positive already cured.
        self.assertEqual(
            [s for s, _ in chat.argv_steers(
                "set -o pipefail; pytest -q | tail -3 || echo fail")],
            [], "pipefail stopped protecting the implicit form")

    def test_a_range_WITH_a_pathspec_still_fires(self):
        """The other half of the same mistake: `--` anywhere suppressed, so
        `git diff base..head -- path` — a GENUINE two-dot range that merely
        also carries a pathspec — went silent. A `--` makes a token a pathspec
        only when the token sits AFTER it."""
        self.assertIn("two-dot-lane-diff",
                      [s for s, _ in chat.argv_steers(
                          "git diff origin/main..HEAD -- src/app.py")],
                      "a real range was muted by its own pathspec")
        # NEGATIVE CONTROL: a dotted PATH after `--` stays silent.
        self.assertEqual(
            [s for s, _ in chat.argv_steers("git diff -- src/generated..bak")],
            [], "a pathspec containing dots was read as a rev range")

    def test_a_BLOCKED_command_gets_its_cure_and_no_steer(self):
        """A blocked command never runs, so its cure is the only thing worth
        saying. Stacking advice on a refusal buries the refusal."""
        rc, out = self.run_hook(
            'helm chat post "see `git diff origin/main..HEAD`"',
            session="blocked-1")
        self.assertEqual(rc, 2)
        self.assertIn("BLOCKED", out)
        self.assertNotIn("[helm steer]", out,
                         "a steer competed with a block for attention")

    # -- the other two rungs, each with its negative control -----------------

    def test_a_typed_ledger_id_is_heard_and_an_interpolated_one_is_not(self):
        rc, out = self.run_hook(
            "helm dispatch verdict a1b2c3d4e5f6 deadbeef --approve ok",
            session="hex-1")
        self.assertIn("TYPED RATHER THAN INTERPOLATED", out)
        self.assertEqual(rc, 0)
        rc, out = self.run_hook(
            'helm dispatch verdict "$ROW" "$TIP" --approve ok', session="hex-2")
        self.assertNotIn(  # noqa: VACUOUS_ASSERTION — P1b/P2 kill this arm
            "[helm steer]", out,
            "the cure itself — passing a variable — was flagged")

    def test_a_pipeline_exit_read_is_heard_and_a_bare_pipeline_is_not(self):
        rc, out = self.run_hook("pytest -q | tail -3 ; rc=$?", session="pipe-1")
        self.assertIn("EXIT STATUS IS ITS LAST STAGE", out)
        self.assertEqual(rc, 0)
        rc, out = self.run_hook("pytest -q | tail -3", session="pipe-2")
        self.assertNotIn("[helm steer]", out,
                         "a pipeline whose status is never read was flagged")

    def test_the_steer_table_never_raises_on_hostile_input(self):
        """FAIL-OPEN TOTAL, like every rung on this surface: a guard on EVERY
        Bash call that can throw wedges the fleet."""
        for cmd in ("", None, "\x00\xff", "a" * 20000, "git diff " + "../" * 500):
            self.assertIsInstance(chat.argv_steers(cmd), list)



class SidechainBeaconArmGuardTest(unittest.TestCase):
    """task/2542 door (1): a subagent may not arm, replace or stop its seat's
    inbox beacon, and only the PreToolUse payload can say it is a subagent.

    THE FAILURE MODE (task/2542, measured on a live seat). A background
    subagent compacts, helm's SessionStart hooks tell it "you are seat X ...
    MANDATORY FIRST ACTION: arm your idle-wake beacon", it arms a Monitor, is
    refused as a duplicate, and re-arms with --replace, which SIGTERMs the
    main conversation's beacon. Claude Code routes a Monitor's events to the
    agent that created the Monitor, so once the subagent ends every wake line
    goes to a dormant sidechain while `helm beacons` reads "covered live 1".

    THE DISCRIMINATOR IS agent_id ON THE PreToolUse PAYLOAD. Read from the
    bytes of Claude Code 2.1.270: the shared hook-input builder sets
    agent_id = toolUseContext.agentId, createSubagentContext always sets that
    field, and the main REPL context never does. A subagent's environ,
    session_id and transcript_path are the main thread's, so nothing else in
    the payload or in the process can tell the two apart.

    WHY THESE ARMS MEASURE PRESENCE. This rung walked the shell once —
    heredoc openers, delimiter words, wrapper scripts, pipeline stages that
    own a document — and the walk is what hid a real arm, twice: a quoted
    FAKE opener handed the lines below it to a document the shell never
    opens and the excision cut a real arm standing among them, and a
    continuation inside a literal document destroyed its terminator and
    swallowed the arm beneath it. A guard whose miss costs the seat its wake
    route does not get to be the more precise reader. So the rung asks
    whether the folded TEXT holds the three pieces — the word `chat`, the
    word `wait`, and a beacon flag — and every spelling those two walks
    argued about is one refusal here.

    These arms drive the shipped hook entry (`cmd_argv_guard` reading a real
    payload on stdin), never a copy of its matcher.
    """

    SEAT = "s1"
    ARM = "helm chat wait --seat s1 --follow"

    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="helm-test-sidechain-guard-")
        self.addCleanup(shutil.rmtree, tmp, True)
        env = mock.patch.dict(_os.environ, {
            "HELM_CHAT_NAME": self.SEAT,
            "HELM_CHAT_DIR": _os.path.join(tmp, "chat")})
        env.start()
        self.addCleanup(env.stop)

    def guard(self, command, agent_id="a1b2", tool="Monitor", **tool_input):
        """(rc, stdout+stderr) of the installed PreToolUse hook verb."""
        payload = {"hook_event_name": "PreToolUse", "tool_name": tool,
                   "session_id": "sess-main", "cwd": "/tmp/repo",
                   "tool_use_id": "toolu_1",
                   "tool_input": dict(tool_input, command=command)}
        if agent_id is not None:
            payload["agent_id"] = agent_id
            payload["agent_type"] = "general-purpose"
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = chat.cmd_argv_guard([])
        return rc, out.getvalue() + err.getvalue()

    def test_a_subagent_monitor_arming_the_seat_beacon_is_refused(self):
        rc, out = self.guard(self.ARM)
        self.assertEqual(rc, 2, out)
        self.assertIn("subagent", out)
        self.assertIn("seat 's1'", out)
        self.assertIn("main conversation", out)

    def test_an_arm_whose_pieces_stand_in_TWO_READINGS_is_refused(self):
        """THE SIBLING OF THE ACTIONS ROW, one function away and with more
        to lose. This rung's three pieces were counted inside ONE folded
        reading at a time, so an arm whose pieces stood in two of them was
        discarded piece by piece: `ch\\at` spells the word only where the
        backslash is DELETED, and `y$'\\n'wait` spells the other only where
        the escape is DECODED (deleted, it reads `ynwait`, and the words are
        whole words). The readings are counted together now.

        A MISS HERE COSTS THE SEAT ITS WAKE ROUTE, which is why this rung
        may not be the more precise reader; the control is the same command
        with one piece removed, which must still pass, because a rung that
        refused every subagent command holding `chat` would be a wall."""
        witness = "helm ch\\at y$'\\n'wait --follow"
        readings = chat.folded_commands(witness)
        pieces = chat._BEACON_WORDS + (chat._BEACON_FLAGS,)
        self.assertEqual(len(pieces), 3)
        self.assertGreater(len(readings), 1, readings)
        # ONE unconditional assertion, positive in its first half: every
        # reading holds SOME piece of the arm, and none holds it whole.
        holding = [len([r for r in readings
                        if any(piece.search(r) for piece in pieces)]),
                   len([r for r in readings
                        if all(piece.search(r) for piece in pieces)])]
        self.assertEqual(holding, [len(readings), 0], readings)
        rc, out = self.guard(witness)
        self.assertEqual(rc, 2, out)
        self.assertIn("subagent", out)
        # CONTROL: the same spelling without the second word still passes
        self.assertEqual(self.guard("helm ch\\at y --follow"), (0, ""))

    def test_the_beacon_rung_takes_the_SAME_fold_cures(self):  # noqa: VACUOUS_ASSERTION — the first assertion is the unconditional refusal control on this hook entry, and every arm below asserts rc 2 or the (0, "") main-thread value
        """THE SIBLING TAKES THE FOLD CURES IN THE SAME PASS, because it
        reads the same text through the same function and its miss costs the
        seat its wake route rather than a paid runner. `\\cX` is an ANSI-C
        escape bash decodes to a TAB, and a TAB SPLITS a glued arm back into
        the words the shell runs: `eval $\'helm chat\\cIwait --follow\'` armed
        a real beacon and this rung read `chatciwait`, one word holding
        neither, and returned 0.

        AND THE MARK IS THE OTHER HALF OF THE SAME FOLD. The Actions pieces
        spanned the expansion mark and these three did not, so
        `helm ch$(echo at) wait --follow` folded to `helm ch<mark> wait
        --follow`, held no whole word, and ARMED A REAL BEACON from a
        subagent — while the Actions rung refused the neighbouring
        `helm chat wa"$(echo it)" --follow` through its own `watch`. One
        rung reading the mark and its sibling ignoring it is two readers of
        one fold, and the miss here costs the seat its wake route, so these
        three are built the way an Actions piece is built now: the same span
        construction, their own edges (`[\\w-]`, no dot, so `bin/helm.chat`
        still reads as the word).

        The controls this rung always owes: a subagent command holding fewer
        than the three pieces still passes, because a rung that refused
        every mention of `chat` would be a wall; a word supplied WHOLE by a
        runtime value is outside this rung as it is outside its sibling; and
        a substitution that SPELLS the words was never that case, because
        the plainest reading holds the text inside it."""
        rc, out = self.guard(self.ARM)
        self.assertEqual(rc, 2, out)
        for witness in ("eval $'helm chat\\cIwait --follow'",
                        "bash -c $'helm chat wait\\cI--follow'",
                        "helm chat wait$'\\cI'--follow"):
            rc, out = self.guard(witness)
            self.assertEqual(rc, 2, (witness, out))
            self.assertIn("subagent", out)
            # the MAIN THREAD is untouched by every one of them
            self.assertEqual(self.guard(witness, agent_id=None), (0, ""))
        # THE CURED GAP: a beacon word assembled around an expansion is the
        # word it assembles to, in all three pieces, quoted or not
        for gap in ('helm ch"$(echo at)" wait --follow',
                    "helm ch$(echo at) wait --follow",
                    "helm chat wa${x}it --follow",
                    "helm chat wait --fol$(echo low)",
                    "helm chat wait --re`echo place`"):
            rc, out = self.guard(gap)
            self.assertEqual(rc, 2, (gap, out))
            self.assertIn("subagent", out)
            # the MAIN THREAD is untouched by every one of them
            self.assertEqual(self.guard(gap, agent_id=None), (0, ""), gap)
        # AND THIS RUNG IS NOW THE ONLY ONE THAT SEES THEM. The Actions
        # rung refused `helm chat wa"$(echo it)" --follow` while its scoped
        # rule fired on any piece — `wa<mark>` reads as the run verb
        # `watch` — and it stopped when that rule narrowed to the rows'
        # ANCHORS, `watch` being an ordinary English word and no row's
        # anchor. That is the right place for the cost: the hazard in this
        # command is a deaf seat, not a paid runner, and the main thread may
        # run it.
        rc, out = self.guard('helm chat wa"$(echo it)" --follow')
        self.assertEqual(rc, 2, out)
        self.assertIn("subagent", out)
        self.assertEqual(
            self.guard('helm chat wa"$(echo it)" --follow', agent_id=None),
            (0, ""))
        # CONTROLS: two pieces and no flag, an expansion beside no piece,
        # and a whole word from a runtime VALUE, which no reading holds
        for allowed in ("eval $'helm chat\\cIwait'", "echo $HOME",
                        "helm $V wait --follow",
                        "helm ch$(echo at) wait"):
            self.assertEqual(self.guard(allowed), (0, ""), allowed)
        # …and the other direction of that boundary: a substitution whose
        # TEXT spells the words is refused by the plainest reading
        rc, out = self.guard("$(echo helm chat wait) --follow")
        self.assertEqual(rc, 2, out)

    def test_the_identical_call_from_the_main_thread_passes_silently(self):
        """THE MUST-HIT TWIN. The same payload minus agent_id is the main
        thread, whose payload carries no agent_id key at all. An EMPTY
        agent_id is missing evidence, and missing evidence is not evidence of
        a sidechain, so both must leave the call exactly as it was: rc 0 and
        not one byte of output."""
        rc, out = self.guard(self.ARM)
        self.assertEqual(rc, 2, "control: the harness can see a refusal")
        self.assertEqual(self.guard(self.ARM, agent_id=None), (0, ""))
        self.assertEqual(self.guard(self.ARM, agent_id=""), (0, ""))

    def test_every_spelling_of_the_arm_is_refused_from_a_subagent(self):  # noqa: VACUOUS_ASSERTION — the observable is compared to the full non-empty spelling tuple, so an empty result fails
        """The guard reads a command string, so every way a shell can run the
        beacon counts: a guard must treat a wrapper as the thing it wraps.
        This is the opposite of the reaper's exact-token rule, on purpose: a
        reaper that over-matches kills a stranger, a guard that under-matches
        lets the seat go deaf."""
        spellings = (
            ("Monitor", "helm chat wait --seat s1 --follow --replace", {}),
            ("Monitor", "/x/helm-wt/seats/s1/bin/helm chat wait --seat s1 "
                        "--follow", {}),
            ("Monitor", "python3 -m helm chat wait --seat s1 --follow", {}),
            ("Bash", "helm chat wait --seat s1 --follow",
             {"run_in_background": True}),
            ("Bash", "bash -c 'cd /x && helm chat wait --seat s1 --follow'",
             {}),
            ("Monitor", "bash -lc \"helm chat wait --seat s1 --follow "
                        "--replace\"", {}),
            ("Monitor", "HELM_CHAT_NAME=s1 timeout 3600 helm chat wait "
                        "--seat s1 --follow 2>&1", {}),
        )
        refused = [c for t, c, x in spellings
                   if self.guard(c, tool=t, **x)[0] == 2]
        self.assertEqual(refused, [c for _t, c, _x in spellings])

    def test_the_walks_that_hid_a_real_arm_are_one_refusal_now(self):  # noqa: VACUOUS_ASSERTION — every tuple arm executes an unconditional rc-2 assertion, and the control below asserts rc 0 on a command holding no flag
        """THE TWO MISSES, and the shape that ends them.

        A QUOTED FAKE OPENER. The shell opens no heredoc inside a quoted
        word, so `echo "<<'EOF'"` is text; read as an opener it handed every
        line below it to a document that never exists, the excision cut
        those lines as data, and a real arm standing among them was never
        seen.

        A CONTINUED DELIMITER, both ways. Bash removes a backslash-newline
        before it reads anything, so `cat <<EO\\` with an `F` beneath opens
        on EOF and an arm after the real EOF is a command; but bash does NOT
        remove the pair inside a literal document, where joining it destroys
        a terminator and the document then swallows the arm below it. One
        join cannot be right in both places, and the walk had to choose.

        Presence chooses neither: the arm's words are in the text in every
        one of these, so every one is refused. A DOCUMENT that merely
        carries the words is refused with them — that is the accepted cost,
        and the control at the end is what shows the rung is still reading
        the flag rather than refusing everything."""
        arm = "helm chat wait --seat s1 --follow"
        for cmd in (
                # the fake opener, with a real arm below it
                "echo \"<<'EOF'\"\n" + arm,
                # the continued delimiter, with a real arm after the real EOF
                "cat <<EO\\\nF\nhello\nEOF\n" + arm,
                # the same pair inside a literal document, above the arm
                "cat <<'EOF'\nx \\\nEOF\n" + arm,
                # a document that is only DATA, and carries the words
                "cat <<'EOF'\n" + arm + "\nEOF",
                "bash -c 'cat > notes.md' <<'EOF'\n" + arm + "\nEOF",
                "bash notes.sh <<'EOF'\n" + arm + "\nEOF",
                # a comment, which the walk had to decide was not a quote
                "# Don't execute the example.\ncat <<'EOF'\n" + arm + "\nEOF",
                "echo hi # " + arm,
                # an unterminated document
                "cat <<NOPE\n" + arm,
                # a delimiter quoted in its middle
                "cat <<'DATA'-END\n" + arm + "\nDATA-END"):
            self.assertEqual(self.guard(cmd, tool="Bash")[0], 2, cmd)
        # CONTROL: the same document shape with a wait that arms nothing —
        # no flag, no refusal, which is what proves these arms read the
        # command and not merely the word `cat`
        self.assertEqual(
            self.guard("cat <<'EOF'\nhelm chat wait --any --room r\nEOF",
                       tool="Bash"), (0, ""))

    def test_a_wait_that_registers_no_beacon_is_never_refused(self):  # noqa: VACUOUS_ASSERTION — the empty blocked list is the contract; the unconditional first assertion shows the same hook entry refusing the arm on this harness
        """`--any` without --follow reads a room and touches no cursor; a
        wait without --follow is a one-shot delivery; neither registers a
        beacon, so neither can take the seat's wake route. This is the
        spelling a subagent actually needs, and it is the direction the
        superset must not swallow."""
        rc, out = self.guard(self.ARM, tool="Bash")
        self.assertEqual(rc, 2, "control: the harness can see a refusal")
        allowed = (
            ("Bash", "helm chat wait --any --room r"),
            ("Monitor", "helm chat wait --any --room r --timeout 600"),
            ("Bash", "helm chat wait --seat s1 --timeout 30"),
            ("Bash", "helm chat --room helm wait --any"),
            ("Bash", "bash --norc -c 'helm chat wait --any --room r'"),
            ("Bash", "helm chat wait --any --room r \\\n  --timeout 60"),
            ("Bash", "helm chat --room helm read --follow"),
            ("Bash", "helm chat post --room r 'the gate is green'"),
            ("Monitor", "tail -F /tmp/x.log"),
            ("Bash", "git log --follow -- helm/chat.py"),
        )
        blocked = [c for t, c in allowed if self.guard(c, tool=t)[0] != 0]
        self.assertEqual(blocked, [])

    def test_a_mention_is_refused_with_the_act_and_the_refusal_says_so(self):
        """WHAT THE SHAPE COSTS, asserted. A subagent that only writes ABOUT
        the beacon is refused with one that arms it: a quoted mention, a
        grep, and a non-beacon wait that shares a command with an unrelated
        `--follow`. The refusal names the cure — say it without the flag
        word, or write it with a tool that is not a shell — because a
        refusal an operator cannot act on is a wedge.

        The last control is the seat's own main thread, which is untouched:
        it is the conversation that OWNS the beacon."""
        rc, out = self.guard(self.ARM, tool="Bash")
        self.assertEqual(rc, 2, "control: the harness can see a refusal")
        for cmd in ("helm chat post 'arm it: helm chat wait --seat s1 "
                    "--follow' --room r",
                    "git grep -n \"chat wait --seat s1 --follow\"",
                    "helm chat wait --any --room r; tail --follow log"):
            rc, out = self.guard(cmd, tool="Bash")
            self.assertEqual(rc, 2, cmd)
            self.assertIn("say it without the flag word", out)
            self.assertIn("without the flag word", out)
            self.assertEqual(self.guard(cmd, agent_id=None, tool="Bash"),
                             (0, ""), cmd)

    def test_replace_from_a_subagent_is_refused_though_its_name_is_the_seat(self):
        """actors.replacement_authority CANNOT SEE THIS, AND A REFACTOR MUST
        NOT MOVE THE RULE BACK INTO THE BEACON PROCESS.

        A subagent inherits the pane's whole environ, HELM_CHAT_NAME
        included (measured: the environ key set of codex-5's subagent-armed
        beacon equals that of codex-4's main-armed one). So the identity layer
        answers DECLARED and admits the rotation, and the `helm chat wait`
        process that --replace starts is a child of a shell with that same
        environ. Only the harness knows which agent issued the tool call, and
        it says so in one place: agent_id on the PreToolUse payload. That is
        why the refusal lives at this guard and nowhere else."""
        from helm import actors
        self.assertEqual(
            actors.replacement_authority(self.SEAT, session="sess-main"),
            (self.SEAT, None),
            "the identity layer admits the rotation; that blind spot is what "
            "this arm exists to pin")
        replace = self.ARM + " --replace"
        rc, out = self.guard(replace)
        self.assertEqual(rc, 2, out)
        self.assertIn("subagent", out)
        self.assertEqual(self.guard(replace, agent_id=None), (0, ""))

    # Four ordinary spellings ran the beacon past this rung when it read the
    # shell. Each arm drives the shipped hook entry for Monitor AND Bash from
    # a subagent, and pairs every spelling with the identical main-thread
    # call, which must stay exactly as it was: rc 0 and no output.

    def assert_refused_only_from_a_subagent(self, spellings):
        rc, out = self.guard(self.ARM)
        self.assertEqual(rc, 2, "control: the harness can see a refusal")
        calls = [(tool, command) for command in spellings
                 for tool in ("Monitor", "Bash")]
        main = [(t, c) for t, c in calls if self.guard(c, agent_id=None, tool=t)
                != (0, "")]
        self.assertEqual(main, [], "a main-thread call changed")
        missed = [(t, c) for t, c in calls if self.guard(c, tool=t)[0] != 2]
        self.assertEqual(missed, [])

    def test_a_line_continuation_inside_the_arm_is_refused(self):  # noqa: VACUOUS_ASSERTION — the helper's first assertion is the unconditional refusal control on this hook entry, and each spelling tuple is non-empty
        """Bash deletes a backslash-newline before it splits words, so the
        continued command is one `helm chat wait` with --follow, and the fold
        joins the pair before anything reads the text."""
        self.assert_refused_only_from_a_subagent((
            "helm chat wait \\\n  --seat s1 --follow",
            "helm chat wait --seat s1 \\\n  --follow --replace",
            "cd /x && helm chat wait \\\n--seat s1 \\\n--follow",
            "helm chat wait --seat s1 --fo\\\nllow",
        ))

    def test_a_two_character_quote_mark_inside_the_arm_is_refused(self):  # noqa: VACUOUS_ASSERTION — the helper's first assertion is the unconditional refusal control on this hook entry, and each spelling tuple is non-empty
        """ONE FOLD DEFECT WAS TWO FALSE ALLOWS. Both deny rungs read the
        same folded text, so the `$` of `$'` and `$"` standing inside a word
        blinded this one exactly as it blinded the Actions rung: `helm
        ch$''at wait --room helm --follow` folded to `helm ch$at …`, which
        holds no word `chat`, and the beacon it arms is the seat's. The mark
        goes with its `$` now, and these spellings are the arm."""
        self.assert_refused_only_from_a_subagent((
            "helm ch$''at wait --room helm --follow",
            "helm chat wa$''it --seat s1 --follow",
            "helm chat wait --seat s1 --fo$''llow",
            'helm ch$""at wait --seat s1 --follow --replace',
        ))

    def test_shell_options_before_the_command_string_are_refused(self):  # noqa: VACUOUS_ASSERTION — the helper's first assertion is the unconditional refusal control on this hook entry, and each spelling tuple is non-empty
        """`bash --norc -c`, `bash -o pipefail -c` and `bash -c -- CMD` all
        run CMD. The wrapper reader stopped at the first long option or
        option argument and never reached -c; nothing here reads -c at
        all."""
        self.assert_refused_only_from_a_subagent((
            "bash --norc -c 'helm chat wait --seat s1 --follow'",
            "bash --login -c 'helm chat wait --seat s1 --follow'",
            "bash -o pipefail -c 'helm chat wait --seat s1 --follow'",
            "bash -c -- 'helm chat wait --seat s1 --follow --replace'",
            "bash --rcfile /dev/null -e -c 'helm chat wait --seat s1 --follow'",
            "sh +o posix -c 'helm chat wait --seat s1 --follow'",
        ))

    def test_a_heredoc_a_shell_reads_as_its_script_is_refused(self):  # noqa: VACUOUS_ASSERTION — the helper's first assertion is the unconditional refusal control on this hook entry, and each spelling tuple is non-empty
        """A quoted heredoc tag makes the body data for the COMMAND, and the
        walk excised it as data. When that command is a shell with no -c and
        no script file, the body is the shell's script and runs — which is
        the distinction this rung no longer has to make, because both are
        text."""
        self.assert_refused_only_from_a_subagent((
            "bash <<'EOF'\nhelm chat wait --seat s1 --follow\nEOF",
            "sh <<\"EOF\"\ncd /x\nhelm chat wait --seat s1 --follow\nEOF",
            "zsh <<EOF\nhelm chat wait --seat s1 --follow\nEOF",
            "bash -s <<'EOF' 2>&1\nhelm chat wait --seat s1 --follow --replace\nEOF",
            "cat <<'EOF' | bash\nhelm chat wait --seat s1 --follow\nEOF",
        ))

    def test_room_before_the_wait_verb_is_refused(self):  # noqa: VACUOUS_ASSERTION — the helper's first assertion is the unconditional refusal control on this hook entry, and each spelling tuple is non-empty
        """cmd_chat deletes the first `--room R` wherever it stands before it
        reads the verb, so `helm chat --room R wait --follow` is the arm —
        and presence does not depend on what stands between the words."""
        self.assert_refused_only_from_a_subagent((
            "helm chat --room helm wait --seat s1 --follow",
            "helm chat --room helm wait --seat s1 --follow --replace",
            "bash -lc 'helm chat --room r wait --follow'",
        ))

    def test_the_words_are_whole_words_and_the_flag_is_a_whole_flag(self):
        """The superset has an edge, and it is the one a false refusal would
        cross. `HELM_CHAT_NAME=s1` is not the word `chat` and `--follow-tags`
        is not the flag, so a subagent that exports the seat's own name or
        pushes tags beside a wait is not arming anything. Each control below
        is paired with the same command carrying the real three pieces, so
        the pair measures the boundary rather than one side of it."""
        rc, out = self.guard(self.ARM, tool="Bash")
        self.assertEqual(rc, 2, "control: the harness can see a refusal")
        for allowed, refused in (
                ("HELM_CHAT_NAME=s1 helm beacons --follow",
                 "HELM_CHAT_NAME=s1 helm chat wait --follow"),
                ("git push --follow-tags && helm chat wait --any",
                 "git push --follow-tags && helm chat wait --follow"),
                ("helm chat read --room r; await the gate",
                 "helm chat read --room r; helm chat wait --replace")):
            self.assertEqual(self.guard(allowed, tool="Bash"), (0, ""), allowed)
            self.assertEqual(self.guard(refused, tool="Bash")[0], 2, refused)


class HookEntryBudgetTest(unittest.TestCase):
    """WHAT THE PreToolUse GATE IS ALLOWED TO COST, bound structurally.

    The owner read a seat this morning where this hook printed THE GUARD TIMED
    OUT several times per turn on every project, against a 2 s budget. Two
    things were paying for it, and this class pins the one that lives in this
    repo: `cmd_chat`'s prologue imported `helm.seats` unconditionally to derive
    a default ROOM, and the argv-guard reads one Bash command string and never
    touches a room, a roster or a seat. That import measured 93 ms of CPU on
    EVERY Bash and Monitor tool call, roughly half this hook's whole cost.

    A WALL-CLOCK OR CPU THRESHOLD IS THE WRONG ARM and was tried first: the hub
    this runs on sits between load 15 and 30, so the same code measures 0.08 s
    and 3.0 s an hour apart, and the arm would either flap or be set so loose
    it asserts nothing. The cure is structural, so the assertion is structural:
    the hook verb must not IMPORT the machinery it does not use. That is
    load-independent, and it fails for the exact reason the budget would.

    EVERY ARM HERE RUNS THE REAL ENTRY SCRIPT as a subprocess, because the
    property is about module-import side effects that an in-process call
    inside a suite (where `helm.seats` is imported already) structurally
    cannot see.
    """

    ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))

    def run_hook(self, payload):
        import subprocess
        probe = (
            "import json, sys, runpy\n"
            "sys.argv = ['helm', 'chat', 'argv-guard', '--hook-json']\n"
            "rc = 0\n"
            "try:\n"
            "    runpy.run_path(%r, run_name='__main__')\n"
            "except SystemExit as e:\n"
            "    rc = e.code or 0\n"
            "sys.stderr.write('PROBE ' + json.dumps({'rc': rc, 'mods': sorted(\n"
            "    m for m in sys.modules if m.startswith('helm.') or '.' not in m)})\n"
            "    + '\\n')\n"
            % _os.path.join(self.ROOT, "bin", "helm"))
        p = subprocess.run([sys.executable, "-c", probe], input=payload,
                           capture_output=True, text=True,
                           env=dict(_os.environ, HELM_NO_TREE_WARNING="1"))
        line = [l for l in p.stderr.splitlines() if l.startswith("PROBE ")]
        self.assertTrue(line, "the probe never reported: " + p.stderr[-800:])
        report = json.loads(line[-1][len("PROBE "):])
        return report["rc"], set(report["mods"]), p.stderr

    def payload(self, command):
        return json.dumps({"tool_name": "Bash", "session_id": "budget-arm",
                           "tool_input": {"command": command}})

    def test_the_gate_still_refuses_through_the_entry_it_is_measured_by(self):
        """THE POSITIVE CONTROL FOR EVERY ARM BELOW. A probe that never reached
        the guard would report a beautifully small module set and prove
        nothing; this one exits 2 on a payload the guard must refuse, which is
        only reachable from inside `cmd_argv_guard`."""
        rc, mods, err = self.run_hook(
            self.payload('git commit -m "the `date` phrase"'))
        self.assertEqual(rc, 2, err[-600:])
        self.assertIn("BLOCKED", err)
        self.assertIn("helm.chat", mods,
                      "the guard's own module was never imported, so this "
                      "probe measured a different program")

    def test_a_passing_command_passes_through_the_same_entry(self):
        rc, mods, err = self.run_hook(self.payload("ls -la"))
        self.assertEqual(rc, 0, err[-600:])
        self.assertIn("helm.chat", mods)

    def test_the_gate_does_not_import_the_delivery_machinery(self):  # noqa: VACUOUS_ASSERTION — the two assertIn lines before the loop are an unconditional positive control on the same observable (the probe's module set is real and non-empty), run through the `run_hook` HELPER the walker does not follow
        """`helm.seats` and the fifteen modules behind it are the delivery
        lane's. The argv-guard answers a shell-substitution question about one
        string; on these commands it reads no room and no roster (the
        advisory rungs that do are gated on the command naming a family
        binary or a foreign tree), on BOTH the refusing and the
        passing arm — a gate whose cost depends on its verdict is a gate whose
        budget nobody can size."""
        # UNCONDITIONAL POSITIVE CONTROL, outside the loop and on the SAME
        # observable: the probe's module set really is this program's. Every
        # assertion below is an ABSENCE, and a probe that reported an empty set
        # — a crashed child, a report line nobody wrote — would satisfy all of
        # them perfectly.
        _, control, _ = self.run_hook(self.payload("ls -la"))
        self.assertIn("helm.chat", control)
        self.assertIn("json", control, "the probe reported no stdlib at all")
        for command, expect in (("ls -la", 0),
                                ('git commit -m "the `date` phrase"', 2)):
            with self.subTest(command=command):
                rc, mods, err = self.run_hook(self.payload(command))
                self.assertEqual(rc, expect, err[-400:])
                self.assertNotIn("helm.seats", mods,
                                 "the room prologue is back on the PreToolUse "
                                 "path: 93 ms of CPU on every Bash call")
                for heavy in ("helm.web", "helm.gate", "helm.landreq",
                              "helm.meld", "helm.seat"):
                    self.assertNotIn(heavy, mods, heavy + " is on the hook path")
                # `hookalarm`'s test-runner seam is only sound while a real
                # hook process does NOT import unittest — the seam turns
                # suppression off for any process that has. This is the arm
                # that keeps that claim measured rather than asserted in a
                # comment.
                self.assertNotIn("unittest", mods,
                                 "a production hook imports unittest, so "
                                 "hookalarm's test-runner seam would disable "
                                 "the rate limit in production too")

    def test_an_agent_call_is_answered_without_the_other_rungs_machinery(self):  # noqa: VACUOUS_ASSERTION — each subTest first asserts the entry's rc (2 on the refusing arm, reachable only inside cmd_argv_guard) and the guard's own module in the same module set
        """THE AGENT RUNG IS ANSWERED BY THE REAL ENTRY, and on both arms it
        loads nothing the Bash rungs use: an Agent call now pays for this
        hook on every delegation, so its admit path must stay a key lookup.
        The subagent arm carries agent_id, which is the key the sidechain
        rung's lazy import turns on for Bash and Monitor."""
        for tool_input, agent_id, expect in (
                ({"prompt": "p", "model": "haiku"}, None, 2),
                ({"prompt": "p"}, None, 0),
                ({"prompt": "p", "model": ""}, "a1b2", 0)):
            payload = {"tool_name": "Agent", "session_id": "budget-arm",
                       "tool_input": tool_input}
            if agent_id:
                payload["agent_id"] = agent_id
            with self.subTest(tool_input=tool_input, agent_id=agent_id):
                rc, mods, err = self.run_hook(json.dumps(payload))
                self.assertEqual(rc, expect, err[-600:])
                self.assertIn("helm.chat", mods)
                if expect == 2:
                    self.assertIn("Agent called with model='haiku'", err)
                for heavy in ("helm.seats", "helm.actors", "helm.web",
                              "helm.gate", "helm.meld", "helm.seat"):
                    self.assertNotIn(heavy, mods,
                                     heavy + " is on the Agent hook path")

    def test_an_agent_call_reads_no_reflex_even_from_a_subagent(self):  # noqa: VACUOUS_ASSERTION — each subTest asserts rc 0 and helm.chat PRESENT in the same module set the absences read, so an empty probe fails
        """task/2971: the nested-spawn steer moved to SubagentStart, so the
        Agent rung is a key lookup again for EVERY caller, a subagent's
        included: no reflex store read, no steer."""
        from helm import reflex
        path = reflex.write({"id": "probe-fanout", "signal": "nested-spawn",
                             "steer": "probe steer: do the bounded work"})
        self.addCleanup(_os.remove, path)
        for agent_id in ("aprobe01", None):
            payload = {"tool_name": "Agent", "session_id": "budget-nested",
                       "tool_input": {"prompt": "p"}}
            if agent_id:
                payload["agent_id"] = agent_id
            with self.subTest(agent_id=agent_id):
                rc, mods, err = self.run_hook(json.dumps(payload))
                self.assertEqual(rc, 0, err[-600:])
                self.assertIn("helm.chat", mods)            # control
                self.assertNotIn("probe steer", err)
                for heavy in ("helm.reflex", "helm.store", "helm.seats",
                              "helm.actors", "helm.web", "helm.gate",
                              "helm.meld", "helm.seat", "helm.inject"):
                    self.assertNotIn(heavy, mods,
                                     heavy + " is on the Agent hook path")


class OwnerDoorPostGuardTest(unittest.TestCase):
    """task/2997: AN AGENT NEVER POSTS TO THE OWNER'S WEB DOOR.

    helm web's decision POSTs record the OWNER's verdict and comments, and
    the bearer they demand is CSRF protection: every agent runs as the
    owner's uid, so it can read the bearer off the page and post as him.
    This rung refuses the accidental route, a shell command sending to the
    local decision endpoints. Every arm drives the shipped hook entry.

    The spellings are built at runtime so this FILE holds no whole one: a
    seat grepping it through Bash must not be refused by its own guard."""

    BASE = "http://" + "127.0.0.1" + ":7433"
    VERDICT = "/api/" + "decisions/verdict"
    COMMENT = "/api/" + "decisions/comment"
    QUEUE = "/api/" + "decisions"

    def hook(self, tool, command):
        payload = {"hook_event_name": "PreToolUse", "tool_name": tool,
                   "session_id": "sess-2997", "cwd": "/tmp/repo",
                   "tool_use_id": "toolu_2997",
                   "tool_input": {"command": command}}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = chat.cmd_argv_guard([])
        return rc, out.getvalue() + err.getvalue()

    def assert_refused(self, command, tool="Bash"):
        rc, out = self.hook(tool, command)
        self.assertEqual(rc, 2, (command, out))
        self.assertIn("[helm argv-guard] BLOCKED", out, command)
        self.assertIn("owner decision door", out, command)
        self.assertIn("web queue", out, "the refusal names the owner's doors")
        self.assertIn("helm decide comment", out, "and what a seat does")

    def assert_allowed(self, command):
        rc, out = self.hook("Bash", command)
        self.assertEqual(rc, 0, (command, out))
        self.assertNotIn("BLOCKED", out, command)

    def test_a_curl_post_to_the_decide_endpoint_is_refused(self):  # noqa: VACUOUS_ASSERTION — the allowed read is the positive control; the refusal is asserted by PRESENCE inside assert_refused (rc 2 and the refusal text)
        # POSITIVE CONTROL on the same hook and host: reading the queue passes,
        # so the refusals below are the rule and not a blanket on the URL
        self.assert_allowed("curl -s " + self.BASE + self.QUEUE)
        self.assert_refused(
            "curl -s -X POST " + self.BASE + self.VERDICT
            + " -H 'Authorization: Bearer t' -d '{\"id\":\"x\",\"choice\":\"1\"}'")
        self.assert_refused(
            "curl -G -X POST --data-urlencode 'id=x' "
            + self.BASE + self.QUEUE)

    def test_every_client_and_spelling_of_the_door_is_refused(self):  # noqa: VACUOUS_ASSERTION — the loop walks a literal non-empty tuple and each iteration asserts rc 2 plus the refusal text
        for cmd in (
                "curl --json '{}' http://localhost:7433" + self.COMMENT,
                "wget --post-data='{}' " + self.BASE + self.VERDICT,
                "python3 -c 'import urllib.request as u; u.urlopen(u.Request(\""
                + self.BASE + self.VERDICT + "\", data=b\"{}\"))'",
                "python3 - <<'EOF'\nimport requests\nrequests.post('"
                + self.BASE + "' + '" + self.COMMENT + "', json={})\nEOF",
                # a seat can start its own server, so any loopback port counts
                "curl -X POST http://127.0.0.1:9999" + self.VERDICT,
                # the queue path itself, with a writing method
                "curl -X PUT " + self.BASE + self.QUEUE,
                "curl -s " + self.BASE + self.COMMENT
                + " \\\n  -d '{\"id\":\"x\",\"text\":\"y\"}'"):
            self.assert_refused(cmd)

    def test_inline_and_raw_http_clients_are_refused(self):  # noqa: VACUOUS_ASSERTION — every subTest asserts rc 2 plus the refusal text through the shipped hook
        for cmd in (
                "node -e \"fetch('" + self.BASE + self.COMMENT
                + "',{method:'POST',body:'{}'})\"",
                "bun -e \"fetch('" + self.BASE + self.COMMENT
                + "',{method:'POST',body:'{}'})\"",
                "deno eval \"fetch('" + self.BASE + self.COMMENT
                + "',{method:'POST',body:'{}'})\"",
                "ruby -e \"require 'net/http'; Net::HTTP.post URI('"
                + self.BASE + self.COMMENT + "'), '{}'\"",
                "perl -e \"use LWP::UserAgent; LWP::UserAgent->new->post('"
                + self.BASE + self.COMMENT + "')\"",
                "perl -e \"use HTTP::Tiny; HTTP::Tiny->new->post('"
                + self.BASE + self.COMMENT + "')\"",
                "php -r \"file_get_contents('" + self.BASE + self.COMMENT
                + "', false, stream_context_create(['http'=>['method'=>'POST']]))\"",
                "printf 'POST " + self.COMMENT
                + " HTTP/1.1\\r\\nHost: 127.0.0.1\\r\\n\\r\\n'"
                + " | socat - TCP:127.0.0.1:7433",
                "printf 'POST " + self.COMMENT
                + " HTTP/1.1\\r\\nHost: 127.0.0.1\\r\\n\\r\\n'"
                + " | nc 127.0.0.1 7433",
                "printf 'POST " + self.COMMENT
                + " HTTP/1.1\\r\\nHost: 127.0.0.1\\r\\n\\r\\n'"
                + " | openssl s_client -connect 127.0.0.1:7433",
                "bash -c \"exec 3<>/dev/tcp/127.0.0.1/7433; printf 'POST "
                + self.COMMENT + " HTTP/1.1\\r\\n\\r\\n' >&3\""):
            with self.subTest(command=cmd):
                self.assert_refused(cmd)

    def test_a_leading_get_curl_does_not_vouch_for_a_second_client(self):  # noqa: VACUOUS_ASSERTION — every subTest asserts rc 2 plus the refusal text through the shipped hook
        for cmd in (
                "curl -G " + self.BASE + self.QUEUE + "; curl -d 'x' "
                + self.BASE + self.QUEUE,
                "curl -G " + self.BASE + self.QUEUE + " && python3 -c "
                "\"import requests; requests.post('" + self.BASE + self.QUEUE
                + "', data='x')\""):
            with self.subTest(command=cmd):
                self.assert_refused(cmd)

    def test_a_monitor_command_is_read_by_the_same_rung(self):  # noqa: VACUOUS_ASSERTION — assert_refused asserts PRESENCE: rc 2 and the refusal text
        self.assert_refused("curl -s -X POST " + self.BASE + self.VERDICT,
                            tool="Monitor")

    def test_reads_and_other_doors_pass(self):  # noqa: VACUOUS_ASSERTION — the loop walks a literal non-empty tuple and each iteration asserts rc 0; the refusal arms above are the positive control on the same hook
        for cmd in (
                "curl -s " + self.BASE + self.QUEUE,
                "curl -G --data-urlencode 'state=open' "
                + self.BASE + self.QUEUE,
                "node -e \"fetch('" + self.BASE + self.QUEUE + "')\"",
                "printf 'GET " + self.QUEUE
                + " HTTP/1.1\\r\\nHost: 127.0.0.1\\r\\n\\r\\n'"
                + " | nc 127.0.0.1 7433",
                "git grep -n " + self.VERDICT + " helm/",
                "curl -s -X POST " + self.BASE + "/api/tasks/comment -d '{}'",
                "helm decide comment abc 'a seat adds to the card'"):
            self.assert_allowed(cmd)

    # -- task/3027: THE EVIDENCE OF A SEND IS READ IN THE CURL CALL THAT NAMES
    # THE DOOR. The rung read method words over the whole command, so a GET
    # of the away card beside any other command whose text said "patch." was
    # refused as a write. Rows 1-2 are the only
    # commands this may newly pass; every other row pins a refusal (or a
    # non-curl client's unchanged reading) the cure must keep.
    CARD = "http://" + "127.0.0.1" + ":7481" + "/api/" + "owner/posture"
    WORDS = ("patch", "post", "put", "delete", "PATCH", "POST")

    def assert_door_refused(self, command):
        rc, out = self.hook("Bash", command)
        self.assertEqual(rc, 2, (command, out))
        self.assertIn("[helm argv-guard] BLOCKED", out, command)
        self.assertIn("sends to helm web's owner", out, command)

    def test_3027_row1_a_get_of_the_card_alone_passes(self):  # noqa: VACUOUS_ASSERTION — the first assertion is unconditional (rc 0 on the card GET) and the loop walks a literal non-empty tuple asserting rc 0 each time; row 3's refusals are the positive control on the same hook
        self.assert_allowed("curl -s " + self.CARD)
        for cmd in ("curl -fsS " + self.CARD + " | python3 -m json.tool",
                    "curl -s " + self.BASE + self.QUEUE):
            with self.subTest(command=cmd):
                self.assert_allowed(cmd)

    def test_3027_row2_a_method_word_in_another_command_is_not_a_send(self):  # noqa: VACUOUS_ASSERTION — the measured case is asserted unconditionally (rc 0) first; the loops walk literal non-empty tuples and assert rc 0 per command; the same card with the method on the curl call is refused in row 3
        # THE MEASURED CASE, in its shape: a card read, then a verdict whose
        # evidence string ends "patch."
        self.assert_allowed(
            "curl -s " + self.CARD + "; helm dispatch verdict 3027 approve "
            "--evidence 'docstring-only patch.'")
        for word in self.WORDS:
            for cmd in (
                    "curl -s " + self.CARD + "; echo " + word,
                    "curl -s " + self.CARD + " && helm chat post 'a " + word
                    + " landed'",
                    "curl -s " + self.CARD + " | grep -c " + word,
                    "curl -s " + self.CARD + "\nhelm dispatch verdict 1 "
                    "approve --evidence 'the " + word + " landed'",
                    "curl -s " + self.CARD + " || helm chat post \"no "
                    + word + "; " + word + " later | " + word + " && x\"",
                    "curl -s " + self.BASE + self.QUEUE + " && echo " + word,
                    "curl -G --data-urlencode 'state=open' " + self.BASE
                    + self.QUEUE + "; echo " + word):
                with self.subTest(command=cmd):
                    self.assert_allowed(cmd)

    def test_3027_row3_a_writing_method_on_the_door_is_refused(self):  # noqa: VACUOUS_ASSERTION — every subTest asserts rc 2 plus the refusal text through the shipped hook, over literal non-empty tuples
        for url in (self.CARD, self.BASE + self.QUEUE):
            for flag in ("-X PATCH", "-X POST", "--request=POST",
                         "--request PUT", "-XPOST", "-sXPOST", "-sX PATCH"):
                for cmd in ("curl " + flag + " " + url,
                            "curl -s " + url + " " + flag + "; echo done",
                            "echo start && curl " + flag + " " + url):
                    with self.subTest(command=cmd):
                        self.assert_door_refused(cmd)

    def test_3027_row4_a_body_on_the_door_is_refused(self):  # noqa: VACUOUS_ASSERTION — every subTest asserts rc 2 plus the refusal text through the shipped hook, over literal non-empty tuples
        for url in (self.CARD, self.BASE + self.QUEUE):
            for flag in ("-d x", "--data x", "--data-raw x", "-F a=b",
                         "--json '{}'", "--form a=b", "-T body.json",
                         "-sd x", "-sF a=b", "-fsSd x", "-Fa=b"):
                for cmd in ("curl " + flag + " " + url,
                            "curl -s " + url + " " + flag + " | head -1"):
                    with self.subTest(command=cmd):
                        self.assert_door_refused(cmd)

    def test_3027_row5_a_second_client_that_sends_is_still_refused(self):  # noqa: VACUOUS_ASSERTION — every subTest asserts rc 2 plus the refusal text through the shipped hook
        for cmd in (
                "curl -s " + self.CARD + "; curl -X POST " + self.CARD,
                "curl -s " + self.CARD + " && curl -d x " + self.CARD,
                "curl -G " + self.BASE + self.QUEUE + "\ncurl --json '{}' "
                + self.BASE + self.QUEUE,
                "curl -s " + self.CARD + "; python3 -c \"import requests; "
                "requests.post('" + self.CARD + "', json={})\"",
                "curl -s " + self.CARD + " | wget --post-data=x "
                + self.CARD):
            with self.subTest(command=cmd):
                self.assert_door_refused(cmd)

    def test_3027_row6_the_door_or_the_method_inside_a_substitution_is_refused(self):  # noqa: VACUOUS_ASSERTION — every subTest asserts rc 2 plus the refusal text through the shipped hook
        base = "http://" + "127.0.0.1" + ":7481"
        for cmd in (
                "curl -X POST $(echo " + self.CARD + ")",
                "curl -X PATCH `echo " + self.CARD + "`",
                "curl -d x \"" + base + "$(printf /api/" + "owner/posture)\"",
                "R=$(curl -s -X POST " + self.CARD + "); echo \"$R\"",
                "echo `curl -d x " + self.CARD + "`",
                # text another command holds, which reaches the curl call's
                # own words through an expansion, is still read
                "curl -s " + self.CARD + " -X \"$(echo POST)\"",
                "M=POST; curl -X \"$M\" " + self.CARD,
                "A='--data x'; curl $A " + self.CARD,
                "U=" + self.CARD + "; curl -X POST \"$U\"",
                # and so is a config curl reads, from its stdin or its rc
                "echo 'request = POST' | curl -K - " + self.CARD,
                "echo 'request = POST' | curl -sK - " + self.CARD,
                "echo 'request = POST' > ~/.curlrc; curl -s " + self.CARD):
            with self.subTest(command=cmd):
                self.assert_door_refused(cmd)

    def test_3027_row7_a_write_subpath_is_refused_on_sight(self):  # noqa: VACUOUS_ASSERTION — every subTest asserts rc 2 plus the decision-door refusal through the shipped hook
        for cmd in ("curl -s " + self.BASE + self.VERDICT,
                    "echo hi; curl -s " + self.BASE + self.COMMENT,
                    "curl -s " + self.CARD + "; echo " + self.VERDICT):
            with self.subTest(command=cmd):
                self.assert_refused(cmd)

    def test_3027_row8_other_clients_keep_their_whole_command_reading(self):  # noqa: VACUOUS_ASSERTION — the loops walk literal non-empty tuples; the allowed arm asserts rc 0 and the refused arm rc 2 plus the refusal text, on the same clients and door
        # a plain read passes, as it did
        for cmd in ("wget -qO- " + self.CARD,
                    "python3 -c \"import urllib.request as u; "
                    "print(u.urlopen('" + self.CARD + "').read())\"",
                    "node -e \"fetch('" + self.CARD + "')\""):
            with self.subTest(command=cmd):
                self.assert_allowed(cmd)
        # and a method word ANYWHERE in the command still refuses: only a
        # curl call is read by its own words (task/3027)
        for cmd in ("wget -qO- " + self.CARD + "; echo patch",
                    "node -e \"fetch('" + self.CARD + "')\" && echo post",
                    "wget --post-data=x " + self.CARD):
            with self.subTest(command=cmd):
                self.assert_door_refused(cmd)



class OwnerPostureForgeGuardTest(unittest.TestCase):
    """task/3018: AN AGENT NEVER MINTS THE OWNER'S DOOR OR HAND-WRITES HIS
    POSTURE.

    The away flag and the fleet notice reach every seat's context as the
    owner's word. Every agent runs as his uid, so a one-liner that mints an
    OwnerDoor, or a redirect onto the flag or the notice file, renders as him.
    This rung refuses the ACCIDENTAL route in the command text: a mint in a
    command that runs anything other than a search or a read, and a write
    spelling aimed at either file. A deliberate process (a script file) is
    outside the threat model; ownerasks and ownernotice say so.

    The spellings are built at runtime so this FILE holds no whole one."""

    MINT = "owner" + "_door"
    FLAG = "/dev/shm/helm-chat/" + "owner-" + "away"
    NOTE = "/dev/shm/helm-chat/" + "owner-" + "notice"

    def hook(self, tool, command=None, path=None):
        tool_input = {"file_path": path, "content": "x"} if path \
            else {"command": command}
        payload = {"hook_event_name": "PreToolUse", "tool_name": tool,
                   "session_id": "sess-3018", "cwd": "/tmp/repo",
                   "tool_use_id": "toolu_3018", "tool_input": tool_input}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = chat.cmd_argv_guard([])
        return rc, out.getvalue() + err.getvalue()

    def assert_refused(self, command, tool="Bash", path=None):
        rc, out = self.hook(tool, command, path)
        self.assertEqual(rc, 2, (command or path, out))
        self.assertIn("[helm argv-guard] BLOCKED", out, command or path)
        self.assertIn("owner's word", out, "the refusal says whose word")
        self.assertIn("helm away status", out, "and names the read")

    def assert_allowed(self, command, tool="Bash", path=None):
        rc, out = self.hook(tool, command, path)
        self.assertEqual(rc, 0, (command or path, out))
        self.assertNotIn("BLOCKED", out, command or path)

    def test_a_mint_in_a_command_that_runs_code_is_refused(self):  # noqa: VACUOUS_ASSERTION — the search control runs first (rc 0 on the same hook), and every refusal asserts rc 2 plus the refusal text by presence
        self.assert_allowed("git grep -n '" + self.MINT + "(' helm/")
        for cmd in (
                "python3 -c 'from helm import away, ownerasks; "
                "away.declare(ownerasks." + self.MINT + "(\"web\"))'",
                "python3 -c 'from helm.ownerasks import " + self.MINT + "; "
                + self.MINT + "(\"web\")'",
                "python3 - <<'EOF'\nfrom helm import ownerasks, ownernotice\n"
                "ownernotice.write_notice('x', ownerasks." + self.MINT
                + "('web'))\nEOF",
                "cd /tmp/repo && python -c \"import helm.ownerasks as o; "
                "o." + self.MINT + "('web')\"",
                "cat <<'EOF' | python3\nfrom helm import ownerasks\n"
                "ownerasks." + self.MINT + "('web')\nEOF",
                "python3 -c 'from helm import ownerasks as o; "
                "o.OwnerDoor(\"web\", mint=o._DOOR_MINT)'"):
            with self.subTest(cmd=cmd):
                self.assert_refused(cmd)

    def test_a_write_onto_the_flag_or_the_notice_is_refused(self):  # noqa: VACUOUS_ASSERTION — the read control runs first (rc 0 on the same hook), and every refusal asserts rc 2 plus the refusal text by presence
        self.assert_allowed("cat " + self.FLAG)
        for path in (self.FLAG, self.NOTE, "$HELM_CHAT_DIR/" + self.NOTE[-12:],
                     '"$HELM_CHAT_DIR"/' + self.NOTE[-12:],      # quoted var
                     "'" + self.FLAG[:-10] + "'" + self.FLAG[-10:]):  # quoted dir
            for cmd in (
                    "echo 'declared by someone (web)' > " + path,
                    "printf '{}' >> " + path,
                    "echo x | tee " + path,
                    "echo x | tee -a " + path,
                    "cp /tmp/forged " + path,
                    "mv /tmp/forged " + path,
                    "install -m 600 /tmp/forged " + path,
                    "ln -s /tmp/forged " + path,
                    "touch " + path,
                    "python3 -c \"open('" + path + "', 'w').write('x')\"",
                    "python3 -c \"import pathlib; pathlib.Path('" + path
                    + "').write_text('x')\""):
                with self.subTest(cmd=cmd):
                    self.assert_refused(cmd)

    def test_a_quoted_name_is_the_same_file(self):  # noqa: VACUOUS_ASSERTION — the two controls (a quoted path to another file) run first at rc 0; each refusal asserts rc 2 plus the refusal text by presence
        """A shell quote around the directory or the name spells the same
        file: "$D"/owner-away, '/dir/'owner-away and /dir/'owner-away' all
        land on the flag. The rung reads through the quote."""
        self.assert_allowed('echo x > "$HELM_CHAT_DIR"/owner-away-log')
        self.assert_allowed("echo x > " + self.FLAG[:-10] + "'notes'")
        d = self.FLAG[:-10]
        for cmd in ('echo x > "$HELM_CHAT_DIR"/owner-away',
                    'echo x >"${HELM_CHAT_DIR}"/owner-notice',
                    'echo x | tee "$D"/owner-notice',
                    "cp /tmp/forged '" + d + "'owner-away",
                    "echo x > " + d + "'owner-notice'",
                    'dd if=/tmp/forged of="$D"/owner-away',
                    "install -m 600 /tmp/forged \"" + d + "\"owner-away"):
            with self.subTest(cmd=cmd):
                self.assert_refused(cmd)

    def test_a_monitor_and_a_write_tool_are_read_too(self):  # noqa: VACUOUS_ASSERTION — assert_refused asserts PRESENCE: rc 2 and the refusal text; the Write control on another file passes on the same hook
        self.assert_refused("printf x > " + self.NOTE, tool="Monitor")
        self.assert_allowed(None, tool="Write", path="/tmp/repo/notes.md")
        self.assert_refused(None, tool="Write", path=self.NOTE)
        self.assert_refused(None, tool="Edit", path=self.FLAG)

    def test_a_mint_that_is_DATA_to_its_program_passes(self):  # noqa: VACUOUS_ASSERTION — every iteration asserts rc 0 over a literal non-empty tuple; the re-executor arm below is the positive control on the same hook and the same words
        """task/2973's fold: a message, a post body, a store statement, a
        search pattern and a document a data program reads never run, so a
        mint spelled there is prose about the rung, not a call."""
        m = self.MINT + "("
        for cmd in (
                "git commit -m 'argv-guard: a seat never mints " + m
                + " in code it runs'",
                "git commit -am 'review: _DOOR_MINT is the card token'",
                "git commit -F - <<'EOF'\nthe rung reads ownerasks."
                + self.MINT + "\nand " + m + "'web')\nEOF",
                "helm chat post --room helm 'finding: the rung reads "
                "ownerasks." + self.MINT + "; " + m + " is refused'",
                "helm chat dm peer-seat 'the guard refuses " + m + "'",
                "helm chat reply abc123 'yes, " + m + "web) is the mint'",
                "helm dispatch send peer-seat lane-x 'refuse " + m + " calls'",
                "helm dispatch verdict abc123 deadbeef --fix 'evidence: "
                + m + " passed the rung'",
                "helm store add prior mint-rule '| the guard refuses " + m + "'",
                "gh pr comment 12 --body 'the guard refuses " + m + "'",
                "gh issue comment 3 --body 'the guard refuses " + m + "'",
                "git grep -n '" + m + "' helm/ && python3 -c 'print(1)'",
                "cat > /tmp/notes.py <<'EOF'\nfrom helm import ownerasks\n"
                "ownerasks." + m + "'web')\nEOF"):
            with self.subTest(cmd=cmd):
                self.assert_allowed(cmd)

    def test_a_re_executor_poisons_the_data_fold(self):  # noqa: VACUOUS_ASSERTION — every iteration asserts rc 2 plus the refusal text by presence over a literal non-empty tuple
        """eval, bash -c, sh -c, python -c, printf -v, source, a substitution
        and xargs into an interpreter run the text they are handed, so the
        same words stay refused there."""
        call = "ownerasks." + self.MINT + "(\\\"web\\\")"
        py = "python3 -c \\\"from helm import ownerasks; " + call + "\\\""
        for cmd in (
                "eval \"" + py + "\"",
                "bash -c \"" + py + "\"",
                "sh -c \"" + py + "\"",
                "python -c 'from helm import ownerasks; ownerasks."
                + self.MINT + "(\"web\")'",
                "printf -v c '%s' 'from helm import ownerasks; ownerasks."
                + self.MINT + "(\"web\")'; python3 -c \"$c\"",
                "source <(echo 'python3 -c \"import helm.ownerasks as o; o."
                + self.MINT + "(1)\"')",
                "echo 'import helm.ownerasks as o; o." + self.MINT
                + "(1)' | xargs -0 python3 -c",
                "helm chat post \"$(python3 -c 'import helm.ownerasks as o; "
                "o." + self.MINT + "(1)')\"",
                "git commit -m 'note' && eval \"python3 -c "
                "'import helm.ownerasks as o; o." + self.MINT + "(1)'\""):
            with self.subTest(cmd=cmd):
                self.assert_refused(cmd)

    def test_a_hand_DELETE_of_the_notice_is_refused_and_of_the_flag_is_not(self):  # noqa: VACUOUS_ASSERTION — each flag delete asserts rc 0 and each notice delete rc 2 plus the refusal text, over literal non-empty tuples on the same hook
        """Clearing the notice is the owner's act (clear_notice is door-gated),
        so a hand delete of it un-says his word fleet-wide. Lifting the away
        FLAG is everyone's by design (away.lift): those deletes pass."""
        for path in (self.FLAG, "\"$HELM_CHAT_DIR\"/" + self.FLAG[-10:]):
            for cmd in ("rm " + path, "rm -f " + path, "unlink " + path,
                        "mv " + path + " /tmp/away-gone",
                        "python3 -c \"import os; os.remove('" + path + "')\"",
                        "python3 -c \"import pathlib; pathlib.Path('" + path
                        + "').unlink()\"",
                        "helm back"):
                with self.subTest(cmd=cmd):
                    self.assert_allowed(cmd)
        # the shell spellings of the notice, quoted as a shell quotes them
        for path in (self.NOTE, "\"$HELM_CHAT_DIR\"/" + self.NOTE[-12:],
                     "/dev/shm/helm-chat/'" + self.NOTE[-12:] + "'"):
            for cmd in ("rm " + path, "rm -f " + self.FLAG + " " + path,
                        "unlink " + path, "truncate -s0 " + path,
                        "shred -u " + path, "mv " + path + " /tmp/gone"):
                with self.subTest(cmd=cmd):
                    self.assert_refused(cmd)
        # the python spellings, with the path as a python string holds it
        for cmd in ("python3 -c \"import os; os.remove('" + self.NOTE + "')\"",
                    "python3 -c \"import os; os.unlink('" + self.NOTE + "')\"",
                    "python3 -c \"import pathlib; pathlib.Path('" + self.NOTE
                    + "').unlink()\""):
            with self.subTest(cmd=cmd):
                self.assert_refused(cmd)
        rc, out = self.hook("Bash", "rm " + self.NOTE)
        self.assertIn("deletes the owner's fleet notice", out)
        self.assertIn("helm back", out, "and names the flag's own door")

    def test_the_names_are_the_writers_own(self):
        from helm import away, ownernotice
        self.assertEqual(chat._POSTURE_NAMES,
                         (away.MARKER_NAME, ownernotice.NOTICE_NAME))

    def test_reads_and_mentions_pass(self):  # noqa: VACUOUS_ASSERTION — the loop walks a literal non-empty tuple and each iteration asserts rc 0; the refusal arms above are the positive control on the same hook
        for cmd in (
                "cat " + self.FLAG + " " + self.NOTE,
                "ls -la " + self.FLAG,
                "stat -c %y " + self.NOTE,
                "helm away status",
                "helm away",
                "helm back",
                "cp " + self.NOTE + " /tmp/notice-backup",
                "echo 'the flag lives at " + self.FLAG + "'",
                "grep -c declared " + self.FLAG + " > /tmp/count",
                "git grep -n " + self.MINT + " -- helm/ tests/",
                "rg -n '" + self.MINT + "\\(' helm",
                "git log -p -S " + self.MINT + " | head",
                "python3 -m unittest tests.test_ownernotice"):
            with self.subTest(cmd=cmd):
                self.assert_allowed(cmd)


if __name__ == "__main__":
    unittest.main()
