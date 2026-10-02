#!/usr/bin/env python3
"""helm pressure-watch and the stall clause on a TIMED OUT line (task/3714).

When agents.slice itself stalls, every seat stalls together, and a reader
that runs from Stop hooks inside the slice stalls with them, so nothing
posts; every hook that times out then says only TIMED OUT. These arms drive
the watcher over a cgroup and /proc tree each test builds, one pass at a
time with the clock in the test's hand, through every cell of the surface:
calm, a short spike, a sustained stall, a stall that continues, a clear and
a new stall, an UNKNOWN reading, a chat that cannot be reached, a row still
owed when its stall closes, a process read that hangs, and a secret planted
in a command line; and the two halves of the timeout clause, the Python one
and the shell one in bin/helm-hook, readable and not. Nothing here reads the
host's /proc or cgroup tree, posts to a real room, pushes to a phone or
reaches systemd.
"""
import contextlib
import glob
import io
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from tests._tmphome import fake_user_systemd
from tests._tmphome import home as _tmp_home

_tmp_home(prefix="helm-test-pressurewatch-", var="HELM_HOME")

from helm import hookrun, pressurewatch, seatceiling  # noqa: E402
from helm import seats_integrator, tasks  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WRAPPER = os.path.join(ROOT, "bin", "helm-hook")
MANAGER = "user.slice/user-1000.slice/user@1000.service"
AGENTS = MANAGER + "/agents.slice"
GB = 1024 ** 3
T0 = 1_000_000.0
#: A synthetic secret planted as a plain positional argument: 40 characters of
#: the word class the old namer kept, so any argv word that leaks carries it.
MARK = "zqplantedmarker0a1b2c3d4e5f6a7b8c9d0e1f2"


