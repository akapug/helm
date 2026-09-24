#!/usr/bin/env python3
"""WHICH SEATS ARE RUNNING ON PHYSICS OLDER THAN THE PHYSICS ON DISK.

THE AXIS NOTHING ELSE MEASURES IS CONFIG-vs-RUNNING. Every plugin, MCP server
and hook a Claude seat has is resolved ONCE, at session start. A seat that has
been up for three days is running the settings that existed three days ago, and
nothing it is told afterwards can bring in a plugin enabled since. The seat
cannot see this from the inside -- it does not know the name of a tool it was
never given -- and every check helm already owns reads the FILE, sees the plugin
enabled, and reports healthy. physics._claude_plugins joins enabledPlugins
against the home's install records and warns "enabled but not installed": that
is CONFIG vs CONFIG, and it is satisfied by exactly the state that hides this.

THE MEASURED CASE (task/2885). A browser-driving plugin was enabled in a seat's
credhome settings.json three days AFTER that seat's process had started. Every
config axis said the plugin was present; the session had none of its tools. The
seat concluded it could not drive a browser and hand-built a DOM shim twice.
The owner had to notice it from outside, because no instrument here could.

TWO CLOCKS, ONE SUBTRACTION, AND BOTH ARE ALREADY ON THE HOST. A process start
is /proc field 22 in the uptime frame (procage); a settings file's mtime is
wall clock. Rather than convert between frames -- which needs btime and invites
a boot-time bug -- BOTH are reduced to an AGE against the same `now`. A seat is
behind when it is OLDER than the configuration it is supposed to be honouring.

WHAT THIS CANNOT SEE, stated here because the output cannot say it. An mtime
proves the file was WRITTEN, never that its contents changed, so a seat named
here may be behind a touch rather than behind a plugin. That direction is the
safe one: the alternative -- knowing what the seat actually read at launch --
is not recoverable from outside the process, and a check that waits for it
reports nothing at all. It watches the seat's OWN config home only: a project
layer under the seat's cwd moves with every checkout, and folding that in would
make every seat permanently stale and the signal worthless.

A SEAT WITH NO NAME IS NOT A SEAT THE OWNER CAN RESTART. Rows helm cannot
attribute to a seat are counted, never named, and the count says so -- the one
thing worse than not reporting them is telling the owner to restart a pid.
Subagent processes are excluded outright: they inherit the seat's environ, they
are born and die inside one turn, and nobody restarts one.
"""
import os
import time

#: The files a seat reads at session start and never again, relative to the
#: seat's own config root. `settings.json` carries enabledPlugins and hooks;
#: the install record is what an enabled plugin is joined against. A home that
#: has neither of these is not behind anything -- absence is not drift.
WATCHED = ("settings.json", os.path.join("plugins", "installed_plugins.json"))
#: How far a config may post-date a seat's start before the seat is called
#: behind it. A launch writes its own settings around the exec, so the two
#: stamps straddle by seconds in the ordinary case; this keeps that from
#: reading as drift.
GRACE_S = 60
#: An mtime this far in the FUTURE is a clock nobody here understands, and a
#: subtraction against it would mint an enormous fake drift. Reported as an
#: input that could not be trusted, never as a finding.
SKEW_S = 60
#: How many stale seats are named in one report before the rest are counted.
NAME_CAP = 5


def _mtime(path):
    """(mtime, why) — the float, or None with the reason it is not a number."""
    try:
        return os.stat(path).st_mtime, None
    except FileNotFoundError:
        return None, "absent"
    except OSError as e:                  # EACCES, EIO, a symlink loop
        return None, "unreadable (%s)" % e.__class__.__name__


def _age(pid):
    from . import procage
    return procage.process_age(pid)


def span(seconds):
    """A duration an owner reads, not an operator: `3 days`, `4 hours`."""
    seconds = int(max(0, seconds))
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            n = seconds // size
            return "%d %s%s" % (n, unit, "s"[:n != 1])
    return "%d seconds" % seconds


