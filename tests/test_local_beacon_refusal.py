#!/usr/bin/env python3
"""A LOCAL-family seat may not end a turn into idle with no beacon (task/3382).

THE MEASUREMENT. Over about 41 hours, the three local seats ended 128 turns
into an idle with no live beacon. They produced one NO ARMED BEACON block and
one recorded advisory. The obligation block (`_beacon_block`) refuses only a
seat that owes dispatch work, and only once per state. The strict re-arm rung
(`_rearm_rung`) only advises, and an exit-0 Stop hook's stderr does not reach
the model. When the seat IS told directly, it arms (41 of 42 idle expiries,
14 of 17 blocks).

THE DESIGN. When the strict probe PROVES that no beacon is bound to this
session, and the seat's VERIFIED runtime family is local (a continuation costs
nothing there), the re-arm line becomes a refusal. There is no latch:
`stop_hook_active` bounds it to one continuation per stop chain. Every other
answer stays today's advice: an unproven probe, a binding that cannot be
resolved, a paid family, a name with no verified runtime, a pane-woken family,
a seat whose delivery is paused.

THE RESIDUAL IS PINNED, NOT HIDDEN. `stop_hook_active` is global. When another
rung makes the continuation, the continuation's stop does not ask about the
beacon. So the guarantee is ONE re-arm opportunity per fresh stop, not "a
local seat cannot idle unarmed".

Hermetic: the probe is a stub or the real one over a fixture /proc tree, and
the live fleet is out of reach.
"""
import json
import os
import re
import shutil
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import (beacons, chat, seat, seat_catalog, seats,  # noqa: E402,F401 — seat is the facade the injection audit requires beside seat_catalog
                  seats_stop_guard, seats_stop_seam, seats_stop_signals)
from tests.test_seats import BeaconProcsBase  # noqa: E402
import tests.test_beacons as tb  # noqa: E402 — the module, so its arms are not collected here again

LOCAL = "qwen27"  # noqa: SEAT_NAME — catalog family: a local model's runtime
PANE = "cursor"  # noqa: SEAT_NAME — catalog family A7 plants a pane wake on (no family catalogues one)
PAID = "codex"  # noqa: SEAT_NAME — catalog family on a paid runtime
NATIVE = "claude"  # noqa: SEAT_NAME — the native harness family
SEAT = "zz-local-seat"
SID = "cccccccc-1111-2222-3333-444444444444"
OTHER = "dddddddd-1111-2222-3333-444444444444"
AGENT = "71717"
REFUSED = "NO BEACON — seat '%s'"
MONITOR = ('Monitor(command: "helm chat wait --seat %s --follow --replace", '
           'description: "inbox beacon", timeout_ms: 1800000)')
_REAL_PROBE = seats_stop_signals.beacon_procs


def runtime(family, backend="proxy"):
    return {"family": family, "agent_harness": "claude", "backend": backend}


class LocalSeatBase(BeaconProcsBase):
    """A launched seat with a verified runtime, driven through the real verb."""

    def setUp(self):
        super().setUp()
        # SeatsBase disables the scratch reaper. A module that drives
        # stop-guard names that setting itself (the pane-wake file's rule).
        self.assertEqual(os.environ["HELM_SCRATCH_GC"], "0")
        self._env = None

    def tearDown(self):
        if self._env is not None:
            self._env.stop()
        super().tearDown()

    def seat_up(self, name=SEAT, family=LOCAL, backend="proxy", session=SID):
        # patch.dict restores on stop; a subscript assign would leak the stamp.
        self._env = mock.patch.dict(os.environ, {"HELM_CHAT_NAME": name,
                                                 "CLAUDE_PID": AGENT})
        self._env.start()
        seats.join(seat=name, session=session, runtime=runtime(family, backend),
                   cwd="/tmp/p")

    def guard(self, payload=None, pids=(), trouble=None, obligation=False,
              probe=None):
        stdin = json.dumps(payload or {"session_id": SID}).encode()
        stub = probe or mock.Mock(return_value=(list(pids), trouble))
        with mock.patch.object(seats, "beacon_procs", stub), \
                mock.patch.object(seats_stop_signals, "_beacon_obligation",
                                  return_value=obligation):
            return self.cmd("stop-guard", ["--hook-json"], stdin=stdin)

    def real_probe(self, root):
        """The PRODUCTION probe, pointed at the fixture process table."""
        def probe(name, proc_dir=None, strict=False, **kw):
            return _REAL_PROBE(name, root, strict=strict, **kw)
        return probe

    def waiter(self, pid, name=SEAT, sid=SID, flags=("--follow",),
               home=True, seat_flag=True, env_name=None, extra=()):
        argv = ["python3", "/x/bin/helm", "chat", "wait"]
        if seat_flag:
            argv += ["--seat", name]
        argv += list(flags)
        env = ["CLAUDE_CODE_SESSION_ID=%s" % sid] if sid else []
        if home:
            env.append("HELM_HOME=%s" % os.environ["HELM_HOME"])
        if env_name:
            env.append("HELM_CHAT_NAME=%s" % env_name)
        return self.proc(pid, argv, env=env + list(extra))

    def assert_refused(self, rc, err, name=SEAT):
        self.assertEqual(rc, 2, err)
        self.assertIn(REFUSED % name, err)
        self.assertIn(MONITOR % name, err)
        self.assertIn('ToolSearch(query: "select:Monitor")', err)
        self.assertIn("Refused once per stop", err)

    def assert_not_refused(self, err, name=SEAT):
        self.assertNotIn(REFUSED % name, err)
        self.assertNotIn(MONITOR % name, err)


