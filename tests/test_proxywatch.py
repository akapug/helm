#!/usr/bin/env python3
"""proxywatch — the two rungs nothing was watching, and the change-latch.

THE OWNER'S ASK, on lifting codex's routing bench: "since codex is back in
service, please set a timer to check on it appropriately to make sure that the
CLI proxy fixes for it actually work."

WHAT WAS ALREADY WATCHED: drops, by helm-silent-drop, every 90 seconds. That is
one of three ways the fixes stop working and it was the only one anything
looked at.

  CONFIG  nothing ran the drift census on a schedule. One seat went a WEEK with
          nonstream-keepalive-interval absent for exactly that reason — the
          generator was fixed and nobody re-read the files.
  HANGS   stated plainly when the bench was lifted and still true: a turn that
          never completes writes no transcript row, so the drop watchdog is
          structurally blind to it.

MOST OF THIS SUITE IS ABOUT THE WATCH BEING ABLE TO FAIL, and about it staying
quiet when nothing moved. A watch that speaks every pass gets filtered, and a
filtered alarm is an absent one — so silence has to be meaningful, which means
the latch is as load-bearing as the detection.
"""
import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from helm import proxywatch, seat  # noqa: E402


def row(seat="codex", config_ok=True, drift=(), age=60, live=True,
        alerted=None, hang=False, log=None, log_detail=None,
        turn=None, turn_evidence=None, probe=None, probe_detail=None,
        family="codex", upstream=None, upstream_detail=None,
        upstream_ms=None, upstream_since=None, liveness=None):
    return {"seat": seat, "family": family, "config_ok": config_ok,
            "drift": list(drift), "transcript_age_s": age,
            "pane_live": live, "alerted_at": alerted,
            "hang_candidate": hang, "log": log, "log_detail": log_detail,
            "probe": probe, "probe_detail": probe_detail,
            "turn_state": turn, "turn_evidence": turn_evidence,
            "liveness": liveness,
            "upstream": upstream, "upstream_detail": upstream_detail,
            "upstream_ms": upstream_ms, "upstream_since": upstream_since}


def rep(*rows, upstream=None, proxy_runtime=None):
    return {"ts": 1000, "seats": list(rows), "upstream": upstream or {},
            "proxy_runtime": proxy_runtime or {}}


def runtime_proof(session="session-ds4pro", observed=1000, route=None):
    return {"v": 1, "session": session, "agent_pid": 4101,
            "agent_starttime": 701, "model": (route or {}).get("alias", "ds4-pro"),
            "local_base_url": "http://127.0.0.1:8360",
            "proxy_pid": 4201, "proxy_identity": "proc:702",
            "proxy_config": "/safe/config.yaml", "config_sha256": "a" * 64,
            "route": route or {"alias": "ds4-pro", "provider": "opencode-go",
                                "upstream_model": "deepseek-v4-pro",
                                "base_url": "https://opencode.ai/zen/go/v1"},
            "observed_at": observed,
            "canary": {"state": "HEALTHY", "status": 200}}


# a real starvation line, VERBATIM from a live proxy.log — the parser is
# tested against what the incident actually wrote, not an imagined format.
GIN_402 = ('[2026-07-29 11:20:00] [a2f8ba2a] [warn ] [gin_logger.go:95] 402 '
           '|        1.255s |       127.0.0.1 | POST    '
           '"/v1/messages?beta=true"')


def gin(code, path="/v1/messages?beta=true", method="POST", level="warn ",
        ts="2026-07-29 11:20:00", body=None, truncated=False):
    line = ('[%s] [a2f8ba2a] [%s] [gin_logger.go:95] %d |        1.255s '
            '|       127.0.0.1 | %s    "%s"' % (ts, level, code, method, path))
    if body is not None:
        line += " | response_body=" + json.dumps(body)
        if truncated:
            line += " [truncated]"
    return line


class SeatEnumerationTest(unittest.TestCase):
    def test_EVERY_minted_seat_is_watched_not_just_one_family(self):
        """THE STARVATION INCIDENT, pinned. The watch used to filter this list
        down to codex and its instances — and another family's seat sat INERT
        for TWO DAYS, every request 402ing into its own proxy.log, while
        the timer probed every ~15 minutes one family short. The instances
        stay pinned too: codex-2/codex-3 were the original narrower bug (the
        first live run showed one row where three were expected)."""
        with mock.patch("helm.seat._minted_seats",
                        return_value=[("codex", "codex"), ("codex", "codex-2"),
                                      ("codex", "codex-3"), ("grok", "grok"),
                                      ("kimi", "kimi")]):
            self.assertEqual(proxywatch._watched_seats(),
                             ["codex", "codex-2", "codex-3", "grok", "kimi"])

    def test_it_never_raises_when_the_register_cannot_be_read(self):
        """A watch that dies on a bad read is a watch that reports nothing,
        which reads exactly like healthy."""
        with mock.patch("helm.seat._minted_seats", side_effect=OSError("boom")):
            self.assertEqual(proxywatch._watched_seats(), ["codex"])


class FindingsTest(unittest.TestCase):
    def test_a_healthy_fleet_says_NOTHING(self):
        self.assertEqual(proxywatch.findings(rep(row())), [])

    def test_CONFIG_DRIFT_is_reported_with_what_it_costs(self):
        """The message must name the CONSEQUENCE, not just the key — the whole
        reason ds4pro's week-long absence went unnoticed is that a missing line
        reads like a detail until someone says what it does."""
        f = proxywatch.findings(rep(row(config_ok=False,
                                        drift=["nonstream-keepalive-interval=None want 15"])))
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0][0], "CONFIG")
        self.assertIn("empty HTTP 200", f[0][1])

    def test_a_HANG_CANDIDATE_needs_a_LIVE_pane(self):
        """A seat nobody launched is not hung, it is off. Reporting an idle
        seat as a hang is how a watch earns the filter it then dies behind."""
        self.assertEqual(proxywatch.findings(rep(row(live=False, age=99999))), [])

    def test_a_hang_is_flagged_as_a_QUESTION_not_a_verdict(self):
        """An agent legitimately thinks for a long time. The rung exists
        because the drop watchdog is structurally blind here, not because a
        quiet pane is proof of anything."""
        f = proxywatch.findings(rep(row(hang=True, age=3600)))
        self.assertEqual(f[0][0], "HANG?")
        self.assertIn("Not a verdict", f[0][1])

    def test_a_seat_that_could_not_be_read_is_SKIPPED_not_called_healthy(self):
        r = rep({"seat": "codex-9", "error": "unknown seat"})
        self.assertEqual(proxywatch.findings(r), [])


class BlockedOnHumanVerdictTest(unittest.TestCase):
    """(owner ruling: complying destroys work) A seat frozen at a
    plan-approval prompt has a DEAD turn loop AND is BLOCKED_ON_HUMAN — the
    HUNG branch used to prescribe `helm seat resume`, which DISCARDS the
    pending plan. A live seat sat exactly there for 135 minutes. The
    verdict must consult the pane-tail classifier (seat.seat_liveness, reused
    — never a second pane-tail reader) before prescribing a restart.

    These tests assert the EFFECT, not the absence of a complaint: a blocked
    fixture must yield a verdict that CARRIES the plan path and NO resume
    instruction; an unreadable classifier must degrade the prescription to
    naming the uncertainty rather than asserting resume over an unread
    state."""

    @staticmethod
    def _findings(liveness):
        # findings() is a PURE reduction over the report: the liveness was
        # sampled once in health() and rides the row. No classifier mock —
        # that would re-test the impurity a cross-family review caught.
        r = rep(row(seat="codex-2", turn="hung", liveness=liveness,
                    turn_evidence="semantic entry stale 57m"))
        return proxywatch.findings(r)

    def test_a_provider_WALL_suppresses_the_destructive_restart(self):
        liv = {"state": "WALLED",
               "blocked_on": "upstream RATE-LIMITED since T",
               "evidence": "pane-tail+proxywatch"}
        f = self._findings(liv)
        self.assertEqual(f[0][0], "WALLED")
        self.assertIn("RATE-LIMITED", f[0][1])
        self.assertIn("does not repair", f[0][1])
        self.assertNotIn("`helm seat resume", f[0][1])

    def test_blocked_on_human_carries_the_plan_path_and_no_resume(self):
        liv = {"state": "BLOCKED_ON_HUMAN",
               "blocked_on": "~/.helm/_global/seats/codex-2/claude/plans/x.md",
               "evidence": "pane-tail"}
        f = self._findings(liv)
        self.assertEqual(f[0][0], "BLOCKED-HUMAN")
        text = f[0][1]
        # the WHERE: the reader can approve without opening the pane by hand
        self.assertIn("plans/x.md", text)
        # the EFFECT that matters: no restart INSTRUCTION survives. (The
        # words "resume/reseed" DO appear — inside the warning that they
        # would destroy the work, which is exactly what the reader must
        # hear; the assertion is that they are never IMPERATIVE.)
        self.assertNotIn("`helm seat resume", text)      # the backticked command
        self.assertNotIn("Action:", text)                # the old prescription lead
        # and it says resume/reseed would destroy the work, by name
        self.assertIn("DISCARD THE PENDING PLAN", text)

    def test_blocked_on_human_without_a_path_still_names_the_prompt(self):
        liv = {"state": "BLOCKED_ON_HUMAN",
               "blocked_on": "do you want to proceed",
               "evidence": "pane-tail"}
        f = self._findings(liv)
        self.assertEqual(f[0][0], "BLOCKED-HUMAN")
        self.assertNotIn("helm seat resume", f[0][1])

    def test_an_unreadable_classifier_degrades_the_prescription(self):
        """HUNG may stand (the fuse composed), but the verdict must name the
        blindness and the read-first gate with NO executable resume command —
        a classifier that could not see the seat cannot clear it of being
        human-blocked, so the restart prescription does not go out (a
        cross-family review: UNKNOWN-with-resume-attached is
        HUNG-with-resume)."""
        liv = {"state": "UNKNOWN", "blocked_on": None,
               "evidence": "read-failed"}
        f = self._findings(liv)
        self.assertEqual(f[0][0], "HUNG")
        text = f[0][1]
        self.assertIn("could not confirm", text)
        self.assertIn("READ THE PANE FIRST", text)
        self.assertNotIn("helm seat resume", text)   # no executable prescription

    def test_findings_is_a_PURE_reduction_over_one_immutable_report(self):
        """A cross-family review HIGH: findings() calling seat_liveness live
        made the verdict depend on WHEN it was asked — one immutable report returned
        HUNG on one render and BLOCKED-HUMAN on the next. The classifier is
        sampled ONCE in health() and rides the row; two renders of the same
        report must agree, and the classifier is never re-consulted."""
        liv = {"state": "BLOCKED_ON_HUMAN",
               "blocked_on": "~/plans/x.md", "evidence": "pane-tail"}
        r = rep(row(seat="codex-2", turn="hung", liveness=liv,
                    turn_evidence="stale"))
        with mock.patch("helm.seat.seat_liveness",
                        side_effect=AssertionError("findings re-read the pane")):
            first = proxywatch.findings(r)
            second = proxywatch.findings(r)
        self.assertEqual(first, second)
        self.assertEqual(first[0][0], "BLOCKED-HUMAN")

    def test_a_liveness_transition_is_a_fingerprint_MOVE(self):
        """The verdict-bearing liveness state rides the fingerprint, so a
        real IDLE->BLOCKED_ON_HUMAN transition on the same hung seat speaks
        once (the state moved), rather than being invisible because the
        watched fingerprint did not change."""
        base = dict(seat="codex-2", turn="hung", turn_evidence="stale")
        fp_blocked = proxywatch.fingerprint(rep(row(
            liveness={"state": "BLOCKED_ON_HUMAN"}, **base)))
        fp_idle = proxywatch.fingerprint(rep(row(
            liveness={"state": "IDLE"}, **base)))
        fp_unknown = proxywatch.fingerprint(rep(row(
            liveness={"state": "UNKNOWN"}, **base)))
        self.assertNotEqual(fp_blocked, fp_idle)
        self.assertNotEqual(fp_blocked, fp_unknown)
        self.assertNotEqual(fp_idle, fp_unknown)

    def test_process_only_LIVE_gets_the_read_first_gate_not_a_resume(self):
        """A cross-family review blocker: LIVE is process evidence only — a named
        process exists, no pane tail was read, so nothing cleared this seat of
        being human-blocked. Admitting LIVE to the vocabulary without this arm
        sent it to the destructive else (resume prescribed) — the exact
        weak-evidence->strong-action promotion the lane removes upstream."""
        liv = {"state": "LIVE", "blocked_on": None,
               "evidence": "seat is named by 1 live claude process (pid 7)"}
        f = self._findings(liv)
        self.assertEqual(f[0][0], "HUNG")
        text = f[0][1]
        self.assertIn("PROCESS-ONLY", text)
        self.assertIn("READ THE PANE FIRST", text)
        self.assertNotIn("helm seat resume", text)   # no executable prescription

    def test_a_genuinely_hung_seat_keeps_the_resume_prescription(self):
        """The control: a seat the classifier clears of being human-blocked
        (RUNNING/IDLE/other) is hung in the old sense, and the original
        resume/reseed prescription is the correct one."""
        liv = {"state": "IDLE", "blocked_on": None, "evidence": "pane-tail"}
        f = self._findings(liv)
        self.assertEqual(f[0][0], "HUNG")
        self.assertIn("helm seat resume", f[0][1])


class _HealthRig(unittest.TestCase):
    """Shared rig: run the REAL health() against a tmpdir seat, hermetically.

    The endpoint probe and fuse readers
    (_inflight_for/_ctx_pct/_spawn_age_s/_compact_threshold) are mocked here
    with caller-chosen values — never left to read a live proxy, socket table,
    or seat spawn register from inside a test.
    """

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="helm-test-pwh-")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _health(self, live, age, infl=None, pct=None, spawn=None,
                include_upstream=False, upstream=None, complete=True,
                pending=False, write_age=None, work=1, seat_name="codex",
                runtime_roster=None, parsed_family=("codex", None)):
        """Let health() DECIDE. An earlier draft recomputed hang_candidate in
        this helper and asserted against its own arithmetic — a test that
        cannot fail, which is why the mutation kept passing. A real transcript
        with a back-dated SEMANTIC record makes the module do the work: `age`
        dates the last completed assistant entry, `write_age` (default: age)
        back-dates the file mtime separately so retry churn is expressible.

        `live` is THREE-VALUED, exactly as the census is: True (a readable
        process names the seat), False (helm read every claude on the host and
        none does), None (helm could not read them all, so it cannot say).
        """
        tp = os.path.join(self.d, "t.jsonl")
        semantic_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                    time.gmtime(time.time() - age))
        assistant = {"type": "assistant", "timestamp": semantic_ts,
                     "message": {"role": "assistant",
                                 "stop_reason":
                                     "end_turn" if complete else "tool_use",
                                 "content": [{"type": "text", "text": "done"}]}}
        with open(tp, "w") as f:
            f.write(json.dumps(assistant) + "\n")
            if pending:
                f.write(json.dumps({"type": "queue-operation",
                                    "timestamp": "2026-07-30T15:01:35.110Z",
                                    "operation": "enqueue"}) + "\n")
        stamp = time.time() - (age if write_age is None else write_age)
        os.utime(tp, (stamp, stamp))
        with mock.patch.object(proxywatch, "_watched_seats", return_value=[seat_name]), \
                mock.patch.object(proxywatch, "probe",
                                  return_value=("healthy", "HTTP 401 fixture", 1)), \
                mock.patch.object(
                    proxywatch, "_live_seats",
                    return_value=({seat_name} if live else set(),
                                  None if live is not None else
                                  "1 live claude process could not be "
                                  "identified (pid 303)")), \
                mock.patch("helm.seat._seat_family", return_value=parsed_family), \
                mock.patch("helm.seat._proxy_home", return_value=self.d), \
                mock.patch("helm.seat._instance_dir", return_value=self.d), \
                mock.patch("helm.autocompact._newest_transcript", return_value=tp), \
                mock.patch("helm.pk.read_json", return_value={}), \
                mock.patch("helm.seats.roster",
                           return_value=runtime_roster or {}), \
                mock.patch.object(proxywatch, "_inflight_for",
                                  return_value=infl) as self.inflight_reader, \
                mock.patch.object(proxywatch, "_ctx_pct", return_value=pct), \
                mock.patch.object(proxywatch, "_spawn_age_s",
                                  return_value=spawn), \
                mock.patch.object(proxywatch, "_compact_threshold",
                                  return_value=90), \
                mock.patch.object(
                    proxywatch, "_open_work",
                    return_value=None if work is None else {"codex": work}
                ) as self.openwork_reader, \
                mock.patch.object(proxywatch, "upstream_health",
                                  return_value=upstream or {}) as self.upstream_reader:
            return proxywatch.health(include_upstream=include_upstream)["seats"][0]


