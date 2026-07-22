#!/usr/bin/env python3
"""helm.fleet — the composition-truth verb. Hermetic where /proc is the input
(probes mocked), but the PARSERS under test are always the real ones:
session's record reader / _resume_sid and fleet's daemon matcher run on real
inputs, never mocked."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import fleet, session  # noqa: E402

SID_A = "12345678-1234-1234-1234-123456789abc"
SID_B = "87654321-4321-4321-4321-cba987654321"


class FleetRowsTest(unittest.TestCase):
    def _wire(self, envs, daemons=frozenset(), sids=None,
              roster=({}, False), terminals=None):
        return [
            mock.patch.object(fleet, "_claude_pids", lambda: sorted(envs)),
            mock.patch.object(fleet, "_daemon_pids", lambda: set(daemons)),
            mock.patch.object(fleet, "_environ", lambda pid: envs.get(pid)),
            mock.patch.object(fleet, "_daemon_for",
                              lambda pid, ds: (sorted(ds)[0] if ds else None)),
            mock.patch.object(fleet, "_sids_for",
                              lambda pids: {p: (sids or {}).get(p, (None, None))
                                            for p in pids}),
            mock.patch.object(fleet, "_roster", lambda: roster),
            mock.patch.object(fleet, "_orca_terminals", lambda: terminals or []),
        ]

    def _rows(self, *a, **kw):
        ps = self._wire(*a, **kw)
        for p in ps:
            p.start()
        try:
            return fleet.rows()
        finally:
            for p in ps:
                p.stop()

    def test_stamps_and_deck_are_read_from_the_live_env(self):
        envs = {10: {"HELM_CHAT_NAME": "a-seat",
                     "CLAUDE_CODE_CHILD_SESSION": "1",
                     "CLAUDE_CODE_SESSION_ID": "x",
                     "HELM_SKILL_DECK": "/home/u/dev/mission-control/skills"}}
        rows, daemons = self._rows(envs, {99}, {10: ("sid-a", "record")})
        r = rows[0]
        self.assertEqual((r["seat"], r["stamps"], r["deck"], r["daemon"]),
                         ("a-seat", 2, "MC", 99))
        self.assertFalse(r["unknown"])

    def test_a_daemonless_process_is_flagged_headless(self):
        rows, _ = self._rows({7: {}}, set(), {7: ("s", "argv~ancestor")})
        self.assertIsNone(rows[0]["daemon"])
        # the argv rung is labeled as the weaker source it is
        self.assertEqual(rows[0]["sid_src"], "argv~ancestor")

    def test_every_column_is_probed_never_cached(self):
        # the verb exists BECAUSE cached mental models rot: rows() must call
        # the live probes on every invocation
        calls = {"n": 0}

        def envs(pid):
            calls["n"] += 1
            return {}
        ps = self._wire({1: {}, 2: {}}, set(), {})
        ps[2] = mock.patch.object(fleet, "_environ", envs)
        for p in ps:
            p.start()
        try:
            fleet.rows()
            fleet.rows()
        finally:
            for p in ps:
                p.stop()
        self.assertEqual(calls["n"], 4)

    def test_unreadable_environ_is_unknown_not_absence(self):
        # env=None is a FAILED probe: home/deck show ?, stamps unproven, the
        # row is marked unknown, and the footer counts it
        rows, _ = self._rows({5: None}, set(), {5: (SID_A, "record")})
        r = rows[0]
        self.assertTrue(r["unknown"])
        self.assertEqual((r["home"], r["deck"], r["stamps"]),
                         ("?", "?", None))
        buf = io.StringIO()
        ps = self._wire({5: None}, set(), {5: (SID_A, "record")})
        for p in ps:
            p.start()
        try:
            with contextlib.redirect_stdout(buf):
                fleet.cmd_fleet([])
        finally:
            for p in ps:
                p.stop()
        self.assertIn("1 row(s) carry UNKNOWN columns", buf.getvalue())
        self.assertIn("failed probes, not absence", buf.getvalue())

    def test_roster_exception_surfaces_as_roster_error(self):
        rows, _ = self._rows({6: {}}, roster=({}, True))
        self.assertEqual((rows[0]["seat"], rows[0]["seat_src"]),
                         (None, "roster-error"))
        # a readable env with a seat name still wins over a broken roster
        rows, _ = self._rows({6: {"HELM_CHAT_NAME": "s"}}, roster=({}, True))
        self.assertEqual(rows[0]["seat_src"], "env")


class SidResolutionTest(unittest.TestCase):
    """The REAL parsers: session._read_session_record and session._resume_sid
    exactly as fleet delegates to them. No mocks on the parser under test."""

    def test_record_with_wrong_procstart_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            sdir = os.path.join(root, "sessions")
            os.makedirs(sdir)
            with open(os.path.join(sdir, "4242.json"), "w") as f:
                json.dump({"pid": 4242, "sessionId": SID_A,
                           "procStart": "111"}, f)
            uid = os.geteuid()
            self.assertEqual(
                session._read_session_record(root, 4242, uid, "222"),
                (None, "record-stale"))
            # same record, matching procStart: accepted — proves the guard
            # (not some earlier failure) is what refused the stale one
            self.assertEqual(
                session._read_session_record(root, 4242, uid, "111"),
                (SID_A, "record-ok"))

    def _snap(self, argv):
        return {"pid": 1, "argv": argv, "env": {}, "environ": b"",
                "uid": os.geteuid(), "start": "1", "cmdline": b"",
                "cwd": None}

    def test_resume_flag_consuming_another_flag_yields_no_sid(self):
        sid, src = fleet._sid_for(1, self._snap(
            ["claude", "--resume", "--model", "opus"]), None, {})
        self.assertEqual((sid, src), (None, None))

    def test_conflicting_resume_sids_yield_no_sid(self):
        sid, src = fleet._sid_for(1, self._snap(
            ["claude", "--resume", SID_A, "--resume=" + SID_B]), None, {})
        self.assertEqual((sid, src), (None, None))

    def test_direct_resume_is_not_an_ancestor_claim(self):
        snap = self._snap(["claude", "--resume", SID_A])
        # no record holds SID_A: plain argv, no ancestor assertion
        self.assertEqual(fleet._sid_for(1, snap, None, {}), (SID_A, "argv"))
        # this pid's own record holds it: still not an ancestor
        self.assertEqual(fleet._sid_for(1, snap, None, {SID_A: 1}),
                         (SID_A, "argv"))

    def test_resume_of_another_pids_recorded_sid_is_an_ancestor(self):
        snap = self._snap(["claude", "--resume", SID_A])
        self.assertEqual(fleet._sid_for(1, snap, None, {SID_A: 999}),
                         (SID_A, "argv~ancestor"))

    def test_pid_record_outranks_argv(self):
        snap = self._snap(["claude", "--resume", SID_A])
        self.assertEqual(fleet._sid_for(1, snap, SID_B, {SID_B: 1}),
                         (SID_B, "record"))


class DaemonDetectionTest(unittest.TestCase):
    def test_substring_lookalikes_are_not_daemons(self):
        for argv in (["node", "/x/not-daemon-entry.js"],
                     ["bash", "-c", "tail -f daemon-entry.js.log"],
                     ["node", "/x/daemon-entry.js.bak"],
                     ["--script=daemon-entry.js"]):
            self.assertFalse(fleet._is_daemon_argv(argv), argv)

    def test_exact_element_matches(self):
        self.assertTrue(fleet._is_daemon_argv(
            ["node", "/opt/orca/daemon-entry.js"]))
        self.assertTrue(fleet._is_daemon_argv(["daemon-entry.js"]))

    def test_ppid_walk_parses_real_proc_stat(self):
        self.assertEqual(fleet._stat_ppid(os.getpid()), os.getppid())
        self.assertIsNone(fleet._stat_ppid(2 ** 22 + 12345))  # no such pid


class PaneMappingTest(unittest.TestCase):
    def test_unique_cwd_join_maps_ambiguity_never_guesses(self):
        terms = [{"handle": "term_1", "worktreePath": "/w/a"},
                 {"handle": "term_2", "worktreePath": "/w/b"},
                 {"handle": "term_3", "worktreePath": "/w/b"}]
        self.assertEqual(fleet._pane_for("/w/a", terms, set()), "term_1")
        # two terminals at one path: ambiguous -> None
        self.assertIsNone(fleet._pane_for("/w/b", terms, set()))
        # two claude rows share the cwd: ambiguous -> None
        self.assertIsNone(fleet._pane_for("/w/a", terms, {"/w/a"}))
        # unknown cwd -> None
        self.assertIsNone(fleet._pane_for(None, terms, set()))


if __name__ == "__main__":
    unittest.main()
