#!/usr/bin/env python3
"""helm brief — the operator's morning brief, composed from what the estate
already knows. READ-ONLY everywhere and NETWORK-NEVER: catalog rows, the typed
store, the inject ledger, drain receipts, the creds probe cycle's cached
observations, doctor-style owner gates. A brief must cost nothing to ask for —
no probe, no mutation, no LLM.

Sections (headline-first; an empty section is omitted entirely):
  SINCE YOU LEFT  — sessions active in the window, bucketed by project
  KNOWLEDGE DELTA — store entries added/updated/retired + drain receipts,
                    plus inject-ledger turn stats (top-firing, silent-rate)
  SEATS           — freshest cached quota observation per account
                    (no cache -> "quota: run `helm creds`", never a probe)
  WAITING ON YOU  — owner-gated items the estate already records
"""
import calendar
import glob
import json
import os
import sys
import time

from . import home, pk

# The creds probe cycle's observation log (providers.NativeQuotaProvider
# appends here). Reading it IS the cheapest quota read there is.
USAGE_HISTORY = os.path.join(os.path.expanduser("~"), ".cache", "helm",
                             "native-usage-history.jsonl")

TOP_FIRING = 3   # inject entries named in the ledger line
MAX_PROJECTS = 6  # session buckets shown (the ~40-line render cap)
MAX_SEATS = 6
MAX_WAITING = 6


def _ts_epoch(s):
    """Store/ledger timestamp -> utc epoch: '...T..:..:..Z', tz-less ISO, or
    date-only ('2026-06-01' stated_ts rows are live in the adopted store).
    Fail-open: unparseable -> 0 (never in any window)."""
    s = str(s or "").strip()
    for fmt, n in (("%Y-%m-%dT%H:%M:%S", 19), ("%Y-%m-%d", 10)):
        try:
            return calendar.timegm(time.strptime(s[:n], fmt))
        except ValueError:
            pass
    return 0


# ------------------------------------------------------------- since you left

def _sessions_delta(cutoff):
    """Catalog rows (read-only, single-flight cached) touched in the window,
    bucketed by registry project; synthetic (pruned-copy) rows excluded."""
    from . import sessions
    buckets = {}
    total = 0
    for r in sessions.rows_for():
        if (r.get("mt") or 0) < cutoff:
            continue
        total += 1
        b = buckets.setdefault(r.get("project") or "-", {
            "name": r.get("project") or "-", "n": 0, "latest_mt": 0, "latest_title": ""})
        b["n"] += 1
        if (r.get("mt") or 0) >= b["latest_mt"]:
            b["latest_mt"] = r.get("mt") or 0
            b["latest_title"] = r.get("t") or ""
    projects = sorted(buckets.values(), key=lambda b: (-b["n"], -b["latest_mt"]))
    return {"total": total, "projects": projects}


# ------------------------------------------------------------ knowledge delta

def _store_entries():
    """Estate-wide store view, ALL statuses: the global root set plus each
    registry project's own root, deduped by path (a project entry shadowing a
    global one still counts once)."""
    from . import registry, store
    by_path = {}
    for root, scope, d in store.roots():
        by_path.update({e["path"]: e for e in store._load_root(root, scope, d).values()})
    for name in sorted(registry.load().get("projects") or {}):
        d = home.project_dir(name)
        by_path.update({e["path"]: e
                        for e in store._load_root("project", "project:" + name, d).values()})
    return list(by_path.values())


def _knowledge_delta(cutoff):
    """Typed-store movement in the window: retired outranks added outranks
    updated per entry (one entry, one bucket), plus drain-receipt action counts
    (the receipts ARE the drained ledger — nothing re-derived)."""
    from . import store
    added, updated, retired = [], [], []
    for e in _store_entries():
        if e.get("type") == "episodic":
            continue  # bulk memory: no authored timestamps, pure noise here
        rid = str(e.get("id") or e.get("term") or "")
        rt = _ts_epoch(e.get("retired_ts"))
        st = _ts_epoch(e.get("stated_ts"))
        lu = _ts_epoch(e.get("last_updated") or e.get("updated_ts"))
        if rt >= cutoff and rt and e.get("status") != store.STATUS_LIVE:
            retired.append(rid)
        elif st and st >= cutoff:
            added.append(rid)
        elif lu and lu >= cutoff:
            updated.append(rid)
    drained = 0
    for rp in sorted(glob.glob(os.path.join(store.adopted_dir(),
                                            "archive", "drain-*", "RECEIPT.json"))):
        r = pk.read_json(rp) or {}
        if _ts_epoch(r.get("ts")) >= cutoff:
            drained += int(r.get("applied") or 0)
    return {"added": sorted(added), "updated": sorted(updated),
            "retired": sorted(retired), "drained": drained}


