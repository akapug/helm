"""Stop-signal fingerprints and the per-guard off switch.

THE SHARED BASE the stop signals stand on, and the reason it is its own file:
`seats_stop_signals` is a facade under a shrinking ratchet, and BOTH halves
inside it — the beacon obligation and the spiral gate — reach for these same
five helpers. Leaving them in the facade meant neither half could ever be
extracted without either dragging its siblings along or importing back through
the file it was leaving, which is a cycle. Lifting the base out first is what
makes those extractions possible, and it is why this file has no opinion about
beacons or spirals: it only answers WHERE a fingerprint lives, WHAT a set of
rows hashes to, WHICH rows are pending, and WHETHER a guard is switched off.

Nothing here reads seat identity or decides anything. `seats_stop_signals`
re-exports all five, so `seats.py` and `seats_delegation` keep their existing
import path and no caller had to change.
"""
import hashlib
import os

from . import chat, home, pk
from .seats_common import _seat_key
from .seats_cursor import (_cursor_transaction_epoch, _occurrence, _sid8,
                           cursor_path)
from .seats_delivery import _cursor, _room_dirty, _say, _scan_rooms, _tail
from .seats_identity import deliverable, seat_scope


def _stop_fp_path(room, seat, session=None, kind="stopfp"):
    """The once-per-fingerprint latch — in the room dir, per (seat, session)
    like the cursor it gates (RAM-side, dies with the boot like the rest of
    the lane's state). kind names the latch lane: stopfp (the inbox block),
    stopwhisper (the contextual-continuation lane's fired-set)."""
    p = os.path.join(chat.chat_dir(),
                     "%s.%s.%s" % (pk.slug(room), kind, _seat_key(seat)))
    s8 = _sid8(session)
    return "%s.%s" % (p, s8) if s8 else p
def _rows_fp(pending):
    """The pending-set fingerprint — blake2b over (room, row-id), the ONE
    identity both the inbox block and the stop-whisper's unlanded leg latch
    on (they must agree on what 'the same rows' means)."""
    import hashlib
    return hashlib.blake2b(
        "|".join("%s:%s" % (rm, r.get("id") or chat.rkey(r))
                 for rm, r in pending).encode("utf-8"),
        digest_size=16).hexdigest()
def _room_epoch(room, seat, session):
    """The room's cursor-transaction seqlock token and whether it is pending.

    Every production cursor write in a room commits through ONE room-scoped
    journal whose token changes on each commit, so two equal non-pending
    readings bracket an interval in which no cursor of that room moved."""
    return _cursor_transaction_epoch(cursor_path(room, seat, session))


def _occurrence_owed(row, start, end, dev, ino, wake, held, done, seat, room,
                     scope, ambient, beacon):
    """Is the row at (dev, ino, start, end) still pending for this consumer?

    ONE PREDICATE for the scan and for the validation of an act taken on one
    of its rows. The wake cursor suppresses what a wake already crossed, its
    `done` set what was consumed out of order, and `deliverable` decides the
    tier; a second spelling of this conjunction is a second answer to the
    same question."""
    token = _occurrence(dev, ino, start)
    same = isinstance(wake, dict) and \
        (wake.get("dev"), wake.get("ino")) == (dev, ino)
    crossed = same and end <= wake.get("off", 0) and token not in held
    return not crossed and token not in done \
        and deliverable(row, seat, room, scope, ambient=ambient, beacon=beacon)


