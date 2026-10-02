#!/usr/bin/env python3
"""task/3899, office floor slice 1: a seat's MOOD, measured and self-reported.

THE OWNER asked for each seat to show whether it is frustrated or happy with
its progress, the way a person at a cubicle does. `helm seat mood` measures it
from the trace helm already keeps and lets the seat say it in one word.

WHAT THESE PIN, one class per claim the module makes:

  JUDGE      the six states and the one-line reason, from synthetic inputs:
             refusals weighted by repeats of one guard, loops (friction runs,
             re-run commands, failing calls), minutes busy without progress,
             a wall (walled, never frustrated: score 0), an open decision card
             (blocked-on-owner), and idle;
  HONESTY    a fresh self-report of flowing/fine beside a measured stuck or
             grinding is DIVERGENT; a stale one is not;
  LEDGER     the friction rows `friction.record` really writes are what the
             measure reads;
  SOURCES    each progress source (tasks, dispatches, lanes, lands) credits
             the seat its real producer names, and a source that cannot be
             read is named, never read as "no progress";
  SELF       `helm seat mood set` records a word for the calling seat's
             ROSTER identity and refuses anything else;
  ASK        the turn-start ask fires at most once an hour per seat, rides the
             reflex lane through `helm inject`, and never fires for a process
             no roster seat names;
  RANK       helm's own friction ranked across seats, plus the burn-down notes
             file where the burn-down notes' ranking plugs in (a synthetic
             fixture here);
  CLI        every verb answers through `helm seat mood` itself.

Every world is a temp HELM_HOME. Nothing reads this machine's ledgers, roster,
transcripts or credentials, and nothing posts or pushes.
"""
import calendar
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-seatmood-", var="HELM_HOME")

from helm import friction, home, pk  # noqa: E402
# the seat facade beside any seat impl the arms reach (the co-occurrence law)
from helm import seat  # noqa: E402,F401

SEAT = "mood-seat"
MIN = 60
NOW = calendar.timegm((2026, 9, 30, 18, 0, 0, 0, 0, 0))


def _sm():
    """The module under test, imported per arm so the pre-change tree reds
    arm by arm rather than as one collection error."""
    from helm import seatmood
    return seatmood


def _sig():
    from helm import seatmood_signals
    return seatmood_signals


def raw(**over):
    """One seat's measured inputs with nothing happening: busy, no friction,
    a progress event five minutes ago."""
    out = {"seat": SEAT, "refusals": [],
           "counters": {"loop-streak": 0, "stuck-streak": 0,
                        "stalled-turns": 0},
           "idle": {"state": "BUSY", "idle_s": None, "turn_opened": None,
                    "why": "a call 1m ago"},
           "progress": (NOW - 5 * MIN, "a commit on lane L"),
           "wall": None, "card": None, "context_pct": None, "self": None,
           "unread": []}
    out.update(over)
    return out


def refusal(ago_min, guard="author-gate", reason="PreToolUse:Bash"):
    return {"at": NOW - ago_min * MIN, "guard": guard, "reason": reason}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-seatmood-case-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = mock.patch.dict(os.environ, {
            "HELM_HOME": os.path.join(self.tmp, "helm"),
            "HELM_CHAT_DIR": os.path.join(self.tmp, "chat"),
            "HELM_CHAT_NAME": SEAT, "HELM_SCRATCH_GC": "0"})
        env.start()
        self.addCleanup(env.stop)

    def run_cli(self, *args):
        """`helm seat mood ...` through the real CLI entry -> (rc, out, err)."""
        from helm import cli
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(["seat", "mood"] + list(args))
            except SystemExit as exc:
                rc = exc.code
        return rc, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------- JUDGE

def judged(**over):
    """The judge over `raw(**over)` at NOW. A function, not a method: `self`
    is one of raw()'s input keys."""
    return _sm().judge(raw(**over), NOW)


