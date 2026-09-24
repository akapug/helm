"""Hermetic tests for `helm seat spawn` (the harness-agnostic self-onboarding
seat spawn) + `helm seat where` (the spawn register resolver). The harness
adapter is always mocked and subprocess.Popen is always patched — no real
pane, process, or metaharness is ever touched."""
import contextlib
import io
import json
import os
import re
import shlex
import shutil
import tempfile
import unittest
from unittest import mock

from tests._tmphome import pin_suite_guard
from helm import cli, harness, pk, seat, seat_exit_owner, seat_paths
from helm import seat_lifecycle_runtime as seat_runtime


def _orca_reply(result, ok=True):
    return json.dumps({"id": "x", "ok": ok, "result": result})


def _herdr_reply(result):
    return json.dumps({"id": "cli:x", "result": result})



def _token_in_command(command):
    """The attempt token a shell command carries, read the way the child would
    receive it: as the `env` ASSIGNMENT, not as a substring of the line."""
    for word in shlex.split(command):
        if word.startswith(seat.SPAWN_ATTEMPT_ENV + "="):
            return word.split("=", 1)[1]
    return None


def _free_pid():
    """A pid NOTHING is using, verified, not a hopeful constant.

    This module hardcoded 4242. After the 2026-07-28 reboot the pid space
    refilled and 4242 became a live ROOT-owned process, so the reap path's
    kill() returned EPERM instead of ESRCH and the test failed with "stale
    headless codex pid 4242 NOT reaped ([Errno 1] Operation not permitted)".

    The PRODUCTION code was right — refusing to signal a process you do not own
    is correct. The test's premise (this pid is free) was simply never checked,
    which made the suite depend on which pids the kernel had handed out. Scan
    downward from the max and confirm /proc has no such entry."""
    import os as _os
    try:
        with open("/proc/sys/kernel/pid_max") as f:
            top = int(f.read().strip())
    except Exception:
        top = 4194304
    for cand in range(top - 1, top - 5000, -1):
        if not _os.path.exists("/proc/%d" % cand):
            return cand
    raise RuntimeError("no free pid found in the top 5000 — cannot fake a dead process")


# Computed ONCE so every assertion in this module talks about the same pid.
_FAKE_PID = _free_pid()


class FakeProc:
    def __init__(self, stdout, rc=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, rc, stderr


# A pane whose composer is EMPTY — one that took its turn. Synthetic, but a
# real frame's shape: `submit` proves delivery by READING THE COMPOSER BACK,
# so a double returning "" models an UNREADABLE pane (UNKNOWN), not a
# working one.
ADVANCED_PANE = "\n".join(("─" * 40, "❯", "─" * 40,
                           "  opus-5 | ~/dev/example/repo",
                           "  ⏵⏵ bypass permissions on"))


class FakeAdapter(harness._CLIAdapter):
    """Records the uniform seam ops spawn drives: list/stop (reap), spawn
    (pane create), send (onboarding injection)."""
    name, path = "fake", "/bin/fake"

    def __init__(self, rows=(), stop_removes=True):
        self.rows = list(rows)
        self.stop_removes = stop_removes
        self.spawned, self.stopped, self.sent, self.order = [], [], [], []
        self.typed = {}

    def spawn(self, command, title=None, cwd=None):
        self.spawned.append((command, title, cwd))
        self.order.append("spawn")
        return "pane-1"

    def list(self):
        return list(self.rows)

    def read(self, handle, limit=3000, timeout=60):
        text = self.typed.get(handle)
        if text is not None:
            return ADVANCED_PANE.replace("\n❯\n", "\n❯\xa0%s\n" % text)
        return ADVANCED_PANE

    def send(self, handle, text, enter=True):
        if enter:
            self.typed.pop(handle, None)
        else:
            self.typed[handle] = text
        self.sent.append((handle, text, enter))
        self.order.append("send")

    def stop(self, handle):
        self.typed.pop(handle, None)
        self.stopped.append(handle)
        self.order.append("stop")
        if self.stop_removes:
            self.rows = [row for row in self.rows
                         if row.get("handle") != handle]


class FakeOrcaAdapter(FakeAdapter):
    name, path = "orca", "/bin/orca"

    def __init__(self, rows=(), resolved=None, stop_removes=True):
        super().__init__(rows, stop_removes=stop_removes)
        self.resolved = resolved or {}
        self.pane_keys = []

    def resolve_pane(self, pane_key):
        self.pane_keys.append(pane_key)
        return dict(self.resolved)


class KeyedOrcaAdapter(FakeOrcaAdapter):
    """One answer PER PANE KEY: a dict is the reply, an exception is raised."""

    def __init__(self, table, rows=()):
        super().__init__(rows)
        self.table = table

    def resolve_pane(self, pane_key):
        self.pane_keys.append(pane_key)
        got = self.table.get(pane_key)
        if isinstance(got, Exception):
            raise got
        return dict(got or {})


def _orca_not_found():
    """Orca's POSITIVE no-such-pane answer, as `OrcaAdapter._runtime_call`
    raises it: an `ok: false` reply becomes a HarnessError carrying the host's
    own message, and `terminal_not_found` is that message."""
    return harness.HarnessError(
        "orca runtime rpc terminal.resolvePane: terminal_not_found")


ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_SPAWN_SEND_DELAY", "HELM_CHAT_NAME",
            "HELM_SUBMIT_SETTLE_S", "HELM_SEAT_ROLE",
            # The endpoint allocator reserves a port something on THIS HOST is
            # already listening on, read from /proc's LISTEN tables. HELM_PROC is
            # that reader's own documented root (`seat_ports._proc_root`), so an
            # arm about allocation pins it at a fixture and stops depending on
            # what the build host happens to have bound.
            "HELM_PROC", "CLAUDE_CONFIG_DIR")


class SpawnBase(unittest.TestCase):
    # A spawn with no --cwd now PROVISIONS the seat's home worktree (the
    # dirty-main cure). Every test that is not about that seam stubs it to a
    # tmp dir, so the suite never touches the real repo's worktrees.
    PATCH_SEAT_HOME = True

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-spawn-")
        self._env = {k: os.environ.get(k) for k in ENV_KEYS}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm-home")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_SPAWN_SEND_DELAY"] = "0"
        os.environ["HELM_SUBMIT_SETTLE_S"] = "0"
        # a seat mint refuses a contract it cannot write in full
        pin_suite_guard(self, self.tmp)
        for k in ("MELD_HOME", "MELD_CHAT_DIR", "HELM_CHAT_NAME",
                  # a runner that exports either of these would make an arm
                  # about the native home or the LISTEN census read the HOST's
                  # state instead of its fixture's
                  "HELM_PROC", "CLAUDE_CONFIG_DIR"):
            os.environ.pop(k, None)
        self.timer = mock.patch.object(seat, "_ensure_autocompact_timer")
        self.ensure_timer = self.timer.start()
        # WAIT GAPS, NEVER WAIT COUNTS. A fake pane never reports a terminal
        # state, so every close runs `stop_pane`'s whole bound, and `submit`
        # re-reads its composer across a verify window. The doubles count
        # reads and never read the clock, so both bounds still run every read
        # (`_PANE_CLOSE_POLLS`, `SUBMIT_VERIFY_READS`) and an unproven close is
        # still unproven after the last one; only the sleeps between reads go.
        self.gaps = [mock.patch.object(seat_exit_owner, "_PANE_CLOSE_DELAY", 0),
                     mock.patch.object(harness, "SUBMIT_VERIFY_INTERVAL_S", 0)]
        for p in self.gaps:
            p.start()
        self.registry = mock.patch.dict(harness.ADAPTERS, {"fake": FakeAdapter})
        self.registry.start()
        self.home = os.path.join(self.tmp, "seat-home")
        os.makedirs(self.home, exist_ok=True)
        self.seat_home = None
        if self.PATCH_SEAT_HOME:
            self.seat_home = mock.patch.object(seat, "_seat_home_cwd",
                                               return_value=self.home)
            self.seat_home.start()

    def tearDown(self):
        if self.seat_home is not None:
            self.seat_home.stop()
        self.registry.stop()
        for p in self.gaps:
            p.stop()
        self.timer.stop()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mint(self, seat_name="codex", family="codex", room=None,
              multi=False):
        d = seat._instance_dir(family, seat_name)
        os.makedirs(d, exist_ok=True)
        launch = os.path.join(d, "launch.sh")
        homing = " HELM_CHAT_ROOM=%s" % room if room else ""
        pin = "" if multi else " CLAUDE_CODE_SUBAGENT_MODEL=gpt-5.6-sol"
        with open(launch, "w") as f:
            f.write("#!/bin/sh\nexec env FAKE=1%s%s claude \"$@\"\n"
                    % (homing, pin))
        os.chmod(launch, 0o700)
        return d, launch

    def _launch_line(self, d, launch_sh, role="worker"):
        """The pane command the SHIPPED producer (`seat_role._launch_command`)
        builds for the attempt THIS spawn PUBLISHED.

        DERIVED, NEVER TRANSCRIBED. The attempt token is minted per attempt
        (`_publish_spawn_attempt`), so no literal can pin the launch line; the
        only way an arm stays BYTE-EXACT is to read the id out of the register
        the producer wrote and re-run the producer on it. Every caller's
        `assertEqual(command, self._launch_line(...))` is therefore also the
        proof that the token on the child's line IS this spawn's published id.

        The two asserts here are what stop the derivation being self-fulfilling:
        a spawn that published no attempt, or a producer that dropped the token
        on the floor, would otherwise make both sides equal and empty."""
        from helm import seat_role
        rec = seat._spawn_record(d) or {}
        token = (rec.get("attempt") or {}).get("id")
        self.assertTrue(token, "the spawn published no attempt id")
        line = seat_role._launch_command(launch_sh, role, token=token)
        self.assertEqual(_token_in_command(line), token,
                         "the producer dropped the attempt token, so this "
                         "expectation would pin nothing")
        return line

    def _record(self, d, handle="p9", harness_name="fake",
                session="session-a", **extra):
        runtime = extra.pop("harness", harness_name)
        rec = {"v": 1, "seat": "codex", "harness": runtime,
               "worktree": os.getcwd(), "room": "main"}
        if runtime != "headless":
            rec["handle"] = handle
            if session is not None:
                rec["session"] = session
        rec.update(extra)
        with open(os.path.join(d, "spawn.json"), "w") as f:
            json.dump(rec, f)

    def _spawn(self, args, adapter, popen=None):
        out, err = io.StringIO(), io.StringIO()
        popen = popen or mock.Mock(return_value=mock.Mock(pid=_FAKE_PID))
        with mock.patch.object(seat, "_write_launch_assets") as wla, \
                mock.patch.object(harness, "detect", return_value=adapter), \
                mock.patch.object(seat.subprocess, "Popen", popen), \
                mock.patch.object(seat, "_pid_identity", return_value="test-start"), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(["spawn"] + list(args))
        return rc, out.getvalue(), err.getvalue(), wla, popen


class HeadlessSpawnTest(SpawnBase):
    """Path 1 — the standalone DEFAULT: no metaharness, detached process,
    onboarding delivered at launch time as the boot first-prompt."""

    def test_headless_spawns_detached_with_onboarding_first_prompt(self):
        d, launch = self._mint()
        rc, out, err, wla, popen = self._spawn(["codex"], None)
        self.assertEqual(rc, 0, err)
        argv = popen.call_args[0][0]
        self.assertEqual(argv[0], launch)          # the minted script, by PATH
        onboard = argv[1]                          # launch.sh "$@" -> claude's
        self.assertIn("helm chat wait --seat codex --follow", onboard)
        self.assertIn("@codex", onboard)           # first prompt at boot
        self.assertIn("END YOUR TURNS", onboard)   # turn discipline is birthright
        self.assertIn("process, post, END", onboard)
        self.assertNotIn("\n", onboard)            # single keystroke burst
        kw = popen.call_args[1]
        self.assertTrue(kw.get("start_new_session"))   # setsid = detached
        self.assertEqual(kw.get("cwd"), self.home)     # its OWN worktree
        wla.assert_called_once()                   # mint hygiene refreshed
        self.assertIn("HEADLESS", out)
        self.assertIn("pid %d" % _FAKE_PID, out)

    def test_headless_lead_gets_ultracode_and_process_role(self):
        d, launch = self._mint()
        rc, _, err, _, popen = self._spawn(
            ["codex", "--role", "lead"], None)
        self.assertEqual(rc, 0, err)
        argv = popen.call_args[0][0]
        self.assertEqual(argv[:3],
                         [launch, "--settings", '{"ultracode":true}'])
        self.assertIn("helm chat wait --seat codex --follow", argv[3])
        self.assertIn("explicit FLEET LEAD", argv[3])
        self.assertIn("helm task list", argv[3])
        self.assertIn("subagents", argv[3])
        self.assertIn("workflows", argv[3])
        self.assertIn("post progress and STOP instead of holding the parent turn open",
                      argv[3])
        self.assertIn("positive live/recent delegation evidence", argv[3])
        self.assertIn("UNKNOWN still blocks", argv[3])
        self.assertNotIn("keep the master active while delegates run", argv[3])
        self.assertIn("Do not park while eligible work queues", argv[3])
        self.assertEqual(popen.call_args[1]["env"]["HELM_SEAT_ROLE"], "lead")
        with open(os.path.join(d, "spawn.json")) as f:
            self.assertEqual(json.load(f)["role"], "lead")

    def test_headless_worker_strips_an_inherited_lead_marker(self):
        d, launch = self._mint()
        os.environ["HELM_SEAT_ROLE"] = "lead"
        rc, _, err, _, popen = self._spawn(["codex"], None)
        self.assertEqual(rc, 0, err)
        argv = popen.call_args[0][0]
        self.assertEqual(argv[0], launch)
        self.assertEqual(len(argv), 2)       # launch + onboarding, no settings
        self.assertNotIn("HELM_SEAT_ROLE", popen.call_args[1]["env"])
        with open(os.path.join(d, "spawn.json")) as f:
            self.assertEqual(json.load(f)["role"], "worker")

    def test_headless_registers_spawn_json_and_roster_mirror(self):
        d, launch = self._mint()
        rc, _, err, _, _ = self._spawn(["codex", "--room", "team-z"], None)
        self.assertEqual(rc, 0, err)
        with open(os.path.join(d, "spawn.json")) as f:
            rec = json.load(f)
        self.assertEqual(rec["harness"], "headless")
        self.assertEqual(rec["pid"], _FAKE_PID)
        self.assertEqual(rec["worktree"], self.home)   # the register carries it
        self.assertEqual(rec["room"], "team-z")
        from helm import seats
        row = seats.roster().get("codex")
        self.assertIsNotNone(row)                  # any agent resolves it
        self.assertEqual(row.get("home_room"), "team-z")
        self.ensure_timer.assert_called_once()

    def test_headless_reaps_stale_same_name_pid_first(self):
        """The exact live bug: a prior bare same-name seat must die before
        the replacement spawns."""
        d, launch = self._mint()
        with open(os.path.join(d, "spawn.json"), "w") as f:
            json.dump({"v": 1, "seat": "codex", "harness": "headless", "pid": 987654,
                       "pid_identity": "old-start"}, f)
        kills = []
        with mock.patch.object(seat, "_recorded_pid_alive",
                               side_effect=[True, False, False, False]), \
                mock.patch.object(seat.os, "kill",
                                  side_effect=lambda p, s: kills.append((p, s))):
            rc, out, err, _, popen = self._spawn(["codex", "--replace"], None)
        self.assertEqual(rc, 0, err)
        self.assertEqual(kills, [(987654, seat.signal.SIGTERM)])
        self.assertIn("reaped stale headless codex (pid 987654)", out)
        self.assertTrue(popen.called)              # then the fresh spawn

    def test_headless_pid_reuse_never_kills_unrelated_process(self):
        d, _ = self._mint()
        with open(os.path.join(d, "spawn.json"), "w") as f:
            json.dump({"v": 1, "seat": "codex", "harness": "headless", "pid": 987654,
                       "pid_identity": "original-start"}, f)
        kills = []
        with mock.patch.object(seat, "_pid_alive", return_value=True), \
                mock.patch.object(seat, "_pid_identity",
                                  return_value="reused-start"), \
                mock.patch.object(seat.os, "kill",
                                  side_effect=lambda p, s: kills.append((p, s))):
            rc, _, err, _, popen = self._spawn(["codex"], None)
        self.assertEqual(rc, 0, err)
        self.assertEqual(kills, [])
        self.assertTrue(popen.called)

    def test_unverifiable_live_headless_pid_aborts_replacement(self):
        d, _ = self._mint()
        with open(os.path.join(d, "spawn.json"), "w") as f:
            json.dump({"v": 1, "seat": "codex", "harness": "headless",
                       "pid": 987654}, f)
        with mock.patch.object(seat, "_recorded_pid_alive", return_value=None):
            rc, _, err, wla, popen = self._spawn(["codex"], None)
        self.assertEqual(rc, 1)
        self.assertIn("identity is unverifiable", err)
        self.assertIn("replacement aborted", err)
        wla.assert_not_called()
        popen.assert_not_called()

    def test_headless_never_leaks_the_seat_token(self):
        d, launch = self._mint()
        with open(os.path.join(seat.seat_dir("codex"), "token"), "w") as f:
            f.write("super-secret-token\n")
        rc, out, err, _, popen = self._spawn(["codex"], None)
        self.assertEqual(rc, 0, err)
        for arg in popen.call_args[0][0]:
            self.assertNotIn("super-secret-token", arg)
            self.assertNotIn("ANTHROPIC", arg)

    def test_headless_register_failure_stops_untracked_process(self):
        """The register a headless launch cannot COMPLETE, not the one it cannot
        begin: a spawn publishes its pending identity before the launch (so the
        child's SessionStart can resolve its own family), so a publication that
        fails launches nothing at all and has no process to stop. The untracked
        process this arm is about comes from the FINALIZE failing after the pid
        exists, which is what the side_effect below drives."""
        self._mint()
        kills = []
        with mock.patch.object(seat, "_register_spawn",
                              side_effect=[True, False]), \
                mock.patch.object(seat, "_recorded_pid_alive",
                                  return_value=False), \
                mock.patch.object(seat.os, "kill",
                                  side_effect=lambda p, s: kills.append((p, s))):
            rc, _, _, _, _ = self._spawn(["codex"], None)
        self.assertEqual(rc, 1)
        self.assertEqual([x for x in kills if x[1]],
                         [(_FAKE_PID, seat.signal.SIGTERM)])


    def test_headless_unknown_after_term_preserves_active_record(self):  # noqa: VACUOUS_ASSERTION — UNKNOWN rc/text and the exact TERM signal positively control retained-record/no-spawn assertions
        d, _ = self._mint()
        self._record(d, harness="headless", pid=987654,
                     pid_identity="old-start")
        kills = []
        with mock.patch.object(seat, "_recorded_pid_alive",
                               side_effect=[True, None]), \
                mock.patch.object(seat.os, "kill",
                                  side_effect=lambda p, s: kills.append((p, s))):
            rc, _, err, wla, popen = self._spawn(
                ["codex", "--replace"], None)
        self.assertEqual(rc, 1)
        self.assertIn("terminal state is UNKNOWN", err)
        self.assertEqual(kills, [(987654, seat.signal.SIGTERM)])
        self.assertTrue(os.path.exists(os.path.join(d, "spawn.json")))
        self.assertEqual(seat_exit_owner.archive_paths(d), [])
        wla.assert_not_called()
        popen.assert_not_called()


