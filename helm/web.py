#!/usr/bin/env python3
"""helm web — the web surface. CLI-first + web parity: every view here is a
projection of what the CLI already answers (registry / store / whoami /
configs / skills / homes / quota). The ONLY mutations that land from the
browser are the owner-requested skills verbs (toggle = reversible rename,
delete = move to trash — archive-not-delete, nothing is ever destroyed;
census-validated), the homes lifecycle verbs (prepare/verify/archive/
unarchive/migrate — directory moves only, archive-not-delete, live-agent
refusals; logins stay human-only), the session verbs (cwd re-home / prune —
metadata + new-copy only), the configs editor (backup→validate→atomic,
recognized files only) and the chat post (an append to the RAM room — the
owner's side of the groupchat). All of it localhost-only, and every mutation
demands the per-process bearer token (MUTATION_TOKEN) — 403 without.

Laws: localhost-only bind (127.0.0.1, default port 7433), Python stdlib only,
one self-contained UI file (web_ui.html) served at /. The store and whoami
modules are built in parallel — their endpoints DEGRADE GRACEFULLY to
{"unavailable": true} when the module is missing or misbehaves.
"""
import json
import os
import re
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
REVIEW_STMT_CAP = 400  # the review panel shows the statement head, not the body


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


def _api_store_review():
    """The owner store-review queue: candidate (fires NOTHING) + provisional
    (xrev-cleared, fires WITH a [provisional] tag) entries — the browse/approve/
    reject surface near the configs view (owner canon: the technical auto-learns
    may go provisionally live once a cross-family review clears them, and the
    owner still gets a way to ratify/reject in the WEB UI). Same graceful degrade
    as the store: absent/raising -> unavailable, never 500."""
    try:
        from . import store
        rows = []
        for e in store.reviewable():
            rows.append({
                "id": str(e.get("id") or ""),
                "type": e.get("type") or "",
                "status": e.get("status") or "",
                "statement": (e.get("statement") or "")[:REVIEW_STMT_CAP],
                "source": e.get("source") or "",       # captured-by (inferred/asked/explicit)
                "captured_ts": str(e.get("stated_ts") or e.get("updated_ts") or ""),
                "scope": e.get("scope") or "",
                "confidence": e.get("confidence"),
                "xrev_by": e.get("xrev_by") or "",     # who attested the /x review
                "xrev_ts": e.get("xrev_ts") or "",
            })
        counts = {"candidate": 0, "provisional": 0}
        for r in rows:
            if r["status"] in counts:
                counts[r["status"]] += 1
        out = {"entries": rows, "counts": counts}
        json.dumps(out)  # unserializable shapes degrade too
        return out
    except Exception:
        return {"unavailable": True}


def _api_store_confirm(payload):
    """Owner ratify: candidate/provisional -> live, through the SAME store.confirm
    the CLI calls (one writer path, atomic-write rails inside the store). id
    required; optional statement (an inline --edit) + type (disambiguate a slug
    shared across reviewable types)."""
    from . import pk, store
    eid = str(payload.get("id") or "").strip()
    if not eid:
        return {"error": "id is required"}, 400
    e, err = store.confirm(eid, pk.now_ts(),
                           new_statement=(payload.get("statement") or None),
                           project=(payload.get("project") or None),
                           ctype=(payload.get("type") or None))
    if err:
        return {"error": err}, 400
    return {"ok": True, "id": str(e["id"]), "type": e["type"],
            "status": e["status"]}, 200


def _api_store_reject(payload):
    """Owner reject: candidate/provisional -> retired IN PLACE (the record law:
    the file stays), through the SAME store.reject the CLI calls. id required;
    optional reason (the reject reason box) + type."""
    from . import pk, store
    eid = str(payload.get("id") or "").strip()
    if not eid:
        return {"error": "id is required"}, 400
    e, err = store.reject(eid, pk.now_ts(),
                          why=str(payload.get("reason") or "").strip(),
                          project=(payload.get("project") or None),
                          ctype=(payload.get("type") or None))
    if err:
        return {"error": err}, 400
    return {"ok": True, "id": str(e["id"]), "type": e["type"],
            "status": e["status"], "retired_why": e.get("retired_why") or ""}, 200


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
    out = configs.tree(_q1(qs, "root") or None, extra_cwds=cwds)
    return out, (400 if out.get("error") else 200)


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
    """Save one config file against the revision the editor actually opened."""
    from . import configs
    if not isinstance(payload.get("revision"), str):
        return {"error": "revision is required; reload before saving", "code": "revision"}, 400
    out = configs.write_file(payload.get("path") or "", payload.get("content") or "",
                             expected_revision=payload["revision"])
    return out, (409 if out.get("code") == "conflict" else 400 if "error" in out else 200)


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
    """The catalog through the ONE acquisition path — transcripts.get_catalog()'s
    single-flight cache (cwd-overrides applied, 5-min TTL). This once ran its own
    second single-flight over catalog.build() directly: a duplicate cache with a
    different TTL that skipped the cwd-override pass, so the burn view could show
    a session under a stale cwd the rest of the UI had already re-homed. One
    cache, one lens, no divergence."""
    return _transcripts().get_catalog()["rows"]


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


