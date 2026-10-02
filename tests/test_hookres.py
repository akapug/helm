#!/usr/bin/env python3
"""The hook resident (task/1825): argv-guard answered by a warm, forked helm
instead of a cold interpreter, byte for byte what the cold run answers, and
the cold run itself whenever the resident cannot take the call.

EVERY ARM RUNS THE SHIPPED FILES. A real `helm hooks resident` (this tree's,
or a copy of it) serves a hermetic helm home; the real `bin/helm-hook` runs
the real `bin/helm` or the real `bin/helm-hookres` under /bin/sh, exactly as
an installed PreToolUse hook does. "The resident answered" is read from the
resident's own trace (HELM_HOOK_RESIDENT_TRACE, one line per call it ran),
never inferred from timing. The COLD twin of every warm call is the same
wrapper with HELM_HOOK_RESIDENT=off, which is the path every host without a
resident takes.

THE ARMS:
  * warm == cold, byte for byte (rc, stdout, stderr): a pass, a pass that
    speaks (a steer on stdout and stderr), two refusals, and an Edit of the
    client script itself (the hook's own shells are never a script's runner);
  * fail open: no endpoint (silent), a dead or gone or stopped endpoint, a
    slow resident, an environment it cannot stand in for (each one line);
  * the resident proves itself: an answer without the endpoint's proof is
    never trusted and never printed (the call runs cold without a word), and
    a wrong token gets no proof back;
  * exactly once: after `go` a vanished resident is an UNKNOWN outcome, never
    a pass; a refusal after `go` runs the call cold; a client stalled with its
    stdout on the socket after `go` is still served once; a real resident
    that answers after the client's wait never runs the call too;
  * a call is tied to the process that sent it, and a request is bounded as
    a whole;
  * a changed tree: the resident re-execs onto it (same pid, new token) and
    no call it serves after the change runs the old code, including one whose
    HEAD moved while the digest was walked;
  * the wrapper and helm/hookres.py SERVED name the same argv.
"""
import contextlib
import errno
import io
import json
import os
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.realpath(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from helm import hookres, stopfacts_resident  # noqa: E402

# A pass that says nothing, a pass that speaks (the pipeline-status steer, on
# stdout and stderr), a refusal from the agent-model rung and one from the
# act deny. Each warm/cold pair gets fresh session ids, so a once-per-session
# latch never makes the second run of a pair quieter than the first.
PASS = {"tool_name": "Bash", "tool_input": {"command": "true"}}
STEER = {"tool_name": "Bash",
         "tool_input": {"command": "make test | tail -5; echo $?"}}
REFUSE = {"tool_name": "Agent",
          "tool_input": {"model": "opus", "prompt": "x"}}
DENY = {"tool_name": "Bash", "tool_input": {"command": "pkill -f foo"}}
# An Edit of the client script: every warm call has a `bash -p .../helm-hookres`
# running it, which is the hook itself and never a runner to warn about.
CLIENT = os.path.join(ROOT, "bin", "helm-hookres")
EDIT_CLIENT = {"tool_name": "Edit",
               "tool_input": {"file_path": CLIENT, "old_string": "a",
                              "new_string": "b"}}
# A fake resident's proof, written into the endpoint it plays.
PROOF = "c" * 32

_N = [0]


def _session():
    _N[0] += 1
    return "hookres-test-%d-%d" % (os.getpid(), _N[0])


def _starttime(pid):
    """A process's start time in clock ticks, the endpoint's fourth field."""
    with open("/proc/%d/stat" % pid, "rb") as f:
        raw = f.read()
    return int(raw[raw.rindex(b")") + 2:].split()[19])


def _state(pid):
    with open("/proc/%d/stat" % pid, "rb") as f:
        raw = f.read()
    return raw[raw.rindex(b")") + 2:].split()[0].decode()


# PR_SET_PTRACER: which process may ptrace (and so pidfd_getfd) this one
# where kernel.yama.ptrace_scope is 1; ANY is (unsigned long)-1.
_PR_SET_PTRACER, _PTRACER_ANY = 0x59616D61, (1 << 64) - 1


def _set_ptracer(who):
    import ctypes
    ctypes.CDLL(None, use_errno=True).prctl(
        ctypes.c_int(_PR_SET_PTRACER), ctypes.c_ulong(who), ctypes.c_ulong(0),
        ctypes.c_ulong(0), ctypes.c_ulong(0))


def _ptrace_scope():
    try:
        with open("/proc/sys/kernel/yama/ptrace_scope") as f:
            return int(f.read())
    except (OSError, ValueError):
        return 0


# A parent the kernel refuses pidfd_getfd on for any other process, on any
# host: it made itself non-dumpable. It runs argv[1:] and exits with its rc.
NONDUMPABLE = ("import ctypes, subprocess, sys\n"
               "ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)\n"
               "sys.exit(subprocess.call(sys.argv[1:]))\n")


class ResidentFixture(unittest.TestCase):
    """A hermetic helm home whose hooks have a recorded interpreter (this
    one), so bin/helm-hook takes the path the resident sits on."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-hookres-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

        def j(*parts):
            return os.path.join(self.tmp, *parts)
        self.j = j
        self.home = j("home")
        for d in ("home", "pathbin", "chat", "proc", "alarm", "elsewhere",
                  os.path.join("home", "_global", ".state", "hookres")):
            os.makedirs(j(d), exist_ok=True)
        # THE INTERPRETER FLOOR, PLANTED: the PATH python3 is a shim, and the
        # record says it leads to this interpreter, so the wrapper runs
        # `<this python> -S` and the resident (started with this python)
        # serves it.
        shim = j("pathbin", "python3")
        with open(shim, "w") as f:
            f.write("#!/bin/sh\nexec %s \"$@\"\n" % shlex.quote(sys.executable))
        os.chmod(shim, 0o755)
        with open(j("home", "_global", ".state", "hook-interp"), "w") as f:
            f.write("%s\t%s\n" % (shim, sys.executable))
        with open(j("home", "_global", "registry.json"), "w") as f:
            json.dump({"projects": {"helm": {"path": ROOT}}}, f)
        self.trace = j("trace")
        self.cwd = j("elsewhere")
        self.env = dict(
            os.environ,
            PATH=j("pathbin") + os.pathsep + os.environ.get("PATH", ""),
            HELM_HOME=self.home, HELM_ADOPTED_DIR=j("adopted"),
            HELM_CHAT_DIR=j("chat"), HELM_PROC=j("proc"),
            HELM_SCRATCH_GC="0", HELM_HOOK_ALARM_DIR=j("alarm"),
            HELM_CHAT_NAME="hookres-fixture",
            HELM_STOPPROBE_LOG=j("stopprobe.log"))
        for k in ("HELM_HOOK_INTERP_RECORD", "HELM_HOOK_INTERP_KEY",
                  "HELM_HOOK_RESIDENT", "HELM_HOOK_RESIDENT_TRACE",
                  "HELM_HOOK_RESIDENT_WAIT_S", "PYTHONWARNINGS"):
            self.env.pop(k, None)
        # THE RESIDENT'S START PROBE asks whether it may take its PARENT's
        # fds with pidfd_getfd (its callers are never its descendants). On a
        # kernel.yama.ptrace_scope=1 host (the fab nodes) yama refuses that
        # for a non-descendant unless the parent names it; this test process
        # is the parent, and names any process for the length of the test.
        # Its callers name nobody, so the resident still takes their pipes
        # through /proc there. Scope 2 and 3 honour no such name, and
        # start_resident says so.
        _set_ptracer(_PTRACER_ANY)
        self.addCleanup(_set_ptracer, 0)

    def endpoint(self, root=ROOT):
        return os.path.join(self.home, "_global", ".state", "hookres",
                            os.path.join(root, "bin").replace(os.sep, "_"))

    def read_endpoint(self, root=ROOT):
        """(port, token, pid, starttime, proof), as the resident wrote them;
        the proof is "" in an endpoint that carries none."""
        with open(self.endpoint(root)) as f:
            parts = f.read().split()
        port, token, pid, start = parts[:4]
        return (int(port), token, int(pid), int(start),
                parts[4] if len(parts) > 4 else "")

    def write_endpoint(self, port, pid=None, start=None, token="f" * 32,
                       proof=PROOF, beat="keep"):
        """An endpoint for a listener this test plays; by default it names
        this process, alive and running, and its heartbeat is kept fresh
        (as a serving resident's poll keeps it), so the client connects.
        `beat` an int writes that epoch second once; None writes none."""
        pid = os.getpid() if pid is None else pid
        start = _starttime(pid) if start is None else start
        with open(self.endpoint(), "w") as f:
            f.write("%d %s %d %d %s\n" % (port, token, pid, start, proof))
        self._beats = getattr(self, "_beats", None)
        if self._beats is not None:
            self._beats.set()
            self._beats = None
        path = self.endpoint() + ".beat"
        if beat is None:
            if os.path.exists(path):
                os.unlink(path)
            return
        if beat != "keep":
            with open(path, "w") as f:
                f.write("%d\n" % beat)
            return
        stop = threading.Event()

        def keep():
            while not stop.is_set():
                with open(path + ".tmp", "w") as f:
                    f.write("%d\n" % int(time.time()))
                os.replace(path + ".tmp", path)
                stop.wait(0.3)
        t = threading.Thread(target=keep, daemon=True)
        t.start()
        self._beats = stop

        def done():
            stop.set()
            t.join(10)
        self.addCleanup(done)

    def raw_request(self, token, pid, root=ROOT):
        """The client's request, as bytes, naming `pid` as its sender."""
        return b"\0".join([
            b"helm-hookres/1", token.encode(), str(pid).encode(),
            os.path.join(root, "bin").encode(), sys.executable.encode(), b"4",
            os.path.join(root, "bin", "helm").encode(), b"chat", b"argv-guard",
            b"--hook-json"]) + b"\0"

    def copy_tree(self, client=None):
        """A copy of this tree's helm/ and bin/ (the resident serves the copy
        by its own path); `client` rewrites the copy's bin/helm-hookres."""
        tree = self.j("tree")
        shutil.copytree(os.path.join(ROOT, "helm"),
                        os.path.join(tree, "helm"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(os.path.join(ROOT, "bin"), os.path.join(tree, "bin"))
        if client is not None:
            p = os.path.join(tree, "bin", "helm-hookres")
            with open(p) as f:
                text = client(f.read())
            with open(p, "w") as f:
                f.write(text)
        return os.path.realpath(tree)

    def settle(self, proc, timeout=30):
        """Wait until every child the resident forked has exited, so what it
        ran is in the trace before the trace is read."""
        p = "/proc/%d/task/%d/children" % (proc.pid, proc.pid)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with open(p) as f:
                    kids = [int(k) for k in f.read().split()]
            except OSError:
                time.sleep(1.0)
                return
            live = []
            for k in kids:
                try:
                    if _state(k) not in ("Z", "X"):
                        live.append(k)
                except OSError:
                    pass
            if not live:
                return
            time.sleep(0.02)
        self.fail("the resident's children did not finish")

    def start_resident(self, root=ROOT, env=None, session=False):
        """A real `helm hooks resident` for `root`, serving; -> Popen.
        `session` starts it in its own session (and process group), as a
        unit's cgroup holds it and its children."""
        if _ptrace_scope() >= 2:
            self.skipTest("kernel.yama.ptrace_scope=%d: no parent can let the "
                          "resident probe pidfd_getfd, so it does not start "
                          "on this host, by design" % _ptrace_scope())
        log = open(self.j("resident-%d.log" % _session_n()), "w")
        self.addCleanup(log.close)
        self.resident_log = log.name
        proc = subprocess.Popen(
            [sys.executable, "-S", os.path.join(root, "bin", "helm"), "hooks",
             "resident"],
            env=dict(env or self.env, HELM_HOOK_RESIDENT_TRACE=self.trace),
            cwd=self.cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=log, start_new_session=session)
        self.addCleanup(self._stop, proc)
        deadline = time.monotonic() + 90
        while not os.path.exists(self.endpoint(root)):
            if proc.poll() is not None or time.monotonic() > deadline:
                with open(log.name) as f:
                    self.fail("the resident did not start: %s" % f.read())
            time.sleep(0.05)
        return proc

    def _stop(self, proc):
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    def served(self):
        try:
            with open(self.trace) as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []

    def hook(self, extra, resident="on", root=ROOT, env=None, budget=60,
             args="chat argv-guard --hook-json"):
        """One PreToolUse argv-guard call through the shipped wrapper."""
        payload = dict(extra, session_id=_session(),
                       hook_event_name="PreToolUse", cwd=self.cwd)
        cmd = "%s gate argv-guard PreToolUse %d %s %s %s" % (
            shlex.quote(os.path.join(root, "bin", "helm-hook")), budget,
            shlex.quote("tool call"),
            shlex.quote(os.path.join(root, "bin", "helm")), args)
        run_env = dict(env or self.env, HELM_HOOK_RESIDENT=resident)
        r = subprocess.run(["sh", "-c", cmd], input=json.dumps(payload),
                           capture_output=True, text=True, env=run_env,
                           cwd=self.cwd, timeout=240)
        return r.returncode, r.stdout, r.stderr


_SN = [0]


def _session_n():
    _SN[0] += 1
    return _SN[0]


class AcceptFailureTest(ResidentFixture):

    def test_retry_only_transient_accept_errors(self):
        """A fatal accept error must stop the heartbeat-producing loop,
        rather than retrying forever while its listen backlog fills."""
        with mock.patch.dict(os.environ, {"HELM_HOME": self.home,
                                          "HELM_ADOPTED_DIR": self.j("adopted")}):
            r = hookres.Resident()
        r.sock = mock.Mock()
        r.sock.accept.side_effect = [OSError(errno.EAGAIN, "try again"),
                                     OSError(errno.EMFILE, "too many files")]
        with mock.patch.object(r, "_reap"), mock.patch.object(r, "_poll"), \
                mock.patch.object(hookres.select, "select",
                                  return_value=([r.sock], [], [])), \
                mock.patch.object(hookres, "_say") as said:
            self.assertEqual(r.serve(), 3)
        self.assertEqual(r.sock.accept.call_count, 2)
        self.assertTrue(r.accept_failed)
        self.assertIn("cannot accept hook calls", said.call_args.args[0])


class WarmIsColdTest(ResidentFixture):

    def test_warm_answers_are_the_cold_answers_byte_for_byte(self):  # noqa: VACUOUS_ASSERTION — every case asserts the trace GREW by one (the resident answered) and the MUST-HIT tail asserts rc 2 and a spoken steer unconditionally
        """THE CONTRACT. For a pass, a pass that speaks, and two refusals, the
        resident's answer (rc, stdout, stderr) is the cold run's, and the
        resident's trace proves it answered each warm call."""
        self.start_resident()
        for name, extra in (("pass", PASS), ("steer", STEER),
                            ("refuse", REFUSE), ("deny", DENY)):
            with self.subTest(case=name):
                before = len(self.served())
                warm = self.hook(extra)
                self.assertEqual(len(self.served()), before + 1,
                                 "the resident did not answer the warm call: "
                                 "%r" % (warm,))
                cold = self.hook(extra, resident="off")
                self.assertEqual(len(self.served()), before + 1,
                                 "HELM_HOOK_RESIDENT=off still asked it")
                self.assertEqual(warm, cold)
        # MUST-HIT: the cases are what they say they are.
        self.assertEqual(self.hook(REFUSE, resident="off")[0], 2)
        self.assertEqual(self.hook(DENY, resident="off")[0], 2)
        rc, out, err = self.hook(STEER, resident="off")
        self.assertEqual(rc, 0)
        self.assertIn("[helm steer]", out)
        self.assertIn("[helm steer]", err)

    def test_an_edit_of_the_client_script_is_answered_as_cold(self):  # noqa: VACUOUS_ASSERTION — the CONTROL runner must be NAMED, warm and cold alike, before the plain edit's silence counts
        """F9. Every warm call runs under a `bash -p .../bin/helm-hookres`,
        which is the hook itself. An Edit of that script is answered as the
        cold run answers it: no shell of the hook's own chain (the client,
        `timeout`, the wrapper) is named as a runner. A shell that really
        runs the script is named by both. HELM_WORK_INTEGRATOR keeps the
        shared-checkout rung from refusing the Edit before the steers."""
        self.start_resident()
        env = dict(self.env, HELM_WORK_INTEGRATOR="1")
        before = len(self.served())
        warm = self.hook(EDIT_CLIENT, env=env)
        self.assertEqual(len(self.served()), before + 1,
                         "the resident did not answer: %r" % (warm,))
        cold = self.hook(EDIT_CLIENT, env=env, resident="off")
        self.assertEqual(warm, cold)
        self.assertNotIn("in place while pid", warm[1] + warm[2])
        # CONTROL: a shell whose argv names the script, outside the hook.
        runner = subprocess.Popen(["bash", "-c", "sleep 60; :", CLIENT],
                                  stdin=subprocess.DEVNULL,
                                  start_new_session=True)

        def stop():
            os.killpg(runner.pid, signal.SIGKILL)
            runner.wait()
        self.addCleanup(stop)
        deadline = time.monotonic() + 30
        while True:
            with open("/proc/%d/cmdline" % runner.pid, "rb") as f:
                if CLIENT.encode() in f.read():
                    break
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        warm = self.hook(EDIT_CLIENT, env=env)
        cold = self.hook(EDIT_CLIENT, env=env, resident="off")
        self.assertEqual(warm, cold)
        self.assertIn("in place while pid %d " % runner.pid,
                      warm[1] + warm[2])

    def test_the_wrapper_asks_for_exactly_the_argv_the_resident_serves(self):  # noqa: VACUOUS_ASSERTION — the SERVED argv is asserted to GROW the trace by one before the extra-argument call is asserted not to
        """helm/hookres.py SERVED and bin/helm-hook's own test name the same
        calls: each SERVED argv reaches the resident, and the same child with
        one more argument does not (the wrapper never asks, so the resident
        never refuses it either)."""
        self.assertEqual(hookres.SERVED,
                         frozenset({("chat", "argv-guard", "--hook-json")}),
                         "SERVED changed: teach bin/helm-hook the same argv "
                         "and extend this arm")
        self.start_resident()
        for tail in sorted(hookres.SERVED):
            before = len(self.served())
            self.hook(PASS, args=" ".join(tail))
            self.assertEqual(len(self.served()), before + 1, tail)
        before = len(self.served())
        rc, _out, err = self.hook(PASS, args="chat argv-guard --hook-json x")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.served()), before)
        self.assertNotIn("hook resident", err)


class FailOpenTest(ResidentFixture):

    def test_no_endpoint_runs_cold_without_a_word(self):  # noqa: VACUOUS_ASSERTION — the equality is against the cold twin, whose own rc 2 on REFUSE the byte-for-byte arm pins; the absence of a line IS the claim
        """No resident serves this checkout for this home: the cold run's
        answer, and nothing added to it."""
        self.assertFalse(os.path.exists(self.endpoint()))
        for extra in (PASS, REFUSE):
            self.assertEqual(self.hook(extra), self.hook(extra, resident="off"))

    def test_a_dead_or_gone_endpoint_runs_cold_and_names_it(self):  # noqa: VACUOUS_ASSERTION — the loop is over fixed cases and runs each; each asserts the named line is present
        """An endpoint whose port nobody answers, and one whose pid is gone
        (another process now holds the number: the start time differs): the
        cold answer, with one line in front of its stderr that names the
        resident as down, and the reason."""
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        for why, start in (("nothing answers", None),
                           ("is gone", _starttime(os.getpid()) + 1)):
            self.write_endpoint(port, start=start)
            for extra in (PASS, REFUSE):
                with self.subTest(why=why, payload=extra["tool_name"]):
                    rc, out, err = self.hook(extra)
                    crc, cout, cerr = self.hook(extra, resident="off")
                    self.assertEqual((rc, out), (crc, cout))
                    line, _, rest = err.partition("\n")
                    self.assertIn("[helm argv-guard] hook resident is down",
                                  line)
                    self.assertIn(why, line)
                    self.assertIn("this call runs cold", line)
                    self.assertEqual(rest, cerr)

    def test_a_stopped_resident_runs_cold_without_a_connect(self):  # noqa: VACUOUS_ASSERTION — the listener's own accept queue is read after SIGCONT and must say none, a positive observable, beside the named line
        """F7. A resident that is stopped (SIGSTOP, state T) cannot answer,
        and with its backlog full a connect to it blocks for the whole
        budget. The client reads the endpoint pid's state before it connects:
        stopped means cold, named, and no connection at all (the listener's
        accept queue is empty once it runs again)."""
        code = ("import socket, sys\n"
                "s = socket.socket()\n"
                "s.bind(('127.0.0.1', 0))\n"
                "s.listen(8)\n"
                "print(s.getsockname()[1], flush=True)\n"
                "sys.stdin.readline()\n"
                "s.setblocking(False)\n"
                "try:\n"
                "    s.accept()\n"
                "    print('connected', flush=True)\n"
                "except BlockingIOError:\n"
                "    print('none', flush=True)\n")
        lsn = subprocess.Popen([sys.executable, "-S", "-c", code],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               text=True)

        def stop():
            if lsn.poll() is None:
                os.kill(lsn.pid, signal.SIGCONT)
                lsn.kill()
            lsn.wait()
            lsn.stdin.close()
            lsn.stdout.close()
        self.addCleanup(stop)
        port = int(lsn.stdout.readline())
        self.write_endpoint(port, pid=lsn.pid)
        os.kill(lsn.pid, signal.SIGSTOP)
        deadline = time.monotonic() + 30
        while _state(lsn.pid) not in ("T", "t"):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        env = dict(self.env, HELM_HOOK_RESIDENT_WAIT_S="3")
        rc, out, err = self.hook(REFUSE, env=env)
        os.kill(lsn.pid, signal.SIGCONT)
        lsn.stdin.write("check\n")
        lsn.stdin.flush()
        self.assertEqual(lsn.stdout.readline().strip(), "none",
                         "the client connected to a stopped resident")
        crc, cout, cerr = self.hook(REFUSE, resident="off")
        self.assertEqual((rc, out), (crc, cout))
        line, _, rest = err.partition("\n")
        self.assertIn("hook resident is stopped", line)
        self.assertIn("this call runs cold", line)
        self.assertEqual(rest, cerr)

    def test_a_resident_alive_but_not_accepting_runs_every_call_cold(self):  # noqa: VACUOUS_ASSERTION — every call must EQUAL its cold twin's rc 2 (the real check ran) and name the heartbeat, positive observables
        """ROUND 3 (a). A resident that is alive and reads as running (a
        frozen cgroup, a serve loop stuck in the kernel: state S or D, not T)
        stops accepting, and its accept queue fills. A connect then blocks
        until the wrapper's budget, and a gate's timeout ALLOWS the call
        unchecked, on every seat. Played here by a SIGSTOPped listener whose
        backlog this test fills, behind an endpoint that names this running
        process. The resident's heartbeat (<endpoint>.beat, the epoch second
        its poll writes) is stale, then missing: every call runs cold with a
        line that names it, and every call runs the real check (rc 2, the
        cold twin's bytes), never ALLOWED unchecked."""
        code = ("import socket, sys, time\n"
                "s = socket.socket()\n"
                "s.bind(('127.0.0.1', 0))\n"
                "s.listen(1)\n"
                "print(s.getsockname()[1], flush=True)\n"
                "time.sleep(600)\n")
        lsn = subprocess.Popen([sys.executable, "-S", "-c", code],
                               stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, text=True)

        def stop():
            os.kill(lsn.pid, signal.SIGCONT)
            lsn.kill()
            lsn.wait()
            lsn.stdout.close()
        self.addCleanup(stop)
        port = int(lsn.stdout.readline())
        os.kill(lsn.pid, signal.SIGSTOP)
        fill = []
        for _ in range(8):
            c = socket.socket()
            c.setblocking(False)
            c.connect_ex(("127.0.0.1", port))
            fill.append(c)
            self.addCleanup(c.close)
        for name, beat in (("stale", int(time.time()) - 30),
                           ("missing", None)):
            self.write_endpoint(port, beat=beat)
            for _ in range(2):
                with self.subTest(beat=name):
                    rc, out, err = self.hook(REFUSE, budget=6)
                    crc, cout, cerr = self.hook(REFUSE, resident="off")
                    self.assertEqual((rc, out), (crc, cout), err)
                    self.assertEqual(rc, 2)
                    self.assertNotIn("UNCHECKED", out)
                    line, _, rest = err.partition("\n")
                    self.assertIn("heartbeat", line)
                    self.assertIn("this call runs cold", line)
                    self.assertEqual(rest, cerr)

    def test_fresh_heartbeat_does_not_let_a_full_backlog_bypass_the_guard(self):  # noqa: VACUOUS_ASSERTION — the cold twin's rc 2 and bytes are unconditional positive controls, and the blocked connect is observed by socket.timeout
        """A live, beating endpoint need not accept: a blocked TCP connect
        must fall back before the wrapper's deadline, and the real refusal
        must reach the harness rather than an unchecked timeout."""
        code = ("import socket, sys, time\n"
                "s = socket.socket()\n"
                "s.bind(('127.0.0.1', 0))\n"
                "s.listen(1)\n"
                "print(s.getsockname()[1], flush=True)\n"
                "time.sleep(600)\n")
        lsn = subprocess.Popen([sys.executable, "-S", "-c", code],
                               stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, text=True)

        def stop():
            lsn.kill()
            lsn.wait()
            lsn.stdout.close()
        self.addCleanup(stop)
        port = int(lsn.stdout.readline())
        fill = []
        for _ in range(8):
            c = socket.socket()
            c.setblocking(False)
            c.connect_ex(("127.0.0.1", port))
            fill.append(c)
            self.addCleanup(c.close)
        # Show that this is the blocked-connect arm, not a fake that accepts
        # and simply declines to answer the handshake.
        with self.assertRaises(socket.timeout):
            socket.create_connection(("127.0.0.1", port), timeout=0.2)
        self.write_endpoint(port, pid=lsn.pid)
        env = dict(self.env, HELM_HOOK_RESIDENT_WAIT_S="0.4")
        # The wrapper's budget is the bound: a connect that spent it would be
        # killed by `timeout` and could not return the cold refusal below.
        rc, out, err = self.hook(REFUSE, env=env, budget=6)
        crc, cout, cerr = self.hook(REFUSE, resident="off")
        self.assertEqual((rc, out), (crc, cout), err)
        self.assertEqual(rc, 2, err)
        line, _, rest = err.partition("\n")
        self.assertIn("hook resident", line)
        self.assertIn("this call runs cold", line)
        self.assertEqual(rest, cerr)

    def _fake(self, behave):
        """A listener that plays a resident; -> its port. Its endpoint names
        this process (alive and running) and carries PROOF. Its thread polls
        a stop flag and is joined at cleanup, so no thread outlives the
        test."""
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(8)
        srv.settimeout(0.1)
        held = []
        stop = threading.Event()

        def loop():
            while not stop.is_set():
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                held.append(conn)
                try:
                    behave(conn)
                except OSError:
                    pass
        t = threading.Thread(target=loop, daemon=True)
        t.start()

        def done():
            stop.set()
            t.join(10)
            srv.close()
            for c in held:
                c.close()
        self.addCleanup(done)
        port = srv.getsockname()[1]
        self.write_endpoint(port)
        return port

    @staticmethod
    def _answers(first, after_go=None, delay=0.0):
        """A fake's behaviour: read the whole request, say `first`; then, on
        `go`, wait `delay` and say `after_go` (None: say nothing more)."""
        def behave(conn):
            conn.settimeout(10)
            buf = b""
            while buf.count(b"\0") < 10:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
            conn.sendall(first)
            if after_go is None:
                return
            got = b""
            while not got.endswith(b"go\n"):
                chunk = conn.recv(16)
                if not chunk:
                    return
                got += chunk
            time.sleep(delay)
            conn.sendall(after_go)
        return behave

    def test_a_slow_resident_runs_cold_after_the_wait(self):
        """A resident that takes the connection and never says `ready`: the
        client waits HELM_HOOK_RESIDENT_WAIT_S, names it, and the cold run
        answers, well inside the budget."""
        self._fake(lambda conn: None)
        env = dict(self.env, HELM_HOOK_RESIDENT_WAIT_S="0.3")
        rc, out, err = self.hook(REFUSE, env=env)
        crc, cout, cerr = self.hook(REFUSE, resident="off")
        self.assertEqual((rc, out), (crc, cout))
        line, _, rest = err.partition("\n")
        self.assertIn("did not answer within 0.3s", line)
        self.assertEqual(rest, cerr)

    def test_after_go_a_vanished_resident_is_an_unknown_outcome(self):
        """`go` hands the call over. A resident that then hangs up without an
        exit status is NOT a pass: the client says the outcome is unknown and
        the wrapper reports a failed guard (the call is unchecked)."""
        def ready_then_vanish(conn):
            self._answers(("ready %s\n" % PROOF).encode(), b"")(conn)
            conn.shutdown(socket.SHUT_RDWR)
        self._fake(ready_then_vanish)
        rc, out, err = self.hook(PASS)
        self.assertEqual(rc, 0)
        self.assertIn("outcome is UNKNOWN", err)
        self.assertIn("THE GUARD FAILED rc=70", err)
        self.assertIn("ALLOWED and UNCHECKED", out)

    def test_a_refusal_after_go_runs_the_call_cold(self):
        """F2. A resident that fails after `go` but before the hook touches
        stdin says `refuse PROOF WHY`: the guard has not run, so the client
        runs it cold, once, and names why."""
        self._fake(self._answers(
            ("ready %s\n" % PROOF).encode(),
            ("refuse %s could not take its caller's fd 2\n" % PROOF).encode()))
        rc, out, err = self.hook(REFUSE)
        crc, cout, cerr = self.hook(REFUSE, resident="off")
        self.assertEqual((rc, out), (crc, cout))
        line, _, rest = err.partition("\n")
        self.assertIn("could not take its caller's fd 2", line)
        self.assertIn("this call runs cold", line)
        self.assertEqual(rest, cerr)

    def test_an_answer_without_the_proof_is_never_trusted_nor_printed(self):  # noqa: VACUOUS_ASSERTION — the CONTROL refusal WITH the proof must be printed on the named line, beside the silent cold twins
        """F1. An endpoint outlives a resident killed without cleanup, and
        any local user may then listen on its port. Only the resident holds
        the endpoint's proof: an answer without it (a bare `ready`, another
        proof, a refusal) is never trusted and its text is never printed.
        The call runs cold without a word, exactly as the cold twin does."""
        other = "d" * 32
        for name, first, after in (
                ("a bare ready, then a pass", b"ready\n", b"rc 0\n"),
                ("another proof, then a pass",
                 ("ready %s\n" % other).encode(), b"rc 0\n"),
                ("a refusal without a proof", b"refuse PRINT-ME\n", None),
                ("a refusal with another proof",
                 ("refuse %s PRINT-ME\n" % other).encode(), None)):
            with self.subTest(case=name):
                self._fake(self._answers(first, after))
                self.assertEqual(self.hook(REFUSE),
                                 self.hook(REFUSE, resident="off"))
        # CONTROL: the same refusal WITH the proof is the resident's own.
        self._fake(self._answers(("refuse %s PRINT-ME\n" % PROOF).encode()))
        rc, out, err = self.hook(REFUSE)
        crc, cout, cerr = self.hook(REFUSE, resident="off")
        self.assertEqual((rc, out), (crc, cout))
        line, _, rest = err.partition("\n")
        self.assertIn("refused this call: PRINT-ME", line)
        self.assertEqual(rest, cerr)

    def test_a_caller_tmout_does_not_end_the_wait_after_go(self):  # noqa: VACUOUS_ASSERTION — rc must EQUAL 0, the answer the resident gives 1.5 s after go, where the cold run of the same payload is 2; the absent UNKNOWN only names the failure
        """F6. `bash -p` still honours TMOUT from the environment, and the
        read after `go` has no -t of its own: a caller's TMOUT=1 would end it
        after a second and report an UNKNOWN outcome while the resident is
        still running the hook. The client unsets it. The payload is one the
        cold run refuses (rc 2), so rc 0 can only be the resident's answer."""
        self._fake(self._answers(("ready %s\n" % PROOF).encode(),
                                 b"rc 0\n", delay=1.5))
        rc, out, err = self.hook(REFUSE, env=dict(self.env, TMOUT="1"))
        self.assertEqual(rc, 0, err)
        self.assertNotIn("UNKNOWN", err)
        self.assertNotIn("UNCHECKED", out)
        self.assertEqual(self.hook(REFUSE, resident="off")[0], 2)

    def test_a_wrong_token_gets_no_proof_and_is_refused_at_once(self):  # noqa: VACUOUS_ASSERTION — the right-token CONTROL grows the trace by one unconditionally before the wrong token is asserted not to, and the raw reply must start with refuse
        """AUTH. The endpoint is private (0600 in a 0700 directory). A call
        that carries another token is refused as soon as the token field
        arrives, and the refusal carries no proof: the client cannot tell it
        from a listener that is not the resident, so it runs cold without a
        word. The resident never ran it."""
        self.start_resident()
        ep = self.endpoint()
        self.assertEqual(stat.S_IMODE(os.stat(ep).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(ep)).st_mode),
                         0o700)
        port, _token, pid, start, proof = self.read_endpoint()
        # CONTROL: the right token is served.
        before = len(self.served())
        self.hook(PASS)
        self.assertEqual(len(self.served()), before + 1)
        with open(ep, "w") as f:
            f.write("%d %s %d %d %s\n" % (port, "0" * 32, pid, start, proof))
        for extra in (PASS, REFUSE):
            self.assertEqual(self.hook(extra), self.hook(extra, resident="off"))
        self.assertEqual(len(self.served()), before + 1,
                         "a call with the wrong token was run")
        # The raw protocol, from a process that is not a client at all: two
        # fields, and the connection held open. The refusal must come now,
        # not when a whole request (never sent) would have arrived.
        c = socket.create_connection(("127.0.0.1", port), timeout=10)
        self.addCleanup(c.close)
        c.sendall(b"helm-hookres/1\0" + b"1" * 32 + b"\0")
        c.settimeout(2.5)
        reply = c.makefile("rb").readline()
        self.assertTrue(reply.startswith(b"refuse "), reply)
        self.assertIn(b"token", reply)
        self.assertNotIn(proof.encode(), reply,
                         "the resident proved itself to a caller without "
                         "the token")

    def test_a_call_naming_another_process_is_refused(self):  # noqa: VACUOUS_ASSERTION — the reply must be a PROVEN refusal naming the victim pid, a positive observable
        """F4. A request names the pid whose stdio the child takes. The pid
        must hold THIS connection's client end on fd 3 (where the client
        opens it): a token holder that names another process, even one with
        its own connection to the resident on fd 3, is refused before that
        process's environment or stdio is touched."""
        self.start_resident()
        port, token, _pid, _start, proof = self.read_endpoint()
        victim = subprocess.Popen(
            ["bash", "-c", "exec 3<>/dev/tcp/127.0.0.1/%d; sleep 60; :"
             % port], env=self.env, cwd=self.cwd, stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)

        def stop():
            os.killpg(victim.pid, signal.SIGKILL)
            victim.wait()
            victim.stdin.close()
        self.addCleanup(stop)
        deadline = time.monotonic() + 30
        while True:
            try:
                if stat.S_ISSOCK(os.stat("/proc/%d/fd/3"
                                         % victim.pid).st_mode):
                    break
            except OSError:
                pass
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        c = socket.create_connection(("127.0.0.1", port), timeout=20)
        self.addCleanup(c.close)
        c.sendall(self.raw_request(token, victim.pid))
        reply = c.makefile("rb").readline()
        self.assertTrue(reply.startswith(("refuse %s " % proof).encode()),
                        reply)
        self.assertIn(b"pid %d" % victim.pid, reply)

    def test_an_environment_it_cannot_stand_in_for_runs_cold(self):  # noqa: VACUOUS_ASSERTION — each refusal line must NAME its key, a positive observable, beside the unchanged trace
        """A caller whose PYTHON* environment differs from the resident's
        would start a different interpreter, and one whose value differs for
        a key helm reads while it imports (F8: helm/__init__ reads
        HELM_GATE_LOADS_DIR before any warm import) would get the resident's
        value: refused, cold, named."""
        self.start_resident()
        with open(self.endpoint() + ".json") as f:
            self.assertIn("HELM_GATE_LOADS_DIR", json.load(f)["import_env"])
        for key, value in (("PYTHONWARNINGS", "ignore"),
                           ("HELM_GATE_LOADS_DIR", "x")):
            with self.subTest(key=key):
                env = dict(self.env, **{key: value})
                before = len(self.served())
                rc, out, err = self.hook(REFUSE, env=env)
                crc, cout, cerr = self.hook(REFUSE, env=env, resident="off")
                self.assertEqual((rc, out), (crc, cout))
                line, _, rest = err.partition("\n")
                self.assertIn(key, line)
                self.assertEqual(rest, cerr)
                self.assertEqual(len(self.served()), before)

    def test_a_resident_answering_after_the_wait_never_runs_the_call_too(self):  # noqa: VACUOUS_ASSERTION — every attempt asserts exactly one run in both branches (a named line: no serve and the cold twin's bytes; no line: one serve), and a run with no late answer is reported UNKNOWN, never a pass
        """F10, a REAL resident. With a wait shorter than any fork, the
        resident answers after the client has already run the call cold.
        Every attempt is exactly one run, sorted by the client's first stderr
        line:

        * a NAMED cold line (every one ends "this call runs cold"): the call
          ran cold, so the trace does not grow once the resident's children
          have finished, and the bytes are the cold twin's. Only "did not
          answer within" is a LATE answer, the case this arm exists for. At
          this wait the connect watchdog can fire first on a loaded host
          ("could not connect within"): one cold run too, and not late;
        * NO line: the resident served it, and the trace grew by one. A call
          with no line that the trace did not record ran cold in SILENCE; the
          failure says so and carries the resident's log (a re-exec, which
          unlinks the endpoint first, names itself there).

        The attempts are bounded by count. Whether one goes late is the
        scheduler's choice, not the code's, so a run with none is UNKNOWN
        (a skip that says F10 was not exercised): never green, never red."""
        proc = self.start_resident()
        env = dict(self.env, HELM_HOOK_RESIDENT_WAIT_S="0.0001")
        late = 0
        for _ in range(16):
            before = len(self.served())
            rc, out, err = self.hook(REFUSE, env=env)
            self.settle(proc)
            grew = len(self.served()) - before
            line, _, rest = err.partition("\n")
            if "this call runs cold" in line:
                self.assertEqual(grew, 0, "the resident ran a call the "
                                 "client had already run cold: %s" % line)
                crc, cout, cerr = self.hook(REFUSE, resident="off")
                self.assertEqual((rc, out, rest), (crc, cout, cerr))
                late += "did not answer within" in line
                if late >= 3:
                    break
                continue
            with open(self.resident_log) as f:
                log = f.read()
            self.assertEqual(grew, 1, "a call with no named line that the "
                             "resident did not serve ran cold in SILENCE; "
                             "stderr %r; resident log:\n%s" % (err, log))
        if not late:
            self.skipTest("UNKNOWN: none of 16 calls went late (each was "
                          "served, or ran cold for another named reason), "
                          "so F10 was not exercised on this host")


class StalledClientTest(ResidentFixture):

    STALL = ("if ! printf 'go\\n' >&3; then",
             "if ! { printf 'go\\n'; sleep 0.3; } >&3; then")

    def test_a_client_stalled_after_go_still_runs_the_guard_once(self):  # noqa: VACUOUS_ASSERTION — each call must GROW the trace by one and equal its cold twin, and the stall patch must have applied exactly once
        """F2. `printf 'go' >&3` points the client's fd 1 at the socket for
        the length of the builtin. A client stalled there (a throttled box)
        longer than any settle wait must still be served exactly once: the
        child keeps the fds 0 and 1 it took at admission and takes only fd 2
        after `go`. A copy of this tree whose client sleeps 0.3 s with fd 1
        on the socket, and the cold twin, answer alike."""
        old, new = self.STALL

        def stall(text):
            self.assertEqual(text.count(old), 1, "the go write moved")
            return text.replace(old, new)
        tree = self.copy_tree(client=stall)
        self.start_resident(root=tree)
        for name, extra in (("steer", STEER), ("refuse", REFUSE)):
            with self.subTest(case=name):
                before = len(self.served())
                warm = self.hook(extra, root=tree)
                self.assertEqual(len(self.served()), before + 1,
                                 "the stalled call was not served: %r"
                                 % (warm,))
                cold = self.hook(extra, root=tree, resident="off")
                self.assertEqual(warm, cold)


class WebRestartTest(ResidentFixture):

    def test_a_call_in_flight_survives_its_units_sigterm(self):  # noqa: VACUOUS_ASSERTION — the call must EQUAL its cold twin's rc 2 and bytes, a positive observable
        """ROUND 3 (c). Served hooks run in helm-web.service's cgroup, and a
        web restart sends SIGTERM to every process in it. A child that has
        taken the call finishes it: it ignores SIGTERM from `ready` on. The
        call is held in the hook here (its payload is not written until
        after the signal), the resident's whole process group gets SIGTERM,
        and the call still answers what the cold run answers, never an
        UNKNOWN outcome that allows it unchecked."""
        proc = self.start_resident(session=True)
        payload = json.dumps(dict(REFUSE, session_id=_session(),
                                  hook_event_name="PreToolUse", cwd=self.cwd))
        cmd = "%s gate argv-guard PreToolUse 60 %s %s chat argv-guard " \
              "--hook-json" % (shlex.quote(os.path.join(ROOT, "bin",
                                                        "helm-hook")),
                               shlex.quote("tool call"),
                               shlex.quote(os.path.join(ROOT, "bin", "helm")))
        before = len(self.served())
        call = subprocess.Popen(["sh", "-c", cmd], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                env=dict(self.env, HELM_HOOK_RESIDENT="on"),
                                cwd=self.cwd)
        self.addCleanup(lambda: call.poll() is None and call.kill())
        deadline = time.monotonic() + 60
        while len(self.served()) == before:
            if call.poll() is not None:
                self.fail("the call ended before it was served: %r"
                          % (call.communicate(),))
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=30)
        out, err = call.communicate(payload, timeout=120)
        warm = (call.returncode, out, err)
        self.assertEqual(warm, self.hook(REFUSE, resident="off"))
        self.assertEqual(warm[0], 2)


