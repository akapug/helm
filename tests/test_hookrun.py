"""helm hooks run — the one-spawn dispatcher (task/630).

WHAT THESE ARMS ARE FOR. Collapsing four hook processes into one process moves
three properties from the OPERATING SYSTEM, which enforced them for free, into
helm code, which now has to. The OS gave us: a rc that could only be swallowed
by an explicit `|| true`, a `timeout` per process, and total fault isolation
because a crash killed only its own process. Every arm below exists because one
of those three is now a line of Python that can be edited away.

Each composition hazard is driven through a STUB `cli.main`. Timer regressions
also route that stub through the real recorder and deliver catchers, with all
owner operations synthetic: cancellation must survive the handlers it crosses.
One integration arm at the end proves the real path with a real verb.
"""

import contextlib
import fcntl
import io
import json
import os
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
# A gate's escaping refusal appends to the friction ledger under the helm
# home, and the arms below drive that escape in-process.
_tmp_home(prefix="helm-test-hookrun-", var="HELM_HOME")

from helm import hookrun  # noqa: E402


def _spec(name, event="E", timeout=2, gate=False, args=None):
    s = {"name": name, "event": event, "args": args or name,
         "timeout": timeout, "matcher": None}
    if gate:
        s["gate"] = True
    return s


class _Stub(object):
    """Replaces cli.main. Each spec's args name the hazard it should perform."""

    def __init__(self):
        self.ran = []
        self.stdin_seen = {}
        self.cwd_seen = {}      # cwd as each handler was HANDED it
        self.sig_seen = {}      # the SIGALRM handler in force during the call
        self.env_seen = {}      # HELM_NO_TREE_WARNING as the handler saw it

    def __call__(self, argv):
        import signal as _sig
        name = argv[0]
        self.ran.append(name)
        self.cwd_seen[name] = os.getcwd()
        self.sig_seen[name] = _sig.getsignal(_sig.SIGALRM)
        self.env_seen[name] = os.environ.get("HELM_NO_TREE_WARNING")
        # EXACTLY ONE READ PER HANDLER, text OR bytes, never both. A real
        # handler reads stdin one way; reading it twice here consumed the
        # stream in the text read and handed the byte branch an empty string,
        # which failed the two arms written to prove the byte path WORKS. The
        # dispatcher was fine — my probe used a byte-only stub, so it passed
        # locally while the suite's dual-read stub failed on the fab.
        if name == "bufread":
            self.stdin_seen[name] = sys.stdin.buffer.read().decode()
        else:
            self.stdin_seen[name] = sys.stdin.read()
        if name == "raise":
            raise RuntimeError("handler blew up")
        if name == "hang":
            time.sleep(30)          # far past any budget below
            return 0
        if name == "exit2":
            return 2
        if name == "exit1":
            return 1
        if name == "sysexit2":
            raise SystemExit(2)
        if name in ("sigprobe", "envprobe"):
            return 0
        if name == "bufread":
            return 0
        if name == "chdir":
            os.chdir("/")
            return 0
        if name == "raiser":
            raise RuntimeError("synthetic handler failure")
        if name == "badexit":
            # A handler that exits with a code helm cannot read. This was the
            # ONLY unchecked path that wrote nothing to stderr at all.
            raise SystemExit(object())
        if name == "unnameable":
            # NEITHER repr NOR type-name works on this one, so a diagnostic
            # that falls back from the first to the second still runs code
            # the handler chose.
            raise SystemExit(_Unnameable())
        if name == "hostilerepr":
            # AND A CODE WHOSE OWN __repr__ RAISES. The diagnostic that made
            # the silent path speak calls `%r` on this object, so the object
            # decides whether this module survives printing its own message.
            raise SystemExit(_Hostile())
        return 0


class _Hostile:
    """An exit code whose repr raises. Not exotic: any object a handler exits
    with runs ITS code inside this module's diagnostic."""

    def __repr__(self):
        raise RuntimeError("this repr refuses")


class _HostileMeta(type):
    """A metaclass whose `__name__` raises.

    `__name__` is an attribute the METACLASS owns, so a diagnostic that guards
    `repr()` and falls back to `type(x).__name__` is still running code the
    handler chose — the foreign call moves one frame in rather than leaving."""

    @property
    def __name__(cls):
        raise RuntimeError("this type refuses to name itself")


class _Unnameable(metaclass=_HostileMeta):
    def __repr__(self):
        raise RuntimeError("this repr refuses")


