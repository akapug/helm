"""No test reads this box's seat memory unless it asks to.

`seats_report.roster_report()` without `pressure=` takes its own reading
through `seatceiling.fleet_pressure()`, which walks the host's /proc and reads
the seat slices under /sys/fs/cgroup. Nothing in the suite stubbed it, so the
roster, web and scratch arms that build a report read the live host — the same
class as the unmocked `host_suspend_gap_s` reader that reddened
test_proxywatch on some hosts only. tests/__init__.py now declares the
production switch `HELM_SEAT_PRESSURE=off` for every test process and refuses,
through an audit hook, a host reading in a test process that lost that default.

THE CENSUS COUNTS THE WALK ITSELF: `seatceiling.slice_members` is wrapped and
called through, and a call on the host's tree is a walk of the host. It spells
the host's paths as literals, so this file runs unchanged against a tree that
has no switch and fails there on the count, not on a missing name.
"""

import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import tests
from helm import scratch, seatceiling, seats_report

HOST_CGROUP, HOST_PROC = "/sys/fs/cgroup", "/proc"
AGENTS = "user.slice/user-1000.slice/user@1000.service/agents.slice"
MB = 1024 * 1024


def _no_sleep(_s):
    return None


@contextlib.contextmanager
def host_walks():
    """[(root, proc)] for every membership walk of the host's tree made
    inside the block, and of every other tree under `.fixture`."""
    real = seatceiling.slice_members
    seen = []

    def walk(root=HOST_CGROUP, proc=HOST_PROC):
        seen.append((str(root), str(proc)))
        return real(root, proc)

    class Walks(list):
        @property
        def host(self):
            return [w for w in self
                    if os.path.normpath(w[0]) == HOST_CGROUP
                    or os.path.normpath(w[1]) == HOST_PROC]

        @property
        def fixture(self):
            return [w for w in self if w not in self.host]

    out = Walks()
    with mock.patch.object(seatceiling, "slice_members", walk):
        try:
            yield out
        finally:
            out.extend(seen)


def fixture_fleet(case):
    """(cgroup root, proc) for one THROTTLED seat: 104% of memory.high and a
    process stalled on the kernel's over-high wchan."""
    top = tempfile.mkdtemp(prefix="helm-pressure-fixture-")
    case.addCleanup(shutil.rmtree, top, True)
    cg, proc = os.path.join(top, "cgroup"), os.path.join(top, "proc")
    rel = "%s/%s" % (AGENTS, seatceiling.seat_slice_name("seat-under-test"))
    files = {
        os.path.join(cg, rel, "memory.current"): "%d\n" % (104 * MB),
        os.path.join(cg, rel, "memory.high"): "%d\n" % (100 * MB),
        os.path.join(cg, rel, "memory.stat"): "anon 4096\nshmem %d\n" % MB,
        os.path.join(cg, rel, "memory.events"): "low 0\nhigh 7\nmax 0\n",
        os.path.join(proc, "41", "cgroup"): "0::/%s/run-p41-i1.scope\n" % rel,
        os.path.join(proc, "41", "stat"): "41 (claude) D 1 0 0 0 0\n",
        os.path.join(proc, "41", "wchan"): "__mem_cgroup_handle_over_high",
    }
    os.makedirs(os.path.join(cg, rel, "run-p41-i1.scope"))
    for path, text in files.items():
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    return cg, proc


