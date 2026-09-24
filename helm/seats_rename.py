#!/usr/bin/env python3
"""Crash-recoverable seat identity re-keying.

Rename is a multi-estate transaction: cursors, wake cursors, keyed sidecars,
private transcript, receipt roots, redirect, then roster publication. A durable
journal makes process death equivalent to an interrupted transaction rather
than an unowned mixture of both identities.
"""

import contextlib
import errno
import fcntl
import json
import os
import stat
import time

from . import chat, home, openflags, pk, seats_advice
from .seats_common import (RENAME_ALIAS_FIELD, _flocked, _seat_key, _seat_label,
                           live_alias, rename_aliases, roster_path)
from .seats_cursor import (_cursor_estate_locks, _cursor_locks,
                           _cursor_path_from_keys, _cursor_topology_lock,
                           _durable_json, _fsync_dir, parse_cursor_path,
                           recover_room_rotation)

_STATE_MARKERS = (
    ".cursor.", ".seen.", ".scan.", ".stopfp.", ".stopbeacon.",
    ".stopwhisper.", ".stopclaime.", ".stopwiring.", ".stoppunt.",
    ".stoplease.", ".stopndp.", ".stopseam.", ".stopseamshare.",
    ".stopspiral.",
)


def _key_bounded(name, key):
    """Match a seat key only at an entire state-filename field boundary."""
    for marker in _STATE_MARKERS:
        probe, start = marker + key, 0
        while True:
            start = name.find(probe, start)
            if start < 0:
                break
            end = start + len(probe)
            if end == len(name) or name[end] == ".":
                return True
            start += 1
    return False


def _bounded_sub(name, old_key, new_key):
    """Rename only whole dot-fields, never a room merely embedding the key."""
    if name.startswith(".beacon-"):
        return ".beacon-" + _bounded_sub(name[8:], old_key, new_key)
    old_dm, new_dm = chat.DM_PREFIX + old_key, chat.DM_PREFIX + new_key
    return ".".join(new_key if field == old_key else
                    new_dm if field == old_dm else field
                    for field in name.split("."))


def rename_journal_path():
    return os.path.join(chat.chat_dir(), ".seat-rename.json")


@contextlib.contextmanager
def _fd_lock(path, flags):
    fd = os.open(path, flags)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextlib.contextmanager
def _rename_scope():
    """Cross-process rename mutex without creating state on a read-only probe."""
    with _fd_lock(chat.chat_dir(), openflags.flags(
            os.O_RDONLY, "O_DIRECTORY", "O_NOFOLLOW")):
        yield


@contextlib.contextmanager
def _existing_roster_scope():
    """Wait for an already-started roster writer without minting its lock."""
    try:
        fd = os.open(roster_path() + ".lock", os.O_RDWR)
    except FileNotFoundError:
        yield
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _journal():
    try:
        with pk.open_regular(rename_journal_path(), encoding="utf-8") as f:
            row = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError) as exc:
        raise OSError("seat rename journal is unreadable") from exc
    required = ("old", "new", "old_lane", "new_lane", "moves",
                "receipt_ids")
    if not isinstance(row, dict) or any(key not in row for key in required) \
            or not isinstance(row["moves"], list) \
            or not isinstance(row["receipt_ids"], list):
        raise OSError("seat rename journal is malformed")
    return row


def _write_journal(row):
    return _durable_json(rename_journal_path(), row)


def _clear_journal():
    try:
        os.remove(rename_journal_path())
    except FileNotFoundError:
        return True
    except OSError:
        return False
    _fsync_dir(chat.chat_dir())
    return True


_INVALID_JOURNAL_NAMES = object()


