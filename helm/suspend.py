"""The post-suspend sweep: a laptop suspend stops the box's clock, not the world's.

MEASURED after a 6h23m suspend (task/2721): every family proxy kept dead
keep-alive connections to its upstream and hung requests for minutes (a seat
sat on "Request timed out, retrying 5/10"; a proxy logged a 500 after 5m08s;
an eight-token canary never returned); the proxywatch pass blocked on the
first hung canary, its proof state went 24,380 s old against a 2,400 s bar,
and no seat could mint a verdict; every 30-minute beacon had expired; every
stall clock counted hours nobody could act in. The cure applied by hand was
the shape this module automates: bounce the proxy processes (agents keep
their panes, context and beacons; one in-flight request fails and the client
retries), then kick one fresh proxywatch pass.

DETECTION IS A CLOCK DIFFERENCE, NOT A LOG SCRAPE. CLOCK_BOOTTIME advances
through a suspend and CLOCK_MONOTONIC does not, so their difference is the
seconds this boot has spent suspended. The rung records that difference on
every run and reads a GROWTH past the threshold as "resumed since last run",
with the growth as the length of the sleep. A first run records and does no
sweep; an unavailable clock is UNKNOWN and does nothing, never a sweep.

The rung rides `helm seat doctor --ensure` (the */3 cron that already
supervises every proxy) and never moves that verb's rc: a host that never
sleeps sees one silent record per pass. Beacon re-arm and stall-clock
correction are the sweep's later arcs (task/2721); this slice is the proxy
and proof half, the two that made the fleet unable to mint.
"""

import json
import os
import subprocess
import sys
import time

# `seat` IS IMPORTED EXPLICITLY at module scope, which is an ancestor of
# every function below: `_live_proxies` imports helm.seat_health, and the
# co-occurrence guard in tests/test_seat_facade_injection.py requires an
# accepted facade import in the same or an enclosing scope. It asserts
# nothing about import order.
from . import home, proxywatch, seat  # noqa: F401

RESUME_THRESHOLD_S = 60.0
_STATE = "suspend.json"


def suspended_seconds():
    """Seconds this boot has spent suspended, or None when unreadable.

    ONE READER OF THIS FACT LIVES IN THE TREE ALREADY and this defers to it:
    `proxywatch.host_suspend_gap_s`, which reports the gap for turn-state
    liveness and already owns the None-means-cannot-tell rule (a zero here
    would read every unreadable host as never-suspended). Two spellings of
    one clock reading is how they drift."""
    return proxywatch.host_suspend_gap_s()


def _state_path():
    return os.path.join(home.helm_home(), home.GLOBAL, ".state", _STATE)


def _read_state():
    try:
        with open(_state_path(), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or not isinstance(d.get("suspended_s"),
                                                   (int, float)):
        return None
    return d


def _write_state(suspended_s, now):
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"suspended_s": suspended_s, "ts": now}, f)
    os.replace(tmp, path)


def detect(now=None, threshold_s=RESUME_THRESHOLD_S, reader=suspended_seconds):
    """One reading against the recorded one. Returns a dict:
    state 'unknown' (clock unavailable; nothing written), 'first-run'
    (recorded, no sweep), 'steady' (growth under the threshold), or 'resumed'
    with gap_s (the growth = the sleep) and last_ts (the previous record)."""
    now = time.time() if now is None else now
    drift = reader()
    if drift is None:
        return {"state": "unknown", "gap_s": None, "last_ts": None}
    prior = _read_state()
    _write_state(drift, now)
    if prior is None:
        return {"state": "first-run", "gap_s": None, "last_ts": None}
    gap = drift - prior["suspended_s"]
    if gap > threshold_s:
        return {"state": "resumed", "gap_s": gap, "last_ts": prior.get("ts")}
    return {"state": "steady", "gap_s": gap, "last_ts": prior.get("ts")}


def _live_proxies():
    """(family, seat) for every minted proxy that has a running pid."""
    from . import seat_health
    out = []
    for family, seat in seat_health._minted_seats():
        if seat_health._running_pid(family, seat):
            out.append((family, seat))
    return out


