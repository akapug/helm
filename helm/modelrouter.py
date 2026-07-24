#!/usr/bin/env python3
"""helm router — the transparent multi-model router (modelrouter.py).

WHY (verified on raw proxy conductor logs): mixed models in ONE claude-code
process work on stock CLIProxyAPI — a Task subagent's `.claude/agents/*.md`
frontmatter `model:` string goes to the wire PER-REQUEST and the proxy routes
each request by model name. The one remaining gap is a CLAUDE parent:
ANTHROPIC_BASE_URL is process-global (no per-subagent base-url exists in the
Task tool or agent frontmatter), so a Claude-parent + codex-subagent instance
needs ONE local endpoint that speaks to both worlds. This router is that
endpoint.

CANON:
  - `claude-*` model requests are forwarded VERBATIM to api.anthropic.com —
    claude-code's OWN OAuth Authorization header, body, and headers ride
    untouched; NO substitution, NO re-auth. The router is a localhost relay
    the bytes transit, nothing more.
  - NON-claude model requests are forwarded to the owning seat's CLIProxyAPI
    backend (seat.FAMILIES + the minted seat's token): the router swaps in the
    seat's proxy token and lets the proxy conduct from there. This COMPOSES
    CLIProxyAPI, it never rebuilds it (compose-dont-parallel).
  - Requests with no model (GETs, /v1/models, …) pass through to Anthropic:
    transparent unless provably non-claude.

HARD LAW: Claude is OAuth-ONLY — this module never reads, writes, injects, or
forwards an ANTHROPIC_API_KEY. The passthrough relays whatever auth claude-code
itself presents, verbatim, and nothing else.

CONDUCTOR LOG: every request appends one JSON line (ts/method/path/model/
route/status/ms) to the router log — the per-request wire evidence the proven
run-1 read off the proxy. Header values and bodies are NEVER logged: the log
must stay safe to paste.

Import-safe, stdlib-only.
"""
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from . import home

ROUTER_PORT_DEFAULT = 8320
ANTHROPIC_UPSTREAM_DEFAULT = "https://api.anthropic.com"

# Hop-by-hop + relay-mechanical headers: never copied across the seam in
# either direction (Host and Content-Length are recomputed by the relay).
_HOP = frozenset((
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "trailers", "transfer-encoding", "upgrade", "host",
    "content-length",
))
# The auth headers a PROXY-routed request must never leak upstream: the
# client's own credential is replaced by the seat's proxy token.
_AUTH = frozenset(("authorization", "x-api-key"))

_USAGE = """usage: helm router <verb> [args]
  up [--seat F] [--port N]     start the router in the background (default seat codex, port %d)
  run [--seat F] [--port N]    foreground serve (the process `up` spawns)
  down                         stop the background router
  status                       liveness + the routing table
  line [--port N]              the claude-PARENT multi-model launch line (OAuth passthrough)
  probes [--dir D]             mint the example probe agents (.claude/agents/*.md) into D
The router is the multi-model seam: claude-* -> api.anthropic.com VERBATIM
(claude-code's own OAuth, untouched); non-claude -> the seat's CLIProxyAPI.""" \
    % ROUTER_PORT_DEFAULT


def anthropic_upstream():
    """The claude-* passthrough origin. Overridable (HELM_ROUTER_ANTHROPIC_
    UPSTREAM) so hermetic tests point at a fake — live use never sets it."""
    return home.env("ROUTER_ANTHROPIC_UPSTREAM", ANTHROPIC_UPSTREAM_DEFAULT)


def router_dir():
    return os.path.join(home.global_dir(), "router")


def build_table():
    """The non-claude routing table, derived from seat state (never invented):
    every FAMILIES entry with a minted seat (a readable token) contributes
    {family: {port, token, models}} — models = the family's default + probe
    models, the exact-match keys that route a request to ITS proxy."""
    from . import seat
    table = {}
    for fam, info in seat.FAMILIES.items():
        token = seat._read_token(fam)
        if not token:
            continue
        models = {info["model"]} | set(info.get("probe_models") or ())
        table[fam] = {"port": info["port"], "token": token, "models": models}
    return table