class HookRunTest(unittest.TestCase):

    def setUp(self):
        from helm import cli
        self._real_main = cli.main
        self.stub = _Stub()
        cli.main = self.stub
        self.addCleanup(setattr, cli, "main", self._real_main)

    # ---- teeth ----------------------------------------------------------
    def test_gate_exit2_blocks(self):
        """A GATE returning 2 must reach the harness as 2. This is the whole
        enforcement layer: `|| true` swallowing this exact code is the bug that
        disarmed helm's only gate for its entire life."""
        rc = hookrun.run_event("E", payload="{}", specs=[_spec("exit2", gate=True)])
        self.assertEqual(rc, 2)

    def test_non_gate_exit2_does_not_block(self):
        """An ADVISORY handler returning 2 must NOT block — today its `|| true`
        swallows it. A dispatcher that took max(rc) across handlers would arm
        every advisory hook into a blocker, silently, fleet-wide."""
        # CONTROL, same observable (rc), mirror condition: the identical spec
        # marked gate=True DOES return 2, so the 0 below is a decision about
        # gate-ness and not a dead rc channel.
        self.assertEqual(
            hookrun.run_event("E", payload="{}", specs=[_spec("exit2", gate=True)]), 2)
        rc = hookrun.run_event("E", payload="{}", specs=[_spec("exit2", gate=False)])
        self.assertEqual(rc, 0)

    def test_gate_blocks_even_when_a_later_handler_is_clean(self):
        """The refusal must survive its siblings' success, and every sibling
        must still have run."""
        specs = [_spec("exit2", gate=True), _spec("ok")]
        rc = hookrun.run_event("E", payload="{}", specs=specs)
        self.assertEqual(rc, 2)
        self.assertEqual(self.stub.ran, ["exit2", "ok"])

    def test_gate_swallows_every_code_except_2(self):
        """spec_command's contract, verbatim: 'A gate propagates ONLY rc 2 and
        swallows the rest.' A crash or a missing binary must still fail open."""
        # CONTROL first and UNCONDITIONAL (not inside the loop): a gate CAN
        # reach 2 through this exact path, so the zeros below are swallowed
        # codes rather than an rc channel that never carries anything.
        self.assertEqual(
            hookrun.run_event("E", payload="{}", specs=[_spec("exit2", gate=True)]), 2)
        for name in ("exit1", "ok"):
            rc = hookrun.run_event("E", payload="{}", specs=[_spec(name, gate=True)])
            self.assertEqual(rc, 0, "%s must not block" % name)

    def test_sysexit_is_honoured_as_a_return(self):
        """A handler that calls sys.exit(2) inside our process raises SystemExit
        rather than returning — untrapped it would kill the dispatcher and skip
        every sibling."""
        specs = [_spec("sysexit2", gate=True), _spec("ok")]
        rc = hookrun.run_event("E", payload="{}", specs=specs)
        self.assertEqual(rc, 2)
        self.assertIn("ok", self.stub.ran)

    # ---- isolation ------------------------------------------------------
    def test_raising_handler_does_not_silence_siblings(self):   # noqa: VACUOUS_ASSERTION — the observable IS the call sequence; a spy is the only instrument for 'the siblings still ran', and its mirror control is test_gate_blocks_even_when_a_later_handler_is_clean
        """THE composition failure this fleet paid four processes to avoid."""
        specs = [_spec("raise"), _spec("ok"), _spec("ok2", args="ok2")]
        rc = hookrun.run_event("E", payload="{}", specs=specs)
        self.assertEqual(self.stub.ran, ["raise", "ok", "ok2"])
        self.assertEqual(rc, 0)

    def test_raising_gate_still_lets_a_later_gate_block(self):
        """A crash in the first handler must not disarm the second's teeth."""
        specs = [_spec("raise", gate=True), _spec("exit2", gate=True)]
        rc = hookrun.run_event("E", payload="{}", specs=specs)
        self.assertEqual(rc, 2)

    def test_chdir_does_not_leak_to_the_next_handler(self):   # noqa: VACUOUS_ASSERTION — the positive control is the cwd DURING dispatch, which is observable only from inside a handler; cwd_seen is that reading, not instrumentation
        """Separate processes made cwd leakage impossible; one process does not."""
        here = os.getcwd()
        self.assertNotEqual(here, "/")   # the handler chdirs to "/"
        hookrun.run_event("E", payload="{}", specs=[_spec("chdir"), _spec("ok")])
        # CONTROL on the SAME observable: the second handler recorded the cwd it
        # was handed, and it is `here` — so cwd was genuinely restored BETWEEN
        # handlers, not merely restored by the time the test looked.
        self.assertEqual(self.stub.cwd_seen.get("ok"), here)
        self.assertEqual(self.stub.cwd_seen.get("chdir"), here)
        self.assertEqual(os.getcwd(), here)

    def test_stdin_is_replayed_to_every_handler(self):   # noqa: VACUOUS_ASSERTION — stdin_seen IS the observable, not instrumentation — an empty replay is indistinguishable from success except by reading what each handler got
        """Each handler does its own sys.stdin.read(). Read once and NOT
        replayed, the second sees '' and silently records nothing — a failure
        that looks exactly like success."""
        specs = [_spec("a", args="a"), _spec("b", args="b")]
        hookrun.run_event("E", payload='{"k": 1}', specs=specs)
        self.assertEqual(self.stub.ran, ["a", "b"])     # control: both ran
        self.assertEqual(self.stub.stdin_seen["a"], '{"k": 1}')
        self.assertEqual(self.stub.stdin_seen["b"], '{"k": 1}')

    def test_stdin_replay_supports_the_BYTE_reader(self):   # noqa: VACUOUS_ASSERTION — stdin_seen IS the observable; the mirror control is the text reader in test_stdin_is_replayed_to_every_handler, which passed throughout the defect
        """THE ARM THIS SUITE WAS MISSING, and the defect it now pins was real.

        The replay object was io.StringIO, which has no `.buffer`.
        seats_cli._hook_stdin reads `sys.stdin.buffer`, raises AttributeError,
        catches its own exception, falls through to a plain reader that reads
        `.buffer` AGAIN, catches again, and RETURNS {}. So deliver, join,
        delegation-stop and stop-guard would each receive an empty payload,
        no-op, and exit 0 — four guards silently disarmed while the dispatcher
        reported success. A text-only arm cannot see it: `record` reads text and
        passed the whole time.
        """
        specs = [_spec("bufread", args="bufread")]
        hookrun.run_event("E", payload='{"session_id": "abc"}', specs=specs)
        self.assertEqual(self.stub.ran, ["bufread"])    # control: it really ran
        self.assertEqual(self.stub.stdin_seen["bufread"], '{"session_id": "abc"}')

    def test_replay_serves_text_and_bytes_to_successive_handlers(self):   # noqa: VACUOUS_ASSERTION — stdin_seen IS the observable for both reader styles
        """Both reader styles, same event, each getting the full payload — the
        two live PostToolUse handlers are one of each."""
        specs = [_spec("bufread", args="bufread"), _spec("t", args="t")]
        hookrun.run_event("E", payload='{"k": 2}', specs=specs)
        self.assertEqual(self.stub.ran, ["bufread", "t"])   # control: both ran
        self.assertEqual(self.stub.stdin_seen["bufread"], '{"k": 2}')
        self.assertEqual(self.stub.stdin_seen["t"], '{"k": 2}')

    def test_sigalrm_handler_is_restored_even_when_none_was_installed(self):   # noqa: VACUOUS_ASSERTION — the positive control is the SIGALRM handler DURING dispatch (ours, proving a restore was owed), observable only from inside a handler
        """signal.signal() returns None when the previous handler did not come
        from Python. Guarding the restore on `is not None` left OUR _alarm
        installed process-wide, free to raise into unrelated code later."""
        import signal
        signal.signal(signal.SIGALRM, signal.SIG_DFL)
        hookrun.run_event("E", payload="{}", specs=[_spec("sigprobe", args="sigprobe", timeout=5)])
        # CONTROL on the SAME observable: the handler recorded the SIGALRM
        # handler it was running under, and it was OURS — so a restore was
        # genuinely owed, and SIG_DFL below is a restore rather than a state
        # that was never disturbed.
        self.assertIs(self.stub.sig_seen.get("sigprobe"), hookrun._alarm)
        self.assertEqual(signal.getsignal(signal.SIGALRM), signal.SIG_DFL)

    def test_tree_warning_is_suppressed_for_the_whole_dispatch(self):   # noqa: VACUOUS_ASSERTION — env_seen IS the observable; its restore mirror is test_tree_warning_env_is_restored_after_dispatch
        """cli.main runs which_helm_warning() — two git subprocesses — on every
        call. Left on, a 2-handler event pays 4 extra process spawns, re-buying
        the cost this module exists to remove."""
        seen = {}

        def probe(argv):
            seen[argv[0]] = os.environ.get("HELM_NO_TREE_WARNING")
            return 0

        from helm import cli
        cli.main = probe
        hookrun.run_event("E", payload="{}", specs=[_spec("a", args="a")])
        self.assertEqual(seen["a"], "1")

    def test_tree_warning_env_is_restored_after_dispatch(self):   # noqa: VACUOUS_ASSERTION — the positive control is the env value DURING dispatch, observable only from inside a handler; asserting it here would be asserting the restore twice
        prior = os.environ.get("HELM_NO_TREE_WARNING")
        os.environ.pop("HELM_NO_TREE_WARNING", None)
        hookrun.run_event("E", payload="{}", specs=[_spec("envprobe", args="envprobe")])
        # CONTROL on the SAME observable: it WAS "1" while the handler ran, so
        # None afterwards is a restore and not a variable nobody ever set.
        self.assertEqual(self.stub.env_seen.get("envprobe"), "1")
        self.assertIsNone(os.environ.get("HELM_NO_TREE_WARNING"))
        if prior is not None:
            os.environ["HELM_NO_TREE_WARNING"] = prior

    # ---- timeouts -------------------------------------------------------
    def test_hung_handler_is_cut_at_its_own_budget(self):
        """A hung handler must not hang the tool call. Bounded at ITS budget,
        not the event's, and the sibling still runs on its own."""
        specs = [_spec("hang", timeout=1), _spec("ok")]
        t0 = time.time()
        rc = hookrun.run_event("E", payload="{}", specs=specs)
        elapsed = time.time() - t0
        self.assertEqual(rc, 0)                      # timeout fails OPEN, as today
        self.assertGreater(elapsed, 0.5, "the hang handler never actually blocked")
        self.assertLess(elapsed, 10, "the 30s sleep was not cut")
        self.assertIn("ok", self.stub.ran, "a hung handler silenced its sibling")

    def test_hung_gate_fails_open_and_says_so(self):
        """rc 124's shape. Fail-open is the law; SILENCE was the bug — a guard
        that never ran must not look like one that ran and found nothing."""
        err = io.StringIO()
        real, sys.stderr = sys.stderr, err
        try:
            rc = hookrun.run_event("E", payload="{}",
                                   specs=[_spec("hang", timeout=1, gate=True)])
        finally:
            sys.stderr = real
        self.assertEqual(rc, 0)
        self.assertIn("TIMED OUT", err.getvalue())
        self.assertIn("UNCHECKED", err.getvalue())

    def test_crash_says_so_too(self):
        """Same law for a crash: allowed, but never in silence."""
        err = io.StringIO()
        real, sys.stderr = sys.stderr, err
        try:
            hookrun.run_event("E", payload="{}", specs=[_spec("raise", gate=True)])
        finally:
            sys.stderr = real
        self.assertIn("HANDLER FAILED", err.getvalue())

    def test_one_handlers_timeout_does_not_consume_the_next_ones(self):
        """Budgets are PER handler. After a handler is cut at 1s, the next must
        still get its own full budget rather than inheriting a spent clock."""
        specs = [_spec("hang", timeout=1), _spec("hang2", args="hang", timeout=1)]
        t0 = time.time()
        hookrun.run_event("E", payload="{}", specs=specs)
        elapsed = time.time() - t0
        self.assertGreater(elapsed, 1.5, "the second handler was not given its own budget")
        self.assertLess(elapsed, 10)

    def test_alarm_is_disarmed_after_a_fast_handler(self):
        """A left-armed itimer would fire into whatever ran next — including
        code outside this dispatcher entirely."""
        import signal
        hookrun.run_event("E", payload="{}", specs=[_spec("ok", timeout=5)])
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))


