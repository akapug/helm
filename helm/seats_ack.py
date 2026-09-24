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
from .seats_common import (_BROADCAST, _canonical_recipient, _clip, _Fault,
                           _reason, _scrub, _seat_label, recipient_matches,
                           roster)
# _fmt_age lives in seats_report and seats_report does NOT import this
# module, so the edge is safe — checked before adding it rather than after.
from .seats_report import _fmt_age
from .seats_identity import _warn_disagreement, derive_seat
from .seats_roster import last_seen, seat_for_session
from .seats_cursor import _cursor_checked

_MENTION_TOKEN = re.compile(r"(?<![A-Za-z0-9._-])@([A-Za-z0-9._-]{1,64})")
# _Fault and _reason MOVED TO seats_common, the floor every reader already
# imports. They were born here because this is where the class was first
# cured — but a fault vocabulary living in whichever module noticed last is
# unreachable from the reader that needs it next: seats_delivery cannot
# import from here (this module imports IT), and the CURSOR reader is exactly
# the next door. The rule belongs at the seam, not at the discovery site.
def fault_lines(faults, limit=5):
    """The INCOMPLETE section of a pending render, as lines.

    WHAT THIS SCREEN COULD NOT READ, NAMED — its own count, and no age claim.
    These are INPUTS, not rows anybody addressed: while they lived inside the
    item list they were counted as addressed rows, sorted ahead of real ones
    on a timestamp they did not have, and ate the whole message cap.

    Each line carries kind + subject + reason, because a generic UNKNOWN
    leaves the reader guessing which file to open — and this ladder shipped
    exactly that mislabel once, printing "lane unreadable" over a ROSTER
    failure. It lives here rather than in the CLI because how a fault READS is
    knowledge about faults; the terminal only prints what it is handed."""
    if not faults:
        return []
    out = ["  ⚠ THIS VIEW IS INCOMPLETE — %d input%s could not be read, so "
           "nothing below may be called absent:"
           % (len(faults), "s"[:len(faults) != 1])]
    out += ["    %-14s %-28s %s" % (f.kind, str(f.where)[:28], f.reason)
            for f in faults[:limit]]
    if len(faults) > limit:
        out.append("    … and %d more unread input" % (len(faults) - limit))
    return out
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
    """(lanes, fault): every private DM lane as its reserved room name.
    list_rooms() hides them (no fanout), but a sender's outbound DMs live in
    the RECIPIENTS' lanes — `pending` must enumerate them to find its own
    stranded words.

    A MISSING NAMESPACE IS PROVEN EMPTY; AN UNREADABLE ONE IS NOT. This
    answered both with [], so denying read on <chat>/dm alone turned one
    genuinely pending DM into total=0 and the CLI printed the green all-clear
    (measured). It is the same law roster_checked already draws one
    floor down, and the reason the split lives HERE rather than in pending:
    the caller cannot recover a distinction its reader already threw away."""
    path = os.path.join(chat.chat_dir(), "dm")
    try:
        names = os.listdir(path)
    except FileNotFoundError:
        return [], None                  # no DM namespace yet — PROVEN empty
    except OSError as exc:
        return [], _Fault("dm-namespace", path, _reason(exc))
    return [chat.DM_PREFIX + n[:-6] for n in names
            if n.endswith(".jsonl")], None
def _all_lanes():
    """(lanes, faults) — BOTH namespaces, each able to say it could not look.
    Room enumeration gets the same treatment as the DM side: an unreadable
    chat dir is not a fleet with no rooms."""
    faults = []
    try:
        rooms = list(chat.list_rooms())
    except OSError as exc:
        rooms = []
        faults.append(_Fault("room-namespace", chat.chat_dir(), _reason(exc)))
    dm, fault = _dm_lanes()
    if fault is not None:
        faults.append(fault)
    return rooms + dm, faults