def _journal_names(row):
    """The exact Actor labels reserved by a parseable pending journal.

    Current journals carry the labels directly because ``old``/``new`` are
    hashed state-file keys, not names an Actor can bind. Older v2 journals can
    recover the same pair from their actor replay payload, and legacy roster
    snapshots disclose the exact removed and added mapping keys. A genuinely
    key-only v1 journal returns ``None`` for compatibility. Any present but
    inconsistent label evidence returns the fail-closed sentinel.
    """
    keys = tuple(row.get(key) for key in ("old", "new"))
    if not all(isinstance(value, str) and value for value in keys):
        return _INVALID_JOURNAL_NAMES

    def valid(values):
        if not all(isinstance(value, str) and value for value in values):
            return False
        try:
            return tuple(_seat_key(value) for value in values) == keys
        except UnicodeError:
            return False

    evidence = []
    direct_present = any(key in row for key in
                         ("source_name", "target_name"))
    direct = tuple(row.get(key) for key in ("source_name", "target_name"))
    if direct_present:
        if not valid(direct):
            return _INVALID_JOURNAL_NAMES
        evidence.append(direct)

    expected_present = "actor_rename" in row or row.get("v", 1) == 2
    expected = row.get("actor_rename")
    actor = (tuple(expected.get(key) for key in
                   ("source_name", "target_name"))
             if isinstance(expected, dict) else (None, None))
    if expected_present:
        if not valid(actor):
            return _INVALID_JOURNAL_NAMES
        evidence.append(actor)

    roster_fields = ("roster_before", "roster_after")
    roster_present = tuple(key in row for key in roster_fields)
    before, after = tuple(row.get(key) for key in roster_fields)
    roster_absent = not any(roster_present) or \
        (all(roster_present) and before is None and after is None)
    if not roster_absent:
        if not all(roster_present) or not isinstance(before, dict) \
                or not isinstance(after, dict):
            return _INVALID_JOURNAL_NAMES
        removed = set(before) - set(after)
        added = set(after) - set(before)
        if len(removed) != 1 or len(added) != 1:
            return _INVALID_JOURNAL_NAMES
        roster = (next(iter(removed)), next(iter(added)))
        if not valid(roster):
            return _INVALID_JOURNAL_NAMES
        evidence.append(roster)

    folded = [tuple(value.casefold() for value in pair) for pair in evidence]
    if folded and any(pair != folded[0] for pair in folded[1:]):
        return _INVALID_JOURNAL_NAMES
    return evidence[0] if evidence else None


def _explicit_chat_symlink_refusal(name, exc):
    """Actionable diagnosis for the one secure-open failure we can prove.

    ``_rename_scope`` deliberately opens the selected chat root with
    O_DIRECTORY|O_NOFOLLOW. Linux reports a final symlink as ENOTDIR; other
    kernels may report ELOOP. Diagnose only those two shapes, only for an
    EXPLICIT chat selection, and only when lstat proves the selected path's
    final component is itself a symlink whose real target is a directory.

    This helper is diagnostic only: it never follows the link for admission,
    never retries the journal read against the target, and cannot replace the
    generic unreadable-journal recovery text when any proof is missing.
    """
    if getattr(exc, "errno", None) not in (errno.ENOTDIR, errno.ELOOP):
        return None
    try:
        path, origin = home.surface_origin(
            "CHAT_DIR", "helm-chat", chat.DEFAULT_DIR)
        if origin != home.EXPLICIT:
            return None
        selected = os.fspath(path)
        if not stat.S_ISLNK(os.lstat(selected).st_mode):
            return None
        # Check the link with the kernel: Python 3.9's strict resolve can
        # normalize a non-directory/../target into a false directory proof.
        if not stat.S_ISDIR(os.stat(selected).st_mode):
            return None
        target = os.path.realpath(selected)
    except Exception:                           # noqa: BLE001 — diagnosis only
        return None
    return (
        "cannot bind %r: explicit chat root %r is a symlink, and secure "
        "pending-rename admission refuses to follow it. Set HELM_CHAT_DIR=%r "
        "(its real directory target), then retry; this first admission remains "
        "refused" % (name, selected, target))


