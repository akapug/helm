"""WHICH ROOMS A PASS LOOKS AT, AND IN WHAT ORDER.

The delivery module answers what a pass FOUND. This one answers where it was
entitled to look: the bounded, fair, per-lane rotation over room identities
that decides a pass's SCOPE before any row is read. It is its own file because
that is a different question from reading a room, and because every consumer
that asks it -- delivery, catch-up, join, the stop guard, the beacon census --
reaches for these three names and nothing else in the delivery module.

EACH CONSUMER LANE OWNS ITS QUEUE. Roster observation must never advance
delivery's or the stop guard's eventual coverage, which is why the ring is
keyed by lane and not by seat alone.

DIRTY ROOMS FIRST, THEN THE ROTATION. A pass hands out foreign rooms in this
order: rooms this lane never handed out, then rooms whose file changed since
the lane last handed them out (the one looked at longest ago first), then
clean rooms from the fair rotation. So a room written since the lane last
looked at it is in the NEXT pass whenever at most `size` rooms are dirty.
When more are, the dirt already there drains at least `size - 1` rooms per
pass in that order, so its wait is ceil(dirty / (size - 1)) passes and does
not scale with the number of rooms. The price is one stat per foreign room
per pass. The room the rotation handed out longest ago is never passed over,
so every room -- a clean one can still owe rows, when a pass was cut short
before reading it -- is handed out within one pass per room in the lane,
however long dirty rooms keep arriving.

`seats_delivery` re-exports `_fair_room_slice`, `_scan_rooms` and
`scan_path`, so no existing import path changed.
"""
import os
import sys

from . import chat, pk
from .seats_common import ROOM_SCAN_CAP, _flocked, _seat_key, dm_lane
from .seats_cursor import _sid8, seat_state_lock
from .seats_identity import seat_scope


#: The ring file's record of each room AS IT WAS when this lane last handed
#: it out, beside `rooms`. An older reader reads only `rooms` and ignores this
#: key, and a writer that drops it costs one lap of plain rotation (every room
#: reads as never handed out), never a room.
LOOKED = "looked"

_GATESLICE_MUTABLE = {
    "_UNLOCKED_RINGS": (
        "the rotation a process keeps while its lane's ring lock cannot be "
        "taken, keyed by ring path; every chat dir a test plants is its own "
        "key"),
}
#: ring path -> the ring this process rotates while that path's lock cannot
#: be taken (task/2520). It is never written to disk: the persisted ring is
#: only ever written under its lock.
_UNLOCKED_RINGS = {}


def scan_path(seat, session=None, lane="deliver"):
    """Per-seat/session/lane overflow-ring state for eventual room coverage."""
    p = os.path.join(chat.chat_dir(), ".scan." + _seat_key(seat))
    s8 = _sid8(session)
    return ".".join(x for x in (p, s8, lane) if x)


def _stamp(room):
    """The room file as one comparable value, or None when it cannot be
    statted. st_mtime_ns is the change signal; the size also catches an
    append landing inside one coarse timestamp tick, and the inode a file
    replaced under its name. None never equals a record, so a room that
    cannot be statted always reads dirty: failing toward looking costs one
    slot, and failing toward clean hides a room nobody proved empty."""
    try:
        st = os.stat(chat.room_path(room))
    except OSError:
        return None
    return "%d:%d:%d" % (st.st_mtime_ns, st.st_size, st.st_ino)


