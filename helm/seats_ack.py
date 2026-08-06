#!/usr/bin/env python3
"""helm seats — the ACK / CONSUME ladder: SENT is not SEEN is not ACTED.

THREE STATES, NOT TWO, and collapsing any pair is what this module exists to
prevent. A row that was DELIVERED reached a seat's cursor. A row that was
SEEN was read past. A row that was ACKED had someone say what they did about
it. Every surface that treats delivery as discharge produces the same
failure: an obligation that everyone can see and nobody owns.

The per-(recipient, row) state is why an addressed word can outlive the turn
that delivered it. Reading past an obligation does not discharge it — that is
a deliberate asymmetry, not an oversight, and it is the reason `ack` exists
as a separate verb rather than a side effect of `read`.

A LEAF AMONG THE REMAINING SECTIONS — and the qualifier is the honest form.
It reads plenty from the already-extracted floor below it (roster, cursor,
identity, the recipient comparators). What it does NOT do is call claims,
report, cli, the stop ladder or the work offer, which is why it could be
lifted straight out of the middle of a knot while those four still hold each
other. A leaf inside a tangle is worth hunting for: it is the one piece that
moves without deciding anything else first.
"""

import json
import os
import re
import time

from . import chat, home, pk
from .seats_common import (_BROADCAST, _canonical_recipient, _seat_label,
                           recipient_matches, roster)
from .seats_identity import _warn_disagreement, derive_seat
from .seats_roster import last_seen, seat_for_session
from .seats_delivery import _cursor

