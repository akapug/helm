#!/usr/bin/env python3
"""idle_dispatch tests — the stranded-obligation join (open dispatch x idle
recipient). Hermetic: dispatches.open_rows, presence, claims, derive_seat, and
dm are all patched, so no ledger/roster/chat node is touched. Verifies the
watchdog flags EXACTLY the stranded dispatches and DMs the resolved sender."""
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-idle-", var="HELM_HOME")

from helm import idle_dispatch  # noqa: E402


def _row(rid, recipient, source="sess-oi", sender=None, deadline_s=2700):
    return {"id": rid, "recipient": recipient, "source": source,
            "sender": sender, "lane": "review", "deadline_s": deadline_s}


class ProviderWallOnAStrandedRowTest(unittest.TestCase):
    """A SEAT ITS PROVIDER IS REFUSING LOOKS EXACTLY LIKE AN ABANDONED ONE.

    No recent activity, no live claim — the STRANDED shape precisely. The alert
    then tells the sender their recipient "may be idle-done or parked WITHOUT
    reporting" and to "re-check or reassign", and every word is wrong about a
    walled seat: it is not parked, it did not fail to report, and reassigning
    punishes it for its provider's 429. Measured live: three codex seats
    sat RATE-LIMITED for five hours while every surface stayed silent.

    The classifier already existed in seat.py; idle_dispatch never asked it."""

    def wall(self, liveness):
        from helm import seat
        with mock.patch.object(seat, "seat_liveness", return_value=liveness):
            return idle_dispatch._provider_wall("codex")

    def test_a_quota_wall_is_NAMED_and_reassignment_is_contradicted(self):
        got = self.wall({"state": "BLOCKED_ON_QUOTA",
                         "blocked_on": "503 auth_unavailable"})
        self.assertIn("BLOCKED_ON_QUOTA", got)
        self.assertIn("503 auth_unavailable", got)
        self.assertIn("not parked", got)

    def test_a_cached_proxywatch_wall_is_named_without_reassignment_blame(self):
        got = self.wall({"state": "WALLED",
                         "blocked_on": "upstream RATE-LIMITED since T"})
        self.assertIn("WALLED", got)
        self.assertIn("RATE-LIMITED", got)
        self.assertIn("not parked", got)
        self.assertIn("does not repair", got)

    def test_a_healthy_seat_adds_NOTHING(self):
        """UNCONDITIONAL CONTROL: the line above proves it DOES speak, so
        silence here measures health and not a dead probe. A fact-adder that
        spoke on every row would make the STRANDED alert unreadable."""
        loud = self.wall({"state": "BLOCKED_ON_QUOTA", "blocked_on": "x"})
        self.assertIn("BLOCKED_ON_QUOTA", loud)
        self.assertEqual(self.wall({"state": "RUNNING"}), "")
        self.assertEqual(self.wall({}), "")

    def test_an_unreadable_liveness_never_corrupts_the_alert(self):
        from helm import seat
        with mock.patch.object(seat, "seat_liveness",
                               side_effect=OSError("pane gone")):
            self.assertEqual(idle_dispatch._provider_wall("codex"), "")

    def test_the_field_REACHES_the_rendered_text(self):
        """THE BUG THIS LANE SHIPPED ONCE. _alert_text builds its format dict
        EXPLICITLY, so a field added to the finding and not there raises
        KeyError inside the alert path — which surfaced as ZERO alerts, not an
        error. Testing the helper alone would not have caught it; this drives
        the renderer."""
        f = {"claim": idle_dispatch.CLAIM_NONE, "sender": "s", "id8": "abcd1234",
             "lane": "l", "recipient": "codex", "presence": "quiet",
             "age_min": 99, "wall": " Provider: BLOCKED_ON_QUOTA (503)."}
        text = idle_dispatch._alert_text(f)
        self.assertIn("STRANDED", text)          # control: the right template
        self.assertIn("BLOCKED_ON_QUOTA", text)  # and the field reached it


