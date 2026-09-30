#!/usr/bin/env python3
"""helm meld, the STANDING mode: one open room per working pair (task/3560).

Premise always-on-slack-like-chat-is-an-owner-priority: a working pair (lead
and worker, lead and codex) keeps ONE room open across tasks, pings back and
forth any time, pulls a third seat in when it needs one, and nobody blocks
waiting on a reply because each seat's beacon wakes it. The capped meld
(helm/meld.py: CAP exchanges, a RECV_TIMEOUT_S blocking recv, sealed on
DONE) stays what it is, the one-question huddle. This mode shares its verbs
and none of its bounds:

  * NO EXCHANGE CAP and no seal: [DONE] is a word in a message, never the end
    of the room.
  * SAY NEVER BLOCKS AND ALWAYS WAKES: every message @mentions the room's
    other participants, so the delivery lane and each seat's beacon carry it
    (the comms stack's wake layer, not a poll loop in this verb).
  * RECV NEVER WAITS: it returns what arrived since this seat's cursor, or
    says nothing is new, at once.
  * A GUEST FOR ONE EXCHANGE: a member pulls a third seat in with a question;
    the guest's first message is followed by its own LEFT row, and from then
    on its rows are ignored like any outsider's.
  * A LATER MEMBER: a member adds a seat, which can then join.

THE ROOM IS THE ARTIFACT (two-source model). Membership is a fold over the
room's own control rows, each counted only when its author is a member at
that point: `[STANDING-OPEN]`, `[STANDING-ADD]`, `[STANDING-GUEST]`,
`[STANDING-LEFT]`, `[STANDING-JOINED]` and `[STANDING-ROUND]` (a dispatch
row's round). Messages are `[STANDING] ...`. The only other state is each
seat's read cursor, a derived convenience beside the room.

ROTATION CARRIES THE FOLD. Past chat.SIZE_CAP the oldest half of the room
rotates out, [STANDING-OPEN] first, and a fold over what is left would find
nobody in it. So chat._rotate hands the rows it is about to drop to
carry_rotation, which folds them and writes a checkpoint beside the room
(`<room>.standing-carry.json`) keyed by the first row that stays; the fold
resumes from the checkpoint keyed by the file's first row. Indices are
absolute across rotations (`base` counts the rows rotated out), so a cursor
saved before a rotation still names the same row after it. Every rotation
ATTEMPT writes a checkpoint before it installs, so the one the file's first
row resumes from is never trimmed. A checkpoint file that cannot be read or
written keeps the room unrotated.

A ROOM BOTH PAIR SEATS LEFT IS REOPENED by either of them: an OPEN row for
the same pair counts again once neither of the pair is a member, and a later
member still in the room stays. An OPEN for another pair (a name collision)
counts for nothing, and the open says so.

THE NAME IS A PURE FUNCTION OF THE PAIR (`meld-0-standing-<a>-<b>`, the two
seats sorted), so a pair cannot have two standing rooms, and a dispatch
between the pair finds its room without being told (review_door).
"""
import hashlib
import os
import re

from . import chat, home, pk

PREFIX = "meld-0-standing-"
_ROOM = re.compile(r"meld-0-standing-[a-z0-9-]{1,44}\Z")
_PART = 14
_KINDS = ("OPEN", "ADD", "GUEST", "LEFT", "JOINED", "ROUND")
_LEAD = r"\A(?:@[\w.-]+\s+)*"
_CONTROL = re.compile(_LEAD + r"\[STANDING-(%s)\] (.*)\Z" % "|".join(_KINDS),
                      re.S)
_MESSAGE = re.compile(_LEAD + r"\[STANDING\] ", re.S)
_FIELD = re.compile(r"\b(pair|seat|by|row|verb)=([\w.,-]+)")
DISCIPLINE = ("a STANDING room for this pair: no exchange cap, never sealed, "
              "say never blocks (the others are woken by their beacons), recv "
              "returns at once. A one-question huddle is still a capped meld.")


def is_standing_room(room):
    """Is `room` a pair's standing room?"""
    return bool(_ROOM.fullmatch(str(room or "")))


