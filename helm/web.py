#!/usr/bin/env python3
"""helm web — the web surface. CLI-first + web parity: every view here is a
projection of what the CLI already answers (registry / store / whoami /
configs / skills). The ONLY mutations that land from the browser are the
owner-requested skills verbs (toggle = reversible rename, delete = move to
trash — archive-not-delete, nothing is ever destroyed), census-validated and
localhost-only.

Laws: localhost-only bind (127.0.0.1, default port 7433), Python stdlib only,
one self-contained UI file (web_ui.html) served at /. The store and whoami
modules are built in parallel — their endpoints DEGRADE GRACEFULLY to
{"unavailable": true} when the module is missing or misbehaves.
"""
import json
import os
import sys
import urllib.parse
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
        per_root = store.counts()
        by_type = {}
        for row in per_root.values():
            if isinstance(row, dict):
                for t, n in row.items():
                    if isinstance(n, int):
                        by_type[t] = by_type.get(t, 0) + n
        out = {"counts": {"by_type": by_type, "total": sum(by_type.values())},
               "per_root": per_root}
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


SESSION_KEYS = ("h", "i", "t", "u", "mt", "project")
SESSION_CAP = 200


def _api_sessions():
    """Newest sessions across every harness, project-lensed; same degrade law."""
    try:
        from . import sessions
        rows = []
        for r in sessions.rows_for(limit=SESSION_CAP):
            row = {k: r.get(k) for k in SESSION_KEYS}
            row["cmd"] = sessions.resume_command(r)
            rows.append(row)
        return {"sessions": rows}
    except Exception:
        return {"unavailable": True}


def _api_configs():
    """The config-file list model: home/user scope + the project-scope cwd tree.
    Same graceful degrade as the store."""
    try:
        from . import configs
        out = {"homes": configs.homes_configs(), "tree": configs.tree()}
        json.dumps(out)
        return out
    except Exception:
        return {"unavailable": True}


def _api_configs_cascade(qs):
    """What a seat at ?cwd= loads — physics' resolved cascade. Optional
    ?harness=claude|codex (default claude) and ?home=DIR (default the
    harness's default home). Returns (obj, status)."""
    from . import configs
    harness = (qs.get("harness") or ["claude"])[0]
    if harness not in ("claude", "codex"):
        return {"error": "harness wants claude|codex, got %r" % harness}, 400
    cwd = (qs.get("cwd") or [None])[0]
    home_p = (qs.get("home") or [None])[0] or os.path.join(
        os.path.expanduser("~"), ".codex" if harness == "codex" else ".claude")
    return configs.resolve(home_p, cwd, harness), 200


# ── skills: census read + the owner-requested enable/disable/delete surface ──
# disable = rename <skill> -> <skill>.disabled (reversible); delete = MOVE to
# the trash dir (archive-not-delete law: nothing is ever destroyed).

TRASH_DIR = os.path.join(os.path.expanduser("~"), ".cache", "helm", "skills-trash")
DISABLED_SUFFIX = ".disabled"


def _api_skills():
    """The census, dupes-flagged, with the real dir path each mutation needs."""
    try:
        from . import skills
        found, bad = skills.census()
        name_dupes, _content_dupes = skills.dupes(found)
        rows = []
        for s in found:
            disabled = s["name"].endswith(DISABLED_SUFFIX)
            group = name_dupes.get(s["name"]) or []
            rows.append({
                "name": s["name"][:-len(DISABLED_SUFFIX)] if disabled else s["name"],
                "disabled": disabled, "home": s["home"], "real": s["real"],
                "path": os.path.join(s["home"], s["name"]),
                "has_manifest": s["has_manifest"],
                "shadowed": len(group) > 1,
                "diverged": len({x["hash"] for x in group}) > 1,
            })
        rows.sort(key=lambda r: (r["name"], r["home"]))
        return {"skills": rows, "bad": [{"path": p, "why": w} for p, w in bad]}
    except Exception:
        return {"unavailable": True}


def _valid_skill_dir(payload):
    """(abspath, None) iff payload['path'] is a REAL skill dir the census knows
    (inside a known skill home) — the gate for every mutation. Else (None, err)."""
    from . import skills
    p = payload.get("path")
    if not isinstance(p, str) or not p.strip():
        return None, ({"error": 'payload wants {"path": "<real skill dir>"}'}, 400)
    ap = os.path.abspath(os.path.expanduser(p))
    found, _bad = skills.census()
    known = {os.path.abspath(os.path.join(s["home"], s["name"])) for s in found}
    if ap not in known or not os.path.isdir(ap):
        return None, ({"error": "refused: not a known skill dir "
                                "(census-validated): %s" % ap}, 400)
    return ap, None


def _api_skills_toggle(payload):
    ap, err = _valid_skill_dir(payload)
    if err:
        return err
    if ap.endswith(DISABLED_SUFFIX):
        new, state = ap[:-len(DISABLED_SUFFIX)], "enabled"
    else:
        new, state = ap + DISABLED_SUFFIX, "disabled"
    if os.path.exists(new):
        return {"error": "refused: %s already exists" % new}, 409
    os.rename(ap, new)
    return {"ok": True, "path": ap, "new_path": new, "state": state}, 200


def _api_skills_delete(payload):
    ap, err = _valid_skill_dir(payload)
    if err:
        return err
    import shutil
    import time
    stamp = (time.strftime("%Y%m%dT%H%M%S", time.gmtime())
             + "-%09d" % (time.time_ns() % 1_000_000_000))
    dest = os.path.join(TRASH_DIR, "%s-%s" % (stamp, os.path.basename(ap)))
    os.makedirs(TRASH_DIR, exist_ok=True)
    shutil.move(ap, dest)
    return {"ok": True, "path": ap, "trash": dest}, 200


API = {
    "/api/registry": _api_registry,
    "/api/store": _api_store,
    "/api/whoami": _api_whoami,
    "/api/sessions": _api_sessions,
    "/api/configs": _api_configs,
    "/api/skills": _api_skills,
}

QUERY_API = {  # GET endpoints that take query params; fn(qs) -> (obj, status)
    "/api/configs/cascade": _api_configs_cascade,
}

POST_API = {  # fn(payload_dict) -> (obj, status)
    "/api/skills/toggle": _api_skills_toggle,
    "/api/skills/delete": _api_skills_delete,
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path != "/":
            path = path.rstrip("/")
        if path == "/":
            return self._ui()
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
        path = self.path.split("?", 1)[0].rstrip("/")
        fn = POST_API.get(path)
        if fn is None:
            return self._json({"error": "not found: %s" % path}, 404)
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
