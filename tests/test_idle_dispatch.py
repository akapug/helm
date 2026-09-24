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

from helm import idle_dispatch, proxywatch  # noqa: E402


def _row(rid, recipient, source="sess-oi", sender=None, deadline_s=2700):
    return {"id": rid, "recipient": recipient, "source": source,
            "sender": sender, "lane": "review", "deadline_s": deadline_s}


class ProviderWallOnAStrandedRowTest(unittest.TestCase):
    """A SEAT ITS PROVIDER IS REFUSING LOOKS EXACTLY LIKE AN ABANDONED ONE.

    No recent activity, no live claim — the STRANDED shape precisely. The alert
    then tells the sender their recipient "may be idle-done or parked WITHOUT
    reporting" and to "re-check or reassign", and every word is wrong about a
    walled seat: it is not parked, it did not fail to report, and reassigning
    punishes it for its provider's 429. Measured 2026-08-04: three codex seats
    sat RATE-LIMITED for five hours while every surface stayed silent.

    The classifier already existed in seat.py; idle_dispatch never asked it."""

    def wall(self, liveness):
        from helm import seat
        with mock.patch.object(seat, "seat_liveness", return_value=liveness):
            return idle_dispatch._provider_wall("codex")

    def test_a_quota_wall_is_NAMED_and_reassignment_is_contradicted(self):
        got = self.wall({"state": "BLOCKED_ON_QUOTA",
                         "blocked_on": "503 auth_unavailable",
                         "remediation": {"restart": "UNKNOWN", "target": None,
                                         "evidence": "origin unmeasured",
                                         "action": None}})
        self.assertIn("BLOCKED_ON_QUOTA", got)
        self.assertIn("503 auth_unavailable", got)
        self.assertIn("Availability:", got)
        self.assertIn("cannot derive whether a restart", got)
        self.assertNotIn("does not repair", got)

    def test_a_cached_proxywatch_wall_is_named_without_reassignment_blame(self):
        got = self.wall({"state": "WALLED",
                         "blocked_on": "upstream RATE-LIMITED since T",
                         "remediation": {"restart": "UNKNOWN", "target": None,
                                         "evidence": "restart effect unmeasured",
                                         "action": None}})
        self.assertIn("WALLED", got)
        self.assertIn("RATE-LIMITED", got)
        self.assertIn("Availability:", got)
        self.assertIn("cannot derive whether a restart", got)
        self.assertNotIn("restart does not repair", got)
        self.assertIn("cannot tell whether reassignment", got)

    def test_a_proxy_cooldown_says_restart_can_help_on_the_same_path(self):
        got = self.wall({"state": "WALLED",
                         "blocked_on": "upstream %s since T" %
                                       proxywatch._PROXY_COOLDOWN,
                         "remediation": {"restart": "HELPFUL",
                                         "target": "proxy",
                                         "evidence": "stale local cooldown",
                                         "action": "PRESCRIBES: restart this exact proxy, then rerun helm proxywatch."}})
        self.assertIn("PRESCRIBES: restart this exact proxy, then rerun helm proxywatch", got)
        self.assertNotIn("restart does not repair", got)
        self.assertIn("cannot tell whether reassignment", got)

    def test_a_wall_without_remediation_evidence_says_it_cannot_tell(self):
        got = self.wall({"state": "WALLED", "blocked_on": "unknown wall"})
        self.assertIn("cannot derive whether a restart would help", got)
        self.assertNotIn("restart/reprobe can help", got)
        self.assertNotIn("restart does not repair", got)

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


# The seat this fixture's roster answers with when the module asks who
# the integrator is. A NAME THIS FLEET DOES NOT USE, on purpose: an arm
# that passes because the fixture name happens to match a live seat is
# measuring the box it runs on.
FIXTURE_INTEGRATOR = "seat-c-integrator"


class IdleDispatchBase(unittest.TestCase):
    """The idle-dispatch world: a fresh HELM_HOME per test and the dispatch,
    seat and integrator reads patched onto plain attributes (`rows`,
    `presence`, `claims`, `age`, `beacons`, `dm_result`), with every DM
    captured in `dms`. The default world has no claims, no beacon, an
    integrator, and recipients that are old enough and quiet.

    THIS CLASS HOLDS NO `test_*` METHOD, and that is its contract. unittest
    collects every inherited `test_*` again under each subclass's own id, so
    an arm written here runs once per subclass. A class that needs this
    fixture subclasses THIS class; its arms go in the subclass."""

    def setUp(self):
        # a fresh HELM_HOME per test so the fcntl latch state never leaks
        self.tmp = tempfile.mkdtemp(prefix="helm-test-idle-")
        # runs even if setUp fails partway; the un-cleaned version leaked one
        # dir per test into the inode exhaustion of 2026-07-24
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
        # seat -> ([pids], trouble). DEFAULT IS NO BEACON, so every pre-existing
        # test keeps its exact one-DM expectation and the wake leg has to be
        # asked for explicitly. This entry is also what keeps the file's
        # hermeticity claim TRUE: _wake_route calls seats.beacon_procs, which
        # reads real /proc, so without this patch a seat that happened to share
        # a name with a live fleet seat would decide the result.
        self.beacons = {}
        self.dm_result = None   # None => the real success shape (row, None)
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
            # RETURNS seats.dm's REAL SUCCESS SHAPE, `(row, None)`. The mock
            # used to return None — the append's return — so every arm here
            # validated a contract production never emits, and a delivery
            # checker that accepted anything non-tuple passed them all.
            # `dm_result` is the REFUSAL SEAM. Default None keeps the real
            # success shape `(row, None)`; a test sets it to a refusal, a
            # falsey-but-not-None error, or a contract violation, and the
            # delivery checker has to answer for it. Without this seam every
            # arm in this file could only ever measure the happy path.
            dm=mock.Mock(side_effect=lambda to, text, **k: (
                self.dms.append((to, text)),
                ({"id": "row"}, None) if self.dm_result is None
                else (self.dm_result(to) if callable(self.dm_result)
                      else self.dm_result))[1]),
            beacon_procs=mock.Mock(
                side_effect=lambda seat, strict=False: self.beacons.get(
                    seat, ([], ""))),
        )
        # THE INTEGRATOR IS A ROLE AND THE MODULE ASKS FOR IT, so the fixture
        # has to answer. Seeded here rather than per arm because the ordinary
        # world has one, and the arm about NOT having one undoes this seed
        # explicitly — a fixture that makes a state impossible cannot be the
        # place an arm about that state lives.
        self.integrator = mock.patch.object(
            idle_dispatch.seats_integrator, "integrator_seat",
            return_value=(FIXTURE_INTEGRATOR, None))
        self.p.start(); self.s.start(); self.integrator.start()

    def tearDown(self):
        self.integrator.stop()
        self.p.stop(); self.s.stop()
        if self._prior is not None:
            os.environ["HELM_HOME"] = self._prior