class TranscriptRealityTest(unittest.TestCase):
    """The semantic reader — progress is a COMPLETED main-chain entry, and
    file mtime survives only as raw-write age. Fixtures are shaped like the
    live retry-churn victim's transcript, not an imagined schema."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="helm-test-pwtr-")
        self.p = os.path.join(self.d, "session.jsonl")
        self.now = time.time()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _write(self, *records, write_age=0):
        with open(self.p, "w") as f:
            for record in records:
                f.write(json.dumps(record) + "\n")
        stamp = self.now - write_age
        os.utime(self.p, (stamp, stamp))

    def _ts(self, age):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                             time.gmtime(self.now - age))

    def test_65min_queue_churn_does_not_refresh_semantic_age(self):
        """MUST-HIT fixture from the live victim: tool completion at T-65m,
        then only user/queue retry bookkeeping while mtime remains fresh."""
        self._write(
            {"type": "assistant", "timestamp": self._ts(65 * 60),
             "message": {"role": "assistant", "stop_reason": "tool_use",
                         "content": [{"type": "tool_use", "name": "Bash"}]}},
            {"type": "user", "timestamp": self._ts(65 * 60),
             "message": {"role": "user",
                         "content": [{"type": "tool_result", "content": "ok"}]}},
            {"type": "queue-operation", "timestamp": self._ts(1),
             "operation": "enqueue"}, write_age=1)
        got = proxywatch.transcript_reality(self.p, now=self.now)
        self.assertEqual(got["semantic_kind"], "tool-result")
        self.assertAlmostEqual(got["semantic_age_s"], 65 * 60, delta=1)
        self.assertAlmostEqual(got["write_age_s"], 1, delta=0.1)
        self.assertTrue(got["pending_after"])

    def test_user_prompt_and_queue_rows_are_writes_not_completed_progress(self):
        self._write(
            {"type": "assistant", "timestamp": self._ts(120),
             "message": {"role": "assistant", "stop_reason": "end_turn",
                         "content": [{"type": "text", "text": "done"}]}},
            {"type": "queue-operation", "timestamp": self._ts(2),
             "operation": "dequeue"},
            {"type": "user", "timestamp": self._ts(2),
             "message": {"role": "user",
                         "content": [{"type": "text", "text": "continue"}]}},
            write_age=1)
        got = proxywatch.transcript_reality(self.p, now=self.now)
        self.assertAlmostEqual(got["semantic_age_s"], 120, delta=1)
        self.assertTrue(got["pending_after"])

    def test_queue_clearing_after_completed_turn_is_nothing_pending(self):
        self._write(
            {"type": "assistant", "timestamp": self._ts(3600),
             "message": {"role": "assistant", "stop_reason": "end_turn",
                         "content": [{"type": "text", "text": "done"}]}},
            {"type": "queue-operation", "timestamp": self._ts(3),
             "operation": "enqueue"},
            {"type": "queue-operation", "timestamp": self._ts(2),
             "operation": "dequeue"}, write_age=1)
        got = proxywatch.transcript_reality(self.p, now=self.now)
        self.assertEqual(got["semantic_kind"], "assistant")
        self.assertFalse(got["pending_after"])

    def test_popAll_clears_every_pending_queue_item(self):
        self._write(
            {"type": "assistant", "timestamp": self._ts(3600),
             "message": {"role": "assistant", "stop_reason": "end_turn",
                         "content": [{"type": "text", "text": "done"}]}},
            {"type": "queue-operation", "timestamp": self._ts(4),
             "operation": "enqueue"},
            {"type": "queue-operation", "timestamp": self._ts(3),
             "operation": "enqueue"},
            {"type": "queue-operation", "timestamp": self._ts(2),
             "operation": "popAll"}, write_age=1)
        got = proxywatch.transcript_reality(self.p, now=self.now)
        self.assertEqual(got["semantic_kind"], "assistant")
        self.assertFalse(got["pending_after"])

    def test_reverse_scan_crosses_many_chunks_until_semantic_truth(self):
        semantic = {"type": "assistant", "timestamp": self._ts(3600),
                    "message": {"role": "assistant", "stop_reason": "end_turn",
                                "content": [{"type": "text", "text": "done"}]}}
        noise = [{"type": "queue-operation", "timestamp": self._ts(1),
                  "operation": "enqueue", "padding": "x" * 2000}
                 for _ in range(80)]
        self._write(semantic, *noise, write_age=1)
        got = proxywatch.transcript_reality(self.p, now=self.now)
        self.assertAlmostEqual(got["semantic_age_s"], 3600, delta=1)
        self.assertTrue(got["pending_after"])

    def test_a_sidechain_entry_is_not_main_chain_progress(self):  # noqa: VACUOUS_ASSERTION — the 3600s reading IS the positive control: an unfiltered sidechain would measure ~5s
        self._write(
            {"type": "assistant", "timestamp": self._ts(3600),
             "message": {"role": "assistant", "stop_reason": "end_turn",
                         "content": [{"type": "text", "text": "done"}]}},
            {"type": "assistant", "isSidechain": True,
             "timestamp": self._ts(5),
             "message": {"role": "assistant", "stop_reason": "end_turn",
                         "content": [{"type": "text", "text": "sub"}]}},
            write_age=1)
        got = proxywatch.transcript_reality(self.p, now=self.now)
        self.assertAlmostEqual(got["semantic_age_s"], 3600, delta=1)

    def test_an_unreadable_transcript_is_all_None_never_a_measurement(self):  # noqa: VACUOUS_ASSERTION — carries its own unconditional positive control: the same reader measures the same fields once the file exists
        got = proxywatch.transcript_reality(os.path.join(self.d, "absent"),
                                            now=self.now)
        self.assertIsNone(got["semantic_age_s"])
        self.assertIsNone(got["write_age_s"])
        self.assertIsNone(got["pending_after"])
        # positive control on the same observables: once a file exists the
        # same reader MEASURES, so the Nones above were blindness, not habit
        self._write(
            {"type": "assistant", "timestamp": self._ts(60),
             "message": {"role": "assistant", "stop_reason": "end_turn",
                         "content": [{"type": "text", "text": "done"}]}},
            write_age=3)
        got = proxywatch.transcript_reality(self.p, now=self.now)
        self.assertAlmostEqual(got["write_age_s"], 3, delta=0.1)
        self.assertAlmostEqual(got["semantic_age_s"], 60, delta=1)
        self.assertFalse(got["pending_after"])

    def test_a_readable_file_with_no_semantic_record_keeps_write_age(self):
        self._write({"type": "queue-operation", "timestamp": self._ts(4),
                     "operation": "enqueue"}, write_age=7)
        got = proxywatch.transcript_reality(self.p, now=self.now)
        self.assertIsNone(got["semantic_age_s"])
        self.assertAlmostEqual(got["write_age_s"], 7, delta=0.1)
        self.assertTrue(got["pending_after"])


class HealthDerivationTest(_HealthRig):
    """health() — where hang_candidate is DERIVED, not where it is reported.

    Added because a mutation did not bite: flagging a hang without requiring a
    live pane passed all 21 tests. FindingsTest feeds health() a pre-built row
    whose hang_candidate is already decided, so nothing exercised the line that
    decides it. Three times in one review cycle the same gap appeared — testing
    the reporting layer and leaving the derivation uncovered — which is its own
    lesson about where tests naturally land.
    """

    def test_a_DEAD_pane_is_never_a_hang_however_stale(self):
        """A seat nobody launched is not hung, it is off. One dead seat's
        transcript measured 8795 minutes old live, and it was simply not
        running."""
        row = self._health(live=False, age=999999)
        self.assertFalse(row["pane_live"])
        self.assertFalse(row["hang_candidate"])

    def test_a_LIVE_pane_past_the_window_IS_a_hang_candidate(self):
        row = self._health(live=True, age=proxywatch.HANG_S + 60)
        self.assertTrue(row["pane_live"])
        self.assertTrue(row["hang_candidate"])

    def test_a_LIVE_pane_INSIDE_the_window_is_not(self):
        row = self._health(live=True, age=60)
        self.assertFalse(row["hang_candidate"])

    def test_pane_liveness_comes_from_the_PROCESS_census(self):
        """Not from the roster, which lags — live measurement found two seats
        with live processes and absent roster rows. A hang check keyed on the
        roster would have called two running seats dead."""
        import inspect
        src = inspect.getsource(proxywatch._live_seats)
        self.assertIn("claude_processes", src)

    def test_the_log_rung_is_DERIVED_from_the_seats_own_proxy_log(self):
        """health() must wire logscan to the seat's proxy home — this class's
        own lesson, third time learned: testing the reporting layer and
        leaving the derivation uncovered is where mutations hide."""
        with open(os.path.join(self.d, "proxy.log"), "w") as f:
            f.write("\n".join((
                gin(401, path="/v1/chat/completions?helm_canary=1",
                    ts="2026-07-29 11:16:00"),
                gin(401, path="/v1/chat/completions",
                    ts="2026-07-29 11:17:00"),
                gin(402, ts="2026-07-29 11:18:00"),
                gin(402, ts="2026-07-29 11:19:00"), GIN_402)) + "\n")
        row = self._health(live=True, age=60)
        self.assertEqual(row["log"], "streak")
        self.assertIn("402 x3", row["log_detail"])
        self.assertEqual(row["log_status_401"]["helm_marked"], 1)
        self.assertEqual(row["log_status_401"]["unmarked"], 1)
        self.assertFalse(row["log_status_401"]["scope"]["truncated"])

    def test_a_seat_with_NO_proxy_log_derives_unknown(self):
        row = self._health(live=True, age=60)
        self.assertEqual(row["log"], "unknown")

    def test_upstream_projection_runs_AFTER_and_does_not_replace_turn_reality(self):
        family = {"codex": {"state": "HEALTHY", "detail": "current detail",
                             "ms": 7, "since": "2026-07-31T00:00:00Z"}}
        row = self._health(live=None, age=proxywatch.HANG_S + 60,
                           include_upstream=True, upstream=family)
        self.assertIsNone(row["pane_live"])
        self.assertEqual(row["turn_state"], "hung-unknown")
        self.assertEqual(row["upstream"], "HEALTHY")
        self.assertEqual(row["upstream_detail"], "current detail")
        self.assertEqual(row["upstream_ms"], 7)
        self.assertEqual(row["upstream_since"], "2026-07-31T00:00:00Z")
        self.upstream_reader.assert_called_once()

    def test_verified_runtime_family_reaches_the_canary_group(self):
        runtime = {"pi-codex": {"runtime": {"family": "codex",
                                                "agent_harness": "pi"},
                                "runtime_verified": True}}
        row = self._health(
            live=True, age=60, seat_name="pi-codex", include_upstream=True,
            runtime_roster=runtime, parsed_family=(None, "unknown display"),
            upstream={"codex": {"state": "HEALTHY"}})
        self.assertEqual(row["family"], "codex")
        passed = self.upstream_reader.call_args.args[0]
        self.assertEqual(passed[0]["seat"], "pi-codex")
        self.assertEqual(passed[0]["family"], "codex")

    def test_local_only_health_never_calls_the_authenticated_rung(self):
        self._health(live=True, age=60, include_upstream=False)
        self.upstream_reader.assert_not_called()


class TurnStateFuseTest(unittest.TestCase):
    """The HUNG fuse — the named verdict no single probe could give.

    Measured live TWICE: the primary codex seat hung, pane LIVE,
    turn loop DEAD. proxywatch said pane=live+transcript-stale (honest, not a
    verdict); the drop watchdog was structurally blind (a turn that never
    completes writes no row to count); roster said amber; chat said quiet.
    Every probe was RIGHT about its own question and the fleet still could not
    name the state — a human had to hand-count transcript rows. Bug class:
    watchdogs-correct-composition-holed.

    These tests drive the pure fuse BOTH directions: HUNG fires only when
    every rung composes, and each benign or blind case gets its own distinct
    verdict — never collapsed, never guessed.
    """

    @staticmethod
    def fuse(live=True, age=proxywatch.HANG_S + 60, log="ok", infl=0,
             pct=34.2, thr=90, spawn=2 * 3600, reality=None, work=1):
        """Defaults are the FULLY COMPOSED hung shape (one open dispatch is
        the pending work); each test bends one rung and asserts the verdict
        moves to that rung's name."""
        return proxywatch.turn_state(live, age, log, infl, pct, thr, spawn,
                                     reality=reality, open_dispatches=work)

    def test_HUNG_fires_when_every_rung_composes(self):
        state, ev = self.fuse()
        self.assertEqual(state, "hung")
        # the evidence must carry the composed signals, not just the name
        self.assertIn("0 in-flight", ev)
        self.assertIn("stale", ev)
        self.assertIn("34.2%", ev)
        self.assertIn("not fresh", ev)

    def test_an_open_request_is_THINKING_never_hung(self):
        """The rung that kills the false alarm: a long legitimate generation
        holds an ESTABLISHED connection for its whole life, and an open
        request IS the turn running. Calling it hung would page the owner on
        every 50-minute think."""
        state, ev = self.fuse(infl=2)
        self.assertEqual(state, "thinking")
        self.assertIn("2 in-flight", ev)

    def test_an_unreadable_socket_census_is_HUNG_UNKNOWN_never_hung(self):
        """THE LOAD-BEARING LAW, same as logscan's: a check that cannot see a
        case returns UNKNOWN, never a false verdict. None is 'blind', 0 is a
        measurement — collapsing them composes a false HUNG out of blindness."""
        state, ev = self.fuse(infl=None)
        self.assertEqual(state, "hung-unknown")
        self.assertIn("socket census", ev)

    def test_unreadable_context_is_HUNG_UNKNOWN(self):
        state, ev = self.fuse(pct=None)
        self.assertEqual(state, "hung-unknown")
        self.assertIn("context%", ev)

    def test_unreadable_spawn_age_is_HUNG_UNKNOWN(self):
        state, ev = self.fuse(spawn=None)
        self.assertEqual(state, "hung-unknown")
        self.assertIn("spawn age", ev)

    def test_a_fresh_spawn_is_FRESH_not_hung(self):
        """`helm seat resume` relaunches onto an OLD transcript, so the stale
        gate passes from second one — only the spawn register knows the seat
        is starting, not hung. The live incident ended in exactly this shape:
        a reseed whose first minutes must not re-page the owner."""
        state, ev = self.fuse(spawn=120)
        self.assertEqual(state, "fresh")
        self.assertIn("starting", ev)

    def test_the_fresh_window_has_an_edge_and_hung_resumes_past_it(self):
        state, _ = self.fuse(spawn=proxywatch.SPAWN_FRESH_S)
        self.assertEqual(state, "fresh")
        state, _ = self.fuse(spawn=proxywatch.SPAWN_FRESH_S + 1)
        self.assertEqual(state, "hung")

    def test_at_the_compact_bar_is_COMPACT_NEEDED(self):
        """The known-benign class: a seat wedged at the compact threshold is
        autocompact's to fix, and naming it HUNG would send a resume at a seat
        that needs a /compact."""
        state, ev = self.fuse(pct=93.0)
        self.assertEqual(state, "compact-needed")
        self.assertIn("93.0%", ev)

    def test_a_starved_seat_reads_STARVED_never_hung(self):
        """The vocabulary stays distinct: the log rung OWNS the starvation
        class (the two-day 402 wall), and the fuse defers to it rather than
        re-diagnosing a refused seat as a hung one."""
        state, ev = self.fuse(log="streak")
        self.assertEqual(state, "starved")
        self.assertIn("STARVED", ev)

    def test_a_dead_pane_is_OFF_however_composed_the_rest_looks(self):
        state, _ = self.fuse(live=False)
        self.assertEqual(state, "off")

    def test_a_recent_turn_is_ok_and_reads_nothing_else(self):
        state, _ = self.fuse(age=60, infl=None, pct=None, spawn=None)
        self.assertEqual(state, "ok")

    def test_no_transcript_on_a_young_process_is_fresh(self):
        """A seat that has never turned has no transcript to date — on a young
        process that is starting, not unknown."""
        state, _ = self.fuse(age=None, spawn=120)
        self.assertEqual(state, "fresh")

    def test_no_transcript_on_an_old_process_is_HUNG_UNKNOWN_not_hung(self):
        """Stale-ness itself is unmeasurable without a dated semantic entry,
        and HUNG requires the measurement — an undated seat can never compose
        it."""
        state, ev = self.fuse(age=None, spawn=2 * 3600)
        self.assertEqual(state, "hung-unknown")
        self.assertIn("semantic transcript age", ev)

    def test_a_seat_OUT_OF_WORK_is_IDLE_not_hung(self):
        """The idle question's negative arm: an old transcript on a seat with
        NOTHING pending is idleness. The hang remedy (resume/reseed) aimed at
        an idle seat kills a healthy pane."""
        state, ev = self.fuse(work=0)
        self.assertEqual(state, "idle")
        self.assertIn("out of work", ev)

    def test_a_seat_WITH_open_work_and_a_stale_transcript_IS_hung(self):
        """The positive control on the same switch (the idle question's other
        arm): the identical stale shape with open dispatch rows addressed to
        the seat composes HUNG, and the evidence carries the count."""
        state, ev = self.fuse(work=2)
        self.assertEqual(state, "hung")
        self.assertIn("2 open dispatches", ev)

    def test_an_unreadable_dispatch_ledger_is_HUNG_UNKNOWN_never_idle(self):
        """The tri-state law at the new input: a ledger helm could not read
        must not measure as an empty board (false IDLE) nor as work (false
        HUNG)."""
        state, ev = self.fuse(work=None)
        self.assertEqual(state, "hung-unknown")
        self.assertIn("open-dispatch census", ev)

    def test_retry_churn_with_a_held_socket_is_HUNG_not_thinking(self):
        """THE RETRY-CHURN MUST-HIT: the live victim held a socket for 65
        minutes while queue-operation retries kept mtime fresh and no semantic
        entry completed. A held socket is pending work, never proof of
        progress."""
        reality = {"semantic_age_s": 65 * 60, "write_age_s": 30,
                   "turn_complete": False, "pending_after": True,
                   "newest_type": "queue-operation"}
        state, ev = self.fuse(age=65 * 60, infl=2, reality=reality)
        self.assertEqual(state, "hung")
        self.assertIn("churn", ev)
        self.assertIn("queue/retry", ev)

    def test_a_silent_long_generation_keeps_THINKING_with_reality_present(self):
        """The false alarm the in-flight rung exists to kill, preserved under
        turn reality: nothing written since the prompt + a held socket is a
        long legitimate generation."""
        reality = {"semantic_age_s": 65 * 60, "write_age_s": 65 * 60,
                   "turn_complete": False, "pending_after": True,
                   "newest_type": "user"}
        state, ev = self.fuse(age=65 * 60, infl=1, reality=reality)
        self.assertEqual(state, "thinking")
        self.assertIn("long generation", ev)

    def test_a_held_socket_with_unreadable_write_age_is_HUNG_UNKNOWN(self):
        reality = {"semantic_age_s": 65 * 60, "write_age_s": None,
                   "turn_complete": False, "pending_after": True}
        state, ev = self.fuse(age=65 * 60, infl=1, reality=reality)
        self.assertEqual(state, "hung-unknown")
        self.assertIn("raw-write age", ev)

    def test_a_queued_prompt_after_a_completed_turn_is_not_idle(self):
        """Transcript-pending work counts as open work even with zero
        dispatch rows: a queued owner prompt the seat never picked up is the
        hang shape, not idleness."""
        reality = {"semantic_age_s": proxywatch.HANG_S + 60, "write_age_s": 30,
                   "turn_complete": True, "pending_after": True}
        state, ev = self.fuse(reality=reality, work=0)
        self.assertEqual(state, "hung")
        self.assertIn("queued transcript work", ev)

    def test_an_open_turn_after_tool_use_is_not_idle(self):
        reality = {"semantic_age_s": proxywatch.HANG_S + 60, "write_age_s": 30,
                   "turn_complete": False, "pending_after": False}
        state, ev = self.fuse(reality=reality, work=0)
        self.assertEqual(state, "hung")
        self.assertIn("open turn", ev)

    def test_a_completed_turn_with_nothing_pending_is_IDLE_even_when_old(self):
        reality = {"semantic_age_s": proxywatch.HANG_S + 60, "write_age_s": 30,
                   "turn_complete": True, "pending_after": False}
        state, ev = self.fuse(reality=reality, work=0)
        self.assertEqual(state, "idle")
        self.assertIn("nothing queued", ev)

    def test_an_unreadable_turn_completion_is_HUNG_UNKNOWN(self):
        """reality's own fields keep the tri-state discipline: a readable file
        whose completion state could not be derived is blindness, not an
        empty pending census."""
        reality = {"semantic_age_s": proxywatch.HANG_S + 60, "write_age_s": 30,
                   "turn_complete": None, "pending_after": False}
        state, ev = self.fuse(reality=reality, work=0)
        self.assertEqual(state, "hung-unknown")
        self.assertIn("turn completion", ev)


class InflightTest(unittest.TestCase):
    """The socket census — how 'in-flight' is actually read, and its honesty.

    NOT from proxy.log: the gin logger writes ONE line per request, at
    COMPLETION, with its duration — an open/streaming request writes NOTHING
    until it finishes, so log-silence cannot distinguish a 90-minute
    generation from a dead turn loop. The kernel socket table can: each
    accepted client connection is one ESTABLISHED row whose LOCAL port is the
    proxy's. Measured live across all five seats of a fleet: the mid-work seat
    held 5, two seats 1 each, and the two idle seats exactly 0.

    Fixtures are byte-shaped like real /proc/net/tcp rows; no live table is
    ever read from a test.
    """

    HDR = ("  sl  local_address rem_address   st tx_queue rx_queue tr "
           "tm->when retrnsmt   uid  timeout inode")
    PORT = 0x207D                            # 8317, codex's — shape realism

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="helm-test-pwi-")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    @staticmethod
    def _v4(lport, st, rport=0xD4A2):
        return ("   1: 0100007F:%04X 0100007F:%04X %s 00000000:00000000 "
                "00:00000000 00000000  1000        0 12345 1 "
                "0000000000000000 20 4 30 10 -1" % (lport, rport, st))

    def _table(self, name, *rows):
        p = os.path.join(self.d, name)
        with open(p, "w") as f:
            f.write("\n".join((self.HDR,) + rows) + "\n")
        return p

    def test_counts_only_ESTABLISHED_rows_whose_LOCAL_port_is_the_proxys(self):
        """One localhost connection appears TWICE in the table — once from
        each end. Counting the proxy-side rows (local port = listen port)
        counts each connection once; the client-side twin, the LISTEN row
        (state 0A), and other ports' traffic are all not it."""
        t = self._table("tcp",
                        self._v4(self.PORT, "0A", rport=0),      # the listener
                        self._v4(self.PORT, "01"),               # proxy side
                        self._v4(0xD4A2, "01", rport=self.PORT),  # its twin
                        self._v4(0x2082, "01"))                  # another port
        self.assertEqual(proxywatch.inflight(8317, tables=(t,)), 1)

    def test_zero_is_a_MEASUREMENT_true_silence_not_blindness(self):
        """The reading the fuse needs: a readable table with no established
        rows is affirmative silence — the hung-composable condition."""
        t = self._table("tcp", self._v4(self.PORT, "0A", rport=0))
        self.assertEqual(proxywatch.inflight(8317, tables=(t,)), 0)

    def test_an_unreadable_table_is_None_never_zero(self):
        """A census that reports silence when it is blind is how a false HUNG
        gets composed — the same unknown-stays-unknown law as logscan."""
        gone = os.path.join(self.d, "absent")
        self.assertIsNone(proxywatch.inflight(
            8317, tables=(gone, gone + "6")))

    def test_one_readable_table_is_enough_to_measure(self):
        t = self._table("tcp6",
                        "   0: 00000000000000000000000001000000:207D "
                        "00000000000000000000000001000000:D4A2 01 "
                        "00000000:00000000 00:00000000 00000000  1000        "
                        "0 12345 1 0000000000000000 20 4 30 10 -1")
        self.assertEqual(proxywatch.inflight(
            8317, tables=(os.path.join(self.d, "absent"), t)), 1)


class HungVerdictDerivationTest(_HealthRig):
    """health() — where the fused verdict is DERIVED, not where it is reported.

    The same lesson HealthDerivationTest carries (its third instance):
    testing the reporting layer and leaving the derivation uncovered is where
    mutations hide. These run the REAL health() and let it wire the readers
    into the fuse.
    """

    STALE = proxywatch.HANG_S + 60

    def test_health_DERIVES_hung_when_every_signal_composes(self):
        row = self._health(live=True, age=self.STALE, infl=0, pct=30.0,
                           spawn=2 * 3600)
        self.assertEqual(row["turn_state"], "hung")
        self.assertIn("0 in-flight", row["turn_evidence"])
        self.assertTrue(row["hang_candidate"])

    def test_an_open_request_derives_THINKING_not_hung(self):
        row = self._health(live=True, age=self.STALE, infl=1, pct=30.0,
                           spawn=2 * 3600)
        self.assertEqual(row["turn_state"], "thinking")

    def test_an_unreadable_census_derives_HUNG_UNKNOWN(self):
        row = self._health(live=True, age=self.STALE, infl=None, pct=30.0,
                           spawn=2 * 3600)
        self.assertEqual(row["turn_state"], "hung-unknown")
        self.assertIn("socket census", row["turn_evidence"])

    def test_a_fresh_spawn_derives_fresh_not_hung(self):
        row = self._health(live=True, age=self.STALE, infl=0, pct=30.0,
                           spawn=60)
        self.assertEqual(row["turn_state"], "fresh")

    def test_at_the_compact_bar_derives_compact_needed(self):
        row = self._health(live=True, age=self.STALE, infl=0, pct=95.0,
                           spawn=2 * 3600)
        self.assertEqual(row["turn_state"], "compact-needed")

    def test_STARVED_still_reads_starved_with_the_fuse_in_place(self):
        """The no-regression direction: a refusal streak in the seat's own
        proxy.log must still read STARVED — from the log rung, verbatim — and
        the fuse must defer to it, not re-verdict the seat as hung."""
        with open(os.path.join(self.d, "proxy.log"), "w") as f:
            f.write("\n".join((gin(402, ts="2026-07-29 11:18:00"),
                                gin(402, ts="2026-07-29 11:19:00"),
                                GIN_402)) + "\n")
        row = self._health(live=True, age=self.STALE, infl=0, pct=30.0,
                           spawn=2 * 3600)
        self.assertEqual(row["log"], "streak")
        self.assertEqual(row["turn_state"], "starved")
        f = proxywatch.findings(rep(row))
        self.assertEqual([lvl for lvl, _t in f], ["STARVED"])

    def test_the_fuse_readers_are_LAZY_a_recent_turn_reads_nothing(self):  # noqa: VACUOUS_ASSERTION — turn_state=="ok" is the positive control; the not-called arms are the laziness contract, and sibling derivation tests prove the same readers fire for candidates
        """A fleet that turned recently pays nothing new: the socket census
        never runs for a seat whose answer cannot change the verdict, and the
        dispatch ledger is never folded for it either."""
        row = self._health(live=True, age=60, infl=0, pct=30.0, spawn=3600)
        self.assertEqual(row["turn_state"], "ok")
        self.inflight_reader.assert_not_called()
        self.openwork_reader.assert_not_called()

    def test_a_dead_pane_derives_off(self):
        row = self._health(live=False, age=self.STALE)
        self.assertEqual(row["turn_state"], "off")

    def test_an_old_completed_turn_with_NO_open_work_derives_IDLE(self):
        """The idle question's negative arm at the derivation layer: stale
        transcript, live pane, everything readable, zero pending anywhere —
        IDLE, and the hang-candidate flag does not survive the verdict."""
        row = self._health(live=True, age=self.STALE, infl=0, pct=30.0,
                           spawn=2 * 3600, work=0)
        self.assertEqual(row["turn_state"], "idle")
        self.assertFalse(row["hang_candidate"],
                         "an idle seat was queued for the hang remedy")
        self.assertEqual(row["open_dispatches"], 0)
        self.assertEqual(proxywatch.findings(rep(row)), [])

    def test_the_same_stale_shape_WITH_open_work_stays_a_hang_candidate(self):
        """The positive control on the same switch (the idle question's other
        arm)."""
        row = self._health(live=True, age=self.STALE, infl=0, pct=30.0,
                           spawn=2 * 3600, work=1)
        self.assertEqual(row["turn_state"], "hung")
        self.assertTrue(row["hang_candidate"])
        self.assertEqual(row["open_dispatches"], 1)

    def test_display_case_seat_reads_the_canonical_open_work_count(self):
        row = self._health(live=True, age=self.STALE, infl=0, pct=30.0,
                           spawn=2 * 3600, work=2, seat_name="CoDeX")
        self.assertEqual(row["open_dispatches"], 2)
        self.assertEqual(row["turn_state"], "hung")

    def test_a_blind_dispatch_ledger_derives_HUNG_UNKNOWN_not_idle(self):
        row = self._health(live=True, age=self.STALE, infl=0, pct=30.0,
                           spawn=2 * 3600, work=None)
        self.assertEqual(row["turn_state"], "hung-unknown")
        self.assertIn("open-dispatch census", row["turn_evidence"])
        self.assertIsNone(row["open_dispatches"])

    def test_health_DERIVES_churn_hung_from_semantic_stall_plus_fresh_writes(self):
        """The retry-churn victim end to end: real transcript with a 46m-old
        open turn, queue row and fresh mtime, a held socket — health() must
        compose HUNG, not thinking."""
        row = self._health(live=True, age=self.STALE, infl=1, pct=30.0,
                           spawn=2 * 3600, complete=False, pending=True,
                           write_age=1, work=1)
        self.assertEqual(row["turn_state"], "hung")
        self.assertIn("queue/retry", row["turn_evidence"])
        self.assertTrue(row["hang_candidate"])

    def test_health_derives_THINKING_for_a_silent_long_generation(self):
        """The no-regression twin: same held socket, but nothing written since
        the prompt — the long-generation shape stays thinking."""
        row = self._health(live=True, age=self.STALE, infl=1, pct=30.0,
                           spawn=2 * 3600, complete=False, pending=True,
                           write_age=self.STALE, work=1)
        self.assertEqual(row["turn_state"], "thinking")


