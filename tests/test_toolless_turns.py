#!/usr/bin/env python3
"""TOOLLESS — a proxied seat whose model stopped making tool calls (task/3533).

MEASURED ON THE LIVE FLEET: the cursor seat answered every turn for 30 hours
with text only. Its proxy.log carried one row per reply,

    ... POST "/v1/messages?beta=true" | stream_v1=committed
        terminal=message_stop text_bytes=1538 tool_uses=0

seven in a row, while the model's text said the tool runtime refused every
call. helm showed the seat idle the whole time. A working seat's rows carry
`tool_uses>=1` until the turn's final answer, which is text-only, so ONE
text-only reply is normal and a RUN of them is the broken shape.

The fixture rows below are the gin_logger shape `tests/test_proxywatch.py`
pins verbatim, with the delivery suffix the fork appends; only the counts and
timestamps vary. Seat names follow the house convention (seat-a), never a
live identity.
"""
import contextlib
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

# helm.seat seeds its impl modules' globals (seat_health's `codex_cred_state`
# among them), so it is imported before seat_health is patched.
from helm import dispatches, proxywatch, seat, seat_health, seat_usability  # noqa: E402,F401


def reply(ts, text_bytes, tool_uses, path="/v1/messages?beta=true"):
    """One completed agent request as the fork's gin_logger writes it."""
    return ('[%s] [8ed50493] [info ] [gin_logger.go:161] 200 |       '
            '12.345s |       127.0.0.1 | POST    "%s" | stream_v1=committed '
            'terminal=message_stop text_bytes=%d tool_uses=%d'
            % (ts, path, text_bytes, tool_uses))


def ts(minute, second=0):
    return "2026-09-27 %02d:%02d:%02d" % (10 + minute // 60, minute % 60,
                                          second)


def working_turn(start):
    """A normal turn: two tool-calling requests, then the text-only answer."""
    return [reply(ts(start), 0, 1), reply(ts(start + 1), 0, 2),
            reply(ts(start + 2), 812, 0)]


def cursor_shape(start=0, n=7):
    """One working turn, then `n` text-only replies five minutes apart — the
    30-hour incident, its turns spaced as a client's prompts are (the live
    rows were hours apart), never inside one fold window."""
    return working_turn(start) + [reply(ts(start + 10 + 5 * i), 1538, 0)
                                  for i in range(n)]