def pending_rename_admission_refusal(name):
    """Why a FIRST Actor admission is fenced by rename, or ``None``.

    The caller holds ``actors._store_lock``. Taking the rename-directory mutex
    after it preserves roster -> actor -> journal order and gives one stable
    journal snapshot. This helper NEVER recovers: recovery owns the roster and
    actor estates and cannot be entered from ``bind`` without relocking them.

    A readable journal fences only its exact source/target labels. If the
    journal cannot be read or cannot safely disclose those labels, every first
    admission refuses: availability is the unavoidable cost of not forking an
    identity whose reserved names are UNKNOWN. Existing canonical binds never
    call this helper.
    """
    try:
        with _rename_scope():
            row = _journal()
    except FileNotFoundError:
        return None
    except Exception as exc:                  # noqa: BLE001 — cannot-look is UNKNOWN
        symlink = _explicit_chat_symlink_refusal(name, exc)
        if symlink:
            return symlink
        return (
            "cannot bind %r: a pending seat rename journal is unreadable "
            "(%s), so its source and target are UNKNOWN and first admission "
            "is refused rather than forking identity. Run `helm chat seats` "
            "to finish rename recovery (or surface the journal repair), then "
            "retry" % (name, type(exc).__name__))
    if row is None:
        return None
    names = _journal_names(row)
    if names is _INVALID_JOURNAL_NAMES:
        return (
            "cannot bind %r: a pending seat rename journal does not safely "
            "identify its source and target, so first admission is refused "
            "rather than forking identity. Run `helm chat seats` to finish "
            "rename recovery (or surface the journal repair), then retry"
            % name)
    if names is None:
        keys = tuple(row[key] for key in ("old", "new"))
        matches = [label for label, value in zip(("source", "target"), keys)
                   if value == _seat_key(name)]
        if not matches:
            return None
        return (
            "cannot bind %r: a pending seat rename holds that %s name. Run "
            "`helm chat seats` to finish rename recovery, then retry"
            % (name, "/".join(matches)))
    cf = str(name).casefold()
    matches = [label for label, value in zip(("source", "target"), names)
               if value.casefold() == cf]
    if not matches:
        return None
    return (
        "cannot bind %r: pending seat rename %r -> %r holds that %s name. "
        "Run `helm chat seats` to finish rename recovery, then retry"
        % (name, names[0], names[1], "/".join(matches)))


def _rel(path):
    root = os.path.realpath(chat.chat_dir())
    real = os.path.realpath(path)
    if os.path.commonpath((root, real)) != root:
        raise OSError("rename path escapes chat state")
    return os.path.relpath(real, root)


def _path(rel):
    if os.path.isabs(rel) or ".." in rel.split(os.sep):
        raise OSError("unsafe rename journal path")
    return os.path.join(chat.chat_dir(), rel)


def _recover_rotations():
    try:
        names = os.listdir(chat.chat_dir())
    except FileNotFoundError:
        return True
    except OSError:
        return False
    for name in names:
        if not name.startswith(".rotation.") or not name.endswith(".json"):
            continue
        row = pk.read_json(os.path.join(chat.chat_dir(), name), None)
        room = row.get("room") if isinstance(row, dict) else None
        if not room or not recover_room_rotation(room):
            return False
    return True


def _roster_changed(row):
    before, after = row.get("roster_before"), row.get("roster_after")
    if not isinstance(after, dict) or not isinstance(before, dict):
        return None
    return {key for key in set(before) | set(after)
            if (key in before) != (key in after)
            or before.get(key) != after.get(key)}


def _roster_direction(row):
    changed = _roster_changed(row)
    if changed is None:
        return "rollback"
    current = pk.read_json(roster_path(), None)
    if not isinstance(current, dict):
        return None

    def matches(image):
        return all((key in current) == (key in image)
                   and current.get(key) == image.get(key) for key in changed)

    if matches(row["roster_after"]):
        return "forward"
    if matches(row["roster_before"]):
        return "rollback"
    return None


def _publish_roster_direction(row, forward):
    changed = _roster_changed(row)
    if changed is None and row.get("roster_before") is None \
            and row.get("roster_after") is None:
        return True
    current = pk.read_json(roster_path(), None)
    if changed is None or not isinstance(current, dict):
        return False
    image = row["roster_after" if forward else "roster_before"]
    for key in changed:
        if key in image:
            current[key] = image[key]
        else:
            current.pop(key, None)
    pk.write_json(roster_path(), current)
    return True


def _destination_redirect(row, forward):
    """A live destination has no redirect; rollback restores its prior route."""
    path = chat._dm_redirect_path(row["new_lane"])
    if forward:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError:
            return False
        return True
    prior = row.get("target_redirect")
    if prior is not None:
        pk.atomic_write(path, prior)
        return True
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        return False
    return True


