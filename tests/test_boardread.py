#!/usr/bin/env python3
"""boardread tests — the record a `helm web` leaves about the owner's board,
and the doctor check that reads it.

THE PROPERTY UNDER TEST IS A DISTINCTION, not a level: "nothing is serving a
board here" and "a live server's board reads are failing" must never produce
the same finding, in either direction. So every doctor arm below pins BOTH the
level and the sentence, and the no-server arms assert there is no FAIL against
a positive control that the check spoke at all.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from helm import beacons, boardread, doctor, home, pk


def levels(results, level):
    return [msg for lvl, msg in results if lvl == level]


class BoardReadBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.envp = mock.patch.dict(os.environ,
                                    {"HELM_HOME": os.path.join(self.tmp.name,
                                                               "helm-home")})
        self.envp.start()
        self.addCleanup(self.envp.stop)
        self.assertTrue(home.helm_home().startswith(self.tmp.name))
        # THE HEARTBEAT MEMO IS PROCESS-WIDE, so one module's writes would
        # otherwise decide whether the next test's write lands at all.
        self.reset_memo()
        self.addCleanup(self.reset_memo)

    def reset_memo(self):
        boardread._LAST.update(ts=0.0, outcome=None)

    def plant(self, **fields):
        """A record written straight to disk, so an arm can name an observer
        this process is not."""
        rec = {"schema": boardread.SCHEMA, "ts": 1000.0, "outcome": "ok",
               "pid": os.getpid(),
               "pid_start": beacons.proc_starttime(os.getpid()),
               "reason": None, "failures": 0,
               "last_failed_ts": None, "last_failed_reason": None}
        rec.update(fields)
        pk.write_json(boardread.path(), rec)
        return rec

    def dead_observer(self):
        """A record whose pid is THIS process wearing a different incarnation
        key — `pid_alive`'s proven-gone case, with no pid to race for."""
        live = beacons.proc_starttime(os.getpid())
        self.assertIsInstance(live, int)          # control: /proc answered
        return self.plant(pid_start=live + 1)


class RecordTest(BoardReadBase):
    def test_a_failed_read_is_written_counted_and_named(self):
        self.assertTrue(boardread.record("failed", reason="the ledger refused"))
        rec = json.load(open(boardread.path()))
        self.assertEqual(rec["outcome"], "failed")
        self.assertEqual(rec["reason"], "the ledger refused")
        self.assertEqual(rec["failures"], 1)
        self.assertEqual(rec["pid"], os.getpid())
        self.assertEqual(rec["last_failed_reason"], "the ledger refused")
        self.assertEqual(oct(os.stat(boardread.path()).st_mode)[-3:], "600")

    def test_every_failure_writes_while_a_steady_outcome_heartbeats(self):
        self.assertTrue(boardread.record("ok", now=1000.0))
        # control: the first write of an outcome always lands, so a False below
        # is the heartbeat and not a broken writer.
        self.assertFalse(boardread.record("ok", now=1001.0))
        self.assertTrue(boardread.record("ok", now=1000.0 + boardread.HEARTBEAT_S))
        self.assertTrue(boardread.record("failed", reason="X", now=1000.1))
        self.assertTrue(boardread.record("failed", reason="X", now=1000.2))
        self.assertEqual(json.load(open(boardread.path()))["failures"], 2)

    def test_a_recorder_that_cannot_write_answers_False_and_never_raises(self):
        with mock.patch.object(boardread.pk, "atomic_write",
                               side_effect=OSError("read-only")):
            self.assertFalse(boardread.record("failed", reason="X"))
        self.assertFalse(os.path.exists(boardread.path()))
        # control on the same observable: the write lands once it can.
        self.assertTrue(boardread.record("failed", reason="X"))
        self.assertTrue(os.path.exists(boardread.path()))

    def test_an_unknown_outcome_is_refused_rather_than_recorded(self):
        self.assertFalse(boardread.record("degraded"))
        self.assertFalse(os.path.exists(boardread.path()))
        self.assertTrue(boardread.record("ok"))      # control: the door opens