class IdleDispatchTest(unittest.TestCase):
    def setUp(self):
        # a fresh HELM_HOME per test so the fcntl latch state never leaks
        self.tmp = tempfile.mkdtemp(prefix="helm-test-idle-")
        # runs even if setUp fails partway; the un-cleaned version leaked one
        # dir per test into a live inode-exhaustion incident
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._prior = os.environ.get("HELM_HOME")
        os.environ["HELM_HOME"] = self.tmp
        # default world: no claims, sender resolves to opus-integrator, every
        # recipient is old enough + quiet (idle) unless a test overrides
        self.rows = []
        self.presence = {}      # recipient -> "fresh"|"quiet"|"absent"
        self.claims = {}        # claim resource key -> {}
        self.age = 3600         # default dispatch age (> IDLE_DISPATCH_S)
        self.dms = []           # captured (to, text)
        self.p = mock.patch.multiple(
            "helm.idle_dispatch.dispatches",
            open_rows=mock.Mock(side_effect=lambda: list(self.rows)),
            _age_s=mock.Mock(side_effect=lambda r, now=None: self.age),
            _is_overdue=mock.Mock(side_effect=lambda r, now=None: self.age >= r["deadline_s"]),
        )
        self.s = mock.patch.multiple(
            "helm.idle_dispatch.seats",
            _live_claims=mock.Mock(side_effect=lambda: dict(self.claims)),
            presence_of=mock.Mock(side_effect=lambda ls: ls),   # ls IS the bucket
            last_seen=mock.Mock(side_effect=lambda rec: self.presence.get(rec, "quiet")),
            derive_seat=mock.Mock(side_effect=lambda src: "opus-integrator"),
            dm=mock.Mock(side_effect=lambda to, text, **k: self.dms.append((to, text))),
        )
        self.p.start(); self.s.start()

    def tearDown(self):
        self.p.stop(); self.s.stop()
        if self._prior is not None:
            os.environ["HELM_HOME"] = self._prior

    def test_stranded_dispatch_dms_the_resolved_sender_exactly_once(self):
        self.rows = [_row("aaaaaaaa1111", "ds4pro")]
        self.presence = {"ds4pro": "absent"}
        res = idle_dispatch.check()
        self.assertEqual(len(res["alerted"]), 1)
        self.assertEqual(len(self.dms), 1)
        to, text = self.dms[0]
        self.assertEqual(to, "opus-integrator")           # DM the sender, not a broadcast
        self.assertIn("ds4pro", text)
        self.assertIn("aaaaaaaa", text)                    # the id8

    def test_a_row_that_stays_stranded_backs_off_instead_of_nagging_forever(self):
        """The flat 15-minute re-alert was an UNBOUNDED alert source.

        A dispatch can stay stranded for days — a retired recipient, a lane
        nobody picks up — and each cycle posted another DM. Measured on the
        live estate: one row stranded ~37h had generated ~148
        identical DMs, and every one is a permanent obligation in the sender's
        stop-guard (which counts UNDELIVERED rows and is not discharged by
        reading). That sender's inbox reached 497, past the point where any
        single item can be acted on — the guard meant to surface obligations
        had buried them.

        Each repeat doubles the wait, capped, so a genuinely stuck row still
        speaks at a rate someone can absorb: ~148 alerts over 37h becomes ~13.
        """
        self.rows = [_row("cccccccc3333", "ds4pro")]
        self.presence = {"ds4pro": "absent"}
        base = time.time()
        with mock.patch.object(idle_dispatch.time, "time", return_value=base):
            self.assertEqual(len(idle_dispatch.check()["alerted"]), 1)
        # one TTL later the FIRST wait is served, but the SECOND needs 2x
        with mock.patch.object(idle_dispatch.time, "time",
                               return_value=base + idle_dispatch.LATCH_TTL_S + 1):
            self.assertEqual(idle_dispatch.check()["alerted"], [],
                             "second alert must wait 2x, not 1x — that doubling "
                             "is the whole fix")
        with mock.patch.object(
                idle_dispatch.time, "time",
                return_value=base + 2 * idle_dispatch.LATCH_TTL_S + 1):
            self.assertEqual(len(idle_dispatch.check()["alerted"]), 1)

    def test_the_backoff_is_capped_so_a_stuck_row_never_goes_silent(self):
        """Doubling without a ceiling eventually means never. A row stranded
        for days must still speak — roughly once per shift — because going
        quiet on an unresolved obligation is the failure this watchdog exists
        to prevent, and would be a worse bug than the noise it replaces."""
        self.assertLessEqual(idle_dispatch.LATCH_BACKOFF_CAP_S, 6 * 60 * 60)
        wait = lambda n: min(idle_dispatch.LATCH_TTL_S * (2 ** n),
                             idle_dispatch.LATCH_BACKOFF_CAP_S)
        self.assertEqual(wait(99), idle_dispatch.LATCH_BACKOFF_CAP_S)

    def test_fresh_recipient_is_busy_not_stranded(self):
        self.rows = [_row("bbbbbbbb2222", "kimi")]
        self.presence = {"kimi": "fresh"}                  # crossed a tool boundary recently
        res = idle_dispatch.check()
        self.assertEqual(res["alerted"], [])
        self.assertEqual(self.dms, [])

    def test_self_dispatch_never_dms_yourself(self):
        # recipient == resolved sender (opus-integrator) -> no coordinator to wake
        self.rows = [_row("cccccccc3333", "opus-integrator")]
        self.presence = {"opus-integrator": "absent"}
        res = idle_dispatch.check()
        self.assertEqual(res["alerted"], [])

    def test_too_fresh_dispatch_gets_the_recipient_time(self):
        self.rows = [_row("dddddddd4444", "ds4pro")]
        self.presence = {"ds4pro": "absent"}
        self.age = idle_dispatch.IDLE_DISPATCH_S - 1       # under the soft window
        res = idle_dispatch.check()
        self.assertEqual(res["alerted"], [])

    def test_claimed_dispatch_is_being_worked(self):
        self.rows = [_row("eeeeeeee5555", "ds4pro")]
        self.presence = {"ds4pro": "quiet"}
        self.claims = {"dispatch:eeeeeeee": {"session": "x"}}   # live claim on it
        res = idle_dispatch.check()
        self.assertEqual(res["alerted"], [])

    # -- the three claim states, which demand three different actions --------

    def test_quiet_recipient_holding_a_lane_claim_is_not_stranded(self):
        """THE NEAR-MISS. The alert DMd a coordinator that a working seat
        "HOLDS NO CLAIM" while that seat held worktree:helm:lane-x
        with 8917s left and 932 uncommitted insertions in its room —
        including a test file that existed nowhere else. It was context-wedged
        (100.1% of window, pane still LIVE), which looks exactly like quiet.
        The recommended action was REASSIGNMENT, which would have destroyed all
        of it.

        The old check tested ONE key, `dispatch:<id8>`, then generalised the
        miss to a sentence about the seat. On the live estate at the time: 21 live
        claims, every one a `worktree:…` key, ZERO `dispatch:…` keys — so the
        clause was false for essentially every seat actually working."""
        self.rows = [_row("a1a1a1a1aaaa", "codex-3")]
        self.presence = {"codex-3": "quiet"}
        self.claims = {"worktree:helm:lane-x":
                       {"holder": "codex-3", "session": "s3"}}
        res = idle_dispatch.check()
        f = res["findings"][0]
        self.assertEqual(f["claim"], idle_dispatch.CLAIM_HOLDING)
        self.assertFalse(f["stranded"])
        self.assertIn("worktree:helm:lane-x", f["held"])
        to, text = self.dms[0]
        self.assertIn("QUIET BUT HOLDING", text)
        self.assertIn("RESCUE, do not reassign", text)
        self.assertIn("worktree:helm:lane-x", text)
        # the verdict AND the recommendation that would have cost the afternoon
        self.assertNotIn("is STRANDED", text)
        self.assertNotIn("Re-check or reassign", text)

    def test_quiet_recipient_holding_nothing_is_still_stranded(self):
        """The guard must not block everything: a genuinely stranded row —
        quiet AND holding no claim on any resource — still reports stranded and
        still recommends reassignment. Another seat's live claim is not this
        recipient's, so it must not suppress the finding."""
        self.rows = [_row("b2b2b2b2bbbb", "ds4pro")]
        self.presence = {"ds4pro": "quiet"}
        self.claims = {"worktree:helm:someone-elses-lane":
                       {"holder": "kimi", "session": "sk"}}
        res = idle_dispatch.check()
        f = res["findings"][0]
        self.assertEqual(f["claim"], idle_dispatch.CLAIM_NONE)
        self.assertTrue(f["stranded"])
        to, text = self.dms[0]
        self.assertIn("is STRANDED", text)
        self.assertIn("Re-check or reassign", text)
        self.assertNotIn("QUIET BUT HOLDING", text)

    def test_unreadable_claims_reports_unknown_and_never_reassign(self):
        """Unreadable is NOT "holds no claim". The old rung returned [] here —
        safe against a false reassign, but it also went silent on the one
        condition where nobody can see who owns what, which is the same silence
        this watchdog exists to break. UNKNOWN is now SURFACED and SAFE: it
        never carries the reassign recommendation."""
        self.rows = [_row("ffffffff6666", "ds4pro")]
        self.presence = {"ds4pro": "absent"}
        with mock.patch("helm.idle_dispatch.seats._live_claims", return_value=None):
            res = idle_dispatch.check()
        f = res["findings"][0]
        self.assertIn(idle_dispatch.CLAIM_UNKNOWN, f.values())   # +control on f
        self.assertFalse(f["stranded"])
        to, text = self.dms[0]
        self.assertIn("UNKNOWN", text)
        self.assertIn("Do NOT reassign on an unread fact", text)
        self.assertNotIn("is STRANDED", text)
        self.assertNotIn("Re-check or reassign", text)

    def test_a_raising_claims_read_is_unknown_not_none(self):
        """The same law one layer out: an EXCEPTION from the claims read is an
        unread fact, not an empty one."""
        self.rows = [_row("c3c3c3c3cccc", "ds4pro")]
        self.presence = {"ds4pro": "quiet"}
        with mock.patch("helm.idle_dispatch.seats._live_claims",
                        side_effect=OSError("boom")):
            res = idle_dispatch.check()
        f = res["findings"][0]
        self.assertIn(idle_dispatch.CLAIM_UNKNOWN, f.values())   # +control on f
        text = self.dms[0][1]
        self.assertIn("claim state UNKNOWN", text)               # +control on text
        self.assertNotIn("Re-check or reassign", text)

    def test_an_unattributable_live_claim_makes_the_answer_unknown(self):
        """The same inversion one level down: a live claim with no readable
        holder cannot be ruled out as THIS recipient's, so "nobody attributed a
        claim to them" must not be reported as "they hold nothing". An
        unattributable row degrades the answer to UNKNOWN — which does not
        recommend reassignment — instead of manufacturing a stranded verdict."""
        self.rows = [_row("e8e8e8e8eeee", "ds4pro")]
        self.presence = {"ds4pro": "quiet"}
        self.claims = {"worktree:helm:orphan": {"session": "s"}}   # no holder
        res = idle_dispatch.check()
        f = res["findings"][0]
        self.assertIn(idle_dispatch.CLAIM_UNKNOWN, f.values())
        self.assertFalse(f["stranded"])
        text = self.dms[0][1]
        self.assertIn("claim state UNKNOWN", text)
        self.assertNotIn("Re-check or reassign", text)

    def test_holder_match_is_case_insensitive(self):
        """A seat token that differs only in case is the SAME holder. Getting
        this wrong reproduces the original bug exactly: a real claim read as
        absent, and 'reassign' recommended over live work."""
        self.rows = [_row("d4d4d4d4dddd", "Codex-3")]
        self.presence = {"Codex-3": "quiet"}
        self.claims = {"worktree:helm:x": {"holder": "codex-3"}}
        f = idle_dispatch.scan()[0]
        self.assertIn(idle_dispatch.CLAIM_HOLDING, f.values())
        self.assertIn("worktree:helm:x", f["held"])   # the real resource attached
        self.assertFalse(f["stranded"])

    def test_an_unrecognised_claim_state_falls_back_to_the_safe_text(self):
        """No future state may inherit the reassign recommendation by default.
        The fallback is the UNKNOWN text, not the STRANDED one."""
        text = idle_dispatch._alert_text(
            {"sender": "opus-integrator", "id8": "e5e5e5e5", "lane": "review",
             "recipient": "ds4pro", "presence": "quiet", "age_min": 60,
             "claim": "some-state-invented-later"})
        self.assertIn("UNKNOWN", text)
        self.assertNotIn("Re-check or reassign", text)

    def test_context_pressure_rides_a_holding_alert(self):
        """What the near-miss case ACTUALLY was: out of window, pane alive. The
        gauge is already computed by autocompact; surfacing it on the alert is
        what turns "why is a working seat quiet?" into an answer."""
        self.rows = [_row("f6f6f6f6ffff", "codex-3")]
        self.presence = {"codex-3": "quiet"}
        self.claims = {"worktree:helm:lr": {"holder": "codex-3"}}
        with mock.patch("helm.autocompact.read",
                        return_value={"pct": 100.1, "window": 320000}):
            idle_dispatch.check()
        text = self.dms[0][1]
        self.assertIn("100.1%", text)
        self.assertIn("320k window", text)

    def test_a_broken_context_gauge_never_suppresses_the_alert(self):
        """A context read is one more FACT on an alert that already fired; it
        must never be able to swallow one. The stranded-work signal outranks
        every convenience riding on it."""
        self.rows = [_row("a7a7a7a7aaaa", "codex-3")]
        self.presence = {"codex-3": "quiet"}
        self.claims = {"worktree:helm:lr": {"holder": "codex-3"}}
        with mock.patch("helm.autocompact.read", side_effect=RuntimeError("x")):
            res = idle_dispatch.check()
        self.assertTrue(res["findings"])
        self.assertIn("QUIET BUT HOLDING", self.dms[0][1])

    def test_latched_once_per_episode_then_rearms(self):
        self.rows = [_row("aaaaaaaa1111", "ds4pro")]
        self.presence = {"ds4pro": "absent"}
        idle_dispatch.check()
        self.dms.clear()
        idle_dispatch.check()                              # within LATCH_TTL_S
        self.assertEqual(self.dms, [])                     # latched, no second DM
        # the dispatch resolves (gone from open_rows) -> latch re-arms
        self.rows = []
        idle_dispatch.check()
        self.rows = [_row("aaaaaaaa1111", "ds4pro")]       # re-strands
        idle_dispatch.check()
        self.assertEqual(len(self.dms), 1)                 # alerts again after re-arm

    def test_dry_run_is_non_mutating(self):
        self.rows = [_row("aaaaaaaa1111", "ds4pro")]
        self.presence = {"ds4pro": "absent"}
        idle_dispatch.check(post=False)                    # dry-run: no DM, no latch write
        self.assertEqual(self.dms, [])
        res = idle_dispatch.check(post=True)               # a real run still alerts
        self.assertEqual(len(self.dms), 1)
        self.assertEqual(len(res["alerted"]), 1)


