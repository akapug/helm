#!/usr/bin/env python3
"""helm beacons — the inbox-beacon REGISTRY and its lifecycle.

A seat's inbox beacon (`helm chat wait --seat <seat> --follow`) is its ONLY
wake path. Nothing external can re-invoke a PTY agent, so a seat whose beacon
is gone cannot be reached by anything, ever — and it looks completely normal
from outside. That is the worst failure shape this project has: a silent one
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
     @kimi measured exactly those orphans on her own seat: detached shells,
     children of dead claude sessions (chat #helm row 623).

THE CURE IS @kimi's SHAPE (row 623), taken as posted: "A beacon registering
itself (seat + session + arm-time in a small registry the reaper reads) covers
exactly that: the registry survives the session that armed it. Otherwise every
compaction mints one more unattributable waiter." Her lesson is the load-bearing
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

  * a DEAF SEAT   — no live beacon: nothing can wake it.
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
a live seat's only wake path.

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
    orphan its child (@helm-claude-2, row 624: "reap the PYTHON process, not
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
kept, because killing the wrong waiter severs a live seat's only wake path
while killing nothing merely leaves clutter that the next re-arm collects.
"""
import errno
import glob
import json
import os
import re
import signal
import time

from . import home, pk

PROC = "/proc"
REG_SUBDIR = "beacons"
_SEAT_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# Beacon-row states.
LIVE, GHOST, UNKNOWN, GONE = "live", "ghost", "unknown", "gone"
# Seat verdicts. UNPROVEN is not a softer DEAF: DEAF is a PROVEN absence of any
# wake path, UNPROVEN means the instruments could not answer. Collapsing them
# would either invent a crisis or hide one. VACANT is not a softer COVERED
# either: it is a PROVEN live wake path with a PROVEN empty house.
COVERED, DEAF, UNPROVEN, VACANT = "covered", "DEAF", "UNPROVEN", "VACANT"

# "the caller did not probe" — distinct from a probe that ran and failed, which
# is None. Without it a census that already knows the scan failed would re-run
# it once per seat and get the same failure every time.
_UNPROBED = object()


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
        return flag
    env = env if env is not None else proc_env(pid, proc_dir)
    if not env:
        return None
    return env.get("HELM_CHAT_NAME") or env.get("MELD_CHAT_NAME") or None


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
    process of ours becomes an UNCLASSIFIED pane rather than a silent drop."""
    if comm == AGENT_COMM:
        return True
    return any(os.path.basename(a) == AGENT_COMM for a in (argv or [])[:2])


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
                with open(path) as f:
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
    return os.path.join(registry_dir(), "%s.%d.json" % (seat, int(pid)))


def entries(seat=None):
    """Every registry row, newest arm first. Rows are other-process-supplied
    data: a row is a CLAIM about a pid, never permission to act on it — every
    consumer re-proves shape and start-time against /proc before doing
    anything at all."""
    out = []
    pattern = "%s.*.json" % seat if valid_seat(seat) else "*.json"
    for path in sorted(glob.glob(os.path.join(registry_dir(), pattern))):
        row = pk.read_json(path, None)
        if not isinstance(row, dict):
            continue
        try:
            row["pid"] = int(row.get("pid"))
        except (TypeError, ValueError):
            continue
        if seat and str(row.get("seat", "")).casefold() != str(seat).casefold():
            continue
        row["_path"] = path
        out.append(row)
    out.sort(key=lambda r: r.get("armed") or 0, reverse=True)
    return out


def register(seat, session=None, pid=None, proc_dir=None, now=None):
    """Write this beacon's row: (seat, session, pid, arm-time) + the two keys
    that make it checkable later — the beacon's own start-time (anti-pid-reuse)
    and the session HOLDER's (pid, start-time), frozen while the session is
    still alive to be asked. Freezing the holder is the whole point of kimi's
    registry: it survives the session that armed it, so a waiter orphaned by a
    compaction or a crash can still be judged when nobody is left to ask."""
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
    path = _entry_path(seat, pid)
    try:
        os.makedirs(registry_dir(), exist_ok=True)
        pk.write_json(path, row)
    except OSError:
        return None                          # a registry miss never stops a beacon
    return row


def release(seat, pid=None):
    """Drop this beacon's row at exit. Best-effort by design: a row whose
    process is gone is pruned by the next census anyway, so a crashed beacon
    leaves a stale row and not a wrong answer."""
    if not valid_seat(seat):
        return False
    try:
        os.unlink(_entry_path(seat, pid or os.getpid()))
        return True
    except OSError:
        return False


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
        if str(row.get("seat", "")).casefold() == str(seat).casefold():
            return True, "registered beacon of seat %s" % seat
    named = waiter_seat(pid, proc_dir, argv=argv, env=env)
    if not named:
        return False, "waiter names no seat — unattributable"
    if str(named).casefold() != str(seat).casefold():
        return False, "waiter belongs to seat %s" % named
    return True, "waiter declares seat %s" % seat


def _scan(seat, proc_dir=None):
    """Every pid whose argv is a waiter shape naming `seat`. Scoped by SEAT, so
    another fleet's beacons are not even enumerated here."""
    root = _proc_root(proc_dir)
    want, out = str(seat).casefold(), []
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
        if named and str(named).casefold() == want:
            out.append(pid)
    return sorted(out)


