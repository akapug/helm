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
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import fleet, session  # noqa: E402

SID_A = "12345678-1234-1234-1234-123456789abc"
SID_B = "87654321-4321-4321-4321-cba987654321"


def srow(pid, sid=None, identity="unknown", root=None, cwd="/w",
         possible=(), reason="record-missing", start="g1", environ=()):
    """One session census row as the census actually shapes it."""
    return {"pid": pid, "resume": sid if identity == "resume" else None,
            "declared": sid if identity == "declared" else None,
            "declared_reason": "record-ok" if identity == "declared"
                               else reason,
            "cwd": cwd, "root": root, "identity": identity, "session": sid,
            "possible_sessions": list(possible), "child": False,
            "ancestor_sid8": "", "force": False,
            "start": start,
            "environ": None if environ is None else dict(environ)}


class FleetRowsTest(unittest.TestCase):
    def _wire(self, envs, census=(), daemons=(), daemons_failed=False,
              daemon_for=None, roster=({}, False), terminals=([], False),
              census_failed=False, who_failed=False, generation=None):
        merged = []
        for r in census:
            r = dict(r)
            # envs is keyed by pid; None = the bracketed environ read failed
            r["environ"] = envs.get(r["pid"], r.get("environ"))
            merged.append(r)
        census = {r["pid"]: r for r in merged}
        daemons = dict(daemons)
        daemon_for = daemon_for or (
            lambda pid, ds, unproven: (("daemon", sorted(ds)[0]) if ds
                                       else ("headless", None)))
        generation = generation or (lambda pid, start: True)
        return [
            mock.patch.object(fleet, "_census",
                              lambda: (census, census_failed, who_failed)),
            mock.patch.object(fleet, "_daemon_pids",
                              lambda: (daemons, set(), daemons_failed)),
            mock.patch.object(fleet, "_daemon_for", daemon_for),
            mock.patch.object(fleet, "_roster", lambda: roster),
            mock.patch.object(fleet, "_orca_terminals", lambda: terminals),
            mock.patch.object(fleet, "_generation_intact", generation),
        ]

    def _rows_full(self, *a, **kw):
        ps = self._wire(*a, **kw)
        for p in ps:
            p.start()
        try:
            return fleet.rows()
        finally:
            for p in ps:
                p.stop()

    def _rows(self, *a, **kw):
        table, daemons, _failed = self._rows_full(*a, **kw)
        return table, daemons

    def _render_rc(self, *a, args=(), **kw):
        buf = io.StringIO()
        ps = self._wire(*a, **kw)
        for p in ps:
            p.start()
        try:
            with contextlib.redirect_stdout(buf):
                rc = fleet.cmd_fleet(list(args))
        finally:
            for p in ps:
                p.stop()
        return buf.getvalue(), rc

    def _render(self, *a, **kw):
        return self._render_rc(*a, **kw)[0]

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
        # the live probes (census included) on every invocation
        calls = {"census": 0, "daemons": 0}
        rows_ = [srow(1), srow(2)]

        def census():
            calls["census"] += 1
            return {r["pid"]: r for r in rows_}, False, False

        def daemon_pids():
            calls["daemons"] += 1
            return {}, set(), False
        ps = self._wire({1: {}, 2: {}}, rows_)
        ps[0] = mock.patch.object(fleet, "_census", census)
        ps[1] = mock.patch.object(fleet, "_daemon_pids", daemon_pids)
        for p in ps:
            p.start()
        try:
            fleet.rows()
            fleet.rows()
        finally:
            for p in ps:
                p.stop()
        self.assertEqual(calls, {"census": 2, "daemons": 2})

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
        rows, _ = self._rows({6: {}}, [srow(6, root="/r")], roster=({}, True))
        self.assertEqual((rows[0]["seat"], rows[0]["seat_src"]),
                         (None, "roster-error"))
        # round-2 finding 4: a failed roster probe is a row-level UNKNOWN —
        # the JSON bit must agree with the footer, and the render must never
        # claim the affirmative '(no seat)' fact
        self.assertTrue(rows[0]["unknown"])
        out = self._render({6: {}}, [srow(6, root="/r")], roster=({}, True))
        self.assertNotIn("(no seat)", out)
        self.assertIn("UNKNOWN columns", out)
        # a readable env with a seat name still wins over a broken roster
        rows, _ = self._rows({6: {"HELM_CHAT_NAME": "s"}}, [srow(6)],
                             roster=({}, True))
        self.assertEqual(rows[0]["seat_src"], "env")

    def test_probed_empty_roster_is_a_fact_not_unknown(self):
        # the affirmative counterpart: roster probe SUCCEEDED and holds no
        # seat -> '(no seat)' renders and the row is not unknown
        census = [srow(6, SID_A, "declared", root="/r")]
        rows, _ = self._rows({6: {}}, census, roster=({}, False))
        self.assertFalse(rows[0]["unknown"])
        self.assertIn("(no seat)", self._render({6: {}}, census))