class FollowsItsCodeTest(ResidentFixture):

    def git(self, tree, *args):
        subprocess.run(["git", "-C", tree, "-c", "user.name=t",
                        "-c", "user.email=t@example.invalid",
                        "-c", "commit.gpgsign=false"] + list(args),
                       check=True, capture_output=True, timeout=120)

    def test_a_changed_tree_is_never_served_from_older_code(self):  # noqa: VACUOUS_ASSERTION — the base call must grow the trace, and served_new must turn True (a served call carrying the new text) or the arm fails
        """A checkout (a git copy of this tree) gets a resident; a commit
        then changes its refusal text, the way a land moves trunk. Every
        call the resident serves after that commit carries the new text,
        including the second before its poll notices (the HEAD witness
        refuses those), and the resident re-execs in place: same pid, new
        token and proof."""
        tree = self.copy_tree()
        self.git(tree, "init", "-q")
        self.git(tree, "add", "-A")
        self.git(tree, "commit", "-q", "-m", "base")
        self.start_resident(root=tree)
        _port0, token0, pid0, _start0, proof0 = self.read_endpoint(tree)
        before = len(self.served())
        rc, _out, err = self.hook(REFUSE, root=tree)
        self.assertEqual(rc, 2)
        self.assertEqual(len(self.served()), before + 1)
        self.assertNotIn("REEXEC-MARK", err)
        with open(os.path.join(tree, "helm", "chat.py"), "a") as f:
            f.write("\n\n_hookres_test_message = agent_model_message\n\n\n"
                    "def agent_model_message(model):\n"
                    "    return 'REEXEC-MARK ' + "
                    "_hookres_test_message(model)\n")
        self.git(tree, "commit", "-q", "-am", "change")
        deadline = time.monotonic() + 120
        served_new = False
        while time.monotonic() < deadline and not served_new:
            before = len(self.served())
            rc, _out, err = self.hook(REFUSE, root=tree)
            self.assertEqual(rc, 2, err)
            if len(self.served()) > before:
                self.assertIn("REEXEC-MARK", err,
                              "the resident served a call from older code")
                served_new = True
            else:
                time.sleep(0.2)
        self.assertTrue(served_new, "the resident never served the new tree")
        _port1, token1, pid1, _start1, proof1 = self.read_endpoint(tree)
        self.assertEqual(pid1, pid0, "it restarted instead of re-exec'ing")
        self.assertNotEqual(token1, token0)
        self.assertNotEqual(proof1, proof0)


