#!/usr/bin/env python3
"""helm scratch — the MOUNT plane: where scratch goes, and who reaps it.

THE INCIDENT (2026-07-24, ground-truthed live): the fleet hit `No space left on
device` writing to /tmp while `df -h /tmp` read **36% used (16G of 44G)**. /tmp
is a tmpfs mounted with an explicit `nr_inodes=1048576` cap and had 1,048,562
of those inodes used — **14 free**. /dev/shm on the same box carries NO cap
(11,501,004 inodes, 1% used); the box had 36G of 87G RAM free. So it was never
a RAM shortage: it was an artificial inode cap, INVISIBLE to every bytes-based
check on the estate. The file explosion was agent scratch — per-session scratch
trees (300-2,500 files each, one held 88,142), whole `git clone` verification
trees and a full Go repo fork, i.e. repos cloned into RAM — and nobody reaping.

Three legs, all SUBSTRATE. RAM scratch is good and fast (helm's own chat bus
lives in /dev/shm by design); the fix is not "stop using RAM", it is "stop
making the agent guess, and reap what dies". A rule an agent must REMEMBER is a
rule that fails under load (premise enforce-not-advise-for-repeated-behavior),
so none of this is advice:

  * SURVEY (survey/doctor_rows) — every mount helm actually writes, measured on
    BOTH axes: bytes AND INODES. This is the only place on the estate that sees
    an inode cap. Over threshold it names the mount, the cap, the top offender,
    and the two REAL fixes (raise nr_inodes; keep big trees off the capped
    mount) so a future agent is not rediscovering this from ENOSPC.
  * ROUTE (resolve) — the substrate picks the mount by CLASS, never the agent:
      small   — a probe/fixture/temp file: the ambient tmp is right and fast.
      big     — a clone / fork / verification tree (thousands of inodes): the
                UNCAPPED tmpfs while it has headroom, else disk.
      durable — anything that must survive: DISK, always. tmpfs is volatile;
                it dies with the boot.
    `HELM_SCRATCH_DIR` overrides the root outright (operator wins).
  * REAP (gc/auto_gc) — dead-session scratch, on an EXISTING hook (the Stop
    hook's silent-mechanical leg), never a new service. The hard part is NOT
    being overbearing, so the laws are:
      - LIVENESS BEFORE AGE. A tree is reapable only when its session id is
        referenced by NO live same-uid process AND no live cwd sits inside it.
        Age alone never reaps — yanking scratch out from under a working agent
        is the failure mode worse than the disk filling.
      - ATTRIBUTABLE ONLY. Only a child whose basename is a session-id-shaped
        token (uuid) or `pid-<n>` is ever a candidate. Anything helm cannot
        attribute to a session/pid is left alone, forever.
      - FAIL-CLOSED on probe trouble: an unlistable/unreadable process table
        means liveness is UNKNOWN, not absent — reap NOTHING.
      - PRESSURE-ESCALATING: 7d TTL at rest, 6h past the warn threshold, 1h
        past critical. Bounded per pass (candidates, victims, stats, procs) so
        a GC tick can never itself be the expensive thing.
      - LOUD: every applied pass writes a one-line pk.event, and doctor surfaces
        the last one — a silent data-eater is not allowed.
      - Kill-switches: HELM_SCRATCH_GC=0 (the reaper), HELM_SCRATCH_TMPDIR=0
        (the launch-env routing).
"""
import glob
import os
import re
import shutil
import sys
import time

from . import home, pk

MB = 1024 * 1024

# ── thresholds ────────────────────────────────────────────────────────────────
WARN_PCT = 85            # either axis at/over this = pressure (the doctor warn)
CRIT_PCT = 95            # either axis at/over this = critical
HEADROOM_PCT = 60        # a mount must be under this on BOTH axes to take a
                         # big tree (a clone is thousands of inodes at once)
BIG_INODE_FLOOR = 250000  # …and still have this many free inodes to spare

# ── bounds (a GC pass must never be the expensive thing) ─────────────────────
CANDIDATE_CAP = 400      # scratch children examined per pass
AGED_CAP = 60            # aged candidates carried into the liveness probe
REAP_CAP = 24            # dirs removed per pass
MTIME_STAT_CAP = 400     # stats per candidate when probing newest-mtime
COUNT_CAP = 20000        # entries counted per tree (inode ranking)
PROC_CAP = 4096          # processes read in one liveness probe
BLOB_CAP = 64 * 1024     # bytes read per process cmdline/environ
THROTTLE_S = 3600        # minimum gap between auto passes

