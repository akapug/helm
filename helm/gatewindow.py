"""ONE WHOLE-SUITE GATE PER PROJECT LANDING WINDOW, enforced by a refusing door.

THE MEASURED DEFECT. Twelve whole-suite gate runs were launched for four
landed trains carrying ten reviewed tips; six of them were killed by hand as
redundant, because every arriving reviewed tip got its own run stacked on the
previous compose room. A green receipt on the TOP car of a stacked train lands
every car beneath it, so each of those six runs bought nothing and spent a
build box for the length of a whole suite. The rule was already in the typed
store twice — as a premise and as a heuristic — and was injected into the
integrator's own turns while it did the opposite. That is the shape of the
failure worth naming: ADVICE DOES NOT BIND A HABIT. The only thing that has
ever held in this tree is a verb that refuses.

WHY THE EXISTING REFUSALS DO NOT COVER THIS. `helm/gate.py`'s admission ladder
refuses a second suite at a per-HOST limit — it counts processes on the box it
is running on. The redundant runs here were on DIFFERENT boxes (two fab
nodes), each admitted honestly by its own host, and each still redundant with
the others because they were all gating the same project's same landing
window. The window is a PROJECT fact; nothing in the tree measured it.

WHAT A WINDOW IS. One window is one trunk head. Every compose room the
integrator stands for a landing window is built by merging reviewed tips onto
the SAME trunk head; the last room built on that head contains every room
built before it on that head, so its receipt is the only one anybody needs.
When trunk moves, the window closes and a new one opens — that is the control
case, and it is admitted. The same-window refusal is the `trunk` key's; the
default keys the one-gate rule by host (WHICH HOST, below).

WHERE THE FACTS COME FROM, and this is the half a seat cannot be trusted with:

  WHICH ROOMS A RUN COVERS is knowable only at launch. Fab's own in-flight
  records on a node (`~/fab/logs/<id>.phase`, read back by `fab status <host>`)
  carry an id, a phase, a priority and a pgid — they do not carry a repo, a
  room, a tree or a project. So the door keeps its own launch record, written
  BEFORE the launch it is about to allow.

  WHETHER A RUN IS STILL ALIVE is knowable only away from this process. A
  record is never believed about liveness. A keyed job is asked of the
  AUTHORITY that owns it, generation-bound, because a keyed job has no id in
  `fab status` at all; a record the earlier plain-gate door wrote is asked of
  the node that took its fab id. Either way a run the owner reports as finished
  is retired on the spot and refuses nothing.

  So neither half is a seat's memory: the record supplies the mapping the node
  cannot, and the node supplies the liveness the record cannot.

EVERY UNKNOWN IN THAT LADDER KEEPS THE RECORD. Its inputs are shaped so that
"I cannot tell" arrives looking exactly like "nothing is running":

  AN AUTHORITY OR NODE THAT CANNOT BE REACHED. An observation that is UNKNOWN,
  or a `fab status <host>` whose per-host block ended in
  `|| echo "<host>: unreachable"` — on STDOUT, still exiting 0, so a down node
  and an idle node leave fab in the same shape — is not evidence that a suite
  stopped. `parse_inflight` is therefore tri-state and keys on the `IN FLIGHT`
  banner, which is the node speaking: banner with no rows is a measured idle
  node, banner absent is UNKNOWN, and UNKNOWN keeps.

  A DISPATCH WITH NO GENERATION. The submit may have launched; only its answer
  was lost, and nothing can be asked about a run with no generation to name.
  Dropping that record leaves a live suite with nothing recording it, which is
  the same open door by another road, so the window is HELD for a bounded grace
  and then retires itself. Neither WAIT nor SUPERSEDE is reachable for a run
  nobody can name, and the refusal says so instead of printing a cure that
  cannot be run.

  FAB SAYING IT LAUNCHED NOTHING IS NOT THAT CASE, and it is not an unknown at
  all (task/3114). When the build stopped before the node registered the job,
  `fab gate submit` exits 75 with the null authority, AUTHORITY_ABSENT and a
  handle whose generation is null. That answer is read in ONE place,
  `fabgate.not_dispatched`, and only the whole shape counts: the door then
  records nothing, holds nothing, prints fab's reason and exits NOT DISPATCHED
  with the retry. A null generation on any other answer is still HELD above.

A RETIRED RECORD IS NOT A DROPPED ONE. Retired means the owner is DONE with the
run, and done is the moment its receipt either arrived or was lost — so `show`
joins every retired record against the local ledger and reports the ones nothing
bound as STRANDED, with the door that still binds them.

A DIRTY ROOM IS REFUSED BEFORE ANY OF THAT. Fab snapshots tracked and untracked
bytes into a dangling commit, so what it would gate is not the room's head — and
a record claiming a head that was never gated is the input every containment
answer downstream is computed from.

THE TWO DOORS THE REFUSAL PRINTS. A refusal that names no way forward is a wall,
and a wall gets routed around. This one names both: WAIT for the running run
(with the command that follows it), or SUPERSEDE it in one act. Supersede kills
the running run and launches the requesting room in its place, and is allowed
only when the requesting room CONTAINS the killed run's head — otherwise a car
the old run was carrying would silently stop being gated, and the refusal names
that car rather than dropping it.

CONTAINMENT IS ASKED OF GIT, NOT OF A TIMESTAMP. A room standing on an older
tip of the same window is covered by the running room that contains it — the
running receipt will bind it — so it is refused outright rather than gated a
second time.

WHICH HOST, AND ONE GATE PER HOST. A request the window admits is ROUTED
(`gatehost.plan`) to the host where it is expected to finish first — the
median of that host's green land gates of the same mode from this door's own
job logs, plus what is left of the gate running there — and Fab is pinned to it
by FAB_EXCLUDE_HOSTS. A busy host that still finishes first is waited for: the
launch is refused (BUSY_HOST) with the relaunch line, and nothing is stacked on
it. By DEFAULT (HELM_GATE_WINDOW_KEY unset or `host`) the one-gate rule is
keyed by host alone, so two rooms of one trunk head (a train and the same train
less the car `helm train blame` ejected) run at once on two hosts;
HELM_GATE_WINDOW_KEY=trunk is the escape that restores the same-window refusal
below.
"""
import binascii
import json
import math
import os
import re
import shlex
import subprocess
import sys
import time

from . import gatehost, home, pk, vcs

STORE_SUBDIR = os.path.join(".state", "gate-window")
RUNS_NAME = "runs.json"
LOGS_SUBDIR = "logs"
LOCK_NAME = "runs.lock"
STORE_VERSION = 1

# The external dispatch CLI. Named here so a census can see the spawn and so an
# arm can substitute the launcher without reconstructing its argv.
FAB_BINARY = "fab"
LAND_AUTHORITY_OPTION = "--land-authority"
SHADOW_OPTION = "--shadow"

# `fab status <host>` prints its live work under this banner, one run per line:
#     RUNNING      priority=normal  <id> (pgid 1234) — <reason>
# Everything above the banner is a FINISHED run's exit line and must not be
# read as liveness.
_INFLIGHT_BANNER = "IN FLIGHT"
_INFLIGHT_LINE = re.compile(
    r"^(?P<state>[A-Z][A-Z0-9-]*)\s+priority=\S+\s+(?P<id>\S+)\s+\(pgid\s+"
    r"(?P<pgid>\d+)\)")
LIVE_STATES = frozenset(("RUNNING", "QUEUED-KEY", "QUEUED-SLOT", "QUEUED-P0"))

# THE TYPED GATE-JOB BOUNDARY, and why the door does not run plain `fab gate`.
# That client dispatches `fab build` with no --gate-request-key, so the node
# writes no `~/fab/gate-jobs/by-key` record — and fab-gate-reconcile-scan's
# CANDIDATE tier IS that record. A run dispatched the plain way can therefore
# only ever be discovered as UNVERIFIED, a tier with no import door at all,
# while the FETCH and IMPORT legs that would have bound it live in the very
# client that died. Measured once at 48 green minutes, lost.
#
# `fab gate submit` dispatches WITH the key, so the node holds a TERMINAL by-key
# record naming the artifact and the receipt, which is exactly what
# `fab gate --import` binds. The job identity is the product this door buys, and
# it is bought BEFORE anything runs: the job id is `gate-<key>` and the key is a
# hash of the request helm itself builds, so a door that cannot build a request
# refuses with nothing dispatched.
#
# THE ROUTE: `fab gate measure` names the node, its interpreter and the
# installed runner; `helm gate fab contract` (this tree, fabgate.request) turns
# those into the exact keyed request; `fab gate submit` admits or joins it.
MEASURE_TIMEOUT_S = 600
SUBMIT_TIMEOUT_S = 900
OBSERVE_TIMEOUT_S = 180
_TREE = re.compile(r"[0-9a-f]{40}\Z")
_ATOM = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,191}\Z")


# How long a dispatch whose fab id was never seen holds the window.
# Nothing on any node can be asked about a run nobody can name, so the
# only honest states are HELD and EXPIRED; this is the bound between
# them, sized past the longest whole suite the fleet runs.
LOST_ANNOUNCE_GRACE_S = 3600

# WHAT `launch` EXITS WHEN FAB LAUNCHED NOTHING: the gate family's NOT RUN
# code (`gate.EXIT_NOT_RUN_CAPACITY`, and this door's own refusal while a
# running suite holds the window). Nothing ran, no receipt exists, and the
# printed status names the retry.
EXIT_NOT_DISPATCHED = 3

# Where each row the authority retired is logged, with fab's own evidence,
# beside the detached clients' logs.
RETIRED_LOG = "retired.jsonl"

# WHY A LAUNCH WAITS ON A HOST (gatehost): the host this gate is routed to is
# running one already. One gate per host, whatever the window key.
BUSY_HOST = "busy-host"

