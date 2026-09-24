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

`seats_delivery` re-exports all three, so no existing import path changed.
"""
import os
import sys

from . import chat, pk
from .seats_common import ROOM_SCAN_CAP, _flocked, _seat_key, dm_lane
from .seats_cursor import _sid8, seat_state_lock
from .seats_identity import seat_scope


def scan_path(seat, session=None, lane="deliver"):
    """Per-seat/session/lane overflow-ring state for eventual room coverage."""
    p = os.path.join(chat.chat_dir(), ".scan." + _seat_key(seat))
    s8 = _sid8(session)
    return ".".join(x for x in (p, s8, lane) if x)
def _fair_room_slice(names, seat, session, size, lane):
    """A bounded round-robin over stable room identities. Survivors retain
    their queue order while newly discovered rooms append behind them, so
    insertions cannot move an old room's goalpost forever. Each consumer lane
    owns its queue: roster observation must never advance delivery or stop."""
    live = set(names)
    if not live or size <= 0:
        return []
    path = scan_path(seat, session, lane)
    try:
        with seat_state_lock(seat, session=session) as current, _flocked(path + ".lock"):
            if not current:
                return sorted(live)[:size]
            state = pk.read_json(path, {}) or {}
            saved = state.get("rooms")
            saved = saved if isinstance(saved, list) else []
            ring = []
            seen = set()
            for name in saved:
                if isinstance(name, str) and name in live and name not in seen:
                    ring.append(name)
                    seen.add(name)
            ring.extend(sorted(live - seen))
            count = min(size, len(ring))
            out = ring[:count]
            pk.write_json(path, {"rooms": ring[count:] + out})
            return out
    except OSError as exc:
        # ROTATION IS THE ONLY THING THAT MAKES THE CAP HONEST, and losing it
        # silently is worse than losing it. This fallback returns the same
        # ALPHABETICAL PREFIX on every pass, forever — so a room sorting after
        # position `size` is never scanned again, and escape 6's "coverage is
        # EVENTUAL" becomes false without one word of warning. A review found
        # it attacking the enumeration rather than the prose, which is the bar
        # this lane set for itself. Fail open (a pass that cannot rotate must
        # still deliver) but never quietly: rotation state is what the claim
        # rests on, and when it is gone the claim has to be withdrawn out loud.
        print("[helm] room rotation state unavailable (%s) — this pass scans a "
              "FIXED prefix; rooms past it are NOT covered, and coverage is "
              "NOT eventual until rotation is readable again"
              % type(exc).__name__, file=sys.stderr)
        return sorted(live)[:size]
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
                scan_lane=None, bounded=True, report=None):
    """Rooms considered by one delivery/gate pass. The seat's private DM lane,
    primary room, home room, and main are pinned first. Foreign rooms use a
    ROOM_SCAN_CAP-bounded overflow budget on hot paths; when that budget
    overflows, a persisted identity queue per consumer lane gives every room
    eventual coverage without observation stealing delivery slots. Join is
    the one-time `bounded=False` caller so every existing room receives an EOF
    admission baseline and pre-join backlog can never emerge in a later slice.

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
            others, seat, session, size, scan_lane)
    else:
        def mtime(n):
            try:
                return os.stat(chat.room_path(n)).st_mtime
            except OSError:
                return 0.0
        others.sort(key=mtime, reverse=True)
        others = others[:size]
    return rooms + others
