#!/usr/bin/env python3
"""THE BEACON'S DOORBELL: one line per burst of addressed rows, never one per row.

WHY. A line on a beacon's stdout is a Monitor event, and a Monitor event is a
NEW USER TURN for the seat that armed it: the turn carries the line and runs
the seat's UserPromptSubmit inject hook as well. A beacon that streams each
addressed row as its own line therefore charges N turns of context for N rows,
and a seat that resumes after a pause receives its whole backlog as one burst
of turns. A seat with a small context window overflows on it. The seat must
still be able to catch up, but at its own pace, by PULLING from the read path.

THE CONTRACT. It is level-triggered, and each clause is one arm in
tests/test_beacon_doorbell.py:

  * While the seat has unread rows in the beacon's match set (DMs, @mentions,
    replies, @all, reactions to its rows; ambient home-room rows stay out
    unless the beacon was armed with --ambient), the waiter emits ONE ring.
    Then it stays silent until a NEW matching row lands, or until the
    backstop below.
  * A new row opens a debounce window (HELM_BEACON_DEBOUNCE_S, 60 s by
    default). Every row that lands inside the window joins the next ring.
  * When the seat READS, that is, its delivery cursor passes the rows a ring
    announced, those rows leave the counts. Reading never rings. A pull the
    seat runs, `helm chat read`, reads the rows it printed in full
    (helm.pull_delivery), including a printed row past one it still owes,
    whose hold the pull releases (`_released`). A row recorded before
    entries carried `start` takes its start from the room file.
  * A row the seat ACKS (`helm chat ack <id>`) leaves the counts at once,
    rung or not: the seat saw it. Another seat's ack of the same row does
    not count (tests/test_doorbell_release.py). The tool-boundary hook and
    the stop guard read the same ack (`_acks`), for their own SESSION, in
    the window of rows they already read: the hook passes the row without
    showing it and drops its hold, and the guard does not count it. An ack
    from a delegate, from a co-named sibling session, recording no session,
    placed before its row, or past that window leaves the row shown and
    counted there, and only the ring, which is per seat, drops it.
  * A ring is not a delivery. The rows it announces stay owed to the
    tool-boundary hook, which shows each one in full at the seat's own tool
    boundaries, so the delivery cursor passes a row only once the seat was
    shown it or acked it.
  * Arming with rows pending rings ONCE at once: the catch-up ring.
  * An owner row or a DM rings at once, and that ring carries every row that
    is pending.
  * Rings are capped per seat per hour (HELM_BEACON_RINGS_PER_H, 12 by
    default). Past the cap the counts accumulate, and the next ring that is
    allowed carries them. An owner row, a DM and the catch-up ring bypass the
    cap, and a ring that bypasses it does not count toward it.
  * THE BACKSTOP: while rows a ring announced stay unread, the waiter rings
    again once every HELM_BEACON_BACKSTOP_S (720 s by default) with no new
    row, so a ring that lands mid-turn and is dropped is not the last one. A
    backstop ring counts against the hourly cap. An idle waiter reaches it
    from a time it keeps in memory and takes the state lock only then.

A RING COSTS THE WOKEN SEAT ONE CALL WHEN THERE IS ONE THING TO DO. Measured
over three days of the fleet: a wake ran a median of 5 and a mean of 12 model
calls to the end of its turn, each re-sending the seat's whole context. One
ring led with a two-day-old row whose dispatch row was already cancelled,
carried only its first line, and named one pull per room; the seat ran eight
calls hunting and found nothing to do. So the ring leads with the row to act
on, carries that row whole, releases a row whose referent closed, and names
ONE pull for exactly the rows to act on.

A RING IS ONE LINE with the SHAPE OF A WAKE LINE: the lead row's wake header
and author, that row's WHOLE body on the one line (`_body`: its line breaks
shown as ` ⏎ `, LEAD_CHARS characters at most, a longer body cut with
`(cut, N more chars: helm chat read --id ID)`), the owed clause when the seat
owes dispatch rows (the one the per-row wake line carries,
`seats_stop_owed.owed_clause`), then the `(+N waiting — helm chat read ...)`
tail that every consumer already strips as fixed text (promptshape, moments,
relevance). The counts and the exact pull command ride inside that tail, and
the tail is always last. So a reader of wake lines needs no second grammar,
and a ring never carries the text of more than one chat row. THE PULL IS
ONE COMMAND (`_pulls`): `helm chat read --id ID,ID,...` naming exactly the
rows the seat must act on (the owner's, DMs, rows addressed to it), the lead
first and then the newest, PULL_IDS at most, the older ones pulled per room
behind it. The rows it need not act on (@all, reactions, room rows) are only
counted, `N not addressed: read when idle`, and a ring of those alone names
the room pulls (`_room_pulls`). The tail's two
sentences of instruction (how rows clear, when it rings again) are said once
per waiter process: its first ring says them, a later ring carries only the
facts (`ring_line`'s `told`), and a restarted waiter says them again. A
sentence counts as said once its ring reached the stream: a ring whose write
raised told the seat nothing, so its retry says them all. The waiter outlives
the context it told, so it says them all again on every RESAY_EVERY-th ring
after its first, and on the first ring after its seat's context started over
(`context_epoch`: a compaction of the seat's own thread, or a new session).

THE LEAD IS THE MOST ACTIONABLE UNREAD ROW, not the oldest and not merely
the newest (`_lead`). While a row the seat must act on is unread (the
owner's, a DM, a row addressed to it), an @all row, a reaction or a room row
never leads; the new rows lead over the rows a ring already announced. Among
them the most direct class leads, and within it the newest row: an owner
row; then a DM, then an @mention or reply, then an @all, each a person's or
a seat's before a subsystem label's (machine_senders); then reactions and
room rows, a person or a seat first. A seat's Monitor filter reads one line
per ring now, and a ring counts rows its lead does not show: a ring can lead
with a watcher's mention while a person's @all is newer. So a filter keyed
on row text (keep only lines naming the seat, drop lines from watchers) must
not drop ring lines.

A ROW WHOSE REFERENT CLOSED DOES NOT COST A CALL (`_release_closed`). A row
addressed to the seat, or a DM, that names a dispatch row (a 12-40 hex id the
ledger resolves, handed to this seat) or a task (`task/N` or a slug id,
read whole by the task module's id reader), every one of them terminal
(cancelled, verdicted, closed, landed), is released when a ring is due, the
way an ack releases it: it leaves the counts and never leads. The state
keeps a trace of each (`closed`: the row id, its room, what closed it), and
the next ring says `N already closed`. A ring left with nothing due is not
rung. A referent that cannot be read (a ledger that will not open, an
ambiguous prefix, a dispatch row whose fold met an event kind this helm
cannot read, a task ledger with a corrupt row, a task the ledger does not
hold) leaves the row live: the
ring fails open for delivery and never hides what it cannot read. A row the
seat SENT stays live though closed, since a FIX on it is the seat's work. A
release is the ring's alone: the tool-boundary hook still shows the row.

DELIVERY IS NOT REIMPLEMENTED HERE. The waiter still drains through
`deliver_any` on the beacon channel, one event per call, which keeps every
property of that path: the cursor commits before the effect, the dead-sink
fence, the mute and owner-rail holds, the receipts. The drain hands its
events to a collector instead of the Monitor pipe, and it passes
`announce_only=True`: every row it wakes joins the wake cursor's `held` set,
the mechanism the mute and owner-rail holds use, so the hook still owes it.
The per-row stream passes nothing, because its wake line IS the row. Each
event is a `WakeLine`, the same text that the per-row stream prints, which
also carries the room, the rows and the cursor position that the event
committed past.

THE STATE IS DURABLE, because a drained row is past the wake cursor: no later
drain sees it again, and only this state can ring for it. The rows that were
drained and not yet announced live in one file per seat under the helm home,
so a waiter that dies inside a debounce window, or while the cap holds,
leaves them to the next waiter that arms. That waiter rings for them at
once. The ring history lives in the same file, so the hourly cap survives the
30-minute re-arm. State is keyed by seat, not by session, so the rows carry
across a /clear. A seat RENAME starts a new file under the new key; the old
key's rows are not carried over.

`helm chat wait --follow --per-row` keeps the stream that the doorbell
replaced, byte for byte.
"""
import collections
import json
import math
import os
import re
import sys
import time