def room_for(a, b):
    """The standing room of the pair {a, b}, in either order."""
    raw = sorted([str(a), str(b)], key=lambda s: (s.casefold(), s))
    parts = [re.sub(r"[^a-z0-9-]", "-", s.casefold()).strip("-") or "seat"
             for s in raw]
    name = PREFIX + "-".join(parts)
    if any(p != s for p, s in zip(parts, raw)) or len(name) > 60:
        digest = hashlib.blake2b("\n".join(raw).encode("utf-8"),
                                 digest_size=4).hexdigest()
        name = "%s%s-%s-%s" % (PREFIX, parts[0][:_PART].strip("-"),
                               parts[1][:_PART].strip("-"), digest)
    return name


def _self_seat():
    from . import meld
    return meld._self_seat()


def _seat_arg(name, via):
    try:
        return home.validate_seat_arg(str(name or "").lstrip("@"))
    except home.SeatNameError as e:
        raise SystemExit("helm chat %s: %s" % (via, e))


def _cf(name):
    return str(name or "").casefold()


_STATE = ("pair", "members", "guests", "admitted", "rounds")
_CARRIES = 4      # checkpoints kept: an older one still keys a stale read


def _pair_in(pair, members):
    """Is either seat of `pair` a member?"""
    return bool({_cf(p) for p in pair} & {_cf(x) for x in members})


def _resume(rows, carries):
    """The rotation checkpoint the fold of `rows` starts from: the one keyed
    by the first row, else None (the room never rotated)."""
    if not rows or not isinstance(carries, list):
        return None
    head = chat.tkey(rows[0])
    for point in reversed(carries):
        if isinstance(point, dict) and point.get("head") == head \
                and isinstance(point.get("base"), int) \
                and all(isinstance(point.get(k), list)
                        for k in ("pair", "members", "guests", "rounds")) \
                and isinstance(point.get("admitted"), dict):
            return point
    return None


def fold(rows, carries=None):
    """{"pair", "members", "guests", "admitted", "rounds", "accepted",
    "base"} — the room's membership, read off its own control rows in order,
    resuming from the rotation checkpoint keyed by rows[0] when there is one.

    A row counts only when its author is a participant at that point, so a
    seat outside the room can neither add itself nor speak. `admitted` maps
    each participant to the index of the row that let it in (its first recv
    starts there); `rounds` lists (row12, verb) per dispatch round; and
    `accepted` is every index whose author was a participant, message or
    control. Indices are absolute: `base` rows rotated out before rows[0]."""
    out = {"pair": [], "members": [], "guests": [], "admitted": {},
           "rounds": [], "accepted": [], "base": 0}
    point = _resume(rows, carries)
    if point is not None:
        out.update(base=point["base"],
                   pair=[str(x) for x in point["pair"]],
                   members=[str(x) for x in point["members"]],
                   guests=[str(x) for x in point["guests"]],
                   admitted={str(k): v for k, v in point["admitted"].items()
                             if isinstance(v, int)},
                   rounds=[tuple(r) for r in point["rounds"]
                           if isinstance(r, list) and len(r) == 2])
    members, guests = out["members"], out["guests"]

    def inside(who):
        return _cf(who) in {_cf(x) for x in members + guests}

    def drop(who):
        for group in (members, guests):
            for x in list(group):
                if _cf(x) == _cf(who):
                    group.remove(x)
        out["admitted"].pop(_cf(who), None)

    for i, m in enumerate(rows or (), out["base"]):
        text, frm = m.get("text") or "", str(m.get("from") or "")
        if m.get("react") or not text or not frm:
            continue
        ctl = _CONTROL.match(text)
        if ctl is None:
            if _MESSAGE.match(text) and inside(frm):
                out["accepted"].append(i)
            continue
        # THE HEAD ONLY: fields after the first " | " are prose (a topic, a
        # question), and a field spelled there must never outrank the head's.
        head = ctl.group(2).split(" | ", 1)[0]
        kind, fields = ctl.group(1), dict(_FIELD.findall(head))
        if kind == "OPEN":
            pair = [p for p in fields.get("pair", "").split(",") if p]
            if len(pair) != 2 or _cf(frm) not in {_cf(p) for p in pair}:
                continue
            # A LATER OPEN counts only for the same pair and only once neither
            # of the pair is a member: either of them reopens a room both
            # left, and a later member still in it stays.
            if out["pair"] and (_pair_in(out["pair"], members)
                                or sorted(map(_cf, pair))
                                != sorted(map(_cf, out["pair"]))):
                continue
            out["pair"] = out["pair"] or pair
            for p in pair:
                drop(p)
                members.append(p)
                out["admitted"][_cf(p)] = i
        elif not inside(frm):
            continue
        elif kind in ("ADD", "GUEST"):
            seat = fields.get("seat")
            if not seat or _cf(frm) not in {_cf(x) for x in members} \
                    or inside(seat):
                continue
            (members if kind == "ADD" else guests).append(seat)
            out["admitted"][_cf(seat)] = i
        elif kind == "LEFT":
            if _cf(fields.get("seat")) != _cf(frm):
                continue
            out["accepted"].append(i)
            drop(frm)
            continue
        elif kind == "ROUND":
            out["rounds"].append((fields.get("row"), fields.get("verb")))
        out["accepted"].append(i)
    return out