class ResidentUnitTest(unittest.TestCase):

    def test_an_import_that_reads_the_environment_is_recorded(self):  # noqa: VACUOUS_ASSERTION — the recorded key and CWD are asserted present before the quiet module's empty set
        tmp = tempfile.mkdtemp(prefix="helm-test-hookres-warm-")
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, "hookres_warm_probe.py"), "w") as f:
            f.write("import os\n"
                    "A = os.environ.get('HELM_HOOKRES_PROBE_KEY')\n"
                    "B = os.getcwd()\n")
        sys.path.insert(0, tmp)
        self.addCleanup(sys.path.remove, tmp)
        self.addCleanup(sys.modules.pop, "hookres_warm_probe", None)
        keys = hookres._warm(("hookres_warm_probe",))
        self.assertIn("HELM_HOOKRES_PROBE_KEY", keys)
        self.assertIn(hookres.CWD, keys)
        self.assertIs(type(os.environ), os._Environ, "the recorder stayed")
        # CONTROL: a module that reads nothing records nothing.
        with open(os.path.join(tmp, "hookres_warm_quiet.py"), "w") as f:
            f.write("X = 1\n")
        self.addCleanup(sys.modules.pop, "hookres_warm_quiet", None)
        self.assertEqual(hookres._warm(("hookres_warm_quiet",)), frozenset())

    def test_import_time_reads_are_recorded_from_the_first_line(self):  # noqa: VACUOUS_ASSERTION — the probe's keys must EQUAL the in-process recorder's on the same module, and helm's own pre-import read must be present
        """F8. By the time the resident warms, helm, cli, hooks and hookres
        are already imported, and what they read while importing escaped
        `_warm`. `_import_env` imports in a fresh interpreter with the
        recorder installed before its first import, so helm/__init__'s
        HELM_GATE_LOADS_DIR read is seen; on a plain module it records what
        the in-process recorder records."""
        tmp = tempfile.mkdtemp(prefix="helm-test-hookres-probe-")
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, "hookres_env_probe.py"), "w") as f:
            f.write("import os\n"
                    "A = os.environ.get('HELM_HOOKRES_PROBE_KEY')\n"
                    "B = os.getcwd()\n")
        keys, why = hookres._import_env(("hookres_env_probe",), paths=(tmp,))
        self.assertIsNone(why)
        sys.path.insert(0, tmp)
        self.addCleanup(sys.path.remove, tmp)
        self.addCleanup(sys.modules.pop, "hookres_env_probe", None)
        self.assertEqual(keys, hookres._warm(("hookres_env_probe",)))
        keys, why = hookres._import_env(("helm",))
        self.assertIsNone(why)
        self.assertIn("HELM_GATE_LOADS_DIR", keys)
        keys, why = hookres._import_env(("hookres_no_such_module",))
        self.assertIsNone(keys)
        self.assertIn("hookres_no_such_module", why or "")

    def test_a_head_that_moves_during_the_digest_walk_is_not_taken(self):
        """F3. `_poll` walks the code digest and reads the HEAD witness. A
        land that writes an already-walked file and then moves its ref during
        the walk leaves the digest unmoved; the witness it keeps must be the
        one read BEFORE the walk, so the next call sees HEAD differ and is
        refused. A later poll with the code still unmoved (a packed-refs
        rewrite) takes the new witness."""
        heads = ["H0"]

        class Walk(object):
            def code_moved(self):
                heads[0] = "H1"
                return None

            def stale(self):
                return False
        with mock.patch.dict(os.environ, {"HELM_HOME": tempfile.gettempdir()}):
            r = hookres.Resident()
        r.root = os.path.join(tempfile.gettempdir(), "hookres-no-root")
        r.follower = Walk()
        r.witness = "H0"
        with mock.patch.object(hookres.stopfacts, "head_witness",
                               lambda root: (None, heads[0])):
            r._poll()
            self.assertEqual(r.witness, "H0", "a HEAD read after the walk "
                             "was taken as the code this process loaded")
            r._poll()
            self.assertEqual(r.witness, "H1")

    def test_a_request_is_bounded_as_a_whole_and_its_token_at_once(self):  # noqa: VACUOUS_ASSERTION — each arm must RAISE Refused naming its reason, a positive observable
        """F5. A peer that trickles one byte at a time is refused when the
        whole request's deadline passes, not when one recv waits too long:
        the refusal names the request's bound and comes while the trickle
        (twenty seconds of bytes against a 0.3 s deadline) is still going.
        A wrong token is refused the moment its field arrives: with the
        connection held open and a 30 s deadline, the refusal names the
        token, not the deadline."""
        token = b"t" * 32
        a, b = socket.socketpair()
        stop, finished = threading.Event(), threading.Event()

        def trickle():
            try:
                b.sendall(hookres.PROTO.encode() + b"\0" + token + b"\0")
                for _ in range(400):
                    if stop.is_set():
                        return
                    b.sendall(b"1")
                    stop.wait(0.05)
            except OSError:
                return
            finished.set()
        t = threading.Thread(target=trickle, daemon=True)
        t.start()

        def done():
            stop.set()
            t.join(10)
            a.close()
            b.close()
        self.addCleanup(done)
        with self.assertRaises(hookres.Refused) as cm:
            hookres._request(a, token, time.monotonic() + 0.3)
        self.assertIn("no complete request within", str(cm.exception))
        self.assertFalse(finished.is_set(), "refused only when the trickle "
                         "ended, not at the request's deadline")
        c, d = socket.socketpair()
        self.addCleanup(c.close)
        self.addCleanup(d.close)
        d.sendall(hookres.PROTO.encode() + b"\0" + b"x" * 32 + b"\0")
        with self.assertRaises(hookres.Refused) as cm:
            hookres._request(c, token, time.monotonic() + 30)
        self.assertIn("token", str(cm.exception))

    def test_what_a_caller_environment_may_differ_in(self):
        env0 = {"HOME": "/h", "LANG": "en_US.UTF-8", "OTHER": "a"}
        keys = frozenset({"HOME"})
        self.assertIsNone(hookres.differs(dict(env0), env0, keys))
        self.assertIsNone(hookres.differs(dict(env0, OTHER="b",
                                               HELM_CHAT_NAME="x"),
                                          env0, keys),
                          "a key no import read is the caller's own")
        self.assertIsNone(hookres.differs(dict(env0, LANG="C.UTF-8"),
                                          env0, keys),
                          "the same character set")
        for env, what in ((dict(env0, HOME="/other"), "HOME"),
                          (dict(env0, PYTHONPATH="/x"), "PYTHONPATH"),
                          (dict(env0, TZ="UTC"), "TZ"),
                          (dict(env0, LANG="C"), "locale")):
            self.assertIn(what, hookres.differs(env, env0, keys) or "")
        self.assertIsNotNone(hookres.differs(dict(env0, OTHER="b"), env0,
                                             frozenset({hookres.ALL})))

    def test_the_import_check_takes_the_modules_it_is_given(self):  # noqa: VACUOUS_ASSERTION — the missing module's refusal must say it does not import, a positive observable beside the None
        self.assertIsNone(stopfacts_resident._preflight(("helm.hookres",)))
        why = stopfacts_resident._preflight(("helm.hookres_no_such_module",))
        self.assertIn("does not import", why or "")
        self.assertEqual(hookres.Follower.PREFLIGHT, hookres.PREFLIGHT)
        self.assertIsNone(stopfacts_resident.Follower.PREFLIGHT)

    def test_the_supervisor_starts_the_interpreter_the_hooks_run(self):  # noqa: VACUOUS_ASSERTION — each of the three picks is asserted EQUAL to a named interpreter, never an absence
        """`interpreter` reads bin/helm-hook's record: the entry for this
        PATH's python3, else the newest usable one, else this interpreter."""
        tmp = tempfile.mkdtemp(prefix="helm-test-hookres-interp-")
        self.addCleanup(shutil.rmtree, tmp, True)
        shim = os.path.join(tmp, "bin", "python3")
        os.makedirs(os.path.dirname(shim))
        with open(shim, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(shim, 0o755)
        rec = os.path.join(tmp, "home", "_global", ".state", "hook-interp")
        os.makedirs(os.path.dirname(rec))
        other = shutil.which("sh")
        saved = {k: os.environ.get(k) for k in ("PATH", "HELM_HOME")}

        def restore():
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(restore)
        os.environ["PATH"] = os.path.dirname(shim) + os.pathsep + \
            os.path.dirname(other)
        os.environ["HELM_HOME"] = os.path.join(tmp, "home")
        with open(rec, "w") as f:
            f.write("%s\t%s\n/elsewhere/python3\t%s\n"
                    % (shim, sys.executable, other))
        self.assertEqual(hookres.interpreter(), sys.executable,
                         "this PATH's entry wins over a newer one")
        with open(rec, "w") as f:
            f.write("/elsewhere/python3\t%s\n/gone/python3\t/gone/python\n"
                    % other)
        self.assertEqual(hookres.interpreter(), other,
                         "the newest USABLE entry, when this PATH's is absent")
        os.unlink(rec)
        self.assertEqual(hookres.interpreter(), sys.executable)

    def test_the_supervisor_stops_on_a_host_that_refuses(self):  # noqa: VACUOUS_ASSERTION — the loop must END after exactly one start and say so once, positive observables
        """ROUND 3 (b), the supervisor's half: a resident that exits
        HOST_REFUSED is not restarted (the host will refuse it every time);
        the supervisor says so once and ends."""
        tmp = tempfile.mkdtemp(prefix="helm-test-hookres-sup-")
        self.addCleanup(shutil.rmtree, tmp, True)
        runs = os.path.join(tmp, "runs")
        fake = os.path.join(tmp, "python3")
        with open(fake, "w") as f:
            f.write("#!/bin/sh\necho x >> %s\nexit %d\n"
                    % (shlex.quote(runs), hookres.HOST_REFUSED))
        os.chmod(fake, 0o755)
        stop = threading.Event()
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"HELM_HOME": tmp}), \
                mock.patch.object(hookres, "interpreter", lambda: fake), \
                mock.patch.object(hookres, "RESTART_S", (0.01, 1.0)), \
                contextlib.redirect_stderr(err):
            t = threading.Thread(target=hookres._supervise_loop,
                                 args=(0.01,), kwargs={"stop": stop},
                                 daemon=True)
            t.start()
            t.join(30)
            stop.set()
            t.join(30)
        with open(runs) as f:
            self.assertEqual(len(f.read().splitlines()), 1)
        self.assertEqual(err.getvalue().count("\n"), 1, err.getvalue())
        self.assertIn("--status", err.getvalue())

    def test_only_the_console_web_keeps_one_alive(self):
        from helm.web_common import DEFAULT_PORT
        saved = {k: os.environ.pop(k, None)
                 for k in ("HELM_HOOK_RESIDENT", "MELD_HOOK_RESIDENT")}
        self.addCleanup(lambda: [os.environ.__setitem__(k, v)
                                 for k, v in saved.items() if v is not None])
        self.assertTrue(hookres.enabled(DEFAULT_PORT))
        self.assertFalse(hookres.enabled(DEFAULT_PORT + 1))
        os.environ["HELM_HOOK_RESIDENT"] = "off"
        self.addCleanup(os.environ.pop, "HELM_HOOK_RESIDENT", None)
        self.assertFalse(hookres.enabled(DEFAULT_PORT))
        self.assertIsNone(hookres.supervise(DEFAULT_PORT))
        os.environ["HELM_HOOK_RESIDENT"] = "on"
        self.assertTrue(hookres.enabled(0))