class IdleDispatchTest(IdleDispatchBase):
    """The idle-dispatch arms, on IdleDispatchBase's world.

    A subclass of THIS class runs every arm below again under its own id,
    which is right only for one that changes the fixture those arms read. A
    class that only needs the fixture subclasses IdleDispatchBase."""

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
        live estate 2026-07-30: one row stranded ~37h had generated ~148
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
        # RECIPIENT == SENDER == THE INTEGRATOR -> no party left who is
        # NEITHER, so there is nobody to wake. Named through the fixture's
        # integrator rather than by a literal: the seat that stands here has
        # to be whoever the module RESOLVES, or this arm is testing the name
        # and not the rule.
        #
        # THE ORDINARY ROW IS THE UNCONDITIONAL CONTROL, in the SAME scan: the
        # rung alerts on it, so the emptiness of the self-addressed row's
        # entry is a decision about THAT row and not a sweep gone quiet.
        self.rows = [_row("cccccccc3333", FIXTURE_INTEGRATOR,
                          sender=FIXTURE_INTEGRATOR),
                     _row("dddddddd4444", "seat-b")]
        self.presence = {FIXTURE_INTEGRATOR: "absent", "seat-b": "absent"}
        res = idle_dispatch.check()
        # `alerted` carries FINDING DICTS, not id8 strings — read the
        # field rather than comparing the list to names.
        self.assertEqual([f["id8"] for f in res["alerted"]],
                         ["dddddddd"])

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
        """THE NEAR-MISS, 2026-08-03. The alert DMd a coordinator that codex-3
        "HOLDS NO CLAIM" while codex-3 held worktree:helm:lr-close-delivered-
        report with 8917s left and 932 uncommitted insertions in its room —
        including a test file that existed nowhere else. It was context-wedged
        (100.1% of window, pane still LIVE), which looks exactly like quiet.
        The recommended action was REASSIGNMENT, which would have destroyed all
        of it.

        The old check tested ONE key, `dispatch:<id8>`, then generalised the
        miss to a sentence about the seat. On the live estate that day: 21 live
        claims, every one a `worktree:…` key, ZERO `dispatch:…` keys — so the
        clause was false for essentially every seat actually working."""
        self.rows = [_row("a1a1a1a1aaaa", "codex-3")]
        self.presence = {"codex-3": "quiet"}
        self.claims = {"worktree:helm:lr-close-delivered-report":
                       {"holder": "codex-3", "session": "s3"}}
        res = idle_dispatch.check()
        f = res["findings"][0]
        self.assertEqual(f["claim"], idle_dispatch.CLAIM_HOLDING)
        self.assertFalse(f["stranded"])
        self.assertIn("worktree:helm:lr-close-delivered-report", f["held"])
        to, text = self.dms[0]
        self.assertIn("QUIET BUT HOLDING", text)
        self.assertIn("RESCUE, do not reassign", text)
        self.assertIn("worktree:helm:lr-close-delivered-report", text)
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
        """What the codex-3 case ACTUALLY was: out of window, pane alive. The
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

    Measured 2026-08-05, twice within one hour on the SAME seat: codex-2 was
    reported STRANDED while seat_liveness said IDLE on pane-tail evidence.
    Two agents independently repeated the claim before either checked."""

    def _finding(self, **over):
        f = {"sender": "me", "id8": "abc12345", "lane": "review",
             "recipient": "codex-9", "presence": "absent", "age_min": 37,
             "overdue": False, "claim": idle_dispatch.CLAIM_NONE,
             "held": [], "context": "", "wall": "", "live_pane": "",
             # EXPLICIT, THOUGH IT IS ALSO WHAT AN OMITTED KEY WOULD YIELD.
             # A fixture that names no wake route IS an unmeasured one, so
             # UNKNOWN is the faithful default rather than a convenient one.
             # Spelling it out is the point: without this line every arm here
             # exercises the UNKNOWN branch by silent omission, and an arm
             # whose branch is decided by a key nobody wrote is one nobody
             # re-reads. An arm that means ARMED or NONE now has to say so.
             "wake_route": idle_dispatch.WAKE_UNKNOWN}
        f.update(over)
        return f

    def _pane(self, state, evidence="pane-tail"):
        from helm import seat          # local, matching this file's idiom
        with mock.patch.object(seat, "seat_liveness",
                               return_value={"state": state, "blocked_on": None,
                                             "evidence": evidence}):
            return idle_dispatch._live_pane("codex-9")

    def test_a_LIVE_recipient_is_called_WORKING_and_never_STRANDED(self):
        """THE WORD IS THE PART A READER ACTS ON.

        Appending a contradicting fact to an alert headed STRANDED leaves the
        reader to choose between them, and the prescribed disposition for
        STRANDED is the destructive one. Both halves of that state are the
        NORMAL CONDITION of a working reviewer: presence goes quiet 120s after
        a tool boundary while a whole-suite gate on this fleet runs ~835s, and
        reviewing, rebasing, retipping and gating take no worktree lease. Their
        conjunction is therefore the steady state of the role rather than a
        signal about it, and a MEASURED live pane is a positive reading that
        outranks an inference drawn from two absences."""
        text = idle_dispatch._alert_text(
            self._finding(live_pane=self._pane("RUNNING")))
        self.assertIn("NOT stranded", text)
        self.assertNotIn("is STRANDED", text,
                         "the headline still says STRANDED about a recipient "
                         "the same alert calls live: %r" % (text,))
        self.assertIn("WAKE", text)
        # the tail already carries the disposition; the headline must not
        # re-state it, so this asserts the SHARED wording, not new prose
        self.assertIn("DO NOT reassign", text)

    def test_a_live_pane_with_NO_BEACON_is_not_sent_at_the_wake_route(self):
        """THE REMEDY TRAVELS THE BEACON, NOT THE PANE, AND THIS FINDING KNOWS
        BOTH. Four alerts told a sender to wake a recipient with an @mention
        while the same rung's console line read `no wake: beacon none`. The
        sender sent it, nobody woke, and the same sentence forbade the only
        other move. One finding, two surfaces, opposite instructions."""
        f = self._finding(live_pane=self._pane("IDLE"),
                          wake_route=idle_dispatch.WAKE_NONE)
        text = idle_dispatch._alert_text(f)
        self.assertIn("NO BEACON IS LISTENING", text,
                      "the alert names a wake route its own finding measured "
                      "as dead: %r" % (text,))
        self.assertNotIn("use its named WAKE route", text)
        # the judgement that was RIGHT is preserved: a live pane still means
        # do not take the work off a seat that holds it
        self.assertIn("DO NOT reassign", text)
        self.assertNotIn("Re-check or reassign", text)

    def test_an_UNMEASURED_wake_path_is_not_reported_as_a_dead_one(self):
        """`WAKE_UNKNOWN` IS NOT `WAKE_NONE` AND THE CONSTANT SAYS SO — "the
        probe could not tell — never none". A tail keyed on `!= WAKE_ARMED`
        would tell the sender a beacon is dead on evidence that only says it
        was never measured, which is the unmeasured-as-negative error one
        refusal further along."""
        f = self._finding(live_pane=self._pane("IDLE"),
                          wake_route=idle_dispatch.WAKE_UNKNOWN)
        text = idle_dispatch._alert_text(f)
        self.assertIn("could NOT be measured", text)
        self.assertNotIn("NO BEACON IS LISTENING", text,
                         "an unmeasured wake path is being reported as a "
                         "measured-dead one: %r" % (text,))
        self.assertIn("DO NOT reassign", text)

    def test_an_ARMED_beacon_still_names_the_wake_route(self):
        """THE MUST-HIT CONTROL. The two arms above assert what the tail stops
        saying; this one proves it still SAYS it when the beacon really is
        listening, so a tail that never names the wake route at all cannot
        pass all three."""
        f = self._finding(live_pane=self._pane("IDLE"),
                          wake_route=idle_dispatch.WAKE_ARMED)
        text = idle_dispatch._alert_text(f)
        self.assertIn("use its named WAKE route", text)
        self.assertNotIn("NO BEACON IS LISTENING", text)
        self.assertNotIn("could NOT be measured", text)

    def test_the_alert_STILL_FIRES_for_a_live_recipient(self):
        """NOTHING IS SUPPRESSED, and that is deliberate. The sender's
        obligation really is idle and they still need to know; only the word
        and the disposition change. An alert that vanished would trade a false
        STRANDED for a silent stall, which is the worse of the two."""
        text = idle_dispatch._alert_text(
            self._finding(live_pane=self._pane("LIVE")))
        self.assertIn("IDLE-DISPATCH", text)
        self.assertIn("abc12345", text)
        self.assertIn("37min", text)

    def test_WITHOUT_a_pane_reading_it_is_still_STRANDED(self):
        """THE POLE, without which the demotion swallows every genuine case.
        No measured pane is not a measurement of life — a seat whose liveness
        cannot be read is exactly the one this rung exists to surface."""
        text = idle_dispatch._alert_text(self._finding(live_pane=""))
        self.assertIn("is STRANDED", text)
        self.assertIn("re-check or reassign", text.lower())

    def test_the_OPERATOR_RENDER_uses_the_same_word_as_the_DM(self):
        """TWO SURFACES ANSWERING ONE QUESTION DIFFERENTLY IS HOW A READER
        LEARNS TO TRUST NEITHER. The console line and the DM are both read by
        the same person about the same row."""
        import io as _io
        from contextlib import redirect_stdout
        live = self._finding(live_pane=self._pane("IDLE"))
        dead = self._finding(live_pane="")
        for f, expect, forbid in ((live, "WORKING", "STRANDED"),
                                  (dead, "STRANDED", "WORKING")):
            buf = _io.StringIO()
            with mock.patch.object(idle_dispatch, "check",
                                   return_value={"findings": [f]}), \
                    redirect_stdout(buf):
                idle_dispatch.cmd_idle_dispatch(["--dry-run"])
            out = buf.getvalue()
            self.assertIn(expect, out, "console said %r for %s"
                          % (out.strip(), expect))
            self.assertNotIn(forbid, out)

    def test_the_CLAIM_STATE_itself_is_untouched_by_the_demotion(self):
        """A WORD CHANGE, NOT A STATE CHANGE. The claim axis answers "does
        this seat hold a lease"; liveness answers "is this seat alive". Mixing
        them is what produced the false STRANDED, so the cure must not mix
        them back the other way — a live recipient still holds no claim, and
        anything reading the claim field must still see that."""
        f = self._finding(live_pane=self._pane("RUNNING"))
        idle_dispatch._alert_text(f)
        self.assertEqual(f["claim"], idle_dispatch.CLAIM_NONE,
                         "the demotion mutated the claim state")

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
            self._finding(wall=" Availability: WALLED — remediation UNKNOWN."))
        self.assertIn("cannot tell whether reassignment", text)
        self.assertIn("work ownership", text)
        self.assertNotIn("DO NOT reassign", text)
        self.assertNotIn("Re-check or reassign", text)


