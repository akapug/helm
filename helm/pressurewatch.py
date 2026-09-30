"""helm pressure-watch — the fleet's own stall, read from OUTSIDE the fleet.

THE FAILURE THIS EXISTS FOR (task/3714). When agents.slice itself stalls,
at its memory.high or its CPU quota, every seat stalls together while the
host can read half idle, and the owner finds out because the box is
unusable. The one present-tense reader, seatceiling.fleet_pressure, runs
from Stop hooks, the roster and the scratch plane, and every one of those
runs INSIDE agents.slice, so the stall starves the reader with the seats it
reads. Hooks time out with lines that say only TIMED OUT.

THIS RUNS ON A USER TIMER IN app.slice, beside agents.slice and outside its
memory.high and CPU quota, once a minute (helm-pressure-watch.timer). Each
pass reads, through seatceiling.fleet_stall:
  * agents.slice cpu.pressure and memory.pressure (PSI, `some`);
  * agents.slice memory.current, memory.high and cpu.max, REPORTED ONLY;
  * the host's /proc/pressure/cpu and /proc/pressure/memory, and swap.
When it alarms it also reads, IN THIS ORDER:
  1. each seat slice under agents.slice from its cgroup files alone
     (slice_facts): memory.current against its own memory.high, and its PSI;
  2. only after the alarm is latched, logged and pushed on those facts, the
     PROCESS DETAIL (consumers): one /proc walk, a one-second CPU sample, and
     each listed process's program and seat. A stalled process's cmdline or
     environ read can block its reader, so this runs under its own budget,
     DETAIL_BUDGET_S, and the row says `Process detail UNKNOWN: <why>` when
     it did not arrive. Read first, it once held back every record of a
     stall: a pass blocked there until the unit's TimeoutStartSec kill.

IT KEYS ON SUSTAINED PSI STALL TIME, NEVER ON FULLNESS. agents.slice at
96-99% of its memory.high with memory PSI 0.00 is its healthy steady state
(measured on a fleet host), so a fullness alarm would page all day. The
stall share of a pass is the growth of the `some` total (microseconds) since
the last pass, over the wall time between them: stall time the kernel
measured. With no usable last pass (the first pass, a gap past MAX_GAP_S, a
total that went backwards, another slice) it is the kernel's own avg60.

THE EPISODE, LATCHED. A pass is HOT when either kind's share is at least
STALL_PCT. A run of hot passes that has lasted SUSTAIN_S is a STALL: ONE row
posts to #main addressed to @all and the owner, naming the top consumers,
and the owner's phone gets one push (helm/notify.py). While the episode is
open nothing more posts. It closes after the fleet reads CLEAR (every kind
under half of STALL_PCT) for CLEAR_S; only then can a new stall post again.
A short spike, a run that has not lasted SUSTAIN_S, is silent.

A PROCESS IS NAMED BY ITS PROGRAM ONLY: the basename of argv[0] (its first
word), or, for an interpreter, of the script it runs (`python3 x.py` is
`x.py`), plus the seat from HELM_CHAT_NAME in the environment of a process of
its slice when that is a plain name. No other word of a command line reaches
the row, the push, the alarm log or the journal: any argument can carry a
secret.

UNKNOWN IS NEVER CALM. A reading that could not be taken (no cgroup v2, no
agents.slice, a pressure file that will not read) is said ONCE, in one row
naming why, and again only after a readable pass; the pass exits 1. A pass
with NO kind read neither opens nor closes an episode and breaks a hot run.
A pass with ONE kind read never reads clear, and it is judged on the kind
that read: a stall measured there opens an episode.

AN ALARM ABOUT THE FLEET MUST NOT DEPEND ON THE FLEET'S CHAT. The phone push
does not wait on the chat row, and a push that landed is saved before any
post is tried. The row is written, with the whole alarm, to the alarm log
(alarm_log_path: <helm home>/_global/.state/pressure-watch-alarms.log)
before any post is tried, and to this pass's output, which is the unit's
journal (`journalctl --user -u helm-pressure-watch`), when the pass ends. A
chat row or a push that did not land stays owed and every pass retries it;
the chat row keeps one event id, so a retry after a post that did land
returns the first row rather than a second one. A row still owed when its
episode closes is kept: it posts late, marked CLOSED, under the same event id
(at most LATE_MAX such rows wait; an older one is dropped to the log). A push
still owed then is dropped, because the stall it would announce is over.
"""
import fcntl
import hashlib
import json
import os
import re
import sys
import threading
import time
import uuid

from . import seatceiling

INTERVAL_S = 60
#: How long a hot run must last before it is a stall.
SUSTAIN_S = 180
#: A pass is hot when a kind's stall share reaches this percent.
STALL_PCT = 20.0
#: An open episode closes after the fleet reads clear for this long.
CLEAR_S = 120
#: Past this gap between two passes nothing proves a run held across it.
MAX_GAP_S = 3 * INTERVAL_S
#: The window the kernel's avg60 covers, used as a first pass's window.
AVG_WINDOW_S = 60
#: How many slices and processes a row names.
TOP_N = 5
#: The sample window the CPU share of each process is measured over.
CPU_WINDOW_S = 1.0
#: The process detail's own budget, taken after the alarm is latched, logged
#: and pushed: past it the row says the detail is UNKNOWN.
DETAIL_BUDGET_S = 10.0
#: What a row says when the pass that opened it ended before the detail.
PENDING_WHY = ("the pass that opened this alarm ended before its process read "
               "finished")
#: Rows of closed episodes that may wait for the room at once.
LATE_MAX = 3
#: The unit's own limit on one pass, under the cadence.
SERVICE_TIMEOUT_S = 50
#: The alarm log rolls to one `.1` generation past this size.
LOG_MAX = 1 << 20

