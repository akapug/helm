#!/usr/bin/env python3
"""helm.webshot — capture-verb contract tests. Runs against a FAKE chrome
script on PATH that speaks the --remote-debugging-pipe CDP protocol
(NUL-delimited JSON over fd 3 in / fd 4 out) and a monkeypatched server-start.
No real helm server, no real chrome, and no image is ever opened. The test
only checks argv, file existence/size, stdout, and that no process is left
behind.

The fake chrome script (written by _fake_cdp_chrome) behaves like the probe:
  - records argv
  - answers createTarget / attachToTarget / setDeviceMetricsOverride /
    Page.enable / Page.navigate with fixed results
  - answers each Runtime.evaluate with the NEXT text from the mode's list,
    repeating the last (so a "stays at 1" mode keeps returning one "not read")
  - answers captureScreenshot with base64 of a given byte count
  - answers Browser.close with {} and then exits
  - modes 'hang' (never answers) and 'exit-at-once' (dies before answering)
"""
import base64
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import webshot  # noqa: E402


def _fake_cdp_chrome(tmpdir, argv_file, png_bytes, texts,
                     hang=False, exit_at_once=False):
    """Write a fake chrome that speaks the CDP pipe protocol.

    ``texts`` is the list of Runtime.evaluate results; the fake returns the
    NEXT one each evaluate call, repeating the last (so a mode with one entry
    keeps returning it). ``png_bytes`` is the raw PNG size the fake hands back
    base64-encoded in the captureScreenshot result. ``hang`` makes the fake
    never answer (sleeping forever after the last command) so a per-call
    timeout fires. ``exit_at_once`` makes it die before answering (rc != 0,
    empty fd 4)."""
    script = os.path.join(tmpdir, "fake-chrome")
    texts_repr = repr(list(texts))
    if texts:
        text_body = (
            "def next_text():\n"
            "    global idx\n"
            "    idx = idx + 1\n"
            "    return list_of_texts[min(idx, len(list_of_texts) - 1)]\n")
    else:
        text_body = (
            "def next_text():\n"
            "    return ''\n")
    body = (
        "#!/usr/bin/env python3\n"
        "import sys, os, time\n"
        "import json\n"
        "argv_file = %r\n"
        "with open(argv_file, 'a') as ah:\n"
        "    ah.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "list_of_texts = %s\n"
        "png_bytes = %d\n"
        "idx = 0\n"
        "hang = %d\n"
        "exit_at_once = %d\n"
        % (argv_file, texts_repr, png_bytes, int(hang), int(exit_at_once))
    )
    body += text_body
    body += (
        "def write_json(obj):\n"
        "    os.write(out_fd, json.dumps(obj).encode('utf-8') + b'\\0')\n"
        "in_fd = 3\n"
        "out_fd = 4\n"
        "buffer = b''\n"
    )
    body += (
        "def read_msg():\n"
        "    global buffer\n"
        "    while True:\n"
        "        try:\n"
        "            chunk = os.read(in_fd, 1 << 20)\n"
        "        except OSError:\n"
        "            return None\n"
        "        if not chunk:\n"
        "            return None\n"
        "        buffer += chunk\n"
        "        if b'\\0' in buffer:\n"
        "            raw, buffer = buffer.split(b'\\0', 1)\n"
        "            return json.loads(raw.decode('utf-8'))\n"
        "    \n"
        "import base64\n"
        "def answer(m):\n"
        "    method, msg_id = m['method'], m['id']\n"
        "    if method == 'Target.createTarget':\n"
        "        write_json({'id': msg_id, 'result': {'targetId': 'T1'}})\n"
        "    elif method == 'Target.attachToTarget':\n"
        "        write_json({'id': msg_id, 'result': {'sessionId': 'S1'}})\n"
        "    elif method == 'Emulation.setDeviceMetricsOverride':\n"
        "        write_json({'id': msg_id})\n"
        "    elif method == 'Page.enable':\n"
        "        write_json({'id': msg_id})\n"
        "    elif method == 'Page.navigate':\n"
        "        write_json({'id': msg_id})\n"
        "    elif method == 'Runtime.evaluate':\n"
        "        write_json({'id': msg_id,\n"
        "                    'result': {'result': {'type': 'string',\n"
        "                                          'value': next_text()}}})\n"
        "    elif method == 'Page.captureScreenshot':\n"
        "        b64 = base64.b64encode(bytes(png_bytes)).decode('utf-8')\n"
        "        write_json({'id': msg_id, 'result': {'data': b64, 'format': 'png'}})\n"
        "    elif method == 'Browser.close':\n"
        "        write_json({'id': msg_id})\n"
        "    else:\n"
        "        write_json({'id': msg_id})\n"
        "if not hang and not exit_at_once:\n"
        "    while True:\n"
        "        m = read_msg()\n"
        "        if m is None:\n"
        "            break\n"
        "        answer(m)\n"
        "if hang:\n"
        "    while True:\n"
        "        time.sleep(1.0)\n"
        "if exit_at_once:\n"
        "    sys.exit(1)\n"
    )
    with open(script, "w") as f:
        f.write(body)
    os.chmod(script, 0o755)
    return script


