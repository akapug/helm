#!/usr/bin/env python3
"""helm beacons — the registry, the lifecycle, and the two-directional alarm.

A seat's inbox beacon is HELM'S ONLY wake path to it, so every test here asserts an
EFFECT: that the ghost is REPORTED, that the deaf seat is REPORTED, that a
re-arm leaves exactly ONE waiter, and that an unprovable case KEEPS the
process. "Nothing raised" is a guaranteed vacuous pass and appears nowhere.

THE REAPER IS PROVEN ON REAL PROCESSES, not on a fixture tree. A stub cannot
show that SIGTERM reached the right pid and missed every other one, which is
the only claim that matters for an actuator that can sever a seat's wake path.
The fixtures are genuine `helm chat wait`-shaped python processes under a
UNIQUE seat name and a tmp HELM_HOME, so the live fleet is unreachable from
here twice over: by seat attribution and by the helm-home gate.
"""
import contextlib
import glob
import hashlib
import io
import itertools
import json
import os
import re
import shutil
import errno
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from helm import beacon_origin, beacons, chat, home, pk, seats  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "MELD_CHAT_NODE_URL", "HELM_PROC", "MELD_PROC",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID",
            # THE LIVE METAHARNESS IS AN INPUT TO THIS MODULE NOW. The census
            # asks the pane inventory how many live panes carry no seat
            # identity, and `harness.detect` finds the REAL daemon from a test
            # process — which would make an assertion about a fixture fleet
            # depend on how many panes the developer happens to have open.
            # Sandboxed here, and each test that wants an inventory injects one.
            "HELM_METAHARNESS")

SID_A = "aaaaaaaa-1111-2222-3333-444444444444"
SID_B = "bbbbbbbb-1111-2222-3333-444444444444"

_real_beacon_procs = seats.beacon_procs


LAUNCHER = 9001          # a live bash wrapper: what a healthy beacon parents to
REAPER = 9002            # `systemd --user`, which adopts this session's orphans


def _stat(pid, starttime, comm="python3", ppid=LAUNCHER):
    """A /proc/<pid>/stat line. After the LAST ')' the fields are state(0),
    ppid(1) … starttime(19) — ppid answers "is the launcher still there" and
    starttime is the incarnation key a recycled pid cannot forge."""
    return "%d (%s) S %s %d\n" % (
        pid, comm, " ".join([str(ppid)] + ["0"] * 17), starttime)


class SeatOfConfigDirTest(unittest.TestCase):
    """task/331: the pane self-declaration rule lived as TWO inline copies of
    a two-dirname check (here and hooks.running_panes) — exactly one level
    deep, so both readers dropped a slice-6 instance pane's CLAUDE_CONFIG_DIR
    at once, invisibly and in agreement. One definition now, both shapes.
    Pure-path: realpath on absent paths only normalizes, no fixture tree."""

    SROOT = "/x/helm-home/_global/seats"

    def test_both_minted_shapes_declare_their_holder(self):
        self.assertEqual(beacons.seat_of_config_dir(
            self.SROOT + "/codex/claude", self.SROOT), "codex")
        self.assertEqual(beacons.seat_of_config_dir(
            self.SROOT + "/codex/instances/codex-2/claude", self.SROOT),
            "codex-2")

    def test_unminted_shapes_declare_nothing(self):
        # POSITIVE CONTROL on the same observable first: the classifier CAN
        # say yes here, so five Nones below mean rejection, not a dead arm
        self.assertEqual(beacons.seat_of_config_dir(
            self.SROOT + "/codex/instances/codex-2/claude", self.SROOT),
            "codex-2")
        for p in (self.SROOT + "/codex/smoke-claude",          # not `claude`
                  self.SROOT + "/claude",                      # no family
                  self.SROOT + "/codex/claude/plugins/claude",  # inside a dir
                  self.SROOT + "/codex/instances/i/instances/j/claude",
                  "/elsewhere/codex/claude"):                  # foreign root
            self.assertIsNone(beacons.seat_of_config_dir(p, self.SROOT), p)

    def test_declared_seats_carries_an_instance_panes_config_dir(self):
        env = {"CLAUDE_CONFIG_DIR":
               self.SROOT + "/codex/instances/codex-2/claude"}
        self.assertEqual(beacons.declared_seats(env, self.SROOT), ["codex-2"])
        # positive control: the family shape still declares
        env = {"CLAUDE_CONFIG_DIR": self.SROOT + "/codex/claude"}
        self.assertEqual(beacons.declared_seats(env, self.SROOT), ["codex"])


class CorruptRosterIsNotAnEmptyFleetTest(unittest.TestCase):
    """`seats.roster()` is `pk.read_json(path, {}) or {}` and pk.read_json
    swallows EVERY exception, so a CORRUPT roster returns {} — byte-identical
    to an empty one. The attendance pass then finds no seats, raises no alert
    and exits 0: a silent no-op that looks exactly like a healthy fleet with
    nothing to report.

    Same could-not-look-recorded-as-a-fact class a review found in proxywatch's
    _read_watch_state, where a corrupt outbox read as {} and a queue holding
    pending alerts delivered nothing."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "roster.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def readable(self, contents=None):
        from helm import seats
        if contents is not None:
            with open(self.path, "w") as f:
                f.write(contents)
        with mock.patch.object(seats, "roster_path", return_value=self.path):
            return beacons._roster_readable()

    def test_a_CORRUPT_roster_is_refused_not_read_as_empty(self):
        ok, why = self.readable("{not json[")
        self.assertFalse(ok)
        self.assertIn("unreadable", why)

    def test_a_VALID_roster_passes(self):
        """UNCONDITIONAL CONTROL: the same helper on the same path says YES for
        parseable JSON, so the refusal above measures corruption and not a
        checker that refuses everything."""
        ok, why = self.readable('{"kimi": {}}')
        self.assertTrue(ok)
        self.assertIsNone(why)

    def test_the_PASS_actually_calls_the_check(self):
        """THE WIRE, not the helper. Mutation proved this: replacing the call
        with `ok, why = True, None` left all 99 tests green, because every
        assertion above drives `_roster_readable` DIRECTLY and none of them
        drives `attend`. A correct helper and an unreached call site are
        indistinguishable from the helper's own tests — third time tonight."""
        with mock.patch.object(beacons, "_roster_readable",
                               return_value=(False, "roster unreadable (test)")):
            out = beacons.attend({"seats": [{"seat": "kimi"}]})
        self.assertIn("SKIPPED", out.get("error") or "")
        self.assertEqual(out.get("written"), [],
                         "a refused pass must write NOTHING")
        # CONTROL on the same observable: with the check PASSING, the same call
        # runs — so the refusal above measures the wire and not a dead verb.
        with mock.patch.object(beacons, "_roster_readable",
                               return_value=(True, None)):
            ok_out = beacons.attend({"seats": []})
        self.assertIsNone(ok_out.get("error"))

    def test_an_ABSENT_roster_is_a_real_empty_and_stays_silent(self):
        """Absent is NOT corrupt. A roster never written is a legitimate
        first-run state; refusing there would break every fresh checkout."""
        ok, why = self.readable()          # file never created
        self.assertTrue(ok)
        self.assertIsNone(why)


class LockFailureMustNotWriteUnlockedTest(unittest.TestCase):
    """`_flocked` FAILS OPEN BY DESIGN — on OSError it sets .f = None and still
    returns, so a bare `with` acquires nothing and the body runs UNLOCKED.

    gate.py:1207 already reads it correctly (`if lock.f is None: return`). Both
    roster writers here did not, so a lock failure silently became a concurrent
    read-modify-write on the shared roster — the race the lock exists to stop,
    at the moment it is most likely: contention.

    REFUSE RATHER THAN DEGRADE. A skipped pass self-heals, because the next one
    re-derives from the register; a clobbered roster does not."""

    class _Unlockable:
        """Stands in for a lock that could not be taken — .f is None, exactly
        as _flocked leaves it after an OSError."""
        f = None
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class _Held:
        f = object()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def test_the_attendance_pass_REFUSES_and_says_why(self):
        from helm import seats
        rep = {"seats": [{"seat": "kimi", "presence": "quiet"}]}
        with mock.patch.object(seats, "_flocked",
                               return_value=self._Unlockable()) as flocked:
            out = beacons.attend(rep)
        self.assertIn("escalation lock unavailable", out.get("error") or "")
        self.assertEqual(flocked.call_count, 1,
                         "a missing outer lock still reached for the roster")
        self.assertEqual(out.get("written"), [],
                         "a refused pass must write NOTHING")

    def test_the_attendance_pass_REFUSES_a_missing_roster_lock(self):
        from helm import seats
        rep = {"seats": [{"seat": "kimi", "presence": "quiet"}]}
        with mock.patch.object(
                seats, "_flocked",
                side_effect=(self._Held(), self._Unlockable())) as flocked:
            out = beacons.attend(rep)
        self.assertIn("roster lock unavailable", out.get("error") or "")
        self.assertEqual(flocked.call_count, 2,
                         "the roster lock failure was never reached")
        self.assertEqual(out.get("written"), [],
                         "a refused pass must write NOTHING")

    def test_a_HELD_lock_still_lets_the_pass_run(self):
        """UNCONDITIONAL CONTROL: without this, the refusal above would also
        hold for a function that never runs at all, and the test would be
        asserting breakage rather than a guard."""
        rep = {"seats": []}
        out = beacons.attend(rep)      # real lock, really acquired
        # STRUCTURAL and unconditional: the call returned a real result shape,
        # so "no error" measures a pass that RAN rather than one that returned
        # early. assertIsNone alone would also hold for a stub returning {}.
        self.assertIsInstance(out, dict)
        self.assertIn("written", out)
        self.assertIn("transitions", out)
        self.assertIsNone(out.get("error"),
                          "the ordinary path must NOT refuse")

    def test_the_ack_writer_refuses_too(self):
        from helm import seats
        with mock.patch.object(seats, "_flocked",
                               return_value=self._Unlockable()):
            self.assertFalse(beacons._ack_alerts([("kimi", {}, None)]))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-beacons-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_METAHARNESS"] = "none"
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "home")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.makedirs(os.environ["HELM_HOME"], exist_ok=True)
        os.makedirs(os.environ["HELM_CHAT_DIR"], exist_ok=True)
        self.proc = os.path.join(self.tmp, "proc")
        os.makedirs(self.proc, exist_ok=True)
        # Both parents a beacon can have, so orphanhood is a CHOICE the fixture
        # makes rather than an accident of an empty tree.
        self.plant(LAUNCHER, ["bash", "-c", "helm chat wait"], comm="bash")
        self.plant(REAPER, ["/usr/lib/systemd/systemd", "--user"],
                   comm="systemd")

    def tearDown(self):
        for k, v in self.prior.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- fixture /proc ----------------------------------------------------
    def plant(self, pid, argv=None, env=None, starttime=100, comm="python3",
              ppid=LAUNCHER):
        d = os.path.join(self.proc, str(pid))
        os.makedirs(d, exist_ok=True)
        if argv is not None:
            with open(os.path.join(d, "cmdline"), "wb") as f:
                f.write(b"\0".join(a.encode() for a in argv) + b"\0")
        with open(os.path.join(d, "environ"), "wb") as f:
            f.write(b"\0".join(e.encode() for e in (env or [])) + b"\0")
        with open(os.path.join(d, "stat"), "w") as f:
            f.write(_stat(pid, starttime, comm, ppid))
        with open(os.path.join(d, "comm"), "w") as f:
            f.write(comm + "\n")
        return d

    def agent(self, pid, seat=None, config_dir=None, starttime=100,
              argv=None, comm="claude"):
        """A claude AGENT PANE — the thing a beacon is NOT.

        `seat=None` is the case that decides most of a real box: a
        hand-launched pane joins the roster under a DERIVED name and stamps
        nothing in its own environ, so it declares no seat while being a
        perfectly live agent."""
        env = ["HOME=%s" % self.tmp,
               "HELM_HOME=%s" % os.environ["HELM_HOME"]]
        if seat:
            env.append("HELM_CHAT_NAME=%s" % seat)
        if config_dir:
            env.append("CLAUDE_CONFIG_DIR=%s" % config_dir)
        return self.plant(pid, argv or ["claude", "--dangerously-skip-permissions"],
                          env, starttime, comm=comm)

    def waiter(self, pid, seat, sid=SID_A, starttime=100, helm_home=None,
               with_flag=True, extra_env=(), ppid=LAUNCHER):
        argv = [sys.executable, "/x/bin/helm", "chat", "wait"]
        if with_flag:
            argv += ["--seat", seat]
        argv += ["--follow"]
        env = ["HOME=%s" % self.tmp,
               "HELM_HOME=%s" % (helm_home or os.environ["HELM_HOME"]),
               "HELM_CHAT_NAME=%s" % seat]
        if sid:
            env.append("CLAUDE_CODE_SESSION_ID=%s" % sid)
        env += list(extra_env)
        return self.plant(pid, argv, env, starttime, ppid=ppid)

    def roster(self, *names, fresh=True):
        rows = {n: {"session": SID_A,
                    "last_seen": time.time() if fresh else 1.0} for n in names}
        path = os.path.join(os.environ["HELM_CHAT_DIR"], ".roster.json")
        with open(path, "w") as f:
            json.dump(rows, f)


# ---------------------------------------------------------------------------
# the liveness oracle
# ---------------------------------------------------------------------------

class SessionStateTest(Base):
    """`session_state` is the whole difference between a beacon and a shape."""

    def test_a_held_session_is_live(self):
        state, why = beacons.session_state(SID_A, live={SID_A: 999})
        self.assertEqual(state, "live")
        self.assertIn("999", why)

    def test_a_holder_that_is_gone_proves_the_session_dead(self):
        """Orphan shells: detached waiters whose claude session exited.
        The holder pid is simply not there any more."""
        state, why = beacons.session_state(
            SID_A, holder=(4242, 100), live={}, proc_dir=self.proc)
        self.assertEqual(state, "dead")
        self.assertIn("4242", why)

    def test_a_recycled_holder_pid_proves_the_session_dead(self):
        """A pid that exists is not the SAME process. The birth stamp is the
        anti-reuse key: a mismatch means the original is gone."""
        self.plant(4242, ["something", "else"], starttime=777)
        state, why = beacons.session_state(
            SID_A, holder=(4242, 100), live={}, proc_dir=self.proc)
        self.assertEqual(state, "dead")
        self.assertIn("4242", why)

    def test_a_live_holder_under_a_DIFFERENT_session_is_UNKNOWN_never_dead(self):
        """THE COMPACTION CASE, and the one predicate that must never exist.

        A compaction renames the session INSIDE the same process — measured
        live 2026-07-30, where a pane was declared unrecoverable by exactly
        this mismatch while it was mid-turn in that very process. Calling this
        DEAD would let a re-arm SIGTERM the live beacon of a healthy seat every
        time it compacted, which is this lane's own bug wearing the opposite
        sign."""
        self.plant(4242, ["claude", "--resume", SID_B], starttime=100)
        state, why = beacons.session_state(
            SID_A, holder=(4242, 100), live={SID_B: 4242}, proc_dir=self.proc)
        self.assertEqual(state, beacons.UNKNOWN)
        self.assertIn("compaction", why)

    def test_no_recorded_holder_is_UNKNOWN(self):
        with mock.patch.object(beacons, "holder_from_records", return_value=None):
            state, why = beacons.session_state(SID_A, live={})
        self.assertEqual(state, beacons.UNKNOWN)
        self.assertIn("no holder", why)

    def test_an_unprobeable_liveness_map_is_UNKNOWN_not_an_empty_world(self):
        """`live_sessions()` returning None means the probe failed. Reading it
        as 'no session is live' would mark every beacon on the box a ghost."""
        with mock.patch.object(beacons, "live_sessions", return_value=None):
            state, why = beacons.session_state(SID_A, holder=(4242, 100))
        self.assertEqual(state, beacons.UNKNOWN)
        self.assertIn("could not be probed", why)

    def test_a_waiter_with_no_session_id_is_UNKNOWN(self):
        state, why = beacons.session_state(None, live={})
        self.assertEqual(state, beacons.UNKNOWN)
        self.assertIn("no harness session id", why)

    def test_pid_alive_separates_gone_from_unreadable(self):
        """The tri-state that everything else rests on: False is the ONLY
        value that is evidence of death."""
        self.plant(11, ["x"], starttime=100)
        self.assertIs(beacons.pid_alive(11, 100, self.proc), True)
        self.assertIs(beacons.pid_alive(11, 999, self.proc), False)   # recycled
        self.assertIs(beacons.pid_alive(12, 100, self.proc), False)   # absent
        os.chmod(os.path.join(self.proc, "11", "stat"), 0)
        try:
            self.assertIsNone(beacons.pid_alive(11, 100, self.proc))
        finally:
            os.chmod(os.path.join(self.proc, "11", "stat"), 0o644)


# ---------------------------------------------------------------------------
# the registry
# ---------------------------------------------------------------------------

class OrphanTest(Base):
    """The launcher question, and the reason it is not `getppid() == 1`.

    MEASURED across all 22 live beacons on this box 2026-07-30: ZERO have
    ppid 1. The one genuinely orphaned beacon — seat gemini, pane `pane=-,
    turn=off`, armed 33h earlier — had ppid 3915, which is
    `/usr/lib/systemd/systemd --user`, uid 1000. systemd's user manager is a
    CHILD SUBREAPER, so it adopts its session's orphans and init never sees
    them. `seats._beacon_orphaned` had used ppid==1 since it was written, which
    means it had never once fired on this host class; gemini's beacon is the
    live proof, since it is exactly the process that check exists to retire."""

    def test_a_beacon_adopted_by_the_USER_MANAGER_is_orphaned(self):
        self.waiter(401, "alpha", ppid=REAPER)
        self.assertIs(beacons.orphaned(401, self.proc), True)

    def test_a_beacon_adopted_by_INIT_is_orphaned(self):
        self.plant(1, ["/sbin/init"], comm="init", ppid=0)
        self.waiter(402, "alpha", ppid=1)
        self.assertIs(beacons.orphaned(402, self.proc), True)

    def test_a_beacon_whose_LAUNCHER_LIVES_is_not_orphaned(self):
        self.waiter(403, "alpha", ppid=LAUNCHER)
        self.assertIs(beacons.orphaned(403, self.proc), False)

    def test_an_UNNAMEABLE_parent_is_UNKNOWN_and_keeps_the_process(self):
        """None keeps the beacon. A parent helm cannot read is not a dead one,
        and killing on that guess severs a live seat's wake path."""
        self.waiter(404, "alpha", ppid=7777)          # no /proc/7777 planted
        self.assertIsNone(beacons.orphaned(404, self.proc))

    def test_an_ORPHAN_is_a_GHOST_even_when_its_session_still_resolves(self):
        """The two death proofs sit at different layers and must not be folded
        together: `orphaned` says the pipe's READER is gone, `session_state`
        says the session is gone. An orphan cannot wake anybody whatever its
        session id resolves to."""
        self.roster("alpha")
        self.waiter(405, "alpha", sid=SID_A, ppid=REAPER)
        with mock.patch.object(beacons, "live_sessions",
                               return_value={SID_A: 90}):
            rep = beacons.census(proc_dir=self.proc)
        self.assertEqual([b["pid"] for b in rep["ghosts"]], [405])
        self.assertEqual([r["seat"] for r in rep["deaf"]], ["alpha"])

    def test_the_seat_self_exit_asks_the_SAME_question(self):
        """`seats._beacon_orphaned` is the beacon's own suicide check. It must
        share this definition, or the census retires a class of beacon the
        beacons themselves keep running.

        It asks through `parent_lost(os.getppid())`, NOT through a /proc read
        of its own pid: getppid() is the race-free answer to "who is my
        parent". Routing the self-check through /proc silently broke the seam
        the orphan test drives, and the beacon loop then never exited — the
        whole suite hung rather than failing."""
        with mock.patch.object(beacons, "parent_lost", return_value=True):
            self.assertTrue(seats._beacon_orphaned())
        with mock.patch.object(beacons, "parent_lost", return_value=False):
            self.assertFalse(seats._beacon_orphaned())
        with mock.patch.object(beacons, "parent_lost", return_value=None):
            self.assertFalse(seats._beacon_orphaned(), "UNKNOWN must not exit")

    def test_the_self_exit_reads_its_OWN_parent_via_getppid(self):
        """The seam an existing test drives, pinned so it cannot be routed
        around again: patching os.getppid must change the answer."""
        self.plant(1, ["/sbin/init"], comm="init", ppid=0)
        with mock.patch.object(os, "getppid", return_value=1):
            self.assertTrue(seats._beacon_orphaned())

    def test_the_self_exit_keeps_its_kill_switch(self):
        os.environ["HELM_BEACON_ORPHAN_EXIT"] = "0"
        try:
            with mock.patch.object(beacons, "parent_lost", return_value=True):
                self.assertFalse(seats._beacon_orphaned())
        finally:
            os.environ.pop("HELM_BEACON_ORPHAN_EXIT", None)



VERSIONED = "/x/.local/share/claude/versions/1.2.3"