def _fair_room_slice(names, seat, session, size, lane, lent=None):
    """Up to `size` of `names`, DIRTY ROOMS FIRST, then a fair rotation.

    The persisted ring is least-recently-handed-out first; newly discovered
    rooms join it behind the rooms it knows. A pass takes, in order:

      1. rooms with no record -- never handed out, or whose stat failed --
         in ring order;
      2. rooms whose stamp differs from the record taken when they were last
         handed out, in ring order: the one looked at longest ago first;
      3. clean rooms, in ring order, into whatever slots remain.

    THE HEAD OF THE RING IS NEVER PASSED OVER. When dirty rooms fill every
    slot and the room handed out longest ago is not among them, it takes the
    last slot (size >= 2). Nothing is ever inserted ahead of a room already
    in the ring, so every room moves up at least one place per pass and is
    handed out within one pass per room in the ring, however long a flood of
    dirty rooms lasts. The dirty rooms pay for that with at most one slot per
    pass: the dirt already there drains within ceil(dirty / size) passes when
    the head is itself dirty or no clean room waits there, and within
    ceil(dirty / (size - 1)) when one does. A room that turns dirty later
    queues by the same order, so one never handed out goes ahead of it.

    HANDED OUT IS NOT READ. The record is taken at hand-out, before anyone
    reads, so a write after this stat always reads dirty next pass. A caller
    that stops before reading every room it was handed passes `lent` (filled
    with {room: (prior record, this pass's stamp)}) and hands the unread rooms
    back through `_give_back`, or they wait for the rotation.

    One stat per room per pass, taken before the lock so the lock is held no
    longer than before. Each consumer lane owns its ring: roster observation
    must never advance delivery or stop."""
    live = set(names)
    if not live or size <= 0:
        return []
    path = scan_path(seat, session, lane)
    now = {name: _stamp(name) for name in live}
    try:
        with seat_state_lock(seat, session=session) as current, _flocked(path + ".lock"):
            if not current:
                return sorted(live)[:size]
            out, state = _hand_out(pk.read_json(path, {}) or {}, live, now,
                                   size, lent)
            pk.write_json(path, state)
            return out
    except OSError as exc:
        # THE RING LOCK FAILS CLOSED (task/2520): the saved ring is never
        # read-modified-written without it. Nor does the pass fall back to a
        # fixed prefix: the same ALPHABETICAL PREFIX on every pass starves
        # every room sorting after it (a dirty, mentioned one included) until
        # the lock is repaired. The pass rotates THIS PROCESS'S ring instead,
        # by the same order, seeded newest-written first, and lends nothing
        # (the saved ring did not move, so there is nothing to give back). A
        # long-lived waiter covers every room within a lap of its own passes;
        # a short-lived process reaches the rooms written most recently. It
        # says so.
        ring = _UNLOCKED_RINGS.get(path) or {"rooms": sorted(
            live, key=lambda r: (now[r] is not None, _newest(now[r]), r))}
        out, _UNLOCKED_RINGS[path] = _hand_out(ring, live, now, size, None)
        print("[helm] room rotation state unavailable (%s) — this pass "
              "rotates in this process's memory, newest-written rooms first, "
              "and the lane's saved rotation does not advance; a short-lived "
              "process reaches only the rooms written most recently, so "
              "coverage is eventual only across a long-lived waiter's passes "
              "until the rotation lock can be taken again"
              % type(exc).__name__, file=sys.stderr)
        return out


def _newest(stamp):
    """Sort key: the most recently written room first (a stamp's mtime)."""
    try:
        return -int(str(stamp).split(":", 1)[0])
    except ValueError:
        return 0


def _hand_out(state, live, now, size, lent):
    """(rooms handed out, the ring after) — the order `_fair_room_slice`
    documents, over a ring read from disk or kept in memory."""
    saved = state.get("rooms")
    saved = saved if isinstance(saved, list) else []
    looked = state.get(LOOKED)
    looked = dict(looked) if isinstance(looked, dict) else {}
    ring = []
    seen = set()
    for name in saved:
        if isinstance(name, str) and name in live and name not in seen:
            ring.append(name)
            seen.add(name)
    ring.extend(sorted(live - seen))
    dirty = [r for r in ring
             if now[r] is None or looked.get(r) != now[r]]
    dirty.sort(key=lambda r: r in looked)   # stable: keeps ring order
    out = dirty[:size]
    if len(out) < size:
        held = set(dirty)
        out += [r for r in ring if r not in held][:size - len(out)]
    elif size > 1 and ring[0] not in out:
        out[-1] = ring[0]
    for r in out:
        if lent is not None:
            lent[r] = (looked.get(r), now[r])
        if now[r] is None:
            looked.pop(r, None)
        else:
            looked[r] = now[r]
    handed = set(out)
    ring = [r for r in ring if r not in handed] + out
    return out, {"rooms": ring, LOOKED: {
        r: looked[r] for r in ring if r in looked}}