def state(census=None, now=None, age=None, stat=None):
    """What is known about seats running behind their own configuration.

    `census`, `now`, `age` and `stat` are injectable so an arm can OWN this
    input instead of consuming live host pressure — and ALL FOUR are needed to
    isolate one: a census alone still leaves the verdict deciding against the
    real /proc and the real filesystem, so the answer would stay a property of
    the box that ran it.

    Keys. `read` False with `why` set: the process enumeration failed, so zero
    stale seats means NOTHING WAS READ and a consumer may not report health.
    `partial`: the census could not certify completeness, so `measured` and
    `stale` are a FLOOR. `stale`: named seats, worst first. `blind`: a row that
    was reached and could not be decided, each carrying WHICH input failed —
    never dropped, because a silent drop reads as a clean seat.
    """
    from . import seats_common, session

    now = time.time() if now is None else now
    age = _age if age is None else age
    stat = _mtime if stat is None else stat
    out = {"read": True, "why": None, "partial": False, "measured": 0,
           "stale": [], "unnamed_stale": 0, "blind": []}
    if census is None:
        try:
            census = session._proc_claude_census()
        except Exception as e:            # noqa: BLE001 — the census parses
            out["read"] = False           # external /proc and transcript state
            out["why"] = "the seat census raised %s: %s" % (
                e.__class__.__name__, e)
            return out
    if census.get("listing_failed"):
        out["read"] = False
        out["why"] = ("the /proc enumeration failed, so no seat could be "
                      "measured — zero rows is a failed probe, not an "
                      "up-to-date fleet")
        return out
    out["partial"] = bool(census.get("census_partial"))
    worst = {}
    for row in census.get("rows") or ():
        if row.get("child"):
            continue                      # a subagent is nobody's to restart
        pid = row.get("pid")
        raw = (row.get("environ") or {}).get("HELM_CHAT_NAME")
        seat = seats_common._seat_label(raw) if raw else None
        root = row.get("root")
        if not root:
            out["blind"].append({
                "seat": seat, "pid": pid,
                "why": "its config root could not be trusted (%s), so which "
                       "settings it honours is UNKNOWN"
                       % (row.get("declared_reason") or "unstated")})
            continue
        started = age(pid)
        if started is None:
            out["blind"].append({
                "seat": seat, "pid": pid,
                "why": "its start time could not be read, so how old it is "
                       "against its settings is UNKNOWN"})
            continue
        behind, skewed, unreadable = [], [], []
        for rel in WATCHED:
            mt, why = stat(os.path.join(root, rel))
            if mt is None:
                if why != "absent":       # absence is not drift; a config that
                    unreadable.append((rel, why))   # cannot be READ is UNKNOWN
                continue
            written = now - mt
            if written < -SKEW_S:
                skewed.append(rel)
                continue
            gap = started - written       # both ages against ONE `now`
            if gap > GRACE_S:
                behind.append((rel, int(gap)))
        if unreadable or skewed:
            out["blind"].append({
                "seat": seat, "pid": pid,
                "why": "; ".join(
                    ["%s is %s" % (r, w) for r, w in unreadable]
                    + ["%s carries a timestamp in the future" % r
                       for r in skewed])})
            continue
        out["measured"] += 1
        if not behind:
            continue
        if seat is None:
            out["unnamed_stale"] += 1
            continue
        gap = max(g for _rel, g in behind)
        prior = worst.get(seat)
        # ONE LINE PER SEAT, WORST FIRST. A relaunched seat can leave an older
        # process wearing the same name; the owner restarts a SEAT, so naming
        # it twice would read as two problems and cure neither.
        if prior is None or gap > prior["behind_s"]:
            worst[seat] = {"seat": seat, "pid": pid, "root": root,
                           "behind_s": gap, "started_s": int(started),
                           "files": tuple(sorted(behind))}
    out["stale"] = sorted(worst.values(),
                          key=lambda r: (-r["behind_s"], r["seat"]))
    return out


# ---------------------------------------------------------------------------
# THE AUTO-MEMORY BASE, a case of the same axis with a CERTAIN cost.
#
# hooks.MEMORY_BASE_ENV moves a linked home's memory dir onto the real path,
# which is what lets a memory write through without a permission prompt. But
# Claude Code resolves that dir ONCE per process: its resolver is memoized on
# a key of (project, trust, canonical root), the variable is not in the key,
# and only a `/cd` into a fresh session clears it (read in the installed
# program, 2.1.280 and 2.1.281). So a session that began before its home
# carried the variable keeps the LINKED path for as long as it lives, even
# after the settings reload puts the variable in its environment, and every
# memory write it makes stops on a permission prompt until it is relaunched.
# Measured: one such seat sat on one such prompt for over an hour.
#
# WHAT THE SESSION STARTED WITH IS READ OFF ITS OLDEST CHILDREN. Claude Code
# hands its own process.env to every child it spawns, and the MCP servers it
# starts in its first seconds keep that copy for life. A child born in the
# session's first MEMORY_WITNESS_S therefore shows whether the variable was
# in force when the dir was resolved; a child born later shows only what the
# reload added since. Measured on a live host: a linked-home seat started
# after the render had it in 5 of 5 early children, and one started before
# it in 0 of 6, while that seat's NEW Bash children carried it.
MEMORY_WITNESS_S = 120