# ── chat: the human-included groupchat (chat.py owns rooms + markers) ──
# The web panel is the OWNER's surface (gui-first-owner law); agents live on
# `helm chat`. GET is an open read (loopback + same-origin only, like every
# GET); POST rides the mutation bearer and drops the owner-unread marker so
# the shipped reflex surfaces the message to every local agent next turn.
#
# The AGENT→OWNER direction (chat.mark_owner_unread is owner→agent only):
# every poll also carries the owner-facing unread signal — rows past the
# owner's last-read cursor, and the subset that @-mention an owner name
# (seats.owner_names(), the delivery filter's owner rule). The cursor is a
# web.py-owned marker in the room dir (<room>.owner-read); the chat view
# advances it via POST /api/chat/read when the owner has actually SEEN the
# room. Fail-open total: any surprise answers zeros — no badge, never an
# error (a GUI-first owner on another tab must never lose the page to this).

def _owner_read_path(room):
    from . import chat, pk
    return os.path.join(chat.chat_dir(), pk.slug(room) + ".owner-read")


def _owner_cursor(room, rows):
    """The owner's last-read position against the CURRENT rows. The stored
    row id wins (rotation-proof — ids are chat._append's stable per-row law);
    else the stored count while it still fits; else 0 (over-notify briefly,
    self-heals on the next read-ack)."""
    from . import pk
    st = pk.read_json(_owner_read_path(room), None)
    if not isinstance(st, dict):
        return 0
    rid = st.get("rid")
    if rid:
        for i in range(len(rows) - 1, -1, -1):
            if rows[i].get("id") == rid:
                return i + 1
    n = st.get("n")
    return n if isinstance(n, int) and 0 <= n <= len(rows) else 0


def _owner_signal(room, rows):
    """{owner_read, owner_unread, owner_mentions [, owner_mention_last,
    owner_mention_preview]} — what landed past the owner's cursor and how
    much of it addresses HIM. Mention matching mirrors seats.deliverable's
    owner rule: seats.owner_names() as the name set, the same @-boundary
    regex. The owner rails' own posts (origin web/tui, owner name) never
    badge the owner; reactions never badge (noise law). owner_mention_last
    is the newest unseen mention's identity — the client's notify-dedup key
    (once per NEW mention decision, not per poll)."""
    try:
        from . import seats
        names = seats.owner_names()
        cur = _owner_cursor(room, rows)
        out = {"owner_read": cur, "owner_unread": 0, "owner_mentions": 0}
        rx = re.compile(r"(?<![A-Za-z0-9._-])@(?:%s)(?![A-Za-z0-9._-])"
                        % "|".join(sorted(map(re.escape, names))),
                        re.I) if names else None
        last = None
        for m in rows[cur:]:
            text = m.get("text")
            if not isinstance(text, str) or not text or m.get("react"):
                continue
            if (m.get("origin") in seats.OWNER_RAILS
                    and str(m.get("from") or "").lower() in names):
                continue
            out["owner_unread"] += 1
            if rx and rx.search(text):
                out["owner_mentions"] += 1
                last = m
        if last is not None:
            # _dsan the identity: a foreign/pre-fix row's from-field is not
            # covered by the join seam and rides this owner-polled JSON raw.
            from . import chat
            out["owner_mention_last"] = "%s|%s" % (last.get("ts") or "",
                                                   chat._dsan(last.get("from") or ""))
            out["owner_mention_preview"] = "%s: %s" % (
                chat._dsan(last.get("from") or "?"), (last.get("text") or "")[:120])
        return out
    except Exception:
        return {"owner_read": 0, "owner_unread": 0, "owner_mentions": 0}


