#!/usr/bin/env python3
"""The Stop ladder's per-rung cost, counted in events and never timed (task/3556).

THE MEASUREMENT. Seven Stop hooks in five minutes on the fleet host read a
p95 of 1354.8 ms against a one-second bar, all of it inside the `chat
stop-guard` body. Profiled rung by rung against a fleet-shaped home (twenty
seats, forty rooms, sixty open dispatch rows in one repository, a live
beacon, about 900 processes on the host), three rungs were paying again for
answers they already had. Medians on four cores, before and after, at load
1.6 and at load 10.2:

  * whisper: an idle seat's work offer derived the project of every open
    dispatch row with two git processes, 120 per stop for sixty rows of ONE
    repository. 299 -> 50 ms and 535 -> 73 ms; the whole stop-guard span
    613 -> 322 ms and 903 -> 388 ms (p95 995 -> 479 ms).
  * beacon: the lenient probe (`_beacon_block`) and the strict one
    (`_rearm_rung`) each walked the whole process table, a stat and a
    cmdline read per process, milliseconds apart in the same rung; and the
    live-session census behind the strict probe read every process's `comm`,
    though only a process whose argv resumes a session can add a row to it.
    78 -> 46 ms and 90 -> 52 ms.
  * identity: the payload homing derived the room again, with two more git
    processes and a registry read, for the directory the chat prologue had
    just derived it for. 29 -> 23 ms and 31 -> 25 ms.

A WALL-CLOCK CEILING CANNOT PIN ANY OF THEM (tests/test_elapsed_ceilings.py):
load only lengthens a span, and a loaded host is where these numbers matter.
So each arm counts the rung's own events -- git-root derivations,
process-table walks, `comm` reads -- which no load can change, and which are
what the cut removed. Each is paired with a positive control: the rung still
reaches the answer it gave before, from the one reading it now takes.
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import chat, seats, seats_identity, sessions  # noqa: E402
from tests import test_seats  # noqa: E402 — the module, so its arms are not collected here again
from tests.test_seats import BeaconProcsBase, SeatsBase  # noqa: E402

SEAT = "cost-seat"
SID = "eeeeeeee-1111-2222-3333-444444444444"


def _walks(fn):
    """(fn's answer, how many times it listed the host's process table)."""
    real = os.listdir
    listed = []

    def listdir(path="."):
        if os.fspath(path) == "/proc":
            listed.append(path)
        return real(path)

    with mock.patch.object(os, "listdir", side_effect=listdir):
        answer = fn()
    return answer, len(listed)


class _Launched(SeatsBase):
    """A launched fleet seat: HELM_CHAT_NAME names it and its join ran, so the
    beacon rungs owe it both probes."""

    def setUp(self):
        super().setUp()
        # A module that drives stop-guard names this itself (the tripwire in
        # tests/test_scratch.py reads each module on its own).
        self.assertEqual(os.environ["HELM_SCRATCH_GC"], "0")
        self._stamp = mock.patch.dict(os.environ, {"HELM_CHAT_NAME": SEAT})
        self._stamp.start()
        seats.join(seat=SEAT, session=SID, cwd="/tmp/p")

    def tearDown(self):
        # Stopped BEFORE SeatsBase restores the environment: stopping after it
        # would wipe the value it just put back.
        self._stamp.stop()
        super().tearDown()


class BeaconRungCost(_Launched):
    """One reading of the process table per stop, and only per stop."""

    def test_one_stop_walks_the_process_table_once(self):
        with contextlib.redirect_stderr(io.StringIO()):   # the rung trace
            (blocks, warns), walks = _walks(lambda: seats.stop_guard(
                session=SID, room="main", seat=SEAT))
        self.assertEqual(walks, 1, "the beacon rung walked the process table "
                                   "once per probe instead of once per stop")
        # POSITIVE CONTROL: the strict probe still ran and still answered from
        # that one reading -- no waiter exists, so it says so.
        self.assertTrue([w for w in warns if "NO PROVEN WAKE PATH" in w],
                        (blocks, warns))

    def test_two_probes_outside_a_stop_each_walk(self):
        """THE READING BELONGS TO THE STOP. A caller that probes, acts and
        probes again (stalebot's before/after pair) must see the table twice."""
        answers, walks = _walks(lambda: (seats.beacon_procs(SEAT),
                                         seats.beacon_procs(SEAT, strict=True)))
        self.assertEqual([pids for pids, _trouble in answers], [[], []])
        self.assertEqual(walks, 2)


class LiveSessionCensusCost(BeaconProcsBase):
    """`sessions.live_sids`, which the strict beacon probe asks, reads a
    process's comm only when its argv could add a session."""

    def test_comm_is_read_only_where_an_argv_resumes_a_session(self):
        for pid in range(201, 209):
            self.proc(pid, ["sleep", "600"], comm="sleep")
        self.proc(300, ["claude", "--resume", "sid-cost"], comm="claude")
        real_open = open
        comms = []

        def counting(path, *args, **kwargs):
            if os.path.basename(os.fspath(path)) == "comm":
                comms.append(os.path.basename(os.path.dirname(path)))
            return real_open(path, *args, **kwargs)

        with mock.patch.object(sessions, "cred_homes", return_value=[]), \
                mock.patch.object(sessions, "open", side_effect=counting,
                                  create=True):
            live = sessions.live_sids()
        self.assertEqual(live, {"sid-cost": 300})
        self.assertEqual(comms, ["300"])


class WhisperRungCost(SeatsBase):
    """The work offer derives each repository's project once per pass."""

    def test_the_work_offer_derives_each_repository_once(self):  # noqa: VACUOUS_ASSERTION — the returned rows are the positive control: all five same-project rows offered and the other repository's row excluded, asserted unconditionally after the derivation counts
        seats.join(session="s-offer", seat="ds4pro", cwd="/tmp/p")
        plant = test_seats.WorkOfferTest.plant_dispatch   # the fixture, by reference
        mine = ["aa%02dbb22" % n for n in range(5)]
        for id8 in mine:
            plant(self, id8 + "cc33dd44", "ghost",
                  repo_id="/tmp/estate/helm/.git")
        plant(self, "ffeebb22cc33dd44", "ghost",
              repo_id="/tmp/estate/some-other-repo/.git")
        asked = []

        def project(path):
            asked.append(path)
            return ("some-other-repo" if "some-other" in str(path or "")
                    else "helm")

        with mock.patch.object(seats, "_git_project", side_effect=project):
            rows = seats._offer_rows("ds4pro")
        self.assertEqual(asked.count("/tmp/estate/helm"), 1,
                         "one git-root derivation per ROW, not per repository")
        self.assertEqual(asked.count("/tmp/estate/some-other-repo"), 1)
        # POSITIVE CONTROL: each same-project row is still offered, and the
        # other repository's row is still excluded.
        self.assertEqual(len(rows), len(mine))
        self.assertEqual(sorted(r[0] for r in rows), mine)


class IdentityRungCost(SeatsBase):
    """A hook standing in a project derives that project's room once."""

    def test_the_hook_derives_the_directory_it_stands_in_once(self):  # noqa: VACUOUS_ASSERTION — the ladder is stubbed so only the homing runs; the room it hands the ladder IS the call's observable, and equality to the cwd's derived room is the positive control
        repo = os.path.join(self.tmp, "proj")
        os.makedirs(repo)
        subprocess.run(["git", "init", "-q", repo], check=True,
                       capture_output=True, timeout=60)
        os.chdir(repo)                  # SeatsBase.tearDown changes back
        here = os.getcwd()
        real = seats_identity._git_root_typed
        derived, rooms = [], []

        def counting(cwd):
            if cwd == here:
                derived.append(cwd)
            return real(cwd)

        def ladder(**kwargs):
            rooms.append(kwargs.get("room"))
            return [], []

        payload = json.dumps({"session_id": SID, "cwd": here}).encode()
        stdin = types.SimpleNamespace(buffer=io.BytesIO(payload))
        with mock.patch.object(seats_identity, "_git_root_typed",
                               side_effect=counting), \
                mock.patch.object(seats, "stop_guard", side_effect=ladder), \
                mock.patch.object(sys, "stdin", stdin), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = chat.cmd_chat(["stop-guard", "--hook-json"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(derived), 1, "the payload homing derived the "
                                          "room the prologue had derived")
        # POSITIVE CONTROL: the ladder ran in the room derived from the cwd.
        self.assertEqual(rooms, [seats_identity.derive_home_room(here)])
        self.assertNotEqual(rooms, ["main"])


if __name__ == "__main__":
    unittest.main()