TTL_BY_LEVEL = {"ok": 7 * 86400, "warn": 6 * 3600, "critical": 3600}

# A scratch child helm will consider AT ALL: a uuid-shaped session id, or an
# explicit pid tag helm itself minted. Everything else is unattributable and is
# never a candidate (the anti-overbearing floor).
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_PIDTAG_RE = re.compile(r"^pid-(\d+)$")
_UUID_IN_BLOB = re.compile(
    rb"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    rb"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

SCRATCH_LEAF = "helm-scratch"    # helm's own routed root, under some mount
CLASSES = ("small", "big", "durable")
PROC = "/proc"                   # module-level so tests point it at a fixture
MOUNTS = "/proc/mounts"
VOLATILE_FS = ("tmpfs", "ramfs", "devtmpfs")


def _off(name):
    return (home.env(name) or "").lower() in ("0", "off", "no")


def cache_root():
    """helm's disk cache root (gc.py's _cache seam, one config surface)."""
    return home.env("CACHE_DIR") or os.path.join(
        os.path.expanduser("~"), ".cache", "helm")


# ---------------------------------------------------------------------------
# the survey — BOTH axes, because an inode cap is invisible to bytes
# ---------------------------------------------------------------------------

def mount_table(path=None):
    """[(mountpoint, fstype, options)] from /proc/mounts, longest-first so a
    prefix lookup hits the most specific mount. [] when unreadable — the
    survey then reports fstype/cap as unknown rather than crashing."""
    try:
        with open(path or MOUNTS) as f:
            raw = f.read()
    except OSError:
        return []
    rows = []
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        # /proc/mounts escapes spaces as \040 in the mountpoint field
        rows.append((parts[1].replace("\\040", " "), parts[2], parts[3]))
    rows.sort(key=lambda r: len(r[0]), reverse=True)
    return rows


def _mount_of(path, table):
    real = os.path.realpath(path)
    for mp, fstype, opts in table:
        if real == mp or real.startswith(mp.rstrip("/") + "/"):
            return mp, fstype, opts
    return None, None, ""


def _nr_inodes(opts):
    """The explicit nr_inodes= cap on a tmpfs mount, or None. THE thing that
    made the incident invisible: a cap in the mount options, nowhere in df -h."""
    for o in (opts or "").split(","):
        if o.startswith("nr_inodes="):
            try:
                return int(o.split("=", 1)[1])
            except ValueError:
                return None
    return None


def _pct(used, avail):
    """df's percentage: used / (used + available). 0 when the axis is
    unmeasurable (a filesystem reporting no inode accounting at all)."""
    tot = used + avail
    return int(round(100.0 * used / tot)) if tot > 0 else 0


def usage(path, table=None):
    """One mount measured on both axes, or None when `path` is unreadable
    (FAIL-OPEN: an unreadable/missing mount is never a warning, it is simply
    unmeasured — the check must not crash on a path that vanished)."""
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    table = mount_table() if table is None else table
    mp, fstype, opts = _mount_of(path, table)
    b_used = (st.f_blocks - st.f_bfree) * st.f_frsize
    b_avail = st.f_bavail * st.f_frsize
    i_total = st.f_files
    i_free = st.f_favail if st.f_favail else st.f_ffree
    i_used = (i_total - st.f_ffree) if i_total else 0
    return {
        "path": path, "mount": mp or path, "fstype": fstype or "?",
        "volatile": (fstype or "") in VOLATILE_FS,
        "nr_inodes": _nr_inodes(opts),
        "bytes_used": b_used, "bytes_avail": b_avail,
        "bytes_pct": _pct(b_used, b_avail),
        "inodes_total": i_total, "inodes_used": i_used, "inodes_free": i_free,
        "inodes_pct": _pct(i_used, i_free) if i_total else None,
        "dev": _dev(path),
    }


def _dev(path):
    try:
        return os.stat(path).st_dev
    except OSError:
        return None


def worst_pct(u):
    """The pressure number for a mount: the WORSE of the two axes. The whole
    point — a mount at 36% bytes and 100% inodes is a mount at 100%."""
    if not u:
        return 0
    return max(u["bytes_pct"], u["inodes_pct"] or 0)


def level_of(pct):
    if pct >= CRIT_PCT:
        return "critical"
    return "warn" if pct >= WARN_PCT else "ok"


def helm_paths():
    """Every path helm actually writes, in report order. Missing paths are
    dropped by the survey (usage() returns None)."""
    from . import chat
    out = []
    try:
        out.append(("chat bus", chat.chat_dir()))
    except Exception:
        pass
    out.append(("helm home", home.helm_home()))
    out.append(("cache", cache_root()))
    out.append(("tmp", _ambient_tmp()))
    override = home.env("SCRATCH_DIR")
    if override:
        out.append(("scratch (HELM_SCRATCH_DIR)", override))
    return [(label, p) for label, p in out if p]


def _ambient_tmp():
    import tempfile
    try:
        return tempfile.gettempdir()
    except Exception:
        return "/tmp"


def survey():
    """[row] — one row per DISTINCT filesystem helm writes to, first label
    wins. Never raises: an unreadable path is skipped, not reported."""
    table = mount_table()
    rows, seen = [], set()
    for label, path in helm_paths():
        u = usage(path, table)
        if not u:
            continue
        key = u["dev"] if u["dev"] is not None else u["mount"]
        if key in seen:
            continue
        seen.add(key)
        u["label"] = label
        rows.append(u)
    return rows


# ---------------------------------------------------------------------------
# routing — the substrate picks the mount, the agent never guesses
# ---------------------------------------------------------------------------

def _writable(path):
    """Can we create under this dir (walking up to the nearest existing
    ancestor)? A candidate mount we cannot write is not a candidate."""
    p = os.path.abspath(path)
    while p and not os.path.isdir(p):
        parent = os.path.dirname(p)
        if parent == p:
            return False
        p = parent
    return os.access(p, os.W_OK | os.X_OK)


def _has_headroom(u, inode_floor=0):
    if not u:
        return False
    if u["bytes_pct"] >= HEADROOM_PCT:
        return False
    ip = u["inodes_pct"]
    if ip is not None and ip >= HEADROOM_PCT:
        return False
    if inode_floor and u["inodes_total"] and u["inodes_free"] < inode_floor:
        return False
    return True


def _volatile_candidates(table):
    """Writable volatile (RAM) mounts helm may route onto, best first: most
    free inodes wins, and an UNCAPPED mount always outranks a capped one.
    /dev/shm is listed explicitly because it is the estate's uncapped mount
    (helm's chat bus already lives there); the ambient tmp rides along."""
    seen, out = set(), []
    for path in ("/dev/shm", _ambient_tmp()):
        u = usage(path, table)
        if not u or not u["volatile"] or not _writable(path):
            continue
        key = u["dev"] if u["dev"] is not None else u["mount"]
        if key in seen:
            continue
        seen.add(key)
        out.append(u)
    out.sort(key=lambda u: (u["nr_inodes"] is not None, -(u["inodes_free"] or 0)))
    return out


def _disk_root(table):
    """A NON-volatile root for durable/overflow scratch. tmpfs dies with the
    boot, so a durable artifact must never land on one; if every candidate is
    volatile we still return the first (loudly documented) rather than fail."""
    cands = [os.path.join(cache_root(), "scratch"),
             os.path.join(home.helm_home(), "_global", "scratch"),
             os.path.join(os.path.expanduser("~"), ".helm-scratch")]
    for c in cands:
        u = usage(os.path.dirname(c), table) or usage(c, table)
        if u and not u["volatile"] and _writable(c):
            return c, True
    return cands[0], False


def route(cls="small"):
    """(path, why) — the ROOT for this scratch class, decided from live mount
    state. No side effects; `resolve` is the one that creates."""
    if cls not in CLASSES:
        raise ValueError("scratch class must be one of %s" % (CLASSES,))
    override = home.env("SCRATCH_DIR")
    if override:
        return os.path.join(os.path.expanduser(override), cls), \
            "HELM_SCRATCH_DIR override"
    table = mount_table()
    disk, real_disk = _disk_root(table)
    if cls == "durable":
        return disk, ("disk (durable never lands on volatile tmpfs)"
                      if real_disk else
                      "NO non-volatile root found — this scratch is VOLATILE")
    vols = _volatile_candidates(table)
    if cls == "big":
        for u in vols:
            if _has_headroom(u, BIG_INODE_FLOOR):
                return os.path.join(u["path"], SCRATCH_LEAF), (
                    "%s: uncapped RAM with headroom (%d%% bytes, %s inodes)"
                    % (u["mount"], u["bytes_pct"],
                       "%d%%" % u["inodes_pct"] if u["inodes_pct"] is not None
                       else "n/a") if u["nr_inodes"] is None else
                    "%s: RAM with headroom (nr_inodes=%d cap)"
                    % (u["mount"], u["nr_inodes"]))
        return disk, "no RAM mount has headroom for a big tree — disk"
    ambient = _ambient_tmp()
    u = usage(ambient, table)
    if u is None or _has_headroom(u):
        return os.path.join(ambient, SCRATCH_LEAF), "ambient tmp (small+fast)"
    for v in vols:
        if _has_headroom(v):
            return os.path.join(v["path"], SCRATCH_LEAF), (
                "%s is under pressure (%d%%) — routed to %s"
                % (u["mount"], worst_pct(u), v["mount"]))
    return disk, "every RAM mount is under pressure — disk"


def tag():
    """The scratch tag: the ambient session id when the harness exports one
    (so the reaper can prove liveness against it), else `pid-<n>`."""
    for var in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID",
                "CODEX_SESSION_ID"):
        v = (os.environ.get(var) or "").strip()
        if _UUID_RE.match(v):
            return v
    return "pid-%d" % os.getpid()