class JudgeTest(unittest.TestCase):
    def test_the_contract_keys_and_states(self):
        sm = _sm()
        self.assertEqual(sm.STATES, ("flowing", "grinding", "stuck",
                                     "blocked-on-owner", "walled", "idle"))
        got = judged()
        self.assertEqual(set(got), {"seat", "state", "score", "reason", "self",
                                    "self_at", "divergent", "signals",
                                    "cause", "checkin"})
        self.assertEqual((got["seat"], got["state"], got["score"],
                          got["cause"], got["checkin"]),
                         (SEAT, "flowing", 0, "progress", None))
        self.assertIn("last progress 5m ago: a commit on lane L", got["reason"])
        self.assertIs(got["divergent"], False)

    def test_three_refusals_by_one_guard_is_grinding_and_says_so(self):
        got = judged(refusals=[refusal(20), refusal(12), refusal(2)])
        self.assertEqual(got["state"], "grinding", got)
        self.assertEqual(got["reason"], "3 refusals by author-gate in 20 min")
        self.assertEqual(got["signals"]["by_guard"], {"author-gate": 3})
        self.assertGreaterEqual(got["score"], 25)

    def test_repeats_of_one_guard_weigh_more_than_scattered_refusals(self):
        """The spec's weighting: three refusals by ONE guard outweigh three
        by three different guards, which read as ordinary friction."""
        same = judged(refusals=[refusal(30, reason="a"),
                                    refusal(20, reason="b"),
                                    refusal(10, reason="c")])
        spread = judged(refusals=[refusal(30, guard="g1"),
                                      refusal(20, guard="g2"),
                                      refusal(10, guard="g3")])
        self.assertGreater(same["score"], spread["score"])
        self.assertEqual(spread["state"], "flowing", spread)
        self.assertEqual(spread["reason"],
                         "3 refusals in 30 min, 1 by g1")

    def test_four_identical_refusals_in_a_row_is_a_loop_and_stuck(self):
        got = judged(refusals=[refusal(9), refusal(8), refusal(7),
                                   refusal(6)], progress=None)
        self.assertEqual(got["state"], "stuck", got)
        self.assertEqual(got["reason"],
                         "the same refusal by author-gate 4 times in a row")
        self.assertEqual(got["signals"]["repeat"], 4)

    def test_a_land_commit_close_or_verdict_ends_the_refusal_run(self):
        refusals = [refusal(20), refusal(19), refusal(18), refusal(17),
                    refusal(3), refusal(2), refusal(1)]
        for event in ("a land of lane 553", "a commit on lane 553",
                      "closed task/553", "a verdict on 553"):
            with self.subTest(event=event):
                got = judged(refusals=refusals,
                             progress=(NOW - 10 * MIN, event),
                             forward_progress=(NOW - 10 * MIN, event))
                self.assertEqual(got["signals"]["repeat"], 3, got)
                self.assertEqual(got["state"], "grinding", got)
        control = judged(refusals=[refusal(i) for i in range(7, 0, -1)],
                         progress=None)
        self.assertEqual((control["signals"]["repeat"], control["state"]),
                         (7, "stuck"))

    def test_a_comment_does_not_claim_a_forward_operation(self):
        refusals = [refusal(i) for i in range(7, 0, -1)]
        got = judged(refusals=refusals,
                     progress=(NOW - 4 * MIN, "a comment on task/553"))
        self.assertEqual((got["signals"]["repeat"], got["state"]),
                         (7, "stuck"))

    def test_spaced_refusals_are_not_a_continuous_loop(self):
        twelve = [refusal(i) for i in (56, 52, 48, 44, 40, 36, 32, 28,
                                       24, 8, 4, 2)]
        spaced = judged(refusals=twelve,
                        progress=(NOW - 12 * MIN, "a land of lane 553"),
                        forward_progress=(NOW - 12 * MIN,
                                          "a land of lane 553"))
        self.assertEqual(spaced["signals"]["refusals"], 12)
        self.assertLess(spaced["signals"]["repeat"], 4, spaced)
        self.assertNotEqual(spaced["state"], "stuck", spaced)
        continuous = judged(refusals=[refusal(i / 6) for i in range(12, 0, -1)],
                            progress=None)
        self.assertEqual((continuous["state"], continuous["signals"]["repeat"]),
                         ("stuck", 12))

    def test_a_progress_gap_over_an_hour_never_reads_flowing(self):
        got = judged(progress=(NOW - 5 * 3600 - 24 * MIN,
                               "a commit on lane simbi"),
                     idle={"state": "BUSY", "turn_opened": NOW - 5 * MIN})
        self.assertNotEqual(got["state"], "flowing", got)
        self.assertEqual(got["cause"], "quiet", got)
        self.assertIn("5h24m", got["reason"])
        idle = judged(progress=(NOW - 5 * 3600 - 24 * MIN, "a commit"),
                      idle={"state": "IDLE", "idle_s": 10 * MIN})
        self.assertEqual(idle["state"], "idle", idle)
        recent = judged(progress=(NOW - 59 * MIN, "a commit"),
                        idle={"state": "BUSY", "turn_opened": NOW - 5 * MIN})
        self.assertEqual(recent["state"], "flowing", recent)

    def test_a_re_run_command_and_failing_calls_are_loops(self):
        rerun = judged(counters={"loop-streak": 3, "stuck-streak": 0,
                                     "stalled-turns": 0})
        self.assertEqual((rerun["state"], rerun["reason"]),
                         ("stuck", "the same command re-run 3 times"))
        failing = judged(counters={"loop-streak": 0, "stuck-streak": 4,
                                       "stalled-turns": 0})
        self.assertEqual((failing["state"], failing["reason"]),
                         ("stuck", "4 failing calls in a row"))

    def test_busy_without_progress_grinds_then_sticks(self):
        grind = judged(progress=(NOW - 50 * MIN, "a verdict on ab12cd34"))
        self.assertEqual(grind["state"], "grinding", grind)
        self.assertEqual(grind["reason"], "no progress for 50m while busy "
                         "(last: a verdict on ab12cd34)")
        stuck = judged(progress=(NOW - 130 * MIN, "a land of lane L"))
        self.assertEqual(stuck["state"], "stuck", stuck)
        self.assertEqual(stuck["signals"]["quiet_s"], 130 * MIN)

    def test_the_quiet_clock_starts_at_the_turn_the_seat_began(self):
        """A seat is not blamed for time it was not working: a turn that
        opened 10 minutes ago measures 10 minutes, not a day."""
        got = judged(progress=None,
                         idle={"state": "BUSY", "idle_s": None,
                               "turn_opened": NOW - 10 * MIN, "why": "x"})
        self.assertEqual(got["state"], "flowing", got)
        self.assertEqual(got["signals"]["quiet_s"], 10 * MIN)

    def test_an_idle_seat_reads_idle_not_quiet(self):
        got = judged(progress=(NOW - 300 * MIN, "x"),
                         idle={"state": "IDLE", "idle_s": 14 * MIN,
                               "turn_opened": None, "why": "ended"})
        self.assertEqual(got["state"], "idle", got)
        self.assertEqual(got["reason"], "idle 14m: its turn ended and "
                         "nothing has run since")
        resting = judged(idle={"state": "RESTING", "idle_s": None,
                                   "turn_opened": None,
                                   "why": "the owner paused it"})
        self.assertEqual((resting["state"], resting["reason"]),
                         ("idle", "resting: the owner paused it"))

    def test_a_walled_seat_is_walled_never_frustrated(self):
        wall = {"source": "family", "family": "kimi", "axis": "money",
                "why": "quota exhausted"}
        got = judged(wall=wall, refusals=[refusal(9), refusal(7),
                                              refusal(5), refusal(3)])
        self.assertEqual((got["state"], got["score"]), ("walled", 0), got)
        self.assertEqual(got["reason"],
                         "family kimi walled on money: quota exhausted")
        self.assertEqual(got["signals"]["wall"], wall)

    def test_an_open_decision_card_is_blocked_on_owner(self):
        card = {"id": "ab12cd34", "title": "pick a cubicle channel",
                "since": NOW - 70 * MIN}
        got = judged(card=card)
        self.assertEqual(got["state"], "blocked-on-owner", got)
        self.assertEqual(got["reason"], 'waiting on the owner: decide card '
                         'ab12cd34 "pick a cubicle channel", open 1h10m')

    def test_self_report_divergence_is_the_honesty_signal(self):
        loop = [refusal(4), refusal(3), refusal(2), refusal(1)]
        fine = {"word": "fine", "why": "all good", "at": NOW - 10 * MIN}
        got = judged(refusals=loop, self=fine)
        self.assertEqual((got["state"], got["self"]), ("stuck", "fine"))
        self.assertIs(got["divergent"], True)
        self.assertEqual(got["self_at"], pk.epoch_ts(NOW - 10 * MIN))
        stale = dict(fine, at=NOW - 3 * 3600)
        self.assertIs(judged(refusals=loop, self=stale)["divergent"],
                      False)
        honest = dict(fine, word="stuck")
        self.assertIs(judged(refusals=loop, self=honest)["divergent"],
                      False)
        flowing = judged(self=fine)
        self.assertIs(flowing["divergent"], False)

    def test_an_unread_source_is_named_never_read_as_no_progress(self):
        got = judged(progress=(NOW - 50 * MIN, "x"),
                         unread=["tasks ledger (OSError: denied)"])
        self.assertIn("(unread: tasks ledger (OSError: denied))",
                      got["reason"])
        self.assertEqual(got["signals"]["unread"],
                         ["tasks ledger (OSError: denied)"])

    def test_unread_progress_cannot_score_a_quiet_busy_seat(self):  # noqa: VACUOUS_ASSERTION — the unconditional readable-progress control below scores a stuck quiet seat on the same judge and points observables
        for progress in (None, (NOW - 130 * MIN, "a land of lane L")):
            got = judged(progress=progress,
                         idle={"state": "BUSY", "idle_s": None,
                               "turn_opened": NOW - 130 * MIN},
                         progress_unread=["tasks ledger (unreadable)"],
                         unread=["tasks ledger (unreadable)"])
            self.assertEqual(got["state"], "flowing", got)
            self.assertEqual(got["signals"]["points"]["quiet"], 0)
            self.assertIn("unread: tasks ledger (unreadable)", got["reason"])
        control = judged(progress=None, idle={"state": "BUSY",
                          "idle_s": None, "turn_opened": NOW - 130 * MIN})
        self.assertEqual(control["state"], "stuck", control)
        self.assertGreater(control["signals"]["points"]["quiet"], 0)

    def test_unread_progress_does_not_erase_a_tight_refusal_loop(self):
        loop = [refusal(i / 6) for i in range(7, 0, -1)]
        got = judged(refusals=loop, progress=None,
                     progress_unread=["lane commits in x (git rc 128)"],
                     unread=["lane commits in x (git rc 128)"])
        self.assertEqual((got["state"], got["signals"]["repeat"]),
                         ("stuck", 7), got)
        spaced = judged(refusals=[refusal(i) for i in (28, 24, 20, 16, 12)],
                        progress=None,
                        progress_unread=["lane commits in x (git rc 128)"],
                        unread=["lane commits in x (git rc 128)"])
        self.assertLess(spaced["signals"]["repeat"], 4, spaced)
        self.assertNotEqual(spaced["state"], "stuck", spaced)

    def test_context_pressure_and_stalled_turns_count(self):
        got = judged(context_pct=91.0,
                         counters={"loop-streak": 0, "stuck-streak": 0,
                                   "stalled-turns": 6})
        self.assertGreater(got["score"], 0)
        self.assertEqual(got["signals"]["context_pct"], 91.0)
        self.assertEqual(got["reason"], "6 turns in a row with no forward op")


