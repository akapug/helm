#!/usr/bin/env python3
"""helm chat node — a node replaying its blocklace reads BOOTING, never hung
(task/3891).

THE MEASURED CASE: the chat node was alive at 100% of one core,
with no listener on :8898, for over an hour. gdb put its main thread in
load_blocklace -> Blocklace::from_checkpoint -> detect_equivocation ->
causal_past, which is cubic on a one-creator lace (emberian/dregg#101).
`helm chat node status` called that "NOT INITIALIZING ... it is hung, not
booting" after a fixed 600 s and prescribed `down` and `up`. Two seats
followed it within the local-infra restart canon, and each restart threw
away the replay: 89 minutes, then 17.

The invariant these arms pin: the verdict comes from the process's own
progress, not from its age. Alive, no listener and the main thread's CPU
advancing is BOOTING, and its remedy is to wait. HUNG needs two readings of
the main thread's CPU at least PROGRESS_WINDOW_S apart that show no
progress. `up` and `down` refuse a BOOTING node unless given --force and a
--reason. Every surface (status, doctor, the chat-signing DEGRADED line and
the owner's console words) reads the one classifier.

Every arm runs on a planted /proc, a systemctl stand-in and a temp HELM_HOME.
None of them reaches systemd, the real node or the live chat rooms.
"""
import contextlib
import io
import json
import os
import shutil
import time
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from helm import cell, chat, chatnode, doctor  # noqa: E402
from tests.test_chatnode_posture import BootBase  # noqa: E402

URL = "http://127.0.0.1:8898"
PID = 173306
START = 4_000_000          # /proc/<pid>/stat field 22, in clock ticks
RUN_ID = "3a5e9072fdb646a5b04f327152efb149"
HZ = os.sysconf("SC_CLK_TCK") or 100
REPLAY_S = 89 * 60         # the first restart threw away 89 minutes
READY_SIGNER = {"configured": True, "usable": True, "state": "ready",
                "reason": "signer ready"}
UNPROVEN = {"dark": None, "why": "not a roster seat", "since": None,
            "seat": None}


def _show(active="active", sub="running", started_ago=REPLAY_S, pid=PID,
          result="success", invocation=RUN_ID):
    """`systemctl show` for the chat node's unit, MainPID included."""
    started = (int((time.monotonic() - started_ago) * 1e6)
               if started_ago is not None else 0)
    return (0, "ActiveState=%s\nSubState=%s\nNRestarts=0\nResult=%s\n"
               "InvocationID=%s\nExecMainStartTimestampMonotonic=%d\n"
               "MainPID=%d" % (active, sub, result, invocation, started, pid))


def _stat(pid, cpu_s, start=START):
    """One /proc stat line: utime carries `cpu_s`, stime one tick. The comm
    holds a space and a paren, so a reader that splits on the first ')'
    reads the wrong fields."""
    fields = ["R", "1", str(pid), str(pid), "0", "-1", "4194560", "0", "0",
              "0", "0", str(int(round(cpu_s * HZ)) - 1), "1", "0", "0", "20",
              "0", "12", "0", str(start)] + ["0"] * 30
    return "%d (dregg (node) x) %s\n" % (pid, " ".join(fields))


_TCP_HEAD = ("  sl  local_address rem_address   st tx_queue rx_queue tr "
             "tm->when retrnsmt   uid  timeout inode\n")


def _tcp_row(i, port, state):
    return ("%4d: 0100007F:%04X 00000000:0000 %s 00000000:00000000 00:00000000 "
            "00000000  1000        0 %d 1 0000000000000000 100 0 0 10 0\n"
            % (i, port, state, 9000 + i))


