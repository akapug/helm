"""HTTP server and CLI for :mod:`helm.web`."""
import sys

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
# EXPLICIT, NOT INHERITED — the rule web_core already states one name at a
# time, applied to the whole family. Each name below reached this module
# ONLY through the globals() splice under this block, so it was bound when
# web.py had already been imported and ABSENT on a direct `from helm import
# <this module>`: a NameError at the first call, or a NameError swallowed by
# a fail-open. Measured in web_common, whose code_drift answered "no drift"
# from an unbound `time` — the half-live detector silenced by an import
# order. The binding is identical either way (web.py imports the same module
# object, and the facade fanout rebinds it over this one), so naming it here
# costs nothing and removes the ordering dependency.
import json
import threading
import traceback
import urllib.parse
import webbrowser

_web = sys.modules.get(__package__ + ".web")
if _web is not None:
    globals().update({name: value for name, value in vars(_web).items()
                      if not (name.startswith("__") and name.endswith("__"))})
globals()["DEFAULT_PORT"] = 7433



class Handler(BaseHTTPRequestHandler):
    # A BOUND ON WAITING FOR THE CLIENT, AND ON NOTHING ELSE. socketserver
    # reads this in setup() and calls connection.settimeout with it, so it
    # bounds socket OPERATIONS -- reading the request line, reading the body,
    # writing the response -- and never the handler's own work. A request that
    # is slow because the server is COMPUTING never touches it, which is why a
    # value here cannot turn a slow answer into a broken one.
    #
    # WITHOUT IT, do_POST's `self.rfile.read(n)` takes n from the client's own
    # Content-Length and blocks forever. MEASURED: a client that announces 4096
    # bytes, sends 1, and holds the connection open gets no answer and pins its
    # handler thread (thread count 2 -> 3, no response in 5s), with a complete
    # body on the same route answering immediately as the control. A client
    # that CLOSES instead is fine -- the read sees EOF, returns short, and the
    # JSON parse answers 400 -- so this is the stalled-peer case: a dropped
    # network with no FIN, a suspended machine, a wedged proxy.
    #
    # THE VALUE IS DELIBERATELY LOOSE. The point is to bound an unbounded wait,
    # not to police latency; the loaded end-to-end MAX measured by the load
    # probe was 10.429s (960 of 960 OK, 24 threads x 40 requests on 32 cpus)
    # and that is server work, not socket waiting. The event stream writes a
    # keepalive every 5s, well inside this, and its loop already treats OSError
    # -- which socket.timeout is -- as the end of a stream.
    timeout = 30

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

    def _guarded(self, dispatch):
        """Run one request body so that NO exception can reach the socket.

        THE FAILURE THIS EXISTS FOR IS MEASURED, NOT IMAGINED. A handler that
        raises BEFORE it writes leaves the connection closed with no response,
        and a caller sees exactly `RemoteDisconnected: Remote end closed
        connection without response` -- reproduced 3 of 3 against this same
        stdlib shape (ThreadingHTTPServer + BaseHTTPRequestHandler,
        protocol_version HTTP/1.0) with a healthy 200 as the control in the
        same process. Three gate receipts carry that exact sentence, plus a
        `KeyError: lines` which is what a caller sees when the body is not the
        JSON it expected.

        THIS IS A LAST RESORT, NOT A REPLACEMENT. Every named guard around
        this one stays: the API calls, the UI read, the JSON serialisation.
        They produce BETTER errors because they know what failed. What none of
        them can do is catch a raise from the code BETWEEN them -- the origin
        check, the cockpit beat, the room lookup, or an exception type a
        narrow `except` does not name -- and it is precisely those that drop
        the connection on `/`, the one request the owner makes by opening the
        cockpit.

        A DROPPED CONNECTION AND A 500 ARE NOT THE SAME OUTCOME TO A BROWSER.
        The first is a network error the page cannot render and the poll loop
        treats as the server being gone; the second is a value the UI can
        show. Turning the first into the second is the whole point.

        THE RESPONSE ITSELF IS BEST-EFFORT AND SAYS SO. If the failure came
        after headers were already sent, writing another status would corrupt
        the stream, so this only reports when nothing has been written yet;
        otherwise the connection closes as before and the traceback still
        reaches stderr, which is where an unexpected failure belongs.
        """
        self._helm_wrote = False
        try:
            dispatch()
        except _CLIENT_GONE as e:
            # A READER WHO CLOSED THE TAB IS NOT A FAULT, and printing a stack
            # for one is the same defect `handle_error` was already cured of —
            # left standing in its sibling. It reaches here by two routes: a
            # hang-up during a normal response write, and a hang-up during the
            # 500 that a handler failure tries to send afterwards. Both are the
            # client's ordinary right to leave.
            #
            # ONE LINE, and it still says WHICH request, because a disconnect
            # that only ever happens on one route is a fact worth having.
            print("helm web: %s hung up during %s (%s)"
                  % (self.client_address[0] if self.client_address else "client",
                     self.path, e.__class__.__name__), file=sys.stderr)
            return
        except Exception:                # noqa: BLE001 — the socket is the point
            import traceback
            traceback.print_exc()
            if getattr(self, "_helm_wrote", False):
                return                   # headers are out; do not corrupt them
            try:
                self._json({"error": "the server failed before it answered; "
                                     "see the helm web log"}, 500)
            except _CLIENT_GONE:
                pass                     # they left while we were apologising
            except Exception:            # noqa: BLE001 — nothing left to try
                pass

    def send_response(self, *args, **kwargs):
        """Record that a status line has left this handler, then send it.

        THE FLAG IS DERIVED HERE BECAUSE THIS IS THE ONE PLACE EVERY STATUS
        LINE GOES THROUGH. Two paths write to this socket and only one of them
        is `_send`: the event stream sends its own 200 and then writes events
        for as long as it lives. A flag set at the call sites would be right
        for the path whose author remembered it and silently wrong for the
        other, and "nothing has been written yet" is exactly the answer the
        last-resort guard acts on -- a false one makes it write a second
        status line into the middle of a live stream, which is worse than the
        dropped connection it exists to replace.
        """
        self._helm_wrote = True
        return BaseHTTPRequestHandler.send_response(self, *args, **kwargs)

    def do_GET(self):
        return self._guarded(self._do_get)

    def do_POST(self):
        return self._guarded(self._do_post)

    def _do_get(self):
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
        except _CLIENT_GONE:
            raise                        # the reader left; `_guarded` says so in
                                         # one line. Writing a 500 to a closed
                                         # socket is what turned a disconnect
                                         # into a stack in the first place.
        except Exception as e:
            self._json({"error": "%s: %s" % (type(e).__name__, e)}, 500)

    def _do_post(self):
        if not self._same_origin():
            return self._json({"error": "forbidden (host/origin not loopback)"}, 403)
        path = self.path.split("?", 1)[0].rstrip("/")
        fn = POST_API.get(path)
        if fn is None:
            return self._json({"error": "not found: %s" % path}, 404)
        # mutations are NEVER open: browser CSRF can fire cross-origin POSTs at
        # 127.0.0.1, so every mutation demands the per-process bearer the UI
        # carries (the predecessor's _mut_authed, ported; helm answers 403).
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
            raw = self.rfile.read(n)
        except TimeoutError:
            # THE CLIENT STOPPED SENDING, AND NOTHING FAILED ON THIS SIDE. The
            # last-resort guard would catch this and answer 500, which would be
            # a lie about whose fault it is and would bury a stalled peer in
            # the same bucket as a real defect. Answering here keeps 500
            # meaning "the server broke".
            return self._json(
                {"error": "the request body did not arrive within %ss"
                          % self.timeout}, 408)
        try:
            payload = json.loads(raw or b"{}")
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
        client-gone (xrev, all E2E-reproduced): a RECONNECT with a
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
                    # death-aware predicate (round 4 P2): a stop's notify must
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
        # the predecessor's token hand-off, ported: UI source stays raw on disk; the
        # per-process mutation bearer is templated in at serve time.
        body = body.replace(b"__HELM_TOKEN__", MUTATION_TOKEN.encode())
        # the console's opening room is DERIVED (default_room), never a literal
        # baked into the JS — the UI cannot know which project it is serving.
        body = body.replace(b"__HELM_ROOM__", default_room().encode())
        # THE BUILD THIS PAGE IS, stamped into the page itself. Templated from
        # the bytes BEFORE the two substitutions above, so the id names the
        # build and not this process: a restart that changed no source hands
        # every open tab the same id and offers nobody a pointless reload.
        body = body.replace(b"__HELM_BUILD__", web_ui_loader.build_id().encode())
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