class LocalSeatRefusalTest(LocalSeatBase):
    """A1-A9: the refusal, and the controls it must leave alone."""

    def test_A1_a_local_seat_with_no_beacon_is_refused_with_the_monitor_call(self):  # noqa: VACUOUS_ASSERTION — assert_refused asserts rc 2 and the refusal text PRESENT
        """The seat's NAME is not a family; its verified runtime is local."""
        self.seat_up()
        rc, _out, err = self.guard()
        self.assert_refused(rc, err)

    def test_A2_a_new_turn_in_the_same_state_is_refused_again(self):  # noqa: VACUOUS_ASSERTION — assert_refused asserts rc 2 and the refusal text PRESENT
        """No latch: the obligation block's once-per-state latch is why a seat
        that was told once heard nothing on every later turn."""
        self.seat_up()
        first = self.guard()
        second = self.guard()
        self.assert_refused(first[0], first[2])
        self.assert_refused(second[0], second[2])

    def test_A3_the_continuation_stop_passes(self):  # noqa: VACUOUS_ASSERTION — the fresh stop in the same arm is the positive control on the same observable
        self.seat_up()
        rc, _out, fresh = self.guard()
        self.assert_refused(rc, fresh)
        rc, _out, err = self.guard({"session_id": SID,
                                    "stop_hook_active": True})
        self.assertEqual(rc, 0, err)
        self.assert_not_refused(err)

    def test_A3_residual_another_rungs_continuation_skips_the_beacon(self):  # noqa: VACUOUS_ASSERTION — the pinned residual IS an absence; A1 and A3 above are its positive controls
        """THE RESIDUAL, PINNED (codex planning check 1). The first stop is
        refused by the INBOX rung while the beacon is live. The beacon dies
        during the continuation. That continuation's stop carries
        stop_hook_active, which skips every beacon rung, so the seat idles
        unarmed. One re-arm opportunity per FRESH stop is the guarantee."""
        self.seat_up()
        chat.post("@%s look at this" % SEAT, who="bob")
        rc, _out, err = self.guard(pids=[4242])
        self.assertEqual(rc, 2, err)
        self.assertIn("undelivered", err)
        self.assert_not_refused(err)
        rc, _out, err = self.guard({"session_id": SID,
                                    "stop_hook_active": True})
        self.assertEqual(rc, 0, err)
        self.assert_not_refused(err)

    def test_A4_probe_trouble_stays_advice(self):
        self.seat_up()
        rc, _out, err = self.guard(trouble="process table unlistable (EPERM)")
        self.assertEqual(rc, 0, err)
        self.assertIn("NO PROVEN WAKE PATH", err)
        self.assertIn("could not be PROVEN", err)
        self.assert_not_refused(err)

    def test_A5_a_live_beacon_passes_silently(self):  # noqa: VACUOUS_ASSERTION — A1 is the positive control on the same verb and fixture
        self.seat_up()
        rc, _out, err = self.guard(pids=[4242])
        self.assertEqual(rc, 0, err)
        self.assertNotIn("NO PROVEN WAKE PATH", err)
        self.assert_not_refused(err)

    def test_A6_a_name_is_not_a_family_and_paid_seats_keep_the_advice(self):  # noqa: VACUOUS_ASSERTION — every case asserts the advice PRESENT beside the absent refusal
        """Each case gets today's advice and no refusal: a native claude
        runtime NAMED like a local family, a paid proxy seat, a native seat,
        and a stop whose session has no runtime record (a relaunch the roster
        has not seen: missing evidence fails open)."""
        # ONE SESSION PER SEAT: a session already bound to another seat makes
        # the join a disputed identity, which registers nothing.
        cases = (("named-like-local", LOCAL, runtime(NATIVE, "native"), 1, 1),
                 ("paid-proxy", "zz-paid-seat", runtime(PAID), 2, 2),
                 ("native", "zz-native-seat", runtime(NATIVE, "native"), 3, 3),
                 ("unrecorded-session", SEAT, runtime(LOCAL), 4, 5))
        for label, name, rt, joined, stopped in cases:
            with self.subTest(case=label):
                sid = "eeeeeeee-1111-2222-3333-44444444444%d"
                with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": name,
                                                  "CLAUDE_PID": AGENT}):
                    seats.join(seat=name, session=sid % joined, runtime=rt,
                               cwd="/tmp/p")
                    rc, _out, err = self.guard({"session_id": sid % stopped})
                self.assertEqual(rc, 0, err)
                self.assertIn("NO PROVEN WAKE PATH", err)
                self.assert_not_refused(err, name)

    def test_A7_a_pane_woken_family_is_neither_refused_nor_advised(self):  # noqa: VACUOUS_ASSERTION — A1 is the positive control on the same verb
        # No family catalogues a pane wake (the cursor seat keeps its Monitor
        # and arms a beacon), so the arm plants one on a real proxy family for
        # its duration: the stop guard's pane reader stays covered.
        pane = dict(seat_catalog.FAMILIES[PANE], wake="pane")
        self.seat_up(PANE, PANE)
        with mock.patch.dict(seat_catalog.FAMILIES, {PANE: pane}):
            self.assertEqual(seat_catalog.FAMILIES[PANE].get("wake"), "pane")
            rc, _out, err = self.guard()
        self.assertEqual(rc, 0, err)
        self.assertNotIn("NO PROVEN WAKE PATH", err)
        self.assert_not_refused(err, PANE)

    def test_A8_the_refusal_rides_emit_blocks(self):  # noqa: VACUOUS_ASSERTION — assert_refused asserts rc 2 and the refusal text PRESENT
        self.seat_up()
        with mock.patch.object(seats_stop_seam, "emit_blocks",
                               wraps=seats_stop_seam.emit_blocks) as spy:
            rc, _out, err = self.guard()
        self.assert_refused(rc, err)
        printed = "\n".join("\n".join(c.args[0]) for c in spy.call_args_list)
        self.assertIn(MONITOR % SEAT, printed)

    def test_A8_it_rides_beside_another_rungs_refusal(self):
        self.seat_up()
        chat.post("@%s look at this" % SEAT, who="bob")
        rc, _out, err = self.guard()
        self.assert_refused(rc, err)
        self.assertIn("undelivered", err)
        self.assertEqual(err.count("Monitor(command:"), 1, err)

    def test_A8_one_beacon_instruction_beside_the_obligation_block(self):
        """Codex planning check 5: `_beacon_block` runs first and may refuse.
        That stop gets ONE instruction (the obligation block's, with its latch
        intact). The next fresh stop, where that latch passes, is refused by
        this rung."""
        self.seat_up()
        rc, _out, first = self.guard(obligation=True)
        self.assertEqual(rc, 2, first)
        self.assertIn("NO ARMED BEACON", first)
        self.assertEqual(first.count("Monitor(command:"), 1, first)
        rc, _out, second = self.guard(obligation=True)
        self.assert_refused(rc, second)
        self.assertNotIn("NO ARMED BEACON", second)
        self.assertEqual(second.count("Monitor(command:"), 1, second)

    def test_A9_a_paused_seat_is_advised_not_refused(self):
        """A refusal would buy a turn the paused provider cannot serve, the
        inbox and owed rungs' own reason for skipping a walled seat."""
        self.seat_up()
        with mock.patch.object(seats_stop_guard, "_delivery_pause",
                               return_value={"state": "DARK"}):
            rc, _out, err = self.guard()
        self.assertEqual(rc, 0, err)
        self.assertIn("NO PROVEN WAKE PATH", err)
        self.assert_not_refused(err)


