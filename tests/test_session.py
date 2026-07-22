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

from helm import session, home, pk


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
            {"pid": 57699, "resume": "aaaa1111-integrator", "child": False,
             "sid8": "", "force": False},
            {"pid": 622078, "resume": "deadbeef-memory-only", "child": True,
             "sid8": "85935aed", "force": False},
            {"pid": 998382, "resume": "cccc2222-rescued", "child": True,
             "sid8": "85935aed", "force": True},
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


class ScanTest(SessionBase):
    def test_live_sids_from_resume_and_stamp(self):
        sids = session.live_sids()
        # argv --resume resolves the full sid...
        self.assertIn("deadbeef-memory-only", sids)
        self.assertEqual(sids["deadbeef-memory-only"], [622078])
        # ...and the child-stamp's inherited SID names the ancestor's open copy
        self.assertIn("85935aed", sids)
        self.assertEqual(sorted(sids["85935aed"]), [622078, 998382])

    def test_open_pids_prefix_both_ways(self):
        self.assertEqual(session.open_pids("deadbeef"), [622078])
        self.assertEqual(session.open_pids("deadbeef-memory-only"), [622078])
        self.assertEqual(session.open_pids("nosuch"), [])

    def test_memory_only_panes_excludes_forced_and_unstamped(self):
        mo = session.memory_only_panes()
        self.assertEqual([r["pid"] for r in mo], [622078])

    def test_ls_flags_memory_only_and_double_open(self):
        self.panes.append({"pid": 700, "resume": "deadbeef-memory-only",
                           "child": False, "sid8": "", "force": False})
        rc, out, _ = run(session.cmd_ls, [])
        self.assertEqual(rc, 0)
        self.assertIn("MEMORY-ONLY", out)
        self.assertIn("DOUBLE-OPEN", out)  # deadbeef open in 622078 + 700
        self.assertIn("stamped+FORCED (rescued)", out)


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

    def test_resume_prints_incantation_with_force(self):
        with self._sid("aaaa1111-integrator"), \
             mock.patch.object(session, "open_pids", return_value=[]):
            rc, out, _ = run(session.cmd_resume, ["aaaa1111"])
        self.assertEqual(rc, 0)
        self.assertIn("claude --resume aaaa1111-integrator", out)
        self.assertIn("CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1", out)
        # child-stamp unsets ride the line (paste-into-stamped-pane safe)
        for v in session._stamp_vars():
            self.assertIn("-u " + v, out)

    def test_port_refuses_live_and_prints_when_clear(self):
        home_rows = [{"name": "cto-example", "path": "/creds/cto-example", "aliases": []}]
        import helm.homes as homes_mod
        with self._sid("aaaa1111-integrator"), \
             mock.patch.object(homes_mod, "homes_list", return_value=home_rows):
            # live copy -> refuse (no incantation line printed)
            with mock.patch.object(session, "open_pids", return_value=[57699]):
                rc, out, _ = run(session.cmd_port,
                                 ["--cred", "cto-example", "aaaa1111"])
            self.assertEqual(rc, 1)
            self.assertIn("LAW 1", out)
            self.assertNotIn("claude --resume", out)
            # clear -> print with the credhome + FORCE
            with mock.patch.object(session, "open_pids", return_value=[]):
                rc, out, _ = run(session.cmd_port,
                                 ["--cred", "cto-example", "aaaa1111"])
            self.assertEqual(rc, 0)
            self.assertIn("CLAUDE_CONFIG_DIR=/creds/cto-example", out)
            self.assertIn("FORCE_SESSION_PERSISTENCE=1", out)

    def test_checkpoint_routes_memory_only_to_rescue(self):
        with self._sid("deadbeef-memory-only"):
            rc, _o, err = run(session.cmd_checkpoint, ["deadbeef"])
        self.assertEqual(rc, 1)
        self.assertIn("MEMORY-ONLY", err)
        self.assertIn("rescue", err)

    def test_checkpoint_mints_new_id_original_untouched(self):
        with self._sid("aaaa1111-integrator"), \
             mock.patch.object(session, "open_pids", return_value=[]), \
             self.cv_ok('{"ok":true}') as cv:
            rc, out, _ = run(session.cmd_checkpoint, ["aaaa1111"])
        self.assertEqual(rc, 0)
        self.assertIn("checkpoint minted:", out)
        # cv prune got --thinking + --to <newid>, never mutating the original
        argv = cv.call_args[0]
        self.assertIn("prune", argv)
        self.assertIn("--thinking", argv)
        self.assertIn("--to", argv)

    def test_cv_absent_degrades_clean(self):
        with self._sid("aaaa1111-integrator"), self.cv_absent():
            rc, _o, err = run(session.cmd_doctor, ["aaaa1111"])
        self.assertEqual(rc, 1)
        self.assertIn("cv not installed", err)
        self.assertNotIn("Traceback", err)


class RescueTest(SessionBase):
    def test_rescue_by_pid_resolves_sid_and_prints_plan(self):
        rc, out, _ = run(session.cmd_rescue, ["622078"])
        self.assertEqual(rc, 0)
        self.assertIn("deadbeef", out)          # resolved from the pid
        self.assertIn("HARVEST", out)            # the rescue-lane first step
        self.assertIn("claude --resume deadbeef", out)
        self.assertIn("FORCE_SESSION_PERSISTENCE=1", out)

    def test_rescue_unknown_pid(self):
        rc, _o, err = run(session.cmd_rescue, ["424242"])
        self.assertEqual(rc, 1)
        self.assertIn("no live claude pid", err)


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

    def test_ask_no_expert_falls_back_to_search(self):
        with self.cv_ok("search-hits") as cv:
            rc, out, _ = run(session.cmd_ask, ["nosuchdomain", "how", "does", "x"])
        self.assertEqual(rc, 0)
        self.assertIn("no expert for domain", out)
        self.assertIn("search-hits", out)

    def test_ask_with_expert_prints_regounded_resume(self):
        self._reg()
        with mock.patch.object(session, "open_pids", return_value=[]), \
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

    def test_ask_scopes_search_to_the_expert_transcript(self):
        """the ladder's rung 2 is the EXPERT's own transcript, not the corpus —
        a hit there is shown as such; a miss falls through to a corpus search."""
        self._reg()
        # expert's own transcript HAS the answer -> shown, no corpus fallback
        with mock.patch.object(session, "_grep_expert", return_value="  …the order is X…"), \
             mock.patch.object(session, "open_pids", return_value=[]):
            rc, out, _ = run(session.cmd_ask, ["helm-orchestration", "order"])
        self.assertIn("expert-transcript hits", out)
        self.assertNotIn("corpus search", out)
        # expert's transcript misses -> falls through to the corpus search
        with mock.patch.object(session, "_grep_expert", return_value=""), \
             mock.patch.object(session, "open_pids", return_value=[]), \
             self.cv_ok("corpus-hits"):
            rc, out, _ = run(session.cmd_ask, ["helm-orchestration", "order"])
        self.assertIn("corpus search", out)
        self.assertIn("corpus-hits", out)


class CliWiringTest(unittest.TestCase):
    def test_verb_registered_and_dispatch_clean(self):
        from helm import cli
        self.assertIn("session", cli.VERBS)
        self.assertIn("session", cli._VERB_HELP)
        rc, _o, err = run(session.cmd_session, ["bogus-verb"])
        self.assertEqual(rc, 2)
        self.assertIn("usage: helm session", err)
        self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