class LauncherRungTest(Base):
    """The launcher the BEACON names, and the two rungs it repairs.

    MEASURED ON THE LIVE BOX 2026-08-22, which is what this class encodes:
    `helm beacons` answered UNPROVEN for 15 of 16 seats — every healthy one —
    and answered `unknown` for the single genuinely dead beacon on the machine.
    Two independent blind spots produced that, and both are cases where the
    instrument questioned a process that could not answer:

      THE PARENT IS A WRAPPER. A beacon runs under `bash -c '... helm chat wait
      ...'`, so when the agent dies systemd adopts the BASH. The wrapper stays
      the beacon's parent and stays alive, and `orphaned` — which asks only the
      immediate parent — reports False for a beacon whose launcher is provably
      gone. Seat infra-qwen: beacon 3743772 -> bash 3743771 (alive) ->
      systemd 6604, agent 3502769 ABSENT.

      THE AGENT DECLARES NOTHING. Native panes carry no HELM_CHAT_NAME, so the
      agent rung had nothing to read and honestly refused to answer, for live
      and dead seats alike.

    THE FIXTURE WAS RICHER THAN PRODUCTION, which is why no existing arm caught
    either one: `Base.agent` plants argv `["claude", ...]` and comm `claude`, a
    shape the launcher stopped emitting. Every arm here plants the VERSIONED
    shape instead, and asserts the fixture really is versioned before drawing
    any conclusion from it — a fixture that quietly reverted to the old name
    would make this whole class pass while proving nothing.

    Synthetic /proc trees only: no host path, no real seat name."""

    def census(self, live=None):
        with mock.patch.object(beacons, "live_sessions",
                               return_value={} if live is None else live):
            return beacons.census(proc_dir=self.proc)

    def versioned(self, pid, seat=None, starttime=100, exe=VERSIONED):
        """A live agent in the shape production actually emits, INCLUDING the
        kernel's own `exe` link — the evidence a live verdict now rests on,
        because argv0 is writable by the process and `exe` is not."""
        d = self.agent(pid, seat, starttime=starttime,
                       argv=[VERSIONED, "--dangerously-skip-permissions"],
                       comm="1.2.3")
        if exe:
            os.symlink(exe, os.path.join(d, "exe"))
        planted = beacons.proc_argv(pid, self.proc) or []
        self.assertEqual(planted[:1], [VERSIONED],
                         "the fixture must plant the VERSIONED shape, or "
                         "every arm below proves something else")
        self.assertEqual(beacons.proc_exe(pid, self.proc), exe or None,
                         "the fixture must plant the exe link the live verdict "
                         "reads, or a passing arm proves nothing about it")
        return d

    def unparseable_stat(self, pid):
        """A stat that READS and does not PARSE — the only way a process is
        alive while its start-time is unknown, and the exact hole the
        anti-reuse check used to fall through."""
        with open(os.path.join(self.proc, str(pid), "stat"), "w") as f:
            f.write("%s (x) S 1\n" % pid)
        self.assertIsNone(beacons.proc_starttime(pid, self.proc))
        self.assertIs(beacons.pid_alive(pid, proc_dir=self.proc), True,
                      "the fixture must leave the process ALIVE, or this "
                      "measures the absent-pid branch instead")

    def test_a_versioned_agent_that_declares_NOTHING_proves_its_seat(self):
        """THE 15-of-16 CASE. No declaration on the pane and no session map
        entry — the two things the old rungs needed — and the seat is still
        proven, because the beacon names its launcher and the launcher is
        alive. `live={}` is not a convenience here: it is what the real box
        returns, and it used to end the question before this evidence was
        read."""
        self.roster("alpha")
        self.waiter(601, "alpha", extra_env=["CLAUDE_PID=90"])
        self.versioned(90)                        # seat=None: declares nothing
        rep = self.census()
        self.assertEqual([r["seat"] for r in rep["covered"]], ["alpha"],
                         "POSITIVE CONTROL: the empty lists below mean "
                         "nothing unless the pass produced a real verdict")
        self.assertEqual(rep["seats"][0]["verdict"], beacons.COVERED)
        self.assertEqual(rep["unproven"], [])
        self.assertEqual(rep["ghosts"], [])

    def test_a_dead_launcher_is_a_GHOST_though_its_parent_is_ALIVE(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is `orphaned(602) is False`, an INTENTIONAL absence assertion that cannot have a positive pole on its own observable: the whole point is that the old rung stays silent on a beacon the new one condemns. Its control is pid 699 on the SAME predicate, proven able to answer True in this fixture, and the effect the test measures is asserted positively as a ghost pid and a DEAF seat
        """THE WRAPPER-BUFFERED ORPHAN, asserted together with the proof that
        the OLD rung genuinely misses it. Without that second assertion this
        arm would also pass against a build where `orphaned` did the work, and
        it would be measuring the wrong rung."""
        self.roster("alpha")
        self.waiter(602, "alpha", ppid=LAUNCHER, extra_env=["CLAUDE_PID=91"])
        # A NON-WAITER on purpose: a second beacon for this seat would land in
        # the ghost list below and the assertion would be measuring the
        # control instead of the defect. `orphaned` reads a ppid and does not
        # care what the process is, so a plain planted pid proves the rung can
        # fire without joining the census.
        self.plant(699, ["sleep", "1"], comm="sleep", ppid=REAPER)
        self.assertIs(beacons.orphaned(699, self.proc), True,
                      "POSITIVE CONTROL: the reaper rung must be ABLE to fire "
                      "in this fixture, or its False below proves nothing")
        self.assertIs(beacons.orphaned(602, self.proc), False,
                      "the reaper rung must NOT fire here — a live bash "
                      "wrapper is the whole reason this case was invisible")
        rep = self.census()
        self.assertEqual([b["pid"] for b in rep["ghosts"]], [602])
        self.assertEqual([r["seat"] for r in rep["deaf"]], ["alpha"])

    def test_a_pid_that_POSTDATES_its_own_beacon_was_recycled(self):
        """PID REUSE, first proof. A parent cannot start after its child, so a
        launcher whose start-time is later is a different process wearing the
        number, and the launcher itself is gone."""
        self.waiter(603, "alpha", starttime=500, extra_env=["CLAUDE_PID=92"])
        self.versioned(92, starttime=900)
        self.assertEqual(beacons.launcher(603, self.proc)[1], "dead")

    def test_a_pid_now_worn_by_something_ELSE_was_recycled(self):
        """PID REUSE, second proof. The original set CLAUDE_PID for its own
        child, so it WAS an agent; anything else holding the number is not it."""
        self.waiter(604, "alpha", extra_env=["CLAUDE_PID=93"])
        self.plant(93, ["/usr/bin/vim", "notes.txt"], comm="vim", starttime=50)
        self.assertEqual(beacons.launcher(604, self.proc)[1], "dead")

    def test_an_UNREADABLE_argv_retires_NOBODY(self):
        """FAIL-CLOSED, and the trap it avoids is specific: comm is now a bare
        VERSION STRING, which identifies nothing. Reading "comm is not claude"
        as death would retire every live seat whose cmdline happened to be
        unreadable for one pass."""
        self.plant(94, None, comm="1.2.3", starttime=50)      # no cmdline file
        self.waiter(605, "alpha", extra_env=["CLAUDE_PID=94"])
        self.versioned(97, starttime=50)
        self.waiter(698, "alpha", extra_env=["CLAUDE_PID=97"])
        self.assertEqual(beacons.launcher(698, self.proc)[1], "live",
                         "POSITIVE CONTROL: the SAME call must reach a verdict "
                         "when the argv is readable, or None below is inert")
        self.assertIsNone(beacons.launcher(605, self.proc)[1],
                          "a missing argv is missing evidence, not a verdict")

    def test_a_beacon_that_names_NO_launcher_is_left_where_it_was(self):
        """A hand-armed beacon has no CLAUDE_PID at all. This rung must add no
        opinion about it — the older rungs keep whatever they concluded."""
        self.roster("alpha")
        self.waiter(606, "alpha", sid=SID_A)
        # NOT a waiter, for the same reason pid 699 is not one above: any
        # extra beacon for this seat joins the census and the ghost assertion
        # below would be measuring the control. `launcher` reads an environ,
        # so a plain planted process exercises it without joining anything.
        self.plant(697, ["sleep", "1"], env=["CLAUDE_PID=98"], comm="sleep")
        self.assertEqual(beacons.launcher(697, self.proc)[0], 98,
                         "POSITIVE CONTROL: this reader MUST return a pid when "
                         "the beacon names one, or the None below is inert")
        self.assertIsNone(beacons.launcher(606, self.proc)[0])
        rep = self.census({SID_A: 90})
        self.assertEqual([b["pid"] for b in rep["seats"][0]["live"]], [606],
                         "POSITIVE CONTROL: the census must have SEEN this "
                         "beacon, or an empty ghost list is about nothing")
        self.assertEqual(rep["ghosts"], [],
                         "a beacon naming no launcher must not become a ghost")

    def test_an_UNREADABLE_START_TIME_is_UNKNOWN_not_a_PASSED_CHECK(self):
        """CODEX-3 FIX (1). The start-time comparison IS the anti-reuse guard.
        It used to be skipped when either read failed, so a recycled pid
        wearing an agent shape fell straight through to `live` and CLEARED THE
        SEAT TO COVERED — the guard degrading silently into its own absence."""
        self.waiter(609, "alpha", extra_env=["CLAUDE_PID=99"])
        self.versioned(99)
        self.assertEqual(beacons.launcher(609, self.proc)[1], "live",
                         "POSITIVE CONTROL on the SAME path: this exact fixture "
                         "MUST reach `live` while the start-time is readable, "
                         "or the UNKNOWN below is not caused by removing it")
        self.unparseable_stat(99)
        self.assertIsNone(beacons.launcher(609, self.proc)[1],
                          "with no start-time the incarnation is unproven, and "
                          "unproven must not clear a seat to COVERED")

    def test_a_FORGED_argv_does_not_survive_the_kernel(self):
        """CODEX-3 FIX (2), and the reason a live verdict reads `exe` at all:
        argv0 is writable BY THE PROCESS, so a stranger that wants to pass for
        an agent forges it in one call. /proc/<pid>/exe is a kernel symlink to
        the inode actually running and cannot be written at all."""
        self.waiter(610, "alpha", extra_env=["CLAUDE_PID=91"])
        d = self.agent(91, None, argv=[VERSIONED, "--x"], comm="1.2.3")
        os.symlink("/usr/bin/vim", os.path.join(d, "exe"))
        self.assertTrue(beacons.is_agent(beacons.proc_argv(91, self.proc),
                                         "1.2.3"),
                        "POSITIVE CONTROL: the forged argv DOES pass the argv "
                        "test — that is what makes the exe check load-bearing")
        self.assertEqual(beacons.launcher(610, self.proc)[1], "dead")

    def test_a_LOOKALIKE_versions_directory_is_not_a_RELEASE(self):
        """CODEX-3 FIX (2). `/tmp/claude/versions/fake` is two mkdirs. Binding
        the modern spelling to the LAYOUT alone accepted it; the leaf must also
        BE a version."""
        self.assertTrue(beacons.is_agent(["/x/.local/share/claude/versions/2.1.238"],
                                         "2.1.238"),
                        "POSITIVE CONTROL: the production layout MUST pass")
        self.assertFalse(beacons.is_agent(["/tmp/claude/versions/fake"], "fake"))
        self.assertFalse(beacons.is_agent(["/tmp/claude/versions/latest"],
                                          "latest"))

    def test_an_UNNAMEABLE_PARENT_does_not_erase_a_PROVEN_launcher(self):
        """CODEX-3 FIX. The legacy demotion fires on `lost is None`, which means
        `orphaned` could not NAME THE PARENT — and it then writes "its launcher
        could not be identified" onto a row where `launcher` HAD identified it,
        by a different and stronger route. The unscoped form does not merely
        lose information, IT ASSERTS A FALSEHOOD about its own row.

        WHAT BUILD FAILS THIS ARM: the one shipped as 5fa566a9f, where the
        demotion is unscoped. The pair below is what makes it discriminating
        rather than merely true — same unnameable parent in both, and the ONLY
        difference is whether a launcher was proven."""
        self.waiter(611, "alpha", ppid=7777,          # no /proc/7777 planted
                    extra_env=["CLAUDE_PID=97"])
        self.versioned(97)
        self.assertIsNone(beacons.orphaned(611, self.proc),
                          "the fixture must produce the UNNAMEABLE-parent case, "
                          "or this arm never reaches the demotion at all")
        row = beacons.classify(611, "alpha", live={}, proc_dir=self.proc)
        self.assertEqual(row["state"], beacons.LIVE)
        self.assertIn("launcher is live agent pid 97", row["why"],
                      "POSITIVE CONTROL on the SAME field: the row must SAY "
                      "which launcher it proved, or the absence asserted next "
                      "is an absence from a sentence nobody wrote")
        self.assertNotIn("could not be identified", row["why"])

    def test_an_UNNAMEABLE_PARENT_still_demotes_when_NO_launcher_is_proven(self):
        """THE OTHER HALF OF THE PAIR, and the reason the scoping is a scoping
        and not a deletion: with no launcher proven, the demotion is exactly as
        right as it always was and must still fire."""
        self.waiter(612, "alpha", ppid=7777, sid=SID_A)   # and NO CLAUDE_PID
        self.assertIsNone(beacons.orphaned(612, self.proc))
        row = beacons.classify(612, "alpha", live={SID_A: 90}, proc_dir=self.proc)
        self.assertEqual(row["state"], beacons.UNKNOWN)
        self.assertIn("could not be identified", row["why"])

    def test_a_REARMED_beacon_clears_the_seat_that_a_GHOST_left_DEAF(self):
        """THE REVERSE ARC. A ghost beside a healthy re-arm must not hold the
        seat down: the dead one is still reported, and the seat is covered."""
        self.roster("alpha")
        self.waiter(607, "alpha", extra_env=["CLAUDE_PID=95"])   # 95 never planted
        self.waiter(608, "alpha", extra_env=["CLAUDE_PID=96"])
        self.versioned(96)
        rep = self.census()
        self.assertEqual([b["pid"] for b in rep["ghosts"]], [607])
        self.assertEqual(rep["seats"][0]["verdict"], beacons.COVERED)
        self.assertEqual(rep["deaf"], [])


class NoOverclaimOnAnySurfaceTest(Base):
    """CODEX-3 FIX (3). DEAF said "nothing can wake it" and the push said
    "nothing can wake them". This census reads BEACONS, so what it can prove is
    the state of HELM'S leg — never the absence of every leg. playapal-qwen has
    a designed external, non-consuming wake path and rendered DEAF the moment
    this lane made its panes visible at all.

    THE PUSH IS THE SURFACE THAT MATTERS MOST and is the one I missed first: I
    grepped the singular phrasing, found seven sites, fixed them, and reported
    it done. The PLURAL lived in the phone body — the one line that reaches the
    owner in the dark, where a wrong claim costs the most and gets the least
    scrutiny. So the arms below are on the RENDERED text of each surface, not
    on the verdict constant, because the constant was never what over-claimed."""

    def render(self, rep):
        return CensusTest.render(self, rep)

    def deaf_census(self):
        self.roster("alpha")                     # rostered, and no beacon at all
        with mock.patch.object(beacons, "live_sessions", return_value={}):
            return beacons.census(proc_dir=self.proc)

    def test_the_RENDERED_deaf_line_claims_only_the_HELM_leg(self):
        rep = self.deaf_census()
        out = self.render(rep)
        self.assertEqual([r["seat"] for r in rep["deaf"]], ["alpha"],
                         "POSITIVE CONTROL: a DEAF row must exist, or the "
                         "phrase assertions below are about an empty render")
        self.assertIn("helm cannot wake it", out)
        self.assertNotIn("nothing can wake it", out)

    def test_the_HEADLINE_does_not_call_the_beacon_the_only_wake_path(self):
        out = self.render(self.deaf_census())
        self.assertIn("helm's only wake path to a seat", out)
        self.assertNotIn("a seat's ONLY wake path", out)

    def test_the_PHONE_body_claims_only_the_HELM_leg(self):
        """The one line read in the dark, and the last place the over-claim was
        still live after I had reported the sweep finished."""
        body = beacons._push_body([("seat-a", {"alarm": True}, None)])
        self.assertIn("1 seat UNREACHABLE (seat-a)", body,
                      "POSITIVE CONTROL: the body must actually describe the "
                      "alarm, or the phrase assertions read an empty string")
        self.assertIn("helm cannot wake them", body)
        self.assertNotIn("nothing can wake them", body)

    def test_a_RECOVERY_push_is_untouched_by_the_correction(self):
        """The correction must not leak into the other polarity: a recovery
        body carries no reachability claim at all and must stay that way."""
        body = beacons._push_body([("seat-a", {"alarm": False}, None)])
        self.assertIn("reachable again", body)
        self.assertNotIn("cannot wake", body)


class VersionedAgentSpellingTest(Base):
    """`is_agent` answered False for 16 OF 16 live agents on this box — every
    seat, both families, both installed versions — because the launcher exec's
    `.../claude/versions/<version>` and the kernel copies that basename into
    comm. A predicate that is False across its entire population is blind, not
    strict, and this one gates a pane census, `hooks.running_panes`, and gate
    capability."""

    def test_the_VERSIONED_path_is_an_agent(self):
        self.assertTrue(beacons.is_agent([VERSIONED, "--x"], "1.2.3"))

    def test_the_OLD_spellings_still_answer(self):
        self.assertTrue(beacons.is_agent(["/usr/bin/claude"], "bash"))
        self.assertTrue(beacons.is_agent(None, "claude"))

    def test_a_LOOKALIKE_directory_is_not_an_agent(self):
        """The reason this matches the install LAYOUT and not the bare word:
        `claude` anywhere in a path would admit any script filed beneath a
        directory of that name, and this predicate decides whether a live
        process is some seat's agent."""
        self.assertTrue(beacons.is_agent(["/opt/claude/versions/1.2.3"],
                                         "1.2.3"),
                        "POSITIVE CONTROL: this path shape MUST pass, or the "
                        "two refusals below are just a broken predicate")
        self.assertFalse(beacons.is_agent(["/opt/notclaude/versions/1.2.3"],
                                          "1.2.3"))
        self.assertFalse(beacons.is_agent(["/x/claude/plugins/1.2.3"], "1.2.3"))

    def test_BOTH_READS_FAILING_is_still_not_a_NO(self):
        """The standing trap, pinned so the third spelling did not quietly
        turn an unreadable process into a decided one."""
        self.assertTrue(beacons.is_agent(None, "claude"),
                        "POSITIVE CONTROL: a None argv is not itself a No")
        self.assertFalse(beacons.is_agent(None, None))


class RegistryTest(Base):
    def test_register_writes_seat_session_pid_and_arm_time(self):
        with mock.patch.object(beacons, "live_sessions", return_value={}):
            row = beacons.register("alpha", session=SID_A, pid=4242,
                                   proc_dir=self.proc, now=1234.0)
        self.assertEqual(row["seat"], "alpha")
        self.assertEqual(row["session"], SID_A)
        self.assertEqual(row["pid"], 4242)
        self.assertEqual(row["armed"], 1234.0)
        got = beacons.entries("alpha")
        self.assertEqual([r["pid"] for r in got], [4242])
        self.assertEqual(got[0]["session"], SID_A)

    def test_the_row_survives_the_session_that_armed_it(self):
        """The lesson, and the reason a file beats a harness task id:
        'stop your own by the task id YOU hold' fails at a compaction boundary
        because the holding session is dead and nobody holds the id."""
        with mock.patch.object(beacons, "live_sessions", return_value={}):
            beacons.register("alpha", session=SID_A, pid=4242,
                             proc_dir=self.proc)
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        self.assertEqual([r["session"] for r in beacons.entries("alpha")],
                         [SID_A])

    def test_the_row_is_keyed_on_pid_so_a_compaction_does_not_invalidate_it(self):
        """Keyed on session, a compaction would orphan the row of a perfectly
        armed beacon. Keyed on pid it is a non-event: the row still names the
        same process and the session is just a fact that moved on."""
        with mock.patch.object(beacons, "live_sessions", return_value={}):
            beacons.register("alpha", session=SID_A, pid=4242,
                             proc_dir=self.proc)
        self.waiter(4242, "alpha", sid=SID_B)          # same proc, new session
        rows = beacons.entries("alpha")
        self.assertEqual([r["pid"] for r in rows], [4242])
        with mock.patch.object(beacons, "live_sessions",
                               return_value={SID_B: 90}):
            got = beacons.classify(4242, "alpha", rows[0], {SID_B: 90},
                                   self.proc)
        self.assertEqual(got["session"], SID_B)        # the live one, from /proc
        self.assertEqual(got["state"], beacons.LIVE)

    def test_the_holder_is_frozen_at_arm_time_so_it_outlives_the_session(self):
        self.plant(90, ["claude"], starttime=555)
        with mock.patch.object(beacons, "live_sessions",
                               return_value={SID_A: 90}):
            row = beacons.register("alpha", session=SID_A, pid=4242,
                                   proc_dir=self.proc)
        self.assertEqual((row["holder_pid"], row["holder_start"]), (90, 555))

    def test_a_path_traversing_seat_name_writes_nothing(self):
        for bad in ("../escape", "a/b", "", "x" * 65):
            self.assertFalse(beacons.valid_seat(bad), bad)
            self.assertIsNone(beacons.register(bad, session=SID_A, pid=1))
        self.assertEqual(glob.glob(os.path.join(beacons.registry_dir(), "*")), [])

    def test_release_drops_the_row(self):
        with mock.patch.object(beacons, "live_sessions", return_value={}):
            beacons.register("alpha", session=SID_A, pid=4242,
                             proc_dir=self.proc)
        self.assertTrue(beacons.release("alpha", 4242))
        self.assertEqual(beacons.entries("alpha"), [])

    def test_case_variants_share_one_registry_key(self):
        with mock.patch.object(beacons, "live_sessions", return_value={}):
            row = beacons.register("Kimi", session=SID_A, pid=4242,
                                   proc_dir=self.proc)
        self.assertEqual(row["seat"], "Kimi")       # display casing survives
        self.assertEqual(beacons._entry_path("Kimi", 4242),
                         beacons._entry_path("kimi", 4242))
        self.assertEqual([r["pid"] for r in beacons.entries("kimi")], [4242])
        self.assertTrue(beacons.release("kImI", 4242))
        self.assertEqual(beacons.entries("KIMI"), [])
    def test_case_variant_release_cleans_a_legacy_raw_cased_path(self):
        """A running pre-upgrade beacon may still own `Kimi.<pid>.json`.
        Discovery and exit cleanup must keep seeing it until it naturally
        re-registers; canonicalizing only new writes would strand that row."""
        os.makedirs(beacons.registry_dir(), exist_ok=True)
        legacy = os.path.join(beacons.registry_dir(), "Kimi.4242.json")
        with open(legacy, "w") as f:
            json.dump({"seat": "Kimi", "pid": 4242, "session": SID_A,
                       "armed": 1.0}, f)
        self.assertEqual([r["pid"] for r in beacons.entries("kimi")], [4242])
        self.assertTrue(beacons.release("KIMI", 4242))
        self.assertFalse(os.path.exists(legacy))
    def test_legacy_duplicate_cannot_shadow_the_canonical_live_row(self):
        """Migration can briefly expose two files for one PID. `entries()` is
        newest-first; a dict comprehension used to overwrite that first row with
        the stale legacy duplicate at all three actuator/census call sites."""
        self.waiter(4242, "kimi", sid=SID_A, starttime=100)
        os.makedirs(beacons.registry_dir(), exist_ok=True)
        canonical = beacons._entry_path("kimi", 4242)
        legacy = os.path.join(beacons.registry_dir(), "Kimi.4242.json")
        rows = ((canonical, {"seat": "kimi", "pid": 4242, "session": SID_A,
                             "starttime": 100, "armed": 200.0,
                             "home": os.environ["HELM_HOME"]}),
                (legacy, {"seat": "Kimi", "pid": 4242, "session": SID_A,
                           "starttime": 999, "armed": 100.0,
                           "home": os.environ["HELM_HOME"]}))
        for path, row in rows:
            with open(path, "w") as f:
                json.dump(row, f)
        chosen = beacons._rows_by_pid("KIMI")[4242]
        self.assertEqual(chosen["_path"], canonical)
        got = beacons.seat_census(
            "Kimi", live={SID_A: LAUNCHER}, proc_dir=self.proc, agents=None)
        self.assertEqual([b["pid"] for b in got["live"]], [4242])
        self.assertTrue(os.path.exists(legacy),
                        "control: the stale duplicate was present for selection")


# ---------------------------------------------------------------------------
# attribution — the gate every signal passes through
# ---------------------------------------------------------------------------

class AttributionTest(Base):
    def test_a_seat_flagged_waiter_in_our_home_is_attributable(self):
        self.waiter(101, "alpha")
        ok, why = beacons.attributable(101, "alpha", self.proc)
        self.assertTrue(ok, why)

    def test_another_seats_waiter_is_never_attributable(self):
        self.waiter(102, "beta")
        ok, why = beacons.attributable(102, "alpha", self.proc)
        self.assertFalse(ok)
        self.assertIn("beta", why)

    def test_a_waiter_in_another_helm_home_is_never_attributable(self):
        """THE CROSS-FLEET GATE. A developer box runs other projects' seats;
        a seat-name collision must not be enough to reach them."""
        self.waiter(103, "alpha", helm_home=os.path.join(self.tmp, "other"))
        ok, why = beacons.attributable(103, "alpha", self.proc)
        self.assertFalse(ok)
        self.assertIn("different helm home", why)

    def test_the_bash_wrapper_is_not_a_waiter(self):
        """#helm 624: reap the PYTHON process, not the grep
        match. The wrapper carries the whole command inside ONE argument."""
        self.plant(104, ["bash", "-c", "helm chat wait --seat alpha --follow"],
                   ["HOME=%s" % self.tmp,
                    "HELM_HOME=%s" % os.environ["HELM_HOME"]])
        self.assertFalse(beacons.is_waiter(104, self.proc))
        ok, why = beacons.attributable(104, "alpha", self.proc)
        self.assertFalse(ok)
        self.assertIn("not a `helm chat wait`", why)

    def test_an_unattributable_waiter_is_never_a_target(self):
        argv = [sys.executable, "/x/bin/helm", "chat", "wait", "--follow"]
        self.plant(105, argv, ["HOME=%s" % self.tmp,
                               "HELM_HOME=%s" % os.environ["HELM_HOME"]])
        ok, why = beacons.attributable(105, "alpha", self.proc)
        self.assertFalse(ok)
        self.assertIn("unattributable", why)

    def test_a_registry_row_can_never_authorize_killing_a_NON_waiter(self):
        """A row is a CLAIM about a pid. If the pid is an agent pane, the shape
        gate refuses before attribution is even consulted."""
        self.plant(106, ["claude", "--dangerously-skip-permissions"],
                   ["HOME=%s" % self.tmp,
                    "HELM_HOME=%s" % os.environ["HELM_HOME"]], starttime=100)
        row = {"seat": "alpha", "pid": 106, "starttime": 100}
        ok, why = beacons.attributable(106, "alpha", self.proc, row=row)
        self.assertFalse(ok)
        self.assertIn("not a `helm chat wait`", why)

    def test_a_registry_row_naming_a_RECYCLED_pid_is_refused(self):
        self.waiter(107, "alpha", starttime=900)
        row = {"seat": "alpha", "pid": 107, "starttime": 100}
        ok, why = beacons.attributable(107, "alpha", self.proc, row=row)
        self.assertFalse(ok)
        self.assertIn("different incarnation", why)

    def test_an_unreadable_environ_is_never_a_target(self):
        d = self.waiter(108, "alpha")
        os.chmod(os.path.join(d, "environ"), 0)
        try:
            ok, why = beacons.attributable(108, "alpha", self.proc)
        finally:
            os.chmod(os.path.join(d, "environ"), 0o644)
        self.assertFalse(ok)
        self.assertIn("environ unreadable", why)


# ---------------------------------------------------------------------------
# the actuator — REAL processes, because a stub cannot prove a signal landed
# ---------------------------------------------------------------------------

def _pids_of(entries):
    """The pids of `(pid, starttime, from_row)` bucket entries."""
    return [e[0] for e in entries]


class NonwakingAdviceTest(Base):
    """The advice printed beside a non-waking incumbent must match WHAT
    ACTUALLY HAPPENED TO IT, and the two authority branches do different
    things. The reporting path stops nobody; the election path routes the same
    refutation into `stop_superseded`, which retires it — or withholds. One
    sentence cannot be true for all three, and the wrong one sends a seat
    hunting a process that is already gone, or reassures it about one that is
    still eating its wakes."""

    def _advice(self, armed):
        from helm import seats_cli, beacons as b
        err = io.StringIO()
        seat = "advice-%d" % os.getpid()
        prior = os.environ.get("HELM_CHAT_NAME")
        os.environ["HELM_CHAT_NAME"] = seat
        try:
            with mock.patch.object(b, "arm", return_value=armed), \
                    mock.patch.object(seats_cli, "wait", return_value=None), \
                    contextlib.redirect_stderr(err):
                seats_cli._cmd_wait(["--seat", seat, "--follow"], "helm")
        finally:
            if prior is None:
                os.environ.pop("HELM_CHAT_NAME", None)
            else:
                os.environ["HELM_CHAT_NAME"] = prior
        return err.getvalue()

    # --- REAL processes, because the render RE-READS fd 1 -----------------
    # The planted /proc of this fixture cannot serve here: the render reads
    # the live /proc, so a record naming pid 4242 describes nothing and every
    # arm built on one silently exercises the not-there branch. Whether a
    # waiter wakes anybody is a property of a REAL open file description.

    def _live(self, sink):
        """A running child whose fd 1 is exactly the sink asked for."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time;time.sleep(30)"], stdout=sink)
        # Cleanups run LIFO, so this registers wait LAST and kill FIRST: the
        # child is signalled and then REAPED, which is what keeps a 30s
        # sleeper from outliving the job that spawned it.
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        if proc.stdout is not None:
            self.addCleanup(proc.stdout.close)
        return proc

    def _exited(self):
        """A pid PROVEN gone, with the start-time it wore while it lived.

        Reaped before the assertion, so /proc has no entry and `pid_alive`
        answers False from evidence rather than from a failed read."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time;time.sleep(30)"],
            stdout=subprocess.DEVNULL)
        start = beacons.proc_starttime(proc.pid)
        proc.kill()
        proc.wait()
        return proc.pid, start

    def _nonwaking(self, proc, **extra):
        rec = {"pid": proc.pid, "sink": beacons.SINK_REFUTES,
               "starttime": beacons.proc_starttime(proc.pid)}
        rec.update(extra)
        return rec

    def test_a_RETIRED_incumbent_is_not_described_as_still_consuming(self):
        out = self._advice({"nonwaking": {"pid": 4242,
                                          "sink": beacons.SINK_REFUTES},
                            "stopped": [4242], "kept": [], "registered": True})
        self.assertIn("4242", out)
        self.assertIn("RETIRED", out)
        self.assertNotIn("still running and still consuming", out,
                         "the election path already stopped it; saying it is "
                         "still consuming sends the seat after a ghost")

    def test_a_WITHHELD_signal_names_the_reason_it_was_withheld(self):
        live = self._live(subprocess.DEVNULL)
        out = self._advice({"nonwaking": self._nonwaking(live),
                            "stopped": [],
                            "kept": [(live.pid, "the sink changed between the "
                                                "decision and the signal — "
                                                "nothing was sent")],
                            "registered": True})
        self.assertIn("WITHHELD", out)
        self.assertIn("sink changed", out,
                      "a withheld signal that does not say WHY is the same "
                      "silence the withholding exists to avoid")
        self.assertIn("still running", out,
                      "this one really is still running, and the seat needs "
                      "to know it now has two waiters")

    def test_the_reporting_path_prescribes_a_MONITOR_not_a_shell_line(self):
        """A bare `helm chat wait` in a terminal cannot re-invoke an agent's
        turn loop, so prescribing one retires a working beacon and replaces it
        with output nobody reads — the exact failure this message is about.

        THIS IS ALSO THE POSITIVE CONTROL for the two arms below. A cure that
        simply stopped making any claim about the sink would satisfy both of
        them and leave the seat with no diagnosis at all; here the re-read
        AGREES with the record, and the present-tense sentence must survive."""
        live = self._live(subprocess.DEVNULL)
        out = self._advice({"nonwaking": self._nonwaking(live),
                            "registered": True})
        self.assertIn("still running and still consuming", out)
        self.assertIn("wakes nobody", out)
        self.assertIn("Monitor(command:", out,
                      "the replacement must be armed the way the original "
                      "was, or the recipe trades a live beacon for a shell")
        self.assertIn("--replace", out)
        self.assertNotIn("EXPIRED", out)

    def test_a_sink_that_became_admissible_is_reported_as_EXPIRED(self):
        """THE RECORD IS A PAST OBSERVATION AND THE MESSAGE IS PRESENT TENSE.

        Between the pass and this line a waiter can dup2 a live pipe over its
        fd 1. The stop pass already withholds on exactly that change — so
        the decision treats the reading as invalidated while the sentence
        explaining it went on publishing it as current fact. Acting on it
        retires the one process that IS waking the seat."""
        live = self._live(subprocess.PIPE)
        out = self._advice({"nonwaking": self._nonwaking(live),
                            "registered": True})
        self.assertIn("EXPIRED", out)
        self.assertNotIn("consumes addressed rows and wakes nobody", out,
                         "fd 1 re-reads as admissible; repeating the stale "
                         "verdict sends the seat after its own waker")

    def test_a_fresh_reading_governs_EVERY_clause_not_just_the_sink(self):
        """THE MESSAGE MAKES THREE CLAIMS AND THEY MUST AGREE.

        It says what the waiter's sink is, whether it is still running and
        consuming, and whether this seat is now reached by anything else.
        Re-reading only the first left the other two speaking from the
        record, so the line could report that the reason to retire it had
        EXPIRED and that it may well be waking the seat, and in the same
        breath that it is still consuming and that only this one wakes the
        seat. A reader cannot tell which clause to believe, which is worse
        than either error alone."""
        live = self._live(subprocess.PIPE)
        out = self._advice({"nonwaking": self._nonwaking(live),
                            "registered": True})
        self.assertIn("EXPIRED", out)
        self.assertNotIn("only this one wakes it", out,
                         "its sink re-reads as admissible, so this is NOT "
                         "the only waiter reaching the seat")
        self.assertNotIn("still running and still consuming", out,
                         "the sink clause says its output reaches a reader "
                         "and this says it consumes silently")

    def test_an_UNREADABLE_process_does_not_also_claim_it_is_running(self):
        """UNKNOWN is an answer, and it has to be the answer EVERYWHERE.

        When liveness cannot be read at all, the sink clause said so and the
        tail beside it went on asserting "still running and still consuming"
        from the record -- a confident claim standing next to its own
        admission of ignorance."""
        live = self._live(subprocess.DEVNULL)
        rec = self._nonwaking(live)
        with mock.patch.object(beacons, "pid_alive", return_value=None):
            out = self._advice({"nonwaking": rec, "registered": True})
        self.assertIn("UNKNOWN", out)
        self.assertNotIn("still running and still consuming", out,
                         "liveness is unreadable, so nothing here may assert "
                         "that it is running")
        self.assertNotIn("--replace", out,
                         "no retirement is prescribed against a pid this "
                         "pass cannot see")

    def test_an_ADMISSIBLE_sink_is_never_rendered_as_REACHING_A_READER(self):
        """ADMISSIBLE IS THE ABSENCE OF A REFUTATION, NOT A DELIVERY PROOF.

        A pipe whose read ends are ALL CLOSED answers ADMISSIBLE and fails
        every write to it with EPIPE, so "its output now reaches a reader" is
        a claim this pass cannot support -- and it was being printed on both
        the reporting and the WITHHELD branch. The strongest true sentence is
        that the reason to retire it has expired."""
        live = self._live(subprocess.PIPE)
        live.stdout.close()          # the only reader is gone; fd 1 unchanged
        out = self._advice({"nonwaking": self._nonwaking(live),
                            "registered": True})
        self.assertIn("EXPIRED", out,
                      "MUST-HIT: the sink really did re-read as admissible")
        self.assertNotIn("reaches a reader", out,
                         "a pipe with no reader left answers ADMISSIBLE too")
        self.assertNotIn("now reaches", out)

    def test_a_waiter_that_has_since_EXITED_gets_no_retirement_recipe(self):
        """Liveness is re-read too, and BOTH sentences must honour it.

        The tail carried its own present-tense claim one sentence from the
        sink clause, so curing only the sink would have left the message
        saying GONE and `still running and still consuming` together. And a
        recipe to re-arm with --replace against a pid that no longer exists
        spends a live beacon to retire nothing."""
        pid, start = self._exited()
        out = self._advice({"nonwaking": {"pid": pid,
                                          "sink": beacons.SINK_REFUTES,
                                          "starttime": start},
                            "registered": True})
        self.assertIn("GONE", out)
        self.assertNotIn("still running and still consuming", out,
                         "the same message cannot call it gone and running")
        self.assertNotIn("--replace", out,
                         "there is nothing left to replace")