def _move_paths(row, forward):
    moves = [(_path(item[0]), _path(item[1])) for item in row["moves"]]
    if not forward:
        moves = [(target, source) for source, target in reversed(moves)]
    for source, target in moves:
        source_exists, target_exists = os.path.exists(source), os.path.exists(target)
        if source_exists and not target_exists:
            os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
            os.replace(source, target)
        elif target_exists and not source_exists:
            continue
        else:
            return False
    return True


def _actor_replay(row, forward, apply):
    """Validate/apply the actor leg; only a v1 journal may omit it."""
    expected = row.get("actor_rename")
    if expected is None:
        return row.get("v", 1) == 1       # v2 missing/null is truncated, not legacy
    from . import actors
    ok, _note = actors.replay_rename(
        expected, forward=forward, apply=apply, _locked=True)
    return ok


def _recover_under_locks(row, direction):
    from .seats_receipts import (finish_room_receipts, resume_room_receipts,
                                 rollback_room_receipts)

    old_lane, new_lane = row["old_lane"], row["new_lane"]
    forward = direction == "forward"
    # Refuse drift BEFORE touching another estate. The actor directory lock is
    # held outside the rename/journal lock, so this proof remains true through
    # the state leg and the eventual actor write.
    if not _actor_replay(row, forward, apply=False):
        return False
    if old_lane == new_lane:              # case-only label change: roster+actor
        if row["moves"] or not _publish_roster_direction(row, forward):
            return False
    else:
        if not _destination_redirect(row, forward) \
                or not _move_paths(row, forward):
            return False
        redirect = chat._dm_redirect_path(old_lane)
        if forward:
            if resume_room_receipts(old_lane, new_lane,
                                    row["receipt_ids"]) is None:
                return False
            pk.atomic_write(redirect, new_lane)
            if not _publish_roster_direction(row, True):
                return False
            if not finish_room_receipts(old_lane, row["receipt_ids"]):
                return False
        else:
            if chat._dm_redirect(old_lane) == pk.slug(new_lane):
                try:
                    os.remove(redirect)
                except FileNotFoundError:
                    pass
                except OSError:
                    return False
            if not rollback_room_receipts(new_lane, row["receipt_ids"]):
                return False
            if not _publish_roster_direction(row, False):
                return False
    if forward and not _actor_replay(row, True, apply=True):
        return False
    return _clear_journal()


def _recover_journal_locked(row, direction=None):
    direction = direction or _roster_direction(row)
    if direction not in ("forward", "rollback"):
        return False
    old_lane, new_lane = row["old_lane"], row["new_lane"]
    cursor_paths = [_path(rel) for move in row["moves"] for rel in move
                    if parse_cursor_path(_path(rel))]
    with contextlib.ExitStack() as rooms:
        acquired = [rooms.enter_context(chat._room_lock(lane))
                    for lane in sorted({old_lane, new_lane})]
        if not all(acquired):
            return False
        with _cursor_topology_lock(), _cursor_estate_locks(
                {row["old"], row["new"]}), \
                _cursor_locks(cursor_paths, estate=False):
            return _recover_under_locks(row, direction)


def recover_seat_rename(roster_locked=False, actor_locked=False):
    """Finish/undo an interrupted rename according to the atomic roster file.

    Recovery takes the same roster -> actor-directory -> rename/journal order
    as the writer. A public rename already holding both outer locks says so and
    never reacquires the non-reentrant actor lock.
    """
    # Existing roster-lock + directory fds wait through both pre-journal writer
    # windows without creating lock files on a refused verb.
    probe_scope = (contextlib.nullcontext() if roster_locked
                   else _existing_roster_scope())
    try:
        with probe_scope, _rename_scope():
            if _journal() is None:
                return True
    except FileNotFoundError:
        return True
    except Exception:
        return False
    # Never hold the directory mutex while waiting for either outer lock:
    # rename writers already hold roster then actor, so inversion deadlocks.
    roster_scope = (contextlib.nullcontext() if roster_locked
                    else _flocked(roster_path() + ".lock"))
    from . import actors
    actor_scope = (contextlib.nullcontext() if actor_locked
                   else actors._store_lock())
    with roster_scope, actor_scope:
        try:
            with _rename_scope(), _flocked(rename_journal_path() + ".lock"):
                row = _journal()
                return True if row is None else _recover_journal_locked(row)
        except Exception:
            return False


