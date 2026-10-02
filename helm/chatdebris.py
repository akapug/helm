#!/usr/bin/env python3
"""The chat directory holds rooms; this module keeps it from holding debris.

WHY IT EXISTS. `chat.list_rooms()` is one getdents over the FLAT chat
directory, and the PostToolUse delivery hook asked it on every tool call of
every seat (seats_delivery.deliver_any -> seats_roomscan._scan_rooms). The
cost is set by EVERY entry in the directory, not by the rooms it returns.
Measured on the live bus: 108,177 entries for 468 rooms, one
listing 0.28s warm, delivery p50 630ms against a 2s budget, and the top
timeout site the listing line itself. Since task/3848 a delivery pass reads
the listing the last pass made (`seats_roomscan.room_names`) and lists only
when a room is created or that listing is 30 s old; every listing that does
run still pays for every entry, which is why the directory is kept small.

WHAT THE ENTRIES WERE, AND WHICH PRODUCER MADE EACH.
  * 49,718 `<cursor>.lock` siblings. `_cursor_locks` opened one per cursor
    until d047d93d5eb, and an flock handle cannot be unlinked at release, so
    every one ever minted was still there. Nothing has opened one since.
  * 53,830 cursors: a delivery+wake pair per consumer per LISTED room. Three
    producers mint them and all three are keyed on the room listing -- the
    join baselines every listed room for the new session and for its seat
    (seats_join._baseline_rooms), the delivery rotation mints a pair on the
    first scan of any listed room that lacks one (seats_delivery._room_dirty
    treats a missing pair as dirty, and deliver -> _init_cursor mints it), and
    a journal restore mints a seat-level pair for EVERY rostered seat in EVERY
    journaled room after resurrecting each one (seats_cursor.
    restore_cursor_sweep).
  * The pair is NOT deferrable to the first read: it is the admission line. A
    tracked seat reads a cursor-less room from offset 0, and the stop guard
    replays one from 0, so a room joined without a baseline would deliver its
    whole backlog as news. What was not needed was the ROOM. 430 of the 468
    were meld rooms idle for over a week, listed only because nothing ever
    took a finished meld off the bus -- and the restore put every one back
    after each reboot, with a fresh pair for every rostered seat.

SO THE DIRECTORY IS KEPT SMALL RATHER THAN INDEXED. An index of rooms is a
second answer to "which rooms exist", and a room is born the first time
anything posts to it, from several writers; any writer that missed the index
would make a room invisible to delivery, the stale-surface class list_rooms'
own docstring records helm paying for twice. Moving cursors to a subdirectory
touches every cursor listing on the delivery, rotation, rename, restore and
gc paths at once. Neither is small. Bounding what the directory holds is: the
sibling locks go (gc's `chat-cursor-locks` row), cursors of a seat session
that is over go (`chat-cursors` for dead sessions, `chat-unpaired-cursors`
for a live session its seat no longer runs), the steer latches of a dead
session go (`chat-steer-latches`), and a meld room idle past the
bound, or a finished meld a day after its last post, is archived off the bus
with every cursor it carried (`helm chat retire-rooms`). The restore honours
the retirement, so a reboot does not bring the rooms back. `helm doctor` counts entries per room and
names the commands when the directory grows past its budget again.
"""
import json
import os
import re
import shutil
import sys
import time

from . import chat, pk

LOCK_EXT = ".lock"
DAY = 86400
IDLE_DAYS = 7                 # a meld room this long without a post is over
CLOSED_IDLE_DAYS = 1          # a FINISHED meld's room this long quiet is over
RETIRE_PREFIX = "meld-"       # a meld room; a task's pair meld is the other
                              # kind retirement may touch (_meld_room)
KEEP_PREFIX = "meld-0-standing-"  # a pair's standing room stays open (task/3560)
PER_ROOM_WARN = 200           # healthy: 2 x (rostered seats + live sessions)
ENTRIES_WARN = 20000          # ~2.6us/entry measured: 20k is ~50ms a listing
ARCHIVE_DIR = "retired-rooms"
INDEX_NAME = "retired-rooms.json"