class ReArmTest(Base):
    """Stop-then-start, proven against genuine `helm chat wait`-shaped
    processes. The seat name is unique to this test run and HELM_HOME is a tmp
    dir, so the live fleet is out of reach by attribution AND by the home
    gate."""

    def setUp(self):
        super().setUp()
        self.seat = "beaconfix-%d" % os.getpid()
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin, exist_ok=True)
        self.script = os.path.join(self.bin, "helm")   # basename IS the marker
        with open(self.script, "w") as f:
            f.write("import time\nwhile True: time.sleep(0.2)\n")
        self.kids = []

    def tearDown(self):
        for p in self.kids:
            try:
                p.kill()
                p.wait(timeout=5)
            except Exception:                          # noqa: BLE001
                pass
        super().tearDown()

    def spawn(self, seat=None, sid=SID_A, helm_home=None, shape="waiter",
              flags=(), sink=subprocess.PIPE, script=None):
        """A PIPE BY DEFAULT, BECAUSE /dev/null IS NOT WHAT A BEACON LOOKS
        LIKE. A real armed waiter's stdout is a socket or pipe to the harness;
        a fixture that spawned every waiter onto /dev/null was modelling the
        one shape production now classifies as NON-WAKING, so every arm here
        would have exercised the repair path instead of the ordinary one.

        These children print nothing and sleep, so an unread pipe never
        fills."""
        env = dict(os.environ)
        env["HELM_HOME"] = helm_home or os.environ["HELM_HOME"]
        env["HOME"] = self.tmp
        env["HELM_CHAT_NAME"] = seat or self.seat
        env["CLAUDE_CODE_SESSION_ID"] = sid
        argv = [sys.executable, script or self.script]
        if shape == "waiter":
            argv += ["chat", "wait", "--seat", seat or self.seat, "--follow"]
            argv += list(flags)
        else:
            argv += ["chat", "post", "hello"]          # a helm proc, not a beacon
        p = subprocess.Popen(argv, env=env, stdout=sink,
                             stderr=subprocess.DEVNULL)
        self.kids.append(p)
        self.wait_visible(p.pid, argv)
        return p

    def wait_visible(self, pid, argv, deadline=5.0):
        """Wait until the child has EXECed — /proc briefly shows the forking
        parent's argv, and scanning inside that window would measure this test
        runner instead of the fixture."""
        end = time.time() + deadline
        while time.time() < end:
            if beacons.proc_argv(pid) == argv:
                return
            time.sleep(0.01)
        self.fail("fixture pid %d never exec'd %r" % (pid, argv))

    def dead(self, proc, deadline=8.0):
        end = time.time() + deadline
        while time.time() < end:
            if proc.poll() is not None:
                return True
            time.sleep(0.05)
        return False

    def wait_paths(self, pattern, count, deadline=8.0):
        end = time.time() + deadline
        while time.time() < end:
            paths = glob.glob(pattern)
            if len(paths) >= count:
                return paths
            time.sleep(0.02)
        self.fail("only %d/%d paths appeared for %s" % (
            len(glob.glob(pattern)), count, pattern))

    def test_ordinary_arm_reuses_one_live_same_session_waiter(self):
        """The incident: a normal Monitor wake was followed by another bare arm,
        which SIGTERMed the healthy incumbent and surfaced exit 143. Exactly one
        attributable LIVE waiter for this seat+session is already the requested
        state, so the second process must neither signal nor replace its row."""
        old, new = self.spawn(), self.spawn()
        with mock.patch.object(beacons, "live_sessions",
                               return_value={SID_A: os.getpid()}):
            self.assertTrue(beacons.register(self.seat, SID_A, old.pid))
            report = beacons.arm(self.seat, session=SID_A, pid=new.pid)
        self.assertEqual(report["already_live"]["pid"], old.pid)
        self.assertEqual(report["already_live"]["session"], SID_A)
        self.assertEqual(report["stopped"], [])
        self.assertFalse(report["registered"])
        self.assertIsNone(old.poll(), "ordinary duplicate killed the incumbent")
        self.assertEqual([r["pid"] for r in beacons.entries(self.seat)],
                         [old.pid])
    def test_ordinary_arm_reuses_an_unregistered_live_same_session_waiter(self):
        """The registry is corroboration, not existence: inherited/pre-registry
        waiters still compose into the idempotent state through argv+env+session
        attribution, rather than being killed merely because no row names them."""
        old, new = self.spawn(), self.spawn()
        with mock.patch.object(beacons, "live_sessions",
                               return_value={SID_A: os.getpid()}):
            report = beacons.arm(self.seat, session=SID_A, pid=new.pid)
        self.assertEqual(report["already_live"]["pid"], old.pid)
        self.assertFalse(report["already_live"]["registered"])
        self.assertEqual(report["stopped"], [])
        self.assertIsNone(old.poll())
        self.assertEqual(beacons.entries(self.seat), [])
    def test_an_incumbent_on_a_PIPE_reports_an_ADMISSIBLE_sink(self):
        """THE POSITIVE POLE of a review's R1, measured on a REAL process.

        A waiter whose stdout is a pipe has a far end this side cannot see, so
        the sink is ADMISSIBLE — not proven live, merely not proven dead. That
        is the strongest thing fd 1 can say and the banner says exactly it."""
        old = self.spawn(sink=subprocess.PIPE)
        new = self.spawn()
        with mock.patch.object(beacons, "live_sessions",
                               return_value={SID_A: os.getpid()}):
            self.assertTrue(beacons.register(self.seat, SID_A, old.pid))
            report = beacons.arm(self.seat, session=SID_A, pid=new.pid)
        self.assertEqual(report["already_live"]["pid"], old.pid)
        self.assertEqual(report["already_live"]["sink"], beacons.SINK_ADMISSIBLE)

    def test_an_incumbent_REDIRECTED_TO_A_FILE_reports_a_REFUTED_sink(self):
        """THE DEFECT ITSELF, as a live process. This waiter is LIVE, carries
        --follow, is not --any, and serves this exact seat and session: every
        field the coverage predicate read before this cure agrees it is a
        beacon. Its stdout is a regular file, so it consumes addressed rows and
        wakes nobody, forever — and the banner it produced told the seat to
        re-arm only if it DIED."""
        path = os.path.join(self.tmp, "swallowed.log")
        with open(path, "w") as fh:
            old = self.spawn(sink=fh)
            new = self.spawn()
            with mock.patch.object(beacons, "live_sessions",
                                   return_value={SID_A: os.getpid()}):
                self.assertTrue(beacons.register(self.seat, SID_A, old.pid))
                report = beacons.arm(self.seat, session=SID_A, pid=new.pid)
        # IT IS NOT already_live, AND THAT IS THE REPAIR HALF OF THE CURE.
        # This waiter's spec is identical to the requested one, so equivalence
        # alone called it an incumbent and exited the newcomer cleanly — a
        # seat told to re-arm would re-arm, the re-arm would exit, and the
        # seat would stay deaf with every surface reporting it covered.
        self.assertIsNone(report.get("already_live"),
                          "a waiter on a non-waking sink must not be treated "
                          "as the live incumbent")
        self.assertIsNone(report.get("conflict"))
        self.assertEqual(report["nonwaking"]["pid"], old.pid)
        self.assertEqual(report["nonwaking"]["sink"], beacons.SINK_REFUTES)
        # AND THE NEW WAITER SURVIVED, which is the whole point: the seat has
        # a live wake path again rather than a clean exit code.
        self.assertIsNone(new.poll(), "the newcomer was exited anyway")

    def test_the_two_sink_poles_are_ARGV_IDENTICAL(self):
        """THE ARM THAT MAKES THE OTHER TWO MEAN ANYTHING. If the file-sunk and
        pipe-sunk waiters differed in argv, the two results above could be
        explained by `waiter_spec` alone and would say nothing about fd 1.

        No `arm` call here on purpose: this asks only what the spec producer
        sees, and a second live waiter would break the exactly-one election the
        other arms depend on."""
        with open(os.path.join(self.tmp, "a.log"), "w") as fh:
            filed = self.spawn(sink=fh)
            piped = self.spawn(sink=subprocess.PIPE)
            self.assertEqual(beacons.proc_argv(filed.pid),
                             beacons.proc_argv(piped.pid))
            self.assertEqual(beacons.waiter_spec(filed.pid),
                             beacons.waiter_spec(piped.pid))
            self.assertNotEqual(beacons.sink_state(filed.pid),
                                beacons.sink_state(piped.pid),
                                "argv-identical waiters MUST differ on the sink")

    def test_sink_state_classifies_REAL_processes_not_invented_strings(self):
        """THE CLASSIFIER AGAINST THE REAL /proc SURFACE. Every other arm here
        hands `sink_state` a fixture; this one hands it live pids and asserts
        the three answers it is allowed to give.

        The UNKNOWN pole is the one that matters most: it must NOT collapse
        into REFUTES, because the caller requires ADMISSIBLE and an unreadable
        sink therefore keeps the arming directive."""
        with open(os.path.join(self.tmp, "sink.txt"), "w") as fh:
            to_file = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"],
                                       stdout=fh)
        self.kids.append(to_file)
        to_null = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"],
                                   stdout=subprocess.DEVNULL)
        self.kids.append(to_null)
        to_pipe = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"],
                                   stdout=subprocess.PIPE)
        self.kids.append(to_pipe)
        self.assertEqual(beacons.sink_state(to_file.pid), beacons.SINK_REFUTES)
        self.assertEqual(beacons.sink_state(to_null.pid), beacons.SINK_REFUTES)
        self.assertEqual(beacons.sink_state(to_pipe.pid), beacons.SINK_ADMISSIBLE)
        # a pid with no /proc entry at all — unreadable, never "refuted"
        self.assertEqual(beacons.sink_state(2 ** 31 - 1), beacons.SINK_UNKNOWN)

    def test_the_sink_reads_the_FD_OBJECT_not_the_pathname_it_points_at(self):
        """The R1 residual. `readlink` hands back a PATH, and a path is
        not the open file description a waiter is holding.

        TWO WAYS THE NAME LIES, and both were live before this. UNLINK the
        file and the link reads "<path> (deleted)", which stats ENOENT — so a
        waiter genuinely writing into a discarded file answered UNKNOWN and
        kept its incumbency. LET SOMETHING ELSE TAKE THE NAME and the stat
        describes the other object entirely.

        IT MATTERS MORE THAN AN ORDINARY MISREAD because this classification
        decides whether `arm` treats an incumbent as non-waking, and on the
        election path that reaches `stop_superseded`. A judgement that ends in
        a signal must not be reading names."""
        path = os.path.join(self.tmp, "sunk.log")
        with open(path, "w") as fh:
            child = subprocess.Popen(
                [sys.executable, "-c", "import time;time.sleep(30)"],
                stdout=fh)
        self.kids.append(child)
        # UNCONDITIONAL POSITIVE on the same observable, so the assertion below
        # cannot pass against a classifier that answers REFUTES for everything.
        self.assertEqual(beacons.sink_state(child.pid), beacons.SINK_REFUTES)
        alive = subprocess.Popen(
            [sys.executable, "-c", "import time;time.sleep(30)"],
            stdout=subprocess.PIPE)
        self.kids.append(alive)
        self.assertEqual(beacons.sink_state(alive.pid), beacons.SINK_ADMISSIBLE)

        # THE NAME NOW LIES: the file is gone, the fd still holds it open.
        os.unlink(path)
        self.assertIn("(deleted)", beacons.waiter_sink(child.pid),
                      "MUST-HIT: the pathname did not go stale, so this arm "
                      "is not exercising the divergence it is named for")
        self.assertEqual(
            beacons.sink_state(child.pid), beacons.SINK_REFUTES,
            "a waiter writing into a DELETED file still wakes nobody; reading "
            "the pathname answered UNKNOWN and let it keep its incumbency")

    def test_the_discard_is_named_by_the_PLATFORM_not_by_a_reference_path(self):
        """Identifying the discard by stat'ing a reference path fails in BOTH
        directions, and the two failures have opposite signs, which is why one
        control cannot catch them.

        A REFERENCE THAT CANNOT BE READ becomes coverage: the comparison
        answers "not null", so a waiter genuinely throwing its output away is
        called ADMISSIBLE and keeps its incumbency. AN IMPOSTOR REFERENCE
        becomes authority: if the path names something else, an ordinary
        character device holding that endpoint answers REFUTES, and REFUTES is
        what reaches `stop_superseded` and signals a process.

        /dev/null is character device (1, 3) by the kernel's registry, and a
        device number is global to the kernel rather than to a mount
        namespace, so nothing needs to be stat'ed to recognise it. THESE ARMS
        PROVE THE REFERENCE IS NOT CONSULTED AT ALL: `os.devnull` is pointed
        at a path that does not exist and then at a regular-file impostor, and
        the answers do not move."""
        to_null = subprocess.Popen(
            [sys.executable, "-c", "import time;time.sleep(30)"],
            stdout=subprocess.DEVNULL)
        self.kids.append(to_null)
        to_pipe = subprocess.Popen(
            [sys.executable, "-c", "import time;time.sleep(30)"],
            stdout=subprocess.PIPE)
        self.kids.append(to_pipe)
        # UNPERTURBED CONTROL FIRST, so a classifier that answered the same
        # thing regardless could not satisfy the arms below.
        self.assertEqual(beacons.sink_state(to_null.pid), beacons.SINK_REFUTES)
        self.assertEqual(beacons.sink_state(to_pipe.pid),
                         beacons.SINK_ADMISSIBLE)

        impostor = os.path.join(self.tmp, "not-really-null")
        with open(impostor, "w") as fh:
            fh.write("x\n")
        for reference in (os.path.join(self.tmp, "nonexistent", "null"),
                          impostor):
            with mock.patch.object(os, "devnull", reference):
                self.assertEqual(
                    beacons.sink_state(to_null.pid), beacons.SINK_REFUTES,
                    "a real discard stays REFUTED with os.devnull at %r — an "
                    "unreadable or impostor reference must never turn a dead "
                    "sink into coverage" % reference)
                self.assertEqual(
                    beacons.sink_state(to_pipe.pid), beacons.SINK_ADMISSIBLE,
                    "a pipe stays ADMISSIBLE with os.devnull at %r — a "
                    "reference must never become authority to signal"
                    % reference)

    def test_a_character_device_that_is_not_null_is_ADMISSIBLE(self):
        """THE DEVICE BRANCH NEEDS A NON-NULL CHARACTER CONTROL, or the
        /dev/null arm is satisfied by any reader that refuses character
        devices as a class. /dev/zero is character (1, 5): it is not the
        discard, so it falls through.

        This is the same bound the docstring states for a tty. A pane cannot
        re-invoke a turn loop either, so a terminal is arguably non-waking
        too — but this function refuses only what it can PROVE reaches no
        reader, and only (1, 3) is that."""
        with open("/dev/zero", "w") as fh:
            st = os.fstat(fh.fileno())
            self.assertTrue(stat.S_ISCHR(st.st_mode),
                            "MUST-HIT: /dev/zero is not a character device on "
                            "this host, so this arm proves nothing")
            self.assertNotEqual(st.st_rdev, os.makedev(1, 3))
            child = subprocess.Popen(
                [sys.executable, "-c", "import time;time.sleep(30)"],
                stdout=fh)
        self.kids.append(child)
        self.assertEqual(
            beacons.sink_state(child.pid), beacons.SINK_ADMISSIBLE,
            "only (1, 3) is the discard; refusing character devices as a "
            "class would refute every pane-backed waiter too")

    def _switching_script(self):
        """A waiter-shaped child that REPLACES ITS OWN fd 1 on command.

        Same pid, same start time, same argv — the three things every other
        check in this file pins — and a different object on fd 1. That is the
        exact shape of the race, and it is deterministic here rather than
        scheduled."""
        bin2 = os.path.join(self.tmp, "switchbin")
        os.makedirs(bin2, exist_ok=True)
        path = os.path.join(bin2, "helm")          # basename IS the marker
        with open(path, "w") as fh:
            fh.write(
                "import os, sys, time\n"
                "trigger = os.environ['SINK_SWITCH']\n"
                "while not os.path.exists(trigger):\n"
                "    time.sleep(0.02)\n"
                "r, w = os.pipe()\n"
                "os.dup2(w, 1)\n"
                "os.close(w)\n"
                "open(trigger + '.done', 'w').close()\n"
                "while True: time.sleep(0.2)\n")
        return path

    def test_a_SINK_DRIVEN_candidate_is_REPORTED_and_never_signalled(self):
        """THE CONTRACT THIS LANE DELIBERATELY INVERTS, and the inversion is
        the cure rather than a loosened assertion.

        A SINK READING DOES NOT AUTHORIZE A SIGNAL. Closing the window between
        that reading and a signal would need proof this pass OWNS the stop it
        observes, and the kernel offers none: a debugger or job-control holder
        can stop the pid between the read and any SIGSTOP, so a SIGCONT
        afterwards resumes a stop this pass never established. A pidfd pins the
        INCARNATION and confers no stop OWNERSHIP.

        So the candidate is REPORTED and the path has ONE behaviour, which is
        why one arm covers it. What makes that affordable is the delivery
        fence: a non-waking consumer cannot take an addressed row out of the
        ledger and discard it, so a surplus one is merely surplus, and
        --replace retires it deliberately."""
        seat = self.seat + "-steady"
        sink_path = os.path.join(self.tmp, "steady.log")
        with open(sink_path, "w") as fh:
            child = self.spawn(seat=seat, sink=fh)
        self.assertEqual(beacons.sink_state(child.pid), beacons.SINK_REFUTES)
        decided = beacons.sink_identity(child.pid)
        report = beacons.stop_superseded(seat, keep_pid=os.getpid(),
                                         expect_sinks={child.pid: decided})
        self.assertNotIn(child.pid, report["stopped"],
                         "a sink reading alone must no longer authorize a "
                         "signal: %r" % report)
        why = dict((p, w) for p, w in report["kept"]).get(child.pid)
        self.assertIsNotNone(why, "a withheld candidate must be REPORTED, "
                                  "never silently skipped: %r" % report)
        self.assertIn("--replace", why,
                      "the report must name the door that DOES retire it")
        self.assertIsNone(child.poll(), "the waiter must still be alive")

    def test_the_incarnation_is_captured_BEFORE_the_attribution_it_guards(self):
        """THE BRACKET MUST SPAN THE DECISION, NOT TRAIL IT.

        The cure this supersedes called `attributable` and only THEN read
        the start time it would recheck against. That leaves the attribution
        itself outside the guard: the scan proves things about W1, W1 exits,
        the pid is reused by W2, and a start time first read AFTER the reuse
        agrees with every later recheck. The bracket then certifies W2 on the
        strength of evidence gathered about W1 — self-consistent, and anchored
        to nothing older than the recycle it is supposed to catch.

        THE WINDOW IS DRIVEN HERE, not described. The pid's start time is made
        to change DURING the attribution — which is exactly a reuse landing in
        that gap — and the pass must withhold. Reading the incarnation first
        makes every recheck span the attribution too, so the change is seen."""
        seat = self.seat + "-midscan"
        sink_path = os.path.join(self.tmp, "midscan.log")
        with open(sink_path, "w") as fh:
            child = self.spawn(seat=seat, sink=fh)
        beacons.register(seat, session=SID_A, pid=child.pid)
        real = beacons.proc_starttime(child.pid)
        self.assertIsNotNone(real, "fixture: the child has no start time")

        recycled = {"yet": False}
        real_attr = beacons.attributable
        real_start = beacons.proc_starttime

        def starttime(pid, proc_dir=None):
            got = real_start(pid, proc_dir)
            if pid == child.pid and recycled["yet"] and got is not None:
                return got + 1          # a different incarnation on this pid
            return got

        def attributable(pid, *a, **kw):
            out = real_attr(pid, *a, **kw)
            if pid == child.pid:
                recycled["yet"] = True  # the reuse lands inside this call
            return out

        with mock.patch.object(beacons, "proc_starttime", starttime), \
                mock.patch.object(beacons, "attributable", attributable):
            report = beacons.stop_superseded(seat, keep_pid=os.getpid())

        self.assertTrue(recycled["yet"],
                        "MUST-HIT: attribution never ran for this pid, so the "
                        "window this arm drives was never opened")
        self.assertNotIn(
            child.pid, report["stopped"],
            "the pid changed incarnation while it was being attributed; "
            "signalling it sends W1's verdict to W2: %r" % (report,))
        self.assertIsNone(child.poll(),
                          "the waiter wearing the recycled number must live")

        # THE PAIRED POLE, ON THE SAME OBSERVABLE. Without it this arm is
        # satisfied by a pass that stops nothing at all, and "not in stopped"
        # would be true for a reason that has nothing to do with incarnations.
        # Identical fixture, identical call, the reuse simply never happens.
        steady_seat = self.seat + "-midscan-control"
        steady_path = os.path.join(self.tmp, "midscan-control.log")
        with open(steady_path, "w") as fh:
            steady = self.spawn(seat=steady_seat, sink=fh)
        beacons.register(steady_seat, session=SID_A, pid=steady.pid)
        control = beacons.stop_superseded(steady_seat, keep_pid=os.getpid())
        self.assertIn(steady.pid, control["stopped"],
                      "CONTROL: an unrecycled waiter must be retired, or the "
                      "withholding above proves nothing: %r" % (control,))

    def test_a_pidfd_that_REFUSES_THIS_PID_withholds_instead_of_signalling(self):
        """TWO DIFFERENT NOTHINGS, AND THE OLD CODE COLLAPSED THEM.

        `_process_handle` returned None both when the PLATFORM has no pidfd —
        where a bare-pid send is merely the old behaviour — and when the kernel
        refused THIS PID. The second is the case the handle exists for: ESRCH
        is the kernel saying the process is already gone, and answering it with
        `os.kill(pid)` sends the signal to whoever holds the number next.

        So the evidence that a recycle is underway drove the exact send the
        design was written to prevent. A per-pid refusal now withholds."""
        seat = self.seat + "-nohandle"
        sink_path = os.path.join(self.tmp, "nohandle.log")
        with open(sink_path, "w") as fh:
            child = self.spawn(seat=seat, sink=fh)
        beacons.register(seat, session=SID_A, pid=child.pid)
        decided = beacons.sink_identity(child.pid)
        self.assertIsNotNone(decided, "fixture: no sink token to decide on")

        killed = []
        real_kill = os.kill

        def spy(pid, sig, *a):
            killed.append((pid, sig))
            return real_kill(pid, sig, *a)

        def refuse(pid, *a, **kw):
            raise OSError(errno.ESRCH, "No such process")

        # NO expect_sinks: a sink-driven candidate is now withheld before the
        # handle is ever opened, so driving this arm that way would exercise
        # the sink rule and not the handle rule it is named for.
        with mock.patch.object(os, "pidfd_open", refuse), \
                mock.patch.object(os, "kill", spy):
            report = beacons.stop_superseded(seat, keep_pid=os.getpid())

        self.assertNotIn(child.pid, report["stopped"], repr(report))
        self.assertNotIn(
            child.pid, [pid for pid, _sig in killed],
            "the kernel refused a handle for this pid and the pass signalled "
            "the NUMBER anyway — that is the recycle the handle prevents")
        why = dict((pid, w) for pid, w in report["kept"]).get(child.pid)
        self.assertIsNotNone(why, "a withheld signal must be REPORTED: %r"
                             % (report,))
        self.assertIn("no kernel handle", why)
        self.assertIsNone(child.poll(), "the waiter must still be alive")

        # THE PAIRED POLE, ON THE SAME OBSERVABLE: the identical call with the
        # kernel answering normally must retire the same shape of waiter.
        # Without it, "not stopped" is satisfied by a pass that never signals.
        ok_seat = self.seat + "-nohandle-control"
        ok_path = os.path.join(self.tmp, "nohandle-control.log")
        with open(ok_path, "w") as fh:
            ok_child = self.spawn(seat=ok_seat, sink=fh)
        beacons.register(ok_seat, session=SID_A, pid=ok_child.pid)
        control = beacons.stop_superseded(ok_seat, keep_pid=os.getpid())
        self.assertIn(ok_child.pid, control["stopped"],
                      "CONTROL: with a handle available the same waiter must "
                      "be retired, or the withholding above proves nothing: "
                      "%r" % (control,))

    def test_a_row_with_NO_start_time_cannot_authorize_a_signal(self):
        """AN ABSENT READING IS UNKNOWN, AND UNKNOWN WITHHOLDS.

        The registry row is the one artifact that can name the incarnation a
        retirement decision was made about. A row that records NO start time
        has forgotten which incarnation it meant — and a cross-check written
        as "withhold when they DISAGREE" reads that silence as agreement, so
        the row authorizes the signal exactly where it proves least."""
        seat = self.seat + "-nostart"
        sink_path = os.path.join(self.tmp, "nostart.log")
        with open(sink_path, "w") as fh:
            child = self.spawn(seat=seat, sink=fh)
        with mock.patch.object(beacons, "proc_starttime", return_value=None):
            self.assertTrue(beacons.register(seat, session=SID_A,
                                             pid=child.pid),
                            "fixture: the row must really be written")
        rows = beacons._rows_by_pid(seat)
        self.assertIsNone(rows[child.pid].get("starttime"),
                          "MUST-HIT: the row carries a start time, so this "
                          "arm is not exercising the null case")

        report = beacons.stop_superseded(seat, keep_pid=os.getpid())
        self.assertNotIn(child.pid, report["stopped"], repr(report))
        why = dict((p, w) for p, w in report["kept"]).get(child.pid)
        self.assertIsNotNone(why, repr(report))
        self.assertIn("no start time", why)
        self.assertIsNone(child.poll())

    def test_a_STALE_ROW_never_decides_the_liveness_of_a_REUSED_pid(self):
        """THE UNION IS KEYED BY INCARNATION, NOT BY NUMBER.

        A stale row for pid P and a LIVE waiter that now wears pid P are two
        different processes. Keyed on the number alone, `pid_alive` is asked
        about the live one using the DEAD one's start time, answers False, and
        renders a running waiter PROVEN GONE.

        THE CONTROL IS THE TELL A PROBE MEASURED: deleting the stale row
        flips the same process back to LIVE. A row that changes its answer
        about a process it does not describe is deciding the liveness of a
        stranger, so both halves are asserted.

        THIS DRIVES `_alongside_buckets` DIRECTLY, and that is the point of
        its extraction. Through `arm` the stale row is pruned upstream before
        the advice ever runs, so a fixture built this way could not reach the
        collision — measured, a mutation of the keying left the answer through
        that door IDENTICAL, which is an inert mutation wearing the look of a
        passing arm."""
        seat = self.seat + "-reused"
        sink_path = os.path.join(self.tmp, "reused.log")
        with open(sink_path, "w") as fh:
            child = self.spawn(seat=seat, sink=fh)
        real = beacons.proc_starttime(child.pid)
        self.assertIsNotNone(real)
        # MUST-HIT: the scan really does see this pid, or the collision this
        # arm is about never arises and every assertion below is free.
        self.assertIn(child.pid, set(beacons._scan(seat) or ()),
                      "MUST-HIT: the process scan does not see the child")

        stale = {child.pid: {"starttime": real - 5000, "seat": seat}}
        live, dead, unknown, stale_rows = beacons._alongside_buckets(
            seat, os.getpid(), stale)
        self.assertIn(child.pid, _pids_of(live),
                      "a LIVE waiter was not reported live because a stale "
                      "row on the same pid supplied a dead incarnation")
        self.assertIn(child.pid, stale_rows,
                      "the stale ROW is real and must still be reported")

        # THE CONTROL: without the row the same process must read the same.
        live2, _d2, _u2, stale2 = beacons._alongside_buckets(
            seat, os.getpid(), {})
        self.assertIn(child.pid, _pids_of(live2))
        self.assertEqual(stale2, [],
                         "no row was supplied, so nothing can be a stale row")
        self.assertEqual(
            child.pid in _pids_of(live), child.pid in _pids_of(live2),
            "removing the stale row changed the verdict about a process the "
            "row never described")
        self.assertIsNone(child.poll())

    def test_an_UNREADABLE_incarnation_does_not_take_the_WHOLE_PASS_down(self):
        """THE PRODUCTION RENDERER, not the helper, and it is the difference
        that matters here.

        Carrying `(pid, starttime, from_row)` to the surface fixed a rendering
        defect and introduced a far worse one: the starttime is None whenever
        /proc could not be read, and sorting the entries as TUPLES reaches
        that field the moment two share a pid -- exactly the collision the
        entries exist to represent. Python raises comparing None with an int,
        `_cmd_wait` swallows it, and the pass then registers NOTHING and
        prints NOTHING. A cure for a cosmetic defect took the whole arm down.

        THE FIXTURE MAKES THE ROW SURVIVE. A stale row whose process is PROVEN
        gone is pruned before the advice runs, which collapses the collision
        before the sort can see it -- a proven-gone row cannot reach the
        defect. An UNREADABLE stat is present-but-unparseable, so `pid_alive` answers
        UNKNOWN, nothing is pruned, and both incarnations reach the sort."""
        seat = self.seat + "-unreadable"
        pid = 4242
        d = self.plant(pid, [sys.executable, "helm", "chat", "wait"],
                       ["HELM_CHAT_NAME=%s" % seat])
        statp = os.path.join(d, "stat")
        os.chmod(statp, 0)
        os.makedirs(beacons.registry_dir(), exist_ok=True)
        with open(beacons._entry_path(seat, pid), "w") as f:
            json.dump({"seat": seat, "pid": pid, "session": "s",
                       "starttime": 100, "armed": 1.0,
                       "home": os.environ["HELM_HOME"]}, f)
        try:
            with mock.patch.object(beacons, "_scan", return_value={pid}):
                out = beacons.arm(seat, session="s", pid=os.getpid(),
                                  proc_dir=self.proc, reap=False)
        finally:
            # RESTORED INLINE, not via addCleanup: cleanups run AFTER
            # tearDown has already removed the tree, so a chmod registered
            # there fails on a path that no longer exists.
            os.chmod(statp, 0o644)
        self.assertIsNotNone(out, "the pass died instead of reporting")
        self.assertIn("ALONGSIDE", out.get("alongside") or "",
                      "MUST-HIT: the collision really did reach the renderer, "
                      "or this arm passes for the wrong reason")
        self.assertIn(str(pid), out["alongside"])

    def test_two_dead_incarnations_of_one_pid_keep_their_SOURCES(self):
        """A pid can be dead TWICE over, and the two halves send a reader to
        different places.

        A stale REGISTRY ROW for pid P is an artifact somebody can prune. An
        exited process the SCAN saw at pid P never had a row, so announcing it
        as a stale row invents an artifact to go and clean up. The buckets
        distinguished the two incarnations by `(pid, starttime)` and then
        returned BARE PIDS, so the renderer had to recover the source by
        asking `p in rows` -- the same pid-keyed question the keying exists to
        stop asking. Both answered yes, and one number was announced twice as
        a stale registry row.

        THE ASSERTION IS ON THE ENTRIES, not on the rendered sentence, because
        the renderer's split is `e[2]` and a test that re-derives that split
        would be asking the code under test what its own answer means."""
        seat = self.seat + "-twice-dead"
        gone = 4242
        # /proc holds nothing for this pid, so every incarnation of it is
        # PROVEN gone; the scan is doubled to produce the collision, which is
        # the one thing a planted tree cannot stage on its own.
        with mock.patch.object(beacons, "_scan", return_value={gone}):
            _live, dead, _unknown, stale_rows = beacons._alongside_buckets(
                seat, os.getpid(), {gone: {"starttime": 100, "seat": seat}},
                proc_dir=self.proc)
        same = [e for e in dead if e[0] == gone]
        self.assertEqual(2, len(same),
                         "one pid, two dead incarnations -- the row's and the "
                         "scan's -- must both survive to the surface: %r"
                         % (dead,))
        self.assertEqual(
            [True, False], sorted((e[2] for e in same), reverse=True),
            "exactly one of them came from a registry row and one from the "
            "scan, and collapsing that is what invented an artifact: %r"
            % (same,))
        self.assertEqual([gone], stale_rows,
                         "the ROW is the only prunable artifact here")

        # THE NO-ROW CONTROL: with no row supplied the same scanned pid is
        # never a stale row, so the arm above cannot pass by calling
        # everything a row.
        with mock.patch.object(beacons, "_scan", return_value={gone}):
            _l2, dead2, _u2, stale2 = beacons._alongside_buckets(
                seat, os.getpid(), {}, proc_dir=self.proc)
        self.assertEqual([], stale2,
                         "nothing was registered, so nothing is prunable")
        self.assertEqual([False], [e[2] for e in dead2 if e[0] == gone],
                         "a scan-only pid must never be sourced to a row")

    def test_two_concurrent_first_arms_commit_exactly_one_waiter(self):
        """Two fresh launchers used to see each other as the sole incumbent and
        both exit. The child probe freezes each decision before either can leave:
        pre-fix both decisions name the peer; with the election, the first lock
        holder sees the other's uncommitted marker, commits, and the second then
        defers to that READY row."""
        race = os.path.join(self.tmp, "race")
        for name in ("ready", "pending", "probe", "result", "bin"):
            os.makedirs(os.path.join(race, name))
        script = os.path.join(race, "bin", "helm")
        with open(script, "w") as f:
            f.write("""import glob, json, os, signal, time
from helm import beacons
root = os.environ['RACE_ROOT']
seat = os.environ['RACE_SEAT']
sid = os.environ['RACE_SID']
pid = os.getpid()
def touch(kind):
    open(os.path.join(root, kind, str(pid)), 'w').close()
def wait_two(kind):
    end = time.time() + 8
    pattern = os.path.join(root, kind, '*')
    while time.time() < end:
        if len(glob.glob(pattern)) >= 2:
            return
        time.sleep(0.01)
    raise RuntimeError('barrier timeout: ' + kind)
beacons.live_sessions = lambda: {sid: os.getppid()}
real_register = beacons.register
def register(*args, **kwargs):
    row = real_register(*args, **kwargs)
    if kwargs.get('phase') == 'arming':
        touch('pending')
        wait_two('pending')
    return row
beacons.register = register
real_probe = beacons._one_live_incumbent
def probe(*args, **kwargs):
    got = real_probe(*args, **kwargs)
    with open(os.path.join(root, 'probe', str(pid)), 'w') as out:
        json.dump(got['pid'] if got else None, out)
    os.kill(pid, signal.SIGSTOP)
    return got
beacons._one_live_incumbent = probe
touch('ready')
wait_two('ready')
report = beacons.arm(seat, session=sid)
tmp_out = os.path.join(root, 'result', str(pid) + '.part')
with open(tmp_out, 'w') as out:
    json.dump(report, out)
# The reader waits for EXISTENCE, so the file must not exist until it is
# whole: open() creates it empty and json.dump flushes only at close.
os.replace(tmp_out, os.path.join(root, 'result', str(pid)))
if report.get('already_live') or report.get('conflict'):
    raise SystemExit(0)
while True:
    time.sleep(0.2)
""")
        env = dict(os.environ, HELM_HOME=os.environ["HELM_HOME"], HOME=self.tmp,
                   HELM_CHAT_NAME=self.seat, CLAUDE_CODE_SESSION_ID=SID_A,
                   PYTHONPATH=ROOT, RACE_ROOT=race, RACE_SEAT=self.seat,
                   RACE_SID=SID_A)
        argv = [sys.executable, script, "chat", "wait", "--seat", self.seat,
                "--follow"]
        kids = [subprocess.Popen(argv, env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL) for _ in range(2)]
        self.kids.extend(kids)
        first = self.wait_paths(os.path.join(race, "probe", "*"), 1)[0]
        with open(first) as f:
            decision = json.load(f)
        first_pid = int(os.path.basename(first))
        if decision is None:
            os.kill(first_pid, signal.SIGCONT)  # winner commits, releases lock
        probes = self.wait_paths(os.path.join(race, "probe", "*"), 2)
        for path in probes:
            os.kill(int(os.path.basename(path)), signal.SIGCONT)
        self.wait_paths(os.path.join(race, "result", "*"), 2)
        end = time.time() + 5
        while time.time() < end and sum(p.poll() is None for p in kids) != 1:
            time.sleep(0.02)
        alive = [p for p in kids if p.poll() is None]
        self.assertEqual(len(alive), 1, "concurrent ensures left zero or two beacons")
        self.assertEqual(beacons._scan(self.seat), [alive[0].pid])
        rows = beacons.entries(self.seat)
        self.assertEqual([r["pid"] for r in rows], [alive[0].pid])
        self.assertNotIn("phase", rows[0], "the winner never committed readiness")
        self.assertEqual(rows[0]["waiter"],
                         beacons.waiter_spec(alive[0].pid, row=rows[0]))
        reports = []
        for path in glob.glob(os.path.join(race, "result", "*")):
            with open(path) as f:
                reports.append(json.load(f))
        self.assertEqual(sum(bool(r["registered"]) for r in reports), 1)
        self.assertEqual(sum(bool(r["already_live"]) for r in reports), 1)
    def test_exec_visible_peer_before_marker_elects_one_oldest_survivor(self):
        """The peer can be waiter-shaped before it writes `phase=arming`.
        Freeze the newer launcher after it defers to the older pre-marker peer,
        then let the older arm while that newer process is still visible. Both
        used to defer; the `(starttime, pid)` election makes the older commit."""
        race = os.path.join(self.tmp, "premarker-race")
        for name in ("ready", "result", "bin"):
            os.makedirs(os.path.join(race, name))
        script = os.path.join(race, "bin", "helm")
        with open(script, "w") as f:
            f.write("""import json, os, signal, time
from helm import beacons
root = os.environ['RACE_ROOT']
seat = os.environ['RACE_SEAT']
sid = os.environ['RACE_SID']
role = os.environ['RACE_ROLE']
pid = os.getpid()
beacons.live_sessions = lambda: {sid: os.getppid()}
open(os.path.join(root, 'ready', role), 'w').close()
if role == 'old':
    gate = os.path.join(root, 'go')
    while not os.path.exists(gate):
        time.sleep(0.01)
report = beacons.arm(seat, session=sid)
tmp_out = os.path.join(root, 'result', role + '.part')
with open(tmp_out, 'w') as out:
    json.dump(report, out)
os.replace(tmp_out, os.path.join(root, 'result', role))
if report.get('already_live') or report.get('conflict'):
    if role == 'new':
        os.kill(pid, signal.SIGSTOP)
    raise SystemExit(0)
while True:
    time.sleep(0.2)
""")
        base = dict(os.environ, HELM_HOME=os.environ["HELM_HOME"], HOME=self.tmp,
                    HELM_CHAT_NAME=self.seat, CLAUDE_CODE_SESSION_ID=SID_A,
                    PYTHONPATH=ROOT, RACE_ROOT=race, RACE_SEAT=self.seat,
                    RACE_SID=SID_A)
        argv = [sys.executable, script, "chat", "wait", "--seat", self.seat,
                "--follow"]
        old = subprocess.Popen(argv, env=dict(base, RACE_ROLE="old"),
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.kids.append(old)
        self.wait_paths(os.path.join(race, "ready", "old"), 1)
        new = subprocess.Popen(argv, env=dict(base, RACE_ROLE="new"),
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.kids.append(new)
        self.wait_paths(os.path.join(race, "result", "new"), 1)
        with open(os.path.join(race, "result", "new")) as f:
            new_report = json.load(f)
        self.assertEqual(new_report["already_live"]["pid"], old.pid)
        open(os.path.join(race, "go"), "w").close()
        self.wait_paths(os.path.join(race, "result", "old"), 1)
        with open(os.path.join(race, "result", "old")) as f:
            old_report = json.load(f)
        try:
            os.kill(new.pid, signal.SIGCONT)
        except ProcessLookupError:
            pass
        end = time.time() + 5
        while time.time() < end and new.poll() is None:
            time.sleep(0.02)
        self.assertTrue(old_report["registered"])
        self.assertIsNone(old_report["already_live"])
        self.assertIsNone(old.poll(), "the elected oldest launcher did not survive")
        self.assertIsNotNone(new.poll(), "the newer pre-marker contender survived")
        self.assertEqual(beacons._scan(self.seat), [old.pid])
        self.assertEqual([r["pid"] for r in beacons.entries(self.seat)], [old.pid])
    def test_case_variants_share_one_replacement_election(self):
        """Chat identity says `Kimi` and `kimi` are one seat. Their explicit
        replacements must therefore enter one critical section: raw-cased lock
        names let both stop passes run together and each target the other,
        admitting the zero-wake-path interleaving the lock exists to forbid."""
        import threading
        upper = self.spawn(seat="Kimi")
        lower = self.spawn(seat="kimi")
        first_inside = threading.Event()
        second_inside = threading.Event()
        release_first = threading.Event()
        calls, reports, errors = [], [], []

        def stop(seat, **_kwargs):
            calls.append(seat)
            if len(calls) == 1:
                first_inside.set()
                if not release_first.wait(3):
                    raise RuntimeError("test never released first election")
            else:
                second_inside.set()
            return {"stopped": [], "kept": [], "pruned": []}

        def run(seat, pid):
            try:
                reports.append(beacons.arm(
                    seat, session=SID_A, pid=pid, replace=True))
            except Exception as exc:                 # thread failures are evidence
                errors.append(exc)

        with mock.patch.object(beacons, "live_sessions",
                               return_value={SID_A: os.getpid()}), \
                mock.patch.object(beacons, "stop_superseded", side_effect=stop):
            first = threading.Thread(target=run, args=("Kimi", upper.pid),
                                     daemon=True)
            first.start()
            started = first_inside.wait(2)
            second, overlapped = None, False
            if started:
                second = threading.Thread(target=run, args=("kimi", lower.pid),
                                          daemon=True)
                second.start()
                overlapped = second_inside.wait(0.3)
            # Always release a broken implementation too: a red concurrency
            # regression must not strand its own non-returning fixture thread.
            release_first.set()
            first.join(3)
            if second:
                second.join(3)
        self.assertTrue(started, "first replacement never entered")
        self.assertFalse(first.is_alive() or (second and second.is_alive()))
        self.assertEqual(errors, [])
        self.assertFalse(overlapped, "case variants entered different arm locks")
        self.assertTrue(second_inside.is_set(), "second replacement never ran")
        self.assertEqual(len(reports), 2)
    def test_same_session_behavior_mismatch_requires_explicit_replace(self):
        variants = (("timeout", ("--timeout", "30")),
                    ("room", ("--room", "different-room")),
                    ("any", ("--any",)), ("ambient", ("--ambient",)))
        for label, flags in variants:
            with self.subTest(label=label):
                seat = "%s-%s" % (self.seat, label)
                old = self.spawn(seat=seat, flags=flags)
                new = self.spawn(seat=seat)
                with mock.patch.object(beacons, "live_sessions",
                                       return_value={SID_A: os.getpid()}):
                    report = beacons.arm(seat, session=SID_A, pid=new.pid)
                self.assertIsNone(report["already_live"])
                self.assertEqual(report["conflict"]["pid"], old.pid)
                self.assertEqual(report["stopped"], [])
                self.assertIsNone(old.poll(), "scope conflict rotated implicitly")
                self.assertFalse(report["registered"])
    def test_arming_contender_is_unknown_and_never_covers_the_seat(self):
        contender = self.spawn()
        spec = beacons.waiter_spec(contender.pid)
        with mock.patch.object(beacons, "live_sessions",
                               return_value={SID_A: os.getpid()}):
            beacons.register(self.seat, SID_A, contender.pid, phase="arming",
                             waiter=spec)
            row = beacons.entries(self.seat)[0]
            classified = beacons.classify(
                contender.pid, self.seat, row, live={SID_A: os.getpid()})
            report = beacons.seat_census(
                self.seat, live={SID_A: os.getpid()},
                agents={"by_seat": {}, "by_pid": {}})
        self.assertEqual(classified["state"], beacons.UNKNOWN)
        self.assertEqual(classified["session_state"], "arming")
        self.assertEqual(report["live"], [])
        self.assertEqual(report["verdict"], beacons.UNPROVEN)
        self.assertIn(contender.pid, [r["pid"] for r in report["unknown"]])
    def test_marker_failure_signals_nothing_even_if_active_retry_succeeds(self):
        old, new = self.spawn(), self.spawn()
        real_register = beacons.register
        def fail_marker(*args, **kwargs):
            if kwargs.get("phase") == "arming":
                return None
            return real_register(*args, **kwargs)
        with mock.patch.object(beacons, "register", side_effect=fail_marker), \
                mock.patch.object(beacons, "stop_superseded") as stop:
            report = beacons.arm(self.seat, session=SID_A, pid=new.pid)
        stop.assert_not_called()
        self.assertIn("marker was not written", report["error"])
        self.assertTrue(report["registered"])
        self.assertIsNone(old.poll(), "marker failure rotated the healthy incumbent")
        self.assertIsNone(new.poll())
    def test_unreadable_launcher_starttime_degrades_without_signaling(self):
        """A pre-marker peer cannot be ordered when this launcher's incarnation
        key is unreadable. Deferring could make both contenders exit; replacement
        could make them signal each other. Keep both and surface the degraded arm."""
        old, new = self.spawn(), self.spawn()
        real_starttime = beacons.proc_starttime
        def unreadable_new(pid, proc_dir=None):
            return None if int(pid) == new.pid else real_starttime(pid, proc_dir)
        with mock.patch.object(beacons, "proc_starttime",
                               side_effect=unreadable_new), \
                mock.patch.object(beacons, "stop_superseded") as stop:
            report = beacons.arm(self.seat, session=SID_A, pid=new.pid)
        stop.assert_not_called()
        self.assertIn("pre-marker order unprovable", report["error"])
        self.assertTrue(report["registered"])
        self.assertIsNone(old.poll())
        self.assertIsNone(new.poll())
    def test_unreadable_incumbent_starttime_is_never_signaled(self):
        """Two identical unreadable stat reads are still UNKNOWN, not a stable
        process incarnation. The newcomer may run, but the old waiter is spared."""
        old, new = self.spawn(), self.spawn()
        real_starttime = beacons.proc_starttime
        def unreadable_old(pid, proc_dir=None):
            return None if int(pid) == old.pid else real_starttime(pid, proc_dir)
        with mock.patch.object(beacons, "proc_starttime",
                               side_effect=unreadable_old), \
                mock.patch.object(beacons.os, "kill") as kill:
            report = beacons.arm(self.seat, session=SID_A, pid=new.pid)
        kill.assert_not_called()
        self.assertEqual(report["stopped"], [])
        self.assertIn((old.pid, "start time unreadable — signal withheld"),
                      report["kept"])
        self.assertTrue(report["registered"])
        self.assertIsNone(old.poll())
        self.assertIsNone(new.poll())
    def test_registered_waiter_keeps_its_captured_room_after_cwd_loss(self):  # noqa: VACUOUS_ASSERTION — captured-room equality is the positive control
        argv = [sys.executable, self.script, "chat", "wait", "--seat",
                self.seat, "--follow"]
        env = {"HELM_HOME": os.environ["HELM_HOME"], "HOME": self.tmp,
               "HELM_CHAT_NAME": self.seat,
               "CLAUDE_CODE_SESSION_ID": SID_A}
        captured = beacons.requested_waiter_spec("captured-project-room")
        row = {"waiter": captured}
        with mock.patch.object(beacons, "proc_cwd", return_value=None), \
                mock.patch.object(
                    seats, "derive_home_room_typed",
                    return_value=(seats.DERIVE_UNKNOWN, None)):
            got = beacons.waiter_spec(999999, argv=argv, env=env, row=row)
            mismatched = beacons.waiter_spec(
                999999, argv=argv, env=env,
                row={"waiter": dict(captured, ambient=True)})
        self.assertEqual(got, captured)
        self.assertIsNone(mismatched["room"],
                          "an unreadable cwd was called a measured #main waiter")
    def test_explicit_replace_rotates_one_live_same_session_waiter(self):
        old, new = self.spawn(), self.spawn()
        with mock.patch.object(beacons, "live_sessions",
                               return_value={SID_A: os.getpid()}):
            report = beacons.arm(self.seat, session=SID_A, pid=new.pid,
                                 replace=True)
        self.assertEqual(report["stopped"], [old.pid])
        self.assertIsNone(report["already_live"])
        self.assertTrue(self.dead(old), "explicit replacement kept the incumbent")
        self.assertIsNone(new.poll(), "explicit replacement killed its successor")
    def test_replace_reconsiders_a_scanned_pid_shadowed_by_a_stale_row(self):
        old, new = self.spawn(), self.spawn()
        with mock.patch.object(beacons, "live_sessions",
                               return_value={SID_A: os.getpid()}):
            row = beacons.register(self.seat, SID_A, old.pid)
            self.assertTrue(row and row["starttime"])
            row["starttime"] -= 1            # prior incarnation, same reused pid
            with open(beacons._entry_path(self.seat, old.pid), "w") as f:
                json.dump(row, f)
            report = beacons.arm(self.seat, session=SID_A, pid=new.pid,
                                 replace=True)
        self.assertEqual(report["stopped"], [old.pid])
        self.assertIn(old.pid, report["pruned"])
        self.assertTrue(self.dead(old), "stale row shadowed the live /proc waiter")
        self.assertIsNone(new.poll())
        self.assertEqual(beacons._scan(self.seat), [new.pid])
    def test_ordinary_arm_rotates_a_live_different_session_waiter(self):
        old, new = self.spawn(sid=SID_B), self.spawn(sid=SID_A)
        with mock.patch.object(beacons, "live_sessions", return_value={
                SID_A: os.getpid(), SID_B: os.getpid()}):
            report = beacons.arm(self.seat, session=SID_A, pid=new.pid)
        self.assertEqual(report["stopped"], [old.pid])
        self.assertIsNone(report["already_live"])
        self.assertTrue(self.dead(old), "a different-session waiter was reused")
        self.assertIsNone(new.poll())
    def test_ordinary_arm_rotates_a_ghost_same_session_waiter(self):
        old, new = self.spawn(), self.spawn()
        # Register without a live holder so the later holder record is the
        # positive death proof. A live holder here would make the row UNKNOWN,
        # and the test name would claim a ghost it never constructed.
        with mock.patch.object(beacons, "live_sessions", return_value={}):
            self.assertTrue(beacons.register(self.seat, SID_A, old.pid))
        with mock.patch.object(beacons, "live_sessions", return_value={}), \
                mock.patch.object(beacons, "holder_from_records",
                                  return_value=(99999999, 1)):
            row = beacons.entries(self.seat)[0]
            classified = beacons.classify(old.pid, self.seat, row, live={})
            self.assertEqual(classified["state"], beacons.GHOST,
                             "control: the fixture must prove a ghost")
            report = beacons.arm(self.seat, session=SID_A, pid=new.pid)
        self.assertEqual(report["stopped"], [old.pid])
        self.assertIsNone(report["already_live"])
        self.assertTrue(self.dead(old), "a proven ghost was reused")
        self.assertIsNone(new.poll())
    def test_one_live_plus_one_ghost_is_not_idempotent(self):
        live_waiter = self.spawn(sid=SID_A)
        ghost = self.spawn(sid=SID_B)
        new = self.spawn(sid=SID_A)
        with mock.patch.object(beacons, "live_sessions", return_value={}):
            self.assertTrue(beacons.register(self.seat, SID_B, ghost.pid))
        with mock.patch.object(beacons, "live_sessions",
                               return_value={SID_A: os.getpid()}), \
                mock.patch.object(beacons, "holder_from_records",
                                  return_value=(99999999, 1)):
            report = beacons.arm(self.seat, session=SID_A, pid=new.pid)
        self.assertIsNone(report["already_live"])
        self.assertCountEqual(report["stopped"], [live_waiter.pid, ghost.pid])
        self.assertTrue(self.dead(live_waiter))
        self.assertTrue(self.dead(ghost))
        self.assertIsNone(new.poll())

    def test_a_re_arm_leaves_exactly_ONE_waiter(self):
        """THE ACCUMULATION CURE, asserted as an effect. Re-arm used to be a
        bare START, so each one left its predecessor running and the count only
        climbed — 48 the night this landed."""
        old_a, old_b = self.spawn(), self.spawn()
        new = self.spawn()
        with mock.patch.object(beacons, "live_sessions",
                               return_value={SID_A: os.getpid()}):
            report = beacons.arm(self.seat, session=SID_A, pid=new.pid)
        self.assertCountEqual(report["stopped"], [old_a.pid, old_b.pid])
        self.assertTrue(self.dead(old_a), "the superseded waiter survived")
        self.assertTrue(self.dead(old_b), "the superseded waiter survived")
        self.assertIsNone(new.poll(), "the re-arm killed the NEW beacon")
        self.assertEqual(beacons._scan(self.seat), [new.pid])
        self.assertEqual([r["pid"] for r in beacons.entries(self.seat)],
                         [new.pid])

    def test_another_seats_waiter_is_KEPT(self):
        theirs = self.spawn(seat="beaconfix-other-%d" % os.getpid())
        mine = self.spawn()
        with mock.patch.object(beacons, "live_sessions", return_value={}):
            report = beacons.arm(self.seat, session=SID_A, pid=mine.pid)
        self.assertEqual(report["stopped"], [])
        self.assertIsNone(theirs.poll(), "reaped another seat's beacon")

    def test_a_waiter_in_ANOTHER_helm_home_is_KEPT(self):
        """The cross-fleet gate, proven on a real process: same seat name, a
        different helm home, and it must survive."""
        other = self.spawn(helm_home=os.path.join(self.tmp, "other-home"))
        mine = self.spawn()
        with mock.patch.object(beacons, "live_sessions", return_value={}):
            report = beacons.arm(self.seat, session=SID_A, pid=mine.pid)
        self.assertEqual(report["stopped"], [])
        self.assertIsNone(other.poll(), "reached into another helm home")
        kept = dict((p, w) for p, w in report["kept"])
        self.assertIn("different helm home", kept.get(other.pid, ""))

    def test_a_NON_waiter_helm_process_is_never_signaled(self):
        """The shape gate on a real process: `helm chat post` is a helm process
        wearing the seat's own env, and it is not a beacon."""
        poster = self.spawn(shape="post")
        mine = self.spawn()
        with mock.patch.object(beacons, "live_sessions", return_value={}):
            report = beacons.arm(self.seat, session=SID_A, pid=mine.pid)
        self.assertEqual(report["stopped"], [])
        self.assertIsNone(poster.poll(), "signaled a non-beacon helm process")

    def test_the_arming_beacon_never_signals_itself(self):
        mine = self.spawn()
        with mock.patch.object(beacons, "live_sessions", return_value={}):
            beacons.arm(self.seat, session=SID_A, pid=mine.pid)
        self.assertIsNone(mine.poll())

    def test_an_unlistable_process_table_signals_NOTHING(self):
        """Absence of evidence is not evidence of supersession."""
        old = self.spawn()
        with mock.patch.object(beacons, "live_sessions", return_value={}), \
                mock.patch.object(beacons, "_scan", return_value=None):
            report = beacons.stop_superseded(self.seat, keep_pid=os.getpid())
        self.assertEqual(report["stopped"], [])
        self.assertIsNone(old.poll())
        self.assertIn("unlistable", " ".join(str(w) for _p, w in report["kept"]))


# ---------------------------------------------------------------------------
# the census — BOTH directions
# ---------------------------------------------------------------------------

class CensusTest(Base):
    def census(self, live):
        with mock.patch.object(beacons, "live_sessions", return_value=live):
            return beacons.census(proc_dir=self.proc)

    def test_a_GHOST_waiter_is_REPORTED(self):
        """The dangerous direction: a waiter from a dead session reads healthy
        to a shape check and makes a dark seat look covered."""
        self.roster("alpha")
        self.waiter(201, "alpha", sid=SID_A)
        with mock.patch.object(beacons, "holder_from_records",
                               return_value=(4242, 100)):
            rep = self.census({})
        self.assertEqual([b["pid"] for b in rep["ghosts"]], [201])
        self.assertEqual([r["seat"] for r in rep["deaf"]], ["alpha"])
        out = self.render(rep)
        self.assertIn("GHOST WAITER pid 201", out)
        self.assertIn("DEAF SEAT alpha", out)

    def test_a_DEAF_seat_is_REPORTED(self):
        """The other direction, and it must fire for a seat that is HERE with
        no beacon at all."""
        self.roster("alpha")
        rep = self.census({SID_A: 90})
        self.assertEqual([r["seat"] for r in rep["deaf"]], ["alpha"])
        self.assertIn("DEAF SEAT alpha", self.render(rep))

    def test_a_covered_seat_raises_NEITHER_alarm(self):
        """A live beacon AND an agent that declares the seat. The agent half is
        not decoration: a live wake path alone stopped being the whole answer
        when VACANT landed, and a fixture without a pane is a seat with nobody
        home."""
        self.roster("alpha")
        self.waiter(202, "alpha", sid=SID_A)
        self.agent(90, "alpha")
        rep = self.census({SID_A: 90})
        self.assertEqual(rep["deaf"], [])
        self.assertEqual(rep["ghosts"], [])
        self.assertEqual(rep["vacant"], [])
        self.assertEqual(rep["seats"][0]["verdict"], beacons.COVERED)

    def test_an_UNPROVABLE_beacon_is_UNPROVEN_and_never_DEAF(self):
        """UNKNOWN never collapses into a verdict. Calling this DEAF invents a
        crisis; calling it covered is the lie the lane closes.

        It is LOUD without being DEAF: the verdict stays UNPROVEN (nothing was
        proven, and the report says so) while the line reads UNREACHABLE,
        because no beacon could be proven live either — an unclassifiable seat
        with no proven wake path is a possible outage, not a clean bill."""
        self.roster("alpha")
        self.waiter(203, "alpha", sid=SID_A)
        with mock.patch.object(beacons, "holder_from_records", return_value=None):
            rep = self.census({})
        self.assertEqual(rep["deaf"], [])
        self.assertEqual(rep["ghosts"], [])
        self.assertEqual([r["seat"] for r in rep["unproven"]], ["alpha"])
        self.assertEqual([r["seat"] for r in rep["unreachable"]], ["alpha"])
        out = self.render(rep)
        self.assertIn("alpha", out)
        self.assertIn("verdict UNPROVEN", out)          # never DEAF
        self.assertIn("UNREACHABLE alpha", out)         # and never quiet

    def test_EVERY_beacon_is_asserted_not_the_newest(self):
        """#helm 622: a seat with five waiters, four stale, read as 'current'
        because the scan took the newest arm time."""
        self.roster("alpha")
        self.waiter(204, "alpha", sid=SID_A)           # ghost
        self.waiter(205, "alpha", sid=SID_A)           # ghost
        self.waiter(206, "alpha", sid=SID_B)           # live
        self.agent(90, "alpha")
        with mock.patch.object(beacons, "holder_from_records",
                               return_value=(4242, 100)):
            rep = self.census({SID_B: 90})
        self.assertEqual(sorted(b["pid"] for b in rep["ghosts"]), [204, 205])
        self.assertEqual(rep["seats"][0]["verdict"], beacons.COVERED)
        self.assertIn("GHOST WAITER pid 204", self.render(rep))
        self.assertIn("GHOST WAITER pid 205", self.render(rep))

    def test_duplicate_live_beacons_are_counted_as_SURPLUS(self):
        """Session liveness cannot dedupe two waiters of the SAME live session
        — that is what the registry and stop-then-start are for — so the count
        is surfaced instead of being silently accepted."""
        self.roster("alpha")
        self.waiter(207, "alpha", sid=SID_A)
        self.waiter(208, "alpha", sid=SID_A)
        rep = self.census({SID_A: 90})
        self.assertEqual(rep["surplus"], 1)
        self.assertIn("+1 surplus", self.render(rep))

    def test_an_ABSENT_roster_seat_raises_no_alarm_but_its_beacon_still_does(self):
        """The live roster carried 190 rows, 179 of them dead placeholders.
        Alarming on those buries the one seat that really is unreachable — an
        alarm that fires for everything says nothing. A beacon HELD by an
        absent seat is the opposite case and still reports."""
        self.roster("ghosttown", fresh=False)
        rep = self.census({SID_A: 90})
        self.assertEqual(rep["seats"], [])
        with mock.patch.object(beacons, "live_sessions", return_value={}):
            beacons.register("ghosttown", session=SID_A, pid=209,
                             proc_dir=self.proc)
        self.waiter(209, "ghosttown", sid=SID_A)
        with mock.patch.object(beacons, "holder_from_records",
                               return_value=(4242, 100)):
            rep = self.census({})
        self.assertEqual([b["pid"] for b in rep["ghosts"]], [209])

    def test_a_dead_waiters_registry_row_is_PRUNED_not_reported(self):
        self.roster("alpha")
        with mock.patch.object(beacons, "live_sessions", return_value={}):
            beacons.register("alpha", session=SID_A, pid=4242,
                             proc_dir=self.proc)
        self.assertEqual(len(beacons.entries("alpha")), 1)
        rep = self.census({SID_A: 90})
        self.assertEqual(beacons.entries("alpha"), [])
        self.assertEqual(rep["ghosts"], [])

    def test_an_unprobeable_liveness_map_says_so_before_any_verdict(self):
        self.roster("alpha")
        self.waiter(210, "alpha", sid=SID_A)
        rep = self.census(None)
        self.assertFalse(rep["live_probe"])
        self.assertEqual(rep["ghosts"], [])
        self.assertIn("could not be probed", self.render(rep))

    def render(self, rep):
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            beacons._print_census(rep)
        return buf.getvalue()


class LivePaneRollTest(Base):
    """A live roster seat belongs on the roll even when its beat and beacon are
    both gone. The agent census is an ADDITIVE third admission source, never a
    process-derived replacement for the roster's fleet boundary."""

    def pane(self, pid, seat=None, helm_home=None):
        env = ["HOME=%s" % self.tmp,
               "HELM_HOME=%s" % (helm_home or os.environ["HELM_HOME"])]
        if seat:
            env.append("HELM_CHAT_NAME=%s" % seat)
        return self.plant(pid, ["claude", "--dangerously-skip-permissions"],
                          env, comm="claude")

    def report(self, live):
        with mock.patch.object(beacons, "live_sessions", return_value=live):
            return beacons.census(proc_dir=self.proc)

    def write_roster(self, rows):
        path = os.path.join(os.environ["HELM_CHAT_DIR"], ".roster.json")
        with open(path, "w") as f:
            json.dump(rows, f)

    def test_an_ABSENT_declared_pane_enters_once_by_normalized_roster_identity(self):
        """The original omission plus two controls from its rejected cure: the
        pane says lower-case `alpha`, the roster owns `Alpha`, and one census
        pass may enumerate agents exactly once."""
        self.roster("Alpha", fresh=False)
        self.pane(9101, "alpha")
        real = beacons.agent_index
        with mock.patch.object(beacons, "agent_index", wraps=real) as scanned:
            rep = self.report({})
        self.assertEqual([r["seat"] for r in rep["seats"]], ["Alpha"])
        self.assertEqual(scanned.call_count, 1,
                         "one census pass scanned agents more than once")

    def test_a_live_session_bound_UNSTAMPED_pane_still_enters(self):
        """A hand-launched pane can declare no seat in its own environ. The
        roster session plus the live-session pid still names the seat without
        inventing identity from the process table."""
        self.roster("alpha", fresh=False)
        self.pane(9102)
        rep = self.report({SID_A: 9102})
        self.assertEqual([r["seat"] for r in rep["seats"]], ["alpha"])
        self.assertEqual(rep["seats"][0]["agent"], None,
                         "an unstamped pane was credited with declaring a seat")

    def test_a_live_HISTORICAL_session_still_binds_its_roster_owner(self):
        """Compaction moves the current sid without killing older live panes.
        The roster's identity index owns both fields; reading only `session`
        makes a zero-beacon seat disappear whenever its live pane holds an id
        retained in `sessions`."""
        self.write_roster({
            "alpha": {"session": SID_A, "sessions": [SID_B], "last_seen": 1.0},
        })
        self.pane(9111)
        rep = self.report({SID_B: 9111})
        self.assertEqual([r["seat"] for r in rep["seats"]], ["alpha"])

    def test_a_DUPLICATED_session_cannot_admit_two_roster_owners(self):
        """A sid remembered by two rows is UNVERIFIED identity. One unstamped
        pane carrying it is not positive evidence for either row, let alone
        both; only an independent self-declaration may admit one owner."""
        rows = {
            "alpha": {"session": SID_A, "last_seen": 1.0},
            "beta": {"session": SID_A, "last_seen": 1.0},
        }
        self.write_roster(rows)
        self.pane(9112)
        self.assertEqual(self.report({SID_A: 9112})["seats"], [])

        self.pane(9113, "alpha")
        rep = self.report({SID_A: 9112})
        self.assertEqual([r["seat"] for r in rep["seats"]], ["alpha"])

    def test_a_process_that_refused_both_detector_reads_stays_indexed(self):
        """Approved task/273's fail-closed detector rule composes with the home
        map: both failed reads carry a possible pane, while a readable non-agent
        remains excluded."""
        d = self.pane(9107)
        os.chmod(os.path.join(d, "cmdline"), 0)
        os.chmod(os.path.join(d, "comm"), 0)
        self.assertIsNone(beacons.proc_argv(9107, self.proc))
        self.assertIsNone(beacons.proc_comm(9107, self.proc))
        idx = beacons.agent_index(self.proc)
        self.assertIn(9107, idx["by_pid"])
        self.assertEqual(idx["by_pid"][9107], [])

        self.waiter(9108, "alpha", sid=SID_A)
        self.assertNotIn(9108, beacons.agent_index(self.proc)["by_pid"],
                         "a readable non-agent was carried too")

    def test_a_held_beacon_cannot_replace_the_rosters_canonical_casing(self):
        """All admission sources name one roster identity. A lower-case beacon
        must not win before the mixed-case roster key and make attendance's
        case-sensitive row lookup miss the seat."""
        self.roster("Alpha", fresh=False)
        self.waiter(9109, "alpha", sid=SID_A)
        with mock.patch.object(beacons, "live_sessions", return_value={}):
            beacons.register("alpha", session=SID_A, pid=9109,
                             proc_dir=self.proc)
        self.pane(9110, "alpha")
        rep = self.report({SID_A: 9110})
        self.assertEqual([r["seat"] for r in rep["seats"]], ["Alpha"])

    def test_live_panes_do_not_admit_graveyard_foreign_home_or_foreign_roster_rows(self):
        """The third source is roster-intersected and home-scoped. `control`
        proves it fired; the other three rows isolate each forbidden widening."""
        self.roster("control", "foreign-home", "graveyard", fresh=False)
        self.pane(9103, "control")
        self.pane(9104, "other-project")       # live, but not in this roster
        self.pane(9105, "foreign-home",
                  helm_home=os.path.join(self.tmp, "other-helm"))
        rep = self.report({})
        self.assertEqual([r["seat"] for r in rep["seats"]], ["control"])

    def test_an_unreadable_agent_census_withholds_additions_without_shrinking(self):
        """None means the process table did not answer, not that it is empty.
        Existing presence/grace admissions survive and the pane-only candidate
        waits for a later readable pass."""
        self.write_roster({
            "grace": {"last_seen": 1.0,
                      "attendance": {"state": beacons.DEAF,
                                     "covered": time.time() - 60}},
            "pane-only": {"session": SID_B, "last_seen": 1.0},
            "present": {"session": SID_A, "last_seen": time.time()},
        })
        self.pane(9106, "pane-only")
        with mock.patch.object(beacons, "agent_index", return_value=None):
            rep = self.report({})
        self.assertEqual([r["seat"] for r in rep["seats"]],
                         ["grace", "present"])
        self.assertFalse(rep["agent_probe"])

    def test_a_malformed_session_index_withholds_only_its_additions(self):
        """One junk historical-session field cannot crash the watchdog. The
        canonical index becomes unreadable, so pane-only attribution waits,
        while independently fresh presence remains on the roll."""
        self.write_roster({
            "pane-only": {"session": SID_B, "last_seen": 1.0},
            "present": {"sessions": 7, "last_seen": time.time()},
        })
        self.pane(9114)
        rep = self.report({SID_B: 9114})
        self.assertEqual([r["seat"] for r in rep["seats"]], ["present"])


class VacantTest(Base):
    """The THIRD direction: a LIVE wake path for a seat with no agent.

    MEASURED on the census's first live run, 2026-07-31. Two seats retired that
    night read "covered, live 1" and every fact under it was true — their
    beacons carried HELM_CHAT_NAME and a live session held each one. A BEACON IS
    A `helm chat wait` PYTHON PROCESS, NOT AN AGENT, so "covered" was true of the
    wake path and unproven of the seat. VACANT is the exact inverse of GHOST: a
    ghost is a dead wake path for a live seat.

    THE FIXTURES ARE SYNTHETIC /proc TREES on purpose — no host path, no real
    seat name, no username reaches this file."""

    def census(self, live):
        with mock.patch.object(beacons, "live_sessions", return_value=live):
            return beacons.census(proc_dir=self.proc)

    def test_a_live_beacon_with_NO_agent_is_VACANT(self):
        """The defect, asserted as an effect: the beacon answers, the house is
        empty. The process the beacon writes into is provably ANOTHER seat's
        agent, so nothing is being inferred from a bare absence."""
        self.roster("alpha")
        self.waiter(601, "alpha", sid=SID_A)
        self.agent(90, "beta")                       # the holder, and it is beta's
        rep = self.census({SID_A: 90})
        self.assertEqual([r["seat"] for r in rep["vacant"]], ["alpha"])
        self.assertEqual(rep["seats"][0]["verdict"], beacons.VACANT)
        self.assertEqual(rep["deaf"], [])
        self.assertEqual(rep["ghosts"], [])
        out = CensusTest.render(self, rep)
        self.assertIn("VACANT SEAT alpha", out)
        self.assertIn("1 VACANT", out)

    def test_a_seat_whose_pane_DECLARES_it_is_still_COVERED(self):
        """The regression that matters. Every working seat must survive this
        verdict landing — a false VACANT tells a reader to stop addressing work
        to a seat that is working."""
        self.roster("alpha")
        self.waiter(602, "alpha", sid=SID_A)
        self.agent(90, "alpha")
        rep = self.census({SID_A: 90})
        self.assertEqual(rep["vacant"], [])
        self.assertEqual(rep["unproven"], [])
        self.assertEqual(rep["seats"][0]["verdict"], beacons.COVERED)
        self.assertIn("1 covered", CensusTest.render(self, rep))

    def test_an_UNSTAMPED_pane_holds_the_seat_at_UNPROVEN_never_VACANT(self):
        """THE ANTI-FALSE-POSITIVE BRANCH, and the reason this verdict is not
        simply "no pane declares the seat".

        A hand-launched `claude` joins the roster under a DERIVED name and
        stamps nothing in its own environ — 8 of the 16 live panes on the box
        this landed from are exactly that, including the seat that motivated
        the verdict. A bare absence check would have called every one of them
        VACANT while it was working. An unattributable pane may BE this seat's
        agent, and that is UNPROVEN."""
        self.roster("alpha")
        self.waiter(603, "alpha", sid=SID_A)
        self.agent(90)                               # live pane, declares nothing
        rep = self.census({SID_A: 90})
        self.assertEqual(rep["vacant"], [])
        self.assertEqual([r["seat"] for r in rep["unproven"]], ["alpha"])
        self.assertIn("declares no seat of its own",
                      CensusTest.render(self, rep))

    def test_an_UNREADABLE_pane_taints_ONLY_the_seat_it_answers_for(self):
        """A pane helm cannot read is not evidence of anything — but it is not
        a reason to un-answer the whole box either. Blast radius exact: the
        seat whose beacon that pane holds goes UNPROVEN, and a seat with its
        own declaring pane stays COVERED."""
        self.roster("alpha", "beta")
        self.waiter(604, "alpha", sid=SID_A)
        self.waiter(605, "beta", sid=SID_B)
        d = self.agent(90)                           # alpha's holder
        self.agent(91, "beta")
        os.chmod(os.path.join(d, "environ"), 0)
        try:
            rep = self.census({SID_A: 90, SID_B: 91})
        finally:
            os.chmod(os.path.join(d, "environ"), 0o644)
        got = {r["seat"]: r["verdict"] for r in rep["seats"]}
        self.assertEqual(got, {"alpha": beacons.UNPROVEN,
                               "beta": beacons.COVERED})

    def test_an_UNLISTABLE_process_table_is_UNPROVEN_never_VACANT(self):
        """`agent_index` returning None means the scan failed. Reading it as
        'no agent exists anywhere' would call the whole fleet vacant."""
        self.roster("alpha")
        self.waiter(606, "alpha", sid=SID_A)
        self.agent(90, "beta")
        with mock.patch.object(beacons, "agent_index", return_value=None):
            rep = self.census({SID_A: 90})
        self.assertEqual(rep["vacant"], [])
        self.assertEqual([r["seat"] for r in rep["unproven"]], ["alpha"])
        self.assertFalse(rep["agent_probe"])

    def test_an_UNPROBEABLE_liveness_map_never_yields_VACANT(self):
        """The other probe this rung leans on. With no session map the pane
        behind the beacon cannot be named at all."""
        self.roster("alpha")
        self.waiter(607, "alpha", sid=SID_A)
        self.agent(90, "beta")
        rep = self.census(None)
        self.assertEqual(rep["vacant"], [])

    def test_a_GHOST_stays_a_GHOST_and_its_seat_stays_DEAF(self):
        """VACANT must not swallow the verdict it is the inverse of. A seat
        with only a ghost has no live wake path, so it is DEAF — the empty
        house is not the finding, the dead pipe is."""
        self.roster("alpha")
        self.waiter(608, "alpha", sid=SID_A, ppid=REAPER)   # orphan -> ghost
        self.agent(90, "beta")
        rep = self.census({SID_A: 90})
        self.assertEqual([b["pid"] for b in rep["ghosts"]], [608])
        self.assertEqual([r["seat"] for r in rep["deaf"]], ["alpha"])
        self.assertEqual(rep["vacant"], [])

    def test_a_SEAT_FAMILY_config_dir_declares_the_seat_too(self):
        """The launch seam's other stamp: a pane whose CLAUDE_CONFIG_DIR sits
        directly under this home's seats dir names its family, which is how a
        seat launched without HELM_CHAT_NAME still declares itself."""
        self.roster("alpha")
        self.waiter(609, "alpha", sid=SID_A)
        cdir = os.path.join(home.global_dir(), "seats", "alpha", "claude")
        os.makedirs(cdir, exist_ok=True)
        self.agent(90, config_dir=cdir)
        rep = self.census({SID_A: 90})
        self.assertEqual(rep["seats"][0]["verdict"], beacons.COVERED)

    def test_a_pane_named_only_by_COMM_is_still_an_agent(self):
        """The two instruments this rung joins spell the same process
        differently: `hooks.running_panes` matches the argv basename, and
        `sessions.live_sids` proves comm == claude EXACTLY. A pane the session
        prober can see and this scan cannot would arrive as an unattributable
        holder and hold a working seat at UNPROVEN forever."""
        self.roster("alpha")
        self.waiter(610, "alpha", sid=SID_A)
        self.agent(90, "alpha", argv=["node", "/x/lib/cli.js"], comm="claude")
        rep = self.census({SID_A: 90})
        self.assertEqual(rep["seats"][0]["verdict"], beacons.COVERED)

    def test_a_process_that_refused_BOTH_detector_reads_stays_in_the_index(self):
        """is_agent(None, None) IS FALSE AND MEANS NOTHING.

        None is what a FAILED read returns, so the detector's answer for a
        hardened process is identical to its answer for vim, and the bare
        `continue` could not tell them apart. This function's own docstring
        says a pane whose reads fail "is NOT dropped ... it is recorded as
        declaring NOTHING" — true of the ENVIRON read, and false of the
        DETECTOR read one line above it until this change.

        CARRIED HERE, EXCLUDED IN hooks.running_panes, and the difference is
        not an inconsistency. That function answers "which panes are OURS to
        manage", so a pid it cannot prove is ours must go. This index answers,
        through gate.suite_cap, "would a big suite STARVE a pane on this box",
        and another user's claude starves exactly as well as ours."""
        d = self.plant(7401, argv=["claude"], env=[], comm="claude")
        os.chmod(os.path.join(d, "cmdline"), 0o000)
        os.chmod(os.path.join(d, "comm"), 0o000)
        self.addCleanup(lambda: None)

        # CONTROL FIRST: both reads really do refuse, or the arm below is
        # asserting about an ordinary readable agent and proves nothing.
        self.assertIsNone(beacons.proc_argv(7401, self.proc))
        self.assertIsNone(beacons.proc_comm(7401, self.proc))

        idx = beacons.agent_index(self.proc)
        self.assertIn(7401, idx["by_pid"],
                      "a process that refused both detector reads was dropped "
                      "from the census whose docstring promises it is kept")
        self.assertEqual(idx["by_pid"][7401], [],
                         "it must declare NOTHING, not be credited with a seat")

        # THE REVIEWER'S RACE: a pid can disappear after listdir but before its
        # files are read. Public helpers return None for both facts, while the
        # read boundary still knows that this one is PROVEN gone. It must not
        # become the same possible-pane entry as the unreadable fixture above.
        os.makedirs(os.path.join(self.proc, "7404"))
        self.assertEqual(beacons._proc_argv(7404, self.proc), (None, "gone"))
        self.assertEqual(beacons._proc_comm(7404, self.proc), (None, "gone"))
        self.assertIsNone(beacons.proc_argv(7404, self.proc))
        self.assertIsNone(beacons.proc_comm(7404, self.proc))
        self.assertNotIn(7404, beacons.agent_index(self.proc)["by_pid"],
                         "a process proven gone was carried as a possible pane")

        # The temporal shape can answer once and disappear before the second
        # read. A stale cmdline that says claude must not overrule the later,
        # stronger proof that the process is gone.
        half = os.path.join(self.proc, "7405")
        os.makedirs(half)
        with open(os.path.join(half, "cmdline"), "wb") as f:
            f.write(b"claude\0")
        self.assertEqual(beacons._proc_argv(7405, self.proc),
                         (["claude"], None))
        self.assertEqual(beacons._proc_comm(7405, self.proc), (None, "gone"))
        self.assertNotIn(7405, beacons.agent_index(self.proc)["by_pid"],
                         "an earlier readable file overruled later gone proof")

        # NEGATIVE CONTROL: a process that ANSWERED and is not claude is a
        # judgement and still leaves. Without this, "keep everything" passes.
        self.waiter(7402, "alpha", sid=SID_A)
        idx2 = beacons.agent_index(self.proc)
        self.assertNotIn(7402, idx2["by_pid"],
                         "a readable non-agent was carried; the cure kept "
                         "everything instead of keeping the unreadable")

    def test_an_unclassifiable_process_keeps_the_suite_cap_CONSERVATIVE(self):
        """THE CONSUMER WHERE MEMBERSHIP IS THE ANSWER.

        agent_verdict treats a missing pid and a pid-declaring-nothing the
        same (both fall to `unnamed`), so the drop was invisible there.
        gate.suite_cap is where it bites: it raises this box's whole-suite
        admission cap ONLY on a census that ran and came back EMPTY. A
        dropped pid could therefore hand a hardened box a RAISED cap on a
        census that never saw its panes — and a big suite would then starve
        the agents it could not see.

        THE INVARIANT, stated before reading the implementation: a census
        that saw a POSSIBLE pane must never grant MORE than a census that saw
        none."""
        from helm import gate

        with mock.patch.object(gate, "_online_cpu_count", return_value=32):
            empty = gate.suite_cap(self.proc)

            d = self.plant(7403, argv=["claude"], env=[], comm="claude")
            os.chmod(os.path.join(d, "cmdline"), 0o000)
            os.chmod(os.path.join(d, "comm"), 0o000)
            with_pane = gate.suite_cap(self.proc)

        self.assertIn(7403, gate._agent_pane_pids(self.proc) or [],
                      "the unclassifiable pid never reached the cap's input")
        self.assertEqual(with_pane, gate.SUITE_CAP,
                         "a box that may be hosting a pane took more than the "
                         "pane-host cap")
        self.assertLessEqual(with_pane, empty,
                             "a census that saw a possible pane granted MORE "
                             "than one that saw none")
        # NON-VACUITY: the capacity source itself is pinned to 32 above, so the
        # empty census must raise and the possible pane must strictly lower it.
        self.assertGreater(empty, with_pane,
                           "the online CPU grant can raise the cap, so the two "
                           "censuses must differ — otherwise the test cannot "
                           "see the defect it exists for")
    def test_foreign_home_panes_never_determine_a_local_seat_verdict(self):
        """Home scoping governs evidence, not only roll admission. A foreign
        same-name pane must not cover alpha, and a foreign other-name pane must
        not prove beta vacant; both local seats remain UNPROVEN."""
        self.roster("alpha", "beta")
        self.waiter(616, "alpha", sid=SID_A)
        self.waiter(617, "beta", sid=SID_B)
        foreign = os.path.join(self.tmp, "other-helm")
        self.plant(90, ["claude"],
                   ["HOME=%s" % self.tmp, "HELM_HOME=%s" % foreign,
                    "HELM_CHAT_NAME=alpha"], comm="claude")
        self.plant(91, ["claude"],
                   ["HOME=%s" % self.tmp, "HELM_HOME=%s" % foreign,
                    "HELM_CHAT_NAME=other-seat"], comm="claude")
        rep = self.census({SID_A: 90, SID_B: 91})
        self.assertEqual(rep["covered"], [])
        self.assertEqual(rep["vacant"], [])
        self.assertEqual([r["seat"] for r in rep["unproven"]],
                         ["alpha", "beta"])

    def test_a_BEACON_is_never_mistaken_for_the_agent_it_waits_for(self):
        """The whole confusion in one assertion: the beacon process satisfies
        no part of the agent test, so a seat can never be covered by its own
        wake path."""
        self.waiter(611, "alpha", sid=SID_A)
        idx = beacons.agent_index(self.proc)
        self.assertEqual(idx["by_seat"], {})
        self.assertNotIn(611, idx["by_pid"])
        self.assertFalse(beacons.is_agent(beacons.proc_argv(611, self.proc),
                                          beacons.proc_comm(611, self.proc)))

    def test_the_VACANT_line_cannot_be_reshaped_by_the_OTHER_seats_name(self):
        """The reason string names the pane's OWN declared seat — a third
        attacker-supplied string from another process's environ, printed to an
        operator's terminal on the loudest line in the report."""
        self.roster("alpha")
        self.waiter(612, "alpha", sid=SID_A)
        self.agent(90, LaunderTest.HOSTILE)
        rep = self.census({SID_A: 90})
        self.assertEqual([r["seat"] for r in rep["vacant"]], ["alpha"],
                         "the fixture did not reach the reason-printing line")
        text = CensusTest.render(self, rep)
        self.assertIn("VACANT SEAT alpha", text)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("‮", text)

    def test_the_summary_COUNTS_covered_instead_of_subtracting(self):
        """Derived by subtraction, the summary called every verdict it did not
        know about 'covered' — so the tally would have contradicted the alarm
        lines printed directly above it."""
        self.roster("alpha", "beta")
        self.waiter(613, "alpha", sid=SID_A)
        self.waiter(614, "beta", sid=SID_B)
        self.agent(90, "gamma")                      # alpha's holder: not alpha's
        self.agent(91, "beta")
        rep = self.census({SID_A: 90, SID_B: 91})
        # THE LITERAL IS THE POINT: it breaks whenever a verdict joins the
        # line, which is precisely when somebody needs to check the tally
        # still accounts for every seat. MISROUTED broke it on arrival and
        # that is the arm working, not the arm being brittle.
        self.assertIn("2 seats, 1 covered, 0 WAKING, 0 DEAF, "
                      "0 DEAF-IN-EFFECT, 0 MISROUTED, 1 VACANT, 0 UNPROVEN",
                      CensusTest.render(self, rep))

    def test_the_census_still_signals_NOTHING_at_a_VACANT_seat(self):
        """READ-ONLY IS ABSOLUTE. Three other fleets' beacons live on the box
        this landed from; reaping by pattern across a shared process table is
        how a live seat's wake path gets severed. A VACANT verdict is a
        sentence, never a signal."""
        self.roster("alpha")
        self.waiter(615, "alpha", sid=SID_A)
        self.agent(90, "beta")
        with mock.patch.object(os, "kill") as killed, \
                mock.patch.object(os, "unlink") as unlinked:
            rep = self.census({SID_A: 90})
        self.assertEqual([r["seat"] for r in rep["vacant"]], ["alpha"])
        self.assertFalse(killed.called, "the census signaled a process")
        self.assertFalse(unlinked.called, "the census unlinked something")
        self.assertIn("This verb signals NOTHING", CensusTest.render(self, rep))


class LaunderTest(Base):
    """This module reads TWO attacker-supplied strings that no join seam ever
    saw — a seat name and a session id, both taken from ANOTHER PROCESS's argv
    or environ — and prints both to an operator's terminal. Any process on a
    shared box can export HELM_CHAT_NAME or CLAUDE_CODE_SESSION_ID."""

    HOSTILE = "\x1b[31mred\x1b[0m‮gnihsihp"

    def test_a_hostile_seat_name_from_another_process_cannot_reshape_the_report(self):
        self.roster("alpha")
        self.waiter(501, "alpha", sid=SID_A)
        row = beacons.classify(501, None, None, {SID_A: 9}, self.proc)
        self.assertNotIn("\x1b", str(row["seat"]))
        # ...and the printed report carries nothing raw either
        with mock.patch.object(beacons, "live_sessions",
                               return_value={SID_A: 9}):
            rep = beacons.census(proc_dir=self.proc)
        self.assertNotIn("\x1b", CensusTest.render(self, rep))

    def test_a_hostile_seat_name_is_laundered_where_it_ENTERS_the_row(self):
        argv = [sys.executable, "/x/bin/helm", "chat", "wait",
                "--seat", self.HOSTILE, "--follow"]
        self.plant(502, argv, ["HOME=%s" % self.tmp,
                               "HELM_HOME=%s" % os.environ["HELM_HOME"]])
        row = beacons.classify(502, None, None, {}, self.proc)
        self.assertNotIn("\x1b", str(row["seat"]))
        self.assertNotIn("‮", str(row["seat"]))

    def test_a_hostile_SESSION_ID_cannot_reach_the_printed_reason(self):
        """The reason strings interpolate 8 bytes of the sid — plenty for an
        ESC sequence, and they go straight to a terminal.

        Aimed at the GHOST line specifically, because that is the one that
        PRINTS `why`. A first version drove the UNPROVEN path, whose line
        carries no reason at all: it passed against the raw-sid mutation and
        was measuring nothing."""
        self.roster("alpha")
        self.waiter(503, "alpha", sid=self.HOSTILE)
        with mock.patch.object(beacons, "live_sessions", return_value={}), \
                mock.patch.object(beacons, "holder_from_records",
                                  return_value=(4242, 100)):
            rep = beacons.census(proc_dir=self.proc)
        self.assertEqual([b["pid"] for b in rep["ghosts"]], [503],
                         "the fixture did not reach the reason-printing line")
        text = CensusTest.render(self, rep)
        self.assertIn("GHOST WAITER pid 503", text)
        self.assertIn("the process that held session", text)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("‮", text)

    def test_a_hostile_roster_KEY_never_enters_the_census_roll(self):
        """The registry is keyed by (seat, pid) in a FILENAME, so the roll
        refuses any name outside the roster's own alphabet outright."""
        self.roster(self.HOSTILE, "alpha")
        self.assertEqual(beacons.roll(), ["alpha"])


class CensusCliTest(Base):
    def test_the_verb_refuses_junk_before_doing_any_work(self):
        import io
        import contextlib
        err = io.StringIO()
        with contextlib.redirect_stderr(err), \
                mock.patch.object(beacons, "census") as spy:
            rc = beacons.cmd_beacons(["--bogus"])
        self.assertEqual(rc, 2)
        self.assertFalse(spy.called)

    def test_json_carries_both_alarm_directions(self):
        import io
        import contextlib
        self.roster("alpha")
        self.waiter(211, "alpha", sid=SID_A)
        buf = io.StringIO()
        with mock.patch.object(beacons, "live_sessions", return_value={}), \
                mock.patch.object(beacons, "holder_from_records",
                                  return_value=(4242, 100)), \
                mock.patch.dict(os.environ, {"HELM_PROC": self.proc}), \
                contextlib.redirect_stdout(buf):
            rc = beacons.cmd_beacons(["--json"])
        self.assertEqual(rc, 0)
        got = json.loads(buf.getvalue())
        self.assertEqual([b["pid"] for b in got["ghosts"]], [211])
        self.assertEqual([r["seat"] for r in got["deaf"]], ["alpha"])


# ---------------------------------------------------------------------------
# the attendance register — the roster records who ANSWERED, not just who is
# DEFINED. Synthetic fixtures only: no real seat name, no host path.
# ---------------------------------------------------------------------------

class RegisterTest(Base):
    """`--post` writes each census verdict ONTO its roster row and posts DEAF
    edges to the fleet room. Every test asserts the EFFECT — the row in the
    file, the body in the post, the exit code — never the absence of a raise."""

    def post(self, live, now=None):
        import io
        import contextlib
        with mock.patch.object(beacons, "live_sessions", return_value=live), \
                mock.patch.dict(os.environ, {"HELM_PROC": self.proc}), \
                mock.patch("helm.chat.post") as posted, \
                contextlib.redirect_stdout(io.StringIO()):
            rc = beacons.cmd_beacons(["--post"])
        return rc, posted

    def read_roster(self):
        path = os.path.join(os.environ["HELM_CHAT_DIR"], ".roster.json")
        with open(path) as f:
            return json.load(f)

    def test_the_census_writes_attendance_INTO_the_roster(self):  # noqa: VACUOUS_ASSERTION — assert_not_called is the no-edge claim; the att-row assertIns are the positive control
        """The owner's literal ask: ONE sheet. The verdict, the seen beat, and
        the covered stamp land on the seat's OWN roster row."""
        self.roster("alpha")
        self.waiter(301, "alpha", sid=SID_A)
        self.agent(90, "alpha")
        rc, posted = self.post({SID_A: 90})
        self.assertEqual(rc, 0)                      # clean census, clean exit
        # noqa: VACUOUS_ASSERTION — the positive control on this same mock is
        # the lifecycle test's assert_called_once; here quiet IS the claim.
        posted.assert_not_called()                   # no edge -> no post
        att = self.read_roster()["alpha"]["attendance"]
        self.assertEqual(att["state"], beacons.COVERED)
        self.assertAlmostEqual(att["covered"], time.time(), delta=30)
        self.assertIsNotNone(att["seen"])            # the last-answered beat

    def test_case_variant_beacon_updates_the_enrolled_roster_spelling(self):
        """A registry row keeps launch-time display casing across a case-only
        roster rename. The census and attendance writer must resolve that held
        identity back to the enrolled key; exact `r.get(seat)` used to produce a
        real verdict for `kimi` and then write nothing to enrolled `Kimi`."""
        path = os.path.join(os.environ["HELM_CHAT_DIR"], ".roster.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump({"Kimi": {"session": SID_A, "sessions": [SID_A],
                                 "last_seen": 1.0}}, f)
        self.waiter(305, "kimi", sid=SID_A)
        with mock.patch.object(beacons, "live_sessions",
                               return_value={SID_A: 90}), \
                mock.patch.dict(os.environ, {"HELM_PROC": self.proc}):
            beacons.register("kimi", session=SID_A, pid=305,
                             proc_dir=self.proc, now=1200.0)
            rep = beacons.census()
            out = beacons.attend(rep, now=1234.0)
        self.assertEqual([r["seat"] for r in rep["seats"]], ["Kimi"])
        self.assertEqual(out["written"], ["Kimi"])
        self.assertEqual(self.read_roster()["Kimi"]["attendance"]["at"], 1234.0)
        # Independent writer control: even an explicitly requested case variant
        # must resolve to the enrolled key rather than being silently skipped.
        variant = {"seats": [dict(rep["seats"][0], seat="kimi")]}
        out = beacons.attend(variant, now=1235.0)
        self.assertEqual(out["written"], ["Kimi"])
        self.assertEqual(self.read_roster()["Kimi"]["attendance"]["at"], 1235.0)

    def test_since_holds_while_the_state_holds_and_covered_never_advances_on_DEAF(self):
        self.roster("alpha")                         # present, no beacon: DEAF
        with mock.patch.object(beacons, "live_sessions", return_value={}), \
                mock.patch.dict(os.environ, {"HELM_PROC": self.proc}):
            beacons.attend(beacons.census(), now=1000.0)
            beacons.attend(beacons.census(), now=2000.0)
        att = self.read_roster()["alpha"]["attendance"]
        self.assertEqual(att["state"], beacons.DEAF)
        self.assertEqual(att["since"], 1000.0)       # carried, not restamped
        self.assertEqual(att["at"], 2000.0)
        self.assertIsNone(att["covered"])            # DEAF never proves answer

    def test_the_DEAF_edge_lifecycle_post_latch_silence_recover(self):  # noqa: VACUOUS_ASSERTION — pass 2's assert_called_once is the positive control for pass 3's latch silence
        """Enter posts ONCE naming the seat and how long unreachable; a still-
        deaf pass reposts NOTHING; recovery posts and re-latches. The latch is
        the register itself — no second state file."""
        self.roster("alpha")
        wdir = self.waiter(302, "alpha", sid=SID_A)
        adir = self.agent(90, "alpha")
        rc, posted = self.post({SID_A: 90})          # pass 1: covered
        self.assertEqual(rc, 0)
        shutil.rmtree(wdir)                          # the pane and its beacon
        shutil.rmtree(adir)                          # die together
        rc, posted = self.post({})                   # pass 2: the edge
        self.assertEqual(rc, 1)                      # faults FOUND, not broken
        posted.assert_called_once()
        body = posted.call_args[0][0]
        self.assertIn("DEAF SEAT alpha", body)
        self.assertIn("unreachable for", body)       # anchored on covered
        self.assertEqual(posted.call_args.kwargs["room"], "helm")
        att = self.read_roster()["alpha"]["attendance"]
        self.assertEqual(att["alerted"], beacons.DEAF)
        rc, posted = self.post({})                   # pass 3: still deaf
        self.assertEqual(rc, 1)
        # noqa: VACUOUS_ASSERTION — pass 2's assert_called_once above is the
        # unconditional positive control on this same observable.
        posted.assert_not_called()                   # latched: one post per edge
        self.waiter(303, "alpha", sid=SID_A)         # pass 4: relaunched
        self.agent(91, "alpha")
        rc, posted = self.post({SID_A: 91})
        self.assertEqual(rc, 0)
        body = posted.call_args[0][0]
        self.assertIn("answers again", body)
        att = self.read_roster()["alpha"]["attendance"]
        self.assertEqual(att["alerted"], beacons.COVERED)

    def test_a_failed_post_leaves_the_edge_armed_for_the_next_pass(self):  # noqa: VACUOUS_ASSERTION — the None latch is the claim; the re-post assert_called_once is the positive control
        """At-least-once: the latch only advances AFTER delivery, and a mute
        watchdog exits 2 — BROKEN — never 1, which the unit declares success."""
        import io
        import contextlib
        self.roster("alpha")                         # present, no beacon: DEAF
        with mock.patch.object(beacons, "live_sessions", return_value={}), \
                mock.patch.dict(os.environ, {"HELM_PROC": self.proc}), \
                mock.patch("helm.chat.post", side_effect=RuntimeError("down")), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = beacons.cmd_beacons(["--post"])
        self.assertEqual(rc, 2)
        # noqa: VACUOUS_ASSERTION — the None IS the latch staying armed, and
        # the re-post below is the positive control that proves it mattered.
        self.assertIsNone(self.read_roster()["alpha"]["attendance"]["alerted"])
        rc, posted = self.post({})                   # chat is back
        posted.assert_called_once()                  # the edge re-detected
        self.assertIn("DEAF SEAT alpha", posted.call_args[0][0])

    def test_a_register_that_cannot_be_written_is_exit_2_and_says_so(self):  # noqa: VACUOUS_ASSERTION — rc==2 + the stderr assertIn are the positive controls; silence is fail-closed
        import io
        import contextlib
        self.roster("alpha")
        err = io.StringIO()
        with mock.patch.object(beacons, "live_sessions", return_value={}), \
                mock.patch.dict(os.environ, {"HELM_PROC": self.proc}), \
                mock.patch("helm.seats._flocked",
                           side_effect=OSError("disk full")), \
                mock.patch("helm.chat.post") as posted, \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            rc = beacons.cmd_beacons(["--post"])
        self.assertEqual(rc, 2)
        self.assertIn("register write failed", err.getvalue())
        # noqa: VACUOUS_ASSERTION — the stderr assertIn above is the positive
        # control; silence on the chat channel is the fail-closed claim.
        posted.assert_not_called()                   # no record -> no alert

    def test_the_register_never_mints_a_roster_row(self):
        """A beacon-held seat that nobody enrolled is censused but not
        attendance-tracked: the roster records who is DEFINED, and the census
        must never define anybody."""
        self.roster("alpha")
        self.waiter(304, "stray", sid=SID_A)         # roll()s in via its beacon
        rc, _ = self.post({SID_A: 90})
        self.assertEqual(sorted(self.read_roster()), ["alpha"])