from . import chat, home, pk
from .seats_common import (_BROADCAST, _clip, _flocked, _scrub, _seat_key,
                           recipient_matches)
from .seats_cursor import _sid8

DEBOUNCE_S = 60.0
RINGS_PER_H = 12
BACKSTOP_S = 720.0
HOUR_S = 3600.0
FIRST_LINE_CHARS = 160
#: Wake events one poll may drain. A backlog larger than this rings once the
#: drain has finished, never partway through it.
DRAIN_PASS = 256
#: The unread events one seat's state keeps per list. Beyond it the oldest go
#: first, and the ring counts only what was kept.
KEEP = 500
#: Rooms a ring names a pull command for; any further rooms are counted.
PULL_ROOMS = 3
#: The lead row's body a ring carries whole, in characters. A longer body is
#: cut there, and the cut names the read that prints the row whole.
LEAD_CHARS = 1200
#: The rows the ring's one pull names by id, newest first. Older addressed
#: rows are pulled per room behind it.
PULL_IDS = 24
#: The referents one row may name for the closed check (`_closed`). A row
#: naming more is never released as closed.
REFS_KEPT = 8
#: A waiter says every sentence of instruction again on each RESAY_EVERY-th
#: ring after its first (rings 1, 11, 21, ...): what it told lives in the
#: waiter process, which outlives the seat's context.
RESAY_EVERY = 10
#: How far back from an event's end `_row_start` reads for the start of an
#: entry recorded without one. An entry whose row begins further back stays.
BACK_BYTES = 256 * 1024

_TAIL = re.compile(r"\s*\(\+\d+ waiting — helm chat read[^)]*\)$")
#: A dispatch row id as a chat row names it (the ledger prints 12 characters),
#: and a task id. A hex token the ledger does not resolve names no referent.
#: A task token is taken whole, slug and range included (`task/12-fix`,
#: `task/1342-1344`), and read by the task module's own id reader (`_refs`),
#: so it is never read as its numeric prefix.
_DISPATCH_REF = re.compile(r"(?<![0-9A-Za-z])[0-9a-f]{12,40}(?![0-9A-Za-z])")
_TASK_REF = re.compile(
    r"(?<![0-9A-Za-z])task/[0-9A-Za-z][0-9A-Za-z-]{0,63}(?![0-9A-Za-z-])")
#: What a row's line breaks become in the one line of a ring.
_BREAK = " ⏎ "
#: A row id the ring's pull may name: nothing that could end the tail.
_PULL_ID = re.compile(r"[0-9A-Za-z_-]{1,64}")


class WakeLine(str):
    """A wake line that IS still the line, plus the delivery event behind it.

    Every sink prints it, appends it and JSON-encodes it exactly as it does the
    plain string, so the per-row stream is byte-identical. The doorbell also
    reads `room`, `rows` (a reaction burst is grouped into one event), `at`,
    the (dev, ino, end) position that the wake cursor committed past, and
    `start`, where the event's first row begins in that file."""

    def __new__(cls, line, room, rows, at, start=None):
        out = super().__new__(cls, line)
        out.room, out.rows, out.at = room, list(rows), tuple(at)
        out.start = start
        return out


def _now():
    return time.time()


def _setting(name, default, floor):
    """A positive knob from HELM_<name>. A value that does not parse falls back
    to the default, and a value below `floor` is raised to it."""
    raw = os.environ.get("HELM_" + name)
    try:
        value = float(raw) if raw not in (None, "") else default
    except ValueError:
        return default
    return max(floor, value) if math.isfinite(value) else default


def debounce_s():
    return _setting("BEACON_DEBOUNCE_S", DEBOUNCE_S, 0.0)


def rings_per_h():
    return int(_setting("BEACON_RINGS_PER_H", RINGS_PER_H, 1))


def backstop_s():
    return _setting("BEACON_BACKSTOP_S", BACKSTOP_S, 60.0)


