"""The rescue: end a throttled seat's runaway CHILD, and never the seat.

Every arm builds its own /proc and cgroup tree and injects the kill and the
sleep: nothing here reads this box's /proc or /sys/fs/cgroup, and nothing
signals a real process. The fixture is the incident at its real sizes (the
files only carry numbers): a pid-named seat slice at 12.95G of a 12G
memory.high, its agent stalled, a python child of a shell at 9.51G RSS, and
shmem 6% of the ceiling.

THE GUARD IS PROVEN BY FAILING. Each arm that asserts nothing was ended pairs
with the same fixture flipped on one variable, which ends the child; and the
planted arms sabotage one rule at a time and show that the assertion the real
arms rest on then fails.
"""

import os
import shutil
import signal
import tempfile
import unittest
from unittest import mock

from helm import seatceiling, seatrescue

AGENTS = "user.slice/user-1000.slice/user@1000.service/agents.slice"
GB = 1024 ** 3
AGENT, SHELL, CHILD, MCP = 41, 42, 43, 44


class Fleet(object):
    """One seat slice and the processes in it, as files."""

    def __init__(self, case, seat="pid41"):
        top = tempfile.mkdtemp(prefix="helm-test-rescue-")
        case.addCleanup(shutil.rmtree, top, True)
        self.cg, self.proc = os.path.join(top, "cgroup"), os.path.join(top,
                                                                        "proc")
        self.rel = "%s/%s" % (AGENTS, seatceiling.seat_slice_name(seat))
        self.slice = os.path.join(self.cg, self.rel)
        os.makedirs(os.path.join(self.slice, "run-p41-i1.scope"))
        self.count = 1486336

    def files(self, current, high, shmem):
        self.write("memory.current", "%d\n" % current)
        self.write("memory.high", "%d\n" % high)
        self.write("memory.stat", "anon %d\nfile 0\n%s" % (
            current, "" if shmem is None else "shmem %d\n" % shmem))
        self.events()

    def write(self, name, text):
        with open(os.path.join(self.slice, name), "w") as fh:
            fh.write(text)

    def events(self):
        self.write("memory.events", "low 0\nhigh %d\nmax 0\noom 0\n"
                                    "oom_kill 0\n" % self.count)

    def tick(self, _s=None):
        """The kernel counting a throttle that is happening NOW."""
        self.count += 200
        self.events()

    def process(self, pid, comm, argv, ppid, rss, stalled=False, start=None,
                status=True):
        d = os.path.join(self.proc, str(pid))
        os.makedirs(d, exist_ok=True)
        stat = "%d (%s) %s %d %s %s 0 0\n" % (
            pid, comm, "D" if stalled else "S", ppid, " ".join(["0"] * 17),
            start or str(1000 + pid))
        files = {"stat": stat, "comm": comm + "\n",
                 "cmdline": "\0".join(argv) + "\0",
                 "cgroup": "0::/%s/run-p41-i1.scope\n" % self.rel,
                 "wchan": "__mem_cgroup_handle_over_high" if stalled else "0"}
        if status:
            files["status"] = "Name:\t%s\nVmRSS:\t%d kB\n" % (comm,
                                                                rss // 1024)
        for name, text in files.items():
            with open(os.path.join(d, name), "w") as fh:
                fh.write(text)

    def gone(self, pid):
        shutil.rmtree(os.path.join(self.proc, str(pid)))

    def reading(self):
        got, trouble = seatceiling.fleet_pressure(root=self.cg, proc=self.proc,
                                                  sleep=self.tick)
        assert trouble is None, trouble
        (r,) = got.values()
        return r


def incident(case, agent_rss=int(2.2 * GB), child_rss=int(9.51 * GB),
             shmem=int(0.06 * 12 * GB)):
    """The wedge, as files: the agent and its python child both stalled.
    `shmem=None` writes a memory.stat with no shmem line."""
    f = Fleet(case)
    f.files(int(12.95 * GB), 12 * GB, shmem)
    f.process(AGENT, "claude", ["claude", "--resume"], 1, agent_rss,
              stalled=True)
    f.process(SHELL, "bash", ["/bin/bash", "-c", "cd x && python3 -"], AGENT,
              4 * 1024 * 1024)
    f.process(CHILD, "python3", ["python3", "-"], SHELL, child_rss,
              stalled=True)
    f.process(MCP, "node", ["node", "/x/mcp/server.js"], AGENT, 90 * 1024 ** 2)
    return f


class Kills(object):
    """The kill seam: records every signal; `exits` says whether a SIGTERM
    makes the fixture process go (it is removed from the fixture /proc)."""

    def __init__(self, fleet, exits=True):
        self.fleet, self.exits, self.calls = fleet, exits, []

    def __call__(self, pid, start, sig, proc):
        self.calls.append((pid, sig))
        if sig == signal.SIGKILL or (sig == signal.SIGTERM and self.exits):
            self.fleet.gone(pid)
        return True


def never_the_agent(case, kills, agents):
    """THE ASSERTION every arm rests on: no agent pid was ever signalled."""
    case.assertEqual([c for c in kills.calls if c[0] in agents], [],
                     "an agent was signalled")


class RescueTest(unittest.TestCase):

    def setUp(self):
        self.prior = {k: os.environ.get(k) for k in
                      (seatrescue.SWITCH, seatrescue.GRACE_ENV)}
        for k in self.prior:
            os.environ.pop(k, None)
        self.addCleanup(self.restore)

    def restore(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def run_rescue(self, f, census=frozenset({AGENT}), held=200, kills=None,
                   sleep=None):
        """(ended, why, kills) for one rescue of `f`, the throttle held
        `held` seconds."""
        r = f.reading()
        ld = seatrescue.load(r, census, f.proc)
        kills = kills or Kills(f)
        now = 10000.0
        done, why = seatrescue.rescue(r, ld, now - held, now, f.proc, f.cg,
                                      census, kill=kills,
                                      sleep=sleep or f.tick)
        return done, why, kills

    # ── the cure ──────────────────────────────────────────────────────────
    def test_the_incident_ends_the_child_and_never_the_agent(self):
        f = incident(self)
        r = f.reading()
        self.assertEqual((r.word, sorted(r.stalled)),
                         (seatceiling.THROTTLED, [AGENT, CHILD]))
        ld = seatrescue.load(r, frozenset({AGENT}), f.proc)
        self.assertEqual((ld.kind, ld.child.pid, ld.agents),
                         (seatrescue.CHILD, CHILD, (AGENT,)))
        self.assertIn("END THAT CHILD", seatrescue.cure_line(r, ld))
        self.assertNotIn("larger memory.high on that slice",
                         seatrescue.cure_line(r, ld))
        done, why, kills = self.run_rescue(f)
        self.assertIsNone(why)
        self.assertEqual(kills.calls, [(CHILD, signal.SIGTERM)])
        never_the_agent(self, kills, {AGENT})
        self.assertEqual((done["pid"], done["argv"], done["how"]),
                         (CHILD, "python3 -", "SIGTERM, and it exited"))
        line = seatrescue.ended_line(done, f.slice, None)
        for want in ("ENDED pid 43", "`python3 -`", "9.51G RSS",
                     "agents-pid41.slice", "THROTTLED for 200s",
                     "2 processes stalled", "shmem was only 6%",
                     "agent (pid 41) was never a candidate"):
            self.assertIn(want, line)

    def test_a_child_that_ignores_sigterm_gets_sigkill_after_the_grace(self):  # noqa: VACUOUS_ASSERTION — kills.calls is asserted EQUAL to both signals, a non-empty observable of the one seam, and the sleeps are summed against the grace
        f = incident(self)
        slept = []

        def sleep(s):
            slept.append(s)
            f.tick()
        done, why, kills = self.run_rescue(f, kills=Kills(f, exits=False),
                                           sleep=sleep)
        self.assertEqual(kills.calls, [(CHILD, signal.SIGTERM),
                                       (CHILD, signal.SIGKILL)], why)
        never_the_agent(self, kills, {AGENT})
        self.assertIn("SIGKILL after 5s", done["how"])
        self.assertGreaterEqual(sum(slept), seatrescue.KILL_GRACE_S)

    def test_an_ancestor_of_an_agent_is_never_a_candidate(self):
        """The shell above a subagent's claude is protected even when it is
        the biggest non-agent: its death can take the agent with it. Control:
        the same shell with no agent under it is the one ended."""
        f = incident(self, child_rss=100 * 1024 ** 2)
        f.process(SHELL, "bash", ["/bin/bash"], AGENT, int(9.51 * GB))
        f.process(45, "claude", ["claude", "-p"], SHELL, 300 * 1024 ** 2)
        done, why, kills = self.run_rescue(f, census=frozenset({AGENT, 45}))
        self.assertIsNone(done)
        self.assertEqual(kills.calls, [])
        self.assertIn("quarter of memory.high", why)
        f.gone(45)
        done, why, kills = self.run_rescue(f)
        self.assertEqual(kills.calls, [(SHELL, signal.SIGTERM)], why)

    # ── what ends nothing, each paired with its flip ──────────────────────
    def test_a_shmem_heavy_slice_ends_nothing(self):
        f = incident(self, shmem=int(0.40 * 12 * GB))
        done, why, kills = self.run_rescue(f)
        self.assertIsNone(done)
        self.assertEqual(kills.calls, [])
        self.assertIn("the load is shmem, not one child", why)
        ld = seatrescue.load(f.reading(), frozenset({AGENT}), f.proc)
        self.assertEqual(ld.kind, seatrescue.SHMEM)
        self.assertIn("reap its scratch", seatrescue.cure_line(f.reading(),
                                                               ld))
        # the flip: the same slice with shmem at the incident's 6%
        f.files(int(12.95 * GB), 12 * GB, int(0.06 * 12 * GB))
        self.assertEqual(self.run_rescue(f)[2].calls,
                         [(CHILD, signal.SIGTERM)])

    def test_an_unreadable_shmem_ends_nothing(self):
        f = incident(self, shmem=None)
        done, why, kills = self.run_rescue(f)
        self.assertIsNone(done)
        self.assertEqual(kills.calls, [])
        self.assertIn("shmem would not read", why)

    def test_an_unreadable_child_rss_ends_nothing(self):
        f = incident(self)
        os.remove(os.path.join(f.proc, str(CHILD), "status"))
        done, why, kills = self.run_rescue(f)
        self.assertIsNone(done)
        self.assertEqual(kills.calls, [])
        self.assertIn("pid 43's RSS would not read", why)

    def test_an_agent_that_cannot_be_told_apart_ends_nothing(self):
        """A versioned comm whose exe will not read, and no census row: it may
        be the agent, so nothing in the slice is ended."""
        f = incident(self)
        f.process(AGENT, "2.1.238", ["/x/claude/versions/2.1.238"], 1,
                  int(2.2 * GB), stalled=True)
        done, why, kills = self.run_rescue(f, census=frozenset())
        self.assertIsNone(done)
        self.assertEqual(kills.calls, [])
        self.assertIn("pid 41 cannot be told from an agent", why)
        # the flip: the census proves it, and the child is ended
        self.assertEqual(self.run_rescue(f)[2].calls,
                         [(CHILD, signal.SIGTERM)])

    def test_a_failed_census_ends_nothing(self):
        f = incident(self)
        done, why, kills = self.run_rescue(f, census=None)
        self.assertIsNone(done)
        self.assertEqual(kills.calls, [])
        self.assertIn("census failed", why)

    def test_unreadable_events_at_the_confirmation_end_nothing(self):
        f = incident(self)

        def lose(_s):
            os.remove(os.path.join(f.slice, "memory.events"))
        done, why, kills = self.run_rescue(f, sleep=lose)
        self.assertIsNone(done)
        self.assertEqual(kills.calls, [])
        self.assertIn("memory.events would not read", why)

    def test_a_spell_under_the_grace_ends_nothing(self):
        f = incident(self)
        done, why, kills = self.run_rescue(f, held=60)
        self.assertIsNone(done)
        self.assertEqual(kills.calls, [])
        self.assertIn("of the 180s grace", why)
        self.assertEqual(self.run_rescue(f, held=180)[2].calls,
                         [(CHILD, signal.SIGTERM)], "control: at the grace")

    def test_a_still_counter_ends_nothing_however_large(self):
        """HISTORY IS NOT NOW. Stalled processes on the plane's reading, a
        counter of 1.49M, and no rise across the confirmation window: the
        repair does not act on the count alone."""
        f = incident(self)
        r = f.reading()
        ld = seatrescue.load(r, frozenset({AGENT}), f.proc)
        kills = Kills(f)
        done, why = seatrescue.rescue(r, ld, 0.0, 1000.0, f.proc, f.cg,
                                      frozenset({AGENT}), kill=kills,
                                      sleep=lambda _s: None)
        self.assertIsNone(done)
        self.assertEqual(kills.calls, [])
        self.assertIn("did not show the throttle now", why)
        done, why = seatrescue.rescue(r, ld, 0.0, 1000.0, f.proc, f.cg,
                                      frozenset({AGENT}), kill=kills,
                                      sleep=f.tick)
        self.assertEqual(kills.calls, [(CHILD, signal.SIGTERM)], why)

    def test_a_pid_reused_between_the_survey_and_the_signal_is_refused(self):
        f = incident(self)
        r = f.reading()
        ld = seatrescue.load(r, frozenset({AGENT}), f.proc)
        f.process(CHILD, "python3", ["python3", "-"], SHELL, int(9.51 * GB),
                  stalled=True, start="99999")
        kills = Kills(f)
        done, why = seatrescue.rescue(r, ld, 0.0, 1000.0, f.proc, f.cg,
                                      frozenset({AGENT}), kill=kills,
                                      sleep=f.tick)
        self.assertIsNone(done)
        self.assertEqual(kills.calls, [])
        self.assertIn("changed between the survey and now", why)
        # the flip: the surveyed generation back, and the same seam ends it
        f.process(CHILD, "python3", ["python3", "-"], SHELL, int(9.51 * GB),
                  stalled=True)
        seatrescue.rescue(r, ld, 0.0, 1000.0, f.proc, f.cg, frozenset({AGENT}),
                          kill=kills, sleep=f.tick)
        self.assertEqual(kills.calls, [(CHILD, signal.SIGTERM)])

    def test_the_switch_off_ends_nothing_and_the_wake_says_a_person_must(self):
        f = incident(self)
        os.environ[seatrescue.SWITCH] = "off"
        done, why, kills = self.run_rescue(f)
        self.assertIsNone(done)
        self.assertEqual(kills.calls, [])
        self.assertIn("HELM_SEAT_RESCUE", why)
        ld = seatrescue.load(f.reading(), frozenset({AGENT}), f.proc)
        self.assertIn("a person must", seatrescue.cure_line(f.reading(), ld))
        os.environ[seatrescue.SWITCH] = "on"
        self.assertEqual(self.run_rescue(f)[2].calls,
                         [(CHILD, signal.SIGTERM)])

    def test_the_agent_as_the_load_names_raise_and_ends_nothing(self):
        f = incident(self, agent_rss=int(11 * GB), child_rss=200 * 1024 ** 2)
        r = f.reading()
        ld = seatrescue.load(r, frozenset({AGENT}), f.proc)
        self.assertEqual(ld.kind, seatrescue.AGENT)
        self.assertIn("larger memory.high on that slice",
                      seatrescue.cure_line(r, ld))
        done, why, kills = self.run_rescue(f)
        self.assertIsNone(done)
        self.assertEqual(kills.calls, [])
        self.assertIn("the load is agent", why)

    def test_a_codex_agent_is_an_agent_by_its_comm(self):
        """Not in the claude census: its own comm proves it. A codex seat's
        slice with a big child ends the child only."""
        f = incident(self)
        f.process(AGENT, "codex", ["codex"], 1, int(2.2 * GB), stalled=True)
        done, why, kills = self.run_rescue(f, census=frozenset())
        self.assertEqual(kills.calls, [(CHILD, signal.SIGTERM)], why)
        self.assertEqual(done["agents"], [AGENT])

    def test_a_codex_agent_is_an_agent_by_its_argv(self):
        """A node launcher running the codex script: argv names the binary."""
        f = incident(self)
        f.process(AGENT, "node", ["node", "/x/bin/codex.js"], 1,
                  int(2.2 * GB), stalled=True)
        done, why, kills = self.run_rescue(f, census=frozenset())
        self.assertEqual(kills.calls, [(CHILD, signal.SIGTERM)], why)
        self.assertEqual(done["agents"], [AGENT])

    def test_the_default_kill_never_signals_a_fixture_tree(self):
        """An arm that forgets the seam signals nothing: the default refuses
        any tree but the host's own /proc."""
        f = incident(self)
        self.assertEqual(seatrescue._signal(CHILD, "1043", signal.SIGTERM,
                                            f.proc), (None, None))
        self.assertEqual(seatrescue.end(
            seatrescue.Proc(CHILD, SHELL, "1043", 1, ("python3",), False),
            f.proc, kill=None, sleep=lambda _s: None),
            (False, "SIGTERM could not be sent"))

    # ── the gates read again at the signal ────────────────────────────────
    def test_an_agent_born_during_confirmation_protects_its_ancestor(self):
        """THE MEMBERS ARE READ AFTER THE CONFIRMATION SLEEP. A claude the
        candidate spawns inside that window makes the candidate its ancestor,
        so it is protected before the signal. Control: with that late agent
        gone, the same rescue ends the child."""
        f = incident(self)
        r = f.reading()
        ld = seatrescue.load(r, frozenset({AGENT}), f.proc)
        self.assertEqual((ld.kind, ld.child.pid), (seatrescue.CHILD, CHILD),
                         "control: the plane's read named the child")
        born = []

        def birth(_s=None):
            if not born:
                f.process(45, "claude", ["claude", "-p"], CHILD,
                          300 * 1024 ** 2)
                born.append(45)
            f.tick()
        kills = Kills(f)
        done, why = seatrescue.rescue(r, ld, 0.0, 1000.0, f.proc, f.cg,
                                      frozenset({AGENT}), kill=kills,
                                      sleep=birth)
        self.assertEqual(kills.calls, [], "the ancestor of an agent born "
                                          "during confirmation was ended")
        never_the_agent(self, kills, {AGENT, 45})
        self.assertIsNone(done)
        self.assertIn("quarter of memory.high", why)
        f.gone(45)
        seatrescue.rescue(r, ld, 0.0, 1000.0, f.proc, f.cg, frozenset({AGENT}),
                          kill=kills, sleep=f.tick)
        self.assertEqual(kills.calls, [(CHILD, signal.SIGTERM)])

    def test_a_child_that_is_no_longer_the_load_is_not_ended(self):  # noqa: VACUOUS_ASSERTION — the kill seam is the injected observable (no real signal may be sent); the same arm asserts it EQUAL to the one SIGTERM once the load is the child again
        """THE SHARES ARE READ AGAIN AT THE SIGNAL. The plane named the child
        as the load; by the signal it holds a sliver of memory.high, or the
        slice's shmem has grown past its quarter. Ending it then relieves
        nothing, so nothing is ended. Control: the child back at its size and
        shmem back at 6%, the same rescue ends it."""
        f = incident(self)
        r = f.reading()
        ld = seatrescue.load(r, frozenset({AGENT}), f.proc)
        self.assertEqual(ld.kind, seatrescue.CHILD, "control: the plane's load")
        kills = Kills(f)

        def rescue():
            return seatrescue.rescue(r, ld, 0.0, 1000.0, f.proc, f.cg,
                                     frozenset({AGENT}), kill=kills,
                                     sleep=f.tick)
        f.process(CHILD, "python3", ["python3", "-"], SHELL, 200 * 1024 ** 2,
                  stalled=True)
        done, why = rescue()
        self.assertEqual(kills.calls, [], "a child that shrank to 200M of a "
                                          "12G ceiling was ended")
        self.assertIn("quarter of memory.high", why)
        f.process(CHILD, "python3", ["python3", "-"], SHELL, int(9.51 * GB),
                  stalled=True)
        f.files(int(12.95 * GB), 12 * GB, int(0.40 * 12 * GB))
        done, why = rescue()
        self.assertEqual(kills.calls, [], "the child of a slice whose shmem "
                                          "grew past a quarter was ended")
        self.assertIn("shmem", why)
        f.files(int(12.95 * GB), 12 * GB, int(0.06 * 12 * GB))
        done, why = rescue()
        self.assertEqual(kills.calls, [(CHILD, signal.SIGTERM)], why)

    def test_missing_or_unreadable_member_cgroup_fails_closed(self):  # noqa: VACUOUS_ASSERTION — both enumerated fact shapes run as subtests and each unconditionally removes the unknown member, then asserts the same kill seam fires once
        """A live process whose cgroup fact cannot be read may be a new agent
        under the candidate. The strict signal-time walk omits neither shape;
        the child is ended only after that unknown member is proven gone."""
        for shape in ("missing", "unreadable"):
            with self.subTest(shape=shape):
                f = incident(self)
                r = f.reading()
                ld = seatrescue.load(r, frozenset({AGENT}), f.proc)
                f.process(45, "claude", ["claude", "-p"], CHILD,
                          300 * 1024 ** 2)
                fact = os.path.join(f.proc, "45", "cgroup")
                if shape == "missing":
                    os.remove(fact)
                else:
                    os.remove(fact)
                    os.mkdir(fact)
                kills = Kills(f)
                done, why = seatrescue.rescue(
                    r, ld, 0.0, 1000.0, f.proc, f.cg, frozenset({AGENT}),
                    kill=kills, sleep=f.tick)
                self.assertIsNone(done)
                self.assertEqual(kills.calls, [])
                self.assertIn("pid 45's cgroup would not read", why)
                f.gone(45)
                seatrescue.rescue(r, ld, 0.0, 1000.0, f.proc, f.cg,
                                  frozenset({AGENT}), kill=kills, sleep=f.tick)
                self.assertEqual(kills.calls, [(CHILD, signal.SIGTERM)])

    def test_high_and_shmem_are_read_after_confirmation_sleep(self):  # noqa: VACUOUS_ASSERTION — both enumerated load facts run as subtests and each unconditionally restores the incident facts, then asserts the same kill seam fires once
        """The sampled rise is a sleep window, not a license to carry the load
        facts across it. A larger ceiling or newly dominant shmem during that
        sleep makes the child no longer the load; restoring the incident facts
        is the green control for each state."""
        for fact in ("memory.high", "shmem"):
            with self.subTest(fact=fact):
                f = incident(self)
                r = f.reading()
                ld = seatrescue.load(r, frozenset({AGENT}), f.proc)

                def change(_s=None):
                    f.tick()
                    if fact == "memory.high":
                        f.write("memory.high", "%d\n" % (48 * GB))
                    else:
                        f.write("memory.stat", "anon 0\nfile 0\nshmem %d\n"
                                % (5 * GB))
                kills = Kills(f)
                done, why = seatrescue.rescue(
                    r, ld, 0.0, 1000.0, f.proc, f.cg, frozenset({AGENT}),
                    kill=kills, sleep=change)
                self.assertIsNone(done)
                self.assertEqual(kills.calls, [])
                self.assertTrue("quarter of memory.high" in why
                                or "shmem" in why, why)
                f.files(int(12.95 * GB), 12 * GB, int(0.06 * 12 * GB))
                seatrescue.rescue(r, ld, 0.0, 1000.0, f.proc, f.cg,
                                  frozenset({AGENT}), kill=kills, sleep=f.tick)
                self.assertEqual(kills.calls, [(CHILD, signal.SIGTERM)])

    def test_same_start_exec_to_claude_is_refused_at_the_final_fence(self):
        """A pidfd pins the incarnation, not its executable. After the
        post-confirm load read, the same pid/start execs claude; the final full
        fence sees agent identity and refuses the signal. Unchanged, it ends."""
        f = incident(self)
        r = f.reading()
        ld = seatrescue.load(r, frozenset({AGENT}), f.proc)
        real = seatrescue.fresh
        calls = []

        def exec_before_final(*args):
            calls.append(1)
            if len(calls) == 3:
                f.process(CHILD, "claude", ["claude", "-p"], SHELL,
                          int(9.51 * GB), stalled=True,
                          start=str(1000 + CHILD))
            return real(*args)
        kills = Kills(f)
        with mock.patch.object(seatrescue, "fresh", side_effect=exec_before_final):
            done, why = seatrescue.rescue(
                r, ld, 0.0, 1000.0, f.proc, f.cg, frozenset({AGENT}),
                kill=kills, sleep=f.tick)
        self.assertEqual(len(calls), 3, "the final fence did not reread")
        self.assertIsNone(done)
        self.assertEqual(kills.calls, [])
        self.assertIn("final fence", why)
        f.process(CHILD, "python3", ["python3", "-"], SHELL,
                  int(9.51 * GB), stalled=True, start=str(1000 + CHILD))
        seatrescue.rescue(r, ld, 0.0, 1000.0, f.proc, f.cg,
                          frozenset({AGENT}), kill=kills, sleep=f.tick)
        self.assertEqual(kills.calls, [(CHILD, signal.SIGTERM)])

    def test_a_signal_that_did_not_end_the_child_is_never_called_ended(self):
        """ENDED ONLY ON A PROVEN EXIT: the entry is gone, a later process
        holds the pid, or it is a corpse. A SIGTERM that could not be sent,
        a child still there after SIGKILL, and one whose stat merely stopped
        reading are not ended, and the words say what happened. Control: a
        child whose entry goes on SIGTERM is ended."""
        f = incident(self)
        p = seatrescue.survey((CHILD,), f.proc, frozenset({AGENT}))[0][CHILD]
        sent = []

        def stays(pid, start, sig, proc):
            sent.append(sig)
            return True

        def unreadable(pid, start, sig, proc):
            sent.append(sig)
            stat = os.path.join(proc, str(pid), "stat")
            if os.path.exists(stat):
                os.remove(stat)
            return True
        quiet = lambda _s: None  # noqa: E731
        self.assertEqual(seatrescue.end(p, f.proc, kill=lambda *a: None,
                                        sleep=quiet),
                         (False, "SIGTERM could not be sent"))
        self.assertEqual(seatrescue.end(p, f.proc, kill=stays, sleep=quiet),
                         (False, "SIGTERM, then SIGKILL after 5s, and its "
                                 "exit was not seen"))
        self.assertEqual(sent, [signal.SIGTERM, signal.SIGKILL])
        self.assertEqual(seatrescue.end(p, f.proc, kill=unreadable,
                                        sleep=quiet),
                         (False, "SIGTERM, then SIGKILL after 5s, and its "
                                 "exit was not seen"))
        f.process(CHILD, "python3", ["python3", "-"], SHELL, int(9.51 * GB),
                  stalled=True)
        self.assertEqual(seatrescue.end(p, f.proc, kill=Kills(f),
                                        sleep=quiet),
                         (True, "SIGTERM, and it exited"))

    def test_the_default_kill_refuses_a_reused_pid_under_its_pidfd(self):  # noqa: VACUOUS_ASSERTION — the send seam stands in for the real signal; the same arm asserts it EQUAL to the one SIGTERM for the surveyed generation
        """THE LAST CHECK BEFORE THE SIGNAL. With the pidfd held, the pid's
        start time is read again, and a later process on the same pid is
        refused and never signalled. The pidfd calls are seams here (no real
        pidfd, no real signal), and the fixture tree stands in for the host's
        /proc for this arm only. Control: the surveyed generation is
        signalled, once."""
        f = incident(self)
        sent = []

        def pidfd_open(_pid):
            return os.open(os.devnull, os.O_RDONLY)

        def send(_fd, sig):
            sent.append(sig)
            if sig == signal.SIGSTOP:
                path = os.path.join(f.proc, str(CHILD), "stat")
                with open(path) as fh:
                    text = fh.read()
                with open(path, "w") as fh:
                    fh.write(text.replace(") D ", ") T ", 1)
                             .replace(") S ", ") T ", 1))
        with mock.patch.object(seatceiling, "HOST_PROC",
                               os.path.normpath(f.proc)), \
                mock.patch.object(os, "pidfd_open", pidfd_open,
                                  create=True), \
                mock.patch.object(signal, "pidfd_send_signal", send,
                                  create=True):
            self.assertEqual(seatrescue._signal(
                CHILD, "99999", signal.SIGTERM, f.proc), (False, None))
            self.assertEqual(sent, [], "a reused pid was signalled")
            self.assertEqual(seatrescue._signal(
                CHILD, str(1000 + CHILD), signal.SIGTERM, f.proc),
                (True, None))
        self.assertEqual(sent, [signal.SIGSTOP, signal.SIGTERM,
                                signal.SIGCONT])

    def test_pidfd_stop_closes_same_start_exec_gap_before_signal(self):
        """A pending same-start exec can land after the ordinary load check.
        SIGSTOP quiesces that incarnation first; the fence then sees claude,
        refuses TERM, and resumes the process helm stopped."""
        f = incident(self)
        r = f.reading()
        expected = seatrescue.load(r, frozenset({AGENT}), f.proc).child
        sent = []

        def send(_fd, sig):
            sent.append(sig)
            if sig == signal.SIGSTOP:
                f.process(CHILD, "claude", ["claude", "-p"], SHELL,
                          int(9.51 * GB), start=str(1000 + CHILD))
                path = os.path.join(f.proc, str(CHILD), "stat")
                with open(path) as fh:
                    text = fh.read()
                with open(path, "w") as fh:
                    fh.write(text.replace(") S ", ") T ", 1))
        with mock.patch.object(seatceiling, "HOST_PROC",
                               os.path.normpath(f.proc)), \
                mock.patch.object(seatceiling, "reads_host",
                                  return_value=False), \
                mock.patch.object(os, "pidfd_open",
                                  side_effect=lambda _pid: os.open(
                                      os.devnull, os.O_RDONLY), create=True), \
                mock.patch.object(signal, "pidfd_send_signal", send,
                                  create=True):
            got = seatrescue._signal(
                CHILD, expected.start, signal.SIGTERM, f.proc,
                lambda: seatrescue._final_fence(
                    r, expected, f.cg, f.proc, frozenset({AGENT})),
                lambda _s: None)
        self.assertIn("final fence", got[1])
        self.assertEqual(sent, [signal.SIGSTOP, signal.SIGCONT],
                         "TERM crossed the stopped identity fence")

    # ── planted violations: each rule removed, the guard must fail ────────
    def test_PLANTED_no_agent_exclusion_signals_the_agent_and_is_caught(self):
        """With the agent the BIGGEST process, a picker that forgot the agent
        rule ends the agent — and the assertion every arm rests on catches
        it. Control: the real picker ends the child on the same fixture."""
        f = incident(self, agent_rss=int(10 * GB), child_rss=int(3.5 * GB))
        done, why, kills = self.run_rescue(f)
        self.assertEqual(kills.calls, [(CHILD, signal.SIGTERM)], why)

        def sabotaged(procs):
            return sorted(procs.values(), key=lambda p: -p.rss)
        f = incident(self, agent_rss=int(10 * GB), child_rss=int(3.5 * GB))
        with mock.patch.object(seatrescue, "candidates", sabotaged), \
                mock.patch.object(seatrescue, "_final_fence",
                                  return_value=None):
            done, why, kills = self.run_rescue(f)
        self.assertEqual(kills.calls[0], (AGENT, signal.SIGTERM), why)
        with self.assertRaises(AssertionError):
            never_the_agent(self, kills, {AGENT})

    def test_PLANTED_no_shmem_gate_ends_the_child_of_a_shmem_slice(self):
        f = incident(self, shmem=int(0.40 * 12 * GB))
        self.assertEqual(self.run_rescue(f)[2].calls, [])
        with mock.patch.object(seatceiling, "SHMEM_FRACTION", 1.01):
            self.assertEqual(self.run_rescue(f)[2].calls,
                             [(CHILD, signal.SIGTERM)],
                             "the shmem arm does not rest on the shmem gate")

    def test_PLANTED_no_grace_ends_the_child_of_a_short_spell(self):
        f = incident(self)
        self.assertEqual(self.run_rescue(f, held=60)[2].calls, [])
        os.environ[seatrescue.GRACE_ENV] = "0"
        self.assertEqual(self.run_rescue(f, held=60)[2].calls,
                         [(CHILD, signal.SIGTERM)],
                         "the grace arm does not rest on the grace")


if __name__ == "__main__":
    unittest.main()