if __name__ == "__main__":
    unittest.main()


class OneInstantPerScanTest(unittest.TestCase):
    """THE SCAN MUST NOT CONTRADICT ITSELF ABOUT ONE ROW.

    scan() asked the clock THREE times per row: once for the freshness
    filter, once for the reported age_min, once for the overdue flag. So a
    row the filter admitted at one age was reported at another, and the
    coordinator read an age that no decision had actually used.

    THIS CLASS DELIBERATELY DOES NOT USE THE SUITE'S USUAL SCAFFOLDING. The
    other tests mock `dispatches._age_s` and `_is_overdue` with lambdas that
    IGNORE their `now` argument, which makes the number of clock reads
    unobservable — 23 tests passed with the defect live. Threading an instant
    can only be tested against the REAL age functions with a MOVING clock, so
    this drives both for real and advances time.time() on every call.

    Found by a subagent while curing the same class in cmd_dispatch, and
    verified here independently before the fix."""

    def _scan_with_moving_clock(self, deadline_s):
        row = {"id": "d" * 32, "recipient": "codex-9", "source": "sess-x",
               "sender": "opus-integrator", "lane": "review",
               "ts": "1970-01-01T00:00:00Z", "deadline_s": deadline_s}
        # 1000 = the instant a correct scan binds. Every LATER read is 1120+,
        # which is a different minute AND on the other side of a 1100s
        # deadline, so a re-read is visible in BOTH reported fields.
        ticks = iter([1000.0] + [1120.0 + i for i in range(40)])
        with mock.patch.multiple(
                "helm.idle_dispatch.seats",
                _live_claims=mock.Mock(return_value={}),
                presence_of=mock.Mock(side_effect=lambda ls: ls),
                last_seen=mock.Mock(return_value="quiet"),
                derive_seat=mock.Mock(return_value="opus-integrator"),
                dm=mock.Mock()):
            with mock.patch.object(idle_dispatch.dispatches, "open_rows",
                                   return_value=[row]):
                with mock.patch.object(idle_dispatch.time, "time",
                                       side_effect=lambda: next(ticks)):
                    return idle_dispatch.scan()

    def test_the_reported_age_is_the_instant_the_filter_used(self):
        found = self._scan_with_moving_clock(deadline_s=1100)
        # POSITIVE CONTROL: the row must SURVIVE the filter, or an empty
        # result would "pass" this test by never reaching the reporting path.
        self.assertEqual(len(found), 1,
                         "the row was filtered out, so the reported fields "
                         "below were never computed and prove nothing")
        self.assertEqual(found[0]["age_min"], 16,
                         "reported age_min came from a LATER clock read than "
                         "the filter used (1120s -> 18m instead of 1000s -> "
                         "16m): the scan contradicts itself about one row")

    def test_the_overdue_flag_is_the_instant_the_filter_used(self):
        found = self._scan_with_moving_clock(deadline_s=1100)
        self.assertEqual(len(found), 1, "row filtered out; nothing measured")
        self.assertFalse(found[0]["overdue"],
                         "overdue was decided against a LATER clock read: at "
                         "the scan's own instant (1000s) the row is INSIDE "
                         "its 1100s deadline, so this marks a live row late")