# ---------------------------------------------------------------------------
# the actuator — re-arm is STOP-then-START, and it only ever touches its own
# ---------------------------------------------------------------------------

def stop_superseded(seat, keep_pid=None, proc_dir=None, sig=signal.SIGTERM):
    """SIGTERM the beacons this seat has superseded. Returns
    {"stopped": [...], "kept": [(pid, why)], "pruned": [...]}.

    THIS IS THE WHOLE CURE FOR THE ACCUMULATION: re-arm used to be START, so
    every re-arm left its predecessor running and the count only climbed. It is
    STOP-then-START now, and a seat only ever stops ITS OWN.

    The anti-reuse bracket is re-asserted AT the kill (rearm._still_waiter's
    discipline): shape and start-time are re-read immediately before os.kill,
    so a waiter that exited into a recycled pid between the scan and the signal
    is skipped rather than hit. Nothing here is authorized by a registry row
    alone — the row selects a candidate, /proc proves it."""
    keep = {int(keep_pid or os.getpid()), os.getpid()}
    report = {"stopped": [], "kept": [], "pruned": []}
    rows = {r["pid"]: r for r in entries(seat)}
    scanned = _scan(seat, proc_dir)
    if scanned is None:
        report["kept"].append((None, "process table unlistable — nothing signaled"))
        scanned = []
    for pid in sorted(set(rows) | set(scanned)):
        if pid in keep:
            continue
        row = rows.get(pid)
        ok, why = attributable(pid, seat, proc_dir, row=row)
        if not ok:
            if row is not None and pid_alive(pid, row.get("starttime"), proc_dir) is False:
                prune(row)                   # the row outlived its process
                report["pruned"].append(pid)
            else:
                report["kept"].append((pid, why))
            continue
        start = proc_starttime(pid, proc_dir)
        if not is_waiter(pid, proc_dir) or proc_starttime(pid, proc_dir) != start:
            report["kept"].append((pid, "identity changed under the scan"))
            continue
        try:
            os.kill(pid, sig)
            report["stopped"].append(pid)
            if row is not None:
                prune(row)
        except OSError as exc:
            report["kept"].append((pid, "signal failed (%s)" % exc))
    return report


