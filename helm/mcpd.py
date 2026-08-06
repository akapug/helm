#!/usr/bin/env python3
"""The helm MCP endpoint — stateless spec rev 2026-07-28, stdlib only.

OWNER-RULED SHAPE (the two owner decision cards of 2026-08-05): the
HYBRID — this server is a SECOND FRONT-END over the same verb layer the CLI
fronts; the CLI remains the universal floor, the diff oracle, and the only
attention transport (beacon/wake stay Monitor + `helm chat wait` — an MCP
notification informs a client process and never wakes an idle model).
Signing for write verbs is server-side per-seat capability (the
owner's signing verdict), landing with chat_post in slice 1; slice 0 is the compliant shell plus
ONE read-only tool proving the path end to end.

WHY STATELESS MAKES THIS A PAGE AND NOT A SUBSYSTEM: the 2026-07-28 core
removed the initialize handshake, protocol sessions, the GET/SSE side
channel, and server-initiated requests. What remains for a compliant
minimal server is exactly what this file does: one localhost POST endpoint,
three mirrored-header validations, three RPCs, three error codes, and
Origin validation. Plain-JSON responses are always legal (clients MUST
accept both shapes), so no SSE is needed until something streams.

A SEAT IS A PER-REQUEST FACT, NEVER CONNECTION STATE: the bearer token on
each request resolves to a seat; `tools/list` MAY shape per seat later.
An absent or unknown token serves the anonymous read-only set — slice 0
has only read-only tools, so anonymous equals everything, and the refusal
plumbing still exists so slice 1's write verbs arrive deny-by-default.
"""
import base64
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROTOCOL_VERSION = "2026-07-28"
SUPPORTED_VERSIONS = (PROTOCOL_VERSION,)
DEFAULT_PORT = 7461
# list results must carry freshness (CacheableResult): short enough that a
# slice-1 tool addition propagates inside a minute, long enough to feed
# client prompt caches
LIST_TTL_MS = 60_000
# discover changes only when the server itself is rebuilt, and its payload is
# caller-independent, so it caches longer and publicly
DISCOVER_TTL_MS = 300_000

# JSON-RPC error codes fixed by the transport spec
E_PARSE = -32700
E_REQUEST = -32600     # parsed, but not one JSON-RPC request object
E_METHOD = -32601      # RESERVED for an unimplemented RPC METHOD (with 404)
E_INVALID = -32602     # invalid params — including an UNKNOWN TOOL NAME
E_HEADER_MISMATCH = -32020
E_UNSUPPORTED_VERSION = -32022

# `Mcp-Name` may legally arrive Base64-wrapped in this exact sentinel, and the
# spec makes DECODING BEFORE COMPARISON a MUST ("servers MUST decode an encoded
# Mcp-Name or Mcp-Param-{Name} value before comparing it to the corresponding
# request body value during Server Validation") — a raw string compare would
# refuse a conforming client the first time a tool name leaves the safe set.
_B64_SENTINEL = re.compile(r"^=\?base64\?(.*)\?=$")

_META_VERSION = "io.modelcontextprotocol/protocolVersion"

_SERVER_INFO = {"name": "helm", "version": "0.1"}
_INSTRUCTIONS = (
    "helm's verb layer over MCP. Tools mirror the `helm` CLI verbs exactly "
    "(the CLI is the diff oracle: same store, two front-ends, outputs "
    "agree). Read tools are anonymous; write tools arrive in slice 1 and "
    "require a per-seat bearer token. Nothing here wakes an idle seat — "
    "chat delivery and beacons stay on the CLI's Monitor loop.")