def _seat_checked(name, snap=None):
    """(is a known roster seat, THE ROSTER READ FAILED) — never a bare bool.

    FALSE MEANS UNRESOLVED, NEVER DEAD. The roster is not a census of who
    exists: a row is created only by an explicit `helm chat join` and nothing
    repairs a missing one, so a working seat is unresolvable for the whole
    window between spawning and joining. A caller that narrates this answer
    must say what the roster could not resolve, not what is out there.

    AND IT RETURNS A PAIR BECAUSE THE PREDICATE FORM WAS THE BUG. This was
    `_is_seat`, one line over the fail-open `roster()`: pk.read_json swallows
    missing, unreadable, malformed and wrong-shaped alike and returns {}, so
    an unreadable roster made EVERY mention resolve to "no such seat" and the
    surface asserted absence from evidence it did not have — the whole fleet
    at once, silently. A bool has nowhere to put "I could not look", which is
    why the failure had to be laundered into a False. roster_checked draws the
    line the fail-open reader erases: a MISSING file is a proven-empty roster
    (failed False, so a genuine miss still reads as a miss); only unreadable
    or malformed state is failed.

    AND THE MALFORMED-ROW CRASH WAS NOT THIS FUNCTION'S TO FIX. `{"recipR":
    []}` took the whole command down with an AttributeError (reproduced at the exact
    tip) and my first cure added a non-mapping branch HERE. It was DEAD CODE:
    roster_checked validates ALL-OR-NOTHING (seats_roster.py:84 — one invalid
    row fails the whole file), so past the guard below every row is already a
    dict and the branch could never run. The crash came from the FAIL-OPEN
    `roster()` inside _recipient_cursor, and it is fixed there, at the reader
    that actually returns unvalidated rows. Measured, not reasoned: the arm
    asserting a row-level diagnosis failed against the real file, which is how
    the dead branch surfaced before it shipped.

    `snap` is the caller's already-taken (roster, failed) pair: one command
    means one acquisition, and two rows in one render can no longer be judged
    against two different snapshots."""
    if snap is None:
        from .seats_roster import roster_checked
        snap = roster_checked()
    known, failed = snap
    if failed:
        return False, True
    return any(recipient_matches(name, key) for key in known), False
def _recipients(m, r=None, include_unresolved=False):
    """The seats a row DIRECTLY ASKS — whose consume state the sender tracks.
    A DM names exactly one; otherwise the roster seats @mentioned plus a reply's
    parent author (rfrom). Reaction additions wake their target author through
    deliverable(), but remain attention-only signals: no ACTED debt is created.
    ack / react / ambient rows therefore address nobody here. Roster-resolved,
    broadcast tokens dropped: only a seat with a cursor can be SEEN or ACTED.
    include_unresolved (the sender's pending view ONLY — never ack's
    authorization path) also yields a @mention that resolves to NO roster
    seat: an address this ROSTER cannot ground, kept VISIBLE not silently
    dropped. consume_state renders it 'unresolved' — a statement about what
    could be resolved, never about whether anyone is there."""
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
    # THE REPLY-PARENT GETS THE SAME TREATMENT AS AN @MENTION, and it did
    # not, which was strictly worse than the wording this lane set out to fix.
    # `keys` is built from the fail-open roster, so a corrupt roster empties
    # it — and a reply-only recipient (no @mention anywhere in the text) then
    # matched nothing and was DROPPED before consume_state could answer.
    # Measured: one unreadable roster turned a live obligation into
    # `total=0, items=[]`, which the CLI prints as "nothing outbound is
    # waiting — every addressed message you sent is consumed (acted)". A
    # FALSE ALL-CLEAR with a checkmark, for the whole fleet at once. The
    # mention branch above already survives this via include_unresolved;
    # keeping the address here lets consume_state render UNKNOWN instead of
    # the row ceasing to exist.
    rf, rf_err = _canonical_recipient(m.get("rfrom"))
    rf_cf = str(rf or "")
    if rf_cf and rf_cf not in seen:
        k = keys.get(rf_cf)
        if k is None and include_unresolved and rf_err is None:
            k = rf
        if k:
            out.append(k)
    return out
