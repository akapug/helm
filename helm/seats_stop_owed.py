#!/usr/bin/env python3
"""The Stop guard's owed-row rung, and the one rendering of an owed row that
the rung and both beacon legs share: `owed_phrases` for the rows, and
`owed_clause` for the clause a wake line or a doorbell ring carries.

A dispatch row has two parties and the Stop ladder had a rung for only one.
`seats_stop_signals._dispatch_candidate` tells the SENDER a row is overdue
(NEEDS CHECK-IN); nothing told the RECIPIENT, at an idle stop, that a row was
waiting on it. This module is that half. It lives beside the other stop rungs
(seats_stop_ndp, seats_stop_claims) rather than inside seats_stop_signals,
whose split budget it would exceed.
"""
import hashlib
import os
import time

from . import projscope, record
from .seats_common import SEAT_BYTES, _clip, _scrub
from .seats_cursor import _write_stop_latch
from .seats_stop_signals import _off, _stop_fp_path

OWED_LATCH = "stopowed"
OWED_SHOWN = 5
#: The state word a held `dispatch:` lease wears while nothing proves its
#: row discharged (`lease_in_progress`).
IN_PROGRESS = "IN PROGRESS"
#: How long a delegate may write nothing to its transcript and still count as
#: running when no end mark says otherwise. One tool call inside an agent (a
#: whole-suite run in the foreground) writes nothing for its whole length.
DELEGATE_QUIET_S = 3600


def lease_in_progress(facts, fresh, seat, holder=None):
    """Is this seat's held `dispatch:` lease the in-progress mark? -> bool.

    THE OWED-ROW RUNG ASKS FOR THIS LEASE, so the lease rung may not answer
    it with an act (task/3696). Measured on the integrator seat: the owed-row
    block said "holding a row's lease marks it in progress", and once the
    lease was held the lease rung refused the stop over it ("act per line
    ... helm chat release"), and again whenever the resident refolded and
    its facts read ABSENT, because the advice it fingerprints changed.

    THE DEFAULT IS IN PROGRESS AND THE PROOF IS OWED BY THE REFUSAL. False
    only when the resident's EXACT facts rule the claim STALE for this session
    (`seats_room_advice.dispatch_reading`: its row was answered, cancelled,
    rebound, retired or carried, or is not in the ledger), which is when the
    release is the act a stop owes. ABSENT or STALE facts prove nothing
    discharged, so they leave the mark alone. A lease about to expire is the
    caller's to refuse, as every lease line is. HELM_STOP_GUARD_IN_PROGRESS=0
    restores the refusal.

    THE RULING IS THE SESSION'S, NOT ONE OF ITS NAMES'. The resident rules a
    claim for its holder and for the session's roster seat, and the two can
    be names of one session: a roster alias the render seat has no ruling
    for, or an older name a process still answers to, whose ruling reads a
    row addressed to the roster name as REBOUND. So the claim is STALE only
    when every ruling made for its holder and for the render seat says so;
    one name that still owes the row keeps the mark."""
    if _off("STOP_GUARD_IN_PROGRESS"):
        return False
    from .seats_room_advice import STALE
    adv = facts.get("advice") if isinstance(facts, dict) else None
    names = {str(holder or "").strip(), str(seat or "").strip()} - {""}
    rulings = [adv[w].get("ruling") for w in names
               if isinstance(adv, dict) and isinstance(adv.get(w), dict)]
    return not (getattr(fresh, "exact", False) and rulings
                and all(r == STALE for r in rulings))


def in_progress_reason(cmd, advice):
    """The line an IN PROGRESS lease prints once. It keeps the release
    command, because this line is the only printing of an auto-claimed
    lease's token, and the row's advice rides after it; ` — ` splits the
    three parts as it does on every lease line."""
    return ("IN PROGRESS: the lease marks this row as being worked, so no act "
            "is owed at this stop. Release it once the row is answered — %s%s"
            % (cmd or "no lease token on this row", advice or ""))


