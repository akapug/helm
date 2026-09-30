"""helm.webshot — the capture verb's plumbing: screenshots of a web tree at a
chosen view, before or after a change. Stands up the tree's own web server on a
throwaway port in a child process, drives a FRESH Chrome over its
--remote-debugging-pipe (never the owner's) at each requested width, and writes
before/after PNGs. Stlib only.

The verb (cmd_web in web_server.py) parses args and calls run(); this module
owns all the process plumbing and the browser contract the tests pin down
(tests/test_webshot.py):

- pick_free_port() -> int in [7600, 7700). The window is by design; the two
  owner ports (7433, 7481) are not even in it, so nothing here can ever bind
  them. It picks by actually binding a socket (so a live listener is skipped),
  never by guessing.
- start_server(tree, port) -> the child Popen. Launches the server, THEN
  waits GET / -> 200 (urllib, 20s); on a dead/broken server it kills the child
  and raises (so run() can refuse). start_new_session=True puts it in its own
  process group so killpg stops the whole thing.
- find_chrome()     -> path to a Chrome/Chromium binary or None.
- run(tree, view, label, widths, out_dir) -> exit code (0 ok, 1 refused).
  Prints every PNG path then a final "BEFORE:"/"AFTER:" ready line on stdout;
  refusals go to stderr and name the offending file. The server child is
  ALWAYS stopped on every exit (try/finally). Each width is one Chrome process
  driven over CDP: it navigates the view, waits until the page's reads settle
  (see the settled constants), and screenshots, and exits. The settled rule is
  "text non-empty AND the 'not read' count has not changed for SETTLE_HOLD_S,
  or the count is 0", poll every SETTLE_POLL_S, deadline SETTLE_DEADLINE_S
  from navigation; at the deadline it captures anyway and warns when the final
  count is above 0.
"""
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from http import client as http_client

# Throwaway capture port window. 7433 (owner's web server) and 7481 (owner's
# *second* server) are deliberately OUTSIDE it, so a capture can never shadow
# either — invariants hold by construction, not by a check.
_PORT_MIN = 7600
_PORT_MAX = 7700   # exclusive bound; window is [7600, 7699]
READY_TIMEOUT_S = 20
# Per-CDP-command timeout. A missing answer is a refusal, never a hang.
CDP_TIMEOUT_S = 30
CLOSE_TIMEOUT_S = 5
EXIT_WAIT_S = 10
MIN_PNG_BYTES = 1000
# The settled-window constants (patchable by the tests). The page prints "not
# read" for a read that is pending OR unreadable, so a home capture rarely
# reaches a true zero; settled is therefore "text non-empty AND the count has
# not changed for SETTLE_HOLD_S (poll every SETTLE_POLL_S), or count is 0".
# SETTLE_DEADLINE_S is the cap from navigation — at the deadline the capture
# happens anyway and the final count drives the WARN.
SETTLE_POLL_S = 0.5
SETTLE_HOLD_S = 3.0
SETTLE_DEADLINE_S = 30.0
NOT_READ = "not read"
# The text that says the view has not yet rendered its data. Only the
# sessions/ledger empty-state placeholder ("loading…") is a true loading
# signal. "not read" is the page's pending placeholder — it appears while
# a read is in flight — and is warned about, never retried.
STALE_TEXT = ("loading…",)
# The three names the OS may use for a Chrome-family binary; first
# shutil.which hit wins.
_CHROME_NAMES = ("google-chrome", "chromium", "chromium-browser")


def pick_free_port():
    """Bind a real socket; hand back the first free port in the capture window.
    The window itself excludes the two owner ports, so no branch picks one."""
    for port in range(_PORT_MIN, _PORT_MAX):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("127.0.0.1", port))
                s.close()
                return port
        except OSError:
            continue
    raise RuntimeError("no free port in %d-%d" % (_PORT_MIN, _PORT_MAX - 1))