class TimerCancellationTest(unittest.TestCase):
    """Cancellation must cross real broad catchers without owner operations."""

    @staticmethod
    def _state():
        return {
            "env": dict(os.environ), "stdin": sys.stdin, "stdout": sys.stdout,
            "stderr": sys.stderr, "argv": list(sys.argv), "cwd": os.getcwd(),
            "alarm": signal.getsignal(signal.SIGALRM),
            "timer": signal.getitimer(signal.ITIMER_REAL),
            "fds": [(os.fstat(fd), fcntl.fcntl(fd, fcntl.F_GETFD))
                    for fd in range(3)],
        }

    def _probe(self, kind, timed_out, sibling_rc=0):
        from helm import actors, cli, record, seats_cli, seats_rename, toolwhisper

        # fstat includes mutable file size. Drain output a preceding test left
        # in the runner's buffer before declaring it part of product state; the
        # pre-dup2 flush below would otherwise mutate this probe's own baseline.
        sys.stdout.flush()
        before = self._state()
        # A preexisting active timer is a different contract, not exercised here.
        self.assertEqual(before["timer"], (0.0, 0.0))
        hits = {name: 0 for name in ("entry", "lowlevel", "interrupted",
                                    "sleep_completed", "returned", "sibling")}
        seen, outcomes, returns, unsafe = {}, [], [], []
        err, out = io.StringIO(), io.StringIO()
        budget, sleep = 0.04, 0.4
        run_one = hookrun.run_one

        def observe(*args, **kwargs):
            rc = run_one(*args, **kwargs)
            outcomes.append(dict(kwargs["outcome"]))
            returns.append(rc)
            return rc

        def forbidden(*args, **kwargs):
            unsafe.append("owner operation")
            raise AssertionError("a synthetic dependency reached an owner operation")

        def marked(name, value):
            def call(*args, **kwargs):
                hits[name] = hits.get(name, 0) + 1
                return value
            return call

        def lowlevel():
            hits["lowlevel"] += 1
            seen["warning"] = os.environ.get("HELM_NO_TREE_WARNING")
            os.chdir(cwd)
            sys.argv = ["synthetic-mutated-argv"]
            if not timed_out:
                return
            start = time.monotonic()
            try:
                time.sleep(sleep)
                hits["sleep_completed"] += 1
            except hookrun._Timeout:
                hits["interrupted"] += 1
                raise                         # the REAL catcher must let it cross
            finally:
                seen["elapsed_s"] = time.monotonic() - start

        def fake_record(event):
            seen["record_payload"] = event
            lowlevel()

        def fake_delivery(**kwargs):
            seen["delivery"] = {k: v for k, v in kwargs.items() if k != "emit"}
            lowlevel()

        def dispatch(args):
            if args[0] == "sibling":
                hits["sibling"] += 1
                seen["sibling_payload"] = sys.stdin.buffer.read()
                seen["sibling_cwd"] = os.getcwd()
                seen["sibling_argv"] = list(sys.argv)
                seen["sibling_write"] = os.write(1, b"synthetic sibling\n")
                return sibling_rc
            hits["entry"] += 1
            if kind == "record":
                rc = record.record(json.loads(sys.stdin.read()))
            else:
                rc = seats_cli.cmd("deliver", ["--hook-json"])
            hits["returned"] += 1
            return rc

        try:
            with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryFile() as fdout:
                payload = {"tool_name": "Read", "session_id": "synthetic-only",
                           "cwd": cwd, "probe": "nonempty-λ"}
                text = json.dumps(payload, ensure_ascii=False)
                saved = os.dup(1)
                try:
                    sys.stdout.flush()
                    os.dup2(fdout.fileno(), 1)
                    with contextlib.ExitStack() as stack:
                        for module, name, replacement in (
                            (cli, "main", dispatch),
                            (hookrun, "run_one", observe),
                            (record, "_record", fake_record),
                            (seats_rename, "recover_seat_rename", marked("rename", True)),
                            (seats_cli, "_payload_homing", marked("homing", ("main", None))),
                            (seats_cli, "_record_posttool_delegation", marked("delegation", None)),
                            (seats_cli, "_hook_emit", marked("emit_factory", forbidden)),
                            (actors, "grant_on_behalf", marked("grant", (None, None))),
                            (seats_cli, "_assert_own_seat", marked("identity", ("fake-seat", None))),
                            (seats_cli, "deliver_any", fake_delivery),
                            (toolwhisper, "for_hook", marked("whisper", None)),
                        ):
                            stack.enter_context(patch.object(module, name, replacement))
                        stack.enter_context(patch.dict(os.environ, {"HELM_NO_TREE_WARNING": "synthetic-prior"}))
                        stack.enter_context(contextlib.redirect_stdout(out))
                        stack.enter_context(contextlib.redirect_stderr(err))
                        rc = hookrun.run_event("PostToolUse", payload=text, specs=[
                            _spec(kind, event="PostToolUse", timeout=budget),
                            _spec("sibling", event="PostToolUse", timeout=budget,
                                  gate=sibling_rc == 2),
                        ])
                        self.assertEqual(os.environ["HELM_NO_TREE_WARNING"], "synthetic-prior")
                finally:
                    os.dup2(saved, 1)
                    fcntl.fcntl(1, fcntl.F_SETFD, before["fds"][1][1])
                    os.close(saved)
                fdout.seek(0)
                wire = fdout.read()
        finally:
            # Assert product restoration BEFORE emergency cleanup, including on
            # a failure path. Cleanup cannot make a leaked state look restored.
            try:
                self.assertEqual(self._state(), before)
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, before["alarm"])
                sys.stdin, sys.stdout, sys.stderr = (before[k] for k in ("stdin", "stdout", "stderr"))
                sys.argv = before["argv"]
                os.chdir(before["cwd"])
                os.environ.clear()
                os.environ.update(before["env"])

        # Positive must-hit and payload checks precede outcome/content checks.
        self.assertEqual(hits["entry"], 1)
        self.assertEqual(hits["lowlevel"], 1)
        self.assertEqual(hits["interrupted"], int(timed_out))
        self.assertEqual(hits["sleep_completed"], 0)
        self.assertEqual(hits["sibling"], 1)
        self.assertEqual(unsafe, [])
        self.assertEqual(seen["sibling_payload"], text.encode())
        self.assertEqual(seen["sibling_cwd"], before["cwd"])
        self.assertEqual(seen["sibling_argv"], ["helm", "sibling"])
        self.assertEqual(seen["warning"], "1")
        self.assertEqual(seen["sibling_write"], len(b"synthetic sibling\n"))
        if kind == "record":
            self.assertEqual(seen["record_payload"], payload)
        else:
            # EXACT EQUALITY IS KEPT HERE ON PURPOSE, unlike the outcome
            # dict below: this arm IS about the delivery call, so an
            # unintended extra kwarg reaching it is a finding rather than
            # noise. `sink_usable` is the intended one, and its VALUE is the
            # contract that matters -- the hook boundary now measures its
            # destination, and its emitter is a closure this reader cannot
            # place, so the answer is UNKNOWN and the row is consumed exactly
            # as it always was. A False here would mean the hook path had
            # started withholding, which is a behaviour change nobody asked
            # for.
            self.assertEqual(seen["delivery"], {
                "session": "synthetic-only", "room": "main", "seat": "fake-seat",
                "cwd": cwd, "channel": "hook", "sink_usable": None,
            })
            for name in ("rename", "homing", "delegation", "emit_factory", "grant", "identity"):
                self.assertEqual(hits[name], 1)
            self.assertEqual(hits.get("whisper", 0), int(not timed_out))
        if timed_out:
            self.assertLess(seen["elapsed_s"], sleep * .8)
        else:
            self.assertEqual(hits["returned"], 1)
        self.assertEqual(wire, b"synthetic sibling\n")
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(returns, [0, sibling_rc])
        self.assertEqual(rc, sibling_rc)
        self.observed = {"hits": hits, "outcomes": outcomes, "returns": returns,
                         "rc": rc, "stderr": err.getvalue(), "stdout": out.getvalue(),
                         "fd1": wire.decode(), "elapsed_s": seen.get("elapsed_s"),
                         "restored_before_cleanup": True}
        # Separate subtests make the baseline's missed status AND missed noise
        # explicit, rather than stopping after the first mismatch.
        with self.subTest(contract="outcome"):
            # STATUS IS THE CONTRACT; the dict is allowed to carry more.
            # Exact-equality here froze the outcome's SHAPE, so adding the
            # `why` field — the whole point of making an UNCHECKED say which
            # of its three causes happened — reddened an arm about statuses.
            self.assertEqual([o["status"] for o in outcomes],
                             ["unchecked" if timed_out else "answered",
                              "answered"])
        with self.subTest(contract="unchecked carries its reason"):
            # AND THE NEW GUARANTEE, asserted rather than merely allowed: an
            # UNCHECKED that gives no reason is what made every caller print a
            # fallback naming two causes at once. An ANSWERED owes no reason.
            if timed_out:
                self.assertEqual(outcomes[0].get("why"),
                                 "handler timed out after 0.04s",
                                 "an UNCHECKED outcome did not say WHICH of "
                                 "timeout, crash or bad exit code it was")
            else:
                self.assertIsNone(outcomes[0].get("why"))
        with self.subTest(contract="timeout diagnostic"):
            # The sentence is SHORT now — hook, budget, consequence — because
            # this one repeats per tool call and the owner could no longer read
            # past it. UNCHECKED survives the trim, and `why` above still
            # carries the full machine-readable cause, so nothing a consumer
            # reads got shorter.
            self.assertEqual(err.getvalue(),
                             "[helm %s] TIMED OUT at 0.04s — this event is UNCHECKED\n" % kind
                             if timed_out else "")

    def test_record_timeout_crosses_actual_broad_catcher(self):
        self._probe("record", timed_out=True)

    def test_deliver_timeout_crosses_actual_broad_catcher(self):
        self._probe("deliver", timed_out=True)

    def test_record_healthy_actual_catcher_is_silent(self):
        self._probe("record", timed_out=False)

    def test_deliver_healthy_actual_catcher_is_silent(self):
        self._probe("deliver", timed_out=False)

    def test_record_timeout_does_not_disarm_blocking_sibling(self):
        self._probe("record", timed_out=True, sibling_rc=2)