# What a CLIENT GOING AWAY looks like from inside a response write. Named once
# because the disconnect answer and the real-fault answer are decided by this
# set and by nothing else; a fourth spelling would silently move a fault into
# the quiet branch.
_CLIENT_GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


class _Server(ThreadingHTTPServer):
    """ThreadingHTTPServer that OWNS its own SSE watcher: state minted per
    instance, stopped by ITS OWN server_close — cross-server interference is
    unexpressible (the meld-converged per-server lifecycle). Idempotent:
    a double close just re-stops an already-stopped watcher."""

    #: THE KERNEL'S ACCEPT BACKLOG, and the reason the console sometimes does
    #: not answer AT ALL rather than answering slowly.
    #:
    #: socketserver's default is 5. This server's handlers are not uniformly
    #: fast — several hold the GIL for seconds while they walk /proc, the room
    #: directory or the dispatch ledger, and the owner's console polls many
    #: routes at once from a phone that opens its own connections per request.
    #: When more than five arrive while those handlers are busy, the kernel does
    #: not queue the sixth: it REFUSES it. The browser reports a connection
    #: error, which looks exactly like the server being down, so a page that was
    #: merely busy reads as a page that is broken — and the owner has no way to
    #: tell those apart from where he is standing.
    #:
    #: A deeper backlog does not make any handler faster and is not a substitute
    #: for fixing the slow ones. It changes a REFUSAL into a WAIT, which is the
    #: honest failure mode for a busy server and the one a browser renders as
    #: loading rather than as dead.
    request_queue_size = 128

    def handle_error(self, request, client_address):
        """A CLIENT THAT HUNG UP IS ONE LINE, NOT A STACK.

        MEASURED on the live server: 70 full tracebacks in ten minutes, every
        one of them `BrokenPipeError` out of `_send`'s `wfile.write` — the
        console's own polls, aborted by the browser when a card's fetch
        deadline passes or a tab closes mid-response. socketserver's default
        prints the whole traceback plus two rule lines per event, so the log
        the owner reads for real faults was almost entirely this, and the
        formatting itself is work the process does while it is already
        overloaded.

        It is a LINE and not silence. A disconnect is a real fact about the
        surface — a card that keeps aborting is a card that is too slow — and a
        log that says nothing cannot tell anyone that. Everything that is NOT a
        disconnect keeps the full traceback, because this is the only place a
        handler fault is reported at all.

        ONE DOOR RATHER THAN A CATCH AT EACH WRITE. `_send`, `send_response`
        and `end_headers` all write to the socket, and a catch added to one of
        them is a catch the next writer does not inherit; every escaping
        exception from every request thread passes through here."""
        exc = sys.exc_info()[1]
        if isinstance(exc, _CLIENT_GONE):
            sys.stderr.write(
                "[helm web] client disconnected mid-response (%s): %s\n"
                % (type(exc).__name__, client_address[0]
                   if isinstance(client_address, tuple) and client_address
                   else client_address))
            sys.stderr.flush()
            return
        super().handle_error(request, client_address)

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



