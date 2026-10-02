import hashlib
import json
import os
import shutil
import tempfile
import unittest
from helm import fabgate, gatehost


def finished_event(host, seconds, scope, key, exit_code=0, state="COMPLETED"):
    snap = {
        "state": state,
        "exit": exit_code,
        "execution_elapsed_s": seconds,
        "exit_class": "OK" if exit_code == 0 else "FAILED",
    }
    return {
        "v": 2,
        "event": "gate-job",
        "key": key,
        "job_id": "gate-" + key,
        "node": host,
        "identity": {"scope": scope, "tree": "a" * 40},
        "snapshot": snap,
    }


class ReadCapacityTupleSeamTest(unittest.TestCase):
    """task/3923: gatewindow's fab seam answers (rc, stdout, stderr)."""

    def test_a_zero_rc_tuple_is_parsed(self):
        got = gatehost.read_capacity(runner=lambda argv: (0, '{"v": 1, "nodes": []}', ""))
        self.assertEqual(got, {"v": 1, "nodes": []})

    def test_a_failing_or_unrunnable_tuple_is_unknown(self):
        for res in ((1, "", "boom"), (None, "", "OSError: no fab"), (0, "not json", "")):
            with self.subTest(res=res):
                self.assertIsNone(gatehost.read_capacity(runner=lambda argv, r=res: r))


class TestGateHostCapacity(unittest.TestCase):
    """task/3923: gatehost reads fab capacity and routes around busy hosts."""

    BUSY = "busybox"
    IDLE = "idlebox"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-gatehost-cap-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.planted = 0

    def plant(self, host, seconds, scope=None, count=3):
        scope = scope or fabgate.whole_scope()
        for _ in range(count):
            self.planted += 1
            key = hashlib.sha256(("%s %d" % (host, self.planted)).encode()).hexdigest()
            path = os.path.join(self.tmp, "gate-%s.log" % key)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(finished_event(host, seconds, scope, key)) + "\n")
            os.utime(path, (1000 + self.planted, 1000 + self.planted))

    def test_train521_busy_host_picks_idle_unmeasured_host(self):
        """train521: busy host has 28 of 30 cores held and work queued ahead,
        while idle host has no measured gate logs but is completely free with 36 cores.
        Plan routes to idle host because the busy host's start delay exceeds the
        unmeasured whole-suite estimate."""
        # busy host has measured gates (~1100 s / 18.3 min)
        self.plant(self.BUSY, 1100.0)
        # idle host has no gate logs planted
        capacity = {
            "v": 1,
            "nodes": [
                {
                    "host": self.BUSY,
                    "state": "UP",
                    "budget_cores": 30,
                    "free": {"cores": 2, "slots": 1},
                    "running": {"cores": 28, "jobs": 2, "slots": 2},
                    "queued": [
                        {"id": "gate-p0-1", "priority": "p0", "cores": 16, "slots": 1, "waited_s": 60.0},
                        {"id": "detached-p0-2", "priority": "p0", "cores": 12, "slots": 1, "waited_s": 30.0},
                    ],
                    "queued_cores": 28,
                },
                {
                    "host": self.IDLE,
                    "state": "UP",
                    "budget_cores": 36,
                    "free": {"cores": 36, "slots": 12},
                    "running": {"cores": 0, "jobs": 0, "slots": 0},
                    "queued": [],
                    "queued_cores": 0,
                },
            ],
        }
        environ = {"HELM_GATE_HOSTS": "%s %s" % (self.BUSY, self.IDLE)}
        route = gatehost.plan(
            gatehost.SERIAL, [], self.tmp, environ=environ, now=100.0, capacity=capacity
        )
        self.assertEqual(route["host"], self.IDLE)
        self.assertIn(self.BUSY, route["exclude"])
        self.assertNotIn(self.IDLE, route["exclude"])
        # The unmeasured estimate is cited with source
        lines = "\n".join(route["lines"])
        self.assertIn(self.IDLE, lines)
        self.assertIn("est ~", lines)

    def test_idle_measured_host_wins_over_idle_unmeasured_host(self):
        """When the measured host is idle, it finishes first and is chosen
        over an unmeasured candidate."""
        self.plant(self.BUSY, 1100.0)
        capacity = {
            "v": 1,
            "nodes": [
                {
                    "host": self.BUSY,
                    "state": "UP",
                    "budget_cores": 30,
                    "free": {"cores": 30, "slots": 12},
                    "running": {"cores": 0, "jobs": 0, "slots": 0},
                    "queued": [],
                    "queued_cores": 0,
                },
                {
                    "host": self.IDLE,
                    "state": "UP",
                    "budget_cores": 36,
                    "free": {"cores": 36, "slots": 12},
                    "running": {"cores": 0, "jobs": 0, "slots": 0},
                    "queued": [],
                    "queued_cores": 0,
                },
            ],
        }
        environ = {"HELM_GATE_HOSTS": "%s %s" % (self.BUSY, self.IDLE)}
        route = gatehost.plan(
            gatehost.SERIAL, [], self.tmp, environ=environ, now=100.0, capacity=capacity
        )
        self.assertEqual(route["host"], self.BUSY)
        self.assertIn(self.IDLE, route["exclude"])
        self.assertNotIn(self.BUSY, route["exclude"])

    def test_unreadable_capacity_falls_back_to_today_and_reports_unknown(self):  # noqa: VACUOUS_ASSERTION — asserts presence of UNKNOWN in lines and absence of free
        """When fab capacity cannot be read, the old rule stands and the
        plan table reports UNKNOWN for capacity, never 'free'."""
        self.plant(self.BUSY, 1100.0)
        environ = {"HELM_GATE_HOSTS": "%s %s" % (self.BUSY, self.IDLE)}
        # Passing capacity=None with a failing capacity_runner simulates unreadable capacity
        def failing_runner(*_a, **_kw):
            return None

        route = gatehost.plan(
            gatehost.SERIAL,
            [],
            self.tmp,
            environ=environ,
            now=100.0,
            capacity_runner=failing_runner,
        )
        self.assertEqual(route["host"], self.BUSY)
        lines = "\n".join(route["lines"])
        self.assertIn("capacity UNKNOWN", lines)
        self.assertNotIn("free", lines.lower())

    def test_chosen_host_is_pinned_for_submit(self):
        """The chosen host is excluded from the exclude list, pinning it for
        fab submit while excluding all other candidates."""
        self.plant("boxA", 1000.0)
        self.plant("boxB", 1500.0)
        capacity = {
            "v": 1,
            "nodes": [
                {
                    "host": "boxA",
                    "state": "UP",
                    "budget_cores": 30,
                    "free": {"cores": 30, "slots": 12},
                    "running": {"cores": 0, "jobs": 0, "slots": 0},
                    "queued": [],
                    "queued_cores": 0,
                },
                {
                    "host": "boxB",
                    "state": "UP",
                    "budget_cores": 30,
                    "free": {"cores": 30, "slots": 12},
                    "running": {"cores": 0, "jobs": 0, "slots": 0},
                    "queued": [],
                    "queued_cores": 0,
                },
            ],
        }
        environ = {"HELM_GATE_HOSTS": "boxA boxB"}
        route = gatehost.plan(
            gatehost.SERIAL, [], self.tmp, environ=environ, now=100.0, capacity=capacity
        )
        self.assertEqual(route["host"], "boxA")
        self.assertEqual(route["exclude"], ["boxB"])


if __name__ == "__main__":
    unittest.main()