def _carry_path(room):
    return os.path.join(chat.chat_dir(), "%s.standing-carry.json"
                        % pk.slug(room))


def _read(room):
    # THE ROWS FIRST, THEN THE CHECKPOINTS: a rotation between the two reads
    # leaves rows that the kept older checkpoint still keys.
    rows, total = chat.read(room)
    return rows, total, fold(rows, pk.read_json(_carry_path(room), None))


def carry_rotation(room, dropped, kept):
    """True once the fold over `dropped` (the rows chat._rotate is about to
    cut, oldest first) is checkpointed under the key of kept[0], the row
    that becomes the room's first. False keeps the room unrotated: a lost
    checkpoint would empty the room's membership."""
    import json
    try:
        # STRICT: a missing sidecar is a room that never rotated, but one
        # that will not read or parse is unknown, and a fold from scratch
        # over it would overwrite the membership with nobody.
        carries = pk.read_json(_carry_path(room), None, strict=True)
        if carries is None:
            carries = []
        elif not isinstance(carries, list):
            return False
        got = fold(dropped, carries)
        point = {k: got[k] for k in _STATE}
        point.update(head=chat.tkey(kept[0]) if kept else None,
                     base=got["base"] + len(dropped),
                     rounds=[list(r) for r in got["rounds"]])
        # THE RESUME POINT IS NEVER TRIMMED. This runs on every rotation
        # ATTEMPT, before the install; attempts that fail later each key a
        # new head, and trimming by age alone would evict the checkpoint
        # the room's real first row still resumes from.
        resume = _resume(dropped, carries)
        older = [c for c in carries if isinstance(c, dict) and c is not resume
                 and c.get("head") != point["head"]]
        keep = [resume] if resume is not None \
            and resume.get("head") != point["head"] else []
        room_left = _CARRIES - 1 - len(keep)
        pk.atomic_write(_carry_path(room), json.dumps(
            (older[-room_left:] if room_left > 0 else []) + keep + [point]))
        return True
    except Exception:                                   # noqa: BLE001
        return False


def participants(room):
    """[seat] — the room's members and guests now."""
    got = _read(room)[2]
    return got["members"] + got["guests"]


def _post(text, room, seat):
    from . import meld
    return meld._post(text, room, seat)


def _lock(room):
    from . import meld
    return chat._room_lock(meld._opening_lock_name(room))


def _mentions(names):
    return " ".join("@%s" % chat._dsan(n) for n in names)


def _need_room(room, via):
    if not is_standing_room(room):
        raise SystemExit("helm chat %s: %s is not a standing room (%s<a>-<b>)"
                         % (via, room, PREFIX))


def _need_member(got, room, seat, via, guest_ok=True):
    names = got["members"] + (got["guests"] if guest_ok else [])
    if _cf(seat) not in {_cf(x) for x in names}:
        raise SystemExit(
            "helm chat %s: %s is not %s room %s (members: %s%s)"
            % (via, chat._dsan(seat), "in" if guest_ok else "a member of",
               room, ", ".join(chat._dsan(x) for x in got["members"]) or
               "none; open it with helm chat %s standing <peer>" % via,
               "; guests: " + ", ".join(chat._dsan(x) for x in got["guests"])
               if got["guests"] else ""))


