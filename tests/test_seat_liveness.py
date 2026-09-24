#!/usr/bin/env python3
"""seat liveness TRUTH — a richer state than LIVE/GONE, and UNKNOWN never
collapses into another value.

The defect: `helm seat where` reported LIVE for AGENT RUNNING, AGENT BLOCKED ON
A HUMAN (codex ~14h at a plan-approval prompt holding 4 of 6 open reviews), and
AGENT EXITED WITH THE PANE ALIVE — three states needing completely different
responses, all indistinguishable to every watchdog that keys off LIVE/GONE.

The evidence is the PANE TAIL and only the pane tail — never /proc, pgrep,
mtime, or cwd, each of which lied about liveness on the live fleet.

These tests pin the classifier (pattern -> state, as DATA) and the lifecycle
walk (every non-answer is an honest UNKNOWN, not a guess). Every pattern has a
mutation test: a state that no longer matches its pattern must change the
verdict, or the test is decoration.
"""
import contextlib
import io
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import proxywatch, seat  # noqa: E402
from helm import seat_lifecycle  # noqa: E402


class ClassifyPaneTailTest(unittest.TestCase):
    """The pattern table is DATA: (state, patterns), most-specific-first."""

    def test_running_is_the_esc_to_interrupt_affordance(self):
        """`esc to interrupt` renders ONLY while a turn is in flight — observed
        live on exactly the working panes (codex, kimi), absent on idle/blocked."""
        tail = "doing work\n  ⏵⏵ bypass permissions on · 1 monitor · esc to interrupt"
        self.assertEqual(seat._classify_pane_tail(tail), ("RUNNING", None))

    def test_idle_is_a_prompt_with_nothing_in_flight(self):
        tail = "done, parked\n❯ \n  ⏵⏵ bypass permissions on · 1 monitor · ← for agents"
        self.assertEqual(seat._classify_pane_tail(tail), ("IDLE", None))

    def test_idle_allows_the_rendered_separator_before_status_chrome(self):
        tail = ("done, parked\n────────────────────────────────\n❯\n"
                "────────────────────────────────\n"
                "  ⏵⏵ bypass permissions on · 1 monitor · ← for agents")
        self.assertEqual(seat._classify_pane_tail(tail), ("IDLE", None))

    def test_idle_allows_the_agents_strip_below_the_composer(self):
        """With the task list visible (ctrl+t) Claude renders the agents strip
        BELOW the status line (`● main`, `◯ claude  <desc>   12m 48s`).
        Measured live 2026-08-04 (codex-3 at 100% context): these rows read as
        newer semantic content, the empty composer returned None, and the
        autocompact watchdog refused at exactly its target state."""
        tail = ("done, parked\n────────────────────────────────\n❯\n"
                "────────────────────────────────\n"
                "  ⏵⏵ bypass permissions on · 1 monitor · ctrl+t to hide tasks"
                " · ← for agents · ↓ to manage\n"
                "  ● main\n"
                "  ◯ claude  Refute duplicate-work guard design"
                "                    12m 48s")
        self.assertEqual(seat._current_prompt_line(tail), "❯")
        self.assertEqual(seat._classify_pane_tail(tail), ("IDLE", None))

    def test_historical_prompt_is_not_current_idle_evidence(self):  # noqa: VACUOUS_ASSERTION — adjacent live IDLE fixtures positively classify current composer chrome
        for tail in (
                "❯ previous command\nassistant output still streaming",
                "❯ previous command\nWhich approach should I use?\n"
                "  1. Safe\n  2. Fast\nEnter to select"):
            with self.subTest(tail=tail):
                self.assertIsNone(seat._classify_pane_tail(tail)[0])

    def test_exited_pane_alive_offers_a_resume(self):
        tail = "Resume this session with: claude --resume 8d2e1ff0-46c3-45b5"
        state, blocked = seat._classify_pane_tail(tail)
        self.assertEqual(state, "EXITED_PANE_ALIVE")
        self.assertIn("claude --resume", blocked)

    def test_blocked_on_human_names_the_prompt(self):
        """The codex 14h case: a plan-approval prompt, needing one keystroke."""
        tail = "Do you want to proceed?\n  1. Yes, and bypass permissions\n  2. No"
        state, blocked = seat._classify_pane_tail(tail)
        self.assertEqual(state, "BLOCKED_ON_HUMAN")
        self.assertIn("bypass permissions", blocked)

    def test_blocked_on_human_carries_the_plan_path_when_printed(self):
        """FIX (P2, cross-family): the WHERE, not just the WHAT. The plan path
        is printed in the approval prompt, and without it an integrator still
        has to open the pane by hand to learn what they are approving — the
        manual step this feature exists to delete."""
        tail = ("Review the plan at "
                "~/.helm/_global/seats/codex/claude/plans/sparkling-bouncing-meerkat.md"
                "\n\nDo you want to proceed?\n  1. Yes, and bypass permissions")
        state, blocked = seat._classify_pane_tail(tail)
        self.assertEqual(state, "BLOCKED_ON_HUMAN")
        self.assertEqual(blocked,
                         "~/.helm/_global/seats/codex/claude/plans/"
                         "sparkling-bouncing-meerkat.md")

    def test_blocked_on_human_never_fabricates_a_path(self):
        """When no path is printed, blocked_on falls back to the prompt text —
        never a made-up path."""
        tail = "Do you want to proceed?\n  1. Yes, and bypass permissions"
        _state, blocked = seat._classify_pane_tail(tail)
        self.assertNotIn("plans/", blocked or "")

    def test_blocked_on_quota_names_the_wall(self):
        """Observed live on grok: 402 balance + 503 auth_unavailable."""
        for tail in ("API Error: 402 Grok Build usage balance exhausted",
                     "503 auth_unavailable: no auth available (providers=xai)"):
            state, blocked = seat._classify_pane_tail(tail)
            self.assertEqual(state, "BLOCKED_ON_QUOTA", tail)
            self.assertTrue(blocked, tail)

    def test_context_full_is_distinct_from_quota(self):
        """'100% context used' is the autocompact boundary, not a cred wall."""
        state, blocked = seat._classify_pane_tail(
            "working…\n100% context used\n❯ ")
        self.assertEqual(state, "CONTEXT_FULL")
        self.assertIn("100% context used", blocked)

    def test_an_unrecognized_tail_is_UNKNOWN_not_a_guess(self):
        """The invariant that makes the whole thing trustworthy: a tail that
        matches nothing is UNKNOWN, never collapsed into a guessed state."""
        state, blocked = seat._classify_pane_tail(
            "some output with no liveness signal at all")
        self.assertIsNone(state)   # _classify returns None -> caller maps UNKNOWN
        self.assertIsNone(blocked)

    def test_blocked_states_carry_the_blocker_and_running_idle_do_not(self):
        """A bare BLOCKED state with no named blocker is useless — the remedy
        needs WHAT it is blocked on. RUNNING/IDLE carry None."""
        self.assertIsNotNone(seat._classify_pane_tail(
            "usage balance exhausted")[1])
        self.assertIsNone(seat._classify_pane_tail("esc to interrupt")[1])


class ClassifyMutationTest(unittest.TestCase):
    """Each pattern is load-bearing: remove the signal from the tail and the
    verdict must change, or the pattern is decoration."""

    def test_no_interrupt_affordance_means_NOT_running(self):
        """MUTATION: drop 'esc to interrupt' -> the same working-pane tail must
        no longer read RUNNING (it falls to IDLE on the prompt marker)."""
        tail = "doing work\n  ⏵⏵ bypass permissions on · 1 monitor"
        state, _ = seat._classify_pane_tail(tail)
        self.assertNotEqual(state, "RUNNING")

    def test_no_resume_line_means_NOT_exited(self):
        """MUTATION: drop the --resume line -> EXITED_PANE_ALIVE must not fire."""
        tail = "the session has ended.\n❯ "
        state, _ = seat._classify_pane_tail(tail)
        self.assertNotEqual(state, "EXITED_PANE_ALIVE")

    def test_a_quota_word_alone_does_not_block_on_human(self):
        """MUTATION-discriminator: 'bypass permissions' appears in the status
        line of EVERY pane (the ⏵⏵ affordance), so BLOCKED_ON_HUMAN must NOT
        fire on a bare status line — only on an actual approval prompt."""
        tail = "❯ \n  ⏵⏵ bypass permissions on · 1 monitor · ← for agents"
        state, _ = seat._classify_pane_tail(tail)
        self.assertNotEqual(state, "BLOCKED_ON_HUMAN",
                            "the status-line affordance is not an approval prompt")


