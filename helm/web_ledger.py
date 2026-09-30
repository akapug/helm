"""Ledger projections for :mod:`helm.web`."""
import json
import re
import sys
# EXPLICIT, NOT INHERITED — web_quota states the rule and the reason: a name
# that reaches this module ONLY through the globals() splice below is bound
# when web.py was already imported and ABSENT on a direct
# `from helm import web_ledger`, which is a NameError swallowed by a fail-open.
# `time` is read on the age legs of both endpoints below, where a swallowed
# NameError would drop exactly the age the reader needs.
import time
# os and threading: the room index below stats each room and guards its
# folds with a lock; imported here for the same reason as `time`.
import os
import threading

_web = sys.modules.get(__package__ + ".web")
if _web is not None:
    globals().update({name: value for name, value in vars(_web).items()
                      if not (name.startswith("__") and name.endswith("__"))})



# ── ledger: read-only projection of the attestation node's public reads ──
# GET-only: receipts (the signed turn ledger, finality tier per turn), cells
# (seat activity, joined to the bounded receipt window), turn status by hash.
# WHICH node: the one where signed turns LAND — chat's room node
# (chat.node_url()) when one is configured, the anchor node (cell.node_url())
# only as fallback. These are DIFFERENT services in production (:8898 vs
# :8899), and reading the anchor node under a live-signing banner rendered
# "no cave turns yet" over 34 real turns (owner report 2026-07-29; the banner
# read the chat node, the panel read the other one). The node is OPTIONAL —
# down/absent answers {"offline": true} at 200 and the tab shows "substrate
# offline", never an error page. The tab's ONE write (message a seat) rides
# the EXISTING /api/chat POST — no new mutation surface.
#
# Since dregg-primary signing went LIVE (chat's signed leg + the premise
# anchor), a cave turn is often something the OWNER can recognize: his web
# post (topic helm.chat — the RAM row keeps {turn}) or a premise attestation
# (topic helm.attest — the entry keeps attest_anchor_turn). _turn_about joins
# those local pointers back onto the receipt window so the tab can SHOW "your
# post landed as turn #N" instead of a bare hash; `transport` carries
# chat.transport_status()'s truth so the UI foregrounds the cave feed only
# when signing really is live — never a hardcoded claim.

LEDGER_TURNS = 40

# ── THE PER-REQUEST WALK, AND WHY BOTH ENDPOINTS SIT BEHIND A TTL ────────
# `_turn_about` and `_native_chat_pulse` are both O(ROOMS) AND O(ENTRY FILES):
# the first walks the attested-prior entry files plus every room file, the
# second reads every room. Measured on a live corpus of 459 rooms / 18,082
# rows / 1,305 entry files, one call each:
#     _turn_about(40 hashes)   181 ms   (store._attested_priors 119 ms of it,
#                                        chat.list_rooms 73 ms, rest the walk)
#     _native_chat_pulse()     162 ms   (chat.read x459 rooms 181 ms of CPU)
# and the console polls BOTH on a 3000 ms timer FROM EVERY OPEN SOCKET
# (70-ledger.js `pollLedgerTick`). Seventeen of the owner's tabs is seventeen
# full walks every three seconds, which is the convoy task/2703 measured. The
# SHAPE, not the instance: the fleet mints rooms continuously, so the cost of
# both walks grows with the room count and never falls back.
#
# 15 s IS FIVE CLIENT TICKS, and `_cached`'s single-flight collapses every
# CONCURRENT socket onto the one walk on top of that. It is not longer because
# both readings are things the owner watches to answer "is the fleet alive",
# and A CONFIDENT STALE BOARD IS WORSE THAN THE TIMEOUT IT REPLACES
# (web_cache) — so NEITHER is served without its own age on the wire.
#
# THE TTL BOUNDS HOW OFTEN A WALK RUNS, NOT WHAT A RUN READS. Each run still
# re-read every room: about 30 MB of raw bytes for the join and about 93 MB
# parsed for the pulse, every 15 s while any ledger page was open. So both
# walks now read the rooms through one index (`_walk_rooms`, below), which
# reads a room only where it moved since the last walk (task/3715).
_LEDGER_WALK_TTL_S = 15

LEDGER_STATUS_KEYS = ("healthy", "dag_height", "latest_height", "block_count",
                      "consensus_live", "federation_mode", "state_producer",
                      "lean_producer", "peer_count", "public_key")

