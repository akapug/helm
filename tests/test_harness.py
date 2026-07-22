"""Hermetic tests for helm.harness (the metaharness adapter seam) + the
`helm seat resume` verb. Every orca/herdr CLI call is a patched
subprocess.run; detect() gets a synthetic `which`; no real metaharness is
ever touched, no pane is ever spawned."""
import contextlib
import io
import json
import os
import shlex
import shutil
import tempfile
import unittest
from unittest import mock

from helm import doctor, harness, seat


def _orca_reply(result, ok=True):
    return json.dumps({"id": "x", "ok": ok, "result": result})


def _herdr_reply(result):
    return json.dumps({"id": "cli:x", "result": result})


def _herdr_error(code="not_found", message="no such pane"):
    return json.dumps({"id": "cli:x", "error": {"code": code, "message": message}})


class FakeProc:
    def __init__(self, stdout, rc=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, rc, stderr


class OrcaAdapterTest(unittest.TestCase):
    def setUp(self):
        self.ad = harness.OrcaAdapter("/fake/bin/orca")

    def _patch(self, stdout, rc=0, stderr=""):
        return mock.patch.object(harness.subprocess, "run",
                                 return_value=FakeProc(stdout, rc, stderr))

    def test_spawn_builds_exact_command_and_parses_handle(self):
        with self._patch(_orca_reply({"terminal": {"handle": "term_abc"}})) as run:
            h = self.ad.spawn("/s/launch.sh --continue", title="codex", cwd="/w")
        self.assertEqual(h, "term_abc")
        self.assertEqual(run.call_args[0][0],
                         ["/fake/bin/orca", "terminal", "create",
                          "--worktree", "path:/w", "--title", "codex",
                          "--command", "/s/launch.sh --continue", "--json"])

    def test_spawn_omits_absent_title_and_cwd(self):
        with self._patch(_orca_reply({"terminal": {"handle": "t1"}})) as run:
            self.ad.spawn("true")
        self.assertEqual(run.call_args[0][0],
                         ["/fake/bin/orca", "terminal", "create",
                          "--command", "true", "--json"])

    def test_read_builds_exact_command_and_joins_tail(self):
        with self._patch(_orca_reply({"terminal": {
                "handle": "t1", "tail": ["line one", "line two"]}})) as run:
            text = self.ad.read("t1", limit=500)
        self.assertEqual(text, "line one\nline two")
        self.assertEqual(run.call_args[0][0],
                         ["/fake/bin/orca", "terminal", "read", "--terminal",
                          "t1", "--limit", "500", "--json"])

    def test_send_with_and_without_enter(self):
        with self._patch(_orca_reply({})) as run:
            self.ad.send("t1", "hello")
            self.assertEqual(run.call_args[0][0],
                             ["/fake/bin/orca", "terminal", "send",
                              "--terminal", "t1", "--text", "hello",
                              "--enter", "--json"])
            self.ad.send("t1", "raw", enter=False)
            self.assertEqual(run.call_args[0][0],
                             ["/fake/bin/orca", "terminal", "send",
                              "--terminal", "t1", "--text", "raw", "--json"])

    def test_stop_builds_close(self):
        with self._patch(_orca_reply({})) as run:
            self.ad.stop("t1")
        self.assertEqual(run.call_args[0][0],
                         ["/fake/bin/orca", "terminal", "close",
                          "--terminal", "t1", "--json"])

    def test_list_parses_rows(self):
        with self._patch(_orca_reply({"terminals": [
                {"handle": "t1", "title": "codex", "connected": True},
                {"handle": "t2", "connected": False}]})):
            rows = self.ad.list()
        self.assertEqual(rows, [
            {"handle": "t1", "title": "codex", "preview": "",
             "status": "connected"},
            {"handle": "t2", "title": "", "preview": "",
             "status": "disconnected"}])

    def test_nonzero_rc_raises(self):
        with self._patch("", rc=1, stderr="boom"):
            with self.assertRaises(harness.HarnessError):
                self.ad.list()

    def test_ok_false_raises(self):
        with self._patch(_orca_reply({}, ok=False)):
            with self.assertRaises(harness.HarnessError):
                self.ad.stop("t1")

    def test_bad_json_raises(self):
        with self._patch("not json"):
            with self.assertRaises(harness.HarnessError):
                self.ad.list()

    def test_spawn_missing_handle_is_loud(self):
        with self._patch(_orca_reply({"terminal": {}})):
            with self.assertRaises(harness.HarnessError):
                self.ad.spawn("true")


class HerdrAdapterTest(unittest.TestCase):
    def setUp(self):
        self.ad = harness.HerdrAdapter("/fake/bin/herdr")

    def _patch(self, stdout, rc=0, stderr=""):
        return mock.patch.object(harness.subprocess, "run",
                                 return_value=FakeProc(stdout, rc, stderr))

    def test_spawn_builds_agent_start_and_parses_pane_id(self):
        reply = _herdr_reply({"type": "agent_started",
                              "agent": {"name": "codex", "pane_id": "w3:p9",
                                        "terminal_id": "term_ff"},
                              "argv": ["sh", "-lc", "cmd"]})
        with self._patch(reply) as run:
            h = self.ad.spawn("/s/launch.sh --continue", title="codex", cwd="/w")
        self.assertEqual(h, "w3:p9")
        self.assertEqual(run.call_args[0][0],
                         ["/fake/bin/herdr", "agent", "start", "codex",
                          "--cwd", "/w", "--no-focus", "--",
                          "sh", "-lc", "/s/launch.sh --continue"])

    def test_list_parses_pane_rows(self):
        reply = _herdr_reply({"type": "pane_list", "panes": [
            {"pane_id": "w3:p1", "label": "codex", "agent_status": "working"},
            {"pane_id": "w3:p2"}]})
        with self._patch(reply) as run:
            rows = self.ad.list()
        self.assertEqual(run.call_args[0][0], ["/fake/bin/herdr", "pane", "list"])
        self.assertEqual(rows, [
            {"handle": "w3:p1", "title": "codex", "status": "working"},
            {"handle": "w3:p2", "title": "", "status": "unknown"}])

    def test_read_builds_pane_read_and_returns_text(self):
        reply = _herdr_reply({"type": "pane_read",
                              "read": {"text": "tail text\nhere", "pane_id": "w3:p1"}})
        with self._patch(reply) as run:
            text = self.ad.read("w3:p1", limit=40)
        self.assertEqual(text, "tail text\nhere")
        self.assertEqual(run.call_args[0][0],
                         ["/fake/bin/herdr", "pane", "read", "w3:p1",
                          "--source", "recent", "--lines", "40"])

    def test_send_enter_uses_pane_run_literal_uses_send_text(self):
        with self._patch(_herdr_reply({})) as run:
            self.ad.send("w3:p1", "make test")
            self.assertEqual(run.call_args[0][0],
                             ["/fake/bin/herdr", "pane", "run", "w3:p1", "make test"])
            self.ad.send("w3:p1", "y", enter=False)
            self.assertEqual(run.call_args[0][0],
                             ["/fake/bin/herdr", "pane", "send-text", "w3:p1", "y"])

    def test_stop_builds_pane_close(self):
        with self._patch(_herdr_reply({})) as run:
            self.ad.stop("w3:p1")
        self.assertEqual(run.call_args[0][0],
                         ["/fake/bin/herdr", "pane", "close", "w3:p1"])

    def test_error_envelope_raises_with_message(self):
        with self._patch(_herdr_error(message="no such pane")):
            with self.assertRaisesRegex(harness.HarnessError, "no such pane"):
                self.ad.read("w9:p9")


class DetectTest(unittest.TestCase):
    @staticmethod
    def _which(installed):
        return lambda b: installed.get(b)

    BOTH = {"orca": "/bin/orca", "herdr": "/bin/herdr"}

    def test_prefers_orca_by_default(self):
        ad = harness.detect(env={}, which=self._which(self.BOTH))
        self.assertIsInstance(ad, harness.OrcaAdapter)
        self.assertEqual(ad.path, "/bin/orca")

    def test_inside_herdr_session_prefers_herdr(self):
        ad = harness.detect(env={"HERDR_ENV": "1"}, which=self._which(self.BOTH))
        self.assertIsInstance(ad, harness.HerdrAdapter)

    def test_picks_whichever_is_installed(self):
        ad = harness.detect(env={}, which=self._which({"herdr": "/bin/herdr"}))
        self.assertIsInstance(ad, harness.HerdrAdapter)
        ad = harness.detect(env={}, which=self._which({"orca": "/bin/orca"}))
        self.assertIsInstance(ad, harness.OrcaAdapter)

    def test_none_installed_returns_none(self):
        self.assertIsNone(harness.detect(env={}, which=self._which({})))

    def test_env_override_wins(self):
        ad = harness.detect(env={"HELM_METAHARNESS": "herdr"},
                            which=self._which(self.BOTH))
        self.assertIsInstance(ad, harness.HerdrAdapter)
        self.assertIsNone(harness.detect(env={"HELM_METAHARNESS": "none"},
                                         which=self._which(self.BOTH)))

    def test_env_override_absent_binary_returns_none(self):
        self.assertIsNone(harness.detect(env={"HELM_METAHARNESS": "orca"},
                                         which=self._which({"herdr": "/bin/herdr"})))


class DoctorMetaharnessTest(unittest.TestCase):
    def test_detected_reports_ok_with_name_and_others(self):
        res = doctor.check_metaharness(
            detect=lambda: harness.OrcaAdapter("/bin/orca"),
            which=lambda b: {"herdr": "/bin/herdr"}.get(b))
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0][0], doctor.OK)
        self.assertIn("metaharness: orca (/bin/orca)", res[0][1])
        self.assertIn("also present: herdr", res[0][1])

    def test_none_warns_with_companion_recommendation(self):
        res = doctor.check_metaharness(detect=lambda: None, which=lambda b: None)
        self.assertEqual(res[0][0], doctor.WARN)
        self.assertIn("orca", res[0][1])
        self.assertIn("herdr", res[0][1])