def _room_seats(room, rows, roster):
    """The sidebar's per-channel roster: which seats are present in THIS
    room, newest-activity first — [{seat, presence}]. 'Present' = the seat
    POSTED here recently (the rows are already read for the owner signal —
    reuse, no extra I/O) OR actually CONSUMED rows here (its room cursor has
    `active=true`; a bare EOF join baseline is NOT presence, or every seat
    would otherwise look present everywhere). Presence is the shared .seen
    beat (seats.last_seen /
    presence_of), so a fresh poster shows 'fresh', an idle one 'quiet';
    owner-rail rows are skipped (the owner is not a seat). Fail-open per
    row; capped so a busy channel can't flood the sidebar.

    Each row also carries `last_seen` (the raw beat, epoch seconds): the
    sidebar renders it as an AGE ('3m'), because a coloured dot with no legend
    and no clock cannot answer the owner's only question — is this seat working
    right now, or has it been quiet for an hour? (owner UX pass 2026-07-21)"""
    from . import seats as _s
    seen, out = set(), []

    def _row(seat):
        # the seat KEY rides the sidebar JSON raw — launder the emitted label
        # (the raw key still indexes roster[]/last_seen above) so a hostile
        # HELM_CHAT_NAME cannot spoof the channel roster or a non-browser reader.
        ls = _s.last_seen(seat, roster[seat])
        return {"seat": _s._seat_label(seat),
                "presence": _s.presence_of(ls), "last_seen": ls}

    for m in reversed(rows[-64:]):           # recent activity, newest first
        frm = str(m.get("from") or "")
        if not frm or m.get("react") or frm in seen or frm not in roster \
                or not _s.room_in_scope(room, roster[frm]):
            continue
        seen.add(frm)
        out.append(_row(frm))
        if len(out) >= 6:
            return out
    for seat in sorted(roster):              # consumers who never posted
        if len(out) >= 6:
            break
        if seat in seen or not _s.room_in_scope(room, roster[seat]):
            continue
        if not _s.room_active(room, seat):  # bare EOF baselines are not presence
            continue
        out.append(_row(seat))
    return out


def _rooms_summary(roster=None):
    """The channel list for the web sidebar: one light row per room —
    {room, total, last (ts), owner_unread, owner_mentions, seats}. Folding the
    per-room owner signal here is what lets the nav badge SUM every channel,
    so a post in a NON-main room is never invisible to the owner (the real
    single-room hole). seats = the per-channel roster (_room_seats) so the
    sidebar shows who is present/active in each room, not just the room name.
    A handful of small tmpfs reads; fail-open per room. `roster` is passed in
    when the caller already read it (the poll needs the same names for the
    composer's @mention completion — one read serves both)."""
    from . import chat, seats as _s
    if roster is None:
        try:
            roster = _s.roster()
        except Exception:
            roster = {}
    out = []
    for room in chat.list_rooms():
        try:
            rows, total = chat.read(room)
            sig = _owner_signal(room, rows)
            out.append({"room": room, "total": total,
                        "last": (rows[-1].get("ts") if rows else None),
                        "owner_unread": sig.get("owner_unread", 0),
                        "owner_mentions": sig.get("owner_mentions", 0),
                        "seats": _room_seats(room, rows, roster)})
        except Exception:
            continue
    return out


def _api_chat(qs):
    """Poll read: rows after ?since= (count already seen) + the new total +
    the transport truth (signed/unsigned + chain head — the panel's tick and
    strip) + the owner-unread signal (_owner_signal — the nav badge on EVERY
    tab) + `rooms` (the channel sidebar list, cross-room unread for the summed
    badge) + `roster` (the live seat names — the composer's @mention completion
    source, so a mention the owner types is an EXACT token seats.deliverable
    will actually match). The panel polls this every ~2s; since past the end
    resets. Rows include reaction rows AND reply rows ({reply_to, rts, rfrom});
    the client aggregates both."""
    try:
        since = int(_q1(qs, "since", "0"))
    except ValueError:
        return {"error": "since wants an integer"}, 400
    try:
        from . import chat, seats as _s
        room = _q1(qs, "room", "main")
        try:
            roster = _s.roster()
        except Exception:
            roster = {}
        rows, total = chat.read(room)   # one read serves the slice AND the signal
        try:
            # the fleet-wide presence bar (dot + one status line per seat) —
            # rides the poll the panel already runs; light (no cursor scans)
            presence = _s.presence_report()
        except Exception:
            presence = []
        # `roster` feeds the composer's @mention list — the seat KEYS ride
        # the JSON wire raw (ensure_ascii=False), so launder each label so a
        # hostile HELM_CHAT_NAME (ESC/bidi) cannot reach a non-browser consumer
        # or spoof the dropdown. The panel still matches on the exact stored
        # key when the owner sends; only this published copy is laundered.
        # the rows carry raw NAME fields (from/tfrom/rfrom/dm) — a hostile one
        # can only exist OUTSIDE the validated join seam (home.chat_name), but
        # launder the EMITTED copy so no such name reaches a non-browser reader
        # of the /api/chat JSON (chat.public_rows; the stored rows stay raw for
        # reaction/reply matching, mirroring the roster's _pub_row owner).
        out = {"room": room,
               "lines": chat.public_rows(
                   rows[since if 0 <= since <= total else 0:]),
               "total": total, "transport": chat.transport_status(),
               "rooms": _rooms_summary(roster),
               "roster": sorted(_s._seat_label(s) for s in roster),
               "presence": presence}
        out.update(_owner_signal(room, rows))
        return out, 200
    except Exception:
        return {"unavailable": True}, 200


