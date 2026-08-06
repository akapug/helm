#!/usr/bin/env python3
"""helm seats — delivery: the cursor, the tail scan, and who a row reaches.

ONE CURSOR SERVES TWO READERS, and that is the design rather than an
accident. The per-(seat, room, session) cursor is what makes delivery
idempotent — a row is handed to a seat once — and the SAME cursor answers the
report's "what is unread". Two cursors would drift, and the drift would be
invisible: the report would say clear while delivery still had rows, or the
reverse, and nothing would fail.

The scan is bounded on purpose at every level (SCAN_CAP bytes per room,
ROOM_SCAN_CAP rooms per pass, a fair slice across rooms) because an unbounded
delivery pass is a denial of service against the seat it is trying to serve:
the cost of catching up must never exceed the cost of the work being
announced.

RESOLUTION IS SEPARATE FROM DELIVERY and reads through roster_checked, not
the plain fail-open reader. That is a defect fix worth not re-breaking:
malformed roster JSON used to reach the resolver through the fail-open path,
so a garbage row could casefold into an address that a fail-open send would
then write to. Untrusted data must not shape the address.
"""

import glob
import json
import os
import re
import sys
import time

from . import chat, home, pk
from .seats_common import (MAX_BYTES, ROOM_SCAN_CAP, SCAN_CAP, _canonical_recipient,
                           _clip, _flocked, _scrub, _seat_key, _seat_label,
                           _CanonicalRecipient, dm_lane, own_name,
                           recipient_matches, roster,
                           roster_path)
# THE LAYERING IS A DAG, not a happy accident: delivery -> roster ->
# identity -> common. Both siblings below defer their own upward calls to
# call time (identity's three roster queries, roster's two delivery ones),
# so nothing here closes a module-level loop.
from .seats_identity import (_delivery_guard, _reaction_wake_body,
                             _same_reaction_target, _warn_disagreement,
                             acting_seat, deliverable, derive_seat,
                             seat_scope)
from .seats_roster import (nonpane_session, roster_checked, runtime_for_session,
                           seat_for_session, touch_seen, write_roster)

def _sid8(session):
    """The session's cursor-key token — filename-safe, 8 chars, None-safe."""
    return pk.slug(str(session))[:8] if session else None
def cursor_path(room, seat, session=None):
    """PER (seat, session) when a session is known — co-named sessions each
    keep their own cursor (fan-out; the @mention-loss fix). Sessionless
    callers (bare CLI) ride the seat-level file."""
    p = os.path.join(chat.chat_dir(),
                     "%s.cursor.%s" % (pk.slug(room), _seat_key(seat)))
    s8 = _sid8(session)
    return "%s.%s" % (p, s8) if s8 else p
def _cursor(room, seat, session=None):
    d = pk.read_json(cursor_path(room, seat, session), None)
    if isinstance(d, dict) and isinstance(d.get("off"), int):
        return d
    return None
def rotation_hold_offset(room, dev, ino, state):
    """Earliest paused cursor offset this room rotation must retain, or None.

    Rotation is RAM etiquette, never authority to discard an addressed row. A
    paused session's unread suffix may temporarily keep a room above SIZE_CAP;
    measured HEALTHY releases the hold and the next append compacts normally.
    Missing cursors hold from zero because a post-join room backfills from zero.
    """
    earliest = None
    for seat, row in roster().items():
        if not isinstance(row, dict):
            continue
        sessions = [s for s in ([row.get("session")] +
                                list(row.get("sessions") or [])) if s]
        sessions = list(dict.fromkeys(sessions)) or [None]
        for session in sessions:
            runtime, verified = runtime_for_session(row, session)
            try:
                from . import proxywatch
                pause, _err = proxywatch.delivery_pause(
                    seat, state=state, runtime=runtime,
                    runtime_verified=verified)
            except Exception:
                pause = {"state": "UNKNOWN"}
            if not pause:
                continue
            cur = _cursor(room, seat, session) or _cursor(room, seat)
            if cur is None or (cur.get("dev"), cur.get("ino")) != (dev, ino):
                return 0
            off = cur.get("off")
            if not isinstance(off, int):
                return 0
            earliest = off if earliest is None else min(earliest, off)
    return earliest