class RegistryTest(unittest.TestCase):
    """The registry must describe what actually fires, in the order it fires."""

    def test_external_specs_are_never_dispatched_in_process(self):
        """fab-suite-pretooluse is a binary helm does not ship. Claiming to run
        it in-process would be a silent no-op where a guard used to be."""
        names = [s["name"] for s in hookrun.event_specs("PreToolUse", tool_name="Bash")]
        self.assertIn("argv-guard", names)
        self.assertNotIn("suite-guard", names)

    def test_record_is_visible_to_the_dispatcher(self):
        """record self-installs and was absent from hooks.SPECS, which is why
        the fleet's highest-traffic hook could not be merged with its sibling."""
        names = [s["name"] for s in hookrun.event_specs("PostToolUse")]
        self.assertIn("record", names)
        self.assertIn("deliver", names)

    def test_posttooluse_order_is_record_then_deliver(self):
        """Pinned against the live installed order, which is an install-sequence
        artifact rather than declaration order. Naive composition reverses it."""
        names = [s["name"] for s in hookrun.event_specs("PostToolUse")]
        self.assertEqual(names, ["record", "deliver"])

    def test_retired_credentials_are_absent_without_reordering_other_guards(self):
        names = [s["name"] for s in hookrun.event_specs("SessionStart")]
        self.assertEqual(names, ["join", "resume-turn"])
        self.assertEqual([s["name"] for s in hookrun.event_specs("Stop")], ["stop-guard"])
        self.assertFalse(any(s["name"].startswith("cred-")
                             for s in hookrun.dispatch_specs()))

    def test_named_matcher_is_not_guessed_for_an_unknown_tool(self):
        """A Bash-only guard must not run against an unknown tool — that would
        be new behaviour, not preserved behaviour."""
        names = [s["name"] for s in hookrun.event_specs("PreToolUse", tool_name=None)]
        # control: the SAME query WITH the tool does resolve it, so the empty
        # answer above is a matcher decision and not a broken lookup.
        self.assertIn("argv-guard",
                      [s["name"] for s in hookrun.event_specs("PreToolUse", tool_name="Bash")])
        self.assertNotIn("argv-guard", names)

    def test_an_alternation_matcher_runs_the_guard_for_each_tool_it_names(self):
        """task/2542: the argv-guard also refuses a subagent's Monitor call
        that arms the seat beacon, so its matcher names two tools. Claude Code
        reads a matcher as a pattern ("Edit|Write"); the merged dispatcher
        compared it to the payload's tool by string equality, so a two-tool
        matcher would have silently disarmed the guard for BOTH tools."""
        names = lambda tool: [s["name"] for s in
                              hookrun.event_specs("PreToolUse", tool_name=tool)]
        self.assertIn("argv-guard", names("Bash"))
        self.assertIn("argv-guard", names("Monitor"))
        # task/2566: the GitHub-Actions rung reads a Write/Edit file_path
        self.assertIn("argv-guard", names("Write"))
        self.assertIn("argv-guard", names("Edit"))
        # the agent-model rung reads an Agent call's model key; the Workflow
        # tool spawns its agents without an Agent call and is not matched
        self.assertIn("argv-guard", names("Agent"))
        self.assertNotIn("argv-guard", names("Workflow"))
        self.assertNotIn("argv-guard", names("Read"))
        self.assertNotIn("argv-guard", names("BashOutput"))


