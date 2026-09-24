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
    The seat's TMPDIR is routed too (launch_tmpdir), and it is the one target
    that is DISK unconditionally: its contents are unbounded AND unattributable,
    so no reaper can ever take them back off a RAM mount.
  * REAP (gc/auto_gc) — dead-session scratch, on an EXISTING hook (the Stop
    hook's silent-mechanical leg), never a new service. The hard part is NOT
    being overbearing, so the laws are:
      - LIVENESS BEFORE AGE. A tree is reapable only when its session id is
        referenced by NO live same-uid process AND no live cwd sits inside it.
        Age alone never reaps — yanking scratch out from under a working agent
        is the failure mode worse than the disk filling. Inside a LIVE tree the
        same law is applied one level down (the second tier below), where the
        evidence is an open descriptor or a live cwd rather than a session id.
      - ATTRIBUTABLE ONLY. Only a child whose basename is a session-id-shaped
        token (uuid) or `pid-<n>` is ever a candidate. Anything helm cannot
        attribute to a session/pid is left alone, forever.
      - FAIL-CLOSED on probe trouble: an unlistable/unreadable process table
        means liveness is UNKNOWN, not absent — reap NOTHING.
      - A BOUND MAY STOP A READING; IT MAY NOT CONCLUDE ONE. Every probe here
        is bounded and that does not change — a GC tick must never be the
        expensive thing. What a bound is not allowed to do is answer. A
        truncated handle table, a stat-capped walk, a `git status` that could
        not be taken, an opaque process that yields no evidence at all: each is
        UNKNOWN, and UNKNOWN KEEPS THE UNIT AND SAYS SO, counted in the report
        rather than quietly read as absence. The failures this law was written
        from all had the same shape — a probe stopped early and the caller
        heard `nothing found`, so a tree that was in use was deleted.
        THIS LAW IS A CLASS, NOT A SENTENCE (`Reading`, below). It was written
        as prose and then broken seven times, one call site at a time, because
        prose asks every future call site to remember: the cure for a truncated
        handle table sat two lines under an uncured process list, and the cure
        for a stat-capped walk sat beside an uncured entry count. Every cap,
        shared budget, wall deadline, hop limit and sized read now passes
        through one primitive that can only return (value, complete), and
        tests/test_scratch.py walks this file's AST to keep it that way.
      - PRESSURE-ESCALATING, on BOTH planes: 7d TTL at rest, 6h past the warn
        threshold, 1h past critical — where `pressure` means the mount for a
        tree on disk and the WORSE of the mount and the HOST'S MEMORY for a tree
        on a RAM-backed mount. Bounded per pass (candidates, victims, stats,
        procs, handles, entries) so a GC tick can never itself be the expensive
        thing.
      - LOUD: every applied pass writes a one-line pk.event, and doctor surfaces
        the last one — a silent data-eater is not allowed.
      - Kill-switches: HELM_SCRATCH_GC=0 (the reaper), HELM_SCRATCH_GC_TIER2=0
        (the second tier alone), HELM_SCRATCH_TMPDIR=0 (the launch-env routing;
        HELM_SCRATCH_TMPDIR=<path> pins it instead).

THE SECOND INCIDENT, same file, different denominator: a 92 GB laptop 23 GB into
swap, its compressed swap device 14 of 16 GB full, while /tmp — a tmpfs, so every
byte of it IS that memory — held 18 GB and the reaper did nothing. Three holes,
all of them INSIDE the laws above rather than exceptions to them:

  1. THE PRESSURE WAS READ OFF THE WRONG DENOMINATOR. Escalation keyed on the
     percentage of the MOUNT; /tmp at 44% of its cap read `ok` and kept the 7d
     at-rest ttl. The scarce resource was HOST MEMORY. So for a VOLATILE mount
     the level now also rises with the host's own scarcity — MemAvailable, swap
     in use, and PSI when the kernel has it (host_memory()). A tree on disk is
     unaffected: it costs the host no memory, so host scarcity is none of its
     business. Unreadable meminfo is UNKNOWN, which escalates NOTHING.
  2. A LIVE SESSION WAS IMMORTAL AND UNBOUNDED. Liveness-before-age is right
     about a session TREE and says nothing about its contents: an integrator
     session lives for days, and one held 5.6 GB with 879 of 920 top-level
     entries untouched for six hours — throwaway worktree copies and helm-home
     copies minted by probes and delegated agents. The SECOND TIER (tier2_scan)
     makes the direct child the unit: reapable while its session runs when
     nothing holds it open (a bounded /proc/*/fd pass), no live cwd sits inside
     it, it is not a name the harness owns, and its newest write is older than a
     short ttl that escalates on the same levels. A live tree is almost never
     itself aged, so the tier reads every CANDIDATE, not the aged ones.
  3. UNATTRIBUTABLE MEANT FOREVER, SILENTLY. `never reap what you cannot
     attribute` still stands — unattributable() explains at length why no age or
     handle probe may overturn it — but the cost is now VISIBLE: the listing
     names every unnamed child of every scratch root with its size and age,
     biggest first, in `helm scratch unattributable`, in the gc dry run, and in
     a doctor row once a RAM mount or the host is pressured.
"""
import glob
import os
import re
import shutil
import stat
import sys
import time

from . import home, openflags, pk, projscope

MB = 1024 * 1024

# ── thresholds ────────────────────────────────────────────────────────────────
WARN_PCT = 85            # either axis at/over this = pressure (the doctor warn)
CRIT_PCT = 95            # either axis at/over this = critical
HEADROOM_PCT = 60        # a mount must be under this on BOTH axes to take a
                         # big tree (a clone is thousands of inodes at once)
BIG_INODE_FLOOR = 250000  # …and still have this many free inodes to spare

# ── bounds (a GC pass must never be the expensive thing) ─────────────────────
# EVERY ONE OF THESE IS HELD BY A `Reading` (below) AND BY NOTHING ELSE. A
# constant that a call site can slice with, compare against or hand to read()
# is a constant that can answer a question, and answering is the one thing a
# bound is not allowed to do. The sizes below are therefore chosen the way
# FD_BUDGET was: an order of magnitude above the whole thing MEASURED on a
# fleet desktop, so the bound is a fence against a pathological estate and not
# a sampler of an ordinary one.
AGED_CAP = 60            # aged candidates carried into the liveness probe
REAP_CAP = 24            # dirs removed per pass
MTIME_STAT_CAP = 400     # stats per candidate when probing newest-mtime
# Counting is also the TRANSCRIPT REFUSAL's reading, and that refusal is
# structural ("seeing the directory at all is enough"), so a count that stopped
# early may not report `protected=False`. Measured on a fleet desktop: a
# COMPLETE count of all 58 attributable candidates is 130,429 entries and
# 0.164s, and the biggest tree this module has ever met held 88,142. So the
# per-tree cap sits above that class and the shared budget above the whole
# estate; a tree past either is KEPT and said, never quietly called unprotected.
COUNT_CAP = 250000       # entries counted per tree (ranking + the refusal)
COUNT_BUDGET = 400000    # …and across one whole tier-one pass
PROC_BUDGET = 65536      # processes read in one liveness probe (961 measured)
BLOB_CAP = 256 * 1024    # bytes read per process cmdline/environ (12 KiB max)
STAT_READ_CAP = 4096     # bytes read from one /proc/<pid>/stat
GITFILE_CAP = 4096       # bytes read from one `.git` pointer file
EVENT_SCAN_CAP = 200     # journal rows read looking for the last applied pass
SUMMARY_LIST_CAP = 3     # items the one audited line spells out in full
THROTTLE_S = 3600        # minimum gap between auto passes

TTL_BY_LEVEL = {"ok": 7 * 86400, "warn": 6 * 3600, "critical": 3600}

# ── the RAM denominator: a volatile mount's scarce resource is HOST MEMORY ───
# A mount percentage answers "how full is this mount" and never "is the box out
# of memory". Measured on the owner's laptop: /tmp (a tmpfs — every byte of it
# IS host memory) read 44% of its cap while the host was 23 GB into swap with
# its compressed swap device 14 of 16 GB full, so the reaper sat at its 7d
# at-rest TTL while RAM was the thing running out. For a VOLATILE mount the
# level must therefore also rise with the HOST's scarcity. Three axes, worst
# wins, each a named threshold:
#   MemAvailable share — the kernel's own answer to "what can I hand out
#     without swapping". Under a quarter of RAM is where the next agent launch
#     on a fleet host starts swapping; under a tenth the box is already there.
#   Swap used share — the memory of past scarcity, which MemAvailable forgets
#     (swapping is what made it look healthy again). A machine only swaps once
#     the working set has exceeded RAM, and a quarter of a swap device in use
#     is far past the kernel's idle-page housekeeping.
#   PSI `some avg60` — the one axis that says tasks ARE stalling right now.
#     Optional: a kernel without PSI has no file, and its absence is not
#     pressure. A tenth of wall time stalled on memory is critical alone.
MEMINFO = "/proc/meminfo"
PSI_MEM = "/proc/pressure/memory"
MEM_AVAIL_WARN_PCT = 25
MEM_AVAIL_CRIT_PCT = 10
SWAP_USED_WARN_PCT = 25
SWAP_USED_CRIT_PCT = 60
PSI_SOME_AVG60_CRIT = 10.0

# ── the second tier: a LIVE session's scratch is bounded too ────────────────
# Liveness protects a whole session TREE and that is right, but an integrator
# session lives for days: one held 5.6 GB with 879 of 920 top-level entries
# untouched for six hours (throwaway worktree copies and helm-home copies minted
# by probes and delegated agents). So INSIDE a live tree the unit is the direct
# child, reapable while its session runs when nothing holds it open, no live cwd
# sits in it, it is not a name the harness owns, and its newest write is older
# than a SHORT ttl that escalates on the same levels.
TIER2_TTL_BY_LEVEL = {"ok": 24 * 3600, "warn": 4 * 3600, "critical": 3600}
TIER2_TREE_CAP = 6        # live trees opened per pass
TIER2_SCAN_CAP = 4096     # entries read per container
TIER2_PROBE_CAP = 200     # aged units carried into the deep probe
TIER2_REAP_CAP = 64       # units removed per pass
# The tier-2 count carries the same transcript refusal, so it is sized the same
# way: measured, a COMPLETE count of all 270 dir units under this box's 17 live
# session trees is 124,091 entries in 0.183s, and the biggest single unit holds
# 23,825. The old 2,000-entry cap sat BELOW fifteen real units.
TIER2_COUNT_CAP = 250000  # entries counted per unit (ranking + the refusal)
TIER2_COUNT_BUDGET = 400000  # entries counted across the whole probe
TIER2_MTIME_CAP = 50000   # stats spent ageing ONE tier-2 unit
TIER2_MTIME_BUDGET = 250000  # …and across the whole tier-2 probe
FD_BUDGET = 400000        # descriptors read in ONE handle pass, whole table
# A registration scan that stops early does not say "no worktree here"; the
# biggest direct-child listing measured on this estate is 1,632 entries.
WT_SCAN_CAP = 65536       # entries scanned looking for a registration, per unit
WT_SCAN_BUDGET = 200000   # …and across one whole pass
WT_DIRTY_CAP = 32         # `git status` spawns priced in one pass
WT_DIRTY_TIMEOUT_S = 10   # …and the wall time any one of them may take
WT_DIRTY_BUDGET_S = 10.0  # …and the wall time ALL of them may take together
TOCTOU_HANDLE_MAX_AGE_S = 2.0  # how stale the handle table may be at a delete
OPAQUE_HOPS = 64          # parent hops walked classifying an opaque process
# The harness owns `<tmp>/claude-<uid>/<project>/<session>/`. Measured there:
# its direct children are `scratchpad/` — where agent-made throwaways go, so it
# is a CONTAINER whose own children are the units — and `tasks/`, the harness's
# own subagent-output dir, which is written continuously (a session whose oldest
# output was eight days old had one written in the minute the scan ran) and is
# read back long after it was written. `tasks` is therefore kept, and so is any
# dot-name: those are plugin state (`.remember/`), never an agent throwaway.
TIER2_CONTAINERS = ("scratchpad",)
TIER2_KEEP = ("tasks",)

# ── the THIRD plane: a SEAT's own memory cgroup ─────────────────────────────
# The host plane and the mount plane both answer for the BOX. A seat can be
# frozen by its OWN slice while the box has a third of its memory free: every
# seat runs under a per-seat memory.high with no swap, and a tmpfs page is
# shared memory charged to the cgroup that wrote it — so a full clone a
# subagent leaves in the seat's scratchpad is memory the seat can neither
# reclaim nor swap. When seatceiling reads a seat's slice THROTTLED or NEAR,
# that seat's unheld tier-2 units on a RAM mount reap at ttl 0, biggest first,
# under every predicate the tier already has. Two limits keep ttl 0 from being
# a blanket delete of a working agent's files:
#   * FLOOR — ttl 0 applies to units at least this big. A receipt or a commit
#     message relieves nothing, and the agent may be about to read it; smaller
#     units keep the ordinary host-pressure ttl.
#   * RELIEF — biggest first, and a slice's units stop being picked once the
#     picks already project it under seatceiling.CLEAR_FRACTION.
# The reading is taken by ANY pass — another seat's Stop hook or a sweep —
# because the throttled seat never reaches its own. SEAT_THROTTLE_S bounds how
# often the automatic leg pays for it, fleet-wide.
PRESSURE_UNIT_FLOOR = 32 * MB
PRESSURE_TOP_CAP = 5        # biggest units a pass names per pressured slice
SEAT_THROTTLE_S = 60        # minimum gap between automatic seat-plane reads
WAKE_BOT = "scratch-gc"     # the author a pressure wake is posted as

# ── an OPAQUE process: what it is allowed to prove ──────────────────────────
# A same-uid process whose /proc the kernel refuses to describe gives the tier
# NEITHER cwd nor fd evidence, so "no evidence" and "unheld" are the same
# reading — and that is the one reading tier 2 may not take, because its
# session is RUNNING. The blanket cure (any denial fails the tier closed) is
# the one that disabled the reaper once, so the classification is a ladder and
# only its last rung stops the tier:
#   DEAD. A process in state Z or X is a corpse: the kernel has already torn
#     down its descriptor table and its cwd, which is WHY /proc/<pid>/fd starts
#     denying reads. It can hold nothing. This is a proof, not a heuristic, and
#     it covered most of the opaque set measured on a live fleet desktop —
#     including the one shell under a terminal that reads like an agent's `cd`.
#   HARMLESS. A live non-dumpable process is one that exec'd setuid/setgid or
#     cleared PR_SET_DUMPABLE. Measured, the live remainder is desktop and
#     credential daemons and a bubblewrapped chat app — none of which can reach
#     the harness scratch estate.
#   AGENT-ADJACENT. Unless it is a SHELL, or something a terminal/multiplexer
#     started: that is exactly where an agent's cwd lives, and it is the case
#     the tier cannot clear. One of those makes the whole tier-2 pass UNKNOWN.
# The last rung is deliberately a NAMED list rather than "anything unrecognised
# is dangerous": the strict polarity turns every new sandboxed desktop app into
# a permanent shutdown of the tier, which is the failure this module already
# made once.
# A pid that EXITED between the listing and the classification is none of the
# three: it is gone, it holds nothing, and calling its absence "unreadable"
# would be a hazard minted by the probe's own race. Measured on a fleet
# desktop, that race is not theoretical — it fired on the first pass.
# Matching is EXACT, never a prefix. `comm` is a 15-byte kernel field, so the
# list carries the truncated spelling the kernel actually reports
# (`gnome-terminal-`); a prefix rule would make the 2-character entry `st`
# match `strace`, and a needle that short is a coin flip, not a reading.
OPAQUE_SHELL_COMMS = ("sh", "bash", "zsh", "dash", "fish", "ksh", "ash",
                      "busybox", "login", "su", "sudo")
OPAQUE_TERMINAL_COMMS = ("tmux", "tmux: server", "tmux: client", "screen",
                         "sshd", "orca", "orca-ide", "orca-linux", "herdr",
                         "gnome-terminal-", "konsole", "xterm", "urxvt",
                         "alacritty", "kitty", "wezterm-gui", "foot", "st",
                         "terminator", "ptyxis", "tilix", "xfce4-terminal")

# ── what helm cannot attribute: visible, never reaped ───────────────────────
# The attribution gate leaves unnamed trees alone forever, which is the right
# default and was also SILENT: measured on the owner's laptop, 8 GB of a 12.8 GB
# /tmp sat in entries no shape gate admits. Silence is the bug; the listing is
# the cure. Bounded like everything else, and sized breadth-first under one
# shared stat budget so a root with 29,455 children cannot turn a doctor row
# into a tree walk.
UNATTR_SCAN_CAP = 65536      # direct children examined per root
UNATTR_STAT_BUDGET = 500000  # stats spent sizing them, whole pass
UNATTR_DOCTOR_BUDGET = 40000  # …and the much smaller one a doctor row spends
UNATTR_PER_ENTRY_CAP = 50000  # stats spent on any ONE entry
UNATTR_LIST_CAP = 10         # rows the doctor/CLI line names
UNATTR_AGE_FLOOR = 7 * 86400  # the LONG age named in the written decision

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


class Reading(object):
    """ONE BOUNDED READING — and the only thing it can hand back is
    (value_so_far, complete).

    THE LAW IN THIS FILE IS A SHAPE, NOT A SENTENCE. `A bound may stop a
    reading; it may not conclude one` was written at the top of this module and
    then broken seven times, one call site at a time: a cap inside a slice, a
    budget inside a `break`, a sized `read()` two lines below a cured one, a
    hop limit that answered `not agent-adjacent`. Each cure patched the
    instance and left the sibling, because prose asks every future call site to
    remember. A class cannot be forgotten: every cap, shared budget, wall
    deadline, hop limit and sized read in this module is held by one of these,
    and there is no accessor that hands back a value without `complete` beside
    it.

    WHAT INCOMPLETE MEANS. Not `wrong` — a partial count is a true floor, a
    partial walk found a true newest mtime. It means THIS IS THE SLICE OF THE
    WORLD THE READING REACHED, so no caller may read it as absence. Three
    things make a reading incomplete: its own cap, a budget shared with the
    rest of the pass, a wall deadline — and one more that is not a bound at
    all, an input that refused to be read (a denied subtree, a vanished
    process, a blob longer than the buffer). They are the same fact to a
    caller, so they are the same flag.

    THE REFUSAL TEXT COMES FROM HERE TOO (`why`), because a call site that
    re-spells the constant in its own message is a message that drifts from the
    bound it describes — and it is a second use of the constant, which is
    exactly what the tripwire in tests/test_scratch.py refuses. That test walks
    this module's AST and fails on EVERY shape a bound can take outside this
    class: a bound constant or one of this class's own bound fields read
    elsewhere, a sized read, a slice with a constant upper bound, a range or
    islice length, and a counter-guarded break, return or loop head stopped by
    a literal, by any module-level name, or by a local holding one. There are
    exactly two admitted homes — this class body, and an argument to
    `Reading(...)` / `Reading.pool(...)`.
    """

    __slots__ = ("what", "cap", "budget", "until", "per_call",
                 "spent", "complete", "stopped", "stopped_in")

    def __init__(self, what, cap=None, budget=None, until=None, per_call=None):
        self.what = what          # what is being read, for the refusal text
        self.cap = cap            # this reading's own allowance
        self.budget = budget      # a SHARED [n] list, spent across readings
        self.until = until        # a wall-clock deadline for the whole reading
        self.per_call = per_call  # the wall time any ONE read may take
        self.spent = 0
        self.complete = True
        self.stopped = None
        self.stopped_in = None    # the INNER reading that actually stopped

    # ── the three bounds, and the fourth thing that stops a reading ────────
    def stop(self, why):
        """This reading is incomplete, for a reason that is not a bound: an
        input that could not be read. The FIRST reason is kept — it is the one
        that made the answer partial."""
        self.complete = False
        if self.stopped is None:
            self.stopped = why

    def stop_from(self, inner):
        """This reading is incomplete because a reading INSIDE it stopped.

        The reason belongs to the inner reading, and handing its whole REFUSAL
        SENTENCE to `stop` nests one inside the other: `the worktree
        registration scan reading stopped at the git pointer file reading
        stopped at its 0-byte read cap, so ...`. Two `stopped at` clauses in
        one sentence is not a refusal an owner can read. So the bound travels
        instead of the prose: the sentence names the reading that ACTUALLY hit
        a bound, and the subject stays the one this caller asked about."""
        self.complete = False
        if self.stopped is None:
            self.stopped = inner.stopped
            self.stopped_in = inner.stopped_in or inner.what

    def spent_out(self):
        """Has a bound stopped this reading? Asking is not free of meaning: a
        reading whose bound is reached is incomplete from that moment, whether
        or not the caller goes on to ask anything else."""
        if self.cap is not None and self.spent >= self.cap:
            self.stop("its %d-%s cap" % (self.cap, self.unit_name()))
            return True
        if self.budget is not None and self.budget[0] <= 0:
            self.stop("the pass's shared %s budget" % self.what)
            return True
        if self.until is not None and time.time() >= self.until:
            self.stop("its wall-clock deadline")
            return True
        return False

    def unit_name(self):
        """What one unit of this reading's allowance is — entries, processes,
        descriptors, spawns, hops. Named by the reading so a refusal says what
        it counted rather than a bare number."""
        return {"process table": "process",
                "handle-table process list": "process",
                "handle table": "descriptor",
                "git status allowance": "spawn",
                "parent walk": "hop",
                "live-tree slice": "tree",
                "aged-candidate window": "candidate",
                "deep-probe window": "unit",
                "per-pass reap window": "unit",
                "journal scan": "row",
                "audited-line listing": "item",
                "doctor-row listing": "row",
                "listing": "row"}.get(self.what, "entry")

    def take(self, n=1):
        """Spend n units of the allowance. False once a bound has stopped the
        reading — and the caller must then treat what it has as PARTIAL."""
        if self.spent_out():
            return False
        self.spent += n
        if self.budget is not None:
            self.budget[0] -= n
        return True

    # ── the shapes every call site in this module uses ────────────────────
    def head(self, seq):
        """(the first `cap` items, complete). The slice a call site is not
        allowed to write itself: `seq[:CAP]` silently drops the fact that
        there was a rest."""
        items = list(seq)
        if self.cap is not None and len(items) > self.cap:
            self.stop("its %d-%s cap" % (self.cap, self.unit_name()))
            items = items[:self.cap]
        self.spent += len(items)
        if self.budget is not None:
            self.budget[0] -= len(items)
        return items, self.complete

    def read(self, fh):
        """(bytes, complete) — a sized read that KNOWS whether it truncated.

        `fh.read(cap)` returns exactly `cap` bytes whether the file ended there
        or not, so the caller cannot tell a whole file from a slice of one.
        This asks for one byte more than it will keep, which is the entire
        difference between a bounded reading and a silent conclusion."""
        if self.cap is None:
            return fh.read(), self.complete
        blob = fh.read(self.cap + 1)
        if len(blob) > self.cap:
            self.stop("its %d-byte read cap" % self.cap)
            blob = blob[:self.cap]
        return blob, self.complete

    @staticmethod
    def pool(n):
        """A SHARED allowance that several readings spend from — a one-item
        list, so every Reading holding it spends the same pot and a pass's
        total cost is bounded however many readings it makes. Minted here
        because a budget belongs to this class's contract and to no call
        site's."""
        return [n]

    def ask(self):
        """How many items to ask an OUTSIDE reader for: one more than this
        reading will keep. The same trick as `read` — a reader handed exactly
        `cap` cannot tell a whole answer from a slice of one — and it is the
        only honest way to bound a call whose own API takes a count."""
        return None if self.cap is None else self.cap + 1

    def timeout(self):
        """The wall time ONE read inside this reading may take: its own
        per-read bound, NARROWED by whatever is left of the shared deadline. A
        per-read timeout checked only before the call lets a pass overrun its
        own budget by a whole timeout — measured worst case, twenty seconds on
        a leg that is supposed to cost a fifth of one."""
        left = None if self.until is None else max(0.0, self.until - time.time())
        if self.per_call is None:
            return left
        return self.per_call if left is None else min(self.per_call, left)

    def done(self, value):
        """(value, complete) — the only way a value leaves a Reading."""
        return value, self.complete

    def why(self, subject=None):
        """The sentence a refusal is built from, or None when the reading
        finished. It names the reading, the bound that stopped it and what is
        therefore UNKNOWN — the three things a reader of the report needs and
        cannot recover from a counter."""
        if self.complete:
            return None
        return "the %s reading stopped at %s, so %s is UNKNOWN" % (
            self.stopped_in or self.what, self.stopped or "an unreadable input",
            subject or "the answer")


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


_ORDER = {"ok": 0, "warn": 1, "critical": 2}


def worse(left, right):
    """The higher of two levels — the estate reads pressure as worst-axis-wins
    on mounts, and the host axis joins that rule rather than inventing one."""
    return left if _ORDER[left] >= _ORDER[right] else right


def _meminfo(path=None):
    """{field: BYTES} from /proc/meminfo, or None when unreadable. The file
    reports kB; converting here means no caller repeats the unit.

    A TRUNCATED READ IS AN UNREADABLE FILE HERE, not a partial one: this
    returns a whole table or nothing, and host_memory's own fail-closed path
    (level ok, escalate nothing, name the trouble) is the right home for a
    reading that did not finish."""
    reading = Reading("meminfo", cap=BLOB_CAP)
    try:
        with open(path or MEMINFO) as f:
            raw, complete = reading.read(f)
    except OSError:
        return None
    if not complete:
        return None
    out = {}
    for line in raw.splitlines():
        name, _sep, rest = line.partition(":")
        parts = rest.split()
        if not parts:
            continue
        try:
            val = int(parts[0])
        except ValueError:
            continue
        out[name.strip()] = val * 1024 if "kB" in parts[1:] else val
    return out or None


def _psi_some_avg60(path=None):
    """The `some avg60` share from /proc/pressure/memory, or None. ABSENCE IS
    NOT PRESSURE: a kernel built without PSI has no file, and the two meminfo
    axes still answer. A truncated read is the same as no file: this axis is
    optional and never escalates on its own silence."""
    reading = Reading("pressure file", cap=BLOB_CAP)
    try:
        with open(path or PSI_MEM) as f:
            raw, complete = reading.read(f)
    except OSError:
        return None
    if not complete:
        return None
    for line in raw.splitlines():
        if not line.startswith("some "):
            continue
        for field in line.split():
            if field.startswith("avg60="):
                try:
                    return float(field.split("=", 1)[1])
                except ValueError:
                    return None
    return None


def host_memory(meminfo=None, psi=None):
    """The HOST-MEMORY axis, for volatile mounts: a dict carrying `level`, the
    `why` that names which axis decided, and `trouble`.

    FAIL-CLOSED, and note which way closed points here. An unreadable
    /proc/meminfo means the host's scarcity is UNKNOWN, so the answer is level
    "ok" plus a named trouble: no escalation, therefore no tree becomes reapable
    that the MOUNT axes had not already named, and the second tier (which exists
    only because of this axis) does nothing at all. Unknown never licenses a
    deletion."""
    info = _meminfo(meminfo)

    def blind(why):
        """A REFUSAL NAMES THE INPUT IT CHECKED. `UNREADABLE` about a file that
        read perfectly well sends the reader after a permission problem that
        does not exist; the real cause on an old kernel or inside a container
        is a file that parsed and did not carry the field."""
        return {"level": "ok", "why": "", "avail_pct": None,
                "swap_used_pct": None, "psi": None,
                "trouble": "host memory UNKNOWN: %s — no escalation" % why}

    path = meminfo or MEMINFO
    if not info:
        return blind("%s UNREADABLE (or empty)" % path)
    total = info.get("MemTotal") or 0
    avail = info.get("MemAvailable")
    if not total or avail is None:
        return blind("%s read fine but carries no %s"
                     % (path, " or ".join(
                         k for k, ok in (("MemTotal", total),
                                         ("MemAvailable", avail is not None))
                         if not ok)))
    avail_pct = int(round(100.0 * avail / total))
    swap_total = info.get("SwapTotal") or 0
    swap_free = info.get("SwapFree") or 0
    swap_pct = (_pct(swap_total - swap_free, swap_free) if swap_total else None)
    psi_val = _psi_some_avg60(psi)
    axes = (
        ("MemAvailable %d%% of RAM" % avail_pct, avail_pct,
         -MEM_AVAIL_WARN_PCT, -MEM_AVAIL_CRIT_PCT, -1),
        (None if swap_pct is None else "swap %d%% used" % swap_pct, swap_pct,
         SWAP_USED_WARN_PCT, SWAP_USED_CRIT_PCT, 1),
        (None if psi_val is None else
         "PSI some avg60 %.1f%% of wall time stalled on memory" % psi_val,
         psi_val, PSI_SOME_AVG60_CRIT, PSI_SOME_AVG60_CRIT, 1),
    )
    # `sign` folds the one axis that reads BACKWARDS (more MemAvailable is
    # better) into the same comparison as the two where more is worse.
    level, why = "ok", []
    for label, value, warn_at, crit_at, sign in axes:
        if label is None:
            continue
        reading = value * sign
        if reading >= crit_at:
            level = worse(level, "critical")
            why.append("%s (critical at %s)" % (label, abs(crit_at)))
        elif reading >= warn_at:
            level = worse(level, "warn")
            why.append("%s (warn at %s)" % (label, abs(warn_at)))
    return {"level": level, "why": "; ".join(why), "avail_pct": avail_pct,
            "swap_used_pct": swap_pct, "psi": psi_val, "trouble": None}


def volatile_devs(rows):
    """The st_dev of every surveyed mount that is RAM-backed. A candidate on one
    of these devices is the only thing the host axis may escalate — a tree on
    disk costs the host no memory, so host scarcity is none of its business."""
    return {u.get("dev") for u in rows or []
            if u.get("volatile") and u.get("dev") is not None}


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
    wins. Unreadable paths are skipped; ambient-budget Expired propagates."""
    projscope.spend_or_raise("reading scratch mount table")
    table = mount_table()
    projscope.spend_or_raise("reading scratch mount table")
    rows, seen = [], set()
    projscope.spend_or_raise("enumerating scratch paths")
    paths = helm_paths()
    projscope.spend_or_raise("enumerating scratch paths")
    for label, path in paths:
        projscope.spend_or_raise("surveying scratch path")
        u = usage(path, table)
        projscope.spend_or_raise("surveying scratch path")
        if not u:
            continue
        key = u["dev"] if u["dev"] is not None else u["mount"]
        if key in seen:
            continue
        seen.add(key)
        u["label"] = label
        rows.append(u)
    projscope.spend_or_raise("finishing scratch survey")
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


# ── a clone into RAM: the source of the tmpfs-clone freeze ──────────────────
# `git clone <local path>` copies the source's WHOLE object store: measured on
# this repository, 1.4G and 14 thousand files in two seconds. Onto a tmpfs that
# is shared memory charged to the cloning seat's own cgroup, which carries no
# swap — so a verifier's throwaway copy is the one allocation that can freeze
# its seat. `git clone --shared` (objects by reference, 45M) and a detached
# worktree (45M; for this repository `helm work peek`, on disk) are the same
# checkout at a thirtieth of the cost. `helm scratch big` routes to a RAM
# mount as well, so it does not cure this. The shell grammar that finds the
# argv is chat's; this reads only the filesystem facts it names.
_CLONE_VALUE_OPTS = ("-o", "--origin", "-b", "--branch", "-u", "--upload-pack",
                     "--reference", "--reference-if-able", "-c", "--config",
                     "--depth", "--shallow-since", "--shallow-exclude",
                     "--separate-git-dir", "-j", "--jobs", "--template",
                     "--filter", "--server-option", "--bundle-uri",
                     "--ref-format", "--revision")
_CLONE_SHARED = ("-s", "--shared")
_CLONE_SHALLOW = ("--depth", "--shallow-since", "--shallow-exclude")


def _clone_argv(args):
    """(options, positionals) of the tokens after `clone`."""
    opts, pos, i = set(), [], 0
    while i < len(args):
        a = args[i]
        if a == "--":
            pos.extend(args[i + 1:])
            break
        if a.startswith("--"):
            head = a.split("=", 1)[0]
            opts.add(head)
            i += 2 if head in _CLONE_VALUE_OPTS and "=" not in a else 1
            continue
        if a.startswith("-") and len(a) > 1:
            if "-" + a[1] in _CLONE_VALUE_OPTS:
                opts.add("-" + a[1])
                i += 2 if len(a) == 2 else 1
                continue
            opts.update("-" + c for c in a[1:])
            i += 1
            continue
        pos.append(a)
        i += 1
    return opts, pos


def _humanish(src):
    """The directory `git clone` names when it is not given one."""
    base = src.rstrip("/")
    if base.endswith("/.git"):
        base = base[:-len("/.git")]
    base = os.path.basename(base.rstrip("/"))
    return base[:-len(".git")] if base.endswith(".git") else base


def clone_into_ram(args, cwd=None, table=None):
    """The RAM mount a `git clone` would copy a LOCAL repository's whole
    object store into, or None. `args` are the tokens after `clone`; `cwd` is
    the directory the command runs in.

    None whenever a fact is missing or the copy is not the incident's shape —
    a false alarm is the one failure an advisory must not have: a remote URL
    or an scp-style `host:path` (a network clone packs what it sends), a
    source that is not a directory here, `--shared` (the cure), a shallow
    clone through file:// or --no-local (it packs a history cut),
    --separate-git-dir (the objects go where it says), a relative path with no
    cwd to resolve it against, or a target whose mount is not tmpfs."""
    opts, pos = _clone_argv(list(args or ()))
    if not pos or opts & set(_CLONE_SHARED) or "--separate-git-dir" in opts:
        return None
    src = pos[0]
    if src.startswith("file://"):
        local, packed = src[len("file://"):], True
    elif "://" in src or re.match(r"^[^/]*:", src):
        return None
    else:
        local, packed = src, "--no-local" in opts
    if packed and opts & set(_CLONE_SHALLOW):
        return None

    def placed(path):
        path = os.path.expanduser(path)
        if os.path.isabs(path):
            return os.path.normpath(path)
        return os.path.normpath(os.path.join(cwd, path)) if cwd else None

    source = placed(local)
    if not source or not os.path.isdir(source):
        return None
    target = placed(pos[1] if len(pos) > 1 else _humanish(src))
    if not target:
        return None
    probe = target
    while not os.path.exists(probe) and os.path.dirname(probe) != probe:
        probe = os.path.dirname(probe)
    mount, fstype, _opts = _mount_of(probe, mount_table()
                                     if table is None else table)
    return mount if fstype in VOLATILE_FS else None


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
    the child lands on a mount helm chose. Opt out with HELM_SCRATCH_TMPDIR=0;
    pin it with HELM_SCRATCH_TMPDIR=<path> or HELM_SCRATCH_DIR=<path>.

    DELIBERATELY NOT `route("small")`, for two honest reasons:
      * TMPDIR content is NOT session-attributable — tools name their own files
        and nothing ties them to a session id — so the reaper cannot reap it.
        Its only protection is landing off the CAPPED mount in the first place.
      * unbounded + unreapable must therefore never share a mount with the CHAT
        BUS (/dev/shm/helm-chat is a design pillar). So the target here is DISK,
        never the other RAM mount: no byte-pressure coupling between a seat's
        build cache and the fleet's message bus.
    The dir is created 0o700: it carries a seat's tempfiles, and tempfile's own
    guarantees are about the file, never about the directory it is handed."""
    path = tmpdir_root()
    if path is None:
        return None
    try:
        os.makedirs(path, exist_ok=True)
        os.chmod(path, 0o700)
    except OSError:
        return None
    return path


def tmpdir_root():
    """The routed TMPDIR a NEW launch gets, WITHOUT creating it — or None when
    the routing is off. See tmpdir_roots() for why that is not the only one."""
    roots = tmpdir_roots()
    return roots[0] if roots else None


def tmpdir_roots():
    """[path] — every routed TMPDIR root that can be in force right now, the
    one a new launch gets FIRST. Existence is not checked; the reaper and the
    listing filter for that themselves.

    WHY THIS EXISTS AT ALL: the harness derives its own per-uid temp root from
    the ambient temp dir, so exporting TMPDIR moves that whole estate. Measured
    on the owner's box, `<routed tmpdir>/claude-<uid>/` held 2.2 GB of harness
    scratch — invisible to a reaper that only globs `<tmp>/claude-*`, which is
    why scratch_specs() carries these roots as well.

    WHY IT IS DISK, unconditionally, rather than 'the ambient tmp while it has
    headroom': the headroom test was a 60% line on the ambient mount, and the
    thing it was gating is UNBOUNDED and UNREAPABLE. Measured on the owner's
    box: /tmp (a 24 GB tmpfs, so every byte of it is RAM) sat at 58%, the test
    passed, the routed target was therefore `/tmp/helm-scratch/tmpdir` — still
    RAM — and one seat's 9.9 GB measurement copy then took /tmp to its cap. A
    class of scratch that nothing can reap does not get to hold a headroom test
    hostage; `big` and `small` still take the fast RAM mounts through `route()`,
    because those are TAGGED and therefore reapable. The operator override
    (HELM_SCRATCH_DIR, or HELM_SCRATCH_TMPDIR=<path>) still wins outright.

    WHY THE LIST IS NOT ONE ENTRY: a seat keeps the env it launched with. A
    change here reaches a running seat never — its TMPDIR, and the
    `claude-<uid>` root under it, stay where they were until it next launches.
    So every root this routing can name is a root some live seat may be
    writing into right now, and dropping any of them from the list makes that
    seat's whole harness scratch estate invisible to the reaper."""
    return _tmpdir_route()[0]


def tmpdir_trouble():
    """The LOUD half of the TMPDIR route: what is wrong with where a new seat's
    temp files will land, or None.

    `_disk_root` returns a second value saying whether it actually FOUND a
    non-volatile root, and this route ignored it — so on a host with no disk
    candidate the docstring's `DISK, unconditionally` silently put an
    unbounded, unreapable estate back on RAM, which is the exact failure the
    route exists to prevent. Silence is the bug; the doctor row and the routing
    table both read this."""
    return _tmpdir_route()[1]


def _tmpdir_route():
    """([path], trouble) — every routed TMPDIR root, and what is wrong with the
    first one.

    AN OPERATOR OVERRIDE ADDS A ROOT; IT DOES NOT DROP THE OTHERS. Returning
    the override ALONE made every root a running seat is still writing into
    invisible to the reaper the moment an operator set HELM_SCRATCH_DIR — and a
    seat keeps the env it launched with, so those roots are live for as long as
    the seat is. The override wins the ROUTE (it is first, and a new launch
    gets it); it does not win the SCAN SET."""
    if _off("SCRATCH_TMPDIR"):
        return [], None
    out, trouble = [], None

    def add(root):
        path = os.path.join(root, "tmpdir")
        if path not in out:
            out.append(path)

    try:
        pinned = (home.env("SCRATCH_TMPDIR") or "").strip()
        if pinned and pinned.lower() not in ("1", "on", "yes"):
            add(os.path.expanduser(pinned))
        override = home.env("SCRATCH_DIR")
        if override:
            add(os.path.join(os.path.expanduser(override), "small"))
        table = mount_table()
        disk, real_disk = _disk_root(table)
        add(os.path.join(disk, "tmpdir-overflow"))
        if not real_disk and not override and not pinned:
            trouble = ("the routed TMPDIR (%s) is NOT on a non-volatile mount "
                       "— no disk root was found, so a seat's unbounded and "
                       "UNREAPABLE temp estate is back on RAM. Give helm a "
                       "disk root (HELM_CACHE_DIR) or pin one with "
                       "HELM_SCRATCH_TMPDIR=<path>" % out[0])
        add(os.path.join(_ambient_tmp(), SCRATCH_LEAF))   # the root it WAS
    except Exception:
        return out, trouble
    return out, trouble


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
    genuinely broken probe) still fails the pass closed.

    THIS IS A LIVENESS READING, AND AN INCOMPLETE ONE IS PASS-WIDE TROUBLE.
    /proc readdir is ascending pid order, so a truncated process list drops the
    most recently started processes — exactly where a just-launched agent
    lives — and a truncated cmdline/environ hides a session id whose OWNER
    cannot be named, so there is no unit to attach the doubt to. Neither can be
    narrowed to a session: the whole pass is UNKNOWN and reaps nothing."""
    proc_dir = proc_dir or PROC
    projscope.spend_or_raise("listing scratch process table")
    try:
        me = os.getuid()
        pids = [n for n in os.listdir(proc_dir) if n.isdigit()]
    except OSError as exc:
        return None, None, "process table unlistable (%s)" % exc
    projscope.spend_or_raise("listing scratch process table")
    table = Reading("process table", cap=PROC_BUDGET)
    pids, complete = table.head(pids)
    if not complete:
        return None, None, table.why("liveness for every session")
    ids, cwds, live_pids = set(), set(), set()
    opaque = 0
    for pid in pids:
        projscope.spend_or_raise("scanning scratch process")
        pdir = os.path.join(proc_dir, pid)
        try:
            if os.stat(pdir).st_uid != me:
                continue
        except OSError:
            continue                  # exited between listdir and stat
        live_pids.add(int(pid))
        for leaf in ("cmdline", "environ"):
            projscope.spend_or_raise("reading scratch process evidence")
            blob_read = Reading("process %s" % leaf, cap=BLOB_CAP)
            try:
                with open(os.path.join(pdir, leaf), "rb") as f:
                    blob, whole = blob_read.read(f)
            except (FileNotFoundError, ProcessLookupError, NotADirectoryError):
                continue              # exited mid-scan: proven not-live
            except PermissionError:
                opaque += 1           # not-dumpable: opaque by design
                continue
            except OSError as exc:
                return None, None, ("process %s %s unreadable (%s)"
                                    % (pid, leaf, exc.__class__.__name__))
            if not whole:
                return None, None, blob_read.why(
                    "which session ids pid %s references" % pid)
            projscope.spend_or_raise("reading scratch process evidence")
            ids.update(m.decode("ascii") for m in _UUID_IN_BLOB.findall(blob))
        projscope.spend_or_raise("reading scratch process cwd")
        try:
            cwds.add(os.path.realpath(os.readlink(os.path.join(pdir, "cwd"))))
        except OSError:
            pass                      # a cwd we cannot read is not evidence
        projscope.spend_or_raise("scanning scratch process")
    projscope.spend_or_raise("finishing scratch process scan")
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


def _proc_stat(pid, proc_dir=None):
    """(comm, state, ppid) from ONE /proc/<pid>/stat read, or None.

    Readable even when the rest of /proc/<pid> is not — which is the whole
    point here. Parsed after the LAST ')' because comm may contain spaces and
    parens, the rule beacons.proc_ppid/proc_starttime already carry. A read
    that did not reach the end of the line is not a shorter answer — it is no
    answer, and the caller's `row is None` branch already treats that as
    UNREADABLE rather than as a classification."""
    reading = Reading("process stat", cap=STAT_READ_CAP)
    try:
        with open(os.path.join(proc_dir or PROC, str(pid), "stat"), "rb") as f:
            raw, complete = reading.read(f)
    except OSError:
        return None
    if not complete:
        return None
    comm, sep, tail = raw.rpartition(b")")
    if not sep:
        return None
    fields = tail.split()
    if len(fields) < 2 or not fields[1].isdigit():
        return None
    return (comm.partition(b"(")[2].decode("utf-8", "replace"),
            fields[0].decode("ascii", "replace"), int(fields[1]))


def opaque_hazard(pid, proc_dir=None):
    """Why this OPAQUE pid makes tier 2 UNKNOWN, or None when it cannot.

    The ladder the constants above describe: a corpse holds nothing, a live
    non-dumpable daemon cannot reach the harness estate, and a shell — or
    anything a terminal or multiplexer started — is exactly where an agent's
    cwd lives and is the one this probe cannot clear. Bounded: one stat read
    per hop.

    A WALK THAT DID NOT REACH pid 1 HAS NOT CLEARED THE PROCESS. The hop
    bound, and an ancestor whose stat could not be read, both leave the
    ancestry unknown above that point — and `not agent-adjacent` is precisely
    a claim about the whole ancestry. So an unfinished walk is a hazard whose
    text says what it could not read, rather than a silent clearance."""
    row = _proc_stat(pid, proc_dir)
    if row is None:
        if not os.path.exists(os.path.join(proc_dir or PROC, str(pid))):
            return None               # EXITED mid-scan: it holds nothing
        return "pid %s is opaque and its state is unreadable" % pid
    comm, state, ppid = row
    if state in ("Z", "X", "x"):
        return None                   # a corpse: no descriptors, no cwd
    if comm in OPAQUE_SHELL_COMMS:
        return ("pid %s (%s) is an opaque live SHELL — a shell is where an "
                "agent's cwd lives" % (pid, comm))
    walk = Reading("parent walk", cap=OPAQUE_HOPS)
    seen, cur = {int(pid)}, ppid
    while True:
        if cur <= 1:
            break                     # the whole ancestry, read to its root
        if cur in seen:
            # A PPID CYCLE IS NOT A ROOT. Two co-existing processes cannot
            # form one — fork builds a tree — so arriving back at a pid this
            # walk already read means a pid was REUSED between two stat reads
            # inside this walk, and the ancestry above that point was never
            # read. `did not reach pid 1` is the same claim here as a spent
            # hop bound, and this function's whole contract is that such a
            # walk has not cleared the process.
            walk.stop("an ancestry that returned to pid %d, which it had "
                      "already read" % cur)
            break
        if not walk.take():
            break                     # the hop bound: the rest is UNKNOWN
        seen.add(cur)
        parent = _proc_stat(cur, proc_dir)
        if parent is None:
            walk.stop("an ancestor (pid %d) whose stat could not be read" % cur)
            break
        pcomm, _pstate, pppid = parent
        if pcomm in OPAQUE_TERMINAL_COMMS:
            return ("pid %s (%s) is opaque and was started by %s (pid %d) — a "
                    "terminal is where an agent's cwd lives"
                    % (pid, comm, pcomm, cur))
        cur = pppid
    if not walk.complete:
        return "pid %s (%s) is opaque and %s" % (
            pid, comm, walk.why("whether a terminal or shell started it"))
    return None


def open_handles(under, proc_dir=None):
    """(held, opaque, hazards, unread, trouble) — every open-file target that
    sits under one of the `under` prefixes, read from /proc/<pid>/fd for
    same-uid processes, bounded in processes and, ACROSS THE WHOLE PASS, in
    descriptors. `unread` is the sentence naming whichever bound stopped the
    reading, or None when the whole table was read.

    THE BOUND IS TOTAL, NOT PER PROCESS, and the difference is the protection.
    A per-process cap read an UNSORTED os.listdir, so a process holding more
    descriptors than the cap could not protect the directory it was working in
    — the probe simply stopped reading and the tier called the unit unheld.
    Measured on a fleet desktop: seventeen same-uid processes hold more than
    256 descriptors (an editor at 972, a browser at 2846), the whole same-uid
    table is ~38,000 descriptors, and reading ALL of it costs 0.07s against
    0.06s for the truncated version. The cap bought a hundredth of a second and
    sold the one thing this probe exists for. The budget stays because a GC
    tick must never be the expensive thing — it is simply an order of magnitude
    above the whole measured table, and hitting it is reported (`unread`)
    rather than silently concluded: a probe that stopped early has not measured
    absence, so the caller must keep every unit it had not already proven held.
    THE PROCESS LIST IS THE SAME KIND OF BOUND, two lines above the descriptor
    one and cured a round later: a process past the cut cannot protect the file
    it holds open, and /proc readdir is ascending pid order, so the processes a
    truncation drops are the most recently started ones.

    `under` is what keeps this small: the probe reads every descriptor and
    remembers only the ones that could pin a scratch unit (measured on a fleet
    host: 625 same-uid processes, 38,312 open descriptors, 29,435 readable
    targets, 1,880 of them under the tmp root; the whole pass cost 0.10s).

    A deleted file still pins its pages on a tmpfs, so a `… (deleted)` target
    counts as held.

    FAIL-CLOSED on a broken probe — an unlistable /proc or a non-permission read
    error returns trouble and the caller reaps NOTHING. A PERMISSION denial is
    deliberately NOT that class, for the reason live_evidence already records: a
    same-uid process whose /proc the kernel refuses to describe has cleared its
    dumpable flag, which no agent harness does. Failing the whole tier closed on
    those would disable it permanently on any real desktop, which is exactly how
    the equivalent environ rule broke once. They are COUNTED, and each one is
    CLASSIFIED (opaque_hazard): the ones that could be hosting an agent's cwd
    come back in `hazards` and the caller treats the pass as UNKNOWN."""
    proc_dir = proc_dir or PROC
    prefixes = tuple(os.path.realpath(u).rstrip("/") for u in under or ())
    projscope.spend_or_raise("listing scratch handle table")
    try:
        me = os.getuid()
        pids = [n for n in os.listdir(proc_dir) if n.isdigit()]
    except OSError as exc:
        return None, 0, [], None, "process table unlistable (%s)" % exc
    projscope.spend_or_raise("listing scratch handle table")
    table = Reading("handle-table process list", cap=PROC_BUDGET)
    fds = Reading("handle table", cap=FD_BUDGET)
    pids, _whole = table.head(pids)
    held, opaque, hazards = set(), 0, []
    for pid in pids:
        projscope.spend_or_raise("scanning scratch process handles")
        pdir = os.path.join(proc_dir, pid)
        fdir = os.path.join(pdir, "fd")
        try:
            if os.stat(pdir).st_uid != me:
                continue
        except OSError:
            continue                      # exited between listdir and stat
        try:
            names = os.listdir(fdir)
        except (FileNotFoundError, ProcessLookupError, NotADirectoryError):
            continue                      # exited mid-scan: proven not-live
        except PermissionError:
            opaque += 1                   # not-dumpable: opaque by design
            why = opaque_hazard(pid, proc_dir)
            if why:
                hazards.append(why)
            continue
        except OSError as exc:
            return None, opaque, hazards, None, (
                "process %s fd unlistable (%s)" % (pid, exc.__class__.__name__))
        if not fds.take(len(names)):
            # THE TOTAL BUDGET IS SPENT. Everything not already proven held is
            # UNKNOWN from here on, and the caller is told so.
            projscope.spend_or_raise("finishing scratch handle scan")
            return held, opaque, hazards, fds.why("whether a unit is held"), None
        for name in names:
            projscope.spend_or_raise("reading scratch process handle")
            try:
                target = os.readlink(os.path.join(fdir, name))
            except OSError:
                continue                  # closed mid-scan, or a socket/pipe
            if target.endswith(" (deleted)"):
                target = target[:-len(" (deleted)")]
            if not prefixes or target.startswith(prefixes):
                held.add(target)
    projscope.spend_or_raise("finishing scratch handle scan")
    return (held, opaque, hazards,
            table.why("whether a unit is held") or
            fds.why("whether a unit is held"), None)


def worktree_link(path):
    """{"admin", "repo", "locked"} when `path` is a REGISTERED git worktree —
    its `.git` is a FILE holding `gitdir: <repo>/.git/worktrees/<name>` — else
    None. Two consequences the reaper owes: removing one leaves a prunable entry
    in the repo's worktree list, and a LOCKED worktree is a do-not-disturb flag
    a janitor must honour rather than discover afterwards.

    A pointer file longer than the read is NOT `no registration`: the reader
    stops and the caller's scan is told, because `there is no worktree here` is
    the answer that lets an rmtree through."""
    reading = Reading("git pointer file", cap=GITFILE_CAP)
    try:
        with open(os.path.join(path, ".git"), "rb") as f:
            blob, complete = reading.read(f)
    except OSError:
        return None
    if not complete:
        return {"admin": None, "repo": None, "locked": False,
                "unread": reading}
    for line in blob.decode("utf-8", "replace").splitlines():
        if not line.startswith("gitdir:"):
            continue
        admin = line.split(":", 1)[1].strip()
        if not admin:
            return None
        parts = admin.rstrip("/").split(os.sep)
        repo = (os.sep.join(parts[:-3]) or os.sep
                if len(parts) >= 3 and parts[-2] == "worktrees" else None)
        return {"admin": admin, "repo": repo,
                "locked": os.path.exists(os.path.join(admin, "locked"))}
    return None


def worktree_links_in(path, reading=None):
    """([link], complete) — every git-worktree registration at `path` or one
    level inside it. One level is where they actually sit: a throwaway copy is
    either the worktree itself or a parent holding a couple of them (measured
    under the tmp root: 51 registrations, all at depth 1 or 2).

    THE `complete` HALF IS THE PROTECTION. An empty list means one of two very
    different things — `I read this directory and there is no checkout in it`,
    or `I stopped reading` — and the caller turns the first into an rmtree. A
    fixture with 701 children and its worktree at readdir index 625 had its
    uncommitted work deleted by an applying pass under the old 512-entry cap:
    no UNKNOWN, no error, no line in the audit."""
    reading = reading or Reading("worktree registration scan", cap=WT_SCAN_CAP)
    out = []

    def admit(where, link):
        if not link:
            return
        if link.get("unread") is not None:
            reading.stop_from(link["unread"])
            return
        out.append((where, link))

    admit(path, worktree_link(path))
    try:
        with os.scandir(path) as it:
            for entry in it:
                projscope.spend_or_raise("scanning scratch worktree links")
                if not reading.take():
                    break
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                admit(entry.path, worktree_link(entry.path))
    except OSError as exc:
        reading.stop("a directory that could not be listed (%s)"
                     % exc.__class__.__name__)
    return reading.done(out)


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
        projscope.spend_or_raise("resolving scratch root")
        try:
            root, _ = route(cls)
        except projscope.Expired:
            raise
        except Exception:
            continue
        projscope.spend_or_raise("resolving scratch root")
        if root in seen:
            continue
        seen.add(root)
        out.append((os.path.join(root, "*"), "helm scratch (%s)" % cls))
    for base in [_ambient_tmp()] + tmpdir_roots():
        # EVERY per-uid harness root the routing can name, not only the one
        # a launch picks today. The extra ones are helm's own doing: launch
        # exports the routed TMPDIR, the harness derives `claude-<uid>` from
        # it, and that estate then lives where no `<tmp>/claude-*` glob can
        # see it. A seat keeps the env it launched with, so a seat started
        # under any of these roots is still writing into it.
        if not base:
            continue
        pattern = os.path.join(base, "claude-*", "*", "*")
        if pattern in seen:
            continue
        seen.add(pattern)
        out.append((pattern, "harness scratchpad"))
    projscope.spend_or_raise("finishing scratch root enumeration")
    return out


def _newest_mtime(path, reading=None):
    """(newest, stats, complete) — newest mtime under a tree, breadth-first,
    bounded by one `Reading`'s own cap and its shared budget.

    A DIRECTORY MTIME IS NOT A TREE MTIME, and that is why this walks at all:
    appending to a file that already exists moves the FILE's mtime and leaves
    every directory above it untouched, so a tree an agent is writing into this
    second reads as old to a one-lstat gate. The cheap gate is a cost filter;
    this is the decision.

    `complete` is the half a bound must always report. It is False when the cap
    or the budget stopped the walk, when a subdirectory could not be read, or
    when the root itself could not be stat'd — i.e. whenever the answer came
    from a truncated or partially-denied read. A sampled walk that found
    nothing recent has NOT measured absence of recent writes; it has measured
    the slice of readdir order it reached. Callers decide what an incomplete
    answer licenses: tier one may still age a tree whose session the liveness
    gate proved DEAD (nobody is writing into it), tier two may not, because its
    session is running. Fail-open to NOW — an unreadable root is never proven
    stale."""
    reading = reading or Reading("newest-write walk", cap=MTIME_STAT_CAP)
    projscope.spend_or_raise("reading scratch tree mtime")
    try:
        newest = os.path.getmtime(path)
    except OSError:
        reading.stop("a root that could not be stat'd")
        return time.time(), 0, False
    queue = [path]
    while queue:
        projscope.spend_or_raise("scanning scratch tree mtimes")
        if reading.spent_out():
            break
        d = queue.pop(0)
        try:
            with os.scandir(d) as it:
                while True:
                    projscope.spend_or_raise("scanning scratch tree mtime")
                    try:
                        e = next(it)
                    except StopIteration:
                        break
                    if not reading.take():
                        break
                    try:
                        newest = max(newest, e.stat(follow_symlinks=False).st_mtime)
                        if e.is_dir(follow_symlinks=False):
                            queue.append(e.path)
                    except OSError:
                        reading.stop("an entry that could not be stat'd")
        except OSError:
            reading.stop("a subtree that could not be read")
    projscope.spend_or_raise("finishing scratch tree mtime scan")
    return newest, reading.spent, reading.complete


# Directory names that make a tree OFF-LIMITS regardless of age or liveness: a
# harness TRANSCRIPT subtree. corpus.py already treats `/tmp/claude-*/**/
# projects/**/*.jsonl` and `**/subagents/**/*.jsonl` as TRAINING CORPUS it backs
# up copy-only — so a reaper that could delete one would be destroying the
# authored record the corpus exists to preserve. Structural refusal, not a
# heuristic: seeing the directory at all is enough.
PROTECTED_DIRS = ("projects", "subagents")


def count_entries(path, reading=None):
    """(entries, protected, complete) — bounded inode count for ranking, plus
    the transcript refusal.

    TWO ANSWERS, AND ONLY ONE OF THEM SURVIVES A BOUND. The count is a FLOOR
    when the reading stopped, which is honest and is all a ranking needs.
    `protected` is not symmetric like that: True is sound from any slice of the
    tree (seeing the directory at all is enough), and False is a claim about
    EVERY entry — so a stopped reading may return True and may never return a
    trustworthy False. That is why `complete` comes back with it: a caller that
    reads `protected=False` from an incomplete walk is the caller that deleted
    a transcript out of a 6,001-entry fixture whose `projects/` sat at readdir
    index 4,262, under a 2,000-entry cap."""
    reading = reading or Reading("entry count", cap=COUNT_CAP)
    n, queue, protected = 0, [path], False
    while queue:
        projscope.spend_or_raise("counting scratch tree entries")
        if reading.spent_out():
            break
        d = queue.pop(0)
        try:
            with os.scandir(d) as it:
                while True:
                    projscope.spend_or_raise("counting scratch tree entry")
                    try:
                        e = next(it)
                    except StopIteration:
                        break
                    if not reading.take():
                        break
                    n += 1
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if e.name in PROTECTED_DIRS:
                                protected = True
                            queue.append(e.path)
                    except OSError:
                        reading.stop("an entry that could not be stat'd")
        except OSError:
            reading.stop("a subtree that could not be read")
    projscope.spend_or_raise("finishing scratch entry count")
    return n, protected, reading.complete


def candidates(now=None, specs=None):
    """[(path, tag, mtime)] — attributable scratch children, bounded. Shape
    gate first (uuid / pid-<n>), so anything helm cannot attribute is never
    even a candidate."""
    now = time.time() if now is None else now
    rows = []
    projscope.spend_or_raise("enumerating scratch specifications")
    scan_specs = specs or scratch_specs()
    projscope.spend_or_raise("enumerating scratch specifications")
    for pattern, label in scan_specs:
        projscope.spend_or_raise("expanding scratch candidates")
        try:
            hits = glob.iglob(pattern)
        except Exception:
            continue
        while True:
            projscope.spend_or_raise("advancing scratch candidate scan")
            try:
                p = next(hits)
            except StopIteration:
                break
            except projscope.Expired:
                raise
            except Exception:
                break
            base = os.path.basename(p)
            if not (_UUID_RE.match(base) or _PIDTAG_RE.match(base)):
                continue
            if not os.path.isdir(p) or os.path.islink(p):
                continue
            rows.append((p, base, label))
    projscope.spend_or_raise("finishing scratch candidate scan")
    return rows


def tier2_keep_reason(name):
    """Why this name inside a live session's scratch is off-limits, or None.
    Conservative by construction: helm reaps what it can prove is an agent's
    own throwaway and keeps everything whose owner it cannot read."""
    if name.startswith("."):
        return "a dot-name is harness or plugin state, never an agent throwaway"
    if name in TIER2_KEEP:
        return "%s is a harness-owned directory in active use" % name
    if name in PROTECTED_DIRS:
        return ("%s holds the authored transcript record — the training corpus "
                "is never reapable" % name)
    return None


def tier2_weight(tree):
    """A CHEAP proxy for how much a live tree has accumulated: the byte size of
    its own directory inode plus its containers'. On a tmpfs a directory's size
    tracks its entry count, so three lstats rank the live trees without opening
    any of them — which is the point, since the pass may only open TIER2_TREE_CAP
    of them and must pick the ones actually growing. A proxy, not a measurement:
    on a filesystem that block-quantises directories it degrades to a tie."""
    total = 0
    for path in [tree] + [os.path.join(tree, c) for c in TIER2_CONTAINERS]:
        projscope.spend_or_raise("weighing live scratch tree")
        try:
            total += os.lstat(path).st_size
        except OSError:
            continue
    return total


def tier2_units(tree, reading=None):
    """([(path, name, own_mtime, kind)], [(path, why-kept)], complete) — the reapable
    UNITS inside one live session tree: its direct children, except that a
    child named as a harness CONTAINER (`scratchpad/`) contributes ITS children
    instead, because that is where the harness puts agent-made throwaways and
    reaping the container itself is the wrong granularity. One lstat per entry,
    no walk, bounded by scan_cap.

    EVERY UNIT CARRIES ITS KIND, and the three kinds are not the same problem:
      dir  — the throwaway worktree/home copies the tier exists for.
      file — real clutter too: a live scratchpad fills with receipts, logs,
             one-off scripts and rendered pages, and measured on a fleet host
             they were 54 of 64 picks. A plain file is reaped by UNLINK, and
             its own mtime is an EXACT age (an append moves it), so the deep
             walk a directory needs does not apply to it at all.
      link — never a unit. A symlink is one inode of clutter, its target may
             be precious and outside the estate entirely, and pricing or
             ageing one means walking through it. Kept, and said.
    Without the kind, a file or a link became a "unit" that the removers then
    refused, every pass, forever: un-reapable picks that filled the per-pass
    cap while the errors never reached the audit line.

    The scan's own bound points the SAFE way — a listing that stops admits
    fewer units and therefore proposes fewer deletions — but it is still a
    reading that stopped, so it comes back with `complete` and the pass says
    which trees it did not finish reading rather than implying it saw them
    whole."""
    reading = reading or Reading("live-tree child listing", cap=TIER2_SCAN_CAP)
    out, kept, containers = [], [], []

    def admit(entry):
        try:
            if entry.is_symlink():
                kept.append((entry.path, "a symlink is never a unit — helm "
                                         "never follows one out of the estate"))
                return
            kind = "dir" if entry.is_dir(follow_symlinks=False) else "file"
            out.append((entry.path, entry.name,
                        entry.stat(follow_symlinks=False).st_mtime, kind))
        except OSError:
            return

    try:
        with os.scandir(tree) as it:
            for entry in it:
                projscope.spend_or_raise("listing live scratch children")
                if not reading.take():
                    break
                if entry.name in TIER2_CONTAINERS:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            containers.append(entry.path)
                            continue
                    except OSError:
                        continue
                admit(entry)
    except OSError as exc:
        reading.stop("a live tree that could not be listed (%s)"
                     % exc.__class__.__name__)
        return [], kept, reading.complete
    for container in containers:
        try:
            with os.scandir(container) as it:
                for entry in it:
                    projscope.spend_or_raise("listing live scratch children")
                    if not reading.take():
                        break
                    admit(entry)
        except OSError as exc:
            reading.stop("a container that could not be listed (%s)"
                         % exc.__class__.__name__)
            continue
    return out, kept, reading.complete


def worktree_dirty(path, timeout):
    """(dirty, trouble) — does this registered worktree hold uncommitted work?

    `git status --porcelain` is the only honest answer (a lock flag says the
    owner asked not to be disturbed; dirtiness says the removal would DESTROY
    something), and untracked files count: a new module nobody has added yet is
    the work most easily lost. ONE bounded spawn, run only after every cheaper
    gate has already passed, so a pass pays for it a handful of times at most.
    A status that cannot be taken — no git, a timeout, a broken registration —
    is UNKNOWN, and unknown keeps the tree.

    The spawn goes through the vcs seam (helm/vcs.py, the one sanctioned git
    spawn site; the direct-spawn audit in tests/test_vcs.py refuses a new
    site outside it). `probe_outcome` is the op that keeps NOTHING RAN apart
    from a non-zero exit, and both of those are the UNKNOWN this function
    promises; only a completed status answers.

    `timeout` is handed down by the pass's allowance (Reading.timeout) so that
    the LAST spawn cannot outlive the pass's wall budget: a per-call timeout is
    a bound on one read, never on the reading. IT IS REQUIRED. A default of
    None is an UNBOUNDED spawn one forgetful call site away, in the function
    whose own docstring promises ONE BOUNDED spawn — the forgettable-bound
    shape this module exists to remove, and the one shape the structural
    tripwire cannot see, because a keyword default is not a bound read."""
    from . import vcs
    try:
        ran, rc, out = vcs.backend(path).probe_outcome(
            path, "status", "--porcelain", timeout=timeout)
    except Exception as exc:              # noqa: BLE001 — unknown keeps it
        return False, "git status unavailable (%s)" % exc.__class__.__name__
    if not ran:
        return False, ("git status unavailable (nothing ran: no git, or the "
                       "%ss bound expired)" % timeout)
    if rc != 0:
        return False, "git status failed (exit %d)" % rc
    return bool((out or "").strip()), None


def dirty_allowance():
    """The pass's shared `git status` allowance, as ONE Reading: a COUNT, a
    DEADLINE, and the wall time any single spawn may take.

    A count alone is not a bound on cost — the spawns each allowed their own
    timeout is a worst case of several minutes per tier, and a GC tick that
    takes minutes IS the expensive thing this module refuses to be. The wall
    clock is the bound that actually holds; the count only keeps a
    fast-but-many case from turning a pass into a spawn storm. Both are shared
    across BOTH tiers, because they spend the same pass — and the per-spawn
    timeout is narrowed by what is left of the deadline (Reading.timeout), so
    the last spawn cannot carry the pass a whole timeout past its own budget."""
    return Reading("git status allowance", cap=WT_DIRTY_CAP,
                   until=time.time() + WT_DIRTY_BUDGET_S,
                   per_call=WT_DIRTY_TIMEOUT_S)


def worktree_refusal(links, allow, scan=None):
    """Why a victim holding these worktree registrations must be KEPT, or None.

    THREE refusals, cheapest first. A SCAN THAT DID NOT FINISH is the first of
    them and the one that took a round to see: `links` is empty in exactly two
    situations, `I read this tree and there is no checkout in it` and `I
    stopped reading`, and only the first may license a removal. Then a LOCKED
    registration, a do-not-disturb flag read from the filesystem, and last an
    UNLOCKED one that still has to be proven clean. `allow` is the pass's
    shared allowance; running out of any of it is UNKNOWN, not clean."""
    if scan is not None and not scan.complete:
        return scan.why("whether a git worktree is registered inside it")
    for wt_path, link in links or ():
        if link["locked"]:
            return ("a LOCKED git worktree is registered at %s — lock is "
                    "do-not-disturb" % wt_path)
    for wt_path, _link in links or ():
        if not allow.take():
            return ("a registered git worktree is at %s and %s"
                    % (wt_path, allow.why("its dirtiness")))
        dirty, trouble = worktree_dirty(wt_path, timeout=allow.timeout())
        if trouble:
            return ("a registered git worktree is at %s and %s — dirtiness "
                    "UNKNOWN" % (wt_path, trouble))
        if dirty:
            return ("a registered git worktree at %s has UNCOMMITTED WORK — "
                    "removing it destroys the only copy" % wt_path)
    return None


def tier2_scan(trees, now, cwds, proc_dir=None, allow=None, pressed=None):
    """{victims, kept, trouble, capped, unknown, sized} — the SECOND TIER's verdict on
    the direct children of LIVE session trees. It decides; it never deletes
    (one remover, in gc, so every deletion carries the same budget and symlink
    rules).

    `trees` is [(path, label, ttl)] — the ttl already carries this tree's mount
    level and, for a volatile mount, the host-memory level.

    Order is the cheap-gate ladder tier one already uses: one lstat per child
    (no walk) rejects everything young by its own mtime; only the survivors,
    oldest first and capped, are worth a deep newest-mtime walk, ONE /proc
    handle pass, an entry count and — last, because it is the only one that
    spawns — a dirtiness check on any git worktree registered inside.

    WHAT A BOUND IS ALLOWED TO CONCLUDE is the law this tier lives under. Its
    session is RUNNING, so every protective probe here is the last thing
    standing between a working agent and its own scratch, and a probe that
    stopped early has measured nothing. A truncated handle table, a walk that
    hit its stat cap, a worktree whose status could not be taken, an opaque
    process that could be hosting an agent's cwd: each of those makes the unit
    (or, for the last, the whole pass) UNKNOWN, and UNKNOWN keeps it and says
    so — counted in `unknown` so the report can never be silent about how much
    it declined to decide. Kill-switch: HELM_SCRATCH_GC_TIER2=0.

    `pressed` is {tree: seatceiling.Pressure} for the live trees whose SEAT's
    own slice reads THROTTLED or NEAR (see PRESSURE_UNIT_FLOOR). Their units
    pass the cheap gate at ttl 0 and are probed first; each is SIZED, and one
    at least the floor is judged at ttl 0 while a smaller one keeps the tree's
    ordinary ttl. Every other predicate below applies to them unchanged.
    `sized` reports [(path, bytes, capped, slice)] for every unit sized, so
    the pass can name a pressured seat's biggest consumers."""
    kept, unknown = [], {"truncated_handles": 0, "incomplete_walk": 0,
                         "worktree": 0, "transcript": 0, "too_large": []}
    pressed, sized = pressed or {}, []

    def out(victims, trouble, capped):
        return {"victims": victims, "kept": kept, "trouble": trouble,
                "capped": capped, "unknown": unknown, "sized": sized}

    if _off("SCRATCH_GC_TIER2"):
        kept.append(("(second tier)", "HELM_SCRATCH_GC_TIER2=0 — off"))
        return out([], None, False)
    open_trees = Reading("live-tree slice", cap=TIER2_TREE_CAP)
    trees, all_trees = open_trees.head(trees)
    if not all_trees:
        kept.append(("(second tier)", open_trees.why(
            "what the live trees this pass did not open are holding") +
            " — they are not units this pass, and drain on the next one"))
    aged = []
    for tree, label, ttl in trees:
        projscope.spend_or_raise("scanning live scratch tree")
        press = pressed.get(tree)
        gate = 0 if press is not None else ttl
        units, unit_kept, listed = tier2_units(tree)
        kept.extend(unit_kept)
        if not listed:
            kept.append((tree, "the child listing of this live tree stopped "
                               "early, so the units past it were not "
                               "considered this pass"))
        for path, name, mtime, kind in units:
            why = tier2_keep_reason(name)
            if why:
                kept.append((path, why))
                continue
            if mtime >= now - gate:
                continue              # young by its own mtime: the cheap gate
            aged.append((path, name, tree, label, ttl, mtime, kind, press))
    if not aged:
        return out([], None, not all_trees)
    # A PRESSURED SEAT'S UNITS ARE PROBED FIRST, then oldest first: the probe
    # window is capped, and a seat frozen by its own scratch cannot wait for
    # the oldest units of every calm seat to drain ahead of it.
    aged.sort(key=lambda row: (row[7] is None, row[5]))
    probe = Reading("deep-probe window", cap=TIER2_PROBE_CAP)
    probed, all_probed = probe.head(aged)
    capped = not all_probed or not all_trees
    held, opaque, hazards, unread, trouble = open_handles(
        [t for t, _lab, _ttl in trees], proc_dir)
    if trouble:
        return out([], trouble + " — open handles UNKNOWN, the second tier "
                                 "reaps nothing", capped)
    if hazards:
        # NO EVIDENCE IS NOT ABSENCE OF EVIDENCE. These processes yield neither
        # a cwd nor a descriptor, and this one could be an agent's shell.
        unknown["opaque_hazard"] = len(hazards)
        return out([], "%s — no fd or cwd evidence is obtainable for it, so "
                       "the second tier is UNKNOWN this pass and reaps nothing"
                       % hazards[0], capped)
    victims = []
    walked = Reading.pool(TIER2_MTIME_BUDGET)
    counting = Reading.pool(TIER2_COUNT_BUDGET)
    scanning = Reading.pool(WT_SCAN_BUDGET)
    allow = dirty_allowance() if allow is None else allow
    priced = Reading.pool(UNATTR_STAT_BUDGET)
    for path, name, tree, label, ttl, own_mtime, kind, press in probed:
        projscope.spend_or_raise("probing live scratch child")
        real = os.path.realpath(path)
        edge = real.rstrip("/") + "/"
        size, size_capped, eff_ttl = None, False, ttl
        if press is not None:
            # SIZED BEFORE IT IS AGED: the floor decides which ttl applies.
            # A walk that stopped early reports a FLOOR, which is still
            # enough to clear the bar when it reaches it — and when it does
            # not, the unit keeps its ordinary ttl, the conservative side.
            size, _entries, size_capped = _tree_bytes(path, priced)
            sized.append((path, size, size_capped, press.slice))
            if size >= PRESSURE_UNIT_FLOOR:
                eff_ttl = 0
        if kind == "file":
            # A FILE'S OWN MTIME IS EXACT: an append moves it, so there is no
            # sample to truncate and no unknown to report.
            newest, n, at_least = own_mtime, 1, False
        else:
            newest, seen, complete = _newest_mtime(path, Reading(
                "newest-write walk", cap=TIER2_MTIME_CAP, budget=walked))
            if not complete:
                unknown["incomplete_walk"] += 1
                unknown["too_large"].append((path, seen))
                kept.append((path, "too large to age cheaply: the newest-write "
                                   "walk stopped after %d entries, so its age "
                                   "is UNKNOWN and it is kept" % seen))
                continue
        if newest >= now - eff_ttl:
            if press is not None and eff_ttl:
                kept.append((path, "its seat's slice is %s, but at %s it is "
                                   "under the %s floor ttl 0 applies to, so "
                                   "its ordinary ttl %s keeps it (written %s "
                                   "ago)" % (press.word, _human(size),
                                             _human(PRESSURE_UNIT_FLOOR),
                                             _dur(ttl),
                                             _dur(int(now - newest)))))
                continue
            kept.append((path, "written %s ago (deep mtime, ttl %s)"
                         % (_dur(int(max(0, now - newest))), _dur(eff_ttl))))
            continue
        holder = next((h for h in held if h == real or h.startswith(edge)),
                      None)
        if holder:
            kept.append((path, "a live process holds %s open" % holder))
            continue
        if unread:
            unknown["truncated_handles"] += 1
            kept.append((path, unread + " — it is kept"))
            continue
        cwd = next((c for c in cwds if c == real or c.startswith(edge)), None)
        if cwd:
            kept.append((path, "a live process is cwd'd inside it (%s)" % cwd))
            continue
        scan = Reading("worktree registration scan", cap=WT_SCAN_CAP,
                       budget=scanning)
        links, _scanned = worktree_links_in(path, scan) if kind == "dir" \
            else ([], True)
        refusal = worktree_refusal(links, allow, scan)
        if refusal:
            if "UNKNOWN" in refusal:
                unknown["worktree"] += 1
            kept.append((path, refusal))
            continue
        if kind == "dir":
            count = Reading("entry count", cap=TIER2_COUNT_CAP,
                            budget=counting)
            n, protected, counted_all = count_entries(path, count)
            at_least = not counted_all
            if protected:
                kept.append((path, "holds a harness transcript subtree (%s) — "
                                   "the training corpus is never reapable"
                             % "/".join(PROTECTED_DIRS)))
                continue
            if not counted_all:
                # THE TRANSCRIPT REFUSAL IS THE ONE THIS MODULE CALLS
                # STRUCTURAL, and a count that stopped has not looked for it.
                unknown["transcript"] += 1
                capped = True
                kept.append((path, count.why(
                    "whether a harness transcript subtree is inside it")))
                continue
        victim = {"path": path, "name": name, "tree": tree,
                  "label": label, "files": n, "at_least": at_least,
                  "age": int(max(0, now - newest)), "ttl": eff_ttl,
                  "kind": kind, "worktrees": links, "opaque": opaque}
        if size is not None:
            victim["bytes"], victim["bytes_capped"] = size, size_capped
        if press is not None and eff_ttl == 0:
            victim["pressure"] = press.slice
        victims.append(victim)
    # BIGGEST FIRST inside the oldest-first probe window, the same rule tier one
    # ranks by: the probe order is what keeps the scan cheap, the reap order is
    # what makes one pass actually give the host its memory back. A PRESSURED
    # seat's ttl-0 units lead, ranked by BYTES — the one measure of what its
    # slice gets back, where an entry count ranks a packfile below a pile of
    # receipts.
    victims.sort(key=lambda v: (v.get("pressure") is None,
                                -(v.get("bytes") or 0)
                                if v.get("pressure") else 0,
                                -v["files"], -v["age"]))
    victims = _relief_stop(victims, pressed, kept)
    reap = Reading("per-pass reap window", cap=TIER2_REAP_CAP)
    taken, all_taken = reap.head(victims)
    return out(taken, None, capped or not all_taken)


def _relief_stop(victims, pressed, kept):
    """The victims left once each pressured slice is RELIEVED, in order.

    Biggest first is only half of the rule; the other half is stopping. A
    slice whose picks so far already project it under CLEAR_FRACTION owes
    nothing more, so its later — smaller — ttl-0 units are kept and said. A
    slice whose ratio cannot be read (a THROTTLED proven by a stalled process
    under an unlimited or unreadable high) is never projected relieved: its
    biggest units all go, which is the safe side for a frozen seat."""
    from . import seatceiling
    by_slice = {p.slice: p for p in pressed.values()}
    freed, out = {}, []
    for v in victims:
        sl = v.get("pressure")
        p = by_slice.get(sl)
        if p is None:
            out.append(v)
            continue
        got = freed.get(sl, 0)
        if seatceiling.share_of(p.current, p.high) is not None and \
                p.current - got < seatceiling.CLEAR_FRACTION * p.high:
            kept.append((v["path"], "its seat's slice %s is already projected "
                                    "under %d%% of memory.high by the bigger "
                                    "units picked before it — kept"
                         % (os.path.basename(sl),
                            round(100 * seatceiling.CLEAR_FRACTION))))
            continue
        freed[sl] = got + (v.get("bytes") or 0)
        out.append(v)
    return out


def unattr_roots():
    """The directories whose UNNAMED children helm should be able to see: every
    scratch root it routes to and, unless an operator override has narrowed
    helm to one root, the ambient tmp itself, the uncapped RAM mount, and the
    routed TMPDIR. Deduplicated, existing only."""
    out, seen = [], set()
    # OPERATOR WINS, the same way route() lets it: with HELM_SCRATCH_DIR set,
    # helm writes only under the override, so the shared mounts are not its
    # business and the listing must not wander onto them.
    shared = [] if home.env("SCRATCH_DIR") else [_ambient_tmp(), "/dev/shm"]
    # EVERY root helm has ever routed to, not the ones it would pick today.
    # Routing is live: a tmp mount that loses its headroom stops being the
    # `small` root, and the tree it already holds would then be listed as an
    # unnamed stranger's — measured, a 2.1 GB `helm-scratch` read that way the
    # moment its mount crossed the headroom line.
    extra = shared + [os.path.join(m, SCRATCH_LEAF) for m in shared] \
        + tmpdir_roots()
    for path in [route(cls)[0] for cls in CLASSES] + extra:
        if not path:
            continue
        real = os.path.realpath(path)
        if real in seen or not os.path.isdir(real):
            continue
        seen.add(real)
        out.append(real)
    return out


def _tree_bytes(path, budget, reading=None):
    """(bytes, entries, capped) — bounded size of one tree, counted in BLOCKS
    (st_blocks), because on a tmpfs blocks are the RAM the tree actually holds
    and apparent size is not.

    ONE FILESYSTEM ONLY, the way `du -x` counts: an entry on another device is
    someone else's mount, not this mount's cost. Measured on the owner's box the
    two biggest rows without that rule were 529 MB AppImage mounts under the tmp
    root — a squashfs image each, not one byte of the tmpfs.

    Spends from a shared `budget` list so a listing of thousands of entries is
    one bounded pass rather than one bounded pass per row; `capped` marks a row
    whose walk stopped early, so its size reads as a FLOOR."""
    reading = reading or Reading("tree sizing", cap=UNATTR_PER_ENTRY_CAP,
                                 budget=budget)
    total, n, queue = 0, 0, [path]
    try:
        st = os.lstat(path)
        total, n, dev = st.st_blocks * 512, 1, st.st_dev
    except OSError:
        reading.stop("a root that could not be stat'd")
        return 0, 0, True
    if not stat.S_ISDIR(st.st_mode):
        return total, n, not reading.complete
    while queue:
        projscope.spend_or_raise("sizing unattributable scratch")
        if reading.spent_out():
            break
        d = queue.pop(0)
        try:
            with os.scandir(d) as it:
                for entry in it:
                    projscope.spend_or_raise("sizing unattributable entry")
                    if not reading.take():
                        break
                    n += 1
                    try:
                        est = entry.stat(follow_symlinks=False)
                    except OSError:
                        reading.stop("an entry that could not be stat'd")
                        continue
                    if est.st_dev != dev:
                        continue          # another mount: not this one's cost
                    total += est.st_blocks * 512
                    if stat.S_ISDIR(est.st_mode):
                        queue.append(entry.path)
        except OSError:
            reading.stop("a subtree that could not be read")
            continue
    return total, n, not reading.complete


def unattributable(roots=None, now=None, budget=None, scan_cap=None):
    """[row] — the children helm CANNOT attribute, biggest first.

    THE DECISION, written down because the brief asked for one either way:
    these stay UNREAPABLE, automatically, at every pressure level including
    critical. The attribution gate is the whole reason the reaper is safe to run
    unattended — helm minted the name, so helm knows the tree is scratch — and
    an unnamed child of a shared tmp root is a tree whose PURPOSE helm cannot
    read: another family's working copy, a fixture mid-run, a socket directory,
    a hand-off artifact written for the owner to read tomorrow. No open handle,
    no live cwd and a long age do not distinguish `abandoned` from `kept`; they
    only prove nobody is touching it right now, which is equally true of a
    finished report. Under critical host-memory pressure the correct automatic
    act is to escalate what helm CAN attribute (tier one and the second tier at
    a 1h ttl) and to make this cost VISIBLE with a named manual act — deleting a
    stranger's data is not a fallback for helm's own accounting gap. The real
    cure is upstream and the doctor line says it: route the tree through
    `helm scratch big|small` and it is born attributable, and therefore reapable.

    Bounded twice over: scan_cap children per root, and ONE shared stat budget
    across every row, so a root with 29,455 children cannot turn a doctor line
    into a tree walk. `at_least` marks a row whose walk hit a cap."""
    now = time.time() if now is None else now
    budget = Reading.pool(UNATTR_STAT_BUDGET) if budget is None else budget
    roots = unattr_roots() if roots is None else roots
    rootset = {os.path.realpath(r) for r in roots}
    known = {}
    try:
        for label, path in helm_paths():
            known[os.path.realpath(path)] = label
    except Exception:
        pass
    rows = []
    for root in roots:
        projscope.spend_or_raise("listing unattributable scratch")
        listing = Reading("root listing", cap=scan_cap or UNATTR_SCAN_CAP)
        first = len(rows)
        try:
            root_dev = os.stat(root).st_dev
        except OSError:
            continue
        try:
            with os.scandir(root) as it:
                for entry in it:
                    projscope.spend_or_raise("reading unattributable entry")
                    if not listing.take():
                        break
                    base = entry.name
                    if _UUID_RE.match(base) or _PIDTAG_RE.match(base):
                        continue          # attributable: tier one owns it
                    real = os.path.realpath(entry.path)
                    if real in rootset:
                        continue          # a root of its own; scanned there
                    if base.startswith("claude-"):
                        continue          # the harness per-uid root, globbed
                    try:
                        est = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if est.st_dev != root_dev:
                        # A MOUNTPOINT, not a directory: another filesystem
                        # mounted here costs this mount nothing, and its entry
                        # is the last thing anyone should be told to remove.
                        continue
                    rows.append({"path": entry.path, "root": root,
                                 "age": int(now - est.st_mtime), "bytes": 0,
                                 "entries": 0, "at_least": False,
                                 "root_unread": None,
                                 "role": known.get(real, "")
                                 or ("helm-owned runtime surface"
                                     if base.startswith("helm-") else "")})
        except OSError as exc:
            listing.stop("a root that could not be listed (%s)"
                         % exc.__class__.__name__)
        if not listing.complete:
            # A LISTING THAT STOPPED IS NOT A COMPLETE ESTATE. Every row from
            # this root carries the fact, because the number the surfaces add
            # up is a FLOOR on the cost and must not read as the whole of it.
            unread = listing.why("what else this root holds")
            for row in rows[first:]:
                row["root_unread"] = unread
    for row in rows:
        # STARVED IS NOT SMALL. Once the shared budget is gone a row's walk
        # never begins, so its "size" is the top-level lstat and it sorts to
        # the bottom no matter what it holds. That is fine for the verb, whose
        # budget is large enough to finish, and it is exactly what invalidates
        # an ORDER claim made under a small one — so the row says which it was.
        row["starved"] = budget[0] <= 0
        row["bytes"], row["entries"], row["at_least"] = _tree_bytes(
            row["path"], budget)
    rows.sort(key=lambda r: (-r["bytes"], -r["age"]))
    return rows


def _same_inode(left, right):
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _empty_tree(fd, removed):
    with os.scandir(fd) as entries:
        while True:
            projscope.spend_or_raise("advancing scratch victim deletion")
            try:
                entry = next(entries)
            except StopIteration:
                break
            name = entry.name
            if entry.is_dir(follow_symlinks=False):
                projscope.spend_or_raise("opening scratch child directory")
                try:
                    child = os.open(
                        name,
                        openflags.flags(
                            os.O_RDONLY, "O_DIRECTORY", "O_NOFOLLOW"),
                        dir_fd=fd)
                except OSError:
                    child = None
                if child is not None:
                    child_stat = os.fstat(child)
                    try:
                        _empty_tree(child, removed)
                    finally:
                        os.close(child)
                    projscope.spend_or_raise("deleting scratch directory")
                    if not _same_inode(
                            child_stat,
                            os.stat(name, dir_fd=fd, follow_symlinks=False)):
                        raise OSError("scratch child changed during deletion")
                    os.rmdir(name, dir_fd=fd)
                    removed[0] += 1
                    continue
            projscope.spend_or_raise("deleting scratch entry")
            os.unlink(name, dir_fd=fd)
            removed[0] += 1


def _remove_tree(path, removed):
    """Cooperatively remove one tree without following swapped symlinks."""
    projscope.spend_or_raise("opening scratch victim directory")
    fd = os.open(path, openflags.flags(os.O_RDONLY, "O_DIRECTORY", "O_NOFOLLOW"))
    victim = os.fstat(fd)
    try:
        _empty_tree(fd, removed)
    finally:
        os.close(fd)
    projscope.spend_or_raise("deleting scratch directory")
    if not _same_inode(victim, os.stat(path, follow_symlinks=False)):
        raise OSError("scratch victim changed during deletion")
    os.rmdir(path)
    removed[0] += 1


def reap_owned(path):
    """Remove `path` — a tree THIS process minted — whatever modes it holds.

    `shutil.rmtree(ignore_errors=True)` silently KEEPS a read-only subtree,
    and the suite plants one: a fixture that extracts a release through
    gateauthority gets 0o555 directories and 0o444 files, so a per-process
    scratch root reaped that way survived every gate run with the release
    tree still inside it (measured: 22 inodes per whole-suite run after the
    routing cure, one root per run, forever). Scratch we minted is ours to
    mode, so every real directory is made u+rwx top-down first and then the
    tree goes. Never raises: the callers are an atexit hook and a `finally`,
    and a reap that could not finish leaves a leak, not a traceback, for the
    census to find.

    THE BOUNDARY IS THE ROOT, AND IT IS HELD BY DESCRIPTOR. The first cut
    walked `os.walk(path)` with an islink guard on CHILDREN only; os.walk
    follows a symlink handed to it as the root, so a root replaced by a link
    before exit had its TARGET's children moded 0o755 -> 0o700 outside the
    named tree, through the actually registered atexit callback (measured
    against the first cut: main preserved 0o755, that cut changed it). And a
    static islink check on the root would leave a check/use window between
    the look and the chmod. So the root is opened O_DIRECTORY|O_NOFOLLOW —
    a symlinked root refuses at the open and nothing is walked or moded —
    every directory is moded through a descriptor opened O_NOFOLLOW relative
    to the walk's own fd, and only then does the plain remove run (which
    itself refuses a symlinked root). Not a claim of immunity to arbitrary
    hostile ancestor renames or ownership transfers.

    NOT the GC's remover (`_remove_tree`): that one walks a victim it does
    not own under a budget and refuses a tree that changes beneath it. This
    one owns its tree outright.
    """
    try:
        flags = openflags.flags(os.O_RDONLY, "O_DIRECTORY", "O_NOFOLLOW")
        fd = os.open(path, flags)
        try:
            os.fchmod(fd, 0o700)
            # Pin the root and each chmod target: a path check followed by
            # chmod can follow a symlink installed between the two calls.
            for _root, dirs, _files, parent in os.fwalk(".", dir_fd=fd):
                for name in dirs:
                    try:
                        child = os.open(name, flags, dir_fd=parent)
                        try:
                            os.fchmod(child, 0o700)
                        finally:
                            os.close(child)
                    except OSError:
                        pass
        finally:
            os.close(fd)
        shutil.rmtree(path, ignore_errors=True)
    except Exception:                       # noqa: BLE001 — see docstring
        pass


def live_cwds(proc_dir=None):
    """Every live same-uid cwd — the CHEAP half of live_evidence, split out
    because the deletion loop re-reads it per victim and must not pay for the
    cmdline/environ regex pass to do it (measured: 0.008s against 0.1s).

    None means UNKNOWN and the caller keeps the unit — and a process list that
    stopped early is exactly that: the cwd sitting inside this victim may be
    one of the pids the reading never reached."""
    proc_dir = proc_dir or PROC
    projscope.spend_or_raise("re-reading scratch cwd table")
    try:
        me = os.getuid()
        pids = [n for n in os.listdir(proc_dir) if n.isdigit()]
    except OSError:
        return None                       # unknown, and the caller keeps
    table = Reading("process table", cap=PROC_BUDGET)
    pids, complete = table.head(pids)
    if not complete:
        return None                       # unknown, and the caller keeps
    out = set()
    for pid in pids:
        projscope.spend_or_raise("re-reading scratch process cwd")
        pdir = os.path.join(proc_dir, pid)
        try:
            if os.stat(pdir).st_uid != me:
                continue
            out.add(os.path.realpath(os.readlink(os.path.join(pdir, "cwd"))))
        except OSError:
            continue
    return out


def _fresh_handles(rep, proc_dir):
    """The deletion-time handle reading, stamped with when it was taken.

    `at` is what makes the window measurable, and the freshness itself is a
    bounded reading: it expires on a wall deadline, the caller re-reads when it
    does, and the report records the widest age any victim was decided on."""
    held, _opaque, hazards, unread, trouble = open_handles(
        rep["tier2_trees"] + [v["path"] for v in rep["victims"]], proc_dir)
    at = time.time()
    return {"held": held, "hazards": hazards, "unread": unread,
            "trouble": trouble, "at": at,
            "fresh": Reading("handle-table freshness",
                             until=at + TOCTOU_HANDLE_MAX_AGE_S)}


def _recheck_victim(v, now, handles, proc_dir, tier2):
    """Why this victim must NOT be removed after all, or None.

    The same gates it passed, taken again immediately before the unlink, and
    the same law: a reading that stopped early keeps the unit. The caller owns
    the handle table's freshness; everything else here is re-taken per victim.
    """
    path = v["path"]
    held, unread, trouble = (handles["held"], handles["unread"],
                             handles["trouble"])
    if trouble or held is None:
        return "handle table unreadable at deletion time (%s)" % (
            trouble or "no table")
    if tier2 and handles["hazards"]:
        return ("%s — that appeared before the deletion, so held is UNKNOWN"
                % handles["hazards"][0])
    try:
        st = os.lstat(path)
    except OSError as exc:
        return "vanished before deletion (%s)" % exc.__class__.__name__
    if stat.S_ISLNK(st.st_mode):
        return "became a symlink between the probe and the deletion"
    if v.get("kind") == "file":
        if not stat.S_ISREG(st.st_mode):
            return "changed kind between the probe and the deletion"
        newest, complete = st.st_mtime, True
    else:
        if not stat.S_ISDIR(st.st_mode):
            return "changed kind between the probe and the deletion"
        newest, _seen, complete = _newest_mtime(path, Reading(
            "newest-write walk", cap=TIER2_MTIME_CAP)) if tier2 \
            else _newest_mtime(path)
        if tier2 and not complete:
            return "the newest-write walk no longer completes — age UNKNOWN"
    ttl = v.get("ttl")
    if ttl is not None and newest >= now - ttl:
        return "written %s ago between the probe and the deletion" % _dur(
            int(max(0, now - newest)))
    real = os.path.realpath(path)
    edge = real.rstrip("/") + "/"
    holder = next((h for h in held if h == real or h.startswith(edge)), None)
    if holder:
        return "a live process opened %s between the probe and the deletion" \
            % holder
    if unread:
        return "at deletion time, " + unread
    cwds = live_cwds(proc_dir)
    if cwds is None:
        return "the cwd table became unreadable at deletion time"
    if next((c for c in cwds if c == real or c.startswith(edge)), None):
        return "a live process cwd'd into it between the probe and the deletion"
    return None


def gc(apply=False, now=None, specs=None, proc_dir=None, ttl=None, rows=None,
       host=None, list_unattributable=False, seats=None):
    """One bounded reap pass -> a report dict. Dry by default.

    Order is load-bearing: measure pressure on BOTH planes — the mount, and for
    a VOLATILE mount the HOST MEMORY that mount is made of -> pick the TTL ->
    shape-gate + age-gate the candidates (cheap) -> ONLY THEN probe liveness
    (one /proc pass, and only when something aged) -> reap biggest-by-inodes
    first, up to REAP_CAP. Probe trouble reaps nothing.

    A tree kept for LIVENESS then meets the SECOND TIER: its own direct children
    are units, and an aged one nothing holds open is reapable while its session
    runs. Liveness before age still holds — it just stopped meaning `unbounded`.

    `host` is injectable so a test can drive the host axis from a fixture;
    `list_unattributable` adds the visible-cost listing (a walk, so it is off by
    default and the Stop-hook leg never pays it).

    `seats` is the THIRD plane — a `seat_plane()` reading. A live tree whose
    session belongs to a seat whose own slice reads THROTTLED or NEAR, on a
    RAM mount, is scanned first and its big unheld units are judged at ttl 0
    (tier2_scan). A tree on disk is not escalated: a disk page is reclaimable,
    and only shared memory is what freezes a seat. None means the plane was
    not read, and nothing escalates."""
    now = time.time() if now is None else now
    projscope.spend_or_raise("surveying scratch pressure")
    surveyed = rows if rows is not None else survey()
    projscope.spend_or_raise("surveying scratch pressure")
    pressure = max([worst_pct(u) for u in surveyed] or [0])
    level = level_of(pressure)
    vol_devs = volatile_devs(surveyed)
    if host is None and vol_devs:
        projscope.spend_or_raise("reading host memory pressure")
        host = host_memory()
    vol_level = worse(level, (host or {}).get("level", "ok")) if vol_devs \
        else level
    ttl_disk = TTL_BY_LEVEL[level] if ttl is None else ttl
    ttl_vol = TTL_BY_LEVEL[vol_level] if ttl is None else ttl
    rep = {"pressure": pressure, "level": level, "ttl": ttl_disk,
           "ttl_volatile": ttl_vol, "level_volatile": vol_level, "host": host,
           "candidates": 0, "aged": 0, "kept": [], "victims": [], "reaped": 0,
           "files": 0, "trouble": None, "capped": False, "tier2": [],
           "tier2_kept": [], "tier2_reaped": 0, "tier2_trouble": None,
           "tier2_trees": [], "tier2_capped": False, "tier2_unknown": {},
           "tier2_files": 0, "reap_capped": False, "probed": 0,
           "unknown": {"transcript": 0, "worktree": 0}, "sampled": 0,
           "handle_window_s": 0.0, "seat_pressure": {}, "seats": seats,
           "worktrees": [], "unattributable": [], "errors": []}
    if list_unattributable:
        try:
            rep["unattributable"] = unattributable(now=now)
        except projscope.Expired:
            raise
        except Exception as exc:          # a listing never gates a reap
            rep["unattributable"] = []
            rep["tier2_trouble"] = "unattributable listing failed (%s)" % (
                exc.__class__.__name__)
    projscope.spend_or_raise("collecting scratch candidates")
    cands = candidates(now=now, specs=specs)
    projscope.spend_or_raise("collecting scratch candidates")
    rep["candidates"] = len(cands)
    aged, live_trees = [], []
    for p, t, lab in cands:
        projscope.spend_or_raise("age-gating scratch candidate")
        cand_ttl = ttl_vol if _dev(p) in vol_devs else ttl_disk
        # TIER ONE MAY AGE FROM A SAMPLE, and the reason is the gate below it:
        # nothing reaches a deletion here until liveness has proved the session
        # DEAD, so there is no agent writing into the part of the tree the walk
        # did not reach. The 88,142-file tree class is exactly what this module
        # exists to remove, and requiring a complete walk would make the
        # biggest dead trees the only immortal ones. The sample is RECORDED on
        # the victim so the report never passes it off as a full reading.
        newest, _seen, complete = _newest_mtime(p)
        if newest < now - cand_ttl:
            aged.append((p, t, lab, not complete, cand_ttl))
    rep["aged"] = len(aged)
    probe = Reading("aged-candidate window", cap=AGED_CAP)
    aged, all_aged = probe.head(aged)
    rep["capped"], rep["probed"] = not all_aged, len(aged)
    tier2_on = not _off("SCRATCH_GC_TIER2")
    if not tier2_on:
        # THE KILL SWITCH SHORT-CIRCUITS BEFORE tier2_scan, so the row that
        # function would add never reached the report: an operator who takes
        # the documented mitigation could not see it in the dry run.
        rep["tier2_kept"].append(
            ("(second tier)", "HELM_SCRATCH_GC_TIER2=0 — off"))
    if not aged and not (tier2_on and cands):
        return rep
    projscope.spend_or_raise("probing scratch liveness")
    live, cwds, trouble = live_evidence(proc_dir)
    projscope.spend_or_raise("probing scratch liveness")
    if trouble:
        rep["trouble"] = trouble + " — liveness UNKNOWN, reaping nothing"
        return rep
    # THE SECOND TIER READS EVERY CANDIDATE, NOT THE AGED ONES. A live tree is
    # almost never aged — the session writing into it keeps its newest mtime
    # minutes old, forever — which is exactly how a days-old integrator session
    # grew to 5.6 GB while tier one was correct about every tree it looked at.
    sessions = (seats or {}).get("sessions") or {}
    pressed = {}
    for p, t, lab in cands if tier2_on else ():
        projscope.spend_or_raise("ranking live scratch tree")
        if not _referenced(t, live, cwds, p):
            continue
        volatile = _dev(p) in vol_devs
        live_trees.append((p, lab, TIER2_TTL_BY_LEVEL[
            vol_level if volatile else level]))
        if volatile and t in sessions:
            pressed[p] = sessions[t]
    # A PRESSURED SEAT'S TREE IS OPENED FIRST. The pass opens a capped number
    # of live trees, heaviest first; a frozen seat's tree must never lose that
    # slot to a calm seat's bigger one.
    live_trees.sort(key=lambda row: (row[0] not in pressed,
                                     -tier2_weight(row[0])))
    opened, _all_opened = Reading("live-tree slice",
                                  cap=TIER2_TREE_CAP).head(live_trees)
    rep["tier2_trees"] = [p for p, _lab, _ttl in opened]
    ranked, allow = [], dirty_allowance()
    counting = Reading.pool(COUNT_BUDGET)
    scanning = Reading.pool(WT_SCAN_BUDGET)
    for p, t, lab, sampled, cand_ttl in aged:
        projscope.spend_or_raise("ranking scratch candidate")
        why_live = _referenced(t, live, cwds, p)
        if why_live:
            rep["kept"].append((p, why_live))
            continue
        count = Reading("entry count", cap=COUNT_CAP, budget=counting)
        n, protected, counted_all = count_entries(p, count)
        if protected:
            rep["kept"].append(
                (p, "holds a harness transcript subtree (%s) — the training "
                    "corpus is never reapable" % "/".join(PROTECTED_DIRS)))
            continue
        if not counted_all:
            # TIER ONE MAY AGE FROM A SAMPLE (the liveness gate above proved
            # the session dead, so nobody is writing into the part the walk
            # missed) — but it may NOT decide the transcript refusal from one.
            # That refusal is about a directory EXISTING, and a reading that
            # stopped has not looked for it.
            rep["unknown"]["transcript"] += 1
            rep["kept"].append((p, count.why(
                "whether a harness transcript subtree is inside it")))
            continue
        scan = Reading("worktree registration scan", cap=WT_SCAN_CAP,
                       budget=scanning)
        links, _scanned = worktree_links_in(p, scan)
        refusal = worktree_refusal(links, allow, scan)
        if refusal:
            if "UNKNOWN" in refusal:
                rep["unknown"]["worktree"] += 1
            rep["kept"].append((p, refusal))
            continue
        ranked.append({"path": p, "tag": t, "label": lab, "files": n,
                       "at_least": not counted_all, "kind": "dir",
                       "sampled": sampled,
                       "ttl": cand_ttl, "worktrees": links})
    projscope.spend_or_raise("sorting scratch victims")
    ranked.sort(key=lambda r: -r["files"])
    projscope.spend_or_raise("sorting scratch victims")
    reap = Reading("per-pass reap window", cap=REAP_CAP)
    rep["victims"], all_reaped = reap.head(ranked)
    rep["reap_capped"] = not all_reaped
    rep["sampled"] = sum(1 for v in rep["victims"] if v["sampled"])
    if live_trees:
        projscope.spend_or_raise("scanning the second scratch tier")
        t2 = tier2_scan(live_trees, now, cwds, proc_dir, allow, pressed)
        rep["tier2"], rep["tier2_kept"] = t2["victims"], t2["kept"]
        rep["tier2_trouble"], rep["tier2_capped"] = t2["trouble"], t2["capped"]
        rep["tier2_unknown"] = t2["unknown"]
        rep["seat_pressure"] = _pressure_report(pressed, t2.get("sized") or [],
                                                seats)
    if list_unattributable:
        # THE REPORT SURFACE PRICES ITS OWN PROPOSAL. Entry counts rank a
        # victim; bytes are what the host gets back, and the operator reading a
        # dry run is the one who needs that number. It is a walk, so the
        # APPLYING pass never pays it — the same reason the listing is opt-in.
        priced = Reading.pool(UNATTR_STAT_BUDGET)
        for v in rep["victims"] + rep["tier2"]:
            v["bytes"], _n, v["bytes_capped"] = _tree_bytes(v["path"], priced)
    if not apply:
        return rep
    removed = [0]
    # THE WINDOW IS ONE VICTIM WIDE, NOT ONE PASS WIDE. Every gate above was
    # decided before the ranking, the pricing and the sort; between then and
    # the first unlink an agent can open a file in one of these units or cd
    # into it. So the evidence is re-taken here, as late as it can be — but the
    # two halves are priced very differently and only one of them can be paid
    # per victim. MEASURED on a fleet desktop (563 same-uid processes, 37,775
    # descriptors): the handle table costs 0.13s to read and the cwd table
    # 0.013s. Per victim, on the 88-victim pass this box actually proposes,
    # that is 11.4s against 1.1s — and a GC tick that takes eleven seconds IS
    # the expensive thing this module refuses to be. So:
    #   * the cwd table, the unit's kind and inode, and (for a directory) the
    #     deep newest-mtime walk are re-taken PER VICTIM;
    #   * the handle table is re-read whenever it is older than
    #     TOCTOU_HANDLE_MAX_AGE_S, which bounds that one gate's window in
    #     SECONDS rather than in victims. A window is a duration; bounding it
    #     by a count would make it wider exactly on the passes that delete the
    #     most. The report carries the widest window the pass actually ran with
    #     (`handle_window_s`) so nothing here is a silent bound.
    both = ([("reaped", v) for v in rep["victims"]] +
            [("tier2_reaped", v) for v in rep["tier2"]])
    handles = _fresh_handles(rep, proc_dir) if both else None
    try:
        for counter, v in both:
            projscope.spend_or_raise("deleting scratch victim")
            before = removed[0]
            if handles["fresh"].spent_out():
                handles = _fresh_handles(rep, proc_dir)
            age = time.time() - handles["at"]
            rep["handle_window_s"] = max(rep["handle_window_s"], round(age, 3))
            stop = _recheck_victim(v, now, handles, proc_dir,
                                   tier2=counter == "tier2_reaped")
            if stop:
                v["skipped"] = stop
                rep["errors"].append((v["path"], stop))
                continue
            try:
                if v.get("kind") == "file":
                    os.unlink(v["path"])  # never follows a link
                    removed[0] += 1
                elif projscope.deadline() is None:
                    shutil.rmtree(v["path"])
                else:
                    _remove_tree(v["path"], removed)
                rep[counter] += 1
                v["done"] = True
                # EACH TIER COUNTS ITS OWN FILES. One accumulator rendered
                # inside the tier-one sentence made the audit line say
                # "reaped 0 dead-session scratch dirs (7 files)".
                rep["tier2_files" if counter == "tier2_reaped"
                    else "files"] += v["files"]
                rep["worktrees"].extend(
                    (path, link) for path, link in v.get("worktrees") or ())
            except OSError as exc:
                v["error"] = str(exc)     # a locked victim is skipped LOUDLY
                rep["errors"].append((v["path"], str(exc)))
                if removed[0] != before:
                    v["partial"] = removed[0] - before
    except projscope.Expired:
        if rep["reaped"] or rep["tier2_reaped"] or removed[0]:
            pk.event("scratch", "gc", summary(rep) +
                     ("; %d tree entries removed before expiry" % removed[0]
                      if removed[0] else ""))
        raise
    if rep["reaped"] or rep["tier2_reaped"]:
        pk.event("scratch", "gc", summary(rep))
    return rep


def _pressure_report(pressed, sized, seats):
    """{slice: {pressure, seat, trees, units}} — what the pass saw of each
    pressured seat. `units` is every unit it sized, biggest first, as
    [path, bytes, capped]; which of them went is read off the victims AFTER
    the deletion loop, so a unit refused at deletion time is never reported
    as reaped."""
    names = (seats or {}).get("slice_seats") or {}
    out = {}
    for tree, p in pressed.items():
        row = out.setdefault(p.slice, {"pressure": p, "trees": [],
                                       "units": [],
                                       "seat": names.get(p.slice)})
        row["trees"].append(tree)
    for path, size, capped, sl in sized:
        if sl in out:
            out[sl]["units"].append((path, size, capped))
    for row in out.values():
        row["units"].sort(key=lambda u: -u[1])
    return out


def pressure_top(rep, sl):
    """[(path, bytes, capped, verdict)] — the biggest units of one pressured
    slice, bounded, each with what this pass did to it."""
    row = (rep.get("seat_pressure") or {}).get(sl) or {}
    fate = {}
    for v in rep.get("tier2") or ():
        fate[v["path"]] = ("NOT removed (%s)" % (v.get("error")
                                                or v.get("skipped"))
                           if v.get("error") or v.get("skipped") else
                           "reaped" if v.get("done") else "would reap")
    for path, why in rep.get("tier2_kept") or ():
        fate.setdefault(path, "kept: " + why)
    listing = Reading("pressure consumer listing", cap=PRESSURE_TOP_CAP)
    shown, _all = listing.head(row.get("units") or [])
    return [(path, size, capped, fate.get(path, "not judged this pass"))
            for path, size, capped in shown]


def summary(rep):
    """The one audited line. It names the host axis and the second tier only
    when they did something, so the at-rest line keeps its shape."""
    host = rep.get("host") or {}
    extra = ""
    if rep.get("tier2_reaped"):
        extra += ("; %d aged %s of a LIVE session (second tier, %d files, "
                  "ttl %s)"
                  % (rep["tier2_reaped"],
                     "child" if rep["tier2_reaped"] == 1 else "children",
                     rep.get("tier2_files", 0),
                     _dur(TIER2_TTL_BY_LEVEL[rep.get("level_volatile",
                                                     rep["level"])])))
    pressed = [v for v in rep.get("tier2") or () if v.get("pressure")
               and v.get("done")]
    if pressed:
        # THE THIRD PLANE NAMES ITSELF: a ttl-0 reap of a live seat's scratch
        # is the one pick in this line no mount or host number explains.
        extra += ("; %d of them at ttl 0 for SEAT memory pressure (%s, %s "
                  "back)"
                  % (len(pressed), ", ".join(sorted({
                      os.path.basename(v["pressure"]) for v in pressed})),
                     _human(sum(v.get("bytes") or 0 for v in pressed))))
    if rep.get("sampled"):
        # THE SAMPLE REACHES THE SURFACE OR IT IS NOT RECORDED. The docstring
        # promised "the report never passes it off as a full reading", and the
        # flag lived in the victim dict and appeared nowhere a reader looks.
        extra += ("; %d of them aged from a SAMPLED walk (the tree was too "
                  "large to read whole; its session was proven dead first)"
                  % rep["sampled"])
    tier1_unknown = rep.get("unknown") or {}
    if any(tier1_unknown.values()):
        extra += ("; %d dead-session tree%s KEPT as UNKNOWN (%s)"
                  % (sum(tier1_unknown.values()),
                     "s"[:sum(tier1_unknown.values()) != 1],
                     ", ".join("%s=%d" % (k, v) for k, v
                               in sorted(tier1_unknown.items()) if v)))
    if host.get("why") and rep.get("level_volatile") != rep["level"]:
        extra += ("; RAM mounts escalated to %s by host memory (%s)"
                  % (rep.get("level_volatile"), host["why"]))
    if host.get("trouble"):
        extra += "; " + host["trouble"]
    unknown = rep.get("tier2_unknown") or {}
    n_unknown = sum(v for k, v in unknown.items() if k != "too_large")
    if n_unknown:
        extra += ("; %d second-tier unit%s KEPT as UNKNOWN (%s)"
                  % (n_unknown, "s"[:n_unknown != 1],
                     ", ".join("%s=%d" % (k, v) for k, v in
                               sorted(unknown.items())
                               if k != "too_large" and v)))
    big = Reading("audited-line listing", cap=SUMMARY_LIST_CAP)
    shown, all_shown = big.head(unknown.get("too_large") or [])
    for path, seen in shown:
        extra += "; %s too large to age cheaply: %d entries" % (path, seen)
    if not all_shown:
        extra += ("; and %d more too large to age cheaply"
                  % (len(unknown["too_large"]) - len(shown)))
    if rep.get("errors"):
        errs = Reading("audited-line listing", cap=SUMMARY_LIST_CAP)
        named, all_named = errs.head(rep["errors"])
        extra += ("; %d victim%s NOT removed (%s%s)"
                  % (len(rep["errors"]), "s"[:len(rep["errors"]) != 1],
                     "; ".join("%s: %s" % row for row in named),
                     "" if all_named else
                     "; and %d more" % (len(rep["errors"]) - len(named))))
    if rep.get("worktrees"):
        # EVERY repo, not the first: a pass that reaps worktrees of two repos
        # and names one leaves the other holding a stale registration nobody
        # has been told about.
        repos = []
        for _path, link in rep["worktrees"]:
            repo = link.get("repo") or "<repo>"
            if repo not in repos:
                repos.append(repo)
        extra += ("; %d reaped tree%s %s a registered git worktree — run %s"
                  % (len(rep["worktrees"]),
                     "s"[:len(rep["worktrees"]) != 1],
                     "was" if len(rep["worktrees"]) == 1 else "were",
                     " and ".join("`git -C %s worktree prune`" % r
                                  for r in repos)))
    return ("reaped %d dead-session scratch dir%s (%s%d files) at %d%% "
            "pressure (%s, ttl %s); %d kept live, %d candidates%s"
            % (rep["reaped"], "s"[:rep["reaped"] != 1],
               ">=" if any(v.get("at_least") for v in rep["victims"]) else "",
               rep["files"], rep["pressure"], rep["level"],
               _dur(rep["ttl"]), len(rep["kept"]), rep["candidates"], extra))


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
    Ordinary janitor failures never raise and nothing prints; ambient-budget
    Expired propagates so the guard reports incomplete coverage instead of
    treating unfinished work as clean. The audit trail is pk.event plus doctor's
    last-pass row. Kill-switch: HELM_SCRATCH_GC=0.

    NOT OVERBEARING, in three layers: (1) PRESSURE-GATED on every plane — at
    rest (every mount under WARN_PCT, the host's own memory under its
    thresholds while helm writes to any RAM-backed mount, and no seat's slice
    THROTTLED or NEAR) the pass reaps NOTHING; deleting files nobody needs
    deleted is its own failure mode. (2) THROTTLED: the full pass to one an
    hour, the seat-plane read to one a minute fleet-wide. (3) BOUNDED, and
    liveness-gated inside gc() so a live agent's scratch is never touched.

    A PRESSING SEAT OVERRIDES THE HOURLY THROTTLE, because the seat it is
    about cannot run this leg itself: a process stalled in its slice's
    over-high throttle never reaches its own Stop hook. Any other seat's stop
    reads every slice, reaps the pressured seat's big unheld scratch, and
    wakes the people who can act — once per spell (`spells`)."""
    now = time.time() if now is None else now
    if _off("SCRATCH_GC"):
        return None
    projscope.spend_or_raise("checking scratch gc schedule")
    due = _due(now)
    seat_due = not _off("SCRATCH_GC_SEATS") and _seat_due(now)
    projscope.spend_or_raise("checking scratch gc schedule")
    if not due and not seat_due:
        return None
    plane = None
    if seat_due:
        try:
            plane = seat_plane()
        except projscope.Expired:
            raise
        except Exception:
            plane = None              # the other planes still run
        _touch_seat_stamp()
    # THE PASS IS OWED ONLY WHEN THERE IS SCRATCH TO ESCALATE. A pressing
    # slice whose sessions own no live tree (a non-claude seat, or a census
    # that could not tie it) still opens its spell below, but a full reap pass
    # every minute would buy that seat nothing.
    line, rep = _auto_pass(now, due, bool(plane and plane.get("sessions")),
                           plane)
    if plane is not None:
        # THE SPELL IS KEPT ON EVERY SEAT-PLANE READ, the calm ones too: a
        # spell ENDS on a calm reading, and a pass that stopped early for
        # being at rest is exactly the reading that ends one.
        try:
            spells(plane, rep, now)
        except projscope.Expired:
            raise
        except Exception:
            pass
    return line


def _auto_pass(now, due, pressing, plane):
    """(summary-or-None, report-or-None) — the gated pass itself. Fail-open:
    an ordinary janitor failure answers (None, None); Expired propagates."""
    if not due and not pressing:
        return None, None
    host = None
    try:
        rows = survey()
        level = level_of(max([worst_pct(u) for u in rows] or [0]))
        if volatile_devs(rows):
            # THE DENOMINATOR THAT WAS MISSING. A RAM-backed mount is made of
            # host memory, so "at rest" has to mean at rest on BOTH planes.
            host = host_memory()
            level = worse(level, host["level"])
        if level == "ok" and not pressing:
            return None, None         # at rest: a survey, and nothing else
    except projscope.Expired:
        raise
    except Exception:
        return None, None
    projscope.spend_or_raise("running scratch gc pass")
    try:
        rep = gc(apply=True, now=now, rows=rows, host=host, seats=plane)
    except projscope.Expired:
        raise
    except Exception:
        return None, None             # fail-open: a janitor never gates a stop
    projscope.spend_or_raise("stamping completed scratch gc pass")
    _touch_stamp()                    # only a completed pass earns the throttle
    return (summary(rep) if rep["reaped"] or rep["tier2_reaped"] else None,
            rep)


def _pressing(plane):
    """Is any slice THROTTLED or NEAR? A HIGH slice — near its ceiling in
    reclaimable page cache — is never pressing: it neither reaps nor wakes."""
    from . import seatceiling
    return bool(plane) and any(seatceiling.pressing(r) for r in
                               (plane.get("slices") or {}).values())


def _seat_stamp_path():
    return os.path.join(cache_root(), "scratch-gc-seats.stamp")


def _seat_due(now):
    try:
        return now - os.path.getmtime(_seat_stamp_path()) >= SEAT_THROTTLE_S
    except OSError:
        return True


def _touch_seat_stamp():
    try:
        os.makedirs(os.path.dirname(_seat_stamp_path()), exist_ok=True)
        with open(_seat_stamp_path(), "w") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# the third plane — a seat's own memory cgroup
# ---------------------------------------------------------------------------

def seat_plane(proc_dir=None, root=None, census=None, sleep=None):
    """{slices, sessions, slice_seats, trouble} — every seat's own slice, read
    NOW, and the sessions whose scratch that reading escalates.

      slices       {slice-path: seatceiling.Pressure}, every seat slice with a
                   live process in it
      sessions     {session-id: Pressure} for each session of a PRESSING slice
                   (THROTTLED or NEAR) — the key live scratch trees are named by
      slice_seats  {slice-path: seat name} for the named seats among them
      trouble      why the fleet could not be read — the slices are UNKNOWN
      unmapped     why a pressing slice could not be tied to its sessions —
                   the slices are known, nothing escalates, the wake still goes

    THE SESSION IS THE CENSUS'S WORD. The slice comes from each process's own
    cgroup line and the session from session._proc_claude_census, the one sid
    owner — never from a uuid that merely appears in some process's
    environment, which would hand one seat's scratch to another seat's
    pressure. The census is paid for only when some slice is pressing; a calm
    fleet costs one /proc walk and three small reads per slice."""
    from . import seatceiling
    projscope.spend_or_raise("reading seat memory pressure")
    readings, trouble = seatceiling.fleet_pressure(
        root=root or seatceiling.CGROUP_ROOT, proc=proc_dir or PROC,
        sleep=sleep)
    plane = {"slices": readings, "sessions": {}, "slice_seats": {},
             "trouble": trouble, "unmapped": None}
    pressing = {path: r for path, r in readings.items()
                if seatceiling.pressing(r)}
    if not pressing:
        return plane
    projscope.spend_or_raise("tying pressing seat slices to sessions")
    if census is None:
        from . import session
        census = session._proc_claude_census()
    if census.get("listing_failed"):
        plane["unmapped"] = ("the seat census failed, so no session could be "
                             "tied to a pressing slice — nothing escalates")
        return plane
    by_pid = {pid: r for r in pressing.values() for pid in r.pids}
    for row in census.get("rows") or ():
        r = by_pid.get(row.get("pid"))
        if r is None:
            continue
        name = (row.get("environ") or {}).get("HELM_CHAT_NAME")
        if isinstance(name, str) and name:
            plane["slice_seats"].setdefault(r.slice, name)
        sid = row.get("session")
        if isinstance(sid, str) and _UUID_RE.match(sid):
            plane["sessions"][sid] = r
    return plane


def _spells_path():
    return os.path.join(cache_root(), "seat-pressure-spells.json")


def spells(plane, rep=None, now=None, post=None, roster=None):
    """[(slice, room, mentions)] woken by THIS call — once per SPELL.

    A spell OPENS the first time a seat's slice reads THROTTLED or NEAR and
    CLOSES on a reading seatceiling.cleared() accepts, or when the slice is
    gone. Its opening wakes the seat's lead and the integrator, with the
    slice's numbers and its biggest scratch; a pass inside the spell wakes
    nobody. The latch is a small file in the helm cache, rewritten under an
    flock so two seats' Stop hooks cannot both open one spell.

    AN UNDELIVERED WAKE DOES NOT LATCH — the next read retries it — because a
    latch that marches past a failed post has dropped the one message the
    spell was for. A plane read with trouble changes nothing: UNKNOWN neither
    opens nor closes a spell. `post` and `roster` are injectable seams; the
    defaults are chat.post and the roster file."""
    import fcntl
    import json
    from . import seatceiling
    now = time.time() if now is None else now
    if not plane or plane.get("trouble"):
        return []
    readings = plane.get("slices") or {}
    path = _spells_path()
    if not os.path.exists(path) and not _pressing(plane):
        return []
    os.makedirs(os.path.dirname(path), exist_ok=True)
    woke = []
    with open(path + ".lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with open(path, encoding="utf-8") as fh:
                state = json.load(fh)
            if not isinstance(state, dict):
                state = {}
        except (OSError, ValueError):
            state = {}
        by_name = {os.path.basename(sl): r for sl, r in readings.items()}
        for name in list(state):
            r = by_name.get(name)
            if r is None or seatceiling.cleared(r):
                del state[name]
        for sl, r in sorted(readings.items()):
            name = os.path.basename(sl)
            if not seatceiling.pressing(r) or name in state:
                continue
            got = _wake(sl, r, plane, rep, post, roster)
            if got is None:
                continue              # undelivered: the next read retries it
            state[name] = {"since": now, "word": r.word}
            woke.append(got)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, sort_keys=True)
        os.replace(tmp, path)
    return woke


def _wake(sl, r, plane, rep, post=None, roster=None):
    """Post ONE wake for a spell that just opened -> (slice, room, mentions),
    or None when it could not be delivered."""
    from . import seatceiling, seats_common, seats_integrator
    seat = (plane.get("slice_seats") or {}).get(sl)
    try:
        rows = seats_common.roster() if roster is None else roster
    except Exception:                 # noqa: BLE001 — the integrator then
        rows = {}                     # resolves as unknown and is said so
    mentions, room = _wake_targets(seat, rows or {}, seats_common,
                                   seats_integrator)
    who = ("@%s" % seat if _inert_seat(seat, seats_common)
           else os.path.basename(sl))
    lines = ["[helm scratch] %s memory cgroup is %s"
             % (who, seatceiling.pressure_line(r))]
    top = pressure_top(rep or {}, sl)
    if top:
        lines.append("Biggest scratch of its sessions: " + "; ".join(
            "%s %s%s (%s)" % (os.path.basename(path), ">=" if capped else "",
                              _human(size), verdict)
            for path, size, capped, verdict in top))
    else:
        lines.append("None of its sessions' scratch sits on a RAM mount this "
                     "pass could size, so reaping cannot relieve it — the "
                     "memory is elsewhere in the slice.")
    lines.append("A throttled seat still reads alive on every liveness "
                 "surface. The no-restart cure is a larger memory.high on "
                 "that slice: helm store get "
                 "prior:a-throttled-seat-is-alive-beaconed-and-useless")
    if mentions:
        lines.append(" ".join("@" + m for m in mentions))
    if post is None:
        from . import chat

        def post(text, room):
            return chat.post(text, room=room, who=WAKE_BOT, sign=False)
    try:
        written = post("\n".join(lines), room)
    except Exception:                 # noqa: BLE001 — retried next read
        return None
    if not isinstance(written, dict) or not written.get("id"):
        return None
    return (sl, room, mentions)


def _inert_seat(name, seats_common):
    """A seat name that can ride a mention unchanged: the label launder's
    fixed point and the mention alphabet. Anything else is not named."""
    return isinstance(name, str) and bool(name) \
        and seats_common._seat_label(name) == name \
        and bool(seats_common._SEAT_TOKEN.fullmatch(name))


def _wake_targets(seat, rows, seats_common, seats_integrator):
    """([mention], room) for a spell's wake.

    THE INTEGRATOR is resolved from the roster, never spelled. THE LEAD is
    the freshest seated peer homed in the same team room whose own tree is a
    checkout rather than a lane room — the seat that owns the room's work.
    The all-hands room names no lead, so a seat homed there wakes the
    integrator alone. The post lands in the seat's home room, or `main`."""
    from .seats_report import presence_of
    from .seats_roster import last_seen
    integrator, _why = seats_integrator.integrator_seat(snapshot=rows)
    row = rows.get(seat) if seat else None
    room = (row or {}).get("home_room") if isinstance(row, dict) else None
    room = room if _inert_seat(room, seats_common) else "main"
    lead = None
    if room != "main":
        best = None
        for name, r in rows.items():
            if name in (seat, integrator) or not isinstance(r, dict) \
                    or not _inert_seat(name, seats_common):
                continue
            cwd = r.get("cwd")
            if r.get("home_room") != room or not isinstance(cwd, str) \
                    or "-wt/" in cwd:
                continue
            seen = last_seen(name, r)
            if presence_of(seen) == "absent":
                continue
            if best is None or (seen or 0) > best[0]:
                best = (seen or 0, name)
        lead = best[1] if best else None
    return [m for m in (lead, integrator)
            if m and _inert_seat(m, seats_common)], room


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
    except projscope.Expired:
        raise
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
        except projscope.Expired:
            raise
        except Exception:
            pass
        out.append((WARN, "fs %s (%s, %s): %s.%s  %s"
                    % (u["mount"], u["label"], u["fstype"],
                       "; ".join(why), top, _fix_line(u))))
    out.extend(host_rows(rows))
    out.extend(unattributable_rows(rows))
    try:
        trouble = tmpdir_trouble()
    except Exception:
        trouble = None
    if trouble:
        out.append((WARN, "scratch TMPDIR: " + trouble))
    last = _last_gc()
    if last:
        out.append((OK, "scratch gc: " + last))
    return out


def host_rows(rows, host=None):
    """The HOST-MEMORY leg: only when helm actually writes to a RAM-backed
    mount, because on a disk-only host this axis decides nothing. It is its own
    row rather than a clause on a mount row — a mount at 44% and a box 23 GB
    into swap are two different facts, and the reaper now reads both."""
    if not volatile_devs(rows):
        return []
    try:
        host = host_memory() if host is None else host
    except Exception:
        return []
    ram = ", ".join(str(u["mount"]) for u in rows if u.get("volatile"))
    if host.get("trouble"):
        return [(WARN, "host memory: %s — RAM mounts (%s) keep the MOUNT ttl "
                       "only" % (host["trouble"], ram))]
    axes = ("MemAvailable %s%% of RAM, swap %s%% used%s"
            % (host["avail_pct"],
               "n/a" if host["swap_used_pct"] is None else host["swap_used_pct"],
               "" if host["psi"] is None
               else ", PSI some avg60 %.1f%%" % host["psi"]))
    if host["level"] == "ok":
        return [(OK, "host memory (%s): %s" % (ram, axes))]
    # THE TTL THE REAPER WILL ACTUALLY USE, which is worse(mount, host) — the
    # host level alone understates it exactly when it matters most, on a box
    # whose RAM mount is also full.
    level = worse(level_of(max([worst_pct(u) for u in rows] or [0])),
                  host["level"])
    return [(WARN, "host memory %s: %s. EVERY BYTE of %s IS THIS MEMORY, and "
                   "no mount percentage can see it, so scratch on those mounts "
                   "drops to a %s ttl and a live session's aged children to %s "
                   "(effective level %s = worse of mount and host; %s)"
             % (host["level"].upper(), axes, ram, _dur(TTL_BY_LEVEL[level]),
                _dur(TIER2_TTL_BY_LEVEL[level]), level, host["why"]))]


def unattributable_rows(rows, listing=None):
    """The VISIBLE-COST leg: what helm will never reap, biggest first.

    Paid only when it matters — a RAM-backed mount at or over the mount warn
    threshold, or a host whose memory is already pressured — because it is a
    bounded tree walk and a doctor row at rest must cost nothing."""
    ram = [u for u in rows or [] if u.get("volatile")]
    if not ram:
        return []
    try:
        pressured = (any(worst_pct(u) >= WARN_PCT for u in ram)
                     or host_memory()["level"] != "ok")
    except Exception:
        return []
    if not pressured:
        return []
    pot = Reading.pool(UNATTR_DOCTOR_BUDGET)
    size = pot[0]
    try:
        listing = unattributable(budget=pot) if listing is None else listing
    except projscope.Expired:
        raise
    except Exception as exc:
        return [(WARN, "unattributable scratch UNMEASURED (%s)"
                 % exc.__class__.__name__)]
    if not listing:
        return []
    total = sum(r["bytes"] for r in listing)
    named, _all_named = Reading("doctor-row listing",
                                cap=UNATTR_LIST_CAP).head(listing)
    top = "; ".join("%s %s%s (%s old)"
                    % (r["path"] + (" [%s: LIVE, never remove]" % r["role"]
                                     if r.get("role") else ""),
                       ">=" if r["at_least"] else "",
                       _human(r["bytes"]), _dur(r["age"]))
                    for r in named)
    unread = next((r["root_unread"] for r in listing if r.get("root_unread")),
                  None)
    # A BUDGET SPENT IN READDIR ORDER CANNOT DELIVER AN ORDER. `unattributable`
    # sorts by bytes, but under the doctor's small budget the rows it reached
    # last were never walked at all, so their size is the top-level lstat and
    # they sort to the bottom whatever they hold. Measured on a fleet desktop:
    # at the doctor budget the three biggest trees on the box — the routed
    # TMPDIR root among them — were all absent from a row that opened with the
    # word "Biggest". The row now claims exactly what its budget bought: every
    # floor-marked row is one the walk did not finish, and the moment there is
    # one of those this is a SAMPLE, with the ranked reading one verb away.
    starved = sum(1 for r in listing if r.get("starved"))
    claim = ("Biggest (sizes marked >= are floors): %s" % top if not starved
             else "SAMPLED, NOT RANKED — this row walks under a %d-stat budget "
                  "and %d of these %d trees were never walked at all, so their "
                  "sizes are top-level floors, they sort last whatever they "
                  "hold, and the true biggest trees may be absent from this "
                  "row entirely; run `helm scratch unattributable` for the "
                  "ranked reading. Sampled: %s"
                  % (size, starved, len(listing), top))
    if unread:
        claim += ". %s, so this count is a FLOOR on the entries too" % unread
    return [(WARN, "unattributable scratch: at least %s in %d entr%s helm "
                   "cannot attribute and therefore NEVER reaps. %s. "
                   "`helm scratch unattributable` lists them; the CURE is to "
                   "mint them attributably (`helm scratch big|small` tags every "
                   "tree with its session), and the only removal is MANUAL "
                   "(`rm -rf <path>` once you have read what it is)"
             % (_human(total), len(listing),
                "y" if len(listing) == 1 else "ies", claim))]


def _last_gc():
    """The last applied auto-pass, from the events journal — the LOUD half: a
    reaper nobody can audit is not allowed.

    The journal read is bounded like every other reading here, so `no pass` and
    `no pass in the rows I read` are told apart: the second is a row of its
    own, not a silent absence."""
    reading = Reading("journal scan", cap=EVENT_SCAN_CAP)
    try:
        rows, complete = reading.head(pk.read_events(reading.ask()) or [])
    except Exception:
        return None
    for row in reversed(rows):
        if row.get("verb") == "scratch" and row.get("target") == "gc":
            return "%s (%s)" % (row.get("summary") or "?",
                                row.get("ts") or "?")
    if not complete:
        return reading.why("when this reaper last applied a pass")
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
    roots, trouble = _tmpdir_route()
    if roots:
        print("    %-8s -> %-46s %s"
              % ("tmpdir", roots[0],
                 trouble or "disk (a seat's temp estate is unreapable, so it "
                            "never lands on RAM)"))
        for extra in roots[1:]:
            print("    %-8s    %-46s (a root live seats still carry — in the "
                  "reaper's scan set)" % ("", extra))


def _victim_state(v, applying):
    """What actually happened to this pick — including the two refusals that
    only exist at deletion time. A `would reap` line about an entry the pass
    then declined to touch is the operator surface lying by omission."""
    if v.get("error"):
        return "FAILED (%s)" % v["error"]
    if v.get("skipped"):
        return "KEPT-LATE (%s)" % v["skipped"]
    return "reaped" if applying else "would reap"


def _print_gc(rep, applying):
    print("helm scratch gc — dead-session scratch (%s)"
          % ("APPLYING" if applying
             else "dry-run; `helm scratch gc --apply` reaps"))
    print("  pressure %d%% (%s) -> ttl %s; %d candidate%s, %d aged%s"
          % (rep["pressure"], rep["level"], _dur(rep["ttl"]),
             rep["candidates"], "s"[:rep["candidates"] != 1], rep["aged"],
             " (only %d probed this pass; the rest drain on the next one)"
             % rep.get("probed", 0) if rep["capped"] else ""))
    if rep.get("reap_capped"):
        print("  tier 1: more reapable trees than one pass removes — the rest "
              "drain on the next pass")
    host = rep.get("host") or {}
    if host.get("trouble"):
        print("  host memory: %s" % host["trouble"])
    elif host:
        print("  host memory %s (MemAvailable %s%%, swap %s%%%s) -> RAM-mount "
              "ttl %s, second tier %s"
              % (host.get("level"), host.get("avail_pct"),
                 "n/a" if host.get("swap_used_pct") is None
                 else host["swap_used_pct"],
                 "" if host.get("psi") is None
                 else ", PSI %.1f%%" % host["psi"],
                 _dur(rep.get("ttl_volatile", rep["ttl"])),
                 _dur(TIER2_TTL_BY_LEVEL[rep.get("level_volatile",
                                                 rep["level"])])))
    _print_seat_plane(rep)
    if rep["trouble"]:
        print("  TROUBLE: %s" % rep["trouble"])
    if rep.get("tier2_trouble"):
        print("  TIER-2 TROUBLE: %s" % rep["tier2_trouble"])
    tier1_unknown = [(k, v) for k, v in sorted((rep.get("unknown") or {}).items())
                     if v]
    if tier1_unknown:
        print("  tier 1: %d dead-session tree%s KEPT as UNKNOWN — a probe "
              "that stopped early has not measured absence (%s)"
              % (sum(v for _k, v in tier1_unknown),
                 "s"[:sum(v for _k, v in tier1_unknown) != 1],
                 ", ".join("%s=%d" % row for row in tier1_unknown)))
    if rep.get("tier2_capped"):
        print("  tier 2: more aged children than one pass probes — the rest "
              "drain on the next pass")
    unknown = rep.get("tier2_unknown") or {}
    counts = [(k, v) for k, v in sorted(unknown.items())
              if k != "too_large" and v]
    if counts:
        print("  tier 2: %d unit%s KEPT as UNKNOWN — a probe that stopped "
              "early has not measured absence (%s)"
              % (sum(v for _k, v in counts),
                 "s"[:sum(v for _k, v in counts) != 1],
                 ", ".join("%s=%d" % row for row in counts)))
    for path, seen in unknown.get("too_large") or ():
        print("  tier 2: %s is too large to age cheaply — the newest-write "
              "walk stopped after %d entries" % (path, seen))
    for p, why in rep["kept"]:
        print("  KEPT   %s  (%s)" % (p, why))
    for v in rep["victims"]:
        state = _victim_state(v, applying)
        print("  %-8s %s  (%s%d entries%s, %s%s)"
              % (state, v["path"], ">=" if v["at_least"] else "",
                 v["files"], _bytes_note(v), v["label"],
                 ", age from a SAMPLED walk" if v.get("sampled") else ""))
    for p, why in rep.get("tier2_kept") or []:
        print("  KEPT-2 %s  (%s)" % (p, why))
    for v in rep.get("tier2") or []:
        state = _victim_state(v, applying)
        print("  %-8s %s  (tier 2: %s, untouched %s, ttl %s, in a "
              "LIVE session's %s%s)"
              % (state, v["path"],
                 "one FILE%s" % _bytes_note(v) if v.get("kind") == "file"
                 else "%s%d entries%s" % (">=" if v["at_least"] else "",
                                          v["files"], _bytes_note(v)),
                 _dur(v["age"]), _dur(v["ttl"]), v["label"],
                 "; SEAT PRESSURE %s" % os.path.basename(v["pressure"])
                 if v.get("pressure") else ""))
        for wt_path, link in v.get("worktrees") or ():
            print("           registered git worktree %s -> after removal run "
                  "`git -C %s worktree prune`"
                  % (wt_path, link.get("repo") or "<repo>"))
    _print_proposal(rep, applying)
    _print_unattributable(rep.get("unattributable") or [])
    if applying:
        print("helm scratch: " + summary(rep))
    elif not rep["victims"] and not rep.get("tier2"):
        print("helm scratch: nothing reapable — %d candidate%s, all live or "
              "fresh" % (rep["candidates"], "s"[:rep["candidates"] != 1]))


def _print_seat_plane(rep):
    """The third plane on the operator surface: every pressing seat slice,
    its numbers, and what the pass saw of its scratch."""
    plane = rep.get("seats")
    if plane is None:
        return
    from . import seatceiling
    for why in (plane.get("trouble"), plane.get("unmapped")):
        if why:
            print("  seat memory: %s" % why)
    readings = plane.get("slices") or {}
    hot = sorted((r for r in readings.values() if r.word),
                 key=lambda r: (not seatceiling.pressing(r), -(r.ratio or 0)))
    if not hot:
        print("  seat memory: %d seat slice%s read, none THROTTLED or NEAR "
              "its memory.high" % (len(readings), "s"[:len(readings) != 1]))
        return
    for r in hot:
        seat = (plane.get("slice_seats") or {}).get(r.slice)
        print("  seat memory: %s%s" % ("@%s " % seat if seat else "",
                                       seatceiling.pressure_line(r)))
        for path, size, capped, verdict in pressure_top(rep, r.slice):
            print("    %9s  %s  (%s)" % ((">=" if capped else "") +
                                         _human(size), path, verdict))


def _print_proposal(rep, applying):
    """THE OPERATOR READS A TOTAL, NOT EIGHTY-EIGHT LINES. The dry run is the
    surface someone reads BEFORE authorising `--apply`, so the proposal states
    itself here, per tier, ahead of the UNATTRIBUTABLE block — which is the
    list of what this pass will NOT touch, and is no answer to `how much does
    this give back`. The bytes are there whenever the dry run has priced
    them."""
    tiers = [("tier 1 (dead sessions)", rep["victims"]),
             ("tier 2 (live sessions' aged children)", rep.get("tier2") or [])]
    if not any(picks for _lab, picks in tiers):
        return
    total_bytes, priced = 0, True
    for _lab, picks in tiers:
        for v in picks:
            if "bytes" in v:
                total_bytes += v["bytes"]
            else:
                priced = False
    print("  %s:" % ("REAPED" if applying else "WOULD REAP"))
    for label, picks in tiers:
        if not picks:
            continue
        took = [v for v in picks if not v.get("error") and not v.get("skipped")]
        size = sum(v.get("bytes", 0) for v in took)
        print("    %-40s %3d unit%s%s%s"
              % (label, len(took), "s"[:len(took) != 1],
                 ", %s%s" % (">=" if any(v.get("bytes_capped") for v in took)
                             else "", _human(size)) if priced else "",
                 "" if len(took) == len(picks) else
                 "  (%d of %d picks refused at deletion time)"
                 % (len(picks) - len(took), len(picks))))
    if priced:
        print("    %-40s      %s%s" % ("TOTAL", "", _human(total_bytes)))


def _bytes_note(v):
    """The size of a victim, when the report surface has priced it."""
    if "bytes" not in v:
        return ""
    return (", %s%s" % (">=" if v.get("bytes_capped") else "",
                        _human(v["bytes"])))


def _print_unattributable(rows, reading=None):
    if not rows:
        return
    reading = reading or Reading("listing", cap=UNATTR_LIST_CAP)
    total = sum(r["bytes"] for r in rows)
    print("  UNATTRIBUTABLE — %s in %d entr%s helm can NEVER reap (biggest "
          "first; removal is a manual act):"
          % (_human(total), len(rows), "y" if len(rows) == 1 else "ies"))
    named, all_named = reading.head(rows)
    for r in named:
        print("    %9s %-8s %s%s"
              % (("%s%s" % (">=" if r["at_least"] else "", _human(r["bytes"]))),
                 _dur(r["age"]), r["path"],
                 "  [%s — a LIVE helm surface, never remove it by hand]"
                 % r["role"] if r.get("role") else ""))
    if not all_named:
        print("    … %d more" % (len(rows) - len(named)))
    unread = next((r["root_unread"] for r in rows if r.get("root_unread")),
                  None)
    if unread:
        print("    NOTE: %s — this listing is a floor" % unread)


def cmd_scratch(args):
    """scratch [small|big|durable [--name N]] | gc [--apply] | unattributable
    [--json] | status — the mount plane. A bare `helm scratch` surveys bytes AND
    inodes and prints the routing table; a class prints the routed dir (created)
    so a caller can `cd $(helm scratch big)`; `gc` reaps dead-session scratch
    (dry-run default) and, inside a live session, its aged unheld children;
    `unattributable` lists what helm can never reap, biggest first, so the cost
    of that refusal is visible instead of silent."""
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
        # THE SWEEP FROM OUTSIDE THE SEAT reads every seat's slice, unthrottled:
        # an operator running this is often doing it FOR a seat that cannot.
        plane = None if _off("SCRATCH_GC_SEATS") else seat_plane()
        # The dry run is the REPORT surface, so it pays for the listing the
        # applying pass must not: an operator reading what would be reaped is
        # exactly who needs to see what never will be.
        rep = gc(apply=applying, list_unattributable=not applying,
                 seats=plane)
        _print_gc(rep, applying)
        if applying and plane is not None:
            for sl, room, mentions in spells(plane, rep):
                print("helm scratch: woke %s in #%s about %s"
                      % (" ".join("@" + m for m in mentions) or "nobody",
                         room, os.path.basename(sl)))
        return 0
    if verb == "unattributable":
        rest = args[1:]
        if [a for a in rest if a != "--json"]:
            print("usage: helm scratch unattributable [--json]",
                  file=sys.stderr)
            return 2
        rows = unattributable()
        if "--json" in rest:
            import json
            print(json.dumps(rows, indent=2))
            return 0
        if not rows:
            print("helm scratch: every child of every scratch root is "
                  "attributable — nothing is outside the reaper's reach")
            return 0
        _print_unattributable(rows, Reading("listing", cap=len(rows)))
        print("helm scratch: %s unattributable across %d root%s. helm NEVER "
              "reaps these — it cannot read what they are, and no open-handle "
              "or age probe distinguishes abandoned from kept. Mint scratch "
              "through `helm scratch big|small` and it is born reapable; "
              "removing one of these is a manual act."
              % (_human(sum(r["bytes"] for r in rows)),
                 len({r["root"] for r in rows}),
                 "s"[:len({r["root"] for r in rows}) != 1]))
        return 0
    if verb in CLASSES:
        rest = args[1:]
        name = None
        if len(rest) > 1 and rest[0] == "--name":
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
          "--apply] | unattributable [--json] | status [--json]   (unknown "
          "verb '%s')" % verb, file=sys.stderr)
    return 2
