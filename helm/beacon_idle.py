"""What an idle beacon waits on, so that it waits without working (task/3873).

A WHOLE DELIVERY PASS AT EVERY TICK COSTS 7-10% OF A CORE PER WAITER,
measured on 32 idle `helm chat wait --seat X --follow` waiters, about 2.5
cores in all. Such a pass lists the chat directory, stamps every foreign
room for the rotation, reads the roster several times, and asks the sink
classifier about its stdout, whose process-table walk (0.3 s of CPU over
1,150 processes, measured) repeats every 15 s. While any row waits unrung,
the doorbell also reads its state and every room tail behind it.

A PASS FINDS ONLY WHAT A ROOM WRITE PUT THERE. `_room_dirty` is the
precheck `deliver_any` makes for itself: a room can hold a row for this
consumer only while its file holds bytes past the consumer's beacon cursor,
and a false negative cannot happen. So the waiter keeps the rooms written
since it last found them clean, and it runs a pass only while one of them
is still dirty. A room is written when the kernel reports a write to its
file (inotify), or, where inotify cannot be had, when one stat of the file
differs from the last one. A row the pass withholds (a deaf sink, a paused
delivery, a rotation cut short) leaves its room dirty, so the next tick
runs a pass again, as every tick did before.

THE WAKES A PASS OWES WITHOUT A ROOM WRITE keep their own clocks:
  * the doorbell's debounce, hourly cap and backstop (`Doorbell.due`);
  * the presence beat: the `.seen` mtime that `presence_of` reads against
    FRESH_S (120 s), stamped every BEAT_S by a delivery call with
    `sink_usable=False`, which stamps presence and reads no room;
  * one pass every SAFETY_S, for a room made dirty without a write to it
    (a cursor that a sweep moved back). That pass reads only its rotation
    slice of the rooms, so the tick after it checks every room's cursor
    again, and a room still dirty keeps a pass at every tick until one
    reads it.
The waiter's expiry and its orphan check stay in `seats_join.wait`, which
still ticks every POLL_S, so a new row waits no longer than it did.
"""
import ctypes
import os
import struct
import time
import weakref

from . import beacon_doorbell, chat, seats_delivery, seats_roomscan
from .seats_common import dm_lane

#: The presence beat's period. `presence_of` reads a seat as fresh for
#: FRESH_S (120 s) after its last beat, so four beats fit in that window.
BEAT_S = 30.0
#: A full pass runs at least this often, whatever the watch reports.
SAFETY_S = 300.0

_IN_MODIFY, _IN_MOVED_FROM, _IN_MOVED_TO, _IN_CREATE = 0x2, 0x40, 0x80, 0x100
_IN_DELETE, _IN_DELETE_SELF, _IN_MOVE_SELF = 0x200, 0x400, 0x800
_IN_Q_OVERFLOW, _IN_IGNORED, _IN_ISDIR = 0x4000, 0x8000, 0x40000000
_MASK = (_IN_MODIFY | _IN_MOVED_FROM | _IN_MOVED_TO | _IN_CREATE
         | _IN_DELETE | _IN_DELETE_SELF | _IN_MOVE_SELF)
_GONE = _IN_IGNORED | _IN_DELETE_SELF | _IN_MOVE_SELF
_EVENT = struct.Struct("iIII")