def _prewarm_configs():
    """Walk the config tree ONCE in the background, so the owner's first open of
    the configs tab is served warm instead of paying for the walk itself.

    IT HANGS OFF THE SERVE PATH, NOT off make_server, and that placement IS the
    test gate: every suite in this tree builds its server with `make_server` and
    none of them reaches `cmd_web`, so no test ever walks the owner's real cwd
    root as a side effect of starting a server. A test that wants this thread
    calls this function, which is also the only way to join it.

    IT NEVER DELAYS STARTUP AND NEVER RAISES INTO THE SERVER. A daemon thread,
    started before serve_forever and never joined; its body swallows everything,
    because a cold config cache is one slow tab and an exception escaping here
    would be a console that did not come up at all. Even the spawn is guarded:
    a machine that cannot start one more thread must still serve.
    """
    def warm():
        from . import configs
        configs.tree()
    return _prewarm("helm-configs-prewarm", warm)


def _prewarm_board():
    """Read the Work page's board ONCE in the background, so the owner's first
    look after a restart finds its legs warm or warming instead of starting
    them. Measured: the first board read after a restart took 70s, the second
    one second. Same law as `_prewarm_configs`: off the serve path, a daemon
    thread nobody joins, and a failure is a slow first read, never a server
    that did not start."""
    def warm():
        from . import web_board
        web_board._api_board()
    return _prewarm("helm-board-prewarm", warm)


def _prewarm(name, warm):
    """Run `warm` on a daemon thread named `name`, swallowing what it raises;
    the thread, or None when even the spawn failed."""
    def run():
        try:
            warm()
        except Exception:            # noqa: BLE001 — a warm cache is a nicety
            pass

    t = threading.Thread(target=run, name=name, daemon=True)
    try:
        t.start()
    except BaseException:            # noqa: BLE001 — never block the serve
        return None
    return t


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
    _prewarm_configs()
    _prewarm_board()
    bound = srv.server_address[1]
    url = "http://%s:%d/" % (BIND, bound)
    print("helm web ⎈ %s  (Ctrl-C to stop)" % url)
    # SAY THAT THIS SERVER EXISTS. Without this, nothing in helm knows one is
    # running, so a server started against a lane worktree and left behind after
    # that lane's work ends is invisible to every instrument — it keeps polling
    # its own endpoints and only the load average can tell. Enough of them at
    # once will starve every seat on the host, including the owner's own turns.
    # Registering is best-effort BY DESIGN: a console that refused to start
    # because it could not write its own bookkeeping would trade the thing the
    # owner uses for the thing that describes it.
    from . import webserve
    webserve.register(bound, root=web_ui_loader.PACKAGE_DIR)
    if do_open:
        import webbrowser
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        # A KILLED SERVER NEVER REACHES THIS, which is why `webserve.live` asks
        # /proc instead of trusting the file's presence. This is the tidy path,
        # not the guarantee.
        webserve.forget(bound)
        srv.server_close()
    return 0



def _port(s):
    try:
        return int(s)
    except ValueError:
        return None
del _web
