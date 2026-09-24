#!/usr/bin/env python3
"""Bounded, best-effort delivery evidence for broadcast rows.

The broadcast row owns its historical recipient/session census. Each observed
hook/beacon effect is one create-only hashed file under that row's hashed
directory: no fleet-global lock, no read-before-append dedupe, and rotation or
DM GC can unlink the exact dropped row directory without scanning sidecars.
"""

import contextlib
import hashlib
import json
import os
import stat
import sys

from . import chat, eventledger, openflags, pk
from .seats_common import (ROOM_SCAN_CAP, SCAN_CAP, _BROADCAST, _flocked,
                           _scrub, _seat_key, _seat_label, recipient_matches)


_CENSUS_FIELD = "delivery_census"
_RECEIPT_SCAN_BYTES = 32 * SCAN_CAP
# Opening a lane has bounded metadata/lock cost even when its file is empty.
# Charge one filesystem accounting block against the same work budget as row
# bytes, so an estate of tiny lanes cannot evade the bound while 257 healthy
# lanes do not globally disable receipts merely because ROOM_SCAN_CAP is 16.
_RECEIPT_LANE_COST = 4096
_RECEIPT_ROW_CAP = 65536


class ReceiptRemapResult:
    __slots__ = ("committed", "rollback_complete")

    def __init__(self, committed, rollback_complete=True):
        self.committed = bool(committed)
        self.rollback_complete = bool(rollback_complete)

    def __bool__(self):
        return self.committed


def _canonical_room(room):
    return pk.slug(str(room or "main"))


def _receipt_room(room):
    """Follow a completed DM rename so a late effect lands in the live lane."""
    room = _canonical_room(room)
    seen = set()
    for _hop in range(ROOM_SCAN_CAP):
        if room in seen or not room.startswith(chat.DM_PREFIX):
            break
        seen.add(room)
        moved = chat._dm_redirect(room)
        if not moved:
            break
        room = _canonical_room(moved)
    return room


def _hash(*parts):
    raw = "\0".join(str(part) for part in parts).encode("utf-8", "surrogatepass")
    return hashlib.sha256(raw).hexdigest()


def _receipt_dir():
    return os.path.join(chat.chat_dir(), ".delivery-receipts")


def _receipt_path(room, row_id):
    """Fixed-alphabet directory for one canonical room+row identity."""
    return os.path.join(_receipt_dir(), _hash(_canonical_room(room), row_id))


def _occurrence_dir(room, row_id, occurrence):
    return os.path.join(_receipt_path(room, row_id), _hash(occurrence))


def _effect_path(room, row_id, occurrence, recipient, effect):
    return os.path.join(_occurrence_dir(room, row_id, occurrence),
                        _hash(recipient, effect) + ".json")


def _recipient_id(seat, session, incarnation=None):
    return _hash(_seat_key(seat), session or "sessionless",
                 incarnation or "legacy")


def broadcast_census(row, room):
    """Freeze exact recipients at append time; later roster churn cannot add any."""
    from .seats_identity import deliverable, seat_scope
    from .seats_roster import roster_checked

    text = row.get("text") if isinstance(row, dict) else None
    if not isinstance(text, str) or not _BROADCAST.search(text):
        return None
    rows, failed = roster_checked()
    recipients = []
    if not failed:
        for seat, roster_row in sorted(rows.items()):
            if not isinstance(roster_row, dict):
                continue
            scope = seat_scope(seat, rows)
            if not deliverable(row, seat, room, scope=scope, ambient=False,
                               beacon=False):
                continue
            sessions = [session for session in
                        [roster_row.get("session")] +
                        list(roster_row.get("sessions") or []) if session]
            sessions = [None] + list(dict.fromkeys(sessions))
            incarnation = roster_row.get("incarnation")
            for session in sessions:
                recipients.append({
                    "recipient": _recipient_id(seat, session, incarnation),
                    "seat": str(seat),
                    "seat_key": _seat_key(seat),
                    "incarnation": incarnation,
                    "session": str(session) if session else None,
                    "muted": _canonical_room(room) in scope["mute"],
                    "wake_eligible": deliverable(
                        row, seat, room, scope=scope, ambient=False, beacon=True),
                })
    return {"v": 1, "room": _canonical_room(room), "unknown": bool(failed),
            "recipients": recipients}