# FAB'S ROLLOUT DRAIN BUDGET (fab's FAB_ROLLOUT_DRAIN_S default): how long a
# launch fab has started may still wait in its rollout before it registers the
# job on the node.
FAB_ROLLOUT_DRAIN_S = 300
# THE RETIRE FLOOR, 1,200 s. A submit helm's own SUBMIT_TIMEOUT_S killed can
# leave fab's launch running, and it can still register the job until fab's
# rollout drain runs out; before then the node's "no such job" is not yet
# evidence, and after it no launch can still be in the rollout wait.
RETIRE_FLOOR_S = SUBMIT_TIMEOUT_S + FAB_ROLLOUT_DRAIN_S


def store_dir(global_dir=None):
    """The window store's directory under the helm home."""
    return os.path.join(global_dir or home.global_dir(), STORE_SUBDIR)


def runs_path(global_dir=None):
    return os.path.join(store_dir(global_dir), RUNS_NAME)


def read_runs(path):
    """Every recorded launch, or an empty list. Never raises: an unreadable
    store must not wall a landing, and liveness is re-measured anyway."""
    return read_runs_checked(path)[0] or []


_ABSENT = object()


def read_runs_checked(path):
    """(rows, None) or (None, why) — the one read of the window store.

    AN ABSENT STORE AND AN UNREADABLE ONE ARE DIFFERENT FACTS. Absent is a
    helm home where no gate was ever launched: no run, an empty list. A store
    that is not JSON, names another version or holds no runs list cannot say
    whether a gate is running. The door (`read_runs`) folds that to empty
    because an unreadable store must not wall a landing; `show` and the web
    board must say UNKNOWN, because "no gate in flight" over a store nobody
    could read is the answer the owner acts on."""
    try:
        blob = pk.read_json(path, default=_ABSENT, strict=True)
    except Exception as exc:              # noqa: BLE001 — named, never empty
        return None, "the gate-window store %s could not be read (%s)" % (
            path, type(exc).__name__)
    if blob is _ABSENT:
        return [], None
    if not isinstance(blob, dict) or blob.get("v") != STORE_VERSION:
        return None, ("the gate-window store %s is not a version %d store"
                      % (path, STORE_VERSION))
    rows = blob.get("runs")
    if not isinstance(rows, list):
        return None, "the gate-window store %s holds no runs list" % path
    return [r for r in rows if isinstance(r, dict)], None


def write_runs(path, runs):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pk.write_json(path, {"v": STORE_VERSION, "runs": list(runs)})


class _Lock:
    """Read-decide-write is one act. Two integrator launches racing here would
    each read an empty store and each admit itself, which is exactly the defect
    this module exists to end.

    O_EXCL ALONE IS NOT MUTUAL EXCLUSION HERE. Creating the lock and then
    writing the holder's pid leaves an interval in which the lock exists and
    names NOBODY; a second taker arriving in it reads an unparseable
    holder, calls it pid 0, concludes the holder is gone, unlinks the file and
    takes a fresh one — and both callers then believe they hold the window.
    So the content that IDENTIFIES the holder is written to a private temp
    file FIRST and the name is published by `link`, which is the atomic act:
    the lock is never visible without its holder.

    AN UNREADABLE HOLDER IS HELD, NOT GONE. "I cannot tell who holds this" and
    "nobody holds this" are different facts, and only one of them licenses a
    break. A dead NAMED holder is broken, because a crashed launcher must not
    wall the next one forever; an unnamed one refuses and says how to clear it.

    A RELEASE ONLY REMOVES WHAT IT STILL HOLDS. Unlinking the path
    unconditionally would let a caller whose lock has already been taken by
    somebody else delete THAT holder's lock on its way out, so release
    compares the inode it opened with the inode standing at the path.
    """

    def __init__(self, path, pid_alive=None):
        self.path = path
        self.fd = None
        self.held = None
        self.pid_alive = pid_alive or _pid_alive

    def __enter__(self):
        where = os.path.dirname(self.path)
        os.makedirs(where, exist_ok=True)
        for _ in range(2):
            mine = os.path.join(where, ".%s.%d.%s" % (
                os.path.basename(self.path), os.getpid(),
                binascii.hexlify(os.urandom(8)).decode("ascii")))
            fd = os.open(mine, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(fd, b"%d\n" % os.getpid())
                try:
                    os.link(mine, self.path)
                except FileExistsError:
                    os.close(fd)
                    fd = None
                    if self._holder_gone():
                        continue
                    raise RuntimeError(
                        "another launch holds the window lock at %s — if no "
                        "launch is running, remove that file" % self.path)
                self.fd, self.held = fd, os.fstat(fd)
                fd = None
                return self
            finally:
                if fd is not None:
                    os.close(fd)
                try:
                    os.unlink(mine)
                except OSError:
                    pass
        raise RuntimeError("could not take the window lock at %s" % self.path)

    def _holder_gone(self):
        """True only when a NAMED holder is measurably gone."""
        try:
            with open(self.path) as fh:
                raw = fh.read().strip()
            standing = os.stat(self.path)
        except OSError:
            return True                       # it vanished; retry the take
        try:
            holder = int(raw)
        except ValueError:
            return False                      # unreadable is HELD, never gone
        if holder <= 0 or self.pid_alive(holder):
            return False
        try:
            fresh = os.stat(self.path)
            if (fresh.st_dev, fresh.st_ino) == (standing.st_dev,
                                                standing.st_ino):
                os.unlink(self.path)
        except OSError:
            pass
        return True

    def __exit__(self, *_exc):
        if self.fd is None:
            return False
        try:
            standing = os.stat(self.path)
            ours = (standing.st_dev, standing.st_ino) == (self.held.st_dev,
                                                          self.held.st_ino)
        except OSError:
            ours = False
        if ours:
            try:
                os.unlink(self.path)
            except OSError:
                pass
        os.close(self.fd)
        self.fd, self.held = None, None
        return False


def dispatching(pid=None, path=None, proc_dir=None):
    """Whether the landing window's OWN dispatch is asking: the window lock
    is held by process `pid` (this one) or by one of its ancestors.

    WHY A PROCESS ASKS (task/3463 item 14). A SLICED land gate's
    `fab gate submit` asks `helm gate run --repo ROOM --plan --json
    --sliced` to size its cores, and it asks from inside this door's
    `dispatch`, under the lock `launch` holds. The plan question refuses a
    compose room (its suite must go through this door) and a red tree with
    no `--again`; asked by this door's own dispatch, both refusals refused
    the door itself, so every sliced gate of a compose room exited 4 with
    nothing dispatched. The lock names its holder's pid, so the lineage is
    the proof: a plain `fab gate` started beside the door, by anyone, is no
    descendant of the holder, and is refused exactly as before. An absent
    or unreadable lock names nobody's lineage."""
    from . import gate
    lock = os.path.join(os.path.dirname(path or runs_path()), LOCK_NAME)
    try:
        with open(lock, encoding="utf-8") as fh:
            holder = int(fh.read().strip())
    except (OSError, ValueError):
        return False
    pid = os.getpid() if pid is None else pid
    return holder > 0 and (holder == pid or holder in gate._proc_ancestors(
        pid, gate._census_proc_dir(proc_dir)))


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def project_id(room):
    """The identity every worktree of one repository shares: the common git
    directory. Two compose rooms of one project answer the same string; a room
    of a DIFFERENT project answers a different one, so a window never refuses
    across projects. UNKNOWN (an unreadable repo) is its own identity and is
    never equal to anything, so an unreadable room can only ever refuse itself.
    """
    be = vcs.backend(room)
    common = be.common_dir(room)
    return os.path.realpath(common) if common else ""


def parse_inflight(text):
    """{id: state} for the runs a node reports as live, or None for UNKNOWN.

    THE DISTINCTION THIS FUNCTION EXISTS TO MAKE. `fab status <host>` ends
    its per-host block with
    `|| echo "$h: unreachable"` — UNREDIRECTED, so the notice goes to STDOUT,
    and the loop's last command is that echo, so the command still exits 0.
    A DOWN NODE AND AN IDLE NODE THEREFORE LEAVE FAB IN THE SAME SHAPE:
    exit 0, some text, no run lines. Read as "nothing is running", a down node
    retires the record and opens the window at the exact moment nothing can be
    verified — fails OPEN on the one input that must fail closed.

    THE BANNER IS THE PROOF THE NODE ANSWERED. fab's remote script prints
    `IN FLIGHT (priority / phase / reason):` unconditionally once it has
    reached the log directory, so its presence is the node speaking and its
    absence is every way a node does not: unreachable, truncated, a notice
    line, an empty pipe. Banner present with no rows under it is a
    MEASUREMENT of an idle node — an empty dict. Banner absent is UNKNOWN.
    """
    if not isinstance(text, str):
        return None
    live, seen_banner = {}, False
    for line in text.splitlines():
        if not seen_banner:
            seen_banner = line.startswith(_INFLIGHT_BANNER)
            continue
        hit = _INFLIGHT_LINE.match(line.strip())
        if hit and hit.group("state") in LIVE_STATES:
            live[hit.group("id")] = hit.group("state")
    return live if seen_banner else None


def node_inflight(host, runner=None):
    """The ids `host` reports in flight, or None when the node cannot be read.
    None is UNKNOWN and is never read as "nothing is running": an unreachable
    node must not silently open a window a live run still holds. A nonzero
    exit is UNKNOWN, and so is a zero exit whose text is not a node's answer —
    fab reports an unreachable host with exit 0, so the exit code alone cannot
    carry this."""
    run = runner or _fab_status
    try:
        rc, out = run(host)
    except OSError:
        return None
    return parse_inflight(out) if rc == 0 else None


def _fab_status(host):
    proc = subprocess.run([FAB_BINARY, "status", host], capture_output=True,
                          text=True, timeout=60, check=False)
    return proc.returncode, proc.stdout


def handle_of(row):
    """The durable gate-job handle a TYPED record names, or None.

    A legacy record — one written by the plain-`fab gate` door — answers None,
    and every door that only a handle can open reads that as "not this row"
    rather than as a reason to reach a command with None in it.
    """
    from . import fabgate
    if not all(row.get(field) for field in
               ("key", "job_id", "generation", "host")):
        return None
    handle, err = fabgate._handle(
        {"v": fabgate.HANDLE_VERSION, "key": row["key"],
         "job_id": row["job_id"], "host": row["host"],
         "generation": row["generation"]}, row["key"])
    return None if err else handle


def _commands(row):
    from . import fabgate
    handle = handle_of(row)
    return fabgate.commands(handle, row.get("room") or ".") if handle else None


def _kill_argv(row):
    """How this row's run is stopped. A typed job is cancelled through its
    authority; the legacy road kills a fab run id."""
    handle = handle_of(row)
    if handle:
        return [FAB_BINARY, "gate", "kill", "--host", handle["host"],
                "--job", handle["job_id"], "--generation",
                handle["generation"]]
    return [FAB_BINARY, "kill", row["host"], row["run_id"]]


def _fab_kill(row):
    proc = subprocess.run(_kill_argv(row), capture_output=True, text=True,
                          timeout=120, check=False)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def job_state(row, observe=None):
    """One typed row's state as its AUTHORITY reports it, or None for UNKNOWN.

    A KEYED JOB IS NOT IN `fab status`. Its id there is fab's run id, which
    `fab gate submit` never prints — it captures the dispatcher's stdout and
    echoes only the handle — so the only thing that can be asked about a typed
    run is the authority that owns it. That is the stronger road anyway: the
    by-key record outlives every client, which is the whole point of dispatching
    through it.

    None is UNKNOWN and is never read as "over": an authority that could not be
    read is not evidence that a suite stopped.
    """
    from . import fabgate
    call = observe or _fab
    rc, out_text, _err = call(observe_argv(row), OBSERVE_TIMEOUT_S)
    event = last_event(out_text)
    if rc != 0 or not isinstance(event, dict) \
            or event.get("event") != "gate-job" \
            or event.get("disposition") == "UNKNOWN":
        return None
    state = fabgate.snapshot(event.get("snapshot"))
    if state["state"] == "UNKNOWN":
        return None
    key, job_id = event.get("key"), event.get("job_id")
    host, generation, tree = (event.get("node"), event.get("generation"),
                              event.get("tree"))
    identity = event.get("identity")
    if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key) \
            or job_id != "gate-" + key \
            or not _ATOM.fullmatch(str(host or "")) \
            or not fabgate._GENERATION.fullmatch(str(generation or "")) \
            or not _TREE.fullmatch(str(tree or "")) \
            or not isinstance(identity, dict) or identity.get("tree") != tree:
        return None
    for expected, observed in ((row.get("key"), key),
                               (row.get("job_id"), job_id),
                               (row.get("host"), host),
                               (row.get("tree"), tree)):
        if expected and expected != observed:
            return None
    expected_generation = row.get("generation")
    if state["state"] == "SUPERSEDED":
        if not expected_generation or generation == expected_generation:
            return None
    elif generation != expected_generation:
        return None
    return state