def _w(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _psi(avg, total):
    """A *.pressure body whose `some` and `full` lines carry `avg` and
    `total`, the shape the kernel writes, newline-terminated."""
    return ("some avg10=%.2f avg60=%.2f avg300=%.2f total=%d\n"
            "full avg10=%.2f avg60=%.2f avg300=%.2f total=%d\n"
            % (avg, avg, avg, total, avg, avg, avg, total))


class Tree(object):
    """A user manager's cgroup tree and a /proc, built in a temp dir: the
    watcher runs from app.slice (proc/self/cgroup says so), agents.slice
    carries the fleet's PSI and its cpu.stat, /proc/stat carries the host's
    process count, and each seat slice holds processes whose own cgroup lines
    name it.

    EACH CPU WINDOW moves every sampled process's ticks on by (after -
    before), agents.slice's usage_usec by the ticks it moved plus
    `short_usec` (cpu no pid sample sees), the host's process count by
    `window_forks`, and starts each process queued with born(), alive after
    the window."""

    def __init__(self, case, controllers=True, fleet=True):
        self.base = tempfile.mkdtemp(prefix="helm-test-pressurewatch-tree-")
        case.addCleanup(shutil.rmtree, self.base, True)
        self.cg = os.path.join(self.base, "cgroup")
        self.proc = os.path.join(self.base, "proc")
        self.fleet = os.path.join(self.cg, AGENTS)
        os.makedirs(self.cg)
        if controllers:
            _w(os.path.join(self.cg, "cgroup.controllers"), "cpu memory io\n")
        _w(os.path.join(self.proc, "self", "cgroup"),
           "0::/%s/app.slice/helm-pressure-watch.service\n" % MANAGER)
        _w(os.path.join(self.proc, "pressure", "cpu"), _psi(3.0, 5))
        _w(os.path.join(self.proc, "pressure", "memory"), _psi(0.5, 7))
        _w(os.path.join(self.proc, "meminfo"),
           "MemTotal: 64000000 kB\nSwapTotal: 1000 kB\nSwapFree: 480 kB\n")
        self.totals = {"cpu": 10 ** 9, "memory": 10 ** 9}
        self.ticks, self.at, self.births = {}, {}, []
        self.usage, self.forks = 7 * 10 ** 9, 40000
        self.short_usec = self.window_forks = 0
        self.cpu_stat = True
        if fleet:
            os.makedirs(self.fleet)
            _w(os.path.join(self.fleet, "memory.current"), "%d\n" % (47 * GB))
            _w(os.path.join(self.fleet, "memory.high"), "%d\n" % (48 * GB))
            _w(os.path.join(self.fleet, "cpu.max"), "800000 100000\n")
            self.write(0, 0)
        self.counters()

    def counters(self):
        """/proc/stat's `processes` and agents.slice's cpu.stat, in the
        shapes the kernel writes them."""
        _w(os.path.join(self.proc, "stat"),
           "cpu  10 0 10 100 0 0 0 0 0 0\ncpu0 10 0 10 100 0 0 0 0 0 0\n"
           "intr 9 1 2 3\nctxt 99\nbtime 1\nprocesses %d\nprocs_running 2\n"
           "procs_blocked 0\n" % self.forks)
        if self.cpu_stat and os.path.isdir(self.fleet):
            _w(os.path.join(self.fleet, "cpu.stat"),
               "usage_usec %d\nuser_usec %d\nsystem_usec 0\n"
               "nr_periods 0\nnr_throttled 0\nthrottled_usec 0\n"
               % (self.usage, self.usage))

    def write(self, cpu, memory):
        """The fleet's two PSI files, `avg` set to each kind's percent."""
        for kind, pct in (("cpu", cpu), ("memory", memory)):
            _w(os.path.join(self.fleet, "%s.pressure" % kind),
               _psi(pct, self.totals[kind]))

    def advance(self, cpu, memory, seconds=60):
        """Grow each `some` total by `pct` percent of `seconds`: the stall
        time the kernel would have counted over that window."""
        self.totals["cpu"] += int(cpu / 100.0 * seconds * 1e6)
        self.totals["memory"] += int(memory / 100.0 * seconds * 1e6)
        self.write(cpu, memory)

    def seat(self, name, pid, current, high, rss_gb, cmdline, ticks=(0, 0),
             env_name=None):
        """One seat slice with one process. `ticks` is its utime+stime
        before and after the CPU sample window."""
        rel = "%s/%s" % (AGENTS, seatceiling.seat_slice_name(name))
        d = os.path.join(self.cg, rel)
        os.makedirs(os.path.join(d, "run-p%d.scope" % pid), exist_ok=True)
        _w(os.path.join(d, "memory.current"), "%d\n" % current)
        _w(os.path.join(d, "memory.high"), "%d\n" % high)
        _w(os.path.join(d, "memory.stat"), "anon %d\nshmem 0\n" % current)
        _w(os.path.join(d, "memory.events"), "low 0\nhigh 0\nmax 0\n")
        _w(os.path.join(d, "memory.pressure"), _psi(35.0, 1))
        _w(os.path.join(d, "cpu.pressure"), _psi(12.0, 1))
        self.process(rel, pid, cmdline, ticks, rss_gb, env_name)
        return d

    def process(self, rel, pid, cmdline, ticks, rss_gb=0.25, env_name=None,
                cwd=None):
        """One process in the slice at `rel`; `cwd` becomes its cwd link."""
        pd = os.path.join(self.proc, str(pid))
        _w(os.path.join(pd, "cgroup"), "0::/%s/run-p%d.scope\n" % (rel, pid))
        _w(os.path.join(pd, "wchan"), "0")
        pages = int(rss_gb * GB) // os.sysconf("SC_PAGE_SIZE")
        _w(os.path.join(pd, "statm"), "%d %d 0 0 0 0 0\n" % (pages * 2, pages))
        _w(os.path.join(pd, "cmdline"), "\0".join(cmdline) + "\0")
        _w(os.path.join(pd, "comm"), os.path.basename(cmdline[0]) + "\n")
        _w(os.path.join(pd, "environ"),
           ("HELM_CHAT_NAME=%s\0" % env_name if env_name else "")
           + "PATH=/usr/bin\0")
        if cwd is not None:
            os.symlink(cwd, os.path.join(pd, "cwd"))
        self.ticks[pid] = ticks
        self.at[pid] = ticks[0]
        self.stat(pid, ticks[0])

    def born(self, seat, pid, cmdline, ticks, cwd=None):
        """A process the next CPU window starts in `seat`'s slice, alive
        after it, having spent `ticks` since its birth."""
        rel = "%s/%s" % (AGENTS, seatceiling.seat_slice_name(seat))
        self.births.append((rel, pid, cmdline, ticks, cwd))

    def stat(self, pid, ticks):
        _w(os.path.join(self.proc, str(pid), "stat"),
           "%d (a proc) S 1 1 1 0 -1 0 0 0 0 0 %d 0 0 0 20 0 1 0\n"
           % (pid, ticks))

    def sleep(self, seconds):
        """The sample seam: the CPU window moves every process's ticks on by
        (after - before), and the counters with them (see the class); any
        other sleep moves nothing."""
        if seconds != pressurewatch.CPU_WINDOW_S:
            return
        moved = 0
        for pid, (a, b) in self.ticks.items():
            self.at[pid] += b - a
            moved += b - a
            self.stat(pid, self.at[pid])
        self.usage += moved * 10 ** 6 // os.sysconf("SC_CLK_TCK") \
            + self.short_usec
        self.forks += self.window_forks
        for rel, pid, cmdline, ticks, cwd in self.births:
            self.process(rel, pid, cmdline, (ticks, ticks), cwd=cwd)
        self.births = []
        self.counters()


class Room(object):
    """The post seam: records every row, raises for the first `fail`."""

    def __init__(self, fail=0):
        self.calls, self.fail = [], fail

    def __call__(self, text, room=None, event_id=None):
        self.calls.append({"text": text, "room": room, "event": event_id})
        if self.fail:
            self.fail -= 1
            raise OSError("the chat node is unreachable")
        return {"id": "row%d" % len(self.calls)}

    @property
    def stalls(self):
        return [c for c in self.calls if "FLEET STALL" in c["text"]]

    @property
    def unknowns(self):
        return [c for c in self.calls if "fleet stall UNKNOWN" in c["text"]]


class _Killed(BaseException):
    """The unit's TimeoutStartSec kill, landing inside a post that never
    returned. A BaseException, as the SIGTERM is: no `except Exception` in
    the pass catches it."""


class KilledRoom(Room):
    """A post the pass is killed inside, for the first `kills` calls: an
    @all row waits on the roster lock, and a stalled seat can hold it."""

    def __init__(self, kills):
        Room.__init__(self)
        self.kills = kills

    def __call__(self, text, room=None, event_id=None):
        if self.kills:
            self.kills -= 1
            self.calls.append({"text": text, "room": room, "event": event_id})
            raise _Killed()
        return Room.__call__(self, text, room=room, event_id=event_id)


class Phone(object):
    """notify's two calls: configured() and owner_push()."""

    def __init__(self, on=True, ok=True):
        self.on, self.ok, self.pushes = on, ok, []

    def configured(self):
        return self.on

    def owner_push(self, body, title=None, receipt=None):
        self.pushes.append((body, title))
        return self.ok


class _Base(unittest.TestCase):
    def setUp(self):
        for path in (pressurewatch.state_path(),
                     pressurewatch.alarm_log_path()):
            if os.path.exists(path):
                os.remove(path)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for key in (seatceiling.FLEET_ENV, pressurewatch.PCT_ENV,
                    pressurewatch.SUSTAIN_ENV):
            os.environ.pop(key, None)
        self.room, self.phone = Room(), Phone()

    def assert_logged(self, text):
        """Every line of a row is in the alarm log: the alarm is logged
        before any post, and the process detail after it."""
        with open(pressurewatch.alarm_log_path(), encoding="utf-8") as fh:
            logged = fh.read()
        for line in text.split("\n"):
            self.assertIn(line, logged)

    def tick(self, tree, now, apply=True):
        return pressurewatch.tick(apply=apply, now=now, root=tree.cg,
                                  proc=tree.proc, post=self.room,
                                  phone=self.phone, sleep=tree.sleep,
                                  owner="owner-handle")

    def stalled(self, tree, passes, start=T0, cpu=85.0, memory=40.0):
        """[(rc, lines)] for a calm first pass at `start` and then `passes`
        hot passes a minute apart."""
        out = [self.tick(tree, start)[:2]]
        for i in range(1, passes + 1):
            tree.advance(cpu, memory)
            out.append(self.tick(tree, start + 60 * i)[:2])
        return out

    def consumers_tree(self):
        tree = Tree(self)
        tree.seat("seat-a", 4101, current=int(9.5 * GB),
                  high=12 * GB, rss_gb=4.5,
                  cmdline=["/usr/bin/python3", "./bin/helm", "web",
                           "--port", "7488"], ticks=(100, 130),
                  env_name="seat-a")
        tree.seat("pid4075358", 4202, current=int(6.25 * GB), high=16 * GB,
                  rss_gb=1.25,
                  cmdline=["python3", "/tmp/x/corpus.py", "--token=SECRET"],
                  ticks=(0, 290), env_name="seat-b")
        tree.seat("seat-c", 4303, current=1 * GB, high=12 * GB,
                  rss_gb=0.5, cmdline=["/opt/claude/versions/2.1.285"],
                  ticks=(5, 6))
        return tree


class SurfaceByStateTest(_Base):
    """Every cell the task names, each driven through real passes."""

    def test_no_pressure_is_silent(self):  # noqa: VACUOUS_ASSERTION — the calm line and the measured 1% share of the same passes are asserted before the empty room
        tree = Tree(self)
        rc, lines, _ = self.tick(tree, T0)
        self.assertEqual(rc, 0)
        self.assertTrue(lines[0].startswith("pressure-watch: calm — "), lines)
        tree.advance(1.0, 0.0)
        rc, lines, data = self.tick(tree, T0 + 60)
        self.assertEqual(rc, 0)
        self.assertIn("cpu 1%, memory 0% of the last 60s", lines[0])
        self.assertAlmostEqual(data["shares"]["cpu"]["percent"], 1.0, 3)
        self.assertEqual(self.room.calls, [])
        self.assertEqual(self.phone.pushes, [])

    def test_a_full_slice_with_no_stall_is_calm(self):  # noqa: VACUOUS_ASSERTION — every pass asserts rc 0 and the last asserts its calm line before the empty room
        """97-99% of memory.high with PSI 0.00 is the fleet's healthy steady
        state: fullness is reported and never alarmed on."""
        tree = Tree(self)
        _w(os.path.join(tree.fleet, "memory.current"), "%d\n" % (49 * GB))
        for i in range(6):
            tree.advance(0.0, 0.0)
            rc, lines, _ = self.tick(tree, T0 + 60 * i)
            self.assertEqual(rc, 0, lines)
        self.assertIn("calm", lines[0])
        self.assertEqual(self.room.calls, [])

    def test_a_short_spike_under_the_sustain_window_is_silent(self):  # noqa: VACUOUS_ASSERTION — the HOT lines of the spike passes are asserted positively before the empty room
        tree = Tree(self)
        got = self.stalled(tree, 2)
        self.assertIn("HOT for 60s of the 180s", got[1][1][0])
        self.assertIn("HOT for 120s of the 180s", got[2][1][0])
        tree.advance(0.0, 0.0)
        rc, lines, _ = self.tick(tree, T0 + 180)
        self.assertEqual(rc, 0)
        self.assertIn("calm", lines[0])
        self.assertEqual([rc for rc, _l in got], [0, 0, 0])
        self.assertEqual(self.room.calls, [])
        self.assertEqual(self.phone.pushes, [])

    def test_a_sustained_stall_posts_one_row_naming_the_consumers(self):
        tree = self.consumers_tree()
        got = self.stalled(tree, 3)
        self.assertEqual([rc for rc, _l in got], [0, 0, 0, 1])
        self.assertEqual(len(self.room.calls), 1, self.room.calls)
        row = self.room.calls[0]
        self.assertEqual(row["room"], "main")
        self.assertTrue(row["event"].startswith("pressure-watch-"))
        text = row["text"]
        self.assertTrue(text.startswith("[MEASURED] @all @owner-handle FLEET "
                                        "STALL: agents.slice has been stalled "
                                        "for 3m"), text)
        self.assertIn("cpu 85%, memory 40% of the last 60s", text)
        self.assertIn("47.00G of 48.00G memory.high (98%) (reported, never "
                      "alarmed on), cpu quota 8.0 cores", text)
        self.assertIn("swap 48% free", text)
        # BY SLICE, biggest first, each against its own memory.high, with
        # the seat its processes name.
        self.assertIn("Slices by memory: agents-seat_a.slice "
                      "(seat-a) 9.50G of 12.00G high (79%), stall "
                      "memory 35%, cpu 12% (avg60); agents-pid4075358.slice "
                      "(seat-b) 6.25G of 16.00G high (39%)", text)
        self.assertIn("agents-seat_c.slice (seat unknown)", text)
        # BY PROCESS, named by its PROGRAM only (the script, for an
        # interpreter), with the seat that launched each: no other word of a
        # command line (a secret can ride one) reaches the room.
        self.assertIn("Processes by memory: 4.50G pid 4101 helm, seat-a; "
                      "1.25G pid 4202 corpus.py, seat-b", text)
        self.assertIn("Processes by cpu: 290% cpu pid 4202 corpus.py, "
                      "seat-b; 30% cpu pid 4101 helm, seat-a", text)
        self.assertIn("pid 4303 claude, agents-seat_c.slice", text)
        self.assertNotIn("SECRET", text)
        self.assertNotIn("7488", text)
        self.assertNotIn("helm web", text)
        self.assertEqual(len(self.phone.pushes), 1)
        body, title = self.phone.pushes[0]
        self.assertEqual(title, pressurewatch.PUSH_TITLE)
        self.assertIn("FLEET STALL 3m", body)
        # The push leaves before any process is read: it names the biggest
        # slice from its cgroup files alone.
        self.assertIn("Biggest slice: agents-seat_a.slice 9.50G of 12.00G "
                      "high", body)
        self.assertIn("pressure-watch: STALL since", got[3][1][0])
        self.assertIn("row posted; phone pushed", got[3][1][0])
        self.assert_logged(text)

    def test_a_stall_that_continues_posts_no_second_row(self):
        tree = Tree(self)
        self.stalled(tree, 3)
        self.assertEqual(len(self.room.stalls), 1)
        for i in range(4, 9):
            tree.advance(95.0, 60.0)
            rc, lines, _ = self.tick(tree, T0 + 60 * i)
            self.assertEqual(rc, 1)
            self.assertIn("STALL since", lines[0])
        self.assertEqual(len(self.room.stalls), 1)
        self.assertEqual(len(self.phone.pushes), 1)

    def test_a_brief_dip_inside_a_stall_does_not_post_again(self):
        tree = Tree(self)
        self.stalled(tree, 3)
        tree.advance(2.0, 0.0)                     # one clear pass
        self.tick(tree, T0 + 240)
        for i in range(5, 9):
            tree.advance(90.0, 0.0)
            self.tick(tree, T0 + 60 * i)
        self.assertEqual(len(self.room.stalls), 1)

    def test_a_clear_then_a_new_stall_posts_a_new_row(self):
        tree = Tree(self)
        self.stalled(tree, 3)
        first = self.room.stalls[0]["event"]
        tree.advance(0.0, 0.0)
        rc, lines, _ = self.tick(tree, T0 + 240)
        self.assertEqual(rc, 1, "one clear minute does not close the episode")
        tree.advance(0.0, 0.0)
        rc, lines, _ = self.tick(tree, T0 + 300)
        self.assertEqual(rc, 0)
        self.assertTrue(any("is CLEAR" in line for line in lines), lines)
        for i in (6, 7):
            tree.advance(80.0, 0.0)
            self.tick(tree, T0 + 60 * i)
        self.assertEqual(len(self.room.stalls), 1,
                         "a new stall posted before it was sustained")
        tree.advance(80.0, 0.0)
        rc, lines, _ = self.tick(tree, T0 + 60 * 8)
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.room.stalls), 2)
        self.assertNotEqual(self.room.stalls[1]["event"], first)
        self.assertEqual(len(self.phone.pushes), 2)

    def test_a_gap_between_passes_proves_no_run(self):  # noqa: VACUOUS_ASSERTION — the pass after the gap asserts its own HOT line, the run restarted at 60s
        """A pass after a gap past MAX_GAP_S starts the run over: nothing
        read the fleet across the gap."""
        tree = Tree(self)
        self.stalled(tree, 2)
        tree.advance(90.0, 0.0, seconds=600)
        rc, lines, _ = self.tick(tree, T0 + 120 + 600)
        self.assertEqual(rc, 0)
        self.assertIn("HOT for 60s of the 180s", lines[0])
        self.assertIn("(kernel avg60)", lines[0])
        self.assertEqual(self.room.calls, [])

    def test_the_knobs_move_the_line_and_a_bad_value_is_the_default(self):  # noqa: VACUOUS_ASSERTION — the stall row the knobs let through is asserted by count and text
        tree = Tree(self)
        os.environ[pressurewatch.PCT_ENV] = "50"
        os.environ[pressurewatch.SUSTAIN_ENV] = "60"
        self.tick(tree, T0)
        tree.advance(40.0, 0.0)
        self.tick(tree, T0 + 60)
        self.assertEqual(self.room.calls, [], "40% is under a 50% line")
        tree.advance(60.0, 0.0)
        self.tick(tree, T0 + 120)
        self.assertEqual(len(self.room.stalls), 1,
                         "60% held 60s is a stall under these knobs")
        self.assertIn("this alarm fires at 50% held 60s",
                      self.room.stalls[0]["text"])
        for value in ("0", "-3", "abc", "101", ""):
            os.environ[pressurewatch.PCT_ENV] = value
            self.assertEqual(pressurewatch.stall_pct(),
                             pressurewatch.STALL_PCT, value)
        for value in ("0", "-3", "abc", "1.5", ""):
            os.environ[pressurewatch.SUSTAIN_ENV] = value
            self.assertEqual(pressurewatch.sustain_s(),
                             pressurewatch.SUSTAIN_S, value)

    def test_a_read_only_pass_posts_and_writes_nothing(self):  # noqa: VACUOUS_ASSERTION — the pass asserts its calm line and its read-only line before the absent state file
        tree = Tree(self)
        rc, lines, _ = self.tick(tree, T0, apply=False)
        self.assertEqual(rc, 0)
        self.assertIn("pressure-watch: calm", lines[0])
        self.assertIn("read only", lines[-1])
        self.assertFalse(os.path.exists(pressurewatch.state_path()))
        self.assertEqual(self.room.calls, [])