def _row_recipient(row, seat, session):
    census = row.get(_CENSUS_FIELD) if isinstance(row, dict) else None
    recipients = census.get("recipients") if isinstance(census, dict) else ()
    keys = {_seat_key(seat)}
    if not session:
        from .seats_common import roster
        current = next((value for name, value in roster().items()
                        if recipient_matches(name, seat)
                        and isinstance(value, dict)), {})
        keys.update(str(value) for value in current.get("seat_keys") or ())
    for recipient in recipients or ():
        if not isinstance(recipient, dict):
            continue
        if session and recipient.get("session") == str(session):
            return recipient.get("recipient"), recipient.get("incarnation")
        if not session and recipient.get("session") is None \
                and recipient.get("seat_key") in keys:
            return recipient.get("recipient"), recipient.get("incarnation")
    from .seats_cursor import seat_incarnation
    incarnation = seat_incarnation(seat, session=session)
    return _recipient_id(seat, session, incarnation), incarnation


def _occurrence_present(room, row_id, occurrence):
    if occurrence == "legacy":
        return None
    try:
        dev, ino, start = (int(value) for value in occurrence.split(":", 2))
        with open(chat.room_path(room), "rb") as f:
            st = os.fstat(f.fileno())
            if (st.st_dev, st.st_ino) != (dev, ino) or start >= st.st_size:
                return False
            f.seek(start)
            raw = f.readline(SCAN_CAP + 1)
        row = json.loads(raw.decode("utf-8"))
        return isinstance(row, dict) and str(row.get("id") or "") == str(row_id)
    except FileNotFoundError:
        return False
    except (OSError, UnicodeError, ValueError):
        return None


def record_delivery_receipt(row, room, seat, session, channel,
                            occurrence=None):
    """Create one effect marker after output returns; never block delivery."""
    if channel not in ("hook", "beacon") or not isinstance(row, dict):
        return
    row_id, text = row.get("id"), row.get("text")
    if not row_id or not isinstance(text, str) or not _BROADCAST.search(text):
        return
    room = _receipt_room(room)
    effect = "hook-delivered" if channel == "hook" else "wake-attempted"
    recipient, incarnation = _row_recipient(row, seat, session)
    occurrence = str(occurrence or "legacy")
    rec = {
        "id": _hash(occurrence, recipient, effect),
        "row": str(row_id), "room": room, "occurrence": occurrence,
        "recipient": recipient,
        "seat": str(seat), "seat_key": _seat_key(seat),
        "incarnation": incarnation,
        "session": str(session) if session else None, "ts": pk.now_ts(),
        "effect": effect, "delivered": channel == "hook",
        "wake_attempted": channel == "beacon",
        "wake_succeeded": None, "turn_executed": None,
    }
    fds, fd, name = [], None, _hash(recipient, effect) + ".json"
    created = False
    try:
        root = _root_fd(create=True)
        fds.append(root)
        rowfd = _mkdir_child(root, os.path.basename(_receipt_path(room, row_id)))
        fds.append(rowfd)
        groupfd = _mkdir_child(rowfd, _hash(occurrence))
        fds.append(groupfd)
        flags = eventledger._flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        fd = os.open(name, flags, 0o600, dir_fd=groupfd)
        created = True
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise OSError("receipt marker is not private regular storage")
        payload = (json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
                   + "\n").encode("utf-8")
        if os.write(fd, payload) != len(payload):
            raise OSError("short receipt write")
        os.fsync(fd)
        os.close(fd)
        fd = None
        if _occurrence_present(room, row_id, occurrence) is False:
            os.unlink(name, dir_fd=groupfd)
            _clear_occurrence(rowfd, _hash(occurrence))
    except FileExistsError:
        pass
    except BaseException as exc:
        # Dispatcher cancellation must clean the same owned partial marker as
        # an ordinary write failure, then escape to its timeout boundary.
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if created or fd is not None:
            try:
                os.unlink(name, dir_fd=groupfd)
            except OSError:
                pass
        if not isinstance(exc, Exception):
            raise
        try:
            sys.stderr.write("[helm delivery receipt] receipt persistence failed; delivery UNKNOWN\n")
        except Exception:
            pass                        # closed stderr must not block delivery
    finally:
        for opened in reversed(fds):
            try:
                os.close(opened)
            except OSError:
                pass