def _pending_rows(room, seat, session=None, backfill=False, scope=None,
                  ambient=True, beacon=False, report=None):
    """Pure exact-session pending rows, suppressing occurrences wake crossed.

    `ambient`/`beacon` are `deliverable`'s two tiers and they decide WHICH
    QUESTION this answers. The default pair is the tool-boundary one — what
    would render for this seat inside a running turn. `ambient=False,
    beacon=True` is the IDLE BEACON's question — what would have WOKEN it —
    and the two differ by exactly the tier a seat is entitled to ignore:
    plain home-room chatter and muted rooms.

    A report carrying an `occurrences` list is filled with the PHYSICAL
    occurrence of every pending row -- room identity, byte range, the exact
    bytes, and the two cursor values the answer was computed from -- plus
    `coherent`, which says whether the room's cursor seqlock stayed still
    across the cursor reads and the room read. Those are the inputs a later
    act must re-validate, bound during the observation that produced them."""
    watch = isinstance(report, dict) and isinstance(
        report.get("occurrences"), list)
    before = _room_epoch(room, seat, session) if watch else None
    # WHY THE CURSOR'S REASON IS ASKED FOR. An UNREADABLE cursor must not reach
    # the same `{"off": 0}` as an ABSENT one: replaying a room from zero on a
    # cursor helm cannot trust brings back rows this seat already consumed
    # -- as PENDING, which is positive overdue evidence, which raises an alarm
    # and types into a pane about an obligation that was already met. Absence
    # still replays, because a room this seat never looked at genuinely starts
    # at zero. An unreadable cursor now yields no rows AND says so, so a caller
    # reading nothing here cannot mistake it for a room with nothing in it.
    curmark = {}
    cur = _cursor(room, seat, session, report=curmark)
    held_cursor = cur
    if cur is None:
        if curmark.get("outcome") == "unreadable":
            _say(report, "unreadable",
                 "the consumption cursor could not be trusted (%s)"
                 % curmark.get("detail"))
            return []
        if session is None and not backfill:
            _say(report, "read", None)
            return []
        cur = {"off": 0}
    if watch:
        report["window"] = None
    got = _tail(room, cur, report=report)
    if not got:
        return []
    dev, ino, _base, entries, _rid, _skip = got
    wakemark = {}
    wake = _cursor(room, seat, session, beacon=True, report=wakemark)
    if wake is None and wakemark.get("outcome") == "unreadable":
        # THE OTHER CURSOR, AND IT FAILS THE OTHER WAY. The wake cursor is what
        # SUPPRESSES rows a wake already crossed; losing it does not hide rows,
        # it un-hides consumed ones. Same finding, opposite direction, so the
        # rows still compute (a caller may need them) but the coverage says the
        # suppression was not applied and absence-of-suppression is not proof.
        _say(report, "unreadable",
             "the wake cursor could not be trusted (%s), so rows a wake "
             "already crossed are not suppressed in this sample"
             % wakemark.get("detail"))
    held = set((wake or {}).get("held") or ())
    done = set((wake or {}).get("done") or ())
    sc = scope if scope is not None else seat_scope(seat)
    window = report.get("window") if watch else None
    # REPLAY IS NOT A PLAIN READ. A present cursor that had consumed into
    # ANOTHER inode -- an offset, a last row id or a pending skip -- makes
    # `_tail` restart at zero and suppress by row id, so byte positions in this
    # window are not positions the cursor ever vouched for. A cursor that never
    # consumed anything vouches for the room from zero, whatever it names.
    replay = held_cursor is not None and \
        (held_cursor.get("dev"), held_cursor.get("ino")) != (dev, ino) and \
        bool(held_cursor.get("off") or held_cursor.get("rid")
             or held_cursor.get("skip"))
    out = []
    for row, start, end in entries:
        if row is None:
            continue
        if _occurrence_owed(row, start, end, dev, ino, wake, held, done,
                            seat, room, sc, ambient, beacon):
            out.append(row)
            if watch and window is not None:
                woff, data = window[2], window[3]
                report["occurrences"].append({
                    "room": room, "row": row, "start": start, "end": end,
                    "dev": dev, "ino": ino,
                    "bytes": data[start - woff:end - woff],
                    "cursor": held_cursor, "wake": wake, "replay": replay})
    if watch:
        after = _room_epoch(room, seat, session)
        report["coherent"] = (before == after and not before[1]
                              and not after[1])
    return out