def _inject_stats(cutoff):
    """Inject-ledger stats over the window (main + one rotated generation).
    None when no ledger exists (an older estate — the section just stays out);
    rows are v:1 shape but every field read is optional (fail-open per line)."""
    from . import inject
    path = inject._ledger_path()
    if not (os.path.exists(path) or os.path.exists(path + ".1")):
        return None
    turns = silent = 0
    fired = {}
    for p in (path + ".1", path):
        try:
            fh = open(p, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict) or _ts_epoch(row.get("ts")) < cutoff:
                    continue
                turns += 1
                if row.get("silent"):
                    silent += 1
                    continue
                for ids in (row.get("fired") or {}).values():
                    for i in ids or ():
                        fired[str(i)] = fired.get(str(i), 0) + 1
    top = sorted(fired.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_FIRING]
    starved, always_n = [], 0
    try:
        # pinned starvation, surfaced where the owner actually looks (the
        # 6/11-never-fired class): store.pinned_stats walks the same ledger
        # generations; ids that never made a single injection are the tail.
        from . import store
        s = store.pinned_stats()
        if s["rows"]:
            always_n = len(s["made"])
            starved = sorted(i for i, c in s["made"].items() if c == 0)
    except Exception:
        pass  # fail-open: the brief never dies on a stats walk
    return {"turns": turns, "silent": silent,
            "silent_rate": round(silent / turns, 2) if turns else 0.0,
            "top": top, "starved": starved, "always_n": always_n}


# --------------------------------------------------------------- seat reality

def _seats():
    """Freshest cached observation per account from the probe cycle's history
    log. NEVER probes: no cache -> no rows (render says run `helm creds`)."""
    best = {}
    try:
        fh = open(USAGE_HISTORY, encoding="utf-8", errors="replace")
    except OSError:
        return []
    with fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            a = r.get("account") if isinstance(r, dict) else None
            if not a or not r.get("gauges"):
                continue
            # key by (provider, account) — one email can seat BOTH an
            # anthropic and a codex identity; account-only keying let the
            # most-recent probe silently replace the other provider's seat
            k = (r.get("provider") or "?", a)
            if k not in best or str(r.get("probed_at") or "") > str(best[k].get("probed_at") or ""):
                best[k] = r
    from .providers import NativeQuotaProvider
    out = []
    for (_prov, a), r in best.items():
        g = NativeQuotaProvider._primary(r["gauges"])
        out.append({"account": a, "provider": r.get("provider") or "?",
                    "headroom_pct": round(100 - g["utilization"] * 100, 1) if g else None,
                    "window": (g or {}).get("label") or "?",
                    "status": r.get("status") or "",
                    "probed_at": r.get("probed_at") or ""})
    out.sort(key=lambda s: (s["provider"], -(s["headroom_pct"] or 0)))
    return out


# ------------------------------------------------------------- waiting on you

def _waiting():
    """Owner-gated items the estate already records — mechanical read-only
    scans only, mirroring what doctor surfaces, no LLM, no probe."""
    from . import registry, store, whoami
    from .premise import _queue_path
    items = []
    p = whoami.load_profile()
    if p["interview_status"] != "done":
        items.append("know-your-user interview %s — `helm interview`"
                     % (p["interview_status"] or "not started"))
    try:
        with open(_queue_path(), encoding="utf-8") as f:
            n = sum(1 for l in f if l.strip())
    except OSError:
        n = 0
    if n:
        items.append("%d attestation%s queued — `helm premise --retry-queue`"
                     % (n, "s"[:n != 1]))
    stale = []
    for name, rec in sorted((registry.load().get("projects") or {}).items()):
        if rec.get("retired"):
            continue
        pd = home.project_dir(name)
        mem = rec.get("memory_dir")
        if (os.path.islink(pd) and not os.path.exists(pd)) \
                or (rec.get("path") and not os.path.exists(rec["path"])) \
                or (mem and not os.path.isdir(mem)):
            stale.append(name)
    if stale:
        items.append("%d project pointer%s stale (%s) — `helm doctor`" % (
            len(stale), "s"[:len(stale) != 1],
            ", ".join(stale[:3]) + (", …" if len(stale) > 3 else "")))
    try:
        files = [f for f in os.listdir(store.adopted_dir()) if f.endswith(".md")]
    except OSError:
        files = []
    dups = {f[5:] for f in files if f.startswith("prem-")} \
        & {f[6:] for f in files if f.startswith("prior-")}
    if dups:
        items.append("%d prem/prior duplicate pair%s — `helm drain --sweep-dups` "
                     "(owner-gated)" % (len(dups), "s"[:len(dups) != 1]))
    return items


