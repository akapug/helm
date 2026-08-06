#!/usr/bin/env python3
"""The retention policy plane + `helm gc` — declared budgets for the exhaust.

Every derived stream helm emits (ledgers, rotated generations, per-session
state, caches, backups) declares a retention budget (size/age/count) in ONE
policy table; `helm gc` reports over-budget (dry-run DEFAULT), `--apply`
enforces. The ancestor learned this at 32MB of guard-ledger — the table is
memGC's declared-budget half, the sweep is its enforcement half. HARD LAWS:

  * AUTHORED content is out of reach BY CLASS: only class `exhaust` is ever
    reaped. `source` (attest-queue: pending attestations), `archive` (drain
    archives + skills-trash: the ONLY copy of authored bytes), and `state`
    (live diff baselines) get loud report rows, never an action — the gate is
    structural in _reapable(), not a per-row opt-out.
  * Reaping is rotate (one atomic os.replace to a dated sibling — the rename
    IS the write-archive-first step; the writer recreates the live file on
    its next append) or prune (stale derived files/dirs whose own modules
    already treat them as lossy — inject-seen's SEEN_TTL delete, the .1
    one-generation clobber). Never a partial rewrite of live bytes.
  * FAIL-OPEN per stream: one unreadable stream reports an error row and the
    sweep continues; gc trouble never blocks anything (rc 0).
  * Age reads the basename stamp first (%Y%m%dT%H%M%S… — move/copy2 carries
    the ORIGIN's mtime, the wrong axis for backups), else newest mtime within
    (an active session dir stays fresh while any file in it moves).
"""
import calendar
import os
import re
import shutil
import sys
import time

from . import home, inject, pk

DAY = 86400
MB = 1024 * 1024
GEN_RE = re.compile(r"\.jsonl\.(1|\d{8}T\d{6}Z?)$")   # .1 + gc's dated rotations
STAMP_RE = re.compile(r"^(\d{8}T\d{6})")              # backup/trash birth stamps


def _state(*parts):
    return os.path.join(home.global_dir(), ".state", *parts)


def _cache(*parts):
    base = home.env("CACHE_DIR") or os.path.join(
        os.path.expanduser("~"), ".cache", "helm")
    return os.path.join(base, *parts)


def _one(path):
    return [path] if os.path.exists(path) else []


def _kids(d, match=None):
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return []
    return [os.path.join(d, n) for n in names if match is None or match(n)]


def _generations():
    """Every rotated generation in the two derived homes — the modules' own
    one-shot .1 files plus gc's dated rotations. Data-driven: any new
    self-rotating jsonl in .state/ or the cache is covered unnamed."""
    return [p for d in (_state(), _cache())
            for p in _kids(d, lambda n: bool(GEN_RE.search(n)))]


def _drains():
    from . import store
    return _kids(os.path.join(store.adopted_dir(), "archive"),
                 lambda n: n.startswith("drain-"))


def _lane_orphans():
    """Lease-less lane worktrees (work.py's join, fail-open []) — surfaced
    here so `helm gc` reports them; `helm work gc --apply` is the actuator."""
    from . import work
    return work.gc_orphans()


def _dead_cursors():
    """Per-session chat read cursors whose session is dead (chat.dead_cursors).
    Liveness-gated, never age-gated: a pane thinking for an hour looks exactly
    like one that exited an hour ago, and dropping a LIVE cursor makes that seat
    re-read its room and re-deliver what it already saw. Budget is count>0
    because a dead session's cursor has no retention value at all — it is pure
    directory-entry tax on every list_rooms, which runs on every tool boundary.

    RAISES when liveness is unprovable, and that is the whole contract. A
    cross-family review caught an earlier draft returning `[] if err else
    victims` under a bare `except Exception: return []` — so BOTH "I could not
    prove any session dead" and "I crashed" arrived at gc as an empty victim
    list, which gc reads as "nothing to prune" and prints in its `in budget`
    line. chat.dead_cursors returns `err` precisely so an unprovable liveness
    refuses to act; laundering that refusal into a clean report is the exact
    class this reaper was built to avoid, one layer up.

    So there is no local try/except: scan() already wraps every find() and puts
    the message in row["error"], _reapable() refuses any row carrying one, and
    cmd_gc prints it as a loud ERR line excluded from `in budget`. The honest
    channel existed the whole time; the handler's own error handling was strictly
    worse than the framework's, and deleting it IS the fix.
    """
    from . import chat
    victims, _kept, err = chat.dead_cursors()
    if err:
        raise RuntimeError("cursor liveness unprovable, reaped nothing: %s" % err)
    return victims


