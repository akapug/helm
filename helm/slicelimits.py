"""helm slice-limits — agents.slice's CPU and memory limits, DERIVED from the
box (task/3847).

THE FAILURE THIS EXISTS FOR. The fleet's ceiling (task/325) was a set of
literals typed into drop-ins by hand. Each was sized from a count somebody
read that day, and each carried its own exit condition in a comment. In
the measured case three of them disagreed: a sizing drop-in said 1800%,
zz-owner-headroom.conf said 800% "until task/1574 lands" (task/1574 was
closed, and nothing lifted the cap), and a `systemctl set-property` file said
1600%. systemd merges the drop-ins of a unit in FILE NAME order across every
unit directory, and the last assignment wins, so zz-owner-headroom.conf won
at every daemon-reload and undid each hand-set value. The fleet was throttled
in 68% of periods while 7.8 of 24 CPUs sat idle.

ONE SOURCE, DERIVED FROM THE BOX:

  CPUQuota      = (online CPUs - owner reserve) x 100%
  MemoryMax     = MemTotal - owner reserve at the kill line
  MemoryHigh    = MemTotal - owner reserve at the throttle line
  CPUWeight     = 25, against the 100 of app.slice and session.slice
  MemorySwapMax = 0

Online CPUs are read from /sys/devices/system/cpu/online, never from nproc:
nproc honours the cgroup quota and the caller's affinity, and it read 8, 14
and 18 on one 24-CPU box. Each owner reserve is declared ONCE, in RESERVES,
as a fraction of the box with a floor, and rounded in the owner's favour. No
number here has to be remembered and lifted by a person: on another box, or
with CPUs taken offline, the derivation moves, and helm doctor names every
limit that no longer matches it.

`--apply` writes ONE drop-in, DROPIN, whose name sorts after every other
drop-in in use. It writes it FIRST, so the files load its values from then
on, then moves every other agents.slice drop-in that sets one of these
limits aside (a renamed file is a backup, and systemd reads only *.conf),
runs `systemctl --user daemon-reload`, and prints the rollback. It never
sets a limit lower than the live one unless `--lower` says so.
"""
import json
import math
import os
import re
import shlex
import subprocess
import sys
import time
from collections import namedtuple
from fractions import Fraction

from . import pk, seatceiling, timerhealth

UNIT = "agents.slice"
#: The per-seat slices' prefix drop-in directory: read for stopgap comments.
SEAT_DROPINS = "agents-.slice.d"
#: helm's one drop-in. Its name sorts after every other one in use ("zz-").
DROPIN = "zzz-helm-slice-limits.conf"
#: The suffix of a drop-in moved aside. It does not end in .conf, so systemd
#: does not read it, and the file is the backup the rollback restores.
MOVED = ".helm-slice-limits-"
SYS_ROOT = "/sys"
GIB = 1024 ** 3
INF = float("inf")

Reserve = namedtuple("Reserve", "fraction floor")
#: WHAT THE OWNER KEEPS, declared once. A reserve is the larger of its
#: fraction of the box and its floor. CPUs round up and memory limits round
#: down to whole GiB, so each rounding goes to the owner. On the 24-CPU,
#: 87.7 GiB box these give 1600% (the value task/3847 measured as right),
#: MemoryHigh 48G and MemoryMax 56G (the sizing that stopped systemd-oomd
#: killing seats at the throttle line).
RESERVES = {
    "cpu": Reserve(Fraction(1, 3), 4),
    "memory_max": Reserve(Fraction(36, 100), 8 * GIB),
    "memory_high": Reserve(Fraction(45, 100), 12 * GIB),
}
#: A quarter of app.slice's and session.slice's default 100: the owner wins
#: every contention 4:1, and the fleet still uses every idle CPU.
CPU_WEIGHT = 25
#: A runaway fleet process is killed, never swapped: swap thrash is what
#: freezes the owner's desktop.
SWAP_MAX = 0
#: The limits helm owns, in the order every line prints them.
MANAGED = ("CPUQuota", "MemoryHigh", "MemoryMax", "CPUWeight",
           "MemorySwapMax")
#: A comment that ends a stopgap on a task: "until task/1574 lands".
UNTIL = re.compile(r"\buntil\b[^.;]*?\btask/([0-9]+)", re.I)

Box = namedtuple("Box", "sys proc cgroup dirs home")
#: One drop-in file. `sets` is {limit: raw value} (None when the file would
#: not read), `other` is true when it also carries any other setting, and
#: `masked` when a same-named file in a higher-priority directory hides it.
Conf = namedtuple("Conf", "path name sets other comment masked")


