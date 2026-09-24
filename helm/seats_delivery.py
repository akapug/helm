#!/usr/bin/env python3
"""Bounded row delivery and recipient resolution.

Delivery owns obligations; wake has a paired cursor because mute is wake-only.
Sparse physical tokens coordinate both orders: ``held`` is beacon-crossed but
hook-owed, while ``done`` is hook-crossed before beacon. Occurrence identity
keeps id-less/duplicate-ID rows distinct and remaps across rotation. Reports
read delivery state only.

Every scan is bounded to keep catchup cost below announced-work cost. Recipient
resolution uses fail-closed ``roster_checked`` so malformed roster data cannot
shape an address for a fail-open send.
"""

import json
import os
import re
import sys
import time

from . import chat, home, hookrun, machine_senders, pk
from .seats_common import (MAX_BYTES, ROOM_SCAN_CAP, SCAN_CAP, _canonical_recipient,
                           _clip, _flocked, _scrub, _seat_key, _seat_label,
                           _CanonicalRecipient, alias_keys, dm_lane, own_name,
                           recipient_matches, roster, roster_path)
# THE LAYERING IS A DAG, not a happy accident: delivery -> roster ->
# identity -> common. Both siblings below defer their own upward calls to
# call time (identity's three roster queries, roster's two delivery ones),
# so nothing here closes a module-level loop.
from .seats_identity import (_delivery_guard, _reaction_wake_body,
                             _same_reaction_target, _warn_disagreement,
                             _warn_once, acting_seat, deliverable,
                             seat_scope)
from .seats_roster import (nonpane_session, roster_checked, seat_for_session,
                           touch_seen, write_roster)
from .seats_runtime import runtime_for_session
from .seats_receipts import receipt_cli, record_delivery_receipt
from .seats_cursor import (_all_cursor_locks, beacon_cursor_path,
                           _commit_cursor_updates, _cursor_checked,
                           _cursor_estate_locks, _cursor_locks, _cursor_pair,
                           _cursor_room_locks,
                           _cursor_topology_lock, _cursor_transaction_epoch,
                           _cursor_transaction_pending, _cursor_path,
                           _cursor_path_from_keys, _cursor_path_index, _cursor_paths, _cursor_state,
                           _occurrence, _occurrence_parts, _sid8, _strict_json,
                           _write_cursor_path, cursor_path,
                           normalize_rotated_cursor, parse_cursor_path,
                           recover_room_rotation, rotation_journal,
                           room_active, seat_state_lock)


def _own_delivery_seat(session, cwd, where):
    """The seat this process may move CURSORS and PRESENCE for, or None.

    DELIVERY IS ALSO THE BIND PATH, and that is why this door is the SPEECH
    one and not the act one. The self-heal is how an un-rostered session
    acquires a roster row at all (`auto_name` -> write_roster), so refusing
    DERIVED here is a LOCKOUT LOOP: a session that never explicitly joined
    could never become rostered, and therefore could never stop being DERIVED.
    Measured — `test_unknown_session_self_heals_roster_with_meaningful_name`
    is the pin, and it is the designed first-contact path.

    AND A DERIVED DELIVERY CANNOT DRAIN A STRANGER: `auto_name` dedupes against
    the roster (a name a DIFFERENT session already holds gets -2/-3…), so the
    minted name provably is not a live rostered seat's. The theft vector was
    never the mint — it was the DISPUTE, an inherited HELM_CHAT_NAME pointing
    at somebody else's row, and that refuses above this, loudly.

    RETURNS None ON REFUSAL rather than raising or printing, because every
    caller of deliver/self-heal is a tool-boundary hook that already treats
    None as "no beat this pass". One loud line per refusal per process —
    silence is what let this class run for days."""
    from . import actors
    actor, err = actors.resolve_speaker(session, cwd,
                                        act="move %s cursors" % where)
    if err:
        _warn_once("no-admitted-actor:%s:%s" % (where, session), err + "\n")
        return None
    return actor.canonical_name if actor else acting_seat(session, cwd)