def _tool_store_resolve(arguments, seat):
    """store_resolve — the first tool, read-only, chosen because its CLI
    twin is the fleet's highest-value single-shot query (JIT canon lookup)
    and its output is pure text: the thinnest possible end-to-end proof."""
    # the SAME two functions the CLI verb composes (store/cli.py resolve):
    # one renderer, two fronts — the owner's differential-oracle ruling is
    # honored by construction and tested against drift in test_mcpd
    from .store.resolve import resolve_prompt
    # THE PRIVATE IMPORT IS DELIBERATE AND LOAD-BEARING (codex flagged it):
    # sharing the CLI's OWN renderer is the entire reason the two front-ends
    # can be pinned equal. A second formatter here would satisfy the module
    # boundary and REINTRODUCE the divergence this slice exists to remove —
    # the coupling IS the guarantee. If store ever publishes a render API
    # this moves to it unchanged; until then the test that pins both fronts
    # line-for-line is what keeps the coupling honest.
    from .store.cli import _fmt
    # PROJECT SCOPE IS A PER-REQUEST FACT, and leaving it out made the two
    # front-ends genuinely disagree: `helm store resolve` INFERS the project
    # from cwd on reads (store/cli.py:188-217) while this tool passed none and
    # got _global only — so a seat asking over MCP saw FEWER entries than the
    # same seat asking on the CLI, in the same directory. codex caught it; my
    # differential oracle did not, because its fixture had no project at all,
    # which is the oracle blind spot worth remembering: a test env with one
    # project cannot see a project bug.
    #
    # The server's OWN cwd is not the caller's, so the argument is explicit and
    # per-request (the stateless idiom — credentials and context are request
    # input, never connection state), defaulting to the server's inference so
    # the common case matches the CLI without anyone passing anything.
    # "Servers MUST: Validate all tool inputs" — the advertised inputSchema is
    # a CONTRACT: str()-coercing a non-string silently resolved a Python repr,
    # and additionalProperties:false is advertised, so an unknown key is a
    # client bug worth naming. Input-shape problems are the EXECUTION tier.
    if not isinstance(arguments, dict):
        return None, "arguments must be an object"
    extra = sorted(set(arguments) - {"text", "project"})
    if extra:
        return None, ("unknown argument(s) %s — this tool's schema declares "
                      "additionalProperties:false and takes only `text`"
                      % ", ".join(repr(k) for k in extra))
    raw = arguments.get("text")
    if raw is not None and not isinstance(raw, str):
        return None, "text must be a string, got %s" % type(raw).__name__
    text = (raw or "").strip()
    if not text:
        return None, "text is required and was empty"
    project = arguments.get("project")
    if project is not None and not isinstance(project, str):
        return None, "project must be a string when given"
    # NO CWD DEFAULT. The server's cwd is a guess about the CALLER, and a
    # guess that is right only while exactly one seat shares the daemon —
    # codex's point, and correct: two seats in different projects would get
    # one wrong answer each, silently. Absent scope means GLOBAL, which is
    # the honest reading of "no scope given", and a caller wanting CLI
    # parity passes the project it already knows. Per-request input, never
    # daemon state: the same rule the whole transport runs on.
    hits = resolve_prompt(text, project=project)
    rendered = "\n".join(_fmt(e) for e in hits)
    structured = {"fired": bool(hits), "hits": len(hits)}
    # the spec's backwards-compat SHOULD: a tool returning structuredContent
    # SHOULD also serialize it into a TextContent block. The FIRST block stays
    # the rendered lines — that is the differential-oracle payload the CLI
    # front emits verbatim — and the JSON rides second.
    return {"content": [{"type": "text",
                         "text": rendered or "(no entries fire)"},
                        {"type": "text", "text": json.dumps(structured)}],
            "structuredContent": structured}, None


# name -> (handler, inputSchema, description). A dict literal, deterministic
# order by construction (insertion order is the wire order — the spec wants
# stable ordering for client prompt caches).
TOOLS = {
    "store_resolve": (
        _tool_store_resolve,
        {"type": "object",
         "properties": {
             "text": {
                 "type": "string",
                 "description": "the sentence to resolve against the typed "
                                "store, in the asker's own words"},
             "project": {
                 "type": "string",
                 "description": "project scope. OMIT for global-only. The "
                                "CLI infers this from YOUR cwd on reads, so "
                                "pass your project to get the same answer "
                                "the CLI would give you"}},
         "required": ["text"],
         "additionalProperties": False},
        "Resolve a sentence against helm's typed knowledge store "
        "(premises/heuristics/lexicon) — the JIT canon lookup; read-only."),
}


def _tools_payload():
    tools = []
    for name, (_fn, schema, description) in TOOLS.items():
        tools.append({"name": name, "description": description,
                      "inputSchema": schema})
    return tools


def _result(rid, payload):
    payload.setdefault("resultType", "complete")
    meta = payload.setdefault("_meta", {})
    meta.setdefault("io.modelcontextprotocol/serverInfo", _SERVER_INFO)
    return {"jsonrpc": "2.0", "id": rid, "result": payload}