_MENTION_TOKEN = re.compile(r"(?<![A-Za-z0-9._-])@([A-Za-z0-9._-]{1,64})")
def _ts_epoch(ts):
    """An ISO chat ts -> epoch seconds (None on garbage). last_seen is a float
    mtime and a row ts is second-floored, so the SEEN fallback compares
    int(last_seen) against this."""
    import calendar
    try:
        return calendar.timegm(time.strptime(str(ts), "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return None
def _dm_lanes():
    """Every private DM lane as its reserved room name. list_rooms() hides
    them (no fanout), but a sender's outbound DMs live in the RECIPIENTS'
    lanes — `pending` must enumerate them to find its own stranded words."""
    try:
        names = os.listdir(os.path.join(chat.chat_dir(), "dm"))
    except OSError:
        return []
    return [chat.DM_PREFIX + n[:-6] for n in names if n.endswith(".jsonl")]
def _all_lanes():
    return list(chat.list_rooms()) + _dm_lanes()
def _is_seat(name):
    """Whether `name` is a known roster seat (casefold) — an address with
    ground somewhere, vs an unresolved token (typo / a seat never online)."""
    return any(recipient_matches(name, key) for key in roster())
def _recipients(m, r=None, include_unresolved=False):
    """The seats a row DIRECTLY ASKS — whose consume state the sender tracks.
    A DM names exactly one; otherwise the roster seats @mentioned plus a reply's
    parent author (rfrom). Reaction additions wake their target author through
    deliverable(), but remain attention-only signals: no ACTED debt is created.
    ack / react / ambient rows therefore address nobody here. Roster-resolved,
    broadcast tokens dropped: only a seat with a cursor can be SEEN or ACTED.
    include_unresolved (the sender's pending view ONLY — never ack's
    authorization path) also yields a @mention that resolves to NO roster
    seat: the deadest, most-stranded address, kept VISIBLE not silently
    dropped. consume_state renders it 'unresolved'."""
    if not isinstance(m, dict) or m.get("ack") or m.get("react") \
            or m.get("ambient"):
        return []
    dm = m.get("dm")
    if dm:
        shown = m.get("dm_display") or dm
        recipient, err = _canonical_recipient(m["dm"], display=shown)
        if err is None:
            return [recipient]
        # Current writers reject malformed recipients at chat.post. A historical
        # or hand-edited row still stays visible to pending's fail-closed audit;
        # ack authorization never accepts it because that caller does not opt in.
        return [str(shown)] if include_unresolved else []
    text = m.get("text") or ""
    r = roster() if r is None else r
    keys = {}
    for key in r:
        canonical, err = _canonical_recipient(key)
        if err is None:
            keys[str(canonical)] = key
    out, seen = [], set()
    for tok in _MENTION_TOKEN.findall(text):
        if _BROADCAST.search("@" + tok):
            continue
        canonical, err = _canonical_recipient(tok)
        cf = str(canonical or "")
        k = keys.get(cf)
        if k is None and include_unresolved and err is None:
            k = canonical                 # keep the raw address display visible
        if k and cf not in seen:
            out.append(k)
            seen.add(cf)
    rf, _err = _canonical_recipient(m.get("rfrom"))
    rf = str(rf or "")
    if rf and rf in keys and rf not in seen:
        out.append(keys[rf])
    return out
def _row_offsets(room):
    """(dev, ino, [(row, end_off)]) for one lane — every complete row and the
    byte offset PAST it, the same identity the delivery cursor commits.
    Lets a row be tested against a recipient's cursor.off without moving
    it. Every chat row ends in a newline (chat._append), so there is no
    partial tail to mis-measure."""
    try:
        with open(chat.room_path(room), "rb") as f:
            st = os.fstat(f.fileno())
            data = f.read()
    except OSError:
        return None, None, []
    out, pos = [], 0
    for chunk in data.split(b"\n"):
        end = pos + len(chunk) + 1
        if chunk:
            row = chat._msg(chunk.decode("utf-8", errors="replace"))
            if row is not None:
                out.append((row, end))
        pos = end
    return st.st_dev, st.st_ino, out
def _recipient_cursor(room, seat):
    """The recipient's delivery cursor for one lane — its newest session's,
    seat-level fallback (roster_report's read pattern). None when the seat has
    no ground here (never joined / not tracked): then it has NOT seen the row."""
    r = roster()
    row = next((value for key, value in r.items()
                if recipient_matches(key, seat)), {})
    return _cursor(room, seat, row.get("session")) or _cursor(room, seat)
def consume_state(m, room, recipient, dev=None, ino=None, end_off=None,
                  acks=None):
    """(state, ackstate) for ONE (recipient, row):
    'acted' | 'seen' | 'sent' | 'unresolved'.
    READ-ONLY over the existing plumbing — no cursor moves, no second ledger:
      acted  an ack row (this recipient, this row id) exists in the lane
      seen   the recipient's cursor passed the row's end offset, OR the seat
             was active in a strictly-later second than the row (touch_seen) —
             BUT only for a row ABOVE the join baseline (a row at/below base
             was never surfaced, so neither SEEN path may fire on it)
      sent   the row was written and neither holds — the stranded state
      unresolved  a room @mention addressing no known seat: no cursor ground
             anywhere, the deadest recipient — surfaced distinctly, never lost
    `acks` = {(target_id, from_cf): ackstate} for the lane; dev/ino/end_off
    are that same lane read (all recomputed when a caller omits them)."""
    tid = m.get("id")
    rcf, _ = _canonical_recipient(recipient)
    rcf = str(rcf or "")
    if acks is None or dev is None:
        dev, ino, rows = _row_offsets(room)
        acks, end_off = {}, None
        for x, e in rows:
            if x.get("ack"):
                sender, _ = _canonical_recipient(x.get("from"))
                acks[(x["ack"], str(sender or ""))] = \
                    x.get("ackstate") or "done"
            if x.get("id") == tid:
                end_off = e
    if tid and (tid, rcf) in acks:
        return "acted", acks[(tid, rcf)]
    cur = _recipient_cursor(room, recipient)
    if cur is None and not m.get("dm") and not _is_seat(recipient):
        return "unresolved", None   # no ground, not a known seat: the deadest
                                    # address — kept VISIBLE, never false-SEEN
    # The join baseline: rows at/below the offset the seat baselined this room
    # at (a room @mention posted BEFORE the join sits below EOF) were NEVER
    # surfaced by deliver and never will be — neither cursor- nor touch_seen-
    # SEEN can fire on them. base carries forward unchanged as the cursor
    # advances but RESETS to 0 on a rotation (deliver re-baselines when dev,ino
    # changes — the old-file offset would else false-SENT the new file's rows);
    # it defaults to 0 (legacy cursors / DM lanes baseline at 0, so every row
    # above 0 stays eligible — no regression). It is a valid offset only against
    # the SAME lane file the cursor baselined against — the `same` guard below.
    same = bool(cur) and (dev, ino) == (cur.get("dev"), cur.get("ino"))
    base = cur.get("base", 0) if same else 0
    below_base = end_off is not None and end_off <= base
    if not below_base:
        if same and end_off is not None and end_off <= cur.get("off", 0):
            return "seen", None
        ls = last_seen(recipient)
        mts = _ts_epoch(m.get("ts"))
        if ls is not None and mts is not None and int(ls) > mts:
            return "seen", None   # touch_seen fallback (a later second)
    return "sent", None
def _locate_row(target_id):
    """(row, room, err): the one chat row a bare id names, across every live
    lane. Exact id wins; an unambiguous >=4-char prefix resolves too (ids are
    12 hex — nobody types them whole). Ambiguous or unknown => a refusal
    string, never a guess."""
    tid = str(target_id or "").strip()
    if not tid:
        return None, None, "ack needs a message id (helm chat read shows ids)"
    hits = []
    for room in _all_lanes():
        for m in chat.read(room)[0]:
            rid = str(m.get("id") or "")
            if rid and (rid == tid or (len(tid) >= 4 and rid.startswith(tid))):
                hits.append((m, room, rid == tid))
    exact = [h for h in hits if h[2]]
    hits = exact or hits
    if not hits:
        return None, None, "no message matches id %r (helm chat read)" % tid
    if len({h[0].get("id") for h in hits}) > 1:
        return None, None, "id %r is ambiguous — use more of it" % tid
    return hits[0][0], hits[0][1], None
def ack(target_id, state="done", note=None, who=None, session=None):
    """(result, err). The RECIPIENT marks a row ACTED. One append-only ack row
    on the SAME lane as the target (the one-writer chat.post path, signed like
    any row), stamped {ack: <target id>, ackstate: done|blocked}; the note is
    its text. REFUSED unless the acker is an actual recipient of the row —
    a foreign or unknown id never writes. Idempotent: a repeat with the same
    acker + state + note appends nothing (append-only, but no duplicate row)."""
    state = str(state or "done").lower()
    if state not in ("done", "blocked"):
        return None, "ack state must be 'done' or 'blocked' (got %r)" % state
    note = (note or "").strip() or None
    _warn_disagreement(session, "ack")   # warn-only here: the CLI leg already
    # REFUSES via chat._seat_actor; this covers programmatic callers loudly
    seat = who or seat_for_session(session) or derive_seat(session)
    m, room, err = _locate_row(target_id)
    if err:
        return None, err
    tid = m.get("id")
    recips = _recipients(m)
    scf, _ = _canonical_recipient(seat)
    scf = str(scf or "")
    if not any(recipient_matches(scf, x) for x in recips):
        # the refusal reaches a terminal (cmd prints err to stderr): launder
        # every roster/identity-borne token via _seat_label, the seats.py
        # publish-owner law — a planted seat name must not reshape the refusal.
        if not recips:
            return None, ("%s is not an addressed message — nothing to ack "
                          "(from %s)" % (str(tid)[:8],
                                         _seat_label(m.get("from") or "?")))
        return None, ("%s is addressed to %s, not you (%s) — only its "
                      "recipient can ack"
                      % (str(tid)[:8],
                         "/".join(_seat_label(x) for x in recips),
                         _seat_label(seat)))
    prior = [x for x in chat.read(room)[0] if x.get("ack") == tid
             and recipient_matches(x.get("from"), scf)]
    prev = prior[-1] if prior else None
    if prev and prev.get("ackstate") == state \
            and (prev.get("text") or "") == (note or ""):
        return {"target": m, "room": room, "state": state, "row": prev,
                "dup": True}, None
    # who=seat (the AMBIENT acker, resolved by _seat_actor at the verb) is the
    # row identity; the signer is the ambient HELM_CELL_PROFILE (profile=None
    # -> cell.profile_name) — never the --seat claim, so a seat cannot forge a
    # signed ACK clearing another seat's obligation (cross-family review).
    row = chat.post(note or "", room=room, who=seat,
                    ack=tid, ackstate=state)
    return {"target": m, "room": room, "state": state, "row": row,
            "dup": False}, None
def pending(seat=None, session=None, cap=50):
    """(items, total): the sender's outbound ADDRESSED rows not yet ACTED, one
    entry per (recipient, row) — SENT-not-SEEN, SEEN-not-ACTED, or the deadest
    UNRESOLVED (a mention no seat answers). Derived read-only off the
    recipients' cursors + touch_seen; an acked pair drops off (consumed). An
    UNREADABLE lane yields one 'unknown' entry (fail CLOSED — never a silent
    empty that reads as all-clear). Ordered oldest-first, bounded."""
    _warn_disagreement(session, "pending")   # read-only: warn, never block
    seat = seat or seat_for_session(session) or derive_seat(session)
    scf, _ = _canonical_recipient(seat)
    scf = str(scf or "")
    r = roster()
    items = []
    for room in _all_lanes():
        dev, ino, rows = _row_offsets(room)
        if dev is None:
            # An unreadable lane is UNKNOWN, never a silent empty: fail CLOSED
            # so the sender never gets a FALSE green all-clear over a lane we
            # could not read (a room whose consume state is genuinely unknown).
            items.append({"id": None, "room": room, "to": None,
                          "state": "unknown", "ts": None,
                          "dm": room.startswith(chat.DM_PREFIX), "text": ""})
            continue
        acks = {}
        for m, _e in rows:
            if m.get("ack"):
                sender, _ = _canonical_recipient(m.get("from"))
                acks[(m["ack"], str(sender or ""))] = \
                    m.get("ackstate") or "done"
        for m, end_off in rows:
            if not recipient_matches(m.get("from"), scf):
                continue
            for rc in _recipients(m, r, include_unresolved=True):
                if recipient_matches(rc, scf):
                    continue
                st, _a = consume_state(m, room, rc, dev, ino, end_off, acks)
                if st == "acted":
                    continue
                shown = getattr(rc, "display", str(rc))
                items.append({"id": m.get("id"), "room": room, "to": shown,
                              "state": st, "ts": m.get("ts"),
                              "dm": bool(m.get("dm")),
                              "text": m.get("text") or ""})
    items.sort(key=lambda x: str(x.get("ts") or ""))
    return items[:cap], len(items)
