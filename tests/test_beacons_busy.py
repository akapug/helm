"""A working pane without a beacon is not a dead seat."""
import io
import json
import os
import unittest
from unittest import mock

from helm import beacons, inject, record, seat_idle
from tests.test_beacons import Base, SID_A

NOW = 1_000_000.0


class BusyProofTest(Base):
    def seat(self, *, last_call, pane="RUNNING", session=SID_A,
             opened=NOW - 2500, ended=NOW - 5000):
        self.roster("qwenlocal")
        self.agent(90, "qwenlocal")
        agents = {"by_seat": {"qwenlocal": [90]}, "home_by_pid": {}}
        liveness = {"state": pane, "session": session,
                    "evidence": "pane-tail"}
        idle = {"state": "BUSY", "session": SID_A,
                "last_call": last_call, "turn_opened": opened,
                "turn_ended": ended}
        with mock.patch("helm.seat.seat_liveness",
                        return_value=liveness), \
                mock.patch("helm.seat_idle.reading", return_value=idle):
            return beacons.seat_census(
                "qwenlocal", live={}, proc_dir=self.proc, agents=agents,
                records={}, now=NOW)

    def test_a_forty_minute_turn_with_a_tool_call_is_busy_not_deaf(self):
        row = self.seat(last_call=NOW - 60)
        self.assertEqual(row["verdict"], beacons.BUSY)
        self.assertFalse(beacons.unreachable(row))

    def test_a_hung_running_pane_stays_deaf(self):
        row = self.seat(last_call=NOW - 65 * 60, opened=NOW - 70 * 60)
        self.assertEqual(row["verdict"], beacons.DEAF)
        self.assertTrue(beacons.unreachable(row))

    def test_idle_and_gone_panes_cannot_prove_busy(self):
        for pane in ("IDLE", "GONE", "UNKNOWN"):
            with self.subTest(pane=pane):
                self.assertEqual(self.seat(last_call=NOW - 1,
                                           pane=pane)["verdict"], beacons.DEAF)

    def test_a_call_before_this_turn_or_from_another_session_is_not_progress(self):
        self.assertEqual(self.seat(last_call=NOW - 2800)["verdict"],
                         beacons.DEAF)
        self.assertEqual(self.seat(last_call=NOW - 1,
                                   session="other-session")["verdict"],
                         beacons.DEAF)

    def test_first_turn_before_call_is_provisional_only_for_ten_minutes(self):
        self.assertEqual(self.seat(last_call=None, opened=NOW - 60,
                                   ended=None)["verdict"], beacons.BUSY)
        self.assertEqual(self.seat(last_call=None, opened=NOW - 601,
                                   ended=None)["verdict"], beacons.DEAF)
        self.assertEqual(self.seat(last_call=None, opened=NOW + 1,
                                   ended=None)["verdict"], beacons.DEAF)

    def test_new_turn_after_an_old_call_is_provisional_not_renewed(self):
        self.assertEqual(self.seat(last_call=NOW - 2000, opened=NOW - 60,
                                   ended=NOW - 100)["verdict"], beacons.BUSY)
        self.assertEqual(self.seat(last_call=NOW - 2000, opened=NOW - 601,
                                   ended=NOW - 700)["verdict"], beacons.DEAF)
        self.assertEqual(self.seat(last_call=None, opened=NOW - 60,
                                   ended=NOW - 30)["verdict"], beacons.DEAF)

    def test_real_prompt_hook_records_first_turn_and_real_call_renews_proof(self):  # noqa: VACUOUS_ASSERTION — counters and BUSY are positive controls for the missing initial call
        self.roster("qwenlocal")
        self.agent(90, "qwenlocal")
        agents = {"by_seat": {"qwenlocal": [90]}, "home_by_pid": {}}
        pane = {"state": "RUNNING", "session": SID_A, "evidence": "pane-tail"}

        def census(at):
            with mock.patch("helm.seat.seat_liveness",
                            return_value=pane):
                return beacons.seat_census("qwenlocal", live={},
                                           proc_dir=self.proc, agents=agents,
                                           records={}, now=at)

        event = {"prompt": "work", "session_id": SID_A,
                 "hook_event_name": "UserPromptSubmit", "cwd": self.tmp,
                 "transcript_path": os.path.join(self.tmp, SID_A + ".jsonl")}
        with mock.patch("helm.inject._whisper._ledger_begin", return_value=None), \
                mock.patch("sys.stdin", io.StringIO(json.dumps(event))), \
                mock.patch("time.time", return_value=NOW - 120):
            self.assertEqual(inject.cmd_inject(["--hook-json"]), 0)
        c = record.counters(SID_A)
        self.assertEqual(c["turn-opened-at"], NOW - 120)
        self.assertNotIn("call-at", c)
        self.assertEqual(census(NOW)["verdict"], beacons.BUSY)
        self.assertEqual(seat_idle.reading("qwenlocal", now=NOW + 400)["state"],
                         seat_idle.BUSY)
        self.assertEqual(census(NOW + 400)["verdict"], beacons.BUSY)
        self.assertEqual(census(NOW + 481)["verdict"], beacons.DEAF)

        tool = {"session_id": SID_A, "tool_name": "Read",
                "hook_event_name": "PostToolUse", "tool_input": {}}
        with mock.patch("time.time", return_value=NOW + 480):
            record.record(tool)
        self.assertEqual(seat_idle.reading("qwenlocal", now=NOW + 500)["state"],
                         seat_idle.BUSY)
        self.assertEqual(census(NOW + 500)["verdict"], beacons.BUSY)
        self.assertEqual(census(NOW + 1081)["verdict"], beacons.DEAF)


if __name__ == "__main__":
    unittest.main()
