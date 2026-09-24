"""helm seatceiling — what the kernel has COUNTED about a seat's throttling.

THE DEFECT THIS EXISTS FOR: a process over its cgroup `memory.high` is stalled
by the kernel in the allocation path. It keeps its process, pane, beacon and
roster row, so every liveness surface reads it healthy — they all answer IS IT
RUNNING. One seat sat that way for an hour of an owner's time while the roster
called it fresh and the beacon plane called it covered, both true and useless.

THIS REPORTS WHAT WAS COUNTED. IT DOES NOT SAY WHETHER A SEAT IS HEALTHY.

Two earlier shapes of this module were taken apart in review and the second
teardown is the reason for this one. It computed an effective-throttle verdict
by walking the cgroup chain and comparing limits — and effective memory
pressure is a kernel-side interaction of per-level usage, reclaim and
controller support that is NOT reconstructable from the numbers a reader can
see. Each round cured the instance named and the next round found the next
face, because the model was wrong rather than incomplete.

So the module stopped modelling and started reading the kernel's own tally.
Every memory cgroup carries `memory.events` — `high` is the number of times
that cgroup was throttled at its boundary. It is the exact event, counted by
the only party that can count it.

WHAT THAT BUYS, AND WHAT IT STILL DOES NOT (both from the review that shaped
this; the second list is the important one):

  * A COUNTER IS HISTORY, NEVER NOW. It says a cgroup HAS been throttled, not
    that it is being throttled, nor for how long, nor how hard, nor that this
    process caused it. Every row says CURRENT STATE UNKNOWN, including rows
    whose counters read zero.
  * ZERO MEANS THE COUNTER READS ZERO. It does not mean "never throttled":
    scope, accounting semantics and object continuity all bound what a zero
    proves, and none of them are visible here.
  * `memory.events` IS HIERARCHICAL — descendants contribute. `memory.events
    .local` is that level's own tally. Both are reported, LABELLED, and
    NEITHER is summed with the other or across levels. A hierarchical count is
    not evidence about an event at that boundary.
  * A MISSING events FILE IS UNKNOWN, NOT "no controller". Permission denied,
    a level that vanished between lookup and read, an unsupported hierarchy
    and a failed lookup are four different absences, and none can be told from
    a disabled controller by the absence alone.
  * `current` AND `high` ARE SEPARATELY-TIMED RAW READS, not an atomic
    snapshot of a ratio. They are reported as observations, never compared.
  * high, max and oom stay SEMANTICALLY DISTINCT and no counter is ever
    converted into a statement about a limit.

THERE IS NO POLICY AXIS AND NO REPAIR. Whether a seat's declared policy matches
what its slice carries is a real question this module deliberately does NOT
answer — asking it correctly means asking systemd what applies to a given unit,
not parsing a prefix drop-in. It has its own row. And observing a process never
authorized mutating the unit above it: the cgroup a pid sits in is discovered,
not owned.

THE PRESENT-TENSE HALF (`fleet_pressure` and below) answers the one question
the counted half refuses: IS THIS SEAT'S SLICE BEING THROTTLED NOW, OR ABOUT
TO BE? A seat stalled in the kernel's over-high throttle cannot reach its own
Stop hook, so every consumer of that answer is some OTHER process — another
seat's Stop pass, a sweep, the roster — and the answer has to be a reading
anyone can take of anyone. It is built only from reads that ARE present-tense,
and never from the cumulative counter alone:

  * THROTTLED — a process of the slice sits in state D on the kernel's
    over-high wchan (conclusive, and it also catches an ANCESTOR's ceiling),
    or the slice's `memory.events` high count ROSE across a short window
    sampled by this reader. A counter that is large and still is history.
  * NEAR — `memory.current` is within NEAR_FRACTION of `memory.high` AND at
    least SHMEM_FRACTION of that ceiling is `shmem` (memory.stat): memory the
    kernel can neither reclaim nor, in a swapless seat slice, swap. This IS a
    comparison of separately-timed reads, which the counted half never makes,
    and it is labelled for what it is: a proximity trigger that licenses
    reaping disposable scratch and one wake, never a statement that the kernel
    is throttling.
  * HIGH — as close to the ceiling, but mostly reclaimable page cache. The
    kernel frees cache on its own before it throttles anyone, and a seat that
    sits in that band routinely would page its lead and the integrator for
    nothing. HIGH is a quiet badge: it is not PRESSING, so it neither reaps
    nor wakes.
  * HIGH-UNREAD — as close to the ceiling, with a memory.stat that would not
    read, so cache cannot be told from tmpfs. It takes HIGH's safe action
    (not pressing) and never HIGH's value: an unreadable shmem is not a
    measured small one, and it is published as null, never 0.

A SPELL ends only once the slice reads under CLEAR_FRACTION and is not
throttled, so a slice hovering at the line is one spell and not a flicker.
"""