# ---------------------------------------------------------------- LEDGER

class FrictionLedgerTest(Base):
    def test_measure_reads_the_rows_friction_record_writes(self):
        sm, sig = _sm(), _sig()
        for ago in (20, 12, 2):
            self.assertTrue(friction.record("author-gate", "PreToolUse:Bash",
                                            now=NOW - ago * MIN))
        # another seat's refusal is not this seat's
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "other-seat"}):
            self.assertTrue(friction.record("author-gate", now=NOW - MIN))
        with mock.patch.object(sig, "_land_progress",
                               return_value=({}, None)):
            got = sm.measure(SEAT, now=NOW)
        self.assertEqual(got["reason"], "3 refusals by author-gate in 20 min")
        self.assertEqual(got["signals"]["refusals"], 3)

    def test_twelve_uninterrupted_real_refusals_remain_stuck(self):
        sm, sig = _sm(), _sig()
        for ago in range(12, 0, -1):
            self.assertTrue(friction.record("stop-guard", "PreToolUse:Bash",
                                            now=NOW - ago * MIN))
        with mock.patch.object(sig, "SOURCES", ()), \
                mock.patch.object(sig, "_idle", return_value={
                    "state": "BUSY", "turn_opened": NOW - 12 * MIN}):
            got = sm.measure(SEAT, now=NOW)
        self.assertEqual((got["signals"]["repeat"], got["state"]),
                         (12, "stuck"))

    def test_an_unreadable_friction_ledger_is_named(self):
        sm, sig = _sm(), _sig()
        os.makedirs(friction.path())            # a directory is not a ledger
        with mock.patch.object(sig, "_land_progress",
                               return_value=({}, None)):
            got = sm.measure(SEAT, now=NOW)
        self.assertTrue(any(u.startswith("friction ledger")
                            for u in got["signals"]["unread"]), got)