def _cursor(room, seat, session=None, beacon=False, report=None):
    """This consumer's cursor for one room, or None.

    `report`, when a dict, is FILLED with WHY a None was returned: the four
    reasons are not one fact and the caller acts differently on each. ABSENT is
    a room this seat has never looked at, and replaying it from zero is right.
    UNREADABLE is a cursor that exists and cannot be trusted -- a transaction in
    flight, an epoch that moved under the read, a malformed file -- and
    replaying THAT from zero brings an already-consumed row back as pending,
    which becomes positive overdue evidence and types into a pane about an
    obligation that was met. Callers passing no dict are unchanged."""
    path = (beacon_cursor_path(room, seat, session) if beacon else
            cursor_path(room, seat, session))
    epoch, pending = _cursor_transaction_epoch(path)
    if pending:
        _say(report, "unreadable", "a cursor transaction was in flight")
        return None
    d = pk.read_json(path, None)
    after, pending = _cursor_transaction_epoch(path)
    if pending or after != epoch:
        _say(report, "unreadable", "the cursor changed under this read")
        return None
    if isinstance(d, dict) and isinstance(d.get("off"), int):
        _say(report, "read", None)
        return normalize_rotated_cursor(room, d, path=path)
    if d is None and not os.path.exists(path):
        _say(report, "absent", "this seat has no cursor for the room")
    else:
        _say(report, "unreadable",
             "the cursor file is present and does not parse as a cursor")
    return None
def remap_rotated_cursors(room, old_dev, old_ino, cut, new_dev, new_ino,
                          retained_starts, prepare=None, install=None):
    """Remap every cursor and install the compacted room in one transaction."""
    room_key = pk.slug(room)
    with _cursor_topology_lock(), _cursor_room_locks({room_key}):
        try:
            names = os.listdir(chat.chat_dir())
        except OSError:
            return False
        parsed = [item for name in names
                  for item in [parse_cursor_path(
                      os.path.join(chat.chat_dir(), name))]
                  if item and item["room"] == room_key]
        paths = [item["path"] for item in parsed]
        mapping = {old: _occurrence(new_dev, new_ino, old - cut)
                   for old in retained_starts}
        with _cursor_estate_locks({item["seat_key"] for item in parsed}), \
                _cursor_locks(paths, estate=False):
            updates, before = {}, {}
            journal = rotation_journal(room)
            for path in paths:
                cur = pk.read_json(path, None)
                cur = normalize_rotated_cursor(room, cur, journal, path)
                if not isinstance(cur, dict) \
                        or (cur.get("dev"), cur.get("ino")) != (old_dev, old_ino):
                    continue
                before[os.path.basename(path)] = cur
                row = dict(cur)
                row["dev"], row["ino"] = new_dev, new_ino
                row["off"] = max(0, int(row.get("off") or 0) - cut)
                row["base"] = max(0, int(row.get("base") or 0) - cut)
                for field in ("held", "done"):
                    tokens = [mapping[parts[2]] for token in row.get(field) or ()
                              for parts in [_occurrence_parts(token)]
                              if parts and parts[:2] == (old_dev, old_ino)
                              and parts[2] in mapping]
                    if tokens:
                        row[field] = sorted(set(tokens))
                    else:
                        row.pop(field, None)
                updates[path] = row
            if prepare is not None:
                try:
                    prepare(before, updates)
                except (OSError, ValueError, TypeError):
                    return False
            return _commit_cursor_updates(updates, finish=install)