def reclaim_seat_key(seat):
    """Retire a completed rename redirect before a roster admits this key anew.

    Caller holds the roster lock, so no rename can start between recovery,
    redirect removal, and the roster publication that follows.
    """
    if not recover_seat_rename(roster_locked=True):
        return False
    key = _seat_key(seat)
    lane = chat.DM_PREFIX + key
    with _rename_scope(), _flocked(rename_journal_path() + ".lock"), \
            chat._room_lock(lane) as locked, _cursor_topology_lock(), \
            _cursor_estate_locks({key}):
        if not locked or os.path.exists(rename_journal_path()):
            return False
        path = chat._dm_redirect_path(lane)
        try:
            os.remove(path)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return _fsync_dir(chat.chat_dir())


def _move_seat_state(old, new, publish=None, roster_before=None,
                     roster_after=None, actor_rename=None, _locks_held=False):
    """Move every keyed artifact; durable recovery owns partial completion.

    Production rename already owns roster+actor. Direct callers take those
    outer locks here so any journal replay still follows roster -> actor ->
    rename/journal and never acquires the actor lock from inside rename scope.
    """
    if not _locks_held:
        from . import actors
        with _flocked(roster_path() + ".lock"), actors._store_lock():
            if not recover_seat_rename(roster_locked=True, actor_locked=True):
                return False
            return _move_seat_state(
                old, new, publish=publish, roster_before=roster_before,
                roster_after=roster_after, actor_rename=actor_rename,
                _locks_held=True)
    if not _recover_rotations():
        return False
    try:
        chat._ensure_dir()
    except OSError:
        return False
    old_key, new_key = _seat_key(old), _seat_key(new)
    old_lane, new_lane = chat.DM_PREFIX + old_key, chat.DM_PREFIX + new_key
    with _rename_scope(), _flocked(rename_journal_path() + ".lock"):
        existing = _journal()
        if existing is not None and not _recover_journal_locked(existing):
            return False
        with contextlib.ExitStack() as rooms:
            acquired = [rooms.enter_context(chat._room_lock(lane))
                        for lane in sorted({old_lane, new_lane})]
            if not all(acquired):
                return False
            with _cursor_topology_lock(), _cursor_estate_locks(
                    {old_key, new_key}):
                root = chat.chat_dir()
                try:
                    names = os.listdir(root)
                except OSError:
                    return False
                same_lane = old_lane == new_lane
                parsed = [] if same_lane else [
                    item for name in names
                    for item in [parse_cursor_path(os.path.join(root, name))]
                    if item and item["seat_key"] == old_key]
                cursors = []
                for item in parsed:
                    room = new_lane if item["room"] == old_lane else item["room"]
                    cursors.append((item["path"], _cursor_path_from_keys(
                        room, new_key, item["session_key"], item["beacon"])))
                cursor_names = {os.path.basename(path)
                                for move in cursors for path in move}
                keyed = []
                if not same_lane:
                    for name in names:
                        cursorish = parse_cursor_path(os.path.join(
                            root, name.removesuffix(".lock"))) is not None
                        if name in cursor_names or cursorish \
                                or not _key_bounded(name, old_key):
                            continue
                        target = os.path.join(root, _bounded_sub(
                            name, old_key, new_key))
                        if target != os.path.join(root, name):
                            keyed.append((os.path.join(root, name), target))
                old_room = chat.room_path(old_lane)
                room_moves = ([(old_room, chat.room_path(new_lane))]
                              if not same_lane and os.path.exists(old_room) else [])
                moves = cursors + keyed + room_moves
                if any(os.path.exists(target) for source, target in moves
                       if source != target):
                    return False
                if same_lane:
                    ids, target_redirect = [], None
                else:
                    rows, _total, fault = chat.read_checked(old_lane)
                    if fault:
                        return False
                    ids = list(dict.fromkeys(str(row.get("id")) for row in rows
                                             if row.get("id")))
                    target_redirect = chat._dm_redirect(new_lane)
                journal = {"v": 2 if actor_rename is not None else 1,
                           "old": old_key, "new": new_key,
                           "source_name": str(old), "target_name": str(new),
                           "old_lane": old_lane, "new_lane": new_lane,
                           "moves": [[_rel(source), _rel(target)]
                                     for source, target in moves],
                           "receipt_ids": ids,
                           "target_redirect": target_redirect,
                           "roster_before": roster_before,
                           "roster_after": roster_after}
                if actor_rename is not None:
                    journal["actor_rename"] = actor_rename
                cursor_paths = [path for move in cursors for path in move]
                with _cursor_locks(cursor_paths, estate=False):
                    try:
                        _write_journal(journal)
                        if not same_lane:
                            from .seats_receipts import move_room_receipts
                            staged = move_room_receipts(old_lane, new_lane, ids)
                            if staged is None:
                                raise OSError("receipt staging failed")
                            if not _destination_redirect(journal, True):
                                raise OSError("destination redirect cleanup failed")
                            for source, target in moves:
                                os.makedirs(os.path.dirname(target), mode=0o700,
                                            exist_ok=True)
                                os.replace(source, target)
                            pk.atomic_write(chat._dm_redirect_path(old_lane),
                                            new_lane)
                        if publish is not None:
                            publish()
                    except Exception:
                        direction = _roster_direction(journal) or "rollback"
                        with contextlib.suppress(Exception):
                            _recover_under_locks(journal, direction)
                        return False
                from .seats_receipts import finish_room_receipts
                finished = (same_lane
                            or finish_room_receipts(old_lane, ids))
                if not _actor_replay(journal, True, apply=True):
                    return False
                if not finished:
                    return True
                _clear_journal()
                return True