def _api_chat_read_post(payload):
    """The owner's read-ack: the chat view is open and visible, everything
    rendered — advance the owner-read cursor to the room's end. The mirror of
    chat.mark_owner_unread's direction, owned HERE (chat.py stays the agents'
    module). Count + newest row id, so rotation cannot strand it."""
    from . import chat, pk
    room = str(payload.get("room") or "main")
    rows, total = chat.read(room)
    pk.write_json(_owner_read_path(room),
                  {"n": total, "rid": rows[-1].get("id") if rows else None})
    return {"ok": True, "room": room, "owner_read": total}, 200


def _chat_profile():
    """Server-side signing identity for the owner's web posts: the server's
    HELM_CELL_PROFILE (the PRD's contract), else the owner's cell `david` —
    never the agent default (the web panel IS the owner surface)."""
    return os.environ.get("HELM_CELL_PROFILE") \
        or os.environ.get("MELD_AGENT_PROFILE") or "david"


def _api_chat_post(payload):
    """The owner's post: append (signed server-side when the room node
    answers) + mark owner-unread. name defaults to david.

    Optional `reply_to` = the parent row's id (the panel's reply button holds
    it): the row threads under that parent and, when signed, its digest BINDS
    it. Delivery: the reply also WAKES the parent's author (mention-tier,
    casefold, via the stamped rfrom — inverted 2026-07-22, replying replaces
    typing the @mention); otherwise it wakes what its text alone would."""
    from . import chat
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        return {"error": 'payload wants {"text": "..."} (non-empty)'}, 400
    room = str(payload.get("room") or "main")
    msg = chat.post(text.strip(), room, who=str(payload.get("name") or "david"),
                    profile=_chat_profile(), origin="web",
                    reply_to=str(payload.get("reply_to") or "") or None)
    chat.mark_owner_unread(room)
    return {"ok": True, "msg": chat.public_rows([msg])[0],
            "total": chat.read(room)[1]}, 200


def _api_chat_dm(payload):
    """The owner's TRUE 1:1 (the ledger 'message a seat' card routes single-
    seat sends here): one private recipient's lane, never a room post — the
    old path posted '@seat …' into #main and called it a DM (owner-flagged).
    Exact-token addressee (seats.dm — premise exact-token-addressee-match);
    signed like a post; the recipient's beacon surfaces it."""
    from . import chat, seats
    text = str(payload.get("text") or "").strip()
    if not text:
        return {"error": "empty text"}, 400
    row, err = seats.dm(str(payload.get("to") or ""), text,
                        who=str(payload.get("name") or "david"),
                        profile=_chat_profile(), origin="web")
    if err:
        return {"error": err}, 400
    return {"ok": True, "msg": chat.public_rows([row])[0]}, 200


def _seat_ephemeral(s):
    """An EPHEMERAL review-subagent: auto-named `agent-<hex>`, no home room, a
    /tmp cwd — a transient fan-out SA (tonight's fable/ds4pro/opus review SAs),
    not a conversational seat you would ever message. Tagged so the live
    'message a seat' picker can hide it while it stays QUERYABLE elsewhere
    (owner steer 2026-07-23: declutter the picker, do not delete). Fail-safe:
    any surprise reads False (a real seat is never mis-hidden)."""
    try:
        name = str(s.get("seat") or "")
        rest = name[6:] if name.startswith("agent-") else ""
        auto = bool(rest) and all(c in "0123456789abcdef" for c in rest)
        return auto and not s.get("home_room") and "/tmp" in str(s.get("cwd") or "")
    except Exception:
        return False


def _api_chat_roster(qs):
    """The seats panel's read: roster presence + per-seat pending deliveries
    + live claims (seats.py — the meld-half's M3 parity surface). Read-only,
    fail-open: any surprise answers empty, never a 500. Each seat is tagged
    `ephemeral` so the live picker can hide done review-SAs (kept queryable)."""
    try:
        from . import seats
        rep = seats.roster_report(_q1(qs, "room", "main"))
        for s in rep.get("seats", []):
            s["ephemeral"] = _seat_ephemeral(s)
        return rep, 200
    except Exception:
        return {"seats": [], "claims": [], "unavailable": True}, 200