def state_dir():
    return os.path.join(home.global_dir(), ".state", "beacon-doorbell")


def state_path(seat):
    return os.path.join(state_dir(), _seat_key(seat) + ".json")


_EVENT_KEYS = ("key", "room", "at", "kind", "owner", "id")


def _fresh():
    """`last` is when the seat was last rung, for the backstop; `rings` holds
    only the rings the hourly cap counts. `closed` is the trace of the rows
    released because their referent had closed (`_release_closed`)."""
    return {"rings": [], "since": None, "last": None, "unrung": [], "owed": [],
            "closed": []}


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _entry(e):
    """One kept event: its `lead` and `ts` only as strings, its `refs` only as
    a list of strings."""
    out = {k: v for k, v in e.items()
           if k not in ("lead", "ts") or isinstance(v, str)}
    refs = out.get("refs")
    if not (isinstance(refs, list) and all(isinstance(r, str) for r in refs)):
        out.pop("refs", None)
    return out


def _load(path):
    """The seat's state. A file that is missing or does not parse is the empty
    state, and a malformed entry is dropped without affecting the others. An
    event's `lead` and `ts` are kept only as strings; an event without them
    still counts and cannot lead. An event whose `refs` is malformed keeps no
    refs, so it is never released as closed."""
    got = pk.read_json(path, None)
    st = _fresh()
    if not isinstance(got, dict):
        return st
    st["rings"] = [t for t in got.get("rings") or () if _number(t)]
    for name in ("since", "last"):
        value = got.get(name)
        st[name] = value if _number(value) else None
    for name in ("unrung", "owed"):
        st[name] = [_entry(e) for e in got.get(name) or ()
                    if isinstance(e, dict) and all(k in e for k in _EVENT_KEYS)
                    and isinstance(e["at"], list) and len(e["at"]) == 3]
    st["closed"] = [c for c in got.get("closed") or ()
                    if isinstance(c, dict) and _number(c.get("t"))]
    return st


def _save(path, st):
    pk.atomic_write(path, json.dumps(st, ensure_ascii=False,
                                     separators=(",", ":")) + "\n")


def _chars(text):
    text = text.strip()
    return text if len(text) <= FIRST_LINE_CHARS \
        else text[:FIRST_LINE_CHARS].rstrip() + "…"


def _body(text, rid):
    """A row's WHOLE body on one line, its line breaks shown as ` ⏎ `, up to
    LEAD_CHARS characters. A longer body is cut there and says how much was
    cut and the read that prints the row whole, so the seat can act on the
    lead without a read call and knows when it cannot."""
    body = _BREAK.join(part for part in (_scrub(x).strip()
                                         for x in text.split("\n")) if part)
    if len(body) <= LEAD_CHARS:
        return body
    rid = _scrub(str(rid or ""))
    return "%s (cut, %d more chars%s)" % (
        body[:LEAD_CHARS].rstrip(), len(body) - LEAD_CHARS,
        ": helm chat read --id %s" % rid if _PULL_ID.fullmatch(rid) else "")


def _headline(ev):
    """The ring's lead: the event's own wake header and author, as delivery
    rendered them, then the row's whole body (`_body`). A reaction keeps its
    own rendered body instead."""
    line = str(ev)
    head = line[:line.find("] ") + 2] if "] " in line else ""
    row = ev.rows[0]
    text = str(row.get("text") or "")
    rendered = _clip(_scrub(text)) if text and not row.get("react") else ""
    cut = line.find(": " + rendered, len(head)) if rendered else -1
    if cut < 0:
        return head + _chars(_TAIL.sub("", line[len(head):]))
    return line[:cut + 2] + _body(text, row.get("id"))


def _refs(text):
    """The referents a row's text names, each once: dispatch row ids (hex
    tokens of 12 to 40 characters) and task ids, each as `tasks.normalize_id`
    spells it (a token it cannot parse names no task). None when it names
    more than REFS_KEPT, or when the task id reader cannot be loaded, so a
    row whose referents were not all kept is never released as closed."""
    tids = set()
    for token in _TASK_REF.findall(text):
        try:
            from . import tasks
            tid = tasks.normalize_id(token)
        except Exception:                               # noqa: BLE001
            return None
        if tid:
            tids.add(tid)
    refs = sorted(set(_DISPATCH_REF.findall(text)) | tids)
    return refs if len(refs) <= REFS_KEPT else None


#: The lead's class order; `_rank` puts the owner before all of them, and a
#: person or a seat before a subsystem label within each of the first three
#: and within the last two together.
_CLASS = {"direct": 0, "addressed": 1, "all": 2, "react": 3, "room": 4}
_AUTHOR = re.compile(r"^\[helm chat[^\]]*\] ([^:\s]{1,80}):")


def _rank(kind, owner, lead):
    """A sortable class for the ring's lead: owner rows first; then DMs,
    then the rows addressed to the seat, then @all, each a person or a seat
    before a subsystem label; then reactions and room rows, a person or a
    seat first. The author is read from the lead as delivery rendered it,
    never from the row."""
    from .machine_senders import is_machine
    m = _AUTHOR.match(lead)
    machine = bool(m and is_machine(m.group(1)))
    cls = _CLASS.get(kind, 4)
    return (0 if owner else 1, min(cls, _CLASS["react"]), machine, cls)


def _actionable(e):
    """A row the seat has to act on: the owner's, a DM, or one addressed to
    it. @all rows, reactions and room rows are read when idle."""
    return bool(e["owner"]) or e["kind"] in ("direct", "addressed")


def _kind(row, room, seat, names):
    """Which count one event joins. The DM lane is decided by its room, so
    that no identity field of the row is read here."""
    if row.get("react"):
        return "react"
    if room.startswith(chat.DM_PREFIX):
        return "direct"            # the DM lane; never a row's identity key
    from .seats_catchup import _addressed
    if _addressed(row, seat, names):
        return "addressed"
    if _BROADCAST.search(str(row.get("text") or "")):
        return "all"
    return "room"