# ---------------------------------------------------------------- SOURCES

class ProgressSourcesTest(Base):
    def test_tasks_credit_a_comment_and_a_close(self):
        from helm import tasks
        sig = _sig()
        row, err = tasks.add("measure the seat mood fixture", SEAT)
        self.assertIsNone(err)
        tid = row["id"]
        before = time.time()
        _r, err = tasks.comment(tid, "measured it", by=SEAT)
        self.assertIsNone(err)
        got, why = sig._task_progress({SEAT.casefold()}, before - 3600)
        self.assertIsNone(why)
        at, what = got[SEAT.casefold()]
        self.assertEqual(what, "a comment on %s" % tid)
        self.assertGreaterEqual(at, before)
        # a comment older than the window credits nothing, nor another seat
        self.assertEqual(sig._task_progress({SEAT.casefold()},
                                            time.time() + 60)[0], {})
        self.assertEqual(sig._task_progress({"nobody"}, before - 3600)[0], {})

    def _dispatch_rows(self, rows):
        from helm import dispatches, eventledger
        for r in rows:
            self.assertTrue(eventledger.append(dispatches.ledger_path(), r))

    def test_dispatch_verdicts_and_holds_credit_the_reader(self):
        sig = _sig()
        rid = "ab" * 16
        self._dispatch_rows([
            {"v": 3, "event": "dispatch", "seq": 0, "id": rid,
             "ts": pk.epoch_ts(NOW - 90 * MIN), "recipient": "reader-seat",
             "sender": "lead-seat", "lane": "lane-x", "kind": "review",
             "status": "open", "tip": "c" * 40, "deadline_s": 3600},
            {"v": 3, "event": "hold", "seq": 1, "id": rid,
             "ts": pk.epoch_ts(NOW - 40 * MIN), "reason": "r",
             "owner_gated": False},
            {"v": 3, "event": "verdict", "seq": 2, "id": rid,
             "ts": pk.epoch_ts(NOW - 20 * MIN), "polarity": "approve"},
        ])
        got, why = sig._dispatch_progress({"reader-seat", "lead-seat"},
                                          NOW - 120 * MIN)
        self.assertIsNone(why)
        self.assertEqual(got["reader-seat"],
                         (NOW - 20 * MIN, "a verdict on abababab"))
        self.assertEqual(got["lead-seat"],
                         (NOW - 90 * MIN, "a dispatch on lane-x"))

    def test_a_landed_close_credits_the_seat_that_sent_the_row(self):
        """task/4033: a merge subject names no author any more, so a land is
        credited from the ledger: a row closed landed or source-clean landed
        credits the seat that sent it (the land request's author), and it is
        forward progress. Any other close credits nothing."""
        sig = _sig()
        rows = (("ab" * 16, "lane-x", "landed", 30),
                ("cd" * 16, "lane-y", "source-clean-landed", 10),
                ("ef" * 16, "lane-z", "superseded", 5))
        for rid, lane, reason, ago in rows:
            self._dispatch_rows([
                {"v": 3, "event": "dispatch", "seq": 0, "id": rid,
                 "ts": pk.epoch_ts(NOW - 300 * MIN),
                 "recipient": "reader-seat", "sender": "lead-seat",
                 "lane": lane, "kind": "review", "status": "open",
                 "tip": "c" * 40, "deadline_s": 3600},
                {"v": 3, "event": "close", "seq": 1, "id": rid,
                 "ts": pk.epoch_ts(NOW - ago * MIN), "close_reason": reason}])
        forward = {}
        got, why = sig._dispatch_progress({"reader-seat", "lead-seat"},
                                          NOW - 120 * MIN, forward=forward)
        self.assertIsNone(why)
        self.assertEqual(got, {"lead-seat":
                               (NOW - 10 * MIN, "a land of lane lane-y")})
        self.assertEqual(forward, got)
        # The superseded close, the newest event, credits nothing: inside a
        # window that holds only it, the seats have no progress.
        self.assertEqual(sig._dispatch_progress(
            {"reader-seat", "lead-seat"}, NOW - 8 * MIN), ({}, None))
        # CONTROL: a window wide enough to hold the landed close on lane-y
        # too credits it.
        self.assertEqual(sig._dispatch_progress(
            {"reader-seat", "lead-seat"}, NOW - 12 * MIN),
            ({"lead-seat": (NOW - 10 * MIN, "a land of lane lane-y")}, None))

    def test_an_unreadable_source_is_a_reason_not_an_empty_answer(self):
        sig = _sig()
        from helm import dispatches
        os.makedirs(dispatches.ledger_path())
        got, why = sig._dispatch_progress({SEAT.casefold()}, NOW - 3600)
        self.assertEqual(got, {})
        self.assertTrue(why and why.startswith("dispatch ledger"), why)
        with mock.patch.object(sig, "SOURCES", (
                ("dispatch ledger", sig._dispatch_progress),)):
            prog, unread = sig.progress({SEAT.casefold()}, NOW)
        self.assertEqual(prog, {})
        self.assertEqual(len(unread), 1)

    def test_unread_progress_is_carried_separately_through_the_measure(self):
        sig, sm = _sig(), _sm()
        with mock.patch.object(sig, "progress", return_value=(
                {}, ["tasks ledger (unreadable)"])), \
                mock.patch.object(sig, "_land_progress",
                                  return_value=({}, None)), \
                mock.patch.object(sig, "_idle", return_value={
                    "state": "BUSY", "turn_opened": NOW - 130 * MIN,
                    "last_call": NOW - 130 * MIN}):
            got = sm.measure(SEAT, now=NOW)
        self.assertEqual(got["state"], "flowing", got)
        self.assertEqual(got["signals"]["progress_unread"],
                         ["tasks ledger (unreadable)"])
        self.assertEqual(got["signals"]["points"]["quiet"], 0)
        self.assertIn("unread: tasks ledger (unreadable)", got["reason"])

    def _repo(self):
        repo = os.path.join(self.tmp, "proj")
        os.makedirs(repo)
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")

        def git(*a, at=None):
            e = dict(env)
            if at is not None:
                e["GIT_AUTHOR_DATE"] = e["GIT_COMMITTER_DATE"] = "%d +0000" % at
            subprocess.run(("git", "-c", "core.hooksPath=/dev/null", "-c",
                            "commit.gpgsign=false", "-C", repo) + a,
                           check=True, env=e, capture_output=True)
        git("init", "-q", "-b", "main")
        git("commit", "-q", "--allow-empty", "-m", "base", at=NOW - 600 * MIN)
        return repo, git

    def test_a_lane_commit_credits_the_lease_holder(self):
        sig = _sig()
        from helm.seats_common import claims_path
        repo, git = self._repo()
        git("branch", "lane/mood-x")
        git("checkout", "-q", "lane/mood-x")
        git("commit", "-q", "--allow-empty", "-m", "work", at=NOW - 7 * MIN)
        git("checkout", "-q", "main")
        git("branch", "lane/empty-y")           # no commit off trunk: no credit
        os.makedirs(os.path.dirname(claims_path()), exist_ok=True)
        pk.write_json(claims_path(), {
            "_fence": 2,
            "worktree:proj:mood-x": {"holder": SEAT, "lease": "l1",
                                     "fence": 1, "ts": "x"},
            "worktree:proj:empty-y": {"holder": "other", "lease": "l2",
                                      "fence": 2, "ts": "x"}})
        with mock.patch.object(sig, "_project_roots",
                               return_value={"proj": repo}):
            got, why = sig._lane_progress({SEAT.casefold(), "other"},
                                          NOW - 3600)
        self.assertIsNone(why)
        self.assertEqual(got, {SEAT.casefold():
                               (NOW - 7 * MIN, "a commit on lane mood-x")})

    def test_a_land_between_real_refusals_ends_the_current_run(self):
        sm, sig = _sm(), _sig()
        repo, git = self._repo()
        git("commit", "-q", "--allow-empty", "-m",
            "train553: merge lane mood-x (task/3902; author %s)" % SEAT,
            at=NOW - 20 * MIN)
        for ago in (45, 44, 43, 42, 4, 3, 2):
            self.assertTrue(friction.record("stop-guard", "PreToolUse:Bash",
                                            now=NOW - ago * MIN))
        def land(keys, since, forward=None):
            return sig._land_progress(keys, since, repo=repo, trunk="main",
                                      forward=forward)
        with mock.patch.object(sig, "SOURCES", (("lands", land),)), \
                mock.patch.object(sig, "_idle", return_value={
                    "state": "BUSY", "turn_opened": NOW - 45 * MIN}):
            got = sm.measure(SEAT, now=NOW)
        self.assertEqual(got["signals"]["refusals"], 7)
        self.assertEqual(got["signals"]["repeat"], 3, got)
        self.assertEqual(got["signals"]["progress"], "a land of lane mood-x")
        self.assertEqual(got["state"], "grinding", got)

    def test_a_later_comment_cannot_hide_a_land_between_twelve_refusals(self):
        from helm import tasks
        sm, sig = _sm(), _sig()
        repo, git = self._repo()
        git("commit", "-q", "--allow-empty", "-m",
            "train553: merge lane mood-x (task/3902; author %s)" % SEAT,
            at=NOW - 5 * MIN - 30)
        for ago in range(15, 3, -1):
            self.assertTrue(friction.record("stop-guard", "PreToolUse:Bash",
                                            now=NOW - ago * MIN))

        def land(keys, since, forward=None):
            return sig._land_progress(keys, since, repo=repo, trunk="main",
                                      forward=forward)

        row = {"task/553": {"comments": [{"ts": NOW - 0.1 * MIN,
                                           "by": SEAT}]}}
        with mock.patch.object(tasks, "snapshot", return_value=(row, None)), \
                mock.patch.object(sig, "SOURCES", (
                    ("lands", land), ("tasks ledger", sig._task_progress))), \
                mock.patch.object(sig, "_idle", return_value={
                    "state": "BUSY", "turn_opened": NOW - 15 * MIN}):
            got = sm.measure(SEAT, now=NOW)
        self.assertEqual(got["signals"]["refusals"], 12)
        self.assertEqual(got["signals"]["progress"], "a comment on task/553")
        self.assertEqual(got["signals"]["repeat"], 2, got)
        self.assertNotEqual(got["state"], "stuck", got)
        continuous = judged(refusals=[refusal(i) for i in range(15, 3, -1)],
                            progress=(NOW - 0.1 * MIN,
                                      "a comment on task/553"))
        self.assertEqual((continuous["signals"]["repeat"], continuous["state"]),
                         (12, "stuck"))

    def test_a_land_credits_the_author_its_merge_names(self):
        sig = _sig()
        repo, git = self._repo()
        git("commit", "-q", "--allow-empty", "-m",
            "train7: merge lane mood-x (task/3899: x; P0; author %s; "
            "reader-seat read SOURCE-CLEAN abc)" % SEAT, at=NOW - 15 * MIN)
        got, why = sig._land_progress({SEAT.casefold()}, NOW - 3600,
                                      repo=repo, trunk="main")
        self.assertIsNone(why)
        self.assertEqual(got, {SEAT.casefold():
                               (NOW - 15 * MIN, "a land of lane mood-x")})