PCT_ENV = "HELM_PRESSURE_WATCH_PCT"
SUSTAIN_ENV = "HELM_PRESSURE_WATCH_SUSTAIN_S"
TIMER_ENV = "HELM_PRESSURE_WATCH_TIMER"
TIMER_OFF_VALUES = ("0", "off", "no", "false")
SERVICE_NAME = "helm-pressure-watch.service"
TIMER_NAME = "helm-pressure-watch.timer"
POSTER = "pressure-watch"
ROOM = "main"
PUSH_TITLE = "helm: the fleet is stalled"

_SERVICE = """[Unit]
Description=helm pressure watch — the fleet's own stall, read from outside agents.slice

[Service]
Type=oneshot
# OUTSIDE THE FLEET, by name. A reader inside agents.slice stalls with the
# seats it reads, and the only detector that ran from Stop hooks inside it
# posted nothing through a 42-minute stall. app.slice is a user service's
# default slice; it is written here so no drop-in can move the watcher in.
Slice=app.slice
WorkingDirectory=%(cwd)s
ExecStart=%(helm)s pressure-watch --post
TimeoutStartSec=%(timeout)ds
# Exit 1 means the pass RAN and FOUND a stall or an UNKNOWN reading; only
# exit 2 means the watcher itself failed.
SuccessExitStatus=1
# The watcher must never become the owner's problem: one pass is a few small
# reads, and a walk of /proc only when it alarms.
MemoryMax=256M
"""

_TIMER = """[Unit]
Description=helm pressure watch every minute

[Timer]
OnBootSec=%(interval)ds
OnUnitActiveSec=%(interval)ds
# The sustain window counts passes; a default one-minute AccuracySec could
# stretch one wait to two.
AccuracySec=1s

[Install]
WantedBy=timers.target
"""


# ---------------------------------------------------------------------------
# the timer
# ---------------------------------------------------------------------------

def timer_switched_off():
    """The HELM_PRESSURE_WATCH_TIMER value when it turns the install off."""
    value = os.environ.get(TIMER_ENV)
    if value is not None and value.strip().lower() in TIMER_OFF_VALUES:
        return value
    return None


def timer_units(interval=INTERVAL_S, inputs=None):
    """(service_path, service_text, timer_path, timer_text). WorkingDirectory
    is this checkout's shared root, never a literal; the unit directory is
    timerhealth.user_unit_dir(). `inputs` replaces per-install values
    (timerhealth.unit_values), which is how the drift census renders this."""
    from . import timerhealth, work
    helm_bin = os.path.join(os.path.expanduser("~"), ".local", "bin", "helm")
    udir = timerhealth.user_unit_dir()
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cwd = work.find_root(here) or here
    return (os.path.join(udir, SERVICE_NAME),
            _SERVICE % timerhealth.unit_values(
                {"helm": helm_bin, "cwd": cwd, "timeout": SERVICE_TIMEOUT_S},
                inputs),
            os.path.join(udir, TIMER_NAME),
            _TIMER % timerhealth.unit_values({"interval": interval}, inputs))


def ensure_timer(interval=INTERVAL_S):
    """(ok, detail) — install and enable the one-minute cadence.

    IDEMPOTENT: unchanged unit files are not rewritten and systemd is not
    reloaded for them. `ok` is True (enabled), False (failed) or None
    (switched off by TIMER_ENV: nothing written, nothing run)."""
    import shutil
    from . import timerhealth
    off = timer_switched_off()
    if off is not None:
        return None, ("install skipped by %s=%s: no unit file written, no "
                      "systemctl run" % (TIMER_ENV, off))
    if interval < 1:
        return False, "interval must be at least 1 second"
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return False, ("systemctl unavailable; run `helm pressure-watch "
                       "--post` from another scheduler")
    spath, service, tpath, timer = timer_units(interval)
    error, unchanged = timerhealth.install_user_timer(
        ((spath, service), (tpath, timer)), (TIMER_NAME,), systemctl,
        keep_unchanged=True, reload_unchanged=False)
    if error:
        return False, error
    return True, "%s every %ds (%s)" % (
        "already installed, unchanged," if unchanged else "installed",
        interval, tpath)


def timer_line():
    """Whether the timer is installed, in one line: its unit file, and the
    enable link `systemctl --user enable` makes. File reads only."""
    from . import timerhealth
    tpath = timer_units()[2]
    wants = os.path.join(timerhealth.user_unit_dir(), "timers.target.wants",
                         TIMER_NAME)
    if not os.path.exists(tpath):
        return ("timer: %s is NOT installed, so nothing watches the fleet "
                "from outside it; `helm pressure-watch --install-timer`"
                % TIMER_NAME)
    if not os.path.lexists(wants):
        return ("timer: %s is written but NOT enabled (%s); `helm "
                "pressure-watch --install-timer`" % (TIMER_NAME, tpath))
    return "timer: %s installed and enabled (%s)" % (TIMER_NAME, tpath)


# ---------------------------------------------------------------------------
# knobs, state, the alarm log
# ---------------------------------------------------------------------------

def _knob(name, default, cast, most=None):
    """A positive number from the environment, else the default."""
    raw = (os.environ.get(name) or "").strip()
    try:
        value = cast(raw)
    except ValueError:
        return default
    if value <= 0 or (most is not None and value > most):
        return default
    return value


def stall_pct():
    return _knob(PCT_ENV, STALL_PCT, float, most=100.0)


def sustain_s():
    return _knob(SUSTAIN_ENV, SUSTAIN_S, int)


