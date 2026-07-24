#!/usr/bin/env python3
"""helm rearm — the land-to-live compression verb.

helm code lands on main; every fresh `helm` invocation is a process off main,
so a CLI-class land is live AT LAND. But LONG-LIVED processes keep executing
the code they loaded at start: an armed `helm chat wait --follow` inbox beacon,
the web service, seat proxies/daemons. Until each re-arms, the fix has not
reached them. `helm rearm` is the CHEAP OWNED pass that closes that gap
minutes after a land batch.

  * dry-run DEFAULT: report which live processes still hold PRE-HEAD code —
    every `helm chat wait` waiter (owning seat + start, STALE if it started
    before main's current HEAD commit time), the web-service unit (active +
    since, stale iff it predates HEAD), and every OTHER long-lived
    helm process predating HEAD as an ADVISORY respawn candidate. Mutates
    nothing.
  * --apply: (1) FIRST post ONE owned ambient ANNOUNCE row to #main (ambient =
    wakes nobody) saying the pass is cycling waiters and why; (2) SIGTERM ONLY
    the stale waiters — each owning agent gets its Monitor-exit notification
    and re-arms on new code at its own turn boundary (the OWNED version of the
    unowned mass-SIGTERM beacon-killer incident class); (3) restart the web
    unit iff it is active AND stale; (4) NEVER touch proxies/daemons/seats —
    advisory only. Idempotent by convergence: staleness is recomputed from live
    state each run, so once the signaled waiters exit a second --apply finds
    nothing stale (a re-signal of a still-dying pid is a harmless no-op).

SAFETY — failed-probe-is-not-absence: only a process whose cmdline argv EXACTLY
matches the waiter shape (a `helm` executable token immediately followed by
`chat wait`) is ever signaled. The bash Monitor wrapper (which carries the same
string INSIDE a single `-c` argument, never as a standalone `helm` token) is
excluded by construction. Ownership is the `--seat` token; a stale waiter with
no readable seat, an unreadable start, or an unreachable HEAD (staleness
unprovable) is SKIPPED and reported, NEVER signaled. The argv shape AND stat
starttime are re-checked immediately before each SIGTERM (who.py's anti-reuse
bracket), so a waiter that exited into a recycled pid during the announce
round-trip is never hit. Reads /proc cmdline + stat only.
"""
import glob
import json
import os
import signal
import subprocess
import sys
import time

from . import chat, pk

PROC = "/proc"          # module-level so tests point it at a fixture tree
WEB_UNIT = "helm-web"   # the systemd --user web-service unit (docs/WEB.md)
ANNOUNCE_ROOM = "main"
ANNOUNCE_NAME = "helm-rearm"
SKEW_S = 2              # whole-second flooring on BOTH /proc/stat btime and git
                        # %ct underestimates a proc's start by up to ~1s, so a
                        # freshly-spawned post-land waiter can compute just under
                        # HEAD. Class STALE only when older than HEAD by this
                        # band => a fresh waiter is never mis-killed (fail-safe).


# ---------------------------------------------------------------------------
# /proc reads (who.py's pattern) + start-epoch conversion
# ---------------------------------------------------------------------------

def _clk_tck():
    try:
        return os.sysconf("SC_CLK_TCK") or 100
    except (ValueError, OSError):
        return 100


def _boot_time():
    """Wall-clock seconds at boot (/proc/stat btime), or None. starttime is
    ticks-since-boot; boot + ticks/tck is the process's CLOCK_REALTIME start,
    the same axis as a git commit's %ct — so the two compare directly."""
    try:
        with open("%s/stat" % PROC) as f:
            for line in f:
                if line.startswith("btime"):
                    return int(line.split()[1])
    except (OSError, IndexError, ValueError):
        return None
    return None


def _stat_field(pid, n, default):
    """Field n of /proc/<pid>/stat COUNTING FROM the state field — comm may
    contain spaces/parens, so parse after the LAST ')' (who.py)."""
    try:
        with open("%s/%d/stat" % (PROC, pid)) as f:
            s = f.read()
        return int(s.rsplit(")", 1)[1].split()[n])
    except (OSError, IndexError, ValueError):
        return default