# ---------------------------------------------------------------- SELF

class SelfReportTest(Base):
    def actor(self, name=SEAT):
        from helm import actors
        return mock.patch.object(
            actors, "resolve_actor",
            return_value=(types.SimpleNamespace(canonical_name=name), None))

    def on_roster(self, *names):
        return mock.patch.object(_sm(), "_roster_key", side_effect=lambda n: (
            (n, None) if n in names else (None, "%s is not a seat on the "
                                          "roster" % n)))

    def test_set_records_the_word_for_the_roster_seat(self):
        sm = _sm()
        with self.actor(), self.on_roster(SEAT):
            rc, out, err = self.run_cli("set", "Fine", "--why",
                                        "the gate is green")
        self.assertEqual(rc, 0, err)
        self.assertIn(SEAT, out)
        said = sm.self_report(SEAT)
        self.assertEqual((said["word"], said["why"]),
                         ("fine", "the gate is green"))
        rows, why = sm.history()
        self.assertIsNone(why)
        self.assertEqual([(r["seat"], r["word"]) for r in rows],
                         [(SEAT, "fine")])

    def test_set_refuses_a_seat_off_the_roster_and_a_bad_word(self):
        with self.actor("stranger"), self.on_roster(SEAT):
            rc, _out, err = self.run_cli("set", "fine")
        self.assertEqual(rc, 1)
        self.assertIn("not a seat on the roster", err)
        with self.actor(), self.on_roster(SEAT):
            for bad in ("two words", "", "x" * 40, "fine!"):
                rc, _out, err = self.run_cli("set", bad)
                self.assertEqual(rc, 2, (bad, err))
        self.assertIsNone(_sm().self_report(SEAT))

    def test_an_identity_the_law_refuses_records_nothing(self):
        from helm import actors
        with mock.patch.object(actors, "resolve_actor",
                               return_value=(None, "DISPUTED: two names")):
            rc, _out, err = self.run_cli("set", "fine")
        self.assertEqual(rc, 1)
        self.assertIn("DISPUTED", err)
        self.assertIsNone(_sm().self_report(SEAT))

    def test_measure_shows_the_self_report_beside_the_measure(self):
        sm, sig = _sm(), _sig()
        with self.actor(), self.on_roster(SEAT):
            row, err = sm.set_mood("grinding", why="slow fab", now=NOW - MIN)
        self.assertIsNone(err)
        with mock.patch.object(sig, "_land_progress",
                               return_value=({}, None)):
            got = sm.measure(SEAT, now=NOW)
        self.assertEqual((got["self"], got["self_at"]),
                         ("grinding", pk.epoch_ts(NOW - MIN)))


