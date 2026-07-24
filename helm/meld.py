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

Protocol (an earlier prototype's scars kept):
  * every protocol row is epoch-fenced `[MELD e:<epoch>]` — a reused room
    can never replay a dead meld's leftovers into a new session;
  * the invite's protocol head (room + join command) comes FIRST so the
    200-byte delivery clip can never eat the join instruction (inverted from a
    live incident, for a head-clip);
  * READY carries no floor marker — it is control-only; recv returns it
    exactly once (convener, invited status) and skips it everywhere else
    (codex F1: control echoes are not chunks);
  * recv accepts only PEER rows ending in a real floor marker; own rows and
    unattributable senders are skipped fail-closed (codex F2);
  * state is keyed room × ACTOR (`<room>.meld.<seat-key>.json`) — helm's two
    seats always share one chat dir, the exact host-global-state clobber an
    earlier prototype's live dogfood refuted (test-fixtures-isolated-what-
    production-shares).

Floor markers (the protocol vocabulary, verbatim): [YIELD] hands the floor,
[HOLD] more coming from the same speaker, [DONE] leaving the meld,
[ABORT] kills it fail-loud (exit 4).

v1 is 2-party (an earlier prototype's precedent); 3+ minds use a plain room + discipline.
NAMING (premise council-is-the-number-one-feature, owner canon 2026-07-23):
MELD is the GENUS — `meld` stays the primary verb; `standup` (informal 2+
convergence, includes this 2-party mindmeld) and `council` (the big FORMAL
convergence — agenda/quorum/recorded verdict, the 0.3 N-of-M machinery) are
SPECIES spellings routed to this same preset, never replacements. Every
spelling echoes itself back in the printed next-commands (`via`).

PINNED PAIR (live-fire 2026-07-23): a meld is its invited pair. The seed
carries `invited=<peer>`; join REFUSES any other seat; recv accepts READY
and chunks ONLY from state["peer"] — a third seat can no longer GO, hijack,
or kill a meld with a forged DONE/ABORT. Identity is env-first
(home.chat_name), aligned with chat.whoname — the roster-first order here
silently overrode HELM_CHAT_NAME for any roster-known session.
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
# the seed's pinned-pair fields — findall[-1] so a topic that EMBEDS a fake
# `convener=… invited=…` run can never outrank the real fields (they are
# appended AFTER the topic, so the last match is always the seed's own)
_INVITED_RE = re.compile(r"convener=[\w.-]+ invited=([\w.-]+) cap=\d")


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
    # ENV-FIRST, aligned with chat.whoname (live-fire 2026-07-23 identity
    # trap): the roster-first order here silently ignored HELM_CHAT_NAME for
    # any session already in the roster — the invite fired AND the peer
    # joined under the roster seat instead of the exported one, zero warning.
    # home.chat_name is THE validated seam (hostile names rejected at source).
    name = home.chat_name()
    if name:
        return name
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


def invite(peer, topic, seat=None, via="meld"):
    """(room, lines) — open a meld: seed the problem ([HOLD], discipline
    included so the joiner needs no skill file), then the @peer invite with
    the protocol HEAD-first (clip-proof). State: convener/invited. The seed
    carries `invited=<peer>` — the pinned pair's durable half (join refuses
    any other seat; recv accepts only this one). An untracked peer gets a
    LOUD warning instead of the unconditional 'wakes at its next boundary'
    line (live-fire: the invite asserted delivery to a seat with no lane)."""
    seat = seat or _self_seat()
    peer = (peer or "").lstrip("@")
    if not peer:
        raise SystemExit("helm %s: invite wants a peer seat name" % via)
    try:  # THE arg-side ingestion seam — a hostile/garbled peer name never
        peer = home.validate_seat_arg(peer)  # becomes a seed field or mention
    except home.SeatNameError as e:
        raise SystemExit("helm %s: %s" % (via, e))
    epoch = int(time.time())
    room = room_name(topic, epoch)
    if state(room, seat):
        raise SystemExit("helm %s: state already exists for room %s" % (via, room))
    seed = ("[MELD e:%d] PROBLEM: %s | convener=%s invited=%s cap=%d "
            "recv-timeout=%ds | MELD DISCIPLINE: reply FAST with what you "
            "already know; a fork that needs research is NOT a meld — close "
            "[DONE] with the async continuation. [HOLD]"
            % (epoch, topic, seat, peer, _cap(), int(_recv_timeout())))
    _post(seed, room, seat)
    # peer is validated above; laundering the EMITTED copies stays as
    # defense-in-depth beneath the seam. topic is CONTENT, left full-fidelity
    # by the class rule (only identity is laundered).
    d_peer = chat._dsan(peer)
    inv = ("@%s [MELD-INVITE e:%d] room=%s JOIN: helm chat %s join %s "
           "THEN: helm chat %s recv %s || topic: %s"
           % (d_peer, epoch, room, via, room, via, room, topic))
    _post(inv, room, seat)
    _write_state(room, seat, {
        "room": room, "epoch": epoch, "role": "convener", "self": seat,
        "peer": peer, "idx": 0, "exchanges": 0, "cap": _cap(),
        "status": "invited", "created": pk.now_ts()})
    from . import seats
    try:  # the wake-truth line: honest per the peer's ACTUAL delivery lane
        tracked = seats.seat_scope(peer)["tracked"]
    except Exception:
        tracked = False
    wake = ("the invite is a durable row — %s wakes at its next tool "
            "boundary or beacon (never lost, only delayed)" % d_peer
            if tracked else
            "WARNING: %s is NOT on the chat roster — no delivery lane "
            "exists; nothing wakes it until it joins (`helm chat join`) or "
            "reads %s itself" % (d_peer, room))
    return room, [
        "MELD-INVITED room=%s epoch=%d peer=%s" % (room, epoch, d_peer),
        wake,
        "next: helm chat %s recv %s   (returns on READY; then speak the "
        "first chunk: helm chat %s say %s --marker YIELD \"...\")"
        % (via, room, via, room)]


def join(room, seat=None, via="meld"):
    """(lines) — join a meld: parse epoch + convener + invited from the seed,
    post the control-only READY (@convener — the wake-back; a READY that
    lands silently strands GO forever, a live incident), state
    joiner/active with idx=0 so the seeded problem is the first recv chunk.
    REFUSES a seat the seed did not invite (live-fire 2026-07-23: a meld
    convened for one seat was consummated by another with zero warning —
    downstream, the convener's DONE @mentioned a ghost). Pre-pin seeds
    (no invited= field) grandfather in unpinned."""
    seat = seat or _self_seat()
    rows, _total = chat.read(room)
    epoch = convener = seedtext = None
    for m in rows:
        em = _EPOCH_RE.search(m.get("text") or "")
        if em and m.get("from"):
            epoch, convener, seedtext = int(em.group(1)), m["from"], m["text"]
            break
    if epoch is None:
        raise SystemExit("helm %s: no meld seed in room %s — was it "
                         "invited? (helm chat read --room %s)" % (via, room, room))
    if convener == seat:
        raise SystemExit("helm %s: %s convened this meld — recv, don't "
                         "join" % (via, seat))
    fm = _INVITED_RE.findall(seedtext or "")
    invited = fm[-1] if fm else None
    if invited and invited != seat:
        raise SystemExit(
            "helm %s: room %s was convened for %s, not %s — join under the "
            "invited name (HELM_CHAT_NAME=%s or --seat %s). A different-seat "
            "join is how melds got hijacked/mis-consummated (live-fire "
            "2026-07-23)." % (via, room, invited, chat._dsan(seat),
                              invited, invited))
    # convener is a SEED ROW's from-field — planted/foreign (a pre-fix or
    # foreign-node seed) it may carry ESC/bidi. Launder it before it enters
    # BOTH the MELD-JOINED display line AND the posted READY text (chat._fmt
    # renders posted text raw fleet-wide — the identity-into-text bypass). The
    # RAW convener stays in state["peer"] for recv's peer matching, mirroring
    # recv's chat._dsan(frm) pattern: launder the EMIT, keep the KEY raw.
    d_convener = chat._dsan(convener)
    _post("@%s [MELD e:%d] READY:%d (%s joined %s)"
          % (d_convener, epoch, epoch, seat, room), room, seat)
    _write_state(room, seat, {
        "room": room, "epoch": epoch, "role": "joiner", "self": seat,
        "peer": convener, "idx": 0, "exchanges": 0, "cap": _cap(),
        "status": "active", "created": pk.now_ts()})
    return ["MELD-JOINED room=%s epoch=%d convener=%s" % (room, epoch, d_convener),
            "next: helm chat %s recv %s   (the seeded problem statement "
            "is your first chunk)" % (via, room)]


def _fall_lines(room, reason, st, via="meld"):
    return ["MELD-BOUND room=%s reason=%s exchanges=%d/%d"
            % (room, reason, st.get("exchanges", 0), st.get("cap", _cap())),
            "the synchronous window is over — fall to async NOW: post your "
            "current state + the required next action as the closing chunk "
            "(it @mentions the peer; the durable row guarantees it lands):",
            "  helm chat %s say %s --marker DONE \"<state + next action>\""
            % (via, room)]


def _ignored_note(ignored, peer):
    """One terse line when the loop skipped floor-shaped rows from non-peer
    seats — silence here is how the unpinned-peer hijack went unnoticed."""
    if not ignored:
        return []
    who = ", ".join(sorted({chat._dsan(x) for x in ignored}))
    return ["(pinned pair: ignored %d floor/READY row(s) from non-peer "
            "seat(s) %s — this meld speaks only with %s)"
            % (len(ignored), who, chat._dsan(peer))]


def recv(room, timeout=None, seat=None, poll=MELD_POLL, via="meld"):
    """(code, lines) — the blocking marker-aware read: return the next PEER
    chunk carrying a real floor marker; skip own/unattributable rows (F2),
    stale epochs (the fence), control echoes (F1), markerless chatter,
    reactions, and — the pinned-pair law — EVERY row from a seat that is not
    state["peer"] (live-fire 2026-07-23: recv accepted READY and chunks from
    ANY non-self sender; a meld convened for one seat was consummated by
    another, and a third seat could kill any meld with a forged DONE/ABORT).
    Bounds are behavior: cap/timeout → (EXIT_BOUND, fall-to-async
    instruction); [ABORT] → (EXIT_ABORT, loud). READY returns exactly once —
    to the invited convener, flipping it active (the GO moment). After your
    own [DONE] recv becomes the COUNTERSIGN WATCH (closer-went-blind,
    live-fire): it returns the peer's closing DONE/ABORT — flipping
    done-mutual and counting the exchange — instead of refusing."""
    seat = seat or _self_seat()
    st = state(room, seat)
    if st is None:
        return 2, ["helm %s: no meld state for %s — invite or join first"
                   % (via, room)]
    if st["status"] == "aborted":
        return 2, ["helm %s: room %s is ABORTED — the meld is dead" % (via, room)]
    if st["status"] == "done-mutual":
        return 2, ["helm %s: room %s is sealed (done-mutual) — both sides "
                   "closed" % (via, room)]
    closing = st["status"] == "done"      # countersign watch, not a refusal
    if st["status"] == "peer-done":
        return 2, ["helm %s: peer already left room %s — nothing further "
                   "arrives; close: helm chat %s say %s --marker DONE "
                   "\"<closing state>\"" % (via, room, via, room)]
    if not closing and st["exchanges"] >= st.get("cap", _cap()):
        return EXIT_BOUND, _fall_lines(room, "cap", st, via)
    peer = str(st.get("peer") or "")
    ignored, late = [], 0
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
            if peer and frm != peer:      # the pinned pair — noted, never obeyed
                if _READY_RE.search(text) or _MARKER_RE.search(text):
                    ignored.append(frm)
                st["idx"] = here
                continue
            if _READY_RE.search(text) and not _MARKER_RE.search(text):
                st["idx"] = here
                if st["role"] == "convener" and st["status"] == "invited":
                    st["status"] = "active"
                    _write_state(room, seat, st)
                    return 0, ["[meld %s e:%d] READY — %s is in. You have "
                               "the floor: helm chat %s say %s --marker "
                               "YIELD \"<first chunk>\""
                               % (room, st["epoch"], chat._dsan(frm), via, room)] \
                        + _ignored_note(ignored, peer)
                continue                  # control echo elsewhere (F1)
            mk = _MARKER_RE.search(text)
            if not mk:
                st["idx"] = here          # markerless chatter is not a chunk
                continue
            marker = mk.group(1)
            if closing and marker in ("YIELD", "HOLD"):
                st["idx"] = here          # late chunk after your DONE — the
                late += 1                 # watch wants only the countersign
                continue
            st["idx"], st["exchanges"] = here, st["exchanges"] + 1
            if marker == "ABORT":
                st["status"] = "aborted"
                _write_state(room, seat, st)
                return EXIT_ABORT, ["[meld %s e:%d] ABORT from %s — the meld "
                                    "is dead, fail-loud:" % (room, st["epoch"], chat._dsan(frm)),
                                    "  %s" % text] + _ignored_note(ignored, peer)
            if marker == "DONE":
                st["status"] = "done-mutual" if closing else "peer-done"
                _write_state(room, seat, st)
                tail = ("done-mutual — the meld is sealed; the room is the "
                        "durable record (log-flush out-of-band)" if closing
                        else "peer left — close your side: helm chat %s say "
                        "%s --marker DONE \"<closing state>\"" % (via, room))
                return 0, ["[meld %s e:%d] %s: %s"
                           % (room, st["epoch"], chat._dsan(frm), text),
                           tail] + _ignored_note(ignored, peer)
            _write_state(room, seat, st)
            floor = ("floor: YOURS — reply fast with what you already know: "
                     "helm chat %s say %s --marker YIELD|HOLD|DONE \"...\""
                     % (via, room)) if marker == "YIELD" else \
                    "floor: PEER'S — more coming; recv again"
            return 0, ["[meld %s e:%d] %s: %s" % (room, st["epoch"], chat._dsan(frm), text),
                       floor] + _ignored_note(ignored, peer)
        if st["idx"] != total:
            st["idx"] = total
        _write_state(room, seat, st)      # consumed ground survives a re-run
        if time.time() >= deadline:
            if closing:
                return EXIT_BOUND, [
                    "MELD-CLOSING room=%s — no countersign from %s yet; "
                    "your side stays closed, the room stays readable "
                    "(helm chat read --room %s)"
                    % (room, chat._dsan(peer or "?"), room)] \
                    + (["(%d late chunk(s) after your DONE skipped)" % late]
                       if late else []) + _ignored_note(ignored, peer)
            return EXIT_BOUND, _fall_lines(room, "timeout", st, via) \
                + _ignored_note(ignored, peer)
        time.sleep(poll)


def say(room, marker, text, seat=None, via="meld"):
    """(lines) — append one bounded chunk: content + floor marker in the one
    text field (text-or-it-didn't-happen). DONE/ABORT @mention the peer (the
    act-moments — a closing that lands silently strands the peer's bound);
    YIELD/HOLD stay mention-free (the peer is inside recv; no cursor spam)."""
    seat = seat or _self_seat()
    marker = (marker or "").upper()
    if marker not in MARKERS:
        raise SystemExit("helm %s: --marker wants one of %s" % (via, "|".join(MARKERS)))
    st = state(room, seat)
    if st is None:
        raise SystemExit("helm %s: no meld state for %s — invite or join "
                         "first" % (via, room))
    if st["status"] == "aborted":
        raise SystemExit("helm %s: room %s is ABORTED" % (via, room))
    if st["status"] == "done-mutual":
        # SEALED: recv refuses it too (both sides closed). Without this, a DONE
        # into a sealed meld re-posts an @peer mention AND regresses the status
        # done-mutual -> done (below), so `meld status` lies and the printed
        # hint drives a phantom 90s countersign watch for an already-consumed
        # countersign — the exact double-command an agent replays post-compaction.
        raise SystemExit("helm %s: room %s is sealed (done-mutual) — both "
                         "sides closed; nothing further posts" % (via, room))
    text = (text or "").strip()
    if not text:
        raise SystemExit("helm %s: say wants text — a bare marker is not "
                         "a chunk (text-or-it-didn't-happen)" % via)
    # st["peer"] is the RELOCATED convener/invitee from-field (join stored the
    # seed row's raw `from` here; invite stored the raw arg). Launder it before
    # it enters the DONE/ABORT posted text — chat._fmt renders posted text raw
    # fleet-wide, so a hostile peer would reshape every reader's terminal. Raw
    # stays in state (only this mention emits it; recv matches on frm==seat).
    mention = "@%s " % chat._dsan(st["peer"]) if marker in ("DONE", "ABORT") else ""
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
        if st["status"] == "done":  # peer not closed yet — the closer is not
            out.append("confirm the countersign: helm chat %s recv %s "
                       "(returns the peer's DONE → done-mutual)" % (via, room))
    return out


def status(seat=None, via="meld"):
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
                      st.get("exchanges"), st.get("cap"),
                      chat._dsan(st.get("peer") or "?")))  # identity laundered
    return out or ["helm %s: no live melds for seat %s" % (via, seat)]