class BindingTest(LocalSeatBase):
    """Codex planning check 2: the strict probe proves a live `helm chat wait`
    PROCESS for the seat. It does not prove THIS session's followed beacon. A
    waiter binds to this stop only when it is a followed beacon (`--follow`,
    not `--any`), in this helm home, under this session. A waiter that does
    not bind is not coverage. A binding that cannot be resolved is UNKNOWN,
    and UNKNOWN stays advice."""

    def stop(self, root, live):
        with mock.patch.object(beacons, "live_sessions", return_value=live):
            return self.guard(probe=self.real_probe(root))

    def other_agent(self):
        """A live agent process that is NOT the one running this stop."""
        self.proc(8801, ["claude", "--dangerously-skip-permissions"],
                  comm="claude")
        return "CLAUDE_PID=8801"

    def test_C1_another_agents_live_waiter_does_not_cover_this_session(self):  # noqa: VACUOUS_ASSERTION — assert_refused asserts rc 2 and the refusal text PRESENT
        """A co-named waiter launched by another agent, under another session,
        is live and is somebody else's wake path."""
        self.seat_up()
        root = self.waiter(501, sid=OTHER, extra=[self.other_agent()])
        rc, _out, err = self.stop(root, {SID: 4242, OTHER: 4343})
        self.assert_refused(rc, err)

    def test_C2_a_waiter_that_is_not_a_beacon_does_not_cover(self):  # noqa: VACUOUS_ASSERTION — assert_refused asserts rc 2 and the refusal text PRESENT
        self.seat_up()
        for label, flags in (("single-shot", ()),
                             ("room-tap", ("--follow", "--any"))):
            with self.subTest(case=label):
                root = self.waiter(502, flags=flags)
                rc, _out, err = self.stop(root, {SID: 4242})
                shutil.rmtree(os.path.join(root, "502"))
                self.assert_refused(rc, err)

    def test_C3_this_sessions_followed_waiter_covers_it(self):  # noqa: VACUOUS_ASSERTION — C1 is the positive control: the same table, one field different
        self.seat_up()
        root = self.waiter(503)
        rc, _out, err = self.stop(root, {SID: 4242})
        self.assertEqual(rc, 0, err)
        self.assertNotIn("NO PROVEN WAKE PATH", err)
        self.assert_not_refused(err)

    def test_C4_an_unbindable_live_waiter_stays_advice(self):
        """Its home cannot be read, so whether it serves this seat's store is
        UNKNOWN: advice, never a refusal, and never today's silence either."""
        self.seat_up()
        root = self.waiter(504, home=False)
        rc, _out, err = self.stop(root, {SID: 4242})
        self.assertEqual(rc, 0, err)
        self.assertIn("NO PROVEN WAKE PATH", err)
        self.assertIn("could not be bound to this session", err)
        self.assert_not_refused(err)

    def test_C5_the_binding_answers_three_ways(self):
        """True binds; False is proven not this stop's beacon; None is
        UNKNOWN. Another session id is proof only when a DIFFERENT agent
        launched the waiter: the same agent under another id (a /clear, or an
        environ that did not follow a rename) and an agent nobody can name are
        both UNKNOWN, so neither can cause a refusal."""
        root = self.waiter(505)
        self.waiter(506, sid=OTHER)
        self.waiter(507, sid=OTHER, extra=["CLAUDE_PID=%s" % AGENT])
        self.waiter(508, sid=OTHER, extra=["CLAUDE_PID=99"])
        self.waiter(509, flags=("--follow", "--any"))
        self.waiter(510, sid=None)
        self.waiter(511, home=False)
        self.waiter(512, home=False, extra=["HELM_HOME=/zz/another-home"])
        self.waiter(513, flags=())
        binds = seats_stop_signals._beacon_binds
        with mock.patch.dict(os.environ, {"CLAUDE_PID": AGENT}):
            got = {pid: binds(pid, SID, root)[0]
                   for pid in (505, 506, 507, 508, 509, 510, 511, 512, 513)}
            self.assertEqual(got, {505: True, 506: None, 507: None,
                                   508: False, 509: False, 510: None,
                                   511: None, 512: False, 513: False})
            self.assertIsNone(binds(505, None, root)[0])
            self.assertIsNone(binds(999, SID, root)[0])     # gone mid-read
        with mock.patch.dict(os.environ, {}):
            os.environ.pop("CLAUDE_PID", None)
            self.assertTrue(binds(505, SID, root)[0])
            self.assertIsNone(binds(508, SID, root)[0])