def _read_receipts(row_id, room, occurrence=None):
    try:
        root = _root_fd()
        rowfd = _child_dir(root, os.path.basename(_receipt_path(room, row_id)))
    except FileNotFoundError:
        if "root" in locals():
            os.close(root)
        return [], None
    except OSError as exc:
        if "root" in locals():
            os.close(root)
        return [], type(exc).__name__
    out, fault = [], None
    wanted = _hash(occurrence) if occurrence is not None else None
    try:
        for group in sorted(os.listdir(rowfd)):
            if wanted is not None and group != wanted:
                continue
            try:
                groupfd = _child_dir(rowfd, group)
            except OSError as exc:
                fault = type(exc).__name__
                continue
            try:
                for name in sorted(os.listdir(groupfd)):
                    if not name.endswith(".json"):
                        continue
                    fd = None
                    try:
                        fd = os.open(name, eventledger._flags(os.O_RDONLY),
                                     dir_fd=groupfd)
                        st = os.fstat(fd)
                        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                            raise OSError("unsafe receipt marker")
                        raw = os.read(fd, eventledger.MAX_EVENT_BYTES + 1)
                        rows, reason = eventledger.checked_rows(raw, strict=True)
                        if reason or len(rows) != 1:
                            fault = reason or "malformed receipt marker"
                            continue
                        rec = rows[0]
                        if rec.get("row") != str(row_id) \
                                or rec.get("room") != _canonical_room(room):
                            fault = "receipt marker identity mismatch"
                            continue
                        out.append(rec)
                    except (OSError, ValueError) as exc:
                        fault = type(exc).__name__
                    finally:
                        if fd is not None:
                            os.close(fd)
            finally:
                os.close(groupfd)
    finally:
        os.close(rowfd)
        os.close(root)
    return out, fault


def _visible_receipts(receipts, row_id, room):
    """Ignore a staged generation until its physical row is the live room row."""
    return [rec for rec in receipts if _occurrence_present(
        room, row_id, str(rec.get("occurrence") or "legacy")) is not False]


def delivery_receipts(row_id, room="main"):
    """Valid live receipts; malformed/torn or staged markers never become NO."""
    return _visible_receipts(_read_receipts(row_id, room)[0], row_id, room)


def _dir_flags():
    return eventledger._flags(openflags.flags(os.O_RDONLY, "O_DIRECTORY"))


def _root_fd(create=False):
    root = _receipt_dir()
    if create:
        eventledger._prepare(os.path.join(root, ".probe"), create=True)
    return os.open(root, _dir_flags())


def _child_dir(parent, name):
    return os.open(name, _dir_flags(), dir_fd=parent)


def _mkdir_child(parent, name):
    try:
        os.mkdir(name, 0o700, dir_fd=parent)
    except FileExistsError:
        pass
    return _child_dir(parent, name)


def _clear_occurrence(rowfd, group):
    try:
        fd = _child_dir(rowfd, group)
    except FileNotFoundError:
        return True
    except OSError:
        try:
            os.unlink(group, dir_fd=rowfd)
            return True
        except OSError:
            return False
    complete = True
    try:
        for name in os.listdir(fd):
            try:
                os.unlink(name, dir_fd=fd)
            except OSError:
                complete = False
    except OSError:
        complete = False
    finally:
        os.close(fd)
    try:
        os.rmdir(group, dir_fd=rowfd)
    except FileNotFoundError:
        pass
    except OSError:
        complete = False
    return complete