def rotation_hold_offset(room, dev, ino, state):
    """Earliest paused or wake-held offset rotation must retain, or None."""
    earliest, room_key = None, pk.slug(room)
    with _cursor_topology_lock(), _cursor_room_locks({room_key}):
        paused = []
        for seat, row in roster().items():
            if not isinstance(row, dict):
                continue
            sessions = [s for s in ([row.get("session")] +
                                    list(row.get("sessions") or [])) if s]
            for session in list(dict.fromkeys(sessions)) or [None]:
                runtime, verified = runtime_for_session(row, session)
                try:
                    from . import proxywatch
                    pause, _err = proxywatch.delivery_pause(
                        seat, state=state, runtime=runtime,
                        runtime_verified=verified)
                except Exception:
                    pause = {"state": "UNKNOWN"}
                if pause:
                    paused.append((seat, session))
        try:
            names = os.listdir(chat.chat_dir())
        except OSError:
            return 0
        held_paths = [item["path"] for name in names
                      for item in [parse_cursor_path(
                          os.path.join(chat.chat_dir(), name))]
                      if item and item["beacon"] and item["room"] == room_key]
        # `_init_cursor`'s resume order for a paused session: its delivery
        # cursor, its wake cursor, then its seat's. A session with none of
        # them resumes at EOF and holds nothing (task/2930: holding it at 0
        # rewrote the whole room on every post over the cap).
        chains = [[cursor_path(room, seat, session),
                   beacon_cursor_path(room, seat, session)]
                  + ([cursor_path(room, seat)] if session else [])
                  for seat, session in paused]
        paths = held_paths + [path for chain in chains for path in chain]
        parsed = [parse_cursor_path(path) for path in paths]
        seats = {item["seat_key"] for item in parsed if item}
        with _cursor_estate_locks(seats), _cursor_locks(paths, estate=False):
            for path in held_paths:
                try:
                    cur = normalize_rotated_cursor(
                        room, _strict_json(path), path=path)
                except OSError:
                    return 0
                if not isinstance(cur, dict) or not isinstance(cur.get("off"), int):
                    return 0
                for token in cur.get("held") or ():
                    parts = _occurrence_parts(token)
                    if parts and parts[:2] == (dev, ino):
                        earliest = min(parts[2], earliest) \
                            if earliest is not None else parts[2]
            for chain in chains:
                path = next((p for p in chain if os.path.exists(p)), None)
                if path is None:
                    continue
                cur = normalize_rotated_cursor(room, pk.read_json(path, None),
                                               path=path)
                valid = isinstance(cur, dict) and isinstance(cur.get("off"), int)
                if not valid or (cur.get("dev"), cur.get("ino")) != (dev, ino):
                    return 0
                earliest = min(cur["off"], earliest) \
                    if earliest is not None else cur["off"]
    return earliest
def _write_cursor(room, seat, dev, ino, off, rid, session=None, active=False,
                  skip=None, base=0, beacon=False, held=(), done=(), occ=None):
    chat._ensure_dir()
    row = {"dev": dev, "ino": ino, "off": off, "rid": rid,
           "active": bool(active), "base": int(base or 0)}
    if skip:
        row["skip"] = skip
    if occ:
        row["occ"] = str(occ)
    if beacon and held:
        row["held"] = [str(x) for x in held if x]
    if beacon and done:
        row["done"] = [str(x) for x in done if x]
    pk.write_json(_cursor_path(room, seat, session, beacon=beacon), row)
def _baseline_state(room, at_start=False):
    """Room identity + final complete offset/id for a safe baseline.
    Keeping the row id lets inode replacement suppress already-baselined rows;
    stopping at the last newline means an in-flight append is never consumed."""
    try:
        with open(chat.room_path(room), "rb") as f:
            st = os.fstat(f.fileno())
            if at_start or not st.st_size:
                return st.st_dev, st.st_ino, 0, None
            off = st.st_size
            f.seek(off - 1)
            if f.read(1) != b"\n":
                pos = off
                while pos:
                    start = max(0, pos - SCAN_CAP)
                    f.seek(start)
                    data = f.read(pos - start)
                    cut = data.rfind(b"\n")
                    if cut >= 0:
                        off = start + cut + 1
                        break
                    pos = start
                else:
                    off = 0
            if not off:
                return st.st_dev, st.st_ino, 0, None
            start = max(0, off - SCAN_CAP)
            f.seek(start)
            data = f.read(off - start)
        chunks = data.split(b"\n")
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
        return st.st_dev, st.st_ino, off, rid
    except OSError:
        return None, None, 0, None
def _backfill_missing_room_cursors(room, seat, sessions=()):
    """Start every missing paired cursor at zero for a post-join room."""
    for session in [None] + list(sessions):
        if _cursor(room, seat, session) is None \
                or _cursor(room, seat, session, beacon=True) is None:
            _init_cursor(room, seat, session, at_start=True)
def _baseline_room_cursors(room, seat, sessions=()):
    """Baseline every paired seat/on-disk session cursor in one transaction."""
    with _all_cursor_locks(room, seat, sessions) as paths:
        state = _baseline_state(room)
        row = _cursor_state(state, base=state[2])
        if not _commit_cursor_updates({path: row for path in paths}):
            raise OSError("cursor baseline transaction failed")