class AttributionCaseTest(LocalSeatBase):
    """Codex planning check 3: `--seat` matching folds case, and the env leg
    compared raw bytes. A waiter named 'zz-case' in its environ read as
    absent for seat 'ZZ-Case', with no trouble. Under a refusal, that is a
    false block. The env leg now resolves the name the way the waiter itself
    does (HELM_CHAT_NAME, else MELD_CHAT_NAME) and folds case."""

    def test_C6_env_attribution_folds_case_like_the_seat_flag(self):
        root = self.waiter(601, name="zz-case", seat_flag=False,
                           env_name="zz-case")
        self.assertEqual(seats.beacon_procs("zz-case", root), ([601], None))
        self.assertEqual(seats.beacon_procs("ZZ-Case", root), ([601], None))
        with mock.patch.object(beacons, "live_sessions",
                               return_value={SID: 4242}):
            self.assertEqual(seats.beacon_procs("ZZ-Case", root, strict=True),
                             ([601], None))

    def test_C6_the_helm_name_outranks_the_legacy_one(self):
        root = self.waiter(602, seat_flag=False, env_name="zz-bob",
                           extra=["MELD_CHAT_NAME=zz-alpha"])
        self.assertEqual(seats.beacon_procs("zz-bob", root), ([602], None))
        self.assertEqual(seats.beacon_procs("zz-alpha", root), ([], None))

    def test_C7_a_case_variant_waiter_is_not_a_false_absence(self):  # noqa: VACUOUS_ASSERTION — C6 is the positive control (the probe finds this waiter); before the fold this stop printed the advice
        self.seat_up("ZZ-Case")
        root = self.waiter(603, name="zz-case", seat_flag=False,
                           env_name="zz-case")
        with mock.patch.object(beacons, "live_sessions",
                               return_value={SID: 4242}):
            rc, _out, err = self.guard(probe=self.real_probe(root))
        self.assertEqual(rc, 0, err)
        self.assertNotIn("NO PROVEN WAKE PATH", err)
        self.assert_not_refused(err, "ZZ-Case")


