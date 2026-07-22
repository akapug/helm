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

from helm import session, home, pk, who


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

    def test_resume_launch_uses_attached_cv_with_clean_env(self):
        dirty = {v: "inherited" for v in session._stamp_vars()}
        with mock.patch.dict(os.environ, dirty), \
             mock.patch.object(subprocess, "call", return_value=0) as call:
            rc, err = session._cv_launch("aaaa1111-integrator")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(call.call_args[0][0],
                         ["cv", "resume", "aaaa1111-integrator", "--launch"])
        self.assertEqual(set(call.call_args.kwargs), {"env"})
        env = call.call_args.kwargs["env"]
        for v in session._stamp_vars():
            self.assertNotIn(v, env)
        self.assertEqual(env[session.FORCE_VAR], "1")

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
        cred = os.path.join(self.tmp, "cto mv")
        os.makedirs(cred)
        cwd = "/work/project with spaces"
        with open(os.path.join(cred, ".claude.json"), "w") as f:
            json.dump({"projects": {cwd: {"hasTrustDialogAccepted": True}}}, f)
        home_rows = [{"name": "cto-example", "path": cred, "aliases": [],
                      "projects_link_ok": True}]
        import helm.homes as homes_mod
        with self._sid("aaaa1111-integrator"), \
             mock.patch.object(session, "_session_cwd", return_value=cwd), \
             mock.patch.object(homes_mod, "homes_list", return_value=home_rows):
            # live copy -> refuse (no incantation line printed)
            with mock.patch.object(session, "open_pids", return_value=[57699]):
                rc, out, _ = run(session.cmd_port,
                                 ["--cred", "cto-example", "aaaa1111"])
            self.assertEqual(rc, 1)
            self.assertIn("LAW 1", out)
            self.assertNotIn("claude --resume", out)
            # clear -> print with quoted credhome/cwd + FORCE
            with mock.patch.object(session, "open_pids", return_value=[]):
                rc, out, _ = run(session.cmd_port,
                                 ["--cred", "cto-example", "aaaa1111"])
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
             mock.patch.object(session, "_grep_expert", return_value=""), \
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
        with mock.patch.object(session, "_grep_expert", return_value="  …the order is X…"), \
             mock.patch.object(session, "open_pids", return_value=[]), \
             mock.patch.object(session, "_session_cwd", return_value="/work"):
            rc, out, _ = run(session.cmd_ask, ["helm-orchestration", "order"])
        self.assertIn("expert-transcript hits", out)
        self.assertNotIn("context pack", out)
        # expert's transcript misses -> falls through to the context pack
        with mock.patch.object(session, "_grep_expert", return_value=""), \
             mock.patch.object(session, "open_pids", return_value=[]), \
             mock.patch.object(session, "_session_cwd", return_value="/work"), \
             self.cv_ok("pack-hits") as cv:
            rc, out, _ = run(session.cmd_ask, ["helm-orchestration", "order"])
        self.assertIn("context pack", out)
        self.assertIn("pack-hits", out)
        self.assertEqual(cv.call_args[0],
                         ("pack", "--limit", "3", "helm orchestration order"))


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


class HeadlessCensusTest(unittest.TestCase):
    """A headless one-shot is not an agent pane, and the census must not count
    it as one. Both populations share the symptom (no transcript on disk); only
    argv carries the DECLARED intent that tells them apart."""

    def test_the_live_remember_plugin_invocation_is_headless(self):
        # verbatim argv measured 2026-07-22 — the call that flipped a CERTIFIED
        # estate to a memory-only FAIL twenty minutes later
        argv = ["/home/owner/.local/bin/claude", "-p", "--output-format", "json",
                "--no-session-persistence", "--exclude-dynamic-system-prompt"]
        self.assertTrue(session._is_headless(argv))

    def test_no_session_persistence_alone_is_enough(self):
        # it states the intent outright, with or without -p
        self.assertTrue(session._is_headless(["claude", "--no-session-persistence"]))

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

    def test_option_terminator_ends_the_flag_scan(self):
        # past the standard `--` terminator every token is positional: a boot
        # prompt exactly equal to -p/--print is prose there, never a flag
        self.assertFalse(session._is_headless(["claude", "--", "-p"]))
        self.assertFalse(session._is_headless(
            ["claude", "--resume", "abc", "--", "--print"]))
        # a real flag BEFORE the terminator still declares headless intent
        self.assertTrue(session._is_headless(["claude", "-p", "--", "prompt"]))

    def test_headless_rows_leave_the_memory_only_census(self):
        rows = [{"pid": 1, "session": "sid-a", "headless": True},
                {"pid": 2, "session": "sid-b", "headless": False}]
        got = session.memory_only_panes(rows=rows, persisting={})
        self.assertEqual([r["pid"] for r in got], [2])

    def row(self, pid, session_id, headless):
        return {"pid": pid, "resume": None, "declared": None,
                "declared_reason": None, "session": session_id,
                "possible_sessions": [], "child": False,
                "ancestor_sid8": "", "force": False, "headless": headless}

    def ls(self, rows, persisting, certify=False):
        fn = session.cmd_doctor_panes if certify else session.cmd_ls
        with mock.patch.object(session, "_proc_claude_rows", return_value=rows), \
             mock.patch.object(session, "_persisting_sids",
                               return_value=persisting):
            return run(fn, [])

    def test_a_headless_row_is_still_rendered_not_hidden(self):
        # excluding from the COUNT must never mean hiding from the OPERATOR:
        # a helm seat running headless is a law violation and has to be visible
        rc, out, err = self.ls([self.row(9, None, True)], {})
        self.assertEqual(rc, 0, err)
        self.assertIn("pid 9", out)
        self.assertIn("headless one-shot", out)

    def test_a_sessionless_headless_row_never_certifies_unknown(self):
        # a headless row has no session BY DESIGN — it is not an unresolved
        # pane, and doctor-panes --certify must not fail the estate over it
        rc, out, err = self.ls([self.row(9, None, True)], {}, certify=True)
        self.assertEqual(rc, 0, err)
        self.assertIn("headless one-shot", out)
        self.assertNotIn("UNKNOWN", out)

    def test_a_headless_row_never_manufactures_a_double_open(self):
        # a one-shot carrying its parent's proven SID is not a second holder
        rows = [self.row(1, "sid-x", False), self.row(2, "sid-x", True)]
        self.assertEqual(session.live_sids(rows), {"sid-x": [1]})
        rc, out, err = self.ls(rows, {"sid-x": __file__}, certify=True)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("DOUBLE-OPEN", out)
        self.assertIn("persisted", out)
        self.assertIn("headless one-shot", out)

    def test_an_unknown_row_still_never_enters_the_pass_bucket(self):
        # the headless exclusion must not weaken the tri-state guarantee
        rows = [{"pid": 3, "session": None, "headless": False}]
        self.assertEqual(session.memory_only_panes(rows=rows, persisting={}), [])
        rc, out, err = self.ls([self.row(3, None, False)], {}, certify=True)
        self.assertEqual(rc, 1)
        self.assertIn("UNKNOWN", out)


if __name__ == "__main__":
    unittest.main()