class AnUncheckedOutcomeSaysWhichCauseTest(unittest.TestCase):
    """THREE WAYS TO NOT ANSWER, AND A CALLER THAT COULD NOT TELL THEM APART.

    Every consumer renders an UNCHECKED from `outcome["why"]`; with that empty
    they printed a fallback naming a timeout AND a crash at once, because the
    dispatcher genuinely did not know which had happened. The fact was already
    being recorded internally under three different tokens, so the gap was in
    the REPORT and never in the measurement. The timeout path is covered by
    TimerCancellationTest; these are the other two.
    """

    def setUp(self):
        from helm import cli
        self._real_main = cli.main
        self.stub = _Stub()
        cli.main = self.stub
        self.addCleanup(setattr, cli, "main", self._real_main)

    def _run(self, handler):
        outcome, err = {}, io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = hookrun.run_one(_spec(handler), "{}", outcome=outcome)
        return rc, outcome, err.getvalue()

    def test_the_STAMP_SURVIVES_a_code_whose_repr_RAISES(self):
        """THE DIAGNOSTIC MAY NOT TAKE DOWN THE EVENT IT DESCRIBES.

        `run_event` dispatches handlers in a bare loop with no handler of its
        own, so anything escaping `run_one` means EVERY LATER HANDLER IN THAT
        EVENT NEVER RUNS. The line added to make this path speak calls `%r` on
        an object the handler chose, and a stderr write can fail on a closed
        stream — so the outcome is stamped BEFORE either of them runs.
        """
        rc, outcome, err = self._run("hostilerepr")
        self.assertEqual(rc, 0, "a hostile repr must still fail OPEN")
        self.assertEqual(outcome["status"], "unchecked",
                         "the stamp was lost, so the caller reads this as a "
                         "handler that ANSWERED")
        self.assertIn("uninterpretable", outcome.get("why") or "",
                      "the outcome carries no reason: %r" % (outcome,))
        # THE REASON IS THE CONSTANT AND NOTHING MORE, deliberately. Naming
        # WHY the code could not be rendered would cost another call on the
        # handler's own object, which is the hazard this path exists to keep
        # out. The detail is carried only when the repr WORKS — the sibling
        # arm below drives that path and asserts the code appears.
        self.assertNotIn("<", outcome.get("why") or "",
                         "the reason fabricated a rendering of an object that "
                         "refused to be rendered: %r" % (outcome,))

    def test_the_FALLBACK_itself_runs_no_code_the_handler_chose(self):
        """THE HAZARD MOVED ONE FRAME IN AND HAD TO LEAVE.

        Guarding `repr()` and degrading to `type(x).__name__` reaches a
        METACLASS attribute, which can raise in its own right — so the
        diagnostic still escaped `run_one` and still aborted the dispatch
        loop, with a stamped outcome that nobody got to read. The stamp is now
        a CONSTANT set before anything foreign runs, and every call that
        touches the handler's object lives inside one guard.
        """
        first, second = {}, {}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc1 = hookrun.run_one(_spec("unnameable"), "{}", outcome=first)
            rc2 = hookrun.run_one(_spec("ok"), "{}", outcome=second)
        self.assertEqual(rc1, 0, "an unnameable code must still fail OPEN")
        self.assertEqual(first["status"], "unchecked")
        self.assertIn("uninterpretable", first.get("why") or "",
                      "the constant stamp did not survive: %r" % (first,))
        # THE PROPERTY THE LOOP NEEDS: the handler AFTER it still answered.
        self.assertEqual(second.get("status"), "answered",
                         "the diagnostic aborted the dispatch loop: %r"
                         % (second,))
        self.assertEqual(rc2, 0)

    def test_a_LATER_handler_still_runs_after_a_hostile_repr(self):
        """THE OUTCOME, not the stamp: the blast radius is the EVENT.

        A stamp that survives in isolation proves nothing about the loop, so
        this drives two handlers through `run_one` in sequence the way
        `run_event` does and requires the second to answer.
        """
        first, second = {}, {}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc1 = hookrun.run_one(_spec("hostilerepr"), "{}", outcome=first)
            rc2 = hookrun.run_one(_spec("ok"), "{}", outcome=second)
        self.assertEqual(rc1, 0)
        self.assertEqual(first["status"], "unchecked")
        # THE POSITIVE: the handler AFTER the hostile one answered, which is
        # the property `run_event`'s unguarded loop actually needs.
        self.assertEqual(rc2, 0)
        self.assertEqual(second.get("status"), "answered",
                         "the handler after a hostile repr did not answer: "
                         "%r" % (second,))

    def test_a_RAISING_handler_names_its_exception(self):
        rc, outcome, err = self._run("raiser")
        self.assertEqual(rc, 0, "a crash must still fail OPEN")
        self.assertEqual(outcome["status"], "unchecked")
        self.assertEqual(outcome.get("why"), "handler raised RuntimeError",
                         "an UNCHECKED from a crash did not say so, so its "
                         "caller cannot tell it from a timeout")
        self.assertIn("RuntimeError", err, "the traceback channel is unchanged")

    def test_an_UNINTERPRETABLE_exit_code_is_SAID_OUT_LOUD(self):
        """The silent one. This path set the status and wrote NOTHING, so the
        caller's fallback said "see stderr" about an empty stderr."""
        rc, outcome, err = self._run("badexit")
        self.assertEqual(rc, 0, "an uninterpretable code must fail OPEN")
        self.assertEqual(outcome["status"], "unchecked")
        # THE POSITIVE FOR THE ARM ABOVE: when the repr WORKS the reason
        # carries the rendered code, so dropping it on a hostile object is a
        # measured trade and not a reason that never says anything.
        self.assertIn("object object at", outcome.get("why") or "",
                      "the readable case lost its detail too: %r" % (outcome,))
        self.assertIn("uninterpretable", outcome.get("why") or "",
                      "the one path that produced no diagnostic at all still "
                      "produces none in the outcome")
        self.assertIn("UNCHECKED", err,
                      "this path wrote nothing to stderr, so an operator "
                      "following 'see stderr' found an empty channel")

    def test_an_ANSWERING_handler_carries_NO_why(self):  # noqa: VACUOUS_ASSERTION — the contract IS the absence of a reason on a clean answer, and its unconditional positive control is the two sibling arms above driving the same door to a populated `why`
        """The control: `why` must mark the UNCHECKED cases and only those, or
        a caller keying on its presence learns nothing."""
        rc, outcome, _err = self._run("ok")
        self.assertEqual(rc, 0)
        self.assertEqual(outcome["status"], "answered")
        self.assertIsNone(outcome.get("why"))