def _init_cursor(room, seat, session=None, at_start=False,
                 inherit_existing=False):
    """Initialize one exact delivery+wake pair under the global cursor order."""
    exact = _cursor_pair(room, seat, session)
    base_pair = _cursor_pair(room, seat)
    paths = set(exact) | (set(base_pair) if session else set())
    if inherit_existing:
        paths.update(_cursor_paths(room, seat))
    with _cursor_locks(paths) as locked:
        bound = seat_for_session(session) if session else None
        if bound is not None and not recipient_matches(bound, seat):
            return False                 # rename won while this initializer waited
        existing = _cursor(room, seat, session)
        wake = _cursor(room, seat, session, beacon=True)
        if existing is not None:
            if wake is None:
                row = dict(existing)
                row.pop("held", None)
                row.pop("done", None)
                if not _commit_cursor_updates({exact[1]: row}):
                    return False
            return True
        if wake is not None:
            row = dict(wake)
            starts = [parts[2] for token in wake.get("held") or ()
                      for parts in [_occurrence_parts(token)]
                      if parts and parts[:2] == (wake.get("dev"), wake.get("ino"))]
            if starts:
                row["off"] = min(int(row.get("off") or 0), min(starts))
                row["base"] = min(int(row.get("base") or 0), row["off"])
                row["rid"] = None
            row.pop("held", None)
            row.pop("done", None)
            return bool(_commit_cursor_updates({exact[0]: row}))
        if inherit_existing and session:
            ground = _baseline_state(room)
            candidates = []
            for path in locked:
                parsed = parse_cursor_path(path)
                if parsed is None or parsed["beacon"]:
                    continue
                cur = normalize_rotated_cursor(
                    room, pk.read_json(path, None), path=path)
                if isinstance(cur, dict) and isinstance(cur.get("off"), int) \
                        and (cur.get("dev"), cur.get("ino")) == ground[:2] \
                        and 0 <= cur["off"] <= ground[2]:
                    candidates.append(cur)
            if candidates:
                row = dict(max(candidates, key=lambda cur: cur["off"]))
                row.pop("held", None)
                row.pop("done", None)
                updates = {path: dict(row) for path in set(exact) | set(base_pair)}
                return bool(_commit_cursor_updates(updates))
        inherited = _cursor(room, seat) if session else None
        if inherited is not None:
            row = dict(inherited)
            row.pop("held", None)
            row.pop("done", None)
            updates = {exact[0]: row, exact[1]: dict(row)}
            if not _commit_cursor_updates(updates):
                return False
            return True
        state = _baseline_state(room, at_start=at_start)
        row = _cursor_state(state, base=state[2])
        updates = {path: dict(row) for path in exact}
        if session:
            updates.update({path: dict(row) for path in base_pair})
        return False if not _commit_cursor_updates(updates) else False
def _say(report, outcome, detail):
    """Record one room read's coverage, when the caller asked for it."""
    if isinstance(report, dict):
        report["outcome"] = outcome
        report["detail"] = detail