def _size(p):
    """Bytes at p, dirs walked. Fail-open 0."""
    try:
        if not os.path.isdir(p):
            return os.path.getsize(p)
        total = 0
        for root, _dirs, files in os.walk(p):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        return total
    except OSError:
        return 0


def _born(p):
    """A path's effective timestamp: basename stamp first, else newest mtime
    within. Fail-open: unreadable reads as NOW — never stale."""
    m = STAMP_RE.match(os.path.basename(p))
    if m:
        try:
            return calendar.timegm(time.strptime(m.group(1), "%Y%m%dT%H%M%S"))
        except ValueError:
            pass
    try:
        newest = os.path.getmtime(p)
        for root, _dirs, files in os.walk(p):
            for f in files:
                try:
                    newest = max(newest, os.path.getmtime(os.path.join(root, f)))
                except OSError:
                    pass
        return newest
    except OSError:
        return time.time()


def _human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d%s" if unit == "B" else "%.1f%s") % (n, unit)
        n /= 1024.0


# The policy table — the POLICY plane itself. One row per stream: class
# (exhaust reapable | source/archive/state report-only), ONE budget axis
# (size bytes | age days | count newest-kept), action, and a finder.
# Self-rotating ledgers budget at their module cap + 1MB slack, so gc firing
# means the module's own rotation broke; the cache jsonls have NO rotation of
# their own — gc is their only cap (the exact 32MB-guard-ledger gap).
POLICIES = (
    {"stream": "chat-cursors", "cls": "exhaust", "act": "prune", "count": 0,
     "find": _dead_cursors},
    {"stream": "inject-ledger", "cls": "exhaust", "act": "rotate",
     "size": inject.LEDGER_MAX + MB,
     "find": lambda: _one(_state("inject-ledger.jsonl"))},
    {"stream": "events", "cls": "exhaust", "act": "rotate",
     "size": pk.EVENTS_MAX + MB,
     "find": lambda: _one(_state("events.jsonl"))},
    {"stream": "keepalive-log", "cls": "exhaust", "act": "rotate", "size": 5 * MB,
     "find": lambda: _one(_cache("keepalive-log.jsonl"))},
    {"stream": "native-usage-history", "cls": "exhaust", "act": "rotate",
     "size": 5 * MB,
     "find": lambda: _one(_cache("native-usage-history.jsonl"))},
    {"stream": "mints", "cls": "exhaust", "act": "rotate", "size": MB,
     "find": lambda: _one(_cache("mints.jsonl"))},
    {"stream": "rotated-generations", "cls": "exhaust", "act": "prune", "age": 30,
     "find": _generations},
    {"stream": "inject-seen", "cls": "exhaust", "act": "prune",
     "age": inject.SEEN_TTL // DAY,
     "find": lambda: _kids(_state("inject-seen"))},
    {"stream": "reflex-state", "cls": "exhaust", "act": "prune", "age": 30,
     "find": lambda: _kids(_state("reflex-state"))},
    {"stream": "store-cache", "cls": "exhaust", "act": "prune", "age": 30,
     "find": lambda: _kids(_cache(), lambda n: n.startswith("store-cache-"))},
    {"stream": "config-backups", "cls": "exhaust", "act": "prune", "age": 30,
     "find": lambda: _kids(_cache("config-backups"))},
    {"stream": "attest-queue", "cls": "source", "act": "report", "size": 256 * 1024,
     "find": lambda: _one(_state("attest-queue.jsonl")),
     "note": "pending attestations — helm premise --retry-queue drains it"},
    {"stream": "coinages", "cls": "state", "act": "report", "size": 256 * 1024,
     "find": lambda: _one(_state("coinages.json")),
     "note": "self-bounding (COINAGE_CAP) — over budget means the cap broke"},
    {"stream": "drift-snapshots", "cls": "state", "act": "report", "count": 64,
     "find": lambda: _kids(_state(), lambda n: n.startswith("drift-snapshot")),
     "note": "live diff baselines, one per scope"},
    {"stream": "work-worktrees", "cls": "state", "act": "report", "count": 0,
     "find": _lane_orphans,
     "note": "lease-less lane rooms — `helm work gc --apply` sweeps "
             "(rescue-commits dirty work to its branch first; never discards)"},
    {"stream": "skills-trash", "cls": "archive", "act": "report", "age": 30,
     "find": lambda: _kids(_cache("skills-trash")),
     "note": "only copy of trashed skills (archive-not-delete law) — owner reaps"},
    {"stream": "drain-archives", "cls": "archive", "act": "report", "age": 90,
     "find": _drains,
     "note": "authored raw originals — owner reaps, never gc"},
)


def _reapable(row):
    """The constitutional gate: gc may touch class `exhaust` and nothing else."""
    return row["over"] and row["cls"] == "exhaust" and not row.get("error")