class WakeTheOwingSeatTest(IdleDispatchBase):
    """THE SECOND LEG: the rung knows exactly which seat owes the row and, for
    its first life, told everyone except that seat.

    Detection without actuation: the sender's inbox filled with STRANDED
    notices while the owing seats stayed quiet, and recovery required someone
    to address the owing seat by hand.

    These arms exist because the FIRST attempt at this leg earned eleven
    findings, six of which were one fact from six angles: a second addressee
    was bolted onto filters and latches every one of which was written for a
    single addressee. So each arm below pins one of those seams rather than
    the happy path.
    """

    def _stranded(self, seat="ds4pro", rid="aaaaaaaa1111", armed=True,
                  display=None):
        """`display` is the spelling that lands on the ROW; the beacon is keyed
        by the canonical form, because that is what the wake must route by."""
        shown = display or seat
        self.rows = [_row(rid, shown)]
        self.presence = {shown: "absent"}
        if armed:
            self.beacons = {idle_dispatch._recipient_key(shown): ([4242], "")}
        return seat

    def _wake_dms(self, seat):
        key = idle_dispatch._recipient_key(seat)
        return [(to, t) for to, t in self.dms if to == key and "WAKE" in t]

    def test_the_owing_seat_is_woken_and_the_sender_is_still_told(self):
        """BOTH legs fire on one finding. The sender alert is not replaced by
        the wake — the coordinator still needs to know their obligation is
        idle, which is this rung's original purpose."""
        seat = self._stranded()
        res = idle_dispatch.check()
        self.assertEqual(len(res["alerted"]), 1, "the sender leg stopped firing")
        self.assertEqual(len(res["woke"]), 1, "the owing seat was never woken")
        self.assertEqual(len(self.dms), 2, "expected exactly two addressees")
        senders = [to for to, _ in self.dms if to == "opus-integrator"]
        self.assertEqual(len(senders), 1)
        woke = self._wake_dms(seat)
        self.assertEqual(len(woke), 1)
        text = woke[0][1]
        self.assertIn("aaaaaaaa", text)              # names the row
        self.assertIn("NOT yours", text)             # the second honest exit
        self.assertIn("identity-binding defect", text)   # the empty-list trap

    def test_the_wake_routes_by_KEY_and_never_by_the_display_spelling(self):
        """THE SEAM THAT COST THE FIRST ATTEMPT THE MOST. Both spellings sit on
        the same finding dict and read identically at a call site; the display
        form is copied unvalidated off the row and routes nowhere.

        THE SPELLINGS MUST ACTUALLY DIFFER OR THE ARM IS BLIND. Measured: a
        row carrying "@DS4Pro" canonicalises to "ds4pro", so routing by the
        display form addresses a token no seat answers to. An earlier version
        of this arm used a seat whose two spellings were identical and a
        route-by-display mutation SURVIVED it — the test named the seam and
        could not see it."""
        seat = self._stranded(display="@DS4Pro")
        shown = self.rows[0]["recipient"]
        key = idle_dispatch._recipient_key(shown)
        # the premise of the whole arm, asserted rather than assumed
        self.assertNotEqual(shown, key, "fixture cannot see a display/key mixup")
        idle_dispatch.check()
        f = idle_dispatch.scan()[0]
        # UNCONDITIONAL POSITIVE CONTROL on the same observable: both fields
        # must actually CARRY something, or "" == "" satisfies the equality
        # below while the wake routes nowhere at all.
        self.assertTrue(f["recipient_key"], "no routing key was resolved")
        self.assertTrue(f["wake_target"], "the wake carried no target")
        self.assertEqual(f["wake_target"], key)
        woke = [(to, t) for to, t in self.dms if "WAKE" in t]
        self.assertEqual(len(woke), 1, "no wake was sent to measure")
        self.assertEqual(woke[0][0], key, "the wake routed by the display "
                                          "spelling, which addresses nobody")
        del seat

    def test_a_LATCHED_SENDER_ALERT_DOES_NOT_SWALLOW_THE_WAKE(self):
        """THE NAMED MUTATION. Leg A admits every finding and fires first; leg
        B admits narrowly. Sharing one latch key means the wake is always the
        leg that loses, and it loses SILENTLY for the whole backoff window.

        THE CONSTRUCTION HAS TO MAKE LEG B ELIGIBLE **AFTER** LEG A HAS
        ALREADY BURNED ITS LATCH, or the arm proves nothing: both legs share
        one backoff formula, so at any instant where leg A is latched a
        same-window leg B is latched too, and separate keys look exactly like
        one key. Here the seat has no beacon on the first pass — leg A fires
        alone — and its beacon comes up before the second, which is an
        ordinary thing to happen to a seat that was being restarted. With a
        shared key, leg A's fresh latch swallows that wake entirely."""
        seat = self._stranded(armed=False)
        base = time.time()
        with mock.patch.object(idle_dispatch.time, "time", return_value=base):
            first = idle_dispatch.check()
        # UNCONDITIONAL CONTROL: leg A really did fire and really did burn its
        # latch, so a zero below measures suppression and not a dead fixture.
        self.assertEqual(len(first["alerted"]), 1, "leg A never fired")
        self.assertEqual(len(first["woke"]), 0, "woke a seat with no beacon")
        self.dms = []
        self.beacons = {seat: ([4242], "")}      # the beacon comes up
        with mock.patch.object(idle_dispatch.time, "time", return_value=base):
            second = idle_dispatch.check()
        self.assertEqual(len(second["alerted"]), 0,
                         "the sender leg lost its own backoff")
        self.assertEqual(len(second["woke"]), 1,
                         "the wake did not fire while the SENDER latch was "
                         "held — the legs are sharing latch state")

    def test_the_rearm_sweep_does_not_reap_the_wake_latch_every_pass(self):
        """The sweep drops latches for rows no longer stranded, keyed on what
        the pass SAW. A wake key missing from that set is reaped every single
        pass, silently turning the backoff into no latch at all — so the seat
        would be re-woken on every run of the watchdog."""
        self._stranded()
        first = idle_dispatch.check()
        self.assertEqual(len(first["woke"]), 1)
        second = idle_dispatch.check()          # same instant, still stranded
        self.assertEqual(len(second["woke"]), 0,
                         "the wake latch was reaped and the seat re-woken")
        self.assertTrue(second["findings"][0]["wake_latched"])

    def test_a_QUIET_HOLDER_is_never_woken_though_the_sender_is_told(self):
        """Quiet + holding is a busy or WEDGED owner whose room may hold
        uncommitted work. The sender alert must still fire — the positive
        control that this arm is testing admission and not a dead rung."""
        seat = self._stranded()
        self.claims = {"worktree:helm:some-lane": {"holder": seat}}
        res = idle_dispatch.check()
        self.assertEqual(len(res["alerted"]), 1, "the sender leg went silent")
        self.assertEqual(res["woke"], [], "a holding owner was poked")
        self.assertEqual(self._wake_dms(seat), [])
        # UNCONDITIONAL POSITIVE CONTROL on the same observable: release the
        # claim and the SAME seat, through the SAME _wake_dms read, is woken.
        # Without this the arm passes just as happily against a wake leg that
        # never fires for anyone.
        self.claims = {}
        released = idle_dispatch.check()
        self.assertEqual(len(released["woke"]), 1,
                         "the wake leg is dead, so the silence above measured "
                         "nothing about HOLDING")
        self.assertEqual(len(self._wake_dms(seat)), 1)

    def test_a_CLAIM_UNKNOWN_row_is_never_woken_even_when_the_recheck_is_clean(self):
        """UNKNOWN IS NOT "HOLDS NO CLAIM", and this is the arm that makes the
        `stranded` admission load-bearing rather than decorative.

        For a HOLDING row the revalidation read catches the seat anyway, so
        deleting `stranded` changes nothing there — a mutation proved exactly
        that. UNKNOWN is the case where the two guards genuinely disagree: the
        scan could not read the ledger, the recheck CAN and finds nothing, and
        the only thing standing between an unread fact and a poke is the
        requirement that the row was STRANDED when it was measured."""
        seat = self._stranded()
        seq = [None, {}]        # scan: unreadable -> UNKNOWN. recheck: clean.
        with mock.patch.object(idle_dispatch.seats, "_live_claims",
                               side_effect=lambda: seq.pop(0)):
            res = idle_dispatch.check()
        f = res["findings"][0]
        self.assertEqual(f["claim"], idle_dispatch.CLAIM_UNKNOWN)
        self.assertFalse(f["stranded"])
        self.assertEqual(res["woke"], [], "poked a seat on an unread fact")
        # UNCONDITIONAL POSITIVE CONTROL on the same observable: the identical
        # seat, beacon and row DO produce a wake once the scan can read the
        # ledger, so the silence above measures UNKNOWN and not a dead leg.
        self.dms = []
        clean = idle_dispatch.check()
        self.assertEqual(len(clean["woke"]), 1,
                         "the wake leg is dead, so the refusal above proved "
                         "nothing about UNKNOWN")

    def test_an_UNREADABLE_LEDGER_refuses_the_wake_without_burning_its_latch(self):
        """An unread fact cannot authorize a poke — and a refusal for missing
        evidence must RETRY when the evidence returns rather than be recorded
        as a wake that happened."""
        seat = self._stranded()
        # scan sees a readable empty ledger (STRANDED); the revalidation read
        # immediately before the send cannot be made.
        seq = [{}, None, {}, {}]
        with mock.patch.object(idle_dispatch.seats, "_live_claims",
                               side_effect=lambda: seq.pop(0)):
            first = idle_dispatch.check()
            self.assertEqual(first["woke"], [], "poked on an unread ledger")
            self.assertIn("unreadable", first["findings"][0]["wake_skipped"])
            second = idle_dispatch.check()
        self.assertEqual(len(second["woke"]), 1,
                         "the latch was burned by a refusal, so the wake was "
                         "lost for the whole backoff window")

    def test_a_seat_that_CLAIMS_THE_ROW_between_scan_and_send_is_not_woken(self):
        """The single most likely thing to happen next: scan spends real time
        on presence, liveness and beacon probes, and the seat picks its row up
        during that window. Waking it for work it just claimed trains it to
        skip the channel."""
        seat = self._stranded()
        key = idle_dispatch._recipient_key(seat)
        seq = [{}, {"worktree:helm:x": {"holder": key}}]
        with mock.patch.object(idle_dispatch.seats, "_live_claims",
                               side_effect=lambda: seq.pop(0)):
            res = idle_dispatch.check()
        self.assertEqual(res["woke"], [])
        self.assertIn("claimed work after the scan",
                      res["findings"][0]["wake_skipped"])
        self.assertEqual(len(res["alerted"]), 1, "the sender leg went silent")

    def test_NO_BEACON_is_no_wake_because_a_DM_would_be_queued_bytes(self):
        """wake_alert's own honesty rule, kept at this door: a DM row plus a
        MEASURED armed beacon is queued to an armed route; with no live beacon
        it is queued bytes and claims no success."""
        seat = self._stranded(armed=False)
        res = idle_dispatch.check()
        self.assertEqual(res["woke"], [], "woke a seat with nothing listening")
        self.assertEqual(len(res["alerted"]), 1, "the sender leg went silent")
        f = res["findings"][0]
        self.assertEqual(f["wake_route"], idle_dispatch.WAKE_NONE)
        self.assertIn("beacon none", idle_dispatch._wake_note(f))

    def test_an_UNPROBEABLE_beacon_is_UNKNOWN_and_never_reads_as_no_beacon(self):
        """The same asymmetry the claim half keeps. Collapsing these would make
        an unreadable /proc silently mean 'do not wake' — a mute switch that
        reviews as a correct refusal."""
        seat = self._stranded()
        self.beacons = {seat: ([], "procfs unreadable")}
        res = idle_dispatch.check()
        f = res["findings"][0]
        self.assertEqual(f["wake_route"], idle_dispatch.WAKE_UNKNOWN)
        self.assertEqual(res["woke"], [])
        self.assertIn("beacon unknown", idle_dispatch._wake_note(f))
        # and it is DISTINGUISHABLE from a measured absence at the surface
        self.beacons = {seat: ([], "")}
        f2 = idle_dispatch.scan()[0]
        self.assertNotEqual(idle_dispatch._wake_note(f),
                            idle_dispatch._wake_note(f2))

    def test_a_WALLED_recipient_is_not_woken_for_its_providers_429(self):
        """The alert text already reasons about this seat: not parked, its
        PROVIDER is refusing it. Waking it delivers a row it cannot answer."""
        seat = self._stranded()
        from helm import seat as _seat
        with mock.patch.object(_seat, "seat_liveness",
                               return_value={"state": "BLOCKED_ON_QUOTA",
                                             "blocked_on": "429"}) as live:
            res = idle_dispatch.check()
        live.assert_called_once_with(seat)
        self.assertEqual(res["woke"], [], "woke a seat its provider is walling")
        self.assertEqual(len(res["alerted"]), 1, "the sender leg went silent")
        self.assertIn("provider wall",
                      idle_dispatch._wake_note(res["findings"][0]))

    def test_a_RENDER_FAILURE_retracts_the_latch_so_the_next_pass_retries(self):
        """A latch records "this seat has been told". A template that raised
        told nobody, and a latch left standing then suppresses every retry for
        the whole backoff window — the exact failure the unreadable-ledger path
        is careful to avoid, one branch over."""
        self._stranded()
        with mock.patch.object(idle_dispatch, "_wake_text",
                               side_effect=KeyError("live_pane")):
            first = idle_dispatch.check()
        self.assertEqual(len(first["woke"]), 1, "the leg never reserved a send")
        self.assertFalse(first["woke"][0]["wake_sent"],
                         "a raised render was recorded as delivered")
        # THE ARM: a later pass must try again. Immediately after a failure
        # the RETRY throttle holds, so the clock is advanced past it — the
        # point is that the DELIVERED latch did not swallow the retry, not
        # that retries are unthrottled.
        with mock.patch.object(idle_dispatch.time, "time",
                               return_value=time.time()
                               + idle_dispatch.RETRY_TTL_S + 1):
            second = idle_dispatch.check()
        self.assertEqual(len(second["woke"]), 1,
                         "the latch survived a send that never happened, so "
                         "the wake is lost for the whole backoff window")
        self.assertTrue(second["woke"][0]["wake_sent"])

    def test_a_dm_REFUSAL_is_not_a_delivery(self):
        """`seats.dm` reports a refusal by RETURNING (None, reason) rather than
        raising, so a caller that only catches exceptions records a delivery
        for a message the resolver declined."""
        seat = self._stranded()
        with mock.patch.object(idle_dispatch.seats, "dm",
                               return_value=(None, "not a routable seat")):
            first = idle_dispatch.check()
        self.assertFalse(first["woke"][0]["wake_sent"],
                         "a refused DM was marked sent")
        self.assertIn(idle_dispatch.WAKE_LATCH + first["woke"][0]["id8"],
                      first["undelivered"])
        # ...and the retry happens with the real transport once the RETRY
        # clock expires. It is a THROTTLE, not a suppression: the immediate
        # next pass declines, a later one sends.
        with mock.patch.object(idle_dispatch.time, "time",
                               return_value=time.time()
                               + idle_dispatch.RETRY_TTL_S + 1):
            second = idle_dispatch.check()
        self.assertEqual(len(second["woke"]), 1, "the refusal suppressed retry")
        self.assertTrue(second["woke"][0]["wake_sent"])
        self.assertEqual(len(self._wake_dms(seat)), 1)

    def test_ANOTHER_seat_claiming_the_row_between_scan_and_send_stops_it(self):
        """`scan` excludes a row whose dispatch:<id8> key is claimed BY ANYONE
        — that key means the row is being worked, whoever holds it. A recheck
        carrying only holder membership re-admits it and wakes a seat that no
        longer owes the row."""
        seat = self._stranded()
        f0 = idle_dispatch.scan()[0]
        taken = {"dispatch:" + f0["id8"]: {"holder": "some-other-seat"}}
        seq = [{}, taken]        # scan: clear. recheck: somebody took the row.
        with mock.patch.object(idle_dispatch.seats, "_live_claims",
                               side_effect=lambda: seq.pop(0)):
            res = idle_dispatch.check()
        self.assertEqual(res["woke"], [], "woke a seat that no longer owes it")
        self.assertIn("claimed after the scan",
                      res["findings"][0]["wake_skipped"])
        self.assertEqual(self._wake_dms(seat), [])
        # UNCONDITIONAL POSITIVE CONTROL on the same observable: with the row
        # unclaimed at recheck the SAME seat IS woken, so the silence above
        # measures the exact-key recheck and not a dead leg.
        clean = idle_dispatch.check()
        self.assertEqual(len(clean["woke"]), 1)
        self.assertEqual(len(self._wake_dms(seat)), 1)

    def test_a_REPEATED_refusal_backs_OFF_instead_of_retrying_every_pass(self):
        """THE CLAIM THIS FILE MADE AND DID NOT KEEP. Restoring the prior entry
        on a failure restores its repeat counter too, so a permanently refusing
        transport retried on EVERY pass — the unbounded alert source the
        delivered backoff exists to prevent, reintroduced by the retraction
        that was supposed to be careful. "Told" and "tried" are separate facts
        and now have separate clocks."""
        self._stranded()
        base = time.time()
        refuse = mock.patch.object(idle_dispatch.seats, "dm",
                                   return_value=(None, "still refused"))
        with refuse, mock.patch.object(idle_dispatch.time, "time",
                                       return_value=base):
            first = idle_dispatch.check()
        self.assertEqual(len(first["woke"]), 1, "nothing was attempted")
        # SAME INSTANT: throttled, so the refusing transport is not hammered.
        with refuse, mock.patch.object(idle_dispatch.time, "time",
                                       return_value=base + 1):
            self.assertEqual(idle_dispatch.check()["woke"], [],
                             "a refused send retried on the very next pass")
        # ONE retry window later: it tries again (still refused)...
        with refuse, mock.patch.object(idle_dispatch.time, "time",
                                       return_value=base
                                       + idle_dispatch.RETRY_TTL_S + 1):
            self.assertEqual(len(idle_dispatch.check()["woke"]), 1)
        # ...and the window has DOUBLED, so the same delay no longer suffices.
        with refuse, mock.patch.object(idle_dispatch.time, "time",
                                       return_value=base
                                       + 2 * idle_dispatch.RETRY_TTL_S + 2):
            self.assertEqual(idle_dispatch.check()["woke"], [],
                             "the retry window did not back off")

    def test_QUIET_does_not_consume_the_latch_it_never_delivered(self):
        """`--quiet` is documented as skipping the DM. It reserved both legs
        under the lock and sent nothing, and the next ordinary pass then read
        those reservations as "already told" and sent zero — a diagnostic flag
        silently destroying the alert."""
        seat = self._stranded()
        quiet = idle_dispatch.check(quiet=True)
        self.assertEqual(self.dms, [], "--quiet sent a message")
        # `woke`/`alerted` are the DECISION lists — what WOULD be sent — so
        # they are populated here exactly as under --dry-run. What --quiet must
        # not do is RESERVE: no token is minted, so nothing is told and nothing
        # is throttled, and the proof of that is the next pass below.
        self.assertEqual(len(quiet["woke"]), 1, "--quiet decided nothing")
        self.assertNotIn("wake_token", quiet["woke"][0],
                         "--quiet minted a reservation token")
        self.assertNotIn("token", quiet["alerted"][0],
                         "--quiet minted a reservation token for the alert")
        # THE ARM: the next ORDINARY pass must still alert and still wake.
        after = idle_dispatch.check()
        self.assertEqual(len(after["alerted"]), 1,
                         "--quiet consumed the sender latch")
        self.assertEqual(len(after["woke"]), 1,
                         "--quiet consumed the wake latch")
        self.assertEqual(len(self._wake_dms(seat)), 1)

    def test_a_dm_return_that_is_NOT_the_contract_fails_CLOSED(self):
        """seats.dm has ONE success shape, `(row, None)`. None, a 1-tuple, a
        3-tuple and a bare object are contract VIOLATIONS, not dialects to
        normalise — and treating them as success was invisible because the
        fixture itself returned None, so every arm validated a shape
        production never emits."""
        # A DISTINCT ROW PER SHAPE, never a re-run of setUp. Calling setUp()
        # inside a test starts a SECOND mock.patch.multiple over the real
        # helm.seats while tearDown stops only one, so the patches leak into
        # every later test in the process — measured as 148 failures and 100
        # errors across unrelated suites, all reporting this class's lambdas.
        # Separate dispatch ids give separate latch keys, which is the only
        # isolation these iterations actually needed.
        for i, bad in enumerate((None, ({"id": "r"},),
                                 ({"id": "r"}, None, "extra"), "delivered", 0)):
            with self.subTest(shape=type(bad).__name__):
                self.dms = []
                # UNIQUE IN THE FIRST EIGHT CHARACTERS, because the latch
                # keys are built from id8 — ids differing only after char 8
                # share a latch, and iteration 0's failure then throttles
                # every later shape through the retry clock.
                self._stranded(rid="%08dffff" % i)
                with mock.patch.object(idle_dispatch.seats, "dm",
                                       return_value=bad):
                    res = idle_dispatch.check()
                self.assertFalse(res["woke"][0]["wake_sent"],
                                 "%r was accepted as a delivery" % (bad,))
                self.assertIn(idle_dispatch.WAKE_LATCH + res["woke"][0]["id8"],
                              res["undelivered"])
        # UNCONDITIONAL POSITIVE CONTROL: the REAL contract shape succeeds
        # through the same checker, so the refusals above discriminate.
        self.dms = []
        self._stranded(rid="99999999ffff")
        ok = idle_dispatch.check()
        self.assertTrue(ok["woke"][0]["wake_sent"],
                        "the delivery checker refuses everything")
        self.assertEqual(ok["undelivered"], [])

    def test_DRY_RUN_reports_what_would_alert_and_reserves_NOTHING(self):
        """`--dry-run` has one job: say what WOULD happen without consuming
        anything. Moving to reservations made it easy to gate the DECISION on
        sending rather than only the WRITE, which reports an empty board —
        the exact opposite of the flag's contract, and indistinguishable at
        the surface from a healthy estate."""
        seat = self._stranded()
        dry = idle_dispatch.check(post=False)
        self.assertEqual(len(dry["alerted"]), 1, "--dry-run reported no alert")
        self.assertEqual(len(dry["woke"]), 1, "--dry-run reported no wake")
        self.assertEqual(self.dms, [], "--dry-run sent a message")
        # AND IT CONSUMED NOTHING: the next real pass still alerts and wakes.
        real = idle_dispatch.check()
        self.assertEqual(len(real["alerted"]), 1, "--dry-run ate the alert")
        self.assertEqual(len(real["woke"]), 1, "--dry-run ate the wake")
        self.assertEqual(len(self._wake_dms(seat)), 1)

    def test_the_wake_does_not_DIAGNOSE_the_seat(self):
        """It cannot tell an idle-done seat from one that never saw the row,
        and both happen. A watchdog that guesses the cause invites an argument
        about the guess instead of an action on the row."""
        seat = self._stranded()
        idle_dispatch.check()
        text = self._wake_dms(seat)[0][1]
        self.assertIn("not a diagnosis", text)
        self.assertIn("this scan", text)     # the age is stated as scan-time