class AnswerablePromptTest(unittest.TestCase):
    """The WHICH. Detecting BLOCKED_ON_HUMAN names the blocker; ANSWERING it
    needs the choice, and the choice must be READ off the dialog. Parsing lives
    beside the classifier so the fleet keeps ONE pane-tail reader — an actuator
    consumes `options` off the liveness row and never opens a pane to look."""

    PROMPT = ("Ready to code?\nHere is Claude's plan:\n"
              "Review the plan at ~/x/plans/quiet-hopping-otter.md\n\n"
              "Steps:\n  1. Read the file\n  2. Patch it\n\n"
              "Claude has written up a plan and is ready to execute. "
              "Would you like to proceed?\n"
              "  1. Yes, and bypass permissions\n"
              "  2. Yes, and manually approve edits\n"
              "  3. No, keep planning\n"
              "  ⏵⏵ bypass permissions on · 1 monitor")

    def test_only_the_builtin_plan_execution_sentence_mints_plan_authority(self):
        """A permission question can quote the SAME real plans/ path and render
        the SAME options. The fixed Claude Code execution sentence, not either
        supporting shape, is the authority discriminator."""
        permission = self.PROMPT.replace(
            "Claude has written up a plan and is ready to execute. "
            "Would you like to proceed?",
            "May I create a local commit after reading the plan above?")
        self.assertEqual(seat._prompt_type(self.PROMPT),
                         seat.PLAN_EXECUTION_PROMPT)
        self.assertEqual(seat._prompt_type(permission), seat.PERMISSION_PROMPT)
        self.assertEqual(seat._prompt_options(permission),
                         seat._prompt_options(self.PROMPT))
        self.assertEqual(seat._classify_pane_tail(permission)[0],
                         "BLOCKED_ON_HUMAN")

    def test_the_dialog_below_the_plans_own_numbered_steps_is_the_one_read(self):
        self.assertEqual(
            seat._prompt_options(self.PROMPT),
            [(1, "Yes, and bypass permissions"),
             (2, "Yes, and manually approve edits"),
             (3, "No, keep planning")])

    def test_a_menu_above_a_newer_numbered_list_is_not_the_menu(self):
        """THE RESURRECTION, measured on shipped code before the cure: the
        reader walked UPWARD for the newest run that happened to carry a Yes/No
        label, so an ANSWERED proceed menu came back out of scrollback because
        the newer list below it offers neither label. A keystroke chosen off it
        lands in the newer list, and the send-time guard re-reads the pane
        through this same helper, so it agrees with the stale reading instead of
        catching the change it exists to catch.

        THE CONTROL IS IN THIS METHOD and unconditional: the SAME menu with
        nothing below it still parses to its two options, so the empty result is
        the newest-run rule firing and not a fixture that was never a menu.
        """
        menu = "Do you want to proceed?\n 1. Yes\n 2. No, keep going\n"
        newer = ("\n...work happened...\n\nPick a target to inspect:\n"
                 " 1. src/main.py\n 2. src/util.py\n 3. tests/\n")
        self.assertEqual(seat._prompt_options(menu),
                         [(1, "Yes"), (2, "No, keep going")])
        self.assertEqual(seat._prompt_options(menu + newer), [])
        self.assertEqual(
            seat.affirmative_choice(seat._prompt_options(menu + newer)),
            (None, None))

    def test_a_menu_below_stale_numbered_prose_is_still_the_menu(self):
        """The other direction, and the reason the rule is NEWEST-WINS rather
        than "refuse whenever the tail holds two runs": a real dialog renders
        BELOW whatever the transcript already printed, so a qualifying menu that
        IS the newest run must still answer."""
        tail = ("Pick a target to inspect:\n 1. src/main.py\n 2. src/util.py\n"
                "\nDo you want to proceed?\n 1. Yes\n 2. No, keep going\n")
        self.assertEqual(seat._prompt_options(tail),
                         [(1, "Yes"), (2, "No, keep going")])
        self.assertEqual(seat.affirmative_choice(seat._prompt_options(tail)),
                         ("1", "Yes"))

    def test_the_first_yes_is_the_choice(self):
        self.assertEqual(
            seat.affirmative_choice(seat._prompt_options(self.PROMPT)),
            ("1", "Yes, and bypass permissions"))

    def test_a_json_round_tripped_row_still_answers(self):
        opts = json.loads(json.dumps(seat._prompt_options(self.PROMPT)))
        self.assertEqual(seat.affirmative_choice(opts)[0], "1")

    def test_numbered_prose_is_not_a_dialog(self):  # noqa: VACUOUS_ASSERTION — the positive control is IN this test and unconditional: the SAME two-line shape with Yes/No labels parses to two options, so the empty result is the label discriminator firing, not the parser being inert
        self.assertEqual(
            seat._prompt_options("  1. Read the file\n  2. Patch it"), [])
        self.assertEqual(len(seat._prompt_options(
            "  1. Yes, read the file\n  2. No, stop")), 2)

    def test_a_run_that_skips_a_number_is_not_a_rendered_dialog(self):  # noqa: VACUOUS_ASSERTION — the positive control is IN this test and unconditional: the SAME prompt renumbered 1/2 parses to two options, so the empty result is the sequence check firing
        self.assertEqual(
            seat._prompt_options("Do you want to proceed?\n 1. Yes\n 3. No"), [])
        self.assertEqual(len(seat._prompt_options(
            "Do you want to proceed?\n 1. Yes\n 2. No")), 2)

    def test_a_dialog_with_no_yes_yields_no_choice(self):
        self.assertEqual(
            seat.affirmative_choice(seat._prompt_options(
                "Pick one:\n 1. No, stop\n 2. No, keep planning")),
            (None, None))

    def test_the_liveness_row_carries_the_options_only_when_blocked(self):
        rec = {"seat": "codex", "harness": "orca", "handle": "term_x"}
        ad = mock.Mock()

        def row(tail):
            ad.read.return_value = tail
            with mock.patch.object(seat, "_spawn_record", return_value=rec), \
                 mock.patch.object(seat, "_seat_family",
                                   return_value=("codex", None)), \
                 mock.patch.object(seat, "_resolve_registered_pane",
                                   return_value=(ad, "term_x", "")), \
                 mock.patch("helm.proxywatch.upstream_snapshot",
                            return_value=({}, None)):
                return seat.seat_liveness("codex")

        blocked = row(self.PROMPT)
        self.assertEqual(blocked["state"], "BLOCKED_ON_HUMAN")
        self.assertEqual(blocked["prompt_type"], seat.PLAN_EXECUTION_PROMPT)
        self.assertEqual(seat.affirmative_choice(blocked["options"])[0], "1")
        permission = row(self.PROMPT.replace(
            "Claude has written up a plan and is ready to execute. "
            "Would you like to proceed?",
            "May I create a local commit after reading the plan above?"))
        self.assertEqual(permission["state"], "BLOCKED_ON_HUMAN")
        self.assertEqual(permission["prompt_type"], seat.PERMISSION_PROMPT)
        self.assertEqual(permission["options"], blocked["options"])
        idle = row("done, parked\n❯\n  ⏵⏵ bypass permissions on · 1 monitor")
        self.assertEqual(idle["state"], "IDLE")
        self.assertNotIn("prompt_type", idle)
        self.assertNotIn("options", idle)