def _give_back(rooms, seat, session, lane, lent, hit=None):
    """Return rooms a pass was handed and never read to the state they had.

    A pass that stops at its first delivery never reads the rooms after the
    hit, and the hit room itself may hold more rows (one event per pass). The
    slice recorded all of them as looked at, so without this they would read
    clean and wait a lap. Each room in `rooms` that `lent` names gets back the
    record it had before this pass; the `hit` room loses its record, so the
    next pass reads it again whatever it held before.

    ONLY A RECORD THIS PASS WROTE IS UNDONE. If another pass handed the room
    out since, its record stands: the other pass may have read it, and undoing
    its record could only make a room it read look unread (never the reverse).
    A failure here costs latency, never a room -- the rotation still covers
    it -- and says so."""
    back = [r for r in rooms if r in lent]
    if not back:
        return
    path = scan_path(seat, session, lane)
    try:
        with seat_state_lock(seat, session=session) as current, \
                _flocked(path + ".lock"):
            state = pk.read_json(path, None) if current else None
            looked = state.get(LOOKED) if isinstance(state, dict) else None
            if not isinstance(looked, dict):
                return
            for r in back:
                prior, stamp = lent[r]
                if looked.get(r) != stamp:
                    continue
                if r == hit or prior is None:
                    looked.pop(r, None)
                else:
                    looked[r] = prior
            pk.write_json(path, state)
    except OSError as exc:
        print("[helm] room rotation give-back failed (%s) — rooms this pass "
              "never read wait for the rotation instead of the next pass"
              % type(exc).__name__, file=sys.stderr)


#: Estate outcomes that mean SOMETHING WENT WRONG. A pass that hits one of
#: these has learned a fact about its own coverage that no later step can
#: unlearn, so they are terminal.
_ESTATE_FAILED = ("unlistable",)


def _estate(report, outcome, detail):
    """Record whether this pass saw the WHOLE room estate, when asked.

    A RECORDED FAILURE IS NEVER OVERWRITTEN. The scan records `unlistable`
    the moment the enumeration raises and then keeps going with the rooms it
    already has -- and every path out of it ends in one more `_estate` call,
    so an unguarded write replaces that failure with `complete`. A consumer
    reading a complete estate over readable empty pins then drains a backlog
    sitting in the room the listing could not name. Coverage only
    ever gets WORSE as a pass proceeds: nothing later in a scan can turn a
    failed enumeration into a seen one."""
    if not isinstance(report, dict):
        return
    if report.get("estate", (None, None))[0] in _ESTATE_FAILED:
        return
    report["estate"] = (outcome, detail)