def arm(seat, session=None, pid=None, proc_dir=None, now=None, reap=True):
    """STOP-then-START, the whole re-arm in one call. Never raises: a registry
    that cannot be written must not stop a beacon from beacon-ing."""
    pid = int(pid or os.getpid())
    out = {"seat": seat, "pid": pid, "stopped": [], "kept": [], "pruned": [],
           "registered": False}
    if not valid_seat(seat):
        out["error"] = "seat name is not registry-safe; beacon runs unregistered"
        return out
    if reap:
        try:
            out.update(stop_superseded(seat, keep_pid=pid, proc_dir=proc_dir))
        except Exception as exc:             # noqa: BLE001
            out["error"] = "stop pass failed (%s)" % exc
    try:
        out["registered"] = bool(register(seat, session=session, pid=pid,
                                          proc_dir=proc_dir, now=now))
    except Exception as exc:                 # noqa: BLE001
        out["error"] = "register failed (%s)" % exc
    return out


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
    state, why = session_state(out["session"], holder=holder, live=live,
                               proc_dir=proc_dir, records=records)
    out["session_state"] = state
    out["why"] = why
    out["state"] = {"live": LIVE, "dead": GHOST}.get(state, UNKNOWN)
    if lost is None and out["state"] == LIVE:
        # The launcher could not be identified, so "live" is not provable.
        out["state"], out["session_state"] = UNKNOWN, state
        out["why"] = "%s, but its launcher could not be identified" % why
    return out


def _verdict(live_, unknown, unlistable, agent, agent_why):
    """(verdict, why) — the seat ladder, in one place so the four verdicts
    cannot drift apart across two call sites.

    A LIVE WAKE PATH IS NO LONGER THE WHOLE ANSWER: that is the one rung this
    change moves. `live_` proves a beacon answers; whether a SEAT answers is a
    different question with a different instrument, and the agent rung is the
    only place the two are allowed to disagree."""
    if unlistable:
        return UNPROVEN, "the process table could not be listed"
    if live_:
        return {True: COVERED, False: VACANT, None: UNPROVEN}[agent], agent_why
    if unknown:
        return UNPROVEN, "no beacon could be PROVEN live (%d unknown)" % len(unknown)
    return DEAF, "no live beacon: nothing can wake it"


def unreachable(row):
    """Does this census row have NO PROVEN WAKE PATH? The ALARM CLASS.

    DEAF is the proven half: no live beacon, nothing can wake it. The other
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
    predicate existed the census could only ever whisper about it."""
    if row.get("verdict") == DEAF:
        return True
    return row.get("verdict") == UNPROVEN and not row.get("live")