def rename_plan(seat, new, sess, until, actor_note="", register_note=""):
    """The six surfaces a rename changes, as the dry-run prints them —
    every guard has already passed; this lists the chat dir to count the
    keyed state files that would move, and writes nothing.

    `register_note` is surface 6, the spawn register, and it is read from
    THE SAME PRODUCER THE APPLY PATH CALLS (`seat_lifecycle_runtime.
    rename_register_identity`, with apply=False), never from a second walk
    that could disagree with it. A caller that already holds that clause may
    pass it. A dry run that named five surfaces while apply changed six was a
    dry run lying about its own verb."""
    if not register_note:
        from . import seat as _facade  # noqa: F401 — facade contract: an impl import is accompanied by the facade in its own scope
        from .seat_lifecycle_runtime import rename_register_identity  # cycle
        register_note = rename_register_identity(seat, new)
    old_lbl = _seat_label(seat)
    keyed = 0
    try:
        for name in os.listdir(chat.chat_dir()):
            if _key_bounded(name, _seat_key(seat)):
                keyed += 1
    except OSError:
        keyed = None
    win = ("until %s" % until) if until else "NO alias window (--alias-hours 0)"
    lines = [
        "dry-run: would rename %s -> %s, carrying %d session%s (%s); nothing "
        "was written. Six surfaces would change:"
        % (old_lbl, new, len(sess), "s"[:len(sess) != 1],
           ", ".join("%.8s" % s for s in sess)),
        "  1 roster: row key %s -> %s; %s keyed state file%s (cursors, seen, "
        "stop state, DM lane) move; alias @%s %s"
        % (old_lbl, new, "?" if keyed is None else keyed,
           "" if keyed == 1 else "s", old_lbl, win),
        "  2 identity admission: a process still carrying HELM_CHAT_NAME=%s "
        "is admitted AS %s %s; after that it is a TAKEOVER dispute again"
        % (old_lbl, new, win),
        "  3 chat addressing: @%s mentions, replies to %s-authored rows and "
        "reactions on them deliver to %s %s; the DM lane dm/%s redirects to "
        "dm/%s" % (old_lbl, old_lbl, new, win, old_lbl, new),
        "  4 beacon: `helm chat wait --seat %s` from that process arms %s %s; "
        "a waiter still spelling %s in argv/env is attributed to %s"
        % (old_lbl, new, win, old_lbl, new),
        "  5 dispatch: a NEW row sent to %s lands on %s %s; rows ALREADY "
        "addressed to %s do not move (that is `helm seat reassign`)"
        % (old_lbl, new, win, old_lbl),
        "  6 spawn register: %s"
        % (register_note or "not consulted"),
        "  actor store: %s" % (actor_note or "not consulted"),
    ]
    return "\n".join(lines)


