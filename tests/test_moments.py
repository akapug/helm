#!/usr/bin/env python3
"""helm.moments — the moment spine (trigger design lanes 1 and 2).

The arrival classifier on every kind's envelope, the ROUTES table's integrity,
the MISSED-MOMENT instrument's ability to FAIL (CRITIC BLOCK 1: a detector
that never fires must read RED, a signature the detector missed must read RED,
a window with a moment and no detection must read RED), the hook's latency
and typed-timeout bars, the payload probe on the shapes captured live from
Claude Code 2.1.281 (CRITIC BLOCK 4), and the machine-author set being the
one registry the tool-boundary delivery reads."""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import moments as M  # noqa: E402
from tests import _notices as N  # noqa: E402


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


class ClassifyTest(unittest.TestCase):
    """Every arrival kind off the harness's real envelope shapes."""

    def kind(self, text, seen=None, now=None):
        return M.classify(text, seen if seen is not None else {"pinned": "x"},
                          now).kind

    def test_each_kind_on_its_envelope(self):
        cases = (
            ("typed", "please rescue him", M.TYPED),
            ("peer wake", N.wake("ready for your verdict"), M.PEER),
            ("machine wake", N.wake("proxy health CHANGED", author="proxywatch"),
             M.MACHINE),
            ("expiry", N.monitor(N.EXPIRED), M.EXPIRED),
            ("hand-back", N.handback("built the lane"), M.HANDBACK),
            ("agent message", N.agent_message("LAND 12 is not clean"),
             M.AGENT_MSG),
            ("agent report", N.agent_done("two findings"), M.BG_RESULT),
            ("failed command", N.command_done("run the gate", code=1),
             M.BG_RESULT),
            ("own monitor line", N.monitor("gate: 12 OK", desc="gate"),
             M.BG_RESULT),
            ("clean command", N.command_done("wait for it"), M.EMPTY),
            ("report elsewhere", N.agent_done(N.DELIVERED), M.EMPTY),
            ("not yet", N.agent_done(N.NOT_YET), M.EMPTY),
        )
        self.assertEqual(self.kind(N.monitor(N.EXPIRED)), M.EXPIRED)  # control
        for label, text, want in cases:
            with self.subTest(kind=label):
                self.assertEqual(self.kind(text), want)

    def test_unseen_waiting_rows_are_never_assumed_machine(self):
        """CRITIC NIT 9: the drain coalesces a burst into ONE wake line, so a
        machine author with `+N waiting` hides authors nobody can see."""
        self.assertEqual(self.kind(N.wake("x", author="beacons")), M.MACHINE)
        self.assertEqual(self.kind(N.wake("x", author="beacons", waiting=3)),
                         M.PEER)

    def test_a_duplicate_needs_the_same_substance_in_this_context(self):
        text = N.handback("built the lane")
        fp = M.classify(text, {"pinned": "x"}).fingerprint
        self.assertEqual(self.kind(text, {"pinned": "x", "subs": [fp]}),
                         M.DUPLICATE)
        self.assertEqual(self.kind(text, {"pinned": "x", "subs": ["other"]}),
                         M.HANDBACK)
        # a person typing the same words twice is not a duplicate
        typed = "yes, go"
        tfp = M.fingerprint(typed)
        self.assertEqual(self.kind(typed, {"pinned": "x", "subs": [tfp]}),
                         M.TYPED)

    def test_a_replay_is_a_row_over_an_hour_old(self):
        now = time.time()
        fresh = N.wake("news", ts=iso(now - 60))
        self.assertEqual(self.kind(fresh, now=now), M.PEER)
        old = N.wake("news", ts=iso(now - M.REPLAY_AGE_S - 60))
        self.assertEqual(self.kind(old, now=now), M.REPLAY)
        mixed = N.monitor(N.wake_line("old", ts=iso(now - 2 * M.REPLAY_AGE_S))
                          + "\n" + N.wake_line("new", ts=iso(now - 60)))
        self.assertEqual(self.kind(mixed, now=now), M.PEER)

    def test_a_row_queued_behind_a_long_turn_is_news(self):
        """MUST-MISS: rows posted during a long turn are delivered one per
        wake after it, so the second is stamped before the turn that took the
        first began. Order is not staleness (185 E2 turns, two consensus
        positives, were silenced by that reading)."""
        now = time.time()
        queued = N.wake("the second row of the burst", ts=iso(now - 600))
        self.assertEqual(self.kind(queued, {"pinned": "x"}, now), M.PEER)

    def test_an_unreadable_stamp_is_never_silenced(self):
        line = N.wake_line("news", ts="not-a-time")
        self.assertEqual(self.kind(N.monitor(line)), M.PEER)

    def test_classify_never_raises(self):
        self.assertEqual(M.classify("hi").kind, M.TYPED)            # control
        for bad in (None, "", 12, "<task-notification>", "<agent-message"):
            with self.subTest(bad=repr(bad)):
                self.assertIn(M.classify(bad).kind, M.ARRIVALS)

    def test_first_context_is_the_absence_of_a_delivered_contract(self):
        self.assertTrue(M.classify("hi", None).first_context)
        self.assertTrue(M.classify("hi", {"pinned": None}).first_context)
        self.assertFalse(M.classify("hi", {"pinned": "fp"}).first_context)


