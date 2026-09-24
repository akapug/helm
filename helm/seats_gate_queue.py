#!/usr/bin/env python3
"""helm seats — the whole-suite gate queue: one FIFO position per repository.

THIS MODULE EXISTS BECAUSE ITS BANNER LIED. In the single-file seats.py these
442 lines sat under a banner reading "claims — the advisory TTL lease", and
they are not claims: claims_path() — the actual lease API — did not appear
for another 440 lines. A claim is a per-resource lease HANDED to a named
holder; a gate position is a place in ONE queue shared by every linked
worktree of a repository, reached through git's common directory. Different
identity, different lifetime, different failure mode. Splitting on the banner
would have shipped a module misnamed by its own header forever.

The queue is deliberately capacity-1 until measured load says otherwise, and
a position is bound to an exact (launcher, child) process PAIR rather than a
pid, because a pid is reused and a starttime is not. Orphan detection reads
/proc through seats_common's pid primitives — the same instrument the rest of
the fleet uses, so there is one answer to "is this process still the one that
took the slot" rather than a second census that can disagree with the first.

Public verbs (gate_queue_enqueue / try_start / bind_child / renew /
validate_binding / prepare_finish / finish / terminals / ack_terminal /
recover_orphans / snapshot) have real callers outside this package —
helm/gate.py drives them — so they are API, not internals, and they keep their
names through seats.py's re-export.
"""

import errno
import functools
import hashlib
import os
import signal
import time

from . import chat, pk, vcs
from .seats_common import (_flocked, _get_live_pid_starttime,
                           _get_pid_starttime, _proc_stat_link)

# Whole-suite gates use positions, not handed claim leases. Poll frequency can
# change how quickly a waiter notices its turn, but it can never change order.
# One queue spans every linked worktree of a repository through Git's common
# directory; one active slot is deliberate until measured load justifies two.
GATE_QUEUE_CAPACITY = 1
_GATE_QUEUE_V = 1
_GATE_STATES = frozenset(("waiting", "starting", "running", "finishing"))
def gate_queue_path(repo):
    repo_id, err = _gate_repo_id(repo)
    if err:
        raise ValueError(err)
    return _gate_queue_file(repo_id)
@functools.lru_cache(maxsize=128)
def _gate_repo_id(repo):
    try:
        common = vcs.backend(repo).common_dir(repo)
    except Exception:
        common = None
    if not common:
        return None, "cannot resolve the repository common directory"
    return os.path.realpath(common), None
def _gate_queue_key(repo_id):
    return hashlib.sha256(repo_id.encode("utf-8")).hexdigest()[:24]
def _gate_queue_file(repo_id):
    return os.path.join(chat.chat_dir(),
                        ".gate-fifo.%s.json" % _gate_queue_key(repo_id))
def _gate_queue_load(path, repo_id):
    if not os.path.lexists(path):
        return {"v": _GATE_QUEUE_V, "fence": 0, "repo_id": repo_id,
                "positions": [], "terminals": []}, None
    marker = object()
    raw = pk.read_json(path, marker)
    if raw is marker or not isinstance(raw, dict) \
            or raw.get("v") != _GATE_QUEUE_V \
            or raw.get("repo_id") != repo_id \
            or type(raw.get("fence")) is not int \
            or not isinstance(raw.get("positions"), list) \
            or not isinstance(raw.get("terminals", []), list):
        return None, "gate FIFO state is unreadable or has an unknown schema"
    positions, terminals = raw["positions"], raw.setdefault("terminals", [])
    for rows, terminal in ((positions, False), (terminals, True)):
        ids, seqs = set(), set()
        for row in rows:
            rid, seq = row.get("id") if isinstance(row, dict) else None, \
                row.get("seq") if isinstance(row, dict) else None
            diagnostic = row.get("diagnostic") if isinstance(row, dict) else None
            if not isinstance(row, dict) or not isinstance(rid, str) or not rid \
                    or type(seq) is not int or seq <= 0 or rid in ids \
                    or seq in seqs or row.get("state") not in _GATE_STATES \
                    or type(row.get("pid")) is not int \
                    or type(row.get("starttime")) is not int \
                    or not isinstance(row.get("holder"), str) \
                    or terminal and (
                        not isinstance(diagnostic, dict)
                        or diagnostic.get("kind") not in (
                            "finish", "orphan", "cancel")
                        or not isinstance(diagnostic.get("status"), str)
                        or not isinstance(diagnostic.get("stage"), str)
                        or not isinstance(diagnostic.get("reason"), str)):
                return None, "gate FIFO state is unreadable or malformed"
            ids.add(rid)
            seqs.add(seq)
    seqs = [row["seq"] for row in positions + terminals]
    if raw["fence"] < 0 or seqs and raw["fence"] < max(seqs):
        return None, "gate FIFO fence is stale or malformed"
    return raw, None