class UpstreamWallCompositionTest(unittest.TestCase):
    """A fresh dark verdict prevents local IDLE from claiming availability."""

    def liveness(self, snapshot, tail="❯\n  ⏵⏵ bypass permissions on",
                 seat_name="codex"):
        rec = {"seat": seat_name, "harness": "orca", "handle": "term_x",
               "worktree": "/w", "room": "helm", "ts": "T"}
        ad = mock.Mock()
        ad.read.return_value = tail
        with mock.patch.object(seat, "_spawn_record", return_value=rec), \
             mock.patch.object(seat, "_seat_family",
                               return_value=("codex", None)), \
             mock.patch.object(seat, "_resolve_registered_pane",
                               return_value=(ad, "term_x", "")), \
             mock.patch("helm.proxywatch.upstream_snapshot",
                        return_value=snapshot) as upstream:
            return seat.seat_liveness(seat_name), upstream

    def test_a_dark_upstream_turns_local_IDLE_into_WALLED(self):
        row, _ = self.liveness(({
            "codex": {"state": "RATE-LIMITED", "dark": True,
                      "since": "2026-08-04T11:06:51Z", "seats": {
                          "codex": {"state": "RATE-LIMITED", "dark": True,
                                    "since": "2026-08-04T11:06:51Z"}}}}, None))
        self.assertEqual(row["state"], "WALLED")
        self.assertEqual(row["evidence"], "pane-tail+proxywatch")
        self.assertIn("RATE-LIMITED", row["blocked_on"])
        self.assertIn("2026-08-04T11:06:51Z", row["blocked_on"])
        self.assertIn(row["state"], seat._STATE_NAMES)
        self.assertEqual(row["remediation"]["restart"],
                         seat.RESTART_UNKNOWN)
        self.assertIsNone(row["remediation"]["target"])
        self.assertIsNone(row["remediation"]["action"])

    def test_a_local_proxy_cooldown_derives_the_opposite_restart_verdict(self):
        row, _ = self.liveness(({
            "codex": {"state": proxywatch._PROXY_COOLDOWN, "dark": True,
                      "since": "2026-08-04T11:06:51Z",
                      "falsification_bar_s": 1800, "seats": {"codex": {
                          "state": proxywatch._PROXY_COOLDOWN, "dark": True,
                          "since": "2026-08-04T11:06:51Z",
                          "falsification_due": True,
                          "falsification_age_s": 3600,
                          "falsification_seat": "codex"}}}}, None))
        self.assertEqual(row["state"], "WALLED")
        self.assertEqual(row["remediation"]["restart"], seat.RESTART_HELPFUL)
        self.assertEqual(row["remediation"]["target"], "proxy")
        self.assertNotIn("does not repair", row["remediation"]["evidence"])
        self.assertIn("PRESCRIBES: restart this exact proxy",
                      row["remediation"]["action"])

    def test_a_fresh_proxy_cooldown_does_not_prescribe_restart(self):
        row, _ = self.liveness(({
            "codex": {"state": proxywatch._PROXY_COOLDOWN, "dark": True,
                      "falsification_bar_s": 1800, "seats": {"codex": {
                          "state": proxywatch._PROXY_COOLDOWN, "dark": True,
                          "falsification_due": False,
                          "falsification_age_s": 10,
                          "falsification_seat": "codex"}}}}, None))
        self.assertEqual(row["state"], "WALLED")
        self.assertEqual(row["remediation"]["restart"], seat.RESTART_UNKNOWN)
        self.assertIsNone(row["remediation"]["action"])

    def test_stale_local_seat_due_prescribes_only_that_exact_seat(self):  # noqa: VACUOUS_ASSERTION — the stale seat's concrete action is the positive control before fresh-seat action absence and identity exclusion
        snap = ({"codex": {
            "state": proxywatch._PROXY_COOLDOWN, "dark": True,
            "falsification_bar_s": 1800, "seats": {
                "codex": {"state": proxywatch._PROXY_COOLDOWN, "dark": True,
                          "falsification_due": True,
                          "falsification_age_s": 3600,
                          "falsification_seat": "codex"},
                "seat-b": {"state": proxywatch._PROXY_COOLDOWN, "dark": True,
                            "falsification_due": False,
                            "falsification_age_s": 10,
                            "falsification_seat": "seat-b"}}}}, None)
        stale, _ = self.liveness(snap)
        fresh, _ = self.liveness(snap, seat_name="seat-b")
        self.assertIn("PRESCRIBES: restart this exact proxy",
                      stale["remediation"]["action"])
        self.assertNotIn("seat-b", stale["remediation"]["action"])
        self.assertEqual(fresh["remediation"]["restart"],
                         seat.RESTART_UNKNOWN)
        self.assertIsNone(fresh["remediation"]["action"])

    def test_healthy_sibling_does_not_mask_broken_seat_or_collapse_family(self):  # noqa: VACUOUS_ASSERTION — both exact liveness states and the broken seat's concrete action are positive controls before non-pause assertions
        snap = ({"codex": {"state": "UNKNOWN", "dark": False,
                           "falsification_bar_s": 1800, "seats": {
            "codex": {"state": proxywatch._PROXY_COOLDOWN, "dark": True,
                      "falsification_due": True,
                      "falsification_age_s": 3600,
                      "falsification_seat": "codex"},
            "seat-b": {"state": "HEALTHY", "dark": False}}}}, None)
        cooled, _ = self.liveness(snap)
        healthy, _ = self.liveness(snap, seat_name="seat-b")
        self.assertEqual(cooled["state"], "IDLE")
        self.assertEqual(healthy["state"], "IDLE")
        state = {"upstream": snap[0]}
        rem = seat.upstream_remediation(state, "codex", "codex")
        self.assertIn("PRESCRIBES: restart this exact proxy", rem["action"])
        self.assertEqual(seat.upstream_remediation(
            state, "codex", "seat-b")["restart"], seat.RESTART_UNKNOWN)
        self.assertFalse(proxywatch.beacon_paused(snap[0]["codex"]))

    def test_malformed_or_cross_family_clocks_never_prescribe_restart(self):  # noqa: VACUOUS_ASSERTION — the unconditional HELPFUL assertion below proves the same renderer/action door is live before every malformed must-miss
        base = {"state": proxywatch._PROXY_COOLDOWN, "dark": True,
                "falsification_due": True, "falsification_age_s": 1800,
                "falsification_seat": "codex"}
        bad = ({"falsification_age_s": True},
               {"falsification_age_s": float("nan")},
               {"falsification_age_s": -1},
               {"falsification_age_s": 10},
               {"falsification_seat": "kimi"})

        def snapshot(record, bar=900):
            return {"upstream": {"codex": {
                "state": proxywatch._PROXY_COOLDOWN, "dark": True,
                "falsification_bar_s": bar, "seats": {"codex": record}}}}

        self.assertEqual(seat.upstream_remediation(
            snapshot(base), "codex", "codex")["restart"], seat.RESTART_HELPFUL)
        for delta in bad:
            rec = dict(base, **delta)
            with self.subTest(delta=delta):
                rem = seat.upstream_remediation(
                    snapshot(rec), "codex", "codex")
                self.assertEqual(rem["restart"], seat.RESTART_UNKNOWN)
                self.assertIsNone(rem["action"])
        rem = seat.upstream_remediation(
            snapshot(base, float("inf")), "codex", "codex")
        self.assertEqual(rem["restart"], seat.RESTART_UNKNOWN)
        self.assertIsNone(rem["action"])

    def test_remediation_consumer_requires_positive_exact_integer_bar(self):  # noqa: VACUOUS_ASSERTION — direct mocked accessor bypasses owner normalization; UNKNOWN/action absence pin each hostile bar while below/exact controls pin boundary polarity
        base = {"state": proxywatch._PROXY_COOLDOWN, "dark": True,
                "falsification_due": True, "falsification_age_s": 900,
                "falsification_seat": "codex"}
        for name, bar, age in (
                ("zero", 0, 0),
                ("negative", -1, 0),
                ("bool", True, 1),
                ("float", 900.0, 900),
                ("non-numeric", "900", 900)):
            record = dict(base, falsification_bar_s=bar,
                          falsification_age_s=age)
            with mock.patch.object(proxywatch, "upstream_seat_record",
                                   return_value=(record, None)):
                rem = seat.upstream_remediation({}, "codex", "codex")
            with self.subTest(name=name):
                self.assertEqual(rem["restart"], seat.RESTART_UNKNOWN)
                self.assertIsNone(rem["action"])

        below = dict(base, falsification_bar_s=900,
                     falsification_age_s=899)
        with mock.patch.object(proxywatch, "upstream_seat_record",
                               return_value=(below, None)):
            self.assertEqual(seat.upstream_remediation(
                {}, "codex", "codex")["restart"], seat.RESTART_UNKNOWN)

        boundary = dict(base, falsification_bar_s=900)
        with mock.patch.object(proxywatch, "upstream_seat_record",
                               return_value=(boundary, None)):
            rem = seat.upstream_remediation({}, "codex", "codex")
        self.assertEqual(rem["restart"], seat.RESTART_HELPFUL)
        self.assertEqual(
            rem["action"], rem["evidence"] +
            ". PRESCRIBES: restart this exact proxy, then rerun helm proxywatch.")

    def test_a_healthy_upstream_preserves_IDLE(self):
        row, _ = self.liveness(({
            "codex": {"state": "HEALTHY", "dark": False, "seats": {
                "codex": {"state": "HEALTHY", "dark": False}}}}, None))
        self.assertEqual(row["state"], "IDLE")
        self.assertEqual(row["evidence"], "pane-tail")

    def test_an_unreadable_upstream_does_not_overwrite_pane_truth(self):
        row, _ = self.liveness((None, "state is stale"))
        self.assertEqual(row["state"], "IDLE")
        self.assertIsNone(row["blocked_on"])

    def test_sticky_dark_UNKNOWN_does_not_fabricate_a_provider_wall(self):
        """proxywatch preserves dark=True across an unreadable family cycle so
        alerts remember the episode. UNKNOWN still cannot assert a CURRENT
        refusal merely because that episode latch survived."""
        row, _ = self.liveness(({
            "codex": {"state": "UNKNOWN", "dark": True,
                      "since": "2026-08-04T11:06:51Z"}}, None))
        self.assertEqual(row["state"], "IDLE")
        self.assertEqual(row["evidence"], "pane-tail")

    def test_RUNNING_outranks_a_cached_family_wall_without_reading_it(self):  # noqa: VACUOUS_ASSERTION — the same test first drives the IDLE arm and proves this exact mock is called once, then the RUNNING arm proves the optimization skips it
        snapshot = ({"codex": {"state": "RATE-LIMITED", "dark": True,
                                "seats": {"codex": {
                                    "state": "RATE-LIMITED", "dark": True}}}},
                    None)
        idle, idle_upstream = self.liveness(snapshot)
        self.assertEqual(idle["state"], "WALLED")
        idle_upstream.assert_called_once_with()  # control: the probe can be called
        row, upstream = self.liveness(snapshot, tail="esc to interrupt")
        self.assertEqual(row["state"], "RUNNING")
        upstream.assert_not_called()

    def test_seat_where_renders_WALLED_not_a_healthy_looking_IDLE(self):
        snapshot = ({"codex": {"state": "RATE-LIMITED", "dark": True,
                               "since": "2026-08-04T11:06:51Z", "seats": {
                                   "codex": {"state": "RATE-LIMITED",
                                             "dark": True,
                                             "since": "2026-08-04T11:06:51Z"}}}},
                    None)
        row, _ = self.liveness(snapshot)
        self.assertEqual(row["state"], "WALLED")  # positive control
        rec = {"seat": "codex", "harness": "orca", "handle": "term_x",
               "worktree": "/w", "room": "helm", "ts": "T"}
        ad = mock.Mock()
        ad.read.return_value = "❯\n  ⏵⏵ bypass permissions on"
        out = io.StringIO()
        with mock.patch.object(seat, "_spawn_record", return_value=rec), \
             mock.patch.object(seat, "_seat_family",
                               return_value=("codex", None)), \
             mock.patch.object(seat, "_resolve_registered_pane",
                               return_value=(ad, "term_x", "")), \
             mock.patch("helm.proxywatch.upstream_snapshot",
                        return_value=snapshot) as upstream, \
             contextlib.redirect_stdout(out):
            rc = seat._where("codex", [])
        self.assertEqual(rc, 0)
        upstream.assert_called_once_with()
        self.assertIn("liveness WALLED (upstream RATE-LIMITED", out.getvalue())
        self.assertNotIn("liveness IDLE", out.getvalue())