def resolve(cls="small", name=None, create=True):
    """(path, why) — the routed scratch dir for this class, created. Layout is
    `<root>/<tag>[/<name>]` so the reaper can attribute (and therefore reap)
    every tree it made."""
    root, why = route(cls)
    path = os.path.join(root, tag())
    if name:
        path = os.path.join(path, pk.slug(str(name)) or "x")
    if create:
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as exc:
            return None, "cannot create %s (%s)" % (path, exc)
    return path, why


def launch_tmpdir():
    """TMPDIR for a launched seat's env, or None when routing is off/unusable.
    The 'agents never think about it' half: every mktemp/tempfile/git-temp in
    the child lands on a mount helm chose. Opt out with HELM_SCRATCH_TMPDIR=0.

    DELIBERATELY NOT `route("small")`, for two honest reasons:
      * TMPDIR content is NOT session-attributable — tools name their own files
        and nothing ties them to a session id — so the reaper cannot reap it.
        Its only protection is landing off the CAPPED mount in the first place.
      * unbounded + unreapable must therefore never share a mount with the CHAT
        BUS (/dev/shm/helm-chat is a design pillar). So the overflow target here
        is DISK, never the other RAM mount: no byte-pressure coupling between a
        seat's build cache and the fleet's message bus.
    Healthy ambient tmp keeps it (fast, and it is where it already was)."""
    if _off("SCRATCH_TMPDIR"):
        return None
    try:
        override = home.env("SCRATCH_DIR")
        if override:
            root = os.path.join(os.path.expanduser(override), "small")
        else:
            table = mount_table()
            ambient = _ambient_tmp()
            u = usage(ambient, table)
            root = (os.path.join(ambient, SCRATCH_LEAF)
                    if u is None or _has_headroom(u)
                    else os.path.join(_disk_root(table)[0], "tmpdir-overflow"))
    except Exception:
        return None
    path = os.path.join(root, "tmpdir")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        return None
    return path