def _write_cursor(room, seat, dev, ino, off, rid, session=None, active=False,
                  skip=None, base=0):
    chat._ensure_dir()
    row = {"dev": dev, "ino": ino, "off": off, "rid": rid,
           "active": bool(active), "base": int(base or 0)}
    if skip:
        row["skip"] = skip
    pk.write_json(cursor_path(room, seat, session), row)
def _baseline_state(room, at_start=False):
    """Room identity + offset + final complete row id for a safe baseline.
    Keeping the row id lets inode replacement suppress already-baselined rows."""
    try:
        with open(chat.room_path(room), "rb") as f:
            st = os.fstat(f.fileno())
            if at_start or not st.st_size:
                return st.st_dev, st.st_ino, 0 if at_start else st.st_size, None
            start = max(0, st.st_size - SCAN_CAP)
            f.seek(start)
            data = f.read()
        chunks = data.split(b"\n")
        if data and not data.endswith(b"\n"):
            chunks.pop()
        if start and chunks:
            chunks.pop(0)
        rid = None
        for chunk in reversed(chunks):
            if not chunk:
                continue
            try:
                row = json.loads(chunk.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("id"):
                rid = row["id"]
                break
        return st.st_dev, st.st_ino, st.st_size, rid
    except OSError:
        return None, None, 0, None
def _write_cursor_path(path, state, active=False, base=0):
    dev, ino, off, rid = state
    pk.write_json(path, {"dev": dev, "ino": ino, "off": off, "rid": rid,
                         "active": bool(active), "base": int(base or 0)})
def _cursor_paths(room, seat, sessions=()):
    """Seat-level plus every known/on-disk session cursor for one room."""
    base = cursor_path(room, seat)
    paths = {base}
    paths.update(cursor_path(room, seat, s) for s in sessions)
    paths.update(p for p in glob.glob(base + ".*") if not p.endswith(".lock"))
    return paths
def room_active(room, seat):
    """Whether any cursor for this seat actually consumed traffic in `room`.
    Normal hook delivery advances a session cursor, not the seat baseline, so
    presence must aggregate every on-disk cursor just like rehome safety does."""
    for path in _cursor_paths(room, seat):
        cur = pk.read_json(path, None)
        if isinstance(cur, dict) and cur.get("active"):
            return True
    return False
def _backfill_missing_room_cursors(room, seat, sessions=()):
    """Start missing cursors at zero when a room was already logically admitted
    but did not exist at the seat's join. Seat-level first, so session cursors
    inherit the same post-join ground instead of EOF-dropping pending traffic."""
    for session in [None] + list(sessions):
        if _cursor(room, seat, session) is not None:
            continue
        path = cursor_path(room, seat, session)
        with _flocked(path + ".lock"):
            if _cursor(room, seat, session) is None:
                _init_cursor(room, seat, session, at_start=True)
def _baseline_room_cursors(room, seat, sessions=()):
    """Baseline seat-level, remembered, and every on-disk session cursor.
    The roster keeps only eight session ids for addressing; cursor safety may
    not inherit that cap because an older still-live session can return later."""
    state = _baseline_state(room)
    for path in _cursor_paths(room, seat, sessions):
        with _flocked(path + ".lock"):
            _write_cursor_path(path, state, base=state[2])   # join offset
def _init_cursor(room, seat, session=None, at_start=False):
    """Baseline at the CURRENT end of room — at JOIN time (codex H5.5), so
    everything posted after session start delivers at the first boundary.
    A fresh SESSION cursor inherits the seat-level baseline when one exists
    (pre-split installs tracked the seat file; those rows must not be
    skipped by an EOF re-baseline — loss is the one forbidden outcome).
    at_start=True baselines at OFFSET 0 instead (multi-room: a room born
    after the seat joined is all post-join news — the mention that created
    the channel must deliver, not vanish under an EOF baseline), still
    binding the room file's identity so rotation detection holds.
    `active` distinguishes a bare EOF join from actual room consumption.
    -> True iff the baseline was inherited (already-tracked ground)."""
    if session:
        seatcur = _cursor(room, seat)
        if seatcur:
            _write_cursor(room, seat, seatcur.get("dev"), seatcur.get("ino"),
                          seatcur["off"], seatcur.get("rid"), session=session,
                          active=seatcur.get("active"), skip=seatcur.get("skip"),
                          base=seatcur.get("base"))      # inherit join baseline
            return True
    state = _baseline_state(room, at_start=at_start)
    _write_cursor(room, seat, *state, session=session, base=state[2])
    return False
def _tail(room, cur):
    """Read one bounded window of complete rows from ONE fstat'd fd.
    -> (dev, ino, base_off, entries, ground_rid, skip_rid), or None when
    there is nothing to read. entries = [(row_dict|None, end_off)] for every
    COMPLETE line; a trailing partial line is never consumed.

    Rotation/replacement restarts at zero and suppresses everything through
    the cursor's last row id. That id can sit beyond the first SCAN_CAP window
    in a legitimately rotated room, so `skip` persists the suppression search
    across boundaries: old rows are parked, never emitted. If a complete pass
    reaches EOF without the id, the replacement did not retain it; reset once
    to zero and accept duplicate replay on the next boundary (loss is the one
    forbidden outcome)."""
    path = chat.room_path(room)
    try:
        f = open(path, "rb")
    except FileNotFoundError:
        return None                      # ABSENT: genuinely nothing to read
    except OSError as exc:
        # THE NINTH ESCAPE (@codex-2): an unreadable room TAIL returned the
        # same None as "genuinely nothing new", so a room helm could not READ
        # was indistinguishable from a room with nothing in it — and the stop
        # read INBOX-CLEAN. That is escape 4's sentence one layer down: escape
        # 4 fixed the cheap STAT precheck and this is the READ behind it, so
        # the precheck could correctly say DIRTY and the reader still answer
        # empty. Permission, EIO, a tmpfs mid-remount: each is a room that
        # MIGHT hold addressed rows. Fail open — a guard that cannot read must
        # not wedge every stop — and never in silence.
        print("[helm] room %s could not be READ (%s) — its rows are UNKNOWN "
              "to this pass, not absent; the inbox is NOT proven clean"
              % (_scrub(str(room)), type(exc).__name__), file=sys.stderr)
        return None
    with f:
        st = os.fstat(f.fileno())
        off, suppress = cur.get("off", 0), cur.get("skip")
        if (st.st_dev, st.st_ino) != (cur.get("dev"), cur.get("ino")) \
                or st.st_size < off:
            off, suppress = 0, cur.get("rid")   # rotation / replacement
        elif st.st_size == off:
            if suppress:
                return st.st_dev, st.st_ino, 0, [], None, None
            return None                          # genuinely nothing new
        f.seek(off)
        data = f.read(SCAN_CAP)
    entries, pos = [], off
    for chunk in data.split(b"\n"):
        end = pos + len(chunk) + 1
        if end > off + len(data):                # trailing partial line
            break
        row = None
        try:
            v = json.loads(chunk.decode("utf-8", errors="replace"))
            if isinstance(v, dict):
                row = v
        except ValueError:
            pass
        if chunk:
            entries.append((row, end))
        pos = end
    base, ground = off, cur.get("rid")
    if suppress is not None:
        for i, (row, end) in enumerate(entries):
            if row is not None and row.get("id") == suppress:
                base, entries, ground = end, entries[i + 1:], suppress
                suppress = None
                break
        if suppress is not None:
            complete_eof = off + len(data) == st.st_size \
                and (not data or data.endswith(b"\n"))
            if complete_eof:
                if off == 0:
                    return st.st_dev, st.st_ino, 0, entries, None, None
                return st.st_dev, st.st_ino, 0, [], None, None
            base = entries[-1][1] if entries else off
            return st.st_dev, st.st_ino, base, [], ground, suppress
    return st.st_dev, st.st_ino, base, entries, ground, None
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
        with _flocked(path + ".lock"):
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
        # EVENTUAL" becomes false without one word of warning. codex-2 found
        # it attacking the enumeration rather than the prose, which is the bar
        # this lane set for itself. Fail open (a pass that cannot rotate must
        # still deliver) but never quietly: rotation state is what the claim
        # rests on, and when it is gone the claim has to be withdrawn out loud.
        print("[helm] room rotation state unavailable (%s) — this pass scans a "
              "FIXED prefix; rooms past it are NOT covered, and coverage is "
              "NOT eventual until rotation is readable again"
              % type(exc).__name__, file=sys.stderr)
        return sorted(live)[:size]
def _scan_rooms(primary="main", seat=None, scope=None, session=None,
                scan_lane=None, bounded=True):
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
        names = []
    others = [n for n in names if n not in seen]
    if not bounded:
        return rooms + sorted(others)
    # THE CAP BOUNDS THE PASS, NOT THE TAIL. `ROOM_SCAN_CAP - 1` assumed
    # `rooms` held exactly one pin, but it holds up to FOUR (the seat's DM
    # lane, primary, home, main) — so a pass advertised as 16 rooms scanned
    # 19. codex-2 measured it. A cap whose documented bound is not the bound
    # it enforces is the same defect as a promise with an unenumerated escape:
    # the number is checkable, and it was wrong. Subtract the pins that are
    # already committed. If the pins alone ever reach the cap the budget is
    # zero and they still go — a pin is pinned because dropping it loses the
    # seat's own mail, and the honest statement is that the floor is the pin
    # count, never that the total is always 16.
    size = max(0, ROOM_SCAN_CAP - len(rooms))
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
def _room_dirty(room, seat, session=None):
    """Lock-free precheck: could `room` hold rows past the (seat, session)
    cursor? A missing cursor is dirty (a room this seat has never looked
    at). Otherwise ONE stat against the atomically-written cursor: same
    file identity and size == off ⇒ clean. False positives are fine
    (deliver re-checks under the lock); a false negative cannot happen —
    an append grows the size, a rotation/replacement changes the inode.
    This keeps the every-tool-call hot path at ~one stat per quiet room."""
    cur = _cursor(room, seat, session) or _cursor(room, seat)
    if cur is None:
        return True
    try:
        st = os.stat(chat.room_path(room))
    except FileNotFoundError:
        return False                        # no room file: nothing to deliver
    except OSError:
        # ONLY ABSENCE IS EMPTINESS. The bare `except OSError: return False`
        # this replaces read every failure as "no room file" — a permission
        # error, an EIO, a tmpfs mid-remount, a path that stopped being a
        # directory. Each of those is a room that MIGHT hold rows, answered
        # as clean, which is the false negative this function's own contract
        # says cannot happen ("false positives are fine; a false negative
        # cannot happen"). The cheap precheck's entire safety argument rests
        # on that asymmetry, so an unreadable room must take the expensive
        # path and let the locked re-check decide. Found enumerating every
        # escape from the delivery promise (#74): a guard that cannot look is
        # not a guard that saw nothing.
        return True
    return (st.st_dev, st.st_ino) != (cur.get("dev"), cur.get("ino")) \
        or st.st_size != cur.get("off")
def deliver(session=None, room="main", seat=None, emit=None, cwd=None,
            backfill=False, scope=None, ambient=True):
    """Serialize the live credential-wall decision with one delivery event."""
    seat = seat or acting_seat(session, cwd)
    if _warn_disagreement(session, "delivery"):
        return None
    with _delivery_guard(seat, session) as pause:
        if pause:
            return None
        return _deliver_unpaused(
            session=session, room=room, seat=seat, emit=emit, cwd=cwd,
            backfill=backfill, scope=scope, ambient=ambient)
def _deliver_unpaused(session=None, room="main", seat=None, emit=None, cwd=None,
                      backfill=False, scope=None, ambient=True):
    """The tool-boundary nudge, ONE room: at most ONE delivery event, oldest
    first; later matches stay PENDING (their count shows, their cursor ground is
    not consumed — codex H6). A reaction event consumes a contiguous same-target
    burst, consistently for boundary hooks and beacons. Returns the label line
    or None.

    ambient=True is THIS lane's own scope (the boundary hook keeps the full
    home-room surface); ambient=False (the default beacon, via deliver_any)
    narrows deliverable() to mentions/replies/DMs/@all — a skipped home-room
    row is still CONSUMED (one cursor offset, hit=None advances), which is
    exactly what a muted room does today: the home room becomes a PULL
    surface (`helm chat read`), never a wake.

    At-least-once (codex H7): when `emit` is given it is called with the
    line BEFORE the cursor commits; emit must do its one unbuffered write.
    A kill between emit and commit re-delivers next boundary.

    Fan-out: the cursor is per (seat, session) — every co-named session sees
    the same @mention on its own boundary; consuming here never starves a
    sibling session (at-most-once BETWEEN co-named sessions was the bug).

    backfill=True (deliver_any's tracked-seat path) makes a MISSING cursor
    baseline at offset 0 and scan THIS boundary — the multi-room law for a
    room born after the seat joined; default keeps the EOF self-heal.

    The public wrapper holds proxywatch's state-transition lock across this
    whole operation. A dark record therefore exists either before the decision
    (nothing mutates) or after the cursor commit (this delivery preceded the
    measured wall); it can never race between check and consumption."""
    if not touch_seen(seat):               # presence FIRST — a seat muted by the
        return None                        # kill-switch below is still ALIVE.
                                           # A REFUSED beat also delivers NOTHING:
                                           # consuming another seat's inbox is the
                                           # silent-MISdelivery half of the same
                                           # bug (the owner's @mention vanished
                                           # into a stranger's context).
    if (home.env("CHAT_DELIVER") or "").lower() in ("0", "off", "no"):
        return None                        # its presence beat keeps gc off it
    known = seat_for_session(session)
    sc = scope if scope is not None else seat_scope(seat)
    # Global order is roster -> cursor: rehome/join baseline cursors while the
    # roster lock is held. Register an unknown session BEFORE taking its cursor
    # lock, otherwise delivery and rehome can each wait forever on the other.
    if session and known is None and _cursor(room, seat, session) is None:
        # addressing-only for a non-pane session: a boundary hook firing for a
        # subagent must not re-point the seat's row at that transient id
        write_roster(seat, session=session,
                     identity=not nonpane_session(session))
    with _flocked(cursor_path(room, seat, session) + ".lock"):
        cur = _cursor(room, seat, session)
        if cur is None:
            inherited = _init_cursor(room, seat, session, at_start=backfill)
            if not inherited and not backfill:
                return None       # fresh EOF baseline: backlog never floods
            cur = _cursor(room, seat, session)  # already-tracked ground —
            if cur is None:                     # deliver from it THIS boundary
                return None
        got = _tail(room, cur)
        if got is None:
            return None
        dev, ino, base, entries, last_rid, skip = got
        # A rotation/replacement (_tail restarts at 0 on a new dev,ino) voids
        # the join baseline: it indexes the now-gone old file, so carrying it
        # forward would false-SENT the new file's genuinely-delivered rows. The
        # pre-join backlog died with the old inode — re-baseline the gate at 0.
        cbase = cur.get("base") \
            if (dev, ino) == (cur.get("dev"), cur.get("ino")) else 0
        hit = None
        last_end = base
        for i, (row, end) in enumerate(entries):
            if row is not None and deliverable(row, seat, room, sc, ambient):
                hit = (i, row, end)
                break
            last_end, last_rid = end, (row or {}).get("id") or last_rid
        if hit is None:
            _write_cursor(
                room, seat, dev, ino, last_end, last_rid, session=session,
                active=cur.get("active") or bool(entries), skip=skip,
                base=cbase)          # advance never moves the base (0 on rot.)
            return None
        i, row, end = hit
        grouped, cursor_end, cursor_rid, last_i = [row], end, row.get("id"), i
        if row.get("react"):
            # One poll sees one append burst. Collapse consecutive addressed
            # reactions for the SAME signed target into one emitted line, while
            # consuming intervening non-deliverable rows exactly as the next
            # scan would. Stop at the first other obligation: crossing it would
            # reorder delivery. The aggregate still emits BEFORE this cursor
            # commits, preserving deliver()'s at-least-once contract.
            for j, (other, other_end) in enumerate(entries[i + 1:], i + 1):
                addressed = other is not None \
                    and deliverable(other, seat, room, sc, ambient)
                if addressed and (not other.get("react")
                                  or not _same_reaction_target(row, other)):
                    break
                if addressed:
                    grouped.append(other)
                last_i, cursor_end = j, other_end
                cursor_rid = (other or {}).get("id") or cursor_rid
        waiting = sum(1 for r, _e in entries[last_i + 1:]
                      if r is not None and deliverable(r, seat, room, sc,
                                                       ambient))
        is_dm = room.startswith(chat.DM_PREFIX)           # a DM is a DM on
        where = (" dm" if is_dm                           # every surface —
                 else "" if room == "main" else " #%s" % room)  # never a room
        # The event line carries the row's OWN timestamp first: a woken
        # agent reads the AGE before the content, so a stale row that somehow
        # reached the beacon (an unmarked old row, a cursor far behind) shows
        # its date before its text — the "author does not apply their own
        # rule under load" class needs the machine to surface age, not a
        # discipline each seat must remember (opus-integrator, #114 brief).
        row_ts = str(row.get("ts") or "?")
        try:
            if row.get("react"):
                line = "[helm chat reaction%s → %s @%s] %s" % (
                    where, seat, row_ts, _reaction_wake_body(grouped, room))
            else:
                line = "[helm chat%s → %s @%s] %s: %s" % (  # reply lands here
                    where, seat, row_ts,
                    chat._dsan(row.get("from") or "?"),     # id laundered
                    _clip(_scrub(row.get("text") or "")))    # unicode text
        except Exception as e:
            # A row that PASSES deliverable() but can't be RENDERED — a malformed
            # non-string from/text field on a foreign/corrupt jsonl row (_scrub
            # does `for ch in s`; a truthy non-str raises) — throws HERE, before
            # the cursor commits. Unhandled it re-raises every poll and strands
            # the whole backlog behind it forever. Skip it: fail LOUD to stderr
            # (never a silent drop) and advance the cursor PAST the offender so
            # the rest drains. H7 holds — this wraps CONSTRUCTION only, never the
            # emit below (an emit that dies must still NOT commit).
            try:
                os.write(2, ("[helm chat] skipped an unrenderable row %r in %s "
                             "(%s) — advancing past it\n"
                             % (row.get("id"), room, e)).encode("utf-8", "replace"))
            except OSError:
                pass          # a dead/closed stderr (daemonized) must NOT re-raise
                              # here — that would strand the backlog it's skipping
            _write_cursor(room, seat, dev, ino, cursor_end, cursor_rid,
                          session=session, active=True, base=cbase)
            return None
        if waiting:
            line += " (+%d waiting — helm chat read%s)" % (
                waiting, " --dm" if is_dm
                else "" if room == "main" else " --room %s" % room)
        if emit is not None:
            emit(line)                  # output FIRST …
        _write_cursor(room, seat, dev, ino, cursor_end, cursor_rid,
                      session=session, active=True,   # … commit after
                      base=cbase)             # carried forward (0 on rotation)
        return line
def deliver_any(session=None, seat=None, emit=None, cwd=None, room="main",
                ambient=True):
    """The MULTI-ROOM boundary nudge (slice 5 — what the PostToolUse hook and
    the beacon actually call): one delivery event per boundary from the first
    room that has one — the primary room first, then the rest of
    _scan_rooms' bounded, newest-activity-first list. An @mention in a
    channel the seat never joined must wake it (the owner's helm-dogfood
    '@opus-integrator' post, live 2026-07-21), so a TRACKED seat — it holds
    a primary-room cursor — meeting a cursor-less room BACKFILLS from offset
    0: a room born after its join is all post-join news. An UNtracked seat
    (never joined / reaped / pre-install) keeps the EOF self-heal everywhere:
    pre-join backlog never floods. Scanning a clean room advances only that
    room's cursor; a hit STOPS the scan, so later rooms keep their pending
    for the next boundary (one nudge per boundary — the budget stays flat).
    Exceptions propagate exactly like deliver's (H7: an emit that died must
    not commit); every caller already wraps fail-open."""
    seat = seat or acting_seat(session, cwd)   # OWN identity first (acting_seat)
    if _warn_disagreement(session, "delivery"):
        return None           # disputed identity: no beat, no consume (see
                              # deliver — same law, and the beat must stop
                              # HERE too or the dispute reads as presence)
    with _delivery_guard(seat, session) as pause:
        if pause:
            # DELIVERY is the actuator. A waiter that froze old config at arm
            # time reads this latch every poll; presence and cursors stay still.
            return None
        if not touch_seen(seat):  # presence even when every room is quiet — and a
            return None           # cross-seat probe drains no one else's inbox
    sc = seat_scope(seat)     # ONE roster read for the whole pass
    tracked = sc["tracked"] \
        or (_cursor(room, seat, session) or _cursor(room, seat)) is not None
    for r in _scan_rooms(
            room, seat=seat, scope=sc, session=session,
            scan_lane="deliver"):
        if not _room_dirty(r, seat, session):
            continue
        # the DM lane ALWAYS backfills from 0 — every row in it is addressed
        # to this seat, so even an untracked (reaped/pre-install) seat must
        # get the DM that created its lane, never an EOF skip
        line = deliver(session=session, room=r, seat=seat, emit=emit, cwd=cwd,
                       backfill=(tracked and r != room)
                       or r.startswith(chat.DM_PREFIX), scope=sc,
                       ambient=ambient)
                                          # the hook passes nothing (full scope);
                                          # only the beacon narrows this
        if line:
            return line
    return None
def _resolve_against(to, r):
    """Resolve `to` without reacquiring the roster `r` the caller already read.

    ROSTER-STABLE, not host-pure: alias evidence may still inspect spawn,
    worktree and process owners, but it receives this exact roster snapshot.
    FACTORED OUT SO IDENTITY AND EVIDENCE CAN COME FROM ONE READ. When this
    logic read the roster itself, recipient_capability performed TWO reads —
    the plain fail-open reader in here, then roster_checked() outside — and two
    reads can observe two states, so canonical identity and membership
    evidence were not one fact. That is precisely what the meld convened to
    establish, so the read has to be the caller's to own."""
    canonical, err = _canonical_recipient(to)
    if err:
        shown = str(to or "").strip().lstrip("@")
        return None, ("recipient %r must be 1-64 chars of [A-Za-z0-9._-] — "
                      "the exact seat token" % shown)
    hits = [k for k in r if recipient_matches(k, canonical)]
    if len(hits) == 1:
        return _CanonicalRecipient(str(canonical), hits[0]), None
    if len(hits) > 1:
        return None, "recipient %r is ambiguous by case" % canonical.display
    from . import seat_identity
    refusal = seat_identity.alias_refusal(canonical.display, roster=r)
    return (None, refusal) if refusal else (canonical, None)
def resolve_recipient(to):
    """Validate one exact address; known alternate names SUGGEST and REFUSE.

    This remains the canonical addressee owner shared by DM and dispatch. An
    unrelated valid token is still legal before its seat joins; only positive
    alias evidence blocks it, and the evidence module can never return a
    replacement operand for this function to route silently.

    UNCHANGED CONTRACT: it reads the roster itself, fail-open, exactly as
    before. Callers that need identity and evidence to be ONE FACT use
    recipient_capability instead of pairing this with a second read.
    """
    return _resolve_against(to, roster())
def recipient_capability(raw):
    """One side-effect-free read of EVERYTHING known about an address.

    LIVES HERE, BESIDE resolve_recipient, because seats owns BOTH halves this
    answer needs — canonical identity AND roster evidence. Deriving it in a
    consumer is how three review rounds each lost a different fact:
      the tri-state collapsed to a two-way branch, so an unreadable roster
      printed a DELIVERY CLAIM;
      the same collapse reappeared one surface over, where UNKNOWN membership
      was rendered as "this mention WILL be SKIPPED" — the OPPOSITE certainty,
      since an unknown seat may already be joined and receive it;
      and the resolver's own error was swallowed, so a malformed address was
      reported as "no roster row" — a true-sounding answer to a question
      nobody asked.
    Each round closed real cases and left the class. A caller cannot collapse
    a distinction it was handed explicitly, so this returns all of it.

    -> {"raw", "canonical", "error", "membership", "evidence"}
       membership: "JOINED" | "ABSENT" | "UNKNOWN"
       evidence:   "populated" | "empty" | "read-failed" | "malformed"

    ABSENT AND UNKNOWN ARE NOT INTERCHANGEABLE, and neither are the two kinds
    of UNKNOWN. "the roster could not be read" and "no seat has joined this
    box yet" are the same verdict for a REFUSAL and different sentences to a
    human. Callers decide policy; this decides nothing.

    THE ASYMMETRY THIS EXISTS TO SERVE: refusing a legitimate address costs a
    real dispatch, while claiming a delivery that cannot happen costs hours of
    a row nobody owns. So membership is JOINED only on positive evidence, and
    UNKNOWN never manufactures a refusal on its own."""
    # ONE READ, AND THE RESOLVER SEES EXACTLY IT. Reading roster_checked
    # FIRST and resolving against that same validated dict closes two defects
    # at once: identity and evidence can no longer describe different states,
    # and MALFORMED DATA CAN NO LONGER SHAPE THE ADDRESS. Wrong-shape JSON
    # used to reach the resolver through the plain fail-open reader, so
    # 'GHOST' casefolded to 'ghost' off garbage that this function then turned
    # around and declared unreadable — untrusted data deciding the address
    # that a fail-open send would write.
    # NAMED `rows`, NOT `roster`, and the rename is load-bearing. The split
    # made this file IMPORT the roster accessor instead of defining it, and a
    # local of the same name then SHADOWS the accessor — invisible while the
    # def lived here, a genuine ambiguity now. The launder tripwire refuses
    # exactly this, and it is right to: a reader cannot tell which `roster`
    # a line means, and neither can the call-site count-pin.
    rows, failed = roster_checked()
    canonical, error = _resolve_against(raw, {} if failed else rows)
    if error:
        return {"raw": raw, "canonical": None, "error": error,
                "membership": "UNKNOWN", "evidence": "malformed",
                "_candidates": ()}
    # roster_checked, NOT roster: the plain reader is FAIL-OPEN by
    # construction, so WELL-FORMED-BUT-WRONG-SHAPED state comes back as a
    # NON-EMPTY dict of garbage and a membership test would then refuse every
    # real seat for not appearing in it. Unparseable BYTES fail-open to {} and
    # are caught by the empty arm; wrong-shaped JSON is where the two readers
    # actually diverge, which is why the grid tests both.
    if failed:
        return {"raw": raw, "canonical": canonical, "error": None,
                "membership": "UNKNOWN", "evidence": "read-failed",
                "_candidates": ()}
    if not rows:
        return {"raw": raw, "canonical": canonical, "error": None,
                "membership": "UNKNOWN", "evidence": "empty",
                "_candidates": ()}
    return {"raw": raw, "canonical": canonical, "error": None,
            "membership": "JOINED" if any(
                recipient_matches(canonical, key) for key in rows) else "ABSENT",
            "evidence": "populated",
            "_candidates": tuple(_seat_label(n) for n in sorted(rows))}
def _dm_canonical(to, text, who=None, session=None, profile=None, sign=None,
                  origin=None, reply_to=None):
    """Post to one already-resolved exact seat token, with no roster read."""
    to, err = _canonical_recipient(to)
    if err:
        return None, "canonical DM recipient must be an exact seat token"
    sender = who or seat_for_session(session) or derive_seat(session)
    if recipient_matches(sender, to):
        return None, "a DM to yourself would never deliver (own posts don't)"
    return chat.post(text, who=sender, profile=profile, sign=sign,
                     origin=origin, dm=to, dm_display=to.display,
                     reply_to=reply_to), None
def dm(to, text, who=None, session=None, profile=None, sign=None, origin=None,
       reply_to=None):
    """One TRUE 1:1 message -> (row, None) or (None, reason). The recipient
    is one unconditional @-stripped, casefolded EXACT token; a live roster may
    choose its display spelling but never shapes routing identity. There is
    never a substring or slug fold (team.a and team-a are different addressees
    with different lanes). The row lands in the recipient's
    private lane only (chat.post dm= — no room fanout by construction),
    signs like any post, and the recipient's beacon/boundary surfaces it
    first in the scan. A DM to a not-yet-joined seat waits in its lane; the
    join baselines that lane at 0, so it delivers. `reply_to` (a parent row id
    or ordinal IN THAT LANE) threads the DM — chat.post owns the resolve."""
    if not isinstance(to, _CanonicalRecipient):
        to, err = resolve_recipient(to)
        if err:
            return None, err
    return _dm_canonical(to, text, who=who, session=session, profile=profile,
                         sign=sign, origin=origin, reply_to=reply_to)
