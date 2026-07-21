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


def cmd_gc(args):
    """gc [--dry | --apply] — enforce the declared retention budgets over the
    derived exhaust. Dry-run default: report what WOULD be reaped, reap
    NOTHING. --apply rotates/prunes exhaust streams only; source/archive/
    state streams are surfaced loudly, never touched. Always rc 0 — gc is a
    janitor, not a gate."""
    args = list(args)
    bad = [a for a in args if a not in ("--dry", "--apply")]
    if bad or ("--dry" in args and "--apply" in args):
        print("usage: helm gc [--dry | --apply]", file=sys.stderr)
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