import os
import re
import sys
import time
from collections import namedtuple

CGROUP_ROOT = "/sys/fs/cgroup"
HOST_PROC = "/proc"

_SUFFIX = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}

#: A POSITIVELY READ "no limit", distinct from unreadable.
UNLIMITED = "unlimited"
#: THE FILE IS NOT THERE — structurally different from a file that will not
#: read, and folding the two turned a healthy fleet into a wall of UNKNOWN.
ABSENT = "absent"

#: The counters this module reports, in the order a reader wants them.
COUNTERS = ("high", "max", "oom", "oom_kill")

#: WHAT ONE LEVEL'S READ ANSWERED. Every level this observation ASKED ABOUT
#: carries exactly one of these, because the earlier shape could only hold
#: levels that answered — a level that went away was dropped from the result,
#: and a dropped level is indistinguishable from a level that was never on the
#: path. An incomplete chain then read complete.
READ = "read"                 # the level's own counter was obtained
NOCOUNTERS = "no-counters"    # the level is there; its events file is not
UNREADABLE = "unreadable"     # the level is there; the read failed
GONE = "gone"                 # the path was named and no directory is there

Level = namedtuple("Level", "cgroup answer events local current high is_root")
#: THE BRACKET THE READ WAS TAKEN INSIDE, both ends. Generation alone does not
#: bound it: a process keeps its generation across a cgroup MIGRATION, so the
#: path it sits in is read at open and at close too.
Bracket = namedtuple("Bracket", "held why")
Observation = namedtuple("Observation", "levels bracket why")


def parse_size(text):
    """Bytes, UNLIMITED, ABSENT, or None for unreadable — FOUR answers.

    `max` is a statement that no limit applies. A missing file states nothing.
    An unparseable one is a gap. Collapsing any of them is how an unreadable
    ceiling came to read as healthy.
    """
    if text is ABSENT:
        return ABSENT
    if text is None:
        return None
    t = text.strip().lower()
    if t == "max":
        return UNLIMITED
    m = re.match(r"^([0-9]+)([kmgt]?)$", t)
    return int(m.group(1)) * _SUFFIX.get(m.group(2), 1) if m else None


def _read(path):
    """Contents, ABSENT when there is no such file, None when it will not read."""
    if not os.path.exists(path):
        return ABSENT
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None


def parse_events(text):
    """{counter: int} from a memory.events file, or None when unobtainable.

    ABSENT and unreadable both answer None DELIBERATELY at this seam: the
    caller must say UNKNOWN either way, because a missing events file does not
    prove the controller is off — it is equally a denied read, a vanished
    level or an unsupported hierarchy, and this module cannot separate them.
    """
    if text is None or text is ABSENT:
        return None
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] in COUNTERS:
            try:
                out[parts[0]] = int(parts[1])
            except ValueError:
                return None
    return out or None


def _unified(raw):
    """The cgroup v2 path out of a /proc/<pid>/cgroup body, or None.

    ONLY the unified line names a path under a v2 root, and the kernel writes
    it in exactly one shape: hierarchy id 0 with an empty controller list.
    A v1 line such as `9:memory:/x` carries an absolute path too, so taking
    the FIRST absolute path walks a v1 path under the v2 mount — a directory
    that is some unrelated cgroup or none at all, whose counters would then be
    reported as this pid's.

    A v1-only or hybrid body therefore answers None, which the caller renders
    as UNKNOWN. The module header already claimed an unsupported hierarchy is
    unknown rather than clear; this is the line that makes that claim true.
    """
    for line in raw.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[0] == "0" and not parts[1] \
                and parts[2].startswith("/"):
            return parts[2]
    return None