def _prune_row_receipts(room, row_id):
    try:
        root = _root_fd()
        row_name = os.path.basename(_receipt_path(room, row_id))
        rowfd = _child_dir(root, row_name)
    except FileNotFoundError:
        if "root" in locals():
            os.close(root)
        return True
    except OSError:
        if "root" in locals():
            os.close(root)
        return False
    complete = True
    try:
        try:
            groups = os.listdir(rowfd)
        except OSError:
            groups, complete = (), False
        for group in groups:
            complete = _clear_occurrence(rowfd, group) and complete
    finally:
        os.close(rowfd)
    try:
        os.rmdir(row_name, dir_fd=root)
    except FileNotFoundError:
        pass
    except OSError:
        complete = False
    os.close(root)
    return complete


def _prune_occurrence(room, row_id, occurrence):
    try:
        root = _root_fd()
        row_name = os.path.basename(_receipt_path(room, row_id))
        rowfd = _child_dir(root, row_name)
    except FileNotFoundError:
        if "root" in locals():
            os.close(root)
        return True
    except OSError:
        if "root" in locals():
            os.close(root)
        return False
    complete = _clear_occurrence(rowfd, _hash(occurrence))
    os.close(rowfd)
    try:
        os.rmdir(row_name, dir_fd=root)
    except FileNotFoundError:
        pass
    except OSError:
        if os.path.exists(_occurrence_dir(room, row_id, occurrence)):
            complete = False
    os.close(root)
    return complete


def prune_room_receipts(room, dropped):
    """Unlink exact dropped occurrences; report whether every unlink completed."""
    complete = True
    for value in dropped:
        if isinstance(value, (tuple, list)) and len(value) == 2:
            complete = _prune_occurrence(room, value[0], value[1]) and complete
        elif value:
            complete = _prune_row_receipts(room, value) and complete
    return complete


def _store_moved_receipt(rec, room, row_id):
    """Create one copied marker without consulting the not-yet-moved transcript."""
    root = _root_fd(create=True)
    rowfd = groupfd = fd = None
    try:
        rowfd = _mkdir_child(root, os.path.basename(_receipt_path(room, row_id)))
        groupfd = _mkdir_child(rowfd, _hash(rec.get("occurrence") or "legacy"))
        name = _hash(rec.get("recipient"), rec.get("effect")) + ".json"
        fd = os.open(name, eventledger._flags(
            os.O_WRONLY | os.O_CREAT | os.O_EXCL), 0o600, dir_fd=groupfd)
        moved = dict(rec, room=_canonical_room(room))
        payload = (json.dumps(moved, ensure_ascii=False, separators=(",", ":"))
                   + "\n").encode("utf-8")
        if os.write(fd, payload) != len(payload):
            raise OSError("short receipt move write")
        os.fsync(fd)
    finally:
        for opened in (fd, groupfd, rowfd, root):
            if opened is not None:
                os.close(opened)


def move_room_receipts(old_room, new_room, row_ids):
    """Stage rewritten DM roots at the new identity; keep old roots for rollback."""
    if _canonical_room(old_room) == _canonical_room(new_room):
        return []
    ids = list(dict.fromkeys(str(value) for value in row_ids if value))
    created = []
    try:
        for row_id in ids:
            if os.path.exists(_receipt_path(new_room, row_id)):
                raise OSError("target receipt root already exists")
            receipts, fault = _read_receipts(row_id, old_room)
            if fault:
                raise OSError("source receipt storage is malformed")
            if not receipts:
                continue
            created.append(row_id)
            for rec in receipts:
                _store_moved_receipt(rec, new_room, row_id)
        return created
    except Exception:
        prune_room_receipts(new_room, created)
        return None


def resume_room_receipts(old_room, new_room, row_ids):
    """Union surviving source markers into authoritative staged destination."""
    if _canonical_room(old_room) == _canonical_room(new_room):
        return []
    ids = list(dict.fromkeys(str(value) for value in row_ids if value))
    present = []
    try:
        for row_id in ids:
            source, source_fault = _read_receipts(row_id, old_room)
            target, target_fault = _read_receipts(row_id, new_room)
            if source_fault or target_fault:
                raise OSError("rename receipt storage is malformed")
            if source or target:
                present.append(row_id)
            for rec in source:
                try:
                    _store_moved_receipt(rec, new_room, row_id)
                except FileExistsError:
                    pass
        return present
    except Exception:
        # Never prune target here: after roster publication it may be the only
        # complete copy while source cleanup is partial.
        return None


