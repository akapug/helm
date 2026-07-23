#!/usr/bin/env python3
"""helm meld — the MINDMELD preset over the ONE comms primitive.

A mindmeld is hyper-speed a2a real-time convergence: both parties reply FAST
with what they ALREADY know; a fork that needs research is NOT a meld — it
falls to async (premise meld-discipline). The preset is THIN by law
(one-comms-primitive): a meld is just a fresh room (`meld-<epoch>-<slug>`)
plus a bounded synchronous discipline read over it — no new transport, no
daemon, no second store. The room IS the artifact.

LATENCY-PURE (premise comms-presets-optimize-their-novel-purity): every meld
post rides the v1 RAM append with sign=False — no signing leg, no node
round-trip, no disk write mid-meld. The out-of-band log-flush leg remains the
durable record, same as any room.

WAKE (premise a2a-wake-foolproof-layers — the verb owns it, never agent
discipline): (1) durable row — the invite/READY/DONE rows are ordinary room
rows; a TRACKED seat's delivery lane backfills a room born after its join
from offset 0, so the mention that created the meld MUST deliver (seats.py
multi-room law); (2) event path — @mention → PostToolUse boundary (busy) or
the join-mandated `wait --follow` beacon (idle); (3) self-timeout — recv's
blocking bound and the exchange cap are BEHAVIOR (exit 3, fall-to-async
instruction printed), never advice. Layers affect latency, never delivery.

Mentions fire ONLY at act-moments — invite (@peer), READY (@convener),
DONE/ABORT (@peer). Mid-meld YIELD/HOLD chunks carry NO mention: both
parties sit inside recv polling the room directly at MELD_POLL, so a meld
never floods the peer's delivery cursor with stale nudges (post-meld
boundary spam / stop-guard blocks — the mention-backlog class).

Protocol (mc-meld.sh lineage, scars kept):
  * every protocol row is epoch-fenced `[MELD e:<epoch>]` — a reused room
    can never replay a dead meld's leftovers into a new session;
  * the invite's protocol head (room + join command) comes FIRST so the
    200-byte delivery clip can never eat the join instruction (buildr #115,
    inverted for a head-clip);
  * READY carries no floor marker — it is control-only; recv returns it
    exactly once (convener, invited status) and skips it everywhere else
    (codex F1: control echoes are not chunks);
  * recv accepts only PEER rows ending in a real floor marker; own rows and
    unattributable senders are skipped fail-closed (codex F2);
  * state is keyed room × ACTOR (`<room>.meld.<seat-key>.json`) — helm's two
    seats always share one chat dir, the exact host-global-state clobber the
    mc-meld live dogfood refuted (test-fixtures-isolated-what-production-shares).

Floor markers (the lineage vocabulary, verbatim): [YIELD] hands the floor,
[HOLD] more coming from the same speaker, [DONE] leaving the meld,
[ABORT] kills it fail-loud (exit 4).

v1 is 2-party (mc-meld precedent); 3+ minds use a plain room + discipline,
or council when independence is the point (a council is never a meld).
"""
import os
import re
import sys
import time

from . import chat, home, pk

MARKERS = ("YIELD", "HOLD", "DONE", "ABORT")
CAP = 5                # accepted inbound chunks before fall-to-async
RECV_TIMEOUT_S = 90.0  # recv blocking bound
MELD_POLL = 0.5        # latency-pure: 4× faster than the room's POLL_S
EXIT_BOUND, EXIT_ABORT = 3, 4

_MARKER_RE = re.compile(r"\[(YIELD|HOLD|DONE|ABORT)\]\s*$")
_EPOCH_RE = re.compile(r"\[MELD(?:-INVITE)? e:(\d+)\]")
_READY_RE = re.compile(r"\bREADY:(\d+)\b")


def _cap():
    try:
        return int(home.env("MELD_CAP") or CAP)
    except ValueError:
        return CAP


def _recv_timeout():
    try:
        return float(home.env("MELD_RECV_TIMEOUT_S") or RECV_TIMEOUT_S)
    except ValueError:
        return RECV_TIMEOUT_S