def _api_todos(qs):
    """The owner's fleet-todo read: every seat and the task it is on right
    now (todos.py — the pull half of the TodoWrite/Task* mirror). Read-only,
    fail-open: any surprise answers empty, never a 500."""
    try:
        from . import todos
        return todos.fleet(), 200
    except Exception:
        return {"seats": [], "orphans": [], "orphans_hidden": 0, "now": 0,
                "unavailable": True}, 200


def _api_chat_seat(payload):
    """The seats panel's one mutation: bind a live agent to a memorable @name
    (seats.rename_seat — roster row + delivery state move together). Bearer-
    gated like every POST; refusals answer 400 with the CLI's own message."""
    from . import seats
    if payload.get("action") != "rename":
        return {"error": "unknown action %r (rename)" % payload.get("action")}, 400
    ok, msg = seats.rename_seat(str(payload.get("seat") or ""),
                                str(payload.get("new") or ""))
    return ({"ok": True, "note": msg} if ok else {"error": msg}), (200 if ok else 400)


def _api_chat_react(payload):
    """The owner's click-to-react: target by ts+from (the row the panel
    holds). Signed like a post; rides the same transport."""
    from . import chat
    e = payload.get("emoji")
    tts, tfrom = payload.get("tts"), payload.get("tfrom")
    if not (isinstance(e, str) and e and isinstance(tts, str) and isinstance(tfrom, str)):
        return {"error": 'payload wants {"emoji", "tts", "tfrom"}'}, 400
    room = str(payload.get("room") or "main")
    row, err = chat.react((tts, tfrom), e, room,
                          who=str(payload.get("name") or "david"),
                          profile=_chat_profile())
    if err:
        return {"error": err}, 400
    return {"ok": True, "msg": chat.public_rows([row])[0],
            "total": chat.read(room)[1]}, 200


# ── ledger: read-only projection of the attestation node's public reads ──
# The same node `helm cell` talks to (HELM_NODE_URL, default :8899), GET-only:
# receipts (the signed turn ledger, finality tier per turn), cells (seat
# activity, joined to the bounded receipt window), turn status by hash. The
# node is OPTIONAL — down/absent answers {"offline": true} at 200 and the tab
# shows "substrate offline", never an error page. The tab's ONE write (message
# a seat) rides the EXISTING /api/chat POST — no new mutation surface.
#
# Since dregg-primary signing went LIVE (chat's signed leg + the premise
# anchor), a cave turn is often something the OWNER can recognize: his web
# post (topic helm.chat — the RAM row keeps {turn}) or a premise attestation
# (topic helm.attest — the entry keeps attest_anchor_turn). _turn_about joins
# those local pointers back onto the receipt window so the tab can SHOW "your
# post landed as turn #N" instead of a bare hash; `transport` carries
# chat.transport_status()'s truth so the UI foregrounds the cave feed only
# when signing really is live — never a hardcoded claim.

LEDGER_TURNS = 40
LEDGER_STATUS_KEYS = ("healthy", "dag_height", "latest_height", "block_count",
                      "consensus_live", "federation_mode", "state_producer",
                      "lean_producer", "peer_count", "public_key")
_TURN_HASH = re.compile(r"[0-9a-f]{64}")


def _turn_about():
    """turn_hash -> what helm KNOWS landed as that cave turn — the owner-
    legible join. Chat rows that signed carry {turn} (the row's chat:b2b
    digest rode a self-write turn, topic helm.chat); store entries carry
    attest_anchor_turn (the premise anchor, topic helm.attest; legacy
    attest_turn reads too). Local reads only; each leg fails open to fewer
    labels, never an error. An unlabeled turn is simply not-ours-to-name."""
    out = {}
    try:
        from . import store
        for e in store.load_all(include_retired=True):
            for k in ("attest_anchor_turn", "attest_turn"):
                t = e.get(k)
                if t:
                    out[t] = {"kind": "attest", "id": e.get("id"),
                              "type": e.get("type")}
    except Exception:
        pass
    try:
        from . import chat
        for room in chat.list_rooms():
            rows, _total = chat.read(room)
            for m in rows:
                t = m.get("turn")
                if t:
                    out[t] = {"kind": "chat", "from": chat._dsan(m.get("from")),
                              "room": room,
                              "text": str(m.get("text") or m.get("react") or "")[:80]}
    except Exception:
        pass
    return out


def _chat_transport():
    """chat.transport_status() with the endpoint's degrade law: any surprise
    answers None (the UI reads missing as unknown and keeps the honest
    fallback framing — never an implied 'signed')."""
    try:
        from . import chat
        return chat.transport_status()
    except Exception:
        return None