def live_delegation(session, transcript, now=None):
    """Is a Workflow run or a background agent this session launched still
    running? -> bool, by listdir and stat only (no subprocess).

    THE RECIPIENT'S OWN WORK, WHERE THE HARNESS KEEPS IT (task/3696). A seat
    that hands an owed row to a Workflow is working it, and before this the
    only way to say so to the owed-row rung was taking the row's lease again,
    measured three times on one row in one afternoon. The session's
    transcript folder (`<dir>/<session>.jsonl` beside `<dir>/<session>/`)
    holds both ends:

      * a Workflow run keeps its agents under `subagents/workflows/<run>/`
        and writes `workflows/<run>.json` when it ends (completed, killed or
        failed), so a run folder with no such file is running;
      * a background agent writes `subagents/agent-<id>.jsonl`, and every
        subagent's end fires SubagentStop, whose hook leaves helm's tombstone
        (`seats_delegation._agent_stopped`), so a transcript with none is
        running.

    Both are bounded by DELEGATE_QUIET_S of quiet, so a session that died
    mid-run does not read as busy for ever. Every read failure is False:
    this only turns the owed-row nag from every idle turn into once per owed
    set, and the nag is the safe side."""
    if not session or not isinstance(transcript, str) \
            or not transcript.endswith(".jsonl"):
        return False
    base = transcript[:-len(".jsonl")]
    sub = os.path.join(base, "subagents")
    now = time.time() if now is None else now

    def fresh(path):
        try:
            return now - os.stat(path).st_mtime <= DELEGATE_QUIET_S
        except OSError:
            return False

    def names(path):
        try:
            return os.listdir(path)
        except OSError:
            return []

    runs = os.path.join(sub, "workflows")
    ended = set(names(os.path.join(base, "workflows")))
    for run in names(runs):
        folder = os.path.join(runs, run)
        if run + ".json" not in ended and (fresh(folder) or any(
                fresh(os.path.join(folder, n)) for n in names(folder))):
            return True
    from .seats_delegation import _agent_stopped
    return any(n.startswith("agent-") and n.endswith(".jsonl")
               and fresh(os.path.join(sub, n))
               and not _agent_stopped(str(session), n[len("agent-"):-6])
               for n in names(sub))