def scan(now=None):
    """The table measured -> one row per stream: {stream, cls, act, budget,
    used, over, victims, bytes} (+note, +error). Report-stream victims are
    display-only — apply gates on _reapable(). Fail-open per stream."""
    now = time.time() if now is None else now
    rows = []
    for p in POLICIES:
        row = {"stream": p["stream"], "cls": p["cls"], "act": p["act"],
               "note": p.get("note", ""), "budget": "", "used": "",
               "victims": [], "bytes": 0, "over": False}
        try:
            found = p["find"]()
            if "size" in p:
                total = sum(_size(f) for f in found)
                row["budget"] = "size>" + _human(p["size"])
                row["used"] = _human(total)
                if total > p["size"]:
                    row.update(over=True, victims=list(found), bytes=total)
            elif "age" in p:
                cut = now - p["age"] * DAY
                stale = [f for f in found if _born(f) < cut]
                row["budget"] = "age>%dd" % p["age"]
                row["used"] = ("%d stale of %d (%s)" % (
                    len(stale), len(found), _human(sum(_size(f) for f in stale)))
                    if stale else "%d fresh" % len(found))
                if stale:
                    row.update(over=True, victims=stale,
                               bytes=sum(_size(f) for f in stale))
            else:
                aged = sorted(found, key=_born, reverse=True)
                extra = aged[p["count"]:]
                row["budget"] = "count>%d" % p["count"]
                row["used"] = str(len(found))
                if extra:
                    row.update(over=True, victims=extra,
                               bytes=sum(_size(f) for f in extra))
        except Exception as exc:
            row["error"] = str(exc)
        rows.append(row)
    return rows


def _reap(row):
    """Enforce ONE reapable row -> ([lines], bytes_reaped). Per-victim
    fail-open: a locked victim is SKIPPED loudly, the rest still reap."""
    lines, reaped = [], 0
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    for v in row["victims"]:
        try:
            n = _size(v)
            if row["act"] == "rotate":
                os.replace(v, v + "." + stamp)  # atomic: the rename IS the archive
                lines.append("rotated %s -> %s" % (v, os.path.basename(v) + "." + stamp))
            elif os.path.isdir(v):
                shutil.rmtree(v)
                lines.append("pruned " + v + "/")
            else:
                os.remove(v)
                lines.append("pruned " + v)
            reaped += n
        except OSError as exc:
            lines.append("SKIPPED %s (%s)" % (v, exc))
    return lines, reaped


_SERVICE = """[Unit]
Description=helm gc — enforce declared retention over the derived exhaust
Documentation=premise:nothing-may-gate-on-an-undrained-pile

[Service]
WorkingDirectory=%(cwd)s
Type=oneshot
# THE DRAIN THAT EXISTED AND WAS NEVER SCHEDULED. gc has always known its own
# per-stream retention policy; nothing ever ran it, so the exhaust grew without
# bound — measured at 11,192 items over budget (11,191 chat cursors
# plus a 31.5MB usage-history file against a 5MB budget) on a fleet whose work
# actuator was ALSO gated on an undrained pile. A policy with no scheduler is a
# policy that does not exist.
# --apply is safe by construction: each stream reaps only what its own declared
# budget names, and non-exhaust streams stay report-only (a dirty lane is never
# discarded here; `helm work gc --apply` rescue-commits first).
ExecStart=%(helm)s gc --apply
# THE SIDEBAR RECURRENCE, KILLED AT THE CADENCE (a repeated owner ask):
# every claim/peek/fold mints a worktree, and with no scheduled reap a
# metaharness sidebar accretes one ghost project per room forever — the owner
# once hand-swept ~90 of them. work gc --apply retires clean LANDED
# rooms, prunes phantom records, and TTL-drops idle peeks; every live-room
# refusal (lease, cwd occupant, bound pane, dirty tree) keeps its room.
ExecStart=%(helm)s work gc --apply --repo %(cwd)s
Nice=15
"""

_TIMER = """[Unit]
Description=periodic helm gc (keeps every declared-retention stream in budget)

[Timer]
OnBootSec=5min
OnUnitActiveSec=%(interval)ds
Persistent=true

[Install]
WantedBy=timers.target
"""

TIMER_INTERVAL_S = 3600