def _line(pid, root, proc):
    """(leaf-path, None) or (None, why) — the pid's unified cgroup, resolved.

    Also the path-half of the bracket: reading it twice is how a MIGRATION is
    detected, which a generation check cannot see because the process keeps
    its generation when it moves.
    """
    raw = _read(os.path.join(proc, str(pid), "cgroup"))
    if raw is None or raw is ABSENT:
        return None, "this pid's cgroup line could not be read"
    rel = _unified(raw)
    if rel is None:
        return None, ("this pid names no cgroup v2 path, so its hierarchy is "
                      "not one this module can read")
    top = os.path.normpath(root)
    leaf = os.path.normpath(os.path.join(top, rel.lstrip("/")))
    if leaf != top and not leaf.startswith(top + os.sep):
        return None, ("this pid's cgroup path does not resolve beneath the "
                      "cgroup root, so it names no level here")
    return leaf, None


def asked(leaf, root=CGROUP_ROOT):
    """Every cgroup path the pid's line names, leaf first, down to the mount
    root. THE QUESTIONS, derived from the PATH and never from the filesystem.

    This is the cut the whole module turns on. Walking the filesystem and
    keeping what was there produced a result that could not REPRESENT a level
    it failed to observe: a directory that went away between the lookup and
    the read was simply absent from the list, and an absent level is
    indistinguishable from a level that was never on the path. So the
    questions are enumerated first, from a string, and every one of them gets
    an answer below.
    """
    top = os.path.normpath(root)
    d, out = os.path.normpath(leaf), []
    while True:
        out.append(d)
        if d == top or os.path.dirname(d) == d:
            break
        d = os.path.dirname(d)
    return out


def answer(path, is_root):
    """The Level for ONE asked-about path. Always a Level, never a skip."""
    if not os.path.isdir(path):
        return Level(path, GONE, None, None, None, None, is_root)
    raw = _read(os.path.join(path, "memory.events.local"))
    local = parse_events(raw)
    if local and "high" in local:
        got = READ
    elif raw is ABSENT:
        got = NOCOUNTERS
    else:
        got = UNREADABLE
    return Level(path, got,
                 parse_events(_read(os.path.join(path, "memory.events"))),
                 local,
                 parse_size(_read(os.path.join(path, "memory.current"))),
                 parse_size(_read(os.path.join(path, "memory.high"))),
                 is_root)


def chain(pid, root=CGROUP_ROOT, proc="/proc"):
    """[Level] for the pid's cgroup and every ancestor, leaf first, or None.

    Every level is reported because every level counts its own throttling, and
    every level ASKED ABOUT is reported because an omission cannot be told
    from a level that was never there. Nothing here decides which one matters:
    comparing limits across levels was the modelling error this module was
    rewritten to stop making.
    """
    leaf, _why = _line(pid, root, proc)
    if leaf is None:
        return None
    top = os.path.normpath(root)
    return [answer(p, p == top) for p in asked(leaf, root)]


def observe(pid, start=None, root=CGROUP_ROOT, proc="/proc"):
    """Observation for `pid`, TAKEN INSIDE A BRACKET WITH TWO ENDS.

    Checking the generation and then reading is not a bracket, it is a
    prefix: the pid can exit and be reused in the window between the check and
    the read, and every level then describes a different process under the
    first one's name. And generation alone does not close it either — a
    process KEEPS its generation when it migrates between cgroups, so the
    cgroup line is read at both ends too.

    A bracket that did not hold does not discard the levels; it marks them.
    The reading still happened and may still be worth printing — what it may
    not do is claim to be about the process the caller named.
    """
    from . import beacons

    def gen():
        return True if start is None else beacons.pid_alive(pid, start, proc)

    opened = gen()
    if start is not None and opened is not True:
        return Observation((), Bracket(False,
                           "the process at this pid is not the one the census "
                           "bracketed (start changed or unreadable), so these "
                           "cgroups cannot be attributed to that seat"), "")
    first, why = _line(pid, root, proc)
    if first is None:
        return Observation((), Bracket(False, why), "")
    top = os.path.normpath(root)
    levels = tuple(answer(p, p == top) for p in asked(first, root))
    last, _ = _line(pid, root, proc)
    closed = gen()
    if start is not None and closed is not True:
        return Observation(levels, Bracket(False,
                           "this pid changed generation DURING the read, so "
                           "the levels below describe a process that is not "
                           "the one the census bracketed"), "")
    if last != first:
        return Observation(levels, Bracket(False,
                           "this process moved between cgroups during the "
                           "read, so the levels below are not one coherent "
                           "chain for it"), "")
    if start is None:
        return Observation(levels, Bracket(False,
                           "no census generation was supplied, so this "
                           "reading is not proven to describe the bracketed "
                           "process"), "")
    return Observation(levels, Bracket(True, ""), "")