class StateTest(BoardReadBase):
    def test_nothing_recorded_is_neither_readable_trouble_nor_a_reading(self):
        st = boardread.state()
        self.assertFalse(st["recorded"])
        self.assertTrue(st["readable"])
        self.assertIsNone(st["outcome"])

    def test_an_unparseable_record_is_UNREADABLE_not_an_absent_one(self):
        os.makedirs(os.path.dirname(boardread.path()), exist_ok=True)
        pk.atomic_write(boardread.path(), "{not json")
        self.assertFalse(boardread.state()["readable"])
        # control: the same path parses once the bytes are a record.
        self.plant()
        self.assertTrue(boardread.state()["readable"])

    def test_a_record_from_another_schema_is_refused_not_migrated(self):
        self.plant()                              # control: this one is read
        self.assertTrue(boardread.state()["readable"])
        self.plant(schema=boardread.SCHEMA + 1)
        self.assertFalse(boardread.state()["readable"])

    def test_the_observer_is_live_for_this_process_and_gone_for_another(self):
        boardread.record("failed", reason="boom")
        self.assertEqual(boardread.state()["observer"], "live")
        self.dead_observer()
        self.assertEqual(boardread.state()["observer"], "gone")

    def test_ages_are_resolved_against_the_readers_clock(self):
        self.plant(ts=1000.0, outcome="failed", reason="boom",
                   last_failed_ts=900.0, last_failed_reason="boom")
        st = boardread.state(now=1060.0)
        self.assertEqual(st["age_s"], 60)
        self.assertEqual(st["last_failed_age_s"], 160)
        # A clock that went backwards may not make a reading younger than now.
        self.assertEqual(boardread.state(now=900.0)["age_s"], 0)


class DoctorCheckTest(BoardReadBase):
    def test_the_check_is_registered_in_the_report_loop(self):
        self.assertIn("check_board_reads", doctor.CHECKS)
        self.assertTrue(hasattr(doctor, "check_board_reads"))

    def test_a_LIVE_servers_failed_read_is_a_FAIL_that_names_the_reason(self):
        boardread.record("failed", reason="the land-pipeline read failed")
        rows = doctor.check_board_reads()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], doctor.FAIL)
        self.assertIn("BOARD UNREADABLE", rows[0][1])
        self.assertIn("the land-pipeline read failed", rows[0][1])
        self.assertIn(str(os.getpid()), rows[0][1])

    def test_no_server_at_all_is_UNKNOWN_and_never_a_fault(self):
        rows = doctor.check_board_reads()
        self.assertEqual(len(rows), 1)       # control: the check did speak
        self.assertEqual(rows[0][0], doctor.OK)
        self.assertIn("UNKNOWN", rows[0][1])
        self.assertEqual(levels(rows, doctor.FAIL), [])

    def test_a_failure_whose_observer_is_gone_is_UNKNOWN_not_a_current_fault(self):
        self.dead_observer()
        self.plant(pid_start=beacons.proc_starttime(os.getpid()) + 1,
                   outcome="failed", reason="the ledger refused", failures=3)
        rows = doctor.check_board_reads()
        self.assertEqual(len(rows), 1)       # control: the check did speak
        self.assertEqual(rows[0][0], doctor.WARN)
        self.assertIn("UNKNOWN", rows[0][1])
        self.assertEqual(levels(rows, doctor.FAIL), [])

    def test_a_live_server_that_answers_is_OK_and_still_reports_past_failures(self):
        self.plant(outcome="ok", failures=2, last_failed_ts=1000.0,
                   last_failed_reason="the ledger refused", ts=1100.0)
        rows = doctor.check_board_reads()
        self.assertEqual([lvl for lvl, _ in rows], [doctor.OK, doctor.OK])
        self.assertIn("2 failed reads recorded", rows[0][1])
        self.assertIn("the board has answered since", rows[1][1])

    def test_a_warming_projection_is_a_named_state_not_a_failed_read(self):
        boardread.record("warming")
        rows = doctor.check_board_reads()
        self.assertEqual(rows[0][0], doctor.OK)
        self.assertIn("WARMING", rows[0][1])
        self.assertEqual(levels(rows, doctor.FAIL), [])

    def test_an_unreadable_record_accuses_the_record_not_the_board(self):
        os.makedirs(os.path.dirname(boardread.path()), exist_ok=True)
        pk.atomic_write(boardread.path(), "{not json")
        rows = doctor.check_board_reads()
        self.assertEqual(rows[0][0], doctor.WARN)
        self.assertIn("does not parse", rows[0][1])
        self.assertEqual(levels(rows, doctor.FAIL), [])


if __name__ == "__main__":
    unittest.main()
