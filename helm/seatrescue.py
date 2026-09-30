"""helm seatrescue — the one REPAIR the seat-memory plane makes: end a runaway
CHILD of a throttled seat, and never the seat.

THE INCIDENT THIS EXISTS FOR (task/3083). A subagent of the integrator ran a
python heredoc that read eighty session transcripts whole, one of them 461 MB,
and json-loaded every line. It reached 9.51G RSS. The Bash tool's timeout did
not end it: the harness moved the command to the background, and nothing
bounds a background command's time. The seat's slice crossed memory.high, and
past that line the kernel stalls EVERY allocating process in the slice, the
agent included. The throttle slowed the child so much that it never reached
memory.max, so the kernel killed nothing (memory.events high 1,486,336,
oom_kill 0). The seat sat wedged for 36 minutes, alive on every liveness
surface, until a person sent SIGTERM to the child ONLY: the slice fell from
12.95G to 3.3G and the seat carried on with its context intact.

THAT ONE SIGNAL IS THE WHOLE CURE, and this module sends it, under gates that
each rest on a reading taken NOW:

  * THE SLICE IS THROTTLED WITH PROCESSES STALLED, on the plane's reading and
    again on this module's own confirmation read, taken immediately before the
    signal: a process of the slice in state D on the kernel's over-high wchan
    AND the slice's memory.events high count rising across a sampled window.
    The cumulative count alone is history (seatceiling's docstring) and never
    licenses anything here.
  * IT HAS HELD PAST A GRACE (HELM_SEAT_RESCUE_GRACE_S). The spell book keeps
    when the unbroken run of throttled-and-stalled readings began; a reading
    that is not both ends the run.
  * SHMEM IS NOT THE LOAD. A slice whose shmem reaches seatceiling's
    SHMEM_FRACTION of memory.high is held by tmpfs pages that no process owns,
    so ending a process frees none of it; the cure there is to reap or raise.
    An UNREADABLE shmem is not a small one, and it ends nothing.
  * ONE CHILD IS THE LOAD. The biggest candidate holds at least CHILD_FRACTION
    of memory.high by its own RSS. A slice whose memory sits in the agent
    itself, or is spread thin, is not one this repair can relieve.

WHO IS NEVER A CANDIDATE. Every agent in the slice is excluded, and either of
two proofs makes a process an agent: its pid is a row of
`session._proc_claude_census` (every live claude process, subagent children
included), or its OWN kernel-side facts name an agent: `procid.is_claude`
(comm, then the exe link), a comm or exe basename `codex`, or an argv whose
first two words name the claude or codex binary. Every ANCESTOR of an agent
inside the slice is excluded too (a launcher whose death can take the agent
with it). And a candidate must be a DESCENDANT of an agent, the seat's own
child, or it is not this repair's to end.

FAIL CLOSED. A member whose start time, argv, identity or RSS will not read, a
census that failed, a memory.high that is not a number, a confirmation read
that does not show the throttle now, and a candidate whose generation, argv or
cgroup moved between the survey and the signal: each one ends nothing, and
the reason comes back so the wake can say it. The plane's reading only NAMES
the child: at the signal the slice's members are walked again and its
memory.high and shmem read again, and the load taken on that reading must
still be that one child.

THE SIGNAL. Through one pidfd, SIGSTOP first quiesces the surveyed generation;
only once /proc proves it stopped are pid/start/argv/agent identity and the
whole load read again, then SIGTERM and SIGCONT are sent. A refusal resumes the
child helm stopped. SIGKILL follows when the same safe target is still alive
after KILL_GRACE_S. ENDED is said only on a PROVEN exit (the entry gone, the pid
held by a later process, or a corpse); a signal that did not end the child
is said as such, and the spell tries again. The kill is a seam: the default
refuses any tree but the host's own /proc, so an arm that forgets to inject
it signals nothing.
"""
import os
import signal
import sys
import time
from collections import namedtuple

from . import procid, seatceiling