class LivenessLifecycleTest(unittest.TestCase):
    """Every non-answer is an HONEST UNKNOWN, not a guess — the lifecycle walk
    the lane brief demanded: no record, headless, read-failed, stale handle."""

    def test_a_seat_with_no_spawn_record_is_UNKNOWN(self):
        with mock.patch.object(seat, "_spawn_record", return_value=None), \
             mock.patch.object(seat, "_seat_family", return_value=("kimi", None)):
            r = seat.seat_liveness("kimi")
        self.assertEqual(r["state"], "UNKNOWN")
        self.assertEqual(r["evidence"], "no-record")

    def test_a_headless_seat_with_a_dead_pid_is_GONE(self):
        rec = {"seat": "kimi", "harness": "headless", "pid": 12345}
        with mock.patch.object(seat, "_spawn_record", return_value=rec), \
             mock.patch.object(seat, "_seat_family", return_value=("kimi", None)), \
             mock.patch.object(seat, "_recorded_pid_alive", return_value=False):
            r = seat.seat_liveness("kimi")
        self.assertEqual(r["state"], "GONE")
        self.assertEqual(r["evidence"], "pid-dead")

    def test_a_headless_seat_with_a_live_pid_is_UNKNOWN_not_LIVE(self):
        """A headless seat has no pane tail, and /proc is FORBIDDEN as a
        liveness probe — so a live headless pid is UNKNOWN, never inferred LIVE."""
        rec = {"seat": "kimi", "harness": "headless", "pid": 12345}
        with mock.patch.object(seat, "_spawn_record", return_value=rec), \
             mock.patch.object(seat, "_seat_family", return_value=("kimi", None)), \
             mock.patch.object(seat, "_recorded_pid_alive", return_value=True):
            r = seat.seat_liveness("kimi")
        self.assertEqual(r["state"], "UNKNOWN")
        self.assertEqual(r["evidence"], "headless")

    def test_a_stale_handle_is_UNKNOWN_not_GONE(self):
        """The register names a pane the harness no longer lists — that is a
        stale register, not proof the agent is gone."""
        rec = {"seat": "kimi", "harness": "orca", "handle": "term_x"}
        with mock.patch.object(seat, "_spawn_record", return_value=rec), \
             mock.patch.object(seat, "_seat_family", return_value=("kimi", None)), \
             mock.patch.object(seat, "_resolve_registered_pane",
                               return_value=(None, None, "is not live")):
            r = seat.seat_liveness("kimi")
        self.assertEqual(r["state"], "UNKNOWN")
        self.assertEqual(r["evidence"], "stale-handle")

    def test_a_failed_pane_read_is_UNKNOWN_not_a_guess(self):
        """A pane we cannot read is a pane we know NOTHING about."""
        rec = {"seat": "kimi", "harness": "orca", "handle": "term_x"}
        ad = mock.Mock()
        ad.read.side_effect = OSError("boom")
        with mock.patch.object(seat, "_spawn_record", return_value=rec), \
             mock.patch.object(seat, "_seat_family", return_value=("kimi", None)), \
             mock.patch.object(seat, "_resolve_registered_pane",
                               return_value=(ad, "term_x", "")):
            r = seat.seat_liveness("kimi")
        self.assertEqual(r["state"], "UNKNOWN")
        self.assertEqual(r["evidence"], "read-failed")

    def test_a_live_pane_with_an_unrecognized_tail_is_UNKNOWN_not_IDLE(self):
        """THE UNKNOWN-NEVER-COLLAPSES invariant at the seat_liveness level. A
        live, readable pane whose tail matches NO known state must report
        UNKNOWN — collapsing it to IDLE (a guessed state) is how a watchdog acts
        on a guess and acts wrong. Distinct from the _classify-level test: this
        exercises the seat_liveness fallback, which a mutation proved had no
        coverage."""
        rec = {"seat": "kimi", "harness": "orca", "handle": "term_x"}
        ad = mock.Mock()
        ad.read.return_value = "totally unrecognized output"
        with mock.patch.object(seat, "_spawn_record", return_value=rec), \
             mock.patch.object(seat, "_seat_family", return_value=("kimi", None)), \
             mock.patch.object(seat, "_resolve_registered_pane",
                               return_value=(ad, "term_x", "")):
            r = seat.seat_liveness("kimi")
        self.assertEqual(r["state"], "UNKNOWN")
        self.assertEqual(r["evidence"], "pane-tail")

    def test_the_resolved_adapter_is_the_one_read_from(self):
        """FIX (P1, cross-family): the read must go through the RESOLVED
        adapter, not a hardcoded OrcaAdapter — a herdr-hosted seat read through
        orca falls to UNKNOWN for no reason, and hardcoding the harness defeats
        the pattern-table's data-change property. Assert the object read from IS
        the adapter the register resolved."""
        rec = {"seat": "kimi", "harness": "herdr", "handle": "term_x"}
        ad = mock.Mock()
        ad.read.return_value = "esc to interrupt"   # a RUNNING tail
        with mock.patch.object(seat, "_spawn_record", return_value=rec), \
             mock.patch.object(seat, "_seat_family", return_value=("kimi", None)), \
             mock.patch.object(seat, "_resolve_registered_pane",
                               return_value=(ad, "term_x", "")):
            r = seat.seat_liveness("kimi")
        ad.read.assert_called_once_with("term_x")
        self.assertEqual(r["state"], "RUNNING")

    def test_a_non_claude_live_pane_falls_to_UNKNOWN_not_IDLE(self):
        """The integrator's not-blocking thought, made a test. `esc to
        interrupt` and the `❯` prompt are claude-family affordances. A live
        non-claude pane mid-turn that renders NEITHER must not be read as IDLE
        (a wrong action — a watchdog would treat a working seat as free) — it is
        an honest UNKNOWN."""
        rec = {"seat": "kimi", "harness": "orca", "handle": "term_x"}
        ad = mock.Mock()
        ad.read.return_value = "some non-claude pane output, working, no markers"
        with mock.patch.object(seat, "_spawn_record", return_value=rec), \
             mock.patch.object(seat, "_seat_family", return_value=("kimi", None)), \
             mock.patch.object(seat, "_resolve_registered_pane",
                               return_value=(ad, "term_x", "")):
            r = seat.seat_liveness("kimi")
        self.assertEqual(r["state"], "UNKNOWN")
        self.assertNotEqual(r["state"], "IDLE")

    def test_adopted_seat_liveness_from_orcaadopt(self):
        """#138: seat.seat_liveness() for an adopted (non-family) seat returns
        RUNNING when orcaadopt proves a live process."""
        info = {
            "seat": "custom-adopted", "provenance": "orca-adopted",
            "state": "LIVE", "evidence": "seat is named by 1 live claude process (pid 7777)",
            "pids": [7777], "pane_pids": [7777]
        }
        with mock.patch.object(seat, "_seat_family", return_value=(None, "unknown seat")), \
             mock.patch("helm.orcaadopt.resolve", return_value=info):
            r = seat.seat_liveness("custom-adopted")
            # LIVE, not RUNNING (#141): process evidence stays a process claim.
            self.assertEqual(r["state"], "LIVE")
            self.assertEqual(r["evidence"], "orca-adopted")
            self.assertIn("7777", r["detail"])

    def test_a_raising_adoption_read_is_not_a_seat_with_no_record(self):
        """A family-less seat whose orca census RAISED must not render like
        one the census does not hold. `no-record` says helm has no record of
        the seat; for a raise, helm never managed to ask. Both halves are
        asserted, so the arm goes red if either collapses into the other."""
        with mock.patch.object(seat, "_seat_family",
                               return_value=(None, "unknown seat")), \
             mock.patch("helm.orcaadopt.resolve",
                        side_effect=OSError("census root unreadable")):
            raised = seat.seat_liveness("custom-adopted")
        with mock.patch.object(seat, "_seat_family",
                               return_value=(None, "unknown seat")), \
             mock.patch("helm.orcaadopt.resolve", return_value=None):
            absent = seat.seat_liveness("custom-adopted")
        self.assertEqual(raised["state"], "UNKNOWN")
        self.assertEqual(raised["evidence"], "adopt-read-failed")
        self.assertIn("OSError", raised["detail"])
        self.assertIn("census root unreadable", raised["detail"])
        self.assertEqual(absent["state"], "UNKNOWN")
        self.assertEqual(absent["evidence"], "no-record")
        self.assertEqual(absent["detail"], "unknown seat")

    def test_every_adopted_evidence_path_speaks_the_declared_vocabulary(self):  # noqa: VACUOUS_ASSERTION — membership is the observable; mutation M1 (LIVE removed from _STATE_NAMES) reddens exactly this arm
        """#141 acceptance (OI's brief): seat_liveness's docstring promises a
        member of _STATE_NAMES and the ADOPTED process-evidence path leaked
        orcaadopt's transport vocabulary instead. Assert the invariant on BOTH
        surfaces that claim the rich shape: seat.seat_liveness() AND the
        liveness dict resolve() embeds (the one `seat where --json` prints)."""
        import unittest.mock as mock
        from helm import orcaadopt
        info = {
            "seat": "adopted-x", "provenance": "orca-adopted",
            "state": "LIVE", "evidence": "seat is named by 1 live claude process (pid 4242)",
            "pids": [4242], "pane_pids": [4242],
            "liveness": {"seat": "adopted-x", "state": "LIVE", "blocked_on": None,
                         "evidence": "seat is named by 1 live claude process (pid 4242)",
                         "detail": None},
        }
        with mock.patch.object(seat, "_seat_family", return_value=(None, "unknown seat")), \
             mock.patch("helm.orcaadopt.resolve", return_value=info):
            r = seat.seat_liveness("adopted-x")
        self.assertIn(r["state"], seat._STATE_NAMES)
        from tests.test_orcaadopt import _proc
        nameless = _proc(4242, seat="adopted-x", pane_key="pk-4242")
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([nameless], [])), \
             mock.patch("helm.seats.roster_checked",
                        return_value=({"adopted-x": {"session": "sid-x"}}, False)), \
             mock.patch("helm.sessions.live_sids", return_value={}):
            live_info = orcaadopt.resolve("adopted-x")
        self.assertIsNotNone(live_info)
        self.assertIn(live_info["liveness"]["state"], seat._STATE_NAMES,
                      "resolve()'s embedded liveness dict claims the rich shape "
                      "and must speak the rich vocabulary")

    def test_process_evidence_reads_LIVE_never_RUNNING(self):
        """#141 semantic arm: RUNNING is PANE-TAIL evidence (a turn in flight);
        LIVE is PROCESS evidence (a process exists — a WEDGED process is still
        a process). The old LIVE->RUNNING upgrade promoted weak evidence to a
        strong claim, which is how a hung seat reads as actively working."""
        import unittest.mock as mock
        info = {"seat": "adopted-x", "provenance": "orca-adopted",
                "state": "LIVE", "evidence": "seat is named by 1 live claude process (pid 4242)",
                "pids": [4242], "pane_pids": [4242]}
        with mock.patch.object(seat, "_seat_family", return_value=(None, "unknown seat")), \
             mock.patch("helm.orcaadopt.resolve", return_value=info):
            r = seat.seat_liveness("adopted-x")
        self.assertEqual(r["state"], "LIVE")
        self.assertNotEqual(r["state"], "RUNNING")

    def test_transport_DEAD_renders_GONE_in_the_rich_dict(self):
        """#141: DEAD is orcaadopt's transport token; the rich vocabulary's
        word for it is GONE. The embedded dict must translate, not leak."""
        import unittest.mock as mock
        from helm import orcaadopt
        with mock.patch.object(orcaadopt, "claude_processes", return_value=([], [])), \
             mock.patch("helm.seats.roster_checked",
                        return_value=({"adopted-x": {"session": "sid-dead"}}, False)), \
             mock.patch("helm.sessions.live_sids", return_value={}):
            info = orcaadopt.resolve("adopted-x")
        self.assertIsNotNone(info)
        self.assertEqual(info["state"], orcaadopt.DEAD)      # transport surface keeps its word
        self.assertEqual(info["liveness"]["state"], "GONE",  # rich surface translates
                         "the rich dict leaked the transport token")


if __name__ == "__main__":
    unittest.main()

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _pane_fixture(name):
    """A pane viewport captured live off this fleet.

    Every row the classifier READS is verbatim: the counter, the frame
    furniture under it, the input box, the model/cwd row and the status strip.
    Only the transcript PROSE bodies are shortened — the classifier never
    reads them, and they carried a live review's contents.
    """
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


def _mid_repaint_tail():
    """A LIVE turn sampled after its output landed and before its counter
    repainted — helm-codex's CL96 tail, rebuilt from the two captures.

    NOT A FIXTURE, and deliberately not written to one. Nobody caught a
    mid-paint viewport off this fleet read-only, and a file in fixtures/ would
    claim they had; helm-codex's own bytes died with the cancelled review row
    a919a291604b and are not recoverable. So the derivation is code, in the
    open: take the live pane WHOLE — transcript, advancing counter, tip,
    toast, input box, model row, status strip, every row verbatim — and insert
    ONE real assistant bullet, verbatim from the other capture, directly under
    the counter.

    THE INSERTION IS THE MODELLED EVENT and it is the only thing constructed:
    the producer appended output under the counter and has not yet repainted
    the counter below it. The footer under the insertion is the one the
    PREVIOUS paint left standing, which is why the result is row-for-row what
    a SPENT frame looks like and why no positional rule can separate them.

    MEASURED, not assumed, at three tips: trunk abdd67c9f698 answers RUNNING,
    this lane's first cut bc0706fff5a9 answers IDLE with an EMPTY composer —
    which is the pair `autocompact._fire` injects into — and this tree answers
    RUNNING.
    """
    live = _pane_fixture("live-counter-running-tail.txt").splitlines()
    counter = max(i for i, line in enumerate(live)
                  if "(1h 10m 28s · ↓ 86.4k tokens)" in line)
    bullet = next(line for line in
                  _pane_fixture("stranded-counter-idle-tail.txt").splitlines()
                  if line.startswith("● Checked live state:"))
    return "\n".join(live[:counter + 1] + [bullet] + live[counter + 1:]) + "\n"


