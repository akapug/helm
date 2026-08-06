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

A fourth incident (2026-07-29) moved the same hazard one surface out: a
`git commit -m` message lost its backticked phrase to the shell and LANDED
mangled. That class inverts the polarity — the body is a flag VALUE, not
positional — and gets its own test class below (MsgFlagClassTest).
"""
import contextlib
import io
import json
import sys
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
        """Found live by ds4pro post-land: --room=helm already carries its
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
        # 2026-07-29: the shell executed `recip in live` before git saw
        # argv, and the LANDED message read "the assignment test is , and"
        self.assertTrue(self.blocks(
            'git commit -m "board: the assignment test is `recip in live`, '
            'and live means the pane exists"'))

    def test_dollar_paren_in_a_double_quoted_m_value_blocks(self):
        self.assertTrue(self.blocks('git commit -m "landed $(git log -1)"'))
        # the attached long form opens the value at the quote (the ds4pro
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
        self.assertIn("RUNS ON YOUR BOX", out)  # says why it matters

    def test_a_hazardous_commit_blocks_with_the_FILE_cure(self):
        """Each class's block names the safe route for ITS surface: a
        quoted-delimiter stdin heredoc cures a chat post, not a commit — the
        commit cure is the -F file route (or Write producing the file)."""
        rc, out = self.run_hook({"tool_name": "Bash", "tool_input": {
            "command": 'git commit -m "the test is `recip in live`, and"'}})
        self.assertEqual(rc, 2)
        self.assertIn("git commit -F", out)     # the file route, named
        self.assertIn("RUNS ON YOUR BOX", out)
        self.assertNotIn("helm chat post", out)  # not the chat cure

    def test_a_hazardous_verdict_blocks_with_its_value_cure(self):
        rc, out = self.run_hook({"tool_name": "Bash", "tool_input": {
            "command": 'helm dispatch verdict abcdef12 %s --fix '
                       '"finding `helm seat resume`"' % ('a' * 40)}})
        self.assertEqual(rc, 2)
        self.assertIn("immutable verdict evidence", out)
        self.assertIn("evidence=$(cat <<'EOF'", out)
        self.assertIn('--fix "$evidence"', out)
        self.assertNotIn("git commit -F", out)

    def test_a_clean_bash_call_passes(self):
        rc, _ = self.run_hook({"tool_name": "Bash", "tool_input": {
            "command": "helm chat post --room helm <<'EOF'\nsafe\nEOF"}})
        self.assertEqual(rc, 0)

    def test_non_bash_tools_pass_untouched(self):
        rc, _ = self.run_hook({"tool_name": "Edit", "tool_input": {
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

    MEASURED 2026-08-04: `helm store add premise "... NOT a top-level
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

class SpecWiringTest(unittest.TestCase):
    def spec(self):
        from helm import hooks
        return next(s for s in hooks.SPECS if s["name"] == "argv-guard")

    def test_it_is_a_GATE_on_PreToolUse_matching_Bash(self):
        s = self.spec()
        self.assertEqual(s["event"], "PreToolUse")
        self.assertEqual(s["matcher"], "Bash")
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
                      [s["name"] for s in hooks.DELIVERY_SPECS])


if __name__ == "__main__":
    unittest.main()

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

    # -- the acceptance test @opus-integrator bound, BOTH arms --------------

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
        """@codex-3: `set -o pipefail; pytest -q | tail -3 ; rc=$?` still fired
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
        """@codex-3: `git diff -- src/generated..bak` fired two-dot-lane-diff,
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
        """@codex-3: the latch keyed on an 8-CHAR PREFIX of the session id, so
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
        """@codex-3 broke my first suppressor by MOVING the token. It keyed on
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
        # or the widening reintroduced the false positive @codex-3 cured.
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