def acquisition_complete(obs):
    """EVERY REQUESTED READ WAS OBTAINED, under the documented exceptions, and
    the supplied-generation and endpoint checks were satisfied.

    IT IS NAMED FOR ACQUISITION BECAUSE THAT IS ALL IT MEASURES. It was
    `complete` and then `uncontradicted`; a reviewer ruled both overclaimed,
    and the second was worse than the first for sounding careful. Typed rather
    than prose, because a consumer composing its own knownness must not parse
    a sentence to learn a reading is partial — fleet carries unknown and an
    exit status for exactly this, and the earlier shape handed it a string
    that nothing joined.

    ENDPOINT AGREEMENT IS NOT ATOMICITY, and the two must not be read as one
    fact. The levels are acquired one at a time, so agreement between the two
    ends CANNOT prove that nothing changed in between. The sharpest case is an
    AWAY-AND-BACK MIGRATION: a process that moves to another cgroup and
    returns during the read is identical at open and at close, while the
    levels acquired in between describe where it went.

    So detect-and-mark is this instrument's contract: it reports changes it
    DETECTED and never promises a coherent moment. That is a statement about
    THIS sequential reader and not a claim that no stronger instrument could
    exist — nothing here establishes that.
    """
    return bool(obs.levels) and obs.bracket.held and not unobtainable(obs)


def throttled_levels(obs):
    """[(Level, local_high)] for levels whose OWN counter is above zero.

    Local, never hierarchical: a hierarchical count includes descendants and
    is not evidence of an event at that boundary. Only levels that ANSWERED
    are here — a level that did not is neither throttled nor clear, and
    `unobtainable` names it.
    """
    return [(l, l.local["high"]) for l in obs.levels
            if l.answer == READ and l.local.get("high")]


def unobtainable(obs):
    """[Level] that did not answer. Never folded into clear.

    THE CGROUP ROOT IS THE ONE STRUCTURAL EXEMPTION and it is narrow twice
    over. A controller's interface files do not exist in the root by kernel
    design, so the root answering NOCOUNTERS is a fact about cgroup v2 rather
    than a read this module failed to take — and counting it would put one
    permanent warning on every process on every healthy box, which is how a
    check teaches its readers to ignore it.

    The exemption is the root AND that one answer. A root that is GONE or
    UNREADABLE is not structural, and a missing events file ANYWHERE ELSE
    stays unobtainable, because permission denied, a level that vanished
    between lookup and read, an unsupported hierarchy and a disabled
    controller cannot be told apart by the absence alone.
    """
    return [l for l in obs.levels
            if l.answer != READ and not (l.is_root and l.answer == NOCOUNTERS)]


def notable(obs):
    """Is there anything here a reader would want to see? NOT an alarm.

    True when some level's own counter is above zero, when a level did not
    answer, or when the bracket did not hold. A surface may use this to decide
    whether to PRINT a row — it must not use it to decide whether a seat is
    unhealthy, because a nonzero cumulative counter persists through a whole
    healthy lifetime and treating it as present-tense trouble is the exact
    inversion this module exists to stop.
    """
    return bool(not obs.bracket.held or throttled_levels(obs)
                or unobtainable(obs))


def counts(obs):
    """{cgroup: own-throttle-count} for levels whose LOCAL counter is above
    zero. The structured form of `summary`, for a caller that must compare
    levels ACROSS processes — a shared ancestor's count is identical on every
    row that inherits it, so repeating it per row buries the one level that
    is actually about that process.
    """
    return {l.cgroup: n for l, n in throttled_levels(obs)}


def asked_count(obs):
    """How many levels this observation ASKED about — the denominator, stated
    by the producer that knows it.

    A consumer counting rows that happen to carry a nonzero map is counting
    something else: a process observed with every counter at zero is observed,
    and it does not share a level it never reported. Re-deriving a population
    from the shape of an answer is how a footer came to say 'all N observed'
    about a subset it had filtered itself.
    """
    return len(obs.levels)


