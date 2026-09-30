"""ROTATION'S CURSOR TRANSACTIONS: the two passes a room's compaction runs
over every cursor that names it.

`chat` compacts a room log that outgrew its cap by cutting a retained tail
into a new inode. Before the cut it asks `rotation_hold_offset` for the
earliest offset any still-unread cursor needs, so no undelivered row is cut
away; after it, `remap_rotated_cursors` moves every cursor onto the new
inode and installs the compacted log in one cursor transaction. Both hold
the cursor topology lock, then the room's lock, then the seat estates', in
`seats_cursor`'s order.

It is its own file because it is a different question from delivering a
room's rows, and because only the rotation (through the `seats` facade)
calls it. `seats_delivery` re-exports both names, so no existing import path
changed.
"""
import os

from . import chat, pk
from .seats_common import roster
from .seats_runtime import runtime_for_session
from .seats_cursor import (_commit_cursor_updates, _cursor_estate_locks,
                           _cursor_locks, _cursor_room_locks,
                           _cursor_topology_lock, _occurrence,
                           _occurrence_parts, _strict_json,
                           beacon_cursor_path, cursor_path,
                           normalize_rotated_cursor, parse_cursor_path,
                           rotation_journal)


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