def state_path():
    from . import home
    return os.path.join(home.global_dir(), ".state", "pressure-watch.json")


def alarm_log_path():
    from . import home
    return os.path.join(home.global_dir(), ".state",
                        "pressure-watch-alarms.log")


def _num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _save(path, state):
    from . import pk
    pk.atomic_write(path, json.dumps(state, sort_keys=True) + "\n")


def _log(text, now):
    """Append one entry to the alarm log; roll it past LOG_MAX. Never
    raises: the log is the record, the row and the push are the reach."""
    path = alarm_log_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            if os.path.getsize(path) > LOG_MAX:
                os.replace(path, path + ".1")
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("=== %s\n%s\n" % (_iso(now), text))
    except OSError:
        pass


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _hhmm(t):
    return time.strftime("%H:%MZ", time.gmtime(t))


# ---------------------------------------------------------------------------
# the stall share
# ---------------------------------------------------------------------------

def shares(reading, last, now):
    """{kind: (percent, window seconds or None)} for each kind that read.

    The window is the gap to the last pass when the `some` total can be
    differenced across it; None means the percent is the kernel's avg60."""
    out = {}
    gap = now - last["at"] if last and _num(last.get("at")) else None
    for kind in seatceiling.PSI_KINDS:
        got = reading.psi.get(kind)
        if got is None:
            continue
        some = got["some"]
        prev = (last or {}).get(kind)
        if gap is not None and 0 < gap <= MAX_GAP_S \
                and last.get("fleet") == reading.fleet \
                and isinstance(prev, int) and some["total"] >= prev:
            pct = 100.0 * (some["total"] - prev) / (gap * 1e6)
            out[kind] = (min(100.0, pct), gap)
        else:
            out[kind] = (some["avg60"], None)
    return out


def _share_text(got):
    if not got:
        return "no stall share read"
    parts = ["%s %d%%" % (k, round(got[k][0]))
             for k in seatceiling.PSI_KINDS if k in got]
    windows = [w for _p, w in got.values() if w is not None]
    return "%s %s" % (", ".join(parts),
                      "of the last %ds" % round(max(windows)) if windows
                      else "(kernel avg60)")


def _gb(n):
    return "%.2fG" % (n / float(1024 ** 3)) if _num(n) else "?"


def _host_text(reading):
    parts = []
    for kind in seatceiling.PSI_KINDS:
        got = reading.host.get(kind)
        parts.append("%s %s" % (kind, "?" if got is None
                                else "%d%%" % round(got["some"]["avg60"])))
    swap = reading.swap
    if swap is None:
        free = "swap unreadable"
    elif not swap[0]:
        free = "no swap"
    else:
        free = "swap %d%% free" % round(100.0 * swap[1] / swap[0])
    return "host %s (PSI some avg60), %s" % (", ".join(parts), free)


def _fleet_text(reading):
    share = seatceiling.share_of(reading.current, reading.high)
    quota = ("cpu quota %.1f cores" % reading.quota if reading.quota
             else "no cpu quota read")
    return "%s of %s memory.high%s (reported, never alarmed on), %s" % (
        _gb(reading.current), _gb(reading.high),
        "" if share is None else " (%d%%)" % round(100 * share), quota)


# ---------------------------------------------------------------------------
# the slices, from cgroup files; the processes, read after the alarm
# ---------------------------------------------------------------------------

_SEAT_NAME = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
#: The shape a program name must have to reach a room; anything else is "?".
_PROGRAM = re.compile(r"^[A-Za-z0-9_.@+:-]{1,40}$")
#: Interpreters, by basename: for these the program is the script they run.
_INTERPRETER = re.compile(r"^(python[0-9.]*|pypy[0-9.]*|node|nodejs|bun|deno|"
                          r"ruby|perl|bash|sh|dash|zsh)$")
#: Python's flags that take no value, so the word after one can still be the
#: script. Any other option (-c, -m, -W, -e, --require, bash -s ...) may make
#: the next word code, a value or a positional, and nothing after it is known
#: to be the script.
_PYTHON_FLAGS = frozenset(("-u", "-B", "-O", "-OO", "-s", "-S", "-E", "-I",
                           "-q", "-P", "-b", "-bb", "-d", "-v", "-x"))
#: Options that, even given WITH `=`, make what follows code or an argument
#: rather than the script: node's and bun's --eval= and --print= (the next
#: word is an argument, or the code), node's --run= (the rest go to the
#: package script). After one, nothing is known to be the script.
_CODE_OPTIONS = frozenset(("--eval", "--print", "--run"))


def _read(path, limit=1 << 16):
    try:
        with open(path, "rb") as fh:
            return fh.read(limit)
    except OSError:
        return None


def _text(path):
    raw = _read(path, 4096)
    return None if raw is None else raw.decode("utf-8", "replace")


def slice_facts(fleet):
    """{"slices", "more", "why"}: the seat slices directly under the fleet
    slice, biggest memory.current first, each against its own memory.high
    and with its own PSI. CGROUP FILES ONLY, never /proc: this is what the
    alarm and the push carry before any process is read, so no stalled
    process can hold them back. A slice whose cgroup.events says it holds no
    process is left out. The seat of a slice is process detail."""
    try:
        names = sorted(n for n in os.listdir(fleet)
                       if seatceiling.SEAT_SLICE.match(n))
    except OSError as exc:
        return {"slices": [], "more": 0,
                "why": "the seat slices under %s would not list (%s)"
                       % (fleet, exc.__class__.__name__)}
    slices = []
    for name in names:
        path = os.path.normpath(os.path.join(fleet, name))
        events = _text(os.path.join(path, "cgroup.events")) or ""
        if not os.path.isdir(path) or "populated 0" in events.splitlines():
            continue
        current, high = (seatceiling.parse_size(
            _text(os.path.join(path, f))) for f in ("memory.current",
                                                    "memory.high"))
        slices.append({"slice": path, "current": current, "high": high,
                       "ratio": seatceiling.share_of(current, high),
                       "psi": seatceiling.slice_psi(path)})
    slices.sort(key=lambda s: -(s["current"] if _num(s["current"]) else -1))
    return {"slices": slices[:TOP_N], "more": max(0, len(slices) - TOP_N),
            "why": ""}