class UnknownIsNeverCalmTest(_Base):
    """A reading that could not be taken says UNKNOWN once, never calm, and
    never raises."""

    def assert_unknown(self, tree, why):
        for i in range(3):
            rc, lines, _ = self.tick(tree, T0 + 60 * i)
            self.assertEqual(rc, 1, lines)
            self.assertTrue(any("UNKNOWN — " + why in line for line in lines),
                            lines)
            self.assertFalse(any("calm" in line for line in lines), lines)
        self.assertEqual(len(self.room.unknowns), 1, self.room.calls)
        self.assertIn("@owner-handle fleet stall UNKNOWN: " + why,
                      self.room.unknowns[0]["text"])
        self.assertIn("read no all-clear", self.room.unknowns[0]["text"])
        self.assertEqual(self.room.stalls, [])

    def test_no_cgroup_v2(self):  # noqa: VACUOUS_ASSERTION — assert_unknown asserts the UNKNOWN line on every pass and the one UNKNOWN row by count and text
        tree = Tree(self, controllers=False)
        self.assert_unknown(tree, "there is no cgroup v2 hierarchy at %s"
                            % tree.cg)

    def test_no_agents_slice(self):  # noqa: VACUOUS_ASSERTION — assert_unknown asserts the UNKNOWN line on every pass and the one UNKNOWN row by count and text
        tree = Tree(self, fleet=False)
        self.assert_unknown(tree, "there is no agents.slice at %s"
                            % tree.fleet)

    def test_a_process_under_no_user_manager(self):  # noqa: VACUOUS_ASSERTION — assert_unknown asserts the UNKNOWN line on every pass and the one UNKNOWN row by count and text
        tree = Tree(self)
        _w(os.path.join(tree.proc, "self", "cgroup"),
           "0::/system.slice/cron.service\n")
        self.assert_unknown(tree, seatceiling.NO_FLEET_PATH)

    def test_a_v1_only_cgroup_line(self):  # noqa: VACUOUS_ASSERTION — assert_unknown asserts the UNKNOWN line on every pass and the one UNKNOWN row by count and text
        tree = Tree(self)
        _w(os.path.join(tree.proc, "self", "cgroup"),
           "12:memory:/user.slice\n")
        self.assert_unknown(tree, seatceiling.NO_FLEET_PATH)

    def test_a_missing_pressure_file(self):  # noqa: VACUOUS_ASSERTION — assert_unknown asserts the UNKNOWN line on every pass and the one UNKNOWN row by count and text
        tree = Tree(self)
        os.remove(os.path.join(tree.fleet, "memory.pressure"))
        self.assert_unknown(tree, "memory.pressure at %s would not read"
                            % tree.fleet)

    def test_a_garbled_pressure_file(self):  # noqa: VACUOUS_ASSERTION — assert_unknown asserts the UNKNOWN line on every pass and the one UNKNOWN row by count and text
        tree = Tree(self)
        _w(os.path.join(tree.fleet, "cpu.pressure"), "some avg10=x total=\n")
        _w(os.path.join(tree.fleet, "memory.pressure"), "")
        self.assert_unknown(tree, "cpu.pressure and memory.pressure at %s "
                            "would not read" % tree.fleet)

    def test_a_readable_pass_rearms_it_and_it_is_said_again(self):
        tree = Tree(self)
        os.remove(os.path.join(tree.fleet, "memory.pressure"))
        self.tick(tree, T0)
        tree.write(0, 0)
        rc, lines, _ = self.tick(tree, T0 + 60)
        self.assertEqual(rc, 0, lines)
        os.remove(os.path.join(tree.fleet, "memory.pressure"))
        self.tick(tree, T0 + 120)
        self.assertEqual(len(self.room.unknowns), 2)
        self.assertNotEqual(self.room.unknowns[0]["event"],
                            self.room.unknowns[1]["event"])

    def test_an_undelivered_unknown_is_retried_with_its_id(self):
        tree = Tree(self, fleet=False)
        self.room.fail = 1
        rc, lines, _ = self.tick(tree, T0)
        self.assertTrue(any("did not post" in line for line in lines), lines)
        self.tick(tree, T0 + 60)
        self.tick(tree, T0 + 120)
        self.assertEqual(len(self.room.unknowns), 2)
        self.assertEqual(self.room.unknowns[0]["event"],
                         self.room.unknowns[1]["event"])

    def test_unknown_neither_closes_an_episode_nor_posts_a_second(self):
        tree = Tree(self)
        self.stalled(tree, 3)
        saved = os.path.join(tree.base, "cpu.pressure")
        shutil.move(os.path.join(tree.fleet, "cpu.pressure"), saved)
        rc, lines, _ = self.tick(tree, T0 + 240)
        self.assertEqual(rc, 1)
        self.assertIn("STALL since", lines[0])
        self.assertTrue(any("UNKNOWN — cpu.pressure" in line
                            for line in lines), lines)
        shutil.move(saved, os.path.join(tree.fleet, "cpu.pressure"))
        tree.advance(90.0, 0.0)
        self.tick(tree, T0 + 300)
        self.assertEqual(len(self.room.stalls), 1)

    def test_a_partly_read_pass_is_judged_on_the_kind_that_read(self):  # noqa: VACUOUS_ASSERTION — the one UNKNOWN row and the one stall row are asserted by count and text after the loop
        """One pressure file that will not read is UNKNOWN, said once and
        never an all-clear; the kind that did read is still judged, so a
        stall measured on it posts its one row."""
        tree = Tree(self)
        gone = os.path.join(tree.fleet, "memory.pressure")
        for i in range(4):
            if i:
                tree.advance(85.0, 0.0)
            os.remove(gone)
            rc, lines, _ = self.tick(tree, T0 + 60 * i)
            self.assertEqual(rc, 1, lines)
            self.assertFalse(any("calm" in line for line in lines), lines)
        self.assertEqual(len(self.room.unknowns), 1, self.room.calls)
        self.assertEqual(len(self.room.stalls), 1, self.room.calls)
        self.assertIn("STALL since", lines[0])
        self.assertIn("cpu 85% of the last 60s", self.room.stalls[0]["text"])

    def test_the_switch_off_reads_nothing_and_says_so(self):  # noqa: VACUOUS_ASSERTION — the not-read line is asserted, and the same tick over a fixture tree posts in every other arm
        """The suite's own HELM_SEAT_PRESSURE=off over the HOST's tree: no
        reading, rc 0, and a line that says it was not read."""
        self.assertEqual(os.environ.get(seatceiling.SWITCH), "off")
        rc, lines, data = pressurewatch.tick(post=self.room, phone=self.phone)
        self.assertEqual((rc, data), (0, {"read": False}))
        self.assertIn("not read", lines[0])
        self.assertEqual(self.room.calls, [])