class SidDelegationTest(FleetRowsTest):
    """codex+codex-2 finding 1: SID truth is session._proc_claude_rows(),
    consumed whole — record, argv, who, cwd-candidate rungs AND the final
    generation recheck — never a fleet-side splice of private helpers."""

    def test_census_calls_the_whole_census_verbatim(self):
        rows = [srow(7, SID_A, "declared", root="/r")]
        with mock.patch.object(
                session, "_proc_claude_census",
                return_value={"rows": rows, "listing_failed": False,
                              "who_failed": False}) as prc:
            self.assertEqual(fleet._census(), ({7: rows[0]}, False, False))
        prc.assert_called_once_with()

    def test_fleet_source_rederives_no_sid_or_config_parsing(self):
        # the design law, pinned at the source level: fleet may CALL the
        # census; the private sid/config helpers it once spliced are gone,
        # and (round-2 finding 1) so are the second comm scan and the
        # unbracketed /proc cwd re-read
        src = inspect.getsource(fleet)
        for banned in ("_proc_snapshot", "_session_record", "_resume_sid",
                       "_sid_for", "_sids_for", "argv~ancestor",
                       "_config_root", "CLAUDE_CONFIG_DIR",
                       "_claude_pids", "glob", "readlink",
                       # round-3 finding 1: env facts come from the census
                       # bracket — fleet never re-opens a proc environ file
                       "proc/%d/environ", "_environ(",
                       # round-3 finding 2: the completeness-blind rows-only
                       # shape is not fleet's entry point
                       "_proc_claude_rows"):
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

    def test_rows_come_solely_from_the_census_no_second_scan(self):
        # round-2 finding 1: a pid the census rejected (its generation
        # recheck failed — no two reads cohere) must never be resurrected by
        # a fleet-side comm scan and composed into a row of fictions
        rows, _ = self._rows({5: {}}, census=())
        self.assertEqual(rows, [])

    def test_census_none_cwd_is_never_re_read_from_proc(self):
        # round-2 finding 1: census cwd=None means the BRACKETED probe
        # failed. Use our OWN pid, whose /proc/<pid>/cwd is readable — a
        # surviving unbracketed fallback would return a real path and clear
        # the unknown bit; the row must stay '?' and UNKNOWN
        pid = os.getpid()
        census = [srow(pid, SID_A, "declared", root="/r", cwd=None)]
        rows, _ = self._rows({pid: {}}, census)
        self.assertEqual(rows[0]["cwd"], "?")
        self.assertTrue(rows[0]["unknown"])

    def test_failed_record_probe_reason_marks_row_unknown(self):
        census = [srow(6, reason="record-replaced", root="/r")]
        rows, _ = self._rows({6: {}}, census)
        self.assertTrue(rows[0]["unknown"])
        # an affirmative blank (record-missing) is NOT a failed probe
        rows, _ = self._rows({6: {}}, [srow(6, reason="record-missing",
                                            root="/r")])
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
        # round-2 finding 4: home='?' is unproven evidence — the row-level
        # bit must say so, not hand JSON consumers a false known-row bit
        self.assertTrue(rows[0]["unknown"])


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

    def _walk(self, tree, daemons, argv=None, start="111", unproven=()):
        with mock.patch.object(fleet, "_stat_ppid", lambda p: tree.get(p)), \
             mock.patch.object(fleet, "_cmdline_argv", lambda p: argv), \
             mock.patch.object(session, "_proc_start", lambda p: start):
            return fleet._daemon_for(7, daemons, set(unproven))

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

    def test_walk_through_an_unproven_pid_is_unknown_never_headless(self):
        # round-2 finding 2, exact probe: daemon-shaped 99 whose starttime
        # read failed is UNPROVEN; child 7->99->1 must answer UNKNOWN, not
        # walk through the maybe-daemon to init and claim proven HEADLESS
        self.assertEqual(self._walk({7: 99, 99: 1}, {}, unproven={99}),
                         ("unknown", None))

    def test_unreadable_cmdline_ancestor_is_unknown(self):
        # a hop whose cmdline could not be read cannot be ruled out as the
        # daemon — headless is unprovable through it
        self.assertEqual(self._walk({7: 42, 42: 1}, {}, unproven={42}),
                         ("unknown", None))