class RollGraceTest(Base):
    """Row #96: presence alone SELF-SILENCES the alarm. A seat that goes deaf
    also stops stamping its beat, so QUIET_S later it read absent and vanished
    from the census at exactly the moment it most needed to be in it."""

    def write_roster(self, rows):
        path = os.path.join(os.environ["HELM_CHAT_DIR"], ".roster.json")
        with open(path, "w") as f:
            json.dump(rows, f)

    def test_a_deaf_and_starved_seat_STAYS_on_the_roll_within_grace(self):
        self.write_roster({"alpha": {
            "last_seen": 1.0,                        # starved: long absent
            "attendance": {"state": beacons.DEAF,
                           "covered": time.time() - 3600}}})
        self.assertEqual(beacons.roll(), ["alpha"])

    def test_the_graveyard_stays_OFF_the_roll(self):
        """The 179-dead-rows lesson holds: an answer older than the grace —
        or no attendance at all, or a junk stamp — never re-admits a row.
        The `control` row is the MUST-HIT that proves this scan scanned: it
        differs from `aged` only in its covered stamp's age, so an empty
        result here is a verdict and not a probe that never looked.

        `aged-standing` is the same control for the SECOND grace stamp: the
        stamp that keeps an unprovable seat on the roll must age off exactly
        like the first one, or the fix for the self-silencing roll becomes the
        179-dead-rows bug wearing the other sign."""
        old = time.time() - beacons.ATTEND_GRACE_S - 10
        self.write_roster({
            "aged": {"last_seen": 1.0,
                     "attendance": {"state": beacons.DEAF, "covered": old}},
            "aged-standing": {"last_seen": 1.0,
                              "attendance": {"state": beacons.UNPROVEN,
                                             "standing": old}},
            "never": {"last_seen": 1.0},
            "junk": {"last_seen": 1.0,
                     "attendance": {"state": beacons.DEAF, "covered": "soon",
                                    "standing": "soon"}},
            "control": {"last_seen": 1.0,
                        "attendance": {"state": beacons.DEAF,
                                       "covered": time.time() - 60}}})
        self.assertEqual(beacons.roll(), ["control"])

    def test_an_UNPROVABLE_seat_STAYS_on_the_roll_after_its_beat_decays(self):
        """THE SELF-SILENCING ROLL, measured 2026-08-03.

        `covered` only ever advances on a COVERED verdict, so a seat the
        instruments cannot classify never earned a grace stamp: ~QUIET_S after
        its beat stopped it dropped off the census entirely — no line, no
        register row, no alarm — and the bug therefore survived for exactly
        the seats the instruments are weakest on (the non-claude families,
        whose session liveness helm cannot prove even when they are healthy).

        `starved` is the MUST-HIT control: identical but for the attendance
        row, it proves the beat really has decayed past the presence cut and
        that this roll is not simply admitting everybody."""
        self.write_roster({
            "unprovable": {"last_seen": 1.0,        # long past QUIET_S
                           "attendance": {"state": beacons.UNPROVEN,
                                          "covered": None,
                                          "standing": time.time() - 3600}},
            "starved": {"last_seen": 1.0}})
        self.assertEqual(beacons.roll(), ["unprovable"])

    def test_the_vanishing_alarm_is_closed_end_to_end(self):
        """The 2026-08-03 morning, in miniature: a covered seat dies, its beat
        decays past QUIET_S — and the census still names it DEAF, because the
        register remembers it answered this morning."""
        import io
        import contextlib
        self.write_roster({"alpha": {"session": SID_A,
                                     "last_seen": time.time()}})
        wdir = self.waiter(305, "alpha", sid=SID_A)
        adir = self.agent(90, "alpha")
        with mock.patch.object(beacons, "live_sessions",
                               return_value={SID_A: 90}), \
                mock.patch.dict(os.environ, {"HELM_PROC": self.proc}), \
                mock.patch("helm.chat.post"), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(beacons.cmd_beacons(["--post"]), 0)
        shutil.rmtree(wdir)                          # 03:44: the pty daemon
        shutil.rmtree(adir)                          # takes every pane with it
        path = os.path.join(os.environ["HELM_CHAT_DIR"], ".roster.json")
        with open(path) as f:
            rows = json.load(f)
        rows["alpha"]["last_seen"] = time.time() - 3600   # the beat decays
        with open(path, "w") as f:
            json.dump(rows, f)
        with mock.patch.object(beacons, "live_sessions", return_value={}), \
                mock.patch.dict(os.environ, {"HELM_PROC": self.proc}), \
                mock.patch("helm.chat.post") as posted, \
                contextlib.redirect_stdout(io.StringIO()):
            rc = beacons.cmd_beacons(["--post"])
        self.assertEqual(rc, 1)
        posted.assert_called_once()
        self.assertIn("DEAF SEAT alpha", posted.call_args[0][0])


