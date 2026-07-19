#!/usr/bin/env python3
"""helm web — the web surface. CLI-first + web parity: every view here is a
projection of what the CLI already answers (registry / store / whoami /
configs / skills / homes / quota). The ONLY mutations that land from the
browser are the owner-requested skills verbs (toggle = reversible rename,
delete = move to trash — archive-not-delete, nothing is ever destroyed;
census-validated), the homes lifecycle verbs (prepare/verify/archive/
unarchive/migrate — directory moves only, archive-not-delete, live-agent
refusals; logins stay human-only), the session verbs (cwd re-home / prune —
metadata + new-copy only) and the configs editor (backup→validate→atomic,
recognized files only). All of it localhost-only, and every mutation demands
the per-process bearer token (MUTATION_TOKEN) — 403 without.

Laws: localhost-only bind (127.0.0.1, default port 7433), Python stdlib only,
one self-contained UI file (web_ui.html) served at /. The store and whoami
modules are built in parallel — their endpoints DEGRADE GRACEFULLY to
{"unavailable": true} when the module is missing or misbehaves.
"""
import json
import os
import secrets
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import registry

BIND = "127.0.0.1"
DEFAULT_PORT = 7433
UI_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_ui.html")

# Per-process anti-CSRF bearer (sesh's MUTATION_TOKEN, ported): a hostile page
# can fire cross-origin POSTs at 127.0.0.1 but can never READ our UI to learn
# the token, so EVERY POST demands it (403 without). It reaches the browser by
# template substitution — _ui() replaces __HELM_TOKEN__ when serving the page.
# HELM_API_TOKEN (legacy SESH_API_TOKEN) pins it; else fresh each process.
MUTATION_TOKEN = (os.environ.get("HELM_API_TOKEN")
                  or os.environ.get("SESH_API_TOKEN") or secrets.token_hex(16))

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


# ── configs editor surface (sesh /api/configs/* contracts, ported exactly) ──
# configs.py owns all behavior (recognition gate, backup→validate→atomic write,
# entry ops, restore); these handlers only adapt query/payload shapes.

def _api_configs_tree(qs):
    """The cwd tree of dirs holding project configs. ?root= narrows the scan;
    live session cwds are folded in (sesh: catalog rows' cwd)."""
    from . import configs
    cwds = []
    try:
        cwds = [r["cwd"] for r in _transcripts().get_catalog()["rows"] if r.get("cwd")]
    except Exception:
        pass  # no catalog on this machine — the scanned roots still answer
    return configs.tree(_q1(qs, "root") or None, extra_cwds=cwds), 200


def _api_configs_homes():
    from . import configs
    return configs.homes_configs()


def _api_configs_resolve(qs):
    """What a seat (home, cwd, harness) loads — the cascade with MCP
    winner/shadowed annotation. home= is a name or a path (sesh contract)."""
    from . import configs
    hp = _resolve_home_path(_q1(qs, "home") or "")
    if not hp:
        return {"error": "need home= (name or path, see /api/configs/homes)"}, 400
    harness = _q1(qs, "harness") or ("codex" if "codex" in hp else "claude")
    return configs.resolve(hp, _q1(qs, "cwd") or None, harness), 200


def _api_configs_file(qs):
    """One recognized config file's content (+editability). Refusals answer 200
    with an error field + empty content — the sesh contract the UI renders."""
    from . import configs
    p = _q1(qs, "path")
    if not p:
        return {"error": "need path="}, 400
    return configs.read_file(p), 200


def _api_configs_backups():
    from . import configs
    return configs.list_backups()


def _api_configs_file_post(payload):
    """Save one config file: backup → validate → atomic write (configs.py)."""
    from . import configs
    out = configs.write_file(payload.get("path") or "", payload.get("content") or "")
    return out, (400 if "error" in out else 200)


def _api_configs_entry_post(payload):
    """Structured entry op (add/remove an MCP server) — never hand-edits JSON."""
    from . import configs
    out = configs.entry_op(payload.get("action") or "", payload.get("path") or "",
                           payload.get("kind") or "", payload.get("name") or "",
                           payload.get("value"))
    return out, (400 if "error" in out else 200)