class ScopeTest(unittest.TestCase):
    """A scope:"helm" handler must still SKIP outside helm when it is reached
    through the dispatcher instead of through its own hook entry.

    It holds by construction — cli.main calls hook_skips_here per handler, and
    the dispatcher passes each spec's real argv including --hook-json, which is
    that function's discriminator. But "holds by construction" is exactly what
    the io.StringIO replay was, so it gets an arm.
    """

    def test_the_argv_the_dispatcher_passes_still_reaches_the_scope_gate(self):
        """hook_skips_here keys on --hook-json being in `rest` and on the joined
        argv matching a fleet-scoped prefix. Split the spec args the way run_one
        does and both must still be true, or every scoped handler would silently
        become fleet-wide the moment it ran under the dispatcher."""
        import shlex
        from helm import hooks
        scoped = hooks.fleet_scoped_args()
        self.assertTrue(scoped, "no fleet-scoped args to check against")
        for spec in hookrun.event_specs("PostToolUse"):
            argv = shlex.split(spec["args"])
            verb, rest = argv[0], argv[1:]
            joined = " ".join([verb] + rest)
            if spec.get("scope") == "helm":
                self.assertIn("--hook-json", rest,
                              "%s loses the scope discriminator" % spec["name"])
                self.assertTrue(any(joined.startswith(sa) for sa in scoped),
                                "%s no longer matches a fleet-scoped prefix" % spec["name"])

    def test_scoped_and_unscoped_handlers_are_classified_differently(self):   # noqa: VACUOUS_ASSERTION — assertTrue(deliver_scoped) IS the unconditional control on the same predicate the assertFalse reads; a run where both answered alike would fail here
        """The control that makes the arm above mean something: deliver IS
        scoped and record is NOT, so a test that found them both scoped (or
        both not) would be reading a broken predicate rather than the truth."""
        from helm import hooks
        scoped = hooks.fleet_scoped_args()
        names = {s["name"]: s for s in hookrun.event_specs("PostToolUse")}
        self.assertIn("deliver", names)
        self.assertIn("record", names)
        deliver_scoped = any(names["deliver"]["args"].startswith(sa) for sa in scoped)
        record_scoped = any(names["record"]["args"].startswith(sa) for sa in scoped)
        self.assertTrue(deliver_scoped, "deliver should be fleet-scoped")
        self.assertFalse(record_scoped, "record is deliberately NOT fleet-scoped")


