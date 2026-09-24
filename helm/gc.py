#!/usr/bin/env python3
"""The retention policy plane + `helm gc` — declared budgets for the exhaust.

Every derived stream helm emits (ledgers, rotated generations, per-session
state, caches, backups) declares a retention budget (size/age/count) in ONE
policy table; `helm gc` reports over-budget (dry-run DEFAULT), while `--apply`
enforces only derived exhaust whose owner exposes a generation-bound actuator.
The ancestor learned this at 32MB of guard-ledger — the table is memGC's
declared-budget half, the sweep its enforcement half. HARD LAWS:

  * AUTHORED content is out of reach BY CLASS. `source` (attest-queue: pending
    attestations), `archive` (drain archives + skills-trash: the ONLY copy of
    authored bytes), and `state` (live generations and diff baselines) get loud
    report rows, never an action.
  * A scanned pathname is not a scanned generation. Every owner-bound candidate
    carries its scan-time inode identity; mutation rechecks it under the owner's
    stable lock, or against an immutable owner-specific generation name, before
    touching the path.
  * FAIL-OPEN per stream: one unreadable stream reports an error row and the
    sweep continues; gc trouble never blocks anything (rc 0).
  * Age reads the basename stamp first (%Y%m%dT%H%M%S… — move/copy2 carries
    the ORIGIN's mtime, the wrong axis for backups), else newest mtime within
    (an active session dir stays fresh while any file in it moves).
"""
import calendar
import fcntl
import os
import re
import shutil
import stat
import sys
import time

from . import gitfacts, home, inject, pk

DAY = 86400
MB = 1024 * 1024
GEN_RE = re.compile(r"\.jsonl\.(1|\d{8}T\d{6}Z?)$")   # all generations
DATED_GEN_RE = re.compile(r"\.jsonl\.(\d{8}T\d{6})Z?$")  # immutable gc archives
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
    """Declared children: absence is empty; unreadable is the caller's error."""
    try:
        names = sorted(os.listdir(d))
    except FileNotFoundError:
        return []
    return [os.path.join(d, n) for n in names if match is None or match(n)]


def _generations(match=GEN_RE):
    """Matching generations in both derived homes, discovered data-first."""
    return [p for d in (_state(), _cache())
            for p in _kids(d, lambda n: bool(match.search(n)))]


def _keepalive_path():
    from . import keepalive
    return keepalive._log_path()


def _keepalive_generations():
    """Immutable dated generations owned by keepalive, never another writer."""
    live = _keepalive_path()
    pattern = re.compile(re.escape(os.path.basename(live))
                         + r"\.\d{8}T\d{6}Z?\Z")
    return _kids(os.path.dirname(live), lambda name: bool(pattern.match(name)))


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

    RAISES when liveness is unprovable, and that is the whole contract. Found by
    a cross-family review on r1, which returned `[] if err else victims` under a
    bare `except Exception: return []` — so BOTH "I could not prove any session
    dead" and "I crashed" arrived at gc as an empty victim list, which gc reads
    as "nothing to prune" and prints in its `in budget` line. chat.dead_cursors
    returns `err` precisely so an unprovable liveness refuses to act; laundering
    that refusal into a clean report is the exact class this reaper was built to
    avoid, one layer above where I was looking.

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


def _cursor_sibling_locks():
    """Every `<cursor>.lock` sibling nobody holds (chatdebris.sibling_locks).

    ORPHAN OR NOT, and that is the change. This row once took only a lock
    whose cursor was gone, because a present cursor's sibling was the flock
    handle `_cursor_locks` held for it. Since d047d93d5eb that stack takes
    one lock per ROOM and never opens a sibling, so a present cursor's lock is
    as dead as an orphan -- and it was the bulk of the directory: 49,718
    siblings on the live bus against 468 rooms, of which the orphan rule could
    select 6. Every `list_rooms` on every tool call read all of them.

    OWNER-BOUND, AND THE LOCK PROTOCOL IS THE POINT: `lock` resolves to the
    VICTIM ITSELF, because for this one stream the victim IS a lock file.
    `_reap` therefore opens it without creating it, takes
    `flock(LOCK_EX|LOCK_NB)` on it and holds it across `_still_victim` and
    the unlink, so a lock anybody holds fails the probe and is reported
    SKIPPED rather than deleted -- a process still running older code keeps
    its handle. The finder has already dropped every inode in /proc/locks;
    the probe is what covers the instant between the scan and the unlink.

    `still` is the per-path re-proof: the finder is a whole-directory
    listing, and asking it again for every victim made the reap quadratic in
    the directory it exists to shrink.
    """
    from . import chatdebris
    return chatdebris.sibling_locks()