class TheCounterCanBeStrandedTest(unittest.TestCase):
    """RUNNING's two affordances are not the same kind of evidence, and one of
    them goes stale — task/2808.

    THE DEFECT, measured live on this fleet, and one-way. `esc to
    interrupt` is footer chrome the TUI repaints every frame. The native
    elapsed-plus-token counter is drawn INSIDE THE TRANSCRIPT, one row above
    the input box, so when the viewport scrolls a whole stale frame — counter,
    tip row, its own rule/❯/model rows — is left stranded above newer output
    and is never repainted. The table matched the counter ANYWHERE in the
    tail, so a seat whose last turn had landed read RUNNING forever.

    WHY THAT IS TERMINAL RATHER THAN TRANSIENT. autocompact refuses to queue
    /compact into an open turn, correctly; a seat it keeps reading RUNNING is
    never eligible, its context only climbs, and a turn that never lands never
    yields the empty composer that would clear the refusal by itself. Measured
    on helm-codex: three consecutive refusals, context 80.2% -> 80.7%, never
    falling. What repaints an otherwise idle pane and strands the frame is the
    seat's OWN beacon Monitor firing — which every seat in this fleet is
    instructed to keep armed.

    THE COUNTER IS FROZEN, WHICH IS THE READING THAT SETTLES IT: read three
    times over several minutes, the stranded pane's counter was byte-identical
    at `35m 5s` while the live pane's advanced 1h 6m -> 1h 9m between reads.
    """

    def test_a_stranded_counter_over_a_landed_turn_is_IDLE(self):
        tail = _pane_fixture("stranded-counter-idle-tail.txt")
        # MUST-HIT seeds: this fixture really carries the defect's inputs.
        self.assertIn("(35m 5s · ↓ 97.3k tokens)", tail,
                      "fixture lost the stranded counter")
        self.assertIn("✻ Cogitated for 10m 25s · done 10:51 AM", tail,
                      "fixture lost the turn-landed marker below the counter")
        self.assertNotIn("esc to interrupt", tail)
        self.assertEqual(seat._current_prompt_line(tail), "❯")
        self.assertEqual(seat._classify_pane_tail(tail), ("IDLE", None))

    def test_output_that_QUOTES_the_turn_landed_row_does_not_end_a_live_turn(self):
        """The marker is a phrase, and a phrase can be quoted. A seat reading
        this module's own fixture prints the turn-landed line as tool output,
        and in the partial-paint window that output sits below a counter that
        has not repainted. Main kept such a frame RUNNING; matching the phrase
        anywhere demoted it, and `/compact` then lands in live work."""
        box = "╭────────╮\n│ >      │\n╰────────╯\n"
        counter = "✽ Boondoggling… (3m 19s · ↓ 9.2k tokens)\n"
        landed = "✻ Cogitated for 10m 25s · done 10:51 AM"
        # THE BULLET IS READ OFF THE RECORDED PANE, NEVER TYPED. Typed from
        # memory it is U+23FA; the recorded panes draw U+25CF.
        recorded = _pane_fixture("stranded-counter-idle-tail.txt")
        bullets = {ln[0] for ln in recorded.splitlines()
                   if len(ln) > 2 and ln[1] == " " and ln[0] not in "❯ "
                   and ln[0] not in seat_lifecycle._TURN_LANDED_GLYPHS}
        self.assertIn("●", bullets, "fixture lost its assistant bullet")
        rows = [("indented tool output", "  ⎿  " + landed),
                ("mid-sentence prose", "  the row reads " + landed)]
        rows += [("column-zero bullet U+%04X" % ord(b), b + landed[1:])
                 for b in sorted(bullets | {"⏺", "-", "*", "•"})]
        for label, row in rows:
            with self.subTest(quoted_as=label):
                tail = counter + "⏺ Bash(cat the fixture)\n" + row + "\n" + box
                self.assertEqual(seat._classify_pane_tail(tail)[0], "RUNNING")
        # THE SAME ROW AS THE PRODUCER DRAWS IT still ends the turn, so the
        # arms above are about the row's SHAPE and not a marker that went deaf.
        spent = counter + "⏺ Done.\n\n" + landed + "\n" + box
        self.assertNotEqual(seat._classify_pane_tail(spent)[0], "RUNNING")

    def test_a_counter_at_the_bottom_of_the_frame_is_RUNNING(self):
        """The control, and it is a SEPARATE pane read in the same sweep —
        not the stranded one with a line deleted."""
        tail = _pane_fixture("live-counter-running-tail.txt")
        self.assertIn("(1h 10m 28s · ↓ 86.4k tokens)", tail)
        self.assertNotIn("esc to interrupt", tail)
        self.assertEqual(seat._classify_pane_tail(tail), ("RUNNING", None))

    def test_everything_a_live_pane_draws_under_its_counter(self):  # noqa: VACUOUS_ASSERTION — the loop arms are positive RUNNING results on a fixed non-empty row tuple, and the unconditional must-miss below proves the predicate still demotes
        """Every row measured UNDER an advancing counter on a live pane.

        THE FIRST CUT WHITELISTED THE DECORATION allowed to follow a counter,
        and that shape cannot be maintained: the producer draws a different
        mix under every pane, and each omission demotes a running turn to
        IDLE. Measured on the fleet, it did — two seats mid-turn read IDLE,
        one of them mid-compact. These are the rows it did not know.
        """
        counter = "· Crystallizing… (1h 10m 28s · ↓ 86.4k tokens)"
        for label, row in (
                ("tip row", "  ⎿ \xa0Tip: Use /clear to start fresh"),
                ("wrapped tip continuation", "     question without interrupting"),
                ("compact progress bar", "  ▰▰▰▰▰▱▱▱▱▱ 12%"),
                ("truncated context gauge", " " * 40 + "0% until a"),
                ("check-prefixed toast",
                 " " * 20 + "✔ Update installed · Restart to update"),
                ("bare toast", "Update installed · Restart to update"),
                ("titled rule", "─" * 40 + " infra admin ─"),
                ("plain rule", "─" * 40),
                ("composer", "❯"),
                ("composer holding a draft", "❯ a draft nobody sent"),
                ("model row", "  opus-5 | ~/dev/akapug/infra"),
                ("status strip", "  ⏵⏵ bypass permissions on · 3 monitors"),
                ("agents strip head", "  ● main"),
                ("agents strip row",
                 "  ◯ general-purpose  Running the suite     12m 11s · ↓ 17k")):
            with self.subTest(row=label):
                self.assertEqual(
                    seat._classify_pane_tail("%s\n%s" % (counter, row))[0],
                    "RUNNING", "%s under the counter demoted a live turn" % label)

    def test_the_one_row_that_proves_a_counter_is_spent(self):
        """REWRITTEN, and the arm it replaces asserted the opposite.

        WHAT STOOD HERE: `counter + assistant bullet` must NOT be RUNNING,
        under the name "the two rows that prove a newer frame". That is the
        CL96 defect written down as the contract — a live turn sampled between
        its output landing and its counter repainting renders exactly those
        two rows, MAIN KEPT THAT TAIL RUNNING, and `autocompact._fire` injects
        `/compact` into IDLE with no liveness re-read. The bullet arm is
        DELETED rather than weakened: a bullet says a frame came LATER, and
        nothing on the screen makes "later" mean "over".

        WHAT REPLACES IT: the turn-landed summary alone, which the producer
        writes only once a turn has ended, plus the two controls that pin what
        is deciding — the same counter with a bullet under it, and with the
        bullet's whole input box under it, are BOTH still a live turn.
        """
        counter = "· Crystallizing… (1h 10m 28s · ↓ 86.4k tokens)"
        self.assertNotEqual(
            seat._classify_pane_tail(
                "%s\n✻ Cogitated for 10m 25s · done 10:51 AM" % counter)[0],
            "RUNNING", "the counter's own replacement did not demote it")
        for label, rows in (
                ("a bullet alone", ["● and then it kept going"]),
                ("a bullet under a whole standing input box",
                 ["● and then it kept going", "❯",
                  "  ⏵⏵ bypass permissions on"])):
            with self.subTest(rows=label):
                self.assertEqual(
                    seat._classify_pane_tail("\n".join([counter] + rows))[0],
                    "RUNNING",
                    "%s is a frame that came later, not a turn that ended; "
                    "the unresolved case must fail toward RUNNING" % label)
        # CONTROL on the same observable: with nothing below it the very same
        # counter is a live turn too, so the arms above are the rows deciding
        # it and not the fixture shape.
        self.assertEqual(seat._classify_pane_tail(counter)[0], "RUNNING")

    def test_a_live_turn_sampled_mid_repaint_is_RUNNING(self):
        """helm-codex's CL96 tail at the classifier — the expensive direction.

        This lane exists because seats that always read RUNNING climb to
        context exhaustion. Curing that by reading a live turn as IDLE trades
        a slow, visible, recoverable failure for a fast, invisible,
        destructive one, and trunk did not have it. A PARTIALLY PAINTED frame
        is a third state; collapsing it into "the turn ended" is the store
        premise `unreadable-and-empty-must-never-share-a-value`.
        """
        tail = _mid_repaint_tail()
        # MUST-HIT seeds: the tail really carries the reproduction's inputs —
        # a counter, fresh output under it, and a standing input box under
        # that, which is why it is indistinguishable from a spent frame.
        self.assertIn("(1h 10m 28s · ↓ 86.4k tokens)", tail)
        below = tail.split("(1h 10m 28s · ↓ 86.4k tokens)", 1)[1]
        self.assertIn("● Checked live state:", below)
        self.assertIn("❯", below.split("● Checked live state:", 1)[1])
        self.assertNotIn("esc to interrupt", tail)
        # The composer is EMPTY, which is the second half of the production
        # gate: a false IDLE here is not refused downstream, it is injected.
        self.assertEqual(seat._current_prompt_line(tail), "❯")
        self.assertEqual(seat._classify_pane_tail(tail), ("RUNNING", None))

    def test_the_last_counter_is_the_one_judged(self):
        """A stranded counter ABOVE a live one must not decide the pane.

        `re.search` answers with the FIRST match, and the viewport that
        produced this bug carries exactly that pair. Judging the first would
        demote a running turn while the real affordance sat lower, unexamined
        — the lesson BLOCKED_ON_QUOTA already paid for.
        """
        tail = ("- Dilly-dallying… (35m 5s · ↓ 97.3k tokens)\n"
                "● newer output below the stranded frame\n"
                "✽ Boondoggling… (12s · ↓ 300 tokens)\n"
                "❯\n  ⏵⏵ bypass permissions on")
        self.assertEqual(seat._classify_pane_tail(tail), ("RUNNING", None))

    def test_the_interrupt_affordance_is_never_demoted(self):
        """It is repainted footer chrome, so it has no staleness problem and
        this change must not give it one — loosening the guard for the
        affordance that never went stale would release a tier nothing measured.
        """
        tail = ("✻ Cogitated for 10m 25s · done 10:51 AM\n❯\n"
                "  ⏵⏵ bypass permissions on · 1 monitor · esc to interrupt")
        self.assertEqual(seat._classify_pane_tail(tail), ("RUNNING", None))