class DaemonScanTest(unittest.TestCase):
    """codex round-2 finding 2: partial probe failures inside the daemon
    scan must surface as UNPROVEN pids — never be silently dropped behind
    scan_failed=False and later converted into a proven-HEADLESS absence.
    The REAL _daemon_pids runs; only the /proc probes are mocked."""

    def _scan(self, argvs, starts, listing=None):
        names = [str(p) for p in argvs] if listing is None else listing
        with mock.patch.object(fleet.os, "listdir", lambda p: names), \
             mock.patch.object(fleet, "_cmdline_argv",
                               lambda p: argvs.get(p)), \
             mock.patch.object(session, "_proc_start",
                               lambda p: starts.get(p)):
            return fleet._daemon_pids()

    DAEMON = ["orca-ide", "/x/daemon-entry.js", "--socket", "/s"]

    def test_proven_daemon_is_bracketed_with_its_starttime(self):
        self.assertEqual(self._scan({99: self.DAEMON}, {99: "111"}),
                         ({99: "111"}, set(), False))

    def test_daemon_shape_without_starttime_is_unproven_not_dropped(self):
        # the review's exact probe: daemon argv recognized, _proc_start=None
        # -> previously ({}, False); now the pid survives as UNPROVEN
        self.assertEqual(self._scan({99: self.DAEMON}, {}),
                         ({}, {99}, False))

    def test_unreadable_cmdline_is_unproven_not_dropped(self):
        self.assertEqual(self._scan({77: None}, {}), ({}, {77}, False))

    def test_ordinary_processes_enter_neither_set(self):
        self.assertEqual(self._scan({8: ["bash", "-c", "sleep 1"]}, {}),
                         ({}, set(), False))

    def test_failed_proc_listing_is_scan_failed(self):
        with mock.patch.object(fleet.os, "listdir",
                               mock.Mock(side_effect=OSError)):
            self.assertEqual(fleet._daemon_pids(), ({}, set(), True))


class OrcaTerminalsTest(unittest.TestCase):
    """codex round-2 finding 3: a successful orca command returning valid
    JSON of the WRONG SCHEMA is a failed probe (pane truth UNKNOWN) — never
    an AttributeError that crashes the whole truth verb."""

    def _terms(self, stdout, rc=0):
        proc = subprocess.CompletedProcess([], rc, stdout=stdout, stderr="")
        with mock.patch.object(fleet.subprocess, "run", return_value=proc):
            return fleet._orca_terminals()

    def test_top_level_list_is_failed_not_a_crash(self):
        # the review's exact probe: stdout '[]' raised AttributeError before
        self.assertEqual(self._terms("[]"), ([], True))

    def test_wrong_shapes_are_failed_probes(self):
        for stdout in ('{"result": []}', '{"result": {"terminals": {}}}',
                       '{"result": {}}', '"ok"', "3",
                       '{"result": {"terminals": [{"handle": "t"}, 3]}}'):
            self.assertEqual(self._terms(stdout), ([], True), stdout)

    def test_documented_shape_passes_through_verbatim(self):
        terms = [{"handle": "t1", "worktreePath": "/w"}]
        self.assertEqual(
            self._terms('{"result": {"terminals": '
                        '[{"handle": "t1", "worktreePath": "/w"}]}}'),
            (terms, False))

    def test_successful_empty_list_is_a_fact_not_a_failure(self):
        self.assertEqual(self._terms('{"result": {"terminals": []}}'),
                         ([], False))

    def test_nonzero_exit_and_bad_json_are_failed(self):
        self.assertEqual(self._terms("", rc=3), ([], True))
        self.assertEqual(self._terms("not json"), ([], True))


