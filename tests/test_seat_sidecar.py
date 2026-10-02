#!/usr/bin/env python3
"""The cursor family's bridge is supervised like its proxy (task/1056).

These arms run a REAL second process: a small HTTP server written into the
seat's vendored-artifact directory and started by helm/seat_sidecar.py through
the family's own sidecar declaration, with only the runtime swapped (python3
for bun) and the port moved to a free one. So "starts it", "restarts it when
it is killed" and "leaves alone what it does not own" are measured on a pid
and a socket, never on a mock of the thing under test.
"""
import contextlib
import hashlib
import io
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from tests import _tmphome  # noqa: F401 — must precede helm.*
from helm import (proxywatch, seat, seat_catalog, seat_health, seat_proxy,
                  seat_sidecar)

FAMILY = "cursor"  # noqa: SEAT_NAME — the catalog FAMILY key under test, never a seat

#: The stand-in bridge. GET /v1/models answers 200, or 502 while a file named
#: `refuse` sits beside it (the shape of a bridge whose Cursor login is dead),
#: or 502 ONCE for a file named `refuse-once` (the real bridge's first model
#: discovery after a start, measured failing once and then serving). A file
#: named `slow-once` holds ONE answer for SLOW_S, single-threaded like the real
#: bridge, whose /models runs a synchronous curl on its only event loop. Each
#: GET appends a line to `hits`, so a surface's probe count is measured.
SLOW_S = 3.0
FAKE_BRIDGE = r'''
import http.server, json, os, sys, time

with open("environ", "w") as f:
    json.dump(sorted(os.environ), f)

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        with open("hits", "a") as f:
            f.write(self.path + "\n")
        if os.path.exists("slow-once"):
            os.remove("slow-once")
            time.sleep(%(slow)s)
        code = 502 if os.path.exists("refuse") else 200
        if os.path.exists("refuse-once"):
            os.remove("refuse-once")
            code = 502
        body = b'{"object": "list", "data": []}'
        self.send_response(code if self.path == "/v1/models" else 404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

http.server.HTTPServer(("127.0.0.1", int(sys.argv[-1])), H).serve_forever()
''' % {"slow": SLOW_S}


#: The stand-in's vetted commit: any 40 hex digits, written into its .git.
PIN = "5eed" * 10
#: The stand-in checkout's tests and docs, which the vetting does not hash,
#: and the files its server writes at runtime, which its .gitignore names.
EXCLUDE = ("test/", "README.md")
RUNTIME_WRITES = ("hits", "refuse", "refuse-once", "slow-once", "environ")


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", root, "-c", "core.hooksPath=/dev/null",
         "-c", "user.name=stand-in", "-c", "user.email=stand-in@example.com",
         "-c", "commit.gpgsign=false"] + list(args),
        check=True, capture_output=True).stdout


def _runtime_digest(root, exclude=EXCLUDE):
    """The vetted-build digest, written here from its definition rather than
    imported: sha256 over "<path>\\0<sha256 of its bytes>\\n" for every file
    `git ls-files --cached --others --exclude-standard` lists outside the
    excluded prefixes, in path order."""
    listed = _git(root, "ls-files", "-z", "--cached", "--others",
                  "--exclude-standard").split(b"\0")
    paths = sorted(p.decode() for p in listed
                   if p and not p.decode().startswith(exclude))
    h = hashlib.sha256()
    for rel in paths:
        with open(os.path.join(root, rel), "rb") as f:
            h.update(("%s\0%s\n" % (rel, hashlib.sha256(f.read()).hexdigest()))
                     .encode())
    return h.hexdigest()


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class SidecarBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-sidecar-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = mock.patch.dict(os.environ, {
            "HELM_HOME": os.path.join(self.tmp, "home"),
            "HELM_ENSURE_QUIET_HEARTBEAT_DIR": os.path.join(self.tmp, "hb")})
        env.start()
        self.addCleanup(env.stop)
        self.port = _free_port()
        # A REAL CHECKOUT: the runtime digest is taken over what git lists,
        # so the stand-in is a committed repository whose HEAD is then moved
        # onto PIN (git lists files from the index, not from HEAD).
        self.home = seat.seat_dir(FAMILY)
        self.assertTrue(self.home.startswith(self.tmp), self.home)
        self.artifact = os.path.join(self.home, "bridge")
        os.makedirs(os.path.join(self.artifact, "test"))
        self._write("fake_bridge.py", FAKE_BRIDGE)
        self._write("helper.py", "# a runtime file no patch names\n")
        self._write("test/test_fake.py", "# a test, not runtime\n")
        self._write("README.md", "docs, not runtime\n")
        self._write(".gitignore", "".join(n + "\n" for n in RUNTIME_WRITES))
        _git(self.artifact, "init", "-q", "-b", "main")
        _git(self.artifact, "add", "-A")
        _git(self.artifact, "commit", "-q", "--no-verify", "-m", "stand-in")
        self.git = os.path.join(self.artifact, ".git")
        self._write(".git/refs/heads/main", PIN + "\n")
        live = seat.FAMILIES[FAMILY]
        vetted = {"fake_bridge.py": {
            "sha256": hashlib.sha256(FAKE_BRIDGE.encode()).hexdigest(),
            "patches": ("stand-in",)}}
        fam = dict(live, base_url="http://127.0.0.1:%d/v1" % self.port,
                   sidecar=dict(live["sidecar"], runtime="python3",
                                argv=("fake_bridge.py", "serve"), needs=(),
                                pin=PIN, required_patches=vetted,
                                runtime_sha256=_runtime_digest(self.artifact),
                                runtime_exclude=EXCLUDE))
        table = mock.patch.dict(seat.FAMILIES, {FAMILY: fam})
        table.start()
        self.addCleanup(table.stop)
        # the declaration under test is the real one with the runtime, the
        # command and the vetted build swapped for the stand-in's
        self.assertEqual(set(fam["sidecar"]), seat_catalog.SIDECAR_KEYS)
        self.assertIsNone(seat_catalog._sidecar_error(seat.FAMILIES))
        # The stand-in listens within a fraction of a second of its start and
        # takes a SIGTERM at once unless an arm stopped it, so a start polls
        # it every 50 ms (not every second) and a stop gives SIGTERM half a
        # second before SIGKILL (not five): the order of the signals and the
        # waits are the real ones, only their spacing is the stand-in's.
        # NO create=True: a patch that may create its attribute survives a
        # rename of the constant, and the module goes back to the real
        # seconds with nothing red (task/3039). Patching a name the module
        # no longer has raises AttributeError here instead.
        for name, value in (("START_WAIT_S", 15.0), ("PROBE_TIMEOUT_S", 2.0),
                            ("WEDGE_CONFIRM_S", 1.5), ("START_POLL_S", 0.05),
                            ("STOP_GRACE_S", 0.5)):
            patch = mock.patch.object(seat_sidecar, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self._reap)

    def _write(self, rel, text, mode="w"):
        with open(os.path.join(self.artifact, rel), mode) as f:
            f.write(text)

    def _hits(self):
        try:
            with open(os.path.join(self.artifact, "hits")) as f:
                return len(f.read().splitlines())
        except OSError:
            return 0

    def _pid(self):
        try:
            with open(os.path.join(self.home, "bridge.pid")) as f:
                return int(f.read().split()[0])
        except (OSError, ValueError, IndexError):
            return None

    def _kill(self, pid, sig=signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return
        for _ in range(50):
            try:
                if os.waitpid(pid, os.WNOHANG)[0]:
                    return
            except ChildProcessError:
                return
            time.sleep(0.1)

    def _reap(self):
        """Continue the bridge (an arm may have stopped it), then SIGKILL it
        and reap it. Only the SIGKILL is waited on: a SIGCONT ends nothing,
        so a wait for the pid to exit after it would run out the whole five
        seconds `_kill` allows."""
        pid = self._pid()
        if pid:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGCONT)
            self._kill(pid)