class _Log(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="helm-test-toolless-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)

    def log(self, lines):
        p = os.path.join(self.d, "proxy.log")
        with open(p, "w") as f:
            f.write("\n".join(lines) + "\n")
        return p


class TheLogCountsToollessTurns(_Log):
    """The count comes from the SAME reader proxywatch already runs."""

    def test_the_cursor_shape_reads_TOOLLESS(self):
        lines = cursor_shape()
        seen = proxywatch.toolless_observation(self.log(lines))
        self.assertEqual(seen["turns"], 7)
        self.assertIs(seen["toolless"], True)
        # SINCE is the first toolless turn, never the working turn's answer.
        self.assertEqual(seen["since"], ts(10))
        # The evidence is the newest row, verbatim.
        self.assertEqual(seen["evidence"], lines[-1])
        # And the owner surface carries the same reading.
        observed = proxywatch.log_observation(self.log(lines))
        self.assertEqual(observed["toolless"]["turns"], 7)

    def test_a_normal_working_log_does_not_read_TOOLLESS(self):
        lines = []
        for turn in range(5):
            lines += working_turn(turn * 10)
        seen = proxywatch.toolless_observation(self.log(lines))
        # The one text-only reply per turn is that turn's final answer.
        self.assertEqual(seen["turns"], 0)
        self.assertIs(seen["toolless"], False)

    def test_ONE_tool_call_clears_it(self):
        lines = cursor_shape()
        self.assertTrue(proxywatch.toolless_observation(
            self.log(lines))["toolless"])
        cleared = lines + working_turn(100)
        seen = proxywatch.toolless_observation(self.log(cleared))
        self.assertEqual(seen["turns"], 0)
        self.assertIs(seen["toolless"], False)
        # A turn still in progress, its tool call the newest row, clears too.
        live = lines + [reply(ts(100), 0, 1)]
        self.assertEqual(proxywatch.toolless_observation(
            self.log(live))["turns"], 0)

    def test_the_bar_is_three_turns(self):
        self.assertEqual(proxywatch.TOOLLESS_N, 3)
        two = proxywatch.toolless_observation(self.log(cursor_shape(n=2)))
        self.assertEqual((two["turns"], two["toolless"]), (2, False))
        three = proxywatch.toolless_observation(self.log(cursor_shape(n=3)))
        self.assertEqual((three["turns"], three["toolless"]), (3, True))

    def test_a_run_with_nothing_before_it_does_not_count_its_first_reply(self):
        """With no tool-using reply in the tail, the oldest text-only reply
        may be the answer of a turn the tail cut off, so it is not counted."""
        only = [reply(ts(10 + i), 1538, 0) for i in range(7)]
        seen = proxywatch.toolless_observation(self.log(only))
        self.assertEqual(seen["turns"], 6)
        self.assertEqual(seen["since"], ts(11))
        self.assertIs(seen["toolless"], True)

    def test_EMPTY_replies_stay_the_silent_drop_watchdogs(self):
        """text_bytes=0 tool_uses=0 is the drop class, already counted by the
        empty-turn census and the silent-drop rung. It neither counts toward
        TOOLLESS nor clears it."""
        empties = working_turn(0) + [reply(ts(10 + i), 0, 0)
                                     for i in range(7)]
        observed = proxywatch.log_observation(self.log(empties))
        self.assertEqual(observed["toolless"]["turns"], 0)
        self.assertIs(observed["toolless"]["toolless"], False)
        # The empty-turn census still owns them: the control that this log
        # carries the drop shape and was not simply unread.
        self.assertEqual(
            observed["empty_turns"]["zero_delivery_unflagged"], 7)
        # Interleaved with the cursor shape, the empties change nothing.
        mixed = cursor_shape()
        mixed = mixed[:5] + [reply(ts(12), 0, 0)] + mixed[5:] + \
            [reply(ts(30), 0, 0)]
        self.assertEqual(proxywatch.toolless_observation(
            self.log(mixed))["turns"], 7)

    def test_canary_rows_are_not_turns(self):
        lines = working_turn(0) + [
            reply(ts(10 + i), 40, 0,
                  path="/v1/messages?beta=true&helm_canary=1")
            for i in range(7)]
        self.assertEqual(proxywatch.toolless_observation(
            self.log(lines))["turns"], 0)

    def test_an_UNINSTRUMENTED_or_unreadable_log_is_None_never_zero(self):
        bare = ('[2026-09-27 10:00:00] [a2f8ba2a] [info ] [gin_logger.go:95] '
                '200 |        1.255s |       127.0.0.1 | POST    '
                '"/v1/messages?beta=true"')
        self.assertIsNone(proxywatch.toolless_observation(
            self.log([bare] * 7)))
        self.assertIsNone(proxywatch.toolless_observation(
            os.path.join(self.d, "absent.log")))
        self.assertIsNone(proxywatch.log_observation(
            os.path.join(self.d, "absent.log"))["toolless"])


class ConcurrentTrafficIsNotATurn(_Log):
    """Every text-only reply is NOT the end of a client turn. Parallel
    subagents each end on a text-only final before the main agent answers,
    and the harness's own background small-model calls (a session title, a
    topic check) are text-only replies on the same proxy. Replies that
    complete within TOOLLESS_FOLD_S of each other are one turn, and a run
    must span TOOLLESS_MIN_SPAN_S of wall time before it trips."""

    def test_a_parallel_subagent_tail_is_one_turn(self):
        # The main agent fans out three subagents; each calls tools and ends
        # on a text-only final seconds apart, then the main agent answers.
        lines = working_turn(0) + [
            reply(ts(10), 0, 3),                      # main: three Task calls
            reply(ts(10, 5), 0, 1), reply(ts(10, 9), 0, 1),
            reply(ts(10, 12), 0, 2), reply(ts(12, 30), 0, 1),
            reply(ts(13, 0), 640, 0),                 # subagent A final
            reply(ts(13, 4), 702, 0),                 # subagent B final
            reply(ts(13, 9), 588, 0),                 # subagent C final
            reply(ts(13, 30), 1204, 0)]               # the main answer
        seen = proxywatch.toolless_observation(self.log(lines))
        self.assertEqual(seen["turns"], 0, seen)
        self.assertIs(seen["toolless"], False)

    def test_a_background_short_reply_beside_a_real_turn_is_not_a_turn(self):
        # Two ordinary question-and-answer turns with no tool call (a real,
        # sub-bar run), each with a title/topic call beside it.
        lines = working_turn(0) + [
            reply(ts(20), 48, 0), reply(ts(20, 6), 930, 0),
            reply(ts(30), 31, 0), reply(ts(30, 8), 1110, 0)]
        seen = proxywatch.toolless_observation(self.log(lines))
        self.assertEqual(seen["turns"], 2, seen)
        self.assertIs(seen["toolless"], False)
        # A third such turn crosses the bar: folding never hides the broken
        # shape, it only stops concurrent replies counting twice.
        more = lines + [reply(ts(40), 40, 0), reply(ts(40, 7), 990, 0)]
        seen = proxywatch.toolless_observation(self.log(more))
        self.assertEqual(seen["turns"], 3, seen)
        self.assertIs(seen["toolless"], True)
        self.assertEqual(seen["since"], ts(20))

    def test_a_run_shorter_than_the_minimum_span_does_not_trip(self):
        # Four text-only replies just outside the fold window of each other:
        # three turns by count, but two minutes of wall time is not a seat
        # that stopped working.
        gap = proxywatch.TOOLLESS_FOLD_S + 5
        lines = working_turn(0) + [
            reply(ts(10 + (gap * i) // 60, (gap * i) % 60), 1538, 0)
            for i in range(3)]
        seen = proxywatch.toolless_observation(self.log(lines))
        self.assertEqual(seen["turns"], 3, seen)
        self.assertLess(seen["span_s"], proxywatch.TOOLLESS_MIN_SPAN_S)
        self.assertIs(seen["toolless"], False)

    def test_the_window_and_span_are_named_constants(self):
        self.assertEqual(proxywatch.TOOLLESS_FOLD_S, 45)
        self.assertEqual(proxywatch.TOOLLESS_MIN_SPAN_S, 300)


def _row(seat="seat-a", family="cursor", toolless=None, **over):
    row = {"seat": seat, "family": family, "config_ok": True, "drift": [],
           "transcript_age_s": 60, "pane_live": True, "alerted_at": None,
           "hang_candidate": False, "log": "ok", "log_detail": None,
           "probe": None, "probe_detail": None, "turn_state": "ok",
           "turn_evidence": None, "liveness": None, "upstream": None,
           "upstream_detail": None, "upstream_ms": None,
           "upstream_since": None, "log_toolless": toolless,
           "census_blind": None, "error": None}
    row.update(over)
    return row


def _rep(*rows):
    return {"ts": 1000, "seats": list(rows), "upstream": {},
            "proxy_runtime": {}}


class TheHealthSurfaceFailsLoud(_Log):

    def reading(self, lines=None):
        return proxywatch.toolless_observation(self.log(lines or cursor_shape()))

    def test_the_finding_names_seat_count_since_and_evidence(self):
        reading = self.reading()
        found = [text for level, text in proxywatch.findings(
            _rep(_row(toolless=reading))) if level == "TOOLLESS"]
        self.assertEqual(len(found), 1, found)
        text = found[0]
        self.assertIn("seat-a", text)
        self.assertIn("7 consecutive turns", text)
        self.assertIn("since %s" % ts(10), text)
        self.assertIn(reading["evidence"], text)
        # And it rides the report the integrator reads.
        self.assertIn("TOOLLESS", "\n".join(proxywatch.report_lines(
            _rep(_row(toolless=reading)))))

    def test_CONTROL_a_working_seat_raises_no_finding(self):
        lines = []
        for turn in range(5):
            lines += working_turn(turn * 10)
        levels = [level for level, _t in proxywatch.findings(
            _rep(_row(toolless=self.reading(lines))))]
        self.assertNotIn("TOOLLESS", levels)

    def test_the_state_moves_the_change_latch(self):
        before = proxywatch.fingerprint(_rep(_row()))
        during = proxywatch.fingerprint(_rep(_row(toolless=self.reading())))
        self.assertNotEqual(before, during)
        # A cleared reading is the same shape as no reading: the latch speaks
        # once on entry and once on clearing, never every pass.
        cleared = proxywatch.toolless_observation(
            self.log(cursor_shape() + working_turn(100)))
        self.assertEqual(before, proxywatch.fingerprint(
            _rep(_row(toolless=cleared))))

    def test_only_a_family_with_tools_can_read_TOOLLESS(self):
        reading = self.reading()
        self.assertIsNotNone(proxywatch.toolless_reading(
            _row(toolless=reading)))
        self.assertIsNone(proxywatch.toolless_reading(
            _row(family="no-such-family", toolless=reading)))
        self.assertIsNone(proxywatch.toolless_reading(_row(family=None,
                                                           toolless=reading)))


def _join(hrow):
    return seat_usability.join(
        health=lambda **_kw: {"ts": 0, "seats": [hrow]},
        upstream=lambda: ({"cursor": {"state": "HEALTHY", "dark": False}},
                          None),
        open_recipients=lambda: ({}, None),
        register=lambda: {"seat-a": {"runtime_verified": True}},
        canonical=lambda name: (name, None),
        live_seats=lambda: ({"seat-a"}, None, {}),
        beacon_live=lambda name: ([], None))


class ATOOLLESSSeatTakesNoNewDispatch(_Log):
    """The existing broken-seat door: seat_usability's `can_take_work is
    False`, which the dispatch write door refuses on for a live pane."""

    def test_the_join_refuses_on_the_toolless_rung(self):
        reading = proxywatch.toolless_observation(self.log(cursor_shape()))
        row = _join(_row(toolless=reading))["seat-a"]
        self.assertEqual(row["verdict"], seat_usability.UNUSABLE)
        self.assertIs(row["can_take_work"], False)
        self.assertEqual(row["refusal"], seat_usability.REFUSE_TOOLLESS)
        self.assertIn("TOOLLESS", row["reason"])
        self.assertFalse(seat_usability.deaf_only(row))

    def test_CONTROL_a_working_seat_is_USABLE(self):
        lines = []
        for turn in range(5):
            lines += working_turn(turn * 10)
        reading = proxywatch.toolless_observation(self.log(lines))
        row = _join(_row(toolless=reading))["seat-a"]
        self.assertEqual(row["verdict"], seat_usability.USABLE, row["reason"])

    def test_the_dispatch_door_refuses_and_force_files(self):
        reading = proxywatch.toolless_observation(self.log(cursor_shape()))
        row = _join(_row(toolless=reading))["seat-a"]
        joined = (row["verdict"], row["reason"], row, None)
        ok, refusal, _warn = dispatches._recipient_seat_rung(
            "seat-a", False, joined=joined)
        self.assertFalse(ok)
        self.assertIn("TOOLLESS", refusal)
        self.assertTrue(dispatches._recipient_seat_rung(
            "seat-a", True, joined=joined)[0])


class SeatDoctorPrintsIt(_Log):

    def doctor(self, lines, down=None):
        path = self.log(lines)
        out = io.StringIO()
        from helm import autocompact
        with mock.patch.dict(os.environ,
                             {"HELM_HOME": os.path.join(self.d, "home")}), \
                mock.patch.object(seat_health, "_proxy_bin",
                                  return_value="/nonexistent/proxy"), \
                mock.patch.object(
                    seat_health, "_proxy_binary_probe",
                    return_value=(True, "", "CLIProxyAPI Version: test")), \
                mock.patch.object(seat_health.shutil, "which",
                                  return_value="/usr/bin/claude"), \
                mock.patch.object(seat_health, "codex_cred_state",
                                  return_value=("valid", None, "fixture")), \
                mock.patch.object(seat_health, "_status"), \
                mock.patch.object(seat_health, "_minted_seats",
                                  return_value=[("cursor", "cursor")]), \
                mock.patch.object(seat_health, "_proxy_home",
                                  return_value=os.path.dirname(path)), \
                mock.patch.object(seat_health, "_running_pid",
                                  return_value=None), \
                mock.patch.object(seat_health, "_config_drift_lines",
                                  return_value=[]), \
                mock.patch.object(seat_health, "_window_margin_rows",
                                  return_value=[]), \
                mock.patch.object(seat_health, "_profile_rows",
                                  return_value=[]), \
                mock.patch.object(autocompact, "report_lines",
                                  return_value=[]), \
                mock.patch.object(proxywatch, "_desired_down",
                                  return_value=(down, None)), \
                contextlib.redirect_stdout(out):
            return seat_health._doctor([]), out.getvalue()

    def test_doctor_names_the_TOOLLESS_seat_and_fails(self):
        lines = cursor_shape()
        rc, text = self.doctor(lines)
        self.assertEqual(rc, 1, text)
        self.assertIn("TOOLLESS", text)
        self.assertIn("cursor", text)
        self.assertIn("7 consecutive turns", text)
        self.assertIn(lines[-1], text)

    def test_a_seat_stood_down_is_not_read_for_TOOLLESS(self):
        """An operator's desired-down seat takes no turns by intent, and
        proxywatch already gives it no verdict; its old log's tail must not
        fail doctor for as long as the record stands."""
        rc, text = self.doctor(cursor_shape(),
                               down={"reason": "parked", "ts": 1})
        self.assertEqual(rc, 0, text)
        self.assertNotIn("TOOLLESS", text)

    def test_CONTROL_a_working_seat_passes_doctor(self):
        lines = []
        for turn in range(5):
            lines += working_turn(turn * 10)
        rc, text = self.doctor(lines)
        self.assertEqual(rc, 0, text)
        self.assertNotIn("TOOLLESS", text)


if __name__ == "__main__":
    unittest.main()
