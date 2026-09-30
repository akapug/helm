import os
import subprocess
import tempfile
import unittest

# dregg-node-snapshot: SIGSTOP the node around the rsync so the disk copy is
# crash-consistent, not a half-written live redb. Hermetic: no real systemd or
# node. systemctl and rsync are stubbed on PATH; the node is a plain `sleep 60`
# child whose /proc/<pid>/stat state we read.

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(ROOT, "scripts", "dregg-node-snapshot")


class TestDreggNodeSnapshot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.tmfs = os.path.join(self.tmp, "tmfs")
        os.makedirs(self.tmfs)
        self.disk = os.path.join(self.tmp, "disk")
        os.makedirs(self.disk)
        self.unit = "dregg-cave.service"
        with open(os.path.join(self.tmfs, "node.key"), "w") as f:
            f.write("k")
        with open(os.path.join(self.tmfs, "dregg.redb"), "w") as f:
            f.write("data")
        self.proc = None
        self.env = dict(os.environ)
        self.env["TMPFS_DIR"] = self.tmfs
        self.env["NODE_DATA"] = self.tmfs
        self.env["CHILD_PID"] = ""

    def tearDown(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _node(self):
        self.proc = subprocess.Popen(["sleep", "60"])
        self.env["CHILD_PID"] = str(self.proc.pid)
        return self.proc.pid

    def _state(self, pid):
        # field 3 of /proc/<pid>/stat is the state letter. comm may be
        # wrapped in parens with spaces inside, so split off the last ')'
        # and take the next whitespace-delimited field.
        with open("/proc/%d/stat" % pid) as f:
            body = f.read()
        after = body[body.rindex(")") + 1:].split()
        return after[0] if after else ""

    def _run(self, *, pid, fail=False, hang=False, timeout=None):
        """Run the script with systemctl (prints MainPID) and rsync stubbed on
        a private bin dir. The rsync stub reads the child's /proc state in a
        short loop until it is actually SIGSTOPped (so the stub does not race
        the kernel), records that state in $STATEFILE, and exits 0 or 1 by
        fail. Returns (script_returncode, observed_state)."""
        bin = os.path.join(self.tmp, "bin")
        os.makedirs(bin)
        with open(os.path.join(bin, "systemctl"), "w") as f:
            f.write("#!/usr/bin/env bash\n")
            f.write(
                'while [ $# -gt 0 ]; do '
                'if [ "$1" = "MainPID" ]; then echo "%s"; fi; '
                'shift; done\n' % str(pid)
            )
        os.chmod(os.path.join(bin, "systemctl"), 0o755)
        rsync = os.path.join(bin, "rsync")
        with open(rsync, "w") as f:
            f.write("#!/usr/bin/env bash\n")
            f.write(
                "pid=$CHILD_PID\n"
                "state=run\n"
                "for _ in $(seq 1 20); do\n"
                "  tail=$(sed 's/.*([^)]*) //' /proc/$pid/stat 2>/dev/null | cut -d' ' -f1)\n"
                "  if [ -n \"$tail\" ]; then state=$tail; fi\n"
                "  if [ \"$tail\" = \"T\" ]; then break; fi\n"
                "  sleep 0.01\n"
                "done\n"
                "echo $state > $STATEFILE\n"
                'if [ "%s" = "1" ]; then sleep 30; fi\n'
                'if [ "%s" = "1" ]; then exit 1; fi\n'
                % ("1" if hang else "0", "1" if fail else "0")
            )
        os.chmod(rsync, 0o755)
        cmd = [SCRIPT, self.unit, self.tmfs, self.disk]
        e = dict(self.env)
        e["PATH"] = bin + ":" + self.env["PATH"]
        e["STATEFILE"] = os.path.join(self.tmp, "state")
        if timeout is not None:
            e["DREGG_SNAPSHOT_TIMEOUT"] = str(timeout)
        r = subprocess.run(cmd, env=e, capture_output=True, text=True,
                           timeout=20)
        with open(e["STATEFILE"]) as f:
            observed_state = f.read().strip()
        return r.returncode, observed_state

    def test_no_node_key_exits_zero_no_copy(self):
        os.remove(os.path.join(self.tmfs, "node.key"))
        self.proc = subprocess.Popen(["sleep", "60"])
        self.addCleanup(lambda: self.proc.kill())
        e = dict(self.env)
        e["PATH"] = os.path.join(self.tmp, "bin") + ":" + e["PATH"]
        out = subprocess.run([SCRIPT, self.unit, self.tmfs, self.disk],
                             env=e, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0)
        self.assertNotIn("dregg.redb", os.listdir(self.disk))

    def test_running_node_stopped_around_copy_then_continued(self):
        self._node()
        # positive control: the child is running, not stopped. A freshly
        # spawned sleep may read as R or S, but never T (T is the stopped
        # state this script produces), so the control is the negation of T.
        self.assertIsNone(self.proc.poll())
        self.assertNotEqual(self._state(self.proc.pid), "T")
        # the rsync stub records the node's state DURING the copy: while the
        # script runs the node is SIGSTOPped (T); the script then SIGCONT's it.
        rc, observed = self._run(pid=self.proc.pid)
        self.assertEqual(rc, 0)
        self.assertEqual(observed, "T")
        self.assertIsNone(self.proc.poll())
        # alive is not enough: a SIGSTOPped process is alive too. It must be running again.
        self.assertNotEqual(self._state(self.proc.pid), "T", "the node was left stopped")

    def test_rsync_failure_exits_nonzero_node_not_stopped(self):
        self._node()
        rc, observed = self._run(pid=self.proc.pid, fail=True)
        self.assertNotEqual(rc, 0)
        # the copy failed mid-way, but the trap resumed the node before exit.
        self.assertEqual(observed, "T")
        self.assertIsNone(self.proc.poll())
        self.assertNotEqual(self._state(self.proc.pid), "T", "a failed copy left the node stopped")

    def test_a_hung_copy_times_out_and_the_node_is_not_left_stopped(self):
        # a copy that hangs (a stalled disk) must not leave the node frozen:
        # the copy is bounded, the script fails, and the node runs again.
        # Nothing else would resume it: a oneshot service has no start
        # timeout by default, and a SIGKILL runs no trap.
        self._node()
        rc, observed = self._run(pid=self.proc.pid, hang=True, timeout=2)
        self.assertEqual(observed, "T")
        self.assertNotEqual(rc, 0)
        self.assertIsNone(self.proc.poll())
        self.assertNotEqual(self._state(self.proc.pid), "T")

    def test_mainpid_zero_runs_without_signal(self):
        # stopped unit (ExecStopPost): rsync runs, nothing signalled.
        self._node()
        rc, observed = self._run(pid=0)
        self.assertEqual(rc, 0)
        self.assertEqual(observed, "S")



class NodeMigrationInstallsTheScript(unittest.TestCase):
    def test_the_wrapper_execs_a_path_node_migration_installs(self):
        src = open(os.path.join(os.path.dirname(SCRIPT), "node-migration.sh")).read()
        target = '"$HOME/.local/bin/dregg-node-snapshot"'
        before_exec, _, _ = src.partition('exec %s "$UNIT"' % target)
        self.assertTrue(_, "the wrapper no longer execs " + target)
        self.assertIn('/dregg-node-snapshot" ' + target, before_exec, "node-migration never installs the script")
        # the wrapper's exec target is installed before the wrapper is written:
        # a fresh migration must not leave the cave with a dead snapshot path.
        self.assertIn('install -m 755', before_exec)


if __name__ == "__main__":
    unittest.main()