def _api_ledger(qs):
    """Aggregate: node status subset + newest signed turns (bounded, each
    labeled via _turn_about when a local pointer names it) + the chat
    transport truth (signed/unsigned — what the tab's framing keys on) + cells
    with per-seat last-activity derived from the receipt window (receipt.agent
    joins cell.id 1:1 — cells with no turn in the window honestly carry None)."""
    from . import cell
    url = cell.node_url()
    status = cell.get_json(url + "/status", timeout=3)
    receipts = cell.get_json(url + "/api/receipts", timeout=3)
    transport = _chat_transport()
    if status is None and receipts is None:
        return {"offline": True, "node": url, "transport": transport}, 200
    turns = sorted((r for r in (receipts or []) if isinstance(r, dict)),
                   key=lambda r: r.get("chain_index", 0),
                   reverse=True)[:LEDGER_TURNS]
    about = _turn_about()
    for r in turns:
        a = about.get(r.get("turn_hash"))
        if a:
            r["about"] = a
    last_ts, seen = {}, {}
    for r in turns:
        a, ts = r.get("agent"), r.get("timestamp")
        if not a:
            continue
        seen[a] = seen.get(a, 0) + 1
        if ts is not None and ts > last_ts.get(a, -1):
            last_ts[a] = ts
    rows = [c for c in (cell.get_json(url + "/api/cells", timeout=3) or [])
            if isinstance(c, dict)]
    for c in rows:
        c["last_turn_ts"] = last_ts.get(c.get("id"))
        c["recent_turns"] = seen.get(c.get("id"), 0)
    rows.sort(key=lambda c: (c.get("last_turn_ts") is None,
                             -(c.get("last_turn_ts") or 0), c.get("id") or ""))
    return {"node": url,
            "status": {k: (status or {}).get(k) for k in LEDGER_STATUS_KEYS},
            "transport": transport,
            "turns": turns, "cells": rows}, 200


def _api_ledger_turn(qs):
    """One turn's durable finality certificate: proxy /api/turn/<hash>/status.
    The hash gate keeps the proxied path literal-only."""
    h = (_q1(qs, "hash") or "").strip().lower()
    if not _TURN_HASH.fullmatch(h):
        return {"error": "hash wants 64 hex chars (a turn hash)"}, 400
    from . import cell
    url = cell.node_url()
    d = cell.get_json(url + "/api/turn/%s/status" % h, timeout=3)
    if d is None:  # node down OR the node refused the hash — same degrade shape
        return {"unavailable": True, "node": url, "hash": h}, 200
    return d, 200


# ── ledger/native: the host-local telemetry surface — local reads only ──
# The cave turn-ledger above is the DURABLE dregg attestation and, now that
# dregg-primary signing is live (owner posts + premise anchors land as real
# cave turns — premise dregg-primary-corrects-native-chain-misunderstanding),
# the PRIMARY evidence. This card is the complementary HOST-LOCAL layer: the
# blake2b attest-chain (premise.py — a tamper-evident local record, honestly
# never a dregg proof), the events journal (append-only mutation receipts,
# honestly NOT hash-chained), and the RAM room's presence-chat pulse (the
# fast a2a lane — agent posts honestly unsigned until seat-signing lands;
# signed rows carry their cave receipt in the chat tab). When no signer is
# configured (HELM_CELL_BIN unset) this layer IS the coordination fallback
# and the UI says so. This endpoint projects all three — no node, no network,
# GET-only, and every leg fails open to an empty section, never an error.

NATIVE_ROWS = 30
# One chain record projects to these keys — provenance the owner cross-checks
# with `helm premise-check`; never the whole record (bounded payload).
NATIVE_REC_KEYS = ("op", "premise_id", "root", "project", "ts", "attest_by",
                   "rec_hash", "chain_index")


def _native_chat_pulse():
    """The fleet-liveness pulse off the RAM rooms: post count, newest row's
    ts/from/room. Reaction rows count as activity (last_*) but not as msgs."""
    from . import chat
    out = {"rooms": 0, "msgs": 0, "last_ts": "", "last_from": "", "last_room": ""}
    try:
        for room in chat.list_rooms():
            rows, total = chat.read(room)
            if not total:
                continue
            out["rooms"] += 1
            out["msgs"] += sum(1 for m in rows if not m.get("react"))
            last = rows[-1]
            if (last.get("ts") or "") > out["last_ts"]:
                from . import chat
                out.update(last_ts=last.get("ts") or "",
                           last_from=chat._dsan(last.get("from") or ""),
                           last_room=room)
    except Exception:
        pass  # a torn room reads as a quieter pulse, never an error
    return out