class BlindCensusIsNotOffTest(_HealthRig):
    """A PROCESS CENSUS THAT COULD NOT LOOK IS NOT A SEAT NOBODY LAUNCHED.

    A cross-family review found: "proxywatch maps
    it to off". `_live_seats()` filtered `procs` for a HELM_CHAT_NAME and threw
    the `unreadable` half away, so a seat whose only claude process helm could
    not identify came back absent from the live set — and `turn_state` reads an
    absent seat as `off`, which this module's own vocabulary defines as "a seat
    nobody launched is not hung".

    That is the exact collapse `hung-unknown` exists to prevent, one rung
    lower. The fuse already refuses to guess when the socket census or the
    context read is blind; the PANE read was the one input that answered a
    blind read with a confident verdict.
    """

    STALE = proxywatch.HANG_S + 60

    def test_a_BLIND_census_derives_HUNG_UNKNOWN_not_off(self):
        """THE MUST-HIT. Nothing about this seat is known: no readable process
        names it, and a live claude on the host could not be identified. `off`
        says helm looked and found nobody — it did not look."""
        row = self._health(live=None, age=self.STALE, infl=0, pct=30.0,
                           spawn=2 * 3600)
        self.assertEqual(
            row["turn_state"], "hung-unknown",
            "a blind process census reported the seat as DELIBERATELY OFF: %s"
            % (row["turn_evidence"],))
        self.assertIn("pane", row["turn_evidence"])

    def test_a_BLIND_census_is_never_a_HANG_either(self):
        """The other direction, and it is the reason `hung-unknown` is a
        distinct word rather than a bias: UNKNOWN must not collapse into `off`
        OR into `hung`. A watchdog acting on either guess acts wrong."""
        row = self._health(live=None, age=self.STALE, infl=0, pct=30.0,
                           spawn=2 * 3600)
        self.assertNotEqual(row["turn_state"], "hung")
        self.assertFalse(row["hang_candidate"],
                         "a seat helm could not see was queued for the hang "
                         "remedy")

    def test_a_PROVEN_absence_still_derives_off(self):
        """THE CONTROL. A census that looked at every claude on the host and
        found none naming this seat has ANSWERED, and `off` is that answer. A
        fix that made every unlaunched seat read `hung-unknown` would bury the
        real ones."""
        row = self._health(live=False, age=self.STALE, infl=0, pct=30.0,
                           spawn=2 * 3600)
        self.assertEqual(row["turn_state"], "off")

    def test_a_LIVE_seat_still_derives_its_real_verdict(self):
        """The positive control on the other side of the same switch."""
        row = self._health(live=True, age=self.STALE, infl=0, pct=30.0,
                           spawn=2 * 3600)
        self.assertEqual(row["turn_state"], "hung")

    def test_the_census_reports_BLINDNESS_from_the_unreadable_bucket(self):
        """The derivation, not the rig: `_live_seats` itself must carry the
        fact. Mocking it in the tests above proves `health` USES the flag;
        this proves the flag is actually produced."""
        from helm import orcaadopt
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([], [303])):
            names, blind = proxywatch._live_seats()
        self.assertEqual(names, set())
        self.assertTrue(blind, "the unreadable bucket was dropped on the floor")
        self.assertIn("303", blind)
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([{"pid": 1, "seat": "codex"}], [])):
            names, blind = proxywatch._live_seats()
        self.assertEqual(names, {"codex"})
        self.assertIsNone(blind, "a readable census must not report blindness")

    def test_a_census_that_RAISES_is_blind_not_empty(self):
        """The same rule at the same function's other exit. `except Exception:
        return set()` said "no seat has a live pane" about a scan that never
        completed — every watched seat then read `off`."""
        from helm import orcaadopt
        with mock.patch.object(orcaadopt, "claude_processes",
                               side_effect=OSError("/proc is not mounted")):
            names, blind = proxywatch._live_seats()
        self.assertEqual(names, set())
        self.assertTrue(blind, "a census that RAISED reported an empty fleet")


class UpstreamSnapshotTest(unittest.TestCase):
    """The CACHED provider verdict, for surfaces that must never probe.

    proxywatch classified RATE-LIMITED / MALFORMED200 / AUTH-UNAVAILABLE for a
    long time and NOTHING read it: `helm seat where codex` printed "LIVE;
    liveness IDLE" while codex had been hard-walled for three hours. Measured
    live: that false picture cost four reviewers 20-153 minutes each."""

    def snap(self, state, err=None, now=None):
        with mock.patch.object(proxywatch, "_read_watch_state",
                               return_value=(state, err)):
            return proxywatch.upstream_snapshot(now=now)

    def test_a_fresh_record_returns_the_verdict(self):
        up, err = self.snap({"ts": 1000, "upstream": {"codex": {
            "state": "RATE-LIMITED", "dark": True}}}, now=1060)
        self.assertIsNone(err)
        self.assertEqual(up["codex"]["state"], "RATE-LIMITED")

    # noqa: VACUOUS_ASSERTION — the control is the FIRST snap() in this same
    # test: the identical record one second INSIDE the bar returns a verdict
    # (assertIsNone(err) + assertIsNotNone(up)). The rung cannot follow it
    # because `up` is then reassigned for the stale case.
    def test_a_STALE_record_is_an_ERROR_not_a_verdict(self):
        """THE FAILURE A CACHE ADDS THAT THE SOURCE DOES NOT HAVE. A record
        from a watcher that stopped an hour ago describes a world that no
        longer exists; rendering it as current rebuilds the exact false picture
        this exists to end, in fresher-looking words."""
        # CONTROL FIRST: the same record INSIDE the bar does return a verdict,
        # so the error below measures the age and not a broken reader.
        up, err = self.snap({"ts": 1000, "upstream": {"codex": {"state": "X"}}},
                            now=1000 + proxywatch.UPSTREAM_CACHE_FRESH_S - 1)
        self.assertIsNone(err)
        self.assertIsNotNone(up)
        up, err = self.snap({"ts": 1000, "upstream": {"codex": {"state": "X"}}},
                            now=1000 + proxywatch.UPSTREAM_CACHE_FRESH_S + 1)
        # STRUCTURAL and unconditional: the refusal NAMES the age it measured,
        # so this is the staleness rung and not some other early return.
        self.assertIn("watcher is not running", err)
        self.assertIn("bar %dm" % (proxywatch.UPSTREAM_CACHE_FRESH_S // 60), err)
        self.assertIsNone(up)

    def test_an_unreadable_state_is_an_error_never_empty(self):
        """The corrupt-outbox case from a cross-family review: a blind read
        must never masquerade as clean. Here that means it must not look like
        'no families are dark'."""
        up, err = self.snap({}, err="corrupt json")
        self.assertIsNone(up)
        self.assertIn("unreadable", err)

    def test_delivery_reader_recovers_last_good_dark_after_primary_damage(self):
        with tempfile.TemporaryDirectory() as tmp:
            primary = os.path.join(tmp, "proxywatch.json")
            backup = primary + ".last-good"
            with open(primary, "w", encoding="utf-8") as handle:
                handle.write("{corrupt")
            with open(backup, "w", encoding="utf-8") as handle:
                json.dump({"ts": 1000, "upstream": {"codex": {
                    "state": "AUTH-401", "dark": True}}}, handle)
            with mock.patch.object(proxywatch, "_state_path",
                                   return_value=primary), \
                    mock.patch.object(proxywatch, "_backup_state_path",
                                      return_value=backup):
                state, err = proxywatch._read_delivery_state()
                pause, perr = proxywatch.delivery_pause(
                    "codex", state=state, now=1001)
            self.assertIsNone(err)
            self.assertIsNone(perr)
            self.assertEqual(pause["state"], "AUTH-401")

    def test_delivery_pause_holds_dark_until_measured_healthy(self):
        dark = {"ts": 1000, "upstream": {"codex": {
            "state": "RATE-LIMITED", "dark": True,
            "since": "episode-start"}}}
        pause, err = proxywatch.delivery_pause("codex-2", state=dark,
                                                now=10 ** 9)
        self.assertIsNone(err)
        self.assertEqual(pause["family"], "codex")
        self.assertEqual(pause["state"], "RATE-LIMITED")
        self.assertTrue(pause["stale"],
                        "stale observation must hold, not invent recovery")

        unknown = {"ts": 1001, "upstream": {"codex": {
            "state": "UNKNOWN", "dark": True,
            "since": "episode-start"}}}
        pause, err = proxywatch.delivery_pause("codex", state=unknown,
                                                now=1002)
        self.assertIsNone(err)
        self.assertEqual(pause["state"], "UNKNOWN")

        healthy = {"ts": 1003, "upstream": {"codex": {
            "state": "HEALTHY", "dark": False}}}
        pause, err = proxywatch.delivery_pause("codex", state=healthy,
                                                now=1004)
        self.assertIsNone(err)
        self.assertIsNone(pause)

    def test_delivery_pause_is_family_scoped_and_corruption_holds_unknown(self):
        dark = {"ts": 1000, "upstream": {
            "codex": {"state": "AUTH-UNAVAILABLE", "dark": True},
            "kimi": {"state": "HEALTHY", "dark": False}}}
        self.assertIsNotNone(proxywatch.delivery_pause(
            "codex-3", state=dark, now=1001)[0])
        self.assertIsNotNone(proxywatch.delivery_pause(
            "pi-codex", state=dark, now=1001,
            runtime={"family": "codex", "agent_harness": "pi"},
            runtime_verified=True)[0])
        self.assertIsNone(proxywatch.delivery_pause(
            "kimi", state=dark, now=1001)[0])
        self.assertEqual(proxywatch.delivery_pause(
            "pi-codex", state=dark, now=1001,
            runtime={"family": "codex"}, runtime_verified=False),
            (None, None), "a foreign runtime mirror cannot steer delivery")
        self.assertEqual(proxywatch.delivery_pause(
            "helm-claude", state=dark, now=1001), (None, None))
        pause, err = proxywatch.delivery_pause("codex", state=[])
        self.assertIsNotNone(pause)
        self.assertEqual(pause["state"], "UNKNOWN")
        self.assertIn("not an object", err)
        for malformed in (
                {"upstream": ["valid-json-wrong-shape"]},
                {"upstream": {"codex": {
                    "state": "UNKNOWN", "dark": "false"}}}):
            pause, err = proxywatch.delivery_pause("codex", state=malformed)
            self.assertIsNotNone(pause)
            self.assertTrue(err)

    def test_the_reason_carries_no_verdict_word(self):
        """REASON AND VERDICT ARE SPLIT — the caller owns the word UNKNOWN. The
        first version returned both and the surface printed
        'upstream UNKNOWN (... upstream UNKNOWN)'."""
        # UNCONDITIONAL, not inside the loop: an empty iterable or a False
        # branch would skip every assertion and the test would still pass.
        _up, err = self.snap({}, err="corrupt json")
        self.assertTrue(err, "control: this path DOES produce a reason")
        self.assertNotIn("UNKNOWN", err)
        _up2, err2 = self.snap({"ts": 1, "upstream": {"a": {}}}, now=10 ** 9)
        self.assertTrue(err2)
        self.assertNotIn("UNKNOWN", err2)


class HostSuspendGapTest(unittest.TestCase):
    """The kernel read that ARMS the suspend correction. Without it the whole
    mechanism lands inert — a parameter no caller ever supplies, which is the
    landed-but-not-in-force shape.

    CLOCK_BOOTTIME advances while the machine is suspended; CLOCK_MONOTONIC
    does not, so their difference IS the suspended total. A fact, not an
    inference — which is why it is the authority and the lockstep signature is
    only its corroborator."""

    def test_it_returns_a_real_reading_on_THIS_host(self):
        """THE ONLY UNMOCKED ASSERTION IN THIS CLASS, and the one that matters:
        the reader must work against the actual kernel, not just against my
        doubles. A mocked-only suite would prove I can parse numbers I invented."""
        v = proxywatch.host_suspend_gap_s()
        self.assertIsInstance(v, int, "a live host must yield a NUMBER, not None")
        self.assertGreaterEqual(v, 0)

    def test_the_noise_band_around_zero_is_ZERO_and_never_UNKNOWN(self):
        """MEASURED LIVE AND IT WAS A REAL DEFECT. The two clock reads are not
        simultaneous, so a never-suspended host yields a delta a few
        NANOSECONDS either side of zero. The first version returned None for
        -0.0000001, which would have made every stale seat hung-unknown and
        suppressed hang detection entirely. Clean mock integers would never
        have shown it."""
        # UNCONDITIONAL POSITIVE CONTROL, same mock path: a delta OUTSIDE the
        # band still reports its value. Without it, `== 0` would also hold for
        # a reader hardcoded to return zero — which is the very shape that
        # would suppress every hang verdict.
        with mock.patch.object(proxywatch.time, "clock_gettime",
                               side_effect=[103600.0, 100000.0]):
            self.assertEqual(proxywatch.host_suspend_gap_s(), 3600)
        with mock.patch.object(proxywatch.time, "clock_gettime",
                               side_effect=[100.0, 100.0000001]):
            self.assertEqual(proxywatch.host_suspend_gap_s(), 0)

    def test_a_real_suspend_is_reported_in_seconds(self):
        with mock.patch.object(proxywatch.time, "clock_gettime",
                               side_effect=[107200.0, 100000.0]):
            self.assertEqual(proxywatch.host_suspend_gap_s(), 7200)

    def test_broken_clock_semantics_are_UNKNOWN_never_zero(self):
        """A substantially negative delta means the two clocks do not mean what
        this depends on. Reporting 0 there is the single change that would turn
        the guard back into the six false HUNGs it exists to prevent."""
        with mock.patch.object(proxywatch.time, "clock_gettime",
                               side_effect=[100.0, 500.0]):
            self.assertIsNone(proxywatch.host_suspend_gap_s())

    def test_a_kernel_without_BOOTTIME_is_UNKNOWN(self):
        with mock.patch.object(proxywatch.time, "clock_gettime",
                               side_effect=OSError("unsupported")):
            self.assertIsNone(proxywatch.host_suspend_gap_s())


class TurnStateHostSuspendTest(unittest.TestCase):
    """A HOST SUSPEND IS NOT A PROPERTY OF ANY SEAT, AND THE LADDER ASKED ONLY
    SEATS. Staleness is measured in WALL time and wall time keeps counting
    while the box is suspended, so every seat comes back stale by the length of
    the suspend, all at once. Six seats each answered "I am stale" truthfully
    and the composition invented six hangs (measured live: six lockstep false
    HUNGs, the finding that opened this fix).

    THE FIX IS NOT A NEW VERDICT BESIDE `hung` — it is that the AGE was never
    the seat's elapsed time. Subtracting the suspend restores the quantity
    every rung below already reasons about correctly.

    A cross-family review noted: the suspend adds the SAME delta to every
    seat, so the seats are SIMILAR and never identical; and UNKNOWN must never
    read as "no suspend", which is why the input is three-valued like
    `pane_live`."""

    BASE = dict(pane_live=True, log_state="ok", inflight_n=0, ctx_pct=10,
                ctx_threshold=90, spawn_age=99999, open_dispatches=3,
                reality={"write_age_s": 9999, "turn_complete": True,
                         "pending_after": 2})

    def verdict(self, age, gap):
        return proxywatch.turn_state(age=age, suspend_gap_s=gap, **self.BASE)

    def test_the_SAME_wall_age_is_hung_or_ok_depending_only_on_the_suspend(self):
        """THE WHOLE FINDING IN ONE ASSERTION: identical inputs except the
        host-level fact, opposite correct verdicts. The control is the first
        line — without it this would also pass for a ladder that never says
        hung at all."""
        hung_v, hung_ev = self.verdict(7500, 0)
        ok_v, _ok_ev = self.verdict(7500, 7200)
        self.assertEqual((hung_v, ok_v), ("hung", "ok"))
        # STRUCTURAL, unconditional: the hung verdict names the measured age it
        # reasoned from. A pair of bare labels would also be produced by a
        # ladder that pattern-matched the gap without reading the transcript.
        self.assertIn("stale 125m", hung_ev)

    def test_a_seat_hung_ACROSS_a_suspend_still_reads_hung(self):
        """The subtraction removes ONLY time the host provably did not run.
        Excess staleness survives it, so this never becomes a way to hide a
        real hang behind a suspend."""
        v, ev = self.verdict(20000, 7200)
        self.assertEqual(v, "hung")
        self.assertIn("stale 213m", ev)          # 333m wall - 120m suspend

    def test_an_UNREADABLE_suspend_is_hung_unknown_never_hung(self):
        """UNKNOWN NEVER READS AS "NO SUSPEND". A reading that could not look
        has made no claim, and collapsing it into zero is exactly how one
        box-level event becomes N false hangs."""
        v, ev = self.verdict(7500, None)
        self.assertEqual(v, "hung-unknown")
        self.assertIn("HOST was suspended", ev)
        # and it is NOT the pre-existing unknown rungs speaking:
        self.assertNotIn("cannot see:", ev)

    def test_an_unreadable_suspend_INSIDE_the_stale_window_changes_nothing(self):
        """Narrow by construction: only a seat that would otherwise be a HANG
        CANDIDATE is affected. Inside the window the suspend cannot change the
        answer, so an unreadable gap must not manufacture doubt."""
        v, ev = self.verdict(60, None)
        self.assertEqual(v, "ok")
        # STRUCTURAL: `ok` here is the fresh-enough rung returning early with
        # no evidence, NOT the suspend rung having quietly fired and said
        # nothing. Those are different code paths with the same label.
        self.assertIsNone(ev)

    def test_the_default_is_zero_so_every_existing_caller_is_unchanged(self):
        """UNCONDITIONAL CONTROL on the seam: callers that never heard of this
        input must behave exactly as before, or the parameter would be a
        silent behaviour change to every existing verdict."""
        without = proxywatch.turn_state(age=7500, **self.BASE)
        self.assertEqual(without, self.verdict(7500, 0))
        self.assertEqual(without[0], "hung")


class TurnVerdictFindingsTest(unittest.TestCase):
    """The verdict's reporting layer — one finding, with evidence and action.

    A detector that computes HUNG but never surfaces it is a sensor with no
    actuator; a verdict without its evidence is a guess with confidence."""

    def test_HUNG_carries_the_evidence_and_the_action(self):
        """A seat the pane-tail classifier clears of being human-blocked is
        hung in the old sense, and the resume/reseed prescription is correct.
        (The classifier is mocked CLEAN here — an unreadable one degrades the
        prescription instead; BlockedOnHumanVerdictTest owns that arm.)"""
        liv = {"state": "IDLE", "blocked_on": None, "evidence": "pane-tail"}
        f = proxywatch.findings(rep(row(
            hang=True, turn="hung", liveness=liv,
            turn_evidence="pane LIVE; transcript stale 62m > 45m; 0 in-flight "
                          "connections at the proxy port")))
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0][0], "HUNG")
        self.assertIn("0 in-flight", f[0][1])          # the composed evidence
        self.assertIn("helm seat resume codex", f[0][1])  # the action
        self.assertIn("reseed", f[0][1])   # live: two resumes did not stick

    def test_THINKING_asks_no_question_and_raises_no_alarm(self):
        """The false alarm the in-flight rung exists to kill: a stale live
        pane WITH an open request is a long generation, and neither HUNG nor
        the old HANG? question may fire over it."""
        self.assertEqual(proxywatch.findings(
            rep(row(hang=True, turn="thinking"))), [])

    def test_compact_needed_fresh_and_idle_are_answers_not_alarms(self):  # noqa: VACUOUS_ASSERTION — deliberate-absence contract; the positive control on the same observable is test_HUNG_carries_the_evidence_and_the_action
        for turn in ("compact-needed", "fresh", "idle"):
            self.assertEqual(proxywatch.findings(
                rep(row(hang=False, turn=turn))), [],
                "%s is an ANSWER to the hang question, not a finding" % turn)
        for turn in ("compact-needed", "fresh"):
            self.assertEqual(proxywatch.findings(
                rep(row(hang=True, turn=turn))), [],
                "%s is an ANSWER to the hang question, not a finding" % turn)

    def test_HUNG_UNKNOWN_keeps_the_question_and_names_the_blind_spot(self):
        """Where the fuse cannot see, the honest HANG? question survives —
        enriched with WHICH input was unreadable, never upgraded to a verdict."""
        f = proxywatch.findings(rep(row(
            hang=True, age=3600, turn="hung-unknown",
            turn_evidence="cannot see: socket census")))
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0][0], "HANG?")
        self.assertIn("Not a verdict", f[0][1])
        self.assertIn("cannot see: socket census", f[0][1])