def summary_of(counts_map, blind=0, why=""):
    """One line from an already-filtered {cgroup: count}. Same contract as
    `summary`: never healthy, always UNKNOWN-now, zero reads as the counter
    reading zero."""
    if why:
        return "throttling UNKNOWN — %s" % why
    parts = []
    if counts_map:
        parts.append("counters read %s" % ", ".join(
            "%s=%d" % (os.path.basename(c), n)
            for c, n in sorted(counts_map.items())))
    if blind:
        parts.append("%d level(s) unobtainable" % blind)
    if not parts:
        parts.append("counters read 0 at every level")
    return ("%s; whether it is throttled NOW is UNKNOWN (these are cumulative "
            "counts, not a present state)" % "; ".join(parts))


def summary(obs):
    """One line. It NEVER says healthy and it always says the state is UNKNOWN.

    An old cumulative count persists for a whole healthy lifetime, so a
    surface that turns a nonzero counter into a present-tense alarm has
    converted history back into a present-action signal — the exact inversion
    this module exists to stop making.
    """
    if not obs.bracket.held:
        return "throttling UNKNOWN — %s" % obs.bracket.why
    hit = throttled_levels(obs)
    blind = unobtainable(obs)
    parts = []
    if hit:
        parts.append("counters read %s" % ", ".join(
            "%s=%d" % (os.path.basename(l.cgroup), n) for l, n in hit))
    if blind:
        parts.append("%d level(s) unobtainable" % len(blind))
    if not parts:
        parts.append("counters read 0 at every level")
    return ("%s; whether it is throttled NOW is UNKNOWN (these are cumulative "
            "counts, not a present state)" % "; ".join(parts))


# ---------------------------------------------------------------------------
# the present tense — is a seat's slice throttled NOW, or about to be?
# ---------------------------------------------------------------------------

#: A slice this close to its memory.high is NEAR: close enough that the next
#: clone or build tips it over, which is the moment reaping throwaway scratch
#: is cheap and a frozen seat is not yet the cost.
NEAR_FRACTION = 0.90
#: A spell ENDS below this, and a rise is only sampled at or above it: under
#: it the slice has real headroom, so neither a wake nor a sleep is owed.
CLEAR_FRACTION = 0.80
#: The window one present-tense rise is sampled over, shared by every slice
#: that needs it — one sleep per reading, never one per slice. A slice over
#: its high ticks the counter many times a second, so a quarter second is
#: long enough to see it and short enough for a hook to pay.
RISE_WINDOW_S = 0.25
#: The kernel's throttle, as /proc/<pid>/wchan names it. A prefix match,
#: because the symbol carries leading underscores on some kernels and the
#: premise that named it was read off one of them.
OVER_HIGH_WCHAN = "mem_cgroup_handle_over_h"
#: The per-seat slice bin/claude launches every seat into. The DEEPEST match on
#: a path wins, because systemd nests a hyphenated name into parent slices and
#: the leaf-most one is the one carrying the seat's own ceiling.
SEAT_SLICE = re.compile(r"^agents-[^/]+\.slice$")
#: Processes read in one membership walk. Above it the walk is UNKNOWN rather
#: than a partial answer, because a slice missed is a seat called calm.
MEMBER_CAP = 65536

#: The share of memory.high that must be `shmem` before a NEAR slice PRESSES.
#: The frozen seat held 11.3G of shmem under a 12.88G ceiling; a seat at the
#: same ratio in page cache held a fiftieth of that. A quarter of the ceiling
#: sits far above an ordinary seat's shmem and far below what froze one.
SHMEM_FRACTION = 0.25

THROTTLED = "THROTTLED"
NEAR = "NEAR"
HIGH = "HIGH"
#: Near the ceiling with shmem UNREADABLE. Its own word, never HIGH's: a
#: reading that could not be taken must not carry a measured one's value.
HIGH_UNREAD = "HIGH-UNREAD"
#: The words that reap and wake. HIGH and HIGH_UNREAD are not among them.
PRESSING = (THROTTLED, NEAR)
#: A CELL word, never a reading's: the slice's memory.current or memory.high
#: would not read, so its share of the ceiling was never taken. The reading's
#: own word stays None, so nothing reaps or wakes on it, but the cell it
#: publishes must not be the empty one a calm slice carries.
UNKNOWN = "UNKNOWN"