class UnknownPlumbingTest(FleetRowsTest):
    """codex finding 3 / codex-2 finding 2: a failed probe is UNKNOWN in the
    ROW and the FOOTER — never converted into HEADLESS/no-pane or an
    owner-cannot-see claim."""

    def test_unprovable_host_renders_unknown_not_headless(self):
        unk = lambda pid, ds, unproven: ("unknown", None)  # noqa: E731
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
        boom = lambda p, ds, unp: self.fail("walk must not run")  # noqa: E731
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


class GenerationBracketTest(FleetRowsTest):
    """codex-2 round-3 finding 1: a census row is only coherent for ITS
    process generation. Env facts come from the census's bracketed environ
    (never a later live re-read), and the host walk's fresh /proc reads are
    only composed in when a FINAL recheck proves the same generation still
    owns the pid — run after all display probes."""

    def test_seat_deck_stamps_come_from_the_census_bracket(self):
        # our OWN pid: if fleet still re-read the live proc environ it would
        # get THIS process's real environment, not the bracketed marker
        pid = os.getpid()
        census = [srow(pid, SID_A, "declared", root="/r")]
        envs = {pid: {"HELM_CHAT_NAME": "bracketed-seat",
                      "CLAUDE_CODE_SESSION_ID": "x",
                      "HELM_SKILL_DECK": "/x/helm-skills"}}
        rows, _ = self._rows(envs, census)
        r = rows[0]
        self.assertEqual((r["seat"], r["seat_src"], r["stamps"], r["deck"]),
                         ("bracketed-seat", "env", 1, "helm"))
        self.assertFalse(r["unknown"])

    def test_reused_pid_display_probes_are_discarded_not_composed(self):
        # the review's exact probe: old canonical row (sid/home/cwd) + a
        # reused pid answering the walk as proven-HEADLESS with a new seat.
        # The failed recheck must kill the host claim to UNKNOWN — never
        # compose old census facts with the new process's ancestry
        census = [srow(7, SID_A, "declared", root="/r", start="g-old")]
        wire = dict(daemon_for=lambda pid, ds, unp: ("headless", None),
                    generation=lambda pid, start: False)
        rows, _ = self._rows({7: {"HELM_CHAT_NAME": "old-seat"}}, census,
                             **wire)
        r = rows[0]
        self.assertEqual((r["daemon_state"], r["daemon"], r["pane"]),
                         ("unknown", None, None))
        self.assertTrue(r["unknown"])
        out = self._render({7: {"HELM_CHAT_NAME": "old-seat"}}, census,
                           **wire)
        self.assertIn("host=?", out)
        self.assertNotIn("HEADLESS", out)
        self.assertIn("UNKNOWN columns", out)

    def test_failed_recheck_also_kills_a_daemon_pane_claim(self):
        census = [srow(7, SID_A, "declared", root="/r", cwd="/w/a")]
        terms = [{"handle": "term_1", "worktreePath": "/w/a"}]
        rows, _ = self._rows({7: {}}, census, daemons={99: "1"},
                             terminals=(terms, False),
                             generation=lambda pid, start: False)
        r = rows[0]
        self.assertEqual((r["daemon_state"], r["daemon"], r["pane"]),
                         ("unknown", None, None))
        self.assertTrue(r["unknown"])

    def test_intact_generation_keeps_the_proven_host(self):
        # affirmative counterpart, and the recheck receives the row's OWN
        # exported bracket generation
        seen = []
        census = [srow(7, SID_A, "declared", root="/r", start="g-live")]
        rows, _ = self._rows({7: {}}, census, daemons={99: "1"},
                             generation=lambda pid, start: (
                                 seen.append((pid, start)) or True))
        self.assertEqual(seen, [(7, "g-live")])
        self.assertEqual(rows[0]["daemon"], 99)
        self.assertFalse(rows[0]["unknown"])

    def test_recheck_runs_after_every_display_probe(self):
        order = []
        census = {7: srow(7, SID_A, "declared", root="/r", cwd="/w/a")}

        def daemon_for(pid, ds, unp):
            order.append("walk")
            return "daemon", 99

        def terminals():
            order.append("terminals")
            return [{"handle": "t", "worktreePath": "/w/a"}], False

        def generation(pid, start):
            order.append("recheck")
            return True
        with mock.patch.object(fleet, "_census",
                               lambda: (census, False, False)), \
             mock.patch.object(fleet, "_daemon_pids",
                               lambda: ({99: "1"}, set(), False)), \
             mock.patch.object(fleet, "_daemon_for", daemon_for), \
             mock.patch.object(fleet, "_roster", lambda: ({}, False)), \
             mock.patch.object(fleet, "_orca_terminals", terminals), \
             mock.patch.object(fleet, "_generation_intact", generation):
            fleet.rows()
        self.assertEqual(order, ["walk", "terminals", "recheck"])

    def test_generation_intact_is_a_real_starttime_recheck(self):
        # the REAL probe: our own live pid verifies against its true
        # starttime, and NOTHING else — wrong bracket, missing bracket, and
        # a nonexistent pid all refuse
        pid = os.getpid()
        start = session._proc_start(pid)
        self.assertIsNotNone(start)
        self.assertTrue(fleet._generation_intact(pid, start))
        self.assertFalse(fleet._generation_intact(pid, "999"))
        self.assertFalse(fleet._generation_intact(pid, None))
        self.assertFalse(fleet._generation_intact(2 ** 22 + 12345, start))