#: The kill switch. `0`, `off` or `no` (any case) turns the repair off; the
#: wake still names the cure, and says that a person must apply it.
SWITCH = "HELM_SEAT_RESCUE"
#: How long a throttle with stalled processes must hold before the repair acts.
GRACE_ENV = "HELM_SEAT_RESCUE_GRACE_S"
GRACE_S = 180
#: SIGTERM, then this long for the process to go, then SIGKILL.
KILL_GRACE_S = 5.0
POLL_S = 0.25
#: The share of memory.high one child must hold by its own RSS to BE the load.
#: The runaway held 79% of its ceiling; an ordinary child holds far under a
#: quarter, and a child under that line frees too little to end a throttle.
CHILD_FRACTION = 0.25
#: Parent hops walked inside one slice before the walk is UNKNOWN.
HOPS = 64
#: argv characters a post shows.
ARGV_CAP = 60
#: Binary basenames that make a process an agent when they lead its argv.
AGENT_BINARIES = ("claude", "codex", "codex.js")

SHMEM, CHILD, AGENT = "shmem", "child", "agent"

#: One member of a slice, as surveyed.
Proc = namedtuple("Proc", "pid ppid start rss argv agent")
#: What holds a slice's memory. `kind` is SHMEM, CHILD, AGENT or None (not
#: attributed; `why` says why). `child` is the biggest candidate (a Proc) or
#: None; `agents` the agent pids found; `agent_rss` the biggest agent's RSS;
#: `shmem_share` the share of memory.high that is shmem, None when unread.
Load = namedtuple("Load", "kind child agents agent_rss shmem_share why")


def on():
    """The switch. On unless it says 0, off or no."""
    return (os.environ.get(SWITCH) or "on").strip().lower() \
        not in ("0", "off", "no")


def grace_s():
    """HELM_SEAT_RESCUE_GRACE_S as whole seconds; a value that is not a
    non-negative integer is the default, never an error."""
    raw = (os.environ.get(GRACE_ENV) or "").strip()
    return int(raw) if raw.isdigit() else GRACE_S


def _text(path, limit=65536):
    try:
        with open(path, "rb") as fh:
            return fh.read(limit).decode("utf-8", "replace")
    except OSError:
        return None


def _stat(pid, proc):
    """(state, ppid, start) off /proc/<pid>/stat, read after its LAST ')'
    because comm may hold spaces and parens, or None when it will not read."""
    text = _text(os.path.join(proc, str(pid), "stat"))
    tail = (text or "").rsplit(")", 1)
    f = tail[1].split() if len(tail) == 2 else []
    if len(f) < 20 or not f[1].isdigit() or not f[19].isdigit():
        return None
    return f[0], int(f[1]), f[19]


def _rss(pid, proc):
    """VmRSS in bytes off /proc/<pid>/status, or None."""
    for line in (_text(os.path.join(proc, str(pid), "status"))
                 or "").splitlines():
        if line.startswith("VmRSS:"):
            parts = line.split()
            return int(parts[1]) * 1024 if len(parts) > 1 \
                and parts[1].isdigit() else None
    return None


def _argv(pid, proc):
    text = _text(os.path.join(proc, str(pid), "cmdline"))
    words = [w for w in (text or "").split("\0") if w]
    return tuple(words) or None


def _agent(pid, proc, census, argv):
    """True / False / None: is this member an agent? None is doubt, and doubt
    ends nothing."""
    if pid in census:
        return True
    comm = _text(os.path.join(proc, str(pid), "comm"))
    if comm is None:
        return None
    comm = comm.strip()
    seen = procid.is_claude(pid, comm.encode(), proc)
    if seen is None:
        return None
    if seen or comm == "codex":
        return True
    exe = procid.exe_of(pid, proc)
    if exe and os.path.basename(exe) in ("claude", "codex"):
        return True
    return any(os.path.basename(w) in AGENT_BINARIES
               or procid._VERSIONED_EXE.search(w) for w in argv[:2])


def survey(pids, proc, census):
    """({pid: Proc}, why) for a slice's members. The first member that will
    not read answers ({}, why): a slice read in part could hide the agent. A
    member that exited since the walk, or is a corpse, is simply not there."""
    out = {}
    for pid in pids:
        st = _stat(pid, proc)
        if st is None:
            if not os.path.isdir(os.path.join(proc, str(pid))):
                continue
            return {}, "pid %d's stat would not read" % pid
        if st[0] in ("Z", "X"):
            continue
        argv = _argv(pid, proc)
        if argv is None:
            return {}, "pid %d's cmdline would not read" % pid
        agent = _agent(pid, proc, census, argv)
        if agent is None:
            return {}, "pid %d cannot be told from an agent" % pid
        rss = _rss(pid, proc)
        if rss is None:
            return {}, "pid %d's RSS would not read" % pid
        out[pid] = Proc(pid, st[1], st[2], rss, argv, agent)
    return out, None


