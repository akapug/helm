#!/usr/bin/env python3
"""helm watchdog — the context-window brick backstop.

The seat launch line teaches CC each proxy model's real window (seat.py mints
BOTH CLAUDE_CODE_MAX_CONTEXT_TOKENS and CLAUDE_CODE_AUTO_COMPACT_WINDOW —
capacity and window, and the second is clamped by the first), which defuses the
small-window class:
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

# THE RESPONSE MARKER, and counting on the wrong side of it was the bug.
# A CLIProxyAPI error log holds the FULL REQUEST followed by this marker and the
# response. The counter used to scan the whole tail, so every occurrence of the
# phrase in the REQUEST BODY counted as a wedge hit — and an agent discussing
# context windows writes that phrase into its own transcript. Measured
# 2026-07-25 on a REAL wedge: 5 occurrences in the request, 1 in the response,
# counted as 6. The alert was a true positive with a fabricated count.
#
# That defeats WEDGE_THRESHOLD's whole purpose. The threshold exists to separate
# a genuine wedge from "a single transient over-large turn CC could still compact
# past" — and a single genuine failure, in any conversation that mentions the
# phrase twice, clears a threshold of 2 on its own. So the discriminator could
# never discriminate. Count RESPONSES, never requests.
RESPONSE_MARKER = "=== response ==="

# >=2 recent ctx-window 400s = the compaction-also-400s loop, i.e. a real wedge
# rather than a single transient over-large turn CC could still compact past.
WEDGE_THRESHOLD = 2
# Scan only the tail of each (large, rotated) proxy log.
TAIL_BYTES = 256 * 1024
# How many of the most recent error logs to consider. The old scan read ONLY
# the newest, so a wedge LOOP — which is by definition several consecutive
# failed requests, each its own log — was judged from a single sample.
RECENT_LOGS = 6
# ...and only ones this fresh. RECENT_LOGS alone would break RECOVERY: a seat
# that wedged, got /cleared, and is now healthy would keep reading WEDGED
# forever, because its old failure logs stay on disk and stay inside the last-6
# window. The newest-only scan got recovery for free and I nearly traded it away
# for loop detection; the window buys both. A wedge is several failures CLOSE
# TOGETHER IN TIME, which is also what "the compaction-also-400s loop" means.
RECENT_SECONDS = 900

# Proxy families that run inside CC via cli-proxy and can therefore wedge this
# way. Claude-family seats never hit it (CC sizes their window correctly).
# Derived from seat.FAMILIES rather than hardcoded: a hardcoded pair silently
# stopped covering ds4pro when it was added, and would have missed gemini and
# grok the same way. An unwatched proxy seat wedges invisibly, which is the exact
# failure this module exists to prevent — so the watch list must grow with the
# fleet by construction, not by someone remembering to edit a tuple.
def proxy_families():
    try:
        from .seat import FAMILIES
        return tuple(sorted(FAMILIES))
    except Exception:
        return ("codex", "kimi", "ds4pro")


PROXY_FAMILIES = proxy_families()

# KNOWN BLIND SPOT, stated because implying coverage is worse than admitting the
# gap: a proxy failure that returns HTTP 200 WITH AN EMPTY OR MALFORMED BODY
# writes NO error log at all — the proxy does not consider it a failure. So this
# watchdog is structurally incapable of seeing it, and no signature can fix that
# because there is no file to scan. Observed live 2026-07-25: a codex seat's
# /compact died on "API returned an empty or malformed response (HTTP 200) —
# check for a proxy or gateway intercepting the request", and the log directory
# contains zero error logs with Status 200. Catching that class needs a change in
# the PROXY (log a 200 whose body is empty/unparseable), not here. `check()`
# reports this limitation rather than reporting silence as health.
BLIND_SPOT = ("a 200-with-empty-body proxy failure writes no error log and is "
              "invisible to this scan — needs a proxy-side fix, not a signature")


def _mtime(path):
    """mtime, or -1 for a log that vanished between glob and stat.

    RESCUED from main's shared working tree 2026-07-25, where it sat UNCOMMITTED
    while 69 processes shared that checkout — one reset and the only copy was
    gone. Whoever wrote it found a real bug in this module: log rotation races
    the scan, and a bare os.path.getmtime used as a sort key raises
    FileNotFoundError from INSIDE sorted(), which is OUTSIDE every try block
    here, so it would crash the whole watchdog pass rather than degrade. A
    vanished file sorts oldest, so it can never be chosen as the newest log.
    """
    try:
        return os.path.getmtime(path)
    except OSError:
        return -1.0


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
    """(failed requests whose RESPONSE carries the signature, newest log path or
    None, unreadable count).

    ONE HIT PER ERROR LOG, because one log IS one failed request — that is what
    makes WEDGE_THRESHOLD's ">=2 recent 400s" mean what it says. The old version
    counted matching LINES inside the single newest log, which conflated "N
    failed requests" with "one failure whose text mentions the phrase N times",
    and because it scanned the request body too, it counted the agent's own
    words about context windows as evidence of a wedge.

    An unreadable log is counted separately and NEVER as a hit or a miss: a file
    we could not parse is not evidence of health.
    """
    import time
    logs = sorted(glob.glob(os.path.join(logdir, "error-*.log")), key=_mtime)
    if not logs:
        return 0, None, 0
    cutoff = time.time() - RECENT_SECONDS
    hits = unreadable = 0
    newest_hit = None
    for path in logs[-RECENT_LOGS:]:
        try:
            if os.path.getmtime(path) < cutoff:
                continue                      # aged out — a recovered seat
        except OSError:
            unreadable += 1
            continue
        resp = _response_side(path)
        if resp is None:
            unreadable += 1
            continue
        if any(sig in resp for sig in SIGNATURES):
            hits += 1
            newest_hit = path
    # Report the newest MATCHING log, not merely the newest log: naming a clean
    # unrelated failure in a wedge alert sends the reader to the wrong evidence.
    return hits, (newest_hit or logs[-1]), unreadable


def _response_side(path):
    """The RESPONSE half of one error log's tail, lowercased — or None if we did
    not actually read a response.

    Returning None rather than falling back to the request text is the whole
    point: an error log is REQUEST + RESPONSE_MARKER + RESPONSE, and scanning the
    request half is what fabricated wedge counts out of an agent's own prose. If
    the marker is not in our tail window the response is out of reach, and
    "could not read" must never be laundered into "read the request instead".
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > TAIL_BYTES:
                f.seek(size - TAIL_BYTES)
            tail = f.read().decode("utf-8", "replace").lower()
    except OSError:
        return None
    i = tail.rfind(RESPONSE_MARKER)
    return tail[i + len(RESPONSE_MARKER):] if i >= 0 else None


def scan(families=PROXY_FAMILIES):
    """Return [{family, count, log, unreadable}] for seats wedging on ctx-window
    (>= WEDGE_THRESHOLD failed requests whose RESPONSE carries the signature)."""
    wedged = []
    for fam in families:
        best, where, unread = 0, None, 0
        for d in _log_dirs(fam):
            n, log, bad = _count_ctx_400s(d)
            unread += bad
            if n > best:
                best, where = n, log
        if best >= WEDGE_THRESHOLD:
            wedged.append({"family": fam, "count": best, "log": where,
                           "unreadable": unread})
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
    # guard_tail refuses junk BEFORE check() (the side-effecting scan + chat
    # alert must never run under a typo'd arg) and BEFORE honoring --help —
    # `watchdog frobnicate --help` is an existence probe and must exit 2.
    from .cli import guard_tail
    rc = guard_tail("helm watchdog", args, flags=("--json", "--quiet"),
                    usage=_USAGE)
    if rc is not None:
        return rc
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
