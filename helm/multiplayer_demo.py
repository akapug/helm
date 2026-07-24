#!/usr/bin/env python3
"""helm multiplayer demo CRDT — the reference client that turns the blind
relay's opaque log into something a human can watch. It is a last-writer-wins
keyed map: each update is ONE self-contained cell assignment, and materialize()
folds a whole log into the current map. Convergent by construction — per key the
winner is the update with the greatest (ts, id), a total order every client
computes identically, so arrival order can never change the result.

The relay never runs this. Helm stays a blind transport (multiplayer.py): the
CRDT lives in the client — this module for the CLI and terminals, mirrored in
web_ui.html for the browser, and a a future external bridge folds the same log
the same way. Non-demo opaque updates are counted, never decoded — the proof
that the transport carries strangers' bytes without understanding them.
"""
import json
import unicodedata

KIND = "helm.demo.lww"
MAX_KEY_BYTES = 200
MAX_VALUE_BYTES = 4096


def _clean(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("%s must be a non-empty string" % field)
    value = unicodedata.normalize("NFC", value.strip())
    if any(unicodedata.category(c) == "Cc" for c in value):
        raise ValueError("%s may not contain control characters" % field)
    return value


def encode(key, value, actor):
    """One opaque LWW cell assignment, ready for LocalRelay.publish. The relay
    stores the returned string undecoded; only a demo client folds it back."""
    key = _clean(key, "key")
    if len(key.encode("utf-8")) > MAX_KEY_BYTES:
        raise ValueError("key exceeds %d bytes" % MAX_KEY_BYTES)
    actor = _clean(actor, "actor")
    if not isinstance(value, str):
        raise ValueError("value must be a string")
    value = unicodedata.normalize("NFC", value)
    if len(value.encode("utf-8")) > MAX_VALUE_BYTES:
        raise ValueError("value exceeds %d bytes" % MAX_VALUE_BYTES)
    return json.dumps({"k": KIND, "key": key, "value": value, "actor": actor},
                      ensure_ascii=False, separators=(",", ":"))


def _cell(update):
    """Decode ONE opaque update to (key, value, actor), or None if it is not a
    demo cell — a corrupt string, another doc type, anything the relay carried
    for a different client. None is the honest answer: this client understands
    only its own type."""
    if not isinstance(update, str):
        return None
    try:
        row = json.loads(update)
    except (ValueError, TypeError):
        return None
    if not isinstance(row, dict) or row.get("k") != KIND:
        return None
    key, value, actor = row.get("key"), row.get("value"), row.get("actor")
    if not (isinstance(key, str) and isinstance(value, str)
            and isinstance(actor, str)):
        return None
    return key, value, actor


def materialize(updates):
    """Fold relay envelopes (the `updates` rows LocalRelay.updates returns) into
    the current LWW map. Returns {board, cells, foreign}: `board` is the map as
    a list of {key, value, actor, ts} sorted by key, `cells` its length, and
    `foreign` the count of opaque updates that were not this demo type. Feeding
    the same log in any order yields the same board — that is the convergence
    the owner watches."""
    cells, foreign = {}, 0
    for env in updates or []:
        cell = _cell(env.get("update")) if isinstance(env, dict) else None
        if cell is None:
            foreign += 1
            continue
        key, value, actor = cell
        stamp = (env.get("ts") or 0, env.get("id") or "")
        cur = cells.get(key)
        if cur is None or stamp >= cur[0]:
            cells[key] = (stamp, value, actor, env.get("ts"))
    board = [{"key": k, "value": c[1], "actor": c[2], "ts": c[3]}
             for k, c in sorted(cells.items())]
    return {"board": board, "cells": len(board), "foreign": foreign}