def starttime_of(pid):
    return _stat_field(pid, 19, float("inf"))


def proc_start_epoch(pid):
    """The process's start as CLOCK_REALTIME epoch, or None when boot time or
    the stat starttime is unreadable (no evidence — the row is never classed
    stale, so it is never signaled)."""
    boot = _boot_time()
    st = starttime_of(pid)
    if boot is None or st == float("inf"):
        return None
    return boot + st / _clk_tck()


def _cmdline(pid):
    """/proc/<pid>/cmdline as an argv list, or None when unreadable. An
    unreadable cmdline is no evidence of the waiter shape — the process is
    simply not a signal candidate."""
    try:
        with open("%s/%d/cmdline" % (PROC, pid), "rb") as f:
            raw = f.read()
    except OSError:
        return None
    argv = [p.decode("utf-8", "replace") for p in raw.split(b"\0") if p]
    return argv or None


def _helm_subargv(argv):
    """The argv AFTER a `helm` invocation marker — a token whose basename is
    `helm` (bin/helm through any symlink), or `-m helm` — else None. This is
    the exact-shape gate: a bash `-c 'helm chat wait …'` wrapper carries the
    text inside ONE argument, has no standalone `helm` token, and returns
    None here — so it is never mistaken for a waiter."""
    for i, tok in enumerate(argv):
        if tok == "-m" and i + 1 < len(argv) and argv[i + 1] == "helm":
            return argv[i + 2:]
        if tok != "-m" and os.path.basename(tok) == "helm":
            return argv[i + 1:]
    return None


def _flag(argv, name):
    """--name VALUE or --name=VALUE from an argv list, else None."""
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(name + "="):
            return a[len(name) + 1:]
    return None


# ---------------------------------------------------------------------------
# HEAD commit time — 'live' code (production runs off main; HEAD IS main's tip)
# ---------------------------------------------------------------------------