def finish_room_receipts(old_room, row_ids):
    """Retire source roots after the transcript, cursors, and roster committed."""
    return prune_room_receipts(old_room, row_ids)


def rollback_room_receipts(new_room, row_ids):
    """Drop staged target roots; source roots were never mutated."""
    return prune_room_receipts(new_room, row_ids)


def remap_room_receipts(room, retained, resume=False):
    """Stage new occurrences; recovery unions into the installed generation."""
    staged = []
    try:
        for row_id, old_occurrence, new_occurrence in retained:
            source, fault = _read_receipts(
                row_id, room, occurrence=old_occurrence)
            if fault:
                raise OSError("source receipt occurrence is malformed")
            if not resume:
                if not _prune_occurrence(room, row_id, new_occurrence):
                    raise OSError("stale receipt occurrence is not removable")
                staged.append((row_id, old_occurrence, new_occurrence))
            for rec in source:
                moved = dict(rec, occurrence=str(new_occurrence))
                try:
                    _store_moved_receipt(moved, room, row_id)
                except FileExistsError:
                    pass
        return ReceiptRemapResult(True)
    except Exception:
        complete = False if resume else rollback_room_receipt_remap(room, staged)
        return ReceiptRemapResult(False, complete)


def rollback_room_receipt_remap(room, retained):
    complete = True
    for row_id, _old_occurrence, new_occurrence in retained:
        complete = _prune_occurrence(room, row_id, new_occurrence) and complete
    return complete


def finish_room_receipt_remap(room, retained):
    complete = True
    for row_id, old_occurrence, _new_occurrence in retained:
        complete = _prune_occurrence(room, row_id, old_occurrence) and complete
    return complete


def _current_seat(rows, frozen):
    session = frozen.get("session")
    if session:
        for seat, row in rows.items():
            if not isinstance(row, dict):
                continue
            sessions = [row.get("session")] + list(row.get("sessions") or [])
            if session in sessions:
                return seat, row
    for seat, row in rows.items():
        keys = {_seat_key(seat)}
        if isinstance(row, dict):
            keys.update(str(value) for value in row.get("seat_keys") or ())
        if frozen.get("seat_key") in keys:
            return seat, row if isinstance(row, dict) else {}
    return frozen.get("seat"), {}


def _broadcast_recipients(row, room):
    """Historical recipient set plus current exact-session delivery/sink facts."""
    from . import beacons, proxywatch
    from .seats_roster import roster_checked
    from .seats_runtime import runtime_for_session

    census = row.get(_CENSUS_FIELD) if isinstance(row, dict) else None
    if not isinstance(census, dict) or census.get("unknown"):
        return None
    frozen = [item for item in census.get("recipients") or ()
              if isinstance(item, dict)]
    rows, failed = roster_checked()
    names, current = [], []
    for recipient in frozen:
        seat, roster_row = _current_seat({} if failed else rows, recipient)
        current.append((recipient, seat, roster_row))
        if seat and not failed:
            names.append(seat)
    try:
        if failed:
            raise OSError("current roster unavailable")
        wake_rows = beacons.census(seats=list(dict.fromkeys(names))).get("seats", [])
        wakes = {str(item.get("seat")).casefold(): item for item in wake_rows}
        live = beacons.live_sessions()
        records = beacons.holder_records()
    except Exception:
        wakes, live, records = {}, None, None
    out = []
    for recipient, seat, roster_row in current:
        session = recipient.get("session")
        runtime, verified = runtime_for_session(roster_row, session)
        try:
            if failed:
                raise OSError("current roster unavailable")
            pause, _err = proxywatch.delivery_pause(
                seat, runtime=runtime, runtime_verified=verified)
        except Exception:
            pause = {"state": "UNKNOWN"}
        wake = wakes.get(str(seat).casefold()) if not failed else None
        exact = [item for item in (wake or {}).get("live", [])
                 if item.get("session") == session] if session else []
        if failed:
            sink = "UNKNOWN"
        elif exact:
            sink = "LIVE"
        elif session and (wake or {}).get("verdict") == beacons.DEAF:
            sink = "NONE"
        elif session and (wake or {}).get("verdict") == beacons.VACANT:
            sink = "VACANT"
        elif session:
            state, _why = beacons.session_state(
                session, live=live, records=records)
            sink = "NONE" if state in ("live", "dead") else "UNKNOWN"
        else:
            verdict = (wake or {}).get("verdict")
            # DEAF-IN-EFFECT IS A LIVE SINK. It REFINES covered and is only
            # reachable with a proven live beacon, so the row physically
            # arrives; what it does not prove is that a turn consumed it —
            # and that is `wake_succeeded`/`turn_executed`, which stay UNKNOWN
            # on their own. Mapping it to UNKNOWN here threw away a physically
            # proven fact to avoid claiming a different one.
            sink = ({beacons.COVERED: "LIVE", beacons.DEAF: "NONE",
                     beacons.DEAF_IN_EFFECT: "LIVE",
                     beacons.VACANT: "VACANT"}.get(verdict, "UNKNOWN"))
        out.append(dict(recipient, current_seat=seat,
                        delivery_paused=bool(pause),
                        delivery_state=(pause or {}).get("state"), sink=sink))
    return out