def _stat_fields(pid, proc="/proc"):
    """(ppid, start_ticks) from /proc/<pid>/stat, or None."""
    try:
        with open(os.path.join(proc, str(pid), "stat"), "rb") as fh:
            raw = fh.read()
        tail = raw[raw.rindex(b")") + 2:].split()
        return int(tail[1]), int(tail[19])
    except (OSError, ValueError, IndexError):
        return None


def _env_value(pid, name, proc="/proc"):
    """(present, value) of one variable in /proc/<pid>/environ; None when the
    environ cannot be read, which is never the same answer as absent."""
    try:
        with open(os.path.join(proc, str(pid), "environ"), "rb") as fh:
            raw = fh.read(1 << 20)
    except OSError:
        return None
    key = name.encode() + b"="
    for kv in raw.split(b"\0"):
        if kv.startswith(key):
            return True, kv[len(key):].decode("utf-8", "replace")
    return False, None


def early_witnesses(proc="/proc"):
    """-> witness(pid, start_ticks) -> [value-or-None] | None: the variable as
    each child born in the parent's first MEMORY_WITNESS_S saw it. ONE /proc
    walk serves every parent; a failed walk answers None for all of them."""
    from .hooks import MEMORY_BASE_ENV
    hz = os.sysconf("SC_CLK_TCK") or 100
    table = None

    def witness(pid, start_ticks):
        nonlocal table
        if table is None:
            try:
                pids = [int(p) for p in os.listdir(proc) if p.isdigit()]
            except OSError:
                return None
            table = {}
            for p in pids:
                f = _stat_fields(p, proc)
                if f:
                    table.setdefault(f[0], []).append((p, f[1]))
        out = []
        for child, born in table.get(int(pid), ()):
            if not 0 <= (born - int(start_ticks)) / float(hz) <= MEMORY_WITNESS_S:
                continue
            got = _env_value(child, MEMORY_BASE_ENV, proc)
            if got is not None:
                out.append(got[1])
        return out
    return witness


def memory_base_state(census=None, witness=None, base=None, age=None):
    """Sessions on a linked home that began BEFORE their home carried the
    memory base -> {read, why, measured, predates, blind}.

    `predates` rows are CERTAIN: the session's own start-time environment,
    read off a child born with it, lacks the base its home needs. `blind`
    rows could not be decided and say which input failed; they are never
    folded into a clean seat. A session whose home needs no base is not in
    scope and is not counted. Every input is injectable so an arm owns it."""
    from . import seats_common, session
    from .hooks import MEMORY_BASE_ENV, memory_base
    base = memory_base if base is None else base
    age = _age if age is None else age
    out = {"read": True, "why": None, "measured": 0, "predates": [],
           "blind": []}
    if census is None:
        try:
            census = session._proc_claude_census()
        except Exception as e:            # noqa: BLE001 — see state()
            out.update(read=False, why="the seat census raised %s: %s"
                       % (e.__class__.__name__, e))
            return out
    if census.get("listing_failed"):
        out.update(read=False, why="the /proc enumeration failed, so no "
                   "session could be measured")
        return out
    witness = early_witnesses() if witness is None else witness
    for row in census.get("rows") or ():
        root = row.get("root")
        if row.get("child") or not root:
            continue
        want = base(root)
        if not want:
            continue
        env = row.get("environ") or {}
        raw = env.get("HELM_CHAT_NAME")
        who = {"seat": seats_common._seat_label(raw) if raw else None,
               "pid": row.get("pid"), "root": root,
               "home": os.path.basename(root.rstrip(os.sep)),
               "session": row.get("session"), "started_s": age(row.get("pid"))}
        if env.get(MEMORY_BASE_ENV) == want:
            out["measured"] += 1          # launched with it: resolved right
            continue
        seen = witness(row.get("pid"), row.get("start") or 0) \
            if row.get("start") else None
        if seen is None:
            out["blind"].append(dict(who, why="its children could not be read, "
                                     "so the environment it started with is "
                                     "UNKNOWN"))
            continue
        if not seen:
            out["blind"].append(dict(who, why="no child from its first %ds "
                                     "is still alive to show the environment "
                                     "it started with" % MEMORY_WITNESS_S))
            continue
        out["measured"] += 1
        if want not in seen:
            out["predates"].append(dict(who, witnesses=len(seen)))
    return out