class BootingBase(BootBase):
    """BootBase's temp HOME, HELM_HOME and planted /proc, with a unit that
    systemd reports active/running as PID for REPLAY_S seconds."""

    def setUp(self):
        super().setUp()
        self.shows = [_show()]
        patch = mock.patch.object(chatnode, "_systemctl", self.systemctl)
        patch.start()
        self.addCleanup(patch.stop)
        self.plant(cpu_s=4868.0)

    def plant(self, cpu_s, listen=False, pid=PID, start=START,
              process_cpu_s=None):
        """/proc as the kernel shows the node: the main thread's CPU in
        task/<pid>/stat, the whole process's (every thread) in <pid>/stat,
        and the TCP tables. The gossip port listens from early on; only
        `listen` puts a listener on the API port."""
        task = os.path.join(self.proc, str(pid), "task", str(pid))
        os.makedirs(task, exist_ok=True)
        with open(os.path.join(task, "stat"), "w", encoding="utf-8") as f:
            f.write(_stat(pid, cpu_s, start))
        with open(os.path.join(self.proc, str(pid), "stat"), "w",
                  encoding="utf-8") as f:
            f.write(_stat(pid, cpu_s * 7 if process_cpu_s is None
                          else process_cpu_s, start))
        net = os.path.join(self.proc, "net")
        os.makedirs(net, exist_ok=True)
        rows = [_tcp_row(0, chatnode.GOSSIP_PORT, "0A"),
                _tcp_row(1, chatnode.PORT, "01")]   # a client, not a listener
        if listen:
            rows.append(_tcp_row(2, chatnode.PORT, "0A"))
        fd = os.path.join(self.proc, str(pid), "fd")
        os.makedirs(fd, exist_ok=True)
        sock = os.path.join(fd, "3")
        if os.path.lexists(sock):
            os.unlink(sock)
        if listen:
            os.symlink("socket:[9002]", sock)
        with open(os.path.join(net, "tcp"), "w", encoding="utf-8") as f:
            f.write(_TCP_HEAD + "".join(rows))
        with open(os.path.join(net, "tcp6"), "w", encoding="utf-8") as f:
            f.write(_TCP_HEAD)

    def seed(self, cpu_s, ago, pid=PID, start=START):
        """An earlier reading of the main thread's CPU, `ago` seconds back,
        as an earlier status read left it."""
        path = chatnode.progress_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"key": "%d@%d" % (pid, start),
                       "samples": [[time.monotonic() - ago, cpu_s]]}, f)

    def age_readings(self, by):
        """Move every recorded reading `by` seconds into the past."""
        path = chatnode.progress_path()
        with open(path, encoding="utf-8") as f:
            rec = json.load(f)
        rec["samples"] = [[t - by, c] for t, c in rec["samples"]]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rec, f)

    def diagnose(self):
        return chatnode.boot_diagnosis(URL)

    def status(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(chatnode.cell, "get_json", return_value=None), \
                mock.patch("helm.chat.transport_status",
                           return_value={"mode": "unsigned"}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = chatnode.cmd_node(["status"])
        return rc, out.getvalue() + err.getvalue()

    def doctor_rows(self):
        with mock.patch.object(chat, "node_head", return_value=None), \
                mock.patch.object(chat, "transport_status",
                                  return_value={"mode": "unsigned"}), \
                mock.patch.object(cell, "bin_status",
                                  return_value={"configured": False,
                                                "usable": False}):
            return doctor.check_chat_node()

    def verb(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = chatnode.cmd_node(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def acted(self):
        """Every systemctl verb that is not a read of the unit's state."""
        return [c[0] for c in self.calls if c and c[0] not in ("show",)]


class ClassifierTest(BootingBase):
    """(1) One classifier: BOOTING, HUNG, past boot, by measured progress."""

    def test_a_replay_burning_cpu_with_no_listener_is_BOOTING_not_hung(self):
        self.seed(4838.0, ago=31)
        d = self.diagnose()
        self.assertEqual(d["state"], "booting")
        self.assertEqual(d["progress"], "advancing")
        self.assertEqual((d["pid"], d["phase"]), (PID, "pre-bind"))
        # the MAIN THREAD's seconds (task/<pid>/stat), not the process's
        self.assertAlmostEqual(d["cpu_s"], 4868.0, places=1)
        self.assertGreater(d["delta_s"], 29)
        rc, out = self.status()
        self.assertEqual(rc, 1)
        self.assertIn("BOOTING at %s" % URL, out)
        self.assertIn("pid %d" % PID, out)
        self.assertIn("booting 1h29m", out)
        self.assertIn("alive with no listener on :%d;" % chatnode.PORT, out)
        self.assertIn("main-thread CPU 4868s", out)
        self.assertIn("do not restart", out)
        self.assertIn("CPU use cannot prove replay progress", out)
        self.assertIn("--force --reason", out)
        self.assertIn("task/3859", out)
        self.assertNotIn("hung", out)
        self.assertNotIn("NOT INITIALIZING", out)
        self.assertNotIn("`helm chat node down`", out)
        rows = self.doctor_rows()
        self.assertEqual([t for level, t in rows if level == doctor.FAIL], [])
        self.assertTrue(any(level == doctor.WARN and "BOOTING at" in t
                            for level, t in rows), rows)

    def test_no_cpu_progress_over_the_window_is_HUNG_with_todays_advice(self):
        """The main thread flat for 45 s while the process as a whole moves
        (its other threads tick): HUNG reads the main thread."""
        self.seed(4868.0, ago=45)
        d = self.diagnose()
        self.assertEqual(d["state"], "hung")
        self.assertIn("past the 600s boot wait", d["line"])
        self.assertIn("no listener on :%d" % chatnode.PORT, d["line"])
        # the window is the seeded 45 s plus however long this arm took to
        # reach the read: assert the measurement, not the second it ended on
        self.assertRegex(d["line"], r"used 0\.0s CPU over the last 4[5-9]s")
        rc, out = self.status()
        self.assertEqual(rc, 1)
        self.assertIn("NOT INITIALIZING at %s" % URL, out)
        self.assertIn("it is hung, not booting", out)
        self.assertIn("`helm chat node down` and `up`", out)
        rows = self.doctor_rows()
        self.assertTrue(any(level == doctor.FAIL and "NOT INITIALIZING" in t
                            for level, t in rows), rows)

    def test_one_reading_is_not_a_window_and_the_second_decides(self):
        d = self.diagnose()
        self.assertEqual((d["state"], d["progress"]), ("booting", "unmeasured"))
        _rc, out = self.status()
        self.assertIn("progress not measured yet", out)
        self.assertIn("BOOTING at %s" % URL, out)
        self.assertNotIn("NOT INITIALIZING", out)
        self.assertNotIn("it is hung", out)
        # the first read recorded itself; a second one a window later, with
        # the main thread not moved, is the measurement that says HUNG
        self.age_readings(chatnode.PROGRESS_WINDOW_S + 5)
        self.assertEqual(self.diagnose()["state"], "hung")

    def test_a_young_boot_with_an_idle_main_thread_is_still_booting(self):
        """The floor stays: inside hung_after_s nothing reads hung."""
        self.shows = [_show(started_ago=120)]
        self.seed(4868.0, ago=45)
        d = self.diagnose()
        self.assertEqual(d["state"], "booting")
        self.assertEqual(d["progress"], "flat")

    def test_a_recycled_pid_never_inherits_a_reading(self):
        self.seed(4868.0, ago=45, start=START - 999)
        d = self.diagnose()
        self.assertEqual(d["state"], "booting")
        self.assertEqual(d["progress"], "unmeasured")

    def test_a_listener_on_the_api_port_is_past_boot_not_booting(self):
        self.plant(cpu_s=4868.0, listen=True)
        self.seed(4838.0, ago=31)
        d = self.diagnose()
        self.assertEqual(d["state"], "unanswered")
        _rc, out = self.status()
        self.assertIn("API UNREACHABLE at %s" % URL, out)
        self.assertIn("listening on :%d" % chatnode.PORT, out)
        self.assertNotIn("BOOTING", out)
        self.assertNotIn("hung", out)

    def test_another_process_listening_does_not_end_this_nodes_boot(self):
        self.plant(cpu_s=4868.0, listen=True)
        os.unlink(os.path.join(self.proc, str(PID), "fd", "3"))
        other = os.path.join(self.proc, "999", "fd")
        os.makedirs(other)
        os.symlink("socket:[9002]", os.path.join(other, "3"))
        self.seed(4838.0, ago=31)
        d = self.diagnose()
        self.assertEqual((d["state"], d["listener"]), ("booting", "other"))
        rc, _out, err = self.verb("down")
        self.assertEqual(rc, 1)
        self.assertIn("REFUSED", err)
        self.assertEqual([c[0] for c in self.calls], ["show", "show"])
        # THE REFUSAL SAYS WHY: the port has a listener, and it is not this
        # node's — never "no listener", a fact the reading measured false
        self.assertIn("another process holds that port", err)
        self.assertNotIn("no listener on :%d," % chatnode.PORT, err)
        self.assertIn("another process holds that port",
                      chatnode.unreachable_line(URL, d))

    def test_unreadable_process_descriptors_cannot_prove_its_listener(self):
        self.plant(cpu_s=4868.0, listen=True)
        os.unlink(os.path.join(self.proc, str(PID), "fd", "3"))
        os.rmdir(os.path.join(self.proc, str(PID), "fd"))
        self.seed(4838.0, ago=31)
        d = self.diagnose()
        self.assertEqual((d["state"], d["listener"]), ("booting", "unknown"))
        self.assertIn("its sockets could not be read",
                      chatnode.unreachable_line(URL, d))

    def test_listener_scan_budget_keeps_large_tables_indeterminate(self):
        self.plant(cpu_s=4868.0, listen=True)
        self.seed(4838.0, ago=31)
        self.assertEqual(chatnode.api_listener(PID), "own")
        with mock.patch.object(chatnode, "LISTENER_SCAN_LIMIT", 1):
            self.assertEqual(chatnode.api_listener(PID), "unknown")
            self.assertEqual(self.diagnose()["state"], "booting")

    def test_a_main_thread_that_cannot_be_read_is_never_hung_nor_booting(self):  # noqa: VACUOUS_ASSERTION — each absence is read beside a presence on the same text: status `out` carries CANNOT TELL, doctor `rows` a WARN, and the door's pass is the stop it issued in self.calls
        """CPU UNREADABLE IS NOT A VERDICT. Its process gone between the
        systemd read and /proc, its /proc hidden, or no main process named:
        past the boot wait that is neither HUNG (whose `down` and `up` advice
        threw two replays away) nor BOOTING (whose door would hold a restart
        on no evidence). It says it cannot tell, and the door passes."""
        self.seed(4868.0, ago=45)                   # a flat window, unread
        shutil.rmtree(os.path.join(self.proc, str(PID)))
        d = self.diagnose()
        self.assertEqual(d["state"], "unmeasured")
        self.assertIn("pid %d) could not be read" % PID, d["line"])
        rc, out = self.status()
        self.assertEqual(rc, 1)
        self.assertIn("CANNOT TELL at %s" % URL, out)
        self.assertIn("past the 600s boot wait", out)
        self.assertNotIn("it is hung", out)
        self.assertNotIn("BOOTING", out)
        self.assertNotIn("`helm chat node down`", out)
        rows = self.doctor_rows()
        self.assertEqual([t for level, t in rows if level == doctor.FAIL], [])
        self.assertTrue(any(level == doctor.WARN and "CANNOT TELL" in t
                            for level, t in rows), rows)
        rc, _out, err = self.verb("down")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("REFUSED", err)
        self.assertIn("stop", self.acted())

    def test_no_main_process_inside_the_wait_initializes_and_past_it_says_so(self):
        self.shows = [_show(started_ago=120, pid=0)]
        self.assertEqual(self.diagnose()["state"], "initializing")
        self.shows = [_show(pid=0)]
        d = self.diagnose()
        self.assertEqual(d["state"], "unmeasured")
        self.assertIn("systemd names no main process to read", d["line"])

    def test_a_stopped_unit_is_down_as_before(self):
        self.shows = [_show("inactive", "dead", started_ago=None, pid=0)]
        self.assertEqual(self.diagnose(),
                         {"state": "down", "line": None, "remedy": None})


class DoorTest(BootingBase):
    """(2) `up` and `down` refuse a BOOTING node unless --force --reason."""

    def test_down_refuses_while_booting_and_says_for_how_long(self):
        self.seed(4838.0, ago=31)
        rc, _out, err = self.verb("down")
        self.assertEqual(rc, 1)
        # the door read the unit once, and that was all
        self.assertEqual([c[0] for c in self.calls], ["show"])
        self.assertIn("REFUSED", err)
        self.assertIn("BOOTING", err)
        self.assertIn("1h29m", err)
        self.assertIn("task/3859", err)
        self.assertIn("--force --reason", err)

    def test_up_refuses_while_booting_before_it_writes_anything(self):  # noqa: VACUOUS_ASSERTION — the unit file an `up` past the door writes is read back by tests/test_chatnode_binary.py (unit_execstart), the positive control on the same path
        self.seed(4838.0, ago=31)
        with self.node_bin():
            rc, _out, err = self.verb("up")
        self.assertEqual(rc, 1)
        self.assertIn("REFUSED", err)
        self.assertEqual([c[0] for c in self.calls], ["show"])
        self.assertFalse(os.path.exists(chatnode.unit_path()))

    def test_force_needs_a_reason(self):  # noqa: VACUOUS_ASSERTION — the same self.calls records the stop in test_force_with_a_reason_stops_a_booting_node_and_says_so, its positive control
        self.seed(4838.0, ago=31)
        rc, _out, err = self.verb("down", "--force")
        self.assertEqual(rc, 2)
        self.assertNotIn("stop", self.acted())
        self.assertIn("--reason", err)

    def test_force_with_a_reason_stops_a_booting_node_and_says_so(self):
        self.seed(4838.0, ago=31)
        rc, _out, err = self.verb("down", "--force", "--reason",
                                  "owner asked for the patched binary")
        self.assertEqual(rc, 0)
        self.assertIn("stop", self.acted())
        self.assertIn("FORCED", err)
        self.assertIn("owner asked for the patched binary", err)

    def test_force_does_not_wait_for_an_unmeasured_old_boot(self):
        with mock.patch.object(chatnode.time, "sleep",
                               side_effect=AssertionError("forced down slept")):
            rc, _out, err = self.verb("down", "--force", "--reason",
                                      "confirmed spin")
        self.assertEqual(rc, 0, err)
        self.assertIn("FORCED", err)
        self.assertNotIn("measuring", err)
        self.assertIn("stop", self.acted())

    def test_spinning_main_thread_explains_the_forced_escape(self):
        self.seed(4838.0, ago=31)
        rc, _out, err = self.verb("down")
        self.assertEqual(rc, 1)
        self.assertIn("--force --reason", err)
        self.assertIn("REFUSED", err)
        self.assertIn("CPU activity alone cannot prove replay progress",
                      self.diagnose()["remedy"])

    def test_up_against_a_hung_node_says_hung_at_once(self):
        """`enable --now` does not restart a running node, so `up` against a
        measured hung one says so at the first look, in status's words,
        rather than waiting out the boot wait and calling it initializing."""
        self.seed(4868.0, ago=45)
        with self.node_bin(), \
                mock.patch.dict(os.environ,
                                {"HELM_CHAT_NODE_BOOT_WAIT_S": "8"}), \
                mock.patch.object(chatnode.cell, "get_json", return_value=None):
            rc, _out, err = self.verb("up")
        self.assertEqual(rc, 1)
        self.assertNotIn("REFUSED", err)
        self.assertNotIn("still initializing", err)
        self.assertIn("NOT INITIALIZING at %s" % URL, err)
        self.assertIn("it is hung, not booting", err)

    def test_a_hung_node_still_stops(self):
        self.seed(4868.0, ago=45)
        rc, _out, _err = self.verb("down")
        self.assertEqual(rc, 0)
        self.assertIn("stop", self.acted())

    def test_a_stopped_node_still_comes_up(self):
        """The door passes a unit that is gone: `up` writes the unit and
        starts it, as before."""
        self.shows = [_show("inactive", "dead", started_ago=None, pid=0)]
        with self.node_bin(), \
                mock.patch.object(chatnode, "wait_boot",
                                  return_value=("initializing", 1)):
            rc, _out, err = self.verb("up")
        self.assertEqual(rc, 1)                 # still initializing: no fault
        self.assertIn("enable", self.acted())
        self.assertTrue(os.path.exists(chatnode.unit_path()))
        self.assertNotIn("REFUSED", err)

    def test_an_unmeasured_old_boot_is_measured_before_the_door_decides(self):  # noqa: VACUOUS_ASSERTION — the loop is over a non-empty literal tuple; the moved row asserts no stop and the unmoved row asserts the stop, on the same self.calls
        """No earlier reading and past the floor: the door takes its own
        second reading a window later rather than guess either way."""
        real_sleep = time.sleep
        for moved, refused in ((True, True), (False, False)):
            with self.subTest(main_thread_moved=moved):
                self.calls.clear()
                if os.path.exists(chatnode.progress_path()):
                    os.unlink(chatnode.progress_path())
                self.plant(cpu_s=4868.0)

                def sleep(s, moved=moved):
                    if moved:
                        self.plant(cpu_s=4869.0)
                    real_sleep(s)
                with mock.patch.object(chatnode, "PROGRESS_WINDOW_S", 0.2), \
                        mock.patch.object(chatnode.time, "sleep", sleep):
                    rc, _out, err = self.verb("down")
                self.assertEqual(rc, 1 if refused else 0, err)
                self.assertEqual("stop" in self.acted(), not refused)
                self.assertIn("measuring", err)


class SigningLineTest(BootingBase):
    """(3) The chat-signing DEGRADED line and the owner's words say BOOTING,
    from the same classifier, instead of 'restore the chat node'."""

    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {
            "HELM_CHAT_DIR": os.path.join(self.tmp, "chat"),
            "HELM_CHAT_NODE_URL": URL, "HELM_CELL_PROFILE": "reader-profile",
            "HELM_SCRATCH_GC": "0",
            "HELM_CACHE_DIR": os.path.join(self.tmp, "cache")})
        env.start()
        self.addCleanup(env.stop)
        getattr(chat, "_SEAT_DARK_MEMO", {}).clear()
        self.addCleanup(lambda: getattr(chat, "_SEAT_DARK_MEMO", {}).clear())
        self.addCleanup(lambda: __import__("shutil").rmtree(
            chat.sign_failures_dir(), True))

    def transport(self):
        with mock.patch.object(chat, "node_head", return_value=None), \
                mock.patch.object(cell, "bin_status",
                                  return_value=READY_SIGNER), \
                mock.patch.object(chat, "_seat_darkness",
                                  side_effect=lambda p, now=None: UNPROVEN,
                                  create=True):
            return chat.transport_status(fleet=True)

    def test_a_booting_node_degrades_signing_with_wait_not_restore(self):
        self.seed(4838.0, ago=31)
        chat._record_sign_failure("ds4pro", chat._diag(
            "node_unreachable", "chat node unreachable at %s" % URL,
            event_epoch=time.time() - 60, event_ts="2026-09-30T23:30:00Z"))
        st = self.transport()
        self.assertEqual((st["mode"], st["code"]),
                         ("degraded", "node_unreachable"))
        self.assertIn("BOOTING", st["cause"])
        self.assertIn("do not restart", st["remediation"])
        self.assertIn("task/3859", st["remediation"])
        self.assertNotIn("restore the chat node", st["remediation"])
        self.assertNotIn("restarts the node", st["owner_say"])
        self.assertIn("starting up", st["owner_say"])
        line = chat.transport_failure_summary(st)
        self.assertIn("BOOTING", line)
        self.assertIn("do not restart", line)
        self.assertNotIn("restore the chat node", line)

    def test_no_recorded_failure_still_reads_booting(self):
        self.seed(4838.0, ago=31)
        st = self.transport()
        self.assertEqual(st["code"], "node_unreachable")
        self.assertIn("BOOTING", st["cause"])
        self.assertIn("do not restart", st["remediation"])

    def test_a_stopped_node_keeps_the_restore_remediation(self):
        self.shows = [_show("inactive", "dead", started_ago=None, pid=0)]
        st = self.transport()
        self.assertEqual(st["code"], "node_unreachable")
        self.assertIn("restore the chat node", st["remediation"])
        self.assertNotIn("BOOTING", st.get("cause") or "")


if __name__ == "__main__":
    unittest.main()
