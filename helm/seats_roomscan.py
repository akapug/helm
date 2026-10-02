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

DIRTY ROOMS FIRST, THEN THE ROTATION. A pass hands out foreign rooms in this
order: rooms this lane never handed out, then rooms whose file changed since
the lane last handed them out (the one looked at longest ago first), then
clean rooms from the fair rotation. So a room written since the lane last
looked at it is in the NEXT pass whenever at most `size` rooms are dirty.
When more are, the dirt already there drains at least `size - 1` rooms per
pass in that order, so its wait is ceil(dirty / (size - 1)) passes and does
not scale with the number of rooms. The price is one stat per foreign room
per pass. The room the rotation handed out longest ago is never passed over,
so every room -- a clean one can still owe rows, when a pass was cut short
before reading it -- is handed out within one pass per room in the lane,
however long dirty rooms keep arriving.

`seats_delivery` re-exports `_fair_room_slice`, `_scan_rooms` and
`scan_path`, so no existing import path changed.

A PASS THAT NOTHING COULD FEED DOES NOT LOOK (`QuietRooms`, task/3848). The
listing and the scan are the whole cost of a pass that finds nothing, and a
beacon waiter made one every POLL_S whether or not anything was posted.
"""
import os
import sys
import time

from . import chat, pk
from .seats_common import ROOM_SCAN_CAP, _flocked, _seat_key, dm_lane
from .seats_cursor import _sid8, seat_state_lock
from .seats_identity import seat_scope


#: The ring file's record of each room AS IT WAS when this lane last handed
#: it out, beside `rooms`. An older reader reads only `rooms` and ignores this
#: key, and a writer that drops it costs one lap of plain rotation (every room
#: reads as never handed out), never a room.
LOOKED = "looked"

_GATESLICE_MUTABLE = {
    "_UNLOCKED_RINGS": (
        "the rotation a process keeps while its lane's ring lock cannot be "
        "taken, keyed by ring path; every chat dir a test plants is its own "
        "key"),
}
#: ring path -> the ring this process rotates while that path's lock cannot
#: be taken (task/2520). It is never written to disk: the persisted ring is
#: only ever written under its lock.
_UNLOCKED_RINGS = {}


def scan_path(seat, session=None, lane="deliver"):
    """Per-seat/session/lane overflow-ring state for eventual room coverage."""
    p = os.path.join(chat.chat_dir(), ".scan." + _seat_key(seat))
    s8 = _sid8(session)
    return ".".join(x for x in (p, s8, lane) if x)


def _stamp(room):
    """The room file as one comparable value, or None when it cannot be
    statted. st_mtime_ns is the change signal; the size also catches an
    append landing inside one coarse timestamp tick, and the inode a file
    replaced under its name. None never equals a record, so a room that
    cannot be statted always reads dirty: failing toward looking costs one
    slot, and failing toward clean hides a room nobody proved empty."""
    stamp = _log_stamp(room)
    return None if stamp == ABSENT else stamp


#: A room log that is provably not there. Only FileNotFoundError is absence:
#: a log that cannot be statted for any other reason might hold anything.
ABSENT = "absent"


def _log_stamp(room):
    """`_stamp`, with a provably absent log told apart (ABSENT) from one that
    cannot be statted (None). A rotation ring must look at both; a consumer
    proving quiet (`QuietRooms`) may count only the first."""
    try:
        st = os.stat(chat.room_path(room))
    except FileNotFoundError:
        return ABSENT
    except OSError:
        return None
    return "%d:%d:%d" % (st.st_mtime_ns, st.st_size, st.st_ino)


def _fair_room_slice(names, seat, session, size, lane, lent=None,
                     proven=None):
    """Up to `size` of `names`, DIRTY ROOMS FIRST, then a fair rotation.

    The persisted ring is least-recently-handed-out first; newly discovered
    rooms join it behind the rooms it knows. A pass takes, in order:

      1. rooms with no record -- never handed out, or whose stat failed --
         in ring order;
      2. rooms whose stamp differs from the record taken when they were last
         handed out, in ring order: the one looked at longest ago first;
      3. clean rooms, in ring order, into whatever slots remain.

    THE HEAD OF THE RING IS NEVER PASSED OVER. When dirty rooms fill every
    slot and the room handed out longest ago is not among them, it takes the
    last slot (size >= 2). Nothing is ever inserted ahead of a room already
    in the ring, so every room moves up at least one place per pass and is
    handed out within one pass per room in the ring, however long a flood of
    dirty rooms lasts. The dirty rooms pay for that with at most one slot per
    pass: the dirt already there drains within ceil(dirty / size) passes when
    the head is itself dirty or no clean room waits there, and within
    ceil(dirty / (size - 1)) when one does. A room that turns dirty later
    queues by the same order, so one never handed out goes ahead of it.

    HANDED OUT IS NOT READ. The record is taken at hand-out, before anyone
    reads, so a write after this stat always reads dirty next pass. A caller
    that stops before reading every room it was handed passes `lent` (filled
    with {room: (prior record, this pass's stamp)}) and hands the unread rooms
    back through `_give_back`, or they wait for the rotation.

    One stat per room per pass, taken before the lock so the lock is held no
    longer than before. Each consumer lane owns its ring: roster observation
    must never advance delivery or stop.

    `proven` is a `QuietRooms` consumer's proof, {room: stamp}: a room it
    proved quiet at the stamp the room has now holds nothing for it to read,
    whatever the ring's record says, so it waits in the rotation as a clean
    room does and leaves the slots to the rooms that moved. Without it a
    consumer that skipped its quiet polls would hand out, first, every room
    its ring had not reached yet, and the one room just written would wait
    behind them."""
    live = set(names)
    if not live or size <= 0:
        return []
    path = scan_path(seat, session, lane)
    now = {name: _stamp(name) for name in live}
    try:
        with seat_state_lock(seat, session=session) as current, _flocked(path + ".lock"):
            if not current:
                return sorted(live)[:size]
            out, state = _hand_out(pk.read_json(path, {}) or {}, live, now,
                                   size, lent, proven)
            pk.write_json(path, state)
            return out
    except OSError as exc:
        # THE RING LOCK FAILS CLOSED (task/2520): the saved ring is never
        # read-modified-written without it. Nor does the pass fall back to a
        # fixed prefix: the same ALPHABETICAL PREFIX on every pass starves
        # every room sorting after it (a dirty, mentioned one included) until
        # the lock is repaired. The pass rotates THIS PROCESS'S ring instead,
        # by the same order, seeded newest-written first, and lends nothing
        # (the saved ring did not move, so there is nothing to give back). A
        # long-lived waiter covers every room within a lap of its own passes;
        # a short-lived process reaches the rooms written most recently. It
        # says so.
        ring = _UNLOCKED_RINGS.get(path) or {"rooms": sorted(
            live, key=lambda r: (now[r] is not None, _newest(now[r]), r))}
        out, _UNLOCKED_RINGS[path] = _hand_out(ring, live, now, size, None,
                                               proven)
        print("[helm] room rotation state unavailable (%s) — this pass "
              "rotates in this process's memory, newest-written rooms first, "
              "and the lane's saved rotation does not advance; a short-lived "
              "process reaches only the rooms written most recently, so "
              "coverage is eventual only across a long-lived waiter's passes "
              "until the rotation lock can be taken again"
              % type(exc).__name__, file=sys.stderr)
        return out


def _newest(stamp):
    """Sort key: the most recently written room first (a stamp's mtime)."""
    try:
        return -int(str(stamp).split(":", 1)[0])
    except ValueError:
        return 0


def _hand_out(state, live, now, size, lent, proven=None):
    """(rooms handed out, the ring after) — the order `_fair_room_slice`
    documents, over a ring read from disk or kept in memory."""
    saved = state.get("rooms")
    saved = saved if isinstance(saved, list) else []
    looked = state.get(LOOKED)
    looked = dict(looked) if isinstance(looked, dict) else {}
    ring = []
    seen = set()
    for name in saved:
        if isinstance(name, str) and name in live and name not in seen:
            ring.append(name)
            seen.add(name)
    ring.extend(sorted(live - seen))
    proven = proven or {}
    dirty = [r for r in ring
             if (now[r] is None or looked.get(r) != now[r])
             and not (now[r] is not None and proven.get(r) == now[r])]
    dirty.sort(key=lambda r: r in looked)   # stable: keeps ring order
    out = dirty[:size]
    if len(out) < size:
        held = set(dirty)
        out += [r for r in ring if r not in held][:size - len(out)]
    elif size > 1 and ring[0] not in out:
        out[-1] = ring[0]
    for r in out:
        if lent is not None:
            lent[r] = (looked.get(r), now[r])
        if now[r] is None:
            looked.pop(r, None)
        else:
            looked[r] = now[r]
    handed = set(out)
    ring = [r for r in ring if r not in handed] + out
    return out, {"rooms": ring, LOOKED: {
        r: looked[r] for r in ring if r in looked}}


def _give_back(rooms, seat, session, lane, lent, hit=None):
    """Return rooms a pass was handed and never read to the state they had.

    A pass that stops at its first delivery never reads the rooms after the
    hit, and the hit room itself may hold more rows (one event per pass). The
    slice recorded all of them as looked at, so without this they would read
    clean and wait a lap. Each room in `rooms` that `lent` names gets back the
    record it had before this pass; the `hit` room loses its record, so the
    next pass reads it again whatever it held before.

    ONLY A RECORD THIS PASS WROTE IS UNDONE. If another pass handed the room
    out since, its record stands: the other pass may have read it, and undoing
    its record could only make a room it read look unread (never the reverse).
    A failure here costs latency, never a room -- the rotation still covers
    it -- and says so."""
    back = [r for r in rooms if r in lent]
    if not back:
        return
    path = scan_path(seat, session, lane)
    try:
        with seat_state_lock(seat, session=session) as current, \
                _flocked(path + ".lock"):
            state = pk.read_json(path, None) if current else None
            looked = state.get(LOOKED) if isinstance(state, dict) else None
            if not isinstance(looked, dict):
                return
            for r in back:
                prior, stamp = lent[r]
                if looked.get(r) != stamp:
                    continue
                if r == hit or prior is None:
                    looked.pop(r, None)
                else:
                    looked[r] = prior
            pk.write_json(path, state)
    except OSError as exc:
        print("[helm] room rotation give-back failed (%s) — rooms this pass "
              "never read wait for the rotation instead of the next pass"
              % type(exc).__name__, file=sys.stderr)


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
                scan_lane=None, bounded=True, report=None, lent=None,
                names=None, proven=None):
    """Rooms considered by one delivery/gate pass. The seat's private DM lane,
    primary room, home room, and main are pinned first. Foreign rooms use a
    ROOM_SCAN_CAP-bounded overflow budget on hot paths; when that budget
    overflows, a persisted identity queue per consumer lane hands out dirty
    rooms first and then rotates (`_fair_room_slice`), so a room written since
    the lane last looked is in the next pass and every room is covered
    eventually, without observation stealing delivery slots. `lent` is passed
    through to that slice for a caller that may stop before reading every
    room. Join is the one-time `bounded=False` caller so every existing room
    receives an EOF admission baseline and pre-join backlog can never emerge
    in a later slice.

    WITHOUT A LANE OR A SEAT the overflow keeps the `size` rooms written most
    recently. A fresh write sorts first there, so that branch never had the
    lap-long wait the rotation had; it is not eventual, though, since a quiet
    room outside the newest `size` is never reached. Every production caller
    that overflows passes both, so only a lane-less probe reaches it.

    The scan is deliberately scope-BLIND (premise beacon-scope-mentions-
    plus-home-room-owner-posts-not-all superseded the G1-G3 homing allowlist):
    an @mention anywhere must eventually surface, while deliverable() applies
    per-row scope. DM lanes other than the seat's own stay invisible because
    list_rooms() omits them. Fail-open: an unlistable dir is just the pins.

    `names`, when given, is the room listing the caller already holds (a
    `QuietRooms` consumer's, read from `room_names`), and the directory is
    not listed again. None lists it, as every caller did before. `proven`
    is that consumer's proof, passed to the rotation (`_fair_room_slice`)."""
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
        names = chat.list_rooms() if names is None else list(names)
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
            others, seat, session, size, scan_lane, lent=lent, proven=proven)
    else:
        def mtime(n):
            # ONLY ABSENCE IS EMPTINESS, as in `_room_dirty`: a room that is
            # gone has nothing to read, and a room that cannot be statted
            # might hold anything, so it sorts first, not last.
            try:
                return os.stat(chat.room_path(n)).st_mtime
            except FileNotFoundError:
                return 0.0
            except OSError:
                return float("inf")
        others.sort(key=mtime, reverse=True)
        others = others[:size]
    return rooms + others


# ── a pass that nothing could feed (task/3848) ──────────────────────────────

#: How long a pass trusts a room listing whose room-set token has not moved.
#: `chat.rooms_generation` moves when a writer CREATES a log, so a listing
#: keyed on it is exact for every writer that moves it. This bound is the
#: backstop for a log made by a writer running older code, which does not
#: move it (web_sse._log_names keeps the same bound for the same reason).
NAMES_MAX_AGE_S = 30.0
#: The shared listing lives in a state family under the chat dir, never in
#: the flat directory it stands in for: an entry there is one more name that
#: every listing reads.
NAMES_FAMILY = "rooms"
NAMES_FILE = "names.json"


def room_names(token=None, now=None):
    """(token, listed_at, names): the room listing, made again only when the
    room set may have moved. Raises OSError when a listing is due and fails,
    exactly as `chat.list_rooms` does.

    ONE LISTING SERVES EVERY CONSUMER. The listing is the whole cost of a pass
    that finds nothing: a getdents over every cursor, lock and state file
    beside the logs (51,196 entries for 503 logs on the live bus, 86 ms of CPU
    each), and it is the same answer for every seat. So it is kept in one file
    in the chat dir's state family, keyed by the room-set token it was listed
    under and stamped with when. A pass whose token matches and whose stamp is
    younger than NAMES_MAX_AGE_S reads that file instead of the directory.
    The caller reads the token BEFORE the listing it keys, so a log created
    between the two is listed now and listed once more, never missed (the
    order web_sse._chat_fingerprint keeps).

    NOT AN INDEX. A room is still born by its first post, and nothing but the
    directory says which rooms exist: this is that answer, remembered for a
    bounded time. A reader that cannot read the file lists; a writer that
    cannot write it costs the next reader one listing. An unreadable token is
    a fresh random one on every read (`chat.rooms_generation`), so it never
    matches and every call lists."""
    token = chat.rooms_generation() if token is None else token
    now = time.time() if now is None else now
    key = token.hex()
    path = chat.state_path(NAMES_FAMILY, NAMES_FILE)
    held = pk.read_json(path, None)
    if _fresh(held, key, now):
        return key, held["at"], list(held["names"])
    names = chat.list_rooms()
    if os.path.isdir(chat.chat_dir()):
        try:
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            pk.write_json(path, {"v": 1, "token": key, "at": now,
                                 "names": names})
        except OSError:
            pass
    return key, now, names


def _fresh(held, key, now):
    """Is `held` a listing made under token `key`, young enough at `now`?"""
    if not isinstance(held, dict) or held.get("token") != key:
        return False
    at, names = held.get("at"), held.get("names")
    return (isinstance(at, (int, float)) and not isinstance(at, bool)
            and 0 <= now - at < NAMES_MAX_AGE_S
            and isinstance(names, list)
            and all(isinstance(n, str) for n in names))


class QuietRooms:
    """One consumer's standing proof that its next pass would find nothing.

    WHAT IS PROVED, ROOM BY ROOM. A room is quiet for this consumer at a
    stamp of its log (st_mtime_ns, size, inode) when, after a pass that found
    nothing, the consumer's own cursor stood at the end of that log -- the
    test `_room_dirty` makes -- or the log was provably absent. Every append
    grows the size and every rotation or restore brings a new inode, so while
    each room's stamp is the one it was proved quiet at, there is no row the
    proof has not seen, and a pass could deliver nothing. That pass is
    skipped: no listing, no roster read, no rotation, no cursor read.

    THE STAMP IS TAKEN BEFORE THE PASS AND THE PROOF AFTER IT. A row appended
    during the pass moves the stamp off the one recorded, so the next poll
    runs a pass: the race costs one pass, never a row.

    ONLY A PASS THAT FOUND NOTHING PROVES ANYTHING. A pass that delivers
    stops at its first event and leaves later rows pending, so it records
    nothing and the room it hit loses its proof. A room no pass could prove
    -- a cursor behind its log, a cursor not minted yet, a log that cannot be
    statted -- keeps every later poll running a full pass until the rotation
    has read it.

    WHAT IT DOES NOT SEE, so no one reads more into a skip: a cursor moved
    BACKWARD with no append (a seat renamed away, a reaped cursor) is not
    looked at again until something is appended, which is the next time
    there is anything to deliver. Nothing in delivery moves a live
    consumer's cursor back.

    `remember=False` is a consumer that lives for one pass, the tool-boundary
    hook: it keeps no proof and never skips, and only borrows the shared
    listing (`room_names`). `clock` is the wall clock, injectable for tests.
    """

    def __init__(self, remember=True, clock=time.time):
        self.remember = remember
        self.clock = clock
        self.clean = {}         # room -> the stamp it was proved quiet at
        self.checked = {}       # room -> the stamp its last verdict was taken at
        self._names = None      # (token key, listed_at, names)

    def room_names(self):
        """The listing, asked of `room_names` only when the room-set token
        moved or the listing this consumer holds has aged past the bound."""
        token = chat.rooms_generation()
        now = self.clock()
        held = self._names
        if held is None or held[0] != token.hex() \
                or not 0 <= now - held[1] < NAMES_MAX_AGE_S:
            held = self._names = room_names(token, now)
        return list(held[2])

    def snapshot(self, names, seat=None, room="main"):
        """{room: stamp} over every room a pass could look at: the listing,
        the primary room and the seat's DM lane, which are the pins
        `_scan_rooms` adds that a listing may not name (home and main are
        listed whenever their logs exist)."""
        rooms = set(names)
        rooms.add(room)
        if seat:
            rooms.add(dm_lane(seat))
        return {r: _log_stamp(r) for r in rooms}

    def quiet(self, snap):
        """Could a pass over the rooms of `snap` deliver nothing?"""
        return self.remember and bool(snap) and all(
            stamp is not None and self.clean.get(room) == stamp
            for room, stamp in snap.items())

    def settle(self, snap, dirty, looked=()):
        """Keep the proof a pass that found nothing leaves: each room of
        `snap` whose log is absent, or whose cursor `dirty(room)` finds at the
        end of its log.

        ASKED ONLY WHERE THE ANSWER CAN HAVE MOVED. A room is asked again when
        its stamp moved since its last verdict, or when this pass handed it
        out (`looked`), which is when this consumer's cursor on it can move --
        a proof included: a pass that found a proved room dirty (a cursor
        moved back) and did not deliver from it must not leave that proof
        standing, or every later poll skips a room it has just seen hold rows.
        Any other room keeps the verdict it had: a proof still stands at the
        same stamp, and a room still unproved stays unproved until the
        rotation hands it out. So the first pass reads every room's cursor
        once, and each pass after reads only the rooms that moved and the
        slice it scanned, never the whole estate again -- a consumer whose
        rooms never prove quiet (delivery switched off, a room that cannot be
        statted) pays no more per pass than the scan it already makes."""
        if not self.remember:
            return
        looked = set(looked)
        clean, checked = {}, {}
        for room, stamp in snap.items():
            if stamp is None:
                continue
            checked[room] = stamp
            if stamp == ABSENT or (self.clean.get(room) == stamp
                                   and room not in looked):
                clean[room] = stamp
                continue
            if self.checked.get(room) == stamp and room not in looked:
                continue                # unproved at this stamp, not re-read
            try:
                if not dirty(room):
                    clean[room] = stamp
            except Exception:           # noqa: BLE001 -- unproved is looked at
                continue
        self.clean, self.checked = clean, checked

    def forget(self, room):
        """The room a pass delivered from is not proved quiet, and its next
        verdict is taken afresh."""
        self.clean.pop(room, None)
        self.checked.pop(room, None)