class LogScanTest(unittest.TestCase):
    """The LOG rung — the starvation incident's failure class, pinned.

    Measured live: a seat's proxy.log held a two-day HTTP 402 wall while
    the watch covered only codex-family seats. The transport class was fully
    legible on disk; owner intent was not — that seat is deliberately parked
    pending CLI proxy cursor support, not waiting on a payment. These tests pin
    both the observed line shape and the boundary between evidence and remedy.
    """

    NOISE = ("[2026-07-29 04:27:02] [--------] [info ] [model_updater.go:133] "
             "periodic model refresh completed, no changes detected")

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="helm-test-pwl-")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _scan(self, *lines):
        p = os.path.join(self.d, "proxy.log")
        with open(p, "w") as f:
            f.write("\n".join(lines) + "\n")
        return proxywatch.logscan(p)

    def test_a_402_streak_on_the_REAL_line_shape_is_a_streak(self):
        """Three of the incident's verbatim 402 lines, with the refresh noise
        the real log interleaves — the scanner must find the streak between
        them."""
        state, detail = self._scan(
            self.NOISE, gin(402, ts="2026-07-29 11:18:00"),
            gin(402, ts="2026-07-29 11:19:00"), self.NOISE, GIN_402)
        self.assertEqual(state, "streak")
        self.assertIn("HTTP 402 x3", detail)

    def test_the_streak_finding_names_the_starvation(self):
        state, detail = self._scan(
            gin(402, ts="2026-07-29 11:18:00"),
            gin(402, ts="2026-07-29 11:19:00"), GIN_402)
        f = proxywatch.findings(rep(row(log=state, log_detail=detail,
                                         turn="starved")))
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0][0], "STARVED")
        self.assertIn("proxy refusing", f[0][1])
        self.assertIn("HTTP 402 x3", f[0][1])
        self.assertIn("starvation", f[0][1])

    def test_a_short_5xx_burst_with_a_healthy_turn_is_BLIP_not_STARVED(self):
        """A live false alarm, synthetic but exact in shape: four
        503s in four seconds beside probe=healthy and turn=ok. A server burst
        is upstream evidence, never credential evidence, and its four-second
        window is below the named STARVED threshold."""
        lines = [gin(503, level="error", ts=ts) for ts in (
            "2026-07-29 22:37:59", "2026-07-29 22:38:00",
            "2026-07-29 22:38:01", "2026-07-29 22:38:03")]
        state, detail = self._scan(*lines)
        self.assertEqual(state, "blip")
        self.assertIn("UPSTREAM/transient", detail)
        self.assertIn("4 refusals across 4s", detail)
        self.assertIn("60s starvation threshold", detail)
        f = proxywatch.findings(rep(row(log=state, log_detail=detail,
                                         probe="healthy", turn="ok")))
        self.assertEqual([lvl for lvl, _text in f], ["BLIP"])
        self.assertNotIn("STARVED", f[0][1])
        self.assertNotIn("credential/billing", f[0][1])
        self.assertIn("taking turns", f[0][1])

    def test_each_status_class_says_only_what_its_codes_support(self):
        cases = (((401, 403, 401), "AUTH"),
                 ((402, 402, 402), "BILLING/quota-exhausted"),
                 ((429, 429, 429), "RATE-LIMITED"),
                 ((500, 502, 503), "UPSTREAM/transient"))
        for codes, cause in cases:
            with self.subTest(codes=codes):
                state, detail = self._scan(*[
                    gin(code, level="error" if code >= 500 else "warn ", ts=ts)
                    for code, ts in zip(codes, (
                        "2026-07-29 11:18:00", "2026-07-29 11:19:00",
                        "2026-07-29 11:20:00"))])
                self.assertEqual(state, "streak")
                self.assertIn("cause %s" % cause, detail)

    def test_fork_error_bodies_name_overload_not_generic_5xx(self):
        """Our CLIProxyAPI fork's silent-swallow-fix logs the quoted error
        body; unanimous named bodies outrank the code class."""
        body = ('{"type":"error","error":{"type":'
                '"service_unavailable_error","message":'
                '"Our servers are currently overloaded"}}')
        state, detail = self._scan(
            gin(503, level="error", ts="2026-07-29 11:18:00", body=body),
            gin(503, level="error", ts="2026-07-29 11:19:00", body=body),
            gin(503, level="error", ts="2026-07-29 11:20:00", body=body))
        self.assertEqual(state, "streak")
        self.assertIn("cause UPSTREAM-OVERLOADED", detail)

    def test_fork_error_bodies_keep_auth_unavailable_out_of_overload(self):
        body = ('{"type":"error","error":{"message":'
                '"auth_unavailable: no auth available '
                '(providers=xai, model=grok-build-0.1)"}}')
        state, detail = self._scan(
            gin(503, level="error", ts="2026-07-29 11:18:00", body=body),
            gin(503, level="error", ts="2026-07-29 11:19:00", body=body),
            gin(503, level="error", ts="2026-07-29 11:20:00", body=body,
                truncated=True))
        self.assertEqual(state, "streak")
        self.assertIn("cause AUTH-UNAVAILABLE", detail)
        self.assertNotIn("UPSTREAM-OVERLOADED", detail)

    def test_disagreeing_bodies_stay_cause_UNKNOWN(self):
        """Mixed named bodies establish refusal, not one cause — the same
        no-laundering law the mixed-code cluster already carries."""
        overload = ('{"type":"error","error":{"type":'
                    '"service_unavailable_error","message":"overloaded"}}')
        auth = ('{"type":"error","error":{"message":'
                '"auth_unavailable: no auth available"}}')
        state, detail = self._scan(
            gin(503, level="error", ts="2026-07-29 11:18:00", body=overload),
            gin(503, level="error", ts="2026-07-29 11:19:00", body=auth),
            gin(503, level="error", ts="2026-07-29 11:20:00", body=overload))
        self.assertEqual(state, "streak")
        self.assertIn("cause UNKNOWN", detail)

    def test_upstream_rows_without_bodies_remain_compatible(self):
        """The unforked shape is the compatibility contract: no suffix, code
        classes decide — exactly the pre-body behavior."""
        state, detail = self._scan(
            gin(503, level="error", ts="2026-07-29 11:18:00"),
            gin(503, level="error", ts="2026-07-29 11:19:00"),
            gin(503, level="error", ts="2026-07-29 11:20:00"))
        self.assertEqual(state, "streak")
        self.assertIn("cause UPSTREAM/transient", detail)

    def test_a_sustained_refusal_beside_a_healthy_turn_reports_disagreement(self):
        state, detail = self._scan(
            gin(503, level="error", ts="2026-07-29 11:18:00"),
            gin(503, level="error", ts="2026-07-29 11:19:00"),
            gin(503, level="error", ts="2026-07-29 11:20:00"))
        f = proxywatch.findings(rep(row(log=state, log_detail=detail,
                                         probe="healthy", turn="ok")))
        self.assertEqual([lvl for lvl, _text in f], ["REFUSAL"])
        self.assertIn("UPSTREAM/transient", f[0][1])
        self.assertIn("taking turns", f[0][1])
        self.assertNotIn("agent is starved", f[0][1])
        self.assertNotIn("credential/billing", f[0][1])

    def test_a_sustained_402_with_a_starved_turn_stays_STARVED_and_billing(self):
        """The starvation positive control: a refusal cluster spanning the
        threshold plus a starved turn is a real starvation finding, and 402
        says billing or quota exhaustion rather than a generic guess."""
        state, detail = self._scan(
            gin(402, ts="2026-07-29 11:18:00"),
            gin(402, ts="2026-07-29 11:19:00"),
            gin(402, ts="2026-07-29 11:20:00"))
        self.assertEqual(state, "streak")
        self.assertIn("BILLING/quota-exhausted", detail)
        f = proxywatch.findings(rep(row(log=state, log_detail=detail,
                                         probe="healthy", turn="starved")))
        self.assertEqual([lvl for lvl, _text in f], ["STARVED"])
        self.assertIn("billing", f[0][1].lower())
        self.assertIn("two-day INERT 402 wall", f[0][1])
        self.assertIn("deliberately parked pending CLI proxy cursor support",
                      f[0][1])
        self.assertIn("not waiting on a payment", f[0][1])

    def test_a_sustained_mixed_cluster_names_cause_UNKNOWN(self):
        """Mixed code classes establish refusal, not cause. The alert may name
        starvation from liveness, but it may not launder mixed evidence into a
        credential, billing, rate-limit, or upstream diagnosis."""
        state, detail = self._scan(
            gin(402, ts="2026-07-29 11:18:00"),
            gin(503, level="error", ts="2026-07-29 11:19:00"),
            gin(402, ts="2026-07-29 11:20:00"))
        self.assertEqual(state, "streak")
        self.assertIn("cause UNKNOWN", detail)
        f = proxywatch.findings(rep(row(log=state, log_detail=detail,
                                         turn="starved")))
        self.assertEqual([lvl for lvl, _text in f], ["STARVED"])
        self.assertIn("cause UNKNOWN", f[0][1])
        self.assertNotIn("likely credential/billing", f[0][1])

    def test_a_streak_that_ENDED_is_healthy_history_is_not_an_alarm(self):
        """Recent successes after the errors mean the seat recovered — a
        watch that alerts on history teaches its reader to ignore it."""
        state, _d = self._scan(GIN_402, GIN_402, GIN_402, gin(200))
        self.assertEqual(state, "ok")
        self.assertEqual(proxywatch.findings(rep(row(log="ok"))), [])

    def test_an_unreadable_log_is_UNKNOWN_never_healthy(self):  # noqa: VACUOUS_ASSERTION — unreadable None has a readable-log positive control below
        observed = proxywatch.log_observation(os.path.join(self.d, "absent.log"))
        self.assertEqual(observed["state"], "unknown")
        self.assertNotIn(observed["state"], ("ok", "idle"))
        self.assertIn("unreadable", observed["detail"])
        self.assertIsNone(observed["status_401"])
        # Compatibility control: existing refusal-state callers get the same
        # honest UNKNOWN rather than a new accounting-shaped exception.
        self.assertEqual(proxywatch.logscan(os.path.join(self.d, "absent.log"))[0],
                         "unknown")
        # Unconditional positive control on the same accounting observable: a
        # readable log produces measured counts, so None above means blindness.
        p = os.path.join(self.d, "present.log")
        with open(p, "w") as f:
            f.write(gin(401, path="/v1/chat/completions?helm_canary=1") + "\n")
        self.assertIsNotNone(proxywatch.log_observation(p)["status_401"])

    def test_unknown_is_not_a_finding_either(self):
        """Unknown stays unknown: no evidence of health, no evidence of
        failure. Saying either would be the lie."""
        self.assertEqual(
            proxywatch.findings(rep(row(log="unknown",
                                        log_detail="unreadable"))), [])

    def test_the_watch_never_counts_its_OWN_reflection(self):
        """The probe rung writes a 401 refusal to POST /v1/chat/completions on
        every healthy pass — a real tail carries three in ONE second
        — and the smoke checks GET /v1/models without a
        credential. Counting those would alarm on every quiet seat forever."""
        state, _d = self._scan(gin(401, path="/v1/chat/completions"),
                               gin(401, path="/v1/chat/completions"),
                               gin(401, path="/v1/chat/completions"),
                               gin(401, path="/v1/models", method="GET"))
        self.assertEqual(state, "idle")

    def test_HELM_MARKED_canary_traffic_is_not_agent_refusal_evidence(self):
        state, detail = self._scan(
            gin(503, path="/v1/messages?beta=true&helm_canary=1",
                level="error", ts="2026-07-29 11:18:00"),
            gin(503, path="/v1/messages?beta=true&helm_canary=1",
                level="error", ts="2026-07-29 11:19:00"),
            gin(503, path="/v1/messages?beta=true&helm_canary=1",
                level="error", ts="2026-07-29 11:20:00"))
        self.assertEqual(state, "idle")
        self.assertIn("no agent traffic", detail)

    def test_401_accounting_separates_HELM_CANARY_from_unmarked_traffic(self):
        """A prior incident's must-hit fixture: a raw status census called every
        401 an external hammer even though the request path already carried provenance.
        Both canary endpoints and their unmarked controls live in one log so a
        classifier that counts only by endpoint, or only by status, fails."""
        p = os.path.join(self.d, "proxy.log")
        lines = (
            gin(401, path="/v1/chat/completions?helm_canary=1"),
            gin(401, path="/v1/messages?beta=true&helm_canary=1"),
            gin(401, path="/v1/chat/completions"),
            gin(401, path="/v1/messages?beta=true"),
            gin(401, path="/v1/messages?not_helm_canary=1"),
            gin(200, path="/v1/messages?beta=true"),
        )
        with open(p, "w") as f:
            f.write("\n".join(lines) + "\n")
        observed = proxywatch.log_observation(p)
        self.assertEqual(observed["status_401"], {
            "helm_marked": 2, "unmarked": 3, "total": 5,
            "basis": "request query marker helm_canary=1",
            "scope": {"bytes": os.path.getsize(p), "truncated": False},
        })
        # Positive control on the refusal classifier: accounting must not change
        # the established answer that the latest real agent request recovered.
        self.assertEqual(observed["state"], "ok")
        rendered_row = row()
        rendered_row["log_status_401"] = observed["status_401"]
        rendered = "\n".join(proxywatch.report_lines(rep(rendered_row)))
        self.assertIn("401[tail=%dB,complete]=helm-marked:2/unmarked:3" %
                      os.path.getsize(p), rendered)

    def test_401_accounting_names_a_TRUNCATED_tail_scope(self):
        """A zero in a bounded tail is not a zero in the file. Put an older
        unmarked 401 outside the read window and require both the scoped count
        and the owner-facing truncation label."""
        p = os.path.join(self.d, "proxy.log")
        older = gin(401, path="/v1/messages?beta=true")
        recent = gin(401, path="/v1/chat/completions?helm_canary=1")
        with open(p, "w") as f:
            f.write(older + "\n" + "x" * 200 + "\n" + recent + "\n")
        tail_bytes = len(recent.encode("utf-8")) + 10
        observed = proxywatch.log_observation(p, tail_bytes=tail_bytes)
        self.assertEqual(observed["status_401"]["helm_marked"], 1)
        self.assertEqual(observed["status_401"]["unmarked"], 0)
        self.assertEqual(observed["status_401"]["scope"],
                         {"bytes": tail_bytes, "truncated": True})
        rendered_row = row()
        rendered_row["log_status_401"] = observed["status_401"]
        rendered = "\n".join(proxywatch.report_lines(rep(rendered_row)))
        self.assertIn("401[tail=%dB,truncated]" % tail_bytes, rendered)

    def test_the_incident_shape_mixed_503_and_402_is_still_ONE_streak(self):
        """The incident's actual tail mixed 503 bursts with the 402s — the
        streak is class-level (refusals), not per-status."""
        state, detail = self._scan(
            gin(503, level="error", ts="2026-07-29 11:18:00"),
            gin(503, level="error", ts="2026-07-29 11:19:00"), GIN_402)
        self.assertEqual(state, "streak")
        self.assertIn("HTTP 402 x3", detail)
        self.assertIn("503", detail)
        self.assertIn("cause UNKNOWN", detail)

    def test_fewer_than_N_trailing_refusals_is_not_a_streak(self):
        state, _d = self._scan(gin(200), GIN_402, GIN_402)
        self.assertEqual(state, "ok")

    def test_a_log_with_no_agent_traffic_is_idle_not_ok(self):
        state, _d = self._scan(self.NOISE, self.NOISE)
        self.assertEqual(state, "idle")


class ProbeTest(unittest.TestCase):
    """The rung that ASKS the proxy, rather than reading about it.

    Config and transcript age never
    ask the proxy anything, and a TCP connect only proves a socket is open.

    NO CREDENTIAL IS SENT. An unauthenticated request separates every failure
    mode on its own, which is what makes the rung both safe and sharp.
    """

    def _probe_returning(self, exc=None, status=None, body=b""):
        import urllib.error

        class _R:
            def __init__(self):
                self.status = status
            def read(self, _n=None):
                return body
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        def fake(_req, timeout=None):
            if exc:
                raise exc
            return _R()
        return mock.patch("urllib.request.urlopen", side_effect=fake)

    def test_a_401_is_HEALTHY_because_the_auth_path_answered(self):
        """Refusing an invalid key is the proxy working. Measured live on
        8317/8360/8390: all three, 401, 27-byte body."""
        import urllib.error
        with self._probe_returning(exc=urllib.error.HTTPError(
                "u", 401, "no", {}, None)):
            state, detail, _ms = proxywatch.probe(8317)
        self.assertEqual(state, "healthy")
        self.assertIn("refused an invalid key", detail)

    def test_a_200_WITH_AN_EMPTY_BODY_is_THE_BUG(self):
        """The known CLIProxyAPI fault: a bad key answered 200-with-nothing,
        which makes an auth failure indistinguishable from a dropped
        completion. That ambiguity is the exact shape the owner reported for a
        week, and it is why this case is named rather than lumped into 'ok'."""
        with self._probe_returning(status=200, body=b"{}"):
            state, detail, _ms = proxywatch.probe(8317)
        self.assertEqual(state, "EMPTY200")
        self.assertIn("dropped completion", detail)

    def test_a_200_with_a_REAL_body_is_not_the_bug(self):
        """The negative control. Only a SUSPICIOUSLY SMALL 200 is the fault;
        flagging every 200 would make the rung useless the day auth succeeds."""
        with self._probe_returning(status=200, body=b"x" * 400):
            state, _d, _ms = proxywatch.probe(8317)
        self.assertEqual(state, "ok")

    def test_a_refused_connection_is_DOWN(self):
        with self._probe_returning(exc=OSError("connection refused")):
            state, _d, _ms = proxywatch.probe(8317)
        self.assertEqual(state, "down")

    def test_the_probe_NEVER_sends_a_real_credential(self):
        """A health check that needed the key would be a new place for the key
        to leak, and would fail closed on every seat whose key it lacked."""
        import inspect
        src = inspect.getsource(proxywatch.probe)
        self.assertIn("deliberately-invalid", proxywatch._BAD_KEY)
        self.assertNotIn("_pi_api_key", src)
        self.assertNotIn("api-keys", src)

    def test_the_keyless_probe_LABELS_ITSELF_in_the_log(self):
        """helm's own reflection must be identifiable in proxy.log, because a
        reader who cannot tell the instrument from the traffic misreads the
        instrument as the fault.

        The gin logger records the QUERY STRING and no headers, so a query
        marker is the only "identifying header" the log can carry — the
        Authorization bearer is invisible to every reader of that file. This
        probe deliberately sends an invalid key to prove the proxy answers
        401; unlabelled, those rows are indistinguishable from real refusals
        on the same path, and a starvation scan drafted from the 4xx tail
        would call every healthy seat starved (one live seat carried ~344 of
        these beside ~241 real successes)."""
        import inspect
        src = inspect.getsource(proxywatch.probe)
        # CONTROL, unconditional and on the same observable: the AUTHENTICATED
        # canary has always carried the marker, so this pins a gap between two
        # probes rather than the mere presence of a constant.
        # the source references the NAME, never the literal value — asserting
        # the value here failed the control first, which is what a control is
        # for: it caught the assertion's shape before the claim was trusted.
        self.assertIn("_CANARY_QUERY",
                      inspect.getsource(proxywatch._canary_once))
        self.assertIn("_CANARY_QUERY", src)
        # and the owner observation both excludes marked rows from refusal state
        # and preserves them in explicit provenance accounting.
        self.assertIn("_is_helm_canary_path",
                      inspect.getsource(proxywatch._log_state))
        self.assertIn("_status_401_provenance",
                      inspect.getsource(proxywatch.log_observation))

    def test_EMPTY200_and_DOWN_reach_findings(self):
        f = proxywatch.findings(rep(dict(row(), probe="EMPTY200",
                                         probe_detail="answered 200")))
        self.assertEqual(f[0][0], "EMPTY200")
        f = proxywatch.findings(rep(dict(row(), probe="down",
                                         probe_detail="refused")))
        self.assertEqual(f[0][0], "DOWN")

    def test_a_healthy_probe_says_nothing(self):
        self.assertEqual(
            proxywatch.findings(rep(dict(row(), probe="healthy"))), [])


class ProxyRuntimeProofTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="helm-test-proxy-runtime-")
        self.state = os.path.join(self.d, "proxywatch.json")
        self.state_patch = mock.patch.object(proxywatch, "_state_path",
                                             return_value=self.state)
        self.state_patch.start()
        proxywatch._PROXY_AUTH_CANARIES.clear()

    def tearDown(self):
        proxywatch._PROXY_AUTH_CANARIES.clear()
        self.state_patch.stop()
        shutil.rmtree(self.d, ignore_errors=True)

    def write_state(self, proofs, ts=1000):
        with open(self.state, "w", encoding="utf-8") as f:
            json.dump({"ts": ts, "proxy_runtime": proofs}, f)

    @staticmethod
    def shape_for(proof):
        base = {key: value for key, value in proof.items()
                if key not in ("observed_at", "canary")}
        return {"url": proof["local_base_url"], "token": "live-secret",
                "model": proof["model"], "proof": base}

    def test_live_session_record_binds_a_proxy_process_without_an_env_session(self):
        sessions_dir = os.path.join(self.d, "sessions")
        os.makedirs(sessions_dir)
        with open(os.path.join(sessions_dir, "live.json"), "w", encoding="utf-8") as f:
            json.dump({"sessionId": "session-ds4pro", "pid": 4101,
                       "procStart": 701}, f)
        env = {"ANTHROPIC_MODEL": "ds4-pro",
               "ANTHROPIC_BASE_URL": "http://127.0.0.1:8360",
               "ANTHROPIC_AUTH_TOKEN": "secret"}
        with mock.patch("helm.sessions.cred_homes", return_value=[self.d]), \
                mock.patch("helm.sessions._pid_is_claude", return_value=True), \
                mock.patch("helm.beacons.proc_env", return_value=env), \
                mock.patch("helm.beacons.proc_starttime", return_value=701):
            runtime, err = proxywatch._live_session_runtime("session-ds4pro")
        self.assertIsNone(err, err)
        self.assertEqual(runtime["pid"], 4101)
        self.assertEqual(runtime["starttime"], 701)
        self.assertEqual(runtime["model"], "ds4-pro")

    def test_live_session_record_refuses_a_contradictory_env_session(self):
        sessions_dir = os.path.join(self.d, "sessions")
        os.makedirs(sessions_dir)
        with open(os.path.join(sessions_dir, "live.json"), "w", encoding="utf-8") as f:
            json.dump({"sessionId": "session-ds4pro", "pid": 4101,
                       "procStart": 701}, f)
        env = {"CLAUDE_CODE_SESSION_ID": "another-session",
               "ANTHROPIC_MODEL": "ds4-pro",
               "ANTHROPIC_BASE_URL": "http://127.0.0.1:8360",
               "ANTHROPIC_AUTH_TOKEN": "secret"}
        with mock.patch("helm.sessions.cred_homes", return_value=[self.d]), \
                mock.patch("helm.sessions._pid_is_claude", return_value=True), \
                mock.patch("helm.beacons.proc_env", return_value=env), \
                mock.patch("helm.beacons.proc_starttime", return_value=701):
            runtime, err = proxywatch._live_session_runtime("session-ds4pro")
        self.assertIsNone(runtime)
        self.assertIn("contradicts the roster session", err)

    def test_ds4pro_authority_is_the_exact_live_session_listener_config_and_route(self):  # noqa: VACUOUS_ASSERTION — exact positive proof fields precede secret-absence checks
        config = os.path.join(self.d, "config.yaml")
        binary = os.path.join(self.d, "cli-proxy-api")
        secret = "session-bound-secret"
        with open(config, "w", encoding="utf-8") as f:
            f.write(seat._config_yaml_key(
                8360, secret, "opencode-go",
                "https://opencode.ai/zen/go/v1", "ds4-pro", "upstream-secret",
                "deepseek-v4-pro"))
        with open(binary, "wb") as f:
            f.write(b"proxy")
        launch = seat._proxy_launch_inputs(config, binary)
        with open(os.path.join(self.d, "proxy.pid"), "w", encoding="utf-8") as f:
            f.write("4201 proc:702 %s\n" % seat._encode_launch_inputs(launch))
        runtime = {"pid": 4101, "starttime": 701, "model": "ds4-pro",
                   "base_url": "http://127.0.0.1:8360", "token": secret}
        listener = {"pid": 4201, "identity": "proc:702",
                    "argv": [binary, "-config", config], "config": config}
        patches = (mock.patch.object(proxywatch, "_roster_session",
                                     return_value=("session-ds4pro", "ds4pro", None)),
                   mock.patch.object(proxywatch, "_live_session_runtime",
                                     return_value=(runtime, None)),
                   mock.patch("helm.seat._port_listeners",
                              return_value=[listener]))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        shape, err = proxywatch._proxy_runtime_shape("cosmetic-label")
        self.assertIsNone(err, err)
        self.assertEqual(shape["proof"]["session"], "session-ds4pro")
        self.assertEqual(shape["proof"]["agent_pid"], 4101)
        self.assertEqual(shape["proof"]["proxy_pid"], 4201)
        self.assertEqual(shape["proof"]["config_sha256"],
                         seat._config_digest(config))
        self.assertEqual(shape["proof"]["route"], {
            "alias": "ds4-pro", "provider": "opencode-go",
            "upstream_model": "deepseek-v4-pro",
            "base_url": "https://opencode.ai/zen/go/v1"})
        self.assertNotIn("family", shape["proof"])
        with mock.patch.object(
                proxywatch, "_canary_once",
                return_value=("HEALTHY", "HTTP 200 with OK", 4, 200,
                              "deepseek-v4-pro", None)):
            proof, err = proxywatch.proxy_runtime_canary(
                "cosmetic-label", observed_at=1000)
        self.assertIsNone(err, err)
        serialized = json.dumps(proof, sort_keys=True)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("upstream-secret", serialized)
        self.assertEqual(proof["canary"], {"state": "HEALTHY", "status": 200})

    def test_oauth_authority_is_live_model_plus_loaded_credential_provider(self):  # noqa: VACUOUS_ASSERTION — every matrix arm proves the full positive route before secret-absence checks
        cases = (
            ("codex", "gpt-5.6-sol", "codex", 8317, 4301),
            ("gemini", "gemini-3.6-flash-high", "antigravity", 8390, 4302),
        )
        for family, model, provider, port, proxy_pid in cases:
            with self.subTest(family=family):
                root = os.path.join(self.d, family)
                auth = os.path.join(root, "auth")
                os.makedirs(auth)
                secret = family + "-session-secret"
                config = os.path.join(root, "config.yaml")
                binary = os.path.join(root, "cli-proxy-api")
                with open(config, "w", encoding="utf-8") as f:
                    f.write(seat._config_yaml(port, auth, secret))
                auth_path = os.path.join(auth, provider + "-one.json")
                with open(auth_path, "w", encoding="utf-8") as f:
                    json.dump({"type": provider, "access_token": "oauth-secret",
                               "email": "private@example.test"}, f)
                auth_index = hashlib.sha256(
                    (provider + ":" + auth_path).encode("utf-8")).digest()[:8].hex()
                trace = "20260805050000-%s-deadbeef" % auth_index
                with open(binary, "wb") as f:
                    f.write(b"proxy")
                launch = seat._proxy_launch_inputs(config, binary)
                with open(os.path.join(root, "proxy.pid"), "w",
                          encoding="utf-8") as f:
                    f.write("%d proc:%d %s\n" %
                            (proxy_pid, proxy_pid + 1,
                             seat._encode_launch_inputs(launch)))
                runtime = {"pid": 4101, "starttime": 701, "model": model,
                           "base_url": "http://127.0.0.1:%d" % port,
                           "token": secret}
                listener = {"pid": proxy_pid, "identity": "proc:%d" % (proxy_pid + 1),
                            "argv": [binary, "-config", config], "config": config}
                with mock.patch.object(
                        proxywatch, "_roster_session",
                        return_value=("session-" + family, "costume", None)), \
                        mock.patch.object(proxywatch, "_live_session_runtime",
                                          return_value=(runtime, None)), \
                        mock.patch("helm.seat._port_listeners",
                                   return_value=[listener]), \
                        mock.patch.object(
                            proxywatch, "_canary_once",
                            return_value=("HEALTHY", "HTTP 200 with OK", 4, 200,
                                          model, trace)):
                    proof, err = proxywatch.proxy_runtime_canary(
                        "costume", observed_at=1000)
                self.assertIsNone(err, err)
                self.assertEqual(proxywatch._proxy_proof_family(proof),
                                 (family, None))
                self.assertEqual(proof["route"], {
                    "alias": model, "provider": provider,
                    "upstream_model": model})
                serialized = json.dumps(proof, sort_keys=True)
                self.assertNotIn(secret, serialized)
                self.assertNotIn("oauth-secret", serialized)
                self.assertNotIn("private@example.test", serialized)
                self.assertNotIn("family", proof)

    def test_oauth_canary_requires_selected_auth_trace_and_response_model(self):  # noqa: VACUOUS_ASSERTION — positive OAuth matrix above proves these checks admit the exact live shape
        shape = {"url": "http://127.0.0.1:8317", "token": "secret",
                 "model": "gpt-5.6-sol", "auth_indexes": ("a" * 16,),
                 "proof": {"v": 1}}
        cases = (
            ("different-model", "20260805050000-%s-deadbeef" % ("a" * 16),
             "response model"),
            ("gpt-5.6-sol", "20260805050000-%s-deadbeef" % ("b" * 16),
             "trace does not name"),
            ("gpt-5.6-sol", "opaque-trace", "trace does not name"),
        )
        for response_model, trace, message in cases:
            with self.subTest(response_model=response_model, trace=trace), \
                    mock.patch.object(proxywatch, "_proxy_runtime_shape",
                                      return_value=(shape, None)), \
                    mock.patch.object(
                        proxywatch, "_canary_once",
                        return_value=("HEALTHY", "HTTP 200", 4, 200,
                                      response_model, trace)):
                proof, err = proxywatch.proxy_runtime_canary("costume")
            self.assertIsNone(proof)
            self.assertIn(message, err)

    def test_oauth_route_refuses_mixed_provider_records_and_model_mismatch(self):  # noqa: VACUOUS_ASSERTION — valid homogeneous controls prove both refusal gates are reachable
        auth = os.path.join(self.d, "auth")
        os.makedirs(auth)
        config = os.path.join(self.d, "config.yaml")
        with open(config, "w", encoding="utf-8") as f:
            f.write(seat._config_yaml(8317, auth, "inbound-secret"))
        for provider in ("codex", "antigravity"):
            with open(os.path.join(auth, provider + ".json"), "w",
                      encoding="utf-8") as f:
                json.dump({"type": provider, "access_token": "secret"}, f)
        route, token, auth_indexes, err = proxywatch._proxy_config_route(config, "gpt-5.6-sol")
        self.assertIsNone(route)
        self.assertIsNone(token)
        self.assertIn("2 provider types", err)
        os.unlink(os.path.join(auth, "antigravity.json"))
        route, token, auth_indexes, err = proxywatch._proxy_config_route(config, "gpt-5.6-sol")
        self.assertIsNone(err, err)
        self.assertEqual(token, "inbound-secret")
        self.assertEqual(len(auth_indexes), 1)
        self.assertRegex(auth_indexes[0], r"^[0-9a-f]{16}$")
        self.assertEqual(proxywatch._proxy_route_family(route), ("codex", None))
        route, token, auth_indexes, err = proxywatch._proxy_config_route(
            config, "gemini-3.6-flash-high")
        self.assertIsNone(route)
        self.assertIsNone(token)
        self.assertIn("unknown or ambiguous", err)

    def test_oauth_route_refuses_global_and_per_auth_alias_surfaces(self):  # noqa: VACUOUS_ASSERTION — generated config and homogeneous provider controls pass before each alias refusal
        auth = os.path.join(self.d, "auth")
        os.makedirs(auth)
        config = os.path.join(self.d, "config.yaml")
        with open(os.path.join(auth, "codex.json"), "w", encoding="utf-8") as f:
            json.dump({"type": "codex", "access_token": "secret"}, f)
        generated = seat._config_yaml(8317, auth, "inbound-secret") + \
            "routing:\n  strategy: round-robin\n"
        with open(config, "w", encoding="utf-8") as f:
            f.write(generated)
        route, token, auth_indexes, err = proxywatch._proxy_config_route(config, "gpt-5.6-sol")
        self.assertIsNone(err, err)
        self.assertEqual(proxywatch._proxy_route_family(route), ("codex", None))
        self.assertEqual(token, "inbound-secret")
        self.assertEqual(len(auth_indexes), 1)
        with open(config, "w", encoding="utf-8") as f:
            f.write(generated + "oauth-model-alias:\n  codex: []\n")
        route, token, auth_indexes, err = proxywatch._proxy_config_route(config, "gpt-5.6-sol")
        self.assertIsNone(route)
        self.assertIsNone(token)
        self.assertIn("unsupported top-level routing fields", err)
        with open(config, "w", encoding="utf-8") as f:
            f.write(generated)
        with open(os.path.join(auth, "codex.json"), "w", encoding="utf-8") as f:
            json.dump({"type": "codex", "access_token": "secret",
                       "model-aliases": [{"name": "foreign", "alias": "gpt-5.6-sol"}]}, f)
        route, token, auth_indexes, err = proxywatch._proxy_config_route(config, "gpt-5.6-sol")
        self.assertIsNone(route)
        self.assertIsNone(token)
        self.assertIn("per-auth model alias", err)

    def test_runtime_change_across_the_canary_fails_closed(self):
        shape = {"url": "http://127.0.0.1:8360", "token": "secret",
                 "model": "ds4-pro", "proof": {"v": 1}}
        changed = dict(shape, model="replacement")
        with mock.patch.object(proxywatch, "_proxy_runtime_shape",
                               side_effect=[(shape, None), (changed, None)]), \
                mock.patch.object(
                    proxywatch, "_canary_once",
                    return_value=("HEALTHY", "HTTP 200", 4, 200,
                                  "deepseek-v4-pro", None)):
            proof, err = proxywatch.proxy_runtime_canary("ds4pro")
        self.assertIsNone(proof)
        self.assertIn("changed across its canary", err)

    def test_oauth_model_provider_route_must_map_to_exactly_one_family(self):  # noqa: VACUOUS_ASSERTION — one-match control proves only duplication closes the gate
        route = {"alias": "gpt-live", "provider": "codex",
                 "upstream_model": "gpt-live"}
        one = {"codex": {"mode": "proxy", "model": "gpt-live",
                          "auth_type": "codex"}}
        with mock.patch.dict(seat.FAMILIES, one, clear=True):
            self.assertEqual(proxywatch._proxy_route_family(route),
                             ("codex", None))
        duplicate = dict(one, costume=dict(one["codex"]))
        with mock.patch.dict(seat.FAMILIES, duplicate, clear=True):
            family, err = proxywatch._proxy_route_family(route)
        self.assertIsNone(family)
        self.assertIn("2 configured families", err)

    def test_storage_key_ds4pro_cannot_override_a_route_mapping_to_Codex(self):
        route = {"alias": "codex-wire", "provider": "openai",
                 "upstream_model": "gpt-live", "base_url": "https://api.openai.test/v1"}
        proof = runtime_proof(route=route)
        self.write_state({"ds4pro": proof})
        configured = {"codex": {"mode": "proxy-key", "model": "codex-wire",
                                 "provider": "openai", "upstream_model": "gpt-live",
                                 "base_url": "https://api.openai.test/v1"}}
        with mock.patch.dict(seat.FAMILIES, configured, clear=True), \
                mock.patch.object(proxywatch, "_roster_identity_for_session",
                                  return_value=("measured-seat", None)), \
                mock.patch.object(proxywatch, "_proxy_runtime_shape",
                                  return_value=(self.shape_for(proof), None)), \
                mock.patch.object(proxywatch, "proxy_runtime_canary",
                                  return_value=(proof, None)):
            family, got, err = proxywatch.proxy_runtime_snapshot(
                "session-ds4pro", now=1000)
        self.assertIsNone(err, err)
        self.assertEqual(family, "codex")
        self.assertEqual(got["route"], route)
        self.assertNotIn("family", got)

    def test_handwritten_cache_without_a_live_runtime_is_unknown(self):  # noqa: VACUOUS_ASSERTION — valid proof schema is the positive control before live provenance refusal
        proof = runtime_proof()
        self.assertEqual(proxywatch._proxy_proof_family(proof), ("ds4pro", None))
        self.write_state({"costume-key": proof})
        with mock.patch.object(proxywatch, "_roster_identity_for_session",
                               return_value=("measured-seat", None)), \
                mock.patch.object(proxywatch, "_proxy_runtime_shape",
                                  return_value=(None, "no exact listener")), \
                mock.patch.object(proxywatch, "proxy_runtime_canary") as canary:
            family, got, err = proxywatch.proxy_runtime_snapshot(
                proof["session"], now=1000)
        self.assertIsNone(family)
        self.assertIsNone(got)
        self.assertIn("cannot re-prove cached evidence", err)
        canary.assert_not_called()

    def test_cached_proof_is_unknown_after_a_bound_process_changes(self):  # noqa: VACUOUS_ASSERTION — valid cached proof is the positive control before one live binding changes
        proof = runtime_proof()
        self.assertEqual(proxywatch._proxy_proof_family(proof), ("ds4pro", None))
        self.write_state({"ds4pro": proof})
        changed = self.shape_for(proof)
        changed["proof"] = dict(changed["proof"], proxy_pid=9999)
        with mock.patch.object(proxywatch, "_roster_identity_for_session",
                               return_value=("measured-seat", None)), \
                mock.patch.object(proxywatch, "_proxy_runtime_shape",
                                  return_value=(changed, None)), \
                mock.patch.object(proxywatch, "proxy_runtime_canary") as canary:
            family, got, err = proxywatch.proxy_runtime_snapshot(
                proof["session"], now=1000)
        self.assertIsNone(family)
        self.assertIsNone(got)
        self.assertIn("no longer matches the cached proof", err)
        canary.assert_not_called()

    def test_first_authority_read_repeats_the_canary_then_reuses_only_process_memory(self):  # noqa: VACUOUS_ASSERTION — first and second reads positively prove authority
        proof = runtime_proof()
        self.write_state({"ds4pro": proof})
        with mock.patch.object(proxywatch, "_roster_identity_for_session",
                               return_value=("measured-seat", None)), \
                mock.patch.object(proxywatch, "_proxy_runtime_shape",
                                  return_value=(self.shape_for(proof), None)) as shape, \
                mock.patch.object(proxywatch, "proxy_runtime_canary",
                                  return_value=(proof, None)) as canary:
            first = proxywatch.proxy_runtime_snapshot(proof["session"], now=1000)
            second = proxywatch.proxy_runtime_snapshot(proof["session"], now=1001)
        self.assertEqual(first, second)
        self.assertEqual(first[:2], ("ds4pro", proof))
        self.assertIsNone(first[2])
        self.assertEqual(shape.call_count, 2,
                         "every authority read must remeasure the live shape")
        canary.assert_called_once_with("measured-seat", observed_at=1000)

    def test_matching_live_shape_without_a_successful_current_canary_is_unknown(self):
        proof = runtime_proof()
        self.write_state({"ds4pro": proof})
        with mock.patch.object(proxywatch, "_roster_identity_for_session",
                               return_value=("measured-seat", None)), \
                mock.patch.object(proxywatch, "_proxy_runtime_shape",
                                  return_value=(self.shape_for(proof), None)), \
                mock.patch.object(proxywatch, "proxy_runtime_canary",
                                  return_value=(None, "HTTP 401")):
            family, got, err = proxywatch.proxy_runtime_snapshot(
                proof["session"], now=1000)
        self.assertIsNone(family)
        self.assertIsNone(got)
        self.assertIn("canary cannot attest authority", err)

    def test_missing_stale_ambiguous_and_session_mismatched_proof_are_unknown(self):  # noqa: VACUOUS_ASSERTION — every arm requires a concrete fail-closed error
        cases = (
            ("missing", {}, 1000),
            ("stale", {"ds4pro": runtime_proof()},
             1000 + proxywatch.UPSTREAM_CACHE_FRESH_S + 1),
            ("ambiguous", {"ds4pro": runtime_proof(),
                           "costume": runtime_proof()}, 1000),
            ("session mismatch", {"ds4pro": runtime_proof(session="other")},
             1000),
        )
        for name, proofs, now in cases:
            with self.subTest(name=name):
                self.write_state(proofs)
                family, proof, err = proxywatch.proxy_runtime_snapshot(
                    "session-ds4pro", now=now)
                self.assertIsNone(family)
                self.assertIsNone(proof)
                self.assertIsNotNone(err)

    def test_record_persists_only_exact_sanitized_proof(self):
        clean = runtime_proof()
        tainted = dict(clean, api_key="must-never-land")
        report = rep(row(), proxy_runtime={"ds4pro": clean, "tainted": tainted})
        self.assertTrue(proxywatch.record(report))
        with open(self.state, encoding="utf-8") as f:
            saved = json.load(f)["proxy_runtime"]
        self.assertEqual(set(saved), {"ds4pro"})
        self.assertNotIn("must-never-land", json.dumps(saved))


class UpstreamCanaryTest(unittest.TestCase):
    def test_three_second_transient_blip_stays_HEALTHY(self):
        attempts = [("UPSTREAM-OVERLOADED", "HTTP 503", 10),
                    ("HEALTHY", "HTTP 200", 20)]
        sleeps = []
        with mock.patch.object(proxywatch, "_upstream_once", side_effect=attempts):
            state, detail, elapsed = proxywatch.upstream_canary(
                "codex", sleep=sleeps.append)
        self.assertEqual((state, elapsed), ("HEALTHY", 30))
        self.assertEqual(sleeps, [proxywatch.UPSTREAM_CONFIRM_S])
        self.assertIn("transient", detail)

    def test_persistent_completed_failure_is_named_after_one_confirmation(self):
        attempts = [("UPSTREAM-OVERLOADED", "HTTP 503", 10),
                    ("UPSTREAM-OVERLOADED", "HTTP 503", 12)]
        sleeps = []
        with mock.patch.object(proxywatch, "_upstream_once", side_effect=attempts) \
                as once:
            state, detail, elapsed = proxywatch.upstream_canary(
                "codex", sleep=sleeps.append)
        self.assertEqual((state, elapsed), ("UPSTREAM-OVERLOADED", 22))
        self.assertEqual(once.call_count, 2)
        self.assertEqual(sleeps, [proxywatch.UPSTREAM_CONFIRM_S])
        self.assertIn("then UPSTREAM-OVERLOADED", detail)

    def test_client_timeout_is_never_retried_into_duplicate_token_burn(self):
        with mock.patch.object(proxywatch, "_upstream_once",
                               return_value=(proxywatch._CLIENT_TIMEOUT,
                                             "30s", 30000)) as once:
            state, _detail, _ms = proxywatch.upstream_canary(
                "kimi", sleep=mock.Mock())
        self.assertEqual(state, "TIMEOUT-500")
        once.assert_called_once_with("kimi")

    def test_request_layer_recognizes_a_client_timeout_without_waiting_for_clock(self):
        with mock.patch("helm.seat._seat_family",
                        return_value=("codex", None)), \
                mock.patch("helm.pi.seat_port", return_value=(8317, None)), \
                mock.patch("helm.pi._pi_api_key", return_value="secret"), \
                mock.patch("urllib.request.urlopen",
                           side_effect=TimeoutError("timed out")):
            state, _detail, _ms = proxywatch._upstream_once("codex")
        self.assertEqual(state, proxywatch._CLIENT_TIMEOUT)

    def test_completed_HTTP_timeout_is_confirmed_and_can_clear(self):
        attempts = [("TIMEOUT-500", "HTTP 500 — request timed out", 5),
                    ("HEALTHY", "HTTP 200 with OK", 7)]
        with mock.patch.object(proxywatch, "_upstream_once", side_effect=attempts) \
                as once:
            state, detail, elapsed = proxywatch.upstream_canary(
                "codex", sleep=lambda _seconds: None)
        self.assertEqual((state, elapsed), ("HEALTHY", 12))
        self.assertEqual(once.call_count, 2)
        self.assertIn("transient TIMEOUT-500", detail)

    def test_dark_then_unreadable_confirmation_preserves_the_dark_evidence(self):
        attempts = [("AUTH-UNAVAILABLE", "no auth", 4),
                    ("UNKNOWN", "connection reset", 2)]
        with mock.patch.object(proxywatch, "_upstream_once", side_effect=attempts):
            state, detail, elapsed = proxywatch.upstream_canary(
                "codex", sleep=lambda _seconds: None)
        self.assertEqual((state, elapsed), ("AUTH-UNAVAILABLE", 6))
        self.assertIn("confirmation unreadable", detail)

    def test_error_body_classification_names_the_supported_layer(self):
        cases = ((503, "service_unavailable_error overloaded",
                  "UPSTREAM-OVERLOADED"),
                 (503, "auth_unavailable: no auth available",
                  "AUTH-UNAVAILABLE"),
                 (500, "request timed out", "TIMEOUT-500"),
                 (503, "other", "UPSTREAM-5XX"),
                 (402, "balance", "QUOTA-402"),
                 (401, "bad key", "AUTH-401"),
                 (429, "slow", "RATE-LIMITED"),
                 (418, "teapot", "UPSTREAM-4XX"))
        for code, text, expected in cases:
            with self.subTest(code=code, text=text):
                self.assertEqual(proxywatch._upstream_state(code, text), expected)

    def test_real_request_is_eight_tokens_on_the_family_launch_model(self):
        from helm import seat
        seen = {}

        class Response:
            status = 200
            def read(self, _n=None):
                return (b'{"type":"message","role":"assistant",'
                        b'"content":[{"type":"text","text":"OK"}],'
                        b'"stop_reason":"end_turn"}')
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return False

        def open_(req, timeout=None):
            seen["req"], seen["timeout"] = req, timeout
            return Response()

        with mock.patch("helm.seat._seat_family", return_value=("codex", None)), \
                mock.patch("helm.pi.seat_port", return_value=(8317, None)), \
                mock.patch("helm.pi._pi_api_key",
                           return_value="secret-local-token"), \
                mock.patch("urllib.request.urlopen", side_effect=open_):
            state, detail, _ms = proxywatch._upstream_once("codex")
        body = json.loads(seen["req"].data)
        self.assertEqual(state, "HEALTHY")
        self.assertIn("with OK", detail)
        self.assertEqual(body["max_tokens"], proxywatch.UPSTREAM_TOKENS)
        self.assertEqual(body["model"], seat.FAMILIES["codex"]["model"])
        self.assertEqual(seen["timeout"], proxywatch.UPSTREAM_TIMEOUT_S)
        self.assertEqual(seen["req"].get_header("Authorization"),
                         "Bearer secret-local-token")
        self.assertIn(proxywatch._CANARY_QUERY, seen["req"].full_url)
        self.assertNotIn("secret-local-token", detail)

    def test_request_layer_returns_response_model_and_selected_auth_trace(self):
        trace = "20260805050000-0123456789abcdef-deadbeef"

        class Response:
            status = 200
            headers = {"X-CPA-TRACE-ID": trace}

            def read(self, _n=None):
                return (b'{"type":"message","role":"assistant",'
                        b'"model":"gpt-5.6-sol",'
                        b'"content":[{"type":"text","text":"OK"}],'
                        b'"stop_reason":"end_turn"}')

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        with mock.patch("urllib.request.urlopen", return_value=Response()):
            state, _detail, _ms, status, model, got_trace = \
                proxywatch._canary_once(
                    "http://127.0.0.1:8317", "secret", "gpt-5.6-sol")
        self.assertEqual((state, status), ("HEALTHY", 200))
        self.assertEqual(model, "gpt-5.6-sol")
        self.assertEqual(got_trace, trace)

    def test_reasoning_only_at_the_eight_token_cap_is_healthy(self):
        payload = (b'{"type":"message","role":"assistant",'
                   b'"content":[{"type":"thinking","thinking":"x"}],'
                   b'"stop_reason":"max_tokens"}')
        self.assertIn("thinking-only", proxywatch._valid_canary_payload(payload))

    def test_empty_reasoning_at_the_token_cap_is_not_completion_evidence(self):
        payload = (b'{"type":"message","role":"assistant",'
                   b'"content":[{"type":"thinking","thinking":""}],'
                   b'"stop_reason":"max_tokens"}')
        self.assertIsNone(proxywatch._valid_canary_payload(payload))
        numeric = (b'{"type":"message","role":"assistant",'
                   b'"content":[{"type":"thinking","thinking":17}],'
                   b'"stop_reason":"max_tokens"}')
        self.assertIsNone(proxywatch._valid_canary_payload(numeric))

    def test_arbitrary_nonempty_200_is_MALFORMED_not_healthy(self):
        self.assertIsNone(proxywatch._valid_canary_payload(
            b'{"error":{"message":"no completion"}}'))
        self.assertIsNone(proxywatch._valid_canary_payload(
            b'{"type":"message","role":"assistant",'
            b'"content":[{"type":"text","text":"OK"}],'
            b'"error":{"type":"overloaded_error"}}'))
        self.assertIsNone(proxywatch._valid_canary_payload(b'<html>ok</html>'))
        self.assertIsNone(proxywatch._valid_canary_payload(
            b'{"type":"message","role":"assistant",'
            b'"content":[{"type":"text","text":"NOT OK"}]}'))
        self.assertIsNone(proxywatch._valid_canary_payload(
            b'{"type":"message","role":"assistant",'
            b'"content":[{"type":"text","text":" OK "}]}'))
        self.assertIsNone(proxywatch._valid_canary_payload(
            b'{"type":"message","role":"assistant","content":['
            b'{"type":"thinking"},{"type":"text","text":"OK"}]}'))
        self.assertIsNone(proxywatch._valid_canary_payload(
            b'{"type":"message","role":"assistant","content":['
            b'{"type":"thinking","thinking":17},'
            b'{"type":"text","text":"OK"}]}'))
        self.assertIsNone(proxywatch._valid_canary_payload(
            b'{"type":"message","role":"user",'
            b'"content":[{"type":"text","text":"OK"}]}'))
        self.assertIsNone(proxywatch._valid_canary_payload(
            b'{"type":"message","role":"assistant","content":['
            b'{"type":"text","text":"OK"},'
            b'{"type":"text","text":"but not exactly"}]}'))

    def test_empty_and_malformed_HTTP_200_are_named_by_the_request_layer(self):
        class Response:
            status = 200

            def __init__(self, payload):
                self.payload = payload

            def read(self, _n=None):
                return self.payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        cases = ((b"", "EMPTY200"),
                 (b'{"error":{"message":"no completion"}}', "MALFORMED200"))
        for payload, expected in cases:
            with self.subTest(expected=expected), \
                    mock.patch("helm.seat._seat_family",
                               return_value=("codex", None)), \
                    mock.patch("helm.pi.seat_port", return_value=(8317, None)), \
                    mock.patch("helm.pi._pi_api_key", return_value="secret"), \
                    mock.patch("urllib.request.urlopen",
                               return_value=Response(payload)):
                state, _detail, _ms = proxywatch._upstream_once("codex")
            self.assertEqual(state, expected)