class Router:
    """The routing decision + conductor log, shared by all handler threads."""

    def __init__(self, table, default_family=None, anthropic=None, log_path=None):
        self.table = table or {}
        self.default_family = default_family
        self.anthropic = anthropic or anthropic_upstream()
        self.log_path = log_path
        self._lock = threading.Lock()

    def decide(self, model):
        """('anthropic', None) or ('proxy', family). claude-* and modelless
        requests pass through verbatim; a non-claude model routes to the family
        that owns it exactly, else the default seat (whose proxy is the
        multi-backend conductor and may still know it)."""
        if not model or model.startswith("claude-"):
            return "anthropic", None
        for fam, entry in sorted(self.table.items()):
            if model in entry["models"]:
                return "proxy", fam
        if self.default_family and self.default_family in self.table:
            return "proxy", self.default_family
        return "anthropic", None

    def log(self, method, path, model, route, fam, status, ms):
        """One conductor line per request — model + route on the wire, NEVER
        header values or bodies (the log must stay paste-safe)."""
        if not self.log_path:
            return
        line = json.dumps({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "method": method, "path": path, "model": model,
            "route": route if fam is None else "%s:%s" % (route, fam),
            "status": status, "ms": int(ms * 1000),
        }, sort_keys=True)
        with self._lock:
            with open(self.log_path, "a") as f:
                f.write(line + "\n")


def logged_models(log_path):
    """The set of model names the conductor log saw on the wire — the smoke
    gate's verification surface (the proven run-1 pattern: trust the log,
    not the subagent's word)."""
    models = set()
    try:
        with open(log_path) as f:
            lines = f.read().splitlines()
    except OSError:
        return models
    for line in lines:
        try:
            m = json.loads(line).get("model")
        except ValueError:
            continue
        if m:
            models.add(m)
    return models


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 75          # idle keep-alive reap; clients reconnect transparently

    # the conductor log is the access log — stderr chatter helps nobody
    def log_message(self, fmt, *args):
        pass

    def _read_body(self):
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            chunks = []
            while True:
                size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    self.rfile.readline()          # trailing CRLF after 0-chunk
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline()              # CRLF after each chunk
            return b"".join(chunks)
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _upstream(self, route, fam):
        """(scheme, netloc) of the decided upstream."""
        r = self.server.router
        if route == "proxy":
            return "http", "127.0.0.1:%d" % r.table[fam]["port"]
        u = urlsplit(r.anthropic)
        return u.scheme, u.netloc

    def _headers_out(self, route, fam):
        """The forwarded header set. Passthrough = everything except the
        hop-by-hop/mechanical set — the client's own Authorization rides
        VERBATIM. Proxy = same, minus the client's auth headers, plus the
        seat's proxy token."""
        out = {}
        for k, v in self.headers.items():
            lk = k.lower()
            if lk in _HOP:
                continue
            if route == "proxy" and lk in _AUTH:
                continue
            out[k] = v
        if route == "proxy":
            out["Authorization"] = "Bearer " + self.server.router.table[fam]["token"]
        return out

    def _relay(self):
        r = self.server.router
        body = self._read_body()
        model = None
        if body:
            try:
                parsed = json.loads(body)
                if isinstance(parsed, dict):
                    model = parsed.get("model")
            except ValueError:
                pass
        route, fam = r.decide(model)
        t0 = time.time()
        scheme, netloc = self._upstream(route, fam)
        conn_cls = HTTPSConnection if scheme == "https" else HTTPConnection
        conn = conn_cls(netloc, timeout=600)
        status = None
        try:
            conn.request(self.command, self.path, body=body or None,
                         headers=self._headers_out(route, fam))
            resp = conn.getresponse()
            status = resp.status
            # log at response-head time (upstream verdict + latency): the line
            # exists the moment the route is proven, not after a long stream.
            r.log(self.command, self.path, model, route, fam, status,
                  time.time() - t0)
            self._send_back(resp)
        except OSError as exc:
            if status is None:       # upstream never answered — log once + 502
                r.log(self.command, self.path, model, route, fam, 502,
                      time.time() - t0)
                self._bail(str(exc))
        finally:
            conn.close()

    def _bail(self, detail):
        payload = json.dumps({"type": "error", "error": {
            "type": "helm_router_upstream_error", "message": detail}}).encode()
        try:
            self.send_response_only(502, "Bad Gateway")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except OSError:
            pass                       # client already gone — nothing to say

    def _send_back(self, resp):
        """Relay status + headers + body. A Content-Length body is copied
        exactly; a stream (SSE — no length) is re-chunked live via read1 so
        tokens reach the client as they arrive, never buffered to the end."""
        self.send_response_only(resp.status, resp.reason)
        cl = resp.getheader("Content-Length")
        for k, v in resp.getheaders():
            if k.lower() in _HOP:
                continue
            self.send_header(k, v)
        bodyless = self.command == "HEAD" or resp.status in (204, 304) \
            or 100 <= resp.status < 200
        if bodyless:
            self.end_headers()
            resp.read()
            return
        if cl is not None:
            self.send_header("Content-Length", cl)   # recomputed, exact copy
            self.end_headers()
            remaining = int(cl)
            while remaining > 0:
                chunk = resp.read1(min(65536, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
            self.wfile.flush()
            return
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        while True:
            chunk = resp.read1(65536)
            if not chunk:
                break
            self.wfile.write(b"%X\r\n" % len(chunk) + chunk + b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = _relay


def start_inprocess(table=None, default_family=None, log_path=None, port=0,
                    anthropic=None):
    """A live router on 127.0.0.1 in a daemon thread — the smoke gate's and the
    tests' entry. port=0 -> ephemeral (server_address[1] is the real port).
    Caller owns shutdown: srv.shutdown(); srv.server_close()."""
    srv = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    srv.daemon_threads = True
    srv.router = Router(table if table is not None else build_table(),
                        default_family, anthropic, log_path)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05},
                     daemon=True).start()
    return srv


# ---------------------------------------------------------------------------
# background lifecycle (pid + meta under _global/router/)
# ---------------------------------------------------------------------------

def _pid_path():
    return os.path.join(router_dir(), "router.pid")


def _meta_path():
    return os.path.join(router_dir(), "router.json")


def _running_pid():
    try:
        with open(_pid_path()) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def _port_open(port, timeout=0.5):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout):
            return True
    except OSError:
        return False