def _stamp(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_ino, st.st_size, st.st_mtime_ns


class _Kernel:
    """Room writes as the kernel reports them: one inotify watch on the chat
    directory and one on its dm/ subdirectory, read without blocking.

    `changed` answers the set of rooms written since the last call, or None
    for every room: the event queue overflowed, or a watched directory went
    away, and the watch re-arms. dm/ can exist while its watch cannot be
    added (ENOSPC, ENOMEM, EACCES); until it is, a DM writes no event this
    reads, so every tick answers None and asks for the watch again. Another
    seat's DM lane is never this seat's room, so a write to it is not
    reported."""

    def __init__(self, libc, fd, seat):
        self.libc, self.fd = libc, fd
        self.dm_room = dm_lane(seat)
        self.dm_file = os.fsencode(os.path.basename(chat.room_path(self.dm_room)))
        self.top = self.dm = None

    def _add(self, path):
        wd = self.libc.inotify_add_watch(self.fd, os.fsencode(path), _MASK)
        return wd if wd >= 0 else None

    def _arm(self):
        d = chat.chat_dir()
        self.top = self._add(d)
        self.dm = self._add(os.path.join(d, "dm"))

    def changed(self):
        rooms = self._read() if self.top is not None else None
        if rooms is None:
            self._arm()
        elif self.dm is None:
            dm = os.path.join(chat.chat_dir(), "dm")
            if os.path.isdir(dm):
                self.dm = self._add(dm)
                return None          # its writes until now went unseen
        return rooms

    def _read(self):
        rooms = set()
        while True:
            try:
                buf = os.read(self.fd, 65536)
            except BlockingIOError:
                return rooms
            except OSError:
                return None
            off = 0
            while off < len(buf):
                wd, mask, _cookie, n = _EVENT.unpack_from(buf, off)
                name = buf[off + 16:off + 16 + n].rstrip(b"\0")
                off += 16 + n
                if mask & _IN_Q_OVERFLOW or (wd == self.top and mask & _GONE):
                    return None
                if wd == self.top and name == b"dm" and mask & _IN_ISDIR:
                    self.dm = self._add(os.path.join(chat.chat_dir(), "dm"))
                    rooms.add(self.dm_room)
                elif wd == self.top and name.endswith(b".jsonl"):
                    rooms.add(os.fsdecode(name[:-6]))
                elif wd == self.dm and name == self.dm_file:
                    rooms.add(self.dm_room)


def _kernel(seat):
    """A `_Kernel` watch, or None where inotify cannot be had."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
    except (OSError, AttributeError):
        return None
    if fd < 0:
        return None
    w = _Kernel(libc, fd, seat)
    weakref.finalize(w, os.close, fd)
    w._arm()
    return w


class _Stamps:
    """Room writes read off the files where inotify cannot be had: one stat
    per room log each tick, over the shared room listing
    (`seats_roomscan.room_names`), which is listed again only when the
    room-set token moves or the listing is NAMES_MAX_AGE_S old. The chat
    directory's own mtime cannot say a room was born: cursors and locks
    beside the logs move it several times a second."""

    def __init__(self, seat):
        self.dm_room = dm_lane(seat)
        self.seen = {}
        self.changed()               # the baseline: later writes are changes

    def changed(self):
        try:
            keys = set(seats_roomscan.room_names()[2])
        except OSError:
            return None
        now = {k: _stamp(chat.room_key_path(k)) for k in keys}
        now[self.dm_room] = _stamp(chat.room_path(self.dm_room))
        out = {k for k in now.keys() | self.seen.keys()
               if now.get(k) != self.seen.get(k)}
        self.seen = now
        return out


class Idle:
    """One follow waiter's answer, each tick, to whether a pass can find
    anything. `skip(bell)` is True when it cannot: then it beats presence
    when a beat is due, and the waiter only sleeps."""

    def __init__(self, seat, session, beat, quiet=None):
        self.seat, self.session, self.beat = seat, session, beat
        self.quiet = quiet           # the waiter's seats_roomscan.QuietRooms
        self.watch = _kernel(seat) or _Stamps(seat)
        self.dirty = None            # None is every room, until a pass has run
        self.passed = self.beaten = None

    def _prune(self):
        """The rooms still dirty for this consumer. A room with no file holds
        nothing, and `_room_dirty` would call it dirty for having no cursor:
        the pass scans a DM lane only once its file exists.

        A ROOM FOUND DIRTY LOSES ITS QUIET PROOF. The waiter's `QuietRooms`
        skips a pass while every log is at the stamp it proved quiet at, so
        a cursor moved back with no write stays proved; this gate finds it
        after the safety pass, and a pass the proof then skipped would run
        at every tick and never read the room."""
        rooms = self.dirty
        if rooms is None:
            rooms = set(seats_roomscan.room_names()[2]) | {dm_lane(self.seat)}
        dirty = {r for r in rooms if os.path.exists(chat.room_path(r))
                 and seats_delivery._room_dirty(r, self.seat, self.session,
                                                beacon=True)}
        for r in dirty if self.quiet is not None else ():
            self.quiet.forget(r)
        return dirty

    def skip(self, bell=None):
        now = time.time()
        try:
            got = self.watch.changed()
        except Exception:                               # noqa: BLE001
            got = None               # cannot read the watch: check every room
        self.dirty = None if got is None or self.dirty is None \
            else self.dirty | got
        if self.passed is not None:
            try:
                self.dirty = self._prune()
            except Exception:                           # noqa: BLE001
                self.dirty = None    # cannot look: a pass looks instead
        safety = self.passed is None or now - self.passed >= SAFETY_S
        if safety or self.dirty is None or self.dirty \
                or (bell is not None and bell.due()):
            if safety:
                self.dirty = None    # the next tick checks every room again
            self.passed = self.beaten = now
            return False
        if now - self.beaten >= BEAT_S:
            self.beaten = now
            try:
                self.beat()
            except Exception:                           # noqa: BLE001
                pass                 # as a pass: a fault never ends the beacon
        return True


def arm(seat, session, stream, doorbell, room, ambient, quiet, deliver,
        usable):
    """(bell, idle) for one follow waiter (`seats_join.wait`): its doorbell
    (`beacon_doorbell.waiter_bell`), which carries `quiet` into every pass,
    and the gate each tick asks first (`Idle`). `deliver` and `usable` are
    the waiter's own lambdas, so `deliver_any` and `destination_usable`
    still resolve in seats_join at every call.

    ONE PROOF FOR THE WAITER'S LIFE (task/3848). Without one, every poll
    lists the whole chat directory and scans a slice of rooms whether or
    not anything was posted: measured at 16 beacons, 0.94 of a core on a
    bus nobody was writing to. A poll whose every room log is still where
    an earlier poll proved this consumer's cursor at its end is skipped
    (seats_roomscan.QuietRooms)."""
    bell = beacon_doorbell.waiter_bell(
        seat, session, stream, doorbell, room=room, ambient=ambient,
        quiet=quiet, deliver=deliver, usable=usable)
    return bell, Idle(seat, session, lambda: deliver(   # presence, no scan
        session=session, seat=seat, sink_usable=False, quiet=quiet), quiet)
