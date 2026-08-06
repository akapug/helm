"""Quota projections for :mod:`helm.web`."""
import sys

_web = sys.modules.get(__package__ + ".web")
if _web is not None:
    globals().update({name: value for name, value in vars(_web).items()
                      if not (name.startswith("__") and name.endswith("__"))})



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
    return (os.environ.get("HELM_ALLOC_MODELS") or "fable,opus,gpt-5.5").split(",")



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
    """Same shape as the predecessor's /api/status: is a provider present, how
    many accounts, does cv exist, how big is the cached catalog."""
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
                         os.environ.get("HELM_QUOTA_CLI") or "")),
                     "accounts": creds_n,
                     "degraded": creds_n == 0,
                     "fallback": "(default) account resume works without a provider"},
        "cv": bool(shutil.which("cv")),
        "catalogRows": len(cat[1]) if cat else None,
        "mutations": "POST JSON + Authorization: Bearer (per-process token, "
                     "templated into the UI)",
    }
del _web
