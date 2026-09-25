"""SSE watcher lifecycle for :mod:`helm.web`."""
import sys
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
import os
import threading
import time

_web = sys.modules.get(__package__ + ".web")
if _web is not None:
    globals().update({name: value for name, value in vars(_web).items()
                      if not (name.startswith("__") and name.endswith("__"))})



# ---------- the SSE doorbell (REARCH-web-0.2 leg 2: poll -> push) ----------
# The lineage's settled pattern (the predecessors' firehose and event glue):
# push says "there's news", the EXISTING cursor read fetches it — events are
# DOORBELLS, never payloads, so the read endpoints stay the one render truth.
# ONE watcher thread stat-sweeps the chat room dir (tmpfs, ~13 files) every
# 250ms and notifies a Condition; each /api/events client blocks on it
# (thread-per-client is already this server's model). The 250ms tick is also
# the coalescer: a burst of posts is at most 4 doorbells/s. Keepalive comment
# every 20s + no-cache/no-transform (the glue anti-proxy-buffering pair);
# EventSource gives the client auto-reconnect for free.
# ---------- the SSE doorbell (REARCH-web-0.2 leg 2: poll -> push) ----------
# The lineage's settled pattern (the predecessors' firehose and event glue):
# push says "there's news", the EXISTING cursor read fetches it — events are
# DOORBELLS, never payloads, so the read endpoints stay the one render truth.
#
# LIFECYCLE IS PER-SERVER (meld-converged 2026-07-23, CD design + kimi
# concurrency clear): each _Server owns its OWN watcher thread + state, so
# the three round-4 races are UNEXPRESSIBLE rather than guarded — no
# generation race (a closed server never re-arms), no refcount (nothing
# shared to count), no cross-server kill (B never sees A's close). Doorbells
# are content-free (data:{}), so there is no cross-server seq space to
# preserve: any fresh watcher answers "did the fingerprint change" and the
# content always arrives via the client's normal cursor poll (the
# dissolve-beats-mechanize verdict). The rooms-summary cache stays GLOBAL —
# it is orthogonal, lock-coupled, and TTL-bounded (round-3 fix).
_SSE_WATCH_S = 0.25
_SSE_IDLE_WATCH_S = 10.0  # poll cadence when no client is connected — background
                           # watcher burns 54% of a core at 250ms forever (#281)

_SSE_DEAD_S = 5.0    # a beat older than this = that server's watcher died



def _sse_state():
    """A fresh per-server watcher state (minted in make_server). `boot` is
    the per-server THREAD-IDENTITY token: each arm bumps it, and a thread
    acts only while it still holds the current boot — found by the meld's
    own liveness pin: a T1 sleeping through stop+re-arm resumed looping on
    the RESTORED flag (two loops), and a crashed T1's finally could stomp
    T2's fresh running (prod-reachable via crash + fast re-arm). Per-server
    dissolved the CROSS-server races; same-server thread identity still
    needs this one token."""
    return {"cond": threading.Condition(), "chat_fp": None, "seq": 0,
            "running": False, "beat": 0.0, "boot": 0, "connected": 0}



_CHAT_NAMES = {}          # base dir -> (dir mtime_ns, read_at, [.jsonl names])

# THE SLICE RUNNER'S DATA AUDIT (helm/gateslice.py) reports any module
# data a test unit leaves behind; these names are process-wide by design.
_GATESLICE_MUTABLE = {
    "_CHAT_NAMES": "room names per base dir, checked against the dir's mtime",
}
_NAMES_MAX_AGE_S = 30.0   # the backstop below; NOT the freshness mechanism


def _log_names(base):
    """The `.jsonl` names in `base`, re-listed only when the DIRECTORY moves.
    -> [names], or None when the dir cannot be read.

    THE LISTING IS THE COST AND ALMOST NONE OF IT IS USEFUL. Measured live
    2026-08-06 on /dev/shm/helm-chat: 8318 entries at the top, of which 103
    are `.jsonl`; one `_chat_fingerprint` was 8.35ms and 5.70ms of that was
    this single listdir. The rest — cursors, locks, stopwhisper state — is
    enumerated four times a second to be discarded four times a second.

    A DIRECTORY'S OWN mtime IS THE EXACT SIGNAL, not a heuristic: it moves
    when an entry is created, deleted or renamed, and does NOT move when an
    existing file is appended. Appends are precisely what the per-file stats
    below already catch, so caching the NAME list across ticks loses nothing
    — a new room or DM lane bumps the dir and the very next tick re-lists.

    THE AGE BACKSTOP IS FOR THE ONE CASE mtime CANNOT COVER, and it is a
    containment bound rather than the mechanism: if a create ever landed
    inside the same mtime_ns as the read that cached the list, that room's
    appends would be invisible until the NEXT directory change, which could
    be never. 30s caps that at 30s instead of forever, and still cuts the
    listing rate by ~120x against a 250ms tick."""
    try:
        dst = os.stat(base)
    except OSError:
        return None
    hit = _CHAT_NAMES.get(base)
    if hit is not None and hit[0] == dst.st_mtime_ns \
            and (time.time() - hit[1]) < _NAMES_MAX_AGE_S:
        return hit[2]
    try:
        names = [n for n in os.listdir(base) if n.endswith(".jsonl")]
    except OSError:
        return None
    _CHAT_NAMES[base] = (dst.st_mtime_ns, time.time(), names)
    return names