def _ancestors(pid, procs):
    """[pid] of this member's ancestors INSIDE the slice, nearest first, or
    None when the walk ran out of hops (UNKNOWN, never "no ancestors")."""
    out, cur = [], procs[pid].ppid
    for _ in range(HOPS):
        if cur not in procs or cur in out or cur == pid:
            return out
        out.append(cur)
        cur = procs[cur].ppid
    return None


def candidates(procs):
    """[Proc] a repair may end, biggest RSS first: never an agent, never an
    ancestor of one, always a descendant of one."""
    kept = set()
    for pid, p in procs.items():
        if p.agent:
            up = _ancestors(pid, procs)
            kept.add(pid)
            # an agent whose parentage ran out of hops protects EVERYONE
            kept.update(procs if up is None else up)
    out = []
    for pid, p in procs.items():
        up = _ancestors(pid, procs)
        if pid in kept or up is None:
            continue
        if any(procs[a].agent for a in up):
            out.append(p)
    return sorted(out, key=lambda p: (-p.rss, p.pid))


def _high_of(r):
    h = r.high
    return h if isinstance(h, int) and not isinstance(h, bool) and h > 0 \
        else None


def load(r, census, proc):
    """What holds this slice's memory -> Load.

    SHMEM FIRST, and it needs no process read: a slice whose shmem reaches
    SHMEM_FRACTION of memory.high is held by tmpfs whatever its processes do.
    Every other verdict rests on a complete survey of the slice's members.
    `census` is the set of agent pids the census proved, or None when the
    census failed."""
    share = seatceiling.share_of(r.shmem, r.high)
    if share is not None and share >= seatceiling.SHMEM_FRACTION:
        return Load(SHMEM, None, (), None, share, None)
    if census is None:
        return Load(None, None, (), None, share,
                    "the seat census failed, so no agent could be told apart")
    procs, why = survey(r.pids, proc, census)
    if why:
        return Load(None, None, (), None, share, why)
    agents = tuple(sorted(p for p, x in procs.items() if x.agent))
    if not agents:
        return Load(None, None, (), None, share,
                    "no agent process was found in the slice")
    picks = candidates(procs)
    child = picks[0] if picks else None
    agent_rss = max(procs[a].rss for a in agents)
    if share is None:
        return Load(None, child, agents, agent_rss, None,
                    "shmem would not read, so tmpfs cannot be ruled out as "
                    "the load")
    high = _high_of(r)
    if high is None:
        return Load(None, child, agents, agent_rss, share,
                    "memory.high is not a number, so no share of it was taken")
    floor = CHILD_FRACTION * high
    if child is not None and child.rss >= floor:
        return Load(CHILD, child, agents, agent_rss, share, None)
    if agent_rss >= floor:
        return Load(AGENT, child, agents, agent_rss, share, None)
    return Load(None, child, agents, agent_rss, share,
                "no one process holds a quarter of memory.high")


def short_argv(argv):
    text = " ".join(argv or ())
    return text if len(text) <= ARGV_CAP else text[:ARGV_CAP - 1] + "…"


def cure_line(r, ld):
    """The wake's cure, named for what holds the memory. The old line said
    "a larger memory.high" whatever held it, which feeds a runaway child."""
    gb = seatceiling._gb
    if ld is not None and ld.kind == SHMEM:
        return ("The load is shmem, %s (%d%% of memory.high): tmpfs pages no "
                "process owns, so ending a process frees none of it. The cure "
                "is to reap its scratch (`helm scratch gc --apply`) or raise "
                "that slice's memory.high."
                % (gb(r.shmem), round(100 * ld.shmem_share)))
    if ld is not None and ld.kind == CHILD:
        c = ld.child
        auto = ("helm ends it itself once the throttle holds past %ds "
                "(HELM_SEAT_RESCUE)." % grace_s() if on() else
                "Automatic rescue is off (HELM_SEAT_RESCUE), so a person "
                "must.")
        return ("The load is a child of the seat: pid %d (`%s`) at %s RSS. "
                "The cure is to END THAT CHILD (SIGTERM; the seat keeps its "
                "context), never a larger memory.high, which a runaway eats. "
                "%s" % (c.pid, short_argv(c.argv), gb(c.rss), auto))
    if ld is not None and ld.kind == AGENT:
        return ("The load is the agent itself (%s RSS). A throttled seat "
                "still reads alive on every liveness surface, and the "
                "no-restart cure is a larger memory.high on that slice: helm "
                "store get prior:a-throttled-seat-is-alive-beaconed-and-useless"
                % gb(ld.agent_rss))
    return ("What holds the memory was not attributed (%s). Read it before "
            "acting: a larger memory.high relieves an agent that outgrew it, "
            "and feeds a runaway child. helm store get "
            "prior:a-throttled-seat-is-alive-beaconed-and-useless"
            % ((ld.why if ld is not None else None) or "no reading"))