class RefusalCommandRearmsTest(tb.Base):
    """Codex planning check 4: a waiter that does not bind but is still live
    makes a plain re-arm exit (`already live`, or a `conflict`). The refusal's
    command must supersede it. Genuine waiter-shaped processes, a unique seat
    and a tmp HELM_HOME keep the live fleet out of reach."""

    spawn = tb.ReArmTest.spawn
    wait_visible = tb.ReArmTest.wait_visible
    dead = tb.ReArmTest.dead

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

    def test_the_refusal_command_rotates_a_stale_live_waiter(self):  # noqa: VACUOUS_ASSERTION — report['stopped'] == [stale.pid] and dead(stale) are the positive controls
        text = seats_stop_guard.rearm_refusal(self.seat)
        cmd = re.search(r'Monitor\(command: "([^"]+)"', text).group(1).split()
        self.assertEqual(cmd[:5], ["helm", "chat", "wait", "--seat", self.seat])
        self.assertIn("--follow", cmd)
        self.assertIn("--replace", cmd)
        stale = self.spawn(flags=["--any"])    # live, this session, not a beacon
        new = self.spawn()
        with mock.patch.object(beacons, "live_sessions",
                               return_value={tb.SID_A: os.getpid()}):
            plain = beacons.arm(self.seat, session=tb.SID_A, pid=new.pid)
            self.assertIsNotNone(plain["conflict"])
            self.assertIsNone(stale.poll(), "a plain arm rotated nothing")
            report = beacons.arm(self.seat, session=tb.SID_A, pid=new.pid,
                                 replace="--replace" in cmd)
        self.assertEqual(report["stopped"], [stale.pid])
        self.assertTrue(self.dead(stale), "the command left the stale waiter")
        self.assertIsNone(new.poll(), "the command killed its own waiter")


class RouteTest(unittest.TestCase):

    def test_the_beacon_missing_stop_route_is_live_and_expiry_stays(self):
        from helm import moments
        route = moments.BY_ID["stop.beacon-missing"]
        self.assertEqual(route.status, moments.LIVE)
        self.assertEqual(route.event, "Stop")
        self.assertIn("seats_stop_guard", route.form)
        self.assertEqual(moments.BY_ID["arrival.monitor-expired"].status,
                         moments.LIVE)


if __name__ == "__main__":
    unittest.main()
