#!/usr/bin/env python3
"""helm creds / swap — the account scorecard and the rollover rescue, in the
terminal. CLI parity for the quota view: same provider, same truth.

creds  — every account with live headroom/state/reset, duplicate flags.
swap   — a seat ran dry mid-work: find the sessions live on a home, pick the
         healthiest OTHER account of the same provider, and print the exact
         resume block per session. helm prints commands; the human runs them
         (a swap is a seat decision, never an automatic mutation).
"""
import os
import time

from . import homes
from .providers import ProviderError, default_provider


def _rows():
    prov = default_provider()
    accounts = prov.accounts()
    states = {s.get("account"): s for s in prov.cred_state()}
    windows = {w.get("account"): w for w in prov.windows()}
    out = []
    for a in accounts:
        name = a.get("account") or a.get("name") or "?"
        st = states.get(name, {})
        w = windows.get(name, {})
        hp = st.get("headroom_pct")
        out.append({
            "account": name,
            "provider": a.get("provider") or "?",
            "tier": a.get("tier") or "",
            "headroom": (hp / 100.0) if hp is not None else None,
            "state": st.get("cred_state") or st.get("status") or "?",
            "resets_at_ms": st.get("resets_at_ms"),
            "windows_left": w.get("windows_left"),
            "windows_per_week": w.get("windows_per_week"),
            "verdict": w.get("verdict") or "",
        })
    return out


def _reset_in(ms):
    if not ms:
        return "-"
    s = ms / 1000.0 - time.time()
    if s <= 0:
        return "now"
    if s < 3600:
        return "%dm" % (s // 60)
    return "%.1fh" % (s / 3600)


def _pct(h):
    return "-" if h is None else "%d%%" % round(float(h) * 100)


def cmd_creds(args):
    """creds [--refresh] — live account scorecard (the quota view, in text)."""
    try:
        rows = _rows()
    except ProviderError as e:
        print("helm creds: no quota provider on this machine (%s) — sessions/resume "
              "still work" % e)
        return 1
    if not rows:
        print("helm creds: no accounts found.")
        return 0
    rows.sort(key=lambda r: (r["provider"], -(r["headroom"] or 0)))
    print("helm creds (%d accounts):" % len(rows))
    print("  %-9s %-30s %-9s %-9s %-8s %-9s %-14s %s" % (
        "provider", "account", "tier", "headroom", "state", "reset", "weekly", "verdict"))
    for r in rows:
        wl = ("%.1f/%.1f" % (r["windows_left"], r["windows_per_week"])
              if r["windows_left"] is not None else "-")
        print("  %-9s %-30s %-9s %-9s %-8s %-9s %-14s %s" % (
            r["provider"], r["account"][:30], (r["tier"] or "-")[:9],
            _pct(r["headroom"]), (r["state"] or "?")[:8],
            _reset_in(r["resets_at_ms"]), wl, r["verdict"]))
    return 0


def cmd_swap(args):
    """swap <home-or-account> — a seat ran dry: print resume-under-a-healthier-
    account blocks for its live sessions. Never mutates; the human runs them."""
    import sys
    if not args:
        print("usage: helm swap <home-name-or-account-email>", file=sys.stderr)
        return 2
    target = args[0]
    listing = [h for h in homes.homes_list() if not h.get("archived")]
    match = next((h for h in listing
                  if target in (h.get("name"), h.get("identity")) or target in (h.get("aliases") or [])), None)
    if match is None:
        print("helm swap: no cred home matches '%s' (see `helm homes`)" % target,
              file=sys.stderr)
        return 1
    provider = match.get("provider")
    try:
        fam = "anthropic" if provider == "claude" else provider
        rows = [r for r in _rows() if r["provider"] == fam
                and r["account"] != match.get("identity")
                and (r["headroom"] is None or r["headroom"] > 0.1)]
    except ProviderError:
        rows = []
    rows.sort(key=lambda r: -(r["headroom"] or 0))
    if not rows:
        print("helm swap: no alternative %s account with known headroom — "
              "add one (`helm homes prepare %s <email>`)" % (provider, provider))
        return 1
    best = rows[0]
    print("helm swap: healthiest %s alternative: %s (headroom %s, state %s)"
          % (provider, best["account"], _pct(best["headroom"]), best["state"]))
    from . import sessions as sess_mod
    target_home = next((h for h in listing if h.get("identity") == best["account"]
                        and h.get("provider") == provider), None)
    env_var = "CLAUDE_CONFIG_DIR" if provider == "claude" else "CODEX_HOME"
    live = match.get("live_pids") or []
    print("  live pids on '%s': %s" % (match.get("name"),
                                       ",".join(map(str, live)) or "none detected"))
    recent = sess_mod.rows_for(limit=5)
    from_home = [r for r in recent if r["h"] == ("claude" if provider == "claude" else "codex")]
    if not from_home:
        print("  no recent %s sessions in the catalog to re-seat" % provider)
        return 0
    print("  resume blocks (newest %d — run in fresh terminals AFTER stopping the dry seat):"
          % len(from_home[:3]))
    for r in from_home[:3]:
        base = sess_mod.resume_command(r)
        if target_home:
            # inject the env prefix onto the HARNESS clause (the last " && "
            # clause), never a raw substring replace — a cwd containing
            # "claude " must not be corrupted (test-pinned)
            head, sep, tail = base.rpartition(" && ")
            if sep and tail.startswith(provider + " "):
                base = "%s && env %s=%s %s" % (head, env_var,
                                               target_home.get("path", "?"), tail)
        print("    " + base)
    return 0