def _host_gate(root, proc):
    """None when this tree may be read now, else why not. A reading of the
    host's own tree honours seatceiling's switch and raises its audit event
    first, exactly as fleet_pressure does."""
    if seatceiling.reads_host(root, proc):
        if not seatceiling.host_read_on():
            return "the host reading is switched off (HELM_SEAT_PRESSURE)"
        sys.audit(seatceiling.HOST_READ_EVENT, root, proc)
    return None


def confirm(r, proc, root, sleep=None, window=seatceiling.RISE_WINDOW_S):
    """(stalled pids, rise, why): the throttle read NOW, both halves. The
    stall and the sampled rise are taken here, right before any signal, and
    never inferred from the counter's size."""
    why = _host_gate(root, proc)
    if why:
        return (), None, why
    before = seatceiling._high_count(r.slice)
    stalled = tuple(seatceiling.stalled(r.pids, proc))
    (sleep or time.sleep)(window)
    after = seatceiling._high_count(r.slice)
    if not isinstance(before, int) or not isinstance(after, int):
        return stalled, None, "the slice's memory.events would not read"
    return stalled, after - before, None


def _in_slice(pid, sl, root, proc):
    raw = seatceiling._read(os.path.join(proc, str(pid), "cgroup"))
    if not isinstance(raw, str) or raw is seatceiling.ABSENT:
        return False
    rel = seatceiling.seat_slice_of(seatceiling._unified(raw))
    if rel is None:
        return False
    return os.path.normpath(os.path.join(root, rel.lstrip("/"))) == \
        os.path.normpath(sl)


def _signal(pid, start, sig, proc, fence=None, sleep=None):
    """(sent, refusal): stop, fence and send through one pidfd.

    A pidfd pins the generation but does not pin argv: the same process may
    exec claude after an ordinary check. SIGSTOP first closes that race. Only
    after /proc proves this generation stopped does `fence` revalidate argv,
    agent identity, slice and load; then the terminal signal and SIGCONT are
    sent through the same pidfd. Every refusal/error resumes a process helm
    stopped. A tree other than the host's /proc is never signalled."""
    if os.path.normpath(str(proc)) != seatceiling.HOST_PROC:
        return None, None
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        return False, None
    except (OSError, AttributeError):
        return None, None
    stopped = False
    try:
        st = _stat(pid, proc)
        if st is None or st[2] != start:
            return False, None
        if st[0] in ("T", "t"):
            return None, "pid %d was already stopped before the fence" % pid
        signal.pidfd_send_signal(fd, signal.SIGSTOP)
        for _ in range(max(1, round(1.0 / POLL_S))):
            st = _stat(pid, proc)
            if st is None or st[2] != start:
                return False, None
            if st[0] in ("T", "t"):
                stopped = True
                break
            (sleep or time.sleep)(POLL_S)
        if not stopped:
            return None, "pid %d would not stop for the final fence" % pid
        why = fence() if fence is not None else None
        if why:
            return None, why
        signal.pidfd_send_signal(fd, sig)
        if sig != signal.SIGKILL:
            signal.pidfd_send_signal(fd, signal.SIGCONT)
        stopped = False
        return True, None
    except ProcessLookupError:
        return False, None
    except (OSError, AttributeError):
        return None, None
    finally:
        if stopped:
            try:
                signal.pidfd_send_signal(fd, signal.SIGCONT)
            except (OSError, AttributeError, ProcessLookupError):
                pass
        os.close(fd)