class SupervisionTest(SidecarBase):
    def test_a_dead_bridge_is_started_and_restarted_after_a_kill(self):
        state, detail, pid = seat_sidecar.verdict(FAMILY)
        self.assertEqual(state, seat_sidecar.DOWN, detail)
        name, row, detail = seat_sidecar.ensure_row(FAMILY)
        self.assertEqual((name, row), (seat_sidecar.label(FAMILY), "respawned"),
                         detail)
        first = self._pid()
        self.assertIsNotNone(first)
        self.assertEqual(seat_sidecar.verdict(FAMILY)[0], seat_sidecar.UP)
        # THE INCIDENT: the bridge dies under a healthy proxy
        self._kill(first)
        self.assertEqual(seat_sidecar.verdict(FAMILY)[0], seat_sidecar.DOWN)
        _name, row, detail = seat_sidecar.ensure_row(FAMILY)
        self.assertEqual(row, "respawned", detail)
        second = self._pid()
        self.assertNotEqual(second, first)
        self.assertEqual(seat_sidecar.verdict(FAMILY)[:1], (seat_sidecar.UP,))
        # and a serving bridge is left alone: no restart, same pid
        _name, row, _detail = seat_sidecar.ensure_row(FAMILY)
        self.assertEqual((row, self._pid()), ("healthy", second))

    def test_a_wedged_bridge_is_stopped_and_restarted(self):
        self.assertEqual(seat_sidecar.ensure_row(FAMILY)[1], "respawned")
        first = self._pid()
        os.kill(first, signal.SIGSTOP)      # alive, holding the port, silent
        with mock.patch.object(seat_sidecar, "STARTUP_GRACE_S", 0.0):
            self.assertEqual(seat_sidecar.verdict(FAMILY)[0],
                             seat_sidecar.WEDGED)
            _name, row, detail = seat_sidecar.ensure_row(FAMILY)
        self.assertEqual(row, "respawned", detail)
        self.assertNotEqual(self._pid(), first)
        self.assertTrue(seat_sidecar._gone(first))
        self._kill(first)

    def test_one_unanswered_probe_is_not_a_wedge_and_the_bridge_is_kept(self):
        """A FALSE WEDGED KILLED A HEALTHY BRIDGE. Its /models holds the only
        event loop for a synchronous curl (up to 7 s), so two overlapping
        probes can outwait PROBE_TIMEOUT_S while Cursor is slow. A wedge is
        confirmed by a second probe, spaced out, before any signal."""
        self.assertEqual(seat_sidecar.ensure_row(FAMILY)[1], "respawned")
        first = self._pid()
        open(os.path.join(self.artifact, "slow-once"), "w").close()
        with mock.patch.object(seat_sidecar, "STARTUP_GRACE_S", 0.0):
            _name, row, detail = seat_sidecar.ensure_row(FAMILY)
        self.assertEqual((row, self._pid()), ("healthy", first), detail)
        self.assertFalse(seat_sidecar._gone(first))
        self.assertFalse(os.path.exists(os.path.join(self.artifact,
                                                     "slow-once")),
                         "the control: the first probe did go unanswered")
        self.assertIn("second probe", detail)

    def test_a_first_non_2xx_after_a_start_is_waited_out(self):  # noqa: VACUOUS_ASSERTION — the respawned row is the unconditional positive control for the consumed refuse-once marker
        open(os.path.join(self.artifact, "refuse-once"), "w").close()
        _name, row, detail = seat_sidecar.ensure_row(FAMILY)
        self.assertEqual(row, "respawned", detail)
        self.assertFalse(os.path.exists(os.path.join(self.artifact,
                                                     "refuse-once")),
                         "the control: the bridge did answer 502 first")

    def test_a_bridge_that_answers_non_2xx_is_reported_never_restarted(self):
        open(os.path.join(self.artifact, "refuse"), "w").close()
        with mock.patch.object(seat_sidecar, "START_WAIT_S", 4.0):
            _name, row, detail = seat_sidecar.ensure_row(FAMILY)
        self.assertEqual(row, "unknown")
        self.assertIn("still unserving", detail)
        first = self._pid()
        self.assertIsNotNone(first)
        _name, row, detail = seat_sidecar.ensure_row(FAMILY)
        self.assertEqual(row, "unknown")
        self.assertEqual(self._pid(), first, "a restart cannot refill a login")
        self.assertIn("HTTP 502", detail)
        self.assertIn(seat_sidecar.credentials_path(FAMILY), detail)

    def test_an_absent_checkout_is_unknown_and_names_the_path(self):  # noqa: VACUOUS_ASSERTION — row unknown and the named path are the unconditional positive control for the absent pidfile
        shutil.rmtree(self.artifact)
        _name, row, detail = seat_sidecar.ensure_row(FAMILY)
        self.assertEqual(row, "unknown")
        self.assertIn(self.artifact, detail)
        self.assertIn(PIN[:12], detail)
        self.assertIsNone(self._pid())

    def test_a_missing_tool_refuses_the_start(self):  # noqa: VACUOUS_ASSERTION — row unknown naming the tool is the unconditional positive control for the absent pidfile
        fam = dict(seat.FAMILIES[FAMILY])
        fam["sidecar"] = dict(fam["sidecar"], needs=("helm-no-such-tool-7f3c",))
        with mock.patch.dict(seat.FAMILIES, {FAMILY: fam}):
            _name, row, detail = seat_sidecar.ensure_row(FAMILY)
        self.assertEqual(row, "unknown")
        self.assertIn("helm-no-such-tool-7f3c", detail)
        self.assertIsNone(self._pid())
        self.assertEqual(seat_sidecar.probe(FAMILY)[0], "refused")

    def test_a_listener_helm_does_not_own_is_never_signalled(self):  # noqa: VACUOUS_ASSERTION — row unknown saying not signalled is the unconditional positive control for the absent pidfile
        holder = socket.socket()
        self.addCleanup(holder.close)
        holder.bind(("127.0.0.1", self.port))
        holder.listen(8)                    # accepts into the backlog, never answers
        _name, row, detail = seat_sidecar.ensure_row(FAMILY)
        self.assertEqual(row, "unknown")
        self.assertIn("not signalled", detail)
        self.assertIsNone(self._pid())
        self.assertIsNotNone(holder.getsockname())

    def test_the_bridge_is_started_with_an_allowlisted_environment(self):  # noqa: VACUOUS_ASSERTION — the kept names found in the started bridge's environment are the unconditional positive control for the two absent ones
        """The vendored bridge is third-party code. A start from a pane's
        shell handed it that pane's whole environment, the seat's own proxy
        bearer (ANTHROPIC_AUTH_TOKEN) included; it gets PATH, HOME, PORT and
        the variables its declaration names, nothing else."""
        with mock.patch.dict(os.environ, {
                "ANTHROPIC_AUTH_TOKEN": "a-pane-bearer",
                "HELM_UNRELATED_SETTING": "x",
                "CURSOR_BRIDGE_TRACE": "1",
                "CURSOR_BRIDGE_STALL_MS": "120000"}):
            self.assertEqual(seat_sidecar.ensure_row(FAMILY)[1], "respawned")
        with open(os.path.join(self.artifact, "environ")) as f:
            seen = set(json.load(f))
        self.assertTrue({"PATH", "HOME", "PORT", "CURSOR_BRIDGE_TRACE",
                         "CURSOR_BRIDGE_STALL_MS"} <= seen, seen)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", seen)
        self.assertNotIn("HELM_UNRELATED_SETTING", seen)

    def test_a_tool_off_path_is_found_in_its_install_dir(self):  # noqa: VACUOUS_ASSERTION — the found path is the unconditional positive control for the None beside it
        bindir = os.path.join(self.tmp, ".faketool", "bin")
        os.makedirs(bindir)
        tool = os.path.join(bindir, "faketool")
        with open(tool, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(tool, 0o755)
        with mock.patch.dict(os.environ, {"HOME": self.tmp, "PATH": "/nonexistent"}):
            self.assertEqual(seat_sidecar.resolve_tool("faketool"), tool)
            self.assertIsNone(seat_sidecar.resolve_tool("helm-no-such-tool-7f3c"))


class VettingTest(SidecarBase):
    """The checkout must be the build the catalog vetted: HEAD on the pin and
    every patched file at its vetted digest. Anything else is UNVETTED, which
    helm neither starts nor launches a seat on."""

    def assertUnvetted(self, *named):
        state, detail, _pid = seat_sidecar.verdict(FAMILY)
        self.assertEqual(state, seat_sidecar.UNVETTED, detail)
        for word in named:
            self.assertIn(word, detail)
        _name, row, detail = seat_sidecar.ensure_row(FAMILY)
        self.assertEqual(row, "unknown", detail)
        return detail

    def test_a_patched_file_that_drifted_is_never_started(self):  # noqa: VACUOUS_ASSERTION — assertUnvetted asserts the UNVETTED state and the unknown row unconditionally, the positive control for the absent pid
        self.assertEqual(seat_sidecar.vetting_gaps(FAMILY), [])
        self._write("fake_bridge.py", "\n# a local edit\n", mode="a")
        self.assertUnvetted("fake_bridge.py", "stand-in")
        self.assertIsNone(self._pid())
        self.assertEqual(seat_sidecar.probe(FAMILY)[0], "refused")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(seat_sidecar.up(FAMILY, quiet=True), 1)
        self.assertIn("not the build the catalog vetted", err.getvalue())
        self.assertIn("BRIDGE UNVETTED", seat_sidecar.badge(FAMILY)[0])

    def test_a_head_off_the_pin_is_unvetted_until_it_returns(self):  # noqa: VACUOUS_ASSERTION — assertUnvetted asserts the UNVETTED state and the unknown row unconditionally, the positive control for the absent pid
        self._write(".git/refs/heads/main", "feed" * 10 + "\n")
        # the refusal names the checkout-side remedy, not only a re-vet
        self.assertUnvetted("feedfeedfeed", PIN[:12],
                            "git -C %s merge --ff-only %s" % (self.artifact, PIN),
                            "restart a running bridge")
        self.assertIsNone(self._pid())
        self._write(".git/refs/heads/main", PIN + "\n")
        self.assertEqual(seat_sidecar.verdict(FAMILY)[0], seat_sidecar.DOWN)

    def test_a_running_bridge_that_drifts_is_reported_and_left_running(self):  # noqa: VACUOUS_ASSERTION — assertUnvetted asserts the UNVETTED state and the unknown row unconditionally, the positive control for the absent pid
        self.assertEqual(seat_sidecar.ensure_row(FAMILY)[1], "respawned")
        first = self._pid()
        self._write("fake_bridge.py", "\n# a local edit\n", mode="a")
        self.assertUnvetted("fake_bridge.py")
        self.assertEqual(self._pid(), first)
        self.assertFalse(seat_sidecar._gone(first), "never signalled")

    def test_every_runtime_file_is_hashed_not_only_the_patched_ones(self):  # noqa: VACUOUS_ASSERTION — assertUnvetted asserts the UNVETTED state unconditionally, the positive control for each empty gap list
        """THE CLAIM IS "ANY LOCAL EDIT READS UNVETTED", so the digest covers
        every file git lists outside the tests and docs: tracked files a patch
        names, tracked files none does, and untracked ones git does not ignore
        (a new bunfig.toml can preload code). Ignored files the server writes,
        and the excluded tests and docs, are not runtime."""
        self.assertEqual(seat_sidecar.vetting_gaps(FAMILY), [])
        self._write("hits", "/v1/models\n")
        self._write("test/test_fake.py", "# a changed test\n", mode="a")
        self._write("README.md", "changed docs\n", mode="a")
        self.assertEqual(seat_sidecar.vetting_gaps(FAMILY), [])
        self._write("helper.py", "# a local edit\n", mode="a")
        detail = self.assertUnvetted("runtime files", "git -C %s status"
                                     % self.artifact)
        self.assertNotIn("fake_bridge.py", detail, "no patched file changed")
        self._write("helper.py", "# a runtime file no patch names\n")
        self.assertEqual(seat_sidecar.vetting_gaps(FAMILY), [])
        self._write("bunfig.toml", 'preload = ["./helper.py"]\n')
        self.assertUnvetted("runtime files")

    def test_head_is_read_from_packed_refs_and_from_a_detached_head(self):  # noqa: VACUOUS_ASSERTION — assertUnvetted asserts the UNVETTED state and the unknown row unconditionally, the positive control for the absent pid
        os.remove(os.path.join(self.git, "refs", "heads", "main"))
        self._write(".git/packed-refs", "# pack-refs with: peeled\n"
                    "%s refs/heads/other\n%s refs/heads/main\n"
                    % ("feed" * 10, PIN))
        self.assertEqual(seat_sidecar._git_head(self.artifact), PIN)
        self._write(".git/HEAD", "feed" * 10 + "\n")
        self.assertEqual(seat_sidecar._git_head(self.artifact), "feed" * 10)
        shutil.rmtree(self.git)
        self.assertIsNone(seat_sidecar._git_head(self.artifact))
        self.assertUnvetted("HEAD is unreadable")


class ServingProcessTest(SidecarBase):
    """The vetting reads the checkout on disk; the port is served by a
    PROCESS, which may be running other bytes. The listener on the bridge
    port must run from the checkout and must be younger than the checkout's
    runtime files, or it is FOREIGN or STALE: never probed (its /models is its
    own code) and never signalled."""

    def test_a_bridge_older_than_its_checkout_is_stale_and_never_signalled(self):  # noqa: VACUOUS_ASSERTION — the stale state and the unknown row are asserted unconditionally, the positive control for the unchanged hit count
        self.assertEqual(seat_sidecar.ensure_row(FAMILY)[1], "respawned")
        first = self._pid()
        later = time.time() + 60        # the checkout moved after the start
        os.utime(os.path.join(self.artifact, "helper.py"), (later, later))
        before = self._hits()
        state, detail, _pid = seat_sidecar.verdict(FAMILY)
        self.assertEqual(state, "stale", detail)
        self.assertIn("kill %d" % first, detail)
        self.assertIn("helm seat up %s" % FAMILY, detail)
        _name, row, detail = seat_sidecar.ensure_row(FAMILY)
        self.assertEqual(row, "unknown", detail)
        self.assertEqual(self._pid(), first)
        self.assertFalse(seat_sidecar._gone(first), "never signalled")
        self.assertEqual(self._hits(), before, "a stale build's /models is "
                         "its own code, and helm never runs it")
        self.assertIn("BRIDGE STALE", seat_sidecar.badge(FAMILY)[0])

    def test_a_listener_run_from_outside_the_checkout_is_foreign_even_when_it_serves(self):  # noqa: VACUOUS_ASSERTION — the foreign state naming the pid is asserted unconditionally, the positive control for the zero hit count
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere)
        with open(os.path.join(elsewhere, "fake_bridge.py"), "w") as f:
            f.write(FAKE_BRIDGE)
        other = subprocess.Popen(
            [sys.executable, "fake_bridge.py", "serve", str(self.port)],
            cwd=elsewhere, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        self.addCleanup(self._kill, other.pid)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.port), 0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        state, detail, _pid = seat_sidecar.verdict(FAMILY)
        self.assertEqual(state, seat_sidecar.FOREIGN, detail)
        self.assertIn("pid %d" % other.pid, detail)
        self.assertIn(elsewhere, detail)
        self.assertIn("not signalled", detail)
        _name, row, _detail = seat_sidecar.ensure_row(FAMILY)
        self.assertEqual(row, "unknown")
        self.assertIsNone(other.poll(), "never signalled")
        self.assertFalse(os.path.exists(os.path.join(elsewhere, "hits")),
                         "a foreign listener is never probed")