class NoTestWalksTheHostUnaskedTest(unittest.TestCase):
    """THE TRIPWIRE. Every surface that reads seat memory, driven the way the
    suite's arms drive it, walks the host zero times in a test process; and
    the same census, once the arm asks, counts the one walk it made."""

    def test_no_surface_walks_the_host_and_an_asking_arm_does(self):  # noqa: VACUOUS_ASSERTION — the empty host-walk list is paired, in this arm, with the same census counting exactly one walk once the arm sets `on`
        out = io.StringIO()
        with host_walks() as walks:
            seats_report.roster_report("main")
            with contextlib.redirect_stdout(out):
                seats_report.render_roster("main", True)
            plane = scratch.seat_plane()
            reading = seatceiling.fleet_pressure()
        self.assertEqual(
            walks.host, [],
            "%d walk(s) of this box's /proc and cgroup tree in a test process "
            "that never asked (roster_report, render_roster, "
            "scratch.seat_plane, fleet_pressure)" % len(walks.host))
        self.assertEqual(reading, ({}, None),
                         "the suite's reading is empty and answered")
        self.assertEqual((plane["slices"], plane["trouble"]), ({}, None))
        # POSITIVE CONTROL on the same census: an arm that ASKS walks the host
        # once, so the empty list above is a measurement, not a blind wrapper.
        with mock.patch.dict(os.environ, {"HELM_SEAT_PRESSURE": "on"}), \
                host_walks() as asked:
            got, trouble = seatceiling.fleet_pressure(sleep=_no_sleep)
        self.assertEqual(asked.host, [(HOST_CGROUP, HOST_PROC)])
        self.assertIsInstance(got, dict)

    def test_a_test_that_lost_the_switch_is_refused_before_the_walk(self):  # noqa: VACUOUS_ASSERTION — assertRaises is the positive observable, and the empty walk list is paired at the end of this arm with the same census counting one walk under `on`
        """`clear=True` or a pop drops the suite's `off`. Unset and empty are
        both the lost default, never a choice, and the refusal escapes the
        report's `except Exception` instead of reading as an empty cell."""
        for lost in (None, "", "  "):
            with self.subTest(value=lost), mock.patch.dict(os.environ), \
                    host_walks() as walks:
                os.environ.pop("HELM_SEAT_PRESSURE", None)
                if lost is not None:
                    os.environ["HELM_SEAT_PRESSURE"] = lost
                with self.assertRaises(tests.HostPressureRefused) as cm:
                    seatceiling.fleet_pressure(sleep=_no_sleep)
                self.assertIn("HELM_SEAT_PRESSURE", str(cm.exception))
                with self.assertRaises(tests.HostPressureRefused):
                    seats_report.roster_report("main")
                self.assertEqual(walks, [], "refused BEFORE the walk")
        # AN `off` THAT REACHES THE EVENT is a switch that stopped working,
        # and it is refused as well: the suite's own value is no ask.
        with self.assertRaises(tests.HostPressureRefused):
            sys.audit(seatceiling.HOST_READ_EVENT, HOST_CGROUP, HOST_PROC)
        with mock.patch.dict(os.environ, {"HELM_SEAT_PRESSURE": "on"}), \
                host_walks() as walks:
            seatceiling.fleet_pressure(sleep=_no_sleep)
        self.assertEqual(len(walks.host), 1,
                         "control: a SET switch is an ask, and is not refused")


class TheSwitchTest(unittest.TestCase):
    """`HELM_SEAT_PRESSURE` is a production value, and the suite's copy of the
    audit event name is the one seatceiling raises."""

    def test_the_suite_declares_off_and_names_the_event_seatceiling_raises(self):
        self.assertEqual(seatceiling.SWITCH, "HELM_SEAT_PRESSURE")
        self.assertEqual(os.environ.get(seatceiling.SWITCH), "off")
        self.assertEqual(tests.PLANTED[seatceiling.SWITCH], "off")
        self.assertEqual(tests.HOST_READ_EVENT, seatceiling.HOST_READ_EVENT)

    def test_off_0_and_no_are_off_in_any_case_and_anything_else_is_on(self):  # noqa: VACUOUS_ASSERTION — every case asserts an exact boolean with assertIs, and the True cases are the positive control for the False ones
        for value, on in (("off", False), (" OFF ", False), ("0", False),
                          ("No", False), ("on", True), ("1", True),
                          ("yes", True), ("", True)):
            with self.subTest(value=value), \
                    mock.patch.dict(os.environ, {seatceiling.SWITCH: value}):
                self.assertIs(seatceiling.host_read_on(), on)
        with mock.patch.dict(os.environ):
            os.environ.pop(seatceiling.SWITCH)
            self.assertIs(seatceiling.host_read_on(), True, "unset is on")

    def test_either_host_tree_is_the_host(self):
        self.assertTrue(seatceiling.reads_host(HOST_CGROUP, HOST_PROC))
        self.assertTrue(seatceiling.reads_host("/sys/fs/cgroup/", "/tmp/p"))
        self.assertTrue(seatceiling.reads_host("/tmp/cg", "/proc/"))
        self.assertFalse(seatceiling.reads_host("/tmp/cg", "/tmp/p"))


class AFixtureTreeRunsTheRealCodeTest(unittest.TestCase):
    """The switch answers the HOST's tree only. A tree an arm built is read
    by the real code under the suite's `off`, and half a host is the host."""

    def test_a_fixture_is_read_and_half_a_host_is_not(self):
        cg, proc = fixture_fleet(self)
        with host_walks() as walks:
            got, trouble = seatceiling.fleet_pressure(root=cg, proc=proc,
                                                      sleep=_no_sleep)
            half = (seatceiling.fleet_pressure(root=cg, sleep=_no_sleep),
                    seatceiling.fleet_pressure(proc=proc, sleep=_no_sleep))
        self.assertIsNone(trouble)
        self.assertEqual(
            {os.path.basename(p): r.word for p, r in got.items()},
            {seatceiling.seat_slice_name("seat-under-test"):
             seatceiling.THROTTLED})
        self.assertEqual(walks.fixture, [(cg, proc)])
        self.assertEqual(half, (({}, None), ({}, None)),
                         "a reading with one host tree is the host's reading")
        self.assertEqual(walks.host, [])


if __name__ == "__main__":
    unittest.main()