class UnreachableAlarmTest(Base):
    """THE 03:44 REPLAY. On 2026-08-03 the orca PTY daemon died and took all
    seven panes with it, and nobody noticed for four hours. The census was the
    one instrument still correct — and every surface it could shout on was
    read by the seats that had just died. These tests hold the rule that
    outage wrote: AN ALARM ABOUT THE FLEET BEING UNREACHABLE MUST NOT DEPEND
    ON A FLEET MEMBER BEING REACHABLE."""

    def post(self, live, push=True, configured=True):
        """One `--post` pass. Returns (rc, chat_post_mock, owner_push_mock).

        `configured` is the PHONE CHANNEL's existence, and it is separate from
        `push` on purpose: an opted-out fleet returns True from `owner_push`
        exactly like a delivered one (notify's docstring: "delivered, OR
        deliberately opted out"), so push=True/configured=False is the one
        combination that distinguishes a real send from a no-op."""
        import io
        import contextlib
        self.err = io.StringIO()
        with mock.patch.object(beacons, "live_sessions", return_value=live), \
                mock.patch.dict(os.environ, {"HELM_PROC": self.proc}), \
                mock.patch("helm.chat.post") as posted, \
                mock.patch("helm.notify.owner_push",
                           return_value=push) as pushed, \
                mock.patch("helm.notify.configured",
                           return_value=configured), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(self.err):
            rc = beacons.cmd_beacons(["--post"])
        return rc, posted, pushed

    def read_roster(self):
        path = os.path.join(os.environ["HELM_CHAT_DIR"], ".roster.json")
        with open(path) as f:
            return json.load(f)

    def test_the_alarm_reaches_the_OWNERS_PHONE_not_only_the_room(self):
        """The refuter's finding, in one test: the old escalation would have
        named the incident within five minutes and woken NOBODY, because it
        posted only to #helm — where every reader was one of the dead seats."""
        self.roster("alpha", "beta", "gamma")        # present, no beacons
        rc, posted, pushed = self.post({})
        self.assertEqual(rc, 1)                      # faults FOUND, not broken
        posted.assert_called_once()                  # the room still gets it
        pushed.assert_called_once()                  # and so does the phone
        body = pushed.call_args[0][0]
        for seat in ("alpha", "beta", "gamma"):
            self.assertIn(seat, body)
        self.assertIn("3 seats UNREACHABLE", body)   # ONE push for the fleet
        self.assertIn("unreachable",
                      pushed.call_args.kwargs["title"].lower())

    def test_a_seat_the_instruments_CANNOT_CLASSIFY_still_wakes_the_owner(self):
        """The seats this bug survived for. A waiter whose session helm cannot
        prove live reads UNPROVEN — never DEAF — and UNPROVEN posted nothing at
        all, so the 2-of-9 seats in that state were silent by construction. No
        proven wake path is an alarm whatever the verdict is called."""
        self.roster("alpha")
        self.waiter(401, "alpha", sid=SID_B)         # a live-shaped waiter…
        with mock.patch.object(beacons, "holder_from_records",
                               return_value=None):  # …whose session is unknown
            rc, posted, pushed = self.post({})
        self.assertEqual(rc, 1)
        att = self.read_roster()["alpha"]["attendance"]
        self.assertEqual(att["state"], beacons.UNPROVEN)  # still not called DEAF
        self.assertIn("PROVEN live", att["why"])     # the register keeps why
        self.assertTrue(att["alarm"])                # and alarms anyway
        posted.assert_called_once()
        self.assertIn("UNREACHABLE SEAT alpha", posted.call_args[0][0])
        pushed.assert_called_once()
        self.assertIn("alpha", pushed.call_args[0][0])

    def test_an_UNPROVABLE_seat_survives_its_own_beat_decaying(self):
        """THE VANISHING ALARM, for the seat class it actually killed — end to
        end through the WRITER, not through a hand-written fixture row.

        A seat helm cannot classify never reaches COVERED, so before this it
        never earned a grace stamp: ~QUIET_S after its beat stopped it left
        the roll and every later census was silent about it. Here the register
        must stamp `standing` itself on pass 1, and pass 2 — after the beat has
        decayed past the presence cut — must still be measuring the seat."""
        self.roster("alpha")
        self.waiter(403, "alpha", sid=SID_B)         # unprovable, not dead
        path = os.path.join(os.environ["HELM_CHAT_DIR"], ".roster.json")
        with mock.patch.object(beacons, "holder_from_records",
                               return_value=None):
            rc, _posted, pushed = self.post({})
            self.assertEqual(rc, 1)
            first = self.read_roster()["alpha"]["attendance"]
            self.assertTrue(first["standing"])       # the WRITER stamps it
            pushed.assert_called_once()              # and the owner was woken
            with open(path) as f:                    # 03:44 + QUIET_S: the
                rows = json.load(f)                  # beat stops arriving
            rows["alpha"]["last_seen"] = time.time() - 3600
            with open(path, "w") as f:
                json.dump(rows, f)
            self.assertEqual(seats.presence_of(rows["alpha"]["last_seen"]),
                             "absent")               # MUST-HIT: it really did
            rc, _posted2, _pushed2 = self.post({})
        self.assertEqual(rc, 1)                      # still a FAULT, not clean
        second = self.read_roster()["alpha"]["attendance"]
        self.assertGreater(second["at"], first["at"])       # still censused
        self.assertEqual(second["standing"], first["standing"])  # not refreshed
        self.assertEqual(second["state"], beacons.UNPROVEN)
        self.assertIn("PROVEN live", second["why"])         # a real verdict
        self.assertTrue(second["alarm"])                    # still alarming

    def test_a_failed_push_RE_PUSHES_without_re_posting_to_the_room(self):  # noqa: VACUOUS_ASSERTION — pass 1's assert_called_once on the SAME chat mock (and assertTrue(alarmed) on the same register row) are the unconditional positive controls; pass 2's silence IS the per-channel claim
        """PER-CHANNEL at-least-once. A dead phone must not re-post to a
        healthy room, nor the reverse — one latch each, each advanced only by
        its own delivery."""
        self.roster("alpha")
        rc, posted, pushed = self.post({}, push=False)
        posted.assert_called_once()                  # the room has the edge
        pushed.assert_called_once()                  # the phone did not
        self.assertIn("owner push FAILED", self.err.getvalue())
        att = self.read_roster()["alpha"]["attendance"]
        self.assertTrue(att["alarmed"])              # chat latched
        self.assertFalse(att["pushed"])              # phone still armed
        rc, posted, pushed = self.post({}, push=True)
        # noqa: VACUOUS_ASSERTION — the pass-1 assert_called_once above is the
        # unconditional positive control on this same chat mock.
        posted.assert_not_called()                   # the room is not spammed
        pushed.assert_called_once()                  # the phone is retried
        self.assertTrue(self.read_roster()["alpha"]["attendance"]["pushed"])

    def test_a_recovered_fleet_pushes_once_and_then_goes_quiet(self):  # noqa: VACUOUS_ASSERTION — the assertIn on pass 2's push body is the unconditional positive control on the same observable; pass 3's silence is the latch claim
        self.roster("alpha")
        self.post({})                                # the edge
        wdir = self.waiter(402, "alpha", sid=SID_A)  # relaunched
        self.agent(90, "alpha")
        rc, posted, pushed = self.post({SID_A: 90})
        self.assertEqual(rc, 0)
        self.assertIn("reachable again", pushed.call_args[0][0])
        rc, posted, pushed = self.post({SID_A: 90})
        self.assertEqual(rc, 0)
        # noqa: VACUOUS_ASSERTION — the call_args assertion above is the
        # positive control; latched silence is the claim here.
        pushed.assert_not_called()
        self.assertTrue(os.path.isdir(wdir))         # nothing was signaled

    def test_an_OPTED_OUT_phone_names_itself_instead_of_reading_as_delivered(self):  # noqa: VACUOUS_ASSERTION — the two stderr assertIns are the positive controls that the alarm RAN; the twin test_the_alarm_reaches_the_OWNERS_PHONE_not_only_the_room proves the same edge DOES push when the channel exists
        """A half-working alarm is worse than none. An unset topic returns
        DELIVERED (deliberate opt-out), so the only way this stops reading as
        success is for the verb to say it on the surface a human is looking
        at — with the KEY NAME, never a value."""
        import io
        import contextlib
        self.roster("alpha")
        err = io.StringIO()
        with mock.patch.object(beacons, "live_sessions", return_value={}), \
                mock.patch.dict(os.environ, {"HELM_PROC": self.proc,
                                             "HELM_NTFY_TOPIC": "",
                                             "MELD_NTFY_TOPIC": ""}), \
                mock.patch("helm.chat.post"), \
                mock.patch("urllib.request.urlopen") as urlopen, \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            beacons.cmd_beacons(["--post"])
        self.assertIn("phone channel OFF", err.getvalue())
        self.assertIn("HELM_NTFY_TOPIC", err.getvalue())
        urlopen.assert_not_called()                  # opted out = no network

    def test_an_OPTED_OUT_phone_does_not_LATCH_a_push_that_never_left(self):  # noqa: VACUOUS_ASSERTION — PASS 2 is the unconditional control on the SAME observable (a real delivery must latch `pushed`), plus assertTrue(att['alarm']) proves an edge existed to latch; mutation-verified 2026-08-04 ('True is not false')
        """The STATE-MACHINE half of the test above, which fixed only the eyes.

        `owner_push` returns True for two different worlds — its own docstring
        says "delivered, OR deliberately opted out (no topic: nothing to
        retry)" — and `pushed` means "the alarm value the last successful PHONE
        push carried". Latching on that True recorded a push that never left.

        THE HARM IS NOT PERMANENT SILENCE, and stating it precisely is what
        makes this testable: the next EDGE still pushes. What was lost is the
        STANDING alarm at configure time. The fleet goes down while the phone
        is off, the edge latches as though delivered, the owner then sets a
        topic — and hears nothing about the fleet that is STILL down, because
        no new transition exists to carry it.

        So the sequence below is the whole contract, and pass 2 doubles as the
        POSITIVE CONTROL on the same observable: if the latch were broken to
        never fire, pass 2 would fail too.
            pass 1, channel OFF -> nothing sent, so nothing latches
            pass 2, channel ON  -> the SAME edge is still armed, pushes, latches
        That is also exactly what this module already promises a FAILED push
        ("it stays armed and re-pushes next pass"). An opt-out sends strictly
        less than a failed push and must not be treated better than one."""
        import io
        import contextlib
        self.roster("alpha")
        with mock.patch.object(beacons, "live_sessions", return_value={}), \
                mock.patch.dict(os.environ, {"HELM_PROC": self.proc,
                                             "HELM_NTFY_TOPIC": "",
                                             "MELD_NTFY_TOPIC": ""}), \
                mock.patch("helm.chat.post"), \
                mock.patch("urllib.request.urlopen") as urlopen, \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            beacons.cmd_beacons(["--post"])
        urlopen.assert_not_called()          # control: genuinely opted out
        att = self.read_roster()["alpha"]["attendance"]
        self.assertTrue(att["alarm"],
                        "no alarm edge was produced — nothing to latch, so "
                        "the assertion below would be vacuous")
        self.assertFalse(att["pushed"],
                         "an opted-out pass latched `pushed`: the register now "
                         "records a phone push that never left the machine")

        # PASS 2 — the channel exists now. The edge must still be armed.
        _rc, _posted, pushed = self.post({})
        pushed.assert_called_once()
        self.assertTrue(self.read_roster()["alpha"]["attendance"]["pushed"],
                        "a real delivery did not latch — the latch is broken "
                        "in the other direction and pass 1 proved nothing")

    def test_a_RECOVERY_edge_survives_an_opted_out_window(self):  # noqa: VACUOUS_ASSERTION — pass 3's assert_called_once + the "reachable again" body match are unconditional positive controls on the SAME push channel; the two assertFalse calls are guarded by the fixture MUST-HIT directly above each
        """a6d5d95f's stale-latch wording, pointed at the OTHER edge value —
        the repro attempt task #199 required before touching anything, banked
        as a pin because it did NOT reproduce (2104f13a already holds).

        `pushed` latched True by a delivered alarm, then the owner opts out,
        then the fleet RECOVERS inside the opt-out window. The recovery edge
        must ride the stale-but-armed latch out of the window: pushed stays
        True through the opted-out pass (nothing left the machine, so nothing
        may advance) and the \"reachable again\" push fires the moment the
        channel returns."""
        self.roster("alpha")
        self.post({})                                # alarm, channel ON
        self.assertTrue(self.read_roster()["alpha"]["attendance"]["pushed"])
        self.waiter(402, "alpha", sid=SID_A)         # relaunched…
        self.agent(90, "alpha")
        _rc, _posted, pushed = self.post({SID_A: 90}, configured=False)
        att = self.read_roster()["alpha"]["attendance"]
        self.assertFalse(att["alarm"], "the fixture failed to recover — "
                         "every assertion below would be vacuous")
        self.assertTrue(att["pushed"],
                        "an opted-out recovery advanced the latch: the "
                        "register recorded a push that never left")
        _rc, _posted, pushed = self.post({SID_A: 90})
        pushed.assert_called_once()                  # the edge survived
        self.assertIn("reachable again", pushed.call_args[0][0])
        self.assertFalse(self.read_roster()["alpha"]["attendance"]["pushed"])


class ConcurrentPassDeliveryEdgeTest(Base):
    """a6d5d95f's remaining finding: "concurrent passes deliver same edge
    twice". `attend` derives the transition batch under the roster lock, but
    delivery ran outside any lock — so a pass whose attend() landed inside
    another pass's attend-to-ack window derived the SAME edge, and the owner
    heard every alarm twice on both channels. Reproduced on this tree
    2026-08-05 before the fix: chat.post 2x, owner_push 2x for one edge.

    The cure is revalidation where the batch is SPENT: one lock spans
    revalidate -> deliver -> ack, each row re-checked against the roster's
    CURRENT latch. These tests pin the exactly-once outcome, the per-channel
    at-least-once retry that revalidation must NOT break, the fail-closed
    refusal when the lock cannot be opened, and the lock actually being HELD
    across delivery (without which revalidation is a smaller window, not a
    closed one)."""

    def edge_pass(self):
        """census+attend once — one pass's derivation, no delivery."""
        rep = beacons.census()
        return rep, beacons.attend(rep)

    def test_a_batch_derived_inside_anothers_window_delivers_ONCE(self):
        self.roster("alpha")                         # present, no beacon: DEAF
        with mock.patch.object(beacons, "live_sessions", return_value={}), \
                mock.patch.dict(os.environ, {"HELM_PROC": self.proc}), \
                mock.patch("helm.chat.post") as posted, \
                mock.patch("helm.notify.owner_push",
                           return_value=True) as pushed, \
                mock.patch("helm.notify.configured", return_value=True):
            rep, reg_a = self.edge_pass()            # pass A opens the window
            _rep, reg_b = self.edge_pass()           # pass B lands INSIDE it
            self.assertEqual(len(reg_b["transitions"]), 1,
                             "pass B did not re-derive the edge — the window "
                             "is closed upstream and this test measures "
                             "nothing")
            beacons.escalate(reg_a["transitions"], rep)
            beacons.escalate(reg_b["transitions"], rep)
        posted.assert_called_once()                  # the room hears it ONCE
        pushed.assert_called_once()                  # the phone buzzes ONCE
        att = self.read_att()
        self.assertTrue(att["alarmed"] and att["pushed"],
                        "exactly-once was achieved by delivering ZERO — the "
                        "latches never advanced")

    def test_a_FAILED_post_still_retries_while_the_delivered_push_does_not(self):  # noqa: VACUOUS_ASSERTION — posted.call_count==2 and the alarmed-latch flip are unconditional positive controls on the same observables; the mid-test assertFalse is the failed-leg ground truth its control sits directly below
        """Revalidation must collapse only edges that LANDED. Pass A's chat
        post fails (latch un-advanced) while its push delivers (latch
        advanced): pass B must retry exactly the failed leg."""
        self.roster("alpha")
        with mock.patch.object(beacons, "live_sessions", return_value={}), \
                mock.patch.dict(os.environ, {"HELM_PROC": self.proc}), \
                mock.patch("helm.chat.post",
                           side_effect=[OSError("tmpfs gone"),
                                        None]) as posted, \
                mock.patch("helm.notify.owner_push",
                           return_value=True) as pushed, \
                mock.patch("helm.notify.configured", return_value=True):
            rep, reg_a = self.edge_pass()
            _rep, reg_b = self.edge_pass()
            out_a = beacons.escalate(reg_a["transitions"], rep)
            self.assertFalse(out_a["chat"])          # A's room leg FAILED
            self.assertFalse(self.read_att()["alarmed"])
            out_b = beacons.escalate(reg_b["transitions"], rep)
            self.assertTrue(out_b["chat"])
        self.assertEqual(posted.call_count, 2,       # attempt + retry
                         "the failed chat leg was collapsed with the "
                         "delivered one — revalidation broke at-least-once")
        pushed.assert_called_once()                  # the delivered leg is not
        att = self.read_att()                        # re-pushed by pass B
        self.assertTrue(att["alarmed"], "the retry did not latch")
        self.assertTrue(att["pushed"])

    def test_an_UNOPENABLE_lock_refuses_delivery_instead_of_racing(self):  # noqa: VACUOUS_ASSERTION — the self-heal control below drives the SAME cmd to a real delivery, so the refusal's assert_not_called measures the guard and not a dead verb
        """Fail-closed, through the WIRE (`cmd_beacons`), because a correct
        guard and an unreached call site are indistinguishable from the
        helper's own tests — this suite's own mutation lesson. Contention
        BLOCKS (a concurrent pass is bounded); only a lock that cannot be
        OPENED lands here, and it must refuse with its own words, not wear
        "chat post failed"."""
        import contextlib
        self.roster("alpha")
        real = seats._flocked
        escalation_calls = []

        def sel(path, *a, **kw):
            if path.endswith(".escalate.lock"):
                escalation_calls.append(path)
                # attend takes the same outer lock now; let it commit the edge,
                # then fail the delivery acquisition this test is about.
                if len(escalation_calls) == 2:
                    return LockFailureMustNotWriteUnlockedTest._Unlockable()
            return real(path, *a, **kw)
        err = io.StringIO()
        with mock.patch.object(beacons, "live_sessions", return_value={}), \
                mock.patch.dict(os.environ, {"HELM_PROC": self.proc}), \
                mock.patch.object(seats, "_flocked", side_effect=sel), \
                mock.patch("helm.chat.post") as posted, \
                mock.patch("helm.notify.owner_push",
                           return_value=True) as pushed, \
                mock.patch("helm.notify.configured", return_value=True), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            rc = beacons.cmd_beacons(["--post"])
        self.assertEqual(rc, 2)                      # the watchdog SAYS broken
        self.assertIn("escalation lock unavailable", err.getvalue())
        self.assertNotIn("chat post failed", err.getvalue())
        posted.assert_not_called()                   # nothing raced out
        pushed.assert_not_called()
        att = self.read_att()
        self.assertFalse(att["alarmed"] or att["pushed"],
                         "a refused delivery advanced a latch")
        # SELF-HEAL CONTROL on the same observables: the next ordinary pass
        # (real locks) delivers the edge the refusal preserved.
        with mock.patch.object(beacons, "live_sessions", return_value={}), \
                mock.patch.dict(os.environ, {"HELM_PROC": self.proc}), \
                mock.patch("helm.chat.post") as posted, \
                mock.patch("helm.notify.owner_push",
                           return_value=True) as pushed, \
                mock.patch("helm.notify.configured", return_value=True), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            beacons.cmd_beacons(["--post"])
        posted.assert_called_once()
        pushed.assert_called_once()

    def test_delivery_runs_with_the_escalation_lock_HELD(self):
        """The exactly-once test above passes for a LOCKLESS revalidation too
        (sequential interleaves cannot see the mutex). This one can: a probe
        inside the delivery leg tries the lock non-blocking, and contention —
        flock denies a second descriptor even within one process — is the
        proof the lock spans delivery rather than just the revalidation."""
        import fcntl
        self.roster("alpha")
        held = []

        def probe(*_a, **_k):
            with open(seats.roster_path() + ".escalate.lock") as f:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    held.append(True)
                else:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                    held.append(False)
        with mock.patch.object(beacons, "live_sessions", return_value={}), \
                mock.patch.dict(os.environ, {"HELM_PROC": self.proc}), \
                mock.patch("helm.chat.post", side_effect=probe), \
                mock.patch("helm.notify.owner_push", return_value=True), \
                mock.patch("helm.notify.configured", return_value=True):
            rep, reg = self.edge_pass()
            out = beacons.escalate(reg["transitions"], rep)
        self.assertEqual(held, [True],
                         "delivery ran without the escalation lock held — "
                         "the revalidate-deliver-ack window is open again")
        self.assertTrue(out["chat"], "the probed delivery leg FAILED — "
                        "`held` was recorded on a pass that delivered nothing")
        self.assertTrue(self.read_att()["alarmed"],
                        "the probed delivery did not latch")

    def read_att(self):
        path = os.path.join(os.environ["HELM_CHAT_DIR"], ".roster.json")
        with open(path) as f:
            return json.load(f)["alpha"]["attendance"]