class UpstreamFamilyHealthTest(unittest.TestCase):
    @staticmethod
    def rows():
        return [row(seat="codex", family="codex", probe="healthy"),
                row(seat="codex-2", family="codex", probe="healthy")]

    def reduce(self, outcomes, rows=None, prior=None):
        with mock.patch.object(proxywatch, "upstream_canary",
                               side_effect=lambda name, family=None: outcomes[name]) as canary:
            got = proxywatch.upstream_health(rows or self.rows(), now=1000,
                                             prior=prior or {})
        return got, canary

    def test_family_named_primary_is_deterministic_and_healthy_stops_siblings(self):
        rows = list(reversed(self.rows()))
        got, canary = self.reduce({"codex": ("HEALTHY", "current", 7)},
                                  rows=rows)
        canary.assert_called_once_with("codex", family="codex")
        self.assertEqual(got["codex"]["members"], {"codex": "HEALTHY"})
        self.assertEqual(got["codex"]["ms"], 7)

    def test_family_primaries_execute_concurrently_with_one_request_each(self):
        barrier = threading.Barrier(2)

        def canary(_name, family=None):
            barrier.wait(timeout=2)
            return "HEALTHY", "current", 1

        rows = [row(seat="codex", family="codex", probe="healthy"),
                row(seat="kimi", family="kimi", probe="healthy")]
        with mock.patch.object(proxywatch, "upstream_canary", side_effect=canary) \
                as probe:
            got = proxywatch.upstream_health(rows, now=1000, prior={})
        self.assertEqual({call.args[0] for call in probe.call_args_list},
                         {"codex", "kimi"})
        self.assertEqual(got["codex"]["state"], "HEALTHY")
        self.assertEqual(got["kimi"]["state"], "HEALTHY")

    def test_primary_worker_pool_is_bounded_at_eight(self):
        from concurrent.futures import ThreadPoolExecutor
        workers = []

        def executor(max_workers):
            workers.append(max_workers)
            return ThreadPoolExecutor(max_workers=max_workers)

        rows = [row(seat="family-%d" % n, family="family-%d" % n,
                    probe="healthy") for n in range(9)]
        with mock.patch("concurrent.futures.ThreadPoolExecutor",
                        side_effect=executor), \
                mock.patch.object(proxywatch, "upstream_canary",
                                  return_value=("HEALTHY", "ok", 1)):
            got = proxywatch.upstream_health(rows, now=1000, prior={})
        self.assertEqual(workers, [8])
        self.assertEqual(len(got), 9)

    def test_corroboration_worker_pool_is_bounded_at_eight(self):
        from concurrent.futures import ThreadPoolExecutor
        workers = []

        def executor(max_workers):
            workers.append(max_workers)
            return ThreadPoolExecutor(max_workers=max_workers)

        rows = [row(seat="codex", family="codex", probe="healthy")] + [
            row(seat="codex-%d" % n, family="codex", probe="healthy")
            for n in range(2, 11)]
        with mock.patch("concurrent.futures.ThreadPoolExecutor",
                        side_effect=executor), \
                mock.patch.object(proxywatch, "upstream_canary",
                                  return_value=("AUTH-401", "bad", 1)) as canary:
            got = proxywatch.upstream_health(rows, now=1000, prior={})
        self.assertEqual(workers, [1, 8])
        self.assertEqual(canary.call_count, 10)
        self.assertEqual(got["codex"]["state"], "AUTH-401")

    def test_lexicographic_primary_is_used_when_the_family_seat_is_absent(self):
        rows = [row(seat="codex-3", family="codex", probe="healthy"),
                row(seat="codex-2", family="codex", probe="healthy")]
        _got, canary = self.reduce(
            {"codex-2": ("HEALTHY", "ok", 1)}, rows=rows)
        canary.assert_called_once_with("codex-2", family="codex")

    def test_dark_primary_is_corroborated_and_a_healthy_sibling_wins(self):
        outcomes = {"codex": ("AUTH-UNAVAILABLE", "no auth", 2),
                    "codex-2": ("HEALTHY", "ok", 3)}
        got, canary = self.reduce(outcomes)
        self.assertEqual(canary.call_count, 2)
        family = got["codex"]
        self.assertEqual(family["state"], "HEALTHY")
        self.assertFalse(family["dark"])
        self.assertEqual(family["ms"], 5)
        self.assertEqual(family["members"],
                         {"codex": "AUTH-UNAVAILABLE", "codex-2": "HEALTHY"})

    def test_unknown_sibling_blocks_a_false_family_dark_verdict(self):
        outcomes = {"codex": ("AUTH-401", "bad", 2),
                    "codex-2": ("UNKNOWN", "blind", 3)}
        family = self.reduce(outcomes)[0]["codex"]
        self.assertEqual(family["state"], "UNKNOWN")
        self.assertFalse(family["dark"])

    def test_every_sibling_runs_and_unknown_blocks_a_healthy_recovery(self):
        rows = self.rows() + [row(seat="codex-3", family="codex",
                                  probe="healthy")]
        outcomes = {"codex": ("AUTH-401", "bad", 2),
                    "codex-2": ("HEALTHY", "ok", 3),
                    "codex-3": ("UNKNOWN", "blind", None)}
        got, canary = self.reduce(outcomes, rows=rows)
        self.assertEqual({call.args[0] for call in canary.call_args_list},
                         {"codex", "codex-2", "codex-3"})
        family = got["codex"]
        self.assertEqual(family["state"], "UNKNOWN")
        self.assertFalse(family["dark"])
        self.assertEqual(family["ms"], 5)

    def test_different_readable_dark_causes_reduce_to_FAMILY_MIXED(self):
        outcomes = {"codex": ("AUTH-401", "bad", 2),
                    "codex-2": ("QUOTA-402", "quota", 3)}
        family = self.reduce(outcomes)[0]["codex"]
        self.assertEqual(family["state"], "FAMILY-MIXED")
        self.assertTrue(family["dark"])

    def test_unanimous_dark_keeps_the_named_cause(self):
        outcome = ("AUTH-UNAVAILABLE", "no auth", 2)
        family = self.reduce({"codex": outcome, "codex-2": outcome})[0]["codex"]
        self.assertEqual(family["state"], "AUTH-UNAVAILABLE")
        self.assertTrue(family["dark"])

    def test_no_locally_healthy_carrier_spends_no_canary(self):
        rows = [row(seat="codex", family="codex", probe="down")]
        family, canary = self.reduce({}, rows=rows)
        canary.assert_not_called()
        self.assertEqual(family["codex"]["state"], "UNKNOWN")
        self.assertEqual(family["codex"]["members"], {})

    def test_current_detail_and_ms_never_come_from_prior_state(self):
        prior = {"upstream": {"codex": {"state": "HEALTHY",
                                          "since": "old-since", "dark": False,
                                          "detail": "stale", "ms": 9999}}}
        family = self.reduce({"codex": ("HEALTHY", "current", 7)},
                             rows=[self.rows()[0]], prior=prior)[0]["codex"]
        self.assertEqual(family["since"], "old-since")
        self.assertEqual(family["detail"], "codex=HEALTHY (current)")
        self.assertEqual(family["ms"], 7)

    def test_unmeasured_result_keeps_latency_unknown_not_zero(self):
        family = self.reduce({"codex": ("UNKNOWN", "token unavailable", None)},
                             rows=[self.rows()[0]])[0]["codex"]
        self.assertIsNone(family["ms"])

    def test_named_dark_change_resets_since_but_unknown_preserves_the_episode(self):
        prior = {"upstream": {"codex": {"state": "AUTH-401",
                                          "since": "episode-start", "dark": True}}}
        changed = self.reduce({"codex": ("QUOTA-402", "quota", 2),
                               "codex-2": ("QUOTA-402", "quota", 2)},
                              prior=prior)[0]["codex"]
        self.assertEqual(changed["since"], proxywatch._iso(1000))
        self.assertTrue(changed["dark"])
        unknown = self.reduce({"codex": ("UNKNOWN", "blind", 2)},
                              rows=[self.rows()[0]], prior=prior)[0]["codex"]
        self.assertEqual(unknown["since"], "episode-start")
        self.assertTrue(unknown["dark"])

    def test_missing_dark_family_is_preserved_as_latched_UNKNOWN(self):
        prior = {"upstream": {"codex": {"state": "AUTH-401",
                                          "since": "episode-start", "dark": True}}}
        got = proxywatch.upstream_health([], now=1000, prior=prior)["codex"]
        self.assertEqual(got["state"], "UNKNOWN")
        self.assertEqual(got["since"], "episode-start")
        self.assertTrue(got["dark"])
        self.assertEqual(got["members"], {})
        report = rep(upstream={"codex": got})
        hits = proxywatch.findings(report)
        self.assertTrue(hits)
        self.assertEqual(hits[0][0], "FAMILY-DARK")
        rendered = "\n".join(proxywatch.report_lines(report))
        self.assertIn("family:codex", rendered)
        self.assertIn("beacon=PAUSED-CRED-WALL", rendered)
        self.assertIn("current evidence is unreadable", rendered)


class FingerprintTest(unittest.TestCase):
    def test_the_same_health_fingerprints_the_same(self):
        self.assertEqual(proxywatch.fingerprint(rep(row())),
                         proxywatch.fingerprint(rep(row())))

    def test_AGE_ALONE_is_not_a_change(self):
        """THE LOAD-BEARING ONE. Transcript age moves every single pass by
        construction. Folding it into the fingerprint would make every pass a
        'change', every pass a message, and the whole watch noise inside a
        day."""
        self.assertEqual(proxywatch.fingerprint(rep(row(age=60))),
                         proxywatch.fingerprint(rep(row(age=99999))))

    def test_current_upstream_detail_ms_and_members_do_not_move_the_latch(self):
        a = {"codex": {"state": "HEALTHY", "dark": False,
                       "detail": "first", "ms": 2, "members": {"codex": "HEALTHY"}}}
        b = {"codex": {"state": "HEALTHY", "dark": False,
                       "detail": "second", "ms": 999,
                       "members": {"codex-2": "HEALTHY"}}}
        self.assertEqual(proxywatch.fingerprint(rep(row(), upstream=a)),
                         proxywatch.fingerprint(rep(row(), upstream=b)))

    def test_named_dark_causes_share_one_episode_bucket(self):
        a = {"codex": {"state": "AUTH-401", "dark": True}}
        b = {"codex": {"state": "FAMILY-MIXED", "dark": True}}
        healthy = {"codex": {"state": "HEALTHY", "dark": False}}
        self.assertEqual(proxywatch.fingerprint(rep(row(), upstream=a)),
                         proxywatch.fingerprint(rep(row(), upstream=b)))
        self.assertNotEqual(proxywatch.fingerprint(rep(row(), upstream=a)),
                            proxywatch.fingerprint(rep(row(), upstream=healthy)))

    def test_a_PROBE_STATE_CHANGE_moves_it(self):
        """Caught by a mutation that did not bite. Dropping probe state from
        the fingerprint left the detection intact and the ALERTING dead: a
        proxy going healthy -> down would be correctly detected, correctly
        turned into a finding, and never announced, because the state had not
        'moved'. A watch that sees a failure and stays quiet is worse than one
        that cannot see it, since its silence is trusted."""
        healthy = rep(dict(row(), probe="healthy"))
        for broken in ("down", "hang", "EMPTY200"):
            self.assertNotEqual(
                proxywatch.fingerprint(healthy),
                proxywatch.fingerprint(rep(dict(row(), probe=broken))),
                "%s must register as a change or nobody is ever told" % broken)

    def test_a_LOG_STATE_change_moves_it(self):
        """The CHANGED discipline for the starvation rung: ok→streak must post
        once, and streak→ok (recovery) once. Without this the two-day 402
        wall would be detected on every pass and announced on none."""
        base = proxywatch.fingerprint(rep(row(log="ok")))
        for other in ("streak", "unknown", "idle"):
            self.assertNotEqual(base,
                                proxywatch.fingerprint(rep(row(log=other))),
                                "log %s must register as a change" % other)

    def test_a_TURN_STATE_change_moves_it(self):  # noqa: VACUOUS_ASSERTION — assertNotEqual against a fixed base is the positive control; each turn value must MOVE the digest
        """The CHANGED discipline for the fused verdict: ok→hung must post
        once, and hung→ok (the resume worked) once. Without this the codex
        hang would be detected on every pass and announced on none — the
        exact silent-watch shape the probe-state mutation already taught."""
        base = proxywatch.fingerprint(rep(row(turn="ok")))
        for other in ("hung", "thinking", "hung-unknown", "starved", "fresh",
                      "idle"):
            self.assertNotEqual(base,
                                proxywatch.fingerprint(rep(row(turn=other))),
                                "turn %s must register as a change" % other)

    def test_config_hang_and_a_NEW_DROP_ALERT_each_move_it(self):
        base = proxywatch.fingerprint(rep(row()))
        self.assertNotEqual(base, proxywatch.fingerprint(rep(row(config_ok=False))))
        self.assertNotEqual(base, proxywatch.fingerprint(rep(row(hang=True))))
        self.assertNotEqual(base, proxywatch.fingerprint(rep(row(alerted=12345))),
                            "a new silent-drop alert is the signal the bench "
                            "lift said would put the bench back")


class ChangeLatchTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="helm-test-pw-")
        self._p = mock.patch.object(proxywatch, "_state_path",
                                    return_value=os.path.join(self.d, "s.json"))
        self._p.start()

    def tearDown(self):
        self._p.stop()
        shutil.rmtree(self.d, ignore_errors=True)

    def test_a_FIRST_run_on_a_healthy_fleet_stays_quiet(self):
        """Installing the timer must not announce itself. A watch whose first
        act is a message teaches its reader that its messages are routine."""
        moved, prev = proxywatch.changed(rep(row()))
        self.assertFalse(moved)
        self.assertIsNone(prev)

    def test_a_FIRST_run_WITH_a_finding_does_speak(self):
        moved, _ = proxywatch.changed(rep(row(config_ok=False, drift=["x"])))
        self.assertTrue(moved)

    def test_legacy_healthy_fingerprint_migrates_without_a_false_change(self):
        family = {"codex": {"state": "HEALTHY", "since": "now",
                            "dark": False, "detail": "ok", "ms": 2,
                            "seat": "codex", "members": {"codex": "HEALTHY"}}}
        report = rep(row(upstream="HEALTHY", upstream_since="now"),
                     upstream=family)
        legacy = {"fingerprint": proxywatch.fingerprint(
            report, include_upstream=False), "ts": report["ts"],
                  "pending_chat": [], "pending_ntfy": []}
        with open(proxywatch._state_path(), "w", encoding="utf-8") as f:
            json.dump(legacy, f)
        self.assertFalse(proxywatch.changed(report)[0])
        proxywatch.record(report, prior_state=legacy)
        with open(proxywatch._state_path(), encoding="utf-8") as f:
            self.assertIn("upstream", json.load(f))
        self.assertFalse(proxywatch.changed(report)[0])

    def test_an_UNCHANGED_state_stays_quiet_on_every_later_pass(self):
        r = rep(row(config_ok=False, drift=["x"]))
        proxywatch.record(r)
        for _ in range(3):
            moved, _ = proxywatch.changed(r)
            self.assertFalse(moved, "a standing problem must not re-announce "
                                    "every fifteen minutes")

    def test_RECOVERY_is_a_change_and_is_reported(self):
        """Returning to healthy is news too — otherwise the last thing anyone
        heard is the problem, forever."""
        proxywatch.record(rep(row(config_ok=False, drift=["x"])))
        moved, _ = proxywatch.changed(rep(row()))
        self.assertTrue(moved)

    def test_an_unwritable_state_file_never_blocks_the_watch(self):
        with mock.patch.object(proxywatch, "_state_path",
                               return_value="/nonexistent/x/s.json"):
            proxywatch.record(rep(row()))          # must not raise


class UpstreamTransitionTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="helm-test-pwup-")
        self._p = mock.patch.object(proxywatch, "_state_path",
                                    return_value=os.path.join(self.d, "s.json"))
        self._p.start()

    def tearDown(self):
        self._p.stop()
        shutil.rmtree(self.d, ignore_errors=True)

    @staticmethod
    def report(state, since="2026-07-31T00:00:00Z", dark=None):
        dark = state in proxywatch._UPSTREAM_DARK if dark is None else dark
        family = {"state": state, "since": since, "dark": dark,
                  "detail": "current evidence", "ms": 7, "seat": "codex",
                  "members": {"codex": state}}
        return rep(row(upstream=state, upstream_since=since,
                       upstream_detail="current evidence", upstream_ms=7),
                   upstream={"codex": family})

    def test_dark_alerts_once_recovery_rearms_and_alerts_once(self):
        dark = self.report("UPSTREAM-OVERLOADED")
        self.assertEqual([x["kind"] for x in proxywatch.upstream_transitions(dark)],
                         ["family-dark"])
        proxywatch.record(dark)
        self.assertEqual(proxywatch.upstream_transitions(dark), [])
        recovered = self.report("HEALTHY", "2026-07-31T01:00:00Z", dark=False)
        self.assertEqual([x["kind"] for x in
                          proxywatch.upstream_transitions(recovered)],
                         ["family-recovered"])
        proxywatch.record(recovered)
        dark_again = self.report("AUTH-401", "2026-07-31T02:00:00Z")
        self.assertEqual([x["kind"] for x in
                          proxywatch.upstream_transitions(dark_again)],
                         ["family-dark"])

    def test_unknown_during_darkness_does_not_prove_recovery(self):
        proxywatch.record(self.report("AUTH-401"))
        unknown = self.report("UNKNOWN", dark=True)
        self.assertEqual(proxywatch.upstream_transitions(unknown), [])
        proxywatch.record(unknown)
        recovered = self.report("HEALTHY", "2026-07-31T01:00:00Z", dark=False)
        self.assertEqual([x["kind"] for x in
                          proxywatch.upstream_transitions(recovered)],
                         ["family-recovered"])

    def test_unknown_persists_the_last_named_dark_state_for_later_since_reset(self):
        proxywatch.record(self.report("AUTH-401", "episode-start"))
        unknown = self.report("UNKNOWN", "episode-start", dark=True)
        proxywatch.record(unknown)
        prior, err = proxywatch._read_watch_state()
        self.assertIsNone(err)
        self.assertEqual(prior["upstream"]["codex"]["state"], "UNKNOWN")
        self.assertEqual(prior["upstream"]["codex"]["last_dark_state"],
                         "AUTH-401")
        rows = [row(seat="codex", family="codex", probe="healthy")]
        with mock.patch.object(proxywatch, "upstream_canary",
                               return_value=("QUOTA-402", "quota", 2)):
            changed = proxywatch.upstream_health(rows, now=2000, prior=prior)
        self.assertEqual(changed["codex"]["since"], proxywatch._iso(2000))

    def test_record_persists_only_episode_memory_not_current_evidence(self):
        report = self.report("HEALTHY", dark=False)
        self.assertTrue(proxywatch.record(report))
        with open(proxywatch._state_path(), encoding="utf-8") as f:
            saved = json.load(f)["upstream"]["codex"]
        self.assertEqual(saved, {"state": "HEALTHY",
                                 "since": "2026-07-31T00:00:00Z",
                                 "dark": False})

    def test_one_family_dark_finding_is_emitted_for_multiple_instances(self):
        family = self.report("AUTH-401")["upstream"]
        report = rep(row(seat="codex", upstream="AUTH-401"),
                     row(seat="codex-2", upstream="AUTH-401"),
                     upstream=family)
        got = [finding for finding in proxywatch.findings(report)
               if finding[0] == "FAMILY-DARK"]
        self.assertEqual(len(got), 1)
        self.assertIn("AUTH-401", got[0][1])

    def test_named_dark_change_does_not_repeat_the_transition_or_fingerprint(self):
        first = self.report("AUTH-401")
        proxywatch.record(first)
        changed = self.report("QUOTA-402", "2026-07-31T00:10:00Z")
        self.assertEqual(proxywatch.upstream_transitions(changed), [])
        self.assertFalse(proxywatch.changed(changed)[0])

    def test_first_HEALTHY_is_a_baseline_not_a_recovery_transition(self):
        self.assertEqual(proxywatch.upstream_transitions(
            self.report("HEALTHY", dark=False)), [])

    def test_failed_phone_dark_then_recovery_delivers_both_without_chat_repeat(self):
        """The phone leg's at-least-once walk: pass 1's push fails and the
        dark edge stays queued; pass 2's push delivers the stuck dark AND the
        fresh recovery in one batch, while chat posts exactly once per edge."""
        dark = self.report("UPSTREAM-OVERLOADED")
        recovered = self.report("HEALTHY", "2026-07-31T01:00:00Z", dark=False)
        pushed = []

        def push(transitions):
            pushed.append(list(transitions))
            return len(pushed) > 1

        with mock.patch.object(proxywatch, "health",
                               side_effect=(dark, recovered)), \
                mock.patch.object(proxywatch, "_owner_push",
                                  side_effect=push), \
                mock.patch("helm.chat.post") as chat:
            self.assertEqual(proxywatch.cmd_proxywatch(["--post"]), 1)
            self.assertEqual(proxywatch.cmd_proxywatch(["--post"]), 0)
        self.assertEqual([t["kind"] for t in pushed[0]], ["family-dark"])
        self.assertEqual([t["kind"] for t in pushed[1]],
                         ["family-dark", "family-recovered"])
        self.assertEqual(chat.call_count, 2,
                         "dark and recovery each post once")
        with open(proxywatch._state_path(), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["pending_ntfy"], [])

    def test_owner_push_is_one_batch_and_opt_in(self):
        transitions = [{"kind": "family-dark", "family": "codex",
                        "state": "UPSTREAM-OVERLOADED"},
                       {"kind": "family-recovered", "family": "kimi",
                        "state": "HEALTHY"}]

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        with mock.patch.dict(os.environ, {"HELM_NTFY_TOPIC": "helm-fleet"}), \
                mock.patch("urllib.request.urlopen",
                           return_value=Response()) as open_:
            self.assertTrue(proxywatch._owner_push(transitions))
        self.assertEqual(open_.call_count, 1)
        req = open_.call_args[0][0]
        self.assertEqual(req.full_url, "https://ntfy.sh/helm-fleet")
        self.assertIn(b"codex dark", req.data)
        self.assertIn(b"kimi recovered", req.data)

    def test_owner_push_without_a_topic_is_a_deliberate_opt_out(self):  # noqa: VACUOUS_ASSERTION — assertTrue(returns True) is the positive claim; the not-called arm is the opt-out contract, and test_owner_push_is_one_batch_and_opt_in proves the same batch posts with a topic
        """No HELM_NTFY_TOPIC acknowledges the batch (True) without a network
        call — the positive-control twin above proves the same batch DOES post
        when the topic exists."""
        transitions = [{"kind": "family-dark", "family": "codex",
                        "state": "AUTH-401"}]
        with mock.patch.dict(os.environ, {"HELM_NTFY_TOPIC": "",
                                          "MELD_NTFY_TOPIC": ""}), \
                mock.patch("urllib.request.urlopen") as open_:
            self.assertTrue(proxywatch._owner_push(transitions))
        open_.assert_not_called()

    def test_owner_push_failure_returns_False_for_the_outbox_retry(self):  # noqa: VACUOUS_ASSERTION — assertFalse IS the observable under test (the outbox retry signal); the delivery positive control is test_owner_push_is_one_batch_and_opt_in
        transitions = [{"kind": "family-dark", "family": "codex",
                        "state": "AUTH-401"}]
        with mock.patch.dict(os.environ, {"HELM_NTFY_TOPIC": "helm-fleet"}), \
                mock.patch("urllib.request.urlopen",
                           side_effect=OSError("no route")):
            self.assertFalse(proxywatch._owner_push(transitions))

    def test_merge_transitions_deduplicates_a_stuck_edge_against_a_fresh_one(self):
        stuck = [{"kind": "family-dark", "family": "codex",
                  "state": "AUTH-401", "since": "t0"}]
        fresh = [{"kind": "family-dark", "family": "codex",
                  "state": "AUTH-401", "since": "t0"},
                 {"kind": "family-recovered", "family": "kimi",
                  "state": "HEALTHY", "since": "t1"}]
        merged = proxywatch._merge_transitions(stuck, fresh)
        self.assertEqual([(t["kind"], t["family"]) for t in merged],
                         [("family-dark", "codex"),
                          ("family-recovered", "kimi")])