class ChatUnreachableTest(_Base):
    """An alarm about the fleet must not depend on the fleet's chat."""

    def test_the_phone_and_the_log_carry_it_and_the_row_is_retried(self):
        tree = Tree(self)
        self.room.fail = 2
        got = self.stalled(tree, 3)
        rc, lines = got[3]
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.phone.pushes), 1,
                         "the push waited on the chat row")
        self.assertIn("row NOT delivered (OSError: the chat node is "
                      "unreachable); retried every pass, and the alarm "
                      "stands in %s and this unit's journal"
                      % pressurewatch.alarm_log_path(), lines[0])
        self.assert_logged(self.room.calls[0]["text"])
        self.assertIn(self.room.calls[0]["text"], lines)
        for i in (4, 5, 6):
            tree.advance(85.0, 40.0)
            rc, lines, _ = self.tick(tree, T0 + 60 * i)
        self.assertEqual(len(self.room.calls), 3, "retried until it landed")
        self.assertEqual({c["event"] for c in self.room.calls},
                         {self.room.calls[0]["event"]},
                         "a retry carried a new event id")
        self.assertIn("row posted", lines[0])
        self.assertEqual(len(self.phone.pushes), 1)

    def test_with_no_phone_the_row_says_where_it_lands(self):  # noqa: VACUOUS_ASSERTION — the row text naming the alarm log and the retried row's status line are asserted
        tree = Tree(self)
        self.phone = Phone(on=False)
        self.room.fail = 1
        self.stalled(tree, 3)
        self.assertEqual(self.phone.pushes, [])
        text = self.room.calls[0]["text"]
        self.assertIn("No phone is configured (HELM_NTFY_TOPIC or Telegram), "
                      "so this row and %s are the only places this alarm "
                      "lands" % pressurewatch.alarm_log_path(), text)
        self.assert_logged(text)
        tree.advance(85.0, 40.0)
        rc, lines, _ = self.tick(tree, T0 + 240)
        self.assertIn("row posted; phone unconfigured", lines[0])

    def test_a_failed_push_stays_owed(self):
        tree = Tree(self)
        self.phone = Phone(ok=False)
        self.stalled(tree, 3)
        tree.advance(85.0, 40.0)
        self.tick(tree, T0 + 240)
        self.assertEqual(len(self.phone.pushes), 2)
        self.phone.ok = True
        tree.advance(85.0, 40.0)
        self.tick(tree, T0 + 300)
        tree.advance(85.0, 40.0)
        rc, lines, _ = self.tick(tree, T0 + 360)
        self.assertEqual(len(self.phone.pushes), 3)
        self.assertIn("phone pushed", lines[0])

    def test_a_pass_killed_inside_the_chat_post_pushes_once(self):
        """The push lands, then the chat post hangs until the unit's
        TimeoutStartSec kills the pass. The push must already be durable:
        left owed on disk, every later pass pushes it again, once a minute
        for as long as the post keeps hanging."""
        tree = Tree(self)
        self.room = KilledRoom(kills=1)
        self.stalled(tree, 2)
        tree.advance(85.0, 40.0)
        with self.assertRaises(_Killed):
            self.tick(tree, T0 + 180)
        self.assertEqual(len(self.phone.pushes), 1,
                         "control: the push landed before the kill")
        self.assertEqual(len(self.room.stalls), 1,
                         "control: the kill landed inside the stall row")
        tree.advance(85.0, 40.0)
        rc, lines, _ = self.tick(tree, T0 + 240)
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.phone.pushes), 1,
                         "the killed pass left its push owed, and the next "
                         "pass pushed it again")
        self.assertEqual(len(self.room.stalls), 2, "the row is retried")
        self.assertEqual(self.room.stalls[0]["event"],
                         self.room.stalls[1]["event"],
                         "a retry carried a new event id")
        self.assertIn("row posted; phone pushed", lines[0])

    def test_a_push_that_lands_on_a_retry_pass_is_saved_before_its_row(self):
        """The first push fails, so a LATER pass pushes. That pass opens no
        episode and reads no process, so nothing else saves between its push
        and its row: when the push lands there and the post hangs until the
        unit's kill, only the push's own save keeps the next pass from
        pushing again. (On the pass that opens the episode, the save after
        the process read covers the same gap, so that arm alone cannot tell.)"""
        tree = Tree(self)
        self.room = KilledRoom(kills=2)
        self.phone.ok = False
        self.stalled(tree, 2)
        tree.advance(85.0, 40.0)
        with self.assertRaises(_Killed):
            self.tick(tree, T0 + 180)
        self.assertEqual(len(self.phone.pushes), 1,
                         "control: the opening pass tried the push")
        self.phone.ok = True
        tree.advance(85.0, 40.0)
        with self.assertRaises(_Killed):
            self.tick(tree, T0 + 240)
        self.assertEqual(len(self.phone.pushes), 2,
                         "control: the retried push landed before the kill")
        tree.advance(85.0, 40.0)
        rc, lines, _ = self.tick(tree, T0 + 300)
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.phone.pushes), 2,
                         "the killed retry pass left its landed push owed, "
                         "and the next pass pushed it again")
        self.assertEqual(len(self.room.stalls), 3, "the row is retried")
        self.assertEqual(len({c["event"] for c in self.room.stalls}), 1,
                         "a retry carried a new event id")
        self.assertIn("row posted; phone pushed", lines[0])

    def test_a_row_still_owed_at_close_posts_late_marked_closed(self):  # noqa: VACUOUS_ASSERTION — the late row's CLOSED head, its whole body and its event id are asserted on room.calls before the count that says it posted once
        """The chat is unreachable for the whole episode, so its row is
        still owed when the fleet reads clear for CLEAR_S. The row is kept
        and posts once the chat is back, marked closed, under its own event
        id; it is never dropped to the alarm log alone."""
        tree = Tree(self)
        self.room.fail = 10 ** 6
        self.stalled(tree, 3)
        for i in (4, 5):
            tree.advance(0.0, 0.0)
            rc, lines, _ = self.tick(tree, T0 + 60 * i)
        self.assertTrue(any("is CLEAR" in line for line in lines), lines)
        owed = self.room.calls[0]
        self.assertIn("FLEET STALL", owed["text"], "control: the row was owed")
        self.room.fail = 0
        tree.advance(0.0, 0.0)
        self.tick(tree, T0 + 360)
        late = self.room.calls[-1]
        self.assertTrue(late["text"].startswith(
            "[pressure-watch] CLOSED, delivered late: "), late["text"])
        self.assertIn(owed["text"], late["text"])
        self.assertEqual(late["event"], owed["event"],
                         "the late row carried a new event id")
        posted = len(self.room.calls)
        tree.advance(0.0, 0.0)
        self.tick(tree, T0 + 420)
        self.assertEqual(len(self.room.calls), posted,
                         "a late row posted twice")
        self.assertEqual(len(self.phone.pushes), 1)

    def test_at_most_late_max_closed_rows_wait_and_the_dropped_one_is_logged(self):
        """LATE_MAX + 1 stalls open and close while the chat is unreachable.
        Only the LATE_MAX newest rows wait; the oldest is dropped, with one
        line in the alarm log naming when its stall began, and it never
        posts once the chat is back."""
        tree = Tree(self)
        self.room.fail = 10 ** 6
        now = T0
        self.tick(tree, now)
        events, closed = [], []
        for _ in range(pressurewatch.LATE_MAX + 1):
            for _hot in range(3):
                now += 60
                tree.advance(85.0, 40.0)
                _rc, _lines, data = self.tick(tree, now)
            events.append(data["state"]["episode"]["event"])
            for _clear in range(2):
                now += 60
                tree.advance(0.0, 0.0)
                _rc, lines, _ = self.tick(tree, now)
            closed.extend(line for line in lines if "is CLEAR" in line)
        self.assertEqual(len(closed), pressurewatch.LATE_MAX + 1,
                         "control: every stall closed")
        self.assertEqual(len(set(events)), pressurewatch.LATE_MAX + 1,
                         "control: every stall opened its own episode")
        with open(pressurewatch.alarm_log_path(), encoding="utf-8") as fh:
            logged = fh.read()
        self.assertEqual(logged.count("late row DROPPED"), 1, logged)
        self.assertIn("late row DROPPED for the stall that began %s"
                      % pressurewatch._hhmm(T0), logged)
        self.room.fail = 0
        before = len(self.room.calls)
        now += 60
        tree.advance(0.0, 0.0)
        self.tick(tree, now)
        posted = self.room.calls[before:]
        self.assertEqual([c["event"] for c in posted], events[1:])
        self.assertTrue(all(c["text"].startswith(
            "[pressure-watch] CLOSED, delivered late: ") for c in posted),
            posted)

    def test_the_default_seam_is_chat_post_as_the_watcher(self):
        tree = Tree(self)
        with mock.patch("helm.chat.post", return_value={"id": "r1"}) as post:
            pressurewatch.tick(now=T0, root=tree.cg, proc=tree.proc,
                               phone=self.phone, sleep=tree.sleep,
                               owner="owner-handle")
            for i in (1, 2, 3):
                tree.advance(85.0, 40.0)
                pressurewatch.tick(now=T0 + 60 * i, root=tree.cg,
                                   proc=tree.proc, phone=self.phone,
                                   sleep=tree.sleep, owner="owner-handle")
        self.assertEqual(post.call_count, 1)
        kw = post.call_args.kwargs
        self.assertEqual(kw["who"], pressurewatch.POSTER)
        self.assertEqual(kw["room"], "main")
        self.assertFalse(kw["sign"])
        self.assertTrue(kw["event_id"].startswith("pressure-watch-"))
        from helm import machine_senders
        self.assertTrue(machine_senders.is_machine(pressurewatch.POSTER))