class StaleBatchOppositeEdgeTest(Base):
    """THE OPPOSITE EDGE — a review's amendment to the delivery-edge fix, and
    the half latch revalidation could not see.

    Re-checking only the LATCHES asks "has anyone delivered this yet?" and
    never "is this still TRUE?". Reproduced on this tree before the fix, both
    polarities:

      ALARM:    pass A derives alarm=True; the seat RECOVERS; pass B writes
                covered/alarm=False and derives NO edge (both latches are
                still false, so nothing is owed); A's batch passes latch
                revalidation and posts "DEAF SEAT alpha ... helm cannot wake
                it" onto a COVERED row, acking alarmed/pushed=True.
                Measured: recovery transitions 0, stale chat=1 push=1, final
                covered + alarm=False + alarmed=True + pushed=True.

      RECOVERY: the mirror, and the dangerous direction — a stale recovery
                batch delivered onto a row that has since gone DEAF again put
                "helm fleet: 1 seat reachable again" on the owner's phone
                while the seat was down, and reset BOTH latches to false.
                Measured: final DEAF + alarm=True + alarmed=False +
                pushed=False.

    The cure re-reads the row's CURRENT attendance and collapses a batch row
    unless the register still records the same (alarm, state). At-least-once
    is untouched: owedness is a property of the ROW — `attend` re-derives an
    edge every pass from (alarm vs alarmed) and (alarm vs pushed) — so a
    collapsed row that is genuinely still owed comes back on the next
    census, while a retried failed leg carries the verdict the register
    holds and survives."""

    def att(self, seat="alpha"):
        path = os.path.join(os.environ["HELM_CHAT_DIR"], ".roster.json")
        with open(path) as f:
            return json.load(f)[seat]["attendance"]

    def deaf_pass(self):
        """census+attend with nothing alive — one DEAF derivation, no delivery."""
        with mock.patch.object(beacons, "live_sessions", return_value={}), \
                mock.patch.dict(os.environ, {"HELM_PROC": self.proc}):
            rep = beacons.census()
            return rep, beacons.attend(rep)

    def covered_pass(self):
        """census+attend with the seat answering — the recovery derivation."""
        with mock.patch.object(beacons, "live_sessions",
                               return_value={SID_A: 90}), \
                mock.patch.dict(os.environ, {"HELM_PROC": self.proc}):
            rep = beacons.census()
            return rep, beacons.attend(rep)

    def revive(self):
        return (self.waiter(302, "alpha", sid=SID_A),
                self.agent(90, "alpha"))

    def test_attendance_cannot_commit_INSIDE_revalidate_deliver_ack(self):
        """The review FIX that the pre-delivery tests cannot see. `escalate`
        revalidated under `.escalate.lock`, but `attend` once wrote under only
        `.lock`, so a newer verdict could commit while the old post was leaving.
        The writer starts from INSIDE chat.post and must remain blocked through
        the phone leg and both acks; after release it sees the delivered alarm
        and derives the recovery that is genuinely owed."""
        import threading
        self.roster("alpha")
        rep_a, reg_a = self.deaf_pass()
        attempted, done = threading.Event(), threading.Event()
        result, failures, bodies = {}, [], []
        attempted_seen, during_chat, during_push = [], [], []

        def recover():
            attempted.set()
            try:
                self.revive()
                result["pass"] = self.covered_pass()
            except BaseException as exc:       # the main thread re-raises it
                failures.append(exc)
            finally:
                done.set()

        threads = []

        def post(body, **_kwargs):
            thread = threading.Thread(target=recover, daemon=True)
            threads.append(thread)
            thread.start()
            attempted_seen.append(attempted.wait(1))
            during_chat.append((done.wait(.2), self.att()["state"]))
            bodies.append(body)

        def push(*_args, **_kwargs):
            during_push.append((done.is_set(), self.att()["state"]))
            return True

        with mock.patch.object(beacons, "live_sessions", return_value={}), \
                mock.patch.dict(os.environ, {"HELM_PROC": self.proc}), \
                mock.patch("helm.chat.post", side_effect=post), \
                mock.patch("helm.notify.owner_push", side_effect=push), \
                mock.patch("helm.notify.configured", return_value=True):
            out = beacons.escalate(reg_a["transitions"], rep_a)
            # JOINED INSIDE THE PATCH SCOPE. The recovery thread opens its own
            # patch of `live_sessions` and HELM_PROC while this block's are
            # open. Leaving this block first restored the real function, and
            # the thread's later exit then restored THIS block's MagicMock,
            # which stayed bound on helm.beacons for every later module in
            # the process. Nested exits are the only order that unwinds.
            for thread in threads:
                thread.join(2)
        self.assertEqual(len(threads), 1, "the chat leg never ran")
        self.assertFalse(threads[0].is_alive(),
                         "attendance stayed blocked after escalation released")
        if failures:
            raise failures[0]
        self.assertEqual(attempted_seen, [True],
                         "the attendance writer never entered during chat.post")
        self.assertEqual(during_chat, [(False, beacons.DEAF)],
                         "attendance crossed the lock while chat was posting")
        self.assertEqual(during_push, [(False, beacons.DEAF)],
                         "attendance crossed the lock before the phone leg")
        self.assertIn("DEAF SEAT alpha", bodies[0])
        self.assertTrue(out["chat"] and out["push"])
        _rep_b, reg_b = result["pass"]
        self.assertEqual([t[1]["alarm"] for t in reg_b["transitions"]], [False],
                         "the recovery committed before the alarm acks and was "
                         "silently overwritten instead of becoming an owed edge")
        att = self.att()
        self.assertEqual((att["state"], att["alarm"]),
                         (beacons.COVERED, False))
        self.assertTrue(att["alarmed"] and att["pushed"],
                        "the delivered alarm vanished before recovery can clear it")

    def test_a_stale_ALARM_is_not_delivered_onto_a_RECOVERED_row(self):  # noqa: VACUOUS_ASSERTION — the control at the end drives the SAME chat/push mocks to a real delivery once the seat dies for real (assert_called_once + the DEAF SEAT body match + both latches True), so the assert_not_called measures the freshness rung and not a dead verb; the two fixture MUST-HITs above prove A derived an alarm and B derived nothing
        """The amendment's exact interleaving. The seat answers again before
        A's alarm ever leaves the machine, so the alarm is not news — it is
        false — and the owner must not be told a covered seat is dead."""
        self.roster("alpha")                         # present, no beacon: DEAF
        rep_a, reg_a = self.deaf_pass()              # A derives the alarm
        self.assertEqual([t[1]["alarm"] for t in reg_a["transitions"]], [True],
                         "pass A derived no alarm edge — the fixture never "
                         "produced the batch this test is about")
        self.revive()                                # the seat comes back…
        _rep_b, reg_b = self.covered_pass()          # …inside A's window
        self.assertEqual(self.att()["state"], beacons.COVERED)
        self.assertEqual(len(reg_b["transitions"]), 0,
                         "pass B derived a recovery edge — the premise of "
                         "this race (B is SILENT because both latches are "
                         "still false) does not hold and it measures nothing")
        with mock.patch.object(beacons, "live_sessions", return_value={}), \
                mock.patch.dict(os.environ, {"HELM_PROC": self.proc}), \
                mock.patch("helm.chat.post") as posted, \
                mock.patch("helm.notify.owner_push",
                           return_value=True) as pushed, \
                mock.patch("helm.notify.configured", return_value=True):
            out = beacons.escalate(reg_a["transitions"], rep_a)
            self.assertEqual(out["alarms"], 0,
                             "a collapsed row still counted as an alarm that "
                             "reached a channel")
            posted.assert_not_called()               # no "DEAF SEAT alpha"
            pushed.assert_not_called()               # no phone buzz
            att = self.att()
            self.assertFalse(att["alarmed"] or att["pushed"],
                             "a stale alarm latched onto the covered row")
            # POSITIVE CONTROL on the SAME mocks: the seat dies for real and
            # the very next pass delivers, so the silence above measures the
            # freshness rung and not a dead verb.
            shutil.rmtree(os.path.join(self.proc, "302"))
            shutil.rmtree(os.path.join(self.proc, "90"))
            rep_c = beacons.census()
            reg_c = beacons.attend(rep_c)
            beacons.escalate(reg_c["transitions"], rep_c)
        posted.assert_called_once()
        self.assertIn("DEAF SEAT alpha", posted.call_args[0][0])
        pushed.assert_called_once()
        self.assertTrue(self.att()["alarmed"] and self.att()["pushed"])

    def test_a_stale_RECOVERY_does_not_tell_the_owner_the_fleet_is_BACK(self):  # noqa: VACUOUS_ASSERTION — the control at the end delivers a REAL recovery on the SAME mocks (answers again / reachable again + both latches cleared); the assertTrue on the surviving latches is the positive half of the claim, and the DEAF SEAT assertIn above proves the alarm landed first
        """The mirror, and the direction that lets the owner go back to sleep:
        a recovery batch delivered onto a row that is DEAF again says "1 seat
        reachable again" about a seat that is down, and disarms both latches
        so the standing alarm reads as delivered-and-cleared."""
        self.roster("alpha")
        with mock.patch.object(beacons, "live_sessions", return_value={}), \
                mock.patch.dict(os.environ, {"HELM_PROC": self.proc}), \
                mock.patch("helm.chat.post") as posted, \
                mock.patch("helm.notify.owner_push", return_value=True), \
                mock.patch("helm.notify.configured", return_value=True):
            rep, reg = beacons.census(), None
            reg = beacons.attend(rep)
            beacons.escalate(reg["transitions"], rep)
        self.assertIn("DEAF SEAT alpha", posted.call_args[0][0])   # MUST-HIT:
        att = self.att()                             # the alarm really landed
        self.assertTrue(att["alarmed"] and att["pushed"])
        wdir, adir = self.revive()                   # A derives the RECOVERY
        rep_a, reg_a = self.covered_pass()
        self.assertEqual([t[1]["alarm"] for t in reg_a["transitions"]], [False],
                         "pass A derived no recovery edge — the fixture never "
                         "produced the batch this test is about")
        shutil.rmtree(wdir)                          # the seat dies AGAIN
        shutil.rmtree(adir)                          # inside A's window
        _rep_b, reg_b = self.deaf_pass()
        self.assertEqual(self.att()["state"], beacons.DEAF)
        self.assertEqual(len(reg_b["transitions"]), 0,
                         "pass B derived an edge — both latches already carry "
                         "True, so this race requires B to be silent")
        with mock.patch.object(beacons, "live_sessions", return_value={}), \
                mock.patch.dict(os.environ, {"HELM_PROC": self.proc}), \
                mock.patch("helm.chat.post") as posted2, \
                mock.patch("helm.notify.owner_push",
                           return_value=True) as pushed2, \
                mock.patch("helm.notify.configured", return_value=True):
            beacons.escalate(reg_a["transitions"], rep_a)
            posted2.assert_not_called()
            pushed2.assert_not_called()
            att = self.att()
            self.assertTrue(att["alarmed"] and att["pushed"],
                            "a stale recovery DISARMED the standing alarm — "
                            "the next pass now owes nothing and the phone "
                            "stays quiet about a seat that is down")
            # POSITIVE CONTROL on the SAME mocks: a REAL recovery still
            # delivers "reachable again" on both channels.
            self.revive()
            with mock.patch.object(beacons, "live_sessions",
                                   return_value={SID_A: 90}):
                rep_c = beacons.census()
                reg_c = beacons.attend(rep_c)
                beacons.escalate(reg_c["transitions"], rep_c)
        self.assertIn("answers again", posted2.call_args[0][0])
        self.assertIn("reachable again", pushed2.call_args[0][0])
        self.assertFalse(self.att()["alarmed"] or self.att()["pushed"])

    def test_a_state_drift_at_EQUAL_alarm_collapses_the_stale_wording(self):  # noqa: VACUOUS_ASSERTION — pass B's own batch is delivered on the SAME chat mock immediately after and asserts the CORRECT sentence (UNREACHABLE SEAT, not DEAF SEAT), so the stale batch's silence is measured against a live verb
        """`state` is revalidated as well as `alarm`, because a DEAF->UNPROVEN
        drift keeps the alarm polarity while flipping the sentence between
        "helm cannot wake it" and "the instruments could not name it" — and at
        equal polarity the latch advances, so the wrong wording is never
        corrected by a later edge. It has to die at delivery or not at all."""
        self.roster("alpha")
        rep_a, reg_a = self.deaf_pass()              # A derives a DEAF alarm
        self.assertEqual([t[1]["state"] for t in reg_a["transitions"]],
                         [beacons.DEAF])
        self.waiter(401, "alpha", sid=SID_B)         # a live-SHAPED waiter…
        with mock.patch.object(beacons, "live_sessions", return_value={}), \
                mock.patch.dict(os.environ, {"HELM_PROC": self.proc}), \
                mock.patch.object(beacons, "holder_from_records",
                                  return_value=None):   # …session unknown
            rep_b = beacons.census()
            reg_b = beacons.attend(rep_b)
        self.assertEqual(self.att()["state"], beacons.UNPROVEN)
        self.assertTrue(self.att()["alarm"],          # SAME alarm polarity —
                        "the drift changed the alarm too; this test needs the "
                        "state to move while the polarity holds")
        with mock.patch.object(beacons, "live_sessions", return_value={}), \
                mock.patch.dict(os.environ, {"HELM_PROC": self.proc}), \
                mock.patch("helm.chat.post") as posted, \
                mock.patch("helm.notify.owner_push", return_value=True), \
                mock.patch("helm.notify.configured", return_value=True):
            beacons.escalate(reg_a["transitions"], rep_a)   # the STALE wording
            posted.assert_not_called()
            # POSITIVE CONTROL on the same mock: B's own batch, which the
            # register DOES still agree with, delivers the correct sentence.
            beacons.escalate(reg_b["transitions"], rep_b)
        posted.assert_called_once()
        self.assertIn("UNREACHABLE SEAT alpha", posted.call_args[0][0])
        self.assertNotIn("DEAF SEAT", posted.call_args[0][0])

    def test_an_alarm_drift_at_EQUAL_state_collapses_the_stale_alarm(self):  # noqa: VACUOUS_ASSERTION — the control at the end removes the live beacon and drives the SAME chat mock to assert_called_once with the latch advanced, so the collapse is measured and not assumed
        """The other half of the pair: `alarm` is NOT a function of `state`.
        UNPROVEN carries either polarity — no live beacon is an alarm, while a
        LIVE beacon behind a pane that declares no seat of its own is a
        working wake path that merely cannot be classified. So a row can hold
        its state and lose its alarm, and a check that compared only `state`
        would deliver "UNREACHABLE SEAT alpha" about a seat with a live
        beacon."""
        self.roster("alpha")
        wdir = self.waiter(401, "alpha", sid=SID_B)  # a live-SHAPED waiter…
        with mock.patch.object(beacons, "live_sessions", return_value={}), \
                mock.patch.dict(os.environ, {"HELM_PROC": self.proc}), \
                mock.patch.object(beacons, "holder_from_records",
                                  return_value=None):   # …session unknown
            rep_a = beacons.census()
            reg_a = beacons.attend(rep_a)
        self.assertEqual([(t[1]["state"], t[1]["alarm"])
                          for t in reg_a["transitions"]],
                         [(beacons.UNPROVEN, True)])
        shutil.rmtree(wdir)                          # the beacon comes back
        self.waiter(302, "alpha", sid=SID_A)         # LIVE this time, behind
        self.agent(90, seat=None)                    # a pane declaring no seat
        with mock.patch.object(beacons, "live_sessions",
                               return_value={SID_A: 90}), \
                mock.patch.dict(os.environ, {"HELM_PROC": self.proc}):
            _rep_b, reg_b = beacons.census(), None
            reg_b = beacons.attend(_rep_b)
        self.assertEqual(self.att()["state"], beacons.UNPROVEN)   # SAME state…
        self.assertFalse(self.att()["alarm"])        # …opposite alarm
        self.assertEqual(len(reg_b["transitions"]), 0,
                         "pass B derived an edge — this race needs B silent")
        with mock.patch.object(beacons, "live_sessions",
                               return_value={SID_A: 90}), \
                mock.patch.dict(os.environ, {"HELM_PROC": self.proc}), \
                mock.patch("helm.chat.post") as posted, \
                mock.patch("helm.notify.owner_push", return_value=True), \
                mock.patch("helm.notify.configured", return_value=True):
            beacons.escalate(reg_a["transitions"], rep_a)
            posted.assert_not_called()
            self.assertFalse(self.att()["alarmed"])
            # POSITIVE CONTROL on the same mock: when the seat genuinely loses
            # its wake path again, the very next pass DOES deliver.
            shutil.rmtree(os.path.join(self.proc, "302"))
            with mock.patch.object(beacons, "live_sessions", return_value={}):
                rep_c = beacons.census()
                reg_c = beacons.attend(rep_c)
                beacons.escalate(reg_c["transitions"], rep_c)
        posted.assert_called_once()
        self.assertIn("alpha", posted.call_args[0][0])
        self.assertTrue(self.att()["alarmed"])

    def test_an_UNCHANGED_verdict_rewritten_by_another_pass_still_DELIVERS(self):  # noqa: VACUOUS_ASSERTION — posted2.assert_called_once + the DEAF SEAT body + the advanced latch are the unconditional positive controls; pushed2.assert_not_called is the already-landed leg, whose ground truth (pushed True) is asserted directly above
        """The over-tight direction, and the one that would silently break
        at-least-once: freshness is about the VERDICT, not about the row. A
        concurrent pass that re-measures the SAME state rewrites `at` (and
        `seen`) on every row it touches, so a revalidation that compared the
        stored attendance to the batch's dict — or compared timestamps —
        would collapse every retry and drop the failed leg for good."""
        self.roster("alpha")
        with mock.patch.object(beacons, "live_sessions", return_value={}), \
                mock.patch.dict(os.environ, {"HELM_PROC": self.proc}), \
                mock.patch("helm.chat.post",
                           side_effect=OSError("tmpfs gone")) as posted, \
                mock.patch("helm.notify.owner_push", return_value=True), \
                mock.patch("helm.notify.configured", return_value=True):
            rep_a, reg_a = beacons.census(), None
            reg_a = beacons.attend(rep_a)
            out_a = beacons.escalate(reg_a["transitions"], rep_a)
        self.assertFalse(out_a["chat"])              # the room leg FAILED…
        self.assertFalse(self.att()["alarmed"])      # …so nothing latched
        self.assertTrue(self.att()["pushed"])        # the phone leg landed
        before = self.att()["at"]
        _rep_b, _reg_b = self.deaf_pass()            # same verdict, new stamps
        self.assertNotEqual(self.att()["at"], before,
                            "the intervening pass rewrote nothing — this test "
                            "cannot show that a rewrite is not staleness")
        self.assertEqual(self.att()["state"], beacons.DEAF)
        with mock.patch.object(beacons, "live_sessions", return_value={}), \
                mock.patch.dict(os.environ, {"HELM_PROC": self.proc}), \
                mock.patch("helm.chat.post") as posted2, \
                mock.patch("helm.notify.owner_push",
                           return_value=True) as pushed2, \
                mock.patch("helm.notify.configured", return_value=True):
            out = beacons.escalate(reg_a["transitions"], rep_a)
        self.assertTrue(out["chat"])
        posted2.assert_called_once()                 # the failed leg RETRIED
        self.assertIn("DEAF SEAT alpha", posted2.call_args[0][0])
        pushed2.assert_not_called()                  # the landed leg did not
        self.assertTrue(self.att()["alarmed"], "the retry did not latch")

    def test_a_collapsed_batch_does_not_claim_the_room_HEARD_the_alarm(self):  # noqa: VACUOUS_ASSERTION — the CONTROL arm runs first on the same stderr and asserts the note DOES fire for a real alarm (plus posted.assert_called_once), so the assertNotIn measures the recount
        """`alarms` drives the operator note "this alarm reached the fleet
        room ONLY", so it must count what SURVIVED revalidation rather than
        what was derived — otherwise the cure above buys silence on the wire
        and pays for it with a false line on the surface a human reads."""
        import contextlib
        self.roster("alpha")
        err = io.StringIO()
        with mock.patch.object(beacons, "live_sessions", return_value={}), \
                mock.patch.dict(os.environ, {"HELM_PROC": self.proc}), \
                mock.patch("helm.chat.post") as posted, \
                mock.patch("helm.notify.owner_push", return_value=True), \
                mock.patch("helm.notify.configured", return_value=False), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            rc = beacons.cmd_beacons(["--post"])     # CONTROL: a real alarm
        self.assertEqual(rc, 1)
        posted.assert_called_once()
        self.assertIn("reached the fleet room ONLY", err.getvalue())
        self.revive()                                # recover and clear it
        with mock.patch.object(beacons, "live_sessions",
                               return_value={SID_A: 90}), \
                mock.patch.dict(os.environ, {"HELM_PROC": self.proc}), \
                mock.patch("helm.chat.post"), \
                mock.patch("helm.notify.owner_push", return_value=True), \
                mock.patch("helm.notify.configured", return_value=True), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            beacons.cmd_beacons(["--post"])
        self.assertFalse(self.att()["alarmed"])      # the latch is clear again
        shutil.rmtree(os.path.join(self.proc, "302"))
        shutil.rmtree(os.path.join(self.proc, "90"))
        real_attend = beacons.attend

        def attend_then_recover(rep, now=None):
            """The seat answers again INSIDE the attend -> escalate window."""
            out = real_attend(rep, now=now)
            self.revive()
            with mock.patch.object(beacons, "live_sessions",
                                   return_value={SID_A: 90}):
                real_attend(beacons.census())
            return out
        err2 = io.StringIO()
        with mock.patch.object(beacons, "live_sessions", return_value={}), \
                mock.patch.dict(os.environ, {"HELM_PROC": self.proc}), \
                mock.patch.object(beacons, "attend",
                                  side_effect=attend_then_recover), \
                mock.patch("helm.chat.post") as posted2, \
                mock.patch("helm.notify.owner_push", return_value=True), \
                mock.patch("helm.notify.configured", return_value=False), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err2):
            beacons.cmd_beacons(["--post"])
        posted2.assert_not_called()                  # nothing was delivered…
        self.assertNotIn("reached the fleet room ONLY", err2.getvalue())
        self.assertEqual(self.att()["state"], beacons.COVERED)


class BeaconsTimerUnitTest(unittest.TestCase):
    def test_the_unit_calls_the_POSTING_form_and_declares_faults_success(self):
        """A timer on the read-only form would compute the answer and tell
        nobody; a unit without SuccessExitStatus=1 shows `failed (exit-code
        1)` for a watchdog that is WORKING — proxywatch did, all through the
        2026-08-03 outage."""
        _sp, service, _tp, timer = beacons.timer_units()
        self.assertIn("beacons --post", service)
        self.assertIn("SuccessExitStatus=1", service)
        self.assertIn("/.local/bin/helm", service)
        self.assertIn("OnUnitActiveSec=300s", timer)

    def test_a_nonsense_interval_is_refused(self):
        ok, err = beacons.ensure_timer(0)
        self.assertFalse(ok)
        self.assertIn("at least 1 second", err)


# ---------------------------------------------------------------------------
# the positive claim resumeturn makes
# ---------------------------------------------------------------------------

class StrictBeaconProcsTest(Base):
    """`beacon_procs(strict=True)` backs resumeturn's "It has an armed inbox
    beacon and will wake on its next @mention". That sentence was satisfiable
    by a zombie."""

    def test_a_GHOST_does_not_satisfy_the_armed_claim(self):
        self.waiter(301, "alpha", sid=SID_A)
        with mock.patch.object(beacons, "live_sessions", return_value={}), \
                mock.patch.object(beacons, "holder_from_records",
                                  return_value=(4242, 100)):
            pids, trouble = seats.beacon_procs("alpha", self.proc, strict=True)
        self.assertEqual(pids, [])
        self.assertIsNone(trouble, "a proven ghost is a proven absence, not UNKNOWN")

    def test_the_alert_text_stops_promising_a_wake_for_a_ghost(self):
        """The end-to-end effect, through the real `_wake_text`: a seat covered
        ONLY by an orphan must not be told it will wake on its next @mention.
        `beacon_procs` is NOT stubbed here — the whole point is what the real
        one now returns."""
        from helm import resumeturn
        self.waiter(302, "alpha", sid=SID_A)
        with mock.patch.object(beacons, "live_sessions", return_value={}), \
                mock.patch.object(beacons, "holder_from_records",
                                  return_value=(4242, 100)), \
                mock.patch.dict(os.environ, {"HELM_PROC": self.proc}), \
                mock.patch.object(seats, "beacon_procs",
                                  lambda seat, proc_dir="/proc", strict=False:
                                  _real_beacon_procs(seat, self.proc, strict)):
            text = resumeturn._wake_text("alpha")
        self.assertNotIn("armed inbox beacon", text)
        self.assertIn("No live inbox beacon", text)

    def test_a_LIVE_session_behind_the_shape_does_satisfy_it(self):
        self.waiter(303, "alpha", sid=SID_A)
        with mock.patch.object(beacons, "live_sessions",
                               return_value={SID_A: 90}):
            pids, trouble = seats.beacon_procs("alpha", self.proc, strict=True)
        self.assertEqual(pids, [303])
        self.assertIsNone(trouble)

    def test_an_UNPROVABLE_waiter_is_trouble_never_an_armed_claim(self):
        self.waiter(304, "alpha", sid=SID_A)
        with mock.patch.object(beacons, "live_sessions", return_value={}), \
                mock.patch.object(beacons, "holder_from_records",
                                  return_value=None):
            pids, trouble = seats.beacon_procs("alpha", self.proc, strict=True)
        self.assertEqual(pids, [])
        self.assertIn("could not be proven live", trouble)

    def test_the_LENIENT_stop_guard_path_is_unchanged(self):
        """Leniency is one-directional and must stay: a missed block only
        weakens the guard, a wrong block stops a healthy seat. The stop guard
        must not start blocking every seat whose session it cannot probe."""
        self.waiter(305, "alpha", sid=SID_A)
        with mock.patch.object(beacons, "live_sessions", return_value={}), \
                mock.patch.object(beacons, "holder_from_records",
                                  return_value=(4242, 100)):
            pids, trouble = seats.beacon_procs("alpha", self.proc)
        self.assertEqual(pids, [305])
        self.assertIsNone(trouble)


# ---------------------------------------------------------------------------
# the resume-turn predicate this lane's root class surfaced
# ---------------------------------------------------------------------------

class ResumeSidShapeTest(unittest.TestCase):
    """MEASURED LIVE 2026-07-30, pid 407141:
        ['claude', '--dangerously-skip-permissions', '--resume',
         'helm coordinator 7-22']
    `--resume` also takes a human session TITLE, and the token after the flag
    was taken as a session id whatever it was. That junk reached
    `turn_restart_identity` rung 2, which compared it to the real compaction
    sid, found no match, and announced 'every addressable process ... holds a
    different session (pid 407141/helm coo…) — the pane that compacted is
    gone' about a pane that was mid-turn INSIDE pid 407141."""

    def test_a_session_TITLE_is_not_session_evidence(self):
        from helm import orcaadopt
        self.assertFalse(orcaadopt.session_shaped("helm coordinator 7-22"))
        self.assertFalse(orcaadopt.session_shaped("main"))
        self.assertFalse(orcaadopt.session_shaped(""))
        self.assertTrue(orcaadopt.session_shaped(SID_A))

    def test_BOTH_layers_share_ONE_shape_predicate(self):
        """Two lanes cured this defect at once — one at rung 2's comparison,
        this one at the extraction — and the collision briefly left TWO
        session-shape predicates in this module, which is the drift hazard a
        shared definition exists to prevent.

        Behavioural, not a spelling check: swap the predicate and the
        extraction must follow, or a second copy has been born again."""
        from helm import orcaadopt
        with mock.patch.object(orcaadopt, "session_shaped",
                               lambda v: v == "zzz"):
            row = self._row_for("--resume", SID_A)
        self.assertIsNone(row["resume_sid"],
                          "the extraction kept its own copy of the shape rule")

    def _row_for(self, *tail):
        """Drive `_read_candidate` through deterministic /proc bytes.

        This still tests the extraction itself; unlike supplying `resume_sid`
        to `turn_restart_identity`, no parsed field is stubbed.
        """
        from helm import orcaadopt

        pid = 424242
        root = "/proc/%d" % pid
        entry = root + "/cmdline"
        comm = root + "/comm"
        stat = root + "/stat"
        environ = root + "/environ"
        argv = ["claude", "-c", "fixture"] + list(tail)
        files = {
            comm: b"claude\n",
            stat: _stat(pid, 777, comm="claude").encode(),
            entry: b"\0".join(v.encode() for v in argv) + b"\0",
            environ: (b"HELM_CHAT_NAME=fixture-seat\0"
                      b"ORCA_PANE_KEY=fixture-pane\0"),
        }
        reads = []
        real_open = open

        def opener(path, mode="r", *args, **kwargs):
            path = str(path)
            if path in files:
                reads.append(path)
                return io.BytesIO(files[path])
            if path.startswith(root + "/"):
                raise AssertionError("unexpected fixture /proc read: %s" % path)
            return real_open(path, mode, *args, **kwargs)

        with mock.patch("builtins.open", opener):
            row, verdict = orcaadopt._read_candidate(pid, entry)

        self.assertEqual(verdict, orcaadopt.ROW)
        self.assertIsNotNone(row, "the planted candidate was not read")
        self.assertEqual(
            reads, [comm, stat, comm, entry, environ, stat],
            "candidate fields must be read inside the full identity bracket")
        return row

    def test_the_EXTRACTION_discards_a_titled_resume_on_a_REAL_process(self):
        """The predicate itself, through the real /proc reads.

        The rung test below SUPPLIES `resume_sid`, so it measures rung 2 and
        NOT the extraction — a mutation putting the junk-accepting version back
        SURVIVED it, which is exactly the 'a test that stubs the mechanism
        under test measures your stub' trap. This binds the fix where it lives:
        a genuine process whose comm is `claude` and whose argv reproduces the
        measured one."""
        row = self._row_for("--dangerously-skip-permissions",
                            "--resume", "helm coordinator 7-22")
        self.assertIsNone(
            row["resume_sid"],
            "a session TITLE was taken as session evidence — that is the value "
            "that declared a live mid-turn pane gone")

    def test_the_EXTRACTION_still_keeps_a_REAL_sid(self):
        """The other direction, or the cure is just 'never trust --resume'."""
        self.assertEqual(self._row_for("--resume", SID_B)["resume_sid"], SID_B)

    def test_a_titled_resume_leaves_the_process_SILENT_not_contradicting(self):
        """The cure asserted at the rung that fired: a process whose --resume
        carries a title must land in the `silent` bucket, where a pane that
        never wrote a sid has always belonged — so the rung does NOT declare
        the compacted pane gone."""
        from helm import orcaadopt
        live = [{"pid": 407141, "seat": "oi", "pane_key": "pk-1",
                 "resume_sid": None}]
        with mock.patch.object(orcaadopt, "roster_sessions",
                               return_value=([], False)):
            ident, why = orcaadopt.turn_restart_identity(
                "oi", SID_A, procs=live, unreadable=[])
        self.assertIsNotNone(ident, why)
        self.assertEqual(int(ident), 407141)

    def test_a_REAL_sid_mismatch_still_refuses(self):
        """The asymmetry is the point: missing evidence does not refuse,
        positive disproof still does."""
        from helm import orcaadopt
        live = [{"pid": 407141, "seat": "oi", "pane_key": "pk-1",
                 "resume_sid": SID_B}]
        with mock.patch.object(orcaadopt, "roster_sessions",
                               return_value=([], False)):
            ident, why = orcaadopt.turn_restart_identity(
                "oi", SID_A, procs=live, unreadable=[])
        self.assertIsNone(ident)
        self.assertIn("different session", why)


class ALeaseExpiryIsWakingNotDeafTest(Base):
    """task/3055 — a beacon that reached its 30-minute lease is a check-in.

    MEASURED ON THE LIVE FLEET: gemini read `UNUSABLE — no live
    beacon` and a review booking to grok was refused, each inside the gap
    between a waiter the harness killed at its Monitor deadline and the one
    its seat armed about 31 seconds later. The killed waiter never ran its
    own `finally`, so its registry row survived it; the census pruned that
    row and wrote DEAF, and the register held DEAF for a whole pass.

    These arms plant exactly that row: a registry entry whose pid is gone and
    whose arm time puts its death at the lease deadline. Only the arm time
    and the declaring pane vary between them."""

    LEASE_S = 1800        # seats_advice.BEACON_TIMEOUT_MS, the Monitor cap

    def plant_row(self, pid, armed_ago, seat="alpha", phase=None):
        """A committed registry row for a waiter that is NOT in /proc."""
        os.makedirs(beacons.registry_dir(), exist_ok=True)
        row = {"seat": seat, "pid": pid, "session": SID_A, "starttime": 100,
               "armed": time.time() - armed_ago,
               "home": os.environ["HELM_HOME"]}
        if phase:
            row["phase"] = phase
        with open(beacons._entry_path(seat, pid), "w") as f:
            json.dump(row, f)

    def census(self):
        with mock.patch.object(beacons, "live_sessions",
                               return_value={SID_A: 90}):
            return beacons.census(proc_dir=self.proc)

    def test_a_beacon_killed_at_its_lease_deadline_reads_WAKING(self):  # noqa: VACUOUS_ASSERTION — the verdict WAKING and the waking bucket naming alpha are the positive observables of the same census; the empty deaf and unreachable buckets are their complement
        """THE RED-FIRST ARM. The tip before task/3055 pruned this row and
        answered DEAF."""
        self.roster("alpha")
        self.agent(90, "alpha")
        self.plant_row(4242, self.LEASE_S + 10)
        rep = self.census()
        self.assertEqual([beacons.WAKING], [r["verdict"] for r in rep["seats"]],
                         rep["seats"][0]["why"])
        self.assertEqual(["alpha"], [r["seat"] for r in rep["waking"]])
        self.assertEqual([], rep["deaf"])
        self.assertEqual([], rep["unreachable"],
                         "a seat inside its re-arm grace is not an outage")
        self.assertEqual(1, len(beacons.entries("alpha")),
                         "the expired row must be KEPT for the grace")
        self.assertEqual([4242], [b["pid"] for b in rep["seats"][0]["expired"]])
        out = CensusTest.render(self, rep)
        self.assertIn("WAKING SEAT alpha", out)
        self.assertIn("30-minute lease", out)
        self.assertIn("1 WAKING, 0 DEAF", out)

    def test_the_register_writes_WAKING_and_never_advances_covered(self):  # noqa: VACUOUS_ASSERTION — the register's state WAKING is the positive observable of the same attend; the absent covered stamp and empty transitions are what WAKING must not write
        self.roster("alpha")
        self.agent(90, "alpha")
        self.plant_row(4242, self.LEASE_S + 10)
        rep = self.census()
        out = beacons.attend(rep)
        self.assertIsNone(out["error"], out)
        with open(os.path.join(os.environ["HELM_CHAT_DIR"],
                               ".roster.json")) as f:
            att = json.load(f)["alpha"]["attendance"]
        self.assertEqual(beacons.WAKING, att["state"])
        self.assertFalse(att["alarm"])
        self.assertIsNone(att["covered"],
                          "WAKING is not a proven answer, so it must not "
                          "stamp the covered grace")
        self.assertEqual([], out["transitions"],
                         "no alarm edge for a seat that is re-arming")

    def test_CONTROL_an_hour_past_the_deadline_is_DEAF_and_pruned(self):  # noqa: VACUOUS_ASSERTION — the verdict DEAF and the unreachable bucket naming alpha are the positive observables; the emptied registry is the prune they imply
        """The grace ends. A seat that never re-armed is DEAF again, and its
        row goes the way of every other dead row."""
        self.roster("alpha")
        self.agent(90, "alpha")
        self.plant_row(4242, self.LEASE_S + 3600)
        rep = self.census()
        self.assertEqual([beacons.DEAF], [r["verdict"] for r in rep["seats"]])
        self.assertEqual([], beacons.entries("alpha"))
        self.assertEqual(["alpha"], [r["seat"] for r in rep["unreachable"]])

    def test_CONTROL_a_death_mid_lease_is_DEAF_at_once(self):  # noqa: VACUOUS_ASSERTION — the verdict DEAF is the positive observable; the emptied registry is the prune it implies
        """Ten minutes into a 30-minute lease is not the lease: a waiter that
        died there crashed or was killed, and nothing will re-arm it on its
        own schedule."""
        self.roster("alpha")
        self.agent(90, "alpha")
        self.plant_row(4242, 600)
        rep = self.census()
        self.assertEqual([beacons.DEAF], [r["verdict"] for r in rep["seats"]])
        self.assertEqual([], beacons.entries("alpha"))

    def test_CONTROL_no_declaring_pane_is_never_WAKING(self):
        """WAKING needs the independent evidence VACANT rests on: a pane that
        declares the seat. Without it the agent that would re-arm may be gone
        with its waiter, and that is DEAF."""
        self.roster("alpha")
        self.plant_row(4242, self.LEASE_S + 10)
        rep = self.census()
        self.assertEqual([beacons.DEAF], [r["verdict"] for r in rep["seats"]])

    def test_CONTROL_an_arming_marker_never_reads_WAKING(self):  # noqa: VACUOUS_ASSERTION — the verdict DEAF is the positive observable; the emptied registry is the prune it implies
        """An `arming` marker never entered wait, so it had no lease."""
        self.roster("alpha")
        self.agent(90, "alpha")
        self.plant_row(4242, self.LEASE_S + 10, phase="arming")
        rep = self.census()
        self.assertEqual([beacons.DEAF], [r["verdict"] for r in rep["seats"]])
        self.assertEqual([], beacons.entries("alpha"))

    def test_CONTROL_a_live_beacon_beside_an_expired_row_is_COVERED(self):
        """The re-arm landed: the new waiter answers, whatever the old row
        says."""
        self.roster("alpha")
        self.agent(90, "alpha")
        self.waiter(613, "alpha", sid=SID_A)
        self.plant_row(4242, self.LEASE_S + 10)
        rep = self.census()
        self.assertEqual([beacons.COVERED], [r["verdict"] for r in rep["seats"]])
        self.assertEqual(1, len(rep["seats"][0]["live"]))

    def test_WAKING_is_outside_the_alarm_class(self):
        self.assertFalse(beacons.unreachable(
            {"verdict": beacons.WAKING, "live": []}))
        # THE CONTROL on the same predicate: DEAF with no live beacon alarms.
        self.assertTrue(beacons.unreachable(
            {"verdict": beacons.DEAF, "live": []}))

    def test_the_grace_is_derived_from_the_census_cadence(self):  # noqa: VACUOUS_ASSERTION — two equalities between named constants, each a positive value
        self.assertEqual(2 * beacons.INTERVAL_S, beacons.REARM_GRACE_S)
        from helm import seats_advice
        self.assertEqual(self.LEASE_S * 1000, seats_advice.BEACON_TIMEOUT_MS,
                         "the fixture's lease must be the harness cap")