def _self_seat():
    from . import seats
    sid = home.session_id()
    # safe_cwd, not os.getcwd(): a bare getcwd here crashed ALL five meld
    # verbs from a deleted cwd (eager-getcwd class); derive_seat handles None.
    return seats.seat_for_session(sid) or \
        seats.derive_seat(sid, seats.safe_cwd())


def state_path(room, seat):
    from . import seats
    return os.path.join(chat.chat_dir(),
                        "%s.meld.%s.json" % (pk.slug(room), seats._seat_key(seat)))


def state(room, seat):
    d = pk.read_json(state_path(room, seat), None)
    return d if isinstance(d, dict) else None


def _write_state(room, seat, st):
    chat._ensure_dir()
    pk.write_json(state_path(room, seat), st)


def room_name(topic, epoch):
    s = pk.slug(topic)[:24].strip("-") or "topic"
    return "meld-%d-%s" % (epoch, s)


def _post(text, room, seat):
    """Every meld row rides the v1 RAM append unsigned — the latency-pure
    path by law (a node round-trip mid-meld dilutes the preset's one axis)."""
    return chat.post(text, room=room, who=seat, sign=False)


def invite(peer, topic, seat=None):
    """(room, lines) — open a meld: seed the problem ([HOLD], discipline
    included so the joiner needs no skill file), then the @peer invite with
    the protocol HEAD-first (clip-proof). State: convener/invited."""
    seat = seat or _self_seat()
    peer = (peer or "").lstrip("@")
    if not peer:
        raise SystemExit("helm meld: invite wants a peer seat name")
    epoch = int(time.time())
    room = room_name(topic, epoch)
    if state(room, seat):
        raise SystemExit("helm meld: state already exists for room %s" % room)
    seed = ("[MELD e:%d] PROBLEM: %s | convener=%s cap=%d recv-timeout=%ds | "
            "MELD DISCIPLINE: reply FAST with what you already know; a fork "
            "that needs research is NOT a meld — close [DONE] with the async "
            "continuation. [HOLD]"
            % (epoch, topic, seat, _cap(), int(_recv_timeout())))
    _post(seed, room, seat)
    inv = ("@%s [MELD-INVITE e:%d] room=%s JOIN: helm chat meld join %s "
           "THEN: helm chat meld recv %s || topic: %s"
           % (peer, epoch, room, room, room, topic))
    _post(inv, room, seat)
    _write_state(room, seat, {
        "room": room, "epoch": epoch, "role": "convener", "self": seat,
        "peer": peer, "idx": 0, "exchanges": 0, "cap": _cap(),
        "status": "invited", "created": pk.now_ts()})
    return room, [
        "MELD-INVITED room=%s epoch=%d peer=%s" % (room, epoch, peer),
        "the invite is a durable row — %s wakes at its next tool boundary "
        "or beacon (never lost, only delayed)" % peer,
        "next: helm chat meld recv %s   (returns on READY; then speak the "
        "first chunk: helm chat meld say %s --marker YIELD \"...\")"
        % (room, room)]


def join(room, seat=None):
    """(lines) — join a meld: parse epoch + convener from the seed, post the
    control-only READY (@convener — the wake-back; a READY that lands
    silently strands GO forever, buildr live-incident), state joiner/active
    with idx=0 so the seeded problem is the first recv chunk."""
    seat = seat or _self_seat()
    rows, _total = chat.read(room)
    epoch = convener = None
    for m in rows:
        em = _EPOCH_RE.search(m.get("text") or "")
        if em and m.get("from"):
            epoch, convener = int(em.group(1)), m["from"]
            break
    if epoch is None:
        raise SystemExit("helm meld: no meld seed in room %s — was it "
                         "invited? (helm chat read --room %s)" % (room, room))
    if convener == seat:
        raise SystemExit("helm meld: %s convened this meld — recv, don't "
                         "join" % seat)
    _post("@%s [MELD e:%d] READY:%d (%s joined %s)"
          % (convener, epoch, epoch, seat, room), room, seat)
    _write_state(room, seat, {
        "room": room, "epoch": epoch, "role": "joiner", "self": seat,
        "peer": convener, "idx": 0, "exchanges": 0, "cap": _cap(),
        "status": "active", "created": pk.now_ts()})
    return ["MELD-JOINED room=%s epoch=%d convener=%s" % (room, epoch, convener),
            "next: helm chat meld recv %s   (the seeded problem statement "
            "is your first chunk)" % room]