def _bounce(family, seat):
    """Restart one proxy process through the landed ownership primitives —
    never a second spawn path. Agent state is untouched: only the proxy
    restarts."""
    from . import seat_proxy
    down = seat_proxy._down(family, seat=seat)
    up = seat_proxy._up(family, quiet=True, seat=seat)
    return down == 0 and up == 0


def _kick_pass():
    """One detached `helm proxywatch --force` so the proof state is rebuilt
    on live connections; the cron must not block on it (a pass can take
    minutes when every proxy was hung)."""
    log = os.path.join(home.helm_home(), home.GLOBAL, "doctor-ensure-cron.log")
    os.makedirs(os.path.dirname(log), exist_ok=True)
    with open(log, "a") as out:
        subprocess.Popen([sys.executable, "-m", "helm", "proxywatch", "--force"],
                         stdout=out, stderr=out, stdin=subprocess.DEVNULL,
                         start_new_session=True)


def _post(text):
    try:
        subprocess.run(["helm", "chat", "post", "--room", "helm"],
                       input=text, text=True, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pass


def sweep(detection, proxies=None, bounce=None, kick_pass=None, post=None):
    """Act on a detection. Only 'resumed' acts; every other state returns
    nothing. Returns the printable lines. Actuators are parameters so the
    arms can prove the decision without a real suspend or a real proxy.

    THE DEFAULTS RESOLVE AT CALL TIME, never at def time: a default bound in
    the signature freezes the function object at import, so a test patching
    the module attribute patches nothing the call can see — the double looks
    installed, the real actuator fires anyway, and the arm measures a live
    proxy bounce in the middle of a suite. Measured on this module's own
    first run: `post` bound at import, the patched double never called, the
    rung reached for a real subprocess inside a mocked Popen."""
    bounce = _bounce if bounce is None else bounce
    kick_pass = _kick_pass if kick_pass is None else kick_pass
    post = _post if post is None else post
    if detection.get("state") != "resumed":
        return []
    gap = detection.get("gap_s") or 0.0
    targets = list(_live_proxies() if proxies is None else proxies)
    bounced, failed = [], []
    for family, seat in targets:
        (bounced if bounce(family, seat) else failed).append(seat)
    kick_pass()
    lines = ["suspend: box resumed after ~%dm asleep — bounced %d proxies%s; "
             "kicked one proxywatch pass"
             % (gap // 60, len(bounced),
                " (%s)" % ", ".join(bounced) if bounced else "")]
    if failed:
        lines.append("suspend: %d proxies did NOT come back cleanly: %s — "
                     "run `helm seat doctor --ensure` again"
                     % (len(failed), ", ".join(failed)))
    post("POST-SUSPEND SWEEP (helm seat doctor --ensure): the box was "
         "suspended ~%dm; bounced %d family proxies%s and kicked a fresh "
         "proxywatch pass so proofs re-mint on live connections. Seats keep "
         "their panes, context and beacons; a request in flight during the "
         "bounce fails once and the client retries. Verdict refusals that read "
         "'proof state is Nm old' clear on that pass. task/2721."
         % (gap // 60, len(bounced), " (%s)" % ", ".join(bounced)
            if bounced else ""))
    return lines


def resume_rung(quiet=False, _now=None):
    """The doctor --ensure rung: detect, sweep, and return lines to print.
    Never raises into the watchdog; never moves its rc.

    `_now` overrides the wall clock the DETECTION reads, for callers whose
    module clock is scripted by tests of an unrelated cadence (seat_health's
    heartbeat). The detection only ever compares differences, so any
    monotonic source is valid."""
    d = detect(now=_now)
    lines = sweep(d)
    if not lines and d["state"] == "unknown":
        # AN UNREADABLE CLOCK IS SAID, ONCE PER PASS, ON EVERY CONTRACT. A
        # watchdog that swallows its own blind spot is the failure mode this
        # module exists to remove, one layer down.
        lines.append("suspend: clock unavailable — resume detection UNKNOWN")
    # A FIRST RUN PRINTS NOTHING, on purpose: it is the steady state for a
    # fresh host (every test fixture is one), the record IS the act, and the
    # byte-exact output contracts the ensure arms pin cannot carry a line
    # that fires on every cold start.
    return lines
