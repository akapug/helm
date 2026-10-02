#!/usr/bin/env python3
"""The moment ledger's writer and its outcome words, with no other import.

helm/moments.py owns the ROUTES table, the arrival classifier and the report,
and importing it pulls the prompt-shape readers with it (14 ms of CPU on a
loaded box). The PreToolUse door (helm/doors.py) appends a row on a chat post
or a rare named act, inside argv-guard, which runs on every tool call, so the
append and the words it writes live here and moments re-exports them: one
writer, one spelling, and a hook that pays only for what it writes.

Fail-open: record() never raises.
"""
import json
import os

from . import home

LEDGER_MAX = 5 * 1024 * 1024
DELIVERED = "delivered"      # the route's content reached the seat
IN_CONTEXT = "in-context"    # already delivered this context (cooled)
APPLIED = "applied"          # a budget-only route: its policy governed
DEFERRED = "deferred"        # the contract waited for a reader
SILENT = "silent"            # a zero-byte route: nothing is the right answer
OVER_CAP = "over-cap"        # applied, but the turn ran past its cap
NO_CONTENT = "no-content"    # MISSED: a content route found nothing to send
TIMED_OUT = "timed-out"      # MISSED: the hook's deadline took the turn
# A shadow route found its rule and logged it instead of saying it (the
# route status of the same name, moments.SHADOW, is spelled the same).
SHADOW = "shadow"
OUTCOMES = (DELIVERED, IN_CONTEXT, APPLIED, DEFERRED, SILENT, OVER_CAP,
            NO_CONTENT, TIMED_OUT, SHADOW)
MISSED = frozenset((NO_CONTENT, TIMED_OUT))


def ledger_path():
    return os.path.join(home.global_dir(), ".state", "moment-ledger.jsonl")


def record(rows):
    """Append moment rows (one per detected route). Fail-open, never raises;
    rotates at LEDGER_MAX with one .1 generation, the inject ledger's shape."""
    if not rows:
        return
    path = ledger_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            if os.path.getsize(path) > LEDGER_MAX:
                os.replace(path, path + ".1")
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False,
                                   separators=(",", ":")) + "\n")
    except Exception:                          # noqa: BLE001
        pass