def _user_load_path(root=None):
    """The full systemd.user(5) user-unit load path, highest priority first
    (Table 2, `--user`), as a tuple of absolute dirs. The fifth entry is the
    user's own unit dir, the one helm writes into. When `root` is a base dir,
    every entry is <root>/<rel> so a test plants the whole path under one tmp
    tree; otherwise the real env/absolute dirs are used. Each path names its
    final component as a single literal ("systemd/user"), the way systemd
    itself joins it, so the load path stays intact without a run of two
    consecutive "systemd"/"user" args."""
    j = os.path.join
    if root is not None:
        cfg_home = j(root, "home", ".config")
        cfg_dirs = j(root, "etc", "xdg")
        data_home = j(root, "home", ".local", "share")
        data_dirs = (j(root, "usr", "local", "share"), j(root, "usr", "share"))
        runtime = j(root, "runtime")
        etc, run = j(root, "etc"), j(root, "run")
        usr_local_lib, usr_lib = j(root, "usr", "local", "lib"), j(root, "usr", "lib")
    else:
        cfg_home = (os.environ.get("XDG_CONFIG_HOME")
                    or os.path.expanduser("~/.config"))
        cfg_dirs = os.environ.get("XDG_CONFIG_DIRS") or "/etc/xdg"
        data_home = (os.environ.get("XDG_DATA_HOME")
                     or os.path.join(os.path.expanduser("~"), ".local", "share"))
        data_dirs = os.environ.get(
            "XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":")
        runtime = os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()
        etc, run = "/etc", "/run"
        usr_local_lib, usr_lib = "/usr/local/lib", "/usr/lib"
    return (
        # 1. $XDG_CONFIG_HOME/systemd/user.control
        j(cfg_home, "systemd", "user.control"),
        # 2. $XDG_RUNTIME_DIR/systemd/user.control
        j(runtime, "systemd", "user.control"),
        # 3. $XDG_RUNTIME_DIR/systemd/transient
        j(runtime, "systemd", "transient"),
        # 4. $XDG_RUNTIME_DIR/systemd/generator.early
        j(runtime, "systemd", "generator.early"),
        # 5. $XDG_CONFIG_HOME/systemd/user — the user's own dir; helm writes here
        j(cfg_home, "systemd/user"),
        # 6. $XDG_CONFIG_DIRS/systemd/user (or /etc/xdg)
        j(cfg_dirs, "systemd/user"),
        # 7. /etc/systemd/user
        j(etc, "systemd/user"),
        # 8. $XDG_RUNTIME_DIR/systemd/user
        j(runtime, "systemd/user"),
        # 9. /run/systemd/user
        j(run, "systemd/user"),
        # 10. $XDG_RUNTIME_DIR/systemd/generator
        j(runtime, "systemd/generator"),
        # 11. $XDG_DATA_HOME/systemd/user
        j(data_home, "systemd/user"),
        # 12. $XDG_DATA_DIRS/systemd/user (one per dir)
        *(j(d, "systemd/user") for d in data_dirs),
        # 13. /usr/local/lib/systemd/user
        j(usr_local_lib, "systemd/user"),
        # 14. /usr/lib/systemd/user
        j(usr_lib, "systemd/user"),
        # 15. $XDG_RUNTIME_DIR/systemd/generator.late
        j(runtime, "systemd", "generator.late"),
    )


def box(sys_root=None, proc=None, cgroup=None, units=None, runtime=None,
        root=None):
    """The roots one reading takes. `dirs` are the unit directories that can
    hold an agents.slice drop-in, highest priority first, as systemd.unit(5)
    lists the full user-unit load path: a drop-in there hides a same-named one
    in a later directory. helm writes into `home`, the user's own unit
    directory. `root` (tests) plants every load-path dir under one tmp base
    so nothing reads the host."""
    sys_root = sys_root or SYS_ROOT
    proc = proc or seatceiling.HOST_PROC
    cgroup = cgroup or seatceiling.CGROUP_ROOT
    if root is not None:
        dirs = _user_load_path(root)
        home = dirs[4]
    else:
        units = os.path.normpath(units or timerhealth.user_unit_dir())
        runtime = runtime or os.path.join(
            os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid(),
            "systemd", "user.control")
        dirs = (os.path.join(os.path.dirname(units), "user.control"),
                runtime, units)
        home = units
    return Box(sys_root, proc, cgroup, dirs, home)


def _text(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None


def _may_read(b):
    """False when a reading of `b` would touch this host and seatceiling's
    switch is off. Otherwise it raises the host-read audit event first, as
    every other reading of the host's tree does."""
    if os.path.normpath(b.sys) != SYS_ROOT \
            and not seatceiling.reads_host(b.cgroup, b.proc):
        return True
    if not seatceiling.host_read_on():
        return False
    sys.audit(seatceiling.HOST_READ_EVENT, b.cgroup, b.proc)
    return True


# ---------------------------------------------------------------- the box

def cpulist(text):
    """How many CPUs a kernel cpulist names ("0-23", "0-3,8-11"), or None."""
    n = 0
    for part in (text or "").strip().split(","):
        lo, dash, hi = part.partition("-")
        if not lo.isdigit() or dash and not hi.isdigit():
            return None
        n += int(hi or lo) - int(lo) + 1
    return n or None


def read_box(b):
    """({"cpus", "cpus_from", "mem", "mem_from"}, "") or (None, why)."""
    if not _may_read(b):
        return None, "this box is not read: %s is off" % seatceiling.SWITCH
    online = os.path.join(b.sys, "devices", "system", "cpu", "online")
    raw = _text(online)
    cpus = cpulist(raw)
    if cpus is None:
        return None, "%s would not read as a CPU list" % online
    meminfo = os.path.join(b.proc, "meminfo")
    kb = [line.split()[1] for line in (_text(meminfo) or "").splitlines()
          if line.startswith("MemTotal:") and len(line.split()) > 1]
    if not kb or not kb[0].isdigit():
        return None, "%s carries no MemTotal" % meminfo
    return {"cpus": cpus, "cpus_from": "%s reads %s" % (online, raw.strip()),
            "mem": int(kb[0]) * 1024,
            "mem_from": "%s MemTotal %s kB" % (meminfo, kb[0])}, ""


def reserve(kind, total):
    """The owner's reserve of `kind` on a box with `total` of it."""
    r = RESERVES[kind]
    return max(Fraction(r.floor), r.fraction * total)


def _gib(n):
    return "%.2fG" % (float(n) / GIB)


def _share(f):
    """A reserve fraction as its reader says it: "45%", else "1/3"."""
    return "%d%%" % (f * 100) if (f * 100).denominator == 1 else str(f)


def derive(cpus, mem):
    """{limit: (value, why)} for a box with `cpus` online CPUs and `mem`
    bytes of MemTotal. CPUQuota is a percent; memory limits are bytes."""
    keep = math.ceil(reserve("cpu", cpus))
    r = RESERVES["cpu"]
    out = {"CPUQuota": (max(1, cpus - keep) * 100,
                        "(%d online CPUs - %d owner reserve) x 100%%; the "
                        "reserve is max(%d, %s of %d)"
                        % (cpus, keep, r.floor, _share(r.fraction), cpus))}
    for name, kind in (("MemoryHigh", "memory_high"),
                       ("MemoryMax", "memory_max")):
        keep, r = reserve(kind, mem), RESERVES[kind]
        out[name] = (max(1, math.floor((mem - keep) / GIB)) * GIB,
                     "MemTotal %s - %s owner reserve, down to whole GiB; the "
                     "reserve is max(%s, %s of MemTotal)"
                     % (_gib(mem), _gib(keep), _gib(r.floor),
                        _share(r.fraction)))
    out["CPUWeight"] = (CPU_WEIGHT, "against the 100 of app.slice and "
                        "session.slice: the owner wins every contention 4:1")
    out["MemorySwapMax"] = (SWAP_MAX, "a runaway fleet process is killed, "
                            "never swapped")
    return out


# ---------------------------------------------------------------- values

def parse_value(name, raw, mem=None):
    """A unit file's value of `name` as a number (INF for no limit), or None
    when it does not parse."""
    raw = (raw or "").strip()
    if name == "CPUQuota":
        m = re.match(r"^([0-9]+(?:\.[0-9]+)?)%$", raw)
        return INF if not raw else float(m.group(1)) if m else None
    if name == "CPUWeight":
        return 100 if not raw else 0 if raw == "idle" \
            else int(raw) if raw.isdigit() else None
    if not raw or raw.lower() == "infinity":
        return INF
    m = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*(%|[KMGTPE]?)$", raw, re.I)
    if not m or m.group(2) == "%" and not mem:
        return None
    if m.group(2) == "%":
        return int(mem * float(m.group(1)) / 100)
    unit = m.group(2).upper()
    return int(float(m.group(1)) * 1024 ** ("KMGTPE".index(unit) + 1)) \
        if unit else int(float(m.group(1)))


def render(name, value):
    """`value` as a unit file writes it."""
    if value == INF:
        return "" if name == "CPUQuota" else "infinity"
    if name == "CPUQuota":
        return "%g%%" % value
    if name != "CPUWeight" and value and value % GIB == 0:
        return "%dG" % (value // GIB)
    return "%d" % value


def show(name, value):
    """`value` as a line shows it."""
    if value is None:
        return "UNKNOWN"
    if value == INF:
        return "no quota" if name == "CPUQuota" else "infinity"
    if name not in ("CPUQuota", "CPUWeight") and value % GIB:
        return _gib(value)
    return render(name, value)


def read_live(b):
    """({limit: value or None}, why): agents.slice's cgroup files now. `why`
    is "" when the slice was found, and says why not otherwise."""
    if not _may_read(b):
        return {}, "this box is not read: %s is off" % seatceiling.SWITCH
    fleet, why = seatceiling.fleet_slice(b.cgroup, b.proc)
    if fleet is None or not os.path.isdir(fleet):
        return {}, why or "there is no %s at %s" % (UNIT, fleet)
    out = {}
    cpu = (_text(os.path.join(fleet, "cpu.max")) or "").split()
    if len(cpu) == 2 and cpu[1].isdigit() and int(cpu[1]):
        out["CPUQuota"] = INF if cpu[0] == "max" else \
            round(int(cpu[0]) * 100.0 / int(cpu[1]), 2) \
            if cpu[0].isdigit() else None
    weight = (_text(os.path.join(fleet, "cpu.weight")) or "").strip()
    out["CPUWeight"] = int(weight) if weight.isdigit() else None
    for name, f in (("MemoryHigh", "memory.high"), ("MemoryMax", "memory.max"),
                    ("MemorySwapMax", "memory.swap.max")):
        v = seatceiling.parse_size(_text(os.path.join(fleet, f)))
        out[name] = INF if v == seatceiling.UNLIMITED else \
            v if isinstance(v, int) else None
    return out, ""


# ---------------------------------------------------------------- the files

def parse_conf(text):
    """(sets, other, comment) of one unit file: the last assignment of each
    MANAGED limit in its [Slice] section, whether it carries anything else,
    and its comment lines joined."""
    sets, other, notes, section = {}, False, [], None
    for line in text.splitlines():
        s = line.strip()
        if not s or s[0] in "#;":
            notes.append(s.lstrip("#;").strip())
            continue
        if s.startswith("["):
            section = s
            continue
        key, eq, value = s.partition("=")
        if eq and section == "[Slice]" and key.strip() in MANAGED:
            sets[key.strip()] = value.strip()
        else:
            other = True
    return sets, other, " ".join(n for n in notes if n)


def _confs(dirs, sub, seen=None):
    """[Conf] for every *.conf in <dir>/<sub>, in systemd's merge order: by
    file name, a hidden same-named file right after the one hiding it. When
    `seen` is given, it is a shared name set: a file whose name is already in
    it (from an earlier, higher-priority dir or phase) is masked, and its own
    name is added to it. The set is returned alongside (and extended in place)."""
    out = []
    if seen is None:
        seen = set()
    for d in dirs:
        where = os.path.join(d, sub)
        try:
            names = sorted(n for n in os.listdir(where) if n.endswith(".conf"))
        except FileNotFoundError:
            continue
        except OSError:
            out.append(Conf(where, where, None, False, "", False))
            continue
        for n in names:
            text = _text(os.path.join(where, n))
            sets, other, comment = parse_conf(text) if text is not None \
                else (None, False, "")
            out.append(Conf(os.path.join(where, n), n, sets, other, comment,
                            n in seen))
            seen.add(n)
    return sorted(out, key=lambda c: (c.name, c.masked))


def unit_files(b):
    """(fragment Conf or None, [Conf] of agents.slice's drop-ins)."""
    path = os.path.join(b.home, UNIT)
    text = _text(path)
    frag = Conf(path, UNIT, *parse_conf(text), masked=False) \
        if text is not None else None
    return frag, _confs(b.dirs, UNIT + ".d")


def ours(b):
    return os.path.join(b.home, UNIT + ".d", DROPIN)


def effective(frag, dropins, mem=None):
    """{limit: (value, raw, path)} as systemd loads the files: the fragment,
    then each drop-in it reads in name order, the last assignment winning."""
    out = {}
    for c in [frag] + [d for d in dropins if not d.masked]:
        for k, raw in ((c and c.sets) or {}).items():
            out[k] = (parse_value(k, raw, mem), raw, c.path)
    return out


def statuses(numbers):
    """{task number: its status, or None when the task ledger will not say}."""
    if not numbers:
        return {}
    from . import tasks
    rows, unavailable = tasks.snapshot()
    return {n: None if unavailable else
            (rows.get("task/%d" % n) or {}).get("status") for n in numbers}


# ---------------------------------------------------------------- one pass

def reading(b=None, status=None):
    """Everything one pass knows, as one dict. `status` maps task numbers to
    statuses (statuses() by default), so an arm owns the task ledger."""
    b = b or box()
    frag, dropins = unit_files(b)
    facts, why = read_box(b)
    live, live_why = read_live(b)
    mem = facts and facts["mem"]
    seat = _confs(b.dirs, SEAT_DROPINS)
    gaps = [(c.path, int(n)) for c in ([frag] if frag else []) + dropins
            + seat if not c.masked for n in UNTIL.findall(c.comment)]
    known = (status or statuses)(sorted({n for _, n in gaps}))
    return {"box": b, "facts": facts, "why": why, "live": live,
            "live_why": live_why, "fragment": frag, "dropins": dropins,
            "effective": effective(frag, dropins, mem), "ours": ours(b),
            "derived": derive(facts["cpus"], mem) if facts else {},
            "stopgaps": [(p, n, known.get(n)) for p, n in gaps]}


def now_of(r, name):
    """(value, where) of `name` now: the live cgroup's, else the files',
    else systemd's default for a limit no file sets (no quota, infinity,
    weight 100). An unset limit is not an unknown one: read as unknown, the
    plan wrote the derivation over an unlimited slice without --lower
    whenever the slice was not running, and kept it when it was."""
    if r["live"].get(name) is not None:
        return r["live"][name], "live"
    got = r["effective"].get(name)
    return (got[0], "files") if got else (parse_value(name, ""), "default")


def plan(r, lower=False):
    """{limit: (value, note)} that --apply writes: the derived value, unless
    it is lower than the value now and `lower` is false."""
    out = {}
    for k in MANAGED:
        want = r["derived"][k][0]
        have, _ = now_of(r, k)
        if have is not None and want < have and not lower:
            out[k] = (have, "kept: %s is %s now, above the derived %s; "
                      "--lower sets it" % (k, show(k, have), show(k, want)))
            continue
        out[k] = (want, "nothing to compare" if have is None
                  else "same" if want == have
                  else "raise" if want > have else "lower (--lower)")
    return out


def others(r):
    """Every agents.slice drop-in besides helm's that sets a managed limit,
    hidden ones too: moving the file that hides one would expose it."""
    return [c for c in r["dropins"] if c.path != r["ours"]
            and (c.sets or c.name == DROPIN)]


def render_dropin(r, p):
    f = r["facts"]
    lines = ["# helm slice-limits (task/3847): the ONE source of %s's limits."
             % UNIT,
             "# Written by `helm slice-limits --apply`; `helm slice-limits` "
             "prints the derivation,",
             "# and helm doctor names any limit that stops matching it. Do "
             "not edit by hand.",
             "# Box: %s; %s." % (f["cpus_from"], f["mem_from"])]
    lines += ["# %s: %s" % (k, r["derived"][k][1]) for k in MANAGED]
    lines += ["# %s: %s" % (k, p[k][1]) for k in MANAGED
              if p[k][1].startswith("kept")]
    return "\n".join(lines + ["[Slice]"] + ["%s=%s" % (k, render(k, p[k][0]))
                                             for k in MANAGED]) + "\n"


def remainder(text):
    """`text` with each managed [Slice] assignment turned into a comment."""
    out, section = [], None
    for line in text.splitlines():
        s = line.strip()
        section = s if s.startswith("[") else section
        if s and s[0] not in "#;" and section == "[Slice]" \
                and s.partition("=")[0].strip() in MANAGED:
            line = "# moved to %s by helm slice-limits: %s" % (DROPIN, s)
        out.append(line)
    return "\n".join(out) + "\n"


def rollback_lines(moves, wrote, had):
    """The shell lines that undo an apply: `moves` are (backup, path)."""
    out = [] if had else ["rm -f %s" % shlex.quote(wrote)]
    out += ["if [ %s -ef %s ]; then rm -f %s; else mv -f %s %s; fi"
            % (shlex.quote(a), shlex.quote(b), shlex.quote(a),
               shlex.quote(a), shlex.quote(b)) for a, b in moves]
    return out + ["systemctl --user daemon-reload"]


def apply(r, lower=False, stamp=None):
    """Write helm's drop-in, move the others aside, reload.
    -> (rc, lines, rollback lines)."""
    p = plan(r, lower)
    text, path = render_dropin(r, p), r["ours"]
    had = os.path.exists(path)
    move = others(r)
    unknown = [k for k in MANAGED if now_of(r, k)[0] is None]
    unread = [c.path for c in r["dropins"] if c.sets is None]
    if unread:
        return 2, ["helm slice-limits: unreadable drop-in (%s); cannot "
                   "safely neutralize an unknown file" % ", ".join(unread)], []
    if unknown and not lower:
        return 2, ["helm slice-limits: existing limit UNKNOWN (%s); "
                   "refusing to lower it without --lower" % ", ".join(
                       unknown)], []
    hiders = [c.path for c in move if c.name == DROPIN]
    if hiders:
        return 2, ["helm slice-limits: %s masks helm's own drop-in; "
                   "refusing a write that cannot take effect first"
                   % ", ".join(hiders)], []
    visible = {c.name for c in move if not c.masked}
    later = [c.path for c in move if c.name > DROPIN
             and (not c.masked or c.name in visible)]
    if later:
        return 2, ["helm slice-limits: %s sorts before %s; refusing a "
                   "write that could expose an older limit before it moves"
                   % (path, ", ".join(later))], []
    if not move and _text(path) == text:
        if all(r["live"].get(k) == p[k][0] for k in MANAGED):
            return 0, ["helm slice-limits: %s already carries the derivation, "
                       "and the live slice matches; nothing written" % path], []
        # Files can agree after a failed reload while the running slice still
        # has the old limits. Re-enter the reload path without rewriting them.
        wrote = False
    else:
        wrote = True
    stamp = stamp or time.strftime("%Y%m%dT%H%M%S")
    moves, lines = [], []
    try:
        # helm's drop-in FIRST. It sorts last and sets every managed limit,
        # so from this rename on the files load the plan, whatever step
        # fails next or whatever kills this process. Written last, a stop
        # between two moves left the files loading the base unit's older
        # literals (MemoryHigh 32G on the measured box), or no limit at all.
        if wrote:
            if had:
                os.link(path, path + MOVED + stamp, follow_symlinks=False)
                moves.append((path + MOVED + stamp, path))
                lines.append("kept a copy: %s -> %s" % (path, moves[-1][0]))
            pk.atomic_write(path, text)
            lines.append("wrote %s" % path)
        for c in move:
            backup = c.path + MOVED + stamp
            # a file that also carries other settings is never absent: the
            # backup is a hard link, and its copy without the limit lines
            # replaces it in one rename
            if c.other:
                os.link(c.path, backup, follow_symlinks=False)
            else:
                os.rename(c.path, backup)
            moves.append((backup, c.path))
            if c.other:
                pk.atomic_write(c.path, remainder(_text(backup)))
            lines.append("moved aside: %s -> %s%s" % (
                c.path, backup, "; its other settings stay in %s" % c.name
                if c.other else ""))
    except OSError as exc:
        return 2, lines + ["helm slice-limits: stopped (%s: %s); the "
                           "rollback below undoes what was done"
                           % (exc.__class__.__name__, exc)], \
            rollback_lines(moves, path, had)
    back = rollback_lines(moves, path, had)
    try:
        done = subprocess.run(["systemctl", "--user", "daemon-reload"],
                              capture_output=True, text=True, timeout=30)
        fail = done.returncode and "exit %d: %s" % (
            done.returncode, (done.stderr or done.stdout).strip()[:200])
    except (OSError, subprocess.TimeoutExpired) as exc:
        fail = "%s: %s" % (exc.__class__.__name__, exc)
    if fail:
        return 1, lines + ["helm slice-limits: the files are written, but "
                           "`systemctl --user daemon-reload` failed (%s); "
                           "the limits apply at the next reload" % fail], back
    live, why = read_live(r["box"])
    off = [k for k in MANAGED if live.get(k) is not None
           and live[k] != p[k][0]]
    lines.append("daemon-reloaded; live %s reads %s" % (UNIT, "; ".join(
        "%s %s" % (k, show(k, live.get(k))) for k in MANAGED))
        if live else "daemon-reloaded; the live slice would not read (%s)"
        % why)
    if off:
        lines.append("helm slice-limits: live %s still differs from what was "
                     "written" % ", ".join(off))
    return (1 if off else 0), lines, back


# ---------------------------------------------------------------- reports

def _sets(c):
    return " ".join("%s=%s" % (k, v) for k, v in sorted((c.sets or {}).items()))


def report(r, lower=False):
    """(lines, drift): the bare verb's text, and whether anything here is
    not produced by the derivation."""
    f, drift = r["facts"], False
    lines = ["helm slice-limits: %s's limits, derived from this box "
             "(task/3847)" % UNIT]
    if not f:
        return lines + ["  cannot derive: %s" % r["why"]], True
    lines.append("  box: %d online CPUs (%s); MemTotal %s (%s)"
                 % (f["cpus"], f["cpus_from"], _gib(f["mem"]), f["mem_from"]))
    p = plan(r, lower)
    for k in MANAGED:
        want, why = r["derived"][k]
        have, where = now_of(r, k)
        eff = r["effective"].get(k)
        drift |= have != want or not eff or eff[2] != r["ours"]
        lines.append("  %-13s %-8s = %s" % (k, show(k, want), why))
        lines.append("  %-13s now %s (%s)%s; --apply writes %s (%s)" % (
            "", show(k, have), where or "not set",
            ", files load %s from %s" % (eff[1] or "(empty)",
                                         os.path.basename(eff[2]))
            if eff else "", show(k, p[k][0]), p[k][1]))
    if r["live_why"]:
        lines.append("  live slice not read: %s" % r["live_why"])
    for c in others(r):
        drift = True
        lines.append("  other source: %s%s: %s" % (
            c.path, " (hidden by a same-named file)" if c.masked else "",
            _sets(c)))
    for c in r["dropins"]:
        if c.sets is None:
            drift = True
            lines.append("  UNREADABLE drop-in: %s (a limit it sets is "
                         "UNKNOWN)" % c.path)
    if r["fragment"] and r["fragment"].sets:
        lines.append("  base unit %s sets %s (the lines above name the file "
                     "each value loads from)" % (r["fragment"].path,
                                                 _sets(r["fragment"])))
    for path, n, st in r["stopgaps"]:
        lines.append("  stopgap: %s waits 'until task/%d', which is %s"
                     % (path, n, st or "UNKNOWN"))
    return lines, drift


def doctor_rows(b=None, status=None):
    """[("ok" | "warn", text)] for helm doctor. A box with no agents.slice
    unit answers one OK and reads nothing else."""
    b = b or box()
    frag, dropins = unit_files(b)
    if frag is None and not dropins:
        live, why = read_live(b)
        if any(os.path.lexists(os.path.join(d, UNIT)) for d in b.dirs) \
                or live or "there is no %s" % UNIT not in why:
            return [("warn", "slice limits: no readable %s unit or drop-in "
                     "in %s, but the slice's absence is unproven (%s); "
                     "its limits are UNKNOWN" % (UNIT, b.home,
                                                  why or "live slice exists"))]
        return [("ok", "slice limits: no %s unit in %s; nothing to size"
                 % (UNIT, b.home))]
    r, rows = reading(b, status), []
    if r["facts"]:
        off = []
        for k in MANAGED:
            want = r["derived"][k][0]
            have, where = now_of(r, k)
            if have != want:
                eff = r["effective"].get(k)
                off.append("%s %s (%s%s; derived %s)" % (
                    k, show(k, have), where or "not set",
                    ", from %s" % os.path.basename(eff[2]) if eff else "",
                    show(k, want)))
        if off:
            rows.append(("warn", "slice limits: %d %s limit(s) differ from "
                         "the derivation from this box: %s. `helm "
                         "slice-limits` prints the derivation" % (
                             len(off), UNIT, "; ".join(off))))
    else:
        rows.append(("warn", "slice limits: cannot derive (%s)" % r["why"]))
    split = [k for k in MANAGED if r["live"].get(k) is not None
             and k in r["effective"] and r["effective"][k][0] is not None
             and r["live"][k] != r["effective"][k][0]]
    if split:
        rows.append(("warn", "slice limits: live %s differs from what the "
                     "unit files load (%s): the next daemon-reload changes it"
                     % (", ".join(split), "; ".join(
                         "%s live %s, files %s from %s" % (
                             k, show(k, r["live"][k]),
                             show(k, r["effective"][k][0]),
                             os.path.basename(r["effective"][k][2]))
                         for k in split))))
    loose = ["%s from %s" % (k, os.path.basename(r["effective"][k][2])
                             if k in r["effective"] else "no file")
             for k in MANAGED if r["effective"].get(k, (0, 0, ""))[2]
             != r["ours"]]
    extra = others(r)
    unread = [c.path for c in r["dropins"] if c.sets is None]
    parts = (["%d %s limit(s) load from a file the derivation did not "
              "write: %s" % (len(loose), UNIT, ", ".join(loose))]
             if loose else []) + (
        ["%d drop-in(s) besides helm's set %s limits, a second source: %s"
         % (len(extra), UNIT, "; ".join("%s (%s)" % (c.path, _sets(c))
                                        for c in extra))]
        if extra else []) + (
        ["%d drop-in(s) would not read, so a limit they set is UNKNOWN: %s"
         % (len(unread), ", ".join(unread))] if unread else [])
    if parts:
        rows.append(("warn", "slice limits: %s. `helm slice-limits --apply` "
                     "writes helm's one drop-in and moves the others aside, "
                     "with backups" % ". ".join(parts)))
    for path, n, st in r["stopgaps"]:
        if st != "closed" and st is not None:
            continue
        rows.append(("warn", "slice limits: %s is a stopgap 'until task/%d', "
                     "and task/%d is %s" % (
                         path, n, n, "closed: its exit condition was met and "
                         "nothing lifted it" if st else
                         "UNKNOWN (the task ledger would not say)")))
    seat = _seat_rows(r)
    rows += [row for row in seat if row[0] == "warn"]
    # a seat note (why a class has no per-seat High) prints as OK, after
    # the fleet's own verdict
    return (rows or [("ok", "slice limits: %s matches the derivation from "
                      "this box (%s), from helm's one drop-in" % (
                          UNIT, ", ".join("%s %s" % (k, show(
                              k, r["derived"][k][0])) for k in MANAGED)))]) \
        + [("ok", text) for level, text in seat if level == "note"]


def _seat_rows(r):
    """The seat half (seatlimits, task/4062): [] on a box with no seat."""
    from . import seatlimits
    try:
        return seatlimits.doctor_rows(seatlimits.extend(r))
    except Exception as exc:              # noqa: BLE001 — a half that cannot
        return [("warn", "seat limits: cannot tell (%s: %s)"  # look says so
                 % (exc.__class__.__name__, exc))]


# ---------------------------------------------------------------- the verb

USAGE = """usage: helm slice-limits [--seats [--role-for SEAT=ROLE,...]]
                          [--apply [--lower]] [--json]
       helm slice-limits --seat-props SEAT
  agents.slice's limits, derived from this box (task/3847): CPUQuota =
  (online CPUs from /sys/devices/system/cpu/online, never nproc, minus the
  owner's reserve) x 100%; MemoryHigh and MemoryMax = MemTotal minus the
  owner's reserve at the throttle line and at the kill line; CPUWeight 25
  against the owner's 100; MemorySwapMax 0. Each reserve is declared once
  (slicelimits.RESERVES) as a fraction of the box with a floor.
  Bare: print each limit with its derivation, its value now (live, else the
  unit files), every other drop-in that sets one, and every stopgap comment
  ('until task/N'); nothing is written. Exit 1 when anything is not produced
  by the derivation, 2 when the box will not read.
  --apply: write ONE drop-in, agents.slice.d/zzz-helm-slice-limits.conf,
  whose name sorts last; move every other drop-in that sets these limits
  aside (renamed with a .helm-slice-limits-<time> suffix, so the rename is
  the backup); run systemctl --user daemon-reload; print the rollback. No
  limit is set lower than its value now unless --lower is given.
  --json: the reading as JSON.
  --seats: the same for every seat slice (agents-<seat>.slice, task/4062):
  MemoryMax = a share of agents.slice's derived MemoryHigh by role (lead
  1/3, worker 1/6; seatlimits.SEAT_SHARES), MemoryHigh = 3/4 of it and
  never under 4G (1G above the measured 3G freeze), MemorySwapMax 0, with a
  table of every running seat slice. A cap under 5G holds no 4G High below
  it: that seat gets MemoryHigh=infinity (no per-seat throttle) and the
  plan, apply and doctor say so ("throttle left to the fleet line: a
  per-seat High under 4G freezes seats"). --apply writes
  agents-.slice.d/zzz-helm-seat-limits.conf (the lead class, every seat's
  default) and agents-<seat>.slice.d/zzz-helm-seat-role.conf for each
  running worker, moves other prefix drop-ins that set seat memory aside,
  reloads, and prints the rollback. A seat that is not running keeps the
  lead default: its role is read from its process, never its name, unless
  --role-for SEAT=ROLE[,SEAT=ROLE...] names it (ROLE lead or worker; it
  also overrides a running seat's). A stopped seat's old role file is moved
  aside as stale when the seat's default is lead (its name, or the lead
  posture its spawn register records), unless --role-for wrote it. It
  refuses to lower a running seat
  without --lower, or one whose anon + shmem x 1.5 does not fit its new
  MemoryHigh (its new MemoryMax when it gets no High), and it refuses, naming the file, when a seat would load a
  limit from any file other than helm's (one that sorts later in the seat's
  own directory, or a same-named one that hides helm's).
  Without --apply, --lower previews what `--apply --lower` does.
  --seat-props SEAT: print the set-property arguments a launch stamps on
  SEAT's slice (HELM_SEAT_ROLE=lead in the environment marks a lead)."""


def _json(r, lower):
    p = plan(r, lower) if r["facts"] else {}

    def num(v):
        return None if v is None else "infinity" if v == INF else v
    return {"facts": r["facts"], "why": r["why"],
            "reserves": {k: {"fraction": str(v.fraction), "floor": v.floor}
                         for k, v in RESERVES.items()},
            "derived": {k: {"value": num(v), "why": w}
                        for k, (v, w) in r["derived"].items()},
            "live": {k: num(v) for k, v in r["live"].items()},
            "live_why": r["live_why"],
            "files": {k: {"value": num(v), "raw": raw, "path": path}
                      for k, (v, raw, path) in r["effective"].items()},
            "plan": {k: {"value": num(v), "note": n}
                     for k, (v, n) in p.items()},
            "others": [{"path": c.path, "sets": c.sets, "masked": c.masked}
                       for c in others(r)],
            "stopgaps": [{"path": a, "task": n, "status": s}
                         for a, n, s in r["stopgaps"]],
            "dropin": r["ours"]}


def cmd_slice_limits(args, b=None, status=None):
    from .cli import guard_tail
    rc = guard_tail("helm slice-limits", args,
                    flags=("--apply", "--lower", "--json", "--seats"),
                    valued=("--seat-props", "--role-for"), usage=USAGE)
    if rc is not None:
        return rc
    if "--seat-props" in args:
        return _cmd_seat_props(args, b)
    if "--seats" in args:
        return _cmd_seats(args, b, status)
    if "--role-for" in args:
        print("helm slice-limits: --role-for only names a seat's role for "
              "--seats", file=sys.stderr)
        return 2
    lower = "--lower" in args
    if lower and "--apply" not in args:
        print("helm slice-limits: --lower only changes what --apply writes",
              file=sys.stderr)
        return 2
    r = reading(b, status)
    lines, drift = report(r, lower)
    if "--json" in args:
        print(json.dumps(_json(r, lower), sort_keys=True, default=str))
    else:
        print("\n".join(lines))
    if not r["facts"]:
        return 2
    if "--apply" not in args:
        if "--json" not in args:
            print("dry run: nothing written. --apply writes %s, moves %d "
                  "other drop-in(s) aside and runs systemctl --user "
                  "daemon-reload" % (r["ours"], len(others(r))))
        return 1 if drift else 0
    if r["fragment"] is None and not r["dropins"]:
        print("helm slice-limits: no %s unit in %s; nothing to size"
              % (UNIT, r["box"].home), file=sys.stderr)
        return 2
    rc, lines, back = apply(r, lower)
    print("\n".join(lines))
    if back:
        print("rollback:\n  " + "\n  ".join(back))
    return rc


def _cmd_seat_props(args, b=None):
    from . import seatlimits
    if len(args) != 2:
        print("helm slice-limits: --seat-props takes only its SEAT",
              file=sys.stderr)
        return 2
    seat = args[args.index("--seat-props") + 1]
    got, why = seatlimits.props(
        seat, os.environ.get(seatlimits.ROLE_ENV) == "lead", b)
    if got is None:
        print("helm slice-limits: cannot derive (%s)" % why, file=sys.stderr)
        return 2
    print(" ".join(got))
    return 0


def _cmd_seats(args, b=None, status=None):
    from . import seatlimits
    # bare, --lower previews what `--apply --lower` would write and refuse
    lower = "--lower" in args
    role_for, why = seatlimits.parse_role_for(
        args[args.index("--role-for") + 1]) if "--role-for" in args \
        else ({}, "")
    if role_for is None:
        print("helm slice-limits: %s" % why, file=sys.stderr)
        return 2
    r = seatlimits.reading(b, status, role_for)
    lines, drift = seatlimits.report(r, lower)
    if "--json" in args:
        print(json.dumps(seatlimits.as_json(r, lower), sort_keys=True,
                         default=str))
    else:
        print("\n".join(lines))
    if not r["classes"]:
        return 2
    if "--apply" not in args:
        if "--json" not in args:
            print("dry run: nothing written. --apply writes helm's seat "
                  "drop-ins, moves %d other prefix drop-in(s) aside and runs "
                  "systemctl --user daemon-reload"
                  % len(seatlimits.others(r)))
        return 1 if drift else 0
    rc, lines, back = seatlimits.apply(r, lower)
    print("\n".join(lines))
    if back:
        print("rollback:\n  " + "\n  ".join(back))
    return rc