# ---------------------------------------------------------------------------
# liveness — the anti-overbearing gate. LIVENESS BEFORE AGE, fail-closed.
# ---------------------------------------------------------------------------

def live_evidence(proc_dir=None):
    """(ids, cwds, trouble) — every uuid-shaped session id referenced by any
    live SAME-UID process (cmdline + environ, one bounded regex pass each),
    plus every live cwd, plus the live pid set. FAIL-CLOSED: on any probe
    trouble the caller reaps NOTHING (an unfinished scan never testifies to
    absence — seats._live_process_evidence's law).

    Same-uid scope is deliberate: a foreign-uid process cannot host this
    user's harness and its environ is unreadable by kernel design, so
    counting that as trouble would fail-close every pass on any real host.

    NOT-DUMPABLE IS THE SAME CLASS (found on the first live run: `systemd
    --user` is uid 1000 with an unreadable environ, and treating that one
    PermissionError as trouble disabled the reaper permanently). A process the
    kernel refuses to describe is OPAQUE, not evidence — and it can never be an
    agent harness, since a non-dumpable process is one that exec'd setuid/setgid
    or cleared PR_SET_DUMPABLE, which no python/node harness does. Opaque
    processes are counted and skipped; only a NON-permission read failure (a
    genuinely broken probe) still fails the pass closed."""
    proc_dir = proc_dir or PROC
    try:
        me = os.getuid()
        pids = [n for n in os.listdir(proc_dir) if n.isdigit()]
    except OSError as exc:
        return None, None, "process table unlistable (%s)" % exc
    if len(pids) > PROC_CAP:          # bounded: never an unbounded scan
        pids = pids[:PROC_CAP]
    ids, cwds, live_pids = set(), set(), set()
    opaque = 0
    for pid in pids:
        pdir = os.path.join(proc_dir, pid)
        try:
            if os.stat(pdir).st_uid != me:
                continue
        except OSError:
            continue                  # exited between listdir and stat
        live_pids.add(int(pid))
        for leaf in ("cmdline", "environ"):
            try:
                with open(os.path.join(pdir, leaf), "rb") as f:
                    blob = f.read(BLOB_CAP)
            except (FileNotFoundError, ProcessLookupError, NotADirectoryError):
                continue              # exited mid-scan: proven not-live
            except PermissionError:
                opaque += 1           # not-dumpable: opaque by design
                continue
            except OSError as exc:
                return None, None, ("process %s %s unreadable (%s)"
                                    % (pid, leaf, exc.__class__.__name__))
            ids.update(m.decode("ascii") for m in _UUID_IN_BLOB.findall(blob))
        try:
            cwds.add(os.path.realpath(os.readlink(os.path.join(pdir, "cwd"))))
        except OSError:
            pass                      # a cwd we cannot read is not evidence
    return {"ids": ids, "pids": live_pids, "opaque": opaque}, cwds, None