def _row_offsets(room):
    """(dev, ino, [(row, end_off)], fault) for one lane — every complete row
    and the byte offset PAST it, the same identity the delivery cursor commits
    (codex H5). Lets a row be tested against a recipient's cursor.off without
    moving it. Every chat row ends in a newline (chat._append), so there is no
    partial tail to mis-measure.

    COMPLETENESS IS WHOLE-LANE, AND OPENING IS NOT READING. An undecodable
    NONEMPTY chunk was silently dropped, so a lane holding one truncated row
    returned rows=[] and read as a lane that opened fine and had nothing in
    it — pending answered total=0 over a corrupt file (measured).
    One bad chunk means no NEGATIVE claim about this lane is safe: nothing
    here can say a row is absent, because the bytes that would have carried
    it are the bytes that did not parse.

    THE ROWS THAT PARSED STAY, as positive evidence. Absence cannot be
    certified, but presence still can, and throwing away what WAS read would
    hide real obligations to punish the file for the one row it lost."""
    path = chat.room_path(room)
    try:
        with open(path, "rb") as f:
            st = os.fstat(f.fileno())
            data = f.read()
    except OSError as exc:
        return None, None, [], _Fault("lane", room, _reason(exc))
    out, pos, undecodable = [], 0, 0
    for chunk in data.split(b"\n"):
        end = pos + len(chunk) + 1
        if chunk:
            row = chat._msg(chunk.decode("utf-8", errors="replace"))
            if row is None:
                undecodable += 1
            else:
                out.append((row, end))
        pos = end
    return (st.st_dev, st.st_ino, out,
            _Fault("row", room, "undecodable") if undecodable else None)
def _recipient_cursor(room, seat, r=None):
    """(cursor, fault) for one lane — the recipient's newest session's, with a
    seat-level fallback (roster_report's read pattern). A None cursor and no
    fault means the seat has no ground here (never joined / not tracked): then
    it has NOT seen the row.

    THE FALLBACK IS EARNED, NOT ASSUMED. It used to be `session or seat`, so an
    unreadable session cursor silently produced the STALE seat-level one and
    the caller could not tell that from "no session cursor exists". Combined
    with an unreadable roster losing the session binding entirely, that made a
    consumed row read `sent` off a cursor at offset 0 (reproduced at the exact tip).
    A fallback SELECTED BY the failed input can never be evidence the failure
    was harmless — so it is admissible only after a PROVEN absence.

    `r` is the caller's ALREADY-READ roster. Without it this re-read the file
    once per recipient per row: a 10-row probe measured 11 roster() calls and
    10 roster_checked() calls, so one render could judge two rows against two
    different snapshots AND paid O(messages x roster) to do it.

    NON-MAPPING ROWS ARE SKIPPED rather than dereferenced. `{"seat": []}` is
    parseable, and `.get` on that list crashed the whole command. pending()
    now hands down the CHECKED snapshot, where no such row survives — so this
    guard is for the STANDALONE caller that passes no `r` and falls back to
    the fail-open `roster()`, which returns rows nothing validated. That path
    is the one that crashed and it is still reachable from every direct
    caller."""
    r = roster() if r is None else r
    row = next((value for key, value in r.items()
                if isinstance(value, dict) and recipient_matches(key, seat)),
               {})
    session = row.get("session")
    if session:
        cur, fault = _cursor_checked(room, seat, session)
        if fault is not None:
            return None, fault      # a read that FAILED earns no fallback
        if cur is not None:
            return cur, None
        # cur None with NO fault: the session cursor is PROVABLY absent, which
        # is the only state that licenses the seat-level one.
    return _cursor_checked(room, seat)
