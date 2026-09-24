#!/usr/bin/env python3
"""Whether the owner's BOARD could be read, recorded by the
process that read it, and readable by anything that is not his eye.

THE HOLE THIS FILLS. `/api/lr` answers 200 with a named `unavailable` body when
its read raises, and one such answer blanks every card the board fans it out
to. That failure was observable on the owner's screen and NOWHERE else: the
server logged nothing and counted nothing, `helm doctor` probed the chat faucet
and the room cells and never the board, and the watchdog has no leg here. So a
dead card could sit on his phone for as long as he tolerated it, and the only
way any agent could learn of it was for him to say so.

WHY EVERY OUTCOME IS RECORDED AND NOT ONLY THE FAILURES. A failures-only file
answers "did a read fail" and cannot answer "is anything reading the board at
all" — silence in it would mean BOTH a healthy server and no server, which is
the same collapse (unreadable and empty sharing one value) that the reader
exists to end. So each read records its outcome AND the identity of the process
that took it, and `state` asks whether that process is still alive before
anything calls a failure current. A record whose observer is gone is a fact
about the past and says nothing about now.

A RECORD IS NEVER WORTH AN OUTAGE. `record` runs inside the request path of the
very endpoint whose failure it reports, so it swallows its own failures and
answers False: a recorder that raised would take the board down in order to say
the board was down. It also does not write on every read — a heartbeat bounds
the writes while an outcome holds, and a FAILURE always writes, because the
event this file exists for may never be the one a rate limit drops.
"""
import json
import os
import threading
import time

from . import home, pk

SCHEMA = 1
#: The three things a land-pipeline read can be, and they are NOT two. A cold
#: projection answering `warming` is a named, self-curing state — the endpoint
#: read nothing, but nothing failed either — so folding it into `ok` would
#: report a read that did not happen and folding it into `failed` would page an
#: agent about a rebuild that is working.
OUTCOMES = ("ok", "warming", "failed")
#: How long one unchanged outcome may go unwritten. It bounds the file's age so
#: a reader can say how recently a live server actually served the board,
#: without putting a write in front of every poll.
HEARTBEAT_S = 60.0
#: The exception name is written verbatim into a record an operator reads, so
#: it is bounded — and bounded by `cut_marked`, which makes the cut say so.
REASON_CHARS = 200

_WRITE_LOCK = threading.Lock()
_LAST = {"ts": 0.0, "outcome": None}


def path():
    return os.path.join(home.helm_home(), "_global", ".state", "board-read.json")


def _starttime(pid):
    """This process's incarnation key, or None when /proc cannot answer.

    None is honest and usable: `beacons.pid_alive` treats a record with no key
    as alive-but-unverifiable rather than as proof of anything."""
    from . import beacons
    return beacons.proc_starttime(pid)


def _due(outcome, now):
    """Is this outcome worth a write? A FAILURE always is."""
    if outcome == "failed":
        return True
    return (_LAST["outcome"] != outcome
            or (now - _LAST["ts"]) >= HEARTBEAT_S)


def record(outcome, reason=None, now=None):
    """Record one land-pipeline read outcome -> True when a record landed.

    False means "nothing was written", which is the ordinary answer inside the
    heartbeat as well as the answer when the write failed. A caller may not
    read health into either: the file, not the return value, is the record.

    `failures` is a running total and a FLOOR. Two processes serving the same
    helm home read-modify-write this file without a lock between them, so a
    concurrent pair can land as one increment. Under-counting is the safe
    direction for a number whose only use is to say a failure recurred.
    """
    if outcome not in OUTCOMES:
        return False
    now = time.time() if now is None else now
    with _WRITE_LOCK:
        if not _due(outcome, now):
            return False
    failed = outcome == "failed"
    try:
        prev = pk.read_json(path(), None)
        prev = prev if isinstance(prev, dict) else {}
        count = prev.get("failures")
        count = count if isinstance(count, int) and count >= 0 else 0
        pid = os.getpid()
        pk.atomic_write(path(), json.dumps({
            "schema": SCHEMA,
            "ts": now,
            "outcome": outcome,
            "pid": pid,
            "pid_start": _starttime(pid),
            "reason": pk.cut_marked(reason, REASON_CHARS) if failed else None,
            "failures": count + (1 if failed else 0),
            "last_failed_ts": now if failed else prev.get("last_failed_ts"),
            "last_failed_reason": (pk.cut_marked(reason, REASON_CHARS) if failed
                                   else prev.get("last_failed_reason")),
        }), mode=0o600)
    except Exception:          # noqa: BLE001 — see the module docstring: this
        return False           # runs inside the request path it reports on
    with _WRITE_LOCK:
        _LAST.update(ts=now, outcome=outcome)
    return True


def state(now=None, proc_dir=None):
    """What is known about board reads on this host, as a dict.

    Three findings, and the reader's whole job is to keep them apart:

      `readable` False — a record exists and does not parse. UNKNOWN, never a
        clean bill: an unreadable instrument accuses the reading and says
        nothing about the board.
      `recorded` False — nothing has ever recorded a read here. Also UNKNOWN,
        and NOT a fault: a host with no `helm web` serves no board, so there is
        no failure to have.
      `observer` — "live", "gone" or "unknown" for the process that took the
        recorded reading. A `failed` outcome is a CURRENT fault only while its
        observer is live; from a dead observer it is a fact about the past.

    `observer` is "unknown" whenever the liveness read merely failed, which is
    never evidence of death — the tri-state comes from `beacons.pid_alive` and
    is passed through rather than rounded off.
    """
    from . import beacons
    now = time.time() if now is None else now
    out = {"path": path(), "readable": True, "recorded": False,
           "outcome": None, "reason": None, "age_s": None, "pid": None,
           "observer": "unknown", "failures": None,
           "last_failed_age_s": None, "last_failed_reason": None}
    if not os.path.exists(out["path"]):
        return out
    rec = pk.read_json(out["path"], None)
    if not isinstance(rec, dict) or rec.get("schema") != SCHEMA \
            or rec.get("outcome") not in OUTCOMES \
            or not isinstance(rec.get("ts"), (int, float)) \
            or not isinstance(rec.get("pid"), int):
        out["readable"] = False
        return out
    alive = beacons.pid_alive(rec["pid"], rec.get("pid_start"),
                              proc_dir=proc_dir)
    last_failed = rec.get("last_failed_ts")
    failures = rec.get("failures")
    out.update(
        recorded=True,
        outcome=rec["outcome"],
        reason=rec.get("reason"),
        # A CLOCK THAT WENT BACKWARDS MAY NOT MAKE A READING YOUNGER THAN NOW.
        # The age is for the operator's sentence; the verdict rests on the
        # observer and the outcome, never on this number.
        age_s=max(0, int(now - rec["ts"])),
        pid=rec["pid"],
        observer={True: "live", False: "gone"}.get(alive, "unknown"),
        failures=failures if isinstance(failures, int) else None,
        last_failed_age_s=(max(0, int(now - last_failed))
                           if isinstance(last_failed, (int, float)) else None),
        last_failed_reason=rec.get("last_failed_reason"),
    )
    return out