class DetailAfterTheAlarmTest(_Base):
    """The alarm is latched, logged and pushed on the slice-level facts
    (cgroup files only) BEFORE any process is read. A stalled process's
    cmdline or environ read can block its reader; when the pass read them
    first, a pass that blocked there until the unit's TimeoutStartSec kill
    recorded nothing, on every pass of the stall."""

    #: How long the stub read hangs before the unit's kill lands in it.
    HANG_S = 2.0

    def test_a_hung_process_read_still_latches_logs_and_pushes_once(self):  # noqa: ORPHANED_MOCK — consumers is reached through the tick helper, from process_detail's worker thread; calls is asserted to hold exactly one read
        tree = self.consumers_tree()
        release = threading.Event()
        self.addCleanup(release.set)
        calls = []

        def hang(*args, **kwargs):
            calls.append(args)
            if not release.wait(self.HANG_S):
                raise _Killed()        # the unit's kill, inside the read
            return None

        budget = mock.patch.object(pressurewatch, "DETAIL_BUDGET_S", 0.2,
                                   create=True)
        with mock.patch.object(pressurewatch, "consumers", hang), budget:
            self.stalled(tree, 2)
            for i in (3, 4):
                tree.advance(85.0, 40.0)
                try:
                    self.tick(tree, T0 + 60 * i)
                except _Killed:
                    pass               # a pass killed inside the hung read
        self.assertEqual(len(self.phone.pushes), 1,
                         "the push waited on the process read")
        self.assertIn("Biggest slice: agents-seat_a.slice 9.50G of 12.00G "
                      "high", self.phone.pushes[0][0])
        with open(pressurewatch.alarm_log_path(), encoding="utf-8") as fh:
            logged = fh.read()
        self.assertEqual(logged.count("FLEET STALL"), 1, logged)
        self.assertEqual(logged.count("Process detail UNKNOWN"), 1, logged)
        with open(pressurewatch.state_path(), encoding="utf-8") as fh:
            latch = json.load(fh)["episode"]
        self.assertEqual(latch["phone"], "pushed")
        self.assertEqual(len(calls), 1, "the process read ran again")
        self.assertEqual(len(self.room.stalls), 1)
        row = self.room.stalls[0]
        self.assertEqual(row["event"], latch["event"])
        self.assertIn("Slices by memory: agents-seat_a.slice 9.50G of 12.00G "
                      "high (79%)", row["text"])
        self.assertIn("Process detail UNKNOWN: the process read did not "
                      "finish within 0.2s", row["text"])
        self.assert_logged(row["text"])

    def test_a_pass_killed_before_the_process_read_owes_a_true_row(self):
        """Killed after the push and before the process read: the row a
        later pass posts says the detail is UNKNOWN and why, never a row
        that looks whole."""
        tree = self.consumers_tree()

        def killed(*args, **kwargs):
            raise _Killed()

        with mock.patch.object(pressurewatch, "process_detail", killed,
                               create=True):
            self.stalled(tree, 2)
            tree.advance(85.0, 40.0)
            with self.assertRaises(_Killed):
                self.tick(tree, T0 + 180)
        self.assertEqual(len(self.phone.pushes), 1,
                         "control: the push landed before the kill")
        self.assertEqual(self.room.stalls, [],
                         "control: the kill landed before the row")
        tree.advance(85.0, 40.0)
        rc, lines, _ = self.tick(tree, T0 + 240)
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.phone.pushes), 1)
        self.assertEqual(len(self.room.stalls), 1)
        self.assertIn("Process detail UNKNOWN: the pass that opened this "
                      "alarm ended before its process read finished",
                      self.room.stalls[0]["text"])


class ProgramOnlyTest(_Base):
    """A process is named by its PROGRAM only: the basename of argv[0], or
    of the script an interpreter runs. Any other word of a command line can
    carry a secret (`redis-cli AUTH <pw>`, `x.py <token>`), and the row goes
    to a room, the push to a phone, the log and the journal to disk."""

    def test_a_planted_argument_reaches_no_output(self):
        tree = Tree(self)
        tree.seat("seat-a", 4101, current=int(9.5 * GB), high=12 * GB,
                  rss_gb=4.5, cmdline=["redis-cli", "AUTH", MARK],
                  ticks=(0, 50), env_name="seat-a")
        tree.seat("seat-b", 4202, current=int(6.25 * GB), high=16 * GB,
                  rss_gb=1.25, cmdline=["/usr/bin/python3", "./x.py", MARK],
                  ticks=(0, 290), env_name="seat-b")
        tree.seat("seat-c", 4303, current=1 * GB, high=12 * GB, rss_gb=0.5,
                  cmdline=["node", "/srv/app/cli.js", "serve", MARK],
                  ticks=(0, 20), env_name="seat-c")
        got = self.stalled(tree, 3)
        tree.advance(85.0, 40.0)
        rc, lines, data = self.tick(tree, T0 + 240)
        row = self.room.stalls[0]["text"]
        with open(pressurewatch.alarm_log_path(), encoding="utf-8") as fh:
            logged = fh.read()
        with open(pressurewatch.state_path(), encoding="utf-8") as fh:
            saved = fh.read()
        seen = {"row": row, "push": self.phone.pushes[0][0], "log": logged,
                "state": saved, "json": json.dumps(data, default=str),
                "pass output": "\n".join(
                    [line for _rc, out in got for line in out] + lines)}
        for where in sorted(seen):
            self.assertNotIn(MARK, seen[where], where)
        self.assertIn("4.50G pid 4101 redis-cli, seat-a", row)
        self.assertIn("1.25G pid 4202 x.py, seat-b", row)
        self.assertIn("pid 4303 cli.js, seat-c", row)
        self.assertIn(row, seen["pass output"])

    def test_each_command_line_is_named_by_its_program(self):  # noqa: VACUOUS_ASSERTION — a fixed, non-empty literal table; every case asserts equality with a non-empty program name
        proc = tempfile.mkdtemp(prefix="helm-test-pressurewatch-argv-")
        self.addCleanup(shutil.rmtree, proc, True)
        cases = (
            (["redis-cli", "AUTH", MARK], "redis-cli"),
            (["/usr/bin/python3", "./x.py", MARK], "x.py"),
            (["python3", "-u", "/srv/tools/x.py", "--token", MARK], "x.py"),
            (["node", "--max-old-space-size=4096", "/srv/app/cli.js", MARK],
             "cli.js"),
            # A flag that takes a value, or runs code: nothing after it is
            # known to be the script, so the interpreter names itself.
            (["python3", "-W", MARK, "x.py"], "python3"),
            (["python3", "-m", MARK], "python3"),
            (["bash", "-c", "exec x " + MARK], "bash"),
            # An option given WITH `=` can still make what follows code or
            # an argument: node's and bun's --eval= and --print=, node's
            # --run=. The next word is never the script.
            (["node", "--eval=console.log(process.argv[1])", MARK], "node"),
            (["node", "--print=1", MARK], "node"),
            (["node", "--run=build", MARK], "node"),
            (["bun", "--eval=x", MARK], "bun"),
            # A bare `--` ends the options: the word after it is the script.
            # bin/helm-hook runs every hook as `<python> -S -- <bin/helm>`.
            (["/opt/py/bin/python3.14", "-S", "--", "/srv/h/bin/helm",
              "hook", MARK], "helm"),
            (["bash", "--", "/srv/x/run.sh", MARK], "run.sh"),
            (["python3", "--"], "python3"),
            (["/home/u/.local/share/claude/versions/2.1.285", "--resume",
              MARK], "claude"),
            # A title rewritten into argv[0]: its first word is the program.
            (["sshd: u@pts/" + MARK], "sshd:"),
            (["/usr/bin/tool --key=a/" + MARK], "tool"),
            ([], "kthreadd"),
        )
        for i, (argv, want) in enumerate(cases):
            pid = 100 + i
            _w(os.path.join(proc, str(pid), "cmdline"),
               "".join(a + "\0" for a in argv))
            _w(os.path.join(proc, str(pid), "comm"), "kthreadd\n")
            with self.subTest(argv=argv):
                self.assertEqual(pressurewatch._cmd(proc, pid), want)