def timer_units(interval=TIMER_INTERVAL_S):
    """(service_path, service_text, timer_path, timer_text). WorkingDirectory is
    DERIVED, never a literal — an operator path baked into a tracked template is
    both a never-track needle and a machine identity this repo cannot carry.
    work.find_root folds a lane worktree back to the SHARED checkout so a
    persistent unit never captures a disposable worktree as its cwd."""
    helm_bin = os.path.join(os.path.expanduser("~"), ".local", "bin", "helm")
    udir = os.path.join(os.path.expanduser("~"), ".config", "systemd", "user")
    from . import work
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cwd = work.find_root(here) or here
    return (os.path.join(udir, "helm-gc.service"),
            _SERVICE % {"helm": helm_bin, "cwd": cwd},
            os.path.join(udir, "helm-gc.timer"),
            _TIMER % {"interval": interval})


def ensure_timer(interval=TIMER_INTERVAL_S):
    """(ok, detail) — install + enable the cadence. Mirrors proxywatch's
    installer exactly, because the drain deserves the same shipping path the
    watchers already have."""
    import shutil
    import subprocess
    from . import pk
    if interval < 1:
        return False, "interval must be at least 1 second"
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return False, ("systemctl unavailable; run `helm gc --apply` from "
                       "another scheduler (cron, a supervisor) — the drain "
                       "matters more than the mechanism")
    spath, service, tpath, timer = timer_units(interval)
    try:
        pk.atomic_write(spath, service)
        pk.atomic_write(tpath, timer)
    except OSError as e:
        return False, "unit write failed: %s" % e
    for cmd in ([systemctl, "--user", "daemon-reload"],
                [systemctl, "--user", "enable", "--now", "helm-gc.timer"]):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            return False, "%s failed: %s" % (
                " ".join(cmd), (r.stderr or r.stdout or "").strip()[:200])
    return True, "timer enabled every %ds (%s)" % (interval, tpath)


def cmd_gc(args):
    """gc [--dry | --apply] [--install-timer] — enforce the declared retention
    budgets over the derived exhaust. Dry-run default: report what WOULD be
    reaped, reap NOTHING. --apply rotates/prunes exhaust streams only; source/
    archive/state streams are surfaced loudly, never touched. --install-timer
    wires the hourly cadence, because a retention policy nothing SCHEDULES is a
    policy that does not exist (measured: 11,192 items over budget on a gc that
    had never once run). Always rc 0 — gc is a janitor, not a gate."""
    args = list(args)
    if "--install-timer" in args:
        ok, detail = ensure_timer()
        print("helm gc: %s" % detail, file=sys.stderr if not ok else sys.stdout)
        return 0 if ok else 1
    bad = [a for a in args if a not in ("--dry", "--apply")]
    if bad or ("--dry" in args and "--apply" in args):
        print("usage: helm gc [--dry | --apply] [--install-timer]",
              file=sys.stderr)
        return 2
    enforcing = "--apply" in args
    rows = scan()
    print("helm gc — declared retention over the derived exhaust ("
          + ("APPLYING" if enforcing else "dry-run; `helm gc --apply` enforces") + ")")
    loud = [r for r in rows if r["over"] or r.get("error")]
    w = max([len(r["stream"]) for r in loud] or [0])
    reaped_items = reaped_bytes = reaped_streams = 0
    report_over = 0
    for r in loud:
        if r.get("error"):
            print("  ERR   %s  %s" % (r["stream"].ljust(w), r["error"]))
            continue
        head = "  OVER  %s  %-7s %-11s %s" % (
            r["stream"].ljust(w), r["cls"], r["budget"], r["used"])
        if not _reapable(r):
            report_over += 1
            print(head + "  report-only: " + r["note"])
            continue
        if not enforcing:
            print(head + "  would %s %d item%s" % (
                r["act"], len(r["victims"]), "s"[:len(r["victims"]) != 1]))
            continue
        print(head)
        lines, n = _reap(r)
        for ln in lines:
            print("        " + ln)
        reaped_items += len(r["victims"])
        reaped_bytes += n
        reaped_streams += 1
    fine = [r for r in rows if not r["over"] and not r.get("error")]
    if fine:
        print("  in budget: %s (%d/%d streams)" % (
            ", ".join(r["stream"] for r in fine), len(fine), len(rows)))
    would = sum(len(r["victims"]) for r in loud if _reapable(r) and not enforcing)
    if enforcing and reaped_streams:
        summary = "reaped %d item%s (%s) across %d stream%s" % (
            reaped_items, "s"[:reaped_items != 1], _human(reaped_bytes),
            reaped_streams, "s"[:reaped_streams != 1])
        pk.event("gc", "exhaust", summary)
        print("helm gc: " + summary
              + ("; %d report-only over budget" % report_over if report_over else ""))
    elif would or report_over:
        print("helm gc: %d item%s would be reaped; %d report-only over budget "
              "— nothing touched" % (would, "s"[:would != 1], report_over))
    else:
        print("helm gc: every stream in budget (%d declared) — nothing to reap"
              % len(rows))
    return 0