def _scan_rooms(primary="main", seat=None, scope=None, session=None,
                scan_lane=None, bounded=True, report=None, lent=None):
    """Rooms considered by one delivery/gate pass. The seat's private DM lane,
    primary room, home room, and main are pinned first. Foreign rooms use a
    ROOM_SCAN_CAP-bounded overflow budget on hot paths; when that budget
    overflows, a persisted identity queue per consumer lane hands out dirty
    rooms first and then rotates (`_fair_room_slice`), so a room written since
    the lane last looked is in the next pass and every room is covered
    eventually, without observation stealing delivery slots. `lent` is passed
    through to that slice for a caller that may stop before reading every
    room. Join is the one-time `bounded=False` caller so every existing room
    receives an EOF admission baseline and pre-join backlog can never emerge
    in a later slice.

    WITHOUT A LANE OR A SEAT the overflow keeps the `size` rooms written most
    recently. A fresh write sorts first there, so that branch never had the
    lap-long wait the rotation had; it is not eventual, though, since a quiet
    room outside the newest `size` is never reached. Every production caller
    that overflows passes both, so only a lane-less probe reaches it.

    The scan is deliberately scope-BLIND (premise beacon-scope-mentions-
    plus-home-room-owner-posts-not-all superseded the G1-G3 homing allowlist):
    an @mention anywhere must eventually surface, while deliverable() applies
    per-row scope. DM lanes other than the seat's own stay invisible because
    list_rooms() omits them. Fail-open: an unlistable dir is just the pins."""
    rooms = []
    if seat:
        lane = dm_lane(seat)
        try:
            if os.path.exists(chat.room_path(lane)):
                rooms.append(lane)
        except OSError:
            pass
    rooms.append(primary)
    if seat:
        sc = scope if scope is not None else seat_scope(seat)
        pins = [sc.get("home"), "main"]
    else:
        pins = ["main"]
    seen = {pk.slug(r) for r in rooms}
    for p in pins:
        if p and p not in seen and os.path.exists(chat.room_path(p)):
            rooms.append(p)
            seen.add(p)
    try:
        names = chat.list_rooms()
    except OSError as exc:
        # A FAILED LISTING IS NOT AN EMPTY ESTATE. `names = []` silently
        # answered "this fleet has no other rooms", so every pending row
        # outside the primary vanished from the scan — the delivery promise's
        # seventh escape, found enumerating the sixth. The scan cannot invent
        # rooms it could not list, so it proceeds with what it has AND says
        # so: an unlistable estate is UNKNOWN coverage, not proven coverage.
        print("[helm] room listing failed (%s) — scanning only rooms already "
              "known to this pass; coverage is UNKNOWN, not complete"
              % type(exc).__name__, file=sys.stderr)
        _estate(report, "unlistable",
                "the room estate could not be listed (%s)" % type(exc).__name__)
        names = []
    others = [n for n in names if n not in seen]
    if not bounded:
        _estate(report, "complete", None)
        return rooms + sorted(others)
    # THE CAP BOUNDS THE PASS, NOT THE TAIL. `ROOM_SCAN_CAP - 1` assumed
    # `rooms` held exactly one pin, but it holds up to FOUR (the seat's DM
    # lane, primary, home, main) — so a pass advertised as 16 rooms scanned
    # 19. A probe measured it. A cap whose documented bound is not the bound
    # it enforces is the same defect as a promise with an unenumerated escape:
    # the number is checkable, and it was wrong. Subtract the pins that are
    # already committed. If the pins alone ever reach the cap the budget is
    # zero and they still go — a pin is pinned because dropping it loses the
    # seat's own mail, and the honest statement is that the floor is the pin
    # count, never that the total is always 16.
    size = max(0, ROOM_SCAN_CAP - len(rooms))
    # WHETHER THIS PASS SAW THE WHOLE ESTATE IS A DIFFERENT FACT FROM HOW EACH
    # ROOM READ, and only this function knows it. A caller holding per-room
    # outcomes for every room it was HANDED cannot tell a complete estate from
    # a rotation that covered a bounded slice of a larger one -- so an empty
    # result over rooms that were fully read looks like a drained backlog even
    # when the room the alarm was raised about was never in the slice.
    _estate(report,
            "capped" if len(others) > size else "complete",
            ("the rotation covered %d of %d foreign rooms this pass"
             % (size, len(others))) if len(others) > size else None)
    if scan_lane and seat and len(others) > size:
        others = _fair_room_slice(
            others, seat, session, size, scan_lane, lent=lent)
    else:
        def mtime(n):
            # ONLY ABSENCE IS EMPTINESS, as in `_room_dirty`: a room that is
            # gone has nothing to read, and a room that cannot be statted
            # might hold anything, so it sorts first, not last.
            try:
                return os.stat(chat.room_path(n)).st_mtime
            except FileNotFoundError:
                return 0.0
            except OSError:
                return float("inf")
        others.sort(key=mtime, reverse=True)
        others = others[:size]
    return rooms + others