class ScrollbackIsNotNowTest(unittest.TestCase):
    """The pane tail is SCROLLBACK, so a printed line outlives the condition.

    Found by dogfooding, not by tests. codex hit 100% context; I injected
    /compact; the compact RAN — `esc to interrupt` present, tasks completing —
    and seat_liveness still said CONTEXT_FULL, because the historical line was
    still in the buffer and was ordered ABOVE the live affordance. A watchdog
    reading that keeps injecting /compact into a seat already compacting.

    TWO patterns in the table are provably about NOW, and the second one
    arrived with native claude support: `esc to interrupt` is the proxy
    families' in-flight affordance, and an elapsed-plus-token counter
    ("(3m 19s · ↓ 9.2k tokens)") is native claude's. Both are status-bar
    affordances rendered only while a turn is in flight. Printed lines — the
    context warning, the quota error, the resume offer, the weekly-limit wall —
    are all history. Live beats history, so RUNNING is checked first."""

    def test_a_compacting_seat_is_RUNNING_not_CONTEXT_FULL(self):
        tail = ("Context low (0% remaining) · Run /compact to compact\n"
                "100% context used\n"
                "  … +16 completed\n"
                "❯\n"
                "  ⏵⏵ bypass permissions on · esc to interrupt · ctrl+t\n")
        state, _why = seat._classify_pane_tail(tail)
        self.assertEqual(state, "RUNNING",
                         "a seat mid-compact read as still context-full")

    def test_a_recovered_quota_seat_is_RUNNING_not_BLOCKED(self):
        tail = ("error: usage balance exhausted\n"
                "(topped up, resumed)\n"
                "❯\n  ⏵⏵ bypass permissions on · esc to interrupt\n")
        self.assertEqual(seat._classify_pane_tail(tail)[0], "RUNNING")

    def test_a_respawned_pane_is_RUNNING_not_EXITED(self):
        tail = ("Resume this session with: claude --resume 9e4e21a1-0356\n"
                "(new session started)\n"
                "❯\n  ⏵⏵ bypass permissions on · esc to interrupt\n")
        self.assertEqual(seat._classify_pane_tail(tail)[0], "RUNNING")

    def test_the_blocked_states_still_win_when_NO_turn_is_running(self):
        """The reorder must not blind the table: with no live affordance the
        printed lines are the best evidence there is, and each must still
        classify to its own remedy."""
        for line, want in (
                ("100% context used", "CONTEXT_FULL"),
                ("error: usage balance exhausted", "BLOCKED_ON_QUOTA"),
                ("Resume this session with: claude --resume abc12345",
                 "EXITED_PANE_ALIVE"),
                ("  1. Yes, and bypass permissions", "BLOCKED_ON_HUMAN")):
            with self.subTest(want=want):
                self.assertEqual(seat._classify_pane_tail(line + "\n❯\n")[0],
                                 want)


class NativeClaudePaneIsReadableTest(unittest.TestCase):
    """The table had never seen a native claude pane, in EITHER direction.

    Three native seats sat parked at a vendor credential dialog for seventeen
    hours while every health surface read fresh. The classifier could not see
    that they were stuck, and could not see them working either: RUNNING keyed
    on an affordance native claude does not render, so a mid-turn native seat
    classified (None, None), and the quota patterns matched none of the text
    native claude actually prints, so a walled one fell through to IDLE — the
    same word a healthy waiting seat gets.

    EVERY LITERAL BELOW IS PRODUCER BYTES. The status lines were read off the
    live panes; the wall and dialog strings come from the shipped executable's
    own strings. The pane tail is a VIEWPORT — about thirty-five lines, and
    asking for forty thousand returns the same thirty-five — so a dialog
    cannot be read back once it is answered, which is exactly why these are
    copied rather than remembered.
    """

    WALL = ("  ⎿ \xa0You've hit your weekly limit · resets Sep 15, 12am "
            "(America/Los_Angeles)")
    HINT = "     /usage-credits to finish what you\u2019re working on."
    FOOTER = ("────────────────────────────\n❯\n────────────────────────────\n"
              "  opus-5 | ~/dev/akapug/helm\n"
              "  ⏵⏵ bypass permissions on · 1 shell, 3 monitors · ← for agents")

    import datetime as _dt
    #: THE WALL ABOVE IS DATED, SO THE CLOCK IS PINNED. `_wall_in_force` judges
    #: its reset instant against now; on the real clock these arms went red on
    #: trunk the day that literal arrived, and every lane's gate with them.
    #: The instant sits three days before the reset, as it did when the bytes
    #: were copied, and WallExpiryTest pins the same way.
    NOW = _dt.datetime(2026, 9, 12, 9, 0)

    def setUp(self):
        real = seat._wall_in_force
        pin = mock.patch.object(seat, "_wall_in_force",
                                lambda line, now=self.NOW: real(line, now))
        pin.start()
        self.addCleanup(pin.stop)
        # MUST-HIT ON THE PIN: the facade answers IN_FORCE at NOW, and the
        # unpinned judge answers EXPIRED a week later, so the fixture really
        # is date-sensitive and the pin is what the arms below stand on.
        self.assertEqual(seat._wall_in_force(self.WALL), seat.WALL_IN_FORCE)
        self.assertEqual(real(self.WALL, self.NOW + self._dt.timedelta(days=7)),
                         seat.WALL_EXPIRED)

    def test_a_mid_turn_native_pane_is_RUNNING(self):  # noqa: VACUOUS_ASSERTION — the must-MISS on the finished form is the last statement and runs unconditionally; the RUNNING positives are per-glyph subTests because the glyph is the varying input, and collapsing them would delete the thing under test
        """The COUNTER is the anchor, not the glyph and not the gerund.

        The spinner glyph cycles between reads and the word comes from a list
        nobody outside the vendor controls, so matching either would be
        matching decoration. A parenthesised elapsed time followed by a token
        count cannot be present unless a turn is running.
        """
        for glyph in ("✽", "✻"):
            with self.subTest(glyph=glyph):
                tail = "%s Boondoggling… (3m 19s · ↓ 9.2k tokens)" % glyph
                self.assertEqual(seat._classify_pane_tail(tail),
                                 ("RUNNING", None))
        # MUST-MISS: the FINISHED form carries no counter and must not read as
        # a turn in flight, or every completed seat reads busy forever.
        done = "✻ Brewed for 2m 0s · done 8:54 AM · 1 shell, 3 monitors still running"
        self.assertNotEqual(seat._classify_pane_tail(done)[0], "RUNNING")

    def test_a_parked_wall_is_BLOCKED_ON_QUOTA_not_IDLE(self):
        """The state three seats sat in, and the one a bare pattern table
        cannot tell from a healthy seat waiting for work."""
        state, detail = seat._classify_pane_tail(
            "  Ran 2 shell commands\n" + self.WALL + "\n" + self.HINT)
        self.assertEqual(state, "BLOCKED_ON_QUOTA")
        self.assertIn("weekly limit", detail)

    def test_the_wall_survives_its_own_hint_and_an_EMPTY_composer(self):
        """Neither is work, and counting either clears the wall on exactly the
        parked pane this exists to catch. The producer prints the remedy hint
        under its own message, and a bare composer IS the parked state."""
        tail = "\n".join([self.WALL, self.HINT, "", self.FOOTER])
        self.assertEqual(seat._classify_pane_tail(tail)[0], "BLOCKED_ON_QUOTA")

    def test_an_unsubmitted_draft_below_the_wall_does_NOT_clear_it(self):
        """The other half of POSITION: what sits below the wall must be work
        the agent PRODUCED, not bytes a human left in the composer. A draft is
        the least-verified thing on a pane — nobody sent it — and reading it as
        recovery un-walls a seat that is still walled."""
        tail = "\n".join([self.WALL, self.HINT, "❯ cred switched, continue",
                          self.FOOTER])
        self.assertEqual(seat._classify_pane_tail(tail)[0], "BLOCKED_ON_QUOTA")
        # MUST-HIT on the same shape: real output in that position DOES clear
        # it, so this arm is about the composer and not about the position —
        # and the footer is present in both, or the tail matches no state at
        # all and the comparison is between two Nones.
        cleared = "\n".join([self.WALL, self.HINT,
                             "● Measurement falsified my own claim.",
                             self.FOOTER])
        self.assertEqual(seat._classify_pane_tail(cleared)[0], "IDLE")

    def test_agent_output_BELOW_the_wall_clears_it(self):
        """POSITION, NOT TIME, and measurement is what chose it.

        The reset clause dates the wall on the account that hit it, and a cred
        SWITCH moves the seat to another account — so a seat can be working
        while its old wall still names an instant days in the future. Three
        seats were in exactly that state. Anything the agent renders BELOW the
        line proves the wall is not binding, whichever credential answered.
        """
        # UNCONDITIONAL POSITIVE CONTROL: the identical tail with NOTHING
        # below the wall really is a wall, so each NotEqual below is about the
        # line that was added and not about a fixture that never classified.
        self.assertEqual(
            seat._classify_pane_tail(
                "\n".join([self.WALL, self.HINT, self.FOOTER]))[0],
            "BLOCKED_ON_QUOTA")
        # A COMPOSER LINE IS DELIBERATELY NOT IN THIS LIST. The pane cannot
        # tell a submitted line from one still sitting unsent in the composer —
        # both render identically — so counting it as work clears a live wall
        # on the least-verified thing on the screen. The draft case is pinned
        # in its own arm above.
        for label, below in (
                ("agent output", "● Measurement falsified my own claim."),
                ("a tool result", "  Ran 1 shell command")):
            with self.subTest(below=label):
                tail = "\n".join([self.WALL, self.HINT, below, self.FOOTER])
                self.assertNotEqual(
                    seat._classify_pane_tail(tail)[0], "BLOCKED_ON_QUOTA",
                    "%s below the wall did not clear it" % label)

    def test_a_live_turn_outranks_a_stale_wall_in_the_same_buffer(self):
        """The doctrine `ScrollbackIsNotNowTest` states, on the new patterns."""
        tail = "\n".join([self.WALL, self.HINT,
                          "✻ Boondoggling… (1m 7s · ↓ 2.2k tokens)"])
        self.assertEqual(seat._classify_pane_tail(tail), ("RUNNING", None))

    def test_the_vendor_dialog_is_its_own_state(self):
        """ENTITLEMENT and REACHABILITY are different questions, and the
        seventeen-hour state was the pair coming apart: the cred switch fixed
        the entitlement and the menu stayed on screen waiting for a keystroke.
        A menu is a redrawn widget, so unlike the wall it has no staleness
        problem at all — which is why the escape arm gates on THIS."""
        tail = "\n".join([self.WALL,
                          "  1. Continue with usage credits",
                          "  2. No, keep my current model"])
        state, detail = seat._classify_pane_tail(tail)
        self.assertEqual(state, "BLOCKED_ON_VENDOR_PROMPT")
        self.assertIn("usage credits", detail)


