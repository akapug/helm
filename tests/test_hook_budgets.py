#!/usr/bin/env python3
"""A CHARACTER BUDGET PER HOOK EVENT, as a number, so bloat cannot creep back.

Owner, P0: "why is this stophook so fuggin long? are our stophooks TRULY
optimized for concision and usefulness each time they run?" — then: treat it as
a general complaint and fix it for all hooks.

WHY A NUMBER AND NOT A REVIEW RULE. Every long message in this estate got long
one true sentence at a time. Each addition was defensible on its own and none
of them could see the others, so the only reader who ever saw the total was the
agent paying for it on every turn — and that reader cannot file a bug. A budget
is the instrument that makes the total visible to the suite instead.

WHAT THESE ARMS DO NOT DO: they never pin prose. A message may be reworded
freely; it may not get longer than its event's budget, lose the fact a reader
cannot reconstruct, or change what the hook DECIDES. The refusal arms below
drive the shipped hook entry and assert the exit code, so a rewording that
accidentally stops refusing reddens here and not in review.

THE BUDGETS ARE CEILINGS WITH HEADROOM, measured against the worst realistic
render of each surface rather than a typical one: the longest live store entry,
a refusal whose offending spelling stands 1,094 characters into a folded
command, a lane name nobody would choose. A ceiling that only the median fits
is a ceiling that reddens for reasons unrelated to bloat.
"""
import contextlib
import io
import json
import os
import re
import sys
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-hookbudget-", var="HELM_HOME")

from helm import actors, chat, punt, resumeturn, saguide  # noqa: E402
from helm import seats_join, toolwhisper  # noqa: E402
from helm.inject import _common, _entries  # noqa: E402
from helm.seats_stop_ndp import ndp_block  # noqa: E402
from helm.seats_stop_signals import spiral_block  # noqa: E402


# ---------------------------------------------------------------------------
# THE BUDGETS. One number per hook event's worst realistic render.
# ---------------------------------------------------------------------------
BUDGETS = {
    # SubagentStart: unconditional for every build-capable child, spent before
    # it reads its own task. The behaviour-changing rules (tests, beacon,
    # sibling sweep, the reviewer's own patch) plus the route to the
    # measurements.
    "SubagentStart/saguide": 560,
    # SubagentStart, after the brief: one live nested-spawn reflex per line.
    # Authored store text at the reflex lane's own width (STEER_CAP 280),
    # plus its prefix and the cut notice when it is cut.
    "SubagentStart/nested-spawn": 320,
    # PreToolUse refusals. The act's own description is interpolated and can
    # be long (a spelling plus its character offset in a folded command), so
    # the ceiling covers the constant prose plus a wide act.
    "PreToolUse/refusal-ci-runner": 470,
    "PreToolUse/refusal-sidechain-beacon": 460,
    # The delegate authority refusal (task/3060): the verb, why a delegate's
    # write is the seat's, the parent, the advisory route and the grant.
    "PreToolUse/refusal-sidechain-authority": 480,
    # The agent-model refusal: the fact, three doors and the premise pointer,
    # with the flag's value echoed at most AGENT_MODEL_ECHO characters wide.
    "PreToolUse/refusal-agent-model": 460,
    # The env-dump refusal (task/3037): what the command prints, its spelling
    # capped at chat._ENV_SPELL, and the cure for that kind.
    "PreToolUse/refusal-env-dump": 420,
    # The shared-checkout refusal (task/3057): the verb capped at
    # chat._TREE_SPELL, the checkout it would run in, the same verb spelled
    # with the lane under that checkout, and the integrator's declaration.
    # The path is said twice, so the ceiling holds an 80-character top.
    "PreToolUse/refusal-shared-checkout": 580,
    # PreToolUse pass-path steers, once per (session, steer).
    "PreToolUse/steer": 450,
    # PreToolUse pass path, EVERYTHING one call is handed at once. The lines
    # ride one envelope, so the per-line ceiling above bounds nothing about
    # a call that trips several.
    "PreToolUse/envelope": 900,
    # SessionStart. The armed banner says nothing is owed and must be the
    # SHORTER of the two; the first-action banner carries a verbatim tool
    # invocation and an absolute guide path, which is most of its budget.
    "SessionStart/join-armed": 330,
    "SessionStart/join-first-action": 480,
    # SessionStart after compaction. ANONYMOUS is deliberately the longest:
    # it is the only variant carrying a harm this turn can still cause (a
    # handoff written with an empty seat stamp). The shared tail carries three
    # facts a compacted reader cannot reconstruct — that both dispatch halves
    # are one question, that a row you SENT is yours to chase, and that a
    # cancelled row is dead however open its chat looks — and they are why
    # this ceiling is not lower.
    "SessionStart/resume-turn": 800,
    # Stop, allow path. The clean-stop line is the highest-frequency message
    # helm prints; on a repeat within one session it is a reminder only.
    "Stop/inbox-clean-first": 280,
    "Stop/inbox-clean-repeat": 160,
    # Stop, block rungs. Each carries interpolated evidence, which is the
    # part of a block a reader cannot reconstruct and is never trimmed.
    "Stop/block-ndp": 500,
    "Stop/block-spiral": 700,
    "Stop/block-punt": 560,
    # PostToolUse, at the moment of a write, once per (session, rule).
    "PostToolUse/toolwhisper": 420,
    # UserPromptSubmit: the JIT store lane, bases plus riders. This is 95.8%
    # of every byte helm injects, and the only budget here that the code
    # enforces at runtime rather than merely being measured against.
    "UserPromptSubmit/jit-lane": _common.JIT_LANE_MAX,
}