class RoutesTableTest(unittest.TestCase):

    def test_ids_are_unique_and_every_live_arrival_row_has_a_detector(self):  # noqa: VACUOUS_ASSERTION — the unconditional assertIsNotNone(arrival.typed) and assertIsNone(act.land.compose) are the positive controls on the same detector field the loop reads
        ids = [r.id for r in M.ROUTES]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIsNotNone(M.BY_ID["arrival.typed"].detector)     # control
        self.assertIsNone(M.BY_ID["act.land.compose"].detector)
        for r in M.ROUTES:
            with self.subTest(route=r.id):
                self.assertTrue(r.status == M.LIVE or r.status.startswith("planned"))
                if r.status == M.LIVE and r.id != "jit":
                    self.assertIsNotNone(r.detector)
                if r.status != M.LIVE:
                    self.assertIsNone(r.detector)

    def test_every_arrival_kind_is_a_route_with_arms(self):
        self.assertIn("arrival.typed", M.ARMS)                      # control
        for kind in M.ARRIVALS:
            with self.subTest(kind=kind):
                rid = "arrival." + kind
                self.assertIn(rid, M.BY_ID)
                self.assertIn(rid, M.ARMS)

    def test_every_kind_has_a_policy_and_zero_bytes_means_fast_path(self):
        self.assertEqual(M.POLICY[M.EMPTY].cap, 0)                  # control
        self.assertEqual(M.POLICY[M.TYPED].jit, 4)
        for kind in M.ARRIVALS:
            with self.subTest(kind=kind):
                p = M.POLICY[kind]
                self.assertEqual(p.cap == 0, kind in M.FAST_PATH)
                self.assertIn(p.jit, (0, 4), "slots are never a rank cut")

    def test_the_result_rows_name_the_event_that_carries_them(self):
        """CRITIC BLOCK 4, MEASURED on 2.1.281: a conflicting merge exits 1
        and fires PostToolUseFailure; a no-match rg is a PostToolUse."""
        self.assertEqual(M.BY_ID["result.merge-conflict"].event,
                         "PostToolUseFailure")
        self.assertEqual(M.BY_ID["result.empty-scan"].event, "PostToolUse")