def _cursor_sibling_lock(path):
    """The one-path re-proof for `chat-cursor-locks` (see `still` above)."""
    from . import chatdebris
    return chatdebris.is_sibling_lock(path)


def _unpaired_session_cursors():
    """Cursors of a live session its seat no longer runs
    (chatdebris.unpaired_session_cursors). The `chat-cursors` row reaps a
    DEAD session's cursors; this one reaps the cursors a session left under a
    seat it has moved away from, which live for as long as the session does.
    Raises, like `_dead_cursors`, when liveness or the roster is unprovable:
    scan() turns that into an ERR row, never an empty stream."""
    from . import chatdebris
    return chatdebris.unpaired_session_cursors()


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
    name = os.path.basename(p)
    m = STAMP_RE.match(name) or DATED_GEN_RE.search(name)
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


def _identity(path):
    """Stable pathname claim: exact inode plus its file kind."""
    s = os.stat(path, follow_symlinks=False)
    return s.st_dev, s.st_ino, stat.S_IFMT(s.st_mode)


def _measure_size(found, policy, _now):
    total = sum(_size(path) for path in found)
    victims = list(found) if total > policy["size"] else []
    return "size>" + _human(policy["size"]), _human(total), victims, total


def _measure_age(found, policy, now):
    victims = [path for path in found
               if _born(path) < now - policy["age"] * DAY]
    size = sum(_size(path) for path in victims)
    used = "%d stale of %d (%s)" % (len(victims), len(found), _human(size)) \
        if victims else "%d fresh" % len(found)
    return "age>%dd" % policy["age"], used, victims, size


def _measure_count(found, policy, _now):
    victims = list(found) if policy["count"] == 0 else \
        sorted(found, key=_born, reverse=True)[policy["count"]:]
    return "count>%d" % policy["count"], str(len(found)), victims, \
        sum(_size(path) for path in victims)


_BUDGET_AXES = {
    "size": _measure_size,
    "age": _measure_age,
    "count": _measure_count,
}