def alias_window_note(old_lbl, new, until):
    """The rename verb's sentence about the alias window, or "" without one."""
    if not until:
        return ""
    return (" Until %s the old name still answers: @%s mentions, replies, "
            "reactions, DMs and dispatches reach %s, and a process whose "
            "HELM_CHAT_NAME is still %s is admitted AS %s (re-export "
            "HELM_CHAT_NAME=%s before then)."
            % (until, old_lbl, new, old_lbl, new, new))


def rename_report(old_lbl, new, sess, window, actor_note, carried):
    """The rename verb's success sentence: what moved, where @old delivers
    now, the alias window, the re-arm and relaunch reminders, the actor
    store's answer and the carried-holdings census. Lives beside
    `rename_plan` and `alias_window_note` because it is the same kind of
    thing — prose about a rename — and seats_roster is at its budget."""
    return ("moved the WHOLE row %s -> %s, carrying %d session%s (%s). "
            % (old_lbl, new, len(sess), "s"[:len(sess) != 1],
               ", ".join("%.8s" % s for s in sess))
            + "@%s now delivers to it.%s If it armed a "          # old
            "beacon on the old name, re-arm: %s — if Monitor "       # name
            "is not in your surface it is DEFERRED: ToolSearch(query: "
            "\"select:Monitor\") first. Launch/resume follows this row's "
            "durable key lineage: an instance directory still named %s "
            "relaunches as %s rather than recreating the old roster key. "
            "actor store: %s"
            % (new, window, seats_advice.beacon_monitor(new), old_lbl, new,
               actor_note)
            + carried)


def alias_until(alias_hours):
    """(until_ts, err) — the alias window's end for a rename, None for no
    window (``alias_hours`` 0), or an error for a value that is not hours."""
    import math
    try:
        hours = float(alias_hours or 0)
    except (TypeError, ValueError):
        hours = -1.0
    if not math.isfinite(hours) or hours < 0:
        return None, "alias window must be a finite, non-negative number of hours"
    return (pk.epoch_ts(time.time() + hours * 3600) if hours else None), None


def rename_window(seat, new, rows, alias_hours, actor_locked=False):
    """(until, actor_note, actor_rename, err) — decide before any write.

    Apply holds the actor directory lock and captures the exact actor id,
    revision and canonical label that the journal may relabel. Dry-run keeps
    its side-effect-free unlocked preflight and needs no replay payload.
    """
    from . import actors
    until, err = alias_until(alias_hours)
    if err:
        return None, "", None, err
    if actor_locked:
        ok, note, reason, expected = actors.rename_expectation(seat, new)
    else:
        ok, note, reason = actors.rename_preflight(seat, new)
        expected = None
    if not ok:
        return None, "", None, ("refusing to rename %s -> %s: %s"
                                % (_seat_label(seat), new, note))
    return until, note, expected if reason == actors.RELABEL else None, None


def rename_record(row, old, until):
    """The `renamed` record for a row that was just re-keyed away from `old`:
    the new hop (None `until` records no window) plus every hop still open
    on the row, each under its ORIGINAL expiry — a second rename inside a
    window never silently cancels the first (task/2338)."""
    prior = [{"old": name, "until": pk.epoch_ts(when)}
             for name, when in rename_aliases(row)]
    if not until and not prior:
        return None
    return {"old": old, "at": pk.now_ts(), "until": until, "prior": prior}


def rename_register_apply(old, new):
    """Surface 6's WRITE, through the same door `rename_plan` READS.

    The roster drives the rename transaction but does not own the spawn
    register, and it may not take `.spawn.lock` inside the roster lock — the
    rebind sweep takes those two the other way round. Both legs live here so
    the register the dry run reports and the register the apply writes can
    never drift apart. `seat_lifecycle_runtime.rename_register_identity` owns
    the why, the fail-open contract, and the rule that an UNREADABLE register
    is not an ABSENT one.
    """
    from . import seat as _facade  # noqa: F401 — facade contract: an impl import is accompanied by the facade in its own scope
    from .seat_lifecycle_runtime import rename_register_identity
    return rename_register_identity(old, new, apply=True)