class MissedMomentMetricTest(unittest.TestCase):
    """CRITIC BLOCK 1: the metric must be able to FAIL."""

    def row(self, **kw):
        base = {"ts": iso(time.time() - 60), "timed_out": False,
                "fast_path": False, "sample": {"rendered_bytes": 0,
                                               "lane_bytes": {}}}
        base.update(kw)
        return base

    def test_the_live_arms_read_green(self):
        rep = M.report(inject_rows=[], moment_rows_=[])
        typed = {r["id"]: r for r in rep["routes"]}["arrival.typed"]
        self.assertEqual(typed["verdict"], "GREEN")                  # control
        self.assertEqual(rep["red"], 0)
        for r in rep["routes"]:
            if r["status"] == M.LIVE and r["arms"]:
                with self.subTest(route=r["id"]):
                    self.assertEqual(r["verdict"], "GREEN", r["why"])
                    self.assertEqual(r["arms_hit"], r["arms"])

    def test_a_detector_that_never_fires_reads_red(self):
        """THE PLANTED FAILURE: the same arms, a detector that is dead."""
        rep = M.report(inject_rows=[], moment_rows_=[],
                       detectors={"arrival.monitor-expired": lambda a: False})
        got = {r["id"]: r for r in rep["routes"]}["arrival.monitor-expired"]
        self.assertEqual(got["verdict"], "RED")
        self.assertIn("detected 0 < expected", got["why"][0])
        self.assertGreater(rep["red"], 0)

    def test_a_detector_that_fires_everywhere_reads_red(self):
        rep = M.report(inject_rows=[], moment_rows_=[],
                       detectors={"arrival.typed": lambda a: True})
        got = {r["id"]: r for r in rep["routes"]}["arrival.typed"]
        self.assertEqual(got["verdict"], "RED")
        self.assertTrue(any("must-miss" in w for w in got["why"]))

    def test_a_signature_the_detector_missed_reads_red(self):
        rows = [self.row(sig=["arrival.monitor-expired"], moments=[])]
        rep = M.report(inject_rows=rows, moment_rows_=[])
        got = {r["id"]: r for r in rep["routes"]}["arrival.monitor-expired"]
        self.assertEqual(got["verdict"], "RED")
        self.assertEqual((got["sig"], got["sig_hit"]), (1, 0))

    def test_a_signature_the_detector_caught_stays_green(self):
        rows = [self.row(sig=["arrival.monitor-expired"],
                         moments=["arrival.monitor-expired"])]
        moms = [{"ts": rows[0]["ts"], "route": "arrival.monitor-expired",
                 "outcome": M.DELIVERED}]
        rep = M.report(inject_rows=rows, moment_rows_=moms)
        got = {r["id"]: r for r in rep["routes"]}["arrival.monitor-expired"]
        self.assertEqual(got["verdict"], "GREEN", got["why"])

    def test_a_week_of_turns_with_no_live_detection_reads_red(self):
        rows = [self.row(sig=[], moments=[]) for _ in range(M.MIN_MEASURED)]
        rep = M.report(inject_rows=rows, moment_rows_=[])
        got = {r["id"]: r for r in rep["routes"]}["arrival.first-context"]
        self.assertEqual(got["verdict"], "RED")
        self.assertIn("0 live detections", got["why"][-1])

    def test_detected_but_not_delivered_reads_red(self):
        ts = iso(time.time() - 60)
        moms = [{"ts": ts, "route": "arrival.monitor-expired",
                 "outcome": M.NO_CONTENT}] * 3 \
            + [{"ts": ts, "route": "arrival.monitor-expired",
                "outcome": M.DELIVERED}]
        rep = M.report(inject_rows=[], moment_rows_=moms)
        got = {r["id"]: r for r in rep["routes"]}["arrival.monitor-expired"]
        self.assertEqual(got["verdict"], "RED")
        self.assertEqual(got["missed"], {M.NO_CONTENT: 3})

    def test_an_unbuilt_route_says_so(self):
        rep = M.report(inject_rows=[], moment_rows_=[])
        got = {r["id"]: r for r in rep["routes"]}["result.merge-conflict"]
        self.assertEqual(got["verdict"], "UNBUILT")


class HookHealthTest(unittest.TestCase):
    """CRITIC BLOCK 1: p95 wall and the typed-turn timeout rate, RED past
    their bars, and rows written before the fields exist count as UNKNOWN."""

    def row(self, arrival, wall, timed_out=False):
        return {"ts": iso(time.time()), "arrival": arrival, "wall_ms": wall,
                "timed_out": timed_out, "fast_path": False,
                "sample": {"rendered_bytes": 100, "lane_bytes": {"jit": 50}},
                "fired": {"jit": ["a"]}}

    def test_a_typed_timeout_past_the_bar_reads_red(self):
        rows = [self.row(M.TYPED, 500) for _ in range(20)] \
            + [self.row(M.TYPED, 8000, timed_out=True)]
        h = M.hook_health(rows)
        self.assertEqual(h["typed_timed_out"], 1)
        self.assertTrue(any("typed-turn timeout" in b for b in h["bars"]))

    def test_healthy_rows_pass_both_bars(self):
        h = M.hook_health([self.row(M.TYPED, 400) for _ in range(50)])
        self.assertEqual(h["typed_turns"], 50)                      # control
        self.assertEqual(h["bars"], [])
        self.assertEqual(h["typed_timeout_rate"], 0.0)

    def test_p95_past_the_bar_reads_red(self):
        h = M.hook_health([self.row(M.PEER, 5000) for _ in range(20)])
        self.assertTrue(any(b.startswith("p95 wall") for b in h["bars"]))

    def test_old_rows_are_unknown_never_clean(self):
        old = {"ts": iso(time.time()), "sample": {"rendered_bytes": 0}}
        h = M.hook_health([old])
        self.assertEqual(h["turns"], 1)
        self.assertEqual((h["measured"], h["unknown"]), (0, 1))

    def test_the_deadline_is_a_base_exception(self):
        """gather's lanes are fail-open with `except Exception`; a deadline
        they swallowed would run the turn on into the wrapper's kill."""
        self.assertTrue(issubclass(M.Deadline, BaseException))
        self.assertFalse(issubclass(M.Deadline, Exception))
        self.assertLess(M.DEADLINE_S, 10)       # hooks.TIMEOUT_S

    def test_process_wall_is_measured(self):
        w = M.process_wall_ms()
        self.assertIsNotNone(w)
        self.assertGreater(w, 0)