def _fall_lines(room, reason, st):
    return ["MELD-BOUND room=%s reason=%s exchanges=%d/%d"
            % (room, reason, st.get("exchanges", 0), st.get("cap", _cap())),
            "the synchronous window is over — fall to async NOW: post your "
            "current state + the required next action as the closing chunk "
            "(it @mentions the peer; the durable row guarantees it lands):",
            "  helm chat meld say %s --marker DONE \"<state + next action>\""
            % room]


def recv(room, timeout=None, seat=None, poll=MELD_POLL):
    """(code, lines) — the blocking marker-aware read: return the next PEER
    chunk carrying a real floor marker; skip own/unattributable rows (F2),
    stale epochs (the fence), control echoes (F1), markerless chatter and
    reactions. Bounds are behavior: cap/timeout → (EXIT_BOUND, fall-to-async
    instruction); [ABORT] → (EXIT_ABORT, loud). READY returns exactly once —
    to the invited convener, flipping it active (the GO moment)."""
    seat = seat or _self_seat()
    st = state(room, seat)
    if st is None:
        return 2, ["helm meld: no meld state for %s — invite or join first"
                   % room]
    if st["status"] == "aborted":
        return 2, ["helm meld: room %s is ABORTED — the meld is dead" % room]
    if st["status"] in ("done", "done-mutual"):
        return 2, ["helm meld: you already left room %s ([DONE])" % room]
    if st["status"] == "peer-done":
        return 2, ["helm meld: peer already left room %s — nothing further "
                   "arrives; close: helm chat meld say %s --marker DONE "
                   "\"<closing state>\"" % (room, room)]
    if st["exchanges"] >= st.get("cap", _cap()):
        return EXIT_BOUND, _fall_lines(room, "cap", st)
    deadline = time.time() + (_recv_timeout() if timeout is None else timeout)
    while True:
        rows, total = chat.read(room, since=st["idx"])
        base = st["idx"]                  # stable — idx mutates in the loop
        for i, m in enumerate(rows):
            here = base + i + 1
            text, frm = m.get("text") or "", str(m.get("from") or "")
            if m.get("react") or not text or not frm or frm == seat:
                st["idx"] = here          # own/unattributable/reaction: skip
                continue                  # fail-closed (F2)
            em = _EPOCH_RE.search(text)
            if em and int(em.group(1)) != st["epoch"]:
                st["idx"] = here          # stale epoch: a dead meld's row
                continue                  # can never replay (the fence)
            if _READY_RE.search(text) and not _MARKER_RE.search(text):
                st["idx"] = here
                if st["role"] == "convener" and st["status"] == "invited":
                    st["status"] = "active"
                    _write_state(room, seat, st)
                    return 0, ["[meld %s e:%d] READY — %s is in. You have "
                               "the floor: helm chat meld say %s --marker "
                               "YIELD \"<first chunk>\""
                               % (room, st["epoch"], frm, room)]
                continue                  # control echo elsewhere (F1)
            mk = _MARKER_RE.search(text)
            if not mk:
                st["idx"] = here          # markerless chatter is not a chunk
                continue
            marker = mk.group(1)
            st["idx"], st["exchanges"] = here, st["exchanges"] + 1
            if marker == "ABORT":
                st["status"] = "aborted"
                _write_state(room, seat, st)
                return EXIT_ABORT, ["[meld %s e:%d] ABORT from %s — the meld "
                                    "is dead, fail-loud:" % (room, st["epoch"], frm),
                                    "  %s" % text]
            if marker == "DONE":
                st["status"] = "peer-done"
                _write_state(room, seat, st)
                return 0, ["[meld %s e:%d] %s: %s"
                           % (room, st["epoch"], frm, text),
                           "peer left — close your side: helm chat meld say "
                           "%s --marker DONE \"<closing state>\"" % room]
            _write_state(room, seat, st)
            floor = ("floor: YOURS — reply fast with what you already know: "
                     "helm chat meld say %s --marker YIELD|HOLD|DONE \"...\""
                     % room) if marker == "YIELD" else \
                    "floor: PEER'S — more coming; recv again"
            return 0, ["[meld %s e:%d] %s: %s" % (room, st["epoch"], frm, text),
                       floor]
        if st["idx"] != total:
            st["idx"] = total
        _write_state(room, seat, st)      # consumed ground survives a re-run
        if time.time() >= deadline:
            return EXIT_BOUND, _fall_lines(room, "timeout", st)
        time.sleep(poll)