def _repo_root():
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        p = subprocess.run(["git", "-C", here, "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout.strip() if p.returncode == 0 else None


def _head_commit():
    """(sha8, commit_epoch) of the helm checkout's HEAD, or (None, None) when
    git is unreachable. Production `helm` runs off the main checkout, so HEAD
    is main's tip. HEAD unresolvable => staleness cannot be asserted =>
    nothing is ever signaled (the fail-safe)."""
    root = _repo_root()
    if not root:
        return None, None
    try:
        p = subprocess.run(
            ["git", "-C", root, "show", "-s", "--format=%h %ct", "HEAD"],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    parts = p.stdout.split() if p.returncode == 0 else []
    if len(parts) < 2:
        return None, None
    try:
        return parts[0], int(parts[1])
    except ValueError:
        return None, None


# ---------------------------------------------------------------------------
# the web-service unit (chatnode._systemctl idiom)
# ---------------------------------------------------------------------------

def _systemctl_show(unit):
    """Unit properties as a dict, or None when systemctl is unavailable. Uses
    --timestamp=unix so ActiveEnterTimestamp is `@<epoch>` (parseable), not a
    locale-timezone string."""
    try:
        p = subprocess.run(
            ["systemctl", "--user", "show", "--timestamp=unix",
             "-p", "LoadState", "-p", "ActiveState", "-p", "MainPID",
             "-p", "ActiveEnterTimestamp", unit],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    d = {}
    for line in p.stdout.splitlines():
        k, sep, v = line.partition("=")
        if sep:
            d[k.strip()] = v.strip()
    return d


def _systemctl_restart(unit):
    """(ok, message) — restart a --user unit, degrading to a loud reason."""
    try:
        p = subprocess.run(["systemctl", "--user", "restart", unit],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, "systemctl unavailable: %s" % exc
    if p.returncode != 0:
        return False, (p.stdout + p.stderr).strip() or "restart failed"
    return True, "restarted"


def _active_enter_epoch(d, mainpid):
    """When the unit became active, as epoch: the @<unix> ActiveEnterTimestamp
    first, falling back to the MainPID's own start epoch (older systemd without
    --timestamp=unix support)."""
    raw = (d or {}).get("ActiveEnterTimestamp", "")
    if raw.startswith("@"):
        try:
            return int(raw[1:])
        except ValueError:
            pass
    return proc_start_epoch(mainpid) if mainpid else None


def _web_state(head):
    d = _systemctl_show(WEB_UNIT)
    if d is None:
        return None
    if d.get("LoadState") in (None, "", "not-found"):
        return {"unit": WEB_UNIT, "loaded": False, "active": False,
                "since": None, "mainpid": None, "stale": False}
    active = d.get("ActiveState") == "active"
    try:
        mainpid = int(d.get("MainPID") or 0) or None
    except ValueError:
        mainpid = None
    since = _active_enter_epoch(d, mainpid)
    stale = bool(active and since and head and since < head)
    return {"unit": WEB_UNIT, "loaded": True, "active": active,
            "since": since, "mainpid": mainpid, "stale": stale}


# ---------------------------------------------------------------------------
# scan (read-only) + apply (the owned mutation)
# ---------------------------------------------------------------------------

def _still_waiter(pid, starttime):
    """Re-validate a scan-time waiter immediately before signaling (who.py's
    anti-reuse bracket): the pid must still carry the SAME stat starttime — the
    canonical anti-recycle key, since a reused pid has a different one — AND the
    exact `chat wait` argv shape. Any mismatch (exited, recycled, or reshaped)
    means it is no longer our waiter, so it is skipped, never signaled. Closes
    the pid-reuse TOCTOU across the announce round-trip: the scan-time guarantee
    that only the waiter shape is ever signaled is re-asserted AT kill time."""
    if starttime_of(pid) != starttime:
        return False
    sub = _helm_subargv(_cmdline(pid) or [])
    return sub is not None and sub[:2] == ["chat", "wait"]


def scan():
    """One read-only pass -> the plan dict: HEAD identity, every waiter
    (status STALE|current|UNKNOWN + signalable), the web unit, and pre-HEAD
    advisory helm processes. Never mutates. The scanning process itself and the
    web MainPID are excluded from the generic process sweep."""
    head_sha, head = _head_commit()
    web = _web_state(head)
    web_pid = web["mainpid"] if web else None
    mypid = os.getpid()
    waiters, advisory = [], []
    for entry in glob.glob(os.path.join(PROC, "[0-9]*")):
        try:
            pid = int(os.path.basename(entry))
        except ValueError:
            continue
        if pid == mypid or pid == web_pid:
            continue
        argv = _cmdline(pid)
        if not argv:
            continue
        sub = _helm_subargv(argv)
        if sub is None:
            continue
        start = proc_start_epoch(pid)
        if sub[:2] == ["chat", "wait"]:
            seat = _flag(sub, "--seat")
            st_ticks = starttime_of(pid)     # raw ticks: the kill-time re-check key
            if start is None or head is None:
                status = "UNKNOWN"       # cannot classify -> never signal
            elif start < head - SKEW_S:
                status = "STALE"
            else:
                status = "current"
            waiters.append({
                "pid": pid, "seat": seat, "start": start,
                "starttime": st_ticks, "status": status,
                "signalable": status == "STALE" and bool(seat),
                "cmdline": " ".join(argv)})
        else:
            verb = sub[0] if sub else "?"
            if start is not None and head is not None and start < head:
                advisory.append({"pid": pid, "verb": verb, "start": start,
                                 "cmdline": " ".join(argv)})
    waiters.sort(key=lambda w: w["pid"])
    advisory.sort(key=lambda a: a["pid"])
    return {"head_sha": head_sha, "head_time": head, "waiters": waiters,
            "web": web, "advisory": advisory}


def _announce_text(head_sha, seats, web_restart):
    bits = []
    if seats:
        bits.append(
            "cycling %d stale inbox waiter%s [%s] — each seat's Monitor exits "
            "and re-arms on the new code at its OWN turn boundary (the owned "
            "beacon-cycle, not an unowned mass-kill)"
            % (len(seats), "s"[:len(seats) != 1], ", ".join(seats)))
    if web_restart:
        bits.append("restarting the stale web service")
    # a resumed/cleared seat that lost its beacon context needs the EXACT
    # re-arm incantation in the row — the monitor-exit alone does not carry it.
    rearm = (" If your beacon was cycled and you resumed/cleared without it, "
             "re-arm: Monitor(command: \"helm chat wait --seat <your-seat> "
             "--follow\", persistent: true) — if Monitor is DEFERRED, "
             "ToolSearch(query: \"select:Monitor\") first." if seats else "")
    return ("helm rearm @ HEAD %s: %s.%s Proxies, daemons and seats are "
            "untouched (advisory only)." % (head_sha or "?", "; ".join(bits),
                                            rearm))


def apply(plan):
    """Execute the owned pass over `plan`. ORDER IS LOAD-BEARING: announce
    FIRST (ambient — wakes nobody), THEN SIGTERM only signalable-stale waiters,
    THEN restart the web unit iff active+stale. Advisory/web-nonstale/uncertain
    are never touched. No stale target => no-op (nothing announced, idempotent).
    Two fail-safes at the kill boundary: (a) fail-CLOSED — if the announce did
    NOT land, the disruptive re-arm is exactly the unexplained mass-SIGTERM this
    verb exists to replace, so signal/restart NOTHING and surface the reason
    loudly; (b) each pid is re-validated (starttime + argv shape) right before
    os.kill, so a waiter that exited into a recycled pid during the announce
    round-trip is skipped, not killed."""
    stale_waiters = [w for w in plan["waiters"] if w["signalable"]]
    web = plan.get("web")
    web_restart = bool(web and web["stale"])
    actions = {"announced": False, "signaled": [], "failed": [], "skipped": [],
               "web_restarted": False, "web_msg": None}
    if not stale_waiters and not web_restart:
        return actions
    txt = _announce_text(plan.get("head_sha"),
                         [w["seat"] for w in stale_waiters], web_restart)
    try:
        chat.post(txt, room=ANNOUNCE_ROOM, who=ANNOUNCE_NAME, ambient=True)
        actions["announced"] = True
    except Exception as exc:
        # fail-CLOSED: announce-first is load-bearing. A chat-node hiccup must
        # not degrade into the unexplained mass-SIGTERM the verb replaces — so
        # signal and restart nothing this pass; the owners still re-arm via the
        # independent Monitor-exit path, and the next --apply retries the announce.
        actions["announce_error"] = str(exc)
        pk.event("rearm", "waiters", "ABORTED: announce failed (%s)" % exc)
        return actions
    for w in stale_waiters:
        if not _still_waiter(w["pid"], w["starttime"]):
            actions["skipped"].append(w["pid"])   # exited/recycled since scan
            continue
        try:
            os.kill(w["pid"], signal.SIGTERM)
            actions["signaled"].append(w["pid"])
        except OSError as exc:
            actions["failed"].append([w["pid"], str(exc)])
    if web_restart:
        ok, msg = _systemctl_restart(web["unit"])
        actions["web_restarted"], actions["web_msg"] = ok, msg
    summary = "signaled %d stale waiter%s%s%s" % (
        len(actions["signaled"]), "s"[:len(actions["signaled"]) != 1],
        "; %d skipped (pid reuse/exit)" % len(actions["skipped"])
        if actions["skipped"] else "",
        "; web restarted" if actions["web_restarted"] else "")
    pk.event("rearm", "waiters", summary)
    return actions


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _age(epoch):
    if not epoch:
        return "?"
    d = time.time() - epoch
    if d < 90:
        return "%ds ago" % int(d)
    if d < 5400:
        return "%dm ago" % int(d / 60)
    if d < 129600:
        return "%dh ago" % int(d / 3600)
    return "%dd ago" % int(d / 86400)


def _web_word(web):
    if not web or not web["loaded"]:
        return "n/a"
    if web["stale"]:
        return "stale"
    return "current" if web["active"] else "inactive"


def _print_report(plan, actions, applying):
    head_sha, head_t = plan["head_sha"], plan["head_time"]
    mode = ("APPLYING" if applying
            else "dry-run; `helm rearm --apply` cycles the stale waiters")
    print("helm rearm — land-to-live: live processes still holding pre-HEAD "
          "code (%s)" % mode)
    if head_t:
        print("  HEAD %s committed %s" % (head_sha, _age(head_t)))
    else:
        print("  HEAD unknown (git unreachable) — nothing can be classed "
              "stale; nothing is signaled")
    waiters = plan["waiters"]
    signaled = set(actions["signaled"]) if actions else set()
    skipped = set(actions.get("skipped", [])) if actions else set()
    aborted = bool(actions and actions.get("announce_error"))
    print("  waiters (helm chat wait): %s"
          % ("" if waiters else "none live"))
    for w in waiters:
        note = ""
        if w["status"] == "STALE" and not w["seat"]:
            note = "  (no --seat — SKIPPED, never signaled)"
        elif w["status"] == "UNKNOWN":
            note = "  (start/HEAD unreadable — SKIPPED)"
        elif w["pid"] in signaled:
            note = "  -> SIGTERM sent (re-arms at its owner's next turn)"
        elif w["pid"] in skipped:
            note = "  -> skipped (exited/recycled before signal — fail-safe)"
        elif applying and aborted and w["signalable"]:
            note = "  -> NOT signaled (announce failed — fail-closed)"
        elif applying and w["signalable"]:
            note = "  -> signal FAILED"
        print("    %-7s pid %-8d seat %-22s started %s%s" % (
            w["status"], w["pid"], w["seat"] or "?", _age(w["start"]), note))
    web = plan["web"]
    if web is None:
        print("  web-service: systemctl --user unavailable (unqueried)")
    elif not web["loaded"]:
        print("  web-service: %s not loaded" % web["unit"])
    else:
        line = "  web-service: %s %s since %s" % (
            web["unit"], _web_word(web), _age(web["since"]))
        if actions and actions["web_restarted"]:
            line += "  -> restarted"
        elif web["stale"]:
            line += "  [--apply restarts]"
        print(line)
    adv = plan["advisory"]
    if adv:
        print("  advisory (pre-HEAD long-lived helm procs — respawn "
              "candidates, NEVER signaled):")
        for a in adv:
            print("    pid %-8d helm %-14s started %s" % (
                a["pid"], a["verb"], _age(a["start"])))
        # a land never self-propagates to a running proxy — each carries
        # pre-HEAD config until respawned by its owner (per-instance + family
        # cli-proxies too).
        print("    respawn recipe: `helm seat down <seat> && helm seat up "
              "<seat>` (or the daemon's own restart) — a land never "
              "self-propagates to a running proxy")
    n_stale = sum(1 for w in waiters if w["status"] == "STALE")
    tail = "%d waiter%s stale, web %s, %d advisory" % (
        n_stale, "s"[:n_stale != 1], _web_word(web), len(adv))
    if applying and actions:
        if actions["announced"]:
            print("  announced to #%s (ambient — woke nobody)" % ANNOUNCE_ROOM)
        elif actions.get("announce_error"):
            print("  ANNOUNCE FAILED (%s) — fail-closed: nothing signaled or "
                  "restarted this pass" % actions["announce_error"])
        print("helm rearm: %s" % tail)
    else:
        print("helm rearm: %s" % tail)


def cmd_rearm(args):
    """rearm [--apply] [--json] — land-to-live: report (dry-run DEFAULT) which
    long-lived processes still hold pre-HEAD code; --apply announces (ambient),
    SIGTERMs only the stale `helm chat wait` waiters so their owners re-arm on
    new code at their own turn boundary, and restarts a stale web unit. Proxies,
    daemons and seats are advisory only, never signaled."""
    args = list(args)
    bad = [a for a in args if a not in ("--apply", "--json")]
    if bad:
        print("usage: helm rearm [--apply] [--json]", file=sys.stderr)
        return 2
    applying = "--apply" in args
    plan = scan()
    actions = apply(plan) if applying else None
    if "--json" in args:
        out = dict(plan)
        if actions is not None:
            out["actions"] = actions
        print(json.dumps(out, indent=2))
        return 0
    _print_report(plan, actions, applying)
    return 0