def _gate_queue_write(path, state):
    pk.write_json(path, state)
def _gate_pid_state(pid, starttime, proc_dir="/proc"):
    if type(pid) is not int or type(starttime) is not int:
        return None
    try:
        if os.stat(os.path.join(proc_dir, str(pid))).st_uid != os.getuid():
            return None
    except OSError:
        return None
    link = _proc_stat_link(pid, proc_dir=proc_dir, with_state=True)
    if not link or link[0] != starttime or link[2] in ("Z", "X"):
        return None
    return link[2]
def _gate_signal(pid, starttime, *signals, proc_dir="/proc"):
    for signum in signals:
        if not _gate_pid_state(pid, starttime, proc_dir=proc_dir):
            return
        try:
            os.kill(pid, signum)
        except OSError:
            return
def _gate_unbound_supervisors(row, proc_dir="/proc"):
    if proc_dir != "/proc":
        return []
    position, parent = row.get("id"), row.get("pid")
    if not isinstance(position, str) or not position or type(parent) is not int:
        return []
    position, parent = position.encode(), str(parent).encode()
    found = []
    try:
        names = os.listdir(proc_dir)
    except OSError:
        return found
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        try:
            if os.stat(os.path.join(proc_dir, name)).st_uid != os.getuid():
                continue
            with open(os.path.join(proc_dir, name, "cmdline"), "rb") as f:
                argv = [arg for arg in f.read().split(b"\0") if arg]
        except OSError:
            continue
        pairs = set(zip(argv, argv[1:]))
        if (b"--position", position) not in pairs \
                or (b"--launcher", parent) not in pairs \
                or b"--supervise" not in argv \
                or not any(os.path.basename(arg) == b"gatechild.py"
                           for arg in argv):
            continue
        link = _proc_stat_link(pid, proc_dir=proc_dir, with_state=True)
        if link and link[2] not in ("Z", "X"):
            found.append((pid, link[0], link[2]))
    return found