# A WIDE BUT REAL ACT, taken from a live refusal rather than invented: the
# rung reports the spelling it found and where it stands in the folded text.
WIDE_ACT = ("workflow run at character 1094 of the folded command runs a "
            "workflow on a local project")


def _fits(case, text):
    """(budget, length) for one case — the assertion's two numbers."""
    return BUDGETS[case], len(text)


class BudgetAssertion(unittest.TestCase):
    def assert_within(self, case, text, floor=40):
        """Under the budget AND substantive.

        The floor is the positive control: an empty string is under every
        budget, so a renderer that silently returned "" would satisfy a
        ceiling-only arm forever."""
        budget, got = _fits(case, text)
        self.assertGreater(got, floor,
                           "%s rendered nothing — the arm proves nothing "
                           "about a message that does not exist" % case)
        self.assertLessEqual(
            got, budget,
            "%s is %d chars against a %d budget. Do not raise the number: "
            "move what grew behind a pointer (a store id, a doc anchor, or a "
            "verb that prints the long form)." % (case, got, budget))


class StaticMessageBudgetTest(BudgetAssertion):
    """One arm per hook event, over the worst realistic render."""

    def test_subagent_start_brief(self):  # noqa: VACUOUS_ASSERTION — assert_within carries the unconditional positive control on the SAME observable (a floor on its length), so a renderer returning "" reddens before any ceiling is reached
        self.assert_within("SubagentStart/saguide", saguide.brief())

    def test_pretooluse_ci_runner_refusal(self):  # noqa: VACUOUS_ASSERTION — assert_within carries the unconditional positive control on the SAME observable (a floor on its length), so a renderer returning "" reddens before any ceiling is reached
        for tool, act in (("Bash", WIDE_ACT),
                          ("Write", "/x/" + ".git" + "hub/work" + "flows/ci.yml")):
            with self.subTest(tool=tool):
                self.assert_within("PreToolUse/refusal-ci-runner",
                                   chat.github_actions_message(act, tool))

    def test_pretooluse_ci_runner_scoped_refusal_at_its_worst(self):  # noqa: VACUOUS_ASSERTION — assert_within carries the unconditional positive control on the SAME observable (a floor on its length), and each witness is first asserted to BE the scoped refusal at a five-digit position
        """THE SCOPED REFUSAL — an anchor beside an unresolved expansion — is
        a second shape of this line, and WIDE_ACT is a row: the scoped one
        rendered past this budget while no arm drew it. So it is drawn here
        THROUGH THE RUNG, never transcribed, for every anchor the shipped
        table carries in a row of more than one piece (a one-piece row
        answers through the row rule and never reaches this shape), with the
        anchor standing past character 9,999 of the folded command."""
        anchors = sorted({text for _s, pieces, _says in chat._ACTIONS_ROWS
                          if len(pieces) > 1
                          for text, _matcher, anchor in pieces if anchor})
        self.assertIn(".git" + "hub", anchors)
        for text in anchors:
            with self.subTest(anchor=text):
                act = chat.github_actions_refusal(
                    command="echo %s; echo %s $V" % ("x" * 10000, text))
                self.assertIsNotNone(act, text)
                self.assertIn("beside an unresolved expansion", act)
                self.assertRegex(act, r"^%s at character \d{5} "
                                 % re.escape(text))
                self.assert_within("PreToolUse/refusal-ci-runner",
                                   chat.github_actions_message(act))

    # the two places a refused spelling stands, each past character 9,999 of
    # the folded command: on the command line, and in a quoted body a SHELL
    # reads, which adds the document marker
    FORMS = (("command line", "echo %s; %s"),
             ("heredoc body", "bash <<'EOF'\n%s\n%s\nEOF"))

    def test_pretooluse_ci_runner_every_row_and_anchor_at_its_worst(self):  # noqa: VACUOUS_ASSERTION — assert_within carries the unconditional positive control on the same observable (a floor on its length), and each witness is first asserted to BE the refusal of its own row, in its own form, at a five-digit position
        """EVERY ROW OF THE SHIPPED TABLE AND EVERY ANCHOR, drawn through the
        rung in BOTH forms at a five-digit position — never a sample. The
        row description is the part of this line that varies, and the one
        WIDE_ACT carries is not the longest: rows measured past the budget
        while no arm drew them, so a row added or reworded tomorrow is inside
        this arm the moment it lands.

        The body is one a SHELL reads, so every row refuses there, the
        anchorless ones included (task/2973); the count at the end holds the
        loop to every row and every anchor in both forms."""
        pad = "x" * 10000
        drawn = 0
        for spelling, pieces, _says in chat._ACTIONS_ROWS:
            anchored = any(a for _t, _m, a in pieces)
            for form, shape in self.FORMS:
                with self.subTest(row=spelling, form=form):
                    act = chat.github_actions_refusal(
                        command=shape % (pad, spelling))
                    self.assertIsNotNone(act, spelling)
                    self.assertRegex(act, r"^%s at character \d{5} of the "
                                     r"folded command%s " % (
                                         re.escape(spelling),
                                         re.escape(chat._IN_A_DOCUMENT)
                                         if form == "heredoc body" else ""))
                    drawn += 1
                    self.assert_within("PreToolUse/refusal-ci-runner",
                                       chat.github_actions_message(act))
        anchors = sorted({t for _s, pieces, _y in chat._ACTIONS_ROWS
                          for t, _m, a in pieces if a})
        for text in anchors:
            for form, shape in self.FORMS:
                with self.subTest(anchor=text, form=form):
                    act = chat.github_actions_refusal(
                        command=shape % (pad, text + " $V"))
                    self.assertRegex(act, r"^%s at character \d{5} "
                                     % re.escape(text))
                    drawn += 1
                    self.assert_within("PreToolUse/refusal-ci-runner",
                                       chat.github_actions_message(act))
        self.assertEqual(drawn, 2 * len(chat._ACTIONS_ROWS) + 2 * len(anchors))

    def test_pretooluse_sidechain_beacon_refusal(self):  # noqa: VACUOUS_ASSERTION — assert_within carries the unconditional positive control on the SAME observable (a floor on its length), so a renderer returning "" reddens before any ceiling is reached
        self.assert_within(
            "PreToolUse/refusal-sidechain-beacon",
            "[helm argv-guard] BLOCKED: "
            + actors.sidechain_beacon_refusal("a-long-seat-name") + ".")

    def test_pretooluse_sidechain_authority_refusal_for_every_verb(self):  # noqa: VACUOUS_ASSERTION — assert_within carries the unconditional positive control on the SAME observable (a floor on its length), and the table is asserted non-empty first
        """Every refused verb, rendered through the shipped refusal, grantable
        and not: the verb is interpolated twice, so the longest one bounds
        the line."""
        from helm import delegate_grant
        self.assertTrue(delegate_grant.REFUSED)
        for group, verb in delegate_grant.REFUSED:
            with self.subTest(verb=verb):
                self.assert_within(
                    "PreToolUse/refusal-sidechain-authority",
                    "[helm argv-guard] BLOCKED: "
                    + actors.sidechain_authority_refusal(
                        "%s %s" % (group, verb),
                        (group, verb) in delegate_grant.GRANTABLE))

    def test_pretooluse_agent_model_refusal_at_its_widest_value(self):  # noqa: VACUOUS_ASSERTION — assert_within carries the unconditional positive control on the SAME observable (a floor on its length), and the widest witness is first asserted to echo exactly the cut
        """Every model name the Agent tool offers, and a value far past the
        cut. The cut is what bounds the line, so the widest render is drawn
        through the renderer rather than typed, and it must show exactly the
        cut and not one character more."""
        for model in ("haiku", "opus", "fable", "sonnet"):
            with self.subTest(model=model):
                self.assert_within("PreToolUse/refusal-agent-model",
                                   chat.agent_model_message(model))
        wide = "m" * 500
        text = chat.agent_model_message(wide)
        self.assertIn("'%s'" % ("m" * chat.AGENT_MODEL_ECHO), text)
        self.assertNotIn("m" * (chat.AGENT_MODEL_ECHO + 1), text)
        self.assert_within("PreToolUse/refusal-agent-model", text)
        # one character in is one character out, whatever the character
        self.assertEqual(len(chat.agent_model_message("\x00" * 500)),
                         len(text))

    def test_pretooluse_env_dump_refusal_for_every_kind(self):  # noqa: VACUOUS_ASSERTION — assert_within carries the unconditional positive control on the SAME observable (a floor on its length), so a renderer returning "" reddens before any ceiling is reached
        """Every kind the rung names, at the widest spelling it can carry.
        The kinds are read from the shipped table, so a kind added tomorrow
        is inside this arm the moment it lands."""
        self.assertEqual(set(chat._ENV_WHAT), set(chat._ENV_CURE))
        self.assertGreaterEqual(len(chat._ENV_WHAT), 6)
        for kind in chat._ENV_WHAT:
            with self.subTest(kind=kind):
                self.assert_within("PreToolUse/refusal-env-dump",
                                   chat.env_dump_message(
                                       (kind, "x" * chat._ENV_SPELL)))

    def test_pretooluse_shared_checkout_refusal_at_its_widest(self):  # noqa: VACUOUS_ASSERTION — assert_within carries the unconditional positive control on the SAME observable (a floor on its length), so a renderer returning "" reddens before any ceiling is reached
        """An 80-character checkout and a spelling at the cap. The cap is
        read from the shipped constant, so the widest render is drawn
        through the renderer rather than typed."""
        top = "/home/" + "d" * 74
        text = chat.shared_checkout_message((top, "x" * chat._TREE_SPELL))
        self.assertEqual(text.count(top), 2)
        self.assert_within("PreToolUse/refusal-shared-checkout", text)

    def test_every_shipped_argv_steer(self):  # noqa: VACUOUS_ASSERTION — assert_within carries the unconditional positive control on the SAME observable (a floor on its length), so a renderer returning "" reddens before any ceiling is reached
        """The SHIPPED table, not a sample of it: a new steer added tomorrow
        is inside this arm the moment it lands."""
        self.assertGreaterEqual(len(chat._STEERS), 4,
                                "control: the steer table must be populated")
        for sid, _pattern, text in chat._STEERS:
            with self.subTest(steer=sid):
                self.assert_within("PreToolUse/steer", "[helm steer] " + text)

    def test_the_clone_rung_line(self):  # noqa: VACUOUS_ASSERTION — assert_within carries the unconditional positive control on the SAME observable (a floor on its length), so a renderer returning "" reddens before any ceiling is reached
        """The clone rung is computed, not a table row, so the table arm above
        cannot see it; its one line is pinned here instead."""
        self.assert_within("PreToolUse/steer",
                           "[helm steer] " + chat._CLONE_STEER)

    def test_the_whole_pass_envelope_with_every_steer_wanting(self):  # noqa: VACUOUS_ASSERTION — assert_within carries the unconditional positive control on the SAME observable (a floor on its length), so a renderer returning "" reddens before any ceiling is reached
        """The worst call: every shipped steer trips at once. What `admit`
        lets through is what the agent is handed."""
        self.assertEqual(chat.ENVELOPE_BUDGET, BUDGETS["PreToolUse/envelope"],
                         "the emitter's ceiling and this table disagree")
        wanting = [(sid, "[helm steer] " + text)
                   for sid, _pattern, text in chat._STEERS]
        self.assert_within("PreToolUse/envelope",
                           "\n".join(chat.admit("budget-envelope-1", wanting)))

    def test_the_envelope_holds_two_of_the_widest_lines(self):
        """The envelope's ceiling is DERIVED from the line's. Raising the
        line's without the envelope's would let one line crowd out every
        other, which is the failure the envelope was given a ceiling for."""
        self.assertGreater(BUDGETS["PreToolUse/steer"], 0)
        self.assertGreaterEqual(BUDGETS["PreToolUse/envelope"],
                                2 * BUDGETS["PreToolUse/steer"])

    def test_the_nested_spawn_line_at_its_widest_steer(self):  # noqa: VACUOUS_ASSERTION — assert_within carries the unconditional positive control on the SAME observable (a floor on its length), so a renderer returning "" reddens before any ceiling is reached
        """task/2971: this line rides SubagentStart after the brief, and its
        text is an AUTHORED store steer, so its width is whatever someone
        wrote; the renderer cuts it and says so."""
        import os
        from helm import reflex
        path = reflex.write({"id": "budget-fanout", "signal": "nested-spawn",
                             "steer": "a subagent that fans out " * 80})
        self.addCleanup(os.remove, path)
        lines = [line for rid, line in reflex.spawn_steers()
                 if rid == "budget-fanout"]
        self.assertEqual(len(lines), 1)
        self.assert_within("SubagentStart/nested-spawn", lines[0])

    def test_the_tree_rung_line_at_its_widest_names(self):  # noqa: VACUOUS_ASSERTION — assert_within carries the unconditional positive control on the SAME observable (a floor on its length), so a renderer returning "" reddens before any ceiling is reached
        """Three names are interpolated and each is bounded by the seat-token
        alphabet's own length, so the worst render is three names at that
        bound — read off the pattern, not typed."""
        from helm.dispatches import _TOKEN
        wide = "n" * 64
        self.assertTrue(_TOKEN.fullmatch(wide))
        self.assertIsNone(_TOKEN.fullmatch(wide + "n"),
                          "control: the bound this arm renders at has moved")
        self.assert_within("PreToolUse/steer",
                           chat.tree_line(wide, wide, wide))

    def test_session_start_join_banners(self):  # noqa: VACUOUS_ASSERTION — assert_within carries the unconditional positive control on the SAME observable (a floor on its length), so a renderer returning "" reddens before any ceiling is reached
        """THE GUIDE PATH IS NOT PART OF THE MESSAGE. It is an absolute path
        whose length is a property of where the checkout sits — 49 characters
        on the owner's box, 66 in a lane worktree, more on a build node — so a
        budget that counted it would measure the filesystem and redden on a
        machine nobody changed. The path is charged at a fixed width instead,
        and the arm below pins that the banner still carries it."""
        from helm.seats_common import GUIDE_PATH
        armed = seats_join.join_banner("a-long-seat-name", "#a-long-room-name",
                                       "", 1234567)
        first = seats_join.join_banner("a-long-seat-name", "#a-long-room-name",
                                       "")
        self.assertIn(GUIDE_PATH, armed)
        self.assertIn(GUIDE_PATH, first)
        stand_in = "x" * 49                 # the owner's own checkout depth
        self.assert_within("SessionStart/join-armed",
                           armed.replace(GUIDE_PATH, stand_in))
        self.assert_within("SessionStart/join-first-action",
                           first.replace(GUIDE_PATH, stand_in))
        # THE BANNER THAT ASKS FOR NOTHING MUST NOT COST MORE THAN THE ONE
        # THAT ASKS FOR WORK. It did — 771 chars to say nothing is owed — and
        # nothing but an arm keeps that from happening again.
        self.assertLess(len(armed), len(first))

    def test_session_start_resume_turn_variants(self):  # noqa: VACUOUS_ASSERTION — assert_within carries the unconditional positive control on the SAME observable (a floor on its length), so a renderer returning "" reddens before any ceiling is reached
        for name in ("GENERIC", "UNCLAIMED", "UNREADABLE", "ANONYMOUS"):
            raw = getattr(resumeturn, name)
            text = raw % "the fresh entries there name other seats" \
                if "%s" in raw else raw
            with self.subTest(variant=name):
                self.assert_within("SessionStart/resume-turn", text)

    def test_stop_block_rungs(self):  # noqa: VACUOUS_ASSERTION — assert_within carries the unconditional positive control on the SAME observable (a floor on its length), so a renderer returning "" reddens before any ceiling is reached
        self.assert_within(
            "Stop/block-ndp",
            ndp_block("3 leases held, 0 subagents spawned this session"))
        self.assert_within(
            "Stop/block-spiral",
            spiral_block("lane/a-fairly-long-lane-name", 4, 24, "seat-b",
                         'helm chat meld open seat-b --about "the findings"',
                         "findings: 2 open on tip abc123def456, WIDENING"))
        lines = punt.gate_lines(
            "I will refactor this later, once the owner is around.",
            open_ask=False)
        self.assertTrue(lines, "control: the punt gate must have fired")
        self.assert_within("Stop/block-punt", "\n".join(lines))

    def test_stop_inbox_clean_latches_to_a_reminder(self):  # noqa: VACUOUS_ASSERTION — assert_within carries the unconditional positive control on the SAME observable (a floor on its length), so a renderer returning "" reddens before any ceiling is reached
        """The highest-frequency Stop message. The full arming instruction is
        worth its bytes ONCE per session; after that the seat has it."""
        from helm.seats_stop_guard import _inbox_clean_line
        first = _inbox_clean_line("main", "a-seat", "sess-inbox-clean")
        repeat = _inbox_clean_line("main", "a-seat", "sess-inbox-clean")
        self.assert_within("Stop/inbox-clean-first", first)
        self.assert_within("Stop/inbox-clean-repeat", repeat, floor=30)
        # THE FIRST ONE CARRIES THE PAYLOAD, the repeat carries the reminder.
        self.assertIn("Monitor(command:", first)
        self.assertNotIn("Monitor(command:", repeat)
        # both still name the ACT, which is the only thing the rung is for
        for line in (first, repeat):
            self.assertIn("inbox clean", line)
            self.assertIn("beacon", line)

    def test_posttooluse_toolwhisper_rules(self):  # noqa: VACUOUS_ASSERTION — assert_within carries the unconditional positive control on the SAME observable (a floor on its length), so a renderer returning "" reddens before any ceiling is reached
        self.assertGreaterEqual(len(toolwhisper.RULES), 2,
                                "control: the rule table must be populated")
        for rule in toolwhisper.RULES:
            with self.subTest(rule=rule["id"]):
                self.assert_within("PostToolUse/toolwhisper",
                                   "[helm whisper] " + rule["say"])