def _find_argv(argv_file):
    """Return the lines of argv recorded by the fake chrome."""
    lines = []
    if os.path.exists(argv_file):
        with open(argv_file) as f:
            lines = f.read().splitlines()
    return lines


class WebShotTest(unittest.TestCase):
    """Contract tests for webshot.run() using a fake CDP chrome and a fake
    server."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="webshot-test-")
        self.tree = os.path.join(self.tmpdir, "tree")
        os.makedirs(os.path.join(self.tree, "helm"), exist_ok=True)
        self.chrome_dir = os.path.join(self.tmpdir, "chrome")
        os.makedirs(self.chrome_dir, exist_ok=True)
        self.argv_file = os.path.join(self.chrome_dir, "argv.txt")
        # Default fake chrome: 2000-byte PNG, texts go three "not read" -> zero.
        self._fake_chrome_path = _fake_cdp_chrome(
            self.chrome_dir, self.argv_file, 2000,
            ["not read not read not read",
             "not read not read",
             "not read",
             "ready text"],
            hang=False, exit_at_once=False)
        os.chmod(self.chrome_dir, 0o755)
        self.port_patch = mock.patch("helm.webshot.pick_free_port",
                                     return_value=7610)
        self.port_patch.start()
        _state = {"done": False}
        self.fake_popen = mock.MagicMock(spec=subprocess.Popen)

        def _poll(*a, **kw):
            return None if not _state["done"] else 0
        def _signal(*a, **kw):
            _state["done"] = True
            return None
        self.fake_popen.terminate = mock.Mock(side_effect=_signal)
        self.fake_popen.kill = mock.Mock(side_effect=_signal)
        self.fake_popen.poll = mock.Mock(side_effect=_poll)
        self.server_patch = mock.patch(
            "helm.webshot.start_server",
            new=mock.Mock(return_value=self.fake_popen))
        self.server_patch.start()
        self.out_dir = os.path.join(self.tmpdir, "out")
        os.makedirs(self.out_dir, exist_ok=True)
        new_path = os.path.join(
            os.path.dirname(self._fake_chrome_path),
            os.pathsep, os.environ.get("PATH", ""))
        self.path_patch = mock.patch.dict(os.environ,
                                         {"PATH": new_path},
                                         clear=False)
        self.path_patch.start()
        self.chrome_patch = mock.patch(
            "helm.webshot.find_chrome", return_value=self._fake_chrome_path)
        self.chrome_patch.start()

    def tearDown(self):
        self.port_patch.stop()
        self.server_patch.stop()
        self.path_patch.stop()
        self.chrome_patch.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run(self, **kw):
        kwargs = {
            "tree": self.tree,
            "view": "sessions",
            "label": "after",
            "widths": [1440, 420],
            "out_dir": self.out_dir,
        }
        kwargs.update(kw)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), \
             contextlib.redirect_stderr(stderr):
            exit_code = webshot.run(**kwargs)
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def _set_chrome(self, path):
        self.chrome_patch.stop()
        self.chrome_patch = mock.patch(
            "helm.webshot.find_chrome", return_value=path)
        self.chrome_patch.start()

    def test_a_three_not_read_settle_to_zero(self):  # noqa: VACUOUS_ASSERTION — for-loops iterate a fixed width set, so every control runs; unconditional exit-code + "not read" absence assertion already present
        """Arms a: texts go from three "not read" to none. No WARN line, and
        the PNG has the fake's 2000 bytes. Exit 0."""
        with mock.patch("helm.webshot.SETTLE_HOLD_S", 0.3), \
             mock.patch("helm.webshot.SETTLE_POLL_S", 0.2), \
             mock.patch("helm.webshot.SETTLE_DEADLINE_S", 3.0):
            exit_code, stdout, stderr = self._run()
        self.assertEqual(exit_code, 0,
                         "stdout=%r stderr=%r" % (stdout, stderr))
        for width in (1440, 420):
            path = os.path.join(self.out_dir, "after-sessions-%d.png" % width)
            self.assertTrue(os.path.exists(path), "missing %s" % path)
            self.assertEqual(os.path.getsize(path), 2000,
                             "PNG size %d not the fake's 2000 bytes" %
                             os.path.getsize(path))
        self.assertNotIn("not read", stdout,
                         "no WARN expected when count settled to zero; %r"
                         % stdout)
        for width in (1440, 420):
            self.assertIn("TEXT %d: ready text" % width, stdout,
                          "missing TEXT line for %d" % width)

    def test_b_stays_at_one_not_read_warns(self):  # noqa: VACUOUS_ASSERTION — for-loops iterate a fixed width set, so every control runs; unconditional exit-code assertion already present
        """Arms b: texts stay at one "not read". The capture happens once the
        count holds for SETTLE_HOLD_S (patched small), and the WARN says 1."""
        texts = ["one not read", "one not read", "one not read"]
        chrome = _fake_cdp_chrome(self.chrome_dir, self.argv_file, 2000, texts)
        self._set_chrome(chrome)
        with mock.patch("helm.webshot.SETTLE_HOLD_S", 0.3), \
             mock.patch("helm.webshot.SETTLE_POLL_S", 0.2), \
             mock.patch("helm.webshot.SETTLE_DEADLINE_S", 3.0):
            exit_code, stdout, stderr = self._run()
        self.assertEqual(exit_code, 0, "stdout=%r stderr=%r" % (stdout, stderr))
        for width in (1440, 420):
            self.assertIn("WARN %d: 1" % width, stdout,
                          "missing WARN for width %d in %r" % (width, stdout))
            self.assertNotIn("never finished loading", stdout,
                             "stale refusal present: %r" % stdout)
            path = os.path.join(self.out_dir, "after-sessions-%d.png" % width)
            self.assertTrue(os.path.exists(path), "missing %s" % path)
            self.assertEqual(os.path.getsize(path), 2000,
                             "PNG %s wrong size" % path)

    def test_c_never_answers_refusal_names_method(self):
        """Arms c: a fake that never answers. A refusal naming the method, no
        PNG left, and the fake process has exited (the finally killpg)."""
        chrome = _fake_cdp_chrome(self.chrome_dir, self.argv_file, 2000,
                                  ["never"], hang=True)
        self._set_chrome(chrome)
        with mock.patch("helm.webshot.READY_TIMEOUT_S", 1), \
             mock.patch("helm.webshot.CDP_TIMEOUT_S", 1):
            exit_code, stdout, stderr = self._run()
        self.assertEqual(exit_code, 1,
                         "expected refusal, stdout=%r stderr=%r" %
                         (stdout, stderr))
        self.assertIn("did not answer", stderr,
                      "no 'did not answer' in stderr %r" % stderr)
        for width in (1440, 420):
            path = os.path.join(self.out_dir, "after-sessions-%d.png" % width)
            self.assertFalse(os.path.exists(path),
                             "leftover PNG %s after hanging chrome" % path)
        self.assertTrue(os.path.exists(self.argv_file), "fake not launched")

    def test_d_exits_at_once_chrome_closed_pipe(self):  # noqa: VACUOUS_ASSERTION — unconditional positive control is the stderr message match; the absent-PNG assertions are the named negative assertion (a refusal must leave no file)
        """Arms d: a fake that exits at once. A refusal saying Chrome closed
        the pipe."""
        chrome = _fake_cdp_chrome(self.chrome_dir, self.argv_file, 2000,
                                  ["x"], exit_at_once=True)
        self._set_chrome(chrome)
        exit_code, stdout, stderr = self._run()
        self.assertEqual(exit_code, 1,
                         "expected refusal, stdout=%r stderr=%r" %
                         (stdout, stderr))
        self.assertIn("chrome closed the pipe", stderr,
                      "expected 'chrome closed the pipe' in stderr %r" % stderr)
        for width in (1440, 420):
            path = os.path.join(self.out_dir, "after-sessions-%d.png" % width)
            self.assertFalse(os.path.exists(path),
                             "leftover PNG %s" % path)

    def test_e_no_leftover_process_on_success_and_refusal(self):  # noqa: VACUOUS_ASSERTION — the empty-list equality on the /proc scan is the named negative assertion (a capture must leave no process); unconditional exit-code and poll() controls are present on both arms
        """Arms e: after run() returns, on success AND on refusal, neither the
        fake Chrome's pid nor the server's pid exists. Record the pids through
        the Popen objects."""
        # --- success arm ---
        rc, _, _ = self._run()
        self.assertEqual(rc, 0, "success arm exit %d" % rc)
        self.assertTrue(self.fake_popen.poll() is not None,
                        "success: server child poll() is None")
        # --- refusal arm ---
        chrome = _fake_cdp_chrome(self.chrome_dir, self.argv_file, 2000,
                                  ["never"], hang=True)
        self._set_chrome(chrome)
        with mock.patch("helm.webshot.READY_TIMEOUT_S", 1), \
             mock.patch("helm.webshot.CDP_TIMEOUT_S", 1), \
             mock.patch("helm.webshot.CLOSE_TIMEOUT_S", 1), \
             mock.patch("helm.webshot.EXIT_WAIT_S", 1):
            rc, _, _ = self._run()
        self.assertEqual(rc, 1, "refusal arm exit %d" % rc)
        self.assertTrue(self.fake_popen.poll() is not None,
                        "refusal: server child poll() is None")
        # No leftover chrome: scan /proc for a python process whose cmdline
        # includes the fake chrome path.
        chrome_base = os.path.basename(self._fake_chrome_path)
        leftover = []
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open("/proc/%s/cmdline" % pid, "rb") as f:
                    cmdline = f.read().decode("utf-8", "replace")
            except (OSError, PermissionError):
                continue
            if chrome_base in cmdline and int(pid) != os.getpid():
                leftover.append(pid)
        self.assertEqual(leftover, [], "leftover chrome pids: %r" % leftover)

    def test_f_port_window_and_profile_and_tree(self):  # noqa: VACUOUS_ASSERTION — unconditional assertTrue(uds) positive control guards the per-profile loop assertions; port-window and tree-mutation assertions are unconditional
        """Arms f: every retained contract still holds — the port window, the
        throwaway profile (under system temp dir), the tree unchanged, and
        partial PNGs deleted on refusal."""
        self.port_patch.stop()
        import socket as _sock
        occupied = _sock.socket()
        occupied.bind(("127.0.0.1", 7600))
        try:
            port = webshot.pick_free_port()
        finally:
            occupied.close()
        self.assertNotIn(port, (7433, 7481), "owner port picked")
        self.assertNotEqual(port, 7600, "occupied port picked")
        self.port_patch.start()

        self._run()
        lines = _find_argv(self.argv_file)
        # Unconditional: the fake was launched at least once, so there must be
        # user-data-dir args to check (two widths -> at least one argv line
        # per launch; the argv file accumulates both).
        uds = []
        for line in lines:
            if "--user-data-dir=" in line:
                uds.append(line.split("--user-data-dir=", 1)[1].strip())
        self.assertTrue(uds,
                        "no --user-data-dir recorded; argv lines %r" % lines)
        for flag in ("--headless=new", "--remote-debugging-pipe"):
            self.assertTrue(any(flag in line for line in lines),
                            "chrome argv lacks %s: %r" % (flag, lines))
        for ud in uds:
            self.assertTrue(os.path.abspath(ud).startswith(
                os.path.abspath(tempfile.gettempdir())),
                "profile %r not under system temp" % ud)
            self.assertNotIn(".config", ud,
                            "profile %r touches owner profile" % ud)
        self.assertEqual(os.listdir(self.tree), ["helm"],
                         "tree mutated: %r" % os.listdir(self.tree))

        small = _fake_cdp_chrome(self.chrome_dir, self.argv_file, 10,
                                 ["ready text"])
        self._set_chrome(small)
        exit_code, stdout, stderr = self._run()
        self.assertEqual(exit_code, 1, "expected refusal for tiny PNG")
        for width in (1440, 420):
            path = os.path.join(self.out_dir, "after-sessions-%d.png" % width)
            self.assertFalse(os.path.exists(path),
                             "leftover partial PNG %s" % path)

    def test_g_text_excerpt_capped_at_300(self):  # noqa: VACUOUS_ASSERTION — unconditional positive control is the assertTrue(found) after the loop; the in-if assertions are the named per-line checks on the same 300-char observable
        """The printed excerpt is the settled view text truncated to at most
        300 chars."""
        long_text = "a" * 400
        chrome = _fake_cdp_chrome(self.chrome_dir, self.argv_file, 2000,
                                  [long_text])
        self._set_chrome(chrome)
        exit_code, stdout, stderr = self._run()
        self.assertEqual(exit_code, 0, "stdout=%r" % stdout)
        found = False
        for line in stdout.splitlines():
            if line.startswith("TEXT 1440:"):
                excerpt = line.split("TEXT 1440: ", 1)[1]
                self.assertEqual(len(excerpt), 300,
                                 "excerpt length %d" % len(excerpt))
                self.assertEqual(excerpt, "a" * 300,
                                 "excerpt %r not the first 300" % excerpt)
                found = True
                break
        self.assertTrue(found, "no TEXT 1440: line in stdout %r" % stdout)

    def test_h_server_gone_proof_runtime_check(self):
        """The server-gone proof in _finish_refusal is a runtime if-statement,
        not an assert: _finish_refusal raises RuntimeError when the child is
        still running and the port is busy; no raise on the happy path."""
        pop = mock.MagicMock(spec=subprocess.Popen)
        pop.poll.return_value = None
        with mock.patch.object(webshot, "_port_free", return_value=False), \
             mock.patch.object(webshot, "_stop_server", return_value=True):
            with self.assertRaises(RuntimeError) as ctx:
                webshot._finish_refusal(pop, 7610, "test message")
            msg = str(ctx.exception)
            self.assertIn("7610", msg, "message missing port in %r" % msg)
            self.assertIn("poll()=", msg, "message missing poll() in %r" % msg)
        pop2 = mock.MagicMock(spec=subprocess.Popen)
        pop2.poll.return_value = 0
        with contextlib.redirect_stderr(io.StringIO()), \
             mock.patch.object(webshot, "_port_free", return_value=True), \
             mock.patch.object(webshot, "_stop_server", return_value=True):
            webshot._finish_refusal(pop2, 7610, "ok")

    def test_i_midload_not_read_yet_prints_warn(self):  # noqa: VACUOUS_ASSERTION — intentional negative control; positive controls (assertIn) are unconditional, absence (assertNotIn) is the named negative assertion
        """J2: a view whose text still holds the page's pending placeholder
        ("not read") twice prints one WARN line naming the width and the
        count before the ready line, and the run still succeeds (rc 0). A view
        without the placeholder prints no WARN. The placeholder is NOT in
        STALE_TEXT, so the capture is not refused and is not retried.
        Restored from part 1 (test_j_midload_not_read_yet_prints_warn),
        adapted to the single-pass CDP code: the placeholder is the page's
        "not read" text, and the fake hands it back via Runtime.evaluate's
        value (not a DOM string)."""
        # First arm: placeholder present twice (count == 2).
        texts_with = ["not read not read"]
        chrome_with = _fake_cdp_chrome(self.chrome_dir, self.argv_file,
                                       2000, texts_with)
        self._set_chrome(chrome_with)
        with mock.patch("helm.webshot.SETTLE_HOLD_S", 0.3), \
             mock.patch("helm.webshot.SETTLE_POLL_S", 0.2), \
             mock.patch("helm.webshot.SETTLE_DEADLINE_S", 3.0):
            exit_code, stdout, stderr = self._run()
        self.assertEqual(exit_code, 0,
                         "placeholder capture should succeed, stdout=%r "
                         "stderr=%r" % (stdout, stderr))
        # "not read" is not a STALE_TEXT, so the stale-refusal reason is
        # absent from the output (the capture is not refused and not retried).
        self.assertNotIn("never finished loading", stdout,
                         "stale refusal present: %r" % stdout)
        # One WARN line per width, naming the count (2), unconditional — no
        # loop, so each assertion is a positive control the guard cannot hide.
        for_width_1440 = (
            "WARN 1440: 2 sections still read \"not read\" after the view "
            "settled (their reads are pending or unreadable); those sections "
            "show no data")
        for_width_420 = (
            "WARN 420: 2 sections still read \"not read\" after the view "
            "settled (their reads are pending or unreadable); those sections "
            "show no data")
        self.assertIn(for_width_1440, stdout,
                      "missing WARN line for width 1440 in stdout %r" % stdout)
        self.assertIn(for_width_420, stdout,
                      "missing WARN line for width 420 in stdout %r" % stdout)
        # WARN prints before the ready line.
        last_warn = stdout.rfind("not read\" (the page's")
        ready = stdout.find("AFTER:")
        self.assertGreater(ready, last_warn,
                           "WARN line after the ready line: %r" % stdout)
        # Second arm: no placeholder, no WARN.
        chrome_without = _fake_cdp_chrome(self.chrome_dir, self.argv_file,
                                          2000, ["ready text"])
        self._set_chrome(chrome_without)
        with mock.patch("helm.webshot.SETTLE_HOLD_S", 0.3), \
             mock.patch("helm.webshot.SETTLE_POLL_S", 0.2), \
             mock.patch("helm.webshot.SETTLE_DEADLINE_S", 3.0):
            exit_code, stdout, _ = self._run()
        self.assertEqual(exit_code, 0, "stdout=%r" % stdout)
        self.assertNotIn("WARN ", stdout,
                         "no placeholder in text yet WARN present: %r" % stdout)

    def test_j_hanging_chrome_timed_out_no_png_port_free(self):  # noqa: VACUOUS_ASSERTION — the unconditional "did not answer" stderr message match is the positive control for the refusal; the absent-PNG assertions and the port-free/poll() checks are the named negative assertion (a hung run must leave no file and a free port)
        """Restored from part 1 (test_j_hanging_chrome_no_png_timed_out_msg).
        A hung chrome (never answers a CDP call; patched CDP timeout 1 s)
        leaves no PNG, exit is 1, stderr says the call "did not answer"
        (the timed-out words, not only a raw rc), no chrome process is
        left, and the server child is gone AND the port is free after run()
        returns (the runtime proof in _finish_refusal). The single-pass CDP
        code has no DOM budget loop, so the hang surfaces as a per-call
        timeout, not a virtualTimeBudget retry."""
        # A fake that records argv and never answers (the 'hang' mode), so
        # the first unanswered CDP call trips the patched CDP_TIMEOUT_S.
        chrome = _fake_cdp_chrome(self.chrome_dir, self.argv_file, 2000,
                                  ["x"], hang=True)
        self._set_chrome(chrome)
        with mock.patch("helm.webshot.READY_TIMEOUT_S", 1), \
             mock.patch("helm.webshot.CDP_TIMEOUT_S", 1), \
             mock.patch("helm.webshot.CLOSE_TIMEOUT_S", 1), \
             mock.patch("helm.webshot.EXIT_WAIT_S", 1):
            exit_code, stdout, stderr = self._run()
        # No PNG left behind.
        for width in (1440, 420):
            path = os.path.join(self.out_dir, "after-sessions-%d.png" % width)
            self.assertFalse(os.path.exists(path),
                             "leftover PNG %s after a hung chrome" % path)
        self.assertEqual(exit_code, 1,
                         "expected exit 1 on hang, stdout=%r stderr=%r" %
                         (stdout, stderr))
        # The message names the timed-out CDP call in words.
        self.assertIn("did not answer", stderr,
                      "no 'did not answer' (timeout) in stderr %r" % stderr)
        # The server child is gone and the port is free (runtime proof).
        self.assertTrue(self.fake_popen.poll() is not None,
                        "server child poll() is None after hung run")
        self.assertTrue(webshot._port_free(7610),
                        "port 7610 still in use after a hung run")
        # No chrome process is left behind.
        chrome_base = os.path.basename(self._fake_chrome_path)
        leftover = []
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open("/proc/%s/cmdline" % pid, "rb") as f:
                    cmdline = f.read().decode("utf-8", "replace")
            except (OSError, PermissionError):
                continue
            if chrome_base in cmdline and int(pid) != os.getpid():
                leftover.append(pid)
        self.assertEqual(leftover, [], "leftover chrome pids: %r" % leftover)

    def test_l_cleanup_failure_named(self):
        """When _cleanup_pngs cannot delete a file (OSError on os.remove), it
        returns that path in the failed list and the refusal names it."""
        png = os.path.join(self.out_dir, "leftover.png")
        with open(png, "w") as f:
            f.write("x")
        orig_remove = os.remove
        def _failing_remove(path):
            if path == png:
                raise OSError("no permission")
            return orig_remove(path)
        with mock.patch("os.remove", _failing_remove):
            deleted, failed = webshot._cleanup_pngs([png], None)
        self.assertEqual(deleted, 0, "expected 0 deleted")
        self.assertEqual(failed, [png],
                         "cleanup did not name %s" % png)
        suffix = webshot._cleanup_suffix(failed)
        self.assertIn("could not delete", suffix)
        self.assertIn(os.path.basename(png), suffix)

    def test_m_generic_exception_cleans_partial_pngs(self):  # noqa: VACUOUS_ASSERTION — unconditional exit-code and "deleted 2 partial captures" controls are present; the per-width isNotIn assertions are the named negative assertion (a refused run must leave no file)
        """Arms m: an arbitrary exception in run() (not a per-width refusal)
        still runs _cleanup_pngs(all_paths, None), so both in-flight PNGs
        are gone and the refusal names them. _capture_view is the seam."""
        for width in (1440, 420):
            path = os.path.join(self.out_dir,
                                "after-sessions-%d.png" % width)
            with open(path, "w") as f:
                f.write("partial")
        with mock.patch("helm.webshot._capture_view",
                        side_effect=ValueError("generic boom")):
            exit_code, stdout, stderr = self._run()
        self.assertEqual(exit_code, 1, "expected refusal on generic exception")
        self.assertIn("generic boom", stderr)
        self.assertIn("deleted 2 partial captures", stderr,
                      "generic-exception refusal named no cleanup: %r" % stderr)
        for width in (1440, 420):
            path = os.path.join(self.out_dir,
                                "after-sessions-%d.png" % width)
            self.assertFalse(os.path.exists(path),
                             "leftover partial PNG %s" % path)

    def test_n_no_chrome_on_path_refuses_and_names_it(self):
        """No Chrome-family binary on PATH: a refusal that says so, exit 1,
        and no PNG."""
        self._set_chrome(None)
        exit_code, stdout, stderr = self._run()
        self.assertEqual(exit_code, 1, "stdout=%r stderr=%r" % (stdout, stderr))
        self.assertIn("no chrome on PATH", stderr)
        for width in (1440, 420):
            self.assertFalse(os.path.exists(os.path.join(
                self.out_dir, "after-sessions-%d.png" % width)))

    def test_o_the_text_is_read_where_real_chrome_puts_it(self):
        """Runtime.evaluate's text sits in result.result.value. A reader of
        the old one-level shape gets nothing, so this pins the real one."""
        real = {"result": {"type": "string", "value": "0 green"}}
        with mock.patch.object(webshot, "_cdp_call", return_value=real):
            self.assertEqual(webshot._text_of(None, None, None, "S1"), "0 green")
        flat = {"value": "0 green"}
        with mock.patch.object(webshot, "_cdp_call", return_value=flat):
            self.assertEqual(webshot._text_of(None, None, None, "S1"), "")

    def test_m_home_dir_rmtree_on_exit(self):  # noqa: VACUOUS_ASSERTION — the absent-home-dir assertions are the named negative assertion (run() must always delete its child home); unconditional exit-code and isNone-home controls are present on both arms
        """The child server's temp home dir (_start_child_home) is rmtree'd by
        run() on every exit, both success and refusal. The tree is untouched."""
        home_dir = tempfile.mkdtemp(prefix="webshot-home-test-")
        webshot._start_child_home = home_dir
        with mock.patch("helm.webshot.start_server",
                        return_value=self.fake_popen):
            exit_code, _, _ = self._run()
        self.assertEqual(exit_code, 0, "success arm %d" % exit_code)
        self.assertFalse(os.path.exists(home_dir),
                         "home dir %s still exists" % home_dir)
        self.assertIsNone(webshot._start_child_home,
                         "home not cleared after success")
        self._set_chrome(None)
        home_dir2 = tempfile.mkdtemp(prefix="webshot-home-test-")
        webshot._start_child_home = home_dir2
        with mock.patch("helm.webshot.start_server",
                        return_value=self.fake_popen):
            exit_code, _, _ = self._run()
        self.assertEqual(exit_code, 1, "refusal arm %d" % exit_code)
        self.assertFalse(os.path.exists(home_dir2),
                         "refusal home dir %s still exists" % home_dir2)
        self.assertIsNone(webshot._start_child_home,
                         "home not cleared after refusal")
        self.assertEqual(os.listdir(self.tree), ["helm"],
                         "tree mutated: %r" % os.listdir(self.tree))