def find_chrome():
    """First Chrome-family binary on PATH via shutil.which over the three names;
    None when none resolves."""
    for name in _CHROME_NAMES:
        path = shutil.which(name)
        if path:
            return path
    return None


_start_child_home = None  # set by _start_child; run() rmtree's it on exit (J1)

def _start_child(tree, port, stderr_log=None):
    """Launch the child that stands up the tree's own web server and serves it.
    The child imports the tree's `helm` package (its own sys.path, so no host
    shadow), pulls in `helm.web` so web_server splices the API globals, binds
    `make_server(port)` and blocks in serve_forever. start_new_session=True
    puts it in its own process group for killpg. stderr (the BrokenPipe lines)
    is sent to `stderr_log` so it never floods the verb's stdout."""
    cwd = os.path.abspath(tree)
    code = (
        "import os, sys\n"
        "os.chdir(%r)\n"
        "sys.path.insert(0, %r)\n"
        "import helm.web  # facade; makes web_server splice its API globals\n"
        "from helm.web_server import make_server\n"
        "srv = make_server(%d)\n"
        "srv.serve_forever()\n"
    ) % (cwd, cwd, port)
    home = tempfile.mkdtemp(prefix="webshot-home-")
    env = dict(os.environ)
    # No host-level HELM_* may point the child's web home away from the tree
    # under capture; a fresh server serves the tree, so give it a throwaway
    # home outside the tree. run() rmtree's it on every exit (J1 cure).
    env["HELM_HOME"] = home
    env.pop("HELM_ADOPTED_DIR", None)
    global _start_child_home
    _start_child_home = home
    stderr = subprocess.DEVNULL
    if stderr_log:
        os.makedirs(os.path.dirname(stderr_log), exist_ok=True)
        stderr_log_fd = open(stderr_log, "w")
        stderr = stderr_log_fd
    return subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=cwd,
        start_new_session=True,
        env=env,
        stderr=stderr,
    )


def _wait_for_url(port, timeout_s):
    """True once GET http://127.0.0.1:port/ returns 200 within timeout_s."""
    import time

    url = "http://127.0.0.1:%d/" % port
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "helm-webshot/1"})
            with urllib.request.urlopen(req, timeout=2) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, http_client.HTTPException, OSError):
            pass
        time.sleep(0.3)
    return False


def _kill_group(pop):
    """Terminate the child's whole process group, then SIGKILL survivors. Only
    the Popen we hold — no pkill/killall. Returns True once poll() is not None."""
    import os as _os
    import signal as _sig
    import time as _time

    def _pgid():
        try:
            return _os.getpgid(pop.pid)
        except (OSError, AttributeError):
            return None

    def _kill(sig):
        gid = _pgid()
        try:
            _os.killpg(gid, sig)
        except (OSError, TypeError, AttributeError):
            try:
                pop.kill() if sig == _sig.SIGKILL else pop.terminate()
            except Exception:
                pass

    try:
        _kill(_sig.SIGTERM)
    except Exception:
        pop.terminate()
    _time.sleep(0.5)
    if pop.poll() is None:
        try:
            _kill(_sig.SIGKILL)
        except Exception:
            pop.kill()
    return pop.poll() is not None


def start_server(tree, port, out_dir=None):
    """Launch the tree's web server as a detached child and confirm it serves:
    wait GET / -> 200 (urllib, 20s). On a dead/broken server, kill the child
    and raise RuntimeError (run() refuses). Return the live child's Popen.
    With `out_dir` the child's stderr goes to <out-dir>/server.log so it does
    not flood the verb's stdout; without it it is DEVNULL."""
    stderr_log = None
    if out_dir:
        os.makedirs(os.path.abspath(out_dir), exist_ok=True)
        stderr_log = os.path.join(os.path.abspath(out_dir), "server.log")
    pop = _start_child(tree, port, stderr_log)
    if not _wait_for_url(port, READY_TIMEOUT_S):
        _kill_group(pop)
        raise RuntimeError(
            "server on port %d never answered 200 within %.0fs" % (port, READY_TIMEOUT_S))
    return pop