class JitLaneBudgetTest(BudgetAssertion):
    """The lane that is 95.8% of helm's injection, and the only one whose
    budget the CODE enforces rather than the suite merely observing."""

    def _entry(self, eid, statement, gloss=None):
        e = {"type": "prior", "class": "certain", "id": eid,
             "statement": statement, "status": "live", "keywords": []}
        if gloss:
            e["gloss"] = gloss
        return e

    def test_the_lane_cannot_exceed_its_budget_however_long_its_entries_are(self):  # noqa: VACUOUS_ASSERTION — assert_within carries the unconditional positive control on the SAME observable (a floor on its length), so a renderer returning "" reddens before any ceiling is reached
        """THE WORST REALISTIC RENDER: four entries far longer than anything
        in the live store. JIT_CAP bounded the COUNT and nothing bounded the
        BYTES, so this is exactly the shape that produced a 3,405-byte turn."""
        from helm.inject._whisper import _jit_lane
        entries = [self._entry("entry-number-%d" % i,
                               "Sentence %d of the statement is quite long. " % i
                               + ("filler words that go on and on " * 120))
                   for i in range(_common.JIT_CAP)]
        lines, ids, _riders = _jit_lane(entries, entries)
        self.assertEqual(len(ids), _common.JIT_CAP,
                         "control: every entry must have rendered")
        self.assert_within("UserPromptSubmit/jit-lane", "\n".join(lines))
        for line in lines:
            self.assertLessEqual(len(line), _common.JIT_LINE_CAP, line)

    def test_a_cut_lines_lane_names_the_route_to_what_was_cut(self):
        """A bare ellipsis is the worst of both — long AND incomplete, with no
        way for the reader even to learn that a route exists. The route is the
        LANE'S one footer (task/2980), not a pointer on every cut line: the
        line keeps its ellipsis and the footer names the verb."""
        from helm.inject._whisper import _jit_lane
        long_one = self._entry("an-entry-with-a-long-statement",
                               "The first sentence is already far too long to "
                               "fit inside the lane's per-entry cap, which is "
                               "the case this arm exists for, and it keeps "
                               "going well past it and past it and past it "
                               "again before it ever reaches a full stop. A "
                               "second sentence follows, unread.")
        line = _entries._entry_line(long_one, cap=_common.JIT_LINE_CAP,
                                    short=True)
        self.assertLessEqual(len(line), _common.JIT_LINE_CAP)
        self.assertTrue(line.endswith("…"), line)
        self.assertNotIn("helm store get", line)
        lines, _ids, _r = _jit_lane([long_one], [long_one])
        self.assertEqual(lines, [line, _common.FOOTER])

    def test_a_cut_lands_on_a_word_boundary(self):
        """A MUTANT SURVIVED HERE: removing the back-off to the last space
        left every arm green, because a ceiling and a pointer say nothing
        about WHERE the cut fell. Severing mid-word is half the complaint —
        a line that ends "…the assignm" reads as damage, not as brevity."""
        e = self._entry("an-id-for-the-boundary-arm",
                        "Supercalifragilistic " * 40 + ". Second.")
        line = _entries._entry_line(e, cap=_common.JIT_LINE_CAP, short=True)
        body = line.split("…", 1)[0]
        self.assertTrue(body.endswith("Supercalifragilistic"),
                        "the cut fell inside a word: %r" % body[-30:])

    def test_the_lane_renders_a_short_first_sentence_whole(self):
        """A MUTANT SURVIVED HERE TOO: flipping the lane's `short` off left
        every budget green, because a long statement is cut to the cap either
        way. The difference is whether the line ENDS AT A SENTENCE or at a
        character count, and only an entry whose first sentence FITS can tell
        those apart. Driven through _jit_lane, not the renderer, because the
        mutant lives in the lane's plan arguments."""
        from helm.inject._whisper import _jit_lane
        e = self._entry("a-lane-entry-with-a-short-opening",
                        "A gate is not an approval. " + ("tail words " * 60))
        lines, ids, _r = _jit_lane([e], [e])
        self.assertEqual(len(ids), 1, "control: the entry must have rendered")
        self.assertEqual(lines[0], "PREMISE a-lane-entry-with-a-short-opening: "
                                   "A gate is not an approval.")

    def test_a_statement_that_fits_is_never_cut_and_never_pointed_at(self):
        """The must-miss beside the arm above: a pointer on every line would
        satisfy it while spending bytes on entries that were never cut."""
        short = self._entry("a-short-entry",
                            "A gate is not an approval. It proves the tree "
                            "and says nothing about the content.")
        line = _entries._entry_line(short, cap=_common.JIT_LINE_CAP, short=True)
        self.assertEqual(line, "PREMISE a-short-entry: A gate is not an "
                               "approval.")
        self.assertNotIn("helm store get", line)
        self.assertNotIn("…", line)

    def test_a_gloss_fires_whole_and_ignores_the_narrower_cap(self):
        """The gloss is the author's own firing line. Reducing IT would take
        the one field a writer has over what a seat receives and make it
        behave like the raw statement — and it would punish exactly the
        authors the gloss campaign is asking to do this work. A gloss is
        charged to LINE_CAP even in the lane whose own cap is narrower."""
        wide = "w" * (_common.JIT_LINE_CAP + 80)
        e = self._entry("a-glossed-entry",
                        "The statement is long and has several sentences. "
                        "Here is the second one. And a third.",
                        gloss="Two sentences. " + wide)
        line = _entries._entry_line(e, cap=_common.JIT_LINE_CAP, short=True)
        self.assertGreater(len(line), _common.JIT_LINE_CAP)
        self.assertLessEqual(len(line), _common.LINE_CAP)
        self.assertIn(wide, line)
        # MUST-MISS: the gloss must not be reduced to its first sentence
        self.assertNotEqual(line, "PREMISE a-glossed-entry: Two sentences.")

    def test_a_dropped_entry_is_named_rather_than_vanishing(self):
        """The pinned lane's law, applied to the lane that never had it: a hit
        the budget could not carry and a hit that never matched must not
        produce the same observable."""
        alarm = _entries.jit_alarm(["prior:one", "prior:two"], _common.JIT_BUDGET)
        self.assertIn("2 hit(s)", alarm)
        self.assertIn("prior:one", alarm)
        self.assertIn("prior:two", alarm)
        self.assertIn("helm store get", alarm)
        self.assertIsNone(_entries.jit_alarm([], _common.JIT_BUDGET),
                          "must-miss: nothing dropped, nothing said")


