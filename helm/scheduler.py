"""Pure who-waits-on-whom projection shared by visual renderers.

This module owns judgement, never layout.  It consumes land-request cards that
were already built by :mod:`helm.landreq`; it does not read the dispatch ledger,
recompute succession, or reinterpret ``owed_by``.  HTML and ASCII callers may
choose geometry, but they receive the same holder groups, ages, SUC totals,
owner asks, ordering, caps, and declared edges.
"""
import os
import re
import time

from . import pk


_HASH_ARROW_TAIL = re.compile(r"(?:\s+|/)[0-9a-f]{7,40}\s*(?:->|→).*$")
_ARROW_TAIL = re.compile(r"\s*(?:->|→).*$")
_STAGE = {
    "OPEN": "open",
    "AWAITING_BUILD": "build",
    "AWAITING_REVIEW": "review",
    "CHANGES_REQUESTED": "changes",
    "REVIEWED": "held",
    "READY": "ready",
    "MERGED_LOCAL": "landing",
}
_OWNER_STATES = {"waiting-owner", "waiting-owner-decision"}
_OWNER_HOLD_KINDS = {
    "waiting-owner": {"owner-input", "usage-reset"},
    "waiting-owner-decision": {"decision"},
}


def _owner_hold_kind(state, kind):
    """Typed queue classification; prose never mints a blocking edge."""
    return kind if isinstance(kind, str) \
        and kind in _OWNER_HOLD_KINDS.get(state, ()) else "unknown"


def plain_title(text):
    """One owner-readable title: no declared hash/arrow tail or punctuation."""
    title = str(text or "")
    title = _HASH_ARROW_TAIL.sub("", title)
    title = _ARROW_TAIL.sub("", title)
    return re.sub(r"\s{2,}", " ", title).strip(" -/(,;:")


def _age(stamp, now):
    epoch = pk.parse_ts_epoch(stamp)
    if epoch is None or now is None or epoch > now:
        return None
    return max(0, int(now - epoch))


def _ask_order(row):
    age = row.get("age_s")
    return age is None, -(age or 0), row.get("plain_title") or ""


def _card_age(card, projection_age_s):
    if card.get("dwell_known") is not True or projection_age_s is None:
        return None
    try:
        dwell = int(card.get("dwell_s"))
    except (TypeError, ValueError):
        return None
    return None if dwell < 0 else dwell + projection_age_s


def read_owner_asks():
    """Return ``(rows, unavailable)`` from the canonical integration-board queue.

    An absent optional board is proven empty.  A configured unreadable board, a
    missing queue key, or an alien queue shape is UNKNOWN.  Informational rows
    are not owner debt.  No age is computed here: the caller supplies the clock
    to :func:`project`, keeping this extraction reusable and deterministic.
    """
    from . import board

    path = board.path()
    try:
        os.lstat(path)
    except FileNotFoundError:
        return [], None
    except OSError as e:
        return [], "board at %s cannot be inspected (%s)" % (
            path, type(e).__name__)
    body = pk.read_json(path)
    if not isinstance(body, dict):
        return [], "board at %s is unreadable or not a JSON object" % path
    if "owner_gated_queue" not in body:
        return [], "board has no owner_gated_queue key"
    queue = body["owner_gated_queue"]
    if not isinstance(queue, list):
        return [], "owner_gated_queue is a %s, not a list" % type(queue).__name__
    rows, malformed = [], []
    for index, record in enumerate(queue):
        if not isinstance(record, dict):
            malformed.append(index)
            continue
        state = record.get("state")
        if not isinstance(state, str):
            malformed.append(index)
            continue
        if state not in _OWNER_STATES:
            continue
        ask, why, since = (record.get("ask"), record.get("why", ""),
                           record.get("since", ""))
        if not all(isinstance(value, str) for value in (ask, why, since)):
            malformed.append(index)
            continue
        ask = " ".join(ask.split())
        if not ask:
            malformed.append(index)
            continue
        kind = record.get("hold_kind")
        rows.append({"ask": ask, "why": " ".join(why.split()),
                     "state": state, "since": since,
                     "hold_kind": kind if isinstance(kind, str) else None})
    rows.sort(key=lambda row: (
        pk.parse_ts_epoch(row["since"]) is None,
        pk.parse_ts_epoch(row["since"]) or 0,
        row["ask"]))
    error = None if not malformed else \
        "owner_gated_queue has malformed actionable row%s: %s" % (
            "" if len(malformed) == 1 else "s", ", ".join(map(str, malformed)))
    return rows, error


def _empty(unavailable, asks, asks_error, asks_dropped=0):
    return {"unavailable": unavailable,
            "row_count": None, "active_count": None,
            "suc": {"stalled": None, "unmeasurable": None,
                    "contrary": None, "total": None, "zero": None},
            "groups": [], "owner_holds": [], "owner_hold_count": None,
            "edges": [{"kind": "waits_on_owner_" +
                        ask["hold_kind"].replace("-", "_"),
                        "from": ask["id"], "to": "owner"}
                       for ask in asks],
            "owner_asks": asks,
            "owner_ask_count": None if asks_error else len(asks) + asks_dropped,
            "owner_asks_dropped": asks_dropped,
            "owner_asks_unavailable": asks_error,
            "projection_age_s": None}


