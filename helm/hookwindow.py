"""The hook END window: two hours and more of hook END rows, each carrying
three raw readings of the box taken at its END (task/3259).

WHY A SECOND STREAM. hook-latency.jsonl rotates by bytes at fleet rate: its
three 1 MiB generations hold 1.6 to 3.6 minutes of rows (measured on the
agents box), so a question over two hours of hooks has no rows to be answered
from there. Growing those generations is not the cure: every span writes a
START and an END of about 500 bytes each, about 800 KB a minute at fleet rate
(measured: 4,982 rows in 3.1 min), so two hours would be about 100 MB.
This stream keeps a compact row of 150 to 170 bytes for the END of every event
span, every installed handler span and the PostToolUse record and delivery
phases, plus every END that timed out or was cancelled, at any stage.

ROTATED BY AGE, BOUNDED BY BYTES. A generation rolls when its first row is
ROTATE_AGE_NS old, and GENERATIONS are kept, so once the fleet has run that
long the stream reaches back at least (GENERATIONS - 1) x ROTATE_AGE_NS = 2 h.
MAX_BYTES per generation is the backstop that keeps a burst from growing it
without bound. A byte roll shortens the window, so the report prints the first
and last row the stream holds rather than assuming the two hours.

THREE RAW READINGS, AND NO LABEL. Each row carries, under these names:
  load1               field 1 of /proc/loadavg: the kernel's 1-minute
                      exponentially damped average of runnable and
                      uninterruptible tasks
  cpus                os.cpu_count(): how many cpus this box has
  psi_cpu_some_avg10  `some avg10` of /proc/pressure/cpu: the percent of time
                      in which at least one runnable task waited for a cpu, a
                      running average over a 10-SECOND window
They are read after the END row is stamped and before that row is written to
any stream (`hooklatency._Event.write` asks for them first), so a reading is
never taken after this process's own telemetry writes. For a timeout that is
after the budget expired and the timed-out span unwound to its END, not the
instant the budget expired; and load1 and avg10 are averages, not instant
values. A value that is not an int or a float, is negative, NaN or infinite,
or is past its bound (`_DOMAIN`) is stored as null: no reading, never a zero
and never a crash. Nothing here decides whether a late hook was the box or the
hook; the report prints the readings and judges nothing.

BEST EFFORT, LIKE EVERY WRITER HERE. A busy lock waits at most LOCK_WAIT_S and
the row is then not written; nothing on a hook path raises from this module.
The incidents stream beside hook-latency.jsonl still keeps every timeout for a
day.
"""
import fcntl
import json
import os
import sys
import time

from . import hooklatency

ROTATE_AGE_NS = 3600 * 10**9
GENERATIONS = 3
MAX_BYTES = 16 * 1024 * 1024
#: The longest a hook waits for this stream's lock before it gives the row up.
LOCK_WAIT_S = 0.01
#: A first line longer than this is not read for its age; the byte cap still
#: bounds that generation.
HEAD_BYTES = 4096
#: Every event span, every installed handler span, and the two PostToolUse
#: phases a composite runs under their own budgets.
KEPT_STAGES = frozenset(("event", "record", "delivery")) \
    | frozenset(hooklatency.HANDLER_STAGES)
#: The outcomes whose quantiles are measured -- the same conditional
#: population `hooklatency.report` uses.
MEASURED = ("completed", "nonzero", "acquired")
#: The proc tree the readings come from; a test points it at a fake one.
PROC = "/proc"
#: The three raw readings, in the order the report prints them.
FIELDS = ("load1", "cpus", "psi_cpu_some_avg10")
#: What each number a row carries may hold besides null: the kinds, the least
#: and the most. load1 counts tasks, so it cannot pass the kernel's PID limit
#: (PID_MAX_LIMIT, 4,194,304 on 64-bit); a cpu count past 65,536 is far above
#: anything Linux reports; a PSI share is a percent. `bool` is not a number.
_DOMAIN = {"load1": ((int, float), 0, 4 * 1024 * 1024),
           "cpus": ((int,), 1, 1 << 16),
           "psi_cpu_some_avg10": ((int, float), 0, 100),
           "ms": ((int, float), 0, hooklatency.MAX_WALL_MS)}


def path():
    return hooklatency.path() + ".window"


def _in_domain(key, value):
    """`value` when `_DOMAIN[key]` admits it, else None. A NaN fails every
    comparison and an infinity passes no bound, and an int is compared with
    its bound exactly, never converted, so no value can raise here."""
    kinds, least, most = _DOMAIN[key]
    return value if type(value) in kinds and least <= value <= most else None