# ---------------------------------------------------------------- ASK

class AskTest(Base):
    def on_roster(self):
        return mock.patch.object(_sm(), "_roster_key",
                                 side_effect=lambda n: (n, None))

    def test_asked_at_most_once_an_hour(self):
        sm = _sm()
        with self.on_roster():
            first = sm.ask("s-1", now=NOW)
            self.assertIsNotNone(first)
            line, seat_ = first
            self.assertEqual(seat_, SEAT)
            self.assertTrue(line.startswith("MOOD:"), line)
            self.assertIn("helm seat mood set", line)
            sm.mark_asked(SEAT, now=NOW)
            self.assertIsNone(sm.ask("s-1", now=NOW + 59 * MIN))
            self.assertIsNotNone(sm.ask("s-1", now=NOW + 61 * MIN))

    def test_a_fresh_self_report_needs_no_ask(self):
        sm = _sm()
        from helm import actors
        with self.on_roster(), mock.patch.object(
                actors, "resolve_actor",
                return_value=(types.SimpleNamespace(canonical_name=SEAT),
                              None)):
            self.assertIsNone(sm.set_mood("flowing", now=NOW - 5 * MIN)[1])
            self.assertIsNone(sm.ask("s-1", now=NOW))

    def test_no_seat_no_ask(self):  # noqa: VACUOUS_ASSERTION — absence is the contract; test_asked_at_most_once_an_hour is the positive control on the same call
        sm = _sm()
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": ""}), \
                self.on_roster():
            self.assertIsNone(sm.ask(None, now=NOW))
        with mock.patch.object(sm, "_roster_key",
                               return_value=(None, "not on the roster")):
            self.assertIsNone(sm.ask("s-1", now=NOW))

    def test_the_ask_rides_inject_once_and_commits_its_latch(self):
        import importlib
        from helm import inject, injection_schema
        sm = _sm()
        _whisper = importlib.import_module("helm.inject._whisper")
        env = {"HELM_ADOPTED_DIR": os.path.join(self.tmp, "adopted"),
               "HELM_CACHE_DIR": os.path.join(self.tmp, "cache"),
               "HELM_SEAT_NAMES": os.path.join(self.tmp, "seat-names.txt")}
        os.makedirs(env["HELM_ADOPTED_DIR"])
        context = (injection_schema.V3, {"harness": "claude",
                                         "session": "s-1"}, {}, None, None)
        with mock.patch.dict(os.environ, env), self.on_roster(), \
                mock.patch.object(_whisper, "_sample_context",
                                  return_value=context):
            said = [inject.gather("carry on with the build", session="s-1")
                    ["reflex"] for _ in range(2)]
        hits = [[ln for ln in lane if ln.startswith("MOOD:")] for lane in said]
        self.assertEqual(len(hits[0]), 1, said)
        self.assertEqual(hits[1], [], said)
        self.assertIsNotNone(sm.asked_at(SEAT))


