#!/usr/bin/env python3
"""Hermetic tests for the session substrate (helm/session.py) — the policy
layer over cv. cv is stubbed (the mechanics are cv's own tested surface); the
/proc scan is stubbed via a planted row list; the experts registry rides a tmp
HELM_HOME."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from helm import session, home, pk, runtime_config, who


def run(fn, args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = fn(list(args))
    return rc, out.getvalue(), err.getvalue()


class SessionBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-session-")
        self._home = os.environ.get("HELM_HOME")
        os.environ["HELM_HOME"] = self.tmp
        # a planted live-pane scan: one persisted integrator, one memory-only
        # pane resuming sid DEADBEEF, one stamped+forced rescue
        self.panes = [
            {"pid": 57699, "resume": "aaaa1111-integrator",
             "session": "aaaa1111-integrator", "child": False,
             "ancestor_sid8": "", "force": False},
            {"pid": 622078, "resume": "deadbeef-memory-only",
             "session": "deadbeef-memory-only", "child": True,
             "ancestor_sid8": "85935aed", "force": False},
            {"pid": 998382, "resume": "cccc2222-rescued",
             "session": "cccc2222-rescued", "child": True,
             "ancestor_sid8": "85935aed", "force": True},
        ]
        self._scan = mock.patch.object(session, "_proc_claude_rows",
                                       return_value=self.panes)
        self._scan.start()

    def tearDown(self):
        self._scan.stop()
        if self._home is None:
            os.environ.pop("HELM_HOME", None)
        else:
            os.environ["HELM_HOME"] = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cv_ok(self, stdout="cv-out"):
        return mock.patch.object(session, "_cv", return_value=(0, stdout, ""))

    def cv_absent(self):
        return mock.patch.object(session, "_cv",
                                 return_value=(127, "", "cv not installed"))

    def persisting(self, *sids):
        out = {}
        for sid in sids:
            path = os.path.join(self.tmp, sid + ".jsonl")
            with open(path, "w") as f:
                f.write("{}\n")
            out[sid] = path
        return out


class RuntimeConfigContextTest(unittest.TestCase):
    SID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

    def snapshot(self, env, pid=41, resume=None, child=False):
        argv = ["claude"] + (["--resume", resume] if resume else [])
        selected = dict(env)
        if child:
            selected["CLAUDE_CODE_CHILD_SESSION"] = "1"
        return "ok", {"pid": pid, "uid": os.geteuid(), "start": "123",
                      "env": selected, "argv": argv,
                      "cmdline": b"claude\0", "environ": b"HOME=/agent\0",
                      "cwd": "/launcher"}

    def resolve(self, env, declared=SID, root="/agent/.claude", resume=None,
                child=False, match=True, event_cwd="/turn"):
        snap = self.snapshot(env, resume=resume, child=child)
        with mock.patch.object(session, "_session_record",
                               return_value=(declared, "record-ok", root)), \
                mock.patch.object(session, "_census_matches",
                                  return_value=match):
            return session.runtime_config_for_session(
                self.SID, 41, event_cwd, snapshot=snap)

    def test_provider_rule_distinguishes_absent_empty_and_explicit(self):  # noqa: VACUOUS_ASSERTION — fixed five-case table includes two positive authorities and three explicit refusals
        cases = (
            ({"HOME": "/agent"}, ("/agent/.claude", "agent-HOME-default")),
            ({"HOME": "/agent", "CLAUDE_CONFIG_DIR": "/explicit/../cfg"},
             ("/cfg", "CLAUDE_CONFIG_DIR")),
            ({"HOME": "/agent", "CLAUDE_CONFIG_DIR": ""}, (None, None)),
            ({}, (None, None)),
            ({"HOME": "relative"}, (None, None)),
        )
        for env, expected in cases:
            with self.subTest(env=env):
                self.assertEqual(runtime_config.resolve("claude", env), expected)

    def test_targeted_process_preserves_event_cwd_and_ignores_global_failure(self):
        snap = self.snapshot({"HOME": "/agent"})
        with mock.patch.object(session, "_runtime_config_snapshot",
                               return_value=snap) as targeted, \
                mock.patch.object(session, "_proc_claude_census",
                                  side_effect=AssertionError("global census")), \
                mock.patch.object(who, "scan",
                                  side_effect=AssertionError("global who")), \
                mock.patch.object(session, "_session_record",
                                  return_value=(self.SID, "record-ok",
                                                "/agent/.claude")), \
                mock.patch.object(session, "_census_matches", return_value=True):
            got, err = session.runtime_config_for_session(
                self.SID, 41, "/turn")
        targeted.assert_called_once_with(41)
        self.assertIsNone(err)
        self.assertEqual(got["cwd"], "/turn")
        self.assertEqual(got["config_home"], "/agent/.claude")
        self.assertEqual(got["config_home_source"], "agent-HOME-default")
        self.assertEqual((got["pid"], got["start"]), (41, "123"))

    def test_explicit_home_and_persistent_child_holder_are_authoritative(self):
        got, err = self.resolve(
            {"HOME": "/agent", "CLAUDE_CONFIG_DIR": "/explicit"},
            root="/explicit", child=True)
        self.assertIsNone(err)
        self.assertEqual(got["config_home"], "/explicit")
        self.assertEqual(got["config_home_source"], "CLAUDE_CONFIG_DIR")

    def test_inherited_or_sessionless_helper_cannot_mint_identity(self):
        got, err = self.resolve({"HOME": "/agent"}, declared=None,
                                child=True)
        self.assertIsNone(got)
        self.assertEqual(err, "session-unresolved")

    def test_present_empty_config_and_missing_home_refuse(self):  # noqa: VACUOUS_ASSERTION — fixed three-case loop exercises empty explicit, missing HOME, and relative HOME refusals
        for env in ({"HOME": "/agent", "CLAUDE_CONFIG_DIR": ""}, {},
                    {"HOME": "relative"}):
            with self.subTest(env=env):
                got, err = self.resolve(env, root=None)
                self.assertIsNone(got)
                self.assertEqual(err, "config-unproven")

    def test_session_conflict_and_pid_generation_change_refuse(self):
        other = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        got, err = self.resolve({"HOME": "/agent"}, resume=other)
        self.assertIsNone(got)
        self.assertEqual(err, "session-ambiguous")
        got, err = self.resolve({"HOME": "/agent"}, match=False)
        self.assertIsNone(got)
        self.assertEqual(err, "pid-generation-changed")
        got, err = session.runtime_config_for_session(
            self.SID, 42, "/turn", snapshot=self.snapshot({"HOME": "/agent"}))
        self.assertIsNone(got)
        self.assertEqual(err, "pid-mismatch")


class ScanTest(SessionBase):
    def test_live_sids_use_resume_not_inherited_ancestor(self):
        sids = session.live_sids()
        self.assertIn("deadbeef-memory-only", sids)
        self.assertEqual(sids["deadbeef-memory-only"], [622078])
        # Every child inherited the daemon's ancestor SID. It is not the
        # child's own session and must never invent holders/double-opens.
        self.assertNotIn("85935aed", sids)

    def test_who_holder_accepts_only_fresh_exact_identity(self):
        sid = "33333333-3333-3333-3333-333333333333"
        self.assertEqual(session._who_holder_sid({
            "session": sid, "session_candidates": []}), sid)
        self.assertIsNone(session._who_holder_sid({
            "session": None, "session_candidates": [sid]}))
        self.assertIsNone(session._who_holder_sid({
            "session": "not-a-session", "session_candidates": []}))

    def test_cwd_session_ids_returns_full_ambiguous_set(self):
        cwd = "/work/a.b"
        d = os.path.join(self.tmp, "projects", "-work-a-b")
        os.makedirs(d)
        sids = ["11111111-1111-1111-1111-111111111111",
                "22222222-2222-2222-2222-222222222222"]
        for sid in sids:
            open(os.path.join(d, sid + ".jsonl"), "w").close()
        open(os.path.join(d, "ignore.txt"), "w").close()
        self.assertEqual(session._cwd_session_ids(self.tmp, cwd), sorted(sids))

    def test_live_sids_include_exact_fresh_session_attribution(self):
        self.panes.append({"pid": 701, "resume": None,
                           "session": "fresh3333-session", "child": False,
                           "ancestor_sid8": "", "force": False})
        self.assertEqual(session.open_pids("fresh3333"), [701])

    def test_open_pids_fail_closed_across_ambiguous_cwd_candidates(self):
        self.panes.append({"pid": 702, "resume": None, "session": None,
                           "possible_sessions": ["old11111-session",
                                                 "active22-session"],
                           "child": False, "ancestor_sid8": "", "force": False})
        self.assertEqual(session.open_pids("active22"), [702])

    def test_open_pids_prefix_both_ways(self):
        self.assertEqual(session.open_pids("deadbeef"), [622078])
        self.assertEqual(session.open_pids("deadbeef-memory-only"), [622078])
        self.assertEqual(session.open_pids("nosuch"), [])

    def test_ls_unknown_is_neither_persisted_nor_memory_only(self):
        rows = [{"pid": 702, "resume": None, "declared": None,
                 "declared_reason": "record-stale", "session": None,
                 "possible_sessions": ["active22-session"], "child": False,
                 "ancestor_sid8": "", "force": False}]
        with mock.patch.object(session, "_proc_claude_rows", return_value=rows), \
             mock.patch.object(session, "_persisting_sids", return_value={}):
            rc, out, err = run(session.cmd_ls, [])
        self.assertEqual(rc, 0, err)
        self.assertIn("UNKNOWN", out)
        self.assertIn("no safety or absence claim", out)
        self.assertNotIn("MEMORY-ONLY", out)
        self.assertNotIn("persisted", out)
        with mock.patch.object(session, "_proc_claude_rows", return_value=rows), \
             mock.patch.object(session, "_persisting_sids", return_value={}):
            cert_rc, cert_out, cert_err = run(session.cmd_doctor_panes, [])
        self.assertEqual(cert_rc, 1, cert_err)
        self.assertIn("UNKNOWN", cert_out)

    def test_incomplete_transcript_census_is_unknown_not_memory_only(self):
        rows = [{"pid": 703, "resume": "aaaa1111-integrator",
                 "declared": None, "session": "aaaa1111-integrator",
                 "possible_sessions": [], "child": False,
                 "ancestor_sid8": "", "force": False}]
        census = session._PersistenceCensus({}, complete=False)
        with mock.patch.object(session, "_proc_claude_rows", return_value=rows), \
             mock.patch.object(session, "_persisting_sids", return_value=census):
            rc, out, err = run(session.cmd_ls, [])
        self.assertEqual(rc, 0, err)
        self.assertIn("UNKNOWN (transcript census incomplete", out)
        self.assertNotIn("MEMORY-ONLY", out)
        self.assertEqual(session.memory_only_panes(rows, census), [])

    def test_memory_only_panes_uses_transcript_truth_not_env(self):
        # Both stamped and unstamped panes with transcripts are persisting;
        # deadbeef has none and is the only at-risk row.
        with mock.patch.object(session, "_persisting_sids", return_value=
                               self.persisting("aaaa1111-integrator",
                                               "cccc2222-rescued")):
            mo = session.memory_only_panes()
        self.assertEqual([r["pid"] for r in mo], [622078])

        # The dangerous inverse: a top-level pane without a transcript is also
        # memory-only; FORCE/stamp state never asserts persistence by itself.
        with mock.patch.object(session, "_persisting_sids", return_value=
                               self.persisting("cccc2222-rescued")):
            mo = session.memory_only_panes()
        self.assertEqual([r["pid"] for r in mo], [57699, 622078])

        # A stamped-without-FORCE pane that has a transcript is persisting (the
        # capcom case); the old env heuristic falsely included it.
        with mock.patch.object(session, "_persisting_sids", return_value=
                               self.persisting("deadbeef-memory-only",
                                               "cccc2222-rescued")):
            mo = session.memory_only_panes()
        self.assertEqual([r["pid"] for r in mo], [57699])

    def test_ls_persistence_is_transcript_truth_not_env(self):
        # persistence is decided by the on-disk transcript, NOT the env stamp
        # (transcript-on-disk-is-persistence-truth-not-env). A second pane
        # double-opens deadbeef; cccc2222 has a transcript, the rest don't.
        self.panes.append({"pid": 700, "resume": "deadbeef-memory-only",
                           "session": "deadbeef-memory-only", "child": False,
                           "ancestor_sid8": "", "force": False})
        with mock.patch.object(session, "_persisting_sids", return_value=
                               self.persisting("cccc2222-rescued")):
            rc, out, _ = run(session.cmd_ls, [])
        self.assertEqual(rc, 0)
        line = {l.split()[1]: l for l in out.splitlines() if l.strip().startswith("pid")}
        # cccc2222 (stamped+forced) HAS a transcript -> persisting, the capcom case
        self.assertIn("persisted", line["998382"])
        # deadbeef (stamped, no FORCE) has NO transcript -> genuinely memory-only
        self.assertIn("MEMORY-ONLY (stamped, no FORCE)", line["622078"])
        # aaaa1111 is TOP-LEVEL but has NO transcript -> still at-risk (the old
        # `not child => persisted` rule mislabeled this safe: the dangerous way)
        self.assertIn("MEMORY-ONLY (no transcript on disk)", line["57699"])
        self.assertIn("DOUBLE-OPEN", out)  # deadbeef open in 622078 + 700

    def test_doctor_panes_fails_bad_states_and_passes_clear_inventory(self):
        with mock.patch.object(session, "_persisting_sids", return_value=
                               self.persisting("cccc2222-rescued")):
            bad_rc, bad_out, bad_err = run(session.cmd_doctor_panes, [])
        self.assertEqual(bad_rc, 1, bad_err)
        self.assertIn("MEMORY-ONLY", bad_out)

        with mock.patch.object(session, "_persisting_sids", return_value=
                               self.persisting("aaaa1111-integrator",
                                               "deadbeef-memory-only",
                                               "cccc2222-rescued")):
            clear_rc, clear_out, clear_err = run(session.cmd_doctor_panes, [])
        self.assertEqual(clear_rc, 0, clear_err)
        self.assertNotIn("MEMORY-ONLY", clear_out)
        self.assertNotIn("DOUBLE-OPEN", clear_out)
        self.assertNotIn("UNKNOWN", clear_out)

        self.panes.append({"pid": 700, "resume": "aaaa1111-integrator",
                           "session": "aaaa1111-integrator", "child": False,
                           "ancestor_sid8": "", "force": False})
        with mock.patch.object(session, "_persisting_sids", return_value=
                               self.persisting("aaaa1111-integrator",
                                               "deadbeef-memory-only",
                                               "cccc2222-rescued")):
            double_rc, double_out, double_err = run(session.cmd_doctor_panes, [])
        self.assertEqual(double_rc, 1, double_err)
        self.assertIn("DOUBLE-OPEN", double_out)


class LawTest(SessionBase):
    """LAW 1 (single-open) + LAW 2 (print-don't-launch)."""
    def _sid(self, full="deadbeef-memory-only"):
        return mock.patch.object(session, "_resolve_sid",
                                 return_value=(full, None))

    def test_resume_refuses_live_open_copy(self):
        with self._sid():
            rc, _out, err = run(session.cmd_resume, ["deadbeef"])
        self.assertEqual(rc, 1)
        self.assertIn("LAW 1", err)
        self.assertIn("622078", err)

    def test_resume_launch_also_gated(self):
        with self._sid(), self.cv_ok() as cv:
            rc, _o, err = run(session.cmd_resume, ["deadbeef", "--launch"])
        self.assertEqual(rc, 1)
        self.assertIn("NOT launching", err)
        cv.assert_not_called()  # never reaches cv with a live copy open

    def test_cv_resume_spec_resolves_the_actual_harness_and_cwd(self):
        with mock.patch.object(
                session, "_cv",
                return_value=(0, "cd '/source cwd'\nclaude --resume sid-a\n", "")):
            argv, env, cwd, err = session._cv_resume_spec("sid-a")
        self.assertIsNone(err)
        self.assertEqual(argv, ["claude", "--resume", "sid-a"])
        self.assertEqual(cwd, "/source cwd")
        self.assertEqual(env[session.FORCE_VAR], "1")

    def test_cv_resume_spec_refuses_multiple_native_commands(self):
        with mock.patch.object(
                session, "_cv",
                return_value=(0, "claude --resume sid-a\ncodex resume sid-b\n", "")):
            argv, env, cwd, err = session._cv_resume_spec("sid-a")
        self.assertEqual((argv, env, cwd), (None, None, None))
        self.assertIn("expected exactly one", err)

    def test_cv_resume_spec_refuses_shell_control_from_cv_output(self):
        with mock.patch.object(
                session, "_cv",
                return_value=(0, "claude --resume sid-a ; other\n", "")):
            argv, env, cwd, err = session._cv_resume_spec("sid-a")
        self.assertEqual((argv, env, cwd), (None, None, None))
        self.assertIn("no executable native launch command", err)

    def test_resume_launch_uses_attached_cv_with_clean_env(self):
        dirty = {v: "inherited" for v in session._stamp_vars()}
        with mock.patch.dict(os.environ, dirty), \
             mock.patch.object(session, "_cv", return_value=(
                 0, "cd '/source cwd'\nclaude --resume sid-a\n", "")) as cv, \
             mock.patch.object(session.seat_launch_owner, "exec_attached",
                               return_value=0) as owner:
            rc, err = session._cv_launch("sid-a")
        self.assertEqual((rc, err), (0, ""))
        cv.assert_called_once()
        self.assertEqual(cv.call_args.args, ("resume", "sid-a"))
        resolved_env = cv.call_args.kwargs["env"]
        for name in session._stamp_vars():
            self.assertNotIn(name, resolved_env)
        self.assertEqual(resolved_env[session.FORCE_VAR], "1")
        argv, owner_env, cwd = owner.call_args.args
        self.assertEqual(argv, ["claude", "--resume", "sid-a"])
        self.assertEqual(cwd, "/source cwd")
        for name in session._stamp_vars():
            self.assertNotIn(name, owner_env)
        self.assertEqual(owner_env[session.FORCE_VAR], "1")

    def test_resume_launch_reports_the_actual_harness_status(self):
        with mock.patch.object(session, "_cv_resume_spec", return_value=(
                ["claude", "--resume", "sid-a"], {}, None, None)), \
             mock.patch.object(session.seat_launch_owner, "exec_attached",
                               return_value=23):
            rc, err = session._cv_launch("sid-a")
        self.assertEqual(rc, 23)
        self.assertEqual(err, "claude exited with status 23")

    def test_resume_launch_is_serialized_and_rechecked(self):
        with self._sid("aaaa1111-integrator"), \
             mock.patch.object(session, "open_pids", return_value=[]), \
             mock.patch.object(session, "_cv_launch", return_value=(0, "")) as launch:
            with session._launch_lock("aaaa1111-integrator"):
                rc, _out, err = run(session.cmd_resume,
                                    ["aaaa1111", "--launch"])
        self.assertEqual(rc, 1)
        self.assertIn("already in progress", err)
        launch.assert_not_called()

    def test_launch_lock_survives_the_headless_exec_boundary(self):
        opened = mock.mock_open()
        opened.return_value.fileno.return_value = 42
        with mock.patch("builtins.open", opened), \
             mock.patch.object(session.os, "makedirs"), \
             mock.patch.object(session.os, "set_inheritable") as inheritable, \
             mock.patch.object(session.fcntl, "flock") as flock:
            with session._launch_lock("aaaa1111-integrator") as acquired:
                self.assertTrue(acquired)
                inheritable.assert_called_once_with(42, True)
        self.assertEqual(inheritable.call_args_list,
                         [mock.call(42, True), mock.call(42, False)])
        self.assertEqual(flock.call_args_list, [
            mock.call(42, session.fcntl.LOCK_EX | session.fcntl.LOCK_NB),
            mock.call(42, session.fcntl.LOCK_UN)])

    def test_resume_launch_preserves_the_actual_harness_status(self):
        with self._sid("aaaa1111-integrator"), \
             mock.patch.object(session, "open_pids", return_value=[]), \
             mock.patch.object(session, "_cv_launch",
                               return_value=(23, "claude exited with status 23")):
            rc, out, err = run(session.cmd_resume,
                               ["aaaa1111", "--launch"])
        self.assertEqual(rc, 23)
        self.assertEqual(out, "")
        self.assertIn("status 23", err)

    def test_resume_launch_calls_attached_helper_after_guard(self):
        with self._sid("aaaa1111-integrator"), \
             mock.patch.object(session, "open_pids", return_value=[]), \
             mock.patch.object(session, "_cv_launch", return_value=(0, "")) as launch:
            rc, out, err = run(session.cmd_resume,
                               ["aaaa1111", "--launch"])
        self.assertEqual((rc, out, err), (0, "", ""))
        launch.assert_called_once_with("aaaa1111-integrator")

    def test_resume_prints_incantation_with_force(self):
        with self._sid("aaaa1111-integrator"), \
             mock.patch.object(session, "open_pids", return_value=[]), \
             mock.patch.object(session, "_session_cwd", return_value="/source cwd"):
            rc, out, _ = run(session.cmd_resume, ["aaaa1111"])
        self.assertEqual(rc, 0)
        self.assertIn("claude --resume aaaa1111-integrator", out)
        self.assertIn("cd '/source cwd'", out)
        self.assertIn("CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1", out)
        # child-stamp unsets ride the line (paste-into-stamped-pane safe)
        for v in session._stamp_vars():
            self.assertIn("-u " + v, out)

    def test_resume_refuses_command_without_recorded_cwd(self):
        with self._sid("aaaa1111-integrator"), \
             mock.patch.object(session, "open_pids", return_value=[]), \
             mock.patch.object(session, "_session_row", return_value=None):
            rc, out, err = run(session.cmd_session,
                               ["resume", "aaaa1111"])
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertIn("no recorded cwd", err)

    def test_port_refuses_live_and_prints_when_clear(self):
        cred = os.path.join(self.tmp, "ops example")
        os.makedirs(cred)
        cwd = "/work/project with spaces"
        with open(os.path.join(cred, ".claude.json"), "w") as f:
            json.dump({"projects": {cwd: {"hasTrustDialogAccepted": True}}}, f)
        home_rows = [{"name": "ops-example", "path": cred, "aliases": [],
                      "projects_link_ok": True}]
        import helm.homes as homes_mod
        with self._sid("aaaa1111-integrator"), \
             mock.patch.object(session, "_session_cwd", return_value=cwd), \
             mock.patch.object(homes_mod, "homes_list", return_value=home_rows):
            # live copy -> refuse (no incantation line printed)
            with mock.patch.object(session, "open_pids", return_value=[57699]):
                rc, out, _ = run(session.cmd_port,
                                 ["--cred", "ops-example", "aaaa1111"])
            self.assertEqual(rc, 1)
            self.assertIn("LAW 1", out)
            self.assertNotIn("claude --resume", out)
            # clear -> print with quoted credhome/cwd + FORCE
            with mock.patch.object(session, "open_pids", return_value=[]):
                rc, out, _ = run(session.cmd_port,
                                 ["--cred", "ops-example", "aaaa1111"])
            self.assertEqual(rc, 0)
            self.assertIn("CLAUDE_CONFIG_DIR='" + cred + "'", out)
            self.assertIn("cd '" + cwd + "'", out)
            self.assertIn("FORCE_SESSION_PERSISTENCE=1", out)

    def test_port_requires_shared_store_and_seeded_trust(self):
        cred = os.path.join(self.tmp, "cred")
        os.makedirs(cred)
        cwd = "/work/project"
        import helm.homes as homes_mod
        with self._sid("aaaa1111-integrator"), \
             mock.patch.object(session, "_session_cwd", return_value=cwd), \
             mock.patch.object(session, "open_pids", return_value=[]):
            owned = [{"name": "x", "path": cred, "aliases": [],
                      "projects_link_ok": False}]
            with mock.patch.object(homes_mod, "homes_list", return_value=owned):
                rc, out, _ = run(session.cmd_port,
                                 ["--cred", "x", "aaaa1111"])
            self.assertEqual(rc, 1)
            self.assertIn("cv port", out)
            self.assertNotIn("claude --resume", out)

            shared = [{"name": "x", "path": cred, "aliases": [],
                       "projects_link_ok": True}]
            with mock.patch.object(homes_mod, "homes_list", return_value=shared):
                rc, out, _ = run(session.cmd_port,
                                 ["--cred", "x", "aaaa1111"])
            self.assertEqual(rc, 1)
            self.assertIn("trust", out)
            self.assertNotIn("claude --resume", out)

    def test_port_refuses_archived_credhome(self):
        import helm.homes as homes_mod
        rows = [{"name": "old", "path": self.tmp, "aliases": [],
                 "provider": "claude", "archived": True, "authed": True,
                 "projects_link_ok": True}]
        with self._sid("aaaa1111-integrator"), \
             mock.patch.object(homes_mod, "homes_list", return_value=rows), \
             mock.patch.object(session, "open_pids", return_value=[]):
            rc, out, err = run(session.cmd_port,
                               ["--cred", "old", "aaaa1111"])
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("archived", err)

    def test_checkpoint_routes_memory_only_to_rescue(self):
        with self._sid("deadbeef-memory-only"):
            rc, _o, err = run(session.cmd_checkpoint, ["deadbeef"])
        self.assertEqual(rc, 1)
        self.assertIn("MEMORY-ONLY", err)
        self.assertIn("rescue", err)

    def test_checkpoint_mints_new_id_original_untouched(self):
        def prune(*argv, **_kwargs):
            newid = argv[argv.index("--to") + 1]
            return 0, json.dumps({"newId": newid}), ""

        with self._sid("aaaa1111-integrator"), \
             mock.patch.object(session, "_proc_claude_rows", return_value=[]), \
             mock.patch.object(session, "_session_cwd", return_value="/source cwd"), \
             mock.patch.object(session, "_cv", side_effect=prune) as cv:
            rc, out, _ = run(session.cmd_checkpoint, ["aaaa1111"])
        self.assertEqual(rc, 0)
        self.assertIn("checkpoint minted:", out)
        self.assertIn("cd '/source cwd'", out)
        # cv prune got --thinking + --to <newid>, never mutating the original
        argv = cv.call_args[0]
        self.assertIn("prune", argv)
        self.assertIn("--thinking", argv)
        self.assertIn("--to", argv)

    def test_checkpoint_rejects_success_without_artifact_receipt(self):
        with self._sid("aaaa1111-integrator"), \
             mock.patch.object(session, "_proc_claude_rows", return_value=[]), \
             mock.patch.object(session, "_session_cwd", return_value="/work"), \
             self.cv_ok('{"ok":true}'):
            rc, out, err = run(session.cmd_checkpoint, ["aaaa1111"])
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("without the requested artifact id", err)

    def test_checkpoint_validates_cwd_before_mutating(self):
        with self._sid("aaaa1111-integrator"), \
             mock.patch.object(session, "_proc_claude_rows", return_value=[]), \
             mock.patch.object(session, "_session_row", return_value=None), \
             mock.patch.object(session, "_cv") as cv, \
             mock.patch.object(pk, "event") as event:
            rc, out, err = run(session.cmd_session,
                               ["checkpoint", "aaaa1111"])
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertIn("no recorded cwd", err)
        cv.assert_not_called()
        event.assert_not_called()

    def test_doctor_propagates_cv_failure_for_persisted_session(self):
        with self._sid("aaaa1111-integrator"), \
             mock.patch.object(session, "_persisting_sids", return_value=
                               self.persisting("aaaa1111-integrator")), \
             mock.patch.object(session, "_cv", return_value=(1, "", "bad session")):
            rc, out, err = run(session.cmd_doctor, ["aaaa1111"])
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("bad session", err)

    def _doctor(self, on_disk, rows):
        """Run doctor for a live sid with a controlled transcript census."""
        with self._sid("aaaa1111-integrator"), \
             mock.patch.object(session, "open_pids", return_value=[901]), \
             mock.patch.object(session, "_persisting_sids",
                               return_value=on_disk), \
             mock.patch.object(session, "_proc_claude_rows",
                               return_value=rows), \
             self.cv_ok("{}"):
            return run(session.cmd_doctor, ["aaaa1111"])

    def test_doctor_flags_an_UNSTAMPED_transcriptless_pane(self):
        """The under-flag, and the reason this fix exists: doctor keyed on the
        env stamp (`child and not force`), so a TOP-LEVEL pane carrying no
        stamp and writing NO transcript reported as plain 'live'. Persistence
        is transcript-truth — the stamp is only ever the REASON, never the
        verdict — and that law landed in `ls` without being propagated here."""
        rows = [{"pid": 901, "child": False, "force": False, "resume": None,
                 "declared": None, "session": "aaaa1111-integrator",
                 "possible_sessions": [], "ancestor_sid8": ""}]
        rc, out, err = self._doctor({}, rows)
        self.assertEqual(rc, 0, err)
        self.assertIn("memory-only", out)
        self.assertIn("rescue", out)          # and it routes to rescue

    def test_doctor_still_names_the_stamped_case_precisely(self):
        rows = [{"pid": 901, "child": True, "force": False, "resume": None,
                 "declared": None, "session": "aaaa1111-integrator",
                 "possible_sessions": [], "ancestor_sid8": ""}]
        rc, out, err = self._doctor({}, rows)
        self.assertEqual(rc, 0, err)
        self.assertIn("bridged-child", out)
        self.assertIn("rescue", out)

    def test_doctor_live_memory_only_needs_neither_catalog_nor_cv(self):
        sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        rows = [{"pid": 901, "child": False, "force": False, "resume": None,
                 "declared": sid, "session": sid, "possible_sessions": [],
                 "ancestor_sid8": ""}]
        with mock.patch.object(session, "_proc_claude_rows", return_value=rows), \
             mock.patch.object(session, "_persisting_sids", return_value={}), \
             mock.patch.object(session, "_resolve_sid") as resolve, \
             mock.patch.object(session, "_cv") as cv:
            rc, out, err = run(session.cmd_doctor, ["aaaaaaaa"])
        self.assertEqual(rc, 0, err)
        self.assertIn("memory-only", out)
        self.assertIn("skipped (no transcript on disk)", out)
        resolve.assert_not_called()
        cv.assert_not_called()

    def test_doctor_calls_a_persisting_pane_live(self):
        rows = [{"pid": 901, "child": True, "force": True, "resume": None,
                 "declared": None, "session": "aaaa1111-integrator",
                 "possible_sessions": [], "ancestor_sid8": ""}]
        rc, out, err = self._doctor(
            self.persisting("aaaa1111-integrator"), rows)
        self.assertEqual(rc, 0, err)
        self.assertIn("live", out)
        self.assertNotIn("memory-only", out)
        self.assertIn("checkpoint", out)      # not the rescue lane

    def test_doctor_keeps_candidate_only_holder_unknown(self):
        rows = [{"pid": 901, "child": False, "force": False, "resume": None,
                 "declared": None, "session": None,
                 "possible_sessions": ["aaaa1111-integrator"],
                 "ancestor_sid8": ""}]
        rc, out, err = self._doctor({}, rows)
        self.assertEqual(rc, 0, err)
        self.assertIn("UNKNOWN", out)
        self.assertIn("verify live pane identity", out)
        self.assertNotIn("memory-only", out)

    def test_doctor_keeps_incomplete_transcript_census_unknown(self):
        rows = [{"pid": 901, "child": False, "force": False, "resume": None,
                 "declared": "aaaa1111-integrator",
                 "session": "aaaa1111-integrator",
                 "possible_sessions": [], "ancestor_sid8": ""}]
        census = session._PersistenceCensus({}, complete=False)
        rc, out, err = self._doctor(census, rows)
        self.assertEqual(rc, 0, err)
        self.assertIn("UNKNOWN (transcript census incomplete)", out)
        self.assertIn("verify live pane identity", out)
        self.assertNotIn("memory-only", out)

    def test_checkpoint_refuses_candidate_only_unknown_holder(self):
        rows = [{"pid": 901, "child": False, "force": False, "resume": None,
                 "declared": None, "session": None,
                 "possible_sessions": ["aaaa1111-integrator"],
                 "ancestor_sid8": ""}]
        with self._sid("aaaa1111-integrator"), \
             mock.patch.object(session, "_proc_claude_rows", return_value=rows), \
             mock.patch.object(session, "_cv") as cv:
            rc, out, err = run(session.cmd_checkpoint, ["aaaa1111"])
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("UNKNOWN", err)
        cv.assert_not_called()

    def test_checkpoint_refuses_incomplete_transcript_census(self):
        rows = [{"pid": 901, "child": False, "force": False, "resume": None,
                 "declared": "aaaa1111-integrator",
                 "session": "aaaa1111-integrator",
                 "possible_sessions": [], "ancestor_sid8": ""}]
        census = session._PersistenceCensus({}, complete=False)
        with self._sid("aaaa1111-integrator"), \
             mock.patch.object(session, "_proc_claude_rows", return_value=rows), \
             mock.patch.object(session, "_persisting_sids", return_value=census), \
             mock.patch.object(session, "_cv") as cv:
            rc, out, err = run(session.cmd_checkpoint, ["aaaa1111"])
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("UNKNOWN", err)
        cv.assert_not_called()

    def test_cv_absent_degrades_clean_for_persisted_session(self):
        with self._sid("aaaa1111-integrator"), \
             mock.patch.object(session, "_persisting_sids", return_value=
                               self.persisting("aaaa1111-integrator")), \
             self.cv_absent():
            rc, _o, err = run(session.cmd_doctor, ["aaaa1111"])
        self.assertEqual(rc, 1)
        self.assertIn("cv not installed", err)
        self.assertNotIn("Traceback", err)


class RescueTest(SessionBase):
    def test_rescue_live_pid_prints_close_first_not_incantation(self):
        with self.cv_ok('{"compactions":0}'), \
             mock.patch.object(session, "_resolve_sid",
                               return_value=("deadbeef-memory-only", None)):
            rc, out, _ = run(session.cmd_rescue, ["622078"])
        self.assertEqual(rc, 1)
        self.assertIn("deadbeef", out)          # resolved from the pid
        self.assertIn("HARVEST", out)            # the rescue-lane first step
        self.assertIn("LAW 1", out)
        self.assertNotIn("claude --resume", out)

    def test_rescue_never_mistakes_stamp_ancestor_for_own_sid(self):
        self.panes[1]["resume"] = None
        self.panes[1]["session"] = None
        rc, _out, err = run(session.cmd_rescue, ["622078"])
        self.assertEqual(rc, 1)
        self.assertIn("spawning ancestor, not this pane", err)
        self.assertIn("85935aed", err)

    def test_rescue_unknown_pid(self):
        rc, _o, err = run(session.cmd_rescue, ["424242"])
        self.assertEqual(rc, 1)
        self.assertIn("no live claude pid", err)

    def test_rescue_pid_uses_declared_identity_without_catalog_or_transcript(self):
        sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        for child in (False, True):
            with self.subTest(child=child):
                row = {"pid": 901, "child": child, "force": False,
                       "resume": None, "declared": sid, "session": sid,
                       "possible_sessions": [], "ancestor_sid8": "85935aed"}
                with mock.patch.object(session, "_proc_claude_rows",
                                       return_value=[row]), \
                     mock.patch.object(session, "_persisting_sids", return_value={}), \
                     mock.patch.object(session, "_resolve_sid") as resolve, \
                     mock.patch.object(session, "_cv") as cv:
                    rc, out, err = run(session.cmd_rescue, ["901"])
                self.assertEqual(rc, 1, err)
                self.assertIn("pid 901 -> sid aaaaaaaa", out)
                self.assertIn("memory-only", out)
                self.assertIn("HARVEST", out)
                self.assertIn("SELF-RECAP", out)
                self.assertNotIn("claude --resume", out)
                resolve.assert_not_called()
                cv.assert_not_called()


class ExpertsTest(SessionBase):
    def _reg(self, sid="aaaa1111-integrator", domain="helm-orchestration"):
        with mock.patch.object(session, "_resolve_sid",
                               return_value=(sid, None)):
            return run(session.cmd_experts,
                       ["--register", sid, "--domain", domain,
                        "--note", "the integrator"])

    def test_register_and_list(self):
        rc, out, _ = self._reg()
        self.assertEqual(rc, 0)
        self.assertIn("registered", out)
        # durable: a fresh read sees it
        rc, out, _ = run(session.cmd_experts, [])
        self.assertIn("helm-orchestration", out)
        self.assertIn("aaaa1111-int", out)
        # the registry file is on disk under _global
        self.assertTrue(os.path.exists(session._experts_path()))

    def test_register_unknown_sid_refused(self):
        with mock.patch.object(session, "_resolve_sid",
                               return_value=(None, "no such session")):
            rc, _o, err = run(session.cmd_experts,
                              ["--register", "ghost", "--domain", "x"])
        self.assertEqual(rc, 1)
        self.assertIn("no such session", err)

    def test_refresh(self):
        self._reg()
        rc, out, _ = run(session.cmd_experts, ["--refresh", "aaaa1111"])
        self.assertEqual(rc, 0)
        self.assertIn("refreshed", out)

    def test_refresh_refuses_ambiguous_prefix(self):
        session._write_experts({
            "aaaa1111-one": {"domain": "x", "last_refreshed": "2026-01-01T00:00:00Z"},
            "aaaa1111-two": {"domain": "y", "last_refreshed": "2026-01-01T00:00:00Z"},
        })
        rc, _out, err = run(session.cmd_experts, ["--refresh", "aaaa1111"])
        self.assertEqual(rc, 1)
        self.assertIn("disambiguate", err)

    def test_ask_routes_to_freshest_expert_for_domain(self):
        session._write_experts({
            "aaaa1111-old": {"domain": "helm", "last_refreshed": "2026-01-01T00:00:00Z"},
            "bbbb2222-new": {"domain": "helm", "last_refreshed": "2026-07-21T00:00:00Z"},
        })
        with mock.patch.object(session, "open_pids", return_value=[]), \
             mock.patch.object(session, "_grep_expert", return_value={"hits": ""}), \
             mock.patch.object(session, "_session_cwd", return_value="/work"), \
             self.cv_ok(""):
            rc, out, _ = run(session.cmd_ask, ["helm", "next"])
        self.assertEqual(rc, 0)
        self.assertIn("bbbb2222-new", out)
        self.assertNotIn("claude --resume aaaa1111-old", out)

    def test_pack_query_preserves_unicode_and_developer_terms(self):
        q = session._pack_query("日本語-domain", "C++ と .claude の直し方は？")
        self.assertIn("日本語", q)
        self.assertIn("C plus plus", q)
        self.assertIn("dot claude", q)
        self.assertIn("直し方は", q)
        self.assertNotIn(":", q)

    def test_ask_no_expert_falls_back_to_search(self):
        with self.cv_ok("search-hits") as cv:
            rc, out, _ = run(session.cmd_ask, ["nosuchdomain", "how", "does", "x"])
        self.assertEqual(rc, 0)
        self.assertIn("no expert for domain", out)
        self.assertIn("search-hits", out)

    def test_ask_with_expert_prints_regounded_resume(self):
        self._reg()
        with mock.patch.object(session, "open_pids", return_value=[]), \
             mock.patch.object(session, "_session_cwd", return_value="/work"), \
             self.cv_ok(""):
            rc, out, _ = run(session.cmd_ask, ["helm-orchestration", "what", "next"])
        self.assertEqual(rc, 0)
        self.assertIn("expert for 'helm-orchestration'", out)
        self.assertIn("claude --resume aaaa1111-integrator", out)
        self.assertIn("RE-GROUND (mandatory)", out)  # expertise goes stale
        self.assertIn("FORCE_SESSION_PERSISTENCE=1", out)

    def test_ask_live_expert_names_the_pane(self):
        self._reg()
        with mock.patch.object(session, "open_pids", return_value=[57699]), \
             self.cv_ok(""):
            rc, out, _ = run(session.cmd_ask, ["helm-orchestration", "q"])
        self.assertIn("LIVE in pid(s) [57699]", out)
        self.assertNotIn("claude --resume", out)

    def test_ask_scopes_search_to_the_expert_transcript(self):
        """the ladder's rung 2 is the EXPERT's own transcript, not the corpus —
        a hit there is shown as such; a miss falls through to a context pack."""
        self._reg()
        # expert's own transcript HAS the answer -> shown, no corpus fallback
        with mock.patch.object(session, "_grep_expert", return_value={"hits": "  …the order is X…"}), \
             mock.patch.object(session, "open_pids", return_value=[]), \
             mock.patch.object(session, "_session_cwd", return_value="/work"):
            rc, out, _ = run(session.cmd_ask, ["helm-orchestration", "order"])
        self.assertIn("expert-transcript hits", out)
        self.assertNotIn("context pack", out)
        # expert's transcript misses -> falls through to the context pack
        with mock.patch.object(session, "_grep_expert", return_value={"hits": ""}), \
             mock.patch.object(session, "open_pids", return_value=[]), \
             mock.patch.object(session, "_session_cwd", return_value="/work"), \
             self.cv_ok("pack-hits") as cv:
            rc, out, _ = run(session.cmd_ask, ["helm-orchestration", "order"])
        self.assertIn("context pack", out)
        self.assertIn("pack-hits", out)
        self.assertEqual(cv.call_args[0],
                         ("pack", "--limit", "3", "helm orchestration order"))

    def _expert_grep_failure(self, exc):
        path = os.path.join(self.tmp, "expert.jsonl")
        with open(path, "w") as f:
            f.write('{"text":"the --planted-marker must hit"}\n')
        row = {"i": "aaaa1111-integrator", "p": path}
        with mock.patch("helm.sessions.rows_for", return_value=[row]):
            real_run = subprocess.run
            with mock.patch("subprocess.run", wraps=real_run) as run:
                control = session._grep_expert(
                    "aaaa1111-integrator", "--planted-marker")
            self.assertIn("--planted-marker", control["hits"],
                          "must-hit control did not reach the planted expert marker")
            argv = run.call_args.args[0]
            self.assertEqual(argv[0], "/usr/bin/grep")
            self.assertEqual(argv[argv.index("--") + 1], "--planted-marker")
            with mock.patch("subprocess.run", side_effect=exc):
                return session._grep_expert("aaaa1111-integrator", "--planted-marker")

    def test_expert_grep_timeout_is_unavailable_not_no_hit(self):
        res = self._expert_grep_failure(subprocess.TimeoutExpired(["grep"], 15))
        self.assertEqual(res["hits"], "")
        self.assertEqual(res["unavailable"]["kind"], "timeout")

    def test_expert_grep_oserror_is_unavailable_not_no_hit(self):
        res = self._expert_grep_failure(OSError("forced exec failure"))
        self.assertEqual(res["hits"], "")
        self.assertEqual(res["unavailable"]["kind"], "exec")

    def test_ask_names_unavailable_expert_transcript_before_context_pack(self):
        self._reg()
        issue = {"stage": "expert-transcript", "kind": "timeout",
                 "message": "expert transcript grep timed out"}
        with mock.patch.object(session, "_grep_expert", return_value={
                "hits": "", "unavailable": issue}), \
             mock.patch.object(session, "open_pids", return_value=[]), \
             mock.patch.object(session, "_session_cwd", return_value="/work"), \
             self.cv_ok("pack-hits"):
            rc, out, _ = run(session.cmd_ask, ["helm-orchestration", "order"])
        self.assertEqual(rc, 0)
        self.assertIn("expert transcript unavailable", out)
        self.assertIn("context pack", out)
        self.assertNotIn("no hit in the expert's own transcript", out)


class CliWiringTest(unittest.TestCase):
    def test_verb_registered_and_dispatch_clean(self):
        from helm import cli
        self.assertIn("session", cli.VERBS)
        self.assertIn("session", cli._VERB_HELP)
        rc, _o, err = run(session.cmd_session, ["bogus-verb"])
        self.assertEqual(rc, 2)
        self.assertIn("usage: helm session", err)
        self.assertNotIn("Traceback", err)


class DeclaredSidTest(unittest.TestCase):
    """The authoritative resolution rung: Claude Code's own pid-keyed
    sessions/<pid>.json. Tested against THIS process and the LIVE kernel, so a
    wrong /proc/<pid>/stat field index fails here instead of silently
    resolving every pane to None (the shape that hid two UNKNOWN panes, and
    with them a real law-1 double-open, behind a 131-candidate guess)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.pid = os.getpid()
        os.makedirs(os.path.join(self.tmp, "sessions"))

    def real_starttime(self):
        with open("/proc/%d/stat" % self.pid, "rb") as f:
            tail = f.read().decode("utf-8", "replace").rpartition(")")[2]
        return tail.split()[19]

    def write(self, **over):
        rec = {"pid": self.pid, "sessionId": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
               "procStart": self.real_starttime()}
        rec.update(over)
        path = os.path.join(self.tmp, "sessions", "%d.json" % rec.get("pid", self.pid))
        with open(path, "w") as f:
            json.dump(rec, f)
        return path

    def test_resolves_against_live_proc_starttime(self):
        self.write()
        self.assertEqual(session._sid_from_session_file(self.pid, self.tmp),
                         "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

    def test_refuses_recycled_pid(self):
        # Same pid, a DIFFERENT boot-relative start: the record describes a
        # dead process whose pid the kernel handed to someone else.
        self.write(procStart=str(int(self.real_starttime()) + 1))
        self.assertIsNone(session._sid_from_session_file(self.pid, self.tmp))

    def test_refuses_record_naming_another_pid(self):
        self.write(pid=self.pid + 1)
        # Written under our own filename, but the body disowns us.
        os.rename(os.path.join(self.tmp, "sessions", "%d.json" % (self.pid + 1)),
                  os.path.join(self.tmp, "sessions", "%d.json" % self.pid))
        self.assertIsNone(session._sid_from_session_file(self.pid, self.tmp))

    def test_absent_and_malformed_are_quiet_misses(self):
        self.assertIsNone(session._sid_from_session_file(self.pid, self.tmp))
        with open(os.path.join(self.tmp, "sessions", "%d.json" % self.pid), "w") as f:
            f.write("{not json")
        self.assertIsNone(session._sid_from_session_file(self.pid, self.tmp))

    def test_missing_procstart_is_unknown_not_identity(self):
        self.write(procStart=None)
        self.assertIsNone(session._sid_from_session_file(self.pid, self.tmp))

    def test_rejects_non_utf8_symlink_fifo_and_oversize_records(self):
        path = os.path.join(self.tmp, "sessions", "%d.json" % self.pid)
        with open(path, "wb") as f:
            f.write(b"\xff")
        self.assertIsNone(session._sid_from_session_file(self.pid, self.tmp))

        os.unlink(path)
        target = os.path.join(self.tmp, "cross-home.json")
        with open(target, "w") as f:
            json.dump({"pid": self.pid, "sessionId":
                       "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                       "procStart": self.real_starttime()}, f)
        os.symlink(target, path)
        self.assertIsNone(session._sid_from_session_file(self.pid, self.tmp))

        os.unlink(path)
        os.mkfifo(path)
        self.assertIsNone(session._sid_from_session_file(self.pid, self.tmp))

        os.unlink(path)
        with open(path, "wb") as f:
            f.write(b" " * (session._SESSION_RECORD_MAX + 1))
        self.assertIsNone(session._sid_from_session_file(self.pid, self.tmp))

        os.unlink(path)
        self.write()
        os.chmod(path, 0)
        try:
            self.assertIsNone(session._sid_from_session_file(self.pid, self.tmp))
        finally:
            os.chmod(path, 0o600)

    def test_rejects_wrong_types_owner_and_record_replacement(self):
        self.write(sessionId=7)
        self.assertIsNone(session._sid_from_session_file(self.pid, self.tmp))
        self.write(procStart=int(self.real_starttime()))
        self.assertIsNone(session._sid_from_session_file(self.pid, self.tmp))
        self.write()
        self.assertEqual(session._read_session_record(
            self.tmp, self.pid, os.geteuid() + 1, self.real_starttime())[1],
            "record-unsafe")
        with mock.patch.object(session, "_record_unchanged", return_value=False):
            self.assertIsNone(session._sid_from_session_file(self.pid, self.tmp))

    def test_explicit_config_homes_do_not_cross_resolve(self):
        self.write(sessionId="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        other = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, other, ignore_errors=True)
        os.mkdir(os.path.join(other, "sessions"))
        with open(os.path.join(other, "sessions", "%d.json" % self.pid), "w") as f:
            json.dump({"pid": self.pid,
                       "sessionId": "bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
                       "procStart": self.real_starttime()}, f)
        self.assertEqual(session._sid_from_session_file(self.pid, self.tmp),
                         "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        self.assertEqual(session._sid_from_session_file(self.pid, other),
                         "bbbbbbbb-cccc-dddd-eeee-ffffffffffff")

    def test_config_home_rejects_caller_tilde_relative_and_writable_paths(self):
        self.write()
        self.assertIsNone(session._sid_from_session_file(self.pid, "~/.claude"))
        self.assertIsNone(session._sid_from_session_file(self.pid, "relative"))
        self.assertIsNone(session._sid_from_session_file(self.pid, self.tmp + "\0x"))

        os.chmod(self.tmp, 0o777)
        try:
            self.assertIsNone(session._sid_from_session_file(self.pid, self.tmp))
        finally:
            os.chmod(self.tmp, 0o700)

        sessions = os.path.join(self.tmp, "sessions")
        os.chmod(sessions, 0o777)
        try:
            self.assertIsNone(session._sid_from_session_file(self.pid, self.tmp))
        finally:
            os.chmod(sessions, 0o755)

        record = os.path.join(sessions, "%d.json" % self.pid)
        os.chmod(record, 0o666)
        try:
            self.assertIsNone(session._sid_from_session_file(self.pid, self.tmp))
        finally:
            os.chmod(record, 0o600)

    def test_config_home_alias_resolves_but_sessions_symlink_does_not(self):
        self.write()
        alias = self.tmp + "-alias"
        os.symlink(self.tmp, alias)
        self.addCleanup(lambda: os.path.lexists(alias) and os.unlink(alias))
        self.assertEqual(session._sid_from_session_file(self.pid, alias),
                         "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

        record = os.path.join(self.tmp, "sessions", "%d.json" % self.pid)
        with open(record, "rb") as f:
            data = f.read()
        os.unlink(record)
        real = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, real, ignore_errors=True)
        os.rmdir(os.path.join(self.tmp, "sessions"))
        os.makedirs(os.path.join(real, "sessions"))
        with open(os.path.join(real, "sessions", "%d.json" % self.pid), "wb") as f:
            f.write(data)
        os.symlink(os.path.join(real, "sessions"), os.path.join(self.tmp, "sessions"))
        self.assertIsNone(session._sid_from_session_file(self.pid, self.tmp))

    def test_missing_record_store_keeps_trusted_home_for_inference(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        sid, reason, trusted = session._session_record(
            self.pid, root, None, os.geteuid(), self.real_starttime())
        self.assertIsNone(sid)
        self.assertEqual(reason, "record-missing")
        self.assertEqual(trusted, root)

    def test_proc_stat_field_22_handles_spaces_and_parentheses(self):
        fields = [b"S"] + [str(i).encode() for i in range(4, 22)] + [b"424242"]
        raw = b"77 (odd ) name (with spaces)) " + b" ".join(fields) + b"\n"
        self.assertEqual(session._starttime_from_stat(raw), "424242")

    def test_transcript_rotation_invalidates_persistence_census(self):
        path = os.path.join(self.tmp, "live.jsonl")
        with open(path, "w") as f:
            f.write("{}\n")
        census = {"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee": path}
        self.assertTrue(session._sid_on_disk(
            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", census))
        os.unlink(path)
        self.assertFalse(session._sid_on_disk(
            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", census))


class VersionedLauncherCensusTest(unittest.TestCase):
    """The launcher stopped being named after itself, and three verbs went dark.

    MEASURED 2026-08-22 on a box running 22 agents: `helm fleet` printed "0 live
    claude process(es)", `helm who` printed "no live claude/codex agent
    processes", `helm session ls` printed "0 live claude panes". Root cause:
    `comm != b"claude"` at two sites here, while the launcher exec's
    `~/.local/share/claude/versions/2.1.238` and the kernel copies THAT basename
    into comm.

    WORSE THAN A FAILED PROBE: `_proc_claude_census()` returned `rows: []` with
    `listing_failed`, `who_failed` and `census_partial` all False — a well-formed
    certification of an empty estate, which is why five review rounds of
    hardening at `seats_claims.py` (every one about an UNREADABLE input never
    becoming a definite answer) passed it straight through.

    NO EXISTING ARM COULD HAVE CAUGHT IT: `ProcSnapshotTest.plant` writes
    `comm = b"claude"`, the shape production stopped emitting. Every arm here
    plants the VERSIONED shape and asserts the fixture really is versioned
    before concluding anything from it."""

    VERSIONED = "/x/.local/share/claude/versions/1.2.3"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        old = session.PROC
        session.PROC = self.tmp
        self.addCleanup(setattr, session, "PROC", old)

    def plant(self, pid, comm, exe=None, argv=None, start="424242"):
        base = os.path.join(self.tmp, str(pid))
        os.makedirs(base)
        with open(os.path.join(base, "comm"), "wb") as f:
            f.write(comm.encode() + b"\n")
        with open(os.path.join(base, "cmdline"), "wb") as f:
            f.write(("\0".join(argv or [self.VERSIONED, "--x"]) + "\0").encode())
        with open(os.path.join(base, "environ"), "wb") as f:
            f.write(("HOME=%s\0" % os.path.expanduser("~")).encode())
        fields = ["S"] + [str(i) for i in range(4, 22)] + [start, "0", "0"]
        with open(os.path.join(base, "stat"), "w") as f:
            f.write("%d (%s) %s\n" % (pid, comm, " ".join(fields)))
        os.symlink(self.tmp, os.path.join(base, "cwd"))
        if exe:
            os.symlink(exe, os.path.join(base, "exe"))
        return base

    def test_a_VERSION_NAMED_agent_is_censused(self):  # noqa: VACUOUS_ASSERTION — the positive pole is the FIRST assertion in the body: pid 51 (legacy comm) must answer True on the same predicate before pid 52 is asked anything, so a predicate that answered False for everything reddens here first. Every assertion in this class was probed in-process against the real gate before commit (legacy True / versioned+exe True / forged False / lookalike False / no-exe None)
        """THE P0. Its positive control is the legacy spelling on the SAME
        predicate: if `claude` stopped answering too, this arm would be
        measuring a broken build rather than the cure."""
        self.plant(51, "claude")                       # the historical shape
        self.assertIs(session._is_agent(51, b"claude"), True,
                      "POSITIVE CONTROL: the legacy comm must still answer")
        self.plant(52, "1.2.3", exe=self.VERSIONED)
        self.assertIs(session._is_agent(52, b"1.2.3"), True)
        self.assertIsNotNone(session._proc_snapshot(52))

    def test_the_ESTATE_stops_certifying_itself_empty(self):
        """The census is the row source for `helm fleet`, `helm session ls` and
        the lease layer's snapshot. An empty one is not a shrug — every
        completeness flag reads False, so consumers treat it as proven.

        MUTATION-PROVEN 2026-08-22: with the predicate reverted to comm-only,
        this arm fails with rows=[] and all three flags still False — i.e. the
        arm's failure REPRODUCES the outage rather than merely differing from
        it."""
        self.plant(53, "1.2.3", exe=self.VERSIONED)
        c = session._proc_claude_census()
        self.assertEqual([r["pid"] for r in c["rows"]], [53])
        self.assertFalse(c["listing_failed"] or c["census_partial"]
                         or c["who_failed"],
                         "and the flags must still mean what they say")

    def test_a_LOOKALIKE_versions_directory_is_not_an_AGENT_here(self):  # noqa: VACUOUS_ASSERTION — the absence (`_proc_snapshot(57) is None`) is paired with `_proc_snapshot(56) is not None` on the SAME function one line above, differing only in the exe link, so an always-None gate fails the control before it reaches the refusal
        """`/tmp/claude/versions/fake` is two mkdirs. Asserted THROUGH this
        module's own gate rather than against the predicate's internals, because
        what matters at this seam is whether the CENSUS admits it."""
        self.plant(56, "1.2.3", exe=self.VERSIONED)
        self.assertIsNotNone(session._proc_snapshot(56),
                             "POSITIVE CONTROL: the production layout MUST be "
                             "admitted, or the refusal below is a broken gate")
        self.plant(57, "fake", exe="/tmp/claude/versions/fake")
        self.assertIs(session._is_agent(57, b"fake"), False)
        self.assertIsNone(session._proc_snapshot(57))

    def test_a_FORGED_argv_does_not_survive_the_kernel(self):  # noqa: VACUOUS_ASSERTION — same shape: `_proc_snapshot(55) is not None` is the unconditional control on the same function, and pid 54 differs from it ONLY in where exe points
        """argv0 is writable BY THE PROCESS; /proc/<pid>/exe is not. The cure
        reads the one the process cannot write."""
        self.plant(55, "1.2.3", exe=self.VERSIONED)
        self.assertIsNotNone(session._proc_snapshot(55),
                             "POSITIVE CONTROL on the SAME observable: an "
                             "IDENTICAL fixture differing only in the exe link "
                             "must be censused, or the refusal below proves "
                             "nothing about the exe check")
        self.plant(54, "1.2.3", exe="/usr/bin/vim")    # same argv, forged
        self.assertIs(session._is_agent(54, b"1.2.3"), False)
        self.assertIsNone(session._proc_snapshot(54))

    def test_an_UNREADABLE_exe_is_PARTIAL_never_ABSENT(self):
        """THE THIRD STATE, and the reason this cure delegates instead of
        keeping its own predicate. Reading /proc/<pid>/exe needs
        PTRACE_MODE_READ where comm does not — measured on this box, 450 of 870
        readable-comm pids refuse it. A pid whose comm LOOKS versioned and whose
        exe we were not permitted to read is UNDECIDABLE, and undecidable must
        raise `census_partial` (the row count becomes a FLOOR) rather than
        certify the pid absent. My first draft collapsed it to False."""
        self.plant(58, "1.2.3")                        # comm versioned, NO exe
        self.assertIsNone(session._is_agent(58, b"1.2.3"),
                          "an unreadable exe on a version-shaped comm cannot "
                          "be decided either way")
        c = session._proc_claude_census()
        self.assertTrue(c["census_partial"],
                        "and the census must say its count is a FLOOR")
        self.plant(59, "1.2.3", exe=self.VERSIONED)
        self.assertIs(session._is_agent(59, b"1.2.3"), True,
                      "POSITIVE CONTROL: the same shape WITH a readable exe "
                      "decides cleanly, so the None above is the permission "
                      "case and not a broken predicate")


    def test_the_LS_SURFACE_says_FLOOR_when_it_could_not_classify_everything(self):  # noqa: VACUOUS_ASSERTION — the two assertions ARE the pair on ONE observable: the same headline string is asserted to LACK "FLOOR" while everything classifies and to CONTAIN it once one pid becomes undecidable, so neither an always-floor nor a never-floor build can pass both
        """THE OWNER-FACING HALF, and the outage was reported through this exact
        line. `_proc_claude_rows` returns a bare list, so an undecidable pid is
        simply not in it and the headline counts what remains AND CALLS THAT THE
        ESTATE. Routing through the census carries `census_partial`, which is
        the flag that makes a count a FLOOR.

        THE PAIR IS THE POINT: both halves render the same surface and differ
        only in whether one pid's exe is readable."""
        self.plant(61, "1.2.3", exe=self.VERSIONED)
        self.assertNotIn("FLOOR", self._ls(),
                         "POSITIVE CONTROL: a fully classified scan must NOT "
                         "cry floor, or the marker means nothing")
        self.plant(62, "1.2.3")                    # versioned comm, exe absent
        out = self._ls()
        self.assertIn("1 live claude panes", out, "the decidable pane is counted")
        self.assertIn("FLOOR", out,
                      "and the count must announce itself as a floor")

    def _ls(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            session._cmd_ls(type("A", (), {"json": False})())
        return buf.getvalue().splitlines()[0]


class ProcSnapshotTest(unittest.TestCase):
    SID = "11111111-1111-1111-1111-111111111111"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.old_proc = session.PROC
        session.PROC = self.tmp
        self.addCleanup(setattr, session, "PROC", self.old_proc)

    def plant(self, pid=41, argv=None, env=None, start="424242"):
        argv = argv or ["/adapter/launch-wrapper", "--resume", self.SID]
        base = os.path.join(self.tmp, str(pid))
        os.makedirs(base)
        with open(os.path.join(base, "comm"), "wb") as f:
            f.write(b"claude\n")
        with open(os.path.join(base, "cmdline"), "wb") as f:
            f.write(("\0".join(argv) + "\0").encode())
        with open(os.path.join(base, "environ"), "wb") as f:
            f.write(env if env is not None else
                    ("HOME=%s\0" % os.path.expanduser("~")).encode())
        fields = ["S"] + [str(i) for i in range(4, 22)] + [start, "0", "0"]
        with open(os.path.join(base, "stat"), "w") as f:
            f.write("%d (claude worker) %s\n" % (pid, " ".join(fields)))
        os.symlink(self.tmp, os.path.join(base, "cwd"))
        return base

    def test_comm_identity_accepts_adapter_argv_and_parses_resume(self):
        self.plant()
        snap = session._proc_snapshot(41)
        self.assertIsNotNone(snap)
        self.assertEqual(session._resume_sid(snap["argv"]), self.SID)

    def test_unreadable_environ_keeps_visible_snapshot_without_home_guess(self):
        self.plant()
        real = session._proc_bytes

        def read(pid, name):
            if name == "environ":
                raise PermissionError("fixture")
            return real(pid, name)

        with mock.patch.object(session, "_proc_bytes", side_effect=read):
            snap = session._proc_snapshot(41)
        self.assertIsNotNone(snap)
        self.assertIsNone(snap["env"])

    def test_exec_or_pid_reuse_during_snapshot_drops_process(self):
        self.plant()
        with mock.patch.object(session, "_proc_matches", return_value=False):
            self.assertIsNone(session._proc_snapshot(41))

    def test_snapshot_captures_stdin_redirection(self):
        # fd/0 is the print-mode evidence _proc_claude_rows composes into
        # `headless`; unreadable fd stays None (proves nothing)
        base = self.plant()
        self.assertIsNone(session._proc_snapshot(41)["stdin"])
        os.makedirs(os.path.join(base, "fd"))
        os.symlink("pipe:[777]", os.path.join(base, "fd", "0"))
        self.assertEqual(session._proc_snapshot(41)["stdin"], "pipe:[777]")

    def test_environment_change_breaks_process_bracket(self):
        self.plant()
        snap = session._proc_snapshot(41)
        with open(os.path.join(self.tmp, "41", "environ"), "wb") as f:
            f.write(b"HOME=/different\0")
        self.assertFalse(session._proc_matches(
            41, snap["start"], snap["cmdline"], snap["environ"], snap["cwd"]))


class ProcCensusTest(unittest.TestCase):
    OLD = "11111111-1111-1111-1111-111111111111"
    NEW = "22222222-2222-2222-2222-222222222222"

    def snap(self, pid=41, argv=None, cwd="/work"):
        argv = argv or ["wrapper", "--resume", self.OLD]
        raw = ("\0".join(argv) + "\0").encode()
        environ = ("HOME=%s\0" % os.path.expanduser("~")).encode()
        return {"pid": pid, "uid": os.geteuid(), "start": str(pid * 10),
                "cmdline": raw, "environ": environ, "argv": argv + [""],
                "env": {"HOME": os.path.expanduser("~")}, "cwd": cwd}

    def rows(self, snapshots, records, who_rows=None, candidates=None,
             matches=True):
        by_pid = {s["pid"]: s for s in snapshots}
        with mock.patch.object(session.os, "listdir",
                               return_value=[str(p) for p in by_pid]), \
             mock.patch.object(session, "_proc_snapshot",
                               side_effect=lambda p: by_pid[p]), \
             mock.patch.object(session, "_session_record",
                               side_effect=lambda p, *_: records[p]), \
             mock.patch.object(session, "_proc_matches", return_value=matches), \
             mock.patch.object(who, "scan", return_value=who_rows or []), \
             mock.patch.object(session, "_cwd_session_ids",
                               return_value=candidates or []):
            return session._proc_claude_rows()

    def test_declared_outranks_resume_and_exposes_independent_double_open(self):
        snaps = [self.snap(41), self.snap(42)]
        records = {41: (self.NEW, "record-ok", "/cfg"),
                   42: (self.NEW, "record-ok", "/cfg")}
        rows = self.rows(snaps, records)
        self.assertEqual([r["session"] for r in rows], [self.NEW, self.NEW])
        self.assertEqual(session.live_sids(rows)[self.NEW], [41, 42])

    def test_bad_record_falls_through_without_skipping_resume_or_who(self):
        record = {41: (None, "record-stale", "/cfg")}
        row = self.rows([self.snap()], record)[0]
        self.assertEqual(row["session"], self.OLD)
        self.assertEqual(row["identity"], "resume")

        bare = self.snap(argv=["claude", "--resume", "--model", "opus"])
        wr = [{"pid": 41, "provider": "anthropic", "child": False,
               "session": self.NEW, "session_candidates": []}]
        row = self.rows([bare], record, who_rows=wr)[0]
        self.assertEqual(row["session"], self.NEW)
        self.assertEqual(row["identity"], "who")

    def test_conflicting_resume_values_are_not_identity(self):
        snap = self.snap(argv=["claude", "--resume", self.OLD,
                               "--resume=" + self.NEW])
        row = self.rows([snap], {41: (None, "record-missing", "/cfg")})[0]
        self.assertIsNone(row["session"])
        self.assertEqual(row["identity"], "unknown")

    def test_stale_who_candidate_stays_possible_without_false_sid(self):
        snap = self.snap(argv=["claude", "--continue"])
        wr = [{"pid": 41, "provider": "anthropic", "child": False,
               "session": None, "session_candidates": [self.NEW]}]
        row = self.rows([snap], {41: (None, "record-missing", "/cfg")},
                        who_rows=wr, candidates=[self.NEW])[0]
        self.assertIsNone(row["session"])
        self.assertEqual(row["identity"], "unknown")
        self.assertEqual(row["possible_sessions"], [self.NEW])
        self.assertEqual(session.live_sids([row]), {})
        self.assertEqual(session.open_pids(self.NEW, [row]), [41])

    def test_failed_rungs_still_collect_deterministic_cwd_candidates(self):
        snap = self.snap(argv=["claude", "--continue"])
        candidates = [self.NEW, self.OLD]
        row = self.rows([snap], {41: (None, "record-corrupt", "/cfg")},
                        candidates=candidates)[0]
        self.assertEqual(row["possible_sessions"], candidates)

    def test_invalid_config_env_never_falls_back_to_callers_default_home(self):
        snap = self.snap(argv=["claude", "--continue"])
        snap["env"] = {"CLAUDE_CONFIG_DIR": None,
                       "HOME": os.path.expanduser("~")}
        row = self.rows([snap], {41: (self.NEW, "record-ok", "/cfg")})[0]
        self.assertIsNone(row["session"])
        self.assertEqual(row["declared_reason"], "config-untrusted")

    def test_final_process_identity_recheck_drops_raced_row(self):
        rows = self.rows([self.snap()],
                         {41: (self.NEW, "record-ok", "/cfg")}, matches=False)
        self.assertEqual(rows, [])

    def test_a_piped_spawn_without_p_is_still_a_sessionless_oneshot(self):
        # the CLI enters print mode by itself when stdin is not a terminal,
        # so the census composes the argv scan with the proven stdin
        # redirection: the legit piped one-shot that omitted -p keeps its
        # certification green even after headless joined the sessionless
        # conjunct
        snap = self.snap(argv=["claude", "--no-session-persistence"])
        snap["stdin"] = "pipe:[4242]"
        row = self.rows([snap], {41: (None, "record-missing", "/cfg")})[0]
        self.assertTrue(row["headless"])
        self.assertTrue(row["nonpersistent"])
        self.assertTrue(session._sessionless_oneshot(row))

    def test_a_tty_pane_with_the_inert_flag_stays_a_pane(self):
        # the other direction of the same conjunct: on a live terminal the
        # flag is inert, print mode never engages, and the row must stay in
        # the census as an unresolved pane rather than certify sessionless
        snap = self.snap(argv=["claude", "--no-session-persistence"])
        snap["stdin"] = "/dev/pts/7"
        row = self.rows([snap], {41: (None, "record-missing", "/cfg")})[0]
        self.assertFalse(row["headless"])
        self.assertTrue(row["nonpersistent"])
        self.assertFalse(session._sessionless_oneshot(row))


class HeadlessCensusTest(unittest.TestCase):
    """A SESSIONLESS one-shot is not an agent pane, and the census must not
    count it as one — but `headless` is only an INTERACTION MODE, never proof
    of sessionlessness: print mode persists unless --no-session-persistence is
    requested, and `claude -p --resume <sid>` is a proven live holder. What
    leaves the health arithmetic is decided by _sessionless_oneshot (mode AND
    no explicit session identity), never by -p alone."""

    def test_the_live_remember_plugin_invocation_is_sessionless(self):
        # verbatim argv measured 2026-07-22 — the call that flipped a CERTIFIED
        # estate to a memory-only FAIL twenty minutes later. It is excluded on
        # its EXPLICIT nonpersistence evidence, not on -p alone.
        argv = ["/home/tester/.local/bin/claude", "-p", "--output-format", "json",
                "--no-session-persistence", "--exclude-dynamic-system-prompt"]
        self.assertTrue(session._is_headless(argv))
        self.assertTrue(session._is_nonpersistent(argv))
        self.assertTrue(session._sessionless_oneshot(
            {"headless": True, "nonpersistent": True, "identity": "unknown",
             "declared": None, "resume": None}))

    def test_headless_and_nonpersistent_are_separate_axes(self):
        # --no-session-persistence states sessionless intent outright; -p is
        # merely an interaction mode — conflating them is the defect both
        # reviewers converged on
        self.assertFalse(session._is_headless(["claude", "--no-session-persistence"]))
        self.assertTrue(session._is_nonpersistent(["claude", "--no-session-persistence"]))
        self.assertTrue(session._is_headless(["claude", "-p", "hi"]))
        self.assertFalse(session._is_nonpersistent(["claude", "-p", "hi"]))

    def test_the_inert_flag_outside_print_mode_never_certifies(self):
        # fable review MED: the CLI documents --no-session-persistence
        # '(only works with --print)' — in an interactive pane the flag is
        # INERT and a real session persists, so the flag alone must never
        # green the row. Sessionless is proven only by flag AND print mode.
        self.assertFalse(session._sessionless_oneshot(
            {"headless": False, "nonpersistent": True, "identity": "unknown",
             "declared": None, "resume": None}))

    def test_stdin_redirection_is_print_mode_evidence(self):
        # measured 2026-07-22: `claude </dev/null` with no flags errors 'when
        # using --print' — the CLI enters print mode on its own when stdin is
        # not a terminal, so a piped spawn that omitted -p is still headless.
        # An absent/unreadable target proves nothing and fails interactive —
        # the direction that refuses certification, never the one that greens.
        for target in ("pipe:[4242]", "socket:[9]", "/dev/null", "/tmp/in"):
            self.assertTrue(session._stdin_redirected(target), target)
        # /dev/ptmx is the pty MASTER — isatty-true, a live terminal, never
        # redirection proof (fable delta-adversarial LOW)
        for target in ("/dev/pts/4", "/dev/tty2", "/dev/console",
                       "/dev/ptmx", "", None):
            self.assertFalse(session._stdin_redirected(target), target)

    def test_a_flag_in_a_value_position_declares_nothing(self):
        # fable review LOW: commander hands a required option-argument the
        # next token even when flag-shaped, so in `--append-system-prompt
        # --no-session-persistence` the flag is PROMPT TEXT. Reading it as
        # declared nonpersistence would green a session the CLI persists —
        # the same ambiguity _resume_sid poisons, held to the same
        # fail-closed law here.
        self.assertFalse(session._is_nonpersistent(
            ["claude", "-p", "--append-system-prompt",
             "--no-session-persistence"]))
        self.assertFalse(session._is_headless(
            ["claude", "--append-system-prompt", "--print"]))
        # with the option's value filled, the following flag is real again
        self.assertTrue(session._is_nonpersistent(
            ["claude", "-p", "--append-system-prompt", "be brief",
             "--no-session-persistence"]))
        # a MEASURED boolean cannot consume its neighbor, and the flag right
        # after it stays declared (the remember-plugin shape must keep green)
        self.assertTrue(session._is_nonpersistent(
            ["claude", "-p", "--no-session-persistence"]))
        self.assertTrue(session._is_headless(["claude", "-c", "--print"]))

    def test_an_interactive_pane_is_not_headless(self):
        for argv in (["claude", "--resume", "abc", "--dangerously-skip-permissions"],
                     ["claude", "--continue"],
                     ["claude", "--model", "opus"]):
            self.assertFalse(session._is_headless(argv), argv)

    def test_a_resumed_pane_whose_prompt_mentions_p_is_not_headless(self):
        # the boot prompt is a positional ARG, never a flag — matching on
        # substrings rather than exact tokens would misread a seat as headless
        # and silently drop a real pane out of the census
        self.assertFalse(session._is_headless(
            ["claude", "--resume", "abc", "You are seat 'codex-3'; use -p sparingly"]))

    SID_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    SID_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

    def test_resume_sid_honors_the_option_terminator(self):
        # codex round-2 HIGH 1, exact probe: a resume token entirely after
        # `--` is positional prompt prose under the same option-region law as
        # _argv_flag — it must never mint a proven session identity and enter
        # live-holder/DOUBLE-OPEN arithmetic
        argv = ["claude", "-p", "--", "--resume", self.SID_A]
        self.assertTrue(session._is_headless(argv))
        self.assertIsNone(session._resume_sid(argv))

    def test_post_terminator_prose_never_drops_the_real_resume(self):
        # the converse defect: a real pre-terminator resume plus a different
        # post-terminator token must not read as two conflicting IDs that
        # erase the real holder from the census
        self.assertEqual(
            session._resume_sid(["claude", "--resume", self.SID_A, "--",
                                 "--resume", self.SID_B]),
            self.SID_A)

    def test_a_resume_meeting_the_terminator_stays_unknown(self):
        # `--resume` refuses the terminator as its value (optional-value law)
        # and stays a BARE resume; the post-terminator UUID is prose, not its
        # value
        self.assertIsNone(session._resume_sid(
            ["claude", "--resume", "--", self.SID_A]))

    def test_an_invalid_resume_occurrence_poisons_a_valid_one(self):
        # Round-3 MED: the fleet lane's fail-closed law, kept intact
        # while adding the terminator — a valid occurrence beside an invalid
        # one is contradictory evidence, not a majority vote
        for argv in (["claude", "--resume", self.SID_A, "--resume"],
                     ["claude", "--resume=" + self.SID_A, "--resume=bad"],
                     ["claude", "--resume", "--print",
                      "--resume", self.SID_A]):
            self.assertIsNone(session._resume_sid(argv), argv)
        # a repeat of the SAME valid value is not a conflict
        self.assertEqual(
            session._resume_sid(["claude", "--resume", self.SID_A,
                                 "--resume=" + self.SID_A]),
            self.SID_A)
        # and post-terminator tokens can neither poison nor conflict
        self.assertEqual(
            session._resume_sid(["claude", "--resume", self.SID_A, "--",
                                 "--resume", "--resume=bad"]),
            self.SID_A)

    def test_short_resume_alias_attributes_the_holder(self):
        # codex advisory HIGH: the real CLI resumes via `-r <sid>`, attached
        # `-r<sid>`, and cluster `-pr <sid>` (commander splits boolean shorts
        # off before a value-taking one) — all measured against the binary.
        # A holder spawned through the short alias must not silently vanish.
        self.assertEqual(
            session._resume_sid(["claude", "-r", self.SID_A]), self.SID_A)
        self.assertEqual(
            session._resume_sid(["claude", "-r" + self.SID_A]), self.SID_A)
        self.assertEqual(
            session._resume_sid(["claude", "-pr", self.SID_A]), self.SID_A)
        # `-pr` is print mode too, and `-cp` splits into --continue --print
        self.assertTrue(session._is_headless(["claude", "-pr", self.SID_A]))
        self.assertTrue(session._is_headless(["claude", "-cp"]))
        # `-c` alone continues; it is neither print mode nor a resume
        self.assertFalse(session._is_headless(["claude", "-c"]))
        self.assertIsNone(session._resume_sid(["claude", "-c"]))

    def test_short_resume_alias_is_fail_closed(self):
        # the lane's poison law holds for the alias exactly as for the long
        # form: bare/trailing, invalid value, and mixed valid+invalid all
        # yield UNKNOWN. `-r=X` carries the LITERAL value `=X` (the CLI does
        # not strip `=` on short flags — measured: rejected as not a UUID),
        # and `-rp` resumes by TITLE "p", unresolvable from argv.
        for argv in (["claude", "-r"],
                     ["claude", "-r", self.SID_A, "-r"],
                     ["claude", "-r=" + self.SID_A],
                     ["claude", "-rp"],
                     ["claude", "-r", self.SID_A, "--resume=bad"],
                     ["claude", "-r", self.SID_A, "-r", self.SID_B]):
            self.assertIsNone(session._resume_sid(argv), argv)
        # same value through both spellings is agreement, not conflict
        self.assertEqual(
            session._resume_sid(["claude", "-r", self.SID_A,
                                 "--resume", self.SID_A]),
            self.SID_A)

    def test_value_taking_short_clusters_stay_opaque(self):
        # `-d [filter]` absorbs its cluster remainder as its attached value,
        # so `-dr` is a debug filter "r" — it must neither mint a resume nor
        # poison a real one, and `-dp` is a filter "p", never print mode
        self.assertIsNone(session._resume_sid(["claude", "-dr"]))
        self.assertEqual(
            session._resume_sid(["claude", "-dr", "--resume", self.SID_A]),
            self.SID_A)
        self.assertFalse(session._is_headless(["claude", "-dp"]))

    def test_optional_value_options_skip_a_flag_shaped_neighbor(self):
        # r7's regression, live-probed on 2.1.218: commander is greedy ONLY
        # for REQUIRED-value options. `claude -d --resume <uuid> -p
        # --no-session-persistence hi` REALLY resumes the uuid (`claude -d
        # --resume BAD -p hi` errors 'not a UUID'), yet the one-consumption
        # presumption read the resume as -d's debug filter — returning
        # resume=None, certifying sessionless green, and dropping the holder
        # from DOUBLE-OPEN arithmetic. Base 690b669 found this holder.
        argv = ["claude", "-d", "--resume", self.SID_A, "-p",
                "--no-session-persistence", "hi"]
        self.assertEqual(session._resume_sid(argv), self.SID_A)
        self.assertTrue(session._is_headless(argv))
        self.assertTrue(session._is_nonpersistent(argv))
        self.assertFalse(session._sessionless_oneshot(
            {"headless": True, "nonpersistent": True, "identity": "resume",
             "declared": None, "resume": self.SID_A}))
        # the greedy control: `--model --resume <uuid>` still mints nothing —
        # required-value options keep consuming their flag-shaped neighbor
        self.assertIsNone(session._resume_sid(
            ["claude", "--model", "--resume", self.SID_A]))
        # every MEASURED optional-value long flag skips a flag-shaped
        # neighbor (`claude <flag> --version` prints the version, 2.1.218)
        for flag in ("--debug", "--from-pr", "--prompt-suggestions",
                     "--remote-control", "--worktree"):
            self.assertEqual(
                session._resume_sid(["claude", flag, "--resume",
                                     self.SID_A]),
                self.SID_A, flag)
            self.assertTrue(
                session._is_headless(["claude", flag, "-p", "hi"]), flag)
        # ... but consumes a NON-dash neighbor as its value (measured:
        # `claude -p -d hi` errors 'Input must be provided' — `hi` became
        # the filter), so that value is opaque prose, never a flag
        self.assertTrue(session._is_headless(["claude", "-d", "api", "-p"]))
        self.assertEqual(
            session._resume_sid(["claude", "-w", "feature", "--resume",
                                 self.SID_A]),
            self.SID_A)
        # bare short aliases carry the same optional-value law
        self.assertTrue(session._is_headless(["claude", "-d", "-p", "hi"]))
        self.assertTrue(session._is_headless(["claude", "-w", "-p", "hi"]))

    def test_optional_value_options_leave_the_terminator_alone(self):
        # measured: `claude -d -- --version` runs `--version` as prompt
        # prose (the required-greedy `--model --` swallows the terminator
        # whole) — so past it every token is positional, and a resume whose
        # would-be value is the terminator stays bare, poisoning the parse
        self.assertFalse(session._is_headless(["claude", "-d", "--", "-p"]))
        self.assertIsNone(session._resume_sid(
            ["claude", "-d", "--", "--resume", self.SID_A]))

    def test_a_flag_refused_by_optional_resume_leaves_it_bare(self):
        # `--resume` is itself optional-value: commander refuses a
        # flag-shaped neighbor as its value, so `--resume -p` is a BARE
        # resume (poison) AND the neighbor is a real flag again
        argv = ["claude", "--resume", "-p", "hi"]
        self.assertIsNone(session._resume_sid(argv))
        self.assertTrue(session._is_headless(argv))

    def test_short_aliases_are_prose_past_the_terminator(self):
        # the option-region law is alias-blind: post-terminator `-r`/`-pr`
        # tokens are prompt prose, never identity and never poison
        self.assertIsNone(session._resume_sid(
            ["claude", "-p", "--", "-r", self.SID_A]))
        self.assertEqual(
            session._resume_sid(["claude", "-r", self.SID_A, "--",
                                 "-r", self.SID_B, "-r"]),
            self.SID_A)
        self.assertFalse(session._is_headless(["claude", "--", "-pr"]))

    def test_a_cluster_in_a_value_position_is_opaque(self):
        # fable delta-adversarial HIGH pair: commander hands `--model` the
        # next token RAW, so in `--model -cp` the cluster is the MODEL NAME
        # (measured: `claude --model -cr` errors about --print input — no
        # continue, no resume parsed). Expanding it defeated the
        # value-position guard with fragments of the very token commander
        # swallowed whole, resurrecting the inert-flag green — and `--model
        # -cr <uuid>` minted a resume the CLI never granted, the module's own
        # named worst direction.
        argv = ["claude", "--model", "-cp", "--no-session-persistence"]
        self.assertFalse(session._is_headless(argv))
        # the flag AFTER the consumed value is real again — and inert outside
        # print mode, so a tty row still never certifies green
        self.assertTrue(session._is_nonpersistent(argv))
        self.assertFalse(session._sessionless_oneshot(
            {"headless": False, "nonpersistent": True, "identity": "unknown",
             "declared": None, "resume": None}))
        self.assertIsNone(session._resume_sid(
            ["claude", "--model", "-cr", self.SID_A]))
        # both cluster orders are opaque — safety is not ordering-dependent
        self.assertFalse(session._is_headless(["claude", "--model", "-pc"]))

    def test_prose_in_a_value_slot_never_poisons_a_real_holder(self):
        # the legit-case regression (fable delta-adversarial MED): prompt
        # prose that merely LOOKS like a short cluster must stay opaque —
        # expanding it poisoned the parse and demoted a proven holder
        self.assertEqual(
            session._resume_sid(["claude", "--append-system-prompt",
                                 "-pr be brief", "--resume", self.SID_A]),
            self.SID_A)

    def test_a_long_resume_in_a_value_position_mints_nothing(self):
        # measured: `claude --model --resume --version` prints the version —
        # --model swallowed `--resume` whole and the CLI parsed no resume.
        # _argv_flag and _resume_sid walk the SAME (token, consumed) pairs,
        # so the docstring symmetry claim is true by construction.
        self.assertIsNone(session._resume_sid(
            ["claude", "--model", "--resume", self.SID_A]))
        # with the value slot filled, the resume is real again
        self.assertEqual(
            session._resume_sid(["claude", "--model", "opus",
                                 "--resume", self.SID_A]),
            self.SID_A)

    def test_measured_booleans_cannot_orphan_their_neighbor(self):
        # the regression the first value-position guard shipped (fable
        # delta-composition LOW): with only three measured booleans,
        # `claude --dangerously-skip-permissions -p` — the fleet's single
        # most common spawn shape — fell to UNKNOWN on a tty. Every flag here
        # is MEASURED boolean against the real CLI (2.1.218, 2026-07-22):
        # `claude <flag> --version` prints the version iff the flag cannot
        # consume its neighbor.
        for flag in ("--dangerously-skip-permissions", "--verbose",
                     "--fork-session", "--strict-mcp-config", "--ide",
                     "--safe-mode", "--bare", "--chrome",
                     "--allow-dangerously-skip-permissions"):
            self.assertTrue(
                session._is_headless(["claude", flag, "-p", "hi"]), flag)
        # and the fleet's live pane shape keeps its proven holder
        self.assertEqual(
            session._resume_sid(["claude", "--dangerously-skip-permissions",
                                 "--resume", self.SID_A]),
            self.SID_A)

    def test_a_value_slot_consumes_even_the_terminator(self):
        # measured: `claude --model -- --version` prints the version — the
        # terminator itself was swallowed as the model name, so the options
        # after it are real, not prose
        self.assertTrue(
            session._is_headless(["claude", "--model", "--", "-p"]))
        self.assertEqual(
            session._resume_sid(["claude", "--model", "--",
                                 "--resume", self.SID_A]),
            self.SID_A)

    def test_option_terminator_ends_the_flag_scan(self):
        # past the standard `--` terminator every token is positional: a boot
        # prompt exactly equal to -p/--print is prose there, never a flag
        self.assertFalse(session._is_headless(["claude", "--", "-p"]))
        self.assertFalse(session._is_headless(
            ["claude", "--resume", "abc", "--", "--print"]))
        self.assertFalse(session._is_nonpersistent(
            ["claude", "--", "--no-session-persistence"]))
        # a real flag BEFORE the terminator still declares the mode/intent
        self.assertTrue(session._is_headless(["claude", "-p", "--", "prompt"]))
        self.assertTrue(session._is_nonpersistent(
            ["claude", "--no-session-persistence", "--", "prompt"]))

    def test_sessionless_rows_leave_the_memory_only_census(self):
        # pid 1's session hint carries NO canonical identity rung (identity
        # unknown) — an ordinary background print worker echoing a SID, not a
        # pane with work at risk. Exclusion keys the ABSENT identity, never
        # the interaction mode alone.
        rows = [{"pid": 1, "session": "sid-a", "headless": True,
                 "identity": "unknown"},
                {"pid": 2, "session": "sid-b", "headless": False,
                 "identity": "who"}]
        got = session.memory_only_panes(rows=rows, persisting={})
        self.assertEqual([r["pid"] for r in got], [2])

    def test_a_print_row_with_explicit_identity_stays_in_memory_only(self):
        # a proven SID never leaves the count merely because the process will
        # exit after one response
        rows = [{"pid": 1, "session": "sid-a", "declared": "sid-a",
                 "identity": "declared", "headless": True}]
        got = session.memory_only_panes(rows=rows, persisting={})
        self.assertEqual([r["pid"] for r in got], [1])

    def row(self, pid, session_id, headless, **kw):
        base = {"pid": pid, "resume": None, "declared": None,
                "declared_reason": None, "session": session_id,
                "possible_sessions": [], "child": False,
                "ancestor_sid8": "", "force": False, "headless": headless,
                "nonpersistent": False}
        base.update(kw)
        # identity mirrors the census derivation (round-3: synthetic
        # rows must not manufacture shapes _proc_claude_rows never emits — a
        # real row with a session but no declared/resume is who-attributed);
        # tests probing the defensive unknown-hint shape override explicitly
        base.setdefault("identity",
                        "declared" if base["declared"] else
                        "resume" if base["resume"] else
                        "who" if base["session"] else "unknown")
        return base

    def ls(self, rows, persisting, certify=False):
        fn = session.cmd_doctor_panes if certify else session.cmd_ls
        with mock.patch.object(session, "_proc_claude_rows", return_value=rows), \
             mock.patch.object(session, "_persisting_sids",
                               return_value=persisting):
            return run(fn, [])

    def test_a_headless_row_is_still_rendered_not_hidden(self):
        # excluding from the COUNT must never mean hiding from the OPERATOR:
        # a helm seat running headless is a law violation and has to be visible
        rc, out, err = self.ls([self.row(8, None, True, nonpersistent=True),
                                self.row(9, None, True)], {})
        self.assertEqual(rc, 0, err)
        self.assertIn("pid 8", out)
        self.assertIn("headless one-shot", out)
        self.assertIn("pid 9", out)
        self.assertIn("[headless]", out)

    def test_an_explicitly_nonpersistent_row_never_certifies_unknown(self):
        # only --no-session-persistence proves sessionless BY REQUEST — that
        # row has no session by design, is not an unresolved pane, and
        # doctor-panes --certify must not fail the estate over it
        rc, out, err = self.ls([self.row(9, None, True, nonpersistent=True)],
                               {}, certify=True)
        self.assertEqual(rc, 0, err)
        self.assertIn("headless one-shot", out)
        self.assertNotIn("UNKNOWN", out)

    def test_an_interactive_pane_with_the_inert_flag_fails_closed(self):
        # fable review MED, exact probe row: interactive `claude
        # --no-session-persistence` has the flag INERT and a real persisted
        # session — before the fix it certified rc=0 under the false label
        # 'headless one-shot'. It is an UNRESOLVED session, and the render
        # says why the operator's flag did nothing.
        rc, out, err = self.ls([self.row(9, None, False, nonpersistent=True)],
                               {}, certify=True)
        self.assertEqual(rc, 1, err)
        self.assertIn("UNKNOWN", out)
        self.assertIn("inert outside print mode", out)
        self.assertNotIn("headless one-shot", out)

    def test_plain_print_mode_without_identity_fails_closed_as_unknown(self):
        # codex round-2 HIGH 2, exact probe: plain -p persists a transcript by
        # default, so a -p row with no resolvable SID is an UNRESOLVED
        # session, never "no session by design" — certify must refuse rc=0
        rc, out, err = self.ls([self.row(9, None, True)], {}, certify=True)
        self.assertEqual(rc, 1, err)
        self.assertIn("UNKNOWN", out)
        self.assertIn("[headless]", out)
        self.assertNotIn("headless one-shot", out)

    def test_a_headless_row_never_manufactures_a_double_open(self):
        # a worker ECHOING its parent's proven SID with NO canonical identity
        # rung of its own (identity unknown — no --resume, no pid record, no
        # exact who attribution) is not a second holder. Defensive shape:
        # the real census only ever sets `session` from a canonical rung, so
        # this pins the fail-direction should an unproven hint ever leak in.
        rows = [self.row(1, "sid-x", False),
                self.row(2, "sid-x", True, identity="unknown")]
        self.assertEqual(session.live_sids(rows), {"sid-x": [1]})
        rc, out, err = self.ls(rows, {"sid-x": __file__}, certify=True)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("DOUBLE-OPEN", out)
        self.assertIn("persisted", out)
        self.assertIn("[headless]", out)

    def test_a_transcriptless_inherited_worker_alarms_its_holder_not_itself(self):
        # holder-arithmetic exclusion and the render must AGREE: the worker's
        # missing transcript is its HOLDER's risk, so the worker is neither
        # counted nor labeled MEMORY-ONLY — the alarm (and the certify FAIL)
        # lands on the holder row, and the exclusion never becomes a
        # sessionless claim (no "one-shot" label without nonpersistence)
        holder = self.row(1, "sid-x", False)
        worker = self.row(2, "sid-x", True, identity="unknown")
        self.assertEqual(
            [r["pid"] for r in session.memory_only_panes(
                rows=[holder, worker], persisting={})],
            [1])
        rc, out, err = self.ls([holder, worker], {}, certify=True)
        self.assertEqual(rc, 1, err)
        self.assertIn("MEMORY-ONLY", out)
        self.assertIn("inherited-session print worker", out)
        self.assertNotIn("headless one-shot", out)

    def test_an_inherited_hint_with_no_live_holder_fails_closed(self):
        # fable review (composition lens): "risk belongs to its holder" names
        # NOBODY when no holder row exists in the census. The shape is
        # impossible today — _proc_claude_rows only sets `session` from
        # canonical rungs — and an impossible shape must fail closed as
        # UNKNOWN, not certify a transcriptless live session green.
        rows = [self.row(2, "sid-x", True, identity="unknown")]
        self.assertEqual(session.memory_only_panes(rows=rows, persisting={}),
                         [])
        rc, out, err = self.ls(rows, {}, certify=True)
        self.assertEqual(rc, 1, err)
        self.assertIn("UNKNOWN", out)
        self.assertIn("no live holder", out)
        self.assertNotIn("MEMORY-ONLY", out)
        self.assertNotIn("headless one-shot", out)

    def test_who_attribution_is_the_processes_own_identity_not_a_hint(self):
        # Round-3 HIGH, exact probe row: _proc_claude_rows suppresses
        # who for child processes and who dedupes shared sessions, so a
        # surviving identity=='who' row is the process's OWN exact canonical
        # attribution — a proven holder that must stay in live-holder
        # arithmetic. Before the fix this exact row produced live_sids={}.
        r = self.row(2, "sid-x", True)
        self.assertEqual(r["identity"], "who")  # helper mirrors the census
        self.assertEqual(session.live_sids([r]), {"sid-x": [2]})
        self.assertEqual(session.open_pids("sid-x", [r]), [2])

    def test_a_who_attributed_print_row_is_a_real_double_open(self):
        # overlapping an interactive holder of the same session, the
        # who-attributed print row IS the second holder — hiding it violated
        # law 1 and let the certifier pass rc=0 on a live overlap
        rows = [self.row(1, "sid-x", False),
                self.row(2, "sid-x", True)]
        self.assertEqual(session.live_sids(rows), {"sid-x": [1, 2]})
        rc, out, err = self.ls(rows, {"sid-x": __file__}, certify=True)
        self.assertEqual(rc, 1, err)
        self.assertIn("DOUBLE-OPEN", out)

    def test_nonpersistence_never_flips_a_who_attributed_holder_sessionless(self):
        # The second probe arm: adding nonpersistent to the who row made
        # _sessionless_oneshot true — certifying green a process whose exact
        # attribution proves it operates on session X while it lives
        r = self.row(2, "sid-x", True, nonpersistent=True)
        self.assertEqual(r["identity"], "who")
        self.assertFalse(session._sessionless_oneshot(r))
        self.assertFalse(session._inherited_hint_worker(r))
        self.assertEqual(session.live_sids([r]), {"sid-x": [2]})

    def test_a_transcriptless_who_attributed_print_row_is_memory_only(self):
        # its transcript risk is its OWN — a who-attributed print row with no
        # transcript on disk counts as memory-only, never as an inherited
        # worker whose alarm belongs elsewhere
        rows = [self.row(2, "sid-x", True)]
        self.assertEqual(
            [r["pid"] for r in session.memory_only_panes(rows=rows,
                                                         persisting={})],
            [2])

    def test_a_print_mode_resume_is_a_real_holder_and_double_open(self):
        # BOTH reviewers' HIGH: `claude -p --resume X` operates on a proven
        # resumable session; overlapping an interactive holder of X it IS a
        # DOUBLE-OPEN, and excluding it on -p alone hides the violation
        rows = [self.row(1, "sid-x", False),
                self.row(2, "sid-x", True, resume="sid-x")]
        self.assertEqual(session.live_sids(rows), {"sid-x": [1, 2]})
        rc, out, err = self.ls(rows, {"sid-x": __file__}, certify=True)
        self.assertEqual(rc, 1, err)
        self.assertIn("DOUBLE-OPEN", out)

    def test_certifier_and_launch_guard_agree_on_a_print_resume_row(self):
        # codex's probe: before the fix, live_sids={} while open_pids(SID)=[2]
        # — the health certifier and the launch guard disagreed about the same
        # process. One truth now.
        rows = [self.row(2, "sid-x", True, resume="sid-x")]
        self.assertEqual(session.live_sids(rows), {"sid-x": [2]})
        self.assertEqual(session.open_pids("sid-x", rows), [2])

    def test_nonpersistence_never_removes_a_proven_sid_from_holders(self):
        # even `--no-session-persistence --resume X` still READS session X
        # while it lives; a proven SID stays in holder arithmetic, fail closed
        rows = [self.row(2, "sid-x", True, resume="sid-x", nonpersistent=True)]
        self.assertEqual(session.live_sids(rows), {"sid-x": [2]})

    def test_a_print_resume_holder_renders_its_session_not_a_design_claim(self):
        # the old label "no session by design" was FALSE for print mode —
        # print persists unless nonpersistence is requested outright
        rc, out, err = self.ls(
            [self.row(2, "sid-x", True, resume="sid-x")],
            {"sid-x": __file__})
        self.assertEqual(rc, 0, err)
        self.assertIn("persisted", out)
        self.assertIn("[headless]", out)
        self.assertIn("session=sid-x", out)
        self.assertNotIn("headless one-shot", out)
        self.assertNotIn("no session by design", out)

    def test_an_unknown_row_still_never_enters_the_pass_bucket(self):
        # the headless exclusion must not weaken the tri-state guarantee
        rows = [{"pid": 3, "session": None, "headless": False}]
        self.assertEqual(session.memory_only_panes(rows=rows, persisting={}), [])
        rc, out, err = self.ls([self.row(3, None, False)], {}, certify=True)
        self.assertEqual(rc, 1)
        self.assertIn("UNKNOWN", out)


class ProxyScrubOnLaunchAndPasteTest(unittest.TestCase):
    """#107's class in this module: a scrub that covers the child-stamp
    register and not the proxy one.

    THE HAZARD, in the words of the seam that exists for it: every non-claude
    family here runs behind CLIProxyAPI but INSIDE Claude Code's harness, so a
    launch or a pasted resume line inheriting the proxy triple starts a Claude
    session aimed at a proxy fronting another vendor — it looks native, it is
    billed native, and it routes elsewhere.

    SYNTHETIC PLACEHOLDERS ONLY, and every assertion is on the KEY SET rather
    than on a value: these tests are public-bound, and `assertIn(wanted, env)`
    is the shape that passes while the thing under test is wrong."""

    PLACEHOLDER = "synthetic-not-a-real-value"

    def setUp(self):
        from helm import seat
        self.seat = seat
        self.prior = {}
        for v in seat.SCRUB_VARS + seat.CHILD_STAMP_VARS:
            self.prior[v] = os.environ.get(v)
            os.environ[v] = self.PLACEHOLDER
        self.addCleanup(self._restore)

    def _restore(self):
        for v, was in self.prior.items():
            if was is None:
                os.environ.pop(v, None)
            else:
                os.environ[v] = was

    def test_a_launch_from_a_PROXIED_seat_carries_no_proxy_key(self):
        env = session._launch_env()
        self.assertEqual([v for v in self.seat.SCRUB_VARS if v in env], [])
        # CONTROL on the same observable: the stamp register is scrubbed by the
        # same call, so an empty proxy list is coverage and not a dead probe.
        self.assertEqual([v for v in self.seat.CHILD_STAMP_VARS if v in env], [])
        # ...and the function still does its original job.
        self.assertEqual(env[session.FORCE_VAR], "1")

    def test_a_launch_from_a_CLEAN_shell_is_unchanged(self):
        self._restore()
        for v in self.seat.SCRUB_VARS + self.seat.CHILD_STAMP_VARS:
            os.environ.pop(v, None)
        env = session._launch_env()
        self.assertEqual([v for v in self.seat.SCRUB_VARS if v in env], [])
        self.assertEqual(env[session.FORCE_VAR], "1")
        # a clean shell must not lose anything else it was carrying.
        # patch.dict rather than addCleanup(os.environ.pop, ...): the env-hygiene
        # guard reads the module STATICALLY and counts a key as restored when it
        # is del'd, popped by a CALL, named in a tuple, or in a patch.dict — a
        # bare `os.environ.pop` handed to addCleanup is an Attribute, never a
        # Call, so it restores at runtime and reads as a leak. Recognised beats
        # clever when the reader is a parser.
        with mock.patch.dict(os.environ,
                             {"HELM_TEST_UNRELATED": self.PLACEHOLDER}):
            self.assertIn("HELM_TEST_UNRELATED", session._launch_env())

    def test_the_scrub_does_not_depend_on_the_proxy_being_UP(self):
        """A seat whose proxy is DOWN still has the triple in its environment —
        the variables are configuration, not a liveness signal — so the scrub
        must key on the NAMES and never on reachability."""
        env = session._launch_env()
        # positive control FIRST: the env is real and populated, so the empty
        # list below is a scrub and not an empty dict.
        self.assertEqual(env[session.FORCE_VAR], "1")
        self.assertIn("PATH", env)
        self.assertEqual([v for v in self.seat.SCRUB_VARS if v in env], [])

    def test_the_PASTEABLE_resume_line_unsets_both_registers(self):
        """The register this module was short. `_print_incantation` mints a line
        a human pastes into whatever shell they have — which is exactly the
        proxied shell the triple lives in."""
        line = session._print_incantation("s" * 8, cwd="/tmp")
        for v in self.seat.SCRUB_VARS:
            self.assertIn("-u " + v, line, "%s not unset in a pasted resume" % v)
        for v in self.seat.CHILD_STAMP_VARS:
            self.assertIn("-u " + v, line, "%s regressed (control)" % v)
        self.assertIn("claude --resume", line)

    def test_the_prefix_IS_the_shared_seam_not_a_local_copy(self):
        """The class fix, pinned. Three sites built this literal inline and the
        proxy triple was missing from every one; a fourth copy would go the same
        way. If this ever stops being the seam's output, the drift has already
        started."""
        from helm import seat
        shared = seat.paste_unset_prefix()
        # unconditional control: an empty var tuple would make the loop below
        # run zero times and pass, so pin that there is something to check.
        self.assertEqual(len(seat.PASTE_UNSET_VARS), 6)
        self.assertTrue(shared.startswith("env -u "))
        # positive control: two functions that both returned "" would satisfy
        # an equality and prove nothing, so pin what the shared value CONTAINS.
        for v in seat.SCRUB_VARS + seat.CHILD_STAMP_VARS:
            self.assertIn("-u " + v, shared)
        self.assertEqual(session._unset_prefix(), shared)


if __name__ == "__main__":
    unittest.main()