def _record(st, events, seat):
    """Fold drained events into `unrung`, oldest first. An event already held
    (another waiter of the same seat drained it too) is counted once. A row
    that cannot be classified still counts, as addressed: every drained event
    is a woken row, so none may leave this function unrecorded."""
    if not events:
        return
    from .machine_senders import owner_rail
    from .seats_address import seat_names
    try:
        names = seat_names(seat)
    except Exception:                                   # noqa: BLE001
        names = [seat]
    held = {e["key"] for e in st["unrung"]} | {e["key"] for e in st["owed"]}
    for ev in events:
        rows = getattr(ev, "rows", None)
        if not rows or len(getattr(ev, "at", ())) != 3:
            continue
        key = "%s|%s|%s|%s" % ((ev.room,) + tuple(ev.at))
        if key in held:
            continue
        held.add(key)
        row = rows[0]
        try:
            kind, owner = _kind(row, ev.room, seat, names), bool(owner_rail(row))
        except Exception:                               # noqa: BLE001
            kind, owner = "addressed", False
        try:
            lead = _headline(ev)
        except Exception:                               # noqa: BLE001
            lead = _chars(_TAIL.sub("", str(ev)))
        st["unrung"].append({"key": key, "room": ev.room, "at": list(ev.at),
                             "kind": kind, "owner": owner, "id": row.get("id"),
                             "ts": str(row.get("ts") or ""), "lead": lead})
        if isinstance(getattr(ev, "start", None), int):
            st["unrung"][-1]["start"] = ev.start
        refs = _refs(str(row.get("text") or "")) \
            if kind in ("addressed", "direct") and not owner else None
        if refs:
            st["unrung"][-1]["refs"] = refs
    st["unrung"] = st["unrung"][-KEEP:]  # noqa: SILENT_CAP — KEEP bounds the state; its comment says counts cover what is kept


def _file_id(room):
    """(dev, ino) of the room's file now; () when it is gone; None unknown."""
    try:
        st = os.stat(chat.room_path(room))
    except FileNotFoundError:
        return ()
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _row_start(room, at, rid):
    """Where an event recorded without `start` (state older than the field)
    begins: the start of its first row, `rid`, read back from the event's end
    `at` on the same file. None when the room's file is another one, the row
    is not within BACK_BYTES of the end, or the file cannot be read."""
    end = at[2]
    if not rid or not isinstance(end, int):
        return None
    try:
        with open(chat.room_path(room), "rb") as f:
            st = os.fstat(f.fileno())
            if (st.st_dev, st.st_ino) != tuple(at[:2]) or st.st_size < end:
                return None
            lo = max(0, end - BACK_BYTES)
            f.seek(lo)
            data = f.read(end - lo)
    except OSError:
        return None
    if not data.endswith(b"\n"):
        return None
    pieces = data[:-1].split(b"\n")
    stop = end - 1                     # where the last piece ends
    # A window that starts mid-file cuts its first piece, so it is not read.
    for k in range(len(pieces) - 1, 0 if lo else -1, -1):
        start = stop - len(pieces[k])
        row = chat._msg(pieces[k].decode("utf-8", "replace"))
        if row is not None and row.get("id") == rid:
            return start
        stop = start - 1
    return None


def _released(wake, e, at):
    """Did a pull release this announced row: the wake cursor passed it on its
    file and holds none of the rows in its byte range [start, end)? The hook
    then passes it without showing it, and the stop guard does not count it.
    A row recorded without its start takes its row's start from the room
    file (`_row_start`); one whose start cannot be found stays."""
    if not isinstance(wake, dict) \
            or (wake.get("dev"), wake.get("ino")) != at[:2] \
            or not (isinstance(wake.get("off"), int) and wake["off"] >= at[2]):
        return False
    start = e.get("start")
    if not isinstance(start, int):
        start = _row_start(e["room"], at, e.get("id"))
        if start is None:
            return False
    from .seats_cursor import _occurrence_parts
    return not any(p and p[:2] == at[:2] and start <= p[2] < at[2]
                   for p in map(_occurrence_parts, wake.get("held") or ()))


#: `_acks`'s default: the seat-level ack, whichever session of the seat
#: wrote it.
EVERY_SESSION = object()


def _acks(row, seat, session=EVERY_SESSION):
    """The row ids that `row` ACKS for `seat`, () when none. `helm chat ack
    <id> [<id> ...]` writes one row from the seat per room, naming the full
    id of each of its rows there, after them (seats_ack.ack_many), and a seat
    that acks a row has seen it. A bulk row names every id (chat.ack_ids),
    and each is acked, not only the first. An ack from another seat acks
    none.

    ONE PREDICATE, AT TWO SCOPES. With no `session` it is the seat-level ack
    that the ring reads (`_acked`): the ring rings per seat, so an ack from
    any session of the seat counts, a delegate's and one that records no
    session included. Given a `session`, as the tool-boundary hook
    (seats_delivery) and the stop guard (seats_stop_fp) give it, whose
    cursors are per session, the ack counts only when the row records that
    session: `session` is its `seats_cursor._sid8` key, which seats_ack
    stamps. An ack a delegate wrote records none (helm.pull_delivery: a
    delegate is not its seat), a co-named sibling's records its own, and an
    ack row older than the stamp records none, so each of those leaves the
    row shown and counted for this session. A None `session` honours no
    ack."""
    if not isinstance(row, dict) or (session is not EVERY_SESSION and not (
            session and row.get("session") == _sid8(session))):
        return ()
    return chat.ack_ids(row) if recipient_matches(row.get("from"), seat) \
        else ()


def _acked_at(entries, seat, session):
    """The starts of the rows in `entries`, the (row, start, end) window a
    hook or a guard pass already read, that `session` of `seat` acked
    (`_acks`) with an ack beginning at or after the row's end. An ack placed
    before its row is not one, as `_acked` reads acks only from a row's end
    on. An ack past the window is not seen, so its row stays owed."""
    last = {a: s for r, s, _e in entries for a in _acks(r, seat, session)}
    return {s for r, s, e in entries
            if isinstance(r, dict) and last.get(r.get("id"), -1) >= e}