class HelmIsTheLoadTest(_Base):
    """task/3841: the stall alarm says whether HELM ITSELF is the load, and
    when it is, files or refreshes ONE P-1 row to the integrator. The
    measured stalls were mostly SHORT-LIVED hook pythons (~15 births/s),
    which a before/after sample of living pids never sees, so the window
    also reads agents.slice's cpu.stat and the host's process count. Every
    row here lands in this module's temp HELM_HOME, never the real ledger."""

    def setUp(self):
        _Base.setUp(self)
        self.clear_ledger()
        self.addCleanup(self.clear_ledger)
        seat = mock.patch.object(seats_integrator, "integrator_seat",
                                 return_value=("seat-integrator", None))
        seat.start()
        self.addCleanup(seat.stop)
        self.root, self.rooms = pressurewatch.helm_checkouts()

    def clear_ledger(self):
        for path in glob.glob(tasks.ledger_path() + "*"):
            if os.path.isdir(path):
                shutil.rmtree(path, True)
            else:
                os.remove(path)

    def helm(self, *rest):
        """This checkout's bin/helm under an interpreter, as a seat runs it."""
        return ["/usr/bin/python3", os.path.join(self.root, "bin", "helm")] \
            + list(rest)

    def p1_rows(self):
        return [r for r in tasks.rows().values()
                if r.get("title") == pressurewatch.P1_TITLE]

    def helm_tree(self):
        """helm's own web process at 200% of one core beside a 50% node:
        helm is 200 of the slice's 250."""
        tree = Tree(self)
        tree.seat("seat-a", 5101, current=int(9.5 * GB), high=12 * GB,
                  rss_gb=2.0, cmdline=self.helm("web", "--token", MARK),
                  ticks=(0, 200), env_name="seat-a")
        tree.seat("seat-b", 5202, current=2 * GB, high=12 * GB, rss_gb=1.0,
                  cmdline=["node", "/srv/app/cli.js", MARK], ticks=(0, 50),
                  env_name="seat-b")
        return tree

    def restall(self, tree, start, at):
        """Two clear minutes close the open episode; a new stall then holds
        until its alarm opens at start + `at`."""
        for t in (240, 300):
            tree.advance(0.0, 0.0)
            self.tick(tree, start + t)
        for t in (at - 120, at - 60, at):
            tree.advance(85.0, 40.0)
            self.tick(tree, start + t)
        self.assertEqual(len(self.room.stalls), 2,
                         "control: a second alarm opened")
        return self.room.stalls[1]["text"]

    def test_a_helm_own_consumer_over_half_names_helm_and_files_one_row(self):  # noqa: VACUOUS_ASSERTION — the HELM line, the cpu line and the filed row are asserted positively before the planted marker's absence
        tree = self.helm_tree()
        got = self.stalled(tree, 3)
        self.assertEqual(got[3][0], 1)
        text = self.room.stalls[0]["text"]
        rows = self.p1_rows()
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        self.assertIn("Processes by cpu: 200% cpu pid 5101 helm, seat-a; 50% "
                      "cpu pid 5202 cli.js, seat-b; short-lived 0% cpu of "
                      "agents.slice's 250% over 1s (its cpu.stat usage less "
                      "every process sampled at both ends), 0 births/s "
                      "host-wide.", text)
        self.assertIn("HELM IS THE LOAD (P-1: fixed ahead of everything): "
                      "helm's own processes used 200% of agents.slice's 250% "
                      "cpu over 1s: 200% cpu pid 5101 helm, seat-a; "
                      "short-lived 0% cpu (0 of 0 births caught alive were "
                      "helm's); 0 births/s host-wide. P-1 row " + row["id"]
                      + " filed to seat-integrator.", text)
        self.assertEqual(text.count("HELM IS THE LOAD"), 1)
        self.assertEqual(
            (row["status"], row["priority"], row["origin"], row["owner"],
             row["project"], row["source"]),
            ("open", "P0", "owner", "seat-integrator", tasks.OWN_PROJECT,
             pressurewatch.POSTER))
        self.assertIn(pressurewatch.P1_RULE, row["note"])
        self.assertIn("used 200% of agents.slice's 250% cpu over 1s",
                      row["note"])
        self.assertIn("200% cpu pid 5101 helm, seat-a", row["note"])
        for where, seen in (("row", text), ("task", json.dumps(row))):
            self.assertNotIn(MARK, seen, where)
        self.assert_logged(text)

    def test_short_lived_births_are_counted_and_name_helm(self):
        """The load is births no pid sample sees: 200% of one core spent by
        processes born inside the window, 15 births/s. Three of the four
        caught alive are helm's (bin/helm, bin/helm-hook by a path relative
        to its cwd, python -m helm), and they spent 60 of the 62 ticks."""
        tree = Tree(self)
        tree.seat("seat-a", 5101, current=4 * GB, high=12 * GB, rss_gb=1.0,
                  cmdline=["/opt/claude/versions/2.1.285"], ticks=(0, 5),
                  env_name="seat-a")
        tree.short_usec, tree.window_forks = 2 * 10 ** 6, 15
        tree.born("seat-a", 6001, self.helm("hook", "stop-guard"), 20)
        tree.born("seat-a", 6002, ["/bin/sh", "bin/helm-hook", "gate", "x"],
                  20, cwd=self.root)
        tree.born("seat-a", 6003, ["python3", "-m", "helm", "hook"], 20)
        tree.born("seat-a", 6004, ["git", "status"], 2)
        self.stalled(tree, 3)
        text = self.room.stalls[0]["text"]
        rows = self.p1_rows()
        self.assertEqual(len(rows), 1, rows)
        self.assertIn("Processes by cpu: 5% cpu pid 5101 claude, seat-a; "
                      "short-lived 200% cpu of agents.slice's 205% over 1s "
                      "(its cpu.stat usage less every process sampled at "
                      "both ends), 15 births/s host-wide.", text)
        self.assertIn("HELM IS THE LOAD (P-1: fixed ahead of everything): "
                      "helm's own processes used 194% of agents.slice's 205% "
                      "cpu over 1s: short-lived 194% cpu (3 of 4 births "
                      "caught alive were helm's); 15 births/s host-wide. P-1 "
                      "row " + rows[0]["id"] + " filed to seat-integrator.",
                      text)

    def test_another_programs_load_says_nothing_and_files_nothing(self):  # noqa: VACUOUS_ASSERTION — the measured short-lived clause of the same row is asserted before the absences
        tree = self.consumers_tree()
        self.stalled(tree, 3)
        text = self.room.stalls[0]["text"]
        self.assertIn("short-lived 0% cpu of agents.slice's 321% over 1s",
                      text, "control: the window was measured")
        self.assertNotIn("HELM IS THE LOAD", text)
        self.assertNotIn("Helm's own cpu share UNKNOWN", text)
        self.assertEqual(tasks.rows(), {})

    def test_a_second_alarm_within_30_minutes_files_no_second_row(self):
        start = time.time()
        tree = self.helm_tree()
        self.stalled(tree, 3, start=start)
        first = self.p1_rows()
        self.assertEqual(len(first), 1, "control: the first alarm filed")
        text = self.restall(tree, start, 480)
        rows = self.p1_rows()
        self.assertEqual([r["id"] for r in rows], [first[0]["id"]])
        self.assertEqual(rows[0].get("comments") or [], [])
        self.assertIn("HELM IS THE LOAD", text)
        self.assertIn("P-1 row %s was filed or refreshed " % rows[0]["id"],
                      text)
        self.assertIn("it is written at most once per 30m.", text)

    def test_a_second_alarm_later_comments_once_on_the_same_row(self):
        start = time.time()
        tree = self.helm_tree()
        self.stalled(tree, 3, start=start)
        text = self.restall(tree, start, 40 * 60)
        rows = self.p1_rows()
        self.assertEqual(len(rows), 1, rows)
        notes = [c for c in rows[0].get("comments") or []
                 if c.get("by") == pressurewatch.POSTER]
        self.assertEqual(len(notes), 1, rows[0].get("comments"))
        self.assertTrue(notes[0]["text"].startswith(
            "[pressure-watch] helm is still the load at "), notes[0]["text"])
        self.assertIn("used 200% of agents.slice's 250% cpu over 1s",
                      notes[0]["text"])
        self.assertIn("P-1 row %s refreshed with a comment." % rows[0]["id"],
                      text)

    def test_an_unwritable_ledger_is_named_and_nothing_raises(self):  # noqa: VACUOUS_ASSERTION — the NOT filed sentence of the same row is asserted before the empty ledger
        os.makedirs(tasks.ledger_path() + ".lock")
        tree = self.helm_tree()
        got = self.stalled(tree, 3)
        self.assertEqual(got[3][0], 1)
        text = self.room.stalls[0]["text"]
        self.assertIn("HELM IS THE LOAD (P-1: fixed ahead of everything): ",
                      text)
        self.assertIn("P-1 row NOT filed: task ledger is not writable", text)
        self.assertEqual(self.p1_rows(), [])
        self.assert_logged(text)

    def test_a_stall_on_memory_alone_files_no_p1(self):  # noqa: VACUOUS_ASSERTION — the alarm and its measured cpu line are asserted positively before the absences
        """The share is of cpu. In a stall on memory alone, helm's processes
        can be most of a small cpu total while another process holds the
        memory, so no HELM line and no row."""
        tree = self.helm_tree()
        got = self.stalled(tree, 3, cpu=0.0, memory=85.0)
        self.assertEqual(got[3][0], 1, "control: the memory stall opened")
        text = self.room.stalls[0]["text"]
        self.assertIn("short-lived 0% cpu of agents.slice's 250% over 1s",
                      text, "control: the window was measured")
        self.assertNotIn("HELM IS THE LOAD", text)
        self.assertEqual(self.p1_rows(), [])

    def test_hook_pythons_in_the_wrappers_shape_are_helms_load(self):
        """bin/helm-hook starts each hook as `<python> -S -- <bin/helm>`:
        the births caught alive in that shape are helm's."""
        tree = Tree(self)
        tree.seat("seat-a", 5101, current=4 * GB, high=12 * GB, rss_gb=1.0,
                  cmdline=["/opt/claude/versions/2.1.285"], ticks=(0, 5),
                  env_name="seat-a")
        tree.short_usec, tree.window_forks = 2 * 10 ** 6, 15
        hook = ["/opt/py/bin/python3.14", "-S", "--",
                os.path.join(self.root, "bin", "helm"), "hook", "stop-guard"]
        tree.born("seat-a", 6001, hook, 20)
        tree.born("seat-a", 6002, hook, 20)
        self.stalled(tree, 3)
        text = self.room.stalls[0]["text"]
        self.assertIn("short-lived 200% cpu (2 of 2 births caught alive "
                      "were helm's)", text)
        self.assertEqual(len(self.p1_rows()), 1)

    def test_a_retitled_row_is_still_the_one_row(self):
        start = time.time()
        tree = self.helm_tree()
        self.stalled(tree, 3, start=start)
        first = self.p1_rows()
        self.assertEqual(len(first), 1, "control: the first alarm filed")
        tid = first[0]["id"]
        _row, err = tasks.update(tid, title="P-1: the hook storm (retitled)")
        self.assertIsNone(err)
        text = self.restall(tree, start, 40 * 60)
        mine = [r for r in tasks.rows().values()
                if r.get("source") == pressurewatch.POSTER]
        self.assertEqual([r["id"] for r in mine], [tid])
        self.assertIn("P-1 row %s refreshed with a comment." % tid, text)

    def test_a_row_closed_inside_the_30_minutes_is_said_closed(self):
        start = time.time()
        tree = self.helm_tree()
        self.stalled(tree, 3, start=start)
        tid = self.p1_rows()[0]["id"]
        _row, err = tasks.update(tid, status="closed", closed_reason="fixed")
        self.assertIsNone(err)
        text = self.restall(tree, start, 480)
        self.assertEqual([r["id"] for r in self.p1_rows()], [tid])
        self.assertIn("P-1 row %s was filed or refreshed " % tid, text)
        self.assertIn("and is now closed, so no P-1 row is open; it is "
                      "written at most once per 30m.", text)

    def test_a_read_only_pass_files_nothing_and_says_so(self):
        tree = self.helm_tree()
        self.stalled(tree, 2)
        tree.advance(85.0, 40.0)
        rc, lines, _data = self.tick(tree, T0 + 180, apply=False)
        self.assertEqual(rc, 1, "control: the read-only pass opened")
        out = "\n".join(lines)
        self.assertIn("HELM IS THE LOAD", out)
        self.assertIn(pressurewatch.P1_READ_ONLY + ".", out)
        self.assertEqual(tasks.rows(), {})
        self.assertEqual(self.room.calls, [])

    def test_an_unreadable_slice_cpu_is_named_not_guessed(self):  # noqa: VACUOUS_ASSERTION — the UNKNOWN line of the same row is asserted before the absences
        tree = self.helm_tree()
        os.remove(os.path.join(tree.fleet, "cpu.stat"))
        tree.cpu_stat = False
        self.stalled(tree, 3)
        text = self.room.stalls[0]["text"]
        self.assertIn("Helm's own cpu share UNKNOWN: agents.slice's cpu.stat "
                      "usage_usec would not read at %s." % tree.fleet, text)
        self.assertNotIn("HELM IS THE LOAD", text)
        self.assertEqual(self.p1_rows(), [])

    def test_helm_own_is_read_from_the_command_line(self):  # noqa: VACUOUS_ASSERTION — a fixed, non-empty literal table; every case asserts identity with a literal True or False
        """HELM-OWN by command line: this checkout's bin/helm and
        bin/helm-hook (through a link, or relative to the process's cwd),
        python -m helm, helm/*.py, the chat node, and a fab client whose cwd
        is a helm checkout. The checkout comes from the module's own
        location; its lane rooms follow work.lane_path."""
        root, rooms = self.root, self.rooms
        links = tempfile.mkdtemp(prefix="helm-test-pressurewatch-bin-")
        self.addCleanup(shutil.rmtree, links, True)
        link = os.path.join(links, "helm")
        os.symlink(os.path.join(root, "bin", "helm"), link)
        fab = "/opt/tools/bin/fab"
        cases = (
            (self.helm("hook", "stop-guard"), None, True),
            (["python3", "-u", link, "chat", "wait"], None, True),
            (["/bin/sh", os.path.join(root, "bin", "helm-hook"), "gate"],
             None, True),
            (["/bin/sh", "bin/helm-hook", "gate"], root, True),
            (["python3", "helm/gateslice.py", "--modules", "x"], root, True),
            (["python3", "-m", "helm", "gate"], None, True),
            (["python3", "-mhelm.cli"], None, True),
            (["python3", os.path.join(rooms, "a-lane", "bin", "helm")], None,
             True),
            (["/home/u/.local/bin/dregg-node-rebased", "run"], None, True),
            (["dregg-cave-node"], None, True),
            (["bash", fab, "test", "--repo", "."], root, True),
            ([fab, "gate", "observe"], os.path.join(rooms, "a-lane"), True),
            (["bash", fab, "test", "--repo", "."], links, False),
            (["python3", "bin/helm"], None, False),
            (["python3", "/srv/other/bin/helm"], None, False),
            (["python3", "-m", "helmet"], None, False),
            (["python3", "-c", "import helm"], None, False),
            # THE HOOK PYTHON'S OWN SHAPE on a fleet host (bin/helm-hook
            # once it has an interpreter recorded), the stall's measured
            # short-lived load.
            (["/opt/py/bin/python3.14", "-S", "--",
              os.path.join(root, "bin", "helm"), "hook", "stop-guard"], None,
             True),
            (["/opt/py/bin/python3.14", "-S", "--", "/srv/other/bin/helm"],
             None, False),
            (["node", "/srv/app/cli.js"], root, False),
            (["git", "status"], root, False),
            ([], root, False),
        )
        for argv, cwd, want in cases:
            with self.subTest(argv=argv, cwd=cwd):
                self.assertIs(pressurewatch._helm_own(argv, cwd), want)
        from helm import work
        self.assertEqual(rooms, os.path.dirname(work.lane_path(root, "x")))