# The policy table — the POLICY plane itself. One row per stream: class,
# ONE budget axis (size bytes | age days | count newest-kept), action, and a
# finder. Owner-bound mutation declares its protocol explicitly: a shared lock
# for live/mutable paths or an immutable owner-specific name.
POLICIES = (
    {"stream": "chat-cursors", "cls": "exhaust", "act": "prune", "count": 0,
     "find": _dead_cursors},
    {"stream": "chat-unpaired-cursors", "cls": "exhaust", "act": "prune",
     "count": 0, "find": _unpaired_session_cursors},
    {"stream": "chat-cursor-locks", "cls": "exhaust", "act": "prune", "count": 0,
     "find": _cursor_sibling_locks, "still": _cursor_sibling_lock,
     "owner": "seats-cursor", "lock": lambda path: path,
     "note": "per-cursor sibling flocks nothing has opened since the stack "
             "took one lock per room — the victim IS the lock, so reap "
             "probes flock(LOCK_NB) on it and skips any held"},
    {"stream": "inject-ledger", "cls": "exhaust", "act": "rotate",
     "size": inject.LEDGER_MAX + MB,
     "find": lambda: _one(_state("inject-ledger.jsonl"))},
    {"stream": "events", "cls": "state", "act": "report",
     "size": pk.EVENTS_MAX + MB,
     "find": lambda: _one(_state("events.jsonl")),
     "note": "live writer generation — owner rotation actuator required"},
    {"stream": "keepalive-log", "cls": "exhaust", "act": "rotate",
     "size": 5 * MB, "find": lambda: _one(_keepalive_path()),
     "owner": "keepalive", "lock": lambda path: path + ".lock"},
    {"stream": "native-usage-history", "cls": "state", "act": "report",
     "size": 5 * MB,
     "find": lambda: _one(_cache("native-usage-history.jsonl")),
     "note": "live writer generation — owner rotation actuator required"},
    {"stream": "mints", "cls": "state", "act": "report", "size": MB,
     "find": lambda: _one(_cache("mints.jsonl")),
     "note": "live writer generation — owner rotation actuator required"},
    {"stream": "rotated-generations", "cls": "exhaust", "act": "prune",
     "age": 30, "find": _keepalive_generations,
     "owner": "keepalive", "immutable": True},
    {"stream": "inject-seen", "cls": "exhaust", "act": "prune",
     "age": inject.SEEN_TTL // DAY,
     "find": lambda: _kids(_state("inject-seen"))},
    {"stream": "reflex-state", "cls": "state", "act": "report", "age": 30,
     "find": lambda: _kids(_state("reflex-state")),
     "note": "mixed canonical todos + promoted-trash/residual authored bytes "
             "+ derived recorder state — owner must separate exhaust first"},
    {"stream": "store-cache", "cls": "exhaust", "act": "prune", "age": 30,
     "find": lambda: _kids(
         _cache(), lambda n: n.startswith("store-cache-") and n.endswith(".json")),
     "owner": "inject-entries", "lock": lambda path: path + ".lock"},
    # THE CAPACITY BOUND, AND DELIBERATELY NOT AN AGE ONE. Every entry in this
    # table is keyed by full object ids, so its answer is a function of fixed
    # content and cannot expire; an age budget here would re-import the
    # invalidation problem the store was built to avoid and would make its own
    # docstring false. COUNT rather than size is the axis because the cost is
    # the BLOCK, not the bytes: measured on this box, 738 entries averaging
    # 432B of content occupied 2.9MB — 4KB apiece — so files are what the disk
    # actually charges for, and the size axis reaps the WHOLE stream at once,
    # which would throw away the live generation on every pass. A pruned entry
    # costs one git spawn to re-derive, which is what the uncached world
    # costs, so this row is the only one in the table whose action cannot be
    # wrong — only more or less expensive.
    {"stream": "gitfacts", "cls": "exhaust", "act": "prune",
     "count": gitfacts.MAX_ENTRIES, "find": gitfacts.entry_paths},
    {"stream": "config-backups", "cls": "archive", "act": "report", "age": 30,
     "find": lambda: _kids(_cache("config-backups")),
     "note": "authored-byte undo snapshots — owner reaps, never gc"},
    {"stream": "attest-queue", "cls": "source", "act": "report", "size": 256 * 1024,
     "find": lambda: _one(_state("attest-queue.jsonl")),
     "note": "pending attestations — helm premise --retry-queue drains it"},
    {"stream": "coinages", "cls": "state", "act": "report", "size": 256 * 1024,
     "find": lambda: _one(_state("coinages.json")),
     "note": "self-bounding (COINAGE_CAP) — over budget means the cap broke"},
    {"stream": "prompt-census", "cls": "state", "act": "report", "size": 512 * 1024,
     "find": lambda: _one(_state("prompt-census.json")),
     "note": "self-bounding (promptcensus.CAP forms, WINDOW turns) — over "
             "budget means the bound broke"},
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


def _owner_bound(policy):
    return bool(policy.get("owner")) and bool(
        policy.get("lock") or policy.get("immutable"))


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
               "note": p.get("note", "owner actuator unavailable"),
               "budget": "", "used": "", "victims": [], "bytes": 0,
               "over": False, "policy": p, "now": now, "claims": {}}
        try:
            found = p["find"]()
            if _owner_bound(p):
                row["claims"] = {path: _identity(path) for path in found}
            axis = next(name for name in _BUDGET_AXES if name in p)
            budget, used, victims, size = _BUDGET_AXES[axis](found, p, now)
            row.update(budget=budget, used=used)
            if victims:
                row.update(over=True, victims=victims, bytes=size)
        except Exception as exc:
            row["error"] = str(exc)
        rows.append(row)
    return rows


def _still_victim(row, path):
    """Reprove exact generation, owner membership, and budget eligibility."""
    claim = row["claims"].get(path)
    try:
        current = _identity(path)
    except OSError:
        return False
    if current != claim or claim[2] != stat.S_IFREG:
        return False
    p = row["policy"]
    if "still" in p:
        # A PER-PATH RE-PROOF, for a count-0 row whose finder is a listing
        # of a whole directory. Re-running that listing once per victim is
        # quadratic in exactly the directory the row exists to shrink:
        # 49,718 victims x a 108,177-entry listing.
        return p["count"] == 0 and bool(p["still"](path))
    found = p["find"]()
    if path not in found:
        return False
    if "age" in p:
        return _born(path) < row["now"] - p["age"] * DAY
    if "size" in p:
        return sum(_size(candidate) for candidate in found) > p["size"]
    if p["count"] == 0:
        return True
    return path in sorted(found, key=_born, reverse=True)[p["count"]:]


def _skipped(path, reason):
    """The one operator-visible spelling for every refused mutation."""
    return "SKIPPED %s (%s)" % (path, reason)