def _gate_position_live(row, proc_dir="/proc", reap=False):
    """Is this queue position still held? REAP IS OFF BY DEFAULT.

    THE DIAGNOSTIC MUST NOT BE DESTRUCTIVE. This answered the
    liveness question by SIGKILLing stopped launchers and then re-reading
    what survived — so `helm gate list`, a READ, killed processes, and its
    first sweep could kill while still seeing the pid and therefore report NO
    orphan. A reader that changes what it is reading cannot be run twice for
    the same answer, which is the one thing a diagnostic is for.

    Reaping now belongs to the queue MUTATIONS — enqueue, try_start,
    prepare_finish, finish — which pass reap=True. Every other caller,
    including ones nobody has audited, gets the pure classifier by DEFAULT:
    the safe direction for an unaudited caller is the one that does LESS.

    Pure classification reads a STOPPED launcher as LIVE, and that is the
    honest answer rather than a weaker one: the process exists and holds the
    slot. It is not an orphan; it is stopped, and saying so is what lets the
    operator decide.
    """
    launcher = _gate_pid_state(row.get("pid"), row.get("starttime"),
                               proc_dir=proc_dir)
    if row.get("state") == "running":
        child = _gate_pid_state(row.get("child_pid"),
                                row.get("child_starttime"), proc_dir=proc_dir)
        # A stopped launcher cannot renew, time out, or finalize. Resume the
        # exact supervisor into its SIGTERM tree-cleanup handler before killing
        # the exact launcher generation; keep the row until both are gone.
        if reap and launcher in ("T", "t"):
            if proc_dir != "/proc":
                return False
            if child:
                _gate_signal(row["child_pid"], row["child_starttime"],
                             signal.SIGCONT, signal.SIGTERM)
            _gate_signal(row["pid"], row["starttime"], signal.SIGKILL)
            return bool(_gate_pid_state(row.get("pid"), row.get("starttime"),
                                        proc_dir=proc_dir)
                        or _gate_pid_state(row.get("child_pid"),
                                           row.get("child_starttime"),
                                           proc_dir=proc_dir))
        if reap and child in ("T", "t"):
            if proc_dir == "/proc":
                _gate_signal(row["child_pid"], row["child_starttime"],
                             signal.SIGCONT, signal.SIGTERM)
                child = _gate_pid_state(row.get("child_pid"),
                                        row.get("child_starttime"),
                                        proc_dir=proc_dir)
            else:
                child = None
        # The child owns execution; the launcher owns receipt finalization.
        # Either exact generation keeps the slot. Only both-dead is orphaned.
        return bool(child or launcher)
    if reap and row.get("state") == "starting" and proc_dir == "/proc" \
            and (not launcher or launcher in ("T", "t")):
        supervisors = _gate_unbound_supervisors(row, proc_dir=proc_dir)
        for pid, starttime, _state in supervisors:
            _gate_signal(pid, starttime, signal.SIGCONT, signal.SIGTERM)
        if launcher in ("T", "t"):
            _gate_signal(row["pid"], row["starttime"], signal.SIGKILL)
        launcher = _gate_pid_state(row.get("pid"), row.get("starttime"),
                                   proc_dir=proc_dir)
        alive = [bool(_gate_pid_state(pid, starttime, proc_dir=proc_dir))
                 for pid, starttime, _state in supervisors]
        return bool(launcher or any(alive))
    if reap and launcher in ("T", "t"):
        if proc_dir != "/proc":
            return False
        _gate_signal(row["pid"], row["starttime"], signal.SIGKILL)
        launcher = _gate_pid_state(row.get("pid"), row.get("starttime"),
                                   proc_dir=proc_dir)
    return bool(launcher)
def _gate_partition(bucket, proc_dir="/proc", reap=False):
    rows, orphaned = [], []
    for row in bucket.get("positions", []):
        (rows if _gate_position_live(row, proc_dir=proc_dir, reap=reap)
         else orphaned).append(row)
    rows.sort(key=lambda row: row["seq"])
    orphaned.sort(key=lambda row: row["seq"])
    return rows, orphaned
def _gate_rows(bucket, proc_dir="/proc", reap=False):
    return _gate_partition(bucket, proc_dir=proc_dir, reap=reap)[0]
def _gate_terminal(row, kind, stage, reason, status="UNKNOWN"):
    """One durable terminal diagnostic, persisted before publication."""
    terminal = dict(row)
    terminal["diagnostic"] = {"kind": kind,
                              "status": str(status or "UNKNOWN"),
                              "stage": str(stage or "UNKNOWN"),
                              "reason": str(reason or "unknown")}
    return terminal


def _gate_record_terminals(bucket, rows, kind="orphan", stage=None,
                           reason="launcher-gone", status="UNKNOWN"):
    """Persist before publication; duplicate delivery is safer than loss."""
    pending = bucket.setdefault("terminals", [])
    by_id = {row["id"]: row for row in pending}
    added = []
    for row in rows:
        prior = by_id.get(row["id"])
        if prior is not None:
            if row.get("announced") and not prior.get("announced"):
                prior["announced"] = row["announced"]
            continue
        terminal = _gate_terminal(
            row, kind, stage if stage is not None else row.get("state"), reason,
            status=status)
        pending.append(terminal)
        by_id[terminal["id"]] = terminal
        added.append(terminal)
    pending.sort(key=lambda row: row["seq"])
    return [dict(row) for row in added]


def _gate_pending(bucket):
    return [dict(row) for row in bucket.get("terminals", [])]