class FirstSentenceTest(unittest.TestCase):
    """The reduction that replaced mid-word truncation."""

    def test_it_stops_at_the_first_real_sentence(self):
        self.assertEqual(
            _entries._first_sentence(
                "A wall is not an identity change. Keep the last proof."),
            "A wall is not an identity change.")

    def test_it_walks_past_an_abbreviation_or_a_bare_id(self):  # noqa: VACUOUS_ASSERTION — the assertion is an EQUALITY against a non-empty literal, not an absence: a reducer returning "" fails it
        """A head shorter than FIRST_SENTENCE_MIN is not a sentence. Without
        this the reduction would deliver 'e.g.' and call it a lesson."""
        for text, want in (
                ("e.g. a case. And here is the sentence that carries it.",
                 "e.g. a case. And here is the sentence that carries it."),
                ("task/1563. One constant-prefix import makes the CLI a "
                 "consumer of everything under helm/.",
                 "task/1563. One constant-prefix import makes the CLI a "
                 "consumer of everything under helm/.")):
            self.assertEqual(_entries._first_sentence(text), want)

    def test_a_statement_with_no_terminator_survives_whole(self):
        self.assertEqual(_entries._first_sentence("no terminator at all here"),
                         "no terminator at all here")


class RepeatSuppressionTest(unittest.TestCase):
    """An identical rendered line is delivered REPEAT_ALLOWANCE times inside
    the window and withheld after; a line whose CONTENT changed always fires.

    THE ALLOWANCE IS NOT ONE ON PURPOSE. The reflex lane is exempt from the
    pinned lane's once-per-session suppression because a reflex fires on a
    signal live THIS turn (owner-asked contract, pinned in tests/test_inject).
    Saying a live steer twice honours that; saying it turn after turn is the
    wallpaper the owner complained about."""

    def _feed(self, lines, seen, turn):
        from helm.inject._whisper import _unrepeated
        kept, fps = _unrepeated(lines, seen, turn)
        seen.setdefault("lines", {}).update(fps)
        return kept

    def test_the_allowance_is_spent_and_then_the_line_is_withheld(self):  # noqa: VACUOUS_ASSERTION — the empty result at the end is read against the two deliveries asserted non-empty above it on the same observable, unconditionally
        seen = {"turn": 0, "lines": {}}
        delivered = [bool(self._feed(["REFLEX: the same sentence"], seen, t))
                     for t in range(1, 5)]
        self.assertEqual(delivered[:_common.REPEAT_ALLOWANCE],
                         [True] * _common.REPEAT_ALLOWANCE,
                         "control: the allowance must actually be spendable")
        self.assertEqual(delivered[_common.REPEAT_ALLOWANCE:],
                         [False] * (4 - _common.REPEAT_ALLOWANCE))

    def test_a_line_whose_content_changed_is_never_swallowed(self):
        """THE FAILURE THAT WOULD MATTER: suppression keyed on anything but
        the rendered bytes silences a steer that now says something else."""
        seen = {"turn": 0, "lines": {}}
        for t in range(1, 6):
            self._feed(["REFLEX: 3 leases held"], seen, t)
        self.assertEqual(self._feed(["REFLEX: 6 leases held"], seen, 6),
                         ["REFLEX: 6 leases held"])

    def test_the_window_resets_the_count(self):
        """A fact worth saying twenty turns apart is worth saying again."""
        seen = {"turn": 0, "lines": {}}
        for t in range(1, 6):
            self._feed(["REFLEX: a fact worth repeating"], seen, t)
        later = 5 + _common.REPEAT_WINDOW_TURNS
        self.assertEqual(self._feed(["REFLEX: a fact worth repeating"],
                                    seen, later),
                         ["REFLEX: a fact worth repeating"])

    def test_no_session_state_delivers_everything(self):  # noqa: VACUOUS_ASSERTION — both assertions are equalities against non-empty literals, not absences
        """Fail-open in the direction that costs least: a repeated line is a
        cheaper failure than a steer that never fires."""
        from helm.inject._whisper import _unrepeated
        kept, fps = _unrepeated(["REFLEX: one", "REFLEX: two"], None, 3)
        self.assertEqual(kept, ["REFLEX: one", "REFLEX: two"])
        self.assertEqual(fps, {})