def _stop_server(pop):
    """Stop the server child: terminate, then kill if it lingers. Idempotent
    (safe on an already-dead process). True once gone."""
    import time as _time

    def _alive():
        return pop.poll() is None

    if not _alive():
        return True
    try:
        pop.terminate()
    except Exception:
        pass
    _time.sleep(0.5)
    if _alive():
        try:
            pop.kill()
        except Exception:
            pass
    _time.sleep(0.3)
    return _alive() is False


def _cleanup_suffix(failed):
    """The trailing note a refusal message appends when cleanup could not
    delete every file it wrote: names each path that survived. Empty when
    the cleanup was complete."""
    if not failed:
        return ""
    return "; could not delete: %s" % ", ".join(failed)


def _port_free(port):
    """True once a connect to 127.0.0.1:port is refused (no standing listener).
    Proves a capture's throwaway port is back to the fleet after a run."""

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect(("127.0.0.1", port))
            s.close()
            return False
    except OSError:
        # refused, reset, or timed out: no live listener on this port.
        return True


def _server_gone(pop, port):
    """True once the server child is stopped (poll() not None) AND the port is
    free (connect refused). Pin of the dev-run leak that left seven 76xx
    child servers listening."""
    return pop.poll() is not None and _port_free(port)


def _cleanup_pngs(written, current):
    """Delete every PNG this run wrote: the confirmed ones in `written` plus
    the current `current` if present (a partial). Returns a (deleted, failed)
    tuple — `failed` lists each path that could not be deleted (empty on a
    full cleanup), so a caller can name what a refusal actually left behind."""
    import os as _os

    deleted = 0
    failed = []
    for p in list(written) + ([current] if current else []):
        if p and _os.path.isfile(p):
            try:
                _os.remove(p)
                deleted += 1
            except OSError:
                failed.append(p)
    return deleted, failed


def _finish_refusal(pop, port, message):
    """Stop the server child, prove no listener leaked, then write the refusal.
    Called on every refusal arm so a rejected run never outlives its server.
    `message` is the refusal body; it is prefixed "refused: "."""
    if pop is not None:
        _stop_server(pop)
        if not _server_gone(pop, port):
            raise RuntimeError(
                "refusal left the server child listening on port %d "
                "(poll()=%s, port free=%s)"
                % (port, pop.poll(), _port_free(port)))
    sys.stderr.write("refused: %s\n" % message)


def _view_slug(view):
    """View name -> filename token: '/' -> '-', other chars preserved."""
    return view.replace("/", "-")


def _make_out_path(out_dir, label, view, width):
    """<out-dir>/<label>-<slug>-<W>.png, creating the out dir on demand."""
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    slug = _view_slug(view)
    return os.path.join(out_dir, "%s-%s-%d.png" % (label, slug, width))


def _cdp_call(proc, fds, buffer, method, params=None, timeout=None,
              session=None):
    """One CDP command over the pipe. `fds` is a dict with 'in' (we write) and
    'out' (we read). The command is one JSON object plus a NUL byte; answers
    are read off the same NUL-delimited stream and matched by "id". A missing
    answer is a refusal — a TimeoutError — never a hang. Raises RuntimeError
    when the answer carries "error". Returns the answer's "result" dict
    ({} when absent)."""
    import time as _time

    if timeout is None:
        timeout = CDP_TIMEOUT_S
    import select as _select

    seq = buffer.get("_seq", 0) + 1
    buffer["_seq"] = seq
    msg = {"id": seq, "method": method, "params": params or {}}
    if session is not None:
        msg["sessionId"] = session
    os.write(fds["in"], json.dumps(msg).encode("utf-8") + b"\0")
    deadline = _time.monotonic() + timeout
    while True:
        raw = b""
        while b"\0" in buffer["buf"]:
            raw, buffer["buf"] = buffer["buf"].split(b"\0", 1)
            got = json.loads(raw.decode("utf-8"))
            if got.get("id") == seq:
                if "error" in got:
                    raise RuntimeError(
                        "%s: %s" % (method, got["error"]))
                return got.get("result", {})
        ready, _, _ = _select.select([fds["out"]], [], [],
                                     max(0.001, deadline - _time.monotonic()))
        if ready:
            chunk = os.read(fds["out"], 1 << 20)
            if not chunk:
                raise RuntimeError("chrome closed the pipe")
            buffer["buf"] += chunk
            if _time.monotonic() > deadline:
                raise TimeoutError(
                    "chrome did not answer %s within %.0fs"
                    % (method, timeout))
        elif _time.monotonic() > deadline:
            raise TimeoutError(
                "chrome did not answer %s within %.0fs"
                % (method, timeout))