def _rss(proc, pid):
    raw = _read(os.path.join(proc, str(pid), "statm"), 256)
    parts = raw.split() if raw else []
    if len(parts) < 2 or not parts[1].isdigit():
        return None
    return int(parts[1]) * os.sysconf("SC_PAGE_SIZE")


def _cpu_ticks(proc, pid):
    """utime + stime in clock ticks, read after the LAST ')' of stat (a comm
    may hold spaces and parens), or None."""
    raw = _read(os.path.join(proc, str(pid), "stat"), 4096)
    if not raw:
        return None
    tail = raw.rsplit(b")", 1)
    fields = tail[1].split() if len(tail) == 2 else []
    if len(fields) < 13 or not fields[11].isdigit() \
            or not fields[12].isdigit():
        return None
    return int(fields[11]) + int(fields[12])


def _script(name, args):
    """The script an interpreter runs, or None when it cannot be known: the
    first word that is not an option, reached across only options that
    cannot take the next word (a Python flag, or a --name=value whose name
    is not one of _CODE_OPTIONS)."""
    python = name.startswith(("python", "pypy"))
    for arg in args:
        if (python and arg in _PYTHON_FLAGS) \
                or (arg.startswith("--") and "=" in arg
                    and arg.split("=", 1)[0] not in _CODE_OPTIONS):
            continue
        return None if arg.startswith("-") else arg
    return None


def _program(argv):
    """THE PROGRAM, AND NO OTHER WORD OF THE COMMAND LINE: the basename of
    argv[0]'s first word (a title rewritten into argv[0] keeps only that),
    or, for an interpreter, the basename of the script it runs. Any other
    argument can carry a secret, and this goes to a room."""
    words = argv[0].split() if argv else []
    head = words[0] if words else ""
    name = "claude" if "/claude/versions/" in head \
        else os.path.basename(head)
    if _INTERPRETER.match(name):
        script = _script(name, argv[1:])
        base = os.path.basename(script.rstrip("/")) if script else ""
        if _PROGRAM.match(base):
            name = base
    return name if _PROGRAM.match(name) else "?"


def _cmd(proc, pid):
    """A process's name for a row: its program (_program), or its comm when
    it has no command line."""
    raw = _read(os.path.join(proc, str(pid), "cmdline"), 4096) or b""
    argv = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
    if argv:
        return _program(argv)
    comm = (_read(os.path.join(proc, str(pid), "comm"), 64) or b"").decode(
        "utf-8", "replace").strip()
    return comm if _PROGRAM.match(comm) else "?"


def _seat_of(path, pids, proc):
    """The seat a slice belongs to: HELM_CHAT_NAME from the environment of
    one of its processes (children inherit it), else None."""
    for pid in sorted(pids)[:8]:
        raw = _read(os.path.join(proc, str(pid), "environ"), 1 << 18)
        for item in (raw or b"").split(b"\0"):
            if item.startswith(b"HELM_CHAT_NAME="):
                name = item[len(b"HELM_CHAT_NAME="):].decode("utf-8",
                                                              "replace")
                if _SEAT_NAME.match(name):
                    return name
    return None


def consumers(root, proc, sleep=None, window=CPU_WINDOW_S, named=()):
    """{"seats", "words", "by_memory", "by_cpu", "why"}: THE PROCESS DETAIL.
    One /proc walk, a `window` CPU sample over every member's stat, each
    member's statm, the cmdline of each listed process and the environ of a
    few processes per named slice. A stalled process's cmdline or environ
    read can block its reader, so this runs only after the alarm is latched,
    logged and pushed, and only under DETAIL_BUDGET_S (process_detail).
    `seats` names the seat of each slice in `named` and of each slice a
    listed process sits in; `words` is each slice's seatceiling word."""
    members, trouble = seatceiling.slice_members(root, proc)
    if trouble:
        return {"seats": {}, "words": {}, "by_memory": [], "by_cpu": [],
                "why": trouble}
    readings, trouble = seatceiling.fleet_pressure(
        root=root, proc=proc, members=members, sleep=sleep)
    pids = sorted({p for ps in members.values() for p in ps})
    before = {p: _cpu_ticks(proc, p) for p in pids}
    (sleep or time.sleep)(window)
    after = {p: _cpu_ticks(proc, p) for p in pids}
    hz = float(os.sysconf("SC_CLK_TCK"))
    rows = []
    for path, ps in members.items():
        for pid in ps:
            a, b = before.get(pid), after.get(pid)
            rows.append({"pid": pid, "slice": path, "rss": _rss(proc, pid),
                         "cpu": None if a is None or b is None or b < a
                         else 100.0 * (b - a) / hz / window})
    by_memory = sorted([r for r in rows if r["rss"] is not None],
                       key=lambda r: -r["rss"])[:TOP_N]
    by_cpu = sorted([r for r in rows if r["cpu"]],
                    key=lambda r: -r["cpu"])[:TOP_N]
    wanted = (set(named) & set(members)) \
        | {r["slice"] for r in by_memory + by_cpu}
    seats = {path: _seat_of(path, members[path], proc)
             for path in sorted(wanted)}
    for row in by_memory + by_cpu:
        row["seat"] = seats.get(row["slice"])
        row["cmd"] = _cmd(proc, row["pid"])
    return {"seats": seats,
            "words": {path: getattr(r, "word", None)
                      for path, r in readings.items()},
            "by_memory": by_memory, "by_cpu": by_cpu, "why": trouble or ""}