def _reap(row):
    """Enforce one row and return exact successful item/byte accounting."""
    lines, items, reaped = [], 0, 0
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())

    # Policies without a newly declared owner protocol retain current-main's
    # actuator unchanged. Their ownership migrations belong to their own slices.
    if not _owner_bound(row["policy"]):
        for victim in row["victims"]:
            try:
                n = _size(victim)
                if row["act"] == "rotate":
                    dst = victim + "." + stamp
                    os.replace(victim, dst)
                    lines.append("rotated %s -> %s" % (
                        victim, os.path.basename(dst)))
                elif os.path.isdir(victim):
                    shutil.rmtree(victim)
                    lines.append("pruned " + victim + "/")
                else:
                    os.remove(victim)
                    lines.append("pruned " + victim)
                items += 1
                reaped += n
            except OSError as exc:
                lines.append(_skipped(victim, exc))
        return lines, items, reaped

    for victim in row["victims"]:
        lock = None
        try:
            lock_path = row["policy"].get("lock")
            if lock_path and lock_path(victim) == victim:
                # THE VICTIM IS ITS OWN LOCK: open it, never create it. "a+"
                # re-created a victim another row had already reaped, then
                # reported it SKIPPED and left the fresh empty file behind.
                lock = os.fdopen(os.open(victim, os.O_RDONLY), "rb")
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif lock_path:
                lock = open(lock_path(victim), "a+")
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            if not _still_victim(row, victim):
                lines.append(_skipped(victim, "no longer owner-eligible or over budget"))
                continue
            n = _size(victim)
            if row["act"] == "rotate":
                dst = victim + "." + stamp
                if os.path.lexists(dst):
                    raise FileExistsError("rotation generation already exists")
                os.replace(victim, dst)
                lines.append("rotated %s -> %s" % (victim, os.path.basename(dst)))
            else:
                os.remove(victim)
                lines.append("pruned " + victim)
            items += 1
            reaped += n
        except Exception as exc:
            lines.append(_skipped(victim, exc))
        finally:
            if lock is not None:
                lock.close()
    return lines, items, reaped


_SERVICE = """[Unit]
Description=helm gc — enforce declared retention over the derived exhaust
Documentation=premise:nothing-may-gate-on-an-undrained-pile

[Service]
WorkingDirectory=%(cwd)s
Type=oneshot
# THE DRAIN THAT EXISTED AND WAS NEVER SCHEDULED. gc has always known its own
# per-stream retention policy; nothing ever ran it, so the exhaust grew without
# bound — measured 2026-07-30 at 11,192 items over budget (11,191 chat cursors
# plus a 31.5MB usage-history file against a 5MB budget) on a fleet whose work
# actuator was ALSO gated on an undrained pile. A policy with no scheduler is a
# policy that does not exist.
# --apply is safe by construction: each stream reaps only what its own declared
# budget names, and non-exhaust streams stay report-only (a dirty lane is never
# discarded here; `helm work gc --apply` rescue-commits first).
ExecStart=%(helm)s gc --apply
# THE SIDEBAR RECURRENCE, KILLED AT THE CADENCE (#157, the owner's FIFTH ask):
# every claim/peek/fold mints a worktree, and with no scheduled reap a
# metaharness sidebar accretes one ghost project per room forever — the owner
# hand-swept ~90 of them on 2026-08-03. work gc --apply retires clean LANDED
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
    reaped_items = reaped_bytes = reaped_streams = skipped_items = 0
    report_over = errors = 0
    for r in loud:
        if r.get("error"):
            errors += 1
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
        lines, items, n = _reap(r)
        for line in lines:
            print("        " + line)
        reaped_items += items
        reaped_bytes += n
        skipped_items += len(r["victims"]) - items
        if items:
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
              + ("; %d skipped" % skipped_items if skipped_items else "")
              + ("; %d report-only over budget" % report_over if report_over else "")
              + ("; %d stream error%s — other retention status UNKNOWN"
                 % (errors, "s"[:errors != 1]) if errors else ""))
    elif enforcing and skipped_items:
        print("helm gc: reaped 0 items; %d skipped — nothing touched" % skipped_items)
    elif would or report_over:
        print("helm gc: %d item%s would be reaped; %d report-only over budget "
              "— nothing touched%s" % (
                  would, "s"[:would != 1], report_over,
                  "; %d stream error%s" % (errors, "s"[:errors != 1])
                  if errors else ""))
    elif errors:
        print("helm gc: %d stream error%s — retention status UNKNOWN; nothing touched"
              % (errors, "s"[:errors != 1]))
    else:
        print("helm gc: every stream in budget (%d declared) — nothing to reap"
              % len(rows))
    return 0