def project(cards, stalled_ids=(), unmeasurable=(), active_ids=None,
            owner_asks=(), owner_asks_unavailable=None, now=None,
            projection_age_s=None, limit_per_group=None, owner_limit=None,
            unavailable=None):
    """Project already-built cards into one renderer-neutral scheduler model.

    Every card whose ``terminal`` field is exactly ``False`` appears in exactly
    one holder group.  ``holder_role``/``holder_seat`` are copied as display
    authority; raw ``owed_by`` remains beside them as enforcement truth.
    Missing holder authority becomes an explicit UNKNOWN group.  The only work
    identity edge is the record-declared ``supersedes`` link; ``waits_on`` edges
    bind each row to its already-resolved holder group and make no lifecycle
    inference.
    """
    now = time.time() if now is None else now
    try:
        projection_age_s = int(projection_age_s) \
            if projection_age_s is not None else None
        if projection_age_s is not None and projection_age_s < 0:
            projection_age_s = None
    except (TypeError, ValueError):
        projection_age_s = None
    asks, asks_error = [], owner_asks_unavailable
    for raw in owner_asks or ():
        if not isinstance(raw, dict):
            if not asks_error:
                asks_error = "actionable owner ask is not an object"
            continue
        state = raw.get("state")
        if not isinstance(state, str):
            if not asks_error:
                asks_error = "actionable owner ask has a non-string state"
            continue
        if state not in _OWNER_STATES:
            continue
        ask, why, since = (raw.get("ask"), raw.get("why", ""),
                           raw.get("since", ""))
        if not all(isinstance(value, str) for value in (ask, why, since)):
            if not asks_error:
                asks_error = "actionable owner ask has a non-string field"
            continue
        ask, why = " ".join(ask.split()), " ".join(why.split())
        title = plain_title(ask)
        if not title:
            if not asks_error:
                asks_error = "actionable owner ask has no readable title"
            continue
        age = _age(since, now)
        kind = _owner_hold_kind(state, raw.get("hold_kind"))
        if kind == "unknown" and not asks_error:
            asks_error = "actionable owner ask has no valid typed hold_kind"
        asks.append({"owner_ask": True, "ask": ask, "why": why,
                     "title": ask, "plain_title": title, "detail": why,
                     "state": state, "hold_kind": kind, "since": since,
                     "age_s": age, "age_known": age is not None})
    asks.sort(key=_ask_order)
    for index, ask in enumerate(asks):
        ask["id"] = "owner-ask-%d" % index
    asks_dropped = 0
    if owner_limit is not None and len(asks) > max(0, int(owner_limit)):
        keep = max(0, int(owner_limit))
        asks_dropped = len(asks) - keep
        asks = asks[:keep]
    if unavailable:
        return _empty(str(unavailable), asks, asks_error, asks_dropped)
    if not isinstance(cards, (list, tuple)):
        return _empty("scheduler rows are not a list", asks,
                      asks_error, asks_dropped)

    reasons = {}
    for item in unmeasurable or ():
        if isinstance(item, dict) and item.get("id"):
            reasons[str(item["id"])] = str(item.get("reason") or "unmeasurable")
    stalled = {str(value) for value in (stalled_ids or ())}
    seen = set()
    rows = []
    for card in cards:
        if not isinstance(card, dict):
            return _empty("scheduler row is not an object", asks,
                          asks_error, asks_dropped)
        if not isinstance(card.get("terminal"), bool):
            return _empty("scheduler row %s has no terminal truth" %
                          (card.get("id") or "?"), asks,
                          asks_error, asks_dropped)
        if card["terminal"]:
            continue
        rid = card.get("id")
        if not isinstance(rid, str) or not rid:
            return _empty("scheduler row has no typed id", asks,
                          asks_error, asks_dropped)
        if rid in seen:
            return _empty("scheduler row id %s is duplicated" % rid, asks,
                          asks_error, asks_dropped)
        seen.add(rid)
        role = card.get("holder_role")
        role = str(role).strip() if isinstance(role, str) and role.strip() else "unknown"
        seat = card.get("holder_seat")
        seat = str(seat).strip() if isinstance(seat, str) and seat.strip() else None
        if role == "unknown":
            seat = None                 # a seat cannot out-authority its role
        state = str(card.get("state") or "")
        owner_gated = card.get("owner_gated") is True
        age = _age(card.get("hold_ts"), now) if owner_gated \
            else _card_age(card, projection_age_s)
        owed_present = "owed_by" in card
        owed = card.get("owed_by")
        owed = str(owed).strip() if isinstance(owed, str) and owed.strip() else None
        row = {"id": rid,
               "chain_root": str(card.get("chain_root") or rid),
               "supersedes": str(card.get("supersedes") or "") or None,
               "lane": str(card.get("lane") or "(no lane)"),
               "title": str(card.get("lane") or "(no lane)"),
               "plain_title": plain_title(card.get("lane") or "(no lane)"),
               "state": state,
               "stage_class": _STAGE.get(state, "unknown"),
               "owed_by": owed, "owed_by_present": owed_present,
               "owed_by_known": owed is not None,
               "holder_role": role, "holder_seat": seat,
               "age_s": age, "age_known": age is not None,
               "stalled": rid in stalled,
               "unmeasurable": rid in reasons,
               "unmeasurable_reason": reasons.get(rid),
               "contrary": bool(card.get("contrary")),
               "honored": bool(card.get("honored")),
               "owner_gated": owner_gated,
               "owner_ask": False,
               "detail": " ".join(str(card.get("hold_reason") or "").split())}
        rows.append(row)

    if not isinstance(active_ids, (list, tuple)):
        return _empty("scheduler active membership is not a list", asks,
                      asks_error, asks_dropped)
    if any(not isinstance(value, str) or not value for value in active_ids):
        return _empty("scheduler active membership has an untyped id", asks,
                      asks_error, asks_dropped)
    active_list = list(active_ids)
    if len(active_list) != len(set(active_list)):
        return _empty("scheduler active membership contains duplicate ids", asks,
                      asks_error, asks_dropped)
    unknown_active = sorted(set(active_list) - seen)
    if unknown_active:
        return _empty("scheduler active membership names unknown row %s" %
                      unknown_active[0], asks, asks_error, asks_dropped)
    active_set = set(active_list)
    for row in rows:
        row["active"] = row["id"] in active_set
    grouped = {}
    for row in rows:
        key = (row["holder_role"], row["holder_seat"])
        grouped.setdefault(key, []).append(row)
    def _group_order(key):
        members = grouped[key]
        active = [row for row in members if row["id"] in active_set]
        suc_total = sum(row["stalled"] + row["unmeasurable"] + row["contrary"]
                        for row in active)
        known = [row["age_s"] for row in members if row["age_s"] is not None]
        oldest = max(known) if known else None
        label = "%s @%s" % key if key[1] else key[0]
        return (-suc_total, oldest is None, -(oldest or 0), label)

    keys = sorted(grouped, key=_group_order)
    groups = []
    edges = [{"kind": "waits_on_owner_" + ask["hold_kind"].replace("-", "_"),
              "from": ask["id"], "to": "owner"}
             for ask in asks]
    for index, key in enumerate(keys):
        role, seat = key
        full = sorted(grouped[key], key=lambda row: (
            row["age_s"] is None, -(row["age_s"] or 0), row["id"]))
        dropped = 0
        shown = full
        if limit_per_group is not None and len(full) > max(0, int(limit_per_group)):
            keep = max(0, int(limit_per_group))
            dropped = len(full) - keep
            shown = full[:keep]
        gid = "holder-%d" % index
        active_here = [row for row in full if row["id"] in active_set]
        known_ages = [row["age_s"] for row in full if row["age_s"] is not None]
        group_suc = {"stalled": sum(row["stalled"] for row in active_here),
                     "unmeasurable": sum(row["unmeasurable"] for row in active_here),
                     "contrary": sum(row["contrary"] for row in active_here)}
        group_suc["total"] = sum(group_suc.values())
        groups.append({"id": gid, "holder_role": role, "holder_seat": seat,
                       "label": "%s @%s" % (role, seat) if seat else role,
                       "count": len(full), "dropped": dropped,
                       "oldest_age_s": max(known_ages) if known_ages else None,
                       "suc": group_suc, "rows": shown})
        for row in full:
            if row["active"]:
                edges.append({"kind": "waits_on", "from": row["id"], "to": gid,
                              "stage_class": row["stage_class"]})
            if row["supersedes"]:
                edges.append({"kind": "supersedes", "from": row["id"],
                              "to": row["supersedes"],
                              "target_known": row["supersedes"] in seen})
    active = [row for row in rows if row["active"]]
    owner_holds = sorted(
        [row for row in active if row["holder_role"] == "owner"],
        key=lambda row: (row["age_s"] is None, -(row["age_s"] or 0), row["id"]))
    suc = {"stalled": sum(row["id"] in stalled for row in active),
           "unmeasurable": sum(row["id"] in reasons for row in active),
           "contrary": sum(row["contrary"] for row in active)}
    suc["total"] = sum(suc.values())
    suc["zero"] = suc["total"] == 0
    return {"unavailable": None, "row_count": len(rows),
            "active_count": len(active), "suc": suc,
            "groups": groups, "edges": edges,
            "owner_holds": owner_holds, "owner_hold_count": len(owner_holds),
            "owner_asks": asks, "owner_ask_count": None if asks_error else len(asks) + asks_dropped,
            "owner_asks_dropped": asks_dropped,
            "owner_asks_unavailable": asks_error,
            "projection_age_s": projection_age_s}
