#!/usr/bin/env python3
"""Canonical cursor identity, locking, transactions, and activity indexing.

Every delivery/wake caller uses these builders and this lock order. Keeping the
mechanism below delivery prevents catchup, initialization, rename, and web
activity from growing competing filename parsers or AB/BA lock orders.
"""

import contextlib
import json
import os
import tempfile
import uuid

from . import chat, pk
from .seats_common import (_Fault, _flocked, _reason, _seat_key,
                           recipient_matches)

_CURSOR_MARK = ".cursor."
_BEACON_PREFIX = ".beacon-"


class CursorCommitResult:
    """Boolean commit verdict carrying whether a failed write was restored."""
    __slots__ = ("committed", "rollback_complete")

    def __init__(self, committed, rollback_complete=True):
        self.committed = bool(committed)
        self.rollback_complete = bool(rollback_complete)

    def __bool__(self):
        return self.committed


def _fsync_dir(path):
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return True
    except OSError:
        return False


def _durable_json(path, row):
    root = os.path.dirname(path)
    os.makedirs(root, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(row, f, separators=(",", ":"))
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        if not _fsync_dir(root):
            raise OSError("journal directory fsync failed")
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
    return row


def _cursor_txn_pointer(path):
    """One atomic transaction owner per room; no partial path publication."""
    parsed = parse_cursor_path(path)
    if parsed is None:
        raise OSError("cursor transaction path is malformed")
    return os.path.join(chat.chat_dir(),
                        ".cursor-txn-room.%s.json" % parsed["room"])


def _cursor_transaction_epoch(path):
    """Persistent room seqlock token plus whether reads must fail closed."""
    try:
        row = _cursor_journal(_cursor_txn_pointer(path))
    except OSError:
        return ("fault",), True
    if row is None:
        return None, False
    if not isinstance(row, dict) or not isinstance(row.get("tx"), str):
        return ("fault",), True
    state = row.get("state")
    return ((state, row["tx"]), state == "prepared") \
        if state in ("prepared", "committed", "rolledback") \
        else (("fault",), True)


def _cursor_transaction_pending(path):
    return _cursor_transaction_epoch(path)[1]


def _strict_json(path):
    try:
        with pk.open_regular(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError) as exc:
        raise OSError("JSON storage is unreadable") from exc


def _cursor_journal(path):
    """Prepared image plus fsynced same-inode outcome witnesses.

    A torn final append is not an outcome. Complete malformed witnesses remain
    UNKNOWN rather than being mistaken for prepared recovery state.
    """
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as exc:
        raise OSError("cursor transaction storage is unreadable") from exc
    lines = raw.splitlines(keepends=True)
    complete = [line for line in lines if line.endswith("\n")]
    if not complete or len(lines) - len(complete) > 1:
        raise OSError("cursor transaction journal is malformed")
    try:
        row = json.loads(complete[0])
    except (ValueError, TypeError) as exc:
        raise OSError("cursor transaction journal is malformed") from exc
    if not isinstance(row, dict):
        raise OSError("cursor transaction journal is malformed")
    for line in complete[1:]:
        try:
            mark = json.loads(line)
        except (ValueError, TypeError) as exc:
            raise OSError("cursor outcome witness is malformed") from exc
        if not isinstance(mark, dict) or mark.get("tx") != row.get("tx") \
                or mark.get("state") not in (
                    "prepared", "committed", "rolledback"):
            raise OSError("cursor outcome witness is malformed")
        row = dict(row, state=mark["state"])
    return row


def _cursor_txn(path):
    journal_path = _cursor_txn_pointer(path)
    row = _cursor_journal(journal_path)
    if row is None:
        return None
    if not isinstance(row, dict) or row.get("state") not in (
            "prepared", "committed", "rolledback"):
        raise OSError("cursor transaction journal is malformed")
    if row["state"] != "prepared":
        return None
    names, before, after = row.get("paths"), row.get("before"), row.get("after")
    if not isinstance(names, list) or not isinstance(before, dict) \
            or not isinstance(after, dict) or len(names) != len(set(names)):
        raise OSError("cursor transaction images are malformed")
    paths = []
    for basename in names:
        if not isinstance(basename, str) or os.path.basename(basename) != basename:
            raise OSError("cursor transaction path escaped chat storage")
        other = os.path.join(chat.chat_dir(), basename)
        parsed = parse_cursor_path(other)
        if parsed is None or _cursor_txn_pointer(other) != journal_path \
                or before.get(basename) is not None \
                and not isinstance(before.get(basename), str) \
                or not isinstance(after.get(basename), str):
            raise OSError("cursor transaction image is malformed")
        paths.append(os.path.abspath(other))
    return journal_path, row, paths


def _cursor_txn_closure(paths):
    out = {os.path.abspath(path) for path in paths}
    for path in list(out):
        tx = _cursor_txn(path)
        if tx is not None:
            out.update(tx[2])
    if len(out) > 4096:
        raise OSError("cursor transaction lock census exceeds bound")
    return out


def _durable_text(path, raw):
    root = os.path.dirname(path)
    os.makedirs(root, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        if not _fsync_dir(root):
            raise OSError("cursor directory fsync failed")
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _publish_cursor_outcome(journal_path, row):
    """Fsync an outcome on the already-durable journal inode.

    Settlement mutates no directory entry: a failed parent-directory fsync can
    neither resurrect ``prepared`` after an emitted effect nor hide this mark.
    """
    mark = (json.dumps({"tx": row["tx"], "state": row["state"]},
                       separators=(",", ":")) + "\n").encode("utf-8")
    fd = os.open(journal_path, os.O_WRONLY | os.O_APPEND)
    try:
        sent = 0
        while sent < len(mark):
            wrote = os.write(fd, mark[sent:])
            if not wrote:
                raise OSError("cursor outcome witness write made no progress")
            sent += wrote
        os.fsync(fd)
    finally:
        os.close(fd)


def _restore_cursor_txn(journal_path, row):
    try:
        for basename in reversed(row["paths"]):
            path = os.path.join(chat.chat_dir(), basename)
            raw = row["before"][basename]
            if raw is None:
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
                if not _fsync_dir(chat.chat_dir()):
                    raise OSError("cursor removal is not durable")
            else:
                _durable_text(path, raw)
        _publish_cursor_outcome(journal_path, dict(row, state="rolledback"))
        return True
    except (OSError, ValueError, TypeError, UnicodeError):
        return False


def _recover_cursor_txns(paths):
    locked = {os.path.abspath(path) for path in paths}
    transactions = {}
    for path in locked:
        tx = _cursor_txn(path)
        if tx is not None:
            transactions[tx[0]] = tx[1:]
    for journal_path, (row, affected) in sorted(transactions.items()):
        if not set(affected) <= locked or not _restore_cursor_txn(
                journal_path, row):
            raise OSError("cursor transaction recovery is incomplete")


def _prepare_cursor_txn(paths, before, updates):
    parsed = [parse_cursor_path(path) for path in paths]
    rooms = {item["room"] for item in parsed if item}
    if len(rooms) != 1 or len(parsed) != len(paths):
        raise OSError("cursor transaction must belong to one room")
    names = [os.path.basename(path) for path in paths]
    after = {os.path.basename(path): json.dumps(
        updates[path], indent=2, ensure_ascii=False, sort_keys=False) + "\n"
             for path in paths}
    row = {"v": 3, "room": next(iter(rooms)), "tx": uuid.uuid4().hex,
           "state": "prepared", "paths": names,
           "before": {name: before[path] for path, name in zip(paths, names)},
           "after": after}
    journal_path = _cursor_txn_pointer(paths[0])
    _durable_json(journal_path, row)
    return journal_path, row


def _sid8(session):
    """The session's cursor-key token — filename-safe, 8 chars, None-safe."""
    return pk.slug(str(session))[:8] if session else None


def _cursor_path_from_keys(room_key, seat_key, session_key=None, beacon=False,
                           root=None):
    """Build a cursor path from already-canonical filename components.

    `root` is the chat dir a caller ALREADY resolved: chat.chat_dir() runs
    home.surface_origin's expanduser+realpath every call, so a per-file loop
    pays that per file for a loop-invariant answer. Passing one snapshot is
    also stricter — a walk cannot splice two roots into one index."""
    name = "%s%s%s" % (room_key, _CURSOR_MARK, seat_key)
    if session_key:
        name += "." + session_key
    return os.path.join(chat.chat_dir() if root is None else root,
                        (_BEACON_PREFIX if beacon else "") + name)


def _cursor_path(room, seat, session=None, beacon=False):
    """Build from public room/seat/session values through canonical keys."""
    return _cursor_path_from_keys(pk.slug(room), _seat_key(seat),
                                  _sid8(session), beacon=beacon)


def cursor_path(room, seat, session=None):
    """The delivery-obligation cursor for one exact seat/session."""
    return _cursor_path(room, seat, session)


def beacon_cursor_path(room, seat, session=None):
    """The idle-wake cursor for the same exact seat/session."""
    return _cursor_path(room, seat, session, beacon=True)


def _cursor_checked(room, seat, session=None):
    """Read exact cursor as present, proven missing, or explicit fault.
    Only proven missing permits fallback; pk.read_json cannot preserve that.
    """
    path = cursor_path(room, seat, session)
    epoch, pending = _cursor_transaction_epoch(path)
    if pending:
        return None, _Fault("cursor", path, "transaction pending")
    try:
        with pk.open_regular(path, encoding="utf-8") as f:
            row = json.load(f)
    except FileNotFoundError:
        after, pending = _cursor_transaction_epoch(path)
        return ((None, _Fault("cursor", path, "transaction changed"))
                if pending or after != epoch else (None, None))
    except (OSError, ValueError) as exc:
        reason = _reason(exc) if isinstance(exc, OSError) else "undecodable"
        return None, _Fault("cursor", path, reason)
    after, pending = _cursor_transaction_epoch(path)
    if pending or after != epoch:
        return None, _Fault("cursor", path, "transaction changed")
    if isinstance(row, dict) and isinstance(row.get("off"), int):
        return normalize_rotated_cursor(room, row, path=path), None
    return None, _Fault("cursor", path, "malformed")


def parse_cursor_path(path):
    """Canonical cursor filename parser, or None for unrelated/malformed state.

    Legacy long session suffixes remain valid: the first field after the seat
    key is opaque and may itself contain dots. Lock files are deliberately not
    cursors; every caller handles the stable sibling lock separately.
    """
    name = os.path.basename(path)
    if name.endswith(".lock"):
        return None
    beacon = name.startswith(_BEACON_PREFIX)
    if beacon:
        name = name[len(_BEACON_PREFIX):]
    room, mark, rest = name.partition(_CURSOR_MARK)
    if not mark or not room or not rest:
        return None
    seat_key, dot, session_key = rest.partition(".")
    if not seat_key or dot and not session_key:
        return None
    return {"room": room, "seat_key": seat_key,
            "session_key": session_key if dot else None,
            "beacon": beacon, "path": os.path.abspath(path)}


def _cursor_lock_key(path):
    """One global lock order: room, seat, base/session, delivery/wake."""
    parsed = parse_cursor_path(path)
    if parsed is None:
        return (1, os.path.abspath(path))
    return (0, parsed["room"], parsed["seat_key"],
            parsed["session_key"] is not None, parsed["session_key"] or "",
            parsed["beacon"])


def _cursor_topology_path():
    return os.path.join(chat.chat_dir(), ".cursor-topology.lock")


@contextlib.contextmanager
def _cursor_topology_lock():
    """Freeze cursor creation and identity-changing topology operations."""
    with _flocked(_cursor_topology_path()):
        yield


def _cursor_room_path(room_key):
    return os.path.join(chat.chat_dir(), ".cursor-room.%s.lock" % room_key)


def _cursor_estate_path(seat_key):
    return os.path.join(chat.chat_dir(), ".cursor-estate.%s.lock" % seat_key)


def _cursor_txn_lock_path(room_key):
    return os.path.join(chat.chat_dir(), ".cursor-txn-room.%s.lock" % room_key)


def seat_incarnation(seat, session=None):
    """Current durable identity generation, optionally bound to one session."""
    from .seats_roster import roster_checked

    rows, fault = roster_checked()
    if fault:
        return None
    hits = [(name, row) for name, row in rows.items()
            if recipient_matches(name, seat) and isinstance(row, dict)]
    sid = str(session) if session else None
    if len(hits) != 1:
        if hits:
            return None
        bound = any(sid in {str(value) for value in
                            [row.get("session")] + list(row.get("sessions") or ())
                            if value}
                    for row in rows.values() if sid and isinstance(row, dict))
        if not bound:
            return "unrostered:%s:%s" % (
                _seat_key(seat), _sid8(sid) if sid else "sessionless")
        return None
    row = hits[0][1]
    sessions = [row.get("session")] + list(row.get("sessions") or ())
    if sid and sid not in {str(value) for value in sessions if value}:
        bound = any(sid in {str(value) for value in
                            [other.get("session")] +
                            list(other.get("sessions") or ()) if value}
                    for other in rows.values() if isinstance(other, dict))
        if bound:
            return None
    incarnation = row.get("incarnation")
    if isinstance(incarnation, str) and incarnation:
        return incarnation
    joined = row.get("joined")
    return "legacy:%s:%s" % (_seat_key(seat), str(joined or "unknown"))


@contextlib.contextmanager
def seat_state_lock(seat, session=None, incarnation=None):
    """Serialize keyed state and reject a stale identity generation."""
    from .seats_rename import recover_seat_rename, rename_journal_path

    expected = incarnation or seat_incarnation(seat, session=session)
    if not expected or not recover_seat_rename():
        yield False
        return
    key = _seat_key(seat)
    with _cursor_estate_locks({key}):
        try:
            actual = seat_incarnation(seat, session=session)
            current = bool(actual and actual == expected
                           and not os.path.exists(rename_journal_path())
                           and chat._dm_redirect(chat.DM_PREFIX + key) is None)
        except OSError:
            current = False
        yield current


def _write_stop_latch(path, seat, value, session=None, incarnation=None):
    """Write a keyed stop sidecar for this seat incarnation only."""
    with seat_state_lock(seat, session=session,
                         incarnation=incarnation) as current:
        if not current:
            return False
        chat._ensure_dir()
        pk.atomic_write(path, value)
        return True


def _remove_stop_latch(path, seat, session=None, incarnation=None):
    """Remove a keyed stop sidecar without crossing rename/key reuse."""
    with seat_state_lock(seat, session=session,
                         incarnation=incarnation) as current:
        if not current:
            return False
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return True


def _probe_stop_latch(path, seat, session=None):
    """Prove latch storage writable and return its identity generation."""
    with seat_state_lock(seat, session=session) as current:
        if not current:
            return None
        incarnation = seat_incarnation(seat, session=session)
        if not incarnation:
            return None
        chat._ensure_dir()
        pk.atomic_write(path, "arm")
        try:
            os.unlink(path)
        except OSError:
            pass
        return incarnation


@contextlib.contextmanager
def _cursor_room_locks(room_keys):
    """Freeze cursor creation/movement inside canonical room keys."""
    with contextlib.ExitStack() as stack:
        for key in sorted({str(key) for key in room_keys if key}):
            stack.enter_context(_flocked(_cursor_room_path(key)))
        yield


@contextlib.contextmanager
def _cursor_estate_locks(seat_keys):
    """Freeze cursor creation/movement for canonical seat keys."""
    with contextlib.ExitStack() as stack:
        for key in sorted({str(key) for key in seat_keys if key}):
            stack.enter_context(_flocked(_cursor_estate_path(key)))
        yield


@contextlib.contextmanager
def _cursor_locks(paths, estate=True):
    """Lock/recover the closure in the sole global path order. ONE LOCK PER
    ROOM (the closure joins only same-room paths and refuses a roomless one),
    never a file per cursor: that file is unlinkable at release — it would
    hand the next caller a fresh inode — so it grew what `list_rooms` scans."""
    wanted = {os.path.abspath(path) for path in paths}
    while True:
        ordered = sorted(_cursor_txn_closure(wanted), key=_cursor_lock_key)
        parsed = [item for path in ordered
                  for item in [parse_cursor_path(path)] if item is not None]
        rooms = {item["room"] for item in parsed}
        seats = {item["seat_key"] for item in parsed}
        scope = (contextlib.ExitStack() if estate else contextlib.nullcontext())
        retry = False
        with scope as scopes, contextlib.ExitStack() as stack:
            if estate:
                scopes.enter_context(_cursor_topology_lock())
                scopes.enter_context(_cursor_room_locks(rooms))
                scopes.enter_context(_cursor_estate_locks(seats))
            for room in sorted(rooms):
                stack.enter_context(_flocked(_cursor_txn_lock_path(room)))
            closed = _cursor_txn_closure(ordered)
            if closed != set(ordered):
                wanted = closed
                retry = True
            else:
                _recover_cursor_txns(ordered)
                yield ordered
        if not retry:
            return


def _cursor_pair(room, seat, session=None):
    return (cursor_path(room, seat, session),
            beacon_cursor_path(room, seat, session))


def _cursor_paths(room, seat, sessions=(), checked=False):
    """Seat-level plus every known/on-disk delivery+wake cursor for one room."""
    paths = set(_cursor_pair(room, seat))
    for session in sessions:
        paths.update(_cursor_pair(room, seat, session))
    wanted = (pk.slug(room), _seat_key(seat))
    try:
        names = os.listdir(chat.chat_dir())
    except OSError:
        if checked:
            raise
        return paths
    for name in names:
        parsed = parse_cursor_path(os.path.join(chat.chat_dir(), name))
        if parsed and (parsed["room"], parsed["seat_key"]) == wanted:
            paths.add(parsed["path"])
    return paths


@contextlib.contextmanager
def _all_cursor_locks(room, seat, sessions=()):
    """Freeze/recover one room+seat estate without missing an initializer."""
    with _cursor_topology_lock(), _cursor_room_locks({pk.slug(room)}), \
            _cursor_estate_locks({_seat_key(seat)}):
        paths = _cursor_paths(room, seat, sessions, checked=True)
        with _cursor_locks(paths, estate=False) as locked:
            yield set(locked)


def _occurrence(dev, ino, start):
    """Stable identity for one physical row occurrence, including duplicates."""
    return "%s:%s:%s" % (dev, ino, start)


def _occurrence_parts(token):
    try:
        dev, ino, start = str(token).split(":", 2)
        return int(dev), int(ino), int(start)
    except (TypeError, ValueError):
        return None


def rotation_journal_path(room):
    return os.path.join(chat.chat_dir(), ".rotation.%s.json" % pk.slug(room))


def _rotation_retired_path(room):
    return os.path.join(chat.chat_dir(), ".rotation.%s.retired" % pk.slug(room))


def rotation_temp_path(room):
    """Fixed crash-reapable future room path; room serialization owns it."""
    return os.path.join(chat.chat_dir(), ".rotation.%s.next" % pk.slug(room))


def cleanup_rotation_temp(room):
    try:
        os.remove(rotation_temp_path(room))
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return _fsync_dir(chat.chat_dir())


def rotation_journal(room):
    """Return a valid active recovery map; absence and UNKNOWN stay distinct."""
    row = _strict_json(rotation_journal_path(room))
    if row is None:
        return None
    required = ("old_dev", "old_ino", "new_dev", "new_ino", "cut",
                "retained")
    if not isinstance(row, dict) or row.get("v") != 1 \
            or row.get("room") != pk.slug(room) \
            or any(key not in row for key in required) \
            or not isinstance(row.get("retained"), list) \
            or "cursors" in row and not isinstance(row["cursors"], dict) \
            or "receipts" in row and not isinstance(row["receipts"], list) \
            or "dropped" in row and not isinstance(row["dropped"], list) \
            or "temp" in row and (not isinstance(row["temp"], str)
                                  or os.path.basename(row["temp"]) != row["temp"]):
        raise OSError("rotation recovery journal is malformed")
    try:
        values = [int(row[key]) for key in required[:-1]]
        retained = [int(value) for value in row["retained"]]
    except (TypeError, ValueError) as exc:
        raise OSError("rotation recovery journal is malformed") from exc
    if any(value < 0 for value in values + retained):
        raise OSError("rotation recovery journal is malformed")
    return dict(row, **dict(zip(required[:-1], values)), retained=retained)


def prepare_rotation_journal(room, old_dev, old_ino, new_dev, new_ino, cut,
                             retained, cursors=None, receipts=None, dropped=None,
                             temp=None):
    """Durably publish the recovery map before any cursor/receipt mutation."""
    existing = rotation_journal(room)
    retired = _rotation_retired_path(room)
    if existing is None:
        try:
            os.remove(retired)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise OSError("retired rotation map cannot be cleared") from exc
        else:
            if not _fsync_dir(chat.chat_dir()):
                raise OSError("retired rotation map cleanup is not durable")
    elif os.path.exists(retired) or (
            existing["old_dev"], existing["old_ino"],
            existing["new_dev"], existing["new_ino"], existing["cut"]) != (
                int(old_dev), int(old_ino), int(new_dev), int(new_ino), int(cut)):
        raise OSError("active rotation recovery map conflicts")
    row = {"v": 1, "room": pk.slug(room), "old_dev": int(old_dev),
           "old_ino": int(old_ino), "new_dev": int(new_dev),
           "new_ino": int(new_ino), "cut": int(cut),
           "retained": [int(value) for value in retained]}
    if cursors:
        row["cursors"] = cursors
    if receipts:
        row["receipts"] = receipts
    if dropped:
        row["dropped"] = dropped
    if temp:
        row["temp"] = os.path.basename(temp)
    return _durable_json(rotation_journal_path(room), row)


def clear_rotation_journal(room):
    """Durably retire the active map while retaining its recovery evidence."""
    path, retired = rotation_journal_path(room), _rotation_retired_path(room)
    if not os.path.exists(path):
        return True
    if os.path.exists(retired):
        return False
    try:
        os.replace(path, retired)
    except OSError:
        return False
    return _fsync_dir(chat.chat_dir())


def normalize_rotated_cursor(room, row, journal=None, path=None):
    """Project either partially committed cursor generation onto the live room."""
    if not isinstance(row, dict):
        return row
    journal = journal or rotation_journal(room)
    if journal is None:
        return row
    try:
        st = os.stat(chat.room_path(room))
    except OSError:
        return row
    old = journal["old_dev"], journal["old_ino"]
    new = journal["new_dev"], journal["new_ino"]
    current = st.st_dev, st.st_ino
    source = row.get("dev"), row.get("ino")
    if source == current or source not in (old, new) or current not in (old, new):
        return row
    forward = source == old and current == new
    if not forward and path:
        saved = (journal.get("cursors") or {}).get(os.path.basename(path))
        if isinstance(saved, dict):
            return saved
    cut = journal["cut"]
    starts = journal["retained"]
    mapping = ({value: value - cut for value in starts} if forward else
               {value - cut: value for value in starts})
    source_id, target_id = (old, new) if forward else (new, old)
    out = dict(row, dev=target_id[0], ino=target_id[1])
    for field in ("off", "base"):
        value = int(out.get(field) or 0)
        out[field] = max(0, value - cut) if forward else value + cut
    for field in ("held", "done"):
        tokens = []
        for token in out.get(field) or ():
            parts = _occurrence_parts(token)
            if parts and parts[:2] == source_id and parts[2] in mapping:
                tokens.append(_occurrence(target_id[0], target_id[1],
                                          mapping[parts[2]]))
        if tokens:
            out[field] = sorted(set(tokens))
        else:
            out.pop(field, None)
    return out


def recover_room_rotation(room, room_locked=False):
    """Finish or roll back a crash-interrupted room generation by live inode."""
    try:
        journal = rotation_journal(room)
    except OSError:
        return False
    if journal is None:
        return True
    if not room_locked:
        with chat._room_lock(room) as locked:
            return locked and recover_room_rotation(room, room_locked=True)
    try:
        st = os.stat(chat.room_path(room))
    except OSError:
        return False
    old = journal["old_dev"], journal["old_ino"]
    new = journal["new_dev"], journal["new_ino"]
    current = st.st_dev, st.st_ino
    if current not in (old, new):
        return False
    saved = journal.get("cursors") or {}
    paths = [os.path.join(chat.chat_dir(), name) for name in saved]
    parsed = [item for path in paths
              for item in [parse_cursor_path(path)] if item is not None]
    with _cursor_topology_lock(), _cursor_room_locks({pk.slug(room)}), \
            _cursor_estate_locks({item["seat_key"] for item in parsed}), \
            _cursor_locks(paths, estate=False):
        updates = {}
        for path in paths:
            row = (saved.get(os.path.basename(path)) if current == old else
                   normalize_rotated_cursor(
                       room, pk.read_json(path, None), journal, path))
            if isinstance(row, dict):
                updates[path] = row
        committed = _commit_cursor_updates(updates)
        if not committed:
            return False
    from .seats_receipts import (finish_room_receipt_remap,
                                 prune_room_receipts,
                                 remap_room_receipts,
                                 rollback_room_receipt_remap)
    retained = [tuple(value) for value in journal.get("receipts") or ()]
    if current == old:
        if not rollback_room_receipt_remap(room, retained):
            return False
        if not cleanup_rotation_temp(room):
            return False
    else:
        remapped = remap_room_receipts(room, retained, resume=True)
        if not remapped or not finish_room_receipt_remap(room, retained) \
                or not prune_room_receipts(room, journal.get("dropped") or ()):
            return False
    return clear_rotation_journal(room)


def _cursor_state(state, active=False, base=0):
    dev, ino, off, rid = state
    return {"dev": dev, "ino": ino, "off": off, "rid": rid,
            "active": bool(active), "base": int(base or 0)}


def _write_cursor_path(path, state, active=False, base=0):
    pk.write_json(path, _cursor_state(state, active=active, base=base))


def _commit_cursor_updates(updates, finish=None):
    """Durably journal both images, commit cursors, then run ``finish``.

    The room-scoped journal is the seqlock and recovery map. Cursor after-images
    are durable before the committed outcome becomes visible; a destructive
    room install can therefore never precede recoverable cursor commit state.
    """
    paths = sorted({os.path.abspath(path) for path in updates},
                   key=_cursor_lock_key)
    if not paths:
        try:
            if finish is not None:
                finish()
            return CursorCommitResult(True)
        except (OSError, ValueError, TypeError):
            return CursorCommitResult(False)
    before = {}
    try:
        for path in paths:
            try:
                with open(path, encoding="utf-8") as f:
                    before[path] = f.read()
            except FileNotFoundError:
                before[path] = None
        journal_path, journal = _prepare_cursor_txn(paths, before, updates)
        for path in paths:
            _durable_text(path, journal["after"][os.path.basename(path)])
        _publish_cursor_outcome(journal_path, dict(journal, state="committed"))
    except (OSError, ValueError, TypeError, UnicodeError):
        complete = ("journal_path" in locals()
                    and _restore_cursor_txn(journal_path, journal))
        return CursorCommitResult(False, complete)
    if finish is not None:
        try:
            finish()
        except (OSError, ValueError, TypeError):
            try:
                _publish_cursor_outcome(
                    journal_path, dict(journal, state="prepared"))
            except (OSError, ValueError, TypeError):
                return CursorCommitResult(False, False)
            return CursorCommitResult(
                False, _restore_cursor_txn(journal_path, journal))
    return CursorCommitResult(True)


def restore_cursor_sweep(room, valid_ids):
    """Repair/mint every delivery+wake pair after journal room replacement."""
    from .seats_delivery import _baseline_state
    from .seats_roster import roster

    room_key = pk.slug(room)
    valid_ids = {value for value in valid_ids if value}
    with _cursor_topology_lock(), _cursor_room_locks({room_key}):
        try:
            names = os.listdir(chat.chat_dir())
        except OSError:
            return 0, 0
        parsed = [item for name in names
                  for item in [parse_cursor_path(
                      os.path.join(chat.chat_dir(), name))]
                  if item and item["room"] == room_key]
        seats = {_seat_key(seat) for seat in roster()
                 if isinstance(seat, str) and seat}
        seats.update(item["seat_key"] for item in parsed)
        with _cursor_estate_locks(seats):
            groups = {(item["seat_key"], item["session_key"])
                      for item in parsed}
            groups.update((seat_key, None) for seat_key in seats)
            paths = {path for seat_key, session_key in groups
                     for path in (_cursor_path_from_keys(
                         room_key, seat_key, session_key),
                         _cursor_path_from_keys(
                         room_key, seat_key, session_key, beacon=True))}
            with _cursor_locks(paths, estate=False):
                state = _baseline_state(room, at_start=False)
                updates, minted, repaired = {}, 0, 0
                for seat_key, session_key in groups:
                    pair = (_cursor_path_from_keys(room_key, seat_key, session_key),
                            _cursor_path_from_keys(
                                room_key, seat_key, session_key, beacon=True))
                    rows = [pk.read_json(path, None) for path in pair]
                    present = [isinstance(row, dict)
                               and isinstance(row.get("off"), int) for row in rows]
                    live = all(present) and all(
                        row.get("rid") in valid_ids for row in rows)
                    if live:
                        continue
                    active = any(bool(row.get("active")) for row in rows
                                 if isinstance(row, dict))
                    row = _cursor_state(state, active=active, base=state[2])
                    updates.update({path: dict(row) for path in pair})
                    if not any(present):
                        minted += 1
                    else:
                        repaired += 1
                if updates and not _commit_cursor_updates(updates):
                    return 0, 0
                return minted, repaired


def _cursor_path_index():
    """One directory snapshot keyed by seat-level delivery cursor path.

    ONE ROOT RESOLUTION FOR THE WHOLE WALK, and the walk is long: a live chat
    dir holds ~105k entries of which ~58k parse as cursors, so keying each one
    through a bare _cursor_path_from_keys resolved the root per cursor file —
    3.35s of a 4.27s build, ~63% of the warm _rooms_summary the web poll pays
    against a 3s TTL (task/2879). The root is in hand on line one and passing
    it down changes no path produced here."""
    root = chat.chat_dir()
    try:
        names = os.listdir(root)
    except OSError:
        return None
    out = {}
    for name in names:
        parsed = parse_cursor_path(os.path.join(root, name))
        if parsed is None:
            continue
        base = _cursor_path_from_keys(parsed["room"], parsed["seat_key"],
                                      root=root)
        out.setdefault(base, []).append(parsed["path"])
    return out


def room_active(room, seat, cursor_paths=None):
    """Whether any delivery or beacon cursor consumed traffic in a room."""
    base = cursor_path(room, seat)
    paths = (_cursor_paths(room, seat) if cursor_paths is None
             else cursor_paths.get(base, ()))
    for path in paths:
        cur = normalize_rotated_cursor(room, pk.read_json(path, None), path=path)
        if isinstance(cur, dict) and cur.get("active"):
            return True
    return False