def _acked(entries, seat):
    """The keys of `entries` whose row the seat itself has ACKED (`_acks`,
    seat-level: from any session of the seat).
    Each room's current file is read from the oldest entry's end on; an
    entry on another file, or in a room that cannot be read, is not
    acked."""
    rooms = collections.defaultdict(list)
    for e in entries:
        if isinstance(e.get("id"), str) and isinstance(e["at"][2], int):
            rooms[e["room"]].append(e)
    out = set()
    for room, group in rooms.items():
        try:
            with open(chat.room_path(room), "rb") as f:
                st = os.fstat(f.fileno())
                here = [e for e in group
                        if tuple(e["at"][:2]) == (st.st_dev, st.st_ino)]
                if not here:
                    continue
                f.seek(min(e["at"][2] for e in here))
                data = f.read()
        except OSError:
            continue
        ids = {a for line in data.split(b"\n")[:-1] if b'"ack"' in line
               for a in _acks(chat._msg(line.decode("utf-8", "replace")),
                              seat)}
        out.update(e["key"] for e in here if e["id"] in ids)
    return out


def _forget_read(st, seat, session):
    """Drop the rows the seat has ACKED from both lists (`_acked`), and the
    announced rows it has READ: its delivery cursor, the one the
    tool-boundary hook advances, has passed them, or a pull the seat ran
    printed them whole past a row it still owes, which released their holds
    (`_released`, helm.pull_delivery).

    A cursor on ANOTHER file is not a read. It is either behind (baselined
    before this file existed, and not moved since), so the row stays unread,
    or the row's file was replaced (rotation), so its offsets name nothing
    now and the row drops. The room's own file decides which. A cursor or a
    file that cannot be read keeps the row."""
    acked = _acked(st["owed"] + st["unrung"], seat)
    if acked:
        st["unrung"] = [e for e in st["unrung"] if e["key"] not in acked]
        st["since"] = st["since"] if st["unrung"] else None
    from .seats_delivery import _cursor
    cursors, files, keep = {}, {}, []
    for e in st["owed"]:
        if e["key"] in acked:
            continue
        room = e["room"]
        if room not in cursors:
            try:
                cursors[room] = (_cursor(room, seat, session),
                                 _cursor(room, seat, session, beacon=True))
            except Exception:                           # noqa: BLE001
                cursors[room] = (None, None)
        (cur, wake), at = cursors[room], tuple(e["at"])
        if cur is None:
            keep.append(e)
        elif (cur.get("dev"), cur.get("ino")) == at[:2]:
            if not (isinstance(cur.get("off"), int) and cur["off"] >= at[2]) \
                    and not _released(wake, e, at):
                keep.append(e)
        else:
            if room not in files:
                files[room] = _file_id(room)
            if files[room] is None or files[room] == at[:2]:
                keep.append(e)
    st["owed"] = keep


def _ref_states(refs, seat):
    """{ref: state} for the referents `refs` name: the word that ended it
    when its ledger says it is TERMINAL for `seat`, False when it is live or
    cannot be read, None for a hex token the dispatch ledger does not resolve
    (a commit, a chat row id: no referent at all).

    A dispatch row is terminal for the seat when it is closed by the ledger's
    one rule (`query.query_is_open`: cancelled, verdicted, closed, landed,
    retired) AND the seat is its recipient: a row handed to the seat that
    ended owes it nothing. A row the seat SENT stays live even closed, since
    a FIX verdict on it is the seat's next work. A task is terminal when its
    status is `closed`. A ledger that cannot be read, a prefix naming more
    than one row, and a task the ledger does not hold each leave the
    referent live: a ring fails open for delivery and never hides what it
    cannot read. So does a row whose fold met an event kind this helm has no
    arm for (`dispatches.unknown_event_kinds`), which the ledger's own ended
    reader (`dispatches._row_ended`) reads as live, since that kind may be
    the one that reopened it; and a task ledger with a corrupt row, read
    strict (`tasks.snapshot`), since the lenient read skips it."""
    out = {}
    hexes = [r for r in refs if not r.startswith("task/")]
    tids = [r for r in refs if r.startswith("task/")]
    if hexes:
        try:
            from . import dispatches, query
            current, unavailable = dispatches.snapshot()
        except Exception:                               # noqa: BLE001
            current, unavailable = None, True
        for ref in hexes:
            if unavailable or not isinstance(current, dict):
                out[ref] = False
                continue
            hits = [row for rid, row in current.items()
                    if str(rid).startswith(ref)]
            if not hits:
                out[ref] = None
                continue
            row = hits[0]
            done = len(hits) == 1 and isinstance(row, dict) \
                and str(row.get("status") or "").strip() \
                and not dispatches.unknown_event_kinds(row) \
                and not query.query_is_open(row) \
                and recipient_matches(row.get("recipient"), seat)
            out[ref] = dispatches.closed_state(row) if done else False
    if tids:
        try:
            from . import tasks
            rows, unavailable = tasks.snapshot(strict=True)
        except Exception:                               # noqa: BLE001
            rows, unavailable = None, True
        for ref in tids:
            row = rows.get(ref) if isinstance(rows, dict) \
                and not unavailable else None
            out[ref] = "closed" if isinstance(row, dict) \
                and row.get("status") == "closed" else False
    return out


def _closed(entries, seat):
    """{key: what closed it} for the entries whose row names a referent and
    whose every referent is terminal for `seat` (`_ref_states`). Only a row
    addressed to the seat or a DM is asked, never the owner's: a row naming
    one live or unreadable referent stays, whatever the others say."""
    asked = [e for e in entries if e.get("refs") and not e["owner"]
             and e["kind"] in ("addressed", "direct")]
    if not asked:
        return {}
    try:
        states = _ref_states({r for e in asked for r in e["refs"]}, seat)
    except Exception:                                   # noqa: BLE001
        return {}
    out = {}
    for e in asked:
        got = [(r, states.get(r)) for r in e["refs"]]
        if any(state is False for _r, state in got) \
                or not any(state for _r, state in got):
            continue
        out[e["key"]] = "; ".join("%s %s" % (r[:12], state)
                                  for r, state in got if state)
    return out