def _run(args):
    """Foreground serve — the process `up` spawns. SIGTERM shuts down clean."""
    port = int(args[args.index("--port") + 1]) if "--port" in args else ROUTER_PORT_DEFAULT
    fam = args[args.index("--seat") + 1] if "--seat" in args else "codex"
    d = router_dir()
    os.makedirs(d, exist_ok=True)
    table = build_table()
    srv = start_inprocess(table=table, default_family=fam,
                          log_path=os.path.join(d, "router.log"), port=port)
    print("helm router: up on 127.0.0.1:%d — claude-* -> %s (verbatim OAuth "
          "passthrough); non-claude -> %s" % (
              srv.server_address[1], anthropic_upstream(),
              ", ".join("%s:%d" % (f, e["port"]) for f, e in sorted(table.items()))
              or "(no seats minted)"))
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *a: stop.set())
    signal.signal(signal.SIGINT, lambda *a: stop.set())
    try:
        while not stop.wait(0.5):
            pass
    finally:
        srv.shutdown()
        srv.server_close()
    return 0


def _up(args):
    port = int(args[args.index("--port") + 1]) if "--port" in args else ROUTER_PORT_DEFAULT
    fam = args[args.index("--seat") + 1] if "--seat" in args else "codex"
    pid = _running_pid()
    if pid:
        print("helm router: already running (pid %d) — `helm router down` first"
              % pid, file=sys.stderr)
        return 1
    d = router_dir()
    os.makedirs(d, exist_ok=True)
    out = open(os.path.join(d, "router.out"), "ab")
    try:
        p = subprocess.Popen(
            [sys.executable, "-m", "helm", "router", "run",
             "--port", str(port), "--seat", fam],
            stdout=out, stderr=out, start_new_session=True)
    finally:
        out.close()
    with open(_pid_path(), "w") as f:
        f.write("%d\n" % p.pid)
    with open(_meta_path(), "w") as f:
        json.dump({"port": port, "seat": fam}, f)
    for _ in range(30):
        if p.poll() is not None or _port_open(port):
            break
        time.sleep(0.2)
    if p.poll() is not None:
        os.remove(_pid_path())
        print("helm router: exited rc %s — tail %s"
              % (p.returncode, os.path.join(d, "router.out")), file=sys.stderr)
        return 1
    print("helm router: up — 127.0.0.1:%d (pid %d, default seat %s)"
          % (port, p.pid, fam))
    return 0