def _gate_queue_txn(repo, mutate, proc_dir="/proc"):
    """Run one strict repository-queue mutation under the stable state lock.

    Ordinary advisory claims historically fail open when flock is unavailable.
    A serializer cannot: two unlocked readers can both call themselves head.
    """
    repo_id, err = _gate_repo_id(repo)
    if err:
        return None, err
    chat._ensure_dir()
    path = _gate_queue_file(repo_id)
    try:
        with _flocked(path + ".lock") as lock:
            if lock.f is None:
                return None, "gate FIFO lock is unavailable"
            state, err = _gate_queue_load(path, repo_id)
            if err:
                return None, err
            before = state.get("positions", [])
            result, changed = mutate(state, state)
            changed = changed or state["positions"] != before
            if changed:
                _gate_queue_write(path, state)
            return result, None
    except OSError as exc:
        return None, "gate FIFO state write failed: %s" % exc
def gate_queue_enqueue(repo, seat, pid=None, proc_dir="/proc"):
    """Record one immutable FIFO position for the exact launcher process."""
    pid = os.getpid() if pid is None else pid
    starttime = _get_pid_starttime(pid, proc_dir=proc_dir)
    if starttime is None:
        return "UNKNOWN", None, "cannot bind the gate position to this process"
    seat = str(seat or "").strip()
    if not seat:
        return "UNKNOWN", None, "cannot resolve the gate holder seat"
    def add(state, bucket):
        rows, orphaned = _gate_partition(bucket, proc_dir=proc_dir, reap=True)
        bucket["positions"] = rows
        _gate_record_terminals(bucket, orphaned)
        pending = [dict(old) for old in bucket["terminals"]]
        state["fence"] += 1
        row = {"id": os.urandom(8).hex(), "seq": state["fence"],
               "holder": seat, "pid": pid, "starttime": starttime,
               "state": "waiting"}
        bucket["positions"].append(row)
        return (dict(row), pending), True

    result, err = _gate_queue_txn(repo, add, proc_dir=proc_dir)
    if err:
        return "UNKNOWN", None, err
    row, orphaned = result
    row["orphaned"] = orphaned
    return "QUEUED", row, None
def gate_queue_try_start(repo, position, pid=None, proc_dir="/proc"):
    """Grant only the oldest live position to its exact launcher."""
    pid = os.getpid() if pid is None else pid
    starttime = _get_pid_starttime(pid, proc_dir=proc_dir)
    if starttime is None:
        return "UNKNOWN", None, "cannot prove the gate launcher process"

    def start(_state, bucket):
        rows, orphaned = _gate_partition(bucket, proc_dir=proc_dir, reap=True)
        bucket["positions"] = rows
        _gate_record_terminals(bucket, orphaned)
        found = _gate_pending(bucket)
        own = next((row for row in rows if row["id"] == position), None)
        changed = bool(orphaned)
        if own is None:
            return ("UNKNOWN", None, found), changed
        if own.get("pid") != pid or own.get("starttime") != starttime:
            return ("UNKNOWN", dict(own), found), changed
        if own["state"] in ("starting", "running"):
            return ("START", dict(own), found), changed
        active = sum(row["state"] in ("starting", "running", "finishing")
                     for row in rows)
        if active >= GATE_QUEUE_CAPACITY or rows[0]["id"] != position:
            return ("WAIT", dict(own), found), changed
        own["state"] = "starting"
        return ("START", dict(own), found), True

    result, err = _gate_queue_txn(repo, start, proc_dir=proc_dir)
    if err:
        return "UNKNOWN", None, err
    state, row, orphaned = result
    if row is not None:
        row["orphaned"] = orphaned
    if state != "UNKNOWN":
        return state, row, None
    return state, row, ("gate position is not owned by this process" if row
                        else "gate position disappeared before it could start")