def _start_chrome_proc(chrome, width):
    """Start one Chrome over --remote-debugging-pipe at `width`x900. The
    process is launched at about:blank; the caller navigates the real view via
    _cdp_prepare. Returns a (proc, fds, buffer, profile) quad: proc is the
    Popen, fds is a dict with 'in' (we write commands) and 'out' (we read
    answers), buffer is the dict the _cdp_call stream state lives in, profile
    is the throwaway per-width user-data dir (deleted by _stop_chrome_proc).
    start_new_session puts it in its own group so _stop_chrome_proc can
    killpg the whole thing."""
    import os as _os
    import signal as _sig

    profile = tempfile.mkdtemp(prefix="webshot-chrome-")
    r_cmd, w_cmd = _os.pipe()   # we write w_cmd; chrome reads fd 3
    r_res, w_res = _os.pipe()   # chrome writes fd 4; we read r_res

    def child():
        _os.dup2(r_cmd, 3)
        _os.dup2(w_res, 4)

    proc = subprocess.Popen(
        [chrome, "--headless=new", "--remote-debugging-pipe",
         "--no-sandbox", "--user-data-dir=" + profile, "about:blank"],
        pass_fds=(3, 4, r_cmd, w_res), preexec_fn=child,
        start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _os.close(r_cmd)
    _os.close(w_res)
    return (proc, {"in": w_cmd, "out": r_res}, {"buf": b""}, profile)


def _cdp_prepare(proc, fds, buffer, url, width):
    """Navigate one target to `url` at `width`x900. createTarget, attach,
    device-metrics, Page.enable, Page.navigate — every CDP command with its
    per-call timeout. Returns the sessionId. Raises on any CDP error or pipe
    close."""
    target = _cdp_call(proc, fds, buffer, "Target.createTarget",
                       {"url": "about:blank"})["targetId"]
    session = _cdp_call(proc, fds, buffer, "Target.attachToTarget",
                        {"targetId": target, "flatten": True})["sessionId"]
    _cdp_call(proc, fds, buffer, "Emulation.setDeviceMetricsOverride",
              {"width": width, "height": 900, "deviceScaleFactor": 1,
               "mobile": False}, session=session)
    _cdp_call(proc, fds, buffer, "Page.enable", session=session)
    _cdp_call(proc, fds, buffer, "Page.navigate",
              {"url": url}, session=session)
    return session


def _text_of(proc, fds, buffer, session):
    """The page's visible text: Runtime.evaluate over document.body, return
    by value, decoded to str ('' when the body is still absent)."""
    r = _cdp_call(proc, fds, buffer,
                  "Runtime.evaluate",
                  {"expression": "document.body ? document.body.innerText : ''",
                   "returnByValue": True}, session=session)
    # Runtime.evaluate answers {"result": {"type": "string", "value": ...}}:
    # the text sits one level INSIDE the command's result, which is where real
    # Chrome puts it (a remote object) and where the pipe probe reads it.
    val = (r.get("result") or {}).get("value")
    return val if isinstance(val, str) else ""


def _view_text(text):
    """The view element's visible text from a full-page innerText: trim,
    collapse whitespace. '' when empty or whitespace-only."""
    return " ".join(text.split()) if text else ""


def _capture_view(chrome, port, view, out_path, width):
    """Drive one Chrome over --remote-debugging-pipe at this width. Polls for
    the view text until it is non-empty and the "not read" count has been
    stable for SETTLE_HOLD_S (or is 0), capped by SETTLE_DEADLINE_S; a single
    Page.navigate gives the view its real time. Returns
    (exit_code, excerpt, reason): (0, excerpt, None) on success,
    (1, None, "the view never finished loading") when the deadline expires with
    the text still empty or a loading placeholder, or (1, None, reason) when
    chrome timed out, closed the pipe, refused a CDP call, or the screenshot
    failed. `excerpt` is the settled view text (<=300 chars). A WARN line
    naming the final count is printed when the count is above 0 (some sections
    still show the page's loading placeholder)."""
    import os as _os
    import time as _time

    url = "http://127.0.0.1:%d/#%s" % (port, view)
    proc, fds, buffer, profile = None, None, None, None
    reason = None
    stale = False
    try:
        proc, fds, buffer, profile = _start_chrome_proc(chrome, width)
        session = _cdp_prepare(proc, fds, buffer, url, width)
        text = ""
        count = 0
        stable_count = None
        stable_since = None
        t0 = _time.monotonic() if hasattr(_time, "monotonic") else _time.time()
        # poll every SETTLE_POLL_S; the view is settled when text is
        # non-empty AND the count has not changed for SETTLE_HOLD_S, or
        # the count is 0; the deadline SETTLE_DEADLINE_S caps the wait
        # and the capture happens anyway (WARN drives on the final count).
        deadline = t0 + SETTLE_DEADLINE_S
        while True:
            try:
                text = _view_text(_text_of(proc, fds, buffer, session))
                count = text.count(NOT_READ)
                if text and count == 0:
                    break
                if text and stable_count is not None \
                        and count == stable_count \
                        and _time.monotonic() - stable_since >= SETTLE_HOLD_S:
                    break
                if _time.monotonic() - t0 >= SETTLE_DEADLINE_S:
                    break
                if stable_count != count:
                    stable_count = count
                    stable_since = _time.monotonic()
                _time.sleep(SETTLE_POLL_S)
            except (RuntimeError, TimeoutError):
                break
        # stale: empty text means the view never rendered (no DOM, a hung
        # dump, or a page with no matching element); a STALE_TEXT hit
        # means it still shows the loading placeholder. Either way this
        # single pass is not a capture with proof.
        stale = not text or any(s in text.lower() for s in STALE_TEXT)
        if not stale:
            shot = _cdp_call(proc, fds, buffer, "Page.captureScreenshot",
                             {"format": "png", "captureBeyondViewport": True},
                             session=session)
            data = shot.get("data")
            if not data:
                return 1, None, "screenshot failed: no data in " \
                    "Page.captureScreenshot"
            payload = base64.b64decode(data.encode("utf-8"))
            if len(payload) < MIN_PNG_BYTES:
                return 1, None, "screenshot failed: only %d bytes " \
                    "(below %d)" % (len(payload), MIN_PNG_BYTES)
            _os.makedirs(os.path.dirname(out_path), exist_ok=True)
            with open(out_path, "wb") as fh:
                fh.write(payload)
            if count:
                print("WARN %d: %d sections still read \"not read\" "
                      "after the view settled (their reads are pending or "
                      "unreadable); those sections show no data"
                      % (width, count))
            return 0, (text[:300] or ""), None
    except (RuntimeError, TimeoutError, OSError) as exc:
        reason = str(exc) if exc else "chrome error"
    finally:
        if proc is not None:
            _stop_chrome_proc(proc, fds, buffer, profile)
    if stale:
        return 1, None, "the view never finished loading"
    return 1, None, reason or "chrome error"

def _stop_chrome_proc(proc, fds, buffer, profile):
    """Leave Chrome EXITED on every path: Browser.close, proc.wait(EXIT_WAIT_S),
    then kill its process group and wait again if still alive. Returns True once
    poll() is not None. Deletes the throwaway profile. Only the Popen we hold
    is signalled — no name, never pkill."""
    import os as _os
    import signal as _sig

    def _alive():
        return proc.poll() is None

    try:
        _cdp_call(proc, fds, buffer, "Browser.close",
                  {"sessionId": buffer.get("_sess")}, timeout=CLOSE_TIMEOUT_S)
    except Exception:
        pass
    for fd in (fds.get("in"), fds.get("out")):
        try:
            _os.close(fd)
        except OSError:
            pass
    try:
        proc.wait(timeout=EXIT_WAIT_S)
    except subprocess.TimeoutExpired:
        try:
            _os.killpg(_os.getpgid(proc.pid), _sig.SIGKILL)
        except (OSError, AttributeError):
            proc.kill()
        proc.wait(timeout=EXIT_WAIT_S)
    if _alive():
        try:
            _os.killpg(_os.getpgid(proc.pid), _sig.SIGKILL)
        except (OSError, AttributeError):
            proc.kill()
    try:
        shutil.rmtree(profile, ignore_errors=True)
    except Exception:
        pass
    return _alive() is False


def run(tree, view, label, widths, out_dir):
    """The capture. 0 on success, 1 when refused (no server, no chrome, a
    tiny/missing PNG, or a view that never finished loading). Refusals go to
    stderr and name the offending file. On success: prints each PNG path, then
    a TEXT excerpt under each path (the view's visible text, first 300 chars),
    then a ready line starting "BEFORE:" or "AFTER:". The server child is
    always stopped (try/finally)."""
    import os as _os
    import sys as _sys
    import time as _time

    tree = _os.path.abspath(tree)
    if not _os.path.isdir(tree):
        _sys.stderr.write("refused: tree %r is not a directory\n" % tree)
        return 1

    pop = None
    all_paths = [_make_out_path(out_dir, label, view, w) for w in widths]
    try:
        port = pick_free_port()
        pop = start_server(tree, port, out_dir)
        chrome = find_chrome()
        if not chrome:
            _finish_refusal(pop, port,
                            "no chrome on PATH (tried %s)"
                            % ", ".join(_CHROME_NAMES))
            return 1
        for width in widths:
            out_path = _make_out_path(out_dir, label, view, width)
            rc, excerpt, reason = _capture_view(chrome, port, view, out_path, width)
            if rc != 0 or not _os.path.exists(out_path):
                deleted, failed = _cleanup_pngs(all_paths, None)
                _finish_refusal(
                    pop, port,
                    "%s (deleted %d partial capture%s%s)"
                    % (reason, deleted,
                       "s" if deleted != 1 else "",
                       _cleanup_suffix(failed)))
                return 1
            size = _os.path.getsize(out_path)
            if size < MIN_PNG_BYTES:
                deleted, failed = _cleanup_pngs(all_paths, None)
                _finish_refusal(
                    pop, port,
                    "screenshot %s is only %d bytes (below %d); not a real "
                    "capture (deleted %d partial capture%s%s)"
                    % (out_path, size, MIN_PNG_BYTES, deleted,
                       "s" if deleted != 1 else "",
                       _cleanup_suffix(failed)))
                return 1
            _sys.stdout.write(out_path + "\n")
            _sys.stdout.write("TEXT %d: %s\n" % (width, excerpt or ""))
        tag = label.upper() + ":"
        _sys.stdout.write(tag + " " + " ".join(
            _make_out_path(out_dir, label, view, w) for w in widths) + "\n")
        return 0
    except Exception as exc:
        deleted, failed = _cleanup_pngs(all_paths, None)
        _finish_refusal(
            pop, port,
            "%s (deleted %d partial capture%s%s)"
            % (str(exc), deleted,
               "s" if deleted != 1 else "",
               _cleanup_suffix(failed)))
        return 1
    finally:
        global _start_child_home
        home = _start_child_home
        _start_child_home = None
        if pop is not None:
            _stop_server(pop)
        if home:
            shutil.rmtree(home, ignore_errors=True)