class RegistryImportFailureTest(unittest.TestCase):
    """A registry family that fails to import must never vanish in silence.

    A FIX on c73740e83d4c. dispatch_specs caught Exception around the
    cred and record imports and passed, so a broken record module made
    PostToolUse quietly run deliver ALONE with an empty stderr — the recorder
    erased, nothing anywhere saying so. It only became silent BECAUSE of this
    lane: before the merge record ran as its own process, so the same breakage
    failed loudly on every tool call while deliver carried on. The merge is
    what turned a visible failure into an invisible one, which makes it this
    lane's regression to own.

    The old comment said "a missing optional module must never wedge a hook".
    Neither module is optional — both ship in this package — so any failure
    here is a real failure, never a legitimate absence.
    """

    def _with_broken(self, target):
        """event_specs + stderr + _AUTH_MISSING with `target`'s import raising."""
        import builtins
        real = builtins.__import__

        def poisoned(name, *a, **k):
            if name == target:
                raise ImportError("forced: simulating a broken module")
            return real(name, *a, **k)

        err = io.StringIO()
        real_err = sys.stderr
        builtins.__import__ = poisoned
        sys.stderr = err
        try:
            names = [s["name"] for s in hookrun.event_specs("PostToolUse")]
            missing = list(hookrun._AUTH_MISSING)
        finally:
            builtins.__import__ = real
            sys.stderr = real_err
        return names, err.getvalue(), missing

    def test_healthy_registry_is_complete_and_silent(self):
        """THE CONTROL, and it runs first: with nothing broken the registry is
        whole and says nothing. Without it, a loud-on-failure arm could pass
        against a registry that is loud always."""
        err = io.StringIO()
        real_err, sys.stderr = sys.stderr, err
        try:
            names = [s["name"] for s in hookrun.event_specs("PostToolUse")]
        finally:
            sys.stderr = real_err
        self.assertEqual(names, ["record", "deliver"])
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(hookrun._AUTH_MISSING, [])

    def test_broken_record_is_announced_not_swallowed(self):
        """The exact red probe: force the record import to raise and
        assert the loss is ANNOUNCED. Measured before the fix: ['deliver'] with
        stderr == ''."""
        names, err, missing = self._with_broken("helm.record")
        self.assertTrue(err.strip(), "the family vanished with an EMPTY stderr")
        self.assertIn("record", err)
        self.assertIn("NOT RUNNING", err)
        self.assertEqual([(m, a) for m, a, _ in missing], [("record", "HOOK_SPECS")])

    def test_broken_cred_is_announced_too(self):
        """The sibling family asks the same question, so it gets the same arm —
        a cure that lands on one of two identical call sites is half a cure."""
        names, err, missing = self._with_broken("helm.cred._common")
        self.assertTrue(err.strip())
        self.assertIn("cred", err)
        self.assertEqual([(m, a) for m, a, _ in missing], [("cred._common", "GUARD_SPECS")])

    def test_surviving_handlers_still_run_when_a_family_is_broken(self):
        """Fail-open is PRESERVED: a registry that wedges every tool call is
        worse than one missing a family. The loss is announced, not fatal."""
        names, _err, _m = self._with_broken("helm.record")
        self.assertIn("deliver", names, "a broken sibling took deliver down with it")

    def test_the_failure_list_resets_between_calls(self):
        """A stale entry would report a family missing long after it recovered —
        the same stale-artifact-reads-as-current class this lane is about."""
        self._with_broken("helm.record")
        self.assertTrue(hookrun._AUTH_MISSING)
        hookrun.event_specs("PostToolUse")          # healthy call
        self.assertEqual(hookrun._AUTH_MISSING, [])


