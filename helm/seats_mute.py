#!/usr/bin/env python3
"""The seat's WAKE FILTER and the room scope it is read against.

EXTRACTED FROM seats_roster BECAUSE OF ITS OWN BUDGET, and they are a coherent
unit on their own terms: a mute is not a fact about the roster ROW, it is a
per-seat filter on which rooms may wake an idle beacon, plus the scope reader
every consumer of that filter asks, plus the baseline that keeps a newly
admitted room's history from waking anybody. seats_roster reached the split's
1000-line finish line and the admission door needed a parameter; these six
functions are what moved so it could have one.

MUTE NEVER CONSUMES AN OBLIGATION. It prevents an eligible room row from
WAKING an idle seat and changes nothing about delivery, pending, stop or pull
— the distinction the whole surface turns on.
"""
import time

from . import chat, pk
from .seats_common import (_flocked, _seat_label, roster, roster_for_write,
                           roster_path)


def set_mute(seat, room, on=True):
    """(ok, message) — the seat's idle-beacon wake filter.

    Mute never consumes or clears delivery, pending, stop, or pull obligations;
    it only prevents eligible room rows from waking an idle seat. Stored on the
    roster row so every beacon session reads one truth.
    """
    room = pk.slug(room)
    with _flocked(roster_path() + ".lock"):
        r = roster_for_write()
        # MUTE MAY NOT ADMIT AN IDENTITY. `r.get(seat) or {}` followed by
        # `r[seat] = row` minted a roster row for ANY token, and because
        # projection is additive a mistyped `--seat` then armed that typo in
        # the machine-global authority PERMANENTLY — a metadata verb creating
        # fleet identity that nothing ever removes. A mute is a statement
        # ABOUT a seat and presupposes one; admission belongs to the join and
        # rename doors, which mean it.
        # DEFERRED, AT THE THINNER LEG. Token resolution belongs to the
        # roster's own module and that module reaches this one, so the import
        # is resolved at call time rather than closing a cycle at load.
        from .seats_roster import _resolve_seat
        key = _resolve_seat(r, seat)
        if key is None:
            return False, ("no seat %s on the roster — mute tunes an existing "
                           "seat's beacon and cannot create one "
                           "(helm chat seats lists them)" % _seat_label(seat))
        row = r.get(key) or {}
        mute = [m for m in row.get("mute") or [] if m != room]
        if on:
            mute.append(room)
        row["mute"] = sorted(mute)
        r[key] = row
        pk.write_json(roster_path(), r)
        # NO PROJECTION HERE. This verb cannot admit an identity — it
        # resolves an existing canonical row or fails — so there is
        # nothing new for the authority to learn, and taking the
        # machine-global flock to discover that convoyed every roster
        # writer behind a metadata-only write. Projection belongs to
        # the doors that actually admit: the join and the rename.
    lbl = _seat_label(seat)   # raw key drove the dict write above; the echoed
    if on:                    # label is laundered (a hostile HELM_CHAT_NAME
        return True, ("%s wake-muted for %s — idle beacon wakes stop; "
                      "tool-boundary delivery and pending obligations remain "
                      "(unmute: helm chat seat unmute %s)"
                      % (room, lbl, room))
    return True, "%s unmuted for %s" % (room, lbl)
def mutes(seat):
    """The seat's muted rooms, sorted — one roster read."""
    return sorted((roster().get(seat) or {}).get("mute") or [])
def _allowed_rooms(home_room):
    """Current live rooms admitted by one home choice (used only to compute a
    rehome delta; the delivery chokepoint remains _scan_rooms)."""
    try:
        rooms = set(chat.list_rooms()) | {"main"}
    except OSError:
        rooms = {"main"}
    return {home_room, "main"} if home_room else rooms
def room_in_scope(room, row):
    """Whether `room` belongs to a roster row's CURRENT delivery scope.
    Un-homed rows retain all-room legacy scope; a home admits only itself + main.
    Cursor activity is historical, so web presence must intersect it with this."""
    home_room = (row or {}).get("home_room")
    return not home_room or pk.slug(room) in {home_room, "main"}
def _rooms_to_baseline(old, new):
    """Rooms whose pre-transition history must be skipped. A destination home
    is always re-baselined, even when the old un-homed scope could theoretically
    see it; clearing to all rooms baselines only newly admitted foreign rooms."""
    if new and new != old:
        # A homed seat already admits #main. Narrowing that scope to explicit
        # main must preserve pending main traffic; every other destination is
        # newly admitted (including legacy un-homed -> homed, which rebases a
        # potentially stale all-room cursor by design).
        return set() if old and new == "main" else {new}
    return _allowed_rooms(new) - _allowed_rooms(old)
def _baseline_rooms(seat, row, rooms):
    """Baseline newly admitted rooms at their current EOF for the seat and all
    remembered sessions. Rehome changes scope, never replays pre-admission
    history or traffic accumulated while the seat was away."""
    # DEFERRED: the roster<->delivery cycle, closed at its thinner leg
    # (two names, two call sites — measured, not assumed).
    from .seats import _baseline_room_cursors
    sessions = [s for s in ([row.get("session")] + list(row.get("sessions") or []))
                if s]
    # DEFERRED, AT THE THINNER LEG. seats_delivery reaches the roster, and the
    # roster reaches this module for the baseline, so a module-level import
    # here closes a cycle. One call site, resolved at call time.
    from .seats_delivery import _baseline_room_cursors
    for room in rooms:
        _baseline_room_cursors(room, seat, sessions)
