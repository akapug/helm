"""Hermetic tests for `helm seat spawn` (the harness-agnostic self-onboarding
seat spawn) + `helm seat where` (the spawn register resolver). The harness
adapter is always mocked and subprocess.Popen is always patched — no real
pane, process, or metaharness is ever touched."""
import contextlib
import io
import json
import os
import shlex
import shutil
import tempfile
import unittest
from unittest import mock

from helm import cli, harness, seat


def _orca_reply(result, ok=True):
    return json.dumps({"id": "x", "ok": ok, "result": result})


def _herdr_reply(result):
    return json.dumps({"id": "cli:x", "result": result})



def _free_pid():
    """A pid NOTHING is using, verified, not a hopeful constant.

    This module hardcoded 4242. After a host reboot the pid space
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

    def __init__(self, rows=()):
        self.rows = list(rows)
        self.spawned, self.stopped, self.sent, self.order = [], [], [], []

    def spawn(self, command, title=None, cwd=None):
        self.spawned.append((command, title, cwd))
        self.order.append("spawn")
        return "pane-1"

    def list(self):
        return list(self.rows)

    def read(self, handle, limit=3000, timeout=60):
        return ADVANCED_PANE

    def send(self, handle, text, enter=True):
        self.sent.append((handle, text, enter))
        self.order.append("send")

    def stop(self, handle):
        self.stopped.append(handle)
        self.order.append("stop")


class FakeOrcaAdapter(FakeAdapter):
    name, path = "orca", "/bin/orca"

    def __init__(self, rows=(), resolved=None):
        super().__init__(rows)
        self.resolved = resolved or {}
        self.pane_keys = []

    def resolve_pane(self, pane_key):
        self.pane_keys.append(pane_key)
        return dict(self.resolved)


ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_SPAWN_SEND_DELAY", "HELM_CHAT_NAME",
            "HELM_SUBMIT_SETTLE_S")


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
        for k in ("MELD_HOME", "MELD_CHAT_DIR", "HELM_CHAT_NAME"):
            os.environ.pop(k, None)
        self.timer = mock.patch.object(seat, "_ensure_autocompact_timer")
        self.ensure_timer = self.timer.start()
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

    def _record(self, d, handle="p9", harness_name="fake", session=None,
                **extra):
        rec = {"v": 1, "seat": "codex", "harness": harness_name,
               "handle": handle, "worktree": os.getcwd(), "room": "main"}
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
        self._mint()
        kills = []
        with mock.patch.object(seat, "_register_spawn", return_value=False), \
                mock.patch.object(seat.os, "kill",
                                  side_effect=lambda p, s: kills.append((p, s))):
            rc, _, _, _, _ = self._spawn(["codex"], None)
        self.assertEqual(rc, 1)
        self.assertEqual([x for x in kills if x[1]],
                         [(_FAKE_PID, seat.signal.SIGTERM)])


class AdapterSpawnTest(SpawnBase):
    """Paths 2+3 — a metaharness is present: the seam gets exactly two calls
    (spawn the pane on the launch.sh PATH, send the onboarding)."""

    def test_adapter_spawns_pane_then_sends_onboarding(self):
        d, launch = self._mint()
        fake = FakeAdapter()
        rc, out, err, wla, popen = self._spawn(["codex"], fake)
        self.assertEqual(rc, 0, err)
        popen.assert_not_called()                  # never headless-doubles
        command, title, cwd = fake.spawned[0]
        self.assertEqual(command, shlex.quote(launch))
        self.assertEqual(title, "codex")
        self.assertEqual(cwd, self.home)
        # The onboarding brief is TYPED (no Enter), then submitted by a bare
        # Enter of its own — and rc 0 above means the composer read back clear.
        self.assertEqual(len(fake.sent), 2)
        handle, text, enter = fake.sent[0]
        self.assertEqual(handle, "pane-1")
        self.assertFalse(enter, "the text leg must NOT carry Enter")
        self.assertEqual(fake.sent[1], ("pane-1", "", True))
        self.assertIn("helm chat wait --seat codex --follow", text)
        self.assertIn("@codex", text)
        self.assertIn("onboarding submitted", out)
        self.assertIn("spawned codex via fake", out)
        with open(os.path.join(d, "spawn.json")) as f:
            rec = json.load(f)
        self.assertEqual(rec["harness"], "fake")
        self.assertEqual(rec["handle"], "pane-1")
        self.ensure_timer.assert_called_once()

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
        self.assertEqual(identity, {"pid": _FAKE_PID, "pane_key": "tab:leaf",
                                    "worktree_id": "workspace:/w"})
        self.assertNotIn("SECRET", identity)
        alive.assert_called_once_with(_FAKE_PID, "123")

    def test_spawn_backfills_session_when_sessionstart_won_the_race(self):
        d, _ = self._mint()
        self._record(d, harness_name="orca")
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
        self._record(d, harness_name="orca")
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

    def test_unrelated_session_start_cannot_rebind_registered_pane(self):
        d, _ = self._mint()
        self._record(d, harness_name="orca", session="good-session",
                     pane_key="tab:leaf", worktree_id="workspace:/w")
        fields = {"handle": "p9", "pane_key": "tab:leaf",
                  "pty_id": "pty-1", "worktree_id": "workspace:/w"}
        with mock.patch.object(seat, "_sessionstart_pane_fields",
                               return_value=(fields, None)):
            self.assertFalse(seat._bind_spawn_session(
                "codex", "other-session", source="startup"))
            self.assertTrue(seat._bind_spawn_session(
                "codex", "clear-session", source="clear"))
        with open(os.path.join(d, "spawn.json")) as f:
            rec = json.load(f)
        self.assertEqual(rec["session"], "clear-session")
        self.assertEqual(rec["handle"], "p9")

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
        prove.assert_called_once_with(d, measured)
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
        self.assertIn("--room team-q", text)
        with open(os.path.join(d, "spawn.json")) as f:
            rec = json.load(f)
        self.assertEqual(rec["room"], "team-q")

    def test_spawn_remint_preserves_multi_shape(self):
        """The re-mint must recover --multi from the old launch: pin absence
        stays pinless, while a normal seat stays pinned."""
        self._mint(multi=True)
        rc, _, err, wla, _ = self._spawn(["codex"], FakeAdapter())
        self.assertEqual(rc, 0, err)
        self.assertIs(wla.call_args.kwargs["multi"], True)
        self._mint(multi=False)
        rc, _, err, wla, _ = self._spawn(["codex"], FakeAdapter())
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
        self._mint()
        fake = FakeAdapter()
        with mock.patch.object(seat, "_register_spawn", return_value=False):
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
        replies = [FakeProc(_orca_reply({"terminals": []})),
                   FakeProc(_orca_reply({"terminal": {"handle": "t7"}})),
                   advanced,                           # the composer PRE-read
                   FakeProc(_orca_reply({})),          # text, no Enter
                   FakeProc(_orca_reply({})),          # the bare Enter
                   advanced]                           # the read-back
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
                                    shlex.quote(launch), "--json"])
        self.assertEqual(calls[2][:5], ["/fake/bin/orca", "terminal", "read",
                                        "--terminal", "t7"],
                         "the composer is read BEFORE anything is typed")
        self.assertEqual(calls[3][:6], ["/fake/bin/orca", "terminal", "send",
                                        "--terminal", "t7", "--text"])
        self.assertIn("helm chat wait --seat codex --follow", calls[3][6])
        self.assertEqual(calls[3][7:], ["--json"],
                         "the text leg must NOT carry --enter")
        self.assertEqual(calls[4], ["/fake/bin/orca", "terminal", "send",
                                    "--terminal", "t7", "--text", "",
                                    "--enter", "--json"])
        self.assertEqual(calls[5][:5], ["/fake/bin/orca", "terminal", "read",
                                        "--terminal", "t7"])

    def test_herdr_path_end_to_end_exact_cli_calls(self):
        """The real HerdrAdapter under spawn: pane list + agent start + the
        SPLIT submit (send-text, then a bare `pane run`), then the composer
        read-back. Exact argv, subprocess fully mocked."""
        d, launch = self._mint()
        advanced = FakeProc(_herdr_reply({"read": {"text": ADVANCED_PANE}}))
        replies = [FakeProc(_herdr_reply({"panes": []})),
                   FakeProc(_herdr_reply({"agent": {"pane_id": "w1:p1"}})),
                   advanced,                            # the composer PRE-read
                   FakeProc(_herdr_reply({})),          # send-text, no Enter
                   FakeProc(_herdr_reply({})),          # the bare Enter
                   advanced]                            # the read-back
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
                                    shlex.quote(launch)])
        # THE SPLIT, at herdr's own argv: `pane send-text` is literal
        # keystrokes with no Enter; `pane run` is text+Enter, so a bare Enter
        # is `pane run <handle> ""`. Both adapters inherit ONE submit, and this
        # is what that one definition looks like on the other CLI.
        self.assertEqual(calls[2][:5], ["/fake/bin/herdr", "pane", "read",
                                        "w1:p1", "--source"],
                         "the composer is read BEFORE anything is typed")
        self.assertEqual(calls[3][:4], ["/fake/bin/herdr", "pane", "send-text",
                                        "w1:p1"])
        self.assertIn("helm chat wait --seat codex --follow", calls[3][4])
        self.assertEqual(calls[4], ["/fake/bin/herdr", "pane", "run",
                                    "w1:p1", ""])
        self.assertEqual(calls[5][:5], ["/fake/bin/herdr", "pane", "read",
                                        "w1:p1", "--source"])


class DryRunTest(SpawnBase):
    def test_print_refuses_title_only_identity_without_spawning(self):
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
        self.assertIn("fake.send(<handle>", out)
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
        self.assertIn("first-prompt", out)


class HomeWorktreeSpawnTest(SpawnBase):
    """A spawn with no
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

    def test_where_without_record_points_at_spawn(self):
        self._mint()
        rc, out, err = self._where(["codex"])
        self.assertEqual(rc, 1)
        self.assertIn("no spawn record", err)
        self.assertIn("helm seat spawn codex", err)

    def test_where_unknown_seat_is_usage_error(self):
        rc, out, err = self._where(["mystery"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown seat", err)

    def test_where_rejects_unknown_option(self):
        rc, _, err = self._where(["codex", "--surprise"])
        self.assertEqual(rc, 2)
        self.assertIn("usage: helm seat where", err)


class RebindTest(SpawnBase):
    """`helm seat rebind` — the REBOOT verb.

    helm keys a seat's register on its orca HANDLE, which is per-pane and dies
    with the machine. Measured the morning after a host reboot:
    `helm seat where` reported 6 of 7 seats GONE while two of them were
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
        self.assertIn("seat rebind --all --apply", service)
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
            ident, err = seat._live_seat_orca_identity("codex")
        self.assertIsNone(ident, "two distinct panes must never resolve")
        self.assertIn("2 distinct live panes", err)
        self.assertIn("refusing to guess", err)

    def test_one_seats_many_processes_are_one_pane_not_an_ambiguity(self):
        """A seat legitimately owns several processes — the pane, its beacon,
        its hooks — all carrying the SAME pane identity. Collapsing on pid
        count would refuse every healthy seat; collapse on the identity."""
        same = (b"HELM_CHAT_NAME=codex\0ORCA_PANE_KEY=pane-A\0"
                b"ORCA_WORKTREE_ID=w1\0")
        envs = {"/proc/101/environ": same, "/proc/202/environ": same,
                "/proc/303/environ": same}

        def fake_open(path, *a, **kw):
            return io.BytesIO(envs[path])

        with mock.patch.object(seat.glob, "glob", return_value=list(envs)), \
                mock.patch("builtins.open", fake_open):
            ident, err = seat._live_seat_orca_identity("codex")
        self.assertIsNone(err, err)
        self.assertEqual(ident["pane_key"], "pane-A")

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
        self.assertEqual(command, shlex.quote(launch))
        self.assertEqual(title, "codex-2")
        _, text, _ = fake.sent[0]
        self.assertIn("helm chat wait --seat codex-2 --follow", text)
        self.assertIn("@codex-2", text)

    def test_onboarding_prompt_contents(self):
        p = seat.onboarding_prompt("codex")
        self.assertIn("helm chat wait --seat codex --follow", p)   # beacon
        self.assertIn("helm chat read", p)                         # catch up
        self.assertIn("helm chat post", p)                         # announce
        self.assertIn("@codex", p)                                 # take work
        self.assertNotIn("\n", p)          # single keystroke burst, one line
        q = seat.onboarding_prompt("codex", room="team-x")
        self.assertIn("--room team-x", q)

    def test_onboarding_takes_work_from_the_ledger_fold_not_history(self):
        """THE RECOVERED-SEAT TRAP, three live instances inside 90 minutes:
        a fresh session reads history, and stale rows read as
        invitations. One seat rebuilt an already-superseded lane; another claimed
        a CANCELLED row, then assembled a LAND READY from a cancelled vehicle
        + an ungated approve + a tip that no longer resolves. The onboarding
        prompt was the vector — it said "rows addressed @you are yours" with
        nothing distinguishing live rows from dead ones.

        The prompt must now direct work-taking through the LEDGER FOLD
        (`helm dispatch list --open`) and state the status law: cancelled /
        superseded / verdicted rows are DEAD however open the chat reads."""
        p = seat.onboarding_prompt("codex")
        # The fold is the work list...
        self.assertIn("helm dispatch list --open", p)
        # ...the status decides, and each dead state is named...
        self.assertIn("CURRENT STATUS decides", p)
        for word in ("cancelled", "superseded", "verdicted"):
            self.assertIn(word, p)
        # ...and the room is explicitly demoted to context.
        self.assertIn("never your work list", p)
        # The fold's own failure mode, an adversarial review's finding: "ONLY
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

    def test_helm_help_lists_spawn_and_where(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["--help"])
        self.assertEqual(rc, 0)
        seat_line = next(l for l in out.getvalue().splitlines()
                         if l.strip().startswith("seat "))
        self.assertIn("spawn", seat_line)
        self.assertIn("where", seat_line)


if __name__ == "__main__":
    unittest.main()