class IntegrationTest(unittest.TestCase):
    """One arm on the REAL path — the stub above proves composition, this proves
    the wiring actually reaches a real verb through cli.main."""

    def test_real_dispatch_runs_and_exits_zero(self):   # noqa: VACUOUS_ASSERTION — assertTrue(hits) is the positive control on a real observable (record's counters.json); rc 0 alone is the absence half
        import json
        import subprocess
        import tempfile
        root = tempfile.mkdtemp(prefix="hookrun-it-")
        env = dict(os.environ)
        env["HELM_HOME"] = os.path.join(root, "helm")
        env["HELM_CHAT_DIR"] = os.path.join(root, "chat")
        env["HELM_CHAT_NAME"] = "hookrun-test"
        os.makedirs(env["HELM_HOME"], exist_ok=True)
        os.makedirs(env["HELM_CHAT_DIR"], exist_ok=True)
        helm = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "bin", "helm")
        payload = json.dumps({"session_id": "t" * 8, "hook_event_name": "PostToolUse",
                              "tool_name": "Bash", "tool_input": {"command": "true"},
                              "tool_response": {"stdout": "", "stderr": ""},
                              "cwd": root}).encode()
        p = subprocess.run([helm, "hooks", "run", "PostToolUse", "--hook-json"],
                           input=payload, capture_output=True, env=env)
        self.assertEqual(p.returncode, 0,
                         "stderr=%s" % p.stderr.decode()[:800])
        # CONTROL on a REAL observable: record writes counters under HELM_HOME.
        # Its presence proves the dispatch reached a real handler through the
        # real cli.main, rather than exiting 0 having run nothing.
        hits = []
        for dirpath, _dirs, files in os.walk(env["HELM_HOME"]):
            for f in files:
                if f == "counters.json":
                    hits.append(os.path.join(dirpath, f))
        self.assertTrue(hits, "no counters.json — the dispatch ran no handler")

    def test_real_dispatch_from_a_foreign_cwd_still_runs_the_unscoped_handler(self):   # noqa: VACUOUS_ASSERTION — assertTrue(hits) is the positive control — counters.json written from a FOREIGN cwd is the whole claim, not the rc
        """From a NON-helm cwd the scoped handlers skip, but record must still
        run — it is deliberately not fleet-scoped, and a dispatcher that scoped
        the whole EVENT rather than each handler would silence it everywhere."""
        import json
        import subprocess
        import tempfile
        root = tempfile.mkdtemp(prefix="hookrun-foreign-")
        env = dict(os.environ)
        env["HELM_HOME"] = os.path.join(root, "helm")
        env["HELM_CHAT_DIR"] = os.path.join(root, "chat")
        env["HELM_CHAT_NAME"] = "hookrun-test"
        os.makedirs(env["HELM_HOME"], exist_ok=True)
        os.makedirs(env["HELM_CHAT_DIR"], exist_ok=True)
        helm = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "bin", "helm")
        payload = json.dumps({"session_id": "f" * 8, "hook_event_name": "PostToolUse",
                              "tool_name": "Bash", "tool_input": {"command": "true"},
                              "tool_response": {"stdout": "", "stderr": ""},
                              "cwd": root}).encode()
        p = subprocess.run([helm, "hooks", "run", "PostToolUse", "--hook-json"],
                           input=payload, capture_output=True, env=env, cwd=root)
        self.assertEqual(p.returncode, 0, "stderr=%s" % p.stderr.decode()[:800])
        hits = []
        for dirpath, _dirs, files in os.walk(env["HELM_HOME"]):
            for f in files:
                if f == "counters.json":
                    hits.append(os.path.join(dirpath, f))
        self.assertTrue(hits, "record was silenced outside helm's own project")


if __name__ == "__main__":
    unittest.main()
