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


class FakeProc:
    def __init__(self, stdout, rc=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, rc, stderr


class FakeAdapter:
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

    def read(self, handle, limit=3000):
        return ""

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
            "HELM_SPAWN_SEND_DELAY", "HELM_CHAT_NAME")


class SpawnBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-spawn-")
        self._env = {k: os.environ.get(k) for k in ENV_KEYS}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm-home")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_SPAWN_SEND_DELAY"] = "0"
        for k in ("MELD_HOME", "MELD_CHAT_DIR", "HELM_CHAT_NAME"):
            os.environ.pop(k, None)
        self.timer = mock.patch.object(seat, "_ensure_autocompact_timer")
        self.ensure_timer = self.timer.start()

    def tearDown(self):
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
        popen = popen or mock.Mock(return_value=mock.Mock(pid=4242))
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
        self.assertEqual(kw.get("cwd"), os.getcwd())
        wla.assert_called_once()                   # mint hygiene refreshed
        self.assertIn("HEADLESS", out)
        self.assertIn("pid 4242", out)

    def test_headless_registers_spawn_json_and_roster_mirror(self):
        d, launch = self._mint()
        rc, _, err, _, _ = self._spawn(["codex", "--room", "team-z"], None)
        self.assertEqual(rc, 0, err)
        with open(os.path.join(d, "spawn.json")) as f:
            rec = json.load(f)
        self.assertEqual(rec["harness"], "headless")
        self.assertEqual(rec["pid"], 4242)
        self.assertEqual(rec["worktree"], os.getcwd())
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
                         [(4242, seat.signal.SIGTERM)])


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
        self.assertEqual(cwd, os.getcwd())
        handle, text, enter = fake.sent[0]
        self.assertEqual(handle, "pane-1")
        self.assertTrue(enter)
        self.assertIn("helm chat wait --seat codex --follow", text)
        self.assertIn("@codex", text)
        self.assertIn("spawned codex via fake", out)
        with open(os.path.join(d, "spawn.json")) as f:
            rec = json.load(f)
        self.assertEqual(rec["harness"], "fake")
        self.assertEqual(rec["handle"], "pane-1")
        self.ensure_timer.assert_called_once()

    def test_session_start_binds_new_session_to_spawn_register(self):
        d, _ = self._mint()
        fake = FakeAdapter()
        rc, _, err, _, _ = self._spawn(["codex"], fake)
        self.assertEqual(rc, 0, err)
        with open(os.path.join(d, "spawn.json")) as f:
            self.assertIsNone(json.load(f)["session"])
        from helm import seats
        seats.join(session="session-live", seat="codex", cwd=os.getcwd())
        with open(os.path.join(d, "spawn.json")) as f:
            self.assertEqual(json.load(f)["session"], "session-live")

    def test_live_session_identity_reads_only_proven_process_orca_keys(self):
        from helm import sessions
        d, _ = self._mint()
        sessions_dir = os.path.join(d, "claude", "sessions")
        os.makedirs(sessions_dir, exist_ok=True)
        with open(os.path.join(sessions_dir, "4242.json"), "w") as f:
            json.dump({"sessionId": "session-live", "pid": 4242,
                       "procStart": "123"}, f)
        real_open = open

        def open_selected(path, *args, **kwargs):
            if path == "/proc/4242/environ":
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
        self.assertEqual(identity, {"pid": 4242, "pane_key": "tab:leaf",
                                    "worktree_id": "workspace:/w"})
        self.assertNotIn("SECRET", identity)
        alive.assert_called_once_with(4242, "123")

    def test_session_start_persists_orca_remint_identity(self):
        d, _ = self._mint()
        self._record(d, harness_name="orca")
        with mock.patch.dict(os.environ, {
                "ORCA_PANE_KEY": "tab-1:leaf-1",
                "ORCA_WORKTREE_ID": "workspace:/w"}, clear=False):
            self.assertTrue(seat._bind_spawn_session("codex", "session-live"))
        with open(os.path.join(d, "spawn.json")) as f:
            rec = json.load(f)
        self.assertEqual(rec["session"], "session-live")
        self.assertEqual(rec["pane_key"], "tab-1:leaf-1")
        self.assertEqual(rec["worktree_id"], "workspace:/w")

    def test_stale_orca_handle_repairs_from_exact_live_session(self):
        d, _ = self._mint()
        sid = "8d2e1ff0-46c3-45b5-b817-8d82d7bc8d74"
        self._record(d, handle="old", harness_name="orca", session=sid)
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
        self.assertEqual(fake.pane_keys,
                         ["old-tab:old-leaf", "old-tab:old-leaf"])
        with open(os.path.join(d, "spawn.json")) as f:
            rec = json.load(f)
        self.assertEqual(rec["handle"], "new")
        self.assertEqual(rec["pane_key"], "old-tab:old-leaf")
        self.assertEqual(rec["pty_id"], "pty-1")
        self.assertEqual(rec["session"], sid)

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
        """The real OrcaAdapter under spawn: terminal list (reap scan) +
        create + send --enter, exact argv, subprocess fully mocked."""
        d, launch = self._mint()
        replies = [FakeProc(_orca_reply({"terminals": []})),
                   FakeProc(_orca_reply({"terminal": {"handle": "t7"}})),
                   FakeProc(_orca_reply({}))]
        ad = harness.OrcaAdapter("/fake/bin/orca")
        with mock.patch.object(harness.subprocess, "run",
                               side_effect=replies) as run:
            rc, out, err, _, _ = self._spawn(["codex"], ad)
        self.assertEqual(rc, 0, err)
        calls = [c[0][0] for c in run.call_args_list]
        self.assertEqual(calls[0], ["/fake/bin/orca", "terminal", "list",
                                    "--json"])
        self.assertEqual(calls[1], ["/fake/bin/orca", "terminal", "create",
                                    "--worktree", "path:" + os.getcwd(),
                                    "--title", "codex", "--command",
                                    shlex.quote(launch), "--json"])
        self.assertEqual(calls[2][:6], ["/fake/bin/orca", "terminal", "send",
                                        "--terminal", "t7", "--text"])
        self.assertIn("helm chat wait --seat codex --follow", calls[2][6])
        self.assertEqual(calls[2][7:], ["--enter", "--json"])

    def test_herdr_path_end_to_end_exact_cli_calls(self):
        """The real HerdrAdapter under spawn: pane list + agent start +
        pane run (text + Enter), exact argv, subprocess fully mocked."""
        d, launch = self._mint()
        replies = [FakeProc(_herdr_reply({"panes": []})),
                   FakeProc(_herdr_reply({"agent": {"pane_id": "w1:p1"}})),
                   FakeProc(_herdr_reply({}))]
        ad = harness.HerdrAdapter("/fake/bin/herdr")
        with mock.patch.object(harness.subprocess, "run",
                               side_effect=replies) as run:
            rc, out, err, _, _ = self._spawn(["codex"], ad)
        self.assertEqual(rc, 0, err)
        calls = [c[0][0] for c in run.call_args_list]
        self.assertEqual(calls[0], ["/fake/bin/herdr", "pane", "list"])
        self.assertEqual(calls[1], ["/fake/bin/herdr", "agent", "start",
                                    "codex", "--cwd", os.getcwd(),
                                    "--no-focus", "--", "sh", "-lc",
                                    shlex.quote(launch)])
        self.assertEqual(calls[2][:4], ["/fake/bin/herdr", "pane", "run",
                                        "w1:p1"])
        self.assertIn("helm chat wait --seat codex --follow", calls[2][4])


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
        self.assertIn("codex: headless pid 4242 — LIVE", out)
        self.assertIn("room team-z", out)
        self.assertIn(os.getcwd(), out)

    def test_where_json_is_machine_readable(self):
        self._mint()
        self._spawn(["codex"], None)
        with mock.patch.object(seat, "_recorded_pid_alive", return_value=False):
            rc, out, err = self._where(["codex", "--json"])
        self.assertEqual(rc, 0, err)
        got = json.loads(out)
        self.assertEqual(got["harness"], "headless")
        self.assertEqual(got["pid"], 4242)
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