class TimerUnitTest(unittest.TestCase):
    def test_default_cadence_is_the_installed_fifteen_minutes(self):
        """900s mirrors the INSTALLED helm-proxywatch.timer — installed is
        truth, the cadence is operational (owner ruling)."""
        self.assertEqual(proxywatch.INTERVAL_S, 900)
        _sp, _service, _tp, timer = proxywatch.timer_units()
        self.assertIn("OnUnitActiveSec=900s", timer)

    def test_template_cadence_equals_the_constant(self):
        """The guard: the SHIPPED template parses back to INTERVAL_S for
        BOTH cadence fields, so the next installed-truth change is ONE edit.
        Host state (~/.config) is deliberately out of scope — the suite pins
        only the internal template==constant agreement."""
        _sp, _service, _tp, timer = proxywatch.timer_units()
        self.assertIn("OnBootSec=%ds\n" % proxywatch.INTERVAL_S, timer)
        self.assertIn("OnUnitActiveSec=%ds\n" % proxywatch.INTERVAL_S, timer)
        # and these are the ONLY cadence fields — a stray hardcoded Sec=
        # line beside the parsed pair would be exactly the drift this pins.
        self.assertEqual(timer.count("Sec="), 2, timer)

    def test_the_unit_calls_the_POSTING_form(self):
        """A timer that ran the read-only form would compute the answer and
        tell nobody — the exact dead-scaffolding shape helm wiring exists to
        catch, arriving via systemd instead of via an import."""
        _sp, service, _tp, timer = proxywatch.timer_units(900)
        self.assertIn("proxywatch --post", service)
        self.assertIn("OnUnitActiveSec=900s", timer)

    def test_the_unit_uses_an_ABSOLUTE_helm_path(self):
        """A persistent unit must never capture a disposable worktree's PATH —
        seat.py's standing law, and the reason its own timer hardcodes the
        binary."""
        _sp, service, _tp, _t = proxywatch.timer_units()
        self.assertIn("/.local/bin/helm", service)

    def test_a_nonsense_interval_is_refused(self):
        ok, err = proxywatch.ensure_timer(0)
        self.assertFalse(ok)
        self.assertIn("at least 1 second", err)

    def test_found_faults_reads_as_SUCCESS_to_systemd(self):
        """Exit 1 (faults FOUND) and exit 2 (the watchdog broke) were one
        signal in systemctl — this unit read `failed (exit-code 1)` for a
        full day while it was the only correct instrument. The unit
        must declare 1 a success so red means broken, and nothing else."""
        _sp, service, _tp, _t = proxywatch.timer_units()
        self.assertIn("SuccessExitStatus=1", service)


class CmdTest(unittest.TestCase):
    def test_exit_code_reports_findings(self):
        with mock.patch.object(proxywatch, "health", return_value=rep(row())):
            self.assertEqual(proxywatch.cmd_proxywatch([]), 0)
        with mock.patch.object(proxywatch, "health",
                               return_value=rep(row(config_ok=False, drift=["x"]))):
            self.assertEqual(proxywatch.cmd_proxywatch([]), 1)

    def test_an_unknown_flag_is_named(self):
        self.assertNotEqual(proxywatch.cmd_proxywatch(["--pst"]), 0)

    def test_a_failing_chat_post_never_wedges_the_watch(self):
        """It runs under systemd every fifteen minutes. A raise here is a unit that
        fails forever and a signal nobody gets. Exit 2, not 1: a watch that
        cannot reach its own alert surface is a BROKEN WATCHDOG, not a finding
        — 1 is declared success in the unit (SuccessExitStatus=1), so
        returning it here would paint a mute watchdog green."""
        with mock.patch.object(proxywatch, "health",
                               return_value=rep(row(config_ok=False, drift=["x"]))), \
                mock.patch.object(proxywatch, "changed", return_value=(True, None)), \
                mock.patch.object(proxywatch, "record", return_value=True), \
                mock.patch("helm.chat.post", side_effect=RuntimeError("down")):
            self.assertEqual(proxywatch.cmd_proxywatch(["--post"]), 2)


class OutboxTest(unittest.TestCase):
    """The durable alert outbox — at-least-once delivery, fail-closed persist,
    independent channel acknowledgement."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._state = os.path.join(self._td.name, "proxywatch.json")
        p = mock.patch.object(proxywatch, "_state_path",
                              return_value=self._state)
        self._spp = p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self._td.cleanup)

    def _detected(self):
        """A report with a finding — the shape that triggers a post."""
        return rep(row(config_ok=False, drift=["missing-keepalive"]))

    def _saved(self):
        with open(self._state, encoding="utf-8") as f:
            return json.load(f)

    def test_record_persists_pending_chat_and_returns_true(self):
        r = rep(row())
        self.assertTrue(proxywatch.record(r, pending_chat=["msg1", "msg2"]))
        saved = self._saved()
        self.assertEqual(saved["pending_chat"], ["msg1", "msg2"])
        self.assertIn("fingerprint", saved)

    def test_record_returns_false_on_write_failure(self):
        r = rep(row())
        with mock.patch("helm.pk.atomic_write", side_effect=OSError("disk")):
            self.assertFalse(proxywatch.record(r, pending_chat=["msg"]))

    def test_read_watch_state_returns_empty_tuple_on_missing_file(self):
        state, err = proxywatch._read_watch_state()
        self.assertEqual(state, {})
        self.assertIsNone(err)

    def test_read_watch_state_returns_error_string_on_corrupt_file(self):
        with open(self._state, "w") as f:
            f.write("{corrupt json[")
        state, err = proxywatch._read_watch_state()
        self.assertEqual(state, {})
        self.assertIn("unreadable", err)

    def test_read_watch_state_returns_pending_queue_on_valid_file(self):
        proxywatch.record(rep(row()), pending_chat=["prior"])
        state, err = proxywatch._read_watch_state()
        self.assertIsNone(err)
        self.assertEqual(state["pending_chat"], ["prior"])

    def test_corrupt_outbox_refuses_delivery(self):  # noqa: VACUOUS_ASSERTION — rc==2 is the positive control; refusal-to-run IS the claim
        """A corrupt outbox must NOT deliver — the queue is unreadable and
        treating it as {} converts at-least-once into a loss. Return 2 (the
        watchdog itself failed — distinct from 1, faults FOUND) and name the
        file path."""
        with open(self._state, "w") as f:
            f.write("{corrupt json[")
        with mock.patch.object(proxywatch, "health",
                               return_value=self._detected()) as health, \
                mock.patch.object(proxywatch, "changed",
                                  return_value=(True, None)), \
                mock.patch("helm.chat.post") as post_mock:
            rc = proxywatch.cmd_proxywatch(["--post"])
            self.assertEqual(rc, 2)
            # noqa: VACUOUS_ASSERTION — the rc==2 above is the positive
            # control; the refusal-to-run IS the claim under test.
            health.assert_not_called()
            post_mock.assert_not_called()

    def test_post_lock_precedes_authenticated_health(self):
        order = []
        real_flock = proxywatch.fcntl.flock

        def flock(fd, op):
            if op == proxywatch.fcntl.LOCK_EX:
                order.append("lock")
            return real_flock(fd, op)

        def health(**_kwargs):
            order.append("health")
            return rep(row())

        with mock.patch.object(proxywatch.fcntl, "flock", side_effect=flock), \
                mock.patch.object(proxywatch, "health", side_effect=health), \
                mock.patch("helm.chat.post"):
            proxywatch.cmd_proxywatch(["--post"])
        self.assertEqual(order[:2], ["lock", "health"])

    def test_cmd_fails_closed_when_initial_persist_fails(self):  # noqa: VACUOUS_ASSERTION — rc==2 is the positive control; fail-closed means nothing delivered
        """A failed initial write must send nothing — the alert must not be
        delivered to a channel without durable record of the edge."""
        with mock.patch.object(proxywatch, "health",
                               return_value=self._detected()), \
                mock.patch.object(proxywatch, "changed",
                                  return_value=(True, None)), \
                mock.patch.object(proxywatch, "record", return_value=False), \
                mock.patch("helm.chat.post") as post_mock:
            rc = proxywatch.cmd_proxywatch(["--post"])
            self.assertEqual(rc, 2)
            # noqa: VACUOUS_ASSERTION — rc==2 is the positive control; the
            # fail-closed claim is exactly that nothing was delivered.
            post_mock.assert_not_called()

    def test_delivered_message_is_acked(self):
        with mock.patch.object(proxywatch, "health",
                               return_value=self._detected()), \
                mock.patch.object(proxywatch, "changed",
                                  return_value=(True, None)), \
                mock.patch("helm.chat.post"):
            proxywatch.cmd_proxywatch(["--post"])
            saved = self._saved()
            self.assertEqual(saved["pending_chat"], [])

    def test_failed_delivery_preserves_message_for_retry(self):
        with mock.patch.object(proxywatch, "health",
                               return_value=self._detected()), \
                mock.patch.object(proxywatch, "changed",
                                  return_value=(True, None)), \
                mock.patch("helm.chat.post",
                           side_effect=RuntimeError("down")):
            proxywatch.cmd_proxywatch(["--post"])
            saved = self._saved()
            self.assertEqual(len(saved["pending_chat"]), 1)
            self.assertIn("proxy health CHANGED", saved["pending_chat"][0])

    def test_force_does_not_invent_an_upstream_transition(self):
        with mock.patch.object(proxywatch, "health", return_value=rep(row())), \
                mock.patch.object(proxywatch, "upstream_transitions",
                                  return_value=[]) as transitions, \
                mock.patch("helm.chat.post") as chat:
            proxywatch.cmd_proxywatch(["--force"])
        transitions.assert_called_once()
        body = chat.call_args.args[0]
        self.assertNotIn("FAMILY-DARK", body)
        self.assertNotIn("FAMILY-RECOVERED", body)
        self.assertIn("no alerting state", body)
        self.assertNotIn("returned to healthy", body)

    def test_a_failing_owner_push_queues_only_the_ntfy_channel(self):
        """Channel independence: chat delivered and acked, the phone did not
        — the final persisted state must keep ONLY the ntfy batch."""
        family = {"codex": {"state": "UPSTREAM-OVERLOADED", "dark": True,
                            "since": "2026-07-31T00:00:00Z",
                            "detail": "HTTP 503", "ms": 7, "seat": "codex",
                            "members": {"codex": "UPSTREAM-OVERLOADED"}}}
        dark = rep(row(upstream="UPSTREAM-OVERLOADED",
                       upstream_since="2026-07-31T00:00:00Z",
                       upstream_detail="HTTP 503"), upstream=family)
        with mock.patch.object(proxywatch, "health", return_value=dark), \
                mock.patch.object(proxywatch, "changed",
                                  return_value=(True, None)), \
                mock.patch.object(proxywatch, "_owner_push",
                                  return_value=False), \
                mock.patch.object(proxywatch, "record",
                                  return_value=True) as record, \
                mock.patch("helm.chat.post"):
            self.assertEqual(proxywatch.cmd_proxywatch(["--post"]), 1)
        final = record.call_args.kwargs
        self.assertEqual(final["pending_chat"], [])
        self.assertEqual([t["kind"] for t in final["pending_ntfy"]],
                         ["family-dark"])

    def test_a_failing_chat_post_queues_only_the_chat_channel(self):
        """The mirror arm: the phone acked its (empty) batch while chat kept
        its body."""
        with mock.patch.object(proxywatch, "health",
                               return_value=self._detected()), \
                mock.patch.object(proxywatch, "changed",
                                  return_value=(True, None)), \
                mock.patch.object(proxywatch, "record",
                                  return_value=True) as record, \
                mock.patch("helm.chat.post",
                           side_effect=RuntimeError("down")):
            self.assertEqual(proxywatch.cmd_proxywatch(["--post"]), 2)
        final = record.call_args.kwargs
        self.assertEqual(len(final["pending_chat"]), 1)
        self.assertEqual(final["pending_ntfy"], [])

    def test_ack_write_failure_exits_nonzero_and_is_loud(self):
        """A failed ack must not silently drop the row. Return 2 (the watchdog
        itself failed) and print the warning so the operator and the timer
        unit both see it."""
        with mock.patch.object(proxywatch, "health",
                               return_value=self._detected()), \
                mock.patch.object(proxywatch, "changed",
                                  return_value=(True, None)), \
                mock.patch("helm.chat.post"), \
                mock.patch.object(proxywatch, "record",
                                  side_effect=[True, False]):
            rc = proxywatch.cmd_proxywatch(["--post"])
            self.assertEqual(rc, 2)
            # First record call persisted the outbox (returned True)
            # chat.post succeeded (mocked silently)
            # Second record call (ack) failed (returned False) -> rc=2

    def test_multiple_deliveries_ack_independently(self):
        """Three bodies in the queue: first succeeds, second fails, third never
        tried because the failed one blocks (ordered delivery is conservative
        but safer: a failed delivery preserves all remaining items)."""
        with mock.patch.object(proxywatch, "health",
                               return_value=rep(row())), \
                mock.patch.object(proxywatch, "changed",
                                  return_value=(True, None)):
            # seed the prior state with a stuck body from last pass
            proxywatch.record(rep(row()), pending_chat=["stale"])
            # now: changed=True adds a new body -> ["stale", "new"]
            post_responses = [None, RuntimeError("second fails")]
            with mock.patch("helm.chat.post",
                            side_effect=post_responses):
                proxywatch.cmd_proxywatch(["--post"])
            saved = self._saved()
            # "stale" was popped (delivered), then "new" failed
            # -> remaining = ["new"]
            self.assertEqual(len(saved["pending_chat"]), 1)
            self.assertIn("proxy health CHANGED", saved["pending_chat"][0])

    def test_stale_pending_item_is_delivered_on_next_pass(self):
        """When a prior pass died between persist and ack, the next pass
        delivers the stale item. At-least-once: a retry beats a lost alert."""
        proxywatch.record(rep(row()), pending_chat=["stale"])
        with mock.patch.object(proxywatch, "health",
                               return_value=rep(row())), \
                mock.patch.object(proxywatch, "changed",
                                  return_value=(False, None)), \
                mock.patch("helm.chat.post") as post_mock:
            proxywatch.cmd_proxywatch(["--post"])
            post_mock.assert_called_once_with("stale", who="proxywatch",
                                              room="helm")
        saved = self._saved()
        self.assertEqual(saved["pending_chat"], [])

    def test_first_run_without_findings_posts_nothing(self):
        """Installing the timer on a healthy fleet must not announce itself."""
        with mock.patch.object(proxywatch, "health",
                               return_value=rep(row())), \
                mock.patch("helm.chat.post") as post_mock:
            proxywatch.cmd_proxywatch(["--post"])
            post_mock.assert_not_called()
            saved = self._saved()
            self.assertEqual(saved["pending_chat"], [])

    def test_lifecycle_walk(self):
        """One alert through the full outbox cycle: persist -> deliver -> ack."""
        # Pass 1: detection fires, persisted, delivered, acked
        with mock.patch.object(proxywatch, "health",
                               return_value=self._detected()), \
                mock.patch.object(proxywatch, "changed",
                                  return_value=(True, None)), \
                mock.patch("helm.chat.post") as post_mock:
            rc = proxywatch.cmd_proxywatch(["--post"])
            self.assertEqual(rc, 1)
            post_mock.assert_called_once()
        saved = self._saved()
        self.assertEqual(saved["pending_chat"], [])
        # Pass 2: quiet — no new edges
        with mock.patch.object(proxywatch, "health",
                               return_value=self._detected()), \
                mock.patch.object(proxywatch, "changed",
                                  return_value=(False, None)), \
                mock.patch("helm.chat.post") as post_mock:
            rc = proxywatch.cmd_proxywatch(["--post"])
            self.assertEqual(rc, 1)
            post_mock.assert_not_called()
        saved = self._saved()
        self.assertEqual(saved["pending_chat"], [])

    def test_process_death_between_persist_and_send_is_recovered(self):
        """If the process dies after persist but before delivery, the next pass
        picks up the pending queue and retries."""
        proxywatch.record(rep(row()), pending_chat=["orphaned"])
        with mock.patch.object(proxywatch, "health",
                               return_value=rep(row())), \
                mock.patch.object(proxywatch, "changed",
                                  return_value=(False, None)), \
                mock.patch("helm.chat.post") as post_mock:
            proxywatch.cmd_proxywatch(["--post"])
            # "orphaned" was delivered even though nothing changed this pass
            post_mock.assert_called_once_with("orphaned", who="proxywatch",
                                              room="helm")
        saved = self._saved()
        self.assertEqual(saved["pending_chat"], [])

    def test_process_death_between_send_and_ack_is_retried(self):
        """If the process dies after sending but before ack-persist, the next
        pass sees the pending item still queued and retries (possibly duplicate
        — that is the at-least-once contract)."""
        proxywatch.record(rep(row()), pending_chat=["delivered-not-acked"])
        with mock.patch.object(proxywatch, "health",
                               return_value=rep(row())), \
                mock.patch.object(proxywatch, "changed",
                                  return_value=(False, None)), \
                mock.patch("helm.chat.post") as post_mock:
            proxywatch.cmd_proxywatch(["--post"])
        self.assertEqual(post_mock.call_count, 1)
        # The item was delivered; verify it was also acked this time
        saved = self._saved()
        self.assertEqual(saved["pending_chat"], [])


class VendorResetTest(unittest.TestCase):
    """The owner-entered vendor-reset channel. A dark family with no
    recorded horizon reads as indefinitely broken; this is the door for the
    one person holding the provider's page. The provenance law underneath:
    the vendor reset and the measured proxy bench horizon are TWO clocks and
    never render as one."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self._cfg = os.path.join(self._td.name, "proxywatch-vendor-resets.json")
        p = mock.patch.object(proxywatch, "_vendor_resets_path",
                              return_value=self._cfg)
        p.start()
        self.addCleanup(p.stop)
        self._future = int((time.time() + 3600) * 1000)

    def test_set_show_clear_round_trips_with_provenance(self):
        rec, err = proxywatch.write_vendor_reset("codex", self._future)
        self.assertIsNone(err)
        self.assertEqual(rec["reset_kind"], "vendor")
        self.assertEqual(rec["reset_source"], "owner")
        self.assertTrue(rec.get("recorded_at"))
        table, err = proxywatch.read_vendor_resets()
        self.assertIsNone(err)
        self.assertEqual(table["codex"]["resets_at_ms"], self._future)
        removed, err = proxywatch.clear_vendor_reset("codex")
        self.assertTrue(removed)
        table, _ = proxywatch.read_vendor_resets()
        self.assertNotIn("codex", table)

    def test_a_past_horizon_is_refused_not_persisted(self):
        rec, err = proxywatch.write_vendor_reset("codex", 1000)
        self.assertIsNone(rec)
        self.assertIn("PAST", err)
        table, _ = proxywatch.read_vendor_resets()
        self.assertEqual(table, {})

    def test_a_malformed_config_is_a_diagnostic_never_a_silent_empty(self):
        with open(self._cfg, "w") as f:
            f.write("{corrupt[")
        table, err = proxywatch.read_vendor_resets()
        self.assertEqual(table, {})
        self.assertTrue(err, "corrupt config must answer with an error")
        # and a wrong-shaped file the same
        with open(self._cfg, "w") as f:
            f.write("[1,2,3]")
        table, err = proxywatch.read_vendor_resets()
        self.assertTrue(err)

    def test_a_proxywatch_pass_preserves_the_horizon(self):
        # THE PRODUCER ARM, driving the REAL composer the pass calls — never
        # a replicated copy (a copy can pass while the pass drops the field).
        proxywatch.write_vendor_reset("codex", self._future)
        rep = {"ts": "2026-08-06T08:00:00Z",
               "upstream": {"codex": {"state": "RATE-LIMITED", "dark": True,
                                      "since": "2026-08-06T07:00:00Z"}},
               "proxy_runtime": {}}
        upstream, verr = proxywatch._compose_upstream_records(rep, {})
        self.assertIsNone(verr)
        rec = upstream["codex"]
        self.assertEqual(rec["resets_at_ms"], self._future)
        self.assertEqual(rec["reset_kind"], "vendor")
        self.assertEqual(rec["reset_source"], "owner")
        # and the measured fields survive beside it — two clocks, one record
        self.assertEqual(rec["state"], "RATE-LIMITED")
        self.assertTrue(rec["dark"])
        # control on the same observable: with no owner input the axis is ABSENT
        proxywatch.clear_vendor_reset("codex")
        upstream, _ = proxywatch._compose_upstream_records(rep, {})
        self.assertNotIn("resets_at_ms", upstream["codex"],
                         "a family with no owner input must not gain one")

    def test_a_past_horizon_expires_at_READ_not_at_render(self):
        # A cross-family review P1: T-1s was composed and the UI rendered
        # "now" forever.
        # The read drops it; nothing downstream ever sees a past instant.
        past = int((time.time() - 5) * 1000)
        with open(self._cfg, "w") as f:
            json.dump({"codex": {"resets_at_ms": past, "reset_kind": "vendor",
                                 "reset_source": "owner",
                                 "recorded_at": "2026-08-06T07:00:00Z"}}, f)
        table, err = proxywatch.read_vendor_resets()
        self.assertIsNone(err)
        self.assertEqual(table, {}, "a past horizon must expire at read")
        # control: a future horizon in the SAME shape survives
        with open(self._cfg, "w") as f:
            json.dump({"codex": {"resets_at_ms": self._future,
                                 "reset_kind": "vendor",
                                 "reset_source": "owner",
                                 "recorded_at": "2026-08-06T07:00:00Z"}}, f)
        table, _ = proxywatch.read_vendor_resets()
        self.assertIn("codex", table)

    def test_a_corrupt_config_SURFACES_as_an_error_never_silent_absence(self):
        with open(self._cfg, "w") as f:
            f.write("{corrupt[")
        upstream, verr = proxywatch._compose_upstream_records(
            {"upstream": {"codex": {"state": "RATE-LIMITED", "dark": True,
                                    "since": "x"}}, "proxy_runtime": {}}, {})
        self.assertTrue(verr, "a corrupt config must surface, not swallow")
        self.assertNotIn("resets_at_ms", upstream["codex"])

    def test_set_and_clear_work_on_a_FRESH_home(self):
        # An independent review's FIX, hit on the owner's first use of the
        # verb: _vendor_config_lock's os.open(O_CREAT) made the FILE but never
        # the parent DIRECTORY, so a fresh home raised FileNotFoundError on
        # the one command this channel exists to provide. The class setUp
        # pre-creates the temp dir, which is why every earlier arm missed it —
        # point the config at a NOT-YET-EXISTING subdirectory to reproduce.
        fresh = os.path.join(self._td.name, "notyet", "deeper",
                             "proxywatch-vendor-resets.json")
        with mock.patch.object(proxywatch, "_vendor_resets_path",
                               return_value=fresh):
            rec, err = proxywatch.write_vendor_reset("codex", self._future)
            self.assertIsNone(err, err)
            self.assertEqual(rec["resets_at_ms"], self._future)
            table, rerr = proxywatch.read_vendor_resets()
            self.assertIn("codex", table)
            removed, cerr = proxywatch.clear_vendor_reset("codex")
            self.assertTrue(removed)
            table, _ = proxywatch.read_vendor_resets()
            self.assertNotIn("codex", table)

    def test_the_cli_verbs_are_REACHABLE(self):
        # A cross-family review P1, and the lesson of this lane: the helpers
        # were driven and the verb never was — guard_tail refused the whole
        # subverb family before the branch ran. Drive the actual entry point.
        import io
        import contextlib
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = proxywatch.cmd_proxywatch(["vendor-reset", "show"])
        self.assertEqual(rc, 0, err.getvalue())
        with contextlib.redirect_stderr(err):
            rc = proxywatch.cmd_proxywatch(["vendor-reset"])
        self.assertEqual(rc, 2)          # bare subverb: usage, reachable
        with contextlib.redirect_stderr(err):
            rc = proxywatch.cmd_proxywatch(["--post"])  # the flag path still routes
        self.assertIn(rc, (0, 1, 2))

    def test_a_stale_observer_never_gains_a_horizon_it_did_not_measure(self):  # noqa: VACUOUS_ASSERTION — the fresh-record block below is the unconditional positive control on the SAME observable (upstream_resets_at_ms annotates when fresh, with provenance), asserted before this arm's absence can pass vacuously
        # The horizon is an OWNER fact, but the ROSTER's stale rule is
        # untouched: a stale record withholds state AND horizon alike, because
        # a watcher that stopped writing cannot assert anything as "now".
        from helm import web_roster
        row = {"seat": "codex"}
        upstream = {"codex": {"state": "RATE-LIMITED", "dark": True,
                              "since": "2026-08-06T07:00:00Z",
                              "resets_at_ms": self._future,
                              "reset_kind": "vendor",
                              "reset_source": "owner"}}
        # stale: age past the freshness gate -> the horizon must NOT annotate
        with mock.patch.object(web_roster, "_UPSTREAM_STALE_S", 60):
            web_roster._annotate_upstream(
                row, upstream, 3600, None)
        self.assertNotIn("upstream_resets_at_ms", row,
                         "a stale record asserted a horizon")
        # the unconditional positive control on the SAME observable: a FRESH
        # record with the same horizon MUST annotate, or the absence above
        # proves nothing (the annotation path could be dead entirely).
        fresh = {"seat": "codex"}
        with mock.patch.object(web_roster, "_UPSTREAM_STALE_S", 60):
            web_roster._annotate_upstream(fresh, upstream, 30, None)
        self.assertEqual(fresh.get("upstream_resets_at_ms"), self._future,
                         "control: a fresh record annotates the horizon")
        self.assertEqual(fresh.get("upstream_reset_source"), "owner")