def _pending_all(room, seat, session=None, scan_lane="pending",
                 ambient=True, beacon=False, coverage=None, scope=None):
    """[(room, row)] pending across one lane's bounded room scan, cursors
    untouched. Delivery, stop-guard, and roster observation own independent
    identity queues so one consumer cannot steal another's eventual coverage.
    The tracked/backfill law and one-roster-read scope match deliver_any.

    `coverage`, when a dict is passed, is FILLED with `{room: (outcome, detail)}`
    for every room this pass ATTEMPTED -- read, truncated or unreadable -- and
    that is a different set from the rooms in the result. THE RESULT IS HITS.
    A room successfully scanned and found empty contributes NOTHING to the
    return value and vanishes from any caller that reconstructs "what did I
    look at" from it, which is task/2463 finding 1 exactly: consume the sole
    pending row and the owed room disappears from `scanned`, so the standing
    alarm it was raised about can never be cleared by the very pass that proves
    it drained. Coverage is the answer to WHAT COULD HAVE BEEN FOUND, and only
    a caller holding it can tell an empty room from an unvisited one.

    A coverage dict carrying an `occurrences` dict also receives, per room
    read the expensive way, `{"coherent": bool, "list": [occurrence, ...]}`
    from `_pending_rows`. `scope`, when given, is the seat scope the caller
    already derived from a roster read it trusts, so eligibility is decided
    against that read and not a second fail-open one."""
    sc = scope if scope is not None else seat_scope(seat)
    tracked = sc["tracked"] or _cursor(room, seat, session) is not None
    cov = coverage if isinstance(coverage, dict) else None
    mark = cov.setdefault("rooms", {}) if cov is not None else None
    occ = cov.get("occurrences") if cov is not None else None
    occ = occ if isinstance(occ, dict) else None
    estate = {}
    out = []
    for r in _scan_rooms(
            room, seat=seat, scope=sc, session=session,
            scan_lane=scan_lane, report=estate):
        if not _room_dirty(r, seat, session):
            # A CLEAN PRECHECK IS AN OBSERVATION, NOT A SKIP. `_room_dirty` is
            # False only for a room whose cursor reads, whose identity matches
            # and whose size equals the offset, or whose file is absent -- both
            # are "nothing sits past this cursor". Every failure to look
            # (missing cursor, unreadable stat) returns True and takes the
            # expensive path, so this branch is a proven-empty room.
            if mark is not None:
                mark[r] = ("read", None)
            continue
        room_report = {"occurrences": []} if occ is not None else {}
        rows = _pending_rows(
            r, seat, session, scope=sc, ambient=ambient, beacon=beacon,
            backfill=(tracked and r != room) or r.startswith(chat.DM_PREFIX),
            report=room_report)
        if mark is not None:
            mark[r] = (room_report.get("outcome") or "read",
                       room_report.get("detail"))
        if occ is not None:
            occ[r] = {"coherent": bool(room_report.get("coherent")),
                      "list": room_report.get("occurrences") or []}
        out.extend((r, row) for row in rows)
    if cov is not None:
        # WHETHER THE ESTATE WAS SEEN WHOLE, beside how each room read. A
        # caller holding per-room outcomes for every room it was HANDED cannot
        # tell a complete estate from a bounded slice of a larger one.
        cov["estate"] = estate.get("estate") or ("complete", None)
    return out
#: How many times a validation re-reads a room's two cursors when a commit
#: lands between its bracketing seqlock reads. Bounded and lockless: a room
#: whose cursors keep moving is UNKNOWN, never waited on.
VALIDATE_READS = 3