def _api_ledger_native(qs):
    """Native attestation pulse: chain head + whole-chain verification + newest
    records, newest receipts, chat pulse. verified is verify_chain() truth — a
    tampered chain surfaces here as verified:false + detail, never hidden."""
    from . import pk, premise
    recs = premise.chain_records()
    verified, detail = premise.verify_chain() if recs else (True, "")
    head = recs[-1] if recs else {}
    try:
        events = pk.read_events(NATIVE_ROWS)[::-1]  # newest-first on the wire
    except Exception:
        events = []
    return {"chain": {"count": len(recs), "verified": bool(verified),
                      "detail": "" if verified else str(detail),
                      "head_index": head.get("chain_index"),
                      "head_hash": head.get("rec_hash", ""),
                      "records": [{k: r.get(k) for k in NATIVE_REC_KEYS}
                                  for r in recs[-NATIVE_ROWS:][::-1]]},
            "events": events, "chat": _native_chat_pulse()}, 200


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


# ── multiplayer cave tab: the owner surface over the blind LocalRelay + TTL
# LocalPresence (multiplayer.py, unchanged). The relay stays blind — the state
# GET is a thin passthrough of opaque envelopes; the BROWSER folds the demo LWW
# CRDT (web_ui.html materializeCave, mirroring helm.multiplayer_demo). Presence
# is decoupled: a heartbeat POST makes the owner a live peer, no doc touched.
# The adapter boundary is untouched, so a builders.dev bridge slots in with zero
# changes to this file (register_adapter + HELM_MULTIPLAYER_BACKEND). ──
MP_OWNER_CONNECTION = "cockpit"


def _mp_actor():
    """The owner seat for cave writes/heartbeats — the same identity the chat
    post signs under (HELM_CELL_PROFILE else 'david'), never the agent default."""
    return _chat_profile()