class DeliveryTruthTest(IdleDispatchBase):
    """WHAT THE OPERATOR IS TOLD MUST MATCH WHAT THE TRANSPORT DID.

    A reviewer measured this module printing `ALERTED @seat` for sends the
    resolver had REFUSED. `check` knew — it set sent=False and kept the
    reason — and the renderer overrode it. That is the founding defect of this
    whole row surviving in the one place nobody instrumented, so these arms
    attack the SURFACE and the STATE, never the happy path.
    """

    def _seed(self, state):
        from helm import pk
        pk.write_json(idle_dispatch._state_path(), state)

    def _state(self):
        from helm import pk
        return pk.read_json(idle_dispatch._state_path(), {}) or {}

    def _stranded_row(self, rid="aaaaaaaa1111", seat="ds4pro"):
        self.rows = [_row(rid, seat)]
        self.presence = {seat: "absent"}
        self.beacons = {idle_dispatch._recipient_key(seat): ([4242], "")}

    # ---- the exact-return contract ---------------------------------------
    def test_a_falsey_but_not_None_error_is_a_REFUSAL_not_a_delivery(self):
        """`(row, "")`, `(row, False)` and `(row, 0)` were all measured reading
        as ACCEPTED, because the check was `if err` and every one of those is
        falsey. The sealed contract permits exactly one success shape. A
        transport that answers with an empty-string reason is still answering
        with a REASON, and treating it as delivery is how a refusal becomes a
        latch that never retries."""
        f = {"id8": "aaaaaaaa", "sender": "opus-integrator"}
        for err in ("", False, 0, 0.0):
            self.dm_result = ({"id": "row"}, err)
            ok, why = idle_dispatch._deliver(
                "opus-integrator", lambda _f: "text", f, "alert")
            self.assertFalse(ok, "a falsey %r error was read as delivered"
                             % (err,))
            self.assertTrue(why, "the refusal carried no reason")
        # UNCONDITIONAL POSITIVE CONTROL on the same call path: the ONE legal
        # shape still succeeds, so the four refusals above measure the contract
        # and not a helper that refuses everything.
        self.dm_result = ({"id": "row"}, None)
        ok, why = idle_dispatch._deliver(
            "opus-integrator", lambda _f: "text", f, "alert")
        self.assertTrue(ok, "the one legal success shape was refused")
        self.assertEqual(why, "")

    # ---- the surface ------------------------------------------------------
    def test_a_refused_send_says_FAILED_at_the_terminal_and_never_ALERTED(self):
        """THE FOUNDING DEFECT, AT THE LAST PLACE IT SURVIVED. With every DM
        refused, the command printed `ALERTED @opus-integrator` and named no
        failure; only stderr, which nobody reads under a pager, disagreed."""
        import contextlib
        import io
        self._stranded_row()
        self.dm_result = (None, "not a routable seat")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            idle_dispatch.cmd_idle_dispatch([])
        out = buf.getvalue()
        self.assertIn("ALERT FAILED", out)
        self.assertIn("not a routable seat", out)
        self.assertNotIn(" ALERTED ", out)
        self.assertIn("WAKE FAILED", out)
        # POSITIVE CONTROL: the same row, the same renderer, a transport that
        # accepts — proving the assertions above discriminate rather than
        # matching a command that prints nothing at all.
        self.dm_result = None
        self._seed({})
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            idle_dispatch.cmd_idle_dispatch([])
        self.assertIn(" ALERTED ", buf2.getvalue())
        self.assertNotIn("ALERT FAILED", buf2.getvalue())

    # ---- --quiet is as non-mutating as --dry-run --------------------------
    def test_quiet_reaps_NOTHING_and_the_control_proves_the_reap_is_live(self):
        """--quiet reported what WOULD happen and then destroyed live state:
        the re-arm sweep drops every record whose dispatch is no longer in the
        scan, and that sweep ran whenever post=True. So a quiet pass over an
        empty scan erased a TOLD record and the next real pass re-alerted a
        seat it had already told."""
        now = time.time()
        self.rows = []                      # nothing stranded: the reap fires
        self._seed({"alert:deadbeef": {"told": {"at": now, "n": 1}}})
        idle_dispatch.check(post=True, quiet=True)
        self.assertIn("alert:deadbeef", self._state(),
                      "--quiet reaped a live record it only meant to report")
        # UNCONDITIONAL POSITIVE CONTROL: the identical call WITHOUT quiet does
        # reap it. Without this the assertion above would also pass if the reap
        # had simply been deleted.
        idle_dispatch.check(post=True, quiet=False)
        self.assertNotIn("alert:deadbeef", self._state(),
                         "the re-arm reap is dead, so the quiet arm proves nothing")

    # ---- the reservation ---------------------------------------------------
    def test_only_the_token_holder_may_finish_the_reservation(self):
        """Equality of the unpredictable token IS ownership. A late result from
        a superseded pass must be DROPPED, not written, or it advances a
        counter that belongs to whoever holds the record now."""
        now = time.time()
        key = idle_dispatch._record_key(idle_dispatch.LEG_ALERT, "aaaaaaaa")
        self._seed({key: {"inflight": {"token": "AAA", "expires_at": now + 60}}})
        self.assertFalse(idle_dispatch._finish(key, "BBB", True),
                         "a non-holder finished someone else's reservation")
        rec = self._state()[key]
        self.assertEqual(rec["inflight"]["token"], "AAA")
        self.assertNotIn("told", rec, "a stale result advanced the TOLD counter")
        # POSITIVE CONTROL: the true holder DOES finish, so the refusal above
        # measures ownership and not a _finish that never writes.
        self.assertTrue(idle_dispatch._finish(key, "AAA", True))
        rec = self._state()[key]
        self.assertEqual(int(rec["told"]["n"]), 1)
        self.assertNotIn("inflight", rec)

    def test_an_EXPIRED_reservation_lets_a_later_pass_send_again(self):
        """AT-LEAST-ONCE, AND THIS IS THE WINDOW. If the send succeeds and the
        CAS never lands — a crash between the two — the reservation must expire
        so a later pass re-sends. A duplicate wake is strictly safer than a
        wake lost forever, and that is a decision, not an accident."""
        now = time.time()
        expired = {"inflight": {"token": "AAA", "expires_at": now - 1}}
        self.assertEqual(idle_dispatch._blocked(expired, now), "",
                         "an expired reservation still blocked the next pass")
        # POSITIVE CONTROL: an unexpired one DOES block, so the empty string
        # above is the expiry and not a _blocked that never blocks.
        live = {"inflight": {"token": "AAA", "expires_at": now + 60}}
        self.assertTrue(idle_dispatch._blocked(live, now))

    def test_the_three_held_states_are_told_APART_not_all_called_latched(self):
        """`(latched)` covered three different facts: already told and waiting
        out the backoff, a failed send waiting out its retry, and another pass
        holding the reservation right now. An operator cannot act on a word
        that means all three."""
        now = time.time()
        told = idle_dispatch._blocked({"told": {"at": now, "n": 1}}, now)
        retry = idle_dispatch._blocked({"retry": {"next_at": now + 30}}, now)
        infl = idle_dispatch._blocked(
            {"inflight": {"token": "A", "expires_at": now + 30}}, now)
        for label, msg in (("told", told), ("retry", retry), ("inflight", infl)):
            self.assertTrue(msg, "%s produced no reason at all" % label)
        self.assertEqual(len({told, retry, infl}), 3,
                         "two held states produced the SAME text: %r"
                         % ([told, retry, infl],))
        # POSITIVE CONTROL: a record in none of those states is NOT held.
        self.assertEqual(idle_dispatch._blocked({}, now), "")

    def test_the_transport_reason_reaches_the_RETRY_record(self):
        """The reason was computed, printed to stderr, and then dropped:
        `_deliver` returned a bare bool, so `_finish` stored the generic
        "send failed" for every failure mode. Retry policy and the operator
        both then had to guess between a down node, a bad seat name and a
        template that raised."""
        self._stranded_row()
        self.dm_result = (None, "not a routable seat")
        idle_dispatch.check()
        recs = [v for k, v in self._state().items() if k.startswith("alert:")]
        self.assertEqual(len(recs), 1, "expected exactly one alert record")
        self.assertIn("not a routable seat", recs[0]["retry"]["reason"])
        # POSITIVE CONTROL: a delivered send leaves TOLD and no retry at all,
        # so the assertion above is about the reason and not about a module
        # that writes a retry record unconditionally.
        self._seed({})
        self.dm_result = None
        idle_dispatch.check()
        recs = [v for k, v in self._state().items() if k.startswith("alert:")]
        self.assertEqual(len(recs), 1)
        self.assertNotIn("retry", recs[0])
        self.assertEqual(int(recs[0]["told"]["n"]), 1)