def _gone(p, proc):
    """Is this generation PROVEN gone? Its /proc entry is absent, a later
    process holds the pid, or it is a corpse. An entry that is there and will
    not read is no proof, and never reads as an exit."""
    st = _stat(p.pid, proc)
    if st is None:
        return not os.path.isdir(os.path.join(proc, str(p.pid)))
    return st[2] != p.start or st[0] in ("Z", "X")


def _wait_gone(p, proc, sleep):
    waited = 0.0
    while waited < KILL_GRACE_S:
        if _gone(p, proc):
            return True
        sleep(POLL_S)
        waited += POLL_S
    return _gone(p, proc)


def end(p, proc, kill=None, sleep=None, fence=None):
    """SIGTERM, then SIGKILL after KILL_GRACE_S -> (ended, how in words).

    `ended` is True only on a PROVEN exit (`_gone`), False when a signal was
    owed and no exit was seen, and None when the generation was gone or the
    final safety fence refused it before a signal. The injected kill seam keeps
    the old four-argument shape; the same fence still runs immediately before
    it so fixture arms exercise the production decision."""
    sleep = sleep or time.sleep

    def send(sig):
        if kill is None:
            return _signal(p.pid, p.start, sig, proc, fence, sleep)
        why = fence() if fence is not None else None
        return (None, why) if why else (kill(p.pid, p.start, sig, proc), None)

    sent, refused = send(signal.SIGTERM)
    if refused:
        return None, refused
    if sent is None:
        return False, "SIGTERM could not be sent"
    if not sent:
        return None, "it was gone before the signal"
    if _wait_gone(p, proc, sleep):
        return True, "SIGTERM, and it exited"
    sent, refused = send(signal.SIGKILL)
    if refused:
        return False, "SIGTERM, then SIGKILL was refused: %s" % refused
    if sent is None:
        return False, "SIGTERM, then SIGKILL could not be sent"
    if _wait_gone(p, proc, sleep):
        return True, ("SIGTERM, then SIGKILL after %gs" % KILL_GRACE_S
                      if sent else "SIGTERM, and it exited")
    return False, ("SIGTERM, then SIGKILL after %gs, and its exit was not "
                   "seen" % KILL_GRACE_S)


def fresh(r, root, proc):
    """(reading, why): the slice as it is NOW for the gates the signal rests
    on — its members walked again, its memory.high and shmem read again — on
    the plane's reading otherwise. The plane's read can be a whole gc pass
    old: an agent born since would not protect its ancestor, and a child that
    shrank, or shmem that grew, would still read as the load."""
    why = _host_gate(root, proc)
    if why:
        return None, why
    members, trouble = seatceiling.slice_members(root, proc, strict=True)
    if trouble:
        return None, trouble
    pids = members.get(os.path.normpath(r.slice))
    if not pids:
        return None, "the slice has no member now"
    high = seatceiling.parse_size(
        seatceiling._read(os.path.join(r.slice, "memory.high")))
    return r._replace(pids=tuple(sorted(pids)), high=high,
                      shmem=seatceiling.stat_value(r.slice, "shmem")), None


def _final_fence(r, expected, root, proc, census):
    """None only while `expected` is still the same non-agent child and load.

    This is called with the pidfd held by the production signal seam. It pays
    the full member/high/shmem/survey read again so a same-start exec, a new
    agent, a migration or a changed load cannot live in the last-check gap."""
    r, why = fresh(r, root, proc)
    if why:
        return why
    ld = load(r, census, proc)
    if ld.kind != CHILD:
        return ld.why or "the load is %s at the final fence" % ld.kind
    pick = ld.child
    if pick.agent:
        return "pid %d became an agent at the final fence" % expected.pid
    if (pick.pid, pick.start, pick.argv) != \
            (expected.pid, expected.start, expected.argv):
        return "the child identity or load changed at the final fence"
    if not _in_slice(expected.pid, r.slice, root, proc):
        return "pid %d left the slice at the final fence" % expected.pid
    return None