def _referenced(tag_name, live, cwds, path):
    """Is this scratch tree LIVE? Any of: its session id is referenced by a
    live process, its pid tag is a live pid, or a live cwd sits inside it."""
    m = _PIDTAG_RE.match(tag_name)
    if m and int(m.group(1)) in live["pids"]:
        return "pid %s is alive" % m.group(1)
    if tag_name in live["ids"]:
        return "session id referenced by a live process"
    real = os.path.realpath(path)
    for c in cwds:
        if c == real or c.startswith(real.rstrip("/") + "/"):
            return "a live process is cwd'd inside it"
    return None


# ---------------------------------------------------------------------------
# the reaper
# ---------------------------------------------------------------------------

def scratch_specs():
    """[(glob, label)] — the dirs whose matched children are per-session
    scratch trees keyed by a session id or pid tag. helm's OWN routed roots
    plus the harness scratchpad estate (corpus.TMP_ROOT_GLOBS's root: the
    harness owns `/tmp/claude-<uid>/<project>/<session>/`, so helm cannot
    place it — but it CAN prove it dead and reap it)."""
    out = []
    seen = set()
    for cls in CLASSES:
        try:
            root, _ = route(cls)
        except Exception:
            continue
        if root in seen:
            continue
        seen.add(root)
        out.append((os.path.join(root, "*"), "helm scratch (%s)" % cls))
    out.append((os.path.join(_ambient_tmp(), "claude-*", "*", "*"),
                "harness scratchpad"))
    return out


def _newest_mtime(path):
    """Bounded newest-mtime under a tree: the dir itself plus a breadth-first
    sample capped at MTIME_STAT_CAP stats (a live agent's newest writes are
    shallow; LIVENESS, not this, is what protects live work). Fail-open to NOW
    — an unreadable tree is never proven stale."""
    try:
        newest = os.path.getmtime(path)
    except OSError:
        return time.time()
    stats, queue = 0, [path]
    while queue and stats < MTIME_STAT_CAP:
        d = queue.pop(0)
        try:
            with os.scandir(d) as it:
                for e in it:
                    stats += 1
                    if stats >= MTIME_STAT_CAP:
                        break
                    try:
                        newest = max(newest, e.stat(follow_symlinks=False).st_mtime)
                        if e.is_dir(follow_symlinks=False):
                            queue.append(e.path)
                    except OSError:
                        pass
        except OSError:
            pass
    return newest


# Directory names that make a tree OFF-LIMITS regardless of age or liveness: a
# harness TRANSCRIPT subtree. corpus.py already treats `/tmp/claude-*/**/
# projects/**/*.jsonl` and `**/subagents/**/*.jsonl` as TRAINING CORPUS it backs
# up copy-only — so a reaper that could delete one would be destroying the
# authored record the corpus exists to preserve. Structural refusal, not a
# heuristic: seeing the directory at all is enough.
PROTECTED_DIRS = ("projects", "subagents")


def count_entries(path):
    """(entries, capped, protected) — bounded inode count for ranking, plus the
    transcript refusal. `capped` means the real count is at least COUNT_CAP
    (the 88,142-file tree class); `protected` means a harness transcript subtree
    lives in here and the tree must never be reaped."""
    n, queue, protected = 0, [path], False
    while queue:
        d = queue.pop(0)
        try:
            with os.scandir(d) as it:
                for e in it:
                    n += 1
                    if n >= COUNT_CAP:
                        return n, True, protected
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if e.name in PROTECTED_DIRS:
                                protected = True
                            queue.append(e.path)
                    except OSError:
                        pass
        except OSError:
            pass
    return n, False, protected