class WakeLifecycleTest(IdleDispatchBase):
    """THREE WINDOWS BETWEEN MEASURING AND ACTING, all found by review.

    Every one has the same shape: a fact is established during the SCAN and
    relied on during the SEND, with the whole scan and every sender alert in
    between. The rung's founding rule is that no live beacon must never be
    claimed as a wake — and measuring once is how that rule got broken by the
    code that enforces it.
    """

    def _stranded_row(self, rid="aaaaaaaa1111", seat="ds4pro"):
        self.rows = [_row(rid, seat)]
        self.presence = {seat: "absent"}
        self.beacons = {idle_dispatch._recipient_key(seat): ([4242], "")}

    def _state(self):
        from helm import pk
        return pk.read_json(idle_dispatch._state_path(), {}) or {}

    def _wake_recs(self):
        return [v for k, v in self._state().items() if k.startswith("wake:")]

    def test_a_beacon_that_DIES_between_scan_and_send_is_never_a_wake(self):
        """The route was proved during the scan; the DM goes out after every
        sender alert. A beacon exiting in that window leaves queued bytes
        nobody reads — and wake_sent went True and the terminal printed WOKE."""
        self._stranded_row()
        seen = []

        def flaky(seat):
            seen.append(seat)
            if len(seen) == 1:                       # the SCAN measurement
                return (idle_dispatch.WAKE_ARMED, [4242])
            return (idle_dispatch.WAKE_NONE, [])     # gone by SEND time

        with mock.patch.object(idle_dispatch, "_wake_route", side_effect=flaky):
            res = idle_dispatch.check()
        self.assertGreaterEqual(len(seen), 2,
                                "the beacon was measured ONCE; the send-time "
                                "recheck is the whole cure")
        self.assertEqual([f for f in res["woke"] if f.get("wake_sent")], [],
                         "a wake was claimed for a beacon that had exited")
        recs = self._wake_recs()
        self.assertEqual(len(recs), 1, "expected exactly one wake record")
        self.assertNotIn("told", recs[0], "a declined wake advanced TOLD")
        self.assertNotIn("retry", recs[0],
                         "a wake nobody attempted earned a RETRY backoff — the "
                         "recipient is penalised for their beacon restarting")
        self.assertNotIn("inflight", recs[0], "the reservation was never released")

    def test_a_STABLE_beacon_still_wakes_and_advances_TOLD(self):
        """UNCONDITIONAL POSITIVE CONTROL for the arm above. Without it, every
        assertion there would also pass if the wake leg had simply stopped
        firing."""
        self._stranded_row()
        res = idle_dispatch.check()
        self.assertTrue([f for f in res["woke"] if f.get("wake_sent")],
                        "the wake leg no longer fires at all")
        recs = self._wake_recs()
        self.assertEqual(len(recs), 1)
        self.assertEqual(int((recs[0].get("told") or {}).get("n") or 0), 1)

    def test_a_row_that_CLOSES_after_the_scan_is_told_to_nobody(self):
        """The scan reads OPEN rows once and only the CLAIMS ledger is re-read
        before sending. A verdict or cancel landing in between leaves claims
        empty, so both messages went out calling a TERMINAL dispatch owed."""
        self._stranded_row()
        first = {"n": 0}
        rows_snapshot = list(self.rows)

        def closes_after_scan():
            first["n"] += 1
            return list(rows_snapshot) if first["n"] == 1 else []

        with mock.patch.object(idle_dispatch.dispatches, "open_rows",
                               side_effect=closes_after_scan):
            res = idle_dispatch.check()
        self.assertGreaterEqual(first["n"], 2,
                                "open rows were read ONCE; the send-time "
                                "recheck is the cure")
        self.assertEqual(self.dms, [],
                         "a terminal dispatch was reported as open and owed")
        self.assertTrue(all(f.get("closed_after_scan") for f in res["findings"]),
                        "the finding does not say why nothing was sent")
        for rec in self._wake_recs():
            self.assertNotIn("told", rec)
            self.assertNotIn("inflight", rec, "the reservation leaked")

    def test_a_wake_latch_does_not_survive_the_row_being_PICKED_UP(self):
        """A completed wake's TOLD record was kept alive through states that
        are not stranded. A recipient who took a normal worktree claim carried
        the previous episode's backoff, so a later release that re-stranded the
        row found the wake SUPPRESSED by a latch earned in an episode that had
        already ended."""
        self._stranded_row()
        idle_dispatch.check()
        self.assertEqual(len(self._wake_recs()), 1, "no wake episode to end")
        # the recipient PICKS IT UP: holding a claim is not stranded
        self.claims = {idle_dispatch._recipient_key("ds4pro"): {}}
        idle_dispatch.check()
        self.assertEqual(self._wake_recs(), [],
                         "the wake latch outlived the episode; a later "
                         "re-strand would be suppressed by a dead backoff")
        # UNCONDITIONAL POSITIVE CONTROL: released and re-stranded, the wake
        # fires AGAIN rather than being held by the old record.
        self.claims = {}
        self.dms = []
        res = idle_dispatch.check()
        self.assertTrue([f for f in res["woke"] if f.get("wake_sent")],
                        "a re-stranded row was never woken again")


class SendBoundaryIdentityTest(IdleDispatchBase):
    """The window INSIDE the send loop, which the previous cure did not close.

    Moving a measurement from SCAN to START-OF-SEND-LOOP shrinks the window;
    it does not remove it. The loop itself spends time: reservations are
    written, an open-row batch is read, and EVERY sender alert is delivered
    before the first wake goes out.
    """

    def _stranded_row(self, rid="aaaaaaaa1111", seat="ds4pro"):
        self.rows = [_row(rid, seat)]
        self.presence = {seat: "absent"}
        self.beacons = {idle_dispatch._recipient_key(seat): ([4242], "")}
        return rid, seat

    def _state(self):
        from helm import pk
        return pk.read_json(idle_dispatch._state_path(), {}) or {}

    def test_a_REBIND_after_scan_never_wakes_the_previous_recipient(self):
        """THE CASE NO TIMING FIX REACHES. An OPEN rebind keeps the dispatch id
        and changes the recipient, so an id-only freshness test passes: the row
        is present, nothing is stale, and the wake goes to the seat that USED
        TO owe it — live, correctly addressed, told to pick up work that is no
        longer theirs."""
        rid, seat = self._stranded_row()
        reads = {"n": 0}

        def rebinds_after_scan():
            reads["n"] += 1
            if reads["n"] == 1:
                return [_row(rid, seat)]
            return [_row(rid, "someone-else")]      # SAME id, NEW recipient

        with mock.patch.object(idle_dispatch.dispatches, "open_rows",
                               side_effect=rebinds_after_scan):
            res = idle_dispatch.check()
        self.assertGreaterEqual(reads["n"], 2,
                                "open rows were read once; the per-send read "
                                "is the cure")
        self.assertEqual(self._wake_dms(seat), [],
                         "the PREVIOUS recipient was woken after a rebind")
        self.assertTrue(any(f.get("rebound_to") == "someone-else"
                            for f in res["findings"]),
                        "the finding does not name the rebind")

    def _wake_dms(self, seat):
        key = idle_dispatch._recipient_key(seat)
        return [(to, t) for to, t in self.dms if to == key and "WAKE" in t]

    def test_a_row_closing_AFTER_the_batch_refresh_still_stops_the_send(self):
        """The previous cure snapshotted a SET OF IDS once, before every alert
        and every wake, so a row closing during the loop still sent for every
        element after the first."""
        rid, seat = self._stranded_row()
        reads = {"n": 0}

        def closes_late():
            reads["n"] += 1
            return [_row(rid, seat)] if reads["n"] <= 1 else []

        with mock.patch.object(idle_dispatch.dispatches, "open_rows",
                               side_effect=closes_late):
            idle_dispatch.check()
        self.assertGreaterEqual(reads["n"], 2)
        self.assertEqual(self.dms, [],
                         "a terminal dispatch was still reported as owed")

    def test_a_claim_taken_DURING_the_send_loop_cancels_the_wake(self):
        """held_now/keys_now were read under the scan lock, before the
        reservations and before every sender alert. A recipient who picks the
        row up in that interval was invisible, so the wake went out for work
        already in hand."""
        rid, seat = self._stranded_row()
        calls = {"n": 0}
        key = idle_dispatch._recipient_key(seat)

        real = idle_dispatch.seats._live_claims

        def claimed_after_scan():
            calls["n"] += 1
            if calls["n"] <= 1:
                return {}                       # free during the scan
            return {"dispatch:" + rid[:8]: {}}  # claimed before the wake

        with mock.patch.object(idle_dispatch.seats, "_live_claims",
                               side_effect=claimed_after_scan):
            idle_dispatch.check()
        self.assertGreaterEqual(calls["n"], 2,
                                "claims were read once; the per-wake read is "
                                "the cure")
        self.assertEqual(self._wake_dms(seat), [],
                         "a seat that had already picked the row up was woken")
        # THE LATCH MUST NOT BURN: a wake we declined is not a failure, so the
        # next episode must be free to wake them if they release it.
        for k, v in self._state().items():
            if k.startswith("wake:"):
                self.assertNotIn("told", v)
                self.assertNotIn("retry", v)

    def test_a_STABLE_row_and_claim_still_wakes(self):
        """UNCONDITIONAL POSITIVE CONTROL for all three arms above. Each of
        them asserts an ABSENCE of DMs, and every one would pass if the wake
        leg had simply stopped firing."""
        rid, seat = self._stranded_row()
        res = idle_dispatch.check()
        self.assertTrue([f for f in res["woke"] if f.get("wake_sent")],
                        "the wake leg no longer fires at all")
        self.assertEqual(len(self._wake_dms(seat)), 1)