class ALivePaneIsNotStrandedTest(unittest.TestCase):
    """ABSENT AND GONE ARE DIFFERENT FACTS, AND THIS RUNG COULD NOT TELL THEM
    APART.

    `presence` measures TIME SINCE A TOOL BOUNDARY, so a live seat sitting
    between turns reads `absent` for exactly the same reason a dead one does.
    Composed with "holds no claim" it lands on STRANDED, whose text says the
    recipient may be "parked WITHOUT reporting" and recommends "re-check or
    reassign" — the destructive action, on a seat that still holds the work.

    Measured live, twice within one hour on the SAME seat: it was
    reported STRANDED while seat_liveness said IDLE on pane-tail evidence.
    Two agents independently repeated the claim before either checked."""

    def _finding(self, **over):
        f = {"sender": "me", "id8": "abc12345", "lane": "review",
             "recipient": "codex-9", "presence": "absent", "age_min": 37,
             "overdue": False, "claim": idle_dispatch.CLAIM_NONE,
             "held": [], "context": "", "wall": "", "live_pane": ""}
        f.update(over)
        return f

    def _pane(self, state, evidence="pane-tail"):
        from helm import seat          # local, matching this file's idiom
        with mock.patch.object(seat, "seat_liveness",
                               return_value={"state": state, "blocked_on": None,
                                             "evidence": evidence}):
            return idle_dispatch._live_pane("codex-9")

    def test_a_LIVE_pane_contradicts_the_reassign_recommendation(self):
        fact = self._pane("IDLE")
        self.assertIn("LIVE", fact)
        self.assertIn("WAKE", fact)
        self.assertIn("pane-tail", fact)          # the EVIDENCE travels
        text = idle_dispatch._alert_text(self._finding(live_pane=fact))
        self.assertIn("DO NOT reassign", text,
                      "the alert still recommends reassigning a seat it has "
                      "just described as live — a self-negating alert leaves "
                      "the reader to pick, and the destructive one gets picked")
        self.assertNotIn("Re-check or reassign", text)

    def test_a_GONE_seat_still_reads_STRANDED(self):  # noqa: VACUOUS_ASSERTION — MUTATION-PROVEN, so not vacuous: flipping
        # _live_pane from an allowlist to a blocklist makes this arm FAIL.
        # The unconditional positive control on the line below is real; the
        # rung cannot link it to the loop because it tracks BINDING
        # identity and the loop rebinds `fact` per state.
        """THE CONTROL THAT MATTERS MOST. The arm above proves the fact CAN
        appear; without this one, a _live_pane that returned the fact for
        EVERY state would pass it while muting every genuine stranded row —
        the failure direction that loses work instead of protecting it."""
        # UNCONDITIONAL POSITIVE CONTROL on the SAME helper: it must speak
        # for a live state, or every empty result below would only prove the
        # helper is dead.
        fact = self._pane("IDLE")
        self.assertTrue(fact,
                        "_pane produced nothing for a LIVE state, so every "
                        "empty result below would measure a dead helper "
                        "rather than a correctly-silent one")
        for state in ("GONE", "UNKNOWN", "EXITED_PANE_ALIVE", "SOME_FUTURE_STATE"):
            with self.subTest(state=state):
                fact = self._pane(state)
                self.assertEqual(fact, "",
                                 "%s produced a live-pane fact, which would "
                                 "mute a real stranded row" % state)
        text = idle_dispatch._alert_text(self._finding(live_pane=self._pane("GONE")))
        self.assertIn("Re-check or reassign", text)
        self.assertNotIn("DO NOT reassign", text)

    def test_an_unreadable_probe_cannot_corrupt_the_alert(self):  # noqa: VACUOUS_ASSERTION — the positive control is the line above the
        # patch: the SAME helper speaks on a readable pane, so silence
        # under OSError measures the probe failing and not a dead call.
        from helm import seat
        # POSITIVE CONTROL through the SAME local the absence uses, so
        # silence below means "the probe failed", never "this call never
        # spoke at all".
        fact = self._pane("IDLE")
        self.assertTrue(fact, "_live_pane is silent even on a READABLE pane")
        with mock.patch.object(seat, "seat_liveness",
                               side_effect=OSError("no pane")):
            fact = idle_dispatch._live_pane("codex-9")
        self.assertEqual(fact, "", "an unreadable probe produced a fact")

    def test_the_provider_wall_contradiction_is_cured_too(self):
        """The self-negating tail predates the pane fact — the wall fact hit
        it first. Curing it for one and not the other would leave a walled
        seat still told to reassign."""
        text = idle_dispatch._alert_text(
            self._finding(wall=" Provider: WALLED — not parked."))
        self.assertIn("DO NOT reassign", text)
        self.assertNotIn("Re-check or reassign", text)