# --------------------------------------------------------------- compose/render

def compose(hours=12.0):
    """The raw brief dict (`--json` prints exactly this)."""
    cutoff = time.time() - hours * 3600
    return {"generated_at": pk.now_ts(), "hours": hours,
            "sessions": _sessions_delta(cutoff),
            "knowledge": _knowledge_delta(cutoff),
            "inject": _inject_stats(cutoff),
            "seats": _seats(),
            "waiting": _waiting()}


def _ago(iso):
    e = _ts_epoch(iso)
    if not e:
        return "?"
    s = max(0, time.time() - e)
    if s < 3600:
        return "%dm ago" % (s // 60)
    if s < 86400:
        return "%dh ago" % (s // 3600)
    return "%dd ago" % (s // 86400)


def render(b):
    """Tight, headline-first text (~40 lines max): every section headed and
    skippable, empty sections omitted, a no-data estate says so in one line."""
    lines = ["helm brief — %s (last %gh)" % (b["generated_at"], b["hours"])]
    s = b["sessions"]
    if s["total"]:
        lines += ["", "SINCE YOU LEFT — %d session%s, %d project%s" % (
            s["total"], "s"[:s["total"] != 1],
            len(s["projects"]), "s"[:len(s["projects"]) != 1])]
        for p in s["projects"][:MAX_PROJECTS]:
            lines.append("  %-20s %3d  %s" % (p["name"][:20], p["n"],
                                              (p["latest_title"] or "")[:46]))
        more = len(s["projects"]) - MAX_PROJECTS
        if more > 0:
            lines.append("  (+%d more project%s)" % (more, "s"[:more != 1]))
    k, inj = b["knowledge"], b["inject"]
    segs = [fmt % len(k[key]) for fmt, key in
            (("+%d added", "added"), ("%d updated", "updated"), ("%d retired", "retired"))
            if k[key]]
    if k["drained"]:
        segs.append("%d drained" % k["drained"])
    live_inject = bool(inj and inj["turns"])
    if segs or live_inject:
        lines += ["", "KNOWLEDGE DELTA — " + (" · ".join(segs) or "store unchanged")]
        for rid in k["added"][:3]:
            lines.append("  + " + rid)
        if live_inject:
            top = ", ".join("%s ×%d" % (i, n) for i, n in inj["top"])
            lines.append("  inject: %d turn%s · %d%% silent%s" % (
                inj["turns"], "s"[:inj["turns"] != 1],
                round(inj["silent_rate"] * 100), " · top " + top if top else ""))
            if inj.get("starved"):
                ids = ", ".join(inj["starved"][:4])
                more = len(inj["starved"]) - 4
                lines.append("  pinned starvation: %d of %d never fired%s — "
                             "`helm store pinned --stats` (demote or reword)" % (
                                 len(inj["starved"]), inj["always_n"],
                                 ": " + ids + (" +%d" % more if more > 0 else "")))
    lines += ["", "SEATS"]
    if b["seats"]:
        for r in b["seats"][:MAX_SEATS]:
            hp = "-" if r["headroom_pct"] is None else "%d%%" % round(r["headroom_pct"])
            lines.append("  %-9s %-30s %4s headroom (%s)  probed %s" % (
                r["provider"], r["account"][:30], hp, r["window"], _ago(r["probed_at"])))
    else:
        lines.append("  quota: run `helm creds`")
    if b["waiting"]:
        lines += ["", "WAITING ON YOU"]
        lines += ["  - " + it for it in b["waiting"][:MAX_WAITING]]
    if not (s["total"] or segs or live_inject or b["waiting"]):
        lines.insert(1, "quiet — nothing new in the window.")
    return "\n".join(lines)


def cmd_brief(args):
    """brief [--hours N] [--json] — the operator's morning brief: session
    activity, knowledge delta, cached seat reality, owner gates. Read-only,
    never probes the network."""
    hours = 12.0
    if "--hours" in args:
        try:
            hours = float(args[args.index("--hours") + 1])
        except (IndexError, ValueError):
            print("usage: helm brief [--hours N] [--json]", file=sys.stderr)
            return 2
    b = compose(hours=hours)
    if "--json" in args:
        print(json.dumps(b, indent=2, ensure_ascii=False))
        return 0
    print(render(b))
    return 0