def process_detail(root, proc, sleep, named):
    """consumers() under DETAIL_BUDGET_S: its answer, or a sentence saying
    why the process detail is UNKNOWN. It runs in a daemon thread the pass
    stops waiting for when the budget is spent: a read blocked in the kernel
    cannot be interrupted, and the alarm it would have held back is already
    latched, logged and pushed."""
    box = {}

    def read():
        try:
            box["got"] = consumers(root, proc, sleep, named=named)
        except BaseException as exc:  # noqa: BLE001 — the row says UNKNOWN
            box["why"] = "the process read failed (%s: %s)" % (
                exc.__class__.__name__, exc)

    worker = threading.Thread(target=read, name="pressure-watch-detail",
                              daemon=True)
    worker.start()
    worker.join(DETAIL_BUDGET_S)
    got = box.get("got")
    if isinstance(got, dict):
        return got
    return box.get("why") or (
        "the process read did not finish within %gs (a stalled process's "
        "/proc cmdline or environ read can block its reader)"
        % DETAIL_BUDGET_S)


def _slice_text(s, seats, word):
    name = os.path.basename(s["slice"])
    if seats is not None:
        name = "%s (%s)" % (name, seats.get(s["slice"]) or "seat unknown")
    share = "" if s["ratio"] is None else " (%d%%%s)" % (
        round(100 * s["ratio"]), ", %s" % word if word else "")
    psi = s["psi"]
    stall = ", ".join("%s %s" % (k, "?" if psi.get(k) is None
                                 else "%d%%" % round(psi[k]["some"]["avg60"]))
                      for k in ("memory", "cpu"))
    return "%s %s of %s high%s, stall %s (avg60)" % (
        name, _gb(s["current"]), _gb(s["high"]), share, stall)


def _proc_text(row, what):
    value = _gb(row["rss"]) if what == "memory" \
        else "%d%% cpu" % round(row["cpu"])
    return "%s pid %d %s, %s" % (value, row["pid"], row["cmd"],
                                 row.get("seat") or os.path.basename(
                                     row["slice"]))


def _detail_lines(facts, detail):
    """The slices, and the process detail as far as it is known. `detail`
    is consumers()'s answer, a sentence saying why it is UNKNOWN, or None
    for the alarm before the read (no process line at all)."""
    whole = isinstance(detail, dict)
    seats, words = (detail["seats"], detail["words"]) if whole else (None, {})
    out = ["Slices UNKNOWN: %s." % facts["why"]] if facts["why"] else []
    if facts["slices"]:
        out.append("Slices by memory: %s%s." % (
            "; ".join(_slice_text(s, seats, words.get(s["slice"]))
                      for s in facts["slices"]),
            "; and %d more" % facts["more"] if facts["more"] else ""))
    if not whole:
        if detail is not None:
            out.append("Process detail UNKNOWN: %s." % detail)
        return out
    if detail["why"]:
        out.append("Process detail UNKNOWN: %s." % detail["why"])
    for key, what in (("by_memory", "memory"), ("by_cpu", "cpu")):
        if detail[key]:
            out.append("Processes by %s: %s." % (what, "; ".join(
                _proc_text(r, what) for r in detail[key])))
    return out


# ---------------------------------------------------------------------------
# the rows
# ---------------------------------------------------------------------------

def _owner():
    """The owner's handle, or None when it cannot be derived."""
    try:
        from . import seats_identity
        name = seats_identity.owner_name()
    except Exception:                  # noqa: BLE001 — @all still reaches
        return None
    return name if isinstance(name, str) and _SEAT_NAME.match(name) else None