def known_seat(name):
    """Is `name` a seat this verb may pair a standing room with?

    The SAME roster read `helm dispatch send` uses to refuse an unknown
    recipient (seats_delivery.recipient_capability): one lookup,
    JOINED/ABSENT/UNKNOWN, not a second seat registry. JOINED and UNKNOWN both
    count as known — UNKNOWN is an empty or unreadable roster, and refusing on
    it would turn the recipient guard from fail-open into fail-closed, which
    every test that relies on an empty roster would flip. Only a POSITIVE
    ABSENT refuses: a populated roster holding no row for the name. A
    flag-shaped name is refused before the read: the seat-token class admits
    '-', so the capability alone would let --help pass as an ordinary name the
    roster has no row for, and a room opened with it is one nothing can speak
    in that the sweep never clears."""
    name = str(name or "")
    if name.lstrip("@").startswith("-"):
        return False
    from . import seats_delivery
    cap = seats_delivery.recipient_capability(name)
    return not cap["error"] and cap["membership"] != "ABSENT"


def open_room(peer, topic=None, seat=None, via="meld"):
    """(room, lines) — open this seat's standing room with `peer`, or say it
    is already open. Idempotent under the room's opening lock: the first
    OPEN row is the room's, and a second open posts nothing."""
    seat = seat or _self_seat()
    peer = _seat_arg(peer, via)
    if _cf(peer) == _cf(seat):
        raise SystemExit("helm chat %s: a standing room pairs you with "
                         "another seat, not yourself" % via)
    if peer.startswith("-"):
        raise SystemExit("helm chat %s: %r is a flag, not a seat name — a "
                         "standing room opens with a seat name"
                         % (via, peer))
    if not known_seat(peer):
        raise SystemExit(
            "helm chat %s: %r is not a known seat, so there is no room to open "
            "with it — `helm chat seats` lists the live seats" % (via, peer))
    room = room_for(seat, peer)
    with _lock(room):
        got = _read(room)[2]
        if _pair_in(got["pair"], got["members"]) \
                or (got["members"] and not got["pair"]):
            _need_member(got, room, seat, via, guest_ok=False)
            return room, ["STANDING-EXISTS room=%s members=%s" % (
                room, ",".join(chat._dsan(x) for x in got["members"]))] \
                + _next_lines(room, via)
        tail = " | " + " ".join(str(topic).replace("@", "").split()) \
            if topic else ""
        _post("@%s [STANDING-OPEN] pair=%s,%s by=%s | %s%s"
              % (chat._dsan(peer), seat, peer, seat, DISCIPLINE, tail),
              room, seat)
        # THE FOLD DECIDES, not the post: an OPEN from a pair other than the
        # room's (a name collision) is a row the fold ignores.
        if _cf(seat) not in {_cf(x) for x in _read(room)[2]["members"]}:
            raise SystemExit(
                "helm chat %s: %s belongs to the pair %s; this open did not "
                "count" % (via, room,
                           ",".join(chat._dsan(x) for x in got["pair"])))
    return room, ["STANDING-OPENED room=%s pair=%s,%s" % (
        room, chat._dsan(seat), chat._dsan(peer)),
        "%s is woken by this row's @mention (its beacon, or its next tool "
        "boundary)" % chat._dsan(peer)] + _next_lines(room, via)


def _next_lines(room, via):
    return ["  say:  helm chat %s say %s \"...\"   (never blocks; wakes the "
            "others)" % (via, room),
            "  read: helm chat %s recv %s   (returns at once)" % (via, room),
            "  pull a third seat for one exchange: helm chat %s pull %s "
            "<seat> \"<question>\"" % (via, room),
            "  add a member: helm chat %s invite <seat> --into %s"
            % (via, room)]