def _tail(room, cur, report=None):
    """Read one bounded window of complete rows from ONE fstat'd fd.
    -> (dev, ino, base_off, entries, ground_rid, skip_rid), or None when
    there is nothing to read. entries = [(row_dict|None, start_off, end_off)] for every
    COMPLETE line; a trailing partial line is never consumed.

    `report`, when a dict is passed, is FILLED with what this read could and
    could not see -- `outcome` one of read/truncated/unreadable/absent, plus a
    `detail` for the two that are not plain reads. The six-tuple answers "what
    did you find" and a caller clearing an alarm needs "what COULD you have
    found": a None return means ABSENT, UNREADABLE and NOTHING-NEW at once, and
    a full window means the rows past SCAN_CAP were never in the sample.
    Callers passing no dict are unchanged; this adds a channel, not a return.

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
        _say(report, "absent", "the room file does not exist")
        return None                      # ABSENT: genuinely nothing to read
    except OSError as exc:
        # THE NINTH ESCAPE: an unreadable room TAIL returned the
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
        _say(report, "unreadable",
             "the room could not be opened (%s)" % type(exc).__name__)
        return None
    with f:
        st = os.fstat(f.fileno())
        off, suppress = cur.get("off", 0), cur.get("skip")
        if (st.st_dev, st.st_ino) != (cur.get("dev"), cur.get("ino")) \
                or st.st_size < off:
            off, suppress = 0, cur.get("rid")   # rotation / replacement
        elif st.st_size == off:
            _say(report, "read", None)
            if suppress:
                return st.st_dev, st.st_ino, 0, [], None, None
            return None                          # genuinely nothing new
        f.seek(off)
        data = f.read(SCAN_CAP)
        # THE BYTES THIS READ SAW, for a caller that asked by putting a
        # `window` key in its report. An authorization taken from one row of
        # this window is only as good as that row's bytes staying where they
        # were, and only this read, on this one fstat'd fd, knows them.
        if isinstance(report, dict) and "window" in report:
            report["window"] = (st.st_dev, st.st_ino, off, data)
        # THE CAP IS THE COVERAGE QUESTION. OLD IS AGE, NOT BYTE OFFSET: the
        # window starts at the cursor and runs FORWARD SCAN_CAP bytes, so enough
        # non-eligible rows between the cursor and the oldest eligible addressed
        # row push that row past the end. A caller reading absence out of a
        # truncated window is reading a gap in its own sampling.
        _say(report, "truncated" if off + len(data) < st.st_size else "read",
             "the window read %d bytes from offset %d and the room is %d bytes: "
             "rows past that are outside this pass, not absent"
             % (len(data), off, st.st_size)
             if off + len(data) < st.st_size else None)
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
            entries.append((row, pos, end))
        pos = end
    base, ground = off, cur.get("rid")
    if suppress is not None:
        for i, (row, _start, end) in enumerate(entries):
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
            base = entries[-1][2] if entries else off
            return st.st_dev, st.st_ino, base, [], ground, suppress
    return st.st_dev, st.st_ino, base, entries, ground, None
# THE ROOM SCAN MOVED OUT, and `seats_roomscan` says why: where a pass is
# entitled to LOOK is a different question from what it FINDS there, and
# every consumer of the rotation reaches for exactly these three names.
# Re-exported so no existing import path changed.
from .seats_roomscan import (_fair_room_slice,  # noqa: F401,E402
                             _scan_rooms, scan_path)
def _room_dirty(room, seat, session=None, beacon=False):
    """Lock-free precheck: could `room` hold rows past this consumer cursor?
    A missing cursor is dirty (a room this seat has never looked
    at). Otherwise ONE stat against the atomically-written cursor: same
    file identity and size == off ⇒ clean. False positives are fine
    (deliver re-checks under the lock); a false negative cannot happen —
    an append grows the size, a rotation/replacement changes the inode.
    This keeps the every-tool-call hot path at ~one stat per quiet room."""
    # An exact session never borrows a co-named seat/session's clean ground.
    # Missing exact state is DIRTY so the locked initializer can inherit once;
    # a seat-level fallback here starved the session forever on quiet rooms.
    cur = _cursor(room, seat, session, beacon=beacon)
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
            backfill=False, scope=None, ambient=True, channel=None,
            sink_usable=None):
    """Serialize the live credential-wall decision with one delivery event."""
    # ORDER IS LOAD-BEARING: the DISPUTE rung speaks first. Both refusals stop
    # the same beat, but a disputed pane's operator is looking for the words
    # "IDENTITY DISPUTE" — three arms pin that string — and a resolver that
    # answered first would replace a diagnosis with a different diagnosis.
    if _warn_disagreement(session, "delivery"):
        return None
    seat = seat or _own_delivery_seat(session, cwd, "deliver")
    if not seat:
        return None
    with _delivery_guard(seat, session) as pause:
        if pause:
            return None
        return _deliver_unpaused(
            session=session, room=room, seat=seat, emit=emit, cwd=cwd,
            backfill=backfill, scope=scope, ambient=ambient, channel=channel,
            sink_usable=sink_usable)
def _deliver_unpaused(session=None, room="main", seat=None, emit=None, cwd=None,
                      backfill=False, scope=None, ambient=True, channel=None,
                      sink_usable=None):
    """The tool-boundary nudge, ONE room: at most ONE delivery event, oldest
    first; later matches stay PENDING (their count shows, their cursor ground is
    not consumed — codex H6). A reaction event consumes a contiguous same-target
    burst, consistently for boundary hooks and beacons. Returns the label line
    or None.

    ambient=True is THIS lane's own scope (the boundary hook keeps the
    home-room surface, less subsystem rows: see machine_senders);
    ambient=False (the default beacon, via deliver_any) narrows waking to
    mentions/replies/DMs/@all. Ordinary ambient rows skipped by that tuning
    remain pull-only as before. Mute is different: it gates wake only, so a
    normally deliverable muted row is held for the hook rather than consumed
    by the beacon cursor.

    At-most-once: cursor state lands before the external effect. Effect failure
    or process death leaves the receipt UNKNOWN but never replays that physical
    occurrence.

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
    if not recover_room_rotation(room):
        return None                         # interrupted rotation stays fail-open
    if not touch_seen(seat, session=session):               # presence FIRST — a seat muted by the
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
    beacon = channel == "beacon"
    had_delivery = _cursor(room, seat, session) is not None
    if not had_delivery or _cursor(room, seat, session, beacon=True) is None:
        inherited = _init_cursor(room, seat, session, at_start=backfill)
        if not had_delivery and not inherited and not backfill:
            return None                 # fresh EOF baseline: backlog never floods
    paths = _cursor_pair(room, seat, session)
    # Quiet delivery takes exact locks; rename/rotation take the same locks.
    with _cursor_locks(paths, estate=False):
        delivery_cur = _cursor(room, seat, session)
        wake_cur = _cursor(room, seat, session, beacon=True)
        if delivery_cur is None or wake_cur is None:
            return None
        cur = wake_cur if beacon else delivery_cur
        got = _tail(room, cur)
        if got is None:
            return None
        dev, ino, base, entries, last_rid, skip = got
        held = set(wake_cur.get("held") or ())
        done = set(wake_cur.get("done") or ())
        same_wake_file = (dev, ino) == (wake_cur.get("dev"),
                                        wake_cur.get("ino"))

        def decision(candidate, start, end):
            """Pure routing decision. Coordination mutates only on commit."""
            token = _occurrence(dev, ino, start)
            if candidate is None:
                return False, False, False, token
            if beacon:
                suppressed = token in done
                if suppressed:
                    return False, False, True, token
                wakes = deliverable(candidate, seat, room, sc, ambient, True)
                normal = deliverable(candidate, seat, room, sc, True, False)
                hold = normal and not wakes and (pk.slug(room) in sc["mute"]
                                                 or machine_senders.owner_rail(candidate))
                return wakes, hold, False, token
            already_woke = same_wake_file and end <= wake_cur.get("off", 0) \
                and token not in held
            reaches = token in held or (not already_woke and deliverable(
                candidate, seat, room, sc, ambient, False))
            return reaches, False, False, token

        def cross(rows):
            """Apply coordination only for physical rows the cursor crosses."""
            for candidate, start, end in rows:
                reaches, hold, suppressed, token = decision(
                    candidate, start, end)
                if beacon:
                    if suppressed:
                        done.discard(token)
                    elif hold:
                        held.add(token)
                    continue
                if token in held and reaches:
                    held.discard(token)

        def coordinated_updates(target):
            """Pair the commit; hook-first aligns wake state when no hold remains."""
            if beacon:
                wake = dict(target)
            else:
                wake = dict(wake_cur)
                ahead = (target.get("dev"), target.get("ino")) != (
                    wake.get("dev"), wake.get("ino")) or target.get("off", 0) > \
                    wake.get("off", 0)
                if ahead and not held:
                    wake = dict(target)
                    wake["active"] = bool(target.get("active") or
                                          wake_cur.get("active"))
                    done.clear()
            for field, values in (("held", held), ("done", done)):
                if values:
                    wake[field] = sorted(values)
                else:
                    wake.pop(field, None)
            return ({paths[1]: wake, paths[0]: dict(delivery_cur)} if beacon
                    else {paths[0]: target, paths[1]: wake})

        # A rotation/replacement (_tail restarts at 0 on a new dev,ino) voids
        # the join baseline: it indexes the now-gone old file, so carrying it
        # forward would false-SENT the new file's genuinely-delivered rows.
        cbase = cur.get("base") \
            if (dev, ino) == (cur.get("dev"), cur.get("ino")) else 0
        hit = None
        crossed = []
        last_end = base
        for i, (row, start, end) in enumerate(entries):
            if decision(row, start, end)[0]:
                hit = (i, row, start, end)
                break
            crossed.append((row, start, end))
            last_end, last_rid = end, (row or {}).get("id") or last_rid
        if hit is None:
            cross(crossed)
            target = _cursor_state((dev, ino, last_end, last_rid),
                                   active=cur.get("active") or bool(entries),
                                   base=cbase)
            if skip:
                target["skip"] = skip
            if not _commit_cursor_updates(coordinated_updates(target)): return None
            return None
        i, row, start, end = hit
        grouped, cursor_end, cursor_rid, last_i = [row], end, row.get("id"), i
        crossed.append((row, start, end))
        if row.get("react"):
            # Collapse a contiguous same-target reaction burst, never crossing
            # the first different obligation merely to count it.
            for j, (other, other_start, other_end) in enumerate(
                    entries[i + 1:], i + 1):
                reaches = decision(other, other_start, other_end)[0]
                if reaches and (not other.get("react")
                                or not _same_reaction_target(row, other)):
                    break
                crossed.append((other, other_start, other_end))
                if reaches:
                    grouped.append(other)
                last_i, cursor_end = j, other_end
                cursor_rid = (other or {}).get("id") or cursor_rid
        waiting = sum(1 for other, other_start, other_end
                      in entries[last_i + 1:]
                      if decision(other, other_start, other_end)[0])
        is_dm = room.startswith(chat.DM_PREFIX)
        where = (" dm" if is_dm else "" if room == "main"
                 else " #%s" % room)
        row_ts = str(row.get("ts") or "?")
        try:
            if row.get("react"):
                line = "[helm chat reaction%s → %s @%s] %s" % (
                    where, seat, row_ts, _reaction_wake_body(grouped, room))
            else:
                line = "[helm chat%s → %s @%s] %s: %s" % (
                    where, seat, row_ts, chat._dsan(row.get("from") or "?"),
                    _clip(_scrub(row.get("text") or "")))
        except Exception as exc:
            cross(crossed)
            target = _cursor_state((dev, ino, cursor_end, cursor_rid),
                                   active=True, base=cbase)
            if not _commit_cursor_updates(coordinated_updates(target)): return None
            try:
                os.write(2, ("[helm chat] skipped an unrenderable row %r in %s "
                             "(%s) — advancing past it\n"
                             % (row.get("id"), room, exc)).encode(
                                 "utf-8", "replace"))
            except OSError:
                pass
            return None
        if waiting:
            line += " (+%d waiting — helm chat read%s)" % (
                waiting, " --dm" if is_dm
                else "" if room == "main" else " --room %s" % room)
        if sink_usable is False:
            # THE FENCE: A CONSUMER THAT CANNOT DELIVER MUST NOT CONSUME.
            #
            # The commit below advances this seat's cursor PAST an addressed
            # occurrence and only then emits, so a consumer whose output
            # reaches nobody has already taken the row out of the ledger as
            # far as every other reader is concerned. That is not a duplicate
            # and it is not noisy: it is a wake that silently never happens
            # while every surface reports coverage. Two live consumers sharing
            # one (seat, session) cursor, the dead-sink one acquiring first,
            # is all it takes.
            #
            # ONLY A PROVEN-UNUSABLE SINK WITHHOLDS. `sink_usable` is
            # TRI-STATE and None means UNKNOWN, which commits exactly as
            # before — this module refuses only what can be PROVEN to reach no
            # reader, the same asymmetry `beacons.sink_state` is built on.
            # The caller supplies it because only the caller knows where
            # `emit` goes: fd 1 is the right thing to stat for a follower
            # writing its own stdout, and the wrong thing entirely for an
            # injected collector whose destination is a list.
            #
            # NOTHING HERE NEEDS AUTHORITY OVER ANOTHER PROCESS. The decision
            # is about whether THIS committer may commit, so it cannot weaken
            # the reap=False rule that a process may not signal another seat's
            # waiter — the fence is in the delivery path, where the row said
            # it belongs.
            return None
        cross(crossed)
        target = _cursor_state((dev, ino, cursor_end, cursor_rid),
                               active=True, base=cbase)
        with hookrun.deadline_held():   # a deadline cannot split commit from emit
            if not _commit_cursor_updates(coordinated_updates(target)): return None
            if emit is not None: emit(line)
        if emit is not None:            # best-effort evidence, still bounded
            record_delivery_receipt(row, room, seat, session, channel,
                                    occurrence=_occurrence(dev, ino, start))
        return line