# ---------------------------------------------------------------- RANK

NOTES = {"source": "synthetic fixture standing in for the burn-down notes",
         "causes": [
             {"cause": "guard:author-gate", "count": 2,
              "example": "note 12: refused on its own lane"},
             {"cause": "the brief lacked the lane path", "count": 4,
              "example": "note 40: asked where the worktree was"}]}


class RankTest(Base):
    def test_rank_merges_the_ledger_and_the_notes(self):
        sm = _sm()
        for ago in (300, 120, 30):
            friction.record("author-gate", "PreToolUse:Bash",
                            now=NOW - ago * MIN)
        friction.record("split-budget", "pre-commit", now=NOW - 60 * MIN)
        friction.record("old-guard", now=NOW - 9 * 86400)      # out of window
        os.makedirs(os.path.dirname(sm.notes_path()), exist_ok=True)
        pk.write_json(sm.notes_path(), NOTES)
        got = sm.rank(days=7, now=NOW)
        self.assertEqual([(c, n) for c, n, _ex in got],
                         [("guard:author-gate", 5),
                          ("the brief lacked the lane path", 4),
                          ("guard:split-budget", 1)])
        self.assertIn(SEAT, got[0][2])
        self.assertEqual(got[1][2], "note 40: asked where the worktree was")

    def test_rank_cli_and_an_unreadable_ledger(self):
        friction.record("author-gate", now=time.time() - MIN)
        rc, out, err = self.run_cli("rank", "--days", "1")
        self.assertEqual(rc, 0, err)
        self.assertIn("guard:author-gate", out)
        rc, out, _err = self.run_cli("rank", "--json")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["ranked"][0][:2],
                         ["guard:author-gate", 1])
        os.remove(friction.path())
        os.makedirs(friction.path())
        rc, _out, err = self.run_cli("rank")
        self.assertEqual(rc, 1)
        self.assertIn("UNREADABLE", err)


