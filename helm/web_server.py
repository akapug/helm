"""HTTP server and CLI for :mod:`helm.web`."""
import sys

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_web = sys.modules.get(__package__ + ".web")
if _web is not None:
    globals().update({name: value for name, value in vars(_web).items()
                      if not (name.startswith("__") and name.endswith("__"))})
globals()["DEFAULT_PORT"] = 7433



class Handler(BaseHTTPRequestHandler):
    def _same_origin(self):
        """DNS-rebinding defense: a bound-to-127.0.0.1 server still answers
        requests a hostile page re-resolves to us, and our GETs leak data +
        the templated token. Pin Host to the loopback literals we bind, and
        reject any cross-origin request outright. This holds regardless of the
        bearer token (which a rebound same-origin page could otherwise read
        off the served page)."""
        port = self.server.server_address[1]
        ok_hosts = {"127.0.0.1:%d" % port, "localhost:%d" % port,
                    "[::1]:%d" % port}
        host = self.headers.get("Host", "")
        if host not in ok_hosts:
            return False
        origin = self.headers.get("Origin") or self.headers.get("Referer")
        if origin:
            from urllib.parse import urlparse
            if urlparse(origin).netloc not in ok_hosts:
                return False
        return True

    def do_GET(self):
        if not self._same_origin():
            return self._json({"error": "forbidden (host/origin not loopback)"}, 403)
        path, _, query = self.path.partition("?")
        if path != "/":
            path = path.rstrip("/")
        if path == "/":
            _cockpit_beat()      # a cockpit just OPENED — the owner is present
            return self._ui()
        if path == "/api/events":
            return self._sse()
        qfn = QUERY_API.get(path)
        if qfn is not None:
            try:
                obj, status = qfn(urllib.parse.parse_qs(query))
            except Exception as e:
                return self._json({"error": "%s: %s" % (type(e).__name__, e)}, 500)
            return self._json(obj, status)
        fn = API.get(path)
        if fn is None:
            return self._json({"error": "not found: %s" % path}, 404)
        try:
            self._json(fn())
        except Exception as e:
            self._json({"error": "%s: %s" % (type(e).__name__, e)}, 500)

    def do_POST(self):
        if not self._same_origin():
            return self._json({"error": "forbidden (host/origin not loopback)"}, 403)
        path = self.path.split("?", 1)[0].rstrip("/")
        fn = POST_API.get(path)
        if fn is None:
            return self._json({"error": "not found: %s" % path}, 404)
        # mutations are NEVER open: browser CSRF can fire cross-origin POSTs at
        # 127.0.0.1, so every mutation demands the per-process bearer the UI
        # carries (the predecessor cockpit's mutation-auth check, ported;
        # helm answers 403).
        if self.headers.get("Authorization", "") != "Bearer " + MUTATION_TOKEN:
            return self._json({"error": "forbidden (mutations always require "
                                        "the bearer token the UI carries)"}, 403)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n > 65536:
            return self._json({"error": "body too large"}, 400)
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._json({"error": "body is not valid JSON"}, 400)
        if not isinstance(payload, dict):
            return self._json({"error": "body wants a JSON object"}, 400)
        try:
            obj, status = fn(payload)
        except Exception as e:
            return self._json({"error": "%s: %s" % (type(e).__name__, e)}, 500)
        self._json(obj, status)

    def _sse(self):
        """text/event-stream: block on the watcher's Condition; emit a `chat`
        doorbell per state change (id = the watcher seq) and a keepalive
        comment on 5s of quiet (the short tick doubles as the health/teardown
        cadence). The client's EventSource reconnects itself; a vanished
        client just raises into the quiet except below. Three exits beyond
        client-gone (cross-family review, all E2E-reproduced): a RECONNECT with a
        stale Last-Event-ID gets an IMMEDIATE catch-up doorbell (events are
        contentless, so one ring replays any gap — the cursor read carries
        the payload); a DEAD WATCHER ends the stream (keepalives from a
        watcherless server would pin ES_LIVE and wedge every client on the
        stretched poll); a CLOSED SERVER socket ends it (streams must not
        outlive server_close)."""
        if not _sse_ensure_watcher(self.server):
            # no watcher could arm — honest 503, the client's ES errors and
            # its 2s poll fallback carries the UI (never a doorbell-less
            # stream that LOOKS live)
            return self._json({"error": "sse watcher unavailable"}, 503)
        sse = self.server._sse
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.end_headers()
        # a stream is never keep-alive-reusable: when this handler returns
        # (dead watcher / server close / client gone) the SOCKET must close so
        # the client's EventSource sees the end — without this the base
        # handler's keep-alive loop just waits for a next request and the
        # "ended" stream looks alive forever (caught by the wire test). Set
        # AFTER the headers: send_header("Connection", "keep-alive") silently
        # RESETS close_connection to False inside the base handler, which is
        # why that header is gone (HTTP/1.1 keeps the connection by default;
        # the stream stays open exactly as long as this loop runs).
        self.close_connection = True
        with sse["cond"]:
            seq = sse["seq"]
        # Last-Event-ID = the reconnect cursor EventSource sends by itself:
        # a mismatch means doorbells rang while this client was away
        catch_up = False
        last_id = self.headers.get("Last-Event-ID")
        if last_id is not None:
            try:
                catch_up = int(last_id) != seq
            except ValueError:
                catch_up = True
        with sse["cond"]:
            sse["connected"] += 1
            sse["cond"].notify_all()
        try:
            self.wfile.write(b": helm sse doorbell\n\n")
            if catch_up:
                self.wfile.write(
                    ("event: chat\nid: %d\ndata: {}\n\n" % seq).encode())
            self.wfile.flush()
            while True:
                with sse["cond"]:
                    # death-aware predicate: a stop's notify must
                    # RELEASE this wait — a seq-only predicate re-slept it and
                    # streams lingered to the full timeout
                    sse["cond"].wait_for(
                        lambda: sse["seq"] != seq or _sse_watcher_dead(sse),
                        timeout=5.0)
                    fired = sse["seq"] != seq
                    seq = sse["seq"]
                    dead = _sse_watcher_dead(sse)
                if dead:
                    return   # end the stream -> client ES errors -> 2s polls
                try:
                    if self.server.socket.fileno() == -1:
                        return   # server_close ran — do not outlive it
                except (OSError, AttributeError):
                    return
                self.wfile.write(
                    ("event: chat\nid: %d\ndata: {}\n\n" % seq).encode()
                    if fired else b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return   # the client went away — the normal end of a stream
        finally:
            with sse["cond"]:
                sse["connected"] -= 1
                sse["cond"].notify_all()

    def _ui(self):
        try:
            body = web_ui_loader.read_bytes()
        except (OSError, ValueError) as e:
            return self._json({"error": "web UI assembly failed: %s" % e}, 500)
        # the predecessor's token hand-off, ported: UI source stays raw on
        # disk; the per-process mutation bearer is templated in at serve time.
        body = body.replace(b"__HELM_TOKEN__", MUTATION_TOKEN.encode())
        # the console's opening room is DERIVED (default_room), never a literal
        # baked into the JS — the UI cannot know which project it is serving.
        body = body.replace(b"__HELM_ROOM__", default_room().encode())
        self._send(body, "text/html; charset=utf-8", no_cache=True)

    def _json(self, obj, status=200):
        try:
            body = json.dumps(obj, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError):
            body = b'{"error":"response contains a non-JSON value"}'
            status = 500
        self._send(body, "application/json; charset=utf-8", status)

    def _send(self, body, ctype, status=200, no_cache=False):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if no_cache:
            # the UI is assembled fresh from disk each request (the token is
            # templated in per-process); a browser cache would hand the owner a
            # STALE page after an upgrade — "where are my channels?" — so the
            # HTML shell must always re-fetch.
            self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # a personal localhost tool; request noise helps no one



class _Server(ThreadingHTTPServer):
    """ThreadingHTTPServer that OWNS its own SSE watcher: state minted per
    instance, stopped by ITS OWN server_close — cross-server interference is
    unexpressible (the meld-converged per-server lifecycle). Idempotent:
    a double close just re-stops an already-stopped watcher."""
    def server_close(self):
        if getattr(self, "_sse", None) is not None:
            _sse_stop(self)
        super().server_close()



def make_server(port=DEFAULT_PORT):
    """Bound-but-not-serving ThreadingHTTPServer on 127.0.0.1. port=0 -> ephemeral
    (tests); the real port is server_address[1]."""
    srv = _Server((BIND, port), Handler)
    srv.daemon_threads = True
    srv._sse = _sse_state()
    return srv



def cmd_web(args):
    """web [--port N] [--open] — serve the read-only web surface on localhost."""
    port = DEFAULT_PORT
    do_open = False
    args = list(args or [])
    while args:
        a = args.pop(0)
        if a == "--open":
            do_open = True
        elif a == "--port" and args:
            port = _port(args.pop(0))
        elif a.startswith("--port="):
            port = _port(a.split("=", 1)[1])
        else:
            print("usage: helm web [--port N] [--open]", file=sys.stderr)
            return 2
        if port is None:
            print("helm web: --port wants an integer", file=sys.stderr)
            return 2
    try:
        srv = make_server(port)
    except OSError as e:
        print("helm web: cannot bind %s:%d (%s)" % (BIND, port, e.strerror or e),
              file=sys.stderr)
        return 1
    url = "http://%s:%d/" % (BIND, srv.server_address[1])
    print("helm web ⎈ %s  (Ctrl-C to stop)" % url)
    if do_open:
        import webbrowser
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        srv.server_close()
    return 0



def _port(s):
    try:
        return int(s)
    except ValueError:
        return None
del _web