def owed_phrases(rows, now=None):
    """One line per owed row, as its RECIPIENT reads it: the id, what kind
    of work it is, the lane, who sent it, its age, and the one command that
    starts it. Shared by the Stop rung and the beacon's wake line, so the two
    surfaces cannot name the same row two ways. Each phrase is a single line:
    the wake line it rides is one Monitor event."""
    from . import dispatches
    now = time.time() if now is None else now
    out = []
    for r in rows:
        rid = _scrub(str(r.get("id") or ""))[:12]
        lane = _clip(_scrub(" ".join(str(r.get("lane") or "").split())), 48)
        who = _clip(_scrub(dispatches.custodian_of(r)), SEAT_BYTES)
        out.append("%s %s '%s' from @%s, %dm old: helm dispatch triage %s"
                   % (rid, _scrub(str(r.get("kind") or "dispatch"))[:12],
                      lane or "an unnamed lane", who or "?",
                      dispatches._age_s(r, now) // 60, rid))
    return out


#: Rows named on one wake line. The line is one Monitor event and one glance.
OWED_ON_WAKE = 3


def owed_clause(seat, line):
    """The clause ` — you OWE <row>; <row>` naming the rows `seat` owes and
    is not working, at most OWED_ON_WAKE of them, or "" when it owes none.
    The one wording of the owed clause on the beacon's path: the per-row
    stream (`seats_join._owed_wake`) and the doorbell's ring
    (`beacon_doorbell.ring_line`) each put it before the line's fixed
    `(+N waiting — ...)` tail, or at the end of a line that has none.

    A row `line` already names (its own dispatch DM) is not said twice. The
    rows come from `dispatches.owed_to` over the resident's owed frontier: the
    Stop rung's predicate, never a second fold on the beacon's poll path.

    IT NEVER RAISES, AND IT IS NEVER CALLED BETWEEN A COMMIT AND ITS WRITE.
    Both lines are composed first: the per-row line before the wake cursor
    commits, the ring before it is recorded as rung. An exception would cost
    the wake. Every failure, and a stop-facts reading that is not EXACT for
    this seat, is ""."""
    try:
        from . import dispatches, stopfacts
        rows, why = dispatches.owed_to(
            seat, snap=stopfacts.read().owed_pair(seat))
        rows = [r for r in rows if str(r.get("id") or "")[:12] not in line]
        if why or not rows:
            return ""
        said = owed_phrases(rows[:OWED_ON_WAKE])
        more = len(rows) - len(said)
        return " — you OWE %s%s" % (
            "; ".join(said),
            "; +%d more: helm dispatch list --mine --open" % more
            if more > 0 else "")
    except Exception:                    # noqa: BLE001 — see the docstring
        return ""


def _owed_rung(session, room, seat, owed_snapshot, busy):
    """(block, warn) for the rows addressed TO this seat that it owes and is
    not working. The recipient's half of
    `seats_stop_signals._dispatch_candidate`, which speaks only to the seat
    that SENT a row.

    THE INCIDENT (measured). A review row reached a seat while it
    was busy on another review. The seat finished that one and then ended
    three idle turns with "Standing by" while the row sat PENDING VERDICT
    addressed to it. The sender's stop said NEEDS CHECK-IN at every stop. The
    recipient's only surface was the auto-claim whisper: one line, latched
    per session, with no start command. Measured on trunk: its first idle
    stop printed that line; the second and third ended unrefused and said
    nothing about the row (the second was told "inbox clean").

    SO AN IDLE SEAT IS TOLD AT EVERY IDLE TURN, AND THE TURN IS THE LATCH.
    Every other block in this module latches on a state fingerprint, because
    re-firing on an unchanged state teaches a reader to skip the block. Here
    the fingerprint latch IS the defect: it spent the one mention on the
    first stop, and every later turn was a turn lost on an obligation the
    seat could not see. The refusal cannot loop: the harness marks the re-stop
    inside the same turn with `stop_hook_active`, and the ladder returns
    before this rung on that stop. Nor is it a wall: acting discharges it.
    Taking the row's lease (`helm chat claim dispatch:<id8>`, or the
    auto-claim) reads WORKING and quiets it for that lease's life, and a
    verdict, cancel, hold or supersession removes the row from what is owed.

    A SEAT BUSY ON OTHER WORK IS TOLD ONCE PER OWED SET. `busy` is the work
    offer's own idle gate (this session holds a live lease) or a Workflow run
    or background agent this session launched still running
    (`live_delegation`, task/3696), passed as a callable so a seat that owes
    nothing never pays for either read. Such a seat needs to know what is
    queued behind its work, not to be pulled off that work at each turn end.
    An idle stop RECORDS the set it named, so a seat told at an idle turn
    that then hands the row to a Workflow is not told the same set again.
    The set fingerprint re-arms when a row arrives or leaves. An unwritable
    latch degrades that block to a warn, the law `_spiral_gate` follows.

    WHAT IT CANNOT SEE IT DOES NOT CLAIM. Its rows come from the resident's
    owed frontier; a snapshot that is not EXACT for this seat returns nothing,
    and the next idle turn asks again. A row this seat is WORKING (its lease,
    or the dispatched lane) is left to the lease rung's own cadence. FAILS
    OPEN TO NOTHING, like every rung on this path."""
    if _off("STOP_GUARD_OWED") or not seat:
        return None, None
    try:
        from . import dispatches
        rows, unavailable = dispatches.owed_to(seat, snap=owed_snapshot)
        if unavailable or not rows:
            return None, None
        lines = owed_phrases(rows[:OWED_SHOWN])
        more = len(rows) - len(lines)
        body = ("[helm stop-guard] you OWE %d dispatch row%s addressed to "
                "'%s', oldest first. Start each one with its triage, which "
                "prints the brief:\n  %s%s\nHolding a row's lease (helm chat "
                "claim dispatch:<id8>), or a Workflow or background agent "
                "working it, marks it in progress; a verdict or cancel closes "
                "it, and a deliberate wait is `helm dispatch hold <id> "
                "<reason>`."
                % (len(rows), "" if len(rows) == 1 else "s", seat,
                   "\n  ".join(lines),
                   ("\n  +%d more: helm dispatch list --mine --open" % more)
                   if more > 0 else ""))
        fp = hashlib.blake2b("|".join(str(r.get("id")) for r in rows)
                             .encode("utf-8"), digest_size=8).hexdigest()
        path = _stop_fp_path(room, seat, session, kind=OWED_LATCH)
        try:
            with open(path) as f:
                last = f.read().strip()
        except OSError:
            last = None
        if not (busy() if callable(busy) else busy):
            # THE TURN IS STILL THE LATCH HERE; the set is only RECORDED, so
            # a busy stop after it knows this seat has heard it. A record
            # that cannot be written costs one more mention, never the block.
            if last != fp:
                try:
                    _write_stop_latch(path, seat, fp, session=session)
                except projscope.Expired:
                    raise
                except Exception as _swallowed:
                    record.swallow("seats_stop_owed._owed_rung.record",
                                   _swallowed)
            return body, None
        if last == fp:
            return None, None
        try:
            latched = _write_stop_latch(path, seat, fp, session=session)
        except OSError:
            latched = False
        return (body, None) if latched else (None, body)
    except projscope.Expired:
        raise
    except Exception as _swallowed:
        record.swallow("seats_stop_owed._owed_rung", _swallowed)
        return None, None