class ADeliveryThatBecameNoTurnTest(Base):
    """task/2463 — a beacon that woke nobody reads healthy on every surface.

    MEASURED ON THE LIVE FLEET: a seat sat at an empty composer for 4h45m with
    six addressed rows delivered and no turn fired, while `helm chat seats`
    said fresh, `helm seat doctor` said USABLE turn=ok pane=live upstream
    HEALTHY, and this census said covered live 1. Every one of those facts was
    TRUE. They are all facts about a PROCESS, and none of them is about a
    DELIVERY.

    THE DISCRIMINATOR IS NOT TURN AGE, and that took a measurement to settle:
    the same seat, read later, showed six rows still pending and a completed
    turn five SECONDS old. A seat can take turns and never consume a row, so a
    rung keyed on "no turn since delivery" would have been silent on the seat
    it was written for. What the rows themselves say is how long they have
    waited, and that is the fact this rung reads.
    """

    OLD = beacons.UNDRAINED_S + 60
    NEW = beacons.UNDRAINED_S - 60

    def verdict(self, waited, agent=True, live=(1,), unknown=(), unlistable=False):
        return beacons._verdict(list(live), list(unknown), unlistable,
                                agent, "its wake path is live and a seat is "
                                "home", undrained=waited)

    def test_a_covered_seat_with_rows_older_than_the_grace_is_deaf_in_effect(self):
        state, why = self.verdict(self.OLD)
        self.assertEqual(state, beacons.DEAF_IN_EFFECT)
        self.assertIn("without being consumed", why)
        self.assertIn("its wake path is live", why,
                      "the covered reason was dropped, so the line no longer "
                      "says what the instruments DID prove")

    def test_the_same_seat_inside_the_grace_is_simply_covered(self):
        """THE CONTROL THAT KEEPS THE RUNG FROM FIRING ON EVERY BUSY SEAT.

        A seat drains at tool boundaries, so a row waiting through one long
        operation is ordinary. Without this arm the rung above is satisfied by
        a predicate that alarms on any pending row at all."""
        self.assertEqual(self.verdict(self.NEW)[0], beacons.COVERED)
        self.assertEqual(self.verdict(None)[0], beacons.COVERED)

    def test_it_refines_covered_and_no_other_verdict(self):
        """A LOUDER VERDICT IS NOT OVERWRITTEN BY A QUIETER ONE. A seat with no
        live beacon is DEAF whatever is waiting — that is the stronger claim —
        and an unclassifiable seat must not acquire a verdict from a fact that
        cannot be attributed to it."""
        self.assertEqual(self.verdict(self.OLD, live=())[0], beacons.DEAF)
        self.assertEqual(self.verdict(self.OLD, agent=False)[0],
                         beacons.VACANT)
        self.assertEqual(self.verdict(self.OLD, agent=None)[0],
                         beacons.UNPROVEN)
        self.assertEqual(self.verdict(self.OLD, unlistable=True)[0],
                         beacons.UNPROVEN)

    def test_the_alarm_class_includes_it(self):
        """THE POINT OF THE VERDICT. `unreachable` is what escalation reads;
        a new verdict that is not in it renders a line nobody acts on."""
        self.assertTrue(beacons.unreachable(
            {"verdict": beacons.DEAF_IN_EFFECT, "live": [1]}))
        # AND THE CONTROL, so this is not satisfied by a predicate that
        # returns True for everything.
        self.assertFalse(beacons.unreachable(
            {"verdict": beacons.COVERED, "live": [1]}))

    @staticmethod
    def reader(hits, attempted=None, bounded=(), estate=("complete", None)):
        """A census reader SHAPED LIKE THE SHIPPED PRODUCER.

        The doubles this replaces returned hits and left `coverage` untouched,
        so they can emit a state `_pending_all` cannot produce. A control
        supplying `scanned=('helm',), seen=()` directly is one the real producer
        cannot emit while it builds `scanned` from hits, so such an arm passes
        over the very bug it is meant to catch. This one records the rooms it ATTEMPTED, the
        way `_pending_all` now does, and the `bounded` argument is how a room
        that was reached and not read whole gets said."""
        rooms = tuple(attempted) if attempted is not None else tuple(
            dict.fromkeys(r for r, _row in hits))

        def read(*_a, **kw):
            cov = kw.get("coverage")
            if isinstance(cov, dict):
                seen = cov.setdefault("rooms", {})
                for r in rooms:
                    seen[r] = ("read", None)
                for r, outcome, detail in bounded:
                    seen[r] = (outcome, detail)
                cov["estate"] = estate
            return hits
        return read

    def test_a_row_from_a_room_read_PARTLY_cannot_date_the_backlog(self):
        """UNCERTAIN SUPPRESSION IS NOT POSITIVE EVIDENCE. The wake cursor is
        what suppresses rows a wake already crossed, so losing it does not
        hide rows -- it UN-HIDES consumed ones, and the oldest of those
        carries an age that reads as an overdue row and types into a pane
        about an obligation that was already met."""
        rows = [("tainted", {"ts": "2026-09-13T20:00:00Z", "id": "old"})]
        now = 1789333200.0                   # 2026-09-13T21:00:00Z
        waited, unreadable, ev = beacons.undrained(
            "alpha", now=now, reader=self.reader(
                rows, attempted=("tainted",),
                bounded=(("tainted", "unreadable",
                          "the wake cursor could not be trusted"),)))
        self.assertIsNone(waited,
                          "a row whose suppression could not be applied dated "
                          "the backlog anyway")
        self.assertIn("not read whole", unreadable or "")
        self.assertIsNone(ev["oldest"])
        # THE ROW IS STILL REPORTED as seen -- withheld from the CLOCK, not
        # from the record.
        self.assertEqual(ev["seen"], (("tainted", "old"),))
        # THE CONTROL, unconditional: the SAME row in a room that WAS read
        # whole dates the backlog exactly as before, even with another room
        # bounded beside it -- so the refusal is about the room the age was
        # measured in and not about any taint anywhere.
        ok_waited, ok_unreadable, ok_ev = beacons.undrained(
            "alpha", now=now, reader=self.reader(
                [("helm", {"ts": "2026-09-13T20:00:00Z", "id": "old"})],
                attempted=("helm", "tainted"),
                bounded=(("tainted", "unreadable", "wake cursor untrusted"),)))
        self.assertEqual(ok_waited, 3600.0)
        self.assertEqual(ok_ev["oldest"], ("helm", "old"))
        self.assertIsNone(ok_unreadable)

    def test_undrained_reads_the_oldest_row_and_reports_what_it_cannot_read(self):
        """SECONDS FROM THE OLDEST ROW, and UNREADABLE is not DRAINED.

        The third assertion is the one that matters: a census that could not be
        taken must not reach the ladder as "nothing is waiting", which is the
        representation collapse that makes an alarm decay into a no-op."""
        rows = [("helm", {"ts": "2026-09-13T20:00:00Z", "id": "old"}),
                ("helm", {"ts": "2026-09-13T21:00:00Z", "id": "new"})]
        now = 1789333200.0                   # 2026-09-13T21:00:00Z
        waited, unreadable, ev = beacons.undrained(
            "alpha", reader=self.reader(rows), now=now)
        self.assertEqual(waited, 3600.0)
        self.assertIsNone(unreadable)
        # THE EVIDENCE NAMES THE ROW THE AGE CAME FROM. Without it a later
        # pass can only ask "is anything waiting", which an unsampled room
        # answers no to.
        self.assertEqual(ev["oldest"], ("helm", "old"))
        self.assertEqual(ev["scanned"], ("helm",))

        # A row whose stamp cannot be parsed is NAMED, and the readable ones
        # still answer — a partial read is not a blank one.
        waited, unreadable, _ev = beacons.undrained(
            "alpha", now=now,
            reader=self.reader(rows + [("helm", {"ts": "whenever"})]))
        self.assertEqual(waited, 3600.0)
        self.assertIn("no readable timestamp", unreadable)

        def boom(*_a, **_k):
            raise OSError("the room is gone")
        waited, unreadable, ev = beacons.undrained("alpha", reader=boom)
        self.assertIsNone(waited)
        self.assertIn("could not be taken", unreadable)
        self.assertIn("OSError", unreadable)
        # A CENSUS THAT NEVER RAN LOOKED AT NOTHING, and says so — an empty
        # `scanned` is what stops `drain_proven` calling this a recovery.
        self.assertEqual(ev["scanned"], ())

    def test_nothing_waiting_is_none_and_not_zero(self):
        """ZERO IS A DURATION AND None IS AN ABSENCE. A rung comparing 0
        against the grace is correct by accident today and wrong the moment
        the comparison is written the other way round."""
        waited, unreadable, ev = beacons.undrained(
            "alpha", reader=self.reader([]))
        self.assertIsNone(waited)
        self.assertIsNone(unreadable)
        # AND AN EMPTY SAMPLE IS NOT AN EMPTY BACKLOG: no reason, no rooms
        # read, so nothing here licenses "drained".
        self.assertEqual((ev["oldest"], ev["scanned"], ev["seen"]),
                         (None, (), ()))

    def test_the_rendered_line_names_the_seat_the_wait_and_the_repair(self):
        """ASSERT THE ROW, NOT THE SCREEN. This census prints a header and a
        per-seat table, so a substring assertion over the whole output can be
        satisfied by a line about a different seat entirely."""
        rep = {"seats": [], "live_probe": True, "agent_probe": True,
               "covered": [], "deaf": [], "vacant": [], "unproven": [],
               "unreachable": [], "ghosts": [], "beacons": 0, "surplus": 0,
               "deaf_in_effect": [{"seat": "alpha", "waited": 17100,
                                   "why": "its wake path is live and a seat "
                                          "is home, and yet a row addressed "
                                          "to it has waited 4h without being "
                                          "consumed",
                                   "live": [1], "ghosts": [], "unknown": [],
                                   "duplicates": 0}]}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            beacons._print_census(rep)
        line = [l for l in out.getvalue().splitlines()
                if "DEAF-IN-EFFECT SEAT" in l]
        self.assertEqual(len(line), 1, out.getvalue())
        self.assertIn("alpha", line[0])
        # THE DURATION EXACTLY ONCE. The verdict's `why` carries its own copy
        # of the wait, so a printer that also emits the aged wait says it
        # twice in one line. A substring assertion cannot see that — only the
        # COUNT can.
        self.assertEqual(line[0].count("4h"), 1, line[0])
        # AND THE ADVERTISED REPAIR MUST BE THE ONE THE PARSER ACCEPTS.
        # `--deliver` is the detached child's own interface and its parser
        # returns 2 without --session/--text-file, so a line printing it hands
        # the operator a command that cannot run.
        self.assertIn("resume-turn --nudge --seat alpha", line[0])
        self.assertNotIn("--deliver", line[0])
        self.assertIn("Re-arming the beacon fixes nothing", line[0])
        # THE SUMMARY COUNTS IT, which is the surface an operator scans first.
        self.assertIn("1 DEAF-IN-EFFECT", beacons._summary(rep))


class TheConsumptionCensusAsksTheBEACONsQuestionTest(Base):
    """task/2463 FIX10 finding 6 — the verdict counted rows that were never
    going to wake anything.

    `deliverable` has two tiers. The BOUNDARY pair (ambient=True) is what
    renders inside a running turn and includes plain home-room chatter and
    muted rooms; the BEACON pair (ambient=False, beacon=True) is what actually
    WAKES a seat, and deliberately drops exactly that tier. This verdict is
    about a seat that was NOT WOKEN, so counting the boundary tier raised an
    alarm about a seat doing nothing wrong — and then typed into its pane.
    """

    def test_the_reader_is_called_with_the_beacons_own_eligibility(self):
        """SPY THE REAL CALL. Reconstructing the pair here would assert my own
        hypothesis back to me; this reads the kwargs the shipped code sends."""
        seen = {}

        def reader(room, seat, **kw):
            seen.update(kw)
            return []
        beacons.undrained("alpha", session="s", room="helm", reader=reader)
        self.assertEqual(seen.get("ambient"), False)
        self.assertEqual(seen.get("beacon"), True)
        self.assertEqual(seen.get("scan_lane"), "beacons",
                         "the census must keep its own identity queue or it "
                         "steals another consumer's eventual coverage")


class AnUnprovenRecoveryIsNotARecoveryTest(Base):
    """task/2463 FIX10 findings 3 and 4, which are ONE hole with two mouths.

    The consumption census is a BOUNDED ROTATING SAMPLE — `_pending_all` walks
    a capped slice of foreign rooms and one capped window of each — and it can
    also simply fail. Both outcomes arrived at the ladder as a wait of None,
    which read as "nothing is waiting", which cleared a standing alarm and
    announced the seat answers again. The backlog never moved: alarm, false
    recovery, alarm, for as long as the rotation kept missing the room.

    SO RECOVERY IS A CLAIM ABOUT ONE ROW. The alarm records the row it was
    raised on, and only reading THAT row's room and not finding it there
    clears it.
    """

    ROW = ("helm", "r1")

    def crow(self, unreadable=None, scanned=(), seen=(), bounded=None):
        """A census row carrying one consumption reading and nothing else."""
        return {"seat": "alpha", "verdict": beacons.COVERED,
                "undrained_unreadable": unreadable,
                "undrained_evidence": {"oldest": None, "scanned": tuple(scanned),
                                       "seen": tuple(seen),
                                       "bounded": dict(bounded or {})}}

    def test_a_room_reached_and_not_read_whole_is_not_an_empty_one(self):
        """task/2463 finding 9, and the prose it refutes was this module's own.

        The byte cap CAN hide a pending row, and the argument that it cannot --
        that an unconsumed OLD row sits at the near edge of the window -- fails
        on one clause: OLD IS AGE, NOT BYTE OFFSET. The window runs FORWARD from
        the cursor, so enough non-eligible rows between the cursor and the
        oldest eligible addressed row push that row past the end. This is the
        behaviour that has to hold instead."""
        crow = self.crow(scanned=(), bounded={
            "helm": ("truncated", "the window read 512 bytes of a 9000-byte room")})
        r = beacons.drain_proven(self.ROW, crow)
        self.assertTrue(r.unknown)
        self.assertFalse(r.proven)
        self.assertIn("could not read it whole", r.lacked)
        self.assertIn("truncated", r.lacked)
        self.assertEqual(r.because("bounded-read"), ("helm", "truncated"))

    def test_a_bounded_read_and_an_unvisited_room_refuse_DIFFERENTLY(self):
        """Both hold the alarm, and a reader acts differently on each: one
        says look again next pass, the other says something is wrong with this
        room. One sentence covering both sent readers to the wrong instrument,
        which is the same collapse this whole class exists to refuse."""
        rotated = beacons.drain_proven(self.ROW, self.crow(scanned=("other",)))
        bounded = beacons.drain_proven(self.ROW, self.crow(
            scanned=(), bounded={"helm": ("unreadable",
                                          "the consumption cursor could not be trusted")}))
        self.assertTrue(rotated.unknown and bounded.unknown)
        self.assertIn("never read helm", rotated.lacked)
        self.assertNotIn("never read helm", bounded.lacked)
        self.assertIn("could not read it whole", bounded.lacked)
        # THE CONTROL, so neither refusal is a fence that can never clear.
        self.assertTrue(beacons.drain_proven(
            self.ROW, self.crow(scanned=("helm",))).proven)

    def test_a_failed_census_never_proves_a_drain(self):
        r = beacons.drain_proven(self.ROW, self.crow(unreadable="the room is gone"))
        self.assertFalse(r.proven)
        self.assertTrue(r.unknown, "a census that failed did not measure a "
                                   "negative, it failed to measure")
        self.assertIn("the room is gone", r.lacked)

    def test_a_sample_that_never_reached_the_room_never_proves_a_drain(self):
        """THE ROTATION'S OWN GAP. Nothing was found because nothing was
        looked at, and those are different facts wearing one value."""
        # The unconditional control: a pass that DID read the room reaches a
        # firm answer, so the refusals below are about the gap and not about a
        # fence that can never resolve.
        self.assertTrue(beacons.drain_proven(
            self.ROW, self.crow(scanned=("helm",), seen=())).proven)
        for crow in (self.crow(),
                     self.crow(scanned=("other-room",),
                               seen=(("other-room", "r9"),))):
            with self.subTest(scanned=crow["undrained_evidence"]["scanned"]):
                r = beacons.drain_proven(self.ROW, crow)
                self.assertFalse(r.proven)
                self.assertTrue(r.unknown)
                self.assertIn("never read helm", r.lacked)

    def test_reading_the_row_s_own_room_without_it_IS_the_proof(self):
        """THE POSITIVE, and without it the two refusals above would be
        satisfied by a fence that simply never clears."""
        r = beacons.drain_proven(
            self.ROW, self.crow(scanned=("helm",), seen=(("helm", "r9"),)))
        self.assertTrue(r.proven)
        self.assertEqual(r.because("scanned-rooms"), ("helm",))

    def test_the_row_still_sitting_in_a_scanned_room_is_not_drained(self):
        r = beacons.drain_proven(
            self.ROW, self.crow(scanned=("helm",), seen=(("helm", "r1"),)))
        self.assertFalse(r.proven)
        self.assertTrue(r.refuted)
        self.assertEqual(r.answer, self.ROW)

    def test_an_alarm_that_named_no_row_holds_nothing_back(self):
        """A legacy alarm, or one whose evidence was lost, has no claim to
        contradict — it must not freeze a seat at DEAF-IN-EFFECT forever."""
        self.assertTrue(beacons.drain_proven(None, self.crow()).proven)

    def test_A_GAP_AND_A_MEASURED_NEGATIVE_ARE_NOT_THE_SAME_ANSWER(self):
        """THE ARM THE OLD RETURN TYPE COULD NOT HOLD, and the reason the two
        findings were one hole. Both of these refuse to clear the alarm, and a
        bool made them identical; they are opposite facts. One says the
        instrument did not look, and the next pass may well clear it. The other
        says the instrument looked and the row is STILL THERE, which is the
        alarm being CONFIRMED, not merely un-cleared."""
        gap = beacons.drain_proven(self.ROW, self.crow())
        still_there = beacons.drain_proven(
            self.ROW, self.crow(scanned=("helm",), seen=(("helm", "r1"),)))
        self.assertTrue(gap.unknown)
        self.assertTrue(still_there.refuted)
        self.assertFalse(gap.proven)
        self.assertFalse(still_there.proven)
        self.assertNotEqual(gap.confidence, still_there.confidence)

    def test_the_held_sentence_is_generated_from_the_reading(self):
        """`_held_why` reads the decision's own grounds rather than re-deriving
        the case from `(owed, crow)`. Two derivations of one fact agree until
        they do not, and the operator's sentence is the half that drifts
        unnoticed."""
        gap = beacons.drain_proven(self.ROW, self.crow())
        self.assertEqual(
            beacons._held_why(gap),
            "the standing alarm is HELD rather than cleared: " + gap.lacked)
        still_there = beacons.drain_proven(
            self.ROW, self.crow(scanned=("helm",), seen=(("helm", "r1"),)))
        self.assertIn("still waiting in helm", beacons._held_why(still_there))


class AnEmptySampleIsNotADrainedBacklogTest(Base):
    """task/2463 findings 2 and 3 — the actuator's half of a lesson that was
    already learned one module over.

    `drain_proven` refuses to read an unsampled room as an empty one. The pane
    repair's own re-check, `_still_owed`, took the census's three-tuple and
    THREW THE EVIDENCE AWAY, so a wait of None became "nothing is waiting any
    more" whatever the sample had managed to cover. The caller records that as
    DRAINED, DRAINED settles the repair against the spell that earned it, and
    the backlog never moves: alarm, false recovery, alarm.
    """

    def test_a_pass_that_read_no_room_cannot_prove_a_drain(self):
        r = beacons.sample_is_complete({"scanned": (), "seen": (),
                                        "bounded": {},
                                        "estate": ("complete", None)})
        self.assertTrue(r.unknown)
        self.assertIn("read no room at all", r.lacked)

    def test_a_pass_that_could_not_read_a_room_WHOLE_cannot_either(self):
        r = beacons.sample_is_complete({
            "scanned": ("helm",), "seen": (),
            "estate": ("complete", None),
            "bounded": {"dm-x": ("truncated", "the window was capped")}})
        self.assertTrue(r.unknown)
        self.assertIn("could not read whole", r.lacked)
        self.assertIn("dm-x", r.lacked)

    def test_a_pass_that_READ_rooms_and_found_nothing_proves_one(self):
        """THE CONTROL. Without it the two refusals above are satisfied by a
        predicate that never returns PROVEN, and the repair would be owed for
        ever instead of falsely settled — a different defect, not a cure."""
        r = beacons.sample_is_complete({"scanned": ("helm", "dm-x"),
                                        "seen": (), "bounded": {},
                                        "estate": ("complete", None)})
        self.assertTrue(r.proven)

        # AND THE NEW REFUSAL BESIDE IT: a nonempty sample over a BOUNDED
        # estate proves nothing about the rooms the rotation did not reach.
        capped = beacons.sample_is_complete(
            {"scanned": ("helm", "dm-x"), "seen": (), "bounded": {},
             "estate": ("capped", "the rotation covered 16 of 40")})
        self.assertTrue(capped.unknown)
        self.assertIn("whole room estate", capped.lacked)
        self.assertEqual(r.answer, ("helm", "dm-x"))

    def test_still_owed_keeps_the_cached_wait_when_the_sample_could_not_see(self):
        """THE SHIPPED ROUTE, not the predicate. `_still_owed` is what decides
        whether the pane is typed into, and it is the seam the finding is
        about."""
        from helm import resumeturn
        blind = (None, None, {"oldest": None, "scanned": (), "seen": (),
                              "bounded": {}, "estate": ("complete", None)})
        with mock.patch.object(beacons, "undrained", return_value=blind):
            waited, why = resumeturn._still_owed("alpha", "s", 900.0)
        self.assertEqual(waited, 900.0,
                         "an empty sample that read nothing was announced as "
                         "a drained backlog")
        self.assertEqual(why, "")
        # THE CONTROL ON THE SAME SEAM: a sample that DID read the rooms and
        # found nothing still reports the drain, so this is not a fence.
        saw = (None, None, {"oldest": None, "scanned": ("helm",), "seen": (),
                            "bounded": {}, "estate": ("complete", None)})
        with mock.patch.object(beacons, "undrained", return_value=saw):
            waited, why = resumeturn._still_owed("alpha", "s", 900.0)
        self.assertIsNone(waited)
        self.assertIn("drained between the verdict", why)

    def test_an_age_measured_in_a_partly_read_room_is_not_proof(self):
        """The WAKE cursor is what suppresses rows a wake already crossed, so
        losing it does not hide rows -- it UN-HIDES consumed ones, and the
        oldest of those carries an age that reads as an overdue row and types
        into a pane about an obligation already met."""
        from helm import resumeturn
        ev = {"oldest": ("helm", "r1"), "scanned": (), "seen": (),
              "estate": ("complete", None),
              "bounded": {"helm": ("unreadable",
                                   "the wake cursor could not be trusted")}}
        with mock.patch.object(beacons, "undrained",
                               return_value=(4200.0, None, ev)):
            waited, why = resumeturn._still_owed("alpha", "s", 900.0)
        self.assertEqual(waited, 900.0, "an age from a room this pass could "
                                        "not read whole was taken as proof")
        self.assertIn("could not read whole", why)
        # THE CONTROL on the same seam: an age from a room read WHOLE is
        # exactly the positive evidence this repair runs on.
        clean = dict(ev, bounded={})
        with mock.patch.object(beacons, "undrained",
                               return_value=(4200.0, None, clean)):
            waited, why = resumeturn._still_owed("alpha", "s", 900.0)
        self.assertEqual(waited, 4200.0)
        self.assertEqual(why, "")

    def test_a_refusal_before_the_act_does_not_settle_the_episode(self):
        """Finding 3. `paused` is delivery to the seat being HELD behind a
        credential wall — the refusal's own sentence says the backlog stays
        owed and the alarm stands, and the latch recorded it as done. Once the
        pause lifted, the same spell could never retry."""
        att = {"since": 100, "repair": {"since": 100, "outcome": "paused"}}
        self.assertTrue(beacons._repair_due(att),
                        "a repair refused before it acted is still owed")
        att["repair"]["outcome"] = "would-wake"
        self.assertTrue(beacons._repair_due(att),
                        "a dry run types nothing and settles nothing")
        # THE CONTROL: the outcomes that DID discharge the obligation still
        # close the episode, or this change would mean typing into a pane on
        # every pass for the whole spell.
        att["repair"]["outcome"] = "wake"
        self.assertTrue(beacons._repair_due(att),
                        "a SPAWN is an attempt; only the child knows whether "
                        "a keystroke landed")
        for done in ("typed", "drained"):
            att["repair"]["outcome"] = done
            self.assertFalse(beacons._repair_due(att), done)