def occurrence_owed(seat, session, occ, scope, ambient=False, beacon=True):
    """(True | False | None, why) -- does the occurrence an act was prepared
    on STILL stand, by the inputs the preparation actually used?

    NON-BLOCKING AND LOCKLESS, because it runs between a composer proof and
    the keystroke that proof licenses: a blocking read there would make the
    proof stale by the time the key is pressed. It asks, in order:

      * the two cursors, read under the room's own seqlock and compared BY
        VALUE with the ones the preparation computed from. Consumption is
        monotone -- an offset only advances within an inode, `done` only
        grows, a remap changes the inode -- so an equal cursor proves this
        exact seat and session consumed nothing since. The room token itself
        is not the key: any seat's commit in the room moves it.
      * the room file, opened once, fstat'd, and the exact bytes at
        [start, end) read from that one fd. A keyed-append rollback, a
        rotation and a journal restore can each remove or replace the row
        WITHOUT this seat's cursor moving; an inode, size and mtime triple
        cannot see a rollback that a later append refilled, and bytes can.
      * the pending predicate, recomputed on the unchanged row with the scope
        derived from the caller's current roster read.

    False names what moved and means the preparation is stale; None means a
    read could not be taken coherently, which authorizes nothing.

    THE SEQLOCK BRACKET COVERS THE BYTES. The room's epoch is read before the
    two cursors and again AFTER the room read, so a delivery that consumes the
    row while its bytes are being read -- the bytes stay, the cursors move --
    lands inside the bracket and the observation is taken again, instead of
    comparing bytes that outlived their consumption against cursors read
    before it."""
    room = occ.get("room")
    try:
        for _n in range(VALIDATE_READS):
            before = _room_epoch(room, seat, session)
            curmark, wakemark = {}, {}
            cur = _cursor(room, seat, session, report=curmark)
            wake = _cursor(room, seat, session, beacon=True, report=wakemark)
            seen = _occurrence_bytes(room, occ)
            after = _room_epoch(room, seat, session)
            if before == after and not before[1] and not after[1]:
                break
        else:
            return None, ("the cursors of %s kept moving across %d lockless "
                          "reads" % (room, VALIDATE_READS))
        for mark in (curmark, wakemark):
            if mark.get("outcome") == "unreadable":
                return None, ("a cursor for %s could not be read (%s)"
                              % (room, mark.get("detail")))
        if cur != occ.get("cursor"):
            return False, ("this seat's delivery cursor for %s moved" % room)
        if wake != occ.get("wake"):
            return False, ("this seat's wake cursor for %s moved" % room)
        if str(room).startswith(chat.DM_PREFIX) and \
                chat._dm_redirect(room) is not None:
            return False, "the DM lane %s was redirected" % room
        moved, got = seen
        if moved:
            return False, moved
        if got != occ.get("bytes"):
            return False, ("the bytes of the owed row in %s changed without a "
                           "cursor advance" % room)
        held = set((wake or {}).get("held") or ())
        done = set((wake or {}).get("done") or ())
        if not _occurrence_owed(occ.get("row"), occ.get("start"),
                                occ.get("end"), occ.get("dev"),
                                occ.get("ino"), wake, held, done, seat, room,
                                scope, ambient, beacon):
            return False, ("the row in %s is no longer addressed to this seat "
                           "under its current scope" % room)
        return True, ""
    except Exception as exc:                 # noqa: BLE001 — a read that
        return None, ("the validation of %s could not be taken (%s)"   # failed
                      % (room, exc.__class__.__name__))                # proves nothing


def _occurrence_bytes(room, occ):
    """(what moved or "", the bytes at the occurrence's range) from ONE open
    of the room file: its identity, its size, and a pread from that fd."""
    start, end = occ.get("start"), occ.get("end")
    try:
        fd = os.open(chat.room_path(room), os.O_RDONLY)
    except FileNotFoundError:
        return "the room %s no longer exists" % room, None
    try:
        st = os.fstat(fd)
        if (st.st_dev, st.st_ino) != (occ.get("dev"), occ.get("ino")):
            return ("the room %s was replaced (rotated or restored), so the "
                    "owed row is not where it was" % room), None
        if st.st_size < end:
            return ("the owed row in %s was cut from the file without a "
                    "cursor advance" % room), None
        return "", os.pread(fd, end - start, start)
    finally:
        os.close(fd)


def _off(name):
    return (home.env(name) or "").lower() in ("0", "off", "no")