def gate_queue_bind_child(repo, position, child_pid, launcher_pid=None,
                          proc_dir="/proc"):
    """Bind only this launcher's active slot to the exact suite generation."""
    launcher_pid = os.getpid() if launcher_pid is None else launcher_pid
    launcher_start = _get_live_pid_starttime(launcher_pid, proc_dir=proc_dir)
    child_start = _get_live_pid_starttime(child_pid, proc_dir=proc_dir)
    if launcher_start is None or child_start is None:
        return False, "cannot bind the active gate to the launcher and suite"

    def bind(_state, bucket):
        own = next((row for row in bucket["positions"]
                    if row["id"] == position), None)
        if own is None or own["state"] != "starting" \
                or own.get("pid") != launcher_pid \
                or own.get("starttime") != launcher_start:
            return False, False
        own["state"] = "running"
        own["child_pid"] = child_pid
        own["child_starttime"] = child_start
        return True, True

    ok, err = _gate_queue_txn(repo, bind, proc_dir=proc_dir)
    return (False, err) if err else (bool(ok), None if ok else
                                     "gate position is not awaiting a child")
def gate_queue_renew(repo, position, child_pid, launcher_pid=None,
                     proc_dir="/proc"):
    """Revalidate this launcher's running position and exact live child."""
    launcher_pid = os.getpid() if launcher_pid is None else launcher_pid
    launcher_start = _get_live_pid_starttime(launcher_pid, proc_dir=proc_dir)
    child_start = _get_live_pid_starttime(child_pid, proc_dir=proc_dir)
    if launcher_start is None or child_start is None:
        return False, "the launcher or suite process is no longer alive"

    def renew(_state, bucket):
        own = next((row for row in bucket["positions"]
                    if row["id"] == position), None)
        if own is None or own["state"] != "running" \
                or own.get("pid") != launcher_pid \
                or own.get("starttime") != launcher_start \
                or own.get("child_pid") != child_pid \
                or own.get("child_starttime") != child_start:
            return False, False
        return True, False

    ok, err = _gate_queue_txn(repo, renew, proc_dir=proc_dir)
    return (False, err) if err else (bool(ok), None if ok else
                                     "the active gate binding changed")
def gate_queue_validate_binding(repo, position, child_pid, child_starttime,
                                launcher_pid=None, proc_dir="/proc"):
    """Validate an unchanged exact binding after its supervisor terminates."""
    launcher_pid = os.getpid() if launcher_pid is None else launcher_pid
    launcher_start = _get_live_pid_starttime(launcher_pid, proc_dir=proc_dir)
    if launcher_start is None:
        return False, "the gate launcher is no longer alive"

    def validate(_state, bucket):
        own = next((row for row in bucket["positions"]
                    if row["id"] == position), None)
        if own is None or own["state"] != "running" \
                or own.get("pid") != launcher_pid \
                or own.get("starttime") != launcher_start \
                or own.get("child_pid") != child_pid \
                or own.get("child_starttime") != child_starttime:
            return False, False
        return True, False

    ok, err = _gate_queue_txn(repo, validate, proc_dir=proc_dir)
    return (False, err) if err else (bool(ok), None if ok else
                                     "the active gate binding changed")
