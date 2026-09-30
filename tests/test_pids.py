#!/usr/bin/env python3
"""The one proof the shared dead pid is above the ceiling.

`DEAD_PID` is a promise: a test that plants it can never lose to a live
process, whatever the box runs with. This test pins that promise against
the kernel's own ceiling, so a future edit that lowers the constant fails
here instead of surfacing as a flake somewhere in the suite.
"""
import os
import unittest

from tests import _pids


class DeadPidCeilingTest(unittest.TestCase):
    def test_DEAD_PID_is_above_pid_max(self):
        try:
            with open("/proc/sys/kernel/pid_max", encoding="utf-8") as fh:
                pid_max = int(fh.read().strip())
        except OSError as err:
            self.skipTest("cannot read /proc/sys/kernel/pid_max here: %s" % err)
        self.assertGreater(_pids.DEAD_PID, pid_max,
                           "DEAD_PID %d is not above pid_max %d; a planted dead "
                           "pid can be a live process on this box"
                           % (_pids.DEAD_PID, pid_max))