def _flag(args, name, default=None):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            v = args[i + 1]
            del args[i:i + 2]
            return v
    return default


def usage(via="meld"):
    return ("usage: helm chat %s invite <peer> <topic...> [--wait] | "
            "join <room> | recv <room> [--timeout S] | say <room> --marker "
            "YIELD|HOLD|DONE|ABORT <text...> | status   [--seat S on any] "
            "(one preset, three spellings: meld = the genus, council/standup "
            "= species)" % via)


def cmd(args, via="meld"):
    """helm chat meld|council|standup invite <peer> <topic...> [--wait] |
    join <room> | recv <room> [--timeout S] | say <room> --marker M
    <text...> | status — every verb takes --seat S (explicit per-command
    identity; live-fire: ambient-only identity forced env -u gymnastics for
    one-off council seats). `via` is the spelling the operator typed — it
    echoes back in every printed next-command."""
    args = list(args or [])
    seat = None
    if "--seat" in args:
        v = _flag(args, "--seat")
        try:
            seat = home.validate_seat_arg(v)
        except home.SeatNameError as e:
            print("helm %s: %s" % (via, e), file=sys.stderr)
            return 2
        if not seat:
            print("helm %s: --seat wants a seat name" % via, file=sys.stderr)
            return 2
    wait = "--wait" in args               # invite --wait = invite + first recv
    if wait:                              # (every convener's literal next call)
        args.remove("--wait")
    verb = args[0] if args else "status"
    try:
        if verb == "invite" and len(args) >= 3:
            room, lines = invite(args[1], " ".join(args[2:]), seat=seat, via=via)
            print("\n".join(lines))
            if not wait:
                return 0
            code, lines = recv(room, seat=seat, via=via)
            print("\n".join(lines), file=sys.stderr if code else sys.stdout)
            return code
        if verb == "join" and len(args) >= 2:
            print("\n".join(join(args[1], seat=seat, via=via)))
            return 0
        if verb == "recv" and len(args) >= 2:
            t = _flag(args, "--timeout")
            code, lines = recv(args[1], timeout=float(t) if t else None,
                               seat=seat, via=via)
            print("\n".join(lines), file=sys.stderr if code else sys.stdout)
            return code
        if verb == "say" and len(args) >= 2:
            marker = _flag(args, "--marker")
            print("\n".join(say(args[1], marker, " ".join(args[2:]),
                                seat=seat, via=via)))
            return 0
        if verb == "status":
            print("\n".join(status(seat=seat, via=via)))
            return 0
    except SystemExit as e:
        print(str(e), file=sys.stderr)
        return 2
    print(usage(via), file=sys.stderr)
    return 2