class _FleetDir(object):
    def fleet_dir(self, cpu="62.53", memory="41.00"):
        d = tempfile.mkdtemp(prefix="helm-test-stall-clause-")
        self.addCleanup(shutil.rmtree, d, True)
        if cpu is not None:
            _w(os.path.join(d, "cpu.pressure"),
               "some avg10=%s avg60=1.00 avg300=1.00 total=9\n"
               "full avg10=0.00 avg60=0.00 avg300=0.00 total=1\n" % cpu)
        if memory is not None:
            _w(os.path.join(d, "memory.pressure"),
               "some avg10=%s avg60=1.00 avg300=1.00 total=9\n" % memory)
        return d


class StallClauseTest(_FleetDir, _Base):
    """The Python half of the TIMED OUT clause."""

    def test_a_readable_fleet_is_carried(self):
        os.environ[seatceiling.FLEET_ENV] = self.fleet_dir()
        self.assertEqual(seatceiling.stall_clause(),
                         "; fleet stall (agents.slice PSI some avg10): "
                         "cpu 62.53%, memory 41.00%")

    def test_an_unreadable_fleet_says_unknown(self):
        os.environ[seatceiling.FLEET_ENV] = self.fleet_dir(memory=None)
        self.assertEqual(seatceiling.stall_clause(),
                         "; fleet stall UNKNOWN: agents.slice pressure would "
                         "not read")

    def test_the_slice_is_found_from_the_process_own_cgroup_line(self):
        tree = Tree(self)
        tree.write(7.5, 0.25)
        _w(os.path.join(tree.proc, "self", "cgroup"),
           "0::/%s/agents-seat.slice/run-p1.scope\n" % AGENTS)
        self.assertEqual(
            seatceiling.stall_clause(root=tree.cg, proc=tree.proc),
            "; fleet stall (agents.slice PSI some avg10): cpu 7.50%, "
            "memory 0.25%")
        _w(os.path.join(tree.proc, "self", "cgroup"), "0::/init.scope\n")
        self.assertEqual(
            seatceiling.stall_clause(root=tree.cg, proc=tree.proc),
            "; fleet stall UNKNOWN: " + seatceiling.NO_FLEET_PATH)

    def test_the_switch_off_says_nothing_about_the_host(self):  # noqa: VACUOUS_ASSERTION — the same reader over HELM_FLEET_CGROUP is asserted to carry a reading in the same arm
        """Nothing named, the host's tree, HELM_SEAT_PRESSURE off: no read
        and no clause (the suite's audit hook would refuse a read)."""
        self.assertEqual(seatceiling.stall_clause(), "")
        os.environ[seatceiling.FLEET_ENV] = self.fleet_dir()
        self.assertIn("cpu 62.53%", seatceiling.stall_clause(),
                      "control: the same reader over a named tree reads")

    def test_a_handler_timeout_line_carries_it(self):  # noqa: VACUOUS_ASSERTION — the whole stderr line and the outcome's why are asserted equal to non-empty literals
        os.environ[seatceiling.FLEET_ENV] = self.fleet_dir()
        from helm import cli
        real = cli.main

        def hang(argv):
            time.sleep(30)
            return 0
        cli.main = hang
        self.addCleanup(setattr, cli, "main", real)
        outcome, err = {}, io.StringIO()
        spec = {"name": "slowguard", "event": "E", "args": "slowguard",
                "timeout": 0.2, "matcher": None, "gate": True}
        with contextlib.redirect_stderr(err):
            rc = hookrun.run_one(spec, "{}", outcome=outcome)
        clause = ("; fleet stall (agents.slice PSI some avg10): cpu 62.53%, "
                  "memory 41.00%")
        self.assertEqual(rc, 0)
        self.assertEqual(err.getvalue(),
                         "[helm slowguard] TIMED OUT at 0.2s — this event is "
                         "UNCHECKED%s\n" % clause)
        self.assertEqual(outcome["why"],
                         "handler timed out after 0.2s%s" % clause)

    def test_a_handler_timeout_with_no_reading_says_unknown(self):
        os.environ[seatceiling.FLEET_ENV] = self.fleet_dir(cpu=None)
        from helm import cli
        real = cli.main

        def hang(argv):
            time.sleep(30)
            return 0
        cli.main = hang
        self.addCleanup(setattr, cli, "main", real)
        outcome, err = {}, io.StringIO()
        spec = {"name": "slowguard", "event": "E", "args": "slowguard",
                "timeout": 0.2, "matcher": None}
        with contextlib.redirect_stderr(err):
            hookrun.run_one(spec, "{}", outcome=outcome)
        self.assertIn("UNCHECKED; fleet stall UNKNOWN: agents.slice pressure "
                      "would not read", err.getvalue())