def candidates(now=None, specs=None):
    """[(path, tag, mtime)] — attributable scratch children, bounded. Shape
    gate first (uuid / pid-<n>), so anything helm cannot attribute is never
    even a candidate."""
    now = time.time() if now is None else now
    rows = []
    for pattern, label in (specs or scratch_specs()):
        try:
            hits = sorted(glob.glob(pattern))
        except Exception:
            continue
        for p in hits:
            base = os.path.basename(p)
            if not (_UUID_RE.match(base) or _PIDTAG_RE.match(base)):
                continue
            if not os.path.isdir(p) or os.path.islink(p):
                continue
            rows.append((p, base, label))
            if len(rows) >= CANDIDATE_CAP:
                return rows
    return rows


def gc(apply=False, now=None, specs=None, proc_dir=None, ttl=None, rows=None):
    """One bounded reap pass -> a report dict. Dry by default.

    Order is load-bearing: measure pressure -> pick the TTL -> shape-gate +
    age-gate the candidates (cheap) -> ONLY THEN probe liveness (one /proc
    pass, and only when something aged) -> reap biggest-by-inodes first, up to
    REAP_CAP. Probe trouble reaps nothing."""
    now = time.time() if now is None else now
    surveyed = rows if rows is not None else survey()
    pressure = max([worst_pct(u) for u in surveyed] or [0])
    level = level_of(pressure)
    ttl = TTL_BY_LEVEL[level] if ttl is None else ttl
    rep = {"pressure": pressure, "level": level, "ttl": ttl, "candidates": 0,
           "aged": 0, "kept": [], "victims": [], "reaped": 0, "files": 0,
           "trouble": None, "capped": False}
    cands = candidates(now=now, specs=specs)
    rep["candidates"] = len(cands)
    aged = [(p, t, lab) for p, t, lab in cands if _newest_mtime(p) < now - ttl]
    rep["aged"] = len(aged)
    if len(aged) > AGED_CAP:
        aged, rep["capped"] = aged[:AGED_CAP], True
    if not aged:
        return rep
    live, cwds, trouble = live_evidence(proc_dir)
    if trouble:
        rep["trouble"] = trouble + " — liveness UNKNOWN, reaping nothing"
        return rep
    ranked = []
    for p, t, lab in aged:
        why_live = _referenced(t, live, cwds, p)
        if why_live:
            rep["kept"].append((p, why_live))
            continue
        n, capped, protected = count_entries(p)
        if protected:
            rep["kept"].append(
                (p, "holds a harness transcript subtree (%s) — the training "
                    "corpus is never reapable" % "/".join(PROTECTED_DIRS)))
            continue
        ranked.append({"path": p, "tag": t, "label": lab, "files": n,
                       "at_least": capped})
    ranked.sort(key=lambda r: -r["files"])
    rep["victims"] = ranked[:REAP_CAP]
    if not apply:
        return rep
    for v in rep["victims"]:
        try:
            shutil.rmtree(v["path"])
            rep["reaped"] += 1
            rep["files"] += v["files"]
        except OSError as exc:
            v["error"] = str(exc)     # a locked victim is skipped LOUDLY
    if rep["reaped"]:
        pk.event("scratch", "gc", summary(rep))
    return rep


def summary(rep):
    return ("reaped %d dead-session scratch dir%s (%s%d files) at %d%% "
            "pressure (%s, ttl %s); %d kept live, %d candidates"
            % (rep["reaped"], "s"[:rep["reaped"] != 1],
               ">=" if any(v.get("at_least") for v in rep["victims"]) else "",
               rep["files"], rep["pressure"], rep["level"],
               _dur(rep["ttl"]), len(rep["kept"]), rep["candidates"]))