class WallExpiryTest(unittest.TestCase):
    """The reset instant is the SECOND rung, for a wall line with nothing
    below it. It is not load-bearing — position is — but it still must not
    invent an answer."""

    import datetime as _dt
    NOW = _dt.datetime(2026, 9, 12, 9, 0)

    def line(self, when):
        return "You've hit your weekly limit · resets %s (America/Los_Angeles)" % when

    def test_a_future_instant_is_in_force_and_a_past_one_is_history(self):  # noqa: VACUOUS_ASSERTION — both assertions are equalities against named verdicts on the same function, and each is the other's control: a build that always answered one of them fails the other
        self.assertEqual(seat._wall_in_force(self.line("Sep 15, 12am"), self.NOW),
                         seat.WALL_IN_FORCE)
        self.assertEqual(seat._wall_in_force(self.line("Sep 10, 12am"), self.NOW),
                         seat.WALL_EXPIRED)

    def test_the_year_is_inferred_as_the_NEAREST_reading(self):  # noqa: VACUOUS_ASSERTION — the two directions across the year boundary are each other's unconditional control; a build that inferred one year always would fail exactly one of them
        """The producer prints no year, and a naive this-year reading is wrong
        exactly when a weekly wall straddles the new year — a wall printed on
        December 30th that resets January 2nd."""
        dec = self._dt.datetime(2026, 12, 30, 9, 0)
        self.assertEqual(seat._wall_in_force(self.line("Jan 2, 12am"), dec),
                         seat.WALL_IN_FORCE)
        jan = self._dt.datetime(2027, 1, 2, 9, 0)
        self.assertEqual(seat._wall_in_force(self.line("Dec 30, 12am"), jan),
                         seat.WALL_EXPIRED)

    def _pinned(self):
        """The shipped `_wall_in_force` with its OWN `now` argument supplied.

        `_classify_pane_tail` takes no clock, so the instant has to be pinned
        at the one function that reads one. Nothing about the decision is
        replaced — the same code answers, at a date these literals mean
        something on."""
        real = seat._wall_in_force
        return mock.patch.object(seat, "_wall_in_force",
                                 lambda line, now=self.NOW: real(line, now))

    def wall(self, when):
        return "  ⎿ \xa0" + self.line(when)

    #: Real agent output, which IS work and does clear a wall above it.
    OUT = "● Measurement falsified my own claim."
    #: A bare composer, which is the parked state and is NOT work.
    COMPOSER = ("────────────────────────────\n❯\n────────────────────────────\n"
                "  opus-5 | ~/dev/akapug/helm\n  ⏵⏵ bypass permissions on")

    def test_the_LATEST_wall_governs_EXPIRY_not_only_recovery(self):
        """A SEAT WALLED RIGHT NOW READ IDLE, and the reason was that two rungs
        on the same line disagreed about which occurrence they were judging.

        A pane that hits the wall twice carries both lines — `re.search`
        returns the FIRST, whose reset instant has already passed. Dating that
        one answers EXPIRED, the matcher moves on, and the pane classifies by
        whatever else is on it: a bare composer, so IDLE. Recovery had already
        been moved onto the last occurrence; expiry had not, so the two rungs
        were reading different walls off one pane.

        The third case is the CONTROL that keeps this from being "always say
        blocked": one expired wall alone is still history, and still IDLE.
        """
        with self._pinned():
            both = "\n".join([self.wall("Sep 10, 12am"),
                               self.wall("Sep 15, 12am"), self.COMPOSER])
            self.assertEqual(seat._classify_pane_tail(both)[0],
                             "BLOCKED_ON_QUOTA")
            # THE ANSWER BETWEEN THEM changes nothing: work counts only if it
            # came after the wall being judged, and this work precedes it.
            answered = "\n".join([self.wall("Sep 10, 12am"), self.OUT,
                                   self.wall("Sep 15, 12am"), self.COMPOSER])
            self.assertEqual(seat._classify_pane_tail(answered)[0],
                             "BLOCKED_ON_QUOTA")
            # CONTROL: the old wall by itself is history and must stay IDLE.
            stale = "\n".join([self.wall("Sep 10, 12am"), self.COMPOSER])
            self.assertEqual(seat._classify_pane_tail(stale)[0], "IDLE")

    #: An UNDATED wall — the spelling that carries no reset clause at all.
    UNDATED = "  ⎿ \xa0usage balance exhausted"

    def test_the_current_wall_is_resolved_ACROSS_PATTERNS_not_within_one(self):
        """THE TWO RUNGS WERE READING DIFFERENT WALLS, and a working seat paid
        for it. There are seven quota spellings. Recovery already anchored on
        the last wall across ALL of them; expiry dated the last occurrence
        WITHIN whichever pattern matched first. On a pane carrying an undated
        credential wall, real work, and a LATER weekly wall whose reset instant
        has passed, the undated line answered BLOCKED — so a seat that had
        demonstrably resumed read walled, and the wall it was judged on was not
        even the most recent one.

        Resolving ONE current wall across every pattern and judging both rungs
        against it settles all three cases below with no per-pattern order."""
        with self._pinned():
            # The mixed tail: the newest wall is EXPIRED, so nothing is walling
            # this seat now and the pane classifies by what is actually there.
            mixed = "\n".join([self.UNDATED, self.OUT,
                                self.wall("Sep 10, 12am"), self.COMPOSER])
            self.assertEqual(seat._classify_pane_tail(mixed)[0], "IDLE")
            # SAME SHAPE, NEWEST WALL STILL IN FORCE: blocked. This is the
            # must-hit — without it the arm above would pass on a build that
            # simply never reports BLOCKED for a mixed tail.
            current = "\n".join([self.UNDATED, self.OUT,
                                  self.wall("Sep 15, 12am"), self.COMPOSER])
            state, detail = seat._classify_pane_tail(current)
            self.assertEqual(state, "BLOCKED_ON_QUOTA")
            self.assertIn("weekly limit", detail,
                          "judged the UNDATED wall, not the current one")
            # AND THE UNDATED WALL KEEPS ITS OWN MEANING when it IS the last
            # one: no reset clause means its expiry is unknowable, and this
            # fails toward the loud state rather than clearing the seat.
            alone = "\n".join([self.UNDATED, self.COMPOSER])
            state, detail = seat._classify_pane_tail(alone)
            self.assertEqual(state, "BLOCKED_ON_QUOTA")
            self.assertIn("UNKNOWN", detail)

    def test_the_two_rungs_share_ONE_coordinate_space(self):
        """A CHARACTER IN AN UNRELATED WORD DECIDED WHETHER A SEAT WAS WALLED.

        Lowercasing is not length-preserving — a dotted capital I lowercases to
        TWO codepoints — and a wall scan and a recovery scan over DIFFERENT
        copies of the tail, with an offset passed from one into the other,
        shift every offset after such a character by one, so the
        shifted end consumed the wall's newline, and the recovery rung then
        skipped the work line sitting right below the wall.

        The pair is the whole arm: identical tails, identical walls, identical
        work, differing only in a diacritic in a word no rung looks at. If they
        ever disagree again, two scans have come back.
        """
        with self._pinned():
            shape = "%s\n" + self.UNDATED + "\n" + self.OUT + "\n" + self.COMPOSER
            dotted = seat._classify_pane_tail(shape % "İpek")
            plain = seat._classify_pane_tail(shape % "Ipek")
            self.assertEqual(dotted[0], plain[0],
                             "a lowercase-expanding character moved the wall: "
                             "%r vs %r" % (dotted, plain))
            # AND THE SHARED ANSWER IS THE CORRECT ONE, not merely a matching
            # pair: work sits below the wall, so both are recovered.
            self.assertEqual(plain[0], "IDLE")

    def test_work_after_the_LATEST_wall_still_clears_it(self):
        """POSITION KEEPS ITS PRIMACY over the instant. Anchoring expiry on the
        last occurrence must not make a recovered seat unrecoverable: a wall
        still in force, with real agent output printed below it, is a seat that
        is working again — and its own wall line is scrollback."""
        with self._pinned():
            worked = "\n".join([self.wall("Sep 15, 12am"), self.OUT,
                                 self.COMPOSER])
            self.assertEqual(seat._classify_pane_tail(worked)[0], "IDLE")
            # MUST-HIT on the same shape: without the output it is blocked, so
            # this arm measures the OUTPUT and not the fixture's other lines.
            parked = "\n".join([self.wall("Sep 15, 12am"), self.COMPOSER])
            self.assertEqual(seat._classify_pane_tail(parked)[0],
                             "BLOCKED_ON_QUOTA")

    def test_an_unparseable_or_absent_clause_is_UNDATED_never_a_guess(self):
        """UNDATED is its own answer. The pre-existing patterns carry no reset
        clause at all and keep their historical meaning; a DATED line this
        build can no longer parse fails toward the loud state, because a false
        wall is visible and recoverable while a false clean bill is the
        seventeen-hour silence."""
        self.assertEqual(seat._wall_in_force("usage balance exhausted", self.NOW),
                         seat.WALL_UNDATED)
        self.assertEqual(seat._wall_in_force(self.line("Smarch 41, 99pm"), self.NOW),
                         seat.WALL_UNDATED)
        # AND AN UNDATED WALL STILL CLASSIFIES, carrying its own uncertainty.
        state, detail = seat._classify_pane_tail(
            "You've hit your weekly limit, somehow")
        self.assertEqual(state, "BLOCKED_ON_QUOTA")
        self.assertIn("UNKNOWN", detail)


