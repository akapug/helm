"""Ledger projections for :mod:`helm.web`."""
import re
import sys

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
# "no cave turns yet" over 34 real turns (owner report; the banner
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

LEDGER_STATUS_KEYS = ("healthy", "dag_height", "latest_height", "block_count",
                      "consensus_live", "federation_mode", "state_producer",
                      "lean_producer", "peer_count", "public_key")

_TURN_HASH = re.compile(r"[0-9a-f]{64}")



def _turn_about():
    """turn_hash -> what helm KNOWS landed as that cave turn — the owner-
    legible join. Chat rows that signed carry {turn} (the row's chat:b2b
    digest rode a self-write turn, topic helm.chat); store entries carry
    attest_anchor_turn (the premise anchor, topic helm.attest; legacy
    attest_turn reads too). Local reads only; each leg fails open to fewer
    labels, never an error. An unlabeled turn is simply not-ours-to-name."""
    out = {}
    try:
        from . import store
        for e in store.load_all(include_retired=True):
            for k in ("attest_anchor_turn", "attest_turn"):
                t = e.get(k)
                if t:
                    out[t] = {"kind": "attest", "id": e.get("id"),
                              "type": e.get("type")}
    except Exception:
        pass
    try:
        from . import chat
        for room in chat.list_rooms():
            rows, _total = chat.read(room)
            for m in rows:
                t = m.get("turn")
                if t:
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
    about = _turn_about()
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
    return {"node": url,
            "status": {k: (status or {}).get(k) for k in LEDGER_STATUS_KEYS},
            "transport": transport,
            "turns": turns, "cells": rows}, 200



def _api_ledger_turn(qs):
    """One turn's durable finality certificate: proxy /api/turn/<hash>/status
    on the SAME node the turns list reads (_ledger_node). The hash gate keeps
    the proxied path literal-only."""
    h = (_q1(qs, "hash") or "").strip().lower()
    if not _TURN_HASH.fullmatch(h):
        return {"error": "hash wants 64 hex chars (a turn hash)"}, 400
    from . import cell
    url = _ledger_node()
    d = cell.get_json(url + "/api/turn/%s/status" % h, timeout=3)
    if d is None:  # node down OR the node refused the hash — same degrade shape
        return {"unavailable": True, "node": url, "hash": h}, 200
    return d, 200



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
    tampered chain surfaces here as verified:false + detail, never hidden."""
    from . import pk, premise
    recs = premise.chain_records()
    verified, detail = premise.verify_chain() if recs else (True, "")
    head = recs[-1] if recs else {}
    try:
        events = pk.read_events(NATIVE_ROWS)[::-1]  # newest-first on the wire
    except Exception:
        events = []
    return {"chain": {"count": len(recs), "verified": bool(verified),
                      "detail": "" if verified else str(detail),
                      "head_index": head.get("chain_index"),
                      "head_hash": head.get("rec_hash", ""),
                      "records": [{k: r.get(k) for k in NATIVE_REC_KEYS}
                                  for r in recs[-NATIVE_ROWS:][::-1]]},
            "events": events, "chat": _native_chat_pulse()}, 200
del _web