def _release_closed(st, seat, now):
    """Release the rows whose referent closed, the way an ack releases a
    row: they leave both lists, so no ring leads with them or counts them
    unread. Each leaves a trace in `closed` (its row id, room and what
    closed it, at `now`), so a release is auditable, and the next ring says
    how many it released. -> whether any was released."""
    closed = _closed(st["owed"] + st["unrung"], seat)
    if not closed:
        return False
    for name in ("owed", "unrung"):
        keep = []
        for e in st[name]:
            if e["key"] in closed:
                st["closed"].append({"t": now, "id": e["id"],
                                     "room": e["room"], "key": e["key"],
                                     "closed": closed[e["key"]]})
            else:
                keep.append(e)
        st[name] = keep
    st["closed"] = st["closed"][-KEEP:]  # noqa: SILENT_CAP — KEEP bounds the trace, as it bounds both lists
    st["since"] = st["since"] if st["unrung"] else None
    return True


def _recent(rings, now):
    return [t for t in rings if -HOUR_S < now - t < HOUR_S]


#: The rings that bypass the hourly cap, so the cap does not count them.
_UNCOUNTED = ("owner", "direct", "catch-up")


def _due(st, now, catch_up):
    """Why the state owes a ring now, or None. An owner row, a DM and the
    catch-up (the first pass of an armed waiter) ring at once: they skip the
    debounce and the hourly cap. Every other ring waits for the cap. New rows
    ring once their window has run, and with none, rows a ring announced and
    the seat has not read ring again once the backstop has run since the last
    ring."""
    unrung = st["unrung"]
    if any(e["owner"] for e in unrung):
        return "owner"
    if any(e["kind"] == "direct" for e in unrung):
        return "direct"
    if unrung and catch_up:
        return "catch-up"
    if len(_recent(st["rings"], now)) >= rings_per_h():
        return None
    if unrung:
        return "debounce" if st["since"] is not None \
            and now - st["since"] >= debounce_s() else None
    return "backstop" if st["owed"] and st["last"] is not None \
        and now - st["last"] >= backstop_s() else None


def _next_check(st, now):
    """When an idle waiter must next read the state with no new event: the
    backstop's due time while announced rows stay unread, moved past the
    hourly cap when the cap holds, else never."""
    if st["unrung"] or not st["owed"] or st["last"] is None:
        return None
    at = st["last"] + backstop_s()
    recent, cap = sorted(_recent(st["rings"], now)), rings_per_h()
    if len(recent) >= cap:
        at = max(at, recent[len(recent) - cap] + HOUR_S)
    return at


def _since_index(room, ids):
    """The `helm chat read --since` offset of the oldest unread row in `room`,
    or None when none of those rows is still in the room."""
    if not ids:
        return None
    try:
        rows, _total = chat.read(room)
    except Exception:                                   # noqa: BLE001
        return None
    return next((i for i, m in enumerate(rows) if m.get("id") in ids), None)


def _room_pulls(unread, seat):
    """The exact pull commands: the DM lane first, then the rooms by unread
    count. Each command starts at the oldest unread row in its room, so a pull
    is bounded to what is new and does not print the whole room."""
    by_room = collections.OrderedDict()
    for e in unread:
        by_room.setdefault(e["room"], []).append(e)
    order = sorted(by_room, key=lambda r: (not r.startswith(chat.DM_PREFIX),
                                           -len(by_room[r]), r))
    out = []
    for room in order[:PULL_ROOMS]:
        where = ("--dm --seat %s" % seat if room.startswith(chat.DM_PREFIX)
                 else "--room %s" % room)
        k = _since_index(room, {e["id"] for e in by_room[room] if e["id"]})
        out.append("helm chat read %s --since %d" % (where, k) if k is not None
                   else "helm chat read %s --limit %d" % (where, len(by_room[room])))
    if len(order) > PULL_ROOMS:
        out.append("and %d more rooms" % (len(order) - PULL_ROOMS))
    return "; ".join(out)


def _pulls(unread, seat, lead=None):
    """ONE PULL, not one per room: `helm chat read --id` naming exactly the
    rows the ring counts as the seat's to act on (`_actionable`), the lead
    first and then the newest, so the pull prints those rows and no room
    around them. Past PULL_IDS the older ones are pulled per room behind it.
    The rows that are not the seat's to act on (@all, reactions, room rows)
    are only counted, to read when idle. A ring with nothing to act on
    names the room pulls (`_room_pulls`)."""
    asked = [e for e in unread if _actionable(e)]
    if not asked:
        return _room_pulls(unread, seat)
    order = sorted(range(len(asked)), reverse=True, key=lambda i: (
        asked[i] is lead, asked[i].get("ts") or "", i))
    named = [asked[i] for i in order
             if _PULL_ID.fullmatch(str(asked[i].get("id") or ""))][:PULL_IDS]
    rest = [e for e in asked if not any(e is x for x in named)]
    out = "helm chat read --id " + ",".join(e["id"] for e in named) \
        if named else _room_pulls(rest, seat)
    if named and rest:
        out += "; and %d older: %s" % (len(rest), _room_pulls(rest, seat))
    idle = len(unread) - len(asked)
    return out + (" · %d not addressed: read when idle" % idle if idle else "")


def _lead(st):
    """The lead event of a ring of `st`. The pool is the new rows the seat
    has to act on (`_actionable`); with none, the unread ones it was already
    rung for; with none of those either, the new rows, else the unread ones.
    So an @all row, a reaction or a room row never leads while a row
    addressed to the seat is unread. Within the pool, the most direct class
    leads (`_rank`), and within it the newest row, the later-recorded one on
    a tie."""
    pool = [e for e in st["unrung"] if _actionable(e)] \
        or [e for e in st["owed"] if _actionable(e)] \
        or st["unrung"] or st["owed"]
    ranked = [(_rank(e["kind"], e["owner"], e["lead"]), e.get("ts") or "", i)
              for i, e in enumerate(pool) if isinstance(e.get("lead"), str)]
    if not ranked:
        return None
    best = min(r for r, _ts, _i in ranked)
    return pool[max(x for x in ranked if x[0] == best)[2]]


