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
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import fleet, seats, session, who  # noqa: E402

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
              census_failed=False, who_failed=False, census_partial=False,
              generation=None, pane_for=None, unproven=()):
        merged = []
        for r in census:
            r = dict(r)
            # envs is keyed by pid; None = the bracketed environ read failed
            r["environ"] = envs.get(r["pid"], r.get("environ"))
            merged.append(r)
        census = {r["pid"]: r for r in merged}
        daemons = dict(daemons)
        daemon_for = daemon_for or (
            lambda pid, start, ds, unproven: (
                ("daemon", sorted(ds)[0]) if ds else ("headless", None)))
        generation = generation or (lambda pid, start: True)
        pane_for = pane_for or (lambda env, terms: (None, True))
        return [
            mock.patch.object(fleet, "_census",
                              lambda: (census, census_failed, who_failed,
                                       census_partial)),
            mock.patch.object(fleet, "_daemon_pids",
                              lambda: (daemons, set(unproven), daemons_failed)),
            mock.patch.object(fleet, "_daemon_for", daemon_for),
            mock.patch.object(fleet, "_roster", lambda: roster),
            mock.patch.object(
                fleet, "_orca_terminals", lambda daemon, start, env: terminals),
            mock.patch.object(fleet, "_pane_for", pane_for),
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
        table, daemons, _flags = self._rows_full(*a, **kw)
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
            return {r["pid"]: r for r in rows_}, False, False, False

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

    def test_roster_fallback_uses_session_not_a_nonexistent_pid_field(self):
        roster = {"codex-2": {"session": SID_A, "sessions": [SID_A]}}
        census = [srow(6, SID_A, "declared", root="/r")]
        rows, _ = self._rows({6: {}}, census, roster=(roster, False))
        self.assertEqual((rows[0]["seat"], rows[0]["seat_src"]),
                         ("codex-2", "roster"))

    def test_duplicate_roster_session_is_ambiguous_and_unknown(self):
        roster = {"a": {"session": SID_A}, "b": {"sessions": [SID_A]}}
        census = [srow(6, SID_A, "declared", root="/r")]
        rows, _ = self._rows({6: {}}, census, roster=(roster, False))
        self.assertEqual(rows[0]["seat_src"], "roster-ambiguous")
        self.assertTrue(rows[0]["unknown"])


class RosterCheckedTest(unittest.TestCase):
    def test_missing_is_empty_but_malformed_or_wrong_shape_is_failed(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "roster.json")
            with mock.patch.object(seats, "roster_path", return_value=path):
                self.assertEqual(seats.roster_checked(), ({}, False))
                for value in ("{bad", "[]", '{"seat": "bad"}'):
                    with open(path, "w") as f:
                        f.write(value)
                    self.assertEqual(seats.roster_checked(), ({}, True), value)
                good = {"seat": {"session": SID_A}}
                with open(path, "w") as f:
                    json.dump(good, f)
                self.assertEqual(seats.roster_checked(), (good, False))


class SidDelegationTest(FleetRowsTest):
    """codex+codex-2 finding 1: SID truth is session._proc_claude_rows(),
    consumed whole — record, argv, who, cwd-candidate rungs AND the final
    generation recheck — never a fleet-side splice of private helpers."""

    def test_census_calls_the_whole_census_verbatim(self):
        rows = [srow(7, SID_A, "declared", root="/r")]
        with mock.patch.object(
                session, "_proc_claude_census",
                return_value={"rows": rows, "listing_failed": False,
                              "who_failed": False,
                              "census_partial": False}) as prc:
            self.assertEqual(fleet._census(),
                             ({7: rows[0]}, False, False, False))
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
        links = {p: ("g%d" % p, parent) for p, parent in tree.items()}
        with mock.patch.object(fleet, "_stat_link", lambda p: links.get(p)), \
             mock.patch.object(fleet, "_cmdline_argv", lambda p: argv), \
             mock.patch.object(session, "_proc_start", lambda p: start):
            return fleet._daemon_for(7, "g7", daemons, set(unproven))

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

    def test_intermediate_parent_reuse_invalidates_the_chain(self):
        calls = {7: 0}

        def link(pid):
            if pid == 7:
                calls[7] += 1
                return ("g7", 50) if calls[7] == 1 else ("g7", 1)
            return {50: ("g50", 99)}.get(pid)
        with mock.patch.object(fleet, "_stat_link", side_effect=link), \
             mock.patch.object(fleet, "_cmdline_argv",
                               return_value=self.DAEMON_ARGV), \
             mock.patch.object(session, "_proc_start", return_value="d1"):
            self.assertEqual(fleet._daemon_for(7, "g7", {99: "d1"}, set()),
                             ("unknown", None))


class CensusRecheckFailureTest(unittest.TestCase):
    def test_live_stat_read_failure_is_unknown_not_generation_mismatch(self):
        error = PermissionError(13, "stat unreadable")
        with mock.patch.object(session, "_proc_bytes", side_effect=error):
            self.assertIsNone(session._census_matches(41, "g1", b"claude\0"))

    def test_gone_pid_is_proven_absence(self):
        error = FileNotFoundError(2, "gone")
        with mock.patch.object(session, "_proc_bytes", side_effect=error):
            self.assertFalse(session._census_matches(41, "g1", b"claude\0"))


class WhoCensusContextTest(unittest.TestCase):
    def _census(self, who_rows, failed_pids=()):
        snap = {"pid": 41, "uid": os.geteuid(), "start": "g1",
                "cmdline": b"claude\0", "environ": b"HOME=/alt\0",
                "argv": ["claude"], "env": {"HOME": "/alt"},
                "cwd": "/w", "stdin": "/dev/pts/1"}

        def scan(accounts=None, status=None):
            status.update({"listing_failed": False,
                           "failed_pids": set(failed_pids)})
            return who_rows

        with mock.patch.object(session.os, "listdir", return_value=["41"]), \
             mock.patch.object(session, "_census_snapshot",
                               return_value=("ok", snap)), \
             mock.patch.object(session, "_session_record",
                               return_value=(None, "record-missing",
                                             "/alt/.claude")), \
             mock.patch.object(session, "_census_matches", return_value=True), \
             mock.patch.object(session, "_cwd_session_ids", return_value=[]), \
             mock.patch.object(who, "scan", side_effect=scan):
            return session._proc_claude_census()["rows"][0]

    def test_who_sid_from_another_home_is_conflict_not_identity(self):
        wr = {"pid": 41, "provider": "anthropic", "child": False,
              "home": "/inspector/.claude", "cwd": "/w",
              "session": SID_A, "session_candidates": []}
        row = self._census([wr])
        self.assertIsNone(row["session"])
        self.assertTrue(row["who_context_mismatch"])

    def test_partial_who_scan_marks_that_pid_failed(self):
        row = self._census([], failed_pids={41})
        self.assertIsNone(row["session"])
        self.assertTrue(row["who_probe_failed"])


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
    """Terminal inventory is bound to one daemon/runtime and must be complete."""

    def _terms(self, listing, status=None, daemon_ppid=10, start="g1"):
        status = status or {
            "ok": True, "result": {"app": {"pid": 10},
                                    "runtime": {"runtimeId": "r1"}}}
        replies = iter((status, listing))
        env = {"ORCA_USER_DATA_PATH": os.path.expanduser("~/.config/orca")}
        with mock.patch.object(fleet, "_orca_json",
                               side_effect=lambda *a: next(replies)), \
             mock.patch.object(fleet, "_daemon_user_data",
                               return_value=os.path.realpath(env["ORCA_USER_DATA_PATH"])), \
             mock.patch.object(fleet, "_stat_ppid", return_value=daemon_ppid), \
             mock.patch.object(session, "_proc_start", return_value=start):
            return fleet._orca_terminals(99, "g1", env)

    @staticmethod
    def listing(terms=(), truncated=False, total=None, runtime="r1"):
        terms = list(terms)
        return {"ok": True, "result": {"terminals": terms,
                                         "truncated": truncated,
                                         "totalCount": len(terms) if total is None else total},
                "_meta": {"runtimeId": runtime}}

    def test_documented_complete_shape_is_runtime_bound(self):
        terms = [{"handle": "t1", "tabId": "a", "leafId": "b",
                  "connected": True, "writable": True}]
        got, failed = self._terms(self.listing(terms))
        self.assertFalse(failed)
        self.assertEqual(got[0]["handle"], "t1")
        self.assertEqual((got[0]["_orca_cli"], got[0]["_runtime_id"]),
                         ("orca", "r1"))

    def test_successful_empty_list_is_a_fact_not_a_failure(self):
        self.assertEqual(self._terms(self.listing()), ([], False))

    def test_truncated_or_count_mismatch_is_failed(self):
        self.assertEqual(self._terms(self.listing([], truncated=True)),
                         ([], True))
        self.assertEqual(self._terms(self.listing([], total=1)), ([], True))

    def test_wrong_runtime_or_daemon_owner_is_failed(self):
        self.assertEqual(self._terms(self.listing(runtime="other")),
                         ([], True))
        self.assertEqual(self._terms(self.listing(), daemon_ppid=11),
                         ([], True))

    def test_wrong_shapes_are_failed_probes(self):
        for listing in (None, {"ok": True, "result": []},
                        {"ok": True, "result": {"terminals": {}}},
                        {"ok": True, "result": {}}):
            self.assertEqual(self._terms(listing), ([], True), listing)


class UnknownPlumbingTest(FleetRowsTest):
    """codex finding 3 / codex-2 finding 2: a failed probe is UNKNOWN in the
    ROW and the FOOTER — never converted into HEADLESS/no-pane or an
    owner-cannot-see claim."""

    def test_unprovable_host_renders_unknown_not_headless(self):
        unk = lambda pid, start, ds, unproven: ("unknown", None)  # noqa: E731
        census = [srow(3, SID_A, "declared", root="/r")]
        rows, _ = self._rows({3: {}}, census, daemon_for=unk)
        self.assertEqual(rows[0]["daemon_state"], "unknown")
        self.assertTrue(rows[0]["unknown"])
        out, rc = self._render_rc({3: {}}, census, daemon_for=unk)
        self.assertEqual(rc, 1)  # row UNKNOWN must never ride a shell PASS
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
        boom = lambda p, start, ds, unp: self.fail("walk must not run")  # noqa: E731
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
        wire = dict(daemon_for=lambda pid, start, ds, unp: ("headless", None),
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

        def daemon_for(pid, start, ds, unp):
            order.append("walk")
            return "daemon", 99

        def terminals(daemon, start, env):
            order.append("terminals")
            return [{"handle": "t"}], False

        def generation(pid, start):
            order.append("recheck")
            return True
        with mock.patch.object(fleet, "_census",
                               lambda: (census, False, False, False)), \
             mock.patch.object(fleet, "_daemon_pids",
                               lambda: ({99: "1"}, set(), False)), \
             mock.patch.object(fleet, "_daemon_for", daemon_for), \
             mock.patch.object(fleet, "_roster", lambda: ({}, False)), \
             mock.patch.object(fleet, "_orca_terminals", terminals), \
             mock.patch.object(fleet, "_pane_for",
                               lambda env, terms: ("t", True)), \
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
        table, daemons, flags = self._rows_full(
            {}, (), census_failed=True)
        self.assertEqual(table, [])
        self.assertTrue(flags["census_failed"])
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


def pfrow(pid, start="g1"):
    """One probe-failed census row exactly as session shapes it: comm proved
    claude, then a mandatory read failed while the pid persisted."""
    return {"pid": pid, "resume": None, "declared": None,
            "declared_reason": "probe-failed", "cwd": None, "root": None,
            "start": start, "environ": None, "identity": "unknown",
            "session": None, "possible_sessions": [], "child": False,
            "ancestor_sid8": "", "force": False, "probe_failed": True}


class PerPidProbeFailureTest(FleetRowsTest):
    """codex-2 HIGH: per-PID mandatory probe failures below the global bits
    must never vanish as proven absence. A post-comm failure is an UNKNOWN
    row in the output AND the exit status; a pre-comm failure is
    census_partial — the estate total is a floor, surfaced like
    listing_failed, exit nonzero."""

    def test_probe_failed_row_is_unknown_and_fails_the_exit_status(self):
        census = [pfrow(41)]
        table, _daemons, flags = self._rows_full({}, census)
        r = table[0]
        self.assertTrue(r["probe_failed"])
        self.assertTrue(r["unknown"])
        self.assertIsNone(r["sid"])
        self.assertFalse(flags["census_failed"])
        self.assertFalse(flags["census_partial"])
        out, rc = self._render_rc({}, census)
        self.assertEqual(rc, 1)
        self.assertIn("pid 41", out)
        self.assertIn("mandatory census probe", out)
        self.assertIn("never proven absence", out)
        self.assertIn("UNKNOWN columns", out)

    def test_probe_failed_reaches_json_consumers(self):
        out, rc = self._render_rc({}, [pfrow(41)], args=("--json",))
        self.assertEqual(rc, 1)
        data = json.loads(out)
        self.assertTrue(data["rows"][0]["probe_failed"])
        self.assertTrue(data["rows"][0]["unknown"])
        self.assertFalse(data["census_partial"])

    def test_census_partial_prints_a_floor_and_exits_nonzero(self):
        census = [srow(1, SID_A, "declared", root="/r")]
        out, rc = self._render_rc({1: {}}, census, census_partial=True)
        self.assertEqual(rc, 1)
        self.assertIn("at least 1 live claude process(es)", out)
        self.assertIn("CENSUS PARTIAL", out)
        self.assertIn("floor", out)

    def test_census_partial_reaches_json_consumers(self):
        out, rc = self._render_rc({}, (), census_partial=True,
                                  args=("--json",))
        self.assertEqual(rc, 1)
        self.assertTrue(json.loads(out)["census_partial"])
        # the healthy counterpart still certifies the affirmative bits
        out, rc = self._render_rc({}, (), args=("--json",))
        self.assertEqual(rc, 0)
        self.assertFalse(json.loads(out)["census_partial"])

    def test_healthy_estate_total_is_not_a_floor(self):
        out, rc = self._render_rc({1: {}},
                                  [srow(1, SID_A, "declared", root="/r")])
        self.assertEqual(rc, 0)
        self.assertNotIn("at least", out)
        self.assertNotIn("CENSUS PARTIAL", out)


@unittest.skipIf(os.geteuid() == 0, "root bypasses file permissions")
class ProbeFailedEndToEndTest(unittest.TestCase):
    """The finding's exact probe, END TO END through the CLI: a planted
    /proc pid whose comm proves 'claude' and whose cmdline raises
    PermissionError must surface as an UNKNOWN row and a nonzero exit —
    helm fleet must never certify 0 live processes after failing to read a
    KNOWN claude pid."""

    def _run(self, args):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        base = os.path.join(tmp, "41")
        os.makedirs(base)
        with open(os.path.join(base, "comm"), "wb") as f:
            f.write(b"claude\n")
        cmdline = os.path.join(base, "cmdline")
        with open(cmdline, "wb") as f:
            f.write(b"claude\0")
        with open(os.path.join(base, "environ"), "wb") as f:
            f.write(b"HOME=/nonexistent-home\0")
        fields = ["S"] + [str(i) for i in range(4, 22)] + ["424242", "0", "0"]
        with open(os.path.join(base, "stat"), "w") as f:
            f.write("41 (claude) %s\n" % " ".join(fields))
        os.symlink(tmp, os.path.join(base, "cwd"))
        os.chmod(cmdline, 0)
        self.addCleanup(os.chmod, cmdline, 0o644)
        buf = io.StringIO()
        with mock.patch.object(session, "PROC", tmp), \
             mock.patch.object(who, "scan", return_value=[]), \
             mock.patch.object(fleet, "_daemon_pids",
                               lambda: ({}, set(), False)), \
             mock.patch.object(fleet, "_daemon_for",
                               lambda *a: ("unknown", None)), \
             mock.patch.object(fleet, "_roster", lambda: ({}, False)), \
             contextlib.redirect_stdout(buf):
            rc = fleet.cmd_fleet(list(args))
        return buf.getvalue(), rc

    def test_unreadable_known_claude_pid_is_unknown_row_nonzero_exit(self):
        out, rc = self._run(["--json"])
        self.assertEqual(rc, 1)
        data = json.loads(out)
        [row] = data["rows"]
        self.assertEqual(row["pid"], 41)
        self.assertTrue(row["probe_failed"])
        self.assertTrue(row["unknown"])
        self.assertIsNone(row["sid"])
        self.assertFalse(data["census_failed"])
        self.assertFalse(data["census_partial"])
        out, rc = self._run([])
        self.assertEqual(rc, 1)
        self.assertIn("pid 41", out)
        self.assertIn("mandatory census probe", out)


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


class PaneIdentityTest(FleetRowsTest):
    """Pane identity comes from the bracketed Orca pane key, never cwd."""

    TERMS = ([{"handle": "term_new", "tabId": "tab", "leafId": "leaf",
               "worktreePath": "/other", "connected": True,
               "writable": True}], False)

    def test_hosted_row_resolves_replacement_handle_by_pane_key(self):
        census = [srow(10, SID_A, "declared", root="/r", cwd="/w/x")]
        env = {"ORCA_PANE_KEY": "tab:leaf",
               "ORCA_TERMINAL_HANDLE": "term_stale"}
        rows, _ = self._rows(
            {10: env}, census, {99: "s1"}, terminals=self.TERMS,
            pane_for=fleet._pane_for)
        self.assertEqual(rows[0]["pane"], "term_new")
        self.assertFalse(rows[0]["unknown"])

    def test_hosted_row_without_authoritative_pane_key_is_unknown(self):
        census = [srow(10, SID_A, "declared", root="/r", cwd="/w/x")]
        out, rc = self._render_rc(
            {10: {}}, census, daemons={99: "s1"}, terminals=self.TERMS,
            pane_for=fleet._pane_for, args=("--json",))
        self.assertEqual(rc, 1)
        self.assertTrue(json.loads(out)["rows"][0]["unknown"])


class EstateProbeExitTest(FleetRowsTest):
    """fable review MED (both lenses): every estate-wide failed probe — who
    scan, daemon scan, terminal list — must reach the machine-readable
    verdict exactly like census_failed/census_partial: exit 1 and a named
    --json completeness bit. A scripted consumer keying on rc or the JSON
    estate bits must never read PASS while that truth went unprobed."""

    def _bits(self, *a, **kw):
        out, rc = self._render_rc(*a, args=("--json",), **kw)
        return json.loads(out), rc

    def test_who_failure_gates_exit_code_and_json(self):
        census = [srow(4, root="/r")]
        _out, rc = self._render_rc({4: {}}, census, who_failed=True)
        self.assertEqual(rc, 1)
        data, rc = self._bits({4: {}}, census, who_failed=True)
        self.assertEqual(rc, 1)
        self.assertTrue(data["who_failed"])
        # healthy counterpart still certifies the affirmative bit
        data, rc = self._bits({4: {}}, census)
        self.assertEqual(rc, 0)
        self.assertFalse(data["who_failed"])

    def test_daemon_scan_failure_gates_exit_code_and_json(self):
        census = [srow(3, SID_A, "declared", root="/r")]
        boom = lambda p, start, ds, unp: self.fail("walk must not run")  # noqa: E731
        _out, rc = self._render_rc({3: {}}, census, daemons_failed=True,
                                   daemon_for=boom)
        self.assertEqual(rc, 1)
        data, rc = self._bits({3: {}}, census, daemons_failed=True,
                              daemon_for=boom)
        self.assertEqual(rc, 1)
        self.assertTrue(data["daemons_failed"])
        data, rc = self._bits({3: {}}, census)
        self.assertEqual(rc, 0)
        self.assertFalse(data["daemons_failed"])

    def test_partial_daemon_census_is_a_floor_and_gates_exit(self):
        census = [srow(3, SID_A, "declared", root="/r")]
        data, rc = self._bits({3: {}}, census, unproven={77})
        self.assertEqual(rc, 1)
        self.assertTrue(data["daemons_partial"])
        out, rc = self._render_rc({3: {}}, census, unproven={77})
        self.assertEqual(rc, 1)
        self.assertIn("DAEMON CENSUS PARTIAL", out)
        self.assertIn("at least 0 orca daemon", out)

    def test_terminal_list_failure_gates_exit_code_and_json(self):
        census = [srow(3, SID_A, "declared", root="/r")]
        hosted = dict(daemons={99: "1"}, terminals=([], True))
        _out, rc = self._render_rc({3: {}}, census, **hosted)
        self.assertEqual(rc, 1)
        data, rc = self._bits({3: {}}, census, **hosted)
        self.assertEqual(rc, 1)
        self.assertTrue(data["terms_failed"])
        data, rc = self._bits({3: {}}, census, daemons={99: "1"})
        self.assertEqual(rc, 0)
        self.assertFalse(data["terms_failed"])

    def test_text_render_names_the_failed_estate_probes(self):
        out, rc = self._render_rc({4: {}}, [srow(4, root="/r")],
                                  who_failed=True, daemons_failed=True)
        self.assertEqual(rc, 1)
        self.assertIn("estate-wide probe(s) FAILED", out)
        self.assertIn("who scan", out)
        self.assertIn("daemon scan", out)
        self.assertIn("never proven blanks", out)


class ProbeOrderingTest(unittest.TestCase):
    """fable review LOW: the daemon scan runs AFTER the census bracket. A
    daemon that starts between the two scans — whose freshly-spawned claude
    IS censused — is then in the set, so the ppid walk cannot pass through
    the missing pid to init and read a false proven-HEADLESS (a ghost
    warning for a process with a live pane)."""

    def test_daemon_scan_runs_after_the_census(self):
        order = []

        def census():
            order.append("census")
            return {}, False, False, False

        def daemon_pids():
            order.append("daemons")
            return {}, set(), False
        with mock.patch.object(fleet, "_census", census), \
             mock.patch.object(fleet, "_daemon_pids", daemon_pids), \
             mock.patch.object(fleet, "_roster", lambda: ({}, False)):
            fleet.rows()
        self.assertEqual(order, ["census", "daemons"])


class PaneMappingTest(unittest.TestCase):
    TERMS = [{"handle": "term_1", "tabId": "a", "leafId": "one",
              "connected": True, "writable": True,
              "worktreePath": "/decoy"},
             {"handle": "term_2", "tabId": "b", "leafId": "two",
              "connected": False, "writable": True,
              "worktreePath": "/w"}]

    def test_exact_pane_key_resolves_without_cwd(self):
        self.assertEqual(fleet._pane_for({"ORCA_PANE_KEY": "a:one"}, self.TERMS),
                         ("term_1", True))

    def test_stale_handle_does_not_override_pane_key(self):
        env = {"ORCA_PANE_KEY": "a:one", "ORCA_TERMINAL_HANDLE": "term_old"}
        self.assertEqual(fleet._pane_for(env, self.TERMS), ("term_1", True))

    def test_transport_ids_upgrade_through_terminal_show(self):
        terms = [{"handle": "term_new", "tabId": "pty:x", "leafId": "pty:x",
                  "worktreeId": "w1", "connected": True, "writable": True,
                  "_orca_cli": "orca", "_runtime_id": "r1"}]
        shown = {"ok": True, "result": {"terminal": {
            "handle": "term_new", "tabId": "stable-tab",
            "leafId": "stable-leaf", "connected": True, "writable": True}},
            "_meta": {"runtimeId": "r1"}}
        env = {"ORCA_PANE_KEY": "stable-tab:stable-leaf",
               "ORCA_WORKTREE_ID": "w1"}
        with mock.patch.object(fleet, "_orca_json", return_value=shown):
            self.assertEqual(fleet._pane_for(env, terms), ("term_new", True))

    def test_missing_disconnected_or_ambiguous_key_is_unproven(self):
        self.assertEqual(fleet._pane_for({}, self.TERMS), (None, False))
        self.assertEqual(fleet._pane_for({"ORCA_PANE_KEY": "b:two"}, self.TERMS),
                         (None, False))
        dup = self.TERMS + [dict(self.TERMS[0], handle="term_3")]
        self.assertEqual(fleet._pane_for({"ORCA_PANE_KEY": "a:one"}, dup),
                         (None, False))


if __name__ == "__main__":
    unittest.main()
