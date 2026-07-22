#!/usr/bin/env python3
"""helm.fleet — the composition-truth verb. Hermetic where /proc is the input
(probes mocked), but the PARSERS under test are always the real ones:
session's record reader / _resume_sid and fleet's daemon matcher/walk run on
real inputs, never mocked. SID truth is DELEGATED: fleet consumes
session._proc_claude_rows() verbatim and re-derives none of it — pinned here
both behaviorally and against the module source."""
import contextlib
import inspect
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


def srow(pid, sid=None, identity="unknown", root=None, cwd="/w",
         possible=(), reason="record-missing"):
    """One session._proc_claude_rows() row as the census actually shapes it."""
    return {"pid": pid, "resume": sid if identity == "resume" else None,
            "declared": sid if identity == "declared" else None,
            "declared_reason": "record-ok" if identity == "declared"
                               else reason,
            "cwd": cwd, "root": root, "identity": identity, "session": sid,
            "possible_sessions": list(possible), "child": False,
            "ancestor_sid8": "", "force": False}


class FleetRowsTest(unittest.TestCase):
    def _wire(self, envs, census=(), daemons=(), daemons_failed=False,
              daemon_for=None, roster=({}, False), terminals=([], False)):
        census = {r["pid"]: r for r in census}
        daemons = dict(daemons)
        daemon_for = daemon_for or (
            lambda pid, ds: (("daemon", sorted(ds)[0]) if ds
                             else ("headless", None)))
        return [
            mock.patch.object(fleet, "_claude_pids",
                              lambda: sorted(set(envs) | set(census))),
            mock.patch.object(fleet, "_census", lambda: census),
            mock.patch.object(fleet, "_daemon_pids",
                              lambda: (daemons, daemons_failed)),
            mock.patch.object(fleet, "_environ", lambda pid: envs.get(pid)),
            mock.patch.object(fleet, "_daemon_for", daemon_for),
            mock.patch.object(fleet, "_roster", lambda: roster),
            mock.patch.object(fleet, "_orca_terminals", lambda: terminals),
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

    def _render(self, *a, **kw):
        buf = io.StringIO()
        ps = self._wire(*a, **kw)
        for p in ps:
            p.start()
        try:
            with contextlib.redirect_stdout(buf):
                fleet.cmd_fleet([])
        finally:
            for p in ps:
                p.stop()
        return buf.getvalue()

    def test_stamps_and_deck_are_read_from_the_live_env(self):
        envs = {10: {"HELM_CHAT_NAME": "a-seat",
                     "CLAUDE_CODE_CHILD_SESSION": "1",
                     "CLAUDE_CODE_SESSION_ID": "x",
                     "HELM_SKILL_DECK": "/home/u/dev/mission-control/skills"}}
        census = [srow(10, SID_A, "declared", root="/h/.claude")]
        rows, daemons = self._rows(envs, census, {99: "111"})
        r = rows[0]
        self.assertEqual((r["seat"], r["stamps"], r["deck"], r["daemon"]),
                         ("a-seat", 2, "MC", 99))
        self.assertEqual((r["sid"], r["sid_src"]), (SID_A, "record"))
        self.assertFalse(r["unknown"])
        self.assertEqual(daemons, [99])

    def test_every_column_is_probed_never_cached(self):
        # the verb exists BECAUSE cached mental models rot: rows() must call
        # the live probes on every invocation
        calls = {"n": 0}

        def envs(pid):
            calls["n"] += 1
            return {}
        ps = self._wire({1: {}, 2: {}}, [srow(1), srow(2)])
        ps[3] = mock.patch.object(fleet, "_environ", envs)
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
        census = [srow(5, SID_A, "declared")]
        rows, _ = self._rows({5: None}, census)
        r = rows[0]
        self.assertTrue(r["unknown"])
        self.assertEqual((r["home"], r["deck"], r["stamps"]),
                         ("?", "?", None))
        out = self._render({5: None}, census)
        self.assertIn("1 row(s) carry UNKNOWN columns", out)
        self.assertIn("failed probes, not absence", out)

    def test_roster_exception_surfaces_as_roster_error(self):
        rows, _ = self._rows({6: {}}, [srow(6)], roster=({}, True))
        self.assertEqual((rows[0]["seat"], rows[0]["seat_src"]),
                         (None, "roster-error"))
        # a readable env with a seat name still wins over a broken roster
        rows, _ = self._rows({6: {"HELM_CHAT_NAME": "s"}}, [srow(6)],
                             roster=({}, True))
        self.assertEqual(rows[0]["seat_src"], "env")


class SidDelegationTest(FleetRowsTest):
    """codex+codex-2 finding 1: SID truth is session._proc_claude_rows(),
    consumed whole — record, argv, who, cwd-candidate rungs AND the final
    generation recheck — never a fleet-side splice of private helpers."""

    def test_census_calls_proc_claude_rows_verbatim(self):
        rows = [srow(7, SID_A, "declared", root="/r")]
        with mock.patch.object(session, "_proc_claude_rows",
                               return_value=rows) as prc:
            self.assertEqual(fleet._census(), {7: rows[0]})
        prc.assert_called_once_with()

    def test_fleet_source_rederives_no_sid_or_config_parsing(self):
        # the design law, pinned at the source level: fleet may CALL the
        # census; the private sid/config helpers it once spliced are gone
        src = inspect.getsource(fleet)
        for banned in ("_proc_snapshot", "_session_record", "_resume_sid",
                       "_sid_for", "_sids_for", "argv~ancestor",
                       "_config_root", "CLAUDE_CONFIG_DIR"):
            self.assertNotIn(banned, src, banned)

    def test_every_census_identity_maps_to_its_source_label(self):
        census = [srow(1, SID_A, "declared", root="/r"),
                  srow(2, SID_A, "resume"),
                  srow(3, SID_B, "who")]
        rows, _ = self._rows({1: {}, 2: {}, 3: {}}, census)
        self.assertEqual([(r["sid"], r["sid_src"]) for r in rows],
                         [(SID_A, "record"), (SID_A, "argv"),
                          (SID_B, "who")])

    def test_cwd_candidate_rung_surfaces_instead_of_being_dropped(self):
        census = [srow(4, possible=[SID_A, SID_B])]
        rows, _ = self._rows({4: {}}, census)
        self.assertEqual(rows[0]["candidates"], [SID_A, SID_B])
        self.assertIn("(2 cwd-candidate(s))", self._render({4: {}}, census))

    def test_second_record_holder_is_double_open_not_ancestor(self):
        # "another pid records this SID" proves DOUBLE-OPEN, not a fork
        census = [srow(1, SID_A, "declared", root="/r"),
                  srow(2, SID_A, "resume")]
        rows, _ = self._rows({1: {}, 2: {}}, census)
        self.assertTrue(all(r["double_open"] for r in rows))
        out = self._render({1: {}, 2: {}}, census)
        self.assertIn("DOUBLE-OPEN", out)
        self.assertIn("live in MULTIPLE pids", out)

    def test_pid_absent_from_census_is_unknown_not_a_blank_fact(self):
        # snapshot/generation-recheck failure = the pid never enters the
        # census; fleet must render UNKNOWN, not an unresolved-looking fact
        rows, _ = self._rows({5: {}}, census=())
        self.assertTrue(rows[0]["unknown"])
        self.assertIsNone(rows[0]["sid"])

    def test_failed_record_probe_reason_marks_row_unknown(self):
        census = [srow(6, reason="record-replaced")]
        rows, _ = self._rows({6: {}}, census)
        self.assertTrue(rows[0]["unknown"])
        # an affirmative blank (record-missing) is NOT a failed probe
        rows, _ = self._rows({6: {}}, [srow(6, reason="record-missing")])
        self.assertFalse(rows[0]["unknown"])


class HomeColumnTest(FleetRowsTest):
    """codex finding 4 / codex-2 finding 3: home is session's canonical
    config root for the TARGET process — never the inspector's ~/.claude,
    never a fleet-side re-derivation of config policy."""

    def test_home_is_the_census_root_not_the_inspector_home(self):
        census = [srow(8, SID_A, "declared", root="/tmp/other-home/.claude")]
        rows, _ = self._rows({8: {"HOME": "/tmp/other-home"}}, census)
        self.assertEqual(rows[0]["home"], "/tmp/other-home/.claude")

    def test_untrusted_or_unproven_config_root_renders_unknown(self):
        # session returns root=None for config-untrusted (incl. the
        # CLAUDE_CONFIG_DIR-present-but-invalid case): never guess ~/.claude
        rows, _ = self._rows({8: {}}, [srow(8, reason="config-untrusted",
                                            root=None)])
        self.assertEqual(rows[0]["home"], "?")


class DaemonDetectionTest(unittest.TestCase):
    def test_substring_lookalikes_are_not_daemons(self):
        for argv in (["node", "/x/not-daemon-entry.js"],
                     ["bash", "-c", "tail -f daemon-entry.js.log"],
                     ["node", "/x/daemon-entry.js.bak"],
                     ["--script=daemon-entry.js"]):
            self.assertFalse(fleet._is_daemon_argv(argv), argv)

    def test_non_orca_runtimes_reading_the_script_are_not_daemons(self):
        # codex-2 finding 4: the SHAPE must be the orca daemon's, not any
        # argv element that basenames to daemon-entry.js
        for argv in (["cat", "/tmp/daemon-entry.js"],
                     ["python", "worker.py", "/tmp/daemon-entry.js"],
                     ["vi", "daemon-entry.js"],
                     ["node", "worker.js", "/x/daemon-entry.js"]):
            self.assertFalse(fleet._is_daemon_argv(argv), argv)

    def test_real_orca_daemon_shapes_match(self):
        self.assertTrue(fleet._is_daemon_argv(
            ["/tmp/.mount_orca-x/orca-ide",
             "/tmp/.mount_orca-x/resources/app.asar.unpacked/out/main/"
             "daemon-entry.js", "--socket", "/x.sock"]))
        self.assertTrue(fleet._is_daemon_argv(
            ["node", "/opt/orca/daemon-entry.js"]))
        self.assertTrue(fleet._is_daemon_argv(["daemon-entry.js"]))

    def test_ppid_walk_parses_real_proc_stat(self):
        self.assertEqual(fleet._stat_ppid(os.getpid()), os.getppid())
        self.assertIsNone(fleet._stat_ppid(2 ** 22 + 12345))  # no such pid


class DaemonWalkTest(unittest.TestCase):
    """codex finding 2: daemon identity is re-proven at match time (argv
    still daemon-shaped, starttime still the scanned incarnation) — bare set
    membership across PID reuse is never trusted. The REAL _daemon_for walk
    runs; only the /proc probes are mocked."""
    DAEMON_ARGV = ["orca-ide", "/x/daemon-entry.js", "--socket", "/s"]

    def _walk(self, tree, daemons, argv=None, start="111"):
        with mock.patch.object(fleet, "_stat_ppid", lambda p: tree.get(p)), \
             mock.patch.object(fleet, "_cmdline_argv", lambda p: argv), \
             mock.patch.object(session, "_proc_start", lambda p: start):
            return fleet._daemon_for(7, daemons)

    def test_matching_incarnation_is_a_proven_daemon(self):
        self.assertEqual(self._walk({7: 99}, {99: "111"},
                                    argv=self.DAEMON_ARGV),
                         ("daemon", 99))

    def test_stale_starttime_membership_is_unknown_not_a_host(self):
        # deterministic probe from the review: stale set {99} + parent 99
        self.assertEqual(self._walk({7: 99}, {99: "111"},
                                    argv=self.DAEMON_ARGV, start="222"),
                         ("unknown", None))

    def test_reused_pid_running_a_lookalike_is_unknown(self):
        self.assertEqual(self._walk({7: 99}, {99: "111"},
                                    argv=["cat", "/tmp/daemon-entry.js"]),
                         ("unknown", None))

    def test_walk_to_init_is_the_only_proven_headless(self):
        self.assertEqual(self._walk({7: 5, 5: 1}, {99: "111"}),
                         ("headless", None))

    def test_unparsable_hop_is_unknown_never_headless(self):
        self.assertEqual(self._walk({7: 5}, {99: "111"}), ("unknown", None))

    def test_exhausted_depth_is_unknown_never_headless(self):
        tree = {p: p + 1 for p in range(7, 40)}
        self.assertEqual(self._walk(tree, {}), ("unknown", None))


class UnknownPlumbingTest(FleetRowsTest):
    """codex finding 3 / codex-2 finding 2: a failed probe is UNKNOWN in the
    ROW and the FOOTER — never converted into HEADLESS/no-pane or an
    owner-cannot-see claim."""

    def test_unprovable_host_renders_unknown_not_headless(self):
        unk = lambda pid, ds: ("unknown", None)  # noqa: E731
        census = [srow(3, SID_A, "declared", root="/r")]
        rows, _ = self._rows({3: {}}, census, daemon_for=unk)
        self.assertEqual(rows[0]["daemon_state"], "unknown")
        self.assertTrue(rows[0]["unknown"])
        out = self._render({3: {}}, census, daemon_for=unk)
        self.assertIn("host=?", out)
        self.assertNotIn("HEADLESS", out)
        self.assertNotIn("owner cannot see", out)
        self.assertIn("UNKNOWN columns", out)

    def test_proven_headless_still_raises_the_ghost_warning(self):
        census = [srow(3, SID_A, "declared", root="/r")]
        out = self._render({3: {}}, census)  # no daemons -> proven headless
        self.assertIn("HEADLESS/no-pane", out)
        self.assertIn("owner cannot see", out)
        self.assertNotIn("UNKNOWN columns", out)

    def test_daemon_scan_failure_makes_every_host_unknown(self):
        boom = lambda pid, ds: self.fail("walk must not run")  # noqa: E731
        rows, _ = self._rows({3: {}}, [srow(3, SID_A, "declared", root="/r")],
                             daemons_failed=True, daemon_for=boom)
        self.assertEqual(rows[0]["daemon_state"], "unknown")
        self.assertTrue(rows[0]["unknown"])

    def test_terminal_list_failure_marks_hosted_rows_unknown(self):
        census = [srow(3, SID_A, "declared", root="/r")]
        rows, _ = self._rows({3: {}}, census, daemons={99: "1"},
                             terminals=([], True))
        self.assertEqual(rows[0]["daemon"], 99)
        self.assertIsNone(rows[0]["pane"])
        self.assertTrue(rows[0]["unknown"])
        # a SUCCESSFUL empty terminal list is a fact, not a failed probe
        rows, _ = self._rows({3: {}}, census, daemons={99: "1"},
                             terminals=([], False))
        self.assertFalse(rows[0]["unknown"])

    def test_unreadable_cwd_marks_the_row_unknown(self):
        rows, _ = self._rows({3: {}}, [srow(3, SID_A, "declared", root="/r",
                                            cwd=None)])
        self.assertEqual(rows[0]["cwd"], "?")
        self.assertTrue(rows[0]["unknown"])


class SidParserTest(unittest.TestCase):
    """The REAL session parsers fleet's census delegates to — no mocks on
    the parser under test."""

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

    def test_resume_flag_consuming_another_flag_yields_no_sid(self):
        self.assertIsNone(session._resume_sid(
            ["claude", "--resume", "--model", "opus"]))

    def test_conflicting_resume_sids_yield_no_sid(self):
        self.assertIsNone(session._resume_sid(
            ["claude", "--resume", SID_A, "--resume=" + SID_B]))

    def test_valid_resume_beside_an_invalid_occurrence_fails_closed(self):
        # codex-2 parser note: contradictory evidence poisons the parse —
        # a valid --resume plus a bare/invalid repeat is UNKNOWN
        self.assertIsNone(session._resume_sid(
            ["claude", "--resume", SID_A, "--resume"]))
        self.assertIsNone(session._resume_sid(
            ["claude", "--resume", SID_A, "--resume=not-a-sid"]))
        self.assertIsNone(session._resume_sid(
            ["claude", "--resume=" + SID_A[:8], "--resume", SID_A]))

    def test_agreeing_repeats_still_resolve(self):
        self.assertEqual(session._resume_sid(
            ["claude", "--resume", SID_A, "--resume=" + SID_A]), SID_A)


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