def say(room, marker, text, seat=None):
    """(lines) — append one bounded chunk: content + floor marker in the one
    text field (text-or-it-didn't-happen). DONE/ABORT @mention the peer (the
    act-moments — a closing that lands silently strands the peer's bound);
    YIELD/HOLD stay mention-free (the peer is inside recv; no cursor spam)."""
    seat = seat or _self_seat()
    marker = (marker or "").upper()
    if marker not in MARKERS:
        raise SystemExit("helm meld: --marker wants one of %s" % "|".join(MARKERS))
    st = state(room, seat)
    if st is None:
        raise SystemExit("helm meld: no meld state for %s — invite or join "
                         "first" % room)
    if st["status"] == "aborted":
        raise SystemExit("helm meld: room %s is ABORTED" % room)
    text = (text or "").strip()
    if not text:
        raise SystemExit("helm meld: say wants text — a bare marker is not "
                         "a chunk (text-or-it-didn't-happen)")
    mention = "@%s " % st["peer"] if marker in ("DONE", "ABORT") else ""
    _post("%s[MELD e:%d] %s [%s]" % (mention, st["epoch"], text, marker),
          room, seat)
    if marker == "DONE":
        st["status"] = "done" if st["status"] != "peer-done" else "done-mutual"
    if marker == "ABORT":
        st["status"] = "aborted"
    if st["status"] == "invited":
        st["status"] = "active"           # convener spoke GO
    _write_state(room, seat, st)
    out = ["[meld %s e:%d] %s: … [%s]" % (room, st["epoch"], seat, marker)]
    if marker == "DONE":
        out.append("you left the meld — /premise anything durable; the room "
                   "log-flushes out-of-band like any room")
    return out


def status(seat=None):
    """[lines] — this actor's live melds off its state files + room truth."""
    from . import seats
    seat = seat or _self_seat()
    key = ".meld.%s.json" % seats._seat_key(seat)
    d = chat.chat_dir()
    try:
        names = sorted(n for n in os.listdir(d) if n.endswith(key))
    except OSError:
        names = []
    out = []
    for n in names:
        st = pk.read_json(os.path.join(d, n), None) or {}
        room = st.get("room") or n.split(".meld.")[0]
        out.append("  %s  role=%s status=%s exchanges=%s/%s peer=%s"
                   % (room, st.get("role"), st.get("status"),
                      st.get("exchanges"), st.get("cap"), st.get("peer")))
    return out or ["helm meld: no live melds for seat %s" % seat]


def _flag(args, name, default=None):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            v = args[i + 1]
            del args[i:i + 2]
            return v
    return default


def cmd(args):
    """helm chat meld invite <peer> <topic...> | join <room> |
    recv <room> [--timeout S] | say <room> --marker M <text...> | status"""
    args = list(args or [])
    verb = args[0] if args else "status"
    try:
        if verb == "invite" and len(args) >= 3:
            _room, lines = invite(args[1], " ".join(args[2:]))
            print("\n".join(lines))
            return 0
        if verb == "join" and len(args) >= 2:
            print("\n".join(join(args[1])))
            return 0
        if verb == "recv" and len(args) >= 2:
            t = _flag(args, "--timeout")
            code, lines = recv(args[1], timeout=float(t) if t else None)
            print("\n".join(lines), file=sys.stderr if code else sys.stdout)
            return code
        if verb == "say" and len(args) >= 2:
            marker = _flag(args, "--marker")
            print("\n".join(say(args[1], marker, " ".join(args[2:]))))
            return 0
        if verb == "status":
            print("\n".join(status()))
            return 0
    except SystemExit as e:
        print(str(e), file=sys.stderr)
        return 2
    print("usage: helm chat meld invite <peer> <topic...> | join <room> | "
          "recv <room> [--timeout S] | say <room> --marker "
          "YIELD|HOLD|DONE|ABORT <text...> | status", file=sys.stderr)
    return 2