def sentences(st):
    """The two sentences of instruction a ring of `st` says, in order: how
    its rows clear, and when the doorbell rings again."""
    # an ack clears only a row addressed to the seat or a DM; a ring of
    # @all rows, reactions or room rows holds nothing an ack can clear
    # (seats_ack.ack_many skips or refuses each), so it names no ack. No
    # parenthesis in this text: the tail readers strip ends at the first ")"
    if not any(e["kind"] in ("addressed", "direct")
               for e in st["owed"] + st["unrung"]):
        handled = ("a row addressed to no one, an @all row, a reaction or "
                   "room chatter, clears by reading, not by ack, so pull, "
                   "and read what is addressed first")
    else:
        handled = ("rows you already handled clear in ONE call, helm chat "
                   "ack <id> <id> …, one row for them all; the rest: pull, "
                   "and read what is addressed first")
    return handled, ("it rings again when a new row lands, or in %d min "
                     "while these stay unread" % round(backstop_s() / 60))


def context_epoch(seat, session):
    """Where `seat`'s context last started over, as a waiter can read it
    cheaply at a ring -> (the session the roster binds the seat to, when the
    seat's own thread last began a compaction), or None when either cannot
    be read. Both are signals the fleet already writes: the SessionStart
    join moves the roster row's `session` for a new session of the pane
    (`seats_roster.write_roster`; a non-pane session never moves it), and
    the PreCompact hook records every compaction under the seat's resume
    key (`resumeturn.note_precompact`), its `agent` None for the seat's main
    thread and a subagent's id for a subagent's, whose compaction leaves the
    seat's context as it was. A change says the context no longer holds what
    this waiter's earlier rings told it. The hook keys the record by the
    process's declared name (`own_name`), so a --seat that differs from it
    only in case reads under the declared spelling."""
    try:
        from . import resumeturn
        from .seats_common import own_name, seat_row
        row = seat_row(seat)[0] or {}
        mine = own_name()
        name = mine if mine and mine.casefold() == str(seat).casefold() \
            else seat
        entry = (pk.read_json(resumeturn.state_path(), {}) or {}).get(
            resumeturn.compaction_key(name, session))
        rec = entry.get("precompact") if isinstance(entry, dict) else None
        at = rec.get("at") if isinstance(rec, dict) \
            and rec.get("agent") is None else None
        return row.get("session"), at
    except Exception:                                   # noqa: BLE001
        return None


def ring_line(st, seat, told=None):
    """ONE line for everything the state holds unread. The lead is the row
    to act on (`_lead`), and the tail names the one pull (`_pulls`) and how
    many rows were released since the last ring because their referent
    closed (`_release_closed`).

    A SEAT THAT OWES A DISPATCH ROW HEARS IT ON THE RING. The lead is
    followed by the owed clause (`seats_stop_owed.owed_clause`, the wording
    the per-row stream's wake line carries), and then the tail. The clause
    goes BEFORE the tail because readers strip the tail as fixed text at the
    end of the line, so the tail stays last. A seat that owes nothing gets
    the ring unchanged.

    A SENTENCE OF INSTRUCTION IS SAID ONCE PER WAITER (task/3382). Two
    sentences tell the seat what to DO, how its rows clear (the bulk ack, or
    a read) and when the doorbell rings again, and they are the same on
    every ring: a local seat's 222 rings in one day repeated them for 361k
    characters. `told` is the set of sentences this waiter process already
    rang (`sentences`): a sentence in it is left out, so a waiter's first
    ring says every sentence that applies and a later ring says only one it
    has not said. This function never adds to `told`: the caller adds a
    ring's sentences once the stream took the line (`Doorbell._poll`), and
    empties it on every RESAY_EVERY-th ring and on the first ring after the
    seat's context started over (`context_epoch`). The
    facts stay on every ring: the lead, the owed clause, the waiting count,
    the exact pull, the unread split and the count new since the last ring.
    `told=None` says every sentence, the ring as it always was."""
    from .seats_stop_owed import owed_clause
    unread = st["owed"] + st["unrung"]
    count = collections.Counter(e["kind"] for e in unread)
    parts = ["%d addressed" % count["addressed"], "%d DM" % count["direct"],
             "%d @all" % count["all"]]
    for kind, label in (("react", "reactions"), ("room", "room rows")):
        if count[kind]:
            parts.append("%d %s" % (count[kind], label))
    owner = sum(1 for e in unread if e["owner"])
    if owner:
        parts.append("%d from the owner" % owner)
    first = _lead(st)
    lead = first["lead"] if first else (
        "[helm chat → %s] doorbell: the lead row could not be rendered" % seat)
    closed = sum(1 for c in st.get("closed") or ()
                 if st["last"] is None or c["t"] > st["last"])
    said = [x for x in sentences(st) if told is None or x not in told]
    return ("%s%s (+%d waiting — %s · doorbell: %d unread = %s · %d new since "
            "the last ring%s%s)"
            % (lead, owed_clause(seat, lead), len(unread) - 1,
               _pulls(unread, seat, first), len(unread),
               ", ".join(parts), len(st["unrung"]),
               " · %d already closed" % closed if closed else "",
               " · " + "; ".join(said) if said else ""))


def _rung(st, now, counted):
    """Record a ring at `now`: the cap counts it only when `counted`, and
    the rows it announced move to `owed` until the seat reads them."""
    if counted:
        st["rings"] = _recent(st["rings"], now) + [now]
    st["last"] = now
    st["owed"] = (st["owed"] + st["unrung"])[-KEEP:]  # noqa: SILENT_CAP — KEEP bounds the state; its comment says counts cover what is kept
    st["unrung"], st["since"] = [], None