def add(room, who, seat=None, via="meld"):
    """(lines) — a member adds `who` as a member; it can then join."""
    seat = seat or _self_seat()
    _need_room(room, via)
    who = _seat_arg(who, via)
    with _lock(room):
        got = _read(room)[2]
        _need_member(got, room, seat, via, guest_ok=False)
        if _cf(who) in {_cf(x) for x in got["members"] + got["guests"]}:
            return ["STANDING-MEMBER room=%s seat=%s (already in)"
                    % (room, chat._dsan(who))]
        _post("@%s [STANDING-ADD] seat=%s by=%s | join: helm chat %s join %s"
              % (chat._dsan(who), who, seat, via, room), room, seat)
    return ["STANDING-ADDED room=%s seat=%s" % (room, chat._dsan(who)),
            "%s is woken by the @mention; it joins with: helm chat %s join %s"
            % (chat._dsan(who), via, room)]


def pull(room, guest, question, seat=None, via="meld"):
    """(lines) — a member pulls `guest` in for ONE exchange: the question is
    the GUEST row itself, and the guest's first message ends its visit."""
    seat = seat or _self_seat()
    _need_room(room, via)
    guest = _seat_arg(guest, via)
    question = " ".join(str(question or "").split())
    if not question:
        raise SystemExit("helm chat %s: pull wants the question the guest "
                         "is pulled in to answer" % via)
    with _lock(room):
        got = _read(room)[2]
        _need_member(got, room, seat, via, guest_ok=False)
        if _cf(guest) in {_cf(x) for x in got["members"] + got["guests"]}:
            raise SystemExit("helm chat %s: %s is already in %s"
                             % (via, chat._dsan(guest), room))
        _post("@%s [STANDING-GUEST] seat=%s by=%s | for ONE exchange: %s | "
              "answer with: helm chat %s say %s \"...\""
              % (chat._dsan(guest), guest, seat, question, via, room),
              room, seat)
    return ["STANDING-GUEST room=%s seat=%s (one exchange; it leaves after "
            "its answer)" % (room, chat._dsan(guest))]


def _cursor_path(room, seat):
    from . import seats
    return os.path.join(chat.chat_dir(), "%s.standing.%s.json"
                        % (pk.slug(room), seats._seat_key(seat)))


def _cursor(room, seat, got):
    raw = pk.read_json(_cursor_path(room, seat), None)
    start = got["admitted"].get(_cf(seat), 0)
    if isinstance(raw, dict) and isinstance(raw.get("idx"), int) \
            and not isinstance(raw.get("idx"), bool) \
            and raw.get("since") == start:
        return raw["idx"]
    return start


def _save_cursor(room, seat, idx, got):
    import json
    pk.atomic_write(_cursor_path(room, seat), json.dumps(
        {"room": room, "self": seat, "idx": idx,
         "since": got["admitted"].get(_cf(seat), 0)}))


def join(room, seat=None, via="meld"):
    """(lines) — a participant (a member, a later member or a guest) says it
    is here. Its reading starts at the row that let it in."""
    seat = seat or _self_seat()
    _need_room(room, via)
    rows, _total, got = _read(room)
    _need_member(got, room, seat, via)
    _post("[STANDING-JOINED] seat=%s" % seat, room, seat)
    guest = _cf(seat) in {_cf(x) for x in got["guests"]}
    return ["STANDING-JOINED room=%s seat=%s%s" % (
        room, chat._dsan(seat), " (guest for one exchange)" if guest else ""),
        "next: helm chat %s recv %s   (returns at once)" % (via, room),
        "the whole log: helm chat read --room %s (%d rows)"
        % (room, len(rows))]


def recv(room, seat=None, via="meld"):
    """(0, lines) — every participant row since this seat's cursor, at once.
    Never waits: the wake is the other side's @mention."""
    seat = seat or _self_seat()
    _need_room(room, via)
    rows, total, got = _read(room)
    _need_member(got, room, seat, via)
    start = _cursor(room, seat, got)
    accepted, base = set(got["accepted"]), got["base"]
    out = []
    for i in range(max(start - base, 0), len(rows)):
        m = rows[i]
        frm = str(m.get("from") or "")
        if base + i not in accepted or _cf(frm) == _cf(seat):
            continue
        out.append("[standing %s] %s: %s" % (room, chat._dsan(frm),
                                             m.get("text") or ""))
    _save_cursor(room, seat, base + len(rows), got)
    if not out:
        out.append("[standing %s] nothing new — recv never waits here; the "
                   "others' @mentions wake you" % room)
    return 0, out


