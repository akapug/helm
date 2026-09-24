#!/usr/bin/env python3
"""task/2673 PART C — a claude pane Orca opened joins the fleet on its own.

An agent loaded in Orca is part of helm automatically. Three things make
that safe, and each has an arm here:

  * the join records the pane's ORCA_PANE_KEY on the roster row;
  * only a session Claude itself records as `interactive` enrols, so a `bg`
    job or a `fork` that inherits the pane's environment is not a seat;
  * a restart in the same pane keeps the seat name, but only when the old
    session is PROVEN dead: a live holder never loses its name.

Every arm runs in a temp HELM_HOME and a temp CLAUDE_CONFIG_DIR, and every
liveness probe is a double: nothing here reads the live roster or a real
credential home.
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import beacons, chat, seats, seats_cli, seats_join, sessions  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "HELM_CHAT_LOG", "HELM_CHAT_ROOM", "HELM_CHAT_ROOM_SOURCE",
            "HELM_CHAT_OWNER_NAMES", "HELM_ADOPTED_DIR", "HELM_SEAT_STORAGE",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID",
            "CLAUDE_CODE_CHILD_SESSION", "CLAUDECODE",
            "CLAUDE_CODE_SUBAGENT_MODEL", "ANTHROPIC_MODEL",
            "ORCA_PANE_KEY", "CLAUDE_CONFIG_DIR")

OLD = "11111111-2222-4333-8444-555555555555"
NEW = "66666666-7777-4888-8999-aaaaaaaaaaaa"
PANE = "pk-orca-1"
WORK = "/home/tester/dev/example/proj"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-orcaenroll-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        prior = {k: os.environ.get(k) for k in ENV_KEYS}

        def restore():
            for k, v in prior.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(restore)
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ.update({
            "HELM_CHAT_ROOM": "main",
            "HELM_HOME": os.path.join(self.tmp, "helm"),
            "HELM_CHAT_DIR": os.path.join(self.tmp, "chat"),
            "HELM_CHAT_NODE_URL": "",
            "HELM_CHAT_OWNER_NAMES": "daria",
            "HELM_ADOPTED_DIR": os.path.join(self.tmp, "adopted"),
            "CLAUDE_CONFIG_DIR": os.path.join(self.tmp, "cfg-home")})
        os.makedirs(os.path.join(self.tmp, "cfg-home", "sessions"))
        chat._ensure_dir()
        # NO REAL CREDENTIAL HOME IS EVER READ: the kind probe sees only the
        # temp CLAUDE_CONFIG_DIR above.
        homes = mock.patch.object(sessions, "cred_homes", return_value=[])
        homes.start()
        self.addCleanup(homes.stop)

    def record(self, sid, kind, pid=None):
        """Claude's own pid-keyed session record, at THIS process's pid, which
        is the first ancestor `session_kind` asks about."""
        path = os.path.join(self.tmp, "cfg-home", "sessions",
                            "%d.json" % (pid or os.getpid()))
        with open(path, "w") as f:
            json.dump({"pid": pid or os.getpid(), "sessionId": sid,
                       "kind": kind, "procStart": "4242"}, f)

    def join(self, sid, **kw):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            return seats_join.join(session=sid, cwd=WORK, **kw)

    @contextlib.contextmanager
    def old_session(self, live):
        """The liveness world for OLD: held by a live claude incarnation, or
        by nothing at all."""
        records = {OLD: (4242, "999")} if live else {}
        with mock.patch.object(beacons, "holder_records",
                               return_value=records), \
                mock.patch.object(beacons, "live_sessions", return_value={}), \
                mock.patch.object(sessions, "_pid_is_claude",
                                  return_value=live):
            yield


class JoinRecordsThePaneKeyTest(Base):

    def test_a_join_from_an_orca_pane_records_its_pane_key(self):
        os.environ["ORCA_PANE_KEY"] = PANE
        seat, _line = self.join(OLD)
        self.assertEqual(seats.roster()[seat].get("pane_key"), PANE)

    def test_a_join_with_no_pane_key_records_none(self):
        # CONTROL on the same observable: the field comes from the env.
        seat, _line = self.join(OLD)
        self.assertTrue(seat)
        self.assertNotIn("pane_key", seats.roster()[seat])


class RestartInTheSamePaneTest(Base):

    def first(self):
        os.environ["ORCA_PANE_KEY"] = PANE
        seat, _ = self.join(OLD)
        self.assertEqual(seats.roster()[seat]["session"], OLD)
        return seat

    def test_a_dead_old_session_hands_its_name_to_the_restart(self):
        name = self.first()
        with self.old_session(live=False):
            again, _ = self.join(NEW)
        self.assertEqual(again, name)
        self.assertEqual(seats.roster()[name]["session"], NEW)

    def test_a_live_old_session_keeps_its_name_and_the_restart_gets_2(self):
        name = self.first()
        with self.old_session(live=True):
            again, _ = self.join(NEW)
        self.assertEqual(again, name + "-2")
        rows = seats.roster()
        self.assertEqual(rows[name]["session"], OLD)
        # ONE PANE KEY, ONE ROW: the pane now holds the new session.
        self.assertEqual(rows[again].get("pane_key"), PANE)
        self.assertNotIn("pane_key", rows[name])

    def test_another_project_in_the_same_pane_does_not_inherit(self):
        name = self.first()
        with self.old_session(live=False):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                again, _ = seats_join.join(
                    session=NEW, cwd="/home/tester/dev/example/other")
        self.assertNotEqual(again, name)

    def test_an_unreadable_liveness_probe_is_not_proof_of_death(self):
        name = self.first()
        with mock.patch.object(beacons, "holder_records", return_value=None), \
                mock.patch.object(beacons, "live_sessions", return_value={}):
            again, _ = self.join(NEW)
        self.assertEqual(again, name + "-2")
        # CONTROL: with a readable world in which NEW is dead, a third
        # restart inherits from the row the pane now names, which is -2.
        with self.old_session(live=False):
            third, _ = self.join("77777777-0000-4000-8000-000000000000")
        self.assertEqual(third, name + "-2")


class OnlyAPaneEnrolsTest(Base):

    def test_a_bg_session_is_not_enrolled(self):
        self.record(OLD, "bg")
        seat, line = self.join(OLD, require_pane=False)
        self.assertIsNone(seat)
        self.assertEqual(line, "")
        self.assertEqual(seats.roster(), {})
        self.record(OLD, "interactive")                  # CONTROL
        self.assertIn(self.join(OLD, require_pane=False)[0], seats.roster())

    def test_a_fork_session_is_not_enrolled_even_by_the_orca_door(self):
        self.record(OLD, "fork")
        seat, _ = self.join(OLD, require_pane=True)
        self.assertIsNone(seat)
        self.assertEqual(seats.roster(), {})
        self.record(OLD, "interactive")                  # CONTROL
        self.assertIn(self.join(OLD, require_pane=True)[0], seats.roster())

    def test_an_interactive_session_is_enrolled(self):
        # CONTROL: the same fixture with the pane kind enrols.
        self.record(OLD, "interactive")
        seat, _ = self.join(OLD, require_pane=True)
        self.assertIn(seat, seats.roster())

    def test_no_record_enrols_in_helm_but_not_through_the_orca_door(self):
        seat, _ = self.join(OLD, require_pane=True)
        self.assertIsNone(seat)
        self.assertEqual(seats.roster(), {})
        seat, _ = self.join(OLD, require_pane=False)
        self.assertIn(seat, seats.roster())

    def test_a_record_for_another_session_does_not_answer(self):
        self.record(NEW, "bg")
        self.assertIsNone(seats_join.session_kind(OLD))
        self.assertEqual(seats_join.session_kind(NEW), "bg")


class TheHookLegTest(Base):
    """The SessionStart hook end to end: payload in, roster row out."""

    def hook(self, outside):
        payload = {"session_id": OLD, "cwd": WORK, "source": "startup"}
        emitted = []
        with mock.patch.object(seats_cli, "_hook_stdin", return_value=payload), \
                mock.patch.object(seats_cli, "_hook_emit",
                                  return_value=emitted.append), \
                mock.patch("helm.hooks.outside_helm", return_value=outside), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = seats_cli.cmd("join", ["--hook-json"])
        self.assertEqual(rc, 0)
        return emitted

    def test_an_orca_pane_outside_helm_enrols_with_its_pane_key(self):
        os.environ["ORCA_PANE_KEY"] = PANE
        self.record(OLD, "interactive")
        emitted = self.hook(outside=True)
        rows = seats.roster()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(next(iter(rows.values())).get("pane_key"), PANE)
        self.assertEqual(len(emitted), 1)

    def test_a_bg_job_outside_helm_is_silent_and_unenrolled(self):
        os.environ["ORCA_PANE_KEY"] = PANE
        self.record(OLD, "bg")
        self.assertEqual(self.hook(outside=True), [])
        self.assertEqual(seats.roster(), {})
        self.record(OLD, "interactive")                  # CONTROL
        self.assertEqual(len(self.hook(outside=True)), 1)
        self.assertEqual(len(seats.roster()), 1)

    def test_inside_helm_a_missing_record_still_enrols(self):
        self.assertEqual(len(self.hook(outside=False)), 1)
        self.assertEqual(len(seats.roster()), 1)


if __name__ == "__main__":
    unittest.main()