def _yn(value):
    return "YES" if value is True else "NO" if value is False else "UNKNOWN"


def receipt_cli(args):
    if len(args) == 1:
        return render_delivery_receipts(args[0])
    print("usage: helm chat receipts <broadcast-id>[@occurrence]", file=sys.stderr)
    return 2


def _locate_receipt_row(target):
    """Freeze DM rename topology across the bounded physical lane census."""
    from .seats_rename import rename_journal_path
    with _flocked(rename_journal_path() + ".lock"):
        return _locate_receipt_row_locked(target)


def _locate_receipt_row_locked(target):
    from .seats_ack import _all_lanes

    raw = str(target or "").strip()
    tid, mark, ordinal = raw.rpartition("@")
    if not mark or not ordinal.isdigit():
        tid, ordinal = raw, None
    lanes, faults = _all_lanes()
    lanes = sorted(dict.fromkeys(lanes))
    hits, scanned, row_count = [], 0, 0
    for room in lanes:
        if _RECEIPT_LANE_COST > _RECEIPT_SCAN_BYTES - scanned:
            faults.append("lane census exceeds bounded work budget")
            break
        scanned += _RECEIPT_LANE_COST
        data = None
        try:
            with chat._room_lock(room) as locked:
                if not locked:
                    raise OSError("room lock unavailable")
                with open(chat.room_path(room), "rb") as f:
                    st = os.fstat(f.fileno())
                    if st.st_size > _RECEIPT_SCAN_BYTES - scanned:
                        faults.append(room)
                    else:
                        data = f.read(st.st_size)
            if data is None:
                break
            scanned += len(data)
            if data and not data.endswith(b"\n"):
                faults.append(room)
                continue
            start = 0
            for line in data.splitlines(keepends=True):
                row_count += 1
                if row_count > _RECEIPT_ROW_CAP or len(line) > SCAN_CAP:
                    faults.append(room)
                    break
                try:
                    row = json.loads(line.decode("utf-8"))
                except (UnicodeError, ValueError):
                    faults.append(room)
                    break
                rid = str(row.get("id") or "") if isinstance(row, dict) else ""
                if rid and (rid == tid or (len(tid) >= 4 and rid.startswith(tid))):
                    hits.append((row, room, _occurrence_token(
                        st.st_dev, st.st_ino, start), rid == tid))
                start += len(line)
        except FileNotFoundError:
            continue
        except OSError:
            faults.append(room)
    if faults:
        return None, None, None, (
            "cannot prove a unique receipt occurrence: one or more lanes are "
            "unreadable, malformed, or exceed the bounded census")
    exact = [hit for hit in hits if hit[3]]
    if ordinal is not None and not exact:
        return None, None, None, (
            "occurrence selectors require the exact full message id; %r is "
            "only a prefix" % tid)
    hits = exact or hits
    if not hits:
        return None, None, None, "no message matches id %r" % tid
    if ordinal is None and len(hits) != 1:
        return None, None, None, ("id %r has %d physical occurrences — use "
                                  "%s@1 through @%d" %
                                  (tid, len(hits), tid, len(hits)))
    index = int(ordinal or "1") - 1
    if not 0 <= index < len(hits):
        return None, None, None, "occurrence @%s is out of range" % ordinal
    row, room, occurrence, _exact = hits[index]
    return row, room, occurrence, None