class TheNudgeRidesTheRevalidatedEdgeTest(Base):
    """THE ONE VERDICT WHOSE REPAIR IS NOT A MESSAGE.

    Every other alarm here asks a READER to act. A DEAF-IN-EFFECT seat is the
    case where the reader IS the seat and it is not reading — its wake path is
    live, so the room post reaches everyone except the one party that has to
    do something. The delivery leg therefore also reaches the PANE.

    ON THE REVALIDATED EDGE, NEVER THE CENSUS, which is what these arms pin:
    once per transition rather than once per pass, and not at all for a seat
    that recovered between derivation and delivery.
    """

    def att(self, seat="alpha"):
        path = os.path.join(os.environ["HELM_CHAT_DIR"], ".roster.json")
        with open(path) as f:
            return json.load(f)[seat]["attendance"]

    def pass_with(self, waited, att=None, woke_result=None, fresh=True):
        """One census+attend with the consumption fact forced, then deliver.

        `fresh=False` keeps the roster THIS pass found, which is what makes a
        SECOND pass a second pass: re-seeding it would erase the attendance
        the first pass wrote and every arm about repeat behaviour would be
        measuring two first passes."""
        if fresh:
            self.roster("alpha")
        if att is not None:
            self.seed_att(**att)
        self.waiter(613, "alpha", sid=SID_A)
        self.agent(90, "alpha")
        # THE EVIDENCE IS PART OF THE FACT NOW. `helm` scans the row's own
        # room, so a forced wait must name a row in it or the recovery rung
        # would read this fixture as "never looked".
        ev = {"oldest": ("helm", "r1"), "scanned": ("helm",),
              "seen": (("helm", "r1"),)}
        with mock.patch.object(beacons, "undrained",
                               return_value=(waited, None, ev)), \
                mock.patch.object(beacons, "live_sessions",
                                  return_value={SID_A: 90}), \
                mock.patch.dict(os.environ, {"HELM_PROC": self.proc}), \
                mock.patch("helm.chat.post"), \
                mock.patch("helm.notify.owner_push", return_value=True), \
                mock.patch("helm.notify.configured", return_value=True), \
                mock.patch("helm.resumeturn.wake_undelivered",
                           side_effect=self._woke(woke_result)) as woke:
            rep = beacons.census()
            reg = beacons.attend(rep)
            out = beacons.escalate(reg["transitions"], rep)
        return rep, reg, out, woke

    @staticmethod
    def _woke(woke_result):
        """The actuator's CONTRACT as a double: a wake installs the episode
        it is about to run, last, and its result names that attempt. A forced
        result (a refusal, an alert) installs nothing, which is exactly what
        the real actuator does when it refuses before the fork."""
        def fake(seat, session, waited=None, dry=False, att=None, **_kw):
            if woke_result is not None:
                return dict(woke_result)
            attempt = (beacons.install_repair(seat, att, time.time())
                       if att is not None else None)
            return {"action": "wake", "attempt": attempt}
        return fake

    def test_a_deaf_in_effect_edge_nudges_the_pane_once(self):
        rep, reg, out, woke = self.pass_with(beacons.UNDRAINED_S + 60)
        self.assertEqual([r["verdict"] for r in rep["seats"]],
                         [beacons.DEAF_IN_EFFECT],
                         "fixture: the census did not reach the verdict, so "
                         "this arm measures nothing")
        self.assertEqual(len(reg["transitions"]), 1, reg)
        woke.assert_called_once()
        self.assertEqual(woke.call_args.args[0], "alpha")
        self.assertEqual(woke.call_args.kwargs.get("waited"),
                         self.att()["waited"],
                         "the delivery re-derived the wait instead of "
                         "carrying the one the verdict was taken on")
        # THE OUTCOME'S DETAIL RIDES WITH IT. The CLI consumes this tuple to
        # say why a repair did NOT type, so a two-wide row would leave a
        # failed repair indistinguishable from a successful one on screen.
        self.assertEqual(out.get("nudged"), [("alpha", "wake", None)])

    def seed_att(self, **fields):
        """Write one attendance row before the pass, so the arm can describe a
        seat the register has ALREADY seen rather than a first sighting."""
        path = os.path.join(os.environ["HELM_CHAT_DIR"], ".roster.json")
        with open(path) as f:
            r = json.load(f)
        r.setdefault("alpha", {})["attendance"] = fields
        with open(path, "w") as f:
            json.dump(r, f)

    def test_an_already_alarmed_seat_that_TURNS_deaf_in_effect_is_repaired(self):
        """THE HOLE THAT GATING ON TRANSITIONS LEFT. A seat already DEAF and
        acknowledged on BOTH channels moves to DEAF-IN-EFFECT without moving
        either latch — alarm was true before and is true after — so there is
        no notification edge, and the repair leg never ran for exactly the
        seat that had just acquired one."""
        rep, reg, out, woke = self.pass_with(
            beacons.UNDRAINED_S + 60,
            att={"state": beacons.DEAF, "alarm": True, "alarmed": True,
                 "pushed": True, "since": 1.0, "at": 1.0})
        self.assertEqual([r["verdict"] for r in rep["seats"]],
                         [beacons.DEAF_IN_EFFECT],
                         "fixture: the census did not reach the verdict, so "
                         "this arm measures nothing")
        self.assertEqual(reg["transitions"], [],
                         "fixture: a notification edge appeared, so this arm "
                         "is no longer about the seat that produces none")
        woke.assert_called_once()
        self.assertEqual(out.get("nudged"), [("alpha", "wake", None)])

    def test_a_settled_repair_is_not_repeated_for_the_same_spell(self):  # noqa: VACUOUS_ASSERTION — the absence is woke.assert_not_called() after a settlement; its unconditional positive control is test_a_deaf_in_effect_edge_nudges_the_pane_once, which drives the same pass on the same fixture and does nudge
        """ONE SPELL, ONE SETTLED REPAIR — otherwise every census pass types
        into the same composer for as long as the rows sit.

        AND A SPAWN IS NOT A SETTLEMENT. The parent records `wake` when it has
        spawned the detached deliverer, which is an ATTEMPT: delivery can be
        paused during that child's settle interval, the child then refuses,
        and the refusal updates resume-state while attendance already reads
        closed — so the pause lifts and the same spell can never retry. Only
        the child saw whether a keystroke landed, so only the child closes it,
        and `settle_repair` is the door it closes through."""
        self.pass_with(beacons.UNDRAINED_S + 60)
        first = self.att()
        self.assertEqual(first["repair"]["outcome"], "wake", first)
        self.assertTrue(beacons._repair_due(first),
                        "a spawn left the episode closed, so a child that "
                        "never typed could never be retried")

        # THE CHILD REPORTS -- FOR THE ATTEMPT IT RAN, which is the only way a
        # named episode closes -- and now the spell is discharged.
        self.assertTrue(beacons.settle_repair("alpha", "typed", "pane took it",
                                              attempt=first["repair"]["attempt"]))
        self.assertFalse(beacons._repair_due(self.att()))
        _rep, _reg, out, woke = self.pass_with(beacons.UNDRAINED_S + 120,
                                               fresh=False)
        woke.assert_not_called()
        self.assertIsNone(out.get("nudged"))

    def cli_pass(self, waited, woke_result=None):
        """The same pass, through the CLI, with stderr captured — the surface
        a human actually reads."""
        import contextlib
        self.roster("alpha")
        self.waiter(613, "alpha", sid=SID_A)
        self.agent(90, "alpha")
        ev = {"oldest": ("helm", "r1"), "scanned": ("helm",),
              "seen": (("helm", "r1"),)}
        err = io.StringIO()
        with mock.patch.object(beacons, "undrained",
                               return_value=(waited, None, ev)), \
                mock.patch.object(beacons, "live_sessions",
                                  return_value={SID_A: 90}), \
                mock.patch.dict(os.environ, {"HELM_PROC": self.proc}), \
                mock.patch("helm.chat.post"), \
                mock.patch("helm.notify.owner_push", return_value=True), \
                mock.patch("helm.notify.configured", return_value=True), \
                mock.patch("helm.resumeturn.wake_undelivered",
                           return_value=(woke_result or {"action": "wake"})), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            beacons.cmd_beacons(["--post"])
        return err.getvalue()

    def test_the_parent_says_SPAWNED_because_that_is_what_it_knows(self):
        """SPAWNING IS NOT TYPING, AND THE LINE A HUMAN READS SAID IT WAS.
        `wake` means the detached deliverer was launched; the child can still
        pause, find the backlog drained, or refuse before any key is pressed.
        A line claiming the keystroke landed is a claim about a result only
        the child can see, and the operator who reads it stops looking."""
        out = self.cli_pass(beacons.UNDRAINED_S + 60)
        self.assertIn("pane repair SPAWNED for", out)
        self.assertNotIn("TYPED into", out)
        self.assertIn("that child's own report", out)
        # THE CONTROL, unconditional and on the same surface: a repair that
        # did not even spawn still says so in its own words, so the change
        # above is about the WAKE line and not about a surface that went
        # quiet.
        refused = self.cli_pass(beacons.UNDRAINED_S + 60,
                                woke_result={"action": "paused",
                                             "detail": "delivery is paused"})
        self.assertIn("did NOT type", refused)
        self.assertIn("delivery is paused", refused)

    def test_a_childs_terminal_result_survives_the_parents_later_write(self):
        """THE CHILD CAN FINISH WHILE THE PARENT IS STILL WALKING THE BATCH.
        The parent records every attempt after the whole pass returns, so a
        child that settled `typed` in the meantime had its result replaced by
        the parent's `wake` — and a repair that had already succeeded became
        due again, spending the cap on a seat that was answering."""
        self.pass_with(beacons.UNDRAINED_S + 60)
        att = self.att()
        self.assertEqual(att["repair"]["outcome"], "wake", att)
        attempt = att["repair"]["attempt"]
        self.assertTrue(beacons.settle_repair("alpha", "typed", "pane took it",
                                              attempt=attempt))
        self.assertEqual(self.att()["repair"]["outcome"], "typed")
        # THE PARENT'S BATCH WRITE ARRIVES AFTER THE CHILD'S REPORT.
        beacons._record_repairs([("alpha", att, {"action": "wake",
                                                 "detail": None})],
                                time.time())
        self.assertEqual(self.att()["repair"]["outcome"], "typed",
                         "the parent's attempt overwrote a terminal result")
        self.assertFalse(beacons._repair_due(self.att()))
        # THE CONTROL, unconditional and through the same call: an episode
        # that is NOT terminal is still the parent's to write, or this guard
        # would have silenced the retry path instead of protecting it.
        # A SECOND ATTEMPT ON THE SAME SPELL IS A DIFFERENT EPISODE, and the
        # id says so: the spell is shared, the attempt is not.
        # THE SETTLED EPISODE IS NOT REOPENED BY THE SAME SPELL, so the retry
        # here has to be a NEW spell -- the only way back after `typed`.
        moved = dict(att, since=att["since"] + 50.0, at=att["since"] + 50.0)
        moved.pop("repair", None)
        self.seed_att(**moved)
        retry = beacons.install_repair("alpha", moved, time.time())
        self.assertNotEqual(retry, attempt)
        self.assertTrue(str(retry).startswith(str(moved["since"])), retry)
        beacons._record_repairs([("alpha", moved, {"action": "alert",
                                                   "detail": "no pane",
                                                   "attempt": retry})],
                                time.time())
        self.assertEqual(self.att()["repair"]["outcome"], "alert")

    def test_a_late_child_cannot_close_a_RETRY_of_its_own_spell(self):
        """THE SPELL IS NOT THE POLICY UNIT, THE ATTEMPT IS. One
        DEAF-IN-EFFECT spell earns several repair attempts — a child that
        never typed leaves the episode open and the next pass launches
        another — so an identity that says only WHICH SPELL still matches
        when child one lands after attempt two was installed, and closes
        somebody else's attempt with its own result. No second spell is
        required for this, which is what makes it the sharper case."""
        self.pass_with(beacons.UNDRAINED_S + 60)
        att = self.att()
        first = att["repair"]["attempt"]
        # THE SAME SPELL, A SECOND ATTEMPT — the shape a retry produces.
        second = beacons.install_repair("alpha", att, time.time())
        self.assertNotEqual(first, second)
        self.assertEqual(self.att()["since"], att["since"],
                         "fixture: the spell moved, so this arm is about two "
                         "spells again and not about a retry")
        # THE LATE CHILD FROM ATTEMPT ONE REPORTS, and attempt two is intact.
        self.assertFalse(beacons.settle_repair("alpha", "typed", "late",
                                               attempt=first))
        self.assertEqual(self.att()["repair"]["outcome"], "wake")
        self.assertTrue(beacons._repair_due(self.att()))
        # THE CONTROL, unconditional: the child that ran for attempt two
        # closes attempt two.
        self.assertTrue(beacons.settle_repair("alpha", "typed", "for two",
                                              attempt=second))
        self.assertEqual(self.att()["repair"]["outcome"], "typed")

    def test_a_delayed_child_cannot_close_a_spell_it_did_not_run_for(self):
        """A CHILD CLOSES THE EPISODE IT WAS LAUNCHED FOR, NOT THE ONE THAT IS
        CURRENT. Child A delivers for spell A and is held up; the attendance
        moves to a DISTINCT spell B and the parent opens B's episode; A's
        report then closed B. No timestamp collision is required — a delay is
        enough — and the comparison that was here rejects a stale STORED
        episode while saying nothing about the CALLER."""
        self.pass_with(beacons.UNDRAINED_S + 60)
        attempt_a = self.att()["repair"]["attempt"]
        spell_a = self.att()["since"]
        # A DISTINCT LATER SPELL, with its own freshly opened episode.
        moved = dict(self.att(), since=spell_a + 100.0, at=spell_a + 100.0)
        moved.pop("repair", None)
        self.seed_att(**moved)
        attempt_b = beacons.install_repair("alpha", moved, time.time())
        self.assertNotEqual(attempt_a, attempt_b)
        self.assertEqual(self.att()["repair"]["attempt"], attempt_b)
        # THE LATE CHILD REPORTS FOR A, and B is untouched.
        self.assertFalse(beacons.settle_repair("alpha", "typed", "late for A",
                                               attempt=attempt_a))
        self.assertEqual(self.att()["repair"]["outcome"], "wake")
        self.assertTrue(beacons._repair_due(self.att()),
                        "spell B was closed by a child that ran for spell A")
        # THE CONTROL, unconditional: the child that DID run for B closes B,
        # so the refusal above is about identity and not a door that never
        # opens.
        self.assertTrue(beacons.settle_repair("alpha", "typed", "for B",
                                              attempt=attempt_b))
        self.assertEqual(self.att()["repair"]["outcome"], "typed")

    def test_a_repair_that_never_reached_the_pane_is_RETRIED(self):
        """A PARENT FAILURE FOLLOWED BY TWO SUCCESSFUL NOTIFICATIONS would
        otherwise lose the repair forever: the latches have nothing left to
        say, so nothing would ever ask again."""
        self.pass_with(beacons.UNDRAINED_S + 60,
                       woke_result={"action": "alert",
                                    "detail": "no pane evidence"})
        first = self.att()
        # AN ALERT BEFORE THE FORK OPENED NO EPISODE -- the actuator refused
        # before it installed, and a refusal writes nothing onto whatever
        # episode might be current. What matters is that the spell is DUE.
        self.assertIsNone(first.get("repair"), first)
        self.assertTrue(beacons._repair_due(first))
        _rep, _reg, out, woke = self.pass_with(beacons.UNDRAINED_S + 120,
                                               fresh=False)
        woke.assert_called_once()
        self.assertEqual(out.get("nudged"), [("alpha", "wake", None)])
        # AND THE RETRY IS ABOUT THE SAME SPELL. If `since` moved, the second
        # attempt would be explained by a NEW DEAF-IN-EFFECT spell and this
        # arm would say nothing about an unsettled outcome being retried.
        self.assertEqual(self.att()["since"], first["since"])

    def test_a_stale_snapshot_cannot_reopen_a_settled_spell(self):
        """task/2463 r6 finding 1. `escalate` decides "due" against the roster
        it read at the top of the pass; the child can settle that very spell
        before the installer runs, and an install that checks only "same
        spell" then REPLACES a terminal `typed` with a fresh `wake` -- the
        repair that already succeeded is owed again and spends the cap on a
        seat that answered. Eligibility is re-derived INSIDE the installer's
        lock, against the row as it is, not as it was."""
        self.pass_with(beacons.UNDRAINED_S + 60)
        snapshot = self.att()                # what the parent is holding
        attempt = snapshot["repair"]["attempt"]
        self.assertTrue(beacons.settle_repair("alpha", "typed", "pane took it",
                                              attempt=attempt))
        self.assertFalse(beacons._repair_due(self.att()))
        # THE STALE SNAPSHOT REACHES THE INSTALLER, and it opens nothing.
        self.assertIsNone(beacons.install_repair("alpha", snapshot,
                                                 time.time()),
                          "a stale snapshot opened an episode on a settled "
                          "spell")
        self.assertEqual(self.att()["repair"]["outcome"], "typed",
                         "a stale snapshot reopened a settled repair")
        self.assertEqual(self.att()["repair"]["attempt"], attempt)
        self.assertFalse(beacons._repair_due(self.att()))
        # THE CONTROL, unconditional: a NEW spell is the way back, and it
        # installs -- or the cure above would be "never install again".
        moved = dict(self.att(), since=snapshot["since"] + 100.0,
                     at=snapshot["since"] + 100.0)
        moved.pop("repair", None)
        self.seed_att(**moved)
        self.assertIsNotNone(beacons.install_repair("alpha", moved,
                                                    time.time()))

    def test_a_refused_wake_does_not_displace_the_running_child(self):
        """task/2463 r6 finding 2. Attempt one's child is in its settle delay;
        the next pass is refused by the debounce -- and with the episode
        installed BEFORE the actuator's own refusals, that refusal had
        already replaced the roster's in-flight attempt with a second one that
        nothing would ever run. Child one then delivered and its own report
        was refused against an attempt it never was. Installation is the
        actuator's LAST act before the fork, so a refusal installs nothing,
        and a refusal's result carries no attempt, so the batch write after
        it lands on nothing either."""
        self.pass_with(beacons.UNDRAINED_S + 60)
        first = self.att()["repair"]
        self.assertEqual(first["outcome"], "wake")
        # THE NEXT PASS IS REFUSED BY THE DEBOUNCE -- the actuator's answer,
        # which opens no episode and names no attempt.
        _rep, _reg, out, _w = self.pass_with(
            beacons.UNDRAINED_S + 120, fresh=False,
            woke_result={"action": "debounce",
                         "detail": "same episode -- a resume fired 3s ago"})
        self.assertEqual(out.get("nudged"),
                         [("alpha", "debounce",
                           "same episode -- a resume fired 3s ago")])
        self.assertEqual(self.att()["repair"], first,
                         "a refused wake rewrote the running child's episode")
        # AND CHILD ONE'S OWN REPORT STILL CLOSES CHILD ONE'S EPISODE.
        self.assertTrue(beacons.settle_repair("alpha", "typed", "landed",
                                              attempt=first["attempt"]))
        self.assertEqual(self.att()["repair"]["outcome"], "typed")
        self.assertFalse(beacons._repair_due(self.att()))

    def test_a_child_with_no_name_cannot_close_a_named_episode(self):
        """task/2463 r6 finding 3, the settlement half. A legacy or manual
        deliverer carries no attempt; letting it settle "whatever is current"
        is the unbound child closing the bound one's episode from the other
        side. It keeps the old behaviour only on an episode that itself has
        no attempt -- the only kind it could have run for."""
        self.pass_with(beacons.UNDRAINED_S + 60)
        named = self.att()["repair"]
        self.assertTrue(named.get("attempt"), named)
        self.assertFalse(beacons.settle_repair("alpha", "typed", "legacy"),
                         "a nameless report was allowed to close a named "
                         "episode")
        self.assertEqual(self.att()["repair"]["outcome"], "wake",
                         "a nameless report closed a named episode")
        # THE CONTROL, unconditional: an episode with NO attempt is the
        # legacy shape, and the legacy caller still closes it.
        legacy = dict(self.att())
        legacy["repair"] = {"at": 1.0, "since": legacy["since"],
                            "outcome": "wake", "detail": ""}
        self.seed_att(**legacy)
        self.assertTrue(beacons.settle_repair("alpha", "typed", "legacy"))
        self.assertEqual(self.att()["repair"]["outcome"], "typed")

    def test_a_withheld_keystroke_leaves_the_spell_owed(self):
        """task/2463 r6 finding 5, the episode half. A child that could not
        re-read the backlog at the act types nothing AND settles nothing:
        `withheld` is a deferral, so the spell is asked again -- neither a
        drain (which would cancel a repair on a failed instrument) nor a
        licence (which would type on a guess)."""
        self.pass_with(beacons.UNDRAINED_S + 60)
        attempt = self.att()["repair"]["attempt"]
        self.assertIn("withheld", beacons._REPAIR_DEFERRED)
        self.assertTrue(beacons.settle_repair("alpha", "withheld",
                                              "census failed", attempt=attempt))
        self.assertEqual(self.att()["repair"]["outcome"], "withheld")
        self.assertTrue(beacons._repair_due(self.att()),
                        "a withheld keystroke settled the spell")
        # THE CONTROL, unconditional: `typed` on the same attempt settles it.
        self.assertTrue(beacons.settle_repair("alpha", "typed", "landed",
                                              attempt=attempt))
        self.assertFalse(beacons._repair_due(self.att()))

    def test_a_covered_edge_nudges_NOTHING(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is woke.assert_not_called(); its unconditional positive control is test_a_deaf_in_effect_edge_nudges_the_pane_once, which drives the SAME three calls on the SAME fixture with only the wait changed and does nudge
        """THE CONTROL, and it differs from the arm above by ONE NUMBER. A
        seat inside the grace is COVERED, produces no alarm edge at all, and
        must never be typed into."""
        rep, _reg, out, woke = self.pass_with(beacons.UNDRAINED_S - 60)
        self.assertEqual([r["verdict"] for r in rep["seats"]],
                         [beacons.COVERED])
        woke.assert_not_called()
        self.assertIsNone(out.get("nudged"))


if __name__ == "__main__":
    unittest.main()


_KEEP = object()


class MisroutedVerdictTest(Base):
    """THE WIRING: a beacon that runs and delivers to the WRONG CONVERSATION.

    MISROUTED is produced from the PRODUCER'S STAMP, read off the registry row
    by `row_origin`, and consumed by `unreachable()`. These arms drive that
    real producer through `seat_census`; none of them injects the verdict, so
    a green pass here means the constant reached the branch the way the fleet
    reaches it rather than the way a test can.

    THE LIVE POPULATION WAS MEASURED BEFORE THIS LANDED: 18 live beacons on
    this box, 15 proven top-level, 0 proven sidechain, 3 unattributable. So
    the rung ships as a label with no current instance rather than a new
    fleet-wide siren -- which is the only responsible way to wire an alarm."""

    SEAT = "misrouted-seat"
    SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    STAMP = "2026-09-15T12:00:00.488Z"

    def setUp(self):
        super().setUp()
        self.cfg = tempfile.mkdtemp(prefix="helm-test-misrouted-")
        self.addCleanup(shutil.rmtree, self.cfg, True)
        self.slug = os.path.join(self.cfg, "projects", "-home-user-dev-x")
        os.makedirs(self.slug)
        self.armed = pk.parse_ts_epoch("2026-09-15T12:00:00Z")




    def _beacon(self, pid, starttime=100, sid=None, origin=None):
        """A registered, scannable, LIVE waiter for this seat.

        `sid` IS A PARAMETER BECAUSE A SEAT'S BEACONS NEED NOT SHARE ONE
        CONVERSATION -- that is the whole content of the MIXED case, and
        planting one session into both a main transcript and a sidechain does
        not model it. It models an AMBIGUOUS session, which the reader
        correctly refuses to attribute at all."""
        sid = sid or self.SID
        self.waiter(pid, self.SEAT, sid=sid, starttime=starttime)
        os.makedirs(beacons.registry_dir(), exist_ok=True)
        row = {"seat": self.SEAT, "pid": pid, "session": sid,
               "starttime": starttime, "armed": self.armed,
               "home": os.environ["HELM_HOME"]}
        # THE ORIGIN FIELDS COME FROM THE PRODUCER'S OWN FORMATTER, never
        # spelled by hand here. A fixture that writes its own idea of the row
        # drifts from the shape `register` actually writes, and then every arm
        # below tests a world that does not exist. `origin=None` takes the
        # refusal branch, which is what an unstamped beacon really looks like.
        stamp = ({"origin": origin, "tool_use_id": "toolu_fixture",
                  "minted": self.armed} if origin else None)
        row.update(beacon_origin.row_fields(stamp, None if origin else "absent"))
        with open(beacons._entry_path(self.SEAT, pid), "w") as f:
            json.dump(row, f)

    def _census(self, sids=()):
        sids = list(sids) or [self.SID]
        return beacons.seat_census(
            self.SEAT, live=dict((s, LAUNCHER) for s in sids),
            proc_dir=self.proc, agents=None,
            homes=dict((s, self.cfg) for s in sids))

    def test_the_SUMMARY_accounts_for_every_seat_and_every_live_beacon(self):
        """THE LINE THAT GOES WRONG WHEN SOMEBODY ADDS A VERDICT -- including
        me, which is how this arm came to exist.

        `_summary` already carries a comment saying its counts are COUNTED and
        never derived by subtraction, because an earlier form reported every
        new verdict as covered the moment one was added. I added MISROUTED and
        did not join it to that line, so a seat that was neither covered nor
        deaf simply vanished from the summary and the buckets stopped summing
        to the seat total. The one verdict nobody had seen before was the one
        the summary declined to mention.

        SO THE ARM ASSERTS THE ARITHMETIC, not the wording. A verdict added
        later fails here without anybody having to remember this line
        exists."""
        self._beacon(4310, origin=beacon_origin.ORIGIN_SUBAGENT)
        row = self._census()
        self.assertEqual(beacons.MISROUTED, row["verdict"],
                         "MUST-HIT: this fixture must actually produce the "
                         "new verdict, or the sums below never see it")
        rep = {"seats": [row], "covered": [], "deaf": [], "deaf_in_effect": [],
               "vacant": [], "unproven": [], "misrouted": [row],
               "ghosts": [], "beacons": 1, "surplus": 0,
               "ownership": beacons.ownership([row], homes={})}
        own = rep["ownership"]
        self.assertEqual([row["seat"]], own["misrouted"])
        line = beacons._summary(rep)
        self.assertIn("1 MISROUTED", line,
                      "a verdict the summary does not name is a seat the "
                      "reader cannot account for: %r" % line)
        buckets = sum(len(rep[k]) for k in
                      ("covered", "deaf", "deaf_in_effect", "vacant",
                       "unproven", "misrouted"))
        self.assertEqual(len(rep["seats"]), buckets,
                         "the verdict buckets must account for every seat")
        self.assertEqual(
            sum(len(r["live"]) for r in rep["seats"]), own["live"])
        self.assertEqual(
            own["live"],
            own["top_level"] + own["sidechain"] + own["unattributed"],
            "the ownership fraction must account for every live beacon")

    def test_a_TOP_LEVEL_arm_is_never_misrouted(self):
        """THE CONTROL THAT KEEPS THE RUNG FROM BEING A BLANKET ALARM."""
        self._beacon(4301, origin=beacon_origin.ORIGIN_MAIN)
        row = self._census()
        self.assertEqual([beacons.OWNER_MAIN], row["owners"])
        self.assertNotEqual(beacons.MISROUTED, row["verdict"])
        self.assertEqual(0, row["unattributed"])
        self.assertFalse(beacons.unreachable(row),
                         "a top-level beacon is a working wake path")

    def test_a_SIDECHAIN_ONLY_arm_is_MISROUTED_and_unreachable(self):
        """The defect the row was filed for, produced rather than injected."""
        self._beacon(4302, origin=beacon_origin.ORIGIN_SUBAGENT)
        row = self._census()
        self.assertEqual([beacons.OWNER_SUBAGENT], row["owners"])
        self.assertEqual(beacons.MISROUTED, row["verdict"])
        self.assertIn("SIDECHAIN", row["why"])
        self.assertTrue(beacons.unreachable(row),
                        "MISROUTED is an ALARM: the seat has a live beacon "
                        "and no wake path, which is the worst combination "
                        "because every process instrument reads it covered")

    def test_ONE_proven_top_level_beacon_defeats_MISROUTED(self):
        """MIXED stays covered. The claim is about the SEAT's wake path, and
        one beacon that really reaches it is a wake path however many
        sidechain siblings stand beside it."""
        other = "ffffffff-1111-2222-3333-444444444444"
        self._beacon(4303, starttime=100, origin=beacon_origin.ORIGIN_MAIN)
        self._beacon(4304, starttime=200, sid=other, origin=beacon_origin.ORIGIN_SUBAGENT)
        row = self._census(sids=(self.SID, other))
        self.assertIn(beacons.OWNER_MAIN, row["owners"])
        self.assertIn(beacons.OWNER_SUBAGENT, row["owners"],
                      "MUST-HIT: the sidechain half really is present, or "
                      "this arm is just the top-level control again")
        self.assertNotEqual(beacons.MISROUTED, row["verdict"])
        self.assertFalse(beacons.unreachable(row))

    def test_an_UNSTAMPED_beacon_is_UNKNOWN_and_never_MISROUTED(self):
        """IGNORANCE IS NOT MISROUTING, and this is the direction that must
        never fail toward an alarm. THE QUESTION IS WHETHER A PRODUCER
        STAMPED THE REGISTRATION, and no stamp reached this row, so nothing
        can be attributed to it. With no producer installed anywhere, this is
        the answer every live beacon in the fleet gives -- which is why the
        alarm is a label with no instance rather than a fleet-wide siren."""
        self._beacon(4305)
        row = self._census()
        self.assertEqual([None], row["owners"])
        self.assertEqual(1, row["unattributed"],
                         "the denominator has to carry this, or zero "
                         "MISROUTED reads as proof that none is misrouted")
        self.assertNotEqual(beacons.MISROUTED, row["verdict"])

    def test_a_MISROUTED_seat_counts_as_unreachable(self):
        """The verdict has to reach the alarm class, or the census names the
        condition and no surface acts on it.

        THIS ARM NO LONGER INJECTS THE VERDICT. It was written while the
        producer was unwired, so a dict literal was the only way to put
        MISROUTED in front of the predicate -- and a hand-built row proves the
        predicate reads a word, never that anything in helm ever says it. The
        END-TO-END pairing now lives in `MisroutedVerdictTest`, which drives a
        planted sidechain transcript through `seat_census` and asserts
        `unreachable()` on the row THAT PRODUCED. What stays here is the
        MUST-MISS: a covered live seat is not an alarm, which is the half that
        keeps the predicate from being satisfied by returning True."""
        # UNCONDITIONAL POSITIVE CONTROL, on a verdict whose alarm membership
        # is not what this lane changed: without it the arm is a lone absence
        # and a predicate that returned False for everything would pass it.
        self.assertTrue(beacons.unreachable({"verdict": beacons.DEAF}),
                        "MUST-HIT: the alarm class is not empty")
        self.assertFalse(beacons.unreachable({"verdict": beacons.COVERED,
                                              "live": True}))



















class TheProducerStampSurvivesTheWholeElectionTest(Base):
    """THE STAMP IS SINGLE-USE AND `arm` WRITES THE ROW UP TO SIX TIMES.

    `arm` registers an `arming` marker before its per-seat election lock and
    then registers again on whichever branch commits. If the stamp were read
    inside `register`, the MARKER would consume it and every committed row
    would say UNKNOWN — a beacon whose origin was established and then thrown
    away by the machinery meant to record it. These arms exist because that is
    the bug the obvious wiring writes, and it is invisible: UNKNOWN is exactly
    what an un-stamped beacon looks like, so nothing would ever go red.
    """

    SEAT = "stamped-seat"
    SID = "11111111-2222-3333-4444-555555555555"

    def _arm_with(self, origin):
        nonce = beacon_origin.mint(origin, "toolu_arm", session=self.SID)
        self.assertIsNotNone(nonce)                 # the fixture's own control
        os.environ[beacon_origin.STAMP_ENV] = nonce
        self.addCleanup(os.environ.pop, beacon_origin.STAMP_ENV, None)
        return beacons.arm(self.SEAT, session=self.SID, pid=os.getpid(),
                           proc_dir=self.proc, reap=False)

    def test_the_COMMITTED_row_carries_the_origin_the_producer_stamped(self):
        report = self._arm_with(beacon_origin.ORIGIN_SUBAGENT)
        self.assertEqual(report["origin"], beacon_origin.ORIGIN_SUBAGENT)
        rows = [r for r in beacons.entries(self.SEAT) if r["pid"] == os.getpid()]
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["origin"], beacon_origin.ORIGIN_SUBAGENT)
        self.assertEqual(rows[0]["origin_tool_use"], "toolu_arm")

    def test_a_MAIN_stamp_reaches_the_row_too(self):
        """The positive control on the arm above: if the wiring only ever
        wrote one constant, both arms would still pass separately."""
        report = self._arm_with(beacon_origin.ORIGIN_MAIN)
        self.assertEqual(report["origin"], beacon_origin.ORIGIN_MAIN)
        rows = [r for r in beacons.entries(self.SEAT) if r["pid"] == os.getpid()]
        self.assertEqual(rows[0]["origin"], beacon_origin.ORIGIN_MAIN)

    def test_NO_stamp_is_UNKNOWN_and_says_why_rather_than_omitting_the_field(self):
        """The overwhelmingly common case today — no producer is installed —
        and it must be quiet, attributed to nobody, and DISTINGUISHABLE from a
        row written before this field existed."""
        os.environ.pop(beacon_origin.STAMP_ENV, None)
        report = beacons.arm(self.SEAT, session=self.SID, pid=os.getpid(),
                             proc_dir=self.proc, reap=False)
        self.assertEqual(report["origin"], beacon_origin.ORIGIN_UNKNOWN)
        rows = [r for r in beacons.entries(self.SEAT) if r["pid"] == os.getpid()]
        self.assertEqual(rows[0]["origin"], beacon_origin.ORIGIN_UNKNOWN)
        self.assertEqual(rows[0]["origin_reason"], "absent")

    def test_a_stamp_from_ANOTHER_session_never_attributes_this_beacon(self):  # noqa: VACUOUS_ASSERTION — the control is unconditional and on the same observable: exactly one row exists and its session equals this seat's, so an empty registry cannot satisfy the refusal being asserted
        """A crossed stamp is the one refusal that could otherwise look like a
        success, because the file is real and its origin is a real value."""
        nonce = beacon_origin.mint(beacon_origin.ORIGIN_MAIN, "toolu_x",
                                   session="00000000-0000-0000-0000-000000000000")
        self.assertIsNotNone(nonce)
        os.environ[beacon_origin.STAMP_ENV] = nonce
        self.addCleanup(os.environ.pop, beacon_origin.STAMP_ENV, None)
        report = beacons.arm(self.SEAT, session=self.SID, pid=os.getpid(),
                             proc_dir=self.proc, reap=False)
        self.assertEqual(report["origin"], beacon_origin.ORIGIN_UNKNOWN)
        rows = [r for r in beacons.entries(self.SEAT) if r["pid"] == os.getpid()]
        # UNCONDITIONAL CONTROL: the beacon really did register. Without this
        # an empty row list would satisfy every absence asserted below.
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["session"], self.SID)
        self.assertEqual(rows[0]["origin_reason"], "session_conflict")

    def test_the_stamp_is_spent_so_a_SECOND_arm_cannot_reuse_it(self):
        """Single-use measured at the door rather than at the helper: a replay
        of the same nonce by a later beacon must not inherit the first one's
        proven origin."""
        first = self._arm_with(beacon_origin.ORIGIN_SUBAGENT)
        self.assertEqual(first["origin"], beacon_origin.ORIGIN_SUBAGENT)
        second = beacons.arm(self.SEAT, session=self.SID, pid=os.getpid(),
                             proc_dir=self.proc, reap=False)
        self.assertEqual(second["origin"], beacon_origin.ORIGIN_UNKNOWN)
        self.assertEqual(
            [r for r in beacons.entries(self.SEAT)
             if r["pid"] == os.getpid()][0]["origin_reason"],
            "consumed_or_absent")


# ---------------------------------------------------------------------------
# THE UNENROLLED DENOMINATOR. `roll` is roster-intersected, so a live pane that
# never announced a seat is not DEAF, not VACANT and not UNPROVEN — it is not
# censused at all, and the seat total reads as a count of the agents on the box
# while being a count of the seats helm knows. Synthetic fixtures only.
# ---------------------------------------------------------------------------

class UnenrolledPaneTest(Base):
    NAMED = {"handle": "term_named", "worktree": "/w/named",
             "provenance": "helm-spawned", "seat": "alpha"}
    BARE = {"handle": "term_bare", "worktree": "/w/bare",
            "provenance": "unowned", "seat": None, "holds_agent": True}
    SHELL = {"handle": "term_shell", "worktree": "/w/shell",
             "provenance": "unowned", "seat": None, "holds_agent": False}

    def inventory(self, rows, note=None):
        from helm import orcaadopt
        return mock.patch.object(orcaadopt, "pane_rows",
                                 return_value=(rows, note))

    def test_only_the_panes_with_NO_identity_are_counted(self):
        with self.inventory([self.NAMED, self.BARE]):
            got = beacons.unenrolled_panes()
        # UNCONDITIONAL CONTROL on the same observable: the inventory really
        # did carry a NAMED pane, so "one unenrolled" is a cut and not an
        # empty read dressed as one.
        self.assertIsNone(got["why"])
        self.assertEqual([r["handle"] for r in got["rows"]], ["term_bare"])

    def test_a_bare_shell_is_reported_apart_and_never_counted(self):
        """task/2673: a pane with no agent process in it is not an unenrolled
        agent, so it leaves the count and is named on its own."""
        with self.inventory([self.NAMED, self.BARE, self.SHELL]):
            got = beacons.unenrolled_panes()
        self.assertEqual([r["handle"] for r in got["rows"]], ["term_bare"])
        self.assertEqual([r["handle"] for r in got["shells"]], ["term_shell"])
        rep = {"unenrolled": got}
        self.assertEqual(beacons._summary_unenrolled(rep),
                         "; 1 unenrolled pane (+1 bare shell)")
        self.assertIn("1 bare shell pane", beacons._unenrolled_line(rep))

    def test_a_pane_that_could_not_be_told_stays_counted_with_doubt(self):
        unsure = dict(self.BARE, holds_agent=None)
        with self.inventory([unsure]):
            got = beacons.unenrolled_panes()
        self.assertEqual(len(got["rows"]), 1)
        self.assertEqual(got["shells"], [])
        self.assertTrue(any("could not be told" in p for p in got["partial"]))

    def test_a_refused_inventory_answers_WHY_and_never_a_bare_zero(self):
        with self.inventory([], note="orca daemon not answering"):
            blind = beacons.unenrolled_panes()
        with self.inventory([self.BARE]):
            sighted = beacons.unenrolled_panes()
        # The PAIR is the test: an empty list means "every pane is named" only
        # when the probe could look, so the blind arm must be distinguishable
        # from a real zero by something other than the list.
        self.assertEqual(blind["rows"], [])
        self.assertEqual(blind["why"], "orca daemon not answering")
        self.assertEqual(len(sighted["rows"]), 1)
        self.assertIsNone(sighted["why"])

    def test_a_raising_inventory_is_named_not_swallowed(self):
        from helm import orcaadopt
        with mock.patch.object(orcaadopt, "pane_rows",
                               side_effect=RuntimeError("daemon exploded")):
            got = beacons.unenrolled_panes()
        self.assertEqual(got["rows"], [])
        self.assertIn("daemon exploded", got["why"])

    def test_a_pane_the_inventory_COULD_NOT_NAME_carries_its_doubt(self):
        doubtful = dict(self.BARE, identity_partial="the roster was unreadable")
        with self.inventory([doubtful]):
            got = beacons.unenrolled_panes()
        self.assertEqual(got["partial"], ["the roster was unreadable"])
        rep = {"unenrolled": got}
        self.assertIn("may be a seat this inventory could not name",
                      beacons._unenrolled_line(rep))

    # --- what the surfaces say -------------------------------------------

    def test_an_INVENTORY_NEVER_TAKEN_prints_no_clause_and_invents_no_zero(self):
        taken = {"unenrolled": {"rows": [self.BARE], "partial": [], "why": None}}
        # CONTROL FIRST, unconditionally: the same helpers DO speak when an
        # inventory was attached, so silence below is the missing key and not
        # a pair of functions that never say anything.
        self.assertIn("1 UNENROLLED PANE", beacons._unenrolled_line(taken))
        self.assertEqual(beacons._summary_unenrolled(taken),
                         "; 1 unenrolled pane")
        self.assertIsNone(beacons._unenrolled_line({}))
        self.assertEqual(beacons._summary_unenrolled({}), "")

    def test_the_summary_says_NOT_COUNTED_rather_than_zero_when_blind(self):
        blind = {"unenrolled": {"rows": [], "partial": [],
                                "why": "no metaharness installed"}}
        clause = beacons._summary_unenrolled(blind)
        self.assertIn("NOT COUNTED", clause)
        self.assertIn("no metaharness installed", clause)
        self.assertNotIn("0 unenrolled", clause)
        # CONTROL: a sighted zero is allowed to say zero.
        self.assertEqual(
            beacons._summary_unenrolled(
                {"unenrolled": {"rows": [], "partial": [], "why": None}}),
            "; 0 unenrolled panes")

    def test_the_fleet_read_carries_the_count_onto_the_summary_line(self):
        import io
        import contextlib
        self.roster("alpha")
        buf = io.StringIO()
        with self.inventory([self.NAMED, self.BARE]), \
                mock.patch.dict(os.environ, {"HELM_PROC": self.proc}), \
                contextlib.redirect_stdout(buf):
            rc = beacons.cmd_beacons([])
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        # The DEAF seat and the unenrolled pane are DISJOINT populations and
        # both must reach the reader: the repair for one does not touch the
        # other, which is the whole reason the line exists.
        self.assertIn("DEAF SEAT alpha", out)
        self.assertIn("1 UNENROLLED PANE", out)
        self.assertIn("; 1 unenrolled pane", out)

    def test_a_SINGLE_SEAT_read_takes_no_inventory_and_claims_no_total(self):
        """A --seat read is one seat's question, never a fleet total, so it
        must not imply one — and the CONTROL is the same verb without the flag,
        on the SAME two observables (did pane_rows get called, did the word
        reach the render). A control on some OTHER observable would prove only
        that the render prints something."""
        import io
        import contextlib
        self.roster("alpha")

        def run(argv):
            from helm import orcaadopt
            buf = io.StringIO()
            with mock.patch.object(
                    orcaadopt, "pane_rows",
                    return_value=([self.BARE], None)) as spy, \
                    mock.patch.dict(os.environ, {"HELM_PROC": self.proc}), \
                    contextlib.redirect_stdout(buf):
                beacons.cmd_beacons(argv)
            return spy.called, buf.getvalue()

        # POSITIVE CONTROL FIRST AND UNCONDITIONALLY: the fleet read DOES take
        # the inventory and DOES print the word, through this exact fixture.
        called, out = run([])
        self.assertTrue(called)
        self.assertIn("unenrolled", out)
        # …and the same call with --seat does neither.
        called, out = run(["--seat", "alpha"])
        self.assertFalse(called)
        self.assertNotIn("unenrolled", out)
        self.assertIn("alpha", out)

    def test_json_carries_the_inventory_for_a_machine_reader(self):
        import io
        import contextlib
        self.roster("alpha")
        buf = io.StringIO()
        with self.inventory([self.BARE]), \
                mock.patch.dict(os.environ, {"HELM_PROC": self.proc}), \
                contextlib.redirect_stdout(buf):
            beacons.cmd_beacons(["--json"])
        got = json.loads(buf.getvalue())
        self.assertEqual([r["handle"] for r in got["unenrolled"]["rows"]],
                         ["term_bare"])
        self.assertIsNone(got["unenrolled"]["why"])


class PromptStallLegTest(Base):
    """The beacons pass sees a frozen seat go DEAF and, on its own, names
    only that consequence. It now carries the prompt-stall watch on its own
    cadence — on the real host, fleet-wide — and a stall is a fault FOUND
    (exit 1), never a clean pass."""

    STALL = {"seat": "alpha", "pid": 4242, "waiting_for": "permission prompt",
             "waited_s": 600, "since": 1.0, "session": SID_A, "root": "/r"}

    def leg(self, seat=None, env=None, got=None, boom=None):
        err = io.StringIO()
        env = dict(env or {})
        with mock.patch.dict(os.environ, env), \
                mock.patch("helm.planprompt.stall_pass",
                           side_effect=boom,
                           return_value=got or {"stalls": [self.STALL],
                                                "lines": ["seat alpha FROZEN"]}
                           ) as sp, \
                contextlib.redirect_stderr(err):
            if "HELM_PROC" not in env:
                os.environ.pop("HELM_PROC", None)
            out = beacons._prompt_stall_leg(seat)
        return out, sp, err.getvalue()

    def test_the_real_host_fleet_pass_runs_the_watch_and_prints_it(self):
        out, sp, err = self.leg()
        sp.assert_called_once_with()
        self.assertEqual(out, [self.STALL])
        self.assertIn("seat alpha FROZEN", err)

    def test_a_seat_read_or_a_foreign_proc_tree_never_reads_the_live_host(self):  # noqa: VACUOUS_ASSERTION — test_the_real_host_fleet_pass_runs_the_watch_and_prints_it drives the same seam and records the call
        """CONTROL above proves the leg calls the watch; here it must not."""
        for kw in ({"seat": "alpha"}, {"env": {"HELM_PROC": self.proc}}):
            out, sp, _err = self.leg(**kw)
            self.assertEqual(out, [])
            sp.assert_not_called()

    def test_a_broken_watch_is_reported_and_never_fatal(self):
        out, _sp, err = self.leg(boom=RuntimeError("fixture"))
        self.assertEqual(out, [])
        self.assertIn("prompt-stall watch skipped", err)

    def test_a_blind_prompt_reading_prints_UNKNOWN_in_the_pass(self):  # noqa: VACUOUS_ASSERTION — the empty return is the no-stall claim; the UNKNOWN text read back from stderr is the positive control
        """The REAL stall pass behind the leg: a session whose presence record
        could not be read reaches the beacons output as UNKNOWN."""
        found = {"read": True, "why": None, "sessions": 0, "stalls": [],
                 "unnamed": 0,
                 "blind": [{"seat": "alpha", "pid": 4242, "why": "record-stale"}]}
        err = io.StringIO()
        with mock.patch.dict(os.environ, {}), \
                mock.patch("helm.planprompt.prompt_stalls", return_value=found), \
                contextlib.redirect_stderr(err):
            os.environ.pop("HELM_PROC", None)
            out = beacons._prompt_stall_leg(None)
        self.assertEqual(out, [])
        self.assertIn("1 session(s) UNKNOWN", err.getvalue())
        self.assertIn("seat alpha (record-stale)", err.getvalue())

    def test_a_stalled_seat_makes_a_clean_census_exit_one(self):
        self.roster("alpha")
        self.waiter(301, "alpha", sid=SID_A)
        self.agent(90, "alpha")

        def run(stalled):
            with mock.patch.object(beacons, "live_sessions",
                                   return_value={SID_A: 90}), \
                    mock.patch.object(beacons, "_prompt_stall_leg",
                                      return_value=stalled), \
                    mock.patch.dict(os.environ, {"HELM_PROC": self.proc}), \
                    mock.patch("helm.chat.post"), \
                    contextlib.redirect_stdout(io.StringIO()):
                return beacons.cmd_beacons(["--post"])
        self.assertEqual(run([]), 0)             # CONTROL: the clean census
        self.assertEqual(run([self.STALL]), 1)