def render(reading, got, since, now, pct, sustain, facts, owner, phone_on,
           detail=None):
    """(row, push) for one episode: the row for #main, the short push.
    `facts` is slice_facts(), from cgroup files alone, and the push carries
    only those. `detail` is as _detail_lines takes it."""
    mention = " @%s" % owner if owner else ""
    mins = int((now - since) // 60)
    lines = ["[MEASURED] @all%s FLEET STALL: agents.slice has been stalled "
             "for %dm (since %s): %s stalled (PSI some; this alarm fires at "
             "%g%% held %ds). Every seat inside it slows together; this "
             "watcher runs outside it."
             % (mention, mins, _hhmm(since), _share_text(got), pct, sustain),
             "Fleet: %s. Host: %s." % (_fleet_text(reading),
                                       _host_text(reading))]
    lines.extend(_detail_lines(facts, detail))
    lines.append("One row per episode: the next posts only after the fleet "
                 "reads clear for %ds. `helm pressure-watch` reads it again."
                 % CLEAR_S)
    if not phone_on:
        lines.append("No phone is configured (HELM_NTFY_TOPIC or Telegram), "
                     "so this row and %s are the only places this alarm "
                     "lands." % alarm_log_path())
    top = facts["slices"][0] if facts["slices"] else None
    push = "FLEET STALL %dm: agents.slice %s stalled.%s" % (
        mins, _share_text(got),
        " Biggest slice: %s %s of %s high." % (
            os.path.basename(top["slice"]), _gb(top["current"]),
            _gb(top["high"])) if top else "")
    return "\n".join(lines), push


def _unknown_row(why, owner):
    return ("[pressure-watch]%s fleet stall UNKNOWN: %s. This watcher cannot "
            "tell whether agents.slice is stalled, so it has read no "
            "all-clear. It says this once, and again only after a pass that "
            "reads." % (" @%s" % owner if owner else "", why))


def _poster(post):
    """The post seam (text, room=, event_id=) -> row, or chat.post."""
    if post is not None:
        return post
    from . import chat

    def default(text, room, event_id):
        return chat.post(text, room=room, who=POSTER, event_id=event_id,
                         sign=False)
    return default


def _posted(post, text, event_id):
    """(landed, why). A raise or a row with no id did not land."""
    try:
        # KEYWORDS, never a third positional: the sender census
        # (tests/test_delivery_truth.py) reads a post's third positional
        # argument as its sender.
        row = post(text, room=ROOM, event_id=event_id)
    except Exception as exc:          # noqa: BLE001 — owed, retried next pass
        return False, "%s: %s" % (exc.__class__.__name__, exc)
    if isinstance(row, dict) and row.get("id"):
        return True, ""
    return False, "the post returned no row id"


def _push(ep, phone, save):
    """Try an open episode's owed push once. The PHONE GOES FIRST, before
    the process detail and before any row: an alarm about the fleet must not
    wait behind a process read or the fleet's chat, and a post to an
    unreachable node can take its whole timeout. The push is owed only while
    a phone is configured and it has not landed.

    A PUSH THAT LANDED IS SAVED AT ONCE. The @all row waits on the roster
    lock, which a stalled seat can hold, and a post that never returns ends
    with the unit's TimeoutStartSec kill. A push that was still owed on disk
    then would go out again on every later pass."""
    if ep.get("phone") != "owed":
        return
    try:
        pushed = phone.owner_push(ep["push"], title=PUSH_TITLE,
                                  receipt=("pressure-watch.push_failed",
                                           "fleet"))
    except Exception:                 # noqa: BLE001 — owed, retried next pass
        pushed = False
    if pushed:
        ep["phone"] = "pushed"
        save()


def _row(ep, post):
    """Try an owed chat row once, under its one event id."""
    if ep.get("chat") != "owed":
        return
    landed, why = _posted(post, ep["body"], ep["event"])
    ep["tries"] = int(ep["tries"]) + 1 if _num(ep.get("tries")) else 1
    if landed:
        ep["chat"] = "posted"
        ep.pop("chat_why", None)
    else:
        ep["chat_why"] = why


def _late(state):
    """The rows of closed episodes still owed to the room, oldest first."""
    got = state.get("late")
    return [item for item in (got if isinstance(got, list) else [])
            if isinstance(item, dict) and item.get("chat") == "owed"
            and isinstance(item.get("event"), str)
            and isinstance(item.get("body"), str)]


def _deliver_late(state, post):
    """Try each owed row of a closed episode once; keep the ones still owed."""
    owed = []
    for item in _late(state):
        _row(item, post)
        if item["chat"] == "owed":
            owed.append(item)
    if owed:
        state["late"] = owed
    else:
        state.pop("late", None)


# ---------------------------------------------------------------------------
# one pass
# ---------------------------------------------------------------------------

def _episode(state):
    """The open episode, or None. A record that is not whole is dropped: a
    latch that cannot be read must not hold back the next alarm."""
    ep = state.get("episode")
    if isinstance(ep, dict) and _num(ep.get("since")) \
            and isinstance(ep.get("event"), str) \
            and isinstance(ep.get("body"), str) \
            and isinstance(ep.get("push"), str):
        return ep
    return None


def _step(state, reading, got, now, pct):
    """Move the hot run and the clear run; -> hot."""
    last = state.get("last") if isinstance(state.get("last"), dict) else None
    gap = now - last["at"] if last and _num(last.get("at")) else None
    if gap is None or gap <= 0 or gap > MAX_GAP_S:
        # NOTHING PROVES A RUN HELD ACROSS A GAP, so both start over.
        state["run_since"] = state["clear_since"] = None
        start = now - AVG_WINDOW_S
    else:
        start = last["at"]
    hot = any(p >= pct for p, _w in got.values())
    clear = not reading.why and len(got) == len(seatceiling.PSI_KINDS) \
        and all(p < pct / 2.0 for p, _w in got.values())
    for key, on in (("run_since", hot), ("clear_since", clear)):
        was = state.get(key)
        state[key] = (was if _num(was) else start) if on else None
    if reading.fleet is not None and got:
        state["last"] = dict({"at": now, "fleet": reading.fleet},
                             **{k: reading.psi[k]["some"]["total"]
                                for k in got})
    return hot


def tick(apply=True, now=None, root=None, proc=None, post=None, phone=None,
         sleep=None, owner=None):
    """(rc, lines, data) for one pass. `apply` posts, pushes and moves the
    latch; without it the pass reads, prints what it would post and writes
    nothing. rc 0: calm, or a hot run that is not yet a stall; rc 1: a stall
    is open or the reading is UNKNOWN. `post` (text, room=, event_id=),
    `phone` (notify's configured() and owner_push()), `sleep` and `owner`
    are seams; the defaults are chat.post, helm.notify, time.sleep and the
    derived owner handle."""
    now = time.time() if now is None else now
    root = root or seatceiling.CGROUP_ROOT
    proc = proc or seatceiling.HOST_PROC
    reading = seatceiling.fleet_stall(root, proc)
    if reading is None:
        return 0, ["pressure-watch: not read — %s is off, so this box's "
                   "fleet is not read and nothing is said"
                   % seatceiling.SWITCH], {"read": False}
    if phone is None:
        from . import notify as phone
    seams = (root, proc, _poster(post), phone, sleep, owner)
    path = state_path()
    if not apply:
        return _pass(_load(path), reading, now, None, seams)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _pass(_load(path), reading, now, path, seams)


def _close(state, ep, now, path, notes):
    """The open episode, or None once the fleet has read clear for CLEAR_S.
    A ROW STILL OWED AT THE CLOSE IS KEPT: it moves to state["late"], marked
    CLOSED, under its own event id, and posts when the room can take it.
    Past LATE_MAX waiting rows the oldest is dropped, and the log says so."""
    since = state.get("clear_since")
    if not ep or not _num(since) or now - since < CLEAR_S:
        return ep
    notes.append("pressure-watch: the stall that began %s is CLEAR (clear "
                 "for %ds); the next stall posts a new row"
                 % (_hhmm(ep["since"]), now - since))
    owed = ep.get("chat") == "owed"
    late = _late(state) + ([{
        "event": ep["event"], "since": ep["since"], "chat": "owed",
        "body": "[pressure-watch] CLOSED, delivered late: the stall below "
                "began %s and read clear from %s; the room could not take "
                "this row while it was open.\n%s"
                % (_hhmm(ep["since"]), _hhmm(since), ep["body"])}]
        if owed else [])
    state["late"] = late[-LATE_MAX:]
    if path:
        _log("episode closed: stalled %s to %s; chat row %s%s"
             % (_hhmm(ep["since"]), _hhmm(now), ep.get("chat"),
                "; it stays owed and posts late, marked closed"
                if owed else ""), now)
        for gone in late[:-LATE_MAX]:
            began = _hhmm(gone["since"]) if _num(gone.get("since")) else "?"
            _log("late row DROPPED for the stall that began %s: %d closed "
                 "rows already wait for the room" % (began, LATE_MAX), now)
    return None


def _open(state, reading, got, now, pct, sustain, phone, owner):
    """A new episode on the SLICE-LEVEL FACTS alone -> (episode, alarm,
    args, named): `alarm` is the row as the alarm log's first entry takes
    it, `args` re-render it once the process detail is in, and `named` are
    the slices whose seats that detail reads. Until then the stored row says
    the detail is UNKNOWN, so a pass killed in between leaves a row that is
    true when a later pass posts it."""
    owner = _owner() if owner is None else owner
    phone_on = _phone_on(phone)
    facts = slice_facts(reading.fleet)
    args = (reading, got, state["run_since"], now, pct, sustain, facts, owner,
            phone_on)
    alarm, push = render(*args)
    return ({"since": state["run_since"], "opened": now,
             "event": "pressure-watch-%s" % uuid.uuid4().hex,
             "body": render(*args, detail=PENDING_WHY)[0], "push": push,
             "chat": "owed",
             "phone": "owed" if phone_on else "unconfigured"}, alarm, args,
            [s["slice"] for s in facts["slices"]])


def _addendum(ep, alarm):
    """The alarm log's second entry for an episode: the lines of its row
    that the logged alarm did not carry, so every line of it is on disk."""
    had = set(alarm.split("\n"))
    new = [line for line in ep["body"].split("\n") if line not in had]
    return "process detail for the stall that began %s:\n%s" % (
        _hhmm(ep["since"]),
        "\n".join(new) if new else "(none: no process sat in a seat slice)")


def _pass(state, reading, now, path, seams):
    """One pass over a loaded state. `path` None is the read-only pass."""
    root, proc, post, phone, sleep, owner = seams
    pct, sustain = stall_pct(), sustain_s()
    got = shares(reading, state.get("last")
                 if isinstance(state.get("last"), dict) else None, now)
    hot = _step(state, reading, got, now, pct)
    notes = []
    ep = _close(state, _episode(state), now, path, notes)
    opened = None
    if ep is None and hot and _num(state.get("run_since")) \
            and now - state["run_since"] >= sustain:
        ep, alarm, args, named = _open(state, reading, got, now, pct,
                                       sustain, phone, owner)
        opened = ep
    if ep is not None:
        was = ep.get("peak") if isinstance(ep.get("peak"), dict) else {}
        ep["peak"] = dict((k, v) for k, v in was.items() if _num(v))
        for kind, (p, _w) in got.items():
            ep["peak"][kind] = max(p, ep["peak"].get(kind, 0))
    state["episode"] = ep
    if not reading.why:
        state.pop("unknown", None)
    if path:
        if opened:
            _log(alarm, now)
        # LATCHED AND LOGGED BEFORE ANY REACH: a retry after a post that
        # landed and a write that did not carries the same event id, and
        # chat returns the first row instead of appending a second.
        _save(path, state)
        # THE PUSH GOES BEFORE THE PROCESS DETAIL AND BEFORE ANY ROW, the
        # UNKNOWN row included: a partly read pass can open an episode and
        # say UNKNOWN in the same pass, and the push waits behind neither.
        if ep is not None:
            _push(ep, phone, lambda: _save(path, state))
    if opened:
        detail = process_detail(root, proc, sleep, named)
        opened["body"] = render(*args, detail=detail)[0]
        if path:
            _log(_addendum(opened, alarm), now)
            _save(path, state)
    if path:
        if ep is not None:
            _row(ep, post)
        _deliver_late(state, post)
        if reading.why and state.get("unknown") != reading.why:
            notes.extend(_say_unknown(state, reading.why, post, owner))
        _save(path, state)
    lines = _status(reading, got, state, ep, now, hot, sustain, path) + notes
    if opened:
        lines.append(opened["body"])
    if not path:
        lines.append("(read only: nothing posted, pushed or written)")
    rc = 1 if ep is not None or reading.why else 0
    return rc, lines, _data(reading, got, state)


def _say_unknown(state, why, post, owner):
    """Post the one UNKNOWN row for this cause; -> [note] when it did not
    land (it is owed, and the next pass tries again with the same id)."""
    said = state.get("unknown_n")
    said = said if isinstance(said, int) and not isinstance(said, bool) \
        else 0
    event = "pressure-watch-unknown-%s" % hashlib.sha256(
        ("%s|%d" % (why, said)).encode("utf-8")).hexdigest()[:16]
    owner = _owner() if owner is None else owner
    landed, failed = _posted(post, _unknown_row(why, owner), event)
    if not landed:
        return ["pressure-watch: the UNKNOWN row did not post (%s); the next "
                "pass tries again" % failed]
    state["unknown"] = why
    state["unknown_n"] = said + 1
    return []


def _status(reading, got, state, ep, now, hot, sustain, path):
    """The pass's own lines, first: the stall, the run, or calm, and an
    UNKNOWN beside any of them."""
    what = "agents.slice %s; %s" % (_share_text(got), _host_text(reading))
    out = []
    if ep is not None:
        if not path and ep.get("chat") == "owed":
            row = "row would post (read only)"
        elif ep.get("chat") == "posted":
            row = "row posted"
        else:
            row = ("row NOT delivered (%s); retried every pass, and the alarm "
                   "stands in %s and this unit's journal"
                   % (ep.get("chat_why") or "not yet tried",
                      alarm_log_path()))
        out.append("pressure-watch: STALL since %s — %s; %s; phone %s"
                   % (_hhmm(ep["since"]), what, row, ep.get("phone")))
    elif not reading.why:
        out.append("pressure-watch: %s — %s" % (
            _run_word(state, now, hot, sustain), what))
    if reading.why:
        out.append("pressure-watch: UNKNOWN — %s; no all-clear is read from "
                   "a reading that was not taken" % reading.why)
    late = _late(state)
    if late:
        out.append("pressure-watch: %d row(s) of closed stalls still owed to "
                   "#main (%s); each pass retries them, marked closed"
                   % (len(late), late[-1].get("chat_why") or "not yet tried"))
    return out


def _phone_on(phone):
    try:
        return bool(phone.configured())
    except Exception:                 # noqa: BLE001 — a phone we cannot ask
        return False


def _run_word(state, now, hot, sustain):
    if hot and _num(state.get("run_since")):
        return "HOT for %ds of the %ds a stall must hold" % (
            now - state["run_since"], sustain)
    return "calm"


def _data(reading, got, state):
    return {"read": True, "fleet": reading.fleet, "why": reading.why,
            "psi": reading.psi, "current": reading.current,
            "high": reading.high, "quota": reading.quota,
            "host": reading.host, "swap": reading.swap,
            "shares": {k: {"percent": p, "window_s": w}
                       for k, (p, w) in got.items()},
            "state": state}


# ---------------------------------------------------------------------------
# the verb
# ---------------------------------------------------------------------------

_USAGE = """usage: helm pressure-watch [--post] [--json] | --install-timer
  The fleet's own stall, read from OUTSIDE agents.slice (task/3714). One
  pass reads agents.slice cpu.pressure and memory.pressure, its memory.high
  and cpu quota (reported only), the host's PSI and swap. It keys on
  SUSTAINED PSI stall time, never on how full the slice is: a pass is hot
  when a kind's stall share since the last pass reaches
  HELM_PRESSURE_WATCH_PCT (20), and a hot run that lasts
  HELM_PRESSURE_WATCH_SUSTAIN_S (180) is a stall. A stall posts ONE row per
  episode to #main for @all and the owner, naming the top slices and
  processes by memory and by cpu and the seat that launched each, and one
  push to the owner's phone; nothing more posts until the fleet reads clear
  for 120s. The episode is latched, logged and pushed from cgroup files
  before any process is read; the process detail follows within 10s or the
  row says it is UNKNOWN. A process is named by its program only (the
  script, for an interpreter), never by any other argument. An UNKNOWN
  reading is said once, never read as calm.
  Bare: read and print what a pass would do, and whether the timer is
  installed; nothing is posted or written. --post: the pass the timer runs
  (exit 1 = a stall is open or the reading is UNKNOWN, 2 = the watcher
  failed). --json: the reading as JSON. --install-timer: write and enable
  helm-pressure-watch.timer, a one-minute app.slice user timer."""


def cmd_pressure_watch(args):
    from .cli import guard_tail
    rc = guard_tail("helm pressure-watch", args,
                    flags=("--post", "--json", "--install-timer"),
                    usage=_USAGE)
    if rc is not None:
        return rc
    if "--install-timer" in args:
        other = sorted(set(args) - {"--install-timer"})
        if other:
            print("helm pressure-watch: --install-timer takes no other flag "
                  "(got %s)" % " ".join(other), file=sys.stderr)
            return 2
        ok, detail = ensure_timer()
        print("helm pressure-watch: %s%s" % (
            "timer NOT installed — " if ok is False else "", detail),
            file=sys.stderr if ok is False else sys.stdout)
        return 2 if ok is False else 0
    try:
        rc, lines, data = tick(apply="--post" in args)
    except Exception as exc:          # noqa: BLE001 — exit 2 is this case
        print("helm pressure-watch: the watcher itself failed (%s: %s)"
              % (exc.__class__.__name__, exc), file=sys.stderr)
        return 2
    if "--json" in args:
        print(json.dumps(data, sort_keys=True, default=str))
    else:
        for line in lines:
            print(line)
    if "--post" not in args and "--json" not in args:
        print(timer_line())
    return rc
