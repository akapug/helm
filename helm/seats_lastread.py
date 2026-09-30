#!/usr/bin/env python3
"""helm seats — the [n] a reader's last `helm chat read` printed (task/3382).

`helm chat read` prints `[n] <8 hex id>` beside each row, and `helm chat ack`
took only the id. MEASURED over 40.5 h of the three local seats'
transcripts: 35 chat acks failed, and 11 of them typed the [n] a read had
just printed. So each read records the [n] -> row id pairs it printed, for
the reader it ran as, and an ack names a row by `[n]` or a bare n through
that record.

ONLY THE LAST READ COUNTS. [n] is an ordinal of the room that read showed,
and a number from an older read of another room names another row, so a
number the last read did not print is REFUSED, never matched against ids
and never resolved against the room as it is now. A read in a session a
delegate is marked in records no numbers: the mark cannot say whose read it
was (pull_delivery.DELEGATE_S), and an [n] resolved against a delegate's
read would ack a row the seat never saw.

A CUT ROW'S [n] ACKS NOTHING until the seat saw the row whole. A short read
(helm.chatshort) prints a long row in part, and its line is a
`pull_delivery.Partial`: the read records that [n] as CUT, so an ack of it
refuses in one line naming `helm chat read --id <id>`, the reader that
prints it whole, and acks nothing. That reader records the row as seen
whole (`whole`), and the same [n] then acks it. A read that cut a row still
delivers none of it (pull_delivery), and an [n] must not clear what the
delivery kept owed.
"""
import re
import time

from . import chat, home, pk
from .seats_common import _seat_key
from .seats_cursor import _sid8

#: The chat state family that holds each reader's last read (chat.state_path).
FAMILY = "lastread"
#: `[n]` as `helm chat read` prints it, or a bare n shorter than an id (the
#: same line `seats_ack._resolve_row` draws between a row NUMBER and an id).
NUMBER = re.compile(r"\[(\d{1,7})\]|(\d{1,%d})" % (chat.ID_SHOWN - 1))


def path(reader):
    """Where `reader`'s last read is recorded."""
    return chat.state_path(FAMILY, _seat_key(reader) + ".json")


def _reader(claimed, session, act):
    """(reader, delegate) for a read by `session` naming `claimed`, or None
    when no door admits it (`actors.resolve_actor`, the door `helm chat ack`
    takes). `delegate`: a delegate of the session is marked reading chat."""
    from . import actors
    from .pull_delivery import _delegate_marked
    actor, err = actors.resolve_actor(session, asserted=claimed, act=act)
    if err or actor is None:
        return None
    return actor.canonical_name, bool(session and _delegate_marked(session))


def remember(room, rows, printed, claimed=None, session=None):
    """Record the [n] -> row id pairs one read printed, for the seat the read
    runs as. `rows` is the room the read numbered and `printed` its
    [(index, row, line)], the list the pull discharges (a row folded into the
    line before it printed no [n]). `claimed` is the read's `--seat`. A
    `Partial` line's [n] is recorded apart, as `cut`.

    THE READER IS THE ADMITTED ACTOR, through the door `helm chat ack` takes
    (`actors.resolve_actor`), so a read that names another seat, or runs
    under an identity no door admits, records nothing and cannot overwrite
    that seat's numbers. Never raises: a read whose record could not be
    written still printed its rows, and an ack then refuses the number."""
    try:
        from .pull_delivery import Partial
        session = session or home.session_id()
        admitted = _reader(claimed, session, "record the [n] a read printed")
        if admitted is None:
            return
        reader, delegate = admitted
        ords = chat.react_ordinals(rows)
        numbers, cut = {}, {}
        for i, m, line in [] if delegate else printed:
            if line is not None and ords.get(i) \
                    and chat._ROW_ID.fullmatch(str(m.get("id") or "")):
                (cut if isinstance(line, Partial) else numbers)[
                    str(ords[i])] = str(m.get("id"))
        pk.write_json(path(reader), {"room": room, "ts": time.time(),
                                     "session": _sid8(session),
                                     "delegate": delegate, "rows": numbers,
                                     "cut": cut})
    except Exception:                                    # noqa: BLE001
        pass


def whole(rid, claimed=None, session=None):
    """`helm chat read --id` printed row `rid` whole to the seat it runs as:
    an [n] its last read recorded as cut for that row now names it, as a
    whole row's [n] does. A read a delegate may have made, or one no door
    admits, changes nothing. Never raises, as `remember`."""
    try:
        session = session or home.session_id()
        admitted = _reader(claimed, session, "record the row a read printed")
        if admitted is None or admitted[1]:
            return
        last = pk.read_json(path(admitted[0]), None)
        cut = last.get("cut") if isinstance(last, dict) else None
        if not isinstance(cut, dict) or rid not in cut.values():
            return
        rows = last.get("rows") if isinstance(last.get("rows"), dict) else {}
        rows.update((n, x) for n, x in cut.items() if x == rid)
        last.update(rows=rows, cut={n: x for n, x in cut.items() if x != rid})
        pk.write_json(path(admitted[0]), last)
    except Exception:                                    # noqa: BLE001
        pass


def by_number(tids, reader):
    """(ids, err): each `[n]` or bare n in `tids` replaced by the row id
    `reader`'s last read printed at [n], every other token as it came. One
    number that read did not print refuses the whole call in one line."""
    if not any(NUMBER.fullmatch(t) for t in tids):
        return list(tids), None
    last = pk.read_json(path(reader), None) or {}
    rows = last.get("rows") if isinstance(last.get("rows"), dict) else {}
    cut = last.get("cut") if isinstance(last.get("cut"), dict) else {}
    out = []
    for t in tids:
        m = NUMBER.fullmatch(t)
        if not m:
            out.append(t)
            continue
        n = m.group(1) or m.group(2)
        if n in rows:
            out.append(str(rows[n]))
            continue
        if n in cut:
            return None, ("%r is a row your last read cut, so you have not "
                          "seen it whole: helm chat read --id %s prints it, "
                          "and then %s acks it"
                          % (t, chat._dsan(str(cut[n])), t))
        if not last:
            why = "no `helm chat read` of yours recorded any [n]"
        elif last.get("delegate"):
            why = ("your last read ran while a delegate of this session was "
                   "reading chat, so its [n] cannot be told from the "
                   "delegate's; ack the id printed beside [n]")
        else:
            got = sorted(set(rows) | set(cut), key=int)
            why = ("your last `helm chat read` did not print [%s] (it printed "
                   "%s of room %s)" % (
                       n, "[%s]..[%s]" % (got[0], got[-1]) if got
                       else "no [n]", chat._dsan(str(last.get("room") or "?"))))
        return None, "%r is a row number: %s" % (t, why)
    return out, None