class RefusalStillRefusesTest(unittest.TestCase):
    """Words moved; decisions did not. Every arm drives the SHIPPED hook entry
    and reads its exit code, because a shortened refusal that stopped refusing
    is the only way this lane could do real harm."""

    def hook(self, tool="Bash", agent_id=None, **tool_input):
        payload = {"hook_event_name": "PreToolUse", "tool_name": tool,
                   "session_id": "sess-hookbudget", "cwd": "/tmp/repo",
                   "tool_use_id": "toolu_hookbudget", "tool_input": tool_input}
        if agent_id:
            payload["agent_id"] = agent_id
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdin",
                               io.StringIO(json.dumps(payload))), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = chat.cmd_argv_guard([])
        return rc, out.getvalue() + err.getvalue()

    # built at runtime so this FILE does not carry a whole spelling: the rung
    # reads text, and a seat grepping a file that held one would be refused
    # by its own guard
    ACT = "gh " + "workflow" + " run ci.yml"

    def test_the_ci_runner_rung_still_exits_2_and_still_routes(self):
        rc, out = self.hook(command=self.ACT)
        self.assertEqual(rc, 2, out)
        self.assertIn("[helm argv-guard] BLOCKED", out)
        # the three facts a refused reader needs, none of them prose:
        self.assertIn("HELM_ALLOW_GITHUB_ACTIONS=1", out)   # the escape
        self.assertIn("at character", out)                  # where it stands
        # THE VERB AND THE ID AS ONE STRING. A mutant that dropped `helm
        # store get` and left the bare id survived an arm that asked only for
        # the id — a name with no verb in front of it is not a route.
        self.assertIn("helm store get "
                      "prior:ci-runs-on-the-local-fabric-never-github-actions",
                      out)
        self.assert_within_budget(out)

    def test_the_override_at_the_front_still_sends_it(self):  # noqa: VACUOUS_ASSERTION — this IS the must-miss for the refusal arm beside it, which asserts rc 2 and four substrings on the same hook over the same command text
        """The must-miss: a rung that refused everything would satisfy the
        arm above forever."""
        rc, out = self.hook(command="HELM_ALLOW_GITHUB_ACTIONS=1 " + self.ACT)
        self.assertEqual(rc, 0, out)
        self.assertNotIn("BLOCKED", out)

    def test_an_unrelated_command_is_untouched(self):  # noqa: VACUOUS_ASSERTION — same pair: the refusal arm above proves this hook speaks at all, so silence here is the rung declining and not the guard being dead
        rc, out = self.hook(command="git status --short")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("BLOCKED", out)

    def test_the_agent_model_rung_exits_2_within_its_budget(self):
        rc, out = self.hook(tool="Agent", prompt="p", model="opus")
        self.assertEqual(rc, 2, out)
        line = [l for l in out.splitlines() if "BLOCKED" in l][0]
        self.assertIn("helm store get " + chat.AGENT_MODEL_PREMISE, line)
        self.assertLessEqual(len(line),
                             BUDGETS["PreToolUse/refusal-agent-model"], line)
        # the must-miss on the same door: no model, no refusal
        rc, out = self.hook(tool="Agent", prompt="p")
        self.assertEqual((rc, out), (0, ""))

    def assert_within_budget(self, out):
        budget = BUDGETS["PreToolUse/refusal-ci-runner"]
        # the hook writes one line; measure the refusal itself, not the frame
        line = [l for l in out.splitlines() if "BLOCKED" in l][0]
        self.assertLessEqual(len(line), budget, line)