#: One slice, read now. `word` is THROTTLED, NEAR, HIGH, HIGH_UNREAD or None;
#: `rise` is the
#: high count's growth across the window (None when it was not sampled or
#: would not read); `stalled` names the pids found in the over-high throttle;
#: `shmem` is memory.stat's shmem, read for a slice past NEAR_FRACTION.
Pressure = namedtuple("Pressure", "slice current high ratio rise stalled pids "
                                  "word window shmem", defaults=(None,))


def seat_slice_of(rel):
    """The seat's own slice on a unified cgroup path, or None: the path cut
    after its DEEPEST `agents-*.slice` component."""
    parts = (rel or "").split("/")
    for i in range(len(parts) - 1, -1, -1):
        if SEAT_SLICE.match(parts[i]):
            return "/".join(parts[:i + 1])
    return None


def seat_slice_name(seat):
    """`agents-<seat>.slice` exactly as bin/claude spells it: every character
    outside [A-Za-z0-9_] folds to `_`, which keeps systemd from nesting a
    hyphenated name into a chain of parent slices."""
    return "agents-%s.slice" % re.sub(r"[^A-Za-z0-9_]", "_", str(seat or ""))


def slice_members(root=CGROUP_ROOT, proc="/proc"):
    """({slice-path: [pid]}, trouble) — every same-uid process that sits under
    a seat slice, grouped by that slice. ONE /proc walk, one small read per
    process.

    DISCOVERED, NOT DERIVED: the slice comes from each process's own cgroup
    line, so a pid-named slice, a renamed seat and a slice nobody listed are
    all found the same way. `trouble` is set when the walk could not be
    completed, and the caller must then treat the fleet as UNKNOWN rather
    than calm."""
    try:
        me = os.getuid()
        names = [n for n in os.listdir(proc) if n.isdigit()]
    except OSError as exc:
        return {}, ("the process table could not be listed (%s)"
                    % exc.__class__.__name__)
    if len(names) > MEMBER_CAP:
        return {}, ("the process table holds %d entries, past the %d this "
                    "walk reads — membership UNKNOWN" % (len(names),
                                                         MEMBER_CAP))
    top = os.path.normpath(root)
    out = {}
    for n in names:
        pdir = os.path.join(proc, n)
        try:
            if os.stat(pdir).st_uid != me:
                continue
        except OSError:
            continue                 # exited between the listing and the stat
        raw = _read(os.path.join(pdir, "cgroup"))
        if raw is None or raw is ABSENT:
            continue
        rel = seat_slice_of(_unified(raw))
        if rel is None:
            continue
        path = os.path.normpath(os.path.join(top, rel.lstrip("/")))
        if not path.startswith(top + os.sep):
            continue
        out.setdefault(path, []).append(int(n))
    return out, None


def stalled(pids, proc="/proc"):
    """[pid] in state D on the kernel's over-high throttle RIGHT NOW. Read off
    /proc/<pid>/stat after its LAST ')' — comm may hold spaces and parens —
    and /proc/<pid>/wchan. A pid that exited is simply not stalled."""
    out = []
    for pid in pids:
        st = _read(os.path.join(proc, str(pid), "stat"))
        if st is None or st is ABSENT:
            continue
        tail = st.rsplit(")", 1)
        fields = tail[1].split() if len(tail) == 2 else []
        if not fields or fields[0] != "D":
            continue
        w = _read(os.path.join(proc, str(pid), "wchan"))
        if w is not None and w is not ABSENT and OVER_HIGH_WCHAN in w:
            out.append(pid)
    return out


def _high_count(path):
    ev = parse_events(_read(os.path.join(path, "memory.events")))
    return None if ev is None else ev.get("high")


def stat_value(path, key):
    """One counter out of a cgroup's memory.stat, in bytes, or None when the
    file or the key will not read. None is UNKNOWN, never zero."""
    text = _read(os.path.join(path, "memory.stat"))
    if text is None or text is ABSENT:
        return None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == key:
            try:
                return int(parts[1])
            except ValueError:
                return None
    return None


def share_of(current, high):
    ok = [isinstance(v, int) and not isinstance(v, bool)
          for v in (current, high)]
    return current / float(high) if all(ok) and high > 0 else None