def rescue(r, ld, since, now, proc, root, census, kill=None, sleep=None,
           window=seatceiling.RISE_WINDOW_S):
    """(done, why): end this slice's runaway child when every gate holds now.

    `done` is a dict naming the child signalled and how it went, `ended`
    True only on a proven exit and False when the signal did not end it; or
    None with `why` when nothing was signalled. `since` is when the unbroken
    throttled-and-stalled run began, from the spell book. `census` is the
    census's agent pids, or None when it failed.

    THE PLANE'S LOAD NAMES THE CHILD; A LOAD READ NOW LICENSES THE SIGNAL.
    The members, memory.high and shmem are read again (`fresh`), the load is
    taken again on them, and the child is ended only when it is still the
    load and still the one the plane named."""
    if not on():
        return None, "automatic rescue is off (HELM_SEAT_RESCUE)"
    if r.word != seatceiling.THROTTLED or not r.stalled:
        return None, "the slice is not throttled with a process stalled now"
    grace = grace_s()
    if since is None or now - since < grace:
        return None, ("throttled %ds of the %ds grace"
                      % (max(0, now - (since or now)), grace))
    if ld is None or ld.kind != CHILD:
        return None, (ld.why if ld is not None and ld.why else
                      "the load is %s, not one child"
                      % (ld.kind if ld is not None else "unread"))
    r, why = fresh(r, root, proc)
    if why:
        return None, why
    stalled, rise, why = confirm(r, proc, root, sleep, window)
    if why:
        return None, why
    if not stalled or not rise:
        return None, ("the confirmation read did not show the throttle now "
                      "(%d stalled, memory.high events %+d)"
                      % (len(stalled), rise or 0))
    # The confirmation sleeps while it samples memory.events. Every fact that
    # licenses a signal is taken after that window, never carried across it.
    r, why = fresh(r, root, proc)
    if why:
        return None, why
    now_ld = load(r, census, proc)
    if now_ld.kind != CHILD:
        return None, (now_ld.why or "the load is %s now, not one child"
                      % now_ld.kind)
    was, pick = ld.child, now_ld.child
    if (pick.pid, pick.start, pick.argv) != (was.pid, was.start, was.argv):
        return None, "the biggest child changed between the survey and now"
    if not _in_slice(was.pid, r.slice, root, proc):
        return None, "pid %d is no longer in the slice" % was.pid
    fence = lambda: _final_fence(r, pick, root, proc, census)  # noqa: E731
    ended, how = end(pick, proc, kill, sleep, fence)
    if ended is None:
        return None, how
    return {"pid": pick.pid, "rss": pick.rss, "argv": short_argv(pick.argv),
            "how": how, "ended": ended, "agents": list(now_ld.agents),
            "stalled": len(stalled), "rise": rise, "window": window,
            "shmem_share": now_ld.shmem_share,
            "held_s": int(now - since)}, None


def ended_line(done, sl, who):
    """The one line that says what was ended, and why."""
    return ("[helm scratch] ENDED pid %d (`%s`, %s RSS) in %s%s: the slice "
            "was THROTTLED for %ds with %d process%s stalled and memory.high "
            "events +%d in %.2fs, and shmem was only %d%% of memory.high, so "
            "that child's RSS was the load. %s. The seat's agent (pid %s) was "
            "never a candidate. HELM_SEAT_RESCUE=off turns this repair off."
            % (done["pid"], done["argv"], seatceiling._gb(done["rss"]),
               os.path.basename(sl), " for %s" % who if who else "",
               done["held_s"], done["stalled"],
               "es"[:2 * (done["stalled"] != 1)], done["rise"],
               done["window"], round(100 * (done["shmem_share"] or 0)),
               done["how"],
               ", ".join(str(a) for a in done["agents"]) or "?"))


def unended_line(done, sl, who):
    """The one line that says a rescue signalled a child and did NOT end it.
    Never ENDED: the child is still there, and the room must not read that
    the wedge was handled."""
    return ("[helm scratch] COULD NOT END pid %d (`%s`, %s RSS) in %s%s: the "
            "slice was THROTTLED for %ds and that child's RSS is the load, "
            "but %s. helm tries again on each reading while the throttle "
            "holds; until one ends it, the cure is a person's. The seat's "
            "agent (pid %s) was never a candidate."
            % (done["pid"], done["argv"], seatceiling._gb(done["rss"]),
               os.path.basename(sl), " for %s" % who if who else "",
               done["held_s"], done["how"],
               ", ".join(str(a) for a in done["agents"]) or "?"))
