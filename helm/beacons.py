#!/usr/bin/env python3
"""helm beacons — the inbox-beacon REGISTRY and its lifecycle.

A seat's inbox beacon (`helm chat wait --seat <seat> --follow`) is HELM'S ONLY
wake path to it. HELM HAS NO SECOND RE-INVOCATION PATH, so a seat whose
beacon is gone cannot be reached FROM HERE — and it looks completely normal
from outside. The qualifier is load-bearing rather than pedantic: this census
reads beacons, so what it can prove is the state of OUR leg, and a seat woken
by some other declared path is outside its instruments entirely (measured on
a project's qwen seat; the declaration that would let a verdict say so is
task/1308). That is the worst failure shape this project has: a silent one
that reads healthy.

TWO DEFECTS, MEASURED 2026-07-30, and the second is the dangerous one.

  1. ACCUMULATION. Re-arm started a new waiter and never stopped the old one,
     so the count only ever grew: 25 waiters across 11 seats when the 0.2
     council measured it, and `pgrep -af "helm chat wait" | wc -l` at 48 the
     night this lane opened. Nothing reaped, and re-arming added another.

  2. LIVENESS WAS INFERRED FROM PROCESS SHAPE, NOT FROM SESSION IDENTITY.
     `seats.beacon_procs` matched the ARGV SHAPE of a `helm chat wait`
     process, so an ORPHAN waiter left over from a DEAD session satisfied it.
     That made `resumeturn`'s recovery claim — "It has an armed inbox beacon
     and will wake on its next @mention" — satisfiable by a zombie. A
     compacted seat that could not auto-resume, covered only by an orphan
     waiter, would stay DARK FOREVER while the alert said it was covered.
     A live seat measured exactly those orphans on itself: detached shells,
     children of dead claude sessions (chat #helm row 623).

THE CURE IS THE SHAPE POSTED IN row 623, taken as posted: "A beacon registering
itself (seat + session + arm-time in a small registry the reaper reads) covers
exactly that: the registry survives the session that armed it. Otherwise every
compaction mints one more unattributable waiter." Its lesson is the load-bearing
part — "stop your own by the task id YOU hold" FAILS at a compaction boundary,
because the holding session is dead and nobody holds the task id any more. A
file outlives the session; a task id does not.

THE ROW IS KEYED ON PID, AND SESSION IS AN ATTRIBUTE THAT MAY LEGALLY CHANGE.
This is not a storage detail, it is the difference between fixing this bug and
inheriting it. A COMPACTION RENAMES THE SESSION INSIDE THE SAME PROCESS —
measured live 2026-07-30 on the integrator's own pane, which was declared
unrecoverable by a session-id mismatch while it was mid-turn in that very
process. A registry keyed on session would invalidate a perfectly armed
beacon's row every time its seat compacted. Keyed on pid, a compaction is a
non-event: the row still names the same process, and its session attribute is
simply a fact that has moved on.

    THE PREDICATE THIS FILE DELIBERATELY DOES NOT HAVE, for the same reason:
    "the launcher is alive but now holds a DIFFERENT session, therefore the old
    session is dead." It is tempting (it would classify every post-compaction
    beacon crisply) and it is the same wrong inference wearing the opposite
    sign — it would call a live seat's real beacon a ghost. Death is proven
    from PROCESS identity only. A launcher alive under a different session is
    UNKNOWN, and UNKNOWN keeps the process.

THE ALARM IS THREE-DIRECTIONAL, and reporting only one direction is how the
count reached 48 while every check said fine:

  * a DEAF SEAT   — no live beacon: HELM cannot wake it. The claim is
    deliberately about OUR leg and not about the world: this census reads
    beacons, so a seat woken by some other declared path is outside what it
    can see, and saying "nothing can wake it" was an over-claim it had no
    instrument for (measured on a project's qwen seat, whose external
    non-consuming leg is real and undeclarable — task/1308 adds the
    declaration and the verdict that needs it).
  * a GHOST WAITER — a beacon whose session is proven dead: it consumes the
    seat's addressed rows into a pipe nobody reads, and it satisfies a naive
    shape check, so it makes a dark seat read as covered.
  * a VACANT SEAT — the exact INVERSE of a ghost: a LIVE wake path for a seat
    with no agent. Both are the same underlying error — treating the
    instrument as the thing it measures.

They are opposite defects and each hides the other.

VACANT, and the reason a session check could never have found it (measured on
the census's FIRST live run, 2026-07-31). Two seats retired that night read
"covered, live 1", and every fact underneath was true: their beacons carry
HELM_CHAT_NAME in their own environ and a live session holds each one. They
reconcile because A BEACON IS A `helm chat wait` PYTHON PROCESS, NOT AN AGENT —
so "covered" was TRUE OF THE WAKE PATH and unproven OF THE SEAT.

    THE CROSS-CHECK IS DELIBERATELY MADE OF DIFFERENT EVIDENCE than the rung it
    checks. The LIVE rung already says "a live process holds this beacon's
    session"; an agent test built on that same session/roster binding would
    agree with it by construction and could never disagree — which is exactly
    how this survived a lane that was otherwise right. So the agent question is
    asked of what a PANE DECLARES ABOUT ITSELF (the launch seam's
    HELM_CHAT_NAME, or a seat-family CLAUDE_CONFIG_DIR), which is evidence the
    beacon never touched.

AND ITS FAILURE DIRECTION IS THE WHOLE DESIGN. Not every pane carries that
stamp: a hand-launched `claude` joins the roster under a DERIVED name and
declares nothing, and 8 of the 16 live panes on this box are exactly that. A
bare "no pane declares this seat -> VACANT" would therefore have called every
one of those seats vacant WHILE IT WAS WORKING — including, all through the
night it was posting council verdicts, the very seat that motivated this
verdict. So VACANT is refused whenever the process the beacon actually writes
into declares no seat of its own: an UNATTRIBUTABLE pane may be this seat's
agent, and that is UNPROVEN. Only when that process is provably SOMEBODY ELSE'S
agent is the house proven empty.

Calling a working seat VACANT is worse than the bug it fixes, because a reader
would stop addressing work to a seat that is working — so every unprovable
branch fails toward UNPROVEN, the same law that keeps this module from killing
a live seat's wake path.

SAFETY — WHAT THIS MODULE MAY AND MAY NOT SIGNAL. It NEVER kills an agent
process; the only thing it ever signals is a `helm chat wait` waiter proven to
belong to ONE named seat, and only during THAT SEAT's OWN re-arm. There is no
fleet-wide reap verb here on purpose: `helm beacons` is a pure READ. Three
independent gates must all pass before a pid is signaled, and every one of them
fails toward KEEPING the process:

  * SHAPE — `rearm._helm_subargv` exact-token matching (a standalone `helm`
    token, or `-m helm`, then `chat wait`). Never a substring: a bash `-c
    'helm chat wait …'` wrapper carries that text inside ONE argument and is
    excluded by construction, and matching it would kill the wrapper and
    orphan its child (row 624: "reap the PYTHON process, not
    the grep match").
  * ATTRIBUTION — a registry row for THIS seat naming this pid with a matching
    start-time, or an exact `--seat`/`HELM_CHAT_NAME` match. Unattributable is
    never a target. This is why reaping is scoped by SEAT and never by a
    process-table pattern: a developer box routinely runs seats belonging to
    OTHER PROJECTS' FLEETS, and a pattern match over the process table cannot
    tell them apart from ours.
  * HOME — the target's effective helm home must equal ours, so a second helm
    home (or a hermetic test) can never reach across into a live fleet's
    waiters even if a seat name collides.

UNKNOWN NEVER AUTHORIZES A KILL. If liveness cannot be proven the process is
kept, because killing the wrong waiter severs a live seat's wake path
while killing nothing merely leaves clutter that the next re-arm collects.
"""
import errno
import glob
import json
import os
import re
import signal
import stat
import sys
import time

from . import beacon_origin, consumption, home, pk
from .seats_advice import BEACON_TIMEOUT_MS

PROC = "/proc"
REG_SUBDIR = "beacons"
_SEAT_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# Beacon-row states.
LIVE, GHOST, UNKNOWN, GONE = "live", "ghost", "unknown", "gone"
# EXPIRED is a GONE row whose death has the shape of the harness's own lease
# (task/3055). Claude Code caps every Monitor at BEACON_TIMEOUT_MS and SIGTERMs
# it there; the waiter's `finally` never runs, so its registry row survives it.
# A row whose `armed` stamp puts its death at that deadline is not evidence of
# a fault: it is the seat's half-hourly check-in, and the seat re-arms in the
# turn the expiry wakes. The row is KEPT for REARM_GRACE_S so the census can
# say so, instead of pruning it and calling the seat DEAF for a 30-second gap.
EXPIRED = "expired"
# Seat verdicts. UNPROVEN is not a softer DEAF: DEAF is a PROVEN absence of any
# wake path, UNPROVEN means the instruments could not answer. Collapsing them
# would either invent a crisis or hide one. VACANT is not a softer COVERED
# either: it is a PROVEN live wake path with a PROVEN empty house.
COVERED, DEAF, UNPROVEN, VACANT = "covered", "DEAF", "UNPROVEN", "VACANT"
# And DEAF-IN-EFFECT is not a softer DEAF either: the wake path is PROVEN LIVE,
# the house is PROVEN OCCUPIED, and rows addressed to this seat have sat
# undelivered for longer than any turn takes. Every liveness instrument is
# right and the seat is not being woken. A seat can be in this state while its
# process takes turns — turn age answers a question about the PROCESS, and this
# one is about CONSUMPTION.
DEAF_IN_EFFECT = "DEAF-IN-EFFECT"
# MISROUTED is the beacon that exists, runs, and delivers TO THE WRONG PLACE.
# A Monitor's events go to the agent that CREATED it, so a beacon armed inside
# a SUBAGENT keeps the registry row, keeps the live process, keeps advancing
# the wake cursor — and every wake line lands in a sidechain that ends when the
# subagent does. Every liveness instrument reads COVERED and the seat is deaf.
# It is not a softer DEAF-IN-EFFECT: that one is about CONSUMPTION and can be
# read off rows alone; this is about OWNERSHIP and needs the holder's own
# transcript to answer. A seat cannot tell the two apart from the inside,
# because a subagent inherits the seat's environ, session id and HELM_CHAT_NAME
# — only the harness boundary separates them.
MISROUTED = "MISROUTED"
# WAKING is not a softer DEAF either, and it is the verdict the 30-minute lease
# made necessary (task/3055). No beacon is live, but the newest one ended at
# its lease deadline within the re-arm grace, and a pane DECLARES the seat, so
# the agent that re-arms it is home. It is the NORMAL state of a healthy seat
# for about 30 seconds every half hour. It is outside the alarm class, the
# re-arm nudge never types into it, and it becomes DEAF once the grace ends.
WAKING = "WAKING"
#: WHICH CONVERSATION armed a beacon. Named rather than spelled inline so the
#: producer and every consumer share ONE spelling -- this module has already
#: been bitten once by a second module-level binding of a verdict word, where
#: last-assignment-wins silently rebound the seat verdict and every consumer
#: stopped matching its own stored rows without anything raising.
OWNER_MAIN = "main"
OWNER_SUBAGENT = "subagent"

# THE GRACE, AND WHY IT IS THIS LONG. The symptom it exists to catch is
# measured in HOURS, and the cost of firing early is an alarm the fleet learns
# to ignore, so this errs entirely toward late. A seat drains its inbox at tool
# boundaries, so the longest legitimate silence is one blocking foreground
# operation; the longest measured here is a whole-suite gate on the rotational
# node at 2242 seconds, and gates run remotely rather than in the seat's own
# turn. 45 minutes clears that by a wide margin and still catches a four-hour
# outage in the first hour.
UNDRAINED_S = 45 * 60

# THE CENSUS CADENCE, defined here rather than beside the timer units below
# because the re-arm grace is derived from it and module constants bind in
# order. `timer_units` and `ensure_timer` read this same name.
INTERVAL_S = 5 * 60

# THE RE-ARM GRACE: two census passes (task/3055). A seat whose beacon ended at
# its lease deadline re-arms in the turn the expiry wakes; measured on the
# live fleet at 31 seconds. Two passes is long enough that one late or skipped
# pass never turns that gap into DEAF, and short enough that a seat which
# never re-arms reads DEAF within ten minutes. DERIVED, never a bare number,
# for the reason `seat_usability._beacon_stale_s` gives: a bound written as a
# number drifts when the cadence changes and nothing reports the drift.
REARM_GRACE_S = 2 * INTERVAL_S

# How far before its lease deadline a beacon may die and still read as having
# EXPIRED. `armed` is stamped when the waiter registers, which is after the
# harness started the Monitor, so the kill lands a little BEFORE armed +
# timeout. One minute covers that start-up skew; a death earlier than this is
# not the lease and stays DEAF at once.
EXPIRY_SKEW_S = 60

# "the caller did not probe" — distinct from a probe that ran and failed, which
# is None. Without it a census that already knows the scan failed would re-run
# it once per seat and get the same failure every time.
_UNPROBED = object()


# A SENTINEL AND NOT None, because None is the REAL answer `live_sessions`
# returns for an unreadable session registry. Collapsing "not fetched yet" into
# "fetched and unreadable" would make the lazy read below indistinguishable
# from its own failure.
_UNREAD = object()


def _proc_root(proc_dir=None):
    return proc_dir or home.env("PROC") or PROC


def valid_seat(seat):
    """A seat name safe to use as a registry FILENAME component.

    The registry is keyed by (seat, pid) in the file name, so a name carrying
    `/` or `..` would write outside the registry dir. Anything that does not
    match the roster's own alphabet is refused and NOTHING is written — the
    beacon still runs, it just does not register (fail-open: a registry miss
    costs a census row, a path escape costs the filesystem)."""
    return bool(seat) and bool(_SEAT_RE.match(str(seat)))


def _seat_key(seat):
    """Canonical identity key for locks, registry paths, and set membership.

    Seat identity is casefold-exact across chat addressing and the roster. A
    raw-cased filesystem key would split one logical seat into two election
    domains (`Kimi` vs `kimi`) and let concurrent replacements signal each
    other. Display casing stays in registry rows and diagnostics; ownership
    uses this one key everywhere."""
    return str(seat).casefold()


# ---------------------------------------------------------------------------
# /proc reads — every one of them tri-state, because "I could not look" and
# "it is not there" are different answers and only one of them is evidence.
# ---------------------------------------------------------------------------

def _read(pid, name, proc_dir=None):
    """(bytes, err) — err is None on success, "gone" when the process proved
    absent, and a reason string when the read failed for any other cause."""
    path = os.path.join(_proc_root(proc_dir), str(pid), name)
    try:
        with open(path, "rb") as f:
            return f.read(256 * 1024), None
    except (FileNotFoundError, NotADirectoryError, ProcessLookupError):
        return b"", "gone"
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return b"", "gone"
        return b"", "%s unreadable (%s)" % (name, exc.__class__.__name__)


def _proc_argv(pid, proc_dir=None):
    """(argv | None, read error | None), preserving gone vs unreadable."""
    raw, err = _read(pid, "cmdline", proc_dir)
    if err:
        return None, err
    return ([p.decode("utf-8", "replace")
             for p in raw.split(b"\0") if p] or None), None


def proc_argv(pid, proc_dir=None):
    return _proc_argv(pid, proc_dir)[0]