if __name__ == "__main__":
    unittest.main()


class FanoutDemandReadingTest(unittest.TestCase):
    """The DEMAND term — see proxywatch.fanout_reading.

    Every arm here is written so that DELETING THE FEATURE REDDENS IT. That is
    not a style note: the night this landed, a reviewer removed a memo-hit
    return from a neighbouring lane and all four of its arms still passed. A
    suite that cannot fail retires a question without answering it.
    """

    def _seat(self, root, name, ages):
        """A seat instance dir holding one subagent transcript per age."""
        d = os.path.join(root, name, "claude", "projects", "proj", "sess",
                         "subagents")
        os.makedirs(d)
        now = time.time()
        for i, age in enumerate(ages):
            p = os.path.join(d, "agent-a%d.jsonl" % i)
            with open(p, "w") as fh:
                fh.write("{}\n")
            os.utime(p, (now - age, now - age))
        return os.path.join(root, name)

    def test_counts_only_subagents_inside_the_window(self):
        """MUST-HIT and its discriminator in one arm: a dead probe returning 0
        everywhere passes an all-old fixture, so the FRESH count is what makes
        the zero mean MEASURED-NONE rather than BLIND."""
        from helm import proxywatch
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        busy = self._seat(root, "busy", [5, 30, 100])
        quiet = self._seat(root, "quiet", [400, 9000])
        mixed = self._seat(root, "mixed", [10, 4000, 20])
        self.assertEqual(proxywatch.fanout_reading(busy)["active"], 3)
        self.assertEqual(proxywatch.fanout_reading(quiet)["active"], 0)
        self.assertEqual(proxywatch.fanout_reading(mixed)["active"], 2)

    def test_unreadable_is_UNKNOWN_and_never_a_measured_zero(self):
        """An UNKNOWN that decays to 0 is the whole bug class: a controller
        would read 'no demand' from a directory it could not open.

        THE POSITIVE CONTROL RUNS FIRST AND IS UNCONDITIONAL. Two of the
        assertions below are ABSENCE assertions, and an absence passes just as
        happily against a reader that answers None to EVERYTHING — which is
        precisely what a swallowed exception produces. Proving the reader CAN
        return a live count, on the same observable, in the same pass, is what
        makes the two Nones mean REFUSED-TO-GUESS rather than BROKEN. helm's
        own vacuous-assertion rung caught this arm without the control, on the
        commit that introduced it.
        """
        from helm import proxywatch
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        live = self._seat(root, "live", [5, 5])
        self.assertEqual(proxywatch.fanout_reading(live)["active"], 2)
        self.assertIsNone(proxywatch.fanout_reading(None)["active"])
        self.assertIsNone(proxywatch.fanout_reading("")["active"])
        # A DIRECTORY THAT DOES NOT EXIST IS UNKNOWN. The first cut of this
        # arm asserted 0 here and reasoned that glob answers honestly on an
        # absent path — true, and the wrong answer anyway. A must-hit control
        # against a LIVE fleet found a seat with no instance dir at all,
        # reading as a confident 0 while the reader could not see the seat.
        gone = os.path.join(root, "no-such-seat")
        self.assertIsNone(proxywatch.fanout_reading(gone)["active"])
        # An instance root that EXISTS but has no claude/projects is ALSO
        # UNKNOWN — a layout we could not read, never an idle seat. THIS ARM
        # ASSERTED 0 HERE AND OUTLIVED ITS OWN CURE BY ONE COMMIT: a
        # cross-family review's fix to the reader (whose FIX said in as many
        # words that an existing-empty instance root should be UNKNOWN) landed
        # while the test kept pinning the pre-cure semantics. The lane then
        # gated RED on the stale expectation while trunk was innocent, and the
        # base-check could not attribute it — so a one-line stale assertion
        # held a five-lane batch.
        empty = os.path.join(root, "empty-seat")
        os.makedirs(empty)
        self.assertIsNone(proxywatch.fanout_reading(empty)["active"])
        # THE DISCRIMINATOR that keeps UNKNOWN from swallowing real zeros: a
        # READABLE projects root with no subagent transcripts is a MEASURED 0.
        # Without this arm a reader that answered None to everything would
        # satisfy every assertion above.
        quiet = os.path.join(root, "quiet-seat")
        os.makedirs(os.path.join(quiet, "claude", "projects", "p", "s"))
        self.assertEqual(proxywatch.fanout_reading(quiet)["active"], 0)

    def test_the_window_is_a_parameter_and_actually_moves_the_answer(self):
        """Pins that window_s is CONSULTED. A hardcoded 180 passes every arm
        above and fails this one."""
        from helm import proxywatch
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        seat = self._seat(root, "s", [50, 300, 1000])
        self.assertEqual(proxywatch.fanout_reading(seat)["active"], 1)
        self.assertEqual(
            proxywatch.fanout_reading(seat, window_s=500)["active"], 2)
        self.assertEqual(
            proxywatch.fanout_reading(seat, window_s=99999)["active"], 3)

    def test_every_unreadable_shape_is_UNKNOWN_not_a_confident_zero(self):
        """A cross-family review's exact-source probes, one arm per adjacent
        state.

        THE ROOT CAUSE THESE PIN: `glob` returns [] on an unreadable directory
        WITHOUT RAISING, so the first two cuts of this reader turned every
        permission and layout failure into a confident 0. The try/except that
        was supposed to catch it could never fire. Each shape below produced a
        measured zero before the scandir rewrite.
        """
        from helm import proxywatch
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)

        # POSITIVE CONTROL FIRST, unconditional: the reader can still count.
        live = self._seat(root, "live", [5, 5])
        self.assertEqual(proxywatch.fanout_reading(live)["active"], 2)

        # (a) instance root exists but has NO claude/projects — a layout we do
        #     not understand, not an idle seat.
        bare = os.path.join(root, "bare")
        os.makedirs(bare)
        self.assertIsNone(proxywatch.fanout_reading(bare)["active"])

        # (b) `claude` is a FILE, not a directory.
        filey = os.path.join(root, "filey")
        os.makedirs(filey)
        with open(os.path.join(filey, "claude"), "w") as fh:
            fh.write("x")
        self.assertIsNone(proxywatch.fanout_reading(filey)["active"])

        # (c) `projects` is a FILE.
        filey2 = os.path.join(root, "filey2")
        os.makedirs(os.path.join(filey2, "claude"))
        with open(os.path.join(filey2, "claude", "projects"), "w") as fh:
            fh.write("x")
        self.assertIsNone(proxywatch.fanout_reading(filey2)["active"])

        # (d) projects exists but is UNREADABLE. Root ignores mode bits, so
        #     this arm would pass vacuously as root — skip rather than lie.
        if os.geteuid() != 0:
            locked = self._seat(root, "locked", [5])
            pdir = os.path.join(locked, "claude", "projects")
            os.chmod(pdir, 0o000)
            self.addCleanup(os.chmod, pdir, 0o755)
            self.assertIsNone(proxywatch.fanout_reading(locked)["active"])

        # The positive control STILL holds after all of it — proving the
        # UNKNOWNs above are discrimination, not a reader stuck on None.
        self.assertEqual(proxywatch.fanout_reading(live)["active"], 2)

    def test_a_subagents_path_that_is_not_a_directory_is_UNKNOWN(self):
        """A cross-family review's catch — THE SAME SILENCE ONE LEVEL DOWN.

        `os.path.isdir(sub)` collapses THREE states into one False: the dir is
        ABSENT (legitimate — that session never fanned out), it EXISTS AS A
        FILE, or we CANNOT TELL. I had already replaced glob's silence at the
        projects level and at the entries level, then borrowed isdir's silence
        here — the third instance of one class inside one function.
        """
        from helm import proxywatch
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)

        # POSITIVE CONTROL, unconditional and first.
        live = self._seat(root, "live", [5])
        self.assertEqual(proxywatch.fanout_reading(live)["active"], 1)

        # ABSENT subagents dir is the LEGITIMATE case and must stay a
        # measured zero — a session that simply never fanned out.
        plain = os.path.join(root, "plain")
        os.makedirs(os.path.join(plain, "claude", "projects", "p", "s"))
        self.assertEqual(proxywatch.fanout_reading(plain)["active"], 0)

        # PRESENT BUT A FILE is a layout we do not understand -> UNKNOWN.
        filey = os.path.join(root, "filey")
        sess = os.path.join(filey, "claude", "projects", "p", "s")
        os.makedirs(sess)
        with open(os.path.join(sess, "subagents"), "w") as fh:
            fh.write("x")
        self.assertIsNone(proxywatch.fanout_reading(filey)["active"])

    def test_an_unreadable_session_dir_is_UNKNOWN(self):
        """A cross-family review FIX named TWO nested collapses: a subagents
        path that is a FILE, and an UNREADABLE SESSION DIR. I cured the first
        DELIBERATELY and the second FELL OUT of the same stat change — so it
        was correct with no arm pinning it, which is a cure nobody can defend
        and one refactor away from silently regressing. Probed live, then
        pinned.
        """
        if os.geteuid() == 0:
            self.skipTest("root ignores mode bits; this arm would pass vacuously")
        from helm import proxywatch
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        live = self._seat(root, "live", [5])            # positive control
        self.assertEqual(proxywatch.fanout_reading(live)["active"], 1)
        inst = os.path.join(root, "locked-session")
        sess = os.path.join(inst, "claude", "projects", "p", "s")
        os.makedirs(os.path.join(sess, "subagents"))
        os.chmod(sess, 0o000)
        self.addCleanup(os.chmod, sess, 0o755)
        self.assertIsNone(proxywatch.fanout_reading(inst)["active"])

    def test_a_dangling_subagents_symlink_is_UNKNOWN_not_absent(self):
        """A cross-family review's fourth boundary, MEASURED at 0 before the
        cure.

        os.stat FOLLOWS SYMLINKS, so a dangling `subagents` link raises ENOENT
        and read as "this session never fanned out". The link EXISTS — it is
        MALFORMED, which is a different fact from absence. lstat establishes
        the link's own existence before its target's.
        """
        from helm import proxywatch
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        live = self._seat(root, "live", [5])            # positive control
        self.assertEqual(proxywatch.fanout_reading(live)["active"], 1)
        inst = os.path.join(root, "dangling")
        sess = os.path.join(inst, "claude", "projects", "p", "s")
        os.makedirs(sess)
        os.symlink(os.path.join(sess, "nowhere"),
                   os.path.join(sess, "subagents"))
        self.assertIsNone(proxywatch.fanout_reading(inst)["active"])

    def test_a_DIRECTORY_named_like_a_transcript_is_not_a_subagent(self):
        """A cross-family review's fifth boundary, MEASURED at 1 before the
        cure.

        NAME IS NOT TYPE. `agent-*.jsonl` is a naming convention and nothing
        enforces it; a DIRECTORY with that name passed the filter and counted
        as a live subagent, inflating the demand reading a controller acts on.
        """
        from helm import proxywatch
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        live = self._seat(root, "live", [5])            # positive control
        self.assertEqual(proxywatch.fanout_reading(live)["active"], 1)
        inst = os.path.join(root, "dirnamed")
        sub = os.path.join(inst, "claude", "projects", "p", "s", "subagents")
        os.makedirs(os.path.join(sub, "agent-imposter.jsonl"))
        self.assertIsNone(proxywatch.fanout_reading(inst)["active"])

    def test_unreadable_project_and_subagents_dirs_are_UNKNOWN(self):
        """A cross-family review found: two OSError branches were CORRECT BUT
        UNPINNED. That is
        the state that bit us on the session dir — right by accident, one
        refactor from regressing, and nothing would say so."""
        if os.geteuid() == 0:
            self.skipTest("root ignores mode bits; this arm would pass vacuously")
        from helm import proxywatch
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        live = self._seat(root, "live", [5])            # positive control
        self.assertEqual(proxywatch.fanout_reading(live)["active"], 1)
        a = os.path.join(root, "lockedproj")
        proj = os.path.join(a, "claude", "projects", "p")
        os.makedirs(os.path.join(proj, "s"))
        os.chmod(proj, 0o000)
        self.addCleanup(os.chmod, proj, 0o755)
        self.assertIsNone(proxywatch.fanout_reading(a)["active"])
        b = self._seat(root, "lockedsub", [5])
        subdir = os.path.join(b, "claude", "projects", "proj", "sess",
                              "subagents")
        os.chmod(subdir, 0o000)
        self.addCleanup(os.chmod, subdir, 0o755)
        self.assertIsNone(proxywatch.fanout_reading(b)["active"])

    def test_a_dangling_MATCHED_agent_symlink_is_UNKNOWN(self):
        """A cross-family review's hand-back gap (1): the follow_symlinks=False
        guard on
        the ENTRY stat was UNPINNED. The mutation — e.stat(follow_symlinks=
        False) -> e.stat() — turns a dangling agent-*.jsonl link into
        FileNotFoundError, which the vanished-file skip then absorbs silently,
        and all thirteen fanout tests stayed green.

        A dangling transcript link is MALFORMED, not a subagent that finished
        while we walked. Those are different facts and only one of them is a
        legitimate skip.
        """
        from helm import proxywatch
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        live = self._seat(root, "live", [5])            # positive control
        self.assertEqual(proxywatch.fanout_reading(live)["active"], 1)
        inst = os.path.join(root, "danglingagent")
        sub = os.path.join(inst, "claude", "projects", "p", "s", "subagents")
        os.makedirs(sub)
        os.symlink(os.path.join(sub, "gone.jsonl"),
                   os.path.join(sub, "agent-dangling.jsonl"))
        self.assertIsNone(proxywatch.fanout_reading(inst)["active"])

    def test_an_empty_but_readable_projects_root_is_a_MEASURED_zero(self):
        """A cross-family review's proof gap (1): the
        0-from-an-empty-projects-root path had
        NO arm creating one, so the mutation `if not project_dirs: return
        UNKNOWN` left every other test green. A zero nobody pins is a zero
        nobody can defend."""
        from helm import proxywatch
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        live = self._seat(root, "live", [5])            # positive control
        self.assertEqual(proxywatch.fanout_reading(live)["active"], 1)
        bare = os.path.join(root, "bare")
        os.makedirs(os.path.join(bare, "claude", "projects"))
        self.assertEqual(proxywatch.fanout_reading(bare)["active"], 0)


class FanoutReachesTheOwnerSurfaceTest(unittest.TestCase):
    """A cross-family review's proof gap (2), and it is the one that matters.

    EVERY arm in FanoutDemandReadingTest calls `fanout_reading` DIRECTLY. So
    deleting the health() assignment, deleting the renderer, or eliding the
    field entirely leaves them all green — the reader would be perfect and
    UNWIRED, which is the exact built-but-never-surfaced failure this whole
    lane exists to cure for credential demand. The reader is the MECHANISM;
    the rendered line is the DELIVERABLE.
    """

    def test_the_rendered_seat_line_carries_fanout_in_all_three_states(self):
        from helm import proxywatch
        r = row()
        r["fanout"] = {"active": 3, "window_s": 180}
        self.assertIn(
            "fanout=3", "\n".join(proxywatch.report_lines(rep(r))),
            "a positive count must REACH the owner-facing line")
        r["fanout"] = {"active": None, "window_s": 180}
        self.assertIn(
            "fanout=UNKNOWN", "\n".join(proxywatch.report_lines(rep(r))),
            "UNKNOWN must be VISIBLE — a blind seat that renders as calm is "
            "the whole defect")
        r["fanout"] = {"active": 0, "window_s": 180}
        rendered = "\n".join(proxywatch.report_lines(rep(r)))
        self.assertNotIn(
            "fanout", rendered,
            "a measured zero stays SILENT — nine lines of fanout=0 every "
            "fifteen minutes is the attention-budget spam this avoids, and "
            "silence is only safe because UNKNOWN prints")
        # MUST-HIT CONTROL on the silence assertion: the same row DOES render
        # its other fields, so the absence above is discrimination and not an
        # empty render.
        self.assertIn("probe=", rendered)


class FanoutSurvivesTheWholeProductionPathTest(_HealthRig):
    """A cross-family review BLOCKER: the previous arm's docstring overclaimed.

    FanoutReachesTheOwnerSurfaceTest injects `row["fanout"]` by hand and calls
    report_lines, so it pins the RENDERER and nothing else — deleting the
    health() assignment left it green. The arm was titled "prove the
    DELIVERABLE, not just the mechanism" while proving ONE HOP OF TWO.

    This drives the REAL health() over a real tmpdir seat, through _HealthRig,
    and asserts the reading survives BOTH hops: health() -> row, and row ->
    rendered line. Deleting either end reddens it.
    """

    def _plant(self, count, age=5):
        sub = os.path.join(self.d, "claude", "projects", "p", "s", "subagents")
        os.makedirs(sub, exist_ok=True)
        now = time.time()
        for i in range(count):
            p = os.path.join(sub, "agent-p%d.jsonl" % i)
            with open(p, "w") as fh:
                fh.write("{}\n")
            os.utime(p, (now - age, now - age))

    def test_health_assigns_fanout_and_the_line_carries_it(self):
        from helm import proxywatch
        self._plant(2)
        row_out = self._health(live=True, age=60)
        # HOP ONE: health() actually computed and attached it.
        self.assertIsNotNone(row_out.get("fanout"),
                             "health() must ATTACH the demand reading")
        self.assertEqual(row_out["fanout"]["active"], 2)
        # HOP TWO: the same row, unmodified, reaches the owner-facing line.
        rendered = "\n".join(proxywatch.report_lines(rep(row_out)))
        self.assertIn("fanout=2", rendered)

    def test_a_seat_with_no_transcripts_is_a_measured_zero_end_to_end(self):  # noqa: VACUOUS_ASSERTION — the control is CROSS-ROW and the rung compares root variables: the same row shape with active=2 is rendered in the same pass and asserted to CARRY fanout=2, so a renderer that never emits the field fails HERE, not silently. A same-variable control (probe= on `rendered`) also proves the line rendered at all. Both are unconditional.
        """The discriminator: an existing readable layout with no subagents
        must render SILENT, not UNKNOWN — otherwise the end-to-end arm above
        would pass against a reader stuck on a constant."""
        from helm import proxywatch
        os.makedirs(os.path.join(self.d, "claude", "projects", "p", "s"),
                    exist_ok=True)
        row_out = self._health(live=True, age=60)
        self.assertEqual(row_out["fanout"]["active"], 0)
        rendered = "\n".join(proxywatch.report_lines(rep(row_out)))
        # POSITIVE CONTROL ON THE SAME OBSERVABLE, which `probe=` was not:
        # the very same row with a non-zero count DOES render the field, so
        # the silence below is the renderer discriminating rather than the
        # field being unreachable from this path. helm's vacuous-assertion
        # rung caught the weaker version on the commit that introduced it.
        loud = dict(row_out, fanout={"active": 2, "window_s": 180})
        self.assertIn("fanout=2",
                      "\n".join(proxywatch.report_lines(rep(loud))))
        self.assertNotIn("fanout", rendered)
        self.assertIn("probe=", rendered)      # the line DID render at all

    def test_an_UNKNOWN_reading_survives_row_text_AND_json(self):
        """A cross-family review's hand-back gap (2): the real-health E2E
        covered
        positive and zero only, so collapsing the health-layer UNKNOWN to 0 —
        or replacing the whole reading with fanout=None — left the direct
        reader tests and the hand-injected renderer arm green.

        THE ASYMMETRY THAT MAKES JSON LOAD-BEARING: the text renderer prints
        UNKNOWN for a MISSING fanout dict and for a MEASURED-unknown alike, so
        the line cannot tell them apart. `--json` dumps the row itself, where
        {"active": null, "window_s": 180} and a bare null are different facts.
        A controller reads the JSON. Pin all three.
        """
        from helm import proxywatch
        # The rig's seat dir exists but has NO claude/projects, so the reader
        # cannot see the seat — UNKNOWN by construction, through real health().
        row_out = self._health(live=True, age=60)
        self.assertIsNotNone(row_out.get("fanout"),
                             "health() must attach the reading even when it "
                             "cannot measure — a missing key and a measured "
                             "UNKNOWN are different facts")
        self.assertIsNone(row_out["fanout"]["active"])
        self.assertEqual(row_out["fanout"]["window_s"], 180)
        rendered = "\n".join(proxywatch.report_lines(rep(row_out)))
        self.assertIn("fanout=UNKNOWN", rendered)
        # THE JSON PATH, which cmd_proxywatch dumps verbatim: the window must
        # survive, not just the null.
        payload = json.loads(json.dumps({"report": rep(row_out)}))
        fan = payload["report"]["seats"][0]["fanout"]
        self.assertIsNone(fan["active"])
        self.assertEqual(fan["window_s"], 180)
