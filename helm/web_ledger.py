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
_LEDGER_WALK_TTL_S = 15

LEDGER_STATUS_KEYS = ("healthy", "dag_height", "latest_height", "block_count",
                      "consensus_live", "federation_mode", "state_producer",
                      "lean_producer", "peer_count", "public_key")

_TURN_HASH = re.compile(r"[0-9a-f]{64}")
_TURN_KEY_BYTES = re.compile(
    rb'"(?:t|\\u0074)(?:u|\\u0075)(?:r|\\u0072)(?:n|\\u006[eE])"\s*:')



def _turn_about(turn_hashes=None):
    """turn_hash -> what helm KNOWS landed as that cave turn — the owner-
    legible join. Chat rows that signed carry {turn} (the row's chat:b2b
    digest rode a self-write turn, topic helm.chat); store entries carry
    attest_anchor_turn (the premise anchor, topic helm.attest; legacy
    attest_turn reads too). Local reads only; each leg fails open to fewer
    labels, never an error. An unlabeled turn is simply not-ours-to-name.

    The ledger asks about forty receipt hashes, not every turn Helm has ever
    seen. Passing that bounded set takes the narrow store projection and parses
    only chat lines that can carry a turn; the no-argument form remains the full
    compatibility read for callers that genuinely want the whole index."""
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
        for room in chat.list_rooms():
            if wanted is None:
                rows, _total = chat.read(room)
            else:
                try:
                    with open(chat.room_path(room), "rb") as f:
                        raw = f.read()
                except OSError:
                    continue
                rows = [m for m in (chat._msg(line.decode("utf-8", "replace"))
                                          for line in raw.split(b"\n")
                                          if _TURN_KEY_BYTES.search(line))
                        if m]
            for m in rows:
                t = m.get("turn")
                if t and (wanted is None or t in wanted):
                    out[t] = {"kind": "chat", "from": chat._dsan(m.get("from")),
                              "room": room,
                              "text": str(m.get("text") or m.get("react") or "")[:80]}
    except Exception:
        pass
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
    walk = _cached("ledger-about", _LEDGER_WALK_TTL_S,
                   lambda: {"about": _turn_about(hashes), "read_ts": time.time()})
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
    ts/from/room. Reaction rows count as activity (last_*) but not as msgs."""
    from . import chat
    out = {"rooms": 0, "msgs": 0, "last_ts": "", "last_from": "", "last_room": ""}
    try:
        for room in chat.list_rooms():
            rows, total = chat.read(room)
            if not total:
                continue
            out["rooms"] += 1
            out["msgs"] += sum(1 for m in rows if not m.get("react"))
            last = rows[-1]
            if (last.get("ts") or "") > out["last_ts"]:
                from . import chat
                out.update(last_ts=last.get("ts") or "",
                           last_from=chat._dsan(last.get("from") or ""),
                           last_room=room)
    except Exception:
        pass  # a torn room reads as a quieter pulse, never an error
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