def gate_queue_prepare_finish(repo, position, pid=None, proc_dir="/proc",
                              result=None):
    """Hold the slot in FINISHING while its room observation is appended.

    `result` IS THE FINISH LINE'S OWN TEXT, STAMPED IN THE SAME WRITE (task/408,
    review of gate-waiter-orphans-with-no-receipt). The row and the GATE FINISH
    post are two separate acts and SIGKILL cannot be caught, so a launcher can
    die between them: the row stays `finishing` and a later queue operation
    publishes a GATE ORPHAN for an attempt whose result is already on the wall.
    Wording alone cannot fix that — it makes the notice non-contradictory and
    leaves it ambiguous.

    STAMPING COSTS NO EXTRA WRITE, WHICH IS WHY IT IS DONE HERE RATHER THAN
    BETWEEN THE POST AND THE REMOVAL. `result` is derived entirely from
    _finish_position's own arguments, so the caller can compute it BEFORE
    asking for the slot — the stamp then rides the write that already sets
    `finishing`, and no new window is opened. A third write placed between the
    post and the removal would have shrunk one window by creating another.

    THE FAILURE DIRECTION IS THE POINT: a death before this write leaves
    exactly today's row, so the worst case is unchanged rather than worse.
    """
    pid = os.getpid() if pid is None else pid
    starttime = _get_pid_starttime(pid, proc_dir=proc_dir)

    def prepare(_state, bucket):
        own = next((row for row in bucket["positions"]
                    if row["id"] == position), None)
        if own is None or starttime is None or own.get("pid") != pid \
                or own.get("starttime") != starttime:
            return (False, None, _gate_pending(bucket)), False
        others = [row for row in bucket["positions"]
                  if row["id"] != position]
        rows, orphaned = _gate_partition(
            {"positions": others}, proc_dir=proc_dir, reap=True)
        _gate_record_terminals(bucket, orphaned)
        own["state"] = "finishing"
        if result:
            # ONE PRINTABLE LINE, CAPPED. This is read back into a chat notice,
            # so an unbounded or multi-line value would be a way to inject one.
            own["announced"] = str(result).splitlines()[0][:200]
            status = own["announced"].split(None, 1)[0].upper()
            detail = own["announced"].partition(" — ")[2]
            reason = detail or {
                "OK": "receipt-minted", "FAILED": "receipt-minted-red",
                "UNMINTED": "receipt-not-minted",
                "REFUSED": "admission-refused",
                "UNKNOWN": "gate-outcome-unknown",
            }.get(status, "gate-finish")
            _gate_record_terminals(
                bucket, [own], kind="finish", stage="finishing",
                reason=reason, status=status)
            pending = next(row for row in bucket["terminals"]
                           if row["id"] == own["id"])
            if rows:
                pending["next_holder"] = rows[0]["holder"]
        bucket["positions"] = sorted([own] + rows,
                                     key=lambda row: row["seq"])
        return (True, dict(rows[0]) if rows else None,
                [row for row in _gate_pending(bucket)
                 if row["id"] != own["id"]]), True

    result, err = _gate_queue_txn(repo, prepare, proc_dir=proc_dir)
    if err:
        return False, None, [], err
    ok, nxt, orphaned = result
    return ok, nxt, orphaned, None if ok else \
        "gate position is not owned by this process"
def gate_queue_finish(repo, position, pid=None, proc_dir="/proc",
                      terminal=None):
    """Release only the caller's own position and name the next live waiter.

    `terminal` is an optional {kind, stage, reason, status} diagnostic for an
    early exit or an undelivered FINISH. It is persisted in the same locked
    write that removes the position, so a successful cleanup can never erase
    the only account of work it skipped.
    """
    pid = os.getpid() if pid is None else pid
    starttime = _get_pid_starttime(pid, proc_dir=proc_dir)

    def finish(_state, bucket):
        own = next((row for row in bucket["positions"]
                    if row["id"] == position), None)
        if own is not None and (starttime is None or own.get("pid") != pid
                                or own.get("starttime") != starttime):
            return (False, None, _gate_pending(bucket)), False
        remaining = [row for row in bucket["positions"]
                     if row["id"] != position]
        if own is not None and own.get("state") == "finishing":
            rows, orphaned = sorted(remaining, key=lambda row: row["seq"]), []
        else:
            rows, orphaned = _gate_partition(
                {"positions": remaining}, proc_dir=proc_dir, reap=True)
        bucket["positions"] = rows
        _gate_record_terminals(bucket, orphaned)
        if own is not None and terminal:
            _gate_record_terminals(
                bucket, [own], kind=terminal.get("kind", "cancel"),
                stage=terminal.get("stage") or own.get("state"),
                reason=terminal.get("reason") or "launcher-exited",
                status=terminal.get("status") or "UNKNOWN")
        return (own is not None, dict(rows[0]) if rows else None,
                _gate_pending(bucket)), own is not None or bool(orphaned)

    result, err = _gate_queue_txn(repo, finish, proc_dir=proc_dir)
    if err:
        return False, None, [], err
    ok, nxt, orphaned = result
    return ok, nxt, orphaned, None if ok else \
        "gate position is not owned by this process"
def gate_queue_terminals(repo):
    """Pending terminal diagnostics, retained until publication is acked."""
    repo_id, err = _gate_repo_id(repo)
    if err:
        return [], err
    path = _gate_queue_file(repo_id)
    try:
        with _flocked(path + ".lock") as lock:
            if lock.f is None:
                return [], "gate FIFO lock is unavailable"
            state, err = _gate_queue_load(path, repo_id)
            if err:
                return [], err
            return _gate_pending(state), None
    except OSError as exc:
        return [], "gate FIFO terminal read failed: %s" % exc