class SurfacesTest(SidecarBase):
    def _mint_config(self):
        from helm import seat_launch_assets
        with open(os.path.join(self.home, "config.yaml"), "w") as f:
            f.write(seat_launch_assets._config_yaml_key(
                seat.FAMILIES[FAMILY]["port"], "inbound", "cursor-bridge",
                seat.FAMILIES[FAMILY]["base_url"], FAMILY, "no-key-required",
                "grok-4.7-high"))

    def _ensure(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(seat, "_minted_seats",
                               return_value=[(FAMILY, FAMILY)]), \
                mock.patch.object(seat, "_ensure_row",
                                  return_value=(FAMILY, "healthy",
                                                "pid 1 port 8315")), \
                mock.patch.object(seat, "_running_pid", return_value=1), \
                mock.patch.object(seat, "_cpu_canary",
                                  return_value=("ok", 1.0, 60.0, "")), \
                mock.patch.object(seat_health, "_cred_follow_pass",
                                  return_value=None), \
                mock.patch("helm.suspend.resume_rung", return_value=[]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seat._ensure(["--ensure"] + list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_the_ensure_pass_rows_the_bridge_before_the_proxy(self):
        self._mint_config()
        rc, out, err = self._ensure()
        self.assertEqual(rc, 0, err)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith(seat_sidecar.label(FAMILY)), out)
        self.assertIn("RESPAWNED", lines[0])
        self.assertTrue(lines[1].startswith(FAMILY + " "), out)
        self.assertEqual(seat_sidecar.verdict(FAMILY)[0], seat_sidecar.UP)
        rc, out, _err = self._ensure("--json")
        doc = json.loads(out)
        self.assertEqual([r.get("process") for r in doc["rows"]],
                         ["sidecar", None])
        self.assertEqual(doc["rows"][0]["state"], "healthy")
        # quiet: a healthy bridge is not news, and the heartbeat counts it
        rc, out, _err = self._ensure("--quiet")
        self.assertEqual(rc, 0)
        self.assertIn("1 proxy row(s) HEALTHY and 1 sidecar row(s)", out)

    def test_seat_list_reads_the_bridge_once(self):
        """`seat list` renders a row from the ONE fleet join, and that join
        already read the bridge; the row's badge reuses that reading instead
        of probing a second time (each probe is a Cursor model listing, and
        two at once queue on the bridge's one event loop)."""
        from tests import test_seat_usability as u
        from helm import seat_usability
        self._mint_config()
        self.assertEqual(seat_sidecar.ensure_row(FAMILY)[1], "respawned")
        before = self._hits()
        joined = seat_usability.join(
            health=u._health([u._hrow(FAMILY, family=FAMILY)]),
            upstream=u._upstream({FAMILY: {"state": "HEALTHY", "dark": False}}),
            open_recipients=u._ledger({}),
            register=u._roster({FAMILY: {"runtime_verified": True}}),
            canonical=u._IDENTITY, live_seats=u._panes((FAMILY,)),
            beacon_live=u._no_beacon)
        self.assertEqual(joined[FAMILY]["sidecar"], seat_sidecar.UP)
        self.assertEqual(self._hits(), before + 1, "the join reads it once")
        row = seat_health._seat_row(FAMILY, usability=joined)
        self.assertEqual(self._hits(), before + 1, "and the row reuses it")
        self.assertNotIn("BRIDGE", row)
        # the reading is the join's own: a join that read it down badges it
        joined[FAMILY]["sidecar"] = seat_sidecar.DOWN
        joined[FAMILY]["sidecar_why"] = "the join's own why"
        row = seat_health._seat_row(FAMILY, usability=joined)
        self.assertIn("BRIDGE DOWN", row)
        self.assertIn("the join's own why", row)
        self.assertEqual(self._hits(), before + 1)

    def test_a_bridge_the_pass_cannot_fix_pages_like_a_proxy(self):
        self._mint_config()
        shutil.rmtree(self.artifact)
        rc, out, err = self._ensure()
        self.assertEqual(rc, 2)
        self.assertIn("UNKNOWN", out.splitlines()[0])
        self.assertIn("1 UNKNOWN row", err)

    def test_the_seat_list_row_badges_a_dead_bridge(self):
        self._mint_config()
        row = seat_health._seat_row(FAMILY)
        first = row.splitlines()[0]
        self.assertIn("BRIDGE DOWN", first)
        self.assertIn("⚠ %s: " % seat_sidecar.label(FAMILY), row)
        self.assertEqual(seat_sidecar.ensure_row(FAMILY)[1], "respawned")
        served = seat_health._seat_row(FAMILY)
        self.assertNotIn("BRIDGE", served)
        self.assertNotIn(seat_sidecar.label(FAMILY), served)

    def test_seat_up_starts_the_bridge_first_and_refuses_without_one(self):
        self._mint_config()
        missing = os.path.join(self.tmp, "no-proxy-binary")
        with mock.patch.dict(os.environ, {"HELM_PROXY_BIN": missing}):
            err = io.StringIO()
            with contextlib.redirect_stderr(err), \
                    contextlib.redirect_stdout(io.StringIO()):
                rc = seat_proxy._up(FAMILY, quiet=True)
            # the proxy start failed on its binary, AFTER the bridge came up
            self.assertEqual(rc, 1)
            self.assertNotIn("refusing to start", err.getvalue())
            self.assertEqual(seat_sidecar.verdict(FAMILY)[0], seat_sidecar.UP)
            self._reap()
            shutil.rmtree(self.artifact)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = seat_proxy._up(FAMILY, quiet=True)
        self.assertEqual(rc, 1)
        self.assertIn("refusing to start the %s proxy" % FAMILY, err.getvalue())


class CanaryCostTest(unittest.TestCase):
    """Every Cursor request bills (a one-word prompt measured 2.24 cents), so
    the watch's upstream canary for a sidecar family is the bridge's unbilled
    /models read, and the money reader owns the quota and wall reading. One
    stand-in plays the bridge AND the proxy, so a billed request sent down
    either road is counted."""

    ENVELOPE = (b'{"type":"message","role":"assistant",'
                b'"content":[{"type":"text","text":"OK"}],'
                b'"stop_reason":"end_turn"}')

    def setUp(self):
        import http.server
        import threading
        seen, test = [], self
        self.seen, self.models_code = seen, 200

        class H(http.server.BaseHTTPRequestHandler):
            def _answer(self, code, body):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                seen.append(("GET", self.path))
                ok = self.path == "/v1/models"
                self._answer(test.models_code if ok else 404,
                             b'{"object": "list", "data": []}')

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                seen.append(("POST", self.path))
                self._answer(200, CanaryCostTest.ENVELOPE)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        # shutdown() returns once serve_forever next looks at its flag, so the
        # loop looks every 50 ms rather than every half second
        threading.Thread(target=self.server.serve_forever,
                         kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        fam = dict(seat.FAMILIES[FAMILY],
                   base_url="http://127.0.0.1:%d/v1" % self.port)
        # the build is vetted here; VettingTest owns the unvetted checkout
        # and ServingProcessTest the stale and foreign listener
        self.gaps, self.held = [], None
        for patch in (mock.patch.dict(seat.FAMILIES, {FAMILY: fam}),
                      mock.patch("helm.pi.seat_port",
                                 return_value=(self.port, None)),
                      mock.patch("helm.pi._pi_api_key",
                                 return_value="local-token"),
                      mock.patch.object(seat_sidecar, "vetting_gaps",
                                        side_effect=lambda _f: self.gaps),
                      mock.patch.object(
                          seat_sidecar, "unprobeable",
                          side_effect=lambda _f: (
                              ("unvetted", "; ".join(self.gaps))
                              if self.gaps else self.held))):
            patch.start()
            self.addCleanup(patch.stop)

    def billed(self):
        return [path for method, path in self.seen if method == "POST"]

    def _live_pane_shape(self):
        """A live pane on the cursor alias, its exact listener and the loaded
        config that routes it to the stand-in: the shape the attestation
        canary measures before it would send its request."""
        tmp = tempfile.mkdtemp(prefix="helm-test-attest-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        config = os.path.join(tmp, "config.yaml")
        binary = os.path.join(tmp, "cli-proxy-api")
        with open(config, "w", encoding="utf-8") as f:
            f.write(seat._config_yaml_key(
                self.port, "inbound", "cursor-bridge",
                seat.FAMILIES[FAMILY]["base_url"], FAMILY, "no-key-required",
                "grok-4.7-high"))
        with open(binary, "wb") as f:
            f.write(b"proxy")
        with open(os.path.join(tmp, "proxy.pid"), "w", encoding="utf-8") as f:
            f.write("4201 proc:702 %s\n" % seat._encode_launch_inputs(
                seat._proxy_launch_inputs(config, binary)))
        runtime = {"agent_harness": "claude", "pid": 4101, "starttime": 701,
                   "model": FAMILY, "token": "inbound",
                   "base_url": "http://127.0.0.1:%d" % self.port}
        listener = {"pid": 4201, "identity": "proc:702",
                    "argv": [binary, "-config", config], "config": config}
        for patch in (mock.patch.object(proxywatch, "_roster_session",
                                        return_value=("session-cursor",
                                                      FAMILY, None)),
                      mock.patch.object(proxywatch, "_live_session_runtime",
                                        return_value=(runtime, None)),
                      mock.patch("helm.seat._port_listeners",
                                 return_value=[listener])):
            patch.start()
            self.addCleanup(patch.stop)

    def test_the_attestation_canary_is_never_sent_down_the_bridge(self):  # noqa: VACUOUS_ASSERTION — the measured cursor route is the unconditional positive control for the empty POST list
        self._live_pane_shape()
        shape, err = proxywatch._proxy_runtime_shape(FAMILY)
        self.assertIsNone(err, err)
        self.assertEqual(proxywatch._proxy_route_family(
            shape["proof"]["route"]), (FAMILY, None))
        proof, err = proxywatch.proxy_runtime_canary(FAMILY, observed_at=1000)
        self.assertIsNone(proof)
        self.assertIn("billed bridge", err)
        rows = [{"seat": FAMILY, "family": FAMILY, "probe": "healthy"}]
        self.assertEqual(proxywatch.proxy_runtime_proofs(rows, 1000), {})
        self.assertEqual(self.billed(), [])
        # the endpoint decides: an alias no catalog row owns, deferred as a
        # candidate, still ends at the bridge; a vendor endpoint does not
        bridge = seat.FAMILIES[FAMILY]["base_url"]
        for base_url, want in ((bridge + "/", FAMILY),
                               ("https://api.example.test/v1", None)):
            candidate = {"alias": "not-catalogued", "provider": "p",
                         "upstream_model": "m", "base_url": base_url}
            self.assertEqual(proxywatch._billed_sidecar_family(
                {"proof": {}, "auth_routes": {"0": (candidate,)}}), want)

    def test_a_stale_or_foreign_listener_is_never_probed_by_the_watch(self):  # noqa: VACUOUS_ASSERTION — the vetted read's one GET is the unconditional positive control for the empty request list
        for held in (("stale", "a stand-in stale listener"),
                     ("foreign", "a stand-in foreign listener")):
            self.held = held
            state, detail, _ms = proxywatch._upstream_once(FAMILY,
                                                           family=FAMILY)
            self.assertEqual(state, "UNKNOWN", detail)
            self.assertIn(held[1], detail)
            self.assertIn("not probed", detail)
        self.assertEqual(self.seen, [])
        self.held = None
        self.assertEqual(proxywatch._upstream_once(FAMILY, family=FAMILY)[0],
                         "HEALTHY")
        self.assertEqual(self.seen, [("GET", "/v1/models")])

    def test_an_unvetted_bridge_is_never_probed(self):  # noqa: VACUOUS_ASSERTION — the vetted read's one GET is the unconditional positive control for the empty request list
        self.gaps = ["HEAD is off the pin the catalog vetted (a stand-in gap)"]
        for _call in range(2):
            state, detail, _ms = proxywatch.upstream_canary(
                FAMILY, sleep=lambda _s: None, family=FAMILY)
            self.assertEqual(state, "UNKNOWN", detail)
            self.assertIn("a stand-in gap", detail)
        self.assertEqual(self.seen, [], "an unvetted build's /models is its "
                         "own code, and the watch never runs it")
        self.gaps = []
        self.assertEqual(proxywatch._upstream_once(FAMILY, family=FAMILY)[0],
                         "HEALTHY")
        self.assertEqual(self.seen, [("GET", "/v1/models")])

    def test_the_cursor_canary_reads_the_bridge_and_bills_nothing(self):  # noqa: VACUOUS_ASSERTION — the three counted GETs of /v1/models are the unconditional positive control for the empty POST list
        state, detail, _ms = proxywatch._upstream_once(FAMILY, family=FAMILY)
        self.assertEqual(state, "HEALTHY", detail)
        self.assertEqual(proxywatch.upstream_canary(
            FAMILY, sleep=lambda _s: None, family=FAMILY)[0], "HEALTHY")
        self.assertEqual(proxywatch._dark_trigger([(FAMILY, [FAMILY])]),
                         {FAMILY: (FAMILY, "HEALTHY", detail)})
        self.assertEqual(self.billed(), [])
        self.assertEqual(self.seen, [("GET", "/v1/models")] * 3)

    def test_a_bridge_that_cannot_serve_reads_its_code_and_still_bills_nothing(self):  # noqa: VACUOUS_ASSERTION — the two counted requests are the unconditional positive control for the empty POST list
        self.models_code = 502
        state, detail, _ms = proxywatch.upstream_canary(
            FAMILY, sleep=lambda _s: None, family=FAMILY)
        self.assertEqual(state, "UPSTREAM-5XX", detail)
        self.assertIn("502", detail)
        self.server.shutdown()
        self.server.server_close()
        state, detail, _ms = proxywatch._upstream_once(FAMILY, family=FAMILY)
        self.assertEqual(state, "UNKNOWN", detail)
        self.assertIn("connection refused", detail)
        self.assertEqual(self.billed(), [])
        self.assertEqual(len(self.seen), 2, "the confirmation re-read the bridge")

    def test_other_families_still_send_their_eight_token_request(self):  # noqa: VACUOUS_ASSERTION — the one counted POST is the unconditional positive control for the absent /models GET
        state, detail, _ms = proxywatch._upstream_once("codex", family="codex")
        self.assertEqual(state, "HEALTHY", detail)
        self.assertEqual(len(self.billed()), 1)
        self.assertTrue(self.billed()[0].startswith("/v1/messages?"))
        self.assertNotIn(("GET", "/v1/models"), self.seen)


from tests import test_seat_spawn as _spawn_rig  # noqa: E402 — the rig, not its arms


class ALaunchOntoADeadBridgeIsRefusedTest(_spawn_rig.SpawnBase):
    """`seat spawn` and `seat resume` refuse a sidecar family's pane while its
    bridge cannot serve: a pane on a dead bridge answers nothing, and the old
    warn-and-launch left the owner a seat that looked started. The bridge here
    is really absent (no checkout under this HELM_HOME), so the refusal is
    seat_sidecar's own verdict, never a mock of it; the controls mock only
    `up` to say it serves."""

    def setUp(self):
        super().setUp()
        self.d, self.launch = self._mint(seat_name=FAMILY, family=FAMILY)
        with open(self.launch) as f:
            self.minted = f.read()

    def drive(self, argv, adapter, serves=False):
        out, err = io.StringIO(), io.StringIO()
        real = seat_sidecar.up
        up = mock.Mock(side_effect=lambda fam, quiet=False: 0) if serves \
            else mock.Mock(side_effect=real)
        with mock.patch.object(seat_sidecar, "up", up), \
                mock.patch.object(seat, "_write_launch_assets") as wla, \
                mock.patch.object(_spawn_rig.harness, "detect",
                                  return_value=adapter), \
                mock.patch.object(seat, "_pid_identity",
                                  return_value="test-start"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(argv)
        up.assert_called_with(FAMILY, quiet=True)
        return rc, err.getvalue(), wla

    def _bind_session(self):
        """The exact session a resume relaunches: a transcript in the seat's
        own claude home and its id on the register (SparkModelPersistenceTest's
        binding, which resume requires before it will relaunch a pane)."""
        sid = "11111111-1111-1111-1111-111111111111"
        project = os.path.join(self.d, "claude", "projects", "test")
        os.makedirs(project, exist_ok=True)
        with open(os.path.join(project, sid + ".jsonl"), "w") as f:
            f.write(json.dumps({"type": "assistant", "sessionId": sid,
                                "cwd": self.home}) + "\n")
        path = os.path.join(self.d, "spawn.json")
        with open(path) as f:
            rec = json.load(f)
        with open(path, "w") as f:
            json.dump(dict(rec, session=sid), f)

    def assertRefused(self, rc, err, wla, adapter, verb):
        self.assertEqual(rc, 1, err)
        self.assertIn("refusing to %s %s" % (verb, FAMILY), err)
        self.assertIn("helm seat up %s" % FAMILY, err)
        self.assertIn(seat_sidecar.label(FAMILY), err)
        self.assertIn("not provisioned", err, "the verdict's own why")
        self.assertEqual((adapter.spawned, adapter.stopped), ([], []))
        wla.assert_not_called()
        with open(self.launch) as f:
            self.assertEqual(f.read(), self.minted)

    def test_spawn_refuses_before_the_reap_and_names_the_fix(self):  # noqa: VACUOUS_ASSERTION — the control spawn's one launched pane is the unconditional positive control for the empty spawned/stopped lists
        adapter = _spawn_rig.FakeAdapter()
        rc, err, wla = self.drive(["spawn", FAMILY, "--cwd", self.home],
                                  adapter)
        self.assertRefused(rc, err, wla, adapter, "spawn")
        self.assertFalse(os.path.exists(os.path.join(self.d, "spawn.json")))
        # CONTROL: the same spawn with a serving bridge launches its pane
        rc, err, _wla = self.drive(["spawn", FAMILY, "--cwd", self.home],
                                   adapter, serves=True)
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(adapter.spawned), 1)

    def test_resume_refuses_before_the_reap_and_names_the_fix(self):
        rc, err, _wla = self.drive(["spawn", FAMILY, "--cwd", self.home],
                                   _spawn_rig.FakeAdapter(), serves=True)
        self.assertEqual(rc, 0, err)
        self._bind_session()
        with open(self.launch) as f:
            self.minted = f.read()
        pane = [{"handle": "pane-1", "title": FAMILY, "status": "idle"}]
        adapter = _spawn_rig.FakeAdapter(rows=pane)
        rc, err, wla = self.drive(["resume", FAMILY], adapter)
        self.assertRefused(rc, err, wla, adapter, "resume")
        # CONTROL: the same resume with a serving bridge relaunches the pane
        rc, err, _wla = self.drive(["resume", FAMILY], adapter, serves=True)
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(adapter.spawned), 1)


class DeclarationTest(unittest.TestCase):
    def table(self, **sidecar):
        spec = dict(seat.FAMILIES[FAMILY]["sidecar"], **sidecar)
        return {"fam": {"port": 8315, "base_url": "http://127.0.0.1:18315/v1",
                        "sidecar": spec}}

    def test_the_live_catalog_passes_and_routes_to_its_bridge(self):
        self.assertIsNone(seat_catalog._sidecar_error())
        self.assertEqual(seat_catalog.sidecar_port(seat.FAMILIES[FAMILY]),
                         18315)
        self.assertIsNone(seat_catalog._sidecar_error(self.table()))

    def test_the_runtime_digest_is_declared_with_its_exclusions(self):
        live = seat.FAMILIES[FAMILY]["sidecar"]
        self.assertEqual(len(live["runtime_sha256"]), 64)
        self.assertIn("test/", live["runtime_exclude"])
        self.assertIn("runtime_sha256", seat_catalog._sidecar_error(
            self.table(runtime_sha256="a" * 63)))
        for bad in ("test/", ("",), ["test/"]):
            self.assertIn("runtime_exclude", seat_catalog._sidecar_error(
                self.table(runtime_exclude=bad)), bad)

    def test_the_cursor_seat_is_told_the_route_to_its_tools(self):
        """The family's `system_line` names the route the bridge's refusals
        name (its reject-names-the-route patch): every tool is an MCP tool on
        server opencode, the bridge's MCP_PROVIDER, called with
        CallDynamicTool, schemas from GetDynamicTools. It rides the launch
        line as one quoted --append-system-prompt token after --model, so it
        reaches every turn of every session that line starts."""
        import shlex
        line = seat.FAMILIES[FAMILY]["system_line"]
        self.assertTrue(line.isascii())
        for word in ("server opencode", "CallDynamicTool", "namespace opencode",
                     "GetDynamicTools", "Bash", "never end the turn"):
            self.assertIn(word, line)
        launch = seat.launch_line(FAMILY)
        self.assertIn(" --append-system-prompt %s" % shlex.quote(line), launch)
        self.assertLess(launch.index("--model cursor"),
                        launch.index("--append-system-prompt"))

    def test_env_keep_names_environment_variables(self):
        keep = seat.FAMILIES[FAMILY]["sidecar"]["env_keep"]
        self.assertIn("CURSOR_BRIDGE_TRACE", keep)
        # the bridge's stall window (its thinking-is-not-a-stall patch): an
        # operator's override reaches a bridge helm starts
        self.assertIn("CURSOR_BRIDGE_STALL_MS", keep)
        for bad in ("TMPDIR", ("tmpdir",), ("A=B",)):
            self.assertIn("env_keep", seat_catalog._sidecar_error(
                self.table(env_keep=bad)), bad)

    def test_the_vetted_proxy_has_the_closed_stall_and_timeout_budget(self):
        """task/3374: the vetted src/proxy.ts is the build whose stall
        watchdog ends a run only on a dead stream, a tool call Cursor opened
        and never completed, or the client's coming idle timeout, and never
        on a model that thinks with heartbeats only. Its first-frame wait and
        watchdog also leave real scheduling room before that timeout. The
        patch list names both closures, so a drift names what it risks; the
        digests themselves are checked by `vetting_gaps` against the checkout
        (VettingTest)."""
        proxy = seat.FAMILIES[FAMILY]["sidecar"]["required_patches"][
            "src/proxy.ts"]
        self.assertIn("thinking-is-not-a-stall", proxy["patches"])
        self.assertIn("first-frame-budget", proxy["patches"])
        self.assertEqual(len(proxy["sha256"]), 64)

    def test_the_vetted_proxy_reports_usage_from_the_request(self):
        """task/3374, its second finding: the vetted src/proxy.ts reports a
        response's usage as an estimate of the request and of what the
        response carried, never Cursor's own count. MEASURED: Cursor counted
        one conversation 77,400 and then 911,810 (helm read 474.9% of this
        window), and a fresh conversation's first response 50,900 output
        tokens; the seat compacted every 8 to 19 minutes. task/3381 then found
        that legal empty requests still read low because the estimate omitted
        prompts the bridge synthesized, tool-call ids, and the fallback schema
        it forwarded. The patch list names both closures and drops the name of
        the build they replaced, so a checkout that drifts back reads as those
        risks by name."""
        proxy = seat.FAMILIES[FAMILY]["sidecar"]["required_patches"][
            "src/proxy.ts"]
        self.assertIn("usage-is-the-request", proxy["patches"])
        self.assertIn("usage-covers-forwarded-payload", proxy["patches"])
        self.assertNotIn("usage-is-cursors-own", proxy["patches"])

    def test_the_vetted_proxy_runs_cursors_own_tools_as_the_callers(self):
        """task/3533: a Cursor tool refused in prose was the model's cue to
        end the turn, and one such turn poisoned a seat's session for 30
        hours. The vetted src/proxy.ts runs Cursor's own read, shell, grep,
        write, ls and fetch as the caller's matching tool and answers Cursor
        with that tool's own result, so the patch list names that closure and
        the seat's system line no longer tells the model those tools are
        refused."""
        proxy = seat.FAMILIES[FAMILY]["sidecar"]["required_patches"][
            "src/proxy.ts"]
        self.assertIn("native-runs-as-caller-tool", proxy["patches"])
        line = seat.FAMILIES[FAMILY]["system_line"]
        self.assertNotIn("are refused here", line)
        for word in ("Shell", "Read", "delete", "background"):
            self.assertIn(word, line)

    def test_the_vetted_proxy_bounds_a_native_read(self):
        """MEASURED on the cursor seat's transcript: each of Cursor's
        reads reached Claude Code as a Read with only file_path, so
        Claude Code read the whole file (a 100 KB module twice within 2 s,
        then 53 KB and 99 KB more); the conversation went from about 90k to
        155k tokens in five seconds and compacted every three minutes. The
        vetted src/proxy.ts sends a read's own offset and limit, never more
        than its bound of lines, and names the route to the rest; the patch
        list names that closure beside the one it bounds, so a checkout that
        drifts back reads as that risk by name. Lines alone let a 339-line,
        99 KB page or one huge line through whole, and a read from the end
        came back as the first lines marked range_applied: the vetted build
        also holds a read to its byte bound, measured on the file before the
        client reads it, and answers a read from the end with an error naming
        tail, so the list names that closure too."""
        proxy = seat.FAMILIES[FAMILY]["sidecar"]["required_patches"][
            "src/proxy.ts"]
        self.assertIn("native-read-bounded", proxy["patches"])
        self.assertIn("native-read-byte-bounded", proxy["patches"])
        self.assertIn("native-runs-as-caller-tool", proxy["patches"])

    def test_the_vetted_proxy_keeps_cursors_conversation(self):
        """task/3817: the vetted bridge replayed the whole Claude Code
        history into a brand-new Cursor conversation (a random id, every
        message a root-prompt blob, Cursor's checkpoint thrown away) at the
        start of every run, where Cursor's own CLI sends its last checkpoint
        back under one conversation id with only the new message. The
        vetted src/proxy.ts keeps a conversation that closed cleanly and
        continues it when the next request extends it by the reply and new
        user text, replaying (and saying why in the log) only on a
        compaction, /clear, edit or unclean run end; and it names every
        caller tool, Monitor included, in Cursor's MCP instructions, since
        the seat decided "the Monitor tool is unavailable" after a
        compaction and left its beacon dead. A review found the first build
        of it seeded a continued run with the checkpoint it continued from,
        so a run that closed with no checkpoint of its own was kept under the
        new history with the previous turn's state, and the next turn lost
        this one on Cursor's side; the vetted build keeps only a checkpoint
        Cursor sent during the run. The patch list names all three closures,
        so a checkout that drifts back reads as those risks by name."""
        proxy = seat.FAMILIES[FAMILY]["sidecar"]["required_patches"][
            "src/proxy.ts"]
        self.assertIn("conversation-kept", proxy["patches"])
        self.assertIn("kept-checkpoint-is-the-runs-own", proxy["patches"])
        self.assertIn("tools-in-mcp-instructions", proxy["patches"])

    def test_every_key_is_read_and_no_other_key_is_admitted(self):
        spec = self.table()["fam"]["sidecar"]
        self.assertIn("unknown ['keeper']", seat_catalog._sidecar_error(
            self.table(keeper="bridge-keeper.sh")))
        del spec["log"]
        self.assertIn("missing ['log']", seat_catalog._sidecar_error(
            {"fam": {"base_url": "http://127.0.0.1:18315/v1", "sidecar": spec}}))

    def test_provenance_is_a_full_pin_an_https_origin_and_digests(self):
        live = seat.FAMILIES[FAMILY]["sidecar"]
        self.assertTrue(live["origin"].startswith("https://"))
        self.assertEqual(len(live["pin"]), 40)
        self.assertIn("40-hex pin", seat_catalog._sidecar_error(
            self.table(pin=live["pin"][:7])))
        self.assertIn("https origin", seat_catalog._sidecar_error(
            self.table(origin="git@example.com:bridge.git")))
        for bad in ({}, {"src/proxy.ts": "a" * 64},
                    {"src/proxy.ts": {"sha256": "a" * 63, "patches": ("x",)}},
                    {"src/proxy.ts": {"sha256": "a" * 64, "patches": ()}}):
            self.assertIn("required_patches", seat_catalog._sidecar_error(
                self.table(required_patches=bad)), bad)

    def test_the_route_must_be_a_loopback_port_outside_helms_bands(self):
        remote = self.table()
        remote["fam"]["base_url"] = "https://bridge.example.com/v1"
        self.assertIn("no local port", seat_catalog._sidecar_error(remote))
        inside = self.table()
        inside["fam"]["base_url"] = "http://127.0.0.1:8330/v1"
        self.assertIn("inside the range", seat_catalog._sidecar_error(inside))
        twice = dict(self.table(), other=self.table()["fam"])
        self.assertIn("both route", seat_catalog._sidecar_error(twice))


if __name__ == "__main__":
    unittest.main()