def _chat_fingerprint():
    """Order-independent combine over ONLY the canonical message logs:
    <room>.jsonl at the dir top + dm/<seat>.jsonl one level down. The chat
    dir also holds THOUSANDS of cursor/lock/stopwhisper state files (9,085
    measured live) — statting them made 7/8 doorbells noise, and because DMs
    live in the dm/ SUBDIR a flat listdir MISSED real DM appends entirely
    (xrev, both E2E-reproduced). The file NAME is folded into each
    term so two logs swapping identical (mtime,size) cannot cancel
    (collision-safe generation)."""
    from . import chat
    try:
        d = chat.chat_dir()
    except OSError:
        return None
    fp, seen = 0, False
    for base in (d, os.path.join(d, "dm")):
        names = _log_names(base)
        if names is None:
            continue
        seen = True
        for name in names:
            try:
                st = os.stat(os.path.join(base, name))
            except OSError:
                continue
            fp ^= hash((base, name, st.st_mtime_ns, st.st_size))
    return fp if seen else None



def _sse_tick(srv, boot=None):
    """One watcher heartbeat for THIS server: refresh its beat; on a
    fingerprint change invalidate the (global) rooms-summary cache BEFORE
    ringing — the poll a doorbell triggers must read FRESH state (round-2
    finding). A stopped server's in-flight tick is a no-op (running checked
    under the server's own cond — kimi pressure-test #2), and so is a
    SUPERSEDED thread's (boot mismatch — direct/test callers pass None)."""
    fp = _chat_fingerprint()
    sse = srv._sse
    with sse["cond"]:
        if not sse["running"] or \
                (boot is not None and sse["boot"] != boot):
            return False        # stopped, or a superseded thread — no-op
        sse["beat"] = time.time()
        if fp == sse["chat_fp"]:
            return False
        sse["chat_fp"] = fp
        _rooms_summary_invalidate()
        sse["seq"] += 1
        sse["cond"].notify_all()
        return True



def _sse_watcher_dead(sse):
    """The stream loop's health predicate, scoped to THAT SERVER's state
    (kimi's one CLEAR condition): not running, or armed-but-silent past
    _SSE_DEAD_S. A dead watcher must END its server's streams — otherwise
    keepalives keep ES_LIVE true and every client sits on the stretched 10s
    poll forever. One server's death can never false-trigger another's."""
    return (not sse["running"]) or \
        (time.time() - sse["beat"] > _SSE_DEAD_S)



def _sse_watcher(srv, boot):
    sse = srv._sse
    try:
        while True:
            with sse["cond"]:
                if not sse["running"] or sse["boot"] != boot:
                    return      # stopped, or superseded — exit clean
                s = _SSE_WATCH_S if sse.get("connected", 0) > 0 \
                    else _SSE_IDLE_WATCH_S
                sse["cond"].wait(timeout=s)
            _sse_tick(srv, boot)
    finally:
        # containment for a CRASH (tick raised): drop running so this
        # server's streams end and its NEXT client re-arms — but ONLY while
        # we still hold the boot: a superseded thread's finally must never
        # stomp its successor's running (the crash+fast-re-arm stomp)
        with sse["cond"]:
            if sse["running"] and sse["boot"] == boot:
                sse["running"] = False
                sse["beat"] = 0.0
                sse["cond"].notify_all()



def _sse_ensure_watcher(srv):
    """Arm THIS server's watcher if none runs; True = one is running. The
    per-server cond serializes racing streams (kimi pressure-test #1); the
    state rolls back if Thread.start refuses (round-3 fix) so a start
    failure is an honest 503, never a permanently-armed flag."""
    sse = srv._sse
    with sse["cond"]:
        if sse["running"]:
            return True
        sse["boot"] += 1                       # mint this thread's identity
        my_boot = sse["boot"]
        sse["running"] = True
        sse["beat"] = time.time()
        sse["chat_fp"] = _chat_fingerprint()   # baseline, no boot storm
    try:
        threading.Thread(target=_sse_watcher, args=(srv, my_boot),
                         daemon=True, name="helm-sse-watcher").start()
        return True
    except Exception:
        with sse["cond"]:
            if sse["boot"] == my_boot:         # never clobber a newer arm
                sse["running"] = False
                sse["beat"] = 0.0
        return False



def _sse_stop(srv):
    """Stop THIS server's watcher (server_close): drop running — the loop
    exits within a tick — and wake its streams so the death-aware wait ends
    them NOW, not at timeout (round-4 P2)."""
    sse = srv._sse
    with sse["cond"]:
        sse["running"] = False
        sse["beat"] = 0.0
        sse["cond"].notify_all()
del _web
