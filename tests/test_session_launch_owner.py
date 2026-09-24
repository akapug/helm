#!/usr/bin/env python3
import contextlib
import fcntl
import os
import pty
import re
import select
import shlex
import signal
import subprocess
import sys
import tempfile
import termios
import time
import unittest

from helm import seat_launch_owner, session


class SessionLaunchOwnerTest(unittest.TestCase):
    def _cv(self, root, command):
        path = os.path.join(root, "cv")
        with open(path, "w") as f:
            f.write("#!/usr/bin/env python3\n")
            f.write("import sys\n")
            f.write("assert sys.argv[1:] == ['resume', 'sid-a']\n")
            f.write("print(%r)\n" % command)
        os.chmod(path, 0o700)
        return path

    @contextlib.contextmanager
    def _owner(self, cv, handoff_arm=None):
        root = os.path.dirname(os.path.dirname(os.path.abspath(session.__file__)))
        code = """
import os
import sys
import time

sys.path.insert(0, sys.argv[1])
from helm import seat_launch_owner, session
expected_session, expected_owner = sys.argv[2:4]
if os.path.realpath(session.__file__) != os.path.realpath(expected_session):
    print('CONTROL:WRONG-SESSION:%s' % session.__file__,
          file=sys.stderr, flush=True)
    raise SystemExit(92)
if os.path.realpath(seat_launch_owner.__file__) != os.path.realpath(expected_owner):
    print('CONTROL:WRONG-OWNER:%s' % seat_launch_owner.__file__,
          file=sys.stderr, flush=True)
    raise SystemExit(93)
print('CONTROL:EXACT-MODULES:%s:%s' %
      (session.__file__, seat_launch_owner.__file__),
      file=sys.stderr, flush=True)
arm = sys.argv[5]
if arm:
    seat_launch_owner.os.tcgetpgrp = lambda _fd: os.getpgrp()
    def controlled_handoff(fd, pgid):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not os.path.exists(arm):
            time.sleep(0.01)
        if not os.path.exists(arm):
            raise OSError('controlled handoff arm timed out')
        raise OSError('controlled handoff failure')
    seat_launch_owner._tty_foreground = controlled_handoff
session.CV = sys.argv[4]
rc, err = session._cv_launch('sid-a')
if err:
    print(err, file=sys.stderr, flush=True)
raise SystemExit(rc)
"""
        master, slave = pty.openpty()
        proc = None
        def control_tty():
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

        try:
            proc = subprocess.Popen(
                [sys.executable, "-I", "-c", code, root,
                 session.__file__, seat_launch_owner.__file__, cv,
                 handoff_arm or ""],
                stdin=slave, stdout=slave, stderr=subprocess.PIPE,
                close_fds=True, preexec_fn=control_tty)
        except OSError:
            os.close(master)
            raise
        finally:
            os.close(slave)
        cleanup_pids = set()
        try:
            yield proc, master, cleanup_pids
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            for pid in cleanup_pids:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    continue
            for _ in range(50):
                if not any(os.path.exists("/proc/%d" % pid)
                           for pid in cleanup_pids):
                    break
                time.sleep(0.01)
            for pid in cleanup_pids:
                if os.path.exists("/proc/%d" % pid):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            if not proc.stderr.closed:
                proc.stderr.close()
            os.close(master)

    def _read_until(self, fd, marker=None, timeout=8):
        out, end = bytearray(), time.time() + timeout
        while time.time() < end:
            ready, _, _ = select.select([fd], [], [], 0.05)
            if not ready:
                continue
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            out.extend(chunk)
            if marker is not None and marker in out:
                break
        return bytes(out)

    def _require_marker(self, proc, master, marker):
        output = self._read_until(master, marker)
        if marker in output:
            return output
        rc = proc.poll()
        err = proc.stderr.read().decode("utf-8", "replace") \
            if rc is not None else "<helper still running>"
        self.fail("launch owner helper died/stalled before %r: rc=%r "
                  "session=%r owner=%r output=%r stderr=%r"
                  % (marker, rc, session.__file__, seat_launch_owner.__file__,
                     output, err))

    def test_cv_resolves_then_owner_preserves_the_harness_nonzero(self):
        with tempfile.TemporaryDirectory(prefix="helm-test-cv-owner-") as d:
            harness = os.path.join(d, "harness.py")
            with open(harness, "w") as f:
                f.write("import sys\nprint('HARNESS-RAN', flush=True)\n"
                        "raise SystemExit(23)\n")
            command = "%s %s" % (shlex.quote(sys.executable),
                                   shlex.quote(harness))
            with self._owner(self._cv(d, command)) as (proc, master, _cleanup):
                output = self._require_marker(proc, master, b"HARNESS-RAN")
                output += self._read_until(master)
                rc = proc.wait(timeout=5)
                err = proc.stderr.read().decode("utf-8", "replace")
                self.assertEqual(rc, 23, err)
                self.assertIn("CONTROL:EXACT-MODULES:", err)
                self.assertEqual(output.count(b"HARNESS-RAN"), 1)
                self.assertEqual(
                    output.count(seat_launch_owner.TERMINAL_DISARM), 1)

    def test_wrapper_term_drains_descendants_before_terminal_cleanup(self):  # noqa: VACUOUS_ASSERTION — descendant exit marker/order and wrapper SIGTERM positively control process absence
        with tempfile.TemporaryDirectory(prefix="helm-test-cv-tree-") as d:
            child = os.path.join(d, "child.py")
            with open(child, "w") as f:
                f.write("import os,signal,sys,time\n"
                        "def stop(*_):\n"
                        " print('DESCENDANT-GONE', flush=True)\n"
                        " raise SystemExit(0)\n"
                        "signal.signal(signal.SIGTERM, stop)\n"
                        "print('DESCENDANT-READY:%d:ARMED' % os.getpid(), flush=True)\n"
                        "time.sleep(60)\n")
            harness = os.path.join(d, "harness.py")
            with open(harness, "w") as f:
                f.write("import subprocess,sys,time,os\n"
                        "p=subprocess.Popen([sys.executable, %r])\n"
                        "print('HARNESS-READY:%%d' %% p.pid, flush=True)\n"
                        "time.sleep(60)\n" % child)
            command = "%s %s" % (shlex.quote(sys.executable),
                                   shlex.quote(harness))
            with self._owner(self._cv(d, command)) as (
                    proc, master, cleanup_pids):
                before = self._require_marker(proc, master, b":ARMED")
                match = re.search(rb"DESCENDANT-READY:(\d+):ARMED", before)
                self.assertIsNotNone(match, before)
                descendant = int(match.group(1))
                cleanup_pids.add(descendant)
                os.kill(proc.pid, signal.SIGTERM)
                after = self._require_marker(
                    proc, master, seat_launch_owner.TERMINAL_DISARM)
                with self.assertRaises(ProcessLookupError):
                    os.kill(descendant, 0)
                cleanup_pids.discard(descendant)
                after += self._read_until(master)
                rc = proc.wait(timeout=8)
                err = proc.stderr.read().decode("utf-8", "replace")
                output = before + after
                self.assertIn("CONTROL:EXACT-MODULES:", err)
                self.assertEqual(rc, -signal.SIGTERM, err)
                self.assertIn(b"DESCENDANT-GONE", output)
                self.assertLess(
                    output.index(b"DESCENDANT-GONE"),
                    output.index(seat_launch_owner.TERMINAL_DISARM))
                with self.assertRaises(ProcessLookupError):
                    os.kill(descendant, 0)

    def test_wrapper_term_drains_descendant_that_escaped_process_group(self):  # noqa: VACUOUS_ASSERTION — escaped exit marker/order and wrapper SIGTERM positively control process absence
        with tempfile.TemporaryDirectory(prefix="helm-test-cv-escaped-") as d:
            child = os.path.join(d, "child.py")
            with open(child, "w") as f:
                f.write("import os,signal,time\n"
                        "def stop(*_):\n"
                        " print('ESCAPED-GONE', flush=True)\n"
                        " raise SystemExit(0)\n"
                        "signal.signal(signal.SIGTERM, stop)\n"
                        "print('ESCAPED-ARMED:%d' % os.getpid(), flush=True)\n"
                        "time.sleep(60)\n")
            harness = os.path.join(d, "harness.py")
            with open(harness, "w") as f:
                f.write("import signal,subprocess,sys,time\n"
                        "p=subprocess.Popen([sys.executable, %r], "
                        "start_new_session=True)\n"
                        "def stop(*_): raise SystemExit(0)\n"
                        "signal.signal(signal.SIGTERM, stop)\n"
                        "time.sleep(60)\n" % child)
            command = "%s %s" % (shlex.quote(sys.executable),
                                   shlex.quote(harness))
            with self._owner(self._cv(d, command)) as (
                    proc, master, cleanup_pids):
                before = self._require_marker(proc, master, b"ESCAPED-ARMED:")
                match = re.search(rb"ESCAPED-ARMED:(\d+)", before)
                self.assertIsNotNone(match, before)
                escaped = int(match.group(1))
                cleanup_pids.add(escaped)
                os.kill(proc.pid, signal.SIGTERM)
                after = self._require_marker(
                    proc, master, seat_launch_owner.TERMINAL_DISARM)
                rc = proc.wait(timeout=8)
                err = proc.stderr.read().decode("utf-8", "replace")
                output = before + after
                self.assertEqual(rc, -signal.SIGTERM, err)
                self.assertIn(b"ESCAPED-GONE", output)
                self.assertLess(output.index(b"ESCAPED-GONE"),
                                output.index(seat_launch_owner.TERMINAL_DISARM))
                with self.assertRaises(ProcessLookupError):
                    os.kill(escaped, 0)
                cleanup_pids.discard(escaped)

    def test_wrapper_term_escalates_a_direct_harness_that_ignores_term(self):  # noqa: VACUOUS_ASSERTION — escalation text, wrapper SIGTERM and one disarm positively control hostile-process absence
        with tempfile.TemporaryDirectory(prefix="helm-test-cv-hostile-") as d:
            harness = os.path.join(d, "harness.py")
            with open(harness, "w") as f:
                f.write("import os,signal,time\n"
                        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                        "print('HOSTILE-ARMED:%d' % os.getpid(), flush=True)\n"
                        "time.sleep(60)\n")
            command = "%s %s" % (shlex.quote(sys.executable),
                                   shlex.quote(harness))
            with self._owner(self._cv(d, command)) as (
                    proc, master, cleanup_pids):
                before = self._require_marker(proc, master, b"HOSTILE-ARMED:")
                match = re.search(rb"HOSTILE-ARMED:(\d+)", before)
                self.assertIsNotNone(match, before)
                harness_pid = int(match.group(1))
                cleanup_pids.add(harness_pid)
                os.kill(proc.pid, signal.SIGTERM)
                after = self._require_marker(
                    proc, master, seat_launch_owner.TERMINAL_DISARM)
                rc = proc.wait(timeout=8)
                err = proc.stderr.read().decode("utf-8", "replace")
                self.assertEqual(rc, -signal.SIGTERM, err)
                self.assertIn("ignored termination; escalating to SIGKILL", err)
                self.assertEqual(
                    (before + after).count(seat_launch_owner.TERMINAL_DISARM), 1)
                with self.assertRaises(ProcessLookupError):
                    os.kill(harness_pid, 0)
                cleanup_pids.discard(harness_pid)

    def test_handoff_failure_escalates_only_after_hostile_harness_arms(self):  # noqa: VACUOUS_ASSERTION — armed marker, handoff rc/text and escalation positively control no disarm/process absence
        with tempfile.TemporaryDirectory(prefix="helm-test-cv-handoff-") as d:
            arm = os.path.join(d, "armed")
            harness = os.path.join(d, "harness.py")
            with open(harness, "w") as f:
                f.write("import os,signal,time\n"
                        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                        "print('HANDOFF-HOSTILE-ARMED:%%d' %% os.getpid(), "
                        "flush=True)\n"
                        "open(%r, 'w').close()\n"
                        "time.sleep(60)\n" % arm)
            command = "%s %s" % (shlex.quote(sys.executable),
                                   shlex.quote(harness))
            with self._owner(self._cv(d, command), arm) as (
                    proc, master, cleanup_pids):
                output = self._require_marker(
                    proc, master, b"HANDOFF-HOSTILE-ARMED:")
                match = re.search(rb"HANDOFF-HOSTILE-ARMED:(\d+)", output)
                self.assertIsNotNone(match, output)
                harness_pid = int(match.group(1))
                cleanup_pids.add(harness_pid)
                output += self._read_until(master)
                rc = proc.wait(timeout=8)
                err = proc.stderr.read().decode("utf-8", "replace")
                self.assertEqual(rc, 125, err)
                self.assertIn("terminal foreground handoff failed: "
                              "controlled handoff failure", err)
                self.assertIn("ignored termination; escalating to SIGKILL", err)
                self.assertNotIn(seat_launch_owner.TERMINAL_DISARM, output)
                with self.assertRaises(ProcessLookupError):
                    os.kill(harness_pid, 0)
                cleanup_pids.discard(harness_pid)


if __name__ == "__main__":
    unittest.main()