def gate_queue_ack_terminal(repo, position):
    """Acknowledge one successfully published terminal diagnostic."""
    def ack(_state, bucket):
        before = bucket.get("terminals", [])
        keep = [row for row in before if row.get("id") != position]
        bucket["terminals"] = keep
        return len(keep) != len(before), len(keep) != len(before)

    removed, err = _gate_queue_txn(repo, ack)
    return (False, err) if err else (bool(removed), None)


def gate_queue_recover_orphans(repo, proc_dir="/proc"):
    """Move safely identifiable dead rows into the durable terminal outbox.

    Classification is non-destructive: no process is signalled. The active FIFO
    loses only rows whose exact launcher/child generations are already gone;
    their complete row bytes survive in `terminals` before `positions` changes.
    """
    def recover(_state, bucket):
        rows, orphaned = _gate_partition(bucket, proc_dir=proc_dir)
        bucket["positions"] = rows
        _gate_record_terminals(bucket, orphaned)
        return _gate_pending(bucket), bool(orphaned)

    pending, err = _gate_queue_txn(repo, recover, proc_dir=proc_dir)
    return ([], err) if err else (pending, None)


def gate_queue_orphans(repo, proc_dir="/proc"):
    """The ORPHANED half of the same partition — rows whose launcher is gone.

    WHY THIS EXISTS: every path that publishes a GATE ORPHAN notice today is
    inside a queue OPERATION (gate.py's _finish_position and the admission
    arm). A launcher that dies while nothing else is gating is therefore never
    announced at all — on a quiet fleet the death is silent for hours, and
    silence reads as health. task/407; found in review of
    gate-waiter-orphans-with-no-receipt, and confirmed by enumerating all six
    call sites rather than by reasoning about them.

    IT REUSES `_gate_partition`, THE ONE DETECTOR, and takes its second
    element exactly as `_gate_rows` takes its first. A second enumerator over
    a live population does not merely duplicate the first: it starts at ZERO
    on every edge the first already paid for, and those edges are invisible
    precisely because the original absorbed them.

    PURELY READ-ONLY NOW, AND THIS PARAGRAPH USED TO SAY THE OPPOSITE. It
    warned that `_gate_position_live` SIGKILLs a launcher it finds STOPPED
    before grading it, so any caller of the partition could reap — which was
    TRUE and is the defect: a diagnostic that destroys what it
    reports cannot be run twice for the same answer. Reaping is now a
    parameter defaulting FALSE, this accessor does not pass it, and the
    classifier reads a stopped launcher as LIVE rather than killing it.

    THE WARNING IS DELETED RATHER THAN SOFTENED, because a stale caution is
    worse than none: a reader who trusts it will avoid a verb that is now
    safe, and a reader who checks it will stop trusting the rest of the
    docstring. A review caught this in the same pass as the cure — the code
    changed and its own description did not.
    """
    repo_id, err = _gate_repo_id(repo)
    if err:
        return [], err
    path = _gate_queue_file(repo_id)
    try:
        with _flocked(path + ".lock") as lock:
            if lock.f is None:
                return [], "gate FIFO lock is unavailable"
            state, err = _gate_queue_load(path, repo_id)
            if err:
                return [], err
            return [dict(row) for row in _gate_partition(
                state, proc_dir=proc_dir)[1]], None
    except OSError as exc:
        return [], "gate FIFO state read failed: %s" % exc


def gate_queue_snapshot(repo, proc_dir="/proc"):
    """A strict read-only queue projection for diagnostics and tests."""
    repo_id, err = _gate_repo_id(repo)
    if err:
        return [], err
    path = _gate_queue_file(repo_id)
    try:
        with _flocked(path + ".lock") as lock:
            if lock.f is None:
                return [], "gate FIFO lock is unavailable"
            state, err = _gate_queue_load(path, repo_id)
            if err:
                return [], err
            return [dict(row) for row in _gate_rows(
                state, proc_dir=proc_dir)], None
    except OSError as exc:
        return [], "gate FIFO state read failed: %s" % exc