def say(room, text, marker=None, seat=None, via="meld"):
    """(lines) — one message, @mentioning every other participant. No cap,
    no seal; a guest's message is its one exchange, and it leaves after."""
    from . import meld
    seat = seat or _self_seat()
    _need_room(room, via)
    text = (text or "").strip()
    if not text:
        raise SystemExit("helm chat %s: say wants text" % via)
    marker = (marker or "").upper()
    if marker and marker not in meld.MARKERS:
        raise SystemExit("helm chat %s: --marker wants one of %s"
                         % (via, "|".join(meld.MARKERS)))
    meld._broadcast_token_refusal(text)
    got = _read(room)[2]
    _need_member(got, room, seat, via)
    others = [x for x in got["members"] + got["guests"] if _cf(x) != _cf(seat)]
    _post(" ".join(x for x in (_mentions(others), "[STANDING]", text + (
        " [%s]" % marker if marker else "")) if x), room, seat)
    lines = ["[standing %s] %s: … (woke %s)" % (
        room, chat._dsan(seat), ", ".join(chat._dsan(x) for x in others)
        or "nobody else is in the room")]
    if _cf(seat) in {_cf(x) for x in got["guests"]}:
        _post("[STANDING-LEFT] seat=%s | one exchange, done" % seat, room,
              seat)
        lines.append("you left %s: a pulled seat speaks once" % room)
    return lines


def leave(room, seat=None, via="meld"):
    """(lines) — this seat leaves the room (a guest before answering, or a
    member stepping out); a member can add it back."""
    seat = seat or _self_seat()
    _need_room(room, via)
    got = _read(room)[2]
    _need_member(got, room, seat, via)
    _post("[STANDING-LEFT] seat=%s" % seat, room, seat)
    return ["you left %s" % room]


def open_for(a, b):
    """The standing room of {a, b} when it is open and both are members in
    it, else None. A read failure is None: the per-chain pair meld then
    opens exactly as before."""
    if not a or not b or _cf(a) == _cf(b):
        return None
    room = room_for(a, b)
    try:
        got = _read(room)[2]
    except Exception:                                   # noqa: BLE001
        return None
    names = {_cf(x) for x in got["members"]}
    return room if {_cf(a), _cf(b)} <= names else None


def post_round(room, row, verb, key, pair_text, acting=None):
    """(round number, reused) — record one dispatch row's round in the
    standing room, once per row and verb. No @mention: the dispatch's own
    DM rings the reader, and one wake is the whole budget."""
    row12 = str(row["id"])[:12]
    sender = str(row.get("sender") or "")
    with _lock(room):
        got = _read(room)[2]
        for n, (r12, v) in enumerate(got["rounds"]):
            if r12 == row12 and v == verb:
                return n + 1, True
        lane = " ".join(str(row.get("lane") or "").replace("@", "").split())
        by = ""
        if acting and acting != sender:
            by = " | opened by %s for %s" % (chat._dsan(acting),
                                             chat._dsan(sender))
        whisper = " | %s" % row["round_whisper"] \
            if row.get("round_whisper") else ""
        _post("[STANDING-ROUND] row=%s verb=%s by=%s | %s %s: %s at %s | "
              "pairing: %s%s%s"
              % (row12, verb, sender, key, lane[:80],
                 row.get("kind") or "dispatch", str(row.get("tip") or "")[:12],
                 pair_text, by, whisper), room, sender)
        return len(got["rounds"]) + 1, False


def status_lines(seat, via="meld"):
    """[lines] — the standing rooms this seat is in."""
    out = []
    try:
        rooms = [r for r in chat.list_rooms() if is_standing_room(r)]
    except Exception:                                   # noqa: BLE001
        return out
    for room in rooms:
        try:
            got = _read(room)[2]
        except Exception:                               # noqa: BLE001
            continue
        if _cf(seat) not in {_cf(x) for x in got["members"] + got["guests"]}:
            continue
        out.append("  %s  standing members=%s%s rounds=%d" % (
            room, ",".join(chat._dsan(x) for x in got["members"]),
            " guests=" + ",".join(chat._dsan(x) for x in got["guests"])
            if got["guests"] else "", len(got["rounds"])))
    return out