def _dur(s):
    if s >= 86400:
        return "%dd" % (s // 86400)
    return "%dh" % (s // 3600) if s >= 3600 else "%ds" % s


def _stamp_path():
    return os.path.join(cache_root(), "scratch-gc.stamp")


def _due(now):
    try:
        return now - os.path.getmtime(_stamp_path()) >= THROTTLE_S
    except OSError:
        return True


def _touch_stamp():
    try:
        os.makedirs(os.path.dirname(_stamp_path()), exist_ok=True)
        with open(_stamp_path(), "w") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    except OSError:
        pass


def auto_gc(now=None):
    """The automatic leg — called from the Stop hook's silent-mechanical lane
    (an EXISTING hook; helm adds no service). Returns the summary line or None.
    NEVER raises, NEVER prints — the audit trail is pk.event plus doctor's
    last-pass row. Kill-switch: HELM_SCRATCH_GC=0.

    NOT OVERBEARING, in three layers: (1) PRESSURE-GATED — at rest (every mount
    under WARN_PCT) the pass reaps NOTHING and costs exactly one statvfs per
    mount; deleting files nobody needs deleted is its own failure mode.
    (2) THROTTLED to one pass an hour. (3) BOUNDED, and liveness-gated inside
    gc() so a live agent's scratch is never touched."""
    now = time.time() if now is None else now
    if _off("SCRATCH_GC") or not _due(now):
        return None
    try:
        rows = survey()
        if level_of(max([worst_pct(u) for u in rows] or [0])) == "ok":
            return None               # at rest: a survey, and nothing else
    except Exception:
        return None
    _touch_stamp()
    try:
        rep = gc(apply=True, now=now, rows=rows)
    except Exception:
        return None                   # fail-open: a janitor never gates a stop
    return summary(rep) if rep["reaped"] else None


# ---------------------------------------------------------------------------
# doctor rows
# ---------------------------------------------------------------------------

OK, WARN = "OK", "WARN"


def _human(n):
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return ("%d%s" if unit == "B" else "%.1f%s") % (n, unit)
        n /= 1024.0


def top_offender(u):
    """The biggest attributable scratch tree ON this mount, bounded — named
    only when the mount is already over threshold (cheap when it matters,
    never paid at rest)."""
    best = None
    for p, _t, _lab in candidates():
        if _dev(p) != u["dev"]:
            continue
        n, capped, _protected = count_entries(p)
        if best is None or n > best[1]:
            best = (p, n, capped)
    return best


def _fix_line(u):
    """The DURABLE pointer: the two real fixes, named, so the next agent to
    meet this does not rediscover it from an ENOSPC."""
    cap = ("This tmpfs carries an explicit nr_inodes=%d cap" % u["nr_inodes"]
           if u["nr_inodes"] else "No explicit nr_inodes cap in the options")
    return ("%s. TWO REAL FIXES: (1) RAISE THE CAP — remount with a bigger "
            "nr_inodes= (or drop the option entirely, as /dev/shm does); "
            "(2) KEEP BIG TREES OFF IT — `helm scratch big` routes a clone/"
            "fork to an uncapped mount, `helm scratch gc --apply` reaps "
            "dead-session scratch" % cap)


def doctor_rows(rows=None):
    """helm doctor's filesystem leg: BOTH axes per mount helm writes. Warns on
    inodes even when bytes are low — the 36%-bytes/100%-inodes shape that made
    the incident invisible. Fail-open: never raises, an unreadable mount is
    simply unmeasured."""
    try:
        rows = survey() if rows is None else rows
    except Exception as exc:
        return [(WARN, "filesystem survey failed (%s: %s) — bytes AND inode "
                       "pressure are UNKNOWN" % (exc.__class__.__name__, exc))]
    out = []
    for u in rows:
        ip = u["inodes_pct"]
        axes = "bytes %d%%, inodes %s" % (
            u["bytes_pct"], "%d%%" % ip if ip is not None else "n/a")
        if worst_pct(u) < WARN_PCT:
            out.append((OK, "fs %s (%s, %s): %s"
                        % (u["mount"], u["label"], u["fstype"], axes)))
            continue
        why = []
        if ip is not None and ip >= WARN_PCT:
            why.append("INODES %d%% (%d of %d used, %d free) while bytes are "
                       "only %d%% — every bytes-based check (df -h) reads "
                       "GREEN and writes still fail 'No space left on device'"
                       % (ip, u["inodes_used"], u["inodes_total"],
                          u["inodes_free"], u["bytes_pct"]))
        if u["bytes_pct"] >= WARN_PCT:
            why.append("BYTES %d%% (%s free)"
                       % (u["bytes_pct"], _human(u["bytes_avail"])))
        top = ""
        try:
            best = top_offender(u)
            if best:
                top = "  Top offender: %s (%s%d entries)." % (
                    best[0], ">=" if best[2] else "", best[1])
        except Exception:
            pass
        out.append((WARN, "fs %s (%s, %s): %s.%s  %s"
                    % (u["mount"], u["label"], u["fstype"],
                       "; ".join(why), top, _fix_line(u))))
    last = _last_gc()
    if last:
        out.append((OK, "scratch gc: " + last))
    return out


def _last_gc():
    """The last applied auto-pass, from the events journal — the LOUD half: a
    reaper nobody can audit is not allowed."""
    try:
        for row in reversed(pk.read_events(200) or []):
            if row.get("verb") == "scratch" and row.get("target") == "gc":
                return "%s (%s)" % (row.get("summary") or "?",
                                    row.get("ts") or "?")
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_survey(rows):
    print("helm scratch — mounts helm writes (BOTH axes; an inode cap is "
          "invisible to df -h):")
    for u in rows:
        ip = u["inodes_pct"]
        print("  %-14s %-28s %-7s bytes %3d%% (%s free)  inodes %4s "
              "(%s free%s)"
              % (u["label"], u["mount"], u["fstype"], u["bytes_pct"],
                 _human(u["bytes_avail"]),
                 "%d%%" % ip if ip is not None else "n/a",
                 u["inodes_free"] if u["inodes_total"] else "n/a",
                 ", nr_inodes=%d CAP" % u["nr_inodes"] if u["nr_inodes"] else ""))


def _print_routes():
    print("  routing (the substrate picks the mount — never the agent):")
    for cls in CLASSES:
        try:
            root, why = route(cls)
        except Exception as exc:
            root, why = "?", str(exc)
        print("    %-8s -> %-46s %s" % (cls, root, why))


def _print_gc(rep, applying):
    print("helm scratch gc — dead-session scratch (%s)"
          % ("APPLYING" if applying
             else "dry-run; `helm scratch gc --apply` reaps"))
    print("  pressure %d%% (%s) -> ttl %s; %d candidate%s, %d aged%s"
          % (rep["pressure"], rep["level"], _dur(rep["ttl"]),
             rep["candidates"], "s"[:rep["candidates"] != 1], rep["aged"],
             " (capped at %d)" % AGED_CAP if rep["capped"] else ""))
    if rep["trouble"]:
        print("  TROUBLE: %s" % rep["trouble"])
    for p, why in rep["kept"]:
        print("  KEPT   %s  (%s)" % (p, why))
    for v in rep["victims"]:
        state = ("SKIPPED (%s)" % v["error"] if v.get("error")
                 else "reaped" if applying else "would reap")
        print("  %-8s %s  (%s%d entries, %s)"
              % (state, v["path"], ">=" if v["at_least"] else "",
                 v["files"], v["label"]))
    if applying:
        print("helm scratch: " + summary(rep))
    elif not rep["victims"]:
        print("helm scratch: nothing reapable — %d candidate%s, all live or "
              "fresh" % (rep["candidates"], "s"[:rep["candidates"] != 1]))


def cmd_scratch(args):
    """scratch [small|big|durable [--name N]] | gc [--apply] | status —
    the mount plane. A bare `helm scratch` surveys bytes AND inodes and prints
    the routing table; a class prints the routed dir (created) so a caller can
    `cd $(helm scratch big)`; `gc` reaps dead-session scratch (dry-run
    default)."""
    args = list(args)
    verb = args[0] if args else "status"
    if verb in ("status", "--json"):
        rows = survey()
        if "--json" in args:
            import json
            print(json.dumps({"survey": rows,
                              "routes": {c: route(c)[0] for c in CLASSES}},
                             indent=2))
            return 0
        _print_survey(rows)
        _print_routes()
        worst = max([worst_pct(u) for u in rows] or [0])
        print("helm scratch: worst mount %d%% (%s)" % (worst, level_of(worst)))
        return 0
    if verb == "gc":
        rest = args[1:]
        bad = [a for a in rest if a not in ("--apply", "--dry")]
        if bad:
            print("usage: helm scratch gc [--dry | --apply]", file=sys.stderr)
            return 2
        applying = "--apply" in rest
        _print_gc(gc(apply=applying), applying)
        return 0
    if verb in CLASSES:
        rest = args[1:]
        name = None
        if rest[:1] == ["--name"] and len(rest) > 1:
            name, rest = rest[1], rest[2:]
        if rest:
            print("usage: helm scratch %s [--name N]" % verb, file=sys.stderr)
            return 2
        path, why = resolve(verb, name)
        if not path:
            print("helm scratch: " + why, file=sys.stderr)
            return 1
        print(path)
        print("# %s" % why, file=sys.stderr)
        return 0
    print("usage: helm scratch [small|big|durable [--name N]] | gc [--dry | "
          "--apply] | status [--json]   (unknown verb '%s')" % verb,
          file=sys.stderr)
    return 2