class EmptyReadIsNotAnUnrecognizedTailTest(unittest.TestCase):
    """`read` turns a FAILED read into "" — a stale handle, a dead runtime and
    a blank pane all arrive as the same empty string. Feeding that to the
    matcher makes a claim about the CONTENT of something never read.

    Live 2026-07-26: orca .46 reminted every terminal handle, all six seats
    answered terminal_handle_stale, and this surface reported "a live pane
    whose tail matches no known state" for every one. Three truths — handle
    stale / read failed / rendering changed — collapsed into one wrong answer,
    each wanting a different remedy."""

    def test_an_empty_tail_never_reaches_the_matcher(self):
        for blank in ("", "   ", "\n\n", None):
            with self.subTest(blank=repr(blank)):
                self.assertIsNone(seat._classify_pane_tail(blank or "")[0],
                                  "the matcher claimed a state for nothing")

    def test_empty_read_is_its_own_evidence_not_pane_tail(self):
        """The evidence string IS the remedy pointer — conflating them sends
        the operator to add a pattern when the handle needs re-resolving."""
        import unittest.mock as m
        with m.patch.object(seat, "_seat_family", return_value=("codex", None)), \
             m.patch.object(seat, "_spawn_record", return_value={"harness": "orca"}), \
             m.patch.object(seat, "_instance_dir", return_value="/tmp"), \
             m.patch.object(seat, "_resolve_registered_pane",
                            return_value=(_BlankAdapter(), "term_x", None)):
            r = seat.seat_liveness("codex")
        self.assertEqual(r["state"], "UNKNOWN")
        self.assertEqual(r["evidence"], "empty-read",
                         "an empty read was reported as an unrecognized tail")
        self.assertIn("stale handle", r["detail"])
        self.assertEqual(r["remediation"]["restart"], seat.RESTART_UNKNOWN)
        self.assertIn("cannot tell", r["remediation"]["evidence"])
        self.assertNotIn("respawn", r["detail"])

    def test_a_REAL_tail_still_classifies(self):
        """The guard must not blind the surface to genuine states."""
        self.assertEqual(
            seat._classify_pane_tail("  ⏵⏵ bypass · esc to interrupt")[0],
            "RUNNING")


class _BlankAdapter:
    def read(self, handle, limit=3000):
        return ""



class VendorEscapeChoiceTest(unittest.TestCase):
    """WHICH option a machine may press at a vendor dialog.

    Knowing a seat is stuck says nothing about how to free it: in this dialog
    family one option resumes work for free, several spend the owner's money,
    two ask a person for budget, and one leaves the seat exactly where it was.
    The fixtures below are built from the producer's own label constants, so an
    option it renames goes red here rather than going quietly unpressed — or,
    far worse, quietly pressed.
    """

    def _dialog(self, *options):
        return ("\n".join(["You've reached your Fable limit", ""] +
                          ["%d. %s" % (n, t)
                           for n, t in enumerate(options, 1)]) + "\n")

    def test_the_forward_option_is_chosen_by_the_producers_own_number(self):
        """The whole reason the digit is captured. Here the free option is
        SECOND, and a hardcoded "1" would have pressed the purchase."""
        tail = self._dialog("Yes, buy usage credits",
                            "Switch to Sonnet and continue",
                            "No, keep my current model")
        digit, kind, seen = seat_lifecycle.vendor_escape_choice(tail)
        self.assertEqual(digit, "2")
        self.assertEqual(kind, seat_lifecycle.ESCAPE_CONTINUE)
        self.assertEqual(seen, "Switch to Sonnet and continue")

    def test_re_enable_and_continue_is_a_spend_and_is_never_pressed(self):
        """THE ONE THAT LOOKS SAFE. It is the most forward-reading label the
        producer offers and the only exit this dialog has, and what it
        re-enables is the credit spend itself."""
        tail = self._dialog("Yes, re-enable and continue",
                            "No, keep my current model")
        digit, kind, seen = seat_lifecycle.vendor_escape_choice(tail)
        self.assertIsNone(digit)
        self.assertEqual(kind, seat_lifecycle.ESCAPE_SPEND)
        self.assertEqual(seen, "Yes, re-enable and continue")

    def test_every_other_spend_label_refuses_with_its_own_text(self):
        # UNCONDITIONAL POSITIVE CONTROL ON THE SAME OBSERVABLE: this
        # function does return a digit, so every `digit is None` below is a
        # statement about these labels rather than about a chooser that never
        # chooses anything.
        self.assertEqual(seat_lifecycle.vendor_escape_choice(
            self._dialog("Switch to Sonnet and continue",
                         "No, keep my current model"))[0], "1")
        labels = ("Yes, buy usage credits", "Buy usage credits",
                  "Continue with usage credits", "Adjust monthly limit",
                  "Buy more")
        checked = 0
        for label in labels:
            with self.subTest(option=label):
                digit, kind, seen = seat_lifecycle.vendor_escape_choice(
                    self._dialog(label, "No, keep my current model"))
                self.assertIsNone(digit)
                self.assertEqual(kind, seat_lifecycle.ESCAPE_SPEND)
                self.assertEqual(seen, label)
                checked += 1
        # Unconditional: every assertion above is inside the loop, so an empty
        # or short label set would report a pass over nothing.
        self.assertEqual(checked, len(labels))
        self.assertEqual(checked, 5)

    def test_both_admin_phrasings_refuse_as_human(self):
        """Asking a person for budget is an outward-facing act on the owner's
        behalf, so it is a refusal even though it costs nothing."""
        # UNCONDITIONAL POSITIVE CONTROL ON THE SAME OBSERVABLE: this
        # function does return a digit, so every `digit is None` below is a
        # statement about these labels rather than about a chooser that never
        # chooses anything.
        self.assertEqual(seat_lifecycle.vendor_escape_choice(
            self._dialog("Switch to Sonnet and continue",
                         "No, keep my current model"))[0], "1")
        labels = ("Request more from your admin",
                  "Request usage credits from your admin")
        checked = 0
        for label in labels:
            with self.subTest(option=label):
                digit, kind, _seen = seat_lifecycle.vendor_escape_choice(
                    self._dialog(label, "No, keep my current model"))
                self.assertIsNone(digit)
                self.assertEqual(kind, seat_lifecycle.ESCAPE_HUMAN)
                checked += 1
        self.assertEqual(checked, 2)

    def test_keeping_the_current_model_is_not_an_escape(self):
        """A dialog whose only other option leaves the seat where it is offers
        no escape at all. ABSENT is the honest answer — pressing the dismissal
        would clear the dialog and report a rescue that did not happen."""
        digit, kind, seen = seat_lifecycle.vendor_escape_choice(
            self._dialog("No, keep my current model", "Cancel"))
        self.assertIsNone(digit)
        self.assertEqual(kind, seat_lifecycle.ESCAPE_ABSENT)
        self.assertEqual(seen, "")

    def test_the_whitelist_outranks_the_dialogs_own_ordering(self):
        """The positive control for the precedence: with a purchase listed
        first and the free option last, the free option still wins. Without
        this the spend refusals above would also pass if the function simply
        returned whatever it saw first."""
        tail = self._dialog("Yes, re-enable and continue",
                            "Request more from your admin",
                            "Switch to Haiku and continue")
        digit, kind, _seen = seat_lifecycle.vendor_escape_choice(tail)
        self.assertEqual(digit, "3")
        self.assertEqual(kind, seat_lifecycle.ESCAPE_CONTINUE)

    def test_a_coloured_dialog_is_still_readable(self):
        """Inherited from `_prompt_options`, and pinned HERE because it is the
        reason this chooser does not scan the tail itself: a pane is rendered
        with colour, and a matcher that saw the escape codes would refuse every
        real dialog while passing every fixture."""
        plain = self._dialog("Yes, buy usage credits",
                             "Switch to Sonnet and continue",
                             "No, keep my current model")
        coloured = plain.replace("Switch to", "\x1b[36mSwitch to").replace(
            "and continue", "and continue\x1b[0m")
        self.assertNotEqual(plain, coloured)     # the fixture really is dyed
        self.assertEqual(seat_lifecycle.vendor_escape_choice(coloured)[0], "2")

    def test_numbered_prose_above_the_dialog_is_not_an_option(self):
        """THE DANGEROUS ONE. A transcript can contain numbered lines that read
        exactly like the free option, and a keystroke chosen off prose lands in
        a composer. Only the LAST contiguous run is the dialog, so here the
        prose must lose to a real dialog that offers no escape at all."""
        prose = ("Here is the plan:\n"
                 "1. Read the config\n"
                 "2. Switch to Sonnet and continue\n"
                 "\nsome intervening output\n\n")
        tail = prose + self._dialog("Yes, re-enable and continue",
                                    "No, keep my current model")
        digit, kind, _seen = seat_lifecycle.vendor_escape_choice(tail)
        self.assertIsNone(digit, "a keystroke was chosen off transcript prose")
        self.assertEqual(kind, seat_lifecycle.ESCAPE_SPEND)
        # MUST-HIT: the same prose ahead of a dialog that DOES offer the free
        # option still resolves, so the refusal above is the run boundary and
        # not the prose poisoning every read.
        ok = prose + self._dialog("Switch to Haiku and continue",
                                  "No, keep my current model")
        self.assertEqual(seat_lifecycle.vendor_escape_choice(ok)[0], "1")

    def test_a_pane_with_no_dialog_at_all_is_absent(self):
        digit, kind, seen = seat_lifecycle.vendor_escape_choice(
            "⏺ Thinking... (12s · 340 tokens)\n")
        self.assertIsNone(digit)
        self.assertEqual(kind, seat_lifecycle.ESCAPE_ABSENT)
        self.assertEqual(seen, "")


class ARepeatedWallIsStillAWallTest(unittest.TestCase):
    """A pane on an exhausted credential prints its error again on each
    attempt, so the tail carries the wall twice.

    Read positionally the SECOND copy sits below the first, is not the
    producer's continuation hint, not chrome and not a composer — so it
    satisfied every 'is this work?' test and cleared the very wall it
    restates. The seat then reported itself IDLE while walled.
    """

    COMPOSER = "❯ "

    def test_the_same_wall_twice_is_not_recovery(self):
        tail = ("usage balance exhausted\n"
                "usage balance exhausted\n" + self.COMPOSER)
        self.assertEqual(seat_lifecycle._classify_pane_tail(tail)[0],
                         "BLOCKED_ON_QUOTA")

    def test_real_output_below_the_wall_is_still_recovery(self):
        """THE MUST-HIT. Without it the fix above could be 'never clear a wall
        at all', which breaks every seat that genuinely recovered."""
        tail = ("usage balance exhausted\n"
                "Here is the answer you asked for.\n" + self.COMPOSER)
        self.assertEqual(seat_lifecycle._classify_pane_tail(tail)[0], "IDLE")

    def test_a_different_wall_spelling_below_the_first_is_not_work_either(self):
        """The rung reads the live pattern table, so every wall spelling the
        classifier knows is covered rather than the one literal I had in mind."""
        tail = ("usage balance exhausted\n"
                "You've hit your weekly limit\n" + self.COMPOSER)
        self.assertEqual(seat_lifecycle._classify_pane_tail(tail)[0],
                         "BLOCKED_ON_QUOTA")

    def test_the_rung_refuses_to_derive_itself_from_an_empty_table(self):
        """An empty pattern list would make the wall test answer False for
        everything and silently restore the defect, so it refuses instead."""
        seat_lifecycle._WALL_LINE_RE = None
        self.addCleanup(setattr, seat_lifecycle, "_WALL_LINE_RE", None)
        with mock.patch.object(seat_lifecycle, "_LIVENESS_STATES", ()):
            with self.assertRaisesRegex(RuntimeError, "empty table"):
                seat_lifecycle._is_wall_line("usage balance exhausted")