def _env_map(raw):
    out = {}
    for item in raw.split(b"\0"):
        key, sep, value = item.partition(b"=")
        if sep:
            out[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return out


def proc_env(pid, proc_dir=None):
    """The process's environment as a dict, or None when it cannot be read.
    None is NOT an empty environment — callers must treat it as unknown."""
    raw, err = _read(pid, "environ", proc_dir)
    return None if err else _env_map(raw)


def proc_cwd(pid, proc_dir=None):
    """The process's current directory, or None when it cannot be read."""
    path = os.path.join(_proc_root(proc_dir), str(pid), "cwd")
    try:
        return os.path.realpath(os.readlink(path))
    except OSError:
        return None


def proc_starttime(pid, proc_dir=None):
    """/proc/<pid>/stat field 22 — the incarnation key, or None.

    THE canonical anti-pid-reuse token: a recycled pid wears a different one.
    Parsed after the LAST ')' because comm may contain spaces and parens."""
    raw, err = _read(pid, "stat", proc_dir)
    if err:
        return None
    try:
        return int(raw.decode("utf-8", "replace").rsplit(")", 1)[1].split()[19])
    except (IndexError, ValueError):
        return None


def pid_alive(pid, starttime=None, proc_dir=None):
    """True | False | None — alive, PROVEN gone, or unknown.

    False is the only value that is ever evidence of death, and it is returned
    for exactly two proven cases: the process is absent, or the pid is now worn
    by a different incarnation (start-time mismatch). A read that merely FAILS
    returns None and no caller may treat that as death."""
    raw, err = _read(pid, "stat", proc_dir)
    if err == "gone":
        return False
    if err:
        return None
    if starttime is None:
        return True                  # alive; incarnation unverifiable
    got = None
    try:
        got = int(raw.decode("utf-8", "replace").rsplit(")", 1)[1].split()[19])
    except (IndexError, ValueError):
        return None
    return True if got == int(starttime) else False


def proc_exe(pid, proc_dir=None):
    """The realpath of the running EXECUTABLE, or None — THE UNFORGEABLE ONE.

    argv is writable BY THE PROCESS ITSELF; /proc/<pid>/exe is a kernel symlink
    to the inode it is executing. A pid that wants to be mistaken for an agent
    can set argv0 to anything, and cannot touch this. Same-uid processes are
    readable, which is every seat of ours; a foreign or vanished one returns
    None, and None is unknown rather than a denial."""
    try:
        return os.path.realpath(os.readlink(
            os.path.join(_proc_root(proc_dir), str(pid), "exe")))
    except OSError:
        return None


def proc_ppid(pid, proc_dir=None):
    """/proc/<pid>/stat field 4 — the CURRENT parent, or None."""
    raw, err = _read(pid, "stat", proc_dir)
    if err:
        return None
    try:
        return int(raw.decode("utf-8", "replace").rsplit(")", 1)[1].split()[1])
    except (IndexError, ValueError):
        return None


def _proc_comm(pid, proc_dir=None):
    """(comm | None, read error | None), preserving gone vs unreadable."""
    raw, err = _read(pid, "comm", proc_dir)
    return (None, err) if err else (
        raw.decode("utf-8", "replace").strip(), None)


def proc_comm(pid, proc_dir=None):
    return _proc_comm(pid, proc_dir)[0]


# The processes that ADOPT orphans. pid 1 is the textbook answer and it is not
# the one that fires on a systemd host: a user session runs under a per-user
# manager (`systemd --user`) which is a CHILD SUBREAPER, so an orphaned process
# reparents to THAT, never to init.
_REAPERS = ("systemd", "init")


def orphaned(pid, proc_dir=None):
    """True | False | None — is this waiter's LAUNCHER gone?

    An orphaned beacon is the purest form of the lie this module exists to end:
    it keeps polling, keeps stamping its seat's presence beat, and keeps
    CONSUMING that seat's addressed rows into a pipe whose reader is dead. It
    satisfies any argv-shape check perfectly and can wake precisely nobody.

    THE TEXTBOOK PREDICATE IS WRONG ON THIS HOST CLASS, and that is measured,
    not theorised. `getppid() == 1` is what helm's own `seats._beacon_orphaned`
    self-check has always used. Measured across all 22 live beacons on this box
    2026-07-30: ZERO have ppid 1. The one genuinely orphaned beacon — seat
    gemini, whose pane reads `pane=-, turn=off` — has ppid 3915, which is
    `/usr/lib/systemd/systemd --user`, uid 1000. systemd's user manager sets
    itself as a CHILD SUBREAPER, so it adopts the orphans of its own session
    and init never sees them. The ppid==1 check is therefore DEAD CODE here,
    and gemini's orphan is the live proof: it is exactly the process that check
    exists to retire, and it has been running for hours.

    So the question is asked of the PARENT rather than of a magic number: a
    beacon whose parent is a reaper was adopted, which means the shell or agent
    that launched it is gone. helm never launches a beacon from systemd (the
    Monitor tool inside an agent does), so a reaper parent cannot be its
    original one. Every other parent — the bash wrapper, the claude pid — means
    the launcher is alive.

    None when the parent cannot be identified, and None keeps the process."""
    return parent_lost(proc_ppid(pid, proc_dir), proc_dir)


def parent_lost(ppid, proc_dir=None):
    """True | False | None for a PARENT PID — the predicate `orphaned` is made
    of, split out so a process asking about ITSELF can pass `os.getppid()`.

    That split is load-bearing rather than tidy: getppid() is the race-free
    answer to "who is my parent" and it is what `seats._beacon_orphaned` must
    use, while a census asking about somebody ELSE has only /proc."""
    if ppid is None:
        return None
    if ppid == 1:
        return True
    comm = proc_comm(ppid, proc_dir)
    if comm is None:
        return None                          # cannot name the parent -> UNKNOWN
    return comm in _REAPERS


def is_waiter(pid, proc_dir=None, argv=None):
    """True only for the EXACT beacon argv shape (rearm.py's gate).

    Exact TOKEN matching, never a substring: the bash `-c 'helm chat wait …'`
    wrapper carries the whole command inside one argument, has no standalone
    `helm` token, and is excluded here by construction."""
    from . import rearm
    argv = argv if argv is not None else proc_argv(pid, proc_dir)
    if not argv:
        return False
    sub = rearm._helm_subargv(argv)
    return bool(sub) and sub[:2] == ["chat", "wait"]


# WHAT A WAITER'S STDOUT IS CONNECTED TO — the one property `waiter_spec`
# structurally cannot see. That function reconstructs behaviour from ARGV, and
# A REDIRECTION IS NOT IN ARGV: the shell consumes `> file` before the process
# is execed, so a waiter can be LIVE, carry `--follow`, omit `--any`, match its
# seat and session, and satisfy every field the coverage predicate reads while
# waking nobody. It consumes addressed rows from the ledger and writes them
# where nothing reads. A cross-family read found it against the agreed
# contract, and that contract had never looked below argv.
#: THE OPERATOR'S OWN DECLARATION THAT A FILE CAPTURE IS WANTED. Spelled once
#: here because two readers ask about it -- the arm-time refusal and the
#: delivery fence -- and a second spelling is how the two came to disagree:
#: the arm accepted the override and let the beacon start, then the fence
#: refused every row it read, so the operator got a waiter that ran forever
#: and consumed nothing while the rows stayed PENDING.
FILE_SINK_OK = "HELM_BEACON_FILE_SINK_OK"
SINK_REFUTES = "refutes"
SINK_ADMISSIBLE = "admissible"
SINK_UNKNOWN = "unknown"


# THE DISCARD IS NAMED BY THE PLATFORM, NOT BY A PATH THIS READER STATS.
# /dev/null is character device (1, 3), fixed by the kernel's device registry,
# and a device number is global to the kernel rather than to a mount namespace
# — so the object a waiter holds can be identified without consulting any
# reference pathname at all.
#
# READING `os.devnull` WAS A SEPARATE UNTRUSTED REFERENCE and it failed in both
# directions. If the caller could not stat it the comparison silently became
# "not null", so a waiter genuinely discarding its output was called
# ADMISSIBLE — an unavailable reference turning into coverage, which is the
# wrong fail direction for a value that decides whether a process is signalled.
# And if the caller's /dev/null were an impostor character node, a target
# holding that same endpoint under its ordinary name would be called REFUTES
# and could then be stopped. A constant has neither failure because it asks
# nothing of the filesystem.
_NULL_RDEV = os.makedev(1, 3)


def waiter_sink(pid, proc_dir=None):
    """The readlink of the process's fd 1, or None when it cannot be read."""
    path = os.path.join(_proc_root(proc_dir), str(pid), "fd", "1")
    try:
        return os.readlink(path)
    except OSError:
        return None


def sink_state(pid, proc_dir=None):
    """Whether this waiter's stdout can reach a reader — one of three answers.

    NECESSARY, NEVER SUFFICIENT, and that asymmetry is the whole content here.
    A REGULAR FILE or /dev/null REFUTES coverage outright: nothing consumes
    those, so a row delivered there is gone from the ledger and read by no one.
    A SOCKET or PIPE is ADMISSIBLE AND NOTHING MORE — this side cannot see
    whether anything still holds the far end, so it proves only that the sink
    is not provably dead. Unreadable is UNKNOWN.

    THE CALLER MUST REQUIRE `SINK_ADMISSIBLE` RATHER THAN REJECT
    `SINK_REFUTES`, because UNKNOWN and REFUTES must not share a value: a seat
    wrongly told to arm loses one tool call, and a seat wrongly told it is
    covered goes deaf until somebody notices. Same fail direction as every
    other unknown in `_one_live_incumbent`.

    A TTY IS DELIBERATELY ADMISSIBLE, and it is the bound worth stating: a
    `chat wait` on a pane cannot re-invoke a turn loop either, so a character
    device that is not /dev/null is arguably non-waking too. It is not refuted
    here because this function refuses only what it can PROVE reaches no
    reader, and a terminal has one."""
    # STAT THE FD, NEVER THE PATHNAME IT READLINKS TO. `readlink` hands back a
    # PATH, and a path is not the open file description: unlink the file and it
    # becomes "<path> (deleted)" which stats ENOENT, or let something else take
    # that name and the stat describes THE OTHER OBJECT. Both readings are
    # about a name; the waiter is holding a FILE. Stat'ing the `/proc/<pid>/fd/1`
    # entry itself follows the magic link to the description and answers about
    # the thing that is actually open — measured: after replacing the path with
    # a socket, stat(readlink) raises ENOENT while stat(fd) still says REG.
    #
    # IT MATTERS MORE HERE THAN IT WOULD ELSEWHERE, because this classification
    # now decides whether `arm` treats an incumbent as non-waking, and on the
    # election path that reaches `stop_superseded`. A cross-family read named
    # the mismatch and that consequence; a judgement that ends in a signal is
    # not a place to be reading names.
    return sink_probe(pid, proc_dir)[0]


def sink_usable_for(dest, follower=True):
    """False when this DESTINATION is PROVEN to reach no reader, else None.

    `follower` says whether the consumer keeps running after it writes. Only
    then can a filter reading its pipe hold a line: a one-shot consumer exits
    after its line, and that exit flushes every filter downstream of it.

    THE ONE CLASSIFIER OF AN ACTUAL DESTINATION, and it takes the OBJECT the
    bytes go to rather than the function that writes them. Knowing which
    function runs is not knowing where its bytes land: `print` writes to
    whatever `sys.stdout` is bound to at that moment, which is not fd 1 by
    nature, so a check keyed on callable identity was measured wrong in BOTH
    directions -- crediting a follower whose fd 1 was a pipe while its stdout
    was redirected to /dev/null, and refusing a perfectly good in-process
    collector whose fd 1 happened to be /dev/null.

    THE OVERRIDE IS PART OF THE CLASSIFICATION, NOT A GATE BESIDE IT. An
    operator who sets `HELM_BEACON_FILE_SINK_OK=1` has declared that a regular
    file IS the destination they want, and a classifier that overrules them is
    measuring the wrong authority: the arm-time check already lets that beacon
    start, so a fence that then refuses every row leaves a waiter running
    forever against rows that stay PENDING. It excuses a regular FILE only --
    /dev/null is non-waking by construction and nobody captures to it in order
    to read it later.

    -> False only where the destination is PROVEN to reach nobody. Everything
    else, including an object with no `fileno` and any read this cannot make,
    is None: UNKNOWN consumes exactly as it always has, because this module
    refuses only what it can prove."""
    if dest is None:
        return None
    try:
        fd = dest.fileno()
    except Exception:                        # noqa: BLE001 — not a kernel sink
        return None
    for _look in range(3):
        try:
            state, token = sink_probe(os.getpid(), fd=fd)
        except Exception:                    # noqa: BLE001 — unreadable is UNKNOWN
            return None
        if not (follower and state == SINK_ADMISSIBLE and token
                and stat.S_ISFIFO(token[0])):
            break
        # A PIPE IS ONLY AS USABLE AS ITS READER. A reader that holds each
        # line keeps the wake in its buffer until the Monitor deadline kills
        # it, after this consumer already committed the row (task/2920).
        # Proven held is proven unusable. A one-shot consumer skips this.
        #
        # THE VERDICT IS THE LAST READING. The reader walk takes tens of
        # milliseconds and fd can be moved under it. A verdict on the reading
        # taken before the walk spends the next row into whatever replaced
        # the pipe, so the fd is read again after the walk, and a changed fd
        # is judged afresh.
        try:
            if line_holder(os.getpid(), fd=fd):
                return False
            if sink_probe(os.getpid(), fd=fd) == (state, token):
                return None
        except Exception:                    # noqa: BLE001 — unreadable is UNKNOWN
            return None
    if state != SINK_REFUTES:
        return None
    if token and stat.S_ISREG(token[0]) \
            and os.environ.get(FILE_SINK_OK) == "1":
        return None
    return False


#: Pipe readers MEASURED to pass each line on at once whatever their flags, by
#: argv0 basename. EVERY OTHER READER HOLDS until `_passes` proves otherwise.
#: A filter built on stdio buffers its output when that output is a pipe, so
#: the line waits in its buffer until the waiter exits -- and a Monitor ends
#: a waiter by killing the pipeline, which takes the buffer with it.
#: Measured, one line followed by 2.5 s of silence. HELD until the producer
#: exited: cut, `head -n`, `sed`, `sed -n`, gawk, mawk, `gawk -W
#: interactive`, fold, `tail -n +1`, GNU grep, rg, `grep -e --line-buffered`
#: (the flag spelled as a pattern), a python loop, and `stdbuf -oL` in front
#: of uutils cut, head or tail or of mawk. PASSED at once: cat, tee, tr,
#: `sed -u`, `sed -nu`, grep or rg with --line-buffered, `stdbuf -oL` or
#: `-o0` in front of GNU sed, GNU grep or gawk, `python -u` or
#: PYTHONUNBUFFERED, `mawk -W interactive`, and a shell `while read` loop.
#: The harness grep runs as argv0 `ugrep` and held a line even with
#: --line-buffered (task/2920), so it is on no list and always holds.
PASS_THROUGH_READERS = frozenset(("cat", "tee", "tr"))
#: `stdbuf` works through a preload that only C stdio honours.
_STDBUF_HONOURED = frozenset(("sed", "grep", "gawk"))
#: A shell holding the read end is transparent: it forwards what its builtins
#: print at once, or it is waiting on the child that really reads the pipe.
_SHELLS = frozenset(("bash", "sh", "dash", "zsh", "ash", "ksh", "mksh"))
_PYTHON = re.compile(r"\Apython[0-9.]*\Z")
#: Option spellings that TAKE an argument, so a flag word standing in that
#: argument's place is data (`grep -e --line-buffered` is a pattern).
_GREP_ARGS = ("efmABCdD", ("--regexp", "--file", "--max-count",
                           "--after-context", "--before-context", "--context",
                           "--directories", "--devices", "--label", "--include",
                           "--exclude", "--exclude-from", "--exclude-dir",
                           "--group-separator", "--binary-files"))
_RG_ARGS = ("efgtTmABCjMErd", ("--regexp", "--file", "--glob", "--iglob",
                               "--type", "--type-not", "--type-add",
                               "--type-clear", "--max-count", "--after-context",
                               "--before-context", "--context", "--threads",
                               "--max-columns", "--encoding", "--replace",
                               "--max-depth", "--max-filesize", "--sort",
                               "--sortr", "--pre", "--pre-glob", "--colors",
                               "--color", "--context-separator",
                               "--path-separator", "--ignore-file", "--engine"))
_SED_ARGS = ("efl", ("--expression", "--file", "--line-length"))
_PY_ARGS = ("WX", ("--check-hash-based-pycs",))
#: Walk bounds: pipeline stages followed, and ancestors whose children are
#: asked first (a wrapper such as `timeout`, `sh -c` or `{ ...; }` puts the
#: filter one generation above the waiter's own siblings).
_HOPS = 8
_NEAR_ANCESTORS = 4
_READER_MEMO = {}

# THE SLICE RUNNER'S DATA AUDIT (helm/gateslice.py) reports any module
# data a test unit leaves behind; these names are process-wide by design.
_GATESLICE_MUTABLE = {
    "_READER_MEMO": (
        "a TTL memo of process-tree reader lookups keyed by the waiter; a "
        "hit only saves a /proc walk"),
}
_EMPTY_TTL_S = 15.0


def _sets_flag(args, short=None, long=None, takes=("", ()), attached="",
               stops="", operand_stops=False):
    """True when `args` sets `short` or `long` AS AN OPTION.

    A token standing where an option's argument goes is that argument, never
    a flag: the next token after `-e`, `--regexp`, or the rest of a cluster
    after a letter in `takes[0]`. `attached` letters take an optional
    argument only in the same token (sed's -i). A letter in `stops` ends
    option parsing (python's -c and -m), as `--` does and, where
    `operand_stops`, the first operand."""
    arg_short, arg_long = takes
    i = 0
    while i < len(args):
        tok = args[i]
        i += 1
        if tok == "--":
            return False
        if tok.startswith("--"):
            name, eq, _value = tok.partition("=")
            if long and name == long and not eq:
                return True
            if name in arg_long and not eq:
                i += 1
            continue
        if tok.startswith("-") and len(tok) > 1:
            body = tok[1:]
            for j, ch in enumerate(body):
                if short and ch == short:
                    return True
                if ch in stops:
                    return False
                if ch in arg_short:
                    if j == len(body) - 1:
                        i += 1
                    break
                if ch in attached:
                    break
            continue
        if operand_stops:
            return False
    return False


def _proc_exe_name(pid, proc_dir=None):
    exe = proc_exe(pid, proc_dir)
    return os.path.basename(exe) if exe else ""


def _passes(pid, argv, proc_dir=None):
    """True when this reader is MEASURED to pass each line on at once."""
    return _measured(argv, _proc_exe_name(pid, proc_dir),
                     lambda: proc_env(pid, proc_dir) or {})


def _stdbuf_split(args):
    """(stdout mode, wrapped argv) for `stdbuf` arguments."""
    mode, i = None, 0
    while i < len(args):
        tok = args[i]
        i += 1
        if tok == "--":
            break
        if tok.startswith("--output="):
            mode = tok.partition("=")[2]
        elif tok in ("-o", "--output") and i < len(args):
            mode, i = args[i], i + 1
        elif tok.startswith("-o"):
            mode = tok[2:]
        elif tok in ("-i", "-e", "--input", "--error"):
            i += 1
        elif not tok.startswith("-"):
            i -= 1
            break
    return mode, args[i:]


def _measured(argv, exe, env):
    """The measured pass rule over one reader's argv, executable basename and
    environment (`env` is a thunk: only a python or stdbuf reader reads it).

    `stdbuf` is judged by what it runs: it may still be setting up when it
    is read (uutils stdbuf unpacks its preload first), and what passes is
    the wrapped program under that mode, never stdbuf itself."""
    name = os.path.basename(argv[0]) if argv else ""
    args = argv[1:]
    if name == "stdbuf":
        mode, cmd = _stdbuf_split(args)
        if not cmd:
            return False
        import shutil
        real = os.path.basename(os.path.realpath(shutil.which(cmd[0])
                                                 or cmd[0]))
        return (mode in ("L", "0") and real in _STDBUF_HONOURED) \
            or _measured(cmd, real, lambda: {})
    if name in PASS_THROUGH_READERS:
        return True
    if name in ("grep", "egrep", "fgrep", "rg") and _sets_flag(
            args, long="--line-buffered",
            takes=_RG_ARGS if name == "rg" else _GREP_ARGS):
        return True
    if name == "sed" and _sets_flag(args, short="u", long="--unbuffered",
                                    takes=_SED_ARGS, attached="i"):
        return True
    if exe == "mawk":
        return any(a == "-Winteractive" or (a == "-W" and b == "interactive")
                   for a, b in zip(args, args[1:] + [""]))
    if _PYTHON.match(name) or _PYTHON.match(exe):
        return bool(env().get("PYTHONUNBUFFERED")) or _sets_flag(
            args, short="u", takes=_PY_ARGS, stops="cm", operand_stops=True)
    return exe in _STDBUF_HONOURED and env().get("_STDBUF_O") in ("L", "0")


def _holder_name(argv):
    """The name a refusal gives a holder: stdbuf answers for what it runs."""
    name = os.path.basename(argv[0]) if argv else ""
    if name == "stdbuf":
        cmd = _stdbuf_split(argv[1:])[1]
        if cmd:
            return os.path.basename(cmd[0])
    return name or "an unreadable reader"


def _is_shell(pid, argv, proc_dir=None):
    name = os.path.basename(argv[0]) if argv else ""
    return name in _SHELLS or _proc_exe_name(pid, proc_dir) in _SHELLS


def _children(pid, proc_dir=None):
    root = _proc_root(proc_dir)
    out = []
    try:
        tids = os.listdir(os.path.join(root, str(pid), "task"))
    except OSError:
        return out
    for tid in tids:
        raw, err = _read(pid, os.path.join("task", tid, "children"), proc_dir)
        if not err:
            out += [int(x) for x in raw.split() if x.isdigit()]
    return out


def _ancestors(pid, proc_dir=None):
    """[pid, parent, grandparent, ...] up to init."""
    chain = [int(pid)]
    while len(chain) < 64:
        ppid = proc_ppid(chain[-1], proc_dir)
        if not ppid or ppid in chain:
            break
        chain.append(ppid)
    return chain


def _read_end(root, pid, fd):
    """True when `pid`'s `fd` is opened for reading (O_RDONLY or O_RDWR)."""
    try:
        with open(os.path.join(root, str(pid), "fdinfo", fd), "rb") as f:
            for line in f.read(4096).splitlines():
                if line.startswith(b"flags:"):
                    return int(line.split()[1], 8) & 3 != 1
    except (OSError, ValueError, IndexError):
        pass
    return False


def _holding(key, pids, root):
    """The pids among `pids` holding the READ end of pipe `key`."""
    out = []
    for pid in pids:
        d = os.path.join(root, str(pid), "fd")
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            try:
                st = os.stat(os.path.join(d, name))
            except OSError:
                continue
            if stat.S_ISFIFO(st.st_mode) and (st.st_dev, st.st_ino) == key \
                    and _read_end(root, pid, name):
                out.append(pid)
                break
    return out


def _readers(key, chain, proc_dir=None):
    """The processes, outside the waiter's own ancestry, that READ pipe
    `key`, or [] when none does.

    The near ancestors' children are asked first, which is where a Monitor
    pipeline puts its filters; only when none holds the pipe is the process
    table walked. The waiter's ANCESTORS are never readers here: one holding
    the pipe is the harness that launched the waiter and reads it directly,
    not a filter in between."""
    root = _proc_root(proc_dir)
    skip = set(chain)
    near = set()
    for a in chain[1:1 + _NEAR_ANCESTORS]:
        near.update(_children(a, proc_dir))
    found = _holding(key, sorted(near - skip), root) \
        or _holding_anywhere(key, chain, root)
    return _through_wrappers(key, found, skip, root, proc_dir)


def _through_wrappers(key, found, skip, root, proc_dir=None):
    """`found` with every WRAPPER replaced by the child that really reads.

    A holder whose child holds the same read end is waiting on that child:
    `timeout 60 grep --line-buffered x` holds the pipe as timeout and reads
    it as grep, and `| { cut; }` holds it as a shell and reads it as cut. So
    the child is judged, never the wrapper. A shell with no such child is
    the reader itself (a `while read` loop) and stays."""
    found = list(found)
    wrappers = set()
    for _level in range(_HOPS):
        kids = {k: p for p in found if p not in wrappers
                for k in _children(p, proc_dir)
                if k not in skip and k not in found}
        held = _holding(key, sorted(kids), root)
        if not held:
            break
        wrappers.update(kids[k] for k in held)
        found += held
    return [p for p in found if p not in wrappers]


def _holding_anywhere(key, chain, root):
    """The process-table walk, remembered per (waiter, pipe).

    A found reader is re-checked on every use, since the pid may exit. An
    EMPTY answer expires after _EMPTY_TTL_S rather than standing forever: a
    follower asks at every poll, and a pipe its harness reads directly
    would otherwise cost a full walk each time."""
    memo_key = (root, chain[0], key)
    got = _READER_MEMO.get(memo_key)
    now = time.monotonic()
    if got is not None:
        pids, at = got
        if pids:
            still = _holding(key, pids, root)
            if still:
                return still
        elif now - at < _EMPTY_TTL_S:
            return []
    try:
        everyone = [int(p) for p in os.listdir(root) if p.isdigit()]
    except OSError:
        return []
    skip = set(chain)
    found = _holding(key, [p for p in everyone if p not in skip], root)
    if len(_READER_MEMO) > 64:
        _READER_MEMO.clear()
    _READER_MEMO[memo_key] = (found, now)
    return found


def line_holder(pid, fd=1, proc_dir=None):
    """argv0 of a filter between `pid`'s `fd` pipe and whatever finally reads
    it that is not PROVEN to pass each line at once, or None.

    THE READER IS FOUND BY THE PIPE, NOT BY FAMILY. A wrapper that forks
    (`timeout 30 wait ... | cut`, `sh -c 'cd X; wait ...' | cut`,
    `{ cd X; wait ...; } | cut`) puts the filter outside the waiter's
    siblings, so `_readers` asks who holds the pipe's read end. The walk
    follows the pipeline stage by stage: `| cat | cut` holds at cut. None
    means no process outside the waiter's ancestry reads the pipe, which is
    the shape of a harness reading its waiter directly."""
    root = _proc_root(proc_dir)
    try:
        frontier = [os.stat(os.path.join(root, str(pid), "fd", str(int(fd))))]
    except OSError:
        return None
    chain = _ancestors(pid, proc_dir)
    seen = set()
    for _hop in range(_HOPS):
        nxt = []
        for st in frontier:
            key = (st.st_dev, st.st_ino)
            if not stat.S_ISFIFO(st.st_mode) or key in seen:
                continue
            seen.add(key)
            for reader in _readers(key, chain, proc_dir):
                argv = proc_argv(reader, proc_dir) or []
                if not _is_shell(reader, argv, proc_dir) \
                        and not _passes(reader, argv, proc_dir):
                    return _holder_name(argv)
                try:
                    nxt.append(os.stat(os.path.join(root, str(reader),
                                                    "fd", "1")))
                except OSError:
                    continue
        if not nxt:
            return None
        frontier = nxt
    return None


def stdout_line_holder():
    """`line_holder` for whatever fd `sys.stdout` writes to right now, or
    None when it has no fd (an in-process collector holds nothing back)."""
    try:
        fd = sys.stdout.fileno()
    except Exception:                        # noqa: BLE001 — not a kernel sink
        return None
    try:
        return line_holder(os.getpid(), fd=fd)
    except Exception:                        # noqa: BLE001 — unreadable is UNKNOWN
        return None


def sink_probe(pid, proc_dir=None, fd=1):
    """(state, token) for one fd from ONE stat — the only honest way to pair them.

    THE FD IS A PARAMETER BECAUSE "WHERE DOES THIS WRITE GO" IS NOT ALWAYS
    ANSWERED BY FD 1. A caller holding a Python file object knows its
    `fileno()`, and that number is the destination — fd 1 is merely the usual
    answer. Defaulting to 1 keeps every existing caller identical while making
    the question askable about the fd that actually matters.

    THE CLASS AND THE IDENTITY MUST COME FROM THE SAME OBSERVATION. Reading
    them with two stats lets the fd change in between, so the token names an
    object the classification never saw: the caller then refuses to signal
    when the sink is unchanged and agrees to signal when it changed, which is
    the guard inverted. A cross-family read found exactly that split in the
    cure this supersedes, where a comment three lines long claimed these were
    read "in the same breath" over two separate syscalls. One stat, both
    answers, no breath between them.

    The token is OBJECT identity — (S_IFMT, st_dev, st_ino, st_rdev) — and
    deliberately not OFD identity, which /proc does not expose portably. That
    is sufficient for the ONE predicate this guard protects: the class is
    inside the token, so every transition that could make a sink waking
    (REG->FIFO, CHR->SOCK) changes it, while the matches it admits — the same
    regular file, the same /dev/null — are all still non-waking."""
    path = os.path.join(_proc_root(proc_dir), str(pid), "fd", str(int(fd)))
    try:
        st = os.stat(path)
    except OSError:
        return SINK_UNKNOWN, None
    token = (stat.S_IFMT(st.st_mode), st.st_dev, st.st_ino, st.st_rdev)
    if stat.S_ISREG(st.st_mode):
        return SINK_REFUTES, token
    if stat.S_ISCHR(st.st_mode) and st.st_rdev == _NULL_RDEV:
        # /dev/null BY DEVICE NUMBER, not by pathname: a bind-mount or a
        # differently-named node with the same rdev is the same discard, and a
        # file called /dev/null in some chroot is not. Every OTHER character
        # device — a tty, /dev/zero — falls through to ADMISSIBLE, because this
        # function refuses only what it can PROVE reaches no reader.
        return SINK_REFUTES, token
    return SINK_ADMISSIBLE, token


def sink_identity(pid, proc_dir=None):
    """A token identifying the OBJECT on fd 1, or None when unreadable.

    A CLASS IS NOT AN IDENTITY, and that gap is what let a stale reading drive
    a signal. `sink_state` answers "regular file"; it does not answer WHICH
    regular file, so a process that dup2s a live pipe over fd 1 after being
    classified still looks like the thing that was classified — its pid,
    start time and argv are all unchanged. The decision was true when it was
    taken and false when it was acted on.

    (st_dev, st_ino) names the description itself, and st_rdev distinguishes
    character devices that share them. Comparing this token before and after
    is what turns "it was dead a moment ago" into "it is the same dead thing
    now"."""
    return sink_probe(pid, proc_dir)[1]


def waiter_spec(pid, proc_dir=None, argv=None, env=None, row=None):
    """The behavior that makes one followed waiter equivalent to another.

    Seat+session identity is necessary but not sufficient: `--timeout` makes a
    waiter finite, while `--room`, `--any`, and `--ambient` change what can wake
    it. A duplicate may defer only to the same normalized behavior. argv/env
    remain authoritative; a corroborated registry row supplies the launch-time
    room only when no explicit/env room survives and the process cwd can no
    longer reconstruct the value `chat.cmd` captured before entering wait."""
    from . import rearm
    argv = argv if argv is not None else proc_argv(pid, proc_dir)
    sub = rearm._helm_subargv(argv or []) or []
    if sub[:2] != ["chat", "wait"]:
        return None
    env = env if env is not None else proc_env(pid, proc_dir)
    raw_timeout = rearm._flag(sub, "--timeout")
    try:
        timeout = float(raw_timeout) if raw_timeout is not None else None
    except (TypeError, ValueError):
        timeout = "invalid"
    room = rearm._flag(sub, "--room")
    if not room and env:
        room = env.get("HELM_CHAT_ROOM") or env.get("MELD_CHAT_ROOM")
    saved = (row or {}).get("waiter")
    stable = {"follow": "--follow" in sub, "any": "--any" in sub,
              "ambient": "--ambient" in sub, "timeout": timeout}
    if not room and isinstance(saved, dict) \
            and all(saved.get(k) == v for k, v in stable.items()):
        room = saved.get("room")
    unknown_room = False
    if not room:
        try:
            from . import seats
            status, room = seats.derive_home_room_typed(proc_cwd(pid, proc_dir))
            unknown_room = status == seats.DERIVE_UNKNOWN
        except Exception:                       # noqa: BLE001 — unknown != equal
            room, unknown_room = None, True
    return dict(stable, room=None if unknown_room else room or "main")


def requested_waiter_spec(room="main", any_row=False, ambient=False,
                          timeout=None):
    """Canonical behavior value for a CLI waiter before it mutates anything."""
    return {"follow": True, "room": room or "main", "any": bool(any_row),
            "ambient": bool(ambient), "timeout": timeout}


def waiter_seat(pid, proc_dir=None, argv=None, env=None):
    """The seat a waiter is attributable to, or None when nothing names one.

    `--seat` first (the launch seam always writes it), then the process's own
    HELM_CHAT_NAME/MELD_CHAT_NAME. A waiter naming no seat is UNATTRIBUTABLE
    and stays that way — inventing an owner for it is how a reaper takes out
    somebody else's beacon."""
    from . import rearm
    argv = argv if argv is not None else proc_argv(pid, proc_dir)
    if not argv:
        return None
    sub = rearm._helm_subargv(argv) or []
    flag = rearm._flag(sub, "--seat")
    if flag:
        return _alias_canonical(flag)
    env = env if env is not None else proc_env(pid, proc_dir)
    if not env:
        return None
    return _alias_canonical(
        env.get("HELM_CHAT_NAME") or env.get("MELD_CHAT_NAME") or None)


def _alias_canonical(name):
    """A waiter that names a seat's OLD name inside its rename alias window
    is that seat's waiter: the argv and environ of a beacon armed before the
    rename still spell the old name, and attributing them to a row that no
    longer exists is how a live seat's beacon reads as nobody's."""
    if not name:
        return None
    try:
        from .seats_common import live_alias
        key, _until = live_alias(name)
    except Exception:                        # noqa: BLE001 — unreadable
        return name                          # roster: the name as declared
    return key if key is not None else name


def proc_session(pid, proc_dir=None, env=None):
    """The harness session id the waiter was armed under, from its OWN environ.

    Same resolution order as `home.session_id`, read from the TARGET's environ
    rather than ours. Measured on this box 2026-07-30: 23 of 23 live beacons
    carried CLAUDE_CODE_SESSION_ID, so session identity is available for every
    real waiter — including the unregistered ones this registry inherits."""
    env = env if env is not None else proc_env(pid, proc_dir)
    if not env:
        return None
    for key in home._SESSION_ENV:
        if env.get(key):
            return env[key]
    return None


def effective_home(env):
    """The helm home a process would resolve, or None when it cannot be read.

    Mirrors `home.helm_home()` from the TARGET's environ. None is unknown, not
    evidence of a foreign home; each caller must keep its own fail-closed
    direction explicit rather than treating an unreadable process as local or
    foreign by default."""
    if not env:
        return None
    value = env.get("HELM_HOME") or env.get("MELD_HOME")
    if value:
        return os.path.realpath(os.path.abspath(os.path.expanduser(value)))
    base = env.get("HOME")
    if not base:
        return None
    return os.path.realpath(os.path.join(base, ".helm"))


def _our_home():
    return os.path.realpath(home.helm_home())


# ---------------------------------------------------------------------------
# the AGENT — the question a beacon can never answer about itself
# ---------------------------------------------------------------------------

AGENT_COMM = "claude"


def is_agent(argv=None, comm=None):
    """True for an AGENT process (a claude PTY), never for a beacon.

    TWO SPELLINGS, because the two EVIDENCE SOURCES name the same process
    differently: an argv0/argv1 BASENAME of "claude", and a comm of exactly
    "claude". A pane that one can see and the other cannot would arrive here as
    an unattributable holder and quietly hold a seat at UNPROVEN forever, so
    both are accepted.

    That used to read "`hooks.running_panes` matches the BASENAME and
    `sessions.live_sids` proves comm EXACTLY", naming two CALLERS whose
    disagreement forced the union. running_panes no longer matches anything
    itself — it hand-rolled the basename test until that duplicate census was
    folded into this function, so the sentence described a caller that had
    stopped existing. The union is still right; its reason is a property of the
    EVIDENCE, not of who happens to be asking.

    CALLERS MUST NOT READ False AS "NOT AN AGENT" WHEN BOTH ARGUMENTS ARE None.
    `is_agent(None, None)` is False and carries no information: None is what
    proc_argv/proc_comm return when the read FAILED, so a process that refused
    to answer is indistinguishable here from vim. This function cannot tell
    them apart — it never sees the errno — so the caller holding the read
    result must. hooks.running_panes does exactly that: both-None on a live
    process of ours becomes an UNCLASSIFIED pane rather than a silent drop.

    A THIRD SPELLING, because the agent stopped being named after itself. The
    launcher now exec's a VERSION-NAMED binary out of a versions directory —
    argv0 `~/.local/share/claude/versions/2.1.238`, and a comm of `2.1.238`,
    which is the basename the kernel copies. Neither spelling above can see
    that, and the consequence was total rather than partial: measured
    2026-08-22 across every live agent on this box, 16 OF 16 ANSWERED FALSE —
    every helm seat, both families, both installed versions. An instrument that
    answers False for its entire population is not strict, it is blind, and
    this one gates a pane census (`agent_index`), the pane half of
    `hooks.running_panes`, and gate capability in `helm/gate.py`.

    The third spelling matches the INSTALL LAYOUT rather than the name, since
    the name is now a version: an argv0 whose last three path components are
    `claude/versions/<anything>`. Deliberately that specific — a bare `claude`
    ANYWHERE in a path would admit any script filed under a directory of that
    name, and this predicate decides whether a live process is some seat's
    agent.

    A VERSION-SHAPED COMM ON ITS OWN IS NOT AN ANSWER EITHER WAY. `2.1.238`
    identifies nothing without the path that gives it a home, so a caller whose
    argv read FAILED and whose comm is a bare version holds no evidence rather
    than a negative one — the both-None trap in a new dress. This function
    still never sees the errno, so that caller must hold the read result."""
    if comm == AGENT_COMM:
        return True
    return any(_agent_path(a) for a in (argv or [])[:2])


# A RELEASE, not any basename: `claude/versions/2.1.238` is the layout and
# `claude/versions/fake` is a lookalike anybody can mkdir. Requiring the leaf
# to BE a version is what separates them without pinning an install prefix.
_VERSION_LEAF = re.compile(r"\d+(?:\.\d+)*\Z")


def _agent_path(path):
    """Does this executable path name a claude agent? -> bool

    Two layouts, and the second is the one the launcher uses now: a file
    literally named `claude`, or `<...>/claude/versions/<release>`. The second
    is matched on the LAYOUT plus a version-shaped leaf rather than on a bare
    `claude` anywhere in the path, because this decides whether a live process
    is some seat's agent and `/tmp/claude/versions/fake` is two mkdirs."""
    if os.path.basename(path) == AGENT_COMM:
        return True
    parts = os.path.normpath(path).split(os.sep)
    return (len(parts) >= 3 and parts[-3:-1] == [AGENT_COMM, "versions"]
            and bool(_VERSION_LEAF.match(parts[-1])))


def launcher(pid, proc_dir=None, env=None):
    """(agent pid | None, 'live' | 'dead' | None, why) — the AGENT that
    launched this beacon, proved from the BEACON'S OWN environ.

    THE EVIDENCE THE OTHER TWO RUNGS GO LOOKING FOR IS ALREADY HERE, one
    process lower down. `orphaned` asks the beacon's PARENT whether it was
    adopted, and `agent_verdict` asks the AGENT to DECLARE a seat. On this host
    class both questions are put to a process that cannot answer them:

      THE PARENT IS A WRAPPER. A beacon is launched as `bash -c '... helm chat
      wait ...'`, so the bash sits between the agent and the waiter. When the
      agent dies it is the WRAPPER that systemd adopts; the wrapper stays the
      beacon's parent and stays ALIVE, `parent_lost` reads comm=bash, and a
      genuinely dead launcher reports False. Measured 2026-08-22 on seat
      <project>-qwen: beacon 3743772 -> bash 3743771 (alive) -> systemd 6604,
      while agent 3502769 was ABSENT. Verdict `unknown`; `ghost` was true.

      THE AGENT DECLARES NOTHING. A native claude pane carries no
      HELM_CHAT_NAME, so `declared_seats` is empty for every one of them and
      `agent_verdict` answers UNPROVEN for healthy and dead seats alike —
      measured the same day, 15 of 16 seats UNPROVEN including every live one.
      The declaration lives on the BEACON (its `--seat` argv), and the beacon
      also NAMES ITS AGENT: CLAUDE_PID, set by the harness for its own
      children.

    So one read answers both rungs: CLAUDE_PID names the launcher, and the
    launcher's liveness IS the wake path's liveness.

    'dead' IS PROVEN, NEVER INFERRED, and PID REUSE is the whole difficulty —
    a recycled pid is alive and is not the launcher. Three independent proofs,
    any one of which is death:
      * the pid is ABSENT.
      * it STARTED AFTER the beacon it would have launched. A parent cannot
        postdate its own child, so the pid has been recycled.
      * it is READABLE AND IS NOT AN AGENT. The original set CLAUDE_PID for its
        own child, so the original WAS one; anything else wearing the pid is a
        different process. A read that merely FAILS proves nothing and returns
        None here — the both-None trap `is_agent` warns about is answered by
        holding the read results, never by reading its False as "not an agent".

    A SESSION MISMATCH IS DELIBERATELY NOT ONE OF THEM. That is a departure
    from the reviewing seat's criterion (3), taken on this module's own
    standing doctrine rather than against it: a COMPACTION RENAMES A SESSION
    INSIDE THE SAME PROCESS, so "alive but holding a different session" is
    exactly what a healthy seat looks like an hour after it compacted. A
    session match may strengthen a positive claim; it can never make a death
    one, and this file already refuses that inference by name.

    None is the whole rest of the world — no CLAUDE_PID (a hand-armed beacon
    has none), an unreadable /proc, a shape nothing can classify — and None
    keeps the process, because absence of evidence retires nobody."""
    env = proc_env(pid, proc_dir) if env is None else env
    try:
        agent = int(str((env or {}).get("CLAUDE_PID")).strip())
    except (TypeError, ValueError):
        return None, None, ("the beacon names no launcher — no CLAUDE_PID in "
                            "its own environ")
    alive = pid_alive(agent, proc_dir=proc_dir)
    if alive is False:
        return agent, "dead", ("its launcher pid %s is gone, so nothing reads "
                               "the pipe it writes wakes into" % agent)
    if alive is None:
        return agent, None, ("its launcher pid %s could not be read" % agent)
    astart, bstart = (proc_starttime(agent, proc_dir),
                      proc_starttime(pid, proc_dir))
    # A MISSING START-TIME IS NOT A PASSED CHECK. This comparison IS the
    # anti-reuse guard; skipping it when either read fails would let a recycled
    # pid wearing an agent shape reach `live` below and clear the seat to
    # COVERED — the guard degrading silently into its own absence, which is the
    # shape this whole module exists to end. Unknown, and unknown keeps nobody
    # alive on the strength of it.
    if astart is None or bstart is None:
        return agent, None, ("its launcher pid %s cannot be proven the same "
                             "incarnation that armed the beacon — a start-time "
                             "could not be read, and the anti-reuse check is "
                             "the whole basis of a live verdict here" % agent)
    if astart > bstart:
        return agent, "dead", ("pid %s started AFTER the beacon it would have "
                               "launched, so the launcher's pid was recycled "
                               "and the launcher itself is gone" % agent)
    # THE KERNEL'S ANSWER OUTRANKS THE PROCESS'S OWN. argv0 is writable by the
    # process; /proc/<pid>/exe is not, so a pid that wants to pass for an agent
    # can forge the first and not the second. A live verdict — which clears a
    # seat to COVERED and tells the fleet it is reachable — is granted only on
    # the unforgeable evidence.
    exe = proc_exe(agent, proc_dir)
    if exe is not None:
        if _agent_path(exe):
            return agent, "live", ("its launcher is live agent pid %s (%s)"
                                   % (agent, exe))
        return agent, "dead", ("pid %s is executing %s, not an agent, so the "
                               "launcher's pid was recycled and the launcher "
                               "itself is gone" % (agent, exe))
    # NO EXE, so no live verdict — but argv can still prove DEATH, because the
    # forgery direction is asymmetric: a stranger has a motive to LOOK like an
    # agent and none to deny it. A process that answers and says it is vim is
    # taken at its word; one that says it is claude is not.
    argv, comm = proc_argv(agent, proc_dir), proc_comm(agent, proc_dir)
    if argv and not is_agent(argv, comm):
        return agent, "dead", ("pid %s is alive and is not an agent, so the "
                               "launcher's pid was recycled and the launcher "
                               "itself is gone" % agent)
    return agent, None, ("its launcher pid %s is alive but its executable "
                         "could not be read, and argv alone is forgeable, so "
                         "the incarnation is unverifiable" % agent)


def seat_of_config_dir(real, seats_root):
    """The seat name a CLAUDE_CONFIG_DIR realpath declares, else None — the
    config dir's HOLDER basename, for exactly the two shapes the estate mints
    (the write gate's law, configs._classify._is_seat_home):

        <seats_root>/<family>/claude                     -> <family>
        <seats_root>/<family>/instances/<seat>/claude    -> <seat>   (slice 6)

    ONE definition, used by declared_seats and hooks.running_panes both. Its
    predecessor was two inline copies of a two-dirname check — one level deep,
    so an instance pane's self-declaration (task/331: codex-2, codex-3) was
    dropped by both readers at once, invisibly and in agreement."""
    if os.path.basename(real) != "claude":
        return None
    holder = os.path.dirname(real)                 # <family> | <seat>
    up2 = os.path.dirname(holder)                  # seats | instances
    if up2 == seats_root:
        return os.path.basename(holder)
    if (os.path.basename(up2) == "instances"
            and os.path.dirname(os.path.dirname(up2)) == seats_root):
        return os.path.basename(holder)
    return None


def declared_seats(env, seats_root):
    """Every seat name a PANE declares ABOUT ITSELF — never a name inferred for
    it from somewhere else.

    That restriction is the point. The roster can bind a session to a seat, and
    the LIVE rung already rests on exactly that binding; a cross-check made of
    the same evidence agrees with the rung by construction. These two are the
    launch seam's own stamps and nothing else: HELM_CHAT_NAME (launch.py writes
    it) and a CLAUDE_CONFIG_DIR matching a minted seat shape under this home's
    seats dir — family or slice-6 instance, seat_of_config_dir's rule, shared
    with `hooks.running_panes`.

    A `claude -p` one-shot spawned by an agent inherits its parent's
    HELM_CHAT_NAME and so counts as the seat being home for as long as it runs.
    That is the SAFE direction — it can only ever hold a seat at COVERED, never
    push a live one to VACANT."""
    out = [env[k] for k in ("HELM_CHAT_NAME", "MELD_CHAT_NAME") if env.get(k)]
    cdir = env.get("CLAUDE_CONFIG_DIR")
    if cdir:
        seat = seat_of_config_dir(os.path.realpath(cdir), seats_root)
        if seat:
            out.append(seat)
    return out


def agent_index(proc_dir=None):
    """{"by_seat": {seat: [pid…]}, "by_pid": {pid: [seat…]},
    "home_by_pid": {pid: helm_home | None}} over every live agent pane — or
    None when the process table itself could not be listed.

    A pane whose ENVIRON cannot be read is NOT dropped and does NOT poison the
    whole scan: it is recorded as declaring NOTHING, which is what it is. That
    keeps the blast radius exact — an unreadable pane holds back only the seat
    whose beacon it actually answers, instead of flipping every covered seat on
    the box to UNPROVEN because one process exited mid-scan.

    AND THE DETECTOR READ NOW OBEYS THAT SAME LAW, which it did not until
    this change. If cmdline AND comm are BOTH UNREADABLE, `is_agent(None,
    None)` is False — indistinguishable from its answer for vim — and a bare
    `continue` removed the pid before the environ law could apply. A process
    proven GONE is different: the pid exited between enumeration and inspection
    and is excluded, because absence is the one definitive non-pane answer.

    UNREADABLE IS CARRIED, GONE IS EXCLUDED, AND THE DIRECTION IS THE WHOLE
    ARGUMENT. The same both-unreadable case in `hooks.running_panes` is
    EXCLUDED, and copying that cure here would have been wrong. running_panes answers "which panes
    are OURS to manage", so a pid it cannot prove is ours must go — admitting
    a foreign pane taints its census. This index answers, through
    `gate.suite_cap`, "would a big suite STARVE a pane on this box", and
    another user's claude starves exactly as well as ours. suite_cap raises
    the cap ONLY on a census that ran and came back EMPTY, so a process we
    could not classify is CARRIED: it might be a pane, and carrying it keeps
    the cap conservative. Same word, same evidence, opposite correct answer —
    and therefore NO uid filter here either.

    WHAT IT WAS WORTH, measured before it was built rather than after: a pid
    missing from `by_pid` reached `agent_verdict` as `unnamed`, and a
    non-empty `unnamed` BLOCKS the FALSE arm, so the drop pushed toward
    UNPROVEN and never toward a false PROVEN VACANT. It failed in the safe
    direction there. `gate._agent_pane_pids` is the consumer where membership
    is the answer, and the drop could hand a hardened box a RAISED suite cap
    on a census that never saw its panes.

    An EMPTY index cannot silently mean an empty world here, which is the trap
    every other absence check in this file has to guard by hand: a beacon only
    reads LIVE because a live claude process holds its session, so that holder
    is in this index by construction. No live beacon, no verdict to taint."""
    root = _proc_root(proc_dir)
    sroot = os.path.realpath(os.path.join(home.global_dir(), "seats"))
    try:
        names = os.listdir(root)
    except OSError:
        return None                          # unlistable: unknown, not empty
    by_seat, by_pid, home_by_pid = {}, {}, {}
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        argv, argv_err = _proc_argv(pid, proc_dir)
        comm, comm_err = _proc_comm(pid, proc_dir)
        # GONE IS A PROVEN NO, not an unreadable maybe. A process may exit
        # between listdir and either detector read; carrying that pid makes a
        # routine race look like a possible pane and keeps suite capacity low
        # for invented evidence. Either successful disappearance proof wins
        # over an earlier readable file from the same dying process.
        if "gone" in (argv_err, comm_err):
            continue
        if not is_agent(argv, comm):
            # BOTH UNREADABLE READS ARE NOT A NO. is_agent(None, None) is False
            # for the same reason it is False for vim, but the preserved read
            # reasons now distinguish the facts. A process that ANSWERED and is
            # not claude leaves; one that refused both reads is carried.
            #
            # AND THE FAIL-CLOSED DIRECTION HERE IS THE OPPOSITE OF
            # hooks.running_panes': that function asks which panes are OURS to
            # manage, while this index asks whether a suite might starve any
            # pane on the box. NO UID FILTER HERE FOR THE SAME reason.
            if argv_err is None or comm_err is None:
                continue
        raw, err = _read(pid, "environ", proc_dir)
        env = None if err else _env_map(raw)
        declared = [] if env is None else declared_seats(env, sroot)
        by_pid[pid] = declared
        home_by_pid[pid] = effective_home(env)
        for seat in declared:
            by_seat.setdefault(str(seat).casefold(), []).append(pid)
    return {"by_seat": by_seat, "by_pid": by_pid,
            "home_by_pid": home_by_pid}


def _pane_in_scope(agents, pid):
    """A pane with a proven foreign helm home is not evidence about this fleet.
    Unknown stays admissible: absence of a home read is not proof of foreignness."""
    pane_home = (agents.get("home_by_pid") or {}).get(pid)
    return pane_home is None or pane_home == _our_home()


def agent_verdict(seat, live_beacons, agents=_UNPROBED, live=None,
                  proc_dir=None):
    """(True | False | None, why) — is an AGENT home for `seat`?

    TRUE  — a live pane DECLARES this seat. Positive, independent evidence.
    FALSE — PROVEN VACANT: nothing declares this seat, and every process its
            live beacons actually write into is provably SOMEBODY ELSE'S agent.
            The wake path terminates inside another seat's pane; a DM lands in
            a live pipe that wakes nobody.
    NONE  — anything less. Above all the case that decides most of a real box:
            the process behind the beacon declares no seat of its own. A
            hand-launched pane joins under a derived name and stamps nothing,
            so an unattributable pane may well BE this seat's agent — calling
            that vacant would tell a reader to stop addressing a working seat.
    """
    if agents is _UNPROBED:
        agents = agent_index(proc_dir)
    if agents is None:
        return None, ("the process table could not be listed, so no pane "
                      "could be named")
    mine = [pid for pid in agents["by_seat"].get(str(seat).casefold(), [])
            if _pane_in_scope(agents, pid)]
    if mine:
        return True, "pane pid %s declares seat %s" % (mine[0], label(seat))
    if not live_beacons:
        return None, "no live beacon names a process to ask about"
    # ASK THE BEACON WHO LAUNCHED IT before asking the session map to name a
    # pane. This rung needs no `live` map and no declaration, which is what
    # makes it the one that answers on a box of native panes: a live beacon
    # armed for this seat was launched BY this seat's agent, so proving that
    # launcher live proves an agent is home. Placed above the `live is None`
    # exit deliberately — an unprobeable session map used to end the question
    # before this evidence was ever read.
    for row in live_beacons:
        lpid, lstate, _why = launcher(row["pid"], proc_dir)
        if lstate == "live" and _pane_in_scope(agents, lpid):
            return True, ("its live beacon (pid %s) was launched by agent pid "
                          "%s, which is therefore this seat's pane"
                          % (row["pid"], lpid))
    if live is None:
        return None, ("session liveness could not be probed, so the pane "
                      "behind the beacon cannot be named")
    unnamed, others = [], []
    for row in live_beacons:
        pid = live.get(row.get("session"))
        if pid is None:
            unnamed.append(row["pid"])
        elif _pane_in_scope(agents, pid) and agents["by_pid"].get(pid):
            others.append((pid, agents["by_pid"][pid][0]))
        else:
            unnamed.append(pid)
    # VACANT NEEDS POSITIVE EVIDENCE, and asking for it in this order is what
    # keeps the empty-house claim from being made out of a missing one: the
    # FALSE arm requires a named other-seat pane to point at, and every other
    # shape — including one no live beacon can produce — falls through to
    # UNPROVEN rather than off the end of a list.
    if others and not unnamed:
        return False, ("no live pane declares this seat, and the process "
                       "behind its beacon (pid %s) is seat %s's agent"
                       % (others[0][0], label(others[0][1])))
    blind = "pid %s" % unnamed[0] if unnamed else "the process"
    return None, ("the process behind its beacon (%s) declares no seat of its "
                  "own — an unattributable pane may be this seat's own agent"
                  % blind)


# ---------------------------------------------------------------------------
# session liveness — the half that makes a positive claim honest
# ---------------------------------------------------------------------------

def _sid8(sid):
    """A session-id fragment safe to interpolate into a PRINTED reason.

    The sid is read from another process's environ, so it is attacker-supplied
    on a shared box: any process can export CLAUDE_CODE_SESSION_ID. Eight raw
    bytes are more than enough for an ESC sequence, and these reason strings go
    straight to an operator's terminal."""
    return label(sid)[:8]


def live_sessions():
    """{sid: pid} for every session a live process is holding, or None.

    Delegates to `sessions.live_sids()`, which proves liveness from the
    pid-keyed session record (comm == "claude" EXACTLY, plus the procStart
    incarnation key) and from `--resume <sid>` in a live argv — never from a
    transcript's age. None means the probe itself failed, which every caller
    must render as UNKNOWN rather than as an empty world."""
    try:
        from . import sessions
        return dict(sessions.live_sids())
    except Exception:                        # noqa: BLE001 — a census never raises
        return None


def holder_records():
    """{sid: (pid, procStart)} from every credential home's pid-keyed session
    record, or None when the homes cannot be enumerated.

    ONE PASS, because the caller is a fleet census: resolved per beacon this
    re-globbed every credential home once per waiter."""
    try:
        from . import sessions
        homes = sessions.cred_homes()
    except Exception:                        # noqa: BLE001
        return None
    out = {}
    for cred in homes:
        for path in glob.glob(os.path.join(cred, "sessions", "*.json")):
            try:
                with pk.open_regular(path) as f:
                    rec = json.load(f)
            except (OSError, ValueError):
                continue
            if not isinstance(rec, dict) or not rec.get("sessionId"):
                continue
            try:
                pid = int(rec.get("pid") or 0)
            except (TypeError, ValueError):
                continue
            if pid:
                out[rec["sessionId"]] = (pid, rec.get("procStart"))
    return out


def session_homes():
    """{sid: credential home} — WHERE each session's transcript lives, or None
    when the homes cannot be enumerated.

    ONE PASS, for the same reason `holder_records` takes one: resolved per
    beacon this re-globbed every credential home once per waiter. A registry
    row knows the session and the arm time and has never known which home
    holds that session's files, so this is the join that supplies it."""
    try:
        from . import sessions
        homes = sessions.cred_homes()
    except Exception:                        # noqa: BLE001
        return None
    out = {}
    for cred in homes:
        for path in glob.glob(os.path.join(cred, "sessions", "*.json")):
            try:
                # A FIFO where a session record belongs would hang this walk
                # for every credential home; pk.open_regular refuses it as a
                # NotRegularFile, which is BOTH an OSError and a ValueError
                # and so is already answered by the clause below. This lane
                # branched before that cure and reintroduced the bare open
                # it closed (task/2667).
                with pk.open_regular(path) as f:
                    rec = json.load(f)
            except (OSError, ValueError):
                continue
            if isinstance(rec, dict) and rec.get("sessionId"):
                out.setdefault(rec["sessionId"], cred)
    return out


def row_origin(row):
    """(owner, why) for ONE registry row, from its PRODUCER STAMP ALONE.

    THIS IS THE ALARM'S AUTHORITY, AND IT REPLACES A TRANSCRIPT JOIN. The
    previous answer came from reading the Monitor tool_use that armed the
    beacon out of a transcript and deciding whether its command really ran a
    waiter — which meant modelling bash's grammar from the outside. Ten rounds
    of that produced ten correct cures and ten next cases, the last one six
    false-PROVEN classes at once, because the only evidence available was TEXT
    THE SUBAGENT CONTROLS.

    A STAMP IS NOT TEXT THE SUBAGENT CONTROLS. It is minted by the producer,
    consumed once, and written causally into this exact registration row. So
    the question stops being "does this command string run a waiter" and
    becomes "what does this row say", which has no grammar to get wrong.

    TWO KINDS OF UNKNOWN, AND THEY ARE DIFFERENT FACTS. A row carrying
    `origin: unknown` was ASKED and could not be established, and its
    `origin_reason` says which refusal. A row carrying NO origin field at all
    was written before this existed and nobody ever asked. Both answer None,
    because neither may become a verdict about a seat -- but a census that
    reported them alike would say "this fleet has unattributable beacons"
    about a fleet that simply predates the question."""
    origin = (row or {}).get("origin")
    if origin in (OWNER_MAIN, OWNER_SUBAGENT):
        return origin, ("its producer stamped this registration as %s"
                        % origin)
    if origin == beacon_origin.ORIGIN_UNKNOWN:
        return None, ("no usable producer stamp reached this registration "
                      "(%s), so its origin is UNKNOWN and is never presumed "
                      "main" % ((row or {}).get("origin_reason") or "absent"))
    return None, ("this beacon registered before origin was recorded, so "
                  "nothing about its producer was ever asked")


def origin_unstamped(row):
    """Is this row's origin unknown ONLY because nothing ever stamped it?

    NO STAMP PRODUCER EXISTS YET (`beacon_origin.mint` has no caller), so on
    this fleet every registration reads either `origin: unknown` with the
    reason "absent" or carries no origin field at all. Neither is a refusal:
    nothing was offered, so nothing was refused. A row with any OTHER reason
    was offered a stamp that failed (stale, crossed, malformed), and that is
    still unattributed in the full sense. The census counts the two apart so
    its line can say "unstamped" when that is the whole of the truth, instead
    of "unattributed", which read as a failure to join beacons to seats."""
    row = row or {}
    origin = row.get("origin")
    if origin in (OWNER_MAIN, OWNER_SUBAGENT):
        return False
    if origin == beacon_origin.ORIGIN_UNKNOWN:
        return (row.get("origin_reason") or "absent") == "absent"
    return True


def holder_from_records(sid, records=None):
    """(pid, procStart) that a credential home records as holding `sid`, else
    None. This is what lets a waiter that never registered still be judged:
    claude writes <credhome>/sessions/<pid>.json naming the session, so a
    session whose recorded holder is gone is PROVABLY dead even though no helm
    row was ever written for it."""
    if not sid:
        return None
    if records is None:
        records = holder_records()
    return (records or {}).get(sid)


def session_state(sid, holder=None, live=None, proc_dir=None, records=None):
    """('live'|'dead'|'unknown', why) for one harness session.

    LIVE    — a live process holds it (`live_sessions`).
    DEAD    — the process that HELD it is proven gone: absent, or the pid is
              now a different incarnation. `holder` is the (pid, starttime)
              pair the registry froze at arm time; absent that, the credential
              home's own session record answers for unregistered waiters.
    UNKNOWN — everything else, and it is a real answer, not a shrug. It
              includes the case that matters most: the holder process is ALIVE
              but no longer holds this session. A compaction renames a session
              INSIDE the same process, so that is exactly what an armed beacon
              on a healthy seat looks like an hour after it compacted. Calling
              it dead would kill live seats' beacons; calling it live would
              re-tell the lie this module exists to end.
    """
    if not sid:
        return UNKNOWN, "the waiter declares no harness session id"
    if live is None:
        live = live_sessions()
    if live is None:
        return UNKNOWN, "session liveness could not be probed"
    if sid in live:
        return "live", "held by live pid %s" % live[sid]
    holder = holder or holder_from_records(sid, records)
    if not holder:
        return UNKNOWN, ("no live process holds session %s… and no holder was "
                         "ever recorded for it" % _sid8(sid))
    hpid, hstart = holder[0], holder[1]
    alive = pid_alive(hpid, hstart, proc_dir)
    if alive is False:
        return "dead", ("the process that held session %s… (pid %s) is gone"
                        % (_sid8(sid), hpid))
    if alive is None:
        return UNKNOWN, "the recorded holder pid %s could not be read" % hpid
    return UNKNOWN, ("holder pid %s is alive but no longer holds session %s… — "
                     "a compaction renames a session in place, so this is not "
                     "evidence of death" % (hpid, _sid8(sid)))


# ---------------------------------------------------------------------------
# the registry — one small file per BEACON, keyed on (seat, pid)
# ---------------------------------------------------------------------------

def registry_dir():
    return os.path.join(home.global_dir(), ".state", REG_SUBDIR)


def _entry_path(seat, pid):
    return os.path.join(registry_dir(), "%s.%d.json" % (_seat_key(seat), int(pid)))


def entries(seat=None):
    """Every registry row, newest arm first. Rows are other-process-supplied
    data: a row is a CLAIM about a pid, never permission to act on it — every
    consumer re-proves shape and start-time against /proc before doing
    anything at all.

    The scan intentionally remains all-row rather than a canonical-name glob:
    pre-canonical installs may still hold `Kimi.<pid>.json`. Filtering the row's
    casefold key keeps those live rows discoverable until register/release
    migrates them, instead of making an upgrade temporarily blind to its own
    incumbent."""
    out = []
    for path in sorted(glob.glob(os.path.join(registry_dir(), "*.json"))):
        row = pk.read_json(path, None)
        if not isinstance(row, dict):
            continue
        try:
            row["pid"] = int(row.get("pid"))
        except (TypeError, ValueError):
            continue
        if seat and _seat_key(row.get("seat", "")) != _seat_key(seat):
            continue
        row["_path"] = path
        out.append(row)
    out.sort(key=lambda r: r.get("armed") or 0, reverse=True)
    return out


def _rows_by_pid(seat):
    """One authoritative registry row per process incarnation.

    `entries` is newest-first and deliberately exposes legacy+canonical files
    during migration. A plain `{pid: row for row in entries(...)}` reverses that
    ordering by overwriting the newest row with the oldest duplicate. Prefer a
    canonical-path row when one exists, otherwise retain the first/newest legacy
    row. This one chooser feeds incumbent election, signalling, and census so a
    stale migration artifact cannot shadow the live row in any of the three."""
    out = {}
    for row in entries(seat):
        pid = row["pid"]
        prior = out.get(pid)
        canonical = row.get("_path") == _entry_path(row.get("seat"), pid)
        prior_canonical = prior and prior.get("_path") == _entry_path(
            prior.get("seat"), pid)
        if prior is None or canonical and not prior_canonical:
            out[pid] = row
    return out


def register(seat, session=None, pid=None, proc_dir=None, now=None,
             phase=None, waiter=None, origin=None):
    """Write this beacon's row: (seat, session, pid, arm-time) + the two keys
    that make it checkable later — the beacon's own start-time (anti-pid-reuse)
    and the session HOLDER's (pid, start-time), frozen while the session is
    still alive to be asked. Freezing the holder is the whole point of the
    registry: it survives the session that armed it, so a waiter orphaned by a
    compaction or a crash can still be judged when nobody is left to ask.

    `phase="arming"` is the short-lived contender marker used by `arm` before
    its per-seat election lock. It is never readiness: peers exclude it from
    incumbent selection and signalling until its owner either commits the row
    as active or withdraws it. `waiter` freezes the CLI's normalized behavior;
    its room is the fallback after a derived cwd disappears, while argv/env
    remain the live authority for every stable option.

    `origin` IS ALREADY-CONSUMED FIELDS AND IS NEVER READ HERE, which is the
    whole reason it is a parameter. The producer stamp is SINGLE-USE, and
    `arm` calls this function up to six times for one beacon — the arming
    marker plus whichever branch commits — so a read inside this function
    would spend the stamp on the marker and leave the committed row saying
    UNKNOWN. The caller reads it once and hands the same answer to every
    write. See helm/beacon_origin.py."""
    if not valid_seat(seat):
        return None
    pid = int(pid or os.getpid())
    session = session or home.session_id()
    live = live_sessions() or {}
    hpid = live.get(session) if session else None
    row = {"seat": str(seat), "session": session, "pid": pid,
           "starttime": proc_starttime(pid, proc_dir),
           "armed": float(now if now is not None else time.time()),
           "holder_pid": hpid,
           "holder_start": proc_starttime(hpid, proc_dir) if hpid else None,
           "home": _our_home()}
    if phase:
        row["phase"] = phase
    if waiter is not None:
        row["waiter"] = waiter
    if origin:
        row.update(origin)
    path = _entry_path(seat, pid)
    try:
        os.makedirs(registry_dir(), exist_ok=True)
        # Retire a pre-canonical case-variant path for this same incarnation.
        # Leaving both would make one beacon appear twice until the census
        # eventually pruned the legacy spelling.
        for old in entries(seat):
            if old["pid"] == pid and old.get("_path") != path:
                try:
                    os.unlink(old["_path"])
                except OSError:
                    pass
        pk.write_json(path, row)
    except OSError:
        return None                          # a registry miss never stops a beacon
    return row


def release(seat, pid=None):
    """Drop this beacon's row at exit. Best-effort by design: a row whose
    process is gone is pruned by the next census anyway, so a crashed beacon
    leaves a stale row and not a wrong answer. Removes legacy case-variant paths
    as well as the canonical path so an upgrade does not strand a duplicate."""
    if not valid_seat(seat):
        return False
    pid = int(pid or os.getpid())
    paths = {_entry_path(seat, pid)}
    paths.update(r["_path"] for r in entries(seat) if r["pid"] == pid)
    removed = False
    for path in paths:
        try:
            os.unlink(path)
            removed = True
        except OSError:
            pass
    return removed


def prune(row):
    try:
        os.unlink(row.get("_path") or _entry_path(row["seat"], row["pid"]))
        return True
    except (OSError, KeyError):
        return False


# ---------------------------------------------------------------------------
# attribution — the gate every signal passes through
# ---------------------------------------------------------------------------

def attributable(pid, seat, proc_dir=None, row=None):
    """(True, why) only when `pid` is PROVEN to be `seat`'s beacon in THIS helm
    home. Every other outcome is (False, why) and means KEEP.

    Three gates, all required. Shape first (an argv that is not a waiter is
    never a target no matter what any registry row claims, which is what stops
    a stale row from ever naming an agent pane); then attribution to this exact
    seat; then the helm home, which is what keeps this fleet's reaper away from
    another project's fleet on the same box."""
    argv = proc_argv(pid, proc_dir)
    if not argv:
        return False, "cmdline unreadable — a process helm cannot see is never a target"
    if not is_waiter(pid, proc_dir, argv=argv):
        return False, "not a `helm chat wait` process"
    env = proc_env(pid, proc_dir)
    if env is None:
        return False, "environ unreadable — home and seat cannot be proven"
    if effective_home(env) != _our_home():
        return False, "belongs to a different helm home"
    if row is not None:
        start = row.get("starttime")
        if start is not None and proc_starttime(pid, proc_dir) != start:
            return False, "registry row names a different incarnation of this pid"
        if _seat_key(row.get("seat", "")) == _seat_key(seat):
            return True, "registered beacon of seat %s" % seat
    named = waiter_seat(pid, proc_dir, argv=argv, env=env)
    if not named:
        return False, "waiter names no seat — unattributable"
    if _seat_key(named) != _seat_key(seat):
        return False, "waiter belongs to seat %s" % named
    return True, "waiter declares seat %s" % seat


def _scan(seat, proc_dir=None):
    """Every pid whose argv is a waiter shape naming `seat`. Scoped by SEAT, so
    another fleet's beacons are not even enumerated here."""
    root = _proc_root(proc_dir)
    want, out = _seat_key(seat), []
    try:
        names = os.listdir(root)
    except OSError:
        return None                          # unlistable: unknown, not empty
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        argv = proc_argv(pid, proc_dir)
        if not argv or not is_waiter(pid, proc_dir, argv=argv):
            continue
        named = waiter_seat(pid, proc_dir, argv=argv)
        if named and _seat_key(named) == want:
            out.append(pid)
    return sorted(out)


def _arm_lock(seat):
    """Acquire the stable per-seat election lock, returning its open file.

    The caller closes it to release. A sibling lock file is mandatory because
    registry rows are atomic-replaced; flocking a replaced data inode would let
    two launchers each believe they held the same critical section."""
    import fcntl
    os.makedirs(registry_dir(), exist_ok=True)
    f = open(os.path.join(registry_dir(), "%s.arm.lock" % _seat_key(seat)), "a+")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
    except Exception:
        f.close()
        raise
    return f


# ---------------------------------------------------------------------------
# the actuator — ordinary arm is idempotent; explicit replace is STOP-then-START
# ---------------------------------------------------------------------------


def _one_live_incumbent(seat, session, keep_pid=None, proc_dir=None,
                        waiter=None):
    """The one committed LIVE waiter already serving (seat, session), else None.

    "One" is proved across registry AND /proc. Short-lived `arming` contenders
    are excluded: they have not entered `wait()` and can never be the reason a
    peer exits. A waiter visible before writing that marker is eligible only if
    its `(starttime, pid)` is strictly older than this launcher; every racer
    therefore elects the same oldest unregistered survivor instead of mutually
    deferring. An unlistable process table, UNKNOWN/GHOST waiter, different
    session, or duplicate LIVE waiters falls through to replacement. A same-
    session waiter with different behavior is returned as non-equivalent so the
    CLI can require explicit `--replace` without silently discarding either
    scope.

    The final start-time recheck pins every read to one process incarnation. A
    pid that changed shape or identity while this probe ran cannot become the
    reason the successor exits and leaves the seat with no wake path."""
    if not session:
        return None
    scanned = _scan(seat, proc_dir)
    if scanned is None:
        return None
    # DISCOVERY IS DEFERRED UNTIL A CANDIDATE NEEDS CLASSIFYING, and the reason
    # is ISOLATION rather than speed. `live_sessions` reaches `cred_homes`,
    # which expands `~` and opens session records under the OPERATOR'S REAL
    # credential homes — `~/.claude`, `~/.claude-homes`, `~/.helm/_global/
    # seats/*` — and it is consumed by exactly one line, the `classify` call in
    # the loop below. Hoisted above the loop it ran for every caller including
    # those with no candidate at all, so any fixture reaching this function
    # gained a transitive read of real credential homes whatever HOME it had
    # planted. A cross-family read found it; it is the same class already
    # cured one lane earlier at a different door.
    #
    # THE ANSWER IS UNCHANGED IN EVERY BRANCH, which is what makes this an
    # isolation fix and not a behaviour change: with no candidate the function
    # returned None before (`owned` is empty) and returns None now; with a
    # candidate it fetches, and an UNREADABLE registry still returns None
    # exactly where the hoisted early exit did.
    live = _UNREAD
    scanned = set(scanned)
    self_pid = int(keep_pid or os.getpid())
    self_start = proc_starttime(self_pid, proc_dir)
    keep = {self_pid, os.getpid()}
    rows = _rows_by_pid(seat)
    owned = []
    for pid in sorted(set(rows) | scanned):
        if pid in keep:
            continue
        row = rows.get(pid)
        ok, _why = attributable(pid, seat, proc_dir, row=row)
        if not ok and row is not None and pid in scanned \
                and row.get("starttime") is not None \
                and pid_alive(pid, row["starttime"], proc_dir) is False:
            prune(row)
            row = None
            ok, _why = attributable(pid, seat, proc_dir)
        if not ok:
            continue
        if row is not None and row.get("phase") == "arming":
            continue
        start = proc_starttime(pid, proc_dir)
        if start is None:
            return None
        if row is None:
            if self_start is None:
                raise RuntimeError(
                    "launcher start time unreadable; pre-marker order unprovable")
            if (start, pid) >= (self_start, self_pid):
                continue                        # newer pre-marker contender
        if live is _UNREAD:
            live = live_sessions()
            if live is None:
                return None
        state = classify(pid, seat, row, live, proc_dir)
        # READ INSIDE THE PIN, not by the caller afterwards. The start-time
        # recheck on the next line is what binds every read above it to ONE
        # incarnation of this pid; a sink read from `seats_join` after this
        # function returns would sit outside that pin and could describe a
        # recycled pid's fd 1.
        # THE TOKEN TRAVELS WITH THE CLASS because they come out of ONE stat,
        # not because two calls sit next to each other. The action that
        # consumes this reading happens later and must be able to ask whether
        # it is still the same object; a token from a second syscall could
        # already describe a different one.
        sink, sink_id = sink_probe(pid, proc_dir)
        if not is_waiter(pid, proc_dir) or proc_starttime(pid, proc_dir) != start:
            return None
        owned.append((state, row, start, waiter_spec(pid, proc_dir, row=row),
                      sink, sink_id))
    if len(owned) != 1:
        return None
    state, row, start, incumbent, sink, sink_id = owned[0]
    if state.get("state") != LIVE or state.get("session") != session:
        return None
    return {"pid": state["pid"], "seat": seat, "session": session,
            "starttime": start, "registered": row is not None,
            "why": state.get("why"), "waiter": incumbent, "sink": sink,
            "sink_id": sink_id,
            "equivalent": incumbent == waiter}

def _process_handle(pid):
    """(handle, refusal) — a kernel handle for this pid, or why there is none.

    TWO DIFFERENT NOTHINGS, AND COLLAPSING THEM IS WHAT MADE THE OLD FALLBACK
    DANGEROUS. `AttributeError` or ENOSYS says THIS PLATFORM has no pidfd: the
    caller may fall back to a bare-pid send, which is the pre-pidfd behaviour
    and no worse than it, so the refusal is None.

    ANY OTHER OSError IS ABOUT THIS PID. ESRCH is the kernel saying the process
    is already gone — the single most important thing it can tell us here — and
    the version of this function this cures answered it by returning
    None, which sent the caller down the bare-`os.kill` path and signalled the
    NUMBER. That is precisely the recycle the handle exists to prevent, reached
    by the evidence that the recycle is happening. A per-pid failure now returns
    a REFUSAL and the caller withholds."""
    opener = getattr(os, "pidfd_open", None)
    if opener is None:
        return None, None
    try:
        return opener(pid), None
    except OSError as exc:
        if exc.errno == errno.ENOSYS:
            return None, None
        return None, ("no kernel handle for this pid (%s) — withheld rather "
                      "than signalling the number" % exc)


def _signal_handle(handle, pid, sig):
    """Signal through the handle when there is one, else by number.

    THE FALLBACK IS NAMED RATHER THAN HIDDEN: without a pidfd this is exactly
    the old racy send, and pretending otherwise would be worse than not having
    the handle at all."""
    if handle is None:
        os.kill(pid, sig)
        return False
    signal.pidfd_send_signal(handle, sig)
    return True


def stop_superseded(seat, keep_pid=None, proc_dir=None,
                    sig=signal.SIGTERM, expect_sinks=None):
    """SIGTERM the beacons this seat has superseded. Returns
    {"stopped": [...], "kept": [(pid, why)], "pruned": [...]}.

    THIS IS THE WHOLE CURE FOR THE ACCUMULATION: rotation used to be START, so
    every rotation left its predecessor running and the count only climbed.
    Explicit replacement is STOP-then-START now, and a seat only ever stops ITS
    OWN. Ordinary arm calls reach this only when the live state is not exactly
    one same-seat same-session incumbent.

    The anti-reuse bracket is re-asserted AT the kill (rearm._still_waiter's
    discipline): shape and start-time are re-read immediately before os.kill,
    so a waiter that exited into a recycled pid between the scan and the signal
    is skipped rather than hit. Nothing here is authorized by a registry row
    alone — the row selects a candidate, /proc proves it."""
    keep = {int(keep_pid or os.getpid()), os.getpid()}
    report = {"stopped": [], "kept": [], "pruned": []}
    rows = _rows_by_pid(seat)
    scanned = _scan(seat, proc_dir)
    if scanned is None:
        report["kept"].append((None, "process table unlistable — nothing signaled"))
        scanned = set()
    else:
        scanned = set(scanned)
    for pid in sorted(set(rows) | scanned):
        if pid in keep:
            continue
        row = rows.get(pid)
        # THE INCARNATION IS CAPTURED BEFORE ANYTHING IS DECIDED ABOUT IT.
        # Reading it after `attributable` leaves the attribution itself inside
        # the unguarded window: the scan proves things about W1, W1 exits, the
        # pid is reused by W2, and a start time read AFTER that agrees with
        # every later recheck — so the bracket certifies W2 using W1's
        # evidence. Read first and every subsequent recheck spans the whole
        # decision, including the attribution that authorized it.
        start = proc_starttime(pid, proc_dir)
        ok, why = attributable(pid, seat, proc_dir, row=row)
        if not ok and row is not None \
                and pid_alive(pid, row.get("starttime"), proc_dir) is False:
            prune(row)                       # the row outlived its incarnation
            report["pruned"].append(pid)
            row = None
            if pid not in scanned:
                continue
            ok, why = attributable(pid, seat, proc_dir)  # independently found heir
        if not ok:
            report["kept"].append((pid, why))
            continue
        if row is not None and row.get("phase") == "arming":
            report["kept"].append((pid, "concurrent arm contender not yet ready"))
            continue
        if start is None:
            report["kept"].append((pid, "start time unreadable — signal withheld"))
            continue
        # THE BRACKET IS ANCHORED TO THE ORIGINAL INCARNATION, NOT TO ITSELF.
        # The cure this supersedes read `start` here and then compared the
        # recheck against THAT — two readings of whoever holds the number now,
        # which agree with each other no matter how many times the pid was
        # recycled before the first one. The registry row carries the start
        # time recorded when the beacon registered, and that is the incarnation
        # the decision to signal was made about. The destructive case is not
        # exotic: a waiter exits, the seat relaunches, the new legitimate
        # beacon lands on the same pid and is attributable to the same seat —
        # so every other check passes and the self-referential bracket kills
        # the replacement it was written to protect.
        recorded = row.get("starttime") if row is not None else None
        if row is not None and recorded is None:
            # A ROW WITH NO INCARNATION PROVES NOTHING AND MUST NOT PASS AS
            # AGREEMENT. Treating a null start time as "nothing to contradict"
            # lets the one artifact that could name the original incarnation
            # authorize a signal precisely when it has forgotten which
            # incarnation it meant. An absent reading is UNKNOWN, and UNKNOWN
            # withholds a destructive act.
            report["kept"].append(
                (pid, "the registry row records no start time, so the "
                      "incarnation it refers to cannot be established — "
                      "signal withheld"))
            continue
        if recorded is not None and int(recorded) != int(start):
            report["kept"].append(
                (pid, "the row's incarnation (start %s) is not the one wearing "
                      "this pid now (start %s) — signal withheld"
                 % (recorded, start)))
            continue
        # THE HANDLE IS OPENED BEFORE THE LAST CHECKS, NOT AFTER, and the
        # order is the whole point. A pidfd names THIS process; a bare pid
        # names whoever holds the number when the signal lands. Opening first
        # means a recycle that happens after this line can only make the send
        # fail with ESRCH — it can never redirect it onto a stranger. Opening
        # afterwards would leave exactly the window the rechecks are for.
        handle, refusal = _process_handle(pid)
        if refusal is not None:
            report["kept"].append((pid, refusal))
            continue
        try:
            if not is_waiter(pid, proc_dir) \
                    or proc_starttime(pid, proc_dir) != start:
                report["kept"].append((pid, "identity changed under the scan"))
                continue
            if (expect_sinks or {}).get(pid) is not None:
                # A SINK READING NO LONGER AUTHORIZES A KILL, AND REMOVING THE
                # FREEZE IS THE POINT RATHER THAN A RETREAT.
                #
                # The window is real: between any final stat of fd 1 and the
                # signal, a RUNNING target can dup2 a live pipe over that fd
                # and become waking. Rechecking cannot close it — every recheck
                # leaves the same gap behind it — and STOPPING the target to
                # close it needs something the kernel does not offer us: proof
                # that THIS pass caused the stop it observes. Without that, a
                # debugger or job-control holder can stop the pid between our
                # read and our SIGSTOP, our send succeeds against an already
                # stopped target, and the SIGCONT that follows resumes a stop
                # we did not establish. A pidfd pins the INCARNATION; it
                # confers no stop OWNERSHIP.
                #
                # THREE ROUNDS OF HARDENING ONE MECHANISM IS EVIDENCE ABOUT THE
                # DESIGN, not about the care taken. So the mechanism goes: this
                # pass never stops another process, and a candidate whose only
                # justification is a sink reading is REPORTED instead of
                # signalled. A deliberate rotation (`--replace`, which carries
                # its own authority) still retires it.
                #
                # WHAT MAKES THAT AFFORDABLE IS THE DELIVERY FENCE, not
                # optimism: a non-waking consumer can no longer take an
                # addressed row out of the ledger and discard it, so a surplus
                # one is now merely surplus. The harm that made this kill worth
                # its risk was removed at the door where it happened.
                report["kept"].append(
                    (pid, "its sink was classified non-waking, and a sink "
                          "reading alone no longer authorizes a signal — this "
                          "pass cannot prove it owns a stop, so it stops "
                          "nobody. Retire it deliberately with --replace"))
                continue
            try:
                _signal_handle(handle, pid, sig)
                report["stopped"].append(pid)
                if row is not None:
                    prune(row)
            except OSError as exc:
                report["kept"].append((pid, "signal failed (%s)" % exc))
        finally:
            if handle is not None:
                os.close(handle)
    return report


def _pids(entries):
    """The pids of `(pid, starttime, from_row)` entries, in order, DE-DUPED.

    A number is rendered once per bucket however many incarnations of it that
    bucket holds: two dead incarnations of pid 4242 are two facts, but "4242,
    4242" reads as a display bug rather than as a distinction, and the buckets
    around this already say which kind each one is."""
    out = []
    for pid in (e[0] for e in entries):
        if pid not in out:
            out.append(pid)
    return ", ".join(str(p) for p in out)


def _alongside_buckets(seat, pid, rows, proc_dir=None):
    """(live, dead, unknown, stale_rows) for every OTHER waiter of this seat.

    EXTRACTED SO THE PROPERTY CAN BE TESTED AT ALL. Inline inside `arm` this
    was reachable only through a door that PRUNES a stale row before the
    advice ever runs, so a fixture built to exercise the pid collision could
    not reach it and a mutation of the keying was measurably INERT — the arm
    agreed with itself either way.

    THE REGISTRY IS NOT THE POPULATION. A waiter that never registered, or
    whose row was pruned out from under it, is invisible to `_rows_by_pid` and
    entirely visible to the process table, and it is the one this advice most
    owes the reader: a LIVE unregistered sibling is exactly what "arming
    alongside" is about.

    AND THE UNION IS KEYED BY INCARNATION, NEVER BY NUMBER. A stale row for
    pid P and a LIVE waiter now wearing pid P are two different processes;
    keyed on the number alone, `pid_alive` is asked about the live one using
    the dead one's start time, answers False, and renders a running waiter
    PROVEN GONE. The tell is that deleting the stale row flips the same
    process back to LIVE — a row that changes its answer about a process it
    does not describe is deciding the liveness of a stranger.

    STALE ROWS ARE ROWS: a scanned incarnation that died is PROVEN GONE and
    belongs in the advice, but it never had a registry entry, so naming it a
    stale row would invent an artifact for the reader to clean up."""
    live, dead, unknown, stale_rows = [], [], [], []
    try:
        scanned = _scan(seat, proc_dir) or ()
    except Exception:                        # noqa: BLE001
        scanned = ()
    seen, cand = set(), []
    for other in sorted(rows or {}):
        if other == pid:
            continue
        start = (rows.get(other) or {}).get("starttime")
        cand.append((other, start, True))
        seen.add((other, start))
    for other in sorted(scanned):
        if other == pid:
            continue
        start = proc_starttime(other, proc_dir)
        if (other, start) in seen:
            continue
        cand.append((other, start, False))
    # THE ENTRY TRAVELS, NOT THE NUMBER. This loop knows three things about
    # each candidate -- its pid, its INCARNATION and whether a registry row is
    # where it came from -- and returning bare pids threw two of them away one
    # step before the only reader that needed them. The renderer then had to
    # reconstruct the source by asking whether the pid appears in `rows`,
    # which is the same pid-keyed question this function was rewritten to stop
    # asking: a stale REGISTERED incarnation and an exited SCANNED successor
    # wearing the same number both answer "yes", so both were announced as
    # stale registry rows and the reader was sent to prune an artifact that
    # never existed. Distinguishing incarnations here and collapsing them at
    # the surface is not distinguishing them at all.
    for other, start, from_row in cand:
        alive = pid_alive(other, start, proc_dir)
        (live if alive is True else dead if alive is False
         else unknown).append((other, start, from_row))
        if alive is False and from_row:
            stale_rows.append(other)
    return live, dead, unknown, sorted(set(stale_rows))


def arm(seat, session=None, pid=None, proc_dir=None, now=None, reap=True,
        replace=False, waiter=None):
    """Ensure this seat has one beacon under one serialized election.

    Before taking the per-seat lock, a newcomer writes an `arming` marker. That
    makes concurrently launched peers visible but not READY: the lock holder
    excludes them from incumbent selection and signalling, commits itself, then
    the next contender either defers to that committed row or deliberately
    replaces it. Two first arms therefore cannot mutually defer to each other.

    An ordinary arm returns structured `already_live` only for one attributable
    LIVE waiter serving the exact same session AND normalized behavior. A same-
    session behavior mismatch returns structured `conflict` and requires
    `replace=True`; uncertain, ghost, different-session, or duplicate committed
    states retain the existing replacement pass. Never raises: if the readiness
    marker, election lock, or incumbent proof fails, signalling is skipped and
    the newcomer runs — duplicate wake paths are safer than zero.

    `reap` IS THE AUTHORITY ARGUMENT, not a tuning knob. Stopping an incumbent
    is an act of REPLACEMENT whether it is asked for with `replace=True` or
    taken on the default path, where an unprovable holder falls past the
    idempotence rung into the stop pass. A caller that cannot prove it IS this
    seat (`actors.replacement_authority`) passes `reap=False`, and then this
    function stops nothing at all: it still probes and reports the incumbent,
    and where the census cannot prove one it arms ALONGSIDE and says so in
    `alongside` instead of clearing the seat."""
    pid = int(pid or os.getpid())
    session = session or home.session_id()
    waiter = waiter if waiter is not None else waiter_spec(pid, proc_dir)
    # THE PRODUCER STAMP IS SPENT HERE, ONCE, AT THE TOP OF THE ELECTION.
    # It is single-use, and `register` runs up to six times below, so the
    # read cannot live there without the arming marker eating the answer.
    # A missing stamp is the overwhelmingly common case today and is
    # UNKNOWN, never main: an unattributed beacon is not this seat's.
    origin = beacon_origin.row_fields(
        *beacon_origin.from_env(session=session))
    out = {"seat": seat, "pid": pid, "stopped": [], "kept": [], "pruned": [],
           "registered": False, "already_live": None, "conflict": None,
           "alongside": None, "origin": origin["origin"]}
    if not valid_seat(seat):
        out["error"] = "seat name is not registry-safe; beacon runs unregistered"
        return out
    expect_sinks = None        # set only when a sink reading justifies a stop
    if not reap:
        # NO AUTHORITY TO REAP IS NOT A LICENCE TO REAP QUIETLY, and it is not
        # a licence to go blind either. This arm may stop nobody, so it REPORTS
        # the incumbent instead: one attributable equivalent waiter is
        # idempotent success (`already_live`), a same-session behavior mismatch
        # is the same structured `conflict` the caller turns into "use
        # --replace", and the one state the reaping path SIGTERMs its way
        # through — UNKNOWN, ghost, or duplicate holders the census cannot
        # prove — arms ALONGSIDE and names the way in. Two wake paths are
        # safer than zero; a reaped parent is neither.
        try:
            incumbent = _one_live_incumbent(
                seat, session, keep_pid=pid, proc_dir=proc_dir, waiter=waiter)
        except Exception as exc:             # noqa: BLE001
            incumbent = None
            out["error"] = ("live-incumbent probe failed (%s); nothing was "
                            "stopped either way" % exc)
        if incumbent and incumbent.get("sink") == SINK_REFUTES:
            # A WAITER ON A NON-WAKING SINK IS NOT AN INCUMBENT, and treating
            # it as one is how a seat stays deaf while every surface says it is
            # covered. Its stdout is a regular file or /dev/null, so it
            # consumes addressed rows and produces no wake — and because its
            # SPEC is identical to the requested one, equivalence alone called
            # it `already_live` and exited the newcomer cleanly. The seat then
            # followed advice to re-arm, the re-arm exited, and nothing said
            # why. A cross-family read found it as the residual of the sink
            # cure: the BANNER stopped claiming coverage, and the ARM still
            # refused the repair the banner asked for.
            #
            # IT FALLS THROUGH RATHER THAN STOPPING ANYTHING. This process has
            # no authority to signal another seat's waiter, and that rule is
            # not weakened by an fd reading. Falling through arms ALONGSIDE,
            # which restores a live wake path immediately; the non-waking
            # waiter is reported so a rotation can retire it deliberately.
            out["nonwaking"] = incumbent
            incumbent = None
        if incumbent:
            key = "already_live" if incumbent.pop("equivalent") else "conflict"
            out[key] = incumbent
            return out
        # THREE WORLDS, NOT ONE SENTENCE. The rows this seat holds are a
        # REGISTRY, and a registry outlives the processes it names: a pid here
        # may be a live waiter, a row whose incarnation is provably gone, or a
        # pid whose state this process cannot read at all. The text shipped at
        # the cure this supersedes asserted BOTH "no authority to stop" AND "could not be
        # proven live" over all three at once, so a seat whose only other rows
        # were stale was told it was arming alongside running waiters that had
        # exited hours earlier — an UNKNOWN and a dead row rendered as a live
        # one. Each pid is bucketed and the bucket is named.
        live, dead, unknown, stale_rows = [], [], [], []
        try:
            rows = _rows_by_pid(seat)
        except Exception:                    # noqa: BLE001
            rows = {}
        live, dead, unknown, stale_rows = _alongside_buckets(
            seat, pid, rows, proc_dir)
        if stale_rows:
            out["stale_rows"] = stale_rows
        others = live + dead + unknown
        if others:
            parts = []
            if live:
                parts.append("%s LIVE" % _pids(live))
            if unknown:
                parts.append("%s UNKNOWN (state unreadable — this is not a "
                             "claim that they are running)" % _pids(unknown))
            # PROVEN GONE SPLITS BY ROW MEMBERSHIP, because the two halves
            # send a reader to different places. A dead pid that HAS a row is
            # a stale registry entry someone can prune; a dead pid found only
            # by the SCAN never had one, so calling it a stale row invents an
            # artifact to go and clean up. The union that made this advice
            # honest about live waiters is the same union that made this
            # sentence wrong about dead ones.
            # AND THE SPLIT READS THE SOURCE THE BUCKETS CARRY, never a
            # second pid lookup. Asking `p in rows` re-derives provenance from
            # the number, so a stale registered incarnation and an exited
            # scanned successor sharing a pid both landed in `dead_rows` and
            # the same number was announced twice as a stale row. The entry
            # already knows which list it came from.
            dead_rows = [e for e in dead if e[2]]
            dead_scan = [e for e in dead if not e[2]]
            if dead_rows:
                parts.append("%s PROVEN GONE (stale registry rows, not "
                             "waiters)" % _pids(dead_rows))
            if dead_scan:
                parts.append("%s PROVEN GONE (seen by the process scan, never "
                             "registered — nothing to prune)"
                             % _pids(dead_scan))
            out["alongside"] = (
                "arms ALONGSIDE pid(s) %s: this process has no authority to "
                "stop another waiter for seat %r, so nothing was signaled. "
                "Their measured states are %s. A deliberate rotation is "
                "--replace from a process that exports HELM_CHAT_NAME=%s or "
                "states --on-behalf."
                # SORTED BY PID, NEVER BY THE WHOLE ENTRY. The entries carry
                # a starttime that is None whenever /proc could not be read,
                # and a tuple sort reaches that field the moment two entries
                # share a number -- exactly the collision these entries exist
                # to represent. Python then raises comparing None with an int,
                # `_cmd_wait` swallows it, and the pass registers NOTHING and
                # prints NOTHING: a cure for a rendering defect that took the
                # whole arm down. Measured: stale (pid, 100, registered) beside
                # scanned (pid, None, scan-only) is the reproducer.
                % (_pids(sorted(others, key=lambda e: e[0])), seat,
                   "; ".join(parts), seat))
        try:
            out["registered"] = bool(register(
                seat, session=session, pid=pid, origin=origin, proc_dir=proc_dir, now=now,
                waiter=waiter))
        except Exception as exc:             # noqa: BLE001
            out["error"] = "register failed (%s)" % exc
        return out
    marked = False
    try:
        marked = bool(register(seat, session=session, pid=pid, origin=origin,
                               proc_dir=proc_dir, now=now, phase="arming",
                               waiter=waiter))
    except Exception as exc:                 # noqa: BLE001
        out["error"] = "arming marker failed (%s)" % exc
    try:
        lock = _arm_lock(seat)
    except Exception as exc:                 # noqa: BLE001
        out["error"] = "arm election lock failed (%s)" % exc
        try:
            out["registered"] = bool(register(
                seat, session=session, pid=pid, origin=origin, proc_dir=proc_dir, now=now,
                waiter=waiter))
        except Exception as reg_exc:         # noqa: BLE001
            out["error"] += "; register failed (%s)" % reg_exc
        if not out["registered"] and marked:
            release(seat, pid)
        return out
    try:
        if not marked:
            note = "arming marker was not written; no incumbent signaled"
            out["error"] = "%s; %s" % (out["error"], note) \
                if out.get("error") else note
            try:
                out["registered"] = bool(register(
                    seat, session=session, pid=pid, origin=origin, proc_dir=proc_dir, now=now,
                    waiter=waiter))
            except Exception as exc:         # noqa: BLE001
                out["error"] += "; register failed (%s)" % exc
            return out
        if not replace:
            try:
                incumbent = _one_live_incumbent(
                    seat, session, keep_pid=pid, proc_dir=proc_dir,
                    waiter=waiter)
            except Exception as exc:         # noqa: BLE001
                out["error"] = ("live-incumbent probe failed (%s); no incumbent "
                                "signaled" % exc)
                try:
                    out["registered"] = bool(register(
                        seat, session=session, pid=pid, origin=origin, proc_dir=proc_dir,
                        now=now, waiter=waiter))
                except Exception as reg_exc:  # noqa: BLE001
                    out["error"] += "; register failed (%s)" % reg_exc
                if not out["registered"] and marked:
                    release(seat, pid)
                return out
            if incumbent and incumbent.get("sink") == SINK_REFUTES:
                # THE SAME RULE ON THE ELECTION PATH, and here it completes
                # rather than merely reports. A waiter whose stdout is a file
                # or /dev/null wakes nobody, so it is not the incumbent this
                # contender should defer to. Falling through reaches
                # `stop_superseded`, which is the one place that DOES hold
                # authority to retire it — so on this path the non-waking
                # waiter is actually removed instead of accumulating beside
                # the new one.
                out["nonwaking"] = incumbent
                # THE RETIREMENT IS BOUND TO THE OBJECT THAT JUSTIFIED IT. The
                # stop pass below re-reads fd 1 and compares it against this
                # token; if the waiter has since dup2'd a live pipe over it,
                # the reason to signal is gone and it is left alone.
                if incumbent.get("sink_id") is not None:
                    expect_sinks = {incumbent["pid"]: incumbent["sink_id"]}
                incumbent = None
            if incumbent:
                key = "already_live" if incumbent.pop("equivalent") else "conflict"
                out[key] = incumbent
                release(seat, pid)           # withdraw the uncommitted contender
                return out
        try:
            out.update(stop_superseded(seat, keep_pid=pid, proc_dir=proc_dir,
                                       expect_sinks=expect_sinks))
        except Exception as exc:             # noqa: BLE001
            out["error"] = "stop pass failed (%s)" % exc
        try:
            out["registered"] = bool(register(
                seat, session=session, pid=pid, origin=origin, proc_dir=proc_dir, now=now,
                waiter=waiter))
        except Exception as exc:             # noqa: BLE001
            out["error"] = "register failed (%s)" % exc
        if not out["registered"] and marked:
            release(seat, pid)
        return out
    finally:
        lock.close()


# ---------------------------------------------------------------------------
# the census — BOTH directions, always
# ---------------------------------------------------------------------------

def classify(pid, seat=None, row=None, live=None, proc_dir=None, records=None):
    """One beacon's row for the census: state + why, and never a guess."""
    holder = None
    if row and row.get("holder_pid"):
        holder = (row["holder_pid"], row.get("holder_start"))
    out = {"pid": pid, "seat": seat, "registered": row is not None,
           "armed": (row or {}).get("armed"), "session": (row or {}).get("session")}
    # GONE is read off the ARGV, not off a stat file: on a real /proc the
    # cmdline disappears with the process, and asking stat separately would
    # make an unreadable-but-live process look dead.
    if not is_waiter(pid, proc_dir):
        out.update(state=GONE, session_state="n/a",
                   why="pid %s is no longer a `helm chat wait` process" % pid)
        return out
    if row and row.get("starttime") is not None \
            and pid_alive(pid, row["starttime"], proc_dir) is False:
        out.update(state=GONE, session_state="n/a",
                   why="pid %s is a different incarnation than the row names"
                       % pid)
        return out
    env = proc_env(pid, proc_dir)
    # A seat name read from another process's argv/environ has passed no
    # join seam at all, so it is laundered where it ENTERS the row rather
    # than trusted to be laundered by whoever prints it.
    out["seat"] = seat or label(waiter_seat(pid, proc_dir, env=env))
    out["session"] = proc_session(pid, proc_dir, env=env) or out["session"]
    out["home_match"] = effective_home(env) == _our_home()
    if row is not None and row.get("phase") == "arming":
        out.update(state=UNKNOWN, session_state="arming",
                   why="the waiter is still contending for the arm lock and has "
                       "not entered wait, so it is not a wake path yet")
        return out
    # ORPHAN FIRST, and it outranks session liveness rather than duplicating
    # it. These two prove death at different layers: `orphaned` says the pipe's
    # READER is gone, `session_state` says the session behind it is gone. A
    # beacon can be orphaned while its session id still resolves to something
    # live, and it still cannot wake anybody — so the launcher question is
    # asked first and answered on its own terms.
    lost = orphaned(pid, proc_dir)
    if lost:
        out["session_state"] = "dead"
        out["state"] = GHOST
        out["why"] = ("its launcher is gone — the beacon was reparented to a "
                      "reaper, so nothing reads the pipe it writes wakes into")
        return out
    # THE LAUNCHER THE BEACON NAMES ITSELF, asked SECOND and outranking the
    # session rung below for the same reason `orphaned` outranks it: both this
    # and the reaper test prove death from PROCESS IDENTITY, which is the only
    # standard this file accepts, while `session_state` reasons about a
    # registry that a compaction can invalidate. It is asked after the reaper
    # test only because that one is cheaper, never because it is weaker — on
    # this host class it is the reaper test that misses, and this is the rung
    # that catches a wrapper-buffered orphan.
    _agent, lstate, lwhy = launcher(pid, proc_dir, env=env)
    if lstate == "dead":
        out.update(session_state="dead", state=GHOST, why=lwhy)
        return out
    state, why = session_state(out["session"], holder=holder, live=live,
                               proc_dir=proc_dir, records=records)
    out["session_state"] = state
    out["why"] = why
    out["state"] = {"live": LIVE, "dead": GHOST}.get(state, UNKNOWN)
    # AND THE SAME LAUNCHER ANSWERS THE POSITIVE HALF, which the first version
    # of this rung left out and its own arms caught: a beacon whose SESSION the
    # registry cannot resolve was UNKNOWN even with its launcher provably
    # alive. That is the identical blindness one layer down — a live wake path
    # reported as unclassifiable because the instrument asked the registry
    # instead of the process. If the agent that launched this beacon is alive
    # and the beacon is alive, the pipe HAS a reader, which is the whole
    # meaning of LIVE here.
    #
    # UNKNOWN ONLY. A GHOST is never promoted: `session_state` proves dead from
    # a recorded holder pid, and two proofs disagreeing is a fact to keep, not
    # one to resolve in favour of the cheerful answer.
    if out["state"] == UNKNOWN and lstate == "live":
        out["state"] = LIVE
        out["why"] = "%s, but %s" % (why, lwhy)
    # THE LEGACY DEMOTION IS SCOPED TO THE CASE THAT STILL NEEDS IT, and the
    # scoping is not a preference — the unscoped form ASSERTS A FALSEHOOD. It
    # fires on `lost is None`, which means `orphaned` could not NAME THE PARENT:
    # a readable ppid whose comm cannot be read. `launcher` identifies the
    # launcher by a different and stronger route — CLAUDE_PID from the beacon's
    # own environ, confirmed against the kernel's `exe` link — so when it has
    # answered `live`, "its launcher could not be identified" is untrue of the
    # very row it is written onto, and it erases a proof with a sentence
    # denying that proof exists. Where no launcher was proven, the demotion is
    # exactly as right as it ever was and is left alone.
    if lost is None and out["state"] == LIVE and lstate != "live":
        # The launcher could not be identified, so "live" is not provable.
        out["state"], out["session_state"] = UNKNOWN, state
        out["why"] = "%s, but its launcher could not be identified" % why
    return out


def _verdict(live_, unknown, unlistable, agent, agent_why, undrained=None,
             sidechain=False, expired=()):
    """(verdict, why) — the seat ladder, in one place so the verdicts cannot
    drift apart across two call sites.

    A LIVE WAKE PATH IS NOT THE WHOLE ANSWER, AND NEITHER IS A LIVE AGENT.
    `live_` proves a beacon answers; whether a SEAT answers is a different
    question with a different instrument, and the agent rung is where those two
    are allowed to disagree. `undrained` is the THIRD question and the only one
    asked of the DELIVERY rather than of a process: seconds the oldest row
    addressed to this seat has waited, or None when nothing is waiting. A
    covered, occupied seat with rows older than the grace was not woken by
    them, whatever every process-shaped instrument says.

    IT REFINES COVERED AND NOTHING ELSE. A seat with no live beacon is DEAF
    whether or not rows are waiting — that verdict is already the stronger
    claim — and an unclassifiable seat stays UNPROVEN rather than acquiring a
    verdict from a fact that cannot be attributed to it.

    `expired` is the other refinement, and it refines DEAF only (task/3055):
    the beacons `seat_census` kept because they ended at their lease deadline
    inside the re-arm grace. With no live and no unknown beacon, at least one
    expired row and a pane that DECLARES this seat (`agent` True — the same
    independent evidence VACANT rests on, so a dead session cannot read
    WAKING), the seat is re-arming, not deaf. Every other combination falls
    through to DEAF unchanged."""
    if unlistable:
        return UNPROVEN, "the process table could not be listed"
    if live_:
        if sidechain:
            # A BEACON THAT RUNS AND DELIVERS TO THE WRONG CONVERSATION. Its
            # wake-lines reach the subagent transcript that armed it and never
            # this seat, so the seat is unreachable while every process-shaped
            # instrument reports a healthy live waiter. This is the ONE rung
            # that can say so, and it is deliberately the narrowest claim the
            # evidence supports: it requires at least one live beacon and
            # EVERY live beacon PROVEN sidechain. One proven top-level beacon
            # defeats it, and an UNKNOWN never contributes to it -- an
            # unreadable transcript is ignorance, not misrouting.
            return MISROUTED, (
                "every live beacon for this seat was armed from a SIDECHAIN, "
                "so its wakes land in the subagent conversation that armed it "
                "and never reach this seat")
        state = {True: COVERED, False: VACANT, None: UNPROVEN}[agent]
        if state == COVERED and undrained is not None \
                and undrained >= UNDRAINED_S:
            return DEAF_IN_EFFECT, (
                "%s, and yet a row addressed to it has waited %s without being "
                "consumed: the wake path is live and nothing woke the seat"
                % (agent_why or "its wake path is live and a seat is home",
                   _age_s(undrained)))
        return state, agent_why
    if unknown:
        return UNPROVEN, "no beacon could be PROVEN live (%d unknown)" % len(unknown)
    if expired and agent is True:
        # THE NEWEST EXPIRY SPEAKS: it is the one the seat is re-arming after,
        # and it is the one whose grace ends last.
        past = min(float(b.get("lease_past") or 0.0) for b in expired)
        return WAKING, (
            "beacon expired %s ago (%d-minute lease); %s; re-arm expected "
            "within %s" % (_age_s(max(0.0, past)), BEACON_TIMEOUT_MS // 60000,
                           agent_why or "a pane declares this seat",
                           _age_s(max(0.0, REARM_GRACE_S - past))))
    return DEAF, "no live beacon: helm cannot wake it"


def repair_argv(seat):
    """THE ONE COMMAND THAT REPAIRS A DEAF-IN-EFFECT SEAT, spelled once.

    Every surface that advises this repair — the census line, the readiness
    check, the docs — reads it from here, because an argv that appears in
    three places is an argv that is wrong in two of them. It was: the line
    printed `--deliver`, which is the detached child's own interface and whose
    parser refuses without `--session` and `--text-file`, so the advertised
    repair could not run."""
    return "helm seat resume-turn --nudge --seat %s" % seat


DRAIN_Q = "is the row the alarm was raised on consumed"


def _held_why(drained):
    """Why a seat that now reads COVERED is still being called DEAF-IN-EFFECT,
    GENERATED FROM the drain reading rather than re-derived beside it.

    IT TAKES THE READING, NOT THE INPUTS. Given `(owed, crow)` this would have
    to work out which case it is in a second time, and the sentence the
    operator reads and the decision the ladder takes would be two derivations
    of one fact that agree only by inspection. The decision carries its own
    grounds, so this only says what follows from them.

    NAMES THE GAP, NOT THE SEAT. The seat may genuinely have caught up; what
    is missing is the reading that would say so, and a reader told "still
    waiting" without that distinction would chase the wrong thing."""
    if drained.unknown:
        because = drained.lacked
    else:
        room = (drained.answer or ("its home room",))[0] or "its home room"
        because = ("the row that raised the alarm is still waiting in %s — a "
                   "row that is still there was not consumed" % room)
    return "the standing alarm is HELD rather than cleared: %s" % because


def drain_proven(owed, crow):
    """A READING of whether THIS pass proved the alarm's row is consumed.

    The alarm names a row; recovery is about that row and about nothing else.
    A pass proves the drain only by reading the row's OWN room and not finding
    it there.

    THREE ANSWERS DO NOT FIT IN A BOOL, AND THAT IS THE DEFECT THIS SHAPE
    ENDS. Returned as a bool, "the census failed", "the rotation never reached
    the room" and "the row is still sitting there" all reach the caller as
    False, while "no row was named" and "the room was read and the row is gone"
    both reach it as True. Five facts in two values, and the pair of opposite
    errors follows from it: one stage refuses a real drain, the stage beside it
    invents one, and neither can do otherwise because the value they read
    cannot tell them apart.

    NOW A GAP IS UNKNOWN AND A MEASURED NEGATIVE IS REFUTED, and only PROVEN
    clears an alarm. A caller that treats UNKNOWN as either answer has to write
    that down, which is the whole point.

    NO ROW NAMED IS NOT A FAILURE. An alarm raised before this field existed,
    or one whose evidence was lost, holds nothing back: there is no claim to
    contradict, so the ordinary verdict stands."""
    if not owed:
        return consumption.proven(
            DRAIN_Q, True,
            consumption.Evidence(
                "no-row-named",
                "the alarm named no row, so there is no claim to contradict"))
    reason = crow.get("undrained_unreadable")
    if reason:
        return consumption.unknown(
            DRAIN_Q,
            "its consumption could not be read this pass (%s) — an unreadable "
            "census is not a drained one" % reason)
    ev = crow.get("undrained_evidence") or _NO_EVIDENCE
    scanned = tuple(str(x) for x in (ev.get("scanned") or ()))
    seen = tuple(tuple(x) for x in (ev.get("seen") or ()))
    bounded = dict(ev.get("bounded") or {})
    owed = (str(owed[0]), str(owed[1]))
    if owed[0] not in scanned:
        # TWO REASONS A ROOM IS NOT IN `scanned`, AND THEY ARE NOT THE SAME
        # REFUSAL. The rotation may simply not have reached it; or this pass
        # DID reach it and could not read all of it. Naming which one is the
        # difference between "look again next pass" and "something is wrong
        # with this room", and a single sentence covering both sent readers to
        # the wrong instrument.
        outcome, detail = bounded.get(owed[0], (None, None))
        if outcome:
            return consumption.unknown(
                DRAIN_Q,
                "this pass reached %s and could not read it whole (%s: %s) — "
                "absence from a window helm could not finish is a gap in the "
                "sampling, not a fact about the room"
                % (owed[0] or "its home room", outcome, detail or "no detail"),
                consumption.Evidence("bounded-read", (owed[0], outcome)))
        return consumption.unknown(
            DRAIN_Q,
            "this pass never read %s, where the row that raised the alarm is "
            "waiting — the room scan rotates and an unsampled room is not an "
            "empty one" % (owed[0] or "its home room"),
            consumption.Evidence("scanned-rooms", scanned))
    looked = (consumption.Evidence("scanned-rooms", scanned),
              consumption.Evidence("rows-seen", seen))
    if owed in seen:
        return consumption.refuted(DRAIN_Q, *looked, answer=owed)
    return consumption.proven(DRAIN_Q, True, *looked)


# THE LEXER DECIDES WHAT IS SYNTAX; NOTHING DOWNSTREAM RE-DECIDES IT.
#
# Every earlier shape of this reader asked a LATER stage to recover something
# the tokenizer had already thrown away, and each time the missing thing was
# the same: whether a delimiter-shaped value was an OPERATOR or DATA, and how
# much of the command a conditional owns.
#
#   * a separator split over raw text called a quoted `;` an operator;
#   * POSIX `shlex` grouped quoted runs correctly but handed back BARE values,
#     so a consumer matching `>` against a token could not tell `'>'` (an
#     argument) from `>` (a redirection);
#   * composing the two fixed the separators and left the word consumer still
#     pattern-matching values, so `helm '>' out chat wait` had its quoted
#     argument and the word after it DELETED, manufacturing a waiter;
#   * matching a redirection only at the START of a word missed one attached to
#     the command word, so `false>/dev/null` arrived as a single token whose
#     basename is `null` and the known-dead `false` was lost;
#   * matching operators without longest-first ordering split `>|` at its `|`,
#     turning a clobber redirection into a pipe and its FILENAME into a
#     command;
#   * and splitting the conditional chain per SEGMENT rather than per PIPELINE
#     let `false && printf x | W` skip only `printf x`, because the `|` that
#     introduced the waiter carries no conditional rule of its own.
#
# So the tokenizer emits OPERATORS AS THEIR OWN TOKENS, longest match first,
# recognized only outside quotes, and marks each word with whether any part of
# it was quoted. Redirections then consume their operand uniformly (the
# operator and its target are always separate tokens, so an "attached" target
# is not a special case), and the parser groups tokens into an AND-OR list
# whose ELEMENTS ARE WHOLE PIPELINES, so a dead branch skips all of one.
#
# Operators are ordered longest-first: `&>>` before `&>` before `&&`, `<<<`
# before `<<` before `<`, and `>|`/`>&`/`>>` before `>`.
_OPERATORS = tuple(sorted(
    ("&>>", "<<<", "<<-", ";;&", "&&", "||", ";;", ";&", "&>", ">>", ">|",
     ">&", "<<", "<&", "<>", ";", "|", "&", ">", "<", "\n", "(", ")"),
    key=len, reverse=True))
#: THE CASE TERMINATORS ARE NOT SEPARATORS, THEY ARE A SYNTAX ERROR HERE.
#: `;;` sat in `_SEPARATORS` and `;&`/`;;&` lexed as two ordinary operators,
#: so `<wait>;; true` read as a plain list and the wait was PROVEN -- while
#: bash rejects the whole command with rc 2 and runs NOTHING (measured, all
#: three spellings). They are only legal inside a `case`, and a `case` needs
#: a `)`, which this lexer already refuses -- so any of them reaching here is
#: in a command bash would not run. Found by a cross-family read, round 8.
_CASE_TERMINATORS = frozenset((";;", ";&", ";;&"))
#: `|&` IS ABSENT FROM THE TABLE ABOVE AND THAT IS CURRENTLY CORRECT BY
#: ACCIDENT, WHICH IS WHY IT IS WRITTEN DOWN. It is bash's shorthand for
#: `2>&1 |`; this lexer reads it as `|` then `&`, a different parse. Measured
#: over 16 commands -- 8 plain and 8 conditional, the latter being where a
#: wrong pipeline split would flip the answer because `&&` and `||` test the
#: PIPELINE's status -- the reader agrees with bash on every one, because the
#: `&` opens a new list item and the remainder parses identically. Adding the
#: operator would CHANGE a parse that currently answers correctly, so the gap
#: is documented rather than closed; anyone closing it owes those 16 cases
#: again.
_HEREDOCS = frozenset(("<<", "<<-"))
_GROUPING = frozenset(("(", ")"))


# THE STATUS OF A LIST IS A SET OF POSSIBILITIES, NEVER ONE VALUE, and holding
# one value is precisely how a conditionally-executed literal got laundered
# into an unconditional predecessor.
_SUCCESS = "success"
_FAILURE = "failure"


def _match_operator(text, i):
    """The longest operator spelling starting at `i`, or None.

    LONGEST FIRST IS NOT A MICRO-OPTIMISATION, IT IS THE MEANING. `>|` read as
    `>` followed by `|` turns one clobber redirection into a redirection plus a
    PIPE, and everything after the operand then reads as a new command — which
    is how `printf x >| helm chat wait` credited a waiter that never runs, its
    `helm` being the output FILENAME."""
    for op in _OPERATORS:
        if text.startswith(op, i):
            return op
    return None


def _skip_expansion(text, i):
    """Index just past a `$(...)`, `${...}` or backtick expansion at `i`.

    AN EXPANSION IS OPAQUE, NOT STRUCTURE. Its text is whatever it produces at
    runtime, so this reader keeps it inside the word it appeared in and never
    looks for operators or commands inside it: a substitution that happens to
    contain `&&` did not put a conditional in THIS command. Returns None when
    the construct is unterminated, which makes the whole command unparseable."""
    if text[i] == "`":
        j = i + 1
        while j < len(text):
            if text[j] == "\\":
                j += 2
                continue
            if text[j] == "`":
                return j + 1
            j += 1
        return None
    opener, closer = ("(", ")") if text[i + 1] == "(" else ("{", "}")
    depth, j = 0, i + 1
    while j < len(text):
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c in "'\"":
            end = text.find(c, j + 1)
            if end < 0:
                return None
            j = end + 1
            continue
        if c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return None


def _lex(command):
    """The command text -> [(kind, value, quoted)], or None when unmodelled.

    `kind` is `"word"` or `"op"`; `quoted` says whether any part of a word came
    out of quotes or an escape, which is THE fact every earlier shape of this
    reader discarded.

    It tracks single quotes, double quotes with backslash escapes, an unquoted
    backslash escape, and `$(...)`/`${...}`/backtick expansions, which it keeps
    whole inside the word they belong to. A `#` that begins a word starts a
    comment to end of line.

    AN FD PREFIX BELONGS TO THE OPERATOR IT PRECEDES: in `2> err.log` the `2`
    is not an argument and `err.log` is not a command, so a pending word that
    is entirely unquoted digits is absorbed into a following `<`/`>` operator.

    Returns None — unparseable, which names nobody — for an unbalanced quote or
    expansion, for a HEREDOC, whose body this reader does not model and could
    otherwise read as commands, and for the GROUPING operators `(` and `)`,
    which put a whole list under one condition."""
    tokens = []
    word, quoted, open_ = [], False, False
    i, n = 0, len(command)

    def flush():
        if open_:
            tokens.append(("word", "".join(word), quoted))

    while i < n:
        c = command[i]
        if c == "\\" and command[i + 1:i + 2] == "\n":
            # A LINE CONTINUATION, AND BOTH CHARACTERS VANISH. Treating the
            # escaped newline as an ordinary escape appended a REAL newline to
            # the pending word, which is wrong three ways at once: it opened a
            # word where the shell has none, so the `#` that may follow was
            # read as text instead of starting a comment; it displaced the
            # command head, so `false && \\<nl> <wait>` answered "not a wait"
            # for the wrong reason; and when a blank or comment line followed,
            # the NEXT newline arrived with a word in front of it, so it read
            # as a separator and the gate in front of the wait was discarded.
            # That last one invents an owner on an ordinary two-line command.
            i += 2
            continue
        if c == "\\" and i + 1 < n:
            word.append(command[i + 1])
            quoted, open_ = True, True
            i += 2
            continue
        if c == "'":
            end = command.find("'", i + 1)
            if end < 0:
                return None
            word.append(command[i + 1:end])
            quoted, open_ = True, True
            i = end + 1
            continue
        if c == '"':
            j = i + 1
            while j < n and command[j] != '"':
                j += 2 if command[j] == "\\" else 1
            if j >= n:
                return None
            word.append(command[i + 1:j].replace("\\", ""))
            quoted, open_ = True, True
            i = j + 1
            continue
        if c == "`" or (c == "$" and command[i + 1:i + 2] in ("(", "{")):
            end = _skip_expansion(command, i)
            if end is None:
                return None
            word.append(command[i:end])
            open_ = True
            i = end
            continue
        if c == "#" and not open_:
            while i < n and command[i] != "\n":
                i += 1
            continue
        if c.isspace() and c != "\n":
            flush()
            word, quoted, open_ = [], False, False
            i += 1
            continue
        op = _match_operator(command, i)
        if op is not None:
            if op in _HEREDOCS or op in _GROUPING \
                    or op in _CASE_TERMINATORS:
                return None
            emit = op
            if open_ and not quoted and op[0] in "<>" \
                    and all(ch.isdigit() for ch in word) and word:
                emit = "".join(word) + op     # the fd prefix is the operator's
            else:
                flush()
            word, quoted, open_ = [], False, False
            tokens.append(("op", emit, False))
            i += len(op)
            continue
        word.append(c)
        open_ = True
        i += 1
    flush()
    return tokens


#: The wait itself is three words, not a builtin, and is recognised positionally
#: by `_holds_the_wait`.


#: THE `+` DIRECTION IS READ THE SAME AS `-`, WHICH IS SAFE AND LOSSY, AND
#: THE OBVIOUS OPTIMISATION IS A TRAP. Measured: `set +n`, `set +u`,
#: `set +o noexec`, `set +o nounset` and `set -u; set +u` all RUN the wait
#: while this reader answers None -- five conservative misses, no false
#: credit. Whitelisting the `+` forms as harmless would recover them AND
#: BREAK THIS, also measured:
#:
#:     set -n; set +n; <wait>              bash runs NOTHING
#:     set -o noexec; set +o noexec        bash runs NOTHING
#:
#: Once noexec is on the shell PARSES without executing, so the `set +n` that
#: would turn it off is itself never executed and the option never comes back.
#: The misses stay.


def _row_stamp(row):
    return row.get("timestamp") if isinstance(row, dict) else None


def unreachable(row):
    """Does this census row have NO PROVEN WAKE PATH? The ALARM CLASS.

    DEAF is the proven half: no live beacon, so helm cannot wake it. The other
    half is the one the 2026-08-03 replay exposed — UNPROVEN WITH NO LIVE
    BEACON. Not one of that seat's beacons could be proven live AND the seat
    could not be classified, which is a POSSIBLE OUTAGE the instruments merely
    failed to name. Silence about a seat is not evidence about the seat.

    IT IS NOT THE OTHER UNPROVEN, and that distinction is the whole reason
    this is a predicate instead of a verdict. A seat whose beacon IS live and
    whose pane declares no seat of its own (`_verdict`'s agent rung) has a
    WORKING wake path and is unclassifiable only because a hand-launched pane
    stamps nothing — 8 of the 16 live panes on this box are exactly that, so
    alarming on it would fire for half the fleet forever and an alarm that
    fires for everything says nothing.

    THE SEATS THIS CATCHES ARE THE NON-CLAUDE FAMILIES. `live_sessions` proves
    liveness from a claude session record; a kimi or codex seat therefore reads
    UNPROVEN while it is perfectly healthy AND while it is dead, and until this
    predicate existed the census could only ever whisper about it.

    WAKING IS NOT IN IT (task/3055). A seat inside its re-arm grace has no
    live beacon for about 30 seconds every half hour by the harness's design,
    and an alarm on that fires for every healthy seat twice an hour. If the
    seat does not re-arm, the next pass past the grace reads DEAF, and that
    one alarms."""
    if row.get("verdict") in (DEAF, DEAF_IN_EFFECT, MISROUTED):
        return True
    return row.get("verdict") == UNPROVEN and not row.get("live")


def undrained(seat, session=None, room=None, reader=None, now=None,
              scope=None, occurrence=False):
    """(seconds the OLDEST row addressed to this seat has waited, unreadable,
    evidence) — THE CONSUMPTION FACT, which no process-shaped instrument
    answers. `evidence` is `{"oldest": (room, id) or None, "scanned": (room,
    ...), "seen": ((room, id), ...)}`: the row the age was measured on, the
    rooms this pass actually read, and every pending row it found in them.

    A LIVE WAITER IS A FACT ABOUT A PROCESS AND NOT ABOUT A DELIVERY. Every
    surface this module already reads asks whether something is RUNNING: a
    beacon answers, a pane holds a pid, an agent claims the seat. A seat can
    satisfy all three, take turns, and still never consume a row addressed to
    it — and then the rows simply sit, which is the one observable the owner
    ever sees.

    SECONDS, NEVER A COUNT. A rising count needs two observations and a place
    to keep the first; the age of the oldest waiting row is the same fact
    available from ONE read, and it is the one that scales with harm.

    THE SCAN LANE IS ITS OWN. `_pending_all` gives each consumer an
    independent identity queue precisely so one cannot steal another's
    eventual coverage, so this census reads under its own lane and moves no
    cursor.

    UNREADABLE IS NOT DRAINED. A census that could not be taken returns its
    reason, and the caller must not let that reach the ladder as "nothing is
    waiting" — that is the representation collapse this whole module exists to
    refuse.

    AND NEITHER IS AN EMPTY SAMPLE. `_pending_all` rotates a BOUNDED slice of
    foreign rooms and reads one capped window of each, so finding nothing is
    evidence about THIS PASS and never about the backlog: the same untouched
    row can be sampled, missed, and sampled again.

    SO THE EVIDENCE SAYS WHAT WAS LOOKED AT, not only what was found. An alarm
    records the row it was raised about; the drain is PROVEN when a later pass
    reads THAT ROW'S ROOM and no longer finds it there. An empty sample that
    never reached the room proves nothing, and a caller that clears an alarm on
    it has read a gap in its own sampling as a fact about the world.

    READING THE ROOM IS ENOUGH ONLY WHEN THE READ WAS WHOLE. It is tempting to
    argue that the byte cap cannot weaken this — that an unconsumed old row
    sits at the near edge of the window and is therefore always in the sample —
    and that argument fails on one clause: OLD IS AGE, NOT BYTE OFFSET. The
    window starts at this consumer's cursor and runs FORWARD a bounded number
    of bytes, so enough non-eligible rows between the cursor and the oldest
    eligible addressed row push that row past the end of it. The narrower
    theorem, that a previously-observed row stays visible under a stable
    forward cursor, is true and is not the guarantee a reader would take from
    the wider one — which is why no version of the wider one belongs here. The
    producer reports whether each room's window was truncated, and a truncated
    read of the owed room answers UNKNOWN.

    `occurrence=True` asks the producer for the PHYSICAL occurrence of the
    oldest row as well, and the evidence then carries it as `occurrence`: the
    room identity, byte range and exact bytes, the two cursor values the
    answer was computed from, and whether the room's cursor seqlock held still
    across that read. An act taken on this answer is re-validated against
    exactly those, so they are bound here, during the observation, and never
    reconstructed afterwards. `scope` is the seat scope a caller derived from
    a roster read it trusts; eligibility is then decided against it.
    """
    read = reader
    if read is None:
        from .seats_stop_fp import _pending_all as read
    try:
        # THE SEAT'S OWN ROOM, NEVER THE CALLER'S. `_pending_all` scans out
        # from the room it is given, and the question here is what THIS seat
        # has waiting — a census run from an integrator's cwd must not answer
        # it from the integrator's home. `main` is the honest last resort for
        # a row that declares none, exactly as the post path treats it.
        # THE BEACON'S QUESTION, NOT THE BOUNDARY'S. This verdict is about a
        # seat that WAS NOT WOKEN, so the rows it may count are exactly the
        # rows that would have woken it: `ambient=False, beacon=True` is the
        # idle beacon's own pair. The default pair counts plain home-room
        # chatter and muted rooms, which a seat is ENTITLED to leave sitting —
        # counting those raised an alarm about a seat doing nothing wrong, and
        # then typed into its pane about it.
        cover = {"occurrences": {}} if occurrence else {}
        hits = read(room or "main", seat, session=session,
                    scan_lane="beacons", ambient=False, beacon=True,
                    coverage=cover,
                    **({"scope": scope} if scope is not None else {}))
    except Exception as e:                   # noqa: BLE001
        return None, ("the pending census could not be taken (%s: %s)"
                      % (e.__class__.__name__, e)), _NO_EVIDENCE
    oldest, unreadable, ref, oldest_row = None, None, None, None
    # SCANNED IS WHAT THE PASS ATTEMPTED, NOT WHERE IT FOUND SOMETHING. Built
    # from the hits, this list lost every room that was read and found EMPTY --
    # so consuming the sole pending row in the owed room made that room vanish
    # from `scanned`, which the drain question reads as "this pass never looked
    # there", so the alarm the consumption should have cleared could never be
    # cleared by it (task/2463 finding 1). The producer now reports the rooms it
    # attempted and how each read went, and `bounded` carries the ones whose
    # answer is not trustworthy as an absence.
    rooms = cover.get("rooms") or {}
    scanned = tuple(r for r, (outcome, _d) in sorted(rooms.items())
                    if outcome == "read")
    bounded = {r: (outcome, detail) for r, (outcome, detail) in rooms.items()
               if outcome != "read"}
    estate = cover.get("estate") or ("unknown", "the producer said nothing")
    seen = []
    for hit_room, row in hits or ():
        here = str(hit_room)
        seen.append((here, str((row or {}).get("id") or "")))
        if here in bounded:
            # A ROW FROM A ROOM THAT WAS NOT READ WHOLE CANNOT DATE THE
            # BACKLOG. The wake cursor is what SUPPRESSES rows a wake already
            # crossed, so losing it does not hide rows -- it UN-HIDES consumed
            # ones, and the oldest of those carries an age that reads as an
            # overdue row and types into a pane about an obligation already
            # met. Uncertain suppression is not positive evidence, and the
            # room's own outcome is what says so. Rooms that WERE read whole
            # still date the backlog, whatever happened elsewhere.
            unreadable = unreadable or (
                "a row addressed to this seat was found in %s, which was not "
                "read whole (%s), so its age cannot date the backlog"
                % (here, bounded[here][0]))
            continue
        stamp = _row_epoch(row)
        if stamp is None:
            unreadable = ("a row addressed to this seat carries no readable "
                          "timestamp, so its age is unknown")
            continue
        if oldest is None or stamp < oldest:
            oldest, ref = stamp, (here, str((row or {}).get("id") or ""))
            oldest_row = row
    ev = {"oldest": ref, "scanned": scanned, "seen": tuple(seen),
          "bounded": bounded, "estate": estate}
    if occurrence:
        # IDENTITY, NOT EQUALITY: the occurrence is the one the producer
        # recorded for THIS row object, so two rows that happen to agree
        # field for field cannot lend each other a byte range.
        seen_room = (cover.get("occurrences") or {}).get(ref[0]) \
            if ref else None
        found = next((o for o in (seen_room or {}).get("list") or ()
                      if o.get("row") is oldest_row), None)
        ev["occurrence"] = (dict(found, coherent=bool(seen_room["coherent"]))
                            if found is not None else None)
    if oldest is None:
        return None, unreadable, ev
    return (max(0.0, (time.time() if now is None else now) - oldest),
            unreadable, ev)


#: The coverage question, asked of a sample before its EMPTINESS is believed.
COVER_Q = "did this pass see enough for an empty result to mean drained"


def sample_is_complete(ev):
    """A Reading: may an EMPTY sample be read as a drained backlog?

    task/2463 finding 2, and it is `drain_proven`'s own lesson at a door that
    never learned it. `_still_owed` took `undrained`'s three-tuple, threw the
    evidence away, and read a wait of None as "nothing is waiting any more" --
    so a rotation that simply did not reach the seat's rooms produced a
    DRAINED verdict, the repair settled against the spell that earned it, and
    the backlog it was raised about never moved. An empty sample is only a
    drained backlog when the sample could SEE.

    THE TWO WAYS IT COULD NOT. A pass that read no room at all found nothing
    by not looking. A pass that reached a room and could not read it whole --
    a cursor it could not trust, a window the byte cap cut short -- found
    nothing in a part of a room. Both are UNKNOWN and neither is a negative."""
    ev = ev if isinstance(ev, dict) else _NO_EVIDENCE
    scanned = tuple(str(x) for x in (ev.get("scanned") or ()))
    bounded = dict(ev.get("bounded") or {})
    if bounded:
        return consumption.unknown(
            COVER_Q,
            "this pass reached %d room(s) it could not read whole (%s), and "
            "an absence inside a window helm could not finish is a gap in the "
            "sampling rather than an empty room"
            % (len(bounded), ", ".join(sorted(bounded))),
            consumption.Evidence("bounded-rooms", tuple(sorted(bounded))))
    if not scanned:
        return consumption.unknown(
            COVER_Q,
            "this pass read no room at all, so it found nothing by not "
            "looking — which is the one result that must never be read as a "
            "cleared backlog",
            consumption.Evidence("scanned-rooms", ()))
    # A NONEMPTY SAMPLE IS NOT A COMPLETE ONE, and reading ANY rooms whole was
    # enough to answer PROVEN here. The room scan is a BOUNDED ROTATION over a
    # larger estate: a pass that fully reads the pinned rooms and every empty
    # room it was handed, while the rotation's slice simply does not include
    # the room an overdue row is sitting in, produces exactly this evidence. An
    # unrelated full read cannot prove a drain.
    outcome, detail = (ev.get("estate")
                       or ("unknown", "the producer said nothing"))
    if outcome != "complete":
        return consumption.unknown(
            COVER_Q,
            "this pass did not see the whole room estate (%s%s), so rooms it "
            "never reached could hold the row — a full read of the rooms it "
            "DID reach says nothing about the ones it did not"
            % (outcome, ": " + detail if detail else ""),
            consumption.Evidence("scanned-rooms", scanned),
            consumption.Evidence("estate", outcome))
    return consumption.proven(
        COVER_Q, scanned,
        consumption.Evidence("scanned-rooms", scanned),
        consumption.Evidence("estate", "complete"))


# A census that never ran looked at nothing. Spelled once so the "did this
# pass reach the alarm's room" question has one answer shape everywhere.
_NO_EVIDENCE = {"oldest": None, "scanned": (), "seen": (), "bounded": {},
                "estate": ("unknown", "no census ran")}


def _row_epoch(row):
    """A chat row's UTC epoch, or None. The store spells it
    `2026-09-13T20:13:52Z`; anything else is unreadable rather than now."""
    import calendar
    try:
        return calendar.timegm(
            time.strptime(str((row or {}).get("ts") or ""),
                          "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return None


def lease_age(row, now=None):
    """Seconds past this registry row's LEASE DEADLINE (armed + the Monitor
    timeout), when its death has the lease's shape, else None.

    Shape means: the row was committed (an `arming` marker never entered wait,
    so it had no lease), it carries a readable arm time, and now falls inside
    [deadline - EXPIRY_SKEW_S, deadline + REARM_GRACE_S]. The value can be
    slightly negative inside the skew. Only a caller that has already PROVEN
    the pid GONE may use this: it is a statement about how a dead beacon died,
    never about whether one is alive."""
    if not isinstance(row, dict) or row.get("phase") == "arming":
        return None
    try:
        armed = float(row.get("armed"))
    except (TypeError, ValueError):
        return None
    past = (time.time() if now is None else float(now)) - armed \
        - BEACON_TIMEOUT_MS / 1000.0
    if -EXPIRY_SKEW_S <= past <= REARM_GRACE_S:
        return past
    return None


def seat_census(seat, live=None, proc_dir=None, records=None, agents=_UNPROBED,
                waited=None, undrained_unreadable=None,
                undrained_evidence=None, homes=None, now=None):
    """Every beacon for ONE seat — EVERY one, not the newest.

    Taking the newest arm-time was the exact narrow-question error that made a
    seat with five waiters, four of them stale, read as "current" (chat #helm
    row 622). The verdict is computed over the whole set.

    A GONE beacon's row is pruned, EXCEPT one that died at its lease deadline
    and is still inside the re-arm grace (`lease_age`, task/3055). That row is
    kept and reported EXPIRED, because the kill that ended it never let its
    own `finally` release the row, and pruning it here would erase the only
    evidence that the seat is re-arming rather than deaf. It is pruned by the
    first pass after the grace, as every other dead row is."""
    rows = _rows_by_pid(seat)
    scanned = _scan(seat, proc_dir)
    unlistable = scanned is None
    beacons = [classify(pid, seat, rows.get(pid), live, proc_dir, records)
               for pid in sorted(set(rows) | set(scanned or []))]
    expired = []
    for row in list(rows.values()):
        gone = [b for b in beacons
                if b["pid"] == row["pid"] and b["state"] == GONE]
        if not gone:
            continue
        past = lease_age(row, now)
        if past is None:
            prune(row)
            continue
        gone[0].update(state=EXPIRED, lease_past=past,
                       why="pid %s ended at its %d-minute lease deadline "
                           "%s ago" % (row["pid"], BEACON_TIMEOUT_MS // 60000,
                                       _age_s(max(0.0, past))))
        expired.append(gone[0])
    beacons = [b for b in beacons if b["state"] not in (GONE, EXPIRED)]
    live_ = [b for b in beacons if b["state"] == LIVE]
    ghosts = [b for b in beacons if b["state"] == GHOST]
    unknown = [b for b in beacons if b["state"] == UNKNOWN]
    agent, agent_why = agent_verdict(seat, live_, agents, live, proc_dir)
    # WHICH CONVERSATION EACH LIVE BEACON ANSWERS TO, and the count of the
    # ones this pass could not attribute at all. The count travels because a
    # verdict built from PROVEN sidechain alone is silent about ignorance:
    # with it absent, zero MISROUTED reads as "no beacon is misrouted" when
    # what actually happened may be "none could be attributed". Numerator and
    # denominator, or the rung makes the census look MORE certain than it is.
    owners, unattributed, unstamped, owner_reasons = [], 0, 0, []
    for b in live_:
        row = rows.get(b["pid"]) or {}
        # THE ROW IS THE ANSWER. Not the transcript, not the command string:
        # this reads the stamp its own producer wrote at registration, so a
        # beacon nothing stamped is UNKNOWN and contributes to no alarm.
        owner, owner_why = row_origin(row)
        owners.append(owner)
        if owner not in (OWNER_MAIN, OWNER_SUBAGENT):
            unattributed += 1
            unstamped += origin_unstamped(row)
            owner_reasons.append(owner_why or "unattributed, no reason given")
    sidechain = bool(live_) and all(o == OWNER_SUBAGENT for o in owners)
    verdict, why = _verdict(live_, unknown, unlistable, agent, agent_why,
                            undrained=waited, sidechain=sidechain,
                            expired=expired)
    return {"seat": seat, "verdict": verdict, "why": why, "agent": agent,
            "owners": owners, "unattributed": unattributed,
            "unstamped": unstamped, "owner_reasons": owner_reasons,
            "beacons": beacons, "waited": waited,
            # WHY THE WAIT IS NONE, WHICH IS NOT THE SAME QUESTION AS WHAT IT
            # IS. `undrained_unreadable` names a census that could not be
            # taken; `undrained_evidence` names the row a wait was measured on
            # and the rooms this pass actually read. With a wait of None and no
            # reason the sample was simply empty, and empty is not drained —
            # see `undrained`.
            "undrained_unreadable": undrained_unreadable,
            "undrained_evidence": undrained_evidence or _NO_EVIDENCE,
            "live": live_, "ghosts": ghosts, "unknown": unknown,
            # KEPT APART FROM `beacons`: an expired row is a dead process, and
            # counting it among the beacons would put it in the fleet's beacon
            # total and its surplus.
            "expired": expired,
            "duplicates": max(0, len(live_) - 1),
            "unlistable": unlistable}


def roll(explicit=None, live=None, agents=None):
    """The seats this census may speak about, and it is deliberately NOT the
    whole roster.

    Measured on the live fleet the day this landed: the roster carries 190
    rows, 179 of them long-dead `tmp-claude-N` entries. Reporting those as DEAF
    SEATS produced 179 alarm lines for seats nobody was trying to reach —
    which is the same class of lying surface this lane exists to close, just
    inverted: an alarm that fires for everything says nothing, and the ONE seat
    that really was unreachable would have been buried in the noise.

    A DEAF SEAT is a seat that IS HERE and cannot be reached. Presence is the
    fleet's own existing answer to "is this row real" (`presence_of`, the same
    cut `helm chat seats` hides behind --all), and it is stamped by SPEAKING as
    well as by delivery — so a seat that lost its beacon but is still working
    stays present and IS alarmed, while the graveyard drops out.

    An ABSENT seat still enters if it HOLDS A BEACON, because that is the other
    direction of the same alarm: a waiter running for a seat that is gone is
    exactly the ghost this module hunts.

    AND AN ABSENT SEAT THAT RECENTLY ANSWERED STAYS ON THE ROLL (row #96).
    Presence alone SELF-SILENCES this alarm: a seat that goes deaf also stops
    stamping its beat, so QUIET_S later it reads absent and drops out of the
    census — it vanishes from the alarm at exactly the moment it most needs to
    be in it. (The registry rung does not save it: the census PRUNES a dead
    waiter's row on the same pass that reports it, so the seat gets ONE deaf
    line and then silence.) The register is the memory that survives the beat:
    a seat whose attendance row shows a recent OWN-RIGHT appearance
    (`within_grace`) stays censused — and stays alarmed — until it recovers, is
    withdrawn from the roster, or ages past the grace. Keyed on the register's
    own stamps, never on raw last_seen recency: measured on the live roster
    2026-08-03, a last-seen cut would have re-admitted six long-departed
    hand-session rows as six permanent DEAF lines, and an alarm that fires for
    everything says nothing.

    A LIVE PANE is the third additive source, but never a second process scan:
    `census` owns the one agent index and passes it here. Additions are
    roster-intersected, case-normalized, and reject a pane proven to belong to a
    different helm home. A roster session may bind an unstamped pane; the pane
    need not self-declare a seat to prove that the roster row is still live.
    None means the agent census was unreadable and adds nothing — presence,
    attendance grace, and held beacons remain authoritative on their own."""
    from . import seats as seats_mod
    if explicit:
        return [n for n in explicit if valid_seat(n)]
    try:
        rows = seats_mod.roster()
    except Exception:                        # noqa: BLE001
        rows = {}
    canonical = {}
    for name in sorted(rows):
        if valid_seat(name):
            canonical.setdefault(str(name).casefold(), name)
    present, admitted = [], set()

    def admit(name):
        key = str(name).casefold()
        if key not in admitted:
            admitted.add(key)
            present.append(canonical.get(key, name))

    for name, row in sorted(rows.items()):
        if not valid_seat(name):
            continue
        row = row if isinstance(row, dict) else {}
        try:
            seen = seats_mod.last_seen(name, row)
        except Exception:                    # noqa: BLE001
            seen = None
        if seats_mod.presence_of(seen) != "absent":
            admit(name)
            continue
        if within_grace(row.get("attendance")):
            admit(name)
    held = {r.get("seat") for r in entries() if valid_seat(r.get("seat"))}
    for name in sorted(held):
        admit(name)

    if agents is None:
        return present
    by_seat = agents.get("by_seat") or {}
    by_pid = agents.get("by_pid") or {}

    for name in sorted(rows):
        if not valid_seat(name):
            continue
        key = str(name).casefold()
        if any(_pane_in_scope(agents, pid)
               for pid in (by_seat.get(key) or [])):
            admit(name)

    # Session attribution belongs to the roster's complete identity index, not
    # whichever `session` value happens to be newest on one row. Historical
    # sessions may still hold live panes, while a sid remembered by two rows is
    # explicitly UNVERIFIED and must never prove either seat present.
    try:
        owners = seats_mod.session_owners(rows)
    except Exception:                        # noqa: BLE001 — malformed index is unknown
        owners = {}
    for sid, pid in sorted((live or {}).items()):
        names = owners.get(sid) or []
        if len(names) != 1 or pid not in by_pid or not _pane_in_scope(agents, pid):
            continue
        name = names[0]
        if not valid_seat(name):
            continue
        key = str(name).casefold()
        claims = by_pid.get(pid) or []
        if not claims or key in {str(seat).casefold() for seat in claims}:
            admit(name)
    return present


def ownership(rows, homes):
    """WHO ARMED THE FLEET'S LIVE BEACONS -- one model, every surface.

    `live` is the denominator and travels with the fractions: `misrouted`
    names what was PROVEN sidechain-only, `unattributed` counts the live
    beacons no producer stamp attributes, and `reasons` says why, so a
    zero in the first reads as "none is misrouted" only when the second is
    zero too. `home_probe` False means no beacon could be attributed at all.

    TWO DIFFERENT JOINS, AND EACH HAS ITS OWN COUNT (task/2925).
    `seat_joined` is SEAT attribution: live beacons whose seat row names a
    live pane for that seat (`agent` True). The origin fields are ORIGIN
    attribution: whether the main turn or a subagent armed the beacon, read
    from a producer stamp. `unstamped` counts the unattributed beacons that
    no producer ever stamped, and `origin_unstamped` is True when that is
    every one of them -- then the honest word is "unstamped", because no
    stamp producer exists yet, and "unattributed" sent a reader to suspect
    the seat join, which was correct."""
    owners = [o for r in rows for o in (r.get("owners") or ())]
    reasons = {}
    for r in rows:
        for why in r.get("owner_reasons") or ():
            reasons[why] = reasons.get(why, 0) + 1
    unattributed = sum(r.get("unattributed") or 0 for r in rows)
    unstamped = sum(r.get("unstamped") or 0 for r in rows)
    return {
        "live": sum(len(r["live"]) for r in rows),
        "seat_joined": sum(len(r["live"]) for r in rows
                           if r.get("agent") is True),
        "top_level": owners.count(OWNER_MAIN),
        "sidechain": owners.count(OWNER_SUBAGENT),
        "unattributed": unattributed,
        "unstamped": unstamped,
        "origin_unstamped": bool(unattributed) and unstamped == unattributed
                            and OWNER_MAIN not in owners
                            and OWNER_SUBAGENT not in owners,
        "misrouted": [r["seat"] for r in rows if r["verdict"] == MISROUTED],
        "home_probe": homes is not None,
        "reasons": reasons,
    }


def _ownership_of(rep):
    """The report's ownership model, derived by the one builder when a caller
    hands a report that did not come from `census()` -- nothing was attributed
    there, and the model says so rather than a renderer inventing zeros."""
    own = rep.get("ownership")
    return own if own is not None else ownership(rep.get("seats") or [], None)


def census(seats=None, proc_dir=None):
    """The fleet read. Seats come from the ROSTER (and from our own registry),
    never from a process-table sweep — the seats of other projects on this box
    are not this fleet's business and must not appear as its defects."""
    live = live_sessions()
    records = holder_records()
    agents = agent_index(proc_dir)
    # ONE PASS OVER THE CREDENTIAL HOMES FOR THE WHOLE CENSUS, for the same
    # reason `holder_records` takes one: per beacon this re-globs every home.
    homes = session_homes()
    seen, ordered = set(), []
    for name in roll(seats, live=live, agents=agents):
        key = str(name).casefold()
        if key not in seen:
            seen.add(key)
            ordered.append(name)
    # ONE ROSTER READ FOR THE WHOLE CENSUS. The consumption fact is
    # session-keyed, and re-reading the roster per seat would put a different
    # instant on every row of one screen.
    # THE IMPORT IS OUTSIDE THE GUARD ON PURPOSE. A module alias resolved
    # INSIDE a broad `except` turns a NameError into a confident statement
    # about data — measured on this very function, where the alias was bound
    # in another one and every seat's consumption fact silently answered
    # None while the census reported a healthy fleet.
    from . import seats as _seats
    # AND THE READABILITY IS A FLAG, NOT A COMPARISON ON THE MAPPING. The
    # roster shape scanner classifies how this module reaches roster rows and
    # refuses syntax it cannot classify; `roster is not None` is a Compare on
    # the mapping itself, which is exactly the shape it cannot read. A bool
    # beside the mapping says the same thing in a shape the scanner knows.
    readable, roster = True, {}
    try:
        roster = _seats.roster()
    except OSError:                          # unreadable is not empty
        readable, roster = False, {}
    rows = []
    for name in ordered:
        # THREE-VALUED AND CARRIED AS THREE VALUES. A duration is a measured
        # wait; `unread` is a census that could not be taken; and both None
        # with no reason is an EMPTY SAMPLE, which is the one a caller is
        # tempted to read as "drained". They travel together from here to
        # attendance so the recovery rule can tell them apart.
        waited, unread, ev = None, None, _NO_EVIDENCE
        if readable:
            row = roster.get(name)
            row = row if isinstance(row, dict) else {}
            waited, unread, ev = undrained(
                name, session=row.get("session"), room=row.get("home_room"))
        else:
            unread = ("the roster could not be read, so no seat's backlog "
                      "was either")
        rows.append(seat_census(name, live=live, proc_dir=proc_dir,
                                records=records, agents=agents,
                                waited=waited, undrained_unreadable=unread,
                                undrained_evidence=ev, homes=homes))
    return {
        "seats": rows,
        "live_probe": live is not None,
        "agent_probe": agents is not None,
        "covered": [r for r in rows if r["verdict"] == COVERED],
        "waking": [r for r in rows if r["verdict"] == WAKING],
        "deaf": [r for r in rows if r["verdict"] == DEAF],
        "deaf_in_effect": [r for r in rows if r["verdict"] == DEAF_IN_EFFECT],
        "vacant": [r for r in rows if r["verdict"] == VACANT],
        "unproven": [r for r in rows if r["verdict"] == UNPROVEN],
        "misrouted": [r for r in rows if r["verdict"] == MISROUTED],
        # ONE OWNERSHIP MODEL, RENDERED BY EVERY SURFACE. The summary line,
        # the human block, the JSON and the alarm all read this dict; none
        # derives its own count, so they cannot disagree about a beacon.
        "ownership": ownership(rows, homes),
        # The ALARM CLASS, a superset of `deaf`: every seat with no proven wake
        # path, including the UNPROVEN ones no beacon could be proven live for.
        "unreachable": [r for r in rows if unreachable(r)],
        "ghosts": [b for r in rows for b in r["ghosts"]],
        "beacons": sum(len(r["beacons"]) for r in rows),
        "surplus": sum(r["duplicates"] for r in rows),
    }


# ---------------------------------------------------------------------------
# the ATTENDANCE REGISTER — the census writes its verdicts INTO the roster
# ---------------------------------------------------------------------------
#
# The roster records who is DEFINED; until 2026-08-03 nothing recorded who
# ANSWERED, so the morning the fleet went down the integrator reconstructed
# presence seat by seat while a correct census verb sat un-run for four hours.
# The register is that verb's output written onto the ONE sheet that already
# exists: each censused roster row gains an `attendance` field —
#
#   state    the census verdict (covered / WAKING / DEAF / VACANT / UNPROVEN /
#            DEAF-IN-EFFECT / MISROUTED); WAKING never advances `covered`,
#            because a beacon that is re-arming is not a proven answer
#   at       when this pass measured it
#   since    when the CURRENT state began (carried while the state holds)
#   seen     the seat's last presence beat, as this pass measured it
#   covered  the last time a census PROVED the seat answered — a grace stamp,
#            and the "how long unreachable" anchor
#   standing the last time the seat was on the roll ON ITS OWN RIGHT (a live
#            presence beat, or a registered beacon) — the OTHER grace stamp,
#            and the one that keeps an UNPROVABLE seat from self-silencing
#   alarm    did this pass find NO PROVEN WAKE PATH? (`unreachable`)
#   alerted  the last state successfully POSTED to chat (human-readable)
#   alarmed  the alarm value the last successful CHAT post carried — the latch
#   pushed   the alarm value the last successful PHONE push carried — the
#            SECOND latch, because a dead phone must not re-post to a healthy
#            room nor the reverse (proxywatch's per-channel outbox law)
#   why      the census's own reason line
#
# NOT A SECOND SOURCE OF TRUTH, by construction: presence stays the seen-file
# beat (touch_seen's self-attested law is untouched — this module never stamps
# a beat for anybody), and the verdict has exactly one home, this field. The
# register never MINTS a roster row either: a seat nobody enrolled is not
# attendance-tracked.

ATTEND_GRACE_S = 24 * 3600   # how long a starved deaf seat stays on the roll
# after its last OWN-RIGHT appearance: long enough to survive the overnight
# case this lane exists for (the owner woke to a 4h outage no instrument had
# named), short enough that an abandoned row stops alarming by the next day.
# The register keeps the truth (state + since) after the alarm line ages out.

_GRACE_STAMPS = ("covered", "standing")


def within_grace(att, now=None):
    """Does this attendance row still earn a place on the roll without a beat?

    TWO STAMPS, and both are anchored on something the SEAT did rather than on
    something the census did:

      covered   a census PROVED this seat answered.
      standing  the seat qualified for the roll on its own right — a live
                presence beat, or a registered beacon of its own.

    Neither is refreshed by the grace itself, which is the property that keeps
    this from becoming a row that lives forever on its own alarm: a starved
    seat ages off exactly ATTEND_GRACE_S after its last REAL appearance.

    `standing` IS THE FIX FOR THE SELF-SILENCING ROLL (measured 2026-08-03).
    Keying the grace on `covered` alone silenced precisely the seats the
    instruments cannot prove: `attend` only ever advances `covered` on a
    COVERED verdict, so a seat that reads UNPROVEN never earns a grace stamp
    at all, and ~QUIET_S after its beat stopped it dropped off the census
    entirely — no line, no register row, no alarm, for the seats a mixed fleet
    can least afford to lose track of (the non-claude families, whose session
    liveness helm cannot prove even when they are healthy). The bug survived
    exactly where the instruments were weakest.

    A junk stamp still never mints a row: it is skipped, not trusted."""
    if not isinstance(att, dict):
        return False
    ref = 0.0
    for key in _GRACE_STAMPS:
        try:
            ref = max(ref, float(att.get(key) or 0))
        except (TypeError, ValueError):
            continue                         # a junk stamp never mints a row
    return ref > 0 and (time.time() if now is None else now) - ref \
        < ATTEND_GRACE_S


def standing_now(seat, seen, held):
    """Is this seat on the roll ON ITS OWN RIGHT this pass? ONE spelling of
    `roll`'s two own-right paths (a non-absent presence beat, or a registered
    beacon), so the stamp `within_grace` reads can never mean something
    different from the admission it is meant to remember. `held` contains
    canonical seat keys, matching chat's casefold identity law."""
    from . import seats as seats_mod
    return seats_mod.presence_of(seen) != "absent" or _seat_key(seat) in held


def _roster_readable():
    """(True, None) or (False, why) — is the roster file actually READABLE?

    `seats.roster()` is `pk.read_json(path, {}) or {}` and `pk.read_json`
    swallows every exception, so a CORRUPT roster returns {} — byte-identical
    to an empty one. The attendance pass then finds no seats, raises no alert,
    and exits 0: a silent no-op that looks exactly like a healthy fleet with
    nothing to report. A cross-family read found it, and it is the same
    could-not-look-recorded-as-a-fact class found in proxywatch's
    _read_watch_state (a corrupt outbox read as {} and a queue with pending
    alerts delivered nothing).

    ABSENT IS NOT CORRUPT. A roster that has never been written is a legitimate
    first-run state and must stay silent; only a file that EXISTS and will not
    parse is the failure worth refusing on.

    Local rather than shared ON PURPOSE, for now: the durable form is a
    tri-state `pk.read_json_checked` that proxywatch, gate and this all use,
    and swapping a core reader late is a wider change than this row's finding
    justifies. Named here so the follow-up is findable."""
    import json
    from . import seats as seats_mod
    path = seats_mod.roster_path()
    try:
        with pk.open_regular(path, encoding="utf-8") as f:
            json.load(f)
    except FileNotFoundError:
        return True, None            # never written yet — a real empty
    except (OSError, ValueError) as exc:
        return False, "roster unreadable (%s: %s)" % (type(exc).__name__, exc)
    return True, None


def attend(rep, now=None):
    """Write one census's verdicts under the escalation then roster locks.
    Returns {"written": [seats], "transitions": [(seat, att, prior)],
    "error": None | reason} — a transition is a DEAF EDGE (a seat entering or
    leaving DEAF relative to the last state successfully posted), detected
    here because the latch lives on the row being written."""
    from . import chat, seats as seats_mod
    now = float(now if now is not None else time.time())
    out = {"written": [], "transitions": [], "error": None}
    try:
        chat._ensure_dir()
        # A CORRUPT ROSTER MUST NOT READ AS AN EMPTY FLEET. Checked BEFORE the
        # lock, because refusing early costs nothing and the lock is contended.
        ok, why = _roster_readable()
        if not ok:
            out["error"] = "%s — attendance pass SKIPPED, not silently empty" % why
            return out
        held = {_seat_key(r["seat"]) for r in entries()
                if valid_seat(r.get("seat"))}
        path = seats_mod.roster_path()
        # One order everywhere: escalation -> roster. `escalate` already holds
        # the outer lock while it revalidates, delivers and acks; attendance
        # must enter that SAME serialization domain or it can commit a newer
        # verdict after revalidation but before the old sentence leaves.
        with seats_mod._flocked(path + ".escalate.lock") as edge_lock:
            if edge_lock.f is None:
                out["error"] = ("escalation lock unavailable — attendance "
                                "pass SKIPPED rather than crossing delivery")
                return out
            with seats_mod._flocked(path + ".lock") as lock:
                # _flocked FAILS OPEN BY DESIGN: on OSError it sets .f = None
                # and still returns, so `with` alone acquires nothing and the
                # body runs UNLOCKED. REFUSE, never degrade: a skipped pass
                # self-heals, while a clobbered roster does not.
                if lock.f is None:
                    out["error"] = ("roster lock unavailable — attendance "
                                    "pass SKIPPED rather than writing unlocked")
                    return out
                r = seats_mod.roster()
                roster_names = {_seat_key(name): name for name in r
                                if valid_seat(name)}
                for crow in rep["seats"]:
                    # The roster owns enrolled display casing. A census may be
                    # explicitly requested with a case variant, or inherit a
                    # legacy beacon row's spelling; both still name this row.
                    seat = roster_names.get(_seat_key(crow["seat"]))
                    row = r.get(seat) if seat is not None else None
                    if not isinstance(row, dict):
                        continue             # the register never mints a row
                    prior = row.get("attendance")
                    prior = prior if isinstance(prior, dict) else {}
                    state = crow["verdict"]
                    # AN UNPROVEN RECOVERY IS NOT A RECOVERY. The alarm was
                    # raised about ONE row; only that row being read out of
                    # its own room clears it. A failed census, or a rotation
                    # that never reached the room, would otherwise announce
                    # "answers again", drop the alarm, and re-raise it next
                    # pass the sample came back — alarm, false recovery,
                    # alarm, with the backlog untouched throughout.
                    owed = prior.get("undrained_row")
                    drained = None
                    if (prior.get("state") == DEAF_IN_EFFECT
                            and state == COVERED):
                        drained = drain_proven(owed, crow)
                    if drained is not None and not drained.proven:
                        state = DEAF_IN_EFFECT
                        held_row = dict(crow, verdict=DEAF_IN_EFFECT,
                                        why=_held_why(drained))
                        # THE REPORT MUST CARRY THE HELD VERDICT, not only
                        # the register. Every downstream reader — the render
                        # the operator sees, and `escalate`'s repair leg —
                        # reads `rep`'s buckets, so a hold that lived only in
                        # the attendance row would be a verdict nobody could
                        # see and no repair would be attempted for.
                        _rebucket(rep, crow, held_row)
                        crow = held_row
                    alarm = unreachable(crow)
                    try:
                        seen = seats_mod.last_seen(seat, row)
                    except Exception:        # noqa: BLE001
                        seen = prior.get("seen")
                    att = {"state": state, "at": now,
                           "since": (prior.get("since") or now)
                                    if prior.get("state") == state else now,
                           "seen": seen,
                           "covered": now if state == COVERED
                                      else prior.get("covered"),
                           "standing": now if standing_now(seat, seen, held)
                                       else prior.get("standing"),
                           "alarm": alarm,
                           # CARRIED SO THE DELIVERY LEG CAN SAY IT. The nudge
                           # tells the seat how long its rows have waited, and
                           # re-deriving that at delivery would measure a
                           # different instant than the verdict was taken on.
                           "waited": crow.get("waited"),
                           # THE ROW THE ALARM IS ABOUT. Recovery is a claim
                           # about THIS row, so it is carried rather than
                           # re-derived: a later pass that cannot see it also
                           # cannot name it.
                           # THE CURRENT OLDEST WHEN THERE IS ONE, AND THE
                           # CARRIED ROW OTHERWISE — a HELD alarm has an empty
                           # sample by definition, so re-deriving here would
                           # erase the identity the next pass needs to clear.
                           "undrained_row": (
                               ((crow.get("undrained_evidence")
                                 or _NO_EVIDENCE).get("oldest") or owed)
                               if state == DEAF_IN_EFFECT else None),
                           "alerted": prior.get("alerted"),
                           "alarmed": bool(prior.get("alarmed")),
                           "pushed": bool(prior.get("pushed")),
                           # CARRIED LIKE THE LATCHES, and for the same
                           # reason: this row is rebuilt from scratch every
                           # pass, so an episode left out of it is an episode
                           # ERASED — and a pane repair whose record is wiped
                           # each pass is repeated on every pass, which is
                           # exactly what having an episode was for.
                           "repair": _settle_rearm(prior.get("repair"),
                                                   state, now),
                           "why": crow["why"]}
                    row["attendance"] = att
                    r[seat] = row
                    out["written"].append(seat)
                    # A transition is an edge on EITHER channel: the chat room
                    # may already carry an alarm the phone never received, and
                    # that seat must stay in the batch until BOTH have it.
                    if alarm != att["alarmed"] or alarm != att["pushed"]:
                        out["transitions"].append((seat, att, prior))
                if out["written"]:
                    pk.write_json(path, r)
    except Exception as exc:                 # noqa: BLE001 — the caller turns
        out["error"] = "%s: %s" % (exc.__class__.__name__, exc)
    return out                               # this into exit 2, never a raise


def _settle_rearm(ep, state, now):
    """The repair episode `attend` carries forward, with an open RE-ARM
    episode settled once its seat reads COVERED (task/2925).

    A re-arm nudge is owed for one DEAF spell. When the seat answers again
    the obligation is met whatever the child reported, so the latch closes
    here rather than waiting on a child that may never report. Only a
    re-arm episode is touched: a DEAF-IN-EFFECT episode's recovery rule is
    `drain_proven`, and covered alone does not discharge it."""
    if state != COVERED or not isinstance(ep, dict) \
            or ep.get("kind") != "rearm" \
            or ep.get("outcome") in _REPAIR_SETTLED:
        return ep
    return dict(ep, outcome="covered", closed_at=now,
                detail="the seat answers again: its beacon is live")


def _ack_alerts(rows, chat_leg=False, push_leg=False):
    """Advance the escalation latch of the channel that just DELIVERED.
    Deliberately a SECOND write after delivery rather than part of `attend`'s:
    a latch may only move once its own post landed, so a failed delivery
    re-detects the same edge next pass (at-least-once, proxywatch's outbox law
    in two fields). PER CHANNEL, because they fail independently — a dead phone
    must not re-post to a healthy room, nor the reverse. Best-effort: a failed
    ack merely re-posts an edge, it never loses one."""
    from . import chat, seats as seats_mod
    try:
        chat._ensure_dir()
        with seats_mod._flocked(seats_mod.roster_path() + ".lock") as lock:
            # Same fail-open helper, same refusal. An ack that writes unlocked
            # can clobber a concurrent attendance transition, and the docstring
            # above promises "ack merely re-posts an edge, it never loses one"
            # — a promise only the lock can keep.
            if lock.f is None:
                return False
            r = seats_mod.roster()
            hit = False
            for seat, att, _prior in rows:
                row = r.get(seat)
                cur = row.get("attendance") if isinstance(row, dict) else None
                if not isinstance(cur, dict):
                    continue
                if chat_leg:
                    cur["alerted"] = att["state"]
                    cur["alarmed"] = bool(att["alarm"])
                if push_leg:
                    cur["pushed"] = bool(att["alarm"])
                hit = True
            if hit:
                pk.write_json(seats_mod.roster_path(), r)
    except Exception:                        # noqa: BLE001
        pass


#: THE OBLIGATION IS DISCHARGED. `drained` discharges it without anything
#: reaching the pane -- there is nothing left to nudge about -- and `typed` is
#: the CHILD reporting that the keystroke actually landed.
#:
#: `wake` IS NOT HERE, and that is the point. The parent records `wake` when it
#: has SPAWNED the child, which is an attempt and not an outcome: delivery can
#: be paused during the child's settle interval, the child then refuses, and
#: the refusal updates resume-state while ATTENDANCE already reads settled. The
#: pause lifts and the same spell can never retry. Only the child knows whether
#: anything was typed, so only the child may close the episode.
#:
#: task/2463 finding 3 removed two members of this tuple, and the reason is the
#: same for both. `paused` is a REFUSAL BEFORE THE ACT -- delivery to the seat
#: is held behind a credential wall, so the backlog stays owed and the alarm
#: stands, which the refusal's own sentence says out loud while the latch
#: recorded it as done. `would-wake` is a DRY RUN; it types nothing. Either one
#: closed the episode against the spell that earned it, so when the pause
#: lifted the same spell could never retry, and the repair was owed forever
#: with nothing left to say about it.
#:
#: `covered` closes a RE-ARM episode (task/2925): the DEAF seat it was opened
#: for answers again, so the pass that sees it covered settles the latch.
_REPAIR_SETTLED = ("typed", "drained", "covered")

#: ATTEMPTED, AWAITING THE CHILD'S OWN REPORT. Recorded so a reader can tell an
#: attempt from an error; not terminal, so the spell retries if nothing lands.
_REPAIR_ATTEMPTED = ("wake",)

#: OUTCOMES THAT ARE NOT FAILURES AND ARE NOT COMPLETIONS. A repair that was
#: refused before it acted is retried on a later pass -- the condition that
#: refused it is re-read at the actuator every time, so a still-paused seat
#: simply refuses again and types nothing. They are named rather than folded
#: into "anything not settled" so a reader can tell a deferral from an error.
#: "withheld" IS THE UNKNOWN: the child could not re-read the backlog at the
#: act, so it typed nothing AND settled nothing. The obligation stands; what
#: it lacks is positive authority to press a key, and those are different.
#: "stale" IS A MINT THAT OUTLIVED ITS LAUNCH WINDOW: the child refused to
#: launch it at all, so the next pass mints a fresh attempt with its own birth.
_REPAIR_DEFERRED = ("paused", "would-wake", "withheld", "stale")


def _rebucket(rep, was, now_row):
    """Move one census row between `rep`'s verdict buckets, in place.

    `roll` builds the buckets by filtering `seats`, so a verdict decided LATER
    — attendance holding an unproven recovery — has to be reflected here or
    the report and the register disagree about the same seat."""
    seats = rep.get("seats")
    if isinstance(seats, list):
        for i, row in enumerate(seats):
            if row is was:
                seats[i] = now_row
                break
    # IDENTITY, NEVER EQUALITY. These are plain dicts and `in`/`remove` would
    # compare by value, so two rows that happened to agree field-for-field
    # would let this drop the wrong one.
    def _swap(bucket, keep):
        if not isinstance(bucket, list):
            return
        bucket[:] = [row for row in bucket if row is not was]
        if keep and not any(row is now_row for row in bucket):
            bucket.append(now_row)

    for key, verdict in (("covered", COVERED), ("waking", WAKING),
                         ("deaf", DEAF),
                         ("vacant", VACANT), ("unproven", UNPROVEN),
                         ("deaf_in_effect", DEAF_IN_EFFECT)):
        _swap(rep.get(key), now_row["verdict"] == verdict)
    _swap(rep.get("unreachable"), unreachable(now_row))


def _repair_due(att):
    """Is a pane repair OWED for the spell this attendance row describes?

    ONE SPELL, ONE SETTLED REPAIR. `since` names the current DEAF-IN-EFFECT
    spell, so an episode recorded against a different `since` belongs to an
    older one and obliges nothing. An episode whose outcome never reached the
    pane — the parent could not prove it, the fork failed — is NOT settled and
    is retried; without that, a failure followed by two successful
    notifications would leave the repair permanently owed and never attempted
    again, because the latches would have nothing left to say.

    AND A DEFERRAL IS NOT A COMPLETION. A repair refused before it acted —
    a paused seat, a dry run — leaves the obligation exactly where it was, so
    it is due again. That costs nothing while the condition holds, because the
    actuator re-reads it and refuses again without typing, and it is the only
    way the same spell can be repaired once the pause lifts."""
    ep = att.get("repair")
    if not isinstance(ep, dict):
        return True
    if ep.get("since") != att.get("since"):
        return True
    return ep.get("outcome") not in _REPAIR_SETTLED


def install_repair(seat, att, now, detail="", kind=None):
    """Open the repair episode BEFORE the attempt, and return the SPELL it
    belongs to — or None when it could not be installed.

    THE EPISODE HAS TO EXIST BEFORE THE FORK OR NOTHING CAN BIND TO IT. The
    episode is opened here rather than after the whole batch returns, because
    a later open leaves two writers arguing over "the current spell": a child
    that finishes early finds no episode to close and returns False, and the
    parent's later write lands on top of a result the child produced. A
    spell that is installed first gives both writers the same name for the
    same thing, and the name is `since` — the attendance stamp the whole
    DEAF-IN-EFFECT ladder already treats as the spell's identity.

    Installing an ATTEMPTED outcome rather than nothing is deliberate: a crash
    between here and the child leaves an episode that `_repair_due` still
    considers open, so the next pass retries.

    THE IDENTITY IS THE ATTEMPT, NOT THE SPELL, and that distinction is the
    whole of this function. One DEAF-IN-EFFECT spell can earn SEVERAL repair
    attempts -- a child that never typed leaves the episode open and the next
    pass launches another -- so a name that says only WHICH SPELL lets a late
    child from attempt one close attempt two, and lets a charge receipt keyed
    on it suppress every later attempt's charge. Unlimited uncharged launches
    from a reused receipt is the second failure and the quieter one. The spell
    stays its own field, because the ladder's due and retry logic is about the
    spell; the ATTEMPT is what crosses the fork.

    `kind` names the leg when it is not the DEAF-IN-EFFECT turn nudge:
    "rearm" is a DEAF seat's re-arm nudge, the one episode that `attend`
    settles when the seat reads covered again.

    AND THE ATTEMPT IS BORN HERE. `born` is the mint instant, written once
    beside the id and never derived from it or from the spell: the spell can
    be hours old while this attempt is seconds old, and a launch window or a
    charge judged on the spell's age misjudges every retry of a long spell.
    Only the CURRENT attempt is kept, so the record is bounded by one per
    seat, and an id this episode no longer names is retired for good."""
    from . import pk, seats as seats_mod
    spell = att.get("since") if isinstance(att, dict) else None
    if spell is None:
        return None
    try:
        with seats_mod._flocked(seats_mod.roster_path() + ".lock") as lock:
            if lock.f is None:
                return None                  # the next pass re-derives it
            r = seats_mod.roster()
            row = r.get(seat)
            cur = row.get("attendance") if isinstance(row, dict) else None
            if not isinstance(cur, dict) or cur.get("since") != spell:
                return None                  # the register moved under us
            prior = cur.get("repair")
            # THE ELIGIBILITY IS RE-DERIVED HERE, NOT TRUSTED FROM THE CALLER'S
            # SNAPSHOT. `escalate` decides "due" against the roster it read at
            # the top of the pass; the child can settle this very spell in the
            # meantime, and an install that checks only "same spell" then
            # REPLACES a terminal `typed` with a fresh `wake` -- reopening a
            # repair that already succeeded. A settled spell is not reopened
            # by anyone; a new spell (a moved `since`) is the only way back.
            if isinstance(prior, dict) and prior.get("since") == spell \
                    and prior.get("outcome") in _REPAIR_SETTLED:
                return None                  # already settled; nothing to open
            n = 1
            if isinstance(prior, dict) and prior.get("since") == spell:
                try:
                    n = int(str(prior.get("attempt") or "").rsplit("#", 1)[-1]) + 1
                except (TypeError, ValueError):
                    n = 2                    # an unreadable prior is still a prior
            attempt = "%s#%d" % (spell, n)
            cur["repair"] = {"at": now, "since": spell, "attempt": attempt,
                             "born": now, "outcome": "wake", "detail": detail}
            if kind:
                cur["repair"]["kind"] = kind
            pk.write_json(seats_mod.roster_path(), r)
            return attempt
    except Exception:                        # noqa: BLE001 — an uninstalled
        return None                          # episode retries, never wedges


def settle_repair(seat, outcome, detail="", attempt=None):
    """Close the standing repair episode for `seat` — THE CHILD'S OWN REPORT.

    The parent can only say it SPAWNED something. This is the other half: the
    detached deliverer calls it once it knows whether a keystroke landed, so
    the episode is closed against the spell that earned it by the only party
    that can see the outcome. An unreachable roster or a lost lock simply
    leaves the episode open, which retries — the safe direction.

    `attempt` IS THE EPISODE THIS CHILD WAS LAUNCHED FOR, carried across the
    fork. Without it a delayed child closes whatever episode happens to be
    current when it lands: child A delivers for attempt one, is held up,
    another attempt is installed or the attendance moves on, and A's report
    closes THAT. No timestamp collision is required, only a delay -- and
    naming the SPELL alone is not enough, because one spell earns SEVERAL
    attempts and a late child's spell still matches. A comparison against the
    stored episode alone rejects a stale RECORD and says nothing about the
    CALLER, which is the half that was missing.

    THE WHOLE DECISION IS INSIDE ONE LOCK. Caller identity, current
    attendance, the installed attempt and the permitted transition are read
    and decided in the same serialization domain that performs the write; a
    terminal check made outside it is a check against a state that can change
    before the write lands. SAME-ATTEMPT WRITES ARE MONOTONIC: a terminal
    outcome is never replaced by a later non-terminal one, whoever writes it.

    A caller that names no attempt is a legacy caller and keeps the old
    behaviour rather than being refused."""
    from . import pk, seats as seats_mod
    try:
        with seats_mod._flocked(seats_mod.roster_path() + ".lock") as lock:
            if lock.f is None:
                return False
            r = seats_mod.roster()
            row = r.get(seat)
            att = row.get("attendance") if isinstance(row, dict) else None
            if not isinstance(att, dict):
                return False
            ep = att.get("repair")
            if not isinstance(ep, dict) or ep.get("since") != att.get("since"):
                return False                 # a different spell; not ours
            if attempt is not None and ep.get("attempt") != attempt:
                return False                 # not the attempt this child ran
            # AND NO NAME CANNOT CLOSE A NAME. A legacy or manual deliverer
            # that carries no attempt is not the child this episode was opened
            # for, so it may not settle it: that is the unbound child closing
            # the bound one's episode, the same defect from the other side.
            # It keeps the old behaviour only on an episode that itself has
            # no attempt, which is the only episode it could have run for.
            if attempt is None and ep.get("attempt"):
                return False
            if ep.get("outcome") in _REPAIR_SETTLED \
                    and outcome not in _REPAIR_SETTLED:
                return False                 # monotonic: terminal never regresses
            ep["outcome"] = outcome
            ep["detail"] = detail or ep.get("detail")
            ep["closed_at"] = time.time()
            pk.write_json(seats_mod.roster_path(), r)
            return True
    except Exception:                        # noqa: BLE001 — open retries
        return False


def _record_repairs(repaired, now):
    """Write each repair's OUTCOME onto its attendance row.

    A SECOND WRITE AFTER THE ACT, for the same reason the channel latches
    take one: an episode may only be marked settled once its own attempt
    returned, so a crash between the attempt and this write re-attempts rather
    than silently closing the obligation."""
    from . import pk, seats as seats_mod
    try:
        with seats_mod._flocked(seats_mod.roster_path() + ".lock") as lock:
            if lock.f is None:
                return                       # the next pass re-derives it
            r = seats_mod.roster()
            hit = False
            for seat, att, got in repaired:
                row = r.get(seat)
                cur = row.get("attendance") if isinstance(row, dict) else None
                if not isinstance(cur, dict):
                    continue
                if cur.get("since") != att.get("since"):
                    continue                 # the register moved on; not ours
                ep = cur.get("repair")
                if isinstance(ep, dict) \
                        and ep.get("since") == att.get("since") \
                        and ep.get("outcome") in _REPAIR_SETTLED:
                    # MONOTONIC, and decided inside the same lock as the
                    # write: a terminal result is never replaced by an
                    # attempt. The child settles the attempt it was launched
                    # for and can finish while this batch is still walking
                    # other seats, so an unconditional write here replaced a
                    # real `typed` with the parent's `wake` and the repair
                    # became retryable after it had already succeeded.
                    continue
                # THIS WRITE IS ABOUT ONE ATTEMPT, AND IT SAYS WHICH. A result
                # that names the attempt it ran for may record against that
                # attempt and no other: a refusal that opened nothing (the
                # actuator refused before it installed) carries no attempt
                # and must not land on whatever episode is current, because
                # that episode is somebody else's running child. The one
                # attempt-less outcome with standing is a PROVEN drain, which
                # is a fact about the SPELL -- and even that waits while an
                # attempt is in flight, since that child will read the same
                # drain and settle itself.
                mine = got.get("attempt")
                have = ep.get("attempt") if isinstance(ep, dict) else None
                if mine is not None:
                    if have != mine:
                        continue             # not the episode this result ran
                elif got.get("action") != "drained" \
                        or (isinstance(ep, dict)
                            and ep.get("since") == att.get("since")
                            and ep.get("outcome") in _REPAIR_ATTEMPTED):
                    continue                 # a refusal opened nothing to write
                # THE BIRTH SURVIVES THE OUTCOME. This write is about the
                # same attempt, so it keeps that attempt's `born`; a record
                # rebuilt without it would read as an attempt of unknown
                # birth, which the child refuses to launch.
                cur["repair"] = {"at": now, "since": att.get("since"),
                                 "attempt": have if mine is None else mine,
                                 "outcome": got.get("action"),
                                 "detail": got.get("detail")}
                if isinstance(ep, dict) and "born" in ep \
                        and ep.get("attempt") == cur["repair"]["attempt"]:
                    cur["repair"]["born"] = ep["born"]
                # THE LEG SURVIVES TOO, or a re-arm episode rewritten here
                # reads as a turn nudge and `attend` never settles it.
                if isinstance(ep, dict) and ep.get("kind") \
                        and ep.get("since") == att.get("since"):
                    cur["repair"]["kind"] = ep["kind"]
                hit = True
            if hit:
                pk.write_json(seats_mod.roster_path(), r)
    except Exception:                        # noqa: BLE001 — an unrecorded
        pass                                 # outcome retries, never wedges


def _alarm_line(seat, att):
    ref = att.get("covered") or att.get("seen")
    how_long = ("unreachable for %s" % _age(ref)) if ref \
        else "never seen answering"
    if att["state"] == DEAF_IN_EFFECT:
        # THE VERDICT'S SENTENCE, ONCE — the same duplication the census line
        # carried: `why` already opens with the live-wake-path and seat-home
        # clauses, so a lead-in restating them said each twice.
        return ("DEAF-IN-EFFECT SEAT %s — %s. Every liveness surface reads "
                "healthy and the delivery still did not become a turn, so "
                "re-arming the beacon fixes nothing: the pane itself has to "
                "be made to take a turn (`%s`)."
                % (label(seat), att.get("why") or "?",
                   repair_argv(label(seat))))
    if att["state"] == DEAF:
        return ("DEAF SEAT %s — %s: no live beacon, so HELM cannot wake it. "
                "It must re-arm `helm chat wait --seat %s --follow` to be "
                "reachable here; a wake leg outside helm is not something this "
                "census can see." % (label(seat), how_long, label(seat)))
    return ("UNREACHABLE SEAT %s — %s: %s. No beacon could be PROVEN live and "
            "the seat could not be classified, so this is a POSSIBLE outage "
            "the instruments could not name — not a clean bill. A family whose "
            "session liveness helm cannot prove reads exactly like this while "
            "it is down." % (label(seat), how_long, att.get("why") or "?"))


PUSH_NAME_CAP = 6


def _push_body(rows):
    """The PHONE body: one line, lock-screen shaped, seats named.

    A push is read in one glance in the dark. It carries the count, the names
    (capped — a seven-seat outage must not arrive as a wall of text) and
    nothing else; the fleet room carries the full report for whoever is left
    to read it."""
    hit = [label(seat) for seat, att, _ in rows if att["alarm"]]
    back = [label(seat) for seat, att, _ in rows if not att["alarm"]]
    parts = []
    for names, what in ((hit, "UNREACHABLE"), (back, "reachable again")):
        if not names:
            continue
        shown = ", ".join(names[:PUSH_NAME_CAP])
        if len(names) > PUSH_NAME_CAP:
            shown += " +%d more" % (len(names) - PUSH_NAME_CAP)
        parts.append("%d seat%s %s (%s)" % (len(names),
                                            "s"[:len(names) != 1], what, shown))
    body = "helm fleet: " + "; ".join(parts)
    if hit:
        return body + " — helm cannot wake them, and the fleet room cannot " \
                      "tell you: its readers are the seats."
    return body + "."


def escalate(transitions, rep, now=None):
    """Deliver the reachability edges on BOTH channels the owner has, and
    return {"chat": bool, "push": bool, "alarms": int}.

    THE ROOM IS NOT ENOUGH, and that is what the 2026-08-03 replay proved. The
    fleet room is the right surface for the fleet: naming each seat and HOW
    LONG it has been unreachable (anchored on its last proven answer, not on
    when this pass happened to notice). But at 03:44 EVERY READER OF THAT ROOM
    WAS ONE OF THE SEVEN SEATS THAT HAD JUST DIED, and the owner was asleep, so
    a perfect alarm fired into an empty room and the outage still ran four
    hours. An alarm about the fleet being unreachable must not depend on a
    fleet member being reachable — so the same edge also goes to the owner's
    phone through `notify.owner_push`, the channel he already reads and the
    only helm surface that survives every seat on the box dying at once.

    Change-latched PER CHANNEL: one post per edge per channel, each latched
    only after its own delivery, so a failed push re-pushes without re-posting
    (and vice versa).

    ONE ESCALATION AT A TIME, AND THE BATCH IS RE-PROVEN WHERE IT IS SPENT
    (a6d5d95f's remaining finding: "concurrent passes deliver same edge
    twice"). Before the shared outer lock, `attend` derived under only the
    roster lock while delivery ran outside its domain, so a second --post pass
    starting inside the first's attend-to-ack window re-derived the same edge
    and the owner heard every alarm twice on both channels (measured on this
    tree: 2 posts + 2 pushes for one edge). Serializing only deliveries would
    not cure it — a stale batch would simply deliver after the lock freed — so
    the batch must DIE at delivery time if its edge already landed: one lock
    spans revalidate ->
    deliver -> ack, and `attend` enters that SAME outer lock before committing
    a newer verdict, so truth cannot change after the check but before the old
    sentence leaves. Each row is re-checked against the roster's CURRENT latch.
    A row whose latch already carries its alarm value was delivered by a
    concurrent pass and collapses; a row whose delivery FAILED kept an
    un-advanced latch and survives revalidation, so at-least-once still holds
    per channel. A lock that cannot even be OPENED is refused, never raced
    (the same fail-closed polarity as `attend`'s roster lock).

    AND THE LATCH IS ONLY HALF THE REVALIDATION — the opposite edge is the
    other half (an amendment to the bound review of the delivery-edge
    fix above, reproduced on this tree before the cure in BOTH polarities).
    Re-checking ONLY the latches asks "has anyone delivered this yet?" and
    never "is this still TRUE?", so a batch that outlives its own verdict
    delivers a statement about a world that no longer exists. Measured, in
    both polarities: pass A derives alarm=True; the seat RECOVERS; pass B
    writes covered/alarm=False and derives NO edge (the latches are still
    false, so nothing is owed); A then passes latch
    revalidation unchanged and posts DEAF SEAT alpha ... helm cannot wake it
    onto a covered row, acking alarmed/pushed=True (observed: recovery
    transitions 0, stale chat=1 push=1). The mirror is worse — a stale
    RECOVERY batch delivered onto a row that has since gone DEAF again told
    the owner's phone "helm fleet: 1 seat reachable again" while the seat was
    down, and reset BOTH latches to false.

    So a row must also still BE what it says: its current attendance is
    re-read and the row collapses unless the register still records the same
    (alarm, state) the batch carries. `state` is checked as well as `alarm`
    because a DEAF->UNPROVEN drift keeps the alarm polarity while flipping
    the sentence between "helm cannot wake it" and "the instruments could not
    name it" — and at equal polarity the latch advances, so that wording is
    never corrected by a later edge. And `alarm` is checked as well as
    `state`, because the two are independent: UNPROVEN carries EITHER
    polarity (no live beacon is an alarm; a LIVE beacon behind a pane that
    declares no seat of its own is a working wake path that merely cannot be
    classified), so a row can hold its state and lose its alarm.

    Nothing is lost by collapsing: OWEDNESS IS A PROPERTY OF THE ROW, not of
    the batch. `attend` re-derives an edge on every pass from (current alarm
    vs alarmed) and (current alarm vs pushed), so a genuinely undelivered
    edge is re-derived by the next census — while
    a retried FAILED leg still survives, because a retry's row carries the
    same (alarm, state) the register holds. An absent or unreadable
    attendance row cannot contradict anything and DELIVERS, the same
    duplicate-beats-a-drop polarity the latch read already takes."""
    out = {"chat": True, "push": True, "alarms": 0}
    # THE REPAIR LEG IS NOT A NOTIFICATION LEG, so it does not share this
    # gate. `transitions` is the edge on the chat/phone LATCHES, and a seat
    # that was already DEAF and alarmed on both channels moves to
    # DEAF-IN-EFFECT without producing one — which is precisely the seat that
    # has just acquired a repair nobody has attempted.
    die = [row for row in (rep or {}).get("deaf_in_effect") or ()]
    # AND A DEAF SEAT WHOSE PANE IS A DECLARED AGENT (task/2925). Its waiter
    # died and nothing re-armed it; the pane is alive and is the only party
    # that can arm a beacon, so it gets the same bounded nudge with a
    # different sentence -- but only while it OWES something (the actuator
    # asks; canon ping-silent-while-owing-never-wake-clean-idle). `agent` True only: None is a census that could not
    # name a pane (a dead roster row reads exactly so) and nothing is typed on
    # a guess. And DEAF only: a WAKING seat (task/3055) is inside its re-arm
    # grace after a lease expiry, which is its ordinary check-in, so it is
    # never in this list and never typed into. It joins it only if the grace
    # passes and the census reads it DEAF.
    mute = [row for row in (rep or {}).get("deaf") or ()
            if row.get("agent") is True]
    if not transitions and not die and not mute:
        return out
    from . import chat, notify, seats as seats_mod
    now = float(now if now is not None else time.time())
    # The DERIVED count, which only the refusal path below ever returns (it
    # delivers nothing, so it reports the batch it was handed). Revalidation
    # replaces this with the count that actually reached a channel.
    out["alarms"] = sum(1 for _s, att, _p in transitions if att["alarm"])
    with seats_mod._flocked(
            seats_mod.roster_path() + ".escalate.lock") as lock:
        if lock.f is None:
            # Contention is not what lands here — the lock BLOCKS; only a
            # lock file that cannot be opened at all does. Deliver nothing:
            # the latches stay un-advanced, so the next pass re-derives the
            # same edges and nothing is lost — while delivering unserialized
            # is exactly the double-alarm this lock exists to stop.
            out["chat"] = out["push"] = False
            out["refused"] = ("escalation lock unavailable — delivery "
                              "SKIPPED rather than racing a concurrent pass")
            return out
        # THE REVALIDATION. `roster()` swallows corruption into {} — for an
        # ALARM path that polarity is deliberate: an unreadable latch reads
        # as un-advanced and the row DELIVERS (a duplicate alarm beats a
        # dropped one; attend refuses corrupt rosters long before here).
        r = seats_mod.roster()

        def _att(seat):
            row = r.get(seat)
            att = row.get("attendance") if isinstance(row, dict) else None
            return att if isinstance(att, dict) else None

        def _latch(seat, field):
            att = _att(seat)
            return bool(att.get(field)) if att is not None else False

        def _still_true(row):
            """Does the register STILL record the verdict this row carries?
            A batch that outlived its own verdict describes a world that is
            gone; the row collapses and the next census re-derives whatever
            is genuinely still owed."""
            att = _att(row[0])
            if att is None:
                return True              # cannot contradict -> deliver
            return (bool(att.get("alarm")) == bool(row[1]["alarm"])
                    and att.get("state") == row[1]["state"])
        fresh = [t for t in transitions if _still_true(t)]
        # THE ONE VERDICT WHOSE REPAIR IS NOT A MESSAGE. Every other alarm
        # here asks a READER to act; a DEAF-IN-EFFECT seat is the case where
        # the reader is the seat itself and it is not reading. Its wake path
        # is live, so re-arming fixes nothing — the pane has to be made to
        # take a turn.
        #
        # ON THE REVALIDATED EDGE, NEVER THE CENSUS. `fresh` is the set that
        # survived both latch and verdict re-checks, so this fires once per
        # transition rather than once per pass, and a seat that recovered
        # between derivation and delivery is not typed into at all. The nudge
        # keeps its OWN debounce besides, because a latch and a cooldown
        # answer different questions.
        # THE PANE REPAIR'S OWN EPISODE. Derived from the CENSUS and the
        # stored repair state, never from `fresh`: a notification latch
        # answers "has the room been told", and this leg needs "has the pane
        # been made to take a turn", which no channel receipt reports. The
        # episode is keyed by the attendance's `since`, so one DEAF-IN-EFFECT
        # spell earns one SUCCESSFUL repair; a FAILED one is retried, because
        # a parent that could not prove the pane and then saw both
        # notifications land would otherwise lose the repair forever. Rate is
        # not this leg's question — `wake_undelivered` keeps its own debounce
        # and cap — OBLIGATION and OUTCOME are.
        # THE ROSTER'S OWN SPELLING, resolved HERE rather than in a helper:
        # the shape scanner bounds what may read this mapping and cannot
        # prove what a callee handed the whole roster would do with it, so
        # passing `r` to a function is itself the escape it refuses.
        roster_names = {_seat_key(name): name for name in r}
        repaired = []
        for crow in die:
            seat = roster_names.get(_seat_key(crow.get("seat")))
            att = _att(seat) if seat else None
            if att is None or not att.get("alarm") \
                    or att.get("state") != DEAF_IN_EFFECT:
                continue                     # the register has moved on
            if not _repair_due(att):
                continue
            row = r.get(seat)
            # THE EPISODE IS OPENED BY THE ACTUATOR, IMMEDIATELY BEFORE THE
            # FORK, and only once every refusal it can make has been made.
            # Opened HERE, before the call, a refused wake -- debounce, a
            # paused seat, a drained backlog -- had already replaced the
            # roster's in-flight attempt with a new one that nothing would
            # ever run, and the running child's own report was then refused
            # against an attempt it never was. Installation is the last act
            # before spawn, or it is not an installation of anything.
            try:
                from . import resumeturn
                got = resumeturn.wake_undelivered(
                    seat, (row or {}).get("session"), waited=att.get("waited"),
                    att=att)
            except Exception as exc:         # noqa: BLE001 — a failed repair
                got = {"action": "alert",    # must never cost the alarm below
                       "detail": "%s: %s" % (exc.__class__.__name__, exc)}
            repaired.append((seat, att, got))
            out.setdefault("nudged", []).append(
                (seat, got.get("action"), got.get("detail")))
        # THE RE-ARM LEG, on the same latch and the same revalidation. The
        # register must still say DEAF with the alarm up, the spell's episode
        # must be due, and the actuator re-derives the pause and whether the
        # seat is still deaf before it types.
        for crow in mute:
            seat = roster_names.get(_seat_key(crow.get("seat")))
            att = _att(seat) if seat else None
            if att is None or not att.get("alarm") \
                    or att.get("state") != DEAF:
                continue
            if not _repair_due(att):
                continue
            row = r.get(seat)
            try:
                from . import resumeturn
                got = resumeturn.rearm_deaf(
                    seat, (row or {}).get("session"), att=att)
            except Exception as exc:         # noqa: BLE001 — never costs
                got = {"action": "alert",    # the alarm below
                       "detail": "%s: %s" % (exc.__class__.__name__, exc)}
            # A CLEAN IDLE DEAF SEAT IS NOT A REPAIR AT ALL: it owes
            # nothing, so nothing was attempted, recorded or reported.
            if got.get("action") == "idle":
                continue
            repaired.append((seat, att, got))
            out.setdefault("rearmed", []).append(
                (seat, got.get("action"), got.get("detail")))
        if repaired:
            _record_repairs(repaired, now)
        chat_rows = [t for t in fresh
                     if t[1]["alarm"] != _latch(t[0], "alarmed")]
        push_rows = [t for t in fresh
                     if t[1]["alarm"] != _latch(t[0], "pushed")]
        # COUNT WHAT SURVIVES, not what was derived: `alarms` drives the
        # operator's "this alarm reached the fleet room ONLY" note, and a
        # collapsed row reached nothing. Counted from the batch (so the seat
        # set is the caller's) but gated on the rows that are actually about
        # to be delivered on at least one channel.
        owed = {t[0] for t in chat_rows} | {t[0] for t in push_rows}
        out["alarms"] = sum(1 for seat, att, _p in transitions
                            if att["alarm"] and seat in owed)
        return _deliver_legs(out, chat_rows, push_rows, rep, now)


def _deliver_legs(out, chat_rows, push_rows, rep, now):
    """The two delivery legs, RUN UNDER escalate's lock (split out only so the
    lock body stays readable — the lock is the caller's, and the ack must land
    inside it or the revalidate-deliver-ack window reopens)."""
    from . import chat, notify
    if chat_rows:
        lines = []
        for seat, att, prior in chat_rows:
            if att["alarm"]:
                lines.append(_alarm_line(seat, att))
            else:
                what = "answers again" if att["state"] == COVERED \
                    else "is no longer PROVEN unreachable (%s)" % att["state"]
                lines.append("%s %s — was unreachable for %s."
                             % (label(seat), what,
                                _age(prior.get("since") or now)))
        body = ("beacons: seat reachability CHANGED\n" + "\n".join(lines) + "\n"
                + _summary(rep))
        try:
            chat.post(body, who="beacons", room="helm")
        except Exception:                    # noqa: BLE001 — retried next pass
            out["chat"] = False
        else:
            _ack_alerts(chat_rows, chat_leg=True)
    if push_rows:
        # A LATCH RECORDS A PUSH THAT HAPPENED, AND AN OPT-OUT SENDS NOTHING.
        # `owner_push` returns True for two different worlds — its docstring
        # says so: "delivered, OR DELIBERATELY OPTED OUT (no topic: nothing to
        # retry)". From notify's own invariants that is right; with no endpoint
        # there is genuinely nothing to retry. But `pushed` means "the alarm
        # value the last successful PHONE push carried", so latching on that
        # True records a push that never left the machine.
        #
        # Read BEFORE the call, never after: a topic set mid-pass would
        # otherwise latch an edge that predates the channel existing.
        #
        # The consequence was never "the phone goes silent forever" — the next
        # EDGE still pushes. What was lost is the STANDING alarm at configure
        # time: fleet goes down while the phone is off, the edge latches, the
        # owner sets HELM_NTFY_TOPIC, and the phone says nothing about the
        # fleet that is STILL down, because no new transition exists to carry
        # it. Silence on exactly the state he turned the channel on to hear.
        #
        # This module already KNEW: `notify.configured()` is called ~240 lines
        # below to tell the HUMAN the alarm reached the room only. That fixed
        # the eyes and left the state machine believing the push happened.
        # Same question, now asked where the latch is.
        armed = notify.configured()
        if notify.owner_push(_push_body(push_rows),
                             title="helm fleet unreachable"
                                   if any(t[1]["alarm"] for t in push_rows)
                                   else "helm fleet",
                             receipt=("beacons.notify_failed", "fleet")):
            if armed:
                _ack_alerts(push_rows, push_leg=True)
            # else: nothing was sent, so nothing latches — the edge stays
            # armed and re-pushes the moment a channel exists, which is
            # exactly what this module already promises a FAILED push
            # ("it stays armed and re-pushes next pass"). An opt-out sent
            # strictly less than a failure and must not be treated better.
        else:
            out["push"] = False
    return out


# ---------------------------------------------------------------------------
# the cadence — the census was the ONLY instrument that stayed correct through
# the 2026-08-03 outage, and it ran only when a human typed it; nobody typed
# it for four hours. proxywatch's unit pattern, verbatim. The cadence itself,
# INTERVAL_S, is defined near the top of the module: REARM_GRACE_S is derived
# from it.
# ---------------------------------------------------------------------------


_SERVICE = """[Unit]
Description=helm beacons (seat-reachability census -> roster attendance register, one pass)

[Service]
Type=oneshot
# Run FROM the repo: helm derives a post's room from cwd, and a unit with no
# WorkingDirectory starts in $HOME, derives no project, and falls back to
# #main (proxywatch's law, measured 2026-07-29).
WorkingDirectory=%(cwd)s
ExecStart=%(helm)s beacons --post
# Exit 1 means the census RAN and FOUND FAULTS (they are on the register and
# in chat); only exit 2 means the watchdog itself failed. Without this line
# the two are one signal: helm-proxywatch read `failed (exit-code 1)` in
# systemctl all through 2026-08-03 while working correctly, so a dead
# watchdog and a working one were indistinguishable.
SuccessExitStatus=1
"""

_TIMER = """[Unit]
Description=helm beacons cadence (external, no demons)

[Timer]
OnBootSec=%(interval)ss
OnUnitActiveSec=%(interval)ss

[Install]
WantedBy=timers.target
"""


def timer_units(interval=INTERVAL_S):
    helm_bin = os.path.join(os.path.expanduser("~"), ".local", "bin", "helm")
    udir = os.path.join(os.path.expanduser("~"), ".config", "systemd", "user")
    # WorkingDirectory is DERIVED, never a literal — proxywatch's rebind-unit
    # law: an operator path baked into a tracked template is a machine
    # identity this repo cannot carry, and work.find_root folds a lane
    # worktree back to the SHARED checkout so a persistent unit never
    # captures a disposable worktree as its cwd.
    from . import work
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cwd = work.find_root(here) or here
    return (os.path.join(udir, "helm-beacons.service"),
            _SERVICE % {"helm": helm_bin, "cwd": cwd},
            os.path.join(udir, "helm-beacons.timer"),
            _TIMER % {"interval": interval})


def ensure_timer(interval=INTERVAL_S):
    """(ok, detail) — install + enable the cadence."""
    import shutil
    import subprocess
    if interval < 1:
        return False, "interval must be at least 1 second"
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return False, ("systemctl unavailable; run `helm beacons --post` "
                       "from another scheduler")
    spath, service, tpath, timer = timer_units(interval)
    try:
        pk.atomic_write(spath, service)
        pk.atomic_write(tpath, timer)
    except OSError as e:
        return False, "unit write failed: %s" % e
    for cmd in ([systemctl, "--user", "daemon-reload"],
                [systemctl, "--user", "enable", "--now", "helm-beacons.timer"]):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            return False, "%s failed: %s" % (
                " ".join(cmd), (r.stderr or r.stdout or "").strip()[:200])
    return True, "timer enabled every %ds (%s)" % (interval, tpath)


# ---------------------------------------------------------------------------
# CLI — the READ is still pure (it signals nothing and writes nothing); only
# --post, the timer's entry, writes the register and posts the edges. There
# is no fleet-wide reap here, deliberately: the only thing that stops a
# beacon is its OWN seat's next re-arm.
# ---------------------------------------------------------------------------

def label(seat):
    """A seat name safe to print.

    TWO UNVALIDATED SOURCES reach this module's sink and they are different in
    kind. The roster KEY is unvalidated at the join seam (a hostile
    HELM_CHAT_NAME lands there verbatim), and — unique to this module — a seat
    name read from ANOTHER PROCESS's `--seat` argv or environ, which no join
    seam ever saw. Both go through `seats._seat_label`, the same ESC/bidi
    scrub + byte cap every other roster-borne display string clears, so the
    most prominent column of this report cannot reshape the terminal of the
    operator reading it."""
    from . import seats
    return seats._seat_label(seat)


def _age(epoch):
    if not epoch:
        return "?"
    return _age_s(time.time() - epoch)


def _age_s(d):
    """The same rendering from a DURATION, for a caller that measured one
    rather than a point in time. `_age` reads the clock; this must not, or a
    duration handed in would be re-dated against now."""
    if d is None:
        return "?"
    if d < 90:
        return "%ds" % int(d)
    if d < 5400:
        return "%dm" % int(d / 60)
    if d < 129600:
        return "%dh" % int(d / 3600)
    return "%dd" % int(d / 86400)


def unenrolled_panes():
    """{"rows": [pane...], "why": reason|None, "partial": [reason...]} -- the
    live metaharness panes carrying NO helm seat identity.

    THE DENOMINATOR THIS CENSUS NEVER HAD, and the reason it needs one is in
    `roll`: every addition there is ROSTER-INTERSECTED by construction, so a
    pane that never announced a seat is not DEAF, not VACANT and not UNPROVEN
    -- it is not censused AT ALL. The seat total is therefore a count of the
    seats helm KNOWS, and it reads as a count of the agents on the box. Both
    sentences are true, which is exactly why the line never looked wrong.

    Measured on this fleet the day this landed: the census reported 19 seats
    with 12 DEAF while the pane inventory held 24 panes, 8 of them carrying no
    identity at all. Not one of those 8 appeared anywhere in the census, under
    any verdict, and 11 of the 12 DEAF seats had a live NAMED pane in that same
    inventory -- so the two populations are DISJOINT, and an operator reading
    the DEAF alarm is reading an alarm that cannot fire for the unenrolled
    half. NOT ALL 12, and the twelfth is the reason to write the number rather
    than the word: one DEAF seat had no pane at all, which is a THIRD state
    again (enrolled, gone) and neither of the two this line is about.

    NOT A SECOND SOURCE OF TRUTH: this asks `orcaadopt.pane_rows`, the one
    place helm decides a pane's seat, and adds no scan of its own.

    FAIL-OPEN AND THE REASON TRAVELS. No metaharness, a dead daemon or a /proc
    census too blind to label anything answers rows=[] WITH a `why`, because an
    empty list on its own says "every pane is named" -- the one reading a blind
    probe must never produce. `partial` carries `pane_rows`' OWN per-row doubt
    (an unowned row the session join could not name), so a count taken while
    the roster was unreadable is never reported as a count of panes that HAVE
    no identity."""
    try:
        from . import orcaadopt
        rows, note = orcaadopt.pane_rows()
    except Exception as exc:                     # noqa: BLE001 -- fail-open law
        return {"rows": [], "partial": [],
                "why": "pane inventory unavailable (%s: %s)"
                       % (type(exc).__name__, exc)}
    if note:
        return {"rows": [], "shells": [], "partial": [], "why": note}
    unowned = [r for r in rows if r.get("provenance") == orcaadopt.UNOWNED]
    # A PANE WITH NO AGENT IN IT IS NOT AN UNENROLLED AGENT. `holds_agent` is
    # pane_rows' answer to whether a live claude process sits in the pane;
    # False is a bare shell and is reported apart, None is could-not-tell and
    # stays counted, with its doubt carried.
    shells = [r for r in unowned if r.get("holds_agent") is False]
    agents = [r for r in unowned if r.get("holds_agent") is not False]
    partial = {r["identity_partial"] for r in agents
               if r.get("identity_partial")}
    unsure = sum(1 for r in agents if r.get("holds_agent") is None)
    if unsure:
        partial.add("whether %d pane%s hold%s an agent could not be told"
                    % (unsure, "s"[:unsure != 1], "s"[:unsure == 1]))
    return {"rows": agents, "shells": shells, "partial": sorted(partial),
            "why": None}


def _unenrolled_line(rep):
    """The census's sentence about the panes it does not speak for, or None.

    None when the caller never took the inventory -- `census()` is the cheap
    library read and does not take it, so a consumer that skipped it gets
    silence rather than an invented zero."""
    un = rep.get("unenrolled")
    if not isinstance(un, dict):
        return None
    if un.get("why"):
        return ("  UNENROLLED PANES: NOT COUNTED (%s) \u2014 the seat total above "
                "is every seat helm KNOWS, never every agent on this box"
                % un["why"])
    rows, shells = un.get("rows") or (), un.get("shells") or ()
    if not rows:
        return ("  %d bare shell pane%s (no agent process, not counted)"
                % (len(shells), "s"[:len(shells) != 1])) if shells else None
    where = ", ".join(sorted({(r.get("worktree") or "?") for r in rows}))
    line = ("  %d UNENROLLED PANE%s \u2014 live, and carrying no helm seat "
            "identity, so NONE of them is in the seat total above: a pane helm "
            "cannot name is not DEAF, it is uncensused, and no verdict here "
            "speaks for it. At %s. `helm seat panes` lists them."
            % (len(rows), "S"[:len(rows) != 1], where))
    for reason in un.get("partial") or ():
        line += ("\n    NOTE: %s \u2014 one of those panes may be a seat this "
                 "inventory could not name, not a pane with no identity"
                 % reason)
    if shells:
        line += ("\n    plus %d bare shell pane%s (no agent process, not "
                 "counted)" % (len(shells), "s"[:len(shells) != 1]))
    return line


def _summary(rep):
    # COUNTED, never derived by subtraction: the old "seats minus deaf minus
    # unproven" reported every new verdict as covered the moment one was added,
    # so the summary would have called the vacant seats covered while the lines
    # above it named them. One builder, because this line is now also the
    # tail of every escalation post and two spellings would drift.
    # AND MISROUTED JOINS IT, because the comment above is exactly the trap a
    # new verdict walks into: a seat that is neither covered nor deaf simply
    # went missing from this line, so the counts stopped summing to the seat
    # total and the one verdict nobody had seen before was the one the summary
    # did not mention. The OWNERSHIP fraction rides here too -- MISROUTED
    # counts what was PROVEN sidechain, and without the denominator a zero
    # reads as "none is misrouted" when the truth may be "none could be
    # attributed".
    # WAKING JOINS IT for the same reason (task/3055), read with `.get`
    # because a report built before the bucket existed, or by a caller that
    # never asked for it, has no WAKING seat to count.
    own = _ownership_of(rep)
    return ("helm beacons: %d seat%s, %d covered, %d WAKING, %d DEAF, "
            "%d DEAF-IN-EFFECT, "
            "%d MISROUTED, %d VACANT, %d UNPROVEN, %d ghost waiter%s, "
            "%d beacon%s (%d surplus); %s" % (
                len(rep["seats"]), "s"[:len(rep["seats"]) != 1],
                len(rep["covered"]), len(rep.get("waking") or ()),
                len(rep["deaf"]),
                len(rep["deaf_in_effect"]), len(own["misrouted"]),
                len(rep["vacant"]),
                len(rep["unproven"]),
                len(rep["ghosts"]), "s"[:len(rep["ghosts"]) != 1],
                rep["beacons"], "s"[:rep["beacons"] != 1], rep["surplus"],
                _ownership_clause(own))) + _summary_unenrolled(rep)


def _ownership_clause(own):
    """The summary's attribution clause: SEAT join first, ORIGIN second.

    Two questions, two counts (task/2925). A reader who saw "8 unattributed"
    took it for the seat join and suspected the census, while the seat join
    was right and the number was the origin stamp that nothing produces. So
    the seat join is printed on its own, and an origin that nothing stamped
    is called unstamped. A mixed fleet keeps the full origin fraction, so the
    sidechain count that MISROUTED rests on is still on the line."""
    joined = "seat-joined %d/%d live beacon%s" % (
        own.get("seat_joined", 0), own["live"], "s"[:own["live"] != 1])
    if own.get("origin_unstamped"):
        return ("%s; origin UNSTAMPED on all %d (no stamp producer exists "
                "yet, so main versus subagent is not measured)"
                % (joined, own["unattributed"]))
    unstamped = own.get("unstamped") or 0
    return ("%s; origin %d top-level / %d sidechain / %d unattributed%s of "
            "%d live" % (joined, own["top_level"], own["sidechain"],
                         own["unattributed"],
                         " (%d unstamped)" % unstamped if unstamped else "",
                         own["live"]))


def _summary_unenrolled(rep):
    """The summary's unenrolled clause, or "" when nothing was measured.

    IT RIDES THE ESCALATION POST, which is the whole point of putting it here
    rather than only on the screen: the summary line is the tail of every
    chat and phone alarm, and an alarm that says "N seats, M DEAF" while the
    box holds unnamed panes has told the reader the fleet is fully accounted
    for. EMPTY WHEN NOT TAKEN: `census()` does not take the pane inventory, so
    a consumer that did not attach one gets the line it always got -- an
    invented "; 0 unenrolled" would be the false clean bill this whole module
    exists to refuse."""
    un = rep.get("unenrolled")
    if not isinstance(un, dict):
        return ""
    if un.get("why"):
        return "; unenrolled panes NOT COUNTED (%s)" % un["why"]
    rows, shells = un.get("rows") or (), un.get("shells") or ()
    return "; %d unenrolled pane%s%s" % (
        len(rows), "s"[:len(rows) != 1],
        " (+%d bare shell%s)" % (len(shells), "s"[:len(shells) != 1])
        if shells else "")


def _print_census(rep):
    print("helm beacons — the inbox beacon is helm's only wake path to a seat")
    if not rep["live_probe"]:
        print("  session liveness could not be probed — every session reads "
              "UNKNOWN and nothing below is a death claim")
    if not rep["seats"]:
        print("  no seats in the roster and no registered beacons")
    for row in rep["seats"]:
        print("  %-22s %-9s live %d  ghost %d  unknown %d%s%s" % (
            label(row["seat"]), row["verdict"], len(row["live"]),
            len(row["ghosts"]),
            len(row["unknown"]),
            "  expired %d" % len(row["expired"]) if row.get("expired") else "",
            "   (+%d surplus)" % row["duplicates"] if row["duplicates"] else ""))
    # OWNERSHIP IS A FRACTION, NEVER A VERDICT ON ITS OWN. MISROUTED counts
    # what was PROVEN sidechain; this line counts the live beacons no
    # transcript could be joined to. Printing the first without the second is
    # what would make a wired rung read as "nothing is misrouted" when the
    # truthful sentence is "nothing COULD BE ATTRIBUTED" -- and the arming
    # form most seats actually use (`cd <repo> && helm chat wait ...`) is
    # UNPROVEN by design, so the unattributed share is expected to be real
    # rather than exceptional.
    own = _ownership_of(rep)
    if own["live"]:
        print("  beacon SEATS: %d of %d live beacon(s) joined to a seat whose "
              "pane is named" % (own.get("seat_joined", 0), own["live"]))
        if own.get("origin_unstamped"):
            print("  beacon ORIGIN: UNSTAMPED on all %d — no stamp producer "
                  "exists yet, so whether the main turn or a subagent armed "
                  "each one is not measured (this is not a seat-join failure)"
                  % own["unattributed"])
        else:
            print("  beacon ORIGIN: %d top-level, %d sidechain, %d "
                  "unattributed of %d live beacon(s)%s"
                  % (own["top_level"], own["sidechain"], own["unattributed"],
                     own["live"],
                     " — UNKNOWN is not a claim that none is misrouted"
                     if own["unattributed"] else ""))
        # THE REASONS ARE AGGREGATED, NOT LISTED PER BEACON: a fleet of
        # forty unattributed beacons usually has two or three causes, and
        # the count beside each is what tells a reader which one to fix.
        word = "unstamped" if own.get("origin_unstamped") else "unattributed"
        for why, n in sorted(own["reasons"].items(),
                             key=lambda kv: (-kv[1], kv[0])):
            print("    %s x%d: %s" % (word, n, why))
    if not own["home_probe"]:
        print("  credential homes could not be enumerated, so NO beacon could "
              "be attributed and MISROUTED cannot be concluded about anybody")
    for row in rep.get("misrouted") or ():
        print("  MISROUTED SEAT %s — %s. It is not DEAF: a live beacon is "
              "running and consuming this seat's rows, which is why every "
              "process-shaped instrument reads it as covered. Re-arm from the "
              "TOP-LEVEL conversation (not a subagent) with `helm chat wait "
              "--seat %s --follow --replace`." % (
                  label(row["seat"]), row["why"], label(row["seat"])))
    for row in rep.get("waking") or ():
        print("  WAKING SEAT %s — %s. Not DEAF: a beacon that reached its "
              "lease deadline is the seat's check-in, and nothing is typed "
              "into it; it reads DEAF only if the grace passes with no "
              "re-arm." % (label(row["seat"]), row["why"]))
    for row in rep["deaf"]:
        print("  DEAF SEAT %s — no live beacon: helm cannot wake it. It must "
              "re-arm `helm chat wait --seat %s --follow` to be reachable "
              "here." % (label(row["seat"]), label(row["seat"])))
    for row in rep["deaf_in_effect"]:
        # THE VERDICT'S OWN SENTENCE, ONCE. `_verdict` already builds the
        # whole claim including the duration, so a lead-in that restated it
        # printed the wait twice in one line.
        print("  DEAF-IN-EFFECT SEAT %s — %s. Re-arming the beacon fixes "
              "nothing here: the waiter is not the thing that failed, so the "
              "repair is to make the pane take a turn: `%s`." % (
                  label(row["seat"]), row["why"],
                  repair_argv(label(row["seat"]))))
    for row in rep["vacant"]:
        print("  VACANT SEAT %s — its wake path is LIVE and nobody is home: "
              "%d live beacon%s consume%s its addressed rows, and %s. A DM to "
              "it is swallowed exactly as a ghost's is — the inverse case, a "
              "live beacon for a dead seat. Only a relaunched agent (or that "
              "seat's own de-arm) ends it." % (
                  label(row["seat"]), len(row["live"]),
                  "s"[:len(row["live"]) != 1], "s"[:len(row["live"]) == 1],
                  row["why"]))
    for row in rep["unproven"]:
        if unreachable(row):
            print("  UNREACHABLE %s (verdict UNPROVEN) — %s. The instruments "
                  "could not classify it AND could not prove it a wake path: "
                  "a POSSIBLE outage they could not name, never a clean bill."
                  % (label(row["seat"]), row["why"]))
        else:
            print("  UNPROVEN %s — %s. Not a deaf seat and not a healthy one; "
                  "the instruments could not answer."
                  % (label(row["seat"]), row["why"]))
    for b in rep["ghosts"]:
        print("  GHOST WAITER pid %s seat %s armed %s ago — %s. It consumes "
              "that seat's addressed rows into a pipe nobody reads, and it "
              "satisfies a naive shape check, so it makes a dark seat read as "
              "covered." % (b["pid"], label(b["seat"]), _age(b["armed"]),
                            b["why"]))
    # BESIDE THE DEAF LINES, NOT UNDER THE SUMMARY. The two populations are
    # disjoint and the reader's next move differs: a DEAF seat is re-armed, an
    # unenrolled pane has nothing to re-arm because helm does not know its
    # name. Printing them apart is what stops one repair being tried on both.
    line = _unenrolled_line(rep)
    if line:
        print(line)
    print(_summary(rep))
    if rep["ghosts"] or rep["surplus"] or rep["vacant"]:
        print("  This verb signals NOTHING. A superseded beacon is stopped by "
              "its OWN seat's next re-arm (stop-then-start) — reaping by "
              "pattern across a shared process table is how a live seat's "
              "wake path gets severed.")


def _prompt_stall_leg(seat):
    """The prompt-stall watch, riding this pass's cadence -> the stalled rows.

    A DEAF verdict is often the CONSEQUENCE of a seat frozen at a Claude Code
    prompt: its beacon times out and it cannot re-arm one. On its own this
    pass names only that consequence and prescribes a re-arm the frozen seat
    cannot perform, while the prompt that caused it goes unnamed.
    planprompt.stall_pass reads the vendor's own presence record for every
    Claude seat and posts the prompt LOUDLY; it types nothing.

    FLEET-WIDE AND REAL-HOST ONLY. A `--seat` read is one seat's question, and
    a pass pointed at another proc tree (HELM_PROC) has no prompt state to
    read there, so neither runs it rather than reading the live host behind
    the caller's back. Fail-open: a broken leg is reported, never fatal."""
    if seat or home.env("PROC"):
        return []
    try:
        from . import planprompt
        got = planprompt.stall_pass()
    except Exception as e:                       # noqa: BLE001 — never blocks
        print("helm beacons: prompt-stall watch skipped (%s)" % e,
              file=sys.stderr)
        return []
    for line in got["lines"]:
        print("helm beacons: %s" % line, file=sys.stderr)
    return got["stalls"]


def cmd_beacons(args):
    """beacons [--seat S] [--json] [--post] [--install-timer] — the beacon
    registry census: every seat's inbox beacons, ALL THREE directions — a DEAF
    seat (no live beacon), a GHOST waiter (a beacon whose session is dead) and
    a VACANT seat (a live beacon with no agent home). The bare read signals
    nothing and writes nothing. --post (the timer's entry) additionally writes
    each verdict onto its roster row (the attendance register) and delivers
    reachability edges on BOTH channels, change-latched per channel: the fleet
    room, and the owner's phone (HELM_NTFY_TOPIC) — because when the fleet is
    unreachable, every reader of the fleet room is one of the unreachable
    seats.

    AND --post ACTUATES, which this help did not say. The fourth verdict is
    DEAF-IN-EFFECT: a seat whose wake path is LIVE and whose turns are not
    consuming the rows addressed to it. Re-arming a beacon fixes nothing
    there, because the waiter is not what failed — so the repair is to make
    the pane take a turn, and --post TYPES INTO THAT SEAT'S PANE. It is
    bounded (one settled repair per spell, and a per-hour cap) and refused
    outright while delivery to the seat is paused, but it is a MUTATION of
    another agent's session, and a reader deciding whether to run this verb is
    entitled to know that BEFORE they run it. WITHOUT --post nothing is
    written and nothing is typed, and that qualification is the whole of the
    promise: --json is a RENDERING choice, not a dry run, so `--json --post`
    attends, renders, escalates and can spawn the pane repair exactly as the
    text form does.

    AND A DEAF SEAT WHOSE PANE DECLARES IT (HELM_CHAT_NAME) AND THAT OWES
    WORK (a pending addressed row, or owed dispatch work) gets the same
    bounded nudge with a different sentence: its waiter died, so --post
    asks the pane to re-arm its beacon and names the exact Monitor call. A
    DEAF seat that owes nothing is never typed into. Nor is one of another
    project: it is reported, with what it owes, and never typed into. A seat
    on a paid (proxy or codex) family that OWES is typed into like a native
    one (task/3055, an owner ruling), and the report names the paid turn. A
    WAKING seat — a beacon that reached its 30-minute lease inside the re-arm
    grace — is never typed into.
    Same bounds, same pause refusal, one settled nudge per DEAF spell, and
    the latch closes when the seat reads covered again. A seat whose pane
    the census could not name is never typed into. --install-timer wires
    the cadence. Exit under --post: 0 clean, 1 the census RAN and found faults,
    2 the watchdog itself failed — distinct on purpose, so a working watchdog
    and a broken one stop being the same signal."""
    import sys
    from .cli import guard_tail
    args = list(args or [])
    rc = guard_tail("helm beacons", args,
                    flags=("--json", "--post", "--install-timer"),
                    valued=("--seat",),
                    usage=(cmd_beacons.__doc__ or "").strip())
    if rc is not None:
        return rc
    if "--install-timer" in args:
        ok, detail = ensure_timer()
        print("helm beacons: %s%s" % ("" if ok else "timer NOT installed — ",
                                      detail),
              file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 2
    from . import rearm
    seat = rearm._flag(args, "--seat")
    rep = census([seat] if seat else None)
    # THE PANE INVENTORY IS TAKEN HERE, NOT IN `census`. `census` is the cheap
    # library read every other consumer shares; this costs one metaharness RPC
    # and one /proc pass (measured 0.3s wall for the whole verb on a 24-pane
    # box) and belongs to the OPERATOR surface that renders a fleet total. It
    # is attached BEFORE attend and escalate on purpose, so the register, the
    # alarm and the screen cannot disagree about how much of the box the
    # census spoke for. A --seat read is one seat's question, never a fleet
    # total, so it takes no inventory and prints no clause about one.
    if not seat:
        rep["unenrolled"] = unenrolled_panes()
    # THE SURFACE RENDERS WHAT WAS PERSISTED, NOT WHAT WAS PROPOSED. `attend`
    # is where a recovery is judged UNPROVEN and the seat is HELD at
    # DEAF-IN-EFFECT, and it rebuckets the report in place when it does. Under
    # `--post` that ran AFTER this render, so ONE invocation could print a seat
    # as COVERED and then write DEAF-IN-EFFECT onto its roster row -- the
    # operator's copy and the register disagreeing about the same seat in the
    # same breath, with the reassuring one on the screen (task/2463 finding 8).
    # The bare read is unchanged: it proposes nothing, so there is nothing to
    # reconcile against.
    reg = attend(rep) if "--post" in args else None
    if "--json" in args:
        print(json.dumps(rep, indent=2, default=str))
    else:
        _print_census(rep)
    if reg is None:
        return 0
    if reg["error"]:
        print("helm beacons: register write failed (%s) — attendance not "
              "recorded" % reg["error"], file=sys.stderr)
        return 2
    # AN UNDELIVERED OWNER DECISION IS AN OWNER-REACHABILITY FAILURE, so this
    # watchdog owns its retry (a second review pass on task/232). A decision
    # card's carry rides the NEXT card's push, which leaves the LAST card ever
    # filed with nothing behind it — and marking it in `helm decide list` tells
    # an AGENT, not the owner, which is the dependency-on-noticing defect the
    # row exists to remove. This pass already runs on a schedule, already
    # pushes through notify, and already re-arms a failed edge; a rival timer
    # would be the parallel mechanism notify.py was written to prevent.
    # Fail-open and quiet: no backlog or no channel sends nothing.
    try:
        from . import ownerasks
        _flushed, _why = ownerasks.flush_unreached()
        if _why:
            print("helm beacons: owner DECISION backlog — %s" % _why,
                  file=sys.stderr)
    except Exception as _e:                      # noqa: BLE001 — never blocks
        print("helm beacons: decision-backlog flush skipped (%s)" % _e,
              file=sys.stderr)
    stalled = _prompt_stall_leg(seat)
    sent = escalate(reg["transitions"], rep)
    if sent.get("refused"):
        # Named for what it IS: the fall-through would report "chat post
        # failed", and a refusal wearing a delivery-failure's words sends the
        # operator to the wrong subsystem.
        print("helm beacons: %s" % sent["refused"], file=sys.stderr)
        return 2
    # THE REPAIR'S OUTCOME, ON THE SURFACE A HUMAN IS LOOKING AT. This leg
    # types into a pane and it was the only leg whose result nothing printed:
    # a parent that could not prove the pane, or a fork that failed, returned
    # its reason into a field no reader consumed, so an automatic repair that
    # never happened was indistinguishable from one that did.
    for entry in sent.get("nudged") or ():
        seat, action = entry[0], entry[1]
        detail = entry[2] if len(entry) > 2 else None
        if action == "wake":
            # SPAWNED IS WHAT THIS PROCESS KNOWS. `wake` means the detached
            # deliverer was launched, and the child can still pause, find the
            # backlog drained, or refuse before a key is pressed -- so a line
            # claiming the keystroke landed is a claim about a result only the
            # child sees. TYPED is printed from an attributable child report,
            # which is the settled episode, never from the launch.
            print("helm beacons: pane repair SPAWNED for %s — a detached "
                  "deliverer was launched to nudge it into a turn, and "
                  "whether it typed is that child's own report; the alarm "
                  "stands until a turn consumes the backlog"
                  % label(seat), file=sys.stderr)
        else:
            print("helm beacons: pane repair for %s did NOT type (%s) — %s"
                  % (label(seat), action, detail or "no reason given"),
                  file=sys.stderr)
    for seat, action, detail in sent.get("rearmed") or ():
        if action == "foreign-project":
            # REPORTED, NEVER TYPED: an owing DEAF seat that is not ours to
            # wake (another project's). A paid family that owes is typed into
            # now (task/3055) and reports through the "wake" arm below.
            print("helm beacons: DEAF seat %s NOT auto-nudged — %s"
                  % (label(seat), detail or action), file=sys.stderr)
        elif action == "wake":
            print("helm beacons: re-arm nudge SPAWNED for DEAF seat %s — a "
                  "detached deliverer was launched to ask its pane to re-arm "
                  "its beacon; whether it typed is that child's own report"
                  % label(seat), file=sys.stderr)
        else:
            print("helm beacons: re-arm nudge for DEAF seat %s did NOT type "
                  "(%s) — %s" % (label(seat), action,
                                 detail or "no reason given"),
                  file=sys.stderr)
    if sent["alarms"]:
        from . import notify
        if not notify.configured():
            # THE HALF-WORKING ALARM, named out loud on the surface a human is
            # actually looking at. An opted-out push returns delivered, so the
            # only way this reads as anything but success is to say it.
            print("helm beacons: phone channel OFF (HELM_NTFY_TOPIC unset) — "
                  "this alarm reached the fleet room ONLY, and the fleet room "
                  "is read by the seats", file=sys.stderr)
        elif not sent["push"]:
            print("helm beacons: owner push FAILED — the fleet room has the "
                  "edge, the phone does not; it stays armed and re-pushes "
                  "next pass", file=sys.stderr)
    if not sent["chat"]:
        print("helm beacons: chat post failed — the edge stays unlatched "
              "and re-posts next pass", file=sys.stderr)
        return 2
    return 1 if (rep["unreachable"] or rep["vacant"] or rep["ghosts"]
                 or stalled) else 0