class AdapterSpawnTest(SpawnBase):
    """Paths 2+3 — a metaharness is present: the seam gets exactly two calls
    (spawn the pane on the launch.sh PATH, send the onboarding)."""

    def test_adapter_spawns_pane_then_sends_onboarding(self):  # noqa: VACUOUS_ASSERTION — rc0, spawned call and submitted wire positively control the no-headless assertion
        d, launch = self._mint()
        fake = FakeAdapter()
        rc, out, err, wla, popen = self._spawn(["codex"], fake)
        self.assertEqual(rc, 0, err)
        popen.assert_not_called()                  # never headless-doubles
        command, title, cwd = fake.spawned[0]
        self.assertEqual(command, self._launch_line(d, launch))
        # CONTROL on the derivation, not on the producer: the token-free form of
        # the SAME producer is a different line, so the equality above is a real
        # pin and not the tautology "the producer agrees with itself about
        # nothing". Blast radius: this arm only — it re-runs `_launch_command`
        # on the same launch.sh with `token=None` and compares strings; it
        # touches no register and no spawn.
        from helm import seat_role
        self.assertNotEqual(command,
                            seat_role._launch_command(launch, "worker"),
                            "the pane command carries no attempt token")
        self.assertEqual(title, "codex")
        self.assertEqual(cwd, self.home)
        # The onboarding brief is TYPED (no Enter), then submitted by a bare
        # Enter of its own — and rc 0 above means the composer read back clear.
        self.assertEqual(len(fake.sent), 2)
        handle, text, enter = fake.sent[0]
        self.assertEqual(handle, "pane-1")
        self.assertFalse(enter, "the text leg must NOT carry Enter")
        self.assertEqual(fake.sent[1], ("pane-1", "", True))
        self.assertEqual(text,
                         "Run `helm seat boot-brief` and follow it.")
        self.assertIn("onboarding submitted", out)
        self.assertIn("spawned codex via fake", out)
        with open(os.path.join(d, "spawn.json")) as f:
            rec = json.load(f)
        self.assertEqual(rec["harness"], "fake")
        self.assertEqual(rec["handle"], "pane-1")
        self.ensure_timer.assert_called_once()

    def test_adapter_lead_model_and_role_are_independent_and_persisted(self):
        d, launch = self._mint()
        fake = FakeAdapter()
        rc, _, err, wla, _ = self._spawn([
            "codex", "--model", "gpt-5.3-codex-spark",
            "--role", "lead"], fake)
        self.assertEqual(rc, 0, err)
        command, title, _ = fake.spawned[0]
        # Byte-exact, and both env assignments in the ONE `env` prefix: the role
        # marker this arm has always pinned and the attempt token, derived from
        # the register this spawn published.
        self.assertEqual(command, self._launch_line(d, launch, role="lead"))
        self.assertIn("env HELM_SEAT_ROLE=lead ", command)
        self.assertTrue(command.endswith(" --settings %s"
                                         % shlex.quote('{"ultracode":true}')))
        self.assertEqual(title, "codex")
        self.assertEqual(wla.call_args.kwargs["model"],
                         "gpt-5.3-codex-spark")
        with open(os.path.join(d, "spawn.json")) as f:
            rec = json.load(f)
        self.assertEqual(rec["role"], "lead")
        self.assertEqual(rec["model"], "gpt-5.3-codex-spark")

    def test_session_start_binds_new_session_to_spawn_register(self):
        d, _ = self._mint()
        fake = FakeOrcaAdapter()
        rc, _, err, _, _ = self._spawn(["codex"], fake)
        self.assertEqual(rc, 0, err)
        with open(os.path.join(d, "spawn.json")) as f:
            self.assertIsNone(json.load(f)["session"])
        from helm import seats
        fields = {"handle": "pane-1", "pane_key": "tab:leaf",
                  "pty_id": "pty-1", "worktree_id": "workspace:/w"}
        with mock.patch.object(seat, "_sessionstart_pane_fields",
                               return_value=(fields, None)):
            seats.join(session="session-live", seat="codex", cwd=os.getcwd())
        with open(os.path.join(d, "spawn.json")) as f:
            self.assertEqual(json.load(f)["session"], "session-live")

    def test_live_session_identity_reads_only_proven_process_orca_keys(self):
        from helm import sessions
        d, _ = self._mint()
        sessions_dir = os.path.join(d, "claude", "sessions")
        os.makedirs(sessions_dir, exist_ok=True)
        with open(os.path.join(sessions_dir, "%d.json" % _FAKE_PID), "w") as f:
            json.dump({"sessionId": "session-live", "pid": _FAKE_PID,
                       "procStart": "123"}, f)
        real_open = open

        def open_selected(path, *args, **kwargs):
            if path == "/proc/%d/environ" % _FAKE_PID:
                return io.BytesIO(
                    b"SECRET=never-returned\0ORCA_PANE_KEY=tab:leaf\0"
                    b"ORCA_WORKTREE_ID=workspace:/w\0")
            return real_open(path, *args, **kwargs)

        with mock.patch.object(sessions, "_pid_is_claude",
                               return_value=True) as alive, \
                mock.patch("builtins.open", side_effect=open_selected):
            identity, err = seat._live_session_orca_identity(
                d, "session-live")
        self.assertIsNone(err)
        self.assertEqual(identity, {"pid": _FAKE_PID, "proc_start": "123",
                                    "pane_key": "tab:leaf",
                                    "worktree_id": "workspace:/w"})
        self.assertNotIn("SECRET", identity)
        alive.assert_called_once_with(_FAKE_PID, "123")

    def _live_claude(self, d, environ):
        """Stand up the pid-keyed session record this proof anchors on, and
        answer that ONE process's environ. `environ` is the exact byte string
        the process publishes, so each arm states its own launch vocabulary."""
        from helm import sessions
        sessions_dir = os.path.join(d, "claude", "sessions")
        os.makedirs(sessions_dir, exist_ok=True)
        with open(os.path.join(sessions_dir, "%d.json" % _FAKE_PID), "w") as f:
            json.dump({"sessionId": "session-live", "pid": _FAKE_PID,
                       "procStart": "123"}, f)
        real_open = open

        def open_selected(path, *args, **kwargs):
            if path == "/proc/%d/environ" % _FAKE_PID:
                return io.BytesIO(environ)
            return real_open(path, *args, **kwargs)
        return (mock.patch.object(sessions, "_pid_is_claude",
                                  return_value=True),
                mock.patch("builtins.open", side_effect=open_selected))

    # THE GENERATION A PROCESS WAS LAUNCHED UNDER IS NOT IN THE REGISTER'S TWO
    # CURRENT NAMES. `launch_line` publishes HELM_CHAT_NAME (the canonical name
    # at launch) and HELM_SEAT_STORAGE (the immutable storage key) into every
    # seat it starts; a rename moves only the first, and the register keeps only
    # the latest of it. These three arms are one triple: the middle generation
    # is this seat's, a stranger's storage is not, and a launch line that
    # published no storage at all decides nothing.
    _MIDDLE = (b"ORCA_PANE_KEY=tab:leaf\0ORCA_WORKTREE_ID=workspace:/w\0"
               b"HELM_CHAT_NAME=codex-b\0HELM_SEAT_STORAGE=%s\0")

    def test_a_generation_between_the_registers_two_names_is_still_this_seat(self):
        d, _ = self._mint()
        self._record(d, harness_name="orca", session="session-live")
        alive, opened = self._live_claude(d, self._MIDDLE % b"codex")
        with alive, opened:
            identity, err = seat._live_session_orca_identity(
                d, "session-live", seat_name="codex-c")
        self.assertIsNone(err, err)
        self.assertEqual(identity["pane_key"], "tab:leaf")

    def test_another_registers_storage_key_does_not_buy_this_seat(self):
        """THE MUST-REFUSE HALF. Without it the arm above is satisfied by a
        proof that accepts any process at all."""
        d, _ = self._mint()
        self._record(d, harness_name="orca", session="session-live")
        alive, opened = self._live_claude(d, self._MIDDLE % b"kimi")
        with alive, opened:
            identity, err = seat._live_session_orca_identity(
                d, "session-live", seat_name="codex-c")
        self.assertIsNone(identity)
        self.assertIn("does not claim seat 'codex-c'", err)
        self.assertIn("for storage 'kimi'", err)

    def test_a_launch_line_that_published_no_storage_decides_nothing(self):
        """A seat started before the storage variable existed publishes only a
        name, and a name from a lost generation cannot prove itself. The
        refusal is the same one as before this widening — absence of the fact
        is not evidence for it."""
        d, _ = self._mint()
        self._record(d, harness_name="orca", session="session-live")
        alive, opened = self._live_claude(
            d, b"ORCA_PANE_KEY=tab:leaf\0ORCA_WORKTREE_ID=workspace:/w\0"
               b"HELM_CHAT_NAME=codex-b\0")
        with alive, opened:
            identity, err = seat._live_session_orca_identity(
                d, "session-live", seat_name="codex-c")
        self.assertIsNone(identity)
        self.assertIn("for storage None", err)

    def test_spawn_backfills_session_when_sessionstart_won_the_race(self):
        d, _ = self._mint()
        self._record(d, harness_name="orca", session=None)
        sessions_dir = os.path.join(d, "claude", "sessions")
        os.makedirs(sessions_dir, exist_ok=True)
        with open(os.path.join(sessions_dir, "42.json"), "w") as f:
            json.dump({"sessionId": "session-live"}, f)
        fake = FakeOrcaAdapter(rows=[{"handle": "p9"}])
        fields = {"handle": "p9", "pane_key": "tab:leaf",
                  "pty_id": "pty-1", "worktree_id": "workspace:/w"}
        with mock.patch.object(seat, "_prove_orca_replacement",
                               return_value=({}, fields, None)):
            self.assertTrue(seat._backfill_spawn_session("codex", d, fake))
        with open(os.path.join(d, "spawn.json")) as f:
            rec = json.load(f)
        self.assertEqual(rec["session"], "session-live")
        self.assertEqual(rec["pane_key"], "tab:leaf")
        from helm import seats
        runtime, verified = seats.runtime_for_session(
            seats.roster()["codex"], "session-live")
        self.assertTrue(verified)
        self.assertEqual(runtime.get("family"), "codex")

    def test_session_start_identity_must_resolve_registered_orca_handle(self):
        rec = {"harness": "orca", "handle": "p9"}
        fake = FakeOrcaAdapter(rows=[{
            "handle": "p9", "status": "connected", "writable": True,
            "pty_id": "pty-1", "worktree_id": "workspace:/w"}],
            resolved={"handle": "p9", "pty_id": "pty-1"})
        with mock.patch.dict(os.environ, {
                "ORCA_PANE_KEY": "tab:leaf",
                "ORCA_WORKTREE_ID": "workspace:/w"}, clear=False), \
                mock.patch.object(seat.shutil, "which", return_value="/bin/orca"), \
                mock.patch.object(harness, "OrcaAdapter", return_value=fake):
            fields, err = seat._sessionstart_pane_fields(rec)
        self.assertIsNone(err)
        self.assertEqual(fields["handle"], "p9")
        self.assertEqual(fields["pane_key"], "tab:leaf")

    def test_session_start_persists_orca_remint_identity(self):
        d, _ = self._mint()
        self._record(d, harness_name="orca", session=None)
        fields = {"handle": "p9", "pane_key": "tab-1:leaf-1",
                  "pty_id": "pty-1", "worktree_id": "workspace:/w"}
        with mock.patch.object(seat, "_sessionstart_pane_fields",
                               return_value=(fields, None)):
            self.assertTrue(seat._bind_spawn_session("codex", "session-live"))
        with open(os.path.join(d, "spawn.json")) as f:
            rec = json.load(f)
        self.assertEqual(rec["session"], "session-live")
        self.assertEqual(rec["pane_key"], "tab-1:leaf-1")
        self.assertEqual(rec["worktree_id"], "workspace:/w")

    def test_same_process_session_change_refreshes_registered_session(self):
        d, _ = self._mint()
        self._record(d, harness_name="orca", session="old-session",
                     pane_key="tab:leaf", worktree_id="workspace:/w")
        fields = {"handle": "p9", "pane_key": "tab:leaf",
                  "pty_id": "pty-1", "worktree_id": "workspace:/w"}
        with mock.patch.object(seat, "_sessionstart_pane_fields",
                               return_value=(fields, None)), \
                mock.patch.object(seat, "_live_session_orca_identity",
                                  return_value=({"pid": 42,
                                                 "proc_start": "start-42"},
                                                None)), \
                mock.patch("helm.sessions.live_sids", return_value={
                    "old-session": 42, "new-session": 42}):
            self.assertTrue(seat._bind_spawn_session(
                "codex", "new-session", source="startup"))
        with open(os.path.join(d, "spawn.json")) as f:
            rec = json.load(f)
        self.assertEqual(rec["session"], "new-session")
        self.assertEqual(rec["session_pid"], 42)
        self.assertEqual(rec["session_pid_identity"], "proc:start-42")
        self.assertEqual(rec["handle"], "p9")

    def test_recorded_process_refresh_needs_no_legacy_session_census(self):
        d, _ = self._mint()
        self._record(d, harness_name="orca", session="old-session",
                     session_pid=42, session_pid_identity="proc:start-42",
                     pane_key="tab:leaf", worktree_id="workspace:/w")
        fields = {"handle": "p9", "pane_key": "tab:leaf",
                  "pty_id": "pty-1", "worktree_id": "workspace:/w"}
        with mock.patch.object(seat, "_sessionstart_pane_fields",
                               return_value=(fields, None)), \
                mock.patch.object(seat, "_live_session_orca_identity",
                                  return_value=({"pid": 42,
                                                 "proc_start": "start-42"},
                                                None)), \
                mock.patch("helm.sessions.live_sids",
                           side_effect=AssertionError("legacy census ran")):
            self.assertTrue(seat._bind_spawn_session(
                "codex", "new-session", source="compact"))
        with open(os.path.join(d, "spawn.json")) as f:
            self.assertEqual(json.load(f)["session"], "new-session")

    def test_unrelated_session_start_cannot_rebind_registered_pane(self):
        d, _ = self._mint()
        self._record(d, harness_name="orca", session="good-session",
                     pane_key="tab:leaf", worktree_id="workspace:/w")
        fields = {"handle": "p9", "pane_key": "tab:leaf",
                  "pty_id": "pty-1", "worktree_id": "workspace:/w"}
        with mock.patch.object(seat, "_sessionstart_pane_fields",
                               return_value=(fields, None)), \
                mock.patch.object(seat, "_live_session_orca_identity",
                                  return_value=({"pid": 43,
                                                 "proc_start": "start-43"},
                                                None)), \
                mock.patch("helm.sessions.live_sids", return_value={
                    "good-session": 42, "other-session": 43}):
            self.assertFalse(seat._bind_spawn_session(
                "codex", "other-session", source="startup"))
        with open(os.path.join(d, "spawn.json")) as f:
            self.assertEqual(json.load(f)["session"], "good-session")

    def test_pid_reuse_cannot_refresh_registered_session(self):
        d, _ = self._mint()
        self._record(d, harness_name="orca", session="good-session",
                     session_pid=42, session_pid_identity="proc:old-start",
                     pane_key="tab:leaf", worktree_id="workspace:/w")
        fields = {"handle": "p9", "pane_key": "tab:leaf",
                  "pty_id": "pty-1", "worktree_id": "workspace:/w"}
        with mock.patch.object(seat, "_sessionstart_pane_fields",
                               return_value=(fields, None)), \
                mock.patch.object(seat, "_live_session_orca_identity",
                                  return_value=({"pid": 42,
                                                 "proc_start": "reused-start"},
                                                None)), \
                mock.patch("helm.sessions.live_sids", return_value={
                    "other-session": 42}):
            self.assertFalse(seat._bind_spawn_session(
                "codex", "other-session", source="clear"))
        with open(os.path.join(d, "spawn.json")) as f:
            self.assertEqual(json.load(f)["session"], "good-session")

    def test_stale_orca_handle_repairs_from_exact_live_session(self):
        d, _ = self._mint()
        sid = "00000000-0000-4000-8000-000000000000"
        self._record(d, handle="old", harness_name="orca", session=sid,
                     pane_key="old-tab:old-leaf",
                     worktree_id="workspace:/w")
        row = {"handle": "new", "title": "unrelated dynamic title",
               "status": "connected", "writable": True,
               "pty_id": "pty-1", "worktree_id": "workspace:/w"}
        fake = FakeOrcaAdapter(
            rows=[row], resolved={"handle": "new", "pty_id": "pty-1",
                                  "tab_id": "reminted-tab",
                                  "leaf_id": "reminted-leaf"})
        identity = {"pid": 42, "pane_key": "old-tab:old-leaf",
                    "worktree_id": "workspace:/w"}
        with mock.patch.object(seat, "_live_session_orca_identity",
                               return_value=(identity, None)):
            ad, handle, detail = seat._resolve_registered_pane(
                "codex", d=d, adapter=fake)
        self.assertIs(ad, fake)
        self.assertEqual(handle, "new")
        self.assertIn("repaired spawn handle", detail)
        self.assertEqual(fake.pane_keys, ["old-tab:old-leaf"])
        with open(os.path.join(d, "spawn.json")) as f:
            rec = json.load(f)
        self.assertEqual(rec["handle"], "new")
        self.assertEqual(rec["pane_key"], "old-tab:old-leaf")
        self.assertEqual(rec["pty_id"], "pty-1")
        self.assertEqual(rec["session"], sid)

    def test_stale_orca_handle_can_use_a_newer_proven_session_without_rebinding(self):
        d, _ = self._mint()
        registered = "00000000-0000-4000-8000-000000000000"
        measured = "11111111-1111-4111-8111-111111111111"
        self._record(d, handle="old", harness_name="orca", session=registered,
                     pane_key="tab:leaf", worktree_id="workspace:/w")
        row = {"handle": "new", "status": "connected", "writable": True,
               "pty_id": "pty-1", "worktree_id": "workspace:/w"}
        fake = FakeOrcaAdapter(
            rows=[row], resolved={"handle": "new", "pty_id": "pty-1"})
        identity = {"pid": 42, "pane_key": "tab:leaf",
                    "worktree_id": "workspace:/w"}
        with mock.patch.object(
                seat, "_live_session_orca_identity",
                return_value=(identity, None)) as prove:
            ad, handle, detail = seat._resolve_registered_pane(
                "codex", d=d, adapter=fake, identity_session=measured)
        self.assertIs(ad, fake)
        self.assertEqual(handle, "new")
        self.assertIn("repaired spawn handle", detail)
        # The MEASURED session, not the registered one, remains this arm's
        # subject. The fourth argument is the seat's own spawn record, which the
        # proof is handed so the session-record root comes from the record the
        # decision is about rather than from an independent re-read; it is
        # matched by a field the repair does not touch.
        prove.assert_called_once_with(d, measured, "codex", mock.ANY)
        passed = prove.call_args.args[3]
        self.assertEqual((passed["seat"], passed["harness"]), ("codex", "orca"))
        with open(os.path.join(d, "spawn.json")) as f:
            rec = json.load(f)
        self.assertEqual(rec["handle"], "new")
        self.assertEqual(rec["session"], registered)

    def test_newer_session_cannot_fill_an_incomplete_spawn_identity(self):  # noqa: VACUOUS_ASSERTION — exact refusal and unchanged register are positive controls
        d, _ = self._mint()
        registered = "00000000-0000-4000-8000-000000000000"
        measured = "11111111-1111-4111-8111-111111111111"
        self._record(d, handle="old", harness_name="orca", session=registered,
                     worktree_id="workspace:/w")
        row = {"handle": "new", "status": "connected", "writable": True,
               "pty_id": "pty-1", "worktree_id": "workspace:/w"}
        fake = FakeOrcaAdapter(
            rows=[row], resolved={"handle": "new", "pty_id": "pty-1"})
        identity = {"pid": 42, "pane_key": "tab:leaf",
                    "worktree_id": "workspace:/w"}
        with mock.patch.object(
                seat, "_live_session_orca_identity",
                return_value=(identity, None)):
            ad, handle, detail = seat._resolve_registered_pane(
                "codex", d=d, adapter=fake, identity_session=measured)
        self.assertIs(ad, fake)
        self.assertIsNone(handle)
        self.assertIn("no complete pane/worktree identity", detail)
        with open(os.path.join(d, "spawn.json")) as f:
            rec = json.load(f)
        self.assertEqual(rec["handle"], "old")
        self.assertEqual(rec["session"], registered)
        self.assertNotIn("pane_key", rec)

    def test_stale_orca_orphaned_replacement_repairs_only_for_send(self):
        d, _ = self._mint()
        self._record(d, handle="old", harness_name="orca", session="s1",
                     pane_key="old-tab:old-leaf",
                     worktree_id="workspace:/w")
        row = {"handle": "new", "status": "connected", "writable": True,
               "orphaned": True, "pty_id": "pty-1",
               "worktree_id": "workspace:/w"}
        fake = FakeOrcaAdapter(
            rows=[row], resolved={"handle": "new", "pty_id": "pty-1"})
        identity = {"pid": 42, "pane_key": "old-tab:old-leaf",
                    "worktree_id": "workspace:/w"}
        with mock.patch.object(seat, "_live_session_orca_identity",
                               return_value=(identity, None)):
            ad, handle, detail = seat._resolve_registered_pane(
                "codex", d=d, adapter=fake, repair=False)
            self.assertIs(ad, fake)
            self.assertIsNone(handle)
            self.assertIn("read-live", detail)
            ad, handle, detail = seat._resolve_registered_pane(
                "codex", d=d, adapter=fake, for_send=True)
        self.assertIs(ad, fake)
        self.assertEqual(handle, "new")
        self.assertIn("SEND-ONLY", detail)
        self.assertEqual(fake.pane_keys,
                         ["old-tab:old-leaf", "old-tab:old-leaf"])
        with open(os.path.join(d, "spawn.json")) as f:
            rec = json.load(f)
        self.assertEqual(rec["handle"], "new")
        self.assertEqual(rec["pane_key"], "old-tab:old-leaf")
        ad, handle, detail = seat._resolve_registered_pane(
            "codex", d=d, adapter=fake, repair=False)
        self.assertIs(ad, fake)
        self.assertIsNone(handle)
        self.assertIn("orphaned", detail)

    def test_orca_replace_reaps_exact_live_pane_omitted_from_inventory(self):
        d, _ = self._mint()
        self._record(d, handle="old", harness_name="orca", session="s1",
                     pane_key="tab:leaf", worktree_id="workspace:/w")
        fake = FakeOrcaAdapter(
            rows=[], resolved={"handle": "current", "pty_id": "pty-1"})
        identity = {"pid": 42, "pane_key": "tab:leaf",
                    "worktree_id": "workspace:/w"}
        with mock.patch.object(seat, "_live_session_orca_identity",
                               return_value=(identity, None)), \
                mock.patch(
                    "helm.seat_lifecycle_runtime._exact_live_session_processes",
                    side_effect=lambda *_: (([], None) if fake.stopped else
                                            ([(42, "test-start")], None))):
            ad, handle, detail = seat._resolve_registered_pane(
                "codex", d=d, adapter=fake, repair=False)
            self.assertIs(ad, fake)
            self.assertIsNone(handle)
            self.assertIn("matched 0 read-live inventory rows", detail)
            rc, out, err, _, _ = self._spawn(
                ["codex", "--replace"], fake)
        self.assertEqual(rc, 0, err)
        self.assertEqual(fake.stopped, ["current"])
        self.assertEqual(fake.order[:2], ["stop", "spawn"])
        self.assertIn("reaped stale codex pane current", out)

    def test_orca_replace_inventory_ambiguity_fails_closed(self):
        d, _ = self._mint()
        row = {"handle": "current", "title": "dynamic",
               "status": "connected", "writable": True,
               "pty_id": "pty-1", "worktree_id": "workspace:/w"}
        fake = FakeOrcaAdapter(
            rows=[row],
            resolved={"handle": "current", "pty_id": "pty-1"})
        identity = {"pid": 42, "pane_key": "tab:leaf",
                    "worktree_id": "workspace:/w"}

        def record_old():
            self._record(d, handle="old", harness_name="orca", session="s1",
                         pane_key="tab:leaf", worktree_id="workspace:/w")

        record_old()
        with mock.patch.object(seat, "_live_session_orca_identity",
                               return_value=(identity, None)), \
                mock.patch(
                    "helm.seat_lifecycle_runtime._exact_live_session_processes",
                    side_effect=lambda *_: (([], None) if fake.stopped else
                                            ([(42, "test-start")], None))):
            rc, _, err, _, _ = self._spawn(["codex", "--replace"], fake)
        self.assertEqual(rc, 0, err)
        self.assertEqual(fake.stopped, ["current"])
        self.assertEqual(len(fake.spawned), 1)

        record_old()
        fake.rows.extend([dict(row), dict(row)])
        fake.stopped.clear()
        fake.spawned.clear()
        fake.order.clear()
        with mock.patch.object(seat, "_live_session_orca_identity",
                               return_value=(identity, None)), \
                mock.patch(
                    "helm.seat_lifecycle_runtime._exact_live_session_processes",
                    return_value=([(42, "test-start")], None)):
            rc, _, err, _, _ = self._spawn(["codex", "--replace"], fake)
        self.assertEqual(rc, 1)
        self.assertIn("matched 2 read-live inventory rows", err)
        self.assertIn("replacement aborted", err)
        self.assertEqual(fake.stopped, [])
        self.assertEqual(fake.spawned, [])

    def test_stale_orca_handle_ambiguity_fails_closed(self):
        d, _ = self._mint()
        self._record(d, handle="old", harness_name="orca", session="s1")
        row = {"handle": "new", "status": "connected", "writable": True,
               "pty_id": "pty-1", "worktree_id": "workspace:/w"}
        fake = FakeOrcaAdapter(
            rows=[row, dict(row)],
            resolved={"handle": "new", "pty_id": "pty-1"})
        identity = {"pid": 42, "pane_key": "tab:leaf",
                    "worktree_id": "workspace:/w"}
        with mock.patch.object(seat, "_live_session_orca_identity",
                               return_value=(identity, None)):
            ad, handle, detail = seat._resolve_registered_pane(
                "codex", d=d, adapter=fake)
        self.assertIs(ad, fake)
        self.assertIsNone(handle)
        self.assertIn("matched 2", detail)
        with open(os.path.join(d, "spawn.json")) as f:
            self.assertEqual(json.load(f)["handle"], "old")

    def test_stale_orca_dry_run_resolves_without_rewriting_register(self):
        d, _ = self._mint()
        self._record(d, handle="old", harness_name="orca", session="s1")
        fake = FakeOrcaAdapter(rows=[{
            "handle": "new", "status": "connected", "writable": True,
            "pty_id": "pty-1", "worktree_id": "workspace:/w"}],
            resolved={"handle": "new", "pty_id": "pty-1"})
        identity = {"pid": 42, "pane_key": "tab:leaf",
                    "worktree_id": "workspace:/w"}
        with mock.patch.object(seat, "_live_session_orca_identity",
                               return_value=(identity, None)):
            ad, handle, detail = seat._resolve_registered_pane(
                "codex", d=d, adapter=fake, repair=False)
        self.assertIs(ad, fake)
        self.assertEqual(handle, "new")
        self.assertIn("register unchanged in dry-run", detail)
        with open(os.path.join(d, "spawn.json")) as f:
            self.assertEqual(json.load(f)["handle"], "old")

    def test_adapter_spawn_refuses_implicit_live_replacement(self):
        d, _ = self._mint()
        self._record(d)
        fake = FakeAdapter(rows=[
            {"handle": "p9", "title": "dynamic", "status": "working"}])
        rc, _, err, wla, popen = self._spawn(["codex"], fake)
        self.assertEqual(rc, 1)
        self.assertIn("pass --replace", err)
        self.assertEqual(fake.stopped, [])
        self.assertEqual(fake.spawned, [])
        wla.assert_not_called()
        popen.assert_not_called()

    def test_disconnected_registered_handle_does_not_need_replace(self):
        d, _ = self._mint()
        self._record(d)
        fake = FakeAdapter(rows=[
            {"handle": "p9", "title": "dynamic", "status": "disconnected"}])
        rc, _, err, _, _ = self._spawn(["codex"], fake)
        self.assertEqual(rc, 0, err)
        self.assertEqual(fake.stopped, [])
        self.assertEqual(len(fake.spawned), 1)

    def test_adapter_reaps_registered_pane_before_spawn(self):
        d, _ = self._mint()
        self._record(d)
        fake = FakeAdapter(rows=[
            {"handle": "p9", "title": "dynamic", "status": "idle"}])
        rc, out, err, _, _ = self._spawn(["codex", "--replace"], fake)
        self.assertEqual(rc, 0, err)
        self.assertEqual(fake.stopped, ["p9"])
        self.assertEqual(fake.order[0], "stop")    # reap strictly first
        self.assertIn("reaped stale codex pane p9", out)

    def test_registered_pane_plus_title_only_decoy_refuses_without_stopping(self):
        d, _ = self._mint()
        self._record(d)
        fake = FakeAdapter(rows=[
            {"handle": "p9", "title": "dynamic", "status": "idle"},
            {"handle": "p2", "title": "codex", "status": "idle"}])
        rc, _, err, wla, popen = self._spawn(["codex"], fake)
        self.assertEqual(rc, 1)
        self.assertEqual(fake.stopped, [])
        self.assertEqual(fake.spawned, [])
        wla.assert_not_called()
        popen.assert_not_called()
        self.assertIn("identity-by-title", err)

    def test_adapter_reap_failure_aborts_replacement(self):
        d, _ = self._mint()
        self._record(d)
        fake = FakeAdapter(rows=[{"handle": "p9", "title": "dynamic"}])
        fake.stop = mock.Mock(side_effect=harness.HarnessError("close failed"))
        rc, _, err, wla, popen = self._spawn(["codex", "--replace"], fake)
        self.assertEqual(rc, 1)
        self.assertIn("pane p9 NOT reaped", err)
        self.assertIn("replacement aborted", err)
        self.assertEqual(fake.spawned, [])
        wla.assert_not_called()
        popen.assert_not_called()

    def test_unregistered_same_title_aborts_without_stopping(self):
        self._mint()
        fake = FakeAdapter(rows=[{"handle": "decoy", "title": "codex"}])
        rc, _, err, wla, popen = self._spawn(["codex"], fake)
        self.assertEqual(rc, 1)
        self.assertEqual(fake.stopped, [])
        self.assertEqual(fake.spawned, [])
        wla.assert_not_called()
        popen.assert_not_called()
        self.assertIn("identity-by-title", err)

    def test_an_aborted_replace_launches_nothing_and_leaves_the_register(self):  # noqa: VACUOUS_ASSERTION — the resolved-stale control at the end asserts the SAME observables positively (one spawn recorded, wla called, a handle in the register), so the empty-spy asserts above cannot pass by the fixture doing nothing
        """task/2798 — THE ABORT AND THE LAUNCH ARE ONE DECISION.

        Measured on the grok seat: `helm seat spawn grok --replace` printed
        "replacement aborted; resolve the stale same-name seat before retrying"
        and the operator then found a live pane for that seat anyway, with the
        register naming no handle. Whatever produced that pane, the sentence is
        what made it unreadable: an operator who is told a replace aborted and
        is NOT told that nothing was launched and nothing was re-pointed cannot
        tell which register to trust, and the sentence named no door out.

        THE FOUR FACTS THE REFUSAL NOW OWES, driven together because they are
        one decision: nothing launched (the adapter's spawn spy and Popen), no
        launch asset re-minted, the register BYTE-IDENTICAL across the refused
        call, and a refusal that names the register path plus the verb that
        clears the ghost.

        CONTROL, in this arm: the same seat with the same adapter and a
        RESOLVED stale record — the pane is stoppable — spawns exactly once,
        writes a register with a handle, and prints none of the abort prose. Without
        it, a `_spawn` that refused unconditionally would pass every assertion
        above. Blast radius: `_spawn_reap` and both legs' returns on it.
        """
        d, _ = self._mint()
        self._record(d)
        spawn_json = os.path.join(d, "spawn.json")
        with open(spawn_json, "rb") as f:
            before = f.read()
        fake = FakeAdapter(rows=[{"handle": "p9", "title": "dynamic"}])
        fake.stop = mock.Mock(side_effect=harness.HarnessError("close failed"))
        rc, _out, err, wla, popen = self._spawn(["codex", "--replace"], fake)
        self.assertEqual(rc, 1, err)
        self.assertIn("replacement aborted", err)
        self.assertIn("NOTHING was launched", err)
        self.assertIn("register was not re-pointed", err)
        # the shipped phrase three other arms assert the ABSENCE of, kept
        self.assertIn("resolve the stale same-name seat", err)
        self.assertIn(spawn_json, err)           # the exact stale evidence
        self.assertIn("helm seat rebind codex --apply", err)   # the verb
        self.assertEqual(fake.spawned, [], "an aborted replace launches nothing")
        popen.assert_not_called()
        wla.assert_not_called()
        with open(spawn_json, "rb") as f:
            self.assertEqual(f.read(), before,
                             "a refused replace must not rewrite the register")

        # CONTROL: the stale record RESOLVES (the pane stops), so the same call
        # launches once and registers a handle.
        self._record(d)
        ok = FakeAdapter(rows=[{"handle": "p9", "title": "dynamic"}])
        rc, _out, err, wla, popen = self._spawn(["codex", "--replace"], ok)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("replacement aborted", err)
        self.assertEqual(len(ok.spawned), 1, err)
        wla.assert_called()
        with open(spawn_json) as f:
            self.assertEqual(json.load(f)["handle"], "pane-1")

    def test_every_refused_replace_leaves_the_register_and_archive_untouched(self):  # noqa: VACUOUS_ASSERTION — the empty write-spy and empty archive lists are each asserted NON-empty by the two launch controls at the end, which drive the same fixtures through the same verb with the refusal removed
        """task/2798 — THE REFUSAL AND EVERY WRITE ARE ONE TRANSACTION.

        `_spawn_reap` tells the operator the register "was not re-pointed", and
        two writes sit upstream of refusals that can still follow them: resolving
        a reminted pane can REPAIR its handle into spawn.json, and a terminal
        pane can be ARCHIVED, each before the title-only refusal, the
        no-`--replace` refusal or a stop that fails. A verb that returns 1 after
        either has rewritten, or removed, the one record `seat where`, `rebind`
        and the next reap all read. One level up the shape repeats: a surface
        the re-mint could never write is a read-only question, and asking it
        after the reap stops the live pane and archives its register in order
        to refuse.

        EVERY CASE DRIVES THE REAL VERB to a refusal that a write could precede,
        and reads four facts the filesystem cannot fake: spawn.json's
        bytes, its mtime, its inode (an atomic rewrite of identical bytes moves
        it), and the archive listing. The write spy is there for the rewrite
        that restores itself.

        CONTROLS, in this arm: the reminted fixture with a pane that DOES stop,
        and the terminal fixture with no decoy, each launch — one archive entry
        naming the pane that was actually closed, writes observed by the same
        spy, a register naming the new pane. Blast radius: `_reap_stale`,
        `_reminted_pane_fields`, and the pre-reap refusals in `_spawn`.
        """
        d, _ = self._mint()
        spawn_json = os.path.join(d, "spawn.json")
        reminted = dict(handle="old", harness_name="orca", session="s1",
                        pane_key="tab:leaf", worktree_id="workspace:/w")
        current = {"handle": "current", "title": "dynamic",
                   "status": "connected", "writable": True,
                   "pty_id": "pty-1", "worktree_id": "workspace:/w"}
        decoy = {"handle": "decoy", "title": "codex", "status": "connected"}
        identity = {"pid": 42, "pane_key": "tab:leaf",
                    "worktree_id": "workspace:/w"}

        def orca(*rows):
            return FakeOrcaAdapter(
                rows=rows, resolved={"handle": "current", "pty_id": "pty-1"})

        def unstoppable(fake):
            fake.stop = mock.Mock(
                side_effect=harness.HarnessError("close failed"))
            return fake

        def observe():
            st = os.stat(spawn_json)
            with open(spawn_json, "rb") as f:
                return (f.read(), st.st_mtime_ns, st.st_ino,
                        tuple(seat_exit_owner.archive_paths(d)))

        def drive(record, fake, args):
            self._record(d, **record)
            before = observe()
            with mock.patch.object(seat, "_live_session_orca_identity",
                                   return_value=(identity, None)), \
                    mock.patch(
                        "helm.seat_lifecycle_runtime."
                        "_exact_live_session_processes",
                        side_effect=lambda *_: (
                            ([], None) if fake.stopped
                            else ([(42, "test-start")], None))), \
                    mock.patch.object(pk, "write_json",
                                      wraps=pk.write_json) as spy:
                rc, _out, err, wla, popen = self._spawn(args, fake)
            writes = [c for c in spy.call_args_list
                      if c.args and c.args[0] == spawn_json]
            return rc, err, wla, popen, writes, before

        outside = os.path.join(self.tmp, "another-seats-claude")
        os.makedirs(outside)
        alias = os.path.join(d, "claude")  # noqa: SEAT_NAME — the instance's config directory, not a seat
        in_flight = dict(seat._new_spawn_attempt(), pid=os.getppid(),
                         pid_identity="test-start")
        refusals = (
            ("a reminted pane that will not stop", reminted,
             lambda: unstoppable(orca(current)), ["codex", "--replace"],
             "NOT reaped", None),
            ("a title-only decoy beside a reminted pane", reminted,
             lambda: orca(current, decoy), ["codex", "--replace"],
             "identity-by-title", None),
            ("a reminted live pane and no --replace", reminted,
             lambda: orca(current), ["codex"], "pass --replace", None),
            ("a title-only decoy beside a terminal pane", {},
             lambda: FakeAdapter(rows=[
                 {"handle": "p9", "title": "dynamic", "status": "disconnected"},
                 dict(decoy, handle="p2")]),
             ["codex", "--replace"], "identity-by-title", None),
            ("another spawn's attempt in flight", {"attempt": in_flight},
             lambda: FakeAdapter(rows=[{"handle": "p9", "title": "dynamic"}]),
             ["codex", "--replace"], "IN FLIGHT", None),
            ("a surface the re-mint could never write", {},
             lambda: FakeAdapter(rows=[{"handle": "p9", "title": "dynamic"}]),
             ["codex", "--replace"], "resolves outside its own instance",
             lambda: os.symlink(outside, alias, target_is_directory=True)),
        )
        for label, record, adapter, args, needle, arrange in refusals:
            with self.subTest(refusal=label):
                if arrange:
                    arrange()
                    self.addCleanup(
                        lambda: os.path.islink(alias) and os.unlink(alias))
                fake = adapter()
                rc, err, wla, popen, writes, before = drive(record, fake, args)
                self.assertEqual(rc, 1, err)
                self.assertIn(needle, err)
                self.assertEqual(observe(), before,
                                 "a refused replace changed the register or "
                                 "its archive")
                self.assertEqual(writes, [])
                self.assertEqual(fake.spawned, [])
                self.assertEqual(fake.stopped, [])
                wla.assert_not_called()
                popen.assert_not_called()
                if arrange:
                    os.unlink(alias)

        # CONTROL 1: the reminted pane STOPS. The archive names the pane that
        # was closed — the reminted handle and pty, never the stale register's.
        fake = orca(current)
        rc, err, _wla, _popen, writes, before = drive(
            reminted, fake, ["codex", "--replace"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(fake.stopped, ["current"])
        self.assertTrue(writes, "the launch leg must be seen by the write spy")
        archived = seat_exit_owner.archive_paths(d)
        self.assertEqual(len(archived), 1, archived)
        closed, unavailable = seat_exit_owner.latest_archived_exit(d, "codex")
        self.assertIsNone(unavailable)
        self.assertEqual((closed["handle"], closed["pty_id"],
                          closed["terminal"]["handle"]),
                         ("current", "pty-1", "current"))
        with open(spawn_json) as f:
            self.assertEqual(json.load(f)["handle"], "pane-1")
        self.assertNotEqual(observe()[:1], before[:1])

        # CONTROL 2: the terminal pane with NO decoy archives once and launches.
        shutil.rmtree(os.path.join(d, "spawn.archive"))
        fake = FakeAdapter(rows=[
            {"handle": "p9", "title": "dynamic", "status": "disconnected"}])
        rc, err, _wla, _popen, writes, _before = drive(
            {}, fake, ["codex", "--replace"])
        self.assertEqual(rc, 0, err)
        self.assertTrue(writes)
        closed, unavailable = seat_exit_owner.latest_archived_exit(d, "codex")
        self.assertIsNone(unavailable)
        self.assertEqual(closed["terminal"]["handle"], "p9")
        self.assertEqual(len(seat_exit_owner.archive_paths(d)), 1)

    def test_cross_seat_register_never_authorizes_reap(self):
        d, _ = self._mint()
        with open(os.path.join(d, "spawn.json"), "w") as f:
            json.dump({"v": 1, "seat": "codex-2", "harness": "fake",
                       "handle": "p9"}, f)
        fake = FakeAdapter(rows=[{"handle": "p9", "title": "dynamic"}])
        rc, _, err, wla, popen = self._spawn(["codex"], fake)
        self.assertEqual(rc, 1)
        self.assertIn("identity mismatch", err)
        self.assertEqual(fake.stopped, [])
        self.assertEqual(fake.spawned, [])
        wla.assert_not_called()
        popen.assert_not_called()

    def test_recorded_other_harness_must_be_reapable(self):
        d, _ = self._mint()
        with open(os.path.join(d, "spawn.json"), "w") as f:
            json.dump({"v": 1, "seat": "codex", "harness": "orca",
                       "handle": "old-pane"}, f)
        fake = FakeAdapter()
        with mock.patch.object(seat.shutil, "which", return_value=None):
            rc, _, err, wla, _ = self._spawn(["codex"], fake)
        self.assertEqual(rc, 1)
        self.assertIn("recorded orca adapter is unavailable", err)
        self.assertEqual(fake.spawned, [])
        wla.assert_not_called()

    def test_adapter_command_never_carries_the_seat_token(self):
        d, _ = self._mint()
        with open(os.path.join(seat.seat_dir("codex"), "token"), "w") as f:
            f.write("super-secret-token\n")
        fake = FakeAdapter()
        rc, _, err, _, _ = self._spawn(["codex"], fake)
        self.assertEqual(rc, 0, err)
        command, _, _ = fake.spawned[0]
        self.assertNotIn("super-secret-token", command)
        self.assertNotIn("ANTHROPIC", command)
        _, text, _ = fake.sent[0]
        self.assertNotIn("super-secret-token", text)

    def test_room_recovered_from_launch_sh_when_no_flag(self):
        d, launch = self._mint(room="team-q")
        fake = FakeAdapter()
        rc, _, err, wla, _ = self._spawn(["codex"], fake)
        self.assertEqual(rc, 0, err)
        self.assertEqual(wla.call_args[0][2], "team-q")   # room preserved
        _, text, _ = fake.sent[0]
        self.assertEqual(text, "Run `helm seat boot-brief` and follow it.")
        with open(os.path.join(d, "spawn.json")) as f:
            rec = json.load(f)
        self.assertEqual(rec["room"], "team-q")

    def test_spawn_remint_preserves_multi_shape(self):  # noqa: VACUOUS_ASSERTION — both successful remints positively assert their opposite multi values across terminal archival
        """The re-mint must recover --multi from the old launch: pin absence
        stays pinless, while a normal seat stays pinned."""
        self._mint(multi=True)
        first = FakeAdapter()
        rc, _, err, wla, _ = self._spawn(["codex"], first)
        self.assertEqual(rc, 0, err)
        self.assertIs(wla.call_args.kwargs["multi"], True)
        path = os.path.join(seat._instance_dir("codex", "codex"), "spawn.json")
        with open(path) as f:
            rec = json.load(f)
        rec["session"] = "session-a"
        with open(path, "w") as f:
            json.dump(rec, f)
        self._mint(multi=False)
        ended = FakeAdapter(rows=[
            {"handle": "pane-1", "status": "disconnected"}])
        rc, _, err, wla, _ = self._spawn(["codex"], ended)
        self.assertEqual(rc, 0, err)
        self.assertIs(wla.call_args.kwargs["multi"], False)

    def test_room_less_spawn_mirror_never_invents_a_main_home(self):
        """The 'main' scatter writer, as-prevented: no --room, no launch.sh
        room -> the spawn record carries room=None and the roster mirror
        writes NO home (the SessionStart join derives the real one)."""
        d, _ = self._mint()                        # launch.sh without a room
        fake = FakeAdapter()
        rc, _, err, _, _ = self._spawn(["codex"], fake)
        self.assertEqual(rc, 0, err)
        with open(os.path.join(d, "spawn.json")) as f:
            rec = json.load(f)
        self.assertIsNone(rec["room"])             # never a defaulted 'main'
        self.assertIsNone(rec["room_source"])
        from helm import seats
        row = seats.roster().get("codex") or {}
        self.assertNotIn("home_room", row)
        self.assertNotIn("home_room_source", row)

    def test_derived_launch_room_mirror_cannot_downgrade_operator_home(self):
        """A launch.sh room the seam stamped derived stays derived through the
        spawn mirror — it must NEVER clobber a deliberate (explicit) home."""
        from helm import seats
        seats.join(session="s-pin", seat="codex", cwd="/tmp/p",
                   room="team-ops")               # the deliberate home
        d = seat._instance_dir("codex", "codex")
        os.makedirs(d, exist_ok=True)
        launch = os.path.join(d, "launch.sh")
        with open(launch, "w") as f:
            f.write("#!/bin/sh\nexec env FAKE=1 HELM_CHAT_ROOM=proj-x "
                    "HELM_CHAT_ROOM_SOURCE=derived "
                    "CLAUDE_CODE_SUBAGENT_MODEL=gpt-5.6-sol claude \"$@\"\n")
        os.chmod(launch, 0o700)
        rc, _, err, _, _ = self._spawn(["codex"], FakeAdapter())
        self.assertEqual(rc, 0, err)
        with open(os.path.join(d, "spawn.json")) as f:
            rec = json.load(f)
        self.assertEqual((rec["room"], rec["room_source"]),
                         ("proj-x", "derived"))    # provenance recorded
        row = seats.roster()["codex"]
        self.assertEqual((row["home_room"], row["home_room_source"]),
                         ("team-ops", "explicit"))  # never downgraded

    def test_spawn_remint_carries_derived_provenance_into_the_script(self):
        """The provenance-laundering hole (codex): _spawn recovered
        room_source from the old launch.sh, then dropped it at
        _write_launch_assets — the reminted script carried
        HELM_CHAT_ROOM=proj-x with NO HELM_CHAT_ROOM_SOURCE=derived, so the
        child's SessionStart join upgraded derived to explicit and could
        overwrite an operator-set home. NON-VACUOUS: asset writing is NOT
        mocked here — read the ACTUAL reminted launch.sh and assert the
        derived stamp survives the round trip (fails without the
        room_source= thread)."""
        d = seat._instance_dir("codex", "codex")
        os.makedirs(d, exist_ok=True)
        launch = os.path.join(d, "launch.sh")
        with open(launch, "w") as f:
            f.write("#!/bin/sh\nexec env FAKE=1 HELM_CHAT_ROOM=proj-x "
                    "HELM_CHAT_ROOM_SOURCE=derived "
                    "CLAUDE_CODE_SUBAGENT_MODEL=gpt-5.6-sol claude \"$@\"\n")
        os.chmod(launch, 0o700)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(harness, "detect",
                               return_value=FakeAdapter()), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(["spawn", "codex"])
        self.assertEqual(rc, 0, err.getvalue())
        with open(launch) as f:
            text = f.read()
        self.assertIn("HELM_CHAT_ROOM=proj-x", text)
        self.assertIn("HELM_CHAT_ROOM_SOURCE=derived", text)
        # and the recovery seam reads the reminted pair back unchanged —
        # the NEXT spawn/resume sees derived too, not a laundered explicit
        self.assertEqual(seat._homing_from_launch(launch),
                         ("proj-x", "derived"))

    def test_spawn_remints_legacy_launch_before_adapter_launch(self):
        """A pre-child-stamp launch.sh must be replaced from current code
        before the adapter starts it, or the child is born MEMORY-ONLY."""
        _, launch = self._mint()
        seen = {}

        class ReadingAdapter(FakeAdapter):
            def spawn(self, command, title=None, cwd=None):
                with open(launch) as f:
                    seen["launch"] = f.read()
                return super().spawn(command, title=title, cwd=cwd)

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(harness, "detect", return_value=ReadingAdapter()), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(["spawn", "codex"])
        self.assertEqual(rc, 0, err.getvalue())
        self.assertIn("unset CLAUDE_CODE_CHILD_SESSION", seen["launch"])
        self.assertIn("exec env -u ANTHROPIC_API_KEY", seen["launch"])

    def test_onboarding_failure_closes_incomplete_pane(self):
        self._mint()
        fake = FakeAdapter()
        fake.send = mock.Mock(side_effect=harness.HarnessError("send failed"))
        rc, _, err, _, _ = self._spawn(["codex"], fake)
        self.assertEqual(rc, 1)
        self.assertEqual(fake.stopped, ["pane-1"])
        self.assertIn("incomplete pane pane-1 closed", err)

    def test_adapter_register_failure_closes_untracked_pane(self):
        """The register a pane spawn cannot COMPLETE. The pending identity is
        published BEFORE `ad.spawn`, so a publication failure creates no pane and
        has none to close; the untracked pane is the one whose FINALIZE fails."""
        self._mint()
        fake = FakeAdapter()
        with mock.patch.object(seat, "_register_spawn",
                               side_effect=[True, False]):
            rc, _, _, _, _ = self._spawn(["codex"], fake)
        self.assertEqual(rc, 1)
        self.assertEqual(fake.stopped, ["pane-1"])

    def test_orca_path_end_to_end_exact_cli_calls(self):
        """The real OrcaAdapter under spawn, at the ARGV: terminal list (reap
        scan) + create + the SPLIT submit — text with NO --enter, then a BARE
        --enter, then the composer read-back that proves the pane advanced."""
        d, launch = self._mint()
        advanced = FakeProc(_orca_reply({"terminal": {
            "tail": ADVANCED_PANE.splitlines()}}))
        wire = "Run `helm seat boot-brief` and follow it."
        held = FakeProc(_orca_reply({"terminal": {"tail":
            ADVANCED_PANE.replace("\n❯\n", "\n❯\xa0%s\n" % wire).splitlines()}}))
        replies = [FakeProc(_orca_reply({"terminals": []})),
                   FakeProc(_orca_reply({"terminal": {"handle": "t7"}})),
                   advanced,                           # clean composer PRE-read
                   FakeProc(_orca_reply({})),          # text, no Enter
                   held,                               # exact pre-Enter proof
                   FakeProc(_orca_reply({})),          # the bare Enter
                   advanced,                           # advance verification
                   FakeProc(_orca_reply({"terminals": []}))]  # session backfill
        ad = harness.OrcaAdapter("/fake/bin/orca")
        with mock.patch.object(harness.subprocess, "run",
                               side_effect=replies) as run:
            rc, out, err, _, _ = self._spawn(["codex"], ad)
        self.assertEqual(rc, 0, err)
        calls = [c[0][0] for c in run.call_args_list]
        self.assertEqual(calls[0], ["/fake/bin/orca", "terminal", "list",
                                    "--json"])
        self.assertEqual(calls[1], ["/fake/bin/orca", "terminal", "create",
                                    "--worktree", "path:" + self.home,
                                    "--title", "codex", "--command",
                                    self._launch_line(d, launch), "--json"])
        self.assertEqual(calls[2][:5], ["/fake/bin/orca", "terminal", "read",
                                        "--terminal", "t7"],
                         "the composer is read BEFORE anything is typed")
        self.assertEqual(calls[3][:6], ["/fake/bin/orca", "terminal", "send",
                                        "--terminal", "t7", "--text"])
        self.assertEqual(calls[3][6],
                         "Run `helm seat boot-brief` and follow it.")
        self.assertEqual(calls[3][7:], ["--json"],
                         "the text leg must NOT carry --enter")
        self.assertEqual(calls[4][:5], ["/fake/bin/orca", "terminal", "read",
                                        "--terminal", "t7"],
                         "the exact typed composer is re-read before Enter")
        self.assertEqual(calls[5], ["/fake/bin/orca", "terminal", "send",
                                    "--terminal", "t7", "--text", "",
                                    "--enter", "--json"])
        self.assertEqual(calls[6][:5], ["/fake/bin/orca", "terminal", "read",
                                        "--terminal", "t7"])

    def test_herdr_path_end_to_end_exact_cli_calls(self):
        """The real HerdrAdapter under spawn: pane list + agent start + the
        SPLIT submit (send-text, then a bare `pane run`), then the composer
        read-back. Exact argv, subprocess fully mocked."""
        d, launch = self._mint()
        advanced = FakeProc(_herdr_reply({"read": {"text": ADVANCED_PANE}}))
        wire = "Run `helm seat boot-brief` and follow it."
        held = FakeProc(_herdr_reply({"read": {"text":
            ADVANCED_PANE.replace("\n❯\n", "\n❯\xa0%s\n" % wire)}}))
        replies = [FakeProc(_herdr_reply({"panes": []})),
                   FakeProc(_herdr_reply({"agent": {"pane_id": "w1:p1"}})),
                   advanced,                            # clean composer PRE-read
                   FakeProc(_herdr_reply({})),          # send-text, no Enter
                   held,                                # exact pre-Enter proof
                   FakeProc(_herdr_reply({})),          # the bare Enter
                   advanced]                            # advance verification
        ad = harness.HerdrAdapter("/fake/bin/herdr")
        with mock.patch.object(harness.subprocess, "run",
                               side_effect=replies) as run:
            rc, out, err, _, _ = self._spawn(["codex"], ad)
        self.assertEqual(rc, 0, err)
        calls = [c[0][0] for c in run.call_args_list]
        self.assertEqual(calls[0], ["/fake/bin/herdr", "pane", "list"])
        self.assertEqual(calls[1], ["/fake/bin/herdr", "agent", "start",
                                    "codex", "--cwd", self.home,
                                    "--no-focus", "--", "sh", "-lc",
                                    self._launch_line(d, launch)])
        # THE SPLIT, at herdr's own argv: `pane send-text` is literal
        # keystrokes with no Enter; `pane run` is text+Enter, so a bare Enter
        # is `pane run <handle> ""`. Both adapters inherit ONE submit, and this
        # is what that one definition looks like on the other CLI.
        self.assertEqual(calls[2][:5], ["/fake/bin/herdr", "pane", "read",
                                        "w1:p1", "--source"],
                         "the composer is read BEFORE anything is typed")
        self.assertEqual(calls[3][:4], ["/fake/bin/herdr", "pane", "send-text",
                                        "w1:p1"])
        self.assertEqual(calls[3][4],
                         "Run `helm seat boot-brief` and follow it.")
        self.assertEqual(calls[4][:5], ["/fake/bin/herdr", "pane", "read",
                                        "w1:p1", "--source"],
                         "the exact typed composer is re-read before Enter")
        self.assertEqual(calls[5], ["/fake/bin/herdr", "pane", "run",
                                    "w1:p1", ""])
        self.assertEqual(calls[6][:5], ["/fake/bin/herdr", "pane", "read",
                                        "w1:p1", "--source"])


    def test_unreadable_session_record_makes_dead_pane_proof_unknown(self):  # noqa: VACUOUS_ASSERTION — UNKNOWN rc/text positively control retained active record and absent archive/spawn
        d, _ = self._mint()
        self._record(d, handle="old-pane", harness_name="orca",
                     session="session-a")
        records = os.path.join(d, "claude", "sessions")
        os.makedirs(records)
        with open(os.path.join(records, "unreadable.json"), "w") as f:
            f.write("{not-json\n")
        fake = FakeOrcaAdapter(rows=[])
        rc, _, err, wla, _ = self._spawn(["codex"], fake)
        self.assertEqual(rc, 1)
        self.assertIn("session process census is UNKNOWN", err)
        self.assertTrue(os.path.exists(os.path.join(d, "spawn.json")))
        self.assertEqual(seat_exit_owner.archive_paths(d), [])
        self.assertEqual(fake.spawned, [])
        wla.assert_not_called()

    def test_a_title_decoy_refuses_before_a_proven_dead_pane_is_archived(self):  # noqa: VACUOUS_ASSERTION — the control at the end removes the decoy from the same fixture and asserts the archive IS written once and the register IS replaced, so the empty archive above is the refusal's and not a reap that never archives
        """A PROVEN-DEAD pane is still not archived by a spawn that REFUSES.

        The refusal prints that the register was not re-pointed, and an archive
        is the register being moved away: the seat is left with no active
        record under a sentence saying nothing changed. The title-only decoy is
        answerable from the inventory the reap already holds, so it is answered
        first and the terminal proof is left for the spawn that will use it.
        """
        d, _ = self._mint()
        spawn_json = os.path.join(d, "spawn.json")
        self._record(d, handle="old-pane", harness_name="orca",
                     session="session-a")
        with open(spawn_json, "rb") as f:
            before = f.read()
        fake = FakeOrcaAdapter(rows=[
            {"handle": "decoy", "title": "codex", "status": "connected"}])
        with mock.patch(
                "helm.seat_lifecycle_runtime._exact_live_session_processes",
                return_value=([], None)):
            rc, out, err, wla, _ = self._spawn(["codex"], fake)
        self.assertEqual(rc, 1)
        self.assertNotIn("archived pane-closed transition", out)
        self.assertIn("identity-by-title", err)
        with open(spawn_json, "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(seat_exit_owner.archive_paths(d), [])
        self.assertEqual(fake.spawned, [])
        wla.assert_not_called()

        # CONTROL: no decoy — the same proven-dead pane is archived once and
        # the spawn replaces the register.
        clear = FakeOrcaAdapter()
        with mock.patch(
                "helm.seat_lifecycle_runtime._exact_live_session_processes",
                return_value=([], None)):
            rc, out, err, wla, _ = self._spawn(["codex"], clear)
        self.assertEqual(rc, 0, err)
        self.assertIn("archived pane-closed transition", out)
        self.assertEqual(len(seat_exit_owner.archive_paths(d)), 1)
        self.assertEqual(len(clear.spawned), 1)
        with open(spawn_json) as f:
            self.assertEqual(json.load(f)["handle"], "pane-1")

    def test_stop_success_without_terminal_proof_preserves_spawn_record(self):  # noqa: VACUOUS_ASSERTION — terminal-NOT-proven rc/text positively control retained record and absent archive/spawn
        d, _ = self._mint()
        self._record(d)
        fake = FakeAdapter(
            rows=[{"handle": "p9", "title": "dynamic", "status": "idle"}],
            stop_removes=False)
        with mock.patch.object(seat_exit_owner, "_PANE_CLOSE_POLLS", 1), \
                mock.patch.object(seat_exit_owner, "_PANE_CLOSE_DELAY", 0):
            rc, _, err, wla, _ = self._spawn(["codex", "--replace"], fake)
        self.assertEqual(rc, 1)
        self.assertIn("terminal state NOT proven", err)
        self.assertTrue(os.path.exists(os.path.join(d, "spawn.json")))
        self.assertEqual(seat_exit_owner.archive_paths(d), [])
        self.assertEqual(fake.spawned, [])
        wla.assert_not_called()

    def test_delayed_terminal_pane_state_archives_then_replaces(self):
        d, _ = self._mint()
        self._record(d)
        live = {"handle": "p9", "title": "dynamic", "status": "idle"}
        closed = dict(live, status="closed")
        fake = FakeAdapter(rows=[live], stop_removes=False)
        after_stop = [live, closed]

        def listed():
            if not fake.stopped:
                return [live]
            return [after_stop.pop(0) if after_stop else closed]

        fake.list = listed
        with mock.patch.object(seat_exit_owner, "_PANE_CLOSE_DELAY", 0):
            rc, out, err, _, _ = self._spawn(["codex", "--replace"], fake)
        self.assertEqual(rc, 0, err)
        self.assertIn("reaped stale codex pane p9", out)
        self.assertEqual(len(seat_exit_owner.archive_paths(d)), 1)


class SparkModelPersistenceTest(SpawnBase):
    """land af391eab: --model wiring was SPAWN-ONLY while launch.sh REFRESHES.

    Five writer sites re-minted with no model (resume, launch, and the three
    add sites), so the FIRST resume of a spark seat rewrote its launch.sh
    back to the family default — sol + a 320k window on a model whose real
    input ceiling is 76k, the overstated-window direction compaction cannot
    recover from. spawn now PERSISTS the explicit model in spawn.json and
    every writer re-derives it (_persisted_model). These arms drive the REAL
    _write_launch_assets (never a mock of it) and assert the EFFECT — the
    model value in the written launch.sh and in the seat record — never the
    absence of an error."""

    SPARK = "gpt-5.3-codex-spark"

    def _drive(self, argv, adapter):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(harness, "detect", return_value=adapter), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(argv)
        return rc, out.getvalue(), err.getvalue()

    def _resume_adapter(self):
        return FakeAdapter(rows=[{"handle": "pane-1", "title": "codex",
                                  "status": "idle"}])

    def _bind_test_session(self, d):
        """Model persistence starts after SessionStart supplied exact identity."""
        sid = "11111111-1111-1111-1111-111111111111"
        project = os.path.join(d, "claude", "projects", "test")
        os.makedirs(project, exist_ok=True)
        with open(os.path.join(project, sid + ".jsonl"), "w") as f:
            f.write(json.dumps({"type": "assistant", "sessionId": sid,
                                "cwd": self.home}) + "\n")
        path = os.path.join(d, "spawn.json")
        with open(path) as f:
            rec = json.load(f)
        rec["session"] = sid
        with open(path, "w") as f:
            json.dump(rec, f)
        return sid

    def test_resume_of_a_spark_seat_keeps_model_context_76000(self):
        """THE ARM named in the verdict. spawn --model spark, then TWO
        resumes: launch.sh and spawn.json must still say spark/76000 after
        each. The second resume is load-bearing — resume re-registers
        spawn.json, so a persist that survives the mint but is dropped from
        the rebuilt record would revert exactly one resume later."""
        d, launch = self._mint()
        rc, _, err = self._drive(
            ["spawn", "codex", "--model", self.SPARK, "--cwd", self.home],
            FakeAdapter())
        self.assertEqual(rc, 0, err)
        with open(os.path.join(d, "spawn.json")) as f:
            self.assertEqual(json.load(f).get("model"), self.SPARK)
        self._bind_test_session(d)
        with open(launch) as f:
            minted = f.read()
        self.assertIn("--model %s" % self.SPARK, minted)
        self.assertIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS=76000", minted)
        for n in (1, 2):
            rc, _, err = self._drive(["resume", "codex"],
                                     self._resume_adapter())
            self.assertEqual(rc, 0, "resume %d: %s" % (n, err))
            with open(launch) as f:
                refreshed = f.read()
            self.assertIn("--model %s" % self.SPARK, refreshed,
                          "resume %d reverted the model" % n)
            self.assertIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS=76000", refreshed)
            self.assertNotIn("320000", refreshed)
            with open(os.path.join(d, "spawn.json")) as f:
                self.assertEqual(json.load(f).get("model"), self.SPARK,
                                 "resume %d dropped the persisted model" % n)

    def test_default_seat_still_tracks_the_family_model_through_resume(self):
        """POSITIVE CONTROL on the same observable: with NO --model the
        refresh DOES write the family default (sol + 320k) — which is what
        makes the spark arm's assertNotIn(320000) a measurement and not a
        vacuous absence — and the record persists model None. Only an
        explicit choice is sticky: a default-following seat must keep
        tracking the family so a catalog default change still propagates."""
        d, launch = self._mint()
        rc, _, err = self._drive(["spawn", "codex", "--cwd", self.home],
                                 FakeAdapter())
        self.assertEqual(rc, 0, err)
        with open(os.path.join(d, "spawn.json")) as f:
            rec = json.load(f)
        self.assertIn("model", rec)
        self.assertIsNone(rec["model"])
        self._bind_test_session(d)
        rc, _, err = self._drive(["resume", "codex"], self._resume_adapter())
        self.assertEqual(rc, 0, err)
        with open(launch) as f:
            refreshed = f.read()
        self.assertIn("--model gpt-6-astra", refreshed)
        self.assertIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS=220000", refreshed)
        self.assertNotIn("76000", refreshed)


class DryRunTest(SpawnBase):
    def test_print_refuses_title_only_identity_without_spawning(self):  # noqa: VACUOUS_ASSERTION — refusal text positively controls the intentional no-actuation assertions
        d, launch = self._mint()
        fake = FakeAdapter(rows=[{"handle": "p9", "title": "codex",
                                  "status": "idle"}])
        rc, out, err, wla, popen = self._spawn(["codex", "--print"], fake)
        self.assertEqual(rc, 0, err)
        self.assertEqual(fake.spawned, [])         # nothing spawned
        self.assertEqual(fake.stopped, [])         # nothing reaped
        self.assertEqual(fake.sent, [])
        popen.assert_not_called()
        wla.assert_not_called()                    # nothing re-minted
        self.assertFalse(os.path.exists(os.path.join(d, "spawn.json")))
        self.assertIn("fake.spawn(command=%s" % shlex.quote(launch), out)
        self.assertIn("fake.submit(<handle>", out)
        self.assertIn("helm seat boot-brief", out)
        self.assertIn("REFUSE mutable-title-only", out)
        self.assertIn("helm chat wait --seat codex --follow", out)

    def test_print_reports_the_registered_handle_it_would_stop(self):
        d, _ = self._mint()
        self._record(d)
        fake = FakeAdapter(rows=[{"handle": "p9", "title": "dynamic"}])
        rc, out, err, wla, popen = self._spawn(
            ["codex", "--replace", "--print"], fake)
        self.assertEqual(rc, 0, err)
        self.assertIn("fake stop registered pane p9", out)
        self.assertEqual(fake.stopped, [])
        self.assertEqual(fake.spawned, [])
        wla.assert_not_called()
        popen.assert_not_called()

    def test_dry_run_headless_shows_detached_plan(self):
        d, launch = self._mint()
        rc, out, err, wla, popen = self._spawn(["codex", "--dry-run"], None)
        self.assertEqual(rc, 0, err)
        popen.assert_not_called()
        wla.assert_not_called()
        self.assertIn("headless", out)
        self.assertIn("detached setsid", out)
        self.assertIn(shlex.quote(launch), out)
        self.assertIn("role:  worker", out)
        self.assertIn("first-prompt", out)

    def test_dry_run_lead_shows_role_and_ultracode_invocation(self):
        _, launch = self._mint()
        rc, out, err, wla, popen = self._spawn(
            ["codex", "--role", "lead", "--dry-run"], None)
        self.assertEqual(rc, 0, err)
        popen.assert_not_called()
        wla.assert_not_called()
        self.assertIn("role:  lead", out)
        self.assertIn("HELM_SEAT_ROLE=lead", out)
        self.assertIn("--settings %s" % shlex.quote('{"ultracode":true}'), out)
        self.assertIn(shlex.quote(launch), out)


class HomeWorktreeSpawnTest(SpawnBase):
    """SLICE 0 (LAYER 1 of the coordination substrate) — a spawn with no
    --cwd lands the seat in its OWN worktree, never the shared main checkout.
    The seam is NOT stubbed here: a throwaway git repo is the ground truth."""
    PATCH_SEAT_HOME = False

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "proj")
        os.makedirs(self.repo)
        self._git("init", "-q", "-b", "main", ".")
        self._git("config", "user.email", "t@t")
        self._git("config", "user.name", "t")
        with open(os.path.join(self.repo, "a.txt"), "w") as f:
            f.write("hi\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "init")
        self.prev = os.getcwd()
        os.chdir(self.repo)
        self.wt = os.path.join(self.tmp, "proj-wt", "seats", "codex")

    def tearDown(self):
        os.chdir(self.prev)
        super().tearDown()

    def _git(self, *args, where=None):
        import subprocess
        return subprocess.run(["git", "-C", where or self.repo] + list(args),
                              capture_output=True, text=True)

    def _spawn(self, args, adapter, remint=False):
        """SpawnBase._spawn patches `seat.subprocess.Popen` — the attribute on
        the SHARED subprocess module — which silently neuters `subprocess.run`
        and therefore every `git` call this slice is built on (git answers a
        Mock returncode ⇒ 'not a repo'). The headless launch is stubbed at its
        OWN seam instead, so real git keeps working. remint=True lets the real
        _write_launch_assets run (trust seeding)."""
        out, err = io.StringIO(), io.StringIO()
        launched = mock.Mock(return_value=_FAKE_PID)
        with contextlib.ExitStack() as stack:
            wla = (mock.Mock() if remint
                   else stack.enter_context(
                       mock.patch.object(seat, "_write_launch_assets")))
            stack.enter_context(mock.patch.object(seat, "_headless_spawn",
                                                  launched))
            stack.enter_context(mock.patch.object(harness, "detect",
                                                 return_value=adapter))
            stack.enter_context(mock.patch.object(seat, "_pid_identity",
                                                 return_value="test-start"))
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            rc = seat.cmd_seat(["spawn"] + list(args))
        return rc, out.getvalue(), err.getvalue(), wla, launched

    @staticmethod
    def _launched_cwd(launched):
        return launched.call_args[0][2]   # _headless_spawn(sh, onboard, cwd, log)

    def test_default_spawn_provisions_the_seat_home_and_never_uses_main(self):
        self._mint()
        rc, out, err, _, launched = self._spawn(["codex"], None)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._launched_cwd(launched), self.wt)
        self.assertNotEqual(self._launched_cwd(launched), self.repo)
        self.assertTrue(os.path.isdir(self.wt))
        self.assertEqual(self._git("rev-parse", "--abbrev-ref", "HEAD",
                                   where=self.wt).stdout.strip(), "seat/codex")
        # a seat HOME is long-lived, so it is NOT worktree-locked (a lane lock
        # is a task lease; locking a home would make prune/gc refuse forever)
        self.assertNotIn("locked", self._git("worktree", "list").stdout)
        # and the register carries the path any agent resolves with `seat where`
        with open(os.path.join(seat._instance_dir("codex", "codex"),
                               "spawn.json")) as f:
            self.assertEqual(json.load(f)["worktree"], self.wt)

    def test_default_spawn_homes_to_the_same_project_room_no_scatter(self):
        """The worktree folds to the project root via --git-common-dir, so
        isolation must NOT scatter the seat out of its project room."""
        from helm import seats
        self._mint()
        rc, _, err, _, _ = self._spawn(["codex"], None)
        self.assertEqual(rc, 0, err)
        self.assertEqual(seats._git_project(self.wt),
                         seats._git_project(self.repo))

    def test_second_spawn_reuses_the_same_home_idempotently(self):
        self._mint()
        rc, _, err, _, _ = self._spawn(["codex"], None)
        self.assertEqual(rc, 0, err)
        with open(os.path.join(self.wt, "seat-scratch.txt"), "w") as f:
            f.write("work in progress\n")
        rc, out, err, _, launched = self._spawn(["codex", "--replace"], None)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._launched_cwd(launched), self.wt)
        self.assertTrue(os.path.exists(os.path.join(self.wt,
                                                    "seat-scratch.txt")))
        rows = [l for l in self._git("worktree", "list").stdout.splitlines()
                if os.path.join("seats", "codex") in l]
        self.assertEqual(len(rows), 1)      # one home, not two

    def test_explicit_cwd_still_wins(self):
        self._mint()
        explicit = os.path.join(self.tmp, "elsewhere")
        os.makedirs(explicit)
        rc, _, err, _, launched = self._spawn(["codex", "--cwd", explicit],
                                              None)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._launched_cwd(launched), explicit)
        self.assertFalse(os.path.isdir(self.wt))   # nothing provisioned

    def test_dry_run_shows_the_home_without_provisioning_it(self):
        self._mint()
        rc, out, err, _, launched = self._spawn(["codex", "--print"], None)
        self.assertEqual(rc, 0, err)
        launched.assert_not_called()
        self.assertIn(self.wt, out)
        self.assertIn("per-seat home worktree", out)
        self.assertFalse(os.path.isdir(self.wt))   # a plan has NO side effects

    def test_provisioning_failure_falls_open_to_the_shared_checkout(self):
        """A git/metaharness hiccup must degrade the spawn to the old shared-
        tree behaviour with a loud note — never abort the spawn."""
        self._mint()
        with mock.patch.object(harness, "ensure_home_worktree",
                               side_effect=harness.HarnessError("disk full")):
            rc, out, err, _, launched = self._spawn(["codex"], None)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._launched_cwd(launched), self.repo)
        self.assertIn("home worktree unavailable", err)
        self.assertIn("disk full", err)

    def test_adapter_without_the_optional_method_uses_the_native_floor(self):
        """ensure_home_worktree is OPTIONAL on an adapter — a metaharness that
        does not implement it still gets the isolated worktree."""
        self._mint()
        fake = FakeAdapter()
        self.assertFalse(hasattr(fake, "ensure_home_worktree"))
        rc, _, err, _, _ = self._spawn(["codex"], fake)
        self.assertEqual(rc, 0, err)
        self.assertEqual(fake.spawned[0][2], self.wt)
        self.assertTrue(os.path.isdir(self.wt))

    def test_outside_a_checkout_the_old_cwd_default_holds(self):
        plain = os.path.join(self.tmp, "no-repo")
        os.makedirs(plain)
        os.chdir(plain)
        self._mint()
        rc, _, err, _, launched = self._spawn(["codex"], None)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._launched_cwd(launched), plain)

    def test_trust_seed_follows_the_home_worktree(self):
        """_write_launch_assets(workdir=cwd) is what skips the folder-trust
        dialog; it must be seeded for the NEW worktree, not the old cwd."""
        d, _ = self._mint()
        rc, _, err, _, _ = self._spawn(["codex"], None, remint=True)
        self.assertEqual(rc, 0, err)
        with open(os.path.join(d, "claude", ".claude.json")) as f:
            trusted = json.load(f)["projects"]
        self.assertIn(os.path.realpath(self.wt), trusted)