def _down(args):
    pid = _running_pid()
    if not pid:
        for p in (_pid_path(),):
            if os.path.exists(p):
                os.remove(p)           # stale
        print("helm router: not running")
        return 0
    os.kill(pid, signal.SIGTERM)
    for _ in range(15):
        try:
            os.kill(pid, 0)
        except OSError:
            break
        time.sleep(0.2)
    os.remove(_pid_path())
    print("helm router: stopped (pid %d)" % pid)
    return 0


def _status(args):
    from . import pk
    pid = _running_pid()
    meta = pk.read_json(_meta_path(), {}) or {}
    port = meta.get("port", ROUTER_PORT_DEFAULT)
    if pid:
        print("helm router: UP pid %d port %s%s" % (
            pid, port, "" if _port_open(port) else " (port not answering!)"))
    else:
        print("helm router: down")
    table = build_table()
    print("  claude-*   -> %s (VERBATIM passthrough — client's own OAuth)"
          % anthropic_upstream())
    for fam, entry in sorted(table.items()):
        print("  %-10s -> 127.0.0.1:%d (%s)" % (
            "|".join(sorted(entry["models"])), entry["port"], fam))
    if not table:
        print("  (no non-claude seats minted — `helm seat add codex`)")
    return 0


def parent_line(port=None):
    """The claude-PARENT multi-model launch line. The parent is a normal
    Max-OAuth claude — so NO ANTHROPIC_AUTH_TOKEN (it would displace OAuth),
    NO ANTHROPIC_API_KEY (hard law, actively unset), NO
    CLAUDE_CODE_SUBAGENT_MODEL (it would blunt-pin every subagent over the
    per-agent frontmatter — the proven mechanism). Only ANTHROPIC_BASE_URL
    moves: onto the router, which relays claude-* verbatim."""
    port = port or ROUTER_PORT_DEFAULT
    return ("env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN "
            "-u CLAUDE_CODE_SUBAGENT_MODEL "
            "ANTHROPIC_BASE_URL=http://127.0.0.1:%d claude" % port)


def _line(args):
    port = int(args[args.index("--port") + 1]) if "--port" in args else None
    print(parent_line(port))
    print("# claude-* rides your own OAuth through the router untouched; "
          "subagent models come from .claude/agents/*.md frontmatter "
          "(`helm router probes` mints examples)", file=sys.stderr)
    return 0


def _probes(args):
    """Mint the example probe agents for every minted seat family into
    <dir>/.claude/agents (default cwd) — the per-agent `model:` frontmatter
    that IS the multi-model mechanism under a claude parent."""
    from . import seat
    base = args[args.index("--dir") + 1] if "--dir" in args else os.getcwd()
    ad = os.path.join(base, ".claude", "agents")
    minted = []
    for fam in sorted(build_table()):
        minted += seat._mint_probe_agents(os.path.join(base, ".claude"), fam)
    if not minted:
        print("helm router: no seats minted — `helm seat add codex` first",
              file=sys.stderr)
        return 1
    for name, model in minted:
        print("  %s -> %s" % (os.path.join(ad, name + ".md"), model))
    print("helm router: %d probe agent%s minted; spawn via Task subagent_type"
          % (len(minted), "s"[:len(minted) != 1]))
    return 0


def cmd_router(args):
    """router up|run|down|status|line|probes — the transparent multi-model
    router: claude-* passthrough (own OAuth, verbatim), non-claude -> seats."""
    args = list(args)
    if not args:
        print(_USAGE, file=sys.stderr)
        return 2
    verb, rest = args[0], args[1:]
    # (handler, plain flags, valued flags) — guard_tail refuses trailing junk
    # BEFORE the handler runs: `router down --bogus --help` used to STOP the
    # router and exit 0 as if --bogus existed.
    table = {"run": (_run, (), ("--port", "--seat")),
             "up": (_up, (), ("--port", "--seat")),
             "down": (_down, (), ()),
             "status": (_status, (), ()),
             "line": (_line, (), ("--port",)),
             "probes": (_probes, (), ("--dir",))}
    if verb not in table:
        print("helm router: unknown verb '%s'" % verb, file=sys.stderr)
        print(_USAGE, file=sys.stderr)
        return 2
    fn, flags, valued = table[verb]
    from .cli import guard_tail
    rc = guard_tail("helm router " + verb, rest, flags=flags, valued=valued,
                    usage=_USAGE)
    if rc is not None:
        return rc
    return fn(rest)