class StatusTest(ResidentFixture):

    def test_status_says_serving_or_not_and_a_second_resident_stands_down(self):
        run = [sys.executable, "-S", os.path.join(ROOT, "bin", "helm"),
               "hooks", "resident"]
        r = subprocess.run(run + ["--status"], env=self.env,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("not running", r.stdout)
        self.start_resident()
        r = subprocess.run(run + ["--status"], env=self.env,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("serving", r.stdout)
        second = subprocess.run(run, env=self.env, capture_output=True,
                                text=True, timeout=120,
                                stdin=subprocess.DEVNULL)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("already served", second.stderr)

    def test_a_serving_resident_keeps_its_heartbeat_and_takes_it_away(self):  # noqa: VACUOUS_ASSERTION — the beat must be an epoch second that ADVANCES while it serves, a positive observable, before its removal is asserted
        """ROUND 3 (a), the resident's half: it writes the epoch second to
        <endpoint>.beat when it starts and on every poll, and removes it with
        its endpoint when it stops."""
        proc = self.start_resident()
        beat = self.endpoint() + ".beat"

        def read():
            with open(beat) as f:
                return int(f.read())
        first = read()
        deadline = time.monotonic() + 30
        while read() == first:
            self.assertLess(time.monotonic(), deadline,
                            "the heartbeat never advanced")
            time.sleep(0.1)
        self.assertGreater(read(), first)
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=30)
        self.assertFalse(os.path.exists(self.endpoint()))
        self.assertFalse(os.path.exists(beat))

    def test_a_host_that_refuses_pidfd_getfd_starts_no_resident(self):  # noqa: VACUOUS_ASSERTION — the resident must EXIT with HOST_REFUSED naming pidfd_getfd, and status must name it, positive observables beside the silent cold call
        """ROUND 3 (b). Where the kernel refuses pidfd_getfd on another
        process (kernel.yama.ptrace_scope>=1 refuses every non-descendant),
        the resident can take no socket stdio, which is what Claude Code
        gives a hook: serving would only slow every call and print a false
        line. So it does not start. It probes pidfd_getfd against its PARENT
        (a process that is not its descendant; a probe against itself always
        passes), exits HOST_REFUSED with one line naming pidfd_getfd, and
        writes no endpoint, so every call runs cold in silence, as with no
        resident. Played by a parent that made itself non-dumpable, which
        the kernel refuses on any host. Its status says why."""
        run = [sys.executable, "-S", os.path.join(ROOT, "bin", "helm"),
               "hooks", "resident"]
        log = self.j("refused.log")
        with open(log, "w") as errf:
            proc = subprocess.Popen(
                [sys.executable, "-S", "-c", NONDUMPABLE] + run,
                env=dict(self.env, HELM_HOOK_RESIDENT_TRACE=self.trace),
                cwd=self.cwd, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=errf,
                start_new_session=True)

            def stop():
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGTERM)
                    proc.wait()
            self.addCleanup(stop)
            deadline = time.monotonic() + 90
            while proc.poll() is None:
                self.assertFalse(os.path.exists(self.endpoint()),
                                 "the resident started on a host that "
                                 "refuses pidfd_getfd")
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.05)
        with open(log) as f:
            said = f.read()
        self.assertEqual(proc.returncode, hookres.HOST_REFUSED, said)
        self.assertEqual(sum("pidfd_getfd" in ln
                             for ln in said.splitlines()), 1, said)
        self.assertFalse(os.path.exists(self.endpoint()))
        self.assertEqual(self.hook(REFUSE), self.hook(REFUSE, resident="off"))
        r = subprocess.run([sys.executable, "-S", "-c", NONDUMPABLE] + run
                           + ["--status"], env=self.env, capture_output=True,
                           text=True, timeout=120)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("pidfd_getfd", r.stdout)


if __name__ == "__main__":
    unittest.main()