def _job_over(state):
    """Has the authority finished with this job? Its own exit or its own
    terminal state, never a timestamp and never a client's absence."""
    from . import fabgate
    return state["exit_state"] == "EXACT" \
        or state["state"] in fabgate.TERMINAL_STATES


def live_runs(runs, inflight=None, observe=None, pid_alive=None, now=None):
    """(live, retired, unknown_hosts), failing toward KEEPING a record.

    A TYPED record — one carrying a gate-job id and a generation — is live iff
    its authority says so. That reading replaces the `fab status` road for it
    entirely, because a keyed job has no id in that listing.

    A record with a fab id is live iff its NODE still reports that id. UNKNOWN
    from a node that could not be read keeps the record: an ssh failure, a
    down box or a truncated answer is not evidence that a suite stopped. Those
    hosts come back in the third value, because a refusal that cannot say
    WHICH of its facts is a reading and which is an unknown sends the operator
    to a node to look for a run that may not be there.

    A record with no id is a dispatch whose announcement was never seen. While
    its launcher is alive it is plainly live. After that nothing on any node
    can be asked about it — there is no id to ask with — so it is HELD for a
    bounded grace rather than dropped: dropping it leaves a live run with
    nothing recording it, which is the same open door by another road. The
    grace is what keeps the hold from becoming permanent. A launch time that
    cannot be read (`_row_ts`) keeps the row live: an age nobody can read is
    never an expired one.
    """
    ask = inflight if inflight is not None else node_inflight
    alive = pid_alive or _pid_alive
    clock = now or time.time
    per_host, live, retired, unknown = {}, [], [], set()
    for row in runs:
        host, run_id = row.get("host"), row.get("run_id")
        if row.get("job_id") and row.get("generation") and host:
            state = job_state(row, observe=observe)
            if state is None:
                unknown.add(host)
                live.append(row)
                continue
            (retired if _job_over(state) else live).append(row)
            continue
        if run_id and host:
            if host not in per_host:
                per_host[host] = ask(host)
            seen = per_host[host]
            if seen is None:
                unknown.add(host)
            (live if seen is None or run_id in seen else retired).append(row)
            continue
        pid = row.get("pid")
        if isinstance(pid, int) and alive(pid):
            live.append(row)
            continue
        # AN AGE NOBODY CAN READ IS NEVER AN EXPIRED ONE: a launch time that
        # is missing, a string, non-finite or a bool keeps the row LIVE, the
        # same reading the retire floor makes, and never raises.
        launched = _row_ts(row)
        held = launched is None or (row.get("announce") == "LOST"
                                    and clock() - launched
                                    < LOST_ANNOUNCE_GRACE_S)
        (live if held else retired).append(row)
    return live, retired, unknown