def consume_state(m, room, recipient, dev=None, ino=None, end_off=None,
                  acks=None, snap=None, faults=None):
    """(state, ackstate) for ONE (recipient, row):
    'acted' | 'seen' | 'sent' | 'unresolved'.
    READ-ONLY over the existing plumbing — no cursor moves, no second ledger:
      acted  an ack row (this recipient, this row id) exists in the lane
      seen   the recipient's cursor passed the row's end offset, OR the seat
             was active in a strictly-later second than the row (touch_seen) —
             BUT only for a row ABOVE the join baseline (a row at/below base
             was never surfaced, so neither SEEN path may fire on it)
      sent   the row was written and neither holds — the stranded state
      unresolved  a room @mention this ROSTER cannot ground: no cursor
             anywhere and no roster row — surfaced distinctly, never lost.
             A statement about what helm could resolve, NOT about whether
             anyone is there (see _seat_checked)
    `acks` = {(target_id, from_cf): ackstate} for the lane; dev/ino/end_off
    are that same lane read (all recomputed when a caller omits them)."""
    tid = m.get("id")
    rcf, _ = _canonical_recipient(recipient)
    rcf = str(rcf or "")
    if acks is None or dev is None:
        # A ROW-LEVEL fault does not change THIS row's verdict: it says no
        # NEGATIVE claim about the lane is safe, while the row in hand already
        # parsed and its cursor comparison is positive evidence. A lane that
        # would not OPEN still arrives as dev=None and is handled below.
        dev, ino, rows, _lane_fault = _row_offsets(room)
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
    # ONE SNAPSHOT, BOTH QUESTIONS, AND READABILITY IS THE FIRST OF THEM.
    # This asked for the CURSOR first and consulted the roster only when the
    # cursor came back None. An unreadable roster fail-opens to {}, which
    # LOSES the recipient's session binding, so the lookup matched a stale
    # SEAT-level cursor instead and returned non-None — the failure branch was
    # skipped entirely and the row reported `sent` while the real session
    # cursor proved it consumed (exact tip: seat off=0, session
    # off=855, row end 855, verdict `sent`). The fallback cursor is SELECTED
    # BY the input that failed, so it can never be the evidence that the
    # failure did not matter — it is admissible only after PROVEN session
    # absence. DMs are covered too: a DM's cursor resolves through the same
    # session binding, so the same unreadable roster misreads it identically,
    # and exempting them would leave the class half-fixed.
    #
    # AN UNREADABLE ROSTER IS NOT AN ABSENT SEAT. Same law the lane read two
    # screens down already follows — an unreadable lane is UNKNOWN, never a
    # silent empty — applied to the other input this decision takes. Without
    # it one unreadable roster makes every mention in the fleet render as an
    # address nobody answers: a confident claim built on the single moment we
    # could not measure.
    found, roster_failed = _seat_checked(recipient, snap)
    if roster_failed:
        if faults is not None:
            faults.append(_Fault("roster", "the seat roster", "unreadable"))
        return "unknown", None
    cur, cur_fault = _recipient_cursor(room, recipient,
                                       snap[0] if snap else None)
    if cur_fault is not None:
        # A CURSOR NOBODY COULD READ DECIDES NOTHING. Falling through here
        # would compare the row against a cursor we do not have and call the
        # result SENT or SEEN — a measured state from an unmeasured input.
        if faults is not None:
            faults.append(cur_fault)
        return "unknown", None
    if cur is None and not m.get("dm") and not found:
        # no cursor ground and no roster row: the address this roster
        # cannot RESOLVE — kept VISIBLE, never false-SEEN, never "dead"
        return "unresolved", None
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
    if below_base:
        # BASELINED — NEVER SURFACED AND NEVER WILL BE, which is not SENT.
        # A row at or below the join baseline was skipped by deliver on
        # purpose (pre-join backlog does not flood a joining seat) and no
        # future delivery will reach back for it. Calling that SENT told the
        # sender "written, never reached their cursor" and pointed them at the
        # SENT remedy — `helm chat ack` — which the RECIPIENT cannot run,
        # because they were never shown the row. task/914 item 4, and agreed
        # in review as the FIFTH polarity of the rendered-obligation law:
        # every obligation must name a clearer who can actually clear it.
        # The honest remedy here is SENDER-side — repost or retarget, or an
        # explicit backfill — so the state has to be distinguishable before
        # any surface can say so.
        return "baselined", None
    if same and end_off is not None and end_off <= cur.get("off", 0):
        return "seen", None
    ls = last_seen(recipient)
    mts = _ts_epoch(m.get("ts"))
    if ls is not None and mts is not None and int(ls) > mts:
        return "seen", None       # touch_seen fallback (a later second)
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
    lanes, faults = _all_lanes()
    # ENUMERATING A LANE IS NOT READING IT, and fixing only the first half is
    # what a reviewer caught here. _all_lanes reports lanes it could not LIST;
    # chat.read then fail-opens per lane, returning [] for a room that exists
    # and will not open, and silently skipping torn lines. So a lane could
    # enumerate perfectly, contribute nothing, and leave `faults` empty — and
    # the absence branch below would then swear the id matches nothing, over a
    # room it never actually read. read_checked reports both, and each becomes
    # an ordinary fault so the existing refusal covers it with no new branch.
    faults = list(faults)
    for room in lanes:
        rows, _total, unread = chat.read_checked(room)
        if unread:
            faults.append(_Fault(unread, room, "the lane could not be read"))
        for m in rows:
            rid = str(m.get("id") or "")
            if rid and (rid == tid or (len(tid) >= 4 and rid.startswith(tid))):
                hits.append((m, room, rid == tid))
    exact = [h for h in hits if h[2]]
    # AN EXACT MATCH PROVES ITSELF; A PREFIX MATCH MAKES A CLAIM ABOUT EVERY
    # LANE. "this id exists here" needs only the row in hand, so a fault
    # elsewhere cannot unseat it. "no OTHER row starts with these characters"
    # is a statement about the whole namespace, and an unread lane is exactly
    # where the colliding row would hide — so the prefix shortcut is withdrawn
    # when anything went unread, rather than resolving to a row that merely
    # looks unique. Typing four more characters is cheap; acking the wrong
    # message is not.
    if not exact and hits and faults:
        return None, None, (
            "%r resolves by PREFIX and uniqueness cannot be proven — %s could "
            "not be read, which is where a second match would hide. Use the "
            "full id, or repair that and retry"
            % (tid, ", ".join(sorted({f.kind for f in faults}))))
    hits = exact or hits
    if not hits:
        # A NAMESPACE WE COULD NOT ENUMERATE IS NOT A NAMESPACE WITHOUT THE
        # ROW. "no message matches" sends the caller to fix an id that may be
        # perfectly good, over lanes nothing looked in.
        if faults:
            return None, None, (
                "cannot prove %r is absent — %s could not be read; repair "
                "that and retry rather than re-typing the id"
                % (tid, ", ".join(sorted({f.kind for f in faults}))))
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
    # ACK DISCHARGES AN OBLIGATION — one of the two acts resolve_identity's
    # docstring forbids BY NAME on a DERIVED identity. One definition of "who
    # is this attributed to" (helm.actors.attributed): a capability from the
    # CLI door, an explicit on-behalf-of name, and AMBIENT REFUSES instead of
    # minting. The frozen first attempt guarded only the ambient branch, and
    # the CLI leg passed `who` straight past it — with a DERIVED name in it,
    # because `chat._seat_actor` answered `whoname()`.
    from . import actors
    seat, _aerr = actors.attributed(who, session, act="acknowledge a row")
    if _aerr:
        return None, _aerr
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
    # row identity; the signer is whatever the signing gate resolves
    # (profile=None -> chat._post_identity -> cell.signing_identity) — never
    # the --seat claim, so a seat cannot forge a signed ACK clearing another
    # seat's obligation (a cross-family read).
    row = chat.post(note or "", room=room, who=seat,
                    ack=tid, ackstate=state)
    return {"target": m, "room": room, "state": state, "row": row,
            "dup": False}, None