# ---------------------------------------------------------------- FLOOR + CLI

class FloorTest(Base):
    def test_floor_measures_every_live_seat_attention_first(self):
        sm, sig = _sm(), _sig()
        raws = {"seat-a": raw(seat="seat-a", card={"id": "c1", "title": "t",
                                               "since": NOW - MIN}),
                "seat-b": raw(seat="seat-b", refusals=[
                    refusal(4), refusal(3), refusal(2), refusal(1)]),
                "seat-c": raw(seat="seat-c")}
        with mock.patch.object(sig, "live_seats",
                               return_value=(["seat-b", "seat-a", "seat-c"],
                                             {}, None)), \
                mock.patch.object(sig, "inputs",
                                  return_value=({k.casefold(): v
                                                 for k, v in raws.items()},
                                                [])):
            got = sm.floor(now=NOW)
            rc, out, err = self.run_cli()
            rc_json, out_json, _ = self.run_cli("--json")
        self.assertEqual([(m["seat"], m["state"]) for m in got],
                         [("seat-a", "blocked-on-owner"), ("seat-b", "stuck"),
                          ("seat-c", "flowing")])
        self.assertEqual(rc, 0, err)
        for name in ("seat-a", "seat-b", "seat-c", "blocked-on-owner"):
            self.assertIn(name, out)
        self.assertEqual(rc_json, 0)
        self.assertEqual(len(json.loads(out_json)), 3)

    def test_an_unreadable_roster_is_never_an_empty_floor(self):
        sig = _sig()
        with mock.patch.object(sig, "live_seats",
                               return_value=(None, None,
                                             "the roster could not be read")):
            rc, _out, err = self.run_cli()
            with self.assertRaises(OSError):
                _sm().floor(now=NOW)
        self.assertEqual(rc, 1)
        self.assertIn("roster could not be read", err)

    def test_one_seat_and_an_unknown_seat(self):
        sm, sig = _sm(), _sig()
        # the verb reads the clock, so this world is built on it
        one = raw(seat="seat-a", progress=(time.time() - 5 * MIN, "a commit"))
        with mock.patch.object(sm, "_roster_key",
                               side_effect=lambda n: (n, None) if n == "seat-a"
                               else (None, "%s is not a seat on the roster"
                                     % n)), \
                mock.patch.object(sig, "inputs", return_value=(
                    {"seat-a": one}, [])):
            rc, out, err = self.run_cli("seat-a")
            rc2, _out2, err2 = self.run_cli("ghost")
        self.assertEqual(rc, 0, err)
        self.assertIn("seat-a", out)
        self.assertIn("flowing", out)
        self.assertEqual(rc2, 1)
        self.assertIn("not a seat on the roster", err2)

    def test_usage_and_unknown_flags(self):
        rc, out, _err = self.run_cli("--help")
        self.assertEqual(rc, 0)
        self.assertIn("helm seat mood set <word>", out)
        rc, _out, err = self.run_cli("rank", "--bogus")
        self.assertEqual(rc, 2, err)


class HostileRosterKeyTest(Base):
    """The display-launder tripwire's stand-in arm for seatmood_signals: a
    roster key carrying a screen-clear and a bidi override, live beside a
    legitimate seat, never reaches the floor, its JSON, or a refusal."""
    ESC, BIDI = "\x1b", "‮"
    HOSTILE = "lane" + ESC + "[2J" + BIDI + "pwn"

    def test_a_hostile_roster_key_never_reaches_the_floor(self):
        from helm import seats
        from helm.seats_roster import seen_path
        sig = _sig()
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": ""}):
            for name, sid in ((self.HOSTILE, "z" * 32), ("seat-a", "y" * 32)):
                seats.write_roster(name, session=sid, cwd=self.tmp)
                with open(seen_path(name), "w"):
                    pass                        # a fresh presence beat
            rows, why = sig.read_roster()
            self.assertIsNone(why)
            self.assertIn(self.HOSTILE, rows, "control: the key is planted")
            with mock.patch.object(sig, "_land_progress",
                                   return_value=({}, None)):
                names, _rows, why = sig.live_seats()
                rc, out, err = self.run_cli()
                rc_json, out_json, err_json = self.run_cli("--json")
                rc_one, out_one, err_one = self.run_cli(self.HOSTILE)
        self.assertIsNone(why)
        self.assertEqual(names, ["seat-a"])
        self.assertEqual((rc, rc_json), (0, 0), err + err_json)
        self.assertIn("seat-a", out)
        self.assertEqual([m["seat"] for m in json.loads(out_json)], ["seat-a"])
        self.assertEqual(rc_one, 2)
        for got in (out, err, out_json, err_json, out_one, err_one):
            self.assertNotIn(self.ESC, got)
            self.assertNotIn(self.BIDI, got)
        self.assertEqual(_sm()._roster_key(self.HOSTILE)[0], None)


if __name__ == "__main__":
    unittest.main()