class CanonicalIdentityAtSendTest(IdleDispatchBase):
    """Identity is CANONICAL; display is text. Comparing the two marks a
    stable row as rebound on every send.

    scan() stores the human-facing spelling in f["recipient"] and the
    canonical form in f["recipient_key"]. My first per-send cure compared the
    DISPLAY field to the row's CANONICAL field, so a row whose display differs
    from its canonical name released BOTH legs without delivering either — and
    the rebind arm could not see it, because its fixture used identical
    spellings on both sides. A fixture that cannot express the difference
    cannot test it.
    """

    def _state(self):
        from helm import pk
        return pk.read_json(idle_dispatch._state_path(), {}) or {}

    def _wake_dms(self, seat):
        key = idle_dispatch._recipient_key(seat)
        return [(to, t) for to, t in self.dms if to == key and "WAKE" in t]

    def test_a_MIXED_DISPLAY_row_is_stable_and_still_delivers(self):
        """recipient "ds4pro" with display "@DS4Pro" is ONE seat. Under the
        display-vs-canonical comparison this row read as rebound on every
        pass, so the rung went silent for every seat whose display spelling
        was not byte-identical to its canonical name."""
        row = _row("aaaaaaaa1111", "ds4pro")
        row["recipient_display"] = "@DS4Pro"
        self.rows = [row]
        self.presence = {"@DS4Pro": "absent", "ds4pro": "absent"}
        self.beacons = {idle_dispatch._recipient_key("ds4pro"): ([4242], "")}
        res = idle_dispatch.check()
        self.assertFalse(any(f.get("rebound_to") for f in res["findings"]),
                         "a stable row was classified as REBOUND because its "
                         "display spelling differs from its canonical name")
        self.assertTrue([f for f in res["woke"] if f.get("wake_sent")],
                        "the wake never fired for a mixed-display row")

    def test_a_REAL_rebind_to_a_different_seat_is_still_caught(self):
        """UNCONDITIONAL POSITIVE CONTROL for the arm above: canonical
        comparison must still catch a genuine change of owner, or the fix
        would be 'never detect a rebind' rather than 'compare correctly'."""
        rid, seat = "aaaaaaaa1111", "ds4pro"
        row = _row(rid, seat)
        row["recipient_display"] = "@DS4Pro"
        self.rows = [row]
        self.presence = {seat: "absent"}
        self.beacons = {idle_dispatch._recipient_key(seat): ([4242], "")}
        reads = {"n": 0}

        def rebinds():
            reads["n"] += 1
            if reads["n"] == 1:
                return [row]
            other = _row(rid, "codex-3")          # SAME id, DIFFERENT seat
            other["recipient_display"] = "@codex-3"
            return [other]

        with mock.patch.object(idle_dispatch.dispatches, "open_rows",
                               side_effect=rebinds):
            res = idle_dispatch.check()
        self.assertTrue(any(f.get("rebound_to") for f in res["findings"]),
                        "a genuine rebind was missed")
        self.assertEqual(self._wake_dms(seat), [],
                         "the previous recipient was woken after a real rebind")

    def test_an_UNREADABLE_claims_ledger_at_the_send_boundary_REFUSES(self):
        """_claims_by_holder's own contract says an unreadable or
        unattributable read must REFUSE the wake: we would be poking a seat
        about work whose ownership we cannot see. My cure only skipped when
        the read SUCCEEDED and found a claim, so an unreadable final read fell
        through to the beacon probe and delivered.

        NOTE THE OPPOSITE POLARITY from the open-row check, which deliberately
        does NOT suppress on an unreadable ledger — absence of proof of
        closure is not proof of closure. Two questions, two directions; I had
        them the same way round once."""
        rid, seat = "aaaaaaaa1111", "ds4pro"
        self.rows = [_row(rid, seat)]
        self.presence = {seat: "absent"}
        self.beacons = {idle_dispatch._recipient_key(seat): ([4242], "")}
        reads = {"n": 0}

        def readable_then_not():
            """READS 1 AND 2 READABLE, READ 3 UNREADABLE.

            A real pass reads claims THREE times: scan(), then
            _claims_by_holder() under the latch lock, then _claims_by_holder()
            beside the wake. My first version failed on read 2, so
            claims_readable was already false at the UNDER-LOCK admission —
            the finding never got a wake reservation and never entered the
            send loop at all. Deleting the branch this arm exists to test left
            it GREEN. The arm stopped at an earlier gate than the one it named.
            """
            reads["n"] += 1
            if reads["n"] <= 2:
                return {}                       # scan AND the under-lock read
            raise OSError("claims ledger unreadable")

        with mock.patch.object(idle_dispatch.seats, "_live_claims",
                               side_effect=readable_then_not):
            res = idle_dispatch.check()
        self.assertGreaterEqual(reads["n"], 3,
                                "claims were read fewer than three times, so "
                                "the send-boundary read never happened and "
                                "this arm tested an earlier gate")
        # THE FINDING MUST HAVE BEEN ADMITTED AND RESERVED before the final
        # refusal — otherwise the refusal under test was never reached.
        self.assertTrue(any(f.get("wake_skipped") ==
                            "claims unreadable at the send boundary"
                            for f in res["findings"]),
                        "no finding carries the send-boundary refusal reason, "
                        "so the branch under test did not run")
        self.assertEqual(self._wake_dms(seat), [],
                         "a wake was sent on an UNREAD ownership fact")
        for k, v in self._state().items():
            if k.startswith("wake:"):
                self.assertNotIn("told", v, "a refused wake advanced TOLD")
                self.assertNotIn("retry", v,
                                 "an unreadable ledger earned the recipient a "
                                 "retry backoff they did not cause")

        # UNCONDITIONAL POSITIVE CONTROL: with the ledger readable throughout,
        # the same row DOES wake — so the refusal above is about readability
        # and not about a wake leg that stopped firing.
        self.dms = []
        idle_dispatch.check()
        self.assertEqual(len(self._wake_dms(seat)), 1,
                         "a readable ledger no longer wakes anyone")