_TURN_HASH = re.compile(r"[0-9a-f]{64}")
_TURN_KEY_BYTES = re.compile(
    rb'"(?:t|\\u0074)(?:u|\\u0075)(?:r|\\u0072)(?:n|\\u006[eE])"\s*:')


# ── THE ROOM INDEX BOTH WALKS READ (task/3715) ───────────────────────────
# The owner's console reached its 2 GB memory ceiling every hour and spilled
# 1.3-1.9 GB more into swap; it logged 741 client hang-ups on /api/ledger in
# one day. A multi-MB buffer read and freed on a handler thread every 15 s is
# exactly what glibc's per-thread arenas keep (helm/webmem.py), and both walks
# above made one per room per run.
#
# WHAT A WALK NEEDS FROM A ROOM IS SMALL, AND A ROOM GROWS AT ITS END. The
# join needs the labels of the rows that carry a turn; the pulse needs a row
# count, a message count and the newest row. So each walk keeps a FOLD per
# room: its answer over the rows read so far, the byte offset it read to, the
# bytes just before that offset, and the file's identity and stat when it read
# them. A walk stats every room, and then:
#   the same file, unchanged                 -> it reads nothing;
#   the same file, grown, and the bytes
#   before the old offset still there        -> it reads only the new bytes;
#   anything else                            -> it folds the room from byte 0.
# "Anything else" is every writer that is not an append. Rotation and the
# journal restore put a NEW file in place (a new inode). The keyed append's
# receipt rollback truncates in place, and the row that lands next sits where
# the rolled-back one did; the tail check catches it, because those bytes
# changed. The stat carries ctime as well as mtime and size, so a change of
# mode alone (a room made unreadable) also makes the walk open the room.
#
# ONLY COMPLETE LINES ARE FOLDED. The offset stops after the last newline: a
# row a writer is part-way through is read when its newline lands. chat.py's
# writers write a row and its newline in one write, so this delays nothing a
# reader could have used.
#
# AN UNREADABLE ROOM IS NAMED, NEVER A QUIETER COUNT. A listed room that will
# not open is reported with its reason (`faults`), and its fold is dropped, so
# no row it held is served as if it were still there. A room that vanished
# between the listing and its stat is gone, not unreadable, and its fold goes
# too; so does the fold of every room the listing no longer names.
_ROOM_FOLDS = {}   # (walk, room file) -> (ident, stat, offset, tail, state)
_ROOM_FOLDS_LOCK = threading.Lock()
#: How many bytes before a fold's offset must still be there for the room to
#: be read on from that offset. A row's id and closing brace sit in them.
_FOLD_TAIL = 256
#: The reason a room that will not open is reported with.
_UNREADABLE = {PermissionError: "denied", IsADirectoryError: "not-a-file",
               NotADirectoryError: "not-a-file"}

# THE SLICE RUNNER'S DATA AUDIT (helm/gateslice.py) reports any module data a
# test unit leaves behind; the index is process-wide by design.
_GATESLICE_MUTABLE = {
    "_ROOM_FOLDS": "keyed by walk and room file, checked against the file's "
                   "stat and the bytes before its offset",
}


def _fault(faults, room, why):
    """Name a room a walk could not read. `room` None is the listing itself;
    `why` is a sentence, or the OSError the read raised."""
    if faults is not None:
        faults.append({"room": room, "reason": why if isinstance(why, str)
                       else _UNREADABLE.get(type(why), "unreadable")})