class FakeAdapter:
    """Records the uniform pane ops seat resume drives."""
    name, path = "fake", "/bin/fake"

    def __init__(self, rows=()):
        self.rows, self.spawned, self.stopped = list(rows), [], []

    def spawn(self, command, title=None, cwd=None):
        self.spawned.append((command, title, cwd))
        return "pane-1"

    def list(self):
        return list(self.rows)

    def read(self, handle, limit=3000):
        return ""

    def send(self, handle, text, enter=True):
        pass

    def stop(self, handle):
        self.stopped.append(handle)


class SeatResumeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-resume-")
        self._env = {k: os.environ.get(k) for k in ("HELM_HOME", "MELD_HOME")}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm-home")
        os.environ.pop("MELD_HOME", None)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mint(self, seat_name="codex", family="codex"):
        d = seat._instance_dir(family, seat_name)
        os.makedirs(d, exist_ok=True)
        launch = os.path.join(d, "launch.sh")
        with open(launch, "w") as f:
            f.write("#!/bin/sh\nexec env FAKE=1 claude \"$@\"\n")
        os.chmod(launch, 0o700)
        return d, launch

    def _resume(self, args, adapter):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(seat, "_write_launch_assets") as wla, \
                mock.patch.object(harness, "detect", return_value=adapter), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(["resume"] + list(args))
        return rc, out.getvalue(), err.getvalue(), wla

    def test_resume_composes_continue_line_via_adapter(self):
        d, launch = self._mint()
        fake = FakeAdapter()
        rc, out, err, wla = self._resume(["codex"], fake)
        self.assertEqual(rc, 0, err)
        command, title, cwd = fake.spawned[0]
        self.assertEqual(command, "%s --continue" % shlex.quote(launch))
        self.assertEqual(title, "codex")
        self.assertEqual(cwd, os.getcwd())   # no session -> caller's cwd
        wla.assert_called_once()             # env refreshed to latest launch.sh
        self.assertIn("resumed codex via fake", out)
        self.assertIn("pane-1", out)

    def test_resume_uses_session_id_and_sniffed_cwd_when_resolvable(self):
        d, launch = self._mint()
        sid = "0199aaaa-bbbb-cccc-dddd-eeeeffff0000"
        proj = os.path.join(d, "claude", "projects", "-work-spot")
        os.makedirs(proj)
        with open(os.path.join(proj, sid + ".jsonl"), "w") as f:
            f.write(json.dumps({"cwd": "/work/spot", "type": "user"}) + "\n")
        fake = FakeAdapter()
        rc, out, err, _ = self._resume(["codex"], fake)
        self.assertEqual(rc, 0, err)
        command, _, cwd = fake.spawned[0]
        self.assertEqual(command, "%s --resume %s" % (shlex.quote(launch), sid))
        self.assertEqual(cwd, "/work/spot")
        self.assertIn("--resume", out)

    def test_resume_stops_stale_same_titled_pane_first(self):
        self._mint()
        fake = FakeAdapter(rows=[{"handle": "p9", "title": "codex", "status": "idle"},
                                 {"handle": "p2", "title": "other", "status": "idle"}])
        rc, out, err, _ = self._resume(["codex"], fake)
        self.assertEqual(rc, 0, err)
        self.assertEqual(fake.stopped, ["p9"])

    def test_resume_stops_stale_pane_before_reminting(self):
        """The re-mint rewrites launch.sh; a still-running stale pane's `sh`
        is reading that very file and the metaharness close is not
        process-synchronous — the stop MUST land before the rewrite."""
        d, _ = self._mint()
        fake = FakeAdapter(rows=[{"handle": "p9", "title": "codex",
                                  "status": "idle"}])
        order = []
        orig_wla = seat._write_launch_assets
        def spy_wla(*a, **k):
            order.append("remint")
        def spy_stop(h):
            order.append("stop")
            fake.stopped.append(h)
        fake.stop = spy_stop
        with mock.patch.object(seat, "_write_launch_assets",
                               side_effect=spy_wla) as wla, \
                mock.patch.object(harness, "detect", return_value=fake):
            rc = seat.cmd_seat(["resume", "codex"])
        self.assertEqual(rc, 0)
        self.assertEqual(order, ["stop", "remint"])  # stop strictly first

    def test_launch_sh_written_atomically(self):
        """The re-minted launch.sh is a tmp+rename, never an O_TRUNC-in-place
        a running reader can catch half-written; mode stays 0700, no litter."""
        d, launch = self._mint()
        seat._write_launch_sh(launch, "#!/bin/sh\nexec env X=1 claude \"$@\"\n")
        with open(launch) as f:
            self.assertIn("claude", f.read())
        import stat as _st
        self.assertEqual(_st.S_IMODE(os.stat(launch).st_mode), 0o700)
        leftovers = [n for n in os.listdir(d) if n.startswith(".launch-")]
        self.assertEqual(leftovers, [])

    def test_resume_never_mints_a_nonexistent_seat(self):
        fake = FakeAdapter()
        rc, out, err, wla = self._resume(["codex"], fake)
        self.assertEqual(rc, 1)
        self.assertEqual(fake.spawned, [])
        wla.assert_not_called()
        self.assertIn("no codex seat minted", err)

    def test_resume_unknown_seat_is_usage_error(self):
        rc, out, err, _ = self._resume(["mystery"], FakeAdapter())
        self.assertEqual(rc, 2)
        self.assertIn("unknown seat", err)

    def test_resume_without_metaharness_prints_recommendation_and_paste(self):
        _, launch = self._mint()
        rc, out, err, _ = self._resume(["codex"], None)
        self.assertEqual(rc, 1)
        self.assertIn("orca", err)
        self.assertIn("herdr", err)
        self.assertIn("%s --continue" % shlex.quote(launch), err)

    def test_resume_instance_seat_uses_instance_dir(self):
        d, launch = self._mint(seat_name="codex-2")
        self.assertIn(os.path.join("instances", "codex-2"), launch)
        fake = FakeAdapter()
        rc, out, err, _ = self._resume(["codex-2"], fake)
        self.assertEqual(rc, 0, err)
        command, title, _ = fake.spawned[0]
        self.assertEqual(command, "%s --continue" % shlex.quote(launch))
        self.assertEqual(title, "codex-2")

    def test_resume_preserves_room_homing_across_the_remint(self):
        d, launch = self._mint()
        with open(launch, "w") as f:
            f.write("#!/bin/sh\nexec env HELM_CHAT_ROOM=team-z claude \"$@\"\n")
        rc, out, err, wla = self._resume(["codex"], FakeAdapter())
        self.assertEqual(rc, 0, err)
        self.assertEqual(wla.call_args[0], ("codex", d, "team-z", "codex"))
        self.assertIsNone(wla.call_args.kwargs["room_source"])

    def test_resume_preserves_derived_room_provenance(self):
        d, launch = self._mint()
        with open(launch, "w") as f:
            f.write("#!/bin/sh\nexec env HELM_CHAT_ROOM='project room' "
                    "HELM_CHAT_ROOM_SOURCE=derived claude \"$@\"\n")
        rc, out, err, wla = self._resume(["codex"], FakeAdapter())
        self.assertEqual(rc, 0, err)
        self.assertEqual(wla.call_args[0],
                         ("codex", d, "project room", "codex"))
        self.assertEqual(wla.call_args.kwargs["room_source"], "derived")

    def test_resume_command_never_carries_the_seat_token(self):
        """TOKEN LAW: the pane command is the launch.sh PATH — the expanded
        line (ANTHROPIC_AUTH_TOKEN=…) must never cross the adapter seam."""
        d, _ = self._mint()
        with open(os.path.join(d, "token"), "w") as f:
            f.write("super-secret-token\n")
        fake = FakeAdapter()
        rc, _, err, _ = self._resume(["codex"], fake)
        self.assertEqual(rc, 0, err)
        command, title, _ = fake.spawned[0]
        self.assertNotIn("super-secret-token", command)
        self.assertNotIn("ANTHROPIC", command)


if __name__ == "__main__":
    unittest.main()