class CensusCompletenessTest(FleetRowsTest):
    """codex-2 round-3 finding 2: the sole-source census carries a
    completeness channel. A failed /proc enumeration is estate-UNKNOWN
    (exit 1), never a certified-empty fleet; a failed who scan marks every
    sub-declared/resume row sid-UNKNOWN."""

    def test_census_failure_cannot_certify_an_empty_estate(self):
        table, daemons, failed = self._rows_full({}, (), census_failed=True)
        self.assertEqual(table, [])
        self.assertTrue(failed)
        out, rc = self._render_rc({}, (), census_failed=True)
        self.assertEqual(rc, 1)
        self.assertIn("CENSUS FAILED", out)
        self.assertIn("UNKNOWN, not empty", out)
        self.assertNotIn("0 live claude process(es)", out)

    def test_census_failure_reaches_json_consumers(self):
        out, rc = self._render_rc({}, (), census_failed=True,
                                  args=("--json",))
        self.assertEqual(rc, 1)
        self.assertTrue(json.loads(out)["census_failed"])
        # and a healthy run still certifies the affirmative bit
        out, rc = self._render_rc({}, (), args=("--json",))
        self.assertEqual(rc, 0)
        self.assertFalse(json.loads(out)["census_failed"])

    def test_successful_empty_census_is_a_fact(self):
        out, rc = self._render_rc({}, ())
        self.assertEqual(rc, 0)
        self.assertIn("0 live claude process(es)", out)

    def test_who_scan_failure_marks_sub_who_rows_sid_unknown(self):
        # declared/resume rows outrank the who rung and stay proven; rows
        # whose ladder bottomed out BELOW them may only look unresolved
        # because the who probe vanished — UNKNOWN, not a proven blank
        census = [srow(1, SID_A, "declared", root="/r"),
                  srow(2, SID_B, "resume", root="/r"),
                  srow(3, possible=[SID_A], root="/r"),
                  srow(4, root="/r")]
        child = srow(5, root="/r")
        child["child"] = True  # who is suppressed for children by design
        envs = {p: {} for p in (1, 2, 3, 4, 5)}
        rows, _ = self._rows(envs, census + [child], who_failed=True)
        by = {r["pid"]: r for r in rows}
        self.assertFalse(by[1]["unknown"])
        self.assertFalse(by[2]["unknown"])
        self.assertTrue(by[3]["unknown"])
        self.assertTrue(by[4]["unknown"])
        self.assertFalse(by[5]["unknown"])
        # the healthy counterpart: same rows, probed who, no taint
        rows, _ = self._rows(envs, census + [child])
        self.assertFalse(any(r["unknown"] for r in rows))


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
