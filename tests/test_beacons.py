#!/usr/bin/env python3
"""helm beacons — the registry, the lifecycle, and the two-directional alarm.

A seat's inbox beacon is its ONLY wake path, so every test here asserts an
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
import glob
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import beacons, chat, home, seats  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "MELD_CHAT_NODE_URL", "HELM_PROC", "MELD_PROC",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID")

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

    Same could-not-look-recorded-as-a-fact class @kimi found in proxywatch's
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
        """@kimi's orphan shells: detached waiters whose claude session exited.
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
        and killing on that guess severs a live seat's only wake path."""
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
        """@kimi's lesson, and the reason a file beats a harness task id:
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
        """@helm-claude-2, #helm 624: reap the PYTHON process, not the grep
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

    def spawn(self, seat=None, sid=SID_A, helm_home=None, shape="waiter"):
        env = dict(os.environ)
        env["HELM_HOME"] = helm_home or os.environ["HELM_HOME"]
        env["HOME"] = self.tmp
        env["HELM_CHAT_NAME"] = seat or self.seat
        env["CLAUDE_CODE_SESSION_ID"] = sid
        argv = [sys.executable, self.script]
        if shape == "waiter":
            argv += ["chat", "wait", "--seat", seat or self.seat, "--follow"]
        else:
            argv += ["chat", "post", "hello"]          # a helm proc, not a beacon
        p = subprocess.Popen(argv, env=env, stdout=subprocess.DEVNULL,
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
        # NON-VACUITY: on a box with too few cores the two are equal anyway,
        # and the assertion above would hold for the wrong reason. Only claim
        # the strict version where the arithmetic can actually separate them.
        if (os.cpu_count() or 0) >= gate._CORES_PER_SUITE * (gate.SUITE_CAP + 1):
            self.assertGreater(empty, with_pane,
                               "this box has the cores to raise the cap, so "
                               "the two censuses must differ — otherwise the "
                               "test cannot see the defect it exists for")
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
        self.assertIn("2 seats, 1 covered, 0 DEAF, 1 VACANT, 0 UNPROVEN",
                      CensusTest.render(self, rep))

    def test_the_census_still_signals_NOTHING_at_a_VACANT_seat(self):
        """READ-ONLY IS ABSOLUTE. Three other fleets' beacons live on the box
        this landed from; reaping by pattern across a shared process table is
        how a live seat's only wake path gets severed. A VACANT verdict is a
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
    """THE OPPOSITE EDGE — codex-2's amendment to the delivery-edge fix, and
    the half latch revalidation could not see.

    Re-checking only the LATCHES asks "has anyone delivered this yet?" and
    never "is this still TRUE?". Reproduced on this tree before the fix, both
    polarities:

      ALARM:    pass A derives alarm=True; the seat RECOVERS; pass B writes
                covered/alarm=False and derives NO edge (both latches are
                still false, so nothing is owed); A's batch passes latch
                revalidation and posts "DEAF SEAT alpha ... nothing can wake
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
        self.assertEqual(len(threads), 1, "the chat leg never ran")
        threads[0].join(2)
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
        "nothing can wake it" and "the instruments could not name it" — and at
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


if __name__ == "__main__":
    unittest.main()