class WhereTest(SpawnBase):
    def _where(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(["where"] + list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_where_resolves_headless_record_with_liveness(self):
        d, _ = self._mint()
        self._spawn(["codex", "--room", "team-z"], None)
        with mock.patch.object(seat, "_recorded_pid_alive", return_value=True):
            rc, out, err = self._where(["codex"])
        self.assertEqual(rc, 0, err)
        self.assertIn("codex: headless pid %d — LIVE" % _FAKE_PID, out)
        self.assertIn("role worker", out)
        self.assertIn("room team-z", out)
        self.assertIn(self.home, out)

    def test_where_json_is_machine_readable(self):
        self._mint()
        self._spawn(["codex"], None)
        with mock.patch.object(seat, "_recorded_pid_alive", return_value=False):
            rc, out, err = self._where(["codex", "--json"])
        self.assertEqual(rc, 0, err)
        got = json.loads(out)
        self.assertEqual(got["harness"], "headless")
        self.assertEqual(got["role"], "worker")
        self.assertEqual(got["pid"], _FAKE_PID)
        self.assertIs(got["alive"], False)

    def test_where_pane_record_checks_the_same_harness(self):
        d, _ = self._mint()
        fake = FakeAdapter()
        self._spawn(["codex"], fake)
        fake.rows = [{"handle": "pane-1", "title": "codex",
                      "status": "connected"}]
        with mock.patch.object(harness, "detect", return_value=fake):
            rc, out, err = self._where(["codex"])
        self.assertEqual(rc, 0, err)
        self.assertIn("fake handle pane-1 — LIVE", out)

    def test_where_reports_the_repaired_orca_handle(self):
        d, _ = self._mint()
        self._record(d, handle="old", harness_name="orca", session="s1")
        fake = FakeOrcaAdapter(rows=[{
            "handle": "new", "status": "connected", "writable": True,
            "pty_id": "pty-1", "worktree_id": "workspace:/w"}],
            resolved={"handle": "new", "pty_id": "pty-1"})
        identity = {"pid": 42, "pane_key": "tab:leaf",
                    "worktree_id": "workspace:/w"}
        with mock.patch.object(seat, "_live_session_orca_identity",
                               return_value=(identity, None)), \
                mock.patch.object(harness, "detect", return_value=fake):
            rc, out, err = self._where(["codex", "--json"])
        self.assertEqual(rc, 0, err)
        got = json.loads(out)
        self.assertEqual(got["handle"], "new")
        self.assertIs(got["alive"], True)

    def test_where_prints_the_quota_group_only_for_a_grouped_family(self):  # noqa: VACUOUS_ASSERTION — the loop is a fixed two-element literal and the grouped leg asserts a non-empty phrase IS present in the output, which is the positive control for the ungrouped leg's absence
        """THE OWNER-FACING HALF of the group split. One credential can meter
        two allowances — the antigravity account meters its Gemini models
        apart from its Claude and GPT ones — so a seat that printed its
        CREDENTIAL would have shown one wall over two independent groups. The
        line names the group, names who shares it, and says the remaining
        percent is UNMEASURED, because nothing reads the vendor's quota
        endpoint yet and a plausible number here is one an owner acts on.

        THE CONTROL IS THE SECOND FAMILY, and it checks the thing a
        substring assertion cannot: a family with no group must not merely
        omit the phrase, it must leave the line WELL-FORMED, with no empty
        field standing in for the absent group."""
        from helm import seat_catalog
        for family, grouped in (("opus46", True), ("codex", False)):  # noqa: SEAT_NAME — catalog FAMILY keys, and which family declares a group IS this arm's subject
            d, _ = self._mint(seat_name=family, family=family)
            self._record(d, seat=family, handle="pane-1")
            fake = FakeAdapter()
            fake.rows = [{"handle": "pane-1", "title": family,
                          "status": "connected"}]
            with mock.patch.object(harness, "detect", return_value=fake):
                rc, out, err = self._where([family])
            self.assertEqual(rc, 0, err)
            self.assertNotIn("; ;", out, "%s left an empty field" % family)
            phrase = seat_catalog.quota_group_phrase(family)
            if grouped:
                self.assertIn(seat_catalog.ANTIGRAVITY_CLAUDE_GPT_GROUP, phrase)
                self.assertIn(seat_catalog.QUOTA_GROUP_UNMEASURED, phrase)
                self.assertIn(phrase, out)
            else:
                self.assertEqual(phrase, "")
                self.assertNotIn("quota group", out)

    def test_where_without_record_points_at_spawn(self):
        self._mint()
        rc, out, err = self._where(["codex"])
        self.assertEqual(rc, 1)
        self.assertIn("no current or archived spawn record", err)
        self.assertIn("helm seat spawn codex", err)

    def test_where_unknown_seat_is_usage_error(self):
        rc, out, err = self._where(["mystery"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown seat", err)

    def test_where_rejects_unknown_option(self):
        rc, _, err = self._where(["codex", "--surprise"])
        self.assertEqual(rc, 2)
        self.assertIn("usage: helm seat where", err)


    def test_where_projects_the_latest_archived_terminal_event(self):
        d, _ = self._mint()
        self._record(d, handle="closed-pane", harness_name="orca",
                     session="session-a")
        rec = seat._spawn_record(d)
        closed, err = seat_exit_owner.archive_spawn(
            "codex", d, rec, "disposable pane exited")
        self.assertIsNone(err)
        self.assertEqual(closed["event"]["event"], "pane-closed")

        rc, text, stderr = self._where(["codex"])
        self.assertEqual(rc, 0, stderr)
        self.assertIn("TERMINAL pane-closed", text)
        self.assertIn("disposable pane exited", text)

        rc, out, stderr = self._where(["codex", "--json"])
        self.assertEqual(rc, 0, stderr)
        got = json.loads(out)
        self.assertIs(got["active"], False)
        self.assertIs(got["archived"], True)
        self.assertIs(got["alive"], False)
        self.assertIsNone(got["liveness"])
        self.assertEqual(got["terminal"]["event"], "pane-closed")


class RebindTest(SpawnBase):
    """`helm seat rebind` — the REBOOT verb.

    helm keys a seat's register on its orca HANDLE, which is per-pane and dies
    with the machine. Measured 2026-07-28, the morning after an Ubuntu reboot:
    `helm seat where` reported 6 of 7 seats GONE while ds4pro and gemini were
    posting in chat at that moment. The seats never died; the REGISTER did."""

    def _cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(["rebind"] + list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_the_timer_reruns_rather_than_firing_once_at_boot(self):
        """THE WHOLE POINT OF THE CADENCE. A register goes stale at boot, but
        at boot the orca panes do not exist yet — a boot-ONLY pass would find
        nothing to bind and report success over an empty fleet, which is the
        confident-answer-over-skipped-work failure exactly. OnBootSec starts
        the clock; OnUnitActiveSec is what makes it heal when the panes
        actually come back (and again when one is replaced mid-day)."""
        _, service, _, timer = seat.rebind_timer_units()
        # The unit's payload is the SWEEP (2026-08-22): it performs this same
        # rebind pass for every LIVE seat and relaunches the seats rebind can
        # only refuse — tests/test_seat_resume_all.py pins the ExecStart.
        self.assertIn("seat resume --all --apply", service)
        self.assertIn("OnBootSec=", timer)
        self.assertIn("OnUnitActiveSec=%ds" % seat.REBIND_INTERVAL_S, timer)

    def test_the_timer_never_captures_a_disposable_worktree(self):
        """A persistent unit outlives the checkout that installed it. This
        verb is routinely run FROM a worktree, so a PATH-resolved helm would
        point the fleet's repair at a directory `helm work gc` later reaps."""
        with mock.patch.object(seat.shutil, "which",
                               return_value="/tmp/helm-wt/gone/bin/helm"):
            _, service, _, _ = seat.rebind_timer_units()
        self.assertIn(os.path.expanduser("~/.local/bin/helm"), service)
        self.assertNotIn("helm-wt", service)

    def test_a_nonpositive_interval_installs_nothing(self):
        ok, detail = seat.ensure_rebind_timer(0)
        self.assertFalse(ok)
        self.assertIn("at least 1 second", detail)

    def test_the_documented_single_seat_form_reaches_the_verb(self):
        """REGRESSION, and a lesson about where the tests were pointed. The
        dispatcher handed the POSITIONAL seat name to a flags-only guard, so
        `helm seat rebind gemini` answered "unknown arg 'gemini'" while
        printing a usage line that shows exactly that call. Only `--all` ever
        worked — which is precisely the form the fleet-wide reboot repair used,
        so the break shipped green.

        Every rebind test above calls seat._rebind() directly, i.e. it runs
        UNDER the dispatcher rather than THROUGH it, and none of them could see
        this. Assert on the CLI surface the operator actually types."""
        rc, out, err = self._cli(["gemini"])
        self.assertNotIn("unknown arg", err,
                         "the seat name is a positional, not an unknown flag")
        self.assertNotIn("usage: seat rebind", err)

    def test_all_takes_no_seat_name(self):
        """The guard that must survive the fix: --all and a name together are
        contradictory, and that refusal comes from _rebind itself."""
        rc, out, err = self._cli(["--all", "gemini"])
        self.assertEqual(rc, 2)
        self.assertIn("--all takes no seat name", err)

    def test_an_actually_unknown_flag_is_still_refused(self):
        """The negative control. Loosening the guard to let the positional
        through must not let a typo'd flag through with it — without this,
        deleting the guard entirely passes the test above."""
        rc, out, err = self._cli(["gemini", "--aply"])
        self.assertEqual(rc, 2)
        self.assertIn("--aply", err)

    def test_rebind_is_refused_when_two_panes_claim_one_seat(self):
        """Ambiguity must FAIL CLOSED. Two live processes claiming one seat name
        is a real condition (double-open, half-finished relaunch) and binding
        either would make the register a coin flip."""
        d, _ = self._mint()
        # TWO DISTINCT PANES, really present. The first version of this test
        # mocked glob to return NOTHING — so it exercised ZERO panes while its
        # name claimed two, and a mutation weakening the guard to `< 1` passed
        # it unchanged. A test whose fixture cannot produce the condition it
        # names proves only that the empty case refuses.
        envs = {
            "/proc/101/environ": b"HELM_CHAT_NAME=codex\0ORCA_PANE_KEY=pane-A\0"
                                 b"ORCA_WORKTREE_ID=w1\0",
            "/proc/202/environ": b"HELM_CHAT_NAME=codex\0ORCA_PANE_KEY=pane-B\0"
                                 b"ORCA_WORKTREE_ID=w2\0",
        }

        def fake_open(path, *a, **kw):
            return io.BytesIO(envs[path])

        with mock.patch.object(seat.glob, "glob", return_value=list(envs)), \
                mock.patch("builtins.open", fake_open):
            ident, err = seat._live_seat_orca_identity(d, "codex")
        self.assertIsNone(ident, "two distinct panes must never resolve")
        self.assertIn("2 distinct live panes", err)
        self.assertIn("refusing to guess", err)

    def test_a_pane_orca_no_longer_resolves_is_a_stale_record(self):
        """task/2798 — THE CLAIMANT CENSUS COUNTS LIVE PROCESSES, NOT RECORDS.

        Measured on the grok seat: `helm seat rebind grok --apply` refused "2
        distinct live panes claim seat 'grok'" while orca listed exactly ONE
        grok terminal. The second claimant was the CLOSED pane's surviving shells —
        the `bash -c` snapshot loader and its `helm chat wait --seat grok
        --follow` beacon — which keep the dead pane's `ORCA_PANE_KEY` and
        `HELM_CHAT_NAME` in their environ for as long as they run. Live pids,
        dead pane. The census had one true conjunct (a process claims the seat)
        and was missing the other (its pane still exists), so a ghost outvoted
        the seat.

        THREE CONTROLS, all in this arm, because each defeats a different wrong
        cure: (a) TWO GENUINELY LIVE claimants — both pane keys resolve — still
        refuse, so the fix is not "always pick one"; (b) a claimant orca could
        not be ASKED about is UNKNOWN and refuses with a sentence that is NOT
        the death constant, so the fix is not "unresolvable means gone" —
        `rebind_refusal_means_dead` licenses a relaunch off that constant; (c)
        the ghost's source is NAMED with its pids, so an operator can act on it
        rather than hunt. Blast radius: `_live_seat_orca_identity` and
        `_pane_claimant_states`.

        THE GHOST IS ORCA'S POSITIVE ANSWER, `terminal_not_found`, and nothing
        weaker: a reply that merely names no handle is a different answer with
        its own arm, `test_each_orca_answer_maps_to_one_claimant_state`.
        """
        d, _ = self._mint()
        live_env = (b"HELM_CHAT_NAME=codex\0ORCA_PANE_KEY=pane-live\0"
                    b"ORCA_WORKTREE_ID=w1\0"
                    b"CLAUDE_CODE_SESSION_ID=session-current\0")
        # the closed pane's two orphaned shells, exactly as measured: same seat
        # name, the DEAD pane key, and no session of their own
        ghost = (b"HELM_CHAT_NAME=codex\0ORCA_PANE_KEY=pane-ghost\0"
                 b"ORCA_WORKTREE_ID=w1\0")
        envs = {"/proc/101/environ": live_env, "/proc/202/environ": ghost,
                "/proc/303/environ": ghost}

        def fake_open(path, *a, **kw):
            return io.BytesIO(envs[path])

        exact = {"pid": 101, "proc_start": "1", "pane_key": "pane-live",
                 "worktree_id": "w1"}

        def census(table):
            err_out = io.StringIO()
            with mock.patch.object(seat.glob, "glob", return_value=list(envs)), \
                    mock.patch("builtins.open", fake_open), \
                    mock.patch.object(seat, "_live_session_orca_identity",
                                      return_value=(exact, None)), \
                    contextlib.redirect_stderr(err_out):
                got = seat._live_seat_orca_identity(
                    d, "codex", adapter=KeyedOrcaAdapter(table))
            return got + (err_out.getvalue(),)

        ident, err, said = census({"pane-live": {"handle": "term-live"},
                                   "pane-ghost": _orca_not_found()})
        self.assertIsNone(err, err)
        self.assertEqual(ident["pane_key"], "pane-live")
        self.assertEqual(ident["session"], "session-current")
        for where in (ident["stale_claimants"], said):
            self.assertIn("STALE-RECORD", where)
            self.assertIn("pane-ghost", where)
            self.assertIn("202", where)          # the ghost's own pids
            self.assertIn("303", where)
        # and the ghost never reaches the register's fields
        self.assertNotIn("pane-ghost", json.dumps(
            {k: v for k, v in ident.items() if k != "stale_claimants"}))

        # CONTROL (a): both claimants really live — the ambiguity stands.
        ident, err, _said = census({"pane-live": {"handle": "term-live"},
                                    "pane-ghost": {"handle": "term-other"}})
        self.assertIsNone(ident)
        self.assertIn("2 distinct live panes", err)

        # CONTROL (b): orca unreachable — UNKNOWN, and never the death sentence
        # that licenses a relaunch.
        ident, err, _said = census(
            {"pane-live": {"handle": "term-live"},
             "pane-ghost": RuntimeError("orca rpc down")})
        self.assertIsNone(ident)
        self.assertIn("UNKNOWN", err)
        self.assertIn("orca rpc down", err)
        self.assertNotEqual(err, seat.NO_DISTINCT_LIVE_PANE % (0, "codex"))
        self.assertNotIn("distinct live panes", err)

    def _claimants(self):
        """The patches for ONE live claimant on `pane-live` and one contested
        claimant on `pane-ghost`; what orca says about each is the adapter's."""
        live_env = (b"HELM_CHAT_NAME=codex\0ORCA_PANE_KEY=pane-live\0"
                    b"ORCA_WORKTREE_ID=w1\0"
                    b"CLAUDE_CODE_SESSION_ID=session-current\0")
        ghost = (b"HELM_CHAT_NAME=codex\0ORCA_PANE_KEY=pane-ghost\0"
                 b"ORCA_WORKTREE_ID=w1\0")
        envs = {"/proc/101/environ": live_env, "/proc/202/environ": ghost}
        real_open = open

        def fake_open(path, *a, **kw):
            return io.BytesIO(envs[path]) if path in envs \
                else real_open(path, *a, **kw)

        exact = {"pid": 101, "proc_start": "1", "pane_key": "pane-live",
                 "worktree_id": "w1"}

        def proof(_d, sid, *args):
            return (exact, None) if sid == "session-current" else (
                None, "recorded session is gone")

        return (mock.patch.object(seat.glob, "glob", return_value=list(envs)),
                mock.patch("builtins.open", fake_open),
                mock.patch.object(seat, "_live_session_orca_identity",
                                  side_effect=proof))

    def test_each_orca_answer_maps_to_one_claimant_state(self):  # noqa: VACUOUS_ASSERTION — each absence asserted inside the loop has its unconditional positive outside it on the same census: STALE-RECORD is asserted PRESENT for the not-found answer before the loop, and the distinct-live-panes sentence is asserted PRESENT by the live control after it
        """task/2798 — THE CENSUS READS ORCA'S ANSWER THE WAY THE ADAPTER
        DEFINES IT, through the REAL `OrcaAdapter.resolve_pane`.

        The contract, read from helm/harness.py:
          · `OrcaAdapter._runtime_call` raises every `ok: false` reply as
            HarnessError("orca runtime rpc <method>: <orca's message>") — the
            ONLY channel a host-stated absence can arrive on, and
            `terminal_not_found` is the message that states it
            (`seat_resume_all.PANE_NOT_FOUND`, the PANE-GONE precedent).
          · `OrcaAdapter.resolve_pane` builds its answer from
            `.get("terminal") or {}`, so a successful reply naming no terminal
            comes back as a dict whose handle is None: orca declined to say.
          · every transport failure and deadline is a HarnessError too, with a
            different text.

        The mapping had the first two REVERSED: the positive not-found was
        caught as UNKNOWN, and the no-handle reply was called a stale record and
        EXCLUDED — so with another live claimant, rebind proceeded past a pane
        nobody had proven gone.

        CONTROL, in this arm: a handle that resolves still reads LIVE, and two
        live claimants still refuse — so neither cure is "treat everything as
        gone" or "treat everything as unknown". Blast radius:
        `_pane_claimant_states`.
        """
        d, _ = self._mint()
        ad = harness.OrcaAdapter("/bin/orca")
        live = {"terminal": {"handle": "term-live", "ptyId": "pty-live"}}

        def census(contested):
            replies = {"pane-live": live, "pane-ghost": contested}

            def runtime_call(method, params, **_kw):
                self.assertEqual(method, "terminal.resolvePane")
                got = replies[params["paneKey"]]
                if isinstance(got, Exception):
                    raise got
                return got

            globbed, opened, proven = self._claimants()
            said = io.StringIO()
            with globbed, opened, proven, \
                    mock.patch.object(ad, "_runtime_call",
                                      side_effect=runtime_call), \
                    contextlib.redirect_stderr(said):
                return seat._live_seat_orca_identity(
                    d, "codex", adapter=ad) + (said.getvalue(),)

        # orca ANSWERED: no such pane -> STALE-RECORD, census proceeds
        ident, err, said = census(_orca_not_found())
        self.assertIsNone(err, err)
        self.assertEqual(ident["pane_key"], "pane-live")
        self.assertIn("STALE-RECORD", said)
        self.assertIn("terminal_not_found", ident["stale_claimants"])

        # orca did NOT answer -> UNKNOWN, naming the lookup, never stale
        unanswered = (
            ("a reply naming no terminal", {}, "without a handle"),
            ("a reply whose terminal has no handle", {"terminal": {}},
             "without a handle"),
            ("a deadline", harness.HarnessError(
                "orca runtime rpc terminal.resolvePane: deadline exceeded "
                "after 5.0s at recv (daemon held the connection open)"),
             "deadline exceeded"),
            ("a different error reply", harness.HarnessError(
                "orca runtime rpc terminal.resolvePane: runtime_unavailable"),
             "runtime_unavailable"),
        )
        for label, contested, needle in unanswered:
            with self.subTest(answer=label):
                ident, err, said = census(contested)
                self.assertIsNone(ident, "an unanswered lookup must refuse")
                self.assertIn("UNKNOWN", err)
                self.assertIn("pane-ghost", err)      # the lookup that failed
                self.assertIn(needle, err)
                self.assertNotIn("STALE-RECORD", said)
                self.assertNotIn("distinct live panes", err)

        # CONTROL: a handle that resolves is LIVE, so two of them still refuse.
        ident, err, _said = census(
            {"terminal": {"handle": "term-other", "ptyId": "pty-other"}})
        self.assertIsNone(ident)
        self.assertIn("2 distinct live panes", err)

    def test_rebind_refuses_an_unanswered_claimant_and_names_the_lookup(self):
        """task/2798 — REBIND DOES NOT PROCEED PAST A PANE NOBODY PROVED GONE.

        The census feeds `rebind_seat`, which WRITES the register. While a
        no-handle reply read as a stale record, a second claimant orca had said
        nothing about was excluded and the register was re-pointed at the other
        one — an ambiguity resolved by a lookup that never answered.

        The refusal names the pane key whose lookup failed, and spawn.json is
        byte-identical across it.

        CONTROL, in this arm: the same fixture with orca's POSITIVE
        `terminal_not_found` for the contested pane rebinds, and the register
        names the live handle — so the refusal above is the unanswered lookup
        and not a rebind that refuses everything. Blast radius:
        `_pane_claimant_states`, `_prove_orca_rebind`, `rebind_seat`.
        """
        d, _ = self._mint()
        spawn_json = os.path.join(d, "spawn.json")
        row = {"handle": "term-live", "status": "connected", "writable": True,
               "pty_id": "pty-live", "worktree_id": "w1"}
        os.environ["HELM_CHAT_NAME"] = "operator-seat"

        def rebind(contested):
            self._record(d, handle="old", harness_name="orca",
                         session="recorded-dead")
            with open(spawn_json, "rb") as f:
                before = f.read()
            fake = KeyedOrcaAdapter(
                {"pane-live": {"handle": "term-live", "pty_id": "pty-live"},
                 "pane-ghost": contested}, rows=[row])
            globbed, opened, proven = self._claimants()
            with globbed, opened, proven, \
                    contextlib.redirect_stderr(io.StringIO()):
                fields, err = seat.rebind_seat("codex", d, fake, apply=True)
            with open(spawn_json, "rb") as f:
                return fields, err, before, f.read()

        fields, err, before, after = rebind({})
        self.assertIsNone(fields)
        self.assertIn("UNKNOWN", err)
        self.assertIn("pane-ghost", err)
        self.assertIn("without a handle", err)
        self.assertEqual(after, before)

        fields, err, before, after = rebind(_orca_not_found())
        self.assertIsNone(err, err)
        self.assertEqual(fields["handle"], "term-live")
        self.assertEqual(fields["session"], "session-current")
        self.assertNotEqual(after, before)
        self.assertEqual(json.loads(after)["handle"], "term-live")

    def test_one_seats_many_processes_are_one_pane_not_an_ambiguity(self):
        """A seat legitimately owns several processes — the pane, its beacon,
        its hooks — all carrying the SAME pane identity. Collapsing on pid
        count would refuse every healthy seat; collapse on the identity."""
        d, _ = self._mint()
        same = (b"HELM_CHAT_NAME=codex\0ORCA_PANE_KEY=pane-A\0"
                b"ORCA_WORKTREE_ID=w1\0"
                b"CLAUDE_CODE_SESSION_ID=session-current\0")
        envs = {"/proc/101/environ": same, "/proc/202/environ": same,
                "/proc/303/environ": same}

        def fake_open(path, *a, **kw):
            return io.BytesIO(envs[path])

        exact = {"pid": 101, "proc_start": "1", "pane_key": "pane-A",
                 "worktree_id": "w1"}
        with mock.patch.object(seat.glob, "glob", return_value=list(envs)), \
                mock.patch("builtins.open", fake_open), \
                mock.patch.object(seat, "_live_session_orca_identity",
                                  return_value=(exact, None)):
            ident, err = seat._live_seat_orca_identity(d, "codex")
        self.assertIsNone(err, err)
        self.assertEqual(ident["pane_key"], "pane-A")
        self.assertEqual(ident["session"], "session-current")

    def test_one_pane_with_conflicting_sessions_refuses_rebind(self):
        d, _ = self._mint()
        base = b"HELM_CHAT_NAME=codex\0ORCA_PANE_KEY=pane-A\0ORCA_WORKTREE_ID=w1\0"
        envs = {"/proc/101/environ": base + b"CLAUDE_CODE_SESSION_ID=session-a\0",
                "/proc/202/environ": base + b"CLAUDE_CODE_SESSION_ID=session-b\0"}

        def fake_open(path, *a, **kw):
            return io.BytesIO(envs[path])

        with mock.patch.object(seat.glob, "glob", return_value=list(envs)), \
                mock.patch("builtins.open", fake_open):
            ident, err = seat._live_seat_orca_identity(d, "codex")
        self.assertIsNone(ident)
        self.assertIn("conflicting sessions", err)
        self.assertIn("refusing to guess", err)

    def test_rebind_refuses_a_new_process_without_a_session_to_bind(self):
        d, _ = self._mint()
        self._record(d, handle="old", harness_name="orca", session="stale")
        row = {"handle": "new", "status": "connected", "writable": True,
               "pty_id": "pty-new", "worktree_id": "workspace:/w"}
        fake = FakeOrcaAdapter(
            rows=[row], resolved={"handle": "new", "pty_id": "pty-new"})
        current = {"pid": 42, "pane_key": "pane-new",
                   "worktree_id": "workspace:/w", "session": None}
        with mock.patch.object(seat, "_live_session_orca_identity",
                               return_value=(None, "stale session is gone")), \
                mock.patch.object(seat, "_live_seat_orca_identity",
                                  return_value=(current, None)):
            fields, err = seat.rebind_seat("codex", d, fake, apply=True)
        self.assertIsNone(fields)
        self.assertIn("current live process carries no session id", err)
        with open(os.path.join(d, "spawn.json")) as f:
            self.assertEqual(json.load(f)["session"], "stale")

    def test_rebind_never_promotes_a_helper_inherited_stale_session(self):
        d, _ = self._mint()
        row = {"handle": "new", "status": "connected", "writable": True,
               "pty_id": "pty-new", "worktree_id": "workspace:/w"}
        fake = FakeOrcaAdapter(
            rows=[row], resolved={"handle": "new", "pty_id": "pty-new"})
        rec = {"seat": "codex", "harness": "orca", "handle": "old",
               "session": "recorded-dead"}
        proc = "/proc/4242/environ"
        env = (b"HELM_CHAT_NAME=codex\0ORCA_PANE_KEY=pane-new\0"
               b"ORCA_WORKTREE_ID=workspace:/w\0"
               b"CLAUDE_CODE_SESSION_ID=helper-stale\0")

        def paths(pattern):
            return [proc] if pattern == "/proc/[0-9]*/environ" else []

        with mock.patch.object(seat.glob, "glob", side_effect=paths), \
                mock.patch("builtins.open", return_value=io.BytesIO(env)):
            _pane, fields, err = seat._prove_orca_rebind(d, rec, fake, [row])
        self.assertIsNone(fields, "helper-only session testimony must not bind")
        self.assertIn("exact live Claude", err)
        self.assertIn("helper-stale", err)

    def test_rebind_refuses_a_candidate_session_with_duplicate_live_agents(self):
        d, _ = self._mint()
        row = {"handle": "new", "status": "connected", "writable": True,
               "pty_id": "pty-new", "worktree_id": "workspace:/w"}
        fake = FakeOrcaAdapter(
            rows=[row], resolved={"handle": "new", "pty_id": "pty-new"})
        rec = {"seat": "codex", "harness": "orca", "handle": "old",
               "session": "recorded-dead"}
        proc = "/proc/4242/environ"
        env = (b"HELM_CHAT_NAME=codex\0ORCA_PANE_KEY=pane-new\0"
               b"ORCA_WORKTREE_ID=workspace:/w\0"
               b"CLAUDE_CODE_SESSION_ID=session-current\0")

        def proof(_d, sid, *args):
            if sid == "recorded-dead":
                return None, "recorded session is gone"
            return None, ("session session-current has 2 exact live Claude "
                          "processes; pane replacement requires exactly one")

        with mock.patch.object(seat.glob, "glob", return_value=[proc]), \
                mock.patch("builtins.open", return_value=io.BytesIO(env)), \
                mock.patch.object(seat, "_live_session_orca_identity",
                                  side_effect=proof):
            _pane, fields, err = seat._prove_orca_rebind(d, rec, fake, [row])
        self.assertIsNone(fields)
        self.assertIn("2 exact live Claude processes", err)

    def test_applied_rebind_restamps_the_live_process_session(self):
        d, _ = self._mint()
        self._record(d, handle="new", harness_name="orca", session="stale")
        row = {"handle": "new", "status": "connected", "writable": True,
               "pty_id": "pty-new", "worktree_id": "workspace:/w"}
        fake = FakeOrcaAdapter(
            rows=[row], resolved={"handle": "new", "pty_id": "pty-new"})
        current = {"pid": 42, "pane_key": "pane-new",
                   "worktree_id": "workspace:/w", "session": "session-current"}
        os.environ["HELM_CHAT_NAME"] = "operator-seat"
        with mock.patch.object(seat, "_live_session_orca_identity",
                               return_value=(None, "stale session is gone")), \
                mock.patch.object(seat, "_live_seat_orca_identity",
                                  return_value=(current, None)):
            fields, err = seat.rebind_seat("codex", d, fake, apply=True)
            self.assertIsNone(err, err)
            self.assertEqual(fields["session"], "session-current")
            from helm import pk, seats
            rows = seats.roster()
            rows["codex"].pop("runtime_sessions")
            pk.write_json(seats.roster_path(), rows)
            retried, err = seat.rebind_seat("codex", d, fake, apply=True)
        self.assertIsNone(err, err)
        self.assertTrue(retried["unchanged"])
        with open(os.path.join(d, "spawn.json")) as f:
            self.assertEqual(json.load(f)["session"], "session-current")
        runtime, verified = seats.runtime_for_session(
            seats.roster()["codex"], "session-current")
        self.assertTrue(verified)
        self.assertEqual(runtime.get("family"), "codex")

    def test_rebind_matches_on_the_seat_name_not_a_title_or_cwd(self):
        """Identity is process-proven. A title or cwd is copyable presentation
        and authorizes nothing — the same standard the pane resolver holds."""
        import inspect
        src = inspect.getsource(seat._live_seat_orca_identity)
        self.assertIn("HELM_CHAT_NAME=", src)
        for forgeable in ("title", "preview", "cwd"):
            self.assertNotIn('row.get("%s")' % forgeable, src)

    def test_rebind_dry_runs_by_default(self):
        """A register rewrite leaves NO trace to inspect afterwards, so it must
        prove-and-report before it writes. Asserted on the source contract:
        apply=False never reaches the write."""
        import inspect
        src = inspect.getsource(seat.rebind_seat)
        self.assertIn("if not apply:", src)
        self.assertLess(src.index("if not apply:"), src.index("pk.write_json"))

    def test_rebind_is_not_repair_and_drops_only_the_stale_guards(self):
        """_prove_orca_replacement guards pane_key/worktree_id drift — correct
        for a handle change WITHIN a generation, and exactly why it refuses a
        reboot with 'spawn pane key conflicts with the live session'. Rebind
        drops those two cached-copy comparisons and NOTHING else: the session
        anchor, the resolve_pane chain and the single-writable-row match all
        remain."""
        import ast, inspect

        def body_src(fn):
            """Source with the DOCSTRING STRIPPED. The first version of this
            test compared raw source and failed on rebind's own docstring, which
            QUOTES the repair error to explain the difference — testing prose,
            not behavior."""
            tree = ast.parse(inspect.getsource(fn).lstrip())
            node = tree.body[0]
            stmts = node.body[1:] if (isinstance(node.body[0], ast.Expr) and
                                      isinstance(node.body[0].value, ast.Constant)
                                      ) else node.body
            return "\n".join(ast.unparse(x) for x in stmts)

        rebind = body_src(seat._prove_orca_rebind)
        repair = body_src(seat._prove_orca_replacement)
        # the repair GUARDS the cached keys; that is its job and it stays
        self.assertIn("conflicts with the live session", repair)
        # the rebind must not — a reboot changes both keys legitimately
        self.assertNotIn("conflicts with the live session", rebind)
        # ...and drops NOTHING else: every real check survives
        for kept in ("resolve_pane", "writable", "_pane_live"):
            self.assertIn(kept, rebind, kept)


class GuardsAndHelpTest(SpawnBase):
    def test_spawn_unminted_seat_refuses(self):
        rc, out, err, wla, popen = self._spawn(["codex"], FakeAdapter())
        self.assertEqual(rc, 1)
        self.assertIn("no codex seat minted", err)
        wla.assert_not_called()
        popen.assert_not_called()

    def test_spawn_unknown_seat_is_usage_error(self):
        rc, out, err, _, _ = self._spawn(["mystery"], FakeAdapter())
        self.assertEqual(rc, 2)
        self.assertIn("unknown seat", err)

    def test_spawn_rejects_missing_option_value_without_traceback(self):
        self._mint()
        rc, _, err, wla, popen = self._spawn(["codex", "--room"],
                                             FakeAdapter())
        self.assertEqual(rc, 2)
        self.assertIn("--room wants a value", err)
        wla.assert_not_called()
        popen.assert_not_called()

    def test_spawn_rejects_unknown_option(self):
        self._mint()
        rc, _, err, wla, _ = self._spawn(["codex", "--surprise"],
                                         FakeAdapter())
        self.assertEqual(rc, 2)
        self.assertIn("unknown option --surprise", err)
        wla.assert_not_called()

    def test_spawn_instance_seat_uses_instance_dir(self):
        d, launch = self._mint(seat_name="codex-2")
        self.assertIn(os.path.join("instances", "codex-2"), launch)
        fake = FakeAdapter()
        rc, _, err, _, _ = self._spawn(["codex-2"], fake)
        self.assertEqual(rc, 0, err)
        command, title, _ = fake.spawned[0]
        self.assertEqual(command, self._launch_line(d, launch))
        self.assertEqual(title, "codex-2")
        _, text, _ = fake.sent[0]
        self.assertEqual(text, "Run `helm seat boot-brief` and follow it.")
        with open(os.path.join(d, "spawn.json")) as f:
            self.assertEqual(json.load(f)["role"], "worker")

    def test_spawn_rejects_unknown_role_before_mint_or_launch(self):
        self._mint()
        rc, _, err, wla, popen = self._spawn(
            ["codex", "--role", "captain"], FakeAdapter())
        self.assertEqual(rc, 2)
        self.assertIn("--role wants one of: worker, lead", err)
        wla.assert_not_called()
        popen.assert_not_called()

    def test_onboarding_prompt_contents(self):
        p = seat.onboarding_prompt("codex")
        self.assertIn("helm chat wait --seat codex --follow", p)   # beacon
        self.assertIn("helm chat read", p)                         # catch up
        self.assertIn("helm chat post", p)                         # announce
        self.assertIn("@codex", p)                                 # take work
        self.assertIn("role worker", p)
        self.assertIn("stay parked on the beacon", p)
        self.assertNotIn("explicit FLEET LEAD", p)
        self.assertNotIn("positive live/recent delegation evidence", p)
        self.assertNotIn("\n", p)          # single keystroke burst, one line
        q = seat.onboarding_prompt("codex", room="team-x")
        self.assertIn("--room team-x", q)

    # The instruction class this brief must not contain: a verb that MOVES or
    # CLOSES somebody else's row. Matched as whole words, so the row STATES a
    # fresh seat must still recognise — "cancelled", "superseded",
    # "verdicted" — survive; it is the imperative that is forbidden.
    _MOVE_VERBS = re.compile(
        r"\b(?:rebind|rebinds|reassign|reassigns|cancel|cancels)\b",
        re.IGNORECASE)
    # A worker seat has no lane-claiming mandate either, so for that role the
    # net is wider. A LEAD is told to claim a bounded lane on purpose and is
    # deliberately outside this one.
    _TAKE_VERBS = re.compile(
        r"\b(?:rebind|rebinds|reassign|reassigns|cancel|cancels|"
        r"claim|claims|claiming)\b", re.IGNORECASE)

    def test_the_onboarding_tells_a_fresh_seat_to_wait_not_to_take(self):
        """A fresh seat's whole first turn is arm, announce, read ITS OWN
        rows, wait. The brief may not hand it a verb that takes a row naming
        somebody else: the seat named on an open row may be reading it at that
        moment, and a row moved out from under a reader costs that read.

        Asserted on the RENDERED brief, never on a source string, because the
        seat is instructed by what the verb prints."""
        worker = seat.onboarding_prompt("codex")
        lead = seat.onboarding_prompt("codex", role="lead")
        # MUST-HIT FIRST: an absence proves nothing until the detector is
        # shown finding the class it is looking for.
        taking = ("helm dispatch rebind <id> --to @codex --force "
                  "--reason 'taking ownership'")
        self.assertTrue(self._MOVE_VERBS.search(taking),
                        "the detector cannot see a take instruction, so its "
                        "silence on the brief below means nothing")
        self.assertTrue(self._TAKE_VERBS.search("claim the first open row"))
        for role, text, pattern in (("worker", worker, self._TAKE_VERBS),
                                    ("lead", lead, self._MOVE_VERBS)):
            with self.subTest(role=role):
                hit = pattern.search(text)
                self.assertIsNone(
                    hit, "the %s brief tells a fresh seat to %r" % (
                        role, hit.group(0) if hit else ""))
        # It says the positive thing instead, and the row STATES survive.
        self.assertIn("WAIT FOR ROWS", worker)
        self.assertIn("idle BY DESIGN", worker)
        self.assertIn("belongs to that seat", worker)
        self.assertIn("cancelled", worker)
        # CONTROL: the brief still does its original job. A brief that had
        # been emptied would pass every assertion above.
        self.assertIn("ARM YOUR INBOX BEACON", worker)
        self.assertIn("helm chat wait --seat codex --follow", worker)
        self.assertNotIn("\n", worker)

    def test_boot_brief_expands_from_the_seats_own_environment(self):
        out = io.StringIO()
        env = {"HELM_CHAT_NAME": "seat-b", "HELM_CHAT_ROOM": "team-x",
               "HELM_SEAT_ROLE": "lead"}
        with mock.patch.dict(os.environ, env, clear=False), \
                contextlib.redirect_stdout(out):
            rc = seat.cmd_seat(["boot-brief"])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("helm chat wait --seat seat-b --follow", text)
        self.assertIn("--room team-x", text)
        self.assertIn("explicit FLEET LEAD", text)
        self.assertIn("pre-authorized to use Agent subagents", text)
        self.assertIn("never ask the owner for permission to delegate", text)

    def test_onboarding_takes_work_from_the_ledger_fold_not_history(self):
        """THE RECOVERED-SEAT TRAP, three live instances inside 90 minutes on
        2026-08-01: a fresh session reads history, and stale rows read as
        invitations. codex-3 rebuilt an already-superseded lane; codex claimed
        a CANCELLED row, then assembled a LAND READY from a cancelled vehicle
        + an ungated approve + a tip that no longer resolves. The onboarding
        prompt was the vector — it said "rows addressed @you are yours" with
        nothing distinguishing live rows from dead ones.

        The prompt must now direct work-taking through the LEDGER FOLD
        (`helm dispatch list --open`) and state the status law: cancelled /
        superseded / verdicted rows are DEAD however open the chat reads."""
        p = seat.onboarding_prompt("codex")
        # The fold is the work list, and the IDENTITY-SCOPED spelling is the
        # one printed: the unfiltered listing hands a fresh seat every open
        # row in the project and leaves the "is this mine" question to its
        # judgement, which is the question a fresh seat is worst at.
        self.assertIn("helm dispatch list --mine --open", p)
        # ...the status decides, and each dead state is named...
        self.assertIn("CURRENT STATUS decides", p)
        for word in ("cancelled", "superseded", "verdicted"):
            self.assertIn(word, p)
        # ...and the room is explicitly demoted to context.
        self.assertIn("never your work list", p)
        # The fold's own failure mode, codex's adversarial finding: "ONLY
        # open rows" with no escape hatch inverts a blind read into no-work.
        # An unreadable fold must be UNKNOWN, never an empty obligation set.
        self.assertIn("UNKNOWN, never empty", p)
        self.assertIn("do not infer no-work", p)
        # Still one pane-safe line: the delivery constraint outranks prose.
        self.assertNotIn("\n", p)

    def test_seat_usage_lists_spawn_and_where(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seat.cmd_seat([])
        self.assertEqual(rc, 2)
        self.assertIn("spawn <seat>", err.getvalue())
        self.assertIn("where <seat>", err.getvalue())
        self.assertIn("lifecycle show", err.getvalue())
        self.assertIn("record <seat> renamed", err.getvalue())

    def test_helm_help_lists_spawn_and_where(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["--help"])
        self.assertEqual(rc, 0)
        seat_line = next(l for l in out.getvalue().splitlines()
                         if l.strip().startswith("seat "))
        self.assertIn("spawn", seat_line)
        self.assertIn("where", seat_line)


class ProjectRegistryBase(SpawnBase):
    """The temp project registry every project-canonical arm stands on."""

    def _register(self, *names, **kw):
        """A temp project registry holding exactly these projects, each rooted at
        a REAL directory, and the seat's default home moved INSIDE the first one.

        The registry is now authority for the workspace as well as the name, so a
        fixture whose registered project roots do not contain the seat's home
        describes a world the shipped verb correctly refuses. `paths` pins a real
        repository for an arm about provisioning; `cwds` rides through so the
        linked-worktree/cv-scope tier can be exercised.
        """
        from helm import home as home_mod
        path = home_mod.registry_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        projects = {}
        for name in names:
            root = kw.get("paths", {}).get(name) or os.path.join(self.tmp, name)
            os.makedirs(root, exist_ok=True)
            projects[name] = {"name": name, "path": root,
                              "cwds": list(kw.get("cwds", {}).get(name, ()))}
        with open(path, "w") as f:
            json.dump({"version": 1, "projects": projects}, f)
        if names and self.seat_home is not None:
            self.home = os.path.join(projects[names[0]]["path"], "seat-home")
            os.makedirs(self.home, exist_ok=True)
            self.seat_home.stop()
            self.seat_home = mock.patch.object(seat, "_seat_home_cwd",
                                               return_value=self.home)
            self.seat_home.start()
        return path


    def _spawn_real_git(self, args, adapter):
        """`self._spawn` WITHOUT the Popen double.

        `mock.patch.object(seat.subprocess, "Popen", ...)` patches the
        subprocess MODULE, so every git call the spawn makes gets the double
        too — which is why `PATCH_SEAT_HOME` exists and why an arm about REAL
        provisioning must not use that runner. A native seat never reaches
        Popen (only the headless proxy leg does), so nothing here is left
        unguarded: `harness.detect` still returns a double and no pane is real.
        """
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(seat, "_write_launch_assets") as wla, \
                mock.patch.object(harness, "detect", return_value=adapter), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(["spawn"] + list(args))
        return rc, out.getvalue(), err.getvalue(), wla

    _PROC_HDR = ("  sl  local_address rem_address   st tx_queue rx_queue tr "
                 "tm->when retrnsmt   uid  timeout inode\n")

    def _pin_proc(self, *listening):
        """Point the endpoint allocator's LISTEN census at a fixture /proc.

        `seat_ports._listen_inodes` — the same reader the adopt-or-refuse door
        uses — takes its root from HELM_PROC, so an arm about which port gets
        allocated can state what this host is listening on instead of inheriting
        it. Without the pin, whatever the build host happens to have bound inside
        8500-8599 would move the answer.
        """
        root = os.path.join(self.tmp, "proc")
        os.makedirs(os.path.join(root, "net"), exist_ok=True)
        for table in ("tcp", "tcp6"):
            with open(os.path.join(root, "net", table), "w") as fh:
                fh.write(self._PROC_HDR)
                if table == "tcp":
                    for index, port in enumerate(listening):
                        fh.write("   %d: 0100007F:%04X 00000000:0000 0A "
                                 "00000000:00000000 00:00000000 00000000  1000 "
                                 "       0 %d 1 0000 100 0 0 10 0\n"
                                 % (index, port, 70110 + index))
        os.environ["HELM_PROC"] = root
        return root

    def _native_home(self, *sessions):
        """A real CLAUDE_CONFIG_DIR holding these session records — the storage a
        native pane's own launch selects, which is NOT the instance directory."""
        home = os.path.join(self.tmp, "native-claude-home")
        os.makedirs(os.path.join(home, "sessions"), exist_ok=True)
        for index, sid in enumerate(sessions):
            with open(os.path.join(home, "sessions", "%d.json" % index), "w") as fh:
                json.dump({"sessionId": sid}, fh)
        os.environ["CLAUDE_CONFIG_DIR"] = home
        return home

    def _bind_session(self, seat_name, session="session-a", handle="pane-1"):
        """Bind a session through the SHIPPED SessionStart door, the way six
        existing arms in this module already do. A pane register with no session
        cannot be ARCHIVED (`seat_exit_owner._spawn_defect`), so a --replace arm
        that skipped this would be measuring the archival contract instead of
        the replacement it claims to be about.

        NOT A STAND-IN FOR THE SPAWN'S OWN BINDING, since round four: a native
        spawn binds its own session where the metaharness can prove which live
        process owns the exact pane
        (`test_the_native_spawn_binds_its_session_from_the_pinned_home` drives
        that end to end). This remains the ARCHIVAL precondition for a seat that
        is about to be REPLACED, and the arms below use it only for that — an
        adapter that cannot prove a pane's process has no session to give them.
        """
        with mock.patch.object(seat, "_sessionstart_pane_fields",
                               return_value=({"handle": handle}, None)):
            self.assertTrue(seat._bind_spawn_session(seat_name, session))

class ProjectCanonicalSeatNameTest(ProjectRegistryBase):
    """task/2440 — a seat is spawned under its PROJECT name.

    Owner canon: teams are per TLA, each actively worked project gets one
    claude TLA and one codex TLA named <project>-<family>, and a team using
    helm must never need to know how helm is made. Every arm here drives the
    shipped verb in DRY form against a temp HELM_HOME and a temp registry;
    nothing is ever really spawned.
    """

    def test_project_codex_resolves_the_family_and_a_home_under_the_project(self):  # noqa: VACUOUS_ASSERTION — the no-actuation doubles sit beside an unconditional assert on the resolved launch path and the project line
        self._register("proj-a")
        _, launch = self._mint(seat_name="proj-a-codex", family="codex")
        rc, out, err, wla, popen = self._spawn(["proj-a-codex", "--print"],
                                              None)
        self.assertEqual(rc, 0, err)
        # family codex: the resolved seat home is codex's tree, keyed by the
        # project-canonical name — never a number.
        self.assertIn(os.path.join("seats", "codex", "instances",
                                   "proj-a-codex"), out)
        self.assertIn(launch, out)
        self.assertIn("project proj-a's codex seat", out)
        popen.assert_not_called()
        wla.assert_not_called()

    def test_project_claude_routes_through_the_native_launch_path(self):  # noqa: VACUOUS_ASSERTION — the no-actuation doubles sit beside unconditional asserts on HELM_CHAT_NAME read from build_env and on the launch line
        self._register("proj-a")
        rc, out, err, wla, popen = self._spawn(["proj-a-claude", "--print"],
                                              None)
        self.assertEqual(rc, 0, err)
        # The name reaches the child through the ONE native writer of the chat
        # name, and the plan reads it back from that producer.
        self.assertIn("HELM_CHAT_NAME=proj-a-claude", out)
        self.assertIn("build_env", out)
        self.assertIn("launch --seat proj-a-claude", out)
        self.assertIn("project proj-a's claude seat", out)
        popen.assert_not_called()
        wla.assert_not_called()

    def test_native_spawn_records_the_project_on_the_seat(self):  # noqa: VACUOUS_ASSERTION — the one absence assert sits beside unconditional asserts on the spawned command, the pane title and three register fields
        self._register("proj-a")
        fake = FakeAdapter()
        rc, out, err, wla, popen = self._spawn(["proj-a-claude"], fake)
        self.assertEqual(rc, 0, err)
        command, title, _ = fake.spawned[0]
        self.assertIn("launch --seat proj-a-claude", command)
        self.assertEqual(title, "proj-a-claude")
        rec = json.load(open(os.path.join(
            seat._instance_dir("claude", "proj-a-claude"), "spawn.json")))
        self.assertEqual(rec["project"], "proj-a")
        self.assertEqual(rec["family"], "claude")
        self.assertEqual(rec["seat"], "proj-a-claude")
        wla.assert_not_called()        # a native seat mints no proxy assets

    def test_native_spawn_says_the_credhome_will_launch_unsynced_when_held(self):  # noqa: VACUOUS_ASSERTION — the no-credhome control leg's absence sits beside the credhome leg's positive verdict, clause and holder pid
        """task/2505: a seat pinned to credhome H that spawns another native
        seat hands the pane its own CLAUDE_CONFIG_DIR, and the pane's launch
        finds the spawner's claude holding H and skips the Orca sync — a line
        printed only inside the new pane. The spawn output says it.

        The fixture is Orca's own layout (claude-accounts/<uuid>/auth with the
        ownership marker) and a credhome under a temp claude homes root; the
        holder probe is stubbed to the spawner's claude pid. CONTROL: the same
        spawn with no credhome pinned prints no freshness line. Mutation: drop
        the `spawn_note` print in seat._spawn_native and the credhome leg loses
        it."""
        from helm import cred, homes
        self._register("proj-a")
        root = os.path.join(self.tmp, "claude-homes")
        orca = os.path.join(self.tmp, "orca")
        import time
        now = int(time.time() * 1000)

        def creds(tok, hours, life_hours=24 * 20):
            return {"claudeAiOauth": {
                "accessToken": tok + "-A", "refreshToken": tok + "-R",
                "expiresAt": now + hours * 3600000,
                "refreshTokenExpiresAt": now + life_hours * 3600000}}

        home = os.path.join(root, "seat-example-test")
        uuid = "0a1b2c3d-0000-4000-8000-00000000cafe"
        auth = os.path.join(orca, "claude-accounts", uuid, "auth")
        for d, files in ((home, {".claude.json": {"oauthAccount": {
                              "emailAddress": "seat@example.test"}},
                          # the home's own refresh lifetime has passed: STALE,
                          # whatever the lineage records
                          ".credentials.json": creds("FAKE-HOME", -10, -1)}),
                         (auth, {"oauth-account.json": {
                              "emailAddress": "seat@example.test"},
                          ".credentials.json": creds("FAKE-ORCA", 8)})):
            os.makedirs(d, exist_ok=True)
            for name, doc in files.items():
                with open(os.path.join(d, name), "w") as fh:
                    json.dump(doc, fh)
        with open(os.path.join(auth, ".orca-managed-claude-auth"), "w") as fh:
            fh.write(uuid)
        register = os.path.join(seat._instance_dir("claude", "proj-a-claude"),
                                "spawn.json")
        with mock.patch.dict(homes.ROOTS, {"claude": root}), \
                mock.patch.dict(homes.DEFAULTS, {"claude": os.path.join(self.tmp, "dflt")}), \
                mock.patch.dict(os.environ, {"ORCA_USER_DATA_PATH": orca}), \
                mock.patch.object(cred, "holders_of",
                                  lambda p, default=False: [(4242, "claude")]):
            cred.cache_clear()
            rc, out, err, _wla, _popen = self._spawn(["proj-a-claude"], FakeAdapter())
            self.assertEqual(rc, 0, err)
            self.assertNotIn("Orca freshness", out)
            os.unlink(register)
            os.environ["CLAUDE_CONFIG_DIR"] = home
            rc, out, err, _wla, _popen = self._spawn(["proj-a-claude"], FakeAdapter())
            cred.cache_clear()
        self.assertEqual(rc, 0, err)
        line = [l for l in out.splitlines() if "Orca freshness" in l]
        self.assertEqual(len(line), 1, out)
        self.assertIn("STALE-vs-ORCA", line[0])
        self.assertIn("will NOT sync it", line[0])
        self.assertIn("pid 4242", line[0])
        self.assertIn(home, line[0])

    def test_native_model_rides_the_pane_command_and_sticks_across_replace(self):  # noqa: VACUOUS_ASSERTION — the one assertNotIn is the no-model control leg, beside unconditional presence asserts on the --model token and the register in both later legs
        """task/2505: a rehomed native seat came up on the credhome's
        settings.json default model, because native spawn refused --model and
        the pane command carried none.

        The model is read back out of the command the adapter was HANDED (the
        only env channel a pane has) and out of the register. CONTROL: the
        first leg — no --model and no prior register — hands a command with no
        `--model` at all, so the later asserts are the flag and not a constant.
        Mutation: drop `model or _persisted_model(d, seat_name)` in `_spawn` and
        the --replace leg's command loses `--model claude-opus-5`."""
        self._register("proj-a")
        register = os.path.join(seat._instance_dir("claude", "proj-a-claude"),
                                "spawn.json")
        fake = FakeAdapter()
        rc, _out, err, _wla, _popen = self._spawn(["proj-a-claude"], fake)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("--model", fake.spawned[0][0])
        os.unlink(register)
        fake = FakeAdapter()
        rc, _out, err, _wla, _popen = self._spawn(
            ["proj-a-claude", "--model", "claude-opus-5"], fake)
        self.assertEqual(rc, 0, err)
        self.assertIn("launch --seat proj-a-claude", fake.spawned[0][0])
        self.assertIn("--model claude-opus-5", fake.spawned[0][0])
        with open(register) as fh:
            self.assertEqual(json.load(fh)["model"], "claude-opus-5")
        # the live-pane shape `test_explicit_replace_reaps_the_live_native_pane`
        # drives: a bound session and the pane in the adapter's inventory
        self._bind_session("proj-a-claude")
        fake = FakeAdapter(rows=[
            {"handle": "pane-1", "title": "dynamic", "status": "idle"}])
        rc, _out, err, _wla, _popen = self._spawn(
            ["proj-a-claude", "--replace"], fake)
        self.assertEqual(rc, 0, err)
        self.assertEqual(fake.stopped, ["pane-1"])
        self.assertIn("--model claude-opus-5", fake.spawned[0][0])
        with open(register) as fh:
            self.assertEqual(json.load(fh)["model"], "claude-opus-5")

    def test_numbered_form_is_unchanged(self):  # noqa: VACUOUS_ASSERTION — MUST-HIT; the absence assert sits beside unconditional asserts on the instance dir and the launch path; this arm was GREEN before the cure
        """No registry at all, and the numbered seat spawns exactly as before —
        the arm that fails the moment the new name form is resolved first."""
        _, launch = self._mint(seat_name="codex-10", family="codex")
        rc, out, err, wla, popen = self._spawn(["codex-10", "--print"], None)
        self.assertEqual(rc, 0, err)
        self.assertIn(os.path.join("instances", "codex-10"), out)
        self.assertIn(launch, out)
        self.assertNotIn("project-canonical", out)
        popen.assert_not_called()
        wla.assert_not_called()

    def test_unregistered_project_is_refused_naming_the_registry(self):  # noqa: VACUOUS_ASSERTION — the refusal TEXT is the positive control: rc 2 plus the project, the registry file and the family list asserted unconditionally
        self._register("proj-a")
        rc, out, err, wla, popen = self._spawn(["seat-b-codex", "--print"],
                                              None)
        self.assertEqual(rc, 2)
        self.assertIn("unknown project 'seat-b'", err)
        self.assertIn("registry.json", err)
        self.assertIn("proj-a", err)            # what the registry does hold
        self.assertIn("codex", err)             # and the family list
        wla.assert_not_called()
        # nothing was spawned or registered (the alias-evidence probe reads git,
        # so the Popen double is not the no-actuation witness here)
        self.assertFalse(os.path.exists(os.path.join(
            seat._instance_dir("codex", "seat-b-codex"), "spawn.json")))

    def test_unknown_family_is_refused_naming_the_family_list(self):  # noqa: VACUOUS_ASSERTION — the refusal TEXT is the positive control: rc 2 plus every family name and the registry file asserted unconditionally
        self._register("proj-a")
        rc, out, err, wla, popen = self._spawn(["proj-a-borg", "--print"],
                                              None)
        self.assertEqual(rc, 2)
        self.assertIn("registry.json", err)
        for family in ("claude", "codex", "gemini", "grok", "kimi"):
            self.assertIn(family, err)
        wla.assert_not_called()
        self.assertFalse(os.path.exists(os.path.join(
            seat.seats_root(), "borg")))

    def test_a_second_same_family_tla_names_the_existing_seat(self):  # noqa: VACUOUS_ASSERTION — the no-actuation doubles sit beside unconditional asserts on the notice naming the existing seat
        self._register("proj-a")
        other, _ = self._mint(seat_name="codex-11", family="codex")
        self._record(other, seat="codex-11", project="proj-a")
        self._mint(seat_name="proj-a-codex", family="codex")
        rc, out, err, wla, popen = self._spawn(["proj-a-codex", "--print"],
                                              None)
        self.assertEqual(rc, 0, err)
        said = out + err
        self.assertIn("codex-11", said)
        self.assertIn("already has a codex seat", said)
        popen.assert_not_called()
        wla.assert_not_called()


class ProjectSeatLifecycleTest(ProjectRegistryBase):
    """task/2440 round two — the NATIVE leg is not a second lifecycle.

    One structural move put the native adapter behind the same lock, prior
    register, reap, --replace refusal, surface-ownership proof, home provision,
    onboarding submit and role policy the proxy adapter already went through.
    Each obligation keeps its OWN arm here, because one cure covering seven
    findings is exactly the shape under which a later edit can drop six of them
    and stay green on the seventh.
    """

    def test_live_native_duplicate_is_refused_without_replace(self):  # noqa: VACUOUS_ASSERTION — the two absence asserts ARE the finding (a second pane must not exist) and stand beside three unconditional positives: rc 1, the shipped refusal text, and the surviving register's bound session
        """FINDING 2 — two spawns of one native seat left two live panes.

        CONTROL: `test_explicit_replace_reaps_the_live_native_pane` below drives
        the SAME fixture with --replace and gets rc 0 plus a reaped pane, so this
        refusal cannot be an unconditional failure of the native path. Blast
        radius of the pair: the shared `_spawn_reap` rung — remove it from the
        native leg and this arm spawns a second pane and returns 0.
        """
        self._register("proj-a")
        first = FakeAdapter()
        self.assertEqual(self._spawn(["proj-a-claude"], first)[0], 0)
        d = seat._instance_dir("claude", "proj-a-claude")
        self._bind_session("proj-a-claude")
        fake = FakeAdapter(rows=[
            {"handle": "pane-1", "title": "dynamic", "status": "working"}])
        rc, _out, err, _wla, _popen = self._spawn(["proj-a-claude"], fake)
        self.assertEqual(rc, 1)
        self.assertIn("pass --replace", err)
        self.assertEqual(fake.spawned, [])
        self.assertEqual(fake.stopped, [])
        # the FIRST seat is still the registered one
        with open(os.path.join(d, "spawn.json")) as f:
            self.assertEqual(json.load(f)["session"], "session-a")

    def test_explicit_replace_reaps_the_live_native_pane(self):  # noqa: VACUOUS_ASSERTION — the session assertIsNone is the REPLACEMENT's own fresh register, beside unconditional positives on rc 0, the stopped handle, the reap-first ordering, the reap note and exactly one new pane
        """FINDING 2's paired positive: --replace ACTS on a native seat."""
        self._register("proj-a")
        first = FakeAdapter()
        self.assertEqual(self._spawn(["proj-a-claude"], first)[0], 0)
        d = seat._instance_dir("claude", "proj-a-claude")
        self._bind_session("proj-a-claude")
        fake = FakeAdapter(rows=[
            {"handle": "pane-1", "title": "dynamic", "status": "idle"}])
        rc, out, err, _wla, _popen = self._spawn(
            ["proj-a-claude", "--replace"], fake)
        self.assertEqual(rc, 0, err)
        self.assertEqual(fake.stopped, ["pane-1"])
        self.assertEqual(fake.order[0], "stop")    # reap strictly first
        self.assertIn("reaped stale proj-a-claude pane pane-1", out)
        self.assertEqual(len(fake.spawned), 1)
        with open(os.path.join(d, "spawn.json")) as f:
            # the replacement's own register: session back to None until its
            # SessionStart binds one
            self.assertIsNone(json.load(f)["session"])

    def test_native_register_refuses_a_sibling_symlinked_instance_dir(self):  # noqa: VACUOUS_ASSERTION — the sentinel's UNCHANGED bytes are read back and compared to a value written in this arm, beside an unconditional rc and refusal-text assert
        """FINDING 8 — the native mkdir + register carried no ownership proof.

        The sentinel is a real sibling seat's register. CONTROL: the same fixture
        with a LITERAL directory instead of the symlink is
        `test_native_spawn_records_the_project_on_the_seat` (rc 0, register
        written), so this arm's rc 2 is the symlink's doing and not the fixture's.
        Blast radius: `_seat_spawn_gate` — drop it from `_spawn`'s native branch
        and `mkdir(exist_ok=True)` follows the link and the register write lands
        on the sentinel.
        """
        self._register("proj-a")
        sibling = seat._instance_dir("claude", "other-claude")
        os.makedirs(sibling, exist_ok=True)
        sentinel = os.path.join(sibling, "spawn.json")
        with open(sentinel, "w") as f:
            json.dump({"v": 1, "seat": "other-claude", "sentinel": True}, f)
        link = seat._instance_dir("claude", "proj-a-claude")
        os.makedirs(os.path.dirname(link), exist_ok=True)
        os.symlink(sibling, link)
        fake = FakeAdapter()
        rc, _out, err, _wla, _popen = self._spawn(["proj-a-claude"], fake)
        self.assertEqual(rc, 2)
        self.assertIn("changes identity through a symlink", err)
        self.assertEqual(fake.spawned, [])
        with open(sentinel) as f:
            self.assertEqual(json.load(f),
                             {"v": 1, "seat": "other-claude", "sentinel": True})

    def test_native_lead_carries_ultracode_and_the_role_env(self):
        """FINDING 7 — --role lead was recorded and never applied.

        CONTROL: the worker arm below drives the identical call with the default
        role and asserts BOTH markers absent, so this arm cannot pass on a
        command that always carries them. Blast radius: `_native_launch_command`
        — take the lead branch out and this arm's two asserts fail while the
        worker arm stays green.
        """
        self._register("proj-a")
        fake = FakeAdapter()
        rc, _out, err, _wla, _popen = self._spawn(
            ["proj-a-claude", "--role", "lead"], fake)
        self.assertEqual(rc, 0, err)
        command = fake.spawned[0][0]
        self.assertTrue(command.startswith("env HELM_SEAT_ROLE=lead "), command)
        self.assertIn("-- --settings", command)
        self.assertIn('{"ultracode":true}', command)
        rec = json.load(open(os.path.join(
            seat._instance_dir("claude", "proj-a-claude"), "spawn.json")))
        self.assertEqual(rec["role"], "lead")

    def _launched_role(self, command, inherited="lead"):
        """The role the CHILD actually receives, measured by RUNNING the exact
        command string the adapter was handed under a shell that exports
        `inherited`.

        THE COMMAND IS THE ONLY ENV CHANNEL a pane adapter has, so asserting on
        substrings answers the wrong question: a command that merely OMITS the
        assignment inherits the launcher's value, and that is the defect. The
        `helm` binary is replaced by a script that prints what it was given —
        `_native_launch_argv` resolves it through `chatnode.helm_bin`, the one
        seam — so what comes back is the launched environment itself.
        """
        import subprocess
        env = dict(os.environ, HELM_SEAT_ROLE=inherited)
        out = subprocess.run(command, shell=True, env=env, text=True,
                             capture_output=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout

    def _role_probe_bin(self):
        """A stand-in `helm` that prints the role env it was launched with."""
        path = os.path.join(self.tmp, "helm-role-probe")
        with open(path, "w") as fh:
            fh.write('#!/bin/sh\nprintf %s "${HELM_SEAT_ROLE-ABSENT}"\n')
        os.chmod(path, 0o700)
        return path

    def test_native_worker_clears_an_inherited_lead_role(self):  # noqa: VACUOUS_ASSERTION — there is no absence assert here at all: both legs of the loop assert an EXACT launched-env string (ABSENT vs lead) read back from a real subprocess, plus rc 0 and the recorded role, and each leg is the other's control
        """ROUND FOUR, finding 6 — the worker command OMITTED the lead marker
        instead of CLEARING it, so a worker spawned from a lead's pane booted
        with a lead brief while its own register said worker.

        Both arms RUN the command the adapter was handed, in a shell exporting
        HELM_SEAT_ROLE=lead. CONTROL, in this arm: the lead role is driven
        through the same runner and must come back `lead`, so a cure that simply
        deleted the variable everywhere would fail here. Blast radius:
        `_native_launch_command`'s env prefix — before the cure the worker's
        bytes carried no env prefix at all and this arm read back `lead`.
        """
        from helm import chatnode
        self._register("proj-a")
        probe = self._role_probe_bin()
        register = os.path.join(seat._instance_dir("claude", "proj-a-claude"),
                                "spawn.json")
        for role, expect in (("worker", "ABSENT"), ("lead", "lead")):
            with self.subTest(role=role):
                # each leg is a FRESH spawn of the seat: leaving the previous
                # leg's register standing would make the second call a duplicate
                # and measure the reap instead of the role
                if os.path.exists(register):
                    os.unlink(register)
                fake = FakeAdapter()
                args = ["proj-a-claude"] + (
                    ["--role", "lead"] if role == "lead" else [])
                with mock.patch.object(chatnode, "helm_bin",
                                       return_value=probe):
                    rc, _out, err, _wla, _popen = self._spawn(args, fake)
                self.assertEqual(rc, 0, err)
                command = fake.spawned[0][0]
                self.assertIn("--seat proj-a-claude", command)
                self.assertEqual(self._launched_role(command), expect)
                rec = json.load(open(os.path.join(
                    seat._instance_dir("claude", "proj-a-claude"),
                    "spawn.json")))
                self.assertEqual(rec["role"], role)
                # the register and the launched env agree, which is the property
                # the finding says they did not have
                self.assertEqual(rec["role"] == "lead", expect == "lead")

    def test_native_worker_carries_no_lead_settings_flag(self):  # noqa: VACUOUS_ASSERTION — the absence assert is the CONTROL for the lead arm above and sits beside an unconditional assert on the full launch command
        """The lead arm's argv control: only a lead gets ultracode."""
        self._register("proj-a")
        fake = FakeAdapter()
        rc, _out, err, _wla, _popen = self._spawn(["proj-a-claude"], fake)
        self.assertEqual(rc, 0, err)
        command = fake.spawned[0][0]
        self.assertIn("launch --seat proj-a-claude", command)
        self.assertNotIn("--settings", command)

    def test_native_spawn_submits_the_onboarding_and_records_it(self):
        """FINDING 6 — the native leg left a bare idle pane.

        CONTROL: `test_native_spawn_reports_an_unsubmitted_brief_honestly` drives
        the same door with an adapter whose composer keeps the text and gets
        rc 1 plus a NOT-PROVEN sentence, so a green here means delivery was
        actually measured. Blast radius: the shared `_spawn_submit` rung.
        """
        self._register("proj-a")
        fake = FakeAdapter()
        rc, out, err, _wla, _popen = self._spawn(["proj-a-claude"], fake)
        self.assertEqual(rc, 0, err)
        # typed without Enter, then submitted by a bare Enter of its own
        self.assertEqual(len(fake.sent), 2)
        handle, text, enter = fake.sent[0]
        self.assertEqual(handle, "pane-1")
        self.assertFalse(enter, "the text leg must NOT carry Enter")
        self.assertEqual(text, "Run `helm seat boot-brief` and follow it.")
        self.assertEqual(fake.sent[1], ("pane-1", "", True))
        self.assertIn("onboarding submitted", out)
        rec = json.load(open(os.path.join(
            seat._instance_dir("claude", "proj-a-claude"), "spawn.json")))
        self.assertEqual(rec["onboarding"], harness.DELIVERED)

    def test_native_spawn_reports_an_unsubmitted_brief_honestly(self):  # noqa: VACUOUS_ASSERTION — the no-register assert is the finding's other half and stands beside unconditional positives on rc 1, the shipped failure text and the closed pane handle
        """FINDING 6's negative: an unreadable/held composer is NOT success."""
        self._register("proj-a")

        class HeldComposer(FakeAdapter):
            """Enter is accepted and the composer still holds the brief — the
            exact live shape `submit` was written to catch."""
            held = None

            def send(self, handle, text, enter=True):
                FakeAdapter.send(self, handle, text, enter)
                if not enter:
                    self.held = text
                elif self.held:
                    self.typed[handle] = self.held

        fake = HeldComposer()
        rc, _out, err, _wla, _popen = self._spawn(["proj-a-claude"], fake)
        self.assertEqual(rc, 1)
        self.assertIn("onboarding was not submitted", err)
        self.assertIn("proj-a-claude", err)
        self.assertEqual(fake.stopped, ["pane-1"])   # the blank pane is closed
        self.assertFalse(os.path.exists(os.path.join(
            seat._instance_dir("claude", "proj-a-claude"), "spawn.json")))

    def test_native_print_creates_no_scratch_tmpdir(self):  # noqa: VACUOUS_ASSERTION — the absence asserts are the finding's whole observable and sit beside unconditional asserts on rc and on the HELM_CHAT_NAME the plan read back from build_env
        """FINDING 10 — --print called a MUTATING env producer.

        The router double both records the call and would create a directory, so
        the arm sees the act and its trace. CONTROL: the same plan with
        `allocate_scratch` left at its default IS the live path, exercised by
        every live native arm above (all green), so this is not an arm that
        passes because build_env is never reached. Blast radius:
        `build_env(allocate_scratch=...)` — remove the parameter's guard and this
        arm's two absence asserts fail while every live arm stays green.
        """
        self._register("proj-a")
        sentinel = os.path.join(self.tmp, "scratch-tmpdir")

        def router():
            os.makedirs(sentinel, exist_ok=True)
            return sentinel

        probe = mock.Mock(side_effect=router)
        from helm import scratch
        env = dict(os.environ)
        env.pop("TMPDIR", None)
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(scratch, "launch_tmpdir", probe):
            rc, out, err, _wla, _popen = self._spawn(
                ["proj-a-claude", "--print"], None)
        self.assertEqual(rc, 0, err)
        self.assertIn("HELM_CHAT_NAME=proj-a-claude", out)
        probe.assert_not_called()
        self.assertFalse(os.path.isdir(sentinel))

    def test_native_default_home_is_really_provisioned(self):
        """FINDING 4 — the live native call always passed provision=False.

        REAL git, no stub: `_seat_home_cwd` is left unpatched and the fixture is
        an actual repository registered under its own name. CONTROL: the cwd the
        adapter receives is asserted to be the deterministic seat-home path AND
        to exist AND to be a registered worktree carrying the seat branch — a
        prospective path satisfies the first and fails the other two, which is
        exactly the pre-cure state. Blast radius: the `provision=True`
        re-resolution inside `_spawn`'s lock.
        """
        from helm import harness as harness_mod
        from helm.work._lanes import _git, worktrees
        root = os.path.join(self.tmp, "real-proj")
        os.makedirs(root)
        _git(root, "init", "-q")
        _git(root, "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "--allow-empty", "-m", "init")
        _git(root, "branch", "-M", "main")
        self._register("real-proj", paths={"real-proj": root})
        self.seat_home.stop()                 # the seam under test
        self.seat_home = None
        fake = FakeAdapter()
        with mock.patch("helm.seats.safe_cwd", return_value=root):
            rc, _out, err, _wla = self._spawn_real_git(
                ["real-proj-claude"], fake)
        self.assertEqual(rc, 0, err)
        cwd = fake.spawned[0][2]
        expect = harness_mod.seat_worktree_path(root, "real-proj-claude")
        self.assertEqual(cwd, expect)
        self.assertTrue(os.path.isdir(cwd), "the home was never created")
        registered = {os.path.realpath(w["path"]) for w in worktrees(root)}
        self.assertIn(os.path.realpath(cwd), registered)
        rc_branch, _out2, _err2 = _git(
            root, "rev-parse", "--verify", "-q",
            harness_mod.seat_branch("real-proj-claude"))
        self.assertEqual(rc_branch, 0, "the seat branch was never cut")

    def test_unregistered_workspace_is_refused_before_provisioning(self):  # noqa: VACUOUS_ASSERTION — the not-created assert is the finding's observable and sits beside unconditional asserts on rc 2 and three clauses of the refusal
        """FINDING 3 — a registered NAME did not bind the workspace written.

        The registry holds proj-a; the operator stands in an unregistered
        checkout. CONTROL:
        `test_registered_linked_worktree_of_the_project_is_admitted` drives the
        same door from a path that IS in the project's scope and gets rc 0, so
        this refusal is about attribution and not about the door being shut.
        Blast radius: `_spawn_scope_error` — remove its call and this arm
        provisions a home and a branch inside the unregistered repo.
        """
        from helm.work._lanes import _git
        unreg = os.path.join(self.tmp, "unreg-u")
        os.makedirs(unreg)
        _git(unreg, "init", "-q")
        self._register("proj-a")
        self.seat_home.stop()
        self.seat_home = None
        self._mint(seat_name="proj-a-codex", family="codex")
        fake = FakeAdapter()
        with mock.patch("helm.seats.safe_cwd", return_value=unreg):
            rc, _out, err, _wla = self._spawn_real_git(["proj-a-codex"], fake)
        self.assertEqual(rc, 2)
        self.assertIn("its workspace is not proj-a's", err)
        self.assertIn("not registered to any project", err)
        self.assertIn("helm sync", err)
        self.assertFalse(os.path.isdir(unreg.rstrip("/") + "-wt"),
                         "a refusal must arrive BEFORE provisioning")

    def test_registered_linked_worktree_of_the_project_is_admitted(self):  # noqa: VACUOUS_ASSERTION — the rc-0 assert is the observable a refusal would break, and it stands beside three unconditional positives from the same call: the project line, the spawned sentence, and the exact home path handed to the adapter
        """FINDING 3's positive: SCOPE, not raw containment.

        A lane worktree of the project lives in a SIBLING directory and shares no
        prefix with the registered root, so a containment check would refuse the
        project's own tree. This is the arm that keeps the cure from being a
        basename equality.
        """
        from helm import harness as harness_mod
        from helm.work._lanes import _git
        root = os.path.join(self.tmp, "scoped")
        os.makedirs(root)
        _git(root, "init", "-q")
        _git(root, "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "--allow-empty", "-m", "init")
        _git(root, "branch", "-M", "main")
        lane = os.path.join(self.tmp, "scoped-wt", "lane-x")
        _git(root, "worktree", "add", "-q", lane, "-b", "lane-x")
        self._register("scoped", paths={"scoped": root})
        self.seat_home.stop()
        self.seat_home = None
        fake = FakeAdapter()
        with mock.patch("helm.seats.safe_cwd", return_value=lane):
            rc, out, err, _wla = self._spawn_real_git(["scoped-claude"], fake)
        self.assertIn("project scoped's claude seat", out)
        self.assertEqual(rc, 0, err)
        self.assertIn("spawned scoped-claude via fake", out)
        # provisioned against the MAIN checkout the registry names, not the lane
        self.assertEqual(fake.spawned[0][2],
                         harness_mod.seat_worktree_path(root, "scoped-claude"))

    def test_a_project_codex_endpoint_is_distinct_from_every_sibling(self):
        """FINDING 1 — every project-codex name aliased the base proxy port.

        Drives the SHIPPED allocator, not a hash: the base seat, a numbered
        instance and two project seats must hold four different endpoints.
        CONTROL: `codex` itself is asserted to still be the family base (8317),
        the number every minted launch.sh already points at — so the cure cannot
        have moved the existing fleet. Blast radius: `_instance_port`'s project
        branch; before it, the last two values were both 8317 and this arm's set
        had two members.
        """
        from helm.seat_catalog import FAMILIES
        self._pin_proc()                  # nothing on this host is listening
        base = FAMILIES["codex"]["port"]
        self.assertEqual(base, 8317)      # the number every launch.sh points at
        self.assertEqual(seat._instance_port("codex", "codex"), base)
        self.assertEqual(seat._instance_port("codex", "codex-2"), base + 2)  # noqa: SEAT_NAME — the numbered instance IS this line's subject: base+N must still be the answer for it, and that is the control proving the project branch did not move the existing fleet
        # THE ALLOCATION IS ITS OWN DOOR NOW (round four, finding 7): reading a
        # port must never spend one, so the producer asked here is the allocator,
        # and `_instance_port` is asserted to be the pure READ of what it wrote.
        self.assertIsNone(seat._instance_port("codex", "proj-a-codex"))
        a, a_err = seat.allocate_instance_port("proj-a-codex")
        b, b_err = seat.allocate_instance_port("proj-b-codex")
        self.assertEqual([a_err, b_err], [None, None])
        # EXACT, not merely non-None: a fresh HELM_HOME's ledger is empty, so the
        # first two project seats take the first two endpoints of the block.
        self.assertEqual([a, b], [seat.PROJECT_PORT_BASE,
                                  seat.PROJECT_PORT_BASE + 1])
        self.assertEqual(len({base, base + 2, a, b}), 4,
                         "endpoints must be injective across sibling forms")
        # the exact-pair assert above already pins both inside the block, so the
        # per-port range loop it replaced added nothing but a conditional
        # statement (and a weaker claim than the one beside it)
        # stable across calls: a second ask is a LEDGER read, not a new take
        self.assertEqual(seat.allocate_instance_port("proj-a-codex"), (a, None))
        self.assertEqual(seat._instance_port("codex", "proj-a-codex"), a)
        self.assertEqual(seat.instance_port_ledger()[0]["proj-a-codex"], a)

    def test_a_numbered_suffix_reaching_the_project_block_is_refused(self):
        """FINDING 1's adversarial half, found by probing the cure itself.

        base+N is unbounded, so codex-183 derives exactly the first allocated
        project endpoint. Headroom between BASES is assertable; disjointness from
        base+N can only be a refusal. CONTROL: codex-99 (the same name shape, one
        derivation below the block) is asserted admissible in the same arm.
        """
        from helm.seat_catalog import FAMILIES
        base = FAMILIES["codex"]["port"]
        low = "codex-%d" % (seat.PROJECT_PORT_BASE - base - 1)
        high = "codex-%d" % (seat.PROJECT_PORT_BASE - base)
        self.assertIsNone(seat._instance_gate("codex", low))
        gate = seat._instance_gate("codex", high)
        self.assertIsNotNone(gate)
        self.assertIn("reaches the project-instance block", gate)
        self.assertIsNone(seat._instance_port("codex", high))

    def test_a_native_family_never_gets_a_port(self):
        """FINDING 1's other half: native claude runs no proxy, so it has no
        endpoint at all, and the proxy gate says so instead of raising."""
        self.assertIsNone(seat._instance_port("claude", "proj-a-claude"))
        self.assertIsNone(seat._existing_instance_port("claude",
                                                      "proj-a-claude"))
        gate = seat._instance_gate("claude", "proj-a-claude")
        self.assertIn("not a proxy-table family", gate)
        self.assertIsNone(seat._seat_spawn_gate("claude", "proj-a-claude"))

    def test_the_register_round_trips_into_where_and_the_reboot_sweep(self):  # noqa: VACUOUS_ASSERTION — the unminted leg's two absence asserts are the refusal's own observable and stand beside four unconditional positives from the SAME call: rc 1 and both clauses of the shipped text, then rc 0 and the register fields after the mint
        """FINDING 5 — a managed project name no managed consumer could read.

        The register is written by the SHIPPED spawn verb, then read back by the
        shipped resolver and by the reboot classifier. CONTROL: the same three
        questions are asked about a name nothing spawned, and all three still
        refuse — so resolution is coming from the register and not from the
        name's shape. Blast radius: `_seat_family`'s register rung; without it
        `_where` diverted to the adoption path and `classify_spawned` returned
        "register sits under no known family".

        THE PRECONDITION IS ITSELF A SHIPPED RULE, and the round-two gate caught
        this arm ignoring it: `proj-a-codex` is a PROXY instance, and a proxy
        instance has no proxy home until the family is minted — so the shipped
        verb refuses with `helm seat add codex` first. Task/2440 admitted the
        NAME; it never claimed a registry entry substitutes for the family mint.
        The refusal is asserted here as the arm's own negative (rc 1, nothing
        spawned, same registry, same name) before `_mint` supplies the family
        asset every sibling project-codex arm already supplies — so this arm now
        pins both halves of the door instead of tripping over one of them.
        """
        from helm import seat_resume_all
        self._register("proj-a")
        unminted = FakeAdapter()
        rc0, _o0, err0, _w0, popen0 = self._spawn(["proj-a-codex"], unminted)
        self.assertEqual(rc0, 1)
        self.assertIn("no proj-a-codex seat minted", err0)
        self.assertIn("helm seat add codex", err0)
        self.assertEqual(unminted.spawned, [])
        popen0.assert_not_called()
        self._mint(seat_name="proj-a-codex", family="codex")
        fake = FakeAdapter()
        rc, _out, err, _wla, _popen = self._spawn(["proj-a-codex"], fake)
        self.assertEqual(rc, 0, err)
        self.assertEqual(seat._seat_family("proj-a-codex"), ("codex", None))
        self.assertEqual(seat.registered_seat_family("proj-a-codex"),
                         ("codex", "proj-a"))
        self.assertEqual(seat._split_seat("proj-a-codex"),
                         ("codex", "proj-a-codex"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                mock.patch.object(harness, "detect", return_value=fake):
            self.assertEqual(seat.cmd_seat(
                ["where", "proj-a-codex", "--json"]), 0)
        row = json.loads(out.getvalue())
        self.assertEqual(row["seat"], "proj-a-codex")
        self.assertEqual(row["family"], "codex")
        self.assertEqual(row["project"], "proj-a")
        sweep, _handle = seat_resume_all.classify_spawned(
            "proj-a-codex", fake, {}, lambda: {}, lambda: set(), 0, False)
        self.assertNotIn("no known family", sweep.reason)
        self.assertTrue(sweep.kind.startswith("codex/"), sweep.kind)
        # the control: a name nothing registered still refuses, everywhere
        self.assertIsNone(seat._seat_family("ghost-codex")[0])
        self.assertEqual(seat.registered_seat_family("ghost-codex"), (None, None))
        err2 = io.StringIO()
        with contextlib.redirect_stderr(err2):
            self.assertEqual(seat.cmd_seat(["where", "ghost-codex"]), 2)
        self.assertIn("unknown seat 'ghost-codex'", err2.getvalue())
        ghost = seat_resume_all.classify_spawned(
            "ghost-codex", fake, {}, lambda: {}, lambda: set(), 0, False)[0]
        self.assertIn("no known family", ghost.reason)

    def test_a_resumed_register_still_names_the_family_that_resolves_it(self):  # noqa: VACUOUS_ASSERTION — the absences (no attempt on the resumed record, no "unknown seat" in the incomplete reason, no INCOMPLETE in the ghost's) each sit beside unconditional positives on the SAME value: the resumed record's family and project, the incomplete reason's exact clause and path, the ghost's exact unknown-seat text
        """THE RESUME LEG WROTE A REGISTER THE RESOLVER COULD NOT READ.

        `_resume` built its register record with no `family` and no `project`,
        although it had resolved the family itself and held the project in the
        record it replaced. A project seat's family is not in its name, so the
        next `_seat_family` answered "unknown seat", the reboot sweep's first rung
        returned UNKNOWN and never re-stamped the seat, and the seat's own next
        resume could not find its register.

        BOTH DIRECTIONS IN ONE ARM, every write through a shipped door: the SPAWN
        writes the prior register, the SessionStart door binds its session, and
        the RESUME verb writes the register under test — a spy on
        `_register_spawn` proves that write is the resume's, and the archive the
        reap wrote in the same act holds the spawn's values. Then the same record
        with the two keys dropped, the exact shape the old resume leg produced,
        must be refused with the INCOMPLETE reason and never "unknown seat",
        while the sweep's state stays UNKNOWN (a reason, not a new state).
        CONTROL: a name nothing registered still reads "unknown seat". Blast
        radius: the record in `_resume` and `_register_reading`'s defect slot.
        """
        from helm import seat_resume_all
        self._pin_proc()
        self._register("proj-a")
        d, _launch = self._mint(seat_name="proj-a-codex", family="codex")
        rc0, _o0, err0, _w0, _p0 = self._spawn(["proj-a-codex"], FakeAdapter())
        self.assertEqual(rc0, 0, err0)
        self._bind_session("proj-a-codex")
        spawned = seat._spawn_record(d)
        self.assertEqual((spawned["family"], spawned["project"]),
                         ("codex", "proj-a"))
        live = FakeAdapter(rows=[{"handle": "pane-1", "title": "dynamic",
                                  "status": "idle"}])
        spy = mock.Mock(wraps=seat._register_spawn)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(seat, "_write_launch_assets"), \
                mock.patch.object(seat, "_up", return_value=0), \
                mock.patch.object(seat, "_register_spawn", spy), \
                mock.patch.object(harness, "detect", return_value=live), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(["resume", "proj-a-codex"])
        self.assertEqual(rc, 0, err.getvalue())
        self.assertEqual(live.stopped, ["pane-1"])
        self.assertEqual(spy.call_count, 1)
        written = spy.call_args[0][3]
        self.assertEqual((written["family"], written["project"]),
                         ("codex", "proj-a"))
        resumed = seat._spawn_record(d)
        self.assertEqual((resumed["family"], resumed["project"]),
                         ("codex", "proj-a"))
        self.assertEqual(resumed["handle"], "pane-1")
        self.assertNotIn("attempt", resumed,
                         "the spawn's record survived; the resume never wrote")
        with open(seat_exit_owner.archive_paths(d)[-1]) as fh:
            archived = json.load(fh)
        self.assertEqual((archived["family"], archived["project"]),
                         ("codex", "proj-a"))
        # the resolver reads the register the resume wrote
        self.assertEqual(seat._seat_family("proj-a-codex"), ("codex", None))
        self.assertEqual(seat.registered_seat_family("proj-a-codex"),
                         ("codex", "proj-a"))
        sweep = seat_resume_all.classify_spawned(
            "proj-a-codex", live, {}, lambda: {}, lambda: set(), 0, False)[0]
        self.assertTrue(sweep.kind.startswith("codex/"), sweep.kind)
        # THE OTHER DIRECTION: the same record as the old resume leg wrote it
        path = os.path.join(d, "spawn.json")
        with open(path, "w") as fh:
            json.dump({key: value for key, value in resumed.items()
                       if key not in ("family", "project")}, fh)
        family, why = seat._seat_family("proj-a-codex")
        self.assertIsNone(family)
        self.assertIn("%s is an INCOMPLETE register, missing family and "
                      "project (it sits in the codex seat tree)" % path, why)
        self.assertNotIn("unknown seat", why)
        self.assertEqual(seat.registered_seat_family("proj-a-codex"),
                         (None, None), "the resolver's own contract is unchanged")
        broken = seat_resume_all.classify_spawned(
            "proj-a-codex", live, {}, lambda: {}, lambda: set(), 0, False)[0]
        self.assertEqual(broken.state, seat_resume_all.UNKNOWN)
        self.assertIn("INCOMPLETE register, missing family and project",
                      broken.reason)
        # CONTROL: a name nothing registered is still an unknown seat
        ghost, ghost_why = seat._seat_family("ghost-codex")
        self.assertIsNone(ghost)
        self.assertIn("unknown seat 'ghost-codex'", ghost_why)
        self.assertNotIn("INCOMPLETE", ghost_why)

    def test_every_register_that_exists_and_resolves_nothing_is_named(self):  # noqa: VACUOUS_ASSERTION — the one absence (a foreign seat's record adds no defect) is an exact equality to the grammar's own refusal text, beside an exact clause and path for each broken record and the complete record's resolution
        """The three ways a register can exist and still resolve nothing, each
        with its own clause: unreadable, a family that contradicts the tree it
        sits in, and a missing key. A record naming ANOTHER seat is not this
        seat's register at all, so it adds nothing and the grammar's own "unknown
        seat" text comes back unchanged. CONTROL: the complete record resolves
        and carries no reason."""
        d, _launch = self._mint(seat_name="proj-a-codex", family="codex")
        path = os.path.join(d, "spawn.json")
        grammar = seat._named_seat_family("proj-a-codex")[1]
        complete = {"v": 1, "seat": "proj-a-codex", "family": "codex",
                    "project": "proj-a"}
        seen = {}
        for label, body in (
                ("unreadable", "{not json"),
                ("contradicts", json.dumps(dict(complete,
                                                family=seat.NATIVE_FAMILY))),
                ("no family", json.dumps({k: v for k, v in complete.items()
                                          if k != "family"})),
                ("foreign", json.dumps(dict(complete, seat="proj-b-codex",
                                            family=None))),
                ("complete", json.dumps(complete))):
            with open(path, "w") as fh:
                fh.write(body)
            seen[label] = seat._seat_family("proj-a-codex")
        self.assertEqual(seen["complete"], ("codex", None))
        self.assertEqual(seen["foreign"], (None, grammar))
        self.assertIn("unknown seat", grammar)
        self.assertEqual(
            {label: seen[label][0]
             for label in ("unreadable", "contradicts", "no family")},
            {"unreadable": None, "contradicts": None, "no family": None})
        self.assertIn("%s exists and does not read as a JSON object" % path,
                      seen["unreadable"][1])
        self.assertIn("%s names family %r but sits in the codex seat tree"
                      % (path, seat.NATIVE_FAMILY), seen["contradicts"][1])
        self.assertIn("%s is an INCOMPLETE register, missing family (it sits "
                      "in the codex seat tree)" % path, seen["no family"][1])
        self.assertIn("a broken record, not an unregistered name",
                      seen["no family"][1])

    def test_native_resume_answers_instead_of_raising(self):
        """FINDING 5, native half: the name resolves, so `resume` must have an
        answer for it — and a native seat has no launch.sh to replay. Naming that
        is the cure; reaching FAMILIES['claude'] would have been a KeyError from
        inside a gate whose job is to answer."""
        self._register("proj-a")
        fake = FakeAdapter()
        rc0, out0, err0 = self._spawn(["proj-a-claude"], fake)[:3]
        self.assertEqual(rc0, 0, err0)
        self.assertIn("spawned proj-a-claude via fake", out0)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), \
                mock.patch.object(harness, "detect", return_value=fake):
            rc = seat.cmd_seat(["resume", "proj-a-claude"])
        self.assertEqual(rc, 2)
        self.assertIn("native claude seat", err.getvalue())
        self.assertIn("spawn proj-a-claude --replace", err.getvalue())

    def test_advertised_forms_are_exactly_the_admitted_ones(self):
        """FINDING 9 — the grammar advertised combinations the gate refused.

        Each list is checked against the door that decides it, not against a
        hand-written expectation: every `project_families()` member must be
        admissible as a project instance, every `spawn_families()` member must be
        admissible bare, and the two names the old list promised in vain must
        refuse with a reason naming the FORM.
        """
        self._register("proj-a")
        for family in seat.project_families():
            name = "proj-a-%s" % family
            fam, project, err = seat.spawn_identity(name)
            self.assertIsNone(err, "%s is advertised but refused: %s"
                              % (name, err))
            self.assertEqual((fam, project), (family, "proj-a"))
            gate = seat._seat_spawn_gate(fam, name) if fam == seat.NATIVE_FAMILY \
                else seat._instance_gate(fam, name)
            self.assertIsNone(gate, "%s resolves but its gate refuses: %s"
                              % (name, gate))
        for family in seat.spawn_families():
            fam, project, err = seat.spawn_identity(family)
            self.assertEqual((fam, project, err), (family, None, None))
            self.assertIsNone(seat._instance_gate(family, family),
                              "%s is advertised bare but its gate refuses"
                              % family)
        # ROUND FOUR, finding 8 — THE NUMBERED FORM IS ITS OWN LIST, and this
        # loop is the half the matrix was missing: every family the help calls
        # numberable must pass the gate that decides it, and every family it does
        # NOT must be refused there. Before the split, `spawn_families()` was
        # printed as the numbered list and it holds the whole table, so the help
        # and the `proj-a-kimi` recovery both advertised `kimi-N`.
        numbered = seat.numbered_families()
        self.assertTrue(numbered, "the numbered list must not be empty")
        for family in numbered:
            name = "%s-2" % family
            self.assertEqual(seat.spawn_identity(name), (family, None, None))
            self.assertIsNone(seat._instance_gate(family, name),
                              "%s is advertised as numberable but refused"
                              % name)
        for family in set(seat.spawn_families()) - set(numbered):
            gate = seat._instance_gate(family, "%s-2" % family)
            self.assertIn("mode=proxy", gate or "",
                          "%s-2 is NOT advertised, so its gate must refuse it"
                          % family)
            # and no message may offer the form that gate just refused
            _f, _p, err = seat.spawn_identity("proj-a-%s" % family)
            self.assertNotIn("%s-N" % family, err or "")
        # the two the old list promised and no door accepted
        for name in ("claude", "claude-2"):
            _fam, _project, err = seat.spawn_identity(name)
            self.assertIn("no bare or numbered seat form", err or "")
        _fam, _project, err = seat.spawn_identity("proj-a-kimi")
        self.assertIn("no per-instance proxy", err or "")
        self.assertIn("spawn `kimi`", err or "")
        self.assertNotIn("kimi", seat.project_families())


class ProjectSeatFailureStateTest(ProjectRegistryBase):
    """task/2440 round four — the FAILURE states and the EXISTING fleet.

    Round three's ten findings were all of one shape: each cure was right about
    the case it was written for and silent about the case beside it — a ledger
    that cannot be read, a seat minted before the block existed, a path that is a
    symlink, a hook that runs before the register, a home nobody selected, an
    inherited env, a preview that commits, a suggestion the next gate refuses, a
    verb that denies its own family, and a pane reported UP after it was closed.
    Each arm below drives the shipped producer for one of them.
    """

    def test_a_malformed_endpoint_ledger_refuses_and_rewrites_nothing(self):
        """FINDING 1 — an unreadable ledger read as {}, so the next seat took a
        port the ledger already recorded and then overwrote the record of it.

        CONTROL, in this arm: the SAME allocation on the SAME ledger succeeds
        once the file is restored, taking the NEXT free port — so the refusal is
        the corruption's doing and not a shut door. Blast radius:
        `instance_port_ledger`'s strict read; before it, the two calls after the
        corruption returned 8500 (A's own endpoint) and rewrote the file.
        """
        self._pin_proc()
        self._register("proj-a", "proj-b")
        path = seat._instance_ports_path()
        held, err = seat.allocate_instance_port("proj-a-codex")
        self.assertEqual((held, err), (seat.PROJECT_PORT_BASE, None))
        intact = open(path).read()
        with open(path, "w") as fh:
            fh.write("{not json")
        ledger, why = seat.instance_port_ledger()
        self.assertIsNone(ledger)
        self.assertIn(path, why)
        port, why2 = seat.allocate_instance_port("proj-b-codex")
        self.assertIsNone(port, "a corrupt ledger must not hand out a port")
        self.assertIn(path, why2)
        # the admission gate refuses too, naming the file, BEFORE any mint
        gate = seat._instance_gate("codex", "proj-b-codex")
        self.assertIn(path, gate or "")
        self.assertIn("no provable per-instance proxy endpoint", gate)
        # and A's allocation was not rewritten: the bytes are exactly as left
        self.assertEqual(open(path).read(), "{not json")
        # a ledger whose ENTRIES are malformed is the same refusal one level down
        with open(path, "w") as fh:
            json.dump({"proj-a-codex": str(seat.PROJECT_PORT_BASE)}, fh)
        self.assertIsNone(seat.instance_port_ledger()[0])
        # CONTROL: restored, the same call allocates the NEXT port and A keeps its
        with open(path, "w") as fh:
            fh.write(intact)
        self.assertEqual(seat.allocate_instance_port("proj-b-codex"),
                         (seat.PROJECT_PORT_BASE + 1, None))
        self.assertEqual(seat.instance_port_ledger()[0]["proj-a-codex"], held)

    def test_an_existing_minted_instance_keeps_and_reserves_its_endpoint(self):
        """REFUSING A NAME IS NOT RECONCILING THE FLEET. An instance already
        holding 8500 must not be handed it a second time, and its own health row
        must not format `port %d` of a None.

        The fixture is a REAL instance config on the exact colliding port.
        CONTROL, in this arm: the config is then removed and the very next
        allocation takes 8500 back — so the reservation came from that file and
        not from an unconditional skip. Blast radius: `_minted_block_census` and
        `_existing_instance_port`.
        """
        from helm import seat_health
        from helm.seat_catalog import FAMILIES
        self._pin_proc()
        self._register("proj-a", "proj-b")
        base = FAMILIES["codex"]["port"]
        name = "codex-%d" % (seat.PROJECT_PORT_BASE - base)   # derives 8500
        d = seat._instance_dir("codex", name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "config.yaml"), "w") as fh:
            fh.write("port: %d\n" % seat.PROJECT_PORT_BASE)
        # RECONCILED: the new project seat does not take the minted seat's port
        self.assertEqual(seat.allocate_instance_port("proj-a-codex"),
                         (seat.PROJECT_PORT_BASE + 1, None))
        # the READER still answers an integer for the seat that holds 8500
        self.assertEqual(seat._existing_instance_port("codex", name),
                         seat.PROJECT_PORT_BASE)
        with mock.patch.object(seat, "_running_pid_rec",
                               return_value={"pid": _FAKE_PID}), \
                mock.patch.object(seat, "_port_open", return_value=True), \
                mock.patch.object(seat_health, "upstream_phrase",
                                  return_value=""), \
                mock.patch.object(seat, "proxy_drift",
                                  return_value=(seat.PROXY_CURRENT, None)):
            live = seat_health._proxy_live_text("codex", name)[0]
        self.assertIn("port %d" % seat.PROJECT_PORT_BASE, live)
        # admission still refuses MINTING that name afresh (the credited half)
        self.assertIn("reaches the project-instance block",
                      seat._instance_gate("codex", name))
        # CONTROL: with the config gone the port is free again
        os.unlink(os.path.join(d, "config.yaml"))
        self.assertEqual(seat.allocate_instance_port("proj-b-codex"),
                         (seat.PROJECT_PORT_BASE, None))

    def test_a_live_listener_inside_the_block_is_reserved(self):
        """FINDING 2's other authority: a port something is already SERVING is
        not free, whoever opened it.

        Driven through the shipped LISTEN reader (`seat_ports._listen_inodes`)
        against a fixture /proc. CONTROL, in this arm: with the same fixture
        holding no LISTEN row, the next allocation takes the port that was
        skipped. Blast radius: the `_listen_inodes` call in the allocator's loop.
        """
        from helm import seat_ports
        self._register("proj-a", "proj-b")
        self._pin_proc(seat.PROJECT_PORT_BASE)
        self.assertTrue(seat_ports._listen_inodes(seat.PROJECT_PORT_BASE),
                        "the fixture /proc must actually show the LISTEN row")
        self.assertEqual(seat.allocate_instance_port("proj-a-codex"),
                         (seat.PROJECT_PORT_BASE + 1, None))
        self._pin_proc()
        self.assertEqual(seat.allocate_instance_port("proj-b-codex"),
                         (seat.PROJECT_PORT_BASE, None))

    def test_previews_and_refusals_spend_no_endpoint_slot(self):  # noqa: VACUOUS_ASSERTION — the empty ledger IS the finding's whole observable, and the unconditional positive control is the LAST line: one real allocation must return the block's FIRST port, which is impossible if any preview spent a slot
        """FINDING 7 — the allocator ran from inside the admission gate, which
        `_spawn` calls BEFORE the --print check and before every later refusal,
        so a hundred previews of a hundred project names filled the 100-wide span
        without creating a single seat.

        CONTROL, in this arm: after the hundred previews, one real allocation is
        asserted to get the FIRST endpoint of the block — pre-cure that call had
        no slot left at all. Blast radius: `_instance_gate`'s endpoint predicate,
        now `_instance_endpoint_error` (pure).
        """
        self._pin_proc()
        names = ["p%02d" % index for index in range(seat.PROJECT_PORT_SPAN)]
        self._register(*names)
        for name in names:
            rc, _out, err, _wla, popen = self._spawn(
                ["%s-codex" % name, "--print"], None)
            # refused at the MINT rung, which is PAST the gate — the exact
            # ordering this arm is about
            self.assertEqual(rc, 1, err)
            self.assertIn("helm seat add codex", err)
            popen.assert_not_called()
        self.assertEqual(seat.instance_port_ledger(), ({}, None))
        self.assertFalse(os.path.exists(seat._instance_ports_path()),
                         "no preview may create the ledger at all")
        # CONTROL: the block is untouched, so a real mint gets its first port
        self.assertEqual(seat.allocate_instance_port("p00-codex"),
                         (seat.PROJECT_PORT_BASE, None))

    def test_a_symlink_out_of_the_project_is_refused(self):  # noqa: VACUOUS_ASSERTION — the nothing-spawned assert is the refusal's own observable and stands beside unconditional positives: rc 2, two clauses of the refusal naming the RESOLVED target, then rc 0 and exactly one pane from the same door with --cwd at the project root
        """FINDING 3 — the workspace check compared LEXICAL paths, so a link
        inside registered A pointing at unregistered U was admitted as A while
        `ad.spawn` entered U and the register claimed A.

        CONTROL, in this arm: the same call with --cwd at the project's own root
        returns 0 — and the credited linked-worktree positive
        (`test_registered_linked_worktree_of_the_project_is_admitted`) proves
        canonicalising did not narrow scope to containment. Blast radius:
        `_nearest_existing_dir`'s realpath; without it the refusal never fires.
        """
        from helm.work._lanes import _git
        root = os.path.join(self.tmp, "proj-a")
        os.makedirs(root, exist_ok=True)
        _git(root, "init", "-q")
        unreg = os.path.join(self.tmp, "unreg-u")
        os.makedirs(unreg)
        _git(unreg, "init", "-q")         # a real repo, so tier 2 really runs
        link = os.path.join(root, "link")
        os.symlink(unreg, link)
        self._register("proj-a", paths={"proj-a": root})
        self.seat_home.stop()
        self.seat_home = None
        self._mint(seat_name="proj-a-codex", family="codex")
        fake = FakeAdapter()
        rc, _out, err, _wla = self._spawn_real_git(
            ["proj-a-codex", "--cwd", link], fake)
        self.assertEqual(rc, 2)
        self.assertIn("its workspace is not proj-a's", err)
        self.assertIn(os.path.realpath(unreg), err)
        self.assertEqual(fake.spawned, [])
        # CONTROL: the project's own root, same call, is admitted
        ok = FakeAdapter()
        rc2, out2, err2, _wla2 = self._spawn_real_git(
            ["proj-a-codex", "--cwd", root], ok)
        self.assertEqual(rc2, 0, err2)
        self.assertEqual(len(ok.spawned), 1)

    def test_the_native_register_resolves_before_the_pane_command_runs(self):
        """FINDING 4 — the register was written AFTER the pane existed, so the
        pane's first SessionStart could not resolve the seat's family, skipped
        the binder entirely, and that first session binding was dropped.

        The probe adapter reads the SHIPPED resolver at the instant the pane
        command is created. CONTROL, in this arm: the same resolver is asked for
        an unspawned sibling name and answers (None, None) at the same instant,
        so a green here is not the resolver answering from the name's shape.
        Blast radius: the pre-spawn `_register_spawn` call.
        """
        register_probe = self

        class RegisterProbe(FakeAdapter):
            def spawn(self, command, title=None, cwd=None):
                self.at_spawn = seat.registered_seat_family(title)
                self.ghost_at_spawn = seat.registered_seat_family("ghost-claude")
                return FakeAdapter.spawn(self, command, title, cwd)

        self._register("proj-a")
        fake = RegisterProbe()
        rc, _out, err, _wla, _popen = self._spawn(["proj-a-claude"], fake)
        self.assertEqual(rc, 0, err)
        self.assertEqual(fake.at_spawn, ("claude", "proj-a"),
                         "the pane command was created before its seat had a "
                         "register a SessionStart could resolve")
        self.assertEqual(fake.ghost_at_spawn, (None, None))
        rec = json.load(open(os.path.join(
            seat._instance_dir("claude", "proj-a-claude"), "spawn.json")))
        self.assertEqual(rec["handle"], "pane-1")   # the handle still lands
        register_probe.assertEqual(rec["project"], "proj-a")

    def test_a_native_pane_that_never_starts_leaves_no_register(self):  # noqa: VACUOUS_ASSERTION — the absent register is the finding's observable and the control runs FIRST and unconditionally: the same seat spawned successfully must leave a register, which is then removed by hand before the failing leg
        """FINDING 4's other half: the register now exists BEFORE the pane, so
        every path where the pane fails has to hand it back — and round six's
        half of that: only a pane PROVEN absent may take the register with it.

        `request_absent` is the adapter's own proof that its create process never
        started, the same predicate the proxy leg already asks. CONTROLS, both in
        this arm: the successful spawn of the same seat (asserted first) leaves a
        register, so the absence is the failure's doing; and the SECOND failing
        leg raises an error carrying NO absence proof, where the register must
        SURVIVE as an incomplete attempt — so a cure that simply deleted on every
        exception fails that leg while this one stays green. Blast radius:
        `_settle_spawn_attempt`'s absent branch.
        """
        self._register("proj-a")
        path = os.path.join(seat._instance_dir("claude", "proj-a-claude"),
                            "spawn.json")

        class Refuses(FakeAdapter):
            absent = True

            def spawn(self, command, title=None, cwd=None):
                raise harness.HarnessError("no pane for you",
                                           request_absent=self.absent)

        ok = FakeAdapter()
        self.assertEqual(self._spawn(["proj-a-claude"], ok)[0], 0)
        self.assertTrue(os.path.exists(path))
        os.unlink(path)
        rc, _out, err, _wla, _popen = self._spawn(["proj-a-claude"], Refuses())
        self.assertEqual(rc, 1)
        self.assertIn("pane spawn failed", err)
        self.assertFalse(os.path.exists(path),
                         "a pane PROVEN never to have started must leave no "
                         "register")
        # CONTROL / the pair: the identical failure with no absence proof KEEPS a
        # truthful incomplete record, because a pane may exist behind it
        unproven = Refuses()
        unproven.absent = False
        rc2, _out2, err2, _wla2, _popen2 = self._spawn(
            ["proj-a-claude"], unproven)
        self.assertEqual(rc2, 1)
        self.assertIn("KEPT as an incomplete attempt", err2)
        kept = json.load(open(path))
        self.assertEqual(kept["attempt"]["state"], seat.SPAWN_ATTEMPT_INCOMPLETE)
        self.assertIn("no handle exists to prove the pane absent",
                      kept["attempt"]["reason"])
        self.assertIsNone(kept["session"], "no session may be invented for a "
                                           "pane helm never proved exists")
        self.assertEqual(kept["family"], "claude")   # still nameable by helm

    def test_the_native_spawn_binds_its_session_from_the_pinned_home(self):  # noqa: VACUOUS_ASSERTION — the control's unbound-session assert is the pair to four unconditional positives asserted first from the same call: rc 0, the pinned config_home, the bound session id and the pane key
        """FINDINGS 4 and 5 — the exact-session census read
        `<instance>/claude/sessions`, which is where a PROXY seat's launch.sh
        pins CLAUDE_CONFIG_DIR. A native seat writes into the home its own launch
        selected, so the census listed an empty directory and read zero live
        processes as proof of death; nothing pinned that home either.

        CONTROL, in this arm: the identical spawn with the session record placed
        under the INSTANCE directory instead leaves the session unbound and says
        so — so the green half proves the census read the PINNED home, not that
        it reads any home it is pointed at. Blast radius:
        `_session_record_root` plus the record's `config_home`.
        """
        self._register("proj-a")
        native_home = self._native_home("session-live")
        fields = {"handle": "pane-1", "pane_key": "tab:leaf",
                  "pty_id": "pty-1", "worktree_id": "workspace:/w"}
        fake = FakeOrcaAdapter(rows=[{"handle": "pane-1"}])
        with mock.patch.object(seat, "_prove_orca_replacement",
                               return_value=({}, fields, None)):
            rc, _out, err, _wla, _popen = self._spawn(["proj-a-claude"], fake)
        self.assertEqual(rc, 0, err)
        d = seat._instance_dir("claude", "proj-a-claude")
        rec = json.load(open(os.path.join(d, "spawn.json")))
        self.assertEqual(rec["config_home"], native_home)
        self.assertEqual(rec["session"], "session-live")
        self.assertEqual(rec["pane_key"], "tab:leaf")
        # CONTROL: the same record, the same adapter, the session file in the
        # OLD place — the census must find nothing and the spawn must say so
        shutil.rmtree(os.path.join(native_home, "sessions"))
        os.makedirs(os.path.join(d, "claude", "sessions"), exist_ok=True)
        with open(os.path.join(d, "claude", "sessions", "9.json"), "w") as fh:
            json.dump({"sessionId": "session-elsewhere"}, fh)
        with open(os.path.join(d, "spawn.json"), "w") as fh:
            json.dump(dict(rec, session=None), fh)
        with mock.patch.object(seat, "_prove_orca_replacement",
                               return_value=({}, fields, None)):
            self.assertFalse(seat._backfill_spawn_session(
                "proj-a-claude", d, fake))
        self.assertIsNone(json.load(open(os.path.join(d, "spawn.json")))
                          ["session"])

    def test_native_up_and_down_answer_instead_of_denying_the_family(self):  # noqa: VACUOUS_ASSERTION — the never-says-unknown-family assert sits beside three unconditional positives per verb (rc 2 and two clauses of the shipped sentence) and a final positive control: the bare family name still gets the unknown-family refusal
        """FINDING 9 — the synopsis promises a project seat round-trips into
        up/down, and both verbs called its family unknown: they route to the
        proxy legs, which index the proxy table, and native claude is not in it.

        CONTROL, in this arm: a bare `claude`, which is NOT a seat at all, still
        gets the unknown-family refusal from the same door — so the new sentence
        is keyed on the REGISTER and not on the word claude. Blast radius: the
        up/down branch in `cmd_seat`.
        """
        self._register("proj-a")
        fake = FakeAdapter()
        self.assertEqual(self._spawn(["proj-a-claude"], fake)[0], 0)
        for verb in ("up", "down"):
            err = io.StringIO()
            with contextlib.redirect_stderr(err), \
                    mock.patch.object(harness, "detect", return_value=fake):
                rc = seat.cmd_seat([verb, "proj-a-claude"])
            said = err.getvalue()
            self.assertEqual(rc, 2, said)
            self.assertIn("registered NATIVE claude seat", said)
            self.assertIn("runs no proxy", said)
            self.assertNotIn("unknown family", said)
            self.assertIn("seat spawn proj-a-claude --replace", said)
        # CONTROL: the bare family name is not a seat and says so
        err2 = io.StringIO()
        with contextlib.redirect_stderr(err2):
            self.assertEqual(seat.cmd_seat(["up", "claude"]), 1)
        self.assertIn("unknown family 'claude'", err2.getvalue())

    def test_a_failed_native_register_reports_the_proven_pane_state(self):  # noqa: VACUOUS_ASSERTION — the never-says-UP assert is the finding itself; each loop leg asserts an EXACT state word (CLOSED vs UNKNOWN) unconditionally, plus rc 1, the pane named in the sentence and the stopped handle, and the two legs are each other's control
        """FINDING 10 — after `stop_pane` had returned PROOF the pane was closed,
        the message said the pane was UP, sending an operator to hunt a pane that
        no longer exists (or to leave one they believe is running).

        Both halves in one arm, and the difference is the PROOF, not the code
        path: a non-orca adapter whose inventory drops the handle proves closure,
        while an orca adapter with no exact-process census cannot — so one says
        CLOSED and the other UNKNOWN, and neither says UP.

        ROUND SIX MOVED THE SECOND HALF'S STATE ASSERT. Narrating the difference
        was only half the cure: both outcomes went to the same unconditional
        restore, which for a fresh project seat DELETES its only record of family,
        project, pane and ownership — while the pane may still be running. So
        CLOSED still leaves nothing, and UNKNOWN now leaves a truthful INCOMPLETE
        record with no session in it. The two legs remain each other's control,
        and the pair is what makes the distinction visible at all: a cure that
        kept the record on BOTH would fail the CLOSED leg. Blast radius:
        `_pane_state_after_close` and `_settle_spawn_attempt`.

        The register mock DELEGATES its first call to the shipped writer, so the
        publication really lands on disk and only the finalize fails — without
        that, the record the UNKNOWN leg is about would never have been written by
        anything.
        """
        self._register("proj-a")
        path = os.path.join(seat._instance_dir("claude", "proj-a-claude"),
                            "spawn.json")
        real_register = seat._register_spawn
        for adapter, expect, survives in ((FakeAdapter(), "CLOSED", False),
                                          (FakeOrcaAdapter(), "UNKNOWN", True)):
            with self.subTest(adapter=adapter.name):
                calls = []

                def publish_then_fail(storage_seat, identity, d, rec):
                    calls.append(rec)
                    return len(calls) == 1 and real_register(
                        storage_seat, identity, d, rec)

                with mock.patch.object(seat, "_register_spawn",
                                       publish_then_fail):
                    rc, _out, err, _wla, _popen = self._spawn(
                        ["proj-a-claude"], adapter)
                self.assertEqual(rc, 1)
                self.assertIn("could not record pane pane-1", err)
                self.assertIn(expect, err)
                self.assertNotIn("is UP", err)
                self.assertEqual(adapter.stopped, ["pane-1"])
                self.assertEqual(len(calls), 2, "the publication and the "
                                                "finalize are two writes")
                self.assertEqual(os.path.exists(path), survives)
                if survives:
                    kept = json.load(open(path))
                    self.assertEqual(kept["attempt"]["state"],
                                     seat.SPAWN_ATTEMPT_INCOMPLETE)
                    self.assertIn("UNKNOWN", kept["attempt"]["reason"])
                    self.assertIsNone(kept["session"])
                    os.unlink(path)


class LaunchEndpointTest(ProjectRegistryBase):
    """task/2440 round five — WHICH SEATS HAVE AN ALLOCATION, and what a launch
    asset renders for the ones that do not.

    The project block gave the project-canonical form an ALLOCATED endpoint. It
    did not give every non-numbered spelling one: `codex-spark` names a model
    window on the family endpoint and `seat-b` names an identity on it, and both
    have shared the family base since this module existed. Routing every
    non-numbered name to the ledger answered None for names no entry can exist
    for, and the launch line renders its endpoint with `%d`.
    """

    def test_a_seat_with_no_allocation_still_renders_its_family_port(self):  # noqa: VACUOUS_ASSERTION — every leg asserts an EXACT rendered endpoint string on a line the shipped producer built, and the project-form leg asserts the opposite classification on the same predicate
        """POSITIVE, through the SHIPPED PRODUCER: `launch_line` is called on the
        seat names trunk already calls it with, and the endpoint it writes is the
        family base.

        CONTROL, the discriminator: `_is_project_instance` must answer False for
        these three names and True for `proj-a-codex` on the SAME predicate. An
        accessor that classified everything as non-project would pass every
        assert above this line and re-alias the project form onto 8317, which is
        the state the block exists to end. Blast radius: the classification of
        one seat name; nothing is written and no ledger is read for the three
        non-project names.
        """
        base = seat.FAMILIES["codex"]["port"]
        want = "ANTHROPIC_BASE_URL=http://127.0.0.1:%d" % base
        for kwargs in ({"seat": "seat-b"},
                       {"seat": "seat-b", "multi": True},
                       {"seat": "codex-spark", "model": "gpt-5.3-codex-spark"}):
            with self.subTest(**kwargs):
                self.assertIn(want, seat.launch_line("codex", **kwargs).split())
                self.assertEqual(seat._launch_endpoint("codex",
                                                       kwargs["seat"]), base)
        # CONTROL: the same predicate separates these names from the project form
        for name in ("seat-b", "codex-spark", "codex", "codex-2"):  # noqa: SEAT_NAME — the numbered and model-window forms ARE this control's subject: the predicate must separate exactly them from the project form
            self.assertFalse(seat_paths._is_project_instance("codex", name), name)
        self.assertTrue(seat_paths._is_project_instance("codex", "proj-a-codex"))

    def test_an_unallocated_project_seat_writes_no_launch_asset(self):
        """NEGATIVE, on an otherwise-valid input: the same `launch_line` call,
        the same family, the same registry — only the seat's FORM differs, so the
        refusal can only come from the allocation gate under test.

        It refuses rather than substituting the base port: 8317 in an unallocated
        project seat's launch line is the alias the block exists to end.

        CONTROL, in this arm: the SAME name renders its ALLOCATED endpoint once
        `allocate_instance_port` has committed one, so the refusal is the missing
        allocation's doing and not the name's. Blast radius: one ledger entry
        under the temp HELM_HOME.
        """
        self._pin_proc()
        self._register("proj-a")
        ledger = seat._instance_ports_path()
        with self.assertRaises(ValueError) as caught:
            seat.launch_line("codex", seat="proj-a-codex")
        self.assertIn("proj-a-codex", str(caught.exception))
        self.assertIn(ledger, str(caught.exception))
        self.assertIn("no allocated endpoint", str(caught.exception))
        # CONTROL: committed, the same call renders that exact allocated port
        port, err = seat.allocate_instance_port("proj-a-codex")
        self.assertEqual((port, err), (seat.PROJECT_PORT_BASE, None))
        self.assertIn("ANTHROPIC_BASE_URL=http://127.0.0.1:%d" % port,
                      seat.launch_line("codex", seat="proj-a-codex").split())

    def test_every_endpoint_a_launch_asset_renders_is_a_number(self):
        """THE ACCESSOR'S WHOLE CONTRACT, stated as the property the `%d` sites
        need: `_launch_endpoint` returns an int or raises a sentence naming the
        seat. It never returns None, which is the value `%d` cannot format.

        The three None states are each driven: a family outside the proxy table,
        a numbered suffix whose derived port reaches the project block, and an
        unallocated project instance. CONTROL: the four spellings that DO have an
        endpoint return it, so an accessor that raised unconditionally fails
        here. Blast radius: pure reads of the FAMILIES table and the (absent)
        ledger.
        """
        base = seat.FAMILIES["codex"]["port"]
        reach = seat.PROJECT_PORT_BASE - base          # codex-183 derives 8500
        # THE UNCONDITIONAL POSITIVE: the answer for the seat every family has
        # is an unprivileged TCP port, which is the whole promise — a number a
        # launch asset can render and a proxy can bind without privilege.
        self.assertGreater(seat._launch_endpoint("codex", "codex"), 1024)
        self.assertEqual(seat._launch_endpoint("codex", "codex"), base)
        for family, name, expect in (
                ("codex", "codex", base),
                ("codex", "codex-2", base + 2),  # noqa: SEAT_NAME — base+N for the numbered form is this leg's subject
                ("codex", "seat-b", base),
                ("codex", "codex-spark", base)):
            with self.subTest(seat=name):
                self.assertEqual(seat._launch_endpoint(family, name), expect)
        for family, name, phrase in (
                (seat.NATIVE_FAMILY, "proj-a-claude", "outside the proxy table"),
                ("codex", "codex-%d" % reach, "project-instance block"),
                ("codex", "proj-a-codex", "no allocated endpoint")):
            with self.subTest(seat=name):
                with self.assertRaises(ValueError) as caught:
                    seat._launch_endpoint(family, name)
                self.assertIn(name, str(caught.exception))
                self.assertIn(phrase, str(caught.exception))
        # THE BOUNDARY, pinned from below so the refusal above is exactly at it:
        # the suffix one lower derives the port just under the block, which is
        # what makes `reach` the FIRST suffix with no endpoint of its own. An arm
        # whose fixture drifted off the boundary fails here rather than passing
        # over a refusal it no longer provokes.
        self.assertEqual(seat._instance_port("codex", "codex-%d" % (reach - 1)),
                         seat.PROJECT_PORT_BASE - 1)

    def test_the_launch_verb_refuses_an_instance_with_no_endpoint(self):
        """`seat launch` is the SECOND door that mints assets carrying an
        endpoint, and it had no endpoint admission at all: `-i 183` derives 8500,
        which the allocation refuses, so the name reached the renderer with
        nothing to render.

        Asked of the PURE predicate, so a preview and a re-mint spend no durable
        slot. CONTROL, same verb, same fixture, one digit different: `-i 2` runs
        to rc 0 and renders base+2, and this gate does not speak for it — an
        unconditional refusal fails there. Blast radius: the launch verb's
        admission; both legs write only under the temp HELM_HOME.
        """
        base = seat.FAMILIES["codex"]["port"]
        family_dir = seat.seat_dir("codex")
        os.makedirs(family_dir, exist_ok=True)
        with open(os.path.join(family_dir, "config.yaml"), "w") as fh:
            fh.write("port: %d\n" % base)
        reach = seat.PROJECT_PORT_BASE - base
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(["launch", "codex", "-i", str(reach)])
        self.assertEqual(rc, 2)
        self.assertIn("project-instance block", err.getvalue())
        self.assertEqual(out.getvalue().strip(), "")
        self.assertFalse(os.path.exists(os.path.join(
            seat._instance_dir("codex", "codex-%d" % reach), "launch.sh")))
        # CONTROL: instance 2 is admitted and renders its own derived endpoint
        out2, err2 = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out2), contextlib.redirect_stderr(err2):
            rc2 = seat.cmd_seat(["launch", "codex", "-i", "2"])
        self.assertEqual(rc2, 0)
        self.assertNotIn("project-instance block", err2.getvalue())
        self.assertIn("ANTHROPIC_BASE_URL=http://127.0.0.1:%d" % (base + 2),
                      out2.getvalue().split())

    def test_seat_up_resolves_an_existing_instances_endpoint_or_says_so(self):
        """`seat up` renders `port %d` for a LIVE proxy, so it asks the READER's
        question: this instance's own config exists, and the port it names is the
        endpoint a proxy would be started on. A None there formatted into `%d`
        took the verb down with a message about string formatting.

        Both legs also check the RECOVERY the refusal offers, because the last
        segment of a project-canonical name is the family rather than a number:
        `launch codex -i codex` is a phrase no door admits.

        CONTROL: the same verb on the same fixture with a port line in the config
        does NOT print the unresolvable-endpoint sentence — it refuses one rung
        later, naming config regeneration, so the sentence is about the missing
        endpoint and not about the seat. Blast radius: `_up`'s endpoint
        resolution; neither leg starts a proxy and both write only under the temp
        HELM_HOME.
        """
        self._register("proj-a")
        family_dir = seat.seat_dir("codex")
        os.makedirs(family_dir, exist_ok=True)
        with open(os.path.join(family_dir, "config.yaml"), "w") as fh:
            fh.write("port: %d\n" % seat.FAMILIES["codex"]["port"])
        inst = seat._instance_dir("codex", "proj-a-codex")
        os.makedirs(inst, exist_ok=True)
        cfg = os.path.join(inst, "config.yaml")
        with open(cfg, "w") as fh:
            fh.write("api-keys:\n  - abc\n")          # a config naming no port
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(seat._up("codex", seat="proj-a-codex"), 1)
        said = err.getvalue()
        self.assertIn("cannot resolve its endpoint", said)
        self.assertIn(cfg, said)
        self.assertIn("helm seat spawn proj-a-codex", said)
        self.assertNotIn("-i codex", said)
        # CONTROL: a config naming a port resolves, and refuses one rung later
        with open(cfg, "w") as fh:
            fh.write("port: %d\napi-keys:\n  - abc\n" % seat.PROJECT_PORT_BASE)
        self.assertEqual(seat._existing_instance_port("codex", "proj-a-codex"),
                         seat.PROJECT_PORT_BASE)
        err2 = io.StringIO()
        with contextlib.redirect_stderr(err2):
            self.assertEqual(seat._up("codex", seat="proj-a-codex"), 1)
        self.assertNotIn("cannot resolve its endpoint", err2.getvalue())
        self.assertIn("config regeneration failed", err2.getvalue())

    def test_a_full_block_refuses_only_the_seats_it_allocates_for(self):
        """THE SIBLING OF THE SAME CONFLATION, one door over: admission also
        sent every non-numbered name to the ledger, so a FULL project block
        refused `codex-spark` — a seat whose endpoint is the family base and is
        not in the block at all.

        CONTROL, in this arm: before the block is filled BOTH names are
        admitted, so the project form's refusal is the fullness and
        `codex-spark`'s admission is not an unconditional None. Blast radius:
        `_instance_endpoint_error`; one ledger file under the temp HELM_HOME.
        """
        self._pin_proc()
        self._register("proj-a")
        # CONTROL: an empty block admits both
        self.assertIsNone(seat._instance_endpoint_error("codex", "codex-spark"))
        self.assertIsNone(seat._instance_endpoint_error("codex", "proj-a-codex"))
        full = {"filler-%d-codex" % n: seat.PROJECT_PORT_BASE + n
                for n in range(seat.PROJECT_PORT_SPAN)}
        # the seats root exists only once something has minted into it, and the
        # two admission reads above deliberately create nothing
        os.makedirs(os.path.dirname(seat._instance_ports_path()), exist_ok=True)
        with open(seat._instance_ports_path(), "w") as fh:
            json.dump(full, fh)
        taken = seat._instance_endpoint_error("codex", "proj-a-codex")
        self.assertIn("fully taken", taken or "")
        self.assertIsNone(seat._instance_endpoint_error("codex", "codex-spark"),
                          "a full project block says nothing about a seat whose "
                          "endpoint is the family base")
        self.assertEqual(seat._launch_endpoint("codex", "codex-spark"),
                         seat.FAMILIES["codex"]["port"])



class EndpointLedgerAuthorityTest(ProjectRegistryBase):
    """task/2440 round six, DOOR A — ONE module owns the instance-port ledger and
    its census, and every reading it cannot complete is a REFUSAL.

    Round five's cure was right about the ledger it could not PARSE and silent
    about every other way the same question goes unanswered: a file holding
    `null`, a table contradicting itself, and three censuses whose unreadable
    sources all rendered as successful empty ones. Each arm below drives the
    shipped reader or the shipped allocator for one of them.
    """

    def _ledger(self, obj=None, raw=None):
        """Write the ledger's bytes directly — this is the FILE the readers under
        test are about, and the states they must refuse are ones no writer of
        helm's would produce."""
        path = seat._instance_ports_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(raw if raw is not None else json.dumps(obj))
        return path

    def test_an_invalid_allocation_table_refuses_and_keeps_its_bytes(self):  # noqa: VACUOUS_ASSERTION — the absence asserts are the refusals' own observable (no table, no ledger file) and every one sits beside unconditional positives from the same call: the refusal text naming the file and the entry, the bytes read back and compared to a value captured in this arm, and two closing controls that ALLOCATE a real endpoint
        """FINDING 1 — `null` read as a fresh fleet, and a table that contradicts
        itself read as an allocation.

        Three refusals, each naming the FILE and the ENTRY: a JSON `null` (the
        sentinel case — it arrived as the same None a missing file does), two
        seats mapped to ONE endpoint, and an entry outside the block that names a
        family's own port. Each is checked at the reader AND through the
        allocator, and the original bytes are read back afterwards.

        CONTROLS, in this arm: a VALID populated mapping is asserted to read back
        unchanged and to keep serving its own seat's recorded port, and a truly
        ABSENT table is asserted to permit the first allocation — so the refusals
        are the corruption's doing and not a shut door. Blast radius:
        `instance_port_ledger`'s validation; before it, the null case returned
        ({}, None) and the duplicate/out-of-block cases returned the table.
        """
        self._pin_proc()
        self._register("proj-a", "proj-b")
        base, top = seat.PROJECT_PORT_BASE, (seat.PROJECT_PORT_BASE
                                             + seat.PROJECT_PORT_SPAN - 1)
        cases = (
            ("null", None, "null", "not an allocation table"),
            ("duplicate", {"proj-a-codex": base, "proj-b-codex": base}, None,
             "to BOTH proj-a-codex and proj-b-codex"),
            ("out-of-block", {"proj-a-codex": 8317}, None,
             "OUTSIDE the project-instance block %d-%d" % (base, top)),
        )
        for label, obj, raw, clause in cases:
            with self.subTest(case=label):
                path = self._ledger(obj, raw=raw)
                intact = open(path).read()
                table, why = seat.instance_port_ledger()
                self.assertIsNone(table, "an invalid table is never usable")
                self.assertIn(path, why)
                self.assertIn(clause, why)
                # the ALLOCATOR refuses on the same reading, and writes nothing
                port, alloc_why = seat.allocate_instance_port("proj-b-codex")
                self.assertIsNone(port)
                self.assertIn(path, alloc_why)
                # and the admission gate refuses before any mint
                self.assertIn("no provable per-instance proxy endpoint",
                              seat._instance_gate("codex", "proj-b-codex") or "")
                self.assertEqual(open(path).read(), intact,
                                 "a refusal must preserve the bytes a human has "
                                 "to read")
        # CONTROL 1: a VALID populated mapping is stable and keeps serving
        path = self._ledger({"proj-a-codex": base, "proj-b-codex": top})
        self.assertEqual(seat.instance_port_ledger(),
                         ({"proj-a-codex": base, "proj-b-codex": top}, None))
        self.assertEqual(seat.allocate_instance_port("proj-a-codex"),
                         (base, None))
        self.assertEqual(seat._instance_port("codex", "proj-b-codex"), top)
        self.assertIsNone(seat._instance_gate("codex", "proj-a-codex"))
        # CONTROL 2: a truly ABSENT table is fresh, and the first allocation lands
        os.unlink(path)
        self.assertEqual(seat.instance_port_ledger(), ({}, None))
        self.assertEqual(seat.allocate_instance_port("proj-a-codex"),
                         (base, None))

    def test_an_unavailable_census_refuses_instead_of_reading_as_free(self):
        """FINDING 2 — three censuses whose unreadable sources each rendered as a
        successful EMPTY one, so the allocator persisted a candidate as free on
        evidence it never saw.

        The three sources are made unavailable INDEPENDENTLY: an instances
        directory helm cannot list, an instance config helm cannot open, and a
        /proc with no TCP table at all. Each must refuse and leave no ledger.

        CONTROLS, in this arm: the readable-EMPTY pair (a listable directory, a
        readable /proc with no LISTEN row) allocates the block's first endpoint,
        and the readable-HELD pair (a config naming 8500) reserves it and hands
        out the next — asserted at the end, from the same sources. Blast radius:
        `_minted_block_census` and `seat_ports.listen_census`; before them each of
        the three unavailable readings returned an empty success.
        """
        from helm import seat_ports
        self._register("proj-a", "proj-b")
        base = seat.PROJECT_PORT_BASE
        ledger = seat._instance_ports_path()
        instances = os.path.join(seat.seat_dir("codex"), "instances")
        held = seat._proxy_home("codex", "seat-a-codex")
        os.makedirs(held, exist_ok=True)
        with open(os.path.join(held, "config.yaml"), "w") as fh:
            fh.write("port: %d\n" % base)

        def refused(tag):
            port, why = seat.allocate_instance_port("proj-a-codex")
            self.assertIsNone(port, "%s must not hand out an endpoint" % tag)
            self.assertIn("UNKNOWN", why)
            self.assertFalse(os.path.exists(ledger),
                             "%s must write no ledger" % tag)
            return why

        # 1. the LISTEN census: a /proc root with neither table
        blind = os.path.join(self.tmp, "blind-proc")
        os.makedirs(os.path.join(blind, "net"), exist_ok=True)
        os.environ["HELM_PROC"] = blind
        self.assertEqual(seat_ports._listen_inodes(base, blind), [],
                         "the LENIENT reader still answers empty — which is "
                         "exactly why the allocator needs the other one")
        self.assertIn("cannot say what is listening", refused("a blind /proc"))
        # 2. an instance config helm cannot open
        self._pin_proc()
        os.chmod(os.path.join(held, "config.yaml"), 0)
        try:
            self.assertIn("config.yaml", refused("an unreadable config"))
        finally:
            os.chmod(os.path.join(held, "config.yaml"), 0o600)
        # 3. an instances directory helm cannot list
        os.chmod(instances, 0)
        try:
            self.assertIn(instances, refused("an unlistable directory"))
        finally:
            os.chmod(instances, 0o700)
        # CONTROL, readable-HELD: 8500 is reserved by that config, so the
        # allocation lands on the next endpoint
        self.assertEqual(seat.allocate_instance_port("proj-a-codex"),
                         (base + 1, None))
        # CONTROL, readable-EMPTY: with the config gone, 8500 is free again
        os.unlink(os.path.join(held, "config.yaml"))
        self.assertEqual(seat.allocate_instance_port("proj-b-codex"),
                         (base, None))
        self.assertEqual(seat_ports.listen_census(base), ([], None))

    def test_existing_endpoint_maintenance_finishes_without_admission(self):  # noqa: VACUOUS_ASSERTION — the empty drift list is the surface the finding is about and it stands beside unconditional positives asserted first for BOTH seats: the exact maintenance endpoint 8500, the `port: 8500` line in the plan the shipped generator produced, and pi's resolved port
        """FINDING 3 — `seat up` RESOLVED an existing high-number endpoint and
        then could not regenerate that instance's config, because the generator
        asked the NEW-ADMISSION door about a seat admission must refuse. The seat
        could not be restarted or reconciled at all.

        The fixture is the shipped minter's OWN config bytes on the colliding
        endpoint: `proj-a-codex` is allocated 8500 and minted, then its ledger
        entry is removed — a seat with a valid config and no allocation, which is
        exactly the state a pre-block `codex-183` is in. The same bytes are then
        placed as codex-183's config, the numbered half of the finding.

        CONTROLS, in this arm: `_launch_endpoint` is asserted to STILL raise for
        both names and `_instance_gate` to still refuse minting codex-183 afresh,
        so the cure did not reopen new admission; and `_config_drift_lines` is
        asserted empty, which is the surface that reported these seats as
        unreadable. Blast radius: `proxy_config_plan`'s endpoint call.
        """
        import shutil
        from helm import pi, seat_health
        self._pin_proc()
        self._register("proj-a")
        base = seat.PROJECT_PORT_BASE
        self.assertEqual(seat.allocate_instance_port("proj-a-codex"),
                         (base, None))
        home = seat._mint_instance_proxy("codex", "proj-a-codex")
        cfg = os.path.join(home, "config.yaml")
        self.assertIn("port: %d" % base, open(cfg).read(),
                      "the fixture must be the MINTER's own bytes")
        os.unlink(seat._instance_ports_path())     # the allocation is gone
        # pi resolves a PROJECT seat's family from its own register, so the seat
        # has to be registered for that consumer to be asked about it at all
        with open(os.path.join(seat._instance_dir("codex", "proj-a-codex"),
                               "spawn.json"), "w") as fh:
            json.dump({"v": 1, "seat": "proj-a-codex", "family": "codex",
                       "project": "proj-a", "harness": "headless"}, fh)
        numbered = "codex-%d" % (base - seat.FAMILIES["codex"]["port"])
        other = seat._proxy_home("codex", numbered)
        os.makedirs(other, exist_ok=True)
        shutil.copy(cfg, os.path.join(other, "config.yaml"))
        for name, path in (("proj-a-codex", cfg),
                           (numbered, os.path.join(other, "config.yaml"))):
            with self.subTest(seat=name):
                # admission refuses, the reader answers, maintenance COMPLETES
                self.assertIsNone(seat._instance_port("codex", name))
                with self.assertRaises(ValueError):
                    seat._launch_endpoint("codex", name)
                self.assertEqual(seat._maintenance_endpoint("codex", name), base)
                plan = seat.proxy_config_plan(path, "codex", name)
                self.assertIn("port: %d" % base, plan["text"])
                self.assertFalse(plan["changed"], "the minted bytes ARE the "
                                                  "desired state")
                self.assertEqual(pi.seat_port(name), (base, None))
        # the drift surface that reported these seats unreadable is clean
        self.assertEqual(seat_health._config_drift_lines(), [])
        # CONTROL: minting the numbered name AFRESH is still refused
        self.assertIn("reaches the project-instance block",
                      seat._instance_gate("codex", numbered) or "")

    def test_an_unresolved_endpoint_is_an_unknown_row_not_a_traceback(self):  # noqa: VACUOUS_ASSERTION — the two assertNotIn calls are the CONTROL half of matched pairs whose positive halves are unconditional in the same arm: the exact UNKNOWN state word, the ledger path in the detail, the stale-pidfile sentence, the exact `grep :8500` recovery and the exact pid in both live-text legs
        """FINDING 3's consumer half — `_ensure_row` and `_down` formatted the
        reader's honest None with `%d`, so ONE unresolvable seat raised a
        TypeError about string formatting: in the supervise sweep that takes the
        verdict for every OTHER seat down with it.

        CONTROLS, in this arm: the same two producers are driven against a seat
        whose config DOES name a port, where the row is not unknown-for-this-
        reason and the `_down` sentence carries the exact `ss` command — so the
        UNKNOWN is the missing endpoint and not an unconditional refusal. Blast
        radius: the None guard in `_ensure_row` and `_existing_instance_port` in
        `_down`.
        """
        from helm import seat_health
        self._pin_proc()
        self._register("proj-a")
        base = seat.PROJECT_PORT_BASE
        family_dir = seat.seat_dir("codex")
        os.makedirs(family_dir, exist_ok=True)
        with open(os.path.join(family_dir, "config.yaml"), "w") as fh:
            fh.write("port: %d\n" % seat.FAMILIES["codex"]["port"])
        for name, port in (("proj-a-codex", None), ("proj-b-codex", base)):
            home = seat._proxy_home("codex", name)
            os.makedirs(home, exist_ok=True)
            with open(os.path.join(home, "config.yaml"), "w") as fh:
                fh.write("api-keys:\n  - abc\n" if port is None
                         else "port: %d\napi-keys:\n  - abc\n" % port)
            # a LIVE pid whose birth identity does not match: the stale-record
            # branch, written in the pidfile's own two-field format
            with open(os.path.join(home, "proxy.pid"), "w") as fh:
                fh.write("%d proc:not-this-process\n" % os.getpid())
        label, state, detail = seat_health._ensure_row("codex", "proj-a-codex")
        self.assertEqual((label, state), ("proj-a-codex", "unknown"))
        self.assertIn("endpoint unresolved", detail)
        self.assertIn(seat._instance_ports_path(), detail)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(seat._down("codex", seat="proj-a-codex"), 0)
        said = out.getvalue() + err.getvalue()
        self.assertIn("pidfile stale", said)
        self.assertIn("cannot resolve this instance's endpoint", said)
        # CONTROL: the seat whose config names a port renders that port
        out2, err2 = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out2), contextlib.redirect_stderr(err2):
            self.assertEqual(seat._down("codex", seat="proj-b-codex"), 0)
        said2 = out2.getvalue() + err2.getvalue()
        self.assertIn("grep :%d" % base, said2)
        self.assertNotIn("cannot resolve this instance's endpoint", said2)
        row = seat_health._ensure_row("codex", "proj-b-codex")
        self.assertNotIn("endpoint unresolved", row[2])
        # AND THE SIBLING RENDERER, one function over: `_proxy_live_text` asks
        # the same reader the same question and formatted the same None with `%d`
        for name, expect in (("proj-a-codex", "port UNRESOLVED"),
                             ("proj-b-codex", "port %d" % base)):
            with self.subTest(live=name), \
                    mock.patch.object(seat, "_running_pid_rec",
                                      return_value={"pid": _FAKE_PID}), \
                    mock.patch.object(seat_health, "upstream_phrase",
                                      return_value=""), \
                    mock.patch.object(seat, "proxy_drift",
                                      return_value=(seat.PROXY_CURRENT, None)):
                live = seat_health._proxy_live_text("codex", name)[0]
            self.assertIn("pid %d" % _FAKE_PID, live)
            self.assertIn(expect, live)

    def test_a_refused_resume_spends_no_endpoint_slot(self):  # noqa: VACUOUS_ASSERTION — the absent ledger IS the finding's whole observable, and the unconditional positive control is the closing pair: the same seat with a valid record must reach the allocation and the ledger must then hold exactly {proj-a-codex: 8500}, twice
        """FINDING 9 — resume allocated ABOVE its own role, model and session
        gates, so a registered project seat with a malformed persisted role
        returned 2 without minting or launching and still consumed a durable slot
        out of a span nothing ever reclaims.

        Three refusals, each from a DIFFERENT gate below the old allocation point,
        each asserted to leave the ledger absent. CONTROL, at the end: the same
        seat with a valid record resumes past every gate, and the ledger then
        holds exactly ONE entry — asserted twice, because a resume must allocate
        once and never again. Blast radius: the position of
        `_ensure_instance_endpoint` in `_resume`.
        """
        self._pin_proc()
        self._register("proj-a")
        d, _launch = self._mint(seat_name="proj-a-codex", family="codex")
        ledger = seat._instance_ports_path()
        record = {"v": 1, "seat": "proj-a-codex", "family": "codex",
                  "project": "proj-a", "harness": "headless", "role": "worker"}

        def resume(rec, args=()):
            if os.path.exists(ledger):
                os.unlink(ledger)
            with open(os.path.join(d, "spawn.json"), "w") as fh:
                json.dump(rec, fh)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                rc = seat.cmd_seat(["resume", "proj-a-codex"] + list(args))
            return rc, err.getvalue()

        for label, rec, args, clause in (
                ("role", dict(record, role="captain"), (),
                 "recorded spawn role is invalid"),
                ("model", dict(record, model="claude-fable-5-1"), (),
                 "is a subagent alias on this family"),
                ("session", record, ("--session", "not-a-uuid"),
                 "not a session id shape")):
            with self.subTest(gate=label):
                rc, said = resume(rec, args)
                self.assertEqual(rc, 2, said)
                self.assertIn(clause, said)
                self.assertFalse(os.path.exists(ledger),
                                 "a deterministic refusal must spend no slot")
        # CONTROL: a valid record reaches the allocation, exactly once
        rc, said = resume(record)
        self.assertNotEqual(rc, 2, said)
        self.assertEqual(seat.instance_port_ledger(),
                         ({"proj-a-codex": seat.PROJECT_PORT_BASE}, None))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            seat.cmd_seat(["resume", "proj-a-codex"])
        self.assertEqual(seat.instance_port_ledger(),
                         ({"proj-a-codex": seat.PROJECT_PORT_BASE}, None),
                         "a second resume re-reads its allocation, never takes "
                         "a second one")


class FakeHerdrAdapter(FakeAdapter):
    name, path = "herdr", "/bin/herdr"


class LaunchIdentityAndLifecycleOrderTest(ProjectRegistryBase):
    """task/2440 round six, DOORS B and C — the launch identity a spawn PRODUCES
    and the order in which it becomes durable.

    Door B: one accessor produces the native config home and feeds BOTH the
    record and the child's launch env; the first hook of either project-proxy leg
    resolves its family from a register published BEFORE the child command runs;
    and the non-Orca warning states only the binding helm can actually perform.
    Door C: the lock-owned transition is SHORTENED — an honest PENDING identity
    under the lock, the lock released while the child starts, then the same
    attempt finalized — and a rollback restores authority only on PROVEN absence.
    """

    def _lock_probe(self, d):
        """Whether this seat's lifecycle lock can be taken RIGHT NOW.

        `flock` is held per open file description, so a fresh descriptor in this
        same process contends exactly as the child's hook process would — which
        is what makes this a real reading of the window the hook has to bind in.
        """
        import fcntl
        os.makedirs(d, mode=0o700, exist_ok=True)
        fd = os.open(os.path.join(d, ".spawn.lock"), os.O_CREAT | os.O_RDWR,
                     0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return "FREE"
        except OSError:
            return "HELD"
        finally:
            os.close(fd)

    def _home_probe_bin(self):
        """A stand-in `helm` that prints the claude home it was launched with."""
        path = os.path.join(self.tmp, "helm-home-probe")
        with open(path, "w") as fh:
            fh.write('#!/bin/sh\nprintf %s "${CLAUDE_CONFIG_DIR-ABSENT}"\n')
        os.chmod(path, 0o700)
        return path

    def test_the_recorded_native_home_is_the_home_the_child_launches_in(self):  # noqa: VACUOUS_ASSERTION — there is no absence assert here: both legs assert an EXACT home string read back from a REAL subprocess and compared to the persisted record, plus rc 0; the assertNotEqual against the adapter's own environment is the third value that makes the equality non-trivial
        """FINDING 4 — the spawn RECORDED the home it selected and left it out of
        the command, and a pane adapter is handed a command and no environment.
        So the child launched in whatever home the TERMINAL exported while every
        later exact-session proof read the recorded path with full confidence: the
        census listed a directory the seat never wrote into and read zero sessions
        as proof the seat was dead.

        Measured by RUNNING the emitted command in an environment that names a
        DIFFERENT home, with `helm` replaced by a script that prints the home it
        actually got (`_native_launch_argv` resolves the binary through
        `chatnode.helm_bin`, the one seam).

        CONTROLS, in this arm: the adapter's environment B is asserted to be a
        third value that the child must NOT come back with, and the second leg
        drives the DEFAULT home (no caller pin at all) through the same runner —
        so a cure that hard-coded one path fails that leg. Blast radius:
        `_native_launch_command`'s config-home assignment; before it both legs
        read back B.
        """
        import subprocess
        from helm import chatnode, homes
        self._register("proj-a")
        probe = self._home_probe_bin()
        d = seat._instance_dir("claude", "proj-a-claude")
        register = os.path.join(d, "spawn.json")
        pinned = os.path.join(self.tmp, "home-A")
        os.makedirs(pinned, exist_ok=True)
        adapter_home = os.path.join(self.tmp, "home-B")
        os.makedirs(adapter_home, exist_ok=True)
        for label, caller, expect in (("pinned", pinned, pinned),
                                      ("default", None,
                                       homes.DEFAULTS["claude"])):
            with self.subTest(home=label):
                if os.path.exists(register):
                    os.unlink(register)
                if caller is None:
                    os.environ.pop("CLAUDE_CONFIG_DIR", None)
                else:
                    os.environ["CLAUDE_CONFIG_DIR"] = caller
                fake = FakeAdapter()
                with mock.patch.object(chatnode, "helm_bin",
                                       return_value=probe):
                    rc, _out, err, _wla, _popen = self._spawn(
                        ["proj-a-claude"], fake)
                self.assertEqual(rc, 0, err)
                recorded = json.load(open(register))["config_home"]
                self.assertEqual(recorded, expect)
                launched = subprocess.run(
                    fake.spawned[0][0], shell=True, text=True,
                    capture_output=True,
                    env=dict(os.environ, CLAUDE_CONFIG_DIR=adapter_home))
                self.assertEqual(launched.returncode, 0, launched.stderr)
                self.assertEqual(launched.stdout, recorded,
                                 "the recorded proof home and the home the child "
                                 "really launched in must be one value")
                self.assertNotEqual(launched.stdout, adapter_home,
                                    "the adapter's own environment must not "
                                    "decide where this seat's sessions live")

    def test_a_project_proxy_register_resolves_before_its_child_command_runs(self):  # noqa: VACUOUS_ASSERTION — the per-leg asserts run inside subTest by design (two legs, each the other's shape control) and the unconditional positives are outside it: rc 0 for both spawns and the final record's COMPLETE attempt and exact pid
        """FINDING 6 — both project-proxy legs started their child before
        publishing the spawn record, and a PROJECT seat's family lives in that
        record. The child's first SessionStart resolved nothing, the binder
        returned False, and the spawn then wrote session None over it — with a
        backfill only Orca has, so a headless project seat never bound at all.

        The probe reads the SHIPPED resolver at the instant the child command is
        created, on both legs: inside `ad.spawn` for the pane leg and inside the
        `Popen` call for the headless one.

        CONTROLS, in this arm: the same resolver is asked for an unspawned sibling
        name at the same instant and must answer (None, None), so a green is not
        the resolver reading the name's shape; and the published record is
        asserted to be PENDING with no handle and no session, which is what
        distinguishes a published identity from a finished one. Blast radius: the
        pre-child `_register_spawn` call on each leg.
        """
        self._pin_proc()
        self._register("proj-a")
        d, _launch = self._mint(seat_name="proj-a-codex", family="codex")
        seen = {}

        def observe(tag):
            seen[tag] = (seat.registered_seat_family("proj-a-codex"),
                         seat.registered_seat_family("ghost-codex"),
                         dict(seat._spawn_record(d) or {}))

        class PaneProbe(FakeAdapter):
            def spawn(self, command, title=None, cwd=None):
                observe("pane")
                return FakeAdapter.spawn(self, command, title, cwd)

        def popen_probe(*args, **kwargs):
            observe("headless")
            return mock.Mock(pid=_FAKE_PID)

        # the proxy fate is not this arm's subject: the instance proxy is minted
        # for real under the temp HELM_HOME and its auto-start is stubbed
        with mock.patch.object(seat, "_up", return_value=0):
            rc, _out, err, _wla, _popen = self._spawn(["proj-a-codex"],
                                                      PaneProbe())
            self.assertEqual(rc, 0, err)
            os.unlink(os.path.join(d, "spawn.json"))
            rc2, _out2, err2, _wla2, _p2 = self._spawn(
                ["proj-a-codex"], None, popen=popen_probe)
            self.assertEqual(rc2, 0, err2)
        for leg in ("pane", "headless"):
            with self.subTest(leg=leg):
                resolved, ghost, published = seen[leg]
                self.assertEqual(resolved, ("codex", "proj-a"),
                                 "the child command was created before its seat "
                                 "had a register a SessionStart could resolve")
                self.assertEqual(ghost, (None, None))
                self.assertEqual(published["attempt"]["state"],
                                 seat.SPAWN_ATTEMPT_PENDING)
                self.assertIsNone(published["session"])
                self.assertIsNone(published.get("handle"))
        final = json.load(open(os.path.join(d, "spawn.json")))
        self.assertEqual(final["attempt"]["state"], seat.SPAWN_ATTEMPT_COMPLETE)
        self.assertEqual(final["pid"], _FAKE_PID)

    def test_the_lifecycle_lock_is_released_while_the_child_starts(self):
        """FINDING 5 — the spawn held the seat's lifecycle lock across the pane
        create, the five-second pane-boot grace and the onboarding submit. The
        seat's own first SessionStart runs inside that window and its binder takes
        that same lock, under the installed five-second hook timeout — so the
        child's own hook could be killed before it bound the session and before
        `chat join` initialized the seat's room cursors, which is a seat that
        starts deaf.

        The probe takes a fresh descriptor on the seat's lock file and tries a
        NON-BLOCKING exclusive flock, which contends exactly as the hook's process
        would.

        CONTROLS, two, both in this arm. The reading taken during the REAP must be
        HELD — the same probe, earlier in the same spawn — which is what proves
        the instrument can see a held lock, so FREE later is a real release. And
        the release door is then replaced by a no-op and the spawn re-driven: the
        reading at the child goes back to HELD, which is the pre-cure value. Blast
        radius: `_seat_lifecycle_lock_released`; it is a seat-local flock, so the
        arm perturbs nothing outside this temp HELM_HOME.
        """
        self._register("proj-a")
        d = seat._instance_dir("claude", "proj-a-claude")
        register = os.path.join(d, "spawn.json")
        readings = {}
        real_reap = seat._spawn_reap

        def reap(*args, **kwargs):
            readings["at_reap"] = self._lock_probe(d)
            return real_reap(*args, **kwargs)

        class Probe(FakeAdapter):
            def spawn(inner, command, title=None, cwd=None):
                readings["at_child"] = self._lock_probe(d)
                return FakeAdapter.spawn(inner, command, title, cwd)

            def send(inner, handle, text, enter=True):
                readings["at_submit"] = self._lock_probe(d)
                return FakeAdapter.send(inner, handle, text, enter)

        with mock.patch.object(seat, "_spawn_reap", reap):
            rc, _out, err, _wla, _popen = self._spawn(["proj-a-claude"],
                                                      Probe())
        self.assertEqual(rc, 0, err)
        self.assertEqual(readings["at_reap"], "HELD",
                         "the prove/act/write transition is still serialized")
        self.assertEqual(readings["at_child"], "FREE")
        self.assertEqual(readings["at_submit"], "FREE")
        # CONTROL: with the release door stubbed out, the child window is HELD
        # again — the exact pre-cure reading this arm is about
        @contextlib.contextmanager
        def never_releases(_d):
            yield False

        os.unlink(register)
        readings.clear()
        with mock.patch.object(seat, "_seat_lifecycle_lock_released",
                               never_releases):
            rc2, _out2, err2, _wla2, _p2 = self._spawn(["proj-a-claude"],
                                                       Probe())
        self.assertEqual(rc2, 0, err2)
        self.assertEqual(readings["at_child"], "HELD")

    def test_a_pending_attempt_refuses_a_concurrent_spawn_or_resume(self):  # noqa: VACUOUS_ASSERTION — the loops are fixed nonempty tuples and every cell asserts an exact refusal text, and the unconditional positive is asserted before them: the fixture's live parent process must have a readable birth identity or the arm reports it
        """FINDING 5's other half — the lock is SHORTENED, not removed, and the
        PENDING attempt is what carries the serialization across the window it no
        longer spans. A second spawn or a resume arriving there must be refused
        BEFORE the reap, because a reap destroys the runtime the first spawn is
        still building.

        The in-flight record is made by the SHIPPED producer (`_new_spawn_attempt`)
        and re-pointed at this process's PARENT, which is a real live process with
        a real birth identity — the two facts the refusal is keyed on.

        CONTROLS, three, all in this arm: an attempt whose state is COMPLETE, one
        whose process is GONE (a crashed spawn, which must stay recoverable or the
        seat is unspawnable forever) and one owned by THIS process must each get
        PAST this door — proven both at the predicate and at the verb, where the
        refusal that follows is the REAP's sentence about a stale same-name seat
        and never the in-flight one. Same fixture, different attempt, different
        refusal, which is what makes the in-flight refusal non-vacuous. Blast
        radius: `_pending_attempt_conflict`.
        """
        self._pin_proc()
        self._register("proj-a")
        d, _launch = self._mint(seat_name="proj-a-codex", family="codex")
        spawn_json = os.path.join(d, "spawn.json")
        mine = seat._new_spawn_attempt()
        parent = os.getppid()
        base = {"v": 1, "seat": "proj-a-codex", "family": "codex",
                "project": "proj-a", "harness": "headless", "role": "worker"}

        def drive(attempt, verb):
            with open(spawn_json, "w") as fh:
                json.dump(dict(base, attempt=attempt), fh)
            fake = FakeAdapter()
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(harness, "detect", return_value=fake), \
                    mock.patch.object(seat, "_up", return_value=0), \
                    mock.patch.object(seat, "_write_launch_assets"), \
                    mock.patch.object(seat.subprocess, "Popen",
                                      mock.Mock(return_value=mock.Mock(
                                          pid=_FAKE_PID))), \
                    contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                rc = seat.cmd_seat([verb, "proj-a-codex"] + (
                    ["--replace"] if verb == "spawn" else []))
            return rc, err.getvalue(), fake

        in_flight = dict(mine, pid=parent,
                         pid_identity=seat._pid_identity(parent))
        self.assertIsNotNone(in_flight["pid_identity"],
                             "the fixture's live process must have a readable "
                             "birth identity, or this arm proves nothing")
        for verb in ("spawn", "resume"):
            with self.subTest(verb=verb):
                rc, said, fake = drive(in_flight, verb)
                self.assertEqual(rc, 1, said)
                self.assertIn("is IN FLIGHT", said)
                self.assertIn(in_flight["id"], said)
                self.assertEqual(fake.spawned, [], "nothing may be spawned over "
                                                   "a live concurrent spawn")
                self.assertEqual(fake.stopped, [], "and nothing may be reaped")
        # CONTROLS: three attempts that are NOT a live foreign spawn get PAST this
        # door — a finished one, an abandoned one, and this process's own. Each is
        # checked at the predicate and then at the verb, where the refusal that
        # follows belongs to the REAP (this fixture's record names a headless seat
        # with no pid, which is unresolvable on purpose) and never to this door.
        for label, attempt in (
                ("complete", dict(in_flight,
                                  state=seat.SPAWN_ATTEMPT_COMPLETE)),
                ("abandoned", dict(in_flight, pid=1,
                                   pid_identity="proc:not-a-real-start")),
                ("mine", mine)):
            with self.subTest(control=label):
                self.assertIsNone(seat._pending_attempt_conflict(
                    dict(base, attempt=attempt)))
                rc, said, fake = drive(attempt, "spawn")
                self.assertEqual(rc, 1, said)
                self.assertNotIn("IN FLIGHT", said)
                self.assertIn("resolve the stale same-name seat", said)
                self.assertEqual(fake.spawned, [])

    def test_the_non_orca_warning_states_only_the_binding_helm_performs(self):  # noqa: VACUOUS_ASSERTION — the assertNotIn is the finding itself (the promise must be gone) and it sits beside unconditional positives from the same call: rc 0, the exact herdr sentence, the declared supported set in BOTH the warning and the binder's refusal
        """FINDING 8 — a successful non-Orca native launch printed that "the
        session binds at the pane's first SessionStart", and the binder that would
        have to do it supports exactly the harnesses it declares and REFUSES every
        other, Herdr among them. The record stayed at session None with nothing
        scheduled to repair it, and the operator was told otherwise.

        Both halves are driven: the spawn's warning for a Herdr pane, and the
        BINDER's own refusal for a Herdr record — which must name the same
        supported set, because the warning is asked of that declaration rather
        than restating it.

        CONTROL, in this arm: the binder is driven for `headless`, a harness in
        the set, where it must NOT refuse for this reason — so the set is a real
        capability and not an unconditional no. Blast radius: the session-warning
        branch and `_sessionstart_pane_fields`'s harness gate.
        """
        self._register("proj-a")
        herdr = FakeHerdrAdapter()
        rc, _out, err, _wla, _popen = self._spawn(["proj-a-claude"], herdr)
        self.assertEqual(rc, 0, err)
        self.assertIn("cannot bind a session from a herdr pane's SessionStart",
                      err)
        self.assertIn(", ".join(seat.SESSION_BINDING_HARNESSES), err)
        self.assertNotIn("so the session binds at the pane's first SessionStart",
                         err)
        rec = json.load(open(os.path.join(
            seat._instance_dir("claude", "proj-a-claude"), "spawn.json")))
        self.assertIsNone(rec["session"], "the warning's whole subject")
        # the capability the warning quotes is the binder's own
        fields, why = seat._sessionstart_pane_fields(dict(rec, harness="herdr"))
        self.assertIsNone(fields)
        self.assertIn("unsupported for harness", why)
        self.assertIn(", ".join(seat.SESSION_BINDING_HARNESSES), why)
        # CONTROL: a harness IN the set is not refused for being unsupported
        supported, why2 = seat._sessionstart_pane_fields(
            dict(rec, harness="headless", pid=os.getppid()))
        self.assertNotIn("unsupported for harness", why2 or "")
        self.assertTrue(supported is not None or why2,
                        "the headless leg answers with fields or a DIFFERENT "
                        "reason, never with the unsupported-harness refusal")


class SpawnAttemptTokenTest(ProjectRegistryBase):
    """task/2440 round seven — ONE DOOR, NOT FIVE PATCHES. A spawn attempt has a
    durable identity of its own, the ATTEMPT TOKEN, minted where the pending
    record is published (`_publish_spawn_attempt`) and threaded UNCHANGED through
    the launch line, the first hook, the finalize and the settlement.

    Round six's five retained findings were one object failing at five stages of
    its life, each stage improvising an identity: the pending predicate called an
    unverifiable process abandoned; the first hook bound ANY same-seat pane that
    could prove itself; the UNKNOWN settlement re-read the published record and
    dropped the handle the spawn held; the manual resume re-minted before it
    asked; the failure prose asserted what `where` would say. Each arm below
    drives the SHIPPED producer at one stage, on an otherwise-valid input that
    can only fail at the gate under test, and carries the control that the
    pre-token behaviour is gone.
    """

    def _pending(self, d, attempt, **extra):
        """A PENDING register written the way the producer writes it, with the
        attempt re-pointed as each arm needs."""
        rec = {"v": 1, "seat": "proj-a-codex", "family": "codex",
               "project": "proj-a", "harness": "headless", "role": "worker",
               "session": None, "attempt": attempt}
        rec.update(extra)
        with open(os.path.join(d, "spawn.json"), "w") as fh:
            json.dump(rec, fh)
        return rec

    def _verb(self, verb, args=(), adapter=None):
        """A verb through `cmd_seat` with the round-six doubles; the REAL
        `_pid_identity`, because the attempts here are keyed on a real one."""
        fake = FakeAdapter() if adapter is None else adapter
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(harness, "detect", return_value=fake), \
                mock.patch.object(seat, "_up", return_value=0), \
                mock.patch.object(seat, "_write_launch_assets") as wla, \
                mock.patch.object(seat.subprocess, "Popen",
                                  mock.Mock(return_value=mock.Mock(
                                      pid=_FAKE_PID))), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = seat.cmd_seat([verb, "proj-a-codex"] + list(args))
        return rc, err.getvalue(), fake, wla

    # ONE definition of "the token this command carries", shared with the
    # byte-exact arms' `_launch_line` derivation, so the two readings of a
    # launch line cannot drift apart.
    _token_in = staticmethod(_token_in_command)

    def test_the_attempt_token_reaches_the_launch_line_of_every_leg(self):  # noqa: VACUOUS_ASSERTION — every leg asserts the token read out of the SHIPPED command or env EQUALS the id of the record published at that instant and the id the final record carries; the two absence asserts are the --print control, beside the unconditional plan line and the missing register
        """THE POSITIVE PER STAGE: one token, minted at publication, present in the
        launch line of the native pane command, the proxy pane command and the
        headless child env — equal to the PENDING record's id at the instant the
        child is created and to the COMPLETE record's id afterwards.

        CONTROL, in this arm: a `--print` plan of the native seat renders its
        launch line with NO token and leaves no register, because a plan mints
        nothing — so the token is a fact about a published attempt and not a
        decoration every command gets. Blast radius: `_publish_spawn_attempt` and
        the `token=` argument of the three launch-line producers; before it no
        command carried the variable at all.
        """
        self._pin_proc()
        self._register("proj-a")
        d_codex, _launch = self._mint(seat_name="proj-a-codex", family="codex")
        d_claude = seat._instance_dir("claude", "proj-a-claude")
        seen = {}

        def observe(leg, d, carried):
            seen[leg] = (carried, dict(seat._spawn_record(d) or {}))

        class PaneProbe(FakeAdapter):
            leg, d = "native", d_claude

            def spawn(inner, command, title=None, cwd=None):
                observe(inner.leg, inner.d, self._token_in(command))
                return FakeAdapter.spawn(inner, command, title, cwd)

        rc, _out, err, _wla, _popen = self._spawn(["proj-a-claude"],
                                                  PaneProbe())
        self.assertEqual(rc, 0, err)
        pane = PaneProbe()
        pane.leg, pane.d = "pane", d_codex

        def popen_probe(*args, **kwargs):
            observe("headless", d_codex,
                    kwargs["env"].get(seat.SPAWN_ATTEMPT_ENV))
            return mock.Mock(pid=_FAKE_PID)

        with mock.patch.object(seat, "_up", return_value=0):
            rc2, _out2, err2, _wla2, _p2 = self._spawn(["proj-a-codex"], pane)
            self.assertEqual(rc2, 0, err2)
            finals = {"pane": json.load(open(os.path.join(d_codex,
                                                          "spawn.json")))}
            os.unlink(os.path.join(d_codex, "spawn.json"))
            rc3, _out3, err3, _wla3, _p3 = self._spawn(
                ["proj-a-codex"], None, popen=popen_probe)
            self.assertEqual(rc3, 0, err3)
        finals["headless"] = json.load(open(os.path.join(d_codex, "spawn.json")))
        finals["native"] = json.load(open(os.path.join(d_claude, "spawn.json")))
        for leg in ("native", "pane", "headless"):
            with self.subTest(leg=leg):
                carried, published = seen[leg]
                self.assertEqual(published["attempt"]["state"],
                                 seat.SPAWN_ATTEMPT_PENDING)
                self.assertTrue(carried, "the child's launch line carries no "
                                         "attempt token")
                self.assertEqual(carried, published["attempt"]["id"],
                                 "the token in the launch line is not the id of "
                                 "the record published for this spawn")
                self.assertEqual(finals[leg]["attempt"]["id"], carried)
                self.assertEqual(finals[leg]["attempt"]["state"],
                                 seat.SPAWN_ATTEMPT_COMPLETE)
        # CONTROL: a plan mints no attempt, so its launch line carries no token
        os.unlink(os.path.join(d_claude, "spawn.json"))
        rc4, out4, err4, _wla4, _p4 = self._spawn(["proj-a-claude", "--print"],
                                                  FakeAdapter())
        self.assertEqual(rc4, 0, err4)
        plan = next(line for line in out4.splitlines()
                    if line.strip().startswith("launch:"))
        self.assertIn("launch --seat proj-a-claude", plan)
        self.assertIsNone(self._token_in(plan.split(":", 1)[1]))
        self.assertFalse(os.path.exists(os.path.join(d_claude, "spawn.json")))

    def test_only_the_hook_carrying_this_attempts_token_binds_a_pending_register(self):  # noqa: VACUOUS_ASSERTION — the negative legs' assertFalse/assertIsNone are the finding (a foreign hook must not bind) and each sits beside unconditional positives from the same call: rc 0, the exact refusal naming the attempt id, the spawn's own handle in the final record; the positive leg asserts the bound session, pane key and COMPLETE attempt
        """FINDING 2 — the first-hook pending exception bound ANY same-seat hook
        that could prove its own Orca pane. A `helm launch --seat S` typed in
        another pane during the spawn's child window proves its pane just as
        well, so its session, pane key and PTY landed in the spawn's PENDING
        record and were finalized under the spawn's handle: one record, two
        occurrences.

        The hook is driven at the exact instant it fires in production — inside
        the adapter's `spawn`, while the record is PENDING with no handle and
        the lock is released — through the SHIPPED binder
        (`_bind_spawn_session` -> `_sessionstart_pane_fields`), against a live
        Orca inventory view that proves the hook's pane. The only difference
        between the legs is what the hook's environment carries.

        POSITIVE: the spawn's own child carries the token read out of the
        launch line it was given, and binds. NEGATIVES, otherwise identical
        inputs: a hook carrying NO token (the manual launch), and one carrying
        ANOTHER token, are both refused with the pending attempt named, and the
        final record carries the spawn's own handle and no session. Both
        negatives are the control that the pre-token behaviour is gone — before
        the token the no-token leg bound `session-B` and pane key `tab:pane-B`
        into this record. Blast radius: the token comparison in
        `_sessionstart_pane_fields`' pending branch, and the refusal print in
        `_bind_spawn_session`.
        """
        self._register("proj-a")
        self._native_home()          # the census reads a fixture home, never ~
        d = seat._instance_dir("claude", "proj-a-claude")
        register = os.path.join(d, "spawn.json")
        probe = self

        class HookAtChild(FakeOrcaAdapter):
            """The child's first SessionStart, at the instant the pane command
            is created. `carries` is what its environment says about the
            attempt: the launch line's token, nothing, or another token."""
            carries, pane, session = "token", "pane-1", "session-child"

            def spawn(inner, command, title=None, cwd=None):
                token = probe._token_in(command)
                inner.pending_at_hook = dict(seat._spawn_record(d) or {})
                env = {"ORCA_PANE_KEY": "tab:" + inner.pane,
                       "ORCA_WORKTREE_ID": "workspace:/w"}
                if inner.carries == "token":
                    env[seat.SPAWN_ATTEMPT_ENV] = token
                elif inner.carries == "other":
                    env[seat.SPAWN_ATTEMPT_ENV] = "not-" + str(token)
                view = FakeOrcaAdapter(
                    rows=[{"handle": inner.pane, "status": "connected",
                           "writable": True, "pty_id": "pty-1",
                           "worktree_id": "workspace:/w"}],
                    resolved={"handle": inner.pane, "pty_id": "pty-1"})
                with mock.patch.dict(os.environ, env, clear=False):
                    if inner.carries == "none":
                        os.environ.pop(seat.SPAWN_ATTEMPT_ENV, None)
                    with mock.patch.object(seat.shutil, "which",
                                           return_value="/bin/orca"), \
                            mock.patch.object(harness, "OrcaAdapter",
                                              return_value=view):
                        inner.bound = seat._bind_spawn_session(title,
                                                               inner.session)
                return FakeAdapter.spawn(inner, command, title, cwd)

        own = HookAtChild()
        rc, _out, err, _wla, _popen = self._spawn(["proj-a-claude"], own)
        self.assertEqual(rc, 0, err)
        # the hook really ran in the window: PENDING, no handle, no session
        self.assertEqual(own.pending_at_hook["attempt"]["state"],
                         seat.SPAWN_ATTEMPT_PENDING)
        self.assertIsNone(own.pending_at_hook["handle"])
        self.assertTrue(own.bound, "the spawn's own child, carrying the token "
                                   "from its launch line, must bind")
        final = json.load(open(register))
        self.assertEqual(final["session"], "session-child")
        self.assertEqual(final["handle"], "pane-1")
        self.assertEqual(final["pane_key"], "tab:pane-1")
        self.assertEqual(final["attempt"]["state"], seat.SPAWN_ATTEMPT_COMPLETE)
        for carries, said in (("none", "carries no attempt token"),
                              ("other", "carries attempt token not-")):
            with self.subTest(hook=carries):
                os.unlink(register)
                foreign = HookAtChild()
                foreign.carries, foreign.pane, foreign.session = \
                    carries, "pane-B", "session-B"
                rc2, _out2, err2, _wla2, _p2 = self._spawn(["proj-a-claude"],
                                                           foreign)
                self.assertEqual(rc2, 0, err2)     # the SPAWN itself succeeds
                attempt_id = foreign.pending_at_hook["attempt"]["id"]
                self.assertFalse(foreign.bound)
                self.assertIn("SessionStart bind REFUSED", err2)
                self.assertIn("PENDING spawn attempt %s" % attempt_id, err2)
                self.assertIn(said, err2)
                self.assertIn("manual `helm launch --seat proj-a-claude`", err2)
                kept = json.load(open(register))
                self.assertIsNone(kept["session"],
                                  "a foreign pane's session bound into this "
                                  "spawn's record")
                self.assertEqual(kept["handle"], "pane-1")
                self.assertNotEqual(kept.get("pane_key"), "tab:pane-B")
                self.assertEqual(kept["attempt"]["id"], attempt_id)
                self.assertEqual(kept["attempt"]["state"],
                                 seat.SPAWN_ATTEMPT_COMPLETE)

    def test_an_unverifiable_pending_attempt_is_a_third_state_never_abandoned(self):  # noqa: VACUOUS_ASSERTION — the loops are fixed nonempty tuples; every refusal cell asserts the exact UNVERIFIABLE sentence with the attempt id, rc 1 and untouched doubles, and the two proof cells assert the opposite verdict and the REAP's sentence, so neither side can be an unconditional answer
        """FINDING 1 — `_pending_attempt_conflict` treated a pending attempt whose
        saved OR current birth identity was None as GONE, and `_pid_identity`'s
        None means UNVERIFIABLE. So a `--replace` arriving while a live spawn's
        identity happened to be unreadable passed the door, reaped, and
        launched a second pane over the first.

        The pending record is made by the SHIPPED producer and re-pointed at
        this process's PARENT, a real live process whose identity IS readable
        (asserted first, or the arm proves nothing). Two unverifiable legs: the
        saved identity is None; the current read returns None. Each is asked at
        the predicate and then at both verbs, with `--replace` on the spawn —
        which must NOT override it.

        CONTROLS, in this arm, both PROOFS of absence: a pid nothing holds
        (`_FAKE_PID`) and pid 1 under a foreign start time (a reborn pid). Each
        reads GONE, passes the predicate, and the verb then fails at the REAP's
        own sentence — so the door still opens on proof, and the pre-token route
        for an unverifiable identity (that same reap sentence, with nothing
        spawned) is asserted GONE from the unverifiable legs. Blast radius:
        `_attempt_process_state` and the UNVERIFIABLE branch of
        `_pending_attempt_conflict`.
        """
        self._pin_proc()
        self._register("proj-a")
        d, _launch = self._mint(seat_name="proj-a-codex", family="codex")
        parent = os.getppid()
        live = dict(seat._new_spawn_attempt(), pid=parent,
                    pid_identity=seat._pid_identity(parent))
        self.assertIsNotNone(live["pid_identity"],
                             "the fixture's live process must have a readable "
                             "birth identity, or this arm proves nothing")
        self.assertEqual(seat._attempt_process_state(live), seat.ATTEMPT_LIVE)
        never = mock.patch.object(seat, "_pid_identity", return_value=None)
        for label, attempt, unreadable in (
                ("saved identity None", dict(live, pid_identity=None), False),
                ("current identity unreadable", live, True)):
            with self.subTest(leg=label):
                with never if unreadable else contextlib.nullcontext():
                    self.assertEqual(seat._attempt_process_state(attempt),
                                     seat.ATTEMPT_UNVERIFIABLE)
                    why = seat._pending_attempt_conflict(
                        self._pending(d, attempt))
                    self.assertIn("UNVERIFIABLE", why)
                    self.assertIn(attempt["id"], why)
                    self.assertIn("NOT treated as abandoned", why)
                    self.assertIn("--replace does not override", why)
                    for verb, args in (("spawn", ["--replace"]),
                                       ("resume", [])):
                        self._pending(d, attempt)
                        rc, said, fake, wla = self._verb(verb, args)
                        self.assertEqual(rc, 1, said)
                        self.assertIn("UNVERIFIABLE", said)
                        self.assertIn(attempt["id"], said)
                        # the pre-token route: abandoned -> the reap's sentence
                        self.assertNotIn("resolve the stale same-name seat",
                                         said)
                        self.assertEqual(fake.spawned, [])
                        self.assertEqual(fake.stopped, [])
                        wla.assert_not_called()
        # CONTROLS: the two PROOFS of absence open the door, and the verb then
        # fails one rung later, at the reap (this fixture's record is a headless
        # seat with no pid, unresolvable on purpose)
        for label, attempt in (
                ("no such pid", dict(live, pid=_FAKE_PID)),
                ("pid reborn", dict(live, pid=1,
                                    pid_identity="proc:not-a-real-start"))):
            with self.subTest(control=label):
                self.assertEqual(seat._attempt_process_state(attempt),
                                 seat.ATTEMPT_GONE)
                self.assertIsNone(seat._pending_attempt_conflict(
                    self._pending(d, attempt)))
                rc, said, fake, _wla = self._verb("spawn", ["--replace"])
                self.assertEqual(rc, 1, said)
                self.assertNotIn("UNVERIFIABLE", said)
                self.assertIn("resolve the stale same-name seat", said)
                self.assertEqual(fake.spawned, [])

    def test_an_unknown_settlement_keeps_the_handle_and_pid_the_spawn_holds(self):  # noqa: VACUOUS_ASSERTION — the session assertIsNone is the no-invented-session law and sits beside the unconditional positives each leg is about: the EXACT retained handle or pid in the kept record, the INCOMPLETE state, and the resolver's detail naming that handle; the CLOSED control asserts the record's absence beside rc 1 and the stopped pane
        """FINDING 3 — the UNKNOWN settlement re-read the register (published
        with handle None, pid None) and changed only the attempt, discarding the
        handle the adapter had returned and the pid the launch had produced. The
        INCOMPLETE record then named a pane nothing could point at: the resolver
        said "no pane identity" and `where` read GONE from a pid of None.

        All three legs — native pane, proxy pane, headless — settle through the
        ONE function, each with its finalize failing (the register mock delegates
        the publication to the shipped writer and refuses the second write, so
        the record the settlement is about was really published). CONTROL, in
        this arm: the same native failure with an adapter whose inventory PROVES
        the close restores authority and leaves no record — so the retention is
        the unproven branch's, not an unconditional keep. Blast radius: the
        `retained=` argument of `_settle_spawn_attempt`; before it every kept
        record here carried handle None / pid None.
        """
        from helm import seat_exit_owner as exit_owner
        self._pin_proc()
        self._register("proj-a")
        self._native_home()
        d_codex, _launch = self._mint(seat_name="proj-a-codex", family="codex")
        d_claude = seat._instance_dir("claude", "proj-a-claude")
        real_register = seat._register_spawn

        def publish_then_fail(calls):
            def register(storage_seat, identity, d, rec):
                calls.append(rec)
                return len(calls) == 1 and real_register(
                    storage_seat, identity, d, rec)
            return register

        # native pane (orca: no exact-process census, so the close is UNKNOWN)
        calls = []
        orca = FakeOrcaAdapter()
        with mock.patch.object(seat, "_register_spawn", publish_then_fail(calls)):
            rc, _out, err, _wla, _popen = self._spawn(["proj-a-claude"], orca)
        self.assertEqual(rc, 1)
        self.assertEqual(len(calls), 2)
        kept = json.load(open(os.path.join(d_claude, "spawn.json")))
        self.assertEqual(kept["attempt"]["state"], seat.SPAWN_ATTEMPT_INCOMPLETE)
        self.assertEqual(kept["handle"], "pane-1",
                         "the handle the adapter returned was discarded")
        self.assertIsNone(kept["session"])
        _ad, handle, detail = seat._resolve_registered_pane(
            "proj-a-claude", d=d_claude, adapter=orca, repair=False)
        self.assertNotIn("has no pane identity", detail)
        self.assertIn("pane-1", detail)
        # proxy pane, same shape
        calls = []
        orca2 = FakeOrcaAdapter()
        with mock.patch.object(seat, "_register_spawn", publish_then_fail(calls)), \
                mock.patch.object(seat, "_up", return_value=0):
            rc2, _out2, err2, _wla2, _p2 = self._spawn(["proj-a-codex"], orca2)
        self.assertEqual(rc2, 1)
        self.assertEqual(len(calls), 2)
        kept2 = json.load(open(os.path.join(d_codex, "spawn.json")))
        self.assertEqual(kept2["attempt"]["state"], seat.SPAWN_ATTEMPT_INCOMPLETE)
        self.assertEqual(kept2["handle"], "pane-1")
        self.assertIsNone(kept2["session"])
        self.assertIn("could not record pane pane-1", err2)
        # headless: the launched pid, with its birth identity, is what survives
        os.unlink(os.path.join(d_codex, "spawn.json"))
        calls = []
        with mock.patch.object(seat, "_register_spawn", publish_then_fail(calls)), \
                mock.patch.object(seat, "_up", return_value=0), \
                mock.patch.object(exit_owner, "terminate_process",
                                  return_value=(None, "signal refused by the "
                                                      "fixture")):
            rc3, _out3, err3, _wla3, _p3 = self._spawn(["proj-a-codex"], None)
        self.assertEqual(rc3, 1)
        self.assertEqual(len(calls), 2)
        kept3 = json.load(open(os.path.join(d_codex, "spawn.json")))
        self.assertEqual(kept3["attempt"]["state"], seat.SPAWN_ATTEMPT_INCOMPLETE)
        self.assertEqual(kept3["pid"], _FAKE_PID,
                         "the pid the launch produced was discarded")
        self.assertEqual(kept3["pid_identity"], "test-start")
        self.assertIsNone(kept3["session"])
        self.assertIn("pid %d" % _FAKE_PID, err3)
        # CONTROL: a PROVEN close restores authority — no record, no retention
        os.unlink(os.path.join(d_claude, "spawn.json"))
        calls = []
        proven = FakeAdapter()
        with mock.patch.object(seat, "_register_spawn", publish_then_fail(calls)):
            rc4, _out4, err4, _wla4, _p4 = self._spawn(["proj-a-claude"], proven)
        self.assertEqual(rc4, 1)
        self.assertEqual(proven.stopped, ["pane-1"])
        self.assertFalse(os.path.exists(os.path.join(d_claude, "spawn.json")))

    def test_a_manual_resume_asks_the_in_flight_door_before_it_writes(self):  # noqa: VACUOUS_ASSERTION — assert_not_called is the finding's observable (no asset written) and stands beside unconditional positives: rc 1, the exact IN FLIGHT sentence with the attempt id; the control asserts the write DID happen and the manual-paste sentence on the same fixture with a settled attempt
        """FINDING 4 — with no metaharness detected, `resume` re-minted the
        seat's launch assets and returned a manual command BEFORE it asked
        whether another spawn's PENDING attempt owned the seat; the in-flight
        door sat below that early return. A standalone `resume S --cwd <valid
        other cwd>` during a proxy spawn's child window rewrote the room, launch
        line and settings of the seat that spawn was still bringing up.

        The in-flight record is the shipped producer's, re-pointed at this
        process's live parent. `harness.detect` answers None, which is the
        no-adapter branch. CONTROL, in this arm: the identical resume over the
        SAME record with its attempt COMPLETE reaches the manual branch — the
        asset writer is called and the manual-paste sentence is printed — so the
        refusal is the door's and not the branch's. Blast radius: the position of
        `_refuse_in_flight_spawn` in `_resume`; before the move the in-flight leg
        here called the writer once and printed the paste.
        """
        self._pin_proc()
        self._register("proj-a")
        d, _launch = self._mint(seat_name="proj-a-codex", family="codex")
        parent = os.getppid()
        live = dict(seat._new_spawn_attempt(), pid=parent,
                    pid_identity=seat._pid_identity(parent))
        self.assertIsNotNone(live["pid_identity"])

        def resume():
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(harness, "detect", return_value=None), \
                    mock.patch.object(seat, "_write_launch_assets") as wla, \
                    contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                rc = seat.cmd_seat(["resume", "proj-a-codex"])
            return rc, err.getvalue(), wla

        self._pending(d, live)
        rc, said, wla = resume()
        self.assertEqual(rc, 1, said)
        self.assertIn("is IN FLIGHT", said)
        self.assertIn(live["id"], said)
        self.assertNotIn("manual paste", said)
        wla.assert_not_called()
        # CONTROL: the settled attempt, same fixture, reaches the manual branch
        self._pending(d, dict(live, state=seat.SPAWN_ATTEMPT_COMPLETE))
        rc2, said2, wla2 = resume()
        self.assertEqual(rc2, 1, said2)
        self.assertNotIn("IN FLIGHT", said2)
        self.assertIn("manual paste", said2)
        self.assertEqual(wla2.call_count, 1)

    def test_the_failed_register_prose_says_what_where_will_resolve(self):  # noqa: VACUOUS_ASSERTION — the one assertNotIn ("resolves nothing" must be gone from the UNKNOWN leg) is the finding, beside unconditional positives per leg: the exact derived clause, the attempt id, the pane, and `where --json` answering the SAME record (handle and state) the sentence promised
        """FINDING 5 — after a failed final register the native leg printed
        "`helm seat where` resolves nothing" unconditionally, including when
        the settlement had just KEPT an INCOMPLETE record with a pane in it.

        The claim is now DERIVED from what `_settle_spawn_attempt` returned,
        and this arm holds the sentence to the verb: for each leg the prose is
        read, then `seat where --json` is driven and its answer compared to
        what the prose promised. CLOSED (non-orca adapter, proven absent, no
        prior register): the prose says no register of this attempt resolves
        and PROVEN absent, and no register exists (a native name with no
        register is `where`'s adoption seam's to answer, which is not this
        attempt's record and not driven here). UNKNOWN (orca, close unproven):
        the prose names the INCOMPLETE attempt and pane `pane-1`, and `where`
        returns that record with that handle and state. The two legs are each
        other's control, and the UNKNOWN leg is the control that the
        unconditional sentence is gone. Blast radius: `_settled_where_claim`
        and the return value of `_settle_spawn_attempt`.
        """
        self._register("proj-a")
        self._native_home()
        d = seat._instance_dir("claude", "proj-a-claude")
        register = os.path.join(d, "spawn.json")
        real_register = seat._register_spawn
        for adapter, state in ((FakeAdapter(), "CLOSED"),
                               (FakeOrcaAdapter(), "UNKNOWN")):
            with self.subTest(state=state):
                if os.path.exists(register):
                    os.unlink(register)
                calls = []

                def publish_then_fail(storage_seat, identity, d, rec):
                    calls.append(rec)
                    return len(calls) == 1 and real_register(
                        storage_seat, identity, d, rec)

                with mock.patch.object(seat, "_register_spawn",
                                       publish_then_fail):
                    rc, _out, err, _wla, _popen = self._spawn(
                        ["proj-a-claude"], adapter)
                self.assertEqual(rc, 1)
                self.assertIn("could not record pane pane-1", err)
                self.assertIn(state, err)
                if state == "CLOSED":
                    self.assertIn("`helm seat where proj-a-claude` resolves "
                                  "no register of this attempt", err)
                    self.assertIn("PROVEN absent", err)
                    self.assertFalse(os.path.exists(register))
                    self.assertIsNone(seat._spawn_record(d))
                    continue
                out, err2 = io.StringIO(), io.StringIO()
                with mock.patch.object(harness, "detect", return_value=adapter), \
                        mock.patch.object(seat, "_prove_orca_replacement",
                                          return_value=(None, None,
                                                        "no live process")), \
                        contextlib.redirect_stdout(out), \
                        contextlib.redirect_stderr(err2):
                    where_rc = seat.cmd_seat(["where", "proj-a-claude",
                                              "--json"])
                attempt_id = calls[0]["attempt"]["id"]
                self.assertNotIn("resolves nothing", err)
                self.assertIn("`helm seat where proj-a-claude` resolves this "
                              "attempt's INCOMPLETE record: orca pane pane-1, "
                              "attempt %s incomplete, session unbound"
                              % attempt_id, err)
                self.assertEqual(where_rc, 0, err2.getvalue())
                answered = json.loads(out.getvalue())
                self.assertEqual(answered["handle"], "pane-1")
                self.assertEqual(answered["attempt"]["id"], attempt_id)
                self.assertEqual(answered["attempt"]["state"],
                                 seat.SPAWN_ATTEMPT_INCOMPLETE)
                self.assertIsNone(answered["session"])


    def test_two_readings_of_one_birth_in_two_domains_are_incomparable(self):  # noqa: VACUOUS_ASSERTION — the two assertNotIn sit beside unconditional positives (the exact UNVERIFIABLE verdict, the sentence with the attempt id, rc 1) and each is the OPPOSITE of an unconditional assertIn on the same fixture one control down: control B asserts the reap sentence IS present and control A asserts IN FLIGHT is
        """ROUND EIGHT, P2 — `_pid_identity` has TWO producers behind one name:
        `proc:<starttime>` when /proc is readable and `ps:<lstart>` on the
        fallback. `_attempt_process_state` compared the two readings as strings
        and called every inequality GONE, so ONE living spawn whose second
        reading fell back to `ps:` read as ABANDONED: the in-flight door opened,
        a concurrent `spawn --replace` passed it, and `_spawn_reap` stops a pane
        before any terminal proof — a live child reaped on a change of spelling.

        The fixture is the SHIPPED producer's attempt re-pointed at this
        process's PARENT — one real, live, unchanging process (asserted LIVE on
        its real reading first, or the arm proves nothing). Only the DOMAIN of
        the second reading varies; the process, the pid and the record are the
        same in every leg.

        CONTROLS, in this arm, on that same fixture, each the discriminator for
        the leg above it: (A) the same domain, the same reading — still LIVE,
        still refused as IN FLIGHT; (B) the same domain, a DIFFERENT birth —
        GONE, the door OPENS and the verb falls through to the reap's own
        sentence, which is exactly the route the cross-domain leg took before
        this cure and is asserted absent from it; (C) a pid nothing holds, whose
        ProcessLookupError is still proof of absence. Blast radius: the domain
        comparison in `_attempt_process_state`; every other caller of it reads
        the same three verdicts.
        """
        self._pin_proc()
        self._register("proj-a")
        d, _launch = self._mint(seat_name="proj-a-codex", family="codex")
        parent = os.getppid()
        real = seat._pid_identity(parent)
        self.assertIsNotNone(real, "the fixture's live process must have a "
                                   "readable birth identity, or this arm "
                                   "proves nothing")
        domain = real.split(":", 1)[0]
        self.assertIn(domain, ("proc", "ps"),
                      "the producer's reading must name its domain: %r" % real)
        live = dict(seat._new_spawn_attempt(), pid=parent, pid_identity=real)
        self.assertEqual(seat._attempt_process_state(live), seat.ATTEMPT_LIVE)
        # the SAME birth, said in the other producer's language
        other_domain = ("ps:Sat Sep 13 11:04:28 2026" if domain == "proc"
                        else "proc:8419")
        # ...and a different birth, said in the SAME language
        same_domain_other_birth = "%s:not-this-birth" % domain
        with mock.patch.object(seat, "_pid_identity",
                               return_value=other_domain):
            self.assertEqual(seat._attempt_process_state(live),
                             seat.ATTEMPT_UNVERIFIABLE)
            why = seat._pending_attempt_conflict(self._pending(d, live))
            self.assertIn("UNVERIFIABLE", why)
            self.assertIn(live["id"], why)
            self.assertIn("--replace does not override", why)
            for verb, args in (("spawn", ["--replace"]), ("resume", [])):
                with self.subTest(verb=verb):
                    self._pending(d, live)
                    rc, said, fake, wla = self._verb(verb, args)
                    self.assertEqual(rc, 1, said)
                    self.assertIn("UNVERIFIABLE", said)
                    self.assertIn(live["id"], said)
                    # the pre-cure route: called abandoned, reaped, respawned
                    self.assertNotIn("resolve the stale same-name seat", said)
                    self.assertEqual(fake.spawned, [])
                    self.assertEqual(fake.stopped, [])
                    wla.assert_not_called()
        # CONTROL A: same domain, same reading — LIVE, refused by name
        with mock.patch.object(seat, "_pid_identity", return_value=real):
            self.assertEqual(seat._attempt_process_state(live),
                             seat.ATTEMPT_LIVE)
            self._pending(d, live)
            rc_a, said_a, fake_a, _wla_a = self._verb("spawn", ["--replace"])
            self.assertEqual(rc_a, 1, said_a)
            self.assertIn("is IN FLIGHT", said_a)
            self.assertIn(live["id"], said_a)
            self.assertEqual(fake_a.spawned, [])
        # CONTROL B: same domain, another birth — GONE, the door opens
        with mock.patch.object(seat, "_pid_identity",
                               return_value=same_domain_other_birth):
            self.assertEqual(seat._attempt_process_state(live),
                             seat.ATTEMPT_GONE)
            self.assertIsNone(seat._pending_attempt_conflict(
                self._pending(d, live)))
            self._pending(d, live)
            rc_b, said_b, fake_b, _wla_b = self._verb("spawn", ["--replace"])
            self.assertEqual(rc_b, 1, said_b)
            self.assertIn("resolve the stale same-name seat", said_b)
            self.assertNotIn("UNVERIFIABLE", said_b)
            self.assertEqual(fake_b.spawned, [])
        # CONTROL C: the pid nothing holds — the producer's own
        # ProcessLookupError, still proof of absence
        self.assertEqual(seat._attempt_process_state(dict(live, pid=_FAKE_PID)),
                         seat.ATTEMPT_GONE)

    def test_a_settlement_that_could_not_persist_reports_an_intention(self):  # noqa: VACUOUS_ASSERTION — the assertNotIn names the pre-cure sentence and stands beside unconditional positives in every leg: the exact UNPERSISTED clause, the disk record re-read from the file, `where --json` agreeing with that file, and the control's exact persisted clause
        """ROUND EIGHT, P3 — the UNKNOWN settlement mutated the re-read record
        with the handle it held and an INCOMPLETE attempt, and when the write
        RAISED it handed that MUTATED dict back as the record `helm seat where`
        would resolve. The prose then described the disk with an attempted
        handle and a state that never reached disk; the proven-absent branch
        said SETTLED_RESTORED whether or not the restore had been swallowed.

        LEG 1, the ordinary failure: publication succeeds and every LATER write
        of `spawn.json` fails at `pk.atomic_write` (before `os.replace`), so the
        final registration AND the settlement both fail — the shipped producers
        do the rest. The record on disk is still PENDING with handle None, and
        the prose must say exactly that, name the intended handle as UNPERSISTED,
        and agree with `seat where --json` read back from the same file.
        LEG 2: the proven-absent branch with its unlink refused — the register is
        still there and the prose says the seat was NOT handed back.
        CONTROL, in this arm: the SAME failure with the settlement write ALLOWED
        (only the final registration refused) renders the persisted claim — the
        exact handle, the INCOMPLETE state, no UNPERSISTED clause — so the new
        wording belongs to a MEASURED failed write and not to every failure.
        Blast radius: the return of `_settle_spawn_attempt` and the branch
        `_settled_where_claim` renders from it.
        """
        from helm import pk
        self._register("proj-a")
        self._native_home()
        d = seat._instance_dir("claude", "proj-a-claude")
        register = os.path.join(d, "spawn.json")
        real_atomic, real_unlink = pk.atomic_write, os.unlink

        def publication_only(path, text):
            """The shipped writer for the publication, a full disk after it."""
            if os.path.basename(path) == "spawn.json" and published:
                raise OSError("No space left on device")
            real_atomic(path, text)
            if os.path.basename(path) == "spawn.json":
                published.append(path)

        published = []
        orca = FakeOrcaAdapter()
        with mock.patch.object(pk, "atomic_write", publication_only):
            rc, _out, err, _wla, _popen = self._spawn(["proj-a-claude"], orca)
        self.assertEqual(rc, 1, err)
        self.assertEqual(len(published), 1, "the publication itself failed, so "
                                            "this arm is not about settlement")
        on_disk = json.load(open(register))
        attempt_id = on_disk["attempt"]["id"]
        self.assertIsNone(on_disk["handle"])
        self.assertEqual(on_disk["attempt"]["state"], seat.SPAWN_ATTEMPT_PENDING)
        self.assertIn("The INCOMPLETE stamp could NOT be written", err)
        self.assertIn("`helm seat where proj-a-claude` resolves the record on "
                      "disk, UNCHANGED by this settlement: orca pane None, "
                      "attempt %s pending, session unbound" % attempt_id, err)
        self.assertIn("helm INTENDED, and did NOT persist: orca pane pane-1, "
                      "attempt %s incomplete, session unbound" % attempt_id,
                      err)
        # the pre-cure sentence: the intended record described AS the disk
        self.assertNotIn("resolves this attempt's INCOMPLETE record", err)
        out, err2 = io.StringIO(), io.StringIO()
        with mock.patch.object(harness, "detect", return_value=orca), \
                mock.patch.object(seat, "_prove_orca_replacement",
                                  return_value=(None, None,
                                                "no live process")), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err2):
            where_rc = seat.cmd_seat(["where", "proj-a-claude", "--json"])
        self.assertEqual(where_rc, 0, err2.getvalue())
        answered = json.loads(out.getvalue())
        self.assertIsNone(answered["handle"], "the verb answered the handle the "
                                              "prose only INTENDED")
        self.assertEqual(answered["attempt"]["state"],
                         seat.SPAWN_ATTEMPT_PENDING)
        # LEG 2: PROVEN absent, and the unlink of this attempt's register
        # fails. The register is removed FIRST so this spawn's `prior` is None —
        # the branch that hands the seat back by REMOVING the record it
        # published, which is the unlink the fixture then refuses.
        os.unlink(register)
        real_register = seat._register_spawn

        def publish_then_fail(calls):
            """The shipped writer for the publication, a refusal for the final
            registration — so the spawn really reaches its settlement."""
            def register(storage_seat, identity, d_, rec):
                calls.append(rec)
                return len(calls) == 1 and real_register(storage_seat, identity,
                                                         d_, rec)
            return register

        def refuse_spawn_json(path, *args, **kwargs):
            if str(path).endswith("spawn.json"):
                raise OSError("Read-only file system")
            return real_unlink(path, *args, **kwargs)

        absent_calls = []
        with mock.patch.object(seat, "_register_spawn",
                               publish_then_fail(absent_calls)), \
                mock.patch.object(os, "unlink", refuse_spawn_json):
            rc2, _out2, err2b, _wla2, _p2 = self._spawn(["proj-a-claude"],
                                                        FakeAdapter())
        self.assertEqual(len(absent_calls), 2, err2b)
        self.assertEqual(rc2, 1, err2b)
        self.assertIn("could not be restored", err2b)
        self.assertIn("The prior register could NOT be put back", err2b)
        self.assertIn("so the seat was NOT handed back", err2b)
        self.assertTrue(os.path.exists(register),
                        "the register the restore could not remove is gone")
        # CONTROL: the same failure with the settlement write ALLOWED
        os.unlink(register)
        with mock.patch.object(seat, "_register_spawn",
                               publish_then_fail([])):
            rc3, _out3, err3, _wla3, _p3 = self._spawn(["proj-a-claude"],
                                                       FakeOrcaAdapter())
        self.assertEqual(rc3, 1, err3)
        kept = json.load(open(register))
        self.assertEqual(kept["handle"], "pane-1")
        self.assertEqual(kept["attempt"]["state"],
                         seat.SPAWN_ATTEMPT_INCOMPLETE)
        self.assertIn("`helm seat where proj-a-claude` resolves this attempt's "
                      "INCOMPLETE record: orca pane pane-1, attempt %s "
                      "incomplete, session unbound" % kept["attempt"]["id"],
                      err3)
        self.assertNotIn("UNPERSISTED", err3)
        self.assertNotIn("did NOT persist", err3)

    def test_a_settlement_whose_reread_failed_says_the_disk_state_is_unknown(self):  # noqa: VACUOUS_ASSERTION — the three assertNotIn name the pre-cure sentence and its two banned disk assertions, and each stands beside unconditional positives: the exact UNKNOWN clause with the reader's own errno, the spied settlement return (state, disk, disk_error, record None), and two controls that assert the OTHER two disk answers verbatim on the same fixture
        """ROUND TEN, P3 — after a failed settlement write the round-nine cure
        RE-READ the register and described what it found. But the re-read went
        through `_spawn_record`, which answers None for a file that is not there
        AND for a file it could not open — so when the storage was UNAVAILABLE
        (the same unavailability that had just refused the write) the prose said
        "resolves no record: none is on disk" about a register nothing had read.
        An absence was asserted from a reader that had only failed.

        The three answers disk can give are now separated. THIS LEG is the third:
        the shipped spawn publishes, its final registration is refused, and the
        SETTLEMENT's own write is refused by a store that is unreadable from that
        moment on (the register is really chmod 000 — the arm asserts the file is
        unreadable, or it is not about this cure at all). The prose must say the
        disk state could NOT be read, name the reader's error, and assert
        NOTHING about a handle, a state or an absence; the settlement's returned
        `unpersisted` must carry disk "unknown" and that error.

        CONTROLS, in this arm, on the same fixture and each the discriminator
        for a different disk answer: (A) the round-nine leg — the same refused
        writes with the register left READABLE — still renders the disk record
        (handle None, PENDING), which is exactly the sentence the unknown leg is
        asserted not to print; (B) the re-read finds NO file (the refusing write
        removes the register) — absent is still rendered as absent. Without the
        cure all three legs print control (B)'s sentence, so A and C both go red.
        Blast radius: the re-read inside `_settle_spawn_attempt` and the
        `unpersisted` branch of `_settled_where_claim`; every successful-write
        path returns `unpersisted` None and never reaches either.
        """
        from helm import pk
        self._register("proj-a")
        self._native_home()
        d = seat._instance_dir("claude", "proj-a-claude")
        register = os.path.join(d, "spawn.json")
        real_atomic = pk.atomic_write
        real_settle = seat._settle_spawn_attempt

        def refusing_writer(after_settlement_write):
            """The SHIPPED writer for the publication and a refusal for every
            later `spawn.json` write, with `after_settlement_write` run just
            before the SECOND refusal — the settlement's own — so the store goes
            bad exactly where a real one would: between the write it refused and
            the re-read that asks the same store."""
            refusals = []

            def write(path, text):
                if os.path.basename(path) == "spawn.json" and published:
                    refusals.append(path)
                    if len(refusals) == 2:
                        after_settlement_write(path)
                    raise OSError("No space left on device")
                real_atomic(path, text)
                if os.path.basename(path) == "spawn.json":
                    published.append(path)

            return write

        def run(after_settlement_write):
            del published[:], settled[:]
            if os.path.exists(register):
                os.chmod(register, 0o600)
                os.unlink(register)
            with mock.patch.object(pk, "atomic_write",
                                   refusing_writer(after_settlement_write)), \
                    mock.patch.object(seat, "_settle_spawn_attempt", spy):
                rc, _out, err, _wla, _popen = self._spawn(["proj-a-claude"],
                                                          FakeOrcaAdapter())
            self.assertEqual(rc, 1, err)
            self.assertEqual(len(published), 1, "the publication itself failed, "
                                                "so this arm is not about the "
                                                "settlement's re-read")
            return err

        published, settled = [], []

        def spy(*args, **kwargs):
            outcome = real_settle(*args, **kwargs)
            settled.append(outcome)
            return outcome

        # THE LEG: the store that refused the write cannot be read either
        err = run(lambda path: os.chmod(register, 0))
        self.assertFalse(os.access(register, os.R_OK),
                         "the register is still readable, so this leg never "
                         "exercised a failed re-read")
        state, rec, unpersisted = settled[-1]
        os.chmod(register, 0o600)
        self.assertEqual(state, seat.SETTLED_UNRECORDED)
        self.assertIsNone(rec, "a failed read was handed back as a record")
        self.assertEqual(unpersisted["disk"], "unknown")
        self.assertIn("Permission denied", unpersisted["disk_error"])
        self.assertIn("`helm seat where proj-a-claude` resolves whatever is on "
                      "disk, and the disk state could NOT be read to say what "
                      "that is (", err)
        self.assertIn(unpersisted["disk_error"], err)
        # the pre-cure sentence, and the two disk facts it must not assert
        self.assertNotIn("resolves no record: none is on disk", err)
        self.assertNotIn("orca pane None", err)
        self.assertNotIn("pending, session unbound", err)
        # CONTROL A: the round-nine leg — the register stays readable
        err_a = run(lambda path: None)
        state_a, rec_a, unpersisted_a = settled[-1]
        self.assertEqual(state_a, seat.SETTLED_UNRECORDED)
        self.assertEqual(unpersisted_a["disk"], "record")
        self.assertIsNone(unpersisted_a["disk_error"])
        self.assertIn("`helm seat where proj-a-claude` resolves the record on "
                      "disk, UNCHANGED by this settlement: orca pane None, "
                      "attempt %s pending, session unbound"
                      % rec_a["attempt"]["id"], err_a)
        self.assertNotIn("could NOT be read", err_a)
        # CONTROL B: the re-read finds no file — absent stays absent
        err_b = run(os.unlink)
        state_b, rec_b, unpersisted_b = settled[-1]
        self.assertEqual(state_b, seat.SETTLED_UNRECORDED)
        self.assertEqual(unpersisted_b["disk"], "absent")
        self.assertIsNone(rec_b)
        self.assertFalse(os.path.exists(register))
        self.assertIn("`helm seat where proj-a-claude` resolves no record: "
                      "none is on disk", err_b)
        self.assertNotIn("could NOT be read", err_b)


class SessionRecordRootTest(SpawnBase):
    """task/2440 round five — the session-record root belongs to the record the
    proof is ABOUT.

    A native seat's session records land under the home its launch selected, so
    the root is read out of the spawn record's `config_home`. Resolving it by a
    FRESH read of `spawn.json` inside the proof made the root an answer about a
    second, separately-timed read: the rebind compares a live process against the
    record in its hand while looking for that process's session under whatever
    home a concurrent writer had just pinned. Every caller already holds the
    record; it is now passed.
    """

    def test_the_session_root_follows_the_record_the_caller_holds(self):
        """The parameter is USED, not accepted and ignored: the same seat dir
        answers two different roots for two different records, and the one in
        hand wins over the one on disk.

        CONTROL: omitting it falls back to the record ON DISK, which is the
        behaviour every caller without a record in hand (`_live_seat_orca_identity`
        starts from a seat NAME) depends on. Blast radius: one path string.
        """
        d, _ = self._mint()
        self._record(d, config_home=os.path.join(self.tmp, "on-disk-home"))
        held = os.path.join(self.tmp, "in-hand-home")
        # THE UNCONDITIONAL POSITIVE: three distinct answers for one seat dir, so
        # an accessor that ignored the parameter collapses them and fails here
        # before any of the three equalities below say which is which.
        answers = [seat_runtime._session_record_root(d, {"config_home": held}),
                   seat_runtime._session_record_root(d),
                   seat_runtime._session_record_root(d, {"seat": "seat-a"})]
        self.assertEqual(len(set(answers)), 3, answers)
        self.assertEqual(seat_runtime._session_record_root(d, {"config_home": held}),
                         os.path.join(held, "sessions"))
        # CONTROL: no record in hand reads the one on disk
        self.assertEqual(seat_runtime._session_record_root(d),
                         os.path.join(self.tmp, "on-disk-home", "sessions"))
        # and a record pinning no home keeps the historical instance path
        self.assertEqual(seat_runtime._session_record_root(d, {"seat": "codex"}),
                         os.path.join(d, "claude", "sessions"))

    def test_a_rebind_proof_reads_the_spawn_record_once(self):
        """THE TRUNK ARM'S SHAPE, driven through the shipped producer: a rebind
        whose recorded session is dead falls back to the seat's own name, and the
        only file the fallback needs is the live process's `/proc/<pid>/environ`.

        CONTROL, and it is what makes the count discriminating: the SAME counting
        `open` is driven over `_session_record_root(d)` with no record, which DOES
        open `spawn.json` — so the counter sees real input and a zero-arg
        instrument cannot pass this arm. Blast radius: `builtins.open` inside the
        two `with` blocks only.
        """
        d, _ = self._mint()
        row = {"handle": "new", "status": "connected", "writable": True,
               "pty_id": "pty-new", "worktree_id": "workspace:/w"}
        rec = {"seat": "codex", "harness": "orca", "handle": "old",
               "session": "recorded-dead"}
        proc = "/proc/4242/environ"
        env = (b"HELM_CHAT_NAME=codex\0ORCA_PANE_KEY=pane-new\0"
               b"ORCA_WORKTREE_ID=workspace:/w\0"
               b"CLAUDE_CODE_SESSION_ID=helper-stale\0")
        fake = FakeOrcaAdapter(
            rows=[row], resolved={"handle": "new", "pty_id": "pty-new"})
        opened = []

        def counting_open(path, *a, **kw):
            opened.append(path)
            return io.BytesIO(env)

        def paths(pattern):
            return [proc] if pattern == "/proc/[0-9]*/environ" else []

        with mock.patch.object(seat.glob, "glob", side_effect=paths), \
                mock.patch("builtins.open", counting_open):
            _pane, fields, err = seat._prove_orca_rebind(d, rec, fake, [row])
        self.assertEqual(opened, [proc])
        self.assertIsNone(fields, "helper-only session testimony must not bind")
        self.assertIn("helper-stale", err)
        # CONTROL: the same instrument over the recordless call DOES see a read
        self._record(d)
        seen = []
        with mock.patch("builtins.open", lambda path, *a, **kw: (
                seen.append(path) or io.BytesIO(env))):
            seat_runtime._session_record_root(d)
        self.assertEqual(seen, [os.path.join(d, "spawn.json")])


if __name__ == "__main__":
    unittest.main()
