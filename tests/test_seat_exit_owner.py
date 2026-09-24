#!/usr/bin/env python3
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from helm import seat_exit_owner


class SeatExitOwnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-seat-exit-")
        self.path = os.path.join(self.tmp, "spawn.json")
        self.rec = {"v": 1, "seat": "seat-a", "harness": "orca",
                    "handle": "pane-a", "session": "session-a"}
        with open(self.path, "w") as f:
            json.dump(self.rec, f)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_close_archives_one_explicit_pane_terminal_transition(self):  # noqa: VACUOUS_ASSERTION — the one archive and pane-closed witness positively control active-record absence
        notes, errors = seat_exit_owner.close_spawn(
            "seat-a", self.tmp, self.rec,
            "registered handle absent and zero live processes")
        self.assertEqual(errors, [])
        self.assertEqual(len(notes), 1)
        self.assertFalse(os.path.exists(self.path))
        archived = seat_exit_owner.archive_paths(self.tmp)
        self.assertEqual(len(archived), 1)
        with open(archived[0]) as f:
            closed = json.load(f)
        self.assertEqual(closed["seat"], "seat-a")
        self.assertEqual(closed["session"], "session-a")
        self.assertEqual(closed["terminal"]["event"], "pane-closed")
        self.assertIn("zero live processes", closed["terminal"]["reason"])

    def test_stop_receipt_never_substitutes_for_terminal_proof(self):
        class Adapter:
            name = "orca"
            def stop(self, _handle):
                return {"terminal": {"ptyKilled": True}}
            def list(self):
                return [{"handle": "pane-a", "status": "connected"}]
        with mock.patch.object(seat_exit_owner, "_PANE_CLOSE_POLLS", 1), \
                mock.patch.object(seat_exit_owner, "_PANE_CLOSE_DELAY", 0):
            reason, unavailable = seat_exit_owner.stop_pane(
                Adapter(), "pane-a", absent_proof=lambda: (None, "live"))
        self.assertIsNone(reason)
        self.assertIn("still reports state connected", unavailable)

    def test_duplicate_exact_handle_rows_are_unknown_even_if_one_is_terminal(self):
        class Adapter:
            name = "fake"
            def stop(self, _handle):
                return None
            def list(self):
                return [{"handle": "pane-a", "status": "closed"},
                        {"handle": "pane-a", "status": "connected"}]
        with mock.patch.object(seat_exit_owner, "_PANE_CLOSE_POLLS", 1), \
                mock.patch.object(seat_exit_owner, "_PANE_CLOSE_DELAY", 0):
            reason, unavailable = seat_exit_owner.stop_pane(Adapter(), "pane-a")
        self.assertIsNone(reason)
        self.assertIn("2 rows for exact handle", unavailable)

    def test_latest_archive_orders_numeric_collision_suffixes(self):
        root = os.path.join(self.tmp, "spawn.archive")
        os.makedirs(root)
        base = dict(self.rec)
        for suffix in ("-9", "-10"):
            rec = dict(base)
            rec["terminal"] = {
                "v": 1, "event": "pane-closed", "ts": "same-stamp",
                "reason": suffix, "seat": "seat-a",
                "session": "session-a",
                "room": None, "harness": "orca", "handle": "pane-a",
                "pid": None, "pid_identity": None}
            with open(os.path.join(
                    root, "spawn.closed-samestamp%s.json" % suffix), "w") as f:
                json.dump(rec, f)
        rec, unavailable = seat_exit_owner.latest_archived_exit(
            self.tmp, "seat-a")
        self.assertIsNone(unavailable)
        self.assertEqual(rec["terminal"]["reason"], "-10")

    def test_archived_projection_rejects_wrong_or_internally_mismatched_identity(self):
        closed, err = seat_exit_owner.archive_spawn(
            "seat-a", self.tmp, self.rec, "pane is dead")
        self.assertIsNone(err)
        rec, unavailable = seat_exit_owner.latest_archived_exit(
            self.tmp, "seat-b")
        self.assertIsNone(rec)
        self.assertIn("belongs to seat seat-a, not seat-b", unavailable)
        with open(closed["archive"]) as f:
            archived = json.load(f)
        archived["terminal"]["session"] = "other-session"
        with open(closed["archive"], "w") as f:
            json.dump(archived, f)
        rec, unavailable = seat_exit_owner.latest_archived_exit(
            self.tmp, "seat-a")
        self.assertIsNone(rec)
        self.assertIn("mismatched session identity", unavailable)

    def test_orca_inventory_and_process_proof_must_agree(self):  # noqa: VACUOUS_ASSERTION — an unconditional empty-inventory/process-zero success controls the fixed two-case contradiction matrix below
        class Adapter:
            name = "orca"
            def __init__(self, rows=None, error=None):
                self.rows, self.error = rows, error
            def stop(self, _handle):
                return None
            def list(self):
                if self.error:
                    raise self.error
                return list(self.rows)

        with mock.patch.object(seat_exit_owner, "_PANE_CLOSE_POLLS", 1), \
                mock.patch.object(seat_exit_owner, "_PANE_CLOSE_DELAY", 0):
            reason, unavailable = seat_exit_owner.stop_pane(
                Adapter([]), "pane-a",
                absent_proof=lambda: ("zero exact-session processes", None))
        self.assertIn("zero exact-session processes", reason)
        self.assertIsNone(unavailable)

        cases = (
            (Adapter(error=OSError("inventory down")),
             lambda: ("zero exact-session processes", None),
             "pane census is UNKNOWN"),
            (Adapter([{"handle": "pane-a", "status": "closed"}]),
             lambda: (None, None), "process termination is unproven"),
        )
        for adapter, absent, expected in cases:
            with self.subTest(expected=expected), \
                    mock.patch.object(seat_exit_owner, "_PANE_CLOSE_POLLS", 1), \
                    mock.patch.object(seat_exit_owner, "_PANE_CLOSE_DELAY", 0):
                reason, unavailable = seat_exit_owner.stop_pane(
                    adapter, "pane-a", absent_proof=absent)
            self.assertIsNone(reason)
            self.assertIn(expected, unavailable)

    def test_archive_schema_refuses_unknown_version_harness_and_mixed_identity(self):  # noqa: VACUOUS_ASSERTION — every subcase positively names its schema defect and preserves the active record
        cases = (
            (dict(self.rec, v=99), "unsupported version"),
            (dict(self.rec, harness="invented"), "unknown harness"),
            (dict(self.rec, session=None), "no exact session"),
            (dict(self.rec, pid=1234, pid_identity="birth"),
             "mixes headless process identity"),
            ({"v": 1, "seat": "seat-a",
              "harness": "headless", "pid": 1234},
             "no process birth identity"),
            ({"v": 1, "seat": "seat-a",
              "harness": "headless", "pid": 1234, "pid_identity": "birth",
              "handle": "pane-a", "session": "session-a"},
             "mixes pane/session identity"),
        )
        for rec, expected in cases:
            with self.subTest(expected=expected):
                with open(self.path, "w") as f:
                    json.dump(rec, f)
                closed, err = seat_exit_owner.archive_spawn(
                    "seat-a", self.tmp, rec, "runtime is absent")
                self.assertIsNone(closed)
                self.assertIn(expected, err)
                self.assertTrue(os.path.exists(self.path))

    def test_hostile_process_rollback_escalates_and_proves_absence(self):  # noqa: VACUOUS_ASSERTION — ARMED plus SIGKILL exit positively control the final process-absence assertion
        code = ("import signal,time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "print('ARMED', flush=True)\n"
                "time.sleep(60)\n")
        proc = subprocess.Popen(
            [sys.executable, "-c", code], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(), "ARMED")
            # The child ignores SIGTERM, so it outlives any TERM grace: the
            # escalation is what is measured, not the grace's length. All 15
            # TERM polls still run; only the gap between them shrinks.
            with mock.patch.object(seat_exit_owner, "_TERM_POLL_DELAY", 0.01):
                reason, unavailable = seat_exit_owner.terminate_process(
                    proc.pid, lambda: proc.poll() is None)
            self.assertIsNone(unavailable)
            self.assertIn("absent", reason)
            self.assertEqual(proc.wait(timeout=3), -9)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()
            proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