_DEV_INODE = re.compile(r"^([0-9a-f]+):([0-9a-f]+):(\d+)$")


def _parse(root, name):
    from .seats_cursor import parse_cursor_path
    return parse_cursor_path(os.path.join(root, name))


def is_sibling_lock(path):
    """Is `path` the stable `<cursor>.lock` sibling of a cursor file?

    THE PARSER IS THE DISCRIMINATOR, NOT THE SUFFIX. `.cursor-room.<room>.lock`,
    `.cursor-estate.<seat>.lock`, `.cursor-txn-room.<room>.lock`,
    `.cursor-topology.lock`, `<room>.lock` and `.roster.json.lock` are
    STANDALONE locks that live processes hold right now; their subject is not
    a cursor name, so `parse_cursor_path` rejects every one of them. Only a
    lock whose subject parses as a cursor is a per-cursor sibling, and no code
    has opened one of those since d047d93d5eb."""
    name = os.path.basename(path)
    if not name.endswith(LOCK_EXT):
        return False
    return _parse(os.path.dirname(path) or ".",
                  name[:-len(LOCK_EXT)]) is not None


def held_inodes(path="/proc/locks"):
    """{(st_dev, st_ino)} of every inode any process holds a lock on, or None
    when the table cannot be read. Each line names its inode as
    `MAJOR:MINOR:INODE` with the device numbers in hex; a blocked waiter's
    line carries an extra `->` token, so the field is found by shape."""
    try:
        with open(path, encoding="ascii", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    held = set()
    for line in lines:
        for tok in line.split():
            m = _DEV_INODE.match(tok)
            if m:
                held.add((os.makedev(int(m.group(1), 16), int(m.group(2), 16)),
                          int(m.group(3))))
                break
    return held


def _listing(root):
    try:
        return os.listdir(root)
    except FileNotFoundError:
        return []


def sibling_locks(root=None, held=None):
    """Every cursor-sibling `.lock` in the chat dir that no process holds.

    ORPHAN OR NOT. The retired `orphan_cursor_locks` took only a lock whose
    cursor was gone, because a present cursor's lock was a live handle. Since
    d047d93d5eb it is not: `_cursor_locks` takes one lock per ROOM, and the
    sibling of a present cursor is as dead as an orphan. Measured on the live
    bus: 49,718 siblings, of which the orphan rule could select 6.

    NOTHING HELD IS NOMINATED. An inode in /proc/locks is skipped here, and
    `helm gc` then takes flock(LOCK_EX|LOCK_NB) on each victim itself before
    unlinking it, so a process still running pre-d047d93d5eb code keeps its
    handle: unlinking a held lock detaches the name, and the next opener
    takes an flock on a fresh inode the holder cannot see. A lock table that
    cannot be read proves nothing unheld, so this raises and gc reports the
    row as an error instead of an empty stream."""
    root = root or chat.chat_dir()
    held = held_inodes() if held is None else held
    if held is None:
        raise RuntimeError("/proc/locks is unreadable, so no cursor lock can "
                           "be proven unheld — nominated nothing")
    out = []
    for name in _listing(root):
        path = os.path.join(root, name)
        if not is_sibling_lock(path):
            continue
        try:
            st = os.stat(path, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if (st.st_dev, st.st_ino) not in held:
            out.append(path)
    return sorted(out)


# ── (b) a live session its seat no longer runs ─────────────────────────────


def _live_pairs():
    """(live sid prefixes, live (seat_key, sid8) waiter pairs, sids any seat
    may still run). Raises when liveness cannot be proven: an unknown session
    is not a dead one, exactly as chat.dead_cursors keeps everything then."""
    from . import beacons, sessions
    from .seats_common import _seat_key
    held = sessions.live_sids()
    live = set(held)
    rows = beacons.entries() or []
    records = beacons.holder_records() if rows else {}
    procs = held if isinstance(held, dict) else dict.fromkeys(live)
    pairs, anyseat = set(), set()
    for row in rows:
        state = beacons.classify(row["pid"], row.get("seat"), row=row,
                                 live=procs, records=records)
        sid = state.get("session")
        if state.get("state") not in (beacons.LIVE, beacons.UNKNOWN) \
                or not isinstance(sid, str) or not sid:
            continue
        live.add(sid)
        if row.get("seat"):
            pairs.add((_seat_key(row["seat"]), str(sid)[:8]))
        else:
            anyseat.add(str(sid)[:8])
    return {str(s)[:8] for s in live if s}, pairs, anyseat


def unpaired_session_cursors(root=None, liveness=None, rows=None):
    """Session cursors whose SEAT no longer runs that session. -> [paths]

    THE CLASS `chat.dead_cursors` CANNOT SEE. It asks one question -- is the
    session alive -- and keeps every cursor whose session is. But a live
    session can leave a seat: it is re-seated, renamed, or a pane is handed
    to another identity, and the roster then binds the session to a DIFFERENT
    seat while the old seat's cursors for it stay, one pair per room, for as
    long as the session lives. Measured on the live bus: 1,328 cursors under
    four seats whose rows remember no session at all, every one keyed on a
    session the roster binds to another seat.

    A PAIR IS OVER only when all of these hold, and each failure keeps it:
      * the session is live (a dead one is chat.dead_cursors' to reap -- two
        actuators for one file is how two policies start);
      * no live waiter runs this (seat, session) pair, and no seatless waiter
        runs the session at all;
      * the seat's roster row does not remember the session, AND another
        seat's row does -- a session nobody claims is unknown, not moved;
      * the room is not the seat's own DM lane: seats_gc keeps a retained
        lane's cursors as the delivery obligation for the rows in it;
      * the seat still holds its seat-level cursor of the same kind in that
        room, the baseline `_init_cursor` would inherit, so removing the pair
        never leaves the seat without an admission line there.
    An unreadable roster or an unprovable liveness raises: gc shows the row
    as an error, which is not the same answer as an empty stream."""
    from .seats_common import _seat_key
    from .seats_rename import rename_journal_path
    from .seats_roster import roster_checked
    root = root or chat.chat_dir()
    names = _listing(root)
    present = set(names)
    candidates = []
    for name in names:
        parsed = _parse(root, name)
        if parsed is None or not parsed["session_key"]:
            continue
        key = parsed["seat_key"]
        if parsed["room"] == pk.slug(chat.DM_PREFIX + key):
            continue
        base = os.path.basename(parsed["path"])
        if base[:len(base) - len(parsed["session_key"]) - 1] not in present:
            continue
        candidates.append((os.path.join(root, name), key,
                           parsed["session_key"][:8]))
    if not candidates:
        return []            # nothing to judge: ask neither liveness nor roster
    if os.path.exists(rename_journal_path()):
        raise RuntimeError("a seat rename is in flight — cursor ownership is "
                           "moving, nominated nothing")
    live, pairs, anyseat = _live_pairs() if liveness is None else liveness
    if rows is None:
        rows, failed = roster_checked()
        if failed:
            raise RuntimeError("roster unreadable — cannot tell which seat "
                               "runs a session, nominated nothing")
    bound = {}
    for seat, row in rows.items():
        if not isinstance(row, dict):
            continue
        for sid in [row.get("session")] + list(row.get("sessions") or ()):
            if sid:
                bound.setdefault(str(sid)[:8], set()).add(_seat_key(seat))
    out = []
    for path, key, sid in candidates:
        if sid not in live or sid in anyseat or (key, sid) in pairs:
            continue
        owners = bound.get(sid, set())
        if owners and key not in owners:
            out.append(path)
    return sorted(out)


# ── (b2) the steer latches of a dead session ───────────────────────────────

#: chat._steer_latch's name: ptusteer.<slugged steer>.<session key>[.a<agent
#: key>], each key a 16-hex blake2b digest (chat._latch_key).
_LATCH = re.compile(r"^ptusteer\.[A-Za-z0-9_-]+\.([0-9a-f]{16})"
                    r"(?:\.a[0-9a-f]{16})?$")


def dead_steer_latches(root=None, live=None, waiters=None):
    """The once-per-context steer latches whose SESSION is dead. -> [paths]

    A latch (chat.steer_unfired) is one file per (steer, session[, subagent])
    in the chat dir, and its only reaper was chat.forget_steers at the SAME
    session's next context boundary. A session that ended never has one, so
    every latch it spent stayed in the directory every `list_rooms` reads,
    and the doors add one per bound rule said. Dead is chat._live_sessions'
    answer, the one the cursor rows read; the name carries a hash of the
    session id, so each live id is hashed to compare. Reaping a latch of a
    live session costs one repeated line at worst, never a lost one.

    No latch in the directory asks no liveness. An unprovable liveness
    raises: gc shows the row as an error, never as an empty stream."""
    root = root or chat.chat_dir()
    found = []
    for name in _listing(root):
        m = _LATCH.match(name) if name.startswith("ptusteer.") else None
        if m:
            found.append((os.path.join(root, name), m.group(1)))
    if not found:
        return []
    keys = {chat._latch_key(str(s)) for s in chat._live_sessions(live, waiters)
            if s}
    return sorted(path for path, key in found if key not in keys)


# ── (c) retire idle meld rooms ──────────────────────────────────────────────


def archive_root():
    return os.path.join(chat.journal_dir(), ARCHIVE_DIR)


def _index_path():
    return os.path.join(chat.journal_dir(), INDEX_NAME)


def retired_through():
    """{room: through_ts} — the newest row time each retired room held.

    THE RESTORE'S FILTER. The disk journal holds every row a retired room
    ever flushed, so without this a reboot's auto-restore rebuilds every
    retired room from it and mints a cursor pair for every rostered seat in
    each. A journal record at or before `through` belongs to the archived
    room; a record after it was posted to a room reborn under the same name
    and still restores. Unreadable reads as {}: restoring too much is the
    behaviour before retirement existed, never a loss."""
    raw = pk.read_json(_index_path(), None)
    rooms = raw.get("rooms") if isinstance(raw, dict) else None
    if not isinstance(rooms, dict):
        return {}
    return {room: entry["through"] for room, entry in rooms.items()
            if isinstance(entry, dict) and isinstance(entry.get("through"), str)}


def _standing_pair_is_kept(room):
    """Does this standing room's pair name only known seats?

    A room is kept while BOTH members are seats — the same known-seat test the
    standing verb uses to open with them, so a room the verb would open the
    verb keeps, and a room it refuses a flag or an absent seat for is one the
    sweep may clear. UNREADABLE and NON-TWO-MEMBER pairs fail closed to KEPT: I
    cannot prove the pair, so I do not retire on the guess (a real room whose
    OPEN row rotated out stays rather than vanishing)."""
    from . import meld_standing
    try:
        pair = meld_standing._read(room)[2]["pair"]
    except Exception:                                   # noqa: BLE001
        return True
    if len(pair) != 2:
        return True
    return all(meld_standing.known_seat(p) for p in pair)


def _meld_room(room):
    """A room retirement may touch: a `meld-` room, or a task's pair meld,
    whose `<scope>-<N>` name carries no prefix (`review_door.is_pair_room`)."""
    from . import review_door
    return room.startswith(RETIRE_PREFIX) or review_door.is_pair_room(room)


def _finished(room):
    """meld.finished, and any failure to answer is NOT finished."""
    from . import meld
    try:
        return meld.finished(room)
    except Exception:
        return False


def _retirable(idle_days=IDLE_DAYS, now=None):
    """[(room, idle_seconds, bound_days, finished)] — the bound each room is
    retired under and whether it was the finished-meld one; `retire_room`
    re-proves both under the room lock."""
    now = time.time() if now is None else now
    out = []
    for room in chat.list_rooms():
        if not _meld_room(room):
            continue
        try:
            idle = now - os.stat(chat.room_path(room)).st_mtime
        except OSError:
            continue
        if room.startswith(KEEP_PREFIX) and _standing_pair_is_kept(room):
            continue
        if idle >= idle_days * DAY:
            out.append((room, idle, idle_days, False))
        elif idle >= CLOSED_IDLE_DAYS * DAY and _finished(room):
            out.append((room, idle, CLOSED_IDLE_DAYS, True))
    return out


def retirable_rooms(idle_days=IDLE_DAYS, now=None):
    """[(room, idle_seconds)] — meld rooms with no append for `idle_days`,
    and FINISHED melds with none for CLOSED_IDLE_DAYS.

    WHY NOT THE LANE. A meld room records its topic, its epoch and its pinned
    pair; it records no lane and no branch, so "the lane landed" is not a
    fact any meld can be asked. The room's own clock is: every post appends,
    and a restore rewrites the file, so its mtime is the last time anything
    happened in it -- conservative in exactly the direction that keeps a
    room. A pair's standing room (KEEP_PREFIX) is the one kind that outlives
    idle: it stays open while both of its pair are seats, and only a room
    whose pair names a flag or a seat the roster has no row for is swept.

    WHY A FINISHED MELD GOES SOONER (task/3519). Measured on the live bus:
    272 of 306 room logs were meld rooms, carrying about 22,000 cursors, and
    about 20,600 of those sat on melds idle over a day. Every cursor a listed
    room loses, the delivery pass mints again, so the ROOM has to leave, not
    its cursors. Whether a meld is over is the meld's own fact
    (`meld.finished`: every member of its newest round closed its side), so
    the shorter bound keys on that and never on quiet alone -- an open meld
    as quiet waits for the idle bound. The day of quiet is the grace in which
    a closer's countersign read and the owner's last look still find the
    room on the bus. NOT AT THE CLOSE ITSELF: the second DONE lands while
    the first closer has yet to read it, so retiring there would take the
    countersign off the bus before its reader arrives."""
    return [(room, idle) for room, idle, _b, _f in _retirable(idle_days, now)]


def _through(path, mtime):
    """The newest `ts` among the room's rows, else the file's mtime."""
    best = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                ts = json.loads(line).get("ts")
            except (ValueError, AttributeError):
                continue
            if isinstance(ts, str) and (best is None or ts > best):
                best = ts
    return best or pk.epoch_ts(mtime)


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _copy_durable(src, dest):
    tmp = dest + ".tmp"
    with open(src, "rb") as f, open(tmp, "wb") as g:
        shutil.copyfileobj(f, g)
        g.flush()
        os.fsync(g.fileno())
    os.replace(tmp, dest)
    if os.path.getsize(dest) != os.path.getsize(src):
        raise OSError("archive copy of %s is short" % os.path.basename(src))


def _record(room, entry):
    index = pk.read_json(_index_path(), None)
    if not isinstance(index, dict) or not isinstance(index.get("rooms"), dict):
        index = {"v": 1, "rooms": {}}
    index["rooms"][room] = entry
    path = _index_path()
    pk.write_json(path, index)
    # THIS RECORD IS THE ONLY THING THAT STOPS THE RESTORE FROM RESURRECTING
    # THE ROOM, and retire_room unlinks the room right after this returns. The
    # archive it points at is fsynced (`_copy_durable`, `_fsync_dir`); the
    # record must be too, or an unclean power loss between `write_json`'s
    # rename and the filesystem's own commit would take the room off tmpfs
    # while the disk still says nothing was retired, and the reboot's restore
    # would rebuild it from the journal. Make it durable before the unlink,
    # like the archive.
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_dir(chat.journal_dir())


def retire_room(room, idle_days=IDLE_DAYS, now=None, closed=False):
    """Archive one idle meld room off the live bus. -> (report, refusal)

    ARCHIVE, NEVER DELETE. The room file and every per-room sibling that is
    not a lock or a cursor (the meld actor snapshots, owner markers) are
    copied to <journal>/retired-rooms/<room>/ and fsynced BEFORE anything on
    the bus is removed; the index entry that stops the restore is written
    next; only then do the tmpfs copies and the room's cursors go. A crash at
    any step leaves the room live or leaves an archive that already holds
    it -- never neither.

    UNDER THE LOCKS EVERY COMPETING WRITER TAKES, in rotation's order: the
    room's append lock (so no post lands between the copy and the unlink),
    then cursor topology, the room's cursor lock, every seat estate that has
    a cursor here, and the room's transaction lock -- which also recovers a
    prepared cursor transaction before anything is removed. Idleness is
    re-proven under them: a post since the scan keeps the room. `closed`
    says the room was nominated under the finished-meld bound, and then the
    meld's being finished is re-proven there too (a pure lifecycle read,
    safe under the room lock).

    WHAT STAYS. The room's standalone locks (`<room>.lock`,
    `.cursor-room.<room>.lock`, `.cursor-txn-room.<room>.lock`) are held by
    this very call; unlinking a held lock hands the next opener a fresh inode
    and two holders. The transaction record beside them is what a lock-free
    cursor reader checks its epoch against. Four entries a room, gone at the
    next boot and never re-minted, because the restore no longer resurrects
    the room. The cursors' `.lock` siblings are gc's `chat-cursor-locks`
    row's, which probes each one before it unlinks."""
    from .seats_cursor import (_cursor_estate_locks, _cursor_locks,
                               _cursor_room_locks, _cursor_topology_lock)
    now = time.time() if now is None else now
    if not _meld_room(room):
        return None, "not a meld room"
    if room.startswith(KEEP_PREFIX) and _standing_pair_is_kept(room):
        return None, "a pair's standing room stays open while both of its pair " \
                     "are seats"
    # The archive and its index live in the durable journal, and the index
    # stops the LIVE bus from replaying the room. An isolated namespace must
    # not retire a live room it only holds a copy of.
    isolated = chat.journal_write_refusal()
    if isolated:
        return None, isolated
    key = pk.slug(room)
    root = chat.chat_dir()
    src = chat.room_path(room)
    with chat._room_lock(room) as locked:
        if not locked:
            return None, "room lock unavailable"
        with _cursor_topology_lock(), _cursor_room_locks({key}):
            try:
                st = os.stat(src)
            except FileNotFoundError:
                return None, "room is already gone"
            if now - st.st_mtime < idle_days * DAY:
                return None, "posted to since the scan"
            if closed and not _finished(room):
                return None, "the meld is no longer finished"
            # the substring test only skips the parse for names that cannot
            # be this room's; the parser still decides every candidate
            names = [n for n in _listing(root) if key in n]
            cursors = [p for p in (_parse(root, n) for n in names)
                       if p is not None and p["room"] == key]
            keep = {os.path.basename(src)}
            siblings = sorted(
                n for n in names
                if n.startswith(key + ".") and n not in keep
                and not n.endswith(LOCK_EXT) and _parse(root, n) is None
                and os.path.isfile(os.path.join(root, n)))
            with _cursor_estate_locks({p["seat_key"] for p in cursors}), \
                    _cursor_locks([p["path"] for p in cursors], estate=False):
                through = _through(src, st.st_mtime)
                dest = os.path.join(archive_root(), key)
                if os.path.exists(dest):
                    dest += "." + time.strftime("%Y%m%dT%H%M%SZ",
                                                time.gmtime(now))
                os.makedirs(dest, mode=0o700)
                for name in [os.path.basename(src)] + siblings:
                    _copy_durable(os.path.join(root, name),
                                  os.path.join(dest, name))
                _fsync_dir(dest)
                _record(room, {"through": through,
                               "retired_at": pk.epoch_ts(now),
                               "archive": os.path.relpath(
                                   dest, chat.journal_dir()),
                               "bytes": st.st_size})
                for name in siblings + [os.path.basename(src)]:
                    os.remove(os.path.join(root, name))
                removed = 0
                for p in cursors:
                    try:
                        os.remove(p["path"])
                        removed += 1
                    except FileNotFoundError:
                        pass
    return {"room": room, "archive": dest, "moved": 1 + len(siblings),
            "cursors": removed, "through": through}, None


def cmd_retire_rooms(args):
    """chat retire-rooms [--apply] [--idle-days N] — archive meld rooms idle
    past the bound, and finished melds quiet past CLOSED_IDLE_DAYS, off the
    live bus (dry-run default)."""
    from .cli import guard_tail
    usage = ("usage: helm chat retire-rooms [--apply] [--idle-days N]  "
             "(archive meld rooms with no post for N days, default %d, and "
             "finished melds with none for %d, to <journal>/%s; their "
             "cursors go with them and the restore no longer resurrects "
             "them. Dry-run by default)"
             % (IDLE_DAYS, CLOSED_IDLE_DAYS, ARCHIVE_DIR))
    rc = guard_tail("helm chat retire-rooms", args, flags=("--apply",),
                    valued=("--idle-days",), usage=usage)
    if rc is not None:
        return rc
    days = IDLE_DAYS
    if "--idle-days" in args:
        raw = args[args.index("--idle-days") + 1]
        try:
            days = float(raw)
        except ValueError:
            days = -1
        if days < 1:
            print("helm chat retire-rooms: --idle-days wants a number of "
                  "days, at least 1 — got %r" % raw, file=sys.stderr)
            return 2
    apply = "--apply" in args
    rooms = _retirable(days)
    if not rooms:
        print("helm chat retire-rooms: no meld room has been idle %g days, "
              "and no finished meld quiet %d — nothing to retire"
              % (days, CLOSED_IDLE_DAYS))
        return 0
    if not apply:
        for room, idle, _bound, done in rooms:
            print("  would retire  %s  (idle %dd%s)"
                  % (room, idle // DAY, ", meld finished" if done else ""))
        print("helm chat retire-rooms: %d meld room%s (idle %g+ days, or "
              "finished and quiet %d+) would be archived to %s — nothing "
              "touched; --apply retires them"
              % (len(rooms), "s"[:len(rooms) != 1], days, CLOSED_IDLE_DAYS,
                 archive_root()))
        return 0
    done = cursors = refused = 0
    for room, _idle, bound, finished in rooms:
        try:
            out, why = retire_room(room, bound, closed=finished)
        except OSError as exc:
            out, why = None, "%s: %s" % (type(exc).__name__, exc)
        if out is None:
            refused += 1
            print("  KEPT     %s  (%s)" % (room, why))
            continue
        done += 1
        cursors += out["cursors"]
        print("  retired  %s  -> %s (%d cursor%s)"
              % (room, out["archive"], out["cursors"],
                 "s"[:out["cursors"] != 1]))
    print("helm chat retire-rooms: retired %d room%s, removed %d cursor%s%s"
          % (done, "s"[:done != 1], cursors, "s"[:cursors != 1],
             "; %d kept" % refused if refused else ""))
    return 1 if refused and not done else 0


# ── the census the doctor rung reads ────────────────────────────────────────


def census(root=None):
    """One listing of the chat dir, classified by kind. Raises OSError when
    the directory exists and cannot be listed."""
    root = root or chat.chat_dir()
    names = _listing(root)
    out = {"entries": len(names), "rooms": 0, "cursors": 0,
           "seat_cursors": 0, "sibling_locks": 0}
    for name in names:
        if name.endswith(".jsonl"):
            out["rooms"] += 1
        elif name.endswith(LOCK_EXT):
            if _parse(root, name[:-len(LOCK_EXT)]) is not None:
                out["sibling_locks"] += 1
        else:
            parsed = _parse(root, name)
            if parsed is not None:
                out["cursors"] += 1
                out["seat_cursors"] += not parsed["session_key"]
    return out