#: THE SWITCH ON THIS BOX'S OWN READING. `off` (or `0`, `no`; any case) answers
#: a reading of the host's /proc or /sys/fs/cgroup with no readings and no
#: trouble, and reads nothing: the roster's memory cell is silent and the
#: scratch plane escalates nothing on memory. Unset means on. A reading of any
#: OTHER tree is taken whatever this says, which is how an arm that builds its
#: own cgroup tree still drives the real code. The test suite declares `off`
#: at tests/__init__.py.
SWITCH = "HELM_SEAT_PRESSURE"
#: Raised through sys.audit immediately before a reading touches the host's
#: tree, with (root, proc). Nothing in helm listens; tests/__init__.py refuses
#: it in a test process that did not ask for the host's reading.
HOST_READ_EVENT = "helm.seatceiling.host_read"


def reads_host(root, proc):
    """Does a reading of (root, proc) touch THIS box? EITHER tree is enough:
    the host's /proc names the host's processes, and the host's cgroup root
    holds the host's numbers, whatever the other argument points at."""
    return (os.path.normpath(str(root)) == CGROUP_ROOT
            or os.path.normpath(str(proc)) == HOST_PROC)


def host_read_on():
    """The switch. On unless it says 0, off or no."""
    return (os.environ.get(SWITCH) or "on").strip().lower() \
        not in ("0", "off", "no")


def fleet_pressure(root=CGROUP_ROOT, proc=HOST_PROC, window=RISE_WINDOW_S,
                   sleep=None, members=None):
    """({slice-path: Pressure}, trouble) for every seat slice with a live
    same-uid process in it.

    PAST NEAR_FRACTION THE KIND OF MEMORY DECIDES. A slice that is not
    throttled reads NEAR only when its shmem reaches SHMEM_FRACTION of the
    ceiling, and HIGH otherwise. An unreadable shmem reads HIGH_UNREAD: the
    same safe action as HIGH — it cannot be shown to be the unreclaimable kind,
    and a real throttle is still caught by the stall and the rise, which do
    not depend on it — under its own word, because it is not a measurement.

    THE RISE IS SAMPLED, NEVER INFERRED. A slice at or above CLEAR_FRACTION
    with no stalled process is read twice across ONE shared window; a slice
    whose high count did not move in that window is not THROTTLED, however
    large the count is. A slice already proven stalled skips the sample, and
    a slice below CLEAR_FRACTION costs no sleep at all — so a calm fleet pays
    one /proc walk and three small reads per slice.

    `sleep` and `members` are injectable so an arm can move a counter inside
    the window and own the population; neither changes what is read.

    A reading of the host's own tree answers ({}, None) without reading when
    SWITCH is off, and otherwise raises HOST_READ_EVENT first."""
    if reads_host(root, proc):
        if not host_read_on():
            return {}, None
        sys.audit(HOST_READ_EVENT, root, proc)
    if members is None:
        members, trouble = slice_members(root, proc)
        if trouble:
            return {}, trouble
    rows, before = {}, {}
    for path, pids in members.items():
        current = parse_size(_read(os.path.join(path, "memory.current")))
        high = parse_size(_read(os.path.join(path, "memory.high")))
        ratio = share_of(current, high)
        hit = stalled(pids, proc)
        rows[path] = (current, high, ratio, hit, tuple(sorted(pids)))
        if not hit and ratio is not None and ratio >= CLEAR_FRACTION:
            before[path] = _high_count(path)
    if before:
        (sleep or time.sleep)(window)
    out = {}
    for path, (current, high, ratio, hit, pids) in rows.items():
        rise = None
        if path in before:
            after = _high_count(path)
            if isinstance(before[path], int) and isinstance(after, int):
                rise = after - before[path]
        near = ratio is not None and ratio >= NEAR_FRACTION
        shmem = stat_value(path, "shmem") if near or hit or rise else None
        if hit or (rise or 0) > 0:
            word = THROTTLED
        elif near and shmem is None:
            word = HIGH_UNREAD
        elif near:
            held = share_of(shmem, high)
            word = NEAR if held is not None and held >= SHMEM_FRACTION \
                else HIGH
        else:
            word = None
        out[path] = Pressure(path, current, high, ratio, rise, tuple(hit),
                             pids, word, window if path in before else None,
                             shmem)
    return out, None