def _load1(proc):
    try:
        with open(os.path.join(proc, "loadavg"), "rb") as f:
            return float(f.read(128).split()[0])
    except Exception:
        return None


def _psi_cpu_some_avg10(proc):
    """The `some avg10` share from <proc>/pressure/cpu, or None. A kernel
    without PSI has no file; that is no reading, never a zero."""
    try:
        with open(os.path.join(proc, "pressure", "cpu"), "rb") as f:
            text = f.read(512)
        for line in text.splitlines():
            if line.startswith(b"some "):
                for field in line.split():
                    if field.startswith(b"avg10="):
                        return float(field[6:])
    except Exception:
        pass
    return None


def _cpu_count():
    try:
        return os.cpu_count()
    except Exception:
        return None


def read_box(proc=None):
    """The three raw readings now, each held to its domain; any may be None.
    Read fresh every time, with no cache. Never raises."""
    proc = PROC if proc is None else proc
    raw = {"load1": _load1(proc), "cpus": _cpu_count(),
           "psi_cpu_some_avg10": _psi_cpu_some_avg10(proc)}
    return {key: _in_domain(key, value) for key, value in raw.items()}


def keeps(row):
    """True for an END row this stream keeps."""
    return (isinstance(row, dict) and row.get("kind") == "END"
            and (row.get("outcome") in hooklatency.RETAINED
                 or row.get("stage") in KEPT_STAGES))


def note(row, box=None):
    """Keep one hook-latency END row here, with `box` (the readings
    `read_box` took for it), or readings taken now. True only for a full
    append. A row the reader would not admit is not written. Never raises."""
    try:
        if not keeps(row) or type(row.get("time_ns")) is not int:
            return False
        box = read_box() if box is None else box
        out = {"v": 1, "t": row["time_ns"],
               # THE SEAT IS A LABEL, NOT EVIDENCE: one that is not an id is
               # written as None rather than refused with the row it names.
               "seat": hooklatency._identity(row.get("seat")),
               "ev": row.get("event"), "st": row.get("stage"),
               "ms": _in_domain("ms", row.get("wall_ms")),
               "out": row.get("outcome")}
        out.update((key, _in_domain(key, box.get(key))) for key in FIELDS)
        if out["ms"] is not None:
            out["ms"] = round(out["ms"], 1)
        if out["out"] in hooklatency.RETAINED:
            out.update(eid=row.get("event_id"), sp=row.get("span_id"),
                       par=row.get("parent_id"), bi=row.get("blocked_in"))
        if not _valid(out):
            return False
        wire = (json.dumps(out, separators=(",", ":"), allow_nan=False)
                + "\n").encode("ascii")
        return _append(path(), wire, row["time_ns"])
    except Exception:
        return False


def _born(dest):
    """The `t` of a generation's first row, or None when it cannot be read."""
    try:
        fd = hooklatency._open_regular(dest, os.O_RDONLY)
    except OSError:
        return None
    try:
        head = os.pread(fd, HEAD_BYTES, 0)
    finally:
        os.close(fd)
    try:
        t = json.loads(head.split(b"\n", 1)[0]).get("t")
    except (ValueError, AttributeError, RecursionError):
        return None
    return t if type(t) is int else None


def _append(dest, wire, now_ns):
    lock = fd = None
    try:
        os.makedirs(os.path.dirname(dest), mode=0o700, exist_ok=True)
        lock = hooklatency._open_regular(dest + ".lock", os.O_CREAT | os.O_RDWR)
        if not hooklatency._poll_flock(lock, fcntl.LOCK_EX,
                                       time.monotonic() + LOCK_WAIT_S):
            return False
        try:
            size = os.stat(dest, follow_symlinks=False).st_size
        except FileNotFoundError:
            size = 0
        if size:
            born = _born(dest)
            if size + len(wire) > MAX_BYTES or (
                    born is not None and now_ns - born >= ROTATE_AGE_NS):
                for n in range(GENERATIONS - 1, 0, -1):
                    src = dest if n == 1 else dest + "." + str(n - 1)
                    try:
                        os.replace(src, dest + "." + str(n))
                    except FileNotFoundError:
                        pass
        fd = hooklatency._open_regular(dest, os.O_CREAT | os.O_WRONLY | os.O_APPEND)
        return os.write(fd, wire) == len(wire)
    except Exception:
        return False
    finally:
        for handle in (fd, lock):
            if handle is not None:
                try:
                    os.close(handle)
                except OSError:
                    pass