def _api_configs_restore_post(payload):
    """Restore a backup over its origin (validated + re-backed-up first)."""
    from . import configs
    out = configs.restore(payload.get("backup") or "")
    return out, (400 if "error" in out else 200)


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


# ── quota surface: creds / history / burn / allocate / status / homes ──
# ABSORBED from sesh (server/sesh.py route handlers, behavior-preserving):
# same JSON shapes, so the ported quota UI works unmodified. providers.py owns
# every fact about accounts/windows/history; these handlers only cache + join.

_qlock = threading.Lock()
_qstate = {}
_qinflight = {}
_PROVIDER = None  # lazy singleton; tests may inject a stub here


def _provider():
    global _PROVIDER
    if _PROVIDER is None:
        from . import providers
        _PROVIDER = providers.default_provider()
    return _PROVIDER


def _cached(key, ttl, fn):
    """Single-flight TTL cache (ported from sesh): concurrent misses on one key
    share one build instead of racing."""
    while True:
        with _qlock:
            ent = _qstate.get(key)
            if ent and time.time() - ent[0] < ttl:
                return ent[1]
            ev = _qinflight.get(key)
            if ev is None:
                _qinflight[key] = threading.Event()
                break
        ev.wait(timeout=300)
    try:
        val = fn()  # computed outside the lock; other keys stay readable
        with _qlock:
            _qstate[key] = (time.time(), val)
        return val
    finally:
        with _qlock:
            _qinflight.pop(key).set()


def _catalog_rows():
    def build():
        from . import catalog
        rows, _stats = catalog.build()
        return rows
    return _cached("catalog", 600, build)


def _claude_home_identity(home_p):
    """oauthAccount email from a home's .claude.json — identity METADATA, never tokens."""
    try:
        with open(os.path.join(home_p, ".claude.json")) as f:
            d = json.load(f)
        return (d.get("oauthAccount") or {}).get("emailAddress")
    except Exception:
        return None


def _norm(s):
    return "".join(ch if ch.isalnum() else "-" for ch in (s or "").lower()).strip("-")


def get_creds(refresh=False):
    if refresh:
        with _qlock:
            _qstate.pop("creds", None)

    def build():
        import glob
        from .providers import ProviderError
        HOME = os.path.expanduser("~")
        prov = _provider()
        try:
            accounts = prov.accounts()
            states = {s.get("account"): s for s in prov.cred_state()}
            windows = {w.get("account"): w for w in prov.windows()}
        except ProviderError:
            return []  # no quota provider on this machine — sessions/resume still work
        merged = []
        for a in accounts:
            if a.get("provider") not in ("anthropic", "codex"):
                continue
            s = states.get(a["name"], {})
            w = windows.get(a["name"], {})
            home_p = a.get("home") or ""
            real = os.path.realpath(home_p) if home_p else ""
            home_name = os.path.basename(home_p) if home_p else None
            identity = _claude_home_identity(real) if a["provider"] == "anthropic" and real else None
            name_lies = bool(identity) and _norm(identity) not in (_norm(home_name), _norm(os.path.basename(real)))
            merged.append({
                "name": a["name"], "provider": a["provider"], "home": home_p,
                "home_name": home_name, "identity": identity, "name_lies": name_lies,
                "active": a.get("active", False), "tier": s.get("tier") or a.get("tier"),
                "headroom": s.get("headroom_pct"), "state": s.get("cred_state", "unknown"),
                "status": s.get("status"), "resets_at_ms": s.get("resets_at_ms"),
                "windows_left": w.get("windows_left"), "windows_per_week": w.get("windows_per_week"),
                "windows_verdict": w.get("verdict"),
            })
        seen = {}
        for h in glob.glob(f"{HOME}/.claude-homes/*/"):
            real = os.path.realpath(h)
            ident = _claude_home_identity(real)
            if ident and os.path.exists(os.path.join(real, ".credentials.json")):
                seen.setdefault(ident, set()).add(real)
        dups = {i: sorted(os.path.basename(p) for p in ps) for i, ps in seen.items() if len(ps) > 1}
        for c in merged:
            if c["provider"] == "anthropic" and c["name"] in dups:
                c["duplicate_homes"] = dups[c["name"]]
        return merged
    return _cached("creds", 60, build)