def _mp_caves():
    """Best-effort plaintext cave list. Cave dirs are hashed keys, but every
    relay doc header carries its cave name in cleartext — read one per dir. A
    presence-only cave (no doc yet) stays invisible; the demo always writes a
    doc, so a live cave is always discoverable."""
    from . import multiplayer
    root = multiplayer.multiplayer_dir()
    caves = set()
    try:
        cave_dirs = os.listdir(root)
    except OSError:
        return []
    for cd in cave_dirs:
        p = os.path.join(root, cd)
        try:
            names = os.listdir(p)
        except OSError:
            continue
        for n in names:
            if not n.endswith(".updates.jsonl"):
                continue
            try:
                with open(os.path.join(p, n), "rb") as f:
                    head = json.loads(f.readline().decode("utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(head, dict) and isinstance(head.get("cave"), str):
                caves.add(head["cave"])
                break  # one header names the cave; no need to read the rest
    return sorted(caves)


def _mp_pub(row, fields):
    """A copy of `row` with each identity/display field in `fields` display-
    laundered through chat._dsan — the cave web wire's analog of chat.public_rows.
    A peer actor/connection/state or an envelope actor rides the BLIND relay from
    any client, so a bidi/control-char payload must never reach the cave panel."""
    from . import chat
    c = dict(row)
    for k in fields:
        if isinstance(c.get(k), str):
            c[k] = chat._dsan(c[k])
    return c


def _api_mp_state(qs):
    """The cave tab's poll (open on loopback, like every GET): the blind relay's
    opaque update log after ?after=CURSOR + the live TTL peers + the discoverable
    caves. This endpoint NEVER decodes an update — it hands the browser the raw
    envelopes and lets the client fold the CRDT, exactly as the contract says."""
    try:
        from . import multiplayer
        relay, presence = multiplayer.adapters()
    except Exception:
        return {"unavailable": True}, 200
    cave = _q1(qs, "cave", "main") or "main"
    doc = _q1(qs, "doc", "board") or "board"
    after = _q1(qs, "after", "0")
    reset = False
    try:
        state = relay.updates(cave, doc, after)
    except ValueError as e:
        # a stale/mismatched cursor (the cave doc's generation rolled, or the
        # tmpfs was wiped) — restart the client from the head so its accumulated
        # log cannot silently desync. A bad cave/doc NAME is a real 400.
        if "cursor" not in str(e):
            return {"error": str(e)}, 400
        try:
            state = relay.updates(cave, doc, 0)
            reset = True
        except ValueError as e2:
            return {"error": str(e2)}, 400
    try:
        peers = presence.peers(cave)
    except ValueError:
        peers = []
    # DISPLAY-launder every identity/display field the SERVER emits, mirroring the
    # chat wire (public_rows/_dsan): peer actor/connection/state, the demo cell's
    # envelope actor, and the discoverable cave names all ride the BLIND relay
    # from any client. The opaque `update` stays RAW — the relay never decodes it,
    # and the browser folds the CRDT and launders the decoded cell at its render
    # seam (the server cannot, without breaking the blind-relay contract).
    from . import chat
    peers = [_mp_pub(p, ("actor", "connection", "state")) for p in peers]
    updates = [_mp_pub({"id": u.get("id"), "actor": u.get("actor"),
                        "ts": u.get("ts"),
                        "bytes": len(str(u.get("update", "")).encode("utf-8")),
                        "update": u.get("update")}, ("actor",))
               for u in state["updates"]]
    caves = [chat._dsan(c) for c in _mp_caves()]
    return {"cave": cave, "doc": doc, "cursor": state["cursor"], "reset": reset,
            "updates": updates, "peers": peers, "caves": caves}, 200


def _api_mp_publish(payload):
    """The cave tab's one write: set a demo LWW cell as the owner. The reference
    encoder (helm.multiplayer_demo) builds the opaque string HERE so the CLI and
    web stay bit-identical, then hands the BLIND relay an undecoded update."""
    from . import multiplayer, multiplayer_demo
    cave = str(payload.get("cave") or "main")
    doc = str(payload.get("doc") or "board")
    key, value = payload.get("key"), payload.get("value")
    if not isinstance(key, str) or not key.strip():
        return {"error": "key is required"}, 400
    if not isinstance(value, str):
        return {"error": "value must be a string"}, 400
    relay, presence = multiplayer.adapters()
    actor = _mp_actor()
    try:
        update = multiplayer_demo.encode(key, value, actor)
        # relay.publish raises ValueError on the update/doc size caps — that is
        # bad INPUT (400), not a server fault (uncaught it would 500). (gate LOW)
        ack = relay.publish(cave, doc, actor, update)
    except ValueError as e:
        return {"error": str(e)}, 400
    # writing keeps the owner present without waiting for the next poll tick
    presence.heartbeat(cave, actor, "editing", multiplayer.DEFAULT_TTL,
                       MP_OWNER_CONNECTION)
    return {"ok": True, "ack": ack}, 200


def _api_mp_presence(payload):
    """The cave tab's owner heartbeat (mutation → bearer). Opening the tab makes
    the OWNER a live peer; the poll refreshes it so he fades out ~TTL after he
    closes it. Presence is decoupled from the doc — this never touches the log."""
    from . import multiplayer
    cave = str(payload.get("cave") or "main")
    state = str(payload.get("state") or "watching")
    _relay, presence = multiplayer.adapters()
    row = presence.heartbeat(cave, _mp_actor(), state, multiplayer.DEFAULT_TTL,
                             MP_OWNER_CONNECTION)
    return {"ok": True, "peer": row}, 200


API = {
    "/api/registry": _api_registry,
    "/api/store": _api_store,
    "/api/store/review": _api_store_review,
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
    "/api/chat": _api_chat,
    "/api/chat/roster": _api_chat_roster,
    "/api/todos": _api_todos,
    "/api/ledger": _api_ledger,
    "/api/ledger/turn": _api_ledger_turn,
    "/api/ledger/native": _api_ledger_native,
    "/api/multiplayer/state": _api_mp_state,
}

POST_API = {  # fn(payload_dict) -> (obj, status); ALL demand the mutation token
    "/api/store/confirm": _api_store_confirm,
    "/api/store/reject": _api_store_reject,
    "/api/skills/toggle": _api_skills_toggle,
    "/api/skills/delete": _api_skills_delete,
    "/api/homes": _api_homes_post,
    "/api/cwd": _api_cwd_post,
    "/api/prune": _api_prune_post,
    "/api/configs/file": _api_configs_file_post,
    "/api/configs/entry": _api_configs_entry_post,
    "/api/configs/restore": _api_configs_restore_post,
    "/api/chat": _api_chat_post,
    "/api/chat/react": _api_chat_react,
    "/api/chat/read": _api_chat_read_post,
    "/api/chat/seat": _api_chat_seat,
    "/api/chat/dm": _api_chat_dm,
    "/api/multiplayer/publish": _api_mp_publish,
    "/api/multiplayer/presence": _api_mp_presence,
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
        self._send(body, "text/html; charset=utf-8", no_cache=True)

    def _json(self, obj, status=200):
        self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", status)

    def _send(self, body, ctype, status=200, no_cache=False):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if no_cache:
            # the UI is served fresh from disk each request (the token is
            # templated in per-process); a browser cache would hand the owner a
            # STALE page after an upgrade — "where are my channels?" — so the
            # HTML shell must always re-fetch.
            self.send_header("Cache-Control", "no-store, must-revalidate")
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