def pressing(p):
    """Does this reading reap and wake? THROTTLED or NEAR — never HIGH, and
    never HIGH_UNREAD."""
    return p is not None and p.word in PRESSING


def cleared(p):
    """True when this reading ENDS a spell: readable, under CLEAR_FRACTION and
    not pressing. An unreadable ratio is not a clear — it is a read that
    failed, and closing a spell on it would re-wake on the next good one."""
    return p is not None and not pressing(p) and p.ratio is not None \
        and p.ratio < CLEAR_FRACTION


def _gb(n):
    return "%.2fG" % (n / float(1024 ** 3)) if isinstance(n, int) else "?"


def _pct(part, whole):
    got = share_of(part, whole)
    return "?" if got is None else "%d%%" % round(100 * got)


def pressure_line(p):
    """One line a person can act on: the word, the slice's own numbers, how
    much of it is shmem, and the present-tense evidence behind a THROTTLED."""
    if p is None or not p.word:
        return ""
    share = ("%d%%" % round(100 * p.ratio)) if p.ratio is not None else "?"
    head = ("%s now: %s at %s of %s memory.high (%s)"
            % (p.word, os.path.basename(p.slice), _gb(p.current), _gb(p.high),
               share))
    held = ("shmem %s (%s of memory.high)" % (_gb(p.shmem),
                                              _pct(p.shmem, p.high))
            if p.shmem is not None else "shmem unreadable")
    if p.word == HIGH:
        return ("%s; not pressing — mostly reclaimable page cache, %s"
                % (head, held))
    if p.word == HIGH_UNREAD:
        return ("%s; not pressing — shmem unreadable: cannot tell cache from "
                "tmpfs" % head)
    why = [held] if p.word == NEAR or p.shmem is not None else []
    if p.stalled:
        why.append("%d process%s stalled in the over-high throttle"
                   % (len(p.stalled), "es"[:2 * (len(p.stalled) != 1)]))
    if p.rise:
        why.append("memory.high events +%d in %.2fs" % (p.rise, p.window))
    return head + ("; " + ", ".join(why) if why else "")


def unread_files(p):
    """The ceiling files whose failed read left this reading with no share of
    memory.high, or () when it has one or needs none: a slice under no ceiling
    (`max`) or gone between the walk and the read answered, and is calm."""
    if p.ratio is not None or p.current is ABSENT or \
            p.high in (UNLIMITED, ABSENT):
        return ()
    return tuple(name for name, v in (("memory.current", p.current),
                                      ("memory.high", p.high)) if v is None)


def pressure_cells(p):
    """The published roster keys for one seat's reading, or {} when there is
    nothing to say. ONE producer: the CLI row and the web badge render these
    same strings, so the two surfaces cannot word one seat apart. A wordless
    reading whose ceiling files would not read publishes UNKNOWN, because {}
    is what a calm slice publishes."""
    if p is None:
        return {}
    if not p.word:
        gap = unread_files(p)
        if not gap:
            return {}
        why = "%s would not read" % " and ".join(gap)
        return {"mem_pressure": UNKNOWN,
                "mem_pressure_text": "UNKNOWN now: %s — %s, so its share of "
                                     "memory.high was not taken"
                                     % (os.path.basename(p.slice), why),
                "mem_pressure_mark": "? memory UNKNOWN (%s)" % why,
                "mem_shmem": p.shmem}
    share = ("%d%%" % round(100 * p.ratio)) if p.ratio is not None else "?"
    mark = ("⚠ MEMORY THROTTLED (%s of memory.high)" % share
            if p.word == THROTTLED else
            "⚠ memory NEAR its ceiling (%s of memory.high, %s shmem)"
            % (share, _pct(p.shmem, p.high))
            if p.word == NEAR else
            "\u00b7 memory HIGH (%s of memory.high), shmem unreadable: cannot "
            "tell cache from tmpfs" % share
            if p.word == HIGH_UNREAD else
            "\u00b7 memory HIGH (%s of memory.high), mostly reclaimable cache"
            % share)
    # `mem_shmem` is BYTES or None, and None is published as JSON null: an
    # unreadable shmem must never reach a reader as a measured 0.
    return {"mem_pressure": p.word, "mem_pressure_text": pressure_line(p),
            "mem_pressure_mark": mark, "mem_shmem": p.shmem}
