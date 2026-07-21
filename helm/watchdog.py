#!/usr/bin/env python3
"""helm watchdog — the context-window brick backstop.

The seat launch line teaches CC each proxy model's real window
(seat.py CLAUDE_CODE_MAX_CONTEXT_TOKENS), which defuses the small-window class:
a seat now compacts before it overflows. But a seat can still wedge the OTHER
way — CC's token gauge silently under-reads (stale usage anchor after a 429
storm / aborted streams), sails past the real window, and 400s "input exceeds
the context window". Once over, in-band compaction replays the same oversized
transcript and 400s too, CC's circuit breaker trips, and the seat is
hard-stalled until an out-of-band /clear. No threshold knob prevents a gauge
under-read, so this external detector is the required backstop.

A wedged seat cannot self-report (external-liveness canon), so the watchdog
scans each proxy seat's error log for the signature and raises a LOUD a2a alert
once per wedge, so the owner/integrator can /clear + relaunch (a relaunched seat
inherits the ctx-window env, so the reborn seat is protected).

Detection only. The rescue (/clear) is an interactive command in the seat's PTY;
a background process must never forge it. Making the wedge VISIBLE is the value.
"""
import glob
import os

from . import home

# CC/proxy surfaces the ctx-window overflow a few ways; a log line matching any
# counts. Kept lowercase for case-insensitive substring matching.
SIGNATURES = ("exceeds the context window", "context_length_exceeded",
              "context window")

# >=2 recent ctx-window 400s = the compaction-also-400s loop, i.e. a real wedge
# rather than a single transient over-large turn CC could still compact past.
WEDGE_THRESHOLD = 2
# Scan only the tail of each (large, rotated) proxy log.
TAIL_BYTES = 256 * 1024

# Proxy families that run inside CC via cli-proxy and can therefore wedge this
# way. Claude-family seats never hit it (CC sizes their window correctly).
PROXY_FAMILIES = ("codex", "kimi")


def _log_dirs(family):
    """Where a family's CLIProxyAPI writes its error logs. codex writes under
    its own seat auth dir; kimi's proxy writes to the shared global log dir."""
    from . import seat
    if family == "kimi":
        cand = [os.path.join(os.path.expanduser("~"), ".cli-proxy-api", "logs")]
    else:
        cand = [os.path.join(seat.seat_dir(family), "auth", "logs")]
    return [d for d in cand if os.path.isdir(d)]


def _state_path():
    return os.path.join(home.helm_home(), home.GLOBAL, ".state", "watchdog.json")


def _count_ctx_400s(logdir):
    """Count ctx-window signature hits in the tail of the newest error log.
    Returns (hits, newest_log_path_or_None)."""
    logs = sorted(glob.glob(os.path.join(logdir, "error-*.log")),
                  key=os.path.getmtime)
    if not logs:
        return 0, None
    newest = logs[-1]
    try:
        size = os.path.getsize(newest)
        with open(newest, "rb") as f:
            if size > TAIL_BYTES:
                f.seek(size - TAIL_BYTES)
            tail = f.read().decode("utf-8", "replace").lower()
    except OSError:
        return 0, newest
    hits = sum(1 for ln in tail.splitlines()
               if any(sig in ln for sig in SIGNATURES))
    return hits, newest


def scan(families=PROXY_FAMILIES):
    """Return [{family, count, log}] for seats wedging on ctx-window
    (>= WEDGE_THRESHOLD ctx-window 400s in their newest error log)."""
    wedged = []
    for fam in families:
        best, where = 0, None
        for d in _log_dirs(fam):
            n, log = _count_ctx_400s(d)
            if n > best:
                best, where = n, log
        if best >= WEDGE_THRESHOLD:
            wedged.append({"family": fam, "count": best, "log": where})
    return wedged


def _alert_text(w):
    return ("⚠️ WATCHDOG: seat %s is WEDGED on the context window "
            "(%d 'exceeds context window' 400s in %s). It cannot recover in-band "
            "— /clear that seat + relaunch; the reborn seat inherits the "
            "ctx-window env and is protected. [ctx-window-recovery-is-clear]"
            % (w["family"], w["count"], os.path.basename(w["log"] or "?")))


def check(families=PROXY_FAMILIES, post=True):
    """Scan, de-dup against prior alerts, and (unless post=False) raise a loud
    a2a alert for each NEWLY-wedged seat. Returns {"wedged": [...],
    "fresh": [...]} — fresh = the ones alerted this call. Alerts fire once per
    wedge (re-fire only if the 400 count climbs); a recovered seat's state is
    cleared so a future wedge alerts again."""
    from . import pk
    wedged = scan(families)
    st = pk.read_json(_state_path(), {}) or {}
    fresh = []
    for w in wedged:
        prev = st.get(w["family"], {})
        if w["count"] > prev.get("alerted_count", 0):
            fresh.append(w)
            st[w["family"]] = {"alerted_count": w["count"], "log": w["log"]}
    live = {w["family"] for w in wedged}
    for fam in list(st):
        if fam not in live:          # recovered -> re-arm for the next wedge
            st.pop(fam, None)
    p = _state_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    pk.write_json(p, st)
    if post and fresh:
        from . import chat
        for w in fresh:
            try:
                chat.post(_alert_text(w), who="watchdog")
            except Exception as e:  # a down chat node must never wedge the watchdog
                print("helm watchdog: alert post failed (%s): %s"
                      % (w["family"], e))
    return {"wedged": wedged, "fresh": fresh}


_USAGE = ("usage: helm watchdog [--json] [--quiet]\n"
          "  Scan proxy seat error logs for the context-window wedge signature\n"
          "  and raise a loud a2a alert (once per wedge) so a human can /clear +\n"
          "  relaunch. Run it periodically (a Monitor/cron loop) as the live\n"
          "  backstop. --quiet = detect only, no chat post. --json = machine form.\n"
          "  Prints a `WEDGE <family> ...` line to stdout on a FRESH wedge.")


def cmd_watchdog(args):
    if "--help" in args or "-h" in args:
        print(_USAGE)
        return 0
    res = check(post="--quiet" not in args)
    if "--json" in args:
        import json
        print(json.dumps(res))
        return 0
    for w in res["fresh"]:
        print("WEDGE %s: %d ctx-window 400s (%s)"
              % (w["family"], w["count"], os.path.basename(w["log"] or "?")))
    stale = [w for w in res["wedged"] if w not in res["fresh"]]
    for w in stale:
        print("wedged %s: %d ctx-window 400s [already alerted]"
              % (w["family"], w["count"]))
    if not res["wedged"]:
        print("watchdog: all proxy seats clear")
    return 0