class PayloadShapeTest(unittest.TestCase):
    """The payload probe on the shapes CAPTURED LIVE (Claude Code 2.1.281,
    scripts/probe-hook-payloads.sh): paths and ids invented,
    every key and the error text's structure as captured."""

    FAILURE = {
        "session_id": "s", "transcript_path": "/tmp/t.jsonl", "cwd": "/tmp/r",
        "prompt_id": "p", "permission_mode": "default",
        "effort": {"level": "medium"}, "hook_event_name": "PostToolUseFailure",
        "tool_name": "Bash",
        "tool_input": {"command": "git merge feature", "description": "d"},
        "tool_use_id": "t",
        "error": "Exit code 1\nAuto-merging f.txt\nCONFLICT (content): Merge "
                 "conflict in f.txt\nAuto-merging g.txt\nCONFLICT (content): "
                 "Merge conflict in g.txt\nAutomatic merge failed; fix "
                 "conflicts and then commit the result.",
        "is_interrupt": False, "duration_ms": 8480}
    NO_MATCH = {
        "session_id": "s", "hook_event_name": "PostToolUse", "tool_name": "Bash",
        "tool_input": {"command": "rg zzz ."},
        "tool_response": {"stdout": "", "stderr": "", "interrupted": False,
                          "isImage": False,
                          "returnCodeInterpretation": "No matches found",
                          "noOutputExpected": False}}
    STOP = {"session_id": "s", "hook_event_name": "Stop",
            "stop_hook_active": False,
            "last_assistant_message": "Waiting for the notification.",
            "background_tasks": [{"id": "b", "type": "shell",
                                  "status": "running"}],
            "session_crons": []}
    SUBAGENT = {"session_id": "s", "hook_event_name": "SubagentStart",
                "agent_id": "a1", "agent_type": "general-purpose"}
    BATCH = {"session_id": "s", "hook_event_name": "PostToolBatch",
             "tool_calls": [{"tool_name": "Bash", "tool_input": {},
                             "tool_use_id": "t1", "tool_response": "Exit code 1"},
                            {"tool_name": "Bash", "tool_input": {},
                             "tool_use_id": "t2",
                             "tool_response": "(Bash completed with no output)"}]}

    def test_a_conflicting_merge_carries_its_paths_in_error(self):
        s = M.payload_shape(self.FAILURE)
        self.assertEqual(s["event"], "PostToolUseFailure")
        self.assertEqual(s["conflict_paths"], 2)
        self.assertNotIn("tool_response", s["keys"])

    def test_a_no_match_scan_is_a_success_with_a_meaning(self):
        s = M.payload_shape(self.NO_MATCH)
        self.assertEqual(s["event"], "PostToolUse")
        self.assertEqual(s["rc_meaning"], "No matches found")

    def test_stop_subagent_and_batch_fields(self):
        stop = M.payload_shape(self.STOP)
        self.assertEqual((stop["background"], stop["background_n"]), (["shell"], 1))
        self.assertEqual(stop["last_len"], len(self.STOP["last_assistant_message"]))
        sub = M.payload_shape(self.SUBAGENT)
        self.assertEqual(sub["agent_type"], "general-purpose")
        self.assertEqual(M.payload_shape(self.BATCH)["calls"], ["Bash", "Bash"])

    def test_the_shape_carries_no_content(self):
        s = M.payload_shape(self.FAILURE)
        self.assertIn("error_len", s)                               # control
        blob = repr(s)
        self.assertIn("conflict_paths", blob)                       # control
        for text in ("Auto-merging", "git merge", "f.txt"):
            self.assertNotIn(text, blob)


class MachineAuthorsSyncTest(unittest.TestCase):
    """The arrival classifier and the tool-boundary delivery read ONE machine
    registry, so they cannot disagree about who a machine is. The walker that
    keeps the registry complete is tests/test_delivery_truth.py's
    test_every_label_helm_posts_under_is_a_known_subsystem."""

    def test_moments_reads_the_one_registry(self):
        from helm import machine_senders
        self.assertIs(M.MACHINE_AUTHORS, machine_senders.SUBSYSTEMS)
        self.assertIn("worktree-gc", M.MACHINE_AUTHORS)        # control
        self.assertNotIn("agent", M.MACHINE_AUTHORS)


if __name__ == "__main__":
    unittest.main()