def _fold_room(walk, path, fold, empty):
    """`walk`'s fold of one room log, read only where the file moved.
    -> the fold's state. `fold(state, lines)` returns the state after the
    complete lines given (bytes, newline stripped), never mutating the one it
    was handed. Raises the OSError of a room that cannot be stat'ed or read,
    and drops its fold first."""
    key = (walk, path)
    try:
        st = os.stat(path)
        ident = (st.st_dev, st.st_ino)
        stamp = (st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        with _ROOM_FOLDS_LOCK:
            hit = _ROOM_FOLDS.get(key)
        # A FOLD THAT REACHES PAST THE FILE'S END IS NEVER SERVED, whatever
        # the stamp says. The stat is taken before the read, so a row appended
        # between the two is folded under the stamp from before it; where the
        # clock is coarser than the writer, the keyed append's rollback puts
        # the file back to that same (size, mtime, ctime). The size is then
        # below the fold's offset, and the room is folded again from byte 0.
        if hit and hit[0] == ident and hit[1] == stamp \
                and st.st_size >= hit[2]:
            return hit[4]
        at, tail, state = 0, b"", empty
        if hit and hit[0] == ident and st.st_size >= hit[2]:
            at, tail, state = hit[2], hit[3], hit[4]
        with open(path, "rb") as f:
            if at:
                f.seek(at - len(tail))
                if f.read(len(tail)) != tail:
                    at, tail, state = 0, b"", empty
                    f.seek(0)
            data = f.read()
    except OSError:
        with _ROOM_FOLDS_LOCK:
            _ROOM_FOLDS.pop(key, None)
        raise
    end = data.rfind(b"\n") + 1
    if end:
        state = fold(state, data[:end - 1].split(b"\n"))
        tail = (tail + data[:end])[-_FOLD_TAIL:]
        at += end
    with _ROOM_FOLDS_LOCK:
        _ROOM_FOLDS[key] = (ident, stamp, at, tail, state)
    return state


def _walk_rooms(walk, fold, empty, faults=None):
    """[(room, state)] for every room chat lists, in its order, each through
    `walk`'s fold (`_fold_room`). A listed room that cannot be read is named
    in `faults`; one gone since the listing is simply absent. The folds of
    rooms the listing no longer names are dropped."""
    from . import chat
    try:
        rooms = chat.list_rooms()
    except OSError as exc:
        _fault(faults, None, exc)
        return []
    out, live = [], set()
    for room in rooms:
        path = chat.room_path(room)
        live.add((walk, path))
        try:
            out.append((room, _fold_room(walk, path, fold, empty)))
        except FileNotFoundError:
            continue
        except OSError as exc:
            _fault(faults, room, exc)
    with _ROOM_FOLDS_LOCK:
        for key in [k for k in _ROOM_FOLDS if k[0] == walk and k not in live]:
            del _ROOM_FOLDS[key]
    return out



def _turn_about(turn_hashes=None, faults=None):
    """turn_hash -> what helm KNOWS landed as that cave turn — the owner-
    legible join. Chat rows that signed carry {turn} (the row's chat:b2b
    digest rode a self-write turn, topic helm.chat); store entries carry
    attest_anchor_turn (the premise anchor, topic helm.attest; legacy
    attest_turn reads too). Local reads only; each leg fails open to fewer
    labels, never an error. An unlabeled turn is simply not-ours-to-name.

    The ledger asks about forty receipt hashes, not every turn Helm has ever
    seen. Passing that bounded set takes the narrow store projection; the
    no-argument form remains the full compatibility read for callers that
    genuinely want the whole index. Both read the rooms through the room index
    (`_walk_rooms`), whose fold parses only the lines that can carry a turn.

    FEWER LABELS IS NEVER SILENT. When `faults` is a list, every room the join
    could not read is appended to it as {room, reason}, and so is a room leg
    that failed as a whole (room None)."""
    wanted = None if turn_hashes is None else {
        h for h in turn_hashes if isinstance(h, str) and h}
    if wanted == set():
        return {}
    out = {}
    try:
        from . import store
        entries = (store.load_all(include_retired=True) if wanted is None
                   else store._attested_priors())
        for e in entries:
            for k in ("attest_anchor_turn", "attest_turn"):
                t = e.get(k)
                if t and (wanted is None or t in wanted):
                    out[t] = {"kind": "attest", "id": e.get("id"),
                              "type": e.get("type")}
    except Exception:
        pass
    try:
        from . import chat

        def fold(labels, lines):
            # turn -> (laundered sender, text) for the rows that carry one; a
            # later row with the same turn wins, as a full read in order would
            out = labels
            for line in lines:
                if not _TURN_KEY_BYTES.search(line):
                    continue
                m = chat._msg(line.decode("utf-8", "replace"))
                t = m.get("turn") if m else None
                if not (t and isinstance(t, str)):
                    continue
                if out is labels:
                    out = dict(labels)
                out[t] = (chat._dsan(m.get("from")),
                          str(m.get("text") or m.get("react") or "")[:80])
            return out
        for room, labels in _walk_rooms("turns", fold, {}, faults):
            for t in (labels if wanted is None else wanted & labels.keys()):
                sender, text = labels[t]
                out[t] = {"kind": "chat", "from": sender, "room": room,
                          "text": text}
    except Exception as exc:
        _fault(faults, None, "the room walk failed (%s)" % type(exc).__name__)
    return out



def _chat_transport():
    """chat.transport_status() with the endpoint's degrade law: any surprise
    answers None (the UI reads missing as unknown and keeps the honest
    fallback framing — never an implied 'signed')."""
    try:
        from . import chat
        return chat.transport_status(fleet=True)
    except Exception:
        return None



def _ledger_node():
    """The node the ledger tab reads: WHERE SIGNED TURNS LAND. Since
    dregg-primary, chat posts sign onto the room node (chat.node_url() — the
    same node transport_status() probes, so the banner and the turns list can
    never describe two different services again). The anchor node
    (cell.node_url()) is only the fallback when chat's transport is disabled
    (HELM_CHAT_NODE_URL set-but-empty, the hermetic/ops kill switch)."""
    try:
        from . import chat
        u = chat.node_url()
        if u:
            return u
    except Exception:
        pass
    from . import cell
    return cell.node_url()



def _cell_seat_names():
    """cell_hex -> seat/profile name, from chat's RAM-side join cache
    ({profile: cell_hex}). Best-effort: an absent/torn cache means fewer
    names, never an error — an unnamed cell renders as its id, and helm
    never invents a name for a cell it cannot join."""
    try:
        from . import chat, pk
        cache = pk.read_json(chat.cells_path(), {}) or {}
        return {v: chat._dsan(k) for k, v in cache.items()
                if isinstance(k, str) and isinstance(v, str)}
    except Exception:
        return {}



def _api_ledger(qs):
    """Aggregate: node status subset + newest signed turns (bounded, each
    labeled via _turn_about when a local pointer names it) + the chat
    transport truth (signed/unsigned — what the tab's framing keys on) + cells
    with per-seat last-activity derived from the receipt window (receipt.agent
    joins cell.id 1:1 — cells with no turn in the window honestly carry None)
    and, when chat's join cache knows the cell, the seat name that signs
    with it."""
    from . import cell
    url = _ledger_node()
    status = cell.get_json(url + "/status", timeout=3)
    receipts = cell.get_json(url + "/api/receipts", timeout=3)
    transport = _chat_transport()
    if status is None and receipts is None:
        return {"offline": True, "node": url, "transport": transport}, 200
    turns = sorted((r for r in (receipts or []) if isinstance(r, dict)),
                   key=lambda r: r.get("chain_index", 0),
                   reverse=True)[:LEDGER_TURNS]
    # A CONSTANT KEY, DELIBERATELY, over one keyed on this window's hashes.
    # `_qstate` is never evicted (web_cache), so a key that moves with the turn
    # window would mint a new permanent entry per turn inside the very process
    # this row watched grow to 6 GB. The constant key is SAFE here because the
    # body is a map from turn_hash to a label, and a label for a hash is true
    # whatever window built it — turn_hash -> what landed as it does not change.
    # The only error a stale window can produce is a MISSING label, which is
    # already this join's documented fail-open: an unlabeled turn is simply
    # not-ours-to-name, and it becomes named within one TTL.
    hashes = [r.get("turn_hash") for r in turns]

    def read():
        faults = []
        about = _turn_about(hashes, faults)
        return {"about": about, "unreadable": faults, "read_ts": time.time()}
    walk = _cached("ledger-about", _LEDGER_WALK_TTL_S, read)
    about = walk["about"]
    for r in turns:
        a = about.get(r.get("turn_hash"))
        if a:
            r["about"] = a
    last_ts, seen = {}, {}
    for r in turns:
        a, ts = r.get("agent"), r.get("timestamp")
        if not a:
            continue
        seen[a] = seen.get(a, 0) + 1
        if ts is not None and ts > last_ts.get(a, -1):
            last_ts[a] = ts
    rows = [c for c in (cell.get_json(url + "/api/cells", timeout=3) or [])
            if isinstance(c, dict)]
    names = _cell_seat_names()
    for c in rows:
        c["last_turn_ts"] = last_ts.get(c.get("id"))
        c["recent_turns"] = seen.get(c.get("id"), 0)
        seat = names.get(c.get("id"))
        if seat:
            c["seat"] = seat
    rows.sort(key=lambda c: (c.get("last_turn_ts") is None,
                             -(c.get("last_turn_ts") or 0), c.get("id") or ""))
    # THE AGE OF THE JOIN, NOT OF THE TURNS. `turns`, `status` and `cells` are
    # read from the node on THIS request; only the local name-join is cached,
    # and this says how old that reading is so no reader has to assume it is
    # live. It carries no card chrome on purpose: a label that IS present is
    # true at any age, and a label that is absent already means nothing more
    # than "not ours to name" — so the honest place for the age is the wire,
    # where a reader that wants to reason about the absence can find it.
    return {"node": url,
            "status": {k: (status or {}).get(k) for k in LEDGER_STATUS_KEYS},
            "transport": transport,
            "about_age_s": round(max(0.0, time.time() - walk["read_ts"]), 1),
            # the rooms that join could not read, each with its reason: a turn
            # missing its label BECAUSE of one is not merely not-ours-to-name
            "about_unreadable": walk.get("unreadable", []),
            "turns": turns, "cells": rows}, 200



# THE NODE ROUTES THIS PROXY MAY READ, in the order it asks, each with the
# field that marks a JSON body as the node's own answer.
#
# There is no `/api/turn/<h>/status` route on any dregg build (fork, base or
# upstream), and a proxy of it answers `unavailable` for every hash. `verdict`
# is the node's answer to "what happened to my turn?": accepted, rejected,
# pending, or unknown (a 404 WITH a body, which is not a verdict but is an
# answer). `anchor` is the older committed-turn read, asked only when a node
# does not serve `verdict`. A build older than both (dregg's
# lane/fee-loop-plus-coordination) serves NEITHER, and there this answers
# unavailable and names the routes that 404'd rather than guessing.
#
# An accepted verdict's `height` is the COMMIT-LOG height (the node's
# `lookup_turn` record), NOT a receipt's `chain_index`. The two are different
# counters, and the ledger list beside this endpoint shows the other one.
_TURN_ROUTES = (("verdict", "verdict"), ("anchor", "anchor_status"))


def _api_ledger_turn(qs):
    """One turn's verdict: proxy /api/turn/<hash>/verdict (else /anchor) on
    the SAME node the turns list reads (_ledger_node). The hash gate keeps the
    proxied path literal-only. The body is the node's own JSON plus `source`
    (which route answered) and `node`; nothing is inferred across routes."""
    h = (_q1(qs, "hash") or "").strip().lower()
    if not _TURN_HASH.fullmatch(h):
        return {"error": "hash wants 64 hex chars (a turn hash)"}, 400
    from . import cell
    url = _ledger_node()
    absent = []
    for route, field in _TURN_ROUTES:
        diag = {}
        d = cell.get_json("%s/api/turn/%s/%s" % (url, h, route), timeout=3,
                          diag=diag)
        if d is None and diag.get("status"):
            # A JSON body on an error status IS the node's answer (a 404
            # `unknown`, a 409 `no_attestation`); an empty one is a missing route.
            try:
                d = json.loads(diag["body"] or "null")
            except ValueError:
                d = None
        if isinstance(d, dict) and field in d:
            return dict(d, source=route, node=url), 200
        if diag.get("status") == 404:
            absent.append(route)
            continue
        if not diag:
            reason = "/%s answered without a %s field" % (route, field)
        elif diag["status"] is None:
            reason = "the node did not answer"
        else:
            reason = "HTTP %s on /%s" % (diag["status"], route)
        return {"unavailable": True, "node": url, "hash": h,
                "reason": reason}, 200
    return {"unavailable": True, "node": url, "hash": h,
            "reason": "the node serves no turn-verdict route (%s all 404): an "
                      "older build — no verdict is inferred"
                      % ", ".join("/" + r for r in absent)}, 200



# ── ledger/native: the host-local telemetry surface — local reads only ──
# The cave turn-ledger above is the DURABLE dregg attestation and, now that
# dregg-primary signing is live (owner posts + premise anchors land as real
# cave turns — premise dregg-primary-corrects-native-chain-misunderstanding),
# the PRIMARY evidence. This card is the complementary HOST-LOCAL layer: the
# blake2b attest-chain (premise.py — a tamper-evident local record, honestly
# never a dregg proof), the events journal (append-only mutation receipts,
# honestly NOT hash-chained), and the RAM room's presence-chat pulse (the
# fast a2a lane — agent posts honestly unsigned until seat-signing lands;
# signed rows carry their cave receipt in the chat tab). When no signer is
# configured (HELM_CELL_BIN unset) this layer IS the coordination fallback
# and the UI says so. This endpoint projects all three — no node, no network,
# GET-only, and every leg fails open to an empty section, never an error.

NATIVE_ROWS = 30

# One chain record projects to these keys — provenance the owner cross-checks
# with `helm premise-check`; never the whole record (bounded payload).
NATIVE_REC_KEYS = ("op", "premise_id", "root", "project", "ts", "attest_by",
                   "rec_hash", "chain_index")



def _native_chat_pulse():
    """The fleet-liveness pulse off the RAM rooms: post count, newest row's
    ts/from/room. Reaction rows count as activity (last_*) but not as msgs.
    The rooms are read through the room index (`_walk_rooms`), and every room
    it could not read is NAMED in `unreadable` as {room, reason}: a denied
    room is a quieter pulse only when the pulse also says so. A line that does
    not parse is skipped, as `chat.read` skips it, and is not named."""
    from . import chat
    out = {"rooms": 0, "msgs": 0, "last_ts": "", "last_from": "", "last_room": "",
           "unreadable": []}

    def fold(state, lines):
        # (rows, messages, (ts, sender) of the newest row) over the lines read
        total, msgs, last = state
        for line in lines:
            m = chat._msg(line.decode("utf-8", "replace")) if line else None
            if not m:
                continue
            total += 1
            msgs += 0 if m.get("react") else 1
            last = (m.get("ts") or "", m.get("from") or "")
        return total, msgs, last
    try:
        for room, (total, msgs, last) in _walk_rooms(
                "pulse", fold, (0, 0, ("", "")), out["unreadable"]):
            if not total:
                continue
            out["rooms"] += 1
            out["msgs"] += msgs
            if last[0] > out["last_ts"]:
                out.update(last_ts=last[0], last_from=chat._dsan(last[1]),
                           last_room=room)
    except Exception as exc:
        _fault(out["unreadable"], None,
               "the room walk failed (%s)" % type(exc).__name__)
    return out



def _api_ledger_native(qs):
    """Native attestation pulse: chain head + whole-chain verification + newest
    records, newest receipts, chat pulse. verified is verify_chain() truth — a
    tampered chain surfaces here as verified:false + detail, never hidden.

    ONE READING OF THE CHAIN, HANDED TO BOTH LEGS. The head, the count and the
    verification are three statements about ONE file, and reading it twice let
    an append between the calls put a head from before it under a verification
    from after it — the same-input divergence /api/lr's read-set was rebuilt to
    close, live on this endpoint through the same seam."""
    from . import pk, premise
    recs = premise.chain_records()
    verified, detail = premise.verify_chain(recs) if recs else (True, "")
    head = recs[-1] if recs else {}
    try:
        events = pk.read_events(NATIVE_ROWS)[::-1]  # newest-first on the wire
    except Exception:
        events = []
    # THE PULSE IS THE READING MOST AT RISK OF BEING CONFIDENTLY STALE, so it
    # is the one that gets its age rendered. Its counts ("N msgs · M rooms")
    # are the owner's is-the-fleet-chattering gauge, and behind a TTL they can
    # lag a new post by up to one window; `last_ts` is the post's OWN stamp and
    # keeps ticking client-side, so age alone could never reveal the lag. The
    # cached body is copied, never mutated — `age_s` is this request's, and the
    # next reader inside the window must not inherit it.
    walk = _cached("ledger-native-pulse", _LEDGER_WALK_TTL_S,
                   lambda: {"pulse": _native_chat_pulse(), "read_ts": time.time()})
    pulse = dict(walk["pulse"],
                 age_s=round(max(0.0, time.time() - walk["read_ts"]), 1))
    return {"chain": {"count": len(recs), "verified": bool(verified),
                      "detail": "" if verified else str(detail),
                      "head_index": head.get("chain_index"),
                      "head_hash": head.get("rec_hash", ""),
                      "records": [{k: r.get(k) for k in NATIVE_REC_KEYS}
                                  for r in recs[-NATIVE_ROWS:][::-1]]},
            "events": events, "chat": pulse}, 200
del _web