def seat_census(seat, live=None, proc_dir=None, records=None, agents=_UNPROBED):
    """Every beacon for ONE seat — EVERY one, not the newest.

    Taking the newest arm-time was the exact narrow-question error that made a
    seat with five waiters, four of them stale, read as "current" (chat #helm
    row 622). The verdict is computed over the whole set."""
    rows = {r["pid"]: r for r in entries(seat)}
    scanned = _scan(seat, proc_dir)
    unlistable = scanned is None
    beacons = [classify(pid, seat, rows.get(pid), live, proc_dir, records)
               for pid in sorted(set(rows) | set(scanned or []))]
    for row in list(rows.values()):
        if any(b["pid"] == row["pid"] and b["state"] == GONE for b in beacons):
            prune(row)
    beacons = [b for b in beacons if b["state"] != GONE]
    live_ = [b for b in beacons if b["state"] == LIVE]
    ghosts = [b for b in beacons if b["state"] == GHOST]
    unknown = [b for b in beacons if b["state"] == UNKNOWN]
    agent, agent_why = agent_verdict(seat, live_, agents, live, proc_dir)
    verdict, why = _verdict(live_, unknown, unlistable, agent, agent_why)
    return {"seat": seat, "verdict": verdict, "why": why, "agent": agent,
            "beacons": beacons,
            "live": live_, "ghosts": ghosts, "unknown": unknown,
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


def census(seats=None, proc_dir=None):
    """The fleet read. Seats come from the ROSTER (and from our own registry),
    never from a process-table sweep — the seats of other projects on this box
    are not this fleet's business and must not appear as its defects."""
    live = live_sessions()
    records = holder_records()
    agents = agent_index(proc_dir)
    seen, ordered = set(), []
    for name in roll(seats, live=live, agents=agents):
        key = str(name).casefold()
        if key not in seen:
            seen.add(key)
            ordered.append(name)
    rows = [seat_census(name, live=live, proc_dir=proc_dir, records=records,
                        agents=agents) for name in ordered]
    return {
        "seats": rows,
        "live_probe": live is not None,
        "agent_probe": agents is not None,
        "covered": [r for r in rows if r["verdict"] == COVERED],
        "deaf": [r for r in rows if r["verdict"] == DEAF],
        "vacant": [r for r in rows if r["verdict"] == VACANT],
        "unproven": [r for r in rows if r["verdict"] == UNPROVEN],
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
#   state    the census verdict (covered / DEAF / VACANT / UNPROVEN)
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
    different from the admission it is meant to remember."""
    from . import seats as seats_mod
    return seats_mod.presence_of(seen) != "absent" or str(seat) in held


def _roster_readable():
    """(True, None) or (False, why) — is the roster file actually READABLE?

    `seats.roster()` is `pk.read_json(path, {}) or {}` and `pk.read_json`
    swallows every exception, so a CORRUPT roster returns {} — byte-identical
    to an empty one. The attendance pass then finds no seats, raises no alert,
    and exits 0: a silent no-op that looks exactly like a healthy fleet with
    nothing to report. codex-2's finding, and the same could-not-look-recorded-
    as-a-fact class @kimi found in proxywatch's _read_watch_state (a corrupt
    outbox read as {} and a queue with pending alerts delivered nothing).

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
        with open(path, encoding="utf-8") as f:
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
        held = {r.get("seat") for r in entries() if valid_seat(r.get("seat"))}
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
                for crow in rep["seats"]:
                    seat = crow["seat"]
                    row = r.get(seat)
                    if not isinstance(row, dict):
                        continue             # the register never mints a row
                    prior = row.get("attendance")
                    prior = prior if isinstance(prior, dict) else {}
                    state = crow["verdict"]
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
                           "alerted": prior.get("alerted"),
                           "alarmed": bool(prior.get("alarmed")),
                           "pushed": bool(prior.get("pushed")),
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


def _alarm_line(seat, att):
    ref = att.get("covered") or att.get("seen")
    how_long = ("unreachable for %s" % _age(ref)) if ref \
        else "never seen answering"
    if att["state"] == DEAF:
        return ("DEAF SEAT %s — %s: no live beacon, nothing can wake it. It "
                "must re-arm `helm chat wait --seat %s --follow` before it can "
                "be reached at all." % (label(seat), how_long, label(seat)))
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
        return body + " — nothing can wake them, and the fleet room cannot " \
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
    other half (codex-2's amendment to the bound review of the delivery-edge
    fix above, reproduced on this tree before the cure in BOTH polarities).
    Re-checking ONLY the latches asks "has anyone delivered this yet?" and
    never "is this still TRUE?", so a batch that outlives its own verdict
    delivers a statement about a world that no longer exists. Measured, in
    both polarities: pass A derives alarm=True; the seat RECOVERS; pass B
    writes covered/alarm=False and derives NO edge (the latches are still
    false, so nothing is owed); A then passes latch
    revalidation unchanged and posts DEAF SEAT alpha ... nothing can wake it
    onto a covered row, acking alarmed/pushed=True (observed: recovery
    transitions 0, stale chat=1 push=1). The mirror is worse — a stale
    RECOVERY batch delivered onto a row that has since gone DEAF again told
    the owner's phone "helm fleet: 1 seat reachable again" while the seat was
    down, and reset BOTH latches to false.

    So a row must also still BE what it says: its current attendance is
    re-read and the row collapses unless the register still records the same
    (alarm, state) the batch carries. `state` is checked as well as `alarm`
    because a DEAF->UNPROVEN drift keeps the alarm polarity while flipping
    the sentence between "nothing can wake it" and "the instruments could not
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
    if not transitions:
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
# it for four hours. proxywatch's unit pattern, verbatim.
# ---------------------------------------------------------------------------

INTERVAL_S = 5 * 60


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
    d = time.time() - epoch
    if d < 90:
        return "%ds" % int(d)
    if d < 5400:
        return "%dm" % int(d / 60)
    if d < 129600:
        return "%dh" % int(d / 3600)
    return "%dd" % int(d / 86400)


def _summary(rep):
    # COUNTED, never derived by subtraction: the old "seats minus deaf minus
    # unproven" reported every new verdict as covered the moment one was added,
    # so the summary would have called the vacant seats covered while the lines
    # above it named them. One builder, because this line is now also the
    # tail of every escalation post and two spellings would drift.
    return ("helm beacons: %d seat%s, %d covered, %d DEAF, %d VACANT, "
            "%d UNPROVEN, %d ghost waiter%s, %d beacon%s (%d surplus)" % (
                len(rep["seats"]), "s"[:len(rep["seats"]) != 1],
                len(rep["covered"]), len(rep["deaf"]), len(rep["vacant"]),
                len(rep["unproven"]),
                len(rep["ghosts"]), "s"[:len(rep["ghosts"]) != 1],
                rep["beacons"], "s"[:rep["beacons"] != 1], rep["surplus"]))


def _print_census(rep):
    print("helm beacons — the inbox beacon is a seat's ONLY wake path")
    if not rep["live_probe"]:
        print("  session liveness could not be probed — every session reads "
              "UNKNOWN and nothing below is a death claim")
    if not rep["seats"]:
        print("  no seats in the roster and no registered beacons")
    for row in rep["seats"]:
        print("  %-22s %-9s live %d  ghost %d  unknown %d%s" % (
            label(row["seat"]), row["verdict"], len(row["live"]),
            len(row["ghosts"]),
            len(row["unknown"]),
            "   (+%d surplus)" % row["duplicates"] if row["duplicates"] else ""))
    for row in rep["deaf"]:
        print("  DEAF SEAT %s — no live beacon: nothing can wake it. It must "
              "re-arm `helm chat wait --seat %s --follow` before it can be "
              "reached at all." % (label(row["seat"]), label(row["seat"])))
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
    print(_summary(rep))
    if rep["ghosts"] or rep["surplus"] or rep["vacant"]:
        print("  This verb signals NOTHING. A superseded beacon is stopped by "
              "its OWN seat's next re-arm (stop-then-start) — reaping by "
              "pattern across a shared process table is how a live seat's only "
              "wake path gets severed.")


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
    seats. --install-timer wires the
    cadence. Exit under --post: 0 clean, 1 the census RAN and found faults,
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
    if "--json" in args:
        print(json.dumps(rep, indent=2, default=str))
    else:
        _print_census(rep)
    if "--post" not in args:
        return 0
    reg = attend(rep)
    if reg["error"]:
        print("helm beacons: register write failed (%s) — attendance not "
              "recorded" % reg["error"], file=sys.stderr)
        return 2
    # AN UNDELIVERED OWNER DECISION IS AN OWNER-REACHABILITY FAILURE, so this
    # watchdog owns its retry (@codex-3's second pass on task/232). A decision
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
    sent = escalate(reg["transitions"], rep)
    if sent.get("refused"):
        # Named for what it IS: the fall-through would report "chat post
        # failed", and a refusal wearing a delivery-failure's words sends the
        # operator to the wrong subsystem.
        print("helm beacons: %s" % sent["refused"], file=sys.stderr)
        return 2
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
    return 1 if (rep["unreachable"] or rep["vacant"] or rep["ghosts"]) else 0