_KEYS = frozenset(("v", "t", "seat", "ev", "st", "ms", "out") + FIELDS)
#: A timed-out or cancelled row also carries its ids and where it was.
_INCIDENT_KEYS = _KEYS | {"eid", "sp", "par", "bi"}


def _refuse_constant(token):
    raise ValueError("non-finite JSON number %s" % token)


def _parse(wire):
    """One JSON value off the wire, or None: a duplicate key, a NaN or
    Infinity literal, an integer past the digit limit, bytes that are not
    UTF-8 and nesting past the recursion limit are all unreadable, never a
    crash."""
    try:
        return json.loads(wire, object_pairs_hook=hooklatency._object,
                          parse_constant=_refuse_constant)
    except (ValueError, RecursionError):
        return None


def _optional_id(value):
    return value is None or hooklatency._identity(value) is not None


def _valid(r):
    """EXACTLY a row `note` writes, every field in its domain. Anything else
    is an unreadable line: never a row, never a stamp."""
    try:
        return _valid_row(r)
    except (TypeError, ValueError):
        return False


def _valid_row(r):
    if not isinstance(r, dict) or not isinstance(r.get("out"), str):
        return False
    incident = r["out"] in hooklatency.RETAINED
    if set(r) != (_INCIDENT_KEYS if incident else _KEYS):
        return False
    if not (type(r["v"]) is int and r["v"] == 1
            and hooklatency._bounded_int(r["t"]) and _optional_id(r["seat"])
            and r["ev"] in hooklatency.EVENTS
            and r["st"] in hooklatency.STAGES
            and r["out"] in hooklatency.OUTCOMES
            and all(r[key] is None or _in_domain(key, r[key]) is not None
                    for key in _DOMAIN)):
        return False
    return not incident or bool(
        hooklatency._identity(r["eid"]) and hooklatency._identity(r["sp"])
        and _optional_id(r["par"])
        and (r["bi"] is None or isinstance(r["bi"], str)))


def report(hours, dest=None, now_ns=None):
    """What the stream holds over the last `hours`: each (event, stage)'s END
    rows and nearest-rank quantiles, every timed-out END row with its raw
    readings, the first and last row the stream holds, and how many lines
    could not be read. Reads; never writes. It judges nothing."""
    dest = path() if dest is None else dest
    now_ns = time.time_ns() if now_ns is None else now_ns
    lo = now_ns - int(hours * 3600 * 10**9)
    blobs, snapshot = hooklatency._snapshot(dest, GENERATIONS, MAX_BYTES)
    rows, unreadable = [], 0
    for wire in (w for blob in blobs for w in blob.splitlines() if w.strip()):
        r = _parse(wire)
        if _valid(r):
            rows.append(r)
        else:
            unreadable += 1
    inside = sorted((r for r in rows if lo <= r["t"] <= now_ns),
                    key=lambda r: r["t"])
    groups = {}
    for r in inside:
        g = groups.setdefault((r["ev"], r["st"]), dict(
            event=r["ev"], stage=r["st"], rows=0, values=[], timeouts=0,
            cancelled=0))
        g["rows"] += 1
        if r["out"] in MEASURED and r["ms"] is not None:
            g["values"].append(r["ms"])
        elif r["out"] == "timeout":
            g["timeouts"] += 1
        elif r["out"] == "cancelled":
            g["cancelled"] += 1
    hooks = []
    for key in sorted(groups):
        g = groups[key]
        values = sorted(g.pop("values"))
        g.update(n=len(values), p50_ms=hooklatency._rank(values, .5),
                 p95_ms=hooklatency._rank(values, .95),
                 p99_ms=hooklatency._rank(values, .99))
        hooks.append(g)
    # EVERY TIMED-OUT ROW, NOT ONE PER DEADLINE: a timeout writes a timeout
    # END for its span and every ancestor, all under one event id, and
    # choosing the innermost is a claim about the tree this print does not
    # make. A reader groups them by event id.
    timeouts = [dict(t=r["t"], event=r["ev"], stage=r["st"], seat=r["seat"],
                     event_id=r["eid"], span_id=r["sp"], parent_id=r["par"],
                     where=r["bi"], **{key: r[key] for key in FIELDS})
                for r in inside if r["out"] == "timeout"]
    first = min((r["t"] for r in rows), default=None)
    return dict(schema=1, hours=hours, now_ns=now_ns,
                first_row_ns=first,
                last_row_ns=max((r["t"] for r in rows), default=None),
                reach_hours=None if first is None
                else (now_ns - first) / 3.6e12,
                rows=len(rows), rows_in_window=len(inside),
                unreadable_lines=unreadable, snapshot=snapshot,
                generations=GENERATIONS,
                rotate_age_hours=ROTATE_AGE_NS / 3.6e12,
                bytes_per_generation=MAX_BYTES,
                hooks=hooks, timeouts=timeouts)