class UndeliveredObligationsTest(unittest.TestCase):
    """task/928 slice B's enumerator: WHICH owed rows were never delivered.

    The owner's P0 is that helm RECORDS obligations and has no delivery seam —
    twelve of fourteen stalled rows were authors handed a revision and never
    told. This is the population a re-delivery sweep acts on.
    """

    def _rows(self, *rows):
        return {r["id"]: r for r in rows}

    def _row(self, rid, delivery="needs-confirmation", recipient="seat-a"):
        return {"id": rid, "delivery": delivery, "recipient": recipient}

    def test_an_UNREADABLE_ledger_is_None_and_never_an_empty_population(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control is in the same method: the SAME call on a READABLE ledger is asserted to return [], so an arm that never reached the function reddens there
        """FAIL CLOSED. `[]` from an unreadable ledger says "nothing is
        undelivered", which is the fail-open this task exists to end — and it
        is worse than a wrong count, because the re-arm sweep reaps every latch
        it did not see this pass, so an empty population would silently erase
        the backoff for every obligation at once.

        LOAD-BEARING MUTATION: return [] instead of None on `unavailable`.
          -> AssertionError: [] is not None
        """
        with mock.patch.object(idle_dispatch, "__name__", idle_dispatch.__name__):
            from helm import dispatches
            with mock.patch.object(dispatches, "snapshot",
                                   return_value=({}, "ledger unreadable")):
                self.assertIsNone(idle_dispatch._undelivered_obligations())
            # MUST-HIT: the SAME call on a readable ledger returns a LIST, so
            # the None above is the unreadable branch and not a broken import.
            with mock.patch.object(dispatches, "snapshot",
                                   return_value=({}, None)), \
                    mock.patch.object(dispatches, "owed", return_value=[]):
                self.assertEqual(idle_dispatch._undelivered_obligations(), [])

    def test_an_OBSERVED_delivery_is_not_undelivered_and_the_rest_are(self):
        """`observed` is earned by a mention the recipient's beacon matched.
        Anything else means the seat was never demonstrably told — including a
        retip that DOWNGRADED an observation earned against the OLD tip.

        LOAD-BEARING MUTATION: drop the `observed` skip -> the observed row
        appears in the population and this arm reddens on the id list.
        """
        from helm import dispatches
        told = self._row("aaa", delivery="observed")
        untold = self._row("bbb", delivery="needs-confirmation")
        never = self._row("ccc", delivery="")
        rows = [told, untold, never]
        with mock.patch.object(dispatches, "snapshot",
                               return_value=(self._rows(*rows), None)), \
                mock.patch.object(dispatches, "owed", return_value=rows):
            got = idle_dispatch._undelivered_obligations()
        # MUST-HIT: the population is non-empty, so the absence of "aaa" below
        # is a decision and not an empty scan.
        self.assertTrue(got, "the enumerator returned nothing to judge")
        self.assertEqual([r["id"] for r in got], ["bbb", "ccc"])

    def test_a_row_with_no_recipient_is_skipped_rather_than_addressed(self):
        """There is nobody to wake. Including it would make the sweep count an
        attempt it can never deliver, and after N such attempts escalate a row
        whose only defect is a missing addressee.
        """
        from helm import dispatches
        rows = [self._row("aaa", recipient=""),
                self._row("bbb", recipient="   "),
                self._row("ccc", recipient="seat-b")]
        with mock.patch.object(dispatches, "snapshot",
                               return_value=(self._rows(*rows), None)), \
                mock.patch.object(dispatches, "owed", return_value=rows):
            got = idle_dispatch._undelivered_obligations()
        self.assertEqual([r["id"] for r in got], ["ccc"])

    def test_the_enumerator_CONSUMES_owed_rather_than_re_deriving_it(self):
        """owed() builds the successor index ONCE and threads
        it, because carrier() per row over a 1,300-row ledger is the shape that
        makes a predicate too expensive to adopt. A second enumerator here
        would drift from it the way _open and _not_closed already did.

        This pins the CALL, which is the only thing that can see the sweep stop
        consuming the shared answer.
        """
        from helm import dispatches
        calls = []
        real_owed = dispatches.owed

        def spy(snap, index=None):
            calls.append(True)
            # returns the LIVE answer, so the assertion below is about what the
            # enumerator did with a real population rather than with a stub
            return list(snap.values())

        row = self._row("aaa")
        with mock.patch.object(dispatches, "snapshot",
                               return_value=(self._rows(row), None)), \
                mock.patch.object(dispatches, "owed", side_effect=spy):
            got = idle_dispatch._undelivered_obligations()
        self.assertEqual(len(calls), 1,
                         "the sweep did not consult dispatches.owed()")
        # THE RESULT IS ASSERTED AND IT IS NON-EMPTY, deliberately: a spy that
        # fires while the function returns garbage proves instrumentation and
        # nothing else, and an EMPTY expectation would hold for a function that
        # consulted owed() and then threw its answer away.
        self.assertEqual([r["id"] for r in got], ["aaa"])

    def test_check_SURFACES_the_redeliverable_population_under_its_own_key(self):
        """The enumeration is only worth building if a caller can SEE it —
        the owner asked to be ABLE TO BURN DOWN ALL ROWS, and a seat that
        cannot ask "what do I owe" cannot start.

        AND THE KEY IS DISTINCT ON PURPOSE. `undelivered` already means "latch
        keys whose send failed" in this same dict. I first bound my population
        to that same name and the binding was DEAD, because the existing one is
        re-initialised further down — one name for two facts, where the loser
        is silent.

        LOAD-BEARING MUTATION: drop "redeliverable" from check()'s return ->
        KeyError here, while every other arm in this file stays green.
        """
        from helm import dispatches
        row = {"id": "aaaaaaaa11", "delivery": "needs-confirmation",
               "recipient": "seat-a", "lane": "lane/x"}
        with mock.patch.object(idle_dispatch, "scan", return_value=[]), \
                mock.patch.object(dispatches, "snapshot",
                                  return_value=({row["id"]: row}, None)), \
                mock.patch.object(dispatches, "owed", return_value=[row]):
            out = idle_dispatch.check(post=False, quiet=True)
        self.assertIn("redeliverable", out)
        got = out["redeliverable"]
        # MUST-HIT: non-empty, so the shape assertions below are about a real
        # row rather than an empty list that would satisfy any of them.
        self.assertTrue(got, "the redeliverable population came back empty")
        self.assertEqual(got[0]["id"], "aaaaaaaa11")
        self.assertEqual(got[0]["recipient"], "seat-a")
        self.assertEqual(got[0]["leg"], idle_dispatch.LEG_REDELIVER)
        # AND THE TWO KEYS ARE NOT THE SAME FACT: `undelivered` is latch keys.
        self.assertIsNot(out.get("undelivered"), got)

    def test_check_reports_redeliverable_None_when_the_ledger_is_unreadable(self):  # noqa: VACUOUS_ASSERTION — the sibling arm above asserts a NON-EMPTY population through the same call path, so an arm that never reached check() reddens there first
        """UNREADABLE IS NOT EMPTY, carried all the way to the caller. `[]`
        here would tell a burn-down surface that nothing is owed on a ledger
        nobody could read — the fail-open this task exists to end.
        """
        from helm import dispatches
        with mock.patch.object(idle_dispatch, "scan", return_value=[]), \
                mock.patch.object(dispatches, "snapshot",
                                  return_value=({}, "ledger unreadable")):
            out = idle_dispatch.check(post=False, quiet=True)
        self.assertIsNone(out["redeliverable"])


class RedeliverableReachesTheHumanSurfaceTest(unittest.TestCase):
    """The second blocker on this slice, made into arms.

    The enumeration existed only at its producer and in tests. The renderer
    showed findings and, when those were empty, printed "no idle dispatches"
    even with rows owed — so the one command that claims to answer "what do I
    owe" said the opposite of what check() had computed. This module's own
    _alert_note docstring already states the law: a leg whose refusals are
    invisible is indistinguishable from a leg that never ran.
    """

    def _line(self, out, needle):
        """The SINGLE output line containing `needle`, refusing zero or many.

        THE CANON, helm store `assert-the-row-not-the-whole-screen` (1.00,
        measured on task/872): a render assertion scoped to the WHOLE output is
        satisfied by the footer and can never fail. My arms asserted against the
        captured buffer, so the summary line supplied every scope word the
        per-row line was supposed to earn — a probe proved it by deleting only
        the per-row scope, and all six arms stayed green.

        THE "EXACTLY ONE" IS THE CANON'S OWN REFINEMENT AND NOT MINE. My
        proposal was "bind the assertion to a line", which would have returned
        the first match and silently tolerated zero — so an ABSENCE assertion
        could still pass by the row having vanished entirely. That is the cure
        wearing the defect's clothes.
        """
        hits = [ln for ln in out.splitlines() if needle in ln]
        self.assertEqual(len(hits), 1,
                         "expected exactly one line containing %r, got %d:\n%s"
                         % (needle, len(hits), out))
        return hits[0]

    def _run(self, redeliverable, findings=()):
        import contextlib
        import io
        res = {"findings": list(findings), "alerted": [], "woke": [],
               "undelivered": [], "redeliverable": redeliverable}
        buf = io.StringIO()
        with mock.patch.object(idle_dispatch, "check", return_value=res), \
                contextlib.redirect_stdout(buf):
            rc = idle_dispatch.cmd_idle_dispatch([])
        return rc, buf.getvalue()

    def test_an_owed_row_is_PRINTED_not_merely_counted_in_the_dict(self):
        rc, out = self._run([{"id8": "aaaaaaaa", "id": "aaaaaaaa11",
                              "recipient": "seat-a", "lane": "lane/x",
                              "delivery": "needs-confirmation",
                              "leg": idle_dispatch.LEG_REDELIVER}])
        row = self._line(out, "aaaaaaaa")       # MUST-HIT: exactly one row line
        self.assertIn("seat-a", row)
        self.assertIn("lane/x", row)
        self.assertEqual(rc, 0)
        # AND THE EMPTY LINE MUST NOT APPEAR, which is the actual defect:
        # findings was empty here, and the old renderer would have claimed
        # there was nothing to do while a row sat owed.
        self.assertNotIn("no idle dispatches", out)

    def test_an_UNREADABLE_ledger_says_UNKNOWN_and_exits_NON_ZERO(self):
        rc, out = self._run(None)
        self.assertIn("UNKNOWN", out)
        self.assertIn("NOT an empty answer", out)
        self.assertNotIn("no idle dispatches", out)
        # THE EXIT CODE IS THE PART A SCRIPT READS. rc 0 here would let a
        # burn-down caller record "nothing owed" for a question never answered.
        self.assertEqual(rc, 1)

    def test_a_genuinely_empty_answer_still_says_so_and_exits_zero(self):
        """THE NEGATIVE CONTROL. Without it the two arms above are satisfied by
        a renderer that shouts UNKNOWN unconditionally.

        AND IT PINS THE SCOPE ON THIS POLARITY TOO, which is a review's second
        review of this slice: the empty branch used to print an unqualified
        "no row is owed undelivered", the totalizing claim on the polarity a
        reader is MOST likely to believe. THIS TEST BLESSED IT — it asserted
        only the substring "no idle dispatches", so the honest narrow branch
        and the totalizing empty branch both satisfied it.
        """
        rc, out = self._run([])
        self.assertIn("no idle dispatches", out)
        self.assertNotIn("UNKNOWN", out)
        self.assertEqual(rc, 0)
        # the empty answer carries the SAME qualifier as the non-empty one
        self.assertIn("dispatch-row", out)
        self.assertIn("FLOOR", out)
        self.assertIn("not obligation-level", out)

    def test_the_render_DECLARES_its_narrower_domain(self):
        """The first blocker is real and its seam does not exist yet, so
        the surface must not imply a completeness it cannot deliver. A count
        presented as a total, computed from dispatch-row delivery, would be the
        same fail-open one layer up."""
        rc, out = self._run([{"id8": "bbbbbbbb", "id": "bbbbbbbb22",
                              "recipient": "seat-b", "lane": "lane/y",
                              "delivery": "", "leg": idle_dispatch.LEG_REDELIVER}])
        self.assertIn("FLOOR", out)
        self.assertIn("not obligation-level", out)
        self.assertEqual(rc, 0)

    def _run_json(self, redeliverable):
        import contextlib
        import io
        import json as _json
        res = {"findings": [], "alerted": [], "woke": [], "undelivered": [],
               "redeliverable": redeliverable}
        buf = io.StringIO()
        with mock.patch.object(idle_dispatch, "check", return_value=res), \
                contextlib.redirect_stdout(buf):
            rc = idle_dispatch.cmd_idle_dispatch(["--json"])
        return rc, _json.loads(buf.getvalue())

    def test_the_JSON_shape_DECLARES_its_scope_on_every_polarity(self):
        """The second blocker: a machine burn-down consumer reads this
        dict and NOTHING ELSE. It never reads --help and never sees the human
        line, so an undeclared list is readable only as the total the ruling
        forbids. The scope has to travel WITH the data.

        All three polarities are armed, because the scope escaping on any one
        of them is the same defect — and the empty one is where it matters
        most, since an empty list plus no scope reads as 'nothing is owed'.
        """
        checked = 0
        for label, pop in (("non-empty", [{"id8": "cccccccc", "id": "cccccccc33",
                                           "recipient": "seat-c", "lane": "lane/z",
                                           "delivery": "",
                                           "leg": idle_dispatch.LEG_REDELIVER}]),
                           ("empty", []),
                           ("unreadable", None)):
            rc, doc = self._run_json(pop)
            self.assertEqual(doc["redeliverable_scope"],
                             idle_dispatch.REDELIVERABLE_SCOPE, label)
            self.assertIs(doc["redeliverable_complete"], False, label)
            self.assertEqual(doc["redeliverable"], pop, label)
            self.assertEqual(rc, 1 if pop is None else 0, label)
            checked += 1
        # POSITIVE CONTROL on this exact loop: three polarities, asserted after
        # it runs. A loop that iterated nothing would satisfy every claim above.
        self.assertEqual(checked, 3)

    def test_the_scope_constant_is_the_ONE_spelling_both_surfaces_use(self):
        """A human line and a JSON field promising the same thing in two
        independently-typed spellings is two promises, and one of them rots.

        THIS ARM WAS FALSE WHEN I FIRST WROTE IT, and a review caught it: it
        claimed BOTH surfaces in its name and executed only _run_json. The
        constant reached the JSON field alone while every human line spelled
        "dispatch-row" by hand, so the arm stayed green with every human scope
        word deleted. A test whose NAME claims more than its BODY executes is
        worse than no test — it is a false all-clear with a reassuring label.

        Both paths now run here, and the human half DERIVES from the machine
        half rather than sitting beside it.

        LOAD-BEARING MUTATION: retype the human lines with a literal
        "dispatch-row" and change REDELIVERABLE_SCOPE — the human assertions
        below fail, because they check for the constant's OWN derived kind
        rather than for the string it happens to equal today.
        """
        self.assertTrue(idle_dispatch.REDELIVERABLE_SCOPE)      # MUST-HIT
        kind = idle_dispatch.REDELIVERABLE_SCOPE_KIND
        self.assertTrue(kind)                                   # MUST-HIT
        self.assertIn(kind, idle_dispatch.REDELIVERABLE_SCOPE)

        # THE MACHINE SURFACE
        _, doc = self._run_json([])
        self.assertEqual(doc["redeliverable_scope"],
                         idle_dispatch.REDELIVERABLE_SCOPE)

        # THE HUMAN SURFACE — the same walk, on BOTH populations that print a
        # scope, because the empty one is where the qualifier escaped before.
        _, populated = self._run([{"id8": "dddddddd", "id": "dddddddd44",
                                   "recipient": "seat-d", "lane": "lane/w",
                                   "delivery": "",
                                   "leg": idle_dispatch.LEG_REDELIVER}])
        _, empty = self._run([])
        # EACH CLAIM BOUND TO THE LINE THAT MUST CARRY IT. Asserting against the
        # buffer let the summary line satisfy the per-row claim.
        self.assertIn(kind, self._line(populated, "dddddddd"))
        self.assertIn(idle_dispatch.REDELIVERABLE_SCOPE_NOTE,
                      self._line(populated, "owed-undelivered:"))
        self.assertIn(idle_dispatch.REDELIVERABLE_SCOPE_NOTE,
                      self._line(empty, "no idle dispatches"))
        # AND THE FLOOR WORDING COMES FROM THE COMPLETE FLAG, one fact:
        self.assertFalse(idle_dispatch.REDELIVERABLE_COMPLETE)
        self.assertIn("a FLOOR, not a total",
                      idle_dispatch.REDELIVERABLE_SCOPE_NOTE)


    def test_the_slug_suffix_and_the_complete_flag_are_ONE_fact(self):
        """The second finding, which survived one cure.

        I derived the human NOTE from REDELIVERABLE_COMPLETE and left the SLUG
        spelling "floor" as a literal, so flipping COMPLETE produced
        scope="dispatch-row-floor" WITH complete=true and a human sentence
        saying COMPLETE total — three surfaces, two answers. Deriving one of
        three from the fact is not deriving; it just moves which pair can
        disagree.

        THE RELATION IS ARMED HERE rather than the values, so this arm survives
        the day someone legitimately flips COMPLETE to True.
        """
        complete = idle_dispatch.REDELIVERABLE_COMPLETE
        slug = idle_dispatch.REDELIVERABLE_SCOPE
        note = idle_dispatch.REDELIVERABLE_SCOPE_NOTE
        kind = idle_dispatch.REDELIVERABLE_SCOPE_KIND
        self.assertTrue(kind)                                   # MUST-HIT
        self.assertTrue(slug.startswith(kind + "-"), slug)
        # THE SUFFIX IS THE FACT, not a second copy of it
        self.assertEqual(slug, "%s-%s" % (kind, "total" if complete else "floor"))
        # and the human sentence agrees with the same fact
        self.assertIn("a COMPLETE total" if complete else "a FLOOR, not a total",
                      note)
        self.assertNotIn("a FLOOR, not a total" if complete
                         else "a COMPLETE total", note)
        # and the JSON field publishes that fact rather than a literal
        _, doc = self._run_json([])
        self.assertIs(doc["redeliverable_complete"], complete)
        self.assertEqual(doc["redeliverable_scope"], slug)


class ADroppedRowIsTheRungsJobTest(IdleDispatchBase):
    """TWO EXCLUSIONS IN THIS RUNG NAMED STATES IT EXISTS TO REPORT.

    An exclusion is only honest when the state it names is a state NOBODY needs
    told, and both of these failed that test for opposite reasons.

    SELF-ADDRESSED: the sender and the recipient are one seat, so the DM would
    be a seat talking to itself — true, and one party short. A row still has a
    THIRD reader who is neither, and owedpush already names them in this rung's
    own words: "a row nobody owns is not a row nobody owes."

    FRESH-BUT-WALLED: freshness measures whether a process is TICKING, and this
    rung asks whether work can COMPLETE. Those are the same reading for a
    healthy seat and OPPOSITE readings for one its provider is refusing, which
    keeps crossing tool boundaries while finishing nothing.

    ONE ROW CAN FAIL BOTH TESTS AT ONCE — overdue, self-addressed, and sitting
    on a seat whose whole provider family is walled with its panes live — and
    the rung then reports "no idle dispatches" while it sits there. Either
    exclusion alone hides it, so every arm below carries its own must-differ
    control IN THE SAME CALL."""

    A = "seat-a"                  # the self-addressed row's seat
    B = "seat-b"                  # an ordinary recipient
    C = "seat-c"                  # an ordinary sender
    U = "seat-under-test"         # the walled recipient

    WALL = {"state": "WALLED", "blocked_on": "upstream RATE-LIMITED since T",
            "remediation": {"restart": "UNKNOWN", "target": None,
                            "evidence": "restart effect unmeasured",
                            "action": None}}
    FINE = {"state": "IDLE", "blocked_on": None, "evidence": "pane-tail",
            "remediation": None}

    def _liveness(self, by_seat):
        """Patch the ONE liveness owner, per seat, for the whole scan."""
        from helm import seat
        return mock.patch.object(
            seat, "seat_liveness",
            side_effect=lambda s, *a, **k: by_seat.get(str(s)))

    # -- the self-addressed row ---------------------------------------------

    def test_a_SELF_ADDRESSED_row_reaches_the_integrator_not_the_void(self):
        """The row whose sender IS its recipient is the one row no seat's own
        `dispatch list --mine --issued` can contain except the seat that cannot
        read it. It must survive to a finding and be routed to the third party
        rather than back at itself.

        BOTH ROWS IN ONE SCAN, so the ordinary row is a positive control on the
        same observables: a cure that marked everything self-addressed, or sent
        every alert to the integrator, satisfies half of this and fails here."""
        self.rows = [_row("aaaa11112222", self.A, sender=self.A),
                     _row("c0c0c0c0cccc", self.B, sender=self.C)]
        self.presence = {self.A: "quiet", self.B: "quiet"}
        by_id = {f["id8"]: f for f in idle_dispatch.scan()}
        self.assertEqual(sorted(by_id), ["aaaa1111", "c0c0c0c0"],
                         "the self-addressed row was dropped before it could "
                         "be classified")
        self.assertTrue(by_id["aaaa1111"]["self_addressed"])
        self.assertEqual(by_id["aaaa1111"]["sender"], FIXTURE_INTEGRATOR)
        self.assertNotEqual(by_id["aaaa1111"]["sender"],
                            by_id["aaaa1111"]["recipient_key"],
                            "routed the alert back at the seat it is about")
        self.assertFalse(by_id["c0c0c0c0"]["self_addressed"])
        self.assertEqual(by_id["c0c0c0c0"]["sender"], self.C)

    def test_a_seat_talking_to_ITSELF_AS_THE_INTEGRATOR_still_has_nobody(self):
        """"Route it to the third party" reads correct until the self-addressed
        seat IS the third party, and then it rebuilds the self-DM the exclusion
        existed to prevent. The rule is "there is someone who is NEITHER", and
        where there is not, the original exclusion is right and stays.

        The ordinary row in the same scan is the positive control: the rung is
        working, this one row genuinely has no reader."""
        self.rows = [_row("cccccccc3333", FIXTURE_INTEGRATOR,
                          sender=FIXTURE_INTEGRATOR),
                     _row("c0c0c0c0cccc", self.B, sender=self.C)]
        self.presence = {FIXTURE_INTEGRATOR: "quiet", self.B: "quiet"}
        got = [f["id8"] for f in idle_dispatch.scan()]
        self.assertEqual(got, ["c0c0c0c0"])

    def test_an_UNRESOLVABLE_integrator_skips_the_row_it_cannot_reroute(self):
        """AN INTEGRATOR NOBODY CAN NAME IS NOT A SEAT THAT FAILS THE MATCH.

        Both halves of the reroute need a NAME: one to ask whether the
        self-talking seat IS the integrator, one to address them. A module
        constant supplies a name unconditionally, which is exactly why it
        looks like it works — the reroute is written, the DM is sent, and the
        seat it names may not exist. Asked instead, the resolver can answer
        that it does not know, and the block's own rule decides what follows:
        there must be someone who is NEITHER, and when nobody can be named
        there is not. The row is SKIPPED, not silently rerouted into the void.

        THE SEED IS UNDONE FOR THIS ARM ONLY. setUp answers the resolver
        because the ordinary world has an integrator, so the arm about NOT
        having one has to remove that answer rather than live somewhere the
        fixture cannot reach.
        """
        rows = [_row("aaaa11112222", self.A, sender=self.A),
                _row("c0c0c0c0cccc", self.B, sender=self.C)]
        presence = {self.A: "quiet", self.B: "quiet"}

        # POSITIVE CONTROL FIRST AND UNCONDITIONAL, on the same two rows: with
        # a resolver that answers, the self-addressed row survives and is
        # rerouted. Without this, the emptiness below would also be satisfied
        # by a scan that had stopped classifying anything at all.
        self.rows, self.presence = list(rows), dict(presence)
        seeded = {f["id8"]: f for f in idle_dispatch.scan()}
        self.assertEqual(sorted(seeded), ["aaaa1111", "c0c0c0c0"])
        self.assertEqual(seeded["aaaa1111"]["sender"], FIXTURE_INTEGRATOR)

        self.rows, self.presence = list(rows), dict(presence)
        with mock.patch.object(idle_dispatch.seats_integrator,
                               "integrator_seat",
                               return_value=(None, "no roster row resolves")):
            got = {f["id8"]: f for f in idle_dispatch.scan()}
        # THE ORDINARY ROW IS UNTOUCHED, which is what makes the missing one a
        # decision about that row rather than the rung going dark.
        self.assertEqual(sorted(got), ["c0c0c0c0"])
        self.assertEqual(got["c0c0c0c0"]["sender"], self.C)

    # -- the fresh-but-walled recipient -------------------------------------

    def test_a_FRESH_recipient_whose_PROVIDER_IS_WALLING_is_not_dropped(self):
        """A walled seat answers — it crosses tool boundaries — so presence
        reads `fresh` forever and every row addressed to it was dropped at the
        freshness gate, before the wall read that would have explained it.

        THE MUST-DIFFER CONTROL IS THE SECOND ROW OF THE SAME SCAN: a fresh
        recipient that is merely BUSY is still dropped, which is the whole
        reason the freshness gate exists. Admitting every fresh recipient
        would satisfy the first assertion and turn this rung into a nag on
        every working seat."""
        self.rows = [_row("d1d1d1d1dddd", self.U, sender=self.C),
                     _row("d2d2d2d2dddd", self.B, sender=self.C)]
        self.presence = {self.U: "fresh", self.B: "fresh"}
        with self._liveness({self.U: self.WALL, self.B: self.FINE}):
            findings = idle_dispatch.scan()
        self.assertEqual([f["id8"] for f in findings], ["d1d1d1d1"],
                         "either the walled row was read as busy and dropped, "
                         "or the merely-busy row was admitted with it")
        f = findings[0]
        self.assertTrue(f["fresh_walled"])
        self.assertEqual(f["presence"], "fresh")
        self.assertIn("WALLED", f["wall"])

    def test_the_WALL_TEXT_travels_on_a_row_admitted_BECAUSE_of_it(self):
        """A row admitted BECAUSE it is walled carries its wall whatever claim
        state it lands in — otherwise the finding prints with no explanation
        but the one fact it declined to read. The CLAIM_NONE gate on that read
        is a cost choice about rows that arrive on their own; the stranded row
        in the same scan proves that path still pays for its own wall."""
        self.rows = [_row("d3d3d3d3dddd", self.U, sender=self.C),
                     _row("d5d5d5d5dddd", self.B, sender=self.C)]
        self.presence = {self.U: "fresh", self.B: "quiet"}
        self.claims = {"worktree:helm:some-lane": {"holder": self.U}}
        with self._liveness({self.U: self.WALL, self.B: self.WALL}):
            by_id = {f["id8"]: f for f in idle_dispatch.scan()}
        self.assertEqual(by_id["d3d3d3d3"]["claim"], idle_dispatch.CLAIM_HOLDING)
        self.assertIn("WALLED", by_id["d3d3d3d3"]["wall"],
                      "admitted for a reason the finding then refused to show")
        self.assertIn("WALLED", by_id["d5d5d5d5"]["wall"])

    def test_a_WALLED_recipient_is_never_woken(self):
        """Admitting the row must not arm the wake leg: delivering a row to a
        seat its provider is refusing bills it for someone else's 429. The
        healthy stranded row in the same scan is the positive control — if
        nothing is ever wakeable the first assertion is satisfied by a dead
        wake leg."""
        self.rows = [_row("d4d4d4d4dddd", self.U, sender=self.C),
                     _row("d5d5d5d5dddd", self.B, sender=self.C)]
        self.presence = {self.U: "fresh", self.B: "quiet"}
        self.beacons = {self.U: ([4242], ""), self.B: ([4243], "")}
        with self._liveness({self.U: self.WALL, self.B: self.FINE}):
            by_id = {f["id8"]: f for f in idle_dispatch.scan()}
        self.assertFalse(by_id["d4d4d4d4"]["wakeable"])
        self.assertTrue(by_id["d5d5d5d5"]["wakeable"])

    # -- the alert has to SAY why the reader is holding it -------------------

    def test_the_alert_TELLS_the_integrator_why_a_row_it_never_sent_arrived(self):
        """Routing without an account is half the cure: the integrator gets an
        alert about a dispatch they did not send, with no sentence saying so.
        The ordinary row's alert, produced by the SAME check, is the control —
        a note printed on every alert explains nothing about this one."""
        self.rows = [_row("aaaa11112222", self.A, sender=self.A),
                     _row("c0c0c0c0cccc", self.B, sender=self.C)]
        self.presence = {self.A: "quiet", self.B: "quiet"}
        idle_dispatch.check()
        by_to = {to: text for to, text in self.dms}
        self.assertIn(FIXTURE_INTEGRATOR, by_to)
        self.assertIn(self.C, by_to)
        self.assertIn("SELF-ADDRESSED", by_to[FIXTURE_INTEGRATOR])
        self.assertIn("you are NEITHER", by_to[FIXTURE_INTEGRATOR])
        self.assertNotIn("SELF-ADDRESSED", by_to[self.C])
        self.assertIn("no recent activity", by_to[self.C])

    def test_the_alert_never_calls_a_WALLED_seat_inactive(self):
        """`presence: fresh` and "(no recent activity)" in one sentence is a
        self-contradiction, and the reader picks whichever half suits them. The
        parenthetical was a constant and had to stop being one. The quiet row
        in the same check proves the constant is still there for everyone."""
        self.rows = [_row("d6d6d6d6dddd", self.U, sender=self.C),
                     _row("d5d5d5d5dddd", self.B, sender=self.C)]
        self.presence = {self.U: "fresh", self.B: "quiet"}
        with self._liveness({self.U: self.WALL, self.B: self.FINE}):
            idle_dispatch.check()
        texts = [t for _to, t in self.dms]
        walled = [t for t in texts if "d6d6d6d6" in t][0]
        quiet = [t for t in texts if "d5d5d5d5" in t][0]
        self.assertIn("fresh", walled)
        self.assertNotIn("no recent activity", walled)
        self.assertIn("activity here is not progress", walled)
        self.assertIn("FRESHNESS GATE", walled)
        self.assertIn("no recent activity", quiet)
        self.assertNotIn("FRESHNESS GATE", quiet)

    def test_an_ordinary_stranded_alert_is_UNCHANGED_by_all_of_this(self):
        """THE REGRESSION CONTROL. Two new slots were cut into all three
        templates; the ordinary row must read exactly as it did, or every alert
        on the estate now carries the cost of two cases it is not."""
        self.rows = [_row("e7e7e7e7eeee", self.B, sender=self.C)]
        self.presence = {self.B: "quiet"}
        idle_dispatch.check()
        text = self.dms[0][1]
        self.assertIn("is STRANDED — the recipient is quiet (no recent "
                      "activity) AND holds no live claim", text)
        self.assertNotIn("SELF-ADDRESSED", text)
        self.assertNotIn("FRESHNESS GATE", text)