def deliver_any(session=None, seat=None, emit=None, cwd=None, room="main",
                ambient=True, channel=None, sink_usable=None):
    """The MULTI-ROOM boundary nudge (slice 5 — what the PostToolUse hook and
    the beacon actually call): one delivery event per boundary from the first
    room that has one — the primary room first, then the rest of
    _scan_rooms' bounded, newest-activity-first list. An @mention in a
    channel the seat never joined must wake it (the owner's helm-dogfood
    mention post), so a TRACKED seat — it holds
    a primary-room cursor — meeting a cursor-less room BACKFILLS from offset
    0: a room born after its join is all post-join news. An UNtracked seat
    (never joined / reaped / pre-install) keeps the EOF self-heal everywhere:
    pre-join backlog never floods. Scanning a clean room advances only that
    room's cursor; a hit STOPS the scan, so later rooms keep their pending
    for the next boundary (one nudge per boundary — the budget stays flat).
    Exceptions propagate exactly like deliver's (H7: commit precedes emit, so
    a died effect remains UNKNOWN and never replays); callers wrap fail-open."""
    # DISPUTE SPEAKS FIRST, then admission — see deliver for why the order is
    # load-bearing. Either way: no beat, no consume; the beat must stop HERE
    # too or the dispute reads as presence.
    if _warn_disagreement(session, "delivery"):
        return None
    seat = seat or _own_delivery_seat(session, cwd, "self-heal")
    if not seat:
        return None
    with _delivery_guard(seat, session) as pause:
        if pause:
            # DELIVERY is the actuator. A waiter that froze old config at arm
            # time reads this latch every poll; presence and cursors stay still.
            return None
        if not touch_seen(seat, session=session):  # presence even when every room is quiet — and a
            return None           # cross-seat probe drains no one else's inbox
    # A CONSUMER THAT CANNOT DELIVER MUST NOT SPEND THE SHARED ROTATION.
    # `_fair_room_slice` advances a PERSISTED per-(seat, session, lane) ring
    # eagerly at SCAN time, before anyone knows whether a row was found or
    # delivered -- so a pass that was going to withhold every row anyway still
    # moved the frontier. With a dead and a usable consumer alternating, each
    # pass handed the other the half it had just left, and the addressed row
    # sat in a room the usable one never reached. Starvation with no error
    # anywhere: both processes healthy, both scanning, the row pending
    # forever.
    #
    # THE FENCE BELONGS BEFORE THE SCAN, not inside the per-room loop. The
    # per-room fence in `deliver` stops the COMMIT, which is what protects the
    # row; this stops the pass from consuming a SHARED budget it cannot act
    # on. Presence is still stamped above, because a waiter that cannot
    # deliver is still present -- that is a fact about the seat, not about
    # this pass's ability to spend rows.
    if sink_usable is False:
        return None
    sc = seat_scope(seat)     # ONE roster read for the whole pass
    primary = (cursor_path(room, seat, session), cursor_path(room, seat))
    tracked = sc["tracked"] or any(
        _cursor_transaction_pending(path) for path in primary) \
        or (_cursor(room, seat, session) or _cursor(room, seat)) is not None
    for r in _scan_rooms(
            room, seat=seat, scope=sc, session=session,
            scan_lane="deliver"):
        if not _room_dirty(r, seat, session, beacon=channel == "beacon"):
            continue
        # the DM lane ALWAYS backfills from 0 — every row in it is addressed
        # to this seat, so even an untracked (reaped/pre-install) seat must
        # get the DM that created its lane, never an EOF skip
        line = deliver(session=session, room=r, seat=seat, emit=emit, cwd=cwd,
                       backfill=(tracked and r != room)
                       or r.startswith(chat.DM_PREFIX), scope=sc,
                       ambient=ambient, channel=channel,
                       sink_usable=sink_usable)
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
    hits = [k for k in r if recipient_matches(k, canonical)] \
        or alias_keys(canonical, r)   # a live rename alias -> the row it names
    if len(hits) == 1:                # ...and the CANONICAL address comes back
        return _CanonicalRecipient(str(hits[0]).casefold(), hits[0]), None
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
    # A DM IS AN ACT, not speech: it lands in one seat's private lane and
    # opens an obligation on the ack ladder. So the author is resolved through
    # the identity layer's one definition — an admitted capability from the CLI
    # door, an explicit on-behalf-of name from a bot or the web surface, and
    # AMBIENT REFUSES rather than minting. The frozen first attempt guarded
    # only the ambient branch here while `chat._seat_actor` above still handed
    # `who` a DERIVED name; closing that meant fixing the SOURCE, which is why
    # `who` now arrives as a capability from every CLI leg.
    from . import actors
    sender, _aerr = actors.attributed(who, session, act="send a DM")
    if _aerr:
        return None, _aerr
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