def get_history(hours=168):
    def build():
        from .providers import ProviderError
        try:
            rows = _provider().history(hours)
        except ProviderError:
            return []
        # downsample to ~240 buckets per account — charts don't need minute-level points
        bucket_s = max(60, (hours * 3600) // 240)
        latest = {}
        for r in rows:  # newest sample per (account, bucket), independent of provider order
            try:
                ts = time.mktime(time.strptime(r["probed_at"][:19], "%Y-%m-%dT%H:%M:%S"))
            except (ValueError, KeyError):
                continue
            k = (r.get("account"), int(ts // bucket_s))
            if k not in latest or r["probed_at"] > latest[k]["probed_at"]:
                latest[k] = r
        return sorted(latest.values(), key=lambda r: r["probed_at"])  # oldest-first, guaranteed
    return _cached(f"history:{hours}", 120, build)


def _probe_epoch(s):
    """True epoch seconds for a provider probed_at (RFC3339 UTC '...Z'; a naive
    local string still parses). Burn buckets join against session mtimes (real
    epochs), so a tz-shifted parse would attribute burn to the wrong hour."""
    import calendar
    try:
        t = time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
    except (TypeError, ValueError):
        return None
    return calendar.timegm(t) if "Z" in s[19:] or "+00:00" in s[19:] else time.mktime(t)


def get_burn(hours=48):
    """The join only this surface can make: this burn spike ↔ that session. v1 is
    TEMPORAL — per account, the per-hour drop in remaining% of the binding '5h'
    gauge (floor 0: resets are not burn), joined to the sessions last-active in
    each hour. A session listed under a bucket was ACTIVE then, not proven to be
    the burner."""
    hours = max(1, min(168, hours))

    def build():
        hist = get_history(hours)
        if not hist:
            return {"buckets": [],
                    "note": "no quota provider — burn attribution needs burn history"}
        now = time.time()
        t_lo = now - hours * 3600
        # remaining% of the '5h' gauge per account, oldest-first (get_history guarantees order)
        samples = {}
        for r in hist:
            g = next((g for g in r.get("gauges", [])
                      if g.get("label") == "5h" and g.get("utilization") is not None), None)
            if g is None:
                continue
            ts = _probe_epoch(r.get("probed_at") or "")
            if ts is None:
                continue
            samples.setdefault(r.get("account"), []).append(
                (ts, (1 - min(1, g["utilization"])) * 100))
        burned = {}  # bucket epoch sec -> {account: pct burned}
        for acct, pts in samples.items():
            pts.sort()
            for (_, r0), (t1, r1) in zip(pts, pts[1:]):
                drop = r0 - r1
                if drop <= 0 or t1 < t_lo:  # floor 0: a rising gauge is a reset, not burn
                    continue
                b = int(t1 // 3600) * 3600
                acc = burned.setdefault(b, {})
                acc[acct] = acc.get(acct, 0) + drop
        by_bucket = {}
        for r in _catalog_rows():
            mt = r.get("mt") or 0
            if mt < t_lo:
                continue
            by_bucket.setdefault(int(mt // 3600) * 3600, []).append(r)
        buckets = []
        for b in sorted(set(burned) | set(by_bucket)):
            sess = sorted(by_bucket.get(b, []), key=lambda r: r["z"], reverse=True)[:8]
            buckets.append({
                "t": b * 1000,
                "byAccount": {a: round(p, 1) for a, p in sorted(burned.get(b, {}).items())
                              if p >= 0.05},
                "sessions": [{"i": s["i"], "t": s["t"][:60], "c": s["c"], "h": s["h"]}
                             for s in sess],
            })
        return {"buckets": buckets,
                "note": "temporal join v1: burn = per-hour drop of each account's binding 5h "
                        "gauge; sessions = last-active that hour, capped at the 8 largest "
                        "per bucket — coincidence in time, not proven causation"}
    return _cached(f"burn:{hours}", 120, build)


def _alloc_models():
    return (os.environ.get("HELM_ALLOC_MODELS")
            or os.environ.get("SESH_ALLOC_MODELS") or "fable,opus,gpt-5.5").split(",")


def get_allocations():
    def build():
        from .providers import ProviderError
        out = {}
        for m in _alloc_models():
            m = m.strip()
            if not m:
                continue
            try:
                ranked = _provider().allocate(m)
            except ProviderError:
                ranked = []
            pick = next((r for r in ranked if r.get("eligible")), None)
            out[m] = {"pick": pick, "ranked": ranked[:5]}
        return out
    return _cached("alloc", 120, build)


def _q1(qs, key, default=None):
    """First value of a parse_qs list, else default."""
    v = qs.get(key)
    return v[0] if v else default


def _api_creds(qs):
    try:
        return get_creds(refresh=_q1(qs, "refresh") == "1"), 200
    except Exception:
        return {"unavailable": True}, 200


def _api_history(qs):
    try:
        return get_history(hours=int(_q1(qs, "hours", "168"))), 200
    except ValueError:
        return {"error": "hours wants an integer"}, 400
    except Exception:
        return {"unavailable": True}, 200


def _api_burn(qs):
    try:
        return get_burn(hours=int(_q1(qs, "hours", "48"))), 200
    except ValueError:
        return {"error": "hours wants an integer"}, 400
    except Exception:
        return {"unavailable": True}, 200


def _api_allocate():
    try:
        return get_allocations()
    except Exception:
        return {"unavailable": True}


def _api_quota_status():
    """Same shape as sesh /api/status: is a provider present, how many accounts,
    does cv exist, how big is the cached catalog."""
    import shutil
    try:
        qcli = type(_provider()).__name__.replace("QuotaProvider", "").lower()
    except Exception:
        qcli = "none"
    creds_n = 0
    try:
        creds_n = len(get_creds())
    except Exception:
        pass
    with _qlock:
        cat = _qstate.get("catalog")
    return {
        "provider": {"configured": qcli,
                     "present": qcli == "native" or bool(shutil.which(
                         os.environ.get("HELM_QUOTA_CLI")
                         or os.environ.get("SESH_QUOTA_CLI") or "tokaware")),
                     "accounts": creds_n,
                     "degraded": creds_n == 0,
                     "fallback": "(default) account resume works without a provider"},
        "cv": bool(shutil.which("cv")),
        "catalogRows": len(cat[1]) if cat else None,
        "mutations": "POST JSON + Authorization: Bearer (per-process token, "
                     "templated into the UI)",
    }


# ── credential homes: the quota view's homes card (homes.py backend) ──

def _api_homes():
    """Every credential home (live, broken-alias, archived) — sesh /api/homes shape."""
    try:
        from . import homes
        return homes.homes_list()
    except Exception:
        return {"unavailable": True}


def _api_homes_post(payload):
    """Home lifecycle: helm prepares/verifies/moves DIRECTORIES only — the human
    runs every login; token contents are never touched (CRED_AUTH_CANON)."""
    from . import homes
    act = payload.get("action")
    if act == "create":
        out = homes.home_create(payload.get("provider"), payload.get("email"))
    elif act == "verify":
        out = homes.home_verify(payload.get("name"), payload.get("provider") or None)
    elif act == "archive":
        out = homes.home_archive(payload.get("name"), payload.get("provider") or None)
    elif act == "unarchive":
        out = homes.home_unarchive(payload.get("name"))
    elif act == "migrate":
        out = homes.home_migrate(payload.get("name"), payload.get("provider") or None)
    else:
        out = {"error": "unknown action %r "
                        "(create | verify | archive | unarchive | migrate)" % (act,)}
    return out, (400 if "error" in out else 200)


def _resolve_home_path(name):
    """Home NAME or path → absolute home path (claude-homes, codex-homes, defaults)."""
    if not name:
        return None
    if name.startswith("/") or name.startswith("~"):
        p = os.path.expanduser(name)
        return p if os.path.isdir(p) else None
    HOME = os.path.expanduser("~")
    if name == "(default-claude)":
        return os.path.join(HOME, ".claude")
    if name == "(default-codex)":
        return os.path.join(HOME, ".codex")
    for root in (os.path.join(HOME, ".claude-homes"), os.path.join(HOME, ".codex-homes")):
        p = os.path.join(root, name)
        if os.path.isdir(p):
            return p
    return None


def _api_physics(qs):
    """What a seat on this home would load — the homes card's physics button."""
    from . import physics
    hp = _resolve_home_path(_q1(qs, "home") or "")
    if not hp:
        return {"error": "need home= (name or path)"}, 400
    harness = _q1(qs, "harness") or (
        "codex" if "/codex" in hp or "codex-homes" in hp else "claude")
    return physics.physics_report(hp, _q1(qs, "cwd") or os.path.expanduser("~"),
                                  harness), 200


def _api_physics_diff(qs):
    """What differs between two homes' physics (cred axis when cwd is omitted)."""
    from . import physics
    a = _resolve_home_path(_q1(qs, "a") or "")
    b = _resolve_home_path(_q1(qs, "b") or "")
    if not a or not b:
        return {"error": "need a= and b= (home names or paths)"}, 400
    return physics.physics_diff(a, b, _q1(qs, "harness") or "claude",
                                cwd=_q1(qs, "cwd") or None), 200


# ── sessions surface: catalog / search / session / cmd / cwd / prune ──
# ABSORBED contracts from sesh (server/sesh.py route handlers): same query
# params + response shapes, thin wrappers over transcripts.py (which owns the
# behavior: single-flight caches, cv seams, overrides). Same degrade law: an
# unexpected failure answers {"unavailable": true}, never a 500.

def _transcripts():
    from . import transcripts
    return transcripts


def _catalog_opensession(rows):
    """Catalog rows with OpenSession-aligned metadata names (cwd is metadata,
    never identity) — sesh /api/catalog?format=opensession, ported."""
    return [{
        "harness": r["h"], "id": r["i"], "cwd": r.get("cwd") or r["c"], "title": r["t"],
        "git": {"branch": r["b"]} if r["b"] else {},
        "createdAt": r["cr"], "updatedAt": r["u"],
        "messageCount": r["m"], "sizeBytes": r["z"], "path": r["p"],
    } for r in rows]


def _api_catalog(qs):
    try:
        cat = _transcripts().get_catalog(refresh=_q1(qs, "refresh") == "1")
        if _q1(qs, "format") == "opensession":
            return {"sessions": _catalog_opensession(cat["rows"]),
                    "stats": cat["stats"], "scanned_at": cat["scanned_at"]}, 200
        return cat, 200
    except Exception:
        return {"unavailable": True}, 200


def _api_search(qs):
    q = _q1(qs, "q")
    if not q:
        return {"error": "need q="}, 400
    try:
        limit = int(_q1(qs, "limit", "40"))
    except ValueError:
        return {"error": "limit wants an integer"}, 400
    try:
        return _transcripts().deep_search(
            q, limit=limit, scope=_q1(qs, "scope") or None,
            include_synthetic=_q1(qs, "synthetic") == "1"), 200
    except Exception:
        return {"unavailable": True}, 200


def _api_session(qs):
    sid = _q1(qs, "sid")
    if not sid:
        return {"error": "need sid="}, 400
    try:
        before = int(_q1(qs, "before")) if _q1(qs, "before") else None
        limit = int(_q1(qs, "limit", "60"))
    except ValueError:
        return {"error": "before/limit want integers"}, 400
    try:
        return _transcripts().get_session(
            sid, before=before, limit=limit,
            find=_q1(qs, "find") or None,
            harness=_q1(qs, "harness") or None), 200
    except Exception:
        return {"unavailable": True}, 200


def _api_cmd(qs):
    sid = _q1(qs, "sid")
    if not sid:
        return {"error": "need sid="}, 400
    acct = _q1(qs, "account")
    if not acct:
        # sesh's degraded-account handling: with no provider (or none reporting)
        # an omitted account is well-defined — the machine's default account.
        try:
            degraded = not get_creds()
        except Exception:
            degraded = True
        if degraded:
            acct = "(default)"
        else:
            return {"error": "need account= (accounts exist — pick one, see /api/creds)"}, 400
    try:
        return _transcripts().make_cmd(acct, sid, _q1(qs, "model") or None), 200
    except Exception:
        return {"unavailable": True}, 200


def _api_cwd_post(payload):
    """Re-home a session's cwd (set) or --reset it (cwd: null). Metadata, never
    identity; claude additionally gets a project-slug symlink so --resume resolves."""
    out = _transcripts().cwd_override(payload)
    return out, (400 if "error" in out else 200)


def _api_prune_post(payload):
    """Resume studio: derive a NEW smaller still-resumable COPY via cv prune;
    the original is never mutated. dry: true previews without running."""
    out = _transcripts().prune_session(
        payload.get("sid") or "", preset=payload.get("preset", "lean"),
        dry=bool(payload.get("dry")), tokens=payload.get("tokens"))
    return out, (400 if "error" in out else 200)


API = {
    "/api/registry": _api_registry,
    "/api/store": _api_store,
    "/api/whoami": _api_whoami,
    "/api/sessions": _api_sessions,
    "/api/configs": _api_configs,
    "/api/skills": _api_skills,
    "/api/status": _api_quota_status,
    "/api/allocate": _api_allocate,
    "/api/homes": _api_homes,
    "/api/configs/homes": _api_configs_homes,
    "/api/configs/backups": _api_configs_backups,
}

QUERY_API = {  # GET endpoints that take query params; fn(qs) -> (obj, status)
    "/api/configs/cascade": _api_configs_cascade,
    "/api/configs/tree": _api_configs_tree,
    "/api/configs/resolve": _api_configs_resolve,
    "/api/configs/file": _api_configs_file,
    "/api/creds": _api_creds,
    "/api/history": _api_history,
    "/api/burn": _api_burn,
    "/api/physics": _api_physics,
    "/api/physics-diff": _api_physics_diff,
    "/api/catalog": _api_catalog,
    "/api/search": _api_search,
    "/api/session": _api_session,
    "/api/cmd": _api_cmd,
}

POST_API = {  # fn(payload_dict) -> (obj, status); ALL demand the mutation token
    "/api/skills/toggle": _api_skills_toggle,
    "/api/skills/delete": _api_skills_delete,
    "/api/homes": _api_homes_post,
    "/api/cwd": _api_cwd_post,
    "/api/prune": _api_prune_post,
    "/api/configs/file": _api_configs_file_post,
    "/api/configs/entry": _api_configs_entry_post,
    "/api/configs/restore": _api_configs_restore_post,
}


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
        if not self._same_origin():
            return self._json({"error": "forbidden (host/origin not loopback)"}, 403)
        path = self.path.split("?", 1)[0].rstrip("/")
        fn = POST_API.get(path)
        if fn is None:
            return self._json({"error": "not found: %s" % path}, 404)
        # mutations are NEVER open: browser CSRF can fire cross-origin POSTs at
        # 127.0.0.1, so every mutation demands the per-process bearer the UI
        # carries (sesh's _mut_authed, ported; helm answers 403).
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

    def _ui(self):
        try:
            with open(UI_PATH, "rb") as f:
                body = f.read()
        except OSError:
            return self._json({"error": "web_ui.html missing beside web.py"}, 500)
        # the sesh token hand-off, ported: the UI file stays raw on disk; the
        # per-process mutation bearer is templated in at serve time.
        body = body.replace(b"__HELM_TOKEN__", MUTATION_TOKEN.encode())
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
