#!/usr/bin/env python3
"""The one attached-process owner shared by every Helm launch surface."""
import os
import signal
import sys
import unittest
from unittest import mock

from helm import seat_launch_owner


class LaunchOwnerTest(unittest.TestCase):
    def test_headless_launch_direct_execs_with_the_exact_environment(self):
        argv = ["/usr/bin/tool", "--flag", "value"]
        env = {"PATH": "/usr/bin", "MARKER": "owned"}
        with mock.patch.object(seat_launch_owner.os, "isatty", return_value=False), \
                mock.patch.object(seat_launch_owner.os, "execvpe") as execvpe:
            rc = seat_launch_owner.exec_attached(argv, env)
        self.assertIsNone(rc)
        execvpe.assert_called_once_with(argv[0], argv, env)
        self.assertEqual(argv[-1], "value")
        self.assertEqual(env["MARKER"], "owned")

    def test_headless_launch_without_an_environment_uses_execvp(self):
        argv = ["/usr/bin/tool", "probe"]
        with mock.patch.object(seat_launch_owner.os, "isatty", return_value=False), \
                mock.patch.object(seat_launch_owner.os, "execvp") as execvp:
            rc = seat_launch_owner.exec_attached(argv)
        self.assertIsNone(rc)
        execvp.assert_called_once_with(argv[0], argv)
        self.assertEqual(argv, ["/usr/bin/tool", "probe"])

    def test_headless_exec_setup_failure_is_an_owned_127(self):
        argv = ["bad\x00command"]
        with mock.patch.object(seat_launch_owner.os, "isatty", return_value=False), \
                mock.patch.object(seat_launch_owner.os, "execvp",
                                  side_effect=ValueError("embedded null byte")):
            rc = seat_launch_owner.exec_attached(argv)
        self.assertEqual(rc, 127)
        self.assertEqual(argv, ["bad\x00command"])

    def test_headless_keyboard_interrupt_is_not_flattened_to_127(self):  # noqa: VACUOUS_ASSERTION — the exact positive outcome is propagation of KeyboardInterrupt; the exec call proves the exception came from the owned boundary
        argv = ["/usr/bin/tool"]
        with mock.patch.object(seat_launch_owner.os, "isatty", return_value=False), \
                mock.patch.object(seat_launch_owner.os, "execvp",
                                  side_effect=KeyboardInterrupt) as execvp:
            with self.assertRaises(KeyboardInterrupt):
                seat_launch_owner.exec_attached(argv)
        execvp.assert_called_once_with(argv[0], argv)

    def test_tty_launch_delegates_the_exact_child_to_the_wait_status_owner(self):
        argv = ["/usr/bin/tool", "--", "caller-value"]
        env = {"MARKER": "owned"}
        with mock.patch.object(seat_launch_owner.os, "isatty", return_value=True), \
                mock.patch.object(seat_launch_owner, "run", return_value=23) as run:
            rc = seat_launch_owner.exec_attached(argv, env, "/work")
        self.assertEqual(rc, 23)
        run.assert_called_once_with(argv, env, "/work")
        self.assertEqual(argv[-1], "caller-value")
        self.assertEqual(env["MARKER"], "owned")

    def test_terminal_foreground_returns_to_the_calling_group_before_reset(self):
        events = []
        status = 0 << 8
        with mock.patch.object(seat_launch_owner.os, "isatty",
                               side_effect=lambda fd: fd == 1), \
                mock.patch.object(seat_launch_owner.os, "tcgetpgrp",
                                  return_value=321), \
                mock.patch.object(seat_launch_owner.os, "fork", return_value=654), \
                mock.patch.object(seat_launch_owner.os, "setpgid"), \
                mock.patch.object(seat_launch_owner.os, "tcsetpgrp",
                                  side_effect=lambda fd, pgid:
                                  events.append(("foreground", fd, pgid))), \
                mock.patch.object(seat_launch_owner.os, "waitpid",
                                  return_value=(654, status)), \
                mock.patch.object(seat_launch_owner, "_drain_group",
                                  return_value=True), \
                mock.patch.object(seat_launch_owner, "_drain_descendants",
                                  return_value=True), \
                mock.patch.object(seat_launch_owner, "_subreaper",
                                  return_value=False), \
                mock.patch.object(seat_launch_owner, "_disarm",
                                  side_effect=lambda _done:
                                  events.append(("disarm",))), \
                mock.patch.object(seat_launch_owner.signal, "signal"), \
                mock.patch.object(seat_launch_owner.signal, "getsignal",
                                  return_value=signal.SIG_DFL), \
                mock.patch.object(seat_launch_owner.signal, "pthread_sigmask",
                                  return_value=set()):
            rc = seat_launch_owner.run([sys.executable, "-c", "pass"])
        self.assertEqual(rc, 0)
        self.assertEqual(events, [("foreground", 1, 654),
                                  ("foreground", 1, 321),
                                  ("disarm",)])

    def test_terminal_foreground_restore_failure_is_owned_and_skips_reset(self):  # noqa: VACUOUS_ASSERTION — rc 125 positively proves the restore failure path; no disarm proves reset is skipped
        status = 0 << 8
        with mock.patch.object(seat_launch_owner.os, "isatty",
                               side_effect=lambda fd: fd == 1), \
                mock.patch.object(seat_launch_owner.os, "tcgetpgrp",
                                  return_value=321), \
                mock.patch.object(seat_launch_owner.os, "fork", return_value=654), \
                mock.patch.object(seat_launch_owner.os, "setpgid"), \
                mock.patch.object(seat_launch_owner.os, "tcsetpgrp",
                                  side_effect=[None, OSError("lost tty")]), \
                mock.patch.object(seat_launch_owner.os, "waitpid",
                                  return_value=(654, status)), \
                mock.patch.object(seat_launch_owner, "_drain_group",
                                  return_value=True), \
                mock.patch.object(seat_launch_owner, "_drain_descendants",
                                  return_value=True), \
                mock.patch.object(seat_launch_owner, "_subreaper",
                                  return_value=False), \
                mock.patch.object(seat_launch_owner, "_disarm") as disarm, \
                mock.patch.object(seat_launch_owner.signal, "signal"), \
                mock.patch.object(seat_launch_owner.signal, "getsignal",
                                  return_value=signal.SIG_DFL), \
                mock.patch.object(seat_launch_owner.signal, "pthread_sigmask",
                                  return_value=set()):
            rc = seat_launch_owner.run([sys.executable, "-c", "pass"])
        self.assertEqual(rc, 125)
        disarm.assert_not_called()

    def test_terminal_foreground_query_failure_refuses_before_fork(self):  # noqa: VACUOUS_ASSERTION — rc 125 positively proves query refusal; untouched fork/subreaper prove refusal ordering
        with mock.patch.object(seat_launch_owner.os, "isatty",
                               side_effect=lambda fd: fd == 1), \
                mock.patch.object(seat_launch_owner.os, "tcgetpgrp",
                                  side_effect=OSError("not controlling tty")), \
                mock.patch.object(seat_launch_owner.os, "fork") as fork, \
                mock.patch.object(seat_launch_owner, "_subreaper") as subreaper:
            rc = seat_launch_owner.run([sys.executable, "-c", "pass"])
        self.assertEqual(rc, 125)
        fork.assert_not_called()
        subreaper.assert_not_called()

    def test_missing_command_refuses_before_touching_the_terminal(self):  # noqa: VACUOUS_ASSERTION — status 2 is the positive refusal result; untouched isatty proves refusal precedes terminal ownership
        with mock.patch.object(seat_launch_owner.os, "isatty") as isatty:
            rc = seat_launch_owner.exec_attached([])
        self.assertEqual(rc, 2)
        isatty.assert_not_called()


if __name__ == "__main__":
    unittest.main()
