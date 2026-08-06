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
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import seat  # noqa: E402


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


class UpstreamWallCompositionTest(unittest.TestCase):
    """A fresh dark verdict prevents local IDLE from claiming availability."""

    def liveness(self, snapshot, tail="❯\n  ⏵⏵ bypass permissions on"):
        rec = {"seat": "codex", "harness": "orca", "handle": "term_x",
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
            return seat.seat_liveness("codex"), upstream

    def test_a_dark_upstream_turns_local_IDLE_into_WALLED(self):
        row, _ = self.liveness(({
            "codex": {"state": "RATE-LIMITED", "dark": True,
                      "since": "2026-08-04T11:06:51Z"}}, None))
        self.assertEqual(row["state"], "WALLED")
        self.assertEqual(row["evidence"], "pane-tail+proxywatch")
        self.assertIn("RATE-LIMITED", row["blocked_on"])
        self.assertIn("2026-08-04T11:06:51Z", row["blocked_on"])
        self.assertIn(row["state"], seat._STATE_NAMES)

    def test_a_healthy_upstream_preserves_IDLE(self):
        row, _ = self.liveness(({
            "codex": {"state": "HEALTHY", "dark": False}}, None))
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
        snapshot = ({"codex": {"state": "RATE-LIMITED", "dark": True}}, None)
        idle, idle_upstream = self.liveness(snapshot)
        self.assertEqual(idle["state"], "WALLED")
        idle_upstream.assert_called_once_with()  # control: the probe can be called
        row, upstream = self.liveness(snapshot, tail="esc to interrupt")
        self.assertEqual(row["state"], "RUNNING")
        upstream.assert_not_called()

    def test_seat_where_renders_WALLED_not_a_healthy_looking_IDLE(self):
        snapshot = ({"codex": {"state": "RATE-LIMITED", "dark": True,
                               "since": "2026-08-04T11:06:51Z"}}, None)
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
                        return_value=snapshot), \
             contextlib.redirect_stdout(out):
            rc = seat._where("codex", [])
        self.assertEqual(rc, 0)
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

class ScrollbackIsNotNowTest(unittest.TestCase):
    """The pane tail is SCROLLBACK, so a printed line outlives the condition.

    Found by dogfooding, not by tests. codex hit 100% context; I injected
    /compact; the compact RAN — `esc to interrupt` present, tasks completing —
    and seat_liveness still said CONTEXT_FULL, because the historical line was
    still in the buffer and was ordered ABOVE the live affordance. A watchdog
    reading that keeps injecting /compact into a seat already compacting.

    `esc to interrupt` is the only pattern in the table that is provably about
    NOW: it is a status-bar affordance rendered only while a turn is in flight.
    Printed lines — the context warning, the quota error, the resume offer —
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

    def test_a_REAL_tail_still_classifies(self):
        """The guard must not blind the surface to genuine states."""
        self.assertEqual(
            seat._classify_pane_tail("  ⏵⏵ bypass · esc to interrupt")[0],
            "RUNNING")


class _BlankAdapter:
    def read(self, handle, limit=3000):
        return ""

