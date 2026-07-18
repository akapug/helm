#!/usr/bin/env python3
"""helm web — the read-only web surface. CLI-first + web parity: every view
here is a projection of what the CLI already answers (registry / store /
whoami); no mutation lands from the browser yet.

Laws: localhost-only bind (127.0.0.1, default port 7433), Python stdlib only,
one self-contained UI file (web_ui.html) served at /. The store and whoami
modules are built in parallel — their endpoints DEGRADE GRACEFULLY to
{"unavailable": true} when the module is missing or misbehaves.
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import registry

BIND = "127.0.0.1"
DEFAULT_PORT = 7433
UI_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_ui.html")

# One store entry projects to these keys on the wire — the strip never needs bodies.
ENTRY_KEYS = ("id", "type", "confidence", "load_class", "scope")
ENTRY_CAP = 500


def _api_registry():
    return registry.load()


def _api_store():
    """Typed-store summary: counts + a bounded entry projection. The store
    module lands in a parallel lane — absent/raising -> unavailable, never 500."""
    try:
        from . import store
        out = {"counts": store.counts()}
        for name in ("entries", "list_entries", "all_entries", "scan"):
            fn = getattr(store, name, None)
            if not callable(fn):
                continue
            out["entries"] = [
                {k: e.get(k) for k in ENTRY_KEYS}
                for e in list(fn())[:ENTRY_CAP] if isinstance(e, dict)
            ]
            break
        json.dumps(out)  # unserializable shapes degrade too
        return out
    except Exception:
        return {"unavailable": True}


def _api_whoami():
    """Operator profile + notes summary; same graceful degrade as the store."""
    try:
        from . import whoami
        out = {}
        for key, names in (("profile", ("profile", "summary", "load_profile", "load")),
                           ("notes", ("notes", "list_notes", "load_notes"))):
            for name in names:
                fn = getattr(whoami, name, None)
                if not callable(fn):
                    continue
                v = fn()
                if v is not None:
                    out[key] = v
                break
        json.dumps(out)
        return out if out else {"unavailable": True}
    except Exception:
        return {"unavailable": True}


API = {
    "/api/registry": _api_registry,
    "/api/store": _api_store,
    "/api/whoami": _api_whoami,
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path != "/":
            path = path.rstrip("/")
        if path == "/":
            return self._ui()
        fn = API.get(path)
        if fn is None:
            return self._json({"error": "not found: %s" % path}, 404)
        try:
            self._json(fn())
        except Exception as e:
            self._json({"error": "%s: %s" % (type(e).__name__, e)}, 500)

    def _ui(self):
        try:
            with open(UI_PATH, "rb") as f:
                body = f.read()
        except OSError:
            return self._json({"error": "web_ui.html missing beside web.py"}, 500)
        self._send(body, "text/html; charset=utf-8")

    def _json(self, obj, status=200):
        self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", status)

    def _send(self, body, ctype, status=200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # a personal localhost tool; request noise helps no one


def make_server(port=DEFAULT_PORT):
    """Bound-but-not-serving ThreadingHTTPServer on 127.0.0.1. port=0 -> ephemeral
    (tests); the real port is server_address[1]."""
    srv = ThreadingHTTPServer((BIND, port), Handler)
    srv.daemon_threads = True
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


if __name__ == "__main__":
    sys.exit(cmd_web(sys.argv[1:]))