def pending(seat=None, session=None, cap=50):
    """(items, total, lanes): the sender's outbound ADDRESSED rows not yet
    ACTED, one entry per (recipient, row) — SENT-not-SEEN, SEEN-not-ACTED, or
    UNRESOLVED (a mention this roster cannot ground). Derived read-only off the
    recipients' cursors + touch_seen; an acked pair drops off (consumed).

    THE CAP BELONGS TO MESSAGES, AND A DIAGNOSTIC IS NOT A MESSAGE. An
    unreadable lane still yields one 'unknown' entry (fail CLOSED — never a
    silent empty that reads as all-clear), but those entries carry NO
    timestamp, and while they shared one list with real rows they sorted to
    the FRONT (`None` became "") and then consumed the entire cap. Measured by
    a probe on the exact tip: 50 unreadable lanes plus ONE real unresolved
    row rendered `51 addressed rows … showing the 50 oldest`, and the single
    actionable obligation was the row that did not fit. Diagnostic uncertainty
    was crowding out the work it was supposed to annotate, and the header made
    two false claims doing it — sentinels are not addressed rows, and
    timestamp-less entries are not proven older than anything.

    So they travel as a THIRD value: `items`/`total` count addressed MESSAGES
    only, and `lanes` carries the unreadable-lane diagnostics whole (bounded
    by rooms on the box, not by traffic). `kind` is the canonical
    discriminator — never the nullable `ts`, because a message may
    legitimately lack a timestamp and must not be filed as a diagnostic.
    Messages sort oldest-first; any without a timestamp sort LAST and are
    never described as oldest."""
    _warn_disagreement(session, "pending")   # read-only: warn, never block
    # RENDER PATH, derive_seat kept deliberately: this lists PENDING rows and
    # mutates nothing (the warn above is "read-only: warn, never block"). The
    # admission door is for acts, not listings.
    seat = seat or seat_for_session(session) or derive_seat(session)
    scf, _ = _canonical_recipient(seat)
    scf = str(scf or "")
    # ONE CHECKED ACQUISITION FOR THE WHOLE COMMAND, threaded into every
    # recipient extraction, cursor lookup and consume_state below. The old
    # path took a snapshot and then re-read per row: a 10-row probe measured
    # 11 roster() calls plus 10 roster_checked() validations — N+1 fail-open
    # reads and N strict ones, any two of which could disagree, to answer one
    # question about one instant (exact tip). It is the settled
    # seats_delivery:735 template, at the third door this week.
    from .seats_roster import roster_checked
    snap = roster_checked()
    r = snap[0] or {}
    items = []
    lanes, faults = _all_lanes()
    if snap[1]:
        faults.append(_Fault("roster", "the seat roster", "unreadable"))
    for room in lanes:
        dev, ino, rows, fault = _row_offsets(room)
        if fault is not None:
            # FAIL CLOSED — BUT AS A FAULT, NOT AS A FAKE ROW. This pushed a
            # synthetic timestamp-less "message" into items, which then sorted
            # to the FRONT and ate the whole cap. A lane nobody could read is
            # not an addressed row and never was; it belongs in the
            # completeness verdict beside the answer, not inside it.
            faults.append(fault)
        if dev is None:
            continue          # nothing to iterate; the fault carries the why
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
                row_faults = []
                st, _a = consume_state(m, room, rc, dev, ino, end_off, acks,
                                       snap=snap, faults=row_faults)
                faults.extend(row_faults)
                if st == "acted":
                    continue
                shown = getattr(rc, "display", str(rc))
                items.append({"id": m.get("id"), "room": room, "to": shown,
                              "kind": "message",
                              "state": st, "ts": m.get("ts"),
                              # WHICH INPUT WE COULD NOT READ, FROM THE FAULT
                              # ITSELF rather than a guess. Three unknowns now
                              # reach this surface — the LANE (its own fault
                              # above), the ROSTER, and the CURSOR — and a
                              # reader who cannot tell them apart opens the
                              # wrong file. This line hardcoded "roster" back
                              # when the roster was the only one it could be,
                              # and a hardcoded cause survives exactly until
                              # the second cause arrives.
                              "why": ("%s unreadable" % row_faults[0].kind
                                      if st == "unknown" and row_faults
                                      else "an input was unreadable"
                                      if st == "unknown" else None),
                              "dm": bool(m.get("dm")),
                              "text": m.get("text") or ""})
    # UNTIMESTAMPED SORTS LAST AND IS NEVER CALLED OLDEST. The old key mapped
    # `None` onto the empty string, which sorts BEFORE every real timestamp —
    # so the entries that knew the least about age went to the front of a list
    # the render then described by age. Only MESSAGES reach this list now, but
    # the guard stays: a real row may legitimately lack a ts, and it must not
    # inherit the ordering claim either.
    items.sort(key=lambda x: (x.get("ts") is None, str(x.get("ts") or "")))
    # DEDUP BY IDENTITY, NEVER BY KIND. consume_state reports the same roster
    # or cursor fault once per ROW it blocked, so one unreadable roster would
    # otherwise print N identical lines and drown the distinct ones. Equal
    # tuples collapse; every DISTINCT provenance survives, which is the
    # property that matters — two different lanes failing must never merge
    # into a single "lane unreadable".
    return items[:cap], len(items), list(dict.fromkeys(faults))