def _error(rid, code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": err}


def dispatch(body, seat=None):
    """One JSON-RPC request -> (http_status, response_object).

    Pure function of the request (plus the ledgers the tools read): the
    statelessness the spec promises is enforced here by construction —
    nothing above this call holds per-caller state, and the handler class
    below carries no instance fields between requests."""
    rid = body.get("id")
    method = body.get("method")
    # HOSTILE-SHAPE GUARDS FIRST. A body can be valid JSON and still be a
    # nonsense request: params as a string, _meta as a string, an unhashable
    # tool name. Each used to raise INSIDE the handler thread; socketserver
    # ate the traceback and closed the socket, so the client saw "closed
    # without response" — neither error tier, indistinguishable from a dead
    # legacy server. Malformed requests are PROTOCOL errors (-32602).
    params = body.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return 400, _error(rid, E_INVALID, "params must be an object")
    meta = params.get("_meta")
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        return 400, _error(rid, E_INVALID, "params._meta must be an object")
    version = meta.get(_META_VERSION)
    if version is not None and version not in SUPPORTED_VERSIONS:
        return 400, _error(rid, E_UNSUPPORTED_VERSION,
                           "unsupported protocol version %r" % version,
                           {"supported": list(SUPPORTED_VERSIONS),
                            "requested": version})
    if method == "server/discover":
        # caching hints are a MUST on every complete result — discover as
        # much as tools/list; discover is the more stable of the two
        return 200, _result(rid, {
            "supportedVersions": list(SUPPORTED_VERSIONS),
            "capabilities": {"tools": {}},
            "instructions": _INSTRUCTIONS,
            "ttlMs": DISCOVER_TTL_MS,
            "cacheScope": "public"})
    if method == "tools/list":
        return 200, _result(rid, {
            "tools": _tools_payload(),
            "ttlMs": LIST_TTL_MS,
            "cacheScope": "private"})
    if method == "tools/call":
        name = params.get("name")
        # UNKNOWN TOOL IS -32602, NOT 404/-32601: the spec's own example is
        # {"code": -32602, "message": "Unknown tool: invalid_tool_name"}, and
        # 404/-32601 is reserved for an unimplemented METHOD — a meaning that
        # doubles as the legacy-server fallback signal, so conflating the two
        # sends a modern client hunting for a 2024-era endpoint.
        entry = TOOLS.get(name) if isinstance(name, str) else None
        if entry is None:
            return 200, _error(rid, E_INVALID, "Unknown tool: %r" % (name,))
        fn, _schema, _desc = entry
        arguments = params.get("arguments") or {}
        try:
            payload, why = fn(arguments, seat)
        except Exception as exc:  # a tool bug is an EXECUTION error the
            # model can read and route around, never a protocol crash —
            # the spec's two-tier convention
            payload, why = None, "%s: %s" % (exc.__class__.__name__, exc)
        if why:
            return 200, _result(rid, {
                "content": [{"type": "text", "text": why}],
                "isError": True})
        return 200, _result(rid, payload)
    return 404, _error(rid, E_METHOD, "unknown method %r" % method)


class _Handler(BaseHTTPRequestHandler):
    server_version = "helm-mcpd/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # stderr logging is the operator's choice, not per-request spam

    def _refuse(self, status, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _gone(self):
        """GET and DELETE are the LEGACY verbs (standalone SSE stream, session
        termination). Both were removed in this revision, and the spec names
        405 for both so an old client learns the era rather than guessing."""
        raw = json.dumps(_error(
            None, E_METHOD,
            "%s is not part of the stateless transport; POST only"
            % self.command)).encode("utf-8")
        self.send_response(405)
        self.send_header("Allow", "POST")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = _gone
    do_DELETE = _gone

    def _accepted(self):
        """202 with NO body — the notification contract, verbatim: 'If the
        server accepts it, the server MUST return HTTP status code 202
        Accepted with no body.' Answering a notification with a result also
        violates JSON-RPC itself, which forbids replying to one at all."""
        self.send_response(202)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _header_value(self, name):
        """One header, Base64-sentinel decoded when it carries one."""
        got = self.headers.get(name)
        if got is None:
            return None
        m = _B64_SENTINEL.match(got)
        if not m:
            return got
        try:
            return base64.b64decode(m.group(1), validate=True).decode("utf-8")
        except Exception:
            return got      # undecodable stays raw and fails the comparison

    ENDPOINT = "/mcp"

    def do_POST(self):
        # ONE endpoint path, per the transport's own words ("The server MUST
        # provide a single HTTP endpoint path"). Serving every path made the
        # server answer /anything, which hides client misconfiguration and
        # widens the surface for no gain (codex).
        # EXACT match, not a normalized one. My first cut rstrip()ed the
        # trailing slash and then admitted "" — which let BOTH "/" and
        # "/mcp/" through the check written to stop exactly that (codex).
        # A fix that re-opens the hole it closes is worse than no fix, so
        # this compares the literal path and nothing else.
        if self.path.split("?", 1)[0] != self.ENDPOINT:
            self._refuse(404, _error(None, E_METHOD,
                                     "no MCP endpoint at %r; POST %s"
                                     % (self.path, self.ENDPOINT)))
            return
        origin = self.headers.get("Origin")
        if origin is not None and not re.match(
                r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$",
                origin):
            # DNS-rebinding defence is a MUST; absent Origin is a non-browser
            # client and legal
            self._refuse(403, _error(None, E_INVALID,
                                     "origin %r refused" % origin))
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, TypeError):
            self._refuse(400, _error(None, E_PARSE, "unparseable body"))
            return
        if not isinstance(body, dict):
            # PARSED FINE, wrong shape — -32600, not -32700. This revision
            # forbids batches ("The body of the HTTP POST MUST be a single
            # JSON-RPC request or notification"), so an array lands here.
            self._refuse(400, _error(None, E_REQUEST, "body must be ONE "
                                     "JSON-RPC request or notification "
                                     "object; batches are not part of this "
                                     "revision"))
            return
        if body.get("jsonrpc") != "2.0":
            self._refuse(400, _error(body.get("id"), E_REQUEST,
                                     "jsonrpc must be \"2.0\"; got %r"
                                     % (body.get("jsonrpc"),)))
            return
        rid = body.get("id")
        # A NOTIFICATION IS ANYTHING WITHOUT AN ID, answered with a bare 202 —
        # never a result. This revision defines no client-to-server
        # notifications over HTTP, so accept-and-drop is both compliant and
        # the whole behaviour. Header requirements are explicitly NOT defined
        # for notification POSTs, so the presence checks must not run here.
        if "id" not in body or rid is None:
            self._accepted()
            return
        # The mirrored headers let infrastructure route without parsing
        # bodies. They are REQUIRED, and Server Validation lists "a required
        # standard header is missing" as its FIRST failure condition — a
        # modern-only server MUST reject a header-less request rather than
        # serving it, because omitting the version header is how a
        # pre-2025-06-18 client speaks and this server supports no such era.
        params = body.get("params") if isinstance(
            body.get("params"), dict) else {}
        meta = params.get("_meta") if isinstance(
            params.get("_meta"), dict) else {}
        required = [("MCP-Protocol-Version", meta.get(_META_VERSION)),
                    ("Mcp-Method", body.get("method"))]
        if body.get("method") == "tools/call":
            required.append(("Mcp-Name", params.get("name")))
        for hdr, want in required:
            got = self._header_value(hdr)
            if got is None:
                self._refuse(400, _error(
                    rid, E_HEADER_MISMATCH,
                    "required header %s is missing" % hdr))
                return
            if want is not None and got != want:
                self._refuse(400, _error(
                    rid, E_HEADER_MISMATCH,
                    "%s header %r does not match the body's %r"
                    % (hdr, got, want)))
                return
        # "Every request MUST include the required _meta fields" — the
        # headers mirror the body, so a request carrying headers and NO body
        # metadata satisfied the mirror check while being non-conforming.
        if not meta.get(_META_VERSION):
            self._refuse(400, _error(
                rid, E_INVALID,
                "params._meta.%s is required on every request"
                % _META_VERSION))
            return
        hdr_version = self._header_value("MCP-Protocol-Version")
        if hdr_version not in SUPPORTED_VERSIONS:
            self._refuse(400, _error(
                rid, E_UNSUPPORTED_VERSION,
                "unsupported protocol version %r" % hdr_version,
                {"supported": list(SUPPORTED_VERSIONS),
                 "requested": hdr_version}))
            return
        seat = None  # slice 1: bearer token -> seat resolution + signing
        status, obj = dispatch(body, seat=seat)
        self._refuse(status, obj)


def serve(port=DEFAULT_PORT, host="127.0.0.1"):
    """Blocking serve loop; 127.0.0.1 by the spec's own SHOULD."""
    httpd = ThreadingHTTPServer((host, int(port)), _Handler)
    return httpd


def serve_background(port=DEFAULT_PORT, host="127.0.0.1"):
    """(httpd, thread) for tests and embedders; caller owns shutdown()."""
    httpd = serve(port=port, host=host)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, t


def cmd_mcpd(args):
    """helm mcpd serve [--port N]. A bind failure is an OPERATOR problem —
    the port is taken, or privileged — so it gets a sentence naming the port
    and the likely cause, never a traceback (codex)."""
    port = DEFAULT_PORT
    rest = list(args)
    if rest and rest[0] == "serve":
        rest = rest[1:]
    while rest:
        if rest[0] == "--port" and len(rest) > 1:
            try:
                port = int(rest[1])
            except ValueError:
                print("helm mcpd: --port wants an integer, got %r"
                      % rest[1], file=sys.stderr)
                return 2
            if not 1 <= port <= 65535:
                print("helm mcpd: --port %d is outside 1-65535" % port,
                      file=sys.stderr)
                return 2
            rest = rest[2:]
            continue
        print("usage: helm mcpd serve [--port N]")
        return 2
    try:
        httpd = serve(port=port)
    except OSError as exc:
        print("helm mcpd: cannot bind 127.0.0.1:%d — %s. Another server may "
              "already hold it (`ss -ltnp | grep %d`), or pass --port N."
              % (port, exc.strerror or exc, port), file=sys.stderr)
        return 1
    print("helm mcpd: stateless MCP (%s) on http://127.0.0.1:%d/mcp — "
          "%d tool(s), CLI remains the floor"
          % (PROTOCOL_VERSION, port, len(TOOLS)))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
    return 0