def _elapsed(seconds):
    seconds = int(max(0, seconds))
    if seconds < 90:
        return "%ds" % seconds
    if seconds < 5400:
        return "%dm%02ds" % (seconds // 60, seconds % 60)
    return "%dh%02dm" % (seconds // 3600, (seconds % 3600) // 60)


def _short(sha):
    return (sha or "?")[:12]


def _row_ts(row):
    """The row's recorded launch time as a finite number, or None when it
    cannot be read. THE ONE READING of a row's age: `live_runs`, the retire
    floor and every render ask this, and None fails closed everywhere."""
    ts = row.get("ts")
    if type(ts) not in (int, float):
        return None
    try:
        launched = float(ts)   # an int no float can hold is not a time either
    except OverflowError:
        return None
    return launched if math.isfinite(launched) else None


def _describe(row, now):
    launched = _row_ts(row)
    return "%s on %s (label %s), running %s, covering %s at %s" % (
        row.get("job_id") or row.get("run_id") or "not-yet-announced",
        row.get("host") or "?",
        row.get("label") or "none",
        _elapsed(now - (now if launched is None else launched)),
        row.get("room") or "?", _short(row.get("head")))


def blocker(request, live, ancestry, per_trunk=True):
    """The live run that already covers this request, and WHY — or (None, "").

    Two ways a request is already covered, in the order the reader should
    think about them:

      SAME-WINDOW: a run is live for this project on the same trunk head. The
      landing window is the unit, so a second whole suite on it is the defect
      this door exists to refuse, whichever way the two rooms contain one
      another. Only while `per_trunk` (HELM_GATE_WINDOW_KEY is `trunk`, the
      escape): keyed by `host`, the default, two rooms of one trunk head run
      at once on two hosts, and the route (`gatehost.plan`) keeps it to one
      per host.

      CONTAINED: the requesting room's head is an ancestor of a live run's
      head. That run's receipt will bind this room's tree when it lands, so
      this request is already answered by a suite that is already running —
      even if trunk moved under it.

    `ancestry(tip, ref)` is the tri-state from the vcs seam. UNKNOWN is not a
    verdict: a containment question nothing could answer never refuses here,
    because the same-window rule is the one that has to hold.
    """
    for row in live:
        if not per_trunk or row.get("project") != request.get("project"):
            continue
        if row.get("trunk") and row.get("trunk") == request.get("trunk"):
            return row, "same-window"
    for row in live:
        if row.get("project") != request.get("project"):
            continue
        if row.get("head") and request.get("head") \
                and ancestry(request["head"], row["head"]) == vcs.ANCESTOR:
            return row, "contained"
    return None, ""


def _busy_why(row, route):
    """Why a gate on `row`'s host means this one waits: the route chose to
    wait on it, or Fab answered a busy host the route did not choose."""
    route = route or {}
    if route.get("wait") is row:
        return "Routed by expected finish: %s." % route["why"]
    if route.get("host"):
        return ("The door routed this gate to %s, but Fab placed it on %s, "
                "which is running one." % (route["host"], row.get("host")))
    return "Fab placed it on %s, which is running one." % row.get("host")


def refusal(request, row, why, now, unknown=(), route=None):
    """The text the door prints instead of spending a second build box.
    `route` is the plan that routed the gate (`gatehost.plan`), whose table a
    BUSY_HOST refusal prints."""
    head = "a whole-suite gate is ALREADY RUNNING for this project"
    if why == BUSY_HOST:
        head = ("a whole-suite gate is ALREADY RUNNING on %s, the host this "
                "gate is routed to" % row.get("host"))
        body = ("One gate per host: a second suite there contends with the "
                "first for its cores, which is what stacked gates cost 13.7 h "
                "of in one week. %s" % _busy_why(row, route))
    elif why == "same-window":
        head += " on this landing window (trunk %s)" % _short(request["trunk"])
        body = ("A green receipt on that run's top car lands every car "
                "beneath it, so a second whole suite on one window buys no "
                "evidence and spends a build box for the length of a suite.")
    else:
        head += ", and it already CONTAINS this room"
        body = ("This room's head %s is an ancestor of that run's head %s: "
                "its receipt binds this tree when it lands, so gating it "
                "again measures the same tree twice."
                % (_short(request["head"]), _short(row.get("head"))))
    lines = [
        "helm gate window: REFUSED — %s." % head,
        "  running: %s" % _describe(row, now),
        "  yours:   %s at %s" % (request.get("room") or "?",
                                 _short(request.get("head"))),
        "  %s" % body,
    ] + list((route or {}).get("lines") or ())
    cmd = _commands(row)
    if not (cmd or row.get("run_id")):
        # A DISPATCH NOBODY CAN NAME. Neither door below is reachable: WAIT
        # has nothing to follow and SUPERSEDE has nothing to kill, so saying
        # them here would be a cure that cannot be run. The hold is bounded,
        # and the way out is the node that owns the job key.
        lines += [
            "  That run carries no generation, so it could not be named: it "
            "cannot be followed, joined",
            "  or killed. The window is HELD for up to %d minutes from its "
            "launch, then retires itself." % (LOST_ANNOUNCE_GRACE_S // 60),
            "  To clear it sooner, `helm gate window show --recover` asks "
            "the node that owns its job key",
            "  and retires the row only when that node affirms it holds no "
            "such job.",
        ]
        return "\n".join(lines)
    if row.get("host") in set(unknown):
        lines += [
            "  Its node %s could not be read just now, so that run is "
            "UNKNOWN, not gone — an" % row["host"],
            "  unreachable box is not evidence that a suite stopped, so the "
            "record stands until the",
            "  node itself says the run is over.",
        ]
    follow = cmd["join"] if cmd else \
        "fab tail %s %s" % (row["host"] or "<host>", row["run_id"])
    wait = [
        "    WAIT      — relaunch this room once %s frees: %s"
        % (row.get("host"), relaunch(request)),
        "                (the running gate: %s)" % follow,
    ] if why == BUSY_HOST else [
        "    WAIT      — follow it to its receipt: %s" % follow,
        "                then compose the NEXT window on the trunk head it "
        "lands.",
    ]
    lines += ["  Two doors are open, and only these two:"] + wait + [
        "    SUPERSEDE — re-run this launch with --supersede: it kills that "
        "run and",
        "                gates THIS room instead. Allowed only if this room "
        "contains",
        "                %s, so no car it was carrying stops being gated."
        % _short(row.get("head")),
    ]
    return "\n".join(lines)


def supersede_plan(request, row, ancestry):
    """(kill, None) when this room may take the running run's place, else
    (None, refusal). The requesting room must CONTAIN the running head: a
    supersede that drops a car is a silently un-gated landing, which is worse
    than the redundant run supersede exists to save."""
    running_head = row.get("head")
    verdict = ancestry(running_head, request.get("head")) if running_head \
        else vcs.UNKNOWN
    if verdict == vcs.ANCESTOR:
        return {"host": row.get("host"),
                "run_id": row.get("job_id") or row.get("run_id"),
                "head": running_head, "row": row}, None
    unknown = verdict == vcs.UNKNOWN
    return None, "\n".join((
        "helm gate window: REFUSED — --supersede would DROP work that is "
        "being gated now.",
        "  running: %s" % _describe(row, time.time()),
        "  yours:   %s at %s" % (request.get("room") or "?",
                                 _short(request.get("head"))),
        "  %s" % ("git could not answer whether this room contains %s, so "
                 "supersede cannot prove nothing is dropped."
                 % _short(running_head) if unknown else
                 "this room does NOT contain %s — that car would stop being "
                 "gated the moment the running run is killed."
                 % _short(running_head)),
        "  Merge that run's room into yours and re-launch, or WAIT: %s"
        % ((_commands(row) or {}).get("join")
           or "fab tail %s %s" % (row.get("host") or "<host>",
                                  row.get("run_id") or "<id>")),
    ))


def _ancestry_for(room):
    be = vcs.backend(room)
    return lambda tip, ref: be.ancestry(room, tip, ref)


def request_for(room, trunk_ref=None, label=None):
    """The window request a room stands for, or (None, refusal)."""
    room = os.path.realpath(room)
    if not os.path.isdir(room):
        return None, "helm gate window: no such room: %s" % room
    be = vcs.backend(room)
    if be.dirty(room):
        return None, (
            "helm gate window: REFUSED — %s is DIRTY, and `fab gate` would "
            "gate a SNAPSHOT of it, not its head.\n"
            "  fab snapshots tracked AND untracked bytes into a dangling "
            "commit so the node can run what you are standing in. Nothing "
            "ever commits that tree, so every commit-time rung was skipped "
            "on its content and it exists on no branch — and the window "
            "record would then claim this run covers a head that was never "
            "gated, which is the fact every containment answer after it is "
            "built on.\n"
            "  Commit the room, then launch. `git -C %s status --short` "
            "names what is uncommitted." % (room, room))
    head = be.head_sha(room)
    if not head:
        return None, ("helm gate window: %s has no resolvable HEAD — a room "
                      "with no head names no tree and can gate nothing" % room)
    trunk = be.head_sha(room, ref=trunk_ref or be.trunk_ref(room))
    if not trunk:
        return None, ("helm gate window: could not resolve this project's "
                      "trunk head from %s — the window is a trunk head, so "
                      "the door cannot tell one window from another without "
                      "it" % room)
    return {"project": project_id(room), "room": room, "head": head,
            "trunk": trunk, "label": label or ""}, None


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def measure_argv(room, tree, land_authority=False, sliced=False):
    """What names the node, its interpreter and fab's installed runner. One
    place builds it, so an arm spies the real call."""
    from . import fabgate
    scope = fabgate.slice_scope() if sliced else fabgate.whole_scope()
    return [FAB_BINARY, "gate", "measure", "--repo", room, "--tree", tree,
            "--scope-json", _canonical(scope), "--json"] \
        + ([LAND_AUTHORITY_OPTION] if land_authority else [])


def land_mode(room, serial=False):
    """(sliced, reason) — which suite this land gate runs.

    SLICED only while the land door would admit the sliced receipt it mints
    (`gate.sliced_land_refusal` answers None: no DISABLE marker, and the
    canary record stands for slices) and the room's tree can run slices.
    Everything else is SERIAL, said with its reason: `--serial`, the marker
    the nightly canary writes on its first divergence (which is how a land
    goes back to serial with no person in the loop), a record that does not
    stand, or a tree that ships no slice runner."""
    if serial:
        return False, "--serial asked for the serial suite"
    from . import gate, gateshadow
    why = gate.sliced_land_refusal()
    if why:
        return False, "the land door refuses a sliced receipt now (%s)" % why
    why = gateshadow.sliceable(room)
    if why:
        return False, why
    return True, ("the canary stands for slices, so this land gate runs as "
                  "slices; `--serial` runs it serial")


# THE LAUNCH LABEL REACHES THE RECEIPT ONLY THROUGH FAB (task/3066). MEASURED:
# `--label train200` stopped here. The label rode the window record and never
# the job: the request identity is a hash of repository, tree, scope,
# interpreter and runner (a label inside it would split one suite into two
# jobs), and Fab runs exactly `dispatch_argv(identity)` on the node, so no
# argv can carry it either. It travels beside the request, the way the
# budgets do: `fab gate submit --label TEXT` records it first-writer and hands
# it to the node as FAB_GATE_LABEL, where `helm gate run` inside a durable job
# mints it (`gate.fab_job_label`). A Fab that cannot take the option would
# refuse the whole submit, so the door passes it only when `fab gate measure`
# says `--label` is among its `submit_options`.
LABEL_OPTION = "--label"


def forwards_option(measured, option):
    """Does the Fab that answered this measure take one submit option?"""
    options = measured.get("submit_options") if isinstance(measured, dict) \
        else None
    return isinstance(options, list) and option in options


def forwards_label(measured):
    """Does the Fab that answered this `gate measure` take `submit --label`?"""
    return forwards_option(measured, LABEL_OPTION)


def submit_argv(room, job, label=None, land_authority=False, shadow=False):
    """The dispatch this door allows: a KEYED gate job, never a plain gate."""
    return [FAB_BINARY, "gate", "submit", "--repo", room,
            "--request-json", _canonical(job), "--json"] \
        + ([LAND_AUTHORITY_OPTION] if land_authority else []) \
        + ([SHADOW_OPTION] if shadow else []) \
        + ([LABEL_OPTION, label] if label else [])


def observe_argv(row):
    """What the AUTHORITY is asked about one recorded run. The node owns job
    existence; this client owns nothing about it."""
    return [FAB_BINARY, "gate", "observe", "--host", row["host"],
            "--job", row["job_id"], "--generation", row["generation"],
            "--json"]


def existence_argv(row):
    """What the AUTHORITY is asked about a row nobody can name: does the node
    that owns its job key hold any record of that key at all?"""
    # `--exists` asks the node about the job KEY alone: a node with no by-key
    # record answers AUTHORITY_ABSENT, and a node that holds the job answers
    # with the job's own generation (task/3115).
    return [FAB_BINARY, "gate", "observe", "--host", row["host"],
            "--job", row["job_id"], "--exists", "--json"]


def import_argv(row):
    """The one door that binds a stranded receipt, as argv rather than a
    string, because this module RUNS it under --recover."""
    return [FAB_BINARY, "gate", "--import", row["host"], row["job_id"],
            "--generation", row["generation"], "--repo", row["room"]]


def logs_dir(runs):
    """The directory of the detached clients' logs, beside the window store.
    Each one ends with Fab's terminal event for its gate, which is where the
    router reads each host's measured speed (`gatehost.history`)."""
    return os.path.join(os.path.dirname(runs), LOGS_SUBDIR)


def log_path(runs, job_id):
    """Where the detached client's whole output lands: beside the window store
    that names it, under the helm home. NOT the caller's terminal — a client
    that outlives its caller has no terminal to write to, and a log nobody can
    name is a client nobody can read."""
    return os.path.join(logs_dir(runs), job_id + ".log")


def follow_argv(row):
    """The detached client's work: JOIN the job to its end, then BIND its
    receipt. Both legs are local, and both must survive the caller.

    ONE SHELL, TWO LEGS, AND THE IMPORT RUNS EITHER WAY. `--join` exits with the
    suite's own status and a RED suite mints a receipt too, so joining the two
    with `&&` would strand every red gate on the node. The commands come from
    fabgate.commands, which is also what the refusal and the STRANDED render
    print, so the operator's copy/paste and the client's own work are one string.
    """
    cmd = _commands(row)
    return None if cmd is None else \
        ["sh", "-c", "%s\n%s\n" % (cmd["join"], cmd["import"])]


def _spawn_detached(argv, log, popen=None):
    """The client, in its OWN SESSION with no terminal and no caller.

    THE MEASURED DEFECT THIS EXISTS FOR. The fetch+import leg lived in a child
    of the launching process, so a backgrounded launch hitting a harness's time
    limit, a TaskStop or earlyoom took the only path to a minted receipt with
    it. start_new_session detaches the process group, so a signal aimed at the
    caller's group never reaches it; stdin is /dev/null and both output streams
    are the log, so nothing it writes needs a terminal that may be gone.
    """
    os.makedirs(os.path.dirname(log), exist_ok=True)
    with open(os.devnull, "rb") as null, open(log, "ab") as sink:
        return (popen or subprocess.Popen)(
            argv, stdin=null, stdout=sink, stderr=subprocess.STDOUT,
            start_new_session=True, close_fds=True, cwd=os.sep)


def _fab(argv, timeout=None, env=None):
    """(rc, stdout, stderr). An OSError is rc None: "fab could not be run" and
    "fab answered badly" are different facts and only one of them is fab's."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, env=env,
                              timeout=timeout or SUBMIT_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, "", "%s: %s" % (type(exc).__name__, exc)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def last_event(text):
    """The last JSON object on a fab gate-job verb's stdout, or None.

    fab's gate verbs answer with one event per line and prefix nothing. STDOUT
    ONLY, and never stderr: submit passes the dispatcher's stderr through, so a
    reader that parsed both streams could bind a warning's shape as the answer.
    """
    row = None
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            found = json.loads(line)
        except ValueError:
            continue
        if isinstance(found, dict):
            row = found
    return row


def _no_identity(why, out_text, err_text):
    return "\n".join((
        "helm gate window: REFUSED — no gate-job identity, so NOTHING was "
        "dispatched.",
        "  %s" % why,
        "  A gate dispatched without a job identity leaves the node holding a "
        "receipt that no",
        "  import door can bind: `fab gate reconcile` can only call it "
        "UNVERIFIED, and a client that",
        "  dies takes the only path to that receipt with it. So the door "
        "refuses here rather than",
        "  spending a whole suite on a run it could not recover.",
        "  fab said: %s" % ((out_text or "").strip() or
                            (err_text or "").strip() or "nothing"),
    ))


def head_tree(room, head):
    """The exact tree object a room's head names, or "". The typed request binds
    a TREE: two commits with one tree are one suite, and the key says so."""
    rc, tree, _err = vcs.backend(room).text(room, "rev-parse",
                                            head + "^{tree}", timeout=30)
    tree = (tree or "").strip().lower()
    return tree if rc == 0 and _TREE.fullmatch(tree) else ""


class Unplaced(str):
    """A refusal that is the ROUTED HOST'S: `fab gate measure` failed with the
    gate pinned there (Fab could not place a gate on it — down, full, or not a
    host it knows), so the door routes again without that host. Every other
    missing identity is the request's own and would fail the same everywhere."""


def _placing(exclude):
    """The keyword the door hands the fab seam so Fab places this gate where
    the route pins it: FAB_EXCLUDE_HOSTS naming every other known host, which
    `fab pick-build` refuses on every placement path. Nothing to exclude
    passes nothing, and Fab's environment is exactly the caller's."""
    if not exclude:
        return {}
    return {"env": dict(os.environ, **{gatehost.EXCLUDE_ENV:
                                       " ".join(exclude)})}


def job_identity(request, fab=None, sliced=False, exclude=()):
    """(identity, refusal) — the keyed request this room stands for, measured
    before anything is dispatched. `exclude` is the route's FAB_EXCLUDE_HOSTS.

    THE JOB ID EXISTS BEFORE THE JOB DOES. It is `gate-<key>` and the key is a
    hash of this request, so the door can refuse for want of an identity while
    no suite is running — which is the only moment a refusal costs nothing.
    """
    from . import fabgate, landwindow, work
    call = fab or _fab
    room = request["room"]
    tree = head_tree(room, request["head"])
    if not tree:
        return None, _no_identity(
            "%s's head %s names no readable tree object, and a gate job binds "
            "a tree." % (room, _short(request["head"])), "", "")
    rc, out_text, err_text = call(
        measure_argv(room, tree, land_authority=True, sliced=sliced),
        MEASURE_TIMEOUT_S, **_placing(exclude))
    measured = last_event(out_text)
    if rc != 0 or not isinstance(measured, dict) \
            or measured.get("event") != "gate-measure" \
            or not isinstance(measured.get("interpreter"), dict) \
            or not isinstance(measured.get("runner"), dict) \
            or not _ATOM.fullmatch(str(measured.get("host") or "")):
        why = _no_identity(
            "`fab gate measure` did not name a host, an interpreter and the "
            "installed runner (exit %s)." % rc, out_text, err_text)
        # A MEASURE FAB RAN AND FAILED is a placement answer about the hosts it
        # was allowed; a Fab that could not be run (rc None) says nothing
        # about any host.
        return None, Unplaced(why) if rc not in (0, None) else why
    if not forwards_option(measured, LAND_AUTHORITY_OPTION):
        return None, _no_identity(
            "the Fab admission door does not advertise %s, so it cannot "
            "retire sliced shadows before choosing this land gate's placement."
            % LAND_AUTHORITY_OPTION, out_text, err_text)
    # A FLAKED TREE CANNOT REUSE ITS RECEIPT. Fab keys the job by this
    # identity, so an unchanged tree would relaunch into the same red result.
    # N recorded FLAKED rows for THIS tree make the next attempt N+1. None,
    # or a store that cannot be read, leaves the field off: without a fresh
    # attempt nothing new can authorize a land.
    root = work.find_root(room)
    counts, unavailable = (landwindow.flake_counts(root) if root else
                           (None, "no project root"))
    attempt = None
    if unavailable:
        print("the flake store could not be read, so this relaunch reuses "
              "the last receipt", file=sys.stderr)
    elif counts.get(tree, 0) >= 1:
        attempt = counts[tree] + 1
    # THE SCOPE, BY NAME (task/3039). This door launches the gate a train
    # LANDS on. The serial scope is Fab's whole scope with no mode flag; the
    # slice scope is the sliced whole suite, which the land door admits only
    # while the canary stands for slices (`land_mode` decided which before
    # this). Neither rides `helm gate run`'s default.
    job, err = fabgate.request(room, tree, fabgate.slice_scope() if sliced
                               else fabgate.whole_scope(),
                               measured["interpreter"], measured["runner"],
                               attempt=attempt)
    if err:
        return None, _no_identity(
            "helm could not build the gate-job request: %s" % err,
            out_text, err_text)
    label = " ".join(str(request.get("label") or "").split())[:120]
    return {"tree": tree, "host": measured["host"], "request": job,
            "mode": "sliced" if sliced else "serial",
            "key": job["key"], "job_id": "gate-" + job["key"],
            "label": label if label and forwards_label(measured) else None}, \
        None


def dispatch(identity, room, fab=None, exclude=()):
    """(handle, disposition, refusal) — the one act that starts a whole suite.
    `exclude` is the route's FAB_EXCLUDE_HOSTS, the pin the measure carried.

    A submit that answers UNKNOWN is not a failed dispatch: fab decides
    same-key existence at its own sink, so the job may be running under the
    name this door already knows. The caller records the identity and holds the
    window rather than admitting a second suite over it.

    A submit fab answers with NOTHING LAUNCHED is neither: its refusal is a
    `fabgate.NotDispatched`, decided by `fabgate.not_dispatched`, and the
    caller records nothing and holds nothing.
    """
    from . import fabgate
    call = fab or _fab
    rc, out_text, err_text = call(submit_argv(
        room, identity["request"], label=identity.get("label"),
        land_authority=True), SUBMIT_TIMEOUT_S, **_placing(exclude))
    event = last_event(out_text)
    nothing = fabgate.not_dispatched(event, identity["key"], rc)
    if nothing:
        return None, None, fabgate.NotDispatched(
            "helm gate window: NOT DISPATCHED — fab launched nothing for %s on "
            "%s, so no suite runs and nothing holds the window.\n  fab said: %s"
            % (identity["job_id"], event["handle"]["host"], nothing))
    if not isinstance(event, dict) or event.get("event") != "gate-job" \
            or event.get("disposition") not in fabgate.DISPOSITIONS:
        return None, None, (
            "helm gate window: `fab gate submit` returned no gate-job event "
            "(exit %s), so this dispatch is UNKNOWN: the job may be running "
            "under %s.\n  fab said: %s"
            % (rc, identity["job_id"],
               (out_text or "").strip() or (err_text or "").strip()
               or "nothing"))
    handle, err = fabgate._handle(event.get("handle"), identity["key"])
    if err or event["disposition"] == "UNKNOWN":
        return None, event["disposition"], (
            "helm gate window: `fab gate submit` answered %s for %s, so this "
            "dispatch is UNKNOWN and the window is HELD.\n  %s\n  Ask the "
            "authority before launching anything else: `helm gate window "
            "show --recover` asks %s whether it holds that job, and retires "
            "this record only if it holds none."
            % (event["disposition"], identity["job_id"],
               fabgate.refusal_text(err, event) if err
               else event.get("reason") or "no exact handle was returned",
               identity["host"]))
    return handle, event["disposition"], None


def _revise(path, token, fields):
    """Apply `fields` to the one record this launch wrote. Matched on the
    launch's own token, never on a pid: pids are reused and two launches from
    one process share one, so a pid match could revise a sibling's row."""
    with _Lock(os.path.join(os.path.dirname(path), LOCK_NAME)):
        runs = read_runs(path)
        for row in runs:
            if row.get("token") == token:
                row.update(fields)
        write_runs(path, runs)


def launch(room, label=None, trunk_ref=None, supersede=False, path=None,
           fab=None, kill=None, inflight=None, observe=None, detach=None,
           pid_alive=None, out=None, now=None, serial=False):
    """The ONE door. Returns (rc, request). Every seam an arm needs to observe
    — the fab calls, the kill, the node read, the authority read, the clock —
    is a parameter whose default is the real thing, so an arm spies the shipped
    path instead of a reimplementation of it."""
    from . import fabgate
    out = out if out is not None else sys.stdout
    path = path or runs_path()
    clock = now or time.time
    request, err = request_for(room, trunk_ref=trunk_ref, label=label)
    if err:
        print(err, file=out)
        return 2, None
    # Fab's admission owner retires every declared shadow before it measures
    # this declared land authority; no detached client-side kill can establish
    # that ordering or close the SCHEDULED-to-RUNNING race.
    ancestry = _ancestry_for(request["room"])
    lock = os.path.join(os.path.dirname(path), LOCK_NAME)
    # DECIDE AND RECORD UNDER ONE LOCK. Releasing between the decision and the
    # record would let two launches each read an empty window and each admit
    # itself — the very defect, reproduced by the door meant to end it. The
    # The identity is measured and the job submitted INSIDE it too: submit is
    # the dispatch, and a dispatch outside the lock is two suites on one window.
    with _Lock(lock, pid_alive=pid_alive):
        live, _retired, unknown = live_runs(
            read_runs(path), inflight=inflight, observe=observe,
            pid_alive=pid_alive, now=now)
        write_runs(path, live)
        row, why = blocker(request, live, ancestry, per_trunk=(
            gatehost.window_key() == gatehost.KEY_TRUNK))
        sliced, mode_why = land_mode(request["room"], serial=serial)

        def routed(failed=()):
            # WHERE THIS GATE FINISHES FIRST, one gate per host (gatehost).
            return gatehost.plan(
                gatehost.SLICED if sliced else gatehost.SERIAL, live,
                logs_dir(path), now=clock(), unread_hosts=unknown,
                failed=failed, capacity_runner=fab or _fab)

        route = None
        if row is None:
            route = routed()
            if route["wait"] is not None:
                row, why = route["wait"], BUSY_HOST
        if row is not None:
            if not supersede:
                print(refusal(request, row, why, clock(), unknown, route),
                      file=out)
                return 3, request
            if not (handle_of(row) or (row.get("run_id") and row["host"])):
                # A KILL NEEDS A NAME. A typed row needs its generation and a
                # legacy row its fab id; a dispatch whose identity was lost has
                # neither, so there is nothing to kill and a supersede here
                # would launch a SECOND suite over a run that is still going.
                print(refusal(request, row, why, clock(), unknown, route),
                      file=out)
                return 3, request
            plan, err = supersede_plan(request, row, ancestry)
            if err:
                print(err, file=out)
                return 3, request
            rc, text = (kill or _fab_kill)(plan["row"])
            if rc != 0:
                print("helm gate window: REFUSED — the running run %s on %s "
                      "did NOT die (fab kill exited %d), so launching now "
                      "would leave two whole suites on one window — the exact "
                      "thing this door exists to prevent.\n  %s"
                      % (plan["run_id"], plan["host"], rc,
                         (text or "").strip()), file=out)
                return 3, request
            print("helm gate window: SUPERSEDED %s on %s — this room contains "
                  "its head %s, so no car it carried stops being gated."
                  % (plan["run_id"], plan["host"], _short(plan["head"])),
                  file=out)
            killed = plan["row"]
            live = [r for r in live
                    if (r.get("job_id"), r.get("run_id"))
                    != (killed.get("job_id"), killed.get("run_id"))]
            route = routed()
        failed = []
        while route["wait"] is None:
            identity, err = job_identity(request, fab=fab, sliced=sliced,
                                         exclude=route["exclude"])
            if not (isinstance(err, Unplaced) and route["host"]):
                break
            # THE ROUTED HOST COULD NOT TAKE IT (down, full, unknown to Fab):
            # route again without it, rather than refuse the train.
            failed.append(route["host"])
            print("helm gate window: %s could not take this gate, so it is "
                  "routed again without it.\n  %s"
                  % (route["host"], err.strip().splitlines()[-1]), file=out)
            route = routed(failed)
        if route["wait"] is not None:
            print(refusal(request, route["wait"], BUSY_HOST, clock(), unknown,
                          route), file=out)
            return 3, request
        if err:
            # NOTHING IS RUNNING YET. The job id is a hash of the request, so
            # the one moment a missing identity costs nothing is before the
            # dispatch — and this is that moment.
            print(err, file=out)
            return 2, request
        # ONE GATE PER HOST AT THE POINT OF TRUTH: the host Fab ANSWERED. The
        # pin excludes only the hosts helm knows, so a Fab that places the
        # gate on a busy host anyway is refused here, before the submit. (Its
        # land-authority lease then lapses on its own term.)
        busy = gatehost.occupant(identity["host"], live)
        if busy is not None:
            print(refusal(request, busy, BUSY_HOST, clock(), unknown, route),
                  file=out)
            return 3, request
        print(routed_text(route, identity["host"]), file=out)
        handle, _disposition, err = dispatch(identity, request["room"],
                                             fab=fab,
                                             exclude=route["exclude"])
        if isinstance(err, fabgate.NotDispatched):
            # FAB LAUNCHED NOTHING, SO NOTHING IS RECORDED: no LOST row, no
            # hold, no client. The store keeps only what was live (less a run
            # a supersede just killed), so the next pass finds the window free.
            write_runs(path, live)
            print("%s\n  retry: %s" % (err, relaunch(request)), file=out)
            return EXIT_NOT_DISPATCHED, request
        token = binascii.hexlify(os.urandom(8)).decode("ascii")
        mine = dict(request)
        mine.update({"pid": None, "ts": clock(), "token": token,
                     "run_id": None, "tree": identity["tree"],
                     "key": identity["key"], "job_id": identity["job_id"],
                     "host": (handle or {}).get("host") or identity["host"],
                     "generation": (handle or {}).get("generation"),
                     # WHETHER THE LABEL RODE THE JOB: a label the record
                     # holds and the receipt will not is said, not implied.
                     "label_forwarded": bool(identity.get("label")),
                     "mode": identity["mode"], "mode_reason": mode_why,
                     # A SUBMIT NOBODY COULD READ STILL MAY HAVE DISPATCHED.
                     # fab decides same-key existence at its own sink, so the
                     # honest states are HELD and EXPIRED, which is exactly
                     # what the lost-announce grace already bounds.
                     "announce": "SEEN" if handle else "LOST"})
        write_runs(path, live + [mine])
    if handle is None:
        print(err, file=out)
        return 4, mine
    # ONE SHADOW PER SERIAL RUN, SCHEDULED AND NEVER SPAWNED HERE: a record
    # `helm gate shadow run` picks up once this suite is over. The serial job
    # above is exactly what it was; the shadow's receipt never enters the
    # land ledger and binds nothing. A SLICED land gate gets none: its serial
    # counterpart is the nightly canary's, run where no land waits on it.
    shadow = None if sliced else _shadow_fenced(out, "schedule", mine)
    # THE CLIENT IS SPAWNED OUTSIDE THE LOCK. It lives for the length of a whole
    # suite, and nothing else may be walled for it.
    log = log_path(path, mine["job_id"])
    try:
        proc = (detach or _spawn_detached)(follow_argv(mine), log)
    except OSError as exc:
        # THE SUITE IS RUNNING AND THE RECORD NAMES IT, so this is not a failed
        # launch: it is a launch with no client to bind the receipt, which is
        # exactly the state `show` now reports and recovers.
        print(dispatched(mine, _disposition), file=out)
        print("helm gate window: WARNING — the client could NOT be detached "
              "(%s), so nothing local will bind this receipt. `helm gate window "
              "show --recover` will, once the node finishes." % exc, file=out)
        _shadow_note(shadow, out)
        return 0, mine
    _revise(path, token, {"pid": proc.pid, "log": log})
    mine.update({"pid": proc.pid, "log": log})
    print(dispatched(mine, _disposition), file=out)
    _shadow_note(shadow, out)
    # THE RECORD OUTLIVES THIS PROCESS, and that is deliberate. The authority is
    # the liveness authority; this client is not. A client that exits, is killed
    # or loses its terminal says nothing about whether the suite it dispatched is
    # still running, and a record that vanished with its client would open the
    # window while a live run still held it. The next read asks the authority and
    # retires the record then.
    return 0, mine


def _shadow_fenced(out, act, *args, **kw):
    """One call into the sliced shadow, FENCED: whatever it raises is said in
    one line and the launch goes on exactly as it would have without it. The
    shadow measures beside the authority and may never stop it."""
    try:
        from . import gateshadow
        return getattr(gateshadow, act)(*args, **kw)
    except Exception as exc:                # noqa: BLE001 — see the docstring
        print("helm gate window: shadow %s skipped (%s: %s); the serial gate "
              "is unaffected" % (act, type(exc).__name__, exc), file=out)
        return None


def _shadow_note(shadow, out):
    if shadow is not None:
        print("  shadow:     a sliced run of this tree is scheduled beside it, "
              "never a land receipt — `helm gate shadow`", file=out)


def routed_text(route, placed):
    """What a dispatching launch says about WHERE: the host, why, and the
    table the route decided on."""
    if route["host"] is None:
        head = "PLACED by Fab on %s — %s" % (placed, route["why"])
    elif route["host"] == placed:
        head = "ROUTED to %s — %s" % (placed, route["why"])
    else:
        head = "ROUTED to %s, but Fab placed it on %s, which runs no gate" % (
            route["host"], placed)
    return "\n".join(["helm gate window: " + head] + list(route["lines"]))


def relaunch(request):
    """The one pasteable line that retries this launch."""
    return " ".join(["helm gate window launch", "--repo",
                     shlex.quote(request.get("room") or ".")]
                    + (["--label", shlex.quote(request["label"])]
                       if request.get("label") else []))


def dispatched(row, disposition):
    """What a launch prints: the job identity, and the two commands that reach
    it. Neither needs this process to be alive."""
    cmd = _commands(row) or {}
    label = row.get("label")
    labelled = () if not label else (
        "  label:      %s (%s)" % (label, "carried to the node; the receipt "
                                   "will name it" if row.get("label_forwarded")
                                   else "recorded here only: this Fab takes no "
                                   "`gate submit --label`, so the receipt's "
                                   "label stays empty"),)
    return "\n".join((
        "helm gate window: DISPATCHED %s (%s) on %s"
        % (row.get("job_id"), disposition, row.get("host")),) + labelled + (
        "  generation: %s" % row.get("generation"),
        "  mode:       %s%s" % (str(row.get("mode") or "serial").upper(),
                                " — " + row["mode_reason"]
                                if row.get("mode_reason") else ""),
        "  room:       %s at %s (trunk %s)"
        % (row.get("room"), _short(row.get("head")), _short(row.get("trunk"))),
        "  client:     detached pid %s, log %s"
        % (row.get("pid"), row.get("log")),
        "  follow:     %s" % cmd.get("status", "?"),
        "  bind:       %s" % cmd.get("import", "?"),
    ))


def ledger_heads(receipts=None):
    """(heads, unavailable) — every head this hub holds a minted receipt for.

    UNREADABLE IS NOT EMPTY. A ledger nobody could read would otherwise accuse
    every record in the store of being stranded, and the cure it names — an
    import — would be run against runs that are already bound.
    """
    from . import gate
    rows, unavailable, _skipped = (receipts or gate.receipts)()
    if unavailable:
        return None, unavailable
    return {row.get("head") for row in rows if row.get("head")}, None


def stranded(retired, heads, observe=None, not_run=None):
    """The finished records this hub has NO receipt for, each with the one door
    that can still bind it.

    THE DEFECT THIS REPLACES. `show` counted these and printed
    "retired record(s) dropped" — the exact words a green 48-minute suite left
    behind, because a record retires when the node is DONE with it, and done is
    the moment its receipt either arrived or was lost. Retired plus no receipt
    for that head is not a dropped record; it is a measurement with nowhere to
    go, and the only honest report of it names the recovery.

    A CANCELED job owes no receipt and is not stranded — a superseded or killed
    run never minted one, and accusing it would send the operator to import
    something that does not exist.

    Nor does a job the NODE REFUSED on its own capacity (task/1740): helm's
    not-run exit and no receipt mean nothing ran. Unlike a cancel nobody chose
    that, and the room still has no gate, so when the caller passes a
    `not_run` list the entry lands there — one authority read, two reports.
    """
    out = []
    for row in retired:
        if heads is not None and row.get("head") in heads:
            continue
        handle = handle_of(row)
        state = job_state(row, observe=observe) if handle else None
        if state is not None and state["state"] == "CANCELED":
            continue
        entry = {"row": row, "handle": handle, "snapshot": state}
        if state is not None and _refused_on_capacity(state):
            if not_run is not None:
                not_run.append(entry)
            continue
        out.append(entry)
    return out


def _refused_on_capacity(state):
    """Did the authority finish this job with helm's NOT RUN exit and SAY it
    holds no receipt? Both, never the number alone: a receipt means a suite
    ran. And the receipt's STATE, never its value: `receipt` is None both for
    ABSENT and for a receipt id the authority could not read (UNKNOWN), and an
    unreadable receipt is not a job that owes none — it stays STRANDED."""
    from . import gate
    return state["exit_state"] == "EXACT" \
        and state["exit"] == gate.EXIT_NOT_RUN_CAPACITY \
        and state.get("receipt_state") == "ABSENT"


def not_run_text(entry, now):
    """What `show` prints for a job its node refused on capacity."""
    from . import gate
    row = entry["row"]
    return "\n".join((
        "NOT RUN   %s" % _describe(row, now),
        "  the node refused this run on its own capacity (exit %d: whole-suite "
        "cap, memory-stall floor or pressured tmp) — nothing ran, so no "
        "receipt is owed for %s" % (gate.EXIT_NOT_RUN_CAPACITY,
                                    _short(row.get("head"))),
        "  relaunch: helm gate window launch --repo %s"
        % (row.get("room") or "<room>")))


def stranded_text(entry, now):
    """What `show` prints instead of silently dropping a retired record."""
    from . import fabgate
    row, state = entry["row"], entry["snapshot"]
    lines = [
        "STRANDED  %s" % _describe(row, now),
        "  the node is DONE with this run and this hub has no receipt for %s"
        % _short(row.get("head")),
    ]
    if state is not None:
        lines.append("  authority: %s exit=%s receipt=%s"
                     % (state["state"],
                        state["exit"] if state["exit_state"] == "EXACT"
                        else state["exit_state"],
                        state["receipt"] or state["receipt_state"]))
    elif entry["handle"] is not None:
        lines.append("  authority: could not be read just now — UNKNOWN, so "
                     "this may yet bind itself")
    if entry["handle"] is not None:
        lines += [
            "  recover: %s"
            % fabgate.commands(entry["handle"], row.get("room") or ".")
            ["import"],
            "  or let this verb do it: helm gate window show --recover",
        ]
    else:
        lines += [
            "  NO GATE-JOB IDENTITY: this run was dispatched the plain way, so "
            "no import door exists",
            "  for it. `fab gate reconcile --host %s --repo %s` will find the "
            "receipt and report it as"
            % (row.get("host") or "<host>", row.get("room") or "<room>"),
            "  authority=UNVERIFIED — discovery without authority — and the "
            "only recovery is a re-run.",
        ]
    return "\n".join(lines)


def recover(entry, runner=None, receipts=None):
    """(rc, text) — bind one stranded receipt through the authoritative door.

    THE IMPORT'S EXIT STATUS IS A CLAIM AND THE LEDGER IS THE AUTHORITY, which
    is the same law `fab gate reconcile` applies to this same door: it re-reads
    rather than believing, because a zero from a command that wrote nothing is
    indistinguishable from a bind.
    """
    row = entry["row"]
    if entry["handle"] is None:
        return 1, ("  NOT RECOVERABLE — there is no import door for a run with "
                   "no job identity, so nothing was attempted.")
    rc, out_text, err_text = (runner or _fab)(import_argv(row),
                                             SUBMIT_TIMEOUT_S)
    heads, unavailable = ledger_heads(receipts)
    if not unavailable and heads and row.get("head") in heads:
        return 0, "  BOUND through the authoritative gate path: %s" \
            % row.get("job_id")
    return 1, ("  STILL OWED — the authoritative path did not bind %s (exit "
               "%s). Do NOT hand-append a receipt and do NOT fall back to a "
               "plain artifact import; re-run the gate.\n  %s"
               % (row.get("job_id"), rc,
                  (err_text or "").strip() or (out_text or "").strip()
                  or "it said nothing"))


def unnamed(row):
    """A row nobody can name: no generation, no fab id, no launcher pid. It
    is exactly what the lost-announce grace holds."""
    return row.get("generation") is None and not row.get("run_id") \
        and row.get("pid") is None


def _fab_words(event, out_text, err_text):
    if isinstance(event, dict):
        snap = event.get("snapshot")
        code = snap.get("reason") if isinstance(snap, dict) else None
        words = "%s%s" % (event.get("reason") or "no reason given",
                          " [%s]" % code if isinstance(code, str) else "")
    else:
        words = (out_text or "").strip() or (err_text or "").strip() \
            or "it said nothing"
    return " ".join(words.split())[:400]


def ask_existence(row, observe=None):
    """(absent, evidence) — does the node that owns this row's job key AFFIRM
    it holds no record of that key? `evidence` is fab's own answer either way,
    and `evidence["why"]` says why a row is kept.

    ONLY `fabgate.job_absent`'s whole shape is absent, and it is keyed on the
    snapshot reason AUTHORITY_ABSENT, never on UNKNOWN alone: a node fab could
    not reach or that refused the read answers UNKNOWN with
    TRANSIENT_AUTHORITY or AUTHORITY_REFUSED, and a node holding the record
    answers JOINED or RECEIPT, with the job's own generation, on exit 0. A fab
    that predates `--exists` refuses the flag at its argument parser (exit 2),
    so it keeps the row too. A fab that could not be run, an answer nothing
    can read, an unreachable node and a job the node does hold all keep the
    row: none is evidence that nothing runs.

    ONLY THE ROW'S RECORDED HOST IS ASKED, and that is the accepted scope. The
    row records the host `fab gate measure` named, which is fab's stored route
    for this job identity, and `fab gate submit` places the key on that same
    stored route, so it is the node that would hold the key's record. The
    residual — a route fab re-picked during the submit — is accepted rather
    than answered by asking every node.
    """
    from . import fabgate
    if not (row.get("key") and row.get("host")
            and row.get("job_id") == "gate-%s" % row["key"]):
        return False, {"why": "it carries no gate-job key, so there is no job "
                              "to ask any node about"}
    argv = existence_argv(row)
    rc, out_text, err_text = (observe or _fab)(argv, OBSERVE_TIMEOUT_S)
    event = last_event(out_text)
    evidence = {"argv": argv, "rc": rc, "event": event}
    said = fabgate.job_absent(event, row["key"], rc)
    if said:
        evidence["said"] = said
        return True, evidence
    if isinstance(event, dict) and event.get("key") == row["key"] \
            and event.get("disposition") in ("JOINED", "RECEIPT") \
            and isinstance(event.get("generation"), str):
        evidence["why"] = "%s HOLDS job %s (generation %s, %s)" % (
            row["host"], row["job_id"], event["generation"],
            event["disposition"])
    else:
        evidence["why"] = ("%s did not affirm that it holds no such job "
                           "(fab gate observe exit %s): %s"
                           % (row["host"], rc,
                              _fab_words(event, out_text, err_text)))
    return False, evidence


def retire(path, row, evidence, now=None):
    """Under the window lock, log fab's evidence and then drop this one
    unnamed row. The log goes FIRST so no row is ever retired without its
    reason on disk; the row is matched by its launch token and job id, and
    only while it is still unnamed, so a row a later launch revised is never
    dropped."""
    log = os.path.join(os.path.dirname(path), LOGS_SUBDIR, RETIRED_LOG)
    with _Lock(os.path.join(os.path.dirname(path), LOCK_NAME)):
        os.makedirs(os.path.dirname(log), exist_ok=True)
        with open(log, "a") as fh:
            fh.write(json.dumps({"ts": (now or time.time)(), "row": row,
                                 "fab": evidence}, sort_keys=True) + "\n")
        write_runs(path, [r for r in read_runs(path) if not (
            unnamed(r) and r.get("token") == row.get("token")
            and r.get("job_id") == row.get("job_id"))])
    return log


def _too_young(launched, when):
    """Why a row under the retire floor is not put to its node yet, or ""."""
    if launched is None:
        return ("its launch time is unreadable, so its age cannot be measured "
                "and it is treated as too young to retire; the node is not "
                "asked")
    if when - launched >= RETIRE_FLOOR_S:
        return ""
    due = launched + RETIRE_FLOOR_S
    return ("it is %s old, under the retire floor of %s (helm's own %ds submit "
            "timeout plus fab's %ds rollout drain): a launch still registering "
            "its job may stand behind it, so the node is not asked yet. It "
            "becomes retirable at %s (in %s)"
            % (_elapsed(when - launched), _elapsed(RETIRE_FLOOR_S),
               SUBMIT_TIMEOUT_S, FAB_ROLLOUT_DRAIN_S, _calendar(due),
               _elapsed(due - when)))


def _calendar(seconds):
    """A wall-clock time as ISO text, or the raw number when no calendar can
    hold it: a finite launch time can still stand past time_t, and a render
    is never a reason to raise over a row the store keeps."""
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))
    except (OverflowError, OSError, ValueError):
        return "%r seconds after the epoch" % seconds


def clear_unnamed(path, live, observe=None, out=None, now=None):
    """The live rows left once every UNNAMED one has been put to its node.

    A row the node affirms holds no such job is RETIRED on the spot and the
    window is free on the next pass; every other answer keeps today's hold
    and says why. A row younger than RETIRE_FLOOR_S, or whose launch time
    cannot be read, is not asked at all and keeps the hold. A row with a
    generation, a fab id or a launcher pid never reaches the question: its
    liveness is asked of its own authority."""
    kept = []
    for row in live:
        if not unnamed(row):
            kept.append(row)
            continue
        when = (now or time.time)()
        young = _too_young(_row_ts(row), when)
        if young:
            print("HELD      %s\n  %s; the window stays held." % (
                _describe(row, when), young), file=out)
            kept.append(row)
            continue
        absent, evidence = ask_existence(row, observe=observe)
        if absent:
            try:
                log = retire(path, row, evidence, now=now)
            except (OSError, RuntimeError) as exc:
                # THE NODE SAID NO SUCH JOB, BUT THE STORE COULD NOT BE
                # WRITTEN (a launch holds the lock, or the disk refused), so
                # the row stands and the next --recover asks again.
                evidence["why"] = "%s holds no such job (%s), but the row " \
                    "could not be retired: %s" % (row["host"],
                                                  evidence["said"], exc)
                absent = False
        if absent:
            print("RETIRED   %s\n  %s holds no job %s, so nothing was "
                  "dispatched and the window is free.\n  fab said (exit %s): "
                  "%s\n  logged: %s" % (_describe(row, when), row["host"],
                                        row["job_id"], evidence["rc"],
                                        evidence["said"], log), file=out)
            continue
        print("HELD      %s\n  %s; the window stays held for up to %d "
              "minutes from its launch." % (
                  _describe(row, when), evidence["why"],
                  LOST_ANNOUNCE_GRACE_S // 60), file=out)
        kept.append(row)
    return kept


def show(path=None, recover_owed=False, observe=None, inflight=None,
         receipts=None, runner=None, out=None, now=None):
    """helm gate window show — what is in flight, and what the node finished
    that this hub never bound. Nonzero when anything is owed, or when the
    store cannot be read (`read_runs_checked`), which is UNKNOWN. With
    `recover_owed` it also binds what is stranded and puts every row nobody
    can name to the node that owns its job key (`clear_unnamed`)."""
    out = out if out is not None else sys.stdout
    path = path or runs_path()
    clock = now or time.time
    rows, unreadable = read_runs_checked(path)
    if unreadable:
        # UNKNOWN, NEVER "NOTHING IN FLIGHT": a store nobody could read cannot
        # say a gate is not running, and --recover must not write over it.
        print("%s — whether a whole-suite gate is in flight is UNKNOWN. Fix "
              "or move that file; the next launch writes a fresh one."
              % unreadable, file=out)
        return 1
    live, retired, unknown = live_runs(rows, inflight=inflight,
                                       observe=observe, now=now)
    if recover_owed:
        live = clear_unnamed(path, live, observe=observe, out=out, now=now)
    when = clock()
    for host in sorted(unknown):
        print("%s could not be read — its runs below are UNKNOWN, not "
              "confirmed" % host, file=out)
    heads, unavailable = ledger_heads(receipts)
    owed, not_run = [], []
    if unavailable:
        print("the local gate ledger could not be read (%s), so no record can "
              "be called stranded: unreadable and empty are different facts."
              % unavailable, file=out)
    else:
        owed = stranded(retired, heads, observe=observe, not_run=not_run)
    for entry in not_run:
        print(not_run_text(entry, when), file=out)
    for entry in owed:
        print(stranded_text(entry, when), file=out)
        if recover_owed:
            _rc, text = recover(entry, runner=runner, receipts=receipts)
            print(text, file=out)
    if not live:
        print("no whole-suite gate is in flight for any project (%d retired "
              "record(s), %d stranded%s)" % (
                  len(retired), len(owed),
                  ", %d NOT RUN on node capacity" % len(not_run)
                  if not_run else ""), file=out)
        return 1 if owed else 0
    for row in live:
        print("%s  trunk %s  %s" % (row.get("project") or "?",
                                    _short(row.get("trunk")),
                                    _describe(row, when)), file=out)
        if _row_ts(row) is None:
            print("  its launch time is unreadable (ts=%s), so it is held: "
                  "an age nobody can read is never expired or retired. Fix "
                  "or remove its row in %s to clear it."
                  % (repr(row.get("ts"))[:60], path), file=out)
    return 1 if owed else 0


def cmd(rest):
    """helm gate window launch|show — the one-suite-per-landing-window door."""
    rest = list(rest or ())
    sub = rest.pop(0) if rest else ""
    if sub == "show":
        return _cmd_show(rest)
    if sub == "launch":
        return _cmd_launch(rest)
    print("usage: helm gate window launch [--repo PATH] [--label TEXT] "
          "[--trunk REF] [--supersede] [--serial]\n"
          "       helm gate window show [--recover]\n"
          "gate window: unknown subverb %r" % sub, file=sys.stderr)
    return 2


def _opts(rest, valued, flags):
    opts, i = {}, 0
    while i < len(rest):
        token = rest[i]
        if token in flags:
            opts[token] = True
        elif token in valued:
            if i + 1 >= len(rest):
                return None, "%s needs a value" % token
            opts[token] = rest[i + 1]
            i += 1
        else:
            return None, "unknown argument %r" % token
        i += 1
    return opts, None


def _cmd_launch(rest):
    # `--serial` IS THE ESCAPE FROM THE SLICED DEFAULT (`land_mode`): a train
    # that touches the runner itself, or an integrator who wants the serial
    # suite, names it.
    opts, err = _opts(rest, {"--repo", "--label", "--trunk"},
                      {"--supersede", "--serial"})
    if err:
        print("gate window launch: %s" % err, file=sys.stderr)
        return 2
    rc, _req = launch(opts.get("--repo") or os.getcwd(),
                      label=opts.get("--label"), trunk_ref=opts.get("--trunk"),
                      supersede=bool(opts.get("--supersede")),
                      serial=bool(opts.get("--serial")))
    return rc


def _cmd_show(rest):
    opts, err = _opts(rest, set(), {"--recover"})
    if err:
        print("gate window show: %s" % err, file=sys.stderr)
        return 2
    return show(recover_owed=bool(opts.get("--recover")))