def render_pending(seat, session):
    """Render ONE sender's outbound pending view. -> exit code.

    LIVES BESIDE ITS DATA, not across a module boundary: `pending` and
    `fault_lines` are right here, and the renderer that decides what each
    state MEANS to an operator has to move whenever they do. It sat in
    seats_cli's 592-line `cmd` dispatcher, which put the legend that
    explains UNRES/BASED/UNKN a file away from the reader that produces
    them — and pushed seats_cli over its 1000-line budget the moment
    trunk grew (the compose leg caught exactly that intersection).

    `session` is a PARAMETER rather than an import: `_env_session` lives
    in seats_cli, and importing it here would close a cycle for one
    lookup the caller already has.
    """
    items, total, faults = pending(seat=seat, session=session)
    # THE ALL-CLEAR NEEDS BOTH EMPTY. Once diagnostics left this list,
    # testing `items` alone would print "every message is consumed ✓" on a
    # box where every lane was UNREADABLE — the exact false green the
    # fail-closed reader exists to prevent, reintroduced by its own fix.
    if not items and not faults:
        print("helm chat: nothing outbound is waiting — every addressed "
              "message you sent is consumed (acted) ✓")
        return 0
    now = time.time()
    n_sent = sum(1 for it in items if it["state"] == "sent")
    n_seen = sum(1 for it in items if it["state"] == "seen")
    n_unres = sum(1 for it in items if it["state"] == "unresolved")
    n_unk = sum(1 for it in items if it["state"] == "unknown")
    head = "%d SENT-not-SEEN, %d SEEN-not-ACTED" % (n_sent, n_seen)
    if n_unres:
        head += ", %d UNRESOLVED (no roster row)" % n_unres
    n_based = sum(1 for it in items if it["state"] == "baselined")
    if n_based:      # the header must not swallow it into the bare total
        head += ", %d BASELINED (yours to repost)" % n_based
    if n_unk:
        # NAMES NO CAUSE IT DOES NOT KNOW. This said "UNREADABLE lane"
        # for every unknown, including the ones where the ROSTER was the
        # failed input — the same mislabel that was cured on the row and
        # left standing in the summary above it. Each row names which
        # input failed; the header only counts them.
        head += ", %d UNKNOWN (a read failed)" % n_unk
    # NO SILENT CAP AND NO AGE CLAIM IT CANNOT SUPPORT: the cap counts
    # MESSAGES, and "oldest" holds only when every shown row carries a ts.
    shown = ""
    if total > len(items):
        dated = all(it.get("ts") for it in items)
        shown = " — showing %d%s; %d more not listed" % (
            len(items), " (oldest)" if dated else "",
            total - len(items))
    # THE DIRECTION IS ON THE RUNNING LINE, NOT ONLY ON THE QUIET ONES.
    # This verb is the OUTBOUND view and three places in the tree say so —
    # the help text, the refusal branch for a stray selector, and the
    # all-clear above. The refusal fires only on a malformed call and the
    # all-clear only when the list is EMPTY, so the one moment the direction
    # was stated was the one moment there was nothing to misread. A reader
    # comparing this number against `catchup`'s "parked N" is comparing their
    # OUTBOX against their INBOX: the two sets are disjoint by construction
    # (and the pointer says PREVIEWS, not parks — `catchup` takes apply=False
    # by default and protects mentions behind --including-mentions, so a line
    # telling a reader it "parks" would be wrong about the verb it names)
    # (`deliverable` refuses a seat's own posts; this verb skips every row the
    # seat did not send), so the difference between them is not a discrepancy
    # and cannot be reconciled. Measured on one seat in one instant: catchup
    # would park 23, the roster's inbound count read 0, this read 175.
    #
    # AND THE UNIT IS PAIRS, NOT ROWS. `items` is one entry per
    # (recipient, row) — a broadcast to five seats counts five — so calling
    # them "rows" overstates the backlog by the fan-out. Measured: 151 rows
    # render as 174 pairs.
    print("helm chat pending (as %s) — OUTBOUND: %d addressed %s YOU SENT "
          "awaiting consume by the recipient (%s)%s. This is NOT your inbox: "
          "`helm chat catchup` PREVIEWS what is addressed TO you (it parks "
          "nothing without --apply, and mentions need --including-mentions), "
          "and `helm chat seats` shows what is waiting FOR you."
          % (_seat_label(seat), total,
             "delivery" if total == 1 else "deliveries", head, shown))
    for it in items:
        where = ("dm" if it["dm"] else "main" if it["room"] == "main"
                 else "#" + it["room"])
        if it["state"] == "unknown":
            # TWO DIFFERENT UNKNOWNS reach this line — an unreadable
            # LANE (no row, no recipient) and an unreadable ROSTER (both
            # present) — so it names WHICH input failed. The extra space
            # matches the %-5s column below, which UNRES widened.
            print("  ⚠ UNKN  %-8s   %-14s %-7s        %s — "
                  "state UNKNOWN, NOT consumed"
                  % (str(it["id"] or "")[:8],
                     _seat_label(it["to"]) if it.get("to") else "?",
                     # NOT "lane unreadable" any more: an unreadable lane
                     # is a FAULT now and never reaches a row, so naming
                     # it here would send the reader to a file that is
                     # fine. A row-level unknown says which input failed.
                     where, it.get("why") or "an input was unreadable"))
            continue
        # UNRES IS NOT GONE. The state means "no roster row answers this
        # @name" — what helm could RESOLVE, not what is out there. The two
        # coincide only if the roster is a census, and it is not: a row is
        # created solely by `helm chat join`. Measured: a seat answered a
        # row one minute after this surface called it GONE.
        glyph, st = (("◌", "UNRES") if it["state"] == "unresolved"
                     else ("○", "SENT") if it["state"] == "sent"
                     else ("▣", "BASED") if it["state"] == "baselined"
                     else ("◐", "SEEN"))
        # AN UNDATED ROW GETS NO AGE. `or now` made the delta ZERO and
        # printed "<1m" — confidently calling an obligation with no
        # timestamp the FRESHEST thing on screen. pending() is explicit
        # that a message may legitimately lack a ts, sorts those LAST and
        # "never describes them as oldest" (:527-533); this renderer then
        # described them as newest. A re-read caught it in the moved code.
        stamp = _ts_epoch(it["ts"])
        # `is not None`, NOT truthiness: _ts_epoch reserves None for garbage
        # and returns 0 for 1970-01-01T00:00:00Z, which is a VALID stamp.
        # `if stamp` called that undated. A re-read caught it, and it is the
        # premise I filed twenty minutes earlier wearing a different mask —
        # the MEASURED case paying for the UNKNOWN one, at the boundary my
        # own pole (a 3-hours-ago stamp) was too far from to see.
        age = _fmt_age(now - stamp) if stamp is not None else "?"
        print("  %s %-5s %-8s → %-14s %-7s %4s  %s"
              % (glyph, st, str(it["id"] or "")[:8], _seat_label(it["to"]),
                 where, age, _clip(_scrub(it["text"]), 60)))
    for line in fault_lines(faults):     # what this screen could NOT read
        print(line)
    # EACH STATE CARRIES ITS OWN REMEDY, and only those that have one:
    # the old trailing "they close it: ack" also covered UNRES (nobody can
    # ack an unresolvable name) and UNKN (nothing was measured). SENT read
    # "recipient dead / away / wedged?" — a guess about a PERSON.
    print("  ○ SENT = written, never reached their cursor — they close it "
          "with `helm chat ack <id> done|blocked`\n"
          "  ◐ SEEN = surfaced, not acted — same remedy, theirs to run\n"
          "  ◌ UNRES = no roster row answers that @name — a typo, OR a "
          "live seat that has not joined; it resolves when they run "
          "`helm chat join`, not by anything you or they can ack\n"
          "  ▣ BASED = posted BEFORE they joined, so deliver skipped it "
          "and never reaches back; they cannot ack a row nobody showed "
          "them. THIS ONE IS YOURS — repost or re-address it\n"
          "  ⚠ UNKN = a read failed, so this row's state was never "
          "measured; the line names which input\n"
          "  age ? = the row carries NO timestamp — undated, not new")
    return 0