class ShellClauseTest(_FleetDir, _Base):
    """The shell half in bin/helm-hook: the SAME sentence, from builtins,
    on a real `timeout` kill of the wrapped child."""

    def run_wrapper(self, fleet=None):
        d = tempfile.mkdtemp(prefix="helm-test-stall-shell-")
        self.addCleanup(shutil.rmtree, d, True)
        child = os.path.join(d, "helm")
        _w(child, "#!/bin/sh\nsleep 30\n")
        os.chmod(child, 0o755)
        wrapper = os.path.join(d, "helm-hook")
        shutil.copy2(WRAPPER, wrapper)
        env = dict(os.environ, HELM_HOOK_ALARM_DIR=os.path.join(d, "alarm"))
        env.pop(seatceiling.FLEET_ENV, None)
        if fleet is not None:
            env[seatceiling.FLEET_ENV] = fleet
        cmd = "%s gate probe Stop 0.5 stop %s" % (shlex.quote(wrapper),
                                                  shlex.quote(child))
        p = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True,
                           env=env, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p

    def assert_same(self, fleet):
        p = self.run_wrapper(fleet)
        os.environ[seatceiling.FLEET_ENV] = fleet
        clause = seatceiling.stall_clause()
        self.assertTrue(clause, "control: the Python half said nothing")
        line = "[helm probe] TIMED OUT at 0.5s — this stop is UNCHECKED"
        self.assertEqual(p.stderr, line + clause + "\n")
        self.assertEqual(json.loads(p.stdout)["systemMessage"], line + clause)

    def test_the_shell_carries_the_same_reading(self):  # noqa: VACUOUS_ASSERTION — assert_same asserts the Python clause non-empty, then the shell line equal to it
        self.assert_same(self.fleet_dir())

    def test_the_shell_says_the_same_unknown(self):  # noqa: VACUOUS_ASSERTION — assert_same asserts the Python clause non-empty, then the shell line equal to it
        self.assert_same(self.fleet_dir(memory=None))
        self.assert_same(os.path.join(self.fleet_dir(), "gone"))

    def test_a_malformed_first_line_is_unknown_in_both(self):  # noqa: VACUOUS_ASSERTION — assert_same asserts the Python clause non-empty, then the shell line equal to it
        d = self.fleet_dir()
        _w(os.path.join(d, "cpu.pressure"), "full avg10=1.00 total=1\n")
        self.assert_same(d)

    def test_the_switch_off_adds_nothing_in_the_shell(self):
        p = self.run_wrapper()
        self.assertEqual(p.stderr, "[helm probe] TIMED OUT at 0.5s — this "
                                   "stop is UNCHECKED\n")


class TimerTest(unittest.TestCase):
    """The unit runs the pass OUTSIDE the fleet, and the verb says whether
    it is installed. The installer's whole contract is pressurewatch's row
    in tests/test_timerhealth.py's installer table."""

    def test_the_units_run_the_post_pass_in_app_slice_every_minute(self):
        fake_user_systemd(self)
        spath, service, tpath, timer = pressurewatch.timer_units()
        self.assertEqual(os.path.basename(spath), pressurewatch.SERVICE_NAME)
        self.assertEqual(os.path.basename(tpath), pressurewatch.TIMER_NAME)
        self.assertIn("\nSlice=app.slice\n", service)
        self.assertIn(" pressure-watch --post\n", service)
        self.assertIn("\nSuccessExitStatus=1\n", service)
        self.assertIn("\nOnUnitActiveSec=60s\n", timer)

    def test_the_timer_line_says_whether_it_is_installed(self):
        fake = fake_user_systemd(self)
        self.assertIn("is NOT installed", pressurewatch.timer_line())
        ok, detail = pressurewatch.ensure_timer()
        self.assertIs(ok, True, detail)
        self.assertIn("written but NOT enabled", pressurewatch.timer_line())
        wants = os.path.join(fake.unit_dir, "timers.target.wants")
        os.makedirs(wants)
        os.symlink(os.path.join(fake.unit_dir, pressurewatch.TIMER_NAME),
                   os.path.join(wants, pressurewatch.TIMER_NAME))
        self.assertIn("installed and enabled", pressurewatch.timer_line())

    def test_doctor_names_the_watcher_until_its_timer_is_enabled(self):  # noqa: VACUOUS_ASSERTION — the missing list is asserted to hold the watcher twice before it is empty, and wired names it
        """check_actuator_wiring's census: NO ACTUATOR while the timer is
        absent or written but not enabled, wired once it is enabled."""
        from helm import wiring
        fake = fake_user_systemd(self)
        hooks = tempfile.mkdtemp(prefix="helm-test-pressurewatch-hooks-")
        self.addCleanup(shutil.rmtree, hooks, True)
        want = {"pressure-watch": wiring.ACTUATORS["pressure-watch"]}

        def census():
            return wiring.actuator_census(
                hook_dir=hooks, unit_dir=fake.unit_dir, crontab="",
                settings_paths=[], obligations=want)
        os.makedirs(fake.unit_dir)
        self.assertEqual(census()["missing"], ["pressure-watch"])
        ok, detail = pressurewatch.ensure_timer()
        self.assertIs(ok, True, detail)
        self.assertEqual(census()["missing"], ["pressure-watch"],
                         "a unit file that is not enabled is not a schedule")
        wants = os.path.join(fake.unit_dir, "timers.target.wants")
        os.makedirs(wants)
        os.symlink(os.path.join(fake.unit_dir, pressurewatch.TIMER_NAME),
                   os.path.join(wants, pressurewatch.TIMER_NAME))
        got = census()
        self.assertEqual(got["missing"], [])
        self.assertIn("pressure-watch", got["wired"])

    def run_cmd(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = pressurewatch.cmd_pressure_watch(list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_the_verb(self):
        fake_user_systemd(self)
        rc, out, err = self.run_cmd("--bogus")
        self.assertEqual(rc, 2, err)
        rc, out, err = self.run_cmd("--install-timer", "--post")
        self.assertEqual(rc, 2)
        self.assertIn("--install-timer takes no other flag", err)
        rc, out, err = self.run_cmd()
        self.assertEqual(rc, 0, err)
        self.assertIn("pressure-watch: not read", out)
        self.assertIn("timer: helm-pressure-watch.timer is NOT installed", out)
        rc, out, err = self.run_cmd("--install-timer")
        self.assertEqual(rc, 0, err)
        self.assertIn("helm pressure-watch: installed every 60s", out)


if __name__ == "__main__":
    unittest.main()