class Doorbell:
    """One waiter's doorbell over its seat's durable state.

    `poll(deliver)` is one waiter pass. `deliver(emit)` is one `deliver_any`
    call on the beacon channel that hands its event to `emit`. The pass drains
    what the wake cursor owes, folds it into the state, and rings through
    `stream` at most once. It never raises: a doorbell fault costs one ring,
    never the waiter, and is reported once on stderr. A state file that cannot
    be written is reported the same way, and the pass then carries the state
    in memory, so the rows it drained still ring."""

    def __init__(self, seat, session, stream, usable=None, deliver=None):
        self.seat, self.session, self.stream = seat, session, stream
        self.usable = usable or (lambda: None)
        self.deliver = deliver       # the `deliver` that `ring` polls with
        self.caught_up = False
        self.pending = True          # the first pass reads the durable state
        self.recheck = None          # the backstop's due time, in memory
        self.mem, self.unsaved = None, False
        self._warned = set()
        self.told = set()            # the sentences this waiter already rang
        self.rang = 0                # the rings the stream took
        self.epoch = None            # context_epoch at the last ring

    def _warn(self, what, exc):
        if what not in self._warned:
            self._warned.add(what)
            print("[helm chat] beacon doorbell %s (%s: %s); the waiter keeps "
                  "running" % (what, type(exc).__name__, exc),
                  file=sys.stderr, flush=True)

    def _drain(self, deliver):
        got = []
        for _ in range(DRAIN_PASS):
            try:
                if not deliver(got.append):
                    return got, False
            except Exception:                           # noqa: BLE001
                return got, False
        return got, True

    def _store(self, path, st):
        self.mem = st
        try:
            _save(path, st)
        except OSError as exc:
            self._warn("state not written", exc)
            self.unsaved = True
            return
        self.unsaved = False

    def ring(self):
        """One waiter pass with the `deliver` that `waiter_bell` armed."""
        return self.poll(self.deliver)

    def poll(self, deliver):
        try:
            return self._poll(deliver)
        except Exception as exc:                        # noqa: BLE001
            self._warn("fault", exc)
            return None

    def _poll(self, deliver):
        events, more = self._drain(deliver)
        now = _now()
        if not events and not self.pending \
                and (self.recheck is None or now < self.recheck):
            return None
        path = state_path(self.seat)
        try:
            os.makedirs(state_dir(), exist_ok=True)
        except OSError as exc:
            self._warn("state not written", exc)
        # task/2520's one opt-in: the drained rows are already past this
        # waiter's wake cursor, so a raise here would lose them for good.
        # THE UNLOCKED PATH IS NOT SAFE, ONLY LESS LOSSY. The state file is
        # per SEAT (`state_path`), not per waiter: two waiters of one seat
        # with distinct wake cursors can both drain, both load the same state
        # and both store it whole, and the second store overwrites the first,
        # so a row only the first drained is never rung. The lock closes that
        # race whenever it can be taken; without it this PROCEEDS UNLOCKED.
        with _flocked(path + ".lock", unlocked_ok=True):
            st = self.mem if self.unsaved else _load(path)
            if events:
                _record(st, events, self.seat)
                if st["unrung"] and st["since"] is None:
                    st["since"] = now
                # THE DRAINED ROWS ARE DURABLE BEFORE ANYTHING ELSE CAN FAIL:
                # a drained row is past the wake cursor, so no later drain
                # sees it again.
                self._store(path, st)
            line = None
            if not more:
                held = len(st["owed"]) + len(st["unrung"])
                _forget_read(st, self.seat, self.session)
                stale = len(st["owed"]) + len(st["unrung"]) != held
                if st["owed"] and st["last"] is None:
                    st["last"], stale = now, True   # a state with no ring time
                why = _due(st, now, not self.caught_up)
                # A SINK PROVEN TO REACH NOBODY GETS NO RING: ringing marks the
                # rows announced, and nobody would have seen the announcement.
                usable = bool(why) and self.usable() is not False
                # A ROW WHOSE REFERENT CLOSED COSTS NO CALL: it is released
                # before the ring is composed, and a ring left with nothing
                # due is not rung at all. Asked only when a ring is due, so
                # the ledgers are read at most once per ring.
                if usable and _release_closed(st, self.seat, now):
                    stale, why = True, _due(st, now, not self.caught_up)
                if why and usable:
                    # THE WAITER OUTLIVES THE CONTEXT IT TOLD: every
                    # RESAY_EVERY-th ring, and the first after the seat's
                    # context started over, says every sentence again
                    epoch = context_epoch(self.seat, self.session)
                    if self.rang % RESAY_EVERY == 0 or epoch != self.epoch:
                        self.told.clear()
                    line = ring_line(st, self.seat, self.told)
                    self.stream(line)
                    # TOLD ONCE THE STREAM TOOK IT: a write that raised
                    # reaches `poll`'s fault, and the retry says them all
                    self.told.update(sentences(st))
                    self.rang, self.epoch = self.rang + 1, epoch
                    _rung(st, now, why not in _UNCOUNTED)
                    stale = True
                if stale:
                    self._store(path, st)
            self.pending = bool(st["unrung"])
            self.recheck = _next_check(st, now)
        if not more:
            self.caught_up = True
        return line


def waiter_bell(seat, session, stream, doorbell, usable, deliver, **scope):
    """The Doorbell a follow waiter rings (seats_join.wait), or None when the
    doorbell is off (`--per-row`), after one pass over the durable state.

    Its `deliver` is the per-row leg's `deliver_any` call on the beacon
    channel, handed to the doorbell's collector; the fence (`usable`) still
    reads the waiter's stream, which is where the ring goes. announce_only: a
    ring is not a delivery, so each row it wakes stays owed to the hook. The
    waiter passes its own lambdas, so `deliver_any` and `destination_usable`
    still resolve in seats_join at every call, where a patch of seats_join
    or the seats facade's fan-out lands.

    A PER-ROW ARM AFTER A DOORBELL WAITER: rows that waiter drained and had
    not rung sit past the wake cursor, so the per-row stream never sees them.
    The hook still owes them, but an idle seat reaches no tool boundary
    (measured: 0 lines for 2 drained rows). Ring the durable state once, then
    stream."""
    if not doorbell:
        Doorbell(seat, session, stream, usable=usable).poll(lambda sink: None)
        return None
    return Doorbell(seat, session, stream, usable=usable, deliver=lambda sink: (
        deliver(session=session, seat=seat, emit=sink, channel="beacon",
                sink_usable=usable(), announce_only=True, **scope)))