class SameTurnRepeatTest(unittest.TestCase):
    """The repeat filter runs over the whole lane, so it must see the copies it
    is delivering in THIS turn and not only what earlier turns recorded."""

    def test_identical_lines_in_one_turn_spend_the_allowance(self):
        from helm.inject._whisper import _unrepeated
        line = "REFLEX: say this once or twice, never three times."
        out, fps = _unrepeated([line] * 3, {"lines": {}}, turn=1)
        self.assertEqual(len(out), _common.REPEAT_ALLOWANCE)
        self.assertEqual([v[0] for v in fps.values()],
                         [_common.REPEAT_ALLOWANCE])
        # a DIFFERENT line beside them is untouched
        out, _ = _unrepeated([line, line, line, "another line"],
                             {"lines": {}}, turn=1)
        self.assertIn("another line", out)


class PointerResolutionTest(unittest.TestCase):
    """EVERY `helm store get <typed-id>` a hook message names must resolve.

    THIS IS THE ARM THE ESTATE DID NOT HAVE. The seat-join banner cited
    premise `native-wake-only-agent-armed` for months; the store holds
    `native-wake-only-agent-armed-or-headless`, so the citation printed at
    every session start led nowhere. A pointer that does not resolve is worse
    than no pointer — it teaches readers that helm's citations are decoration.
    """

    NAMED = (
        chat.GITHUB_ACTIONS_PREMISE,
        chat.AGENT_MODEL_PREMISE,
        "prior:taskstop-kills-the-wrapper-never-the-remote-fab-job",
        "heuristic:review-begins-with-cat-file",
        "prior:refactors-are-preapproved-land-now",
    )

    def test_the_runner_refusal_names_a_place_that_ANSWERS(self):
        """A pointer that resolves and does not answer is a route to nothing
        with a green check on it. The refusal promises what still passes; the
        place it names for that must actually carry the allow list, and the
        Write form must hand over a command a reader can run."""
        import os
        msg = chat.github_actions_message("an Actions spelling", "Bash")
        self.assertIn(chat.GITHUB_ACTIONS_DOC, msg)
        self.assertIn(chat.GITHUB_ACTIONS_PREMISE, msg)
        doc = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), chat.GITHUB_ACTIONS_DOC.split(",")[0])
        with open(doc, encoding="utf-8") as f:
            text = f.read()
        for phrase in ("gh pr list", "gh issue view", "SINGLE spelling"):
            self.assertIn(phrase, text)
        write = chat.github_actions_message("/x/a.yml", "Write")
        self.assertIn(chat.GITHUB_ACTIONS_OVERRIDE + " tee <path> <<'EOF'",
                      write)

    def test_every_id_named_in_a_hook_message_is_in_the_store(self):
        from helm import store
        from helm.inject._entries import load_entries
        try:
            entries = load_entries()
        except Exception as exc:                       # pragma: no cover
            self.skipTest("live store unreadable: %r" % (exc,))
        if len(entries) < 100:
            self.skipTest("store holds %d entries — too thin to testify"
                          % len(entries))
        have = set()
        for e in entries:
            try:
                have.add(store.typed_id(e))
            except Exception:
                continue
        # THE MUST-HIT, said out loud: a reader that raised on every row
        # would leave `have` empty, and "nothing resolves" would then read
        # identically to "these five do not".
        self.assertGreater(len(have), 100,
                           "control: the typed-id reader returned almost "
                           "nothing, so its verdict is about itself")
        missing = [tid for tid in self.NAMED if tid not in have]
        self.assertEqual(missing, [],
                         "hook messages name store ids that do not resolve: "
                         "%s" % ", ".join(missing))

    def test_the_dead_spelling_reaches_no_reader(self):
        """The cured defect, pinned so it cannot come back. The banner cited a
        premise the store does not hold, at every session start; the guide
        carried the same dead id. Both surfaces are checked at the point a
        READER meets them — the rendered banner and the guide's own text —
        rather than anywhere the spelling may legitimately appear as the
        subject of a comment."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        dead = "native-wake-only-agent-armed"
        live = dead + "-or-headless"
        banners = (seats_join.join_banner("s", "#r", "", 42),
                   seats_join.join_banner("s", "#r", ""))
        for banner in banners:
            self.assertEqual(banner.count(dead), 0, banner)
        with open(os.path.join(root, "docs", "NEW_AGENT_GUIDE.md"),
                  encoding="utf-8") as f:
            guide = f.read()
        # control: the guide must still cite the premise at all
        self.assertGreater(guide.count(live), 0)
        self.assertEqual(guide.count(dead), guide.count(live))

    def test_every_store_pointer_a_hook_message_prints_is_well_formed(self):
        """Runs with NO live store, so a build node with no ~/.helm still
        protects the half that is checkable without one: a pointer naming a
        type the store cannot hold never resolves, whatever its slug.

        IT READS THE RENDERED MESSAGES, not the source. The literals are
        assembled across implicit concatenations and %-interpolations, so a
        regex over the .py files found ZERO pointers and reported a clean
        tree — a sweep measuring itself, which is the shape the control below
        exists to catch."""
        import re
        rendered = [
            chat.github_actions_message(WIDE_ACT),
            chat.github_actions_message("/x/a.yml", "Write"),
            chat.agent_model_message("opus"),
            spiral_block("lane/x", 3, 12, "seat-b", "cure", "evidence"),
            "\n".join(punt.gate_lines(
                "I will refactor this later, once the owner is around.",
                open_ask=False)),
            _entries.jit_alarm(["prior:x"], _common.JIT_BUDGET),
        ] + ["[helm steer] " + t for _s, _p, t in chat._STEERS]
        pat = re.compile(r"helm store get ([a-z]+):([a-z0-9-]+)")
        found = [m.groups() for text in rendered for m in pat.finditer(text)]
        self.assertGreater(len(found), 3,
                           "control: the sweep found almost no pointers, so "
                           "it is reporting on itself and not on the messages")
        types = {"prior", "premise", "lexicon", "heuristic", "reference",
                 "capability"}
        bad = [(t, slug) for t, slug in found if t not in types]
        self.assertEqual(bad, [], "hook messages name unknown entry types")


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