def _iso(t):
    return "-" if t is None else time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                               time.gmtime(t / 1e9))


def _fmt(value):
    return "-" if value is None else "%.1f" % value


USAGE = ("usage: helm hooks latency --hours H [--json]\n"
         "  The END window stream (hook-latency.jsonl.window) over the last H "
         "hours,\n  0 < H <= 72: each event and stage's END rows with their "
         "p50/p95/p99, and\n  every timed-out END row with the raw load1, cpus "
         "and psi_cpu_some_avg10\n  (a 10-second average) read at its END. It "
         "judges nothing.")


def _hours(text):
    try:
        value = float(text)
    except ValueError:
        return None
    return value if 0 < value <= 72 else None


def cmd(args):
    """`helm hooks latency --hours H [--json]`."""
    argv, hours, want_json = list(args), None, False
    while argv:
        head = argv.pop(0)
        if head in ("--help", "-h"):
            print(USAGE)
            return 0
        if head == "--json":
            want_json = True
        elif head == "--hours" and argv:
            hours = _hours(argv.pop(0))
            if hours is None:
                print("--hours takes a number of hours above 0 and at most 72"
                      "\n" + USAGE, file=sys.stderr)
                return 2
        elif head in ("--since", "--until"):
            print("--hours reads the END window stream; --since/--until read "
                  "the raw stream. Give one or the other.\n" + USAGE,
                  file=sys.stderr)
            return 2
        else:
            print(USAGE, file=sys.stderr)
            return 2
    if hours is None:
        print(USAGE, file=sys.stderr)
        return 2
    result = report(hours)
    if want_json:
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0
    print("Hook END window (hook-latency.jsonl.window), last %.2f h. The "
          "stream holds %d rows from %s to %s (it reaches back %s h; %d "
          "generations rolled hourly, %d MiB cap each); %d in the window; "
          "unreadable lines %d; snapshot %s."
          % (hours, result["rows"], _iso(result["first_row_ns"]),
             _iso(result["last_row_ns"]),
             "-" if result["reach_hours"] is None
             else "%.2f" % result["reach_hours"],
             GENERATIONS, MAX_BYTES >> 20, result["rows_in_window"],
             result["unreadable_lines"], result["snapshot"]["snapshot"]))
    print("rows counts every END row in the window; n and the nearest-rank "
          "quantiles count completed/nonzero/acquired rows.")
    print("%-26s %-16s %6s %6s %9s %9s %9s %8s %9s"
          % ("event", "stage", "rows", "n", "p50_ms", "p95_ms", "p99_ms",
             "timeouts", "cancelled"))
    for g in result["hooks"]:
        print("%-26s %-16s %6d %6d %9s %9s %9s %8d %9d"
              % (g["event"], g["stage"], g["rows"], g["n"], _fmt(g["p50_ms"]),
                 _fmt(g["p95_ms"]), _fmt(g["p99_ms"]), g["timeouts"],
                 g["cancelled"]))
    if result["timeouts"]:
        print("Timed-out END rows in the window. A timeout writes one for its "
              "span and for every ancestor, under one event id. The readings "
              "are raw: load1 is the 1-minute load average, "
              "psi_cpu_some_avg10 the percent of the last 10 s with a task "
              "waiting for a cpu; - is no reading:")
        for i in result["timeouts"]:
            print("  %s  %s/%s  %s  event %s span %s parent %s  load1=%s "
                  "cpus=%s psi_cpu_some_avg10=%s  %s"
                  % (_iso(i["t"]), i["event"], i["stage"], i["seat"] or "-",
                     i["event_id"], i["span_id"], i["parent_id"] or "-",
                     _fmt(i["load1"]), i["cpus"] or "-",
                     _fmt(i["psi_cpu_some_avg10"]),
                     i["where"] or "(location unavailable)"))
    return 0