def _occurrence_token(dev, ino, start):
    return "%s:%s:%s" % (dev, ino, start)


def render_delivery_receipts(target_id):
    """Render one room-locked physical census and its best-effort effects."""
    row = room = occurrence = None
    receipts, receipt_fault, err = [], None, None
    for _attempt in range(3):
        row, room, occurrence, err = _locate_receipt_row(target_id)
        if err:
            break
        try:
            with chat._room_lock(room) as locked:
                if not locked:
                    raise OSError("room lock unavailable")
                present = _occurrence_present(
                    room, str(row.get("id") or ""), occurrence)
                if present is False:
                    continue
                if present is None:
                    raise OSError("physical occurrence is unreadable")
                rid = str(row.get("id") or "")
                receipts, receipt_fault = _read_receipts(
                    rid, room, occurrence=occurrence)
            break
        except OSError:
            err = "cannot lock and verify the selected physical occurrence"
            break
    else:
        err = "the selected occurrence changed during bounded receipt lookup"
    if err:
        print("helm chat receipts: " + _scrub(err), file=sys.stderr)
        return 1
    text = row.get("text") if isinstance(row, dict) else None
    shown_target = _scrub(str(row.get("id") or target_id))[:8]
    if not isinstance(text, str) or not _BROADCAST.search(text):
        print("helm chat receipts: %s is not an exact-token @all/@fleet/@everyone "
              "broadcast" % shown_target, file=sys.stderr)
        return 1
    rid = str(row.get("id") or "")
    recipients = _broadcast_recipients(row, room)
    shown_room = _scrub(_canonical_room(room))
    if recipients is None:
        print("helm chat receipts %s [#%s] — historical recipient census UNKNOWN; "
              "%d observed effect%s" % (shown_target, shown_room, len(receipts),
                                         "s"[:len(receipts) != 1]))
        return 0
    print("helm chat receipts %s [#%s] — %d historical recipient session%s, "
          "%d observed effect%s" % (
              shown_target, shown_room, len(recipients),
              "s"[:len(recipients) != 1], len(receipts),
              "s"[:len(receipts) != 1]))
    for recipient in recipients:
        matched = [item for item in receipts
                   if item.get("recipient") == recipient.get("recipient")]
        if not matched and recipient.get("session"):
            matched = [item for item in receipts
                       if item.get("session") == recipient.get("session")]
        effects = {item.get("effect") for item in matched}
        hook = True if "hook-delivered" in effects else None
        attempted = True if "wake-attempted" in effects else None
        sid = _scrub(str(recipient.get("session") or "?"))[:8]
        delivery = ("PAUSED(%s)" % _scrub(
            str(recipient.get("delivery_state") or "UNKNOWN"))
                    if recipient.get("delivery_paused") else "ACTIVE")
        print("  @%s sid=%s delivery=%s muted=%s wake=%s sink=%s "
              "effects=%s hook_delivered=%s wake_attempted=%s "
              "wake_succeeded=UNKNOWN turn_executed=UNKNOWN" % (
                  _seat_label(recipient.get("current_seat")), sid, delivery,
                  "YES" if recipient.get("muted") else "NO",
                  "ELIGIBLE" if recipient.get("wake_eligible") else "INELIGIBLE",
                  recipient.get("sink") or "UNKNOWN",
                  ",".join(sorted(effects)) if effects else "UNKNOWN",
                  _yn(hook), _yn(attempted)))
    if not recipients:
        print("  no recipient session was in this broadcast's room scope when "
              "the row landed")
    if receipt_fault:
        print("  receipt storage is malformed/unreadable; unobserved effects are "
              "UNKNOWN")
    else:
        print("  receipt writes are best-effort; unobserved effects are UNKNOWN")
    return 0
